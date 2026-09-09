"""Optimizer for PV Excess Control. Pure logic engine - no Home Assistant dependencies.

Runs 5 phases per cycle:
  Phase 1:   ASSESS  - Calculate averaged excess, apply hysteresis, check constraints
  Phase 2:   ALLOCATE - Assign excess power to appliances by priority
  Phase 2.5: PREEMPT - Shed lower-priority ON appliances to start higher-priority IDLE ones
  Phase 3:   SHED - Reduce/turn off lowest-priority appliances when excess is negative
  Phase 4:   BATTERY DISCHARGE PROTECTION - Limit battery discharge for big consumers
"""
from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.pv_excess_control.const import (
    DEFAULT_DYNAMIC_ON_THRESHOLD,
    DEFAULT_GRID_VOLTAGE,
    DEFAULT_OFF_THRESHOLD,
    DEFAULT_ON_THRESHOLD,
)
from custom_components.pv_excess_control.models import (
    Action,
    ApplianceConfig,
    runtime_demand_seconds,
    runtime_protected,
    ApplianceState,
    BatteryDischargeAction,
    ControlDecision,
    OptimizerResult,
    Plan,
    PowerState,
    TariffInfo,
)
from custom_components.pv_excess_control.status_formatter import format_duration

_LOGGER = logging.getLogger(__name__)


def _step_floor(value: float, step: float) -> float:
    """Round down to nearest multiple of step."""
    return math.floor(value / step) * step


def recent_power_history(
    history: list[PowerState], seconds: float, anchor: datetime,
) -> list[PowerState]:
    """Select snapshots in the trailing window, preserving their input order.

    The lower boundary is exclusive; the current snapshot is included. Legacy
    naive timestamps represent UTC, just like the coordinator's aware values.
    Future samples never contribute to a past controller cycle.
    """
    anchor = anchor if anchor.tzinfo is not None else anchor.replace(tzinfo=timezone.utc)
    return [sample for sample in history
            if 0 <= (anchor - (sample.timestamp if sample.timestamp.tzinfo is not None
                              else sample.timestamp.replace(tzinfo=timezone.utc))).total_seconds() < seconds]


class Optimizer:
    """Pure-logic optimization engine.

    Takes power state, appliance configs/states, plan, and tariff data as input.
    Returns control decisions and battery actions as output.
    No side effects, no HA dependencies.
    """

    def __init__(
        self,
        grid_voltage: int = DEFAULT_GRID_VOLTAGE,
        timezone_str: str | None = None,
        enable_preemption: bool = True,
        off_threshold: int = DEFAULT_OFF_THRESHOLD,
        min_good_samples: int = 3,
    ) -> None:
        self.grid_voltage = grid_voltage
        self._tz = ZoneInfo(timezone_str) if timezone_str else None
        self.enable_preemption = enable_preemption
        self._off_threshold = off_threshold
        self._min_good_samples = min_good_samples
        # Per-cycle accounting; initialized for direct allocation callers too.
        self._plan_influence: str = "none"
        self._grid_allowed_ids: set[str] = set()
        self._running_grid_credit: dict[str, float] = {}
        self._running_solar_share: dict[str, float] = {}
        self._initial_grid_instant_budget: float | None = None

    def optimize(
        self,
        power_state: PowerState,
        appliances: list[ApplianceConfig],
        appliance_states: list[ApplianceState],
        plan: Plan,
        power_history: list[PowerState],
        tariff: TariffInfo,
        plan_influence: str = "none",
        min_battery_soc: float | None = None,
        force_charge: bool = False,
        auto_grid_charge_engaged: bool = False,
    ) -> OptimizerResult:
        """Run the optimization cycle and return decisions.

        Phase 1:   ASSESS - compute averaged excess, apply hysteresis
        Phase 2:   ALLOCATE - assign excess to appliances by priority
        Phase 2.5: PREEMPT - shed lower-priority ON appliances for higher-priority IDLE ones
        Phase 3:   SHED - reduce/turn off lowest priority when over-budget
        Phase 4:   BATTERY DISCHARGE PROTECTION - limit discharge for big consumers
        """
        self._plan_influence = plan_influence
        self._grid_allowed_ids = {a.id for a in appliances if self._grid_allowed(a, tariff)}

        # Build lookup of appliance states by ID
        state_by_id: dict[str, ApplianceState] = {
            s.appliance_id: s for s in appliance_states
        }

        # Dependency resolution lookups
        self._config_by_id: dict[str, ApplianceConfig] = {a.id: a for a in appliances}
        self._state_by_id = state_by_id
        self._pending_dep_decisions: dict[str, ControlDecision] = {}

        # Reverse dependency map: dependency_id -> [dependent_ids]
        self._reverse_deps: dict[str, list[str]] = {}
        for a in appliances:
            if a.requires_appliance and a.requires_appliance in self._config_by_id:
                self._reverse_deps.setdefault(a.requires_appliance, []).append(a.id)

        # Phase 1: ASSESS
        avg_excess = self._calculate_average_excess(power_history)

        _LOGGER.debug(
            "Optimizer start: %d appliances, avg_excess=%s, current_excess=%s",
            len(appliances),
            f"{avg_excess:.0f}W" if avg_excess is not None else "unavailable",
            f"{power_state.excess_power:.0f}W" if power_state.excess_power is not None else "unavailable",
        )

        # Sort appliances by (helper_only, priority, id):
        #   - non-helpers (helper_only=False=0) first, helpers (=1) last
        #   - within each group, by priority (1=highest)
        #   - secondary sort by id ensures deterministic ordering for ties
        # Hoisted above the None-routing so both the normal path and the
        # safety-only path use the same deterministic order. Helpers must
        # evaluate AFTER their dependents so the helper-only short-circuit
        # in _apply_safety_rules can find dependent decisions in the
        # in-progress decisions list.
        sorted_appliances = sorted(appliances, key=lambda a: (a.helper_only, a.priority, a.id))

        # If ASSESS produced no trustworthy excess, take the safety-only
        # path: run safety checks and Phase 4, skip Phases 2/2.5/3.
        if avg_excess is None:
            return self._optimize_safety_only(
                state_by_id=state_by_id,
                sorted_appliances=sorted_appliances,
                power_state=power_state,
                min_battery_soc=min_battery_soc,
                force_charge=force_charge,
                auto_grid_charge_engaged=auto_grid_charge_engaged,
            )

        # Pre-compute per-appliance averaged excess for those with custom windows.
        # If the narrow window has fewer than min_good_samples good samples,
        # _calculate_average_excess returns None — we simply do not record a
        # per-appliance entry, so the allocation loop below uses the main
        # avg_budget fallback (the same branch as appliances with no
        # custom window).
        self._appliance_avg_excess: dict[str, float] = {}
        for app in appliances:
            if app.averaging_window is not None and app.averaging_window > 0:
                recent = recent_power_history(power_history, app.averaging_window, power_state.timestamp)
                per_app_avg = self._calculate_average_excess(recent)
                if per_app_avg is not None:
                    self._appliance_avg_excess[app.id] = per_app_avg

        # Phase 2: ALLOCATE — dual budget model.
        # avg_budget tracks the averaged excess (used for turn-on gates
        # and as the bump-ceiling in already-ON dynamic-current
        # adjustments). instant_budget tracks the instantaneous excess
        # (used by Phase 3 SHED and as the reactive reading for dynamic
        # current reductions). Both are debited by the same power_delta
        # for each allocation and stay in lockstep relative to each
        # other — only their starting points differ.
        decisions: list[ControlDecision] = []
        avg_budget: float = avg_excess
        instant_budget: float = (
            power_state.excess_power
            if power_state.excess_power is not None
            else avg_excess
        )

        self._running_grid_credit = self._running_grid_credits(
            sorted_appliances, state_by_id, instant_budget, tariff,
        )
        grid_credit = sum(self._running_grid_credit.values())
        avg_budget += grid_credit
        instant_budget += grid_credit
        self._initial_grid_instant_budget = instant_budget

        total_consumed = 0.0  # Track total power consumed by all appliances
        for appliance in sorted_appliances:
            state = state_by_id.get(appliance.id)
            if state is None:
                # No state found for this appliance - skip with IDLE
                decisions.append(ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.IDLE,
                    target_current=None,
                    reason="No state data available",
                    overrides_plan=False,
                ))
                continue

            # Use per-appliance averaged excess if configured, adjusted by prior consumption
            if appliance.id in self._appliance_avg_excess:
                app_avg_budget = self._appliance_avg_excess[appliance.id] + grid_credit - total_consumed
            else:
                app_avg_budget = avg_budget

            decision, power_consumed = self._allocate_appliance(
                appliance, state, app_avg_budget, instant_budget, plan, tariff,
                decisions=decisions,
                state_by_id=state_by_id,
            )
            decision, power_consumed = self._limit_current_increase(
                appliance, state, decision, power_consumed,
            )
            decisions.append(decision)
            _LOGGER.debug(
                "  Allocate %s (p=%d, %sW): avg=%.0fW inst=%.0fW -> %s (%s)",
                appliance.name, appliance.priority, appliance.nominal_power,
                avg_budget, instant_budget, decision.action, decision.reason,
            )
            avg_budget -= power_consumed
            instant_budget -= power_consumed
            total_consumed += power_consumed

        # Inject dependency decisions (replace IDLE decisions for dependencies)
        for dep_id, dep_decision in self._pending_dep_decisions.items():
            for i, d in enumerate(decisions):
                if d.appliance_id == dep_id and d.action == Action.IDLE:
                    decisions[i] = dep_decision
                    break

        # Phase 2.5: PREEMPT - shed lower-priority for higher-priority.
        # PREEMPT is a turn-on decision, so feasibility checks read
        # avg_budget. It mutates instant_budget in lockstep: both are
        # threaded through and returned.
        if self.enable_preemption:
            avg_budget, instant_budget = self._preempt(
                decisions, sorted_appliances, state_by_id,
                avg_budget, instant_budget,
            )

        # Phase 3: SHED — reads the instantaneous budget (physical reality).
        instant_budget = self._shed(
            decisions, sorted_appliances, state_by_id, instant_budget,
            force_shed=force_charge,
        )

        # Phase 4: BATTERY DISCHARGE PROTECTION
        battery_action = self._battery_discharge_protection(
            decisions, sorted_appliances,
            battery_soc=power_state.battery_soc,
            min_battery_soc=min_battery_soc,
            force_charge=force_charge,
            auto_grid_charge_engaged=auto_grid_charge_engaged,
        )

        return OptimizerResult(
            decisions=decisions,
            battery_discharge_action=battery_action,
        )

    def _limit_current_increase(
        self, appliance: ApplianceConfig, state: ApplianceState,
        decision: ControlDecision, power_delta: float,
    ) -> tuple[ControlDecision, float]:
        """Hold small/frequent automatic increases before allocating the next load.

        Reductions, starts, overrides and out-of-range setpoint repairs always
        proceed. Recompute both demand and funded imports when holding current:
        releasing a proposed grid-funded increase must never create solar credit.
        """
        current = state.current_amperage
        target = decision.target_current
        if (not state.is_on or decision.action != Action.SET_CURRENT
                or appliance.override_active or decision.overrides_plan
                or decision.bypasses_cooldown or current is None or target is None
                or not math.isfinite(current)
                or not appliance.min_current <= current <= appliance.max_current
                or target <= current):
            return decision, power_delta
        elapsed = state.seconds_since_current_change
        too_soon = (elapsed is not None and appliance.current_update_interval > 0
                    and elapsed < appliance.current_update_interval)
        too_small = target - current + 1e-9 < appliance.current_min_change
        if not too_soon and not too_small:
            return decision, power_delta
        watts_per_amp = self.grid_voltage * max(appliance.phases, 1)
        proposed_power = target * watts_per_amp
        held_power = current * watts_per_amp
        proposed_grid = decision.grid_supplement_watts
        held_grid = max(held_power - (proposed_power - proposed_grid), 0.0)
        power_delta += held_power - proposed_power - (held_grid - proposed_grid)
        reason = ("Current increase waiting for update interval" if too_soon
                  else "Current increase below minimum change")
        return replace(decision, target_current=current, reason=reason,
                       grid_supplement_watts=held_grid,
                       uses_grid_supplement=held_grid > 0), power_delta

    def _calculate_average_excess(self, power_history: list[PowerState]) -> float | None:
        """Calculate the average excess power from the history window.

        Returns ``None`` when fewer than ``self._min_good_samples``
        good samples are available. A "good" sample is one whose
        ``excess_power`` is not ``None``, not NaN, and not Infinity.

        ``None`` signals to the optimizer's main entry that the
        current excess is untrustworthy and Phase 2/3 should be
        skipped. See the safety-only path in ``optimize()``.
        """
        good_samples = [
            ps.excess_power
            for ps in power_history
            if ps.excess_power is not None
            and not math.isnan(ps.excess_power)
            and not math.isinf(ps.excess_power)
        ]
        if len(good_samples) < self._min_good_samples:
            return None
        return sum(good_samples) / len(good_samples)

    def _plan_says_on(self, appliance_id: str, plan: Plan) -> bool:
        """Check if the plan has an ON entry for this appliance in the current time window."""
        from datetime import datetime
        # Use HA timezone if configured, otherwise fall back to system local timezone.
        now = datetime.now(self._tz) if self._tz else datetime.now().astimezone()
        for entry in plan.entries:
            if entry.appliance_id != appliance_id:
                continue
            if entry.action in (Action.ON, Action.SET_CURRENT):
                if entry.window:
                    window_start = entry.window.start
                    window_end = entry.window.end
                    try:
                        if window_start <= now <= window_end:
                            return True
                    except TypeError:
                        # Mixed naive/aware — compare as naive
                        now_naive = now.replace(tzinfo=None)
                        start_naive = window_start.replace(tzinfo=None) if window_start.tzinfo else window_start
                        end_naive = window_end.replace(tzinfo=None) if window_end.tzinfo else window_end
                        if start_naive <= now_naive <= end_naive:
                            return True
        return False

    def _has_running_dependent(
        self,
        helper_id: str,
        decisions: list[ControlDecision],
        state_by_id: dict[str, ApplianceState],
    ) -> bool:
        """Return True if any appliance with requires_appliance=helper_id is
        running this cycle.

        "Running this cycle" means either:
          - The dependent has an ON or SET_CURRENT decision in the in-progress
            ``decisions`` list (normal allocation path).
          - The dependent has no decision yet OR has an IDLE decision, AND is
            currently physically ON (safety-only path, or transient IDLE from
            a safety rule like max_daily_activations — in both cases the
            controller treats IDLE as "no change this cycle").

        An ``Action.OFF`` decision is treated as authoritative: even if HA
        still reports the dependent as physically on (state lag), we respect
        the optimizer's intent to stop it and return False for that dependent.
        """
        dependent_ids = self._reverse_deps.get(helper_id, [])
        if not dependent_ids:
            return False
        decision_by_id = {d.appliance_id: d for d in decisions}
        for dep_id in dependent_ids:
            dec = decision_by_id.get(dep_id)
            if dec is not None and dec.action in (Action.ON, Action.SET_CURRENT):
                return True
            if dec is not None and dec.action == Action.OFF:
                # Authoritative OFF: optimizer has decided to stop this dependent.
                # Do not fall back to state (HA may still report is_on=True due
                # to lag; we trust the optimizer's intent).
                continue
            # dec is None (safety-only path, no rule fired) OR
            # dec.action == Action.IDLE (no change this cycle → trust current state).
            # In both cases, fall back to the dependent's physical state.
            dep_state = state_by_id.get(dep_id)
            if dep_state is not None and dep_state.is_on:
                return True
        return False

    def _apply_safety_rules(
        self,
        appliance: ApplianceConfig,
        state: ApplianceState,
        decisions: list[ControlDecision],
        state_by_id: dict[str, ApplianceState],
    ) -> tuple[ControlDecision, float] | None:
        """Apply excess-independent safety rules to a single appliance.

        Checks (in order): max_daily_runtime, max_daily_activations,
        manual override, helper_only short-circuit, EV connected,
        EV SoC target, on_only, dependency availability, time window.

        ``decisions`` and ``state_by_id`` are needed for the helper_only
        short-circuit to inspect dependent state. Other rules ignore them.

        Returns a ``(ControlDecision, power_consumed)`` tuple if any
        rule fires, or ``None`` if no safety rule applies (allocation
        logic should proceed).

        Used by both the normal allocation path and the safety-only
        path triggered when ASSESS returns None.
        """
        # Max daily runtime check: if exceeded, turn OFF (safety limit - overrides everything)
        if (
            appliance.max_daily_runtime is not None
            and state.runtime_today >= appliance.max_daily_runtime
        ):
            if appliance.on_only:
                _LOGGER.info("Max daily runtime overrides on_only for %s", appliance.name)
            if appliance.override_active:
                _LOGGER.info("Max daily runtime overrides manual override for %s", appliance.name)
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.OFF,
                    target_current=None,
                    reason=f"Max daily runtime reached ({state.runtime_today} >= {appliance.max_daily_runtime})",
                    overrides_plan=False,
                    bypasses_cooldown=True,
                ),
                0.0,
            )

        # Max daily activations check: if reached and appliance is OFF, block turn-on
        if (
            appliance.max_daily_activations is not None
            and not state.is_on
            and state.activations_today >= appliance.max_daily_activations
        ):
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.IDLE,
                    target_current=None,
                    reason=f"Max daily activations reached ({state.activations_today}/{appliance.max_daily_activations})",
                    overrides_plan=False,
                    bypasses_cooldown=True,
                ),
                0.0,
            )

        # Manual override check (runs before EV connected check so overrides work
        # even when EV is disconnected — user explicitly asked to override)
        if appliance.override_active:
            if appliance.dynamic_current and appliance.current_entity:
                # Dynamic current appliance: set to max current instead of plain ON
                phases = max(appliance.phases, 1)
                if appliance.ev_connected_entity and state.ev_connected is False:
                    # Presetting an unplugged charger creates no new demand,
                    # regardless of whether its enabled switch is ON or OFF.
                    power_consumed = 0.0
                elif state.is_on:
                    current_power = (max(state.current_power, 0.0)
                                     if state.current_power_available else 0.0)
                    target_power = appliance.max_current * self.grid_voltage * phases
                    power_consumed = max(target_power - current_power, 0.0)
                else:
                    power_consumed = appliance.max_current * self.grid_voltage * phases
                return (
                    ControlDecision(
                        appliance_id=appliance.id,
                        action=Action.SET_CURRENT,
                        target_current=appliance.max_current,
                        reason="Manual override active (dynamic current at max)",
                        overrides_plan=True,
                    ),
                    power_consumed,
                )
            power_consumed = appliance.nominal_power if not state.is_on else 0.0
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.ON,
                    target_current=None,
                    reason="Manual override active",
                    overrides_plan=True,
                ),
                power_consumed,
            )

        # Helper-only short-circuit: appliance never runs on its own,
        # only as a slave to its dependents. Placed AFTER manual override
        # (override always wins) and BEFORE EV connected / on_only / time
        # window so those rules don't apply to helpers (helpers have no
        # agency of their own — see design spec for rationale).
        if appliance.helper_only:
            if self._has_running_dependent(appliance.id, decisions, state_by_id):
                return (
                    ControlDecision(
                        appliance_id=appliance.id,
                        action=Action.ON,
                        target_current=None,
                        reason="Helper-only: dependent is running",
                        overrides_plan=False,
                    ),
                    0.0,  # Power already credited via dep injection in _allocate_on
                )
            else:
                action = Action.OFF if state.is_on else Action.IDLE
                return (
                    ControlDecision(
                        appliance_id=appliance.id,
                        action=action,
                        target_current=None,
                        reason="Helper-only: no dependent running",
                        overrides_plan=False,
                        bypasses_cooldown=True,
                    ),
                    0.0,
                )

        # EV connected check: only proceed if ev_connected is explicitly True.
        # If the sensor is unavailable (None) or disconnected (False), stop if ON or skip if OFF.
        # Placed after override check so manual overrides still work for disconnected EVs.
        if appliance.ev_connected_entity and state.ev_connected is not True:
            action = Action.OFF if state.is_on else Action.IDLE
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=action,
                    target_current=None,
                    reason="EV not confirmed connected (sensor: %s)" % (
                        "unavailable" if state.ev_connected is None else "disconnected"
                    ),
                    overrides_plan=False,
                    bypasses_cooldown=True,
                ),
                0.0,
            )

        # EV SoC target check: stop charging when target reached
        # Placed before on_only so that EV SoC target is respected even for on_only appliances
        if (appliance.ev_target_soc is not None
                and state.ev_soc is not None
                and state.ev_soc >= appliance.ev_target_soc):
            action = Action.OFF if state.is_on else Action.IDLE
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=action,
                    target_current=None,
                    reason=f"EV SoC target reached ({state.ev_soc:.0f}% >= {appliance.ev_target_soc:.0f}%)",
                    overrides_plan=False,
                    bypasses_cooldown=True,
                ),
                0.0,
            )

        if (appliance.enable_condition_entity and state.enable_condition is not True
                and (not state.is_on or appliance.enable_condition_mode == "while_running")):
            return ControlDecision(
                appliance.id, Action.OFF if state.is_on else Action.IDLE, None,
                "Enable condition is not confirmed on", False, bypasses_cooldown=True,
            ), 0.0

        if appliance.remaining_runtime_entity:
            demand = runtime_demand_seconds(appliance, state)
            if demand == 0 or (demand is None and not state.is_on):
                action = Action.ON if state.is_on and appliance.on_only else Action.OFF if state.is_on else Action.IDLE
                return ControlDecision(
                    appliance.id, action, None,
                    "Remaining runtime complete" if demand == 0 else "Remaining runtime unavailable",
                    False, bypasses_cooldown=True,
                ), 0.0

        # Fixed on-only loads retain their supply; dynamic loads still regulate.
        if appliance.on_only and state.is_on and not appliance.dynamic_current:
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.ON,
                    target_current=None,
                    reason="on_only appliance - staying on",
                    overrides_plan=False,
                ),
                0.0,  # Already consuming power, no new allocation needed
            )

        # Dependency availability check
        # Note: this site deliberately does NOT set bypasses_cooldown=True.
        # A missing dependency is a configuration condition (the user
        # disabled or removed the dependency), not a runtime safety
        # constraint, and the existing behavior — respect the switch
        # interval — is the conservative default. If we ever want
        # immediate-OFF semantics here, set the flag and update the
        # bypass-flag test class.
        if appliance.requires_appliance:
            dep_config = self._config_by_id.get(appliance.requires_appliance)
            dep_state = state_by_id.get(appliance.requires_appliance)
            dep_gate_blocked = (
                dep_config is not None and dep_config.enable_condition_entity
                and not dep_config.override_active
                and (dep_state is None or dep_state.enable_condition is not True)
                and (dep_state is None or not dep_state.is_on or dep_config.enable_condition_mode == "while_running")
            )
            dep_delay_pending = (
                dep_config is not None and dep_state is not None
                and not dep_state.is_on and not dep_config.override_active
                and dep_config.start_delay > dep_state.solar_start_elapsed
            )
            dep_runtime_blocked = (
                dep_config is not None and dep_config.remaining_runtime_entity
                and not dep_config.override_active
                and (dep_state is None
                     or (runtime_demand_seconds(dep_config, dep_state) is None and not dep_state.is_on)
                     or (runtime_demand_seconds(dep_config, dep_state) == 0
                         and not (dep_state.is_on and dep_config.on_only)))
            )
            if dep_config is None or dep_gate_blocked or dep_delay_pending or dep_runtime_blocked:
                action = Action.OFF if state.is_on else Action.IDLE
                return (
                    ControlDecision(
                        appliance_id=appliance.id, action=action, target_current=None,
                        reason=f"Dependency '{appliance.requires_appliance}' unavailable (disabled, removed, condition/runtime blocked or start delay pending)",
                        overrides_plan=False,
                    ),
                    0.0,
                )

        # Time window check: restrict appliance to specific operating hours
        if appliance.start_after is not None or appliance.end_before is not None:
            from datetime import datetime
            current_time = datetime.now(self._tz).time() if self._tz else datetime.now().time()
            if not self._is_within_time_window(current_time, appliance.start_after, appliance.end_before):
                action = Action.OFF if state.is_on else Action.IDLE
                # Build a descriptive reason
                window_parts: list[str] = []
                if appliance.start_after is not None:
                    window_parts.append(f"after {appliance.start_after.strftime('%H:%M')}")
                if appliance.end_before is not None:
                    window_parts.append(f"before {appliance.end_before.strftime('%H:%M')}")
                window_desc = " and ".join(window_parts)
                return (
                    ControlDecision(
                        appliance_id=appliance.id,
                        action=action,
                        target_current=None,
                        reason=f"Outside operating window ({window_desc})",
                        overrides_plan=False,
                        bypasses_cooldown=True,
                    ),
                    0.0,
                )

        return None

    def _optimize_safety_only(
        self,
        state_by_id: dict[str, ApplianceState],
        sorted_appliances: list[ApplianceConfig],
        power_state: PowerState,
        min_battery_soc: float | None,
        *,
        force_charge: bool = False,
        auto_grid_charge_engaged: bool = False,
    ) -> OptimizerResult:
        """Run safety checks and Phase 4 only; skip Phase 2/2.5/3.

        Called when ASSESS returns None. Appliances with no safety
        rule firing receive no decision in the result — the
        controller treats 'absent from result' as 'no change this
        cycle', keeping currently-ON appliances ON without Phase 3
        SHED deciding to turn them off on untrustworthy data.

        Exception: currently-ON big consumers that pass all safety
        checks emit an ON decision so Phase 4 (battery discharge
        protection) can see them and apply discharge limits.
        """
        decisions: list[ControlDecision] = []
        for appliance in sorted_appliances:
            state = state_by_id.get(appliance.id)
            if state is None:
                decisions.append(ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.IDLE,
                    target_current=None,
                    reason="No state data available",
                    overrides_plan=False,
                ))
                continue
            safety_result = self._apply_safety_rules(
                appliance, state,
                decisions=decisions,
                state_by_id=state_by_id,
            )
            if safety_result is not None:
                decision, _ = safety_result
                decisions.append(decision)
            elif state.is_on and appliance.is_big_consumer:
                # Emit a hold-ON decision so Phase 4 can see this big consumer
                # and apply battery discharge limits. Without a decision entry
                # _battery_discharge_protection would not detect it as active.
                decisions.append(ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.ON,
                    target_current=None,
                    reason="Excess unavailable - holding state",
                    overrides_plan=False,
                ))

        battery_action = self._battery_discharge_protection(
            decisions, sorted_appliances,
            battery_soc=power_state.battery_soc,
            min_battery_soc=min_battery_soc,
            force_charge=force_charge,
            auto_grid_charge_engaged=auto_grid_charge_engaged,
        )

        _LOGGER.info(
            "Optimizer safety-only path: %d decisions, battery_action=%s",
            len(decisions), battery_action,
        )
        return OptimizerResult(
            decisions=decisions,
            battery_discharge_action=battery_action,
        )

    def _grid_allowed(self, appliance: ApplianceConfig, tariff: TariffInfo) -> bool:
        """Grid permission includes cheap tariffs and the existing opportunity rule."""
        return appliance.allow_grid_supplement and (
            self._is_cheap_for_appliance(tariff, appliance)
            or tariff.current_price < tariff.feed_in_tariff
        )

    def _grid_target_power(self, appliance: ApplianceConfig, tariff: TariffInfo) -> float:
        """Power eligible for tariff funding; extra solar may raise dynamic current."""
        if not appliance.dynamic_current:
            return appliance.nominal_power
        phases = max(appliance.phases, 1)
        amps = self._cheap_window_target_amps(appliance, tariff, phases)
        return (amps if amps is not None else appliance.min_current) * self.grid_voltage * phases

    def _running_grid_credits(
        self, appliances: list[ApplianceConfig], states: dict[str, ApplianceState],
        excess: float, tariff: TariffInfo,
    ) -> dict[str, float]:
        """Recover only tariff-funded measured draw already present in residual.

        Assign physical solar to running consumers in priority order before
        identifying imports. Unused grid permission never creates solar budget.
        Safety/override/helper/idle loads receive no artificial credit.
        States for paused/disabled consumers remain unmanaged household draw.
        """
        managed_ids = {app.id for app in appliances}
        solar = max(excess + sum(max(st.current_power, 0.0)
                                for app_id, st in states.items()
                                if app_id in managed_ids and st.is_on
                                and st.current_power_available), 0.0)
        credits = {}
        self._running_solar_share = {}
        for app in appliances:
            state = states.get(app.id)
            if (state is None or not state.is_on or state.current_power <= 0
                    or not state.current_power_available):
                continue
            solar_share = min(solar, state.current_power)
            solar -= solar_share
            self._running_solar_share[app.id] = solar_share
            if not self._grid_allowed(app, tariff):
                continue
            if self._apply_safety_rules(app, state, [], states) is not None:
                continue
            target = self._grid_target_power(app, tariff)
            cap = app.max_grid_power if app.max_grid_power is not None else target
            needed = max(min(state.current_power, target) - solar_share, 0.0)
            if app.dynamic_current or needed <= cap:
                credits[app.id] = min(needed, max(cap, 0.0))
        return credits

    def _allocate_grid_supported(
        self, appliance: ApplianceConfig, state: ApplianceState,
        avg_budget: float, instant_budget: float, tariff: TariffInfo,
    ) -> tuple[ControlDecision, float] | None:
        """Allocate solar plus an explicitly bounded imported portion.

        Account for the change in physical demand minus the change in funded
        imports. This prevents the same grid allowance being spent twice on
        consecutive cycles or by another consumer.
        """
        if not self._grid_allowed(appliance, tariff):
            return None
        old_credit = self._running_grid_credit.get(appliance.id, 0.0)
        current = max(state.current_power, 0.0) if state.is_on else 0.0
        solar = max(min(avg_budget, instant_budget) + current - old_credit, 0.0)
        if state.is_on:
            # A lower-priority running deficit is resolved by SHED later;
            # it must not erase this consumer's assigned solar meanwhile.
            initial = self._initial_grid_instant_budget
            prior_commitment = max(initial - instant_budget, 0.0) if initial is not None else 0.0
            solar = max(solar, self._running_solar_share.get(appliance.id, 0.0) - prior_commitment)
        desired = self._grid_target_power(appliance, tariff)
        cap = max(appliance.max_grid_power if appliance.max_grid_power is not None else desired, 0.0)
        dep_power = 0.0
        if not state.is_on and appliance.requires_appliance:
            dep = self._state_by_id.get(appliance.requires_appliance)
            config = self._config_by_id.get(appliance.requires_appliance)
            if dep is not None and not dep.is_on and config is not None:
                dep_power = config.nominal_power
                if solar < dep_power:
                    return None
                solar -= dep_power
        if appliance.dynamic_current:
            watts_per_amp = self.grid_voltage * max(appliance.phases, 1)
            target_power = min(max(solar, desired), solar + cap)
            amps = min(_step_floor(target_power / watts_per_amp, appliance.current_step), appliance.max_current)
            if amps < appliance.min_current:
                # Return a normal decision, but revoke any partial running
                # credit: a load below its minimum cannot use that allowance.
                if state.is_on and appliance.on_only:
                    amps = appliance.min_current
                elif state.is_on:
                    return ControlDecision(appliance.id, Action.ON, None,
                                           "Insufficient solar and grid allowance for minimum current", False), old_credit
                else:
                    return None
            target_power = amps * watts_per_amp
            action = Action.SET_CURRENT
        else:
            target_power = current if state.is_on else appliance.nominal_power
            amps = None
            action = Action.ON
        grid = max(target_power - solar, 0.0)
        if grid > cap + 1e-6:
            if state.is_on and appliance.dynamic_current and appliance.on_only:
                # On-only preserves supply at minimum current even if funding
                # is exhausted. Credit only the explicitly allowed grid share.
                grid = cap
            elif state.is_on:
                return ControlDecision(appliance.id, Action.ON, None,
                                       "Grid allowance insufficient; normal shedding applies", False), old_credit
            else:
                return None
        if dep_power > 0:
            self._pending_dep_decisions[appliance.requires_appliance] = ControlDecision(
                appliance.requires_appliance, Action.ON, None,
                f"Started as dependency for {appliance.name}", False,
            )
        reason = (f"Grid supplement: {grid:.0f}W from grid ({target_power:.0f}W total)"
                  if grid > 0 else f"Solar-supported load ({target_power:.0f}W)")
        if state.is_on and appliance.on_only and target_power > solar + cap:
            reason = f"On-only minimum current: {amps:.1f}A; grid funding limited to {cap:.0f}W"
        return ControlDecision(
            appliance.id, action, amps, reason, False,
            uses_grid_supplement=grid > 0, grid_supplement_watts=grid,
        ), target_power - current - grid + old_credit + dep_power

    def _allocate_appliance(
        self,
        appliance: ApplianceConfig,
        state: ApplianceState,
        avg_budget: float,
        instant_budget: float,
        plan: Plan,
        tariff: TariffInfo,
        decisions: list[ControlDecision] | None = None,
        state_by_id: dict[str, ApplianceState] | None = None,
    ) -> tuple[ControlDecision, float]:
        """Determine the desired action for a single appliance.

        Budgets:
            avg_budget: per-appliance or global averaged excess. Used as
                the turn-on gate for currently-OFF appliances and as
                the upper bump ceiling (via ``min(instant, avg)``) for
                already-ON dynamic-current adjustments.
            instant_budget: instantaneous excess (from the latest
                power_state snapshot, decremented by each prior
                allocation's delta). Used as the reactive view for
                dynamic-current adjustments and as the physical-reality
                check read by Phase 3 SHED.

        Returns:
            A tuple of (ControlDecision, power_delta). power_delta is
            positive if the appliance commits more power from the
            budgets; negative if it frees power; zero if no budget
            change. The caller applies power_delta to both budgets.
        """
        # --- Pre-checks (highest priority, checked first) ---
        # All excess-independent safety rules live in _apply_safety_rules
        # so the safety-only optimizer path (triggered when ASSESS returns
        # None) can reuse them.
        safety_result = self._apply_safety_rules(
            appliance, state,
            decisions=decisions if decisions is not None else [],
            state_by_id=state_by_id if state_by_id is not None else {},
        )
        if safety_result is not None:
            return safety_result

        if not state.current_power_available:
            # Hold the last physical state without inventing power credit.
            # An eligible running load still needs the battery discharge
            # block while its imported portion cannot be measured.
            return ControlDecision(
                appliance.id, Action.ON if state.is_on else Action.IDLE, None,
                "Appliance power reading unavailable - holding state", False,
                uses_grid_supplement=state.is_on and self._grid_allowed(appliance, tariff),
            ), 0.0

        grid_result = self._allocate_grid_supported(
            appliance, state, avg_budget, instant_budget, tariff,
        )
        if grid_result is not None:
            return grid_result
        if not state.is_on and self._grid_allowed(appliance, tariff):
            # If supplementation is infeasible, stale solar history must not
            # start the same load through the ordinary allocation fallback.
            avg_budget = min(avg_budget, instant_budget)

        if not state.is_on and appliance.start_delay > 0:
            avg_budget = min(avg_budget, instant_budget)

        # --- Already-ON appliances ---
        # Note: instant_budget (from measured grid power) already reflects
        # these appliances' consumption. For non-dynamic appliances we
        # return power_delta=0 because the power is already in the
        # measurement. For dynamic-current adjustments we return the
        # delta between the new commanded power and the currently drawn
        # power — that delta is the new commitment on top of what the
        # grid measurement already reflects. The SHED phase uses
        # state.current_power to correctly calculate freed power when
        # turning off.
        if state.is_on:
            if appliance.dynamic_current and appliance.current_entity:
                # Dual-budget bump clamp: use min(instant_budget, avg_budget)
                # as the excess view for target computation. Asymmetric by
                # design — reacts fast when instant_budget drops (physical
                # drop), refuses to bump upward past what avg_budget says
                # is sustainable (transient peak). Add current_power back
                # since it's already reflected in the grid measurement.
                phases = max(appliance.phases, 1)
                # The coordinator normalizes no-meter estimates. A valid
                # measured zero must not fall back to a configured current.
                current_power = max(state.current_power, 0.0)
                excess_for_adjustment = min(instant_budget, avg_budget)
                available = excess_for_adjustment + current_power - max(self._off_threshold, 0)
                raw_amps = available / (self.grid_voltage * phases)
                target_amps = _step_floor(raw_amps, appliance.current_step)

                if target_amps < appliance.min_current and not appliance.on_only:
                    # Not enough for minimum current and no override - SHED will handle turning off
                    reason = _format_staying_on_dynamic(
                        current_amperage=state.current_amperage,
                        current_power=current_power,
                        off_threshold=self._off_threshold,
                        instant_budget=instant_budget,
                    )
                    return (
                        ControlDecision(
                            appliance_id=appliance.id,
                            action=Action.ON,
                            target_current=None,
                            reason=reason,
                            overrides_plan=False,
                        ),
                        0.0,
                    )

                target_amps = max(appliance.min_current, min(target_amps, appliance.max_current))
                power_at_target = target_amps * self.grid_voltage * phases
                power_delta = power_at_target - current_power
                return (
                    ControlDecision(
                        appliance_id=appliance.id,
                        action=Action.SET_CURRENT,
                        target_current=target_amps,
                        reason=f"Dynamic current adjustment: {target_amps:.1f}A ({available:.0f}W available)",
                        overrides_plan=False,
                    ),
                    power_delta,  # Only the delta from current consumption
                )
            else:
                # Non-dynamic: keep ON, no new allocation needed
                reason = _format_staying_on_standard(
                    current_power=state.current_power,
                    off_threshold=self._off_threshold,
                    instant_budget=instant_budget,
                )
                return (
                    ControlDecision(
                        appliance_id=appliance.id,
                        action=Action.ON,
                        target_current=None,
                        reason=reason,
                        overrides_plan=False,
                    ),
                    0.0,  # Already consuming, already in measured excess
                )

        # --- Dynamic current appliances (currently OFF) ---
        if appliance.dynamic_current:
            return self._allocate_dynamic_current(
                appliance, state, avg_budget, tariff, plan,
            )

        # --- Standard (on/off) appliances (currently OFF) ---
        return self._allocate_standard(
            appliance, state, avg_budget, tariff, plan,
        )

    def _allocate_standard(
        self,
        appliance: ApplianceConfig,
        state: ApplianceState,
        avg_budget: float,
        tariff: TariffInfo,
        plan: Plan | None = None,
    ) -> tuple[ControlDecision, float]:
        """Allocate a standard on/off appliance using hysteresis thresholds.

        This is only called for appliances that are currently OFF, so the
        averaged view (``avg_budget``) is the right conservative gate for
        a new start.

        Tariff supplementation is handled by the common allocation entry point.
        """
        # Calculate dependency power if dependency is OFF
        dep_power = 0.0
        if appliance.requires_appliance:
            dep_state = self._state_by_id.get(appliance.requires_appliance)
            dep_config = self._config_by_id.get(appliance.requires_appliance)
            if dep_state and not dep_state.is_on and dep_config:
                dep_power = dep_config.nominal_power

        # Determine plan-aware threshold
        plan_on = (
            self._plan_says_on(appliance.id, plan)
            if self._plan_influence != "none" and plan is not None
            else False
        )

        if plan_on and self._plan_influence == "light":
            # Plan says ON: reduced threshold (no buffer)
            threshold = appliance.nominal_power
        elif plan_on and self._plan_influence == "plan_follows":
            # Plan says ON: allow activation with minimal excess (but not zero)
            threshold = max(appliance.nominal_power * 0.1, 50.0)
        else:
            # Normal threshold
            on_buf = appliance.on_threshold if appliance.on_threshold is not None else DEFAULT_ON_THRESHOLD
            threshold = appliance.nominal_power + on_buf

        # Positive shutdown thresholds also reserve a buffer before starting.
        if self._off_threshold > 0 and not (plan_on and self._plan_influence == "plan_follows"):
            threshold = max(threshold, appliance.nominal_power + self._off_threshold)
        power_needed = threshold + dep_power
        if avg_budget >= power_needed:
            pending = self._solar_start_delay(appliance, state)
            if pending is not None:
                return pending, 0.0
            # For plan_follows, only deduct the solar portion from the excess
            # budget when excess is less than nominal_power -- the remainder
            # is expected to be drawn from the grid as planned.
            if plan_on and self._plan_influence == "plan_follows":
                power_consumed = min(avg_budget, appliance.nominal_power)
            else:
                power_consumed = appliance.nominal_power
            # If dependency is OFF, inject a pending decision to turn it ON
            if dep_power > 0:
                self._pending_dep_decisions[appliance.requires_appliance] = ControlDecision(
                    appliance_id=appliance.requires_appliance, action=Action.ON,
                    target_current=None,
                    reason=f"Started as dependency for {appliance.name}",
                    overrides_plan=False,
                )
                power_consumed = power_consumed + dep_power
            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.ON,
                    target_current=None,
                    reason=f"Excess available ({avg_budget:.0f}W >= {power_needed:.0f}W needed)",
                    overrides_plan=False, solar_start_qualified=True,
                ),
                power_consumed,
            )

        # Deadline must-run: force ON if deadline is approaching and min_runtime not met
        if (
            appliance.schedule_deadline is not None
            and (runtime_demand_seconds(appliance, state) or 0) > 0
        ):
            from datetime import datetime
            current_time = datetime.now(self._tz).time() if self._tz else datetime.now().time()
            deadline = appliance.schedule_deadline
            remaining_runtime = (runtime_demand_seconds(appliance, state) or 0)

            # Calculate time until deadline
            now_seconds = current_time.hour * 3600 + current_time.minute * 60 + current_time.second
            deadline_seconds = deadline.hour * 3600 + deadline.minute * 60
            is_overnight = deadline_seconds <= now_seconds
            if is_overnight:
                deadline_seconds += 86400  # overnight deadline
            time_until_deadline = deadline_seconds - now_seconds

            if time_until_deadline <= remaining_runtime * 1.1:  # 10% buffer
                deadline_label = deadline.strftime("%H:%M") + (
                    " (tomorrow)" if is_overnight else ""
                )
                reason = (
                    f"Deadline must-run: {format_duration(remaining_runtime)} "
                    f"remaining, deadline {deadline_label} "
                    f"(in {format_duration(time_until_deadline)})"
                )
                return (
                    ControlDecision(
                        appliance_id=appliance.id,
                        action=Action.ON,
                        target_current=None,
                        reason=reason,
                        overrides_plan=False,
                        bypasses_cooldown=True,
                    ),
                    appliance.nominal_power,
                )

        return (
            ControlDecision(
                appliance_id=appliance.id,
                action=Action.IDLE,
                target_current=None,
                reason=f"Insufficient excess ({avg_budget:.0f}W < {power_needed:.0f}W needed)",
                overrides_plan=False,
            ),
            0.0,
        )

    def _allocate_dynamic_current(
        self,
        appliance: ApplianceConfig,
        state: ApplianceState,
        avg_budget: float,
        tariff: TariffInfo,
        plan: Plan | None = None,
    ) -> tuple[ControlDecision, float]:
        """Allocate a dynamic current appliance (e.g., EV charger).

        Calculates optimal amperage from the averaged excess power:
            amps = avg_budget / (grid_voltage * phases)
        Then clamps to [min_current, max_current].

        This is only called for appliances that are currently OFF, so the
        averaged view (``avg_budget``) is the right conservative gate for
        a new start.
        """
        phases = max(appliance.phases, 1)

        # Determine plan-aware threshold buffer
        plan_on = (
            self._plan_says_on(appliance.id, plan)
            if self._plan_influence != "none" and plan is not None
            else False
        )

        if plan_on and self._plan_influence == "light":
            # Plan says ON: no hysteresis buffer
            dynamic_buffer = 0.0
        elif plan_on and self._plan_influence == "plan_follows":
            # Plan says ON: allow activation at minimum current with no buffer
            dynamic_buffer = 0.0
        else:
            dynamic_buffer = appliance.on_threshold if appliance.on_threshold is not None else DEFAULT_DYNAMIC_ON_THRESHOLD

        dynamic_buffer = max(dynamic_buffer, max(self._off_threshold, 0))
        dep_power = 0.0
        if appliance.requires_appliance:
            dep_state = self._state_by_id.get(appliance.requires_appliance)
            dep_config = self._config_by_id.get(appliance.requires_appliance)
            if dep_state and not dep_state.is_on and dep_config and appliance.requires_appliance not in self._pending_dep_decisions:
                dep_power = dep_config.nominal_power
        min_watts_needed = appliance.min_current * self.grid_voltage * phases + dynamic_buffer + dep_power

        if avg_budget < min_watts_needed:
            # Deadline must-run: force ON at minimum current if deadline is approaching
            if (
                appliance.schedule_deadline is not None
                and (runtime_demand_seconds(appliance, state) or 0) > 0
            ):
                from datetime import datetime
                current_time = datetime.now(self._tz).time() if self._tz else datetime.now().time()
                deadline = appliance.schedule_deadline
                remaining_runtime = (runtime_demand_seconds(appliance, state) or 0)
                now_seconds = current_time.hour * 3600 + current_time.minute * 60 + current_time.second
                deadline_seconds = deadline.hour * 3600 + deadline.minute * 60
                is_overnight = deadline_seconds <= now_seconds
                if is_overnight:
                    deadline_seconds += 86400
                time_until_deadline = deadline_seconds - now_seconds
                if time_until_deadline <= remaining_runtime * 1.1:
                    min_power = appliance.min_current * self.grid_voltage * phases
                    deadline_label = deadline.strftime("%H:%M") + (
                        " (tomorrow)" if is_overnight else ""
                    )
                    reason = (
                        f"Deadline must-run: {format_duration(remaining_runtime)} "
                        f"remaining, deadline {deadline_label} "
                        f"(in {format_duration(time_until_deadline)})"
                    )
                    return (
                        ControlDecision(
                            appliance_id=appliance.id,
                            action=Action.SET_CURRENT,
                            target_current=appliance.min_current,
                            reason=reason,
                            overrides_plan=False,
                            bypasses_cooldown=True,
                        ),
                        min_power,
                    )

            return (
                ControlDecision(
                    appliance_id=appliance.id,
                    action=Action.IDLE,
                    target_current=None,
                    reason=(
                        f"Insufficient excess for min current "
                        f"({avg_budget:.0f}W < {min_watts_needed:.0f}W needed "
                        f"[{appliance.min_current:.1f}A * {self.grid_voltage}V * {phases} + "
                        f"{dynamic_buffer:.0f}W buffer])"
                    ),
                    overrides_plan=False,
                ),
                0.0,
            )

        pending = self._solar_start_delay(appliance, state)
        if pending is not None:
            return pending, 0.0
        raw_amps = (avg_budget - dep_power - max(self._off_threshold, 0)) / (self.grid_voltage * phases)
        clamped_amps = _step_floor(raw_amps, appliance.current_step)

        natural_target_amps = max(appliance.min_current, min(clamped_amps, appliance.max_current))

        target_amps = natural_target_amps
        power_consumed = target_amps * self.grid_voltage * phases
        if dep_power > 0:
            self._pending_dep_decisions[appliance.requires_appliance] = ControlDecision(
                appliance.requires_appliance, Action.ON, None,
                f"Started as dependency for {appliance.name}", False,
            )
            power_consumed += dep_power

        return (
            ControlDecision(
                appliance_id=appliance.id,
                action=Action.SET_CURRENT,
                target_current=target_amps,
                reason=f"Dynamic current set to {target_amps:.1f}A ({power_consumed:.0f}W)",
                overrides_plan=False, solar_start_qualified=True,
            ),
            power_consumed,
        )

    def _solar_start_delay(self, appliance: ApplianceConfig, state: ApplianceState) -> ControlDecision | None:
        """A qualified pending start consumes neither power nor dependencies."""
        if appliance.schedule_deadline is not None and (runtime_demand_seconds(appliance, state) or 0) > 0:
            now = datetime.now(self._tz) if self._tz else datetime.now()
            deadline = datetime.combine(now.date(), appliance.schedule_deadline, tzinfo=now.tzinfo)
            if deadline <= now:
                deadline += timedelta(days=1)
            if (deadline - now).total_seconds() <= max(0, (runtime_demand_seconds(appliance, state) or 0)) * 1.1:
                return None
        if appliance.start_delay > 0 and state.solar_start_elapsed < appliance.start_delay:
            return ControlDecision(
                appliance.id, Action.IDLE, None,
                f"Solar start delay ({state.solar_start_elapsed:.0f}/{appliance.start_delay:.0f}s)",
                False, solar_start_qualified=True,
            )
        return None

    # ------------------------------------------------------------------
    # Tariff helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_cheap_tariff(tariff: TariffInfo) -> bool:
        """Return True if the current tariff qualifies as cheap."""
        return tariff.current_price <= tariff.cheap_price_threshold

    @staticmethod
    def _is_cheap_for_appliance(tariff: TariffInfo, appliance: ApplianceConfig) -> bool:
        """Return True if the current tariff is cheap for this specific appliance."""
        threshold = (
            appliance.cheap_price_threshold
            if appliance.cheap_price_threshold is not None
            else tariff.cheap_price_threshold
        )
        return tariff.current_price <= threshold

    def _cheap_window_target_amps(
        self,
        appliance: ApplianceConfig,
        tariff: TariffInfo,
        phases: int,
    ) -> float | None:
        """Return the cheap-window target amps for a dynamic-current appliance.

        Returns None when:
        - cheap_grid_target_current is unset, or
        - allow_grid_supplement is False, or
        - the current tariff is not cheap for this appliance.

        Clamp to the configured current range and floor to current_step.
        The grid power cap applies later to the imported portion after solar
        is accounted for; it is not a total charging-power limit.
        """
        if appliance.cheap_grid_target_current is None:
            return None
        if not appliance.allow_grid_supplement:
            return None
        if not self._is_cheap_for_appliance(tariff, appliance):
            return None

        cap_amps = appliance.max_current

        target_amps = max(appliance.cheap_grid_target_current, appliance.min_current)
        target_amps = min(target_amps, cap_amps)
        return _step_floor(target_amps, appliance.current_step)

    # ------------------------------------------------------------------
    # Time window helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_within_time_window(
        current: time,
        start: time | None,
        end: time | None,
    ) -> bool:
        """Check if the current time falls within the operating window.

        Handles four cases:
        - Only start_after set: current >= start_after
        - Only end_before set: current < end_before
        - Both set, same-day window (start < end): start <= current < end
        - Both set, overnight window (start >= end): current >= start OR current < end
        """
        if start is not None and end is not None:
            if start < end:
                # Same-day window, e.g. 08:00 - 18:00
                return start <= current < end
            else:
                # Overnight window, e.g. 22:00 - 06:00
                return current >= start or current < end
        elif start is not None:
            return current >= start
        elif end is not None:
            return current < end
        # Both None — should not be called, but treat as always within window
        return True

    # ------------------------------------------------------------------
    # Phase 2.5: PREEMPT
    # ------------------------------------------------------------------

    def _preempt(
        self,
        decisions: list[ControlDecision],
        sorted_appliances: list[ApplianceConfig],
        state_by_id: dict[str, ApplianceState],
        avg_budget: float,
        instant_budget: float,
    ) -> tuple[float, float]:
        """Phase 2.5: PREEMPT - shed lower-priority appliances to start higher-priority ones.

        After ALLOCATE, some higher-priority appliances may be IDLE due to
        insufficient excess. This phase checks if shedding lower-priority ON
        appliances would free enough power to start them.

        Modifies decisions in-place by replacing entries. Tracks both
        budgets in lockstep: every freed/consumed power delta applied to
        avg_budget is mirrored to instant_budget. Feasibility checks and
        target_amps computation use avg_budget (PREEMPT is a conservative
        turn-on decision).

        Returns:
            (new_avg_budget, new_instant_budget) — both updated with the
            net effect of all preemptions performed.
        """
        # Build lookup structures
        appliance_by_id: dict[str, ApplianceConfig] = {
            a.id: a for a in sorted_appliances
        }
        decision_index: dict[str, int] = {
            d.appliance_id: i for i, d in enumerate(decisions)
        }

        # Find IDLE decisions where reason contains "insufficient excess"
        idle_candidates: list[tuple[str, ApplianceConfig]] = []
        for decision in decisions:
            if decision.action != Action.IDLE:
                continue
            if "insufficient excess" not in decision.reason.lower():
                continue
            appliance = appliance_by_id.get(decision.appliance_id)
            if appliance is None:
                continue
            idle_candidates.append((decision.appliance_id, appliance))

        # Sort by priority (lowest number = highest priority = process first)
        idle_candidates.sort(key=lambda item: (item[1].priority, item[0]))

        for idle_id, idle_app in idle_candidates:
            # Calculate power_needed to start this appliance
            if idle_app.dynamic_current and idle_app.current_entity:
                phases = max(idle_app.phases, 1)
                dyn_buf = idle_app.on_threshold if idle_app.on_threshold is not None else DEFAULT_DYNAMIC_ON_THRESHOLD
                power_needed = (
                    idle_app.min_current * self.grid_voltage * phases
                    + dyn_buf
                )
            else:
                on_buf = idle_app.on_threshold if idle_app.on_threshold is not None else DEFAULT_ON_THRESHOLD
                power_needed = idle_app.nominal_power + on_buf

            # Add dependency power if dependency is OFF
            dep_power = 0.0
            dep_id: str | None = None
            if idle_app.requires_appliance:
                dep_state = state_by_id.get(idle_app.requires_appliance)
                dep_config = appliance_by_id.get(idle_app.requires_appliance)
                if dep_state and not dep_state.is_on and dep_config:
                    # Also check that the dependency decision is not already ON
                    dep_idx = decision_index.get(idle_app.requires_appliance)
                    if dep_idx is not None:
                        dep_decision = decisions[dep_idx]
                        if dep_decision.action not in (Action.ON, Action.SET_CURRENT):
                            dep_power = dep_config.nominal_power
                            dep_id = idle_app.requires_appliance

            power_needed = max(power_needed, (idle_app.min_current * self.grid_voltage * max(idle_app.phases, 1) if idle_app.dynamic_current else idle_app.nominal_power) + max(self._off_threshold, 0))
            power_needed += dep_power

            # Collect preemptable ON/SET_CURRENT decisions for lower-priority appliances
            preemptable: list[tuple[str, ApplianceConfig, float]] = []
            for decision in decisions:
                if decision.action not in (Action.ON, Action.SET_CURRENT):
                    continue
                app = appliance_by_id.get(decision.appliance_id)
                if app is None:
                    continue
                # Must be strictly lower priority (higher number)
                if app.priority <= idle_app.priority:
                    continue
                # Never preempt the idle candidate's own dependency
                if idle_app.requires_appliance and app.id == idle_app.requires_appliance:
                    continue
                # Never preempt on_only
                if app.on_only:
                    continue
                # Never preempt protected appliances
                if app.protect_from_preemption:
                    continue
                # Never preempt overridden
                if app.override_active:
                    continue
                # Never preempt grid-supplemented
                if decision.uses_grid_supplement:
                    continue
                # Never preempt dependency-protected (has dependents that are ON)
                if app.id in self._reverse_deps:
                    has_running_dep = any(
                        d.action in (Action.ON, Action.SET_CURRENT)
                        for d in decisions
                        if d.appliance_id in self._reverse_deps[app.id]
                    )
                    if has_running_dep:
                        continue
                # Never preempt appliances with unmet min_daily_runtime
                state = state_by_id.get(app.id)
                if (
                    state is not None and runtime_protected(app, state)
                ):
                    continue

                if state is not None and not state.current_power_available:
                    continue

                # Allocation already debited the commanded current's delta.
                # Undo that committed demand, including newly starting loads,
                # rather than crediting their unrelated nominal rating.
                freed = self._committed_power(app, state, decision)
                if freed <= 0:
                    continue
                preemptable.append((app.id, app, freed))

            # Sort preemptable: highest priority number first (least important first)
            preemptable.sort(key=lambda item: (-item[1].priority, item[0]))

            # Accumulate freed power until we have enough.
            # Feasibility check uses avg_budget (the conservative turn-on view).
            start_budget = (min(avg_budget, instant_budget)
                            if idle_id in self._grid_allowed_ids or idle_app.start_delay > 0 else avg_budget)
            total_freed = 0.0
            to_preempt: list[tuple[str, ApplianceConfig, float]] = []
            for p_id, p_app, freed in preemptable:
                to_preempt.append((p_id, p_app, freed))
                total_freed += freed
                if start_budget + total_freed >= power_needed:
                    break

            # Check if enough power can be freed (against avg_budget)
            if start_budget + total_freed < power_needed:
                continue  # Not enough even with all candidates; skip this idle appliance

            pending = self._solar_start_delay(idle_app, state_by_id[idle_id])
            if pending is not None:
                decisions[decision_index[idle_id]] = pending
                continue

            # Execute preemption: replace preempted decisions with OFF.
            # Freed power credits BOTH budgets in lockstep.
            for p_id, p_app, freed in to_preempt:
                idx = decision_index[p_id]
                decisions[idx] = ControlDecision(
                    appliance_id=p_id,
                    action=Action.OFF,
                    target_current=None,
                    reason=f"Preempted for higher-priority {idle_app.name}",
                    overrides_plan=False,
                )
                avg_budget += freed
                instant_budget += freed
                _LOGGER.debug(
                    "  Preempt %s (p=%d): freed %.0fW for %s (p=%d)",
                    p_app.name, p_app.priority, freed,
                    idle_app.name, idle_app.priority,
                )

            # Replace idle decision with ON or SET_CURRENT. Target_amps is
            # derived from avg_budget (the conservative turn-on view).
            idle_idx = decision_index[idle_id]
            if idle_app.dynamic_current and idle_app.current_entity:
                phases = max(idle_app.phases, 1)
                available = (min(avg_budget, instant_budget)
                             if idle_id in self._grid_allowed_ids or idle_app.start_delay > 0 else avg_budget)
                raw_amps = (available - dep_power - max(self._off_threshold, 0)) / (self.grid_voltage * phases)
                target_amps = _step_floor(raw_amps, idle_app.current_step)
                target_amps = max(
                    idle_app.min_current,
                    min(target_amps, idle_app.max_current),
                )
                power_consumed = target_amps * self.grid_voltage * phases
                decisions[idle_idx] = ControlDecision(
                    appliance_id=idle_id,
                    action=Action.SET_CURRENT,
                    target_current=target_amps,
                    reason=f"Preemption: dynamic current at {target_amps:.1f}A ({power_consumed:.0f}W)",
                    overrides_plan=False, solar_start_qualified=True,
                )
            else:
                power_consumed = idle_app.nominal_power
                decisions[idle_idx] = ControlDecision(
                    appliance_id=idle_id,
                    action=Action.ON,
                    target_current=None,
                    reason=f"Preemption: started after shedding lower-priority appliances",
                    overrides_plan=False, solar_start_qualified=True,
                )
            avg_budget -= power_consumed
            instant_budget -= power_consumed

            # If dependency needs starting, replace its decision too
            if dep_id is not None and dep_id in decision_index:
                dep_idx = decision_index[dep_id]
                decisions[dep_idx] = ControlDecision(
                    appliance_id=dep_id,
                    action=Action.ON,
                    target_current=None,
                    reason=f"Started as dependency for {idle_app.name} (preemption)",
                    overrides_plan=False,
                )
                avg_budget -= dep_power
                instant_budget -= dep_power

            _LOGGER.debug(
                "  Preempt result: %s ON, avg=%.0fW inst=%.0fW",
                idle_app.name, avg_budget, instant_budget,
            )

        return avg_budget, instant_budget

    # ------------------------------------------------------------------
    # Phase 3: SHED
    # ------------------------------------------------------------------

    def _shed(
        self,
        decisions: list[ControlDecision],
        sorted_appliances: list[ApplianceConfig],
        state_by_id: dict[str, ApplianceState],
        instant_budget: float,
        force_shed: bool = False,
    ) -> float:
        """Phase 3: SHED - turn off or reduce lowest-priority appliances first.

        When instant_budget is below the OFF threshold after allocation,
        shed appliances starting from lowest priority (highest priority number)
        until the power balance is restored.

        Shedding rules:
        - Never shed on_only appliances
        - Never shed manually overridden appliances
        - Never shed grid-supplemented appliances
        - Never shed bypasses_cooldown decisions (deadline must-run)
        - Never shed a dependency while any of its dependents are running
        - Reduce dynamic current before turning off
        - Prefer shedding appliances that have met their min_daily_runtime
        - Stop once instant_budget >= OFF_THRESHOLD (-50W)

        Modifies decisions in-place by replacing entries.

        Returns:
            Updated instant_budget after shedding.
        """
        if instant_budget >= self._off_threshold:
            return instant_budget

        # Build lookup structures
        appliance_by_id: dict[str, ApplianceConfig] = {
            a.id: a for a in sorted_appliances
        }
        decision_index: dict[str, int] = {
            d.appliance_id: i for i, d in enumerate(decisions)
        }

        # Collect shed candidates: ON/SET_CURRENT decisions that can be shed
        candidates: list[tuple[str, ApplianceConfig]] = []
        for decision in decisions:
            if decision.action not in (Action.ON, Action.SET_CURRENT):
                continue
            appliance = appliance_by_id.get(decision.appliance_id)
            if appliance is None:
                continue
            state = state_by_id.get(decision.appliance_id)
            if state is not None and not state.current_power_available:
                continue
            # Never shed on_only appliances
            if appliance.on_only:
                continue
            # Never shed manually overridden appliances
            if appliance.override_active:
                continue
            # Never shed deadline-forced or other cooldown-bypassing decisions
            if decision.bypasses_cooldown:
                continue
            # Skip grid-supplemented appliances (they consume from grid, not solar)
            if decision.uses_grid_supplement:
                continue
            # Never shed a dependency while any dependent is still running
            if appliance.id in self._reverse_deps:
                has_running_dep = any(
                    d.action in (Action.ON, Action.SET_CURRENT)
                    for d in decisions
                    if d.appliance_id in self._reverse_deps[appliance.id]
                )
                if has_running_dep:
                    continue
            candidates.append((decision.appliance_id, appliance))

        # Sort candidates: lowest priority first (highest number), then
        # prefer shedding appliances that have met their min_daily_runtime
        def shed_sort_key(item: tuple[str, ApplianceConfig]) -> tuple[int, int]:
            app_id, appliance = item
            state = state_by_id.get(app_id)
            # Primary: reverse priority (highest number = shed first)
            priority_key = -appliance.priority
            # Secondary: prefer shedding appliances that have met their minimum
            # met_min = 0 means "shed this first", 1 means "try to keep"
            met_min = 1  # default: hasn't met (protect it)
            if appliance.min_daily_runtime is not None and state is not None:
                if state.runtime_today >= appliance.min_daily_runtime:
                    met_min = 0  # met minimum, OK to shed first
            elif appliance.min_daily_runtime is None:
                met_min = 0  # no minimum requirement, OK to shed
            return (priority_key, met_min)

        candidates.sort(key=shed_sort_key)

        # Shed candidates until instant_budget >= 0
        for app_id, appliance in candidates:
            if instant_budget >= self._off_threshold:
                break

            # Hard min_runtime constraint: don't shed if minimum not met
            # (bypassed during force_charge to prioritise battery charging)
            state = state_by_id.get(app_id)
            if (not force_shed
                    and state is not None
                    and runtime_protected(appliance, state)):
                idx = decision_index[app_id]
                remaining = (runtime_demand_seconds(appliance, state) or 0)
                decisions[idx] = replace(
                    decisions[idx],
                    reason=("Continuing bounded runtime cycle: remaining runtime unavailable"
                            if appliance.remaining_runtime_entity and runtime_demand_seconds(appliance, state) is None
                            else f"Staying on: runtime demand not met ({format_duration(remaining)} remaining)"
                            if appliance.remaining_runtime_entity
                            else f"Staying on: minimum daily runtime not met ({format_duration(remaining)} remaining)"),
                )
                _LOGGER.debug(
                    "  Skipping shed of %s: min_runtime not met (%s < %s)",
                    appliance.name, state.runtime_today, appliance.min_daily_runtime,
                )
                continue

            idx = decision_index[app_id]
            current_decision = decisions[idx]

            committed_power = self._committed_power(appliance, state, current_decision)
            # For dynamic current appliances: try reducing current first
            if appliance.dynamic_current and current_decision.action in (Action.ON, Action.SET_CURRENT):
                state = state_by_id.get(app_id)
                new_decision, power_freed = self._shed_dynamic_current(
                    appliance, state, instant_budget, committed_power=committed_power,
                )
                if new_decision is not None:
                    decisions[idx] = new_decision
                    instant_budget += power_freed
                    continue

            # Undo only the demand currently committed in this cycle's budget.
            freed_power = committed_power
            decisions[idx] = ControlDecision(
                appliance_id=app_id,
                action=Action.OFF,
                target_current=None,
                reason=f"Shed: insufficient excess (priority {appliance.priority})",
                overrides_plan=False,
            )
            instant_budget += freed_power
            _LOGGER.debug(
                "  Shed %s: freed %.0fW, inst=%.0fW",
                appliance.name, freed_power, instant_budget,
            )

        return instant_budget

    def _committed_power(
        self, appliance: ApplianceConfig, state: ApplianceState | None,
        decision: ControlDecision,
    ) -> float:
        """Demand already reflected in budgets after allocation.

        Current-setting decisions replace measured demand; a new ON decision
        commits nominal demand. An unchanged running load keeps its normalized
        measured/estimated draw, including a valid zero.
        """
        if decision.action == Action.SET_CURRENT and decision.target_current is not None:
            return decision.target_current * self.grid_voltage * max(appliance.phases, 1)
        if state is not None and state.is_on:
            return max(state.current_power, 0.0) if state.current_power_available else 0.0
        return appliance.nominal_power

    def _shed_dynamic_current(
        self,
        appliance: ApplianceConfig,
        state: ApplianceState | None,
        instant_budget: float,
        committed_power: float | None = None,
    ) -> tuple[ControlDecision | None, float]:
        """Try to reduce dynamic current on an already-ON appliance.

        Calculates the maximum power we can provide (current consumption + remaining excess)
        and derives a new target amperage. If the new amperage is at least min_current,
        reduce instead of turning off.

        Returns:
            (new_decision, power_freed) or (None, 0) if reduction isn't viable.
        """
        phases = max(appliance.phases, 1)

        # Reconcile against this cycle's commanded demand when called from
        # SHED; direct callers use coordinator-normalized draw, including zero.
        current_power = (committed_power if committed_power is not None else
                         max(state.current_power, 0.0)
                         if state is not None and state.current_power_available else 0.0)

        # Available power = current consumption + instant_budget
        # (instant_budget is negative when committed decisions would draw
        # grid power, so this reduces the available power)
        available_power = current_power + instant_budget - max(self._off_threshold, 0)
        if available_power <= 0:
            return None, 0.0

        raw_amps = available_power / (self.grid_voltage * phases)
        new_amps = _step_floor(raw_amps, appliance.current_step)

        if new_amps < appliance.min_current:
            return None, 0.0

        new_amps = min(new_amps, appliance.max_current)
        new_power = new_amps * self.grid_voltage * phases
        power_freed = current_power - new_power

        decision = ControlDecision(
            appliance_id=appliance.id,
            action=Action.SET_CURRENT,
            target_current=new_amps,
            reason=f"Shed: reduced current to {new_amps:.1f}A ({new_power:.0f}W)",
            overrides_plan=False,
        )
        return decision, power_freed

    # ------------------------------------------------------------------
    # Phase 4: BATTERY DISCHARGE PROTECTION
    # ------------------------------------------------------------------

    def _battery_discharge_protection(
        self,
        decisions: list[ControlDecision],
        appliances: list[ApplianceConfig],
        battery_soc: float | None = None,
        min_battery_soc: float | None = None,
        *,
        force_charge: bool = False,
        auto_grid_charge_engaged: bool = False,
    ) -> BatteryDischargeAction:
        """Phase 4: Limit battery discharge when battery is at risk or grid is preferred.

        Three independent protections, evaluated in priority order:
        1. SoC-based (safety): When battery_soc < min_battery_soc, shed all
           shedable appliances and prevent all discharge (max_discharge_watts=0).
        2. Cheap-tariff / grid-import: When the integration is in any flavour
           of grid-import mode (any per-cycle decision with grid support,
           OR manual force_charge switch ON, OR auto-grid-charge engaged), block
           all discharge. No appliance shedding — we want loads to run on cheap
           grid, not be turned off.
        3. Big-consumer-based: If any big consumer is actively ON (or SET_CURRENT),
           set the battery max discharge to the lowest battery_max_discharge_override
           among active big consumers.

        SoC-based protection takes priority. Cheap-tariff overrules big-consumer
        because 0 is the most restrictive value.
        """
        appliance_by_id: dict[str, ApplianceConfig] = {
            a.id: a for a in appliances
        }

        # --- SoC-based protection ---
        if (
            battery_soc is not None
            and min_battery_soc is not None
            and battery_soc < min_battery_soc
        ):
            _LOGGER.info(
                "Battery SoC %.1f%% is below minimum %.1f%% — shedding appliances "
                "and blocking discharge",
                battery_soc, min_battery_soc,
            )
            # Shed all non-on_only, non-overridden appliances.
            # Shed big consumers first, then standard appliances.
            # ControlDecision is frozen, so we replace entries in the list.

            # Pass 1: shed big consumers
            for i, decision in enumerate(decisions):
                if decision.action not in (Action.ON, Action.SET_CURRENT):
                    continue
                appliance = appliance_by_id.get(decision.appliance_id)
                if appliance is None:
                    continue
                if appliance.on_only:
                    continue
                if appliance.override_active:
                    continue
                if not appliance.is_big_consumer:
                    continue
                decisions[i] = ControlDecision(
                    appliance_id=decision.appliance_id,
                    action=Action.OFF,
                    target_current=None,
                    reason=(
                        f"Battery SoC protection: {battery_soc:.1f}% < "
                        f"{min_battery_soc:.1f}% (big consumer shed)"
                    ),
                    overrides_plan=False,
                    bypasses_cooldown=True,
                )

            # Pass 2: shed remaining standard appliances
            for i, decision in enumerate(decisions):
                if decision.action not in (Action.ON, Action.SET_CURRENT):
                    continue
                appliance = appliance_by_id.get(decision.appliance_id)
                if appliance is None:
                    continue
                if appliance.on_only:
                    continue
                if appliance.override_active:
                    continue
                decisions[i] = ControlDecision(
                    appliance_id=decision.appliance_id,
                    action=Action.OFF,
                    target_current=None,
                    reason=(
                        f"Battery SoC protection: {battery_soc:.1f}% < "
                        f"{min_battery_soc:.1f}% (appliance shed)"
                    ),
                    overrides_plan=False,
                    bypasses_cooldown=True,
                )

            return BatteryDischargeAction(
                should_limit=True,
                max_discharge_watts=0,
            )

        # --- Cheap-tariff / grid-import discharge block ---
        # Block all discharge when the integration is in any "grid-import mode":
        # (a) any per-cycle decision with grid support (cheap-window
        #     override OR opportunity-cost path produce this tag);
        # (b) the manual force_charge switch is ON;
        # (c) the auto-grid-charge state machine is engaged.
        # Returns immediately with max_discharge_watts=0; this naturally overrules
        # any big-consumer override (since 0 is the smallest possible value).
        # Does NOT shed appliances — loads should run on cheap grid.
        grid_supplement_decisions = [
            d for d in decisions
            if d.action in (Action.ON, Action.SET_CURRENT)
            and d.uses_grid_supplement
        ]
        if grid_supplement_decisions or force_charge or auto_grid_charge_engaged:
            if grid_supplement_decisions:
                names = [
                    appliance_by_id[d.appliance_id].name
                    for d in grid_supplement_decisions
                    if d.appliance_id in appliance_by_id
                ]
                _LOGGER.info(
                    "Battery discharge blocked: grid-supplemented appliances active (%s)",
                    ", ".join(names) if names else "?",
                )
            elif force_charge:
                _LOGGER.info(
                    "Battery discharge blocked: manual force_charge switch ON",
                )
            else:
                _LOGGER.info(
                    "Battery discharge blocked: auto-grid-charge engaged",
                )
            return BatteryDischargeAction(
                should_limit=True,
                max_discharge_watts=0,
            )

        # --- Big-consumer-based discharge rate limiting ---
        # Find active big consumers and their discharge overrides
        active_overrides: list[float] = []
        for decision in decisions:
            if decision.action not in (Action.ON, Action.SET_CURRENT):
                continue
            appliance = appliance_by_id.get(decision.appliance_id)
            if appliance is None:
                continue
            if not appliance.is_big_consumer:
                continue
            if appliance.battery_max_discharge_override is not None:
                active_overrides.append(appliance.battery_max_discharge_override)
            else:
                _LOGGER.warning(
                    "Big consumer '%s' is active but has no battery_max_discharge_override "
                    "configured — battery discharge protection has no effect for this appliance",
                    appliance.name,
                )

        if active_overrides:
            limit_watts = min(active_overrides)
            consumer_names = [
                appliance_by_id[d.appliance_id].name
                for d in decisions
                if d.action in (Action.ON, Action.SET_CURRENT)
                and appliance_by_id.get(d.appliance_id) is not None
                and appliance_by_id[d.appliance_id].is_big_consumer
            ]
            _LOGGER.debug(
                "  Battery protection: limiting discharge to %.0fW (active big consumers: %s)",
                limit_watts, ", ".join(consumer_names),
            )
            return BatteryDischargeAction(
                should_limit=True,
                max_discharge_watts=limit_watts,
            )

        return BatteryDischargeAction(should_limit=False)


def _format_staying_on_standard(
    *,
    current_power: float,
    off_threshold: float,
    instant_budget: float,
) -> str:
    """Reason string for a non-dynamic appliance that stays on.

    Note: ``instant_budget`` at this call site has already been decremented
    by allocations to higher-priority appliances earlier in the cycle, so
    it is NOT identical to the system-wide "Excess Power" sensor. This is
    a decision-local instantaneous view — exactly the value Phase 3 SHED
    reads, so "(current: ±NNNW)" is the physically meaningful shed
    proximity indicator.
    """
    threshold_sign = "-" if off_threshold < 0 else "+"
    remaining_sign = "-" if instant_budget < 0 else "+"
    text = (
        f"Staying on ({current_power:.0f}W drawn) - "
        f"shed at {threshold_sign}{abs(off_threshold):.0f}W "
        f"(current: {remaining_sign}{abs(instant_budget):.0f}W)"
    )
    # Match SHED's strict less-than condition exactly (see _shed at the
    # `instant_budget < self._off_threshold` check). Using <= would
    # produce a one-tick boundary inconsistency where the suffix fires
    # but SHED does not. Note: in the public optimize() flow this branch
    # is effectively unreachable because any condition that satisfies
    # it also triggers SHED, which then overwrites the staying-on
    # decision. The suffix is preserved here as a defensive marker for
    # direct unit tests of this helper and any future code path that
    # might consume this reason string before SHED runs.
    if instant_budget < off_threshold:
        text += " (shed imminent)"
    return text


def _format_staying_on_dynamic(
    *,
    current_amperage: float | None,
    current_power: float,
    off_threshold: float,
    instant_budget: float,
) -> str:
    """Reason string for a dynamic current appliance that stays on.

    Falls back to the standard format when amperage is unknown.
    """
    if current_amperage is None:
        return _format_staying_on_standard(
            current_power=current_power,
            off_threshold=off_threshold,
            instant_budget=instant_budget,
        )
    threshold_sign = "-" if off_threshold < 0 else "+"
    remaining_sign = "-" if instant_budget < 0 else "+"
    text = (
        f"Staying on at {current_amperage:.1f}A ({current_power:.0f}W drawn) - "
        f"shed at {threshold_sign}{abs(off_threshold):.0f}W "
        f"(current: {remaining_sign}{abs(instant_budget):.0f}W)"
    )
    # Match SHED's strict less-than condition exactly (see _shed at the
    # `instant_budget < self._off_threshold` check). Using <= would
    # produce a one-tick boundary inconsistency where the suffix fires
    # but SHED does not. Note: in the public optimize() flow this branch
    # is effectively unreachable because any condition that satisfies
    # it also triggers SHED, which then overwrites the staying-on
    # decision. The suffix is preserved here as a defensive marker for
    # direct unit tests of this helper and any future code path that
    # might consume this reason string before SHED runs.
    if instant_budget < off_threshold:
        text += " (shed imminent)"
    return text

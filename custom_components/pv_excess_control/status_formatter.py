"""Compose the user-visible status string and attributes for an appliance.

This module is pure Python with no Home Assistant imports. It takes a
ControlDecision, ApplianceState, ApplianceConfig, BatteryDischargeAction,
and optional Plan, and returns a FormattedStatus containing:

- A decorated state string (the decision's reason + conditional suffixes
  for switch cooldown, battery soft-limit, and plan deviation).
- Structured attributes for a Lovelace card.

See docs/stabilization.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .const import Action
from .models import (
    ApplianceConfig,
    ApplianceState,
    BatteryDischargeAction,
    ControlDecision,
    Plan,
    PlanEntry,
)

HA_STATE_MAX_LENGTH = 255


@dataclass(frozen=True)
class FormattedStatus:
    """Composed status for a single appliance.

    `text` is the decorated state string for the HA sensor state.
    The other fields map 1:1 to sensor extra_state_attributes.
    """
    text: str
    action: str
    overrides_plan: bool
    cooldown_seconds_remaining: int | None
    switch_deferred: bool
    headroom_watts: float | None
    plan_action: str | None
    plan_window_start: datetime | None
    plan_window_end: datetime | None


def format_duration(seconds: float) -> str:
    """Render a duration in seconds as a compact human-readable string.

    Rules:
    - < 60s → "Ns"
    - 60s ≤ d < 600s → "Nmin" or "Nmin Ms" when M > 0
    - 600s ≤ d < 3600s → "Nmin" (seconds suppressed at this scale)
    - ≥ 3600s → "Nh" or "Nh Mmin" when M > 0 (seconds suppressed)

    Negative inputs are clamped to zero.
    """
    total = max(0, int(seconds))

    if total < 60:
        return f"{total}s"

    if total < 600:
        mins, secs = divmod(total, 60)
        if secs == 0:
            return f"{mins}min"
        return f"{mins}min {secs}s"

    if total < 3600:
        mins = total // 60
        return f"{mins}min"

    hours, rem = divmod(total, 3600)
    mins = rem // 60
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}min"


def format_status(
    decision: ControlDecision,
    state: ApplianceState,
    config: ApplianceConfig,
    *,
    switch_interval: int,
    battery_action: BatteryDischargeAction,
    plan: Plan | None,
    now: datetime,
) -> FormattedStatus:
    """Compose the final state string and attributes for an appliance.

    The base string is `decision.reason`. Conditional suffixes are
    appended in order: cooldown → battery → plan. If the combined
    string exceeds HA's 255-character state limit, the BASE REASON is
    truncated to make room — the suffixes are kept whole because they
    convey the most actionable information (cooldown remaining, battery
    limit, plan deviation). See the design doc for details.
    """
    # Build suffixes into a separate list so we can reserve their space
    # before truncating the base reason.
    suffixes: list[str] = []

    # --- S1: switch cooldown suffix ---
    cooldown_remaining, switch_deferred = _compute_cooldown(
        decision, state, switch_interval, now
    )
    if switch_deferred:
        suffixes.append(
            f" (switch deferred - "
            f"{format_duration(cooldown_remaining)} cooldown)"
        )

    # --- S3: battery discharge soft-limit suffix ---
    if _should_show_battery_limit(decision, config, battery_action):
        suffixes.append(
            f" [battery discharge limited to "
            f"{battery_action.max_discharge_watts:.0f}W]"
        )

    # --- S4: plan deviation suffix ---
    plan_action: str | None = None
    plan_window_start: datetime | None = None
    plan_window_end: datetime | None = None
    if decision.overrides_plan:
        matched = (
            _find_matching_plan_entry(plan, config.id, now)
            if plan is not None
            else None
        )
        # Suppress the suffix when the plan's action for this window matches
        # the decision's action. `overrides_plan=True` is currently set by
        # manual-override branches in the optimizer (see optimizer.py:313,
        # 324), which can coincide with a plan that already wants the same
        # action. Showing "[plan wanted: ON during 14:00-15:00]" on a
        # decision that is also ON reads like a disagreement when there
        # isn't one — so treat the "override" as a no-op for display when
        # plan and decision agree.
        if matched is not None and matched.action != decision.action:
            plan_action = matched.action.value
            plan_window_start = matched.window.start if matched.window else None
            plan_window_end = matched.window.end if matched.window else None
            plan_action_display = plan_action.upper().replace("_", " ")
            if matched.window:
                start = matched.window.start.strftime("%H:%M")
                end = matched.window.end.strftime("%H:%M")
                suffixes.append(
                    f" [plan wanted: {plan_action_display} "
                    f"during {start}-{end}]"
                )
            else:
                suffixes.append(f" [plan wanted: {plan_action_display}]")
        elif matched is None:
            suffixes.append(" [overrides plan]")
        # If matched is not None and actions agree, emit no suffix and leave
        # plan_action / plan_window_* as None — the formatter disagrees with
        # the optimizer's overrides_plan flag in this case because there is
        # no functional deviation to surface.

    text = _compose_with_truncation(decision.reason, suffixes)

    return FormattedStatus(
        text=text,
        action=decision.action.value,
        overrides_plan=decision.overrides_plan,
        cooldown_seconds_remaining=cooldown_remaining if switch_deferred else None,
        switch_deferred=switch_deferred,
        headroom_watts=None,
        plan_action=plan_action,
        plan_window_start=plan_window_start,
        plan_window_end=plan_window_end,
    )


def _compose_with_truncation(reason: str, suffixes: list[str]) -> str:
    """Compose `reason` + concatenated `suffixes`, capped at 255 chars.

    When the combined string exceeds the limit, the BASE REASON is
    truncated (with a "..." marker) so the suffixes survive whole.
    Suffixes carry the most actionable information (cooldown countdown,
    battery limit, plan deviation) and are usually short, so preserving
    them at the cost of the longer base reason is the better trade-off
    for the user.

    Degenerate fallback: if the suffixes alone are already so long that
    no useful reason can fit (less than 10 chars of headroom), revert
    to the simpler "truncate the whole composed string" behavior. This
    is unreachable in practice with the current ~30-50 char suffixes
    but is a safe fallback against future suffix bloat.
    """
    suffix_str = "".join(suffixes)
    suffix_len = len(suffix_str)

    # Degenerate case: suffixes alone fill almost the entire budget
    if suffix_len > HA_STATE_MAX_LENGTH - 10:
        composed = reason + suffix_str
        if len(composed) <= HA_STATE_MAX_LENGTH:
            return composed
        return composed[: HA_STATE_MAX_LENGTH - 3] + "..."

    reason_budget = HA_STATE_MAX_LENGTH - suffix_len
    if len(reason) <= reason_budget:
        return reason + suffix_str
    return reason[: reason_budget - 3] + "..." + suffix_str


def _compute_cooldown(
    decision: ControlDecision,
    state: ApplianceState,
    switch_interval: int,
    now: datetime,
) -> tuple[int, bool]:
    """Return (seconds_remaining, switch_deferred) for the cooldown decoration.

    A decision "would switch" when it flips the on/off state. SET_CURRENT
    and IDLE never count as switches. Safety-OFF decisions bypass the
    cooldown entirely via the `bypasses_cooldown` flag.
    """
    if decision.bypasses_cooldown:
        return (0, False)
    if state.last_state_change is None:
        return (0, False)

    would_switch_on = decision.action == Action.ON and not state.is_on
    would_switch_off = decision.action == Action.OFF and state.is_on
    if not (would_switch_on or would_switch_off):
        return (0, False)

    elapsed = (now - state.last_state_change).total_seconds()
    remaining = int(switch_interval - elapsed)
    if remaining <= 0:
        return (0, False)
    return (remaining, True)


def _should_show_battery_limit(
    decision: ControlDecision,
    config: ApplianceConfig,
    battery_action: BatteryDischargeAction,
) -> bool:
    """Return True when the battery soft-limit suffix should be appended.

    Conditions, checked in order:
    1. `battery_action.should_limit` must be True.
    2. `battery_action.max_discharge_watts` must not be None. This is a
       defensive guard — the two optimizer call sites that set
       `should_limit=True` both provide a numeric value (see
       optimizer.py around `_battery_discharge_protection`), so the
       combination `should_limit=True, max_discharge_watts=None` is
       structurally unreachable in normal operation. The guard exists
       only to protect against future constructor changes.
    3. The appliance must be a big consumer. The battery limit is a
       single global state, not per-appliance; when a big consumer
       triggers the discharge rate limit, every currently-active big
       consumer displays the suffix (this is intentional — any big
       consumer operating under the limit should show it).
    4. The optimizer's own reason must not already contain
       "Battery SoC protection". The SoC-shed paths at
       optimizer.py:1301 and :1323 produce that prefix, and we skip
       the suffix there to avoid duplicate mentions.
    """
    if not battery_action.should_limit:
        return False
    if battery_action.max_discharge_watts is None:
        return False
    if not config.is_big_consumer:
        return False
    if "Battery SoC protection" in decision.reason:
        return False
    return True


def _find_matching_plan_entry(
    plan: Plan,
    appliance_id: str,
    now: datetime,
) -> PlanEntry | None:
    """Return the first plan entry for this appliance whose window contains now.

    Handles naive/aware datetime mismatches by falling back to naive
    comparison if direct comparison raises TypeError (matches the
    optimizer's own handling in `_plan_says_on`).

    Note on windowless entries: this helper returns a `PlanEntry` whose
    `window` is None as if it "always applies", but no current code path
    in `planner.py` actually produces windowless entries — every
    `PlanEntry(...)` constructor passes an explicit `TariffWindow`. The
    branch is a latent semantic divergence from `optimizer._plan_says_on`
    which skips windowless entries entirely, but is unreachable from the
    current planner. If windowless entries ever become reachable, both
    helpers should be reconciled.
    """
    for entry in plan.entries:
        if entry.appliance_id != appliance_id:
            continue
        window = entry.window
        if window is None:
            # Latent branch — see the note above. Current planner does
            # not produce windowless entries.
            return entry
        try:
            if window.start <= now <= window.end:
                return entry
        except TypeError:
            now_naive = now.replace(tzinfo=None)
            start_naive = (
                window.start.replace(tzinfo=None)
                if window.start.tzinfo else window.start
            )
            end_naive = (
                window.end.replace(tzinfo=None)
                if window.end.tzinfo else window.end
            )
            if start_naive <= now_naive <= end_naive:
                return entry
    return None

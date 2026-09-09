"""End-to-end scenario tests for PV Excess Control.

Each test sets up realistic conditions and verifies the system makes correct
decisions, exercising the complete optimizer flow:
  sensors -> optimizer -> decisions
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.pv_excess_control.models import (
    Action,
    ApplianceConfig,
    ApplianceState,
    BatteryDischargeAction,
    BatteryStrategy,
    BatteryTarget,
    ControlDecision,
    Plan,
    PlanEntry,
    PlanReason,
    PowerState,
    TariffInfo,
    TariffWindow,
)
from custom_components.pv_excess_control.optimizer import Optimizer

# Import shared helpers from the unit-level test module
from tests.test_optimizer import (
    _empty_plan,
    _make_appliance,
    _optimizer_for_tests,
    _make_power,
    _make_state,
    _make_tariff,
    _utcnow,
)


# ---------------------------------------------------------------------------
# Scenario 1: Sunny Day
# ---------------------------------------------------------------------------

class TestSunnyDay:
    """PV producing 5kW, household load 1.5kW -> 3.5kW excess.

    Three appliances:
      - EV charger  (priority  1, 3000W)
      - Water heater (priority  5, 2000W)
      - Pool pump   (priority 10, 1000W)

    Expected allocation:
      EV charger  ON  (consumes 3000W; 500W remaining)
      Water heater IDLE (needs 2000+200=2200W; only 500W left)
      Pool pump   IDLE (needs 1000+200=1200W; only 500W left)
    """

    def test_priority_allocation_with_limited_excess(self):
        """Sunny day: allocate 3.5kW excess to highest priority first."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliances = [
            _make_appliance(id="ev", priority=1, nominal_power=3000.0),
            _make_appliance(id="water", priority=5, nominal_power=2000.0),
            _make_appliance(id="pool", priority=10, nominal_power=1000.0),
        ]
        states = [
            _make_state(id="ev"),
            _make_state(id="water"),
            _make_state(id="pool"),
        ]
        power = _make_power(excess=3500.0, pv=5000.0)
        history = [power] * 6

        result = optimizer.optimize(power, appliances, states, _empty_plan(), history, _make_tariff())
        decisions = {d.appliance_id: d for d in result.decisions}

        # EV charger gets the 3kW (highest priority)
        assert decisions["ev"].action == Action.ON, (
            f"EV charger should be ON (priority 1 with 3.5kW excess) but got {decisions['ev'].action}"
        )
        # After EV takes 3kW only 500W remain - not enough for water (2.2kW needed) or pool (1.2kW needed)
        assert decisions["water"].action == Action.IDLE, (
            f"Water heater should be IDLE (insufficient remaining excess) but got {decisions['water'].action}"
        )
        assert decisions["pool"].action == Action.IDLE, (
            f"Pool pump should be IDLE (insufficient remaining excess) but got {decisions['pool'].action}"
        )

    def test_priority_order_is_respected_regardless_of_input_order(self):
        """Priority ordering applies even when appliances are given out of order."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        # Deliberately pass lowest priority first
        appliances = [
            _make_appliance(id="pool", priority=10, nominal_power=1000.0),
            _make_appliance(id="water", priority=5, nominal_power=2000.0),
            _make_appliance(id="ev", priority=1, nominal_power=3000.0),
        ]
        states = [
            _make_state(id="pool"),
            _make_state(id="water"),
            _make_state(id="ev"),
        ]
        power = _make_power(excess=3500.0, pv=5000.0)

        result = optimizer.optimize(power, appliances, states, _empty_plan(), [power], _make_tariff())
        decisions = {d.appliance_id: d for d in result.decisions}

        assert decisions["ev"].action == Action.ON
        assert decisions["water"].action == Action.IDLE
        assert decisions["pool"].action == Action.IDLE

    def test_all_appliances_fit_when_excess_is_large(self):
        """With enough excess all three appliances turn ON."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliances = [
            _make_appliance(id="ev", priority=1, nominal_power=3000.0),
            _make_appliance(id="water", priority=5, nominal_power=2000.0),
            _make_appliance(id="pool", priority=10, nominal_power=1000.0),
        ]
        states = [
            _make_state(id="ev"),
            _make_state(id="water"),
            _make_state(id="pool"),
        ]
        # 3000+200 + 2000+200 + 1000+200 = 6600W needed
        power = _make_power(excess=7000.0, pv=8500.0)

        result = optimizer.optimize(power, appliances, states, _empty_plan(), [power], _make_tariff())
        decisions = {d.appliance_id: d for d in result.decisions}

        assert decisions["ev"].action == Action.ON
        assert decisions["water"].action == Action.ON
        assert decisions["pool"].action == Action.ON


# ---------------------------------------------------------------------------
# Scenario 2: Cloudy Day (Hysteresis)
# ---------------------------------------------------------------------------

class TestCloudyDay:
    """Excess oscillating: history [500, -20, 300, -30, 200, -10] watts.

    One appliance (1kW) currently ON. The OFF threshold is -50W.
    Average excess over history = (500-20+300-30+200-10)/6 = 940/6 ≈ 157W.
    Since average (157W) > OFF threshold (-50W), the appliance stays ON.
    """

    def test_hysteresis_prevents_flapping_on_oscillating_excess(self):
        """Oscillating excess with positive average: appliance stays ON."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliances = [_make_appliance(id="heater", nominal_power=1000.0)]
        states = [_make_state(id="heater", is_on=True, current_power=1000.0)]

        history = [
            _make_power(excess=500),
            _make_power(excess=-20),
            _make_power(excess=300),
            _make_power(excess=-30),
            _make_power(excess=200),
            _make_power(excess=-10),
        ]
        # Current reading is mildly negative
        power = _make_power(excess=-10)

        result = optimizer.optimize(power, appliances, states, _empty_plan(), history, _make_tariff())

        assert len(result.decisions) == 1
        decision = result.decisions[0]
        # Should stay ON: average ~157W is well above the -50W OFF threshold
        assert decision.action != Action.OFF, (
            f"Appliance should NOT be turned off (history avg > OFF threshold) but got {decision.action}"
        )

    def test_hysteresis_turns_off_when_avg_below_threshold(self):
        """History with large negatives: average drops below -50W -> OFF."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliances = [_make_appliance(id="heater", nominal_power=1000.0)]
        states = [_make_state(id="heater", is_on=True, current_power=1000.0)]

        # Average = (-300-400-200-500-100-350)/6 ≈ -308W (well below -50W threshold)
        history = [
            _make_power(excess=-300),
            _make_power(excess=-400),
            _make_power(excess=-200),
            _make_power(excess=-500),
            _make_power(excess=-100),
            _make_power(excess=-350),
        ]
        power = _make_power(excess=-350)

        result = optimizer.optimize(power, appliances, states, _empty_plan(), history, _make_tariff())

        assert len(result.decisions) == 1
        assert result.decisions[0].action == Action.OFF, (
            "Appliance should turn OFF when average excess is far below OFF threshold"
        )

    def test_single_negative_spike_triggers_shed_by_design(self):
        """Non-dynamic appliance: one negative instant reading DOES cause
        shed, even if the averaged history is positive.

        This is a deliberate behavior change from the pre-2026-04-08
        optimizer, which read avg_excess for the shed decision and
        therefore ignored transient physical drops. The new dual-budget
        model reads instant_budget (physical reality) in Phase 3 SHED.

        For non-dynamic appliances there is no min-clamp to reduce
        power gracefully, so the only available action is full OFF.
        Dynamic-current appliances get a softer response via the
        Phase 2 ALLOCATE min-clamp; see TestDynamicCurrentBumpClamp.

        If prod observation shows this is too aggressive, a follow-up
        can add cycle-based hysteresis (shed only after N consecutive
        cycles below off_threshold). Not in scope for 2026-04-08.
        """
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliances = [_make_appliance(id="heater", nominal_power=1000.0)]
        states = [_make_state(id="heater", is_on=True, current_power=1000.0)]

        history = [
            _make_power(excess=600),
            _make_power(excess=800),
            _make_power(excess=-200),  # single spike
            _make_power(excess=700),
            _make_power(excess=900),
            _make_power(excess=600),
        ]
        power = _make_power(excess=-200)

        result = optimizer.optimize(power, appliances, states, _empty_plan(), history, _make_tariff())

        # New behavior: instant_budget = -200 < off_threshold (-50) → shed fires.
        assert result.decisions[0].action == Action.OFF
        assert "Shed" in result.decisions[0].reason


# ---------------------------------------------------------------------------
# Scenario 3: Cheap Night Tariff
# ---------------------------------------------------------------------------

class TestCheapNightTariff:
    """No solar (excess=0), cheap tariff below threshold.

    Appliance with allow_grid_supplement=True, max_grid_power=2000W.
    Expected: appliance turns ON from grid.
    """

    def test_cheap_tariff_with_zero_excess_turns_appliance_on(self):
        """Cheap tariff, no solar: grid supplement activates appliance."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(
            id="water_heater",
            nominal_power=2000.0,
            allow_grid_supplement=True,
            max_grid_power=2000.0,
        )
        state = _make_state(id="water_heater")
        power = _make_power(excess=0.0, pv=0.0)
        tariff = _make_tariff(current_price=0.05, feed_in=0.08, cheap_threshold=0.10)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=tariff,
        )

        assert len(result.decisions) == 1
        decision = result.decisions[0]
        # Cheap tariff (0.05 <= 0.10 threshold) + allow_grid_supplement -> ON
        assert decision.action == Action.ON, (
            f"Appliance should be ON from grid supplement during cheap tariff but got {decision.action}. "
            f"Reason: {decision.reason}"
        )
        assert "grid supplement" in decision.reason.lower() or "grid" in decision.reason.lower()

    def test_expensive_tariff_with_no_excess_stays_idle(self):
        """Expensive tariff, no solar: grid supplement does NOT activate appliance."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(
            id="water_heater",
            nominal_power=2000.0,
            allow_grid_supplement=True,
            max_grid_power=2000.0,
        )
        state = _make_state(id="water_heater")
        power = _make_power(excess=0.0, pv=0.0)
        tariff = _make_tariff(current_price=0.30, feed_in=0.08, cheap_threshold=0.10)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=tariff,
        )

        assert result.decisions[0].action == Action.IDLE


# ---------------------------------------------------------------------------
# Scenario 4: EV Deadline (plan entry with DEADLINE reason)
# ---------------------------------------------------------------------------

class TestEVDeadline:
    """Plan has a deadline entry for the EV charger approaching.

    The optimizer receives a plan with a DEADLINE entry for the EV. When there
    is enough excess the optimizer turns the EV ON (the plan entry confirms intent).
    When there is no excess but a deadline entry says ON, the plan bias is noted
    in the plan entries even if the optimizer itself doesn't yet enforce it.
    """

    def _make_plan_with_deadline_entry(self, appliance_id: str = "ev") -> Plan:
        """Create a plan that has a DEADLINE entry for the given appliance."""
        now = _utcnow()
        window = TariffWindow(
            start=now,
            end=now + timedelta(hours=1),
            price=0.25,
            is_cheap=False,
        )
        entry = PlanEntry(
            appliance_id=appliance_id,
            action=Action.ON,
            target_current=None,
            window=window,
            reason=PlanReason.DEADLINE,
            priority=1,
        )
        return Plan(
            created_at=now,
            horizon=timedelta(hours=12),
            entries=[entry],
            battery_target=BatteryTarget(
                target_soc=90.0,
                target_time=datetime(2026, 3, 23, 7, 0, tzinfo=timezone.utc),
                strategy=BatteryStrategy.BALANCED,
            ),
            confidence=0.9,
        )

    def test_ev_turns_on_with_deadline_plan_and_excess(self):
        """EV charger ON when plan says DEADLINE and excess is available."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(id="ev", priority=1, nominal_power=3000.0)
        state = _make_state(id="ev")
        power = _make_power(excess=4000.0)
        plan = self._make_plan_with_deadline_entry("ev")

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=plan,
            power_history=[power],
            tariff=_make_tariff(),
        )

        decision = result.decisions[0]
        assert decision.appliance_id == "ev"
        assert decision.action == Action.ON, (
            f"EV should be ON with excess available and deadline plan but got {decision.action}"
        )

    def test_plan_deadline_entry_is_structured_correctly(self):
        """Plan entry with DEADLINE reason has correct fields."""
        plan = self._make_plan_with_deadline_entry("ev")
        assert len(plan.entries) == 1
        entry = plan.entries[0]
        assert entry.appliance_id == "ev"
        assert entry.action == Action.ON
        assert entry.reason == PlanReason.DEADLINE
        assert entry.window is not None

    def test_ev_idle_without_excess_despite_deadline(self):
        """Without excess the optimizer keeps EV IDLE (plan hint not yet enforced)."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(id="ev", priority=1, nominal_power=3000.0)
        state = _make_state(id="ev")
        # Zero excess: not enough for 3000+200=3200W
        power = _make_power(excess=0.0)
        plan = self._make_plan_with_deadline_entry("ev")

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=plan,
            power_history=[power],
            tariff=_make_tariff(),
        )

        decision = result.decisions[0]
        # Current optimizer only uses excess; plan enforcement is Phase 5
        assert decision.action == Action.IDLE


# ---------------------------------------------------------------------------
# Scenario 5: Export Limit
# ---------------------------------------------------------------------------

class TestExportLimit:
    """PV producing 6kW, load 1kW -> excess 5kW.

    Without export limit a 2kW appliance would absorb part of the excess.
    The optimizer sees 5kW excess and turns the 2kW appliance ON, which
    reduces grid export from 5kW to 3kW (within a 3kW cap).
    """

    def test_appliance_absorbs_excess_keeping_export_within_limit(self):
        """5kW excess: 2kW appliance turns ON, reducing grid export to 3kW."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(id="heat_pump", nominal_power=2000.0, priority=1)
        state = _make_state(id="heat_pump")
        # PV=6kW, load=1kW -> excess=5kW
        power = _make_power(excess=5000.0, pv=6000.0)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=_make_tariff(),
        )

        decision = result.decisions[0]
        assert decision.action == Action.ON, (
            f"Appliance should absorb excess power (5kW available, 2.2kW needed) but got {decision.action}"
        )

    def test_multiple_appliances_absorb_large_excess(self):
        """High excess: multiple appliances run to reduce grid export."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliances = [
            _make_appliance(id="heat_pump", nominal_power=2000.0, priority=1),
            _make_appliance(id="water_heater", nominal_power=2000.0, priority=2),
        ]
        states = [
            _make_state(id="heat_pump"),
            _make_state(id="water_heater"),
        ]
        # 5kW excess - enough for both (2200+2200=4400W needed)
        power = _make_power(excess=5000.0, pv=6000.0)

        result = optimizer.optimize(
            power_state=power,
            appliances=appliances,
            appliance_states=states,
            plan=_empty_plan(),
            power_history=[power],
            tariff=_make_tariff(),
        )

        decisions = {d.appliance_id: d for d in result.decisions}
        assert decisions["heat_pump"].action == Action.ON
        assert decisions["water_heater"].action == Action.ON

    def test_insufficient_excess_keeps_appliance_idle(self):
        """With only 1kW excess a 2kW appliance stays IDLE."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(id="heat_pump", nominal_power=2000.0, priority=1)
        state = _make_state(id="heat_pump")
        power = _make_power(excess=1000.0, pv=2000.0)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=_make_tariff(),
        )

        assert result.decisions[0].action == Action.IDLE


# ---------------------------------------------------------------------------
# Scenario 6: EV Disconnected
# ---------------------------------------------------------------------------

class TestEVDisconnected:
    """Plenty of excess but EV is not plugged in.

    ev_connected_entity is set, ev_connected=False in state.
    Expected: EV charger stays IDLE with 'disconnected' in reason.
    """

    def test_ev_disconnected_stays_idle_despite_excess(self):
        """EV charger IDLE when cable not connected, even with ample excess."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(
            id="ev_charger",
            priority=1,
            nominal_power=7400.0,
            ev_connected_entity="binary_sensor.ev_connected",
        )
        state = _make_state(id="ev_charger", ev_connected=False)
        # More than enough excess for the EV charger
        power = _make_power(excess=8000.0, pv=9000.0)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=_make_tariff(),
        )

        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert decision.action == Action.IDLE, (
            f"EV charger should be IDLE when disconnected but got {decision.action}"
        )
        assert "disconnected" in decision.reason.lower(), (
            f"Reason should mention 'disconnected' but got: {decision.reason!r}"
        )

    def test_ev_connected_turns_on_with_excess(self):
        """Sanity check: EV charger turns ON when connected and excess available."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(
            id="ev_charger",
            priority=1,
            nominal_power=7400.0,
            ev_connected_entity="binary_sensor.ev_connected",
        )
        # ev_connected=True
        state = _make_state(id="ev_charger", ev_connected=True)
        power = _make_power(excess=8000.0, pv=9000.0)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=_make_tariff(),
        )

        assert result.decisions[0].action == Action.ON

    def test_ev_without_connected_entity_turns_on(self):
        """If no ev_connected_entity is configured the EV check is skipped."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(
            id="ev_charger",
            priority=1,
            nominal_power=7400.0,
            ev_connected_entity=None,  # not configured
        )
        state = _make_state(id="ev_charger", ev_connected=False)
        power = _make_power(excess=8000.0)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=_make_tariff(),
        )

        # No ev_connected_entity -> disconnected check skipped -> ON
        assert result.decisions[0].action == Action.ON


# ---------------------------------------------------------------------------
# Scenario 7: Big Consumer Battery Protection
# ---------------------------------------------------------------------------

class TestBigConsumerBatteryProtection:
    """Two big consumers both active.

    Heat pump:  big consumer, battery_max_discharge_override=300W
    EV charger: big consumer, battery_max_discharge_override=500W

    Expected: BatteryDischargeAction with should_limit=True and
              max_discharge_watts=300 (the minimum of the two overrides).
    """

    def test_two_big_consumers_use_lowest_discharge_limit(self):
        """Two big consumers both ON: use the most restrictive discharge limit."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        heat_pump = _make_appliance(
            id="heat_pump",
            priority=1,
            nominal_power=2000.0,
            is_big_consumer=True,
            battery_max_discharge_override=300.0,
        )
        ev_charger = _make_appliance(
            id="ev_charger",
            priority=2,
            nominal_power=7400.0,
            is_big_consumer=True,
            battery_max_discharge_override=500.0,
        )
        state_hp = _make_state(id="heat_pump")
        state_ev = _make_state(id="ev_charger")
        # Enough excess for both
        power = _make_power(excess=10000.0, pv=11000.0)

        result = optimizer.optimize(
            power_state=power,
            appliances=[heat_pump, ev_charger],
            appliance_states=[state_hp, state_ev],
            plan=_empty_plan(),
            power_history=[power],
            tariff=_make_tariff(),
        )

        decisions = {d.appliance_id: d for d in result.decisions}
        assert decisions["heat_pump"].action == Action.ON
        assert decisions["ev_charger"].action == Action.ON

        battery_action = result.battery_discharge_action
        assert battery_action.should_limit is True, (
            "Battery discharge should be limited when big consumers are active"
        )
        assert battery_action.max_discharge_watts == 300.0, (
            f"Expected limit of 300W (lowest override) but got {battery_action.max_discharge_watts}W"
        )

    def test_single_big_consumer_sets_discharge_limit(self):
        """Single big consumer active: discharge limit set to its override."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        heat_pump = _make_appliance(
            id="heat_pump",
            priority=1,
            nominal_power=2000.0,
            is_big_consumer=True,
            battery_max_discharge_override=300.0,
        )
        state = _make_state(id="heat_pump")
        power = _make_power(excess=3000.0)

        result = optimizer.optimize(
            power_state=power,
            appliances=[heat_pump],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=_make_tariff(),
        )

        assert result.battery_discharge_action.should_limit is True
        assert result.battery_discharge_action.max_discharge_watts == 300.0

    def test_no_big_consumers_no_discharge_limit(self):
        """No big consumers active: battery discharge is not limited."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(
            id="washer",
            priority=1,
            nominal_power=1000.0,
            is_big_consumer=False,
        )
        state = _make_state(id="washer")
        power = _make_power(excess=2000.0)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=_make_tariff(),
        )

        assert result.battery_discharge_action.should_limit is False
        assert result.battery_discharge_action.max_discharge_watts is None

    def test_big_consumer_idle_does_not_trigger_limit(self):
        """Big consumer that is IDLE (insufficient excess) does not trigger limit."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        heat_pump = _make_appliance(
            id="heat_pump",
            priority=1,
            nominal_power=5000.0,  # Very large - won't fit in excess
            is_big_consumer=True,
            battery_max_discharge_override=300.0,
        )
        state = _make_state(id="heat_pump")
        # Only 1kW excess - not enough for 5kW heat pump
        power = _make_power(excess=1000.0)

        result = optimizer.optimize(
            power_state=power,
            appliances=[heat_pump],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=_make_tariff(),
        )

        decisions = {d.appliance_id: d for d in result.decisions}
        assert decisions["heat_pump"].action == Action.IDLE
        # Big consumer is not active -> no discharge limit
        assert result.battery_discharge_action.should_limit is False


# ---------------------------------------------------------------------------
# Scenario 8: High Feed-In Tariff
# ---------------------------------------------------------------------------

class TestHighFeedInTariff:
    """A favorable tariff permits operation but cannot manufacture solar.

    At one net meter, consuming 1kW from an existing 2kW surplus leaves
    1kW export; it is not simultaneous full export and a fictitious import.
    """

    def test_high_feed_in_accounts_for_physical_solar(self):
        """Enough physical solar means zero imported portion, regardless of tariff."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(
            id="water_heater",
            nominal_power=1000.0,
            allow_grid_supplement=True,
            max_grid_power=2000.0,
        )
        state = _make_state(id="water_heater")
        # 2kW excess - more than enough for the 1kW appliance (1000+200=1200W needed)
        power = _make_power(excess=2000.0, pv=3500.0)
        # Feed-in (0.12) > grid price (0.05): prefer exporting
        tariff = _make_tariff(current_price=0.05, feed_in=0.12, cheap_threshold=0.10)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=tariff,
        )

        assert len(result.decisions) == 1
        decision = result.decisions[0]
        assert decision.action == Action.ON
        assert not decision.uses_grid_supplement
        assert decision.grid_supplement_watts == 0

    def test_high_feed_in_does_not_apply_without_grid_supplement(self):
        """Opportunity cost only applies to allow_grid_supplement appliances.

        A regular solar-only appliance with enough excess should still turn ON.
        """
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(
            id="pool_pump",
            nominal_power=1000.0,
            allow_grid_supplement=False,  # No grid supplement
        )
        state = _make_state(id="pool_pump")
        power = _make_power(excess=2000.0)
        tariff = _make_tariff(current_price=0.05, feed_in=0.12, cheap_threshold=0.10)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=tariff,
        )

        # No allow_grid_supplement -> opportunity cost check skipped -> ON from solar
        assert result.decisions[0].action == Action.ON

    def test_normal_tariff_turns_appliance_on(self):
        """When feed-in < grid price (normal case) the appliance turns ON from solar."""
        optimizer = _optimizer_for_tests(grid_voltage=230)
        appliance = _make_appliance(
            id="water_heater",
            nominal_power=1000.0,
            allow_grid_supplement=True,
            max_grid_power=2000.0,
        )
        state = _make_state(id="water_heater")
        power = _make_power(excess=2000.0)
        # Normal: feed-in (0.08) < grid price (0.25) -> use solar
        tariff = _make_tariff(current_price=0.25, feed_in=0.08, cheap_threshold=0.10)

        result = optimizer.optimize(
            power_state=power,
            appliances=[appliance],
            appliance_states=[state],
            plan=_empty_plan(),
            power_history=[power],
            tariff=tariff,
        )

        # Grid price > feed-in: opportunity cost check doesn't trigger -> ON
        assert result.decisions[0].action == Action.ON

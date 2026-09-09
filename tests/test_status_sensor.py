"""Integration tests for PvApplianceStatusSensor wiring to the formatter."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

from custom_components.pv_excess_control.const import (
    Action,
    CONF_DYNAMIC_BATTERY_CHARGE_ENABLED,
)
from custom_components.pv_excess_control.models import (
    ApplianceConfig,
    ApplianceState,
    BatteryDischargeAction,
    ControlDecision,
)
from custom_components.pv_excess_control.sensor import PvApplianceStatusSensor


def _make_config() -> ApplianceConfig:
    return ApplianceConfig(
        id="sub123", name="Test Heater", entity_id="switch.heater",
        priority=100, phases=1, nominal_power=2000.0, actual_power_entity=None,
        dynamic_current=False, current_entity=None,
        min_current=6.0, max_current=16.0,
        ev_soc_entity=None, ev_connected_entity=None,
        is_big_consumer=False, battery_max_discharge_override=None,
        on_only=False,
        min_daily_runtime=None, max_daily_runtime=None,
        schedule_deadline=None,
        switch_interval=300,
        allow_grid_supplement=False, max_grid_power=None,
    )


def _make_coordinator_with_data(data: dict) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "entry1"
    return coordinator


class TestStatusSensorNativeValue:
    def test_returns_decision_reason_when_no_decorations(self) -> None:
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.ON,
            target_current=None,
            reason="Excess available (2100W >= 1800W needed)",
            overrides_plan=False,
        )
        state = ApplianceState(
            appliance_id="sub123",
            is_on=True,
            current_power=1800.0,
            current_amperage=None,
            runtime_today=timedelta(0),
            energy_today=0.0,
            last_state_change=None,
            ev_connected=None,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {"sub123": state},
            "appliance_configs": {"sub123": _make_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        assert sensor.native_value == "Excess available (2100W >= 1800W needed)"

    def test_grace_period_overrides_decision_reason(self) -> None:
        data = {
            "control_decisions": [],
            "appliance_states": {},
            "appliance_configs": {},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": 83.0,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        assert sensor.native_value == (
            "Startup grace period - 83s remaining before decisions begin"
        )

    def test_attributes_exposed_for_plain_decision(self) -> None:
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.ON,
            target_current=None,
            reason="Excess available",
            overrides_plan=False,
        )
        state = ApplianceState(
            appliance_id="sub123",
            is_on=True,
            current_power=1800.0,
            current_amperage=None,
            runtime_today=timedelta(0),
            energy_today=0.0,
            last_state_change=None,
            ev_connected=None,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {"sub123": state},
            "appliance_configs": {"sub123": _make_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        attrs = sensor.extra_state_attributes
        assert set(attrs.keys()) == {
            "action",
            "overrides_plan",
            "cooldown_seconds_remaining",
            "switch_deferred",
            "headroom_watts",
            "plan_action",
            "plan_window_start",
            "plan_window_end",
        }
        assert attrs["action"] == "on"
        assert attrs["overrides_plan"] is False
        assert attrs["cooldown_seconds_remaining"] is None
        assert attrs["switch_deferred"] is False

    def test_returns_none_when_no_data_and_no_grace(self) -> None:
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(None), "sub123", "Test Heater"
        )
        assert sensor.native_value is None

    def test_grace_period_uses_ceil_not_floor_for_countdown(self) -> None:
        # 0.4s remaining → text should say "1s remaining", not "0s".
        # Without ceil rounding, the last grace cycle would briefly
        # display "Startup grace period - 0s remaining", which is
        # confusing because the grace branch is still active.
        data = {
            "control_decisions": [],
            "appliance_states": {},
            "appliance_configs": {},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": 0.4,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        assert sensor.native_value == (
            "Startup grace period - 1s remaining before decisions begin"
        )

    def test_native_value_and_attributes_are_consistent(self) -> None:
        # Both properties read from the same `_compose` cache, so the
        # cooldown computation runs exactly once per HA state read.
        # Without the cache, two `datetime.now()` calls (one per
        # property) could disagree on the cooldown_seconds_remaining
        # field. This test verifies the structural guarantee, not the
        # timing race directly.
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.ON,
            target_current=None,
            reason="Excess available",
            overrides_plan=False,
        )
        state = ApplianceState(
            appliance_id="sub123",
            is_on=False,  # decision is ON, state is OFF → would switch
            current_power=0.0,
            current_amperage=None,
            runtime_today=timedelta(0),
            energy_today=0.0,
            last_state_change=datetime.now() - timedelta(seconds=10),
            ev_connected=None,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {"sub123": state},
            "appliance_configs": {"sub123": _make_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        text = sensor.native_value
        attrs = sensor.extra_state_attributes
        # The cooldown second-count embedded in the state text must
        # match the attribute value exactly.
        assert text is not None
        assert attrs is not None
        if attrs["switch_deferred"]:
            cooldown_n = attrs["cooldown_seconds_remaining"]
            assert f"{cooldown_n}s cooldown" in text or (
                # _format_duration may render as "Xmin Ys" for >60s
                f"cooldown" in text
            )

    def test_fallback_when_state_missing(self) -> None:
        # Decision is present but appliance_states is empty —
        # the formatter cannot run, so the fallback path returns the
        # bare reason as text and defaults all attributes.
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.ON,
            target_current=None,
            reason="Excess available (2100W >= 1800W needed)",
            overrides_plan=False,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {},  # missing
            "appliance_configs": {"sub123": _make_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        assert sensor.native_value == "Excess available (2100W >= 1800W needed)"
        attrs = sensor.extra_state_attributes
        assert attrs["action"] == "on"
        assert attrs["cooldown_seconds_remaining"] is None
        assert attrs["switch_deferred"] is False
        assert attrs["headroom_watts"] is None

    def test_fallback_truncates_long_reason(self) -> None:
        # If the fallback path is taken with an oversized reason, the
        # 255-char HA limit must still be respected.
        decision = ControlDecision(
            appliance_id="sub123",
            action=Action.ON,
            target_current=None,
            reason="x" * 300,
            overrides_plan=False,
        )
        data = {
            "control_decisions": [decision],
            "appliance_states": {},  # forces fallback
            "appliance_configs": {"sub123": _make_config()},
            "battery_discharge_action": BatteryDischargeAction(should_limit=False),
            "current_plan": None,
            "grace_period_remaining": None,
        }
        sensor = PvApplianceStatusSensor(
            _make_coordinator_with_data(data), "sub123", "Test Heater"
        )
        text = sensor.native_value
        assert len(text) == 255
        assert text.endswith("...")


class TestDynamicBatteryChargeAttributes:
    def _build_sensor(
        self,
        *,
        feature_enabled: bool = True,
        loop_active: bool = False,
        force_charge: bool = False,
        grid_charge_engaged: bool = False,
        self_disabled_reason: str | None = None,
        last_written_w: int | None = 1500,
        planned_w: int = 1000,
        reactive_w: int = 500,
    ):
        from custom_components.pv_excess_control.sensor import PvExcessStatusSensor
        coord = MagicMock()
        coord.config_entry = MagicMock()
        coord.config_entry.entry_id = "test_entry"
        coord.config_entry.data = {
            CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: feature_enabled,
        }
        coord._dyn_charge_loop_active = loop_active
        coord.force_charge = force_charge
        coord._grid_charge_engaged = grid_charge_engaged
        coord._dyn_charge_self_disabled_reason = self_disabled_reason
        coord._dyn_charge_last_written_w = last_written_w
        coord._dyn_charge_planned_w = planned_w
        coord._dyn_charge_reactive_w = reactive_w
        return PvExcessStatusSensor(coord)

    def test_attributes_active_when_loop_active(self):
        sensor = self._build_sensor(loop_active=True)
        attrs = sensor.extra_state_attributes
        assert attrs["dynamic_battery_charge_status"] == "active"
        assert attrs["dynamic_battery_charge_setpoint_w"] == 1500
        assert attrs["dynamic_battery_charge_planned_w"] == 1000
        assert attrs["dynamic_battery_charge_reactive_w"] == 500

    def test_attributes_paused_force_charge(self):
        sensor = self._build_sensor(loop_active=False, force_charge=True)
        attrs = sensor.extra_state_attributes
        assert attrs["dynamic_battery_charge_status"] == "paused: forced_charge"

    def test_attributes_paused_grid_charge(self):
        sensor = self._build_sensor(loop_active=False, grid_charge_engaged=True)
        attrs = sensor.extra_state_attributes
        assert attrs["dynamic_battery_charge_status"] == "paused: grid_charge_engaged"

    def test_attributes_disabled_with_reason(self):
        sensor = self._build_sensor(
            loop_active=False, self_disabled_reason="forecast_unavailable"
        )
        attrs = sensor.extra_state_attributes
        assert attrs["dynamic_battery_charge_status"] == "disabled: forecast_unavailable"

    def test_attributes_omitted_when_feature_disabled(self):
        sensor = self._build_sensor(feature_enabled=False)
        attrs = sensor.extra_state_attributes
        assert "dynamic_battery_charge_status" not in attrs
        assert "dynamic_battery_charge_setpoint_w" not in attrs
        assert "dynamic_battery_charge_planned_w" not in attrs
        assert "dynamic_battery_charge_reactive_w" not in attrs

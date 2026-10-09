"""Tests for compute_battery_charge_setpoint (#17)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from custom_components.pv_excess_control.coordinator import (
    compute_battery_charge_setpoint,
)
from custom_components.pv_excess_control.models import (
    BatteryChargeCurve, BatteryChargeSetpoint,
)

UTC = timezone.utc


def _curve_with_planned_w(planned_w: int) -> BatteryChargeCurve:
    return BatteryChargeCurve(
        created_at=datetime(2026, 5, 5, 0, tzinfo=UTC),
        setpoints=(BatteryChargeSetpoint(
            datetime(2026, 5, 5, 0, tzinfo=UTC),
            datetime(2026, 5, 6, 0, tzinfo=UTC),
            planned_w,
        ),),
        expected_curtailment_kwh=0.0,
        headroom_kwh=0.0,
        fallback_reason=None,
    )


class TestSetpointPureFunction:
    NOW = datetime(2026, 5, 5, 12, tzinfo=UTC)
    DEFAULTS = dict(
        export_soft_ceiling_w=6000,
        battery_max_charge_power_w=5000,
        battery_trickle_charge_power_w=100,
    )

    def test_excess_below_ceiling_uses_planned(self):
        result = compute_battery_charge_setpoint(
            now=self.NOW, excess_w=4000.0,
            curve=_curve_with_planned_w(2000),
            **self.DEFAULTS,
        )
        assert result == 2000

    def test_excess_above_ceiling_lifts(self):
        result = compute_battery_charge_setpoint(
            now=self.NOW, excess_w=8000.0,  # 2000 over ceiling
            curve=_curve_with_planned_w(500),
            **self.DEFAULTS,
        )
        # max(500, 2000) = 2000
        assert result == 2000

    def test_setpoint_clamped_to_max(self):
        result = compute_battery_charge_setpoint(
            now=self.NOW, excess_w=15000.0,  # would lift to 9000W
            curve=_curve_with_planned_w(0),
            **self.DEFAULTS,
        )
        assert result == 5000  # clamped to max

    def test_setpoint_clamped_to_trickle(self):
        result = compute_battery_charge_setpoint(
            now=self.NOW, excess_w=0.0,
            curve=None,
            **self.DEFAULTS,
        )
        assert result == 100  # trickle floor

    def test_curve_none_uses_trickle_floor(self):
        result = compute_battery_charge_setpoint(
            now=self.NOW, excess_w=4000.0,
            curve=None,
            **self.DEFAULTS,
        )
        # excess_w (4000) < ceiling (6000), reactive=0, planned=trickle=100
        assert result == 100

    def test_excess_none_zero_reactive(self):
        result = compute_battery_charge_setpoint(
            now=self.NOW, excess_w=None,
            curve=_curve_with_planned_w(2000),
            **self.DEFAULTS,
        )
        assert result == 2000

    def test_now_outside_curve_falls_back_to_trickle(self):
        # Curve covers tomorrow only.
        curve = BatteryChargeCurve(
            created_at=self.NOW,
            setpoints=(BatteryChargeSetpoint(
                datetime(2026, 5, 6, 0, tzinfo=UTC),
                datetime(2026, 5, 7, 0, tzinfo=UTC),
                3000,
            ),),
            expected_curtailment_kwh=0.0,
            headroom_kwh=0.0,
            fallback_reason=None,
        )
        result = compute_battery_charge_setpoint(
            now=self.NOW, excess_w=4000.0,
            curve=curve,
            **self.DEFAULTS,
        )
        # No matching slot, planned defaults to trickle, reactive=0,
        # final = max(trickle, min(trickle, max)) = trickle = 100.
        assert result == 100


# ---------------------------------------------------------------------------
# Task 5 tests: _dyn_charge_should_run, _write_battery_max_charge,
# _dispatch_dynamic_battery_charge
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock, patch
from custom_components.pv_excess_control.const import (
    CONF_DYNAMIC_BATTERY_CHARGE_ENABLED,
    CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY,
    CONF_BATTERY_MAX_CHARGE_POWER_W,
    CONF_BATTERY_TRICKLE_CHARGE_POWER_W,
    CONF_EXPORT_LIMIT,
)


def _coordinator_with_dyn_charge_enabled(**overrides):
    """Build a coordinator stub with dyn-charge enabled and supporting state."""
    coord = MagicMock()
    coord.async_save_daily_state = AsyncMock()
    coord.async_prepare_battery_unload = AsyncMock(return_value=True)
    coord.config_entry = MagicMock()
    coord.config_entry.entry_id = "test_entry_id"
    coord.config_entry.data = {
        CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
        CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: "number.batt_max_charge",
        CONF_BATTERY_MAX_CHARGE_POWER_W: 5000,
        CONF_BATTERY_TRICKLE_CHARGE_POWER_W: 100,
        CONF_EXPORT_LIMIT: 6000,
    }
    coord.config_entry.data.update(overrides)
    coord.force_charge = False
    coord._grid_charge_engaged = False
    coord._battery_hold_engaged = False
    # Startup grace fully elapsed by default — set _startup_time far in the past.
    coord._startup_time = datetime(2020, 1, 1)
    coord._dyn_charge_self_disabled_reason = None
    coord._dyn_charge_loop_active = False
    coord._dyn_charge_release_pending = False
    coord._dyn_charge_pause_released = False
    coord._dyn_charge_last_written_w = None
    coord._dyn_charge_last_write_time = None
    coord._dyn_charge_last_warn_time = None
    coord._dyn_charge_planned_w = 0
    coord._dyn_charge_reactive_w = 0
    coord.hass = MagicMock()
    coord.hass.services.async_call = AsyncMock()
    coord.hass.states.get = MagicMock(
        return_value=MagicMock(state="500", attributes={"min": 0, "max": 5000})
    )
    return coord


class TestShouldRunPredicate:
    def test_returns_false_when_feature_disabled(self):
        from custom_components.pv_excess_control.coordinator import _dyn_charge_should_run
        coord = _coordinator_with_dyn_charge_enabled()
        coord.config_entry.data[CONF_DYNAMIC_BATTERY_CHARGE_ENABLED] = False
        assert _dyn_charge_should_run(coord) is False

    def test_returns_false_during_force_charge(self):
        from custom_components.pv_excess_control.coordinator import _dyn_charge_should_run
        coord = _coordinator_with_dyn_charge_enabled()
        coord.force_charge = True
        assert _dyn_charge_should_run(coord) is False

    def test_returns_false_during_grid_charge_engaged(self):
        from custom_components.pv_excess_control.coordinator import _dyn_charge_should_run
        coord = _coordinator_with_dyn_charge_enabled()
        coord._grid_charge_engaged = True
        assert _dyn_charge_should_run(coord) is False

    def test_returns_false_during_startup_grace(self):
        from custom_components.pv_excess_control.coordinator import _dyn_charge_should_run
        coord = _coordinator_with_dyn_charge_enabled()
        # Startup time is just now -> grace period (120s) is fully active.
        coord._startup_time = datetime.now()
        assert _dyn_charge_should_run(coord) is False

    def test_returns_false_when_self_disabled(self):
        from custom_components.pv_excess_control.coordinator import _dyn_charge_should_run
        coord = _coordinator_with_dyn_charge_enabled()
        coord._dyn_charge_self_disabled_reason = "forecast_unavailable"
        assert _dyn_charge_should_run(coord) is False

    def test_returns_true_when_all_clear(self):
        from custom_components.pv_excess_control.coordinator import _dyn_charge_should_run
        coord = _coordinator_with_dyn_charge_enabled()
        assert _dyn_charge_should_run(coord) is True


class TestWriteBatteryMaxCharge:
    @pytest.mark.asyncio
    async def test_writes_to_configured_entity(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        await PvExcessCoordinator._write_battery_max_charge.__get__(coord)(2000)
        coord.hass.services.async_call.assert_awaited_once()
        args = coord.hass.services.async_call.call_args
        assert args.args[:2] == ("number", "set_value")
        assert args.args[2]["entity_id"] == "number.batt_max_charge"
        assert args.args[2]["value"] == 2000.0

    @pytest.mark.asyncio
    async def test_dedupe_skips_within_deadband(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        from datetime import datetime, timezone
        coord = _coordinator_with_dyn_charge_enabled()
        coord._dyn_charge_last_written_w = 2000
        coord._dyn_charge_last_write_time = datetime.now(timezone.utc)
        await PvExcessCoordinator._write_battery_max_charge.__get__(coord)(2030)
        coord.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dedupe_writes_when_idle_timeout_elapsed(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        from datetime import datetime, timezone, timedelta
        coord = _coordinator_with_dyn_charge_enabled()
        coord._dyn_charge_last_written_w = 2000
        coord._dyn_charge_last_write_time = datetime.now(timezone.utc) - timedelta(seconds=400)
        await PvExcessCoordinator._write_battery_max_charge.__get__(coord)(2030)
        coord.hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skip_write_when_entity_unavailable(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord.hass.states.get = MagicMock(
            return_value=MagicMock(state="unavailable", attributes={})
        )
        await PvExcessCoordinator._write_battery_max_charge.__get__(coord)(2000)
        coord.hass.services.async_call.assert_not_awaited()
        assert coord._dyn_charge_last_written_w is None

    @pytest.mark.asyncio
    async def test_skip_write_when_entity_missing(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord.hass.states.get = MagicMock(return_value=None)
        await PvExcessCoordinator._write_battery_max_charge.__get__(coord)(2000)
        coord.hass.services.async_call.assert_not_awaited()


class TestDispatcherStateMachine:
    @pytest.mark.asyncio
    async def test_pause_on_force_charge_writes_max_once(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord._dyn_charge_loop_active = True  # was running last cycle
        coord.force_charge = True              # rising edge
        coord._write_battery_max_charge = AsyncMock(return_value=True)
        power_state = MagicMock()
        power_state.excess_power = 4000.0
        await PvExcessCoordinator._dispatch_dynamic_battery_charge.__get__(coord)(power_state)
        coord._write_battery_max_charge.assert_awaited_once_with(5000)
        assert coord._dyn_charge_loop_active is False

    @pytest.mark.asyncio
    async def test_pause_on_grid_charge_engaged_writes_max_once(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord._dyn_charge_loop_active = True       # was running last cycle
        coord._grid_charge_engaged = True          # rising edge via grid-charge
        coord._write_battery_max_charge = AsyncMock(return_value=True)
        power_state = MagicMock()
        power_state.excess_power = 4000.0
        await PvExcessCoordinator._dispatch_dynamic_battery_charge.__get__(coord)(power_state)
        coord._write_battery_max_charge.assert_awaited_once_with(5000)
        assert coord._dyn_charge_loop_active is False

    @pytest.mark.asyncio
    async def test_paused_steady_state_no_writes(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord._dyn_charge_loop_active = False  # already paused
        coord.force_charge = True
        coord._write_battery_max_charge = AsyncMock(return_value=True)
        power_state = MagicMock()
        power_state.excess_power = 4000.0
        await PvExcessCoordinator._dispatch_dynamic_battery_charge.__get__(coord)(power_state)
        coord._write_battery_max_charge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_marks_loop_active_and_writes(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord._dyn_charge_loop_active = False  # was paused
        coord._write_battery_max_charge = AsyncMock(return_value=True)
        coord.current_plan = None
        power_state = MagicMock()
        power_state.excess_power = 4000.0
        await PvExcessCoordinator._dispatch_dynamic_battery_charge.__get__(coord)(power_state)
        assert coord._dyn_charge_loop_active is True
        coord._write_battery_max_charge.assert_awaited_once()


class TestUnloadWritesMax:
    @pytest.mark.asyncio
    async def test_unload_writes_max(self):
        from custom_components.pv_excess_control import async_unload_entry
        from custom_components.pv_excess_control.const import DOMAIN
        coord = _coordinator_with_dyn_charge_enabled()
        coord._write_battery_max_charge = AsyncMock(return_value=True)
        coord.config_entry.entry_id = "test_entry"
        hass = MagicMock()
        hass.data = {
            DOMAIN: {
                "test_entry": coord,
                "test_entry_config_snapshot": {},
                "test_entry_subentry_count": 0,
            },
        }
        hass.config_entries = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        await async_unload_entry(hass, coord.config_entry)
        coord._write_battery_max_charge.assert_awaited_with(5000)

    @pytest.mark.asyncio
    async def test_unload_skips_write_when_feature_disabled(self):
        from custom_components.pv_excess_control import async_unload_entry
        from custom_components.pv_excess_control.const import (
            DOMAIN, CONF_DYNAMIC_BATTERY_CHARGE_ENABLED,
        )
        coord = _coordinator_with_dyn_charge_enabled()
        coord.config_entry.data[CONF_DYNAMIC_BATTERY_CHARGE_ENABLED] = False
        coord._write_battery_max_charge = AsyncMock(return_value=True)
        coord.config_entry.entry_id = "test_entry"
        hass = MagicMock()
        hass.data = {
            DOMAIN: {
                "test_entry": coord,
                "test_entry_config_snapshot": {},
                "test_entry_subentry_count": 0,
            },
        }
        hass.config_entries = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        await async_unload_entry(hass, coord.config_entry)
        coord._write_battery_max_charge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unload_skips_write_when_max_w_zero(self):
        from custom_components.pv_excess_control import async_unload_entry
        from custom_components.pv_excess_control.const import (
            DOMAIN, CONF_BATTERY_MAX_CHARGE_POWER_W,
        )
        coord = _coordinator_with_dyn_charge_enabled()
        coord.config_entry.data[CONF_BATTERY_MAX_CHARGE_POWER_W] = 0
        coord._write_battery_max_charge = AsyncMock(return_value=True)
        coord.config_entry.entry_id = "test_entry"
        hass = MagicMock()
        hass.data = {
            DOMAIN: {
                "test_entry": coord,
                "test_entry_config_snapshot": {},
                "test_entry_subentry_count": 0,
            },
        }
        hass.config_entries = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        await async_unload_entry(hass, coord.config_entry)
        coord._write_battery_max_charge.assert_not_awaited()


# ---------------------------------------------------------------------------
# Task 8: coordinator startup validator tests
# ---------------------------------------------------------------------------

class TestStartupValidator:
    def test_validator_no_op_when_feature_disabled(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord.config_entry.data = {CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: False}
        coord._dyn_charge_self_disabled_reason = None
        coord._get_battery_config = MagicMock(return_value=MagicMock(capacity_kwh=10.0))
        PvExcessCoordinator._validate_dynamic_battery_charge_config(coord)
        assert coord._dyn_charge_self_disabled_reason is None

    def test_validator_sets_invalid_battery_capacity_when_no_battery_config(self):
        # All earlier checks (entity_id, max_w, export_limit) pass; only capacity is missing.
        # Verifies the validator's capacity check is reachable as the LAST gate.
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord._dyn_charge_self_disabled_reason = None
        coord._get_battery_config = MagicMock(return_value=None)
        coord.hass.states.get = MagicMock(return_value=MagicMock(state="500"))
        PvExcessCoordinator._validate_dynamic_battery_charge_config(coord)
        assert coord._dyn_charge_self_disabled_reason == "invalid_battery_capacity"

    def test_validator_sets_entity_not_configured(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord.config_entry.data = {
            CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
            CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: None,
        }
        coord._dyn_charge_self_disabled_reason = None
        coord._get_battery_config = MagicMock(return_value=MagicMock(capacity_kwh=10.0))
        PvExcessCoordinator._validate_dynamic_battery_charge_config(coord)
        assert coord._dyn_charge_self_disabled_reason == "entity_not_configured"

    def test_validator_allows_late_entity_availability(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord.config_entry.data = {
            CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
            CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: "number.missing",
            CONF_BATTERY_MAX_CHARGE_POWER_W: 5000,
            CONF_EXPORT_LIMIT: 6000,
        }
        coord._dyn_charge_self_disabled_reason = None
        coord._get_battery_config = MagicMock(return_value=MagicMock(capacity_kwh=10.0))
        coord.hass.states.get = MagicMock(return_value=None)
        PvExcessCoordinator._validate_dynamic_battery_charge_config(coord)
        assert coord._dyn_charge_self_disabled_reason is None

    def test_validator_sets_entity_wrong_domain(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord.config_entry.data = {
            CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
            CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: "sensor.batt",
            CONF_BATTERY_MAX_CHARGE_POWER_W: 5000,
            CONF_EXPORT_LIMIT: 6000,
        }
        coord._dyn_charge_self_disabled_reason = None
        coord._get_battery_config = MagicMock(return_value=MagicMock(capacity_kwh=10.0))
        coord.hass.states.get = MagicMock(return_value=MagicMock(state="500"))
        PvExcessCoordinator._validate_dynamic_battery_charge_config(coord)
        assert coord._dyn_charge_self_disabled_reason == "entity_wrong_domain"

    def test_validator_sets_invalid_max_power(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord.config_entry.data = {
            CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
            CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: "number.batt",
            CONF_BATTERY_MAX_CHARGE_POWER_W: 0,  # invalid
            CONF_EXPORT_LIMIT: 6000,
        }
        coord._dyn_charge_self_disabled_reason = None
        coord._get_battery_config = MagicMock(return_value=MagicMock(capacity_kwh=10.0))
        coord.hass.states.get = MagicMock(return_value=MagicMock(state="500"))
        PvExcessCoordinator._validate_dynamic_battery_charge_config(coord)
        assert coord._dyn_charge_self_disabled_reason == "invalid_max_power"

    def test_validator_sets_invalid_export_limit(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord.config_entry.data = {
            CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
            CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: "number.batt",
            CONF_BATTERY_MAX_CHARGE_POWER_W: 5000,
            CONF_EXPORT_LIMIT: 0,  # invalid
        }
        coord._dyn_charge_self_disabled_reason = None
        coord._get_battery_config = MagicMock(return_value=MagicMock(capacity_kwh=10.0))
        coord.hass.states.get = MagicMock(return_value=MagicMock(state="500"))
        PvExcessCoordinator._validate_dynamic_battery_charge_config(coord)
        assert coord._dyn_charge_self_disabled_reason == "invalid_export_limit"

    def test_validator_passes_when_all_valid(self):
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
        coord = _coordinator_with_dyn_charge_enabled()
        coord.config_entry.data = {
            CONF_DYNAMIC_BATTERY_CHARGE_ENABLED: True,
            CONF_INVERTER_BATTERY_MAX_CHARGE_POWER_ENTITY: "number.batt_max_charge",
            CONF_BATTERY_MAX_CHARGE_POWER_W: 5000,
            CONF_EXPORT_LIMIT: 6000,
        }
        coord._dyn_charge_self_disabled_reason = None
        coord._get_battery_config = MagicMock(return_value=MagicMock(capacity_kwh=10.0))
        coord.hass.states.get = MagicMock(return_value=MagicMock(state="500"))
        PvExcessCoordinator._validate_dynamic_battery_charge_config(coord)
        assert coord._dyn_charge_self_disabled_reason is None

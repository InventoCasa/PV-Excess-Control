"""Tests for PV Excess Control integration setup and coordinator.

Uses standard unittest.mock since pytest-homeassistant-custom-component
is not available. These tests verify the production logic is correct
and can be adapted to use HA test fixtures later.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.pv_excess_control.const import (
    CONF_BATTERY_CHARGE_POWER,
    CONF_BATTERY_DISCHARGE_POWER,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_BATTERY_STRATEGY,
    CONF_CONTROLLER_INTERVAL,
    CONF_GRID_EXPORT,
    CONF_GRID_VOLTAGE,
    CONF_IMPORT_EXPORT,
    CONF_LOAD_POWER,
    CONF_PLAN_INFLUENCE,
    CONF_PLANNER_INTERVAL,
    CONF_PV_POWER,
    CONF_TARIFF_PROVIDER,
    CONF_FORECAST_PROVIDER,
    DEFAULT_CONTROLLER_INTERVAL,
    DEFAULT_GRID_VOLTAGE,
    DEFAULT_PLANNER_INTERVAL,
    DEFAULT_STARTUP_GRACE_PERIOD,
    DOMAIN,
    BatteryStrategy,
    PlanInfluence,
    TariffProvider as TariffProviderEnum,
    ForecastProvider as ForecastProviderEnum,
)
from custom_components.pv_excess_control.models import (
    BatteryDischargeAction,
    ControlDecision,
    PowerState,
    Action,
    TariffInfo,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class MockState:
    """Minimal mock for an HA state object."""

    def __init__(self, state: str, attributes: dict | None = None) -> None:
        self.state = state
        self.attributes = attributes or {}


class MockStates:
    """Minimal mock for hass.states."""

    def __init__(self, states: dict[str, MockState] | None = None) -> None:
        self._states = states or {}

    def get(self, entity_id: str) -> MockState | None:
        return self._states.get(entity_id)


class MockServiceRegistry:
    """Minimal mock for hass.services."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(
        self, domain: str, service: str, service_data: dict | None = None, **kwargs
    ) -> None:
        self.calls.append((domain, service, service_data or {}))


class MockConfigEntries:
    """Minimal mock for hass.config_entries."""

    async def async_forward_entry_setups(self, entry: Any, platforms: list) -> None:
        pass

    async def async_unload_platforms(self, entry: Any, platforms: list) -> bool:
        return True


class MockHttp:
    """Minimal mock for hass.http (aiohttp server)."""

    def __init__(self) -> None:
        self._registered: list[tuple[str, str]] = []

    def register_static_path(self, url_path: str, path: str, cache_headers: bool = True) -> None:
        self._registered.append((url_path, path))


class MockHassConfig:
    """Minimal mock for hass.config."""

    time_zone: str = "UTC"


class MockHass:
    """Minimal mock for HomeAssistant instance."""

    def __init__(self, states: dict[str, MockState] | None = None) -> None:
        self.states = MockStates(states)
        self.services = MockServiceRegistry()
        self.config_entries = MockConfigEntries()
        self.data: dict = {}
        self.http = MockHttp()
        self.config = MockHassConfig()
        # bus needed by DataUpdateCoordinator
        self.bus = MagicMock()
        self.bus.async_listen_once = MagicMock(return_value=MagicMock())
        # async_add_job needed for scheduling
        self.async_add_job = MagicMock()
        self.loop = asyncio.get_event_loop()
        # async_create_task needed by DataUpdateCoordinator
        self.async_create_task = MagicMock(side_effect=lambda coro, **kw: asyncio.ensure_future(coro))


def _make_config_entry(
    data: dict | None = None,
    entry_id: str = "test_entry_123",
    subentries: dict | None = None,
) -> MagicMock:
    """Create a mock ConfigEntry."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = data or {
        CONF_PV_POWER: "sensor.pv_power",
        CONF_GRID_EXPORT: "sensor.grid_export",
        CONF_LOAD_POWER: "sensor.load_power",
        CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
        CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
        CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
        CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
        CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
    }
    entry.subentries = subentries or {}
    return entry


def _make_coordinator(
    hass: MockHass | None = None,
    entry: MagicMock | None = None,
    states: dict[str, MockState] | None = None,
):
    """Create a PvExcessCoordinator with mocked HA."""
    from custom_components.pv_excess_control.coordinator import PvExcessCoordinator

    if hass is None:
        hass = MockHass(states)
    if entry is None:
        entry = _make_config_entry()

    # Patch out DataUpdateCoordinator's HA-specific init internals
    with patch.object(
        PvExcessCoordinator,
        "__init__",
        lambda self, h, e: None,
    ):
        coord = PvExcessCoordinator.__new__(PvExcessCoordinator)

    # Manually initialize coordinator attributes
    coord.hass = hass
    coord.config_entry = entry
    coord.logger = MagicMock()
    coord.name = DOMAIN
    coord.update_interval = timedelta(
        seconds=entry.data.get(CONF_CONTROLLER_INTERVAL, DEFAULT_CONTROLLER_INTERVAL)
    )

    grid_voltage = entry.data.get(CONF_GRID_VOLTAGE, DEFAULT_GRID_VOLTAGE)

    from custom_components.pv_excess_control.optimizer import Optimizer
    from custom_components.pv_excess_control.planner import Planner
    from custom_components.pv_excess_control.energy import create_tariff_provider

    coord.optimizer = Optimizer(grid_voltage=grid_voltage)
    coord.planner = Planner(grid_voltage=grid_voltage)
    coord.power_history = []
    coord._last_sensor_available = {}
    coord._last_appliance_configs = []
    coord.current_plan = None
    coord.appliance_states = {}
    coord.control_decisions = []
    coord.battery_discharge_action = None
    coord._planner_interval = entry.data.get(
        CONF_PLANNER_INTERVAL, DEFAULT_PLANNER_INTERVAL
    )
    coord._planner_counter = 0
    coord._startup_time = datetime.now()
    coord._enabled = True
    coord._forecast_provider = None
    coord._last_tariff_info = None

    # Runtime-writable control state (entity-driven)
    coord.force_charge = False
    coord.appliance_enabled = {}
    coord.appliance_overrides = {}
    coord.appliance_priorities = {}
    coord.appliance_min_daily_runtime = {}
    coord.appliance_max_daily_runtime = {}
    coord.battery_strategy = entry.data.get(
        CONF_BATTERY_STRATEGY, BatteryStrategy.BALANCED
    )

    coord._plan_influence = entry.data.get(
        CONF_PLAN_INFLUENCE, PlanInfluence.LIGHT
    )
    coord._last_discharge_limit = None
    coord._last_state_change = {}
    coord._last_applied_current = {}
    coord._activations_today = {}
    coord._needed_by_others = set()
    coord._previous_is_on = {}
    coord._was_enabled = True

    # Inverter forced grid-charge state machine fields
    coord._inverter_ctl = None
    coord._grid_charge_engaged = entry.data.get("_grid_charge_engaged", False)
    coord._grid_charge_engage_ts = None
    coord._force_charge_prev = False
    coord._latest_tariff = None
    coord._latest_power_state = None

    tariff_type = entry.data.get(CONF_TARIFF_PROVIDER, TariffProviderEnum.NONE)
    coord._tariff_provider = create_tariff_provider(tariff_type, "")

    # Analytics and notifications
    from custom_components.pv_excess_control.analytics import AnalyticsTracker
    from custom_components.pv_excess_control.notifications import NotificationManager

    coord.analytics = AnalyticsTracker(feed_in_tariff=0.0, normal_import_price=0.25)
    coord.notifications = NotificationManager(
        hass, notification_settings=None, notification_service=None,
    )

    return coord


# ---------------------------------------------------------------------------
# Tests: coordinator power state collection
# ---------------------------------------------------------------------------


class TestCollectPowerState:
    """Tests for _collect_power_state."""

    def test_basic_power_state_collection(self):
        """Coordinator reads PV, grid export, and load from sensor entities."""
        states = {
            "sensor.pv_power": MockState("3500"),
            "sensor.grid_export": MockState("1200"),
            "sensor.load_power": MockState("2300"),
        }
        coord = _make_coordinator(states=states)

        ps = coord._collect_power_state()

        assert isinstance(ps, PowerState)
        assert ps.pv_production == 3500.0
        assert ps.grid_export == 1200.0
        assert ps.load_power == 2300.0
        # excess = PV - load
        assert ps.excess_power == 1200.0
        assert ps.battery_soc is None
        assert ps.battery_power is None
        assert ps.timestamp is not None

    def test_unavailable_sensors_return_none(self):
        """Unavailable sensors propagate as None (not 0.0) in PowerState fields."""
        states = {
            "sensor.pv_power": MockState("unavailable"),
            "sensor.grid_export": MockState("unknown"),
            "sensor.load_power": MockState(""),
        }
        coord = _make_coordinator(states=states)

        ps = coord._collect_power_state()

        assert ps.pv_production is None
        assert ps.grid_export is None
        assert ps.load_power is None

    def test_missing_sensors_return_none(self):
        """Sensors that don't exist in HA return None in PowerState fields."""
        coord = _make_coordinator(states={})

        ps = coord._collect_power_state()

        assert ps.pv_production is None
        assert ps.grid_export is None
        assert ps.load_power is None

    def test_combined_import_export_sensor_positive(self):
        """Combined import/export sensor: positive = export."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_IMPORT_EXPORT: "sensor.grid_power",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4000"),
            "sensor.grid_power": MockState("1500"),  # exporting
            "sensor.load_power": MockState("2500"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.grid_export == 1500.0
        assert ps.grid_import == 0.0

    def test_combined_import_export_sensor_negative(self):
        """Combined import/export sensor: negative = import."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_IMPORT_EXPORT: "sensor.grid_power",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("500"),
            "sensor.grid_power": MockState("-800"),  # importing
            "sensor.load_power": MockState("1300"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.grid_export == 0.0
        assert ps.grid_import == 800.0

    def test_battery_sensors_read(self):
        """Battery SOC and power sensors are read when configured."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_SOC: "sensor.battery_soc",
            "battery_power": "sensor.battery_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("5000"),
            "sensor.grid_export": MockState("2000"),
            "sensor.load_power": MockState("3000"),
            "sensor.battery_soc": MockState("75"),
            "sensor.battery_power": MockState("500"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.battery_soc == 75.0
        assert ps.battery_power == 500.0

    def test_separate_battery_charge_discharge_sensors(self):
        """Separate charge/discharge sensors are combined into battery_power."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_SOC: "sensor.battery_soc",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("5000"),
            "sensor.grid_export": MockState("2000"),
            "sensor.load_power": MockState("3000"),
            "sensor.battery_soc": MockState("60"),
            "sensor.battery_charge": MockState("1500"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        # charge(1500) - discharge(0) = 1500 (net charging)
        assert ps.battery_power == 1500.0
        assert ps.battery_soc == 60.0

    def test_separate_battery_sensors_discharging(self):
        """Separate sensors when battery is discharging."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_SOC: "sensor.battery_soc",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("500"),
            "sensor.grid_export": MockState("0"),
            "sensor.load_power": MockState("2000"),
            "sensor.battery_soc": MockState("40"),
            "sensor.battery_charge": MockState("0"),
            "sensor.battery_discharge": MockState("1500"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        # charge(0) - discharge(1500) = -1500 (net discharging)
        assert ps.battery_power == -1500.0

    def test_combined_battery_sensor_takes_precedence(self):
        """When combined battery_power is set, it takes precedence over separate sensors."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_SOC: "sensor.battery_soc",
            CONF_BATTERY_POWER: "sensor.battery_combined",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("5000"),
            "sensor.grid_export": MockState("2000"),
            "sensor.load_power": MockState("3000"),
            "sensor.battery_soc": MockState("70"),
            "sensor.battery_combined": MockState("800"),  # combined wins
            "sensor.battery_charge": MockState("1500"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        # Combined sensor takes precedence
        assert ps.battery_power == 800.0

    def test_excess_calculated_from_grid_when_no_load(self):
        """When load_power is 0 or unavailable, excess = grid_export - grid_import."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("3000"),
            "sensor.grid_export": MockState("1500"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        # No load sensor configured: excess = grid_export - grid_import
        assert ps.excess_power == 1500.0

    def test_hybrid_export_tiny_positive_uses_pv_minus_load(self):
        """Reproduces prod oscillation: with a battery configured, a tiny
        positive grid_export reading must NOT cause excess to collapse to
        the export value. Excess should reflect pv - load (the surplus
        currently being absorbed by the battery)."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4216"),
            "sensor.grid_export": MockState("5"),  # <-- the bug trigger
            "sensor.load_power": MockState("778"),
            "sensor.battery_charge": MockState("3442"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        # Hybrid topology: should use pv - load = 4216 - 778 = 3438
        # Currently buggy: returns 5 because grid_export > 0
        assert ps.excess_power == 3438.0

    def test_hybrid_export_zero_uses_pv_minus_load(self):
        """When grid_export is exactly 0 and a battery is configured,
        excess must equal pv - load. (Today's code happens to return
        the same value via the existing fallback; this test guards
        against a future regression of the new branch.)"""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4200"),
            "sensor.grid_export": MockState("0"),
            "sensor.load_power": MockState("800"),
            "sensor.battery_charge": MockState("3400"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.excess_power == 3400.0

    def test_hybrid_export_large_positive_still_uses_pv_minus_load(self):
        """Battery near full: even with a large positive grid_export,
        the new branch must use pv - load. This proves the topology
        check (has_battery) wins over the magnitude of grid_export."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("5000"),
            "sensor.grid_export": MockState("4000"),  # battery near full
            "sensor.load_power": MockState("800"),
            "sensor.battery_charge": MockState("200"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        # pv - load = 5000 - 800 = 4200, NOT grid_export (4000)
        assert ps.excess_power == 4200.0

    def test_hybrid_battery_discharging_pv_minus_load_goes_negative(self):
        """Evening sanity check: when the battery is discharging and load
        exceeds PV, excess must go negative so the optimizer sheds."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("200"),
            "sensor.grid_export": MockState("0"),
            "sensor.load_power": MockState("1000"),
            "sensor.battery_charge": MockState("0"),
            "sensor.battery_discharge": MockState("800"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        # pv - load = 200 - 1000 = -800
        assert ps.excess_power == -800.0

    def test_hybrid_uses_combined_battery_power_sensor(self):
        """has_battery must also detect the combined CONF_BATTERY_POWER
        configuration, not just separate charge/discharge sensors."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_POWER: "sensor.battery_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4500"),
            "sensor.grid_export": MockState("8"),  # tiny positive
            "sensor.load_power": MockState("900"),
            "sensor.battery_power": MockState("3500"),  # positive = charging
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        # pv - load = 4500 - 900 = 3600, NOT grid_export (8)
        assert ps.excess_power == 3600.0

    def test_no_battery_grid_export_branch_unchanged(self):
        """Regression guard: pure-inverter setups (no battery sensor)
        must still use grid_export when it's positive. This is the
        byte-for-byte unchanged path."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4000"),
            "sensor.grid_export": MockState("1200"),
            "sensor.load_power": MockState("2600"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        # No battery → grid_export branch wins → 1200.
        # pv − load = 4000 − 2600 = 1400, so a regression that collapsed
        # this path into pv − load would fail with 1400 != 1200.
        assert ps.excess_power == 1200.0

    def test_combined_import_export_unchanged_with_battery(self):
        """Regression guard: combined import/export sensor topology
        must take precedence over has_battery and use the
        grid_export - grid_import formula unchanged."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_IMPORT_EXPORT: "sensor.grid_power",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4000"),
            "sensor.grid_power": MockState("1500"),  # combined: positive = export
            "sensor.load_power": MockState("2500"),
            "sensor.battery_charge": MockState("0"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        # Combined branch wins: excess = grid_export - grid_import = 1500 - 0
        assert ps.excess_power == 1500.0

    def test_pv_unavailable_hybrid_branch_returns_none_excess(self):
        """Hybrid config, PV sensor unavailable → excess and pv_production both None."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("unavailable"),
            "sensor.grid_export": MockState("0"),
            "sensor.load_power": MockState("800"),
            "sensor.battery_charge": MockState("0"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.pv_production is None
        assert ps.excess_power is None

    def test_load_unavailable_hybrid_with_grid_export_zero_returns_none(self):
        """Hybrid config, load unavailable, grid_export=0 → fallback needs load → None."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4000"),
            "sensor.grid_export": MockState("0"),
            "sensor.load_power": MockState("unavailable"),
            "sensor.battery_charge": MockState("0"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.load_power is None
        assert ps.excess_power is None

    def test_pv_unavailable_non_hybrid_grid_export_branch_unaffected(self):
        """Pure inverter (no battery), PV unavailable, grid_export positive.

        The grid_export branch does not need PV, so excess_power equals
        grid_export. pv_production is still None in PowerState.
        """
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("unavailable"),
            "sensor.grid_export": MockState("1200"),
            "sensor.load_power": MockState("800"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.pv_production is None
        assert ps.excess_power == 1200.0

    def test_pv_unavailable_non_hybrid_grid_export_fallback_returns_none(self):
        """Pure inverter, PV unavailable, grid_export == 0 → fallback needs PV → None."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("unavailable"),
            "sensor.grid_export": MockState("0"),
            "sensor.load_power": MockState("800"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.pv_production is None
        assert ps.excess_power is None

    def test_grid_export_unavailable_grid_export_branch_returns_none(self):
        """Pure inverter, grid_export unavailable → don't fall through, excess=None.

        Nuance 1a from the spec: user configured grid_export as their truth
        source; silently switching to pv-load would mask the misconfiguration.
        """
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4000"),
            "sensor.grid_export": MockState("unavailable"),
            "sensor.load_power": MockState("800"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.grid_export is None
        assert ps.excess_power is None

    def test_combined_import_export_unavailable_returns_none(self):
        """Combined sensor topology, combined sensor unavailable → excess=None."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_IMPORT_EXPORT: "sensor.grid_power",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4000"),
            "sensor.grid_power": MockState("unavailable"),
            "sensor.load_power": MockState("800"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.grid_export is None
        assert ps.grid_import is None
        assert ps.excess_power is None

    def test_load_unavailable_hybrid_falls_through_to_grid_export(self):
        """Hybrid config, PV=4000, load unavailable, grid_export=1500.

        Hybrid predicate is False (load_power is None), so it falls through
        to the grid_export_entity branch. grid_export=1500>0 → excess=1500.
        """
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4000"),
            "sensor.grid_export": MockState("1500"),
            "sensor.load_power": MockState("unavailable"),
            "sensor.battery_charge": MockState("0"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.load_power is None
        assert ps.excess_power == 1500.0

    def test_all_sensors_good_hybrid_still_works(self):
        """Regression guard: hybrid happy path still produces pv-load."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("4500"),
            "sensor.grid_export": MockState("0"),
            "sensor.load_power": MockState("900"),
            "sensor.battery_charge": MockState("3600"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.pv_production == 4500.0
        assert ps.load_power == 900.0
        assert ps.excess_power == 3600.0  # pv - load

    def test_upstream_sensor_fields_are_none_when_unavailable(self):
        """PowerState upstream fields are None (not 0.0) when sensors are unavailable."""
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        states = {
            "sensor.pv_power": MockState("unavailable"),
            "sensor.grid_export": MockState("unavailable"),
            "sensor.load_power": MockState("unavailable"),
        }
        hass = MockHass(states)
        coord = _make_coordinator(hass=hass, entry=entry)

        ps = coord._collect_power_state()

        assert ps.pv_production is None
        assert ps.grid_export is None
        assert ps.load_power is None

    def test_sensor_unavailable_logs_warning_once(self, caplog):
        """First cycle after transition: one WARNING. Subsequent cycles: no additional log."""
        import logging
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        good_states = {
            "sensor.pv_power": MockState("4000"),
            "sensor.grid_export": MockState("0"),
            "sensor.load_power": MockState("800"),
            "sensor.battery_charge": MockState("3200"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(good_states)
        coord = _make_coordinator(hass=hass, entry=entry)

        # Cycle 1: all sensors good — primes the _last_sensor_available dict
        coord._collect_power_state()
        caplog.clear()

        # Cycle 2: PV transitions to unavailable.
        hass.states._states["sensor.pv_power"] = MockState("unavailable")

        with caplog.at_level(logging.WARNING, logger="custom_components.pv_excess_control"):
            coord._collect_power_state()

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "sensor.pv_power" in r.getMessage()
            and "unavailable" in r.getMessage().lower()
        ]
        assert len(warnings) == 1, (
            f"Expected exactly 1 WARNING, got {len(warnings)}: "
            f"{[r.getMessage() for r in warnings]}"
        )

        # Cycle 3: still unavailable — no additional warning
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="custom_components.pv_excess_control"):
            coord._collect_power_state()

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 0, (
            f"Expected no additional WARNING, got {len(warnings)}"
        )

    def test_sensor_recovery_logs_info(self, caplog):
        """Transition unavailable → available: one INFO log line."""
        import logging
        entry = _make_config_entry(data={
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_BATTERY_CHARGE_POWER: "sensor.battery_charge",
            CONF_BATTERY_DISCHARGE_POWER: "sensor.battery_discharge",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        })
        bad_states = {
            "sensor.pv_power": MockState("unavailable"),
            "sensor.grid_export": MockState("0"),
            "sensor.load_power": MockState("800"),
            "sensor.battery_charge": MockState("0"),
            "sensor.battery_discharge": MockState("0"),
        }
        hass = MockHass(bad_states)
        coord = _make_coordinator(hass=hass, entry=entry)

        # Cycle 1: PV unavailable — primes the dict
        coord._collect_power_state()
        caplog.clear()

        # Cycle 2: PV recovers
        hass.states._states["sensor.pv_power"] = MockState("4000")

        with caplog.at_level(logging.INFO, logger="custom_components.pv_excess_control"):
            coord._collect_power_state()

        infos = [
            r for r in caplog.records
            if r.levelno == logging.INFO
            and "sensor.pv_power" in r.getMessage()
            and "available" in r.getMessage().lower()
        ]
        assert len(infos) == 1, (
            f"Expected exactly 1 INFO, got {len(infos)}: "
            f"{[r.getMessage() for r in infos]}"
        )


# ---------------------------------------------------------------------------
# Tests: power history buffer
# ---------------------------------------------------------------------------


class TestPowerHistory:
    """Tests for power history management."""

    def test_history_appends(self):
        """Power states are appended to history."""
        coord = _make_coordinator(states={
            "sensor.pv_power": MockState("1000"),
            "sensor.grid_export": MockState("500"),
            "sensor.load_power": MockState("500"),
        })

        ps = coord._collect_power_state()
        coord.power_history.append(ps)

        assert len(coord.power_history) == 1
        assert coord.power_history[0].pv_production == 1000.0

    def test_history_max_size_enforced(self):
        """History is capped at MAX_HISTORY_SIZE entries."""
        from custom_components.pv_excess_control.coordinator import MAX_HISTORY_SIZE

        coord = _make_coordinator(states={
            "sensor.pv_power": MockState("1000"),
            "sensor.grid_export": MockState("500"),
            "sensor.load_power": MockState("500"),
        })

        # Fill history beyond max
        for i in range(MAX_HISTORY_SIZE + 20):
            ps = PowerState(
                pv_production=float(i),
                grid_export=0.0,
                grid_import=0.0,
                load_power=0.0,
                excess_power=float(i),
                battery_soc=None,
                battery_power=None,
                ev_soc=None,
                timestamp=datetime.now(),
            )
            coord.power_history.append(ps)
            if len(coord.power_history) > MAX_HISTORY_SIZE:
                coord.power_history.pop(0)

        assert len(coord.power_history) == MAX_HISTORY_SIZE
        # Oldest entry should have been popped (first entry should be 20)
        assert coord.power_history[0].pv_production == 20.0
        assert coord.power_history[-1].pv_production == float(
            MAX_HISTORY_SIZE + 20 - 1
        )


# ---------------------------------------------------------------------------
# Tests: startup grace period
# ---------------------------------------------------------------------------


class TestStartupGracePeriod:
    """Tests for startup grace period behavior."""

    def test_grace_period_active_on_startup(self):
        """Coordinator should skip optimization during startup grace period."""
        coord = _make_coordinator(states={
            "sensor.pv_power": MockState("3000"),
            "sensor.grid_export": MockState("1500"),
            "sensor.load_power": MockState("1500"),
        })
        # Just created, so startup time is now
        elapsed = (datetime.now() - coord._startup_time).total_seconds()
        assert elapsed < DEFAULT_STARTUP_GRACE_PERIOD

    def test_grace_period_expired(self):
        """After grace period, optimization should proceed."""
        coord = _make_coordinator(states={
            "sensor.pv_power": MockState("3000"),
            "sensor.grid_export": MockState("1500"),
            "sensor.load_power": MockState("1500"),
        })
        # Backdate startup time
        coord._startup_time = datetime.now() - timedelta(
            seconds=DEFAULT_STARTUP_GRACE_PERIOD + 10
        )

        elapsed = (datetime.now() - coord._startup_time).total_seconds()
        assert elapsed >= DEFAULT_STARTUP_GRACE_PERIOD

    def test_disabled_skips_optimization(self):
        """When disabled, coordinator skips optimization."""
        coord = _make_coordinator(states={
            "sensor.pv_power": MockState("3000"),
            "sensor.grid_export": MockState("1500"),
            "sensor.load_power": MockState("1500"),
        })
        coord.enabled = False
        assert coord.enabled is False


# ---------------------------------------------------------------------------
# Tests: enabled property
# ---------------------------------------------------------------------------


class TestEnabledProperty:
    """Tests for the enabled master switch."""

    def test_enabled_by_default(self):
        """Coordinator is enabled by default."""
        coord = _make_coordinator()
        assert coord.enabled is True

    def test_toggle_enabled(self):
        """Can toggle enabled state."""
        coord = _make_coordinator()
        coord.enabled = False
        assert coord.enabled is False
        coord.enabled = True
        assert coord.enabled is True


# ---------------------------------------------------------------------------
# Tests: coordinator data output
# ---------------------------------------------------------------------------


class TestBuildCoordinatorData:
    """Tests for _build_coordinator_data."""

    def test_empty_state(self):
        """Data output when no history exists."""
        coord = _make_coordinator()
        data = coord._build_coordinator_data()

        assert data["power_state"] is None
        assert data["power_history"] == []
        assert data["current_plan"] is None
        assert data["control_decisions"] == []
        assert data["battery_discharge_action"] is None
        assert data["appliance_states"] == {}
        assert data["enabled"] is True

    def test_with_power_history(self):
        """Data output includes latest power state."""
        coord = _make_coordinator(states={
            "sensor.pv_power": MockState("3000"),
            "sensor.grid_export": MockState("1500"),
            "sensor.load_power": MockState("1500"),
        })

        ps = coord._collect_power_state()
        coord.power_history.append(ps)
        data = coord._build_coordinator_data()

        assert data["power_state"] is ps
        assert len(data["power_history"]) == 1

    def test_data_contains_decisions(self):
        """Data output includes control decisions."""
        coord = _make_coordinator()

        decision = ControlDecision(
            appliance_id="test_1",
            action=Action.ON,
            target_current=None,
            reason="test",
            overrides_plan=False,
        )
        coord.control_decisions = [decision]
        data = coord._build_coordinator_data()

        assert len(data["control_decisions"]) == 1
        assert data["control_decisions"][0].appliance_id == "test_1"


# ---------------------------------------------------------------------------
# Tests: tariff info retrieval
# ---------------------------------------------------------------------------


class TestGetTariffInfo:
    """Tests for _get_tariff_info."""

    def test_default_tariff_provider_returns_info(self):
        """Default (none) tariff provider returns basic TariffInfo with inf price when no entity."""
        coord = _make_coordinator()
        tariff = coord._get_tariff_info()

        assert isinstance(tariff, TariffInfo)
        assert tariff.current_price == float("inf")
        assert tariff.feed_in_tariff == 0.0


# ---------------------------------------------------------------------------
# Tests: empty plan creation
# ---------------------------------------------------------------------------


class TestCreateEmptyPlan:
    """Tests for _create_empty_plan."""

    def test_empty_plan_structure(self):
        """Empty plan has correct structure."""
        coord = _make_coordinator()
        plan = coord._create_empty_plan()

        assert plan.entries == []
        assert plan.confidence == 0.0
        assert plan.horizon == timedelta(hours=24)
        assert plan.battery_target is not None
        assert plan.battery_target.target_soc == 100.0


# ---------------------------------------------------------------------------
# Tests: appliance config extraction
# ---------------------------------------------------------------------------


class TestGetApplianceConfigs:
    """Tests for _get_appliance_configs."""

    def test_no_subentries(self):
        """No subentries means no appliance configs."""
        coord = _make_coordinator()
        configs = coord._get_appliance_configs()
        assert configs == []

    def test_subentry_conversion(self):
        """Subentries are converted to ApplianceConfig objects."""
        from custom_components.pv_excess_control.const import (
            CONF_APPLIANCE_NAME,
            CONF_APPLIANCE_ENTITY,
            CONF_APPLIANCE_PRIORITY,
            CONF_NOMINAL_POWER,
            CONF_PHASES,
            CONF_DYNAMIC_CURRENT,
        )

        subentry = MagicMock()
        subentry.data = {
            CONF_APPLIANCE_NAME: "Heat Pump",
            CONF_APPLIANCE_ENTITY: "switch.heat_pump",
            CONF_APPLIANCE_PRIORITY: 10,
            CONF_NOMINAL_POWER: 2000.0,
            CONF_PHASES: 3,
            CONF_DYNAMIC_CURRENT: False,
        }

        entry = _make_config_entry(subentries={"sub_1": subentry})
        coord = _make_coordinator(entry=entry)

        configs = coord._get_appliance_configs()

        assert len(configs) == 1
        assert configs[0].id == "sub_1"
        assert configs[0].name == "Heat Pump"
        assert configs[0].entity_id == "switch.heat_pump"
        assert configs[0].priority == 10
        assert configs[0].nominal_power == 2000.0
        assert configs[0].phases == 3
        assert configs[0].dynamic_current is False


# ---------------------------------------------------------------------------
# Tests: appliance state reading
# ---------------------------------------------------------------------------


class TestGetApplianceStates:
    """Tests for _get_appliance_states."""

    def test_reads_entity_state(self):
        """Reads the on/off state of an appliance entity."""
        from custom_components.pv_excess_control.models import ApplianceConfig

        states = {
            "switch.heat_pump": MockState("on"),
        }
        coord = _make_coordinator(states=states)

        config = ApplianceConfig(
            id="sub_1",
            name="Heat Pump",
            entity_id="switch.heat_pump",
            priority=10,
            phases=1,
            nominal_power=2000.0,
            actual_power_entity=None,
            dynamic_current=False,
            current_entity=None,
            min_current=0.0,
            max_current=16.0,
            ev_soc_entity=None,
            ev_connected_entity=None,
            is_big_consumer=False,
            battery_max_discharge_override=None,
            on_only=False,
            min_daily_runtime=None,
            max_daily_runtime=None,
            schedule_deadline=None,
            switch_interval=300,
            allow_grid_supplement=False,
            max_grid_power=None,
        )

        result = coord._get_appliance_states([config])

        assert "sub_1" in result
        assert result["sub_1"].is_on is True
        assert result["sub_1"].appliance_id == "sub_1"


    def test_disabled_appliance_state_preserved(self):
        """Disabling an appliance should NOT reset its runtime/energy counters."""
        from custom_components.pv_excess_control.models import ApplianceConfig, ApplianceState

        states = {"switch.pump": MockState("on")}
        # Create a subentry so the coordinator knows the appliance exists
        sub_mock = MagicMock()
        sub_mock.data = {
            "appliance_name": "Pool Pump",
            "appliance_entity": "switch.pump",
            "appliance_priority": 2,
            "nominal_power": 1000.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
        }
        entry = _make_config_entry(subentries={"sub_pump": sub_mock})
        coord = _make_coordinator(states=states, entry=entry)

        config = ApplianceConfig(
            id="sub_pump", name="Pool Pump", entity_id="switch.pump",
            priority=2, phases=1, nominal_power=1000.0, actual_power_entity=None,
            dynamic_current=False, current_entity=None, min_current=0.0,
            max_current=16.0, ev_soc_entity=None, ev_connected_entity=None,
            is_big_consumer=False, battery_max_discharge_override=None,
            on_only=False, min_daily_runtime=None, max_daily_runtime=None,
            schedule_deadline=None, switch_interval=300,
            allow_grid_supplement=False, max_grid_power=None,
        )

        # First call: build state with runtime accumulated
        result1 = coord._get_appliance_states([config])
        assert "sub_pump" in result1

        # Simulate accumulated runtime by setting state directly
        coord.appliance_states["sub_pump"] = ApplianceState(
            appliance_id="sub_pump", is_on=True, current_power=1000.0,
            current_amperage=None, runtime_today=timedelta(hours=1, minutes=30),
            energy_today=1.5, last_state_change=None,
            ev_connected=None, ev_soc=None,
        )

        # Now simulate disabling: call _get_appliance_states with EMPTY configs
        # (disabled appliance excluded from configs)
        result2 = coord._get_appliance_states([])

        # The disabled appliance's state should be PRESERVED
        assert "sub_pump" in result2
        assert result2["sub_pump"].runtime_today == timedelta(hours=1, minutes=30)
        assert result2["sub_pump"].energy_today == 1.5

    def test_removed_appliance_state_not_preserved(self):
        """State for a completely removed appliance (no subentry) should NOT be preserved."""
        from custom_components.pv_excess_control.models import ApplianceConfig, ApplianceState

        states = {"switch.pump": MockState("on")}
        # No subentries — the appliance was fully removed
        entry = _make_config_entry(subentries={})
        coord = _make_coordinator(states=states, entry=entry)

        # Simulate leftover state from before removal
        coord.appliance_states["old_appliance"] = ApplianceState(
            appliance_id="old_appliance", is_on=False, current_power=0.0,
            current_amperage=None, runtime_today=timedelta(hours=2),
            energy_today=3.0, last_state_change=None,
            ev_connected=None, ev_soc=None,
        )

        result = coord._get_appliance_states([])

        # Removed appliance should NOT be preserved (not in subentries)
        assert "old_appliance" not in result


# ---------------------------------------------------------------------------
# Tests: planner counter
# ---------------------------------------------------------------------------


class TestPlannerInterval:
    """Tests for planner interval tracking."""

    def test_planner_counter_increments(self):
        """Planner counter increments each cycle."""
        coord = _make_coordinator()
        assert coord._planner_counter == 0
        coord._planner_counter += 1
        assert coord._planner_counter == 1

    def test_planner_interval_ratio(self):
        """Planner runs every N controller cycles."""
        coord = _make_coordinator()
        # Default: planner=900s, controller=30s => ratio=30
        ratio = int(coord._planner_interval // coord.update_interval.total_seconds())
        assert ratio == 30  # 900 / 30 = 30


# ---------------------------------------------------------------------------
# Tests: update_from_subentries (sync UI changes without reload)
# ---------------------------------------------------------------------------


class TestUpdateFromSubentries:
    """update_from_subentries() must sync priority and runtime dicts from
    subentry data so UI changes take effect without a full reload."""

    def _make_entry_with_subentries(self, subentries_data: dict) -> MagicMock:
        """Build a mock config entry whose subentries map matches subentries_data."""
        entry = _make_config_entry()
        subentries = {}
        for sub_id, data in subentries_data.items():
            sub = MagicMock()
            sub.data = data
            subentries[sub_id] = sub
        entry.subentries = subentries
        return entry

    def test_priority_updated_from_subentry(self):
        """update_from_subentries refreshes appliance_priorities from subentry data."""
        from custom_components.pv_excess_control.const import CONF_APPLIANCE_PRIORITY

        entry = self._make_entry_with_subentries({"sub1": {CONF_APPLIANCE_PRIORITY: 200}})
        coord = _make_coordinator(entry=entry)
        coord.appliance_priorities = {"sub1": 500}  # stale value

        coord.update_from_subentries()

        assert coord.appliance_priorities["sub1"] == 200

    def test_min_daily_runtime_updated_from_subentry(self):
        """update_from_subentries refreshes appliance_min_daily_runtime."""
        from custom_components.pv_excess_control.const import (
            CONF_APPLIANCE_PRIORITY,
            CONF_MIN_DAILY_RUNTIME,
        )

        entry = self._make_entry_with_subentries(
            {"sub1": {CONF_APPLIANCE_PRIORITY: 300, CONF_MIN_DAILY_RUNTIME: 90}}
        )
        coord = _make_coordinator(entry=entry)
        coord.appliance_priorities = {"sub1": 300}
        coord.appliance_min_daily_runtime = {}

        coord.update_from_subentries()

        assert coord.appliance_min_daily_runtime["sub1"] == 90

    def test_min_daily_runtime_removed_when_absent(self):
        """update_from_subentries pops min_daily_runtime when key is not in subentry."""
        from custom_components.pv_excess_control.const import CONF_APPLIANCE_PRIORITY

        entry = self._make_entry_with_subentries({"sub1": {CONF_APPLIANCE_PRIORITY: 300}})
        coord = _make_coordinator(entry=entry)
        coord.appliance_min_daily_runtime = {"sub1": 60}  # stale

        coord.update_from_subentries()

        assert "sub1" not in coord.appliance_min_daily_runtime

    def test_max_daily_runtime_updated_from_subentry(self):
        """update_from_subentries refreshes appliance_max_daily_runtime."""
        from custom_components.pv_excess_control.const import (
            CONF_APPLIANCE_PRIORITY,
            CONF_MAX_DAILY_RUNTIME,
        )

        entry = self._make_entry_with_subentries(
            {"sub1": {CONF_APPLIANCE_PRIORITY: 300, CONF_MAX_DAILY_RUNTIME: 240}}
        )
        coord = _make_coordinator(entry=entry)
        coord.appliance_max_daily_runtime = {}

        coord.update_from_subentries()

        assert coord.appliance_max_daily_runtime["sub1"] == 240

    def test_max_daily_runtime_removed_when_absent(self):
        """update_from_subentries pops max_daily_runtime when key is not in subentry."""
        from custom_components.pv_excess_control.const import CONF_APPLIANCE_PRIORITY

        entry = self._make_entry_with_subentries({"sub1": {CONF_APPLIANCE_PRIORITY: 300}})
        coord = _make_coordinator(entry=entry)
        coord.appliance_max_daily_runtime = {"sub1": 120}  # stale

        coord.update_from_subentries()

        assert "sub1" not in coord.appliance_max_daily_runtime

    def test_multiple_subentries_all_updated(self):
        """update_from_subentries processes every subentry."""
        from custom_components.pv_excess_control.const import CONF_APPLIANCE_PRIORITY

        entry = self._make_entry_with_subentries({
            "sub1": {CONF_APPLIANCE_PRIORITY: 100},
            "sub2": {CONF_APPLIANCE_PRIORITY: 800},
        })
        coord = _make_coordinator(entry=entry)
        coord.appliance_priorities = {}

        coord.update_from_subentries()

        assert coord.appliance_priorities["sub1"] == 100
        assert coord.appliance_priorities["sub2"] == 800


# ---------------------------------------------------------------------------
# Tests: async_setup_entry / async_unload_entry
# ---------------------------------------------------------------------------


class TestSetupAndUnload:
    """Tests for integration setup and unload using __init__.py functions.

    These test the public API contract of async_setup_entry and
    async_unload_entry. Full HA integration is mocked.
    """

    @pytest.mark.asyncio
    async def test_async_setup_entry_creates_coordinator(self):
        """async_setup_entry creates coordinator and stores in hass.data."""
        from custom_components.pv_excess_control import (
            async_setup_entry,
            PLATFORMS,
        )
        from custom_components.pv_excess_control.coordinator import PvExcessCoordinator

        hass = MockHass(states={
            "sensor.pv_power": MockState("1000"),
            "sensor.grid_export": MockState("500"),
            "sensor.load_power": MockState("500"),
        })
        entry = _make_config_entry()

        # Patch the coordinator to avoid real HA DataUpdateCoordinator behavior
        with patch.object(
            PvExcessCoordinator, "async_config_entry_first_refresh", new_callable=AsyncMock
        ) as mock_refresh, patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as mock_forward:
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert DOMAIN in hass.data
        assert entry.entry_id in hass.data[DOMAIN]
        assert isinstance(hass.data[DOMAIN][entry.entry_id], PvExcessCoordinator)
        mock_refresh.assert_awaited_once()
        mock_forward.assert_awaited_once_with(entry, PLATFORMS)

    @pytest.mark.asyncio
    async def test_async_unload_entry_removes_data(self):
        """async_unload_entry removes coordinator from hass.data."""
        from custom_components.pv_excess_control import async_unload_entry, PLATFORMS

        hass = MockHass()
        entry = _make_config_entry()

        # Pre-populate hass.data as if setup had run
        coordinator = _make_coordinator(hass=hass, entry=entry)
        hass.data[DOMAIN] = {entry.entry_id: coordinator}

        with patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_unload:
            result = await async_unload_entry(hass, entry)

        assert result is True
        assert entry.entry_id not in hass.data[DOMAIN]
        mock_unload.assert_awaited_once_with(entry, PLATFORMS)

    @pytest.mark.asyncio
    async def test_async_unload_entry_failure(self):
        """async_unload_entry preserves data on platform unload failure."""
        from custom_components.pv_excess_control import async_unload_entry

        hass = MockHass()
        entry = _make_config_entry()

        coordinator = _make_coordinator(hass=hass, entry=entry)
        hass.data[DOMAIN] = {entry.entry_id: coordinator}

        with patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await async_unload_entry(hass, entry)

        assert result is False
        # Coordinator should still be in hass.data
        assert entry.entry_id in hass.data[DOMAIN]


# ---------------------------------------------------------------------------
# Tests: full update cycle (integration-style)
# ---------------------------------------------------------------------------


class TestUpdateCycle:
    """Tests for the full _async_update_data cycle."""

    @pytest.mark.asyncio
    async def test_update_collects_power_and_appends_history(self):
        """Update cycle collects power state and appends to history."""
        states = {
            "sensor.pv_power": MockState("4000"),
            "sensor.grid_export": MockState("2000"),
            "sensor.load_power": MockState("2000"),
        }
        coord = _make_coordinator(states=states)
        # Skip grace period
        coord._startup_time = datetime.now() - timedelta(
            seconds=DEFAULT_STARTUP_GRACE_PERIOD + 1
        )

        data = await coord._async_update_data()

        assert len(coord.power_history) == 1
        assert coord.power_history[0].pv_production == 4000.0
        assert data["power_state"] is not None
        assert data["power_state"].pv_production == 4000.0

    @pytest.mark.asyncio
    async def test_update_skips_during_grace_period(self):
        """Optimizer is skipped during grace period but power state is still collected."""
        states = {
            "sensor.pv_power": MockState("3000"),
            "sensor.grid_export": MockState("1500"),
            "sensor.load_power": MockState("1500"),
        }
        coord = _make_coordinator(states=states)
        # Keep startup time as now (within grace period)

        data = await coord._async_update_data()

        # Power state should still be collected
        assert len(coord.power_history) == 1
        # But no control decisions should be made
        assert data["control_decisions"] == []

    @pytest.mark.asyncio
    async def test_update_skips_when_disabled(self):
        """Optimizer is skipped when coordinator is disabled."""
        states = {
            "sensor.pv_power": MockState("3000"),
            "sensor.grid_export": MockState("1500"),
            "sensor.load_power": MockState("1500"),
        }
        coord = _make_coordinator(states=states)
        coord._enabled = False
        coord._startup_time = datetime.now() - timedelta(
            seconds=DEFAULT_STARTUP_GRACE_PERIOD + 1
        )

        data = await coord._async_update_data()

        # Power state collected
        assert len(coord.power_history) == 1
        # No optimization
        assert data["control_decisions"] == []

    @pytest.mark.asyncio
    async def test_update_enforces_history_max_size(self):
        """Update cycle enforces MAX_HISTORY_SIZE limit."""
        from custom_components.pv_excess_control.coordinator import MAX_HISTORY_SIZE

        states = {
            "sensor.pv_power": MockState("1000"),
            "sensor.grid_export": MockState("500"),
            "sensor.load_power": MockState("500"),
        }
        coord = _make_coordinator(states=states)

        # Pre-fill history to just below max
        for i in range(MAX_HISTORY_SIZE):
            coord.power_history.append(
                PowerState(
                    pv_production=float(i),
                    grid_export=0.0,
                    grid_import=0.0,
                    load_power=0.0,
                    excess_power=0.0,
                    battery_soc=None,
                    battery_power=None,
                    ev_soc=None,
                    timestamp=datetime.now(),
                )
            )

        assert len(coord.power_history) == MAX_HISTORY_SIZE

        # One more update should cap at MAX_HISTORY_SIZE
        await coord._async_update_data()
        assert len(coord.power_history) == MAX_HISTORY_SIZE

    @pytest.mark.asyncio
    async def test_planner_counter_resets(self):
        """Planner counter resets when it reaches the ratio threshold."""
        coord = _make_coordinator(states={
            "sensor.pv_power": MockState("1000"),
            "sensor.grid_export": MockState("500"),
            "sensor.load_power": MockState("500"),
        })

        # Set counter to one below reset threshold
        ratio = max(
            1,
            int(coord._planner_interval // coord.update_interval.total_seconds()),
        )
        coord._planner_counter = ratio - 1

        await coord._async_update_data()

        # Counter should have reset to 0 after reaching ratio
        assert coord._planner_counter == 0

    @pytest.mark.asyncio
    async def test_update_with_all_unavailable_sensors_does_not_raise(self, caplog):
        """_async_update_data must not raise when all power sensors are unavailable."""
        states = {
            "sensor.pv_power": MockState("unavailable"),
            "sensor.grid_export": MockState("unavailable"),
            "sensor.load_power": MockState("unavailable"),
        }
        coord = _make_coordinator(states=states)
        # Skip grace period so analytics + attribution code is exercised
        coord._startup_time = datetime.now() - timedelta(
            seconds=DEFAULT_STARTUP_GRACE_PERIOD + 1
        )

        # Must not raise TypeError (or any other exception)
        data = await coord._async_update_data()

        # PowerState with None fields should be stored in history
        assert len(coord.power_history) == 1
        assert coord.power_history[0].excess_power is None
        # No TypeError should appear in the captured log
        assert "TypeError" not in caplog.text


# ---------------------------------------------------------------------------
# Tests: _needed_by_others cooldown bypass (Bug C from 2026-04-09 incident)
# ---------------------------------------------------------------------------


class TestNeededByOthersCooldownBypass:
    """Tests for the Bug C fix: appliances referenced by another appliance's
    requires_appliance bypass the switch-interval cooldown uniformly."""

    def test_needed_by_others_set_populated_by_requires_appliance(self):
        """A config with requires_appliance=X puts X in _needed_by_others."""
        sub_helper = MagicMock()
        sub_helper.data = {
            "appliance_name": "Pool Pump",
            "appliance_entity": "switch.pool_pump",
            "appliance_priority": 500,
            "nominal_power": 200.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
        }
        sub_dep = MagicMock()
        sub_dep.data = {
            "appliance_name": "Chlorgen",
            "appliance_entity": "switch.chlorgen",
            "appliance_priority": 400,
            "nominal_power": 40.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
            "requires_appliance": "sub_helper",
        }
        entry = _make_config_entry(
            subentries={"sub_helper": sub_helper, "sub_dep": sub_dep},
        )
        coord = _make_coordinator(entry=entry)

        coord._get_appliance_configs()

        assert coord._needed_by_others == {"sub_helper"}

    def test_needed_by_others_set_empty_when_no_dependencies(self):
        """No requires_appliance means empty _needed_by_others."""
        sub = MagicMock()
        sub.data = {
            "appliance_name": "Standalone",
            "appliance_entity": "switch.standalone",
            "appliance_priority": 500,
            "nominal_power": 200.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
        }
        entry = _make_config_entry(subentries={"sub_1": sub})
        coord = _make_coordinator(entry=entry)

        coord._get_appliance_configs()

        assert coord._needed_by_others == set()

    def test_needed_by_others_refreshed_on_dependent_removal(self):
        """Removing the dependent shrinks _needed_by_others on the next cycle."""
        sub_helper = MagicMock()
        sub_helper.data = {
            "appliance_name": "Pool Pump",
            "appliance_entity": "switch.pool_pump",
            "appliance_priority": 500,
            "nominal_power": 200.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
        }
        sub_dep = MagicMock()
        sub_dep.data = {
            "appliance_name": "Chlorgen",
            "appliance_entity": "switch.chlorgen",
            "appliance_priority": 400,
            "nominal_power": 40.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
            "requires_appliance": "sub_helper",
        }
        entry = _make_config_entry(
            subentries={"sub_helper": sub_helper, "sub_dep": sub_dep},
        )
        coord = _make_coordinator(entry=entry)
        coord._get_appliance_configs()
        assert coord._needed_by_others == {"sub_helper"}

        # Remove the dependent (simulate user deleting the subentry)
        entry.subentries = {"sub_helper": sub_helper}
        coord._get_appliance_configs()

        assert coord._needed_by_others == set()

    def test_cooldown_bypassed_for_appliance_in_needed_by_others(self):
        """The switch-interval guard skips appliances in _needed_by_others."""
        from custom_components.pv_excess_control.models import (
            ApplianceConfig, BatteryDischargeAction, ControlDecision, Action,
            OptimizerResult,
        )
        states = {"switch.pool_pump": MockState("off")}
        sub_helper = MagicMock()
        sub_helper.data = {
            "appliance_name": "Pool Pump",
            "appliance_entity": "switch.pool_pump",
            "appliance_priority": 500,
            "nominal_power": 200.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
        }
        sub_dep = MagicMock()
        sub_dep.data = {
            "appliance_name": "Chlorgen",
            "appliance_entity": "switch.chlorgen",
            "appliance_priority": 400,
            "nominal_power": 40.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
            "requires_appliance": "sub_helper",
        }
        entry = _make_config_entry(
            subentries={"sub_helper": sub_helper, "sub_dep": sub_dep},
        )
        coord = _make_coordinator(states=states, entry=entry)

        # Populate configs and _needed_by_others
        configs = coord._get_appliance_configs()
        # Simulate: pool pump was turned off 10 seconds ago (cooldown would
        # normally block the next on-command for 290 more seconds)
        coord._last_state_change["sub_helper"] = datetime.now() - timedelta(seconds=10)

        # Build an ON decision for the helper without bypasses_cooldown flag
        decision = ControlDecision(
            appliance_id="sub_helper",
            action=Action.ON,
            target_current=None,
            reason="Helper-only: dependent is running",
            overrides_plan=False,
        )
        result = OptimizerResult(
            decisions=[decision],
            battery_discharge_action=BatteryDischargeAction(
                should_limit=False,
                max_discharge_watts=None,
            ),
        )

        # Apply decisions
        asyncio.get_event_loop().run_until_complete(coord._apply_decisions(result))

        # The service call should have been made (cooldown bypassed because
        # sub_helper is in _needed_by_others)
        turn_on_calls = [c for c in coord.hass.services.calls if c[1] == "turn_on"]
        assert len(turn_on_calls) == 1
        assert turn_on_calls[0][2]["entity_id"] == "switch.pool_pump"

    def test_cooldown_respected_for_appliance_not_needed_by_others(self):
        """A standalone appliance (not in _needed_by_others) still respects cooldown."""
        from custom_components.pv_excess_control.models import (
            BatteryDischargeAction, ControlDecision, Action, OptimizerResult,
        )
        states = {"switch.standalone": MockState("off")}
        sub = MagicMock()
        sub.data = {
            "appliance_name": "Standalone",
            "appliance_entity": "switch.standalone",
            "appliance_priority": 500,
            "nominal_power": 200.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
        }
        entry = _make_config_entry(subentries={"sub_1": sub})
        coord = _make_coordinator(states=states, entry=entry)

        coord._get_appliance_configs()
        # Recent toggle: 10 seconds ago
        coord._last_state_change["sub_1"] = datetime.now() - timedelta(seconds=10)

        decision = ControlDecision(
            appliance_id="sub_1",
            action=Action.ON,
            target_current=None,
            reason="Excess available",
            overrides_plan=False,
        )
        result = OptimizerResult(
            decisions=[decision],
            battery_discharge_action=BatteryDischargeAction(
                should_limit=False,
                max_discharge_watts=None,
            ),
        )

        asyncio.get_event_loop().run_until_complete(coord._apply_decisions(result))

        # The service call should NOT have been made (cooldown still in effect)
        turn_on_calls = [c for c in coord.hass.services.calls if c[1] == "turn_on"]
        assert len(turn_on_calls) == 0

    def test_chained_dependencies_both_in_needed_by_others(self):
        """In a dependency chain A→B→C (A requires B, B requires C), both
        B and C should be in _needed_by_others. A is not needed by anyone."""
        sub_a = MagicMock()
        sub_a.data = {
            "appliance_name": "A",
            "appliance_entity": "switch.a",
            "appliance_priority": 300,
            "nominal_power": 100.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
            "requires_appliance": "sub_b",
        }
        sub_b = MagicMock()
        sub_b.data = {
            "appliance_name": "B",
            "appliance_entity": "switch.b",
            "appliance_priority": 400,
            "nominal_power": 200.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
            "requires_appliance": "sub_c",
        }
        sub_c = MagicMock()
        sub_c.data = {
            "appliance_name": "C",
            "appliance_entity": "switch.c",
            "appliance_priority": 500,
            "nominal_power": 300.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
        }
        entry = _make_config_entry(
            subentries={"sub_a": sub_a, "sub_b": sub_b, "sub_c": sub_c},
        )
        coord = _make_coordinator(entry=entry)

        coord._get_appliance_configs()

        # Both B and C are referenced by another appliance → needed by others
        # A is not referenced by anyone → NOT in the set
        assert coord._needed_by_others == {"sub_b", "sub_c"}

    def test_off_transition_also_bypasses_cooldown_for_needed_appliance(self):
        """The _needed_by_others bypass applies to OFF commands too, not only ON."""
        from custom_components.pv_excess_control.models import (
            BatteryDischargeAction, ControlDecision, Action, OptimizerResult,
        )
        states = {"switch.pool_pump": MockState("on")}
        sub_helper = MagicMock()
        sub_helper.data = {
            "appliance_name": "Pool Pump",
            "appliance_entity": "switch.pool_pump",
            "appliance_priority": 500,
            "nominal_power": 200.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
        }
        sub_dep = MagicMock()
        sub_dep.data = {
            "appliance_name": "Chlorgen",
            "appliance_entity": "switch.chlorgen",
            "appliance_priority": 400,
            "nominal_power": 40.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
            "requires_appliance": "sub_helper",
        }
        entry = _make_config_entry(
            subentries={"sub_helper": sub_helper, "sub_dep": sub_dep},
        )
        coord = _make_coordinator(states=states, entry=entry)

        # Populate configs and _needed_by_others
        coord._get_appliance_configs()
        # Simulate: pool pump was turned on 10 seconds ago (cooldown would
        # normally block the next off-command for 290 more seconds)
        coord._last_state_change["sub_helper"] = datetime.now() - timedelta(seconds=10)

        # Build an OFF decision for the helper without bypasses_cooldown flag
        decision = ControlDecision(
            appliance_id="sub_helper",
            action=Action.OFF,
            target_current=None,
            reason="Helper-only: no dependent running",
            overrides_plan=False,
        )
        result = OptimizerResult(
            decisions=[decision],
            battery_discharge_action=BatteryDischargeAction(
                should_limit=False,
                max_discharge_watts=None,
            ),
        )

        asyncio.get_event_loop().run_until_complete(coord._apply_decisions(result))

        # The OFF service call should have been made (cooldown bypassed because
        # sub_helper is in _needed_by_others)
        turn_off_calls = [c for c in coord.hass.services.calls if c[1] == "turn_off"]
        assert len(turn_off_calls) == 1
        assert turn_off_calls[0][2]["entity_id"] == "switch.pool_pump"


# ---------------------------------------------------------------------------
# Tests: activation counting on physical state transitions (Bug A from 2026-04-09 incident)
# ---------------------------------------------------------------------------


class TestActivationCountingOnStateTransition:
    """Tests for Bug A fix: activations_today is incremented when a physical
    off→on transition is detected between cycles, not when a turn_on service
    call succeeds."""

    def _make_config(self, appliance_id: str = "sub_1", entity_id: str = "switch.test"):
        from custom_components.pv_excess_control.models import ApplianceConfig
        return ApplianceConfig(
            id=appliance_id,
            name="Test",
            entity_id=entity_id,
            priority=500,
            phases=1,
            nominal_power=1000.0,
            actual_power_entity=None,
            dynamic_current=False,
            current_entity=None,
            min_current=0.0,
            max_current=16.0,
            ev_soc_entity=None,
            ev_connected_entity=None,
            is_big_consumer=False,
            battery_max_discharge_override=None,
            on_only=False,
            min_daily_runtime=None,
            max_daily_runtime=None,
            schedule_deadline=None,
            switch_interval=300,
            allow_grid_supplement=False,
            max_grid_power=None,
        )

    def _make_subentry(self, appliance_id: str, entity_id: str):
        sub = MagicMock()
        sub.data = {
            "appliance_name": "Test",
            "appliance_entity": entity_id,
            "appliance_priority": 500,
            "nominal_power": 1000.0,
            "phases": 1,
            "dynamic_current": False,
            "switch_interval": 300,
            "on_only": False,
            "allow_grid_supplement": False,
            "is_big_consumer": False,
        }
        return sub

    def test_off_to_on_transition_counts_activation(self):
        """prev=False, current=True → counter increments by 1."""
        coord = _make_coordinator(
            states={"switch.test": MockState("off")},
            entry=_make_config_entry(
                subentries={"sub_1": self._make_subentry("sub_1", "switch.test")},
            ),
        )
        config = self._make_config()

        # First cycle: appliance is off, baseline seeded
        coord.hass.states._states["switch.test"] = MockState("off")
        coord._get_appliance_states([config])
        assert coord._activations_today.get("sub_1", 0) == 0

        # Second cycle: appliance is now on — transition detected
        coord.hass.states._states["switch.test"] = MockState("on")
        coord._get_appliance_states([config])
        assert coord._activations_today["sub_1"] == 1

    def test_on_to_off_transition_does_not_count(self):
        """prev=True, current=False → counter unchanged."""
        coord = _make_coordinator(
            states={"switch.test": MockState("on")},
            entry=_make_config_entry(
                subentries={"sub_1": self._make_subentry("sub_1", "switch.test")},
            ),
        )
        config = self._make_config()

        # First cycle: on (baseline)
        coord._get_appliance_states([config])
        assert coord._activations_today.get("sub_1", 0) == 0

        # Second cycle: off
        coord.hass.states._states["switch.test"] = MockState("off")
        coord._get_appliance_states([config])
        assert coord._activations_today.get("sub_1", 0) == 0

    def test_already_on_at_startup_does_not_count(self):
        """First-cycle seed should NOT count an existing on-state as a new activation."""
        coord = _make_coordinator(
            states={"switch.test": MockState("on")},
            entry=_make_config_entry(
                subentries={"sub_1": self._make_subentry("sub_1", "switch.test")},
            ),
        )
        config = self._make_config()

        # First cycle ever: _previous_is_on is empty. State is on.
        coord._get_appliance_states([config])

        # No activation counted on the seed cycle
        assert coord._activations_today.get("sub_1", 0) == 0
        # But _previous_is_on should now have the baseline
        assert coord._previous_is_on["sub_1"] is True

    def test_failed_service_call_with_optimistic_on_then_off_no_count(self):
        """HA state briefly flips on then off within the same cycle (Sonoff scenario).
        The next cycle's state read shows off, so no transition is detected."""
        coord = _make_coordinator(
            states={"switch.test": MockState("off")},
            entry=_make_config_entry(
                subentries={"sub_1": self._make_subentry("sub_1", "switch.test")},
            ),
        )
        config = self._make_config()

        # First cycle: off baseline
        coord._get_appliance_states([config])

        # Simulate a failed turn_on: the service call was made, HA briefly
        # reported on, then the device callback reported off. We don't count
        # the intent. The next state read shows off.
        coord.hass.states._states["switch.test"] = MockState("off")
        coord._get_appliance_states([config])
        assert coord._activations_today.get("sub_1", 0) == 0

    def test_external_automation_turn_on_counts(self):
        """If an external actor turns on the appliance, the next cycle's
        state-diff still counts the activation."""
        coord = _make_coordinator(
            states={"switch.test": MockState("off")},
            entry=_make_config_entry(
                subentries={"sub_1": self._make_subentry("sub_1", "switch.test")},
            ),
        )
        config = self._make_config()

        # Cycle 1: off
        coord._get_appliance_states([config])
        # External turn_on between cycles
        coord.hass.states._states["switch.test"] = MockState("on")
        # Cycle 2: state read detects on
        coord._get_appliance_states([config])

        assert coord._activations_today["sub_1"] == 1

    def test_multiple_transitions_count_correctly(self):
        """off→on→off→on across cycles → counter = 2."""
        coord = _make_coordinator(
            states={"switch.test": MockState("off")},
            entry=_make_config_entry(
                subentries={"sub_1": self._make_subentry("sub_1", "switch.test")},
            ),
        )
        config = self._make_config()

        # Cycle 1: off (baseline)
        coord._get_appliance_states([config])
        assert coord._activations_today.get("sub_1", 0) == 0

        # Cycle 2: on → count 1
        coord.hass.states._states["switch.test"] = MockState("on")
        coord._get_appliance_states([config])
        assert coord._activations_today["sub_1"] == 1

        # Cycle 3: off → no change
        coord.hass.states._states["switch.test"] = MockState("off")
        coord._get_appliance_states([config])
        assert coord._activations_today["sub_1"] == 1

        # Cycle 4: on → count 2
        coord.hass.states._states["switch.test"] = MockState("on")
        coord._get_appliance_states([config])
        assert coord._activations_today["sub_1"] == 2

    def test_stale_previous_is_on_cleaned_on_subentry_removal(self):
        """Removing a subentry purges _previous_is_on on the next config build."""
        sub = self._make_subentry("sub_1", "switch.test")
        entry = _make_config_entry(subentries={"sub_1": sub})
        coord = _make_coordinator(
            states={"switch.test": MockState("on")},
            entry=entry,
        )
        config = self._make_config()

        # Populate _previous_is_on via a state read
        coord._get_appliance_states([config])
        assert "sub_1" in coord._previous_is_on

        # Remove the subentry
        entry.subentries = {}
        coord._get_appliance_configs()

        # Stale cleanup should have removed it
        assert "sub_1" not in coord._previous_is_on

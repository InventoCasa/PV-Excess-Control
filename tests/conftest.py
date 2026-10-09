"""Shared test fixtures for PV Excess Control tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.pv_excess_control.models import (
    Action,
    ApplianceConfig,
    ApplianceState,
    BatteryDischargeAction,
    BatteryStrategy,
    BatteryTarget,
    ControlDecision,
    OptimizerResult,
    Plan,
    PowerState,
    TariffInfo,
    TariffWindow,
)


@pytest.fixture
def sample_power_state():
    """A typical power state with solar excess."""
    return PowerState(
        pv_production=4000.0,
        grid_export=1500.0,
        grid_import=0.0,
        load_power=2500.0,
        excess_power=1500.0,
        battery_soc=80.0,
        battery_power=500.0,
        ev_soc=None,
        timestamp=datetime.now(),
    )


@pytest.fixture
def sample_appliance_config():
    """A simple switch appliance config."""
    return ApplianceConfig(
        id="test_app",
        name="Test Appliance",
        entity_id="switch.test_app",
        priority=5,
        phases=1,
        nominal_power=1000.0,
        actual_power_entity=None,
        dynamic_current=False,
        current_entity=None,
        min_current=0.0,
        max_current=0.0,
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


@pytest.fixture
def sample_tariff():
    """Standard tariff info."""
    return TariffInfo(
        current_price=0.25,
        feed_in_tariff=0.08,
        cheap_price_threshold=0.10,
        battery_charge_price_threshold=0.05,
    )


@pytest.fixture
def mock_hass():
    """Mock Home Assistant instance."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    hass.config = MagicMock()
    hass.config.path = MagicMock(return_value="/config/test")
    hass.data = {}
    hass.http = MagicMock()
    hass.http.register_static_path = MagicMock()
    return hass


# ---------------------------------------------------------------------------
# Coordinator test fixtures (used by test_coordinator.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_inverter_controller():
    """An AsyncMock standing in for an InverterGridChargeController."""
    ctl = MagicMock()
    ctl.engage = AsyncMock(return_value=None)
    ctl.disengage = AsyncMock(return_value=None)
    ctl.verify_engaged = AsyncMock(return_value=None)
    ctl.verify_disengaged = AsyncMock(return_value=None)
    ctl.confirmation_level = "physical"
    from custom_components.pv_excess_control.models import InverterGridChargeConfig
    ctl.config = InverterGridChargeConfig("switch.charge", "on", "off")
    return ctl


@pytest.fixture
def mock_tariff_at():
    """Build a TariffInfo at a given price and threshold."""
    def _build(current_price, battery_charge_price_threshold=0.02, cheap_price_threshold=0.05, feed_in_tariff=0.08):
        return TariffInfo(
            current_price=current_price,
            feed_in_tariff=feed_in_tariff,
            cheap_price_threshold=cheap_price_threshold,
            battery_charge_price_threshold=battery_charge_price_threshold,
        )
    return _build


@pytest.fixture
def mock_power_state_with_soc():
    """Build a PowerState with the given battery_soc."""
    def _build(battery_soc):
        return PowerState(
            pv_production=0.0,
            grid_export=0.0,
            grid_import=0.0,
            load_power=0.0,
            excess_power=0.0,
            battery_soc=battery_soc,
            battery_power=None,
            ev_soc=None,
            timestamp=datetime.now(timezone.utc),
        )
    return _build


@pytest.fixture
def coordinator_factory():
    """Build a PvExcessCoordinator with optional config_data overrides and an injectable inverter_ctl."""
    from unittest.mock import patch as _patch

    from custom_components.pv_excess_control.const import (
        CONF_CONTROLLER_INTERVAL,
        CONF_FORECAST_PROVIDER,
        CONF_GRID_EXPORT,
        CONF_GRID_VOLTAGE,
        CONF_LOAD_POWER,
        CONF_PLAN_INFLUENCE,
        CONF_PLANNER_INTERVAL,
        CONF_PV_POWER,
        CONF_TARIFF_PROVIDER,
        DEFAULT_CONTROLLER_INTERVAL,
        DEFAULT_GRID_VOLTAGE,
        DEFAULT_PLANNER_INTERVAL,
        BatteryStrategy,
        PlanInfluence,
        TariffProvider as TariffProviderEnum,
        ForecastProvider as ForecastProviderEnum,
    )
    from custom_components.pv_excess_control.coordinator import PvExcessCoordinator
    from custom_components.pv_excess_control.optimizer import Optimizer
    from custom_components.pv_excess_control.planner import Planner
    from custom_components.pv_excess_control.energy import create_tariff_provider
    from custom_components.pv_excess_control.analytics import AnalyticsTracker
    from custom_components.pv_excess_control.notifications import NotificationManager

    import asyncio

    class _MockHassConfig:
        time_zone: str = "UTC"

    class _MockConfigEntries:
        def async_update_entry(self, entry, **kwargs):
            if "data" in kwargs:
                entry.data = kwargs["data"]

    class _MockHass:
        def __init__(self):
            self.states = MagicMock()
            self.services = MagicMock()
            self.services.async_call = AsyncMock()
            self.config_entries = _MockConfigEntries()
            self.data = {}
            self.http = MagicMock()
            self.config = _MockHassConfig()
            self.bus = MagicMock()
            self.bus.async_listen_once = MagicMock(return_value=MagicMock())
            self.async_add_job = MagicMock()
            self.loop = asyncio.get_event_loop()
            self.async_create_task = MagicMock(
                side_effect=lambda coro, **kw: asyncio.ensure_future(coro)
            )

    def _build(config_data=None, inverter_ctl=...):
        hass = _MockHass()

        base_data = {
            CONF_PV_POWER: "sensor.pv_power",
            CONF_GRID_EXPORT: "sensor.grid_export",
            CONF_LOAD_POWER: "sensor.load_power",
            CONF_GRID_VOLTAGE: DEFAULT_GRID_VOLTAGE,
            CONF_CONTROLLER_INTERVAL: DEFAULT_CONTROLLER_INTERVAL,
            CONF_PLANNER_INTERVAL: DEFAULT_PLANNER_INTERVAL,
            CONF_TARIFF_PROVIDER: TariffProviderEnum.NONE,
            CONF_FORECAST_PROVIDER: ForecastProviderEnum.NONE,
        }
        if config_data:
            base_data.update(config_data)

        entry = MagicMock()
        entry.entry_id = "test_coordinator_entry"
        entry.data = base_data
        entry.subentries = {}

        with _patch.object(PvExcessCoordinator, "__init__", lambda self, h, e: None):
            coord = PvExcessCoordinator.__new__(PvExcessCoordinator)

        coord.hass = hass
        coord.config_entry = entry
        coord.logger = MagicMock()
        coord.name = "pv_excess_control"
        coord.update_interval = timedelta(seconds=DEFAULT_CONTROLLER_INTERVAL)

        grid_voltage = entry.data.get(CONF_GRID_VOLTAGE, DEFAULT_GRID_VOLTAGE)

        coord.optimizer = Optimizer(grid_voltage=grid_voltage)
        coord.planner = Planner(grid_voltage=grid_voltage)
        coord.power_history = []
        coord._last_sensor_available = {}
        coord._last_appliance_configs = []
        coord.current_plan = None
        coord.appliance_states = {}
        coord.control_decisions = []
        coord.battery_discharge_action = None
        coord._planner_interval = entry.data.get(CONF_PLANNER_INTERVAL, DEFAULT_PLANNER_INTERVAL)
        coord._planner_counter = 0
        coord._startup_time = datetime.now()
        coord._enabled = True
        coord._forecast_provider = None
        coord._last_tariff_info = None
        coord.force_charge = False
        coord.appliance_enabled = {}
        coord.appliance_overrides = {}
        coord.appliance_priorities = {}
        coord.appliance_min_daily_runtime = {}
        coord.appliance_max_daily_runtime = {}
        coord.battery_strategy = BatteryStrategy.BALANCED
        coord._plan_influence = PlanInfluence.LIGHT
        coord._last_discharge_limit = None
        coord._last_state_change = {}
        coord._last_applied_current = {}
        coord._activations_today = {}
        coord._needed_by_others = set()
        coord._previous_is_on = {}
        coord._was_enabled = True

        tariff_type = entry.data.get(CONF_TARIFF_PROVIDER, TariffProviderEnum.NONE)
        coord._tariff_provider = create_tariff_provider(tariff_type, "")

        coord.analytics = AnalyticsTracker(feed_in_tariff=0.0, normal_import_price=0.25)
        coord.notifications = NotificationManager(
            hass, notification_settings=None, notification_service=None,
        )

        # Grid charge state machine fields
        coord._grid_charge_engaged = entry.data.get("_grid_charge_engaged", False)
        coord._grid_charge_engage_ts = None
        coord._force_charge_prev = coord.force_charge
        coord._latest_tariff = None
        coord._latest_power_state = None

        # Inject inverter controller if explicitly supplied
        if inverter_ctl is not ...:
            coord._inverter_ctl = inverter_ctl
        else:
            coord._inverter_ctl = None

        with _patch("custom_components.pv_excess_control.battery_journal.BatteryOwnershipJournal"):
            coord._init_battery_control()
        coord._battery_storage_ready = True
        coord._battery_journal = AsyncMock()
        return coord

    return _build


@pytest.fixture
def freeze_time(monkeypatch):
    """Patches the coordinator module's `_time.monotonic` so the state machine
    sees controllable elapsed time. Returns an object with .tick(seconds: float).
    Use as `with freeze_time() as ft:` then `ft.tick(seconds=N)`."""
    class _FT:
        def __init__(self):
            self._t = 1_000_000.0
        def __enter__(self):
            monkeypatch.setattr(
                "custom_components.pv_excess_control.coordinator._time.monotonic",
                lambda: self._t,
            )
            return self
        def __exit__(self, *args):
            return None
        def tick(self, seconds: float):
            self._t += seconds
    return _FT

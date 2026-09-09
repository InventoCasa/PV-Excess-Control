"""Comprehensive tests for PV Excess Control data models."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from custom_components.pv_excess_control.models import (
    Action,
    ApplianceConfig,
    ApplianceState,
    BatteryChargeSetpoint,
    BatteryChargeCurve,
    BatteryConfig,
    BatteryDischargeAction,
    BatteryStrategy,
    BatteryTarget,
    ControlDecision,
    OptimizerResult,
    Plan,
    PlanEntry,
    PlanReason,
    PowerState,
    TariffInfo,
    TariffWindow,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime(2026, 3, 22, 12, 0, 0, tzinfo=timezone.utc)


def _make_appliance_config(**kwargs) -> ApplianceConfig:
    """Return a minimal ApplianceConfig with sensible defaults."""
    defaults = dict(
        id="appliance_1",
        name="Washing Machine",
        entity_id="switch.washing_machine",
        priority=100,
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
    defaults.update(kwargs)
    return ApplianceConfig(**defaults)


# ---------------------------------------------------------------------------
# TestPowerState
# ---------------------------------------------------------------------------

class TestPowerState:
    def test_create_with_battery(self):
        ts = _utcnow()
        ps = PowerState(
            pv_production=5000.0,
            grid_export=1200.0,
            grid_import=0.0,
            load_power=3800.0,
            excess_power=1200.0,
            battery_soc=80.5,
            battery_power=500.0,
            ev_soc=None,
            timestamp=ts,
        )
        assert ps.pv_production == 5000.0
        assert ps.grid_export == 1200.0
        assert ps.grid_import == 0.0
        assert ps.load_power == 3800.0
        assert ps.excess_power == 1200.0
        assert ps.battery_soc == 80.5
        assert ps.battery_power == 500.0
        assert ps.ev_soc is None
        assert ps.timestamp is ts

    def test_create_without_battery(self):
        ts = _utcnow()
        ps = PowerState(
            pv_production=3000.0,
            grid_export=0.0,
            grid_import=500.0,
            load_power=3500.0,
            excess_power=-500.0,
            battery_soc=None,
            battery_power=None,
            ev_soc=None,
            timestamp=ts,
        )
        assert ps.battery_soc is None
        assert ps.battery_power is None
        assert ps.excess_power == -500.0

    def test_create_with_ev_soc(self):
        ts = _utcnow()
        ps = PowerState(
            pv_production=7000.0,
            grid_export=2000.0,
            grid_import=0.0,
            load_power=5000.0,
            excess_power=2000.0,
            battery_soc=90.0,
            battery_power=0.0,
            ev_soc=45.0,
            timestamp=ts,
        )
        assert ps.ev_soc == 45.0

    def test_is_frozen(self):
        ts = _utcnow()
        ps = PowerState(
            pv_production=1000.0,
            grid_export=0.0,
            grid_import=0.0,
            load_power=1000.0,
            excess_power=0.0,
            battery_soc=None,
            battery_power=None,
            ev_soc=None,
            timestamp=ts,
        )
        with pytest.raises(Exception):  # frozen dataclass raises FrozenInstanceError
            ps.pv_production = 9999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestApplianceConfig
# ---------------------------------------------------------------------------

class TestApplianceConfig:
    def test_create_simple_switch(self):
        cfg = _make_appliance_config()
        assert cfg.id == "appliance_1"
        assert cfg.name == "Washing Machine"
        assert cfg.entity_id == "switch.washing_machine"
        assert cfg.priority == 100
        assert cfg.phases == 1
        assert cfg.nominal_power == 2000.0
        assert cfg.actual_power_entity is None
        assert cfg.dynamic_current is False
        assert cfg.current_entity is None
        assert cfg.ev_soc_entity is None
        assert cfg.ev_connected_entity is None
        assert cfg.is_big_consumer is False
        assert cfg.battery_max_discharge_override is None
        assert cfg.on_only is False
        assert cfg.min_daily_runtime is None
        assert cfg.max_daily_runtime is None
        assert cfg.schedule_deadline is None
        assert cfg.switch_interval == 300
        assert cfg.allow_grid_supplement is False
        assert cfg.max_grid_power is None
        # defaults for mutable runtime state
        assert cfg.override_active is False
        assert cfg.override_until is None

    def test_create_ev_charger_with_all_fields(self):
        deadline = time(7, 0)
        until = datetime(2026, 3, 23, 7, 0, 0, tzinfo=timezone.utc)
        cfg = ApplianceConfig(
            id="ev_charger_1",
            name="EV Charger",
            entity_id="switch.ev_charger",
            priority=10,
            phases=3,
            nominal_power=11000.0,
            actual_power_entity="sensor.ev_charger_power",
            dynamic_current=True,
            current_entity="number.ev_charger_current",
            min_current=6.0,
            max_current=16.0,
            ev_soc_entity="sensor.ev_soc",
            ev_connected_entity="binary_sensor.ev_connected",
            is_big_consumer=True,
            battery_max_discharge_override=0.0,
            on_only=False,
            min_daily_runtime=timedelta(hours=2),
            max_daily_runtime=timedelta(hours=8),
            schedule_deadline=deadline,
            switch_interval=60,
            allow_grid_supplement=True,
            max_grid_power=3000.0,
            override_active=True,
            override_until=until,
        )
        assert cfg.id == "ev_charger_1"
        assert cfg.phases == 3
        assert cfg.dynamic_current is True
        assert cfg.current_entity == "number.ev_charger_current"
        assert cfg.min_current == 6.0
        assert cfg.max_current == 16.0
        assert cfg.ev_soc_entity == "sensor.ev_soc"
        assert cfg.ev_connected_entity == "binary_sensor.ev_connected"
        assert cfg.is_big_consumer is True
        assert cfg.battery_max_discharge_override == 0.0
        assert cfg.min_daily_runtime == timedelta(hours=2)
        assert cfg.max_daily_runtime == timedelta(hours=8)
        assert cfg.schedule_deadline == deadline
        assert cfg.switch_interval == 60
        assert cfg.allow_grid_supplement is True
        assert cfg.max_grid_power == 3000.0
        assert cfg.override_active is True
        assert cfg.override_until == until

    def test_sorting_by_priority(self):
        """Lower priority number means higher priority (sorted first)."""
        cfgs = [
            _make_appliance_config(id="low", priority=500),
            _make_appliance_config(id="high", priority=10),
            _make_appliance_config(id="mid", priority=200),
        ]
        sorted_cfgs = sorted(cfgs, key=lambda c: c.priority)
        assert [c.id for c in sorted_cfgs] == ["high", "mid", "low"]

    def test_override_mutable(self):
        """ApplianceConfig is not frozen - override fields can be mutated."""
        cfg = _make_appliance_config()
        cfg.override_active = True
        cfg.override_until = _utcnow()
        assert cfg.override_active is True
        assert cfg.override_until is not None


# ---------------------------------------------------------------------------
# TestTariffWindow
# ---------------------------------------------------------------------------

class TestTariffWindow:
    def test_create_basic_window(self):
        start = datetime(2026, 3, 22, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 22, 11, 0, tzinfo=timezone.utc)
        w = TariffWindow(start=start, end=end, price=0.08, is_cheap=True)
        assert w.start == start
        assert w.end == end
        assert w.price == 0.08
        assert w.is_cheap is True

    def test_create_expensive_window(self):
        start = datetime(2026, 3, 22, 18, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 22, 20, 0, tzinfo=timezone.utc)
        w = TariffWindow(start=start, end=end, price=0.35, is_cheap=False)
        assert w.is_cheap is False
        assert w.price == 0.35

    def test_is_frozen(self):
        start = datetime(2026, 3, 22, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 22, 11, 0, tzinfo=timezone.utc)
        w = TariffWindow(start=start, end=end, price=0.10, is_cheap=False)
        with pytest.raises(Exception):
            w.price = 0.99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestTariffInfo
# ---------------------------------------------------------------------------

class TestTariffInfo:
    def test_create_and_net_savings(self):
        info = TariffInfo(
            current_price=0.30,
            feed_in_tariff=0.08,
            cheap_price_threshold=0.15,
            battery_charge_price_threshold=0.12,
        )
        assert info.current_price == 0.30
        assert info.feed_in_tariff == 0.08
        assert info.cheap_price_threshold == 0.15
        assert info.battery_charge_price_threshold == 0.12
        assert info.windows == []
        assert pytest.approx(info.net_savings_per_kwh) == 0.22

    def test_net_savings_negative_when_feed_in_exceeds_price(self):
        info = TariffInfo(
            current_price=0.05,
            feed_in_tariff=0.10,
            cheap_price_threshold=0.08,
            battery_charge_price_threshold=0.07,
        )
        assert pytest.approx(info.net_savings_per_kwh) == -0.05

    def test_with_windows(self):
        start = datetime(2026, 3, 22, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 22, 11, 0, tzinfo=timezone.utc)
        window = TariffWindow(start=start, end=end, price=0.08, is_cheap=True)
        info = TariffInfo(
            current_price=0.25,
            feed_in_tariff=0.07,
            cheap_price_threshold=0.12,
            battery_charge_price_threshold=0.10,
            windows=[window],
        )
        assert len(info.windows) == 1
        assert info.windows[0] is window


# ---------------------------------------------------------------------------
# TestControlDecision
# ---------------------------------------------------------------------------

class TestControlDecision:
    def test_create_on_decision(self):
        decision = ControlDecision(
            appliance_id="appliance_1",
            action=Action.ON,
            target_current=None,
            reason="excess_available",
            overrides_plan=False,
        )
        assert decision.appliance_id == "appliance_1"
        assert decision.action == Action.ON
        assert decision.target_current is None
        assert decision.reason == "excess_available"
        assert decision.overrides_plan is False

    def test_create_set_current_decision(self):
        decision = ControlDecision(
            appliance_id="ev_charger_1",
            action=Action.SET_CURRENT,
            target_current=10.0,
            reason="dynamic allocation",
            overrides_plan=True,
        )
        assert decision.action == Action.SET_CURRENT
        assert decision.target_current == 10.0
        assert decision.overrides_plan is True

    def test_create_off_decision(self):
        decision = ControlDecision(
            appliance_id="appliance_2",
            action=Action.OFF,
            target_current=None,
            reason="insufficient_excess",
            overrides_plan=False,
        )
        assert decision.action == Action.OFF

    def test_create_idle_decision(self):
        decision = ControlDecision(
            appliance_id="appliance_3",
            action=Action.IDLE,
            target_current=None,
            reason="switch_interval_not_elapsed",
            overrides_plan=False,
        )
        assert decision.action == Action.IDLE

    def test_is_frozen(self):
        decision = ControlDecision(
            appliance_id="a",
            action=Action.ON,
            target_current=None,
            reason="test",
            overrides_plan=False,
        )
        with pytest.raises(Exception):
            decision.action = Action.OFF  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestPlan
# ---------------------------------------------------------------------------

class TestPlan:
    def _make_battery_target(self) -> BatteryTarget:
        return BatteryTarget(
            target_soc=90.0,
            target_time=datetime(2026, 3, 23, 7, 0, tzinfo=timezone.utc),
            strategy=BatteryStrategy.BALANCED,
        )

    def test_create_empty_plan(self):
        now = _utcnow()
        bt = self._make_battery_target()
        plan = Plan(
            created_at=now,
            horizon=timedelta(hours=12),
            entries=[],
            battery_target=bt,
            confidence=0.8,
        )
        assert plan.created_at == now
        assert plan.horizon == timedelta(hours=12)
        assert plan.entries == []
        assert plan.battery_target is bt
        assert plan.confidence == 0.8

    def test_plan_with_entries(self):
        now = _utcnow()
        bt = self._make_battery_target()
        start = datetime(2026, 3, 22, 13, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 22, 14, 0, tzinfo=timezone.utc)
        window = TariffWindow(start=start, end=end, price=0.08, is_cheap=True)
        entry = PlanEntry(
            appliance_id="appliance_1",
            action=Action.ON,
            target_current=None,
            window=window,
            reason=PlanReason.CHEAP_TARIFF,
            priority=100,
        )
        plan = Plan(
            created_at=now,
            horizon=timedelta(hours=6),
            entries=[entry],
            battery_target=bt,
            confidence=0.9,
        )
        assert len(plan.entries) == 1
        assert plan.entries[0].action == Action.ON
        assert plan.entries[0].reason == PlanReason.CHEAP_TARIFF

    def test_plan_confidence_range(self):
        now = _utcnow()
        bt = self._make_battery_target()
        plan_low = Plan(created_at=now, horizon=timedelta(hours=1), entries=[], battery_target=bt, confidence=0.0)
        plan_high = Plan(created_at=now, horizon=timedelta(hours=1), entries=[], battery_target=bt, confidence=1.0)
        assert plan_low.confidence == 0.0
        assert plan_high.confidence == 1.0


# ---------------------------------------------------------------------------
# TestOptimizerResult
# ---------------------------------------------------------------------------

class TestOptimizerResult:
    def test_create_with_decisions_and_battery_discharge_action(self):
        d1 = ControlDecision(
            appliance_id="appliance_1",
            action=Action.ON,
            target_current=None,
            reason="excess_available",
            overrides_plan=False,
        )
        d2 = ControlDecision(
            appliance_id="ev_charger_1",
            action=Action.SET_CURRENT,
            target_current=8.0,
            reason="dynamic allocation",
            overrides_plan=False,
        )
        bda = BatteryDischargeAction(should_limit=True, max_discharge_watts=1500.0)
        result = OptimizerResult(decisions=[d1, d2], battery_discharge_action=bda)
        assert len(result.decisions) == 2
        assert result.decisions[0] is d1
        assert result.decisions[1] is d2
        assert result.battery_discharge_action.should_limit is True
        assert result.battery_discharge_action.max_discharge_watts == 1500.0

    def test_create_with_no_limit(self):
        bda = BatteryDischargeAction(should_limit=False)
        result = OptimizerResult(decisions=[], battery_discharge_action=bda)
        assert result.decisions == []
        assert result.battery_discharge_action.should_limit is False
        assert result.battery_discharge_action.max_discharge_watts is None

    def test_battery_discharge_action_is_frozen(self):
        bda = BatteryDischargeAction(should_limit=True, max_discharge_watts=2000.0)
        with pytest.raises(Exception):
            bda.should_limit = False  # type: ignore[misc]

    def test_optimizer_result_decisions_mutable(self):
        """OptimizerResult.decisions list itself is mutable."""
        bda = BatteryDischargeAction(should_limit=False)
        result = OptimizerResult(decisions=[], battery_discharge_action=bda)
        d = ControlDecision(
            appliance_id="a",
            action=Action.IDLE,
            target_current=None,
            reason="test",
            overrides_plan=False,
        )
        result.decisions.append(d)
        assert len(result.decisions) == 1


# ---------------------------------------------------------------------------
# TestEnums
# ---------------------------------------------------------------------------

class TestEnums:
    def test_action_values(self):
        assert Action.ON == "on"
        assert Action.OFF == "off"
        assert Action.SET_CURRENT == "set_current"
        assert Action.IDLE == "idle"

    def test_battery_strategy_values(self):
        assert BatteryStrategy.BATTERY_FIRST == "battery_first"
        assert BatteryStrategy.APPLIANCE_FIRST == "appliance_first"
        assert BatteryStrategy.BALANCED == "balanced"

    def test_plan_reason_values(self):
        assert PlanReason.EXCESS_AVAILABLE == "excess_available"
        assert PlanReason.EV_DISCONNECTED == "ev_disconnected"
        assert PlanReason.MAX_RUNTIME_REACHED == "max_runtime_reached"

    def test_action_is_str(self):
        """StrEnum members are strings and can be used as such."""
        assert isinstance(Action.ON, str)
        assert Action.ON.upper() == "ON"


# ---------------------------------------------------------------------------
# TestBatteryConfig
# ---------------------------------------------------------------------------

class TestBatteryConfig:
    def test_create_battery_config(self):
        cfg = BatteryConfig(
            capacity_kwh=10.0,
            max_discharge_entity="number.battery_max_discharge",
            max_discharge_default=5000.0,
            target_soc=90.0,
            target_time=time(7, 0),
            strategy=BatteryStrategy.BATTERY_FIRST,
            allow_grid_charging=False,
        )
        assert cfg.capacity_kwh == 10.0
        assert cfg.max_discharge_entity == "number.battery_max_discharge"
        assert cfg.max_discharge_default == 5000.0
        assert cfg.target_soc == 90.0
        assert cfg.target_time == time(7, 0)
        assert cfg.strategy == BatteryStrategy.BATTERY_FIRST
        assert cfg.allow_grid_charging is False

    def test_create_battery_config_no_entity(self):
        cfg = BatteryConfig(
            capacity_kwh=5.0,
            max_discharge_entity=None,
            max_discharge_default=None,
            target_soc=80.0,
            target_time=time(6, 30),
            strategy=BatteryStrategy.BALANCED,
            allow_grid_charging=True,
        )
        assert cfg.max_discharge_entity is None
        assert cfg.max_discharge_default is None
        assert cfg.allow_grid_charging is True

    def test_is_frozen(self):
        cfg = BatteryConfig(
            capacity_kwh=10.0,
            max_discharge_entity=None,
            max_discharge_default=None,
            target_soc=80.0,
            target_time=time(7, 0),
            strategy=BatteryStrategy.BALANCED,
            allow_grid_charging=False,
        )
        with pytest.raises(Exception):
            cfg.target_soc = 50.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestApplianceState
# ---------------------------------------------------------------------------

class TestApplianceState:
    def test_create_basic(self):
        state = ApplianceState(
            appliance_id="appliance_1",
            is_on=True,
            current_power=1800.0,
            current_amperage=None,
            runtime_today=timedelta(hours=1, minutes=30),
            energy_today=2.7,
            last_state_change=_utcnow(),
            ev_connected=None,
        )
        assert state.appliance_id == "appliance_1"
        assert state.is_on is True
        assert state.current_power == 1800.0
        assert state.current_amperage is None
        assert state.runtime_today == timedelta(hours=1, minutes=30)
        assert state.energy_today == 2.7
        assert state.ev_connected is None

    def test_create_ev_state(self):
        state = ApplianceState(
            appliance_id="ev_charger_1",
            is_on=True,
            current_power=7200.0,
            current_amperage=10.0,
            runtime_today=timedelta(hours=3),
            energy_today=21.6,
            last_state_change=_utcnow(),
            ev_connected=True,
        )
        assert state.current_amperage == 10.0
        assert state.ev_connected is True


# ---------------------------------------------------------------------------
# TestBatteryTarget
# ---------------------------------------------------------------------------

class TestBatteryTarget:
    def test_create(self):
        target_time = datetime(2026, 3, 23, 7, 0, tzinfo=timezone.utc)
        bt = BatteryTarget(
            target_soc=95.0,
            target_time=target_time,
            strategy=BatteryStrategy.BATTERY_FIRST,
        )
        assert bt.target_soc == 95.0
        assert bt.target_time == target_time
        assert bt.strategy == BatteryStrategy.BATTERY_FIRST

    def test_is_frozen(self):
        bt = BatteryTarget(
            target_soc=80.0,
            target_time=_utcnow(),
            strategy=BatteryStrategy.BALANCED,
        )
        with pytest.raises(Exception):
            bt.target_soc = 50.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TestBatteryChargeCurve
# ---------------------------------------------------------------------------

class TestBatteryChargeCurve:
    def test_setpoint_is_frozen(self):
        sp = BatteryChargeSetpoint(
            start=datetime(2026, 5, 5, 9, tzinfo=timezone.utc),
            end=datetime(2026, 5, 5, 10, tzinfo=timezone.utc),
            max_charge_w=1500,
        )
        with pytest.raises(Exception):
            sp.max_charge_w = 2000  # frozen dataclass

    def test_curve_holds_setpoints_and_telemetry(self):
        sp = BatteryChargeSetpoint(
            datetime(2026, 5, 5, 9, tzinfo=timezone.utc),
            datetime(2026, 5, 5, 10, tzinfo=timezone.utc),
            1500,
        )
        curve = BatteryChargeCurve(
            created_at=datetime(2026, 5, 5, 8, tzinfo=timezone.utc),
            setpoints=(sp,),
            expected_curtailment_kwh=4.5,
            headroom_kwh=3.0,
            fallback_reason=None,
        )
        assert curve.setpoints[0].max_charge_w == 1500
        assert curve.fallback_reason is None

    def test_curve_fallback_reason_string(self):
        curve = BatteryChargeCurve(
            created_at=datetime(2026, 5, 5, 8, tzinfo=timezone.utc),
            setpoints=(),
            expected_curtailment_kwh=0.0,
            headroom_kwh=0.0,
            fallback_reason="forecast_unavailable",
        )
        assert curve.fallback_reason == "forecast_unavailable"

    def test_plan_battery_charge_curve_defaults_to_none(self):
        plan = Plan(
            created_at=datetime(2026, 5, 5, 0, tzinfo=timezone.utc),
            horizon=timedelta(hours=24),
            entries=[],
            battery_target=BatteryTarget(
                target_soc=80.0,
                target_time=datetime(2026, 5, 5, 18, tzinfo=timezone.utc),
                strategy=BatteryStrategy.BALANCED,
            ),
            confidence=0.5,
        )
        assert plan.battery_charge_curve is None

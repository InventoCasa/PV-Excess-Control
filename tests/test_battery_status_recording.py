"""Long plans retain live detail and fit Home Assistant's recorded attributes."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest

from custom_components.pv_excess_control.sensor import PvExcessStatusSensor


def timeline(count=288):
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    rows = []
    for index in range(count):
        left = start + timedelta(minutes=10 * index)
        row = {"start": left.isoformat(), "end": (left + timedelta(minutes=10)).isoformat(),
               "action": ("charge", "hold", "self_consumption")[index % 3],
               "grid_charge_kwh": round((index % 17) / 100, 3) if index % 3 == 0 else 0.,
               "soc_end": round((index * 7.3) % 100, 1)}
        if row["action"] == "charge":
            row["grid_charge_available_at"] = (left + timedelta(seconds=30)).isoformat()
        rows.append(row)
    return rows


def sensor_for(diagnostics):
    coordinator = MagicMock()
    coordinator.config_entry.entry_id = "test_entry"
    coordinator.config_entry.data = {}
    coordinator.battery_control_diagnostics.return_value = diagnostics
    return PvExcessStatusSensor(coordinator)


def recorded(sensor):
    return {k: v for k, v in sensor.extra_state_attributes.items()
            if k not in sensor._unrecorded_attributes}


def test_full_two_day_plans_keep_actual_state_and_lossless_history():
    primary = timeline()
    lower = [{**row, "soc_end": max(0., row["soc_end"] - 3)} for row in timeline()]
    data = {"battery_plan": primary, "battery_pv_risk_plan": lower,
            "battery_control_state": "holding", "battery_control_reason": "economic_hold",
            "battery_grid_energy_kwh": 2.4, "battery_forecast_source_status": "available"}
    sensor = sensor_for(data)
    live = sensor.extra_state_attributes
    assert live["battery_plan"] == primary and live["battery_pv_risk_plan"] == lower
    history = recorded(sensor)
    assert len(json.dumps(history).encode()) < 16384
    assert history["battery_control_state"] == "holding"
    assert history["battery_control_reason"] == "economic_hold"
    from custom_components.pv_excess_control.battery_plan_archive import decode_battery_plan_archive
    restored = decode_battery_plan_archive(history["battery_plan_archive"])
    assert restored == {"primary": primary, "lower": lower}
    assert "battery_plan_archive" not in data  # no coordinator mutation


def test_metadata_budget_never_sacrifices_actual_state_for_optional_archive():
    sensor = sensor_for({"battery_plan": timeline(), "battery_pv_risk_plan": timeline(),
                         "battery_control_state": "charging", "other_metadata": "x" * 14500})
    history = recorded(sensor)
    assert len(json.dumps(history).encode()) < 16384
    assert history["battery_control_state"] == "charging"
    assert history["other_metadata"] == "x" * 14500
    assert history["battery_plan_archive"]["status"] == "unavailable"


def test_missing_plan_is_distinct_from_actual_zero_reading():
    sensor = sensor_for({"battery_plan": [], "battery_pv_risk_plan": [],
                         "battery_grid_energy_kwh": None, "battery_grid_charge_observed_power_w": 0.})
    history = recorded(sensor)
    assert history["battery_grid_energy_kwh"] is None
    assert history["battery_grid_charge_observed_power_w"] == 0.
    assert history["battery_plan_archive"]["status"] == "available"


def test_archive_envelope_cannot_displace_near_limit_actual_metadata():
    sensor = sensor_for({"battery_plan": timeline(), "battery_pv_risk_plan": timeline(),
                         "battery_control_state": "charging", "other_metadata": "x" * 16280})
    history = recorded(sensor)
    assert len(json.dumps(history).encode()) < 16384
    assert history["battery_control_state"] == "charging"
    assert history["other_metadata"] == "x" * 16280
    assert "battery_plan_archive" not in history


@pytest.mark.asyncio
async def test_home_assistant_recorder_persists_filtered_state_and_archive(hass):
    from homeassistant.core import Event, State
    from homeassistant.components.recorder.db_schema import StateAttributes
    from custom_components.pv_excess_control.battery_plan_archive import decode_battery_plan_archive
    sensor = sensor_for({"battery_plan": timeline(), "battery_pv_risk_plan": timeline(),
                         "battery_control_state": "self_consumption"})
    sensor.hass = hass
    sensor.entity_id = "sensor.test_battery_status"
    sensor.platform = SimpleNamespace(platform_name="sensor", config_entry=None)
    await sensor.async_internal_added_to_hass()
    attrs = sensor.extra_state_attributes
    unfiltered = State(sensor.entity_id, "ok", attrs)
    assert StateAttributes.shared_attrs_bytes_from_event(
        Event("state_changed", {"new_state": unfiltered}), None) == b"{}"
    state = State(sensor.entity_id, "ok", attrs, state_info=sensor._state_info)
    raw = StateAttributes.shared_attrs_bytes_from_event(Event("state_changed", {"new_state": state}), None)
    assert raw != b"{}" and len(raw) < 16384
    history = json.loads(raw)
    assert history["battery_control_state"] == "self_consumption"
    assert "battery_plan" not in history and "battery_pv_risk_plan" not in history
    assert decode_battery_plan_archive(history["battery_plan_archive"])["primary"] == timeline()

"""Battery planning configuration rejects unsafe or ambiguous inputs."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol

from custom_components.pv_excess_control.config_flow import (
    PvExcessControlConfigFlow,
    PvExcessControlOptionsFlow,
    _battery_schema,
    _validate_battery_section,
)


NUMERIC_LIMITS = {
    "battery_grid_target_soc": (80, 0, 100),
    "battery_roundtrip_efficiency": (0.85, 0.5, 1),
    "battery_pv_forecast_factor": (1, 0.1, 1),
    "battery_wear_cost_per_kwh": (0, 0, 1),
    "battery_soc_hysteresis": (2, 0, 20),
    "battery_input_max_age_seconds": (300, 30, 3600),
    "battery_forecast_max_age_seconds": (21600, 300, 172800),
}


def automatic_config():
    return {
        "auto_battery_grid_charge": True,
        "battery_grid_charge_power_w": 2500,
        "battery_max_charge_power_w": 5000,
        "inverter_force_charge_enable_entity": "input_select.charge",
        "inverter_force_charge_enable_engage_value": "Charge",
        "inverter_force_charge_enable_disengage_value": "Stop",
        "inverter_force_charge_enable_feedback_entity": "sensor.charge_command",
        "inverter_force_charge_mode_entity": "input_select.mode",
        "inverter_force_charge_mode_engage_value": "Forced",
        "inverter_force_charge_mode_disengage_value": "Self",
        "inverter_force_charge_mode_feedback_entity": "sensor.mode",
        "inverter_force_charge_power_entity": "input_number.power",
        "inverter_force_charge_power_feedback_entity": "sensor.power",
    }


@pytest.mark.parametrize("key,limits", NUMERIC_LIMITS.items())
def test_new_numeric_fields_are_visible_and_have_conservative_defaults(key, limits):
    keys = {marker.schema: marker for marker in _battery_schema().schema}
    assert key in keys
    assert keys[key].default() == limits[0]
    assert keys["auto_battery_grid_charge"].default() is False
    assert keys["battery_hold_verified"].default() is False


@pytest.mark.parametrize("key,limits", NUMERIC_LIMITS.items())
@pytest.mark.parametrize("kind", ["nan", "infinity", "below", "above"])
def test_planning_numbers_must_be_finite_and_within_bounds(key, limits, kind):
    _, lower, upper = limits
    value = {"nan": float("nan"), "infinity": float("inf"), "below": lower-0.1, "above": upper+0.1}[kind]
    with pytest.raises(vol.Invalid, match=key):
        _validate_battery_section({key: value})


@pytest.mark.parametrize("power", [float("nan"), float("inf"), -1])
def test_grid_power_invalid_even_when_automation_is_off(power):
    with pytest.raises(vol.Invalid, match="battery_grid_charge_power_w"):
        _validate_battery_section({"battery_grid_charge_power_w": power})


def test_automatic_grid_charge_rejects_zero_power():
    data = automatic_config()
    data["battery_grid_charge_power_w"] = 0
    with pytest.raises(vol.Invalid, match="battery_grid_charge_power_w"):
        _validate_battery_section(data)


@pytest.mark.parametrize("kind", ["enable", "mode", "power"])
@pytest.mark.parametrize("replacement", [None, "input_number.feedback", "same"])
def test_automatic_helper_mappings_require_distinct_physical_feedback(kind, replacement):
    data = automatic_config()
    key = f"inverter_force_charge_{kind}_feedback_entity"
    if replacement is None:
        del data[key]
    else:
        data[key] = data[f"inverter_force_charge_{kind}_entity"] if replacement == "same" else replacement
    with pytest.raises(vol.Invalid, match="feedback"):
        _validate_battery_section(data)


def test_automatic_complete_mappings_are_valid():
    _validate_battery_section(automatic_config())


def test_direct_device_entities_can_acknowledge_their_own_states():
    data = automatic_config()
    for kind, domain in [("enable", "switch"), ("mode", "select"), ("power", "number")]:
        data[f"inverter_force_charge_{kind}_entity"] = f"{domain}.{kind}"
        data.pop(f"inverter_force_charge_{kind}_feedback_entity")
    _validate_battery_section(data)


@pytest.mark.parametrize("profile", [[500]*23, [500]*25, [500]*23+[float("nan")], [500]*23+[-1], [500]*23+[True], '{broken', 'null', {'midnight':500}])
def test_load_profile_rejects_incomplete_or_invalid_hourly_values(profile):
    with pytest.raises(vol.Invalid, match="battery_load_profile_w"):
        _validate_battery_section({"battery_load_profile_w": profile})


def test_load_profile_json_is_stored_as_24_numeric_hour_values():
    data = {"battery_load_profile_w": json.dumps([500]*24)}
    _validate_battery_section(data)
    assert data["battery_load_profile_w"] == [500.0]*24


def test_blank_profile_removes_explicit_profile():
    data = {"battery_load_profile_w": " "}
    _validate_battery_section(data)
    assert "battery_load_profile_w" not in data


def test_profile_form_displays_existing_list_as_json_text():
    schema = _battery_schema({"battery_load_profile_w": [600]*24})
    marker = next(k for k in schema.schema if k.schema == "battery_load_profile_w")
    assert json.loads(marker.description["suggested_value"]) == [600]*24


@pytest.mark.parametrize("data", [{"battery_grid_target_soc":5,"min_battery_soc":10}, {"battery_target_soc":5,"min_battery_soc":10}])
def test_charge_target_cannot_be_below_configured_reserve(data):
    with pytest.raises(vol.Invalid, match="soc"):
        _validate_battery_section(data)


def test_hold_requires_complete_verified_mapping():
    with pytest.raises(vol.Invalid, match="battery_hold"):
        _validate_battery_section({"battery_hold_verified": True})


def test_verified_hold_helper_requires_physical_feedback():
    data = {"battery_hold_verified":True,"battery_hold_entity":"input_boolean.hold", "battery_hold_engage_value":"on", "battery_hold_release_value":"off"}
    with pytest.raises(vol.Invalid, match="feedback"):
        _validate_battery_section(data)
    data["battery_hold_feedback_entity"] = "binary_sensor.hold"
    _validate_battery_section(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_class", [PvExcessControlConfigFlow, PvExcessControlOptionsFlow])
async def test_form_submission_parses_profile_and_preserves_unrelated_values(flow_class):
    flow = flow_class()
    flow.data = {"battery_target_soc":100,"_grid_charge_engaged":True,"unrelated":"keep"}
    flow.async_step_settings = AsyncMock(return_value={"type":"form", "step_id":"settings"})
    result = await flow.async_step_battery({"battery_target_soc":100,"battery_load_profile_w":json.dumps([700]*24)})
    assert result["step_id"] == "settings"
    assert flow.data["battery_load_profile_w"] == [700]*24
    assert flow.data["_grid_charge_engaged"] is True
    assert flow.data["unrelated"] == "keep"


@pytest.mark.asyncio
async def test_options_can_clear_new_optional_fields_without_removing_runtime_state():
    flow = PvExcessControlOptionsFlow()
    flow.data = {"battery_load_profile_w":[700]*24,"battery_hold_entity":"switch.hold","battery_hold_feedback_entity":"sensor.hold", "inverter_force_charge_enable_feedback_entity":"sensor.command", "_grid_charge_engaged":True,"unrelated":"keep"}
    flow.async_step_settings = AsyncMock(return_value={"step_id":"settings"})
    result = await flow.async_step_battery({"battery_target_soc":100,"battery_hold_verified":False})
    assert result["step_id"] == "settings"
    assert "battery_load_profile_w" not in flow.data
    assert "battery_hold_entity" not in flow.data
    assert "battery_hold_feedback_entity" not in flow.data
    assert "inverter_force_charge_enable_feedback_entity" not in flow.data
    assert flow.data["_grid_charge_engaged"] is True
    assert flow.data["unrelated"] == "keep"


@pytest.mark.asyncio
async def test_invalid_profile_is_reported_on_its_field_and_retained_for_editing():
    flow = PvExcessControlConfigFlow()
    result = await flow.async_step_battery({"battery_target_soc":100,"battery_load_profile_w":"[1, 2]"})
    assert result["errors"] == {"battery_load_profile_w":"invalid_battery_load_profile"}
    marker = next(k for k in result["data_schema"].schema if k.schema == "battery_load_profile_w")
    assert marker.description["suggested_value"] == "[1, 2]"


def test_planning_fields_have_labels_and_help_in_both_languages():
    from pathlib import Path
    component = Path(__file__).parents[1] / "custom_components/pv_excess_control"
    new_fields = set(NUMERIC_LIMITS) | {"battery_load_profile_w","battery_hold_verified","battery_hold_entity","battery_hold_engage_value","battery_hold_release_value","battery_hold_feedback_entity","battery_hold_feedback_engage_value","battery_hold_feedback_release_value","inverter_force_charge_enable_feedback_entity","inverter_force_charge_mode_feedback_entity","inverter_force_charge_power_feedback_entity"}
    for filename in ["strings.json","translations/en.json","translations/de.json"]:
        data = json.loads((component/filename).read_text())
        for section in ["config", "options"]:
            battery = data[section]["step"]["battery"]
            assert new_fields <= battery["data"].keys()
            assert new_fields <= battery["data_description"].keys()
            assert "invalid_battery_load_profile" in data[section]["error"]


def test_present_capacity_must_be_positive():
    with pytest.raises(vol.Invalid, match="battery_capacity"):
        _validate_battery_section({"battery_capacity":0})


def test_hold_feedback_values_cannot_be_the_same():
    with pytest.raises(vol.Invalid, match="battery_hold"):
        _validate_battery_section({"battery_hold_verified":True,"battery_hold_entity":"switch.hold","battery_hold_engage_value":"on","battery_hold_release_value":"off","battery_hold_feedback_engage_value":"ready","battery_hold_feedback_release_value":"ready"})


@pytest.mark.asyncio
async def test_options_save_preserves_current_ownership_and_runtime_state(monkeypatch):
    from unittest.mock import MagicMock
    entry = MagicMock()
    entry.data = {
        "inverter_type":"hybrid", "battery_grid_target_soc":80,
        "_grid_charge_engaged":False, "_grid_charge_cleanup_pending":False,
        "_battery_hold_cleanup_pending":False, "control_enabled":True,
        "force_charge":False,"disabled_appliances":[],"_pending_stop_appliances":[],
        "obsolete_runtime_key":"removed meanwhile",
    }
    flow = PvExcessControlOptionsFlow()
    flow.hass = MagicMock()
    monkeypatch.setattr(PvExcessControlOptionsFlow, "config_entry", property(lambda self: entry))
    flow.data = dict(entry.data)
    flow.async_create_entry = MagicMock(return_value={"type":"create_entry"})
    # The options page remains open while controls and cleanup ownership change.
    entry.data = {
        **entry.data,
        "_grid_charge_engaged":True, "_grid_charge_cleanup_pending":True,
        "_battery_hold_cleanup_pending":True, "control_enabled":False,
        "force_charge":True,"disabled_appliances":["load"],"_pending_stop_appliances":["load"],
        "new_runtime_key":{"hold":"pending"},
    }
    entry.data.pop("obsolete_runtime_key")
    flow.data["battery_grid_target_soc"] = 65
    await flow.async_step_settings({"controller_interval":"30", "planner_interval":"900"})
    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    for key, value in entry.data.items():
        if key not in {"inverter_type", "battery_grid_target_soc"}:
            assert saved[key] == value, key
    assert saved["battery_grid_target_soc"] == 65
    assert "obsolete_runtime_key" not in saved


@pytest.mark.asyncio
async def test_options_save_does_not_restore_cleared_profile_or_feedback(monkeypatch):
    from unittest.mock import MagicMock
    entry = MagicMock()
    entry.data = {"inverter_type":"hybrid", "battery_load_profile_w":[500]*24,
                  "inverter_force_charge_enable_feedback_entity":"sensor.old", "_grid_charge_engaged":True}
    flow = PvExcessControlOptionsFlow()
    flow.hass = MagicMock()
    monkeypatch.setattr(PvExcessControlOptionsFlow, "config_entry", property(lambda self: entry))
    flow.data = {"inverter_type":"hybrid", "_grid_charge_engaged":False}
    flow.async_create_entry = MagicMock(return_value={"type":"create_entry"})
    await flow.async_step_settings({"controller_interval":"30", "planner_interval":"900"})
    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert "battery_load_profile_w" not in saved
    assert "inverter_force_charge_enable_feedback_entity" not in saved
    assert saved["_grid_charge_engaged"] is True


@pytest.mark.parametrize("key", ["battery_hold_feedback_engage_value", "battery_hold_feedback_release_value", "battery_hold_feedback_entity", "inverter_force_charge_enable_feedback_entity", "battery_load_profile_w"])
def test_empty_optional_text_is_removed_before_controller_mapping(key):
    data = {"battery_hold_verified":True,"battery_hold_entity":"switch.hold",
            "battery_hold_engage_value":"on","battery_hold_release_value":"off", key:"  "}
    _validate_battery_section(data)
    assert key not in data


def test_blank_hold_feedback_overrides_allow_durable_mapping():
    from dataclasses import asdict
    from custom_components.pv_excess_control.battery_journal import _decode_config
    from custom_components.pv_excess_control.models import InverterGridChargeConfig
    data = {"battery_hold_verified":True,"battery_hold_entity":"switch.hold",
            "battery_hold_engage_value":"on","battery_hold_release_value":"off",
            "battery_hold_feedback_engage_value":"", "battery_hold_feedback_release_value":""}
    _validate_battery_section(data)
    config = InverterGridChargeConfig(
        enable_entity_id=data["battery_hold_entity"],
        enable_engage_value=data["battery_hold_engage_value"],
        enable_disengage_value=data["battery_hold_release_value"],
        enable_feedback_engage_value=data.get("battery_hold_feedback_engage_value"),
        enable_feedback_disengage_value=data.get("battery_hold_feedback_release_value"),
    )
    assert _decode_config(asdict(config)) == config



def test_automatic_planning_requires_adjustable_charge_power():
    data = automatic_config()
    data.pop("inverter_force_charge_power_entity")
    with pytest.raises(vol.Invalid, match="inverter_force_charge_power_entity"):
        _validate_battery_section(data)


@pytest.mark.parametrize("power", [None, 0, -1, float("nan"), float("inf")])
def test_automatic_planning_requires_native_solar_charge_limit_without_dynamic_cap(power):
    data = automatic_config()
    data["dynamic_battery_charge_enabled"] = False
    if power is None:
        data.pop("battery_max_charge_power_w")
    else:
        data["battery_max_charge_power_w"] = power
    with pytest.raises(vol.Invalid, match="battery_max_charge_power_w"):
        _validate_battery_section(data)


def test_manual_only_switch_mapping_does_not_require_planner_power_limits():
    _validate_battery_section({"auto_battery_grid_charge":False,
        "inverter_force_charge_enable_entity":"switch.charge",
        "inverter_force_charge_enable_engage_value":"on",
        "inverter_force_charge_enable_disengage_value":"off"})

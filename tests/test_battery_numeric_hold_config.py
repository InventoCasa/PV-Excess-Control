"""Numeric discharge holds preserve native units and require real feedback."""
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from custom_components.pv_excess_control.config_flow import (
    PvExcessControlConfigFlow,
    PvExcessControlOptionsFlow,
    _battery_schema,
    _validate_battery_section,
)


def numeric_hold(**changes):
    data = {
        "battery_hold_verified": True,
        "battery_hold_entity": "input_number.discharge_limit",
        "battery_hold_engage_value": "10",
        "battery_hold_release_value": "5000",
        "battery_hold_feedback_entity": "sensor.discharge_limit",
    }
    data.update(changes)
    return data


@pytest.mark.parametrize("domain", ["number", "input_number"])
def test_hold_selector_offers_numeric_commands(domain):
    selector = next(value for key, value in _battery_schema().schema.items()
                    if key.schema == "battery_hold_entity")
    assert domain in selector.config["domain"]


@pytest.mark.parametrize("key", [
    "battery_hold_engage_value", "battery_hold_release_value",
    "battery_hold_feedback_engage_value", "battery_hold_feedback_release_value",
])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e9999", "on", True, [], {}, -1, -0.01, "-0.01"])
def test_numeric_hold_rejects_nonfinite_negative_or_nonnumeric_values(key, value):
    with pytest.raises(vol.Invalid) as exc:
        _validate_battery_section(numeric_hold(**{key: value}))
    assert exc.value.path == [key]


@pytest.mark.parametrize("engage,release", [("10", "10.0"), ("0.01", "1e-2"), (10, "10.0")])
def test_numeric_command_values_must_differ_numerically(engage, release):
    with pytest.raises(vol.Invalid) as exc:
        _validate_battery_section(numeric_hold(
            battery_hold_engage_value=engage, battery_hold_release_value=release,
        ))
    assert exc.value.path == ["battery_hold_engage_value"]


@pytest.mark.parametrize("engage,release", [("10", "10.0"), ("0.01", "1e-2"), (0, "0.0")])
def test_numeric_feedback_values_must_differ_numerically(engage, release):
    with pytest.raises(vol.Invalid) as exc:
        _validate_battery_section(numeric_hold(
            battery_hold_feedback_engage_value=engage,
            battery_hold_feedback_release_value=release,
        ))
    assert exc.value.path == ["battery_hold_feedback_engage_value"]


@pytest.mark.parametrize("feedback", [None, "input_number.discharge_limit", "input_number.other"])
def test_numeric_helper_requires_distinct_physical_feedback(feedback):
    with pytest.raises(vol.Invalid) as exc:
        _validate_battery_section(numeric_hold(battery_hold_feedback_entity=feedback))
    assert exc.value.path == ["battery_hold_feedback_entity"]


def test_direct_number_can_use_its_own_physical_readback():
    data = numeric_hold(battery_hold_entity="number.discharge_limit")
    data.pop("battery_hold_feedback_entity")
    _validate_battery_section(data)


@pytest.mark.parametrize("engage", ["0", 0, 0.0])
def test_zero_is_a_valid_explicit_numeric_command(engage):
    data = numeric_hold(battery_hold_engage_value=engage)
    _validate_battery_section(data)
    assert float(data["battery_hold_engage_value"]) == 0


def test_explicit_feedback_uses_feedback_entity_units_without_conversion():
    data = numeric_hold(
        battery_hold_engage_value="0.01", battery_hold_release_value="5",
        battery_hold_feedback_engage_value="10", battery_hold_feedback_release_value="5000",
    )
    before = deepcopy(data)
    _validate_battery_section(data)
    assert data == before


def test_numeric_literals_are_saved_as_durable_text_without_unit_conversion():
    data = numeric_hold(
        battery_hold_engage_value=0, battery_hold_release_value=5,
        battery_hold_feedback_engage_value=0.0, battery_hold_feedback_release_value=5000.0,
    )
    _validate_battery_section(data)
    assert data["battery_hold_engage_value"] == "0"
    assert data["battery_hold_release_value"] == "5"
    assert data["battery_hold_feedback_engage_value"] == "0.0"
    assert data["battery_hold_feedback_release_value"] == "5000.0"


def test_omitted_feedback_values_use_native_command_values():
    data = numeric_hold(battery_hold_entity="number.discharge_limit",
                        battery_hold_engage_value="0.01", battery_hold_release_value="5")
    data.pop("battery_hold_feedback_entity")
    _validate_battery_section(data)
    assert "battery_hold_feedback_engage_value" not in data
    assert "battery_hold_feedback_release_value" not in data


@pytest.mark.parametrize("entity,engage,release,feedback", [
    ("switch.hold", "on", "off", None),
    ("select.hold", "10", "10.0", None),
    ("input_select.hold", "Pause", "Normal", "sensor.hold_state"),
    ("input_boolean.hold", "on", "off", "binary_sensor.hold_state"),
])
def test_existing_state_based_mappings_keep_their_string_semantics(entity, engage, release, feedback):
    data = numeric_hold(battery_hold_entity=entity, battery_hold_engage_value=engage,
                        battery_hold_release_value=release)
    if feedback is None:
        data.pop("battery_hold_feedback_entity")
    else:
        data["battery_hold_feedback_entity"] = feedback
    _validate_battery_section(data)
    assert data["battery_hold_engage_value"] == engage
    assert data["battery_hold_release_value"] == release


@pytest.mark.asyncio
@pytest.mark.parametrize("flow_type", [PvExcessControlConfigFlow, PvExcessControlOptionsFlow])
async def test_numeric_hold_error_stays_on_field_and_preserves_user_input(flow_type):
    flow = flow_type()
    flow.data = {}
    flow.async_step_settings = AsyncMock(return_value={"step_id": "settings"})
    data = numeric_hold(battery_hold_release_value="10.0")
    result = await flow.async_step_battery(data)
    assert result["step_id"] == "battery"
    assert result["errors"] == {"battery_hold_engage_value": "invalid_battery_hold"}
    marker = next(key for key in result["data_schema"].schema
                  if key.schema == "battery_hold_release_value")
    assert marker.description["suggested_value"] == "10.0"


@pytest.mark.asyncio
async def test_options_save_and_reopen_preserve_numeric_units_and_current_runtime(monkeypatch):
    entry = MagicMock()
    entry.data = {
        "inverter_type": "hybrid", "battery_target_soc": 100,
        "battery_grid_charge_power_w": 5000, "battery_max_charge_power_w": 6000,
        "battery_load_profile_w": [500.0] * 24,
        "battery_hold_verified": False, "_grid_charge_engaged": False,
        "disabled_appliances": ["independent_load"],
    }
    entry.subentries = {"existing_load": MagicMock(data={"daily_runtime": 123})}
    flow = PvExcessControlOptionsFlow()
    flow.hass = MagicMock()
    monkeypatch.setattr(PvExcessControlOptionsFlow, "config_entry", property(lambda self: entry))
    flow.data = deepcopy(entry.data)
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    mapping = numeric_hold(
        battery_hold_engage_value="0.01", battery_hold_release_value="5",
        battery_hold_feedback_engage_value="10", battery_hold_feedback_release_value="5000",
        battery_hold_reassert_entity="automation.write_discharge_limit",
    )
    battery_input = {key: value for key, value in entry.data.items()
                     if key.startswith("battery_")}
    result = await flow.async_step_battery({**battery_input, **mapping})
    assert result["step_id"] == "settings"
    entry.data["_grid_charge_engaged"] = True
    entry.data["_battery_hold_cleanup_pending"] = True
    await flow.async_step_settings({"controller_interval": "30", "planner_interval": "900"})
    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    for key, value in mapping.items():
        assert saved[key] == value
    assert saved["battery_grid_charge_power_w"] == 5000
    assert saved["battery_load_profile_w"] == [500.0] * 24
    assert saved["_grid_charge_engaged"] is True
    assert saved["_battery_hold_cleanup_pending"] is True
    assert saved["disabled_appliances"] == ["independent_load"]
    assert entry.subentries["existing_load"].data["daily_runtime"] == 123
    reopened = _battery_schema(saved)
    fields = {key.schema: key for key in reopened.schema}
    for key in mapping.keys() - {"battery_hold_verified"}:
        assert fields[key].description["suggested_value"] == mapping[key]


def test_reassert_bridge_is_optional_and_only_offers_automations():
    fields = {key.schema: (key, value) for key, value in _battery_schema().schema.items()}
    assert "battery_hold_reassert_entity" in fields
    marker, selector = fields["battery_hold_reassert_entity"]
    assert isinstance(marker, vol.Optional)
    assert selector.config["domain"] == ["automation"]
    assert marker.description["suggested_value"] is None


@pytest.mark.parametrize("entity", ["script.retry", "input_boolean.retry", "automation.", "automation.invalid.extra", True, 4])
def test_reassert_bridge_rejects_invalid_automation_entities(entity):
    with pytest.raises(vol.Invalid) as exc:
        _validate_battery_section(numeric_hold(battery_hold_reassert_entity=entity))
    assert exc.value.path == ["battery_hold_reassert_entity"]


@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.parametrize("entity", ["switch.hold", "select.hold", "input_boolean.hold", "input_select.hold"])
def test_reassert_bridge_is_only_for_numeric_hold_commands(entity, verified):
    with pytest.raises(vol.Invalid) as exc:
        _validate_battery_section(numeric_hold(
            battery_hold_entity=entity, battery_hold_verified=verified,
            battery_hold_reassert_entity="automation.write_discharge_limit",
        ))
    assert exc.value.path == ["battery_hold_reassert_entity"]


@pytest.mark.parametrize("feedback", [None, "number.discharge_limit", "input_number.feedback"])
def test_reassert_bridge_requires_separate_physical_feedback_even_for_direct_numbers(feedback):
    with pytest.raises(vol.Invalid) as exc:
        _validate_battery_section(numeric_hold(
            battery_hold_entity="number.discharge_limit",
            battery_hold_feedback_entity=feedback,
            battery_hold_reassert_entity="automation.write_discharge_limit",
        ))
    assert exc.value.path == ["battery_hold_feedback_entity"]


@pytest.mark.parametrize("verified", [True, False])
def test_reassert_bridge_preserves_explicit_physical_numeric_mapping(verified):
    data = numeric_hold(battery_hold_verified=verified,
                        battery_hold_reassert_entity="automation.write_discharge_limit")
    before = deepcopy(data)
    _validate_battery_section(data)
    assert data == before


def test_blank_reassert_bridge_is_removed_and_default_mapping_is_unchanged():
    data = numeric_hold(battery_hold_reassert_entity="  ")
    _validate_battery_section(data)
    assert "battery_hold_reassert_entity" not in data


@pytest.mark.asyncio
async def test_options_can_remove_reassert_bridge_without_resurrecting_old_configuration(monkeypatch):
    entry = MagicMock()
    entry.data = {"inverter_type": "hybrid", **numeric_hold(
        battery_hold_reassert_entity="automation.write_discharge_limit",
    ), "_battery_hold_cleanup_pending": True}
    flow = PvExcessControlOptionsFlow()
    flow.hass = MagicMock()
    monkeypatch.setattr(PvExcessControlOptionsFlow, "config_entry", property(lambda self: entry))
    flow.data = deepcopy(entry.data)
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    await flow.async_step_battery(numeric_hold())
    assert "battery_hold_reassert_entity" not in flow.data
    await flow.async_step_settings({"controller_interval": "30", "planner_interval": "900"})
    saved = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert "battery_hold_reassert_entity" not in saved
    assert saved["_battery_hold_cleanup_pending"] is True


def test_reassert_bridge_has_labels_and_help_in_both_languages():
    import json
    from pathlib import Path

    component = Path(__file__).parents[1] / "custom_components/pv_excess_control"
    for filename in ["strings.json", "translations/en.json", "translations/de.json"]:
        data = json.loads((component / filename).read_text())
        for section in ["config", "options"]:
            battery = data[section]["step"]["battery"]
            assert battery["data"]["battery_hold_reassert_entity"]
            assert battery["data_description"]["battery_hold_reassert_entity"]


@pytest.mark.parametrize("key,value", [
    ("battery_hold_engage_value", None),
    ("battery_hold_release_value", None),
    ("battery_hold_feedback_entity", None),
    ("battery_hold_engage_value", "nan"),
    ("battery_hold_engage_value", "-1"),
    ("battery_hold_release_value", "10.0"),
])
def test_shared_numeric_draft_requires_a_complete_valid_mapping(key, value):
    data = numeric_hold(battery_hold_verified=False,
                        battery_max_discharge_entity="input_number.discharge_limit",
                        **{key: value})
    with pytest.raises(vol.Invalid):
        _validate_battery_section(data)


def test_valid_shared_numeric_mapping_does_not_enable_unverified_economic_hold():
    data = numeric_hold(battery_hold_verified=False,
                        battery_max_discharge_entity="input_number.discharge_limit")
    _validate_battery_section(data)
    assert data["battery_hold_verified"] is False
    assert data["battery_hold_engage_value"] == "10"
    assert data["battery_hold_feedback_entity"] == "sensor.discharge_limit"


@pytest.mark.parametrize("hold_entity", [None, "number.other", "select.hold", "switch.hold"])
def test_unrelated_or_removed_hold_draft_can_still_be_incomplete(hold_entity):
    data = {
        "battery_hold_verified": False,
        "battery_max_discharge_entity": "input_number.discharge_limit",
    }
    if hold_entity is not None:
        data["battery_hold_entity"] = hold_entity
    _validate_battery_section(data)
    assert data["battery_hold_verified"] is False

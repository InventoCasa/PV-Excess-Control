"""Current-command delivery, cadence and negative-price regression tests."""

from unittest.mock import MagicMock

import pytest

from tests.test_init import _make_coordinator, _make_config_entry, MockState
from custom_components.pv_excess_control.models import (
    Action,
    ControlDecision,
    OptimizerResult,
    BatteryDischargeAction,
)


def ev_coordinator(connected="on", current="6", on="on"):
    sub = MagicMock()
    sub.data = {
        "appliance_entity": "switch.ev",
        "appliance_name": "EV",
        "nominal_power": 3680,
        "dynamic_current": True,
        "current_entity": "number.amps",
        "min_current": 6,
        "max_current": 16,
        "ev_connected_entity": "binary_sensor.connected",
        "current_update_interval": 300,
        "current_min_change": 1,
    }
    return _make_coordinator(
        entry=_make_config_entry(subentries={"ev": sub}),
        states={
            "switch.ev": MockState(on),
            "number.amps": MockState(current),
            "binary_sensor.connected": MockState(connected),
        },
    )


def result(amps):
    return OptimizerResult(
        [ControlDecision("ev", Action.SET_CURRENT, amps, "test", False)],
        BatteryDischargeAction(False),
    )


async def test_confirmed_current_is_not_rewritten_after_reload():
    coord = ev_coordinator()
    await coord._apply_decisions(result(6))
    assert coord.hass.services.calls == []


async def test_current_already_set_off_charger_only_needs_turn_on():
    coord = ev_coordinator(on="off")
    await coord._apply_decisions(result(6))
    assert coord.hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.ev"})
    ]


async def test_disconnected_override_presets_once_without_turning_on():
    coord = ev_coordinator(connected="off", on="off")
    coord.appliance_overrides["ev"] = True
    await coord._apply_decisions(result(16))
    await coord._apply_decisions(result(16))
    assert coord.hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.amps", "value": 16})
    ]


async def test_successful_write_exposes_elapsed_time_to_optimizer():
    coord = ev_coordinator()
    await coord._apply_decisions(result(8))
    states = coord._get_appliance_states(coord._get_appliance_configs())
    assert states["ev"].seconds_since_current_change is not None
    assert 0 <= states["ev"].seconds_since_current_change < 5
    cfg = coord._get_appliance_configs()[0]
    assert cfg.current_update_interval == 300 and cfg.current_min_change == 1


@pytest.mark.parametrize(
    "field", ["cheap_price_threshold", "battery_charge_price_threshold"]
)
def test_negative_price_threshold_accepted_by_energy_schema(field):
    from custom_components.pv_excess_control.config_flow import _energy_schema

    schema = _energy_schema("none")
    assert schema({"tariff_provider": "none", field: -0.05})[field] == -0.05


def test_negative_appliance_price_accepted():
    from custom_components.pv_excess_control.config_flow import (
        _appliance_constraints_schema,
    )

    assert (
        _appliance_constraints_schema()({"cheap_price_threshold": -0.05})[
            "cheap_price_threshold"
        ]
        == -0.05
    )


@pytest.mark.parametrize("observed", ["6", "unavailable"])
async def test_unconfirmed_write_is_retried_after_confirmation_grace(observed):
    from unittest.mock import patch

    coord = ev_coordinator(current=observed)
    with patch(
        "custom_components.pv_excess_control.coordinator._time.monotonic",
        return_value=1000,
    ):
        await coord._apply_decisions(result(8))
    with patch(
        "custom_components.pv_excess_control.coordinator._time.monotonic",
        return_value=1100,
    ):
        await coord._apply_decisions(result(8))
    assert len(coord.hass.services.calls) == 1
    with patch(
        "custom_components.pv_excess_control.coordinator._time.monotonic",
        return_value=1301,
    ):
        await coord._apply_decisions(result(8))
    assert len(coord.hass.services.calls) == 2


async def test_reduction_is_delivered_during_confirmation_grace():
    coord = ev_coordinator()
    await coord._apply_decisions(result(10))
    await coord._apply_decisions(result(7))
    assert [c[2]["value"] for c in coord.hass.services.calls] == [10, 7]


async def test_delayed_readback_cannot_free_unrealized_power():
    from dataclasses import replace
    from tests.test_block3_optimizer import _run
    from tests.test_optimizer import _make_appliance, _make_state

    coord = ev_coordinator()
    await coord._apply_decisions(result(8))
    configs = coord._get_appliance_configs()
    state = coord._get_appliance_states(configs)["ev"]
    assert state.current_amperage == 8
    state = replace(state, current_power=1840)
    lower = _make_appliance(id="lower", nominal_power=700, priority=999)
    decisions = _run([configs[0], lower], [state, _make_state(id="lower")], 460)
    assert next(d for d in decisions if d.appliance_id == "lower").action != Action.ON
    # A real reduction to the stale reported value must still be delivered.
    await coord._apply_decisions(result(6))
    assert coord.hass.services.calls[-1][2]["value"] == 6
    assert len(coord.hass.services.calls) == 2


async def test_out_of_range_readback_is_repaired_during_grace():
    coord = ev_coordinator()
    await coord._apply_decisions(result(8))
    coord.hass.states._states["number.amps"] = MockState("18")
    assert (
        coord._get_appliance_states(coord._get_appliance_configs())[
            "ev"
        ].current_amperage
        == 18
    )
    await coord._apply_decisions(result(8))
    assert len(coord.hass.services.calls) == 2

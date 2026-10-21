"""Lossless bounded storage of published battery plans in Recorder attributes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
import importlib
import json
import random
import zlib

import pytest


def codec():
    name = 'custom_components.pv_excess_control.battery_plan_archive'
    assert importlib.util.find_spec(name) is not None, 'battery plan archive is not implemented'
    return importlib.import_module(name)


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())


def plan(count=192, seed=17):
    randomizer = random.Random(seed)
    start = datetime(2034, 6, 7, microsecond=123456, tzinfo=timezone.utc)
    width = timedelta(hours=48) / count
    rows = []
    for index in range(count):
        left = start + width * index
        action = ('self_consumption', 'hold', 'charge')[index % 3]
        row = {'start': left.isoformat(), 'end': (left + width).isoformat(),
               'action': action, 'grid_charge_kwh': round(randomizer.random(), 3) if action == 'charge' else 0.,
               'soc_end': round(randomizer.random() * 100, 1)}
        if action == 'charge':
            row['grid_charge_available_at'] = (left + timedelta(seconds=30)).isoformat()
        rows.append(row)
    return rows


@pytest.mark.parametrize('count', [192, 288])
def test_two_full_48h_plans_roundtrip_under_default_attribute_budget(count):
    primary, lower = plan(count), plan(count, 71)
    original = deepcopy((primary, lower))
    archive = codec().encode_battery_plan_archive(primary, lower)
    assert archive['status'] == 'available'
    assert archive['included'] == ['primary', 'lower']
    assert size(archive) <= 12288
    assert codec().decode_battery_plan_archive(archive) == {'primary': primary, 'lower': lower}
    assert (primary, lower) == original


def test_preserves_primary_with_explicit_lower_omission_when_budget_is_tight():
    primary, lower = plan(), plan(seed=99)
    budget = size(codec().encode_battery_plan_archive(primary)) + 100
    archive = codec().encode_battery_plan_archive(primary, lower, max_bytes=budget)
    assert archive['status'] == 'partial' and archive['reason'] == 'size_limit'
    assert archive['included'] == ['primary'] and size(archive) <= budget
    assert codec().decode_battery_plan_archive(archive) == {'primary': primary}


@pytest.mark.parametrize('budget', [0, 128])
def test_insufficient_budget_is_explicit_and_small_not_an_empty_plan(budget):
    archive = codec().encode_battery_plan_archive(plan(), plan(seed=22), max_bytes=budget)
    assert archive['status'] == 'unavailable' and archive['included'] == []
    assert 'data' not in archive and size(archive) <= 128
    with pytest.raises(ValueError):
        codec().decode_battery_plan_archive(archive)


def test_empty_primary_is_distinct_from_unavailable_and_optional_lower_absence():
    archive = codec().encode_battery_plan_archive([])
    assert codec().decode_battery_plan_archive(archive) == {'primary': []}


def envelope(raw):
    return {'version': 1, 'encoding': 'json+zlib+base64', 'status': 'available',
            'included': ['primary'],
            'data': base64.b64encode(zlib.compress(raw)).decode()}


@pytest.mark.parametrize('value', [None, {}, [None], [{'bad': float('nan')}],
                                   [{'bad': float('inf')}], [{1: 'non-JSON key'}]])
def test_invalid_encoder_input_does_not_break_actual_sensor_history(value):
    archive = codec().encode_battery_plan_archive(value)
    assert archive['status'] == 'unavailable' and archive['reason'] == 'invalid_plan'
    assert size(archive) <= 128


@pytest.mark.parametrize('budget', [-1, True, 1.5, None])
def test_invalid_budget_is_a_small_controlled_unavailability(budget):
    result = codec().encode_battery_plan_archive([], max_bytes=budget)
    assert result['status'] == 'unavailable' and result['reason'] == 'invalid_budget'
    assert size(result) <= 128


@pytest.mark.parametrize('field,value', [
    ('version', 2), ('version', True), ('encoding', 'unknown'), ('data', 'not base64!'),
    ('included', ['lower']), ('included', ['primary', 'lower']), ('included', 'primary'),
    ('status', 'partial'), ('status', 'unavailable'), ('data', 'A' * 13000),
])
def test_invalid_envelope_or_declared_plan_set_is_rejected(field, value):
    archive = envelope(b'{"primary":[]}')
    archive[field] = value
    with pytest.raises(ValueError):
        codec().decode_battery_plan_archive(archive)


@pytest.mark.parametrize('raw', [
    b'not JSON', b'{"primary":{}}', b'{"primary":[],"unexpected":[]}',
    b'{"primary":[],"primary":[]}', b'{"primary":[{"n":NaN}]}',
    b'{"primary":[{"n":1e999}]}', b'{"primary":[null]}', b'\xff',
])
def test_invalid_inflated_mapping_and_numbers_are_rejected(raw):
    with pytest.raises(ValueError):
        codec().decode_battery_plan_archive(envelope(raw))


def test_decompression_bomb_is_rejected_before_json_loading():
    archive = envelope(b' ' * (256 * 1024 + 1))
    assert size(archive) < 12288
    with pytest.raises(ValueError):
        codec().decode_battery_plan_archive(archive)


@pytest.mark.parametrize('modification', ['truncate', 'garbage', 'second_stream'])
def test_incomplete_or_trailing_compressed_data_is_rejected(modification):
    data = zlib.compress(b'{"primary":[]}')
    data = data[:-2] if modification == 'truncate' else data + (
        b'garbage' if modification == 'garbage' else zlib.compress(b'{"primary":[]}'))
    archive = envelope(b'{"primary":[]}')
    archive['data'] = base64.b64encode(data).decode()
    with pytest.raises(ValueError):
        codec().decode_battery_plan_archive(archive)


def test_continuation_slot_limit_and_nested_cycles_are_bounded():
    assert codec().encode_battery_plan_archive([{}] * 577)['status'] == 'unavailable'
    value = {}; value['recursive'] = value
    assert codec().encode_battery_plan_archive([value])['status'] == 'unavailable'


def test_unicode_extra_fields_and_explicit_empty_lower_roundtrip_losslessly():
    primary = [{'text': 'Zeitfenster ä ☀', 'numbers': [0, 0., -0., 1.2345678901234567], 'optional': None}]
    archive = codec().encode_battery_plan_archive(primary, [])
    assert codec().decode_battery_plan_archive(archive) == {'primary': primary, 'lower': []}

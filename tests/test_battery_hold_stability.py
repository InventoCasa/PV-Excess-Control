"""Bounded release confirmation preserves deadlines and immediate overrides."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.pv_excess_control.battery_hold_stability import HoldReleaseConfirmation


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
CONTEXT = ('tariff-window', .20, 'forecast', 'consumer-policy')


def propose(filter_, seconds, action='self_consumption', context=CONTEXT, end=900):
    return filter_.propose(NOW+timedelta(seconds=seconds), action, context,
                           NOW+timedelta(seconds=end))


def test_native_without_confirmed_hold_never_gets_a_lease():
    filter_ = HoldReleaseConfirmation()
    assert propose(filter_, 0) is None
    assert filter_.authorized_until is None


def test_persistent_native_releases_at_sixty_seconds_without_rearming():
    filter_ = HoldReleaseConfirmation()
    assert filter_.authorize(NOW, NOW+timedelta(minutes=10), CONTEXT)
    deadline = NOW+timedelta(seconds=60)
    assert propose(filter_, 0) == deadline
    assert propose(filter_, 30) == deadline
    assert propose(filter_, 59) == deadline
    assert propose(filter_, 60) is None
    assert propose(filter_, 61) is None
    assert filter_.authorized_until is None
    assert filter_.pending_until is None


def test_repeated_callbacks_do_not_count_as_elapsed_confirmation():
    filter_ = HoldReleaseConfirmation()
    filter_.authorize(NOW, NOW+timedelta(minutes=10), CONTEXT)
    for seconds in (0, 0, .01, .1, 1, 10):
        assert propose(filter_, seconds) == NOW+timedelta(seconds=60)


def test_return_to_hold_cancels_pending_without_extending_authorization():
    filter_ = HoldReleaseConfirmation()
    until = NOW+timedelta(minutes=10)
    filter_.authorize(NOW, until, CONTEXT)
    propose(filter_, 0)
    assert propose(filter_, 30, 'hold') is None
    assert filter_.pending_until is None
    assert filter_.authorized_until == until
    assert propose(filter_, 40) == NOW+timedelta(seconds=100)


def test_hold_proposal_is_not_physical_authorization():
    filter_ = HoldReleaseConfirmation()
    assert propose(filter_, 0, 'hold') is None
    assert propose(filter_, 1) is None


@pytest.mark.parametrize('authorization,window', [(40, 900), (600, 20)])
def test_existing_hold_or_tariff_boundary_wins_over_confirmation(authorization, window):
    filter_ = HoldReleaseConfirmation()
    filter_.authorize(NOW, NOW+timedelta(seconds=authorization), CONTEXT)
    boundary = min(authorization, window)
    assert propose(filter_, 0, end=window) == NOW+timedelta(seconds=boundary)
    assert propose(filter_, boundary, end=window+900) is None
    assert propose(filter_, boundary+1, end=window+900) is None


def test_new_earlier_window_can_shorten_but_never_extend_pending_deadline():
    filter_ = HoldReleaseConfirmation()
    filter_.authorize(NOW, NOW+timedelta(minutes=10), CONTEXT)
    assert propose(filter_, 0) == NOW+timedelta(seconds=60)
    assert propose(filter_, 10, end=25) == NOW+timedelta(seconds=25)
    assert propose(filter_, 20, end=900) == NOW+timedelta(seconds=25)
    assert propose(filter_, 25) is None


def test_changed_hard_context_cannot_reuse_old_hold_authorization():
    filter_ = HoldReleaseConfirmation()
    filter_.authorize(NOW, NOW+timedelta(minutes=10), CONTEXT)
    propose(filter_, 0)
    assert propose(filter_, 1, context=('new-price-window',)) is None
    assert filter_.context is None
    assert propose(filter_, 2) is None


@pytest.mark.parametrize('action', ['charge', 'disabled', None])
def test_other_actions_cancel_confirmation_immediately(action):
    filter_ = HoldReleaseConfirmation()
    filter_.authorize(NOW, NOW+timedelta(minutes=10), CONTEXT)
    propose(filter_, 0)
    assert propose(filter_, 1, action) is None
    assert propose(filter_, 2) is None


def test_reset_and_fresh_confirmation_do_not_resurrect_pending_decision():
    filter_ = HoldReleaseConfirmation()
    filter_.authorize(NOW, NOW+timedelta(minutes=10), CONTEXT)
    propose(filter_, 0)
    filter_.reset()
    assert propose(filter_, 1) is None
    assert filter_.authorize(NOW+timedelta(seconds=2), NOW+timedelta(seconds=100), CONTEXT)
    assert propose(filter_, 2) == NOW+timedelta(seconds=62)


def test_backward_time_fails_closed_even_when_reauthorizing():
    filter_ = HoldReleaseConfirmation()
    filter_.authorize(NOW, NOW+timedelta(minutes=10), CONTEXT)
    propose(filter_, 20)
    assert propose(filter_, 19) is None
    assert propose(filter_, 21) is None
    filter_.authorize(NOW+timedelta(seconds=30), NOW+timedelta(minutes=10), CONTEXT)
    assert not filter_.authorize(NOW+timedelta(seconds=29), NOW+timedelta(minutes=10), CONTEXT)
    assert propose(filter_, 31) is None


@pytest.mark.parametrize('bad', [None, '2030-01-01', 0, NOW.replace(tzinfo=None)])
@pytest.mark.parametrize('field', ['now', 'until', 'window_end'])
def test_invalid_times_discard_existing_authorization(bad, field):
    filter_ = HoldReleaseConfirmation()
    filter_.authorize(NOW, NOW+timedelta(minutes=10), CONTEXT)
    propose(filter_, 0)
    if field == 'until':
        assert not filter_.authorize(NOW, bad, CONTEXT)
    else:
        assert filter_.propose(bad if field == 'now' else NOW, 'self_consumption', CONTEXT,
                               bad if field == 'window_end' else NOW+timedelta(minutes=10)) is None
    assert propose(filter_, 1) is None


@pytest.mark.parametrize('seconds', [0, -1])
def test_expired_authorization_is_not_accepted(seconds):
    filter_ = HoldReleaseConfirmation()
    assert not filter_.authorize(NOW, NOW+timedelta(seconds=seconds), CONTEXT)
    assert propose(filter_, 0) is None


def test_autumn_clock_fold_uses_absolute_elapsed_time():
    filter_ = HoldReleaseConfirmation()
    zone = ZoneInfo('Europe/Berlin')
    now = datetime(2030, 10, 27, 2, 59, 30, tzinfo=zone, fold=0)
    utc = now.astimezone(timezone.utc)
    until = (utc+timedelta(minutes=10)).astimezone(zone)
    assert filter_.authorize(now, until, CONTEXT)
    expected = utc+timedelta(seconds=60)
    assert filter_.propose(now, 'self_consumption', CONTEXT, until) == expected
    second = (utc+timedelta(seconds=40)).astimezone(zone)
    assert second.fold == 1
    assert filter_.propose(second, 'self_consumption', CONTEXT, until) == expected
    assert filter_.propose(expected.astimezone(zone), 'self_consumption', CONTEXT, until) is None

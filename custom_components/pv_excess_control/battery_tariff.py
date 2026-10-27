"""Detached tariff observations for coherent battery planning handoffs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .models import TariffInfo, TariffWindow

_CURRENT_PRICE_TOLERANCE = .00001
_FOUR_DECIMAL_PRICE_TOLERANCE = .00005 + 1e-12


@dataclass(frozen=True)
class BatteryTariffSnapshot:
    """One validated provider observation, independent of mutable shared context."""

    current_price: float
    feed_in_tariff: float
    cheap_price_threshold: float
    battery_charge_price_threshold: float
    windows: tuple[TariffWindow, ...]


def _number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _utc(value: datetime) -> datetime:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError("price_window_mismatch")
    return value.astimezone(timezone.utc)


def freeze_battery_tariff(
    now: datetime,
    live_price: float,
    candidate: TariffInfo | BatteryTariffSnapshot | None,
) -> BatteryTariffSnapshot:
    """Validate a current provider read without repairing contradictory inputs.

    The adapter supplies a fresh live price and provider observation, and remains
    responsible for sensor freshness. A stale snapshot is never advanced merely
    because its future windows appear to cover the new time. A real provider read
    must agree with the live price and contain one unambiguous current interval.
    Future gaps and overlaps remain explicit for the planner's contiguous-horizon
    validation.

    Rounded provider windows may differ by half a four-decimal price step. Only
    the active interval receives the full-precision live value, so a rounded
    window cannot conceal a live price above the configured purchase limit.
    """
    if (not _number(live_price) or not isinstance(candidate, (TariffInfo, BatteryTariffSnapshot))
            or not all(_number(value) for value in (
                candidate.current_price, candidate.feed_in_tariff,
                candidate.cheap_price_threshold, candidate.battery_charge_price_threshold,
            ))):
        raise ValueError("price_unavailable")
    if abs(candidate.current_price - live_price) > _CURRENT_PRICE_TOLERANCE:
        raise ValueError("price_snapshot_changed")
    instant = _utc(now)
    if not isinstance(candidate.windows, (list, tuple)):
        raise ValueError("price_window_mismatch")
    windows = []
    for row in candidate.windows:
        if not isinstance(row, TariffWindow) or not _number(row.price):
            raise ValueError("price_window_mismatch")
        start, end = _utc(row.start), _utc(row.end)
        if end <= start:
            raise ValueError("price_window_mismatch")
        windows.append(TariffWindow(start, end, row.price, row.is_cheap))
    windows.sort(key=lambda row: row.start)
    active = [index for index, row in enumerate(windows) if row.start <= instant < row.end]
    if len(active) != 1:
        raise ValueError("price_window_mismatch")
    index = active[0]
    row = windows[index]
    if abs(row.price - live_price) > _FOUR_DECIMAL_PRICE_TOLERANCE:
        raise ValueError("price_window_mismatch")
    windows[index] = TariffWindow(
        row.start, row.end, live_price, live_price <= candidate.cheap_price_threshold,
    )
    return BatteryTariffSnapshot(
        live_price, candidate.feed_in_tariff, candidate.cheap_price_threshold,
        candidate.battery_charge_price_threshold, tuple(windows),
    )

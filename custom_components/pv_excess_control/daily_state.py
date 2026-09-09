"""Validation of persisted daily counters; no Home Assistant dependencies."""

from __future__ import annotations

import math


def nonnegative_number(value: object, maximum: float | None = None) -> float:
    """Accept finite non-negative numbers; reject corrupt storage values."""
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number) or number < 0:
        return 0.0
    return min(number, maximum) if maximum is not None else number

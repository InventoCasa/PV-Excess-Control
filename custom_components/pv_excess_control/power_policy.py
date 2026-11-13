"""Measured power and battery-priority budgets, independent of HA I/O."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class BatteryBudget:
    available_w: float | None
    reserved_w: float | None
    source: str


def battery_first_budget(
    excess_w: float | None,
    battery_soc: float | None,
    target_soc: float,
    charging_w: float | None,
    running_load_w: float,
    configured_max_w: float | None,
    live_limit_w: float | None = None,
    live_limit_configured: bool = False,
) -> BatteryBudget:
    """Reserve only PV available above unmanaged load, up to the charge cap.

    Without a known cap, measured charging is protected but unknown extra
    charging capacity cannot be inferred from SoC. A configured live cap takes
    precedence, including zero and unavailable, so forecast throttling and BMS
    limits are respected instead of silently bypassed.
    """
    if excess_w is None or not math.isfinite(excess_w):
        return BatteryBudget(None, None, "power_unavailable")
    if battery_soc is None or not math.isfinite(battery_soc) or not 0 <= battery_soc <= 100:
        return BatteryBudget(min(excess_w, 0.0), None, "soc_unavailable")
    if battery_soc >= target_soc:
        return BatteryBudget(excess_w, 0.0, "target_reached")
    limit = configured_max_w if configured_max_w is not None and configured_max_w > 0 else None
    source = "configured_charge_limit"
    if live_limit_configured:
        if live_limit_w is None or not math.isfinite(live_limit_w) or live_limit_w < 0:
            return BatteryBudget(min(excess_w, 0.0), None, "charge_limit_unavailable")
        limit = min(limit, live_limit_w) if limit is not None else live_limit_w
        source = "live_charge_limit"
    if limit is None:
        if charging_w is None or not math.isfinite(charging_w):
            return BatteryBudget(min(excess_w, 0.0), None, "charge_power_unavailable")
        limit = max(charging_w, 0.0)
        source = "measured_charging"
    available_solar = max(excess_w + max(running_load_w, 0.0), 0.0)
    reserved = min(limit, available_solar)
    return BatteryBudget(excess_w - reserved, reserved, source)


def forecast_reserve_needed(
    remaining_today_kwh: float | None,
    battery_soc: float | None,
    target_soc: float,
    capacity_kwh: float | None,
    hours_to_target: float,
    base_load_w: float,
    buffer_kwh: float,
    currently_reserving: bool = False,
) -> bool | None:
    """Decide whether Balanced must protect battery charging in real time.

    Charging is reserved only when the remaining PV forecast cannot cover both
    the energy the battery still needs to reach ``target_soc`` and the expected
    household load until the target time, plus a buffer. Once reserving, the
    margin must exceed twice the buffer before appliances get the charging
    power back, so forecast noise does not toggle loads.

    Returns ``None`` when a required input is missing or invalid; the caller
    decides the fallback.
    """
    values = (remaining_today_kwh, battery_soc, capacity_kwh)
    if any(v is None or not math.isfinite(v) for v in values):
        return None
    if not 0 <= battery_soc <= 100 or capacity_kwh <= 0:
        return None
    needed_kwh = max(target_soc - battery_soc, 0.0) / 100.0 * capacity_kwh
    if needed_kwh <= 0:
        return False
    usage_kwh = max(hours_to_target, 0.0) * max(base_load_w, 0.0) / 1000.0
    margin_kwh = remaining_today_kwh - needed_kwh - usage_kwh
    threshold = buffer_kwh * 2 if currently_reserving else buffer_kwh
    return margin_kwh < threshold

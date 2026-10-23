"""Conditional, SOC-bounded forecasts of tariff-funded EV charging.

Solar-following operation remains reactive. The predicted funded power does
not rely on additional solar to exceed a configured grid-power allowance.
This module shares tariff/current permission with the pure runtime optimizer.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, time, timezone
import math

from .models import ApplianceConfig, ApplianceState, TariffInfo
from .optimizer import Optimizer


@dataclass(frozen=True)
class EvChargeRun:
    start: datetime
    end: datetime
    power_w: float
    price: float
    target_current: float

    @property
    def energy_kwh(self):
        return self.power_w * (self.end-self.start).total_seconds() / 3600000


@dataclass(frozen=True)
class EvChargeForecast:
    status: str
    runs: tuple[EvChargeRun, ...] = ()
    remaining_ac_kwh: float | None = None
    planned_ac_kwh: float | None = None
    unmet_ac_kwh: float | None = None
    required_minutes: float | None = None
    known_until: datetime | None = None


def configured_ev_forecast(app: ApplianceConfig) -> bool:
    return bool(app.ev_battery_capacity_entity or app.ev_battery_capacity_kwh is not None)


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int,float)) and math.isfinite(value)


def _offset_changes(start, end, zone):
    """Find actual offset transitions, including half-hour changes, in UTC."""
    changes=[]
    left=start
    while left<end:
        right=min(end,left+timedelta(hours=1))
        offset=left.astimezone(zone).utcoffset()
        if right.astimezone(zone).utcoffset()!=offset:
            lo,hi=int(left.timestamp()),int(right.timestamp())
            while hi-lo>1:
                mid=(lo+hi)//2
                if datetime.fromtimestamp(mid,timezone.utc).astimezone(zone).utcoffset()==offset:
                    lo=mid
                else:
                    hi=mid
            changes.append(datetime.fromtimestamp(hi,timezone.utc))
        left=right
    return changes


def build_ev_charge_forecast(
    now: datetime, app: ApplianceConfig, state: ApplianceState, tariff: TariffInfo,
    *, grid_voltage: float = 230, horizon_hours: float = 48,
) -> EvChargeForecast:
    """Estimate chronological eligible windows from a real SOC observation.

    No forecast energy is treated as delivered. A caller may reuse this frozen
    result within a bounded refresh epoch; next estimates use observed SOC.
    Missing values remain unknown, and the energy requirement never resets at
    midnight. Connection/target/safety remain real-time execution conditions.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        return EvChargeForecast("invalid_time")
    start = now.astimezone(timezone.utc)
    if state.ev_connected is not True:
        return EvChargeForecast("disconnected" if state.ev_connected is False else "connection_unknown")
    if app.override_active or app.helper_only:
        return EvChargeForecast("manual_or_helper_control")
    if app.remaining_runtime_entity or app.require_contiguous_runtime or app.min_daily_runtime:
        return EvChargeForecast("runtime_constraint_unavailable")
    if app.enable_condition_entity and state.enable_condition is not True:
        return EvChargeForecast("condition_unavailable")
    soc = state.ev_soc
    if not _number(soc) or not 0 <= soc <= 100:
        return EvChargeForecast("soc_unavailable")
    observed = state.ev_soc_observed_at
    if (not isinstance(observed,datetime) or observed.tzinfo is None
            or not -5 <= (start-observed.astimezone(timezone.utc)).total_seconds() <= 86400):
        return EvChargeForecast("soc_stale")
    capacity = state.ev_capacity_kwh
    if not _number(capacity) or not 0 < capacity <= 300:
        return EvChargeForecast("capacity_unavailable")
    efficiency, target = app.ev_charge_efficiency, app.ev_target_soc
    if (not _number(efficiency) or not .5 <= efficiency <= 1
            or not _number(target) or not 0 <= target <= 100
            or not _number(grid_voltage) or grid_voltage <= 0
            or not _number(horizon_hours) or not 0 < horizon_hours <= 48
            or not app.dynamic_current or type(app.phases) is not int or not 1 <= app.phases <= 3
            or not all(_number(v) for v in (app.min_current,app.max_current,app.current_step))
            or not 0 < app.min_current <= app.max_current or app.current_step <= 0
            or (app.max_grid_power is not None and (not _number(app.max_grid_power) or app.max_grid_power < 0))):
        return EvChargeForecast("invalid_configuration")
    need = max(0., capacity * (target-soc) / 100 / efficiency)
    if need == 0:
        return EvChargeForecast("target_reached", remaining_ac_kwh=0.,planned_ac_kwh=0.,unmet_ac_kwh=0.,required_minutes=0.)
    threshold = tariff.cheap_price_threshold if app.cheap_price_threshold is None else app.cheap_price_threshold
    if not all(_number(v) for v in (threshold,tariff.feed_in_tariff)):
        return EvChargeForecast("invalid_tariff")
    if app.cheap_grid_target_current is not None and not _number(app.cheap_grid_target_current):
        return EvChargeForecast("invalid_configuration")
    rows = []
    try:
        for row in tariff.windows:
            if row.start.tzinfo is None or row.end.tzinfo is None or not _number(row.price):
                raise ValueError
            left,right = row.start.astimezone(timezone.utc),row.end.astimezone(timezone.utc)
            if right <= left:
                raise ValueError
            if right > start and left < start+timedelta(hours=horizon_hours):
                rows.append((max(start,left),min(right,start+timedelta(hours=horizon_hours)),row.price))
    except (ValueError,TypeError,AttributeError):
        return EvChargeForecast("invalid_tariff")
    rows.sort()
    if not rows:
        return EvChargeForecast("missing_tariff")
    cursor = start
    for left,right,_ in rows:
        if left != cursor:
            return EvChargeForecast("tariff_gap_or_overlap")
        cursor = right
    if len(rows) > 300:
        return EvChargeForecast("too_many_tariff_intervals")
    policy = Optimizer(grid_voltage=grid_voltage)
    watt_per_amp = grid_voltage * app.phases

    def power(price):
        info = replace(tariff,current_price=price)
        if not policy._grid_allowed(app,info):
            return 0.
        watts = policy._grid_target_power(app,info)
        if app.max_grid_power is not None:
            watts = min(watts,app.max_grid_power)
        amps = math.floor((watts/watt_per_amp+1e-9)/app.current_step)*app.current_step
        return amps*watt_per_amp if app.min_current-1e-9 <= amps <= app.max_current+1e-9 else 0.

    observed_power = None
    power_stamp = state.current_power_observed_at
    if (state.is_on and state.current_power_available and _number(state.current_power) and state.current_power >= 100
            and isinstance(power_stamp,datetime) and power_stamp.tzinfo is not None
            and 0 <= (start-power_stamp.astimezone(timezone.utc)).total_seconds() <= 300):
        observed_power = max(100.,round(state.current_power/100)*100.)
    reference_power = power(min(threshold,tariff.feed_in_tariff))
    if (observed_power is not None and power(rows[0][2]) > 0
            and policy._is_within_time_window(now.time(),app.start_after,app.end_before)):
        reference_power = min(reference_power,observed_power)
    required_minutes = need*60000/reference_power if reference_power > 0 else None
    used_by_day, starts_by_day = {},{}
    offset_changes=_offset_changes(start,cursor,now.tzinfo)
    runs = []
    remaining = need
    previous_end = None
    measured_episode = observed_power is not None
    for left,right,price in rows:
        if remaining <= 1e-10:
            break
        command_power = power(price)
        if command_power <= 0:
            measured_episode=False
            continue
        boundaries = {left,right,*(stamp for stamp in offset_changes if left<stamp<right)}
        day = left.astimezone(now.tzinfo).date()-timedelta(days=1)
        last = right.astimezone(now.tzinfo).date()+timedelta(days=1)
        while day <= last:
            for bound in (time(),app.start_after,app.end_before):
                if bound is not None:
                    for fold in (0,1):
                        stamp = datetime.combine(day,bound,tzinfo=now.tzinfo).replace(fold=fold).astimezone(timezone.utc)
                        if left < stamp < right:
                            boundaries.add(stamp)
            day += timedelta(days=1)
        points=sorted(boundaries)
        for begin,end in zip(points,points[1:]):
            local = begin.astimezone(now.tzinfo)
            if not policy._is_within_time_window(local.time(),app.start_after,app.end_before):
                measured_episode=False
                continue
            watts = min(command_power,observed_power) if measured_episode else command_power
            day = local.date()
            used = used_by_day.get(day,state.runtime_today.total_seconds() if day==now.date() else 0.)
            starts = starts_by_day.get(day,state.activations_today if day==now.date() else 0)
            continuous = previous_end == begin or (begin==start and state.is_on)
            if app.max_daily_activations is not None and starts >= app.max_daily_activations and not continuous:
                measured_episode=False
                continue
            duration = min((end-begin).total_seconds(),remaining*3600000/watts)
            if app.max_daily_runtime is not None:
                duration = min(duration,max(0.,app.max_daily_runtime.total_seconds()-used))
            if duration <= 1e-8:
                measured_episode=False
                continue
            end = begin+timedelta(seconds=duration)
            take = watts*duration/3600000
            run = EvChargeRun(begin,end,watts,price,command_power/watt_per_amp)
            if runs and runs[-1].end==begin and abs(runs[-1].power_w-watts)<1e-8 and runs[-1].target_current==run.target_current:
                prior = runs[-1]
                prior_seconds = (prior.end-prior.start).total_seconds()
                runs[-1] = replace(prior,end=end,price=(prior.price*prior_seconds+price*duration)/(prior_seconds+duration))
            else:
                runs.append(run)
            previous_end=end
            remaining=max(0.,remaining-take)
            used_by_day[day]=used+duration
            starts_by_day[day]=starts+(not continuous)
    return EvChargeForecast("available",tuple(runs),need,need-remaining,remaining,required_minutes,cursor)

"""Bounded complete comparisons of earlier charging and later native discharge."""
from __future__ import annotations

from dataclasses import replace
import math


_MAX_SLOTS = 576
_PRICE_LEVELS = 4
_QUANTITIES = (.25, .5, .75, 1.)


def improve_charge_hold_exchange(timeline, config, efficiency, initial, incumbent):
    """Keep the incumbent unless a fully validated joint exchange costs less.

    Up to four existing purchase-price levels, four later hold subsets and four
    quantities create at most 64 complete proposals. Existing episode/action
    boundaries stay fixed except selected holds become native operation. This
    does not start new episodes or change a confirmed/prepared current charge.
    Capacity and later fixed purchases constrain extra quantities before replay;
    fresh physical, timing, PV and terminal guards remain the final authority.
    """
    from . import battery_planner as bp
    from .battery_continuation import replay_continuation

    if (not config.hold_supported or config.grid_charge_active
            or config.grid_charge_prepared or config.grid_charge_minimum_until is not None):
        return incumbent
    slots = bp._slots(incumbent)
    if (not timeline or not slots or len(slots) > _MAX_SLOTS
            or slots[0].action == "charge"):
        return incumbent

    def interval(slot):
        return bp._Interval(slot.start, slot.end, slot.price, slot.pv_kwh, slot.load_kwh)

    maximum_grid = {}
    for index, slot in enumerate(slots):
        if (slot.action != "charge" or slot.grid_charge_kwh <= bp._EPS
                or slot.charging_hours <= 0 or slot.price > config.charge_price_limit):
            continue
        maximum = min(config.max_charge_power_w * slot.charging_hours / 1000,
                      config.native_charge_power_w * slot.charging_hours / 1000 - slot.pv_charge_kwh)
        maximum = bp._executable_grid_energy(interval(slot), config, slot.pv_charge_kwh,
                                             maximum, charging_hours=slot.charging_hours)
        if maximum > slot.grid_charge_kwh + bp._EPS:
            maximum_grid[index] = maximum
    if not maximum_grid:
        return incumbent

    def allocate(indices, release_at, budget=math.inf):
        # Track the exact extra inventory at every intervening boundary. This
        # reserves room for unchanged later purchases even when a cheaper
        # interval is considered before an earlier, more expensive interval.
        extra_inventory = [0.] * release_at
        quantities = {}
        for index in indices:
            slot = slots[index]
            room = min((
                (config.capacity_kwh * (config.grid_target_soc / 100
                    if slots[k].action == "charge" else 1.)
                 - slots[k].soc_end * config.capacity_kwh / 100 - extra_inventory[k]) / efficiency
                for k in range(index, release_at)
            ), default=0.)
            extra = min(budget, maximum_grid[index] - slot.grid_charge_kwh, max(0., room))
            grid = bp._executable_grid_energy(interval(slot), config, slot.pv_charge_kwh,
                slot.grid_charge_kwh + extra, charging_hours=slot.charging_hours)
            extra = max(0., grid - slot.grid_charge_kwh)
            if extra <= bp._EPS:
                continue
            quantities[index] = grid
            budget = max(0., budget - extra)
            for k in range(index, release_at):
                extra_inventory[k] += extra * efficiency
        return quantities

    best = incumbent
    seen = set()
    levels = sorted({slots[i].price for i in maximum_grid})[:_PRICE_LEVELS]
    for price in levels:
        purchases = sorted((i for i in maximum_grid if slots[i].price <= price),
                           key=lambda i: (slots[i].price, slots[i].start))
        earliest_purchase_end = min(slots[i].end for i in purchases)
        threshold = price / config.roundtrip_efficiency + config.wear_cost_per_kwh
        holds = [i for i, slot in enumerate(slots) if slot.action == "hold"
                 and slot.price > threshold + bp._ECONOMIC_TIE_EPS
                 and slot.start >= earliest_purchase_end
                 and (max(0., slot.load_kwh-slot.pv_kwh)
                      + max(0., slot.external_load_kwh-slot.reserved_pv_kwh)) > bp._EPS]
        if not holds:
            continue
        ranked = sorted(holds, key=lambda i: (-slots[i].price, slots[i].start))
        masks = [tuple(holds)]
        masks.extend(tuple(sorted(ranked[:max(1, math.ceil(len(ranked)*fraction))]))
                     for fraction in (.25, .5, .75))
        for released in dict.fromkeys(masks):
            release_at = min(released)
            preceding = [i for i in purchases if slots[i].end <= slots[release_at].start]
            if not preceding:
                continue
            maximum = allocate(preceding, release_at)
            total = sum(grid-slots[i].grid_charge_kwh for i, grid in maximum.items())
            if total <= bp._EPS:
                continue
            for fraction in _QUANTITIES:
                quantities = maximum if fraction == 1. else allocate(preceding, release_at, total*fraction)
                signature = (released, tuple(sorted(quantities.items())))
                if not quantities or signature in seen:
                    continue
                seen.add(signature)
                proposal = tuple(replace(slot, action="self_consumption") if i in released else
                    replace(slot, grid_charge_kwh=quantities[i]) if i in quantities else slot
                    for i, slot in enumerate(slots))
                plan = bp.BatteryGridPlan(proposal, sum(s.grid_charge_kwh for s in proposal),
                                         0., "economic_plan", config.grid_target_soc)
                candidate = replay_continuation(timeline, config, efficiency, initial, plan)
                if candidate is not None and bp._prefer_economic_node(candidate, best):
                    best = candidate
    return best

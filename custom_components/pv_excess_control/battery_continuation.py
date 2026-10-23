"""Re-evaluate a previous charging trajectory against fresh physical inputs."""
from dataclasses import replace
import math


def replay_continuation(timeline, config, efficiency, initial, previous_plan):
    """Return a fully validated economic candidate, never a runtime charge lease.

    Existing action and power boundaries remain fixed. Fresh measurements,
    forecasts, tariffs and limits determine every new balance. A failed guard
    simply excludes this candidate; the normal search and immediate stops remain.
    """
    from . import battery_planner as bp

    if (not config.grid_charge_active or previous_plan is None or not previous_plan.valid
            or not previous_plan.slots or not timeline):
        return None
    previous = previous_plan.slots
    try:
        if (len(previous) > 576 or any(
                row.end <= row.start or row.action not in ("charge", "hold", "self_consumption")
                for row in previous)
                or any(left.end != right.start for left, right in zip(previous, previous[1:]))
                or bp.charge_episode_at(previous_plan, timeline[0].start) is None):
            return None
        edges = {point for row in previous for point in (row.start, row.end)}
        edges.update(row.grid_charge_available_at for row in previous if row.grid_charge_available_at is not None)
        refined = []
        for row in timeline:
            points = [row.start, *sorted(point for point in edges if row.start < point < row.end), row.end]
            for start, end in zip(points, points[1:]):
                fraction = (end - start) / (row.end - row.start)
                refined.append(replace(row, start=start, end=end, pv=row.pv * fraction,
                    load=row.load * fraction, reserved_pv=row.reserved_pv * fraction,
                    external_load=row.external_load * fraction,
                    withheld_pv=row.withheld_pv * fraction,
                    elapsed_hours=row.elapsed_hours + (start - row.start).total_seconds() / 3600))
        if len(refined) > 576:
            return None
        baseline = initial
        baseline_solar, baseline_energy = [], []
        for row in refined:
            baseline, solar = bp._transition(row, baseline, config, efficiency, "self_consumption")
            baseline_solar.append(solar)
            baseline_energy.append(baseline.energy)
        reserve = config.capacity_kwh * config.reserve_soc / 100
        native_margin = [max(0., energy - reserve) for energy in baseline_energy]
        future_margin = native_margin[:]
        for index in range(len(future_margin) - 2, -1, -1):
            future_margin[index] = min(future_margin[index], future_margin[index + 1])
        bounds = [0.] * (len(refined) + 1)
        bounds[-1] = baseline.energy
        for index in range(len(refined) - 1, -1, -1):
            row = refined[index]
            discharge = min(row.deficit_kwh, row.discharge_limit(config) * row.hours / 1000) / efficiency
            bounds[index] = min(config.capacity_kwh, max(0., bounds[index + 1] + discharge - baseline_solar[index]))

        def loss_debt(candidate, solar, index):
            physical = candidate.slot.startup_pv_loss_kwh * efficiency
            loss = min(physical, max(0., baseline_solar[index] - solar))
            return min(max(0., candidate.startup_pv_loss - physical + loss), native_margin[index])

        node = initial
        old_index = 0
        for index, row in enumerate(refined):
            while old_index < len(previous) and previous[old_index].end <= row.start:
                old_index += 1
            old = previous[old_index] if old_index < len(previous) else None
            # A moving horizon may gain a suffix. Native operation there adds no
            # new purchase or shifted future charging instruction.
            action = old.action if old is not None else "self_consumption"
            if old is not None and not old.start <= row.start < row.end <= old.end:
                return None
            if action != "charge":
                if (action == "hold" and not config.hold_supported
                        or node.charge_active and (
                            not node.charge_grid_delivered
                            or node.charge_minimum_until is not None and row.start < node.charge_minimum_until
                            or node.charge_available_at is not None and row.start < node.charge_available_at)):
                    return None
                candidate, solar = bp._transition(row, node, config, efficiency, action)
            else:
                if (row.price > config.charge_price_limit
                        or not node.charge_active and node.charge_not_before is not None
                        and row.start < node.charge_not_before):
                    return None
                _, minimum = bp._charge_timing(row, node, config)
                if minimum is not None and minimum > refined[-1].end:
                    return None
                held, solar = bp._transition(row, node, config, efficiency, "charge")
                hours = held.slot.charging_hours
                if hours <= 0:
                    candidate = held
                else:
                    episode = bp.charge_episode_at(previous_plan, row.start)
                    if episode is None or not math.isfinite(episode.power_w) or episode.power_w <= 0:
                        return None
                    grid = episode.power_w * hours / 1000 - solar / efficiency
                    debit = min(loss_debt(held, solar, index), future_margin[index])
                    maximum = min(config.max_charge_power_w * hours / 1000,
                        config.native_charge_power_w * hours / 1000 - solar / efficiency,
                        (min(config.capacity_kwh * config.grid_target_soc / 100,
                             max(0., bounds[index + 1] - debit)) - held.energy) / efficiency)
                    if (grid <= bp._EPS or grid > maximum + bp._EPS
                            or abs(bp._executable_grid_energy(row, config, solar / efficiency, grid,
                                charging_hours=hours) - grid) > bp._EPS):
                        return None
                    candidate = bp._add_grid_charge(node, held, config, efficiency, grid, row.elapsed_hours)
            outstanding = loss_debt(candidate, solar, index)
            candidate = replace(candidate, startup_pv_loss=outstanding)
            debit = min(outstanding, future_margin[index])
            if (candidate.energy > max(0., bounds[index + 1] - debit) + bp._EPS
                    or solar + candidate.slot.startup_pv_loss_kwh * efficiency + bp._EPS < baseline_solar[index]):
                return None
            node = candidate
        if node.charge_active and (not node.charge_grid_delivered
                or node.charge_minimum_until is not None or node.charge_available_at is not None):
            return None
        return node
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        return None

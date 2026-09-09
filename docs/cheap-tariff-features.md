# Cheap-Tariff Features

PV Excess Control responds to cheap (or negative) tariff windows in three ways:

1. **Boolean appliances with `allow_grid_supplement = true`** run at full
   `nominal_power` from the grid during cheap windows (existing behaviour).
2. **Dynamic-current appliances with `allow_grid_supplement = true`** run
   at the new optional `cheap_grid_target_current` (defaulting to
   `max_current` for new entries) during cheap windows. Capped by
   `max_grid_power` when set.
3. **Battery** can be commanded to forced grid charge via the new
   `auto_battery_grid_charge` toggle plus a configured inverter wiring.
   See `docs/inverter-grid-charge-setup.md`.

## Per-appliance settings

| Field                       | Where        | Effect                                                                                |
|-----------------------------|--------------|---------------------------------------------------------------------------------------|
| `allow_grid_supplement`     | appliance    | Master gate for *any* grid import on that appliance.                                  |
| `cheap_price_threshold`     | appliance    | Per-appliance cheap threshold (overrides the global one if set).                      |
| `max_grid_power`            | appliance    | Hard cap on grid contribution (newly meaningful on dynamic appliances since v…).      |
| `cheap_grid_target_current` | dynamic only | Target current during cheap windows; defaults to `max_current` on new entries.        |

### Migration note for existing dynamic-current appliances

Existing dynamic-current subentries had `cheap_grid_target_current = None`
implicitly, which preserved the old min-current behaviour. The form now
pre-fills `max_current` on first edit. If you saved an appliance form
after this version, it activates the new max-current behaviour. To keep
the old min-current behaviour explicitly, set
`cheap_grid_target_current` to `min_current`.

## Battery settings

| Field                                 | Effect                                                     |
|---------------------------------------|------------------------------------------------------------|
| `battery_charge_price_threshold`      | Cheap-window threshold for battery decisions.              |
| `battery_target_soc`                  | Auto-engage stops once SoC reaches this.                   |
| `auto_battery_grid_charge`            | Master toggle for the new auto behaviour.                  |
| `battery_grid_charge_power_w`         | Power written to the inverter on engage.                   |
| `grid_charge_engage_min_duration_minutes` | Hysteresis floor before disengage is allowed.          |
| `inverter_force_charge_*` (3 triplets)| Wiring to your inverter's HA entities.                     |

## Worked example: production-style setup

Octopus EPEX-linked tariff with negative midday hours. Configuration:

- `cheap_price_threshold: 0.05` (per-appliance, Kona)
- `battery_charge_price_threshold: 0.02`
- Kona: `dynamic_current=true`, `min_current=6A`, `max_current=16A`,
  `phases=3`, `allow_grid_supplement=true`,
  `cheap_grid_target_current=16A`, `max_grid_power=11000W`.
- Battery: `battery_target_soc=100`, `auto_battery_grid_charge=true`,
  `battery_grid_charge_power_w=5000`, mkaiser wiring as in
  `docs/inverter-grid-charge-setup.md`.

Behaviour:

- 12:00 (price = 0.026 €/kWh): Kona engages at 16A (cheap-tariff for Kona)
  using grid + solar. Battery: above 0.02 → no auto engage.
- 13:00–15:00 (price ≤ −0.06 €/kWh): Kona at 16A, battery in forced
  grid-charge at 5000 W. Manual `force_charge` switch is OFF and not used.
- 16:00 (price = 0.080 €/kWh): Kona's grid-supplement gate closes (above
  0.05); battery's gate already closed by 13:00 if SoC reached 100.

The manual `force_charge` switch remains available as an emergency
override that engages the inverter regardless of price (and additionally
sheds appliances).

## Battery discharge protection during cheap windows

When the integration is in any "grid-import mode", it tells the inverter
to refuse battery discharge by writing `0` to the configured
`battery_max_discharge_entity` (e.g.
`input_number.set_sg_battery_max_discharge_power` on Sungrow). Loads then
draw from grid instead of bleeding the battery you just charged.

The block fires when ANY of these are true:

1. **Any appliance decision in the current cycle is grid-supplemented** —
   i.e. the optimizer explicitly marks the decision as grid-supported. This
   includes the cheap-window override (Sites 1/2/3) AND the existing
   opportunity-cost path (`grid_price < feed_in_tariff`).
2. **Manual `force_charge` switch is ON** — explicit user signal.
3. **Auto-grid-charge state machine is engaged** — battery is being
   force-charged from grid.

The block writes `max_discharge_watts = 0`, which **overrules** any
active `battery_max_discharge_override` from a big consumer (because 0
is the most restrictive value). It does NOT shed appliances — the goal
is for loads to run on cheap grid, not be turned off.

**Priority order in Phase 4:**

1. SoC-protection (`battery_soc < min_battery_soc`) → highest, sheds
   appliances + max_discharge=0.
2. **Cheap-tariff / grid-import block (NEW)** → max_discharge=0, no
   shedding.
3. Big-consumer override → `min(active_overrides)`.
4. None of the above → no limit; the coordinator restores
   `battery_max_discharge_default`.

### Worked example

Negative-price midday on prod (`-€0.137/kWh`):

- Kona running at 16 A (cheap-window override active, decision tagged
  `"Grid supplement (cheap-window target): 16.0A"`).
- Solar 6 kW, Kona 11 kW, base load 700 W → ~5.7 kW shortfall.
- **Without** the discharge block: home battery supplements ~5 kW into
  Kona (grid imports only ~700 W). Battery drains over the cheap window.
- **With** the discharge block: integration writes `0` to the inverter's
  max-discharge entity. Inverter refuses battery discharge. The full
  5.7 kW shortfall comes from grid — at -€0.137/kWh you're earning
  €0.78/h for that import. Battery stays at its current SoC (or rises,
  if `auto_battery_grid_charge` is also on).

When Kona stops or price rises above the threshold and the
grid-supplement decision goes away, the integration restores
`battery_max_discharge_default` on the same path it does today.

### Caveats

- Requires `battery_max_discharge_entity` to be configured (same entity
  used by the existing big-consumer protection). Without it, the block
  computes the action but the coordinator has nothing to write to —
  silent no-op.
- The opportunity-cost path (`grid_price < feed_in_tariff`) becomes a
  trigger for users who have only `allow_grid_supplement=true` set
  (not the new `auto_battery_grid_charge` or `cheap_grid_target_current`).
  This is a behaviour change vs. before this version: their battery will
  no longer supplement appliances during opportunity-cost windows.

See [current grid-budget semantics](energy-policy.md#stable-tariff-support): `max_grid_power` caps the imported portion, and runtime behavior no longer depends on status wording.

# Conditions, delays and current regulation

All new controls are optional. Existing appliance configurations keep their previous defaults.

| Setting | Meaning |
| --- | --- |
| `enable_condition_entity` | A binary sensor or input boolean allowing automatic starts. Unknown/unavailable is not permission to start. |
| `enable_condition_mode` | `start_only` (default) checks new starts. `while_running` also stops an active appliance if permission disappears, including `on_only`. Explicit override retains its existing precedence. |
| `start_delay` | Seconds of continuously sufficient solar budget before a new start. A lost condition or insufficient budget resets the wait. Intentional cheap-grid starts, deadline must-runs and overrides bypass this delay. |
| `phase_count_entity` | Integer 1–3 describing actual active phases. Zero/unknown retains the last valid reading, or static configured phases at startup. This controls accounting, not hardware phase switching. |
| `current_update_interval` | Minimum seconds between automatic current increases after a successful write. Default 0. |
| `current_min_change` | Smallest automatic current increase in amperes. Default 0. |

Reductions and out-of-range current corrections remain immediate. Running dynamic
`on_only` appliances continue regulating down to their minimum current when solar
falls. `on_only` does not itself start an unfunded load. For a charger that should
receive supply immediately on plug-in, a Home Assistant automation can turn on
its supply; automatic current regulation then follows the running-state rules.

A pending solar start does not reserve power or start dependencies. Helper-only
appliances cannot have independent start gates/delays; configure these on the
consumers that require the helper. Ordinary dependencies still respect their own
conditions and start delay.

Positive `off_threshold` values up to 500 W retain an export buffer for solar
operation, including dynamic current rounding. Deliberately grid-funded operation
keeps its separate funding rules. Global `on_threshold` is an optional fallback;
an explicit appliance value wins, and leaving both unset preserves type-specific
start margins.

The appliance **Allow Grid Supplement** switch and global **Cheap Price Threshold**
and **Battery Charge Price Threshold** numbers persist through reloads. Prices are
currency/kWh, including negative values; use the complete end-customer price.

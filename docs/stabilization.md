# Appliance control and restart resilience

This stabilization update preserves the existing optimizer and battery strategies. It fixes daily-state storage, device ownership, configuration synchronization and ambiguous control transitions.

## Daily counters

Runtime, energy and activation counts are stored per integration entry and restored before the first update after a restart or reload. Deactivated and paused appliances retain their counters. Observed physical operation continues to count even when automatic control is suspended. Power-based completion thresholds still apply.

Counters belong to the Home Assistant local calendar day. An overdue day reset is recovered by the next update, and a midnight callback cannot reset the same day a second time. Energy analytics are restored alongside the device counters. Time spent while Home Assistant was stopped is not estimated or added.

A pending save is scheduled at most 60 seconds out; subsequent updates do not postpone it. Pending data is also flushed on normal Home Assistant shutdown and integration unload. Abrupt power loss can still lose changes since the last completed save. Read/write failures are logged; a failed load is not overwritten with fresh empty data before another reload.

## Enabled, Paused and Override

| Control | Effect |
|---|---|
| Enabled ON | Allows automatic control unless Paused is on. Cancels an unfinished explicit stop. |
| Enabled OFF | Cancels Override, disables automatic control and requests physical shutdown. This keeps the previous meaning of Enabled. |
| Paused ON | Suspends automatic control without changing the physical device. Counters continue from observed operation. |
| Paused OFF | Returns the device to automatic control if Enabled is on. |
| Override ON | Explicitly forces operation, including when Enabled is off or Paused is on; existing override safety rules remain in force. Cancels an unfinished explicit stop. |
| Override OFF | Resumes automatic control when Enabled is on and Paused is off. When Enabled is off, requests physical shutdown. When Paused is on, automatic control remains suspended. |

Pause hands control to the user or another automation: the optimizer does not enforce that appliance's runtime or battery rules while it is paused. An active Override takes precedence over Pause. The integration's master disable remains an explicit request to stop managed appliances.

A shutdown request stays pending until the physical entity reports OFF. Failures are logged and retried on subsequent coordinator cycles, including after a reload. The status sensor shows `Shutdown pending` rather than implying the device has stopped. Enabling the device or activating Override cancels the pending request. Removing the device removes its pending request on the next cycle.

## Daily minimum and status

The existing minimum daily runtime remains a hard protection against ordinary surplus-based shedding. This update does not silently reinterpret it as a preferred target. When that protection prevents shedding, the status explains the remaining daily runtime instead of saying `shed imminent` indefinitely. Maximum runtime, on-only behavior, deadline enforcement and minimum-battery protection retain their existing precedence.

The complete field report in issue #57 still needs the affected installation's configuration and logs to establish whether this protection explains every reported occurrence.

## Configuration and devices

Editing a device's priority or daily runtime in its options takes effect without reloading the integration. Changes that affect planning invalidate the old plan. Number entities use the current synchronous HA subentry update API.

Each appliance now has its own logical HA device and its entities belong to its configuration subentry. Migration preserves existing entity IDs, unique IDs, custom names, icons and disabled states. Removing a subentry lets HA remove its entities. Only recognized integration-owned legacy orphan records are cleaned up; unrelated entities and the main device are preserved.

Generated entity labels omit the appliance name because Home Assistant already adds the logical device's name. Existing custom names are retained.

## Development checks

The primary test environment is pinned in `requirements_test.txt` and needs Python 3.14.2 or newer (Home Assistant 2026.8.0). The integration continues to target Home Assistant 2025.8 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest tests/ -q
```

The new tests exercise HA's actual Store, service calls, setup/reload and entity/device registries. They do not contact a physical appliance, battery or production Home Assistant installation.

## Current regulation and prices (Block 3)

Power averages use timestamp windows, independent of the controller interval;
up to 30 minutes are retained. Legacy naive timestamps are interpreted as UTC.
`current_update_interval` (seconds) and `current_min_change` (A) default to zero.
They restrict automatic increases only. Reductions, starts and overrides remain
immediate, and held current targets are accounted for before allocating power to
other consumers. Confirmed current settings are reused after reload. Disconnected
EV overrides can preset current without switching the charger on or reserving power.

Price thresholds accept negative values consistently, in currency/kWh (for
example -0.05 EUR/kWh). Use the complete end-customer price including applicable
fees and taxes; the integration does not infer taxes or convert cents to euros.

A usable average requires at least three valid samples. A very short appliance
window with fewer samples retains the existing fallback to the global average.
For an independently effective appliance average, allow at least three controller
intervals (for example 90 seconds with a 30-second controller interval).

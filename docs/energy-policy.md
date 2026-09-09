# Hybrid balance, Battery First and grid supplementation

This update makes measured surplus, battery reservation and authorized grid contribution explicit. The existing lifecycle controls and daily-state persistence remain in place.

## Measured surplus and battery priority

The Excess Power sensor reports the physical, battery-neutral surplus. Its new attributes explain the optimizer's usable budget:

| Attribute | Meaning |
|---|---|
| `available_excess_power_w` | Surplus available after battery-priority reservation; can be negative. |
| `battery_charge_reserve_w` | Power reserved for charging; null if the reservation cannot be determined. |
| `battery_priority_status` | Reservation source, target reached, inactive, or the missing reading. |

For a signed combined grid meter and mapped battery power:

```text
surplus = grid export - grid import + battery charging - battery discharging
```

A battery charging at 2 kW from a 3 kW grid import therefore does not become 2 kW of fictitious solar: the remaining surplus is −1 kW. Use consistent measurement points/units; inverter conversion losses and asynchronous sensor updates can cause small differences.

For a hybrid PV/household-load mapping, surplus is PV production minus household load. Household load includes managed appliances and excludes battery charging. A zero load is valid. A missing required reading does not switch a hybrid installation to a different calculation. Separate charging/discharging sensors are only treated as zero when that half was not configured, not when its configured sensor is unavailable. With a signed grid meter, map both directions if the battery can both charge and discharge; an unmapped direction is assumed not to contribute.

Non-hybrid installations retain the existing direct-export-meter preference and PV/load fallback at zero export. An export-only sensor cannot quantify grid import; signed grid metering or a complete PV/load mapping is needed for dependable deficit detection.

## Battery First without a forecast

Below the configured target SoC, Battery First reserves charging power before ordinary surplus allocation. This works even without a forecast provider. Once the target is reached, the reservation is released.

- If **Battery Max Charge Power (W)** is configured, it bounds the reservation. A configured live max-charge-power entity further limits it, including an intentional zero limit. This also respects the existing dynamic-battery-charge throttle. The live limit must report W/kW/MW (unitless helpers are interpreted as W); an ampere limit needs a separate conversion and is not silently treated as watts. This unit conversion is for reading the Battery First cap. If dynamic battery charging is also enabled, its existing writer requires a W (or unitless-W) writable entity or an explicit conversion adapter; it does not convert outgoing setpoints to kW/MW.
- The reservation cannot exceed available solar above unmanaged house load. Managed running loads are included when determining reclaimable power, allowing ordinary loads to be shed so the battery can charge.
- Without a known charging limit, the integration protects the **measured charging power**. This prevents new loads taking power already charging the battery. It cannot infer unused charging capacity or reliably reclaim additional power from running loads; configure a realistic charging limit if that behavior is required.
- A configured maximum is an upper bound, not a guarantee that the BMS can accept it. Near-full tapering or other BMS restrictions may require a live limit sensor to avoid reserving too much.

Example: PV minus household load is 2966 W, the battery charges at 2476 W, and SoC is below target. With measured-charge fallback, 490 W remains for appliances. If total surplus is instead 8000 W and the configured charging maximum is 2500 W, 5500 W remains available even before the battery reaches target.

Unknown required battery readings prevent new positive-surplus allocation, but known negative surplus still reaches deficit shedding. Missing current physical balance prevents allocation from stale history. Explicit override, on-only/daily-runtime constraints and minimum-SoC protection retain their existing precedence. Balanced and Appliance First are not redesigned by this update.

## Stable tariff support

When **Allow Grid Supplement** is enabled and the current tariff qualifies as cheap for the appliance (or satisfies the existing feed-in comparison), the optimizer evaluates solar plus allowed grid power for both new starts and already-running appliances. A fixed-power consumer no longer loses support simply because it has changed from OFF to ON.

**Max Grid Power limits the imported portion, not total appliance power.** For example, a 1-phase charger drawing 6 A at 230 V needs 1380 W. With 920 W solar and a 500 W grid cap, it can run using 460 W grid. A 500 W cap alone would not support that minimum current without enough solar.

Dynamic consumers use the configured cheap-window target, or minimum current when no target is set; additional available solar can raise current. The target is bounded by solar plus the grid allowance. At tariff expiry or when permission is removed, ordinary surplus regulation resumes. Actual response time follows controller polling and the configured switching interval. Configure a writable battery-discharge limit entity to enforce the discharge block at the inverter. Deliberate tariff starts can use fully covering solar without the ordinary solar-only activation buffer; more available solar must not make the same tariff start less feasible.

Running authorized grid draw is already present in measured net power, so its necessary funded portion is recovered in the optimizer budget. Unused grid headroom is not treated as additional solar. Paused/disabled consumers remain part of household demand. Deficits belonging to other household loads are not billed against a managed appliance's individual allowance. The settings are per appliance and do not implement a global house-connection import limit.

At a single net meter the integration does not pretend to export all PV and simultaneously buy that same power back for an appliance. Favorable feed-in comparison remains an eligibility condition; physical solar consumption is still accounted for.

## Meter availability and estimates

A mapped valid 0 W reading stays zero in allocation, preemption, shedding, override budgeting and analytics. A mapped unavailable reading is distinguished from zero: ordinary regulation of that consumer waits for recovery, without contributing fictitious power to another load. Existing safety rules, including minimum battery SoC, remain effective. An already-running tariff-supported consumer retains the battery-discharge block while tariff eligibility remains valid, even though its missing meter cannot supply a watt estimate.

If tariff eligibility expires while that appliance's meter is unavailable, the hold remains until the reading recovers; normal tariff-based shutdown therefore cannot be promised during the outage. Low-SoC safety still applies. This is visible as an unavailable-power status and should be resolved by restoring the sensor.

When no appliance power meter is configured, ON fixed loads use their configured nominal draw as an estimate. Dynamic loads use observed current, phases and configured voltage when available. An explicitly disconnected EV contributes zero. These are estimates, not additional physical measurements; inaccurate nominal ratings affect budget accuracy. Pausing a load removes it from managed-resource allocation while its consumption stays in the household balance.

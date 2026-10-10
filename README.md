<!-- IF YOU EDIT THIS FILE, also update README.de.md -->
<p align="center"><a href="README.de.md">Deutsch</a> · English</p>

# PV Excess Control

**A comprehensive Home Assistant integration for intelligent solar excess power optimization and cheap grid tariff management.**

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/HA-2025.8%2B-blue)](https://www.home-assistant.io)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Sponsor](https://img.shields.io/badge/Sponsor-%E2%9D%A4-ea4aaa)](https://github.com/sponsors/InventoCasa)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-%E2%98%95-FFDD00)](https://buymeacoffee.com/henrikic)


## About

PV Excess Control is built and maintained by Henrik Wasserfuhr, founder of [**InventoCasa**](https://inventocasa.de). We are specialized smart home integrators, designing and deploying complete Home Assistant environments for new builds, renovations, and retrofits.

This integration is open source because I believe in giving back to the community that makes Home Assistant great. It’s also a core component I use in my professional builds. If you want a complete smart home designed, configured, and commissioned from start to finish, InventoCasa takes on a limited number of custom projects each year.

→ [inventocasa.de](https://inventocasa.de) - Bring your smart home vision to life

## Features

The [0.4.0rc1 candidate notes](docs/release-candidate.md) explain migration and validation status. New optional [conditions, delays and controls](docs/generic-controls.md) and [forecast/runtime inputs](docs/forecast-runtime.md) keep existing configuration defaults.

### Core Optimization & Planning
- **Smart Planning** - 24-hour forward-looking optimizer with weather-aware pre-planning and configurable plan influence.
- **Priority-Based Appliance Control** - Manage multiple appliances with configurable priorities (1-1000).
- **Opportunity Cost** - Factors in feed-in tariff revenue when making decisions.
- **Appliance Dependencies** - Chain appliances so one only runs when another is active.
- **Per-Appliance Averaging Window** - Custom smoothing period per appliance for excess power calculations.
- **Min/Max Runtime & Time Windows** - Ensure appliances run for required durations and restrict them to specific hours.

### EV & Battery Management
- **EV SoC-Aware Charging** - Considers EV battery level, connection status, and user-defined targets.
- **Schedule Deadlines** - Set constraints like "EV must be charged by 7am".
- **Dynamic Current Control** - Variable amperage for EV chargers and wallboxes (6-32 A).
- **Battery-Aware Optimization** - Three strategies: Battery First, Appliance First, Balanced.
- **Minimum Battery SoC Protection** - Shed appliances when battery level drops below a configured threshold.
- **Battery Discharge Protection** - Limit discharge rate when big consumers are running.
- **Forecast-aware grid charging** — plans useful battery energy from future prices, household demand and PV forecasts, accounting for storage losses and an optional wear allowance. A configurable PV forecast factor accounts for overestimated production in grid-charge planning; its default 1.0 preserves existing forecasts. Current major consumers and bounded operating windows reduce available solar energy without double-counting household demand. Separate solar/grid targets preserve room for sunshine; charging remains opt-in. Fresh inputs and acknowledged inverter commands are required. Optional, physically verified discharge hold reserves energy for expensive hours. See [Battery Management](docs/features/battery-management.md).
- **Battery control recovery** — disabling control or unloading the integration releases charging and holding started by the integration. Failed or unconfirmed releases remain pending for retry; status attributes report the reason.
- **Dynamic battery charging** — forecast-aware throttle that defers morning charging when the day's expected curtailment can fill the battery on its own, plus a reactive cap-follower that absorbs PV when measured excess approaches the export limit. Opt-in. See [docs/dynamic-battery-charging.md](docs/dynamic-battery-charging.md).

### Tariffs & Grid
- **Tariff Integration** - Support for Tibber, Awattar, Nordpool, Octopus Energy, and generic price sensors.
- **Export Limit Management** - Absorb would-be-curtailed power when feed-in caps apply.
- **Grid Supplementation** - Allow a small amount of grid power to top up appliances.

### UI, Analytics & Integrations
- **Solar Forecast Integration** - Solcast, Forecast.Solar, and generic forecast sensors.
- **Extensive Dashboard Examples** — Build your own dashboard with Mushroom, ApexCharts and other community cards. [Full YAML examples included](docs/dashboard-examples.md).
- **Self-Consumption Analytics** - Track savings, self-consumption ratio, energy statistics.
- **Manual Override** - Force appliances on/off from the dashboard.
- **Configurable Notifications** - Per-event toggles for appliance changes, daily summaries, warnings.

## Requirements

- Home Assistant 2025.8 or newer
- A solar inverter with power sensors exposed to Home Assistant
- [HACS](https://hacs.xyz/) for the recommended installation method

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant sidebar
2. Click the three-dot menu and select **Custom repositories**
3. Add `https://github.com/InventoCasa/PV-Excess-Control` as an **Integration**
4. Search for "PV Excess Control" and click **Download**
5. Restart Home Assistant
6. Go to **Settings → Devices & Services → Add Integration** and search for **PV Excess Control**

### Manual

1. Download or clone this repository
2. Copy the `custom_components/pv_excess_control` folder into your `config/custom_components/` directory
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration** and search for **PV Excess Control**

## Quick Start

1. **Add the integration** - Settings → Devices & Services → Add Integration → PV Excess Control
2. **Configure your inverter** - Select Standard or Hybrid, then map your power sensors
3. **Configure energy pricing** - Select your tariff provider or leave it as None
4. **Add appliances** - Use the integration's sub-device UI to add each appliance
5. **Set up your dashboard** — See the [Dashboard Examples](docs/dashboard-examples.md) for ready-to-use YAML configurations using popular community cards.

See the [full documentation](docs/) for detailed setup guides.

## Documentation

- [Installation Guide](docs/installation.md)
- [Configuration](docs/configuration/)
  - [Initial Setup](docs/configuration/initial-setup.md)
  - [Sensor Mapping](docs/configuration/sensor-mapping.md)
  - [Adding Appliances](docs/configuration/adding-appliances.md)
  - [Energy Pricing](docs/configuration/energy-pricing.md)
  - [Solar Forecast](docs/configuration/solar-forecast.md)
  - [Multi-Inverter Setup](docs/configuration/multi-inverter.md)
- [Features](docs/features/)
  - [Battery Management](docs/features/battery-management.md)
  - [Dynamic Current Control](docs/features/dynamic-current.md)
  - [EV Charging](docs/features/ev-charging.md)
  - [Tariff Optimization](docs/features/tariff-optimization.md)
  - [Export Limiting](docs/features/export-limiting.md)
  - [Weather Pre-Planning](docs/features/weather-preplanning.md)
  - [Notifications](docs/features/notifications.md)
  - [Analytics](docs/features/analytics.md)
- [Dashboard](docs/dashboard/)
  - [Dashboard Examples](docs/dashboard-examples.md)
  - [Entity Reference](docs/dashboard/custom-dashboards.md)
- [Advanced](docs/advanced/)
  - [How It Works](docs/advanced/how-it-works.md)
  - [Priority Guide](docs/advanced/priority-guide.md)
  - [Troubleshooting](docs/advanced/troubleshooting.md)
  - [Automation Examples](docs/advanced/automation-examples.md)
- [Migration from Blueprint](docs/migration.md)

## Architecture

The integration uses a hybrid real-time + planning approach:

- **Real-time Controller** (every 30 s) - Reads live sensor data, applies optimizer decisions
- **Forward-Looking Planner** (every 15 min) - Creates optimal 24-hour schedules using forecast and tariff data
- **Pure-Logic Optimizer** - Zero HA dependencies, fully unit-testable decision engine

## Support this project

PV Excess Control is designed to reduce your energy bills and maximize your solar investment. If this integration brings measurable value to your home and you'd like to support its ongoing development, consider [sponsoring me on GitHub](https://github.com/sponsors/InventoCasa) or [buying me a coffee ☕](https://buymeacoffee.com/henrikic). Every contribution helps keep the code open and actively maintained.

## Contributing

Contributions are welcome! Please open an issue first to discuss proposed changes. Pull requests should include tests for new logic and must pass the existing test suite.

Development tests require Python 3.14.2+; the pinned test environment uses Home Assistant 2026.8.0. See [control and restart behavior](docs/stabilization.md) for the stabilization changes.

```bash
pip install -r requirements_test.txt
python3 -m pytest tests/ -q
```


## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)** - see the [LICENSE](LICENSE) file for details.

**What this means:**
- **Personal use** - fully free, no restrictions
- **Commercial use** - if you integrate this into a product or service, you must open-source your entire work under AGPL-3.0
- **Commercial licensing** - for proprietary/commercial use without the AGPL obligations, [contact InventoCasa](https://inventocasa.de/kontakt/) for a commercial license

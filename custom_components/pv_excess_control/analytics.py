"""Analytics tracker for PV Excess Control.

Calculates self-consumption ratios, savings, and per-appliance statistics.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .daily_state import nonnegative_number

_LOGGER = logging.getLogger(__name__)


@dataclass
class ApplianceStats:
    """Per-appliance statistics."""

    energy_today_kwh: float = 0.0
    runtime_today: timedelta = field(default_factory=timedelta)
    savings_today: float = 0.0


@dataclass
class RunRecord:
    """Record of a single appliance run period."""

    appliance_id: str
    start_time: datetime
    power_watts: float
    source: str  # "solar", "cheap_tariff", "grid"
    tariff_price: float
    feed_in_tariff: float


class AnalyticsTracker:
    """Tracks energy analytics including savings and self-consumption ratios."""

    def __init__(
        self,
        feed_in_tariff: float = 0.0,
        normal_import_price: float = 0.25,
    ) -> None:
        self.feed_in_tariff = feed_in_tariff
        self.normal_import_price = normal_import_price
        self._appliance_stats: dict[str, ApplianceStats] = {}
        self._total_solar_consumed_kwh: float = 0.0
        self._total_solar_produced_kwh: float = 0.0
        self._total_grid_export_kwh: float = 0.0
        self._total_savings: float = 0.0
        self._last_reset: datetime = datetime.now()

    def record_cycle(
        self,
        appliance_id: str,
        power_watts: float,
        duration_seconds: float,
        source: str,
        current_price: float,
    ) -> None:
        """Record one control cycle for an appliance.

        Args:
            appliance_id: Unique identifier for the appliance.
            power_watts: Power consumption in watts.
            duration_seconds: Duration of the cycle in seconds.
            source: Energy source - "solar", "cheap_tariff", or "grid".
            current_price: Current import tariff price per kWh.
        """
        energy_kwh = (power_watts * duration_seconds) / 3_600 / 1_000
        stats = self._appliance_stats.setdefault(appliance_id, ApplianceStats())
        stats.energy_today_kwh += energy_kwh
        stats.runtime_today += timedelta(seconds=duration_seconds)

        if source == "solar":
            savings = energy_kwh * (current_price - self.feed_in_tariff)
            self._total_solar_consumed_kwh += energy_kwh
        elif source == "cheap_tariff":
            savings = energy_kwh * (self.normal_import_price - current_price)
        else:
            savings = 0.0

        if not math.isfinite(savings):
            savings = 0.0

        stats.savings_today += max(savings, 0.0)
        self._total_savings += max(savings, 0.0)
        _LOGGER.debug(
            "Analytics: %s %.1fW for %ds source=%s savings=%.4f",
            appliance_id, power_watts, duration_seconds, source, max(savings, 0.0),
        )

    def record_solar_production(
        self, power_watts: float, duration_seconds: float
    ) -> None:
        """Record total solar production for self-consumption ratio calculation.

        Args:
            power_watts: Solar production power in watts.
            duration_seconds: Duration of the measurement period in seconds.
        """
        energy_kwh = (power_watts * duration_seconds) / 3_600 / 1_000
        self._total_solar_produced_kwh += energy_kwh

    def record_grid_export(self, power_watts: float, duration_seconds: float) -> None:
        """Record grid export for tracking.

        Args:
            power_watts: Grid export power in watts.
            duration_seconds: Duration of the measurement period in seconds.
        """
        energy_kwh = (power_watts * duration_seconds) / 3_600 / 1_000
        self._total_grid_export_kwh += energy_kwh

    @property
    def self_consumption_ratio(self) -> float:
        """Percentage of solar energy consumed by MANAGED appliances (not total household) (0-100)."""
        if self._total_solar_produced_kwh <= 0:
            return 0.0
        return min(
            100.0,
            (self._total_solar_consumed_kwh / self._total_solar_produced_kwh) * 100,
        )

    @property
    def savings_today(self) -> float:
        """Total savings accumulated today in the configured currency."""
        return self._total_savings

    @property
    def solar_consumed_kwh(self) -> float:
        """Total solar energy consumed locally today in kWh."""
        return self._total_solar_consumed_kwh

    @property
    def grid_export_kwh(self) -> float:
        """Total energy exported to the grid today in kWh."""
        return self._total_grid_export_kwh

    def get_appliance_stats(self, appliance_id: str) -> ApplianceStats:
        """Return stats for a specific appliance.

        Returns a default ApplianceStats if the appliance has no recorded data.
        """
        return self._appliance_stats.get(appliance_id, ApplianceStats())

    def snapshot_daily(self) -> dict:
        """Serialize totals alongside appliance counters for consistent summaries."""
        return {
            "solar_consumed_kwh": self._total_solar_consumed_kwh,
            "solar_produced_kwh": self._total_solar_produced_kwh,
            "grid_export_kwh": self._total_grid_export_kwh,
            "savings": self._total_savings,
            "appliances": {
                key: {"energy_kwh": stats.energy_today_kwh,
                      "runtime_seconds": stats.runtime_today.total_seconds(),
                      "savings": stats.savings_today}
                for key, stats in self._appliance_stats.items()
            },
        }

    def restore_daily(self, data: object, active_ids: set[str]) -> None:
        """Restore validated totals, excluding removed appliance records."""
        if not isinstance(data, dict):
            return
        self._total_solar_consumed_kwh = nonnegative_number(data.get("solar_consumed_kwh"))
        self._total_solar_produced_kwh = nonnegative_number(data.get("solar_produced_kwh"))
        self._total_grid_export_kwh = nonnegative_number(data.get("grid_export_kwh"))
        self._total_savings = nonnegative_number(data.get("savings"))
        rows = data.get("appliances", {})
        if isinstance(rows, dict):
            self._appliance_stats = {
                key: ApplianceStats(
                    energy_today_kwh=nonnegative_number(row.get("energy_kwh")),
                    runtime_today=timedelta(seconds=nonnegative_number(row.get("runtime_seconds"), 86400)),
                    savings_today=nonnegative_number(row.get("savings")),
                )
                for key, row in rows.items()
                if key in active_ids and isinstance(row, dict)
            }

    def reset_daily(self) -> None:
        """Reset daily counters. Should be called at midnight."""
        self._appliance_stats.clear()
        self._total_solar_consumed_kwh = 0.0
        self._total_solar_produced_kwh = 0.0
        self._total_grid_export_kwh = 0.0
        self._total_savings = 0.0
        self._last_reset = datetime.now()

"""Notification manager for PV Excess Control."""
from __future__ import annotations

from datetime import datetime

import logging

from homeassistant.core import HomeAssistant

from .const import DEFAULT_NOTIFICATION_SETTINGS, NotificationEvent

_LOGGER = logging.getLogger(__name__)


class NotificationManager:
    """Manages sending notifications for configurable events with rate limiting."""

    def __init__(
        self,
        hass: HomeAssistant,
        notification_settings: dict[str, bool] | None = None,
        notification_service: str | None = None,
    ) -> None:
        self.hass = hass
        self.settings = notification_settings if notification_settings is not None else DEFAULT_NOTIFICATION_SETTINGS.copy()
        self.service = notification_service  # e.g., "notify.mobile_app_phone"
        self._last_sent: dict[str, datetime] = {}
        self._rate_limit_seconds = 300  # 5 minutes
        self._battery_grid_charge_notified = False

    async def async_notify(
        self,
        event_type: str,
        message: str,
        title: str = "PV Excess Control",
        *,
        bypass_rate_limit: bool = False,
        blocking: bool = False,
    ) -> bool:
        """Send notification if event is enabled and not rate-limited."""
        if not self.settings.get(event_type, False):
            return False
        if not bypass_rate_limit and self._is_rate_limited(event_type):
            return False

        # Set BEFORE the call to avoid TOCTOU race
        if not bypass_rate_limit:
            self._last_sent[event_type] = datetime.now()

        if self.service:
            if "." not in self.service:
                _LOGGER.warning("Invalid notification service format: %s (expected domain.service)", self.service)
                return False
            domain, service_name = self.service.split(".", 1)
            await self.hass.services.async_call(
                domain,
                service_name,
                {"message": message, "title": title},
                **({"blocking": True} if blocking else {}),
            )
        else:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "message": message,
                    "title": title,
                    "notification_id": f"pv_excess_{event_type}",
                },
                **({"blocking": True} if blocking else {}),
            )

        return True

    def _is_rate_limited(self, event_type: str) -> bool:
        """Return True if the event was sent within the rate limit window."""
        last = self._last_sent.get(event_type)
        if last is None:
            return False
        return (datetime.now() - last).total_seconds() < self._rate_limit_seconds

    # ------------------------------------------------------------------
    # Convenience methods for common notifications
    # ------------------------------------------------------------------

    async def notify_appliance_on(self, name: str, reason: str, power: float) -> bool:
        """Send notification that an appliance was turned on."""
        return await self.async_notify(
            NotificationEvent.APPLIANCE_ON,
            f"{name} started: {reason} ({power:.0f}W)",
        )

    async def notify_appliance_off(self, name: str, reason: str) -> bool:
        """Send notification that an appliance was turned off."""
        return await self.async_notify(
            NotificationEvent.APPLIANCE_OFF,
            f"{name} stopped: {reason}",
        )

    async def notify_override(self, name: str, until: str | None = None) -> bool:
        """Send notification that a manual override was activated."""
        msg = f"{name} override active"
        if until:
            msg += f" until {until}"
        return await self.async_notify(NotificationEvent.OVERRIDE_ACTIVATED, msg)

    async def notify_force_charge(self, active: bool) -> bool:
        """Send notification about battery force charge start/stop."""
        action = "started" if active else "stopped"
        return await self.async_notify(
            NotificationEvent.FORCE_CHARGE,
            f"Battery force charge {action}",
        )

    async def notify_battery_grid_charge_engaged(self, power_w: float) -> bool:
        """Announce qualified measured grid flow, remembering successful delivery."""
        if self._battery_grid_charge_notified:
            return False
        sent = await self.async_notify(
            NotificationEvent.FORCE_CHARGE,
            f"Battery grid charge started: about {power_w:.0f}W grid contribution",
            blocking=True,
        )
        self._battery_grid_charge_notified = sent
        return sent

    async def notify_battery_grid_charge_disengaged(self, reason: str) -> bool:
        """Close an announced episode even within the start rate-limit window."""
        if not self._battery_grid_charge_notified:
            return False
        # Consume the pair even if this attempt fails; a delivery error must
        # never leave later charging episodes permanently suppressed.
        self._battery_grid_charge_notified = False
        sent = await self.async_notify(
            NotificationEvent.FORCE_CHARGE,
            "Battery grid charge stopped",
            bypass_rate_limit=True,
            blocking=True,
        )
        return sent

    async def notify_sensor_unavailable(self, sensor_name: str) -> bool:
        """Send notification that a required sensor is unavailable."""
        return await self.async_notify(
            NotificationEvent.SENSOR_UNAVAILABLE,
            f"{sensor_name} unavailable - control paused",
        )

    async def notify_daily_summary(
        self, ratio: float, savings: float, solar_kwh: float
    ) -> bool:
        """Send daily summary notification."""
        return await self.async_notify(
            NotificationEvent.DAILY_SUMMARY,
            f"Today: {ratio:.0f}% self-consumption, saved {savings:.2f}, {solar_kwh:.1f} kWh solar",
        )

    async def notify_forecast_warning(self, tomorrow_kwh: float, actions: str) -> bool:
        """Send notification warning about low solar forecast tomorrow."""
        return await self.async_notify(
            NotificationEvent.FORECAST_WARNING,
            f"Tomorrow: low solar ({tomorrow_kwh:.1f} kWh). {actions}",
        )

    async def notify_plan_deviation(self, name: str, details: str) -> bool:
        """Send notification that an appliance deviated from its plan."""
        return await self.async_notify(
            NotificationEvent.PLAN_DEVIATION,
            f"{name} deviated from plan: {details}",
        )

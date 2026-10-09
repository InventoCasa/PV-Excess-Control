"""Generic inverter forced grid-charge controller with bounded readback checks.

Engage order: mode → power → command. Release always attempts both command
and mode, even if one fails. Number setpoints remain unchanged on release.
Helpers confirm only their own state unless separate physical readback entities
are configured; diagnostics distinguish these levels of confirmation.
"""
from __future__ import annotations

import asyncio
import logging
import math

from homeassistant.core import HomeAssistant, State

from .models import InverterGridChargeConfig

_LOGGER = logging.getLogger(__name__)

SUPPORTED_ENABLE_DOMAINS = frozenset({"input_select", "select", "switch", "input_boolean"})
SUPPORTED_POWER_DOMAINS = frozenset({"input_number", "number"})
_HELPER_DOMAINS = frozenset({"input_select", "input_number", "input_boolean"})
_TRUTHY_STRINGS = frozenset({"on", "true", "1", "yes"})
_UNAVAILABLE = frozenset({"unknown", "unavailable", "none", ""})


class InverterConfirmationError(RuntimeError):
    """An inverter command could not be confirmed within its timeout."""


class InverterGridChargeController:
    """Drive forced grid-charge entities and verify reported control state."""

    def __init__(self, hass: HomeAssistant, config: InverterGridChargeConfig) -> None:
        self._hass = hass
        self._config = config
        self._validate_domains()
        if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
            raise ValueError("Inverter confirmation timeout must be finite and positive")
        if not math.isfinite(config.power_tolerance_w) or config.power_tolerance_w < 0:
            raise ValueError("Inverter power tolerance must be finite and nonnegative")

    @property
    def config(self) -> InverterGridChargeConfig:
        """Return the immutable actuator mapping for durable cleanup ownership."""
        return self._config

    @property
    def confirmation_level(self) -> str:
        """Whether every configured channel has a physical rather than helper state."""
        for command, feedback in self._channels():
            if self._domain(feedback or command) in _HELPER_DOMAINS:
                return "entity"
        return "physical"

    def _channels(self) -> list[tuple[str, str | None]]:
        config = self._config
        channels = [(config.enable_entity_id, config.enable_feedback_entity_id)]
        if config.mode_entity_id:
            channels.append((config.mode_entity_id, config.mode_feedback_entity_id))
        if config.power_entity_id:
            channels.append((config.power_entity_id, config.power_feedback_entity_id))
        return channels

    def _validate_domains(self) -> None:
        enable_domain = self._domain(self._config.enable_entity_id)
        if enable_domain not in SUPPORTED_ENABLE_DOMAINS:
            raise ValueError(
                f"enable_entity '{self._config.enable_entity_id}' has unsupported domain '{enable_domain}'. "
                f"Supported domains: {sorted(SUPPORTED_ENABLE_DOMAINS)}"
            )
        if self._config.mode_entity_id is not None:
            mode_domain = self._domain(self._config.mode_entity_id)
            if mode_domain not in SUPPORTED_ENABLE_DOMAINS:
                raise ValueError(
                    f"mode_entity '{self._config.mode_entity_id}' has unsupported domain '{mode_domain}'."
                )
        if self._config.power_entity_id is not None:
            power_domain = self._domain(self._config.power_entity_id)
            if power_domain not in SUPPORTED_POWER_DOMAINS:
                raise ValueError(
                    f"power_entity '{self._config.power_entity_id}' has unsupported domain '{power_domain}'."
                )

    async def engage(self, power_w: float) -> None:
        """Confirm each engage step; attempt release after any partial failure."""
        config = self._config
        # Validate the complete power request before changing inverter mode.
        power_value = self._power_value(power_w)
        try:
            if config.mode_entity_id is not None:
                await self._write(config.mode_entity_id, config.mode_engage_value)
                await self._confirm([(config.mode_entity_id, config.mode_feedback_entity_id,
                                      config.mode_engage_value, False)])
            if config.power_entity_id is not None:
                await self._write(config.power_entity_id, power_value)
                await self._confirm([(config.power_entity_id, config.power_feedback_entity_id, power_w, True)])
            await self._write(config.enable_entity_id, config.enable_engage_value)
            await self.verify_engaged(power_w)
        except Exception:
            try:
                await self.disengage()
            except Exception:
                _LOGGER.exception("Inverter release after failed grid-charge command was not confirmed")
            raise
        # CancelledError intentionally propagates. The caller retains ownership
        # and must retry cleanup; cancellation must not look like a confirmed stop.

    async def disengage(self) -> None:
        """Attempt both release writes independently and confirm the final state."""
        config = self._config
        errors: list[Exception] = []
        commands = [(config.enable_entity_id, config.enable_disengage_value)]
        if config.mode_entity_id is not None:
            commands.append((config.mode_entity_id, config.mode_disengage_value))
        for entity_id, value in commands:
            try:
                await self._write(entity_id, value)
            except Exception as error:
                errors.append(error)
        try:
            await self.verify_disengaged()
        except Exception as error:
            errors.append(error)
        if errors:
            raise errors[0]

    async def verify_engaged(self, power_w: float) -> None:
        """Check reported mode, command and power without issuing writes."""
        self._power_value(power_w)
        config = self._config
        expected = [(config.enable_entity_id, config.enable_feedback_entity_id, config.enable_engage_value, False)]
        if config.mode_entity_id is not None:
            expected.append((config.mode_entity_id, config.mode_feedback_entity_id, config.mode_engage_value, False))
        if config.power_entity_id is not None:
            expected.append((config.power_entity_id, config.power_feedback_entity_id, power_w, True))
        await self._confirm(expected, enable_feedback_value=config.enable_feedback_engage_value)

    async def verify_disengaged(self) -> None:
        """Check reported release command and self-consumption mode without writes."""
        config = self._config
        expected = [(config.enable_entity_id, config.enable_feedback_entity_id, config.enable_disengage_value, False)]
        if config.mode_entity_id is not None:
            expected.append((config.mode_entity_id, config.mode_feedback_entity_id, config.mode_disengage_value, False))
        await self._confirm(expected, enable_feedback_value=config.enable_feedback_disengage_value)

    async def _confirm(
        self,
        expected: list[tuple[str, str | None, object, bool]],
        *,
        enable_feedback_value: str | None = None,
    ) -> None:
        """Wait for matching readable states, keeping helpers and feedback separate.

        Stable hardware registers may keep old HA timestamps while polling.
        Available matching control values therefore do not require a state change;
        measurement freshness for charging decisions is enforced by the caller.
        """
        mismatches: list[str] = []
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                while True:
                    mismatches = []
                    for command, feedback, value, is_power in expected:
                        for entity_id in dict.fromkeys((command, feedback or command)):
                            feedback_override = (
                                command == self._config.enable_entity_id
                                and entity_id == feedback
                                and entity_id != command
                                and enable_feedback_value is not None
                            )
                            expected_value = enable_feedback_value if feedback_override else value
                            if not self._matches(
                                entity_id, command, expected_value, is_power,
                                normalize_switch=not feedback_override,
                            ):
                                mismatches.append(entity_id)
                    if not mismatches:
                        return
                    await asyncio.sleep(min(0.1, self._config.timeout_seconds / 5))
        except TimeoutError as error:
            raise InverterConfirmationError(
                f"Could not confirm inverter state: {', '.join(mismatches)}"
            ) from error

    def _matches(
        self, entity_id: str, command: str, value: object, is_power: bool,
        *, normalize_switch: bool = True,
    ) -> bool:
        state = self._hass.states.get(entity_id)
        if not isinstance(state, State) or state.state.strip().lower() in _UNAVAILABLE:
            return False
        if is_power:
            try:
                actual_w = float(state.state) * self._power_multiplier(state)
            except (TypeError, ValueError):
                return False
            return math.isfinite(actual_w) and abs(actual_w - float(value)) <= self._config.power_tolerance_w
        expected = str(value).strip()
        if normalize_switch and self._domain(command) in {"switch", "input_boolean"}:
            expected = "on" if expected.lower() in _TRUTHY_STRINGS else "off"
        return state.state.strip() == expected

    def _power_value(self, power_w: float) -> float:
        """Convert watts to the configured number's unit without unsafe clamping."""
        power_w = float(power_w)
        if not math.isfinite(power_w) or power_w < 0:
            raise ValueError("Inverter charging power must be finite and nonnegative")
        if not self._config.power_entity_id:
            return power_w
        state = self._hass.states.get(self._config.power_entity_id)
        if not isinstance(state, State) or state.state.strip().lower() in _UNAVAILABLE:
            raise ValueError("Inverter power control is unavailable")
        value = power_w / self._power_multiplier(state)
        for attr, lower in (("min", True), ("max", False)):
            limit = state.attributes.get(attr)
            if limit is None:
                continue
            limit = float(limit)
            if not math.isfinite(limit) or (value < limit if lower else value > limit):
                raise ValueError(f"Inverter charging power outside configured {attr} range")
        return value

    @staticmethod
    def _power_multiplier(state: State) -> float:
        unit = state.attributes.get("unit_of_measurement")
        if unit in {None, "", "W"}:
            return 1.0
        if unit == "kW":
            return 1000.0
        raise ValueError(f"Unsupported inverter power unit: {unit}")

    async def _write(self, entity_id: str, value: object) -> None:
        domain = self._domain(entity_id)
        if domain in {"input_select", "select"}:
            service = "select_option"
            data = {"entity_id": entity_id, "option": str(value)}
        elif domain in {"input_number", "number"}:
            service = "set_value"
            data = {"entity_id": entity_id, "value": float(value)}
        elif domain in {"switch", "input_boolean"}:
            service = "turn_on" if str(value).strip().lower() in _TRUTHY_STRINGS else "turn_off"
            data = {"entity_id": entity_id}
        else:
            raise ValueError(f"Unsupported entity domain for inverter grid charge: {domain}")
        async with asyncio.timeout(self._config.timeout_seconds):
            await self._hass.services.async_call(domain, service, data, blocking=True)

    @staticmethod
    def _domain(entity_id: str) -> str:
        return entity_id.split(".", 1)[0]

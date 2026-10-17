"""Generic inverter forced grid-charge controller with bounded readback checks.

Engage order: mode → power → command. Release always attempts both command
and mode, even if one fails. Charging-power setpoints remain unchanged on release.
Helpers confirm only their own state unless separate physical readback entities
are configured; diagnostics distinguish these levels of confirmation.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import math

from homeassistant.core import HomeAssistant, State, valid_entity_id

from .models import InverterGridChargeConfig

_LOGGER = logging.getLogger(__name__)

SUPPORTED_ENABLE_DOMAINS = frozenset({"input_select", "select", "switch", "input_boolean", "number", "input_number"})
SUPPORTED_POWER_DOMAINS = frozenset({"input_number", "number"})
_HELPER_DOMAINS = frozenset({"input_select", "input_number", "input_boolean"})
_TRUTHY_STRINGS = frozenset({"on", "true", "1", "yes"})
_UNAVAILABLE = frozenset({"unknown", "unavailable", "none", ""})


def _numeric_value(value: object) -> float:
    """Parse a nonnegative native-unit command, never a truthy string."""
    if isinstance(value, bool):
        raise ValueError("Invalid numeric inverter command")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid numeric inverter command") from error
    if not math.isfinite(number) or number < 0:
        raise ValueError("Invalid numeric inverter command")
    return number


def _validate_numeric_mapping(config: InverterGridChargeConfig) -> None:
    """Validate numeric mappings independently of live HA state and storage."""
    if config.enable_reassert_entity_id is not None:
        bridge = config.enable_reassert_entity_id
        feedback = config.enable_feedback_entity_id
        if (not isinstance(bridge, str) or not valid_entity_id(bridge)
                or bridge.split(".", 1)[0] != "automation"
                or config.enable_entity_id.split(".", 1)[0] not in SUPPORTED_POWER_DOMAINS
                or not feedback or feedback == config.enable_entity_id
                or feedback.split(".", 1)[0] in _HELPER_DOMAINS):
            raise ValueError("Numeric bridge reassertion needs an automation and separate physical feedback")
    for entity, engage, release in (
        (config.enable_entity_id, config.enable_engage_value, config.enable_disengage_value),
        (config.mode_entity_id, config.mode_engage_value, config.mode_disengage_value),
    ):
        if entity and entity.split(".", 1)[0] in SUPPORTED_POWER_DOMAINS:
            if _numeric_value(engage) == _numeric_value(release):
                raise ValueError("Numeric inverter commands must differ")
    if config.enable_entity_id.split(".", 1)[0] in SUPPORTED_POWER_DOMAINS:
        engage = config.enable_feedback_engage_value
        release = config.enable_feedback_disengage_value
        engage = config.enable_engage_value if engage is None else engage
        release = config.enable_disengage_value if release is None else release
        if _numeric_value(engage) == _numeric_value(release):
            raise ValueError("Numeric inverter feedback values must differ")


class InverterConfirmationError(RuntimeError):
    """An inverter command could not be confirmed within its timeout."""


class InverterGridChargeController:
    """Drive forced grid-charge entities and verify reported control state."""

    def __init__(self, hass: HomeAssistant, config: InverterGridChargeConfig) -> None:
        self._hass = hass
        self._config = config
        self._validate_domains()
        _validate_numeric_mapping(config)
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
        self._validate_start(power_w)
        try:
            await self.prepare(power_w, require_stopped=False)
            await self.enable_prepared(power_w, require_stopped=False)
        except Exception:
            try:
                await self.disengage()
            except Exception:
                _LOGGER.exception("Inverter release after failed grid-charge command was not confirmed")
            raise
        # Cancellation leaves cleanup with the caller's durable ownership.

    async def prepare(self, power_w: float, *, require_stopped: bool = True) -> None:
        """Confirm mode and power while leaving the charge command stopped.

        The caller must retain cleanup ownership throughout preparation. Direct
        engage remains available for manual commands and existing integrations.
        """
        config = self._config
        self._validate_start(power_w)
        if require_stopped:
            await self.verify_disengaged()
        if config.mode_entity_id is not None:
            await self._write_and_confirm(config.mode_entity_id, config.mode_engage_value,
                lambda: self._confirm([(config.mode_entity_id, config.mode_feedback_entity_id,
                                       config.mode_engage_value, False)]))
        await self.set_power(power_w)

    def _validate_start(self, power_w: float) -> None:
        config = self._config
        # Validate the complete power request before changing inverter mode.
        self._power_value(power_w)
        for entity, engage, release in (
            (config.enable_entity_id, config.enable_engage_value, config.enable_disengage_value),
            (config.mode_entity_id, config.mode_engage_value, config.mode_disengage_value),
        ):
            if entity and self._domain(entity) in SUPPORTED_POWER_DOMAINS:
                self._validate_numeric_setpoint(entity, engage)
                self._validate_numeric_setpoint(entity, release)

    async def set_power(self, power_w: float) -> None:
        """Adjust only the charge power, preserving an ongoing charge episode."""
        config = self._config
        power_value = self._power_value(power_w)
        if config.power_entity_id is not None:
            await self._write_and_confirm(config.power_entity_id, power_value,
                lambda: self._confirm([(config.power_entity_id, config.power_feedback_entity_id, power_w, True)]))

    async def enable_prepared(
        self, power_w: float, *, require_stopped: bool = True,
        before_enable: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Enable only after the prepared mode and latest power are confirmed."""
        config = self._config
        if before_enable is not None:
            await before_enable()
        self._power_value(power_w)
        expected = []
        if config.mode_entity_id is not None:
            expected.append((config.mode_entity_id, config.mode_feedback_entity_id, config.mode_engage_value, False))
        if config.power_entity_id is not None:
            expected.append((config.power_entity_id, config.power_feedback_entity_id, power_w, True))
        if require_stopped:
            expected.append((config.enable_entity_id, config.enable_feedback_entity_id, config.enable_disengage_value, False))
        mismatches = self._mismatches(expected, enable_feedback_value=config.enable_feedback_disengage_value)
        if mismatches:
            raise InverterConfirmationError(f"Could not confirm prepared inverter state: {', '.join(mismatches)}")
        # No further wait separates authorization/readback from the enable
        # write. The write and final readback share the advertised phase budget.
        await self._write_and_confirm(config.enable_entity_id, config.enable_engage_value,
                                      lambda: self.verify_engaged(power_w))

    async def _write_and_confirm(self, entity_id, value, confirm):
        written = False
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                await self._write(entity_id, value)
                written = True
                await confirm()
        except TimeoutError as error:
            if not written:
                raise
            raise InverterConfirmationError(f"Could not confirm inverter command: {entity_id}") from error

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
                    mismatches = self._mismatches(expected, enable_feedback_value=enable_feedback_value)
                    if not mismatches:
                        return
                    await asyncio.sleep(min(0.1, self._config.timeout_seconds / 5))
        except TimeoutError as error:
            raise InverterConfirmationError(
                f"Could not confirm inverter state: {', '.join(mismatches)}"
            ) from error

    def _mismatches(self, expected, *, enable_feedback_value=None):
        mismatches = []
        for command, feedback, value, is_power in expected:
            for entity_id in dict.fromkeys((command, feedback or command)):
                feedback_override = (command == self._config.enable_entity_id
                                     and entity_id == feedback and entity_id != command
                                     and enable_feedback_value is not None)
                expected_value = enable_feedback_value if feedback_override else value
                if not self._matches(entity_id, command, expected_value, is_power,
                                     normalize_switch=not feedback_override):
                    mismatches.append(entity_id)
        return mismatches

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
        if self._domain(command) in SUPPORTED_POWER_DOMAINS:
            # A numeric hold setpoint must not inherit the much wider measured
            # charging-power tolerance. Overrides are in the feedback's native
            # unit; no implicit W/kW conversion is safe for generic commands.
            try:
                actual, expected = _numeric_value(state.state), _numeric_value(value)
            except ValueError:
                return False
            return math.isclose(actual, expected, rel_tol=0., abs_tol=1e-6)
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

    def _validate_numeric_setpoint(self, entity_id: str, value: object) -> float:
        """Reject unavailable, out-of-range or unrepresentable commands."""
        number = _numeric_value(value)
        state = self._hass.states.get(entity_id)
        if not isinstance(state, State) or state.state.strip().lower() in _UNAVAILABLE:
            raise ValueError("Numeric inverter control is unavailable")
        minimum = state.attributes.get("min")
        maximum = state.attributes.get("max")
        step = state.attributes.get("step")
        if minimum is not None:
            minimum = _numeric_value(minimum)
            if number < minimum:
                raise ValueError("Numeric inverter command below minimum")
        if maximum is not None:
            maximum = _numeric_value(maximum)
            if number > maximum or (minimum is not None and minimum > maximum):
                raise ValueError("Numeric inverter command above maximum")
        if step is not None:
            step = _numeric_value(step)
            if step <= 0:
                raise ValueError("Invalid numeric inverter step")
            steps = (number - (minimum or 0.)) / step
            if not math.isfinite(steps) or not math.isclose(steps, round(steps), rel_tol=0., abs_tol=1e-6):
                raise ValueError("Numeric inverter command outside step lattice")
        return number

    async def _write(self, entity_id: str, value: object) -> None:
        domain = self._domain(entity_id)
        bridge = self._config.enable_reassert_entity_id if entity_id == self._config.enable_entity_id else None
        unchanged_numeric = bool(bridge and self._matches(entity_id, entity_id, value, False))
        if domain in {"input_select", "select"}:
            service = "select_option"
            data = {"entity_id": entity_id, "option": str(value)}
        elif domain in {"input_number", "number"}:
            service = "set_value"
            # Existing power-channel requests have their own watt conversion;
            # strict numeric command validation applies to enable/mode maps.
            number = (self._validate_numeric_setpoint(entity_id, value)
                      if entity_id in {self._config.enable_entity_id, self._config.mode_entity_id}
                      else float(value))
            data = {"entity_id": entity_id, "value": number}
        elif domain in {"switch", "input_boolean"}:
            service = "turn_on" if str(value).strip().lower() in _TRUTHY_STRINGS else "turn_off"
            data = {"entity_id": entity_id}
        else:
            raise ValueError(f"Unsupported entity domain for inverter grid charge: {domain}")
        async with asyncio.timeout(self._config.timeout_seconds):
            await self._hass.services.async_call(domain, service, data, blocking=True)
            if bridge and unchanged_numeric:
                # A YAML input_number bridge commonly listens to state_changed.
                # Repeating an already-matching helper value emits no such event.
                # Explicitly rerun only its configured bridge when the physical
                # setpoint still differs, without pulsing any alternate value.
                config = self._config
                if _numeric_value(value) == _numeric_value(config.enable_engage_value):
                    expected = config.enable_feedback_engage_value
                else:
                    expected = config.enable_feedback_disengage_value
                expected = value if expected is None else expected
                if not self._matches(config.enable_feedback_entity_id, entity_id, expected, False):
                    if not self._matches(entity_id, entity_id, value, False):
                        raise InverterConfirmationError("Numeric helper changed before bridge reassertion")
                    state = self._hass.states.get(bridge)
                    if not isinstance(state, State) or state.state.strip().lower() in _UNAVAILABLE:
                        raise ValueError("Numeric command bridge is unavailable")
                    await self._hass.services.async_call(
                        "automation", "trigger",
                        {"entity_id": bridge, "skip_condition": False}, blocking=True,
                    )

    @staticmethod
    def _domain(entity_id: str) -> str:
        return entity_id.split(".", 1)[0]

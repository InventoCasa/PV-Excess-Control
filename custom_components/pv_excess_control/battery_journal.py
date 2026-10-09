"""Durable records of owned battery controls and their original actuator maps.

A confirmed disk record must precede a command that requires later cleanup.
Reload recovery uses these mappings even if the current options have changed.
"""
from __future__ import annotations

from dataclasses import asdict, fields
import json
import math
import os

from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.helpers.storage import Store

from .inverter_control import SUPPORTED_ENABLE_DOMAINS, SUPPORTED_POWER_DOMAINS
from .models import InverterGridChargeConfig


_CONFIG_FIELDS = frozenset(field.name for field in fields(InverterGridChargeConfig))
_NUMERIC_FIELDS = frozenset({"timeout_seconds", "power_tolerance_w"})
_ON_VALUES = frozenset({"on", "true", "1", "yes"})
_OFF_VALUES = frozenset({"off", "false", "0", "no"})


def _validate_existing_json(path: str) -> bool:
    """Preserve a corrupt ownership record at its path for every restart.

    HA's general storage loader renames corrupt JSON and treats it as missing.
    An ownership journal must keep blocking recovery until repaired instead.
    """
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as record:
        json.load(record)
    return True


def _decode_config(raw: object) -> InverterGridChargeConfig | None:
    """Reject malformed cleanup instructions instead of forgetting ownership."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - _CONFIG_FIELDS:
        raise ValueError("Invalid battery ownership controller mapping")
    try:
        config = InverterGridChargeConfig(**raw)
    except TypeError as error:
        raise ValueError("Incomplete battery ownership controller mapping") from error
    for name in _CONFIG_FIELDS - _NUMERIC_FIELDS:
        value = getattr(config, name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"Invalid battery ownership field: {name}")
        if name.endswith("entity_id") and value is not None and not valid_entity_id(value):
            raise ValueError(f"Invalid battery ownership entity: {name}")
    for name in _NUMERIC_FIELDS:
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Invalid battery ownership number: {name}")
    if config.timeout_seconds <= 0 or config.power_tolerance_w < 0:
        raise ValueError("Invalid battery ownership confirmation limits")
    if config.enable_entity_id is None:
        raise ValueError("Battery ownership enable entity is required")
    for entity, engage, release in (
        (config.enable_entity_id, config.enable_engage_value, config.enable_disengage_value),
        (config.mode_entity_id, config.mode_engage_value, config.mode_disengage_value),
    ):
        if entity is None:
            continue
        domain = entity.split(".", 1)[0]
        if domain not in SUPPORTED_ENABLE_DOMAINS:
            raise ValueError("Unsupported battery ownership command entity")
        if not engage or not release or engage == release:
            raise ValueError("Battery ownership engage and release commands must differ")
        if domain in {"switch", "input_boolean"}:
            engage, release = engage.strip().lower(), release.strip().lower()
            if (engage not in _ON_VALUES | _OFF_VALUES or release not in _ON_VALUES | _OFF_VALUES
                    or (engage in _ON_VALUES) == (release in _ON_VALUES)):
                raise ValueError("Battery ownership binary commands must select opposite states")
    if config.power_entity_id and config.power_entity_id.split(".", 1)[0] not in SUPPORTED_POWER_DOMAINS:
        raise ValueError("Unsupported battery ownership power entity")
    return config


class BatteryOwnershipJournal:
    """Persist cleanup ownership independently from delayed config-entry writes."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._key = f"pv_excess_control.{entry_id}.battery_ownership"
        self._store = Store(hass, 1, self._key)
        self._loaded_record_exists = False

    @property
    def loaded_record_exists(self) -> bool:
        """Distinguish an authoritative cleared record from a missing legacy file."""
        return self._loaded_record_exists

    async def async_load(self) -> tuple[InverterGridChargeConfig | None, InverterGridChargeConfig | None]:
        """Restore original mappings, failing closed for an unreadable record."""
        self._loaded_record_exists = False
        existed = await self._hass.async_add_executor_job(_validate_existing_json, self._store.path)
        saved = await self._store.async_load()
        if saved is None:
            if existed:
                raise ValueError("Battery ownership journal is unreadable")
            return None, None
        if not isinstance(saved, dict) or set(saved) != {"charge", "hold"}:
            raise ValueError("Invalid battery ownership journal structure")
        restored = _decode_config(saved["charge"]), _decode_config(saved["hold"])
        self._loaded_record_exists = True
        return restored

    async def async_save(
        self,
        charge_config: InverterGridChargeConfig | None,
        hold_config: InverterGridChargeConfig | None,
    ) -> None:
        """Await storage and confirm persistence before allowing hardware writes.

        Store logs some write failures instead of raising them, and may defer
        writes during shutdown. A fresh Store avoids its pending in-memory data
        when checking that the requested ownership is actually recoverable.
        """
        payload = {}
        for name, config in (("charge", charge_config), ("hold", hold_config)):
            if config is not None and not isinstance(config, InverterGridChargeConfig):
                raise ValueError("Invalid battery ownership controller configuration")
            raw = asdict(config) if config is not None else None
            _decode_config(raw)
            payload[name] = raw
        await self._store.async_save(payload)
        readback = Store(self._hass, 1, self._key)
        await self._hass.async_add_executor_job(_validate_existing_json, readback.path)
        persisted = await readback.async_load()
        if persisted != payload:
            raise OSError("Battery ownership journal could not be persisted")

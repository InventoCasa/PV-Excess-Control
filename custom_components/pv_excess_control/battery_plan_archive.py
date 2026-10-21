"""Lossless, bounded archives of published battery plans for Recorder history.

Expanded plans remain live attributes. This optional archive preserves their
JSON values without forcing Recorder to discard the actual control history.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
import json
import math
import zlib

_VERSION = 1
_ENCODING = "json+zlib+base64"
_MAX_ARCHIVE_BYTES = 12288
_MAX_DECODED_BYTES = 256 * 1024
_MAX_SLOTS = 576


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _valid_json(value) -> bool:
    pending = [(value, 0)]
    nodes = text_length = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 32768 or depth > 64:
            return False
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                return False
            text_length += sum(len(key) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            text_length += len(item)
        elif isinstance(item, float):
            if not math.isfinite(item):
                return False
        elif item is not None and not isinstance(item, (bool, int)):
            return False
        if text_length > _MAX_DECODED_BYTES:
            return False
    return True


def _valid_payload(payload) -> bool:
    return (isinstance(payload, dict)
            and set(payload) in ({"primary"}, {"primary", "lower"})
            and all(isinstance(rows, list) and len(rows) <= _MAX_SLOTS
                    and all(isinstance(row, dict) for row in rows)
                    for rows in payload.values())
            and _valid_json(payload))


def _unavailable(reason: str) -> dict:
    # Remains below128 bytes even when the caller has no archive budget left.
    return {"version": _VERSION, "encoding": _ENCODING, "status": "unavailable",
            "included": [], "reason": reason}


def _pack(payload: dict, budget: int, *, partial: bool = False) -> dict | None:
    raw = _json_bytes(payload)
    if len(raw) > _MAX_DECODED_BYTES:
        return None
    result = {"version": _VERSION, "encoding": _ENCODING,
              "status": "partial" if partial else "available",
              "included": list(payload),
              "data": base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")}
    if partial:
        result["reason"] = "size_limit"
    return result if len(_json_bytes(result)) <= budget else None


def encode_battery_plan_archive(primary, lower=None, *, max_bytes: int = _MAX_ARCHIVE_BYTES) -> dict:
    """Archive complete plans, then primary only, or explicit unavailability.

    The budget includes the JSON envelope and is capped at12288 bytes. A small
    unavailable envelope is returned even for a zero budget; callers reserve
    128 bytes for that outcome. Invalid optional input never raises into the
    sensor update. Inputs are not changed and no plan values are rounded.
    """
    if type(max_bytes) is not int or max_bytes < 0:
        return _unavailable("invalid_budget")
    budget = min(max_bytes, _MAX_ARCHIVE_BYTES)
    if budget == 0:
        return _unavailable("size_limit")
    payload = {"primary": primary}
    if lower is not None:
        payload["lower"] = lower
    try:
        if not _valid_payload(payload):
            return _unavailable("invalid_plan")
        complete = _pack(payload, budget)
        if complete is not None:
            return complete
        if lower is not None:
            partial = _pack({"primary": primary}, budget, partial=True)
            if partial is not None:
                return partial
    except (ValueError, TypeError, OverflowError, RecursionError):
        return _unavailable("invalid_plan")
    return _unavailable("size_limit")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate archive JSON key")
        result[key] = value
    return result


def decode_battery_plan_archive(archive) -> dict:
    """Decode a bounded complete/partial archive, or raise a controlled error.

    Unknown versions, oversized input, incomplete/trailing compressed streams,
    decompression bombs and malformed payloads are rejected. Missing archives
    never become an empty plan. Returned values reproduce the published JSON.
    """
    try:
        if not isinstance(archive, Mapping):
            raise ValueError("invalid archive envelope")
        if set(archive) - {"version", "encoding", "status", "included", "data", "reason"}:
            raise ValueError("unknown archive envelope fields")
        if type(archive.get("version")) is not int or archive["version"] != _VERSION:
            raise ValueError("unsupported archive version")
        if archive.get("encoding") != _ENCODING:
            raise ValueError("unsupported archive encoding")
        status, included = archive.get("status"), archive.get("included")
        if status not in ("available", "partial"):
            raise ValueError("archive unavailable")
        if included not in (["primary"], ["primary", "lower"]):
            raise ValueError("invalid included plans")
        if status == "partial" and (included != ["primary"] or archive.get("reason") != "size_limit"):
            raise ValueError("invalid partial archive")
        data = archive.get("data")
        if not isinstance(data, str) or len(data) > _MAX_ARCHIVE_BYTES:
            raise ValueError("invalid archive data")
        if len(_json_bytes(dict(archive))) > _MAX_ARCHIVE_BYTES:
            raise ValueError("archive exceeds size limit")
        compressed = base64.b64decode(data, validate=True)
        stream = zlib.decompressobj()
        raw = stream.decompress(compressed, _MAX_DECODED_BYTES + 1)
        if (len(raw) > _MAX_DECODED_BYTES or stream.unconsumed_tail
                or stream.unused_data or not stream.eof):
            raise ValueError("invalid or oversized compressed archive")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not _valid_payload(payload) or list(payload) != included:
            raise ValueError("invalid archive plans")
        return payload
    except (ValueError, TypeError, OverflowError, RecursionError, zlib.error, binascii.Error) as exc:
        raise ValueError("Invalid or unavailable battery plan archive") from exc

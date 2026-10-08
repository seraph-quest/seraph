"""Closed A1.1 owned-pipe protocol. No service dispatch or ownership epoch yet."""
from __future__ import annotations

import asyncio
import json
import math
import re
import struct
from typing import Any

MAX_FRAME = 1_048_576
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_PENDING = 32
CONTROL_TIMEOUT = 5.0
METHODS = frozenset({"bootstrap.hello", "runtime.ready", "runtime.status", "runtime.quiesce", "runtime.shutdown", "invocation.cancel"})
FIELDS = frozenset({"protocol", "boot_nonce", "request_id", "seq", "kind", "method", "invocation_ref", "composition_epoch", "composition_digest", "package_digest", "deadline_at", "payload"})
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class ProtocolError(ValueError):
    """A boot must be fenced; malformed input must never dispatch."""


def integer(value: Any, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError("invalid integer")
    return value


def closed(value: Any, fields: set[str] | frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or value.keys() != fields:
        raise ProtocolError("unknown or missing fields")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("duplicate JSON key")
        result[key] = value
    return result


def _tree(value: Any, depth: int = 1, count: list[int] | None = None) -> None:
    count = count if count is not None else [0]
    count[0] += 1
    if depth > 16 or count[0] > 4096:
        raise ProtocolError("JSON complexity exceeded")
    if type(value) is float and not math.isfinite(value):
        raise ProtocolError("nonfinite JSON number")
    if isinstance(value, dict):
        for key, item in value.items():
            _tree(key, depth + 1, count)
            _tree(item, depth + 1, count)
    elif isinstance(value, list):
        for item in value:
            _tree(item, depth + 1, count)


def decode_json(data: bytes) -> Any:
    if not 1 <= len(data) <= MAX_FRAME:
        raise ProtocolError("frame size exceeded")
    try:
        value = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("nonfinite JSON number")))
        _tree(value)
        return value
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError("invalid UTF-8 JSON") from exc


def _plugins(value: Any) -> None:
    if type(value) is not list or len(value) > 64:
        raise ProtocolError("invalid plugins")
    seen: set[str] = set()
    for plugin in value:
        closed(plugin, {"id", "state", "reason"})
        identifier = plugin["id"]
        if type(identifier) is not str or not 1 <= len(identifier) <= 128 or identifier in seen:
            raise ProtocolError("invalid plugin identity")
        seen.add(identifier)
        if type(plugin["state"]) is not str or plugin["state"] not in {"ready", "blocked", "stopped"}:
            raise ProtocolError("invalid plugin state")
        if plugin["reason"] is not None and (type(plugin["reason"]) is not str or len(plugin["reason"]) > 256):
            raise ProtocolError("invalid plugin reason")


def validate_frame(value: Any) -> dict[str, Any]:
    frame = closed(value, FIELDS)
    integer(frame["protocol"], 1, 1)
    integer(frame["seq"], 1)
    integer(frame["deadline_at"], 1)
    for field in ("boot_nonce", "composition_digest", "package_digest"):
        if type(frame[field]) is not str or not HEX64.fullmatch(frame[field]):
            raise ProtocolError("invalid boot identity")
    if type(frame["request_id"]) is not str or not TOKEN.fullmatch(frame["request_id"]):
        raise ProtocolError("invalid request identity")
    method, kind = frame["method"], frame["kind"]
    if type(method) is not str or method not in METHODS or type(kind) is not str or kind not in {"request", "response"}:
        raise ProtocolError("unknown method or kind")
    if frame["composition_epoch"] is not None:
        raise ProtocolError("A1.1 controls have no ownership epoch")
    if method == "invocation.cancel":
        if type(frame["invocation_ref"]) is not str or not TOKEN.fullmatch(frame["invocation_ref"]):
            raise ProtocolError("invalid invocation reference")
    elif frame["invocation_ref"] is not None:
        raise ProtocolError("lifecycle controls have no invocation")
    payload = frame["payload"]
    if kind == "request":
        if method == "runtime.ready":
            raise ProtocolError("ready is a response only")
        closed(payload, set())
    elif method == "runtime.ready":
        closed(payload, {"state", "plugins"})
        if payload["state"] != "ready":
            raise ProtocolError("invalid readiness")
        _plugins(payload["plugins"])
    elif method == "runtime.status":
        closed(payload, {"state", "plugins", "resources_remaining"})
        if type(payload["state"]) is not str or payload["state"] not in {"ready", "quiescing"}:
            raise ProtocolError("invalid runtime state")
        _plugins(payload["plugins"])
        integer(payload["resources_remaining"], 0, 4096)
    elif method == "runtime.quiesce":
        closed(payload, {"state"})
        if payload["state"] != "quiescing":
            raise ProtocolError("invalid quiescence")
    elif method == "runtime.shutdown":
        closed(payload, {"state", "resources_remaining", "cordis_disposal"})
        if payload["state"] != "stopped" or type(payload["cordis_disposal"]) is not str or payload["cordis_disposal"] not in {"confirmed", "unconfirmed"}:
            raise ProtocolError("invalid shutdown")
        integer(payload["resources_remaining"], 0, 4096)
    elif method == "invocation.cancel":
        closed(payload, {"cancelled"})
        if type(payload["cancelled"]) is not bool:
            raise ProtocolError("invalid cancellation")
    else:
        raise ProtocolError("hello is a request only")
    return frame


def encode_frame(value: dict[str, Any]) -> bytes:
    validate_frame(value)
    _tree(value)
    data = json.dumps(value, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")
    if not 1 <= len(data) <= MAX_FRAME:
        raise ProtocolError("frame size exceeded")
    return struct.pack(">I", len(data)) + data


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        header = await reader.readexactly(4)
        length = struct.unpack(">I", header)[0]
        if not 1 <= length <= MAX_FRAME:
            raise ProtocolError("frame size exceeded")
        return validate_frame(decode_json(await reader.readexactly(length)))
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError("owned pipe lost or incomplete frame") from exc

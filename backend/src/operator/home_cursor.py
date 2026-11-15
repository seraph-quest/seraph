"""Bounded, stateless Home keyset cursor; never an ownership grant."""
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import re
import struct


class HomeCursorError(ValueError):
    def __init__(self, code="continuation_invalid"):
        self.code = code
        super().__init__(code)


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
NULL_DUE = (1 << 63) - 1
HEADER = struct.Struct(">BqqB8s16s16sBqqqBH")
KINDS = ("active_goal", "programme", "task_next_action", "prepared_output", "approval",
    "blocked_task", "blocked_approval", "blocked_programme", "inbox_decision")
DESC_BANDS = {0, 1, 7}


def micros(value: datetime) -> int:
    value = value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
    delta = value - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def timestamp(value: int) -> datetime:
    from datetime import timedelta
    try:
        return EPOCH + timedelta(microseconds=value)
    except (OverflowError, ValueError):
        raise HomeCursorError() from None


def source_id(value: str, *, task=False) -> bytes:
    if not isinstance(value, str) or not value:
        raise HomeCursorError("unsupported_source")
    try:
        raw = value.encode("ascii" if task else "utf-8")
    except UnicodeError:
        raise HomeCursorError("unsupported_source") from None
    if len(raw) > 512:
        raise HomeCursorError("unsupported_source")
    if task and (not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,512}", value)
            or value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise HomeCursorError("unsupported_source")
    return raw


def programme_key(goal_id: str, programme_id: str, revision: int) -> bytes:
    goal = source_id(goal_id)
    if (len(programme_id) != 32 or any(c not in "0123456789abcdef" for c in programme_id)
            or type(revision) is not int or not 0 < revision < 1 << 64):
        raise HomeCursorError("unsupported_source")
    return struct.pack(">H", len(goal)) + goal + programme_id.encode("ascii") + struct.pack(">Q", revision)


def inbox_key(identifier, revision, commitment):
    raw=source_id(identifier)
    if type(revision) is not int or not 0<revision<1<<63 or not isinstance(commitment,bytes) or len(commitment)!=32:
        raise HomeCursorError("unsupported_source")
    return struct.pack(">H",len(raw))+raw+struct.pack(">Q",revision)+commitment


def decode_inbox_key(key):
    try:
        size=struct.unpack(">H",key[:2])[0]
        if len(key)!=2+size+8+32: raise ValueError()
        identifier=key[2:2+size].decode("utf-8")
        revision=struct.unpack(">Q",key[2+size:2+size+8])[0]
        if inbox_key(identifier,revision,key[-32:])!=key: raise ValueError()
        return identifier,revision,key[-32:]
    except (ValueError,UnicodeError,struct.error):
        raise HomeCursorError() from None


@dataclass(frozen=True)
class Position:
    band: int
    value: int
    due_us: int
    source_us: int
    kind: int
    key: bytes

    def compare(self, other):
        left, right = (self.band,), (other.band,)
        if left != right:
            return -1 if left < right else 1
        if self.value != other.value:
            result = -1 if self.value < other.value else 1
            return -result if self.band in DESC_BANDS else result
        left = (self.due_us, self.source_us, self.kind, self.key)
        right = (other.due_us, other.source_us, other.kind, other.key)
        return (left > right) - (left < right)


class CursorCodec:
    def __init__(self, server_key: bytes):
        self.key = hmac.digest(server_key, b"seraph-home-continuation-v1", "sha256")
        self.key_id = hmac.digest(self.key, b"seraph-home-continuation-key-id-v1", "sha256")[:8]

    def binding(self, domain, value):
        return hmac.digest(self.key, domain + value.encode("utf-8"), "sha256")[:16]

    def root_binding(self, root, identity, context):
        # NULL is a distinct historical scope, never an omitted identity.
        identity_bytes = b"N" if identity is None else b"S" + identity.encode("utf-8")
        return hmac.digest(self.key, b"root-identity:" + root.encode("utf-8") + b"\x00" + identity_bytes
            + b"\x00context:" + context.encode("utf-8"), "sha256")[:16]

    def encode(self, *, as_of, expires, limit, principal, root, identity, context, position):
        raw = HEADER.pack(1, as_of, expires, limit, self.key_id,
            self.binding(b"owner:", principal), self.root_binding(root, identity, context),
            position.band, position.value, position.due_us, position.source_us,
            position.kind, len(position.key)) + position.key
        raw += hmac.digest(self.key, b"cursor:" + raw, "sha256")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii")
        if len(encoded) > 1024:
            raise HomeCursorError("unsupported_source")
        return encoded

    def decode(self, encoded, *, now, limit, principal, root, identity, context):
        if not isinstance(encoded, str) or not 1 <= len(encoded) <= 1024:
            raise HomeCursorError()
        try:
            raw = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(raw).decode("ascii") != encoded or len(raw) < HEADER.size + 33:
                raise ValueError()
            fields = HEADER.unpack(raw[:HEADER.size])
            version, as_of, expires, recorded_limit, key_id, owner, original_root, band, value, due, source, kind, size = fields
            if (version != 1 or recorded_limit != limit or key_id != self.key_id or band > 7
                    or kind >= len(KINDS) or len(raw) != HEADER.size + size + 32
                    or not hmac.compare_digest(owner, self.binding(b"owner:", principal))
                    or not hmac.compare_digest(raw[-32:], hmac.digest(self.key, b"cursor:" + raw[:-32], "sha256"))):
                raise ValueError()
            if not hmac.compare_digest(original_root, self.root_binding(root, identity, context)):
                raise HomeCursorError("continuation_stale")
            if not as_of <= now < expires or expires - as_of > 300_000_000:
                raise HomeCursorError("continuation_expired")
            timestamp(as_of); timestamp(expires); timestamp(source)
            if due != NULL_DUE:
                timestamp(due)
            key = raw[HEADER.size:-32]
            if kind==8:
                decode_inbox_key(key)
            elif kind in {1, 7}:
                length = struct.unpack(">H", key[:2])[0]
                goal = key[2:2+length].decode("utf-8")
                programme = key[2+length:2+length+32].decode("ascii")
                revision = struct.unpack(">Q", key[2+length+32:])[0]
                if programme_key(goal, programme, revision) != key:
                    raise ValueError()
            else:
                identity = key.decode("ascii" if kind in {2, 3, 5} else "utf-8")
                if source_id(identity, task=kind in {2, 3, 5}) != key:
                    raise ValueError()
            return as_of, expires, Position(band, value, due, source, kind, key)
        except HomeCursorError:
            raise
        except (ValueError, UnicodeError, struct.error):
            raise HomeCursorError() from None

"""Append-only execution-v2 WAL kernel.

Stdlib plus frozen EV2 contracts/state machine only. Import and construction
perform no filesystem, network, env, thread, task, Sentry, projector or
exporter work. Enqueue never opens, writes, flushes or fsyncs. Durability is
an explicit drain/fsync ack.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Deque, Mapping, Optional, Sequence, Tuple

from app.bot.execution.contracts import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
    ContractValidationError,
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    Venue,
)
from app.bot.execution.state_machine import (
    InvalidTransition,
    apply_events,
    initial_spread_state,
)

SCHEMA_VERSION = "bbot.execution.wal.v2"
GENESIS_HASH = "0" * 64
WAL_FILENAME = "wal.jsonl"
WAL_DIRNAME = "wal.v2"
SUBMIT_WORST_CASE_EVENTS = 5
RECORD_KEYS = (
    "schema_version",
    "wal_seq",
    "run_id",
    "prev_hash",
    "event",
    "event_content_hash",
    "event_sequence_hash",
    "record_hash",
)
CRASH_BEFORE_WRITE = "before_write"
CRASH_AFTER_WRITE_BEFORE_FLUSH = "after_write_before_flush"
CRASH_AFTER_FLUSH_BEFORE_FSYNC = "after_flush_before_fsync"
CRASH_TORN_WRITE = "torn_write"
CRASH_AFTER_FSYNC_BEFORE_ACK = "after_fsync_before_ack"
CRASH_STAGES = (
    CRASH_BEFORE_WRITE,
    CRASH_AFTER_WRITE_BEFORE_FLUSH,
    CRASH_AFTER_FLUSH_BEFORE_FSYNC,
    CRASH_TORN_WRITE,
    CRASH_AFTER_FSYNC_BEFORE_ACK,
)
_DENIED_PATH_PREFIXES = (
    "/data/live",
    "/data/bars",
    "/data/bbot/journal",
)
_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "passphrase",
        "password",
        "authorization",
        "cookie",
        "set_cookie",
        "signature",
        "sign",
        "access_token",
        "refresh_token",
        "bearer_token",
        "private_key",
        "client_secret",
        "raw_payload",
        "raw_frame",
        "frame",
        "request_body",
        "response_body",
        "headers",
        "canonical_request",
        "account_id",
        "uid",
        "member_id",
        "wallet_address",
        "exchange_order_id",
        "order_id",
        "orderid",
        "client_order_id",
        "clordid",
        "ordid",
        "execid",
        "exec_id",
        "balance",
        "available_balance",
        "equity",
        "margin",
        "account_value",
        "fill_price",
    }
)
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
_HASH_CHARS = frozenset("0123456789abcdef")

CrashHook = Callable[[str], None]
ClockNs = Callable[[], int]
FsyncFn = Callable[[int], None]


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _assert_public(node: object, *, path: str = "$") -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            nk = _norm_key(key)
            if nk in _FORBIDDEN_PUBLIC_KEYS:
                raise WalError("forbidden_field")
            _assert_public(value, path=f"{path}.{nk}")
        return
    if isinstance(node, (list, tuple)):
        for i, item in enumerate(node):
            _assert_public(item, path=f"{path}[{i}]")


def _redact_text(text: str) -> str:
    lowered = text.lower()
    for key in _FORBIDDEN_PUBLIC_KEYS:
        if key in lowered:
            return "redacted"
    return text


def _require_run_id(value: object) -> str:
    if not isinstance(value, str) or not value or value[0] in "._:-":
        raise WalError("invalid_run_id")
    if len(value) > 128 or any(ch not in _ID_CHARS for ch in value):
        raise WalError("invalid_run_id")
    return value


def _require_positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WalError("invalid_config", field=field)
    return value


def _require_nonneg_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WalError("invalid_config", field=field)
    return value


def _require_hash(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise WalIntegrityError("bad_hash", field=field)
    if any(ch not in _HASH_CHARS for ch in value):
        raise WalIntegrityError("bad_hash", field=field)
    return value


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _record_hash_for(envelope: Mapping[str, Any]) -> str:
    body = {key: envelope[key] for key in RECORD_KEYS if key != "record_hash"}
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _is_open_intent_accepted(event: ExecutionEvent) -> bool:
    return (
        event.event_type is ExecutionEventType.INTENT_ACCEPTED
        and event.payload.get("action") == IntentAction.OPEN.value
    )


def _qualifying_recon_venue(event: ExecutionEvent) -> Optional[Venue]:
    if event.event_type is not ExecutionEventType.RECONCILIATION:
        return None
    if event.payload.get("matched") is not True:
        return None
    venue = event.venue
    if venue is not Venue.OKX and venue is not Venue.BYBIT:
        return None
    if event.leg_id is None:
        return venue
    if isinstance(event.leg_id, str) and event.leg_id:
        return venue
    return None


def admission_capacity_ok(
    *,
    queue_depth: int,
    max_queue: int,
    reserved_tail: int,
    count: int,
    open_intent: bool,
    scanned: bool,
    hard_full: bool,
    integrity_unhealthy: bool,
) -> bool:
    """Pure enqueue-admission check. No mutation, disk, or drain."""
    if not scanned or hard_full or integrity_unhealthy:
        return False
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return False
    if (
        isinstance(queue_depth, bool)
        or not isinstance(queue_depth, int)
        or queue_depth < 0
    ):
        return False
    if isinstance(max_queue, bool) or not isinstance(max_queue, int) or max_queue < 1:
        return False
    if (
        isinstance(reserved_tail, bool)
        or not isinstance(reserved_tail, int)
        or reserved_tail < 0
        or reserved_tail >= max_queue
    ):
        return False
    if count == 0:
        return True
    if queue_depth + count > max_queue:
        return False
    if open_intent and queue_depth + count > (max_queue - reserved_tail):
        return False
    return True


def _denied_path(path: Path) -> bool:
    posix = path.as_posix()
    parts = path.parts
    for prefix in _DENIED_PATH_PREFIXES:
        if posix == prefix or posix.startswith(prefix + "/"):
            return True
        prefix_parts = tuple(part for part in prefix.split("/") if part)
        if len(parts) >= len(prefix_parts):
            for index in range(0, len(parts) - len(prefix_parts) + 1):
                if parts[index : index + len(prefix_parts)] == prefix_parts:
                    return True
    return False


def _validate_wal_path(path: Path) -> Path:
    if path.name != WAL_FILENAME or path.parent.name != WAL_DIRNAME:
        raise WalError("invalid_path")
    if _denied_path(path):
        raise WalError("denied_path")
    return path


class WalError(ValueError):
    """Fail-closed WAL error. Public text is redacted."""

    def __init__(self, code: str, **_ignored: object) -> None:
        self.code = _redact_text(str(code))
        super().__init__(f"wal error code={self.code}")

    def to_public_dict(self) -> dict[str, Any]:
        out = {"code": self.code}
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return f"WalError(code={self.code!r})"


class WalIntegrityError(WalError):
    """Hash-chain, sequence or schema integrity failure."""

    def __repr__(self) -> str:
        return f"WalIntegrityError(code={self.code!r})"


def _validate_wal_record(record: "WalRecord") -> None:
    if record.schema_version != SCHEMA_VERSION:
        raise WalIntegrityError("schema_mismatch")
    if isinstance(record.wal_seq, bool) or not isinstance(record.wal_seq, int) or record.wal_seq < 1:
        raise WalIntegrityError("bad_wal_seq")
    _require_run_id(record.run_id)
    _require_hash(record.prev_hash, field="prev_hash")
    if not isinstance(record.event, ExecutionEvent):
        raise WalError("invalid_event")
    if record.event.run_id != record.run_id:
        raise WalIntegrityError("run_mismatch")
    if record.event.schema_version != CONTRACT_SCHEMA_VERSION:
        raise WalIntegrityError("invalid_event_schema")
    content_hash = _require_hash(record.event_content_hash, field="event_content_hash")
    sequence_hash = _require_hash(record.event_sequence_hash, field="event_sequence_hash")
    if record.event.content_hash() != content_hash or record.event.sequence_hash() != sequence_hash:
        raise WalIntegrityError("event_hash_mismatch")
    stored_hash = _require_hash(record.record_hash, field="record_hash")
    envelope = {
        "schema_version": record.schema_version,
        "wal_seq": record.wal_seq,
        "run_id": record.run_id,
        "prev_hash": record.prev_hash,
        "event": record.event.to_public_dict(),
        "event_content_hash": content_hash,
        "event_sequence_hash": sequence_hash,
    }
    if stored_hash != _record_hash_for(envelope):
        raise WalIntegrityError("bad_hash")


def require_wal_record(record: object) -> "WalRecord":
    if not isinstance(record, WalRecord):
        raise WalError("invalid_record")
    _validate_wal_record(record)
    public = record.to_public_dict()
    if set(public) != set(RECORD_KEYS):
        raise WalIntegrityError("exact_key_violation")
    return record


@dataclass(frozen=True)
class WalRecord:
    schema_version: str
    wal_seq: int
    run_id: str
    prev_hash: str
    event: ExecutionEvent
    event_content_hash: str
    event_sequence_hash: str
    record_hash: str

    def __post_init__(self) -> None:
        _validate_wal_record(self)
        public = {
            "schema_version": self.schema_version,
            "wal_seq": self.wal_seq,
            "run_id": self.run_id,
            "prev_hash": self.prev_hash,
            "event": self.event.to_public_dict(),
            "event_content_hash": self.event_content_hash,
            "event_sequence_hash": self.event_sequence_hash,
            "record_hash": self.record_hash,
        }
        if set(public) != set(RECORD_KEYS):
            raise WalIntegrityError("exact_key_violation")
        _assert_public(public)

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": self.schema_version,
            "wal_seq": self.wal_seq,
            "run_id": self.run_id,
            "prev_hash": self.prev_hash,
            "event": self.event.to_public_dict(),
            "event_content_hash": self.event_content_hash,
            "event_sequence_hash": self.event_sequence_hash,
            "record_hash": self.record_hash,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "WalRecord("
            f"wal_seq={self.wal_seq}, run_id={self.run_id!r}, "
            f"event_type={self.event.event_type.value!r}, "
            f"record_hash={self.record_hash!r})"
        )


@dataclass(frozen=True)
class WalAppendAck:
    accepted: bool
    durable: bool
    wal_seq: Optional[int]
    reason_code: Optional[str]

    def __post_init__(self) -> None:
        if self.durable:
            raise WalError("enqueue_not_durable")
        if self.accepted and self.wal_seq is None:
            raise WalError("missing_wal_seq")
        if not self.accepted and self.wal_seq is not None:
            raise WalError("nack_must_not_consume_seq")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "accepted": self.accepted,
            "durable": False,
            "wal_seq": self.wal_seq,
            "reason_code": self.reason_code,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "WalAppendAck("
            f"accepted={self.accepted}, durable=False, "
            f"wal_seq={self.wal_seq}, reason_code={self.reason_code!r})"
        )


@dataclass(frozen=True)
class WalDurableAck:
    durable: bool
    wal_seq: int
    run_id: str
    record_hash: str
    reconciliation_token: Optional[str]

    def __post_init__(self) -> None:
        if not self.durable:
            raise WalError("durable_ack_required")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "durable": True,
            "wal_seq": self.wal_seq,
            "run_id": self.run_id,
            "record_hash": self.record_hash,
            "reconciliation_token": self.reconciliation_token,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "WalDurableAck("
            f"durable=True, wal_seq={self.wal_seq}, run_id={self.run_id!r})"
        )


@dataclass(frozen=True)
class WalHealth:
    queue_depth: int
    next_wal_seq: int
    durable_wal_seq: int
    durable_lag: int
    writer_unhealthy: bool
    integrity_unhealthy: bool
    hard_full: bool
    venue_reconciliation_complete: bool
    blocks_opens: bool
    torn_tail: bool
    fsync_count: int

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "queue_depth": self.queue_depth,
            "next_wal_seq": self.next_wal_seq,
            "durable_wal_seq": self.durable_wal_seq,
            "durable_lag": self.durable_lag,
            "writer_unhealthy": self.writer_unhealthy,
            "integrity_unhealthy": self.integrity_unhealthy,
            "hard_full": self.hard_full,
            "venue_reconciliation_complete": self.venue_reconciliation_complete,
            "blocks_opens": self.blocks_opens,
            "torn_tail": self.torn_tail,
            "fsync_count": self.fsync_count,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "WalHealth("
            f"queue_depth={self.queue_depth}, durable_lag={self.durable_lag}, "
            f"blocks_opens={self.blocks_opens})"
        )


@dataclass(frozen=True)
class ReplayResult:
    state: Any
    records: Tuple[WalRecord, ...]
    durable_watermark: int
    torn_tail: bool
    opens_allowed: bool
    requires_venue_reconciliation: bool
    integrity_ok: bool

    def __post_init__(self) -> None:
        if self.opens_allowed:
            raise WalError("replay_must_not_allow_opens")
        if not self.requires_venue_reconciliation:
            raise WalError("replay_requires_venue_reconciliation")

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "durable_watermark": self.durable_watermark,
            "torn_tail": self.torn_tail,
            "opens_allowed": False,
            "requires_venue_reconciliation": True,
            "integrity_ok": self.integrity_ok,
            "record_count": len(self.records),
            "state": self.state.to_public_dict(),
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "ReplayResult("
            f"durable_watermark={self.durable_watermark}, torn_tail={self.torn_tail}, "
            "opens_allowed=False, requires_venue_reconciliation=True)"
        )


@dataclass(frozen=True)
class ProjectionAck:
    applied: bool
    duplicate: bool
    watermark: int
    run_id: str
    wal_seq: int
    record_hash: str

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "applied": self.applied,
            "duplicate": self.duplicate,
            "watermark": self.watermark,
            "run_id": self.run_id,
            "wal_seq": self.wal_seq,
            "record_hash": self.record_hash,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "ProjectionAck("
            f"applied={self.applied}, duplicate={self.duplicate}, "
            f"watermark={self.watermark})"
        )


@dataclass(frozen=True)
class _PendingRecord:
    wal_seq: int
    event: ExecutionEvent
    record: WalRecord
    line: bytes


class InMemoryProjector:
    """Idempotent in-memory projection boundary. No database driver or DSN."""

    def __init__(self) -> None:
        self._watermark = 0
        self._run_id: Optional[str] = None
        self._hashes: dict[Tuple[str, int], str] = {}
        self._records: list[WalRecord] = []
        self._failed = False

    def apply(self, record: WalRecord) -> ProjectionAck:
        try:
            record = require_wal_record(record)
        except WalError:
            self._failed = True
            raise
        key = (record.run_id, record.wal_seq)
        existing = self._hashes.get(key)
        if existing is not None:
            if existing != record.record_hash:
                self._failed = True
                raise WalIntegrityError("projector_conflict")
            return ProjectionAck(
                applied=False,
                duplicate=True,
                watermark=self._watermark,
                run_id=record.run_id,
                wal_seq=record.wal_seq,
                record_hash=record.record_hash,
            )
        if self._run_id is None:
            self._run_id = record.run_id
        elif record.run_id != self._run_id:
            self._failed = True
            raise WalIntegrityError("run_mismatch")
        if record.wal_seq != self._watermark + 1:
            self._failed = True
            raise WalIntegrityError("projector_gap")
        self._hashes[key] = record.record_hash
        self._watermark = record.wal_seq
        self._records.append(record)
        return ProjectionAck(
            applied=True,
            duplicate=False,
            watermark=self._watermark,
            run_id=record.run_id,
            wal_seq=record.wal_seq,
            record_hash=record.record_hash,
        )

    @property
    def watermark(self) -> int:
        return self._watermark

    @property
    def failed(self) -> bool:
        return self._failed

    def projection(self) -> Mapping[str, Any]:
        records = tuple(item.to_public_dict() for item in self._records)
        out = {
            "schema_version": SCHEMA_VERSION,
            "watermark": self._watermark,
            "run_id": self._run_id,
            "records": records,
        }
        _assert_public(out)
        return MappingProxyType(out)

    def __repr__(self) -> str:
        return f"InMemoryProjector(watermark={self._watermark})"


def encode_wal_record(
    event: ExecutionEvent,
    *,
    wal_seq: int,
    run_id: str,
    prev_hash: str,
) -> Tuple[WalRecord, bytes]:
    if not isinstance(event, ExecutionEvent):
        raise WalError("invalid_event")
    if event.run_id != run_id:
        raise WalError("run_mismatch")
    if event.schema_version != CONTRACT_SCHEMA_VERSION:
        raise WalError("invalid_event_schema")
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "wal_seq": wal_seq,
        "run_id": run_id,
        "prev_hash": prev_hash,
        "event": event.to_public_dict(),
        "event_content_hash": event.content_hash(),
        "event_sequence_hash": event.sequence_hash(),
    }
    record_hash = _record_hash_for(envelope)
    envelope["record_hash"] = record_hash
    line = _canonical_json(envelope) + "\n"
    record = WalRecord(
        schema_version=SCHEMA_VERSION,
        wal_seq=wal_seq,
        run_id=run_id,
        prev_hash=prev_hash,
        event=event,
        event_content_hash=event.content_hash(),
        event_sequence_hash=event.sequence_hash(),
        record_hash=record_hash,
    )
    return record, line.encode("utf-8")


def decode_wal_line(raw_line: bytes, *, expected_run_id: str, expected_seq: int, expected_prev: str) -> WalRecord:
    if raw_line.endswith(b"\r"):
        raise WalIntegrityError("middle_corruption")
    try:
        text = raw_line.decode("utf-8")
        parsed = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WalIntegrityError("middle_corruption") from None
    if not isinstance(parsed, dict):
        raise WalIntegrityError("middle_corruption")
    if set(parsed) != set(RECORD_KEYS):
        raise WalIntegrityError("exact_key_violation")
    if parsed.get("schema_version") != SCHEMA_VERSION:
        raise WalIntegrityError("schema_mismatch")
    wal_seq = parsed.get("wal_seq")
    if isinstance(wal_seq, bool) or not isinstance(wal_seq, int) or wal_seq < 1:
        raise WalIntegrityError("bad_wal_seq")
    if wal_seq != expected_seq:
        raise WalIntegrityError("sequence_gap")
    run_id = parsed.get("run_id")
    if run_id != expected_run_id:
        raise WalIntegrityError("run_mismatch")
    prev_hash = _require_hash(parsed.get("prev_hash"), field="prev_hash")
    if prev_hash != expected_prev:
        raise WalIntegrityError("chain_break")
    stored_hash = _require_hash(parsed.get("record_hash"), field="record_hash")
    computed = _record_hash_for(parsed)
    if stored_hash != computed:
        raise WalIntegrityError("bad_hash")
    event_raw = parsed.get("event")
    if not isinstance(event_raw, Mapping):
        raise WalIntegrityError("invalid_event")
    try:
        event = ExecutionEvent.from_public_dict(event_raw)
    except ContractValidationError:
        raise WalIntegrityError("invalid_event") from None
    if event.run_id != expected_run_id:
        raise WalIntegrityError("run_mismatch")
    content_hash = _require_hash(parsed.get("event_content_hash"), field="event_content_hash")
    sequence_hash = _require_hash(
        parsed.get("event_sequence_hash"), field="event_sequence_hash"
    )
    if event.content_hash() != content_hash or event.sequence_hash() != sequence_hash:
        raise WalIntegrityError("event_hash_mismatch")
    return WalRecord(
        schema_version=SCHEMA_VERSION,
        wal_seq=wal_seq,
        run_id=expected_run_id,
        prev_hash=prev_hash,
        event=event,
        event_content_hash=content_hash,
        event_sequence_hash=sequence_hash,
        record_hash=stored_hash,
    )


class ExecutionWal:
    """Bounded in-memory admission plus explicit durable JSONL writer."""

    def __init__(
        self,
        path: Any,
        *,
        run_id: str,
        max_queue: int,
        reserved_tail: int,
        max_durable_lag: int,
        clock_ns: Optional[ClockNs] = None,
        fsync_fn: Optional[FsyncFn] = None,
        crash_hook: Optional[CrashHook] = None,
    ) -> None:
        self._path = _validate_wal_path(Path(path))
        self._run_id = _require_run_id(run_id)
        self._max_queue = _require_positive_int(max_queue, field="max_queue")
        self._reserved_tail = _require_nonneg_int(reserved_tail, field="reserved_tail")
        if self._reserved_tail >= self._max_queue:
            raise WalError("invalid_config", field="reserved_tail")
        self._max_durable_lag = _require_nonneg_int(
            max_durable_lag, field="max_durable_lag"
        )
        self._clock_ns = clock_ns
        self._fsync_fn = fsync_fn
        self._crash_hook = crash_hook
        self._process_nonce = uuid.uuid4().hex
        self._queue: Deque[_PendingRecord] = deque()
        self._next_seq = 1
        self._enqueue_prev_hash = GENESIS_HASH
        self._durable_seq = 0
        self._durable_hash = GENESIS_HASH
        self._durable_file_size = 0
        self._durable_records: list[WalRecord] = []
        self._writer_unhealthy = False
        self._integrity_unhealthy = False
        self._hard_full = False
        self._venue_reconciled = False
        self._torn_tail = False
        self._torn_pending = False
        self._scanned = False
        self._fsync_count = 0
        self._recon_epoch = 0
        self._recon_intent_id: Optional[str] = None
        self._current_process_recon: dict[str, Tuple[int, str, Venue, str, int]] = {}
        self._reconciled_venues: set[Venue] = set()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run_id(self) -> str:
        return self._run_id

    def enqueue_batch(self, events: Sequence[ExecutionEvent]) -> Tuple[WalAppendAck, ...]:
        """Atomically admit events. Mutates queue/seq/hash only on full success."""
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise WalError("invalid_event")
        event_list = list(events)
        if not event_list:
            return ()
        open_intent = False
        for event in event_list:
            if not isinstance(event, ExecutionEvent):
                raise WalError("invalid_event")
            if event.run_id != self._run_id:
                raise WalError("run_mismatch")
            if event.schema_version != CONTRACT_SCHEMA_VERSION:
                raise WalError("invalid_event_schema")
            if _is_open_intent_accepted(event):
                open_intent = True
        if not self._scanned:
            return (
                WalAppendAck(
                    accepted=False,
                    durable=False,
                    wal_seq=None,
                    reason_code="replay_required",
                ),
            )
        if self._hard_full or self._integrity_unhealthy:
            return (
                WalAppendAck(
                    accepted=False,
                    durable=False,
                    wal_seq=None,
                    reason_code="hard_unhealthy",
                ),
            )
        depth = len(self._queue)
        count = len(event_list)
        if depth >= self._max_queue:
            self._hard_full = True
            return (
                WalAppendAck(
                    accepted=False,
                    durable=False,
                    wal_seq=None,
                    reason_code="queue_full",
                ),
            )
        reserved_floor = self._max_queue - self._reserved_tail
        if not admission_capacity_ok(
            queue_depth=depth,
            max_queue=self._max_queue,
            reserved_tail=self._reserved_tail,
            count=count,
            open_intent=open_intent,
            scanned=self._scanned,
            hard_full=self._hard_full,
            integrity_unhealthy=self._integrity_unhealthy,
        ):
            reason = (
                "reserved_tail"
                if open_intent and depth + count > reserved_floor
                else "queue_full"
            )
            return (
                WalAppendAck(
                    accepted=False,
                    durable=False,
                    wal_seq=None,
                    reason_code=reason,
                ),
            )
        local_seq = self._next_seq
        local_hash = self._enqueue_prev_hash
        encoded: list[_PendingRecord] = []
        for event in event_list:
            record, line = encode_wal_record(
                event,
                wal_seq=local_seq,
                run_id=self._run_id,
                prev_hash=local_hash,
            )
            encoded.append(
                _PendingRecord(wal_seq=local_seq, event=event, record=record, line=line)
            )
            local_hash = record.record_hash
            local_seq += 1
        acks: list[WalAppendAck] = []
        for pending in encoded:
            self._queue.append(pending)
            acks.append(
                WalAppendAck(
                    accepted=True,
                    durable=False,
                    wal_seq=pending.wal_seq,
                    reason_code=None,
                )
            )
        self._next_seq = local_seq
        self._enqueue_prev_hash = local_hash
        if len(self._queue) >= self._max_queue:
            self._hard_full = True
        return tuple(acks)

    def enqueue(self, event: ExecutionEvent) -> WalAppendAck:
        acks = self.enqueue_batch((event,))
        if not acks:
            return WalAppendAck(
                accepted=False,
                durable=False,
                wal_seq=None,
                reason_code="queue_full",
            )
        return acks[0]

    def drain_once(self) -> Optional[WalDurableAck]:
        if not self._queue:
            return None
        if self._writer_unhealthy or self._integrity_unhealthy:
            raise WalError("writer_unhealthy")
        self._scan_existing_prefix()
        if self._writer_unhealthy or self._integrity_unhealthy:
            raise WalError("writer_unhealthy")
        pending = self._queue[0]
        if pending.wal_seq != self._durable_seq + 1:
            self._integrity_unhealthy = True
            raise WalIntegrityError("sequence_gap")
        if pending.record.prev_hash != self._durable_hash:
            self._integrity_unhealthy = True
            raise WalIntegrityError("chain_break")
        disk_size = self._path.stat().st_size if self._path.exists() else 0
        if disk_size != self._durable_file_size:
            self._integrity_unhealthy = True
            raise WalIntegrityError("external_wal_change")
        try:
            self._maybe_crash(CRASH_BEFORE_WRITE)
            self._ensure_parent()
            self._isolate_torn_suffix()
            with self._path.open("ab") as fh:
                try:
                    self._maybe_crash(CRASH_TORN_WRITE)
                except BaseException:
                    fh.write(pending.line[: max(1, len(pending.line) // 2)].rstrip(b"\n"))
                    self._writer_unhealthy = True
                    self._torn_tail = True
                    self._torn_pending = True
                    raise
                fh.write(pending.line)
                self._maybe_crash(CRASH_AFTER_WRITE_BEFORE_FLUSH)
                fh.flush()
                self._maybe_crash(CRASH_AFTER_FLUSH_BEFORE_FSYNC)
                fsync_fn = self._fsync_fn if self._fsync_fn is not None else os.fsync
                fsync_fn(fh.fileno())
                self._fsync_count += 1
                self._durable_file_size += len(pending.line)
        except WalError:
            self._writer_unhealthy = True
            raise
        except OSError:
            self._writer_unhealthy = True
            raise WalError("write_failed") from None
        except BaseException:
            self._writer_unhealthy = True
            raise
        ack = self._commit_durable(pending)
        try:
            self._maybe_crash(CRASH_AFTER_FSYNC_BEFORE_ACK)
        except BaseException:
            self._writer_unhealthy = True
            raise
        return ack

    def drain_all(self) -> Tuple[WalDurableAck, ...]:
        acks: list[WalDurableAck] = []
        while True:
            ack = self.drain_once()
            if ack is None:
                break
            acks.append(ack)
        return tuple(acks)

    def matches_replay_anchor(self, replay: ReplayResult) -> bool:
        """Bind a live writer to a verified startup replay, not caller state alone."""
        if not isinstance(replay, ReplayResult):
            return False
        tail = replay.records[-1] if replay.records else None
        return bool(
            self._scanned
            and replay.integrity_ok
            and not replay.torn_tail
            and not self._writer_unhealthy
            and not self._integrity_unhealthy
            and not self._torn_tail
            and not self._queue
            and replay.durable_watermark == self._durable_seq
            and len(replay.records) == len(self._durable_records)
            and (tail.record_hash if tail else GENESIS_HASH) == self._durable_hash
            and (tail.run_id if tail else self._run_id) == self._run_id
        )

    def drain_and_prove_last(self, expected_event: ExecutionEvent) -> bool:
        """Fsync queued records and prove the accepted tail without replay.

        The caller must serialize all WAL enqueues through the execution-engine
        lock, after validating a startup replay anchor. A write error never
        produces a dispatch proof, including after-fsync/before-ACK failures.
        """
        if (
            not self._scanned
            or self._writer_unhealthy
            or self._integrity_unhealthy
            or self._torn_tail
            or not self._queue
            or self._queue[-1].event != expected_event
        ):
            return False
        expected = self._queue[-1].record
        acks = self.drain_all()
        health = self.health()
        return bool(
            acks
            and acks[-1].durable
            and acks[-1].wal_seq == expected.wal_seq
            and acks[-1].record_hash == expected.record_hash
            and self._durable_records
            and self._durable_records[-1] == expected
            and self._durable_seq == expected.wal_seq
            and self._durable_hash == expected.record_hash
            and not health.writer_unhealthy
            and not health.integrity_unhealthy
            and not health.torn_tail
            and health.queue_depth == 0
            and health.durable_lag == 0
        )

    def replay(self) -> ReplayResult:
        records, torn_tail = self._read_prefix(apply_to_runtime=True)
        events = tuple(record.event for record in records)
        try:
            state = apply_events(initial_spread_state(run_id=self._run_id), events)
        except (InvalidTransition, ContractValidationError):
            self._integrity_unhealthy = True
            raise WalIntegrityError("replay_rejected") from None
        return ReplayResult(
            state=state,
            records=records,
            durable_watermark=self._durable_seq,
            torn_tail=torn_tail,
            opens_allowed=False,
            requires_venue_reconciliation=True,
            integrity_ok=not self._integrity_unhealthy,
        )

    def health(self) -> WalHealth:
        lag = len(self._queue)
        reserved_floor = self._max_queue - self._reserved_tail
        blocks = (
            not self._scanned
            or not self._venue_reconciled
            or self._writer_unhealthy
            or self._integrity_unhealthy
            or self._hard_full
            or lag >= reserved_floor
            or lag > self._max_durable_lag
        )
        return WalHealth(
            queue_depth=lag,
            next_wal_seq=self._next_seq,
            durable_wal_seq=self._durable_seq,
            durable_lag=lag,
            writer_unhealthy=self._writer_unhealthy,
            integrity_unhealthy=self._integrity_unhealthy,
            hard_full=self._hard_full,
            venue_reconciliation_complete=self._venue_reconciled,
            blocks_opens=blocks,
            torn_tail=self._torn_tail,
            fsync_count=self._fsync_count,
        )

    def can_admit(self, count: int, *, open_intent: bool = False) -> bool:
        """Pure remaining-queue check for a future enqueue batch."""
        return admission_capacity_ok(
            queue_depth=len(self._queue),
            max_queue=self._max_queue,
            reserved_tail=self._reserved_tail,
            count=count,
            open_intent=open_intent,
            scanned=self._scanned,
            hard_full=self._hard_full,
            integrity_unhealthy=self._integrity_unhealthy,
        )

    def can_admit_no_order_audit(self, count: int) -> bool:
        """Capacity/read-only health gate that does not assert venue reconciliation.

        Audit-only events cannot dispatch or publish exposure. The WAL must
        still have a verified prefix and healthy writer before it can accept
        them. Live `submit()` continues to require venue reconciliation.
        """
        return bool(
            self._scanned
            and not self._writer_unhealthy
            and not self._integrity_unhealthy
            and admission_capacity_ok(
                queue_depth=len(self._queue),
                max_queue=self._max_queue,
                reserved_tail=self._reserved_tail,
                count=count,
                open_intent=True,
                scanned=self._scanned,
                hard_full=self._hard_full,
                integrity_unhealthy=self._integrity_unhealthy,
            )
        )

    def mark_venue_reconciled(self, token: object) -> Venue:
        if not isinstance(token, str) or not token:
            raise WalError("invalid_reconciliation_token")
        bound = self._current_process_recon.get(token)
        if bound is None:
            raise WalError("invalid_reconciliation_token")
        wal_seq, record_hash, venue, intent_id, epoch = bound
        if epoch != self._recon_epoch:
            raise WalError("invalid_reconciliation_token")
        if wal_seq > self._durable_seq:
            raise WalError("invalid_reconciliation_token")
        matched_record = next(
            (
                item
                for item in self._durable_records
                if item.wal_seq == wal_seq and item.record_hash == record_hash
            ),
            None,
        )
        if matched_record is None:
            raise WalError("invalid_reconciliation_token")
        bound_venue = _qualifying_recon_venue(matched_record.event)
        if bound_venue is None or bound_venue is not venue:
            raise WalError("invalid_reconciliation_token")
        if matched_record.event.intent_id != intent_id:
            raise WalError("invalid_reconciliation_token")
        if self._recon_intent_id is None:
            self._recon_intent_id = intent_id
        elif self._recon_intent_id != intent_id:
            raise WalError("intent_mismatch")
        self._reconciled_venues.add(venue)
        if Venue.OKX in self._reconciled_venues and Venue.BYBIT in self._reconciled_venues:
            self._venue_reconciled = True
        return venue

    def begin_restart(self) -> None:
        """Advance the in-process reconciliation epoch.

        Prior tokens become invalid even when this same in-memory WAL was
        already venue-reconciled. Token format is unchanged. This is not
        durability: enqueue still does not fsync or drain.
        """
        if not self._scanned:
            raise WalError("replay_required")
        self._advance_recon_epoch()

    def _advance_recon_epoch(self) -> None:
        self._recon_epoch += 1
        self._current_process_recon.clear()
        self._reconciled_venues.clear()
        self._recon_intent_id = None
        self._venue_reconciled = False

    def _commit_durable(self, pending: _PendingRecord) -> WalDurableAck:
        self._queue.popleft()
        self._durable_seq = pending.wal_seq
        self._durable_hash = pending.record.record_hash
        self._durable_records.append(pending.record)
        if pending.event.event_type is ExecutionEventType.STREAM_GENERATION_MISMATCH:
            self._advance_recon_epoch()
            return WalDurableAck(
                durable=True,
                wal_seq=pending.wal_seq,
                run_id=self._run_id,
                record_hash=pending.record.record_hash,
                reconciliation_token=None,
            )
        token = None
        venue = _qualifying_recon_venue(pending.event)
        if venue is not None:
            token = (
                f"{self._process_nonce}:{self._recon_epoch}:"
                f"{pending.wal_seq}:{pending.record.record_hash}"
            )
            self._current_process_recon[token] = (
                pending.wal_seq,
                pending.record.record_hash,
                venue,
                pending.event.intent_id,
                self._recon_epoch,
            )
        return WalDurableAck(
            durable=True,
            wal_seq=pending.wal_seq,
            run_id=self._run_id,
            record_hash=pending.record.record_hash,
            reconciliation_token=token,
        )

    def _maybe_crash(self, stage: str) -> None:
        if self._crash_hook is None:
            return
        self._crash_hook(stage)

    def _ensure_parent(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _isolate_torn_suffix(self) -> None:
        if not self._torn_pending:
            return
        if not self._path.exists():
            self._torn_pending = False
            return
        data = self._path.read_bytes()
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            prefix = b""
            suffix = data
        else:
            prefix = data[: last_nl + 1]
            suffix = data[last_nl + 1 :]
        if suffix:
            torn_path = self._path.with_name(self._path.name + ".torn")
            with torn_path.open("ab") as handle:
                handle.write(suffix)
                handle.flush()
                fsync_fn = self._fsync_fn if self._fsync_fn is not None else os.fsync
                fsync_fn(handle.fileno())
        with self._path.open("r+b") as handle:
            handle.truncate(len(prefix))
            handle.flush()
            fsync_fn = self._fsync_fn if self._fsync_fn is not None else os.fsync
            fsync_fn(handle.fileno())
        self._torn_pending = False
        self._torn_tail = False

    def _scan_existing_prefix(self) -> None:
        if self._scanned:
            return
        self._read_prefix(apply_to_runtime=True)

    def _read_prefix(self, *, apply_to_runtime: bool) -> Tuple[Tuple[WalRecord, ...], bool]:
        path = self._path
        if not path.exists():
            if apply_to_runtime:
                self._scanned = True
                self._torn_tail = False
                self._torn_pending = False
            return (), False
        data = path.read_bytes()
        if data == b"":
            if apply_to_runtime:
                self._scanned = True
                self._torn_tail = False
                self._torn_pending = False
            return (), False
        torn_tail = not data.endswith(b"\n")
        if torn_tail:
            last_nl = data.rfind(b"\n")
            prefix = data[: last_nl + 1] if last_nl != -1 else b""
        else:
            prefix = data
        records: list[WalRecord] = []
        expected_seq = 1
        expected_prev = GENESIS_HASH
        if prefix:
            lines = prefix.split(b"\n")
            if lines and lines[-1] == b"":
                lines = lines[:-1]
            for line in lines:
                if line == b"" or line.strip() == b"":
                    self._integrity_unhealthy = True
                    raise WalIntegrityError("middle_corruption")
                try:
                    record = decode_wal_line(
                        line,
                        expected_run_id=self._run_id,
                        expected_seq=expected_seq,
                        expected_prev=expected_prev,
                    )
                except WalIntegrityError:
                    self._integrity_unhealthy = True
                    raise
                records.append(record)
                expected_seq = record.wal_seq + 1
                expected_prev = record.record_hash
        if apply_to_runtime:
            if self._durable_seq and records:
                if records[-1].wal_seq < self._durable_seq:
                    self._integrity_unhealthy = True
                    raise WalIntegrityError("sequence_gap")
            self._durable_records = list(records)
            self._durable_seq = records[-1].wal_seq if records else 0
            self._durable_hash = records[-1].record_hash if records else GENESIS_HASH
            self._durable_file_size = len(data)
            if not self._queue:
                self._next_seq = self._durable_seq + 1
                self._enqueue_prev_hash = self._durable_hash
            elif self._queue[0].wal_seq != self._durable_seq + 1:
                self._integrity_unhealthy = True
                raise WalIntegrityError("sequence_gap")
            self._torn_tail = torn_tail
            self._torn_pending = torn_tail
            self._scanned = True
        return tuple(records), torn_tail

    def __repr__(self) -> str:
        return (
            "ExecutionWal("
            f"run_id={self._run_id!r}, next_wal_seq={self._next_seq}, "
            f"durable_wal_seq={self._durable_seq})"
        )

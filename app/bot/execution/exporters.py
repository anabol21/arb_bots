"""Pure WAL-tail exporters and Grok/Sentry-compatible envelopes.

Stdlib plus frozen EV2 contracts and WAL value objects only. This module
never loads the live Sentry SDK, the bot Sentry helper, logging network
handlers or database clients. Sink invocation is outside enqueue/writer
calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Tuple

from app.bot.execution.contracts import (
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    SpreadDirection,
)
from app.bot.execution.wal import (
    SCHEMA_VERSION,
    WalError,
    WalHealth,
    WalIntegrityError,
    WalRecord,
    require_wal_record,
)

SENTRY_EVENT_OPEN = "open"
SENTRY_EVENT_CLOSE = "close"
SENTRY_FINGERPRINT_PREFIX = "theta_k1"
SENTRY_CONTOUR = "gear22_theta_k1"
SENTRY_KIND = "trade"
SENTRY_LEVEL = "error"
_PUBLIC_COIN_RE = re.compile(r"^[A-Z0-9]{2,16}$")
_PUBLIC_SIDES = frozenset({SpreadDirection.LONG.value, SpreadDirection.SHORT.value})

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

Sink = Callable[["SentryEnvelope"], None]


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _assert_public(node: object) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if _norm_key(key) in _FORBIDDEN_PUBLIC_KEYS:
                raise WalError("forbidden_field")
            _assert_public(value)
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _assert_public(item)


@dataclass(frozen=True)
class SentryEnvelope:
    event: str
    trade_id: str
    message: str
    level: str
    tags: Mapping[str, str]
    fingerprint: Tuple[str, str, str]

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "event": self.event,
            "trade_id": self.trade_id,
            "message": self.message,
            "level": self.level,
            "tags": dict(self.tags),
            "fingerprint": list(self.fingerprint),
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "SentryEnvelope("
            f"event={self.event!r}, trade_id={self.trade_id!r}, "
            f"fingerprint={self.fingerprint!r})"
        )


@dataclass(frozen=True)
class MetricsSnapshot:
    queue_depth: int
    durable_lag: int
    fsync_count: int
    cursor_lag: int

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "queue_depth": self.queue_depth,
            "durable_lag": self.durable_lag,
            "fsync_count": self.fsync_count,
            "cursor_lag": self.cursor_lag,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "MetricsSnapshot("
            f"queue_depth={self.queue_depth}, durable_lag={self.durable_lag}, "
            f"fsync_count={self.fsync_count}, cursor_lag={self.cursor_lag})"
        )


@dataclass(frozen=True)
class WalExportCursor:
    last_wal_seq: int
    last_record_hash: Optional[str]

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "last_wal_seq": self.last_wal_seq,
            "last_record_hash": self.last_record_hash,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "WalExportCursor("
            f"last_wal_seq={self.last_wal_seq}, "
            f"last_record_hash={self.last_record_hash!r})"
        )


def _public_identity(coin: object, side: object) -> Optional[Tuple[str, str]]:
    if not isinstance(coin, str) or not isinstance(side, str):
        return None
    if coin.strip().upper() == "NA" or side.strip().upper() == "NA":
        return None
    if not _PUBLIC_COIN_RE.fullmatch(coin):
        return None
    if side not in _PUBLIC_SIDES:
        return None
    return coin, side


def map_lifecycle_to_sentry_envelope(
    event: ExecutionEvent,
    *,
    close_coin: Optional[str] = None,
    close_side: Optional[str] = None,
) -> Optional[SentryEnvelope]:
    """Map compatible lifecycle evidence to a pure envelope. No SDK I/O."""
    if not isinstance(event, ExecutionEvent):
        raise WalError("invalid_event")
    if event.event_type is ExecutionEventType.INTENT_ACCEPTED:
        if event.payload.get("action") == IntentAction.OPEN.value:
            identity = _public_identity(
                event.payload.get("coin"), event.payload.get("spread_direction")
            )
            if identity is None:
                return None
            return _envelope_for(event, SENTRY_EVENT_OPEN, coin=identity[0], side=identity[1])
        return None
    if event.event_type is ExecutionEventType.FLATNESS_PROVEN:
        identity = _public_identity(close_coin, close_side)
        if identity is None:
            identity = _public_identity(
                event.payload.get("coin"), event.payload.get("spread_direction")
            )
        if identity is None:
            return None
        return _envelope_for(event, SENTRY_EVENT_CLOSE, coin=identity[0], side=identity[1])
    return None


def metrics_snapshot(*, health: WalHealth, cursor: WalExportCursor) -> MetricsSnapshot:
    if not isinstance(health, WalHealth) or not isinstance(cursor, WalExportCursor):
        raise WalError("invalid_metrics_input")
    cursor_lag = health.durable_wal_seq - cursor.last_wal_seq
    if cursor_lag < 0:
        cursor_lag = 0
    return MetricsSnapshot(
        queue_depth=health.queue_depth,
        durable_lag=health.durable_lag,
        fsync_count=health.fsync_count,
        cursor_lag=cursor_lag,
    )


def _envelope_for(event: ExecutionEvent, name: str, *, coin: str, side: str) -> Optional[SentryEnvelope]:
    identity = _public_identity(coin, side)
    if identity is None:
        return None
    coin, side = identity
    trade_id = event.intent_id
    message = f"theta_k1 trade {name} coin={coin} side={side} trade_id={trade_id}"
    tags = MappingProxyType(
        {
            "event": name,
            "coin": coin,
            "side": side,
            "trade_id": trade_id,
            "contour": SENTRY_CONTOUR,
            "kind": SENTRY_KIND,
        }
    )
    envelope = SentryEnvelope(
        event=name,
        trade_id=trade_id,
        message=message,
        level=SENTRY_LEVEL,
        tags=tags,
        fingerprint=(SENTRY_FINGERPRINT_PREFIX, trade_id, name),
    )
    _assert_public(envelope.to_public_dict())
    return envelope


class InMemoryExporter:
    """Durable-record export cursor. Sink failure does not advance."""

    def __init__(self, *, sink: Optional[Sink] = None) -> None:
        self._sink = sink
        self._cursor = WalExportCursor(last_wal_seq=0, last_record_hash=None)
        self._hashes: dict[Tuple[str, int], str] = {}
        self._envelopes: list[SentryEnvelope] = []
        self._run_id: Optional[str] = None
        self._close_context: dict[str, Tuple[str, str]] = {}

    @property
    def cursor(self) -> WalExportCursor:
        return self._cursor

    @property
    def envelopes(self) -> Tuple[SentryEnvelope, ...]:
        return tuple(self._envelopes)

    def export(self, record: WalRecord) -> WalExportCursor:
        record = require_wal_record(record)
        key = (record.run_id, record.wal_seq)
        existing = self._hashes.get(key)
        if existing is not None:
            if existing != record.record_hash:
                raise WalIntegrityError("exporter_conflict")
            return self._cursor
        if self._run_id is None:
            self._run_id = record.run_id
        elif record.run_id != self._run_id:
            raise WalIntegrityError("run_mismatch")
        if record.wal_seq != self._cursor.last_wal_seq + 1:
            raise WalIntegrityError("exporter_gap")
        event = record.event
        if (
            event.event_type is ExecutionEventType.INTENT_ACCEPTED
            and event.payload.get("action") == IntentAction.CLOSE.value
        ):
            identity = _public_identity(
                event.payload.get("coin"), event.payload.get("spread_direction")
            )
            if identity is not None:
                self._close_context[event.intent_id] = identity
        close_coin = None
        close_side = None
        if event.event_type is ExecutionEventType.FLATNESS_PROVEN:
            stored = self._close_context.get(event.intent_id)
            if stored is not None:
                close_coin, close_side = stored
        envelope = map_lifecycle_to_sentry_envelope(
            event, close_coin=close_coin, close_side=close_side
        )
        if envelope is not None and self._sink is not None:
            self._sink(envelope)
        if envelope is not None:
            self._envelopes.append(envelope)
        self._hashes[key] = record.record_hash
        self._cursor = WalExportCursor(
            last_wal_seq=record.wal_seq,
            last_record_hash=record.record_hash,
        )
        return self._cursor

    def snapshot_metrics(self, health: WalHealth) -> MetricsSnapshot:
        return metrics_snapshot(health=health, cursor=self._cursor)

    def __repr__(self) -> str:
        return f"InMemoryExporter(last_wal_seq={self._cursor.last_wal_seq}, schema={SCHEMA_VERSION!r})"

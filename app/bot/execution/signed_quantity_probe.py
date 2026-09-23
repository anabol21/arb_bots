"""Signed read-only two-venue quantity probe bound to a durable EV2 plan.

It cannot authorize order sends or K=1 publication: account ownership,
private-stream continuity, and cross-request snapshot consistency remain
separate gates. No live runtime imports this module yet.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.execution.contracts import ExecutionEventType, Venue
from app.bot.execution.durable_projection import (
    DurableManagerCandidate,
    DurableProjectionError,
    inspect_durable_manager_candidate,
)
from app.bot.execution.venue_quantity_compare import (
    QuantityCompareError,
    QuantityComparison,
    compare_candidate_quantities,
)
from app.bot.execution.wal import ReplayResult, WalHealth
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.venue import VenueEndpoints, endpoints_for_venue
from app.bot.private.ws_w4_baseline import (
    BaselineError,
    _BYBIT_OPEN,
    _BYBIT_POS,
    _OKX_OPEN,
    _OKX_POS,
    _bybit_signed_get,
    _okx_signed_get,
)


class SignedQuantityProbeError(RuntimeError):
    """The signed read-only probe cannot establish usable venue evidence."""


@dataclass(frozen=True)
class SignedQuantityProbeResult:
    comparison: QuantityComparison
    wal_run_id: str
    wal_seq: int
    wal_record_hash: str
    elapsed_ms: int
    account_ownership_required: bool = True
    private_reseed_required: bool = True
    snapshot_consistency_required: bool = True
    okx_pagination_required: bool = True
    publication_ready: bool = False


def _durable_instruments(
    candidate: DurableManagerCandidate,
    replay: ReplayResult,
    health: WalHealth,
) -> dict[Venue, str]:
    committed = (
        candidate.projection.trade_id
        if candidate.projection.publication == "close" else None
    )
    try:
        inspected = inspect_durable_manager_candidate(
            engine_state=replay.state, replay=replay, health=health,
            committed_trade_id=committed,
        )
    except DurableProjectionError as exc:
        raise SignedQuantityProbeError("durable_wal_not_ready") from exc
    if inspected.journal_evidence() != candidate.journal_evidence():
        raise SignedQuantityProbeError("candidate_wal_mismatch")
    target_intent = (
        candidate.projection.close_intent_id
        if candidate.projection.publication == "close"
        else candidate.projection.trade_id
    )
    if not target_intent:
        raise SignedQuantityProbeError("intent_id_missing")
    sends = [
        record.event for record in replay.records
        if record.event.event_type is ExecutionEventType.REQUEST_SENT
        and record.event.intent_id == target_intent
    ]
    if len(sends) != 2 or {event.venue for event in sends} != {Venue.BYBIT, Venue.OKX}:
        raise SignedQuantityProbeError("two_venue_send_plan_required")
    legs = {leg.venue: leg for leg in candidate.projection.legs}
    if set(legs) != {Venue.BYBIT, Venue.OKX}:
        raise SignedQuantityProbeError("two_venue_legs_required")
    instruments: dict[Venue, str] = {}
    spread_side = candidate.projection.side
    if spread_side not in {"long", "short"}:
        raise SignedQuantityProbeError("spread_side_missing")
    for event in sends:
        venue = event.venue
        assert venue is not None
        payload = event.payload
        instrument = payload.get("instrument")
        if not isinstance(instrument, str) or not instrument:
            raise SignedQuantityProbeError("instrument_missing")
        if payload.get("client_id") != legs[venue].client_id:
            raise SignedQuantityProbeError("client_id_mismatch")
        closing = candidate.projection.publication == "close"
        if payload.get("reduce_only") is not closing:
            raise SignedQuantityProbeError("reduce_only_mismatch")
        opening_buy = (venue is Venue.OKX) == (spread_side == "long")
        expected_side = "sell" if (opening_buy == closing) else "buy"
        if payload.get("side") != expected_side:
            raise SignedQuantityProbeError("order_side_mismatch")
        try:
            quantity = Decimal(str(payload.get("quantity")))
        except (InvalidOperation, ValueError) as exc:
            raise SignedQuantityProbeError("plan_quantity_invalid") from exc
        if not quantity.is_finite() or quantity <= 0:
            raise SignedQuantityProbeError("plan_quantity_invalid")
        if not closing and quantity != legs[venue].effective_open_quantity:
            raise SignedQuantityProbeError("plan_quantity_mismatch")
        instruments[venue] = instrument
    return instruments


def _server_time_ms(response: Mapping[str, Any], *, end_ms: int,
                    max_age_ms: int) -> None:
    if not isinstance(response, Mapping):
        raise SignedQuantityProbeError("bybit_response_invalid")
    raw = response.get("time")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise SignedQuantityProbeError("bybit_server_time_missing")
    try:
        stamp = int(raw)
    except ValueError as exc:
        raise SignedQuantityProbeError("bybit_server_time_invalid") from exc
    if end_ms - stamp > max_age_ms or stamp > end_ms + max_age_ms:
        raise SignedQuantityProbeError("bybit_server_time_stale")


def probe_signed_candidate_quantities(
    *,
    candidate: DurableManagerCandidate,
    replay: ReplayResult,
    health: WalHealth,
    bybit_credentials: LiveCredentials,
    okx_credentials: LiveCredentials,
    pool_symbols: Mapping[Venue, Sequence[str]],
    expected_key_fingerprints: Mapping[Venue, str],
    endpoints: Optional[VenueEndpoints] = None,
    http_get_json: Optional[Callable[..., Mapping[str, Any]]] = None,
    clock_ms: Optional[Callable[[], int]] = None,
    max_elapsed_ms: int = 5000,
    max_bybit_age_ms: int = 5000,
) -> SignedQuantityProbeResult:
    """Perform four allowlisted signed GETs, without live mutation.

    Key fingerprints bind configured credentials to a release manifest, not
    to exchange UID. The return value is never a publication approval.
    """

    instruments = _durable_instruments(candidate, replay, health)
    if isinstance(max_elapsed_ms, bool) or not isinstance(max_elapsed_ms, int) or max_elapsed_ms <= 0:
        raise SignedQuantityProbeError("invalid_time_limit")
    if isinstance(max_bybit_age_ms, bool) or not isinstance(max_bybit_age_ms, int) or max_bybit_age_ms <= 0:
        raise SignedQuantityProbeError("invalid_time_limit")
    credentials = {Venue.BYBIT: bybit_credentials, Venue.OKX: okx_credentials}
    if set(expected_key_fingerprints) != {Venue.BYBIT, Venue.OKX}:
        raise SignedQuantityProbeError("key_fingerprint_missing")
    for venue, cred in credentials.items():
        if not cred.api_key or not cred.api_secret:
            raise SignedQuantityProbeError("read_credentials_missing")
        actual = hashlib.sha256(cred.api_key.encode("utf-8")).hexdigest()
        if not actual or expected_key_fingerprints[venue] != actual:
            raise SignedQuantityProbeError("key_fingerprint_mismatch")

    selected_endpoints = endpoints or endpoints_for_venue("live")
    live_endpoints = endpoints_for_venue("live")
    if (
        selected_endpoints.venue != "live"
        or selected_endpoints.bybit_rest != live_endpoints.bybit_rest
        or selected_endpoints.okx_rest != live_endpoints.okx_rest
    ):
        raise SignedQuantityProbeError("live_read_endpoints_required")
    now = clock_ms or (lambda: int(time.time() * 1000))
    start_ms = now()
    try:
        bybit_positions = _bybit_signed_get(
            credentials=bybit_credentials, base=selected_endpoints.bybit_rest,
            path=_BYBIT_POS, query="category=linear&settleCoin=USDT&limit=200",
            http_get_json=http_get_json,
        )
        bybit_orders = _bybit_signed_get(
            credentials=bybit_credentials, base=selected_endpoints.bybit_rest,
            path=_BYBIT_OPEN,
            query="category=linear&settleCoin=USDT&openOnly=0&limit=50",
            http_get_json=http_get_json,
        )
        okx_positions = _okx_signed_get(
            credentials=okx_credentials, base=selected_endpoints.okx_rest,
            path_with_query=f"{_OKX_POS}?instType=SWAP",
            http_get_json=http_get_json,
        )
        okx_orders = _okx_signed_get(
            credentials=okx_credentials, base=selected_endpoints.okx_rest,
            path_with_query=f"{_OKX_OPEN}?instType=SWAP",
            http_get_json=http_get_json,
        )
    except (
        BaselineError, urllib.error.URLError, TimeoutError, OSError,
        TypeError, ValueError,
    ) as exc:
        raise SignedQuantityProbeError("signed_read_failed") from exc
    end_ms = now()
    if (
        isinstance(start_ms, bool) or isinstance(end_ms, bool)
        or not isinstance(start_ms, int) or not isinstance(end_ms, int)
        or start_ms <= 0 or end_ms < start_ms
        or end_ms - start_ms > max_elapsed_ms
    ):
        raise SignedQuantityProbeError("snapshot_window_invalid")
    _server_time_ms(bybit_positions, end_ms=end_ms,
                    max_age_ms=max_bybit_age_ms)
    _server_time_ms(bybit_orders, end_ms=end_ms,
                    max_age_ms=max_bybit_age_ms)
    try:
        comparison = compare_candidate_quantities(
            candidate=candidate, instruments=instruments, pool_symbols=pool_symbols,
            bybit_positions=bybit_positions, okx_positions=okx_positions,
            bybit_open_orders=bybit_orders, okx_open_orders=okx_orders,
        )
    except QuantityCompareError as exc:
        raise SignedQuantityProbeError("quantity_snapshot_invalid") from exc
    comparison = replace(comparison, requires_signed_fresh_source=False)
    return SignedQuantityProbeResult(
        comparison=comparison,
        wal_run_id=candidate.run_id,
        wal_seq=candidate.wal_seq,
        wal_record_hash=candidate.wal_record_hash,
        elapsed_ms=end_ms - start_ms,
    )

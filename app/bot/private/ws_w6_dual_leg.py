"""W6 bounded WS dual-leg market samples (explicit ``--ws-w6-dual-leg`` only).

Immutable matched TRUMP profiles (~$6–8, ≪ 100 USD/venue):
  - Bybit TRUMPUSDT BUY qty 4.0 market
  - OKX TRUMP-USDT-SWAP SELL qty 40 contracts (ctVal 0.1 TRUMP)
  - Flatten: Bybit SELL reduce_only 4.0; OKX BUY reduce_only 40

Sequential: first Bybit must fill, else abort OKX. Flatten both after a
completed open pair. Stop n on abort, incomplete flatten, or leftover.
Requires VENUE=live, LIVE_ORDERS=1, BBOT_PRIVATE_W6=1, ``--w6-n=1..20``,
and ``--w6-approve-one-shot``. Default / W3 / W4 / W5 CLI never binds this.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.bot.private.journal_v1 import (
    JournalValidationError,
    PrivateJournalWriter,
    derive_latency_intervals_from_op_events,
    new_opaque_id,
    scan_all_journal_events,
)
from app.bot.private.order_approval import ApprovalVault
from app.bot.private.order_lease import (
    LeaseState,
    OrderStateSnapshot,
    observed_terminal_state,
)
from app.bot.private.order_metadata import MetadataError, MetadataProvider, parse_decimal
from app.bot.private.order_plan import OrderPlan, OrderPlanError, build_order_plan
from app.bot.private.order_preflight import PositionModeProvider, PreflightError
from app.bot.private.order_sender import (
    ApprovalBoundSender,
    TransportAck,
    assert_default_entrypoint_cannot_transport,
)
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.paths import resolve_data_root
from app.bot.private.secrets import load_live_secrets
from app.bot.private.venue import endpoints_for_venue
from app.bot.private.ws_gates import WsProfileGateError, assert_ws_w6_send_gates
from app.bot.private.ws_private import (
    PrivateStreamRuntime,
    WsOrderStateProvider,
)
from app.bot.private.ws_reseed import build_signed_rest_reseed
from app.bot.private.ws_socket import (
    PrivateWsSocket,
    assert_no_default_ws_socket,
    unbind_socket_factory,
)
from app.bot.private.ws_w4_baseline import (
    BaselineError,
    FlatBaselinePort,
    assert_flat,
)
from app.bot.private.ws_w4_postonly import (
    _handshake_private_and_trade,
    _wait_private_terminal,
    assert_w4_okx_net_mode,
)
from app.bot.private.ws_w5_market import _WsTradePlaceTransport

LOG = logging.getLogger("bbot.private.ws_w6")

W6_TERMINAL_WAIT_SEC = 15.0
W6_N_MIN = 1
W6_N_MAX = 20
W6_PAIR_ALIAS = "TRUMP-USDT-PERP"
# Bybit linear minNotionalValue is 5 USDT. 3.0 TRUMP at mark ~1.67 ≈ $5.01.
W6_BYBIT_MIN_NOTIONAL = Decimal("5")
W6_SAMPLE_MAX_NOTIONAL = Decimal("15")

W6_LEGS: dict[str, dict[str, Any]] = {
    "bybit": {
        "exchange": "bybit",
        "venue": "bybit_live",
        "symbol": "TRUMPUSDT",
        "qty": "4.0",
        "open_side": "buy",
        "flatten_side": "sell",
        "mode": "market",
        "leg_role": "first",
    },
    "okx": {
        "exchange": "okx",
        "venue": "okx_live",
        "symbol": "TRUMP-USDT-SWAP",
        "qty": "40",
        "open_side": "sell",
        "flatten_side": "buy",
        "mode": "market",
        "leg_role": "second",
    },
}


class W6ProfileError(ValueError):
    """Reject anything outside the immutable W6 dual-leg profiles."""


@dataclass
class DualFlatBaseline:
    """Route baseline checks to the matching venue port."""

    bybit: FlatBaselinePort
    okx: FlatBaselinePort

    def check(self, *, exchange: str, symbol: str) -> Any:
        if exchange == "okx":
            return self.okx.check(exchange=exchange, symbol=symbol)
        return self.bybit.check(exchange=exchange, symbol=symbol)


@dataclass(frozen=True)
class DualPositionModeProvider:
    bybit: PositionModeProvider
    okx: PositionModeProvider

    def get(self, venue: str) -> Any:
        if venue == "okx_live":
            return self.okx.get(venue)
        return self.bybit.get(venue)


@dataclass
class _DualOrderStateProvider:
    bybit: WsOrderStateProvider
    okx: WsOrderStateProvider

    def get(self, plan: OrderPlan) -> OrderStateSnapshot:
        if str(plan.venue).startswith("okx"):
            return self.okx.get(plan)
        return self.bybit.get(plan)


@dataclass
class W6Report:
    status: str
    n_requested: int = 0
    n_completed: int = 0
    n_aborted: int = 0
    symbol_alias: str = W6_PAIR_ALIAS
    subscription_ready: bool = False
    reseed_matched: bool = False
    sends_blocked: bool = True
    trade_ws_bound: bool = False
    orders_sent: int = 0
    flat_after: bool = False
    error_code: Optional[str] = None
    venue_code: Optional[str] = None
    latency_ms: dict[str, Any] = field(default_factory=dict)
    open_mode: str = "sequential"

    def as_public_dict(self) -> dict[str, Any]:
        from app.bot.private.ws_private import sanitize_venue_code

        out: dict[str, Any] = {
            "status": self.status,
            "n_requested": self.n_requested,
            "n_completed": self.n_completed,
            "n_aborted": self.n_aborted,
            "symbol_alias": self.symbol_alias,
            "subscription_ready": self.subscription_ready,
            "reseed_matched": self.reseed_matched,
            "sends_blocked": self.sends_blocked,
            "trade_ws_bound": self.trade_ws_bound,
            "orders_sent": self.orders_sent,
            "flat_after": self.flat_after,
            "mode": "market",
            "open_mode": self.open_mode,
            "first_leg": "bybit_buy",
            "second_leg": "okx_sell",
            "latency_ms": dict(self.latency_ms),
        }
        if self.error_code:
            out["error_code"] = self.error_code
        vc = sanitize_venue_code(self.venue_code)
        if vc is not None:
            out["venue_code"] = vc
        return out


def resolve_w6_leg(exchange: str) -> dict[str, Any]:
    key = exchange.strip().lower()
    if key not in W6_LEGS:
        raise W6ProfileError("W6 exchange must be bybit or okx")
    return dict(W6_LEGS[key])


def assert_w6_n(n: int) -> int:
    if not isinstance(n, int) or isinstance(n, bool):
        raise W6ProfileError("W6 n must be int 1..20")
    if n < W6_N_MIN or n > W6_N_MAX:
        raise W6ProfileError("W6 n must be in 1..20")
    return n


def assert_exact_w6_open_plan(plan: OrderPlan, profile: Mapping[str, Any]) -> None:
    if plan.venue != profile["venue"]:
        raise W6ProfileError("plan venue outside W6 profile")
    if plan.symbol != profile["symbol"]:
        raise W6ProfileError("plan symbol outside W6 profile")
    if plan.side != profile["open_side"]:
        raise W6ProfileError("W6 open side mismatch")
    if plan.mode != "market" or plan.post_only:
        raise W6ProfileError("W6 only allows market mode")
    if plan.qty != profile["qty"]:
        raise W6ProfileError("W6 qty must match immutable profile")
    if plan.reduce_only:
        raise W6ProfileError("W6 open rejects reduce_only")
    if plan.price is not None:
        raise W6ProfileError("W6 market must not include price")
    if plan.k_live != 1:
        raise W6ProfileError("W6 requires K_live=1")


def assert_exact_w6_flatten_plan(
    plan: OrderPlan, profile: Mapping[str, Any], *, dual_leg_id: str
) -> None:
    if plan.venue != profile["venue"]:
        raise W6ProfileError("flatten venue outside W6 profile")
    if plan.symbol != profile["symbol"]:
        raise W6ProfileError("flatten symbol outside W6 profile")
    if plan.side != profile["flatten_side"]:
        raise W6ProfileError("W6 flatten side mismatch")
    if plan.mode != "market" or plan.post_only:
        raise W6ProfileError("W6 flatten only allows market")
    if plan.qty != profile["qty"]:
        raise W6ProfileError("W6 flatten qty must match profile")
    if not plan.reduce_only:
        raise W6ProfileError("W6 flatten requires reduce_only")
    if plan.dual_leg_id != dual_leg_id:
        raise W6ProfileError("W6 flatten dual_leg_id must link open pair")
    if plan.k_live != 1:
        raise W6ProfileError("W6 requires K_live=1")


def assert_w6_okx_net_mode(
    *,
    venue: str,
    position_mode_provider: PositionModeProvider,
) -> None:
    try:
        assert_w4_okx_net_mode(
            exchange="okx",
            venue=venue,
            position_mode_provider=position_mode_provider,
        )
    except Exception as exc:  # noqa: BLE001 — remap W4ProfileError
        from app.bot.private.ws_w4_postonly import W4ProfileError

        if isinstance(exc, W4ProfileError):
            raise W6ProfileError(str(exc)) from exc
        raise


def assert_w6_notional(
    meta: Any, qty: str, *, min_usd: Optional[Decimal] = None
) -> Decimal:
    q = parse_decimal(qty, field="qty")
    notional = meta.market_notional_usdt(q)
    if min_usd is not None and notional < min_usd:
        raise W6ProfileError("W6 notional below venue min-notional floor")
    if notional > W6_SAMPLE_MAX_NOTIONAL:
        raise W6ProfileError("W6 notional above sample cap")
    if notional >= Decimal("100"):
        raise W6ProfileError("W6 notional fails <100 USD cap")
    return notional


def assert_w6_transport_is_ws_trade(transport: Any) -> None:
    from app.bot.private.order_transport import is_live_http_order_transport

    if is_live_http_order_transport(transport):
        raise W6ProfileError("W6 refuses HTTP order transport; require trade WS")
    if isinstance(transport, _WsTradePlaceTransport):
        return
    if getattr(transport, "_bbot_ws_trade", False):
        return
    raise W6ProfileError("W6 requires explicit ws_trade transport binding")


def _creds_from_live(secrets: Any, exchange: str) -> LiveCredentials:
    if exchange == "bybit":
        return LiveCredentials(
            api_key=secrets.bybit_api_key,
            api_secret=secrets.bybit_api_secret,
        )
    return LiveCredentials(
        api_key=secrets.okx_api_key,
        api_secret=secrets.okx_api_secret,
        passphrase=secrets.okx_passphrase,
    )


def _journal_venue(plan: OrderPlan) -> str:
    return "okx" if str(plan.venue).startswith("okx") else "bybit"


def _as_dual_baseline(baseline: Any) -> DualFlatBaseline:
    if isinstance(baseline, DualFlatBaseline):
        return baseline
    return DualFlatBaseline(bybit=baseline, okx=baseline)


def _percentile(sorted_vals: Sequence[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def _summarize_ms(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    s = sorted(float(v) for v in values)
    return {
        "n": len(s),
        "mean": float(statistics.fmean(s)),
        "p50": _percentile(s, 0.50),
        "p90": _percentile(s, 0.90),
        "min": s[0],
        "max": s[-1],
    }


def _append_leg_latency(journal: PrivateJournalWriter, plan: OrderPlan) -> None:
    op_events = [
        e
        for e in scan_all_journal_events(journal.data_root)
        if str(e.get("operation_id")) == str(plan.order_attempt_id)
    ]
    intervals = derive_latency_intervals_from_op_events(op_events)
    if not intervals:
        raise JournalValidationError("W6 latency_summary requires derivable intervals")
    body = journal.build_latency_summary_event(
        venue=_journal_venue(plan),
        environment="live",
        operation_id=plan.order_attempt_id,
        intervals_ms=intervals,
        latency_basis="monotonic_local",
        sample_count=1,
        dual_leg_id=plan.dual_leg_id,
        leg_id=plan.leg_id,
    )
    journal.append(body)


def _commit_stream_terminal(
    *,
    runtime: PrivateStreamRuntime,
    journal: PrivateJournalWriter,
    lease: Any,
    plan: OrderPlan,
    term: str,
) -> None:
    runtime.journal_terminal_from_stream(plan, terminal_state=term)
    _append_leg_latency(journal, plan)
    lease.mark_terminal()


def _commit_rest_flat_matched(
    *,
    journal: PrivateJournalWriter,
    lease: Any,
    plan: OrderPlan,
    scope: str = "post_dispatch_ambiguity",
) -> None:
    journal.append(
        {
            "event_type": "reconciliation",
            "operation_id": plan.order_attempt_id,
            "venue": _journal_venue(plan),
            "environment": "live",
            "outcome": "observed",
            "dual_leg_id": plan.dual_leg_id,
            "leg_id": plan.leg_id,
            "reconciliation_scope": scope,
            "reconciliation_state": "matched",
            "observation_source": "rest_reconcile",
            "transport": "rest",
        }
    )
    lease.mark_terminal()


def _journal_abort(
    journal: PrivateJournalWriter,
    *,
    observed_plan: OrderPlan,
    peer_plan: OrderPlan,
    abort_reason: str,
) -> None:
    journal.append(
        {
            "event_type": "dual_leg_abort",
            "operation_id": observed_plan.order_attempt_id,
            "venue": _journal_venue(observed_plan),
            "environment": "live",
            "outcome": "observed",
            "dual_leg_id": observed_plan.dual_leg_id,
            "leg_id": observed_plan.leg_id,
            "peer_leg_id": peer_plan.leg_id,
            "abort_reason": abort_reason,
            "request_fingerprint": observed_plan.request_fingerprint,
        }
    )


def _try_journal_abort(
    journal: PrivateJournalWriter,
    *,
    observed_plan: OrderPlan,
    peer_plan: OrderPlan,
    abort_reason: str,
) -> bool:
    try:
        _journal_abort(
            journal,
            observed_plan=observed_plan,
            peer_plan=peer_plan,
            abort_reason=abort_reason,
        )
        return True
    except (OSError, RuntimeError, ValueError, TypeError, JournalValidationError):
        return False


def _clear_dispatch_block_if_idle(sender: ApprovalBoundSender) -> None:
    still = any(
        lease.blocks_new_sends
        for lease in sender.lease_supervisor._leases.values()  # noqa: SLF001
    )
    if not still:
        sender.lease_supervisor._dispatch_blocking = False  # noqa: SLF001


def _runtime_for_plan(
    plan: OrderPlan,
    *,
    bybit_runtime: PrivateStreamRuntime,
    okx_runtime: PrivateStreamRuntime,
) -> PrivateStreamRuntime:
    if str(plan.venue).startswith("okx"):
        return okx_runtime
    return bybit_runtime


def _profile_for_plan(plan: OrderPlan) -> dict[str, Any]:
    """Return a profile matching the plan's actual symbol, not hardcoded TRUMP."""
    base_profile = resolve_w6_leg("okx" if str(plan.venue).startswith("okx") else "bybit")
    profile = dict(base_profile)
    profile["symbol"] = str(plan.symbol)
    return profile


def _collect_open_latency(journal: PrivateJournalWriter) -> dict[str, Any]:
    events = [
        e
        for e in scan_all_journal_events(journal.data_root)
        if str(e.get("run_id") or "") == str(journal.run_id)
    ]
    reduce_by_op: dict[str, bool] = {}
    for ev in events:
        if ev.get("event_type") != "order_prepared":
            continue
        op = str(ev.get("operation_id") or "")
        if op:
            reduce_by_op[op] = bool(ev.get("reduce_only"))

    def _bucket() -> dict[str, list[float]]:
        return {"bybit_rtt": [], "okx_rtt": [], "bybit_fill": [], "okx_fill": []}

    open_b = _bucket()
    flat_b = _bucket()
    for ev in events:
        if ev.get("event_type") != "latency_summary":
            continue
        intervals = ev.get("latency_intervals_ms") or {}
        if not isinstance(intervals, Mapping):
            continue
        op = str(ev.get("operation_id") or "")
        dest = flat_b if reduce_by_op.get(op, False) else open_b
        rtt = intervals.get("request_ack_rtt")
        fill = intervals.get("ack_terminal_receive")
        venue = str(ev.get("venue") or "")
        key_rtt = "okx_rtt" if venue == "okx" else "bybit_rtt"
        key_fill = "okx_fill" if venue == "okx" else "bybit_fill"
        if isinstance(rtt, (int, float)):
            dest[key_rtt].append(float(rtt))
        if isinstance(fill, (int, float)):
            dest[key_fill].append(float(fill))
    return {
        "note": "request_ack_rtt is round-trip, not one-way; this run_id only",
        "open": {
            "bybit_request_ack_rtt": _summarize_ms(open_b["bybit_rtt"]),
            "okx_request_ack_rtt": _summarize_ms(open_b["okx_rtt"]),
            "bybit_ack_terminal_receive": _summarize_ms(open_b["bybit_fill"]),
            "okx_ack_terminal_receive": _summarize_ms(open_b["okx_fill"]),
        },
        "flatten": {
            "bybit_request_ack_rtt": _summarize_ms(flat_b["bybit_rtt"]),
            "okx_request_ack_rtt": _summarize_ms(flat_b["okx_rtt"]),
            "bybit_ack_terminal_receive": _summarize_ms(flat_b["bybit_fill"]),
            "okx_ack_terminal_receive": _summarize_ms(flat_b["okx_fill"]),
        },
    }


def _pair_skew_ms(a: Optional[int], b: Optional[int]) -> Optional[float]:
    if a is None or b is None:
        return None
    return abs(float(a) - float(b)) / 1_000_000.0


def _collect_pair_open_latency(
    journal: PrivateJournalWriter,
    *,
    dispatch_pairs: Sequence[tuple[Optional[int], Optional[int]]],
) -> dict[str, Any]:
    """Pair open-leg skew. Not journal latency_summary names; report-only."""
    events = [
        e
        for e in scan_all_journal_events(journal.data_root)
        if str(e.get("run_id") or "") == str(journal.run_id)
    ]
    reduce_by_op: dict[str, bool] = {}
    dual_of_op: dict[str, str] = {}
    for ev in events:
        if ev.get("event_type") != "order_prepared":
            continue
        op = str(ev.get("operation_id") or "")
        if not op:
            continue
        reduce_by_op[op] = bool(ev.get("reduce_only"))
        did = str(ev.get("dual_leg_id") or "")
        if did:
            dual_of_op[op] = did

    by_dual: dict[str, dict[str, dict[str, int]]] = {}
    for ev in events:
        et = str(ev.get("event_type") or "")
        op = str(ev.get("operation_id") or "")
        if reduce_by_op.get(op, False):
            continue
        did = dual_of_op.get(op) or str(ev.get("dual_leg_id") or "")
        if not did:
            continue
        venue = str(ev.get("venue") or "")
        if venue not in {"bybit", "okx"}:
            continue
        slot = by_dual.setdefault(did, {}).setdefault(venue, {})
        if et == "request_sent" and isinstance(ev.get("send_monotonic_ns"), int):
            slot["send"] = int(ev["send_monotonic_ns"])
        if et == "ack_received" and isinstance(ev.get("receive_monotonic_ns"), int):
            slot["ack"] = int(ev["receive_monotonic_ns"])
        if et == "terminal_update" and isinstance(ev.get("receive_monotonic_ns"), int):
            slot["term"] = int(ev["receive_monotonic_ns"])

    send_skew: list[float] = []
    ack_skew: list[float] = []
    term_skew: list[float] = []
    pair_span: list[float] = []
    for pair in by_dual.values():
        b = pair.get("bybit") or {}
        o = pair.get("okx") or {}
        sk = _pair_skew_ms(b.get("send"), o.get("send"))
        if sk is not None:
            send_skew.append(sk)
        ak = _pair_skew_ms(b.get("ack"), o.get("ack"))
        if ak is not None:
            ack_skew.append(ak)
        tk = _pair_skew_ms(b.get("term"), o.get("term"))
        if tk is not None:
            term_skew.append(tk)
        if b.get("send") is not None and o.get("send") is not None:
            first_send = min(int(b["send"]), int(o["send"]))
            lasts = [v for v in (b.get("term"), o.get("term")) if v is not None]
            if lasts:
                pair_span.append((max(int(x) for x in lasts) - first_send) / 1_000_000.0)

    dispatch_skew = [
        sk
        for sk in (
            _pair_skew_ms(a, b) for a, b in dispatch_pairs
        )
        if sk is not None
    ]
    return {
        "note": (
            "pair skew is |bybit-okx| on the same dual_leg_id. "
            "journal send_monotonic_ns includes request_sent fsync; "
            "dispatch_skew_ms is after both request_sent durable, immediately "
            "before WS transport. request_ack_rtt remains RTT, not one-way."
        ),
        "journal_send_skew_ms": _summarize_ms(send_skew),
        "dispatch_skew_ms": _summarize_ms(dispatch_skew),
        "ack_receive_skew_ms": _summarize_ms(ack_skew),
        "terminal_receive_skew_ms": _summarize_ms(term_skew),
        "pair_span_first_send_to_last_terminal_ms": _summarize_ms(pair_span),
    }


def _place_pair_parallel(
    *,
    bybit_kw: Mapping[str, Any],
    okx_kw: Mapping[str, Any],
) -> tuple[PlaceWaitResult, PlaceWaitResult]:
    barrier = threading.Barrier(2)
    out: dict[str, PlaceWaitResult] = {}

    def worker(key: str, kwargs: Mapping[str, Any]) -> None:
        try:
            out[key] = _place_and_wait_fill(dispatch_barrier=barrier, **kwargs)
        except Exception:  # noqa: BLE001
            try:
                barrier.abort()
            except Exception:  # noqa: BLE001
                pass
            out[key] = PlaceWaitResult(
                status="ack_fail",
                transport_invoked=False,
                error_code="internal_error",
            )

    t_b = threading.Thread(target=worker, args=("bybit", dict(bybit_kw)), daemon=False)
    t_o = threading.Thread(target=worker, args=("okx", dict(okx_kw)), daemon=False)
    t_b.start()
    t_o.start()
    t_b.join()
    t_o.join()
    return (
        out.get(
            "bybit",
            PlaceWaitResult("ack_fail", False, "internal_error"),
        ),
        out.get(
            "okx",
            PlaceWaitResult("ack_fail", False, "internal_error"),
        ),
    )


@dataclass
class PlaceWaitResult:
    status: str
    transport_invoked: bool
    error_code: Optional[str] = None
    venue_code: Optional[str] = None
    dispatch_monotonic_ns: Optional[int] = None


def _place_and_wait_fill(
    *,
    sender: ApprovalBoundSender,
    runtime: PrivateStreamRuntime,
    provider: WsOrderStateProvider,
    place_transport: _WsTradePlaceTransport,
    plan: OrderPlan,
    token: Any,
    credentials: LiveCredentials,
    env: Mapping[str, str],
    journal: PrivateJournalWriter,
    terminal_wait_sec: float,
    fill_inject_fn: Optional[Any],
    inject_kind: str,
    dispatch_barrier: Optional[threading.Barrier] = None,
) -> PlaceWaitResult:
    """status ok|ack_fail|no_fill|journal_failed."""
    runtime.register_plan_fingerprint(plan)
    place_transport._plan = plan  # noqa: SLF001
    sender._transport = place_transport  # noqa: SLF001
    res = sender.send_approved(
        plan,
        token,
        credentials,
        env,
        journal_transport="ws_trade",
        reconnect_generation=runtime.reconnect_generation,
        dispatch_barrier=dispatch_barrier,
    )
    invoked = bool(res.transport_invoked)
    dispatch_ns = res.dispatch_monotonic_ns
    if res.status != "ack":
        if dispatch_barrier is not None and not invoked:
            try:
                dispatch_barrier.abort()
            except Exception:  # noqa: BLE001
                pass
        return PlaceWaitResult(
            status="ack_fail",
            transport_invoked=invoked,
            error_code=res.error_code,
            venue_code=res.venue_code,
            dispatch_monotonic_ns=dispatch_ns,
        )
    if fill_inject_fn is not None:
        try:
            fill_inject_fn(inject_kind, plan)
        except Exception:  # noqa: BLE001
            pass
    snap = _wait_private_terminal(
        runtime, provider, plan, timeout_sec=float(terminal_wait_sec)
    )
    term = observed_terminal_state(snap)
    if term != "filled":
        # Caller may still flatten leftover exposure; do not block sends yet.
        return PlaceWaitResult(
            status="no_fill",
            transport_invoked=invoked,
            error_code="unknown",
            dispatch_monotonic_ns=dispatch_ns,
        )
    try:
        assert sender.lease is not None
        _commit_stream_terminal(
            runtime=runtime,
            journal=journal,
            lease=sender.lease,
            plan=plan,
            term="filled",
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        JournalValidationError,
    ):
        # Do not mark process-blocked here: caller may still flatten leftover
        # exposure on the same venue. Caller fail-closes after that attempt.
        return PlaceWaitResult(
            status="journal_failed",
            transport_invoked=invoked,
            error_code="internal_error",
            dispatch_monotonic_ns=dispatch_ns,
        )
    return PlaceWaitResult(
        status="ok",
        transport_invoked=invoked,
        dispatch_monotonic_ns=dispatch_ns,
    )


def _flatten_venue(
    *,
    sender: ApprovalBoundSender,
    runtime: PrivateStreamRuntime,
    provider: WsOrderStateProvider,
    place_transport: _WsTradePlaceTransport,
    vault: ApprovalVault,
    credentials: LiveCredentials,
    env: Mapping[str, str],
    metadata_provider: MetadataProvider,
    profile: Mapping[str, Any],
    dual_leg_id: str,
    journal: PrivateJournalWriter,
    baseline: DualFlatBaseline,
    terminal_wait_sec: float,
    fill_inject_fn: Optional[Any],
    inject_kind: str,
) -> tuple[str, int]:
    """Flatten one venue. Returns (ok|flatten_incomplete|flatten_plan_rejected, orders_delta)."""
    try:
        flatten = build_order_plan(
            venue=profile["venue"],
            symbol=profile["symbol"],
            side=profile["flatten_side"],
            mode="market",
            metadata_provider=metadata_provider,
            qty=profile["qty"],
            reduce_only=True,
            dual_leg_id=dual_leg_id,
            expires_in_sec=60,
        )
        assert_exact_w6_flatten_plan(flatten, profile, dual_leg_id=dual_leg_id)
    except (OrderPlanError, W6ProfileError, MetadataError):
        sender.lease_supervisor.mark_process_sends_blocked()
        return ("flatten_plan_rejected", 0)
    token = vault.issue(flatten)
    placed = _place_and_wait_fill(
        sender=sender,
        runtime=runtime,
        provider=provider,
        place_transport=place_transport,
        plan=flatten,
        token=token,
        credentials=credentials,
        env=env,
        journal=journal,
        terminal_wait_sec=terminal_wait_sec,
        fill_inject_fn=fill_inject_fn,
        inject_kind=inject_kind,
    )
    orders = 1 if placed.transport_invoked else 0
    if placed.status != "ok":
        sender.lease_supervisor.mark_process_sends_blocked()
        return ("flatten_incomplete", orders)
    try:
        after = baseline.check(exchange=profile["exchange"], symbol=profile["symbol"])
        assert_flat(after)
    except BaselineError:
        sender.lease_supervisor.mark_process_sends_blocked()
        return ("flatten_incomplete", orders)
    return ("ok", orders)


def _recover_inflight_w6(
    *,
    sender: ApprovalBoundSender,
    bybit_runtime: PrivateStreamRuntime,
    okx_runtime: PrivateStreamRuntime,
    bybit_provider: WsOrderStateProvider,
    okx_provider: WsOrderStateProvider,
    bybit_transport: _WsTradePlaceTransport,
    okx_transport: _WsTradePlaceTransport,
    bybit_creds: LiveCredentials,
    okx_creds: LiveCredentials,
    metadata_provider: MetadataProvider,
    journal: PrivateJournalWriter,
    baseline: DualFlatBaseline,
    rest_order_recon: Optional[Any],
    terminal_wait_sec: float,
    vault: ApprovalVault,
    env: Mapping[str, str],
    issue_approval: bool,
) -> Optional[str]:
    unresolved = [
        lease
        for lease in list(sender.lease_supervisor._leases.values())  # noqa: SLF001
        if lease.state != LeaseState.TERMINAL
    ]
    if not unresolved and not sender.lease_supervisor.has_blocking_lease():
        return None
    if not unresolved and sender.lease_supervisor.has_blocking_lease():
        return "recovery_blocked"

    observe_sec = max(0.05, min(float(terminal_wait_sec), 5.0))
    for lease in unresolved:
        plan = lease.plan
        profile = _profile_for_plan(plan)
        runtime = _runtime_for_plan(
            plan, bybit_runtime=bybit_runtime, okx_runtime=okx_runtime
        )
        provider = bybit_provider if profile["exchange"] == "bybit" else okx_provider
        same_symbol = str(plan.symbol) == str(profile["symbol"])
        sender._lease = lease  # noqa: SLF001
        if not same_symbol:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"

        term: Optional[str] = None
        source: Optional[str] = None
        if rest_order_recon is not None:
            try:
                snap_r = rest_order_recon.get(plan)
                t = observed_terminal_state(snap_r)
                if t is not None:
                    term = t
                    source = "rest"
            except Exception:  # noqa: BLE001
                pass
        if term is None:
            runtime.register_plan_fingerprint(plan)
            snap = _wait_private_terminal(
                runtime, provider, plan, timeout_sec=observe_sec
            )
            term = observed_terminal_state(snap)
            if term is not None:
                source = "stream"

        if term is not None and source == "rest" and term in {"cancelled", "expired"}:
            try:
                _commit_rest_flat_matched(journal=journal, lease=lease, plan=plan)
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                JournalValidationError,
            ):
                sender.lease_supervisor.mark_process_sends_blocked()
                return "recovery_journal_failed"
            continue

        if term is not None and source == "stream":
            try:
                if term == "filled":
                    _commit_stream_terminal(
                        runtime=runtime,
                        journal=journal,
                        lease=lease,
                        plan=plan,
                        term="filled",
                    )
                else:
                    runtime.journal_terminal_from_stream(plan, terminal_state=term)
                    lease.mark_terminal()
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                JournalValidationError,
            ):
                sender.lease_supervisor.mark_process_sends_blocked()
                return "recovery_journal_failed"
            if term == "filled" and not bool(plan.reduce_only):
                # Fall through to flatten leftover exposure.
                pass
            else:
                continue

        try:
            base = baseline.check(
                exchange=profile["exchange"], symbol=profile["symbol"]
            )
        except BaselineError:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        if base.ok:
            try:
                if lease.state != LeaseState.TERMINAL:
                    _commit_rest_flat_matched(journal=journal, lease=lease, plan=plan)
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                JournalValidationError,
            ):
                sender.lease_supervisor.mark_process_sends_blocked()
                return "recovery_journal_failed"
            continue

        if runtime.sends_blocked or runtime.reseed_required:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        if not issue_approval:
            sender.lease_supervisor.mark_process_sends_blocked()
            return "recovery_blocked"
        transport = bybit_transport if profile["exchange"] == "bybit" else okx_transport
        creds = bybit_creds if profile["exchange"] == "bybit" else okx_creds
        st, _ = _flatten_venue(
            sender=sender,
            runtime=runtime,
            provider=provider,
            place_transport=transport,
            vault=vault,
            credentials=creds,
            env=env,
            metadata_provider=metadata_provider,
            profile=profile,
            dual_leg_id=plan.dual_leg_id,
            journal=journal,
            baseline=baseline,
            terminal_wait_sec=float(terminal_wait_sec),
            fill_inject_fn=None,
            inject_kind="recovery_flatten",
        )
        if st != "ok":
            return "recovery_blocked"
        if lease.state != LeaseState.TERMINAL:
            try:
                _commit_rest_flat_matched(
                    journal=journal,
                    lease=lease,
                    plan=plan,
                    scope="dual_leg_state",
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                JournalValidationError,
            ):
                sender.lease_supervisor.mark_process_sends_blocked()
                return "recovery_journal_failed"

    _clear_dispatch_block_if_idle(sender)
    if sender.lease_supervisor.has_blocking_lease():
        return "recovery_blocked"
    return None


def _bind_okx_inst_id(
    *,
    runtime: PrivateStreamRuntime,
    metadata_provider: MetadataProvider,
    profile: Mapping[str, Any],
) -> Optional[str]:
    try:
        meta = metadata_provider.get(profile["venue"], profile["symbol"])
        code = meta.inst_id_code
        if not isinstance(code, int) or isinstance(code, bool) or code <= 0:
            raise W6ProfileError("okx W6 requires positive instIdCode")
        runtime.okx_inst_id_code = code
    except (MetadataError, W6ProfileError):
        return "plan_rejected"
    return None


def run_w6_dual_leg(
    *,
    n: int,
    env: Optional[Mapping[str, str]] = None,
    metadata_provider: MetadataProvider,
    position_mode_provider: PositionModeProvider,
    baseline: Any,
    bybit_private_socket: Optional[PrivateWsSocket] = None,
    bybit_trade_socket: Optional[PrivateWsSocket] = None,
    okx_private_socket: Optional[PrivateWsSocket] = None,
    okx_trade_socket: Optional[PrivateWsSocket] = None,
    issue_approval: bool = False,
    bybit_credentials: Optional[LiveCredentials] = None,
    okx_credentials: Optional[LiveCredentials] = None,
    load_secrets: bool = True,
    journal: Optional[PrivateJournalWriter] = None,
    data_root: Optional[Path] = None,
    rest_probe_fn: Optional[Any] = None,
    ack_timeout_sec: float = 5.0,
    terminal_wait_sec: float = W6_TERMINAL_WAIT_SEC,
    rest_order_recon: Optional[Any] = None,
    bybit_place_override: Optional[Any] = None,
    okx_place_override: Optional[Any] = None,
    fill_inject_fn: Optional[Any] = None,
    parallel_open: bool = False,
    send_gate: Optional[Any] = None,
) -> W6Report:
    """Execute n dual-leg market rounds (Bybit buy / OKX sell).

    Default is sequential: Bybit must fill before OKX is sent.
    ``parallel_open=True`` (W7) dispatches both WS places after a barrier.
    """
    e = dict(env if env is not None else os.environ)
    gate = send_gate if send_gate is not None else assert_ws_w6_send_gates
    open_mode = "parallel" if parallel_open else "sequential"
    dispatch_pairs: list[tuple[Optional[int], Optional[int]]] = []

    def _latency() -> dict[str, Any]:
        payload = _collect_open_latency(j)
        if parallel_open:
            payload["pair"] = _collect_pair_open_latency(
                j, dispatch_pairs=dispatch_pairs
            )
        return payload

    def _report(**kwargs: Any) -> W6Report:
        kwargs.setdefault("open_mode", open_mode)
        return W6Report(**kwargs)

    try:
        gate(e)
        n_req = assert_w6_n(int(n))
        bybit_p = resolve_w6_leg("bybit")
        okx_p = resolve_w6_leg("okx")
    except (WsProfileGateError, W6ProfileError, TypeError, ValueError):
        return _report(
            status="rejected_before_socket",
            n_requested=int(n) if isinstance(n, int) and not isinstance(n, bool) else 0,
            error_code="invalid_request",
        )

    dual_base = _as_dual_baseline(baseline)
    root = data_root if data_root is not None else resolve_data_root(e)
    j = journal if journal is not None else PrivateJournalWriter(
        root, run_id=new_opaque_id("run")
    )

    if bybit_credentials is None or okx_credentials is None:
        if not load_secrets:
            return _report(
                status="secrets_unavailable",
                n_requested=n_req,
                error_code="auth_unavailable",
            )
        try:
            secrets = load_live_secrets(e, require_complete=True)
            if bybit_credentials is None:
                bybit_credentials = _creds_from_live(secrets, "bybit")
            if okx_credentials is None:
                okx_credentials = _creds_from_live(secrets, "okx")
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return _report(
                status="secrets_unavailable",
                n_requested=n_req,
                error_code="auth_unavailable",
            )
        if rest_order_recon is None:
            from app.bot.private.ws_w4_baseline import build_signed_rest_order_recon

            rest_order_recon = build_signed_rest_order_recon(
                bybit_credentials=bybit_credentials,
                okx_credentials=okx_credentials,
                endpoints=endpoints_for_venue("live"),
                require_position_flat=True,
            )

    try:
        assert_flat(
            dual_base.check(exchange="bybit", symbol=bybit_p["symbol"])
        )
        assert_flat(dual_base.check(exchange="okx", symbol=okx_p["symbol"]))
    except BaselineError:
        return _report(
            status="baseline_not_flat",
            n_requested=n_req,
            error_code="invalid_request",
        )

    try:
        bybit_meta = metadata_provider.get(bybit_p["venue"], bybit_p["symbol"])
        okx_meta = metadata_provider.get(okx_p["venue"], okx_p["symbol"])
        assert_w6_notional(
            bybit_meta, bybit_p["qty"], min_usd=W6_BYBIT_MIN_NOTIONAL
        )
        assert_w6_notional(okx_meta, okx_p["qty"])
    except (MetadataError, W6ProfileError):
        return _report(
            status="plan_rejected",
            n_requested=n_req,
            error_code="invalid_request",
        )

    bybit_reseed = build_signed_rest_reseed(
        exchange="bybit",
        credentials=bybit_credentials,
        endpoints=endpoints_for_venue("live"),
        probe_fn=rest_probe_fn,
    )
    okx_reseed = build_signed_rest_reseed(
        exchange="okx",
        credentials=okx_credentials,
        endpoints=endpoints_for_venue("live"),
        probe_fn=rest_probe_fn,
    )
    bybit_runtime = PrivateStreamRuntime.create_gated(
        exchange="bybit",
        symbol_alias=bybit_p["symbol"],
        journal=j,
        credentials=bybit_credentials,
        env=e,
        rest_reseed=bybit_reseed,
        profile_gate=gate,
    )
    okx_runtime = PrivateStreamRuntime.create_gated(
        exchange="okx",
        symbol_alias=okx_p["symbol"],
        journal=j,
        credentials=okx_credentials,
        env=e,
        rest_reseed=okx_reseed,
        profile_gate=gate,
    )

    bybit_transport = bybit_place_override or _WsTradePlaceTransport(
        runtime=bybit_runtime, ack_timeout_sec=ack_timeout_sec
    )
    okx_transport = okx_place_override or _WsTradePlaceTransport(
        runtime=okx_runtime, ack_timeout_sec=ack_timeout_sec
    )
    try:
        assert_w6_transport_is_ws_trade(bybit_transport)
        assert_w6_transport_is_ws_trade(okx_transport)
    except W6ProfileError:
        return _report(
            status="http_transport_rejected",
            n_requested=n_req,
            error_code="transport_error",
        )

    vault = ApprovalVault(journal=j, venue="bybit", environment="live")
    bybit_sender = ApprovalBoundSender(
        journal=j,
        approval_vault=vault,
        metadata_provider=metadata_provider,
        position_mode_provider=position_mode_provider,
        transport=bybit_transport,
        data_root=root,
    )
    okx_sender = ApprovalBoundSender(
        journal=j,
        approval_vault=vault,
        metadata_provider=metadata_provider,
        position_mode_provider=position_mode_provider,
        transport=okx_transport,
        lease_supervisor=bybit_sender.lease_supervisor,
        data_root=root,
    )
    bybit_provider = WsOrderStateProvider(bybit_runtime)
    okx_provider = WsOrderStateProvider(okx_runtime)
    dual_provider = _DualOrderStateProvider(bybit=bybit_provider, okx=okx_provider)
    bybit_sender.lease_supervisor.order_state_provider = dual_provider

    orders_sent = 0
    n_completed = 0
    n_aborted = 0
    try:
        if (
            bybit_private_socket is None
            or bybit_trade_socket is None
            or okx_private_socket is None
            or okx_trade_socket is None
        ):
            return _report(
                status="sockets_required",
                n_requested=n_req,
                error_code="transport_error",
            )

        bybit_runtime.bind_sockets(
            private=bybit_private_socket, trade=bybit_trade_socket, env=e
        )
        okx_runtime.bind_sockets(
            private=okx_private_socket, trade=okx_trade_socket, env=e
        )
        trade_bound = (
            bybit_runtime.trade_socket is not None
            and okx_runtime.trade_socket is not None
        )

        for runtime, exchange in ((bybit_runtime, "bybit"), (okx_runtime, "okx")):
            hs_err = _handshake_private_and_trade(
                runtime, exchange=exchange, ack_timeout_sec=ack_timeout_sec
            )
            if hs_err is not None:
                status_map = {
                    "auth_failed": "auth_failed",
                    "venue_rejected": "subscribe_failed",
                    "reseed_required": "reseed_required",
                }
                return _report(
                    status=status_map.get(hs_err, "handshake_failed"),
                    n_requested=n_req,
                    trade_ws_bound=trade_bound,
                    subscription_ready=hs_err not in {"auth_failed"},
                    reseed_matched=False,
                    sends_blocked=True,
                    error_code="auth_failed" if "auth" in hs_err else "unknown",
                )

        inst_err = _bind_okx_inst_id(
            runtime=okx_runtime,
            metadata_provider=metadata_provider,
            profile=okx_p,
        )
        if inst_err is not None:
            return _report(
                status=inst_err,
                n_requested=n_req,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                error_code="invalid_request",
            )

        recovery_err = _recover_inflight_w6(
            sender=bybit_sender,
            bybit_runtime=bybit_runtime,
            okx_runtime=okx_runtime,
            bybit_provider=bybit_provider,
            okx_provider=okx_provider,
            bybit_transport=bybit_transport,
            okx_transport=okx_transport,
            bybit_creds=bybit_credentials,
            okx_creds=okx_credentials,
            metadata_provider=metadata_provider,
            journal=j,
            baseline=dual_base,
            rest_order_recon=rest_order_recon,
            terminal_wait_sec=float(terminal_wait_sec),
            vault=vault,
            env=e,
            issue_approval=issue_approval,
        )
        if recovery_err is not None:
            recovery_error_code = (
                "recovery_blocked" if recovery_err == "recovery_blocked"
                else "recovery_journal_failed" if recovery_err == "recovery_journal_failed"
                else "unknown"
            )
            return _report(
                status=recovery_err,
                n_requested=n_req,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                error_code=recovery_error_code,
            )

        try:
            assert_w6_okx_net_mode(
                venue=okx_p["venue"],
                position_mode_provider=position_mode_provider,
            )
        except (W6ProfileError, PreflightError):
            return _report(
                status="okx_position_mode_rejected",
                n_requested=n_req,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                error_code="invalid_request",
            )

        if not issue_approval:
            return _report(
                status="approval_required",
                n_requested=n_req,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                error_code="invalid_request",
            )

        for _round in range(n_req):
            try:
                assert_flat(
                    dual_base.check(exchange="bybit", symbol=bybit_p["symbol"])
                )
                assert_flat(
                    dual_base.check(exchange="okx", symbol=okx_p["symbol"])
                )
            except BaselineError:
                return _report(
                    status="baseline_not_flat",
                    n_requested=n_req,
                    n_completed=n_completed,
                    n_aborted=n_aborted,
                    trade_ws_bound=True,
                    subscription_ready=True,
                    reseed_matched=True,
                    orders_sent=orders_sent,
                    error_code="invalid_request",
                    latency_ms=_latency(),
                )

            if (
                bybit_runtime.reseed_required
                or bybit_runtime.sends_blocked
                or okx_runtime.reseed_required
                or okx_runtime.sends_blocked
            ):
                return _report(
                    status="stream_blocked",
                    n_requested=n_req,
                    n_completed=n_completed,
                    trade_ws_bound=True,
                    subscription_ready=True,
                    reseed_matched=False,
                    sends_blocked=True,
                    orders_sent=orders_sent,
                    error_code="unknown",
                    latency_ms=_latency(),
                )

            dual_id = new_opaque_id("dual")
            try:
                bybit_open = build_order_plan(
                    venue=bybit_p["venue"],
                    symbol=bybit_p["symbol"],
                    side=bybit_p["open_side"],
                    mode="market",
                    metadata_provider=metadata_provider,
                    qty=bybit_p["qty"],
                    reduce_only=False,
                    dual_leg_id=dual_id,
                    expires_in_sec=60,
                )
                assert_exact_w6_open_plan(bybit_open, bybit_p)
                okx_open = build_order_plan(
                    venue=okx_p["venue"],
                    symbol=okx_p["symbol"],
                    side=okx_p["open_side"],
                    mode="market",
                    metadata_provider=metadata_provider,
                    qty=okx_p["qty"],
                    reduce_only=False,
                    dual_leg_id=dual_id,
                    expires_in_sec=60,
                )
                assert_exact_w6_open_plan(okx_open, okx_p)
                okx_runtime.okx_inst_id_code = okx_open.inst_id_code
            except (OrderPlanError, W6ProfileError, MetadataError):
                return _report(
                    status="plan_rejected",
                    n_requested=n_req,
                    n_completed=n_completed,
                    trade_ws_bound=True,
                    subscription_ready=True,
                    reseed_matched=True,
                    orders_sent=orders_sent,
                    error_code="invalid_request",
                    latency_ms=_latency(),
                )

            bybit_token = vault.issue(bybit_open)
            bybit_place_kw = {
                "sender": bybit_sender,
                "runtime": bybit_runtime,
                "provider": bybit_provider,
                "place_transport": bybit_transport,
                "plan": bybit_open,
                "token": bybit_token,
                "credentials": bybit_credentials,
                "env": e,
                "journal": j,
                "terminal_wait_sec": float(terminal_wait_sec),
                "fill_inject_fn": fill_inject_fn,
                "inject_kind": "bybit_open",
            }
            okx_place_kw_base = {
                "sender": okx_sender,
                "runtime": okx_runtime,
                "provider": okx_provider,
                "place_transport": okx_transport,
                "plan": okx_open,
                "credentials": okx_credentials,
                "env": e,
                "journal": j,
                "terminal_wait_sec": float(terminal_wait_sec),
                "fill_inject_fn": fill_inject_fn,
                "inject_kind": "okx_open",
            }

            if parallel_open:
                okx_token = vault.issue(okx_open)
                okx_place_kw_base["token"] = okx_token
                b_res, o_res = _place_pair_parallel(
                    bybit_kw=bybit_place_kw, okx_kw=okx_place_kw_base
                )
                dispatch_pairs.append(
                    (b_res.dispatch_monotonic_ns, o_res.dispatch_monotonic_ns)
                )
                if b_res.transport_invoked:
                    orders_sent += 1
                if o_res.transport_invoked:
                    orders_sent += 1
                if b_res.status != "ok" or o_res.status != "ok":
                    n_aborted += 1
                    abort_reason = (
                        "peer_rejected"
                        if "ack_fail" in {b_res.status, o_res.status}
                        else (
                            "safety_guard"
                            if "journal_failed" in {b_res.status, o_res.status}
                            else "peer_timeout"
                        )
                    )
                    observed = bybit_open if b_res.status != "ok" else okx_open
                    peer = okx_open if observed is bybit_open else bybit_open
                    abort_journal_ok = _try_journal_abort(
                        j,
                        observed_plan=observed,
                        peer_plan=peer,
                        abort_reason=abort_reason,
                    )
                    bybit_flat_ok = True
                    okx_flat_ok = True
                    try:
                        b_base = dual_base.check(
                            exchange="bybit", symbol=bybit_p["symbol"]
                        )
                        if not b_base.ok:
                            flat_st, flat_n = _flatten_venue(
                                sender=bybit_sender,
                                runtime=bybit_runtime,
                                provider=bybit_provider,
                                place_transport=bybit_transport,
                                vault=vault,
                                credentials=bybit_credentials,
                                env=e,
                                metadata_provider=metadata_provider,
                                profile=bybit_p,
                                dual_leg_id=dual_id,
                                journal=j,
                                baseline=dual_base,
                                terminal_wait_sec=float(terminal_wait_sec),
                                fill_inject_fn=fill_inject_fn,
                                inject_kind="bybit_flatten",
                            )
                            orders_sent += flat_n
                            bybit_flat_ok = flat_st == "ok"
                    except BaselineError:
                        bybit_flat_ok = False
                        bybit_sender.lease_supervisor.mark_process_sends_blocked()
                    try:
                        o_base = dual_base.check(
                            exchange="okx", symbol=okx_p["symbol"]
                        )
                        if not o_base.ok:
                            o_st, o_n = _flatten_venue(
                                sender=okx_sender,
                                runtime=okx_runtime,
                                provider=okx_provider,
                                place_transport=okx_transport,
                                vault=vault,
                                credentials=okx_credentials,
                                env=e,
                                metadata_provider=metadata_provider,
                                profile=okx_p,
                                dual_leg_id=dual_id,
                                journal=j,
                                baseline=dual_base,
                                terminal_wait_sec=float(terminal_wait_sec),
                                fill_inject_fn=fill_inject_fn,
                                inject_kind="okx_flatten",
                            )
                            orders_sent += o_n
                            okx_flat_ok = o_st == "ok"
                    except BaselineError:
                        okx_flat_ok = False
                        okx_sender.lease_supervisor.mark_process_sends_blocked()
                    bybit_sender.lease_supervisor.mark_process_sends_blocked()
                    both_flat = bybit_flat_ok and okx_flat_ok
                    err = b_res.error_code or o_res.error_code
                    vcode = b_res.venue_code or o_res.venue_code
                    if not abort_journal_ok:
                        return _report(
                            status="abort_journal_failed",
                            n_requested=n_req,
                            n_completed=n_completed,
                            n_aborted=n_aborted,
                            trade_ws_bound=True,
                            subscription_ready=True,
                            reseed_matched=True,
                            sends_blocked=True,
                            orders_sent=orders_sent,
                            flat_after=both_flat,
                            error_code="internal_error",
                            venue_code=vcode,
                            latency_ms=_latency(),
                        )
                    return _report(
                        status="open_leg_incomplete"
                        if both_flat
                        else "flatten_incomplete",
                        n_requested=n_req,
                        n_completed=n_completed,
                        n_aborted=n_aborted,
                        trade_ws_bound=True,
                        subscription_ready=True,
                        reseed_matched=True,
                        sends_blocked=True,
                        orders_sent=orders_sent,
                        flat_after=both_flat,
                        error_code=err or "unknown",
                        venue_code=vcode,
                        latency_ms=_latency(),
                    )
            else:
                b_res = _place_and_wait_fill(**bybit_place_kw)
                if b_res.transport_invoked:
                    orders_sent += 1
                if b_res.status != "ok":
                    n_aborted += 1
                    abort_reason = (
                        "peer_rejected"
                        if b_res.status == "ack_fail"
                        else (
                            "safety_guard"
                            if b_res.status == "journal_failed"
                            else "peer_timeout"
                        )
                    )
                    abort_journal_ok = _try_journal_abort(
                        j,
                        observed_plan=bybit_open,
                        peer_plan=okx_open,
                        abort_reason=abort_reason,
                    )
                    flat_after = False
                    try:
                        b_base = dual_base.check(
                            exchange="bybit", symbol=bybit_p["symbol"]
                        )
                        if not b_base.ok:
                            flat_st, flat_n = _flatten_venue(
                                sender=bybit_sender,
                                runtime=bybit_runtime,
                                provider=bybit_provider,
                                place_transport=bybit_transport,
                                vault=vault,
                                credentials=bybit_credentials,
                                env=e,
                                metadata_provider=metadata_provider,
                                profile=bybit_p,
                                dual_leg_id=dual_id,
                                journal=j,
                                baseline=dual_base,
                                terminal_wait_sec=float(terminal_wait_sec),
                                fill_inject_fn=fill_inject_fn,
                                inject_kind="bybit_flatten",
                            )
                            orders_sent += flat_n
                            flat_after = flat_st == "ok"
                        else:
                            flat_after = True
                    except BaselineError:
                        bybit_sender.lease_supervisor.mark_process_sends_blocked()
                    bybit_sender.lease_supervisor.mark_process_sends_blocked()
                    if not abort_journal_ok:
                        return _report(
                            status="abort_journal_failed",
                            n_requested=n_req,
                            n_completed=n_completed,
                            n_aborted=n_aborted,
                            trade_ws_bound=True,
                            subscription_ready=True,
                            reseed_matched=True,
                            sends_blocked=True,
                            orders_sent=orders_sent,
                            flat_after=flat_after,
                            error_code="internal_error",
                            venue_code=b_res.venue_code,
                            latency_ms=_latency(),
                        )
                    return _report(
                        status="first_leg_incomplete",
                        n_requested=n_req,
                        n_completed=n_completed,
                        n_aborted=n_aborted,
                        trade_ws_bound=True,
                        subscription_ready=True,
                        reseed_matched=True,
                        sends_blocked=True,
                        orders_sent=orders_sent,
                        flat_after=flat_after,
                        error_code=b_res.error_code or "unknown",
                        venue_code=b_res.venue_code,
                        latency_ms=_latency(),
                    )

                okx_token = vault.issue(okx_open)
                okx_place_kw_base["token"] = okx_token
                o_res = _place_and_wait_fill(**okx_place_kw_base)
                if o_res.transport_invoked:
                    orders_sent += 1
                if o_res.status != "ok":
                    n_aborted += 1
                    abort_journal_ok = _try_journal_abort(
                        j,
                        observed_plan=okx_open,
                        peer_plan=bybit_open,
                        abort_reason="safety_guard",
                    )
                    flat_st, flat_n = _flatten_venue(
                        sender=bybit_sender,
                        runtime=bybit_runtime,
                        provider=bybit_provider,
                        place_transport=bybit_transport,
                        vault=vault,
                        credentials=bybit_credentials,
                        env=e,
                        metadata_provider=metadata_provider,
                        profile=bybit_p,
                        dual_leg_id=dual_id,
                        journal=j,
                        baseline=dual_base,
                        terminal_wait_sec=float(terminal_wait_sec),
                        fill_inject_fn=fill_inject_fn,
                        inject_kind="bybit_flatten",
                    )
                    orders_sent += flat_n
                    okx_flat_ok = True
                    try:
                        o_base = dual_base.check(
                            exchange="okx", symbol=okx_p["symbol"]
                        )
                        if not o_base.ok:
                            o_st, o_n = _flatten_venue(
                                sender=okx_sender,
                                runtime=okx_runtime,
                                provider=okx_provider,
                                place_transport=okx_transport,
                                vault=vault,
                                credentials=okx_credentials,
                                env=e,
                                metadata_provider=metadata_provider,
                                profile=okx_p,
                                dual_leg_id=dual_id,
                                journal=j,
                                baseline=dual_base,
                                terminal_wait_sec=float(terminal_wait_sec),
                                fill_inject_fn=fill_inject_fn,
                                inject_kind="okx_flatten",
                            )
                            orders_sent += o_n
                            okx_flat_ok = o_st == "ok"
                    except BaselineError:
                        okx_flat_ok = False
                        okx_sender.lease_supervisor.mark_process_sends_blocked()
                    bybit_sender.lease_supervisor.mark_process_sends_blocked()
                    both_flat = flat_st == "ok" and okx_flat_ok
                    if not abort_journal_ok:
                        return _report(
                            status="abort_journal_failed",
                            n_requested=n_req,
                            n_completed=n_completed,
                            n_aborted=n_aborted,
                            trade_ws_bound=True,
                            subscription_ready=True,
                            reseed_matched=True,
                            sends_blocked=True,
                            orders_sent=orders_sent,
                            flat_after=both_flat,
                            error_code="internal_error",
                            venue_code=o_res.venue_code,
                            latency_ms=_latency(),
                        )
                    return _report(
                        status="second_leg_incomplete"
                        if both_flat
                        else "flatten_incomplete",
                        n_requested=n_req,
                        n_completed=n_completed,
                        n_aborted=n_aborted,
                        trade_ws_bound=True,
                        subscription_ready=True,
                        reseed_matched=True,
                        sends_blocked=True,
                        orders_sent=orders_sent,
                        flat_after=both_flat,
                        error_code=o_res.error_code or "unknown",
                        venue_code=o_res.venue_code,
                        latency_ms=_latency(),
                    )

            flat_b, n_b = _flatten_venue(
                sender=bybit_sender,
                runtime=bybit_runtime,
                provider=bybit_provider,
                place_transport=bybit_transport,
                vault=vault,
                credentials=bybit_credentials,
                env=e,
                metadata_provider=metadata_provider,
                profile=bybit_p,
                dual_leg_id=dual_id,
                journal=j,
                baseline=dual_base,
                terminal_wait_sec=float(terminal_wait_sec),
                fill_inject_fn=fill_inject_fn,
                inject_kind="bybit_flatten",
            )
            orders_sent += n_b
            if flat_b != "ok":
                o_st, o_n = _flatten_venue(
                    sender=okx_sender,
                    runtime=okx_runtime,
                    provider=okx_provider,
                    place_transport=okx_transport,
                    vault=vault,
                    credentials=okx_credentials,
                    env=e,
                    metadata_provider=metadata_provider,
                    profile=okx_p,
                    dual_leg_id=dual_id,
                    journal=j,
                    baseline=dual_base,
                    terminal_wait_sec=float(terminal_wait_sec),
                    fill_inject_fn=fill_inject_fn,
                    inject_kind="okx_flatten",
                )
                orders_sent += o_n
                bybit_sender.lease_supervisor.mark_process_sends_blocked()
                return _report(
                    status="flatten_incomplete",
                    n_requested=n_req,
                    n_completed=n_completed,
                    n_aborted=n_aborted,
                    trade_ws_bound=True,
                    subscription_ready=True,
                    reseed_matched=True,
                    sends_blocked=True,
                    orders_sent=orders_sent,
                    error_code="unknown",
                    latency_ms=_latency(),
                )

            flat_o, n_o = _flatten_venue(
                sender=okx_sender,
                runtime=okx_runtime,
                provider=okx_provider,
                place_transport=okx_transport,
                vault=vault,
                credentials=okx_credentials,
                env=e,
                metadata_provider=metadata_provider,
                profile=okx_p,
                dual_leg_id=dual_id,
                journal=j,
                baseline=dual_base,
                terminal_wait_sec=float(terminal_wait_sec),
                fill_inject_fn=fill_inject_fn,
                inject_kind="okx_flatten",
            )
            orders_sent += n_o
            if flat_o != "ok":
                return _report(
                    status="flatten_incomplete",
                    n_requested=n_req,
                    n_completed=n_completed,
                    n_aborted=n_aborted,
                    trade_ws_bound=True,
                    subscription_ready=True,
                    reseed_matched=True,
                    sends_blocked=True,
                    orders_sent=orders_sent,
                    error_code="unknown",
                    latency_ms=_latency(),
                )
            n_completed += 1

        latency = _latency()
        try:
            assert_flat(
                dual_base.check(exchange="bybit", symbol=bybit_p["symbol"])
            )
            assert_flat(dual_base.check(exchange="okx", symbol=okx_p["symbol"]))
            flat_after = True
        except BaselineError:
            flat_after = False
            bybit_sender.lease_supervisor.mark_process_sends_blocked()
            return _report(
                status="flatten_incomplete",
                n_requested=n_req,
                n_completed=n_completed,
                n_aborted=n_aborted,
                trade_ws_bound=True,
                subscription_ready=True,
                reseed_matched=True,
                sends_blocked=True,
                orders_sent=orders_sent,
                flat_after=False,
                error_code="unknown",
                latency_ms=latency,
            )

        return _report(
            status="ok",
            n_requested=n_req,
            n_completed=n_completed,
            n_aborted=n_aborted,
            subscription_ready=True,
            reseed_matched=True,
            sends_blocked=bybit_runtime.sends_blocked or okx_runtime.sends_blocked,
            trade_ws_bound=True,
            orders_sent=orders_sent,
            flat_after=flat_after,
            latency_ms=latency,
        )
    finally:
        for runtime in (bybit_runtime, okx_runtime):
            for sock in (runtime.private_socket, runtime.trade_socket):
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:  # noqa: BLE001
                        pass


def parse_w6_cli_args(argv: Sequence[str]) -> tuple[Optional[int], bool]:
    n: Optional[int] = None
    approve = False
    for arg in argv:
        if arg.startswith("--w6-n="):
            raw = arg.split("=", 1)[1].strip()
            try:
                n = int(raw)
            except ValueError:
                n = -1
        elif arg == "--w6-approve-one-shot":
            approve = True
    return n, approve


@dataclass
class W6RuntimeBindings:
    bybit_credentials: LiveCredentials
    okx_credentials: LiveCredentials
    metadata_provider: MetadataProvider
    position_mode_provider: PositionModeProvider
    baseline: DualFlatBaseline
    bybit_private_socket: PrivateWsSocket
    bybit_trade_socket: PrivateWsSocket
    okx_private_socket: PrivateWsSocket
    okx_trade_socket: PrivateWsSocket
    rest_order_recon: Optional[Any] = None


def _public_http_get_json(url: str, headers: Mapping[str, str]):
    from app.bot.private.ws_w4_postonly import _public_http_get_json as _w4_get

    return _w4_get(url, headers)


def open_w6_production_bindings(
    *,
    env: Mapping[str, str],
) -> W6RuntimeBindings:
    """Signed flat baseline both venues before any private/trade sockets."""
    from app.bot.private.order_preflight import (
        LiveHttpMetadataProvider,
        LiveSignedPositionModeProvider,
    )
    from app.bot.private.ws_private import trade_ws_url_for_exchange
    from app.bot.private.ws_socket import WebsocketsSocketFactory, bind_socket_factory
    from app.bot.private.ws_w4_baseline import (
        SignedRestFlatBaseline,
        build_signed_rest_order_recon,
    )

    bybit_p = resolve_w6_leg("bybit")
    okx_p = resolve_w6_leg("okx")
    secrets = load_live_secrets(dict(env), require_complete=True)
    bybit_creds = _creds_from_live(secrets, "bybit")
    okx_creds = _creds_from_live(secrets, "okx")
    ep = endpoints_for_venue("live")
    meta = LiveHttpMetadataProvider(http_get_json=_public_http_get_json)
    bybit_pos = LiveSignedPositionModeProvider(
        exchange="bybit",
        credentials=bybit_creds,
        bybit_base=ep.bybit_rest,
        okx_base=ep.okx_rest,
        symbol=bybit_p["symbol"],
    )
    okx_pos = LiveSignedPositionModeProvider(
        exchange="okx",
        credentials=okx_creds,
        bybit_base=ep.bybit_rest,
        okx_base=ep.okx_rest,
        symbol=okx_p["symbol"],
    )
    pos = DualPositionModeProvider(bybit=bybit_pos, okx=okx_pos)
    bybit_base = SignedRestFlatBaseline(
        exchange="bybit",
        credentials=bybit_creds,
        endpoints=ep,
    )
    okx_base = SignedRestFlatBaseline(
        exchange="okx",
        credentials=okx_creds,
        endpoints=ep,
    )
    baseline = DualFlatBaseline(bybit=bybit_base, okx=okx_base)
    rest_order_recon = build_signed_rest_order_recon(
        bybit_credentials=bybit_creds,
        okx_credentials=okx_creds,
        endpoints=ep,
        require_position_flat=True,
    )
    assert_flat(baseline.check(exchange="bybit", symbol=bybit_p["symbol"]))
    assert_flat(baseline.check(exchange="okx", symbol=okx_p["symbol"]))
    assert_w6_okx_net_mode(venue=okx_p["venue"], position_mode_provider=pos)
    assert_w6_notional(
        meta.get(bybit_p["venue"], bybit_p["symbol"]),
        bybit_p["qty"],
        min_usd=W6_BYBIT_MIN_NOTIONAL,
    )
    assert_w6_notional(meta.get(okx_p["venue"], okx_p["symbol"]), okx_p["qty"])

    factory = WebsocketsSocketFactory()
    bind_socket_factory(factory)
    bybit_priv = factory.open(ep.bybit_private_ws)
    bybit_trade = factory.open(trade_ws_url_for_exchange("bybit", ep))
    okx_priv = factory.open(ep.okx_private_ws)
    okx_trade = factory.open(trade_ws_url_for_exchange("okx", ep))
    return W6RuntimeBindings(
        bybit_credentials=bybit_creds,
        okx_credentials=okx_creds,
        metadata_provider=meta,
        position_mode_provider=pos,
        baseline=baseline,
        bybit_private_socket=bybit_priv,
        bybit_trade_socket=bybit_trade,
        okx_private_socket=okx_priv,
        okx_trade_socket=okx_trade,
        rest_order_recon=rest_order_recon,
    )


def main_ws_w6_dual_leg(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    bindings: Optional[W6RuntimeBindings] = None,
) -> int:
    """CLI entry for ``--ws-w6-dual-leg --w6-n=N --w6-approve-one-shot``."""
    argv = list(argv or [])
    e = dict(env if env is not None else os.environ)
    n, approve_one_shot = parse_w6_cli_args(argv)

    def _print(report: W6Report) -> None:
        print(json.dumps(report.as_public_dict(), ensure_ascii=False, indent=2, sort_keys=True))

    try:
        assert_ws_w6_send_gates(e)
        if n is None:
            raise W6ProfileError("W6 requires --w6-n=1..20")
        assert_w6_n(n)
    except (WsProfileGateError, W6ProfileError):
        _print(
            W6Report(
                status="rejected_before_socket",
                n_requested=n if isinstance(n, int) and n > 0 else 0,
                error_code="invalid_request",
            )
        )
        return 1

    if not approve_one_shot:
        _print(
            W6Report(
                status="approval_required",
                n_requested=n,
                error_code="invalid_request",
            )
        )
        return 1

    assert_default_entrypoint_cannot_transport()

    owned = bindings is None
    active: Optional[W6RuntimeBindings] = bindings
    try:
        if active is None:
            try:
                assert_no_default_ws_socket()
            except RuntimeError:
                _print(
                    W6Report(
                        status="rejected_before_socket",
                        n_requested=n,
                        error_code="transport_error",
                    )
                )
                return 2
            try:
                active = open_w6_production_bindings(env=e)
            except (
                BaselineError,
                W6ProfileError,
                PreflightError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
            ):
                unbind_socket_factory()
                _print(
                    W6Report(
                        status="bind_failed",
                        n_requested=n,
                        error_code="transport_error",
                    )
                )
                return 2

        report = run_w6_dual_leg(
            n=n,
            env=e,
            metadata_provider=active.metadata_provider,
            position_mode_provider=active.position_mode_provider,
            baseline=active.baseline,
            bybit_private_socket=active.bybit_private_socket,
            bybit_trade_socket=active.bybit_trade_socket,
            okx_private_socket=active.okx_private_socket,
            okx_trade_socket=active.okx_trade_socket,
            bybit_credentials=active.bybit_credentials,
            okx_credentials=active.okx_credentials,
            load_secrets=False,
            issue_approval=True,
            rest_order_recon=active.rest_order_recon,
        )
        _print(report)
        if report.status == "ok":
            return 0
        if report.status in {
            "secrets_unavailable",
            "approval_required",
            "rejected_before_socket",
        }:
            return 1
        return 2
    finally:
        if owned:
            unbind_socket_factory()
            assert_default_entrypoint_cannot_transport()

"""EV2-08 Gear 2.2 strategy bridge tests. Deterministic, no I/O or submit."""

from __future__ import annotations

import ast
import inspect
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from app.bot.execution.contracts import (
    SCHEMA_VERSION,
    ExecutionEvent,
    ExecutionEventType,
    IntentAction,
    SpreadDirection,
    SpreadStatus,
    Venue,
    derive_client_id,
)
from app.bot.execution.state_machine import apply_events, initial_spread_state
from app.bot.execution.strategy_bridge import (
    CANARY_STAGE,
    CONTEXT_SCHEMA_VERSION,
    DEFAULT_NOTIONAL_USDT,
    INTENT_TTL_NS,
    PARITY_SCHEMA_VERSION,
    RISK_POLICY_REVISION,
    SCHEMA_VERSION as BRIDGE_SCHEMA,
    BridgeClocks,
    BridgeConfig,
    BridgeIds,
    DivergenceClass,
    DivergenceFacts,
    Gear22StrategyBridge,
    OpenTradeContext,
    SlotKind,
    StrategyBridgeError,
    classify_divergence,
    commit_open_context,
    fill_spread_from_quotes,
    project_slot,
    restore_context,
)
from app.bot.theta_screener import ThetaSnapshot
from app.bot.theta_trade_manager import (
    DEFAULT_LIVE_CANARY_NOTIONAL_USDT,
    GEAR22_HTML_TOP30,
    POLICY_ID,
    OpenPosition,
    SlotState,
    ThetaDecision,
    decide_theta_k1,
)
from research.gear22_backtest.params_frozen import DEFAULT_OBSERVE_PARAMS, PREVIOUS
from research.gear22_backtest.policy import PolicyParams

RUN_ID = "run_aa11bb22cc33dd44ee55ff6677889900"
OKX_LEG = "leg_okx"
BYBIT_LEG = "leg_bybit"
QTY = "1"
MONO_NS = 1_000_000_000
WALL_NS = 1_750_000_000_000_000_000
FORBIDDEN = ("api_key", "api_secret", "passphrase", "signature", "order_id", "fill_price")


class Clock:
    def __init__(self, *, mono: int = 1000) -> None:
        self.seq = 0
        self.mono = mono

    def next(self) -> tuple[int, int]:
        self.seq += 1
        self.mono += 1
        return self.seq, self.mono


class SeqIds:
    def __init__(self) -> None:
        self.n = 0

    def new_intent_id(self) -> str:
        self.n += 1
        return f"intent{self.n:024d}"


class FixedClocks:
    def __init__(self, mono: int = MONO_NS, wall: int = WALL_NS) -> None:
        self.mono = mono
        self.wall = wall

    def monotonic_ns(self) -> int:
        return self.mono

    def wall_ns(self) -> int:
        return self.wall


def _snap(
    coin: str,
    side: str,
    theta_1m: float | None,
    *,
    floor: float = 0.20,
    p50_1m: float | None = None,
    ts_ms: int = 1_700_000_000_000,
) -> ThetaSnapshot:
    p1 = p50_1m if p50_1m is not None else (
        (floor + theta_1m) if theta_1m is not None else None
    )
    return ThetaSnapshot(
        base_coin=coin,
        side=side,
        ts_ms=ts_ms,
        p50_1m=p1,
        p50_5m=p1,
        floor_tf_select_a25=floor,
        theta_1m=theta_1m,
        theta_5m=(p1 - floor) if p1 is not None else None,
        computed_at_ms=ts_ms + 1,
    )


def _books(
    *,
    okx_ask: float = 100.0,
    okx_bid: float = 99.0,
    bybit_ask: float = 100.5,
    bybit_bid: float = 100.2,
    okx_ask_sz: float = 10.0,
    okx_bid_sz: float = 10.0,
    bybit_ask_sz: float = 10.0,
    bybit_bid_sz: float = 10.0,
) -> dict[str, Any]:
    return {
        "okx": {
            "bid_price": okx_bid,
            "ask_price": okx_ask,
            "bid_size": okx_bid_sz,
            "ask_size": okx_ask_sz,
            "local_recv_ts_ms": 1,
        },
        "bybit": {
            "bid_price": bybit_bid,
            "ask_price": bybit_ask,
            "bid_size": bybit_bid_sz,
            "ask_size": bybit_ask_sz,
            "local_recv_ts_ms": 1,
        },
    }


def _qualify(coin: str = "KAITO") -> list[ThetaSnapshot]:
    return [
        _snap(coin, "long", 0.60, p50_1m=0.80, floor=0.20),
        _snap(coin, "short", 0.01),
    ]


def _hold_snaps(coin: str = "KAITO") -> list[ThetaSnapshot]:
    return [
        _snap(coin, "long", 0.10, p50_1m=0.30, floor=0.20),
        _snap(coin, "short", 0.01, p50_1m=0.10, floor=0.09),
    ]


def _close_snaps(coin: str = "KAITO") -> list[ThetaSnapshot]:
    return [
        _snap(coin, "long", 0.30, p50_1m=0.80, floor=0.50),
        _snap(coin, "short", 0.60, p50_1m=1.20, floor=0.60),
    ]


def _close_quotes(coin: str = "KAITO") -> dict[str, Any]:
    return {coin: _books(okx_bid=100.5, bybit_ask=100.0)}


def _open_quotes(*coins: str) -> dict[str, Any]:
    if not coins:
        coins = ("KAITO",)
    return {coin: _books() for coin in coins}


def _bridge(
    *,
    ids: Optional[SeqIds] = None,
    clocks: Optional[FixedClocks] = None,
    config: Optional[BridgeConfig] = None,
) -> tuple[Gear22StrategyBridge, SeqIds, FixedClocks]:
    seq = ids or SeqIds()
    clock = clocks or FixedClocks()
    cfg = config or BridgeConfig(run_id=RUN_ID)
    bridge = Gear22StrategyBridge(
        run_id=RUN_ID,
        config=cfg,
        clocks=BridgeClocks(monotonic_ns=clock.monotonic_ns, wall_ns=clock.wall_ns),
        ids=BridgeIds(new_intent_id=seq.new_intent_id),
    )
    return bridge, seq, clock


def _event(
    clock: Clock,
    event_type: ExecutionEventType,
    *,
    intent_id: str,
    venue: Optional[Venue] = None,
    leg_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> ExecutionEvent:
    seq, mono = clock.next()
    return ExecutionEvent(
        schema_version=SCHEMA_VERSION,
        event_id=f"evt_{mono:032d}",
        event_type=event_type,
        intent_id=intent_id,
        run_id=RUN_ID,
        sequence=seq,
        monotonic_ns=mono,
        venue=venue,
        leg_id=leg_id,
        payload=payload or {},
    )


def _arm(
    clock: Clock,
    intent_id: str,
    *,
    coin: str = "KAITO",
    direction: str = "long",
    action: str = "open",
) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.INTENT_ACCEPTED,
        intent_id=intent_id,
        payload={
            "action": action,
            "coin": coin,
            "spread_direction": direction,
            "lot_tolerance": "0",
        },
    )


def _sent(
    clock: Clock,
    venue: Venue,
    leg_id: str,
    intent_id: str,
    *,
    reduce_only: bool = False,
    coin: str = "KAITO",
) -> ExecutionEvent:
    side = "buy" if venue is Venue.OKX else "sell"
    if reduce_only:
        side = "sell" if venue is Venue.OKX else "buy"
    instrument = f"{coin}-USDT-SWAP" if venue is Venue.OKX else f"{coin}USDT"
    return _event(
        clock,
        ExecutionEventType.REQUEST_SENT,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={
            "quantity": QTY,
            "reduce_only": reduce_only,
            "instrument": instrument,
            "side": side,
            "client_id": derive_client_id(intent_id, venue, reduce_only=reduce_only),
        },
    )


def _ack(clock: Clock, venue: Venue, leg_id: str, intent_id: str, *, ok: bool = True) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.ACK_ACCEPTED if ok else ExecutionEventType.ACK_REJECTED,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={} if ok else {"reason_code": "venue_rejected"},
    )


def _fill(clock: Clock, venue: Venue, leg_id: str, intent_id: str) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.FILL,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"quantity": QTY},
    )


def _pos(clock: Clock, venue: Venue, leg_id: str, intent_id: str, quantity: str) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.POSITION_OBSERVED,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"quantity": quantity},
    )


def _orders(clock: Clock, venue: Venue, leg_id: str, intent_id: str, count: int) -> ExecutionEvent:
    return _event(
        clock,
        ExecutionEventType.OPEN_ORDERS_OBSERVED,
        intent_id=intent_id,
        venue=venue,
        leg_id=leg_id,
        payload={"open_order_count": count},
    )


def _dispatch(clock: Clock, intent_id: str, *, coin: str = "KAITO") -> list[ExecutionEvent]:
    return [
        _arm(clock, intent_id, coin=coin),
        _sent(clock, Venue.OKX, OKX_LEG, intent_id, coin=coin),
        _sent(clock, Venue.BYBIT, BYBIT_LEG, intent_id, coin=coin),
    ]


def _happy_open(clock: Clock, intent_id: str, *, coin: str = "KAITO") -> list[ExecutionEvent]:
    return [
        *_dispatch(clock, intent_id, coin=coin),
        _ack(clock, Venue.OKX, OKX_LEG, intent_id),
        _ack(clock, Venue.BYBIT, BYBIT_LEG, intent_id),
        _fill(clock, Venue.OKX, OKX_LEG, intent_id),
        _fill(clock, Venue.BYBIT, BYBIT_LEG, intent_id),
    ]


def _flat_after_open(clock: Clock, open_id: str, close_id: str, *, coin: str = "KAITO") -> list[ExecutionEvent]:
    open_events = _happy_open(clock, open_id, coin=coin)
    close_clock = Clock(mono=clock.mono)
    return [
        *open_events,
        _arm(close_clock, close_id, coin=coin, action="close"),
        _sent(close_clock, Venue.OKX, OKX_LEG, close_id, reduce_only=True, coin=coin),
        _sent(close_clock, Venue.BYBIT, BYBIT_LEG, close_id, reduce_only=True, coin=coin),
        _ack(close_clock, Venue.OKX, OKX_LEG, close_id),
        _ack(close_clock, Venue.BYBIT, BYBIT_LEG, close_id),
        _fill(close_clock, Venue.OKX, OKX_LEG, close_id),
        _fill(close_clock, Venue.BYBIT, BYBIT_LEG, close_id),
        _pos(close_clock, Venue.OKX, OKX_LEG, close_id, "0"),
        _pos(close_clock, Venue.BYBIT, BYBIT_LEG, close_id, "0"),
        _orders(close_clock, Venue.OKX, OKX_LEG, close_id, 0),
        _orders(close_clock, Venue.BYBIT, BYBIT_LEG, close_id, 0),
        _event(
            close_clock,
            ExecutionEventType.FLATNESS_PROVEN,
            intent_id=close_id,
            payload={"positions_flat": True, "open_orders_flat": True},
        ),
    ]


def _context_for(
    intent_id: str,
    *,
    coin: str = "KAITO",
    side: str = "long",
    quotes: Optional[dict[str, Any]] = None,
    fill_spread_pp: Optional[float] = None,
    theta: float = 0.60,
) -> OpenTradeContext:
    books = quotes or _open_quotes(coin)
    spread = fill_spread_pp
    if spread is None:
        spread = fill_spread_from_quotes(books, coin, side)
    assert spread is not None
    return OpenTradeContext(
        schema_version=CONTEXT_SCHEMA_VERSION,
        trade_id=intent_id,
        open_intent_id=intent_id,
        coin=coin,
        side=side,
        open_signal_ts_ms=1_700_000_000_000,
        open_fill_ts_ms=1_700_000_000_000,
        fill_spread_pp=spread,
        open_theta_1m=theta,
        open_notional=DEFAULT_NOTIONAL_USDT,
        signal_snapshot_ref="h" + ("ab" * 32),
        signal_mono_ns=MONO_NS,
        signal_wall_ns=WALL_NS,
    )


def _assert_no_forbidden(payload: dict[str, Any]) -> None:
    blob = str(payload).lower()
    for key in FORBIDDEN:
        if key in blob:
            raise AssertionError(f"forbidden {key} leaked")


class LockedConstantsTests(unittest.TestCase):
    def test_exact_html_top30_order(self) -> None:
        self.assertEqual(
            GEAR22_HTML_TOP30,
            (
                "KAITO",
                "HOME",
                "WAL",
                "RVN",
                "ONT",
                "2Z",
                "BICO",
                "HMSTR",
                "CAP",
                "BLEND",
                "EDEN",
                "KMNO",
                "GPS",
                "ME",
                "ZBT",
                "MOVE",
                "COAI",
                "AZTEC",
                "APR",
                "YB",
                "ICX",
                "AT",
                "H",
                "MUBARAK",
                "ACU",
                "LA",
                "BEAT",
                "PARTI",
                "SIGN",
                "GIGGLE",
            ),
        )
        self.assertEqual(len(GEAR22_HTML_TOP30), 30)
        self.assertEqual(GEAR22_HTML_TOP30[0], "KAITO")
        self.assertEqual(GEAR22_HTML_TOP30[-1], "GIGGLE")

    def test_frozen_params_ttl_and_notional(self) -> None:
        self.assertEqual(DEFAULT_OBSERVE_PARAMS.theta_open, 0.50)
        self.assertEqual(DEFAULT_OBSERVE_PARAMS.p50_open, 0.60)
        self.assertEqual(DEFAULT_OBSERVE_PARAMS.min_profit_pp, 0.20)
        self.assertEqual(DEFAULT_OBSERVE_PARAMS.min_theta_close, 0.05)
        self.assertEqual(POLICY_ID, "gear22_frozen_v1")
        self.assertEqual(INTENT_TTL_NS, 1_000_000_000)
        self.assertEqual(DEFAULT_NOTIONAL_USDT, Decimal("10"))
        self.assertEqual(DEFAULT_LIVE_CANARY_NOTIONAL_USDT, 10.0)
        self.assertEqual(CANARY_STAGE, "gear22_live_canary")
        self.assertEqual(RISK_POLICY_REVISION, "risk.v1")

    def test_divergence_enum_is_finite_without_unknown(self) -> None:
        values = {item.value for item in DivergenceClass}
        self.assertEqual(
            values,
            {
                "match",
                "ack_not_open",
                "pending_inflight",
                "fill_model",
                "missing_open_context",
                "engine_gate",
                "id_scheme",
                "notional_policy",
                "coin_order",
                "param_override",
                "size_gate",
                "close_while_not_open",
            },
        )
        self.assertNotIn("unknown", values)
        with self.assertRaises(StrategyBridgeError) as ctx:
            classify_divergence(DivergenceFacts(decisions_match=False))
        self.assertEqual(ctx.exception.reason_code, "unclassified_non_match")


class CoinOrderAndScanTests(unittest.TestCase):
    def test_long_first_when_both_sides_qualify(self) -> None:
        snaps = [
            _snap("KAITO", "long", 0.60, p50_1m=0.80, floor=0.20),
            _snap("KAITO", "short", 0.70, p50_1m=0.90, floor=0.20),
        ]
        bridge, _, _ = _bridge()
        tick = bridge.observe(snaps, _open_quotes("KAITO"), initial_spread_state(run_id=RUN_ID))
        self.assertEqual(tick.decision.action, "open")
        self.assertEqual(tick.decision.side, "long")
        self.assertEqual(tick.decision.base_coin, "KAITO")
        self.assertEqual(tick.intent.spread_direction, SpreadDirection.LONG)

    def test_first_eligible_follows_html_top30(self) -> None:
        snaps = _hold_snaps("KAITO") + _qualify("HOME") + _qualify("GIGGLE")
        quotes = _open_quotes("KAITO", "HOME", "GIGGLE")
        bridge, _, _ = _bridge()
        tick = bridge.observe(snaps, quotes, initial_spread_state(run_id=RUN_ID))
        self.assertEqual(tick.decision.base_coin, "HOME")
        self.assertEqual(tick.parity.coin_rank, GEAR22_HTML_TOP30.index("HOME") + 1)

    def test_reversed_order_is_coin_order_divergence(self) -> None:
        snaps = _qualify("KAITO") + _qualify("GIGGLE")
        quotes = _open_quotes("KAITO", "GIGGLE")
        reversed_order = tuple(reversed(GEAR22_HTML_TOP30))
        bridge, _, _ = _bridge(config=BridgeConfig(run_id=RUN_ID, coin_order=reversed_order))
        tick = bridge.observe(snaps, quotes, initial_spread_state(run_id=RUN_ID))
        self.assertEqual(tick.decision.base_coin, "GIGGLE")
        self.assertEqual(tick.divergence, DivergenceClass.COIN_ORDER)
        self.assertIsNone(tick.intent)
        self.assertIsNone(bridge.inflight)


class EquivalentSlotParityTests(unittest.TestCase):
    def test_projected_open_slot_matches_legacy_decide(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        bridge, seq, _ = _bridge()
        open_tick = bridge.observe(_qualify(), _open_quotes(), idle)
        self.assertEqual(open_tick.decision.action, "open")
        intent_id = open_tick.intent.intent_id
        opened = apply_events(idle, _happy_open(Clock(), intent_id))
        quotes = _open_quotes()
        context = _context_for(intent_id, quotes=quotes, fill_spread_pp=1.0)
        bridge.commit_open_context(opened, context)
        close_snaps = _close_snaps()
        close_quotes = _close_quotes()
        tick = bridge.observe(close_snaps, close_quotes, opened)
        legacy = decide_theta_k1(
            close_snaps,
            slot=SlotState(position=_position(context), pending=False),
            thr=0.2,
            quotes=close_quotes,
            notional_usdt=10.0,
            coin_order=GEAR22_HTML_TOP30,
            policy_params=DEFAULT_OBSERVE_PARAMS,
        )
        self.assertEqual(tick.decision.action, legacy.action)
        self.assertEqual(tick.decision.reason, legacy.reason)
        self.assertEqual(tick.decision.base_coin, legacy.base_coin)
        self.assertEqual(tick.decision.side, legacy.side)
        self.assertEqual(tick.divergence, DivergenceClass.MATCH)

    def test_free_slot_open_matches_legacy(self) -> None:
        snaps = _qualify()
        quotes = _open_quotes()
        bridge, _, _ = _bridge()
        tick = bridge.observe(snaps, quotes, initial_spread_state(run_id=RUN_ID))
        legacy = decide_theta_k1(
            snaps,
            slot=SlotState(),
            thr=0.2,
            quotes=quotes,
            notional_usdt=10.0,
            coin_order=GEAR22_HTML_TOP30,
            policy_params=DEFAULT_OBSERVE_PARAMS,
        )
        self.assertEqual(tick.decision.action, legacy.action)
        self.assertEqual(tick.decision.side, legacy.side)
        self.assertEqual(tick.decision.base_coin, legacy.base_coin)
        self.assertEqual(tick.divergence, DivergenceClass.MATCH)


def _position(context: OpenTradeContext) -> OpenPosition:
    return OpenPosition(
        trade_id=context.trade_id,
        base_coin=context.coin,
        side=context.side,
        open_signal_ts_ms=context.open_signal_ts_ms,
        open_fill_ts_ms=context.open_fill_ts_ms,
        open_fill_spread=context.fill_spread_pp,
        open_notional=float(context.open_notional),
        open_theta_1m=context.open_theta_1m,
        fill_spread_pp=context.fill_spread_pp,
    )


class IntentIdentityTests(unittest.TestCase):
    def test_open_trade_id_equals_intent_id(self) -> None:
        bridge, _, _ = _bridge()
        tick = bridge.observe(_qualify(), _open_quotes(), initial_spread_state(run_id=RUN_ID))
        self.assertIsNotNone(tick.intent)
        self.assertEqual(tick.intent.action, IntentAction.OPEN)
        self.assertEqual(tick.trade_id, tick.intent.intent_id)
        self.assertEqual(tick.intent.coin, "KAITO")
        self.assertEqual(tick.intent.spread_direction, SpreadDirection.LONG)
        self.assertEqual(tick.intent.notional_usdt, Decimal("10"))
        self.assertEqual(tick.intent.policy_version, POLICY_ID)
        self.assertEqual(tick.intent.expiry_mono_ns - tick.intent.signal_mono_ns, INTENT_TTL_NS)
        self.assertEqual(tick.intent.signal_snapshot_ref, tick.snapshot_ref)

    def test_close_gets_new_intent_and_retains_open_trade_id(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        bridge, _, _ = _bridge()
        open_tick = bridge.observe(_qualify(), _open_quotes(), idle)
        open_id = open_tick.intent.intent_id
        opened = apply_events(idle, _happy_open(Clock(), open_id))
        bridge.commit_open_context(opened, _context_for(open_id, fill_spread_pp=1.0))
        close_tick = bridge.observe(_close_snaps(), _close_quotes(), opened)
        self.assertEqual(close_tick.decision.action, "close")
        self.assertIsNotNone(close_tick.intent)
        self.assertEqual(close_tick.intent.action, IntentAction.CLOSE)
        self.assertNotEqual(close_tick.intent.intent_id, open_id)
        self.assertEqual(close_tick.trade_id, open_id)
        self.assertEqual(close_tick.intent.coin, "KAITO")
        self.assertEqual(close_tick.intent.spread_direction, SpreadDirection.LONG)
        self.assertEqual(close_tick.divergence, DivergenceClass.MATCH)


class AckIsNotAuthorityTests(unittest.TestCase):
    def test_armed_never_commits_or_opens(self) -> None:
        intent_id = "intent000000000000000000000001"
        armed = apply_events(initial_spread_state(run_id=RUN_ID), [_arm(Clock(), intent_id)])
        self.assertEqual(armed.status, SpreadStatus.ARMED)
        projection = project_slot(armed, None, None)
        self.assertIsNone(projection.position)
        self.assertTrue(projection.pending)
        self.assertTrue(projection.ack_not_open)
        self.assertFalse(projection.allow_open)
        self.assertFalse(projection.allow_close)
        with self.assertRaises(StrategyBridgeError) as ctx:
            commit_open_context(armed, _context_for(intent_id))
        self.assertEqual(ctx.exception.reason_code, "ack_not_open")
        bridge, _, _ = _bridge()
        tick = bridge.observe(_qualify(), _open_quotes(), armed)
        self.assertIsNone(tick.intent)
        self.assertEqual(tick.divergence, DivergenceClass.ACK_NOT_OPEN)

    def test_ack_only_dispatching_never_creates_position(self) -> None:
        intent_id = "intent000000000000000000000002"
        clock = Clock()
        state = apply_events(
            initial_spread_state(run_id=RUN_ID),
            [
                *_dispatch(clock, intent_id),
                _ack(clock, Venue.OKX, OKX_LEG, intent_id),
                _ack(clock, Venue.BYBIT, BYBIT_LEG, intent_id),
            ],
        )
        self.assertEqual(state.status, SpreadStatus.DISPATCHING)
        projection = project_slot(state, None, None)
        self.assertIsNone(projection.position)
        self.assertTrue(projection.ack_not_open)
        bridge, _, _ = _bridge()
        tick = bridge.observe(_qualify(), _open_quotes(), state)
        self.assertIsNone(tick.intent)
        self.assertIsNone(bridge.context)
        self.assertEqual(tick.divergence, DivergenceClass.ACK_NOT_OPEN)


class OpenContextAuthorityTests(unittest.TestCase):
    def test_open_without_context_fails_closed(self) -> None:
        intent_id = "intent000000000000000000000003"
        opened = apply_events(initial_spread_state(run_id=RUN_ID), _happy_open(Clock(), intent_id))
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        projection = project_slot(opened, None, None)
        self.assertTrue(projection.fail_closed_missing_context)
        self.assertIsNone(projection.position)
        self.assertFalse(projection.allow_close)
        bridge, _, _ = _bridge()
        tick = bridge.observe(_close_snaps(), _close_quotes(), opened)
        self.assertIsNone(tick.intent)
        self.assertEqual(tick.divergence, DivergenceClass.MISSING_OPEN_CONTEXT)
        self.assertNotEqual(tick.decision.action, "close")

    def test_open_with_matching_context_can_close(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        bridge, _, _ = _bridge()
        open_tick = bridge.observe(_qualify(), _open_quotes(), idle)
        opened = apply_events(idle, _happy_open(Clock(), open_tick.intent.intent_id))
        committed = bridge.commit_open_context(
            opened, _context_for(open_tick.intent.intent_id, fill_spread_pp=1.0)
        )
        self.assertEqual(committed.trade_id, opened.open_intent_id)
        tick = bridge.observe(_close_snaps(), _close_quotes(), opened)
        self.assertEqual(tick.decision.action, "close")
        self.assertIsNotNone(tick.intent)
        self.assertEqual(tick.projection.slot_kind, SlotKind.OPEN)


class InflightLatchTests(unittest.TestCase):
    def test_two_observations_before_accept_emit_one_open(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        bridge, _, _ = _bridge()
        first = bridge.observe(_qualify(), _open_quotes(), idle)
        second = bridge.observe(_qualify(), _open_quotes(), idle)
        self.assertIsNotNone(first.intent)
        self.assertIsNone(second.intent)
        self.assertEqual(second.divergence, DivergenceClass.PENDING_INFLIGHT)
        self.assertTrue(second.projection.pending)
        self.assertIsNotNone(bridge.inflight)

    def test_two_close_observes_while_open_emit_one_close(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        bridge, _, _ = _bridge()
        open_tick = bridge.observe(_qualify(), _open_quotes(), idle)
        opened = apply_events(idle, _happy_open(Clock(), open_tick.intent.intent_id))
        bridge.commit_open_context(
            opened, _context_for(open_tick.intent.intent_id, fill_spread_pp=1.0)
        )
        first = bridge.observe(_close_snaps(), _close_quotes(), opened)
        second = bridge.observe(_close_snaps(), _close_quotes(), opened)
        self.assertEqual(first.decision.action, "close")
        self.assertIsNotNone(first.intent)
        self.assertEqual(first.intent.action, IntentAction.CLOSE)
        self.assertIsNone(second.intent)
        self.assertNotEqual(second.decision.action, "close")
        self.assertFalse(second.projection.allow_close)
        self.assertTrue(second.projection.pending)
        self.assertEqual(second.divergence, DivergenceClass.PENDING_INFLIGHT)
        self.assertEqual(second.projection.trade_id, open_tick.intent.intent_id)
        self.assertEqual(opened.status, SpreadStatus.OPEN)
        self.assertIsNotNone(bridge.inflight)
        self.assertEqual(bridge.inflight.action, IntentAction.CLOSE)

    def test_reject_clears_latch_and_allows_retry(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        bridge, _, _ = _bridge()
        first = bridge.observe(_qualify(), _open_quotes(), idle)
        bridge.clear_inflight_on_reject(first.intent.intent_id)
        retry = bridge.observe(_qualify(), _open_quotes(), idle)
        self.assertIsNotNone(retry.intent)
        self.assertNotEqual(retry.intent.intent_id, first.intent.intent_id)
        self.assertEqual(retry.divergence, DivergenceClass.MATCH)

    def test_state_transition_consumes_latch(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        bridge, _, _ = _bridge()
        first = bridge.observe(_qualify(), _open_quotes(), idle)
        armed = apply_events(idle, [_arm(Clock(), first.intent.intent_id)])
        bridge.consume_inflight_on_transition(armed)
        self.assertIsNone(bridge.inflight)
        tick = bridge.observe(_qualify(), _open_quotes(), armed)
        self.assertEqual(tick.divergence, DivergenceClass.ACK_NOT_OPEN)


class FiniteDivergenceFixturesTests(unittest.TestCase):
    def test_each_non_match_is_exact(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        halt_clock = Clock()
        halt_id = "intent000000000000000000000012"
        halted = apply_events(
            idle,
            [
                *_dispatch(halt_clock, halt_id),
                _event(
                    halt_clock,
                    ExecutionEventType.FAULT,
                    intent_id=halt_id,
                    payload={"halt": True, "reason_code": "halt"},
                ),
            ],
        )
        self.assertEqual(halted.status, SpreadStatus.HALTED)
        bridge, _, _ = _bridge()
        self.assertEqual(
            bridge.observe(_qualify(), _open_quotes(), halted).divergence,
            DivergenceClass.ENGINE_GATE,
        )
        armed = apply_events(idle, [_arm(Clock(), "intent000000000000000000000013")])
        self.assertEqual(
            Gear22StrategyBridge(
                run_id=RUN_ID,
                clocks=BridgeClocks(monotonic_ns=FixedClocks().monotonic_ns, wall_ns=FixedClocks().wall_ns),
                ids=BridgeIds(new_intent_id=SeqIds().new_intent_id),
            ).observe(_qualify(), _open_quotes(), armed).divergence,
            DivergenceClass.ACK_NOT_OPEN,
        )
        tiny = {"KAITO": _books(okx_ask_sz=0.01, bybit_bid_sz=0.01)}
        size_tick = Gear22StrategyBridge(
            run_id=RUN_ID,
            clocks=BridgeClocks(monotonic_ns=FixedClocks().monotonic_ns, wall_ns=FixedClocks().wall_ns),
            ids=BridgeIds(new_intent_id=SeqIds().new_intent_id),
        ).observe(_qualify(), tiny, idle)
        self.assertEqual(size_tick.decision.reject_reason, "insufficient_size")
        self.assertEqual(size_tick.divergence, DivergenceClass.SIZE_GATE)
        param_bridge, _, _ = _bridge(config=BridgeConfig(run_id=RUN_ID, policy_params=PREVIOUS))
        param_tick = param_bridge.observe(_qualify(), _open_quotes(), idle)
        self.assertEqual(param_tick.divergence, DivergenceClass.PARAM_OVERRIDE)
        self.assertIsNone(param_tick.intent)
        self.assertIsNone(param_bridge.inflight)
        notional_bridge, _, _ = _bridge(
            config=BridgeConfig(run_id=RUN_ID, notional_usdt=Decimal("19"))
        )
        notional_tick = notional_bridge.observe(_qualify(), _open_quotes(), idle)
        self.assertEqual(notional_tick.divergence, DivergenceClass.NOTIONAL_POLICY)
        self.assertIsNone(notional_tick.intent)
        self.assertIsNone(notional_bridge.inflight)
        self.assertEqual(
            classify_divergence(DivergenceFacts(fill_model_diverged=True)),
            DivergenceClass.FILL_MODEL,
        )
        self.assertEqual(
            classify_divergence(DivergenceFacts(id_scheme_diverged=True)),
            DivergenceClass.ID_SCHEME,
        )
        self.assertEqual(
            classify_divergence(DivergenceFacts(close_while_not_open=True)),
            DivergenceClass.CLOSE_WHILE_NOT_OPEN,
        )
        opened = apply_events(idle, _happy_open(Clock(), "intent000000000000000000000014"))
        missing = Gear22StrategyBridge(
            run_id=RUN_ID,
            clocks=BridgeClocks(monotonic_ns=FixedClocks().monotonic_ns, wall_ns=FixedClocks().wall_ns),
            ids=BridgeIds(new_intent_id=SeqIds().new_intent_id),
        ).observe(_close_snaps(), _close_quotes(), opened)
        self.assertEqual(missing.divergence, DivergenceClass.MISSING_OPEN_CONTEXT)
        pending_bridge, _, _ = _bridge()
        pending_bridge.observe(_qualify(), _open_quotes(), idle)
        self.assertEqual(
            pending_bridge.observe(_qualify(), _open_quotes(), idle).divergence,
            DivergenceClass.PENDING_INFLIGHT,
        )
        reversed_bridge, _, _ = _bridge(
            config=BridgeConfig(run_id=RUN_ID, coin_order=tuple(reversed(GEAR22_HTML_TOP30)))
        )
        reversed_tick = reversed_bridge.observe(
            _qualify("KAITO") + _qualify("GIGGLE"), _open_quotes("KAITO", "GIGGLE"), idle
        )
        self.assertEqual(reversed_tick.divergence, DivergenceClass.COIN_ORDER)
        self.assertIsNone(reversed_tick.intent)
        self.assertIsNone(reversed_bridge.inflight)

    def test_observe_fill_model_flag_is_exact(self) -> None:
        bridge, _, _ = _bridge()
        tick = bridge.observe(
            _qualify(),
            _open_quotes(),
            initial_spread_state(run_id=RUN_ID),
            fill_model_diverged=True,
        )
        self.assertEqual(tick.divergence, DivergenceClass.FILL_MODEL)
        self.assertIsNone(tick.intent)
        self.assertIsNone(bridge.inflight)

    def test_hard_intent_gates_block_each_canonical_divergence(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        cases = (
            (
                BridgeConfig(run_id=RUN_ID, coin_order=tuple(reversed(GEAR22_HTML_TOP30))),
                False,
                DivergenceClass.COIN_ORDER,
            ),
            (
                BridgeConfig(run_id=RUN_ID, policy_params=PREVIOUS),
                False,
                DivergenceClass.PARAM_OVERRIDE,
            ),
            (
                BridgeConfig(run_id=RUN_ID, notional_usdt=Decimal("19")),
                False,
                DivergenceClass.NOTIONAL_POLICY,
            ),
            (BridgeConfig(run_id=RUN_ID), True, DivergenceClass.FILL_MODEL),
        )
        for config, fill_model, expected in cases:
            with self.subTest(expected=expected.value):
                bridge, _, _ = _bridge(config=config)
                tick = bridge.observe(
                    _qualify(),
                    _open_quotes(),
                    idle,
                    fill_model_diverged=fill_model,
                )
                self.assertEqual(tick.divergence, expected)
                self.assertIsNone(tick.intent)
                self.assertIsNone(bridge.inflight)

    def test_classify_divergence_rejects_multiple_flags(self) -> None:
        with self.assertRaises(StrategyBridgeError) as ctx:
            classify_divergence(
                DivergenceFacts(ack_not_open=True, pending_inflight=True)
            )
        self.assertEqual(ctx.exception.reason_code, "unclassified_non_match")

        bridge, _, _ = _bridge(
            config=BridgeConfig(
                run_id=RUN_ID,
                coin_order=tuple(reversed(GEAR22_HTML_TOP30)),
                notional_usdt=Decimal("19"),
            )
        )
        with self.assertRaises(StrategyBridgeError) as observe_ctx:
            bridge.observe(
                _qualify("KAITO") + _qualify("GIGGLE"),
                _open_quotes("KAITO", "GIGGLE"),
                initial_spread_state(run_id=RUN_ID),
            )
        self.assertEqual(
            observe_ctx.exception.reason_code, "unclassified_non_match"
        )
        self.assertIsNone(bridge.inflight)


class DeterminismAndIsolationTests(unittest.TestCase):
    def test_snapshot_hash_stable_across_replays(self) -> None:
        snaps = _qualify()
        quotes = _open_quotes()
        idle = initial_spread_state(run_id=RUN_ID)
        refs = []
        publics = []
        for _ in range(3):
            bridge, _, _ = _bridge(ids=SeqIds(), clocks=FixedClocks())
            tick = bridge.observe(snaps, quotes, idle)
            refs.append(tick.snapshot_ref)
            publics.append(tick.to_public_dict())
        self.assertEqual(refs[0], refs[1])
        self.assertEqual(refs[1], refs[2])
        self.assertTrue(refs[0].startswith("h"))
        self.assertEqual(len(refs[0]), 65)
        self.assertEqual(publics[0], publics[1])
        self.assertEqual(publics[0]["intent"]["signal_snapshot_ref"], refs[0])
        _assert_no_forbidden(publics[0])

    def test_bridge_source_has_no_journal_place_sentry_or_submit(self) -> None:
        path = Path(inspect.getsourcefile(Gear22StrategyBridge))
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_calls = {
            "submit",
            "place",
            "init_sentry",
            "capture_trade_event",
            "capture_exception",
            "append_rows",
            "execute_decision",
            "_execute_live_send",
        }
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
        self.assertFalse(forbidden_calls & called)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertTrue(all("private" not in name for name in imported))
        self.assertTrue(all("engine" not in name.split(".")[-1] for name in imported))
        self.assertTrue(all("sentry" not in name for name in imported))
        self.assertNotIn("app.bot.runtime", imported)

    def test_parity_schema_is_not_theta_trades(self) -> None:
        self.assertEqual(PARITY_SCHEMA_VERSION, "bbot.execution.parity.v1")
        self.assertEqual(CONTEXT_SCHEMA_VERSION, "bbot.execution.strategy_context.v1")
        self.assertEqual(BRIDGE_SCHEMA, "bbot.execution.strategy_bridge.v1")
        self.assertNotIn("theta_trade", PARITY_SCHEMA_VERSION)


class ContextRestoreTests(unittest.TestCase):
    def test_restore_matching_record_and_clear_on_flat(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        open_id = "intent000000000000000000000020"
        close_id = "intent000000000000000000000021"
        opened = apply_events(idle, _happy_open(Clock(), open_id))
        record = _context_for(open_id, fill_spread_pp=1.0)
        result = restore_context(opened, [record])
        self.assertTrue(result.restored)
        self.assertEqual(result.divergence, DivergenceClass.MATCH)
        self.assertEqual(result.context.trade_id, open_id)
        missing = restore_context(opened, [])
        self.assertFalse(missing.restored)
        self.assertEqual(missing.divergence, DivergenceClass.MISSING_OPEN_CONTEXT)
        wrong = _context_for(open_id, coin="HOME", fill_spread_pp=1.0)
        with self.assertRaises(StrategyBridgeError) as ctx:
            restore_context(opened, [wrong])
        self.assertEqual(ctx.exception.reason_code, "identity_mismatch")
        with self.assertRaises(StrategyBridgeError):
            commit_open_context(opened, wrong)
        flat = apply_events(idle, _flat_after_open(Clock(), open_id, close_id))
        self.assertEqual(flat.status, SpreadStatus.FLAT)
        bridge, _, _ = _bridge()
        bridge.restore_context(opened, [record])
        self.assertIsNotNone(bridge.context)
        tick = bridge.observe(_qualify(), _open_quotes(), flat)
        self.assertIsNone(bridge.context)
        self.assertEqual(tick.decision.action, "open")
        self.assertTrue(tick.projection.allow_open)

    def test_restore_non_open_is_fail_closed_and_preserves_instance_context(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        open_id = "intent000000000000000000000020"
        opened = apply_events(idle, _happy_open(Clock(), open_id))
        record = _context_for(open_id, fill_spread_pp=1.0)
        bridge, _, _ = _bridge()
        restored = bridge.restore_context(opened, [record])
        self.assertTrue(restored.restored)
        self.assertEqual(bridge.context.trade_id, open_id)
        with self.assertRaises(StrategyBridgeError) as module_ctx:
            restore_context(idle, [record])
        self.assertEqual(module_ctx.exception.reason_code, "invalid_spread_state")
        with self.assertRaises(StrategyBridgeError) as instance_ctx:
            bridge.restore_context(idle, [record])
        self.assertEqual(instance_ctx.exception.reason_code, "invalid_spread_state")
        self.assertIsNotNone(bridge.context)
        self.assertEqual(bridge.context.trade_id, open_id)
        armed = apply_events(idle, [_arm(Clock(), open_id)])
        with self.assertRaises(StrategyBridgeError) as armed_ctx:
            restore_context(armed, [record])
        self.assertEqual(armed_ctx.exception.reason_code, "invalid_spread_state")

    def test_failed_open_restore_does_not_erase_existing_context(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        open_id = "intent000000000000000000000022"
        opened = apply_events(idle, _happy_open(Clock(), open_id))
        record = _context_for(open_id, fill_spread_pp=1.0)
        bridge, _, _ = _bridge()
        bridge.restore_context(opened, [record])
        missing = bridge.restore_context(opened, [])
        self.assertFalse(missing.restored)
        self.assertEqual(missing.divergence, DivergenceClass.MISSING_OPEN_CONTEXT)
        self.assertIsNotNone(bridge.context)
        self.assertEqual(bridge.context.trade_id, open_id)

    def test_duplicate_matching_restore_records_must_agree(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        open_id = "intent000000000000000000000023"
        opened = apply_events(idle, _happy_open(Clock(), open_id))
        first = _context_for(open_id, fill_spread_pp=1.0)
        second = _context_for(open_id, fill_spread_pp=1.25)
        with self.assertRaises(StrategyBridgeError) as ctx:
            restore_context(opened, [first, second])
        self.assertEqual(ctx.exception.reason_code, "invalid_context")
        agreed = restore_context(opened, [first, _context_for(open_id, fill_spread_pp=1.0)])
        self.assertTrue(agreed.restored)
        self.assertEqual(agreed.context.fill_spread_pp, 1.0)

    def test_from_public_dict_wraps_missing_and_bad_fields(self) -> None:
        raw = _context_for("intent000000000000000000000024", fill_spread_pp=1.0).to_public_dict()
        missing = dict(raw)
        del missing["open_signal_ts_ms"]
        with self.assertRaises(StrategyBridgeError) as missing_ctx:
            OpenTradeContext.from_public_dict(missing)
        self.assertEqual(missing_ctx.exception.reason_code, "invalid_context")
        bad = dict(raw)
        bad["open_notional"] = 20.0
        with self.assertRaises(StrategyBridgeError) as bad_ctx:
            OpenTradeContext.from_public_dict(bad)
        self.assertEqual(bad_ctx.exception.reason_code, "invalid_context")
        with self.assertRaises(StrategyBridgeError) as empty_ctx:
            OpenTradeContext.from_public_dict({})
        self.assertEqual(empty_ctx.exception.reason_code, "invalid_context")

    def test_commit_requires_open_identity(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        with self.assertRaises(StrategyBridgeError) as ctx:
            commit_open_context(idle, _context_for("intent000000000000000000000030"))
        self.assertEqual(ctx.exception.reason_code, "ack_not_open")
        opened = apply_events(idle, _happy_open(Clock(), "intent000000000000000000000031"))
        mismatch = _context_for("intent000000000000000000000099", fill_spread_pp=1.0)
        with self.assertRaises(StrategyBridgeError) as ctx:
            commit_open_context(opened, mismatch)
        self.assertEqual(ctx.exception.reason_code, "identity_mismatch")

    def test_closing_retains_trade_id_but_does_not_close_again(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        open_id = "intent000000000000000000000040"
        close_id = "intent000000000000000000000041"
        clock = Clock()
        opened = apply_events(idle, _happy_open(clock, open_id))
        close_clock = Clock(mono=clock.mono)
        closing = apply_events(
            opened,
            [
                _arm(close_clock, close_id, action="close"),
                _sent(close_clock, Venue.OKX, OKX_LEG, close_id, reduce_only=True),
                _sent(close_clock, Venue.BYBIT, BYBIT_LEG, close_id, reduce_only=True),
            ],
        )
        self.assertEqual(closing.status, SpreadStatus.CLOSING)
        context = _context_for(open_id, fill_spread_pp=1.0)
        projection = project_slot(closing, context, None)
        self.assertEqual(projection.slot_kind, SlotKind.CLOSING_CORRELATION)
        self.assertEqual(projection.trade_id, open_id)
        self.assertTrue(projection.pending)
        self.assertFalse(projection.allow_close)
        self.assertIsNone(projection.position)
        bridge, _, _ = _bridge()
        bridge.restore_context(opened, [context])
        tick = bridge.observe(_close_snaps(), _close_quotes(), closing)
        self.assertIsNone(tick.intent)
        self.assertEqual(tick.divergence, DivergenceClass.ENGINE_GATE)


class PendingOpenContextTests(unittest.TestCase):
    def test_commit_proven_open_preserves_original_tick_fields(self) -> None:
        idle = initial_spread_state(run_id=RUN_ID)
        signal_ts_ms = 1_700_123_000_000
        snaps = [
            _snap("KAITO", "long", 0.60, p50_1m=0.80, floor=0.20, ts_ms=signal_ts_ms),
            _snap("KAITO", "short", 0.01, ts_ms=signal_ts_ms),
        ]
        signal_quotes = _open_quotes()
        proof_quotes = {"KAITO": _books(okx_ask=100.0, bybit_bid=103.0)}
        invented_ref = "h" + ("ab" * 32)
        bridge, _, clock = _bridge()
        open_tick = bridge.observe(snaps, signal_quotes, idle)
        self.assertIsNotNone(open_tick.intent)
        open_id = open_tick.intent.intent_id
        clock.wall = WALL_NS + 5_000_000_000
        armed = apply_events(idle, [_arm(Clock(), open_id)])
        with self.assertRaises(StrategyBridgeError) as armed_ctx:
            bridge.commit_proven_open(armed, proof_quotes)
        self.assertEqual(armed_ctx.exception.reason_code, "ack_not_open")
        self.assertIsNone(bridge.context)
        dispatch_clock = Clock()
        dispatching = apply_events(idle, _dispatch(dispatch_clock, open_id))
        with self.assertRaises(StrategyBridgeError) as dispatch_ctx:
            bridge.commit_proven_open(dispatching, proof_quotes)
        self.assertEqual(dispatch_ctx.exception.reason_code, "ack_not_open")
        self.assertIsNone(bridge.context)
        opened = apply_events(idle, _happy_open(Clock(), open_id))
        committed = bridge.commit_proven_open(opened, proof_quotes)
        expected_fill = fill_spread_from_quotes(proof_quotes, "KAITO", "long")
        signal_fill = fill_spread_from_quotes(signal_quotes, "KAITO", "long")
        self.assertIsNotNone(expected_fill)
        self.assertNotEqual(expected_fill, 1.0)
        self.assertNotEqual(expected_fill, signal_fill)
        self.assertEqual(committed.trade_id, open_id)
        self.assertEqual(committed.open_intent_id, open_id)
        self.assertEqual(committed.coin, open_tick.decision.base_coin)
        self.assertEqual(committed.side, open_tick.decision.side)
        self.assertEqual(committed.open_theta_1m, open_tick.decision.theta_1m)
        self.assertEqual(committed.open_signal_ts_ms, signal_ts_ms)
        self.assertEqual(committed.signal_snapshot_ref, open_tick.snapshot_ref)
        self.assertNotEqual(committed.signal_snapshot_ref, invented_ref)
        self.assertEqual(committed.signal_mono_ns, open_tick.intent.signal_mono_ns)
        self.assertEqual(committed.signal_wall_ns, open_tick.intent.signal_wall_ns)
        self.assertEqual(committed.open_notional, open_tick.intent.notional_usdt)
        self.assertEqual(committed.fill_spread_pp, expected_fill)
        self.assertEqual(committed.open_fill_ts_ms, (WALL_NS + 5_000_000_000) // 1_000_000)
        self.assertNotEqual(committed.open_fill_ts_ms, signal_ts_ms)
        self.assertIs(bridge.context, committed)

    def test_observe_rejects_run_id_mismatch(self) -> None:
        bridge, _, _ = _bridge()
        with self.assertRaises(StrategyBridgeError) as ctx:
            bridge.observe(
                _qualify(),
                _open_quotes(),
                initial_spread_state(run_id="run_other0000000000000000000000001"),
            )
        self.assertEqual(ctx.exception.reason_code, "invalid_spread_state")
        self.assertIsNone(bridge.inflight)
        self.assertIsNone(bridge.context)


class PublicExportTests(unittest.TestCase):
    def test_package_exports_bridge_api(self) -> None:
        import app.bot.execution as execution

        self.assertIs(execution.Gear22StrategyBridge, Gear22StrategyBridge)
        self.assertIs(execution.project_slot, project_slot)
        self.assertEqual(execution.INTENT_TTL_NS, INTENT_TTL_NS)
        self.assertIn("Gear22StrategyBridge", execution.__all__)


if __name__ == "__main__":
    unittest.main()

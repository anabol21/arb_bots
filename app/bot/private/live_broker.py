"""Live Broker mount: strategy filters stay; W6 leaves the send hot path.

``place`` keeps the same live_broker / StubBroker gates the manager already
uses: K_live=1, already-in-position, held_coin, open vs close, qty/lot.

Default send is ``ws_trivial_dual_leg`` (Contour B / bybit_ws queue→ws.send)
with W6 ``build_trade_place`` frames. Full W6 recover → approve → lease →
prepare_approved+journal/preflight is **not** on signal→ws.send.

Opt back into W6 (safety experiments only):

    BBOT_PRIVATE_SEND_PATH=w6
    BBOT_PRIVATE_W6=1

``BBOT_PRIVATE_W6=1`` alone does **not** switch the live manager.
The new default does **not** require ``BBOT_PRIVATE_W6=1``.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from app.bot.journal import JournalWriter
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.venue import live_orders_enabled, resolve_venue, send_allowed
from app.bot.private.ws_trivial_dual_leg import (
    SEND_PATH_TRIVIAL,
    SEND_PATH_W6,
    TrivialDualSender,
    TrivialSendError,
    TrivialSendResult,
    build_signed_place_text,
    resolve_live_send_path,
    send_signed_dual,
    w6_manager_opt_in,
    warm_trade_send_fn,
)
from app.bot.stub_broker import (
    InstrumentMeta,
    PendingIntent,
    PendingLeg,
    StubBroker,
    legs_for_spread_side,
    reverse_sides,
    signal_price_for_leg,
)


class LiveBrokerError(RuntimeError):
    """Live broker construction / send-path selection failure."""


def make_live_broker(
    *,
    data_root: Path,
    journal: JournalWriter,
    trade_lat_ms: int,
    notional_usdt: float,
    log: Callable[[str], None],
    env: Mapping[str, str],
    send_fn: Optional[Callable[[Any], None]] = None,
    inst_id_codes: Optional[Mapping[str, int]] = None,
    bybit_credentials: Optional[LiveCredentials] = None,
    okx_credentials: Optional[LiveCredentials] = None,
    sender: Optional[TrivialDualSender] = None,
) -> "LiveBroker":
    venue = resolve_venue(env)
    if venue != "live":
        raise LiveBrokerError(
            f"BBOT_BROKER=private_live requires VENUE=live, got {venue!r}"
        )
    if not live_orders_enabled(env):
        raise LiveBrokerError("BBOT_BROKER=private_live requires LIVE_ORDERS=1")
    if not send_allowed(env):
        raise LiveBrokerError("BBOT_BROKER=private_live requires send_allowed")
    path = resolve_live_send_path(env)
    log(
        f"broker_mount | kind=private_live | venue=live | send_path={path} | "
        f"w6_opt_in={w6_manager_opt_in(env)}"
    )
    return LiveBroker(
        data_root=data_root,
        journal=journal,
        trade_lat_ms=trade_lat_ms,
        notional_usdt=notional_usdt,
        log=log,
        env=dict(env),
        send_fn=send_fn,
        inst_id_codes=inst_id_codes,
        bybit_credentials=bybit_credentials,
        okx_credentials=okx_credentials,
        sender=sender,
    )


class LiveBroker(StubBroker):
    """Stub filters + trivial (default) or W6 (opt-in) dual-leg send."""

    def __init__(
        self,
        *,
        data_root: Path,
        journal: JournalWriter,
        trade_lat_ms: int = 100,
        notional_usdt: float = 100.0,
        log: Optional[Callable[[str], None]] = None,
        env: Optional[Mapping[str, str]] = None,
        send_fn: Optional[Callable[[Any], None]] = None,
        inst_id_codes: Optional[Mapping[str, int]] = None,
        bybit_credentials: Optional[LiveCredentials] = None,
        okx_credentials: Optional[LiveCredentials] = None,
        sender: Optional[TrivialDualSender] = None,
    ) -> None:
        super().__init__(
            data_root=data_root,
            journal=journal,
            trade_lat_ms=trade_lat_ms,
            notional_usdt=notional_usdt,
            log=log,
        )
        self.env = dict(env if env is not None else os.environ)
        self.send_path = resolve_live_send_path(self.env)
        self._send_fn = send_fn
        self._inst_id_codes = {str(k): int(v) for k, v in dict(inst_id_codes or {}).items()}
        self._bybit_credentials = bybit_credentials
        self._okx_credentials = okx_credentials
        self._owned_sender = sender is None
        self._sender = sender
        self.last_send_result: Optional[TrivialSendResult] = None
        self.last_send_path: Optional[str] = None

    def _get_sender(self) -> TrivialDualSender:
        if self._sender is None:
            send_fn = self._send_fn
            if send_fn is None:
                session = self._warm_session_or_none()
                if session is not None:
                    send_fn = warm_trade_send_fn(session)
            self._sender = TrivialDualSender(send_fn=send_fn)
            self._owned_sender = True
        return self._sender

    def close(self) -> None:
        if self._owned_sender and self._sender is not None:
            self._sender.close()
            self._sender = None

    def _warm_session_or_none(self) -> Any:
        try:
            from app.bot.private.ws_warm_session import get_process_warm_session
        except Exception:  # noqa: BLE001
            return None
        return get_process_warm_session()

    def _bybit_creds(self) -> Optional[LiveCredentials]:
        if self._bybit_credentials is not None:
            return self._bybit_credentials
        session = self._warm_session_or_none()
        if session is not None:
            return getattr(session, "bybit_credentials", None)
        return None

    def on_valid_tick(self, **kwargs: Any) -> bool:
        """Live fills are venue-observed, not Trade_Lat stub fills.

        Pending is cleared in ``place`` after both ws.send. A later tick
        must not invent a second fill.
        """
        return False

    def place(
        self,
        *,
        spread_side: str,
        base_coin: str,
        signal_ts_ms: int,
        okx_book: dict[str, Any],
        bybit_book: dict[str, Any],
        meta: InstrumentMeta,
        close_of: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> Optional[str]:
        """Strategy filters, then default trivial dual send (or W6 opt-in)."""
        if self.pending is not None:
            return "k_live_blocked"
        if spread_side in ("open_long", "open_short") and self.position is not None:
            return "already_in_position"
        if spread_side == "close":
            if self.position is None:
                return "flat_cannot_close"
            if (
                self.held_coin is not None
                and str(base_coin).upper() != self.held_coin.upper()
            ):
                return "not_held_coin"
            open_side = close_of or self.position
            okx_side, bybit_side = reverse_sides(*legs_for_spread_side(open_side))
            journal_side = "close"
            open_spread_side = open_side
            phase = "close"
            reduce_only = True
        else:
            okx_side, bybit_side = legs_for_spread_side(spread_side)
            journal_side = spread_side
            open_spread_side = None
            phase = "open"
            reduce_only = False

        notional = self.notional_usdt
        plan, abort = self._qty_plan(
            meta=meta,
            okx_side=okx_side,
            bybit_side=bybit_side,
            okx_book=okx_book,
            bybit_book=bybit_book,
            notional=notional,
        )
        place_ts = max(int(time.time() * 1000), int(signal_ts_ms))
        if plan is None:
            return abort or "qty_abort"

        okx_qty, bybit_qty, okx_px, bybit_px = plan
        intent_id = str(uuid.uuid4())
        pending = PendingIntent(
            intent_id=intent_id,
            base_coin=base_coin.upper(),
            spread_side=journal_side,
            signal_ts_ms=int(signal_ts_ms),
            place_ts_ms=place_ts,
            ack_ts_ms=place_ts,
            trade_lat_ms=self.trade_lat_ms,
            notional=notional,
            okx_symbol=meta.okx_symbol,
            bybit_symbol=meta.bybit_symbol,
            legs=[
                PendingLeg("okx", okx_side, okx_px, okx_qty, notional),
                PendingLeg("bybit", bybit_side, bybit_px, bybit_qty, notional),
            ],
            open_spread_side=open_spread_side,
            status="placed",
            extra=dict(extra or {}),
        )

        send_abort = self.default_live_send_pair(
            pending=pending,
            phase=phase,
            reduce_only=reduce_only,
        )
        if send_abort:
            return send_abort

        if journal_side in ("open_long", "open_short"):
            self.position = journal_side
            self.held_coin = pending.base_coin.upper()
        elif journal_side == "close":
            self.position = None
            self.held_coin = None
        self.pending = None
        self._persist_pending()
        self._log(
            f"live_broker | sent | intent_id={intent_id} | side={journal_side} | "
            f"coin={base_coin} | send_path={self.last_send_path} | k_live=1"
        )
        return None

    def default_live_send_pair(
        self,
        *,
        pending: PendingIntent,
        phase: str,
        reduce_only: bool,
    ) -> Optional[str]:
        """Live-manager send entry. Default = trivial; W6 is explicit opt-in."""
        if w6_manager_opt_in(self.env):
            self.last_send_path = SEND_PATH_W6
            return self._send_via_w6(pending=pending, phase=phase)
        self.last_send_path = SEND_PATH_TRIVIAL
        return self._send_via_trivial(
            pending=pending, phase=phase, reduce_only=reduce_only
        )

    def _send_via_trivial(
        self,
        *,
        pending: PendingIntent,
        phase: str,
        reduce_only: bool,
    ) -> Optional[str]:
        okx_leg = next(leg for leg in pending.legs if leg.exchange == "okx")
        bybit_leg = next(leg for leg in pending.legs if leg.exchange == "bybit")
        inst_code = self._inst_id_codes.get(pending.okx_symbol)
        if inst_code is None:
            return "okx_inst_id_code_missing"
        creds = self._bybit_creds()
        if creds is None:
            return "bybit_credentials_missing"
        dual_id = pending.intent_id.replace("-", "")[:32]
        try:
            bybit_text, bybit_req, _ = build_signed_place_text(
                venue="bybit",
                symbol=pending.bybit_symbol,
                side=bybit_leg.leg_side,
                qty=str(bybit_leg.qty),
                credentials=creds,
                reduce_only=reduce_only,
                order_attempt_id=f"b{dual_id}"[:36],
                dual_leg_id=dual_id,
            )
            okx_text, okx_req, _ = build_signed_place_text(
                venue="okx",
                symbol=pending.okx_symbol,
                side=okx_leg.leg_side,
                qty=str(okx_leg.qty),
                credentials=self._okx_credentials,
                reduce_only=reduce_only,
                inst_id_code=int(inst_code),
                order_attempt_id=f"o{dual_id}"[:32],
                dual_leg_id=dual_id,
            )
        except (TrivialSendError, ValueError, TypeError, KeyError) as exc:
            return f"frame_build_failed:{type(exc).__name__}"

        place_io = None
        session = self._warm_session_or_none()
        if session is not None:
            if not session.is_ready():
                return "warm_session_not_ready"
            place_io = session.place_io_section()

        try:
            result = send_signed_dual(
                sender=self._get_sender(),
                bybit_text=bybit_text,
                okx_text=okx_text,
                bybit_req_id=bybit_req,
                okx_req_id=okx_req,
                phase=phase,
                place_io=place_io,
            )
        except (TrivialSendError, RuntimeError, TimeoutError) as exc:
            return f"trivial_send_failed:{type(exc).__name__}"
        self.last_send_result = result
        if result.error:
            return f"trivial_send_failed:{result.error}"
        if result.first_sent_ns is None or result.second_sent_ns is None:
            return "trivial_send_incomplete"
        return None

    def _send_via_w6(
        self,
        *,
        pending: PendingIntent,
        phase: str,
    ) -> Optional[str]:
        """Explicit opt-in only. Imports W6 inside this branch.

        W6 remains TRUMP-profile + recover/approval/lease. Not the default.
        """
        from app.bot.private.ws_gates import assert_ws_w6_send_gates
        from app.bot.private.ws_w6_dual_leg import (
            W6_LEGS,
            open_w6_production_bindings,
            run_w6_dual_leg,
        )

        try:
            assert_ws_w6_send_gates(self.env)
        except Exception as exc:  # noqa: BLE001
            return f"w6_gate:{type(exc).__name__}"
        bybit_sym = str(W6_LEGS["bybit"]["symbol"])
        okx_sym = str(W6_LEGS["okx"]["symbol"])
        if pending.bybit_symbol != bybit_sym or pending.okx_symbol != okx_sym:
            return "w6_path_trump_only"
        try:
            active = open_w6_production_bindings(env=self.env)
            report = run_w6_dual_leg(
                n=1,
                env=self.env,
                metadata_provider=active.metadata_provider,
                position_mode_provider=active.position_mode_provider,
                baseline=active.baseline,
                bybit_credentials=active.bybit_credentials,
                okx_credentials=active.okx_credentials,
                load_secrets=False,
                issue_approval=True,
                rest_order_recon=active.rest_order_recon,
                parallel_open=True,
                warm_session=self._warm_session_or_none(),
            )
        except Exception as exc:  # noqa: BLE001
            return f"w6_send_failed:{type(exc).__name__}"
        if report.status != "ok":
            return f"w6_send_failed:{report.status}"
        _ = phase
        return None

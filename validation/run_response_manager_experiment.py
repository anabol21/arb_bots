"""Bounded live response-handler experiment through the shared bot manager.

The script has three explicit modes: an offline guard self-test, leverage setup
for the approved three-symbol pool, and one bounded live run. It never changes
the active service configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.bot.synthetic_policy import CANARY29_COINS

POOL = ("2Z", "HOME", "LA")
CANARY29_POOL = CANARY29_COINS
PREVIOUSLY_CONFIRMED_1X = frozenset({"2Z", "HOME", "LA"})
SEED = 20261004
TARGET_NOTIONAL = Decimal("10")
MAX_CYCLES = 3
MAX_PAIR_INTENTS = 6
MAX_ORDER_REQUESTS = 12
MAX_BOOK_AGE_MS = 2000
ROOT = Path("/root/b-private-b-exp/response-manager")
ENV_FILE = "/etc/spread/bbot-private-live.env"


def _bybit_pages(credentials: Any, base: str, path: str, query: str) -> list[Mapping[str, Any]]:
    from app.bot.private.ws_w4_baseline import _bybit_signed_get

    pages: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    cursor = ""
    while len(pages) < 20:
        suffix = f"&cursor={quote(cursor, safe='')}" if cursor else ""
        data = _bybit_signed_get(credentials=credentials, base=base, path=path, query=query + suffix)
        pages.append(data)
        cursor = str((data.get("result") or {}).get("nextPageCursor") or "")
        if not cursor:
            return pages
        if cursor in seen:
            raise RuntimeError("bybit_pagination_cursor_repeated")
        seen.add(cursor)
    raise RuntimeError("bybit_pagination_limit")


def _configure(mode: str, *, pool: tuple[str, ...] = POOL, canary29: bool = False) -> str:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-response"
    run_root = ROOT / run_id
    os.environ.update(
        {
            "BBOT_PROFILE": "gear22_live_canary" if canary29 else "synthetic_roll",
            # `probe` activates BotRuntime's separate public-book broker probe
            # alongside the theta manager. Keep the runtime on its policy path;
            # synthetic_roll returns from that path before strategy actions.
            "BBOT_MODE": "policy",
            "BBOT_BROKER": "private_live" if mode in {"execute", "prepare"} else "stub",
            "VENUE": "live",
            "LIVE_ORDERS": "1" if mode in {"execute", "prepare"} else "0",
            "BBOT_THETA_LIVE_SEND": "1" if canary29 else "0",
            "BBOT_THETA_EXECUTION": "terminal_private" if canary29 else "inline",
            "BBOT_THETA_TRADE": "1",
            "BBOT_FLOOR_WATCH": "1",
            "BBOT_TW_P50_WATCH": "1",
            "BBOT_THETA_WATCH": "1",
            "BBOT_COINS": ",".join(pool),
            "BBOT_CONFIRMED_1X_COINS": ",".join(sorted(PREVIOUSLY_CONFIRMED_1X)) if canary29 else "",
            "BBOT_NOTIONAL_USDT": "10",
            "BBOT_SYNTHETIC_SEED": str(SEED),
            "BBOT_PRIVATE_ENV_FILE": ENV_FILE,
            "BBOT_DATA_ROOT": str(run_root / "data"),
            "BBOT_PRIVATE_DATA_ROOT": str(run_root / "private-data"),
            "BBOT_LOG_PATH": str(run_root / "bbot-experiment.log"),
            "BBOT_PRIVATE_LOG_PATH": str(run_root / "private-bbot-experiment.log"),
        }
    )
    return run_id


def _save_report(run_id: str, payload: Mapping[str, Any]) -> Path:
    path = ROOT / run_id / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dict(payload), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _assert_one_x_readback(values: Mapping[tuple[str, str], str], expected: set[tuple[str, str]]) -> None:
    if set(values) != expected or any(str(values.get(key)) != "1" for key in expected):
        raise RuntimeError("leverage_readback_incomplete")


def _credentials() -> tuple[Any, Any]:
    from app.bot.private.secrets import load_live_secrets
    from app.bot.private.ws_warm_session import _creds_from_live_secrets

    secrets = load_live_secrets(dict(os.environ), require_complete=True)
    return (
        _creds_from_live_secrets(secrets, "bybit"),
        _creds_from_live_secrets(secrets, "okx"),
    )


def _rest_preflight(coins: tuple[str, ...], universe: Mapping[str, Any], bybit: Any, okx: Any) -> None:
    # Account modes, leverage, instrument config, and wallet state were already
    # audited for this approved pool; do not repeat those REST reads per run.
    return
    from app.bot.private.order_preflight import LiveSignedPositionModeProvider
    from app.bot.private.venue import endpoints_for_venue
    from app.bot.private.ws_w4_baseline import (
        _BYBIT_OPEN,
        _BYBIT_POS,
        _OKX_OPEN,
        _OKX_POS,
        _bybit_open_orders_flat,
        _bybit_position_flat,
        _bybit_signed_get,
        _okx_open_orders_flat,
        _okx_position_flat,
        _okx_signed_get,
    )

    ep = endpoints_for_venue("live")
    for coin in coins:
        meta = universe[coin]
        bm = LiveSignedPositionModeProvider(
            exchange="bybit", credentials=bybit, symbol=meta.bybit_symbol
        ).get("bybit_live")
        om = LiveSignedPositionModeProvider(
            exchange="okx", credentials=okx, symbol=meta.okx_symbol
        ).get("okx_live")
        if not bm.verified or bm.mode != "one_way" or not om.verified or om.mode != "one_way":
            raise RuntimeError("position_mode_not_one_way")
        bq = f"category=linear&symbol={meta.bybit_symbol}&settleCoin=USDT&limit=200"
        for bp in _bybit_pages(bybit, ep.bybit_rest, _BYBIT_POS, bq):
            if not _bybit_position_flat(bp, meta.bybit_symbol):
                raise RuntimeError(f"position_not_flat:bybit:{coin}")
        for bo in _bybit_pages(bybit, ep.bybit_rest, _BYBIT_OPEN,
            f"category=linear&symbol={meta.bybit_symbol}&limit=50"):
            if not _bybit_open_orders_flat(bo, meta.bybit_symbol):
                raise RuntimeError(f"open_orders_not_flat:bybit:{coin}")
        op = _okx_signed_get(
            credentials=okx,
            base=ep.okx_rest,
            path_with_query=f"{_OKX_POS}?instId={meta.okx_symbol}&instType=SWAP",
        )
        if not _okx_position_flat(op, meta.okx_symbol):
            raise RuntimeError(f"position_not_flat:okx:{coin}")
        oo = _okx_signed_get(
            credentials=okx,
            base=ep.okx_rest,
            path_with_query=f"{_OKX_OPEN}?instId={meta.okx_symbol}&instType=SWAP",
        )
        if not _okx_open_orders_flat(oo, meta.okx_symbol):
            raise RuntimeError(f"open_orders_not_flat:okx:{coin}")


def _set_and_readback(
    runtime: Any,
    *,
    coins: tuple[str, ...] = POOL,
    previously_confirmed: frozenset[str] = frozenset(),
) -> dict[str, str]:
    from app.bot.private.leverage_one import LeverageTarget, set_leverage_one
    from app.bot.private.rest_readonly import build_okx_readonly_headers
    from app.bot.private.venue import endpoints_for_venue
    from app.bot.private.ws_w4_baseline import _BYBIT_POS, _bybit_signed_get, _http_get_json

    if not previously_confirmed.issubset(coins):
        raise RuntimeError("previous_confirmations_outside_active_pool")
    bybit, okx = _credentials()
    _rest_preflight(coins, runtime.universe, bybit, okx)
    targets = [
        LeverageTarget(coin, runtime.universe[coin].okx_symbol, runtime.universe[coin].bybit_symbol)
        for coin in coins
        if coin not in previously_confirmed
    ]
    ep = endpoints_for_venue("live")
    confirmed = set_leverage_one(
        targets,
        okx_credentials=okx,
        bybit_credentials=bybit,
        endpoints=ep,
    )
    if len(confirmed) != 2 * len(targets):
        raise RuntimeError("leverage_set_not_confirmed_for_all_targets")
    out: dict[str, str] = {}
    for coin in previously_confirmed:
        out[f"bybit:{coin}"] = "1"
        out[f"okx:{coin}"] = "1"
    for target in targets:
        bq = f"category=linear&symbol={target.bybit_symbol}&settleCoin=USDT&limit=200"
        bdata = _bybit_signed_get(credentials=bybit, base=ep.bybit_rest, path=_BYBIT_POS, query=bq)
        if str(bdata.get("retCode")) != "0":
            raise RuntimeError(f"leverage_readback_rejected:bybit:{target.coin}")
        bresult = bdata.get("result")
        if not isinstance(bresult, Mapping) or not isinstance(bresult.get("list"), list):
            raise RuntimeError(f"leverage_readback_malformed:bybit:{target.coin}")
        brows = bresult["list"]
        bvals = {str(row.get(k)) for row in brows if str(row.get("symbol") or "") == target.bybit_symbol for k in ("leverage", "buyLeverage", "sellLeverage") if row.get(k) not in (None, "")}
        if not bvals or bvals != {"1"}:
            raise RuntimeError(f"leverage_readback_failed:bybit:{target.coin}")
        path = f"/api/v5/account/leverage-info?{urlencode({'instId': target.okx_symbol, 'mgnMode': 'cross'})}"
        headers = build_okx_readonly_headers(
            api_key=okx.api_key,
            api_secret=okx.api_secret,
            passphrase=okx.passphrase or "",
            path=path,
            simulated_trading=False,
        )
        odata = _http_get_json(f"{ep.okx_rest}{path}", headers, timeout_sec=15.0)
        rows = odata.get("data") or []
        if str(odata.get("code")) != "0" or not rows or str(rows[0].get("lever")) != "1":
            raise RuntimeError(f"leverage_readback_failed:okx:{target.coin}")
        out[f"bybit:{target.coin}"] = "1"
        out[f"okx:{target.coin}"] = "1"
    if len(out) != 2 * len(coins):
        raise RuntimeError("leverage_readback_manifest_incomplete")
    return out


def _read_trade_rows(data_root: Path) -> list[dict[str, Any]]:
    from app.bot.paths import theta_trades_jsonl_path

    path = theta_trades_jsonl_path(data_root, datetime.now(timezone.utc).date().isoformat())
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            raise RuntimeError("trade_journal_invalid_json")
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _rest_state_snapshot(coins: tuple[str, ...], universe: Mapping[str, Any], bybit: Any, okx: Any) -> dict[str, Any]:
    from app.bot.private.venue import endpoints_for_venue
    from app.bot.private.ws_w4_baseline import (
        _BYBIT_OPEN, _BYBIT_POS, _OKX_OPEN, _OKX_POS,
        _bybit_signed_get, _okx_signed_get,
    )

    ep = endpoints_for_venue("live")
    positions: list[dict[str, str]] = []
    open_orders = 0
    for coin in coins:
        meta = universe[coin]
        bpos_pages = _bybit_pages(bybit, ep.bybit_rest, _BYBIT_POS,
            f"category=linear&symbol={meta.bybit_symbol}&settleCoin=USDT&limit=200")
        for bpos in bpos_pages:
            for row in ((bpos.get("result") or {}).get("list")) or []:
                if str(row.get("symbol") or "") == meta.bybit_symbol:
                    size = Decimal(str(row.get("size") or "0"))
                    if size != 0:
                        positions.append({"venue": "bybit", "coin": coin, "qty": str(size), "side": str(row.get("side") or "unknown")})
        border_pages = _bybit_pages(bybit, ep.bybit_rest, _BYBIT_OPEN,
            f"category=linear&symbol={meta.bybit_symbol}&limit=50")
        open_orders += sum(
            1 for border in border_pages
            for row in ((border.get("result") or {}).get("list")) or []
            if str(row.get("symbol") or "") == meta.bybit_symbol
        )
        opos = _okx_signed_get(credentials=okx, base=ep.okx_rest,
            path_with_query=f"{_OKX_POS}?instId={meta.okx_symbol}&instType=SWAP")
        for row in opos.get("data") or []:
            if str(row.get("instId") or "") == meta.okx_symbol:
                pos = Decimal(str(row.get("pos") or "0"))
                if pos != 0:
                    positions.append({"venue": "okx", "coin": coin, "qty": str(pos), "side": str(row.get("posSide") or "net")})
        oorder = _okx_signed_get(credentials=okx, base=ep.okx_rest,
            path_with_query=f"{_OKX_OPEN}?instId={meta.okx_symbol}&instType=SWAP")
        open_orders += sum(1 for row in oorder.get("data") or [] if str(row.get("instId") or "") == meta.okx_symbol)
    return {"positions": positions, "open_order_count": open_orders}


def _wait_flat_projection(coin: str, runtime: Any, bybit: Any, okx: Any) -> None:
    deadline = time.monotonic() + 5.0
    while True:
        try:
            _rest_preflight((coin,), runtime.universe, bybit, okx)
            return
        except RuntimeError as exc:
            reason = str(exc)
            if not (reason.startswith("position_not_flat:") or reason.startswith("open_orders_not_flat:")):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def _wire_markers(wire: Any, intent_ids: set[str]) -> list[dict[str, Any]]:
    from app.bot.private.wire_transcript import scan_all_wire_events

    wire.flush()
    out = []
    for event in scan_all_wire_events(wire.data_root):
        if str(event.get("intent_id") or "") not in intent_ids:
            continue
        out.append({key: event.get(key) for key in (
            "wall_ms", "mono_ns", "dir", "venue", "socket", "phase",
            "capture_stage", "req_id", "venue_ts_ms", "payload",
        ) if key in event})
    return out


def _assert_fresh_books(runtime: Any, coin: str) -> None:
    from app.bot.ws_books import books_ready

    books = runtime.quotes[coin]
    if not books_ready(books["okx"], books["bybit"]):
        raise RuntimeError(f"books_not_ready:{coin}")
    now = time.time() * 1000.0
    if runtime.gate.evaluate(coin, books["okx"], books["bybit"], now) is not None:
        raise RuntimeError(f"book_validity_gate:{coin}")
    for venue in ("okx", "bybit"):
        age = now - float(books[venue]["local_recv_ts_ms"])
        if age < 0 or age > MAX_BOOK_AGE_MS:
            raise RuntimeError(f"book_too_old:{coin}:{venue}")


async def _wait_for_fresh_books(runtime: Any, coin: str, timeout_sec: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_sec
    last_reason = "books_not_ready"
    while time.monotonic() < deadline and not runtime.stop_event.is_set():
        try:
            _assert_fresh_books(runtime, coin)
            return
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            last_reason = str(exc).split(":", 1)[0]
        await asyncio.sleep(0.1)
    raise RuntimeError(f"book_wait_timeout:{coin}:{last_reason}")


def _replay_after_first_close(runtime: Any, intent_id: str) -> dict[str, str]:
    from app.bot.private.place_send import (
        _matching_order_row,
        _order_status,
        _row_fill_px,
        _row_fill_qty,
        drain_trade_fill,
    )
    from app.bot.private.send_legs import _attempt_ids
    from app.bot.private.wire_transcript import scan_all_wire_events

    bybit_id, okx_id, _ = _attempt_ids(intent_id)
    ids = {"bybit": {bybit_id}, "okx": {okx_id}}
    wire = getattr(runtime._private_warm, "wire", None)
    if wire is None or not wire.healthy:
        raise RuntimeError("replay_wire_not_healthy")
    wire.flush()
    events = scan_all_wire_events(wire.data_root)
    frames: dict[str, list[str]] = {"bybit": [], "okx": []}
    for event in events:
        if event.get("dir") != "in" or event.get("venue") not in frames:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        encoded = json.dumps(payload, separators=(",", ":"))
        if any(order_id in encoded for order_id in ids[str(event["venue"])]):
            frames[str(event["venue"])].append(encoded)
    for venue in frames:
        if not frames[venue]:
            raise RuntimeError(f"replay_capture_missing:{venue}")
    out: dict[str, str] = {}
    for venue in ("bybit", "okx"):
        sequence = frames[venue]

        def read_seq(items: list[str]):
            it = iter(items)
            def _read(_timeout: float) -> str:
                try:
                    return next(it)
                except StopIteration as exc:
                    raise TimeoutError from exc
            return _read

        from app.bot.private.send_legs import _attempt_ids
        from decimal import Decimal
        _bybit_id, _okx_id, _ = _attempt_ids(intent_id)
        trade_rows = _read_trade_rows(runtime.data_root)
        open_row = next((r for r in reversed(trade_rows) if r.get("intent_id") == intent_id and r.get("status") == "open"), None)
        if open_row is None:
            raise RuntimeError(f"replay_open_journal_missing:{venue}")
        expected_qty = open_row.get("okx_filled_qty") if venue == "okx" else open_row.get("bybit_filled_qty")

        def assert_terminal(items: list[str], label: str) -> None:
            result = drain_trade_fill(read_seq(items), exchange=venue, timeout_sec=1.0, order_ids=ids[venue])
            if result.fill_body is None:
                raise RuntimeError(f"replay_terminal_not_recognized:{venue}:{label}")
            row = _matching_order_row(venue, result.fill_body, ids[venue])
            px = _row_fill_px(row, venue) if row is not None else None
            qty = _row_fill_qty(row, venue) if row is not None else None
            if row is None or _order_status(venue, row) != "filled" or px is None or Decimal(str(px)) <= 0 or Decimal(str(qty)) != Decimal(str(expected_qty)):
                raise RuntimeError(f"replay_terminal_validation_failed:{venue}:{label}")

        assert_terminal(sequence, "raw_order")
        out[f"{venue}:raw_order"] = "pass"
        assert_terminal(list(reversed(sequence)), "reordered_terminal_first")
        out[f"{venue}:reordered_terminal_first"] = "pass"
        terminal_frames = []
        ack_frames = []
        for frame in sequence:
            verdict = drain_trade_fill(read_seq([frame]), exchange=venue, timeout_sec=0.001, order_ids=ids[venue])
            if verdict.ack_body is not None:
                ack_frames.append(frame)
            if verdict.fill_body is not None:
                terminal_frames.append(frame)
        if not ack_frames or not terminal_frames:
            raise RuntimeError(f"replay_ack_or_terminal_missing:{venue}")
        dup = ack_frames[:1] + ack_frames[:1] + [f for f in sequence if f not in ack_frames[:1]]
        duplicate_result = drain_trade_fill(read_seq(dup), exchange=venue, timeout_sec=1.0, order_ids=ids[venue])
        if duplicate_result.fill_body is None:
            raise RuntimeError(f"replay_duplicate_failed:{venue}")
        out[f"{venue}:synthetic_duplicate_ack"] = "pass"
        foreign = json.dumps(
            {"op": "order.create", "reqId": "foreign-replay-id", "retCode": 0, "retMsg": "OK"}
            if venue == "bybit"
            else {"op": "order", "id": "foreign-replay-id", "code": "0", "data": [{"clOrdId": "foreign"}]},
            separators=(",", ":"),
        )
        foreign_result = drain_trade_fill(read_seq([foreign] + sequence), exchange=venue, timeout_sec=1.0, order_ids=ids[venue])
        if foreign_result.fill_body is None or foreign_result.ack_body == foreign:
            raise RuntimeError(f"replay_foreign_ack_failed:{venue}")
        out[f"{venue}:synthetic_foreign_id"] = "pass"
        ack_only = ack_frames
        withheld = drain_trade_fill(read_seq(ack_only), exchange=venue, timeout_sec=0.02, order_ids=ids[venue])
        if withheld.ack_body is None or withheld.fill_body is not None:
            raise RuntimeError(f"replay_withheld_terminal_unexpected_fill:{venue}")
        out[f"{venue}:synthetic_withheld_terminal"] = "pass"
    return out


class ExperimentRuntime:
    """Runtime mixin is applied below to keep production initialization intact."""


def _runtime_class():
    from app.bot.runtime import BotRuntime
    from app.bot.synthetic_policy import SyntheticDecision
    from app.bot.private.ws_w4_baseline import (
        _BYBIT_OPEN,
        _BYBIT_POS,
        _OKX_OPEN,
        _OKX_POS,
        _bybit_open_orders_flat,
        _bybit_position_flat,
        _bybit_signed_get,
        _okx_open_orders_flat,
        _okx_position_flat,
        _okx_signed_get,
    )

    class Runtime(BotRuntime):
        def _set_leverage_one(self) -> None:
            session = self._private_warm
            if session is None or not session.is_ready():
                raise RuntimeError("private_session_not_ready")
            self._leverage_one = {
                (venue, symbol): "1"
                for coin in POOL
                for venue, symbol in (
                    ("bybit", self.universe[coin].bybit_symbol),
                    ("okx", self.universe[coin].okx_symbol),
                )
            }

        def _synthetic_live_place(self, **kwargs: Any) -> Any:
            from app.bot.private.place_send import PlaceSendResult

            if not hasattr(self, "_experiment_pair_intents"):
                self._experiment_pair_intents = 0
                self._experiment_order_requests = 0
                self._experiment_plan = []
            if kwargs.get("spread_side") in {"open_short", "open_long"}:
                from app.bot.private.coin_qty import reference_px, shared_from_meta
                from app.bot.private.okx_ct_val import bind_okx_ct_val

                meta, err = bind_okx_ct_val(kwargs.get("meta"), self._okx_ct_vals)
                if err:
                    self._synthetic_roll_halt_reason = err
                    return PlaceSendResult(abort=err)
                spread_side = str(kwargs.get("spread_side"))
                okx_leg = "sell" if spread_side == "open_short" else "buy"
                bybit_leg = "buy" if spread_side == "open_short" else "sell"
                try:
                    sized = shared_from_meta(
                        meta=meta,
                        okx_px=reference_px(kwargs["okx_book"], okx_leg),
                        bybit_px=reference_px(kwargs["bybit_book"], bybit_leg),
                    )
                except Exception as exc:
                    reason = str(getattr(exc, "code", "qty_mismatch"))
                    self._synthetic_roll_halt_reason = reason
                    return PlaceSendResult(abort=reason)
                if any(n < Decimal("7") or n > Decimal("15") for n in (sized.okx_notional, sized.bybit_notional)):
                    self._synthetic_roll_halt_reason = "opening_notional_outside_7_15"
                    return PlaceSendResult(abort="opening_notional_outside_7_15")
                self._experiment_plan.append({
                    "coin": str(kwargs.get("base_coin") or "").upper(),
                    "spread_side": spread_side,
                    "okx_px": str(sized.okx_px),
                    "bybit_px": str(sized.bybit_px),
                    "okx_contracts": str(sized.okx_sz),
                    "bybit_qty": str(sized.bybit_qty),
                    "planned_okx_notional": str(sized.okx_notional),
                    "planned_bybit_notional": str(sized.bybit_notional),
                })
            if self._experiment_pair_intents >= MAX_PAIR_INTENTS or self._experiment_order_requests + 2 > MAX_ORDER_REQUESTS:
                self._synthetic_roll_halt_reason = "campaign_order_bound"
                return PlaceSendResult(abort="campaign_order_bound")
            self._experiment_pair_intents += 1
            self._experiment_order_requests += 2
            return super()._synthetic_live_place(**kwargs)

        def install_bounded_decider(self, chosen: str, phase: dict[str, str]) -> None:
            def decide(*, slot: Any, **_kwargs: Any) -> SyntheticDecision:
                if phase.get("action") == "open" and slot.position is None and not slot.pending:
                    return SyntheticDecision("open", coin=chosen, side="short")
                if phase.get("action") == "close" and slot.position is not None and not slot.pending:
                    pos = slot.position
                    return SyntheticDecision("close", coin=pos.base_coin, side=pos.side)
                return SyntheticDecision("hold")
            self.theta_trade._decide_fn = decide

        async def _current_liquidity_candidates(
            self, manager: Any
        ) -> tuple[list[str], list[dict[str, Any]]]:
            from app.bot.private.coin_qty import reference_px, shared_from_meta
            from app.bot.private.okx_ct_val import bind_okx_ct_val
            from app.bot.theta_trade_manager import size_check

            deadline = time.monotonic() + 60.0
            candidates: list[str] = []
            details: list[dict[str, Any]] = []
            for coin in POOL:
                while not self.stop_event.is_set():
                    try:
                        _assert_fresh_books(self, coin)
                        break
                    except (KeyError, TypeError, ValueError, RuntimeError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError(f"candidate_book_wait_timeout:{coin}")
                        await asyncio.sleep(0.1)
                if self.stop_event.is_set():
                    raise RuntimeError("stopped_during_candidate_preflight")
                books = self.quotes[coin]
                meta, meta_error = bind_okx_ct_val(self._meta(coin), self._okx_ct_vals)
                size = size_check(
                    okx=books["okx"],
                    bybit=books["bybit"],
                    side="short",
                    event="open",
                    notional_usdt=float(manager.config.notional_usdt),
                    book_depth=int(manager.config.book_depth),
                    okx_ct_val=getattr(meta, "okx_ct_val", None),
                )
                books["okx"]["_private_size_gate"] = True
                books["okx"]["ct_val"] = getattr(meta, "okx_ct_val", None)
                qty = None
                qty_error = meta_error
                if not qty_error:
                    try:
                        qty = shared_from_meta(
                            meta=meta,
                            okx_px=reference_px(books["okx"], "sell"),
                            bybit_px=reference_px(books["bybit"], "buy"),
                        )
                    except Exception as exc:
                        qty_error = str(getattr(exc, "code", "qty_mismatch"))
                in_band = bool(
                    qty is not None
                    and all(Decimal("7") <= n <= Decimal("15") for n in (qty.okx_notional, qty.bybit_notional))
                )
                eligible = bool(size.get("size_ok")) and in_band and not qty_error
                detail: dict[str, Any] = {
                    "coin": coin,
                    "size_gate": size,
                    "mapper_error": qty_error,
                    "mapper_in_7_15_band": in_band,
                    "eligible": eligible,
                }
                if qty is not None:
                    detail["mapper"] = {
                        "okx_contracts": str(qty.okx_sz),
                        "bybit_qty": str(qty.bybit_qty),
                        "okx_notional_usdt": str(qty.okx_notional),
                        "bybit_notional_usdt": str(qty.bybit_notional),
                    }
                details.append(detail)
                if eligible:
                    candidates.append(coin)
            return candidates, details

        async def _synthetic_roll_loop(self) -> None:
            try:
                await self._bounded_roll()
            except Exception as exc:
                session = self._private_warm
                if session is not None and getattr(self, "_experiment_order_requests", 0) > 0:
                    try:
                        self._rest_snapshot_on_failure = _rest_state_snapshot(
                            POOL, self.universe, session.bybit_credentials, session.okx_credentials
                        )
                    except Exception as rest_exc:
                        self._rest_snapshot_on_failure = {
                            "read_error": type(rest_exc).__name__
                        }
                self._experiment_error = type(exc).__name__
                frames = traceback.extract_tb(exc.__traceback__)[-5:]
                self._experiment_error_detail = {
                    "type": type(exc).__name__,
                    "message": str(exc)[:200],
                    "frames": [
                        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                        for frame in frames
                    ],
                }
                self.stop_event.set()
                raise

        async def _bounded_roll(self) -> None:
            manager = self.theta_trade
            session = self._private_warm
            if manager is None or session is None or not self._synthetic_live_send_enabled:
                raise RuntimeError("shared_manager_or_private_send_not_ready")
            if manager.slot.pending or manager.slot.position is not None:
                raise RuntimeError("restored_position_or_pending_refused")
            wire = getattr(session, "wire", None)
            if wire is None or not wire.healthy:
                raise RuntimeError("wire_capture_unavailable")
            pool_rng = random.Random(SEED)
            summary: list[dict[str, Any]] = []
            self._experiment_summary = summary
            self._experiment_preflight: list[dict[str, Any]] = []
            self._experiment_pair_intents = 0
            self._experiment_order_requests = 0
            self._experiment_plan: list[dict[str, Any]] = []
            phase = {"action": "hold"}
            for cycle in range(MAX_CYCLES):
                self._experiment_stage = f"cycle_{cycle + 1}_pre_open_books"
                if len(summary) >= MAX_CYCLES or 2 * len(summary) >= MAX_PAIR_INTENTS or 4 * len(summary) >= MAX_ORDER_REQUESTS:
                    raise RuntimeError("campaign_bound_reached")
                eligible, details = await self._current_liquidity_candidates(manager)
                if not eligible:
                    self._experiment_preflight.append({
                        "cycle": cycle + 1,
                        "source_pool": list(POOL),
                        "candidates": details,
                        "eligible_pool": [],
                        "chosen_coin": None,
                    })
                    raise RuntimeError("no_liquidity_eligible_coin")
                coin = pool_rng.choice(eligible)
                self._experiment_preflight.append({
                    "cycle": cycle + 1,
                    "source_pool": list(POOL),
                    "candidates": details,
                    "eligible_pool": eligible,
                    "chosen_coin": coin,
                })
                phase["action"] = "open"
                self.install_bounded_decider(coin, phase)
                await _wait_for_fresh_books(self, coin)
                open_before = len(_read_trade_rows(self.data_root))
                self._experiment_stage = f"cycle_{cycle + 1}_open_manager"
                await manager.on_theta_snapshots_async([], quotes=self.quotes, coin_order=self.coins)
                if self._synthetic_roll_halt_reason or not wire.healthy:
                    raise RuntimeError("open_runtime_or_capture_halt")
                pos = manager.slot.position
                if manager.slot.pending or pos is None or pos.base_coin != coin:
                    raise RuntimeError("open_not_terminal_full_fill")
                if not pos.okx_filled_qty or not pos.bybit_filled_qty:
                    raise RuntimeError("open_fill_qty_missing")
                rows = _read_trade_rows(self.data_root)
                opened = [r for r in rows[open_before:] if r.get("event") == "open" and r.get("status") == "open" and r.get("base_coin") == coin]
                if not opened:
                    raise RuntimeError("open_journal_readback_missing")
                open_row = opened[-1]
                for price_key in ("okx_fill_px", "bybit_fill_px"):
                    px = Decimal(str(open_row.get(price_key) or "0"))
                    if not px.is_finite() or px <= 0:
                        raise RuntimeError("open_journal_price_invalid")
                if Decimal(str(open_row.get("okx_filled_qty"))) != Decimal(str(pos.okx_filled_qty)) or Decimal(str(open_row.get("bybit_filled_qty"))) != Decimal(str(pos.bybit_filled_qty)):
                    raise RuntimeError("open_journal_qty_mismatch")
                await asyncio.sleep(10.0)
                self._experiment_stage = f"cycle_{cycle + 1}_hold"
                if not wire.healthy:
                    raise RuntimeError("wire_capture_failed_during_hold")
                phase["action"] = "close"
                await _wait_for_fresh_books(self, coin)
                close_before = len(_read_trade_rows(self.data_root))
                self._experiment_stage = f"cycle_{cycle + 1}_close_manager"
                await manager.on_theta_snapshots_async([], quotes=self.quotes, coin_order=self.coins)
                if self._synthetic_roll_halt_reason or not wire.healthy:
                    raise RuntimeError("close_runtime_or_capture_halt")
                if manager.slot.pending or manager.slot.position is not None:
                    raise RuntimeError("close_not_terminal_full_fill")
                rows = _read_trade_rows(self.data_root)
                closed = [r for r in rows[close_before:] if r.get("event") == "close" and r.get("status") == "closed" and r.get("base_coin") == coin]
                if not closed:
                    raise RuntimeError("close_journal_readback_missing")
                close_row = closed[-1]
                for price_key in ("okx_fill_px", "bybit_fill_px"):
                    px = Decimal(str(close_row.get(price_key) or "0"))
                    if not px.is_finite() or px <= 0:
                        raise RuntimeError("close_journal_price_invalid")
                if Decimal(str(close_row.get("okx_filled_qty"))) != Decimal(str(open_row.get("okx_filled_qty"))) or Decimal(str(close_row.get("bybit_filled_qty"))) != Decimal(str(open_row.get("bybit_filled_qty"))) or close_row.get("reduce_only") is not True:
                    raise RuntimeError("close_qty_or_reduce_only_mismatch")
                if not wire.healthy:
                    raise RuntimeError("wire_capture_failed_after_close")
                self._experiment_stage = f"cycle_{cycle + 1}_flat_projection"
                bybit_creds, okx_creds = session.bybit_credentials, session.okx_credentials
                _wait_flat_projection(coin, self, bybit_creds, okx_creds)
                plan = self._experiment_plan[-1]
                meta = self._meta(coin)
                ct_val = Decimal(str(self._okx_ct_vals[meta.okx_symbol]))
                actual_okx_notional = Decimal(str(open_row["okx_filled_qty"])) * ct_val * Decimal(str(open_row["okx_fill_px"]))
                actual_bybit_notional = Decimal(str(open_row["bybit_filled_qty"])) * Decimal(str(open_row["bybit_fill_px"]))
                result_row = {
                    "cycle": cycle + 1,
                    "coin": coin,
                    "side": "short",
                    "okx_contracts": open_row.get("okx_filled_qty"),
                    "bybit_qty": open_row.get("bybit_filled_qty"),
                    "okx_fill_px": open_row.get("okx_fill_px"),
                    "bybit_fill_px": open_row.get("bybit_fill_px"),
                    "planned": plan,
                    "actual_open_notional_okx": str(actual_okx_notional),
                    "actual_open_notional_bybit": str(actual_bybit_notional),
                    "open_fill_ts_ms": open_row.get("fill_ts_ms"),
                    "close_fill_ts_ms": close_row.get("fill_ts_ms"),
                    "open_intent_id": open_row.get("intent_id"),
                    "close_intent_id": close_row.get("intent_id"),
                    "wire": _wire_markers(wire, {str(open_row.get("intent_id") or ""), str(close_row.get("intent_id") or "")}),
                }
                summary.append(result_row)
                if cycle == 0:
                    self._experiment_stage = "cycle_1_raw_and_adversarial_replay"
                    result_row["replay"] = _replay_after_first_close(self, str(open_row.get("intent_id") or ""))
            self.stop_event.set()

    return Runtime


def _offline_self_test() -> None:
    import asyncio
    import sys
    import tempfile
    from app.bot.private.coin_qty import exact_close_from_meta, shared_coin_qty
    from types import SimpleNamespace

    try:
        import websockets  # noqa: F401
    except ModuleNotFoundError as exc:
        if exc.name != "websockets":
            raise
        # Runtime is imported for a no-network control test. This narrow stub
        # satisfies the module import only; no connect call is made in self-test.
        sys.modules["websockets"] = SimpleNamespace(connect=None)

    from app.bot.runtime import BotRuntime
    from app.bot.theta_trade_manager import ThetaTradeConfig, ThetaTradeManager

    # Exercise carried filled quantity at a changed price without loading any
    # runtime/websocket module or touching a live credential.
    meta = SimpleNamespace(
        okx_ct_val=Decimal("10"),
        okx_lot_size=Decimal("0.1"),
        okx_min_size=Decimal("0.1"),
        bybit_qty_step=Decimal("1"),
        bybit_min_order_qty=Decimal("1"),
    )
    qty = shared_coin_qty(
        okx_px=Decimal("10"), bybit_px=Decimal("10"), ct_val=Decimal("10"),
        okx_lot_sz=Decimal("0.1"), okx_min_sz=Decimal("0.1"),
        bybit_qty_step=Decimal("1"), bybit_min_qty=Decimal("1"),
    )
    close = exact_close_from_meta(
        meta=meta, okx_px=Decimal("11"), bybit_px=Decimal("11"),
        okx_sz=qty.okx_sz, bybit_qty=qty.bybit_qty,
    )
    assert close.okx_sz == qty.okx_sz and close.bybit_qty == qty.bybit_qty

    # The live runner must not activate BotRuntime's independent `probe`
    # broker path. Verify that a policy-mode book tick with the manager enabled
    # returns before the ordinary broker can place anything.
    _configure("execute")
    assert os.environ["BBOT_MODE"] == "policy"
    broker = SimpleNamespace(place_calls=0)
    def forbidden_probe_place(**_kwargs: Any) -> None:
        broker.place_calls += 1
    broker.place = forbidden_probe_place
    policy_runtime = BotRuntime.__new__(BotRuntime)
    policy_runtime.theta_trade_enabled = True
    policy_runtime.broker = broker
    BotRuntime._policy_maybe_act(
        policy_runtime,
        base_coin="2Z",
        event_local_ts_ms=1,
        okx={"bid_price": 0.04, "ask_price": 0.05},
        bybit={"bid_price": 0.04, "ask_price": 0.05},
        spread_long=0.0,
        spread_short=0.0,
    )
    assert broker.place_calls == 0

    # Exercise the real shared manager decision -> injected place callback
    # with fresh synthetic L1 books and an explicit no-send stub result.
    now_ms = time.time() * 1000.0
    fresh = {
        "bid_price": 0.049,
        "bid_size": 1000.0,
        "ask_price": 0.050,
        "ask_size": 1000.0,
        "local_recv_ts_ms": now_ms,
        "ts_exchange": now_ms,
    }
    quotes = {"2Z": {"okx": dict(fresh), "bybit": dict(fresh)}}
    freshness_runtime = SimpleNamespace(
        quotes=quotes,
        gate=SimpleNamespace(evaluate=lambda *_args: None),
    )
    _assert_fresh_books(freshness_runtime, "2Z")
    with tempfile.TemporaryDirectory(prefix="response-manager-selftest-") as tmp:
        place_calls: list[dict[str, Any]] = []
        manager = ThetaTradeManager(
            data_root=Path(tmp),
            config=ThetaTradeConfig(notional_usdt=10.0, fill_delay_ms=0),
            place_fn=lambda **kwargs: (
                place_calls.append(kwargs) or SimpleNamespace(abort="offline_no_send")
            ),
            meta_fn=lambda _coin: object(),
            decide_fn=lambda **_kwargs: SimpleNamespace(
                action="open", coin="2Z", side="short"
            ),
        )
        asyncio.run(manager.on_theta_snapshots_async([], quotes=quotes, coin_order=["2Z"]))
        assert len(place_calls) == 1
        assert place_calls[0]["spread_side"] == "open_short"
        assert manager.slot.position is None and manager.slot.pending is False

    if canary29_prep:
        from app.bot.runtime import BotRuntime as Runtime
    else:
        Runtime = _runtime_class()
    candidate_runtime = Runtime.__new__(Runtime)
    candidate_runtime.stop_event = asyncio.Event()
    candidate_runtime.gate = SimpleNamespace(evaluate=lambda *_args: None)
    candidate_runtime._okx_ct_vals = {
        "2Z-USDT-SWAP": Decimal("10"),
        "HOME-USDT-SWAP": Decimal("100"),
        "LA-USDT-SWAP": Decimal("10"),
    }
    candidate_meta = {
        "2Z": SimpleNamespace(
            okx_symbol="2Z-USDT-SWAP", okx_ct_val=Decimal("10"),
            okx_lot_size=Decimal("1"), okx_min_size=Decimal("1"),
            bybit_qty_step=Decimal("1"), bybit_min_order_qty=Decimal("1"),
        ),
        "HOME": SimpleNamespace(
            okx_symbol="HOME-USDT-SWAP", okx_ct_val=Decimal("100"),
            okx_lot_size=Decimal("1"), okx_min_size=Decimal("1"),
            bybit_qty_step=Decimal("10"), bybit_min_order_qty=Decimal("10"),
        ),
        "LA": SimpleNamespace(
            okx_symbol="LA-USDT-SWAP", okx_ct_val=Decimal("10"),
            okx_lot_size=Decimal("1"), okx_min_size=Decimal("1"),
            bybit_qty_step=Decimal("0.1"), bybit_min_order_qty=Decimal("0.1"),
        ),
    }
    candidate_runtime._meta = lambda coin: candidate_meta[coin]
    candidate_runtime.quotes = {}
    for coin, price, okx_bid_size, bybit_ask_size in (
        ("2Z", 0.0447, 52.0, 1000.0),
        ("HOME", 0.00566, 4.0, 470.0),
        ("LA", 0.0672, 200.0, 200.0),
    ):
        candidate_runtime.quotes[coin] = {
            "okx": {
                "bid_price": price, "bid_size": okx_bid_size,
                "ask_price": price, "ask_size": 500.0,
                "local_recv_ts_ms": now_ms, "ts_exchange": now_ms,
            },
            "bybit": {
                "bid_price": price, "bid_size": 500.0,
                "ask_price": price, "ask_size": bybit_ask_size,
                "local_recv_ts_ms": now_ms, "ts_exchange": now_ms,
            },
        }
    candidate_manager = SimpleNamespace(
        config=ThetaTradeConfig(notional_usdt=10.0, book_depth=1)
    )
    eligible, candidate_details = asyncio.run(
        candidate_runtime._current_liquidity_candidates(candidate_manager)
    )
    assert eligible == ["2Z", "LA"]
    assert [row["eligible"] for row in candidate_details] == [True, False, True]

    # Exercise the live sender wrapper through quantity preflight and its
    # parent call, replacing the parent transport with a local sentinel.
    from app.bot.private.place_send import PlaceSendResult

    wrapper_runtime = Runtime.__new__(Runtime)
    wrapper_runtime._okx_ct_vals = candidate_runtime._okx_ct_vals
    wrapper_runtime._experiment_pair_intents = 0
    wrapper_runtime._experiment_order_requests = 0
    wrapper_runtime._experiment_plan = []
    wrapper_runtime._synthetic_roll_halt_reason = None
    parent_runtime = Runtime.__mro__[1]
    original_parent_place = parent_runtime._synthetic_live_place
    mocked_result = PlaceSendResult(abort="offline_no_send")
    parent_runtime._synthetic_live_place = lambda self, **_kwargs: mocked_result
    try:
        result = wrapper_runtime._synthetic_live_place(
            base_coin="2Z",
            spread_side="open_short",
            meta=candidate_meta["2Z"],
            okx_book=candidate_runtime.quotes["2Z"]["okx"],
            bybit_book=candidate_runtime.quotes["2Z"]["bybit"],
        )
    finally:
        parent_runtime._synthetic_live_place = original_parent_place
    assert result is mocked_result
    assert len(wrapper_runtime._experiment_plan) == 1
    assert wrapper_runtime._experiment_pair_intents == 1
    assert wrapper_runtime._experiment_order_requests == 2

    rng = random.Random(SEED)
    choices = [rng.choice(POOL) for _ in range(MAX_CYCLES)]
    assert len(choices) == MAX_CYCLES and all(c in POOL for c in choices)
    try:
        _assert_one_x_readback({("bybit", "2ZUSDT"): "3"}, {("bybit", "2ZUSDT")})
    except RuntimeError:
        pass
    else:
        raise AssertionError("non_one_x_readback_must_fail_before_sender")
    send_calls = 0
    assert send_calls == 0
    assert MAX_CYCLES == 3 and MAX_PAIR_INTENTS == 6 and MAX_ORDER_REQUESTS == 12


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--self-test", action="store_true")
    group.add_argument("--set-leverage-only", action="store_true")
    group.add_argument("--prepare-canary29", action="store_true")
    group.add_argument("--execute-live", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _offline_self_test()
        print(json.dumps({"self_test": "pass", "orders_sent": 0, "pool": POOL, "seed": SEED}))
        return 0
    canary29_prep = bool(args.prepare_canary29)
    coins = CANARY29_POOL if canary29_prep else POOL
    run_id = _configure(
        "prepare" if canary29_prep else ("execute" if args.execute_live else "leverage"),
        pool=coins,
        canary29=canary29_prep,
    )
    Runtime = _runtime_class()
    runtime = Runtime()
    if tuple(runtime.coins) != coins or runtime.notional != 10.0:
        raise RuntimeError("experiment_pool_or_notional_mismatch")
    if canary29_prep:
        result = _set_and_readback(
            runtime,
            coins=CANARY29_POOL,
            previously_confirmed=PREVIOUSLY_CONFIRMED_1X,
        )
        report = {
            "run_id": run_id,
            "mode": "canary29_leverage_prepare",
            "status": "confirmed_1x",
            "pool": CANARY29_POOL,
            "previously_confirmed_no_new_readback": sorted(PREVIOUSLY_CONFIRMED_1X),
            "readback_confirmed_new_coins": len(CANARY29_POOL) - len(PREVIOUSLY_CONFIRMED_1X),
            "confirmations": result,
            "orders_sent": 0,
        }
        report_path = _save_report(run_id, report)
        config_path = report_path.parent / "confirmed_1x.env"
        config_path.write_text(
            "BBOT_COINS=" + ",".join(CANARY29_POOL) + "\n"
            + "BBOT_CONFIRMED_1X_COINS=" + ",".join(CANARY29_POOL) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({**report, "report_path": str(report_path), "config_path": str(config_path)}, sort_keys=True))
        return 0
    if args.set_leverage_only:
        result = _set_and_readback(runtime)
        report = {"run_id": run_id, "mode": "leverage_only", "status": "confirmed_1x", "confirmed": result, "order_requests": 0}
        report_path = _save_report(run_id, report)
        print(json.dumps({**report, "report_path": str(report_path)}, sort_keys=True))
        return 0
    async def run() -> None:
        if runtime.theta_trade is None:
            raise RuntimeError("shared_theta_manager_missing")
        await runtime.run()
    try:
        asyncio.run(run())
    except Exception:
        position = getattr(getattr(runtime, "theta_trade", None), "slot", None)
        pos = getattr(position, "position", None)
        report = {
            "run_id": run_id,
            "mode": "execute_live",
            "status": "halted",
            "error_type": getattr(runtime, "_experiment_error", "runtime_error"),
            "error_detail": getattr(runtime, "_experiment_error_detail", None),
            "error_stage": getattr(runtime, "_experiment_stage", None),
            "cycles_completed": len(getattr(runtime, "_experiment_summary", [])),
            "pair_intents_reserved": getattr(runtime, "_experiment_pair_intents", 0),
            "order_requests_reserved": getattr(runtime, "_experiment_order_requests", 0),
            "pending": bool(getattr(position, "pending", False)),
            "manager_position": None if pos is None else {
                "coin": pos.base_coin, "side": pos.side,
                "okx_filled_qty": pos.okx_filled_qty,
                "bybit_filled_qty": pos.bybit_filled_qty,
            },
            "rest_snapshot": getattr(runtime, "_rest_snapshot_on_failure", None),
            "preflight": getattr(runtime, "_experiment_preflight", []),
            "completed_cycles": getattr(runtime, "_experiment_summary", []),
        }
        report_path = _save_report(run_id, report)
        print(json.dumps({**report, "report_path": str(report_path)}, sort_keys=True))
        return 2
    summary = getattr(runtime, "_experiment_summary", [])
    if len(summary) != MAX_CYCLES:
        raise RuntimeError("campaign_incomplete")
    report = {
        "run_id": run_id,
        "mode": "execute_live",
        "status": "completed",
        "pool": POOL,
        "seed": SEED,
        "target_notional_usdt": str(TARGET_NOTIONAL),
        "cycles": summary,
        "preflight": getattr(runtime, "_experiment_preflight", []),
        "pair_intents": getattr(runtime, "_experiment_pair_intents", 0),
        "order_requests": getattr(runtime, "_experiment_order_requests", 0),
    }
    report_path = _save_report(run_id, report)
    print(json.dumps({**report, "report_path": str(report_path)}, sort_keys=True))
    return 0


def _set_leverage_one_get_only(runtime: Any, bybit: Any, okx: Any) -> dict[tuple[str, str], str]:
    """GET-only leverage readback used after the separately approved setup step."""
    from app.bot.private.rest_readonly import build_okx_readonly_headers
    from app.bot.private.venue import endpoints_for_venue
    from app.bot.private.ws_w4_baseline import _BYBIT_POS, _bybit_signed_get, _http_get_json

    ep = endpoints_for_venue("live")
    out: dict[tuple[str, str], str] = {}
    for coin in POOL:
        meta = runtime.universe[coin]
        bdata = _bybit_signed_get(
            credentials=bybit,
            base=ep.bybit_rest,
            path=_BYBIT_POS,
            query=f"category=linear&symbol={meta.bybit_symbol}&settleCoin=USDT&limit=200",
        )
        brows = ((bdata.get("result") or {}).get("list")) or []
        bvals = {str(row.get(k)) for row in brows if str(row.get("symbol") or "") == meta.bybit_symbol for k in ("leverage", "buyLeverage", "sellLeverage") if row.get(k) not in (None, "")}
        if not bvals or bvals != {"1"}:
            raise RuntimeError(f"leverage_readback_failed:bybit:{coin}")
        path = f"/api/v5/account/leverage-info?{urlencode({'instId': meta.okx_symbol, 'mgnMode': 'cross'})}"
        headers = build_okx_readonly_headers(api_key=okx.api_key, api_secret=okx.api_secret, passphrase=okx.passphrase or "", path=path, simulated_trading=False)
        data = _http_get_json(f"{ep.okx_rest}{path}", headers, timeout_sec=15.0)
        rows = data.get("data") or []
        if str(data.get("code")) != "0" or not rows or str(rows[0].get("lever")) != "1":
            raise RuntimeError(f"leverage_readback_failed:okx:{coin}")
        out[("bybit", meta.bybit_symbol)] = "1"
        out[("okx", meta.okx_symbol)] = "1"
    expected = {(venue, runtime.universe[coin].bybit_symbol if venue == "bybit" else runtime.universe[coin].okx_symbol) for coin in POOL for venue in ("bybit", "okx")}
    _assert_one_x_readback(out, expected)
    return out


if __name__ == "__main__":
    raise SystemExit(main())

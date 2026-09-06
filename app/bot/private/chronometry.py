"""Post-ack Contour B trade chronometry (canary WAL/EDEN).

Freeze the L1 ring, derive signal vs fill spread, write JSON + HTML.
Never waits on the signal→send path. Optional fill wait is after dual_ack
and off the place return unless ``BBOT_CHRONOMETRY_SYNC=1``.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.private.chronometry_dashboard import render_dashboard_html
from app.bot.private.l1_tick_ring import (
    L1Tick,
    SignalBookSnapshot,
    capture_signal_book,
    fill_spread_pct,
    freeze_window,
    get_signal_book,
    store_signal_book,
)
from app.bot.private.paths import trade_report_dir
from app.bot.private.ws_messages import okx_ws_id_is_legal
from app.bot.private.wire_transcript import (
    iter_wire_files,
    read_wire_jsonl,
    scan_all_wire_events,
)

SCHEMA_VERSION = "bbot.canary.chronometry.v1"
DEFAULT_LOOKBACK_SEC = 30.0
DEFAULT_LOOKAHEAD_SEC = 15.0
DEFAULT_FILL_WAIT_SEC = 8.0
ENV_LOOKBACK_SEC = "BBOT_CHRONOMETRY_LOOKBACK_SEC"
ENV_LOOKAHEAD_SEC = "BBOT_CHRONOMETRY_LOOKAHEAD_SEC"
ENV_FILL_WAIT_SEC = "BBOT_CHRONOMETRY_FILL_WAIT_SEC"
ENV_SYNC = "BBOT_CHRONOMETRY_SYNC"
CANARY_PROFILES = frozenset({"canary_wal_eden", "canary"})

_FILL_PRICE_KEYS = frozenset(
    {
        "execprice",
        "execpx",
        "fillpx",
        "avgpx",
        "avgprice",
        "lastpx",
        "fill_price",
        "exec_price",
    }
)
_ORDER_ID_KEYS = frozenset(
    {
        "orderlinkid",
        "clordid",
        "ordid",
        "orderid",
        "sorderid",
    }
)

LogFn = Callable[[str], None]


def chronometry_enabled(
    env: Optional[Mapping[str, str]] = None,
    *,
    coin: Optional[str] = None,
) -> bool:
    """On for canary profile unless explicitly off. Opt-in otherwise."""
    del coin
    e = env if env is not None else os.environ
    raw = str(e.get("BBOT_CHRONOMETRY") or "").strip().lower()
    if raw in {"0", "false", "off", "no"}:
        return False
    profile = str(e.get("BBOT_PROFILE") or "").strip().lower()
    if profile in CANARY_PROFILES:
        return True
    return raw in {"1", "true", "on", "yes"}


def resolve_lookback_ms(env: Optional[Mapping[str, str]] = None) -> int:
    return _positive_sec_ms(env, ENV_LOOKBACK_SEC, DEFAULT_LOOKBACK_SEC, cap=300.0)


def resolve_lookahead_ms(env: Optional[Mapping[str, str]] = None) -> int:
    return _positive_sec_ms(env, ENV_LOOKAHEAD_SEC, DEFAULT_LOOKAHEAD_SEC, cap=120.0)


def resolve_fill_wait_sec(env: Optional[Mapping[str, str]] = None) -> float:
    e = env if env is not None else os.environ
    raw = e.get(ENV_FILL_WAIT_SEC)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_FILL_WAIT_SEC
    val = float(raw)
    if val < 0 or val > 60:
        raise ValueError(f"{ENV_FILL_WAIT_SEC} must be in [0, 60], got {val}")
    return val


def _positive_sec_ms(
    env: Optional[Mapping[str, str]],
    name: str,
    default: float,
    *,
    cap: float,
) -> int:
    e = env if env is not None else os.environ
    raw = e.get(name)
    if raw is None or str(raw).strip() == "":
        return int(default * 1000)
    val = float(raw)
    if val <= 0 or val > cap:
        raise ValueError(f"{name} must be in (0, {cap}], got {val}")
    return int(val * 1000)


def _truthy(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "on", "yes"}


def spread_kind_for_side(
    spread_side: str,
    *,
    open_spread_side: Optional[str] = None,
) -> str:
    """Which policy formula matches this open/close."""
    side = str(spread_side).strip().lower()
    if side == "open_long":
        return "long"
    if side == "open_short":
        return "short"
    if side == "close":
        opened = str(open_spread_side or "").strip().lower()
        if opened == "open_long":
            return "short"
        if opened == "open_short":
            return "long"
    raise ValueError(
        f"cannot derive spread_kind from side={spread_side!r} "
        f"open={open_spread_side!r}"
    )


def venues_for_kind(spread_kind: str) -> tuple[str, str]:
    """``(sell_venue, buy_venue)`` for the matching side."""
    kind = str(spread_kind).strip().lower()
    if kind == "long":
        return "bybit", "okx"
    if kind == "short":
        return "okx", "bybit"
    raise ValueError(f"spread_kind must be long|short, got {spread_kind!r}")


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def extract_fill_price(payload: Any) -> Optional[float]:
    """First recognizable exec/avg fill price in a redacted wire payload."""
    found: list[float] = []

    def _walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                nk = str(key).strip().lower().replace("-", "_")
                if nk in _FILL_PRICE_KEYS:
                    px = _finite(value)
                    if px is not None and px > 0:
                        found.append(px)
                _walk(value)
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return found[0] if found else None


def _payload_ids(payload: Any) -> set[str]:
    out: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                nk = str(key).strip().lower().replace("-", "_")
                if nk in _ORDER_ID_KEYS or nk in {"reqid", "req_id", "id"}:
                    if value is not None and not isinstance(value, bool):
                        text = str(value).strip()
                        if text:
                            out.add(text)
                _walk(value)
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return out


def _event_matches_intent(
    event: Mapping[str, Any],
    *,
    intent_id: str,
    dual_leg_id: Optional[str],
    req_ids: set[str],
) -> bool:
    if event.get("intent_id") == intent_id:
        return True
    if dual_leg_id and event.get("dual_leg_id") == dual_leg_id:
        return True
    rid = event.get("req_id")
    if rid and str(rid) in req_ids:
        return True
    ids = _payload_ids(event.get("payload"))
    if dual_leg_id:
        for item in ids:
            if dual_leg_id in item:
                return True
    return bool(ids & req_ids)


def collect_wire_events(
    *,
    data_root: Path,
    intent_id: str,
    dual_leg_id: Optional[str] = None,
    req_ids: Optional[Sequence[str]] = None,
    extra_events: Optional[Sequence[Mapping[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Wire rows for this intent. Process transcript first, then disk."""
    wanted = {str(r) for r in (req_ids or ()) if r}
    found: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any, Any]] = set()

    def _add(ev: Mapping[str, Any]) -> None:
        if not _event_matches_intent(
            ev, intent_id=intent_id, dual_leg_id=dual_leg_id, req_ids=wanted
        ):
            return
        key = (ev.get("wall_ms"), ev.get("dir"), ev.get("venue"), ev.get("req_id"))
        if key in seen:
            return
        seen.add(key)
        found.append(dict(ev))

    if extra_events:
        for ev in extra_events:
            if isinstance(ev, Mapping):
                _add(ev)
    private_root = data_root / "private"
    for root in (data_root, private_root):
        if not root.is_dir():
            continue
        for path in iter_wire_files(root):
            for ev in read_wire_jsonl(path):
                _add(ev)
        # scan_all is the same glob; keep as fallback if layout differs
        if not any(True for _ in iter_wire_files(root)):
            for ev in scan_all_wire_events(root):
                _add(ev)
    found.sort(key=lambda ev: int(ev.get("wall_ms") or 0))
    return found


def _mono_to_wall(
    mono_ns: Optional[int],
    *,
    anchor_wall_ms: int,
    anchor_mono_ns: int,
) -> Optional[int]:
    if mono_ns is None:
        return None
    return int(anchor_wall_ms + (int(mono_ns) - int(anchor_mono_ns)) / 1_000_000.0)


def markers_from_send_ack(
    *,
    signal_ts_ms: int,
    send_result: Any = None,
    ack_result: Any = None,
    wire_events: Optional[Sequence[Mapping[str, Any]]] = None,
    anchor_wall_ms: Optional[int] = None,
    anchor_mono_ns: Optional[int] = None,
) -> list[dict[str, Any]]:
    """signal / send / ack / fill markers. Wire wins over mono conversion."""
    markers: list[dict[str, Any]] = [
        {"kind": "signal", "venue": None, "wall_ms": int(signal_ts_ms), "price": None}
    ]
    by_kind: dict[tuple[str, Optional[str]], dict[str, Any]] = {
        ("signal", None): markers[0]
    }

    def _put(kind: str, venue: Optional[str], wall_ms: Optional[int], **extra: Any) -> None:
        if wall_ms is None:
            return
        rec = {
            "kind": kind,
            "venue": venue,
            "wall_ms": int(wall_ms),
            "price": extra.pop("price", None),
        }
        rec.update(extra)
        key = (kind, venue)
        prev = by_kind.get(key)
        if prev is None:
            by_kind[key] = rec
            markers.append(rec)
            return
        prev.update({k: v for k, v in rec.items() if v is not None})

    if send_result is not None and anchor_wall_ms is not None and anchor_mono_ns is not None:
        first_v = getattr(send_result, "first_venue", "bybit")
        second_v = getattr(send_result, "second_venue", "okx")
        _put(
            "send",
            first_v,
            _mono_to_wall(
                getattr(send_result, "first_sent_ns", None),
                anchor_wall_ms=anchor_wall_ms,
                anchor_mono_ns=anchor_mono_ns,
            ),
        )
        _put(
            "send",
            second_v,
            _mono_to_wall(
                getattr(send_result, "second_sent_ns", None),
                anchor_wall_ms=anchor_wall_ms,
                anchor_mono_ns=anchor_mono_ns,
            ),
        )

    if ack_result is not None:
        for outcome in (getattr(ack_result, "bybit", None), getattr(ack_result, "okx", None)):
            if outcome is None:
                continue
            venue = getattr(outcome, "venue", None)
            wall = getattr(outcome, "wall_ms", None)
            if wall is None and anchor_wall_ms is not None and anchor_mono_ns is not None:
                wall = _mono_to_wall(
                    getattr(outcome, "recv_ns", None),
                    anchor_wall_ms=anchor_wall_ms,
                    anchor_mono_ns=anchor_mono_ns,
                )
            _put("ack", venue, wall, req_id=getattr(outcome, "req_id", None))

    for ev in wire_events or ():
        venue = ev.get("venue")
        wall = ev.get("wall_ms")
        direction = ev.get("dir")
        if direction == "out":
            _put("send", venue, wall, req_id=ev.get("req_id"), source="wire")
        elif direction == "in":
            px = extract_fill_price(ev.get("payload"))
            if px is not None:
                _put(
                    "fill",
                    venue,
                    wall,
                    price=px,
                    fill_delivery_ms=ev.get("fill_delivery_ms"),
                    source="wire",
                )
            elif ev.get("socket") == "trade" or ev.get("op") in {
                "order.create",
                "order",
                None,
            }:
                # Trade ACK / error inbound.
                _put("ack", venue, wall, req_id=ev.get("req_id"), source="wire")

    markers.sort(key=lambda m: (int(m["wall_ms"]), str(m["kind"])))
    return markers


def latency_table(markers: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Optional[int]]]:
    signal = next((m for m in markers if m.get("kind") == "signal"), None)
    signal_ms = int(signal["wall_ms"]) if signal else None
    out: dict[str, dict[str, Optional[int]]] = {
        "signal_to_send": {"bybit": None, "okx": None},
        "send_to_ack": {"bybit": None, "okx": None},
        "signal_to_fill": {"bybit": None, "okx": None},
        "fill_delivery": {"bybit": None, "okx": None},
    }
    for venue in ("bybit", "okx"):
        send = next(
            (m for m in markers if m.get("kind") == "send" and m.get("venue") == venue),
            None,
        )
        ack = next(
            (m for m in markers if m.get("kind") == "ack" and m.get("venue") == venue),
            None,
        )
        fill = next(
            (m for m in markers if m.get("kind") == "fill" and m.get("venue") == venue),
            None,
        )
        if signal_ms is not None and send is not None:
            out["signal_to_send"][venue] = int(send["wall_ms"]) - signal_ms
        if send is not None and ack is not None:
            out["send_to_ack"][venue] = int(ack["wall_ms"]) - int(send["wall_ms"])
        if signal_ms is not None and fill is not None:
            out["signal_to_fill"][venue] = int(fill["wall_ms"]) - signal_ms
        if fill is not None and fill.get("fill_delivery_ms") is not None:
            out["fill_delivery"][venue] = int(fill["fill_delivery_ms"])
    return out


def fill_prices_from_markers(
    markers: Sequence[Mapping[str, Any]],
) -> dict[str, Optional[float]]:
    prices: dict[str, Optional[float]] = {"bybit": None, "okx": None}
    for m in markers:
        if m.get("kind") != "fill":
            continue
        venue = m.get("venue")
        px = _finite(m.get("price"))
        if venue in prices and px is not None:
            prices[str(venue)] = px
    return prices


@dataclass
class ChronometryContext:
    intent_id: str
    base_coin: str
    spread_side: str
    phase: str
    signal_ts_ms: int
    data_root: Path
    open_spread_side: Optional[str] = None
    dual_leg_id: Optional[str] = None
    req_ids: tuple[str, ...] = ()
    signal_book: Optional[SignalBookSnapshot] = None
    send_result: Any = None
    ack_result: Any = None
    wire_events: Optional[list[dict[str, Any]]] = None
    ticks: Optional[list[L1Tick]] = None
    anchor_wall_ms: Optional[int] = None
    anchor_mono_ns: Optional[int] = None
    lookback_ms: int = int(DEFAULT_LOOKBACK_SEC * 1000)
    lookahead_ms: int = int(DEFAULT_LOOKAHEAD_SEC * 1000)


def build_chronometry_artifact(ctx: ChronometryContext) -> dict[str, Any]:
    kind = spread_kind_for_side(ctx.spread_side, open_spread_side=ctx.open_spread_side)
    sell_venue, buy_venue = venues_for_kind(kind)
    snap = ctx.signal_book or get_signal_book(ctx.intent_id)
    signal_spread = snap.signal_spread_pct(kind) if snap is not None else None

    wire = ctx.wire_events
    if wire is None:
        wire = collect_wire_events(
            data_root=ctx.data_root,
            intent_id=ctx.intent_id,
            dual_leg_id=ctx.dual_leg_id,
            req_ids=ctx.req_ids,
        )
    markers = markers_from_send_ack(
        signal_ts_ms=ctx.signal_ts_ms,
        send_result=ctx.send_result,
        ack_result=ctx.ack_result,
        wire_events=wire,
        anchor_wall_ms=ctx.anchor_wall_ms,
        anchor_mono_ns=ctx.anchor_mono_ns,
    )
    last_marker = max(int(m["wall_ms"]) for m in markers) if markers else ctx.signal_ts_ms
    start_ms = int(ctx.signal_ts_ms) - int(ctx.lookback_ms)
    end_ms = max(int(last_marker) + int(ctx.lookahead_ms), int(ctx.signal_ts_ms) + int(ctx.lookahead_ms))
    ticks = ctx.ticks
    if ticks is None:
        ticks = freeze_window(ctx.base_coin, start_ms=start_ms, end_ms=end_ms)
    fills = fill_prices_from_markers(markers)
    fill_spread = None
    if fills.get("bybit") is not None and fills.get("okx") is not None:
        fill_spread = fill_spread_pct(
            spread_kind=kind,
            bybit_exec=fills["bybit"],
            okx_exec=fills["okx"],
        )
    notes: list[str] = []
    if not ticks:
        notes.append(
            "public L1 ring empty for this window — ticks were not retained "
            "(overnight gap / process restart). Signal book snapshot is still shown. "
            "Do not invent a tape."
        )
    if fill_spread is None:
        notes.append("fill spread unavailable (exec/avg prices not observed yet)")

    okx_ids = [
        str(m.get("req_id"))
        for m in markers
        if m.get("venue") == "okx" and m.get("req_id")
    ]
    illegal_okx = [rid for rid in okx_ids if not okx_ws_id_is_legal(rid)]
    if illegal_okx:
        notes.append(
            "okx_req_id_not_legal (underscore/charset) — display only; "
            "not sent by this dashboard"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "intent_id": ctx.intent_id,
        "base_coin": str(ctx.base_coin).upper(),
        "spread_side": ctx.spread_side,
        "phase": ctx.phase,
        "open_spread_side": ctx.open_spread_side,
        "spread_kind": kind,
        "sell_venue": sell_venue,
        "buy_venue": buy_venue,
        "window": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "lookback_ms": int(ctx.lookback_ms),
            "lookahead_ms": int(ctx.lookahead_ms),
        },
        "signal_ts_ms": int(ctx.signal_ts_ms),
        "signal_book": snap.to_dict() if snap is not None else None,
        "signal_spread_pct": signal_spread,
        "fill_prices": fills,
        "fill_spread_pct": fill_spread,
        "ticks": [t.to_dict() for t in ticks],
        "tick_count": len(ticks),
        "ticks_missing": len(ticks) == 0,
        "markers": markers,
        "latency_ms": latency_table(markers),
        "notes": notes,
    }


def write_chronometry_files(
    artifact: Mapping[str, Any],
    *,
    data_root: Path,
) -> dict[str, Path]:
    import json

    intent_id = str(artifact["intent_id"])
    out_dir = trade_report_dir(data_root, intent_id)
    json_path = out_dir / "chronometry.json"
    html_path = out_dir / "dashboard.html"
    tmp_json = out_dir / ".chronometry.json.tmp"
    tmp_html = out_dir / ".dashboard.html.tmp"
    tmp_json.write_text(
        json.dumps(dict(artifact), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp_json.replace(json_path)
    tmp_html.write_text(render_dashboard_html(artifact), encoding="utf-8")
    tmp_html.replace(html_path)
    return {"dir": out_dir, "json": json_path, "html": html_path}


def persist_signal_book_on_place(
    *,
    intent_id: str,
    okx_book: Mapping[str, Any],
    bybit_book: Mapping[str, Any],
    event_local_ts_ms: Optional[int] = None,
    extra: Optional[dict[str, Any]] = None,
) -> SignalBookSnapshot:
    """In-memory snapshot at place. Disk write is with the dashboard after ack."""
    snap = capture_signal_book(
        okx_book,
        bybit_book,
        event_local_ts_ms=event_local_ts_ms,
    )
    store_signal_book(intent_id, snap)
    if extra is not None:
        extra["signal_bybit_bid"] = snap.bybit_bid
        extra["signal_bybit_ask"] = snap.bybit_ask
        extra["signal_okx_bid"] = snap.okx_bid
        extra["signal_okx_ask"] = snap.okx_ask
        extra["signal_spread_long_pct"] = snap.spread_long_pct
        extra["signal_spread_short_pct"] = snap.spread_short_pct
    return snap


def emit_after_dual_ack(
    *,
    data_root: Path,
    pending: Any,
    phase: str,
    env: Optional[Mapping[str, str]] = None,
    send_result: Any = None,
    ack_result: Any = None,
    wire_events: Optional[list[dict[str, Any]]] = None,
    ticks: Optional[list[L1Tick]] = None,
    anchor_wall_ms: Optional[int] = None,
    anchor_mono_ns: Optional[int] = None,
    log: Optional[LogFn] = None,
) -> Optional[dict[str, Path]]:
    """After both trade ACKs. Fail-soft: never abort a successful place."""
    e = env if env is not None else os.environ
    coin = getattr(pending, "base_coin", None)
    if not chronometry_enabled(e, coin=coin):
        return None
    logger = log or (lambda _m: None)
    try:
        ctx = _context_from_pending(
            data_root=data_root,
            pending=pending,
            phase=phase,
            env=e,
            send_result=send_result,
            ack_result=ack_result,
            wire_events=wire_events,
            ticks=ticks,
            anchor_wall_ms=anchor_wall_ms,
            anchor_mono_ns=anchor_mono_ns,
        )
        wait_sec = resolve_fill_wait_sec(e)
        if _truthy(e.get(ENV_SYNC)) or wait_sec <= 0:
            return _emit_now(ctx, logger)
        thread = threading.Thread(
            target=_emit_after_fill_wait,
            args=(ctx, wait_sec, logger),
            name=f"chronometry-{ctx.intent_id[:8]}",
            daemon=True,
        )
        thread.start()
        return None
    except Exception as exc:  # noqa: BLE001 — never fail a live trade
        logger(
            f"chronometry_failed | intent_id={getattr(pending, 'intent_id', '')} | "
            f"err={type(exc).__name__}"
        )
        return None


def _context_from_pending(
    *,
    data_root: Path,
    pending: Any,
    phase: str,
    env: Mapping[str, str],
    send_result: Any,
    ack_result: Any,
    wire_events: Optional[list[dict[str, Any]]],
    ticks: Optional[list[L1Tick]],
    anchor_wall_ms: Optional[int],
    anchor_mono_ns: Optional[int],
) -> ChronometryContext:
    req_ids: list[str] = []
    if send_result is not None:
        for item in getattr(send_result, "items", []) or []:
            rid = getattr(item, "req_id", None)
            if rid:
                req_ids.append(str(rid))
    extra = getattr(pending, "extra", None) or {}
    snap = get_signal_book(pending.intent_id)
    if snap is None and extra.get("signal_bybit_bid") is not None:
        snap = SignalBookSnapshot(
            bybit_bid=_finite(extra.get("signal_bybit_bid")),
            bybit_ask=_finite(extra.get("signal_bybit_ask")),
            okx_bid=_finite(extra.get("signal_okx_bid")),
            okx_ask=_finite(extra.get("signal_okx_ask")),
            spread_long_pct=_finite(extra.get("signal_spread_long_pct")),
            spread_short_pct=_finite(extra.get("signal_spread_short_pct")),
        )
    return ChronometryContext(
        intent_id=str(pending.intent_id),
        base_coin=str(pending.base_coin),
        spread_side=str(pending.spread_side),
        phase=phase,
        signal_ts_ms=int(pending.signal_ts_ms),
        data_root=Path(data_root),
        open_spread_side=getattr(pending, "open_spread_side", None),
        dual_leg_id=str(pending.intent_id).replace("-", "")[:32],
        req_ids=tuple(req_ids),
        signal_book=snap,
        send_result=send_result,
        ack_result=ack_result,
        wire_events=wire_events,
        ticks=ticks,
        anchor_wall_ms=anchor_wall_ms,
        anchor_mono_ns=anchor_mono_ns,
        lookback_ms=resolve_lookback_ms(env),
        lookahead_ms=resolve_lookahead_ms(env),
    )


def _emit_now(ctx: ChronometryContext, logger: LogFn) -> dict[str, Path]:
    if ctx.wire_events is None:
        ctx.wire_events = collect_wire_events(
            data_root=ctx.data_root,
            intent_id=ctx.intent_id,
            dual_leg_id=ctx.dual_leg_id,
            req_ids=ctx.req_ids,
        )
    artifact = build_chronometry_artifact(ctx)
    paths = write_chronometry_files(artifact, data_root=ctx.data_root)
    logger(
        "chronometry_wrote | "
        f"intent_id={ctx.intent_id} | coin={ctx.base_coin} | "
        f"ticks={artifact['tick_count']} | "
        f"signal_spread={artifact['signal_spread_pct']} | "
        f"fill_spread={artifact['fill_spread_pct']} | "
        f"html={paths['html']}"
    )
    return paths


def _emit_after_fill_wait(ctx: ChronometryContext, wait_sec: float, logger: LogFn) -> None:
    deadline = time.monotonic() + float(wait_sec)
    try:
        while time.monotonic() < deadline:
            ctx.wire_events = collect_wire_events(
                data_root=ctx.data_root,
                intent_id=ctx.intent_id,
                dual_leg_id=ctx.dual_leg_id,
                req_ids=ctx.req_ids,
            )
            fills = fill_prices_from_markers(
                markers_from_send_ack(
                    signal_ts_ms=ctx.signal_ts_ms,
                    send_result=ctx.send_result,
                    ack_result=ctx.ack_result,
                    wire_events=ctx.wire_events,
                    anchor_wall_ms=ctx.anchor_wall_ms,
                    anchor_mono_ns=ctx.anchor_mono_ns,
                )
            )
            if fills.get("bybit") is not None and fills.get("okx") is not None:
                break
            time.sleep(0.25)
        _emit_now(ctx, logger)
    except Exception as exc:  # noqa: BLE001
        logger(
            f"chronometry_failed | intent_id={ctx.intent_id} | err={type(exc).__name__}"
        )

"""Append-only terminal LEG journal (bbot.journal.v0)."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Set, Tuple

from app.bot.paths import legs_jsonl_path, resolve_data_root

SCHEMA_VERSION = "bbot.journal.v0"
FEE_RATE = 0.00075
ALLOWED_STATUS = frozenset({"filled", "aborted"})
ALLOWED_EXCHANGE = frozenset({"okx", "bybit"})
ALLOWED_LEG_SIDE = frozenset({"buy", "sell"})
ALLOWED_SPREAD_SIDE = frozenset({"open_long", "open_short", "close"})

LegKey = Tuple[str, str]  # (intent_id, exchange)


class JournalDuplicateError(RuntimeError):
    """Raised when a dual-leg write would complete or duplicate a prior intent/exchange."""


def event_date_utc_from_signal_ts_ms(signal_ts_ms: int | float) -> str:
    """UTC calendar date of signal_ts_ms (host-local timezone forbidden)."""
    return datetime.utcfromtimestamp(float(signal_ts_ms) / 1000.0).date().isoformat()


def load_existing_leg_keys(path: Path) -> Set[LegKey]:
    """Scan legs.jsonl for ``(intent_id, exchange)`` keys; ignore truncated last line."""
    keys: Set[LegKey] = set()
    if not path.is_file():
        return keys
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return keys
    if not text:
        return keys
    lines = text.split("\n")
    # Drop trailing empty from final newline; last non-empty may be truncated.
    while lines and lines[-1] == "":
        lines.pop()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # Truncated or corrupt line: ignore (especially last line after crash).
            if i == len(lines) - 1:
                continue
            continue
        intent_id = rec.get("intent_id")
        exchange = rec.get("exchange")
        if intent_id is not None and exchange is not None:
            keys.add((str(intent_id), str(exchange)))
    return keys


def build_leg_record(
    *,
    intent_id: str,
    base_coin: str,
    exchange: str,
    leg_side: str,
    spread_side: str,
    signal_ts_ms: int | float,
    place_ts_ms: int | float,
    ack_ts_ms: int | float,
    fill_ts_ms: Optional[int | float],
    trade_lat_ms: int | float,
    signal_price: float,
    fill_price: Optional[float],
    qty: float,
    notional: float,
    status: str,
    abort_reason: Optional[str] = None,
    suppress_reason: Optional[str] = None,
    tick_valid: Optional[bool] = None,
    okx_symbol: Optional[str] = None,
    bybit_symbol: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    if exchange not in ALLOWED_EXCHANGE:
        raise ValueError(f"invalid exchange: {exchange}")
    if leg_side not in ALLOWED_LEG_SIDE:
        raise ValueError(f"invalid leg_side: {leg_side}")
    if spread_side not in ALLOWED_SPREAD_SIDE:
        raise ValueError(f"invalid spread_side: {spread_side}")
    if status not in ALLOWED_STATUS:
        raise ValueError(f"invalid status: {status}")
    if float(place_ts_ms) < float(signal_ts_ms):
        raise ValueError("place_ts_ms must be >= signal_ts_ms")

    event_date = event_date_utc_from_signal_ts_ms(signal_ts_ms)
    fee = float(FEE_RATE) * float(notional)

    if status == "filled":
        if fill_ts_ms is None or fill_price is None:
            raise ValueError("filled requires fill_ts_ms and fill_price")
        if float(fill_ts_ms) < float(signal_ts_ms) + float(trade_lat_ms):
            raise ValueError("fill_ts_ms must be >= signal_ts_ms + Trade_Lat_ms")
        resolved_tick_valid = True if tick_valid is None else bool(tick_valid)
        if not resolved_tick_valid:
            raise ValueError("filled requires tick_valid=true")
    else:
        # aborted
        if fill_ts_ms is not None:
            raise ValueError("aborted requires fill_ts_ms=null")
        fill_price = None
        resolved_tick_valid = False if tick_valid is None else bool(tick_valid)
        if resolved_tick_valid:
            raise ValueError("aborted / null fill requires tick_valid=false")

    rec: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "intent_id": str(intent_id),
        "base_coin": str(base_coin).upper(),
        "exchange": exchange,
        "leg_side": leg_side,
        "spread_side": spread_side,
        "event_date": event_date,
        "signal_ts_ms": int(signal_ts_ms),
        "place_ts_ms": int(place_ts_ms),
        "ack_ts_ms": int(ack_ts_ms),
        "fill_ts_ms": None if fill_ts_ms is None else int(fill_ts_ms),
        "Trade_Lat_ms": int(trade_lat_ms),
        "signal_price": float(signal_price),
        "fill_price": None if fill_price is None else float(fill_price),
        "qty": float(qty),
        "notional": float(notional),
        "fee": float(fee),
        "tick_valid": bool(resolved_tick_valid),
        "suppress_reason": suppress_reason,
        "status": status,
        "abort_reason": abort_reason,
        "would_send": True,
        "send": False,
        "k_live": 1,
    }
    if okx_symbol is not None:
        rec["okx_symbol"] = okx_symbol
    if bybit_symbol is not None:
        rec["bybit_symbol"] = bybit_symbol
    if extra:
        for k, v in extra.items():
            if k in rec:
                continue
            rec[k] = v
    return rec


class JournalWriter:
    """Append complete JSONL terminal legs under BBOT_DATA_ROOT only."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        # Soft guard: refuse obvious D trees
        text = str(self.data_root.resolve())
        for bad in ("/data/live", "/data/bars", "/data/compacted", "/data/spool"):
            if text == bad or text.startswith(bad + os.sep):
                raise RuntimeError(f"JournalWriter refuses D path: {self.data_root}")

    def append_leg(self, record: Mapping[str, Any]) -> Path:
        status = record.get("status")
        if status not in ALLOWED_STATUS:
            raise ValueError(f"legs.jsonl accepts only filled|aborted, got {status!r}")
        event_date = record["event_date"]
        path = legs_jsonl_path(self.data_root, str(event_date))
        line = json.dumps(dict(record), separators=(",", ":"), ensure_ascii=False)
        # Write complete line then flush — never leave truncated JSON as a record.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        return path

    def append_dual_legs(self, leg_okx: Mapping[str, Any], leg_bybit: Mapping[str, Any]) -> list[Path]:
        """Write both complete JSON lines in one open/flush/fsync (crash-safe pair).

        Idempotent: if either ``(intent_id, exchange)`` already exists for this
        event_date, raise ``JournalDuplicateError`` and write nothing — do not
        complete a broken half-pair after a prior sequential-write crash.
        """
        if leg_okx.get("intent_id") != leg_bybit.get("intent_id"):
            raise ValueError("dual legs must share intent_id")
        if float(leg_okx["notional"]) != float(leg_bybit["notional"]):
            raise ValueError("dual legs must share notional")
        if leg_okx.get("exchange") != "okx" or leg_bybit.get("exchange") != "bybit":
            raise ValueError("expected (okx, bybit) leg pair")
        for rec in (leg_okx, leg_bybit):
            if rec.get("status") not in ALLOWED_STATUS:
                raise ValueError(
                    f"legs.jsonl accepts only filled|aborted, got {rec.get('status')!r}"
                )
        if leg_okx.get("event_date") != leg_bybit.get("event_date"):
            raise ValueError("dual legs must share event_date")

        intent_id = str(leg_okx["intent_id"])
        event_date = str(leg_okx["event_date"])
        path = legs_jsonl_path(self.data_root, event_date)
        existing = load_existing_leg_keys(path)
        key_okx = (intent_id, "okx")
        key_bybit = (intent_id, "bybit")
        if key_okx in existing or key_bybit in existing:
            raise JournalDuplicateError(
                f"refuse dual-leg write: intent_id={intent_id!r} already has "
                f"okx={key_okx in existing} bybit={key_bybit in existing}; "
                "operator/restart should abort that intent in state"
            )

        line_okx = json.dumps(dict(leg_okx), separators=(",", ":"), ensure_ascii=False)
        line_bybit = json.dumps(dict(leg_bybit), separators=(",", ":"), ensure_ascii=False)
        # Both complete lines, then one flush/fsync — never orphan one exchange.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line_okx)
            fh.write("\n")
            fh.write(line_bybit)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        return [path, path]


def write_sample_legs_to_temp(
    tmp_root: Path,
    *,
    intent_id: str = "probe-intent-1",
    base_coin: str = "BTC",
    signal_ts_ms: Optional[int] = None,
    trade_lat_ms: int = 100,
    notional: float = 100.0,
) -> Path:
    """Optional local helper: journal two filled legs without WS.

    Writes under ``tmp_root`` (treated as BBOT_DATA_ROOT). Returns legs.jsonl path.
    """
    from time import time

    sig = int(signal_ts_ms if signal_ts_ms is not None else time() * 1000)
    place = sig
    fill = sig + int(trade_lat_ms)
    writer = JournalWriter(tmp_root)
    common = dict(
        intent_id=intent_id,
        base_coin=base_coin,
        spread_side="open_long",
        signal_ts_ms=sig,
        place_ts_ms=place,
        ack_ts_ms=place,
        fill_ts_ms=fill,
        trade_lat_ms=trade_lat_ms,
        notional=notional,
        status="filled",
        tick_valid=True,
        okx_symbol="BTC-USDT-SWAP",
        bybit_symbol="BTCUSDT",
    )
    okx = build_leg_record(
        exchange="okx",
        leg_side="buy",
        signal_price=50000.0,
        fill_price=50001.0,
        qty=0.002,
        **common,
    )
    bybit = build_leg_record(
        exchange="bybit",
        leg_side="sell",
        signal_price=50010.0,
        fill_price=50009.0,
        qty=0.002,
        **common,
    )
    writer.append_dual_legs(okx, bybit)
    return legs_jsonl_path(tmp_root, event_date_utc_from_signal_ts_ms(sig))


def default_writer_from_env() -> JournalWriter:
    return JournalWriter(resolve_data_root())


def _self_test_duplicate_append() -> None:
    """Calling append_dual_legs twice with same intent_id must not duplicate."""
    with tempfile.TemporaryDirectory(prefix="bbot-journal-") as tmp:
        root = Path(tmp)
        path = write_sample_legs_to_temp(root, intent_id="dup-intent-1", signal_ts_ms=1_700_000_000_000)
        text1 = path.read_text(encoding="utf-8")
        n1 = sum(1 for ln in text1.splitlines() if ln.strip())
        assert n1 == 2, f"expected 2 lines after first write, got {n1}"

        writer = JournalWriter(root)
        # Rebuild same pair and attempt second write.
        okx = build_leg_record(
            intent_id="dup-intent-1",
            base_coin="BTC",
            exchange="okx",
            leg_side="buy",
            spread_side="open_long",
            signal_ts_ms=1_700_000_000_000,
            place_ts_ms=1_700_000_000_000,
            ack_ts_ms=1_700_000_000_000,
            fill_ts_ms=1_700_000_000_100,
            trade_lat_ms=100,
            signal_price=50000.0,
            fill_price=50001.0,
            qty=0.002,
            notional=100.0,
            status="filled",
            tick_valid=True,
        )
        bybit = build_leg_record(
            intent_id="dup-intent-1",
            base_coin="BTC",
            exchange="bybit",
            leg_side="sell",
            spread_side="open_long",
            signal_ts_ms=1_700_000_000_000,
            place_ts_ms=1_700_000_000_000,
            ack_ts_ms=1_700_000_000_000,
            fill_ts_ms=1_700_000_000_100,
            trade_lat_ms=100,
            signal_price=50010.0,
            fill_price=50009.0,
            qty=0.002,
            notional=100.0,
            status="filled",
            tick_valid=True,
        )
        raised = False
        try:
            writer.append_dual_legs(okx, bybit)
        except JournalDuplicateError:
            raised = True
        assert raised, "second append_dual_legs must raise JournalDuplicateError"
        text2 = path.read_text(encoding="utf-8")
        n2 = sum(1 for ln in text2.splitlines() if ln.strip())
        assert n2 == 2, f"expected still 2 lines after refused duplicate, got {n2}"
        assert text2 == text1, "file content must be unchanged after refused duplicate"
    print("journal_duplicate_append_ok", flush=True)


if __name__ == "__main__":
    _self_test_duplicate_append()

"""Gear-2 dual-leg trade chronology: journal markers + nearby public L1 ticks.

Operator tool for inspecting signal vs place/ack/fill timing against collector
L1. Does not send orders, touch live units, or invent ``live_broker.py``.

Typical VPS dumps (evidence, not gospel)::

    /data/bbot-gear2/journal/event_date=*/legs.jsonl
    /data/bbot-gear2/private/journal/event_date=*/events.jsonl
    /data/live/base_coin=<COIN>/event_date=*/…parquet   # lean hive
    /data/compacted/spread_*.parquet                    # compacted lean

Point helpers at a local ``data_root`` (and optional ``ticks_root``) so a copied
dump works off-VPS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import pandas as pd
import pyarrow.parquet as pq

# Default neighborhood around the trade (signal − pad … dual_fill + pad).
DEFAULT_PAD_MS = 3_000

MARKER_KINDS = (
    "signal",
    "place",
    "ack",
    "fill",
    "dual_fill_complete",
    "private_prepared",
    "private_sent",
    "private_ack",
    "private_terminal",
)

# Public legs.jsonl timestamp fields we understand (optional extras allowed).
_PUBLIC_TS_FIELDS = (
    "signal_ts_ms",
    "place_ts_ms",
    "send_ts_ms",
    "ack_ts_ms",
    "fill_ts_ms",
)

_PRIVATE_EVENT_TO_KIND = {
    "order_prepared": "private_prepared",
    "request_sent": "private_sent",
    "ack_received": "private_ack",
    "terminal_update": "private_terminal",
}


@dataclass(frozen=True)
class ChronologyMarker:
    """One labeled vertical marker on the chronology axis (Unix ms)."""

    kind: str
    ts_ms: int
    exchange: Optional[str] = None
    label: str = ""
    source: str = "public_legs"  # public_legs | private_events | derived
    fill_source: Optional[str] = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def display_label(self) -> str:
        parts = [self.label or self.kind]
        if self.exchange:
            parts.append(f"[{self.exchange}]")
        if self.fill_source:
            parts.append(f"fill_source={self.fill_source}")
        return " ".join(parts)


@dataclass
class DualLegIntent:
    """Paired public legs for one intent_id (+ optional private events)."""

    intent_id: str
    legs: list[dict[str, Any]]
    private_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def base_coin(self) -> str:
        for leg in self.legs:
            coin = leg.get("base_coin")
            if coin:
                return str(coin).upper()
        return "?"

    @property
    def spread_side(self) -> Optional[str]:
        for leg in self.legs:
            if leg.get("spread_side"):
                return str(leg["spread_side"])
        return None

    @property
    def qty(self) -> Optional[float]:
        for leg in self.legs:
            if leg.get("qty") is not None:
                return float(leg["qty"])
        return None

    def leg_for(self, exchange: str) -> Optional[dict[str, Any]]:
        ex = exchange.lower()
        for leg in self.legs:
            if str(leg.get("exchange", "")).lower() == ex:
                return leg
        return None

    def signal_ts_ms(self) -> Optional[int]:
        vals = [_as_int_ms(leg.get("signal_ts_ms")) for leg in self.legs]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None

    def fill_ts_ms_by_exchange(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for leg in self.legs:
            ex = str(leg.get("exchange", "")).lower()
            ts = _as_int_ms(leg.get("fill_ts_ms"))
            if ex and ts is not None:
                out[ex] = ts
        return out

    def dual_fill_complete_ms(self) -> Optional[int]:
        """Later of the two leg fills when both are present."""
        fills = self.fill_ts_ms_by_exchange()
        if len(fills) < 2:
            # Still useful: return the only fill if one leg filled.
            return max(fills.values()) if fills else None
        return max(fills.values())


def _as_int_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ms_to_utc_label(ts_ms: int) -> str:
    return (
        datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_rfc3339_z_to_ms(value: str) -> int:
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def resolve_bbot_layout(data_root: Path) -> dict[str, Path]:
    """Map a dump root to journal / private journal paths.

    Accepts either::

        data_root/journal/...
        data_root/private/journal/...

    or a private-only root::

        data_root/journal/event_date=*/events.jsonl
    """
    root = Path(data_root)
    public_journal = root / "journal"
    private_journal = root / "private" / "journal"
    if not private_journal.is_dir() and (root / "journal").is_dir():
        # Allow pointing data_root at /data/bbot-gear2/private directly.
        sample = list((root / "journal").glob("event_date=*/events.jsonl"))
        if sample and not list((root / "journal").glob("event_date=*/legs.jsonl")):
            private_journal = root / "journal"
            public_journal = root.parent / "journal" if root.name == "private" else public_journal
    return {
        "data_root": root,
        "public_journal": public_journal,
        "private_journal": private_journal,
    }


def iter_jsonl_records(path: Path, *, skip_truncated: bool = True) -> list[dict[str, Any]]:
    """Read JSONL objects; optionally skip a truncated final line."""
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    if not text:
        return []
    lines = text.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    records: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if skip_truncated and i == len(lines) - 1:
                continue
            raise
        if isinstance(obj, dict):
            records.append(obj)
    return records


def list_legs_jsonl(data_root: Path) -> list[Path]:
    layout = resolve_bbot_layout(data_root)
    journal = layout["public_journal"]
    if not journal.is_dir():
        return []
    return sorted(journal.glob("event_date=*/legs.jsonl"))


def list_private_events_jsonl(data_root: Path) -> list[Path]:
    layout = resolve_bbot_layout(data_root)
    journal = layout["private_journal"]
    if not journal.is_dir():
        return []
    return sorted(journal.glob("event_date=*/events.jsonl"))


def load_all_legs(data_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in list_legs_jsonl(data_root):
        rows.extend(iter_jsonl_records(path))
    return rows


def load_all_private_events(data_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in list_private_events_jsonl(data_root):
        rows.extend(iter_jsonl_records(path))
    return rows


def group_legs_by_intent(legs: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for leg in legs:
        intent = leg.get("intent_id")
        if intent is None:
            continue
        by.setdefault(str(intent), []).append(dict(leg))
    return by


def list_intent_ids(data_root: Path) -> list[str]:
    return sorted(group_legs_by_intent(load_all_legs(data_root)).keys())


def load_intent(
    data_root: Path,
    intent_id: str,
    *,
    private_dual_leg_id: Optional[str] = None,
) -> DualLegIntent:
    """Load both public legs for ``intent_id`` and optional private events.

    Private rows are matched by ``dual_leg_id`` equal to ``private_dual_leg_id``
    or, by default, ``intent_id`` (when journals share the same opaque id).
    """
    by = group_legs_by_intent(load_all_legs(data_root))
    if intent_id not in by:
        raise KeyError(f"intent_id not found in legs.jsonl under {data_root}: {intent_id}")
    legs = by[intent_id]
    dual_key = private_dual_leg_id or intent_id
    private: list[dict[str, Any]] = []
    for ev in load_all_private_events(data_root):
        if str(ev.get("dual_leg_id") or "") == dual_key:
            private.append(ev)
    return DualLegIntent(intent_id=intent_id, legs=legs, private_events=private)


def fill_source_for_leg(leg: Mapping[str, Any]) -> Optional[str]:
    """Return journal ``fill_source`` when present; never invent a venue fill."""
    raw = leg.get("fill_source")
    if raw is None:
        return None
    return str(raw)


def build_markers(intent: DualLegIntent) -> list[ChronologyMarker]:
    """Build chronological markers from public legs (+ optional private events)."""
    markers: list[ChronologyMarker] = []

    for leg in intent.legs:
        ex = str(leg.get("exchange", "")).lower() or None
        fs = fill_source_for_leg(leg)

        sig = _as_int_ms(leg.get("signal_ts_ms"))
        if sig is not None:
            markers.append(
                ChronologyMarker(
                    kind="signal",
                    ts_ms=sig,
                    exchange=ex,
                    label="signal",
                    fill_source=None,
                    extra={"spread_side": leg.get("spread_side"), "qty": leg.get("qty")},
                )
            )

        # Prefer explicit place_ts_ms; accept send_ts_ms as place alias when present.
        place = _as_int_ms(leg.get("place_ts_ms"))
        send = _as_int_ms(leg.get("send_ts_ms"))
        place_eff = place if place is not None else send
        if place_eff is not None:
            markers.append(
                ChronologyMarker(
                    kind="place",
                    ts_ms=place_eff,
                    exchange=ex,
                    label="place" if place is not None else "send/place",
                    extra={"send_ts_ms": send, "place_ts_ms": place},
                )
            )

        ack = _as_int_ms(leg.get("ack_ts_ms"))
        if ack is not None:
            markers.append(
                ChronologyMarker(
                    kind="ack",
                    ts_ms=ack,
                    exchange=ex,
                    label="ack",
                )
            )

        fill = _as_int_ms(leg.get("fill_ts_ms"))
        if fill is not None:
            markers.append(
                ChronologyMarker(
                    kind="fill",
                    ts_ms=fill,
                    exchange=ex,
                    label="fill",
                    fill_source=fs,
                    extra={
                        "fill_price": leg.get("fill_price"),
                        "Trade_Lat_ms": leg.get("Trade_Lat_ms"),
                        "okx_latency_ms": leg.get("okx_latency_ms"),
                        "bybit_latency_ms": leg.get("bybit_latency_ms"),
                    },
                )
            )

    dual = intent.dual_fill_complete_ms()
    if dual is not None and len(intent.fill_ts_ms_by_exchange()) >= 2:
        markers.append(
            ChronologyMarker(
                kind="dual_fill_complete",
                ts_ms=dual,
                exchange=None,
                label="dual_fill_complete",
                source="derived",
            )
        )

    for ev in intent.private_events:
        et = str(ev.get("event_type") or "")
        kind = _PRIVATE_EVENT_TO_KIND.get(et)
        if kind is None:
            continue
        ts = None
        if ev.get("event_ts_utc"):
            try:
                ts = parse_rfc3339_z_to_ms(str(ev["event_ts_utc"]))
            except (TypeError, ValueError):
                ts = None
        if ts is None:
            continue
        markers.append(
            ChronologyMarker(
                kind=kind,
                ts_ms=ts,
                exchange=str(ev.get("venue") or "").lower() or None,
                label=et,
                source="private_events",
                extra={
                    "observation_source": ev.get("observation_source"),
                    "terminal_state": ev.get("terminal_state"),
                    "leg_id": ev.get("leg_id"),
                },
            )
        )

    markers.sort(key=lambda m: (m.ts_ms, m.kind, m.exchange or ""))
    return markers


def neighborhood_window_ms(
    intent: DualLegIntent,
    *,
    pad_ms: int = DEFAULT_PAD_MS,
) -> tuple[int, int]:
    """Return [start_ms, end_ms) covering signal/fill ± pad."""
    anchors: list[int] = []
    sig = intent.signal_ts_ms()
    if sig is not None:
        anchors.append(sig)
    dual = intent.dual_fill_complete_ms()
    if dual is not None:
        anchors.append(dual)
    for leg in intent.legs:
        for key in _PUBLIC_TS_FIELDS:
            v = _as_int_ms(leg.get(key))
            if v is not None:
                anchors.append(v)
    if not anchors:
        raise ValueError(f"intent {intent.intent_id} has no timestamp anchors")
    start = min(anchors) - int(pad_ms)
    end = max(anchors) + int(pad_ms) + 1
    return start, end


def _detect_ticks_layout(ticks_root: Path) -> str:
    root = Path(ticks_root)
    if not root.exists():
        raise FileNotFoundError(f"ticks_root does not exist: {root}")
    if list(root.glob("spread_*.parquet")) or list(root.glob("**/spread_*.parquet")):
        return "compacted"
    if list(root.glob("base_coin=*")):
        return "hive"
    if list(root.glob("*.jsonl")) or (root / "ticks.jsonl").is_file():
        return "jsonl"
    # Nested hive under coin-only dump: /data/live/base_coin=SOL
    if root.name.startswith("base_coin=") or list(root.glob("event_date=*")):
        return "hive"
    raise FileNotFoundError(
        f"no lean ticks under {root} (expected spread_*.parquet, "
        f"base_coin=*/event_date=*/*.parquet, or ticks.jsonl)"
    )


def _list_compacted_files(ticks_root: Path, start_ms: int, end_ms: int) -> list[Path]:
    files: list[Path] = []
    for p in sorted(Path(ticks_root).rglob("spread_*.parquet")):
        win = _parse_lean_file_window(p)
        if win is None:
            files.append(p)
            continue
        a, b = win
        if a < end_ms and b > start_ms:
            files.append(p)
    return files


def _parse_lean_file_window(path: Path) -> Optional[tuple[int, int]]:
    name = path.name
    if not (name.startswith("spread_") and name.endswith(".parquet")):
        return None
    stem = name[len("spread_") : -len(".parquet")]
    parts = stem.split("_")
    if len(parts) != 2:
        return None
    try:
        a = datetime.strptime(parts[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        b = datetime.strptime(parts[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(a.timestamp() * 1000), int(b.timestamp() * 1000)


def _list_hive_files(
    ticks_root: Path,
    *,
    coin: Optional[str],
    start_ms: int,
    end_ms: int,
) -> list[Path]:
    root = Path(ticks_root)
    dates = set()
    # Include partition dates covering the window (UTC calendar).
    t = start_ms
    while t < end_ms:
        dates.add(
            datetime.fromtimestamp(t / 1000.0, tz=timezone.utc).date().isoformat()
        )
        t += 86_400_000
    dates.add(
        datetime.fromtimestamp((end_ms - 1) / 1000.0, tz=timezone.utc).date().isoformat()
    )

    files: list[Path] = []
    coin_u = coin.upper() if coin else None

    def _scan(base: Path) -> None:
        if not base.is_dir():
            return
        for day in dates:
            day_dir = base / f"event_date={day}"
            if day_dir.is_dir():
                files.extend(sorted(day_dir.glob("*.parquet")))

    if root.name.startswith("base_coin="):
        if coin_u and root.name.split("=", 1)[1].upper() != coin_u:
            return []
        _scan(root)
    elif coin_u:
        _scan(root / f"base_coin={coin_u}")
        # Also accept lowercase coin folder if present.
        _scan(root / f"base_coin={coin_u.lower()}")
    else:
        for child in sorted(root.glob("base_coin=*")):
            _scan(child)
    # Deduplicate while preserving order.
    seen: set[Path] = set()
    out: list[Path] = []
    for p in files:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _read_parquet_rows(
    paths: Sequence[Path],
    *,
    start_ms: int,
    end_ms: int,
    coin: Optional[str],
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    coin_u = coin.upper() if coin else None
    wanted = [
        "event_local_ts_ms",
        "base_coin",
        "trigger",
        "okx_bid_price",
        "okx_ask_price",
        "bybit_bid_price",
        "bybit_ask_price",
        "okx_ts_ms",
        "bybit_ts_ms",
        "okx_local_recv_ts_ms",
        "bybit_local_recv_ts_ms",
        "calc_local_ts_ms",
    ]
    for path in paths:
        try:
            names = set(pq.read_schema(path).names)
            cols = [c for c in wanted if c in names]
            if "event_local_ts_ms" not in cols:
                continue
            table = pq.read_table(path, columns=cols)
        except Exception:
            continue
        if table.num_rows == 0:
            continue
        df = table.to_pandas()
        df["event_local_ts_ms"] = pd.to_numeric(df["event_local_ts_ms"], errors="coerce")
        df = df.dropna(subset=["event_local_ts_ms"])
        df["event_local_ts_ms"] = df["event_local_ts_ms"].round().astype("int64")
        df = df[(df["event_local_ts_ms"] >= start_ms) & (df["event_local_ts_ms"] < end_ms)]
        if coin_u and "base_coin" in df.columns:
            df = df[df["base_coin"].astype(str).str.upper() == coin_u]
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=wanted)
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values("event_local_ts_ms").reset_index(drop=True)
    return out


def load_ticks_jsonl(
    path: Path,
    *,
    start_ms: int,
    end_ms: int,
    coin: Optional[str] = None,
) -> pd.DataFrame:
    rows = iter_jsonl_records(path)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "event_local_ts_ms" not in df.columns:
        raise KeyError(f"ticks jsonl missing event_local_ts_ms: {path}")
    df["event_local_ts_ms"] = pd.to_numeric(df["event_local_ts_ms"], errors="coerce")
    df = df.dropna(subset=["event_local_ts_ms"])
    df["event_local_ts_ms"] = df["event_local_ts_ms"].round().astype("int64")
    df = df[(df["event_local_ts_ms"] >= start_ms) & (df["event_local_ts_ms"] < end_ms)]
    if coin and "base_coin" in df.columns:
        df = df[df["base_coin"].astype(str).str.upper() == coin.upper()]
    return df.sort_values("event_local_ts_ms").reset_index(drop=True)


def load_nearby_ticks(
    ticks_root: Path,
    *,
    start_ms: int,
    end_ms: int,
    coin: Optional[str] = None,
) -> pd.DataFrame:
    """Load lean L1 in [start_ms, end_ms) for one coin from hive / compacted / jsonl."""
    root = Path(ticks_root)
    layout = _detect_ticks_layout(root)
    if layout == "jsonl":
        candidates = []
        direct = root / "ticks.jsonl"
        if direct.is_file():
            candidates.append(direct)
        candidates.extend(sorted(root.glob("*.jsonl")))
        frames = [
            load_ticks_jsonl(p, start_ms=start_ms, end_ms=end_ms, coin=coin)
            for p in candidates
        ]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame()
        return (
            pd.concat(frames, ignore_index=True)
            .sort_values("event_local_ts_ms")
            .drop_duplicates()
            .reset_index(drop=True)
        )
    if layout == "compacted":
        files = _list_compacted_files(root, start_ms, end_ms)
    else:
        files = _list_hive_files(root, coin=coin, start_ms=start_ms, end_ms=end_ms)
    return _read_parquet_rows(files, start_ms=start_ms, end_ms=end_ms, coin=coin)


def derive_tick_series(df: pd.DataFrame) -> pd.DataFrame:
    """Add mid prices / spreads used for chronology panels (read-time only)."""
    if df.empty:
        return df.copy()
    out = df.copy()
    for c in (
        "okx_bid_price",
        "okx_ask_price",
        "bybit_bid_price",
        "bybit_ask_price",
    ):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if {"okx_bid_price", "okx_ask_price"} <= set(out.columns):
        out["okx_mid"] = (out["okx_bid_price"] + out["okx_ask_price"]) / 2.0
    if {"bybit_bid_price", "bybit_ask_price"} <= set(out.columns):
        out["bybit_mid"] = (out["bybit_bid_price"] + out["bybit_ask_price"]) / 2.0
    if {
        "bybit_bid_price",
        "okx_ask_price",
    } <= set(out.columns):
        out["spread_long"] = (
            (out["bybit_bid_price"] - out["okx_ask_price"]) / out["bybit_bid_price"] * 100.0
        )
    if {
        "okx_bid_price",
        "bybit_ask_price",
    } <= set(out.columns):
        out["spread_short"] = (
            (out["okx_bid_price"] - out["bybit_ask_price"]) / out["okx_bid_price"] * 100.0
        )
    out["event_dt"] = pd.to_datetime(out["event_local_ts_ms"], unit="ms", utc=True)
    return out


@dataclass
class ChronologyPlotData:
    """Hermetic plot payload: tick series + markers (no figure required)."""

    intent_id: str
    base_coin: str
    spread_side: Optional[str]
    qty: Optional[float]
    window_start_ms: int
    window_end_ms: int
    ticks: pd.DataFrame
    markers: list[ChronologyMarker]
    dual_fill_complete_ms: Optional[int]
    offset_ms: int = 0

    def marker_ts_by_kind(
        self,
        kind: str,
        *,
        exchange: Optional[str] = None,
    ) -> list[int]:
        out: list[int] = []
        for m in self.markers:
            if m.kind != kind:
                continue
            if exchange is not None and (m.exchange or "") != exchange.lower():
                continue
            out.append(m.ts_ms)
        return out

    def axis_ts_ms(self, ts_ms: int) -> int:
        return int(ts_ms) - int(self.offset_ms)


def build_chronology(
    data_root: Path,
    intent_id: str,
    *,
    ticks_root: Optional[Path] = None,
    pad_ms: int = DEFAULT_PAD_MS,
    offset_ms: int = 0,
    private_dual_leg_id: Optional[str] = None,
) -> ChronologyPlotData:
    """Entry point: ``data_root`` + ``intent_id`` → ticks neighborhood + markers.

    ``ticks_root`` defaults to ``data_root / "ticks"`` (fixture layout) so a
    self-contained dump works; for VPS point it at ``/data/live`` or compacted.
    """
    intent = load_intent(
        Path(data_root),
        intent_id,
        private_dual_leg_id=private_dual_leg_id,
    )
    start_ms, end_ms = neighborhood_window_ms(intent, pad_ms=pad_ms)
    markers = build_markers(intent)
    t_root = Path(ticks_root) if ticks_root is not None else Path(data_root) / "ticks"
    ticks = pd.DataFrame()
    if t_root.exists():
        ticks = load_nearby_ticks(
            t_root,
            start_ms=start_ms,
            end_ms=end_ms,
            coin=intent.base_coin,
        )
        ticks = derive_tick_series(ticks)
    return ChronologyPlotData(
        intent_id=intent.intent_id,
        base_coin=intent.base_coin,
        spread_side=intent.spread_side,
        qty=intent.qty,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
        ticks=ticks,
        markers=markers,
        dual_fill_complete_ms=intent.dual_fill_complete_ms(),
        offset_ms=int(offset_ms),
    )


_MARKER_COLORS = {
    "signal": "#c0392b",
    "place": "#8e44ad",
    "ack": "#2980b9",
    "fill": "#27ae60",
    "dual_fill_complete": "#16a085",
    "private_prepared": "#7f8c8d",
    "private_sent": "#9b59b6",
    "private_ack": "#3498db",
    "private_terminal": "#2ecc71",
}


def make_chronology_figure(plot: ChronologyPlotData):
    """Build a Plotly figure: spread + per-venue mid with vertical markers."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        subplot_titles=(
            f"{plot.base_coin} spread_long / spread_short (%)",
            "OKX L1 mid",
            "Bybit L1 mid",
        ),
        row_heights=[0.4, 0.3, 0.3],
    )

    ticks = plot.ticks
    if not ticks.empty and "event_dt" in ticks.columns:
        x = ticks["event_dt"]
        if "spread_long" in ticks.columns:
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=ticks["spread_long"],
                    mode="lines",
                    name="spread_long",
                    line=dict(color="#1f77b4", width=1),
                ),
                row=1,
                col=1,
            )
        if "spread_short" in ticks.columns:
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=ticks["spread_short"],
                    mode="lines",
                    name="spread_short",
                    line=dict(color="#ff7f0e", width=1),
                ),
                row=1,
                col=1,
            )
        if "okx_mid" in ticks.columns:
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=ticks["okx_mid"],
                    mode="lines",
                    name="okx_mid",
                    line=dict(color="#2ca02c", width=1),
                ),
                row=2,
                col=1,
            )
        if "bybit_mid" in ticks.columns:
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=ticks["bybit_mid"],
                    mode="lines",
                    name="bybit_mid",
                    line=dict(color="#9467bd", width=1),
                ),
                row=3,
                col=1,
            )

    # Deduplicate legend entries for repeated kinds across exchanges.
    seen_legend: set[str] = set()
    for m in plot.markers:
        color = _MARKER_COLORS.get(m.kind, "#333333")
        ts_axis = plot.axis_ts_ms(m.ts_ms)
        xval = datetime.fromtimestamp(ts_axis / 1000.0, tz=timezone.utc)
        legend = m.display_label()
        show = legend not in seen_legend
        seen_legend.add(legend)
        for row in (1, 2, 3):
            fig.add_vline(
                x=xval,
                line_width=1.5 if m.kind in {"signal", "fill", "dual_fill_complete"} else 1,
                line_dash="dash" if m.kind.startswith("private_") else "solid",
                line_color=color,
                row=row,
                col=1,
            )
        # Invisible scatter for legend / hover only (top panel).
        fig.add_trace(
            go.Scatter(
                x=[xval],
                y=[None],
                mode="markers",
                name=legend,
                marker=dict(color=color, size=10, symbol="line-ns-open"),
                hovertemplate=(
                    f"{legend}<br>ts_ms={m.ts_ms}<br>"
                    f"utc={ms_to_utc_label(m.ts_ms)}<extra></extra>"
                ),
                showlegend=show,
            ),
            row=1,
            col=1,
        )

    fill_note = ""
    for m in plot.markers:
        if m.kind == "fill" and m.fill_source:
            fill_note = (
                f" | fill_source={m.fill_source} "
                "(label from journal — not assumed venue-reported)"
            )
            break

    title = (
        f"intent={plot.intent_id}  coin={plot.base_coin}  "
        f"side={plot.spread_side}  qty={plot.qty}"
        f"{fill_note}"
    )
    if plot.dual_fill_complete_ms is not None:
        sig = next((m.ts_ms for m in plot.markers if m.kind == "signal"), None)
        if sig is not None:
            title += (
                f" | signal→dual_fill={plot.dual_fill_complete_ms - sig} ms"
            )

    fig.update_layout(
        title=title,
        height=800,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        margin=dict(l=60, r=30, t=80, b=40),
    )
    fig.update_xaxes(title_text="UTC" if plot.offset_ms == 0 else "UTC (offset applied)", row=3, col=1)
    fig.update_yaxes(title_text="pp", row=1, col=1)
    fig.update_yaxes(title_text="price", row=2, col=1)
    fig.update_yaxes(title_text="price", row=3, col=1)
    return fig


def summarize_intent(plot: ChronologyPlotData) -> dict[str, Any]:
    """Compact dict for notebook / tests."""
    by_ex = {}
    for m in plot.markers:
        if m.kind != "fill":
            continue
        by_ex[m.exchange or "?"] = {
            "fill_ts_ms": m.ts_ms,
            "fill_source": m.fill_source,
            "utc": ms_to_utc_label(m.ts_ms),
        }
    sig = plot.marker_ts_by_kind("signal")
    return {
        "intent_id": plot.intent_id,
        "base_coin": plot.base_coin,
        "spread_side": plot.spread_side,
        "qty": plot.qty,
        "window_ms": [plot.window_start_ms, plot.window_end_ms],
        "signal_ts_ms": sig[0] if sig else None,
        "dual_fill_complete_ms": plot.dual_fill_complete_ms,
        "fills": by_ex,
        "n_ticks": int(len(plot.ticks)),
        "n_markers": len(plot.markers),
        "marker_kinds": sorted({m.kind for m in plot.markers}),
    }


FIXTURE_INTENT_ID = "intent-fixture-sol-001"
FIXTURE_SIGNAL_TS_MS = 1_725_000_000_000
FIXTURE_DIRNAME = "gear2_trade_chronology"


def fixture_root(repo_root: Optional[Path] = None) -> Path:
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    return root / "research" / "fixtures" / FIXTURE_DIRNAME

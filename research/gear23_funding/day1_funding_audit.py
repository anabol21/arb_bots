"""Day-1 Gear 2.3 funding ledger.

Re-scores the journalled Bybit–OKX round trips against a read-only account
pull of settlement and trade cashflows. Does not place orders and does not
treat a missing journal fee as zero.

Execution of the private pull is recorded in MANIFEST.json. This script only
reads local files under research/gear23_funding/data/.
"""

from __future__ import annotations

import csv
import json
import math
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TABLES = ROOT / "tables"
FIGURES = ROOT / "figures"
LEDGER = DATA / "ledger_export.json"
CASH = DATA / "account_cashflows.jsonl"
OKX_FEES = DATA / "okx_trade_fees.jsonl"
PUBLIC = DATA / "public_funding_rates.json"

# Preset entry filter. Not tuned on this sample.
NEAR_SETTLEMENT_MS = 30 * 60 * 1000
FEE_MODEL_PP = 0.30
MIN_PROFIT_PP = 0.20
MATCH_MS = 30_000
SIZE_REL = 0.02


def utc(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fnum(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def load_json(path: Path):
    return json.loads(path.read_text())


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


@dataclass
class LegFill:
    ts: float
    px: float
    qty: float
    intent_id: str
    ctx: dict | None


@dataclass
class RoundTrip:
    coin: str
    side: str
    open: LegFill
    close: LegFill
    ct_val: float
    ct_source: str
    bybit_qty_open: float
    bybit_qty_close: float
    okx_qty_open: float
    okx_qty_close: float
    price_usdt: float
    entry_notional: float
    funding_rows: list[dict] = field(default_factory=list)
    fee_usdt: float | None = None
    fee_legs: int = 0
    fee_gaps: list[str] = field(default_factory=list)

    @property
    def open_ts(self) -> float:
        return self.open.ts

    @property
    def close_ts(self) -> float:
        return self.close.ts

    @property
    def hold_h(self) -> float:
        return (self.close_ts - self.open_ts) / 3_600_000

    @property
    def bybit_symbol(self) -> str:
        return f"{self.coin}USDT"

    @property
    def okx_symbol(self) -> str:
        return f"{self.coin}-USDT-SWAP"


def ctx_for(rows: list[dict], intent_id: str, event: str) -> dict | None:
    for row in rows:
        if row.get("intent_id") == intent_id and row.get("event") == event and row.get("ctx"):
            return row["ctx"]
    return None


def infer_ct(bybit_qty: float, okx_qty: float) -> tuple[float | None, str]:
    if okx_qty <= 0 or bybit_qty <= 0:
        return None, "missing_qty"
    ratio = bybit_qty / okx_qty
    for candidate in (0.01, 0.1, 1.0, 10.0, 100.0, 1000.0, 10000.0):
        if abs(ratio - candidate) / candidate <= SIZE_REL:
            return candidate, "qty_ratio"
    return None, "qty_ratio_not_standard"


def pair_trades(life: list[dict]) -> tuple[list[RoundTrip], list[dict]]:
    fills = [
        row for row in life
        if row.get("schema_version") == "bbot.synthetic_roll.v1"
        and fnum(row.get("bybit_fill_px")) is not None
        and fnum(row.get("okx_fill_px")) is not None
        and row.get("event") in {"open", "close"}
    ]
    fills.sort(key=lambda row: (fnum(row["fill_ts_ms"]) or 0, row.get("event") != "open"))
    open_by_coin: dict[str, dict] = {}
    trips: list[RoundTrip] = []
    problems: list[dict] = []
    for row in fills:
        coin = str(row.get("base_coin") or "")
        if row["event"] == "open":
            if coin in open_by_coin:
                prev = open_by_coin[coin]
                problems.append({
                    "problem": "open_without_close",
                    "coin": coin,
                    "intent_id": prev.get("intent_id"),
                    "ts": prev.get("fill_ts_ms"),
                })
            open_by_coin[coin] = row
            continue
        opened = open_by_coin.pop(coin, None)
        if opened is None:
            problems.append({"problem": "close_without_open", "coin": coin, "intent_id": row.get("intent_id"), "ts": row.get("fill_ts_ms")})
            continue
        if str(opened.get("side")) != str(row.get("side")):
            problems.append({"problem": "side_mismatch", "coin": coin})
            continue
        open_ctx = ctx_for(life, opened["intent_id"], "open")
        close_ctx = ctx_for(life, row["intent_id"], "close")
        ct = None
        source = "missing"
        if open_ctx and fnum(open_ctx.get("okx_ct_val")):
            ct = float(open_ctx["okx_ct_val"])
            source = "book_at_open"
        elif close_ctx and fnum(close_ctx.get("okx_ct_val")):
            ct = float(close_ctx["okx_ct_val"])
            source = "book_at_close"
        else:
            ct, source = infer_ct(float(opened["bybit_filled_qty"]), float(opened["okx_filled_qty"]))
        if ct is None:
            problems.append({"problem": "ct_val_unknown", "coin": coin, "intent_id": opened.get("intent_id")})
            continue
        bq_o = float(opened["bybit_filled_qty"])
        bq_c = float(row["bybit_filled_qty"])
        oq_o = float(opened["okx_filled_qty"])
        oq_c = float(row["okx_filled_qty"])
        side = str(opened.get("side") or "")
        # Long spread: short Bybit, long OKX. Cash uses each leg's own coin qty.
        if side == "long":
            price = (
                float(opened["bybit_fill_px"]) * bq_o
                - float(opened["okx_fill_px"]) * oq_o * ct
                - float(row["bybit_fill_px"]) * bq_c
                + float(row["okx_fill_px"]) * oq_c * ct
            )
        elif side == "short":
            price = (
                -float(opened["bybit_fill_px"]) * bq_o
                + float(opened["okx_fill_px"]) * oq_o * ct
                + float(row["bybit_fill_px"]) * bq_c
                - float(row["okx_fill_px"]) * oq_c * ct
            )
        else:
            problems.append({"problem": "unknown_side", "coin": coin, "side": side})
            continue
        entry_notional = float(opened["bybit_fill_px"]) * bq_o
        trips.append(RoundTrip(
            coin=coin,
            side=side,
            open=LegFill(float(opened["fill_ts_ms"]), float(opened["bybit_fill_px"]), bq_o, opened["intent_id"], open_ctx),
            close=LegFill(float(row["fill_ts_ms"]), float(row["bybit_fill_px"]), bq_c, row["intent_id"], close_ctx),
            ct_val=ct,
            ct_source=source,
            bybit_qty_open=bq_o,
            bybit_qty_close=bq_c,
            okx_qty_open=oq_o,
            okx_qty_close=oq_c,
            price_usdt=price,
            entry_notional=entry_notional,
        ))
    for coin, row in open_by_coin.items():
        problems.append({"problem": "open_without_close", "coin": coin, "intent_id": row.get("intent_id"), "ts": row.get("fill_ts_ms")})
    trips.sort(key=lambda trip: trip.open_ts)
    return trips, problems


def size_ok(row_size: float, qty: float) -> bool:
    if qty <= 0:
        return False
    return abs(row_size - qty) / qty <= SIZE_REL


def assign_funding(trips: list[RoundTrip], rows: list[dict]) -> list[dict]:
    funding = [row for row in rows if row.get("stream") in {"SETTLEMENT", "FUNDING"}]
    unassigned = []
    for row in funding:
        ts = fnum(row.get("ts_ms"))
        size = abs(fnum(row.get("size")) or 0)
        cash = fnum(row.get("funding"))
        if ts is None or cash is None or size <= 0:
            unassigned.append({**row, "reason": "unreadable"})
            continue
        venue = row["venue"]
        hits = []
        for trip in trips:
            symbol = trip.bybit_symbol if venue == "bybit" else trip.okx_symbol
            if row.get("instrument") != symbol:
                continue
            qty = trip.bybit_qty_open if venue == "bybit" else trip.okx_qty_open
            if not size_ok(size, qty):
                continue
            if trip.open_ts < ts < trip.close_ts:
                hits.append(trip)
        if len(hits) == 1:
            hits[0].funding_rows.append(row)
        elif len(hits) == 0:
            unassigned.append({**row, "reason": "no_unique_open_interval"})
        else:
            unassigned.append({**row, "reason": "multiple_intervals", "n": len(hits)})
    return unassigned


def assign_fees(trips: list[RoundTrip], rows: list[dict]) -> None:
    trades = [row for row in rows if row.get("stream") == "TRADE"]
    used: set[str] = set()
    for trip in trips:
        legs = [
            ("bybit", "open", trip.bybit_symbol, trip.open_ts, trip.bybit_qty_open),
            ("bybit", "close", trip.bybit_symbol, trip.close_ts, trip.bybit_qty_close),
            ("okx", "open", trip.okx_symbol, trip.open_ts, trip.okx_qty_open),
            ("okx", "close", trip.okx_symbol, trip.close_ts, trip.okx_qty_close),
        ]
        total = 0.0
        ok_legs = 0
        for venue, event, symbol, ts, qty in legs:
            cands = []
            for row in trades:
                if row.get("venue") != venue or row.get("instrument") != symbol:
                    continue
                if row.get("id_hash") in used:
                    continue
                rts = fnum(row.get("ts_ms"))
                rsize = abs(fnum(row.get("size")) or 0)
                if rts is None or rsize <= 0 or abs(rts - ts) > MATCH_MS:
                    continue
                cands.append(row)
            exact = [row for row in cands if size_ok(abs(fnum(row.get("size")) or 0), qty)]
            if len(exact) > 1:
                trip.fee_gaps.append(f"{venue}:{event}:ambiguous={len(exact)}")
                continue
            chosen = exact if len(exact) == 1 else []
            if not chosen and cands:
                summed = sum(abs(fnum(row.get("size")) or 0) for row in cands)
                if size_ok(summed, qty):
                    chosen = cands
            if not chosen:
                trip.fee_gaps.append(f"{venue}:{event}:n={len(cands)}")
                continue
            signed_leg = 0.0
            bad = False
            for row in chosen:
                fee = fnum(row.get("fee"))
                if fee is None:
                    bad = True
                    break
                # Bybit fee is an unsigned cost. OKX fee is a signed balance change.
                signed_leg += -abs(fee) if venue == "bybit" else fee
                used.add(row["id_hash"])
            if bad:
                trip.fee_gaps.append(f"{venue}:{event}:no_fee")
                continue
            total += signed_leg
            ok_legs += 1
        trip.fee_legs = ok_legs
        trip.fee_usdt = total if ok_legs == 4 else None


def pp(usdt: float | None, notional: float) -> float | None:
    if usdt is None or notional <= 0:
        return None
    return usdt / notional * 100.0


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "gear23-funding-day1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_public(symbols: list[str], start_ms: int, end_ms: int) -> dict:
    if PUBLIC.exists():
        return load_json(PUBLIC)
    out: dict[str, list[dict]] = {"bybit": [], "okx": []}
    for coin in symbols:
        end = end_ms
        guard = 0
        while end > start_ms and guard < 8:
            guard += 1
            url = (
                "https://api.bybit.com/v5/market/funding/history"
                f"?category=linear&symbol={coin}USDT&startTime={start_ms}&endTime={end}&limit=200"
            )
            page = http_json(url)
            batch = (page.get("result") or {}).get("list") or []
            if not batch:
                break
            for row in batch:
                out["bybit"].append({
                    "coin": coin,
                    "ts_ms": int(row["fundingRateTimestamp"]),
                    "rate": float(row["fundingRate"]),
                })
            oldest = min(int(row["fundingRateTimestamp"]) for row in batch)
            if oldest <= start_ms or len(batch) < 200:
                break
            end = oldest - 1
        after = ""
        guard = 0
        while guard < 8:
            guard += 1
            url = (
                "https://www.okx.com/api/v5/public/funding-rate-history"
                f"?instId={coin}-USDT-SWAP&limit=100"
            )
            if after:
                url += f"&after={after}"
            page = http_json(url)
            batch = page.get("data") or []
            if not batch:
                break
            stop = False
            for row in batch:
                ts = int(row["fundingTime"])
                if ts < start_ms:
                    stop = True
                    continue
                if ts > end_ms:
                    continue
                rate = row.get("realizedRate") or row.get("fundingRate")
                out["okx"].append({"coin": coin, "ts_ms": ts, "rate": float(rate)})
            oldest = min(int(row["fundingTime"]) for row in batch)
            if stop or len(batch) < 100 or oldest <= start_ms:
                break
            after = str(oldest)
    PUBLIC.write_text(json.dumps(out))
    return out


def sign_check(rows: list[dict], public: dict) -> dict:
    """Compare Bybit settlement feeRate with the public funding rate."""
    same = flipped = unmatched = 0
    cash_matches_public_short_receives = 0
    cash_matches_fee_rate_formula = 0
    checked = 0
    for row in rows:
        if row.get("venue") != "bybit" or row.get("stream") != "SETTLEMENT":
            continue
        rate = fnum(row.get("fee_rate"))
        cash = fnum(row.get("funding"))
        qty = abs(fnum(row.get("size")) or 0)
        px = fnum(row.get("trade_price"))
        ts = fnum(row.get("ts_ms"))
        inst = str(row.get("instrument") or "")
        if None in (rate, cash, px, ts) or qty <= 0 or not inst.endswith("USDT"):
            continue
        coin = inst[:-4]
        near = [
            item for item in public["bybit"]
            if item["coin"] == coin and abs(item["ts_ms"] - ts) <= 120_000
        ]
        if not near:
            unmatched += 1
            continue
        public_rate = near[0]["rate"]
        if rate == 0 or public_rate == 0:
            continue
        checked += 1
        if (rate > 0) == (public_rate > 0):
            same += 1
        else:
            flipped += 1
        side_sign = 1.0 if str(row.get("side")).lower() == "buy" else -1.0
        formula = side_sign * qty * px * rate
        if abs(cash - formula) <= 1e-4:
            cash_matches_fee_rate_formula += 1
        # Standard: short receives when public rate > 0.
        short = side_sign < 0
        standard = (qty * px * public_rate) if short else -(qty * px * public_rate)
        if abs(cash - standard) <= max(1e-4, 0.02 * abs(cash) if cash else 1e-4):
            cash_matches_public_short_receives += 1
    return {
        "checked": checked,
        "fee_rate_same_sign_as_public": same,
        "fee_rate_flipped_vs_public": flipped,
        "unmatched_public": unmatched,
        "cash_matches_row_formula": cash_matches_fee_rate_formula,
        "cash_matches_standard_public": cash_matches_public_short_receives,
    }


def last_rate(series: list[dict], coin: str, ts: float) -> tuple[float | None, float | None]:
    prior = [row for row in series if row["coin"] == coin and row["ts_ms"] <= ts]
    if not prior:
        return None, None
    row = max(prior, key=lambda item: item["ts_ms"])
    return row["rate"], row["ts_ms"]


def next_predicted(series: list[dict], coin: str, ts: float) -> float | None:
    prior = sorted(row["ts_ms"] for row in series if row["coin"] == coin and row["ts_ms"] <= ts)
    if len(prior) < 2:
        return None
    gaps = [prior[i] - prior[i - 1] for i in range(1, len(prior))][-3:]
    gaps.sort()
    interval = gaps[len(gaps) // 2]
    return prior[-1] + interval


def expected_next_cash(trip: RoundTrip, bybit_rate: float | None, okx_rate: float | None) -> float | None:
    """One upcoming settlement from the last public rates.

    Sign is the usual linear-perp convention, checked against Bybit's own
    settlement rows in ``sign_check``: positive rate means longs pay.
    Long spread is short Bybit and long OKX.
    """
    if bybit_rate is None or okx_rate is None or trip.open.ctx is None:
        return None
    if trip.side == "long":
        bybit_cash = trip.bybit_qty_open * trip.open.px * bybit_rate
        okx_px = fnum(trip.open.ctx.get("okx_ask")) or trip.open.px
        okx_cash = -trip.okx_qty_open * trip.ct_val * okx_px * okx_rate
    elif trip.side == "short":
        okx_px = fnum(trip.open.ctx.get("okx_bid")) or trip.open.px
        bybit_cash = -trip.bybit_qty_open * trip.open.px * bybit_rate
        okx_cash = trip.okx_qty_open * trip.ct_val * okx_px * okx_rate
    else:
        return None
    return bybit_cash + okx_cash


def funding_sum(trip: RoundTrip, venue: str) -> float:
    return sum(fnum(row.get("funding")) or 0.0 for row in trip.funding_rows if row.get("venue") == venue)


def check_model(trip: RoundTrip) -> dict:
    """Exchange cash versus size × settlement price × rate on the same row."""
    residuals = []
    for row in trip.funding_rows:
        cash = fnum(row.get("funding"))
        qty = abs(fnum(row.get("size")) or 0)
        px = fnum(row.get("trade_price"))
        rate = fnum(row.get("fee_rate"))
        if cash is None or px is None or qty <= 0 or rate is None or row.get("venue") != "bybit":
            continue
        side_sign = 1.0 if str(row.get("side")).lower() == "buy" else -1.0
        model = side_sign * qty * px * rate
        residuals.append(cash - model)
    if not residuals:
        return {"n": 0, "max_abs": None}
    return {"n": len(residuals), "max_abs": max(abs(x) for x in residuals)}


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _svg_frame(title: str, xlabel: str, ylabel: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="820" height="460" viewBox="0 0 820 460">
<rect width="820" height="460" fill="#f8fafc"/>
<text x="48" y="32" font-family="ui-sans-serif,sans-serif" font-size="16" fill="#0f172a">{title}</text>
<text x="410" y="440" text-anchor="middle" font-family="ui-sans-serif,sans-serif" font-size="12" fill="#334155">{xlabel}</text>
<text x="18" y="230" transform="rotate(-90 18 230)" text-anchor="middle" font-family="ui-sans-serif,sans-serif" font-size="12" fill="#334155">{ylabel}</text>
{body}
</svg>
"""


def plot(trips: list[RoundTrip]) -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    left, right, top, bottom = 64, 790, 52, 400
    width, height = right - left, bottom - top

    def xy_hold(trip: RoundTrip) -> tuple[float, float]:
        fund = funding_sum(trip, "bybit") + funding_sum(trip, "okx")
        return trip.hold_h, pp(fund, trip.entry_notional) or 0.0

    holds = [xy_hold(trip) for trip in trips]
    max_x = max(x for x, _ in holds) or 1
    min_y = min(y for _, y in holds)
    max_y = max(y for _, y in holds)
    pad = max(0.05, (max_y - min_y) * 0.08)
    min_y -= pad
    max_y += pad

    def sx(x: float) -> float:
        return left + width * (x / max_x)

    def sy(y: float) -> float:
        return top + height * (1 - (y - min_y) / (max_y - min_y))

    zero = sy(0)
    parts = [f'<line x1="{left}" y1="{zero:.1f}" x2="{right}" y2="{zero:.1f}" stroke="#94a3b8"/>']
    for trip, (x, y) in zip(trips, holds):
        color = "#b45309" if y < 0 else "#0f766e"
        parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="5" fill="{color}"/>')
        if abs(y) >= 0.4:
            parts.append(
                f'<text x="{sx(x)+6:.1f}" y="{sy(y)-6:.1f}" font-size="11" '
                f'font-family="ui-sans-serif,sans-serif" fill="#334155">{trip.coin}</text>'
            )
    (FIGURES / "funding_vs_hold.svg").write_text(_svg_frame(
        "Фактический funding внутри удержания",
        "Удержание, часы",
        "Funding обеих ног, % одного номинала",
        "\n".join(parts),
    ))

    price = [pp(trip.price_usdt, trip.entry_notional) or 0 for trip in trips]
    net = [p + (pp(funding_sum(trip, "bybit") + funding_sum(trip, "okx"), trip.entry_notional) or 0) for trip, p in zip(trips, price)]
    lo = min(price + net) - 0.1
    hi = max(price + net) + 0.1
    n = max(len(trips) - 1, 1)

    def sx_i(i: int) -> float:
        return left + width * (i / n)

    def sy2(y: float) -> float:
        return top + height * (1 - (y - lo) / (hi - lo))

    parts = [f'<line x1="{left}" y1="{sy2(0):.1f}" x2="{right}" y2="{sy2(0):.1f}" stroke="#94a3b8"/>']
    def polyline(values: list[float], color: str) -> str:
        pts = " ".join(f"{sx_i(i):.1f},{sy2(y):.1f}" for i, y in enumerate(values))
        return f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>'

    parts.append(polyline(price, "#64748b"))
    parts.append(polyline(net, "#0f766e"))
    parts.append('<text x="64" y="48" font-size="12" fill="#64748b" font-family="ui-sans-serif,sans-serif">серый: цена исполнения</text>')
    parts.append('<text x="250" y="48" font-size="12" fill="#0f766e" font-family="ui-sans-serif,sans-serif">зелёный: цена + funding</text>')
    (FIGURES / "price_vs_funding.svg").write_text(_svg_frame(
        "Цена исполнения и funding",
        "Сделка по времени входа",
        "% одного входного номинала Bybit",
        "\n".join(parts),
    ))


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def main() -> None:
    TABLES.mkdir(parents=True, exist_ok=True)
    ledger = load_json(LEDGER)
    cash_rows = [row for row in load_jsonl(CASH) if row.get("kind") != "summary"]
    trips, problems = pair_trades(ledger["lifecycle"])
    unassigned = assign_funding(trips, cash_rows)
    okx_fees = [row for row in load_jsonl(OKX_FEES) if row.get("kind") != "summary"]
    bybit_fees = [
        row for row in cash_rows
        if row.get("stream") == "TRADE" and row.get("venue") == "bybit"
    ]
    assign_fees(trips, bybit_fees + okx_fees)

    start_ms = int(min(trip.open_ts for trip in trips) - 8 * 3_600_000)
    end_ms = int(max(trip.close_ts for trip in trips) + 3_600_000)
    coins = sorted({trip.coin for trip in trips})
    public = fetch_public(coins, start_ms, end_ms)
    sign = sign_check(cash_rows, public)
    print("sign_check", json.dumps(sign))

    registry = []
    for i, trip in enumerate(trips, start=1):
        fund_b = funding_sum(trip, "bybit")
        fund_o = funding_sum(trip, "okx")
        fund = fund_b + fund_o
        price_pp = pp(trip.price_usdt, trip.entry_notional)
        fund_pp = pp(fund, trip.entry_notional)
        fee_pp = pp(trip.fee_usdt, trip.entry_notional)
        net_pf = None if price_pp is None or fund_pp is None else price_pp + fund_pp
        net_all = None if net_pf is None or fee_pp is None else net_pf + fee_pp
        open_spread = fnum((trip.open.ctx or {}).get("selected_spread_pct"))
        bybit_rate, bybit_rate_ts = last_rate(public["bybit"], trip.coin, trip.open_ts)
        okx_rate, okx_rate_ts = last_rate(public["okx"], trip.coin, trip.open_ts)
        next_ts = next_predicted(public["bybit"], trip.coin, trip.open_ts)
        near = None if next_ts is None else (next_ts - trip.open_ts) < NEAR_SETTLEMENT_MS
        # One next settlement, last public rates, entry size. Income is positive.
        expected = expected_next_cash(trip, bybit_rate, okx_rate)
        expected_pp = pp(expected, trip.entry_notional)
        hurdle = None
        if open_spread is not None and expected_pp is not None:
            # Haircut only the expected cash outflow of the next settlement.
            cost_pp = max(-(expected_pp), 0.0)
            hurdle = open_spread - FEE_MODEL_PP - cost_pp
        model = check_model(trip)
        registry.append({
            "n": i,
            "coin": trip.coin,
            "side": trip.side,
            "open_utc": utc(trip.open_ts),
            "close_utc": utc(trip.close_ts),
            "hold_h": round(trip.hold_h, 3),
            "ct_val": trip.ct_val,
            "ct_source": trip.ct_source,
            "bybit_qty": trip.bybit_qty_open,
            "okx_contracts": trip.okx_qty_open,
            "entry_notional_usdt": round(trip.entry_notional, 4),
            "open_spread_signal_pct": open_spread,
            "price_usdt": round(trip.price_usdt, 6),
            "price_pp": None if price_pp is None else round(price_pp, 4),
            "fee_usdt": None if trip.fee_usdt is None else round(trip.fee_usdt, 6),
            "fee_pp": None if fee_pp is None else round(fee_pp, 4),
            "fee_legs": trip.fee_legs,
            "fee_gaps": ";".join(trip.fee_gaps),
            "funding_bybit_usdt": round(fund_b, 6),
            "funding_okx_usdt": round(fund_o, 6),
            "funding_usdt": round(fund, 6),
            "funding_pp": None if fund_pp is None else round(fund_pp, 4),
            "funding_events": len(trip.funding_rows),
            "price_plus_funding_pp": None if net_pf is None else round(net_pf, 4),
            "price_fee_funding_pp": None if net_all is None else round(net_all, 4),
            "bybit_model_max_abs_residual_usdt": model["max_abs"],
            "bybit_model_rows": model["n"],
            "last_bybit_rate": bybit_rate,
            "last_okx_rate": okx_rate,
            "expected_next_funding_usdt": None if expected is None else round(expected, 6),
            "expected_next_funding_pp": None if expected_pp is None else round(expected_pp, 4),
            "entry_hurdle_pp": None if hurdle is None else round(hurdle, 4),
            "near_settlement_30m": near,
            "pass_near_filter": None if near is None else (not near),
            "pass_hurdle": None if hurdle is None else hurdle >= MIN_PROFIT_PP,
        })

    write_csv(TABLES / "round_trips.csv", registry)
    write_csv(TABLES / "pairing_problems.csv", problems)
    outside = []
    for row in unassigned:
        outside.append({
            "venue": row.get("venue"),
            "instrument": row.get("instrument"),
            "ts_utc": utc(float(row["ts_ms"])) if fnum(row.get("ts_ms")) else "",
            "funding": row.get("funding"),
            "size": row.get("size"),
            "side": row.get("side"),
            "reason": row.get("reason"),
        })
    write_csv(TABLES / "funding_unassigned.csv", outside)

    def subset(flag: str) -> list[dict]:
        return [row for row in registry if row.get(flag) is True]

    def total(rows: list[dict], key: str) -> float | None:
        vals = [row[key] for row in rows if row.get(key) is not None]
        if len(vals) != len(rows) or not rows:
            return None
        return round(sum(vals), 4)

    priced = [row for row in registry if row["price_pp"] is not None]
    flipped = [
        row for row in priced
        if row["price_pp"] < 0 and row["price_plus_funding_pp"] is not None and row["price_plus_funding_pp"] >= 0
    ]
    still_loss = [
        row for row in priced
        if row["price_plus_funding_pp"] is not None and row["price_plus_funding_pp"] < 0
    ]
    short_hold = [row for row in priced if row["hold_h"] < 0.25 and (row["open_spread_signal_pct"] or 0) > 0 and row["price_pp"] < 0.05]
    fee_complete = [row for row in registry if row["fee_usdt"] is not None]
    normal_after = [row for row in fee_complete if (row["price_fee_funding_pp"] or -1) > 0]
    summary = {
        "paired_round_trips": len(trips),
        "pairing_problems": problems,
        "funding_events_assigned": sum(row["funding_events"] for row in registry),
        "funding_events_unassigned": len(unassigned),
        "fee_complete_trades": len(fee_complete),
        "price_pp_sum": total(priced, "price_pp"),
        "price_plus_funding_pp_sum": total(priced, "price_plus_funding_pp"),
        "full_pp_sum": total(priced, "price_fee_funding_pp"),
        "full_pp_sum_near_kept": total(subset("pass_near_filter"), "price_fee_funding_pp"),
        "full_pp_sum_hurdle_kept": total(subset("pass_hurdle"), "price_fee_funding_pp"),
        "near_excluded": sum(row.get("pass_near_filter") is False for row in registry),
        "hurdle_excluded": sum(row.get("pass_hurdle") is False for row in registry),
        "price_loss_then_funding_nonnegative": len(flipped),
        "still_negative_after_funding": len(still_loss),
        "hold_under_15m_and_price_pp_under_0.05": len(short_hold),
        "median_price_pp": median([row["price_pp"] for row in priced]),
        "median_price_plus_funding_pp": median([row["price_plus_funding_pp"] for row in priced if row["price_plus_funding_pp"] is not None]),
        "median_full_pp_when_fee_known": median([row["price_fee_funding_pp"] for row in fee_complete if row["price_fee_funding_pp"] is not None]),
        "median_positive_full_pp": median([row["price_fee_funding_pp"] for row in normal_after if row["price_fee_funding_pp"] is not None]),
        "variant_near_kept": len(subset("pass_near_filter")),
        "variant_near_price_plus_funding_pp_sum": total(subset("pass_near_filter"), "price_plus_funding_pp"),
        "variant_hurdle_known": sum(row["pass_hurdle"] is not None for row in registry),
        "variant_hurdle_kept": len(subset("pass_hurdle")),
        "variant_hurdle_price_plus_funding_pp_sum": total(subset("pass_hurdle"), "price_plus_funding_pp"),
        "sign_check": sign,
        "funding_wiped_positive_price": sum(
            1 for row in priced
            if row["price_pp"] > 0 and row["price_plus_funding_pp"] is not None and row["price_plus_funding_pp"] <= 0
        ),
        "public_bybit_points": len(public["bybit"]),
        "public_okx_points": len(public["okx"]),
        "denominator": "entry Bybit fill price × Bybit filled qty, percent = USDT / that notional × 100",
        "manual_labels_not_applied": {"funding_converged_losses": 12, "short_spread": 5, "normal_profit": 9, "normal_median_pct": 0.15},
    }
    (TABLES / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    plot(trips)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

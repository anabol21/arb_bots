"""Bybit ∩ OKX USDT-swap listing growth vs the frozen universe CSV.

Track 2 research only. Does not rewrite ``bybit_okx_universe.csv`` and does
not touch the collector.

The CSV is a snapshot of names (git: 2026-08-14, 345 ``base_coin`` values;
``take`` added later without changing the name set). New listings after that
freeze never enter the collector. This script compares the frozen name set to
the live public catalogs and uses

    joint_listed_ms = max(bybit.launchTime, okx.listTime)

as a proxy for when the second USDT linear/swap leg became available.

Public REST only (no private keys):

- Bybit ``GET /v5/market/instruments-info?category=linear``
- OKX ``GET /api/v5/public/instruments?instType=SWAP``

Raw JSON is cached under ``research/data/universe_listing_cache/``.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from research.is_crypto import is_crypto, normalize_base_coin

REPO = Path(__file__).resolve().parents[1]
DEFAULT_UNIVERSE = REPO / "bybit_okx_universe.csv"
CACHE_DIR = REPO / "research" / "data" / "universe_listing_cache"
OUT_CSV = REPO / "research" / "data" / "universe_listing_growth.csv"
OUT_HTML = REPO / "research" / "data" / "universe_listing_growth.html"
DAILY_CSV = REPO / "research" / "data" / "universe_listing_growth_daily.csv"

BYBIT_URL = "https://api.bybit.com/v5/market/instruments-info"
OKX_URL = "https://www.okx.com/api/v5/public/instruments"
UA = {"User-Agent": "spread-universe-listing-growth/1.0"}

# First git commit of bybit_okx_universe.csv (c7df8dc). Name set unchanged since.
ANCHOR_CSV_FREEZE = datetime(2026, 8, 14, tzinfo=timezone.utc)
# User asked about July; same frozen names, earlier reporting bucket.
ANCHOR_JULY = datetime(2026, 7, 1, tzinfo=timezone.utc)

BYBIT_CACHE = "bybit_linear.json"
OKX_CACHE = "okx_swap.json"
META_CACHE = "fetch_meta.json"


@dataclass(frozen=True)
class VenueInstrument:
    base_coin: str
    symbol: str
    listed_ms: int
    status: str
    settle: str


@dataclass
class JointName:
    base_coin: str
    bybit_symbol: str
    okx_symbol: str
    bybit_listed_ms: int
    okx_listed_ms: int
    joint_listed_ms: int
    later_venue: str
    in_csv: bool
    is_crypto: bool

    @property
    def joint_listed_utc(self) -> datetime:
        return datetime.fromtimestamp(self.joint_listed_ms / 1000.0, tz=timezone.utc)


@dataclass
class GrowthReport:
    fetched_at_utc: datetime
    n_csv: int
    n_live: int
    n_live_crypto: int
    live_minus_csv: list[str]
    csv_minus_live: list[str]
    names: list[JointName]
    csv_names: frozenset[str]


def _http_get_json(url: str, *, timeout: float = 40.0, retries: int = 6) -> Any:
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                raise
            time.sleep(45.0 if exc.code == 429 else min(2**attempt * 0.6, 15.0))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            time.sleep(min(2**attempt * 0.6, 15.0))
    raise RuntimeError(f"GET failed after retries: {url} ({last})")


def _parse_ms(raw: Any) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_bybit_linear(payload: Any) -> dict[str, VenueInstrument]:
    """USDT linear perpetuals in Trading status, keyed by base coin.

    If several symbols share a base (rare), keep the earliest launchTime.
    """
    rows = _bybit_list(payload)
    out: dict[str, VenueInstrument] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        settle = str(row.get("settleCoin") or "").upper()
        status = str(row.get("status") or "")
        ctype = str(row.get("contractType") or "")
        if settle != "USDT":
            continue
        if status.lower() != "trading":
            continue
        if ctype and ctype not in ("LinearPerpetual",):
            continue
        base = normalize_base_coin(str(row.get("baseCoin") or ""))
        symbol = str(row.get("symbol") or "")
        listed = _parse_ms(row.get("launchTime"))
        if not base or not symbol or listed is None:
            continue
        inst = VenueInstrument(
            base_coin=base,
            symbol=symbol,
            listed_ms=listed,
            status=status,
            settle=settle,
        )
        prev = out.get(base)
        if prev is None or inst.listed_ms < prev.listed_ms:
            out[base] = inst
    return out


def _bybit_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("list"), list):
        return result["list"]
    if isinstance(payload.get("list"), list):
        return payload["list"]
    return []


def parse_okx_swap(payload: Any) -> dict[str, VenueInstrument]:
    """USDT perpetual swaps in live state, keyed by base coin."""
    rows = _okx_list(payload)
    out: dict[str, VenueInstrument] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        inst_id = str(row.get("instId") or "")
        state = str(row.get("state") or "").lower()
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        if state and state not in ("live",):
            continue
        settle = str(row.get("settleCcy") or "USDT").upper()
        if settle != "USDT":
            continue
        base = normalize_base_coin(inst_id.split("-", 1)[0])
        listed = _parse_ms(row.get("listTime"))
        if not base or listed is None:
            continue
        inst = VenueInstrument(
            base_coin=base,
            symbol=inst_id,
            listed_ms=listed,
            status=state or "live",
            settle=settle,
        )
        prev = out.get(base)
        if prev is None or inst.listed_ms < prev.listed_ms:
            out[base] = inst
    return out


def _okx_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return []


def later_venue(bybit_ms: int, okx_ms: int) -> str:
    if bybit_ms > okx_ms:
        return "bybit"
    if okx_ms > bybit_ms:
        return "okx"
    return "tie"


def intersect_live(
    bybit: dict[str, VenueInstrument],
    okx: dict[str, VenueInstrument],
    *,
    csv_names: Iterable[str] = (),
) -> list[JointName]:
    csv_set = frozenset(normalize_base_coin(c) for c in csv_names)
    names: list[JointName] = []
    for base in sorted(set(bybit) & set(okx)):
        b = bybit[base]
        o = okx[base]
        joint = max(b.listed_ms, o.listed_ms)
        names.append(
            JointName(
                base_coin=base,
                bybit_symbol=b.symbol,
                okx_symbol=o.symbol,
                bybit_listed_ms=b.listed_ms,
                okx_listed_ms=o.listed_ms,
                joint_listed_ms=joint,
                later_venue=later_venue(b.listed_ms, o.listed_ms),
                in_csv=base in csv_set,
                is_crypto=is_crypto(base),
            )
        )
    return names


def load_csv_names(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "base_coin" not in reader.fieldnames:
            raise ValueError(f"{path} has no base_coin column")
        return [normalize_base_coin(row["base_coin"]) for row in reader if row.get("base_coin")]


def build_report(
    names: list[JointName],
    csv_names: Iterable[str],
    *,
    fetched_at: Optional[datetime] = None,
) -> GrowthReport:
    csv_set = frozenset(normalize_base_coin(c) for c in csv_names)
    live = {n.base_coin for n in names}
    live_crypto = {n.base_coin for n in names if n.is_crypto}
    return GrowthReport(
        fetched_at_utc=fetched_at or datetime.now(timezone.utc),
        n_csv=len(csv_set),
        n_live=len(live),
        n_live_crypto=len(live_crypto),
        live_minus_csv=sorted(live - csv_set),
        csv_minus_live=sorted(csv_set - live),
        names=names,
        csv_names=csv_set,
    )


def is_new(name: JointName, *, anchor: datetime) -> bool:
    """Name is new relative to the frozen CSV: missing from it, or listed after the anchor."""
    if not name.in_csv:
        return True
    return name.joint_listed_utc >= anchor


def daily_cadence(
    names: Iterable[JointName],
    *,
    anchor: datetime,
    until: Optional[datetime] = None,
    crypto_only: bool = False,
) -> list[dict[str, Any]]:
    """Count new joint listings per UTC calendar day from ``anchor`` through ``until``.

    A name counts on the UTC date of ``joint_listed`` only if that timestamp is
    on/after the anchor. Names missing from the CSV but listed *before* the
    anchor are not put in these bars (they are a snapshot-gap, not growth).
    Zero-count days are kept so the axis is a continuous calendar.
    """
    until = until or datetime.now(timezone.utc)
    start = _utc_day(anchor)
    end = _utc_day(until)
    days: list[datetime] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)

    counts = {d: 0 for d in days}
    for n in names:
        if crypto_only and not n.is_crypto:
            continue
        ts = n.joint_listed_utc
        if ts < anchor or ts > until:
            continue
        bucket = _utc_day(ts)
        if bucket in counts:
            counts[bucket] += 1

    rows: list[dict[str, Any]] = []
    cum = 0
    for d in days:
        cum += counts[d]
        rows.append(
            {
                "day_utc": d.date().isoformat(),
                "n_new": counts[d],
                "n_cum": cum,
            }
        )
    return rows


def _utc_day(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_bybit_linear(*, cache_dir: Path, refresh: bool) -> Any:
    path = cache_dir / BYBIT_CACHE
    if path.is_file() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    rows: list[Any] = []
    cursor = ""
    while True:
        params = {"category": "linear", "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        url = f"{BYBIT_URL}?{urllib.parse.urlencode(params)}"
        payload = _http_get_json(url)
        if int(payload.get("retCode") or 0) != 0:
            raise RuntimeError(f"Bybit instruments-info failed: {payload.get('retMsg')}")
        result = payload.get("result") or {}
        chunk = result.get("list") or []
        rows.extend(chunk)
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            break
    wrapped = {"retCode": 0, "result": {"list": rows, "nextPageCursor": ""}}
    _write_json(path, wrapped)
    return wrapped


def fetch_okx_swap(*, cache_dir: Path, refresh: bool) -> Any:
    path = cache_dir / OKX_CACHE
    if path.is_file() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    url = f"{OKX_URL}?{urllib.parse.urlencode({'instType': 'SWAP'})}"
    payload = _http_get_json(url)
    if str(payload.get("code") or "") not in ("0", "0.0", ""):
        # OKX success is code "0"
        if payload.get("code") not in (0, "0"):
            raise RuntimeError(f"OKX instruments failed: {payload.get('msg')}")
    _write_json(path, payload)
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def write_names_csv(path: Path, names: list[JointName]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "base_coin",
        "in_csv",
        "is_crypto",
        "bybit_symbol",
        "okx_symbol",
        "bybit_listed_utc",
        "okx_listed_utc",
        "joint_listed_utc",
        "later_venue",
        "joint_listed_ms",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for n in sorted(names, key=lambda x: (x.joint_listed_ms, x.base_coin)):
            w.writerow(
                {
                    "base_coin": n.base_coin,
                    "in_csv": "yes" if n.in_csv else "no",
                    "is_crypto": "yes" if n.is_crypto else "no",
                    "bybit_symbol": n.bybit_symbol,
                    "okx_symbol": n.okx_symbol,
                    "bybit_listed_utc": ms_to_iso(n.bybit_listed_ms),
                    "okx_listed_utc": ms_to_iso(n.okx_listed_ms),
                    "joint_listed_utc": ms_to_iso(n.joint_listed_ms),
                    "later_venue": n.later_venue,
                    "joint_listed_ms": n.joint_listed_ms,
                }
            )


def write_html(
    path: Path,
    report: GrowthReport,
    *,
    daily_all_freeze: list[dict],
    daily_crypto_freeze: list[dict],
    daily_all_july: list[dict],
    daily_crypto_july: list[dict],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_after_freeze = [n for n in report.names if n.joint_listed_utc >= ANCHOR_CSV_FREEZE]
    new_after_july = [n for n in report.names if n.joint_listed_utc >= ANCHOR_JULY]
    missed = [n for n in report.names if (not n.in_csv) and n.joint_listed_utc < ANCHOR_CSV_FREEZE]
    new_not_in_csv = [n for n in report.names if not n.in_csv]
    new_crypto_freeze = [n for n in new_after_freeze if n.is_crypto]
    new_crypto_july = [n for n in new_after_july if n.is_crypto]

    def _svg(rows: list[dict], title: str) -> str:
        return _bar_svg(rows, title, date_key="day_utc")

    new_rows = "".join(_name_tr(n) for n in sorted(new_not_in_csv, key=lambda x: -x.joint_listed_ms))
    missed_rows = "".join(_name_tr(n) for n in missed)
    gone_rows = "".join(
        f"<tr><td><code>{html.escape(c)}</code></td></tr>" for c in report.csv_minus_live
    )
    if not gone_rows:
        gone_rows = "<tr><td><em>none</em></td></tr>"
    if not missed_rows:
        missed_rows = "<tr><td colspan='7'><em>none</em></td></tr>"
    if not new_rows:
        new_rows = "<tr><td colspan='7'><em>none — live intersection matches CSV names</em></td></tr>"

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Bybit ∩ OKX listing growth vs frozen CSV</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 24px; color: #111; max-width: 1100px; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
  .nums {{ display: flex; flex-wrap: wrap; gap: 12px; }}
  .card {{ border: 1px solid #ddd; padding: 12px 16px; min-width: 140px; }}
  .card b {{ display: block; font-size: 1.4rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
  th, td {{ border-bottom: 1px solid #eee; padding: 4px 8px; text-align: left; }}
  th {{ background: #f6f6f6; }}
  code {{ font-size: 0.9em; }}
  .note {{ color: #444; font-size: 0.9rem; line-height: 1.45; }}
  .warn {{ background: #fff8e6; border: 1px solid #e6d08a; padding: 10px 12px; }}
  svg {{ max-width: 100%; height: auto; }}
</style>
</head>
<body>
<h1>Bybit ∩ OKX USDT-swap listing growth</h1>
<p class="note">
Frozen pool: <code>bybit_okx_universe.csv</code> first committed 2026-08-14
(345 names). A later commit added <code>take</code>; the <code>base_coin</code>
set did not change. The collector still watches that CSV. This page is the
live public catalogs versus that snapshot — not how often the CSV was updated
(it was not, by names) and not profitability.
</p>
<p class="note">Fetched UTC: {html.escape(report.fetched_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))}.
Proxy: <code>joint_listed_ms = max(bybit.launchTime, okx.listTime)</code>.</p>

<div class="nums">
  <div class="card"><span>n_csv</span><b>{report.n_csv}</b></div>
  <div class="card"><span>n_live intersect</span><b>{report.n_live}</b></div>
  <div class="card"><span>n_live crypto</span><b>{report.n_live_crypto}</b></div>
  <div class="card"><span>|live − csv|</span><b>{len(report.live_minus_csv)}</b></div>
  <div class="card"><span>|csv − live|</span><b>{len(report.csv_minus_live)}</b></div>
  <div class="card"><span>joint ≥ 14 Aug (all / crypto)</span><b>{len(new_after_freeze)} / {len(new_crypto_freeze)}</b></div>
  <div class="card"><span>joint ≥ 1 Jul (all / crypto)</span><b>{len(new_after_july)} / {len(new_crypto_july)}</b></div>
</div>

<div class="warn note">
Limitations: no daily catalog archive, so a name with listTime before the freeze
cannot be proven to have been in the intersection on a given halted day.
<code>launchTime</code>/<code>listTime</code> are vendor fields. Inverse, dated
futures, and non-USDT are out of this intersection, as in the CSV.
<code>is_crypto</code> uses the existing denylist (equities already in the CSV).
Newly listed stock/ETF perps are usually <em>not</em> on that list, so the
post-freeze crypto series can equal the all-swaps series. Do not read that
overlap as “no equities listed.” The all-swaps daily bars are the listing rate.
</div>

<h2>Daily cadence from CSV freeze (2026-08-14)</h2>
<p class="note">Bars count names whose <code>joint_listed</code> falls on that UTC
calendar day and on/after the freeze. Empty days stay on the axis. Left scale
is daily count; right scale is cumulative. Equities inflate the all-swaps
series; <code>is_crypto</code> is the second series (denylist may miss new stocks).</p>
{_svg(daily_all_freeze, "all USDT swaps")}
{_svg(daily_crypto_freeze, "is_crypto only")}

<h2>Daily cadence from 1 Jul 2026</h2>
<p class="note">Same frozen name list. July is an earlier reporting bucket, not a
different CSV.</p>
{_svg(daily_all_july, "all USDT swaps")}
{_svg(daily_crypto_july, "is_crypto only")}

<h2>Names in live ∩ not in CSV</h2>
<table>
<thead><tr>
<th>base</th><th>crypto</th><th>joint listed UTC</th><th>later venue</th>
<th>Bybit launch</th><th>OKX list</th><th>symbols</th>
</tr></thead>
<tbody>
{new_rows}
</tbody>
</table>

<h2>In live ∩, listed before freeze, missing from CSV</h2>
<p class="note">Snapshot gap at freeze (filter, ROW slice, or matching), not growth after freeze.</p>
<table>
<thead><tr>
<th>base</th><th>crypto</th><th>joint listed UTC</th><th>later venue</th>
<th>Bybit launch</th><th>OKX list</th><th>symbols</th>
</tr></thead>
<tbody>
{missed_rows}
</tbody>
</table>

<h2>In CSV, missing from live ∩ (delist / rename / halt)</h2>
<table>
<thead><tr><th>base_coin</th></tr></thead>
<tbody>
{gone_rows}
</tbody>
</table>
</body>
</html>
"""
    path.write_text(body, encoding="utf-8")


def _name_tr(n: JointName) -> str:
    return (
        "<tr>"
        f"<td><code>{html.escape(n.base_coin)}</code></td>"
        f"<td>{'yes' if n.is_crypto else 'no'}</td>"
        f"<td>{html.escape(ms_to_iso(n.joint_listed_ms))}</td>"
        f"<td>{html.escape(n.later_venue)}</td>"
        f"<td>{html.escape(ms_to_iso(n.bybit_listed_ms))}</td>"
        f"<td>{html.escape(ms_to_iso(n.okx_listed_ms))}</td>"
        f"<td><code>{html.escape(n.bybit_symbol)}</code> / <code>{html.escape(n.okx_symbol)}</code></td>"
        "</tr>"
    )


def _row_day(row: dict) -> str:
    return str(row.get("day_utc") or row.get("week_start_utc") or "")


def _bar_svg(
    rows: list[dict],
    title: str,
    *,
    date_key: str = "day_utc",
    width: int = 1040,
    height: int = 240,
) -> str:
    if not rows:
        return f"<p><em>No daily rows for {html.escape(title)}</em></p>"
    left, right, bottom, top = 40, 40, 44, 28
    inner_w = width - left - right
    inner_h = height - top - bottom
    ymax_new = max((int(r["n_new"]) for r in rows), default=0) or 1
    ymax_cum = max((int(r["n_cum"]) for r in rows), default=0) or 1
    n = len(rows)
    gap = 1.0 if n > 40 else 2.0
    bar_w = max(1.5, (inner_w / n) - gap)
    step = bar_w + gap
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">',
        f'<text x="{left}" y="16" font-size="13" font-family="sans-serif">{html.escape(title)}</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + inner_h}" stroke="#ccc"/>',
        f'<line x1="{width - right}" y1="{top}" x2="{width - right}" y2="{top + inner_h}" stroke="#ccc"/>',
        f'<line x1="{left}" y1="{top + inner_h}" x2="{width - right}" y2="{top + inner_h}" stroke="#ccc"/>',
    ]
    for frac in (0.0, 0.5, 1.0):
        y = top + inner_h * (1 - frac)
        left_val = int(round(ymax_new * frac))
        right_val = int(round(ymax_cum * frac))
        parts.append(
            f'<text x="{left - 6}" y="{y + 4}" text-anchor="end" font-size="10" fill="#4c6ef5">{left_val}</text>'
        )
        parts.append(
            f'<text x="{width - right + 6}" y="{y + 4}" font-size="10" fill="#c92a2a">{right_val}</text>'
        )
    pts: list[str] = []
    label_every = max(1, n // 8)
    for i, r in enumerate(rows):
        x = left + i * step
        h = inner_h * (int(r["n_new"]) / ymax_new)
        y = top + inner_h - h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" fill="#4c6ef5" opacity="0.85"/>'
        )
        cx = x + bar_w / 2
        cy = top + inner_h - inner_h * (int(r["n_cum"]) / ymax_cum)
        pts.append(f"{cx:.1f},{cy:.1f}")
        day = str(r.get(date_key) or _row_day(r))
        if i == 0 or i == n - 1 or i % label_every == 0:
            label = day[5:] if len(day) >= 10 else day
            parts.append(
                f'<text x="{cx:.1f}" y="{top + inner_h + 14}" text-anchor="middle" font-size="9" fill="#444">{html.escape(label)}</text>'
            )
    parts.append(
        f'<polyline fill="none" stroke="#c92a2a" stroke-width="2" points="{" ".join(pts)}"/>'
    )
    parts.append(
        '<text x="200" y="16" font-size="11" fill="#4c6ef5" font-family="sans-serif">daily new (bars, left)</text>'
        '<text x="400" y="16" font-size="11" fill="#c92a2a" font-family="sans-serif">cumulative (line, right)</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def format_summary(report: GrowthReport, *, daily_crypto_freeze: list[dict]) -> str:
    new_after = [n for n in report.names if n.joint_listed_utc >= ANCHOR_CSV_FREEZE]
    new_crypto = [n for n in new_after if n.is_crypto]
    missed = [n.base_coin for n in report.names if (not n.in_csv) and n.joint_listed_utc < ANCHOR_CSV_FREEZE]
    cum = daily_crypto_freeze[-1]["n_cum"] if daily_crypto_freeze else 0
    lines = [
        "Bybit ∩ OKX listing growth vs frozen CSV",
        f"fetched_utc={report.fetched_at_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"n_csv={report.n_csv}  n_live={report.n_live}  n_live_crypto={report.n_live_crypto}",
        f"|live-csv|={len(report.live_minus_csv)}  |csv-live|={len(report.csv_minus_live)}",
        f"joint_listed >= 2026-08-14: all={len(new_after)} crypto={len(new_crypto)}",
        f"crypto daily cum since freeze={cum}",
        f"in live, not in CSV, listed before freeze (snapshot gap)={len(missed)}",
        "CSV name set was not updated after freeze; growth below is venue catalogs only.",
        "is_crypto denylist is frozen with the CSV: new equity/ETF perps after the freeze",
        "still count as crypto=yes, so the two freeze series can match. Prefer all-swaps cadence.",
    ]
    if report.live_minus_csv:
        sample = ", ".join(report.live_minus_csv[:20])
        extra = "" if len(report.live_minus_csv) <= 20 else f" … +{len(report.live_minus_csv) - 20}"
        lines.append(f"new names (not in CSV): {sample}{extra}")
    if report.csv_minus_live:
        lines.append("gone from live ∩: " + ", ".join(report.csv_minus_live))
    return "\n".join(lines)


def run(
    *,
    universe: Path = DEFAULT_UNIVERSE,
    cache_dir: Path = CACHE_DIR,
    refresh: bool = False,
    out_csv: Path = OUT_CSV,
    out_html: Path = OUT_HTML,
    daily_csv: Path = DAILY_CSV,
) -> GrowthReport:
    cache_dir.mkdir(parents=True, exist_ok=True)
    csv_names = load_csv_names(universe)
    bybit_payload = fetch_bybit_linear(cache_dir=cache_dir, refresh=refresh)
    okx_payload = fetch_okx_swap(cache_dir=cache_dir, refresh=refresh)
    bybit = parse_bybit_linear(bybit_payload)
    okx = parse_okx_swap(okx_payload)
    names = intersect_live(bybit, okx, csv_names=csv_names)
    fetched_at = datetime.now(timezone.utc)
    meta_path = cache_dir / META_CACHE
    if not refresh and meta_path.is_file():
        try:
            prev = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = datetime.strptime(prev["fetched_at_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (KeyError, ValueError, json.JSONDecodeError):
            pass
    meta = {
        "fetched_at_utc": fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_bybit_usdt_perp": len(bybit),
        "n_okx_usdt_swap": len(okx),
        "refresh": refresh,
    }
    _write_json(meta_path, meta)
    report = build_report(names, csv_names, fetched_at=fetched_at)

    until = fetched_at
    d_all_f = daily_cadence(names, anchor=ANCHOR_CSV_FREEZE, until=until, crypto_only=False)
    d_cr_f = daily_cadence(names, anchor=ANCHOR_CSV_FREEZE, until=until, crypto_only=True)
    d_all_j = daily_cadence(names, anchor=ANCHOR_JULY, until=until, crypto_only=False)
    d_cr_j = daily_cadence(names, anchor=ANCHOR_JULY, until=until, crypto_only=True)

    write_names_csv(out_csv, names)
    _write_daily_both(daily_csv, d_all_f, d_cr_f, d_all_j, d_cr_j)
    write_html(
        out_html,
        report,
        daily_all_freeze=d_all_f,
        daily_crypto_freeze=d_cr_f,
        daily_all_july=d_all_j,
        daily_crypto_july=d_cr_j,
    )
    stale_weekly = REPO / "research" / "data" / "universe_listing_growth_weekly.csv"
    if stale_weekly.is_file() and stale_weekly.resolve() != daily_csv.resolve():
        stale_weekly.unlink()
    print(format_summary(report, daily_crypto_freeze=d_cr_f))
    print(f"wrote {out_csv}")
    print(f"wrote {daily_csv}")
    print(f"wrote {out_html}")
    return report


def _write_daily_both(
    path: Path,
    all_f: list[dict],
    cr_f: list[dict],
    all_j: list[dict],
    cr_j: list[dict],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["anchor", "crypto_only", "day_utc", "n_new", "n_cum"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for anchor, crypto, rows in (
            ("csv_freeze_2026-08-14", False, all_f),
            ("csv_freeze_2026-08-14", True, cr_f),
            ("july_2026-07-01", False, all_j),
            ("july_2026-07-01", True, cr_j),
        ):
            for r in rows:
                w.writerow(
                    {
                        "anchor": anchor,
                        "crypto_only": "yes" if crypto else "no",
                        "day_utc": r["day_utc"],
                        "n_new": r["n_new"],
                        "n_cum": r["n_cum"],
                    }
                )


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    p.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    p.add_argument("--refresh", action="store_true", help="refetch public catalogs")
    p.add_argument("--out-csv", type=Path, default=OUT_CSV)
    p.add_argument("--out-html", type=Path, default=OUT_HTML)
    p.add_argument("--daily-csv", type=Path, default=DAILY_CSV)
    args = p.parse_args(argv)
    run(
        universe=args.universe,
        cache_dir=args.cache_dir,
        refresh=args.refresh,
        out_csv=args.out_csv,
        out_html=args.out_html,
        daily_csv=args.daily_csv,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

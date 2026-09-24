"""Parameter-space relief for gear 2.2 method 3. Observation accounting only.

``policy.decide`` and ``FROZEN`` are not retuned. Each grid point is one
``run_combo`` plus a ``balance_equalize`` cash ledger. The four heatmap
numbers use applied closes only. Holes and price mismatches stay out of
those averages and are reported beside them.

Calculation order (the ETA line is mandatory before the remaining combos):

1. load the feature hive once
2. pilot combos, including the frozen cell
3. print a theoretical remaining-time estimate
4. remaining combos
5. one L1 load of the union of open/close keys and 5-minute marks inside holds
6. method-3 ledger per cell
7. ``grid.json``, cell HTML, ``index.html``
"""

from __future__ import annotations

import argparse
import gc
import html
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from research.gear22_backtest.params_frozen import FROZEN
from research.gear22_backtest.policy import PolicyParams
from research.gear22_backtest.replay import ClosedTrade
from research.gear22_backtest.sweep import RunResult, run_combo, store_from_hive
from research.gear22_backtest.venue_5m_run import (
    FIRST_BUCKET,
    _divergence_note,
    _sidecar_last_second,
)
from research.gear22_backtest.venue_balance_run import (
    drop_terminal_delisted_open,
    list_hive_event_dates,
)
from research.gear22_backtest.venue_coin_pnl import coin_pnl_section_html, round_net_usd
from research.gear22_backtest.venue_ledger import (
    START_CASH,
    LedgerStep,
    align_bucket_5m_start,
    approx_vs_cash_usd_5m,
    bucket_5m_mark_ts,
    coverage_counts_5m,
    equity_series_5m,
    iter_bucket_5m_starts,
    load_last_books,
    open_fill_exit_pp,
    price_run,
    run_ledger,
    timestamp_keys_for_run,
    write_balance_5m_html,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HIVE = REPO_ROOT / "output" / "gear22_backtest_features_by_date"
DEFAULT_TICKS = REPO_ROOT / "output" / "lean_ticks"
DEFAULT_BOOKS = REPO_ROOT / "output" / "gear22_books_1hz"
DEFAULT_OUT = REPO_ROOT / "research" / "data" / "venue_balance" / "param_space"

def _axis(start: float, stop: float, step: float = 0.05) -> tuple[float, ...]:
    count = int(round((stop - start) / step))
    return tuple(round(start + i * step, 2) for i in range(count + 1))


THETA_OPEN = _axis(0.20, 0.80)
P50_OPEN = _axis(0.20, 1.00)
MIN_PROFIT_PP = (0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)
MIN_THETA_CLOSE = 0.05
FROZEN_POINT = (0.50, 0.60, 0.20)
SQUARE = 0.05
PILOT_N = 3
MODE = "balance_equalize"
TERMINAL_CELL_STYLE = """
  body { background:#070b10; color:#d7e2ea; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
  h1, h2, h3 { color:#e8f4ff; letter-spacing:0.03em; }
  a { color:#3ddcff; }
  .warn { background:#101820; border-color:#1e3a4c; color:#b7c9d6; }
  .note, .meta, ul { color:#9fb3c4; }
  svg { background:#0c1218; border-color:#1c2c3a; }
  svg text { fill:#c5d4e0; }
  svg line { stroke:#243140; }
  th { background:#101820; color:#9fb3c4; }
  td { color:#d7e2ea; border-color:#1e2c38; }
  tr.icx td { background:#2a1518; color:#ffd0d4; }
"""

MONEY_METRICS = ("total_profit", "avg_profit")
METRICS = (
    ("total_profit", "Суммарная прибыль, $"),
    ("avg_profit", "Средний профит на сделку, $"),
    ("mean_hold_h", "Среднее время держания, ч"),
    ("n_trades", "Количество сделок"),
    ("coin_top_share", "Доля сделок лучшей монеты"),
    ("n_coins", "Число монет"),
    ("week_top_share", "Доля сделок в самой плотной неделе"),
    ("max_gap_h", "Самый длинный простой, ч"),
    ("position_ratio", "В позиции / вне позиции"),
)
INTEGER_METRICS = ("n_trades", "n_coins")


@dataclass(frozen=True)
class CellRun:
    theta_open: float
    p50_open: float
    min_profit_pp: float
    run: RunResult
    open_fill_pp: float | None
    open_exit_pp: float | None
    combo_s: float


def grid_points() -> list[tuple[float, float, float]]:
    """Cartesian product. Frozen ``(0.50, 0.60, 0.20)`` is on the grid."""
    return [
        (theta, p50, profit)
        for theta in THETA_OPEN
        for p50 in P50_OPEN
        for profit in MIN_PROFIT_PP
    ]


def params_for(theta: float, p50: float, profit: float) -> PolicyParams:
    return PolicyParams(
        theta_open=theta,
        p50_open=p50,
        min_profit_pp=profit,
        min_theta_close=MIN_THETA_CLOSE,
        min_spread_open=None,
        fee_round_trip_pp=float(FROZEN.fee_round_trip_pp),
    )


def cell_stem(theta: float, p50: float, profit: float) -> str:
    """``t0.50_p0.60_m0.25`` — two decimals so neighbouring profits stay distinct."""
    return f"t{theta:.2f}_p{p50:.2f}_m{profit:.2f}"


def point_key(theta: float, p50: float, profit: float) -> tuple[float, float, float]:
    return (round(float(theta), 2), round(float(p50), 2), round(float(profit), 2))


def remaining_seconds(sample_seconds: list[float], n_done: int, n_total: int) -> float:
    """Mean of the samples already timed, times the cells still to run."""
    if n_done <= 0 or not sample_seconds:
        raise ValueError("ETA needs at least one timed sample")
    if n_total < n_done:
        raise ValueError("n_total is smaller than n_done")
    mean = sum(sample_seconds) / len(sample_seconds)
    return mean * (n_total - n_done)


def format_eta(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes, sec = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}ч {minutes}мин"
    if minutes:
        return f"{minutes}мин {sec}с"
    return f"{sec}с"


def mark_keys_for_holds(
    run: RunResult,
    *,
    first_bucket_s: int,
    last_bucket_s: int,
) -> set[tuple[str, int]]:
    """5-minute close-side marks while a policy hold is open. No book I/O.

    A mark at the last second of a bucket counts when
    ``ts_open <= mark < ts_close``. A still-open leg counts through the last
    bucket of the window.
    """
    keys: set[tuple[str, int]] = set()
    spans: list[tuple[str, int, int | None]] = [
        (str(trade.coin).upper(), int(trade.ts_open), int(trade.ts_close))
        for trade in run.trades
    ]
    if run.open_coin is not None and run.open_ts is not None:
        spans.append((str(run.open_coin).upper(), int(run.open_ts), None))
    last_mark = bucket_5m_mark_ts(last_bucket_s)
    for coin, ts_open, ts_close in spans:
        end = last_mark + 1 if ts_close is None else ts_close
        bucket = align_bucket_5m_start(max(first_bucket_s, ts_open - (bucket_5m_mark_ts(0))))
        if bucket < first_bucket_s:
            bucket = first_bucket_s
        while bucket <= last_bucket_s:
            mark = bucket_5m_mark_ts(bucket)
            if mark >= end:
                break
            if mark >= ts_open:
                keys.add((coin, mark))
            bucket += 300
    return keys


def book_keys_for_run(
    run: RunResult,
    *,
    first_bucket_s: int,
    last_bucket_s: int,
) -> set[tuple[str, int]]:
    return timestamp_keys_for_run(run) | mark_keys_for_holds(
        run, first_bucket_s=first_bucket_s, last_bucket_s=last_bucket_s
    )


def applied_closes(steps: list[LedgerStep]) -> list[LedgerStep]:
    return [step for step in steps if step.status == "closed"]


def cell_metrics(steps: list[LedgerStep]) -> dict:
    """Four heatmap numbers plus hole / mismatch / open-leg side fields."""
    closed = applied_closes(steps)
    nets = [
        round_net_usd(
            float(step.pnl_bybit or 0.0),
            float(step.pnl_okx or 0.0),
            float(step.fee_bybit or 0.0),
            float(step.fee_okx or 0.0),
        )
        for step in closed
    ]
    holds = [
        (int(step.ts_close) - int(step.ts_open)) / 3600.0
        for step in closed
        if step.ts_open is not None and step.ts_close is not None
    ]
    n = len(closed)
    total = float(sum(nets))
    open_step = next((step for step in steps if step.status == "open"), None)
    return {
        "total_profit": total,
        "avg_profit": (total / n) if n else None,
        "mean_hold_h": (sum(holds) / len(holds)) if holds else None,
        "n_trades": n,
        "n_hole": sum(1 for step in steps if step.status == "skipped_hole"),
        "n_mismatch": sum(1 for step in steps if step.status == "price_mismatch"),
        "open_coin": None if open_step is None else open_step.coin,
        "open_side": None if open_step is None else open_step.side,
        **trade_concentration(closed),
    }


def _iso_week_label(ts_s: int) -> str:
    iso = datetime.fromtimestamp(int(ts_s), tz=timezone.utc).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def trade_concentration(closed: list[LedgerStep]) -> dict:
    """Coin and calendar concentration of applied closes. Empty when there are none."""
    empty = {
        "coin_top_share": None,
        "n_coins": None,
        "week_top_share": None,
        "max_gap_h": None,
        "top_coin": None,
        "top_week": None,
    }
    if not closed:
        return empty
    by_coin: dict[str, int] = {}
    by_week: dict[str, int] = {}
    times: list[int] = []
    for step in closed:
        by_coin[step.coin] = by_coin.get(step.coin, 0) + 1
        if step.ts_close is not None:
            label = _iso_week_label(int(step.ts_close))
            by_week[label] = by_week.get(label, 0) + 1
            times.append(int(step.ts_close))
    n = len(closed)
    top_coin = max(by_coin, key=by_coin.get)
    top_week = max(by_week, key=by_week.get) if by_week else None
    gap = None
    if len(times) >= 2:
        times.sort()
        gap = max(times[i + 1] - times[i] for i in range(len(times) - 1)) / 3600.0
    return {
        "coin_top_share": by_coin[top_coin] / n,
        "n_coins": len(by_coin),
        "week_top_share": (by_week[top_week] / n) if top_week is not None else None,
        "max_gap_h": gap,
        "top_coin": top_coin,
        "top_week": top_week,
    }


def position_time_ratio(exposure_s: int, span_s: int) -> float | None:
    """Time in the slot divided by time flat. Empty when the slot never goes flat."""
    flat = int(span_s) - int(exposure_s)
    if int(span_s) <= 0 or flat <= 0:
        return None
    return int(exposure_s) / flat


def coin_rounds_from_steps(steps: list[LedgerStep]) -> list[dict]:
    rounds = []
    for step in applied_closes(steps):
        pnl_b = float(step.pnl_bybit or 0.0)
        pnl_o = float(step.pnl_okx or 0.0)
        fee_b = float(step.fee_bybit or 0.0)
        fee_o = float(step.fee_okx or 0.0)
        rounds.append(
            {
                "coin": step.coin,
                "side": step.side,
                "ts_close": int(step.ts_close or 0),
                "net": round_net_usd(pnl_b, pnl_o, fee_b, fee_o),
                "pnl": pnl_b + pnl_o,
                "fees": fee_b + fee_o,
            }
        )
    return rounds


def _pilot_order(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Frozen cell first when it still needs a run, then the two corners, then the rest."""
    wanted = set(points)
    preferred = (
        FROZEN_POINT,
        (THETA_OPEN[0], P50_OPEN[0], MIN_PROFIT_PP[0]),
        (THETA_OPEN[-1], P50_OPEN[-1], MIN_PROFIT_PP[-1]),
    )
    head = [point for point in preferred if point in wanted]
    seen = set(head)
    return head + [point for point in points if point not in seen]


def reusable_cells(
    cells: list[CellRun],
    wanted: list[tuple[float, float, float]],
) -> list[CellRun]:
    """Keep checkpoint rows whose rounded parameters sit on the new grid."""
    wanted_set = set(wanted)
    out: list[CellRun] = []
    seen: set[tuple[float, float, float]] = set()
    for cell in cells:
        key = point_key(cell.theta_open, cell.p50_open, cell.min_profit_pp)
        if key not in wanted_set or key in seen:
            continue
        out.append(cell)
        seen.add(key)
    return out


def _run_one(store, point: tuple[float, float, float]) -> CellRun:
    theta, p50, profit = point
    t0 = time.perf_counter()
    result = drop_terminal_delisted_open(run_combo(store, params_for(theta, p50, profit)))
    fill_pp, exit_pp = open_fill_exit_pp(store, result)
    return CellRun(
        theta_open=theta,
        p50_open=p50,
        min_profit_pp=profit,
        run=result,
        open_fill_pp=fill_pp,
        open_exit_pp=exit_pp,
        combo_s=time.perf_counter() - t0,
    )


def run_combos(
    store,
    points: list[tuple[float, float, float]],
    *,
    on_batch=None,
) -> list[CellRun]:
    """Pilot, print the remaining-time estimate, then the rest of the grid."""
    ordered = _pilot_order(points)
    if not ordered:
        return []
    pilot = min(PILOT_N, len(ordered))
    done: list[CellRun] = []
    for i, point in enumerate(ordered):
        if i == pilot and pilot < len(ordered):
            eta = remaining_seconds([cell.combo_s for cell in done], pilot, len(ordered))
            print(
                "ETA remaining combos: "
                f"{format_eta(eta)} ({eta:.0f}s) "
                f"after {pilot} samples, {len(ordered) - pilot} left, "
                f"mean {sum(c.combo_s for c in done) / pilot:.2f}s/combo. "
                "Book load and cell pages are extra and start after every combo.",
                flush=True,
            )
        cell = _run_one(store, point)
        done.append(cell)
        print(
            f"  combo {i + 1}/{len(ordered)} {cell_stem(cell.theta_open, cell.p50_open, cell.min_profit_pp)} "
            f"closed={cell.run.n_closed} open_end={0 if cell.run.open_coin is None else 1} "
            f"{cell.combo_s:.2f}s",
            flush=True,
        )
        if on_batch is not None and (i + 1) % 40 == 0:
            on_batch(done)
    return done


def _dump_checkpoint(path: Path, cells: list[CellRun]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for cell in cells:
        run = cell.run
        payload.append(
            {
                "theta_open": cell.theta_open,
                "p50_open": cell.p50_open,
                "min_profit_pp": cell.min_profit_pp,
                "combo_s": cell.combo_s,
                "open_fill_pp": cell.open_fill_pp,
                "open_exit_pp": cell.open_exit_pp,
                "open_coin": run.open_coin,
                "open_side": run.open_side,
                "open_ts": run.open_ts,
                "mtm_pp": run.mtm_pp,
                "mtm_ts": run.mtm_ts,
                "exposure_s": run.exposure_s,
                "trades": [
                    {
                        "coin": trade.coin,
                        "side": trade.side,
                        "ts_open": trade.ts_open,
                        "ts_close": trade.ts_close,
                        "fill_spread_pp": trade.fill_spread_pp,
                        "exit_spread_pp": trade.exit_spread_pp,
                        "potential_pp": trade.potential_pp,
                        "reason_open": trade.reason_open,
                        "reason_close": trade.reason_close,
                    }
                    for trade in run.trades
                ],
            }
        )
    path.write_text(json.dumps(payload), encoding="utf-8")


def _load_checkpoint(path: Path) -> list[CellRun]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cells = []
    for row in raw:
        trades = [
            ClosedTrade(
                coin=trade["coin"],
                side=trade["side"],
                ts_open=int(trade["ts_open"]),
                ts_close=int(trade["ts_close"]),
                fill_spread_pp=float(trade["fill_spread_pp"]),
                exit_spread_pp=float(trade["exit_spread_pp"]),
                potential_pp=float(trade["potential_pp"]),
                reason_open=trade["reason_open"],
                reason_close=trade["reason_close"],
            )
            for trade in row["trades"]
        ]
        cells.append(
            CellRun(
                theta_open=float(row["theta_open"]),
                p50_open=float(row["p50_open"]),
                min_profit_pp=float(row["min_profit_pp"]),
                run=RunResult(
                    trades=trades,
                    open_coin=row["open_coin"],
                    open_side=row["open_side"],
                    open_ts=None if row["open_ts"] is None else int(row["open_ts"]),
                    mtm_pp=float(row["mtm_pp"]),
                    mtm_ts=None if row["mtm_ts"] is None else int(row["mtm_ts"]),
                    exposure_s=int(row["exposure_s"]),
                ),
                open_fill_pp=None if row["open_fill_pp"] is None else float(row["open_fill_pp"]),
                open_exit_pp=None if row["open_exit_pp"] is None else float(row["open_exit_pp"]),
                combo_s=float(row["combo_s"]),
            )
        )
    return cells


def _fmt_metric(key: str, value) -> str:
    if value is None:
        return "—"
    if key in INTEGER_METRICS:
        return str(int(value))
    return f"{float(value):.2f}"


def render_index_html(grid: dict) -> str:
    """Static heatmap. Grid JSON is inlined so the page opens as ``file://``."""
    payload = json.dumps(grid, ensure_ascii=False)
    metric_options = "\n".join(
        f'<option value="{html.escape(key)}">{html.escape(label)}</option>'
        for key, label in METRICS
    )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Gear 2.2 — рельеф параметров</title>
<style>
  body {{ margin: 0; background: #070b10; color: #d7e2ea; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}
  .shell {{ padding: 18px 22px 32px; }}
  h1 {{ font-size: 1.15rem; font-weight: 560; letter-spacing: 0.04em; margin: 0 0 10px; color: #e8f4ff; }}
  .warn {{ background: #101820; border: 1px solid #1e3a4c; padding: 10px 12px; line-height: 1.45; max-width: 1100px; color: #b7c9d6; }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 12px 18px; margin: 14px 0; align-items: end; }}
  label {{ font-size: 0.75rem; letter-spacing: 0.06em; text-transform: uppercase; color: #8aa0b4; display: flex; flex-direction: column; gap: 4px; }}
  select {{ font: inherit; font-size: 0.9rem; padding: 6px 8px; background: #0c141c; color: #e8f4ff; border: 1px solid #2a4558; }}
  #stage {{ position: relative; overflow: auto; border: 1px solid #1c2c3a; background: #0a1016; }}
  #tip {{
    position: fixed; display: none; z-index: 5; pointer-events: none;
    background: #0e1720; color: #e8f4ff; padding: 8px 10px; border: 1px solid #3ddcff;
    font-size: 12px; line-height: 1.45; max-width: 320px;
  }}
  .note {{ color: #8aa0b4; font-size: 0.82rem; max-width: 1100px; }}
  a {{ color: #3ddcff; }}
</style>
</head>
<body>
<div class="shell">
<h1>GEAR 2.2 · РЕЛЬЕФ · МЕТОД 3</h1>
<div class="warn">
Это observation accounting по проведённым закрытиям <code>balance_equalize</code>.
Квадрат — одна точка сетки, не среднее по интервалу.
<code>min_theta_close = 0.05</code>. Дыры и price_mismatch не входят в четыре метрики.
Открытая в конце нога в четыре числа не входит. Всё ещё открытая ICX снята как делистинг.
Не оценка прибыльности и не live.
</div>
<p class="note"><a href="volume.html">Трёхмерная карта тех же трёх параметров</a></p>
<div class="controls">
  <label>метрика<select id="metric">{metric_options}</select></label>
  <label>ось X<select id="axis-x"></select></label>
  <label>ось Y<select id="axis-y"></select></label>
  <label>фиксированная ось<select id="axis-fixed"></select></label>
</div>
<p class="note" id="slice-note"></p>
<div id="stage"></div>
<div id="tip"></div>
</div>
<script>
const GRID = {payload};
const AXES = [
  {{key: "theta_open", label: "theta_open"}},
  {{key: "p50_open", label: "p50_open"}},
  {{key: "min_profit_pp", label: "min_profit_pp"}}
];
const MONEY = new Set(["total_profit", "avg_profit"]);
const SQUARE = 0.05;
const state = {{
  metric: "total_profit",
  x: "theta_open",
  y: "p50_open",
  fixed: "min_profit_pp",
  fixedValue: 0.20
}};

function uniq(key) {{
  return [...new Set(GRID.cells.map(c => c[key]))].sort((a, b) => a - b);
}}
function fmtNum(key, value) {{
  if (value === null || value === undefined) return "—";
  if (key === "n_trades" || key === "n_coins") return String(value);
  return Number(value).toFixed(2);
}}
function fmtAxis(value) {{
  return Number(value).toFixed(2);
}}

function fillSelect(el, options, selected) {{
  el.innerHTML = options.map(o => {{
    const sel = String(o.value) === String(selected) ? " selected" : "";
    return `<option value="${{o.value}}"${{sel}}>${{o.label}}</option>`;
  }}).join("");
}}

function syncControls() {{
  fillSelect(document.getElementById("axis-x"), AXES.map(a => ({{value: a.key, label: a.label}})), state.x);
  fillSelect(document.getElementById("axis-y"), AXES.map(a => ({{value: a.key, label: a.label}})), state.y);
  const fixedKey = AXES.map(a => a.key).find(k => k !== state.x && k !== state.y);
  state.fixed = fixedKey;
  const values = uniq(fixedKey);
  if (!values.includes(state.fixedValue)) state.fixedValue = values.includes(0.20) ? 0.20 : values[0];
  const fixedLabel = AXES.find(a => a.key === fixedKey).label;
  fillSelect(
    document.getElementById("axis-fixed"),
    values.map(v => ({{value: v, label: fixedLabel + " = " + fmtAxis(v)}})),
    state.fixedValue
  );
}}

function scaleOf(metric) {{
  const vals = GRID.cells.map(c => c[metric]).filter(v => v !== null && v !== undefined);
  if (!vals.length) return {{min: 0, max: 1, money: MONEY.has(metric)}};
  return {{min: Math.min(...vals), max: Math.max(...vals), money: MONEY.has(metric)}};
}}

function colorOf(value, scale) {{
  if (value === null || value === undefined) return "#e9ecef";
  if (scale.money) {{
    const ext = Math.max(Math.abs(scale.min), Math.abs(scale.max), 1e-9);
    const t = Math.max(-1, Math.min(1, value / ext));
    if (t >= 0) return mix("#f8f9fa", "#2b8a3e", t);
    return mix("#f8f9fa", "#c92a2a", -t);
  }}
  const span = scale.max - scale.min || 1;
  return mix("#edf2ff", "#1c3fad", (value - scale.min) / span);
}}

function mix(a, b, t) {{
  const pa = [1, 3, 5].map(i => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map(i => parseInt(b.slice(i, i + 2), 16));
  const c = pa.map((v, i) => Math.round(v + (pb[i] - v) * t));
  return "#" + c.map(v => v.toString(16).padStart(2, "0")).join("");
}}

function textOn(fill) {{
  const r = parseInt(fill.slice(1, 3), 16);
  const g = parseInt(fill.slice(3, 5), 16);
  const b = parseInt(fill.slice(5, 7), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? "#081018" : "#f4f8fb";
}}

function render() {{
  if (state.x === state.y) state.y = AXES.map(a => a.key).find(k => k !== state.x);
  syncControls();
  const scale = scaleOf(state.metric);
  const slice = GRID.cells.filter(c => Math.abs(c[state.fixed] - Number(state.fixedValue)) < 1e-9);
  const xs = uniq(state.x);
  const ys = uniq(state.y);
  const padL = 78, padR = 168, padT = 28, padB = 46;
  const pxPerUnit = 72 / SQUARE;
  const xMin = Math.min(...xs) - SQUARE / 2;
  const xMax = Math.max(...xs) + SQUARE / 2;
  const yMin = Math.min(...ys) - SQUARE / 2;
  const yMax = Math.max(...ys) + SQUARE / 2;
  const plotW = (xMax - xMin) * pxPerUnit;
  const plotH = (yMax - yMin) * pxPerUnit;
  const width = padL + plotW + padR;
  const height = padT + plotH + padB;
  const side = SQUARE * pxPerUnit;
  const xCenter = v => padL + (v - xMin) * pxPerUnit;
  const yCenter = v => padT + (yMax - v) * pxPerUnit;
  const parts = [];
  parts.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${{width}}" height="${{height}}" viewBox="0 0 ${{width}} ${{height}}">`);
  xs.forEach(v => {{
    parts.push(`<text x="${{xCenter(v)}}" y="${{height - 16}}" text-anchor="middle" font-size="12" fill="#9fb3c4">${{fmtAxis(v)}}</text>`);
  }});
  ys.forEach(v => {{
    parts.push(`<text x="${{padL - 8}}" y="${{yCenter(v) + 4}}" text-anchor="end" font-size="12" fill="#9fb3c4">${{fmtAxis(v)}}</text>`);
  }});
  const xLabel = AXES.find(a => a.key === state.x).label;
  const yLabel = AXES.find(a => a.key === state.y).label;
  parts.push(`<text x="${{padL}}" y="16" font-size="12" fill="#3ddcff">${{yLabel}} ↑</text>`);
  slice.forEach(cell => {{
    const cx = xCenter(cell[state.x]);
    const cy = yCenter(cell[state.y]);
    const fill = colorOf(cell[state.metric], scale);
    const frozen = cell.frozen ? ' stroke="#3ddcff" stroke-width="3"' : ' stroke="#10202c" stroke-width="1"';
    const label = fmtNum(state.metric, cell[state.metric]);
    const ink = textOn(fill);
    parts.push(`<a href="${{cell.href}}"><rect class="cell" x="${{cx - side / 2}}" y="${{cy - side / 2}}" width="${{side}}" height="${{side}}" fill="${{fill}}"${{frozen}} data-i="${{cell.i}}"/></a>`);
    parts.push(`<text x="${{cx}}" y="${{cy + 4}}" text-anchor="middle" font-size="11" fill="${{ink}}" pointer-events="none">${{label}}</text>`);
  }});
  const barX = padL + plotW + 36;
  const barY = padT;
  const barH = plotH;
  const cMax = scale.money ? Math.max(Math.abs(scale.min), Math.abs(scale.max)) : scale.max;
  const cMin = scale.money ? -cMax : scale.min;
  for (let i = 0; i < 48; i++) {{
    const shown = cMax + (cMin - cMax) * (i / 47);
    parts.push(`<rect x="${{barX}}" y="${{barY + barH * i / 48}}" width="16" height="${{barH / 48 + 0.8}}" fill="${{colorOf(shown, scale)}}"/>`);
  }}
  parts.push(`<text x="${{barX + 22}}" y="${{barY + 10}}" font-size="11" fill="#9fb3c4">${{fmtNum(state.metric, cMax)}}</text>`);
  if (scale.money) {{
    parts.push(`<text x="${{barX + 22}}" y="${{barY + barH / 2}}" font-size="11" fill="#9fb3c4">0</text>`);
  }}
  parts.push(`<text x="${{barX + 22}}" y="${{barY + barH}}" font-size="11" fill="#9fb3c4">${{fmtNum(state.metric, cMin)}}</text>`);
  parts.push("</svg>");
  document.getElementById("stage").innerHTML = parts.join("");
  const fixedLabel = AXES.find(a => a.key === state.fixed).label;
  const metricLabel = GRID.metric_labels[state.metric];
  document.getElementById("slice-note").textContent =
    metricLabel + " · " + xLabel + " × " + yLabel + " · " + fixedLabel + " = " + fmtAxis(state.fixedValue)
    + " · шкала по всем " + GRID.cells.length + " точкам"
    + (scale.money ? " · ноль на цветовой шкале" : "")
    + " · обведена текущая точка (0.50, 0.60, 0.20). Квадрат шириной " + SQUARE + " в координатах параметров.";
  document.querySelectorAll("rect.cell").forEach(node => {{
    node.addEventListener("mousemove", ev => showTip(ev, GRID.cells[Number(node.dataset.i)]));
    node.addEventListener("mouseleave", () => {{ document.getElementById("tip").style.display = "none"; }});
  }});
}}

function showTip(ev, cell) {{
  const tip = document.getElementById("tip");
  const open = cell.open_coin ? (cell.open_coin + " " + cell.open_side) : "нет";
  tip.innerHTML = [
    "theta_open " + fmtAxis(cell.theta_open),
    "p50_open " + fmtAxis(cell.p50_open),
    "min_profit_pp " + fmtAxis(cell.min_profit_pp),
    "суммарная прибыль " + fmtNum("total_profit", cell.total_profit) + " $",
    "средний профит " + fmtNum("avg_profit", cell.avg_profit) + " $",
    "держание " + fmtNum("mean_hold_h", cell.mean_hold_h) + " ч",
    "сделок " + fmtNum("n_trades", cell.n_trades),
    "лучшая монета " + (cell.top_coin || "—") + " · доля " + fmtNum("coin_top_share", cell.coin_top_share),
    "монет " + fmtNum("n_coins", cell.n_coins),
    "плотная неделя " + (cell.top_week || "—") + " · доля " + fmtNum("week_top_share", cell.week_top_share),
    "простой " + fmtNum("max_gap_h", cell.max_gap_h) + " ч",
    "в позиции / вне " + fmtNum("position_ratio", cell.position_ratio),
    "дыры " + cell.n_hole + " · mismatch " + cell.n_mismatch,
    "открыто в конце: " + open
  ].join("<br>");
  tip.style.display = "block";
  tip.style.left = (ev.clientX + 14) + "px";
  tip.style.top = (ev.clientY + 14) + "px";
}}

document.getElementById("metric").addEventListener("change", ev => {{ state.metric = ev.target.value; render(); }});
document.getElementById("axis-x").addEventListener("change", ev => {{ state.x = ev.target.value; render(); }});
document.getElementById("axis-y").addEventListener("change", ev => {{ state.y = ev.target.value; render(); }});
document.getElementById("axis-fixed").addEventListener("change", ev => {{
  state.fixedValue = Number(ev.target.value);
  render();
}});
render();
</script>
</body>
</html>
"""


def build_grid_payload(rows: list[dict]) -> dict:
    cells = []
    for i, row in enumerate(rows):
        cells.append({**row, "i": i})
    return {
        "square": SQUARE,
        "min_theta_close": MIN_THETA_CLOSE,
        "axes": {
            "theta_open": list(THETA_OPEN),
            "p50_open": list(P50_OPEN),
            "min_profit_pp": list(MIN_PROFIT_PP),
        },
        "metric_labels": {key: label for key, label in METRICS},
        "cells": cells,
    }


def _cell_page(
    path: Path,
    cell: CellRun,
    steps: list[LedgerStep],
    books: dict,
    bucket_starts: list[int],
    *,
    first_s: int,
    last_s: int,
    span_s: int,
) -> dict:
    metrics = cell_metrics(steps)
    metrics["position_ratio"] = position_time_ratio(cell.run.exposure_s, span_s)
    points = equity_series_5m(steps, books, bucket_starts)
    compared = approx_vs_cash_usd_5m(steps, books, bucket_starts, equity=points)
    n_marked, n_gap, n_flat = coverage_counts_5m(steps, bucket_starts, points)
    label = (
        f"balance_equalize  θ={cell.theta_open:.2f}  "
        f"p50={cell.p50_open:.2f}  min_profit={cell.min_profit_pp:.2f}"
    )
    stem = cell_stem(cell.theta_open, cell.p50_open, cell.min_profit_pp)
    open_note = "Открытой ноги в конце нет."
    if metrics["open_coin"]:
        open_note = f"В конце открыта {metrics['open_coin']} {metrics['open_side']} (в четыре метрики не входит)."
    notes = [
        (
            f"theta_open={cell.theta_open:.2f}, p50_open={cell.p50_open:.2f}, "
            f"min_profit_pp={cell.min_profit_pp:.2f}, min_theta_close={MIN_THETA_CLOSE:.2f}. "
            "Пул canary-30 с первой секунды. Метод 3: после закрытия кэш делится поровну, "
            "следующая нога = (Bybit + OKX) / 2."
        ),
        (
            f"Проведённых закрытий {metrics['n_trades']}, дыр {metrics['n_hole']}, "
            f"price_mismatch {metrics['n_mismatch']}. Сумма net {metrics['total_profit']:.2f} $. "
            + open_note
        ),
        "Расхождение приближения и истины: " + _divergence_note(compared) + ".",
        "Всё ещё открытая ICX снята как делистинг. Закрытые ICX остаются. Не прибыль и не live.",
    ]
    rounds = coin_rounds_from_steps(steps)
    extra = coin_pnl_section_html(rounds) if rounds else "<p class='note'>Закрытых сделок нет.</p>"
    write_balance_5m_html(
        path,
        mode_series=[(label, bucket_starts, points)],
        notes=notes,
        first_s=first_s,
        last_s=last_s,
        gap_counts=[(label, n_marked, n_gap, n_flat)],
        profit_series=[(label, bucket_starts, compared)],
        extra_html=extra,
        title=f"Gear 2.2 — {stem}",
        heading=f"Метод 3 · {stem}",
        warn=(
            "Это <strong>observation accounting</strong> кассы метода 3 на одной точке сетки: "
            f"<code>theta_open={cell.theta_open:.2f}</code>, "
            f"<code>p50_open={cell.p50_open:.2f}</code>, "
            f"<code>min_profit_pp={cell.min_profit_pp:.2f}</code>, "
            f"<code>min_theta_close={MIN_THETA_CLOSE:.2f}</code>. "
            "Окно hive <strong>2026-08-10</strong> … последний день (UTC). Старт cash $100 / $100. "
            "Один пункт на UTC 5-минутный бакет, цена стакана — на последней секунде бакета. "
            "Ниже — приближение и истина, затем net по монетам. "
            "Дыры и price_mismatch в сумму не входят. Не оценка прибыльности и не live."
        ),
        lead_html="<p class='note'><a href='../index.html'>← рельеф параметров</a></p>",
        source_note="источник: run_combo + касса balance_equalize · L1: lean_ticks + gear22_books_1hz.",
        extra_style=TERMINAL_CELL_STYLE,
    )
    frozen = (
        abs(cell.theta_open - FROZEN_POINT[0]) < 1e-9
        and abs(cell.p50_open - FROZEN_POINT[1]) < 1e-9
        and abs(cell.min_profit_pp - FROZEN_POINT[2]) < 1e-9
    )
    return {
        "theta_open": cell.theta_open,
        "p50_open": cell.p50_open,
        "min_profit_pp": cell.min_profit_pp,
        "frozen": frozen,
        "href": f"cells/{stem}.html",
        **metrics,
    }


def render_volume_html(grid: dict) -> str:
    """Orbit view of the same grid. JSON is inlined so the page opens as ``file://``."""
    payload = json.dumps(grid, ensure_ascii=False)
    metric_options = "\n".join(
        f'<option value="{html.escape(key)}">{html.escape(label)}</option>'
        for key, label in METRICS
    )
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Gear 2.2 — трёхмерная карта параметров</title>
<style>
  body {{ margin: 0; background: #070b10; color: #d7e2ea; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }}
  header {{ display: flex; flex-wrap: wrap; gap: 14px 22px; align-items: end; padding: 14px 18px 8px; }}
  h1 {{ font-size: 1.05rem; letter-spacing: 0.04em; margin: 0; color: #e8f4ff; }}
  a {{ color: #3ddcff; }}
  label {{ font-size: 0.72rem; letter-spacing: 0.06em; text-transform: uppercase; color: #8aa0b4; display: flex; flex-direction: column; gap: 4px; }}
  select, button {{ font: inherit; font-size: 0.88rem; padding: 6px 8px; background: #0c141c; color: #e8f4ff; border: 1px solid #2a4558; }}
  button {{ cursor: pointer; }}
  #wrap {{ position: relative; height: calc(100vh - 92px); }}
  canvas {{ width: 100%; height: 100%; display: block; background: #0a1016; }}
  #tip {{
    position: fixed; display: none; z-index: 5; pointer-events: none;
    background: #0e1720; color: #e8f4ff; padding: 8px 10px; border: 1px solid #3ddcff;
    font-size: 12px; line-height: 1.45; max-width: 320px;
  }}
  .note {{ color: #8aa0b4; font-size: 0.78rem; padding: 0 18px 10px; }}
</style>
</head>
<body>
<header>
  <h1>GEAR 2.2 · ОБЪЁМ ПАРАМЕТРОВ</h1>
  <a href="index.html">плоская карта</a>
  <label>метрика<select id="metric">{metric_options}</select></label>
  <button type="button" id="reset">вид сначала</button>
</header>
<p class="note">Оси в реальных значениях: X theta_open, Y p50_open вверх, Z min_profit_pp. Кубик — одна точка, не среднее по объёму. Тяните, чтобы повернуть, колесо — масштаб. Клик открывает страницу точки. Бирюзовая кромка — текущая точка (0.50, 0.60, 0.20). Наблюдение, не прибыль и не live.</p>
<div id="wrap"><canvas id="view"></canvas></div>
<div id="tip"></div>
<script>
const GRID = {payload};
const MONEY = new Set(["total_profit", "avg_profit"]);
const HALF = 0.025;
const state = {{ metric: "total_profit", yaw: -0.7, pitch: 0.45, dist: 2.4 }};
const canvas = document.getElementById("view");
const ctx = canvas.getContext("2d");
let hover = null;

function fmtNum(key, value) {{
  if (value === null || value === undefined) return "—";
  if (key === "n_trades" || key === "n_coins") return String(value);
  return Number(value).toFixed(2);
}}
function fmtAxis(value) {{ return Number(value).toFixed(2); }}
function uniq(key) {{
  return [...new Set(GRID.cells.map(c => c[key]))].sort((a, b) => a - b);
}}
function scaleOf(metric) {{
  const vals = GRID.cells.map(c => c[metric]).filter(v => v !== null && v !== undefined);
  if (!vals.length) return {{min: 0, max: 1, money: MONEY.has(metric)}};
  return {{min: Math.min(...vals), max: Math.max(...vals), money: MONEY.has(metric)}};
}}
function mix(a, b, t) {{
  const pa = [1, 3, 5].map(i => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map(i => parseInt(b.slice(i, i + 2), 16));
  const c = pa.map((v, i) => Math.round(v + (pb[i] - v) * t));
  return "#" + c.map(v => v.toString(16).padStart(2, "0")).join("");
}}
function colorOf(value, scale) {{
  if (value === null || value === undefined) return "#2a3542";
  if (scale.money) {{
    const ext = Math.max(Math.abs(scale.min), Math.abs(scale.max), 1e-9);
    const t = Math.max(-1, Math.min(1, value / ext));
    if (t >= 0) return mix("#1a2330", "#2b8a3e", t);
    return mix("#1a2330", "#c92a2a", -t);
  }}
  const span = scale.max - scale.min || 1;
  return mix("#16324a", "#3ddcff", (value - scale.min) / span);
}}
function shade(hex, k) {{
  const n = [1, 3, 5].map(i => Math.max(0, Math.min(255, Math.round(parseInt(hex.slice(i, i + 2), 16) * k))));
  return "#" + n.map(v => v.toString(16).padStart(2, "0")).join("");
}}

const AX = {{
  x: uniq("theta_open"),
  y: uniq("p50_open"),
  z: uniq("min_profit_pp")
}};
const mid = {{
  x: (AX.x[0] + AX.x[AX.x.length - 1]) / 2,
  y: (AX.y[0] + AX.y[AX.y.length - 1]) / 2,
  z: (AX.z[0] + AX.z[AX.z.length - 1]) / 2
}};

function world(theta, p50, profit) {{
  return [theta - mid.x, p50 - mid.y, profit - mid.z];
}}
function rotate(p) {{
  const cy = Math.cos(state.yaw), sy = Math.sin(state.yaw);
  const x1 = cy * p[0] + sy * p[2];
  const z1 = -sy * p[0] + cy * p[2];
  const cp = Math.cos(state.pitch), sp = Math.sin(state.pitch);
  return [x1, cp * p[1] - sp * z1, sp * p[1] + cp * z1];
}}
function project(p, w, h) {{
  const z = p[2] + state.dist;
  const f = Math.min(w, h) * 0.92 / z;
  return [w / 2 + p[0] * f, h / 2 - p[1] * f, z];
}}

const FACES = [
  [[-1,-1,1],[1,-1,1],[1,1,1],[-1,1,1], 1.05],
  [[1,-1,-1],[-1,-1,-1],[-1,1,-1],[1,1,-1], 0.62],
  [[-1,1,-1],[1,1,-1],[1,1,1],[-1,1,1], 1.18],
  [[-1,-1,1],[1,-1,1],[1,-1,-1],[-1,-1,-1], 0.55],
  [[1,-1,-1],[1,-1,1],[1,1,1],[1,1,-1], 0.85],
  [[-1,-1,1],[-1,-1,-1],[-1,1,-1],[-1,1,1], 0.75]
];

function draw() {{
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = Math.max(1, Math.floor(w * dpr));
  canvas.height = Math.max(1, Math.floor(h * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const scale = scaleOf(state.metric);
  const faces = [];
  GRID.cells.forEach((cell, i) => {{
    const base = colorOf(cell[state.metric], scale);
    const o = world(cell.theta_open, cell.p50_open, cell.min_profit_pp);
    FACES.forEach(face => {{
      const view = face.slice(0, 4).map(s => rotate([
        o[0] + s[0] * HALF, o[1] + s[1] * HALF, o[2] + s[2] * HALF
      ]));
      const e1 = [view[1][0] - view[0][0], view[1][1] - view[0][1], view[1][2] - view[0][2]];
      const e2 = [view[3][0] - view[0][0], view[3][1] - view[0][1], view[3][2] - view[0][2]];
      const nz = e1[0] * e2[1] - e1[1] * e2[0];
      if (nz >= 0) return;
      const proj = view.map(p => project(p, w, h));
      const depth = (view[0][2] + view[1][2] + view[2][2] + view[3][2]) / 4;
      faces.push({{ proj, depth, color: shade(base, face[4]), i, frozen: cell.frozen }});
    }});
  }});
  faces.sort((a, b) => a.depth - b.depth);
  faces.forEach(face => {{
    ctx.beginPath();
    face.proj.forEach((p, k) => k ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    ctx.closePath();
    ctx.fillStyle = face.color;
    ctx.fill();
    if (face.frozen) {{
      ctx.strokeStyle = "#3ddcff";
      ctx.lineWidth = 1.4;
      ctx.stroke();
    }}
  }});
  drawAxes(w, h);
  hover = faces;
}}

function drawAxes(w, h) {{
  const ticks = [
    ...AX.x.map(v => ({{ p: world(v, AX.y[0] - 0.04, AX.z[0]), t: fmtAxis(v) }})),
    ...AX.y.map(v => ({{ p: world(AX.x[0] - 0.04, v, AX.z[0]), t: fmtAxis(v) }})),
    ...AX.z.map(v => ({{ p: world(AX.x[AX.x.length - 1] + 0.03, AX.y[0], v), t: fmtAxis(v) }}))
  ];
  ctx.fillStyle = "#9fb3c4";
  ctx.font = "11px ui-monospace, Menlo, Consolas, monospace";
  ticks.forEach(tick => {{
    const s = project(rotate(tick.p), w, h);
    ctx.fillText(tick.t, s[0], s[1]);
  }});
  const labels = [
    {{ p: world(mid.x, AX.y[0] - 0.1, AX.z[0]), t: "theta_open" }},
    {{ p: world(AX.x[0] - 0.12, mid.y, AX.z[0]), t: "p50_open" }},
    {{ p: world(AX.x[AX.x.length - 1] + 0.08, AX.y[0], mid.z), t: "min_profit_pp" }}
  ];
  ctx.fillStyle = "#3ddcff";
  labels.forEach(lab => {{
    const s = project(rotate(lab.p), w, h);
    ctx.fillText(lab.t, s[0], s[1]);
  }});
}}

function pointIn(proj, x, y) {{
  let inside = false;
  for (let i = 0, j = proj.length - 1; i < proj.length; j = i++) {{
    const xi = proj[i][0], yi = proj[i][1], xj = proj[j][0], yj = proj[j][1];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }}
  return inside;
}}

canvas.addEventListener("mousemove", ev => {{
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  if (!hover) return;
  let best = null;
  for (let k = hover.length - 1; k >= 0; k--) {{
    if (pointIn(hover[k].proj, x, y)) {{ best = hover[k]; break; }}
  }}
  const tip = document.getElementById("tip");
  if (!best) {{ tip.style.display = "none"; return; }}
  const cell = GRID.cells[best.i];
  const open = cell.open_coin ? (cell.open_coin + " " + cell.open_side) : "нет";
  tip.innerHTML = [
    "theta_open " + fmtAxis(cell.theta_open),
    "p50_open " + fmtAxis(cell.p50_open),
    "min_profit_pp " + fmtAxis(cell.min_profit_pp),
    GRID.metric_labels[state.metric] + " " + fmtNum(state.metric, cell[state.metric]),
    "лучшая монета " + (cell.top_coin || "—"),
    "открыто в конце: " + open
  ].join("<br>");
  tip.style.display = "block";
  tip.style.left = (ev.clientX + 14) + "px";
  tip.style.top = (ev.clientY + 14) + "px";
}});
canvas.addEventListener("mouseleave", () => {{ document.getElementById("tip").style.display = "none"; }});
canvas.addEventListener("click", ev => {{
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  if (!hover) return;
  for (let k = hover.length - 1; k >= 0; k--) {{
    if (pointIn(hover[k].proj, x, y)) {{
      location.href = GRID.cells[hover[k].i].href;
      return;
    }}
  }}
}});
let drag = null;
canvas.addEventListener("pointerdown", ev => {{ drag = {{ x: ev.clientX, y: ev.clientY, yaw: state.yaw, pitch: state.pitch }}; canvas.setPointerCapture(ev.pointerId); }});
canvas.addEventListener("pointermove", ev => {{
  if (!drag) return;
  state.yaw = drag.yaw + (ev.clientX - drag.x) * 0.008;
  state.pitch = Math.max(-1.2, Math.min(1.2, drag.pitch + (ev.clientY - drag.y) * 0.008));
  draw();
}});
canvas.addEventListener("pointerup", () => {{ drag = null; }});
canvas.addEventListener("wheel", ev => {{
  ev.preventDefault();
  state.dist = Math.max(1.15, Math.min(6, state.dist * (ev.deltaY > 0 ? 1.08 : 0.92)));
  draw();
}}, {{ passive: false }});
document.getElementById("metric").addEventListener("change", ev => {{ state.metric = ev.target.value; draw(); }});
document.getElementById("reset").addEventListener("click", () => {{
  state.yaw = -0.7; state.pitch = 0.45; state.dist = 2.4; draw();
}});
window.addEventListener("resize", draw);
draw();
</script>
</body>
</html>
"""


def write_viewer(out_dir: Path, rows: list[dict]) -> None:
    payload = build_grid_payload(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "grid.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "index.html").write_text(render_index_html(payload), encoding="utf-8")
    (out_dir / "volume.html").write_text(render_volume_html(payload), encoding="utf-8")


def _axis_bounds(books_dir: Path) -> tuple[int, int, list[int]]:
    last_second = _sidecar_last_second(books_dir)
    if last_second is None:
        raise FileNotFoundError(f"no 1 Hz book parts in {books_dir}")
    first_s = int(FIRST_BUCKET.timestamp())
    first_bucket = align_bucket_5m_start(first_s)
    last_bucket = align_bucket_5m_start(last_second)
    return first_bucket, last_bucket, iter_bucket_5m_starts(first_bucket, last_bucket)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gear 2.2 parameter-space relief (observation only).")
    parser.add_argument("--hive", type=Path, default=DEFAULT_HIVE)
    parser.add_argument("--ticks", type=Path, default=DEFAULT_TICKS)
    parser.add_argument("--books-1hz", type=Path, default=DEFAULT_BOOKS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--from-checkpoint", action="store_true")
    args = parser.parse_args(argv)

    points = grid_points()
    print(f"grid n={len(points)} min_theta_close={MIN_THETA_CLOSE}", flush=True)
    checkpoint = args.out / "combo_checkpoint.json"
    partial = args.out / "combo_checkpoint_partial.json"
    span_path = args.out / "span.json"
    loaded: list[CellRun] = []
    if checkpoint.is_file():
        loaded.extend(_load_checkpoint(checkpoint))
    if partial.is_file():
        loaded.extend(_load_checkpoint(partial))
    reused = reusable_cells(loaded, points)
    have = {point_key(cell.theta_open, cell.p50_open, cell.min_profit_pp) for cell in reused}
    missing = [point for point in points if point not in have]
    print(f"reuse={len(reused)} new={len(missing)}", flush=True)
    span_s: int | None = None
    if span_path.is_file():
        span_s = int(json.loads(span_path.read_text(encoding="utf-8"))["span_s"])
    if missing and not args.from_checkpoint:
        dates = list_hive_event_dates(args.hive)
        if not dates:
            print(f"no hive dates in {args.hive}")
            return 2
        print(f"loading hive {dates[0]}..{dates[-1]} n={len(dates)}", flush=True)
        t0 = time.perf_counter()
        store = store_from_hive(args.hive, dates=dates)
        span_s = int(store.span_s)
        span_path.parent.mkdir(parents=True, exist_ok=True)
        span_path.write_text(json.dumps({"span_s": span_s}), encoding="utf-8")
        print(
            f"  rows={store.n_rows} coins={len(store.coins)} span_s={span_s} "
            f"load_s={time.perf_counter() - t0:.1f}",
            flush=True,
        )

        def _save_partial(done: list[CellRun]) -> None:
            _dump_checkpoint(partial, reused + done)
            print(f"  checkpoint partial n={len(reused) + len(done)}", flush=True)

        fresh = run_combos(store, missing, on_batch=_save_partial)
        del store
        gc.collect()
        cells = reused + fresh
        _dump_checkpoint(checkpoint, cells)
        if partial.is_file():
            partial.unlink()
        print(f"wrote {checkpoint}", flush=True)
    else:
        cells = reused
        if args.from_checkpoint:
            print(f"from checkpoint reuse={len(cells)} missing={len(missing)}", flush=True)
            if missing:
                print("checkpoint does not cover the grid; rerun without --from-checkpoint")
                return 2

    if span_s is None:
        print("span_s missing; rerun so the hive load records it")
        return 2
    if len(cells) != len(points):
        print(f"expected {len(points)} combos, got {len(cells)}")
        return 2

    first_bucket, last_bucket, bucket_starts = _axis_bounds(args.books_1hz)
    print(
        f"axis {datetime.fromtimestamp(first_bucket, tz=timezone.utc):%Y-%m-%d %H:%M} .. "
        f"{datetime.fromtimestamp(last_bucket, tz=timezone.utc):%Y-%m-%d %H:%M} "
        f"buckets={len(bucket_starts)}",
        flush=True,
    )
    all_keys: set[tuple[str, int]] = set()
    for cell in cells:
        all_keys |= book_keys_for_run(
            cell.run, first_bucket_s=first_bucket, last_bucket_s=last_bucket
        )
    print(f"loading L1 for {len(all_keys)} keys", flush=True)
    t0 = time.perf_counter()
    books = load_last_books(args.ticks, all_keys, books_1hz_dir=args.books_1hz)
    print(f"  books found={len(books)} / {len(all_keys)} in {time.perf_counter() - t0:.1f}s", flush=True)

    cells_dir = args.out / "cells"
    rows = []
    for i, cell in enumerate(cells):
        rounds, open_leg = price_run(
            cell.run,
            books,
            open_fill_pp=cell.open_fill_pp,
            open_exit_pp=cell.open_exit_pp,
        )
        ledger = run_ledger(rounds, MODE, open_leg=open_leg)
        row = _cell_page(
            cells_dir / f"{cell_stem(cell.theta_open, cell.p50_open, cell.min_profit_pp)}.html",
            cell,
            ledger.steps,
            books,
            bucket_starts,
            first_s=first_bucket,
            last_s=last_bucket,
            span_s=span_s,
        )
        rows.append(row)
        if row["frozen"] or (i + 1) % 25 == 0:
            print(
                f"  page {i + 1}/{len(cells)} {row['href']} "
                f"net={row['total_profit']:.2f} n={row['n_trades']} "
                f"hole={row['n_hole']} mismatch={row['n_mismatch']}",
                flush=True,
            )
    rows.sort(key=lambda row: (row["theta_open"], row["p50_open"], row["min_profit_pp"]))
    for i, row in enumerate(rows):
        row["i"] = i
    write_viewer(args.out, rows)
    keep = {f"{cell_stem(*point)}.html" for point in points}
    removed = 0
    for path in cells_dir.glob("*.html"):
        if path.name not in keep:
            path.unlink()
            removed += 1
    print(f"removed stale cell pages={removed}", flush=True)
    frozen_rows = [row for row in rows if row["frozen"]]
    if len(frozen_rows) != 1:
        print("frozen cell missing from grid")
        return 2
    frozen = frozen_rows[0]
    print(
        f"frozen cell net={frozen['total_profit']:.4f} n={frozen['n_trades']} "
        f"hole={frozen['n_hole']} mismatch={frozen['n_mismatch']} "
        f"end_cash_sum={START_CASH * 2 + frozen['total_profit']:.4f}",
        flush=True,
    )
    print(f"wrote {args.out / 'index.html'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Per-coin realized cash for the frozen gear-2.2 ledger, as HTML.

Observation accounting only. Net of one closed round is price PnL on both
legs minus that round's taker fees. Equalize does not change the sum.
"""

from __future__ import annotations

import argparse
import csv
import html
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STEPS = REPO_ROOT / "research" / "data" / "venue_balance" / "frozen_steps.csv"
DEFAULT_OUT = REPO_ROOT / "research" / "data" / "venue_balance" / "coin_pnl.html"
HIGHLIGHT = "ICX"


def round_net_usd(pnl_bybit: float, pnl_okx: float, fee_bybit: float, fee_okx: float) -> float:
    """Cash change of one closed round, before equalize (the sum is unchanged)."""
    return pnl_bybit + pnl_okx - fee_bybit - fee_okx


def load_closed_rounds(
    path: Path,
    *,
    window: str = "all",
    mode: str = "balance_equalize",
    label: str = "frozen",
) -> list[dict]:
    """Closed rounds in file order. Skipped and still-open rows are omitted."""
    out: list[dict] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("window") != window or row.get("mode") != mode:
                continue
            if label and row.get("label") != label:
                continue
            if row.get("status") != "closed":
                continue
            pnl_b = float(row["pnl_bybit"])
            pnl_o = float(row["pnl_okx"])
            fee_b = float(row["fee_bybit"])
            fee_o = float(row["fee_okx"])
            out.append(
                {
                    "coin": str(row["coin"]),
                    "side": str(row["side"]),
                    "ts_close": int(float(row["ts_close"])),
                    "net": round_net_usd(pnl_b, pnl_o, fee_b, fee_o),
                    "pnl": pnl_b + pnl_o,
                    "fees": fee_b + fee_o,
                }
            )
    return out


def coin_totals(rounds: list[dict]) -> list[dict]:
    """One row per coin, largest net first."""
    acc: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "net": 0.0, "pnl": 0.0, "fees": 0.0}
    )
    for rnd in rounds:
        row = acc[rnd["coin"]]
        row["n"] += 1
        row["net"] += rnd["net"]
        row["pnl"] += rnd["pnl"]
        row["fees"] += rnd["fees"]
        if rnd["net"] > 1e-9:
            row["wins"] += 1
    total = sum(row["net"] for row in acc.values())
    ranked = []
    for coin, row in acc.items():
        ranked.append(
            {
                "coin": coin,
                "n": row["n"],
                "wins": row["wins"],
                "net": row["net"],
                "pnl": row["pnl"],
                "fees": row["fees"],
                "share": (row["net"] / total) if total else 0.0,
            }
        )
    ranked.sort(key=lambda item: item["net"], reverse=True)
    return ranked


def _bar_svg(ranked: list[dict], *, highlight: str) -> str:
    width = 980
    left = 88
    right = 120
    top = 28
    row_h = 26
    inner_w = width - left - right
    height = top + row_h * max(len(ranked), 1) + 28
    max_net = max((row["net"] for row in ranked), default=1.0)
    if max_net <= 0:
        max_net = 1.0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="net по монетам">',
        f'<text x="{left}" y="18" font-size="14" font-family="ui-sans-serif, system-ui, sans-serif" fill="#111">'
        "Реализованный net по монете, $</text>",
    ]
    for i, row in enumerate(ranked):
        y = top + i * row_h
        bar_w = inner_w * (row["net"] / max_net)
        color = "#c92a2a" if row["coin"] == highlight else "#1c7ed6"
        parts.append(
            f'<text x="{left - 8}" y="{y + 16}" text-anchor="end" font-size="12" '
            f'font-family="ui-sans-serif, system-ui, sans-serif" fill="#111">{html.escape(row["coin"])}</text>'
        )
        parts.append(
            f'<rect x="{left}" y="{y + 4}" width="{bar_w:.2f}" height="16" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{left + bar_w + 8:.2f}" y="{y + 16}" font-size="12" '
            f'font-family="ui-sans-serif, system-ui, sans-serif" fill="#333">'
            f'{row["net"]:.2f} $ · {row["n"]} сд.</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _cumulative_svg(rounds: list[dict], *, highlight: str) -> str:
    width = 980
    height = 360
    left, right, top, bottom = 64, 24, 36, 40
    inner_w = width - left - right
    inner_h = height - top - bottom
    cum = []
    total = 0.0
    for rnd in rounds:
        total += rnd["net"]
        cum.append(total)
    y_max = max(cum) if cum else 1.0
    y_min = min(0.0, min(cum) if cum else 0.0)
    span = y_max - y_min or 1.0
    n = max(len(cum) - 1, 1)

    def x_of(i: int) -> float:
        return left + inner_w * (i / n)

    def y_of(value: float) -> float:
        return top + inner_h * (1.0 - (value - y_min) / span)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="накопление net">',
        f'<text x="{left}" y="20" font-size="14" font-family="ui-sans-serif, system-ui, sans-serif" fill="#111">'
        "Накопление net по закрытиям, $</text>",
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + inner_h}" stroke="#bbb"/>',
        f'<line x1="{left}" y1="{y_of(0):.2f}" x2="{width - right}" y2="{y_of(0):.2f}" stroke="#ddd"/>',
    ]
    if cum:
        pts = " ".join(f"{x_of(i):.2f},{y_of(v):.2f}" for i, v in enumerate(cum))
        parts.append(f'<polyline fill="none" stroke="#1c7ed6" stroke-width="2" points="{pts}"/>')
        for i, rnd in enumerate(rounds):
            if rnd["coin"] != highlight:
                continue
            parts.append(
                f'<circle cx="{x_of(i):.2f}" cy="{y_of(cum[i]):.2f}" r="3.5" fill="#c92a2a"/>'
            )
    if rounds:
        first = datetime.fromtimestamp(rounds[0]["ts_close"], tz=timezone.utc).strftime("%Y-%m-%d")
        last = datetime.fromtimestamp(rounds[-1]["ts_close"], tz=timezone.utc).strftime("%Y-%m-%d")
        parts.append(
            f'<text x="{left}" y="{height - 8}" font-size="11" fill="#333" '
            f'font-family="ui-sans-serif, system-ui, sans-serif">{html.escape(first)} UTC</text>'
        )
        parts.append(
            f'<text x="{width - right}" y="{height - 8}" text-anchor="end" font-size="11" fill="#333" '
            f'font-family="ui-sans-serif, system-ui, sans-serif">{html.escape(last)} UTC</text>'
        )
    parts.append(
        f'<text x="{left + 8}" y="{top + 16}" font-size="12" fill="#c92a2a" '
        f'font-family="ui-sans-serif, system-ui, sans-serif">красные точки — закрытия {html.escape(highlight)}</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts)


def _table(ranked: list[dict], *, highlight: str) -> str:
    body = []
    for row in ranked:
        cls = ' class="icx"' if row["coin"] == highlight else ""
        body.append(
            "<tr{cls}><td>{coin}</td><td>{n}</td><td>{wins}</td>"
            "<td>{net:.2f}</td><td>{avg:.2f}</td><td>{fees:.2f}</td><td>{share:.0%}</td></tr>".format(
                cls=cls,
                coin=html.escape(row["coin"]),
                n=row["n"],
                wins=row["wins"],
                net=row["net"],
                avg=row["net"] / row["n"],
                fees=row["fees"],
                share=row["share"],
            )
        )
    return (
        "<table><thead><tr><th>монета</th><th>сделок</th><th>net &gt; 0</th>"
        "<th>net, $</th><th>среднее, $</th><th>комиссии, $</th><th>доля</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def coin_pnl_section_html(
    rounds: list[dict],
    *,
    highlight: str = HIGHLIGHT,
) -> str:
    """Fragment for the 5-minute page: bars, cumulative net, table."""
    ranked = coin_totals(rounds)
    total = sum(row["net"] for row in ranked)
    return (
        "<section id='coin-pnl'>\n"
        "<h2>Прибыль по монетам, balance_equalize, пул с ICX</h2>\n"
        "<p class='note'>Закрытые сделки третьего метода на полном пуле. "
        "Net = ценовой PnL обеих ног минус комиссии входа и выхода. "
        f"Закрытий {len(rounds)}, монет {len(ranked)}, сумма net {total:.2f} $. "
        f"Красный — {html.escape(highlight)}. "
        "Открытая в конце нога ICX сюда не входит.</p>\n"
        f"{_bar_svg(ranked, highlight=highlight)}\n"
        f"{_cumulative_svg(rounds, highlight=highlight)}\n"
        f"{_table(ranked, highlight=highlight)}\n"
        "</section>"
    )


def write_coin_pnl_html(
    path: Path,
    rounds: list[dict],
    *,
    highlight: str = HIGHLIGHT,
    window: str = "all",
    mode: str = "balance_equalize",
) -> None:
    ranked = coin_totals(rounds)
    total = sum(row["net"] for row in ranked)
    n = len(rounds)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Gear 2.2 — net по монетам</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 24px; color: #111; max-width: 1040px; }}
  h1 {{ font-size: 1.35rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
  .warn {{ background: #fff8e6; border: 1px solid #e6d08a; padding: 10px 12px; line-height: 1.45; }}
  .note {{ color: #333; font-size: 0.92rem; line-height: 1.45; }}
  svg {{ max-width: 100%; height: auto; display: block; border: 1px solid #eee; background: #fff; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.92rem; }}
  th, td {{ border-bottom: 1px solid #eee; text-align: right; padding: 4px 8px; }}
  th:first-child, td:first-child {{ text-align: left; }}
  tr.icx td {{ background: #fff5f5; font-weight: 600; }}
</style>
</head>
<body>
<h1>Прибыль по монетам — balance_equalize, пул с ICX</h1>
<div class="warn note">
Это <strong>observation accounting</strong> по закрытым сделкам frozen gear&nbsp;2.2,
метод 3: после закрытия кэш делится поровну, следующая нога = (Bybit + OKX) / 2.
Net сделки = ценовой PnL обеих ног минус комиссии входа и выхода.
Окно <code>{html.escape(window)}</code>, режим <code>{html.escape(mode)}</code>.
Открытая в конце нога ICX в эту сумму не входит: монету делистинговали.
Не оценка прибыльности и не live.
</div>
<p class="note">Закрытий: <strong>{n}</strong> · монет: <strong>{len(ranked)}</strong> ·
сумма net: <strong>{total:.2f} $</strong>. Красный — {html.escape(highlight)}.</p>
{coin_pnl_section_html(rounds, highlight=highlight)}
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-coin realized cash HTML (observation only).")
    parser.add_argument("--steps", type=Path, default=DEFAULT_STEPS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--window", default="all")
    parser.add_argument("--mode", default="balance_equalize")
    args = parser.parse_args(argv)
    rounds = load_closed_rounds(args.steps, window=args.window, mode=args.mode)
    if not rounds:
        print(f"no closed rounds in {args.steps} window={args.window} mode={args.mode}")
        return 2
    write_coin_pnl_html(args.out, rounds, window=args.window, mode=args.mode)
    ranked = coin_totals(rounds)
    total = sum(row["net"] for row in ranked)
    print(f"rounds={len(rounds)} coins={len(ranked)} net={total:.4f}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline phone-friendly PNG around a θ K=1 would_send trade.

Reads ``theta_trades/`` (+ optional ``theta/``) under ``BBOT_DATA_ROOT``.
Not on the hot path.

Usage::

    PYTHONPATH=. python -m app.bot.theta_trade_plot \\
      --data-root /data/bbot-gear22 \\
      --trade-id <uuid> \\
      --out /tmp/theta-trade.png
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional, Sequence


def discover_trade_files(data_root: Path) -> list[Path]:
    root = Path(data_root) / "theta_trades"
    if not root.is_dir():
        return []
    return sorted(root.glob("event_date=*/trades.jsonl"))


def discover_theta_files(data_root: Path) -> list[Path]:
    root = Path(data_root) / "theta"
    if not root.is_dir():
        return []
    return sorted(root.glob("event_date=*/metrics.jsonl"))


def load_jsonl(paths: Sequence[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def find_trade_events(
    rows: Sequence[dict[str, Any]], trade_id: str
) -> list[dict[str, Any]]:
    tid = str(trade_id)
    out = [r for r in rows if str(r.get("trade_id") or "") == tid]
    out.sort(key=lambda r: int(r.get("signal_ts_ms") or 0))
    return out


def render_trade_png(
    *,
    trade_rows: Sequence[dict[str, Any]],
    theta_rows: Sequence[dict[str, Any]],
    out_path: Path,
    window_ms: int = 600_000,
    inset_ms: int = 200,
) -> Path:
    """Render spread + θ_1m panel. Requires matplotlib."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "matplotlib required for PNG output; pip install matplotlib"
        ) from exc

    if not trade_rows:
        raise ValueError("no trade rows")
    open_row = next((r for r in trade_rows if r.get("event") == "open"), trade_rows[0])
    coin = str(open_row.get("base_coin") or "").upper()
    side = str(open_row.get("side") or "long")
    opp = "short" if side == "long" else "long"
    center = int(open_row.get("signal_ts_ms") or 0)
    lo = center - int(window_ms) // 2
    hi = center + int(window_ms) // 2

    own = [
        r
        for r in theta_rows
        if str(r.get("base_coin") or "").upper() == coin
        and str(r.get("side") or "") == side
        and lo <= int(r.get("ts_ms") or 0) <= hi
    ]
    opposite = [
        r
        for r in theta_rows
        if str(r.get("base_coin") or "").upper() == coin
        and str(r.get("side") or "") == opp
        and lo <= int(r.get("ts_ms") or 0) <= hi
    ]
    own.sort(key=lambda r: int(r.get("ts_ms") or 0))
    opposite.sort(key=lambda r: int(r.get("ts_ms") or 0))

    fig, axes = plt.subplots(2, 1, figsize=(6.0, 8.0), sharex=False)
    fig.suptitle(f"{coin} {side} trade {open_row.get('trade_id')}", fontsize=11)

    ax0 = axes[0]
    # Spread markers from trade journal (signal/fill).
    for r in trade_rows:
        sig = r.get("spread_signal")
        fill = r.get("spread_fill")
        ts_s = int(r.get("signal_ts_ms") or 0)
        ts_f = int(r.get("fill_ts_ms") or 0)
        if sig is not None:
            ax0.axvline(ts_s, color="C0", alpha=0.5, linewidth=1)
            ax0.scatter([ts_s], [float(sig)], color="C0", s=28, zorder=3, label="signal")
        if fill is not None and ts_f:
            ax0.axvline(ts_f, color="C3", alpha=0.5, linewidth=1, linestyle="--")
            ax0.scatter([ts_f], [float(fill)], color="C3", s=28, zorder=3, label="fill")
    ax0.set_ylabel("spread %")
    ax0.set_title("spread @ signal/fill")
    handles, labels = ax0.get_legend_handles_labels()
    by_label = dict(zip(labels, handles))
    if by_label:
        ax0.legend(by_label.values(), by_label.keys(), loc="best", fontsize=8)

    # Optional 0–200ms inset relative to signal.
    if inset_ms > 0 and open_row.get("spread_signal") is not None:
        ax_in = ax0.inset_axes([0.55, 0.55, 0.4, 0.4])
        t0 = int(open_row["signal_ts_ms"])
        xs = [0, int(open_row.get("latency_ms") or 0)]
        ys = [float(open_row["spread_signal"]), float(open_row.get("spread_fill") or open_row["spread_signal"])]
        ax_in.plot(xs, ys, marker="o")
        ax_in.set_xlim(0, max(inset_ms, xs[-1] + 1))
        ax_in.set_title(f"0–{inset_ms}ms", fontsize=7)
        ax_in.tick_params(labelsize=6)

    ax1 = axes[1]
    if own:
        ax1.plot(
            [int(r["ts_ms"]) for r in own],
            [r.get("theta_1m") for r in own],
            color="C0",
            label=f"θ_1m {side}",
        )
    if opposite:
        ax1.plot(
            [int(r["ts_ms"]) for r in opposite],
            [r.get("theta_1m") for r in opposite],
            color="C1",
            label=f"θ_1m {opp}",
        )
    thr = open_row.get("theta_thr")
    if thr is not None:
        ax1.axhline(float(thr), color="k", linestyle=":", linewidth=1, label="θ_thr")
    for r in trade_rows:
        ax1.axvline(int(r.get("signal_ts_ms") or 0), color="C0", alpha=0.3)
        if r.get("fill_ts_ms"):
            ax1.axvline(int(r["fill_ts_ms"]), color="C3", alpha=0.3, linestyle="--")
    ax1.set_ylabel("θ_1m")
    ax1.set_xlabel("ts_ms")
    ax1.legend(loc="best", fontsize=8)
    ax1.set_title("θ_1m own + opposite")

    fig.tight_layout()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PNG plot for a θ K=1 would_send trade")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--trade-id", type=str, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--window-ms", type=int, default=600_000)
    parser.add_argument("--inset-ms", type=int, default=200)
    args = parser.parse_args(list(argv) if argv is not None else None)

    trade_rows = find_trade_events(
        load_jsonl(discover_trade_files(args.data_root)), args.trade_id
    )
    if not trade_rows:
        raise SystemExit(f"trade_id not found: {args.trade_id}")
    theta_rows = load_jsonl(discover_theta_files(args.data_root))
    path = render_trade_png(
        trade_rows=trade_rows,
        theta_rows=theta_rows,
        out_path=args.out,
        window_ms=args.window_ms,
        inset_ms=args.inset_ms,
    )
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

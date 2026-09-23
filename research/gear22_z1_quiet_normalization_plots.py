"""Gear 2.2 experiment Z1 - stage 3: figures and the per-window atlas.

Consumes the frozen manifest, the tick cache and the stage-2 tables. Writes PNG
into ``<out>/plots`` and a lazily-loading HTML atlas. Read-only with respect to
``output/lean_ticks``, the canonical simulator and every strategy parameter.
No PnL anywhere.
"""

from __future__ import annotations

import html
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import TwoSlopeNorm

from research.gear22_z1_quiet_normalization_lib import CACHE, HALF_MS, OUT_V2
from research.gear22_z1_quiet_normalization_stats import (
    DIRECTIONS,
    PRIMARY_P,
    Window,
    accept_band,
    build_window,
    pooled_table,
    wq,
    wtable,
)

DPI = 110
SERIES_BUCKETS = 1200  # min/max envelope buckets per series in the atlas
ECDF_POINTS = 1500


def _plots_dir(out_dir: Path) -> Path:
    d = out_dir / "plots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _coin_colors(coins: list[str]) -> dict[str, tuple]:
    cmap = plt.get_cmap("tab10")
    return {c: cmap(i % 10) for i, c in enumerate(sorted(coins))}


def _envelope(ts: np.ndarray, y: np.ndarray, buckets: int = SERIES_BUCKETS):
    """Min/max decimation: keeps the visible range of a long quiet series."""
    n = ts.size
    if n <= 2 * buckets:
        return ts, y
    idx = np.linspace(0, n, buckets + 1).astype(int)
    xs, ys = [], []
    for a, b in zip(idx[:-1], idx[1:]):
        if b <= a:
            continue
        seg = y[a:b]
        lo, hi = int(np.argmin(seg)), int(np.argmax(seg))
        for j in sorted((lo, hi)):
            xs.append(ts[a + j])
            ys.append(seg[j])
    return np.asarray(xs), np.asarray(ys)


def _ecdf_xy(v: np.ndarray, w: np.ndarray, points: int = ECDF_POINTS):
    uv, uw = wtable(v, w)
    if uv.size == 0:
        return np.empty(0), np.empty(0)
    c = np.cumsum(uw) / uw.sum()
    if uv.size > points:
        k = np.unique(np.linspace(0, uv.size - 1, points).astype(int))
        uv, c = uv[k], c[k]
    return uv, c


def _wquantiles(v: np.ndarray, w: np.ndarray, ps: np.ndarray) -> np.ndarray:
    uv, uw = wtable(v, w)
    return np.array([wq(uv, uw, float(p)) for p in ps])


def _rep_arrays(win: Window, rep: str):
    return {
        "rc": (win.rc_v, win.rc_w),
        "rl": (win.rl_v, win.rl_w),
        "zp": (win.zp_v, win.zp_w),
    }[rep]


REP_LABEL = {"rc": "r_causal", "rl": "r_local", "zp": "z_plus_causal"}


# --- figures ----------------------------------------------------------------

def plot_ecdfs(wins: list[Window], pdir: Path) -> list[Path]:
    coins = sorted({w.coin for w in wins})
    colors = _coin_colors(coins)
    out = []
    for rep in ("rc", "rl", "zp"):
        fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True)
        for ax, direction in zip(axes, DIRECTIONS):
            for w in wins:
                if w.direction != direction:
                    continue
                x, y = _ecdf_xy(*_rep_arrays(w, rep))
                if x.size == 0:
                    continue
                ax.step(x, y, where="post", lw=0.9, alpha=0.85, color=colors[w.coin])
            ax.set_title(f"{REP_LABEL[rep]} - {direction}")
            ax.set_xlabel(REP_LABEL[rep])
            ax.grid(alpha=0.25)
            lo = -4 if rep != "zp" else -0.2
            ax.set_xlim(lo, 12 if rep != "zp" else 8)
        axes[0].set_ylabel("time-weighted ECDF")
        handles = [
            plt.Line2D([], [], color=colors[c], lw=2, label=c) for c in coins
        ]
        fig.legend(handles=handles, loc="lower center", ncol=10, frameon=False)
        fig.suptitle(
            f"ECDF of {REP_LABEL[rep]} on the evaluation half, one colour per coin"
        )
        fig.tight_layout(rect=(0, 0.06, 1, 1))
        p = pdir / f"ecdf_{REP_LABEL[rep]}.png"
        fig.savefig(p, dpi=DPI)
        plt.close(fig)
        out.append(p)
    return out


def plot_qq_within_coin(wins: list[Window], pdir: Path, rep: str = "rc") -> Path:
    ps = np.linspace(0.01, 0.995, 220)
    coins = sorted({w.coin for w in wins})
    fig, axes = plt.subplots(2, 5, figsize=(19, 8))
    for ax, coin in zip(axes.ravel(), coins):
        for direction, style in zip(DIRECTIONS, ("-", "--")):
            group = [w for w in wins if w.coin == coin and w.direction == direction]
            group.sort(key=lambda w: w.quiet_id)
            if len(group) < 2:
                continue
            base = group[0]
            qb = _wquantiles(*_rep_arrays(base, rep), ps)
            for other in group[1:]:
                qo = _wquantiles(*_rep_arrays(other, rep), ps)
                ax.plot(
                    qb,
                    qo,
                    style,
                    lw=1.1,
                    label=f"{other.quiet_id[-9:]} ({direction[0]})",
                )
        lim = ax.get_xlim() + ax.get_ylim()
        if lim:
            m = [min(lim), max(lim)]
            ax.plot(m, m, color="k", lw=0.8, alpha=0.6)
        ax.set_title(coin, fontsize=10)
        ax.grid(alpha=0.25)
        ax.legend(fontsize=6, loc="upper left")
    fig.suptitle(
        f"QQ between windows of the same coin, {REP_LABEL[rep]} "
        "(x = first window of the coin, solid long / dashed short)"
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    p = pdir / f"qq_within_coin_{REP_LABEL[rep]}.png"
    fig.savefig(p, dpi=DPI)
    plt.close(fig)
    return p


def plot_qq_between_coins(wins: list[Window], pdir: Path, rep: str = "rc") -> Path:
    ps = np.linspace(0.01, 0.995, 220)
    coins = sorted({w.coin for w in wins})
    fig, axes = plt.subplots(2, 5, figsize=(19, 8))
    for ax, coin in zip(axes.ravel(), coins):
        for direction, style in zip(DIRECTIONS, ("-", "--")):
            pool = [w for w in wins if w.direction == direction]
            mine = [w for w in pool if w.coin == coin]
            others = [w for w in pool if w.coin != coin]
            if not mine or not others:
                continue
            ov, ow = pooled_table(others, rep)
            mv, mw = pooled_table(mine, rep)
            qo = np.array([wq(ov, ow, float(p)) for p in ps])
            qm = np.array([wq(mv, mw, float(p)) for p in ps])
            ax.plot(qo, qm, style, lw=1.2, label=direction)
        lim = ax.get_xlim() + ax.get_ylim()
        m = [min(lim), max(lim)]
        ax.plot(m, m, color="k", lw=0.8, alpha=0.6)
        ax.set_title(coin, fontsize=10)
        ax.grid(alpha=0.25)
        ax.legend(fontsize=7, loc="upper left")
    fig.suptitle(
        f"QQ coin vs leave-one-coin-out pool, {REP_LABEL[rep]} "
        "(x = pool of the other coins, equal weight per coin)"
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    p = pdir / f"qq_between_coins_{REP_LABEL[rep]}.png"
    fig.savefig(p, dpi=DPI)
    plt.close(fig)
    return p


def plot_tail_heatmap(stats: pd.DataFrame, pdir: Path) -> Path:
    order = stats.sort_values(["base_coin", "quiet_id"])["quiet_id"].unique().tolist()
    fig, axes = plt.subplots(2, 3, figsize=(17, 11))
    for i, direction in enumerate(DIRECTIONS):
        for j, rep in enumerate(("rc", "rl", "zp")):
            ax = axes[i, j]
            sub = stats[stats["direction"] == direction].set_index("quiet_id")
            cols = [f"{rep}_Q{int(p * 100)}" for p in PRIMARY_P]
            m = sub.reindex(order)[cols].to_numpy(dtype=float)
            im = ax.imshow(m, aspect="auto", cmap="viridis")
            ax.set_xticks(range(len(cols)), [f"P{int(p * 100)}" for p in PRIMARY_P])
            ax.set_yticks(range(len(order)), order, fontsize=6)
            ax.set_title(f"{REP_LABEL[rep]} - {direction}", fontsize=10)
            for a in range(m.shape[0]):
                for b in range(m.shape[1]):
                    if np.isfinite(m[a, b]):
                        ax.text(
                            b, a, f"{m[a, b]:.1f}", ha="center", va="center",
                            fontsize=5, color="w",
                        )
            fig.colorbar(im, ax=ax, fraction=0.05)
    fig.suptitle("Tail quantiles of the normalized spread, observed arm")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    p = pdir / "heatmap_tail_quantiles.png"
    fig.savefig(p, dpi=DPI)
    plt.close(fig)
    return p


def plot_loco_heatmap(loco: pd.DataFrame, pdir: Path) -> Path:
    g = loco[(loco["scope"] == "global") & (loco["representation"] == "rc")]
    fig, axes = plt.subplots(1, 2, figsize=(14, 11))
    for ax, direction in zip(axes, DIRECTIONS):
        sub = g[g["direction"] == direction]
        piv = sub.pivot_table(
            index="quiet_id", columns="p", values="exceed_observed", aggfunc="first"
        )
        piv = piv.sort_index()
        ratio = piv.to_numpy(dtype=float) / np.array([1 - p for p in piv.columns])
        ratio = np.where(ratio > 0, ratio, 1e-3)
        im = ax.imshow(
            np.log2(ratio),
            aspect="auto",
            cmap="coolwarm",
            norm=TwoSlopeNorm(vcenter=0.0, vmin=-6, vmax=6),
        )
        ax.set_xticks(range(piv.shape[1]), [f"P{int(p * 100)}" for p in piv.columns])
        ax.set_yticks(range(piv.shape[0]), piv.index.tolist(), fontsize=6)
        ax.set_title(f"log2( exceedance / (1-p) ), r_causal, {direction}", fontsize=10)
        for a in range(ratio.shape[0]):
            for b in range(ratio.shape[1]):
                lo, hi = accept_band(float(piv.columns[b]))
                v = piv.to_numpy(dtype=float)[a, b]
                ok = lo <= v <= hi
                ax.text(
                    b, a, "ok" if ok else "x", ha="center", va="center",
                    fontsize=6, color="k" if ok else "w",
                )
        fig.colorbar(im, ax=ax, fraction=0.05)
    fig.suptitle(
        "Leave-one-coin-out calibration: pooled threshold applied to the held-out coin\n"
        "0 = nominal, band = [(1-p)/2, 2(1-p)]"
    )
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    p = pdir / "heatmap_loco_calibration.png"
    fig.savefig(p, dpi=DPI)
    plt.close(fig)
    return p


def plot_distance_matrices(dist: pd.DataFrame, pdir: Path) -> Path:
    d = dist[dist["representation"] == "rc"]
    ids = sorted(set(d["a"]) | set(d["b"]))
    pos = {q: i for i, q in enumerate(ids)}
    fig, axes = plt.subplots(2, 2, figsize=(17, 16))
    for i, direction in enumerate(DIRECTIONS):
        for j, col in enumerate(("w1_raw", "w1_res_matched")):
            ax = axes[i, j]
            m = np.full((len(ids), len(ids)), np.nan)
            sub = d[d["direction"] == direction]
            for _, r in sub.iterrows():
                a, b = pos[r["a"]], pos[r["b"]]
                m[a, b] = m[b, a] = r[col]
            np.fill_diagonal(m, 0.0)
            im = ax.imshow(m, cmap="magma_r", aspect="auto")
            ax.set_xticks(range(len(ids)), ids, rotation=90, fontsize=5)
            ax.set_yticks(range(len(ids)), ids, fontsize=5)
            ax.set_title(f"{col} - r_causal - {direction}", fontsize=10)
            fig.colorbar(im, ax=ax, fraction=0.046)
    fig.suptitle("W1 distance between normalized evaluation-half distributions")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    p = pdir / "distance_matrices.png"
    fig.savefig(p, dpi=DPI)
    plt.close(fig)
    return p


def plot_kappa_vs_error(loco: pd.DataFrame, pdir: Path) -> Path:
    g = loco[(loco["scope"] == "global") & (loco["representation"] == "rc")].copy()
    g["ratio"] = g["exceed_observed"] / (1.0 - g["p"])
    g["ratio"] = g["ratio"].replace(0, 1e-3)
    coins = sorted(g["base_coin"].unique())
    colors = _coin_colors(coins)
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), sharey=True)
    for ax, direction in zip(axes, DIRECTIONS):
        sub = g[g["direction"] == direction]
        for coin, cg in sub.groupby("base_coin"):
            ax.scatter(
                cg["kappa"], np.log2(cg["ratio"]), s=26, alpha=0.8,
                color=colors[coin], label=coin,
            )
        ax.axhline(0, color="k", lw=0.8)
        ax.axhline(-1, color="grey", lw=0.7, ls=":")
        ax.axhline(1, color="grey", lw=0.7, ls=":")
        ax.set_xlabel(r"$\kappa = \delta_s/\sigma_0$")
        ax.set_title(direction)
        ax.grid(alpha=0.25)
    axes[0].set_ylabel(r"$\log_2$( exceedance / (1-p) )")
    axes[1].legend(fontsize=7, ncol=2)
    fig.suptitle(
        "Tail-calibration error against quote-lattice coarseness "
        "(dotted lines = practical-equivalence band)"
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    p = pdir / "kappa_vs_tail_calibration_error.png"
    fig.savefig(p, dpi=DPI)
    plt.close(fig)
    return p


def plot_liquidity_vs_tails(stats: pd.DataFrame, pdir: Path) -> Path:
    fig, axes = plt.subplots(2, 3, figsize=(15, 8), sharex=True)
    for i, direction in enumerate(DIRECTIONS):
        for j, p_ in enumerate(PRIMARY_P):
            ax = axes[i, j]
            col = f"rc_Q{int(p_ * 100)}"
            sub = stats[stats["direction"] == direction]
            data, labels = [], []
            for liq in ("major", "alt"):
                v = sub.loc[sub["liquidity_class_pre"] == liq, col].dropna()
                data.append(v.to_numpy())
                labels.append(f"{liq} (n={len(v)})")
            ax.boxplot(data, tick_labels=labels, widths=0.5)
            for k, v in enumerate(data):
                ax.scatter(
                    np.full(len(v), k + 1) + np.random.uniform(-0.09, 0.09, len(v)),
                    v, s=16, alpha=0.7, color="tab:blue",
                )
            ax.set_title(f"r_causal P{int(p_ * 100)} - {direction}", fontsize=10)
            ax.grid(alpha=0.25)
    fig.suptitle("Tail quantiles of r_causal by pre-declared liquidity class")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    p = pdir / "liquidity_vs_tail_quantiles.png"
    fig.savefig(p, dpi=DPI)
    plt.close(fig)
    return p


def plot_delta_f_lsigma(stats: pd.DataFrame, pdir: Path) -> Path:
    coins = sorted(stats["base_coin"].unique())
    colors = _coin_colors(coins)
    fig, axes = plt.subplots(2, 2, figsize=(17, 9))
    for i, direction in enumerate(DIRECTIONS):
        sub = stats[stats["direction"] == direction].sort_values(
            ["base_coin", "quiet_id"]
        )
        for j, (col, ref, name) in enumerate(
            [
                ("delta_F", 0.5, r"$\Delta F=(F_1-F_0)/\sigma_0$"),
                ("L_sigma", np.log(1.25), r"$L_\sigma=\log(\sigma_1/\sigma_0)$"),
            ]
        ):
            ax = axes[i, j]
            x = np.arange(len(sub))
            ax.bar(
                x, sub[col].to_numpy(dtype=float),
                color=[colors[c] for c in sub["base_coin"]],
            )
            ax.axhline(ref, color="crimson", lw=0.9, ls="--")
            ax.axhline(-ref, color="crimson", lw=0.9, ls="--")
            ax.axhline(0, color="k", lw=0.8)
            ax.set_xticks(x, sub["quiet_id"], rotation=90, fontsize=5)
            ax.set_title(f"{name} - {direction}", fontsize=10)
            ax.grid(alpha=0.25, axis="y")
    fig.suptitle(
        "Floor shift and scale change between the calibration and evaluation half"
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    p = pdir / "delta_F_and_L_sigma.png"
    fig.savefig(p, dpi=DPI)
    plt.close(fig)
    return p


def plot_variance_decomposition(vdec: pd.DataFrame, pdir: Path) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5), sharey=True)
    for ax, direction in zip(axes, DIRECTIONS):
        sub = vdec[vdec["direction"] == direction]
        x = np.arange(len(sub))
        bottom = np.zeros(len(sub))
        for col, label, color in (
            ("frac_class", "liquidity class", "tab:blue"),
            ("frac_coin", "coin within class", "tab:orange"),
            ("frac_window", "window within coin", "tab:green"),
        ):
            v = sub[col].to_numpy(dtype=float)
            ax.bar(x, v, bottom=bottom, label=label, color=color)
            bottom += v
        ax.set_xticks(x, sub["metric"], rotation=90, fontsize=7)
        ax.set_title(direction)
        ax.grid(alpha=0.25, axis="y")
    axes[0].set_ylabel("share of the sum of squares")
    axes[1].legend(fontsize=8)
    fig.suptitle("Variance decomposition of the tail quantiles")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    p = pdir / "variance_decomposition.png"
    fig.savefig(p, dpi=DPI)
    plt.close(fig)
    return p


# --- atlas ------------------------------------------------------------------

def atlas_panel(
    man_row: pd.Series, wl: Window, ws: Window, panel_dir: Path
) -> Path:
    df = pd.read_parquet(CACHE / f"{man_row['quiet_id']}.parquet")
    ts = df["ts"].to_numpy(dtype=np.int64)
    start, end = int(man_row["start_ms"]), int(man_row["end_ms"])
    mid = start + HALF_MS
    hours = (ts - start) / 3_600_000.0

    fig, axes = plt.subplots(1, 3, figsize=(15, 3.4))
    for ax, win, col in zip(axes[:2], (wl, ws), ("spread_long", "spread_short")):
        x, y = _envelope(hours, df[col].to_numpy(dtype=float))
        ax.plot(x, y, lw=0.5, color="tab:blue")
        ax.axvline(6.0, color="k", lw=1.0, ls="--")
        ax.axhline(win.F0, color="tab:green", lw=1.0, label="F0 (cal median)")
        ax.axhline(win.F0_75, color="tab:orange", lw=1.0, ls=":", label="F0_75")
        ax.axhline(win.F1, color="crimson", lw=1.0, ls="--", label="F1 (eval median)")
        lo = np.nanpercentile(y, 0.2)
        hi = np.nanpercentile(y, 99.8)
        pad = 0.15 * (hi - lo) if hi > lo else 1e-6
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_xlabel("hours from window start")
        ax.set_title(f"{win.direction}  sigma0={win.sigma0:.2e}  kappa={win.kappa:.3f}",
                     fontsize=8)
        ax.grid(alpha=0.25)
        ax.legend(fontsize=6, loc="upper right")
    ax = axes[2]
    for win, color in zip((wl, ws), ("tab:blue", "tab:red")):
        x, y = _ecdf_xy(win.rc_v, win.rc_w)
        if x.size:
            ax.step(x, y, where="post", lw=1.0, color=color, label=win.direction)
    ax.set_xlim(-4, 12)
    ax.set_xlabel("r_causal")
    ax.set_title("ECDF of r_causal (eval half)", fontsize=8)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=7)
    fig.suptitle(
        f"{man_row['quiet_id']}  {man_row['window_start_ts_utc']} -> "
        f"{man_row['window_end_ts_utc']}  [{man_row['liquidity_class_pre']}]",
        fontsize=10,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    p = panel_dir / f"{man_row['quiet_id']}.png"
    fig.savefig(p, dpi=95)
    plt.close(fig)
    return p


def build_atlas(
    man: pd.DataFrame, wins: dict[tuple[str, str], Window], out_dir: Path
) -> Path:
    panel_dir = out_dir / "atlas_panels"
    panel_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for _, r in man[man["primary"]].sort_values(["base_coin", "quiet_id"]).iterrows():
        wl = wins[(r["quiet_id"], "long")]
        ws = wins[(r["quiet_id"], "short")]
        p = atlas_panel(r, wl, ws, panel_dir)
        status = {wl.status, ws.status}
        badge = (
            "tick_resolution_limited"
            if "tick_resolution_limited" in status
            else "ok"
        )
        rows.append(
            "<figure class='panel'>"
            f"<figcaption><b>{html.escape(r['quiet_id'])}</b> "
            f"&middot; {html.escape(r['liquidity_class_pre'])} "
            f"&middot; regime {html.escape(r['quiet_regime_id'])} "
            f"&middot; cluster {html.escape(r['calendar_cluster_id'])} "
            f"&middot; n_ticks {int(r['n_ticks']):,} "
            f"&middot; unknown time {r['unk_frac']:.3%} "
            f"&middot; <span class='badge {badge}'>{badge}</span>"
            "</figcaption>"
            f"<img loading='lazy' src='atlas_panels/{html.escape(p.name)}' "
            f"alt='{html.escape(r['quiet_id'])}'>"
            "</figure>"
        )
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Gear 2.2 Z1 - quiet-window atlas (manifest v2)</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px;
        background: #fafafa; color: #222; }}
 h1 {{ font-size: 20px; }}
 p.lead {{ max-width: 70em; color: #444; font-size: 14px; }}
 figure.panel {{ margin: 0 0 22px 0; background: #fff; border: 1px solid #e2e2e2;
                border-radius: 6px; padding: 8px; }}
 figcaption {{ font-size: 12px; margin-bottom: 6px; color: #333; }}
 img {{ width: 100%; height: auto; display: block; }}
 .badge {{ padding: 1px 6px; border-radius: 3px; font-size: 11px; }}
 .badge.ok {{ background: #e6f4ea; color: #137333; }}
 .badge.tick_resolution_limited {{ background: #fce8e6; color: #a50e0e; }}
</style></head><body>
<h1>Gear 2.2 &middot; Z1 &middot; quiet-window atlas (manifest v2, {len(rows)} windows)</h1>
<p class="lead">One panel per declared quiet window. Left and centre: the executable
spread for the long and the short direction, min/max decimated for file size, with the
calibration median <code>F0</code>, the calibration upper quartile <code>F0_75</code>,
the evaluation median <code>F1</code> and the dashed calibration/evaluation boundary at
+6 h. Right: the time-weighted ECDF of <code>r_causal=(s-F0)/sigma0</code> on the
evaluation half. Research artifact, Track M: no PnL, no strategy parameter, read-only
with respect to the collector.</p>
{"".join(rows)}
</body></html>
"""
    p = out_dir / "atlas.html"
    p.write_text(doc)
    return p


def run_stage3(out_dir: Path = OUT_V2) -> dict:
    man = pd.read_csv(out_dir / "manifest_frozen.csv")
    stats = pd.read_csv(out_dir / "window_stats.csv")
    loco = pd.read_csv(out_dir / "loco_calibration.csv")
    dist = pd.read_csv(out_dir / "distances.csv")
    vdec = pd.read_csv(out_dir / "variance_decomposition.csv")
    pdir = _plots_dir(out_dir)

    primary = man[man["primary"]].reset_index(drop=True)
    built: dict[tuple[str, str], Window] = {}
    for _, r in primary.iterrows():
        for d in DIRECTIONS:
            built[(r["quiet_id"], d)] = build_window(r, d)
    usable = [w for w in built.values() if w.status == "ok"]

    made = []
    made += plot_ecdfs(usable, pdir)
    made.append(plot_qq_within_coin(usable, pdir, "rc"))
    made.append(plot_qq_within_coin(usable, pdir, "rl"))
    made.append(plot_qq_between_coins(usable, pdir, "rc"))
    made.append(plot_tail_heatmap(stats, pdir))
    made.append(plot_loco_heatmap(loco, pdir))
    made.append(plot_distance_matrices(dist, pdir))
    made.append(plot_kappa_vs_error(loco, pdir))
    made.append(plot_liquidity_vs_tails(stats, pdir))
    made.append(plot_delta_f_lsigma(stats, pdir))
    made.append(plot_variance_decomposition(vdec, pdir))
    atlas = build_atlas(man, built, out_dir)
    return {"plots": [str(p) for p in made], "atlas": str(atlas)}


if __name__ == "__main__":
    import json

    print(json.dumps(run_stage3(), indent=2))

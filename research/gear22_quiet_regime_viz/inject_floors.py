"""Inject one corridor + SMA-12-floor figure into already-built 6-row pages.

VPS dashboards (e.g. Desktop/gear22_viz_sept) were generated before this
panel existed. September ticks live on the VPS and are not required here.

This module:

- extracts SMA-3 (display), SMA-12 (floor metric), ``tw_p95``, ``tw_p99``
  (and ``tw_p01`` / ``tw_p05`` if present)
- reconstructs missing p5 / p1 from candle inspect hist ``customdata``
- computes tf-select α25 = min(trim_3h_α25, trim_12h_α25) of SMA-12
- appends one single-row corridor figure after each existing 6-row stack
- does not rewrite candles, quantiles, histograms, ``index.html``, or ``coins.json``
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Callable

import numpy as np

from research.gear22_quiet_regime_viz.metrics_ext import extension_help_html
from research.gear22_quiet_regime_viz.plot import (
    _fig_to_div,
    build_floor_only_figure,
)
from research.gear22_quiet_regime_viz.quantiles import hist_cdf_quantile

INJECT_START = "<!-- gear22-floors-injected-start -->"
INJECT_END = "<!-- gear22-floors-injected-end -->"
FLOORS_META = (
    "injected: one corridor graph per side — p1–p99 dashed, p5–p95 fill, "
    "SMA-3, tf-select α25 = min(trim 3h/12h at α=25%) of SMA-12"
)
FLOORS_SUB = (
    " After each 6-row stack: one graph — TW p1–p99 (dashed) and "
    "p5–p95 corridor, SMA-3 inside the inner band, tf-select α25 "
    "floor (from SMA-12). Observation only — not a live threshold."
)
_OLD_FLOORS_SUBS: tuple[str, ...] = (
    " After each 6-row stack: three SMA-12 floor graphs (median / mean / trim10), "
    "each with 3h / 6h / 12h memory plus a faint SMA-12 reference. Observation "
    "only — not a live threshold.",
    " After each 6-row stack: one graph with SMA-12, trim 3h, trim 12h, and "
    "tf-select = min(trim 3h, trim 12h). Observation only — not a live threshold.",
    " After each 6-row stack: one graph with SMA-12, trim 3h/12h and "
    "tf-select at α=10% (solid) and α=25% (dashed). Observation only — "
    "not a live threshold.",
    " After each 6-row stack: two graphs — (1) SMA-12 + trim 3h/12h + "
    "tf-select at α=10% (solid) and α=25% (dashed); (2) SMA-12 + "
    "median 3h/12h + tf-select med. Observation only — not a live "
    "threshold.",
    " After each 6-row stack: one graph — TW p1–p99 (dashed) and "
    "p5–p95 corridor, SMA-12 inside the inner band, tf-select α25 "
    "floor. Observation only — not a live threshold.",
)
_SMA_NAME_RE = {
    "long": re.compile(r'"name"\s*:\s*"long SMA-12'),
    "short": re.compile(r'"name"\s*:\s*"short SMA-12'),
}
_SMA3_NAME_RE = {
    "long": re.compile(r'"name"\s*:\s*"long SMA-3'),
    "short": re.compile(r'"name"\s*:\s*"short SMA-3'),
}
_OHLC_NAME_RE = {
    "long": re.compile(r'"name"\s*:\s*"long 5m OHLC"'),
    "short": re.compile(r'"name"\s*:\s*"short 5m OHLC"'),
}
_COIN_TITLE_RE = re.compile(
    r"<title>\s*Gear 2\.2 quiet-regime — ([^<]+)</title>",
    re.IGNORECASE,
)


def _decode_plotly_numeric(obj: Any) -> np.ndarray:
    if obj is None:
        return np.asarray([], dtype="float64")
    if isinstance(obj, list):
        out = []
        for v in obj:
            if v is None:
                out.append(float("nan"))
            else:
                out.append(float(v))
        return np.asarray(out, dtype="float64")
    if isinstance(obj, dict) and "bdata" in obj:
        raw = base64.b64decode(obj["bdata"])
        dtype = str(obj.get("dtype") or "f8")
        np_dtype = {
            "f8": "<f8",
            "f4": "<f4",
            "i4": "<i4",
            "i8": "<i8",
        }.get(dtype, "<f8")
        return np.frombuffer(raw, dtype=np_dtype).astype("float64", copy=True)
    raise TypeError(f"unsupported plotly array payload: {type(obj)!r}")


def extract_trace_xy(html: str, name_regex: re.Pattern[str], label: str) -> tuple[list[str], np.ndarray]:
    """Return ``(x_iso_strings, y_float64)`` for the first matching trace."""
    m = name_regex.search(html)
    if m is None:
        raise KeyError(f"{label} trace not found")
    decoder = json.JSONDecoder()
    x_key = html.find(',"x":', m.start())
    if x_key < 0:
        x_key = html.find(', "x":', m.start())
    if x_key < 0:
        raise KeyError(f"{label} x array not found")
    colon = html.find(":", x_key)
    x_obj, x_end = decoder.raw_decode(html, colon + 1)
    y_key = html.find(',"y":', x_end)
    if y_key < 0:
        y_key = html.find(', "y":', x_end)
    if y_key < 0:
        raise KeyError(f"{label} y array not found")
    y_colon = html.find(":", y_key)
    y_obj, _ = decoder.raw_decode(html, y_colon + 1)
    if not isinstance(x_obj, list):
        raise TypeError(f"{label} x is not a list")
    y = _decode_plotly_numeric(y_obj)
    if len(x_obj) != int(y.size):
        raise ValueError(f"{label} length mismatch x={len(x_obj)} y={y.size}")
    return [str(v) for v in x_obj], y


def extract_sma12_xy(html: str, side: str) -> tuple[list[str], np.ndarray]:
    """Return ``(x_iso_strings, y_float64)`` for ``{side} SMA-12×5m``."""
    side_u = str(side).lower()
    pat = _SMA_NAME_RE.get(side_u)
    if pat is None:
        raise KeyError(f"unknown side {side!r}")
    return extract_trace_xy(html, pat, f"{side_u} SMA-12")


def extract_sma3_xy(html: str, side: str) -> tuple[list[str], np.ndarray]:
    """Return ``(x_iso_strings, y_float64)`` for ``{side} SMA-3×5m``."""
    side_u = str(side).lower()
    pat = _SMA3_NAME_RE.get(side_u)
    if pat is None:
        raise KeyError(f"unknown side {side!r}")
    return extract_trace_xy(html, pat, f"{side_u} SMA-3")


def extract_named_side_xy(
    html: str, side: str, suffix: str
) -> tuple[list[str], np.ndarray]:
    """Extract ``"{side} {suffix}"`` (e.g. ``long tw_p95``)."""
    side_u = str(side).lower()
    pat = re.compile(rf'"name"\s*:\s*"{re.escape(side_u)} {re.escape(suffix)}"')
    return extract_trace_xy(html, pat, f"{side_u} {suffix}")


def extract_ohlc_customdata(html: str, side: str) -> list[Any]:
    """Candlestick inspect payloads for ``{side} 5m OHLC`` (may be empty)."""
    side_u = str(side).lower()
    pat = _OHLC_NAME_RE.get(side_u)
    if pat is None:
        raise KeyError(f"unknown side {side!r}")
    m = pat.search(html)
    if m is None:
        return []
    idx = html.rfind('"customdata"', 0, m.start())
    if idx < 0:
        return []
    decoder = json.JSONDecoder()
    colon = html.find(":", idx)
    obj, _ = decoder.raw_decode(html, colon + 1)
    if not isinstance(obj, list):
        return []
    return obj


def _x_to_ms(x: Any) -> int | None:
    s = str(x).replace("Z", "")
    if "+" in s:
        s = s.split("+", 1)[0]
    try:
        ts = np.datetime64(s)
        return int(ts.astype("datetime64[ms]").astype("int64"))
    except Exception:
        return None


def _align_to_master(
    master_x: list[str], other_x: list[str], other_y: np.ndarray
) -> np.ndarray:
    out = np.full(len(master_x), np.nan, dtype="float64")
    by_s = {str(a): float(b) for a, b in zip(other_x, other_y)}
    by_ms: dict[int, float] = {}
    for a, b in zip(other_x, other_y):
        ms = _x_to_ms(a)
        if ms is not None:
            by_ms[int(ms)] = float(b)
    for i, x in enumerate(master_x):
        if str(x) in by_s:
            out[i] = by_s[str(x)]
            continue
        ms = _x_to_ms(x)
        if ms is not None and int(ms) in by_ms:
            out[i] = by_ms[int(ms)]
    return out


def _customdata_by_bs(payloads: list[Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for cd in payloads:
        if not isinstance(cd, dict):
            continue
        bs = cd.get("bs")
        if bs is None:
            continue
        try:
            out[int(bs)] = cd
        except (TypeError, ValueError):
            continue
    return out


def _hist_is_time_weighted(cd: dict[str, Any]) -> bool:
    flag = cd.get("c_w")
    if flag in ("tw_ms", "tw", "time", "time_weighted"):
        return True
    return False


def _series_from_customdata(
    master_x: list[str],
    by_bs: dict[int, dict[str, Any]],
    extractor: Callable[[dict[str, Any]], float],
) -> np.ndarray:
    out = np.full(len(master_x), np.nan, dtype="float64")
    for i, x in enumerate(master_x):
        ms = _x_to_ms(x)
        if ms is None:
            continue
        cd = by_bs.get(int(ms))
        if cd is None:
            continue
        try:
            val = float(extractor(cd))
        except (TypeError, ValueError):
            continue
        if np.isfinite(val):
            out[i] = val
    return out


def _inspect_tw(cd: dict[str, Any], key: str) -> float:
    tw = cd.get("tw")
    if not isinstance(tw, dict) or key not in tw:
        return float("nan")
    try:
        return float(tw[key])
    except (TypeError, ValueError):
        return float("nan")


def _hist_q(cd: dict[str, Any], q: float) -> float:
    return hist_cdf_quantile(cd.get("lo"), cd.get("hi"), cd.get("c") or [], q)


def _try_named(html: str, side: str, suffix: str) -> tuple[list[str], np.ndarray] | None:
    try:
        return extract_named_side_xy(html, side, suffix)
    except (KeyError, ValueError, TypeError):
        return None


def resolve_corridor_series(
    html: str,
    side: str,
    master_x: list[str],
) -> dict[str, Any]:
    """Resolve p01/p05/p95/p99 for inject, with an explicit source per edge.

    Priority
    --------
    p05: ``tw_p05`` → hist CDF → ``tw_p25`` fallback (named honestly)
    p01: ``tw_p01`` → inspect ``tw.p01`` → hist CDF → missing (never min)
    p95: ``tw_p95``
    p99: ``tw_p99`` → inspect ``tw.p99`` → hist CDF
    """
    payloads = extract_ohlc_customdata(html, side)
    by_bs = _customdata_by_bs(payloads)
    sample = next(iter(by_bs.values()), None)
    hist_src = "hist_tw" if sample is not None and _hist_is_time_weighted(sample) else "hist"

    def from_named(suffix: str) -> np.ndarray | None:
        got = _try_named(html, side, suffix)
        if got is None:
            return None
        _, y = got
        if y.size == len(master_x):
            return np.asarray(y, dtype="float64")
        return _align_to_master(master_x, got[0], y)

    p95 = from_named("tw_p95")
    if p95 is None:
        p95 = np.full(len(master_x), np.nan, dtype="float64")
        p95_source = "missing"
    else:
        p95_source = "tw_p95"

    p99 = from_named("tw_p99")
    p99_source = "tw_p99" if p99 is not None else "missing"
    if p99 is None and by_bs:
        p99 = _series_from_customdata(master_x, by_bs, lambda cd: _inspect_tw(cd, "p99"))
        if np.isfinite(p99).any():
            p99_source = "inspect_tw"
        else:
            p99 = _series_from_customdata(master_x, by_bs, lambda cd: _hist_q(cd, 0.99))
            p99_source = hist_src if np.isfinite(p99).any() else "missing"
    if p99 is None:
        p99 = np.full(len(master_x), np.nan, dtype="float64")

    p05 = from_named("tw_p05")
    p05_source = "tw_p05" if p05 is not None else "missing"
    if p05 is None and by_bs:
        p05 = _series_from_customdata(master_x, by_bs, lambda cd: _hist_q(cd, 0.05))
        p05_source = hist_src if np.isfinite(p05).any() else "missing"
    if p05 is None or p05_source == "missing":
        p25 = from_named("tw_p25")
        if p25 is not None and np.isfinite(p25).any():
            p05 = p25
            p05_source = "tw_p25"
        elif p05 is None:
            p05 = np.full(len(master_x), np.nan, dtype="float64")

    p01 = from_named("tw_p01")
    p01_source = "tw_p01" if p01 is not None else "missing"
    if p01 is None and by_bs:
        p01 = _series_from_customdata(master_x, by_bs, lambda cd: _inspect_tw(cd, "p01"))
        if np.isfinite(p01).any():
            p01_source = "inspect_tw"
        else:
            p01 = _series_from_customdata(master_x, by_bs, lambda cd: _hist_q(cd, 0.01))
            p01_source = hist_src if np.isfinite(p01).any() else "missing"
    if p01 is None:
        p01 = np.full(len(master_x), np.nan, dtype="float64")

    return {
        "p01": p01,
        "p05": p05,
        "p95": p95,
        "p99": p99,
        "p01_source": p01_source,
        "p05_source": p05_source,
        "p95_source": p95_source,
        "p99_source": p99_source,
    }


def _strip_previous_injects(html: str) -> str:
    out = html
    while True:
        a = out.find(INJECT_START)
        if a < 0:
            break
        b = out.find(INJECT_END, a)
        if b < 0:
            raise ValueError("unclosed gear22-floors-injected marker")
        b += len(INJECT_END)
        if b < len(out) and out[b] == "\n":
            b += 1
        out = out[:a] + out[b:]
    return out


def _insert_after_six_row_stack(html: str, side: str, block: str) -> str:
    """Insert ``block`` after the 1180px 6-row figure that owns this side's SMA-12."""
    pat = _SMA_NAME_RE[side]
    m = pat.search(html)
    if m is None:
        raise KeyError(f"{side} SMA-12 trace not found")
    script_end = html.find("</script>", m.start())
    if script_end < 0:
        raise ValueError(f"{side} figure script end not found")
    div_end = html.find("</div>", script_end)
    if div_end < 0:
        raise ValueError(f"{side} figure wrapper end not found")
    insert_at = div_end + len("</div>")
    return html[:insert_at] + "\n" + block + html[insert_at:]


def _upsert_floors_meta(html: str) -> str:
    row = f"<tr><th>floors</th><td>{FLOORS_META}</td></tr>"
    existing = re.search(
        r"<tr><th>floors</th><td>.*?</td></tr>",
        html,
        flags=re.I | re.S,
    )
    if existing:
        return html[: existing.start()] + row + html[existing.end() :]
    close = html.find("</tbody></table>")
    if close < 0:
        return html
    return html[:close] + row + html[close:]


def _append_floors_sub(html: str) -> str:
    for old in _OLD_FLOORS_SUBS:
        if old in html:
            return html.replace(old, FLOORS_SUB)
    if FLOORS_SUB in html:
        return html
    marker = '<p class="sub">'
    start = html.find(marker)
    if start < 0:
        return html
    end = html.find("</p>", start)
    if end < 0:
        return html
    return html[:end] + FLOORS_SUB + html[end:]


def _upsert_ext_help(html: str) -> str:
    help_html = extension_help_html()
    start = html.find("<section class='ext-help'>")
    if start < 0:
        start = html.find('<section class="ext-help">')
    if start >= 0:
        end = html.find("</section>", start)
        if end >= 0:
            end += len("</section>")
            return html[:start] + help_html + html[end:]
    anchor = '<aside id="candle-inspect"'
    pos = html.find(anchor)
    if pos < 0:
        pos = html.rfind("</body>")
    if pos < 0:
        return html
    return html[:pos] + help_html + "\n" + html[pos:]


def _coin_from_html(html: str, path: Path) -> str:
    m = _COIN_TITLE_RE.search(html)
    if m:
        return m.group(1).strip().upper()
    name = path.name
    prefix = "gear22_quiet_regime_"
    if name.startswith(prefix) and name.endswith(".html"):
        return name[len(prefix) : -5].upper()
    return path.stem.upper()


def inject_coin_html(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Patch one existing 6-row coin page in place. Returns a small report."""
    path = Path(path)
    html = path.read_text(encoding="utf-8")
    html = _strip_previous_injects(html)
    coin = _coin_from_html(html, path)
    long_x, long_y = extract_sma12_xy(html, "long")
    short_x, short_y = extract_sma12_xy(html, "short")
    long_x3, long_sma3 = extract_sma3_xy(html, "long")
    short_x3, short_sma3 = extract_sma3_xy(html, "short")
    if int(long_sma3.size) != len(long_x):
        long_sma3 = _align_to_master(long_x, long_x3, long_sma3)
    if int(short_sma3.size) != len(short_x):
        short_sma3 = _align_to_master(short_x, short_x3, short_sma3)
    long_c = resolve_corridor_series(html, "long", long_x)
    short_c = resolve_corridor_series(html, "short", short_x)
    long_fig = build_floor_only_figure(
        coin=coin,
        side="long",
        x=long_x,
        sma12=long_y,
        sma3=long_sma3,
        p01=long_c["p01"],
        p05=long_c["p05"],
        p95=long_c["p95"],
        p99=long_c["p99"],
        p01_source=long_c["p01_source"],
        p05_source=long_c["p05_source"],
        p95_source=long_c["p95_source"],
        p99_source=long_c["p99_source"],
    )
    short_fig = build_floor_only_figure(
        coin=coin,
        side="short",
        x=short_x,
        sma12=short_y,
        sma3=short_sma3,
        p01=short_c["p01"],
        p05=short_c["p05"],
        p95=short_c["p95"],
        p99=short_c["p99"],
        p01_source=short_c["p01_source"],
        p05_source=short_c["p05_source"],
        p95_source=short_c["p95_source"],
        p99_source=short_c["p99_source"],
    )
    long_div = _fig_to_div(long_fig, include_plotlyjs=False)
    short_div = _fig_to_div(short_fig, include_plotlyjs=False)
    long_block = (
        f"{INJECT_START}\n"
        f'<h3 class="floor-block">LONG floor — p1–p99 / p5–p95 corridor + '
        f"SMA-3 + tf-select α25</h3>\n"
        f"{long_div}\n"
        f"{INJECT_END}"
    )
    short_block = (
        f"{INJECT_START}\n"
        f'<h3 class="floor-block">SHORT floor — p1–p99 / p5–p95 corridor + '
        f"SMA-3 + tf-select α25</h3>\n"
        f"{short_div}\n"
        f"{INJECT_END}"
    )
    html = _insert_after_six_row_stack(html, "long", long_block)
    html = _insert_after_six_row_stack(html, "short", short_block)
    html = _upsert_floors_meta(html)
    html = _append_floors_sub(html)
    html = _upsert_ext_help(html)
    report = {
        "path": str(path),
        "coin": coin,
        "n_bars": int(long_y.size),
        "long_finite": int(np.isfinite(long_y).sum()),
        "short_finite": int(np.isfinite(short_y).sum()),
        "long_p01_source": long_c["p01_source"],
        "long_p05_source": long_c["p05_source"],
        "long_p95_source": long_c["p95_source"],
        "long_p99_source": long_c["p99_source"],
    }
    if not dry_run:
        path.write_text(html, encoding="utf-8")
    return report


def inject_html_dir(out_dir: Path, *, dry_run: bool = False) -> list[dict[str, Any]]:
    """Inject floors into every ``gear22_quiet_regime_*.html`` in ``out_dir``.

    Leaves ``index.html``, ``coins.json``, and ``plotly.min.js`` untouched
    except that coin pages keep loading the sibling plotly script.
    """
    root = Path(out_dir)
    pages = sorted(root.glob("gear22_quiet_regime_*.html"))
    if not pages:
        raise SystemExit(f"no coin HTML pages in {root}")
    reports: list[dict[str, Any]] = []
    for page in pages:
        if page.name.lower() == "index.html":
            continue
        try:
            rep = inject_coin_html(page, dry_run=dry_run)
        except KeyError as exc:
            print(f"skip {page.name}: {exc}")
            continue
        print(
            f"injected {page.name} coin={rep['coin']} "
            f"bars={rep['n_bars']} finite_long={rep['long_finite']} "
            f"p01={rep['long_p01_source']} p05={rep['long_p05_source']}"
        )
        reports.append(rep)
    if not reports:
        raise SystemExit(f"no pages injected in {root}")
    return reports


def maybe_annotate_index(index_path: Path) -> None:
    """Add a one-line floors note to the VPS index without touching coin links."""
    path = Path(index_path)
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    note = (
        "<p>Per-coin pages include one floor graph after each LONG/SHORT 6-row "
        "stack: dashed p1–p99 and filled p5–p95 corridor, SMA-3, and "
        "tf-select α25 (min of 3h/12h 25%-trimmed means of SMA-12).</p>\n"
    )
    old_notes = (
        "<p>Per-coin pages include SMA-12 floor graphs "
        "(median / mean / trim10 × 3h/6h/12h) after each LONG/SHORT 6-row stack.</p>\n",
        "<p>Per-coin pages include one floor graph after each LONG/SHORT 6-row "
        "stack: SMA-12, trim 3h, trim 12h, and tf-select = min(trim 3h, trim 12h).</p>\n",
        "<p>Per-coin pages include one floor graph after each LONG/SHORT 6-row "
        "stack: SMA-12 plus trim 3h / trim 12h / tf-select at α=10% and α=25%.</p>\n",
        "<p>Per-coin pages include two floor graphs after each LONG/SHORT 6-row "
        "stack: (1) SMA-12 + trim 3h/12h + tf-select at α=10% and α=25%; "
        "(2) SMA-12 + median 3h/12h + tf-select med.</p>\n",
        "<p>Per-coin pages include one floor graph after each LONG/SHORT 6-row "
        "stack: dashed p1–p99 and filled p5–p95 corridor, SMA-12, and "
        "tf-select α25 (min of 3h/12h 25%-trimmed means of SMA-12).</p>\n",
    )
    for old in old_notes:
        if old in text:
            path.write_text(text.replace(old, note), encoding="utf-8")
            return
    if "SMA-3" in text and "tf-select α25" in text and "p5–p95" in text:
        return
    end = text.find("</p>")
    if end < 0:
        return
    path.write_text(text[: end + 4] + "\n" + note + text[end + 4 :], encoding="utf-8")


__all__ = [
    "extract_sma12_xy",
    "extract_sma3_xy",
    "extract_named_side_xy",
    "extract_ohlc_customdata",
    "resolve_corridor_series",
    "inject_coin_html",
    "inject_html_dir",
    "maybe_annotate_index",
]

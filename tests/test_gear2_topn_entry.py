"""Gear 2: 1.5 Top-N open gate and spread histogram helpers."""

from __future__ import annotations

import unittest

import numpy as np

from research.gear2_coin_overview_html import group_sorted_coins, nav_neighbors
from research.gear2_regime_topn import (
    BAR_MS,
    causal_composite_at_ticks,
    coin_in_topn,
    completed_bar_start_ms,
    tick_window_for_completed_bar,
    topn_intervals_ms,
    topn_span_note,
)
from research.gear2_spread_plots import (
    BYBIT_PX_NAME,
    TOPN_LEGEND_NAME,
    VOL_NAME,
    RunningSpreadHist,
    format_pcts,
    hist_from_values,
    make_spread_ts_hist_figure,
    spread_percentiles,
    update_stats_from_table,
)
from research.lean_ticks_io import _even_take_per_coin
import pyarrow as pa


class CompletedBarTests(unittest.TestCase):
    def test_tick_uses_previous_closed_bar(self) -> None:
        bar = 300_000
        # ts exactly on a 5m boundary t → bar [t-5m, t)
        self.assertEqual(completed_bar_start_ms(1_200_000), 1_200_000 - bar)
        # ts inside (t, t+5m) still uses [t-5m, t) relative to floor
        self.assertEqual(completed_bar_start_ms(1_200_000 + 1), 1_200_000 - bar)
        self.assertEqual(completed_bar_start_ms(1_200_000 + bar - 1), 1_200_000 - bar)


class TopNFailClosedTests(unittest.TestCase):
    def test_missing_bar_or_coin_is_not_topn(self) -> None:
        b = completed_bar_start_ms(1_200_000)
        topn = {b: frozenset({"AAA", "BTC"})}
        self.assertTrue(coin_in_topn("AAA", 1_200_000, topn))
        self.assertFalse(coin_in_topn("QNT", 1_200_000, topn))
        self.assertFalse(coin_in_topn("USDC", 1_200_000, topn))
        self.assertFalse(coin_in_topn("AAA", 1_200_000 + 300_000, topn))  # next bar absent


class PercentileHistTests(unittest.TestCase):
    def test_percentiles_on_plotted_sample(self) -> None:
        y = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 100.0])
        pcts = spread_percentiles(y)
        self.assertIn(50, pcts)
        self.assertIn(95, pcts)
        self.assertIn(99, pcts)
        self.assertGreaterEqual(pcts[99], pcts[95])
        self.assertGreaterEqual(pcts[95], pcts[50])
        fig = make_spread_ts_hist_figure(
            x_long=["2026-08-05T00:00:00Z", "2026-08-05T00:01:00Z"],
            y_long=[0.1, 0.2],
            x_short=["2026-08-05T00:00:00Z", "2026-08-05T00:01:00Z"],
            y_short=[-0.1, 0.0],
            hist_long=y,
            hist_short=-y,
            title="synthetic",
            thresh=0.5,
            height=360,
            hist_note="percentiles on this plotted sample",
        )
        kinds = {tr.type for tr in fig.data}
        self.assertIn("histogram", kinds)
        self.assertTrue(any(tr.type in ("scatter", "scattergl") for tr in fig.data))
        names = [tr.name for tr in fig.data]
        self.assertIn("spread_long", names)
        self.assertIn(BYBIT_PX_NAME, names)
        self.assertIn(VOL_NAME, names)
        self.assertEqual(sum(1 for tr in fig.data if tr.name == BYBIT_PX_NAME), 1)
        self.assertEqual(sum(1 for tr in fig.data if tr.name == VOL_NAME), 1)
        self.assertIn("p50", format_pcts(pcts))
        # 3 stacked time panels: spread (row 1), price (row 3), 1.5 score (row 4)
        self.assertIsNotNone(fig.layout.yaxis)
        self.assertIsNotNone(fig.layout.yaxis4)
        self.assertIsNotNone(fig.layout.yaxis5)
        self.assertIn("spread", (fig.layout.yaxis.title.text or "").lower())
        self.assertIn("mid", (fig.layout.yaxis4.title.text or "").lower())
        self.assertIn("1.5", (fig.layout.yaxis5.title.text or "").lower())


class PerCoinCapTests(unittest.TestCase):
    def test_even_take_per_coin(self) -> None:
        table = pa.table(
            {
                "base_coin": ["AAA", "AAA", "AAA", "BBB", "BBB"],
                "event_local_ts_ms": [1, 2, 3, 1, 2],
            }
        )
        out = _even_take_per_coin(table, 2)
        df = out.to_pandas()
        self.assertLessEqual(int((df["base_coin"] == "AAA").sum()), 2)
        self.assertLessEqual(int((df["base_coin"] == "BBB").sum()), 2)
        self.assertGreaterEqual(len(df), 3)


class TopNIntervalTests(unittest.TestCase):
    def test_tick_window_matches_coin_in_topn(self) -> None:
        b = 1_200_000
        lo, hi = tick_window_for_completed_bar(b)
        self.assertEqual(lo, b + BAR_MS)
        self.assertEqual(hi, b + 2 * BAR_MS)
        topn = {b: frozenset({"AAA"})}
        self.assertTrue(coin_in_topn("AAA", lo, topn))
        self.assertTrue(coin_in_topn("AAA", hi - 1, topn))
        self.assertFalse(coin_in_topn("AAA", hi, topn))
        self.assertFalse(coin_in_topn("AAA", lo - 1, topn))

    def test_merge_consecutive_and_fail_closed(self) -> None:
        b0 = 1_200_000
        b1 = b0 + BAR_MS
        b3 = b0 + 3 * BAR_MS
        topn = {
            b0: frozenset({"AAA", "BTC"}),
            b1: frozenset({"AAA"}),
            b3: frozenset({"AAA"}),
        }
        spans = topn_intervals_ms("AAA", topn)
        self.assertEqual(len(spans), 2)
        lo0, _ = tick_window_for_completed_bar(b0)
        _, hi1 = tick_window_for_completed_bar(b1)
        self.assertEqual(spans[0], (lo0, hi1))
        self.assertEqual(spans[1], tick_window_for_completed_bar(b3))
        self.assertEqual(topn_intervals_ms("QNT", topn), [])
        self.assertIn("не подстав", topn_span_note("QNT", [], has_hist=False) or "")
        self.assertIn("ни разу", topn_span_note("BBB", [], has_hist=True) or "")


class NavGroupTests(unittest.TestCase):
    def test_prev_next_stays_in_group_and_disables_at_ends(self) -> None:
        crypto, other = group_sorted_coins(
            ["AAPL", "BTC", "ETH", "MSFT"],
            lambda c: c in {"BTC", "ETH"},
        )
        self.assertEqual(crypto, ["BTC", "ETH"])
        self.assertEqual(other, ["AAPL", "MSFT"])
        self.assertEqual(nav_neighbors(crypto, "BTC"), (None, "ETH"))
        self.assertEqual(nav_neighbors(crypto, "ETH"), ("BTC", None))
        self.assertEqual(nav_neighbors(other, "AAPL"), (None, "MSFT"))
        self.assertEqual(nav_neighbors(other, "MSFT"), ("AAPL", None))
        # wrapping would put ETH next to AAPL — must not
        prev_c, next_c = nav_neighbors(crypto, "ETH")
        self.assertNotEqual(next_c, "AAPL")
        self.assertIsNone(next_c)


class AllTickHistTests(unittest.TestCase):
    def test_running_hist_near_numpy_percentile(self) -> None:
        rng = np.random.default_rng(0)
        y = rng.normal(0.1, 0.2, size=20_000)
        y = y[(y > -2) & (y < 2)]
        rh = RunningSpreadHist(lo=-10.0, hi=10.0, n_bins=20_000)
        rh.update(y)
        exact = spread_percentiles(y)
        approx = rh.percentiles()
        for p in (50, 95, 99):
            self.assertAlmostEqual(approx[p], exact[p], delta=0.005)

    def test_display_bars_window_is_tighter_than_full_domain(self) -> None:
        rng = np.random.default_rng(1)
        y = rng.normal(0.05, 0.12, size=30_000)
        rh = RunningSpreadHist(lo=-10.0, hi=10.0, n_bins=20_000)
        rh.update(y)
        pcts_before = rh.percentiles()
        edges, _counts = rh.display_bars()
        pcts_after = rh.percentiles()
        self.assertEqual(pcts_before[50], pcts_after[50])
        self.assertEqual(pcts_before[95], pcts_after[95])
        self.assertEqual(pcts_before[99], pcts_after[99])
        span = float(edges[-1] - edges[0])
        self.assertLess(span, 4.0)
        self.assertGreater(span, 0.2)
        self.assertEqual(rh.lo, -10.0)
        self.assertEqual(rh.hi, 10.0)

    def test_figure_legend_below_plot_and_hist_x_is_tight(self) -> None:
        rng = np.random.default_rng(2)
        y = rng.normal(0.0, 0.15, size=8_000)
        rh = RunningSpreadHist(lo=-10.0, hi=10.0, n_bins=20_000)
        rh.update(y)
        edges, counts = rh.display_bars()
        pcts = rh.percentiles()
        fig = make_spread_ts_hist_figure(
            x_long=["2026-08-05T00:00:00Z", "2026-08-05T00:01:00Z"],
            y_long=[0.1, 0.2],
            x_short=["2026-08-05T00:00:00Z", "2026-08-05T00:01:00Z"],
            y_short=[-0.1, 0.0],
            title="layout",
            hist_long_binned=(edges, counts),
            hist_short_binned=(edges, counts),
            hist_long_pcts=pcts,
            hist_short_pcts=pcts,
            hist_note="квантили и гистограммы — по всем тикам",
            height=640,
        )
        self.assertLessEqual(float(fig.layout.legend.y or 0), 0.05)
        xr = fig.layout.xaxis2.range
        self.assertIsNotNone(xr)
        self.assertLess(float(xr[1]) - float(xr[0]), 4.0)

    def test_hist_from_values_exact_percentiles(self) -> None:
        y = np.linspace(-1.0, 1.0, 1001)
        edges, counts, pcts = hist_from_values(y, n_bars=40)
        exact = spread_percentiles(y)
        self.assertEqual(pcts[50], exact[50])
        self.assertGreater(int(counts.sum()), 0)
        self.assertEqual(len(edges) - 1, 40)

    def test_streamed_stats_count_all_rows(self) -> None:
        table = pa.table(
            {
                "event_local_ts_ms": [1_000, 2_000, 86_400_000 + 3_000],
                "base_coin": ["AAA", "AAA", "AAA"],
                "okx_bid_price": [1.0, 1.0, 1.0],
                "okx_ask_price": [1.01, 1.01, 1.01],
                "bybit_bid_price": [1.02, 1.02, 1.02],
                "bybit_ask_price": [1.03, 1.03, 1.03],
            }
        )
        accs: dict = {}
        update_stats_from_table(accs, table)
        self.assertIn("AAA", accs)
        self.assertEqual(accs["AAA"].n_ticks, 3)
        self.assertEqual(sum(accs["AAA"].day_counts.values()), 3)

    def test_figure_accepts_binned_hist_and_topn_spans(self) -> None:
        y = np.array([0.0, 0.1, 0.2, 0.3])
        edges, counts, pcts = hist_from_values(y, n_bars=8)
        b = 1_200_000
        spans = topn_intervals_ms("AAA", {b: frozenset({"AAA"})})
        fig = make_spread_ts_hist_figure(
            x_long=["2026-08-05T00:05:00Z", "2026-08-05T00:10:00Z"],
            y_long=[0.1, 0.2],
            x_short=["2026-08-05T00:05:00Z", "2026-08-05T00:10:00Z"],
            y_short=[-0.1, 0.0],
            title="synthetic all-tick",
            hist_long_binned=(edges, counts),
            hist_short_binned=(edges, counts),
            hist_long_pcts=pcts,
            hist_short_pcts=pcts,
            hist_note="квантили и гистограммы — по всем тикам; линия — прореженная",
            topn_intervals_ms=spans,
            height=360,
        )
        kinds = {tr.type for tr in fig.data}
        self.assertIn("bar", kinds)
        names = [tr.name for tr in fig.data]
        self.assertIn(TOPN_LEGEND_NAME, names)
        title = fig.layout.title.text or ""
        self.assertIn("всем тикам", title)


class HtmlPageTests(unittest.TestCase):
    def test_index_has_two_lists_and_coin_nav_stays_in_group(self) -> None:
        import tempfile
        from pathlib import Path

        from research.gear2_coin_overview_html import (
            KLASS_CRYPTO,
            KLASS_OTHER,
            write_coin_html,
            write_index,
        )

        rows = [
            {
                "coin": "BTC",
                "klass": KLASS_CRYPTO,
                "href": "BTC.html",
                "n_all": 10,
                "n_plot": 8,
                "n_days": 15,
                "missing": "—",
                "p_long": "p50=0",
                "p_short": "p50=0",
                "n_topn": 2,
            },
            {
                "coin": "ETH",
                "klass": KLASS_CRYPTO,
                "href": "ETH.html",
                "n_all": 9,
                "n_plot": 8,
                "n_days": 15,
                "missing": "—",
                "p_long": "p50=0",
                "p_short": "p50=0",
                "n_topn": 0,
            },
            {
                "coin": "AAPL",
                "klass": KLASS_OTHER,
                "href": "AAPL.html",
                "n_all": 7,
                "n_plot": 7,
                "n_days": 15,
                "missing": "—",
                "p_long": "p50=0",
                "p_short": "p50=0",
                "n_topn": 0,
            },
        ]
        fig = make_spread_ts_hist_figure(
            x_long=["2026-08-05T00:00:00Z"],
            y_long=[0.1],
            x_short=["2026-08-05T00:00:00Z"],
            y_short=[-0.1],
            title="t",
            height=240,
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            write_index(out / "index.html", rows, n_files=3, elapsed_s=1.0, note="n")
            idx = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn("<h2>Крипто</h2>", idx)
            self.assertIn("<h2>Не крипто</h2>", idx)
            self.assertIn("id=\"crypto\"", idx)
            self.assertIn("id=\"non-crypto\"", idx)
            self.assertIn("всем тикам", idx)
            write_coin_html(
                out / "ETH.html",
                "ETH",
                KLASS_CRYPTO,
                fig,
                "<p>квантили и гистограммы — по всем тикам</p>",
                "plotly.min.js",
                prev_coin="BTC",
                next_coin=None,
                group_label=KLASS_CRYPTO,
            )
            eth = (out / "ETH.html").read_text(encoding="utf-8")
            self.assertIn('id="nav-prev"', eth)
            self.assertIn("BTC.html", eth)
            self.assertIn("конец списка", eth)
            self.assertNotIn("AAPL.html", eth)
            self.assertIn("всем тикам", eth)
            self.assertIn("ArrowLeft", eth)
            self.assertIn('id="period-panel"', eth)
            self.assertIn("Начало (UTC)", eth)
            self.assertIn("period-plot", eth)
            self.assertIn("overview_period.js", eth)
            self.assertIn("gear2_overview_server.py", eth)
            self.assertIn("всем тикам", idx)
            self.assertIn("gear2_overview_server.py", idx)


class CausalScoreAlignTests(unittest.TestCase):
    def test_tick_uses_completed_bar_not_lookahead(self) -> None:
        import pandas as pd

        features = pd.DataFrame(
            {
                "bar_start_ts_ms": [900_000, 1_200_000],
                "composite_geom": [1.5, 9.9],
            }
        )
        # ts on 5m boundary t=1_200_000 → bar [t−5m, t) = 900_000, not 1_200_000
        s = causal_composite_at_ticks([1_200_000, 1_200_001, 1_499_999], features)
        self.assertEqual(list(s), [1.5, 1.5, 1.5])
        s_next = causal_composite_at_ticks([1_500_000], features)
        self.assertEqual(float(s_next[0]), 9.9)

    def test_missing_features_are_nan(self) -> None:
        import numpy as np

        s = causal_composite_at_ticks([1_200_000], None)
        self.assertTrue(np.isnan(s).all())


class PeriodUiTests(unittest.TestCase):
    def test_inject_is_idempotent_and_mentions_utc_server(self) -> None:
        from research.gear2_coin_overview_html import (
            FILE_SERVER_NOTE,
            MAX_ALL_TICK_POINTS,
            inject_period_into_page,
        )

        page = """<!DOCTYPE html>
<html><head><style>body{}</style></head>
<body>
<div class="wrap">
<h1>0G</h1>
<div class="plot-slot">overview</div>
</div>
<script>
document.addEventListener("keydown", function (e) {});
</script>
</body></html>
"""
        once = inject_period_into_page(
            page,
            "0G",
            calendar_start="2026-08-05T00:00:00Z",
            calendar_end="2026-08-20T00:00:00Z",
            period_start="2026-08-19T11:00:00Z",
            period_end="2026-08-19T12:00:00Z",
            max_points=MAX_ALL_TICK_POINTS,
        )
        self.assertIn('id="period-panel"', once)
        self.assertIn("Начало (UTC)", once)
        self.assertIn("period-plot", once)
        self.assertIn("overview_period.js", once)
        self.assertIn("локальный сервер", FILE_SERVER_NOTE)
        self.assertIn("локальный сервер", once)
        self.assertEqual(once.count('id="period-panel"'), 1)
        twice = inject_period_into_page(
            once,
            "0G",
            calendar_start="2026-08-05T00:00:00Z",
            calendar_end="2026-08-20T00:00:00Z",
            period_start="2026-08-19T11:00:00Z",
            period_end="2026-08-19T12:00:00Z",
            max_points=MAX_ALL_TICK_POINTS,
        )
        self.assertEqual(once, twice)

    def test_window_too_wide_does_not_downsample(self) -> None:
        from research.gear2_coin_overview_html import WindowTooWideError

        err = WindowTooWideError(400_000, 300_000)
        self.assertEqual(err.n, 400_000)
        self.assertEqual(err.max_points, 300_000)
        self.assertIn("Сузьте период", str(err))
        self.assertIn("без прореживания", str(err))

    def test_default_period_uses_last_file_hour(self) -> None:
        from pathlib import Path

        from research.gear2_coin_overview_html import default_period_bounds, ms_to_iso_z
        from research.lean_ticks_io import parse_ts_ms

        last = Path("spread_20260819T115500Z_20260819T120000Z.parquet")
        ps, pe = default_period_bounds(
            parse_ts_ms("2026-08-05T00:00:00Z"),
            parse_ts_ms("2026-08-20T00:00:00Z"),
            [last],
        )
        self.assertEqual(ms_to_iso_z(pe), "2026-08-19T12:00:00Z")
        self.assertEqual(ms_to_iso_z(ps), "2026-08-19T11:00:00Z")


class OverviewServerBusyPortTests(unittest.TestCase):
    def test_busy_port_message_for_overview_server(self) -> None:
        from research.gear2_overview_server import format_busy_port_message

        msg = format_busy_port_message(
            "127.0.0.1",
            8765,
            [
                (
                    "Python",
                    8761,
                    "python research/gear2_overview_server.py --no-patch-html --port 8765",
                )
            ],
        )
        self.assertIn("уже занят", msg)
        self.assertIn("PID 8761", msg)
        self.assertIn("gear2_overview_server.py", msg)
        self.assertIn("уже запущен", msg)
        self.assertIn("http://127.0.0.1:8765/index.html", msg)
        self.assertIn("kill 8761", msg)
        self.assertIn("--port", msg)

    def test_busy_port_message_unrelated_does_not_suggest_kill(self) -> None:
        from research.gear2_overview_server import format_busy_port_message

        msg = format_busy_port_message(
            "127.0.0.1",
            8765,
            [("nginx", 99, "nginx: master process")],
        )
        self.assertIn("другим процессом", msg)
        self.assertNotIn("kill 99", msg)
        self.assertIn("--port", msg)

    def test_is_addr_in_use_detects_errno(self) -> None:
        import errno

        from research.gear2_overview_server import _is_addr_in_use

        self.assertTrue(_is_addr_in_use(OSError(errno.EADDRINUSE, "Address already in use")))
        self.assertFalse(_is_addr_in_use(OSError(errno.EPERM, "denied")))


if __name__ == "__main__":
    unittest.main()

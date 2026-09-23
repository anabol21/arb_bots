"""Hermetic tests for gear-2.2 trade Plotly windows and side markers."""

from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from research.gear22_backtest.plot_trades import (
    PAD_S,
    close_spread_column,
    iter_trade_windows,
    load_window,
    open_spread_column,
    trades_figure,
    utc_date_strings,
    window_title,
)
from research.gear22_backtest.replay import OpenPosition

# 2026-08-12 00:00:00Z and a midnight-spanning pair.
_TS_MIDNIGHT = 1_786_492_800  # 2026-08-12T00:00:00Z
_TS_BEFORE = _TS_MIDNIGHT - 60
_TS_AFTER = _TS_MIDNIGHT + 60


def _trade_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = dict(
        coin="SOL",
        side="long",
        ts_open=1_000_000,
        ts_close=1_000_100,
        fill_spread_pp=0.40,
        exit_spread_pp=0.05,
        potential_pp=0.15,
        reason_open="open_long",
        reason_close="close_min_profit",
        status="closed",
    )
    row.update(overrides)
    return row


class TestGear22TradeWindows(unittest.TestCase):
    def test_pad_15_minutes(self) -> None:
        df = pd.DataFrame([_trade_row(ts_open=10_000, ts_close=10_500)])
        windows = iter_trade_windows(df, pad_s=PAD_S)
        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.t_left, 10_000 - 900)
        self.assertEqual(w.t_right, 10_500 + 900)
        self.assertEqual(w.hold_s, 500)
        self.assertEqual(w.ts_close, 10_500)

    def test_unclosed_uses_last_ts_s(self) -> None:
        df = pd.DataFrame(
            [
                _trade_row(
                    ts_close=float("nan"),
                    status="open",
                    reason_close="unclosed",
                    exit_spread_pp=-0.50,
                    potential_pp=-0.40,
                )
            ]
        )
        pos = OpenPosition(
            coin="SOL",
            side="long",
            ts_open=1_000_000,
            fill_spread_pp=0.40,
            reason_open="open_long",
            last_ts_s=1_002_000,
            last_potential_pp=-0.40,
            last_exit_spread_pp=-0.50,
        )
        windows = iter_trade_windows(df, [pos], pad_s=900)
        self.assertEqual(len(windows), 1)
        w = windows[0]
        self.assertEqual(w.ts_end, 1_002_000)
        self.assertIsNone(w.ts_close)
        self.assertEqual(w.t_right, 1_002_000 + 900)
        self.assertEqual(w.status, "open")
        self.assertIn("unclosed", window_title(w, 1))

    def test_sort_ts_open_then_coin(self) -> None:
        df = pd.DataFrame(
            [
                _trade_row(coin="ZZZ", ts_open=5, ts_close=6),
                _trade_row(coin="AAA", ts_open=5, ts_close=6),
                _trade_row(coin="MMM", ts_open=1, ts_close=2),
            ]
        )
        coins = [w.coin for w in iter_trade_windows(df)]
        self.assertEqual(coins, ["MMM", "AAA", "ZZZ"])

    def test_utc_dates_span_midnight(self) -> None:
        days = utc_date_strings(_TS_BEFORE - 10, _TS_AFTER + 10)
        self.assertEqual(days, ["2026-08-11", "2026-08-12"])

    def test_open_close_spread_columns(self) -> None:
        self.assertEqual(open_spread_column("long"), "spread_last_long")
        self.assertEqual(close_spread_column("long"), "spread_last_short")
        self.assertEqual(open_spread_column("short"), "spread_last_short")
        self.assertEqual(close_spread_column("short"), "spread_last_long")


class TestGear22TradeHiveSlice(unittest.TestCase):
    def test_load_window_concat_days_and_clip(self) -> None:
        with self._tmp_hive() as hive:
            sl = load_window(hive, "SOL", _TS_BEFORE - 30, _TS_AFTER + 30)
            ts = list(sl["ts_s"])
            self.assertEqual(ts, [_TS_BEFORE, _TS_MIDNIGHT, _TS_AFTER])
            self.assertTrue((sl["coin"].astype(str) == "SOL").all())
            missing = load_window(hive, "SOL", 0, 10)
            self.assertTrue(missing.empty)

    def test_figure_fill_exit_use_side_columns(self) -> None:
        with self._tmp_hive() as hive:
            df = pd.DataFrame(
                [
                    _trade_row(
                        ts_open=_TS_BEFORE,
                        ts_close=_TS_AFTER,
                        fill_spread_pp=1.11,
                        exit_spread_pp=2.22,
                    )
                ]
            )
            fig = trades_figure(hive, df, pad_s=60)
            self.assertIsNotNone(fig)
            assert fig is not None
            self.assertEqual(len(fig.data), 8)
            fill = fig.data[6]
            exit_tr = fig.data[7]
            self.assertAlmostEqual(float(fill.y[0]), 0.40)  # hive spread_last_long
            self.assertAlmostEqual(float(exit_tr.y[0]), 0.50)  # hive spread_last_short
            self.assertEqual(len(fig.layout.updatemenus[0].buttons), 1)
            self.assertEqual(len(fig.layout.sliders[0].steps), 1)
            self.assertEqual(list(fig.layout.sliders[0].steps[0].args[0]), ["0"])


    def test_two_trades_slider_does_not_copy_xy(self) -> None:
        with self._tmp_hive() as hive:
            df = pd.DataFrame(
                [
                    _trade_row(
                        coin="SOL",
                        ts_open=_TS_BEFORE,
                        ts_close=_TS_BEFORE,
                        fill_spread_pp=0.40,
                        exit_spread_pp=0.10,
                    ),
                    _trade_row(
                        coin="BTC",
                        ts_open=_TS_AFTER,
                        ts_close=_TS_AFTER,
                        side="short",
                        fill_spread_pp=0.50,
                        exit_spread_pp=0.41,
                    ),
                ]
            )
            fig = trades_figure(hive, df, pad_s=60)
            assert fig is not None
            self.assertEqual(len(fig.frames), 2)
            self.assertEqual(list(fig.layout.sliders[0].steps[1].args[0]), ["1"])
            self.assertNotIn("x", fig.layout.sliders[0].steps[1].args[0])

    def test_write_html_standalone(self) -> None:
        import tempfile

        from research.gear22_backtest.plot_trades import write_trades_html

        with self._tmp_hive() as hive:
            df = pd.DataFrame(
                [
                    _trade_row(
                        ts_open=_TS_BEFORE,
                        ts_close=_TS_AFTER,
                        fill_spread_pp=0.40,
                        exit_spread_pp=0.50,
                    )
                ]
            )
            fig = trades_figure(hive, df, pad_s=60)
            with tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "trades.html"
                out = write_trades_html(fig, path, verbose=False)
                self.assertTrue(out.is_file())
                self.assertGreater(out.stat().st_size, 1000)
                text = out.read_text(encoding="utf-8")
            self.assertIn("Plotly", text)
            self.assertIn("theta_1m", text)

    def test_downsample_keeps_open_close_no_fill(self) -> None:
        from research.gear22_backtest.plot_trades import (
            downsample_slice,
            iter_trade_windows,
        )

        ts = list(range(10_000, 15_000))
        sl = pd.DataFrame(
            {
                "ts_s": ts,
                "coin": ["SOL"] * len(ts),
                "theta_1m_long": [float("nan")] * 10 + [1.0] * (len(ts) - 10),
                "theta_1m_short": [0.0] * len(ts),
                "p50_1m_long": [0.0] * len(ts),
                "p50_1m_short": [0.0] * len(ts),
                "spread_last_long": [0.1] * len(ts),
                "spread_last_short": [0.2] * len(ts),
            }
        )
        df = pd.DataFrame(
            [_trade_row(ts_open=10_000, ts_close=14_999, fill_spread_pp=0.1)]
        )
        window = iter_trade_windows(df, pad_s=0)[0]
        out = downsample_slice(sl, window, max_points=200)
        self.assertLessEqual(len(out), 205)
        self.assertIn(10_000, set(out["ts_s"]))
        self.assertIn(14_999, set(out["ts_s"]))
        self.assertTrue(out["theta_1m_long"].isna().any())

    def test_empty_trades_return_none(self) -> None:
        self.assertIsNone(trades_figure("unused", pd.DataFrame()))

    def _tmp_hive(self):
        import tempfile
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self._write_day(
                    root,
                    "2026-08-11",
                    [_TS_BEFORE],
                    spread_long=0.40,
                    spread_short=0.10,
                )
                self._write_day(
                    root,
                    "2026-08-12",
                    [_TS_MIDNIGHT, _TS_AFTER],
                    spread_long=0.41,
                    spread_short=0.50,
                )
                yield root

        return _ctx()

    def _write_day(
        self,
        hive: Path,
        day: str,
        ts_list: list[int],
        *,
        spread_long: float,
        spread_short: float,
    ) -> None:
        n = len(ts_list)
        # Two coins so coin filter is load-bearing.
        rows = {
            "ts_s": ts_list + ts_list,
            "coin": ["SOL"] * n + ["BTC"] * n,
            "theta_1m_long": [0.5] * (2 * n),
            "theta_1m_short": [0.1] * (2 * n),
            "p50_1m_long": [0.6] * (2 * n),
            "p50_1m_short": [0.2] * (2 * n),
            "spread_last_long": [spread_long] * (2 * n),
            "spread_last_short": [spread_short] * (2 * n),
        }
        folder = hive / f"event_date={day}"
        folder.mkdir(parents=True)
        table = pa.Table.from_pandas(pd.DataFrame(rows), preserve_index=False)
        pq.write_table(table, folder / "part-000.parquet")


if __name__ == "__main__":
    unittest.main()

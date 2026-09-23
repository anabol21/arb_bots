"""Hermetic tests for the gear-2.2 Bybit/OKX venue cash ledger."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from research.gear22_backtest.params_frozen import FROZEN
from research.gear22_backtest.venue_ledger import (
    BYBIT_TAKER_FEE,
    FIXED_NOTIONAL,
    OKX_TAKER_FEE,
    START_CASH,
    Book,
    PricedOpen,
    PricedRound,
    align_bucket_5m_start,
    approx_potential_pp,
    approx_usd,
    approx_vs_cash_usd_5m,
    book_matches_spread,
    bucket_5m_mark_ts,
    hour_mark_ts,
    hourly_equity_series,
    iter_bucket_5m_starts,
    iter_hour_starts,
    load_last_books,
    load_last_books_1hz,
    mark_open_equity,
    one_step_neighbors,
    run_ledger,
    spread_pp,
)


def _book(
    bybit_bid: float,
    bybit_ask: float,
    okx_bid: float,
    okx_ask: float,
) -> Book:
    return Book(
        bybit_bid=bybit_bid,
        bybit_ask=bybit_ask,
        okx_bid=okx_bid,
        okx_ask=okx_ask,
    )


def _round(
    side: str,
    open_book: Book,
    close_book: Book,
    *,
    potential_pp: float = 0.5,
    prices_match: bool = True,
    ts_open: int = 1_000,
    ts_close: int = 1_100,
    coin: str = "SOL",
) -> PricedRound:
    return PricedRound(
        coin=coin,
        side=side,  # type: ignore[arg-type]
        ts_open=ts_open,
        ts_close=ts_close,
        open_book=open_book,
        close_book=close_book,
        potential_pp=potential_pp,
        prices_match=prices_match,
    )


class TestSidesAndFees(unittest.TestCase):
    def test_long_bybit_loses_when_bybit_rises(self) -> None:
        # Open long: sell Bybit bid 100 / buy OKX ask 100.
        # Close: buy Bybit ask 110 / sell OKX bid 110.
        open_b = _book(100.0, 100.1, 99.9, 100.0)
        close_b = _book(109.9, 110.0, 110.0, 110.1)
        result = run_ledger([_round("long", open_b, close_b)], "fixed100")
        self.assertEqual(result.n_applied, 1)
        self.assertLess(result.steps[0].pnl_bybit or 0.0, 0.0)
        self.assertGreater(result.steps[0].pnl_okx or 0.0, 0.0)

    def test_short_opposite_signs(self) -> None:
        open_b = _book(100.0, 100.0, 100.0, 100.0)
        # Same price move as long test: Bybit long gains, OKX short loses.
        close_b = _book(109.9, 110.0, 110.0, 110.1)
        result = run_ledger([_round("short", open_b, close_b)], "fixed100")
        self.assertEqual(result.n_applied, 1)
        self.assertGreater(result.steps[0].pnl_bybit or 0.0, 0.0)
        self.assertLess(result.steps[0].pnl_okx or 0.0, 0.0)

    def test_four_fees_flat_prices(self) -> None:
        flat = _book(100.0, 100.0, 100.0, 100.0)
        result = run_ledger([_round("long", flat, flat)], "fixed100")
        step = result.steps[0]
        self.assertAlmostEqual(step.fee_bybit or 0.0, 0.20, places=10)
        self.assertAlmostEqual(step.fee_okx or 0.0, 0.10, places=10)
        self.assertAlmostEqual(result.cash_bybit, 100.0 - 0.20, places=10)
        self.assertAlmostEqual(result.cash_okx, 100.0 - 0.10, places=10)

    def test_close_fee_uses_exit_price(self) -> None:
        open_b = _book(100.0, 100.0, 100.0, 100.0)
        # Long close: Bybit ask 200, OKX bid 200. qty=1 each.
        close_b = _book(199.0, 200.0, 200.0, 201.0)
        result = run_ledger([_round("long", open_b, close_b)], "fixed100")
        step = result.steps[0]
        qty = FIXED_NOTIONAL / 100.0
        fee_b = BYBIT_TAKER_FEE * qty * 100.0 + BYBIT_TAKER_FEE * qty * 200.0
        fee_o = OKX_TAKER_FEE * qty * 100.0 + OKX_TAKER_FEE * qty * 200.0
        self.assertAlmostEqual(step.fee_bybit or 0.0, fee_b, places=10)
        self.assertAlmostEqual(step.fee_okx or 0.0, fee_o, places=10)


class TestModes(unittest.TestCase):
    def test_v1_cashes_diverge_and_stay(self) -> None:
        flat = _book(100.0, 100.0, 100.0, 100.0)
        result = run_ledger([_round("long", flat, flat)], "fixed100")
        self.assertNotAlmostEqual(result.cash_bybit, result.cash_okx)
        self.assertAlmostEqual(result.cash_bybit, 99.80, places=8)
        self.assertAlmostEqual(result.cash_okx, 99.90, places=8)

    def test_v2_equal_after_round(self) -> None:
        flat = _book(100.0, 100.0, 100.0, 100.0)
        result = run_ledger([_round("long", flat, flat)], "fixed100_equalize")
        self.assertAlmostEqual(result.cash_bybit, result.cash_okx, places=10)
        mid = (99.80 + 99.90) / 2.0
        self.assertAlmostEqual(result.cash_bybit, mid, places=10)

    def test_v3_second_notional_equals_equalized(self) -> None:
        flat = _book(100.0, 100.0, 100.0, 100.0)
        result = run_ledger(
            [_round("long", flat, flat, ts_open=1), _round("long", flat, flat, ts_open=2)],
            "balance_equalize",
        )
        self.assertEqual(result.n_applied, 2)
        first_mid = (99.80 + 99.90) / 2.0
        self.assertAlmostEqual(result.steps[0].cash_bybit, first_mid, places=10)
        self.assertAlmostEqual(result.steps[1].notional or 0.0, first_mid, places=10)

    def test_v1_leverage_when_cash_below_100(self) -> None:
        flat = _book(100.0, 100.0, 100.0, 100.0)
        result = run_ledger(
            [_round("long", flat, flat)],
            "fixed100",
            start_bybit=50.0,
            start_okx=50.0,
        )
        self.assertEqual(result.steps[0].notional, 100.0)
        # Fees alone push cash negative-ish relative to 50 start.
        self.assertAlmostEqual(result.cash_bybit, 50.0 - 0.20, places=10)

    def test_v1_still_notional_100_after_negative(self) -> None:
        # Drive Bybit cash negative, then another $100 round still books.
        open_b = _book(100.0, 100.0, 100.0, 100.0)
        # Long: Bybit short loses hard (bid 100 → ask 300).
        close_b = _book(299.0, 300.0, 100.0, 100.1)
        r1 = _round("long", open_b, close_b, ts_open=1, ts_close=2)
        r2 = _round("long", open_b, open_b, ts_open=3, ts_close=4)
        result = run_ledger([r1, r2], "fixed100")
        self.assertEqual(result.n_applied, 2)
        self.assertLess(result.steps[0].cash_bybit, 0.0)
        self.assertEqual(result.steps[1].notional, 100.0)

    def test_v3_skips_when_sum_nonpositive(self) -> None:
        flat = _book(100.0, 100.0, 100.0, 100.0)
        result = run_ledger(
            [_round("long", flat, flat)],
            "balance_equalize",
            start_bybit=-10.0,
            start_okx=5.0,
        )
        self.assertEqual(result.n_applied, 0)
        self.assertEqual(result.n_skipped_nonpositive, 1)
        self.assertEqual(result.steps[0].status, "skipped_nonpositive")
        self.assertAlmostEqual(result.cash_bybit, -10.0, places=10)
        self.assertAlmostEqual(result.cash_okx, 5.0, places=10)


class TestPriceMismatch(unittest.TestCase):
    def test_mismatch_does_not_add_potential_and_continues(self) -> None:
        flat = _book(100.0, 100.0, 100.0, 100.0)
        bad = _round("long", flat, flat, potential_pp=12.34, prices_match=False)
        good = _round("long", flat, flat, potential_pp=0.5, ts_open=2, ts_close=3)
        result = run_ledger([bad, good], "fixed100")
        self.assertEqual(result.n_price_mismatch, 1)
        self.assertEqual(result.n_skipped_hole, 0)
        self.assertEqual(result.n_not_applied, 0)
        self.assertEqual(result.n_applied, 1)
        self.assertTrue(result.complete)
        self.assertEqual(result.steps[0].status, "price_mismatch")
        self.assertEqual(result.steps[1].status, "closed")
        # potential_pp is recorded on the step but never becomes cash.
        self.assertEqual(result.steps[0].potential_pp, 12.34)
        self.assertIsNone(result.steps[0].pnl_bybit)
        # Only the good round's fees hit cash (flat long).
        self.assertAlmostEqual(result.cash_bybit, START_CASH - 0.20, places=10)
        self.assertAlmostEqual(result.cash_okx, START_CASH - 0.10, places=10)

    def test_missing_book_skip_does_not_freeze_later(self) -> None:
        flat = _book(100.0, 100.0, 100.0, 100.0)
        hole = PricedRound(
            coin="SOL",
            side="long",
            ts_open=1,
            ts_close=2,
            open_book=_book(float("nan"), float("nan"), float("nan"), float("nan")),
            close_book=flat,
            potential_pp=9.9,
            prices_match=False,
            is_hole=True,
        )
        later = _round("long", flat, flat, potential_pp=0.5, ts_open=3, ts_close=4)
        result = run_ledger([hole, later], "fixed100")
        self.assertEqual(result.n_skipped_hole, 1)
        self.assertEqual(result.n_price_mismatch, 0)
        self.assertEqual(result.n_not_applied, 0)
        self.assertEqual(result.n_applied, 1)
        self.assertEqual(result.steps[0].status, "skipped_hole")
        self.assertEqual(result.steps[1].status, "closed")
        self.assertAlmostEqual(result.cash_bybit, START_CASH - 0.20, places=10)
        self.assertAlmostEqual(result.cash_okx, START_CASH - 0.10, places=10)


class TestLoadLastBooks(unittest.TestCase):
    def test_picks_last_tick_at_or_before_t_ms(self) -> None:
        # Second boundary 12:00:00 UTC on 2026-08-01. Feature join is
        # last tick with event_local_ts_ms <= ts_s * 1000.
        start = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 8, 1, 12, 5, 0, tzinfo=timezone.utc)
        t0 = int(start.timestamp() * 1000)
        fname = (
            f"spread_{start.strftime('%Y%m%dT%H%M%SZ')}_"
            f"{end.strftime('%Y%m%dT%H%M%SZ')}.parquet"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / fname
            # Ticks at/before the boundary plus one after; pick last <= t0.
            ts = [t0 - 800, t0 - 200, t0, t0 + 500]
            table = pa.table(
                {
                    "event_local_ts_ms": pa.array(ts, type=pa.int64()),
                    "base_coin": pa.array(["SOL"] * 4, type=pa.string()),
                    "bybit_bid_price": pa.array([10.0, 20.0, 30.0, 99.0]),
                    "bybit_ask_price": pa.array([10.1, 20.1, 30.1, 99.1]),
                    "okx_bid_price": pa.array([10.0, 20.0, 30.0, 99.0]),
                    "okx_ask_price": pa.array([10.1, 20.1, 30.1, 99.1]),
                }
            )
            pq.write_table(table, path)
            books = load_last_books(root, [("SOL", t0 // 1000)])
            self.assertIn(("SOL", t0 // 1000), books)
            book = books[("SOL", t0 // 1000)]
            self.assertAlmostEqual(book.bybit_bid, 30.0)
            self.assertNotAlmostEqual(book.bybit_bid, 99.0)

    def test_1hz_sidecar_fills_lean_gap_without_overriding(self) -> None:
        # Lean covers only an early second; 1 Hz part covers a later gap key.
        lean_start = datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc)
        lean_end = datetime(2026, 8, 27, 11, 5, 0, tzinfo=timezone.utc)
        lean_t = int(lean_start.timestamp() * 1000) + 30_000
        gap_t = int(
            datetime(2026, 8, 27, 12, 0, 5, tzinfo=timezone.utc).timestamp() * 1000
        )
        lean_name = (
            f"spread_{lean_start.strftime('%Y%m%dT%H%M%SZ')}_"
            f"{lean_end.strftime('%Y%m%dT%H%M%SZ')}.parquet"
        )
        with tempfile.TemporaryDirectory() as td:
            lean_root = Path(td) / "lean"
            hz_root = Path(td) / "hz"
            lean_root.mkdir()
            hz_root.mkdir()
            pq.write_table(
                pa.table(
                    {
                        "event_local_ts_ms": pa.array([lean_t], type=pa.int64()),
                        "base_coin": pa.array(["SOL"], type=pa.string()),
                        "bybit_bid_price": pa.array([11.0]),
                        "bybit_ask_price": pa.array([11.1]),
                        "okx_bid_price": pa.array([11.0]),
                        "okx_ask_price": pa.array([11.1]),
                    }
                ),
                lean_root / lean_name,
            )
            pq.write_table(
                pa.table(
                    {
                        "ts_s": pa.array([gap_t // 1000], type=pa.int64()),
                        "base_coin": pa.array(["SOL"], type=pa.string()),
                        "event_local_ts_ms": pa.array([gap_t], type=pa.int64()),
                        "bybit_bid_price": pa.array([22.0]),
                        "bybit_ask_price": pa.array([22.1]),
                        "okx_bid_price": pa.array([22.0]),
                        "okx_ask_price": pa.array([22.1]),
                    }
                ),
                hz_root / "part-20260827T120000Z.parquet",
            )
            # Lean-only key keeps lean price; gap key uses sidecar.
            books = load_last_books(
                lean_root,
                [("SOL", lean_t // 1000), ("SOL", gap_t // 1000)],
                books_1hz_dir=hz_root,
            )
            self.assertAlmostEqual(books[("SOL", lean_t // 1000)].bybit_bid, 11.0)
            self.assertAlmostEqual(books[("SOL", gap_t // 1000)].bybit_bid, 22.0)

    def test_lean_reads_next_named_window_for_early_rows(self) -> None:
        # Query 12:04:44. The tick 10 ms before the boundary is stored in the
        # file labeled 12:05–12:10. The previous file only has an older tick.
        boundary = datetime(2026, 8, 1, 12, 4, 44, tzinfo=timezone.utc)
        t_ms = int(boundary.timestamp() * 1000)
        slots = [
            datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 1, 12, 5, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 1, 12, 10, 0, tzinfo=timezone.utc),
        ]

        def _name(a: datetime, b: datetime) -> str:
            return (
                f"spread_{a.strftime('%Y%m%dT%H%M%SZ')}_"
                f"{b.strftime('%Y%m%dT%H%M%SZ')}.parquet"
            )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pq.write_table(
                pa.table(
                    {
                        "event_local_ts_ms": pa.array([t_ms - 8_000], type=pa.int64()),
                        "base_coin": pa.array(["CAP"], type=pa.string()),
                        "bybit_bid_price": pa.array([1.0]),
                        "bybit_ask_price": pa.array([1.1]),
                        "okx_bid_price": pa.array([1.0]),
                        "okx_ask_price": pa.array([1.1]),
                    }
                ),
                root / _name(slots[0], slots[1]),
            )
            pq.write_table(
                pa.table(
                    {
                        "event_local_ts_ms": pa.array(
                            [t_ms - 10, t_ms + 500], type=pa.int64()
                        ),
                        "base_coin": pa.array(["CAP", "CAP"], type=pa.string()),
                        "bybit_bid_price": pa.array([2.0, 99.0]),
                        "bybit_ask_price": pa.array([2.1, 99.1]),
                        "okx_bid_price": pa.array([2.0, 99.0]),
                        "okx_ask_price": pa.array([2.1, 99.1]),
                    }
                ),
                root / _name(slots[1], slots[2]),
            )
            books = load_last_books(root, [("CAP", t_ms // 1000)])
            self.assertAlmostEqual(books[("CAP", t_ms // 1000)].bybit_bid, 2.0)

    def test_1hz_reads_next_part_when_tick_precedes_stamp(self) -> None:
        # Query 05:38:45. The tick 14 s earlier is in the part stamped 05:40.
        # The part stamped 04:40 holds only an older tick.
        query = datetime(2026, 9, 6, 5, 38, 45, tzinfo=timezone.utc)
        t_ms = int(query.timestamp() * 1000)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pq.write_table(
                pa.table(
                    {
                        "event_local_ts_ms": pa.array([t_ms - 73_000], type=pa.int64()),
                        "base_coin": pa.array(["ICX"], type=pa.string()),
                        "bybit_bid_price": pa.array([1.0]),
                        "bybit_ask_price": pa.array([1.1]),
                        "okx_bid_price": pa.array([1.0]),
                        "okx_ask_price": pa.array([1.1]),
                    }
                ),
                root / "part-20260906T044000Z.parquet",
            )
            pq.write_table(
                pa.table(
                    {
                        "event_local_ts_ms": pa.array([t_ms - 14_000], type=pa.int64()),
                        "base_coin": pa.array(["ICX"], type=pa.string()),
                        "bybit_bid_price": pa.array([2.0]),
                        "bybit_ask_price": pa.array([2.1]),
                        "okx_bid_price": pa.array([2.0]),
                        "okx_ask_price": pa.array([2.1]),
                    }
                ),
                root / "part-20260906T054000Z.parquet",
            )
            books = load_last_books_1hz(root, [("ICX", t_ms // 1000)])
            self.assertAlmostEqual(books[("ICX", t_ms // 1000)].bybit_bid, 2.0)


class TestNeighbors(unittest.TestCase):
    def test_one_step_neighbors_nine_labels_one_knob(self) -> None:
        labelled = one_step_neighbors(FROZEN)
        self.assertEqual(len(labelled), 9)
        labels = [label for label, _ in labelled]
        self.assertEqual(len(set(labels)), 9)
        self.assertEqual(labels[0], "frozen")
        self.assertEqual(labelled[0][1], FROZEN)
        frozen_fields = {
            "theta_open",
            "p50_open",
            "min_profit_pp",
            "min_theta_close",
            "min_spread_open",
            "fee_round_trip_pp",
        }
        for label, params in labelled[1:]:
            diffs = [
                name
                for name in frozen_fields
                if getattr(params, name) != getattr(FROZEN, name)
            ]
            self.assertEqual(
                len(diffs),
                1,
                msg=f"{label} should differ in exactly one knob, got {diffs}",
            )


class TestSpreadMatch(unittest.TestCase):
    def test_book_matches_recomputed_spread(self) -> None:
        book = _book(100.0, 101.0, 99.0, 99.5)
        long_pp = spread_pp(book, "long")
        short_pp = spread_pp(book, "short")
        self.assertTrue(book_matches_spread(book, "long", long_pp))
        self.assertTrue(book_matches_spread(book, "short", short_pp))
        self.assertFalse(book_matches_spread(book, "long", long_pp + 0.01))


class TestHourlyMarkEquity(unittest.TestCase):
    def test_open_leg_equity_moves_with_mark_and_matches_close_cash(self) -> None:
        # Open long at flat 100; mid mark moves; close book is the exit L1.
        open_b = _book(100.0, 100.1, 99.9, 100.0)
        mid_b = _book(104.0, 105.0, 106.0, 107.0)
        close_b = _book(109.9, 110.0, 110.0, 110.1)
        closed = run_ledger([_round("long", open_b, close_b)], "fixed100")
        self.assertEqual(closed.n_applied, 1)
        step = closed.steps[0]
        cash_b = float(step.cash_bybit_after_open or 0.0)
        cash_o = float(step.cash_okx_after_open or 0.0)
        notional = float(step.notional or 0.0)

        mid = mark_open_equity("long", open_b, mid_b, notional, cash_b, cash_o)
        at_close = mark_open_equity("long", open_b, close_b, notional, cash_b, cash_o)
        self.assertIsNotNone(mid)
        self.assertIsNotNone(at_close)
        assert mid is not None and at_close is not None
        self.assertNotAlmostEqual(mid[0], at_close[0])
        self.assertNotAlmostEqual(mid[1], at_close[1])

        # Mark excludes exit fees: cash_after_open + unrealized == after_close + close_fees.
        qty_b = notional / open_b.bybit_bid
        qty_o = notional / open_b.okx_ask
        close_fee_b = BYBIT_TAKER_FEE * qty_b * close_b.bybit_ask
        close_fee_o = OKX_TAKER_FEE * qty_o * close_b.okx_bid
        self.assertAlmostEqual(
            at_close[0],
            float(step.cash_bybit_after_close or 0.0) + close_fee_b,
            places=10,
        )
        self.assertAlmostEqual(
            at_close[1],
            float(step.cash_okx_after_close or 0.0) + close_fee_o,
            places=10,
        )

    def test_bucket_5m_grid_marks_end_of_bucket(self) -> None:
        first = int(datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc).timestamp())
        last = int(datetime(2026, 8, 10, 0, 10, tzinfo=timezone.utc).timestamp())
        starts = iter_bucket_5m_starts(first, last)
        self.assertEqual(starts, [first, first + 300, first + 600])
        self.assertEqual(bucket_5m_mark_ts(starts[0]), first + 299)
        self.assertEqual(bucket_5m_mark_ts(starts[1]), first + 599)
        self.assertEqual(align_bucket_5m_start(first + 299), first)
        self.assertEqual(align_bucket_5m_start(first + 300), first + 300)

    def test_hourly_series_marks_inside_open_and_gaps_missing_book(self) -> None:
        open_b = _book(100.0, 100.0, 100.0, 100.0)
        mid_b = _book(101.0, 102.0, 103.0, 104.0)
        close_b = _book(100.0, 100.0, 100.0, 100.0)
        # Hour 0 flat; hours 1–2 inside open; hour 3 after close.
        hour0 = 1_700_000_000
        hours = iter_hour_starts(hour0, hour0 + 3 * 3600)
        ts_open = hour0 + 3600  # after hour-0 mark (xx:59:59)
        ts_close = hour0 + 3 * 3600  # at start of hour 3 → hour-2 still open
        result = run_ledger(
            [_round("long", open_b, close_b, ts_open=ts_open, ts_close=ts_close)],
            "fixed100",
        )
        step = result.steps[0]
        books = {
            ("SOL", ts_open): open_b,
            ("SOL", hour_mark_ts(hours[1])): mid_b,
            # hours[2] deliberately missing → gap while still open
        }
        points = hourly_equity_series([step], books, hours)
        self.assertEqual(len(points), 4)
        self.assertEqual(points[0], (START_CASH, START_CASH))
        self.assertIsNotNone(points[1])
        self.assertIsNone(points[2])
        # After close: post-close cash (flat prices → fees only).
        self.assertIsNotNone(points[3])
        assert points[3] is not None
        self.assertAlmostEqual(points[3][0], result.cash_bybit, places=10)
        self.assertAlmostEqual(points[3][1], result.cash_okx, places=10)

    def test_open_at_end_stays_marked_not_force_closed(self) -> None:
        open_b = _book(100.0, 100.0, 100.0, 100.0)
        mark_b = _book(110.0, 111.0, 90.0, 91.0)
        leg = PricedOpen(
            coin="ICX",
            side="long",
            ts_open=1_000,
            open_book=open_b,
            mark_book=mark_b,
            mark_ts=2_000,
            prices_match=True,
            mark_prices_match=True,
        )
        result = run_ledger([], "fixed100", open_leg=leg)
        self.assertEqual(result.n_open_end, 1)
        step = result.steps[0]
        hour = 3600
        # Mark at hour-end while still open.
        books = {
            ("ICX", 1_000): open_b,
            ("ICX", hour_mark_ts(hour)): mark_b,
        }
        points = hourly_equity_series([step], books, [hour])
        self.assertEqual(len(points), 1)
        self.assertIsNotNone(points[0])
        # Not equal to start cash; marked with unrealized.
        assert points[0] is not None
        self.assertNotAlmostEqual(points[0][0], START_CASH)
        self.assertAlmostEqual(points[0][0], float(step.equity_bybit or 0.0), places=8)


class TestApproxVsCash(unittest.TestCase):
    def test_formula_is_open_plus_opposite_minus_entry_and_exit_fees(self) -> None:
        open_b = _book(100.0, 101.0, 98.0, 99.0)
        mark_b = _book(102.0, 103.0, 100.0, 101.0)
        pp = approx_potential_pp("long", open_b, mark_b)
        self.assertIsNotNone(pp)
        assert pp is not None
        self.assertAlmostEqual(
            pp,
            spread_pp(open_b, "long") + spread_pp(mark_b, "short") - 0.15 - 0.15,
        )
        self.assertAlmostEqual(approx_usd(100.0, pp), pp)
        self.assertAlmostEqual(approx_usd(50.0, pp), pp / 2.0)

    def test_series_uses_live_opposite_spread_then_closed_round(self) -> None:
        open_b = _book(100.0, 100.2, 99.0, 99.4)
        mid_b = _book(101.0, 101.5, 100.0, 100.4)
        close_b = _book(100.5, 100.8, 99.5, 99.9)
        first = 1_700_000_000 - (1_700_000_000 % 300)
        starts = iter_bucket_5m_starts(first, first + 600)
        ts_open = first + 300
        ts_close = first + 700
        result = run_ledger(
            [_round("long", open_b, close_b, ts_open=ts_open, ts_close=ts_close)],
            "fixed100",
        )
        mid_ts = bucket_5m_mark_ts(starts[1])
        books = {
            ("SOL", ts_open): open_b,
            ("SOL", mid_ts): mid_b,
            ("SOL", ts_close): close_b,
        }
        compared = approx_vs_cash_usd_5m(result.steps, books, starts)
        self.assertEqual(len(compared), 3)
        self.assertIsNotNone(compared[0])
        self.assertIsNotNone(compared[1])
        self.assertIsNotNone(compared[2])
        assert compared[0] is not None and compared[1] is not None and compared[2] is not None
        self.assertAlmostEqual(compared[0][0], 0.0)
        self.assertAlmostEqual(compared[0][1], 0.0)
        pp = approx_potential_pp("long", open_b, mid_b)
        assert pp is not None
        self.assertAlmostEqual(compared[1][0], approx_usd(100.0, pp))
        self.assertNotAlmostEqual(compared[1][0], compared[1][1])
        closed_pp = approx_potential_pp("long", open_b, close_b)
        assert closed_pp is not None
        self.assertAlmostEqual(compared[2][0], approx_usd(100.0, closed_pp))
        true_after = result.cash_bybit + result.cash_okx - 2.0 * START_CASH
        self.assertAlmostEqual(compared[2][1], true_after)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for app.bot.sizing — no network, no keys, no send."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import unittest

from app.bot.runtime import load_universe
from app.bot.sizing import (
    LIVE_SIZE_COINS,
    REF_MODE_WORSE_ASK,
    SizingError,
    ceil_to_lot,
    is_live_size_coin,
    lcm_lot,
    plan_dual_leg_qty,
)
from app.bot.stub_broker import InstrumentMeta


# Realistic marks for the $10–20 band check (not live quotes).
_PX = {
    "BTC": (Decimal("100000"), Decimal("100000")),
    "ETH": (Decimal("4000"), Decimal("4000")),
    "SOL": (Decimal("150"), Decimal("150")),
    "XRP": (Decimal("0.60"), Decimal("0.60")),
}

# Ample L1 so band tests isolate the shared-coin formula.
_DEEP = Decimal("1000")


def _universe() -> dict[str, InstrumentMeta]:
    return load_universe(Path(__file__).resolve().parents[1] / "bybit_okx_universe.csv")


def _book(px: Decimal, *, bid_sz: Decimal = _DEEP, ask_sz: Decimal = _DEEP) -> dict[str, str]:
    return {
        "bid_price": format(px, "f"),
        "ask_price": format(px, "f"),
        "bid_size": format(bid_sz, "f"),
        "ask_size": format(ask_sz, "f"),
    }


class LiveSizeContourTests(unittest.TestCase):
    def test_product_contour_is_sol_xrp_only(self) -> None:
        self.assertEqual(LIVE_SIZE_COINS, ("SOL", "XRP"))
        self.assertTrue(is_live_size_coin("sol"))
        self.assertTrue(is_live_size_coin("XRP"))
        self.assertFalse(is_live_size_coin("BTC"))
        self.assertFalse(is_live_size_coin("ETH"))


class CeilLotTests(unittest.TestCase):
    def test_ceils_up_not_floor(self) -> None:
        # Stub floor of 0.001 BTC at lot 0.01 is 0; ceil must be 0.01.
        self.assertEqual(ceil_to_lot(Decimal("0.001"), Decimal("0.01")), Decimal("0.01"))
        self.assertEqual(ceil_to_lot("0.0666", "0.1"), Decimal("0.1"))
        self.assertEqual(ceil_to_lot("16.667", "0.1"), Decimal("16.7"))
        self.assertEqual(ceil_to_lot("0.07", "0.01"), Decimal("0.07"))

    def test_rejects_non_positive_lot(self) -> None:
        with self.assertRaises(SizingError):
            ceil_to_lot(1, 0)


class LcmLotTests(unittest.TestCase):
    def test_sol_xrp_universe_lots(self) -> None:
        # SOL/XRP: OKX 0.01 coin, Bybit 0.1 coin → shared step 0.1 coin.
        self.assertEqual(lcm_lot("0.01", "0.1"), Decimal("0.1"))
        self.assertEqual(lcm_lot("0.1", "0.01"), Decimal("0.1"))

    def test_unequal_lots_snap_to_shared_multiple(self) -> None:
        self.assertEqual(lcm_lot("0.03", "0.05"), Decimal("0.15"))
        self.assertEqual(lcm_lot("0.01", "0.001"), Decimal("0.01"))

    def test_rejects_non_positive(self) -> None:
        with self.assertRaises(SizingError):
            lcm_lot(0, "0.1")


class UniverseBandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.uni = _universe()

    def _plan(self, coin: str, **kwargs: object) -> object:
        meta = self.uni[coin]
        okx_px, bybit_px = _PX[coin]
        return plan_dual_leg_qty(meta, okx_px, bybit_px, **kwargs)

    def test_sol_shared_coin_qty_not_independent_lots(self) -> None:
        plan = self._plan("SOL")
        self.assertTrue(plan.feasible, plan.reason)
        # Was 0.07 vs 0.1 when each venue sized from $10 alone.
        self.assertEqual(plan.coin_qty, Decimal("0.1"))
        self.assertEqual(plan.okx_qty, Decimal("0.1"))
        self.assertEqual(plan.bybit_qty, Decimal("0.1"))
        self.assertEqual(plan.qty_okx, plan.qty_bybit)
        self.assertEqual(plan.notional_okx, Decimal("15.0"))
        self.assertEqual(plan.notional_bybit, Decimal("15.0"))
        self.assertEqual(plan.ref_mode, REF_MODE_WORSE_ASK)
        self.assertEqual(plan.okx_ct_val, Decimal("1"))
        self.assertGreaterEqual(plan.notional_okx, Decimal("10"))
        self.assertLessEqual(plan.notional_okx, Decimal("20"))
        self.assertLess(plan.notional_okx, Decimal("100"))
        self.assertLess(plan.notional_bybit, Decimal("100"))

    def test_xrp_shared_coin_qty_not_independent_lots(self) -> None:
        plan = self._plan("XRP")
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.coin_qty, Decimal("16.7"))
        self.assertEqual(plan.okx_qty, Decimal("16.7"))
        self.assertEqual(plan.bybit_qty, Decimal("16.7"))
        self.assertEqual(plan.qty_okx, plan.qty_bybit)
        self.assertGreaterEqual(plan.notional_okx, Decimal("10"))
        self.assertLessEqual(plan.notional_okx, Decimal("20"))
        self.assertLess(plan.notional_okx, Decimal("100"))
        self.assertLess(plan.notional_bybit, Decimal("100"))

    def test_sol_about_106_same_coins(self) -> None:
        meta = self.uni["SOL"]
        plan = plan_dual_leg_qty(meta, Decimal("106"), Decimal("106"))
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.coin_qty, Decimal("0.1"))
        self.assertEqual(plan.okx_qty, plan.bybit_qty)
        self.assertEqual(plan.okx_qty, Decimal("0.1"))
        self.assertEqual(plan.notional_okx, Decimal("10.6"))
        self.assertEqual(plan.notional_bybit, Decimal("10.6"))

    def test_xrp_7_15_vs_7_2_regression_now_equal(self) -> None:
        """Live leftover: independent ceil produced OKX 7.15 vs Bybit 7.2."""
        meta = self.uni["XRP"]
        plan = plan_dual_leg_qty(meta, Decimal("1.40"), Decimal("1.40"))
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.coin_qty, Decimal("7.2"))
        self.assertEqual(plan.okx_qty, Decimal("7.2"))
        self.assertEqual(plan.bybit_qty, Decimal("7.2"))
        self.assertEqual(plan.qty_okx, plan.qty_bybit)
        self.assertEqual(plan.notional_okx, Decimal("10.08"))
        self.assertEqual(plan.notional_bybit, Decimal("10.08"))

    def test_btc_infeasible_if_passed(self) -> None:
        plan = self._plan("BTC")
        self.assertFalse(plan.feasible)
        self.assertIn(
            plan.reason,
            {
                "okx_notional_at_or_above_cap",
                "bybit_notional_at_or_above_cap",
                "okx_notional_above_band",
                "bybit_notional_above_band",
            },
        )
        # Shared LCM of 0.01 and 0.001 is 0.01 coin — blows $10–20 / <100.
        self.assertEqual(plan.coin_qty, Decimal("0.01"))
        self.assertEqual(plan.qty_okx, Decimal("0.01"))
        self.assertEqual(plan.qty_bybit, Decimal("0.01"))
        self.assertGreaterEqual(plan.notional_okx, Decimal("100"))

    def test_eth_infeasible_if_passed(self) -> None:
        plan = self._plan("ETH")
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.reason, "okx_notional_above_band")
        self.assertEqual(plan.coin_qty, Decimal("0.01"))
        self.assertEqual(plan.qty_okx, Decimal("0.01"))
        self.assertEqual(plan.qty_bybit, Decimal("0.01"))
        self.assertEqual(plan.notional_okx, Decimal("40"))
        self.assertEqual(plan.notional_bybit, Decimal("40"))

    def test_bybit_min_notional_abort(self) -> None:
        meta = InstrumentMeta(
            base_coin="SOL",
            okx_symbol="SOL-USDT-SWAP",
            bybit_symbol="SOLUSDT",
            okx_lot_size=0.01,
            okx_min_size=0.01,
            bybit_qty_step=0.1,
            bybit_min_order_qty=0.1,
            bybit_min_notional_value=50.0,
        )
        plan = plan_dual_leg_qty(meta, Decimal("150"), Decimal("150"))
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.reason, "bybit_notional_below_min")
        self.assertEqual(plan.coin_qty, Decimal("0.1"))

    def test_non_positive_price(self) -> None:
        plan = plan_dual_leg_qty(self.uni["SOL"], 0, 150)
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.reason, "non_positive_price")
        self.assertEqual(plan.coin_qty, Decimal("0"))


class DepthGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.uni = _universe()

    def test_deep_books_ok_for_sol(self) -> None:
        px = Decimal("106")
        plan = plan_dual_leg_qty(
            self.uni["SOL"],
            px,
            px,
            okx_book=_book(px),
            bybit_book=_book(px),
            okx_side="sell",
            bybit_side="buy",
            require_l1=True,
        )
        self.assertTrue(plan.feasible, plan.reason)
        self.assertTrue(plan.depth_ok)
        self.assertEqual(plan.coin_qty, Decimal("0.1"))
        self.assertEqual(plan.okx_exec_size, _DEEP)
        self.assertEqual(plan.bybit_exec_size, _DEEP)

    def test_thin_okx_bid_aborts_sell(self) -> None:
        px = Decimal("106")
        plan = plan_dual_leg_qty(
            self.uni["SOL"],
            px,
            px,
            okx_book=_book(px, bid_sz=Decimal("0.05")),
            bybit_book=_book(px),
            okx_side="sell",
            bybit_side="buy",
            require_l1=True,
        )
        self.assertFalse(plan.feasible)
        self.assertFalse(plan.depth_ok)
        self.assertEqual(plan.reason, "l1_depth_thin")
        self.assertEqual(plan.coin_qty, Decimal("0.1"))
        self.assertEqual(plan.okx_exec_size, Decimal("0.05"))

    def test_thin_bybit_ask_aborts_buy(self) -> None:
        px = Decimal("1.40")
        plan = plan_dual_leg_qty(
            self.uni["XRP"],
            px,
            px,
            okx_book=_book(px),
            bybit_book=_book(px, ask_sz=Decimal("7.0")),
            okx_side="sell",
            bybit_side="buy",
            require_l1=True,
        )
        self.assertFalse(plan.feasible)
        self.assertFalse(plan.depth_ok)
        self.assertEqual(plan.reason, "l1_depth_thin")
        self.assertEqual(plan.coin_qty, Decimal("7.2"))
        self.assertEqual(plan.bybit_exec_size, Decimal("7.0"))

    def test_missing_l1_fail_closed_when_required(self) -> None:
        plan = plan_dual_leg_qty(
            self.uni["SOL"],
            Decimal("106"),
            Decimal("106"),
            okx_side="sell",
            bybit_side="buy",
            require_l1=True,
        )
        self.assertFalse(plan.feasible)
        self.assertFalse(plan.depth_ok)
        self.assertEqual(plan.reason, "l1_depth_missing")

    def test_flatten_side_thin_aborts(self) -> None:
        px = Decimal("106")
        plan = plan_dual_leg_qty(
            self.uni["SOL"],
            px,
            px,
            okx_book=_book(px, ask_sz=Decimal("0.01")),
            bybit_book=_book(px),
            okx_side="sell",
            bybit_side="buy",
            flatten_okx_side="buy",
            flatten_bybit_side="sell",
            require_l1=True,
        )
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.reason, "l1_depth_thin")

    def test_exact_l1_size_equals_coin_qty_ok(self) -> None:
        px = Decimal("106")
        plan = plan_dual_leg_qty(
            self.uni["SOL"],
            px,
            px,
            okx_book=_book(px, bid_sz=Decimal("0.1"), ask_sz=Decimal("0.1")),
            bybit_book=_book(px, bid_sz=Decimal("0.1"), ask_sz=Decimal("0.1")),
            okx_side="sell",
            bybit_side="buy",
            require_l1=True,
        )
        self.assertTrue(plan.feasible, plan.reason)
        self.assertTrue(plan.depth_ok)

    def test_public_dict_carries_new_fields(self) -> None:
        px = Decimal("1.40")
        plan = plan_dual_leg_qty(
            self.uni["XRP"],
            px,
            px,
            okx_book=_book(px, bid_sz=Decimal("20"), ask_sz=Decimal("21")),
            bybit_book=_book(px, bid_sz=Decimal("22"), ask_sz=Decimal("23")),
            okx_side="sell",
            bybit_side="buy",
            require_l1=True,
        )
        pub = plan.as_public_dict()
        self.assertEqual(pub["coin_qty"], "7.2")
        self.assertEqual(pub["okx_qty"], "7.2")
        self.assertEqual(pub["bybit_qty"], "7.2")
        self.assertTrue(pub["depth_ok"])
        self.assertEqual(pub["okx_bid_size"], "20")
        self.assertEqual(pub["bybit_ask_size"], "23")


class OkxCtValTests(unittest.TestCase):
    def test_ct_val_not_one_converts_okx_qty_keeps_same_coins(self) -> None:
        # Hypothetical: 1 OKX contract = 0.1 coin; lots already in those units.
        meta = InstrumentMeta(
            base_coin="SOL",
            okx_symbol="SOL-USDT-SWAP",
            bybit_symbol="SOLUSDT",
            okx_lot_size=1,
            okx_min_size=1,
            bybit_qty_step=0.1,
            bybit_min_order_qty=0.1,
        )
        plan = plan_dual_leg_qty(
            meta,
            Decimal("10"),
            Decimal("10"),
            okx_ct_val=Decimal("0.1"),
        )
        # raw 10/10=1 coin → LCM(1*0.1, 0.1)=0.1 → ceil 1.0 coin.
        self.assertEqual(plan.coin_qty, Decimal("1.0"))
        self.assertEqual(plan.bybit_qty, Decimal("1.0"))
        self.assertEqual(plan.okx_qty, Decimal("10"))
        self.assertEqual(plan.okx_qty * plan.okx_ct_val, plan.coin_qty)
        self.assertEqual(plan.bybit_qty, plan.coin_qty)


if __name__ == "__main__":
    unittest.main()

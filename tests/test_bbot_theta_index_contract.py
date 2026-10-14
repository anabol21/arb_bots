"""Contract tests for shared theta snapshot lookups."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.bot import theta_trade_manager as manager
from app.bot.theta_screener import (
    LiveThetaScreener,
    ThetaSnapshot,
    theta_from_inputs,
)
from app.bot.floor_watcher import FloorSnapshot, LiveFloorObserver
from app.bot.tw_p50_watcher import TwP50Snapshot


def _snap(coin: str, side: str) -> ThetaSnapshot:
    return theta_from_inputs(
        base_coin=coin,
        side=side,
        ts_ms=1_000,
        p50_1m=-1.0,
        floor=0.0,
        computed_at_ms=1_001,
        coverage_1m=1.0,
        n_1m=10,
    )


def _quotes(coins: tuple[str, ...]) -> dict:
    return {
        coin: {
            "okx": {"bid_price": 100.0, "ask_price": 100.0},
            "bybit": {"bid_price": 100.0, "ask_price": 100.0},
        }
        for coin in coins
    }


class ThetaIndexContractTests(unittest.TestCase):
    def test_indexed_feature_build_matches_public_sequence_path(self) -> None:
        snapshots = [_snap("BTC", "long"), _snap("BTC", "short")]
        kwargs = {
            "coin": "BTC",
            "ts_s": 1,
            "quotes": _quotes(("BTC",)),
        }
        sequence_result = manager.build_feature_snapshot(
            snapshots=snapshots,
            **kwargs,
        )
        index_result = manager.build_feature_snapshot(
            snapshots=(),
            snapshot_index={(snap.base_coin, snap.side): snap for snap in snapshots},
            **kwargs,
        )
        self.assertEqual(sequence_result, index_result)

    def test_candidate_loop_reuses_one_snapshot_index(self) -> None:
        coins = ("BTC", "ETH", "WAL", "TRUST")
        snapshots = [_snap(coin, side) for coin in coins for side in ("long", "short")]
        seen_index_ids: list[int] = []
        original = manager.build_feature_snapshot

        def recording_builder(*args, **kwargs):
            seen_index_ids.append(id(kwargs["snapshot_index"]))
            return original(*args, **kwargs)

        with patch.object(
            manager, "build_feature_snapshot", side_effect=recording_builder
        ):
            decision = manager.decide_theta_k1(
                snapshots,
                slot=manager.SlotState(),
                thr=0.2,
                quotes=_quotes(coins),
                notional_usdt=100.0,
                coin_order=coins,
                ts_s=1,
            )

        self.assertEqual(decision.action, "skip")
        self.assertEqual(len(seen_index_ids), len(coins))
        self.assertEqual(len(set(seen_index_ids)), 1)

    def test_theta_from_tw_uses_one_atomic_floor_cut(self) -> None:
        class FloorCut:
            def __init__(self) -> None:
                self.calls = 0

            def snapshot_last_floors(self):
                self.calls += 1
                return {
                    ("BTC", "long"): FloorSnapshot("BTC", "long", 0.1, 10, 11),
                    ("ETH", "long"): FloorSnapshot("ETH", "long", 0.2, 10, 11),
                }

            def last_floor(self, *_args):
                raise AssertionError("per-row reads would allow a mixed floor cut")

        floors = FloorCut()
        screener = LiveThetaScreener(["BTC", "ETH"], floor_observer=floors)
        tw = [
            TwP50Snapshot("BTC", "long", 20, 0.5, 2, 1.0, 21),
            TwP50Snapshot("ETH", "long", 20, 0.6, 3, 1.0, 21),
        ]

        result = screener.compute_from_tw_snapshots(tw, computed_at_ms=22)

        self.assertEqual(floors.calls, 1)
        self.assertEqual(
            [(row.floor_tf_select_a25, row.theta_1m) for row in result],
            [(0.1, 0.4), (0.2, 0.39999999999999997)],
        )

    def test_floor_snapshot_cut_is_a_stable_copy(self) -> None:
        floors = LiveFloorObserver(["BTC"])
        floors.seed_last_floor("BTC", "long", 0.1, bar_end_ms=10, computed_at_ms=11)
        floors.seed_last_floor("BTC", "short", 0.2, bar_end_ms=10, computed_at_ms=11)
        first_cut = floors.snapshot_last_floors()

        floors._maybe_store_last_floors([
            {"base_coin": "BTC", "side": "long", "floor_tf_select_a25": 0.3, "bar_end_ms": 20},
            {"base_coin": "BTC", "side": "short", "floor_tf_select_a25": 0.4, "bar_end_ms": 20},
        ])
        second_cut = floors.snapshot_last_floors()

        self.assertEqual(first_cut[("BTC", "long")].floor_tf_select_a25, 0.1)
        self.assertEqual(first_cut[("BTC", "short")].floor_tf_select_a25, 0.2)
        self.assertEqual(second_cut[("BTC", "long")].floor_tf_select_a25, 0.3)
        self.assertEqual(second_cut[("BTC", "short")].floor_tf_select_a25, 0.4)


if __name__ == "__main__":
    unittest.main()

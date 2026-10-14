"""Contract tests for shared theta snapshot lookups."""

from __future__ import annotations

from unittest.mock import patch

from app.bot.theta_screener import ThetaSnapshot, theta_from_inputs
from app.bot import theta_trade_manager as manager


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


def test_indexed_feature_build_matches_public_sequence_path() -> None:
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
    assert sequence_result == index_result


def test_candidate_loop_reuses_one_snapshot_index() -> None:
    coins = ("BTC", "ETH", "WAL", "TRUST")
    snapshots = [_snap(coin, side) for coin in coins for side in ("long", "short")]
    seen_index_ids: list[int] = []
    original = manager.build_feature_snapshot

    def recording_builder(*args, **kwargs):
        seen_index_ids.append(id(kwargs["snapshot_index"]))
        return original(*args, **kwargs)

    with patch.object(manager, "build_feature_snapshot", side_effect=recording_builder):
        decision = manager.decide_theta_k1(
            snapshots,
            slot=manager.SlotState(),
            thr=0.2,
            quotes=_quotes(coins),
            notional_usdt=100.0,
            coin_order=coins,
            ts_s=1,
        )

    assert decision.action == "skip"
    assert len(seen_index_ids) == len(coins)
    assert len(set(seen_index_ids)) == 1

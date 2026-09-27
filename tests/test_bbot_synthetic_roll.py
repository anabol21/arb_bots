"""Synthetic roll policy, K=1 size gate, and local place journal chain.

No exchange sockets.
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.bot.paths import theta_step_chrono_jsonl_path, theta_trades_jsonl_path
from app.bot.private.place_send import PlaceSendResult, place_local
from app.bot.synthetic_policy import (
    SyntheticDecision,
    decide_synthetic_roll,
    make_synthetic_decide,
)
from app.bot.theta_trade_manager import ThetaTradeConfig, ThetaTradeManager


POOL = ("BTC", "ETH", "SOL")


def _find_seed(coins: tuple[str, ...], later: tuple[int, ...]) -> tuple[int, str, str]:
    sides = ("long", "short")
    for seed in range(1_000_000):
        rng = random.Random(seed)
        if rng.randint(0, 100) != 17:
            continue
        coin = str(rng.choice(list(coins)))
        side = str(rng.choice(sides))
        if any(rng.randint(0, 100) != roll for roll in later):
            continue
        return seed, coin, side
    raise AssertionError(f"no seed for {later}")


def _find_open_hold_close_seed(coins: tuple[str, ...]) -> tuple[int, str, str]:
    """Open (17), a hold while the slot is busy, then close (31).

    After the open the slot is not flat, so a later 17 does not draw a coin.
    The middle roll must not be 31, or the close happens one tick early.
    """
    sides = ("long", "short")
    for seed in range(100_000):
        rng = random.Random(seed)
        if rng.randint(0, 100) != 17:
            continue
        coin = str(rng.choice(list(coins)))
        side = str(rng.choice(sides))
        if rng.randint(0, 100) == 31:
            continue
        if rng.randint(0, 100) != 31:
            continue
        return seed, coin, side
    raise AssertionError("no seed for open, hold, close")


def _find_first_roll(roll: int) -> int:
    for seed in range(10_000):
        rng = random.Random(seed)
        if rng.randint(0, 100) == roll:
            return seed
    raise AssertionError(roll)


def _book(px: float = 2.0, sz: float = 1000.0) -> dict:
    return {
        "bid_price": px,
        "ask_price": px,
        "bid_size": sz,
        "ask_size": sz,
    }


def _meta(coin: str) -> SimpleNamespace:
    return SimpleNamespace(
        base_coin=coin,
        okx_symbol=f"{coin}-USDT-SWAP",
        bybit_symbol=f"{coin}USDT",
        okx_lot_size=1,
        okx_min_size=1,
        okx_ct_val=1,
        bybit_qty_step=1,
        bybit_min_order_qty=1,
        bybit_min_notional_value=5,
    )


def _quotes(sz: float = 1000.0, px: float = 2.0) -> dict:
    return {c: {"okx": _book(px, sz), "bybit": _book(px, sz)} for c in POOL}


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


class PolicyTests(unittest.TestCase):
    def test_17_opens_one_pool_coin_and_second_17_holds(self) -> None:
        seed, coin, side = _find_seed(POOL, (17,))
        rng = random.Random(seed)
        slot = SimpleNamespace(position=None, pending=False)
        first = decide_synthetic_roll(slot=slot, coins=POOL, rng=rng)
        self.assertEqual(first.action, "open")
        self.assertEqual(first.roll, 17)
        self.assertIn(first.coin, POOL)
        self.assertEqual(first.coin, coin)
        self.assertIn(first.side, ("long", "short"))
        self.assertEqual(first.side, side)
        slot.position = SimpleNamespace(base_coin=first.coin, side=first.side)
        second = decide_synthetic_roll(slot=slot, coins=POOL, rng=rng)
        self.assertEqual(second.action, "hold")
        self.assertEqual(second.roll, 17)

    def test_31_closes_held_coin_and_31_flat_holds(self) -> None:
        seed, coin, side = _find_seed(POOL, (31,))
        rng = random.Random(seed)
        slot = SimpleNamespace(position=None, pending=False)
        opened = decide_synthetic_roll(slot=slot, coins=POOL, rng=rng)
        slot.position = SimpleNamespace(base_coin=opened.coin, side=opened.side)
        closed = decide_synthetic_roll(slot=slot, coins=POOL, rng=rng)
        self.assertEqual(closed.action, "close")
        self.assertEqual(closed.coin, coin)
        self.assertEqual(closed.side, side)
        flat_seed = _find_first_roll(31)
        flat = decide_synthetic_roll(
            slot=SimpleNamespace(position=None, pending=False),
            coins=POOL,
            rng=random.Random(flat_seed),
        )
        self.assertEqual(flat.action, "hold")
        self.assertEqual(flat.roll, 31)

    def test_other_rolls_hold_including_ends(self) -> None:
        for roll in (0, 16, 18, 30, 32, 100):
            seed = _find_first_roll(roll)
            decision = decide_synthetic_roll(
                slot=SimpleNamespace(position=None, pending=False),
                coins=POOL,
                rng=random.Random(seed),
            )
            self.assertEqual(decision.action, "hold", roll)
            self.assertEqual(decision.roll, roll)


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "bbot"
        self.root.mkdir()
        self.calls: list[dict] = []

    def _manager(self, rng: random.Random, place=None) -> ThetaTradeManager:
        def _place(**kwargs):
            self.calls.append(kwargs)
            if place is not None:
                return place(**kwargs)
            return place_local(data_root=self.root, **kwargs)

        return ThetaTradeManager(
            data_root=self.root,
            config=ThetaTradeConfig(notional_usdt=10.0, fill_delay_ms=0),
            decide_fn=make_synthetic_decide(POOL, rng),
            place_fn=_place,
            meta_fn=_meta,
        )

    def test_second_17_does_not_send(self) -> None:
        seed, _coin, _side = _find_seed(POOL, (17,))
        mgr = self._manager(random.Random(seed))
        mgr.on_theta_snapshots([], quotes=_quotes(), coin_order=POOL, now_ms=1_700_000_000_000)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(mgr.slot.position)
        self.assertIn(mgr.slot.position.base_coin, POOL)
        mgr.on_theta_snapshots([], quotes=_quotes(), coin_order=POOL, now_ms=1_700_000_001_000)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(mgr.slot.position)

    def test_size_gate_blocks_open_and_close(self) -> None:
        seed, _coin, _side = _find_seed(POOL, (31,))
        mgr = self._manager(random.Random(seed))
        mgr.on_theta_snapshots(
            [], quotes=_quotes(sz=0.01), coin_order=POOL, now_ms=1_700_000_000_000
        )
        self.assertEqual(self.calls, [])
        self.assertIsNone(mgr.slot.position)
        # Open on a thick book, then a thin book must block the close.
        seed2, _c, _s = _find_seed(POOL, (31,))
        mgr2 = self._manager(random.Random(seed2))
        mgr2.on_theta_snapshots(
            [], quotes=_quotes(sz=1000), coin_order=POOL, now_ms=1_700_000_000_000
        )
        self.assertEqual(len(self.calls), 1)
        held = mgr2.slot.position
        self.assertIsNotNone(held)
        mgr2.on_theta_snapshots(
            [], quotes=_quotes(sz=0.01), coin_order=POOL, now_ms=1_700_000_001_000
        )
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(mgr2.slot.position)
        self.assertEqual(mgr2.slot.position.base_coin, held.base_coin)

    def test_pending_open_does_not_send_again(self) -> None:
        calls = {"n": 0}

        def _pending(**_kwargs):
            calls["n"] += 1
            return PlaceSendResult(completed=False, keep_pending=True)

        def _always_open(**_kwargs):
            return SyntheticDecision(action="open", coin="BTC", side="long", roll=17)

        mgr = ThetaTradeManager(
            data_root=self.root,
            config=ThetaTradeConfig(notional_usdt=10.0, fill_delay_ms=0),
            decide_fn=_always_open,
            place_fn=_pending,
            meta_fn=_meta,
        )
        mgr.on_theta_snapshots([], quotes=_quotes(), coin_order=POOL, now_ms=1_700_000_000_000)
        mgr.on_theta_snapshots([], quotes=_quotes(), coin_order=POOL, now_ms=1_700_000_001_000)
        self.assertEqual(calls["n"], 1)
        self.assertTrue(mgr.slot.pending)
        self.assertIsNone(mgr.slot.position)

    def test_process_local_chain_one_slot(self) -> None:
        seed, coin, side = _find_open_hold_close_seed(POOL)
        mgr = self._manager(random.Random(seed))
        ts0 = 1_700_000_000_000
        mgr.on_theta_snapshots([], quotes=_quotes(), coin_order=POOL, now_ms=ts0)
        self.assertIsNotNone(mgr.slot.position)
        self.assertEqual(mgr.slot.position.base_coin, coin)
        self.assertEqual(mgr.slot.position.side, side)
        self.assertFalse(mgr.slot.pending)
        mgr.on_theta_snapshots([], quotes=_quotes(), coin_order=POOL, now_ms=ts0 + 1000)
        self.assertIsNotNone(mgr.slot.position)
        self.assertEqual(len(self.calls), 1)
        mgr.on_theta_snapshots([], quotes=_quotes(), coin_order=POOL, now_ms=ts0 + 2000)
        self.assertIsNone(mgr.slot.position)
        self.assertFalse(mgr.slot.pending)
        self.assertEqual(len(self.calls), 2)

        day = "2023-11-14"
        trades = _rows(theta_trades_jsonl_path(self.root, day))
        statuses = [r["status"] for r in trades if r.get("status") in {"pending", "open", "closed"}]
        self.assertEqual(statuses, ["pending", "open", "pending", "closed"])
        coins = {r["base_coin"] for r in trades if r.get("base_coin")}
        self.assertEqual(coins, {coin})
        open_row = next(r for r in trades if r.get("status") == "open")
        closed_row = next(r for r in trades if r.get("status") == "closed")
        self.assertEqual(open_row["side"], side)
        self.assertIn("latency_ms", open_row)
        self.assertIn("latency_ms", closed_row)
        self.assertEqual(open_row["coin_qty"], "5")

        chrono = _rows(theta_step_chrono_jsonl_path(self.root, day))
        by_intent: dict[str, list[dict]] = {}
        for row in chrono:
            by_intent.setdefault(row["intent_id"], []).append(row)
        self.assertEqual(len(by_intent), 2)
        for rows in by_intent.values():
            _assert_duration_blocks(self, rows)
        text = str(self.root.resolve())
        for bad in ("/data/live", "/data/bars", "/data/compacted", "/data/spool"):
            self.assertFalse(text == bad or text.startswith(bad + "/"))
        with self.assertRaises(RuntimeError):
            theta_step_chrono_jsonl_path(Path("/data/live"), day)


def _assert_duration_blocks(test: unittest.TestCase, rows: list[dict]) -> None:
    needed = (
        "preprocess",
        "channel_check",
        "ws_send",
        "journal_pending",
        "wait_fill",
        "fill_done",
    )
    for block in needed:
        enters = [r for r in rows if r.get("block") == block and r.get("edge") == "enter"]
        exits = [r for r in rows if r.get("block") == block and r.get("edge") == "exit"]
        test.assertEqual(len(enters), 1, block)
        test.assertEqual(len(exits), 1, block)
        test.assertIn("wall_ms", enters[0])
        test.assertIn("mono_ns", enters[0])
        test.assertIn("wall_ms", exits[0])
        test.assertIn("mono_ns", exits[0])
        test.assertGreaterEqual(exits[0]["mono_ns"], enters[0]["mono_ns"])
        test.assertGreaterEqual(exits[0]["wall_ms"], enters[0]["wall_ms"])


class RuntimeWireTests(unittest.TestCase):
    @unittest.skipUnless(
        __import__("importlib").util.find_spec("websockets") is not None,
        "websockets not installed",
    )
    def test_profile_selects_synthetic_decide_not_gear22(self) -> None:
        from unittest.mock import patch

        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "synthetic_roll",
            "BBOT_BROKER": "stub",
            "BBOT_COINS": "BTC,ETH",
            "BBOT_DATA_ROOT": str(tmp / "bbot"),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_SYNTHETIC_SEED": "1",
            "VENUE": "testnet",
            "LIVE_ORDERS": "0",
        }
        with patch.dict("os.environ", env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
            self.assertEqual(rt.profile, "synthetic_roll")
            self.assertEqual(rt.coins, ["BTC", "ETH"])
            self.assertEqual(rt.notional, 10.0)
            self.assertIsNotNone(rt.theta_trade)
            self.assertIsNotNone(rt.theta_trade._decide_fn)  # noqa: SLF001
            self.assertFalse(rt.theta_trade.live_send)
            self.assertIs(rt.theta_trade._place_fn, rt._synthetic_local_place)  # noqa: SLF001

        env["BBOT_PROFILE"] = "gear22_would_send"
        with patch.dict("os.environ", env, clear=False):
            from app.bot.runtime import BotRuntime

            gear = BotRuntime()
            self.assertEqual(gear.profile, "gear22_would_send")
            self.assertIsNone(gear.theta_trade._decide_fn)  # noqa: SLF001
            self.assertFalse(gear.theta_trade.live_send)


if __name__ == "__main__":
    unittest.main()

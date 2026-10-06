from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.bot.synthetic_policy import decide_canary29_roll


POOL = tuple(f"C{i}" for i in range(29))


class FixedRng:
    def __init__(self, roll: int, side: str = "long") -> None:
        self.roll = roll
        self.side = side
        self.draws = 0

    def randint(self, low: int, high: int) -> int:
        self.draws += 1
        assert (low, high) == (1, 100)
        return self.roll

    def choice(self, choices: tuple[str, str]) -> str:
        assert choices == ("long", "short")
        return self.side


class Canary29PolicyTests(unittest.TestCase):
    def test_open_uses_utc_second_pool_index_and_random_side(self) -> None:
        rng = FixedRng(17, "short")
        decision = decide_canary29_roll(
            slot=SimpleNamespace(position=None, pending=False),
            coins=POOL,
            rng=rng,  # type: ignore[arg-type]
            ts_s=58,
        )
        self.assertEqual((decision.action, decision.coin, decision.side), ("open", "C0", "short"))
        self.assertEqual(rng.draws, 1)

    def test_rolls_once_while_pending_and_holds_without_reroll(self) -> None:
        rng = FixedRng(17)
        decision = decide_canary29_roll(
            slot=SimpleNamespace(position=None, pending=True),
            coins=POOL,
            rng=rng,  # type: ignore[arg-type]
            ts_s=58,
        )
        self.assertEqual(decision.action, "hold")
        self.assertEqual(rng.draws, 1)

    def test_close_uses_actual_held_position(self) -> None:
        decision = decide_canary29_roll(
            slot=SimpleNamespace(
                position=SimpleNamespace(base_coin="C22", side="short"),
                pending=False,
            ),
            coins=POOL,
            rng=FixedRng(31),  # type: ignore[arg-type]
            ts_s=1,
        )
        self.assertEqual((decision.action, decision.coin, decision.side), ("close", "C22", "short"))


if __name__ == "__main__":
    unittest.main()

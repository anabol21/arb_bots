"""Close/flatten must not be rejected by W6 open-side sample-notional caps.

Live Gear-2 can open a dual whose lot-ceiled size sits above W6_SAMPLE_MAX_NOTIONAL
(e.g. 0.2 SOL ≈ $19.7 with BBOT_NOTIONAL_USDT=10). Close of that held size must
flatten with profile qty and must not raise W6ProfileError «notional above sample
cap». Open path keeps the sample cap.
"""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


class W6CloseSampleCapTests(unittest.TestCase):
    def _live_env(self, td: str) -> dict:
        from app.bot.private.secrets import LIVE_KEY_NAMES

        live_env = Path(td) / "bbot-private-live.env"
        live_env.write_text(
            "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
            encoding="utf-8",
        )
        return {
            "VENUE": "live",
            "LIVE_ORDERS": "1",
            "BBOT_PRIVATE_W6": "1",
            "BBOT_PRIVATE_ENV_FILE": str(live_env),
            "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
        }

    def _meta(self):
        from app.bot.private.selftest import R3OrdersTests

        return R3OrdersTests()._meta(mark_max_age_ns=60_000_000_000)

    def _position(self):
        from app.bot.private.selftest import R3OrdersTests

        return R3OrdersTests()._position()

    def _held_legs_above_sample_cap(self) -> tuple[dict, dict]:
        """TRUMP qty whose mark notional exceeds W6_SAMPLE_MAX_NOTIONAL ($15).

        Bybit 10 * $1.70 = $17; OKX 100 * 0.1 * $1.70 = $17. Mirrors a
        lot-ceiled open (e.g. 0.2 SOL) that was allowed then must close.
        """
        from app.bot.private.ws_w6_dual_leg import resolve_w6_leg

        bybit = dict(resolve_w6_leg("bybit"))
        okx = dict(resolve_w6_leg("okx"))
        bybit["qty"] = "10.0"
        okx["qty"] = "100"
        return bybit, okx

    def test_assert_w6_notional_open_still_rejects_above_sample_cap(self) -> None:
        from app.bot.private.ws_w6_dual_leg import (
            W6ProfileError,
            assert_w6_notional,
        )

        meta = self._meta().get("bybit_live", "TRUMPUSDT")
        with self.assertRaises(W6ProfileError) as ctx:
            assert_w6_notional(meta, "10.0", min_usd=Decimal("5"))
        self.assertIn("sample cap", str(ctx.exception))

    def test_assert_w6_notional_close_skips_sample_cap(self) -> None:
        from app.bot.private.ws_w6_dual_leg import assert_w6_notional

        meta = self._meta().get("bybit_live", "TRUMPUSDT")
        notional = assert_w6_notional(
            meta, "10.0", min_usd=Decimal("5"), enforce_open_caps=False
        )
        self.assertGreater(notional, Decimal("15"))

    def test_open_path_plan_rejects_held_size_above_sample_cap(self) -> None:
        from app.bot.private.selftest import W2PrivateWsTests
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg

        bybit_leg, okx_leg = self._held_legs_above_sample_cap()
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            rep = run_w6_dual_leg(
                n=1,
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                baseline=FakeFlatBaseline(),
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                load_secrets=False,
                bybit_leg=bybit_leg,
                okx_leg=okx_leg,
                flatten_only=False,
            )
            self.assertEqual(rep.status, "plan_rejected")
            self.assertEqual(rep.orders_sent, 0)

    def test_flatten_only_closes_held_qty_above_sample_cap(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.selftest import W2PrivateWsTests, W6PrivateWsDualLegTests
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg

        bybit_leg, okx_leg = self._held_legs_above_sample_cap()
        helper = W6PrivateWsDualLegTests()
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            helper._push_hs(bpriv, btrade, okx=False)
            helper._push_hs(opriv, otrade, okx=True)

            placed_qtys: list[str] = []

            def inject(kind: str, plan) -> None:
                placed_qtys.append(str(plan.qty))
                okx = str(plan.venue).startswith("okx")
                symbol = "TRUMP-USDT-SWAP" if okx else "TRUMPUSDT"
                priv = opriv if okx else bpriv
                helper._inject_fill(priv, plan, okx=okx, symbol=symbol)

            rep = run_w6_dual_leg(
                n=1,
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                baseline=FakeFlatBaseline(),
                bybit_private_socket=bpriv,
                bybit_trade_socket=btrade,
                okx_private_socket=opriv,
                okx_trade_socket=otrade,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                fill_inject_fn=inject,
                terminal_wait_sec=2.0,
                bybit_leg=bybit_leg,
                okx_leg=okx_leg,
                flatten_only=True,
            )
            self.assertNotEqual(rep.status, "plan_rejected", rep.as_public_dict())
            self.assertEqual(rep.status, "ok", rep.as_public_dict())
            self.assertEqual(rep.orders_sent, 2)
            self.assertTrue(rep.flat_after)
            self.assertEqual(placed_qtys, ["10.0", "100"])


if __name__ == "__main__":
    unittest.main()

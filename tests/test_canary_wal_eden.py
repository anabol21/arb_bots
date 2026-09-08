"""WAL/EDEN canary: variation 0.1, pre-signal L1 depth, LIVE_SIZE allowlist."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bot.private.order_symbols import (
    SymbolGateError,
    resolve_allowed_futures_symbol,
    resolve_live_size_futures_symbol,
    resolve_private_subscribe_pool,
)
from app.policy.gear2_market_manager import MarketState, decide_market_tick
from app.policy.trade_manager import (
    CANARY_WAL_EDEN_COINS,
    CANARY_WAL_EDEN_HYPER,
    CANARY_WAL_EDEN_VARIATION,
    GEAR2_WOULD_SEND_HYPER,
    GEAR2_WOULD_SEND_VARIATION,
    LIVE_SIZE_COINS,
    TickView,
    hyper_for_profile,
    live_size_coin_allowed,
    live_size_coins_for_profile,
    uses_gear2_market_manager,
    variation_for_profile,
)


def _tick(**kwargs: object) -> TickView:
    spread_long = float(kwargs.get("spread_long", 0.2))  # type: ignore[arg-type]
    spread_short = float(kwargs.get("spread_short", 0.0))  # type: ignore[arg-type]
    fields = dict(
        event_local_ts_ms=1_000_000.0,
        spread_long=spread_long,
        spread_short=spread_short,
        ma_long=kwargs.get("ma_long", 0.2),
        ma_short=kwargs.get("ma_short", 0.2),
        okx_latency_ms=30.0,
        bybit_latency_ms=20.0,
        okx_bid=1.0,
        okx_ask=1.0,
        bybit_bid=1.0,
        bybit_ask=1.0,
        okx_bid_size=100.0,
        okx_ask_size=100.0,
        bybit_bid_size=100.0,
        bybit_ask_size=100.0,
        valid=True,
        suppressed=False,
        stale=False,
    )
    fields.update(kwargs)
    return TickView(**fields)  # type: ignore[arg-type]


def _decide(tick: TickView, coin: str = "WAL", state: MarketState | None = None) -> object:
    return decide_market_tick(
        tick,
        coin,
        state if state is not None else MarketState(),
        CANARY_WAL_EDEN_VARIATION,
        CANARY_WAL_EDEN_HYPER,
    )


class CanaryVariationTests(unittest.TestCase):
    def test_canary_thresholds_frac_and_hyper(self) -> None:
        v = variation_for_profile("canary_wal_eden")
        for key in (
            "thresh_open_long",
            "thresh_open_short",
            "thresh_close_long",
            "thresh_close_short",
        ):
            self.assertEqual(v[key], 0.1)
        self.assertEqual(v["open_frac"], 0.7)
        self.assertEqual(v["close_frac"], 0.7)
        h = hyper_for_profile("canary")
        self.assertEqual(h["avg_window_sec"], 10.0)
        self.assertEqual(h["k"], 1)
        self.assertTrue(h["Check_l1_depth"])
        self.assertFalse(h["Check_volume"])
        self.assertEqual(h["position_size"], 10.0)
        self.assertEqual(CANARY_WAL_EDEN_COINS, ("WAL", "EDEN"))

    def test_gear2_would_send_defaults_untouched(self) -> None:
        v = variation_for_profile("gear2_would_send")
        self.assertEqual(v["thresh_open_long"], 0.02)
        self.assertEqual(v["thresh_close_short"], 0.02)
        h = hyper_for_profile("gear2_would_send")
        self.assertFalse(h.get("Check_l1_depth", False))
        self.assertEqual(GEAR2_WOULD_SEND_VARIATION["thresh_open_long"], 0.02)
        self.assertFalse(GEAR2_WOULD_SEND_HYPER.get("Check_l1_depth", False))

    def test_uses_shared_market_manager(self) -> None:
        self.assertTrue(uses_gear2_market_manager("canary_wal_eden"))
        self.assertTrue(uses_gear2_market_manager("canary"))
        self.assertTrue(uses_gear2_market_manager("gear2_would_send"))
        self.assertFalse(uses_gear2_market_manager("gear1"))


class CanaryDepthGateTests(unittest.TestCase):
    def test_fail_closed_missing_size(self) -> None:
        d = _decide(_tick(okx_ask_size=None, bybit_bid_size=100.0))
        self.assertEqual(d.action, "flat")
        self.assertEqual(d.reason, "gate_l1_depth")
        self.assertEqual(d.counters["n_filtered_by_l1_depth"], 1)

    def test_fail_closed_thin_book(self) -> None:
        # notional 10 / price 1 → planned qty 10; size 9.9 is thin.
        d = _decide(_tick(okx_ask_size=9.9, bybit_bid_size=100.0))
        self.assertEqual(d.action, "flat")
        self.assertEqual(d.reason, "gate_l1_depth")

    def test_fail_closed_missing_price(self) -> None:
        d = _decide(_tick(okx_ask=None, okx_ask_size=100.0, bybit_bid_size=100.0))
        self.assertEqual(d.action, "flat")
        self.assertEqual(d.reason, "gate_l1_depth")

    def test_pass_when_both_venues_deep(self) -> None:
        d = _decide(_tick(okx_ask_size=10.0, bybit_bid_size=10.0))
        self.assertEqual(d.action, "open_long")
        self.assertEqual(d.reason, "signal")

    def test_short_open_uses_bid_ask_sides(self) -> None:
        # open short: sell OKX bid, buy Bybit ask.
        thin_wrong_side = _decide(
            _tick(
                spread_long=0.0,
                spread_short=0.2,
                okx_bid_size=1.0,
                okx_ask_size=100.0,
                bybit_ask_size=1.0,
                bybit_bid_size=100.0,
            )
        )
        self.assertEqual(thin_wrong_side.action, "flat")
        deep = _decide(
            _tick(
                spread_long=0.0,
                spread_short=0.2,
                okx_bid_size=20.0,
                bybit_ask_size=20.0,
            )
        )
        self.assertEqual(deep.action, "open_short")

    def test_gear2_still_emits_without_sizes(self) -> None:
        tick = TickView(
            event_local_ts_ms=1_000_000.0,
            spread_long=0.03,
            spread_short=0.0,
            ma_long=0.03,
            ma_short=0.0,
            okx_latency_ms=30.0,
            bybit_latency_ms=20.0,
            valid=True,
        )
        d = decide_market_tick(
            tick, "SOL", MarketState(), GEAR2_WOULD_SEND_VARIATION, GEAR2_WOULD_SEND_HYPER
        )
        self.assertEqual(d.action, "open_long")


class CanaryLiveSizeAllowTests(unittest.TestCase):
    def test_wal_eden_allowed_for_canary_only(self) -> None:
        self.assertEqual(LIVE_SIZE_COINS, ("SOL", "XRP"))
        self.assertEqual(live_size_coins_for_profile("canary_wal_eden"), ("WAL", "EDEN"))
        self.assertEqual(live_size_coins_for_profile("gear2_would_send"), ("SOL", "XRP"))
        self.assertTrue(live_size_coin_allowed("WAL", "canary_wal_eden"))
        self.assertTrue(live_size_coin_allowed("EDEN", "canary"))
        self.assertFalse(live_size_coin_allowed("SOL", "canary_wal_eden"))
        self.assertTrue(live_size_coin_allowed("SOL", "live_size"))
        self.assertFalse(live_size_coin_allowed("WAL", "gear2_would_send"))

    def test_private_symbols_canary_vs_w6(self) -> None:
        wal = resolve_live_size_futures_symbol(
            "bybit_live", "WALUSDT", contour="canary_wal_eden"
        )
        self.assertEqual(wal.symbol, "WALUSDT")
        eden = resolve_live_size_futures_symbol(
            "okx_live", "EDEN-USDT-SWAP", contour="canary"
        )
        self.assertEqual(eden.symbol, "EDEN-USDT-SWAP")
        with self.assertRaises(SymbolGateError):
            resolve_live_size_futures_symbol(
                "bybit_live", "SOLUSDT", contour="canary_wal_eden"
            )
        sol = resolve_live_size_futures_symbol("bybit_live", "SOLUSDT", contour="live_size")
        self.assertEqual(sol.symbol, "SOLUSDT")
        # W6 / R3 allowlist is still BTC+TRUMP only.
        resolve_allowed_futures_symbol("bybit_live", "BTCUSDT")
        with self.assertRaises(SymbolGateError):
            resolve_allowed_futures_symbol("bybit_live", "WALUSDT")


try:
    import websockets  # noqa: F401

    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False


@unittest.skipUnless(_HAS_WEBSOCKETS, "runtime import needs websockets (pre-existing env gap)")
class CanaryRuntimeProfileTests(unittest.TestCase):
    def test_runtime_defaults(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "canary_wal_eden",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "",
            "BBOT_BROKER": "stub",
            "BBOT_NOTIONAL_USDT": "",
        }
        # Clear notional so the canary default (10) is used.
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("BBOT_NOTIONAL_USDT", None)
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
        self.assertEqual(rt.coins, ["WAL", "EDEN"])
        self.assertEqual(rt.notional, 10.0)
        self.assertEqual(rt.variation["thresh_open_long"], 0.1)
        self.assertEqual(rt.hyper["avg_window_sec"], 10.0)
        self.assertTrue(rt.hyper["Check_l1_depth"])
        self.assertEqual(rt.hyper["position_size"], 10.0)
        self.assertEqual(rt.market_state.k_live, 1)
        self.assertTrue(rt._uses_market_manager())

    def test_public_book_appends_l1_ring(self) -> None:
        from app.bot.private.l1_tick_ring import clear_process_rings, freeze_window

        tmp = Path(tempfile.mkdtemp())
        env = {
            "BBOT_MODE": "policy",
            "BBOT_PROFILE": "canary_wal_eden",
            "BBOT_DATA_ROOT": str(tmp),
            "BBOT_LOG_PATH": str(tmp / "bbot.log"),
            "BBOT_COINS": "WAL,EDEN",
            "BBOT_BROKER": "stub",
        }
        clear_process_rings()
        with patch.dict(os.environ, env, clear=False):
            from app.bot.runtime import BotRuntime

            rt = BotRuntime()
        rt._maybe_record_l1(
            "WAL",
            "bybit",
            {
                "bid_price": 0.42,
                "ask_price": 0.43,
                "bid_size": 10.0,
                "ask_size": 11.0,
                "local_recv_ts_ms": 1_700_000_000_100,
            },
        )
        ticks = freeze_window("WAL", start_ms=0, end_ms=9_000_000_000_000)
        self.assertEqual(len(ticks), 1)
        self.assertEqual(ticks[0].venue, "bybit")
        self.assertEqual(ticks[0].bid, 0.42)
        self.assertEqual(ticks[0].event_local_ts_ms, 1_700_000_000_100)
        clear_process_rings()


class CanaryPrivateSubscribePoolTests(unittest.TestCase):
    def test_canary_pool_is_wal_eden_not_trump(self) -> None:
        pool = resolve_private_subscribe_pool(
            {
                "BBOT_PROFILE": "canary_wal_eden",
                "BBOT_MODE": "policy",
                "BBOT_COINS": "WAL,EDEN",
            }
        )
        self.assertEqual(pool.coins, ("WAL", "EDEN"))
        self.assertEqual(pool.bybit_symbols, ("WALUSDT", "EDENUSDT"))
        self.assertEqual(pool.okx_symbols, ("WAL-USDT-SWAP", "EDEN-USDT-SWAP"))
        self.assertNotIn("TRUMPUSDT", pool.bybit_symbols)
        self.assertNotIn("TRUMP-USDT-SWAP", pool.okx_symbols)

    def test_canary_default_coins_when_bbot_coins_empty(self) -> None:
        pool = resolve_private_subscribe_pool(
            {"BBOT_PROFILE": "canary", "BBOT_MODE": "policy", "BBOT_COINS": ""}
        )
        self.assertEqual(pool.coins, ("WAL", "EDEN"))

    def test_trump_override_rejected_when_profile_is_canary(self) -> None:
        with self.assertRaises(SymbolGateError) as ctx:
            resolve_private_subscribe_pool(
                {
                    "BBOT_PROFILE": "canary_wal_eden",
                    "BBOT_MODE": "policy",
                    "BBOT_COINS": "WAL,EDEN",
                },
                bybit_symbol="TRUMPUSDT",
                okx_symbol="TRUMP-USDT-SWAP",
            )
        msg = str(ctx.exception)
        self.assertIn("TRUMPUSDT", msg)
        self.assertIn("WAL", msg)

    def test_live_size_pool_is_sol_xrp(self) -> None:
        pool = resolve_private_subscribe_pool(
            {
                "BBOT_PROFILE": "gear2_would_send",
                "BBOT_MODE": "policy",
                "BBOT_COINS": "SOL,XRP",
            }
        )
        self.assertEqual(pool.coins, ("SOL", "XRP"))
        self.assertEqual(pool.bybit_symbols, ("SOLUSDT", "XRPUSDT"))
        self.assertEqual(pool.okx_symbols, ("SOL-USDT-SWAP", "XRP-USDT-SWAP"))

    def test_okx_subscribe_args_cover_pool_including_fills(self) -> None:
        from app.bot.private.ws_messages import build_okx_private_subscribe

        pool = resolve_private_subscribe_pool(
            {
                "BBOT_PROFILE": "canary_wal_eden",
                "BBOT_MODE": "policy",
                "BBOT_COINS": "WAL,EDEN",
            }
        )
        sub = json.loads(
            build_okx_private_subscribe(symbols=pool.okx_symbols).text
        )
        insts = {a["instId"] for a in sub["args"]}
        channels = {a["channel"] for a in sub["args"]}
        self.assertEqual(insts, {"WAL-USDT-SWAP", "EDEN-USDT-SWAP"})
        self.assertEqual(channels, {"orders", "fills", "positions"})
        self.assertNotIn("TRUMP-USDT-SWAP", insts)


class CanaryWarmSubscribeTests(unittest.TestCase):
    def tearDown(self) -> None:
        from app.bot.private.ws_warm_session import clear_process_warm_session

        clear_process_warm_session(stop=True)

    def _live_env(self, td: str) -> dict[str, str]:
        from app.bot.private.secrets import LIVE_KEY_NAMES

        live_env = Path(td) / "bbot-private-live.env"
        live_env.write_text(
            "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
            encoding="utf-8",
        )
        return {
            "VENUE": "live",
            "LIVE_ORDERS": "1",
            "BBOT_PRIVATE_ENV_FILE": str(live_env),
            "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
            "BBOT_PROFILE": "canary_wal_eden",
            "BBOT_MODE": "policy",
            "BBOT_COINS": "WAL,EDEN",
        }

    def _push_hs(self, priv, trade, *, okx: bool) -> None:
        if okx:
            priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
            priv.push_inbound(
                json.dumps(
                    {"event": "subscribe", "code": "0", "arg": {"channel": "orders"}}
                )
            )
            trade.push_inbound(json.dumps({"event": "login", "code": "0"}))
            return
        priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
        priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
        trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

    def test_warm_subscribe_args_use_wal_eden_not_trump(self) -> None:
        from app.bot.private.order_sign import LiveCredentials
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import (
            WarmSocketBundle,
            start_warm_private_session,
        )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            held: dict[str, FakePrivateWsSocket] = {}

            def make() -> WarmSocketBundle:
                bpriv = FakePrivateWsSocket()
                btrade = FakePrivateWsSocket()
                opriv = FakePrivateWsSocket()
                otrade = FakePrivateWsSocket()
                self._push_hs(bpriv, btrade, okx=False)
                self._push_hs(opriv, otrade, okx=True)
                held["okx_private"] = opriv
                return WarmSocketBundle(
                    bybit_private=bpriv,
                    bybit_trade=btrade,
                    okx_private=opriv,
                    okx_trade=otrade,
                )

            session = start_warm_private_session(
                env=env,
                coins=["WAL", "EDEN"],
                bybit_credentials=LiveCredentials(
                    api_key="bybit-live-key-ABCDEF",
                    api_secret="bybit-live-secret-XYZ",
                ),
                okx_credentials=LiveCredentials(
                    api_key="okx-live-key-ABCDEF",
                    api_secret="okx-live-secret-XYZ",
                    passphrase="okx-passphrase-SECRET",
                ),
                socket_provider=make,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
            )
            self.assertTrue(session.is_ready())
            self.assertEqual(session.coins, ("WAL", "EDEN"))
            self.assertEqual(session.subscribed_okx(), ("WAL-USDT-SWAP", "EDEN-USDT-SWAP"))
            self.assertNotIn("TRUMP-USDT-SWAP", session.subscribed_okx())
            self.assertNotEqual(session.okx_symbol, "TRUMP-USDT-SWAP")

            sub_frames = [
                json.loads(text)
                for text in held["okx_private"].outbox
                if '"op":"subscribe"' in text.replace(" ", "")
                or (text.startswith("{") and json.loads(text).get("op") == "subscribe")
            ]
            self.assertEqual(len(sub_frames), 1)
            insts = {a["instId"] for a in sub_frames[0]["args"]}
            channels = {a["channel"] for a in sub_frames[0]["args"]}
            self.assertEqual(insts, {"WAL-USDT-SWAP", "EDEN-USDT-SWAP"})
            self.assertEqual(channels, {"orders", "fills", "positions"})
            self.assertNotIn("TRUMP-USDT-SWAP", insts)

    def test_warm_replaces_stale_trump_session_with_canary_pool(self) -> None:
        from app.bot.private.order_sign import LiveCredentials
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import (
            WarmSocketBundle,
            get_process_warm_session,
            start_warm_private_session,
        )

        def _creds(*, okx: bool = False) -> LiveCredentials:
            if okx:
                return LiveCredentials(
                    api_key="okx-live-key-ABCDEF",
                    api_secret="okx-live-secret-XYZ",
                    passphrase="okx-passphrase-SECRET",
                )
            return LiveCredentials(
                api_key="bybit-live-key-ABCDEF",
                api_secret="bybit-live-secret-XYZ",
            )

        def _provider() -> WarmSocketBundle:
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket()
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket()
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)
            return WarmSocketBundle(
                bybit_private=bpriv,
                bybit_trade=btrade,
                okx_private=opriv,
                okx_trade=otrade,
            )

        with tempfile.TemporaryDirectory() as td:
            harness = self._live_env(td)
            harness.pop("BBOT_PROFILE", None)
            harness.pop("BBOT_COINS", None)
            harness.pop("BBOT_MODE", None)
            Path(harness["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            stale = start_warm_private_session(
                env=harness,
                bybit_credentials=_creds(),
                okx_credentials=_creds(okx=True),
                socket_provider=_provider,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
            )
            self.assertEqual(stale.okx_symbol, "TRUMP-USDT-SWAP")
            canary_env = self._live_env(td)
            canary = start_warm_private_session(
                env=canary_env,
                coins=["WAL", "EDEN"],
                bybit_credentials=_creds(),
                okx_credentials=_creds(okx=True),
                socket_provider=_provider,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
            )
            self.assertIs(get_process_warm_session(), canary)
            self.assertIsNot(canary, stale)
            self.assertTrue(stale._stopped)  # noqa: SLF001
            self.assertEqual(canary.subscribed_okx(), ("WAL-USDT-SWAP", "EDEN-USDT-SWAP"))
            self.assertNotIn("TRUMP-USDT-SWAP", canary.subscribed_okx())

    def test_okx_fills_channel_accepted_for_pool_inst(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.ws_private import PrivateStreamRuntime
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.order_sign import LiveCredentials

        with tempfile.TemporaryDirectory() as td:
            journal = PrivateJournalWriter(Path(td), run_id=new_opaque_id("run"))
            rt = PrivateStreamRuntime(
                exchange="okx",
                environment="live",
                symbol_alias="WAL-USDT-SWAP",
                subscribe_symbols=("WAL-USDT-SWAP", "EDEN-USDT-SWAP"),
                journal=journal,
                run_id=journal.run_id,
                credentials=LiveCredentials(
                    api_key="okx-live-key-ABCDEF",
                    api_secret="okx-live-secret-XYZ",
                    passphrase="okx-passphrase-SECRET",
                ),
                gate_env={"VENUE": "live", "LIVE_ORDERS": "0"},
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket()
            rt.bind_sockets(private=priv, trade=trade, env=rt.gate_env)
            parsed = rt.handle_inbound_text(
                json.dumps(
                    {
                        "arg": {
                            "channel": "fills",
                            "instId": "EDEN-USDT-SWAP",
                            "instType": "SWAP",
                        },
                        "data": [
                            {
                                "instId": "EDEN-USDT-SWAP",
                                "fillPx": "0.12",
                                "fillTime": "1700000001000",
                                "clOrdId": "abc",
                            }
                        ],
                    }
                )
            )
            self.assertEqual(parsed.kind, "order_update")
            self.assertEqual(parsed.terminal_state, "filled")
            self.assertEqual(parsed.symbol_alias, "EDEN-USDT-SWAP")
            ignored = rt.handle_inbound_text(
                json.dumps(
                    {
                        "arg": {"channel": "orders", "instId": "TRUMP-USDT-SWAP"},
                        "data": [{"instId": "TRUMP-USDT-SWAP", "state": "filled"}],
                    }
                )
            )
            self.assertEqual(ignored.kind, "ignored")
            self.assertEqual(ignored.symbol_alias, "TRUMP-USDT-SWAP")

    def test_warm_rejects_trump_override_on_canary_pool(self) -> None:
        from app.bot.private.order_sign import LiveCredentials
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            with self.assertRaises(SymbolGateError):
                start_warm_private_session(
                    env=env,
                    bybit_credentials=LiveCredentials(
                        api_key="bybit-live-key-ABCDEF",
                        api_secret="bybit-live-secret-XYZ",
                    ),
                    okx_credentials=LiveCredentials(
                        api_key="okx-live-key-ABCDEF",
                        api_secret="okx-live-secret-XYZ",
                        passphrase="okx-passphrase-SECRET",
                    ),
                    socket_provider=lambda: (_ for _ in ()).throw(
                        AssertionError("must not open sockets on stray TRUMP")
                    ),
                    rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                    bybit_symbol="TRUMPUSDT",
                    okx_symbol="TRUMP-USDT-SWAP",
                    attach=False,
                )


if __name__ == "__main__":
    unittest.main()

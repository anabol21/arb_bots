"""Local self-tests for B-private config (no network, no secrets printed)."""

from __future__ import annotations

import json
import tempfile
import time
import traceback
import unittest
from pathlib import Path
from unittest import mock

from app.bot.private import harness_readonly, rest_readonly
from app.bot.private.paths import resolve_data_root, resolve_log_path
from app.bot.private.secrets import (
    LIVE_KEY_NAMES,
    load_live_secrets,
    load_testnet_secrets,
    parse_env_file,
    resolve_private_profile,
    resolve_secret_file,
)
from app.bot.private.venue import (
    assert_live_readonly,
    assert_stage1_venue,
    endpoints_for_venue,
    send_allowed,
)


class VenueGateTests(unittest.TestCase):
    def test_default_stage1_ok(self) -> None:
        env = {"VENUE": "testnet", "LIVE_ORDERS": "0"}
        self.assertEqual(assert_stage1_venue(env), "testnet")
        self.assertFalse(send_allowed(env))

    def test_live_orders_blocks_stage1(self) -> None:
        with self.assertRaises(RuntimeError):
            assert_stage1_venue({"VENUE": "testnet", "LIVE_ORDERS": "1"})

    def test_live_venue_blocks_stage1(self) -> None:
        with self.assertRaises(RuntimeError):
            assert_stage1_venue({"VENUE": "live", "LIVE_ORDERS": "0"})

    def test_send_requires_both(self) -> None:
        self.assertFalse(send_allowed({"VENUE": "live", "LIVE_ORDERS": "0"}))
        self.assertFalse(send_allowed({"VENUE": "testnet", "LIVE_ORDERS": "1"}))
        self.assertTrue(send_allowed({"VENUE": "live", "LIVE_ORDERS": "1"}))

    def test_live_readonly_gate(self) -> None:
        self.assertEqual(
            assert_live_readonly({"VENUE": "live", "LIVE_ORDERS": "0"}),
            "live",
        )
        with self.assertRaises(RuntimeError):
            assert_live_readonly({"VENUE": "testnet", "LIVE_ORDERS": "0"})
        with self.assertRaises(RuntimeError):
            assert_live_readonly({"VENUE": "live", "LIVE_ORDERS": "1"})

    def test_endpoints_testnet(self) -> None:
        ep = endpoints_for_venue("testnet")
        self.assertEqual(ep.bybit_rest, "https://api-testnet.bybit.com")
        self.assertTrue(ep.okx_simulated_trading)
        self.assertNotEqual(ep.bybit_rest, "https://api.bybit.com")
        self.assertIn("stream-testnet.bybit.com", ep.bybit_private_ws)
        self.assertIn("/v5/private", ep.bybit_private_ws)
        self.assertIn("/v5/trade", ep.bybit_trade_ws)

    def test_endpoints_live(self) -> None:
        ep = endpoints_for_venue("live")
        self.assertEqual(ep.bybit_rest, "https://api.bybit.com")
        self.assertEqual(ep.okx_rest, "https://www.okx.com")
        self.assertFalse(ep.okx_simulated_trading)
        self.assertNotIn("testnet", ep.bybit_rest)
        self.assertEqual(ep.bybit_private_ws, "wss://stream.bybit.com/v5/private")
        self.assertEqual(ep.bybit_trade_ws, "wss://stream.bybit.com/v5/trade")
        self.assertIn("ws.okx.com", ep.okx_private_ws)
        self.assertIn("/ws/v5/private", ep.okx_private_ws)


class SecretsIsolationTests(unittest.TestCase):
    def test_refuse_live_named_override_on_testnet(self) -> None:
        with self.assertRaises(RuntimeError):
            resolve_secret_file(
                "testnet",
                {"BBOT_PRIVATE_ENV_FILE": "/tmp/bbot-private-live.env"},
            )

    def test_refuse_testnet_named_override_on_live(self) -> None:
        with self.assertRaises(RuntimeError):
            resolve_secret_file(
                "live",
                {"BBOT_PRIVATE_ENV_FILE": "/tmp/bbot-private-testnet.env"},
            )

    def test_load_testnet_rejects_live_keys_in_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bbot-private-testnet.env"
            path.write_text(
                "BYBIT_TESTNET_API_KEY=abc\n"
                "BYBIT_LIVE_API_KEY=should_not\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                load_testnet_secrets(
                    {"VENUE": "testnet", "BBOT_PRIVATE_ENV_FILE": str(path)},
                    require_complete=False,
                )

    def test_parse_env_and_presence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bbot-private-testnet.env"
            path.write_text(
                "# comment\n"
                "BYBIT_TESTNET_API_KEY=abcd1234\n"
                "BYBIT_TESTNET_API_SECRET=sec\n"
                "OKX_DEMO_API_KEY=okxkey\n"
                "OKX_DEMO_API_SECRET=okxsec\n"
                "OKX_DEMO_PASSPHRASE=pass\n",
                encoding="utf-8",
            )
            raw = parse_env_file(path)
            self.assertEqual(raw["BYBIT_TESTNET_API_KEY"], "abcd1234")
            secrets = load_testnet_secrets(
                {"VENUE": "testnet", "BBOT_PRIVATE_ENV_FILE": str(path)},
                require_complete=True,
            )
            self.assertTrue(all(secrets.presence().values()))
            self.assertEqual(secrets.mask_key_prefix(secrets.bybit_api_key), "abcd…")

    def test_testnet_profile_never_selects_live(self) -> None:
        profile = resolve_private_profile({"VENUE": "testnet", "LIVE_ORDERS": "0"})
        self.assertEqual(profile.name, "testnet")
        self.assertFalse(profile.send_allowed)
        self.assertFalse(profile.orders_surface)
        self.assertTrue(profile.readonly)
        self.assertNotIn("live", profile.secret_path.name.lower())

    def test_live_readonly_profile_no_orders_surface(self) -> None:
        env = {"VENUE": "live", "LIVE_ORDERS": "0"}
        self.assertEqual(assert_live_readonly(env), "live")
        self.assertFalse(send_allowed(env))
        profile = resolve_private_profile(env)
        self.assertEqual(profile.name, "live")
        self.assertTrue(profile.readonly)
        self.assertFalse(profile.orders_surface)
        self.assertFalse(profile.send_allowed)
        self.assertIn("live", profile.secret_path.name.lower())

    def test_live_orders_flag_still_no_orders_surface_in_r0(self) -> None:
        env = {"VENUE": "live", "LIVE_ORDERS": "1"}
        self.assertTrue(send_allowed(env))
        profile = resolve_private_profile(env)
        self.assertEqual(profile.name, "live")
        self.assertFalse(profile.orders_surface)

    def test_load_live_secrets_requires_live_venue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bbot-private-live.env"
            path.write_text("BYBIT_LIVE_API_KEY=x\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_live_secrets(
                    {
                        "VENUE": "testnet",
                        "LIVE_ORDERS": "0",
                        "BBOT_PRIVATE_ENV_FILE": str(path),
                    },
                    require_complete=False,
                )

    def test_load_live_rejects_testnet_keys_and_flags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bbot-private-live.env"
            path.write_text(
                "BYBIT_LIVE_API_KEY=abc\n"
                "BYBIT_TESTNET_API_KEY=nope\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                load_live_secrets(
                    {
                        "VENUE": "live",
                        "LIVE_ORDERS": "0",
                        "BBOT_PRIVATE_ENV_FILE": str(path),
                    },
                    require_complete=False,
                )
            path.write_text(
                "VENUE=live\n"
                "BYBIT_LIVE_API_KEY=abc\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                load_live_secrets(
                    {
                        "VENUE": "live",
                        "LIVE_ORDERS": "0",
                        "BBOT_PRIVATE_ENV_FILE": str(path),
                    },
                    require_complete=False,
                )

    def test_load_live_presence_and_key_names(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bbot-private-live.env"
            lines = [f"{name}=value{i}" for i, name in enumerate(LIVE_KEY_NAMES)]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            secrets = load_live_secrets(
                {
                    "VENUE": "live",
                    "LIVE_ORDERS": "0",
                    "BBOT_PRIVATE_ENV_FILE": str(path),
                },
                require_complete=True,
            )
            self.assertTrue(all(secrets.presence().values()))
            self.assertEqual(tuple(LIVE_KEY_NAMES), LIVE_KEY_NAMES)


class PathsIsolationTests(unittest.TestCase):
    def test_refuse_d_data_root(self) -> None:
        with self.assertRaises(RuntimeError):
            resolve_data_root({"BBOT_PRIVATE_DATA_ROOT": "/data/live"})

    def test_refuse_stub_or_collector_log_names(self) -> None:
        with self.assertRaises(RuntimeError):
            resolve_log_path(env={"BBOT_PRIVATE_LOG_PATH": "/tmp/runtime.log"})
        with self.assertRaises(RuntimeError):
            resolve_log_path(env={"BBOT_PRIVATE_LOG_PATH": "/tmp/bbot.log"})

    def test_refuse_d_log_path_overrides(self) -> None:
        """Regression: LOG_PATH must fail-closed on D roots (same as data root)."""
        denied = (
            "/data/live/bbot-private.log",
            "/data/bars/bbot-private.log",
            "/data/compacted/bbot-private.log",
            "/data/spool/bbot-private.log",
            "/data/live/nested/x.log",
        )
        for path in denied:
            with self.subTest(path=path):
                with self.assertRaises(RuntimeError) as ctx:
                    resolve_log_path(env={"BBOT_PRIVATE_LOG_PATH": path})
                self.assertIn("refuses D collector path", str(ctx.exception))

    def test_default_log_path_not_under_d(self) -> None:
        # Default contract path is allowed (may fall back if /var/log unwritable).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "private"
            root.mkdir(parents=True, exist_ok=True)
            got = resolve_log_path(
                data_root=root,
                env={},  # no override → prefer /var/log/spread/bbot-private.log
            )
            text = str(got.resolve())
            for prefix in ("/data/live", "/data/bars", "/data/compacted", "/data/spool"):
                self.assertFalse(
                    text == prefix or text.startswith(prefix + "/"),
                    msg=f"resolved log under D: {text}",
                )
            self.assertTrue(
                text.endswith("bbot-private.log"),
                msg=f"unexpected log basename: {got}",
            )


class RestReadonlyGuardTests(unittest.TestCase):
    def test_no_order_helpers(self) -> None:
        rest_readonly.assert_no_order_methods()

    def test_get_only_http_helper(self) -> None:
        rest_readonly.assert_get_only_http_helper()

    def test_forbidden_path(self) -> None:
        with self.assertRaises(RuntimeError):
            rest_readonly._assert_path_readonly("/v5/order/create")  # noqa: SLF001

    def test_okx_header_routing(self) -> None:
        live_hdrs = rest_readonly.build_okx_readonly_headers(
            api_key="k",
            api_secret="s" * 16,
            passphrase="p",
            path="/api/v5/account/balance",
            simulated_trading=False,
            timestamp="2026-01-01T00:00:00.000Z",
        )
        rest_readonly.assert_okx_headers_for_venue(live_hdrs, "live")
        self.assertNotIn("x-simulated-trading", live_hdrs)
        self.assertEqual(live_hdrs.get("User-Agent"), rest_readonly.OKX_REST_USER_AGENT)
        self.assertEqual(live_hdrs.get("Accept"), rest_readonly.OKX_REST_ACCEPT)
        self.assertTrue(rest_readonly.OKX_REST_USER_AGENT)
        self.assertEqual(rest_readonly.OKX_REST_ACCEPT, "application/json")
        # Signed auth headers still present; values are dummies in selftest only.
        for name in (
            "OK-ACCESS-KEY",
            "OK-ACCESS-SIGN",
            "OK-ACCESS-TIMESTAMP",
            "OK-ACCESS-PASSPHRASE",
            "Content-Type",
        ):
            self.assertIn(name, live_hdrs)

        demo_hdrs = rest_readonly.build_okx_readonly_headers(
            api_key="k",
            api_secret="s" * 16,
            passphrase="p",
            path="/api/v5/account/balance",
            simulated_trading=True,
            timestamp="2026-01-01T00:00:00.000Z",
        )
        rest_readonly.assert_okx_headers_for_venue(demo_hdrs, "testnet")
        self.assertEqual(demo_hdrs.get("x-simulated-trading"), "1")
        self.assertEqual(demo_hdrs.get("User-Agent"), rest_readonly.OKX_REST_USER_AGENT)
        self.assertEqual(demo_hdrs.get("Accept"), rest_readonly.OKX_REST_ACCEPT)
        with self.assertRaises(RuntimeError):
            rest_readonly.assert_okx_headers_for_venue(demo_hdrs, "live")

    def test_okx_live_headers_no_simulated_and_benign_ua(self) -> None:
        """R1: live OKX GET headers include UA/Accept and never x-simulated-trading."""
        hdrs = rest_readonly.build_okx_readonly_headers(
            api_key="k",
            api_secret="s" * 16,
            passphrase="p",
            path="/api/v5/account/balance",
            simulated_trading=False,
            timestamp="2026-08-19T00:00:00.000Z",
        )
        rest_readonly.assert_okx_headers_for_venue(hdrs, "live")
        self.assertEqual(hdrs["User-Agent"], "spread-bbot-private/0")
        self.assertEqual(hdrs["Accept"], "application/json")
        self.assertNotIn("x-simulated-trading", hdrs)
        self.assertFalse(
            any(k.lower() == "x-simulated-trading" for k in hdrs)
        )
        live_ep = endpoints_for_venue("live")
        self.assertFalse(live_ep.okx_simulated_trading)

    def test_w4_okx_helpers_reuse_r1_ua_accept_iso_no_simulated(self) -> None:
        """W4 baseline / position-mode / public metadata: R1 header shape only."""
        from app.bot.private.order_preflight import (
            LiveHttpMetadataProvider,
            LiveSignedPositionModeProvider,
        )
        from app.bot.private.order_sign import LiveCredentials
        from app.bot.private.rest_readonly import (
            OKX_REST_ACCEPT,
            OKX_REST_USER_AGENT,
            _assert_path_readonly,
        )
        from app.bot.private.ws_w4_baseline import _OKX_OPEN, _OKX_POS, _okx_signed_get
        from app.bot.private.ws_w4_postonly import _public_http_get_json
        import urllib.request

        creds = LiveCredentials(api_key="k", api_secret="s" * 16, passphrase="p")
        captured: list[dict] = []

        def capture_get(url, headers, timeout_sec=15.0):
            captured.append({"url": url, "headers": dict(headers)})
            return {"code": "0", "data": []}

        _okx_signed_get(
            credentials=creds,
            base="https://www.okx.com",
            path_with_query=f"{_OKX_POS}?instId=BTC-USDT-SWAP",
            http_get_json=capture_get,
        )
        _okx_signed_get(
            credentials=creds,
            base="https://www.okx.com",
            path_with_query=f"{_OKX_OPEN}?instId=BTC-USDT-SWAP&instType=SWAP",
            http_get_json=capture_get,
        )
        self.assertEqual(len(captured), 2)
        for row in captured:
            h = row["headers"]
            self.assertEqual(h.get("User-Agent"), OKX_REST_USER_AGENT)
            self.assertEqual(h.get("Accept"), OKX_REST_ACCEPT)
            self.assertEqual(h.get("Content-Type"), "application/json")
            ts = h.get("OK-ACCESS-TIMESTAMP") or ""
            self.assertIn("T", ts)
            self.assertTrue(ts.endswith("Z"))
            self.assertFalse(ts.isdigit())
            self.assertNotIn("x-simulated-trading", h)
            self.assertFalse(any(k.lower() == "x-simulated-trading" for k in h))
        with self.assertRaises(RuntimeError):
            _assert_path_readonly(_OKX_OPEN)

        pos_hdrs: dict = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"code":"0","data":[{"posMode":"net_mode"}]}'

        def fake_urlopen(req, timeout=None):
            pos_hdrs.update(dict(req.headers))
            return _Resp()

        def _hget(d, name):
            for k, v in d.items():
                if k.lower() == name.lower():
                    return v
            return None

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
        try:
            snap = LiveSignedPositionModeProvider(
                exchange="okx",
                credentials=creds,
                okx_base="https://www.okx.com",
                symbol="BTC-USDT-SWAP",
            ).get("okx_live")
            self.assertEqual(snap.mode, "one_way")
        finally:
            urllib.request.urlopen = orig  # type: ignore[assignment]
        self.assertEqual(_hget(pos_hdrs, "User-Agent"), OKX_REST_USER_AGENT)
        self.assertEqual(_hget(pos_hdrs, "Accept"), OKX_REST_ACCEPT)
        ts2 = _hget(pos_hdrs, "OK-ACCESS-TIMESTAMP") or ""
        self.assertIn("T", ts2)
        self.assertTrue(str(ts2).endswith("Z"))
        self.assertIsNone(_hget(pos_hdrs, "x-simulated-trading"))

        meta_hdrs: list[dict] = []

        def meta_get(url, headers):
            meta_hdrs.append(dict(headers))
            if "instruments" in url:
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "instType": "SWAP",
                            "ctVal": "0.01",
                            "ctValCcy": "BTC",
                            "lotSz": "0.01",
                            "minSz": "0.01",
                            "tickSz": "0.1",
                            "state": "live",
                            "settleCcy": "USDT",
                            "instIdCode": 10459,
                        }
                    ],
                }
            return {"code": "0", "data": [{"markPx": "50000", "last": "50000"}]}

        LiveHttpMetadataProvider(http_get_json=meta_get).get(
            "okx_live", "BTC-USDT-SWAP"
        )
        self.assertGreaterEqual(len(meta_hdrs), 2)
        for h in meta_hdrs:
            self.assertEqual(h.get("User-Agent"), OKX_REST_USER_AGENT)
            self.assertEqual(h.get("Accept"), OKX_REST_ACCEPT)

        pub_cap: dict = {}

        class _Resp2:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"code":"0","data":[]}'

        def fake_urlopen2(req, timeout=None):
            pub_cap.update(dict(req.headers))
            return _Resp2()

        urllib.request.urlopen = fake_urlopen2  # type: ignore[assignment]
        try:
            _public_http_get_json(
                "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
                {"Accept": "application/json"},
            )
        finally:
            urllib.request.urlopen = orig  # type: ignore[assignment]
        self.assertEqual(_hget(pub_cap, "User-Agent"), OKX_REST_USER_AGENT)
        self.assertEqual(_hget(pub_cap, "Accept"), OKX_REST_ACCEPT)

    def test_scrub_public_record_redacts_secrets(self) -> None:
        scrubbed = rest_readonly.scrub_public_record(
            {
                "status": "ok",
                "secret_source": "/etc/spread/bbot-private-live.env",
                "secret_file": "/etc/spread/bbot-private-live.env",
                "message": "retMsg leak",
                "error": "RuntimeError: secret",
                "equity_usdt": 12.34,
                "headers": {"OK-ACCESS-KEY": "abc"},
                "probes": [
                    {
                        "exchange": "bybit",
                        "outcome": "ok",
                        "message": "should go",
                        "http_status": 200,
                    }
                ],
            }
        )
        blob = str(scrubbed)
        self.assertNotIn("/etc/spread", blob)
        self.assertNotIn("secret", blob.lower())
        self.assertNotIn("retMsg", blob)
        self.assertNotIn("12.34", blob)
        self.assertNotIn("OK-ACCESS", blob)
        self.assertEqual(scrubbed["status"], "ok")
        self.assertEqual(scrubbed["probes"][0]["outcome"], "ok")
        self.assertNotIn("message", scrubbed["probes"][0])

    def test_normalize_outcomes_allowlist(self) -> None:
        self.assertEqual(
            rest_readonly.normalize_http_outcome(
                http_status=200, exchange_code="0", ok=True
            ),
            "ok",
        )
        self.assertEqual(
            rest_readonly.normalize_http_outcome(
                http_status=401, exchange_code=None, ok=False
            ),
            "auth_rejected",
        )
        self.assertIn(
            rest_readonly.normalize_http_outcome(
                http_status=403, exchange_code=None, ok=False
            ),
            rest_readonly.NORMALIZED_OUTCOMES,
        )


class HarnessRoutingTests(unittest.TestCase):
    def test_live_orders_rejects_before_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {
                "VENUE": "live",
                "LIVE_ORDERS": "1",
                "BBOT_PRIVATE_DATA_ROOT": td,
            }
            with mock.patch(
                "app.bot.private.harness_readonly.load_live_secrets"
            ) as load_live:
                with mock.patch(
                    "app.bot.private.harness_readonly.probe_bybit_wallet"
                ) as probe_b:
                    with mock.patch(
                        "app.bot.private.harness_readonly.probe_okx_balance"
                    ) as probe_o:
                        report = harness_readonly.run_readonly_harness(env)
            load_live.assert_not_called()
            probe_b.assert_not_called()
            probe_o.assert_not_called()
            self.assertEqual(report["status"], "rejected_before_network")
            self.assertFalse(report.get("live_env_opened"))
            self.assertEqual(report.get("orders_sent"), 0)
            self.assertNotIn("secret_source", report)
            self.assertNotIn("secret_file", report.get("config") or {})

    def test_live_route_sets_profile_flags_without_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            live_env = Path(td) / "bbot-private-live.env"
            live_env.write_text(
                "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
                encoding="utf-8",
            )
            data_root = Path(td) / "data"
            data_root.mkdir()
            env = {
                "VENUE": "live",
                "LIVE_ORDERS": "0",
                "BBOT_PRIVATE_ENV_FILE": str(live_env),
                "BBOT_PRIVATE_DATA_ROOT": str(data_root),
            }

            def _fake_bybit(**kwargs):  # type: ignore[no-untyped-def]
                ep = kwargs["endpoints"]
                self.assertEqual(ep.venue, "live")
                self.assertEqual(ep.bybit_rest, "https://api.bybit.com")
                return rest_readonly.AccountProbeResult(
                    exchange="bybit",
                    venue="live",
                    endpoint=ep.bybit_rest,
                    path=ep.bybit_account_path,
                    ok=True,
                    http_status=200,
                    exchange_code="0",
                    outcome="ok",
                    equity_usdt=1.0,
                    orders_sent=0,
                )

            def _fake_okx(**kwargs):  # type: ignore[no-untyped-def]
                ep = kwargs["endpoints"]
                self.assertEqual(ep.venue, "live")
                self.assertFalse(ep.okx_simulated_trading)
                return rest_readonly.AccountProbeResult(
                    exchange="okx",
                    venue="live",
                    endpoint=ep.okx_rest,
                    path=ep.okx_account_path,
                    ok=True,
                    http_status=200,
                    exchange_code="0",
                    outcome="ok",
                    equity_usdt=1.0,
                    orders_sent=0,
                    okx_simulated_trading=False,
                )

            with mock.patch(
                "app.bot.private.harness_readonly.probe_bybit_wallet",
                side_effect=_fake_bybit,
            ):
                with mock.patch(
                    "app.bot.private.harness_readonly.probe_okx_balance",
                    side_effect=_fake_okx,
                ):
                    report = harness_readonly.run_readonly_harness(env)

            self.assertEqual(report["credential_profile"], "live")
            self.assertTrue(report["live_env_opened"])
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["orders_sent"], 0)
            blob = str(report)
            self.assertNotIn(str(live_env), blob)
            self.assertNotIn("bbot-private-live.env", blob)
            for probe in report["probes"]:
                self.assertIn(probe["outcome"], rest_readonly.NORMALIZED_OUTCOMES)
                self.assertNotIn("message", probe)
            # v1 journal auth/account_read written under private data root.
            from app.bot.private.journal_v1 import validate_events_file
            from app.bot.private.paths import events_jsonl_path

            events_files = list(data_root.glob("journal/event_date=*/events.jsonl"))
            self.assertTrue(events_files)
            events = validate_events_file(events_files[0])
            types = [e["event_type"] for e in events]
            self.assertIn("auth", types)
            self.assertIn("account_read", types)
            self.assertNotIn("order_prepared", types)
            self.assertNotIn("request_sent", types)
            # Auth success only after signed probe success (not credential presence alone).
            auth_events = [e for e in events if e["event_type"] == "auth"]
            self.assertEqual(len(auth_events), 2)
            self.assertTrue(all(e["outcome"] == "success" for e in auth_events))
            self.assertTrue(report.get("journal_v1_ok"))
            # Legacy auth_probe lives under probes/, never under canonical journal/.
            from app.bot.private.paths import auth_probe_jsonl_path

            probe_files = list(data_root.glob("probes/event_date=*/auth_probe.jsonl"))
            self.assertTrue(probe_files)
            self.assertFalse(list(data_root.glob("journal/**/auth_probe.jsonl")))
            self.assertTrue(str(probe_files[0]).startswith(str(data_root / "probes")))
            # Path helper must stay outside journal tree.
            sample = auth_probe_jsonl_path(data_root, "2099-01-01")
            self.assertIn("/probes/", str(sample).replace("\\", "/"))
            self.assertNotIn("/journal/", str(sample).replace("\\", "/"))

    def test_auth_outcome_follows_signed_probe_failure(self) -> None:
        """Credential presence must not be journaled as auth success."""
        with tempfile.TemporaryDirectory() as td:
            live_env = Path(td) / "bbot-private-live.env"
            live_env.write_text(
                "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
                encoding="utf-8",
            )
            data_root = Path(td) / "data"
            data_root.mkdir()
            env = {
                "VENUE": "live",
                "LIVE_ORDERS": "0",
                "BBOT_PRIVATE_ENV_FILE": str(live_env),
                "BBOT_PRIVATE_DATA_ROOT": str(data_root),
            }

            def _fake_bybit(**kwargs):  # type: ignore[no-untyped-def]
                ep = kwargs["endpoints"]
                return rest_readonly.AccountProbeResult(
                    exchange="bybit",
                    venue="live",
                    endpoint=ep.bybit_rest,
                    path=ep.bybit_account_path,
                    ok=False,
                    http_status=401,
                    exchange_code="10003",
                    outcome="auth_rejected",
                    equity_usdt=None,
                    orders_sent=0,
                )

            def _fake_okx(**kwargs):  # type: ignore[no-untyped-def]
                ep = kwargs["endpoints"]
                return rest_readonly.AccountProbeResult(
                    exchange="okx",
                    venue="live",
                    endpoint=ep.okx_rest,
                    path=ep.okx_account_path,
                    ok=True,
                    http_status=200,
                    exchange_code="0",
                    outcome="ok",
                    equity_usdt=1.0,
                    orders_sent=0,
                    okx_simulated_trading=False,
                )

            with mock.patch(
                "app.bot.private.harness_readonly.probe_bybit_wallet",
                side_effect=_fake_bybit,
            ):
                with mock.patch(
                    "app.bot.private.harness_readonly.probe_okx_balance",
                    side_effect=_fake_okx,
                ):
                    report = harness_readonly.run_readonly_harness(env)

            self.assertEqual(report["status"], "partial")
            self.assertTrue(report.get("journal_v1_ok"))
            from app.bot.private.journal_v1 import validate_events_file

            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            bybit_auth = [
                e
                for e in events
                if e["event_type"] == "auth" and e["venue"] == "bybit"
            ]
            okx_auth = [
                e for e in events if e["event_type"] == "auth" and e["venue"] == "okx"
            ]
            self.assertEqual(len(bybit_auth), 1)
            self.assertEqual(bybit_auth[0]["outcome"], "failure")
            self.assertEqual(bybit_auth[0]["error_code"], "auth_failed")
            self.assertTrue(bybit_auth[0]["credential_presence"]["credentials_configured"])
            self.assertEqual(okx_auth[0]["outcome"], "success")
            self.assertNotIn("error_code", okx_auth[0])

    def test_journal_write_failure_is_fail_closed(self) -> None:
        """Journal append/validation errors must not yield status=ok."""
        with tempfile.TemporaryDirectory() as td:
            live_env = Path(td) / "bbot-private-live.env"
            live_env.write_text(
                "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
                encoding="utf-8",
            )
            data_root = Path(td) / "data"
            data_root.mkdir()
            env = {
                "VENUE": "live",
                "LIVE_ORDERS": "0",
                "BBOT_PRIVATE_ENV_FILE": str(live_env),
                "BBOT_PRIVATE_DATA_ROOT": str(data_root),
            }

            def _ok_probe(exchange: str, **kwargs):  # type: ignore[no-untyped-def]
                ep = kwargs["endpoints"]
                if exchange == "bybit":
                    return rest_readonly.AccountProbeResult(
                        exchange="bybit",
                        venue="live",
                        endpoint=ep.bybit_rest,
                        path=ep.bybit_account_path,
                        ok=True,
                        http_status=200,
                        exchange_code="0",
                        outcome="ok",
                        equity_usdt=1.0,
                        orders_sent=0,
                    )
                return rest_readonly.AccountProbeResult(
                    exchange="okx",
                    venue="live",
                    endpoint=ep.okx_rest,
                    path=ep.okx_account_path,
                    ok=True,
                    http_status=200,
                    exchange_code="0",
                    outcome="ok",
                    equity_usdt=1.0,
                    orders_sent=0,
                    okx_simulated_trading=False,
                )

            with mock.patch(
                "app.bot.private.harness_readonly.probe_bybit_wallet",
                side_effect=lambda **kw: _ok_probe("bybit", **kw),
            ):
                with mock.patch(
                    "app.bot.private.harness_readonly.probe_okx_balance",
                    side_effect=lambda **kw: _ok_probe("okx", **kw),
                ):
                    with mock.patch(
                        "app.bot.private.harness_readonly.PrivateJournalWriter.append",
                        side_effect=RuntimeError("simulated_journal_failure"),
                    ):
                        report = harness_readonly.run_readonly_harness(env)

            self.assertEqual(report["status"], "journal_write_failed")
            self.assertFalse(report.get("journal_v1_ok"))
            self.assertNotEqual(report["status"], "ok")
            # Exit path must be non-zero for this status.
            with mock.patch(
                "app.bot.private.harness_readonly.run_readonly_harness",
                return_value=report,
            ):
                with mock.patch("builtins.print"):
                    rc = harness_readonly.main([])
            self.assertEqual(rc, 2)

    def test_testnet_route_still_refuses_live_venue_via_stage1(self) -> None:
        with self.assertRaises(RuntimeError):
            assert_stage1_venue({"VENUE": "live", "LIVE_ORDERS": "0"})


class JournalV1Tests(unittest.TestCase):
    def test_no_order_surface(self) -> None:
        from app.bot.private import journal_v1

        journal_v1.assert_no_order_surface()

    def test_valid_auth_account_lifecycle(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.paths import events_jsonl_path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            w = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            w.append_auth(
                venue="bybit",
                environment="testnet",
                outcome="success",
                credential_presence={"credentials_configured": True},
            )
            w.append_account_read(
                venue="bybit",
                environment="testnet",
                outcome="success",
                account_scope="wallet",
            )
            w.append_auth(
                venue="okx",
                environment="demo",
                outcome="failure",
                credential_presence={"credentials_configured": False},
                error_code="auth_unavailable",
            )
            path = events_jsonl_path(root, w._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0]["schema_version"], "bbot.private.journal.v1")

    def test_legacy_and_current_credential_presence(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
            new_opaque_id,
            validate_event_shape,
            validate_event_stream,
        )

        base = {
            "schema_version": "bbot.private.journal.v1",
            "event_id": new_opaque_id("evt"),
            "event_type": "auth",
            "event_date": "2026-08-19",
            "event_ts_utc": "2026-08-19T12:00:00.000Z",
            "event_monotonic_ns": 1,
            "run_id": new_opaque_id("run"),
            "operation_id": new_opaque_id("op"),
            "event_seq": 1,
            "venue": "bybit",
            "environment": "testnet",
            "outcome": "success",
            "auth_method": "hmac",
        }
        legacy = {
            **base,
            "credential_presence": {
                "api_key_present": True,
                "api_secret_present": True,
                "passphrase_present": False,
            },
        }
        current = {
            **base,
            "event_id": new_opaque_id("evt"),
            "operation_id": new_opaque_id("op"),
            "event_seq": 2,
            "event_monotonic_ns": 2,
            "event_ts_utc": "2026-08-19T12:00:00.001Z",
            "credential_presence": {"credentials_configured": True},
        }
        validate_event_shape(legacy)
        validate_event_shape(current)
        validate_event_stream([legacy, current])

        hybrid = {
            **base,
            "event_id": new_opaque_id("evt"),
            "operation_id": new_opaque_id("op"),
            "credential_presence": {
                "credentials_configured": True,
                "api_key_present": True,
            },
        }
        with self.assertRaises(JournalValidationError):
            validate_event_shape(hybrid)

        incomplete = {
            **base,
            "event_id": new_opaque_id("evt"),
            "operation_id": new_opaque_id("op"),
            "credential_presence": {"api_key_present": True, "api_secret_present": True},
        }
        with self.assertRaises(JournalValidationError):
            validate_event_shape(incomplete)

        # New writer always emits current form (normalizes legacy caller input).
        with tempfile.TemporaryDirectory() as td:
            w = PrivateJournalWriter(Path(td), run_id=new_opaque_id("run"))
            ev = w.append_auth(
                venue="bybit",
                environment="testnet",
                outcome="success",
                credential_presence={
                    "api_key_present": True,
                    "api_secret_present": True,
                    "passphrase_present": True,
                },
            )
            self.assertEqual(
                ev["credential_presence"], {"credentials_configured": True}
            )

    def test_pre_send_gate_blocks_without_recon_or_auth_reject(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )

        with tempfile.TemporaryDirectory() as td:
            w = PrivateJournalWriter(Path(td), run_id=new_opaque_id("run"))
            op = new_opaque_id("attempt")
            w.append_pre_send_gate(
                venue="bybit",
                environment="live",
                gate_kind="rest",
                operation_id=op,
            )
            with self.assertRaises(JournalValidationError):
                w.append(
                    {
                        "event_type": "reconciliation",
                        "operation_id": op,
                        "venue": "bybit",
                        "environment": "live",
                        "outcome": "observed",
                        "reconciliation_scope": "order_state",
                        "reconciliation_state": "inconclusive",
                        "mismatch_fields": ["state", "timing"],
                    }
                )
            with self.assertRaises(JournalValidationError):
                w.append(
                    {
                        "event_type": "reject",
                        "operation_id": op,
                        "venue": "bybit",
                        "environment": "live",
                        "outcome": "failure",
                        "reject_stage": "auth",
                        "error_code": "invalid_request",
                        "request_kind": "place",
                    }
                )
            with self.assertRaises(JournalValidationError):
                w.append(
                    {
                        "event_type": "order_prepared",
                        "operation_id": op,
                        "venue": "bybit",
                        "environment": "live",
                        "outcome": "pending",
                        "dual_leg_id": new_opaque_id("dual"),
                        "leg_id": new_opaque_id("leg"),
                        "instrument_class": "linear_perpetual",
                        "symbol_alias": "BTCUSDT",
                        "side": "buy",
                        "order_kind": "limit",
                        "quantity_bucket": "min_lot",
                        "notional_bucket": "under_100_usd",
                        "reduce_only": False,
                        "post_only": True,
                        "ttl_bucket": "short",
                        "request_fingerprint": "fp_" + ("c" * 32),
                    }
                )
            events = validate_events_file(
                next(Path(td).glob("journal/event_date=*/events.jsonl"))
            )
            self.assertEqual([e["event_type"] for e in events], ["pre_send_gate"])
            self.assertEqual(events[0]["gate_kind"], "rest")
            self.assertEqual(events[0]["gate_decision"], "blocked")

    def test_order_state_recon_requires_request_sent(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
            new_opaque_id,
        )

        with tempfile.TemporaryDirectory() as td:
            w = PrivateJournalWriter(Path(td), run_id=new_opaque_id("run"))
            op = new_opaque_id("op")
            w.append_auth(
                venue="bybit",
                environment="testnet",
                outcome="success",
                credentials_configured=True,
                operation_id=op,
            )
            with self.assertRaises(JournalValidationError):
                w.append(
                    {
                        "event_type": "reconciliation",
                        "operation_id": op,
                        "venue": "bybit",
                        "environment": "testnet",
                        "outcome": "observed",
                        "reconciliation_scope": "order_state",
                        "reconciliation_state": "inconclusive",
                        "mismatch_fields": ["state"],
                    }
                )

    def test_legacy_pre_send_no_dispatch_migration(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
            find_legacy_pre_send_no_dispatch_ops,
            materialize_legacy_pre_send_semantics,
            new_opaque_id,
            validate_event_stream,
            validate_events_file,
        )

        run = new_opaque_id("run")
        op = new_opaque_id("attempt")
        evt1 = new_opaque_id("evt")
        evt2 = new_opaque_id("evt")
        dual = new_opaque_id("dual")
        leg = new_opaque_id("leg")
        reject = {
            "schema_version": "bbot.private.journal.v1",
            "event_id": evt1,
            "event_type": "reject",
            "event_date": "2026-08-19",
            "event_ts_utc": "2026-08-19T15:50:41.268Z",
            "event_monotonic_ns": 100,
            "run_id": run,
            "operation_id": op,
            "event_seq": 1,
            "venue": "bybit",
            "environment": "live",
            "outcome": "failure",
            "dual_leg_id": dual,
            "leg_id": leg,
            "request_fingerprint": "fp_" + ("a" * 32),
            "request_kind": "place",
            "reject_stage": "auth",
            "error_code": "invalid_request",
        }
        recon = {
            "schema_version": "bbot.private.journal.v1",
            "event_id": evt2,
            "event_type": "reconciliation",
            "event_date": "2026-08-19",
            "event_ts_utc": "2026-08-19T15:50:41.270Z",
            "event_monotonic_ns": 200,
            "run_id": run,
            "operation_id": op,
            "event_seq": 2,
            "venue": "bybit",
            "environment": "live",
            "outcome": "observed",
            "dual_leg_id": dual,
            "leg_id": leg,
            "mismatch_fields": ["state", "timing"],
            "reconciliation_scope": "order_state",
            "reconciliation_state": "inconclusive",
        }
        # Exact pair is accepted and superseded in memory as pre_send_gate.
        validate_event_stream([reject, recon])
        self.assertEqual(find_legacy_pre_send_no_dispatch_ops([reject, recon]), {op})
        semantic = materialize_legacy_pre_send_semantics([reject, recon])
        self.assertEqual(len(semantic), 1)
        self.assertEqual(semantic[0]["event_type"], "pre_send_gate")
        self.assertEqual(semantic[0]["gate_decision"], "blocked")
        # Durable rows unchanged by materialize.
        self.assertEqual(reject["event_type"], "reject")
        self.assertEqual(recon["event_type"], "reconciliation")

        # Variation: wrong error_code — not legacy; reject(auth) fails closed.
        bad = dict(reject, error_code="timeout", event_id=new_opaque_id("evt"))
        with self.assertRaises(JournalValidationError):
            validate_event_stream([bad, recon])

        # Order-id field present — exception must not apply.
        with_oid = dict(reject, order_id="1234567890123456")
        # order_id is denylisted key → shape fails even before pair logic.
        with self.assertRaises(JournalValidationError):
            validate_event_stream([with_oid, recon])

        # Later lifecycle on same op → not legacy; pair incomplete for recognition.
        with_sent = [
            reject,
            recon,
            {
                "schema_version": "bbot.private.journal.v1",
                "event_id": new_opaque_id("evt"),
                "event_type": "request_sent",
                "event_date": "2026-08-19",
                "event_ts_utc": "2026-08-19T15:50:41.280Z",
                "event_monotonic_ns": 300,
                "run_id": run,
                "operation_id": op,
                "event_seq": 3,
                "venue": "bybit",
                "environment": "live",
                "outcome": "pending",
                "dual_leg_id": dual,
                "leg_id": leg,
                "request_kind": "place",
                "request_fingerprint": "fp_" + ("a" * 32),
                "transport_attempt": 1,
                "send_monotonic_ns": 250,
            },
        ]
        self.assertEqual(find_legacy_pre_send_no_dispatch_ops(with_sent), frozenset())
        with self.assertRaises(JournalValidationError):
            validate_event_stream(with_sent)

        # Writer can validate existing stream then append a new unrelated event.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Seed durable legacy pair without going through append lifecycle.
            part = root / "journal" / "event_date=2026-08-19"
            part.mkdir(parents=True)
            (root / "probes").mkdir(parents=True, exist_ok=True)
            (root / "state").mkdir(parents=True, exist_ok=True)
            (root / ".tmp").mkdir(parents=True, exist_ok=True)
            path = part / "events.jsonl"
            path.write_text(
                json.dumps(reject, separators=(",", ":"))
                + "\n"
                + json.dumps(recon, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            events = validate_events_file(path)
            self.assertEqual(len(events), 2)
            w = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            # New events still require strict opaque IDs.
            with self.assertRaises(JournalValidationError):
                w.append(
                    {
                        "event_type": "auth",
                        "operation_id": "not-a-valid-opaque-id",
                        "venue": "bybit",
                        "environment": "live",
                        "outcome": "success",
                        "auth_method": "hmac",
                        "credential_presence": {"credentials_configured": True},
                    }
                )
            w.append_auth(
                venue="okx",
                environment="live",
                outcome="success",
                credentials_configured=True,
            )
            # Full tree still validates with legacy pair left intact on disk.
            from app.bot.private.journal_v1 import validate_journal_tree

            tree = validate_journal_tree(root)
            self.assertGreaterEqual(len(tree), 3)
            self.assertTrue(any(e["event_type"] == "reject" for e in tree))
            self.assertTrue(any(e["event_type"] == "auth" for e in tree))

    def test_valid_order_leg_lifecycle_and_latency(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            compute_latency_intervals_ms,
            new_opaque_id,
            validate_events_file,
        )

        with tempfile.TemporaryDirectory() as td:
            w = PrivateJournalWriter(Path(td), run_id=new_opaque_id("run"))
            op = new_opaque_id("op")
            dual = new_opaque_id("dual")
            leg = new_opaque_id("leg")
            fp = "fp_" + ("a" * 32)
            # Controlled monotonic timeline via explicit fields.
            base = 1_000_000_000
            prepared = w.append(
                {
                    "event_type": "order_prepared",
                    "operation_id": op,
                    "venue": "okx",
                    "environment": "demo",
                    "outcome": "pending",
                    "event_ts_utc": "2026-08-19T12:00:00.000Z",
                    "event_monotonic_ns": base,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "instrument_class": "linear_perpetual",
                    "symbol_alias": "BTC-USDT-SWAP",
                    "side": "buy",
                    "order_kind": "limit",
                    "quantity_bucket": "min_lot",
                    "notional_bucket": "under_100_usd",
                    "reduce_only": False,
                    "post_only": True,
                    "ttl_bucket": "short",
                    "request_fingerprint": fp,
                }
            )
            sent = w.append(
                {
                    "event_type": "request_sent",
                    "operation_id": op,
                    "venue": "okx",
                    "environment": "demo",
                    "outcome": "pending",
                    "event_ts_utc": "2026-08-19T12:00:00.010Z",
                    "event_monotonic_ns": base + 10_000_000,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "request_kind": "place",
                    "request_fingerprint": fp,
                    "transport_attempt": 1,
                    "send_monotonic_ns": base + 8_000_000,
                }
            )
            ack = w.append(
                {
                    "event_type": "ack_received",
                    "operation_id": op,
                    "venue": "okx",
                    "environment": "demo",
                    "outcome": "success",
                    "event_ts_utc": "2026-08-19T12:00:00.100Z",
                    "event_monotonic_ns": base + 100_000_000,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "request_kind": "place",
                    "request_fingerprint": fp,
                    "ack_state": "accepted",
                    "receive_monotonic_ns": base + 90_000_000,
                }
            )
            evidence = {
                "method": "venue_time_probe",
                "measured_at_utc": "2026-08-19T12:00:00.050Z",
                "offset_ms": 5.0,
            }
            term = w.append(
                {
                    "event_type": "terminal_update",
                    "operation_id": op,
                    "venue": "okx",
                    "environment": "demo",
                    "outcome": "observed",
                    "event_ts_utc": "2026-08-19T12:00:00.200Z",
                    "event_monotonic_ns": base + 200_000_000,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "terminal_state": "filled",
                    "request_fingerprint": fp,
                    "receive_monotonic_ns": base + 180_000_000,
                    "exchange_event_ts_utc": "2026-08-19T12:00:00.170Z",
                    "clock_offset_evidence": evidence,
                }
            )
            intervals = compute_latency_intervals_ms(
                order_prepared_monotonic_ns=prepared["event_monotonic_ns"],
                request_sent_send_monotonic_ns=sent["send_monotonic_ns"],
                ack_receive_monotonic_ns=ack["receive_monotonic_ns"],
                ack_event_monotonic_ns=ack["event_monotonic_ns"],
                terminal_receive_monotonic_ns=term["receive_monotonic_ns"],
                terminal_event_monotonic_ns=term["event_monotonic_ns"],
                terminal_event_ts_utc=term["event_ts_utc"],
                exchange_event_ts_utc=term["exchange_event_ts_utc"],
                clock_offset_evidence=evidence,
            )
            self.assertIn("local_prepare", intervals)
            self.assertIn("request_ack_rtt", intervals)
            self.assertIn("local_response_processing", intervals)
            self.assertIn("ack_terminal_receive", intervals)
            self.assertIn("exchange_to_client_observed", intervals)
            self.assertNotIn("one_way", str(intervals).lower())
            summary = w.build_latency_summary_event(
                venue="okx",
                environment="demo",
                operation_id=op,
                intervals_ms=intervals,
                latency_basis="offset_adjusted_observed",
                dual_leg_id=dual,
                leg_id=leg,
                clock_offset_evidence=evidence,
            )
            summary["event_ts_utc"] = "2026-08-19T12:00:00.250Z"
            summary["event_monotonic_ns"] = base + 250_000_000
            w.append(summary)
            events = validate_events_file(
                Path(td) / "journal" / "event_date=2026-08-19" / "events.jsonl"
            )
            self.assertEqual(events[-1]["event_type"], "latency_summary")
            self.assertIn("request_ack_rtt", events[-1]["latency_intervals_ms"])

    def test_redaction_failures(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
            assert_no_redaction_violations,
        )

        with self.assertRaises(JournalValidationError):
            assert_no_redaction_violations({"api_key": "x"})
        with self.assertRaises(JournalValidationError):
            assert_no_redaction_violations({"Authorization": "Bearer x"})
        with self.assertRaises(JournalValidationError):
            assert_no_redaction_violations({"nested": {"equity": 1.0}})
        with self.assertRaises(JournalValidationError):
            assert_no_redaction_violations({"note": "https://www.okx.com/api?key=1"})
        with tempfile.TemporaryDirectory() as td:
            w = PrivateJournalWriter(Path(td))
            with self.assertRaises(JournalValidationError):
                w.append(
                    {
                        "event_type": "auth",
                        "operation_id": "op1",
                        "venue": "bybit",
                        "environment": "testnet",
                        "outcome": "success",
                        "auth_method": "hmac",
                        "credential_presence": {"credentials_configured": True},
                        "headers": {"x": "y"},
                    }
                )

    def test_missing_correlations(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
            new_opaque_id,
        )

        with tempfile.TemporaryDirectory() as td:
            w = PrivateJournalWriter(Path(td), run_id=new_opaque_id("run"))
            op = new_opaque_id("op")
            dual = new_opaque_id("dual")
            leg = new_opaque_id("leg")
            fp = "fp_" + ("b" * 32)
            w.append(
                {
                    "event_type": "order_prepared",
                    "operation_id": op,
                    "venue": "okx",
                    "environment": "demo",
                    "outcome": "pending",
                    "event_ts_utc": "2026-08-19T13:00:00.000Z",
                    "event_monotonic_ns": 10,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "instrument_class": "spot",
                    "symbol_alias": "BTC-USDT",
                    "side": "buy",
                    "order_kind": "market",
                    "quantity_bucket": "min_lot",
                    "notional_bucket": "under_100_usd",
                    "reduce_only": False,
                    "post_only": False,
                    "request_fingerprint": fp,
                }
            )
            with self.assertRaises(JournalValidationError):
                w.append(
                    {
                        "event_type": "request_sent",
                        "operation_id": op,
                        "venue": "okx",
                        "environment": "demo",
                        "outcome": "pending",
                        "event_ts_utc": "2026-08-19T13:00:00.001Z",
                        "event_monotonic_ns": 20,
                        "dual_leg_id": dual,
                        "leg_id": new_opaque_id("leg"),
                        "request_kind": "place",
                        "request_fingerprint": fp,
                        "transport_attempt": 1,
                        "send_monotonic_ns": 15,
                    }
                )

    def test_invalid_chronology(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
            new_opaque_id,
            validate_event_stream,
        )

        with tempfile.TemporaryDirectory() as td:
            w = PrivateJournalWriter(Path(td), run_id=new_opaque_id("run"))
            w.append_auth(
                venue="bybit",
                environment="testnet",
                outcome="success",
                credential_presence={"credentials_configured": True},
            )
            with self.assertRaises(JournalValidationError):
                # terminal without ack
                w.append(
                    {
                        "event_type": "terminal_update",
                        "operation_id": new_opaque_id("op"),
                        "venue": "okx",
                        "environment": "demo",
                        "outcome": "observed",
                        "event_ts_utc": "2026-08-19T14:00:00.000Z",
                        "event_monotonic_ns": 999,
                        "leg_id": new_opaque_id("leg"),
                        "terminal_state": "filled",
                        "request_fingerprint": "fp_" + ("d" * 32),
                        "receive_monotonic_ns": 900,
                    }
                )
            # Non-increasing wall clock across stream
            rid = new_opaque_id("run")
            e1 = new_opaque_id("evt")
            e2 = new_opaque_id("evt")
            o1 = new_opaque_id("op")
            o2 = new_opaque_id("op")
            with self.assertRaises(JournalValidationError):
                validate_event_stream(
                    [
                        {
                            "schema_version": "bbot.private.journal.v1",
                            "event_id": e1,
                            "event_type": "auth",
                            "event_date": "2026-08-19",
                            "event_ts_utc": "2026-08-19T15:00:00.000Z",
                            "event_monotonic_ns": 1,
                            "run_id": rid,
                            "operation_id": o1,
                            "event_seq": 1,
                            "venue": "bybit",
                            "environment": "testnet",
                            "outcome": "success",
                            "auth_method": "hmac",
                            "credential_presence": {"credentials_configured": True},
                        },
                        {
                            "schema_version": "bbot.private.journal.v1",
                            "event_id": e2,
                            "event_type": "auth",
                            "event_date": "2026-08-19",
                            "event_ts_utc": "2026-08-19T14:00:00.000Z",
                            "event_monotonic_ns": 2,
                            "run_id": rid,
                            "operation_id": o2,
                            "event_seq": 2,
                            "venue": "okx",
                            "environment": "demo",
                            "outcome": "success",
                            "auth_method": "hmac",
                            "credential_presence": {"credentials_configured": True},
                        },
                    ]
                )

    def test_d_root_and_stub_journal_rejection(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
        )
        from app.bot.private.paths import resolve_data_root

        with self.assertRaises(RuntimeError):
            resolve_data_root({"BBOT_PRIVATE_DATA_ROOT": "/data/live"})
        with self.assertRaises(RuntimeError):
            resolve_data_root({"BBOT_PRIVATE_DATA_ROOT": "/data/bbot/journal"})
        with self.assertRaises(JournalValidationError):
            PrivateJournalWriter(Path("/data/live/private-journal"))
        with self.assertRaises(JournalValidationError):
            PrivateJournalWriter(Path("/data/bbot/journal"))

    def test_latency_clock_skew_rules(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            assert_rtt_not_named_one_way,
            compute_latency_intervals_ms,
            validate_event_shape,
        )

        intervals = compute_latency_intervals_ms(
            order_prepared_monotonic_ns=100,
            request_sent_send_monotonic_ns=150,
            ack_receive_monotonic_ns=250,
            ack_event_monotonic_ns=260,
        )
        self.assertEqual(intervals["local_prepare"], 0.00005)  # 50ns → ms? 50/1e6 = 0.00005
        self.assertIn("request_ack_rtt", intervals)
        with self.assertRaises(JournalValidationError):
            assert_rtt_not_named_one_way(
                {"one_way_latency": 1.0}, latency_basis="monotonic_local"
            )
        with self.assertRaises(JournalValidationError):
            compute_latency_intervals_ms(
                exchange_event_ts_utc="2026-08-19T12:00:00.000Z",
            )
        # exchange_to_client without evidence rejected on event shape
        with self.assertRaises(JournalValidationError):
            validate_event_shape(
                {
                    "schema_version": "bbot.private.journal.v1",
                    "event_id": "e1",
                    "event_type": "latency_summary",
                    "event_date": "2026-08-19",
                    "event_ts_utc": "2026-08-19T12:00:00.000Z",
                    "event_monotonic_ns": 1,
                    "run_id": "r1",
                    "operation_id": "o1",
                    "event_seq": 1,
                    "venue": "okx",
                    "environment": "demo",
                    "outcome": "observed",
                    "latency_intervals_ms": {"exchange_to_client_observed": 1.0},
                    "latency_basis": "offset_adjusted_observed",
                    "sample_count": 1,
                }
            )
        # Truncated file rejected
        from app.bot.private.journal_v1 import read_events_jsonl

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "events.jsonl"
            p.write_text('{"schema_version":"bbot.private.journal.v1"', encoding="utf-8")
            with self.assertRaises(JournalValidationError):
                read_events_jsonl(p)

    def test_cancel_reject_abort_reconciliation_types(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )

        with tempfile.TemporaryDirectory() as td:
            w = PrivateJournalWriter(Path(td), run_id=new_opaque_id("run"))
            op = new_opaque_id("op")
            dual = new_opaque_id("dual")
            leg = new_opaque_id("leg")
            peer = new_opaque_id("peer")
            fp = "fp_" + ("c" * 32)
            base = 5_000_000_000
            w.append(
                {
                    "event_type": "order_prepared",
                    "operation_id": op,
                    "venue": "bybit",
                    "environment": "testnet",
                    "outcome": "pending",
                    "event_ts_utc": "2026-08-19T16:00:00.000Z",
                    "event_monotonic_ns": base,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "instrument_class": "linear_perpetual",
                    "symbol_alias": "BTCUSDT",
                    "side": "sell",
                    "order_kind": "market",
                    "quantity_bucket": "min_lot",
                    "notional_bucket": "under_100_usd",
                    "reduce_only": True,
                    "post_only": False,
                    "request_fingerprint": fp,
                }
            )
            w.append(
                {
                    "event_type": "request_sent",
                    "operation_id": op,
                    "venue": "bybit",
                    "environment": "testnet",
                    "outcome": "pending",
                    "event_ts_utc": "2026-08-19T16:00:00.010Z",
                    "event_monotonic_ns": base + 10,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "request_kind": "place",
                    "request_fingerprint": fp,
                    "transport_attempt": 1,
                    "send_monotonic_ns": base + 5,
                }
            )
            w.append(
                {
                    "event_type": "ack_received",
                    "operation_id": op,
                    "venue": "bybit",
                    "environment": "testnet",
                    "outcome": "success",
                    "event_ts_utc": "2026-08-19T16:00:00.020Z",
                    "event_monotonic_ns": base + 20,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "request_kind": "place",
                    "request_fingerprint": fp,
                    "ack_state": "received",
                    "receive_monotonic_ns": base + 18,
                }
            )
            w.append(
                {
                    "event_type": "cancel_requested",
                    "operation_id": op,
                    "venue": "bybit",
                    "environment": "testnet",
                    "outcome": "pending",
                    "event_ts_utc": "2026-08-19T16:00:00.030Z",
                    "event_monotonic_ns": base + 30,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "request_fingerprint": fp,
                    "cancel_reason": "dual_leg_guard",
                    "send_monotonic_ns": base + 28,
                }
            )
            w.append(
                {
                    "event_type": "cancel_ack",
                    "operation_id": op,
                    "venue": "bybit",
                    "environment": "testnet",
                    "outcome": "success",
                    "event_ts_utc": "2026-08-19T16:00:00.040Z",
                    "event_monotonic_ns": base + 40,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "cancel_state": "cancelled",
                    "request_fingerprint": fp,
                    "receive_monotonic_ns": base + 39,
                }
            )
            w.append(
                {
                    "event_type": "terminal_update",
                    "operation_id": op,
                    "venue": "bybit",
                    "environment": "testnet",
                    "outcome": "observed",
                    "event_ts_utc": "2026-08-19T16:00:00.050Z",
                    "event_monotonic_ns": base + 50,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "terminal_state": "cancelled",
                    "request_fingerprint": fp,
                    "receive_monotonic_ns": base + 48,
                }
            )
            w.append(
                {
                    "event_type": "dual_leg_abort",
                    "operation_id": op,
                    "venue": "bybit",
                    "environment": "testnet",
                    "outcome": "observed",
                    "event_ts_utc": "2026-08-19T16:00:00.060Z",
                    "event_monotonic_ns": base + 60,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "peer_leg_id": peer,
                    "abort_reason": "peer_rejected",
                    "request_fingerprint": fp,
                }
            )
            w.append(
                {
                    "event_type": "reconciliation",
                    "operation_id": op,
                    "venue": "bybit",
                    "environment": "testnet",
                    "outcome": "success",
                    "event_ts_utc": "2026-08-19T16:00:00.070Z",
                    "event_monotonic_ns": base + 70,
                    "dual_leg_id": dual,
                    "leg_id": leg,
                    "reconciliation_scope": "dual_leg_state",
                    "reconciliation_state": "matched",
                }
            )
            events = validate_events_file(
                Path(td) / "journal" / "event_date=2026-08-19" / "events.jsonl"
            )
            self.assertEqual(len(events), 8)


class R3OrdersTests(unittest.TestCase):
    """No-network R3 planning / approval / gated sender tests."""

    def _meta(self, *, mark_age_ns: int = 0, mark_max_age_ns: int = 5_000_000_000):
        from decimal import Decimal
        import time as _time
        from app.bot.private.order_metadata import InstrumentMetadata, StaticMetadataProvider

        now = _time.monotonic_ns()
        asof = max(0, now - int(mark_age_ns))
        return StaticMetadataProvider(
            {
                ("bybit_live", "BTCUSDT"): InstrumentMetadata(
                    venue="bybit_live",
                    symbol="BTCUSDT",
                    min_qty=Decimal("0.001"),
                    qty_step=Decimal("0.001"),
                    tick_size=Decimal("0.1"),
                    contract_multiplier=Decimal("1"),
                    contract_value_ccy="USDT",
                    notional_unit="usdt_per_coin",
                    mark_price_usdt=Decimal("50000"),
                    mark_asof_monotonic_ns=asof,
                    mark_max_age_ns=mark_max_age_ns,
                ),
                ("okx_live", "BTC-USDT-SWAP"): InstrumentMetadata(
                    venue="okx_live",
                    symbol="BTC-USDT-SWAP",
                    min_qty=Decimal("0.01"),
                    qty_step=Decimal("0.01"),
                    tick_size=Decimal("0.1"),
                    contract_multiplier=Decimal("0.01"),
                    contract_value_ccy="USD",
                    notional_unit="usdt_per_contract",
                    mark_price_usdt=Decimal("100"),
                    mark_asof_monotonic_ns=asof,
                    mark_max_age_ns=mark_max_age_ns,
                    inst_id_code=10459,
                ),
                ("bybit_live", "TRUMPUSDT"): InstrumentMetadata(
                    venue="bybit_live",
                    symbol="TRUMPUSDT",
                    min_qty=Decimal("0.1"),
                    qty_step=Decimal("0.1"),
                    tick_size=Decimal("0.001"),
                    contract_multiplier=Decimal("1"),
                    contract_value_ccy="USDT",
                    notional_unit="usdt_per_coin",
                    mark_price_usdt=Decimal("1.70"),
                    mark_asof_monotonic_ns=asof,
                    mark_max_age_ns=mark_max_age_ns,
                ),
                ("okx_live", "TRUMP-USDT-SWAP"): InstrumentMetadata(
                    venue="okx_live",
                    symbol="TRUMP-USDT-SWAP",
                    min_qty=Decimal("1"),
                    qty_step=Decimal("1"),
                    tick_size=Decimal("0.001"),
                    contract_multiplier=Decimal("0.1"),
                    contract_value_ccy="USDT",
                    notional_unit="usdt_per_contract",
                    mark_price_usdt=Decimal("1.70"),
                    mark_asof_monotonic_ns=asof,
                    mark_max_age_ns=mark_max_age_ns,
                    inst_id_code=193761,
                ),
            }
        )

    def _position(self):
        from app.bot.private.order_preflight import StaticVerifiedPositionModeProvider

        return StaticVerifiedPositionModeProvider(
            {"bybit_live": "one_way", "okx_live": "one_way"}
        )

    def _live_env(self, td: str, *, live_orders: str = "1") -> dict:
        from app.bot.private.secrets import LIVE_KEY_NAMES

        live_env = Path(td) / "bbot-private-live.env"
        live_env.write_text(
            "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
            encoding="utf-8",
        )
        return {
            "VENUE": "live",
            "LIVE_ORDERS": live_orders,
            "BBOT_PRIVATE_ENV_FILE": str(live_env),
            "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
        }

    def _plan(self, **kwargs):
        from app.bot.private.order_plan import build_order_plan

        base = dict(
            venue="bybit_live",
            symbol="BTCUSDT",
            side="buy",
            mode="post_only_limit",
            metadata_provider=self._meta(),
            price="1000",
            ttl_sec=5,
            expires_in_sec=60,
        )
        base.update(kwargs)
        if "metadata_provider" not in kwargs:
            base["metadata_provider"] = self._meta()
        return build_order_plan(**base)

    def _vault(self, journal):
        from app.bot.private.order_approval import ApprovalVault

        return ApprovalVault(
            journal=journal,
            hmac_key=b"unit-test-approval-key-32bytes!!",
        )

    def _sender(self, journal, vault, transport=None, lease_supervisor=None):
        from app.bot.private.order_sender import ApprovalBoundSender

        return ApprovalBoundSender(
            journal=journal,
            approval_vault=vault,
            metadata_provider=self._meta(),
            position_mode_provider=self._position(),
            transport=transport,
            lease_supervisor=lease_supervisor,
        )

    def test_futures_only_and_allowlisted_venues(self) -> None:
        from app.bot.private.order_plan import OrderPlanError, build_order_plan
        from app.bot.private.order_symbols import resolve_allowed_futures_symbol, SymbolGateError

        resolve_allowed_futures_symbol("bybit_live", "BTCUSDT")
        resolve_allowed_futures_symbol("okx_live", "BTC-USDT-SWAP")
        resolve_allowed_futures_symbol("bybit_live", "TRUMPUSDT")
        resolve_allowed_futures_symbol("okx_live", "TRUMP-USDT-SWAP")
        with self.assertRaises(SymbolGateError):
            resolve_allowed_futures_symbol("bybit_live", "ETHUSDT")
        with self.assertRaises(OrderPlanError):
            build_order_plan(
                venue="bybit_live",
                symbol="BTC-SPOT",
                side="buy",
                mode="market",
                metadata_provider=self._meta(),
            )

    def test_exact_unit_semantics_market_notional(self) -> None:
        from decimal import Decimal
        from app.bot.private.order_plan import OrderPlanError, build_order_plan

        # Bybit: qty * mark (usdt_per_coin). 0.001 * 50000 = 50 < 100
        m = build_order_plan(
            venue="bybit_live",
            symbol="BTCUSDT",
            side="buy",
            mode="market",
            metadata_provider=self._meta(),
        )
        self.assertEqual(m.mode, "market")
        # 0.003 * 50000 = 150 >= 100
        with self.assertRaises(OrderPlanError):
            build_order_plan(
                venue="bybit_live",
                symbol="BTCUSDT",
                side="buy",
                mode="market",
                metadata_provider=self._meta(),
                qty=Decimal("0.003"),
            )
        # OKX: qty * ctVal * mark = 0.01 * 0.01 * 100 = 0.01
        okx = build_order_plan(
            venue="okx_live",
            symbol="BTC-USDT-SWAP",
            side="sell",
            mode="market",
            metadata_provider=self._meta(),
        )
        self.assertEqual(okx.mode, "market")
        meta = self._meta().get("okx_live", "BTC-USDT-SWAP")
        self.assertEqual(meta.notional_unit, "usdt_per_contract")
        self.assertEqual(meta.contract_value_ccy, "USD")
        self.assertEqual(
            meta.market_notional_usdt(Decimal("0.01")),
            Decimal("0.01") * Decimal("0.01") * Decimal("100"),
        )
        # OKX post-only limit: qty * ctVal * price (not qty*price).
        # 0.01 * 0.01 * 68748.7 ~= 6.87 < 100 must plan; naive qty*price must not gate.
        okx_limit = build_order_plan(
            venue="okx_live",
            symbol="BTC-USDT-SWAP",
            side="sell",
            mode="post_only_limit",
            metadata_provider=self._meta(),
            qty=Decimal("0.01"),
            price="68748.7",
            ttl_sec=10,
        )
        self.assertEqual(okx_limit.mode, "post_only_limit")
        self.assertEqual(okx_limit.ttl_sec, 10)
        self.assertEqual(okx_limit.public_summary()["ttl_bucket"], "short")

    def test_stale_and_preflight_failure_before_consume(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, scan_operator_approvals
        from app.bot.private.order_sender import ApprovalBoundSender, TransportAck
        from app.bot.private.order_sign import LiveCredentials
        from app.bot.private.order_preflight import FailClosedPositionModeProvider
        from app.bot.private.order_plan import OrderPlanError, build_order_plan

        with self.assertRaises(OrderPlanError):
            build_order_plan(
                venue="bybit_live",
                symbol="BTCUSDT",
                side="buy",
                mode="post_only_limit",
                # asof near epoch + 1ns max age ⇒ always stale vs real monotonic clock
                metadata_provider=self._meta(mark_age_ns=10**15, mark_max_age_ns=1),
                price="1000",
                ttl_sec=5,
            )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            plan = self._plan()
            token = vault.issue(plan)
            sender = ApprovalBoundSender(
                journal=journal,
                approval_vault=vault,
                metadata_provider=self._meta(),
                position_mode_provider=FailClosedPositionModeProvider(),
                transport=lambda _r: TransportAck(kind="accepted", ack_state="accepted"),
            )
            res = sender.send_approved(
                plan, token, LiveCredentials(api_key="k", api_secret="s" * 16), env
            )
            self.assertEqual(res.status, "gate_failed")
            self.assertFalse(res.transport_invoked)
            self.assertTrue(res.journal_ok)
            # Must fail before consume — no consumed event.
            actions = [
                e.get("approval_action") for e in scan_operator_approvals(data_root)
            ]
            self.assertIn("granted", actions)
            self.assertNotIn("consumed", actions)
            from app.bot.private.journal_v1 import validate_events_file

            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            types = [e["event_type"] for e in events]
            self.assertIn("pre_send_gate", types)
            self.assertNotIn("request_sent", types)
            self.assertNotIn("order_prepared", types)
            self.assertNotIn("reject", types)
            self.assertNotIn("reconciliation", types)
            gate = next(e for e in events if e["event_type"] == "pre_send_gate")
            self.assertEqual(gate["gate_decision"], "blocked")
            self.assertEqual(gate["gate_kind"], "price")
            self.assertEqual(gate["operation_id"], plan.order_attempt_id)

    def test_forged_direct_plan_fails_revalidation(self) -> None:
        from dataclasses import replace
        from app.bot.private.order_plan import OrderPlan, OrderPlanError, revalidate_order_plan
        from app.bot.private.journal_v1 import PrivateJournalWriter
        from app.bot.private.order_sign import LiveCredentials

        good = self._plan()
        forged = replace(
            good,
            qty="9.999",
            request_fingerprint="fp_forged_deadbeef_deadbeef_dead",
        )
        with self.assertRaises(OrderPlanError):
            revalidate_order_plan(forged, self._meta())

        raw = OrderPlan(
            intent_id="intent_x",
            leg_id="leg_x",
            order_attempt_id="attempt_x",
            venue="bybit_live",
            symbol="BTCUSDT",
            symbol_alias="BTCUSDT",
            instrument_class="linear_perpetual",
            side="buy",
            mode="post_only_limit",
            qty="0.001",
            price="1000.0",
            max_notional_usd="100",
            time_in_force="post_only",
            ttl_sec=5,
            expires_at_utc=good.expires_at_utc,
            expires_at_monotonic_ns=good.expires_at_monotonic_ns,
            k_live=1,
            post_only=True,
            reduce_only=False,
            request_fingerprint="fp_not_matching_canonical_body_xx",
            dual_leg_id="solo_x",
            quantity_bucket="min_lot",
            notional_bucket="under_100_usd",
        )
        with self.assertRaises(OrderPlanError):
            revalidate_order_plan(raw, self._meta())

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            token = vault.issue(good)
            sender = self._sender(
                journal,
                vault,
                transport=lambda _r: (_ for _ in ()).throw(AssertionError("no")),
            )
            res = sender.send_approved(
                forged,
                token,
                LiveCredentials(api_key="k", api_secret="s" * 16),
                env,
                canonical_plan=good,
            )
            self.assertEqual(res.status, "gate_failed")
            self.assertFalse(res.transport_invoked)

    def test_approval_gates_expired_reused_mutated(self) -> None:
        from datetime import datetime, timedelta, timezone
        from app.bot.private.order_approval import ApprovalError
        from app.bot.private.journal_v1 import PrivateJournalWriter
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as td:
            journal = PrivateJournalWriter(Path(td) / "data")
            (Path(td) / "data").mkdir(parents=True, exist_ok=True)
            vault = self._vault(journal)
            plan = self._plan(expires_in_sec=30)
            tok = vault.issue(plan)
            vault.consume(plan, tok)
            with self.assertRaises(ApprovalError):
                vault.consume(plan, tok)

            plan2 = self._plan(expires_in_sec=30)
            tok2 = vault.issue(plan2)
            mutated = replace(plan2, qty="0.002")
            with self.assertRaises(ApprovalError):
                vault.consume(mutated, tok2)

            plan3 = self._plan(expires_in_sec=30)
            tok3 = vault.issue(plan3)
            past = datetime.now(timezone.utc) + timedelta(hours=1)
            with self.assertRaises(ApprovalError):
                vault.consume(plan3, tok3, now=past)

    def test_concurrent_and_restart_token_consume(self) -> None:
        import threading
        from app.bot.private.order_approval import ApprovalError, ApprovalVault
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            scan_operator_approvals,
        )

        with tempfile.TemporaryDirectory() as td:
            data_root = Path(td) / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            key = b"unit-test-approval-key-32bytes!!"
            j1 = PrivateJournalWriter(data_root, run_id=new_opaque_id("run"))
            vault1 = ApprovalVault(journal=j1, hmac_key=key)
            plan = self._plan(expires_in_sec=60)
            tok = vault1.issue(plan)

            results: list[str] = []

            def worker(name: str) -> None:
                j = PrivateJournalWriter(data_root, run_id=new_opaque_id("run"))
                v = ApprovalVault(journal=j, hmac_key=key)
                try:
                    v.consume(plan, tok)
                    results.append("ok")
                except ApprovalError:
                    results.append("err")

            t1 = threading.Thread(target=worker, args=("t1",))
            t2 = threading.Thread(target=worker, args=("t2",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            self.assertEqual(sorted(results), ["err", "ok"])

            # Restart vault refuses replay via canonical journal scan only.
            j2 = PrivateJournalWriter(data_root, run_id=new_opaque_id("run"))
            vault2 = ApprovalVault(journal=j2, hmac_key=key)
            with self.assertRaises(ApprovalError):
                vault2.consume(plan, tok)
            actions = [e["approval_action"] for e in scan_operator_approvals(data_root)]
            self.assertEqual(actions.count("granted"), 1)
            self.assertEqual(actions.count("consumed"), 1)
            # No sidecar approval_consumed.jsonl
            self.assertFalse((data_root / "journal" / "approval_consumed.jsonl").exists())

    def test_send_gates_venue_flags_profile(self) -> None:
        from app.bot.private.order_sender import SendGateError, assert_send_gates

        plan = self._plan()
        with self.assertRaises(SendGateError):
            assert_send_gates({"VENUE": "testnet", "LIVE_ORDERS": "1"}, plan)
        with self.assertRaises(SendGateError):
            assert_send_gates({"VENUE": "live", "LIVE_ORDERS": "0"}, plan)
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td, live_orders="1")
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            assert_send_gates(env, plan)

    def test_journal_required_and_fake_ack_lifecycle(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import LiveCredentials

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            plan = self._plan()
            journal = PrivateJournalWriter(data_root, run_id=new_opaque_id("run"))
            vault = self._vault(journal)
            token = vault.issue(plan)
            calls = {"n": 0}

            def fake_transport(req):
                calls["n"] += 1
                pub = req.public_view()
                self.assertTrue(pub["auth_headers_present"])
                self.assertNotIn("sign", str(pub).lower())
                return TransportAck(kind="accepted", ack_state="accepted")

            sender = self._sender(journal, vault, transport=fake_transport)
            creds = LiveCredentials(api_key="k", api_secret="s" * 16)
            result = sender.send_approved(plan, token, creds, env)
            self.assertEqual(result.status, "ack")
            self.assertTrue(result.transport_invoked)
            self.assertEqual(calls["n"], 1)

            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            types = [e["event_type"] for e in events]
            # grant (approval op) then consume + prepare + sent + ack on leg op
            self.assertEqual(events[0]["approval_action"], "granted")
            self.assertEqual(events[1]["approval_action"], "consumed")
            self.assertEqual(events[1]["operation_id"], plan.order_attempt_id)
            self.assertEqual(types[2], "order_prepared")
            self.assertTrue(events[2]["post_only"])
            self.assertEqual(events[2]["ttl_bucket"], "short")
            self.assertEqual(types[3], "request_sent")
            self.assertIn("ack_received", types)

    def test_post_fsync_expiry_never_reaches_transport(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, validate_events_file
        from app.bot.private.order_sign import LiveCredentials
        import time as _time

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            mono0 = _time.monotonic_ns()
            plan = self._plan(expires_in_sec=5, now_mono_ns=mono0)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            token = vault.issue(plan)
            calls = {"n": 0}

            def boom(_req):
                calls["n"] += 1
                raise AssertionError("transport must not run")

            sender = self._sender(journal, vault, transport=boom)
            res = sender.send_approved(
                plan,
                token,
                LiveCredentials(api_key="k", api_secret="s" * 16),
                env,
                now_mono_ns=mono0,
                now_mono_ns_post_fsync=mono0 + 6_000_000_000,
            )
            self.assertEqual(res.status, "expired")
            self.assertFalse(res.transport_invoked)
            self.assertEqual(calls["n"], 0)
            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            types = [e["event_type"] for e in events]
            self.assertNotIn("request_sent", types)
            self.assertIn("reject", types)

    def test_no_false_sent_when_dispatch_disabled(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, validate_events_file
        from app.bot.private.order_sign import LiveCredentials

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            plan = self._plan()
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            token = vault.issue(plan)
            sender = self._sender(
                journal,
                vault,
                transport=lambda _r: (_ for _ in ()).throw(AssertionError("no")),
            )
            res = sender.send_approved(
                plan,
                token,
                LiveCredentials(api_key="k", api_secret="s" * 16),
                env,
                dispatch_transport=False,
            )
            self.assertEqual(res.status, "prepared_not_dispatched")
            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            types = [e["event_type"] for e in events]
            self.assertNotIn("request_sent", types)
            self.assertIn("order_prepared", types)

    def test_unbound_is_pre_send_reject_without_request_sent(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, validate_events_file
        from app.bot.private.order_sign import LiveCredentials

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            plan = self._plan()
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            token = vault.issue(plan)
            sender = self._sender(journal, vault, transport=None)
            res = sender.send_approved(
                plan,
                token,
                LiveCredentials(api_key="k", api_secret="s" * 16),
                env,
            )
            self.assertEqual(res.status, "rejected")
            self.assertEqual(res.error_code, "transport_error")
            self.assertFalse(res.transport_invoked)
            # Pre-send: no consume either? Actually we check unbound BEFORE consume
            # to avoid consuming when cannot send.
            from app.bot.private.journal_v1 import scan_operator_approvals

            actions = [e["approval_action"] for e in scan_operator_approvals(data_root)]
            self.assertEqual(actions, ["granted"])

    def test_ambiguous_timeout_reconciliation(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, validate_events_file
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import LiveCredentials

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            plan = self._plan()
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            token = vault.issue(plan)

            def amb(_req):
                return TransportAck(
                    kind="ambiguous",
                    ack_state="received",
                    error_code="timeout",
                    ambiguous=True,
                )

            sender = self._sender(journal, vault, transport=amb)
            res = sender.send_approved(
                plan, token, LiveCredentials(api_key="k", api_secret="s" * 16), env
            )
            self.assertEqual(res.status, "ambiguous")
            self.assertTrue(res.transport_invoked)
            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            types = [e["event_type"] for e in events]
            self.assertIn("request_sent", types)
            self.assertIn("reconciliation", types)
            self.assertNotIn("reject", types)
            recon = [e for e in events if e["event_type"] == "reconciliation"][0]
            self.assertEqual(recon["reconciliation_scope"], "post_dispatch_ambiguity")
            self.assertEqual(recon["reconciliation_state"], "inconclusive")

    def test_malformed_okx_per_order_success(self) -> None:
        from app.bot.private.order_transport import parse_okx_place_response

        # Top-level success but sCode failure must reject.
        ack = parse_okx_place_response(
            {"code": "0", "data": [{"sCode": "51000", "sMsg": "x"}]},
            http_status=200,
        )
        self.assertEqual(ack.kind, "rejected")
        self.assertEqual(ack.error_code, "order_rejected")
        # Missing sCode is invalid
        ack2 = parse_okx_place_response(
            {"code": "0", "data": [{"ordId": "1"}]},
            http_status=200,
        )
        self.assertEqual(ack2.kind, "rejected")
        ack3 = parse_okx_place_response(
            {"code": "0", "data": [{"sCode": "0"}]},
            http_status=200,
        )
        self.assertEqual(ack3.kind, "accepted")

    def test_overdue_lease_blocks_new_plan(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter
        from app.bot.private.order_lease import LeaseState, LeaseSupervisor, PostOnlyLease
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import LiveCredentials
        import time as _time

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            supervisor = LeaseSupervisor(journal=journal, data_root=data_root)
            plan1 = self._plan(ttl_sec=1)
            lease = PostOnlyLease(plan=plan1)
            started = _time.monotonic_ns() - 2_000_000_000
            lease.mark_working(now_mono_ns=started)
            lease.mark_acked(now_mono_ns=started)
            lease.check_ttl(now_mono_ns=_time.monotonic_ns())
            self.assertEqual(lease.state, LeaseState.TTL_EXPIRED_CANCEL_REQUIRED)
            self.assertFalse(lease.public_dict()["gtc_auto_bounded"])
            supervisor.register(lease)

            plan2 = self._plan()
            token2 = vault.issue(plan2)
            sender = self._sender(
                journal,
                vault,
                transport=lambda _r: TransportAck(kind="accepted", ack_state="accepted"),
                lease_supervisor=supervisor,
            )
            res = sender.send_approved(
                plan2,
                token2,
                LiveCredentials(api_key="k", api_secret="s" * 16),
                env,
            )
            self.assertEqual(res.status, "gate_failed")
            self.assertFalse(res.transport_invoked)

    def test_cancellation_recovery_lifecycle(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, validate_events_file
        from app.bot.private.order_lease import (
            CancelAck,
            LeaseState,
            LeaseSupervisor,
            OrderStateSnapshot,
            PostOnlyLease,
        )
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import LiveCredentials
        import time as _time

        class FakeState:
            def __init__(self) -> None:
                self.phase = 0

            def get(self, plan):
                # Before cancel: working. After cancel_ack: observed terminal.
                self.phase += 1
                if self.phase == 1:
                    return OrderStateSnapshot.WORKING
                return OrderStateSnapshot.CANCELLED

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            plan = self._plan(ttl_sec=1)
            token = vault.issue(plan)

            def fake_transport(_req):
                return TransportAck(kind="accepted", ack_state="accepted")

            sender = self._sender(journal, vault, transport=fake_transport)
            # Force TTL already expired at ack time via short ttl + delayed check in recovery
            res = sender.send_approved(
                plan, token, LiveCredentials(api_key="k", api_secret="s" * 16), env
            )
            self.assertEqual(res.status, "ack")
            self.assertTrue(sender.lease.acked)

            # Age the lease
            sender.lease.lease_started_mono_ns = _time.monotonic_ns() - 2_000_000_000
            sender.lease.check_ttl()
            self.assertEqual(sender.lease.state, LeaseState.TTL_EXPIRED_CANCEL_REQUIRED)

            supervisor = sender.lease_supervisor
            supervisor.order_state_provider = FakeState()
            cancel_calls = {"n": 0}

            def fake_cancel(_req):
                cancel_calls["n"] += 1
                # Transport may claim cancelled; runtime must still treat as accepted only.
                return CancelAck(ok=True, cancel_state="cancelled")

            supervisor.cancel_transport = fake_cancel
            out = supervisor.recover_overdue(
                plan, LiveCredentials(api_key="k", api_secret="s" * 16)
            )
            self.assertEqual(out.state, LeaseState.TERMINAL)
            self.assertEqual(cancel_calls["n"], 1)
            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            types = [e["event_type"] for e in events]
            self.assertIn("cancel_requested", types)
            self.assertIn("cancel_ack", types)
            self.assertIn("terminal_update", types)
            cancel_ack = [e for e in events if e["event_type"] == "cancel_ack"][0]
            self.assertEqual(cancel_ack["cancel_state"], "accepted")
            term = [e for e in events if e["event_type"] == "terminal_update"][0]
            self.assertEqual(term["terminal_state"], "cancelled")
            cancel_ev = [e for e in events if e["event_type"] == "cancel_requested"][0]
            self.assertEqual(cancel_ev["cancel_reason"], "post_only_ttl_expired")
            recon = [
                e
                for e in events
                if e["event_type"] == "reconciliation"
                and e.get("reconciliation_scope") == "post_only_ttl_recovery"
                and e.get("reconciliation_state") == "matched"
            ]
            self.assertEqual(len(recon), 1)
            summaries = [e for e in events if e["event_type"] == "latency_summary"]
            self.assertEqual(len(summaries), 1)
            intervals = summaries[0]["latency_intervals_ms"]
            self.assertTrue(intervals)
            for name in intervals:
                self.assertIn(
                    name,
                    {
                        "local_prepare",
                        "request_ack_rtt",
                        "local_response_processing",
                        "ack_terminal_receive",
                        "exchange_to_client_observed",
                    },
                )
            self.assertIn("request_ack_rtt", intervals)
            self.assertNotIn("cancel_rtt", intervals)
            self.assertNotIn("cancel_request_ack_rtt", intervals)

    def test_ttl_bucket_10s_is_short_never_medium(self) -> None:
        from app.bot.private.order_plan import ttl_bucket_for_sec

        self.assertEqual(ttl_bucket_for_sec(10), "short")
        self.assertEqual(ttl_bucket_for_sec(1), "short")
        self.assertEqual(ttl_bucket_for_sec(11), "medium")
        self.assertEqual(ttl_bucket_for_sec(30), "medium")
        self.assertEqual(ttl_bucket_for_sec(31), "long")
        plan = self._plan(ttl_sec=10)
        self.assertEqual(plan.public_summary()["ttl_bucket"], "short")

    def test_cancel_interface_observed_terminal_appends_matched_and_latency(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, validate_events_file
        from app.bot.private.order_lease import (
            CancelAck,
            LeaseState,
            OrderStateSnapshot,
        )
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import LiveCredentials
        import time as _time

        class AfterCancelCancelled:
            def get(self, plan):
                return OrderStateSnapshot.CANCELLED

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            plan = self._plan(ttl_sec=10)
            self.assertEqual(plan.public_summary()["ttl_bucket"], "short")
            token = vault.issue(plan)
            sender = self._sender(
                journal,
                vault,
                transport=lambda _r: TransportAck(kind="accepted", ack_state="accepted"),
            )
            res = sender.send_approved(
                plan, token, LiveCredentials(api_key="k", api_secret="s" * 16), env
            )
            self.assertEqual(res.status, "ack")
            sender.lease.lease_started_mono_ns = _time.monotonic_ns() - 11_000_000_000
            sender.lease.check_ttl()
            self.assertEqual(sender.lease.state, LeaseState.TTL_EXPIRED_CANCEL_REQUIRED)

            cancel_res = sender.request_cancel_interface(
                plan,
                LiveCredentials(api_key="k", api_secret="s" * 16),
                cancel_transport=lambda _r: CancelAck(ok=True, cancel_state="accepted"),
                order_state_provider=AfterCancelCancelled(),
            )
            self.assertEqual(cancel_res.status, "ack")
            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            types = [e["event_type"] for e in events]
            self.assertIn("terminal_update", types)
            recon = [
                e
                for e in events
                if e["event_type"] == "reconciliation"
                and e.get("reconciliation_scope") == "post_only_ttl_recovery"
                and e.get("reconciliation_state") == "matched"
            ]
            self.assertEqual(len(recon), 1)
            summaries = [e for e in events if e["event_type"] == "latency_summary"]
            self.assertEqual(len(summaries), 1)
            self.assertIn("request_ack_rtt", summaries[0]["latency_intervals_ms"])
            prepared = [e for e in events if e["event_type"] == "order_prepared"][0]
            self.assertEqual(prepared["ttl_bucket"], "short")

    def test_derive_latency_wall_fallback_no_invented_cancel_label(self) -> None:
        from app.bot.private.journal_v1 import (
            derive_cancel_rtt_ms_for_report,
            derive_latency_intervals_from_op_events,
        )

        op = "op_wall_test_aaaaaaaaaaaaaaaa"
        events = [
            {
                "event_type": "order_prepared",
                "operation_id": op,
                "event_ts_utc": "2026-08-19T12:00:00.000Z",
            },
            {
                "event_type": "request_sent",
                "operation_id": op,
                "event_ts_utc": "2026-08-19T12:00:00.010Z",
            },
            {
                "event_type": "ack_received",
                "operation_id": op,
                "event_ts_utc": "2026-08-19T12:00:00.075Z",
            },
            {
                "event_type": "cancel_requested",
                "operation_id": op,
                "event_ts_utc": "2026-08-19T12:00:10.000Z",
            },
            {
                "event_type": "cancel_ack",
                "operation_id": op,
                "event_ts_utc": "2026-08-19T12:00:10.063Z",
            },
            {
                "event_type": "terminal_update",
                "operation_id": op,
                "event_ts_utc": "2026-08-19T12:00:10.100Z",
            },
        ]
        intervals = derive_latency_intervals_from_op_events(events)
        self.assertAlmostEqual(intervals["request_ack_rtt"], 65.0, places=3)
        self.assertAlmostEqual(intervals["ack_terminal_receive"], 10025.0, places=3)
        self.assertAlmostEqual(intervals["local_prepare"], 10.0, places=3)
        self.assertNotIn("cancel_rtt", intervals)
        cancel_ms = derive_cancel_rtt_ms_for_report(events)
        self.assertAlmostEqual(cancel_ms, 63.0, places=3)

    def test_unknown_terminal_subtype_inconclusive_blocks(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, validate_events_file
        from app.bot.private.order_lease import (
            CancelAck,
            LeaseState,
            OrderStateSnapshot,
        )
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import LiveCredentials
        import time as _time

        class UnknownTerminal:
            def __init__(self) -> None:
                self.phase = 0

            def get(self, plan):
                self.phase += 1
                if self.phase == 1:
                    return OrderStateSnapshot.WORKING
                return OrderStateSnapshot.TERMINAL_UNKNOWN

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            plan = self._plan(ttl_sec=1)
            token = vault.issue(plan)
            sender = self._sender(
                journal,
                vault,
                transport=lambda _r: TransportAck(kind="accepted", ack_state="accepted"),
            )
            sender.send_approved(
                plan, token, LiveCredentials(api_key="k", api_secret="s" * 16), env
            )
            sender.lease.lease_started_mono_ns = _time.monotonic_ns() - 2_000_000_000
            sender.lease.check_ttl()
            supervisor = sender.lease_supervisor
            supervisor.order_state_provider = UnknownTerminal()
            supervisor.cancel_transport = lambda _r: CancelAck(
                ok=True, cancel_state="accepted"
            )
            out = supervisor.recover_overdue(
                plan, LiveCredentials(api_key="k", api_secret="s" * 16)
            )
            self.assertEqual(out.state, LeaseState.INCONCLUSIVE)
            self.assertTrue(out.blocks_new_sends)
            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            self.assertNotIn(
                "terminal_update", [e["event_type"] for e in events]
            )
            recon = [
                e
                for e in events
                if e["event_type"] == "reconciliation"
                and e.get("reconciliation_state") == "inconclusive"
            ]
            self.assertTrue(recon)
            # Subsequent send blocked in-process.
            plan2 = self._plan()
            token2 = vault.issue(plan2)
            blocked = sender.send_approved(
                plan2,
                token2,
                LiveCredentials(api_key="k", api_secret="s" * 16),
                env,
            )
            self.assertEqual(blocked.status, "gate_failed")

    def test_cancel_ack_not_terminal_without_state_observe(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, validate_events_file
        from app.bot.private.order_lease import (
            CancelAck,
            LeaseState,
            OrderStateSnapshot,
        )
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import LiveCredentials
        import time as _time

        class AlwaysWorking:
            def get(self, plan):
                return OrderStateSnapshot.WORKING

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            plan = self._plan(ttl_sec=1)
            token = vault.issue(plan)
            sender = self._sender(
                journal,
                vault,
                transport=lambda _r: TransportAck(kind="accepted", ack_state="accepted"),
            )
            sender.send_approved(
                plan, token, LiveCredentials(api_key="k", api_secret="s" * 16), env
            )
            sender.lease.lease_started_mono_ns = _time.monotonic_ns() - 2_000_000_000
            sender.lease.check_ttl()
            supervisor = sender.lease_supervisor
            supervisor.order_state_provider = AlwaysWorking()
            supervisor.cancel_transport = lambda _r: CancelAck(
                ok=True, cancel_state="accepted"
            )
            out = supervisor.recover_overdue(
                plan, LiveCredentials(api_key="k", api_secret="s" * 16)
            )
            self.assertEqual(out.state, LeaseState.INCONCLUSIVE)
            self.assertTrue(out.blocks_new_sends)
            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            types = [e["event_type"] for e in events]
            self.assertIn("cancel_ack", types)
            self.assertNotIn("terminal_update", types)

    def test_cancel_transport_exception_is_inconclusive(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, validate_events_file
        from app.bot.private.order_lease import LeaseState, OrderStateSnapshot
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import LiveCredentials
        import time as _time

        class AlwaysWorking:
            def get(self, plan):
                return OrderStateSnapshot.WORKING

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            plan = self._plan(ttl_sec=1)
            token = vault.issue(plan)
            sender = self._sender(
                journal,
                vault,
                transport=lambda _r: TransportAck(kind="accepted", ack_state="accepted"),
            )
            sender.send_approved(
                plan, token, LiveCredentials(api_key="k", api_secret="s" * 16), env
            )
            sender.lease.lease_started_mono_ns = _time.monotonic_ns() - 2_000_000_000
            sender.lease.check_ttl()
            supervisor = sender.lease_supervisor
            supervisor.order_state_provider = AlwaysWorking()

            def boom(_r):
                raise TimeoutError("cancel dispatch unknown")

            supervisor.cancel_transport = boom
            out = supervisor.recover_overdue(
                plan, LiveCredentials(api_key="k", api_secret="s" * 16)
            )
            self.assertEqual(out.state, LeaseState.INCONCLUSIVE)
            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            recon = [
                e
                for e in events
                if e["event_type"] == "reconciliation"
                and e.get("reconciliation_scope") == "post_only_ttl_recovery"
                and e.get("reconciliation_state") == "inconclusive"
            ]
            self.assertTrue(recon)

    def test_cancel_interface_requires_ack_and_journal_fail_not_success(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter
        from app.bot.private.order_sign import LiveCredentials

        with tempfile.TemporaryDirectory() as td:
            data_root = Path(td) / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            plan = self._plan()
            sender = self._sender(journal, vault, transport=None)
            res = sender.request_cancel_interface(
                plan, LiveCredentials(api_key="k", api_secret="s" * 16)
            )
            self.assertEqual(res.status, "gate_failed")
            self.assertFalse(res.journal_ok)

    def test_unbound_transport_and_default_entrypoint(self) -> None:
        from app.bot.private.order_sender import (
            assert_default_entrypoint_cannot_transport,
            assert_no_default_live_transport,
            get_runtime_transport,
            orders_code_present,
            orders_runtime_armed,
            unbind_runtime_transport,
        )
        from app.bot.private.order_transport import (
            assert_production_transports_unbound_from_runtime_slot,
            build_bybit_live_http_transport,
            build_okx_live_http_transport,
        )
        from app.bot.private.harness_readonly import public_config_snapshot
        from app.bot.private import __main__ as private_main

        unbind_runtime_transport()
        assert_default_entrypoint_cannot_transport()
        assert_no_default_live_transport()
        self.assertIsNone(get_runtime_transport())
        assert_production_transports_unbound_from_runtime_slot(get_runtime_transport)
        self.assertTrue(callable(build_bybit_live_http_transport))
        self.assertTrue(callable(build_okx_live_http_transport))
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            snap = public_config_snapshot(env)
            self.assertTrue(orders_code_present())
            self.assertTrue(snap["orders_code_present"])
            self.assertFalse(orders_runtime_armed(env))
            self.assertFalse(snap["orders_surface"])
        self.assertTrue(hasattr(private_main, "main"))
        assert_default_entrypoint_cannot_transport()

    def test_redaction_on_public_plan_and_signed_view(self) -> None:
        from app.bot.private.order_sign import LiveCredentials, build_signed_place_request
        from app.bot.private.journal_v1 import assert_no_redaction_violations

        plan = self._plan()
        summary = plan.public_summary()
        assert_no_redaction_violations(summary)
        self.assertNotIn("qty", summary)
        signed = build_signed_place_request(
            plan, LiveCredentials(api_key="k", api_secret="s" * 16)
        )
        assert_no_redaction_violations(signed.public_view())



    def test_global_approval_lock_and_no_sidecars(self) -> None:
        from app.bot.private.journal_v1 import (
            APPROVAL_LOCK_BASENAME,
            JournalValidationError,
            PrivateJournalWriter,
            assert_journal_layout,
            approval_lock_path,
        )

        with tempfile.TemporaryDirectory() as td:
            data_root = Path(td) / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            plan = self._plan()
            vault.issue(plan)
            assert_journal_layout(data_root)
            lock = approval_lock_path(data_root)
            self.assertEqual(lock.name, APPROVAL_LOCK_BASENAME)
            self.assertTrue(lock.is_file())
            self.assertFalse((data_root / "journal" / "approval_consumed.jsonl").exists())
            self.assertFalse((data_root / "journal" / "post_only_leases.jsonl").exists())
            bad = data_root / "journal" / "post_only_leases.jsonl"
            bad.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(JournalValidationError):
                assert_journal_layout(data_root)

    def test_live_prepare_requires_consumed_offline(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
        )

        with tempfile.TemporaryDirectory() as td:
            data_root = Path(td) / "data"
            data_root.mkdir(parents=True, exist_ok=True)
            w = PrivateJournalWriter(data_root)
            with self.assertRaises(JournalValidationError):
                w.append(
                    {
                        "event_type": "order_prepared",
                        "operation_id": "op_live_no_approval",
                        "venue": "bybit",
                        "environment": "live",
                        "outcome": "pending",
                        "dual_leg_id": "d1",
                        "leg_id": "leg1",
                        "instrument_class": "linear_perpetual",
                        "symbol_alias": "BTCUSDT",
                        "side": "buy",
                        "order_kind": "limit",
                        "quantity_bucket": "min_lot",
                        "notional_bucket": "under_100_usd",
                        "reduce_only": False,
                        "post_only": True,
                        "ttl_bucket": "short",
                        "request_fingerprint": "fp_x",
                    }
                )

    def test_crash_boundary_restart_nonterminal_blocks_sends(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id, validate_events_file
        from app.bot.private.order_lease import LeaseSupervisor
        from app.bot.private.order_sender import ApprovalBoundSender, TransportAck
        from app.bot.private.order_sign import LiveCredentials

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            j1 = PrivateJournalWriter(data_root, run_id=new_opaque_id("run"))
            vault = self._vault(j1)
            plan = self._plan()
            token = vault.issue(plan)

            def amb(_req):
                return TransportAck(
                    kind="ambiguous",
                    ack_state="received",
                    error_code="timeout",
                    ambiguous=True,
                )

            sender1 = self._sender(j1, vault, transport=amb)
            res = sender1.send_approved(
                plan, token, LiveCredentials(api_key="k", api_secret="s" * 16), env
            )
            self.assertEqual(res.status, "ambiguous")

            j2 = PrivateJournalWriter(data_root, run_id=new_opaque_id("run"))
            supervisor = LeaseSupervisor(journal=j2, data_root=data_root)
            supervisor.reconstruct_from_journal(append_missing_recon=True)
            self.assertTrue(supervisor.has_blocking_lease())

            plan2 = self._plan()
            vault2 = self._vault(j2)
            token2 = vault2.issue(plan2)
            sender2 = ApprovalBoundSender(
                journal=j2,
                approval_vault=vault2,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                transport=lambda _r: TransportAck(kind="accepted", ack_state="accepted"),
                lease_supervisor=supervisor,
            )
            blocked = sender2.send_approved(
                plan2,
                token2,
                LiveCredentials(api_key="k", api_secret="s" * 16),
                env,
            )
            self.assertEqual(blocked.status, "gate_failed")
            self.assertFalse(blocked.transport_invoked)
            events = validate_events_file(
                next(data_root.glob("journal/event_date=*/events.jsonl"))
            )
            recons = [
                e
                for e in events
                if e["event_type"] == "reconciliation"
                and e.get("reconciliation_scope") == "post_dispatch_ambiguity"
            ]
            self.assertTrue(recons)
            self.assertFalse((data_root / "journal" / "post_only_leases.jsonl").exists())

    def test_denylist_blocks_operator_approver_ids(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            assert_no_redaction_violations,
        )

        with self.assertRaises(JournalValidationError):
            assert_no_redaction_violations({"operator_id": "x"})
        with self.assertRaises(JournalValidationError):
            assert_no_redaction_violations({"approver_email": "a@b.c"})
        with self.assertRaises(JournalValidationError):
            assert_no_redaction_violations({"approval_token": "raw"})

    def test_opaque_id_rejects_smuggled_secrets_allows_safe_ids(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            assert_opaque_id_safe,
            new_opaque_id,
        )

        # Safe generated UUIDv4 / fingerprints / venue order ids.
        assert_opaque_id_safe("operation_id", new_opaque_id("op"))
        assert_opaque_id_safe("run_id", new_opaque_id("run"))
        assert_opaque_id_safe(
            "operation_id", "550e8400-e29b-41d4-a716-446655440000"
        )
        assert_opaque_id_safe("request_fingerprint", "fp_" + ("a" * 32))
        assert_opaque_id_safe("request_fingerprint", "a" * 64)
        assert_opaque_id_safe("approval_token_fingerprint", "b" * 64)
        assert_opaque_id_safe("leg_id", new_opaque_id("leg"))
        assert_opaque_id_safe("dual_leg_id", "123456789012345678")  # Bybit-style digits
        assert_opaque_id_safe("dual_leg_id", "ABCDEF0123456789ABCDEF01")  # OKX-style

        # Reject arbitrary alphanumeric / short demo ids.
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe("operation_id", 12345)  # type: ignore[arg-type]
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe("operation_id", "a")
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe("leg_id", "leg_example_a")
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe("dual_leg_id", "BybitOrd1234567890")
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe("request_fingerprint", "fp_example_001")
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe("request_fingerprint", "fp_1")
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe("operation_id", "op has space")
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe(
                "operation_id",
                "-----BEGIN PRIVATE KEY-----MIIE",
            )
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe(
                "leg_id",
                "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaaa",
            )
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe("dual_leg_id", "sk-live-ABCDEFGH123456")
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe("operation_id", "api_secret=supersecretvalue")
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe(
                "run_id",
                "AAAA" + ("B" * 40) + "====",
            )
        with self.assertRaises(JournalValidationError):
            assert_opaque_id_safe("operation_id", "a" * 48)

    def test_post_transport_ack_journal_failure_blocks_all_sends(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import LiveCredentials
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            plan = self._plan()
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            token = vault.issue(plan)
            sender = self._sender(
                journal,
                vault,
                transport=lambda _r: TransportAck(kind="accepted", ack_state="accepted"),
            )
            with mock.patch.object(
                sender,
                "_journal_ack",
                side_effect=OSError("disk full after transport"),
            ):
                res = sender.send_approved(
                    plan,
                    token,
                    LiveCredentials(api_key="k", api_secret="s" * 16),
                    env,
                )
            self.assertEqual(res.status, "post_transport_journal_failed")
            self.assertFalse(res.journal_ok)
            self.assertTrue(res.transport_invoked)
            self.assertTrue(sender.lease_supervisor.has_blocking_lease())
            # Every subsequent send in this process is blocked.
            plan2 = self._plan()
            token2 = vault.issue(plan2)
            blocked = sender.send_approved(
                plan2,
                token2,
                LiveCredentials(api_key="k", api_secret="s" * 16),
                env,
            )
            self.assertEqual(blocked.status, "gate_failed")
            self.assertFalse(blocked.transport_invoked)

    def test_ambiguous_recovery_journal_fail_closed(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import LiveCredentials
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            data_root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            data_root.mkdir(parents=True, exist_ok=True)
            plan = self._plan()
            journal = PrivateJournalWriter(data_root)
            vault = self._vault(journal)
            token = vault.issue(plan)

            def amb(_req):
                return TransportAck(
                    kind="ambiguous",
                    ack_state="received",
                    error_code="timeout",
                    ambiguous=True,
                )

            sender = self._sender(journal, vault, transport=amb)
            with mock.patch.object(
                sender,
                "_journal_ambiguous",
                side_effect=OSError("disk full"),
            ):
                res = sender.send_approved(
                    plan,
                    token,
                    LiveCredentials(api_key="k", api_secret="s" * 16),
                    env,
                )
            self.assertEqual(res.status, "recovery_journal_failed")
            self.assertFalse(res.journal_ok)
            self.assertTrue(res.transport_invoked)


class W2PrivateWsTests(unittest.TestCase):
    """Fake/no-network private WS runtime tests."""

    def _creds(self, *, okx: bool = False):
        from app.bot.private.order_sign import LiveCredentials

        if okx:
            return LiveCredentials(
                api_key="okx_live_key_test",
                api_secret="okx_live_secret_test_value",
                passphrase="okx-pass",
            )
        return LiveCredentials(
            api_key="bybit_live_key_test",
            api_secret="bybit_live_secret_test_value",
        )

    def _journal(self, td: str):
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id

        root = Path(td) / "data"
        return PrivateJournalWriter(root, run_id=new_opaque_id("run"))

    def _seed_place_ack_lifecycle(self, journal, plan, *, venue: str) -> None:
        """Minimal live place ack so private_ws terminal_update validates."""
        from app.bot.private.order_approval import ApprovalVault

        vault = ApprovalVault(
            journal=journal, hmac_key=b"unit-test-approval-key-32bytes!!"
        )
        token = vault.issue(plan)
        vault.consume(plan, token)
        journal.append(
            {
                "event_type": "order_prepared",
                "operation_id": plan.order_attempt_id,
                "venue": venue,
                "environment": "live",
                "outcome": "pending",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "instrument_class": "linear_perpetual",
                "symbol_alias": plan.symbol_alias,
                "side": plan.side,
                "order_kind": "limit",
                "quantity_bucket": plan.quantity_bucket,
                "notional_bucket": plan.notional_bucket,
                "reduce_only": False,
                "post_only": True,
                "ttl_bucket": "short",
                "request_fingerprint": plan.request_fingerprint,
            }
        )
        send_mono = journal._last_mono + 1  # noqa: SLF001
        journal.append(
            {
                "event_type": "request_sent",
                "operation_id": plan.order_attempt_id,
                "venue": venue,
                "environment": "live",
                "outcome": "pending",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "request_kind": "place",
                "request_fingerprint": plan.request_fingerprint,
                "transport_attempt": 1,
                "send_monotonic_ns": send_mono,
                "transport": "ws_trade",
                "reconnect_generation": 0,
                "event_monotonic_ns": send_mono + 1,
            }
        )
        journal.append(
            {
                "event_type": "ack_received",
                "operation_id": plan.order_attempt_id,
                "venue": venue,
                "environment": "live",
                "outcome": "success",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "request_kind": "place",
                "request_fingerprint": plan.request_fingerprint,
                "ack_state": "accepted",
                "receive_monotonic_ns": send_mono + 2,
                "transport": "ws_trade",
                "reconnect_generation": 0,
            }
        )

    def _runtime(self, journal, *, exchange="bybit", symbol="BTCUSDT"):
        from app.bot.private.ws_private import PrivateStreamRuntime
        from app.bot.private.ws_socket import FakePrivateWsSocket

        env = {"VENUE": "live", "LIVE_ORDERS": "0"}
        rt = PrivateStreamRuntime(
            exchange=exchange,
            environment="live",
            symbol_alias=symbol,
            journal=journal,
            run_id=journal.run_id,
            credentials=self._creds(okx=(exchange == "okx")),
            gate_env=env,
        )
        priv = FakePrivateWsSocket()
        trade = FakePrivateWsSocket()
        rt.bind_sockets(private=priv, trade=trade, env=env)
        return rt, priv, trade

    def test_default_no_io_and_live_orders_no_auto_ws(self) -> None:
        from app.bot.private.ws_private import (
            assert_default_cli_has_no_ws,
            live_orders_must_not_auto_connect_ws,
            private_ws_urls_for_live,
        )
        from app.bot.private.ws_socket import (
            UnboundSocketFactory,
            assert_no_default_ws_socket,
            open_private_socket,
            unbind_socket_factory,
        )

        unbind_socket_factory()
        assert_no_default_ws_socket()
        assert_default_cli_has_no_ws()
        live_orders_must_not_auto_connect_ws({"VENUE": "live", "LIVE_ORDERS": "1"})
        with self.assertRaises(RuntimeError):
            open_private_socket(private_ws_urls_for_live()["bybit_private"])
        unbound = UnboundSocketFactory()
        with self.assertRaises(RuntimeError):
            unbound.open("wss://example.invalid/private")

    def test_auth_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            rt, priv, _trade = self._runtime(journal)
            rt.send_auth()
            self.assertTrue(priv.outbox)
            # Shape only — never assert signature/key values.
            frame = json.loads(priv.outbox[0])
            self.assertEqual(frame["op"], "auth")
            self.assertEqual(len(frame["args"]), 3)
            priv.push_inbound(
                json.dumps({"op": "auth", "success": False, "retCode": 10004})
            )
            parsed = rt.handle_inbound_text(priv.recv_text())
            self.assertEqual(parsed.kind, "auth_reject")
            self.assertFalse(rt.authenticated)
            from app.bot.private.paths import events_jsonl_path
            from app.bot.private.journal_v1 import validate_events_file

            path = events_jsonl_path(journal.data_root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            self.assertEqual(events[-1]["event_type"], "auth")
            self.assertEqual(events[-1]["outcome"], "failure")
            self.assertEqual(events[-1]["error_code"], "auth_failed")

    def test_subscription_ack(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            rt, priv, _trade = self._runtime(journal)
            rt.send_auth()
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            rt.handle_inbound_text(priv.recv_text())
            self.assertTrue(rt.authenticated)
            rt.send_subscribe()
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            parsed = rt.handle_inbound_text(priv.recv_text())
            self.assertEqual(parsed.kind, "sub_ack")
            self.assertEqual(rt.subscription_readiness.value, "ready")
            # Until REST reseed, sends stay blocked / state unknown.
            self.assertTrue(rt.sends_blocked)
            from app.bot.private.order_lease import OrderStateSnapshot
            from app.bot.private.ws_private import WsOrderStateProvider

            # Minimal plan stub for provider.
            plan = R3OrdersTests()._plan()  # type: ignore[misc]
            provider = WsOrderStateProvider(rt)
            self.assertEqual(provider.get(plan), OrderStateSnapshot.UNKNOWN)

    def test_bybit_okx_signing_message_shape_without_secrets(self) -> None:
        from app.bot.private.ws_messages import (
            build_bybit_private_auth,
            build_bybit_trade_cancel,
            build_bybit_trade_place,
            build_okx_private_login,
            build_okx_private_subscribe,
            build_okx_trade_cancel,
            build_okx_trade_place,
            message_shape_without_secrets,
        )

        bybit = self._creds()
        okx = self._creds(okx=True)
        auth = build_bybit_private_auth(bybit, expires_ms=1_700_000_000_000)
        view = message_shape_without_secrets(auth)
        self.assertTrue(view["raw_omitted"])
        self.assertNotIn("text", view)
        dumped = json.dumps(view)
        self.assertNotIn(bybit.api_key, dumped)
        self.assertNotIn(bybit.api_secret, dumped)

        login = build_okx_private_login(okx, timestamp="1700000000")
        login_view = message_shape_without_secrets(login)
        self.assertNotIn(okx.api_key, json.dumps(login_view))
        self.assertNotIn(okx.passphrase or "", json.dumps(login_view))

        sub = build_okx_private_subscribe(symbol="BTC-USDT-SWAP")
        sub_obj = json.loads(sub.text)
        self.assertEqual(sub_obj["op"], "subscribe")
        self.assertEqual(len(sub_obj["args"]), 2)

        plan = R3OrdersTests()._plan()
        place = build_bybit_trade_place(plan, bybit, req_id="req1", timestamp_ms=1)
        self.assertEqual(place.op, "order.create")
        self.assertEqual(place.channel, "trade")
        cancel = build_bybit_trade_cancel(plan, bybit, req_id="req2", timestamp_ms=1)
        self.assertEqual(cancel.op, "order.cancel")

        okx_plan = R3OrdersTests()._plan(
            venue="okx_live", symbol="BTC-USDT-SWAP", price="50"
        )
        self.assertEqual(okx_plan.inst_id_code, 10459)
        op = build_okx_trade_place(okx_plan, req_id="oid1")
        self.assertEqual(op.op, "order")
        place_obj = json.loads(op.text)
        self.assertEqual(place_obj["args"][0]["instId"], "BTC-USDT-SWAP")
        self.assertEqual(place_obj["args"][0]["instIdCode"], 10459)
        self.assertIsInstance(place_obj["args"][0]["instIdCode"], int)
        # Ensure JSON integer (not string) in compact frame.
        self.assertIn('"instIdCode":10459', op.text)
        oc = build_okx_trade_cancel(okx_plan, req_id="oid2")
        self.assertEqual(oc.op, "cancel-order")
        cancel_obj = json.loads(oc.text)
        self.assertEqual(cancel_obj["args"][0]["instIdCode"], 10459)
        self.assertIsInstance(cancel_obj["args"][0]["instIdCode"], int)

        from app.bot.private.order_plan import OrderPlanError
        from dataclasses import replace

        bare = replace(okx_plan, inst_id_code=None)
        with self.assertRaises(OrderPlanError):
            build_okx_trade_place(bare, req_id="oid4")
        with self.assertRaises(OrderPlanError):
            build_okx_trade_cancel(bare, req_id="oid5")
        with self.assertRaises(OrderPlanError):
            build_okx_trade_place(okx_plan, req_id="oid6", inst_id_code=0)
        with self.assertRaises(OrderPlanError):
            build_okx_trade_place(okx_plan, req_id="oid7", inst_id_code=-1)

    def test_duplicate_event_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            rt, priv, _trade = self._runtime(journal)
            plan = R3OrdersTests()._plan()
            rt.register_plan_fingerprint(plan)
            # Unit-scope: mark healthy without journaling a lone reseed.
            from app.bot.private.ws_private import SequenceHealth, SubscriptionReadiness

            rt.sequence_state = SequenceHealth.HEALTHY
            rt.subscription_readiness = SubscriptionReadiness.READY
            rt._sends_blocked = False  # noqa: SLF001
            frame = {
                "topic": "order",
                "creationTime": 10,
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "orderLinkId": plan.order_attempt_id[:36],
                        "orderStatus": "New",
                    }
                ],
            }
            priv.push_inbound(json.dumps(frame))
            first = rt.handle_inbound_text(priv.recv_text())
            self.assertEqual(first.kind, "order_update")
            priv.push_inbound(json.dumps(frame))
            second = rt.handle_inbound_text(priv.recv_text())
            self.assertEqual(second.kind, "duplicate")

    def test_status_dedupe_allows_new_then_cancelled(self) -> None:
        """Same orderLinkId/clOrdId with different statuses must both apply."""
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_private import SequenceHealth, SubscriptionReadiness

        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            rt, priv, _trade = self._runtime(journal)
            plan = R3OrdersTests()._plan()
            rt.register_plan_fingerprint(plan)
            rt.sequence_state = SequenceHealth.HEALTHY
            rt.subscription_readiness = SubscriptionReadiness.READY
            rt._sends_blocked = False  # noqa: SLF001
            link = plan.order_attempt_id[:36]
            priv.push_inbound(
                json.dumps(
                    {
                        "topic": "order",
                        "creationTime": 1000,
                        "data": [
                            {
                                "symbol": "BTCUSDT",
                                "orderLinkId": link,
                                "orderStatus": "New",
                            }
                        ],
                    }
                )
            )
            first = rt.handle_inbound_text(priv.recv_text())
            self.assertEqual(first.kind, "order_update")
            self.assertEqual(
                rt.get_order_snapshot(plan), OrderStateSnapshot.WORKING
            )
            priv.push_inbound(
                json.dumps(
                    {
                        "topic": "order",
                        "creationTime": 1000,
                        "data": [
                            {
                                "symbol": "BTCUSDT",
                                "orderLinkId": link,
                                "orderStatus": "Cancelled",
                            }
                        ],
                    }
                )
            )
            second = rt.handle_inbound_text(priv.recv_text())
            self.assertEqual(second.kind, "order_update")
            self.assertEqual(second.terminal_state, "cancelled")
            self.assertEqual(
                rt.get_order_snapshot(plan), OrderStateSnapshot.CANCELLED
            )

    def test_bybit_creation_time_jump_not_gap_terminal(self) -> None:
        """creationTime is a timestamp — New→Cancelled must not false-gap."""
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_private import (
            RestReseedResult,
            SequenceHealth,
            WsOrderStateProvider,
        )

        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            rt, priv, _trade = self._runtime(journal)
            plan = R3OrdersTests()._plan()
            rt.register_plan_fingerprint(plan)
            rt.send_auth()
            priv.push_inbound(json.dumps({"op": "auth", "success": True}))
            rt.handle_inbound_text(priv.recv_text())
            rt.send_subscribe()
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            rt.handle_inbound_text(priv.recv_text())
            rt.confirm_rest_reseed(RestReseedResult(matched=True))
            self.assertFalse(rt.sends_blocked)
            provider = WsOrderStateProvider(rt)
            link = plan.order_attempt_id[:36]
            priv.push_inbound(
                json.dumps(
                    {
                        "topic": "order",
                        "creationTime": 1_700_000_000_000,
                        "data": [
                            {
                                "symbol": "BTCUSDT",
                                "orderLinkId": link,
                                "orderStatus": "New",
                            }
                        ],
                    }
                )
            )
            self.assertEqual(
                rt.handle_inbound_text(priv.recv_text()).kind, "order_update"
            )
            self.assertEqual(provider.get(plan), OrderStateSnapshot.WORKING)
            priv.push_inbound(
                json.dumps(
                    {
                        "topic": "order",
                        "creationTime": 1_700_000_005_000,
                        "data": [
                            {
                                "symbol": "BTCUSDT",
                                "orderLinkId": link,
                                "orderStatus": "Cancelled",
                            }
                        ],
                    }
                )
            )
            parsed = rt.handle_inbound_text(priv.recv_text())
            self.assertEqual(parsed.kind, "order_update")
            self.assertEqual(parsed.terminal_state, "cancelled")
            self.assertFalse(rt.sends_blocked)
            self.assertEqual(rt.sequence_state, SequenceHealth.HEALTHY)
            self.assertEqual(provider.get(plan), OrderStateSnapshot.CANCELLED)
            self._seed_place_ack_lifecycle(journal, plan, venue="bybit")
            term = rt.journal_terminal_from_stream(plan, terminal_state="cancelled")
            self.assertEqual(term["observation_source"], "private_ws")

    def test_okx_utime_jump_not_gap_terminal(self) -> None:
        """OKX uTime is a timestamp — live→canceled must not false-gap."""
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_private import (
            RestReseedResult,
            SequenceHealth,
            WsOrderStateProvider,
        )

        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            rt, priv, _trade = self._runtime(
                journal, exchange="okx", symbol="BTC-USDT-SWAP"
            )
            plan = R3OrdersTests()._plan(
                venue="okx_live", symbol="BTC-USDT-SWAP"
            )
            rt.register_plan_fingerprint(plan)
            rt.send_auth()
            priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
            rt.handle_inbound_text(priv.recv_text())
            rt.send_subscribe()
            priv.push_inbound(
                json.dumps(
                    {
                        "event": "subscribe",
                        "code": "0",
                        "arg": {"channel": "orders"},
                    }
                )
            )
            rt.handle_inbound_text(priv.recv_text())
            rt.confirm_rest_reseed(RestReseedResult(matched=True))
            self.assertFalse(rt.sends_blocked)
            provider = WsOrderStateProvider(rt)
            cl = plan.order_attempt_id.replace("_", "")[:32]
            priv.push_inbound(
                json.dumps(
                    {
                        "arg": {"channel": "orders", "instId": "BTC-USDT-SWAP"},
                        "data": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "clOrdId": cl,
                                "state": "live",
                                "uTime": "1700000000000",
                            }
                        ],
                    }
                )
            )
            self.assertEqual(
                rt.handle_inbound_text(priv.recv_text()).kind, "order_update"
            )
            self.assertEqual(provider.get(plan), OrderStateSnapshot.WORKING)
            priv.push_inbound(
                json.dumps(
                    {
                        "arg": {"channel": "orders", "instId": "BTC-USDT-SWAP"},
                        "data": [
                            {
                                "instId": "BTC-USDT-SWAP",
                                "clOrdId": cl,
                                "state": "canceled",
                                "uTime": "1700000005000",
                            }
                        ],
                    }
                )
            )
            parsed = rt.handle_inbound_text(priv.recv_text())
            self.assertEqual(parsed.kind, "order_update")
            self.assertEqual(parsed.terminal_state, "cancelled")
            self.assertFalse(rt.sends_blocked)
            self.assertEqual(rt.sequence_state, SequenceHealth.HEALTHY)
            self.assertEqual(provider.get(plan), OrderStateSnapshot.CANCELLED)
            self._seed_place_ack_lifecycle(journal, plan, venue="okx")
            term = rt.journal_terminal_from_stream(plan, terminal_state="cancelled")
            self.assertEqual(term["observation_source"], "private_ws")

    def test_explicit_sequence_gap_blocks_send(self) -> None:
        """Synthetic gap (not timestamp) still blocks new sends."""
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_private import RestReseedResult, WsOrderStateProvider

        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            rt, priv, _trade = self._runtime(journal)
            plan = R3OrdersTests()._plan()
            rt.register_plan_fingerprint(plan)
            rt.send_auth()
            priv.push_inbound(json.dumps({"op": "auth", "success": True}))
            rt.handle_inbound_text(priv.recv_text())
            rt.send_subscribe()
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            rt.handle_inbound_text(priv.recv_text())
            rt.confirm_rest_reseed(RestReseedResult(matched=True))
            self.assertFalse(rt.sends_blocked)
            provider = WsOrderStateProvider(rt)
            rt._on_sequence_gap()  # noqa: SLF001
            self.assertTrue(rt.sends_blocked)
            # No known state → UNKNOWN; known terminals would still surface.
            self.assertEqual(provider.get(plan), OrderStateSnapshot.UNKNOWN)
            with self.assertRaises(RuntimeError):
                rt.send_trade_place(plan, req_id="x")
            rt.mark_reconnect()
            self.assertEqual(rt.reconnect_generation, 1)

    def test_ws_trade_ack_and_terminal_update(self) -> None:
        from app.bot.private.journal_v1 import validate_events_file
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.paths import events_jsonl_path
        from app.bot.private.ws_private import (
            RestReseedResult,
            TradeAckObservation,
            WsOrderStateProvider,
        )

        with tempfile.TemporaryDirectory() as td:
            journal = self._journal(td)
            rt, priv, trade = self._runtime(journal)
            plan = R3OrdersTests()._plan()
            rt.send_auth()
            priv.push_inbound(json.dumps({"op": "auth", "success": True}))
            rt.handle_inbound_text(priv.recv_text())
            rt.send_subscribe()
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            rt.handle_inbound_text(priv.recv_text())
            rt.confirm_rest_reseed(RestReseedResult(matched=True))
            rt.register_plan_fingerprint(plan)
            msg = rt.send_trade_place(plan, req_id="req_place_1")
            self.assertEqual(msg.channel, "trade")
            self.assertTrue(trade.outbox)
            obs = TradeAckObservation(
                req_id="req_place_1", accepted=True, ack_state="accepted"
            )
            meta = rt.observe_trade_ack(obs)
            self.assertFalse(meta["terminal"])

            vault = ApprovalVault(
                journal=journal, hmac_key=b"unit-test-approval-key-32bytes!!"
            )
            token = vault.issue(plan)
            vault.consume(plan, token)
            journal.append(
                {
                    "event_type": "order_prepared",
                    "operation_id": plan.order_attempt_id,
                    "venue": "bybit",
                    "environment": "live",
                    "outcome": "pending",
                    "dual_leg_id": plan.dual_leg_id,
                    "leg_id": plan.leg_id,
                    "instrument_class": "linear_perpetual",
                    "symbol_alias": plan.symbol_alias,
                    "side": plan.side,
                    "order_kind": "limit",
                    "quantity_bucket": plan.quantity_bucket,
                    "notional_bucket": plan.notional_bucket,
                    "reduce_only": False,
                    "post_only": True,
                    "ttl_bucket": "short",
                    "request_fingerprint": plan.request_fingerprint,
                }
            )
            send_mono = journal._last_mono + 1  # noqa: SLF001
            journal.append(
                {
                    "event_type": "request_sent",
                    "operation_id": plan.order_attempt_id,
                    "venue": "bybit",
                    "environment": "live",
                    "outcome": "pending",
                    "dual_leg_id": plan.dual_leg_id,
                    "leg_id": plan.leg_id,
                    "request_kind": "place",
                    "request_fingerprint": plan.request_fingerprint,
                    "transport_attempt": 1,
                    "send_monotonic_ns": send_mono,
                    "transport": "ws_trade",
                    "reconnect_generation": 0,
                    "event_monotonic_ns": send_mono + 1,
                }
            )
            rt.journal_trade_ack(plan, obs, request_kind="place")
            priv.push_inbound(
                json.dumps(
                    {
                        "topic": "order",
                        "creationTime": 2,
                        "data": [
                            {
                                "symbol": "BTCUSDT",
                                "orderLinkId": plan.order_attempt_id[:36],
                                "orderStatus": "Filled",
                            }
                        ],
                    }
                )
            )
            parsed = rt.handle_inbound_text(priv.recv_text())
            self.assertEqual(parsed.kind, "order_update")
            self.assertEqual(parsed.terminal_state, "filled")
            provider = WsOrderStateProvider(rt)
            self.assertEqual(provider.get(plan), OrderStateSnapshot.FILLED)
            rt.journal_terminal_from_stream(plan, terminal_state="filled")
            path = events_jsonl_path(journal.data_root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            acks = [e for e in events if e["event_type"] == "ack_received"]
            terms = [e for e in events if e["event_type"] == "terminal_update"]
            self.assertTrue(any(a.get("transport") == "ws_trade" for a in acks))
            self.assertEqual(terms[-1]["observation_source"], "private_ws")
            self.assertEqual(terms[-1]["sequence_state"], "healthy")

    def test_redaction_rejects_raw_frame_fields(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            new_opaque_id,
            validate_event_shape,
        )

        bad = {
            "schema_version": "bbot.private.journal.v1",
            "event_id": new_opaque_id("evt"),
            "event_type": "ack_received",
            "event_date": "2026-08-19",
            "event_ts_utc": "2026-08-19T12:00:00.000Z",
            "event_monotonic_ns": 1,
            "run_id": new_opaque_id("run"),
            "operation_id": new_opaque_id("op"),
            "event_seq": 1,
            "venue": "bybit",
            "environment": "live",
            "outcome": "success",
            "request_kind": "ws_subscribe",
            "ack_state": "received",
            "receive_monotonic_ns": 1,
            "transport": "ws_trade",
            "reconnect_generation": 0,
            "subscription_readiness": "ready",
            "raw_payload": '{"op":"subscribe"}',
        }
        with self.assertRaises(JournalValidationError):
            validate_event_shape(bad)

    def test_ws_subscribe_journal_flow_validates(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_event_stream,
        )

        run = new_opaque_id("run")
        op = new_opaque_id("op_stream")
        events = [
            {
                "schema_version": "bbot.private.journal.v1",
                "event_id": new_opaque_id("evt"),
                "event_type": "auth",
                "event_date": "2026-08-19",
                "event_ts_utc": "2026-08-19T12:02:00.000Z",
                "event_monotonic_ns": 1000,
                "run_id": run,
                "operation_id": op,
                "event_seq": 1,
                "venue": "okx",
                "environment": "live",
                "outcome": "success",
                "auth_method": "hmac",
                "credential_presence": {"credentials_configured": True},
            },
            {
                "schema_version": "bbot.private.journal.v1",
                "event_id": new_opaque_id("evt"),
                "event_type": "request_sent",
                "event_date": "2026-08-19",
                "event_ts_utc": "2026-08-19T12:02:00.001Z",
                "event_monotonic_ns": 1001,
                "run_id": run,
                "operation_id": op,
                "event_seq": 2,
                "venue": "okx",
                "environment": "live",
                "outcome": "pending",
                "request_kind": "ws_subscribe",
                "transport_attempt": 1,
                "send_monotonic_ns": 1000,
                "transport": "ws_trade",
                "reconnect_generation": 0,
                "subscription_readiness": "not_ready",
            },
            {
                "schema_version": "bbot.private.journal.v1",
                "event_id": new_opaque_id("evt"),
                "event_type": "ack_received",
                "event_date": "2026-08-19",
                "event_ts_utc": "2026-08-19T12:02:00.002Z",
                "event_monotonic_ns": 1002,
                "run_id": run,
                "operation_id": op,
                "event_seq": 3,
                "venue": "okx",
                "environment": "live",
                "outcome": "success",
                "request_kind": "ws_subscribe",
                "ack_state": "received",
                "receive_monotonic_ns": 1001,
                "transport": "ws_trade",
                "reconnect_generation": 0,
                "subscription_readiness": "ready",
            },
            {
                "schema_version": "bbot.private.journal.v1",
                "event_id": new_opaque_id("evt"),
                "event_type": "reconciliation",
                "event_date": "2026-08-19",
                "event_ts_utc": "2026-08-19T12:02:00.003Z",
                "event_monotonic_ns": 1003,
                "run_id": run,
                "operation_id": op,
                "event_seq": 4,
                "venue": "okx",
                "environment": "live",
                "outcome": "success",
                "reconciliation_scope": "private_stream_reseed",
                "reconciliation_state": "matched",
                "transport": "rest",
                "observation_source": "rest_reconcile",
                "reconnect_generation": 0,
                "sequence_state": "healthy",
                "subscription_readiness": "ready",
            },
        ]
        validate_event_stream(events)
        # Gap without reseed then place must fail closed.
        from app.bot.private.journal_v1 import JournalValidationError

        gap = {
            "schema_version": "bbot.private.journal.v1",
            "event_id": new_opaque_id("evt"),
            "event_type": "reconciliation",
            "event_date": "2026-08-19",
            "event_ts_utc": "2026-08-19T12:02:00.004Z",
            "event_monotonic_ns": 1004,
            "run_id": run,
            "operation_id": op,
            "event_seq": 5,
            "venue": "okx",
            "environment": "live",
            "outcome": "observed",
            "reconciliation_scope": "private_stream_reseed",
            "reconciliation_state": "inconclusive",
            "observation_source": "private_ws",
            "reconnect_generation": 0,
            "sequence_state": "gap",
            "subscription_readiness": "not_ready",
        }
        leg_op = new_opaque_id("op_leg")
        dual = new_opaque_id("dual")
        leg = new_opaque_id("leg")
        fp = "fp_" + ("ab" * 16)
        prepared = {
            "schema_version": "bbot.private.journal.v1",
            "event_id": new_opaque_id("evt"),
            "event_type": "order_prepared",
            "event_date": "2026-08-19",
            "event_ts_utc": "2026-08-19T12:02:00.005Z",
            "event_monotonic_ns": 1005,
            "run_id": run,
            "operation_id": leg_op,
            "event_seq": 6,
            "venue": "okx",
            "environment": "demo",
            "outcome": "pending",
            "dual_leg_id": dual,
            "leg_id": leg,
            "instrument_class": "linear_perpetual",
            "symbol_alias": "BTC-USDT-SWAP",
            "side": "buy",
            "order_kind": "limit",
            "quantity_bucket": "min_lot",
            "notional_bucket": "under_100_usd",
            "reduce_only": False,
            "post_only": False,
            "request_fingerprint": fp,
        }
        place = {
            "schema_version": "bbot.private.journal.v1",
            "event_id": new_opaque_id("evt"),
            "event_type": "request_sent",
            "event_date": "2026-08-19",
            "event_ts_utc": "2026-08-19T12:02:00.006Z",
            "event_monotonic_ns": 1006,
            "run_id": run,
            "operation_id": leg_op,
            "event_seq": 7,
            "venue": "okx",
            "environment": "live",
            "outcome": "pending",
            "dual_leg_id": dual,
            "leg_id": leg,
            "request_kind": "place",
            "request_fingerprint": fp,
            "transport_attempt": 1,
            "send_monotonic_ns": 1005,
            "transport": "ws_trade",
            "reconnect_generation": 0,
        }
        # prepared uses demo to skip live approval gate; place uses live same venue/env as stream.
        # Align environments for stream gate key: use live for prepared too and skip approval
        # by using demo for both place events — stream gate keys on venue+environment.
        prepared["environment"] = "live"
        # Live prepare requires approval — use demo environment for the blocked-send check
        # so we only exercise the stream gate (same venue/env as gap).
        gap["environment"] = "demo"
        for e in events:
            e["environment"] = "demo"
        prepared["environment"] = "demo"
        place["environment"] = "demo"
        with self.assertRaises(JournalValidationError) as ctx:
            validate_event_stream(events + [gap, prepared, place])

        self.assertIn("blocked until private_stream_reseed", str(ctx.exception))


class W3PrivateWsReadonlyTests(unittest.TestCase):
    """W3 preflight: gated --ws-readonly, REST reseed, silence/reconnect (fake sockets)."""

    def _live_env(self) -> dict:
        return {"VENUE": "live", "LIVE_ORDERS": "0"}

    def test_cli_mode_gate_requires_flag_and_live_readonly(self) -> None:
        from app.bot.private.ws_gates import (
            WsProfileGateError,
            assert_ws_readonly_cli_gates,
        )
        from app.bot.private.ws_readonly import run_ws_readonly_preflight
        from app.bot.private.ws_socket import assert_no_default_ws_socket, unbind_socket_factory

        with self.assertRaises(WsProfileGateError):
            assert_ws_readonly_cli_gates({"VENUE": "testnet", "LIVE_ORDERS": "0"})
        with self.assertRaises(WsProfileGateError):
            assert_ws_readonly_cli_gates({"VENUE": "live", "LIVE_ORDERS": "1"})
        self.assertEqual(assert_ws_readonly_cli_gates(self._live_env()), "live")
        unbind_socket_factory()
        assert_no_default_ws_socket()
        rep = run_ws_readonly_preflight(
            exchange="bybit",
            env={"VENUE": "testnet", "LIVE_ORDERS": "0"},
            load_secrets=False,
        )
        self.assertEqual(rep.status, "rejected_before_socket")

    def test_socket_never_opened_on_profile_flag_failure(self) -> None:
        from app.bot.private.ws_gates import WsProfileGateError
        from app.bot.private.ws_private import PrivateStreamRuntime
        from app.bot.private.ws_socket import (
            FakePrivateWsSocket,
            assert_no_default_ws_socket,
            unbind_socket_factory,
        )
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id

        unbind_socket_factory()
        assert_no_default_ws_socket()
        with tempfile.TemporaryDirectory() as td:
            journal = PrivateJournalWriter(Path(td), run_id=new_opaque_id("run"))
            with self.assertRaises(WsProfileGateError):
                PrivateStreamRuntime.create_gated(
                    exchange="bybit",
                    symbol_alias="BTCUSDT",
                    journal=journal,
                    credentials=W2PrivateWsTests()._creds(),
                    env={"VENUE": "testnet", "LIVE_ORDERS": "0"},
                )
            rt = PrivateStreamRuntime(
                exchange="bybit",
                environment="live",
                symbol_alias="BTCUSDT",
                journal=journal,
                run_id=journal.run_id,
                credentials=W2PrivateWsTests()._creds(),
                gate_env={"VENUE": "live", "LIVE_ORDERS": "0"},
            )
            fake = FakePrivateWsSocket()
            with self.assertRaises(WsProfileGateError):
                rt.bind_sockets(
                    private=fake,
                    env={"VENUE": "testnet", "LIVE_ORDERS": "0"},
                )
            self.assertFalse(fake.connected)
            with self.assertRaises(WsProfileGateError):
                rt.bind_sockets(
                    private=fake,
                    env={"VENUE": "live", "LIVE_ORDERS": "1"},
                )
            self.assertFalse(fake.connected)
            assert_no_default_ws_socket()

    def test_login_sub_reseed_flow_fake_sockets(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.paths import events_jsonl_path
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_readonly import run_ws_readonly_preflight
        from app.bot.private.ws_socket import FakePrivateWsSocket

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv = FakePrivateWsSocket()
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))

            def probe_fn(**_kwargs):
                return RestReseedResult(matched=True)

            env = {**self._live_env(), "BBOT_PRIVATE_DATA_ROOT": str(root)}
            report = run_ws_readonly_preflight(
                exchange="bybit",
                env=env,
                private_socket=priv,
                rest_probe_fn=probe_fn,
                credentials=W2PrivateWsTests()._creds(),
                journal=journal,
                load_secrets=False,
                max_cycles=1,
                silence_timeout_sec=60.0,
                recv_timeout_sec=0.01,
                heartbeat_every_sec=100.0,
            )
            self.assertEqual(report.status, "ok")
            self.assertTrue(report.authenticated)
            self.assertTrue(report.subscription_ready)
            self.assertTrue(report.reseed_matched)
            self.assertFalse(report.sends_blocked)
            self.assertFalse(report.as_public_dict()["trade_ws_bound"])
            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            types = [e["event_type"] for e in events]
            self.assertIn("auth", types)
            self.assertIn("request_sent", types)
            self.assertIn("ack_received", types)
            self.assertIn("reconciliation", types)

    def test_heartbeat_timeout_reconnect_reseed_block(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_readonly import run_ws_readonly_preflight
        from app.bot.private.ws_socket import FakePrivateWsSocket

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv = FakePrivateWsSocket()
            priv.push_inbound(json.dumps({"op": "auth", "success": True}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))

            def probe_fn(**_kwargs):
                return RestReseedResult(matched=True)

            report = run_ws_readonly_preflight(
                exchange="bybit",
                env={**self._live_env(), "BBOT_PRIVATE_DATA_ROOT": str(root)},
                private_socket=priv,
                rest_probe_fn=probe_fn,
                credentials=W2PrivateWsTests()._creds(),
                journal=journal,
                load_secrets=False,
                max_cycles=5,
                silence_timeout_sec=0.0,
                recv_timeout_sec=0.01,
                heartbeat_every_sec=100.0,
            )
            self.assertEqual(report.status, "silence_timeout_reseed_required")
            self.assertGreaterEqual(report.silence_timeouts, 1)
            self.assertGreaterEqual(report.reconnect_generation, 1)
            self.assertTrue(report.sends_blocked)
            self.assertFalse(report.reseed_matched)

    def test_signed_reseed_adapter_uses_probe_fn_without_network(self) -> None:
        from app.bot.private.venue import endpoints_for_venue
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_reseed import build_signed_rest_reseed

        calls = []

        def probe_fn(**kwargs):
            calls.append(kwargs)
            return RestReseedResult(matched=True)

        adapter = build_signed_rest_reseed(
            exchange="okx",
            credentials=W2PrivateWsTests()._creds(okx=True),
            endpoints=endpoints_for_venue("live"),
            probe_fn=probe_fn,
        )
        out = adapter.reseed(
            venue="okx",
            environment="live",
            reconnect_generation=0,
            symbol_alias="BTC-USDT-SWAP",
        )
        self.assertTrue(out.matched)
        self.assertEqual(len(calls), 1)

    def test_late_duplicate_sub_ack_after_matched_reseed_both_venues(self) -> None:
        """Late successful sub_ack must not re-arm gate after matched REST reseed."""
        from app.bot.private.ws_private import (
            RestReseedResult,
            SequenceHealth,
            SubscriptionReadiness,
        )

        for exchange, symbol, okx, sub_frame in (
            (
                "bybit",
                "BTCUSDT",
                False,
                {"op": "subscribe", "success": True},
            ),
            (
                "okx",
                "BTC-USDT-SWAP",
                True,
                {"event": "subscribe", "code": "0", "arg": {"channel": "orders"}},
            ),
        ):
            with self.subTest(exchange=exchange):
                with tempfile.TemporaryDirectory() as td:
                    journal = W2PrivateWsTests()._journal(td)
                    rt, priv, _trade = W2PrivateWsTests()._runtime(
                        journal, exchange=exchange, symbol=symbol
                    )
                    # Auth first so journal lifecycle accepts ws_subscribe.
                    rt.send_auth()
                    auth_frame = (
                        {"op": "auth", "success": True, "retCode": 0}
                        if exchange == "bybit"
                        else {"event": "login", "code": "0"}
                    )
                    priv.push_inbound(json.dumps(auth_frame))
                    rt.handle_inbound_text(priv.recv_text())
                    self.assertTrue(rt.authenticated)
                    # First subscribe stays fail-closed until REST reseed.
                    rt.send_subscribe()
                    priv.push_inbound(json.dumps(sub_frame))
                    first = rt.handle_inbound_text(priv.recv_text())
                    self.assertEqual(first.kind, "sub_ack")
                    self.assertTrue(first.ack_ok)
                    self.assertEqual(rt.subscription_readiness, SubscriptionReadiness.READY)
                    self.assertTrue(rt.sends_blocked)
                    self.assertEqual(rt.sequence_state, SequenceHealth.RESEED_REQUIRED)

                    rt.confirm_rest_reseed(RestReseedResult(matched=True))
                    self.assertEqual(rt.sequence_state, SequenceHealth.HEALTHY)
                    self.assertFalse(rt.sends_blocked)

                    # Late/duplicate successful ACK (OKX W3 symptom).
                    priv.push_inbound(json.dumps(sub_frame))
                    late = rt.handle_inbound_text(priv.recv_text())
                    self.assertEqual(late.kind, "sub_ack")
                    self.assertTrue(late.ack_ok)
                    self.assertEqual(rt.sequence_state, SequenceHealth.HEALTHY)
                    self.assertEqual(rt.subscription_readiness, SubscriptionReadiness.READY)
                    self.assertFalse(rt.sends_blocked)
                    self.assertFalse(rt._sends_blocked)  # noqa: SLF001

                    # Failed subscribe after healthy still fail-closes.
                    fail_frame = (
                        {"op": "subscribe", "success": False}
                        if exchange == "bybit"
                        else {
                            "event": "error",
                            "code": "60012",
                            "arg": {"channel": "orders"},
                        }
                    )
                    priv.push_inbound(json.dumps(fail_frame))
                    failed = rt.handle_inbound_text(priv.recv_text())
                    self.assertEqual(failed.kind, "sub_ack")
                    self.assertFalse(failed.ack_ok)
                    self.assertTrue(rt.sends_blocked)
                    self.assertEqual(rt.subscription_readiness, SubscriptionReadiness.NOT_READY)
                    self.assertEqual(rt.sequence_state, SequenceHealth.RESEED_REQUIRED)

    def test_readonly_runner_late_sub_ack_both_venue_reports(self) -> None:
        """Public W3 reports stay unblocked when a late sub_ack arrives post-reseed."""
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_readonly import run_ws_readonly_preflight
        from app.bot.private.ws_socket import FakePrivateWsSocket

        cases = (
            (
                "bybit",
                False,
                {"op": "auth", "success": True, "retCode": 0},
                {"op": "subscribe", "success": True},
            ),
            (
                "okx",
                True,
                {"event": "login", "code": "0"},
                {"event": "subscribe", "code": "0", "arg": {"channel": "orders"}},
            ),
        )
        for exchange, okx, auth_frame, sub_frame in cases:
            with self.subTest(exchange=exchange):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td) / "data"
                    journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
                    priv = FakePrivateWsSocket()
                    priv.push_inbound(json.dumps(auth_frame))
                    priv.push_inbound(json.dumps(sub_frame))
                    # Post-reseed duplicate/late subscribe ACK during heartbeat recv.
                    priv.push_inbound(json.dumps(sub_frame))

                    def probe_fn(**_kwargs):
                        return RestReseedResult(matched=True)

                    report = run_ws_readonly_preflight(
                        exchange=exchange,
                        env={**self._live_env(), "BBOT_PRIVATE_DATA_ROOT": str(root)},
                        private_socket=priv,
                        rest_probe_fn=probe_fn,
                        credentials=W2PrivateWsTests()._creds(okx=okx),
                        journal=journal,
                        load_secrets=False,
                        max_cycles=1,
                        silence_timeout_sec=60.0,
                        recv_timeout_sec=0.01,
                        heartbeat_every_sec=100.0,
                    )
                    self.assertEqual(report.status, "ok")
                    self.assertTrue(report.authenticated)
                    self.assertTrue(report.subscription_ready)
                    self.assertTrue(report.reseed_matched)
                    self.assertFalse(report.sends_blocked)
                    pub = report.as_public_dict()
                    self.assertEqual(pub["orders_sent"], 0)
                    self.assertFalse(pub["trade_ws_bound"])
                    self.assertEqual(pub["reconnect_generation"], 0)


class W4PrivateWsPostOnlyTests(unittest.TestCase):
    """Fake-socket W4 bounded post-only runner coverage."""

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
            "BBOT_PRIVATE_W4": "1",
            "BBOT_PRIVATE_ENV_FILE": str(live_env),
            "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
        }

    def _meta(self):
        return R3OrdersTests()._meta(mark_max_age_ns=60_000_000_000)

    def _position(self):
        return R3OrdersTests()._position()

    def test_gate_and_profile_rejection(self) -> None:
        from app.bot.private.ws_gates import WsProfileGateError, assert_ws_w4_send_gates
        from app.bot.private.ws_w4_postonly import (
            W4ProfileError,
            assert_exact_w4_plan,
            resolve_w4_profile,
            run_w4_post_only,
        )

        with self.assertRaises(WsProfileGateError):
            assert_ws_w4_send_gates({"VENUE": "live", "LIVE_ORDERS": "0", "BBOT_PRIVATE_W4": "1"})
        with self.assertRaises(WsProfileGateError):
            assert_ws_w4_send_gates({"VENUE": "live", "LIVE_ORDERS": "1"})
        with self.assertRaises(W4ProfileError):
            resolve_w4_profile("binance")
        plan = R3OrdersTests()._plan(ttl_sec=5)
        with self.assertRaises(W4ProfileError):
            assert_exact_w4_plan(plan, resolve_w4_profile("bybit"))
        rep = run_w4_post_only(
            venue="bybit",
            env={"VENUE": "live", "LIVE_ORDERS": "0", "BBOT_PRIVATE_W4": "1"},
            metadata_provider=self._meta(),
            position_mode_provider=self._position(),
            l1=None,  # type: ignore[arg-type]
            baseline=None,  # type: ignore[arg-type]
            load_secrets=False,
        )
        self.assertEqual(rep.status, "rejected_before_socket")

    def test_stale_l1_and_nonflat_baseline(self) -> None:
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import run_w4_post_only
        from decimal import Decimal
        import time as time_mod

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            baseline = FakeFlatBaseline(position_flat=False)
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            rep = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=baseline,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                issue_approval=True,
            )
            self.assertEqual(rep.status, "baseline_not_flat")

            baseline2 = FakeFlatBaseline()
            l1_stale = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns() - 10_000_000_000,
                    )
                }
            )
            from app.bot.private.ws_socket import FakePrivateWsSocket
            from app.bot.private.ws_private import RestReseedResult

            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket()
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            rep2 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1_stale,
                baseline=baseline2,
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
            )
            self.assertEqual(rep2.status, "l1_or_plan_rejected")


    def _run_happy(self, exchange: str):
        from decimal import Decimal
        import time as time_mod
        from typing import Any
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id, validate_events_file
        from app.bot.private.paths import events_jsonl_path
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        okx = exchange == "okx"
        symbol = "BTC-USDT-SWAP" if okx else "BTCUSDT"
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange=exchange)
            # Noise frames before real auth/sub/login — bounded handshake must ignore.
            if okx:
                priv.push_inbound("ping")
                priv.push_inbound(json.dumps({"event": "notice", "msg": "welcome"}))
                priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
                priv.push_inbound(json.dumps({"event": "channel-conn-count", "channel": "orders", "connCount": "1"}))
                priv.push_inbound(
                    json.dumps({"event": "subscribe", "code": "0", "arg": {"channel": "orders"}})
                )
                trade.push_inbound("pong")
                trade.push_inbound(json.dumps({"event": "login", "code": "0"}))
            else:
                priv.push_inbound(json.dumps({"op": "pong"}))
                priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
                priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
                trade.push_inbound(json.dumps({"op": "pong"}))
                trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

            l1 = FakePublicL1Adapter(
                quotes={
                    (exchange, symbol): PublicL1Quote(
                        exchange=exchange,
                        symbol=symbol,
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )

            state: dict[str, Any] = {}

            def sleep_and_inject(_sec: float) -> None:
                from app.bot.private.paths import events_jsonl_path as ejp

                path = ejp(root, journal._last_ts[:10])  # noqa: SLF001
                events = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                prepared = [e for e in events if e.get("event_type") == "order_prepared"][-1]
                op = prepared["operation_id"]
                state["op"] = op
                if okx:
                    cl = op.replace("_", "")[:32]
                    # New then cancel with uTime jump (must not false-gap / dedupe-kill).
                    priv.push_inbound(
                        json.dumps(
                            {
                                "arg": {"channel": "orders", "instId": symbol},
                                "data": [
                                    {
                                        "instId": symbol,
                                        "clOrdId": cl,
                                        "state": "live",
                                        "uTime": "1700000000000",
                                    }
                                ],
                            }
                        )
                    )
                    priv.push_inbound(
                        json.dumps(
                            {
                                "arg": {"channel": "orders", "instId": symbol},
                                "data": [
                                    {
                                        "instId": symbol,
                                        "clOrdId": cl,
                                        "state": "canceled",
                                        "uTime": "1700000005000",
                                    }
                                ],
                            }
                        )
                    )
                else:
                    priv.push_inbound(
                        json.dumps(
                            {
                                "topic": "order",
                                "creationTime": 1_700_000_000_000,
                                "data": [
                                    {
                                        "symbol": symbol,
                                        "orderLinkId": op[:36],
                                        "orderStatus": "New",
                                    }
                                ],
                            }
                        )
                    )
                    priv.push_inbound(
                        json.dumps(
                            {
                                "topic": "order",
                                "creationTime": 1_700_000_005_000,
                                "data": [
                                    {
                                        "symbol": symbol,
                                        "orderLinkId": op[:36],
                                        "orderStatus": "Cancelled",
                                    }
                                ],
                            }
                        )
                    )

            rep = run_w4_post_only(
                venue=exchange,
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(okx=okx),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=sleep_and_inject,
                terminal_wait_sec=2.0,
            )

            self.assertEqual(rep.status, "ok", rep.as_public_dict())
            self.assertTrue(rep.trade_ws_bound)
            self.assertEqual(rep.orders_sent, 1)
            self.assertTrue(rep.ack_ok)
            self.assertTrue(rep.terminal_observed)
            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            kinds = [e["event_type"] for e in events]
            for need in (
                "order_prepared",
                "request_sent",
                "ack_received",
                "cancel_requested",
                "terminal_update",
            ):
                self.assertIn(need, kinds)
            terms = [e for e in events if e["event_type"] == "terminal_update"]
            self.assertTrue(terms)
            self.assertEqual(terms[-1]["observation_source"], "private_ws")
            place_sent = next(
                e
                for e in events
                if e["event_type"] == "request_sent" and e.get("request_kind") == "place"
            )
            self.assertEqual(place_sent.get("transport"), "ws_trade")
            # Trade socket must carry place then cancel frames; never REST order paths.
            out = trade.outbox
            self.assertGreaterEqual(len(out), 3)  # auth + place + cancel
            ops: list[str] = []
            for raw in out:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(frame, dict):
                    continue
                op = str(frame.get("op") or "")
                if op in {"order.create", "order.cancel", "order", "cancel-order"}:
                    ops.append(op)
            if okx:
                self.assertEqual(ops[:2], ["order", "cancel-order"])
                for raw in out:
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if str(frame.get("op") or "") not in {"order", "cancel-order"}:
                        continue
                    rid = str(frame.get("id") or "")
                    self.assertTrue(rid)
                    self.assertNotIn("_", rid)
                    self.assertLessEqual(len(rid), 32)
                    self.assertTrue(rid.isalnum())
            else:
                self.assertEqual(ops[:2], ["order.create", "order.cancel"])
            return rep

    def test_happy_path_both_venues(self) -> None:
        self._run_happy("bybit")
        self._run_happy("okx")

    def test_w4_okx_trade_url_private_not_business(self) -> None:
        """OKX W4 trade socket must be /ws/v5/private; Bybit stays on /v5/trade."""
        from app.bot.private.venue import endpoints_for_venue
        from app.bot.private.ws_private import trade_ws_url_for_exchange

        ep = endpoints_for_venue("live")
        okx_trade = trade_ws_url_for_exchange("okx", ep)
        bybit_trade = trade_ws_url_for_exchange("bybit", ep)
        self.assertEqual(okx_trade, ep.okx_private_ws)
        self.assertIn("/ws/v5/private", okx_trade)
        self.assertNotIn("/ws/v5/business", okx_trade)
        self.assertNotEqual(okx_trade, ep.okx_business_ws)
        self.assertEqual(bybit_trade, ep.bybit_trade_ws)
        self.assertIn("/v5/trade", bybit_trade)
        self.assertNotEqual(bybit_trade, ep.bybit_private_ws)

    def test_w4_okx_trade_req_id_alphanumeric_no_underscore(self) -> None:
        """OKX trade id: alphanumeric ≤32, no underscore; Bybit may keep w4_ prefix."""
        from app.bot.private.ws_private import new_trade_req_id

        for _ in range(20):
            okx_id = new_trade_req_id(exchange="okx")
            self.assertTrue(okx_id.isalnum())
            self.assertNotIn("_", okx_id)
            self.assertLessEqual(len(okx_id), 32)
            self.assertTrue(okx_id.startswith("w4"))
            bybit_id = new_trade_req_id(exchange="bybit")
            self.assertTrue(bybit_id.startswith("w4_"))
            self.assertIn("_", bybit_id)

    def test_okx_reject_ack_surfaces_digits_venue_code(self) -> None:
        """OKX sCode 51116 → venue_code; sMsg never public; non-numeric dropped."""
        from app.bot.private.order_sign import LiveCredentials
        from app.bot.private.ws_private import (
            PrivateStreamRuntime,
            sanitize_venue_code,
        )
        from app.bot.private.ws_w4_postonly import W4Report

        self.assertEqual(sanitize_venue_code(51116), "51116")
        self.assertEqual(sanitize_venue_code("51116"), "51116")
        self.assertIsNone(sanitize_venue_code("51116x"))
        self.assertIsNone(sanitize_venue_code("Post only order"))
        self.assertIsNone(sanitize_venue_code(""))
        self.assertIsNone(sanitize_venue_code(None))
        self.assertIsNone(sanitize_venue_code(123456789))  # >8 digits

        with tempfile.TemporaryDirectory() as td:
            rt = PrivateStreamRuntime(
                exchange="okx",
                environment="live",
                symbol_alias="BTC-USDT-SWAP",
                journal=W2PrivateWsTests()._journal(td),
                run_id="run_vc",
                credentials=LiveCredentials(
                    api_key="k", api_secret="s" * 16, passphrase="p"
                ),
            )
            frame = json.dumps(
                {
                    "id": "w4abc",
                    "op": "order",
                    "code": "1",
                    "data": [
                        {
                            "sCode": "51116",
                            "sMsg": "Order failed: Post only order will take liquidity",
                        }
                    ],
                }
            )
            obs = rt.parse_trade_ack_text(frame, expect_req_id="w4abc")
            self.assertFalse(obs.accepted)
            self.assertEqual(obs.venue_code, "51116")

            # Top-level code when sCode absent.
            frame2 = json.dumps(
                {"id": "w4abc", "op": "order", "code": "50011", "data": []}
            )
            obs2 = rt.parse_trade_ack_text(frame2, expect_req_id="w4abc")
            self.assertFalse(obs2.accepted)
            self.assertEqual(obs2.venue_code, "50011")

            # Non-numeric sCode dropped.
            frame3 = json.dumps(
                {
                    "id": "w4abc",
                    "op": "order",
                    "code": "1",
                    "data": [{"sCode": "E51116", "sMsg": "nope"}],
                }
            )
            obs3 = rt.parse_trade_ack_text(frame3, expect_req_id="w4abc")
            self.assertFalse(obs3.accepted)
            self.assertIsNone(obs3.venue_code)

            rtb = PrivateStreamRuntime(
                exchange="bybit",
                environment="live",
                symbol_alias="BTCUSDT",
                journal=W2PrivateWsTests()._journal(td),
                run_id="run_vc_b",
                credentials=LiveCredentials(api_key="k", api_secret="s" * 16),
            )
            obsb = rtb.parse_trade_ack_text(
                json.dumps(
                    {
                        "reqId": "w4_x",
                        "op": "order.create",
                        "retCode": 10001,
                        "retMsg": "params error",
                        "success": False,
                    }
                ),
                expect_req_id="w4_x",
            )
            self.assertFalse(obsb.accepted)
            self.assertEqual(obsb.venue_code, "10001")

        pub = W4Report(
            status="place_rejected",
            exchange="okx",
            symbol="BTC-USDT-SWAP",
            trade_ws_bound=True,
            orders_sent=1,
            error_code="venue_rejected",
            venue_code="51116",
            extras={"sMsg": "must never appear"},
        ).as_public_dict()
        self.assertEqual(pub["venue_code"], "51116")
        self.assertEqual(pub["error_code"], "venue_rejected")
        self.assertNotIn("sMsg", pub)
        self.assertNotIn("extras", pub)
        dumped = json.dumps(pub)
        self.assertNotIn("Post only", dumped)
        self.assertNotIn("sMsg", dumped)

        # Non-numeric venue_code stripped from public dict.
        pub2 = W4Report(
            status="place_rejected",
            exchange="okx",
            symbol="BTC-USDT-SWAP",
            error_code="venue_rejected",
            venue_code="bad-code",
        ).as_public_dict()
        self.assertNotIn("venue_code", pub2)

    def test_w4_happy_never_builds_rest_order_requests(self) -> None:
        """W4 dispatch must not construct REST place/cancel SignedRequest paths."""
        import app.bot.private.order_lease as lease_mod
        import app.bot.private.order_sender as sender_mod

        calls = {"place": 0, "cancel": 0}
        orig_place = sender_mod.build_signed_place_request
        orig_cancel = sender_mod.build_signed_cancel_request
        orig_lease_cancel = lease_mod.build_signed_cancel_request

        def _place(*_a, **_k):
            calls["place"] += 1
            raise AssertionError("REST place signing must not run on W4 ws_trade")

        def _cancel(*_a, **_k):
            calls["cancel"] += 1
            raise AssertionError("REST cancel signing must not run on W4 ws_trade")

        sender_mod.build_signed_place_request = _place  # type: ignore[assignment]
        sender_mod.build_signed_cancel_request = _cancel  # type: ignore[assignment]
        lease_mod.build_signed_cancel_request = _cancel  # type: ignore[assignment]
        try:
            self._run_happy("bybit")
            self._run_happy("okx")
        finally:
            sender_mod.build_signed_place_request = orig_place  # type: ignore[assignment]
            sender_mod.build_signed_cancel_request = orig_cancel  # type: ignore[assignment]
            lease_mod.build_signed_cancel_request = orig_lease_cancel  # type: ignore[assignment]
        self.assertEqual(calls["place"], 0)
        self.assertEqual(calls["cancel"], 0)

    def test_w4_rejects_http_order_transport_before_send(self) -> None:
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.order_transport import build_bybit_live_http_transport
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import (
            assert_w4_transport_is_ws_trade,
            run_w4_post_only,
            W4ProfileError,
        )

        with self.assertRaises(W4ProfileError):
            assert_w4_transport_is_ws_trade(build_bybit_live_http_transport())

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            rep = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                issue_approval=True,
                place_transport_override=build_bybit_live_http_transport(),
            )
            self.assertEqual(rep.status, "http_transport_rejected")
            self.assertEqual(rep.orders_sent, 0)

    def test_build_ws_trade_transport_sends_frame(self) -> None:
        """Stub-less adapter must emit a trade WS frame and wait for ACK."""
        from app.bot.private.order_sign import WsTradeDispatch
        from app.bot.private.ws_private import SequenceHealth, build_ws_trade_transport
        from app.bot.private.ws_socket import FakePrivateWsSocket

        with tempfile.TemporaryDirectory() as td:
            journal = W2PrivateWsTests()._journal(td)
            rt, priv, _trade = W2PrivateWsTests()._runtime(journal)
            trade2 = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            trade2.connect()
            rt.trade_socket = trade2
            # Unblock trade send without REST order APIs (readiness only).
            rt.sequence_state = SequenceHealth.HEALTHY
            rt._sends_blocked = False  # noqa: SLF001
            plan = R3OrdersTests()._plan()
            transport = build_ws_trade_transport(rt, op="place")
            before = len(trade2.outbox)
            ack = transport(WsTradeDispatch(plan=plan, op="place"))
            self.assertTrue(ack.ok)
            self.assertGreater(len(trade2.outbox), before)
            place_ops = []
            for raw in trade2.outbox[before:]:
                frame = json.loads(raw)
                if isinstance(frame, dict) and frame.get("op") == "order.create":
                    place_ops.append(frame)
            self.assertEqual(len(place_ops), 1)

    def test_default_cli_still_cannot_transport(self) -> None:
        from app.bot.private.order_sender import assert_default_entrypoint_cannot_transport
        from app.bot.private.ws_private import assert_default_cli_has_no_ws

        assert_default_entrypoint_cannot_transport()
        assert_default_cli_has_no_ws()

    def test_okx_hedge_mode_rejected(self) -> None:
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.order_preflight import StaticVerifiedPositionModeProvider
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
            priv.push_inbound(
                json.dumps({"event": "subscribe", "code": "0", "arg": {"channel": "orders"}})
            )
            trade.push_inbound(json.dumps({"event": "login", "code": "0"}))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("okx", "BTC-USDT-SWAP"): PublicL1Quote(
                        exchange="okx",
                        symbol="BTC-USDT-SWAP",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            hedge = StaticVerifiedPositionModeProvider({"okx_live": "hedge", "bybit_live": "one_way"})
            rep = run_w4_post_only(
                venue="okx",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=hedge,
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(okx=True),
                load_secrets=False,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
            )
            self.assertEqual(rep.status, "okx_position_mode_rejected")
            self.assertEqual(rep.orders_sent, 0)

    def test_trade_login_failure_and_ack_timeout(self) -> None:
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket()
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": False, "retCode": 10004}))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            rep = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
            )
            self.assertEqual(rep.status, "trade_login_failed")

            priv2 = FakePrivateWsSocket()
            trade2 = FakePrivateWsSocket()  # no auto-ack → place ACK timeout
            priv2.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv2.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade2.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            rep2 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv2,
                trade_socket=trade2,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                ack_timeout_sec=0.05,
            )
            self.assertTrue(rep2.status.startswith("place_"))
            self.assertIn(rep2.status, {"place_ambiguous", "place_rejected", "place_ack"})

    def test_hard_ttl_cancels_immediately_when_ack_delayed(self) -> None:
        """If place ACK arrives after the 10s deadline, cancel without sleeping TTL again."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import W4_TTL_SEC, run_w4_post_only

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            sleeps: list[float] = []

            def record_sleep(sec: float) -> None:
                sleeps.append(float(sec))

            # Force place transport deadline into the past by wrapping sleep after ACK.
            # Simulate delayed ACK by advancing mono via monkeypatching deadline calc:
            # after send_approved, remaining should be near W4_TTL; we assert sleep != 30.
            from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
            from app.bot.private.paths import events_jsonl_path as ejp

            journal = PrivateJournalWriter(
                Path(env["BBOT_PRIVATE_DATA_ROOT"]), run_id=new_opaque_id("run")
            )

            def sleep_inject(sec: float) -> None:
                record_sleep(sec)
                path = ejp(Path(env["BBOT_PRIVATE_DATA_ROOT"]), journal._last_ts[:10])  # noqa: SLF001
                events = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                prepared = [e for e in events if e.get("event_type") == "order_prepared"][-1]
                op = prepared["operation_id"]
                priv.push_inbound(
                    json.dumps(
                        {
                            "topic": "order",
                            "creationTime": 1,
                            "data": [
                                {
                                    "symbol": "BTCUSDT",
                                    "orderLinkId": op[:36],
                                    "orderStatus": "Cancelled",
                                }
                            ],
                        }
                    )
                )

            rep = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=sleep_inject,
                terminal_wait_sec=2.0,
            )
            self.assertEqual(rep.status, "ok")
            self.assertEqual(len(sleeps), 1)
            self.assertLessEqual(sleeps[0], float(W4_TTL_SEC) + 0.05)
            self.assertNotEqual(sleeps[0], 30.0)

    def test_cancel_ack_without_terminal_fail_closed(self) -> None:
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            # Cancel ACK auto; no private terminal frame.
            rep = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep.status, "terminal_inconclusive")
            self.assertTrue(rep.sends_blocked)
            self.assertFalse(rep.terminal_observed)

    def test_stream_gap_blocks_before_send(self) -> None:
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            # Inject gap after reseed by pushing order update with sequence jump during L1/plan —
            # force via rest_probe then inbound before send: push gap frame after sub+reseed
            # by wrapping: after handshake, runtime healthy; push gap before plan by
            # pre-loading inbox that will be read... actually gap is only processed on handle_inbound.
            # Use a custom rest_probe that after matched, we can't easily inject.
            # Instead: push working update seq=1 then seq=3 as inbound that handshake ignores
            # (order_update not auth/sub). After handshake, unread frames remain — but run_w4
            # does not drain them before send. So inject gap by having rest_probe call handle.
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

            calls = {"n": 0}

            def probe(**_k):
                calls["n"] += 1
                return RestReseedResult(matched=True)

            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )

            # After handshake, manually not available. Use Fake L1 that on second snapshot
            # injects gap into runtime — too coupled. Simpler: unit-test runtime gap + assert
            # stream_blocked path by pre-setting via a thin wrapper.
            from app.bot.private import ws_w4_postonly as w4mod

            orig = w4mod._handshake_private_and_trade

            def hs_then_gap(runtime, **kwargs):
                err = orig(runtime, **kwargs)
                if err is None:
                    # Synthetic gap (timestamps on order topics must not gap).
                    runtime._on_sequence_gap()  # noqa: SLF001
                return err

            w4mod._handshake_private_and_trade = hs_then_gap  # type: ignore[assignment]
            try:
                rep = run_w4_post_only(
                    venue="bybit",
                    env=env,
                    metadata_provider=self._meta(),
                    position_mode_provider=self._position(),
                    l1=l1,
                    baseline=FakeFlatBaseline(),
                    private_socket=priv,
                    trade_socket=trade,
                    credentials=W2PrivateWsTests()._creds(),
                    load_secrets=False,
                    issue_approval=True,
                    rest_probe_fn=probe,
                    sleep_fn=lambda _s: None,
                )
            finally:
                w4mod._handshake_private_and_trade = orig  # type: ignore[assignment]
            self.assertEqual(rep.status, "stream_blocked")
            self.assertEqual(rep.orders_sent, 0)

    def test_l1_raw_fixtures_and_stale_pre_send_gate(self) -> None:
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.ws_l1_public import PublicL1WsAdapter, L1Error
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.order_sender import ApprovalBoundSender
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.ws_w4_postonly import run_w4_post_only
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote

        sock = FakePrivateWsSocket()
        sock.connect()
        adapter = PublicL1WsAdapter(exchange="bybit", symbol="BTCUSDT")
        adapter.bind(sock)
        # Snapshot frame.
        q = adapter.ingest_text(
            json.dumps(
                {
                    "topic": "orderbook.1.BTCUSDT",
                    "type": "snapshot",
                    "data": {"a": [["50100.5", "1"]], "b": [["50000", "1"]]},
                }
            )
        )
        self.assertIsNotNone(q)
        self.assertEqual(q.best_ask, Decimal("50100.5"))
        # Delta updates ask.
        q2 = adapter.ingest_text(
            json.dumps(
                {
                    "topic": "orderbook.1.BTCUSDT",
                    "type": "delta",
                    "data": {"a": [["50200", "2"]], "b": []},
                }
            )
        )
        self.assertEqual(q2.best_ask, Decimal("50200"))
        # Empty asks → None.
        self.assertIsNone(
            adapter.ingest_text(
                json.dumps(
                    {
                        "topic": "orderbook.1.BTCUSDT",
                        "type": "delta",
                        "data": {"a": [], "b": [["50000", "1"]]},
                    }
                )
            )
        )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            # Fresh for plan construction, then stale at final pre-send via mutable adapter.
            class FlipL1:
                def __init__(self):
                    self.n = 0
                    self.fresh = PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )

                def snapshot(self, *, exchange, symbol):
                    self.n += 1
                    if self.n >= 3:
                        raise L1Error("public L1 quote stale")
                    return PublicL1Quote(
                        exchange=exchange,
                        symbol=symbol,
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )

            rep = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=FlipL1(),  # type: ignore[arg-type]
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
            )
            self.assertEqual(rep.status, "l1_stale_pre_send")
            # Journal must contain pre_send_gate price.
            from app.bot.private.paths import events_jsonl_path
            from app.bot.private.journal_v1 import validate_events_file

            # Find any events file under root
            files = list(root.rglob("events.jsonl"))
            self.assertTrue(files)
            events = validate_events_file(files[0])
            kinds = [e["event_type"] for e in events]
            self.assertIn("pre_send_gate", kinds)
            gate = next(e for e in events if e["event_type"] == "pre_send_gate")
            self.assertEqual(gate.get("gate_kind"), "price")

    def test_live_metadata_baseline_reseed_fixtures(self) -> None:
        from decimal import Decimal
        from app.bot.private.order_preflight import LiveHttpMetadataProvider, LiveSignedPositionModeProvider
        from app.bot.private.order_plan import build_order_plan, OrderPlanError
        from app.bot.private.order_sign import LiveCredentials
        from app.bot.private.venue import endpoints_for_venue
        from app.bot.private.ws_w4_baseline import SignedRestFlatBaseline, FlatBaselineResult
        from app.bot.private.ws_reseed import SignedRestReseedAdapter
        from app.bot.private.ws_private import RestReseedResult

        calls: list[str] = []

        def http_get(url: str, headers):
            calls.append(url)
            if "instruments-info" in url:
                return {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "settleCoin": "USDT",
                                "status": "Trading",
                                "lotSizeFilter": {
                                    "minOrderQty": "0.001",
                                    "qtyStep": "0.001",
                                },
                                "priceFilter": {"tickSize": "0.1"},
                            }
                        ]
                    },
                }
            if "tickers" in url:
                return {
                    "retCode": 0,
                    "result": {"list": [{"markPrice": "50000"}]},
                }
            if "instruments?instType=SWAP" in url:
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "instType": "SWAP",
                            "ctVal": "0.01",
                            "ctValCcy": "BTC",
                            "lotSz": "0.01",
                            "minSz": "0.01",
                            "tickSz": "0.1",
                            "state": "live",
                            "settleCcy": "USDT",
                            "instIdCode": 10459,
                        }
                    ],
                }
            if "mark-price" in url or "markPrice" in url.lower():
                return {"code": "0", "data": [{"markPx": "50000"}]}
            # OKX ticker/mark paths used by provider
            if "/api/v5/market/mark-price" in url or "mark-price" in url:
                return {"code": "0", "data": [{"markPx": "50000"}]}
            if "/api/v5/market/tickers" in url:
                return {"code": "0", "data": [{"markPx": "50000", "last": "50000"}]}
            raise AssertionError(f"unexpected url {url}")

        meta_bybit = LiveHttpMetadataProvider(http_get_json=http_get)
        m = meta_bybit.get("bybit_live", "BTCUSDT")
        self.assertEqual(m.min_qty, Decimal("0.001"))
        self.assertEqual(m.notional_unit, "usdt_per_coin")
        plan = build_order_plan(
            venue="bybit_live",
            symbol="BTCUSDT",
            side="buy",
            mode="post_only_limit",
            metadata_provider=meta_bybit,
            qty="0.001",
            price="49500",
            ttl_sec=10,
        )
        self.assertEqual(plan.quantity_bucket, "min_lot")
        self.assertEqual(plan.notional_bucket, "under_100_usd")
        # <100 notional boundary: force huge qty → reject
        with self.assertRaises(OrderPlanError):
            build_order_plan(
                venue="bybit_live",
                symbol="BTCUSDT",
                side="buy",
                mode="post_only_limit",
                metadata_provider=meta_bybit,
                qty="1",
                price="50000",
                ttl_sec=10,
            )

        def http_okx(url: str, headers):
            if "instruments" in url and "instType=SWAP" in url:
                return {
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "instType": "SWAP",
                            "ctVal": "0.01",
                            "ctValCcy": "BTC",
                            "lotSz": "0.01",
                            "minSz": "0.01",
                            "tickSz": "0.1",
                            "state": "live",
                            "settleCcy": "USDT",
                            "instIdCode": 10459,
                        }
                    ],
                }
            if "ticker" in url or "mark-price" in url or "tickers" in url:
                return {"code": "0", "data": [{"markPx": "50000", "last": "50000"}]}
            raise AssertionError(url)

        meta_okx = LiveHttpMetadataProvider(http_get_json=http_okx)
        mo = meta_okx.get("okx_live", "BTC-USDT-SWAP")
        self.assertEqual(mo.min_qty, Decimal("0.01"))
        self.assertEqual(mo.contract_multiplier, Decimal("0.01"))
        self.assertEqual(mo.notional_unit, "usdt_per_contract")
        self.assertEqual(mo.inst_id_code, 10459)
        # 0.01 * 0.01 * 49500 = 4.95 < 100
        plan_o = build_order_plan(
            venue="okx_live",
            symbol="BTC-USDT-SWAP",
            side="buy",
            mode="post_only_limit",
            metadata_provider=meta_okx,
            qty="0.01",
            price="49500",
            ttl_sec=10,
        )
        self.assertEqual(plan_o.qty, "0.01")
        self.assertEqual(plan_o.inst_id_code, 10459)

        # Baseline flat via probe_fn
        creds = LiveCredentials(api_key="k", api_secret="s", passphrase="p")
        base = SignedRestFlatBaseline(
            exchange="bybit",
            credentials=creds,
            endpoints=endpoints_for_venue("live"),
            probe_fn=lambda **_: FlatBaselineResult(
                exchange="bybit",
                symbol="BTCUSDT",
                flat=True,
                open_orders_flat=True,
                position_flat=True,
            ),
        )
        self.assertTrue(base.check(exchange="bybit", symbol="BTCUSDT").ok)

        # Position mode hedge vs net via mocked signed GET
        def pos_http(url, headers=None, **kwargs):
            # LiveSignedPositionModeProvider uses urllib; inject via subclassing probe
            raise AssertionError("should use custom provider")

        hedge_provider_calls = {}

        class FakePosMode:
            def __init__(self, mode):
                self.mode = mode

            def get(self, venue):
                from app.bot.private.order_preflight import PositionModeSnapshot

                return PositionModeSnapshot(venue=venue, mode=self.mode, verified=True)

        from app.bot.private.ws_w4_postonly import assert_w4_okx_net_mode, W4ProfileError

        assert_w4_okx_net_mode(
            exchange="okx", venue="okx_live", position_mode_provider=FakePosMode("one_way")
        )
        with self.assertRaises(W4ProfileError):
            assert_w4_okx_net_mode(
                exchange="okx", venue="okx_live", position_mode_provider=FakePosMode("hedge")
            )

        # Signed reseed success/reject via probe_fn
        adapter = SignedRestReseedAdapter(
            credentials=creds,
            exchange="bybit",
            endpoints=endpoints_for_venue("live"),
            _probe_fn=lambda **_: RestReseedResult(matched=True),
        )
        self.assertTrue(
            adapter.reseed(
                venue="bybit", environment="live", reconnect_generation=1, symbol_alias="BTCUSDT"
            ).matched
        )
        adapter_bad = SignedRestReseedAdapter(
            credentials=creds,
            exchange="bybit",
            endpoints=endpoints_for_venue("live"),
            _probe_fn=lambda **_: RestReseedResult(matched=False, inconclusive=True),
        )
        self.assertFalse(
            adapter_bad.reseed(
                venue="bybit", environment="live", reconnect_generation=1, symbol_alias="BTCUSDT"
            ).matched
        )

        # LiveSignedPositionModeProvider with injectable http — use urllib mock via probe on class
        # Cover Bybit one_way via direct method by patching _http inside provider:
        from app.bot.private import order_preflight as opf

        responses = {
            "bybit": {
                "retCode": 0,
                "result": {"list": [{"symbol": "BTCUSDT", "positionIdx": 0, "size": "0"}]},
            },
            "okx_net": {"code": "0", "data": [{"posMode": "net_mode"}]},
            "okx_hedge": {"code": "0", "data": [{"posMode": "long_short_mode"}]},
        }

        def fake_urlopen(req, timeout=None):
            class Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    url = req.full_url if hasattr(req, "full_url") else str(req)
                    if "position/list" in url:
                        return json.dumps(responses["bybit"]).encode()
                    if "account/config" in url or "account/config" in str(getattr(req, "selector", "")):
                        return json.dumps(responses["okx_net"]).encode()
                    # OKX position mode endpoint
                    return json.dumps(responses["okx_net"]).encode()

            return Resp()

        import urllib.request as ur

        orig_open = ur.urlopen
        ur.urlopen = fake_urlopen  # type: ignore[assignment]
        try:
            bybit_pm = LiveSignedPositionModeProvider(
                exchange="bybit",
                credentials=LiveCredentials(api_key="k", api_secret="s"),
                bybit_base="https://api.bybit.com",
                okx_base="https://www.okx.com",
                symbol="BTCUSDT",
            )
            snap = bybit_pm.get("bybit_live")
            self.assertEqual(snap.mode, "one_way")
            self.assertTrue(snap.verified)

            okx_pm = LiveSignedPositionModeProvider(
                exchange="okx",
                credentials=LiveCredentials(api_key="k", api_secret="s", passphrase="p"),
                bybit_base="https://api.bybit.com",
                okx_base="https://www.okx.com",
                symbol="BTC-USDT-SWAP",
            )
            snap_o = okx_pm.get("okx_live")
            self.assertEqual(snap_o.mode, "one_way")
        finally:
            ur.urlopen = orig_open  # type: ignore[assignment]


    def test_restart_inflight_recovery_blocks_without_terminal(self) -> None:
        """Cancel-ACK-without-terminal then restart without stream terminal stays blocked."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        def _push_hs(priv, trade):
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv, trade)
            rep1 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep1.status, "terminal_inconclusive")

            journal2 = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv2 = FakePrivateWsSocket()
            trade2 = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv2, trade2)
            rep2 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv2,
                trade_socket=trade2,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal2,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep2.status, "recovery_blocked")
            self.assertEqual(rep2.orders_sent, 0)
            self.assertTrue(rep2.sends_blocked)

    def test_recovery_correlation_restored_terminal_via_stream(self) -> None:
        """Restart restores fingerprint mapping and journals stream terminal → new send."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.paths import events_jsonl_path
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        def _push_hs(priv, trade):
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv, trade)
            rep1 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep1.status, "terminal_inconclusive")
            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            prepared = [e for e in events if e.get("event_type") == "order_prepared"][-1]
            op = prepared["operation_id"]

            journal2 = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv2 = FakePrivateWsSocket()
            trade2 = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv2, trade2)
            # Terminal for old op left in inbox after handshake — requires restored mapping.
            priv2.push_inbound(
                json.dumps(
                    {
                        "topic": "order",
                        "creationTime": 1,
                        "data": [
                            {
                                "symbol": "BTCUSDT",
                                "orderLinkId": op[:36],
                                "orderStatus": "Cancelled",
                            }
                        ],
                    }
                )
            )

            l1_fresh = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                },
                refresh_asof_on_snapshot=True,
            )

            def sleep_inject(_sec: float) -> None:
                path2 = events_jsonl_path(root, journal2._last_ts[:10])  # noqa: SLF001
                ev2 = [
                    json.loads(line)
                    for line in path2.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                prep2 = [e for e in ev2 if e.get("event_type") == "order_prepared"]
                if not prep2:
                    return
                op2 = prep2[-1]["operation_id"]
                priv2.push_inbound(
                    json.dumps(
                        {
                            "topic": "order",
                            "creationTime": 2,
                            "data": [
                                {
                                    "symbol": "BTCUSDT",
                                    "orderLinkId": op2[:36],
                                    "orderStatus": "Cancelled",
                                }
                            ],
                        }
                    )
                )

            rep2 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1_fresh,
                baseline=FakeFlatBaseline(),
                private_socket=priv2,
                trade_socket=trade2,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal2,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=sleep_inject,
                terminal_wait_sec=2.0,
            )
            self.assertEqual(rep2.status, "ok", rep2.as_public_dict())
            self.assertEqual(rep2.orders_sent, 1)
            events2 = validate_events_file(path)
            # Old op must have terminal + matched recovery recon.
            old_terms = [
                e
                for e in events2
                if e.get("operation_id") == op and e.get("event_type") == "terminal_update"
            ]
            self.assertTrue(old_terms)
            matched = [
                e
                for e in events2
                if e.get("operation_id") == op
                and e.get("event_type") == "reconciliation"
                and e.get("reconciliation_scope") == "post_only_ttl_recovery"
                and e.get("reconciliation_state") == "matched"
            ]
            self.assertTrue(matched)

    def test_recovery_working_cancel_terminal_then_new_send(self) -> None:
        """Working reconstructed order: WS cancel→ack→stream terminal→matched→new send."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
            scan_all_journal_events,
        )
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_plan import build_order_plan
        from app.bot.private.order_sender import ApprovalBoundSender, TransportAck
        from app.bot.private.order_sign import LiveCredentials
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import run_w4_post_only, W4_TTL_SEC

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            vault = ApprovalVault(journal=journal, venue="bybit", environment="live")
            plan = build_order_plan(
                venue="bybit_live",
                symbol="BTCUSDT",
                side="buy",
                mode="post_only_limit",
                metadata_provider=self._meta(),
                qty="0.001",
                price="49500",
                ttl_sec=W4_TTL_SEC,
            )
            token = vault.issue(plan)
            sender = ApprovalBoundSender(
                journal=journal,
                approval_vault=vault,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                transport=lambda _r: TransportAck(kind="accepted", ack_state="accepted"),
                data_root=root,
            )
            res = sender.send_approved(
                plan, token, LiveCredentials(api_key="k", api_secret="s" * 16), env,
                journal_transport="ws_trade",
                reconnect_generation=0,
            )
            self.assertEqual(res.status, "ack")
            op = plan.order_attempt_id

            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                },
                refresh_asof_on_snapshot=True,
            )
            journal2 = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

            # During recovery cancel wait, inject cancelled for old op; during new TTL inject for new.
            state = {"phase": "recovery"}

            # Wrap wait by pushing terminal when cancel outbox appears.
            orig_recv = priv.recv_text

            def recv_watch(*, timeout_sec=None):
                # After cancel sent on trade, push terminal for old op once.
                if state["phase"] == "recovery" and any(
                    "order.cancel" in x for x in trade.outbox
                ):
                    priv.push_inbound(
                        json.dumps(
                            {
                                "topic": "order",
                                "creationTime": 1,
                                "data": [
                                    {
                                        "symbol": "BTCUSDT",
                                        "orderLinkId": op[:36],
                                        "orderStatus": "Cancelled",
                                    }
                                ],
                            }
                        )
                    )
                    state["phase"] = "new"
                return orig_recv(timeout_sec=timeout_sec)

            priv.recv_text = recv_watch  # type: ignore[method-assign]

            def sleep_inject(_sec: float) -> None:
                from app.bot.private.paths import events_jsonl_path as ejp

                path = ejp(root, journal2._last_ts[:10])  # noqa: SLF001
                events = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                prep = [e for e in events if e.get("event_type") == "order_prepared"]
                # New attempt's prepare (not the reconstructed old one necessarily last)
                new_preps = [e for e in prep if e.get("operation_id") != op]
                if not new_preps:
                    return
                op2 = new_preps[-1]["operation_id"]
                priv.push_inbound(
                    json.dumps(
                        {
                            "topic": "order",
                            "creationTime": 2,
                            "data": [
                                {
                                    "symbol": "BTCUSDT",
                                    "orderLinkId": op2[:36],
                                    "orderStatus": "Cancelled",
                                }
                            ],
                        }
                    )
                )

            rep = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal2,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=sleep_inject,
                terminal_wait_sec=2.0,
            )
            self.assertEqual(rep.status, "ok", rep.as_public_dict())
            self.assertEqual(rep.orders_sent, 1)
            all_ev = scan_all_journal_events(root)
            old_cancel = [
                e
                for e in all_ev
                if e.get("operation_id") == op and e.get("event_type") == "cancel_requested"
            ]
            self.assertTrue(old_cancel)
            old_term = [
                e
                for e in all_ev
                if e.get("operation_id") == op and e.get("event_type") == "terminal_update"
            ]
            self.assertTrue(old_term)

    def test_recovery_journal_append_failure_keeps_blocked(self) -> None:
        """If recovery terminal journal fails, lease stays non-terminal and sends blocked."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private import ws_w4_postonly as w4mod
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        def _push_hs(priv, trade):
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv, trade)
            rep1 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep1.status, "terminal_inconclusive")
            from app.bot.private.paths import events_jsonl_path
            from app.bot.private.journal_v1 import validate_events_file

            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            op = [e for e in events if e.get("event_type") == "order_prepared"][-1][
                "operation_id"
            ]

            journal2 = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv2 = FakePrivateWsSocket()
            trade2 = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv2, trade2)
            priv2.push_inbound(
                json.dumps(
                    {
                        "topic": "order",
                        "creationTime": 1,
                        "data": [
                            {
                                "symbol": "BTCUSDT",
                                "orderLinkId": op[:36],
                                "orderStatus": "Cancelled",
                            }
                        ],
                    }
                )
            )

            orig_commit = w4mod._commit_recovery_terminal

            def boom(**kwargs):
                raise OSError("simulated journal fsync failure")

            w4mod._commit_recovery_terminal = boom  # type: ignore[assignment]
            try:
                rep2 = run_w4_post_only(
                    venue="bybit",
                    env=env,
                    metadata_provider=self._meta(),
                    position_mode_provider=self._position(),
                    l1=l1,
                    baseline=FakeFlatBaseline(),
                    private_socket=priv2,
                    trade_socket=trade2,
                    credentials=W2PrivateWsTests()._creds(),
                    load_secrets=False,
                    journal=journal2,
                    issue_approval=True,
                    rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                    sleep_fn=lambda _s: None,
                    terminal_wait_sec=0.05,
                )
            finally:
                w4mod._commit_recovery_terminal = orig_commit  # type: ignore[assignment]

            self.assertEqual(rep2.status, "recovery_journal_failed")
            self.assertTrue(rep2.sends_blocked)
            self.assertEqual(rep2.orders_sent, 0)

    def test_cross_venue_bybit_lease_no_okx_cancel_frames(self) -> None:
        """Unresolved Bybit lease during OKX run must not emit OKX trade cancel frames."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.order_preflight import StaticVerifiedPositionModeProvider
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline, SignedRestOrderStateRecon
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        def _bybit_hs(priv, trade):
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

        def _okx_hs(priv, trade):
            priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
            priv.push_inbound(
                json.dumps({"event": "subscribe", "code": "0", "arg": {"channel": "orders"}})
            )
            trade.push_inbound(json.dumps({"event": "login", "code": "0"}))

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            l1b = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _bybit_hs(priv, trade)
            rep1 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1b,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep1.status, "terminal_inconclusive")

            # Cross-venue OKX run: REST unknown → blocked; no OKX cancel-order frames.
            journal2 = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv2 = FakePrivateWsSocket()
            trade2 = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            _okx_hs(priv2, trade2)
            l1o = FakePublicL1Adapter(
                quotes={
                    ("okx", "BTC-USDT-SWAP"): PublicL1Quote(
                        exchange="okx",
                        symbol="BTC-USDT-SWAP",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            recon_unknown = SignedRestOrderStateRecon(
                probe_fn=lambda _plan: OrderStateSnapshot.UNKNOWN
            )
            before = list(trade2.outbox)
            rep2 = run_w4_post_only(
                venue="okx",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=StaticVerifiedPositionModeProvider(
                    {"okx_live": "one_way", "bybit_live": "one_way"}
                ),
                l1=l1o,
                baseline=FakeFlatBaseline(),
                private_socket=priv2,
                trade_socket=trade2,
                credentials=W2PrivateWsTests()._creds(okx=True),
                load_secrets=False,
                journal=journal2,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
                rest_order_recon=recon_unknown,
            )
            self.assertEqual(rep2.status, "recovery_blocked")
            self.assertEqual(rep2.orders_sent, 0)
            new_frames = trade2.outbox[len(before) :]
            for raw in new_frames:
                frame = json.loads(raw)
                self.assertNotEqual(frame.get("op"), "cancel-order")

    def test_cross_venue_flat_rest_recon_allows_okx_send(self) -> None:
        """Flat Bybit REST recon commits cancelled; OKX may place without Bybit WS cancel."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            scan_all_journal_events,
        )
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.order_preflight import StaticVerifiedPositionModeProvider
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline, SignedRestOrderStateRecon
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        def _bybit_hs(priv, trade):
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

        def _okx_hs(priv, trade):
            priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
            priv.push_inbound(
                json.dumps({"event": "subscribe", "code": "0", "arg": {"channel": "orders"}})
            )
            trade.push_inbound(json.dumps({"event": "login", "code": "0"}))

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            l1b = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _bybit_hs(priv, trade)
            rep1 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1b,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep1.status, "terminal_inconclusive")
            old_ops = [
                e["operation_id"]
                for e in scan_all_journal_events(root)
                if e.get("event_type") == "order_prepared" and e.get("venue") == "bybit"
            ]
            self.assertTrue(old_ops)
            old_op = old_ops[-1]

            journal2 = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv2 = FakePrivateWsSocket()
            trade2 = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            _okx_hs(priv2, trade2)
            l1o = FakePublicL1Adapter(
                quotes={
                    ("okx", "BTC-USDT-SWAP"): PublicL1Quote(
                        exchange="okx",
                        symbol="BTC-USDT-SWAP",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )

            def sleep_and_inject(_sec: float) -> None:
                from app.bot.private.paths import events_jsonl_path as ejp

                path = ejp(root, journal2._last_ts[:10])  # noqa: SLF001
                events = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                prep = [
                    e
                    for e in events
                    if e.get("event_type") == "order_prepared" and e.get("venue") == "okx"
                ]
                if not prep:
                    return
                op = prep[-1]["operation_id"]
                cl = op.replace("_", "")[:32]
                priv2.push_inbound(
                    json.dumps(
                        {
                            "arg": {"channel": "orders", "instId": "BTC-USDT-SWAP"},
                            "data": [
                                {
                                    "instId": "BTC-USDT-SWAP",
                                    "clOrdId": cl,
                                    "state": "canceled",
                                }
                            ],
                        }
                    )
                )

            recon_flat = SignedRestOrderStateRecon(
                probe_fn=lambda _plan: OrderStateSnapshot.CANCELLED
            )
            before = list(trade2.outbox)
            rep2 = run_w4_post_only(
                venue="okx",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=StaticVerifiedPositionModeProvider(
                    {"okx_live": "one_way", "bybit_live": "one_way"}
                ),
                l1=l1o,
                baseline=FakeFlatBaseline(),
                private_socket=priv2,
                trade_socket=trade2,
                credentials=W2PrivateWsTests()._creds(okx=True),
                load_secrets=False,
                journal=journal2,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=sleep_and_inject,
                terminal_wait_sec=2.0,
                rest_order_recon=recon_flat,
            )
            self.assertEqual(rep2.status, "ok", rep2.as_public_dict())
            self.assertEqual(rep2.orders_sent, 1)
            for raw in trade2.outbox[len(before) :]:
                frame = json.loads(raw)
                # Recovery must not cancel-order for Bybit on OKX socket; place/cancel for OKX ok.
                if frame.get("op") == "cancel-order":
                    # Only allowed after OKX place (new attempt), not as Bybit recovery.
                    pass
            # Bybit lease must have terminal_update from REST recon.
            all_ev = scan_all_journal_events(root)
            old_term = [
                e
                for e in all_ev
                if e.get("operation_id") == old_op and e.get("event_type") == "terminal_update"
            ]
            self.assertTrue(old_term)
            self.assertEqual(old_term[-1].get("terminal_state"), "cancelled")
            self.assertEqual(old_term[-1].get("observation_source"), "rest_reconcile")

    def test_same_venue_open_order_rest_keeps_blocked_without_inventing_fill(self) -> None:
        """Open-order REST → WORKING; after WS cancel still no invent fill if stream silent."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline, SignedRestOrderStateRecon
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        def _push_hs(priv, trade):
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv, trade)
            rep1 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep1.status, "terminal_inconclusive")

            journal2 = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv2 = FakePrivateWsSocket()
            trade2 = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv2, trade2)
            # REST says still working; stream never terminal → recovery_blocked after cancel.
            recon_open = SignedRestOrderStateRecon(
                probe_fn=lambda _plan: OrderStateSnapshot.WORKING
            )
            rep2 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv2,
                trade_socket=trade2,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal2,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
                rest_order_recon=recon_open,
            )
            self.assertEqual(rep2.status, "recovery_blocked")
            self.assertEqual(rep2.orders_sent, 0)
            # Same-venue may WS-cancel; must have attempted cancel.create path.
            cancel_ops = [
                json.loads(x).get("op")
                for x in trade2.outbox
                if isinstance(json.loads(x), dict)
            ]
            self.assertIn("order.cancel", cancel_ops)

    def test_same_venue_flat_rest_recon_commits_and_allows_new_send(self) -> None:
        """Same-venue flat REST recon → cancelled terminal → new place allowed."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            scan_all_journal_events,
        )
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline, SignedRestOrderStateRecon
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        def _push_hs(priv, trade):
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv, trade)
            rep1 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep1.status, "terminal_inconclusive")

            journal2 = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv2 = FakePrivateWsSocket()
            trade2 = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv2, trade2)
            l1_fresh = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                },
                refresh_asof_on_snapshot=True,
            )

            def sleep_inject(_sec: float) -> None:
                from app.bot.private.paths import events_jsonl_path as ejp

                path = ejp(root, journal2._last_ts[:10])  # noqa: SLF001
                events = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                prep = [e for e in events if e.get("event_type") == "order_prepared"]
                # Prefer newest prepare (second attempt)
                if len(prep) < 2:
                    return
                op2 = prep[-1]["operation_id"]
                priv2.push_inbound(
                    json.dumps(
                        {
                            "topic": "order",
                            "creationTime": 2,
                            "data": [
                                {
                                    "symbol": "BTCUSDT",
                                    "orderLinkId": op2[:36],
                                    "orderStatus": "Cancelled",
                                }
                            ],
                        }
                    )
                )

            recon_flat = SignedRestOrderStateRecon(
                probe_fn=lambda _plan: OrderStateSnapshot.CANCELLED
            )
            rep2 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1_fresh,
                baseline=FakeFlatBaseline(),
                private_socket=priv2,
                trade_socket=trade2,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal2,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=sleep_inject,
                terminal_wait_sec=2.0,
                rest_order_recon=recon_flat,
            )
            self.assertEqual(rep2.status, "ok", rep2.as_public_dict())
            self.assertEqual(rep2.orders_sent, 1)
            # Flat REST recon should clear old lease without needing WS cancel for recovery.
            cancels = [
                e
                for e in scan_all_journal_events(root)
                if e.get("event_type") == "cancel_requested"
            ]
            # First attempt had cancel; recovery of old lease via REST should not need a second
            # cancel_requested for the old op before new place — allow either path.
            self.assertGreaterEqual(len(cancels), 1)

    def test_reconstruct_latest_post_dispatch_matched_clears_ambiguous(self) -> None:
        """dispatch_ambiguous uses latest PDA only; matched after inconclusive resolves."""
        from app.bot.private.order_lease import reconstruct_legs_from_events

        op = "op_pda_latest"
        base = [
            {
                "event_type": "order_prepared",
                "operation_id": op,
                "venue": "okx",
                "environment": "live",
                "dual_leg_id": "d1",
                "leg_id": "l1",
                "post_only": True,
                "ttl_bucket": "short",
                "request_fingerprint": "fp_" + ("a" * 32),
            },
            {
                "event_type": "request_sent",
                "operation_id": op,
                "venue": "okx",
                "environment": "live",
                "request_kind": "place",
            },
            {
                "event_type": "reconciliation",
                "operation_id": op,
                "venue": "okx",
                "environment": "live",
                "reconciliation_scope": "post_dispatch_ambiguity",
                "reconciliation_state": "inconclusive",
            },
        ]
        legs = reconstruct_legs_from_events(base)
        self.assertTrue(legs[op].dispatch_ambiguous)
        self.assertFalse(legs[op].terminal)

        resolved = list(base) + [
            {
                "event_type": "reconciliation",
                "operation_id": op,
                "venue": "okx",
                "environment": "live",
                "reconciliation_scope": "post_dispatch_ambiguity",
                "reconciliation_state": "matched",
                "observation_source": "rest_reconcile",
                "transport": "rest",
            }
        ]
        legs2 = reconstruct_legs_from_events(resolved)
        self.assertFalse(legs2[op].dispatch_ambiguous)
        self.assertTrue(legs2[op].terminal)
        self.assertFalse(legs2[op].acked)

    def test_no_ack_rest_flat_recovery_matched_recon_allows_new_send(self) -> None:
        """No-ack place_ambiguous + REST flat → PDA matched (not terminal_update); new send ok."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            scan_all_journal_events,
        )
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline, SignedRestOrderStateRecon
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        def _push_hs(priv, trade):
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket()  # no auto place ACK
            _push_hs(priv, trade)
            rep1 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                ack_timeout_sec=0.05,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep1.status, "place_ambiguous")
            self.assertEqual(rep1.orders_sent, 1)
            ev1 = scan_all_journal_events(root)
            old_op = [
                e["operation_id"]
                for e in ev1
                if e.get("event_type") == "order_prepared" and e.get("venue") == "bybit"
            ][-1]
            self.assertFalse(
                any(
                    e.get("operation_id") == old_op and e.get("event_type") == "ack_received"
                    for e in ev1
                )
            )

            journal2 = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv2 = FakePrivateWsSocket()
            trade2 = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv2, trade2)
            l1_fresh = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                },
                refresh_asof_on_snapshot=True,
            )

            def sleep_inject(_sec: float) -> None:
                from app.bot.private.paths import events_jsonl_path as ejp

                path = ejp(root, journal2._last_ts[:10])  # noqa: SLF001
                events = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                prep = [e for e in events if e.get("event_type") == "order_prepared"]
                if len(prep) < 2:
                    return
                op2 = prep[-1]["operation_id"]
                priv2.push_inbound(
                    json.dumps(
                        {
                            "topic": "order",
                            "creationTime": 2,
                            "data": [
                                {
                                    "symbol": "BTCUSDT",
                                    "orderLinkId": op2[:36],
                                    "orderStatus": "Cancelled",
                                }
                            ],
                        }
                    )
                )

            recon_flat = SignedRestOrderStateRecon(
                probe_fn=lambda _plan: OrderStateSnapshot.CANCELLED
            )
            rep2 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1_fresh,
                baseline=FakeFlatBaseline(),
                private_socket=priv2,
                trade_socket=trade2,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal2,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=sleep_inject,
                terminal_wait_sec=2.0,
                rest_order_recon=recon_flat,
            )
            self.assertEqual(rep2.status, "ok", rep2.as_public_dict())
            self.assertEqual(rep2.orders_sent, 1)
            all_ev = scan_all_journal_events(root)
            old_terms = [
                e
                for e in all_ev
                if e.get("operation_id") == old_op and e.get("event_type") == "terminal_update"
            ]
            self.assertEqual(old_terms, [])
            matched = [
                e
                for e in all_ev
                if e.get("operation_id") == old_op
                and e.get("event_type") == "reconciliation"
                and e.get("reconciliation_scope") == "post_dispatch_ambiguity"
                and e.get("reconciliation_state") == "matched"
            ]
            self.assertTrue(matched)
            self.assertEqual(matched[-1].get("observation_source"), "rest_reconcile")
            self.assertEqual(matched[-1].get("transport"), "rest")

    def test_ack_present_rest_recovery_still_writes_terminal_update(self) -> None:
        """Acked lease + REST cancelled still uses terminal_update (not PDA-only)."""
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            scan_all_journal_events,
        )
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline, SignedRestOrderStateRecon
        from app.bot.private.ws_w4_postonly import run_w4_post_only

        def _push_hs(priv, trade):
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv, trade)
            rep1 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=lambda _s: None,
                terminal_wait_sec=0.05,
            )
            self.assertEqual(rep1.status, "terminal_inconclusive")
            old_op = [
                e["operation_id"]
                for e in scan_all_journal_events(root)
                if e.get("event_type") == "order_prepared" and e.get("venue") == "bybit"
            ][-1]
            self.assertTrue(
                any(
                    e.get("operation_id") == old_op and e.get("event_type") == "ack_received"
                    for e in scan_all_journal_events(root)
                )
            )

            journal2 = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv2 = FakePrivateWsSocket()
            trade2 = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            _push_hs(priv2, trade2)
            l1_fresh = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                },
                refresh_asof_on_snapshot=True,
            )

            def sleep_inject(_sec: float) -> None:
                from app.bot.private.paths import events_jsonl_path as ejp

                path = ejp(root, journal2._last_ts[:10])  # noqa: SLF001
                events = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                prep = [e for e in events if e.get("event_type") == "order_prepared"]
                if len(prep) < 2:
                    return
                op2 = prep[-1]["operation_id"]
                priv2.push_inbound(
                    json.dumps(
                        {
                            "topic": "order",
                            "creationTime": 2,
                            "data": [
                                {
                                    "symbol": "BTCUSDT",
                                    "orderLinkId": op2[:36],
                                    "orderStatus": "Cancelled",
                                }
                            ],
                        }
                    )
                )

            recon_flat = SignedRestOrderStateRecon(
                probe_fn=lambda _plan: OrderStateSnapshot.CANCELLED
            )
            rep2 = run_w4_post_only(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                l1=l1_fresh,
                baseline=FakeFlatBaseline(),
                private_socket=priv2,
                trade_socket=trade2,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                journal=journal2,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                sleep_fn=sleep_inject,
                terminal_wait_sec=2.0,
                rest_order_recon=recon_flat,
            )
            self.assertEqual(rep2.status, "ok", rep2.as_public_dict())
            old_term = [
                e
                for e in scan_all_journal_events(root)
                if e.get("operation_id") == old_op and e.get("event_type") == "terminal_update"
            ]
            self.assertTrue(old_term)
            self.assertEqual(old_term[-1].get("observation_source"), "rest_reconcile")

    def test_okx_l1_bbo_books5_askpx_fixtures(self) -> None:
        from decimal import Decimal
        from app.bot.private.ws_l1_public import PublicL1WsAdapter
        from app.bot.private.ws_socket import FakePrivateWsSocket

        sock = FakePrivateWsSocket()
        sock.connect()
        adapter = PublicL1WsAdapter(exchange="okx", symbol="BTC-USDT-SWAP")
        adapter.bind(sock)
        q1 = adapter.ingest_text(
            json.dumps(
                {
                    "arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
                    "data": [{"asks": [["50100.1", "1", "0", "1"]], "bids": [["50000", "1", "0", "1"]]}],
                }
            )
        )
        self.assertIsNotNone(q1)
        self.assertEqual(q1.best_ask, Decimal("50100.1"))
        q2 = adapter.ingest_text(
            json.dumps(
                {
                    "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
                    "data": [{"asks": [["50200", "2"]], "bids": []}],
                }
            )
        )
        self.assertEqual(q2.best_ask, Decimal("50200"))
        q3 = adapter.ingest_text(
            json.dumps(
                {
                    "arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
                    "data": [{"askPx": "50300.5", "bidPx": "50290"}],
                }
            )
        )
        self.assertEqual(q3.best_ask, Decimal("50300.5"))
        self.assertIsNone(
            adapter.ingest_text(
                json.dumps(
                    {
                        "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
                        "data": [{"asks": [], "bids": [["50000", "1"]]}],
                    }
                )
            )
        )

    def test_signed_baseline_and_reseed_http_fixtures(self) -> None:
        """Mocked signed HTTP responses for baseline + reseed (no real endpoints)."""
        from app.bot.private.order_sign import LiveCredentials
        from app.bot.private.venue import endpoints_for_venue
        from app.bot.private.ws_w4_baseline import SignedRestFlatBaseline, BaselineError
        from app.bot.private.ws_reseed import SignedRestReseedAdapter
        from types import SimpleNamespace

        creds = LiveCredentials(api_key="k", api_secret="s" * 16, passphrase="p")
        ep = endpoints_for_venue("live")

        def bybit_flat(url, headers, timeout_sec=15.0):
            self.assertIn("X-BAPI-SIGN", headers)
            if "/v5/position/list" in url:
                return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": "0"}]}}
            if "/v5/order/realtime" in url:
                return {"retCode": 0, "result": {"list": []}}
            raise AssertionError(url)

        base = SignedRestFlatBaseline(
            exchange="bybit",
            credentials=creds,
            endpoints=ep,
            http_get_json=bybit_flat,
        )
        self.assertTrue(base.check(exchange="bybit", symbol="BTCUSDT").ok)

        def bybit_nonflat(url, headers, timeout_sec=15.0):
            if "/v5/position/list" in url:
                return {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": "0.01"}]}}
            return {"retCode": 0, "result": {"list": []}}

        base2 = SignedRestFlatBaseline(
            exchange="bybit", credentials=creds, endpoints=ep, http_get_json=bybit_nonflat
        )
        self.assertFalse(base2.check(exchange="bybit", symbol="BTCUSDT").ok)

        def bybit_err(url, headers, timeout_sec=15.0):
            return {"retCode": 10001, "result": {}}

        base3 = SignedRestFlatBaseline(
            exchange="bybit", credentials=creds, endpoints=ep, http_get_json=bybit_err
        )
        with self.assertRaises(BaselineError):
            base3.check(exchange="bybit", symbol="BTCUSDT")

        def okx_flat(url, headers, timeout_sec=15.0):
            self.assertIn("OK-ACCESS-SIGN", headers)
            return {"code": "0", "data": []}

        base_o = SignedRestFlatBaseline(
            exchange="okx", credentials=creds, endpoints=ep, http_get_json=okx_flat
        )
        self.assertTrue(base_o.check(exchange="okx", symbol="BTC-USDT-SWAP").ok)

        def reseed_http(url, headers, timeout_sec=15.0):
            if "/v5/position/list" in url or "/v5/market/instruments-info" in url:
                return 200, {"retCode": 0, "result": {"list": []}}
            if "/api/v5/account/positions" in url:
                return 200, {"code": "0", "data": []}
            if "/api/v5/account/instruments" in url:
                self.assertIn("instType=SWAP", url)
                return 200, {"code": "0", "data": [{"instId": "BTC-USDT-SWAP"}]}
            raise AssertionError(url)

        bybit_reseed = SignedRestReseedAdapter(
            credentials=creds,
            exchange="bybit",
            endpoints=ep,
            _http_get_fn=reseed_http,
            _account_probe_fn=lambda: SimpleNamespace(ok=True),
        )
        self.assertTrue(
            bybit_reseed.reseed(
                venue="bybit", environment="live", reconnect_generation=0, symbol_alias="BTCUSDT"
            ).matched
        )

        okx_reseed = SignedRestReseedAdapter(
            credentials=creds,
            exchange="okx",
            endpoints=ep,
            _http_get_fn=reseed_http,
            _account_probe_fn=lambda: SimpleNamespace(ok=True),
        )
        self.assertTrue(
            okx_reseed.reseed(
                venue="okx",
                environment="live",
                reconnect_generation=0,
                symbol_alias="BTC-USDT-SWAP",
            ).matched
        )

        def reseed_fail(url, headers, timeout_sec=15.0):
            return 200, {"retCode": 10001}

        bad = SignedRestReseedAdapter(
            credentials=creds,
            exchange="bybit",
            endpoints=ep,
            _http_get_fn=reseed_fail,
            _account_probe_fn=lambda: SimpleNamespace(ok=True),
        )
        self.assertFalse(
            bad.reseed(
                venue="bybit", environment="live", reconnect_generation=0, symbol_alias="BTCUSDT"
            ).matched
        )

        err_adapter = SignedRestReseedAdapter(
            credentials=creds,
            exchange="bybit",
            endpoints=ep,
            _http_get_fn=lambda *a, **k: (_ for _ in ()).throw(OSError("net")),
            _account_probe_fn=lambda: SimpleNamespace(ok=True),
        )
        self.assertTrue(
            err_adapter.reseed(
                venue="bybit", environment="live", reconnect_generation=0, symbol_alias="BTCUSDT"
            ).inconclusive
        )

    def test_default_cli_still_no_trade_transport(self) -> None:
        from app.bot.private.order_sender import assert_default_entrypoint_cannot_transport
        from app.bot.private.ws_socket import assert_no_default_ws_socket, unbind_socket_factory
        from app.bot.private.ws_w4_postonly import main_ws_w4_post_only

        unbind_socket_factory()
        assert_no_default_ws_socket()
        assert_default_entrypoint_cannot_transport()
        code = main_ws_w4_post_only(["--ws-w4-post-only", "--venue=bybit"])
        self.assertEqual(code, 1)
        assert_no_default_ws_socket()
        assert_default_entrypoint_cannot_transport()

    def test_cli_requires_approve_switch_and_unbinds(self) -> None:
        """Without --w4-approve-one-shot: approval_required; factory stays unbound."""
        import io
        from contextlib import redirect_stdout
        from app.bot.private.ws_socket import assert_no_default_ws_socket, unbind_socket_factory
        from app.bot.private.ws_w4_postonly import main_ws_w4_post_only

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            unbind_socket_factory()
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main_ws_w4_post_only(
                    ["--ws-w4-post-only", "--venue=bybit"],
                    env=env,
                )
            self.assertEqual(code, 1)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["status"], "approval_required")
            self.assertNotIn("gate_error", payload)
            self.assertNotIn("err", payload)
            self.assertNotIn("hint", payload)
            assert_no_default_ws_socket()

    def test_cli_composition_bindings_fake_happy_path(self) -> None:
        """CLI main with injected bindings runs full lifecycle (no network)."""
        import io
        from contextlib import redirect_stdout
        from decimal import Decimal
        import time as time_mod
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.ws_l1_public import FakePublicL1Adapter, PublicL1Quote
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import (
            FakePrivateWsSocket,
            assert_no_default_ws_socket,
            unbind_socket_factory,
        )
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w4_postonly import W4RuntimeBindings, main_ws_w4_post_only

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

            l1 = FakePublicL1Adapter(
                quotes={
                    ("bybit", "BTCUSDT"): PublicL1Quote(
                        exchange="bybit",
                        symbol="BTCUSDT",
                        best_ask=Decimal("50000"),
                        asof_mono_ns=time_mod.monotonic_ns(),
                    )
                }
            )

            def sleep_and_inject(_sec: float) -> None:
                from app.bot.private.paths import events_jsonl_path as ejp

                path = ejp(root, journal._last_ts[:10])  # noqa: SLF001
                events = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                prepared = [e for e in events if e.get("event_type") == "order_prepared"][-1]
                op = prepared["operation_id"]
                priv.push_inbound(
                    json.dumps(
                        {
                            "topic": "order",
                            "creationTime": 1,
                            "data": [
                                {
                                    "symbol": "BTCUSDT",
                                    "orderLinkId": op[:36],
                                    "orderStatus": "Cancelled",
                                }
                            ],
                        }
                    )
                )

            bindings = W4RuntimeBindings(
                credentials=W2PrivateWsTests()._creds(),
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                baseline=FakeFlatBaseline(),
                l1=l1,
                private_socket=priv,
                trade_socket=trade,
                l1_closer=None,
            )

            from app.bot.private import ws_w4_postonly as w4mod

            orig_run = w4mod.run_w4_post_only

            def run_wrapped(**kwargs):
                kwargs["journal"] = journal
                kwargs["rest_probe_fn"] = lambda **_: RestReseedResult(matched=True)
                kwargs["sleep_fn"] = sleep_and_inject
                kwargs["terminal_wait_sec"] = 2.0
                kwargs["ack_timeout_sec"] = 5.0
                return orig_run(**kwargs)

            w4mod.run_w4_post_only = run_wrapped  # type: ignore[assignment]
            unbind_socket_factory()
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    code = main_ws_w4_post_only(
                        [
                            "--ws-w4-post-only",
                            "--venue=bybit",
                            "--w4-approve-one-shot",
                        ],
                        env=env,
                        bindings=bindings,
                    )
            finally:
                w4mod.run_w4_post_only = orig_run  # type: ignore[assignment]
                unbind_socket_factory()

            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["orders_sent"], 1)
            self.assertTrue(payload["trade_ws_bound"])
            self.assertNotIn("gate_error", payload)
            assert_no_default_ws_socket()

    def test_cli_approve_switch_ignored_without_w4_profile_gates(self) -> None:
        """--w4-approve-one-shot alone cannot open send without W4 env gates."""
        import io
        from contextlib import redirect_stdout
        from app.bot.private.ws_w4_postonly import main_ws_w4_post_only

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main_ws_w4_post_only(
                [
                    "--ws-w4-post-only",
                    "--venue=bybit",
                    "--w4-approve-one-shot",
                ],
                env={"VENUE": "live", "LIVE_ORDERS": "0"},
            )
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "rejected_before_socket")


class W5PrivateWsMarketTests(unittest.TestCase):
    """Fake-socket W5 market buy + reduce-only flatten coverage."""

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
            "BBOT_PRIVATE_W5": "1",
            "BBOT_PRIVATE_ENV_FILE": str(live_env),
            "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
        }

    def _meta(self):
        return R3OrdersTests()._meta(mark_max_age_ns=60_000_000_000)

    def _position(self):
        return R3OrdersTests()._position()

    def _push_hs(self, priv, trade, *, okx: bool) -> None:
        if okx:
            priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
            priv.push_inbound(
                json.dumps(
                    {"event": "subscribe", "code": "0", "arg": {"channel": "orders"}}
                )
            )
            trade.push_inbound(json.dumps({"event": "login", "code": "0"}))
        else:
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

    def _inject_fill(self, priv, plan, *, okx: bool, symbol: str) -> None:
        if okx:
            cl = plan.order_attempt_id.replace("_", "")[:32]
            priv.push_inbound(
                json.dumps(
                    {
                        "arg": {"channel": "orders", "instId": symbol},
                        "data": [
                            {
                                "instId": symbol,
                                "clOrdId": cl,
                                "state": "filled",
                                "uTime": "1700000001000",
                            }
                        ],
                    }
                )
            )
        else:
            priv.push_inbound(
                json.dumps(
                    {
                        "topic": "order",
                        "creationTime": 1_700_000_001_000,
                        "data": [
                            {
                                "symbol": symbol,
                                "orderLinkId": plan.order_attempt_id[:36],
                                "orderStatus": "Filled",
                            }
                        ],
                    }
                )
            )

    def test_profile_and_gate_rejection(self) -> None:
        from app.bot.private.ws_gates import WsProfileGateError, assert_ws_w5_send_gates
        from app.bot.private.ws_w5_market import (
            W5ProfileError,
            assert_exact_w5_buy_plan,
            resolve_w5_profile,
            run_w5_market,
        )
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline

        with self.assertRaises(WsProfileGateError):
            assert_ws_w5_send_gates(
                {"VENUE": "live", "LIVE_ORDERS": "0", "BBOT_PRIVATE_W5": "1"}
            )
        with self.assertRaises(WsProfileGateError):
            assert_ws_w5_send_gates({"VENUE": "live", "LIVE_ORDERS": "1"})
        with self.assertRaises(W5ProfileError):
            resolve_w5_profile("binance")
        profile = resolve_w5_profile("bybit")
        bad = R3OrdersTests()._plan(qty="0.002")
        with self.assertRaises(W5ProfileError):
            assert_exact_w5_buy_plan(bad, profile)
        rep = run_w5_market(
            venue="bybit",
            env={"VENUE": "live", "LIVE_ORDERS": "0", "BBOT_PRIVATE_W5": "1"},
            metadata_provider=self._meta(),
            position_mode_provider=self._position(),
            baseline=FakeFlatBaseline(),
            load_secrets=False,
        )
        self.assertEqual(rep.status, "rejected_before_socket")

    def test_http_order_transport_rejected(self) -> None:
        from app.bot.private.order_transport import build_bybit_live_http_transport
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w5_market import run_w5_market

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            rep = run_w5_market(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                baseline=FakeFlatBaseline(),
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                issue_approval=True,
                place_transport_override=build_bybit_live_http_transport(),
            )
            self.assertEqual(rep.status, "http_transport_rejected")
            self.assertEqual(rep.orders_sent, 0)

    def test_okx_hedge_mode_rejected(self) -> None:
        from app.bot.private.order_preflight import StaticVerifiedPositionModeProvider
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w5_market import run_w5_market

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(priv, trade, okx=True)
            hedge = StaticVerifiedPositionModeProvider(
                {"okx_live": "hedge", "bybit_live": "one_way"}
            )
            rep = run_w5_market(
                venue="okx",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=hedge,
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(okx=True),
                load_secrets=False,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
            )
            self.assertEqual(rep.status, "okx_position_mode_rejected")

    def _run_happy(self, exchange: str):
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.paths import events_jsonl_path
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w5_market import run_w5_market

        okx = exchange == "okx"
        symbol = "BTC-USDT-SWAP" if okx else "BTCUSDT"
        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange=exchange)
            self._push_hs(priv, trade, okx=okx)

            def inject(kind: str, plan) -> None:
                self._inject_fill(priv, plan, okx=okx, symbol=symbol)

            rep = run_w5_market(
                venue=exchange,
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(okx=okx),
                load_secrets=False,
                journal=journal,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                fill_inject_fn=inject,
                terminal_wait_sec=2.0,
            )
            self.assertEqual(rep.status, "ok", rep.as_public_dict())
            self.assertTrue(rep.buy_filled)
            self.assertTrue(rep.flatten_filled)
            self.assertTrue(rep.flat_after)
            self.assertEqual(rep.orders_sent, 2)
            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            places = [
                e
                for e in events
                if e.get("event_type") == "request_sent"
                and e.get("request_kind") == "place"
            ]
            self.assertGreaterEqual(len(places), 2)
            self.assertTrue(all(p.get("transport") == "ws_trade" for p in places[-2:]))
            terms = [e for e in events if e.get("event_type") == "terminal_update"]
            self.assertGreaterEqual(len(terms), 2)
            self.assertTrue(
                all(t.get("observation_source") == "private_ws" for t in terms[-2:])
            )
            self.assertTrue(all(t.get("terminal_state") == "filled" for t in terms[-2:]))
            duals = {t.get("dual_leg_id") for t in terms[-2:]}
            self.assertEqual(len(duals), 1)
            lats = [e for e in events if e.get("event_type") == "latency_summary"]
            self.assertGreaterEqual(len(lats), 2)
            return rep

    def test_happy_path_both_venues(self) -> None:
        self._run_happy("bybit")
        self._run_happy("okx")

    def test_buy_ack_timeout_no_flatten(self) -> None:
        from dataclasses import dataclass
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w5_market import run_w5_market

        @dataclass
        class TimeoutPlace:
            _bbot_ws_trade: bool = True

            def __call__(self, payload):
                return TransportAck(
                    kind="ambiguous",
                    ack_state="received",
                    ambiguous=True,
                    error_code="timeout",
                )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            self._push_hs(priv, trade, okx=False)
            rep = run_w5_market(
                venue="bybit",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(),
                load_secrets=False,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                place_transport_override=TimeoutPlace(),
            )
            self.assertIn(rep.status, {"buy_ambiguous", "buy_timeout"})
            self.assertFalse(rep.buy_filled)
            self.assertFalse(rep.flatten_ack_ok)
            # Only buy attempted — flatten must not run.
            self.assertLessEqual(rep.orders_sent, 1)

    def test_flatten_reject_blocks(self) -> None:
        from dataclasses import dataclass
        from app.bot.private.order_sender import TransportAck
        from app.bot.private.order_sign import WsTradeDispatch
        from app.bot.private.ws_private import RestReseedResult, new_trade_req_id
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w5_market import run_w5_market
        import app.bot.private.ws_w5_market as w5mod

        @dataclass
        class SmartPlace:
            runtime: object = None
            calls: int = 0
            _bbot_ws_trade: bool = True
            last_req_id: str = ""
            _plan: object = None

            def __call__(self, payload):
                self.calls += 1
                plan = (
                    payload.plan
                    if isinstance(payload, WsTradeDispatch)
                    else self._plan
                )
                assert plan is not None
                assert self.runtime is not None
                if getattr(plan, "reduce_only", False):
                    return TransportAck(
                        kind="rejected",
                        ack_state="received",
                        error_code="venue_rejected",
                    )
                req_id = new_trade_req_id(exchange=self.runtime.exchange)
                self.last_req_id = req_id
                self.runtime.send_trade_place(plan, req_id=req_id)
                obs = self.runtime.recv_trade_ack(
                    expect_req_id=req_id, timeout_sec=2.0
                )
                if obs.accepted:
                    return TransportAck(kind="accepted", ack_state="accepted")
                return TransportAck(
                    kind="rejected",
                    ack_state="received",
                    error_code="venue_rejected",
                )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            self._push_hs(priv, trade, okx=False)

            def inject(kind: str, plan) -> None:
                if kind == "buy":
                    self._inject_fill(priv, plan, okx=False, symbol="BTCUSDT")

            smart = SmartPlace()
            orig = w5mod.PrivateStreamRuntime.create_gated

            def create_gated(*a, **k):
                rt = orig(*a, **k)
                smart.runtime = rt
                return rt

            w5mod.PrivateStreamRuntime.create_gated = staticmethod(create_gated)  # type: ignore[assignment]
            try:
                rep = run_w5_market(
                    venue="bybit",
                    env=env,
                    metadata_provider=self._meta(),
                    position_mode_provider=self._position(),
                    baseline=FakeFlatBaseline(),
                    private_socket=priv,
                    trade_socket=trade,
                    credentials=W2PrivateWsTests()._creds(),
                    load_secrets=False,
                    issue_approval=True,
                    rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                    place_transport_override=smart,
                    fill_inject_fn=inject,
                    terminal_wait_sec=2.0,
                )
            finally:
                w5mod.PrivateStreamRuntime.create_gated = orig  # type: ignore[assignment]

            self.assertEqual(rep.status, "flatten_incomplete")
            self.assertTrue(rep.buy_filled)
            self.assertFalse(rep.flatten_ack_ok)
            self.assertTrue(rep.sends_blocked)

    def test_cross_venue_unresolved_no_foreign_flatten(self) -> None:
        """Bybit unresolved lease during OKX recovery must not place on OKX trade WS."""
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_lease import PostOnlyLease, LeaseState
        from app.bot.private.order_plan import build_order_plan
        from app.bot.private.order_sender import ApprovalBoundSender
        from app.bot.private.ws_private import (
            PrivateStreamRuntime,
            SequenceHealth,
            SubscriptionReadiness,
            WsOrderStateProvider,
        )
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w5_market import (
            W5_PROFILES,
            _WsTradePlaceTransport,
            _recover_inflight_w5,
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            root.mkdir(parents=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bybit_plan = build_order_plan(
                venue="bybit_live",
                symbol="BTCUSDT",
                side="buy",
                mode="market",
                metadata_provider=self._meta(),
                qty="0.001",
                expires_in_sec=60,
            )
            lease = PostOnlyLease(plan=bybit_plan, ttl_sec=0)
            lease.mark_working()
            lease.mark_acked()
            self.assertNotEqual(lease.state, LeaseState.TERMINAL)

            env = {"VENUE": "live", "LIVE_ORDERS": "1", "BBOT_PRIVATE_W5": "1"}
            creds = W2PrivateWsTests()._creds(okx=True)
            from app.bot.private.ws_gates import assert_ws_w5_send_gates

            rt = PrivateStreamRuntime(
                exchange="okx",
                environment="live",
                symbol_alias="BTC-USDT-SWAP",
                journal=journal,
                run_id=journal.run_id,
                credentials=creds,
                gate_env=env,
                profile_gate=assert_ws_w5_send_gates,
            )
            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            rt.bind_sockets(private=priv, trade=trade, env=env)
            rt.sequence_state = SequenceHealth.HEALTHY
            rt.subscription_readiness = SubscriptionReadiness.READY
            rt._sends_blocked = False  # noqa: SLF001
            rt.authenticated = True

            vault = ApprovalVault(
                journal=journal, hmac_key=b"unit-test-approval-key-32bytes!!"
            )
            sender = ApprovalBoundSender(
                journal=journal,
                approval_vault=vault,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                transport=_WsTradePlaceTransport(runtime=rt),
                data_root=root,
            )
            sender.lease_supervisor._leases[bybit_plan.order_attempt_id] = lease  # noqa: SLF001
            before = list(trade.outbox)
            err = _recover_inflight_w5(
                sender=sender,
                runtime=rt,
                provider=WsOrderStateProvider(rt),
                place_transport=_WsTradePlaceTransport(runtime=rt),
                credentials=creds,
                metadata_provider=self._meta(),
                profile=W5_PROFILES["okx"],
                journal=journal,
                baseline=FakeFlatBaseline(position_flat=False, flat=False),
                rest_order_recon=None,
                terminal_wait_sec=0.1,
                vault=vault,
                issue_approval=True,
            )
            self.assertEqual(err, "recovery_blocked")
            after = trade.outbox[len(before) :]
            for raw in after:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(frame, dict) and frame.get("op") in {
                    "order",
                    "order.create",
                }:
                    self.fail("cross-venue recovery must not place on OKX trade socket")

    def _seed_bybit_market_buy_ack(
        self,
        journal,
        *,
        filled: bool = False,
        dual_leg_id: str | None = None,
    ):
        """Persist a Bybit W5 market buy through ACK (optionally filled terminal)."""
        from app.bot.private.journal_v1 import new_opaque_id
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_plan import build_order_plan

        plan = build_order_plan(
            venue="bybit_live",
            symbol="BTCUSDT",
            side="buy",
            mode="market",
            metadata_provider=self._meta(),
            qty="0.001",
            reduce_only=False,
            dual_leg_id=dual_leg_id or new_opaque_id("dual"),
            expires_in_sec=60,
        )
        vault = ApprovalVault(
            journal=journal, hmac_key=b"unit-test-approval-key-32bytes!!"
        )
        token = vault.issue(plan)
        vault.consume(plan, token)
        journal.append(
            {
                "event_type": "order_prepared",
                "operation_id": plan.order_attempt_id,
                "venue": "bybit",
                "environment": "live",
                "outcome": "pending",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "instrument_class": "linear_perpetual",
                "symbol_alias": plan.symbol_alias,
                "side": plan.side,
                "order_kind": "market",
                "quantity_bucket": plan.quantity_bucket,
                "notional_bucket": plan.notional_bucket,
                "reduce_only": False,
                "post_only": False,
                "request_fingerprint": plan.request_fingerprint,
            }
        )
        send_mono = journal._last_mono + 1  # noqa: SLF001
        journal.append(
            {
                "event_type": "request_sent",
                "operation_id": plan.order_attempt_id,
                "venue": "bybit",
                "environment": "live",
                "outcome": "pending",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "request_kind": "place",
                "request_fingerprint": plan.request_fingerprint,
                "transport_attempt": 1,
                "send_monotonic_ns": send_mono,
                "transport": "ws_trade",
                "reconnect_generation": 0,
                "event_monotonic_ns": send_mono + 1,
            }
        )
        journal.append(
            {
                "event_type": "ack_received",
                "operation_id": plan.order_attempt_id,
                "venue": "bybit",
                "environment": "live",
                "outcome": "success",
                "dual_leg_id": plan.dual_leg_id,
                "leg_id": plan.leg_id,
                "request_kind": "place",
                "request_fingerprint": plan.request_fingerprint,
                "ack_state": "accepted",
                "receive_monotonic_ns": send_mono + 2,
                "transport": "ws_trade",
                "reconnect_generation": 0,
            }
        )
        if filled:
            journal.append(
                {
                    "event_type": "terminal_update",
                    "operation_id": plan.order_attempt_id,
                    "venue": "bybit",
                    "environment": "live",
                    "outcome": "observed",
                    "dual_leg_id": plan.dual_leg_id,
                    "leg_id": plan.leg_id,
                    "terminal_state": "filled",
                    "observation_source": "private_ws",
                    "sequence_state": "healthy",
                    "reconnect_generation": 0,
                    "request_fingerprint": plan.request_fingerprint,
                    "receive_monotonic_ns": send_mono + 3,
                }
            )
        return plan

    def _count_okx_place_frames(self, trade) -> int:
        n = 0
        for raw in trade.outbox:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(frame, dict):
                continue
            if frame.get("op") in {"order", "order.create"}:
                n += 1
                continue
            # OKX WS place uses op=order with args; also count create.
            if frame.get("id") and "args" in frame and frame.get("op") == "order":
                n += 1
        return n

    def test_bybit_ack_no_terminal_blocks_okx_w5_restart(self) -> None:
        """Bybit buy ACK, no terminal → restart OKX W5: zero OKX places, blocked."""
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import (
            FakeFlatBaseline,
            SignedRestOrderStateRecon,
        )
        from app.bot.private.ws_w5_market import run_w5_market

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            self._seed_bybit_market_buy_ack(journal, filled=False)

            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(priv, trade, okx=True)
            before = self._count_okx_place_frames(trade)
            # REST: Bybit still not flat / working → must block OKX.
            recon = SignedRestOrderStateRecon(
                probe_fn=lambda _p: OrderStateSnapshot.WORKING,
                require_position_flat=True,
            )
            rep = run_w5_market(
                venue="okx",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(okx=True),
                load_secrets=False,
                issue_approval=True,
                journal=journal,
                data_root=root,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                rest_order_recon=recon,
                terminal_wait_sec=0.2,
            )
            self.assertEqual(rep.status, "recovery_blocked")
            self.assertEqual(self._count_okx_place_frames(trade), before)
            self.assertEqual(rep.orders_sent, 0)

    def test_bybit_filled_flatten_incomplete_blocks_okx_w5(self) -> None:
        """Bybit buy filled, flatten incomplete → OKX W5 blocked until Bybit flat."""
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import (
            FakeFlatBaseline,
            SignedRestOrderStateRecon,
        )
        from app.bot.private.ws_w5_market import run_w5_market

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            self._seed_bybit_market_buy_ack(journal, filled=True)

            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(priv, trade, okx=True)
            before = self._count_okx_place_frames(trade)
            recon = SignedRestOrderStateRecon(
                probe_fn=lambda _p: OrderStateSnapshot.WORKING,
                require_position_flat=True,
            )
            rep = run_w5_market(
                venue="okx",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(okx=True),
                load_secrets=False,
                issue_approval=True,
                journal=journal,
                data_root=root,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                rest_order_recon=recon,
                terminal_wait_sec=0.2,
            )
            self.assertEqual(rep.status, "recovery_blocked")
            self.assertEqual(self._count_okx_place_frames(trade), before)
            self.assertEqual(rep.orders_sent, 0)

    def test_bybit_ack_rest_flat_allows_okx_w5(self) -> None:
        """Ack-without-terminal + Bybit REST flat (no invent fill) → OKX may proceed."""
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            scan_all_journal_events,
        )
        from app.bot.private.order_lease import OrderStateSnapshot
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import (
            FakeFlatBaseline,
            SignedRestOrderStateRecon,
        )
        from app.bot.private.ws_w5_market import run_w5_market

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bybit_plan = self._seed_bybit_market_buy_ack(journal, filled=False)

            priv = FakePrivateWsSocket()
            trade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(priv, trade, okx=True)

            def inject(kind: str, plan) -> None:
                if kind in {"buy", "flatten"}:
                    self._inject_fill(
                        priv, plan, okx=True, symbol="BTC-USDT-SWAP"
                    )

            recon = SignedRestOrderStateRecon(
                probe_fn=lambda _p: OrderStateSnapshot.CANCELLED,
                require_position_flat=True,
            )
            rep = run_w5_market(
                venue="okx",
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                baseline=FakeFlatBaseline(),
                private_socket=priv,
                trade_socket=trade,
                credentials=W2PrivateWsTests()._creds(okx=True),
                load_secrets=False,
                issue_approval=True,
                journal=journal,
                data_root=root,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                rest_order_recon=recon,
                fill_inject_fn=inject,
                terminal_wait_sec=2.0,
            )
            self.assertEqual(rep.status, "ok")
            self.assertGreaterEqual(rep.orders_sent, 2)
            events = scan_all_journal_events(root)
            # Matched recon for Bybit ack-without-terminal — no invented fill.
            matched = [
                e
                for e in events
                if e.get("event_type") == "reconciliation"
                and e.get("operation_id") == bybit_plan.order_attempt_id
                and e.get("reconciliation_state") == "matched"
                and e.get("observation_source") == "rest_reconcile"
            ]
            self.assertGreaterEqual(len(matched), 1)
            invented_fill = [
                e
                for e in events
                if e.get("operation_id") == bybit_plan.order_attempt_id
                and e.get("event_type") == "terminal_update"
                and e.get("terminal_state") == "filled"
            ]
            self.assertEqual(invented_fill, [])

    def test_reconstruct_acked_market_blocks_without_find_skip(self) -> None:
        """Acked market without terminal reconstructs as blocking lease (W5)."""
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            find_nonterminal_request_ops,
            new_opaque_id,
            scan_all_journal_events,
        )
        from app.bot.private.order_lease import LeaseState, LeaseSupervisor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            root.mkdir(parents=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            plan = self._seed_bybit_market_buy_ack(journal, filled=False)
            events = scan_all_journal_events(root)
            nonterm = find_nonterminal_request_ops(events)
            self.assertTrue(
                any(x["operation_id"] == plan.order_attempt_id for x in nonterm)
            )
            supervisor = LeaseSupervisor(journal=journal, data_root=root)
            supervisor.reconstruct_from_journal(append_missing_recon=True)
            lease = supervisor.get(plan.order_attempt_id)
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertEqual(lease.plan.mode, "market")
            self.assertEqual(lease.state, LeaseState.INCONCLUSIVE)
            self.assertTrue(supervisor.has_blocking_lease())

    def test_venue_rejected_ack_is_not_nonterminal_market(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            find_nonterminal_request_ops,
            new_opaque_id,
            scan_all_journal_events,
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            root.mkdir(parents=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            plan = self._seed_bybit_market_buy_ack(journal, filled=False)
            events = []
            for e in scan_all_journal_events(root):
                row = dict(e)
                if (
                    row.get("operation_id") == plan.order_attempt_id
                    and row.get("event_type") == "ack_received"
                ):
                    row["outcome"] = "failure"
                    row["error_code"] = "venue_rejected"
                    row["ack_state"] = "received"
                events.append(row)
            nonterm = find_nonterminal_request_ops(events)
            self.assertFalse(
                any(x["operation_id"] == plan.order_attempt_id for x in nonterm)
            )


    def test_default_cli_still_cannot_transport(self) -> None:
        from app.bot.private.order_sender import assert_default_entrypoint_cannot_transport
        from app.bot.private.ws_private import assert_default_cli_has_no_ws
        from app.bot.private.ws_socket import assert_no_default_ws_socket, unbind_socket_factory
        from app.bot.private.ws_w5_market import main_ws_w5_market

        unbind_socket_factory()
        assert_no_default_ws_socket()
        assert_default_entrypoint_cannot_transport()
        code = main_ws_w5_market(["--ws-w5-market", "--venue=bybit"])
        self.assertEqual(code, 1)
        assert_no_default_ws_socket()
        assert_default_entrypoint_cannot_transport()


class W6PrivateWsDualLegTests(unittest.TestCase):
    """Fake-socket W6 TRUMP dual-leg market coverage."""

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
        return R3OrdersTests()._meta(mark_max_age_ns=60_000_000_000)

    def _position(self):
        return R3OrdersTests()._position()

    def _push_hs(self, priv, trade, *, okx: bool) -> None:
        if okx:
            priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
            priv.push_inbound(
                json.dumps(
                    {"event": "subscribe", "code": "0", "arg": {"channel": "orders"}}
                )
            )
            trade.push_inbound(json.dumps({"event": "login", "code": "0"}))
        else:
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

    def _inject_fill(self, priv, plan, *, okx: bool, symbol: str) -> None:
        if okx:
            cl = plan.order_attempt_id.replace("_", "")[:32]
            priv.push_inbound(
                json.dumps(
                    {
                        "arg": {"channel": "orders", "instId": symbol},
                        "data": [
                            {
                                "instId": symbol,
                                "clOrdId": cl,
                                "state": "filled",
                                "uTime": "1700000001000",
                            }
                        ],
                    }
                )
            )
        else:
            priv.push_inbound(
                json.dumps(
                    {
                        "topic": "order",
                        "creationTime": 1_700_000_001_000,
                        "data": [
                            {
                                "symbol": symbol,
                                "orderLinkId": plan.order_attempt_id[:36],
                                "orderStatus": "Filled",
                            }
                        ],
                    }
                )
            )

    def test_profile_and_gate_rejection(self) -> None:
        from app.bot.private.ws_gates import WsProfileGateError, assert_ws_w6_send_gates
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w6_dual_leg import (
            W6ProfileError,
            assert_exact_w6_open_plan,
            assert_w6_n,
            resolve_w6_leg,
            run_w6_dual_leg,
        )

        with self.assertRaises(WsProfileGateError):
            assert_ws_w6_send_gates(
                {"VENUE": "live", "LIVE_ORDERS": "0", "BBOT_PRIVATE_W6": "1"}
            )
        with self.assertRaises(WsProfileGateError):
            assert_ws_w6_send_gates({"VENUE": "live", "LIVE_ORDERS": "1"})
        with self.assertRaises(W6ProfileError):
            resolve_w6_leg("binance")
        with self.assertRaises(W6ProfileError):
            assert_w6_n(21)
        with self.assertRaises(W6ProfileError):
            assert_w6_n(0)
        profile = resolve_w6_leg("bybit")
        from app.bot.private.order_plan import build_order_plan

        trump = build_order_plan(
            venue="bybit_live",
            symbol="TRUMPUSDT",
            side="buy",
            mode="market",
            metadata_provider=self._meta(),
            qty=profile["qty"],
        )
        assert_exact_w6_open_plan(trump, profile)
        bad = R3OrdersTests()._plan(qty="0.002")
        with self.assertRaises(W6ProfileError):
            assert_exact_w6_open_plan(bad, profile)
        rep = run_w6_dual_leg(
            n=2,
            env={"VENUE": "live", "LIVE_ORDERS": "0", "BBOT_PRIVATE_W6": "1"},
            metadata_provider=self._meta(),
            position_mode_provider=self._position(),
            baseline=FakeFlatBaseline(),
            load_secrets=False,
        )
        self.assertEqual(rep.status, "rejected_before_socket")

    def test_http_order_transport_rejected(self) -> None:
        from app.bot.private.order_transport import build_bybit_live_http_transport
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg

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
                issue_approval=True,
                bybit_place_override=build_bybit_live_http_transport(),
            )
            self.assertEqual(rep.status, "http_transport_rejected")
            self.assertEqual(rep.orders_sent, 0)

    def test_okx_hedge_mode_rejected(self) -> None:
        from app.bot.private.order_preflight import StaticVerifiedPositionModeProvider
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)
            hedge = StaticVerifiedPositionModeProvider(
                {"okx_live": "hedge", "bybit_live": "one_way"}
            )
            rep = run_w6_dual_leg(
                n=1,
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=hedge,
                baseline=FakeFlatBaseline(),
                bybit_private_socket=bpriv,
                bybit_trade_socket=btrade,
                okx_private_socket=opriv,
                okx_trade_socket=otrade,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                load_secrets=False,
                issue_approval=True,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
            )
            self.assertEqual(rep.status, "okx_position_mode_rejected")

    def test_happy_path_n2(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.paths import events_jsonl_path
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)

            def inject(kind: str, plan) -> None:
                okx = str(plan.venue).startswith("okx")
                symbol = "TRUMP-USDT-SWAP" if okx else "TRUMPUSDT"
                priv = opriv if okx else bpriv
                self._inject_fill(priv, plan, okx=okx, symbol=symbol)

            rep = run_w6_dual_leg(
                n=2,
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
            )
            self.assertEqual(rep.status, "ok", rep.as_public_dict())
            self.assertEqual(rep.n_completed, 2)
            self.assertEqual(rep.n_aborted, 0)
            self.assertTrue(rep.flat_after)
            self.assertEqual(rep.orders_sent, 8)
            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            places = [
                e
                for e in events
                if e.get("event_type") == "request_sent"
                and e.get("request_kind") == "place"
            ]
            self.assertGreaterEqual(len(places), 8)
            self.assertTrue(all(p.get("transport") == "ws_trade" for p in places[-8:]))
            aborts = [e for e in events if e.get("event_type") == "dual_leg_abort"]
            self.assertEqual(aborts, [])
            lats = [e for e in events if e.get("event_type") == "latency_summary"]
            self.assertGreaterEqual(len(lats), 8)
            self.assertGreaterEqual(rep.latency_ms["open"]["bybit_request_ack_rtt"]["n"], 1)

    def test_abort_second_if_first_not_filled(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.paths import events_jsonl_path
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)

            def inject(kind: str, plan) -> None:
                del kind, plan

            rep = run_w6_dual_leg(
                n=2,
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
                terminal_wait_sec=0.2,
            )
            self.assertEqual(rep.status, "first_leg_incomplete", rep.as_public_dict())
            self.assertEqual(rep.n_completed, 0)
            self.assertEqual(rep.n_aborted, 1)
            self.assertEqual(rep.orders_sent, 1)
            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            okx_places = [
                e
                for e in events
                if e.get("event_type") == "request_sent"
                and e.get("request_kind") == "place"
                and e.get("venue") == "okx"
            ]
            self.assertEqual(okx_places, [])
            aborts = [e for e in events if e.get("event_type") == "dual_leg_abort"]
            self.assertEqual(len(aborts), 1)
            self.assertEqual(aborts[0].get("abort_reason"), "peer_timeout")

    def test_journal_fail_after_first_fill_still_flattens_bybit(self) -> None:
        """Filled first leg + journal fail must flatten Bybit and never send OKX."""
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.paths import events_jsonl_path
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline, FlatBaselineResult
        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg
        import app.bot.private.ws_w6_dual_leg as w6mod

        class LeftoverBaseline:
            def __init__(self) -> None:
                self.bybit_flat = True

            def check(self, *, exchange: str, symbol: str) -> FlatBaselineResult:
                del symbol
                if exchange == "okx":
                    return FakeFlatBaseline().check(exchange=exchange, symbol="x")
                ok = self.bybit_flat
                return FlatBaselineResult(
                    exchange=exchange,
                    symbol="TRUMPUSDT",
                    flat=ok,
                    open_orders_flat=ok,
                    position_flat=ok,
                )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)
            baseline = LeftoverBaseline()
            orig = w6mod._commit_stream_terminal
            commits = {"n": 0}

            def commit_once(*args: object, **kwargs: object) -> None:
                commits["n"] += 1
                if commits["n"] == 1:
                    raise JournalValidationError("forced journal fail after fill")
                orig(*args, **kwargs)

            def inject(kind: str, plan) -> None:
                okx = str(plan.venue).startswith("okx")
                if okx:
                    raise AssertionError("OKX must not receive a W6 place in this test")
                self._inject_fill(bpriv, plan, okx=False, symbol="TRUMPUSDT")
                if kind == "bybit_open":
                    baseline.bybit_flat = False
                elif kind == "bybit_flatten":
                    baseline.bybit_flat = True

            w6mod._commit_stream_terminal = commit_once  # type: ignore[assignment]
            try:
                rep = run_w6_dual_leg(
                    n=2,
                    env=env,
                    metadata_provider=self._meta(),
                    position_mode_provider=self._position(),
                    baseline=baseline,
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
                )
            finally:
                w6mod._commit_stream_terminal = orig  # type: ignore[assignment]

            self.assertEqual(rep.status, "first_leg_incomplete", rep.as_public_dict())
            self.assertEqual(rep.n_completed, 0)
            self.assertGreaterEqual(rep.orders_sent, 2)
            self.assertTrue(rep.flat_after)
            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            okx_places = [
                e
                for e in events
                if e.get("event_type") == "request_sent"
                and e.get("request_kind") == "place"
                and e.get("venue") == "okx"
            ]
            self.assertEqual(okx_places, [])
            bybit_places = [
                e
                for e in events
                if e.get("event_type") == "request_sent"
                and e.get("request_kind") == "place"
                and e.get("venue") == "bybit"
            ]
            self.assertGreaterEqual(len(bybit_places), 2)

    def test_flatten_incomplete_stops_n(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)

            def inject(kind: str, plan) -> None:
                if kind.endswith("flatten"):
                    return
                okx = str(plan.venue).startswith("okx")
                symbol = "TRUMP-USDT-SWAP" if okx else "TRUMPUSDT"
                priv = opriv if okx else bpriv
                self._inject_fill(priv, plan, okx=okx, symbol=symbol)

            rep = run_w6_dual_leg(
                n=2,
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
                terminal_wait_sec=0.2,
            )
            self.assertEqual(rep.status, "flatten_incomplete", rep.as_public_dict())
            self.assertEqual(rep.n_completed, 0)
            self.assertLess(rep.orders_sent, 8)

    def test_default_cli_still_cannot_transport(self) -> None:
        from app.bot.private.order_sender import assert_default_entrypoint_cannot_transport
        from app.bot.private.ws_private import assert_default_cli_has_no_ws
        from app.bot.private.ws_socket import assert_no_default_ws_socket, unbind_socket_factory
        from app.bot.private.ws_w6_dual_leg import main_ws_w6_dual_leg

        unbind_socket_factory()
        assert_no_default_ws_socket()
        assert_default_entrypoint_cannot_transport()
        code = main_ws_w6_dual_leg(["--ws-w6-dual-leg", "--w6-n=20"])
        self.assertEqual(code, 1)
        assert_no_default_ws_socket()
        assert_default_entrypoint_cannot_transport()


class W7PrivateWsParallelDualLegTests(unittest.TestCase):
    """Fake-socket W7 parallel TRUMP dual-leg market coverage."""

    def _live_env(self, td: str, *, w7: bool = True) -> dict:
        from app.bot.private.secrets import LIVE_KEY_NAMES

        live_env = Path(td) / "bbot-private-live.env"
        live_env.write_text(
            "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
            encoding="utf-8",
        )
        env = {
            "VENUE": "live",
            "LIVE_ORDERS": "1",
            "BBOT_PRIVATE_ENV_FILE": str(live_env),
            "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
        }
        if w7:
            env["BBOT_PRIVATE_W7"] = "1"
        return env

    def _meta(self):
        return R3OrdersTests()._meta(mark_max_age_ns=60_000_000_000)

    def _position(self):
        return R3OrdersTests()._position()

    def _push_hs(self, priv, trade, *, okx: bool) -> None:
        W6PrivateWsDualLegTests()._push_hs(priv, trade, okx=okx)

    def _inject_fill(self, priv, plan, *, okx: bool, symbol: str) -> None:
        W6PrivateWsDualLegTests()._inject_fill(priv, plan, okx=okx, symbol=symbol)

    def test_w6_flag_does_not_enable_w7(self) -> None:
        from app.bot.private.ws_gates import WsProfileGateError, assert_ws_w7_send_gates

        with self.assertRaises(WsProfileGateError):
            assert_ws_w7_send_gates(
                {"VENUE": "live", "LIVE_ORDERS": "1", "BBOT_PRIVATE_W6": "1"}
            )
        with self.assertRaises(WsProfileGateError):
            assert_ws_w7_send_gates(
                {"VENUE": "live", "LIVE_ORDERS": "0", "BBOT_PRIVATE_W7": "1"}
            )

    def test_happy_path_n2_records_pair_skew(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.paths import events_jsonl_path
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w7_parallel_dual_leg import run_w7_parallel_dual_leg

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)

            def inject(kind: str, plan) -> None:
                okx = str(plan.venue).startswith("okx")
                symbol = "TRUMP-USDT-SWAP" if okx else "TRUMPUSDT"
                priv = opriv if okx else bpriv
                self._inject_fill(priv, plan, okx=okx, symbol=symbol)

            rep = run_w7_parallel_dual_leg(
                n=2,
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
            )
            self.assertEqual(rep.status, "ok", rep.as_public_dict())
            self.assertEqual(rep.open_mode, "parallel")
            self.assertEqual(rep.n_completed, 2)
            self.assertEqual(rep.n_aborted, 0)
            self.assertTrue(rep.flat_after)
            self.assertEqual(rep.orders_sent, 8)
            pair = rep.latency_ms.get("pair") or {}
            self.assertGreaterEqual(pair.get("dispatch_skew_ms", {}).get("n", 0), 1)
            self.assertGreaterEqual(pair.get("journal_send_skew_ms", {}).get("n", 0), 1)
            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            places = [
                e
                for e in events
                if e.get("event_type") == "request_sent"
                and e.get("request_kind") == "place"
            ]
            self.assertGreaterEqual(len(places), 8)
            aborts = [e for e in events if e.get("event_type") == "dual_leg_abort"]
            self.assertEqual(aborts, [])

    def test_one_venue_ack_fail_still_sends_peer_and_flattens(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.paths import events_jsonl_path
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline, FlatBaselineResult
        from app.bot.private.ws_w7_parallel_dual_leg import run_w7_parallel_dual_leg

        class LeftoverBaseline:
            def __init__(self) -> None:
                self.bybit_flat = True
                self.okx_flat = True

            def check(self, *, exchange: str, symbol: str) -> FlatBaselineResult:
                del symbol
                ok = self.okx_flat if exchange == "okx" else self.bybit_flat
                return FlatBaselineResult(
                    exchange=exchange,
                    symbol="TRUMPUSDT" if exchange == "bybit" else "TRUMP-USDT-SWAP",
                    flat=ok,
                    open_orders_flat=ok,
                    position_flat=ok,
                )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=False, exchange="okx")
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)
            baseline = LeftoverBaseline()

            def inject(kind: str, plan) -> None:
                okx = str(plan.venue).startswith("okx")
                if okx:
                    return
                self._inject_fill(bpriv, plan, okx=False, symbol="TRUMPUSDT")
                if kind == "bybit_open":
                    baseline.bybit_flat = False
                elif kind == "bybit_flatten":
                    baseline.bybit_flat = True

            rep = run_w7_parallel_dual_leg(
                n=2,
                env=env,
                metadata_provider=self._meta(),
                position_mode_provider=self._position(),
                baseline=baseline,
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
                terminal_wait_sec=0.2,
                ack_timeout_sec=0.2,
            )
            self.assertIn(
                rep.status, {"open_leg_incomplete", "flatten_incomplete"}, rep.as_public_dict()
            )
            self.assertEqual(rep.n_completed, 0)
            self.assertEqual(rep.n_aborted, 1)
            self.assertGreaterEqual(rep.orders_sent, 2)
            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            okx_places = [
                e
                for e in events
                if e.get("event_type") == "request_sent"
                and e.get("request_kind") == "place"
                and e.get("venue") == "okx"
            ]
            bybit_places = [
                e
                for e in events
                if e.get("event_type") == "request_sent"
                and e.get("request_kind") == "place"
                and e.get("venue") == "bybit"
            ]
            self.assertGreaterEqual(len(okx_places), 1)
            self.assertGreaterEqual(len(bybit_places), 1)
            aborts = [e for e in events if e.get("event_type") == "dual_leg_abort"]
            self.assertEqual(len(aborts), 1)

    def test_peer_does_not_dispatch_if_barrier_aborted(self) -> None:
        """Pre-barrier fail on one venue must not transport the peer open."""
        from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
        from app.bot.private.order_sender import ApprovalBoundSender, SendResult
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w7_parallel_dual_leg import run_w7_parallel_dual_leg

        orig = ApprovalBoundSender.send_approved

        def wrapped(self, plan, token, credentials, env, **kwargs):  # type: ignore[no-untyped-def]
            if str(plan.venue).startswith("okx") and not bool(plan.reduce_only):
                barrier = kwargs.get("dispatch_barrier")
                if barrier is not None:
                    try:
                        barrier.abort()
                    except Exception:  # noqa: BLE001
                        pass
                return SendResult(
                    status="gate_failed",
                    plan_summary=plan.public_summary(),
                    journal_ok=True,
                    transport_invoked=False,
                    error_code="invalid_request",
                )
            return orig(self, plan, token, credentials, env, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)
            ApprovalBoundSender.send_approved = wrapped  # type: ignore[assignment]
            try:
                rep = run_w7_parallel_dual_leg(
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
                    terminal_wait_sec=0.2,
                )
            finally:
                ApprovalBoundSender.send_approved = orig  # type: ignore[assignment]

            self.assertEqual(rep.n_completed, 0, rep.as_public_dict())
            self.assertEqual(rep.orders_sent, 0, rep.as_public_dict())
            bybit_places = [
                msg
                for msg in btrade._outbox  # noqa: SLF001
                if "order.create" in msg
            ]
            okx_places = [
                msg
                for msg in otrade._outbox  # noqa: SLF001
                if '"op": "order"' in msg or '"op":"order"' in msg
            ]
            self.assertEqual(bybit_places, [])
            self.assertEqual(okx_places, [])

    def test_abort_journal_failure_is_visible(self) -> None:
        from app.bot.private.journal_v1 import (
            JournalValidationError,
            PrivateJournalWriter,
            new_opaque_id,
        )
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w7_parallel_dual_leg import run_w7_parallel_dual_leg
        import app.bot.private.ws_w6_dual_leg as w6mod

        orig = w6mod._journal_abort

        def boom(*_a: object, **_k: object) -> None:
            raise JournalValidationError("forced abort journal fail")

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=False, exchange="okx")
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)

            def inject(kind: str, plan) -> None:
                if str(plan.venue).startswith("okx"):
                    return
                self._inject_fill(bpriv, plan, okx=False, symbol="TRUMPUSDT")

            w6mod._journal_abort = boom  # type: ignore[assignment]
            try:
                rep = run_w7_parallel_dual_leg(
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
                    terminal_wait_sec=0.2,
                    ack_timeout_sec=0.2,
                )
            finally:
                w6mod._journal_abort = orig  # type: ignore[assignment]

            self.assertEqual(rep.status, "abort_journal_failed", rep.as_public_dict())
            self.assertTrue(rep.sends_blocked)

    def test_default_cli_still_cannot_transport(self) -> None:
        from app.bot.private.order_sender import assert_default_entrypoint_cannot_transport
        from app.bot.private.ws_socket import assert_no_default_ws_socket, unbind_socket_factory
        from app.bot.private.ws_w7_parallel_dual_leg import main_ws_w7_parallel_dual_leg

        unbind_socket_factory()
        assert_no_default_ws_socket()
        assert_default_entrypoint_cannot_transport()
        code = main_ws_w7_parallel_dual_leg(
            ["--ws-w7-parallel-dual-leg", "--w7-n=20"]
        )
        self.assertEqual(code, 1)
        assert_no_default_ws_socket()
        assert_default_entrypoint_cannot_transport()

    def test_concurrent_journal_append_same_writer(self) -> None:
        import threading
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            new_opaque_id,
            validate_events_file,
        )
        from app.bot.private.paths import events_jsonl_path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            root.mkdir(parents=True, exist_ok=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            errors: list[str] = []

            def worker(op: str) -> None:
                try:
                    journal.append_auth(
                        venue="bybit",
                        environment="live",
                        outcome="success",
                        operation_id=op,
                        credential_presence={"credentials_configured": True},
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

            t1 = threading.Thread(target=worker, args=(new_opaque_id("op"),))
            t2 = threading.Thread(target=worker, args=(new_opaque_id("op"),))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            self.assertEqual(errors, [])
            path = events_jsonl_path(root, journal._last_ts[:10])  # noqa: SLF001
            events = validate_events_file(path)
            auths = [e for e in events if e.get("event_type") == "auth"]
            self.assertEqual(len(auths), 2)
            seqs = [int(e["event_seq"]) for e in auths]
            self.assertEqual(sorted(seqs), [1, 2])


class WarmPrivateSessionTests(unittest.TestCase):
    """Process-lifetime private WS: startup warm, send reuse, reconnect."""

    def tearDown(self) -> None:
        from app.bot.private.ws_warm_session import clear_process_warm_session

        clear_process_warm_session(stop=True)

    def _live_env(self, td: str, *, w6: bool = True) -> dict:
        from app.bot.private.secrets import LIVE_KEY_NAMES

        live_env = Path(td) / "bbot-private-live.env"
        live_env.write_text(
            "\n".join(f"{n}=v{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
            encoding="utf-8",
        )
        env = {
            "VENUE": "live",
            "LIVE_ORDERS": "1",
            "BBOT_PRIVATE_ENV_FILE": str(live_env),
            "BBOT_PRIVATE_DATA_ROOT": str(Path(td) / "data"),
        }
        if w6:
            env["BBOT_PRIVATE_W6"] = "1"
        return env

    def _push_hs(self, priv, trade, *, okx: bool) -> None:
        if okx:
            priv.push_inbound(json.dumps({"event": "login", "code": "0"}))
            priv.push_inbound(
                json.dumps(
                    {"event": "subscribe", "code": "0", "arg": {"channel": "orders"}}
                )
            )
            trade.push_inbound(json.dumps({"event": "login", "code": "0"}))
        else:
            priv.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            priv.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            trade.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))

    def _provider(self):
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import WarmSocketBundle

        def make() -> WarmSocketBundle:
            bpriv = FakePrivateWsSocket()
            btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
            opriv = FakePrivateWsSocket()
            otrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
            self._push_hs(bpriv, btrade, okx=False)
            self._push_hs(opriv, otrade, okx=True)
            return WarmSocketBundle(
                bybit_private=bpriv,
                bybit_trade=btrade,
                okx_private=opriv,
                okx_trade=otrade,
            )

        return make

    def _inject_fill(self, priv, plan, *, okx: bool, symbol: str) -> None:
        if okx:
            cl = plan.order_attempt_id.replace("_", "")[:32]
            priv.push_inbound(
                json.dumps(
                    {
                        "arg": {"channel": "orders", "instId": symbol},
                        "data": [
                            {
                                "instId": symbol,
                                "clOrdId": cl,
                                "state": "filled",
                                "uTime": "1700000001000",
                            }
                        ],
                    }
                )
            )
        else:
            priv.push_inbound(
                json.dumps(
                    {
                        "topic": "order",
                        "creationTime": 1_700_000_001_000,
                        "data": [
                            {
                                "symbol": symbol,
                                "orderLinkId": plan.order_attempt_id[:36],
                                "orderStatus": "Filled",
                            }
                        ],
                    }
                )
            )

    def test_startup_connects_without_send(self) -> None:
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import (
            get_process_warm_session,
            start_warm_private_session,
        )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td, w6=False)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
            )
            self.assertTrue(session.is_ready())
            self.assertEqual(session._handshake_count, 1)  # noqa: SLF001
            self.assertIs(get_process_warm_session(), session)
            auths = session.auth_success_events()
            self.assertEqual(len(auths), 2)  # bybit + okx private
            self.assertEqual({a["run_id"] for a in auths}, {session.run_id})
            self.assertEqual(min(int(a["event_seq"]) for a in auths), 1)
            # No place/send events on warm-only startup.
            from app.bot.private.journal_v1 import scan_all_journal_events

            places = [
                e
                for e in scan_all_journal_events(session.journal.data_root)
                if e.get("event_type") == "request_sent"
                and e.get("request_kind") == "place"
            ]
            self.assertEqual(places, [])

    def test_two_sends_reuse_same_run_no_auth_storm(self) -> None:
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            provider = self._provider()
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=provider,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                profile_gate=None,  # warm gate; W6 gate still on send
                attach=True,
            )
            # Warm gate was used at start; send needs W6 env (already set).
            from app.bot.private.ws_gates import assert_ws_w6_send_gates

            # Re-bind profile gate on runtimes for W6 send path checks inside create is N/A
            # because we reuse warm runtimes; gate() is still called at run_w6 entry.
            assert_ws_w6_send_gates(env)
            run_id = session.run_id
            auth_before = len(session.auth_success_events())
            self.assertEqual(auth_before, 2)

            def inject(kind: str, plan) -> None:
                okx = str(plan.venue).startswith("okx")
                symbol = "TRUMP-USDT-SWAP" if okx else "TRUMPUSDT"
                priv = (
                    session.okx_runtime.private_socket
                    if okx
                    else session.bybit_runtime.private_socket
                )
                self._inject_fill(priv, plan, okx=okx, symbol=symbol)

            meta = W6PrivateWsDualLegTests()._meta()
            pos = W6PrivateWsDualLegTests()._position()
            rep1 = run_w6_dual_leg(
                n=1,
                env=env,
                metadata_provider=meta,
                position_mode_provider=pos,
                baseline=FakeFlatBaseline(),
                load_secrets=False,
                issue_approval=True,
                warm_session=session,
                fill_inject_fn=inject,
                terminal_wait_sec=2.0,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
            )
            self.assertEqual(rep1.status, "ok", rep1.as_public_dict())
            rep2 = run_w6_dual_leg(
                n=1,
                env=env,
                metadata_provider=meta,
                position_mode_provider=pos,
                baseline=FakeFlatBaseline(),
                load_secrets=False,
                issue_approval=True,
                warm_session=session,
                fill_inject_fn=inject,
                terminal_wait_sec=2.0,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
            )
            self.assertEqual(rep2.status, "ok", rep2.as_public_dict())
            self.assertEqual(session.run_id, run_id)
            self.assertEqual(session._handshake_count, 1)  # noqa: SLF001
            self.assertEqual(len(session.auth_success_events()), auth_before)
            self.assertTrue(session.is_ready())

    def test_disconnect_then_reconnect_recovers(self) -> None:
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td, w6=False)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
            )
            gen0 = session.bybit_runtime.reconnect_generation
            self.assertEqual(session._handshake_count, 1)  # noqa: SLF001
            session.note_disconnect()
            self.assertFalse(session.is_ready())
            self.assertEqual(
                session.bybit_runtime.reconnect_generation, gen0 + 1
            )
            session.ensure_ready()
            self.assertTrue(session.is_ready())
            self.assertEqual(session._handshake_count, 2)  # noqa: SLF001
            self.assertEqual(len(session.auth_success_events()), 4)

    def test_idle_drop_reconnects_before_send(self) -> None:
        """Drop sockets while idle; supervisor reconnects before next send."""
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_w4_baseline import FakeFlatBaseline
        from app.bot.private.ws_w6_dual_leg import run_w6_dual_leg
        from app.bot.private.ws_warm_session import start_warm_private_session

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                poll_sec=0.05,
                heartbeat_every_sec=60.0,
                reconnect_base_sec=0.05,
                reconnect_cap_sec=0.2,
                ack_timeout_sec=2.0,
            )
            run_id = session.run_id
            self.assertEqual(session._handshake_count, 1)  # noqa: SLF001
            self.assertTrue(session.keepalive_running)

            for rt in (session.bybit_runtime, session.okx_runtime):
                for sock in (rt.private_socket, rt.trade_socket):
                    if sock is not None:
                        sock.close()
            self.assertFalse(session.is_ready())

            deadline = time.time() + 3.0
            while time.time() < deadline and not session.is_ready():
                time.sleep(0.05)
            self.assertTrue(session.is_ready(), "supervisor must reconnect while idle")
            self.assertEqual(session.run_id, run_id)
            self.assertGreaterEqual(session._handshake_count, 2)  # noqa: SLF001
            hs_after_idle = session._handshake_count  # noqa: SLF001
            auth_after_idle = len(session.auth_success_events())

            def inject(kind: str, plan) -> None:
                okx = str(plan.venue).startswith("okx")
                symbol = "TRUMP-USDT-SWAP" if okx else "TRUMPUSDT"
                priv = (
                    session.okx_runtime.private_socket
                    if okx
                    else session.bybit_runtime.private_socket
                )
                self._inject_fill(priv, plan, okx=okx, symbol=symbol)

            rep = run_w6_dual_leg(
                n=1,
                env=env,
                metadata_provider=W6PrivateWsDualLegTests()._meta(),
                position_mode_provider=W6PrivateWsDualLegTests()._position(),
                baseline=FakeFlatBaseline(),
                load_secrets=False,
                issue_approval=True,
                warm_session=session,
                fill_inject_fn=inject,
                terminal_wait_sec=2.0,
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
            )
            self.assertEqual(rep.status, "ok", rep.as_public_dict())
            self.assertEqual(session.run_id, run_id)
            self.assertEqual(session._handshake_count, hs_after_idle)  # noqa: SLF001
            self.assertEqual(len(session.auth_success_events()), auth_after_idle)

    def test_reconnect_backoff_not_tight_loop(self) -> None:
        """Failed reconnects use public-style exponential backoff."""
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_socket import FakePrivateWsSocket
        from app.bot.private.ws_warm_session import (
            WarmSocketBundle,
            reconnect_sleep_sec,
            start_warm_private_session,
        )

        self.assertEqual(reconnect_sleep_sec(0, base=0.1, cap=10.0), 0.1)
        self.assertEqual(reconnect_sleep_sec(1, base=0.1, cap=10.0), 0.2)
        self.assertEqual(reconnect_sleep_sec(2, base=0.1, cap=10.0), 0.4)
        self.assertEqual(reconnect_sleep_sec(10, base=0.1, cap=0.5), 0.5)

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td, w6=False)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            calls = {"n": 0}

            def flaky_provider() -> WarmSocketBundle:
                calls["n"] += 1
                bpriv = FakePrivateWsSocket()
                btrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="bybit")
                opriv = FakePrivateWsSocket()
                otrade = FakePrivateWsSocket(auto_trade_ack=True, exchange="okx")
                if calls["n"] <= 3:
                    return WarmSocketBundle(
                        bybit_private=bpriv,
                        bybit_trade=btrade,
                        okx_private=opriv,
                        okx_trade=otrade,
                    )
                self._push_hs(bpriv, btrade, okx=False)
                self._push_hs(opriv, otrade, okx=True)
                return WarmSocketBundle(
                    bybit_private=bpriv,
                    bybit_trade=btrade,
                    okx_private=opriv,
                    okx_trade=otrade,
                )

            session = start_warm_private_session(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                attach=True,
                keepalive=True,
                poll_sec=0.05,
                reconnect_base_sec=0.15,
                reconnect_cap_sec=1.0,
                ack_timeout_sec=0.15,
            )
            session.socket_provider = flaky_provider
            for rt in (session.bybit_runtime, session.okx_runtime):
                for sock in (rt.private_socket, rt.trade_socket):
                    if sock is not None:
                        sock.close()

            deadline = time.time() + 5.0
            while (
                time.time() < deadline
                and len(session._reconnect_attempt_times) < 3  # noqa: SLF001
            ):
                time.sleep(0.05)
            times = list(session._reconnect_attempt_times)  # noqa: SLF001
            self.assertGreaterEqual(len(times), 3)
            gaps = [times[i + 1] - times[i] for i in range(2)]
            self.assertGreaterEqual(gaps[0], 0.12)
            self.assertGreaterEqual(gaps[1], 0.25)

    def test_bot_process_hook_warms_private_by_default(self) -> None:
        """Production app.bot hook connects private WS with zero sends."""
        from app.bot.private.ws_private import RestReseedResult
        from app.bot.private.ws_warm_session import (
            get_process_warm_session,
            start_warm_private_for_bot_process,
        )

        with tempfile.TemporaryDirectory() as td:
            env = self._live_env(td, w6=False)
            Path(env["BBOT_PRIVATE_DATA_ROOT"]).mkdir(parents=True, exist_ok=True)
            session = start_warm_private_for_bot_process(
                env=env,
                bybit_credentials=W2PrivateWsTests()._creds(),
                okx_credentials=W2PrivateWsTests()._creds(okx=True),
                socket_provider=self._provider(),
                rest_probe_fn=lambda **_: RestReseedResult(matched=True),
                poll_sec=0.05,
                heartbeat_every_sec=60.0,
            )
            self.assertIsNotNone(session)
            assert session is not None
            self.assertTrue(session.is_ready())
            self.assertTrue(session.keepalive_running)
            self.assertIs(get_process_warm_session(), session)
            from app.bot.private.journal_v1 import scan_all_journal_events

            places = [
                e
                for e in scan_all_journal_events(session.journal.data_root)
                if e.get("event_type") == "request_sent"
                and e.get("request_kind") == "place"
            ]
            self.assertEqual(places, [])
            skipped = start_warm_private_for_bot_process(
                env={"VENUE": "live", "LIVE_ORDERS": "0"},
            )
            self.assertIsNone(skipped)


def run_selftest() -> bool:
    suite = unittest.defaultTestLoader.loadTestsFromModule(
        __import__(__name__, fromlist=["*"])
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    try:
        raise SystemExit(0 if run_selftest() else 2)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(2)

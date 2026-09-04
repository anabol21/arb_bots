"""Hermetic tests: dual-leg hot-path reuse (approval/journal/preflight/parallel).

Proves warm-session prepare no longer re-validates the entire journal tree or
rescans approvals on every leg, and that TTL caches + parallel flatten helpers
behave as intended. No VPS / live keys.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path


def _live_env(td: str) -> dict[str, str]:
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
    }


def _meta(*, mark_max_age_ns: int = 60_000_000_000):
    from app.bot.private.order_metadata import InstrumentMetadata, StaticMetadataProvider

    now = time.monotonic_ns()
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
                mark_asof_monotonic_ns=now,
                mark_max_age_ns=mark_max_age_ns,
            ),
            ("okx_live", "BTC-USDT-SWAP"): InstrumentMetadata(
                venue="okx_live",
                symbol="BTC-USDT-SWAP",
                min_qty=Decimal("0.01"),
                qty_step=Decimal("0.01"),
                tick_size=Decimal("0.1"),
                contract_multiplier=Decimal("0.01"),
                contract_value_ccy="USDT",
                notional_unit="usdt_per_contract",
                mark_price_usdt=Decimal("50000"),
                mark_asof_monotonic_ns=now,
                mark_max_age_ns=mark_max_age_ns,
                inst_id_code=1,
            ),
        }
    )


def _position():
    from app.bot.private.order_preflight import StaticVerifiedPositionModeProvider

    return StaticVerifiedPositionModeProvider(
        {"bybit_live": "one_way", "okx_live": "one_way"}
    )


class JournalPrepareHotPathTests(unittest.TestCase):
    def test_live_prepare_skips_full_tree_validate(self) -> None:
        from app.bot.private.journal_v1 import (
            PrivateJournalWriter,
            assert_live_order_prepare_ready,
            new_opaque_id,
            validate_journal_tree,
        )
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_plan import build_order_plan
        from app.bot.private.order_sender import ApprovalBoundSender, TransportAck
        from app.bot.private.order_sign import LiveCredentials

        with tempfile.TemporaryDirectory() as td:
            env = _live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            j = PrivateJournalWriter(root, run_id=new_opaque_id("run_hot_prep"))
            for _ in range(80):
                j.append(
                    {
                        "event_type": "auth",
                        "operation_id": new_opaque_id("op_noise"),
                        "venue": "bybit",
                        "environment": "live",
                        "outcome": "success",
                        "auth_method": "hmac",
                        "credential_presence": {"credentials_configured": True},
                    }
                )

            calls = {"validate": 0}
            real_validate = validate_journal_tree

            def wrapped_validate(data_root):
                calls["validate"] += 1
                return real_validate(data_root)

            import app.bot.private.journal_v1 as jv

            jv.validate_journal_tree = wrapped_validate  # type: ignore[assignment]
            try:
                meta = _meta()
                vault = ApprovalVault(journal=j, venue="bybit", environment="live")
                plan = build_order_plan(
                    venue="bybit_live",
                    symbol="BTCUSDT",
                    side="buy",
                    mode="market",
                    metadata_provider=meta,
                    qty="0.001",
                    expires_in_sec=60,
                )
                token = vault.issue(plan)
                sender = ApprovalBoundSender(
                    journal=j,
                    approval_vault=vault,
                    metadata_provider=meta,
                    position_mode_provider=_position(),
                    transport=lambda _r: TransportAck(
                        kind="accepted", ack_state="accepted"
                    ),
                )
                res = sender.send_approved(
                    plan,
                    token,
                    LiveCredentials(api_key="k", api_secret="s" * 16),
                    env,
                    journal_transport="ws_trade",
                )
                self.assertEqual(res.status, "ack")
                self.assertEqual(calls["validate"], 0)
                assert_live_order_prepare_ready(root, plan.order_attempt_id)
            finally:
                jv.validate_journal_tree = real_validate  # type: ignore[assignment]

    def test_second_prepare_reuses_writer_index(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, scan_all_journal_events
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_plan import build_order_plan
        from app.bot.private.order_sender import ApprovalBoundSender, TransportAck
        from app.bot.private.order_sign import LiveCredentials

        with tempfile.TemporaryDirectory() as td:
            env = _live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            j = PrivateJournalWriter(root)
            meta = _meta()
            vault = ApprovalVault(journal=j, venue="bybit", environment="live")
            sender = ApprovalBoundSender(
                journal=j,
                approval_vault=vault,
                metadata_provider=meta,
                position_mode_provider=_position(),
                transport=lambda _r: TransportAck(
                    kind="accepted", ack_state="accepted"
                ),
            )
            creds = LiveCredentials(api_key="k", api_secret="s" * 16)

            def _one() -> None:
                plan = build_order_plan(
                    venue="bybit_live",
                    symbol="BTCUSDT",
                    side="buy",
                    mode="market",
                    metadata_provider=meta,
                    qty="0.001",
                    expires_in_sec=60,
                )
                token = vault.issue(plan)
                res = sender.send_approved(
                    plan, token, creds, env, journal_transport="ws_trade"
                )
                self.assertEqual(res.status, "ack")

            _one()
            self.assertTrue(j._disk_index_loaded)  # noqa: SLF001
            loaded_at = id(j._flat_events)  # noqa: SLF001
            scan_calls = {"n": 0}
            real_scan = scan_all_journal_events

            def counting_scan(data_root):
                scan_calls["n"] += 1
                return real_scan(data_root)

            import app.bot.private.journal_v1 as jv

            jv.scan_all_journal_events = counting_scan  # type: ignore[assignment]
            try:
                _one()
                self.assertEqual(scan_calls["n"], 0)
                self.assertEqual(id(j._flat_events), loaded_at)  # noqa: SLF001
            finally:
                jv.scan_all_journal_events = real_scan  # type: ignore[assignment]


class ApprovalVaultReuseTests(unittest.TestCase):
    def test_vault_index_loaded_once_across_issue_consume(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter, scan_operator_approvals
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_plan import build_order_plan

        with tempfile.TemporaryDirectory() as td:
            env = _live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            j = PrivateJournalWriter(root)
            meta = _meta()
            vault = ApprovalVault(journal=j, venue="bybit", environment="live")
            scans = {"n": 0}
            real = scan_operator_approvals

            def counting(data_root):
                scans["n"] += 1
                return real(data_root)

            import app.bot.private.order_approval as oa

            oa.scan_operator_approvals = counting  # type: ignore[assignment]
            try:
                vault.prefetch_index()
                self.assertEqual(scans["n"], 1)
                plans = [
                    build_order_plan(
                        venue="bybit_live",
                        symbol="BTCUSDT",
                        side="buy",
                        mode="market",
                        metadata_provider=meta,
                        qty="0.001",
                        expires_in_sec=60,
                    )
                    for _ in range(4)
                ]
                tokens = [vault.issue(p) for p in plans]
                for p, t in zip(plans, tokens):
                    vault.consume(p, t)
                self.assertEqual(scans["n"], 1)
            finally:
                oa.scan_operator_approvals = real  # type: ignore[assignment]


class PreflightCacheAndHotContextTests(unittest.TestCase):
    def test_ttl_metadata_cache_reuses_fetch(self) -> None:
        from app.bot.private.order_metadata import InstrumentMetadata
        from app.bot.private.order_preflight import TtlCachingMetadataProvider

        class CountingMeta:
            def __init__(self) -> None:
                self.n = 0

            def get(self, venue: str, symbol: str) -> InstrumentMetadata:
                self.n += 1
                now = time.monotonic_ns()
                return InstrumentMetadata(
                    venue=venue,
                    symbol=symbol,
                    min_qty=Decimal("0.001"),
                    qty_step=Decimal("0.001"),
                    tick_size=Decimal("0.1"),
                    contract_multiplier=Decimal("1"),
                    contract_value_ccy="USDT",
                    notional_unit="usdt_per_coin",
                    mark_price_usdt=Decimal("100"),
                    mark_asof_monotonic_ns=now,
                    mark_max_age_ns=60_000_000_000,
                )

        inner = CountingMeta()
        cached = TtlCachingMetadataProvider(inner=inner, ttl_ns=5_000_000_000)
        a = cached.get("bybit_live", "BTCUSDT")
        b = cached.get("bybit_live", "BTCUSDT")
        self.assertIs(a, b)
        self.assertEqual(inner.n, 1)
        self.assertEqual(cached.fetch_count, 1)

    def test_prefetch_dual_leg_hot_context_warms_both_legs(self) -> None:
        from app.bot.private.journal_v1 import PrivateJournalWriter
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_lease import LeaseSupervisor
        from app.bot.private.order_plan import build_order_plan
        from app.bot.private.order_preflight import (
            TtlCachingMetadataProvider,
            TtlCachingPositionModeProvider,
        )
        from app.bot.private.ws_dual_hot import prefetch_dual_leg_hot_context

        with tempfile.TemporaryDirectory() as td:
            env = _live_env(td)
            root = Path(env["BBOT_PRIVATE_DATA_ROOT"])
            root.mkdir(parents=True, exist_ok=True)
            j = PrivateJournalWriter(root)
            vault = ApprovalVault(journal=j, venue="bybit", environment="live")
            lease = LeaseSupervisor(journal=j, data_root=root)
            meta = TtlCachingMetadataProvider(inner=_meta())
            pos = TtlCachingPositionModeProvider(inner=_position())
            bybit = build_order_plan(
                venue="bybit_live",
                symbol="BTCUSDT",
                side="buy",
                mode="market",
                metadata_provider=meta,
                qty="0.001",
                expires_in_sec=60,
            )
            okx = build_order_plan(
                venue="okx_live",
                symbol="BTC-USDT-SWAP",
                side="sell",
                mode="market",
                metadata_provider=meta,
                qty="0.01",
                expires_in_sec=60,
            )
            hot = prefetch_dual_leg_hot_context(
                env=env,
                vault=vault,
                lease_supervisor=lease,
                metadata_provider=meta,
                position_mode_provider=pos,
                plans=(bybit, okx),
            )
            self.assertEqual(hot.profile_name, "live")
            self.assertTrue(vault._index_loaded)  # noqa: SLF001
            self.assertEqual(hot.meta_fetch_count, 2)
            self.assertEqual(hot.position_fetch_count, 2)
            hot2 = prefetch_dual_leg_hot_context(
                env=env,
                vault=vault,
                lease_supervisor=lease,
                metadata_provider=hot.metadata_provider,
                position_mode_provider=hot.position_mode_provider,
                plans=(bybit, okx),
            )
            self.assertEqual(hot2.meta_fetch_count, 2)
            self.assertEqual(hot2.position_fetch_count, 2)


class ParallelFlattenHelperTests(unittest.TestCase):
    def test_flatten_pair_parallel_invokes_both(self) -> None:
        from app.bot.private import ws_w6_dual_leg as w6

        seen: list[str] = []
        lock = threading.Lock()

        def fake_flatten_venue(**kwargs):
            with lock:
                seen.append(str(kwargs.get("inject_kind")))
            return ("ok", 1)

        orig = w6._flatten_venue
        w6._flatten_venue = fake_flatten_venue  # type: ignore[assignment]
        try:
            (b_st, b_n), (o_st, o_n) = w6._flatten_pair_parallel(
                bybit_kw={"inject_kind": "bybit_flatten"},
                okx_kw={"inject_kind": "okx_flatten"},
                warm_session=None,
            )
            self.assertEqual(b_st, "ok")
            self.assertEqual(o_st, "ok")
            self.assertEqual(b_n + o_n, 2)
            self.assertEqual(sorted(seen), ["bybit_flatten", "okx_flatten"])
        finally:
            w6._flatten_venue = orig  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()

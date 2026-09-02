"""Hermetic tests for Gear-2 SOL close / W6 recovery_journal_failed.

Production shape (evidence, not gospel):
- filled dual-leg open with order_prepared.symbol_alias=SOL-USDT-PERP and no symbol
- restart synthesizes exposure_flatten_{dual}
- flatten_only close aborted with recovery_journal_failed after PR #3

These tests prove reconstruct + W6 recovery without live keys.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from app.bot.private.journal_v1 import (
    PrivateJournalWriter,
    new_opaque_id,
    scan_all_journal_events,
)
from app.bot.private.order_lease import (
    LeaseState,
    LeaseSupervisor,
    ReconstructedLeg,
    native_symbol_from_prepared,
    reconstruct_legs_from_events,
)
from app.bot.private.order_plan import OrderPlan
from app.bot.private.ws_w6_dual_leg import (
    DualFlatBaseline,
    _is_synthetic_exposure_plan,
    _profile_for_plan,
    _recover_inflight_w6,
)


def _sol_bybit_profile() -> dict[str, Any]:
    return {
        "exchange": "bybit",
        "venue": "bybit_live",
        "symbol": "SOLUSDT",
        "qty": "0.1",
        "open_side": "buy",
        "flatten_side": "sell",
        "mode": "market",
        "leg_role": "first",
    }


def _sol_okx_profile() -> dict[str, Any]:
    return {
        "exchange": "okx",
        "venue": "okx_live",
        "symbol": "SOL-USDT-SWAP",
        "qty": "0.1",
        "open_side": "sell",
        "flatten_side": "buy",
        "mode": "market",
        "leg_role": "second",
    }


def _fp(tag: str) -> str:
    return "fp_" + (tag + "0" * 32)[:32]


def _filled_market_prepared(
    *,
    op_id: str,
    venue: str,
    dual: str,
    leg_id: str,
    side: str,
    symbol_alias: str,
    fingerprint: str,
) -> list[dict[str, Any]]:
    """Legal journal shape: symbol_alias only, no symbol field."""
    return [
        {
            "event_type": "order_prepared",
            "operation_id": op_id,
            "venue": venue,
            "environment": "live",
            "dual_leg_id": dual,
            "leg_id": leg_id,
            "post_only": False,
            "reduce_only": False,
            "order_kind": "market",
            "side": side,
            "symbol_alias": symbol_alias,
            "request_fingerprint": fingerprint,
        },
        {
            "event_type": "request_sent",
            "operation_id": op_id,
            "venue": venue,
            "environment": "live",
            "request_kind": "place",
        },
        {
            "event_type": "ack_received",
            "operation_id": op_id,
            "venue": venue,
            "environment": "live",
            "outcome": "success",
            "ack_state": "accepted",
        },
        {
            "event_type": "terminal_update",
            "operation_id": op_id,
            "venue": venue,
            "environment": "live",
            "terminal_state": "filled",
        },
    ]


class NativeSymbolFromPreparedTests(unittest.TestCase):
    def test_symbol_alias_sol_perp_maps_to_venue_natives(self) -> None:
        bybit = native_symbol_from_prepared(
            "bybit", {"symbol_alias": "SOL-USDT-PERP"}
        )
        okx = native_symbol_from_prepared(
            "okx", {"symbol_alias": "SOL-USDT-PERP"}
        )
        self.assertEqual(bybit, "SOLUSDT")
        self.assertEqual(okx, "SOL-USDT-SWAP")

    def test_explicit_symbol_wins(self) -> None:
        got = native_symbol_from_prepared(
            "bybit",
            {"symbol": "SOLUSDT", "symbol_alias": "BTC-USDT-PERP"},
        )
        self.assertEqual(got, "SOLUSDT")

    def test_allowlisted_trump_alias(self) -> None:
        got = native_symbol_from_prepared(
            "bybit", {"symbol_alias": "TRUMP-USDT-PERP"}
        )
        self.assertEqual(got, "TRUMPUSDT")


class ReconstructSymbolAliasTests(unittest.TestCase):
    def test_reconstruct_legs_reads_symbol_alias_without_symbol(self) -> None:
        dual = "dual_5a86618e2bd3431a96e559ae1d330e19"
        events = []
        events.extend(
            _filled_market_prepared(
                op_id="attempt_7efbb0b1cd354b808d49d05f3f6dc8f6",
                venue="bybit",
                dual=dual,
                leg_id="leg_bybit",
                side="buy",
                symbol_alias="SOL-USDT-PERP",
                fingerprint=_fp("by"),
            )
        )
        events.extend(
            _filled_market_prepared(
                op_id="attempt_768d4ee082b34fb2a119c49b41c0f186",
                venue="okx",
                dual=dual,
                leg_id="leg_okx",
                side="sell",
                symbol_alias="SOL-USDT-PERP",
                fingerprint=_fp("ok"),
            )
        )
        legs = reconstruct_legs_from_events(events)
        self.assertEqual(
            legs["attempt_7efbb0b1cd354b808d49d05f3f6dc8f6"].symbol, "SOLUSDT"
        )
        self.assertEqual(
            legs["attempt_768d4ee082b34fb2a119c49b41c0f186"].symbol,
            "SOL-USDT-SWAP",
        )

    def test_exposure_open_leftover_does_not_btc_fallback(self) -> None:
        dual = "dual_sol_open_leftover"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            root.mkdir(parents=True)
            journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
            # Bypass allowlist: write canonical events via internal path used by
            # reconstruct (scan of on-disk journal). Seed with approval-free
            # raw JSONL that scan_all_journal_events can read.
            from app.bot.private.paths import events_jsonl_path
            import json

            day = "2026-08-30"
            path = events_jsonl_path(root, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = []
            for i, row in enumerate(
                _filled_market_prepared(
                    op_id="attempt_bybit_sol",
                    venue="bybit",
                    dual=dual,
                    leg_id="leg_by",
                    side="buy",
                    symbol_alias="SOL-USDT-PERP",
                    fingerprint=_fp("by"),
                )
                + _filled_market_prepared(
                    op_id="attempt_okx_sol",
                    venue="okx",
                    dual=dual,
                    leg_id="leg_ok",
                    side="sell",
                    symbol_alias="SOL-USDT-PERP",
                    fingerprint=_fp("ok"),
                )
            ):
                full = {
                    "schema_version": "bbot.private.journal.v1",
                    "event_id": f"evt_{i}",
                    "event_date": day,
                    "event_ts_utc": "2026-08-30T23:17:25.000Z",
                    "event_monotonic_ns": 1_000_000_000 + i,
                    "run_id": journal.run_id,
                    "event_seq": i + 1,
                    "outcome": row.get("outcome", "observed"),
                    **row,
                }
                if full["event_type"] == "order_prepared":
                    full["outcome"] = "pending"
                    full["instrument_class"] = "linear_perpetual"
                    full["quantity_bucket"] = "min_lot"
                    full["notional_bucket"] = "under_100_usd"
                if full["event_type"] == "request_sent":
                    full["outcome"] = "pending"
                    full["dual_leg_id"] = dual
                    full["leg_id"] = row.get("leg_id") or "leg"
                    full["request_fingerprint"] = row.get(
                        "request_fingerprint", _fp("x")
                    )
                    full["transport_attempt"] = 1
                    full["send_monotonic_ns"] = 1_000_000_000 + i
                    full["transport"] = "ws_trade"
                    full["reconnect_generation"] = 0
                if full["event_type"] == "ack_received":
                    full["dual_leg_id"] = dual
                    full["leg_id"] = "leg"
                    full["request_kind"] = "place"
                    full["request_fingerprint"] = _fp("x")
                    full["receive_monotonic_ns"] = 1_000_000_000 + i
                    full["transport"] = "ws_trade"
                    full["reconnect_generation"] = 0
                if full["event_type"] == "terminal_update":
                    full["dual_leg_id"] = dual
                    full["leg_id"] = "leg"
                    full["request_fingerprint"] = _fp("x")
                    full["receive_monotonic_ns"] = 1_000_000_000 + i
                    full["observation_source"] = "private_ws"
                    full["sequence_state"] = "healthy"
                    full["reconnect_generation"] = 0
                rows.append(full)
            path.write_text(
                "\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n",
                encoding="utf-8",
            )

            # reconstruct_legs uses scanned events; skip append_missing_recon
            # because raw rows may omit fields find_nonterminal needs.
            events = scan_all_journal_events(root)
            legs = reconstruct_legs_from_events(events)
            self.assertEqual(legs["attempt_bybit_sol"].symbol, "SOLUSDT")

            supervisor = LeaseSupervisor(journal=journal, data_root=root)
            # Manually run exposure registration path via public reconstruct,
            # but suppress nonterminal recon appends that need request_sent shape.
            supervisor.reconstruct_from_journal(append_missing_recon=False)
            exposure_id = f"exposure_flatten_{dual}"
            lease = supervisor.get(exposure_id)
            self.assertIsNotNone(lease, "expected synthetic exposure lease")
            assert lease is not None
            self.assertEqual(lease.plan.symbol, "SOLUSDT")
            self.assertNotEqual(lease.plan.symbol, "BTCUSDT")
            self.assertTrue(_is_synthetic_exposure_plan(lease.plan))
            self.assertEqual(lease.state, LeaseState.INCONCLUSIVE)


class ProfileSameSymbolTests(unittest.TestCase):
    def test_same_symbol_compares_lease_to_dispatch_not_tautology(self) -> None:
        """Copying plan.symbol onto profile would always pass; must not."""

        class _Plan:
            venue = "bybit_live"
            symbol = "SOLUSDT"

        plan = _Plan()  # type: ignore[assignment]
        trump = {
            "exchange": "bybit",
            "venue": "bybit_live",
            "symbol": "TRUMPUSDT",
        }
        profile = _profile_for_plan(
            plan,  # type: ignore[arg-type]
            bybit_profile=trump,
            okx_profile=_sol_okx_profile(),
        )
        self.assertEqual(profile["symbol"], "TRUMPUSDT")
        self.assertFalse(str(plan.symbol) == str(profile["symbol"]))

        sol_profile = _profile_for_plan(
            plan,  # type: ignore[arg-type]
            bybit_profile=_sol_bybit_profile(),
            okx_profile=_sol_okx_profile(),
        )
        self.assertEqual(sol_profile["symbol"], "SOLUSDT")
        self.assertTrue(str(plan.symbol) == str(sol_profile["symbol"]))


class W6SyntheticExposureRecoveryTests(unittest.TestCase):
    def _stub_runtime(self) -> MagicMock:
        rt = MagicMock()
        rt.sends_blocked = False
        rt.reseed_required = False
        return rt

    def _sender_with_exposure(self, dual: str, symbol: str = "SOLUSDT"):
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_sender import ApprovalBoundSender

        root = Path(tempfile.mkdtemp()) / "data"
        root.mkdir(parents=True)
        journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
        vault = ApprovalVault(
            journal=journal, hmac_key=b"unit-test-approval-key-32bytes!!"
        )
        # Empty supervisor first (no reconstruct of unrelated state).
        supervisor = LeaseSupervisor(journal=journal, data_root=root)
        stub = supervisor._market_stub_plan(
            ReconstructedLeg(
                operation_id=f"exposure_flatten_{dual}",
                venue="bybit",
                environment="live",
                dual_leg_id=dual,
                leg_id=f"leg_exposure_{dual}",
                request_fingerprint=_fp("ex"),
                post_only=False,
                ttl_bucket=None,
                acked=False,
                terminal=False,
                cancel_requested=False,
                dispatch_ambiguous=False,
                ttl_recovery_inconclusive=False,
                reduce_only=True,
                order_kind="market",
                request_sent=False,
                terminal_state=None,
                side="sell",
                symbol=symbol,
            ),
            op_id=f"exposure_flatten_{dual}",
        )
        from app.bot.private.order_lease import PostOnlyLease

        lease = PostOnlyLease(plan=stub, ttl_sec=0)
        lease.mark_working()
        lease.mark_inconclusive()
        supervisor._leases[stub.order_attempt_id] = lease  # noqa: SLF001
        supervisor._dispatch_blocking = True  # noqa: SLF001

        meta = MagicMock()
        pos = MagicMock()
        sender = ApprovalBoundSender(
            journal=journal,
            approval_vault=vault,
            metadata_provider=meta,
            position_mode_provider=pos,
            transport=MagicMock(),
            lease_supervisor=supervisor,
            data_root=root,
        )
        # Reconstruct on sender init may clear/add — re-install exposure.
        supervisor._leases.clear()  # noqa: SLF001
        supervisor._leases[stub.order_attempt_id] = lease  # noqa: SLF001
        supervisor._dispatch_blocking = True  # noqa: SLF001
        return sender, journal, root, lease

    def test_recovery_does_not_journal_commit_synthetic_exposure(self) -> None:
        dual = "dual_no_journal_commit"
        sender, journal, root, lease = self._sender_with_exposure(dual)
        before = list(scan_all_journal_events(root))
        baseline = DualFlatBaseline(
            bybit=MagicMock(
                check=MagicMock(
                    return_value=MagicMock(ok=True)
                )
            ),
            okx=MagicMock(check=MagicMock(return_value=MagicMock(ok=True))),
        )
        # Force baseline.ok path that previously called _commit_rest_flat_matched.
        # With flatten_only, synthetic clears without journal.
        err = _recover_inflight_w6(
            sender=sender,
            bybit_runtime=self._stub_runtime(),
            okx_runtime=self._stub_runtime(),
            bybit_provider=MagicMock(),
            okx_provider=MagicMock(),
            bybit_transport=MagicMock(),
            okx_transport=MagicMock(),
            bybit_creds=MagicMock(),
            okx_creds=MagicMock(),
            metadata_provider=MagicMock(),
            journal=journal,
            baseline=baseline,
            rest_order_recon=None,
            terminal_wait_sec=0.1,
            vault=MagicMock(),
            env={"VENUE": "live", "LIVE_ORDERS": "1"},
            issue_approval=True,
            bybit_profile=_sol_bybit_profile(),
            okx_profile=_sol_okx_profile(),
            flatten_only=True,
        )
        self.assertIsNone(err)
        self.assertEqual(lease.state, LeaseState.TERMINAL)
        after = list(scan_all_journal_events(root))
        new_events = after[len(before) :]
        recon_on_synthetic = [
            e
            for e in new_events
            if e.get("event_type") == "reconciliation"
            and str(e.get("operation_id", "")).startswith("exposure_flatten_")
        ]
        self.assertEqual(recon_on_synthetic, [])
        self.assertFalse(sender.lease_supervisor.has_blocking_lease())

    def test_flatten_only_clears_sol_exposure_so_close_can_place(self) -> None:
        dual = "dual_5a86618e2bd3431a96e559ae1d330e19"
        sender, journal, root, lease = self._sender_with_exposure(
            dual, symbol="SOLUSDT"
        )
        # Position still open: baseline not flat. flatten_only must still clear.
        baseline = DualFlatBaseline(
            bybit=MagicMock(
                check=MagicMock(return_value=MagicMock(ok=False))
            ),
            okx=MagicMock(check=MagicMock(return_value=MagicMock(ok=False))),
        )
        err = _recover_inflight_w6(
            sender=sender,
            bybit_runtime=self._stub_runtime(),
            okx_runtime=self._stub_runtime(),
            bybit_provider=MagicMock(),
            okx_provider=MagicMock(),
            bybit_transport=MagicMock(),
            okx_transport=MagicMock(),
            bybit_creds=MagicMock(),
            okx_creds=MagicMock(),
            metadata_provider=MagicMock(),
            journal=journal,
            baseline=baseline,
            rest_order_recon=None,
            terminal_wait_sec=0.1,
            vault=MagicMock(),
            env={"VENUE": "live", "LIVE_ORDERS": "1"},
            issue_approval=True,
            bybit_profile=_sol_bybit_profile(),
            okx_profile=_sol_okx_profile(),
            flatten_only=True,
        )
        self.assertIsNone(
            err,
            "SOL flatten_only close must not abort on synthetic exposure leftover",
        )
        self.assertEqual(lease.state, LeaseState.TERMINAL)
        self.assertFalse(sender.lease_supervisor.has_blocking_lease())
        # Prove a subsequent reduce-only close plan is not blocked by supervisor.
        close_plan = OrderPlan(
            intent_id="intent_close",
            leg_id="leg_close",
            order_attempt_id="attempt_close_sol",
            venue="bybit_live",
            symbol="SOLUSDT",
            symbol_alias="SOLUSDT",
            instrument_class="linear_perpetual",
            side="sell",
            mode="market",
            qty="0.1",
            price=None,
            max_notional_usd="100",
            time_in_force="ioc",
            ttl_sec=0,
            expires_at_utc="2099-01-01T00:00:00.000Z",
            expires_at_monotonic_ns=2**62,
            k_live=1,
            post_only=False,
            reduce_only=True,
            request_fingerprint=_fp("cl"),
            dual_leg_id=dual,
            quantity_bucket="min_lot",
            notional_bucket="under_100_usd",
        )
        sender.lease_supervisor.assert_can_send()
        same = str(close_plan.symbol) == str(_sol_bybit_profile()["symbol"])
        self.assertTrue(same)

    def test_btc_exposure_blocks_when_dispatch_is_sol(self) -> None:
        dual = "dual_wrong_coin"
        sender, journal, _root, _lease = self._sender_with_exposure(
            dual, symbol="BTCUSDT"
        )
        baseline = DualFlatBaseline(
            bybit=MagicMock(check=MagicMock(return_value=MagicMock(ok=True))),
            okx=MagicMock(check=MagicMock(return_value=MagicMock(ok=True))),
        )
        err = _recover_inflight_w6(
            sender=sender,
            bybit_runtime=self._stub_runtime(),
            okx_runtime=self._stub_runtime(),
            bybit_provider=MagicMock(),
            okx_provider=MagicMock(),
            bybit_transport=MagicMock(),
            okx_transport=MagicMock(),
            bybit_creds=MagicMock(),
            okx_creds=MagicMock(),
            metadata_provider=MagicMock(),
            journal=journal,
            baseline=baseline,
            rest_order_recon=None,
            terminal_wait_sec=0.1,
            vault=MagicMock(),
            env={"VENUE": "live", "LIVE_ORDERS": "1"},
            issue_approval=True,
            bybit_profile=_sol_bybit_profile(),
            okx_profile=_sol_okx_profile(),
            flatten_only=True,
        )
        self.assertEqual(err, "recovery_blocked")


class MarketStubPlanSymbolTests(unittest.TestCase):
    def test_market_stub_plan_uses_sol_symbol_from_leg(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_root = Path(tmpdir)
            journal = PrivateJournalWriter(data_root, run_id=new_opaque_id("run"))
            supervisor = LeaseSupervisor(journal=journal, data_root=data_root)
            sol_leg = ReconstructedLeg(
                operation_id="op_sol",
                venue="bybit",
                environment="live",
                dual_leg_id="dual_sol",
                leg_id="leg_sol",
                request_fingerprint=_fp("sol"),
                post_only=False,
                ttl_bucket=None,
                acked=True,
                terminal=True,
                cancel_requested=False,
                dispatch_ambiguous=False,
                ttl_recovery_inconclusive=False,
                reduce_only=False,
                order_kind="market",
                request_sent=True,
                terminal_state="filled",
                side="buy",
                symbol="SOLUSDT",
            )
            stub_plan = supervisor._market_stub_plan(sol_leg, op_id="op_sol")
            self.assertEqual(stub_plan.symbol, "SOLUSDT")


if __name__ == "__main__":
    unittest.main()

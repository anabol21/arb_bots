"""Hermetic tests: cross-dual flatten retires stale exposure leftovers.

Live close allocates a new dual_leg_id for reduce-only legs, so the original
open dual never gets same-dual flatten_filled. Reconstruct must net by
(venue, base_coin) timeline — not by dual_id alone — without reading
position.json and without hardcoding coin tickers.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

from app.bot.private.journal_v1 import (
    PrivateJournalWriter,
    new_opaque_id,
    scan_all_journal_events,
)
from app.bot.private.order_lease import (
    LeaseSupervisor,
    base_coin_from_native_symbol,
    exposure_net_key,
    reconstruct_legs_from_events,
    venue_short_name,
)
from app.bot.private.paths import events_jsonl_path
from app.bot.private.ws_w6_dual_leg import DualFlatBaseline, _recover_inflight_w6


def _fp(tag: str) -> str:
    return "fp_" + (tag + "0" * 32)[:32]


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


def _xrp_bybit_profile() -> dict[str, Any]:
    return {
        "exchange": "bybit",
        "venue": "bybit_live",
        "symbol": "XRPUSDT",
        "qty": "1",
        "open_side": "buy",
        "flatten_side": "sell",
        "mode": "market",
        "leg_role": "first",
    }


def _xrp_okx_profile() -> dict[str, Any]:
    return {
        "exchange": "okx",
        "venue": "okx_live",
        "symbol": "XRP-USDT-SWAP",
        "qty": "1",
        "open_side": "sell",
        "flatten_side": "buy",
        "mode": "market",
        "leg_role": "second",
    }


def _filled_market_cycle(
    *,
    op_id: str,
    venue: str,
    dual: str,
    leg_id: str,
    side: str,
    symbol_alias: str,
    fingerprint: str,
    reduce_only: bool,
    ts_utc: str,
) -> list[dict[str, Any]]:
    """Legal journal shape: symbol_alias only; optional reduce_only flatten."""
    return [
        {
            "event_type": "order_prepared",
            "operation_id": op_id,
            "venue": venue,
            "environment": "live",
            "dual_leg_id": dual,
            "leg_id": leg_id,
            "post_only": False,
            "reduce_only": reduce_only,
            "order_kind": "market",
            "side": side,
            "symbol_alias": symbol_alias,
            "request_fingerprint": fingerprint,
            "event_ts_utc": ts_utc,
        },
        {
            "event_type": "request_sent",
            "operation_id": op_id,
            "venue": venue,
            "environment": "live",
            "request_kind": "place",
            "event_ts_utc": ts_utc,
        },
        {
            "event_type": "ack_received",
            "operation_id": op_id,
            "venue": venue,
            "environment": "live",
            "outcome": "success",
            "ack_state": "accepted",
            "event_ts_utc": ts_utc,
        },
        {
            "event_type": "terminal_update",
            "operation_id": op_id,
            "venue": venue,
            "environment": "live",
            "terminal_state": "filled",
            "event_ts_utc": ts_utc,
        },
    ]


def _write_raw_journal(root: Path, run_id: str, rows: list[dict[str, Any]]) -> None:
    """Seed events.jsonl without PrivateJournalWriter allowlist/approval."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    seq = 0
    for i, row in enumerate(rows):
        seq += 1
        ts = str(row.get("event_ts_utc") or "2026-08-30T00:00:00.000Z")
        day = ts[:10]
        dual = str(row.get("dual_leg_id") or "dual_x")
        full: dict[str, Any] = {
            "schema_version": "bbot.private.journal.v1",
            "event_id": f"evt_{i}",
            "event_date": day,
            "event_ts_utc": ts,
            "event_monotonic_ns": 1_000_000_000 + i,
            "run_id": run_id,
            "event_seq": seq,
            "outcome": row.get("outcome", "observed"),
            **{k: v for k, v in row.items() if k != "event_ts_utc"},
        }
        full["event_ts_utc"] = ts
        if full["event_type"] == "order_prepared":
            full["outcome"] = "pending"
            full["instrument_class"] = "linear_perpetual"
            full["quantity_bucket"] = "min_lot"
            full["notional_bucket"] = "under_100_usd"
        if full["event_type"] == "request_sent":
            full["outcome"] = "pending"
            full["dual_leg_id"] = dual
            full["leg_id"] = row.get("leg_id") or "leg"
            full["request_fingerprint"] = row.get("request_fingerprint", _fp("x"))
            full["transport_attempt"] = 1
            full["send_monotonic_ns"] = 1_000_000_000 + i
            full["transport"] = "ws_trade"
            full["reconnect_generation"] = 0
        if full["event_type"] == "ack_received":
            full["dual_leg_id"] = dual
            full["leg_id"] = row.get("leg_id") or "leg"
            full["request_kind"] = "place"
            full["request_fingerprint"] = row.get("request_fingerprint", _fp("x"))
            full["receive_monotonic_ns"] = 1_000_000_000 + i
            full["transport"] = "ws_trade"
            full["reconnect_generation"] = 0
        if full["event_type"] == "terminal_update":
            full["dual_leg_id"] = dual
            full["leg_id"] = row.get("leg_id") or "leg"
            full["request_fingerprint"] = row.get("request_fingerprint", _fp("x"))
            full["receive_monotonic_ns"] = 1_000_000_000 + i
            full["observation_source"] = "private_ws"
            full["sequence_state"] = "healthy"
            full["reconnect_generation"] = 0
        by_day.setdefault(day, []).append(full)

    for day, day_rows in by_day.items():
        path = events_jsonl_path(root, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(r, separators=(",", ":")) for r in day_rows) + "\n",
            encoding="utf-8",
        )


def _dual_open_rows(
    *,
    dual: str,
    alias: str,
    ts: str,
    tag: str,
) -> list[dict[str, Any]]:
    return _filled_market_cycle(
        op_id=f"attempt_{tag}_by_open",
        venue="bybit",
        dual=dual,
        leg_id=f"leg_{tag}_by",
        side="buy",
        symbol_alias=alias,
        fingerprint=_fp(f"{tag}bo"),
        reduce_only=False,
        ts_utc=ts,
    ) + _filled_market_cycle(
        op_id=f"attempt_{tag}_ok_open",
        venue="okx",
        dual=dual,
        leg_id=f"leg_{tag}_ok",
        side="sell",
        symbol_alias=alias,
        fingerprint=_fp(f"{tag}oo"),
        reduce_only=False,
        ts_utc=ts,
    )


def _dual_flatten_rows(
    *,
    dual: str,
    alias: str,
    ts: str,
    tag: str,
) -> list[dict[str, Any]]:
    return _filled_market_cycle(
        op_id=f"attempt_{tag}_by_flat",
        venue="bybit",
        dual=dual,
        leg_id=f"leg_{tag}_byf",
        side="sell",
        symbol_alias=alias,
        fingerprint=_fp(f"{tag}bf"),
        reduce_only=True,
        ts_utc=ts,
    ) + _filled_market_cycle(
        op_id=f"attempt_{tag}_ok_flat",
        venue="okx",
        dual=dual,
        leg_id=f"leg_{tag}_okf",
        side="buy",
        symbol_alias=alias,
        fingerprint=_fp(f"{tag}of"),
        reduce_only=True,
        ts_utc=ts,
    )


def _supervisor_from_rows(rows: list[dict[str, Any]]) -> tuple[LeaseSupervisor, Path]:
    root = Path(tempfile.mkdtemp()) / "data"
    root.mkdir(parents=True)
    journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
    _write_raw_journal(root, journal.run_id, rows)
    supervisor = LeaseSupervisor(journal=journal, data_root=root)
    supervisor.reconstruct_from_journal(append_missing_recon=False)
    return supervisor, root


class ExposureNetKeyTests(unittest.TestCase):
    def test_base_coin_from_natives_and_aliases(self) -> None:
        self.assertEqual(base_coin_from_native_symbol("SOLUSDT"), "SOL")
        self.assertEqual(base_coin_from_native_symbol("SOL-USDT-SWAP"), "SOL")
        self.assertEqual(base_coin_from_native_symbol("XRP-USDT-PERP"), "XRP")
        self.assertEqual(base_coin_from_native_symbol("TRUMPUSDT"), "TRUMP")
        self.assertIsNone(base_coin_from_native_symbol(None))
        self.assertIsNone(base_coin_from_native_symbol(""))

    def test_exposure_net_key_binds_venue_and_coin(self) -> None:
        self.assertEqual(exposure_net_key("bybit", "SOLUSDT"), ("bybit", "SOL"))
        self.assertEqual(exposure_net_key("okx_live", "SOL-USDT-SWAP"), ("okx", "SOL"))
        self.assertEqual(venue_short_name("bybit_live"), "bybit")
        self.assertNotEqual(
            exposure_net_key("bybit", "SOLUSDT"),
            exposure_net_key("bybit", "XRPUSDT"),
        )


class CrossDualExposureRetireTests(unittest.TestCase):
    def test_later_flatten_different_dual_retires_open_exposure(self) -> None:
        """(a) open dual A + later reduce-only dual B → no exposure leftover for A."""
        dual_a = "dual_5a86618e2bd3431a96e559ae1d330e19"
        dual_b = "dual_acce9dec74dd4ecca2c5afd1d7bfe256"
        rows = _dual_open_rows(
            dual=dual_a,
            alias="SOL-USDT-PERP",
            ts="2026-08-30T23:17:25.000Z",
            tag="a",
        ) + _dual_flatten_rows(
            dual=dual_b,
            alias="SOL-USDT-PERP",
            ts="2026-09-02T09:11:00.000Z",
            tag="b",
        )
        supervisor, _root = _supervisor_from_rows(rows)
        self.assertIsNone(supervisor.get(f"exposure_flatten_{dual_a}"))
        self.assertIsNone(supervisor.get(f"exposure_flatten_{dual_b}"))
        self.assertFalse(supervisor.has_blocking_lease())

    def test_unflattened_open_still_mints_exposure(self) -> None:
        """(b) SOL leftover with no flatten still blocks."""
        dual_a = "dual_leftover_still_open"
        rows = _dual_open_rows(
            dual=dual_a,
            alias="SOL-USDT-PERP",
            ts="2026-08-30T23:17:25.000Z",
            tag="a",
        )
        supervisor, _root = _supervisor_from_rows(rows)
        exposure = supervisor.get(f"exposure_flatten_{dual_a}")
        self.assertIsNotNone(exposure)
        assert exposure is not None
        self.assertEqual(exposure.plan.symbol, "SOLUSDT")
        self.assertTrue(supervisor.has_blocking_lease())

    def test_sequential_sol_cycles_only_latest_unflattened_remains(self) -> None:
        """(d) open/close/open → only the latest open dual keeps exposure."""
        dual_a = "dual_cycle_a"
        dual_b = "dual_cycle_b_flat"
        dual_c = "dual_cycle_c_reopen"
        rows = (
            _dual_open_rows(
                dual=dual_a,
                alias="SOL-USDT-PERP",
                ts="2026-08-30T10:00:00.000Z",
                tag="a",
            )
            + _dual_flatten_rows(
                dual=dual_b,
                alias="SOL-USDT-PERP",
                ts="2026-09-02T09:11:00.000Z",
                tag="b",
            )
            + _dual_open_rows(
                dual=dual_c,
                alias="SOL-USDT-PERP",
                ts="2026-09-02T10:01:00.000Z",
                tag="c",
            )
        )
        supervisor, _root = _supervisor_from_rows(rows)
        self.assertIsNone(supervisor.get(f"exposure_flatten_{dual_a}"))
        self.assertIsNone(supervisor.get(f"exposure_flatten_{dual_b}"))
        leftover = supervisor.get(f"exposure_flatten_{dual_c}")
        self.assertIsNotNone(leftover)
        assert leftover is not None
        self.assertEqual(leftover.plan.symbol, "SOLUSDT")
        self.assertEqual(leftover.plan.dual_leg_id, dual_c)

    def test_terminal_ts_propagates_from_journal(self) -> None:
        dual = "dual_ts_check"
        rows = _dual_open_rows(
            dual=dual,
            alias="SOL-USDT-PERP",
            ts="2026-08-30T23:17:25.000Z",
            tag="t",
        )
        root = Path(tempfile.mkdtemp()) / "data"
        root.mkdir(parents=True)
        journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
        _write_raw_journal(root, journal.run_id, rows)
        legs = reconstruct_legs_from_events(scan_all_journal_events(root))
        self.assertEqual(
            legs["attempt_t_by_open"].terminal_ts_utc,
            "2026-08-30T23:17:25.000Z",
        )


class XrpOpenNotBlockedByRetiredSolTests(unittest.TestCase):
    def _stub_runtime(self) -> MagicMock:
        rt = MagicMock()
        rt.sends_blocked = False
        rt.reseed_required = False
        return rt

    def test_xrp_open_not_recovery_blocked_after_cross_dual_sol_flatten(self) -> None:
        """(c) After SOL open A + flatten B, XRP open recover is not recovery_blocked."""
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_sender import ApprovalBoundSender

        dual_a = "dual_5a86618e2bd3431a96e559ae1d330e19"
        dual_b = "dual_acce9dec74dd4ecca2c5afd1d7bfe256"
        rows = _dual_open_rows(
            dual=dual_a,
            alias="SOL-USDT-PERP",
            ts="2026-08-30T23:17:25.000Z",
            tag="a",
        ) + _dual_flatten_rows(
            dual=dual_b,
            alias="SOL-USDT-PERP",
            ts="2026-09-02T09:11:00.000Z",
            tag="b",
        )

        root = Path(tempfile.mkdtemp()) / "data"
        root.mkdir(parents=True)
        journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
        _write_raw_journal(root, journal.run_id, rows)
        vault = ApprovalVault(
            journal=journal, hmac_key=b"unit-test-approval-key-32bytes!!"
        )
        supervisor = LeaseSupervisor(journal=journal, data_root=root)
        supervisor.reconstruct_from_journal(append_missing_recon=False)
        self.assertIsNone(supervisor.get(f"exposure_flatten_{dual_a}"))
        self.assertFalse(supervisor.has_blocking_lease())

        sender = ApprovalBoundSender(
            journal=journal,
            approval_vault=vault,
            metadata_provider=MagicMock(),
            position_mode_provider=MagicMock(),
            transport=MagicMock(),
            lease_supervisor=supervisor,
            data_root=root,
        )
        # Sender init may reconstruct again; ensure leftover still gone.
        sender.lease_supervisor.reconstruct_from_journal(append_missing_recon=False)
        self.assertFalse(sender.lease_supervisor.has_blocking_lease())

        baseline = DualFlatBaseline(
            bybit=MagicMock(check=MagicMock(return_value=MagicMock(ok=True))),
            okx=MagicMock(check=MagicMock(return_value=MagicMock(ok=True))),
        )
        err: Optional[str] = _recover_inflight_w6(
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
            bybit_profile=_xrp_bybit_profile(),
            okx_profile=_xrp_okx_profile(),
            flatten_only=False,
        )
        self.assertIsNone(
            err,
            "flat slot + retired SOL dual must not recovery_block XRP open",
        )

    def test_live_sol_leftover_still_blocks_xrp_open(self) -> None:
        """Unflattened SOL exposure must still recovery_block a different-coin open."""
        from app.bot.private.order_approval import ApprovalVault
        from app.bot.private.order_sender import ApprovalBoundSender

        dual_a = "dual_sol_still_held"
        rows = _dual_open_rows(
            dual=dual_a,
            alias="SOL-USDT-PERP",
            ts="2026-08-30T23:17:25.000Z",
            tag="a",
        )
        root = Path(tempfile.mkdtemp()) / "data"
        root.mkdir(parents=True)
        journal = PrivateJournalWriter(root, run_id=new_opaque_id("run"))
        _write_raw_journal(root, journal.run_id, rows)
        vault = ApprovalVault(
            journal=journal, hmac_key=b"unit-test-approval-key-32bytes!!"
        )
        supervisor = LeaseSupervisor(journal=journal, data_root=root)
        supervisor.reconstruct_from_journal(append_missing_recon=False)
        self.assertIsNotNone(supervisor.get(f"exposure_flatten_{dual_a}"))

        sender = ApprovalBoundSender(
            journal=journal,
            approval_vault=vault,
            metadata_provider=MagicMock(),
            position_mode_provider=MagicMock(),
            transport=MagicMock(),
            lease_supervisor=supervisor,
            data_root=root,
        )
        sender.lease_supervisor.reconstruct_from_journal(append_missing_recon=False)
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
            bybit_profile=_xrp_bybit_profile(),
            okx_profile=_xrp_okx_profile(),
            flatten_only=False,
        )
        self.assertEqual(err, "recovery_blocked")


if __name__ == "__main__":
    unittest.main()

"""No-order companion status must fail closed on stale or dead processes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.bot.private.readiness_status import ReadonlyStatusWriter, read_readonly_status
from app.bot.execution.readiness import snapshot_from_readonly_companions
from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.ws_private import RestReseedResult
from app.bot.private.ws_readonly import run_ws_readonly_preflight
from app.bot.private.ws_socket import FakePrivateWsSocket


def runtime(*, connected: bool = True, matched: bool = True):
    return SimpleNamespace(
        private_socket=SimpleNamespace(connected=connected),
        authenticated=True,
        subscription_readiness=SimpleNamespace(value="ready"),
        sequence_state=SimpleNamespace(value="healthy"),
        reseed_required=not matched,
        sends_blocked=not matched,
        reconnect_generation=4,
    )


class ReadonlyStatusTests(unittest.TestCase):
    def test_companion_publishes_ready_then_revokes_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "bybit.json"
            observed: list[tuple[bool, int]] = []

            class InspectSocket(FakePrivateWsSocket):
                calls = 0

                def recv_text(self, *, timeout_sec=None):
                    self.calls += 1
                    if self.calls == 3:
                        observed.append(read_readonly_status(path, "bybit", max_age_ns=1_000_000_000))
                    return super().recv_text(timeout_sec=timeout_sec)

            socket = InspectSocket()
            socket.push_inbound(json.dumps({"op": "auth", "success": True, "retCode": 0}))
            socket.push_inbound(json.dumps({"op": "subscribe", "success": True}))
            with patch("app.bot.private.readiness_status._linux_identity", return_value=("boot", "123")):
                report = run_ws_readonly_preflight(
                    exchange="bybit",
                    env={"VENUE": "live", "LIVE_ORDERS": "0"},
                    private_socket=socket,
                    rest_probe_fn=lambda **_kwargs: RestReseedResult(matched=True),
                    credentials=LiveCredentials(api_key="k", api_secret="s"),
                    journal=PrivateJournalWriter(root, run_id=new_opaque_id("run")),
                    load_secrets=False,
                    max_cycles=1,
                    recv_timeout_sec=0.0,
                    status_path=path,
                )
                self.assertEqual(report.status, "ok")
                self.assertEqual(observed, [(True, 0)])
                self.assertEqual(read_readonly_status(path, "bybit", max_age_ns=1_000_000_000), (False, 0))

    def test_dual_snapshot_is_private_only_and_fails_closed_per_venue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bybit = Path(td) / "bybit.json"
            okx = Path(td) / "okx.json"
            with patch("app.bot.private.readiness_status._linux_identity", return_value=("boot", "123")):
                bybit_writer = ReadonlyStatusWriter(bybit, "bybit")
                okx_writer = ReadonlyStatusWriter(okx, "okx")
                bybit_writer.publish(runtime(), ready=True)
                okx_writer.publish(runtime(), ready=True)
                snapshot = snapshot_from_readonly_companions(bybit, okx, max_age_ns=1_000_000_000)
                self.assertTrue(snapshot.bybit_private_ready)
                self.assertTrue(snapshot.okx_private_ready)
                self.assertFalse(snapshot.bybit_trade_ready)
                self.assertFalse(snapshot.okx_trade_ready)
                okx_writer.publish(runtime(connected=False), ready=True)
                snapshot = snapshot_from_readonly_companions(bybit, okx, max_age_ns=1_000_000_000)
                self.assertTrue(snapshot.bybit_private_ready)
                self.assertFalse(snapshot.okx_private_ready)

    def test_ready_requires_live_identity_freshness_and_all_private_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bybit.json"
            with patch("app.bot.private.readiness_status._linux_identity", return_value=("boot", "123")):
                writer = ReadonlyStatusWriter(path, "bybit", clock_ns=lambda: 100)
                writer.publish(runtime(), ready=True)
                self.assertEqual(
                    read_readonly_status(path, "bybit", max_age_ns=10, clock_ns=lambda: 109),
                    (True, 4),
                )
                self.assertEqual(
                    read_readonly_status(path, "bybit", max_age_ns=10, clock_ns=lambda: 111),
                    (False, 0),
                )
                self.assertEqual(
                    read_readonly_status(path, "okx", max_age_ns=10, clock_ns=lambda: 101),
                    (False, 0),
                )
                writer.publish(runtime(connected=False), ready=True)
                self.assertEqual(
                    read_readonly_status(path, "bybit", max_age_ns=10, clock_ns=lambda: 101),
                    (False, 0),
                )
                writer.publish(runtime(matched=False), ready=True)
                self.assertEqual(
                    read_readonly_status(path, "bybit", max_age_ns=10, clock_ns=lambda: 101),
                    (False, 0),
                )

    def test_exit_pid_reuse_corruption_and_symlink_are_unready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "okx.json"
            with patch("app.bot.private.readiness_status._linux_identity", return_value=("boot", "123")):
                writer = ReadonlyStatusWriter(path, "okx", clock_ns=lambda: 100)
                writer.publish(runtime(), ready=True)
                writer.publish(runtime(), ready=False)
                self.assertEqual(read_readonly_status(path, "okx", max_age_ns=10, clock_ns=lambda: 101), (False, 0))
                writer.publish(runtime(), ready=True)
            with patch("app.bot.private.readiness_status._linux_identity", return_value=("boot", "456")):
                self.assertEqual(read_readonly_status(path, "okx", max_age_ns=10, clock_ns=lambda: 101), (False, 0))
            path.write_text(json.dumps({"ready": True}), encoding="ascii")
            self.assertEqual(read_readonly_status(path, "okx", max_age_ns=10, clock_ns=lambda: 101), (False, 0))
            alias = Path(td) / "alias.json"
            alias.symlink_to(path)
            self.assertEqual(read_readonly_status(alias, "okx", max_age_ns=10, clock_ns=lambda: 101), (False, 0))

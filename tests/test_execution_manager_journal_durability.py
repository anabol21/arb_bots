"""EV2-12B2a: first K=1 lifecycle row survives directory-entry loss."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bot.theta_trade_manager import (
    OpenPosition,
    SCHEMA_VERSION,
    ThetaTradeConfig,
    ThetaTradeJournalWriter,
    ThetaTradeManager,
    ThetaTradeRecoveryError,
    replay_theta_trade_history,
)


class ManagerJournalDirectoryDurabilityTests(unittest.TestCase):
    def test_first_lifecycle_row_fsyncs_all_new_directory_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            row = {
                "schema_version": SCHEMA_VERSION,
                "trade_id": "trade-a",
                "base_coin": "BTC",
                "side": "long",
                "event": "open",
                "would_send": True,
                "send": False,
                "live_send": False,
                "signal_ts_ms": 1_700_000_000_000,
                "fill_ts_ms": 1_700_000_000_070,
                "spread_fill": 0.2,
                "notional_usdt": 20.0,
                "policy_id": "test",
            }
            opened_dirs: dict[int, Path] = {}
            fsynced_dirs: list[Path] = []
            real_open = os.open
            real_fsync = os.fsync

            def tracked_open(path: str | os.PathLike[str], flags: int, *args: object) -> int:
                fd = real_open(path, flags, *args)
                opened_dirs[fd] = Path(path)
                return fd

            def tracked_fsync(fd: int) -> None:
                if fd in opened_dirs:
                    fsynced_dirs.append(opened_dirs[fd])
                real_fsync(fd)

            with patch("app.bot.theta_trade_manager.os.open", side_effect=tracked_open), patch(
                "app.bot.theta_trade_manager.os.fsync", side_effect=tracked_fsync
            ):
                paths = ThetaTradeJournalWriter(root).append_rows([row])

            self.assertEqual(len(paths), 1)
            self.assertEqual(
                fsynced_dirs,
                [paths[0].parent, paths[0].parent.parent, root],
            )
            replay = replay_theta_trade_history(root, expected_policy_id="test")
            self.assertEqual(replay.position.trade_id if replay.position else None, "trade-a")

    def test_data_root_fsync_failure_never_publishes_k1_slot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = ThetaTradeManager(
                data_root=root,
                config=ThetaTradeConfig(policy_id="test"),
            )
            row = {
                "schema_version": SCHEMA_VERSION,
                "trade_id": "trade-a",
                "base_coin": "BTC",
                "side": "long",
                "event": "open",
                "would_send": True,
                "send": False,
                "live_send": False,
                "signal_ts_ms": 1_700_000_000_000,
                "fill_ts_ms": 1_700_000_000_070,
                "spread_fill": 0.2,
                "notional_usdt": 20.0,
                "policy_id": "test",
            }
            position = OpenPosition(
                trade_id="trade-a", base_coin="BTC", side="long",
                open_signal_ts_ms=1_700_000_000_000,
                open_fill_ts_ms=1_700_000_000_070,
                open_fill_spread=0.2, open_notional=20.0,
                open_theta_1m=None,
            )
            root_fd: int | None = None
            real_open = os.open
            real_fsync = os.fsync

            def tracked_open(path: str | os.PathLike[str], flags: int, *args: object) -> int:
                nonlocal root_fd
                fd = real_open(path, flags, *args)
                if Path(path) == root:
                    root_fd = fd
                return fd

            def fail_root_fsync(fd: int) -> None:
                if fd == root_fd:
                    raise OSError("injected data-root fsync failure")
                real_fsync(fd)

            with patch("app.bot.theta_trade_manager.os.open", side_effect=tracked_open), patch(
                "app.bot.theta_trade_manager.os.fsync", side_effect=fail_root_fsync
            ):
                with self.assertRaisesRegex(ThetaTradeRecoveryError, "trade_history_write_failed"):
                    manager._commit_lifecycle_row(row, position_after=position)

            self.assertIsNone(manager.slot.position)
            self.assertTrue(manager.recovery_blocked)


if __name__ == "__main__":
    unittest.main()

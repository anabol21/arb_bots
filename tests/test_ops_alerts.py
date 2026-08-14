from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from validation import ops_alerts


class OpsAlertsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.data = root / "data"
        self.live = self.data / "live"
        self.compacted = self.data / "compacted"
        self.archive = self.live / "archived"
        self.spool = self.data / "spool"
        self.bars = self.data / "bars"
        self.logs = root / "logs"
        for path in (
            self.live,
            self.compacted,
            self.archive,
            self.spool,
            self.bars / "bar_5m",
            self.logs,
        ):
            path.mkdir(parents=True)
        self.compactor_log = self.logs / "compactor.log"
        self.transfer_log = self.logs / "transfer.log"
        self.bars_transfer_log = self.logs / "bars-transfer.log"
        self.runtime_log = self.logs / "runtime.log"
        now = time.time()
        self.compactor_log.write_text(
            json.dumps({"timestamp": now, "event": "compaction_complete"})
            + "\n"
            + json.dumps({"timestamp": now, "event": "archive_retention_complete"})
            + "\n",
            encoding="utf-8",
        )
        self.transfer_log.write_text("", encoding="utf-8")
        self.bars_transfer_log.write_text("", encoding="utf-8")
        self.runtime_log.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _args(self, **overrides: object) -> SimpleNamespace:
        base = dict(
            data_root=self.data,
            live=self.live,
            compacted=self.compacted,
            archive=self.archive,
            spool=self.spool,
            bars=self.bars,
            compactor_log=self.compactor_log,
            transfer_log=self.transfer_log,
            bars_transfer_log=self.bars_transfer_log,
            runtime_log=self.runtime_log,
            min_free_gb=0.0000001,
            max_backlog_files=20,
            max_backlog_mb=512.0,
            max_archive_age_hours=36.0,
            max_spool_files=100,
            max_bars_backlog_age_minutes=60.0,
            lookback_sec=900.0,
            max_compaction_lag_minutes=30.0,
            max_missing_complete_cycles=3,
            cycle_seconds=300.0,
            max_live_growth_mb=256.0,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_ok_when_thresholds_clear(self) -> None:
        code = ops_alerts.run_checks(self._args())
        self.assertEqual(code, 0)

    def test_backlog_and_compaction_alert(self) -> None:
        (self.compacted / "spread_x.parquet").write_bytes(b"1234567890")
        self.compactor_log.write_text(
            json.dumps(
                {"timestamp": time.time(), "event": "compaction_alert"}
            )
            + "\n",
            encoding="utf-8",
        )
        code = ops_alerts.run_checks(
            self._args(max_backlog_files=0, max_backlog_mb=0.0)
        )
        self.assertEqual(code, 1)

    def test_zero_watchdog_counter_is_not_an_alert(self) -> None:
        self.transfer_log.write_text(
            json.dumps(
                {
                    "timestamp": time.time(),
                    "event": "backup_summary",
                    "transfer_watchdog_kills": 0,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "timestamp": time.time(),
                    "event": "backup_summary",
                    "transfer_watchdog_kills": 2,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        code = ops_alerts.run_checks(self._args())
        self.assertEqual(code, 1)

    def test_old_active_bars_backlog_is_not_healthy(self) -> None:
        bar = self.bars / "bar_5m" / "base_coin=BTC" / "batch.parquet"
        bar.parent.mkdir()
        bar.write_bytes(b"bar")
        old = time.time() - 61 * 60
        os.utime(bar, (old, old))

        code = ops_alerts.run_checks(
            self._args(max_bars_backlog_age_minutes=60.0)
        )

        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()

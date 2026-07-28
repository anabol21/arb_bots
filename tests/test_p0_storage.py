from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.mount_state import MountFailureState
from app.storage.spool import DurableSpool
from app.storage.writer import ParquetPublisher


def valid_record(base_coin: str = "BTC") -> dict[str, object]:
    return {
        "base_coin": base_coin,
        "trigger": "okx",
        "calc_local_ts_ms": 1_700_000_000_100,
        "okx_local_recv_ts_ms": 1_700_000_000_000,
        "bybit_local_recv_ts_ms": 1_700_000_000_050,
        "okx_latency_ms": 10,
        "bybit_latency_ms": 20,
        "spread_long": 1.0,
        "spread_short": -1.0,
    }


class P0StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.mount_state = MountFailureState()
        self.logger = logging.getLogger(f"p0-storage-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.logger.propagate = False
        self.spool = DurableSpool(
            logger=self.logger,
            mount_failure_state=self.mount_state,
            root=root / "spool",
        )
        self.publisher = ParquetPublisher(
            parquet_root=root / "mount",
            logger=self.logger,
            failed_batches_logger=self.logger,
            mount_failure_state=self.mount_state,
            spool=self.spool,
            max_queue=1,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_queue_accepts_raw_records_and_suppresses_full_queue_amplification(self) -> None:
        self.publisher._started = True
        records = [valid_record()]
        self.assertTrue(self.publisher.enqueue_records(records))
        queued = self.publisher._queue.queue[0]
        self.assertIs(queued["records"], records)

        self.assertFalse(self.publisher.ready_for_enqueue(100_000))
        self.assertFalse(self.publisher.ready_for_enqueue(100_001))
        self.assertEqual(
            self.publisher.metrics_snapshot()["backpressure_hits_total"],
            1,
        )

    def test_force_spool_accounts_valid_and_rejected_records_durably(self) -> None:
        records = [valid_record(), {"base_coin": "", "trigger": "okx"}]

        self.assertTrue(
            self.publisher.durably_spool_records(
                records,
                reason="mount_failure_shutdown",
            )
        )

        parquet_files = self.spool.iter_spool_files()
        quarantine_files = list((self.spool.root / "_quarantine").glob("*.json"))
        self.assertEqual(len(parquet_files), 1)
        self.assertEqual(len(quarantine_files), 1)
        payload = json.loads(quarantine_files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["record_count"], 1)
        self.assertIn("invalid_base_coin", payload["records"][0]["reasons"])
        self.assertIn("invalid_event_dt", payload["records"][0]["reasons"])

        metrics = self.publisher.metrics_snapshot()
        self.assertEqual(metrics["accepted_records_total"], 1)
        self.assertEqual(metrics["rejected_records_total"], 1)
        self.assertEqual(metrics["quarantined_records_total"], 1)

    def test_non_mount_parquet_error_falls_back_to_local_spool(self) -> None:
        original_write_table = __import__(
            "app.storage.writer",
            fromlist=["pq"],
        ).pq.write_table

        def fail_only_mounted_write(table, where, *args, **kwargs):
            if str(where).startswith(str(self.publisher.parquet_root)):
                raise ValueError("synthetic schema/parquet failure")
            return original_write_table(table, where, *args, **kwargs)

        job = {
            "job_id": "job-1",
            "records": [valid_record()],
            "rows": 1,
            "enqueued_at": 0.0,
        }
        with (
            mock.patch(
                "app.storage.writer.assert_storage_mount_writable",
                return_value=None,
            ),
            mock.patch(
                "app.storage.writer.is_mount_failure_error",
                return_value=False,
            ),
            mock.patch(
                "app.storage.writer.pq.write_table",
                side_effect=fail_only_mounted_write,
            ),
        ):
            outcome = self.publisher._publish_job(job)

        self.assertEqual(outcome, "locally_spooled")
        self.assertEqual(len(self.spool.iter_spool_files()), 1)
        self.assertFalse(self.mount_state.is_dead())

    def test_normal_shutdown_full_queue_spools_and_clears_runtime_buffer(self) -> None:
        root = Path(self.temp_dir.name)
        env = {
            "SPREAD_PARQUET_ROOT": str(root / "runtime-mount"),
            "SPREAD_RUNTIME_LOG": str(root / "runtime.log"),
            "SPREAD_FAILED_BATCHES_LOG": str(root / "failed.log"),
        }
        with mock.patch.dict(os.environ, env):
            runtime = importlib.import_module("app.screaner_b_o")

        try:
            self.publisher._started = True
            self.assertTrue(self.publisher.enqueue_records([valid_record("ETH")]))
            runtime.publisher = self.publisher
            runtime.opportunities_buffer[:] = [valid_record("BTC")]

            self.assertTrue(runtime.flush_opportunities_for_shutdown())

            self.assertEqual(runtime.opportunities_buffer, [])
            self.assertEqual(len(self.spool.iter_spool_files()), 1)
            metrics = self.publisher.metrics_snapshot()
            self.assertEqual(metrics["accepted_records_total"], 1)
            self.assertEqual(metrics["rejected_records_total"], 0)
        finally:
            runtime.publisher = None
            runtime.opportunities_buffer.clear()
            for logger in (runtime.runtime_logger, runtime.failed_batches_logger):
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()
            sys.modules.pop("app.screaner_b_o", None)


if __name__ == "__main__":
    unittest.main()

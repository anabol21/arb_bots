from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyarrow.parquet as pq

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

    def test_publisher_uses_local_tmp_then_final_parquet(self) -> None:
        root = Path(self.temp_dir.name)
        local_primary = root / "data" / "live"
        publisher = ParquetPublisher(
            parquet_root=local_primary,
            logger=self.logger,
            failed_batches_logger=self.logger,
            mount_failure_state=self.mount_state,
            spool=self.spool,
            max_queue=1,
        )

        publisher.start()
        self.assertTrue(publisher.enqueue_records([valid_record()]))
        publisher.shutdown()

        finals = list(local_primary.rglob("*.parquet"))
        self.assertEqual(len(finals), 1)
        self.assertEqual(pq.ParquetFile(finals[0]).metadata.num_rows, 1)
        self.assertEqual(list((local_primary / ".tmp").glob("*")), [])

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
                "app.storage.writer.assert_storage_root_writable",
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

    def _runtime_path_env(self, root: Path) -> dict[str, str]:
        return {
            "SPREAD_PARQUET_ROOT": str(root / "runtime-mount"),
            "SPREAD_RUNTIME_LOG": str(root / "runtime.log"),
            "SPREAD_FAILED_BATCHES_LOG": str(root / "failed.log"),
            "SPREAD_SPOOL_ROOT": str(root / "spool"),
        }

    def _cleanup_runtime(self, runtime: object) -> None:
        publisher = getattr(runtime, "publisher", None)
        if publisher is not None:
            publisher.shutdown()
        bars_publisher = getattr(runtime, "bars_publisher", None)
        if bars_publisher is not None:
            bars_publisher.shutdown()
        recovery_worker = getattr(runtime, "recovery_worker", None)
        if recovery_worker is not None:
            recovery_worker.shutdown()
        bars_recovery_worker = getattr(runtime, "bars_recovery_worker", None)
        if bars_recovery_worker is not None:
            bars_recovery_worker.shutdown()
        runtime.publisher = None
        runtime.bars_publisher = None
        runtime.recovery_worker = None
        runtime.bars_recovery_worker = None
        runtime.spool = None
        runtime.bars_spool = None
        runtime.opportunities_buffer.clear()
        if hasattr(runtime, "bar_buffer"):
            runtime.bar_buffer.clear()
        if hasattr(runtime, "seen_bar_keys"):
            runtime.seen_bar_keys.clear()
        for logger in (runtime.runtime_logger, runtime.failed_batches_logger):
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        sys.modules.pop("app.screaner_b_o", None)

    async def _wait_until_main_ready(self, runtime: object, timeout_sec: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            if (
                getattr(runtime, "publisher", None) is not None
                and getattr(runtime, "spool", None) is not None
                and getattr(runtime, "recovery_worker", None) is not None
            ):
                return
            await asyncio.sleep(0.01)
        raise TimeoutError("main() did not finish storage startup in time")

    def test_expected_cancel_after_successful_flush_exits_cleanly(self) -> None:
        root = Path(self.temp_dir.name)
        env = self._runtime_path_env(root)
        # Keep path envs for import AND main(): DurableSpool resolves SPREAD_SPOOL_ROOT
        # at construction time, not at module import.
        with mock.patch.dict(os.environ, env):
            sys.modules.pop("app.screaner_b_o", None)
            runtime = importlib.import_module("app.screaner_b_o")

            async def _exercise() -> None:
                runtime.pairs = []
                runtime.opportunities_buffer.clear()
                runtime.mount_failure_state = MountFailureState()
                with mock.patch.object(runtime, "assert_storage_root_writable"):
                    main_task = asyncio.create_task(runtime.main())
                    await self._wait_until_main_ready(runtime)
                    main_task.cancel()
                    await main_task

            try:
                asyncio.run(_exercise())
            finally:
                self._cleanup_runtime(runtime)

    def test_cancel_with_mount_failure_still_exits_nonzero(self) -> None:
        root = Path(self.temp_dir.name)
        env = self._runtime_path_env(root)
        with mock.patch.dict(os.environ, env):
            sys.modules.pop("app.screaner_b_o", None)
            runtime = importlib.import_module("app.screaner_b_o")

            async def _exercise() -> None:
                runtime.pairs = []
                runtime.opportunities_buffer.clear()
                runtime.mount_failure_state = MountFailureState()
                with mock.patch.object(runtime, "assert_storage_root_writable"):
                    main_task = asyncio.create_task(runtime.main())
                    # Wait until monitor_primary_storage_failure is running so
                    # mark_dead cannot race startup before the watcher exists.
                    await self._wait_until_main_ready(runtime)
                    # Give the monitor one poll interval to be parked in sleep.
                    await asyncio.sleep(0.12)
                    runtime.mount_failure_state.mark_dead(
                        source="test",
                        reason="synthetic_primary_failure",
                    )
                    with self.assertRaises(RuntimeError) as raised:
                        await asyncio.wait_for(main_task, timeout=2.0)
                    self.assertIn("primary storage failure", str(raised.exception))

            try:
                asyncio.run(_exercise())
            finally:
                self._cleanup_runtime(runtime)


if __name__ == "__main__":
    unittest.main()

"""Periodic recovery of durable local spool files to mounted storage."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import uuid
from pathlib import Path

import pyarrow.parquet as pq

from .mount_state import MountFailureState
from .paths import (
    assert_storage_mount_writable,
    is_mount_failure_error,
    partition_dir,
    tmp_dir,
)
from .spool import DurableSpool, SpoolQuotaExceeded

DEFAULT_RECOVERY_INTERVAL_SEC = 30.0


class SpoolRecoveryWorker:
    """Recover final spool files idempotently using their original batch IDs."""

    def __init__(
        self,
        *,
        spool: DurableSpool,
        parquet_root: Path,
        logger: logging.Logger,
        mount_failure_state: MountFailureState,
    ) -> None:
        raw_interval = os.environ.get("SPREAD_SPOOL_RECOVERY_INTERVAL_SEC")
        self.interval_sec = (
            DEFAULT_RECOVERY_INTERVAL_SEC
            if raw_interval is None
            else float(raw_interval)
        )
        if self.interval_sec <= 0:
            raise ValueError("SPREAD_SPOOL_RECOVERY_INTERVAL_SEC must be > 0")
        self.spool = spool
        self.parquet_root = parquet_root
        self.logger = logger
        self.mount_failure_state = mount_failure_state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name="spool-recovery",
            daemon=True,
        )
        self._thread.start()
        self.logger.info(
            "spool_recovery_started | root=%s | interval_sec=%s",
            self.spool.root,
            self.interval_sec,
        )

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=min(self.interval_sec + 1.0, 5.0))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._recover_once()
            except SpoolQuotaExceeded:
                return
            except Exception as exc:
                self.spool.mark_recovery_failed()
                self.logger.error(
                    "spool_recovery_failed | reason=worker_error | error=%r",
                    exc,
                )
            self._stop.wait(self.interval_sec)

    def _recover_once(self) -> None:
        self.spool.refresh_inventory_and_monitor()
        files = self.spool.iter_spool_files()
        if not files:
            return

        try:
            assert_storage_mount_writable()
        except Exception as exc:
            self.spool.mark_recovery_failed()
            self.mount_failure_state.mark_dead(
                source="recovery",
                reason=repr(exc),
            )
            self.logger.critical(
                "mount_lost | source=recovery | error=%r",
                exc,
            )
            return

        for spool_path in files:
            if self._stop.is_set() or self.mount_failure_state.is_dead():
                return
            try:
                final_path, batch_id = self._publish_spool_file(spool_path)
                self.spool.mark_recovered(spool_path)
                self.logger.info(
                    "spool_recovered | batch_id=%s | spool_path=%s | path=%s",
                    batch_id,
                    spool_path,
                    final_path,
                )
            except Exception as exc:
                self.spool.mark_recovery_failed()
                self.logger.error(
                    "spool_recovery_failed | spool_path=%s | error=%r",
                    spool_path,
                    exc,
                )
                if is_mount_failure_error(exc):
                    self.mount_failure_state.mark_dead(
                        source="recovery",
                        reason=repr(exc),
                        batch_id=self._batch_id(spool_path),
                    )
                    return

    def _publish_spool_file(self, spool_path: Path) -> tuple[Path, str]:
        base_coin = spool_path.parent.parent.name
        event_date = spool_path.parent.name
        batch_id = self._batch_id(spool_path)
        final_path = (
            partition_dir(self.parquet_root, base_coin, event_date)
            / f"batch_{batch_id}.parquet"
        )
        expected = pq.ParquetFile(spool_path)
        expected_rows = expected.metadata.num_rows
        expected_schema = expected.schema_arrow

        if final_path.exists():
            self._validate_published(
                final_path,
                expected_rows=expected_rows,
                expected_schema=expected_schema,
            )
            return final_path, batch_id

        assert_storage_mount_writable(probe_write=False)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        recovery_tmp = (
            tmp_dir(self.parquet_root)
            / f"batch_{batch_id}.recovery_{uuid.uuid4().hex}.parquet.tmp"
        )
        try:
            with spool_path.open("rb") as source, recovery_tmp.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())

            self._validate_published(
                recovery_tmp,
                expected_rows=expected_rows,
                expected_schema=expected_schema,
            )
            os.replace(recovery_tmp, final_path)
            self._fsync_dir(final_path.parent)
            self._validate_published(
                final_path,
                expected_rows=expected_rows,
                expected_schema=expected_schema,
            )
            return final_path, batch_id
        except Exception as exc:
            if not is_mount_failure_error(exc):
                recovery_tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _batch_id(path: Path) -> str:
        name = path.name
        if not name.startswith("batch_") or not name.endswith(".parquet"):
            raise ValueError(f"invalid spool batch filename: {path}")
        return name[len("batch_"):-len(".parquet")]

    @staticmethod
    def _validate_published(
        path: Path,
        *,
        expected_rows: int,
        expected_schema,
    ) -> None:
        published = pq.ParquetFile(path)
        if published.metadata.num_rows != expected_rows:
            raise ValueError(
                f"recovery row mismatch: expected={expected_rows} "
                f"got={published.metadata.num_rows}"
            )
        if not published.schema_arrow.equals(expected_schema):
            raise ValueError(f"recovery schema mismatch: path={path}")

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

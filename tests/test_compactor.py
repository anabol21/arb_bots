from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq

from app.storage.compactor import CompactorConfig, compact_once, run_archive_retention_only


NOW = 1_700_000_700.0
COMPLETED_MTIME = (int(NOW // 300) - 2) * 300 + 10
CURRENT_MTIME = int(NOW // 300) * 300 + 10


class CompactorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.live = root / "live"
        self.compacted = root / "compacted"
        self.live.mkdir()
        self.logger = logging.getLogger(f"compactor-test-{id(self)}")
        self.logger.handlers = [logging.NullHandler()]
        self.logger.propagate = False
        self.config = CompactorConfig(
            live_root=self.live,
            compacted_root=self.compacted,
            retention_hours=24,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(
        self,
        relative: str,
        values: list[int],
        *,
        mtime: float = COMPLETED_MTIME,
    ) -> Path:
        path = self.live / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            pa.table({"value": values, "symbol": ["BTC"] * len(values)}),
            path,
        )
        os.utime(path, (mtime, mtime))
        return path

    def _manifests(self) -> list[Path]:
        return list((self.compacted / ".state").glob("*.json"))

    def _outputs(self) -> list[Path]:
        return list(self.compacted.glob("spread_*.parquet"))

    def test_happy_path_compacts_and_archives_sources(self) -> None:
        first = self._write("coin=BTC/part-a.parquet", [1, 2])
        second = self._write("coin=ETH/part-b.parquet", [3])

        result = compact_once(self.config, now_epoch=NOW, logger=self.logger)

        self.assertEqual(result, 0)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue((self.live / "archived/coin=BTC/part-a.parquet").is_file())
        self.assertTrue((self.live / "archived/coin=ETH/part-b.parquet").is_file())
        outputs = self._outputs()
        self.assertEqual(len(outputs), 1)
        self.assertEqual(pq.ParquetFile(outputs[0]).metadata.num_rows, 3)
        manifest = json.loads(self._manifests()[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(
            [source["path"] for source in manifest["sources"]],
            ["coin=BTC/part-a.parquet", "coin=ETH/part-b.parquet"],
        )
        self.assertEqual(manifest["total_rows"], 3)

    def test_excludes_current_interval_tmp_and_archived_files(self) -> None:
        eligible = self._write("eligible.parquet", [1])
        current = self._write("current.parquet", [2], mtime=CURRENT_MTIME)
        temporary = self._write("ignored.inprogress.parquet", [3])
        archived = self._write(
            "archived/already.parquet",
            [4],
            mtime=NOW - 25 * 3600,
        )
        trailing_tmp = self.live / "ignored.parquet.tmp"
        trailing_tmp.write_bytes(b"not parquet")
        os.utime(trailing_tmp, (COMPLETED_MTIME, COMPLETED_MTIME))

        result = compact_once(self.config, now_epoch=NOW, logger=self.logger)

        self.assertEqual(result, 0)
        self.assertFalse(eligible.exists())
        self.assertTrue(current.exists())
        self.assertTrue(temporary.exists())
        self.assertTrue(archived.exists())
        self.assertTrue(trailing_tmp.exists())
        manifest = json.loads(self._manifests()[0].read_text(encoding="utf-8"))
        self.assertEqual(
            [source["path"] for source in manifest["sources"]],
            ["eligible.parquet"],
        )

    def test_row_mismatch_returns_nonzero_and_does_not_archive(self) -> None:
        source = self._write("source.parquet", [1, 2])
        real_validate = __import__(
            "app.storage.compactor",
            fromlist=["_validate_final"],
        )._validate_final

        def report_mismatch(
            path: Path,
            expected_rows: int,
            expected_sha256: str | None = None,
        ) -> int:
            if path.name.endswith(".inprogress"):
                raise ValueError(
                    f"compacted row mismatch: expected={expected_rows} actual=1"
                )
            return real_validate(path, expected_rows, expected_sha256)

        with mock.patch(
            "app.storage.compactor._validate_final",
            side_effect=report_mismatch,
        ):
            result = compact_once(self.config, now_epoch=NOW, logger=self.logger)

        self.assertEqual(result, 1)
        self.assertTrue(source.exists())
        self.assertFalse((self.live / "archived/source.parquet").exists())
        self.assertEqual(self._outputs(), [])
        manifest = json.loads(self._manifests()[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "planned")

    def test_schema_mismatch_fails_without_archiving_sources(self) -> None:
        first = self._write("first.parquet", [1])
        second = self.live / "second.parquet"
        pq.write_table(
            pa.table({"value": ["different"], "symbol": ["BTC"]}),
            second,
        )
        os.utime(second, (COMPLETED_MTIME, COMPLETED_MTIME))

        result = compact_once(self.config, now_epoch=NOW, logger=self.logger)

        self.assertEqual(result, 1)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self._outputs(), [])
        self.assertFalse((self.live / "archived").exists())

    def test_late_source_uses_new_immutable_generation(self) -> None:
        first = self._write("first.parquet", [1])
        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        self.assertFalse(first.exists())

        late = self._write("late.parquet", [999])
        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )

        self.assertFalse(late.exists())
        outputs = sorted(self._outputs())
        self.assertEqual(len(outputs), 2)
        self.assertEqual(
            sorted(
                pq.ParquetFile(path).read()["value"].to_pylist()
                for path in outputs
            ),
            [[1], [999]],
        )
        manifests = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self._manifests())
        ]
        self.assertEqual(
            sorted(source["path"] for manifest in manifests for source in manifest["sources"]),
            ["first.parquet", "late.parquet"],
        )
        self.assertEqual({manifest["status"] for manifest in manifests}, {"complete"})

    def test_restart_after_final_before_archive_only_finishes_archive(self) -> None:
        source = self._write("nested/source.parquet", [1, 2])

        with mock.patch(
            "app.storage.compactor._archive_sources",
            side_effect=RuntimeError("simulated kill before archive"),
        ):
            first_result = compact_once(
                self.config,
                now_epoch=NOW,
                logger=self.logger,
            )

        self.assertEqual(first_result, 1)
        self.assertTrue(source.exists())
        self.assertEqual(len(self._outputs()), 1)

        with mock.patch(
            "app.storage.compactor.pq.write_table",
            side_effect=AssertionError("valid final must not be rebuilt"),
        ):
            second_result = compact_once(
                self.config,
                now_epoch=NOW,
                logger=self.logger,
            )

        self.assertEqual(second_result, 0)
        self.assertFalse(source.exists())
        self.assertTrue((self.live / "archived/nested/source.parquet").exists())
        self.assertEqual(len(self._outputs()), 1)
        manifest = json.loads(self._manifests()[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete")

    def test_same_row_corrupt_final_is_not_accepted_on_restart(self) -> None:
        source = self._write("source.parquet", [1])
        with mock.patch(
            "app.storage.compactor._archive_sources",
            side_effect=RuntimeError("simulated kill before archive"),
        ):
            self.assertEqual(
                compact_once(self.config, now_epoch=NOW, logger=self.logger),
                1,
            )

        output = self._outputs()[0]
        pq.write_table(
            pa.table({"value": [999], "symbol": ["BTC"]}),
            output,
        )

        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            1,
        )
        self.assertTrue(source.exists())
        self.assertFalse((self.live / "archived/source.parquet").exists())
        manifest = json.loads(self._manifests()[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "published")

    def test_old_source_gets_full_archive_retention_after_compaction(self) -> None:
        old_mtime = NOW - 48 * 3600
        source = self._write("old.parquet", [1], mtime=old_mtime)

        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )

        self.assertFalse(source.exists())
        archived = self.live / "archived/old.parquet"
        self.assertTrue(archived.exists())
        self.assertGreater(archived.stat().st_mtime, old_mtime)

    def test_corrupt_final_never_allows_expired_archive_deletion(self) -> None:
        self._write("source.parquet", [1])
        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        archived = self.live / "archived/source.parquet"
        os.utime(archived, (NOW - 48 * 3600, NOW - 48 * 3600))
        output = self._outputs()[0]
        pq.write_table(
            pa.table({"value": [999], "symbol": ["BTC"]}),
            output,
        )

        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            1,
        )

        self.assertTrue(archived.exists())
        self.assertEqual(
            pq.ParquetFile(archived).read()["value"].to_pylist(),
            [1],
        )

    def test_known_inprogress_artifact_is_rebuilt_from_manifest_sources(self) -> None:
        source = self._write("source.parquet", [1, 2, 3])

        with mock.patch(
            "app.storage.compactor._write_final",
            side_effect=RuntimeError("simulated kill during write"),
        ):
            first_result = compact_once(
                self.config,
                now_epoch=NOW,
                logger=self.logger,
            )

        self.assertEqual(first_result, 1)
        manifest = json.loads(self._manifests()[0].read_text(encoding="utf-8"))
        artifact = (
            self.compacted / ".tmp" / (manifest["output"] + ".inprogress")
        )
        artifact.write_bytes(b"partial parquet")

        second_result = compact_once(
            self.config,
            now_epoch=NOW,
            logger=self.logger,
        )

        self.assertEqual(second_result, 0)
        self.assertFalse(artifact.exists())
        self.assertFalse(source.exists())
        self.assertEqual(pq.ParquetFile(self._outputs()[0]).metadata.num_rows, 3)

    def test_complete_manifest_accepts_output_already_moved_to_sent(self) -> None:
        self._write("source.parquet", [1, 2])
        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        output = self._outputs()[0]
        sent_dir = self.compacted / "sent"
        sent_dir.mkdir(parents=True, exist_ok=True)
        sent_path = sent_dir / output.name
        os.replace(output, sent_path)

        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        manifest = json.loads(self._manifests()[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["artifact_location"], "sent")
        self.assertTrue(sent_path.is_file())
        self.assertFalse(output.exists())

    def test_complete_manifest_missing_both_lifecycle_paths_is_offloaded(self) -> None:
        """After sent/ retention, local lifecycle copies may be gone."""
        self._write("source.parquet", [1])
        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        output = self._outputs()[0]
        output.unlink()

        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        manifest = json.loads(self._manifests()[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest.get("artifact_location"), "offloaded")

    def test_complete_manifest_sent_then_retained_is_offloaded(self) -> None:
        self._write("source.parquet", [1, 2])
        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        output = self._outputs()[0]
        sent_dir = self.compacted / "sent"
        sent_dir.mkdir(parents=True, exist_ok=True)
        sent_path = sent_dir / output.name
        os.replace(output, sent_path)
        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        sent_path.unlink()
        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        manifest = json.loads(self._manifests()[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["artifact_location"], "offloaded")

    def test_streaming_write_handles_many_small_sources(self) -> None:
        """Regression for OOM path: many small files must compact via iter_batches."""
        for index in range(40):
            self._write(f"coin=C{index:02d}/part.parquet", [index, index + 100])

        with mock.patch(
            "app.storage.compactor.pq.ParquetFile.read",
            side_effect=AssertionError("full read() must not be used"),
        ):
            result = compact_once(self.config, now_epoch=NOW, logger=self.logger)

        self.assertEqual(result, 0)
        outputs = self._outputs()
        self.assertEqual(len(outputs), 1)
        self.assertEqual(pq.ParquetFile(outputs[0]).metadata.num_rows, 80)

    def test_max_windows_limits_work_per_oneshot(self) -> None:
        """Salvage safety: only N new windows compacted per process."""
        # Two distinct completed mtime windows (interval=300).
        older = COMPLETED_MTIME - 300
        self._write("w0.parquet", [1], mtime=older)
        self._write("w1.parquet", [2], mtime=COMPLETED_MTIME)
        limited = CompactorConfig(
            live_root=self.live,
            compacted_root=self.compacted,
            retention_hours=24,
            max_windows=1,
        )

        first = compact_once(limited, now_epoch=NOW, logger=self.logger)
        self.assertEqual(first, 0)
        self.assertEqual(len(self._outputs()), 1)
        # Exactly one live source should remain for the second window.
        remaining_live = [
            path
            for path in self.live.rglob("*.parquet")
            if "archived" not in path.parts
        ]
        self.assertEqual(len(remaining_live), 1)

        second = compact_once(limited, now_epoch=NOW, logger=self.logger)
        self.assertEqual(second, 0)
        self.assertEqual(len(self._outputs()), 2)
        self.assertEqual(
            [
                path
                for path in self.live.rglob("*.parquet")
                if "archived" not in path.parts
            ],
            [],
        )

    def test_iter_batch_rows_config_accepted(self) -> None:
        self._write("source.parquet", list(range(20)))
        tiny = CompactorConfig(
            live_root=self.live,
            compacted_root=self.compacted,
            retention_hours=24,
            iter_batch_rows=3,
        )
        self.assertEqual(compact_once(tiny, now_epoch=NOW, logger=self.logger), 0)
        self.assertEqual(pq.ParquetFile(self._outputs()[0]).metadata.num_rows, 20)

    def test_retention_only_prunes_without_new_compaction(self) -> None:
        self._write("source.parquet", [1, 2, 3])
        self.assertEqual(
            compact_once(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        archived = self.live / "archived/source.parquet"
        self.assertTrue(archived.is_file())
        old_mtime = NOW - (25 * 3600)
        os.utime(archived, (old_mtime, old_mtime))

        # No new live sources; retention-only must still prune eligible archived.
        result = run_archive_retention_only(
            self.config, now_epoch=NOW + 10, logger=self.logger
        )
        self.assertEqual(result, 0)
        self.assertFalse(archived.exists())
        self.assertEqual(len(self._outputs()), 1)

    def _write_complete_state(
        self,
        *,
        output: str,
        window_end: float,
        sources: list[str],
        artifact_location: str = "offloaded",
    ) -> Path:
        state_root = self.compacted / ".state"
        state_root.mkdir(parents=True, exist_ok=True)
        path = state_root / f"{Path(output).stem}.json"
        payload = {
            "status": "complete",
            "output": output,
            "output_sha256": "a" * 64,
            "window_start": int(window_end) - 300,
            "window_end": int(window_end),
            "total_rows": 1,
            "input_bytes": 1,
            "source_files_count": len(sources),
            "artifact_location": artifact_location,
            "sources": [
                {"path": relative, "size": 1, "mtime_ns": 0, "sha256": "b" * 64}
                for relative in sources
            ],
        }
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    def test_expired_complete_state_pruned_after_archives_gone(self) -> None:
        output = "spread_20220101T000000Z_20220101T000500Z.parquet"
        archived = self.live / "archived/old-source.parquet"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_bytes(b"x")
        old = NOW - 48 * 3600
        os.utime(archived, (old, old))
        state = self._write_complete_state(
            output=output,
            window_end=NOW - 48 * 3600,
            sources=["old-source.parquet"],
        )
        # Offloaded: no local compacted/sent artifact.
        self.assertEqual(
            run_archive_retention_only(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        self.assertFalse(archived.exists())
        self.assertFalse(state.exists())

    def test_expired_state_kept_while_pending_in_compacted(self) -> None:
        from app.storage.compactor import JsonLogger, _prune_expired_complete_states

        output = "spread_20220101T001000Z_20220101T001500Z.parquet"
        self.compacted.mkdir(parents=True, exist_ok=True)
        pending = self.compacted / output
        pending.write_bytes(b"pending-bytes")
        state = self._write_complete_state(
            output=output,
            window_end=NOW - 48 * 3600,
            sources=["still-pending.parquet"],
            artifact_location="compacted",
        )
        stats = _prune_expired_complete_states(
            self.config, NOW, JsonLogger(self.logger)
        )
        self.assertEqual(stats["deferred_pending_compacted"], 1)
        self.assertEqual(stats["pruned_manifests"], 0)
        self.assertTrue(state.exists())
        self.assertTrue(pending.exists())

    def test_young_complete_state_not_pruned(self) -> None:
        output = "spread_20220101T002000Z_20220101T002500Z.parquet"
        state = self._write_complete_state(
            output=output,
            window_end=NOW - 3600,
            sources=["young.parquet"],
        )
        self.assertEqual(
            run_archive_retention_only(self.config, now_epoch=NOW, logger=self.logger),
            0,
        )
        self.assertTrue(state.exists())


if __name__ == "__main__":
    unittest.main()

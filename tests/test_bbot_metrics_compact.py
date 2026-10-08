"""Closed-day zstd compaction of would_send theta/ + tw_p50/ metrics."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.bot import metrics_compact as mc
from app.bot.metrics_compact import (
    MetricsDayCompactor,
    compress_day_file,
    day_metrics_files,
    iter_lines,
    rotate_compress_enabled,
)
from app.bot.theta_screener import ThetaJournalWriter
from app.bot.tw_p50_watcher import TwP50JournalWriter

HAVE_ZSTD = shutil.which("zstd") is not None


def _ms(y, m, d, hh=0, mm=0, ss=0, ms=0) -> int:
    dt = datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000) + ms


class _Clock:
    def __init__(self, sec: float) -> None:
        self.t = float(sec)
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.t

    def set(self, sec: float) -> None:
        with self.lock:
            self.t = float(sec)


def _row(ts_ms: int, coin: str = "KAITO", side: str = "long", seq: int = 0) -> dict:
    return {
        "schema_version": "bbot.theta.v1",
        "base_coin": coin,
        "side": side,
        "ts_ms": ts_ms,
        "theta_1m": 0.01,
        "seq": seq,
        "computed_at_ms": ts_ms + 5,
    }


def _read_all(kind_root: Path) -> list[dict]:
    out: list[dict] = []
    for p in day_metrics_files(kind_root):
        for line in iter_lines(p):
            if line.strip():
                out.append(json.loads(line))
    return out


class FlagTests(unittest.TestCase):
    def test_flag_default_off(self) -> None:
        self.assertFalse(rotate_compress_enabled({}))
        self.assertTrue(rotate_compress_enabled({"BBOT_METRICS_ROTATE_COMPRESS": "1"}))
        self.assertFalse(rotate_compress_enabled({"BBOT_METRICS_ROTATE_COMPRESS": "0"}))
        self.assertEqual(mc.compress_rate_bytes({}), 24 * 1024 * 1024)
        self.assertEqual(mc.compress_rate_bytes({"BBOT_METRICS_COMPRESS_MBPS": "0"}), 0)

    def test_legacy_writer_unchanged_when_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            w = ThetaJournalWriter(Path(tmp))
            self.assertIsNone(w.rollover)
            w.append_rows([_row(_ms(2026, 10, 7, 23, 59, 59)), _row(_ms(2026, 10, 8))])
            self.assertTrue((Path(tmp) / "theta/event_date=2026-10-07/metrics.jsonl").is_file())
            self.assertTrue((Path(tmp) / "theta/event_date=2026-10-08/metrics.jsonl").is_file())


@unittest.skipUnless(HAVE_ZSTD, "zstd binary required")
class CompressFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.day = self.root / "theta" / "event_date=2026-10-07"
        self.day.mkdir(parents=True)
        self.src = self.day / "metrics.jsonl"
        with self.src.open("w") as fh:
            for i in range(5000):
                fh.write(json.dumps(_row(_ms(2026, 10, 7, 12) + i, seq=i)) + "\n")
        self.orig = self.src.read_bytes()

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_ok_roundtrip_and_original_removed(self) -> None:
        res = compress_day_file(self.src)
        self.assertEqual(res.status, "ok", res.reason)
        self.assertEqual(res.lines, 5000)
        self.assertFalse(self.src.exists())
        self.assertFalse((self.day / "metrics.jsonl.zst.tmp").exists())
        zst = self.day / "metrics.jsonl.zst"
        self.assertEqual("".join(iter_lines(zst)).encode(), self.orig)
        # Idempotent: nothing left to do.
        self.assertEqual(compress_day_file(self.src).status, "skipped")

    def test_stale_tmp_is_redone_from_original(self) -> None:
        (self.day / "metrics.jsonl.zst.tmp").write_bytes(b"garbage-partial")
        res = compress_day_file(self.src)
        self.assertEqual(res.status, "ok", res.reason)
        self.assertEqual("".join(iter_lines(self.day / "metrics.jsonl.zst")).encode(), self.orig)

    def test_resume_after_rename_before_unlink(self) -> None:
        res = compress_day_file(self.src)
        self.assertEqual(res.status, "ok")
        self.src.write_bytes(self.orig)  # simulate crash before unlink
        res2 = compress_day_file(self.src)
        self.assertEqual(res2.status, "ok", res2.reason)
        self.assertEqual(res2.reason, "resumed_existing_zst")
        self.assertFalse(self.src.exists())

    def test_existing_zst_mismatch_keeps_original(self) -> None:
        compress_day_file(self.src)
        self.src.write_bytes(self.orig + b'{"extra":1}\n')
        res = compress_day_file(self.src)
        self.assertEqual(res.status, "failed")
        self.assertIn("mismatch", res.reason)
        self.assertTrue(self.src.exists())

    def test_failure_keeps_original(self) -> None:
        fake = self.root / "fake_zstd"
        fake.write_text("#!/bin/sh\nexit 3\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        res = compress_day_file(self.src, zstd_bin=str(fake))
        self.assertEqual(res.status, "failed")
        self.assertEqual(self.src.read_bytes(), self.orig)
        self.assertFalse((self.day / "metrics.jsonl.zst").exists())
        self.assertFalse((self.day / "metrics.jsonl.zst.tmp").exists())

    def test_verify_mismatch_keeps_original(self) -> None:
        # zstd that writes a valid but WRONG archive (drops last line).
        real = shutil.which("zstd")
        fake = self.root / "lossy_zstd"
        fake.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"-3\" ]; then\n"
            f"  head -n 10 | {real} -q -c -\n"
            "  cat >/dev/null\n"
            "  exit 0\n"
            "fi\n"
            f"exec {real} \"$@\"\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        res = compress_day_file(self.src, zstd_bin=str(fake))
        self.assertEqual(res.status, "failed")
        self.assertIn("verify_mismatch", res.reason)
        self.assertEqual(self.src.read_bytes(), self.orig)
        self.assertFalse((self.day / "metrics.jsonl.zst").exists())

    def test_paced_rate_is_respected(self) -> None:
        size = len(self.orig)
        rate = size / 0.5  # whole file in >= ~0.5 s per pass
        res = compress_day_file(self.src, rate_bytes=rate)
        self.assertEqual(res.status, "ok", res.reason)
        self.assertGreaterEqual(res.seconds, 0.9)  # two paced passes
        self.assertEqual("".join(iter_lines(self.day / "metrics.jsonl.zst")).encode(), self.orig)

    def test_corrupt_tmp_output_keeps_original(self) -> None:
        fake = self.root / "corrupt_zstd"
        real = shutil.which("zstd")
        fake.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"-3\" ]; then\n"
            "  cat >/dev/null; printf 'not-a-zstd-frame'; exit 0\n"
            "fi\n"
            f"exec {real} \"$@\"\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        res = compress_day_file(self.src, zstd_bin=str(fake))
        self.assertEqual(res.status, "failed")
        self.assertEqual(self.src.read_bytes(), self.orig)
        self.assertFalse((self.day / "metrics.jsonl.zst").exists())
        self.assertFalse((self.day / "metrics.jsonl.zst.tmp").exists())

    def test_missing_zstd(self) -> None:
        orig_which = mc.shutil.which
        try:
            mc.shutil.which = lambda name: None  # type: ignore[assignment]
            res = compress_day_file(self.src)
        finally:
            mc.shutil.which = orig_which  # type: ignore[assignment]
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.reason, "zstd_not_found")
        self.assertTrue(self.src.exists())


@unittest.skipUnless(HAVE_ZSTD, "zstd binary required")
class RolloverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.failures: list = []
        self.compactor = MetricsDayCompactor(on_failure=self.failures.append)

    def tearDown(self) -> None:
        self.compactor.stop()
        self._td.cleanup()

    def test_continuous_rows_across_midnight_no_loss_no_dup(self) -> None:
        start = _ms(2026, 10, 7, 23, 58, 0)
        clock = _Clock(start / 1000.0)
        w = TwP50JournalWriter(
            self.root, rotate_compress=True, compactor=self.compactor, grace_sec=30, clock=clock
        )
        self.assertTrue(self.compactor.wait_idle(10))
        expected = []
        seq = 0
        # 1 Hz for 6 minutes across 00:00 UTC; wall clock lags row ts by 3.5 s
        # (late rows with the old date are written after midnight wall).
        for s in range(360):
            ts = start + s * 1000
            clock.set((ts + 3500) / 1000.0)
            batch = []
            for coin in ("KAITO", "WAL"):
                for side in ("long", "short"):
                    r = _row(ts, coin, side, seq)
                    seq += 1
                    batch.append(r)
            expected.extend(batch)
            w.append_rows(batch)
            if s == 200:
                # Writer has handed off by now; new-day file keeps growing.
                self.assertTrue(self.compactor.wait_idle(30))
                self.assertTrue((self.root / "tw_p50/event_date=2026-10-07/metrics.jsonl.zst").is_file())
                self.assertFalse((self.root / "tw_p50/event_date=2026-10-07/metrics.jsonl").exists())
        self.assertTrue(self.compactor.wait_idle(30))
        got = _read_all(self.root / "tw_p50")
        self.assertEqual(len(got), len(expected))
        self.assertEqual(sorted(r["seq"] for r in got), list(range(seq)))
        # Every row in the partition matching its own UTC date (no reroute needed).
        old = list(iter_lines(self.root / "tw_p50/event_date=2026-10-07/metrics.jsonl.zst"))
        self.assertTrue(all(json.loads(x)["ts_ms"] < _ms(2026, 10, 8) for x in old))
        self.assertEqual(w.rollover.rerouted_rows, 0)
        self.assertEqual(self.failures, [])

    def test_late_row_after_seal_is_rerouted_not_lost(self) -> None:
        clock = _Clock(_ms(2026, 10, 7, 23, 59, 50) / 1000.0)
        w = ThetaJournalWriter(
            self.root, rotate_compress=True, compactor=self.compactor, grace_sec=10, clock=clock
        )
        w.append_rows([_row(_ms(2026, 10, 7, 23, 59, 50), seq=0)])
        clock.set(_ms(2026, 10, 8, 0, 0, 1) / 1000.0)
        w.append_rows([_row(_ms(2026, 10, 8, 0, 0, 1), seq=1)])
        clock.set(_ms(2026, 10, 8, 0, 0, 30) / 1000.0)  # grace expired
        w.append_rows([_row(_ms(2026, 10, 8, 0, 0, 30), seq=2)])
        self.assertTrue(self.compactor.wait_idle(10))
        # Very late row stamped with the sealed day.
        w.append_rows([_row(_ms(2026, 10, 7, 23, 59, 59), seq=3)])
        self.assertTrue(self.compactor.wait_idle(10))
        d7 = self.root / "theta/event_date=2026-10-07"
        self.assertTrue((d7 / "metrics.jsonl.zst").is_file())
        self.assertFalse((d7 / "metrics.jsonl").exists())  # never recreated
        new_day = [json.loads(x) for x in iter_lines(self.root / "theta/event_date=2026-10-08/metrics.jsonl")]
        self.assertEqual([r["seq"] for r in new_day], [1, 2, 3])
        self.assertEqual(sorted(r["seq"] for r in _read_all(self.root / "theta")), [0, 1, 2, 3])
        self.assertEqual(w.rollover.rerouted_rows, 1)

    def test_startup_sweep_compresses_closed_days_only(self) -> None:
        kind = self.root / "theta"
        for d in ("2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"):
            p = kind / f"event_date={d}" / "metrics.jsonl"
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"d": d}) + "\n")
        # leftovers: stale tmp with original; tmp next to an existing final
        (kind / "event_date=2026-10-06/metrics.jsonl.zst.tmp").write_bytes(b"partial")
        clock = _Clock(_ms(2026, 10, 8, 12) / 1000.0)
        ThetaJournalWriter(self.root, rotate_compress=True, compactor=self.compactor, clock=clock)
        self.assertTrue(self.compactor.wait_idle(20))
        for d in ("2026-10-05", "2026-10-06", "2026-10-07"):
            day = kind / f"event_date={d}"
            self.assertFalse((day / "metrics.jsonl").exists(), d)
            self.assertFalse((day / "metrics.jsonl.zst.tmp").exists(), d)
            self.assertEqual(json.loads("".join(iter_lines(day / "metrics.jsonl.zst"))), {"d": d})
        self.assertTrue((kind / "event_date=2026-10-08/metrics.jsonl").is_file())
        self.assertFalse((kind / "event_date=2026-10-08/metrics.jsonl.zst").exists())
        # tmp beside a final .zst with no original is cleaned on next sweep
        stale = kind / "event_date=2026-10-05/metrics.jsonl.zst.tmp"
        stale.write_bytes(b"x")
        self.compactor.submit_sweep(kind, "2026-10-08", reason="test")
        self.assertTrue(self.compactor.wait_idle(10))
        self.assertFalse(stale.exists())

    def test_failure_keeps_original_and_reports(self) -> None:
        def boom(path: Path) -> mc.CompactResult:
            return mc.CompactResult(path=path, status="failed", reason="boom")

        comp = MetricsDayCompactor(compress_fn=boom, on_failure=self.failures.append)
        p = self.root / "theta/event_date=2026-10-07/metrics.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text("{}\n")
        clock = _Clock(_ms(2026, 10, 8, 1) / 1000.0)
        w = ThetaJournalWriter(self.root, rotate_compress=True, compactor=comp, clock=clock)
        self.assertTrue(comp.wait_idle(10))
        w.append_rows([_row(_ms(2026, 10, 8, 1))])  # writer unaffected
        comp.stop()
        self.assertTrue(p.is_file())
        self.assertEqual([f.reason for f in self.failures], ["boom"])

    def test_default_failure_hook_logs_and_calls_sentry(self) -> None:
        from app.bot import sentry_setup

        calls = []
        orig = sentry_setup.capture_ops_event
        sentry_setup.capture_ops_event = lambda *a, **k: calls.append((a, k))  # type: ignore[assignment]
        try:
            with self.assertLogs("bbot.metrics_compact", level="WARNING") as cm:
                mc._default_on_failure(mc.CompactResult(path=Path("/x"), status="failed", reason="r"))
        finally:
            sentry_setup.capture_ops_event = orig  # type: ignore[assignment]
        self.assertIn("metrics_compact_failed", cm.output[0])
        self.assertEqual(calls[0][0][0], "metrics_compact_failed")


if __name__ == "__main__":
    unittest.main()

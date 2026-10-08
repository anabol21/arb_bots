"""Closed-day zstd compaction for would_send ``theta/`` and ``tw_p50/`` metrics.

Layout (unchanged for the live day)::

    {data_root}/{kind}/event_date=YYYY-MM-DD/metrics.jsonl       # open / live day
    {data_root}/{kind}/event_date=YYYY-MM-DD/metrics.jsonl.zst   # closed day

Event dates are **UTC** dates of the row ``ts_ms`` (same rule as the writers).

No-gap rollover design (see ``DayRollover``):

* The hot path never waits on compression. When the UTC wall date changes the
  writer simply starts appending to the new day's file (append-per-batch, no
  long-lived handle) and keeps the previous day *open for late rows* during a
  short grace window (``BBOT_METRICS_ROTATE_GRACE_SEC``, default 120 s).
* After the grace window the previous day is *sealed*: the writer will never
  open it again and a background worker thread compresses it (nice/ionice
  ``zstd -3 -T1`` subprocess fed at a paced rate, ``BBOT_METRICS_COMPRESS_MBPS``
  default 16 MiB/s, because the unit has CPUQuota=100% shared with the bot),
  verifies (paced ``zstd -dc`` with frame checksums + line count + byte count
  + sha256 of the decompressed stream vs. the original), fsyncs, atomically
  renames ``.zst.tmp`` -> ``.zst`` and only then deletes the original.
* Late-row policy: a row whose own UTC date is older than the current day and
  not in grace (sealed) is **re-routed into the current day's file** (row
  content and ``ts_ms`` unchanged). Nothing is lost or duplicated; the only
  effect is that such a row lives in the next day's partition, so readers that
  select by ``ts_ms`` should read the adjacent day too (they already do).
* Startup sweep: closed days (< current UTC date) left as ``metrics.jsonl``
  (restart across midnight, crash mid-compaction) are compressed the same way.
  A stale ``.zst.tmp`` is discarded and redone from the original; when both the
  original and a final ``.zst`` exist, the ``.zst`` is re-verified against the
  original before the original is removed.
* Failure (zstd missing, verify mismatch, source changed, low disk...): the
  original is kept, a WARNING is logged and a Sentry event is emitted; the
  trading loop is never affected.

Enabled by ``BBOT_METRICS_ROTATE_COMPRESS=1`` (default off; set in the
would_send prod unit).
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Callable, Iterator, Mapping, Optional, Sequence

ENV_FLAG = "BBOT_METRICS_ROTATE_COMPRESS"
ENV_GRACE = "BBOT_METRICS_ROTATE_GRACE_SEC"
ENV_RATE = "BBOT_METRICS_COMPRESS_MBPS"
DEFAULT_GRACE_SEC = 120.0
# The would_send unit runs with CPUQuota=100% and the bot itself uses ~70% of
# it; compression shares that cgroup, so it is paced (MiB/s of *uncompressed*
# data) to stay a few % of a core instead of bursting to the quota.
DEFAULT_RATE_MBPS = 16.0
METRICS_NAME = "metrics.jsonl"
ZST_NAME = "metrics.jsonl.zst"
TMP_NAME = "metrics.jsonl.zst.tmp"
_CHUNK = 1 << 20

_log = logging.getLogger("bbot.metrics_compact")


def rotate_compress_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """``BBOT_METRICS_ROTATE_COMPRESS`` = 1/true/on/yes; default off."""
    e = env if env is not None else os.environ
    raw = str(e.get(ENV_FLAG) or "").strip().lower()
    return raw in ("1", "true", "on", "yes")


def rotate_grace_sec(env: Optional[Mapping[str, str]] = None) -> float:
    e = env if env is not None else os.environ
    raw = str(e.get(ENV_GRACE) or "").strip()
    if not raw:
        return DEFAULT_GRACE_SEC
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_GRACE_SEC
    return max(0.0, val)


def compress_rate_bytes(env: Optional[Mapping[str, str]] = None) -> float:
    """``BBOT_METRICS_COMPRESS_MBPS`` (MiB/s, default 16; 0 = unpaced)."""
    e = env if env is not None else os.environ
    raw = str(e.get(ENV_RATE) or "").strip()
    try:
        mbps = float(raw) if raw else DEFAULT_RATE_MBPS
    except ValueError:
        mbps = DEFAULT_RATE_MBPS
    return max(0.0, mbps) * 1024 * 1024


class _Pacer:
    """Sleep so that consumed bytes never run ahead of ``rate`` bytes/s."""

    def __init__(self, rate: float) -> None:
        self.rate = float(rate)
        self.t0 = time.monotonic()
        self.n = 0

    def consume(self, n: int) -> None:
        if self.rate <= 0:
            return
        self.n += n
        ahead = self.n / self.rate - (time.monotonic() - self.t0)
        if ahead > 0.002:
            time.sleep(ahead)


def utc_date_of_ms(ts_ms: int | float) -> str:
    return datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc).date().isoformat()


def utc_date_of_sec(ts_sec: float) -> str:
    return datetime.fromtimestamp(float(ts_sec), tz=timezone.utc).date().isoformat()


def row_event_date(row: Mapping[str, object]) -> str:
    """Same partition rule as the legacy writers: UTC date of ``ts_ms``."""
    ts_ms = int(row.get("ts_ms") or row.get("computed_at_ms") or 0)  # type: ignore[arg-type]
    return utc_date_of_ms(ts_ms)


# ---------------------------------------------------------------------------
# Readers (transparent .jsonl / .jsonl.zst)
# ---------------------------------------------------------------------------


def day_metrics_files(kind_root: Path) -> list[Path]:
    """Per event_date: ``metrics.jsonl`` if present else ``metrics.jsonl.zst``.

    If both exist (crash between rename and unlink) the plain file wins; the
    two are byte-identical once verified.
    """
    out: list[Path] = []
    root = Path(kind_root)
    if not root.is_dir():
        return out
    for day in sorted(root.glob("event_date=*")):
        plain = day / METRICS_NAME
        zst = day / ZST_NAME
        if plain.is_file():
            out.append(plain)
        elif zst.is_file():
            out.append(zst)
    return out


def iter_lines(path: Path, *, zstd_bin: Optional[str] = None) -> Iterator[str]:
    """Yield text lines (with trailing newline) of ``.jsonl`` or ``.jsonl.zst``."""
    p = Path(path)
    if p.name.endswith(".zst"):
        binary = zstd_bin or shutil.which("zstd")
        if binary is None:
            raise FileNotFoundError("zstd binary not found for " + str(p))
        proc = subprocess.Popen(
            [binary, "-dc", "-q", str(p)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                yield raw.decode("utf-8")
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc != 0:
            raise OSError(f"zstd -dc failed rc={rc} path={p}")
    else:
        with p.open("r", encoding="utf-8") as fh:
            yield from fh


# ---------------------------------------------------------------------------
# Single-file compaction
# ---------------------------------------------------------------------------


@dataclass
class CompactResult:
    path: Path
    status: str  # ok | skipped | failed
    reason: str = ""
    lines: int = 0
    orig_bytes: int = 0
    zst_bytes: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _digest_stream(
    fh: IO[bytes],
    *,
    pacer: Optional[_Pacer] = None,
    sink: Optional[IO[bytes]] = None,
    dontneed_fd: Optional[int] = None,
) -> tuple[str, int, int]:
    h = hashlib.sha256()
    lines = 0
    nbytes = 0
    last_advise = 0
    while True:
        buf = fh.read(_CHUNK)
        if not buf:
            break
        h.update(buf)
        lines += buf.count(b"\n")
        nbytes += len(buf)
        if sink is not None:
            sink.write(buf)
        if dontneed_fd is not None and nbytes - last_advise >= 64 * _CHUNK:
            _dontneed(dontneed_fd)
            last_advise = nbytes
        if pacer is not None:
            pacer.consume(len(buf))
    return h.hexdigest(), lines, nbytes


def _digest_file(path: Path, rate: float = 0.0) -> tuple[str, int, int]:
    with path.open("rb") as fh:
        out = _digest_stream(fh, pacer=_Pacer(rate), dontneed_fd=fh.fileno())
        _dontneed(fh.fileno())
    return out


def _digest_zst(path: Path, zstd_bin: str, rate: float = 0.0) -> tuple[str, int, int]:
    """Decompress (frame checksums verified by zstd; rc!=0 => corrupt) + digest."""
    proc = subprocess.Popen(
        _low_prio_prefix() + [zstd_bin, "-dc", "-q", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    try:
        out = _digest_stream(proc.stdout, pacer=_Pacer(rate))
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc != 0:
        raise OSError(f"zstd -dc rc={rc}")
    return out


def _dontneed(fd: int) -> None:
    fn = getattr(os, "posix_fadvise", None)
    flag = getattr(os, "POSIX_FADV_DONTNEED", None)
    if fn is None or flag is None:
        return
    try:
        fn(fd, 0, 0, flag)
    except OSError:
        return


def _fsync_path(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _low_prio_prefix() -> list[str]:
    prefix: list[str] = []
    nice = shutil.which("nice")
    if nice:
        prefix += [nice, "-n", "19"]
    ionice = shutil.which("ionice")
    if ionice:
        prefix += [ionice, "-c3"]
    return prefix


def _stat_sig(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def compress_day_file(
    path: Path,
    *,
    level: int = 3,
    zstd_bin: Optional[str] = None,
    min_free_bytes: int = 512 * 1024 * 1024,
    rate_bytes: Optional[float] = None,
) -> CompactResult:
    """Compress one closed-day ``metrics.jsonl`` -> ``metrics.jsonl.zst``.

    Pass 1 (paced): read the original once, hashing it while feeding a
    nice/ionice ``zstd -<level> -T1`` on stdin -> ``.zst.tmp``.
    Pass 2 (paced): ``zstd -dc .zst.tmp`` (frame checksum verified, rc==0 is the
    ``zstd -t`` equivalent) -> sha256 + line count + byte count must equal the
    original's; the original's size/mtime must be unchanged. Then fsync tmp,
    atomic rename to ``.zst``, fsync dir, unlink original, fsync dir.
    The original is deleted only after all of that; any failure keeps it.
    """
    t0 = time.monotonic()
    src = Path(path)
    day = src.parent
    tmp = day / TMP_NAME
    dst = day / ZST_NAME
    res = CompactResult(path=src, status="failed")
    rate = compress_rate_bytes() if rate_bytes is None else float(rate_bytes)

    def _done(status: str, reason: str = "") -> CompactResult:
        res.status = status
        res.reason = reason
        res.seconds = round(time.monotonic() - t0, 3)
        return res

    binary = zstd_bin or shutil.which("zstd")
    if not src.is_file():
        return _done("skipped", "no_source")
    if binary is None:
        return _done("failed", "zstd_not_found")
    try:
        sig0 = _stat_sig(src)
        res.orig_bytes = sig0[0]

        # Crash between rename and unlink: verify existing .zst, then unlink.
        if dst.is_file():
            o_dig = _digest_file(src, rate)
            z_dig = _digest_zst(dst, binary, rate)
            res.lines = o_dig[1]
            res.zst_bytes = dst.stat().st_size
            if o_dig != z_dig:
                return _done("failed", "existing_zst_mismatch")
            if _stat_sig(src) != sig0:
                return _done("failed", "source_changed")
            src.unlink()
            _fsync_path(day)
            return _done("ok", "resumed_existing_zst")

        free = shutil.disk_usage(str(day)).free
        if free < min_free_bytes + sig0[0] // 4:
            return _done("failed", f"low_disk_free={free}")

        if tmp.exists():
            tmp.unlink()  # stale from a crash; redo from the original
        cmd = _low_prio_prefix() + [binary, f"-{int(level)}", "-T1", "-q", "-c", "-"]
        with tmp.open("wb") as out_fh, src.open("rb") as in_fh, tempfile.TemporaryFile() as err_fh:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=out_fh, stderr=err_fh
            )
            assert proc.stdin is not None
            feed_err: Optional[BaseException] = None
            try:
                o_dig = _digest_stream(
                    in_fh, pacer=_Pacer(rate), sink=proc.stdin, dontneed_fd=in_fh.fileno()
                )
            except BaseException as exc:  # noqa: BLE001 — e.g. BrokenPipe
                feed_err = exc
                o_dig = ("", 0, 0)
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            proc.wait()
            err_fh.seek(0)
            err_b = err_fh.read(400)
            _dontneed(in_fh.fileno())
            out_fh.flush()
            os.fsync(out_fh.fileno())
        if proc.returncode != 0 or feed_err is not None:
            _safe_unlink(tmp)
            err = (err_b or b"").decode("utf-8", "replace").strip()[:200]
            why = f"feed={type(feed_err).__name__}" if feed_err is not None else ""
            return _done("failed", f"zstd_rc={proc.returncode} {why} {err}".strip())

        res.lines = o_dig[1]
        try:
            z_dig = _digest_zst(tmp, binary, rate)
        except OSError as exc:
            _safe_unlink(tmp)
            return _done("failed", f"zstd_test_failed {exc}")
        if o_dig != z_dig or o_dig[2] != sig0[0]:
            _safe_unlink(tmp)
            return _done(
                "failed",
                f"verify_mismatch lines={o_dig[1]}/{z_dig[1]} bytes={o_dig[2]}/{z_dig[2]}/{sig0[0]}",
            )
        if _stat_sig(src) != sig0:
            _safe_unlink(tmp)
            return _done("failed", "source_changed")

        os.replace(tmp, dst)
        _fsync_path(day)
        res.zst_bytes = dst.stat().st_size
        src.unlink()
        _fsync_path(day)
        return _done("ok")
    except Exception as exc:  # noqa: BLE001 — never raise into the bot
        _safe_unlink(tmp)
        return _done("failed", f"{type(exc).__name__}: {exc}"[:300])


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def closed_day_files(kind_root: Path, before_date: str) -> list[Path]:
    """Uncompressed ``metrics.jsonl`` for event_date < ``before_date`` (sorted).

    Also cleans stale ``.zst.tmp`` whose original is gone but a final ``.zst``
    exists. A lone ``.zst.tmp`` (no original, no final) is left untouched and
    logged (never delete what may be the only copy).
    """
    out: list[Path] = []
    root = Path(kind_root)
    if not root.is_dir():
        return out
    for day in sorted(root.glob("event_date=*")):
        if not day.is_dir():
            continue
        date = day.name.split("=", 1)[1]
        if not date or date >= before_date:
            continue
        plain = day / METRICS_NAME
        tmp = day / TMP_NAME
        if plain.is_file():
            out.append(plain)  # compress_day_file redoes a stale tmp
        elif tmp.exists():
            if (day / ZST_NAME).is_file():
                _safe_unlink(tmp)
                _log.info("metrics_compact_stale_tmp_removed | path=%s", tmp)
            else:
                _log.warning("metrics_compact_orphan_tmp_kept | path=%s", tmp)
    return out


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


def _default_on_failure(result: CompactResult) -> None:
    _log.warning(
        "metrics_compact_failed | path=%s | reason=%s | kept_original=1",
        result.path,
        result.reason,
    )
    try:
        from app.bot.sentry_setup import capture_ops_event

        capture_ops_event(
            "metrics_compact_failed",
            kind="metrics_compact",
            level="warning",
            extras={"path": str(result.path), "reason": result.reason},
        )
    except Exception:  # noqa: BLE001
        return


@dataclass
class _Job:
    kind_root: Path
    before_date: str
    reason: str


class MetricsDayCompactor:
    """One low-priority daemon thread; sweeps are serialized (one file at a time)."""

    def __init__(
        self,
        *,
        compress_fn: Callable[[Path], CompactResult] = compress_day_file,
        on_failure: Callable[[CompactResult], None] = _default_on_failure,
    ) -> None:
        self._compress = compress_fn
        self._on_failure = on_failure
        self._q: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._pending = 0
        self.results: list[CompactResult] = []

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="bbot-metrics-compact", daemon=True
            )
            self._thread.start()

    def submit_sweep(self, kind_root: Path, before_date: str, *, reason: str) -> None:
        """Queue: compress every uncompressed day < ``before_date`` under ``kind_root``."""
        with self._lock:
            self._pending += 1
            self._idle.clear()
        self._q.put(_Job(Path(kind_root), str(before_date), reason))
        self._ensure_thread()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        return self._idle.wait(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        self._q.put(None)
        t = self._thread
        if t is not None:
            t.join(timeout)

    def _run(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            try:
                self._do_job(job)
            except Exception as exc:  # noqa: BLE001
                _log.warning("metrics_compact_job_error | err=%s", type(exc).__name__)
            finally:
                with self._lock:
                    self._pending -= 1
                    if self._pending <= 0:
                        self._pending = 0
                        self._idle.set()

    def _do_job(self, job: _Job) -> None:
        files = closed_day_files(job.kind_root, job.before_date)
        _log.info(
            "metrics_compact_sweep | reason=%s | root=%s | before=%s | n=%s",
            job.reason,
            job.kind_root,
            job.before_date,
            len(files),
        )
        for path in files:
            result = self._compress(path)
            self.results.append(result)
            if result.status == "ok":
                _log.info(
                    "metrics_compact_ok | path=%s | lines=%s | orig=%s | zst=%s | sec=%s | note=%s",
                    result.path,
                    result.lines,
                    result.orig_bytes,
                    result.zst_bytes,
                    result.seconds,
                    result.reason or "-",
                )
            elif result.status == "failed":
                self._on_failure(result)


_shared: Optional[MetricsDayCompactor] = None
_shared_lock = threading.Lock()


def shared_compactor() -> MetricsDayCompactor:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = MetricsDayCompactor()
        return _shared


# ---------------------------------------------------------------------------
# Rollover bookkeeping used by the writers
# ---------------------------------------------------------------------------


@dataclass
class DayRollover:
    """Decide target day per row + when a closed day can be handed off.

    ``current`` follows the UTC **wall** date (never moved by odd row ts).
    ``grace`` maps previous days still accepting late rows -> wall deadline.
    """

    current: str
    grace_sec: float = DEFAULT_GRACE_SEC
    grace: dict[str, float] = field(default_factory=dict)
    rerouted_rows: int = 0

    def advance(self, now_sec: float) -> None:
        wall = utc_date_of_sec(now_sec)
        if wall > self.current:
            self.grace[self.current] = now_sec + self.grace_sec
            self.current = wall

    def target(self, event_date: str) -> str:
        if event_date >= self.current or event_date in self.grace:
            return event_date
        self.rerouted_rows += 1
        return self.current

    def pop_expired(self, now_sec: float) -> list[str]:
        done = sorted(d for d, dl in self.grace.items() if now_sec >= dl)
        for d in done:
            del self.grace[d]
        return done

    def min_open(self) -> str:
        return min([self.current, *self.grace.keys()])


class RotatingMetricsJsonl:
    """Shared append logic for theta / tw_p50 writers (optional rollover+zstd)."""

    def __init__(
        self,
        kind_root: Path,
        path_fn: Callable[[str], Path],
        *,
        rotate_compress: bool,
        compactor: Optional[MetricsDayCompactor] = None,
        grace_sec: Optional[float] = None,
        clock: Callable[[], float] = time.time,
        dontneed: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.kind_root = Path(kind_root)
        self.path_fn = path_fn
        self.rotate_compress = bool(rotate_compress)
        self.clock = clock
        self._dontneed = dontneed
        self._lock = threading.Lock()
        self._warned_reroute = False
        self.rollover: Optional[DayRollover] = None
        self.compactor: Optional[MetricsDayCompactor] = None
        if self.rotate_compress:
            self.compactor = compactor or shared_compactor()
            today = utc_date_of_sec(self.clock())
            self.rollover = DayRollover(
                current=today,
                grace_sec=rotate_grace_sec() if grace_sec is None else float(grace_sec),
            )
            _log.info(
                "metrics_compact_startup_sweep | root=%s | before=%s | grace_sec=%s",
                self.kind_root,
                today,
                self.rollover.grace_sec,
            )
            self.compactor.submit_sweep(self.kind_root, today, reason="startup")

    def append_rows(self, rows: Sequence[Mapping[str, object]], dumps: Callable[[Mapping[str, object]], str]) -> list[Path]:
        if not rows:
            return []
        with self._lock:
            roll = self.rollover
            now = self.clock()
            if roll is not None:
                roll.advance(now)
            by_date: dict[str, list[Mapping[str, object]]] = {}
            before = roll.rerouted_rows if roll is not None else 0
            for row in rows:
                d = row_event_date(row)
                if roll is not None:
                    d = roll.target(d)
                by_date.setdefault(d, []).append(row)
            written: list[Path] = []
            for event_date, batch in by_date.items():
                path = self.path_fn(event_date)
                with path.open("a", encoding="utf-8") as fh:
                    for rec in batch:
                        fh.write(dumps(rec))
                        fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                    if self._dontneed is not None:
                        self._dontneed(fh.fileno())
                written.append(path)
            handoff_before: Optional[str] = None
            expired: list[str] = []
            if roll is not None:
                if roll.rerouted_rows > before and not self._warned_reroute:
                    self._warned_reroute = True
                    _log.warning(
                        "metrics_late_rows_rerouted | root=%s | into=%s | total=%s",
                        self.kind_root,
                        roll.current,
                        roll.rerouted_rows,
                    )
                expired = roll.pop_expired(now)
                if expired:
                    handoff_before = roll.min_open()
        if handoff_before is not None and self.compactor is not None:
            _log.info(
                "metrics_rollover_handoff | root=%s | sealed=%s | before=%s",
                self.kind_root,
                ",".join(expired),
                handoff_before,
            )
            self.compactor.submit_sweep(self.kind_root, handoff_before, reason="rollover")
        return written

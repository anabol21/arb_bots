"""Operator alerts for disk, backlog, archive age, and compaction health.

Emits structured JSON lines to stdout (and optionally a log file). Exit code 0
when all checks are healthy; 1 when one or more alerts fire. Read-only against
production paths by default.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DATA_ROOT = Path("/data")
DEFAULT_LIVE = Path("/data/live")
DEFAULT_COMPACTED = Path("/data/compacted")
DEFAULT_ARCHIVE = Path("/data/live/archived")
DEFAULT_SPOOL = Path("/data/spool")
DEFAULT_COMPACTOR_LOG = Path("/var/log/spread/compactor.log")
DEFAULT_TRANSFER_LOG = Path("/var/log/spread/backup-transfer.log")
DEFAULT_BARS_TRANSFER_LOG = Path("/var/log/spread/bars-backup-transfer.log")
DEFAULT_RUNTIME_LOG = Path("/var/log/spread/runtime.log")

_COMPACTED_WINDOW_RE = re.compile(
    r"spread_(\d{8}T\d{6}Z)_(\d{8}T\d{6}Z)(?:_g\d+)?\.parquet$"
)


def emit(event: str, **fields: Any) -> None:
    payload = {"timestamp": time.time(), "event": event, **fields}
    print(json.dumps(payload, sort_keys=True), flush=True)


def disk_free_bytes(path: Path) -> int:
    st = os.statvfs(path)
    return int(st.f_bavail * st.f_frsize)


def tree_files(root: Path, *, suffix: str | None = None) -> tuple[int, int, float | None]:
    if not root.exists():
        return 0, 0, None
    count = 0
    size = 0
    oldest_mtime: float | None = None
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            if suffix is not None and path.suffix != suffix:
                continue
            st = path.stat()
            count += 1
            size += st.st_size
            if oldest_mtime is None or st.st_mtime < oldest_mtime:
                oldest_mtime = st.st_mtime
        except FileNotFoundError:
            continue
    return count, size, oldest_mtime


def hive_files(root: Path) -> tuple[int, int, float | None]:
    """Return active bar files only; sent copies and state do not count as backlog."""
    return tree_files(root / "bar_5m", suffix=".parquet")


def count_log_events(path: Path, event_name: str, *, lookback_sec: float) -> int:
    if not path.is_file():
        return 0
    cutoff = time.time() - lookback_sec
    hits = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or event_name not in line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _payload_epoch(payload)
                if ts is not None and ts < cutoff:
                    continue
                if payload.get("event") == event_name:
                    hits += 1
                    continue
                # Numeric counter fields: only count positive values so a
                # successful transfer with transfer_watchdog_kills=0 is quiet.
                value = payload.get(event_name)
                if isinstance(value, (int, float)) and value > 0:
                    hits += 1
    except OSError as exc:
        emit("ops_alert_read_error", path=str(path), error=repr(exc))
    return hits


def _payload_epoch(payload: dict[str, Any]) -> float | None:
    raw = payload.get("timestamp")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def newest_log_event_age_sec(path: Path, event_name: str) -> float | None:
    """Age in seconds of the newest matching JSON event, or None if absent."""
    if not path.is_file():
        return None
    newest: float | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or event_name not in line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("event") != event_name:
                    continue
                ts = _payload_epoch(payload)
                if ts is None:
                    continue
                if newest is None or ts > newest:
                    newest = ts
    except OSError as exc:
        emit("ops_alert_read_error", path=str(path), error=repr(exc))
        return None
    if newest is None:
        return None
    return max(0.0, time.time() - newest)


def newest_compacted_window_end_epoch(compacted: Path) -> float | None:
    newest: float | None = None
    if not compacted.exists():
        return None
    for path in compacted.glob("spread_*.parquet"):
        match = _COMPACTED_WINDOW_RE.match(path.name)
        if match is None:
            continue
        try:
            end = datetime.strptime(match.group(2), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        epoch = end.timestamp()
        if newest is None or epoch > newest:
            newest = epoch
    sent = compacted / "sent"
    if sent.exists():
        for path in sent.glob("spread_*.parquet"):
            match = _COMPACTED_WINDOW_RE.match(path.name)
            if match is None:
                continue
            try:
                end = datetime.strptime(match.group(2), "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            epoch = end.timestamp()
            if newest is None or epoch > newest:
                newest = epoch
    return newest


def journal_oom_restart_count(*, unit: str, lookback_sec: float) -> int | None:
    """Best-effort OOM/kill count from journalctl; None if journal unavailable."""
    since = f"-{max(1, int(lookback_sec))}s"
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                unit,
                "--since",
                since,
                "--no-pager",
                "-o",
                "cat",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode not in (0, 1):
        return None
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    markers = (
        "oom-kill",
        "Memory cgroup out of memory",
        "Killed process",
        "status=9/KILL",
        "OOM",
    )
    hits = 0
    for line in text.splitlines():
        if any(marker in line for marker in markers):
            hits += 1
    return hits


def journal_term_count(*, unit: str, lookback_sec: float) -> int | None:
    """Best-effort count of systemd SIGTERM outcomes, kept separate from OOM."""
    since = f"-{max(1, int(lookback_sec))}s"
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                unit,
                "--since",
                since,
                "--no-pager",
                "-o",
                "cat",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode not in (0, 1):
        return None
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    return sum("status=15/TERM" in line for line in text.splitlines())


def systemd_unit_active(unit: str) -> bool | None:
    """Return service/timer active state, or None when systemd is unavailable."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 3:
        return False
    return None


def transfer_watchdog_kills(manifest: Path) -> int:
    if not manifest.is_file():
        return 0
    try:
        connection = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT COALESCE(SUM(watchdog_kills), 0) FROM transfers"
            ).fetchone()
            if row is not None:
                return int(row[0])
        except sqlite3.Error:
            # Older schemas may not have watchdog_kills; fall back to log scan.
            return 0
        finally:
            connection.close()
    except sqlite3.Error:
        return 0
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--live", type=Path, default=DEFAULT_LIVE)
    parser.add_argument("--compacted", type=Path, default=DEFAULT_COMPACTED)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--spool", type=Path, default=DEFAULT_SPOOL)
    parser.add_argument("--bars", type=Path, default=Path("/data/bars"))
    parser.add_argument("--compactor-log", type=Path, default=DEFAULT_COMPACTOR_LOG)
    parser.add_argument("--transfer-log", type=Path, default=DEFAULT_TRANSFER_LOG)
    parser.add_argument(
        "--bars-transfer-log", type=Path, default=DEFAULT_BARS_TRANSFER_LOG
    )
    parser.add_argument("--runtime-log", type=Path, default=DEFAULT_RUNTIME_LOG)
    parser.add_argument("--min-free-gb", type=float, default=3.0)
    parser.add_argument("--max-backlog-files", type=int, default=20)
    parser.add_argument("--max-backlog-mb", type=float, default=512.0)
    parser.add_argument("--max-archive-age-hours", type=float, default=36.0)
    parser.add_argument("--max-spool-files", type=int, default=100)
    parser.add_argument(
        "--max-bars-backlog-age-minutes",
        type=float,
        default=60.0,
        help=(
            "Alert when active bars backlog has a file older than this; "
            "prevents an increasing bars queue from reporting healthy."
        ),
    )
    parser.add_argument("--lookback-sec", type=float, default=900.0)
    parser.add_argument(
        "--max-compaction-lag-minutes",
        type=float,
        default=30.0,
        help="Alert when wall clock − newest compacted window end exceeds this.",
    )
    parser.add_argument(
        "--max-missing-complete-cycles",
        type=int,
        default=3,
        help=(
            "Alert when compaction_complete or archive_retention_complete "
            "is older than cycles × lookback-ish window (cycle≈300s)."
        ),
    )
    parser.add_argument(
        "--cycle-seconds",
        type=float,
        default=300.0,
        help="Expected timer period for silence checks (default 300).",
    )
    parser.add_argument(
        "--max-live-growth-mb",
        type=float,
        default=256.0,
        help=(
            "Alert when live parquet bytes exceed this while tick compacted "
            "backlog is empty (uncompacted growth)."
        ),
    )
    parser.add_argument("--once", action="store_true", help="Run one check cycle and exit")
    return parser.parse_args()


def run_checks(args: argparse.Namespace) -> int:
    alerts: list[dict[str, Any]] = []
    free = disk_free_bytes(args.data_root)
    free_gb = free / (1024**3)
    pending = list(args.compacted.glob("spread_*.parquet"))
    pending_count = len(pending)
    pending_bytes = sum(path.stat().st_size for path in pending if path.is_file())
    pending_mb = pending_bytes / (1024**2)
    archive_count, archive_bytes, archive_oldest = tree_files(
        args.archive, suffix=".parquet"
    )
    spool_count, spool_bytes, _ = tree_files(args.spool)
    sent = args.compacted / "sent"
    sent_count, sent_bytes, _ = tree_files(sent, suffix=".parquet")
    live_count, live_bytes, _ = tree_files(args.live, suffix=".parquet")
    bars_pending_count, bars_pending_bytes, bars_oldest_mtime = hive_files(args.bars)
    # Exclude archived from "active live growth" signal when possible.
    archived_under_live = 0
    try:
        _, archived_under_live, _ = tree_files(args.archive, suffix=".parquet")
    except OSError:
        archived_under_live = archive_bytes
    active_live_bytes = max(0, live_bytes - archived_under_live)
    compaction_alerts = count_log_events(
        args.compactor_log, "compaction_alert", lookback_sec=args.lookback_sec
    )
    watchdog_log = count_log_events(
        args.transfer_log,
        "transfer_watchdog_kills",
        lookback_sec=args.lookback_sec,
    )
    enospc_tick = count_log_events(
        args.transfer_log, "transfer_enospc_alert", lookback_sec=args.lookback_sec
    )
    enospc_bars = count_log_events(
        args.bars_transfer_log,
        "transfer_enospc_alert",
        lookback_sec=args.lookback_sec,
    )
    bars_skipped_busy = count_log_events(
        args.bars_transfer_log,
        "bars_transfer_skipped_busy",
        lookback_sec=args.lookback_sec,
    )
    bars_deferred_shared_lock_busy = count_log_events(
        args.bars_transfer_log,
        "transfer_deferred_shared_lock_busy",
        lookback_sec=args.lookback_sec,
    )
    bars_microbatch_deferred = count_log_events(
        args.bars_transfer_log,
        "hive_microbatch_deferred",
        lookback_sec=args.lookback_sec,
    )
    bars_microbatch_results = count_log_events(
        args.bars_transfer_log,
        "hive_microbatch_result",
        lookback_sec=args.lookback_sec,
    )
    watchdog_db = transfer_watchdog_kills(
        args.compacted / ".state" / "backup_manifest.sqlite3"
    )
    compaction_complete_age = newest_log_event_age_sec(
        args.compactor_log, "compaction_complete"
    )
    retention_complete_age = newest_log_event_age_sec(
        args.compactor_log, "archive_retention_complete"
    )
    newest_window_end = newest_compacted_window_end_epoch(args.compacted)
    compaction_lag_minutes = (
        None
        if newest_window_end is None
        else round((time.time() - newest_window_end) / 60.0, 3)
    )
    silence_limit_sec = args.max_missing_complete_cycles * args.cycle_seconds
    oom_compactor = journal_oom_restart_count(
        unit="spread-compactor.service", lookback_sec=args.lookback_sec
    )
    oom_collector = journal_oom_restart_count(
        unit="spread-collector.service", lookback_sec=max(args.lookback_sec, 3600.0)
    )
    term_compactor = journal_term_count(
        unit="spread-compactor.service", lookback_sec=args.lookback_sec
    )
    term_bars = journal_term_count(
        unit="spread-bars-backup-transfer.service", lookback_sec=args.lookback_sec
    )
    bars_transfer_active = systemd_unit_active("spread-bars-backup-transfer.service")
    compactor_timer_active = systemd_unit_active("spread-compactor.timer")
    # Log heuristic fallback when journal is unavailable.
    oom_log_heuristic = count_log_events(
        args.compactor_log, "compactor_fatal", lookback_sec=args.lookback_sec
    ) + count_log_events(
        args.runtime_log, "MemoryError", lookback_sec=args.lookback_sec
    )

    snapshot = {
        "disk_free_bytes": free,
        "disk_free_gb": round(free_gb, 3),
        "backlog_files": pending_count,
        "backlog_mb": round(pending_mb, 3),
        "archive_files": archive_count,
        "archive_bytes": archive_bytes,
        "archive_oldest_age_hours": (
            None
            if archive_oldest is None
            else round((time.time() - archive_oldest) / 3600.0, 3)
        ),
        "spool_files": spool_count,
        "spool_bytes": spool_bytes,
        "sent_files": sent_count,
        "sent_bytes": sent_bytes,
        "active_live_bytes": active_live_bytes,
        "active_live_mb": round(active_live_bytes / (1024**2), 3),
        "bars_backlog_files": bars_pending_count,
        "bars_backlog_mb": round(bars_pending_bytes / (1024**2), 3),
        "bars_backlog_oldest_age_minutes": (
            None
            if bars_oldest_mtime is None
            else round((time.time() - bars_oldest_mtime) / 60.0, 3)
        ),
        "bars_transfer_skipped_busy_lookback": bars_skipped_busy,
        "bars_transfer_deferred_shared_lock_busy_lookback": (
            bars_deferred_shared_lock_busy
        ),
        "bars_microbatch_deferred_lookback": bars_microbatch_deferred,
        "bars_microbatch_results_lookback": bars_microbatch_results,
        "compaction_alerts_lookback": compaction_alerts,
        "transfer_watchdog_kills_log_lookback": watchdog_log,
        "transfer_watchdog_kills_db": watchdog_db,
        "compaction_complete_age_sec": compaction_complete_age,
        "archive_retention_complete_age_sec": retention_complete_age,
        "compaction_lag_minutes": compaction_lag_minutes,
        "transfer_enospc_tick_lookback": enospc_tick,
        "transfer_enospc_bars_lookback": enospc_bars,
        "oom_compactor_journal_lookback": oom_compactor,
        "oom_collector_journal_lookback": oom_collector,
        "oom_log_heuristic_lookback": oom_log_heuristic,
        "term_compactor_journal_lookback": term_compactor,
        "term_bars_journal_lookback": term_bars,
        "bars_transfer_service_active": bars_transfer_active,
        "compactor_timer_active": compactor_timer_active,
    }
    emit("ops_alert_snapshot", **snapshot)

    if free_gb < args.min_free_gb:
        alerts.append(
            {
                "alert": "disk_free_low",
                "disk_free_gb": round(free_gb, 3),
                "threshold_gb": args.min_free_gb,
            }
        )
    if pending_count > args.max_backlog_files or pending_mb > args.max_backlog_mb:
        alerts.append(
            {
                "alert": "transfer_backlog_high",
                "backlog_files": pending_count,
                "backlog_mb": round(pending_mb, 3),
                "max_files": args.max_backlog_files,
                "max_mb": args.max_backlog_mb,
            }
        )
    if spool_count > args.max_spool_files:
        alerts.append(
            {
                "alert": "spool_backlog_high",
                "spool_files": spool_count,
                "max_files": args.max_spool_files,
            }
        )
    if (
        bars_oldest_mtime is not None
        and (time.time() - bars_oldest_mtime) / 60.0
        > args.max_bars_backlog_age_minutes
    ):
        alerts.append(
            {
                "alert": "bars_backlog_age_high",
                "oldest_age_minutes": round(
                    (time.time() - bars_oldest_mtime) / 60.0, 3
                ),
                "threshold_minutes": args.max_bars_backlog_age_minutes,
                "backlog_files": bars_pending_count,
            }
        )
    if archive_oldest is not None:
        age_h = (time.time() - archive_oldest) / 3600.0
        if age_h > args.max_archive_age_hours:
            alerts.append(
                {
                    "alert": "archive_age_high",
                    "oldest_age_hours": round(age_h, 3),
                    "threshold_hours": args.max_archive_age_hours,
                    "archive_files": archive_count,
                }
            )
    if compaction_alerts > 0:
        alerts.append(
            {
                "alert": "compaction_alert",
                "count_lookback": compaction_alerts,
                "lookback_sec": args.lookback_sec,
            }
        )
    if watchdog_log > 0 or watchdog_db > 0:
        alerts.append(
            {
                "alert": "transfer_watchdog_kills",
                "log_lookback": watchdog_log,
                "db_total": watchdog_db,
            }
        )
    if (
        compaction_lag_minutes is not None
        and compaction_lag_minutes > args.max_compaction_lag_minutes
    ):
        alerts.append(
            {
                "alert": "compaction_lag_high",
                "lag_minutes": compaction_lag_minutes,
                "threshold_minutes": args.max_compaction_lag_minutes,
            }
        )
    if (
        compaction_complete_age is None
        or compaction_complete_age > silence_limit_sec
    ):
        alerts.append(
            {
                "alert": "compaction_complete_missing",
                "age_sec": compaction_complete_age,
                "threshold_sec": silence_limit_sec,
                "missing_cycles": args.max_missing_complete_cycles,
            }
        )
    if (
        retention_complete_age is None
        or retention_complete_age > silence_limit_sec
    ):
        alerts.append(
            {
                "alert": "archive_retention_complete_missing",
                "age_sec": retention_complete_age,
                "threshold_sec": silence_limit_sec,
                "missing_cycles": args.max_missing_complete_cycles,
            }
        )
    if pending_count == 0 and (active_live_bytes / (1024**2)) > args.max_live_growth_mb:
        alerts.append(
            {
                "alert": "live_growth_with_empty_tick_backlog",
                "active_live_mb": round(active_live_bytes / (1024**2), 3),
                "threshold_mb": args.max_live_growth_mb,
                "backlog_files": pending_count,
            }
        )
    if enospc_tick > 0 or enospc_bars > 0:
        alerts.append(
            {
                "alert": "transfer_enospc",
                "tick_count": enospc_tick,
                "bars_count": enospc_bars,
                "lookback_sec": args.lookback_sec,
            }
        )
    if term_compactor or term_bars:
        alerts.append(
            {
                "alert": "forced_term_signal",
                "compactor_journal": term_compactor,
                "bars_journal": term_bars,
                "lookback_sec": args.lookback_sec,
            }
        )
    oom_hits = (oom_compactor or 0) + (oom_collector or 0) + oom_log_heuristic
    if oom_hits > 0:
        alerts.append(
            {
                "alert": "oom_signal",
                "compactor_journal": oom_compactor,
                "collector_journal": oom_collector,
                "log_heuristic": oom_log_heuristic,
                "lookback_sec": args.lookback_sec,
            }
        )

    for alert in alerts:
        emit("ops_alert", severity="warning", **alert)
    if not alerts:
        emit("ops_alert_ok", **snapshot)
        return 0
    return 1


def main() -> int:
    args = parse_args()
    return run_checks(args)


if __name__ == "__main__":
    raise SystemExit(main())

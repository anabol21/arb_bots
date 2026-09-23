#!/usr/bin/env python3
"""Assemble valid tick history into compacted lean 5-minute files.

Destination: output/lean_ticks (Mac). Format matches backup1tb:spread-compacted:
spread_YYYYMMDDTHHMMSSZ_YYYYMMDDTHHMMSSZ.parquet, 16 lean columns, zstd.

Does not write D trees, backup remotes, or delete sources.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.schema.lean_event import LEAN_TICK_BODY_COLS  # noqa: E402

INTERVAL_MS = 300_000
COMPACTED_RE = "spread_"
BOOK_COLS = (
    "okx_bid_price",
    "okx_ask_price",
    "bybit_bid_price",
    "bybit_ask_price",
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def window_name(start_ms: int) -> str:
    start = datetime.fromtimestamp(start_ms / 1000.0, timezone.utc)
    end = start + timedelta(milliseconds=INTERVAL_MS)
    fmt = "%Y%m%dT%H%M%SZ"
    return f"spread_{start.strftime(fmt)}_{end.strftime(fmt)}.parquet"


def window_floor_ms(ts_ms: int) -> int:
    return (int(ts_ms) // INTERVAL_MS) * INTERVAL_MS


def compacted_names_between(start: datetime, end_exclusive: datetime) -> list[str]:
    t0 = int(start.timestamp())
    t1 = int(end_exclusive.timestamp())
    w = (t0 // 300) * 300
    names: list[str] = []
    fmt = "%Y%m%dT%H%M%SZ"
    while w < t1:
        a = datetime.fromtimestamp(w, timezone.utc)
        b = datetime.fromtimestamp(w + 300, timezone.utc)
        names.append(f"spread_{a.strftime(fmt)}_{b.strftime(fmt)}.parquet")
        w += 300
    return names


def schema_ok(path: Path) -> tuple[bool, list[str]]:
    try:
        names = list(pq.read_schema(path).names)
    except Exception:
        return False, []
    missing = [c for c in LEAN_TICK_BODY_COLS if c not in names]
    if missing:
        return False, names
    if any(c not in names for c in BOOK_COLS):
        return False, names
    return True, names


def is_lean_only(names: list[str]) -> bool:
    return list(names) == list(LEAN_TICK_BODY_COLS) or set(names) == set(LEAN_TICK_BODY_COLS)


def install_compacted_file(src: Path, dest_dir: Path, *, replace: bool = False) -> str:
    dest = dest_dir / src.name
    ok, names = schema_ok(src)
    if not ok:
        return "skip_schema"
    if dest.exists() and not replace:
        dest_ok, _ = schema_ok(dest)
        if dest_ok:
            return "exists"
    dest_dir.mkdir(parents=True, exist_ok=True)
    if is_lean_only(names):
        try:
            os.link(src, dest)
            return "hardlink"
        except OSError:
            shutil.copy2(src, dest)
            return "copy"
    _rewrite_lean(src, dest)
    return "rewrite_lean"


def _rewrite_lean(src: Path, dest: Path) -> None:
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        pf = pq.ParquetFile(src)
        writer: pq.ParquetWriter | None = None
        for batch in pf.iter_batches(columns=list(LEAN_TICK_BODY_COLS), batch_size=16_384):
            if writer is None:
                writer = pq.ParquetWriter(where=str(tmp), schema=batch.schema, compression="zstd")
            writer.write_batch(batch)
        if writer is not None:
            writer.close()
        if not tmp.exists():
            return
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def gather_compacted(src_dirs: list[Path], dest_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for src_dir in src_dirs:
        if not src_dir.is_dir():
            _log(f"missing compacted dir {src_dir}")
            continue
        files = sorted(src_dir.glob("spread_*.parquet"))
        _log(f"compacted source {src_dir} files={len(files)}")
        for path in files:
            action = install_compacted_file(path, dest_dir)
            counts[action] += 1
            counts["seen"] += 1
    return dict(counts)


def _select_lean(batch: pa.RecordBatch) -> pa.RecordBatch | None:
    names = set(batch.schema.names)
    if any(c not in names for c in BOOK_COLS):
        return None
    arrays = []
    fields = []
    for col in LEAN_TICK_BODY_COLS:
        if col not in names:
            return None
        arrays.append(batch.column(col))
        fields.append(batch.schema.field(col))
    return pa.RecordBatch.from_arrays(arrays, schema=pa.schema(fields))


def compact_hive_date(hive_root: Path, event_date: str, dest_dir: Path) -> dict[str, int]:
    """One UTC calendar date of per-coin hive → 5-minute lean files (event-time bins)."""
    stats = defaultdict(int)
    dest_dir.mkdir(parents=True, exist_ok=True)
    writers: dict[int, pq.ParquetWriter] = {}
    tmps: dict[int, Path] = {}
    skipped_windows = 0

    try:
        for coin_dir in sorted(p for p in hive_root.iterdir() if p.name.startswith("base_coin=")):
            date_dir = coin_dir / f"event_date={event_date}"
            if not date_dir.is_dir():
                continue
            for path in sorted(date_dir.glob("*.parquet")):
                ok, _names = schema_ok(path)
                if not ok:
                    stats["skip_schema"] += 1
                    continue
                stats["files_ok"] += 1
                pf = pq.ParquetFile(path)
                try:
                    for batch in pf.iter_batches(columns=list(LEAN_TICK_BODY_COLS), batch_size=16_384):
                        lean = _select_lean(batch)
                        if lean is None or lean.num_rows == 0:
                            continue
                        ts = lean.column("event_local_ts_ms").to_numpy()
                        floors = (ts // INTERVAL_MS) * INTERVAL_MS
                        for start_ms in np.unique(floors):
                            start_i = int(start_ms)
                            name = window_name(start_i)
                            final = dest_dir / name
                            if final.exists() and start_i not in writers:
                                skipped_windows += 1
                                continue
                            mask = pa.array(floors == start_i)
                            part = lean.filter(mask)
                            if part.num_rows == 0:
                                continue
                            if start_i not in writers:
                                tmp = dest_dir / f".{name}.{os.getpid()}.tmp"
                                tmps[start_i] = tmp
                                writers[start_i] = pq.ParquetWriter(
                                    where=str(tmp),
                                    schema=part.schema,
                                    compression="zstd",
                                )
                            writers[start_i].write_batch(part)
                            stats["rows"] += part.num_rows
                finally:
                    del pf
        stats["windows_written"] = len(writers)
        stats["windows_skipped_existing"] = skipped_windows
    finally:
        for start_ms, w in writers.items():
            w.close()
            tmp = tmps[start_ms]
            final = dest_dir / window_name(start_ms)
            if tmp.exists():
                os.replace(tmp, final)
        for tmp in tmps.values():
            tmp.unlink(missing_ok=True)
    return dict(stats)


def list_hive_dates(hive_root: Path) -> list[str]:
    dates: set[str] = set()
    if not hive_root.is_dir():
        return []
    for coin_dir in hive_root.iterdir():
        if not coin_dir.name.startswith("base_coin="):
            continue
        for date_dir in coin_dir.iterdir():
            if date_dir.name.startswith("event_date=") and date_dir.is_dir():
                dates.add(date_dir.name.split("=", 1)[1])
    return sorted(dates)


def dest_inventory(dest_dir: Path) -> dict[str, object]:
    files = sorted(dest_dir.glob("spread_*.parquet")) if dest_dir.is_dir() else []
    names = [p.name for p in files]
    return {
        "n_files": len(files),
        "first": names[0] if names else None,
        "last": names[-1] if names else None,
        "bytes": sum(p.stat().st_size for p in files),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", type=Path, default=REPO / "output" / "lean_ticks")
    parser.add_argument("--hive", type=Path, default=REPO / "output" / "spreads_parquet_by_coins")
    parser.add_argument("--skip-hive", action="store_true")
    parser.add_argument("--skip-local-compacted", action="store_true")
    parser.add_argument(
        "--ingest-dir",
        type=Path,
        help="Extra compacted dir to merge (e.g. output/_backup_pull). v1 is rewritten to lean.",
    )
    parser.add_argument(
        "--write-rclone-files",
        type=Path,
        help="Write backup window names (Aug 3..now+1d) minus dest, then exit",
    )
    args = parser.parse_args()
    dest = args.dest
    dest.mkdir(parents=True, exist_ok=True)

    local_compacted = [
        REPO / "output" / "vacation_return_20260810" / "ticks",
        REPO / "output" / "lean_ticks_recent",
    ]

    report: dict[str, object] = {"dest": str(dest)}

    if not args.skip_local_compacted:
        report["local_compacted"] = gather_compacted(local_compacted, dest)
        _log(f"local compacted → {report['local_compacted']}")

    if args.write_rclone_files:
        have = {p.name for p in dest.glob("spread_*.parquet")}
        start = datetime(2026, 8, 3, tzinfo=timezone.utc)
        end = datetime.now(timezone.utc) + timedelta(days=1)
        names = [n for n in compacted_names_between(start, end) if n not in have]
        args.write_rclone_files.write_text("\n".join(names) + ("\n" if names else ""))
        _log(f"wrote {len(names)} missing window names → {args.write_rclone_files}")
        report["rclone_missing"] = len(names)
        print(json.dumps(report, indent=2))
        return 0

    if args.ingest_dir:
        report["ingest"] = gather_compacted([args.ingest_dir], dest)
        _log(f"ingest {args.ingest_dir} → {report['ingest']}")

    if not args.skip_hive and args.hive.is_dir():
        dates = list_hive_dates(args.hive)
        _log(f"hive dates {dates[0] if dates else None} → {dates[-1] if dates else None} n={len(dates)}")
        hive_stats = []
        for event_date in dates:
            _log(f"hive compact {event_date}")
            st = compact_hive_date(args.hive, event_date, dest)
            _log(f"  {st}")
            hive_stats.append({"event_date": event_date, **st})
        report["hive"] = hive_stats

    report["inventory"] = dest_inventory(dest)
    summary = dest / "_assemble_summary.json"
    summary.write_text(json.dumps(report, indent=2) + "\n")
    _log(f"summary {summary}")
    _log(f"inventory {report['inventory']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

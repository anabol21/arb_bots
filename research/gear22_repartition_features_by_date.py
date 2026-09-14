"""Repartition gear 2.2 backtest features from coin/event_date to event_date.

Read-only on the source hive. Writes a sibling directory so the original
``coin=/event_date=`` layout is left intact.

Inside each UTC day the 30 coins are concatenated in time-major order:
``(ts_s, coin)`` — at each 1 Hz tick the canary coins appear one after
another, which is the scan a multi-coin backtest wants.

    PYTHONPATH=. ./venv/bin/python research/gear22_repartition_features_by_date.py
    PYTHONPATH=. ./venv/bin/python research/gear22_repartition_features_by_date.py --all-parts
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Optional, Sequence

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

SCHEMA_VERSION = "gear22_bt_features_v1"
DEFAULT_SRC = Path("output/gear22_backtest_features")
DEFAULT_DST = Path("output/gear22_backtest_features_by_date")
DEFAULT_PART_NAME = "part-000.parquet"
N_COINS_HOUR_ROWS = 30 * 3600  # one hour of 1 Hz × 30 coins


def git_head(repo: Path) -> Optional[str]:
    import subprocess

    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_source_manifest(src: Path) -> dict[str, Any]:
    path = src / "MANIFEST.json"
    if not path.is_file():
        raise FileNotFoundError(f"missing source manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_event_dates(src: Path, coins: Sequence[str]) -> list[str]:
    dates: set[str] = set()
    for coin in coins:
        for d in (src / f"coin={coin}").glob("event_date=*"):
            if d.is_dir():
                dates.add(d.name.split("=", 1)[1])
    return sorted(dates)


def list_source_parts(
    src: Path, coin: str, event_date: str, part_name: str, *, all_parts: bool
) -> list[Path]:
    """One named file, or every ``part-*.parquet`` in lexical order (part-000 then part-001)."""
    folder = src / f"coin={coin}" / f"event_date={event_date}"
    if all_parts:
        return sorted(p for p in folder.glob("part-*.parquet") if p.is_file())
    path = folder / part_name
    return [path] if path.is_file() else []


def dest_part(dst: Path, event_date: str, part_name: str) -> Path:
    return dst / f"event_date={event_date}" / part_name


def _unify_coin(table: pa.Table, coins: Sequence[str]) -> pa.Table:
    """Dictionary-encode coin with a fixed 30-symbol dictionary; put coin after ts_s."""
    coin_str = pc.cast(table["coin"], pa.string())
    indices = pc.cast(pc.index_in(coin_str, value_set=pa.array(list(coins))), pa.int8())
    if pc.any(pc.equal(indices, -1)).as_py() or pc.any(pc.is_null(indices)).as_py():
        unknown = sorted(set(coin_str.to_pylist()) - set(coins))
        raise ValueError(f"coin not in manifest dictionary: {unknown}")
    coin_dict = pa.DictionaryArray.from_arrays(
        indices, pa.array(list(coins), type=pa.string())
    )
    arrays = []
    names = []
    for name in table.column_names:
        if name == "coin":
            continue
        arrays.append(table[name].combine_chunks())
        names.append(name)
        if name == "ts_s":
            arrays.append(coin_dict)
            names.append("coin")
    if "coin" not in names:
        raise ValueError("ts_s missing; cannot place coin column")
    return pa.Table.from_arrays(arrays, names=names)


def concat_one_day(
    src: Path,
    coins: Sequence[str],
    event_date: str,
    part_name: str,
    *,
    all_parts: bool = False,
) -> pa.Table:
    tables: list[pa.Table] = []
    missing: list[str] = []
    for coin in coins:
        paths = list_source_parts(src, coin, event_date, part_name, all_parts=all_parts)
        if not paths:
            missing.append(coin)
            continue
        raw = pa.concat_tables([pq.ParquetFile(p).read() for p in paths])
        tables.append(_unify_coin(raw, coins))
    if missing:
        raise FileNotFoundError(
            f"{event_date}: missing source parts for {missing}"
        )
    table = pa.concat_tables(tables)
    rank = pc.cast(
        pc.index_in(pc.cast(table["coin"], pa.string()), value_set=pa.array(list(coins))),
        pa.int8(),
    )
    table = table.append_column("_coin_rank", rank)
    table = table.sort_by([("ts_s", "ascending"), ("_coin_rank", "ascending")])
    return table.drop(["_coin_rank"])


def write_day(table: pa.Table, dest: Path, *, overwrite: bool) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not overwrite:
        return dest
    tmp = dest.with_name(dest.name + ".tmp")
    pq.write_table(
        table,
        tmp,
        compression="zstd",
        row_group_size=N_COINS_HOUR_ROWS,
    )
    tmp.replace(dest)
    return dest


def _ts_unique_ok(ts: pa.Array, n_coins: int) -> tuple[bool, int]:
    uniq = pc.unique(ts)
    n_ts = len(uniq)
    return n_ts * n_coins == len(ts), n_ts


def sanity_day(table: pa.Table, coins: Sequence[str], event_date: str) -> dict[str, Any]:
    n_coins = len(coins)
    ts = table["ts_s"]
    coin = pc.cast(table["coin"], pa.string())
    ts_np_ok, n_ts = _ts_unique_ok(ts, n_coins)
    first_block = pc.cast(coin.slice(0, n_coins), pa.string()).to_pylist()
    diffs = pc.subtract(ts.slice(1), ts.slice(0, len(ts) - 1))
    nondec = bool(pc.all(pc.greater_equal(diffs, 0)).as_py()) if len(ts) > 1 else True
    rec = {
        "event_date": event_date,
        "rows": table.num_rows,
        "n_ts": n_ts,
        "rows_eq_n_ts_times_n_coins": ts_np_ok,
        "ts_nondecreasing": nondec,
        "first_ts_coin_block": first_block,
        "first_block_matches_manifest": first_block == list(coins),
    }
    if not rec["rows_eq_n_ts_times_n_coins"]:
        raise ValueError(f"{event_date}: row count {table.num_rows} != n_ts {n_ts} × {n_coins}")
    if not rec["ts_nondecreasing"]:
        raise ValueError(f"{event_date}: ts_s not non-decreasing")
    if not rec["first_block_matches_manifest"]:
        raise ValueError(f"{event_date}: first coin block {first_block} != manifest")
    return rec


def build_manifest(
    dst: Path,
    src_manifest: dict[str, Any],
    coins: Sequence[str],
    days: list[dict[str, Any]],
    src: Path,
) -> Path:
    rows = sum(int(d["rows"]) for d in days)
    obj = {
        "schema_version": src_manifest.get("schema_version", SCHEMA_VERSION),
        "status": "complete",
        "source": str(src),
        "source_partition": src_manifest.get("partition"),
        "partition": "event_date=",
        "sort": "ts_s, coin (manifest / canary order)",
        "row_group": "1 hour × n_coins 1 Hz rows",
        "coins": list(coins),
        "n_coins": len(coins),
        "since": src_manifest.get("since"),
        "until": src_manifest.get("until"),
        "cadence": src_manifest.get("cadence", "1Hz"),
        "nan_rule": src_manifest.get("nan_rule"),
        "occ_above_floor_add": src_manifest.get("occ_above_floor_add"),
        "usable": src_manifest.get("usable"),
        "omitted_columns": src_manifest.get("omitted_columns"),
        "git_head": git_head(Path(".")),
        "rows_total": rows,
        "days": days,
        "wide": True,
        "compression": "zstd",
        "note": (
            "Sibling of coin=/event_date= hive. Same rows, time-major within "
            "each UTC day so a backtest can scan all coins together."
        ),
    }
    path = dst / "MANIFEST.json"
    atomic_write_json(path, obj)
    return path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", type=Path, default=DEFAULT_SRC)
    p.add_argument("--dst", type=Path, default=DEFAULT_DST)
    p.add_argument("--part-name", default=DEFAULT_PART_NAME)
    p.add_argument(
        "--all-parts",
        action="store_true",
        help="Concat every part-*.parquet per coin/day (needed for VPS 08-27 part-001 + Mac part-000).",
    )
    p.add_argument("--overwrite", action="store_true")
    p.add_argument(
        "--dates",
        nargs="*",
        default=None,
        help="Optional UTC dates YYYY-MM-DD; default: all dates in source",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    src = args.src.resolve()
    dst = args.dst.resolve()
    src_manifest = load_source_manifest(src)
    coins = list(src_manifest["coins"])
    if len(coins) != 30:
        print(f"warn: manifest n_coins={len(coins)} (expected 30)", flush=True)
    dates = args.dates or list_event_dates(src, coins)
    dst.mkdir(parents=True, exist_ok=True)
    print(
        f"repartition {src} → {dst} dates={len(dates)} coins={len(coins)}",
        flush=True,
    )
    day_recs: list[dict[str, Any]] = []
    t0 = time.time()
    for i, event_date in enumerate(dates, 1):
        dest = dest_part(dst, event_date, args.part_name)
        t_day = time.time()
        if dest.is_file() and not args.overwrite:
            table = pq.ParquetFile(dest).read()
            rec = sanity_day(table, coins, event_date)
            rec["skipped_existing"] = True
            rec["wall_s"] = round(time.time() - t_day, 2)
            day_recs.append(rec)
            print(
                f"[{i}/{len(dates)}] skip {event_date} rows={rec['rows']:,}",
                flush=True,
            )
            continue
        table = concat_one_day(
            src, coins, event_date, args.part_name, all_parts=args.all_parts
        )
        rec = sanity_day(table, coins, event_date)
        write_day(table, dest, overwrite=args.overwrite)
        rec["skipped_existing"] = False
        rec["bytes"] = dest.stat().st_size
        rec["wall_s"] = round(time.time() - t_day, 2)
        day_recs.append(rec)
        print(
            f"[{i}/{len(dates)}] {event_date} rows={rec['rows']:,} "
            f"n_ts={rec['n_ts']:,} {rec['wall_s']:.1f}s "
            f"{dest.stat().st_size / 1e6:.1f} MB",
            flush=True,
        )
        del table
    man = build_manifest(dst, src_manifest, coins, day_recs, src)
    print(
        f"done rows={sum(d['rows'] for d in day_recs):,} "
        f"wall={time.time() - t0:.1f}s manifest={man}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

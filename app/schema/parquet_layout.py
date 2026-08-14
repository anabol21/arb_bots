"""Раскладка parquet на диске (hive-партиции спредов)."""

from __future__ import annotations

from pathlib import Path

# Имена партиций в пути; в body файла не хранятся.
PARTITION_KEYS: tuple[str, ...] = ("base_coin", "event_date")

# Колонка, которую writer считает для партиции и затем отбрасывает из body.
PARTITION_DATE_COL = "event_date"

# The collector publishes small, immutable bar batches here.  This source layout
# is intentionally unchanged; compaction creates a separate durable dataset.
BAR_5M_SOURCE_ROOT = Path("/data/bars/bar_5m")
# v1 artifacts remain immutable at /data/bars_compacted.  v2 owns this root,
# including its .state/ and sent/ lifecycle directories, so validators and
# model readers never accidentally union the generations.
BAR_5M_COMPACTED_ROOT = Path("/data/bars_compacted_v2/bar_5m")
BAR_5M_COMPACTED_LAYOUT_VERSION = 2


def spreads_partition_dir(root: Path | str, base_coin: str, event_date: str) -> Path:
    """Каталог дня: <root>/base_coin=<coin>/event_date=<YYYY-MM-DD>."""
    return Path(root) / f"base_coin={base_coin}" / f"event_date={event_date}"


def bar_5m_compacted_path(
    root: Path | str,
    base_coin: str,
    event_date: str,
    window_start: str,
    window_end: str,
    inputset_digest: str,
) -> Path:
    """Final v2 path for one frozen input set in a closed UTC hour."""
    if len(inputset_digest) < 12:
        raise ValueError("inputset_digest must contain at least 12 hex characters")
    return (
        spreads_partition_dir(root, base_coin, event_date)
        / f"bar_5m_{window_start}_{window_end}_inputset={inputset_digest}.parquet"
    )

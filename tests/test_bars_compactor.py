from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.schema.lean_event import LEAN_BAR_5M_BODY_COLS
from app.schema.parquet_layout import BAR_5M_COMPACTED_ROOT, bar_5m_compacted_path
from app.storage.bars_compactor import (
    BarsCompactorConfig,
    _inputset_digest,
    compact_once,
)
from validation.check_compacted_bars import inspect_compacted_bars


def _write_source(root: Path, name: str, starts: list[int]) -> Path:
    path = root / "base_coin=BTC" / "event_date=2026-08-13" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "bar_start_ts_ms": starts,
            "bar_end_ts_ms": [value + 300_000 for value in starts],
            "base_coin": ["BTC"] * len(starts),
            "ref_exchange": ["okx"] * len(starts),
            "volume": [1.0] * len(starts),
        }
    ).select(LEAN_BAR_5M_BODY_COLS)
    pq.write_table(table, path, compression="zstd")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    return path


def _config(tmp_path: Path) -> BarsCompactorConfig:
    return BarsCompactorConfig(
        source_root=tmp_path / "source",
        output_root=tmp_path / "output",
        archive_root=tmp_path / "archive",
        grace_seconds=0,
        retention_hours=1,
        lock_path=tmp_path / "lock",
    )


def test_v2_default_root_is_separate_from_preserved_v1_root(tmp_path: Path) -> None:
    assert BAR_5M_COMPACTED_ROOT == Path("/data/bars_compacted_v2/bar_5m")
    legacy = tmp_path / "bars_compacted" / "bar_5m" / "bar_5m_legacy.parquet"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"preserved-v1")

    v2_root = tmp_path / "bars_compacted_v2" / "bar_5m"
    result = inspect_compacted_bars(v2_root)

    assert result["files"] == 0
    assert result["errors"] == []
    assert not list(v2_root.rglob("*"))
    assert legacy.read_bytes() == b"preserved-v1"


def test_compacts_archives_and_requires_remote_before_retention(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _write_source(config.source_root, "batch_a.parquet", [1_699_999_600_000])
    second = _write_source(config.source_root, "batch_b.parquet", [1_699_999_900_000])

    result = compact_once(config, now=1_700_004_000)

    output = config.output_root / result["output"]
    assert result["outputs"] == 1
    assert pq.ParquetFile(output).metadata.num_rows == 2
    assert not first.exists() and not second.exists()
    archived = sorted(config.archive_root.rglob("batch_*.parquet"))
    assert len(archived) == 2

    os.utime(archived[0], (1_699_000_000, 1_699_000_000))
    compact_once(config, now=1_700_004_000 + 7200)
    assert archived[0].exists(), "local compaction alone must not authorize retention"

    database = config.output_root / ".state" / "backup_manifest.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE transfers (filename TEXT PRIMARY KEY, state TEXT)")
    connection.execute("INSERT INTO transfers VALUES (?, 'sent')", (result["output"],))
    connection.commit()
    connection.close()
    compact_once(config, now=1_700_004_000 + 7200)
    assert not archived[0].exists()


def test_restart_resumes_published_manifest_after_archive_move(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_source(config.source_root, "batch_a.parquet", [1_699_999_600_000])
    result = compact_once(config, now=1_700_004_000)
    assert result["outputs"] == 1

    # A repeated one-shot sees the archived state and does not duplicate output.
    retry = compact_once(config, now=1_700_004_100)
    assert retry["outputs"] == 0
    assert len(list(config.output_root.rglob("bar_5m_*.parquet"))) == 1
    smoke = inspect_compacted_bars(config.output_root)
    assert smoke["files"] == 1
    assert smoke["errors"] == []
    assert smoke["manifest_states"] == {"archived": 1}
    assert smoke["pending_count"] == 0
    assert smoke["terminal_conflicts"] == 0
    assert smoke["terminal_late_quarantines"] == 0


def test_validator_accepts_sent_copy_before_manifest_remote_retained(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_source(config.source_root, "batch_a.parquet", [1_699_999_600_000])
    result = compact_once(config, now=1_700_004_000)
    active = config.output_root / result["output"]
    retained = config.output_root / "sent" / result["output"]
    retained.parent.mkdir(parents=True)
    os.replace(active, retained)

    smoke = inspect_compacted_bars(config.output_root)

    assert smoke["errors"] == []
    assert smoke["manifest_states"] == {"archived": 1}


def test_changed_or_corrupt_source_fails_without_archiving(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _write_source(config.source_root, "batch_a.parquet", [1_699_999_600_000])

    # Produce a manifest snapshot, then change the batch before materialization.
    from app.storage import bars_compactor

    candidate = bars_compactor._find_candidate(config, 1_700_004_000)
    assert candidate is not None
    coin, event_date, window_start, sources, late_sources = candidate
    assert late_sources == []
    name = "bar_5m_20231114T220000Z_20231114T230000Z.parquet"
    state = bars_compactor._manifest_path(config, coin, event_date, name)
    bars_compactor._atomic_json(
        state,
        {
            "layout_version": 2,
            "status": "planned",
            "base_coin": coin,
            "event_date": event_date,
            "window_start_epoch_s": window_start,
            "window_end_epoch_s": window_start + 3600,
            "output_relative": f"base_coin={coin}/event_date={event_date}/{name}",
            "sources": sources,
            "rows": 1,
        },
    )
    source.write_bytes(b"not parquet")
    with pytest.raises(Exception, match="source changed or missing"):
        compact_once(config, now=1_700_004_000)
    assert source.exists()
    assert not list(config.archive_root.rglob("*.parquet"))


def test_current_open_hour_is_not_eligible(tmp_path: Path) -> None:
    config = _config(tmp_path)
    # 22:00 UTC is still open at 22:30, even though this tiny batch is old.
    _write_source(config.source_root, "batch_a.parquet", [1_700_000_000_000])

    result = compact_once(config, now=1_700_001_800)

    assert result["outputs"] == 0
    assert result["event"] == "bars_compaction_idle"


def test_late_source_for_frozen_window_is_quarantined(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _write_source(config.source_root, "batch_a.parquet", [1_699_999_600_000])
    compact_once(config, now=1_700_004_000)
    assert not first.exists()

    late = _write_source(config.source_root, "batch_late.parquet", [1_699_999_900_000])
    result = compact_once(config, now=1_700_004_000)

    assert result["event"] == "bars_compaction_quarantined_late_sources"
    assert late.exists()
    quarantines = list((config.output_root / ".state" / "quarantine").rglob("*.json"))
    assert len(quarantines) == 1


def test_changed_reappeared_source_for_frozen_window_is_quarantined(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _write_source(config.source_root, "batch_a.parquet", [1_699_999_600_000])
    compact_once(config, now=1_700_004_000)
    assert not first.exists()

    # The same relative name reappears with different frozen bytes/rows/SHA.
    reappeared = _write_source(
        config.source_root, "batch_a.parquet", [1_699_999_900_000]
    )
    result = compact_once(config, now=1_700_004_000)

    assert result["event"] == "bars_compaction_quarantined_late_sources"
    assert reappeared.exists()
    quarantine = next((config.output_root / ".state" / "quarantine").rglob("*.json"))
    payload = __import__("json").loads(quarantine.read_text(encoding="utf-8"))
    assert payload["reason"] == "changed_source_for_frozen_window"
    assert payload["source"]["path"].endswith("batch_a.parquet")


def test_local_output_collision_quarantines_manifest_without_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_source(config.source_root, "batch_a.parquet", [1_699_999_600_000])
    from app.storage import bars_compactor

    candidate = bars_compactor._find_candidate(config, 1_700_004_000)
    assert candidate is not None
    coin, event_date, window_start, sources, late_sources = candidate
    assert late_sources == []
    output = bar_5m_compacted_path(
        config.output_root,
        coin,
        event_date,
        bars_compactor._stamp(window_start),
        bars_compactor._stamp(window_start + 3600),
        _inputset_digest(sources),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    # Valid schema and row count, but a different immutable parquet payload.
    table = pa.table(
        {
            "bar_start_ts_ms": [1_699_999_600_000],
            "bar_end_ts_ms": [1_699_999_900_000],
            "base_coin": ["BTC"],
            "ref_exchange": ["bybit"],
            "volume": [999.0],
        }
    ).select(LEAN_BAR_5M_BODY_COLS)
    pq.write_table(table, output, compression="zstd")

    result = compact_once(config, now=1_700_004_000)

    assert result["event"] == "bars_compaction_quarantined_output_collision"
    state = bars_compactor._manifest_path(config, coin, event_date, output.name)
    manifest = bars_compactor._load_manifest(state)
    assert manifest["status"] == "quarantined"
    assert manifest["quarantine_reason"] == "local_output_identity_collision"
    assert compact_once(config, now=1_700_004_000)["outputs"] == 0
    assert not list(config.archive_root.rglob("*.parquet"))


def test_inputset_digest_makes_output_identity_collision_safe(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _write_source(config.source_root, "batch_a.parquet", [1_699_999_600_000])
    second = _write_source(config.source_root, "batch_b.parquet", [1_699_999_900_000])
    from app.storage import bars_compactor

    candidate = bars_compactor._find_candidate(config, 1_700_004_000)
    assert candidate is not None
    _, _, _, sources, late = candidate
    assert late == []
    digest = _inputset_digest(sources)

    result = compact_once(config, now=1_700_004_000)

    assert f"inputset={digest}" in result["output"]
    assert not first.exists() and not second.exists()


def test_restart_recovers_final_before_manifest_publish(tmp_path: Path) -> None:
    """A crash after final rename cannot accept a different pre-existing file."""
    config = _config(tmp_path)
    _write_source(config.source_root, "batch_a.parquet", [1_699_999_600_000])
    from app.storage import bars_compactor

    candidate = bars_compactor._find_candidate(config, 1_700_004_000)
    assert candidate is not None
    coin, event_date, window_start, sources, late_sources = candidate
    assert late_sources == []
    start, end = bars_compactor._stamp(window_start), bars_compactor._stamp(window_start + 3600)
    output = bar_5m_compacted_path(
        config.output_root, coin, event_date, start, end, _inputset_digest(sources)
    )
    state = bars_compactor._manifest_path(config, coin, event_date, output.name)
    manifest = {
        "layout_version": 2,
        "status": "planned",
        "base_coin": coin,
        "event_date": event_date,
        "window_start_epoch_s": window_start,
        "window_end_epoch_s": window_start + 3600,
        "output_relative": output.relative_to(config.output_root).as_posix(),
        "inputset_sha256": _inputset_digest(sources),
        "sources": sources,
        "rows": 1,
    }
    bars_compactor._atomic_json(state, manifest)
    bars_compactor._write_output(config, manifest, output)
    # Simulate process death before the manifest's output SHA/status is saved.
    result = compact_once(config, now=1_700_004_000)

    assert result["outputs"] == 1
    recovered = bars_compactor._load_manifest(state)
    assert recovered["status"] == "archived"
    assert recovered["output_sha256"] == bars_compactor._sha256(output)

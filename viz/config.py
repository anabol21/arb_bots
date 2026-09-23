"""Paths and caps for the Mac-side spread viz service."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TICKS = REPO / "output" / "lean_ticks"
DEFAULT_CATALOG = REPO / "viz" / "catalog.duckdb"
DEFAULT_WEB_DIST = REPO / "viz" / "web" / "dist"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8787
MAX_ALL_TICK_POINTS = 300_000
GAP_BREAK_MS = 6 * 60 * 1000
FIVE_MIN_MS = 5 * 60 * 1000

# Sync isolation (VPS hop only — never for live UI queries)
DEFAULT_VPS_HOST = "root@38.180.94.108"
DEFAULT_VPS_STAGING = "/root/mac_lean_pull"
DEFAULT_BACKUP_REMOTE = "backup1tb:spread-compacted"
DEFAULT_RCLONE_BIN = "/opt/rclone-1.74.4/rclone"
DEFAULT_TRANSFERS = 4

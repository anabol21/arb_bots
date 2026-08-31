# Research testdata

Offline fixture copies for research / validation. **Not** the live collector mount.

## lean_ticks / 2026-08-08

| Field | Value |
|-------|--------|
| Source | `output/lean_ticks/` (read-only copy; originals untouched) |
| UTC day | `2026-08-08` |
| Coverage | 288 / 288 five-minute slots (0 missing) |
| Storage | Parquet files under this tree are stored via **Git LFS** (see repo `.gitattributes`) |

Use this path for unit tests, notebook smoke checks, and reproducible offline runs. Do not treat it as production VPS/mounted storage.

# Bars backup — 2026-08-05

**Вердикт до фикса: NO** — bars жили только на VPS (`/data/bars`), remote имел лишь tick-артефакты.

## Evidence (до)

| Что | Результат |
|-----|-----------|
| Code `backup_transfer._candidate_files` | только flat `spread_*.parquet` в `BACKUP_COMPACTED_DIR` |
| systemd `spread-backup-transfer` | `BACKUP_COMPACTED_DIR=/data/compacted`, `BACKUP_RCLONE_PATH=spread-compacted` |
| Compactor | только `/data/live` → `/data/compacted`; bars **не** в compaction |
| VPS `/data/bars` | ~5.3M, ~667 parquet под `bar_5m/base_coin=…/event_date=…` |
| Remote `backup1tb:spread-compacted` | только `spread_*.parquet` (~267 objects / ~5.66 GiB) |
| Remote `backup1tb:spread-bars` | **directory not found** |

## Fix

1. `app/storage/backup_transfer.py`: layout `flat` (ticks) / `hive` (bars). Hive использует relative path как identity, confirm+SHA verify до local move в `sent/`, retention 12h.
2. Отдельный unit/timer: `spread-bars-backup-transfer.{service,timer}` → remote `backup1tb:spread-bars`, lock `/run/spread-bars-backup.lock`, log `/var/log/spread/bars-backup-transfer.log`.
3. Tick unit **не** трогали: remote `backup1tb:spread-compacted` без изменений.

## Remote paths

| Dataset | Local first materialization | Durable remote |
|---------|-----------------------------|----------------|
| Ticks (compacted) | `/data/compacted/spread_*.parquet` | `backup1tb:spread-compacted/spread_*.parquet` |
| Bars (`bar_5m`) | `/data/bars/bar_5m/.../batch_*.parquet` | `backup1tb:spread-bars/bar_5m/.../batch_*.parquet` |

После confirm local bars → `/data/bars/sent/<relative>`; expired sent удаляются через 12h.

## Smoke (VPS 2026-08-05 ~12:10 UTC)

- Manual `--max-files 1`: transferred `bar_5m/base_coin=0G/.../batch_…df07d730.parquet` (3555 B) → remote + `/data/bars/sent/...`; `transfer_success=true`.
- Timer `spread-bars-backup-transfer.timer` enabled; oneshot started catch-up of remaining ~665 files (SHA download_verify ≈45–50s/file → multi-hour first drain).
- Tick remote untouched: `backup1tb:spread-compacted` still ~268 objects / ~5.67 GiB (grew only by normal tick transfer).

## Residual risk

- Первый catch-up: много мелких файлов × download_verify → долгий oneshot; timer догонит / продолжит после завершения.
- **Steady-state throughput**: ~сотня bar parquet / 5m при ~50s/file verify **может не успевать** — backlog на `/data/bars` вырастет. Следующий шаг при росте backlog: компакция bars в крупные артефакты (как ticks) или ослабленный verify только size для hive.
- Bars без compaction (намеренно); identity = relative hive path.
- Empty coin/date dirs под `/data/bars/bar_5m` могут оставаться после move (косметика).

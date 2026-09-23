# E2 LIVE OWNERSHIP — FINISHED

**Владелец / Owner:** chat/track **(D) latency E2** (Orchestrator + Validation).  
**Статус:** `finished` — полный повтор ~2h (run id `20260810b` / `e2b`) завершён; отчёт [`latency-e2b-results-20260810.md`](latency-e2b-results-20260810.md).  
**Артефакты:** не удалять / не compact-into / не reclaim disk без согласования Validation/Orchestrator.

Other agents (esp. compaction/backup neighbors): do **not** delete experiment path or rotate/truncate e2b logs without owner approval. PIDs are dead.

| Поле | Значение |
|------|----------|
| Run id | `e2_n1_xrp_20260810b` (`e2b`) |
| Host | `root@38.244.198.42` (historical / E2b host; prod migrated to `38.180.94.108`) |
| Start UTC | `2026-08-10T19:41:36Z` |
| Expected end UTC | `2026-08-10T21:41:36Z` |
| Actual ping end UTC | `2026-08-10T21:41:37Z` (`duration_elapsed` → `finished`) |
| Shadow end UTC | `2026-08-10T22:37:11Z` (`shutdown_flush_done` after post-window TERM) |
| Shadow PID | `1264180` (dead) |
| Ping PID | `1264183` (dead) |
| H1 verdict | **поддержана** — [`latency-e2b-results-20260810.md`](latency-e2b-results-20260810.md) |
| Prior abort (keep intact) | `/var/log/spread/e2_n1_xrp_*`, `/data/experiments/e2_n1_xrp/` — `measurement_failed` @ 18:04Z |
| Shadow PID file | `/var/log/spread/e2b_n1_xrp_shadow.pid` |
| Ping PID file | `/var/log/spread/e2b_n1_xrp_ping.pid` |
| Shadow runtime log | `/var/log/spread/e2b_n1_xrp_runtime.log` |
| Ping dual log | `/var/log/spread/e2b_n1_xrp_ping_dual.log` |
| Parquet / spool | `/data/experiments/e2_n1_xrp_20260810b/live`, `.../spool` |
| VPS marker | `/data/experiments/e2_n1_xrp_20260810b/DO_NOT_TOUCH.md` · `/var/log/spread/E2_LATENCY_OWNED.txt` → **finished** |
| Dashboard | [`latency-e2-dashboard.md`](latency-e2-dashboard.md) |
| Anti-scope this chat | compaction, backup_transfer, retention, disk reclaim, prod `/data/live` |
| Prod collector | `inactive` during E2b (left as-is) |
| Parallel (D) trial | hostcap-c on NEW — [`latency-host-compare-20260810.md`](latency-host-compare-20260810.md) (**complete**; separate verdict) |

### Final status (Validation, `2026-08-10T22:37:20Z`)

| Check | Evidence |
|-------|----------|
| Ping `1264183` | **finished** · `reason=duration_elapsed` · samples OKX 41224 / Bybit 58329 |
| Shadow `1264180` | **shutdown_flush_done** · published_rows=168291 · failures=0 |
| Overlap / steady | ≈120.0 / ≈115.0 мин |
| H1 | **supported** (p99 S/P ≈1.09× / 1.05×; dual>1 с = 0) |
| `gate #1` | **open** (запрет сохранён) |

Канон дизайна: [`latency-e2-dashboard.md`](latency-e2-dashboard.md).

# DOSE-N OWNERSHIP — FINISHED `dose_n50_prod337_20260811d`

> ## ВЛАДЕЛЕЦ / OWNER — Track (D) latency
>
> Запуск `dose_n50_prod337_20260811d` завершён; владение Track (D) latency / Runtime+Validation
> закончилось в `2026-08-11T14:55:48Z`. Артефакты остаются историческими и доступны только для чтения:
> **не** удалять, не обнулять, не ротировать и не уплотнять N50 shadow, PID-файлы, ping, журналы или
> experiment root без отдельного решения. Production PID `24505` был read-only endpoint; его не
> останавливали, не перезапускали и не меняли.
>
> | Arm | PID / unit | UTC window | Isolated artifacts |
> |---|---|---|---|
> | Supervisor | `166358` · `dose-n50-prod337-20260811d.service` | `inactive`, `Result=success`, `ExecMainStatus=0` | `/var/log/spread/dose_n50_prod337_20260811d_{supervisor,runtime,ping_dual}.log` |
> | N=50 shadow | `166387` | штатный shutdown после ping; `shutdown_flush_done`, 564 558 строк, 5 650 файлов | `/data/experiments/dose_n50_prod337_20260811d/{live,spool}` |
> | matched XRP ping | `166430` | `14:20:53.836Z` → `14:50:55.792Z`; `duration_elapsed` | `/var/log/spread/dose_n50_prod337_20260811d_ping_dual.log` |
> | production N≈337 | `24505` · `spread-collector` | XRP lean прочитан в том же окне; unit `active` в `15:02:12Z` | `/data/live/base_coin=XRP/**` |
>
> VPS markers: `/var/log/spread/DO_NOT_TOUCH_LATENCY_DOSE_20260811D.txt` and
> `/data/experiments/dose_n50_prod337_20260811d/DO_NOT_TOUCH.md`. Итоги и локальные raw-копии:
> [`latency-dose-n-results.md`](latency-dose-n-results.md).

# DOSE-N OWNERSHIP — FINISHED `dose_n_20260811c` (historical)

> ## ВЛАДЕЛЕЦ / OWNER — Track (D) latency
>
> **Эксперимент:** `dose_n_20260811c`; владелец — Track (D) latency dose-response / Runtime+Validation; владение окончено.  
> **Исторический DO NOT TOUCH:** unit `dose-n-supervisor-20260811c.service`, его PID и дочерние PID,
> `/var/log/spread/dose_n*20260811c*`, `/var/log/spread/DO_NOT_TOUCH_LATENCY_DOSE_20260811C.txt`,
> `/data/experiments/dose_n{3,10,337_prod}_20260811c`. До завершения серии другим Track (D) работам
> (compaction/backup/retention включительно) разрешено только чтение: **не** kill/restart, truncate/rotate,
> delete/compact/reclaim эти цели. Начало/конец UTC и PID записываются в VPS marker и `arm_meta.env`.

**Статус:** `finished_with_measurement_failure`. `N=3` и `N=10` завершились штатно; `N≈337` невалидна из-за duplicate prod-ping и перезаписанного первого raw ping-log. После второго завершения supervisor стал `failed` (wrapper syntax error); production не остановлен. Итог: [`latency-dose-n-results.md`](latency-dose-n-results.md).
Серия b остаётся историческим `stopped_incomplete` артефактом в [`latency-dose-n-results.md`](latency-dose-n-results.md).

| Поле | Значение |
|------|----------|
| Series date | `20260811c` (current live series) |
| Prior abort | `20260811` / `dose_n1_20260811` — mid-window TERM @ `23:34:12Z` (~12.5 мин); артефакты **keep** |
| Host | `root@38.180.94.108` (`a845945761.local`) |
| Chrony at start (c) | pending supervisor smoke |
| Arms | `n3` → `n10` shadow; затем `n≈337` production XRP ∩ fresh ping |
| Window / arm | 1500 s (~25 мин) |
| Launch | persistent unit `dose-n-supervisor-20260811c.service` + `setsid` children |
| Supervisor | status `/var/log/spread/dose_n_supervisor_20260811c.status`; log `/var/log/spread/dose_n_supervisor_20260811c.log` |
| Ownership marker | `/var/log/spread/DO_NOT_TOUCH_LATENCY_DOSE_20260811C.txt` |
| Dashboard | [`latency-dose-n-dashboard.md`](latency-dose-n-dashboard.md) |
| Results | [`latency-dose-n-results.md`](latency-dose-n-results.md) |
| Prod | must be active and expose readable current XRP lean parquet before `n≈337`; **не** трогать |

### Текущий план и live PIDs (UTC)

| Arm | Definition | Start / expected end | PID(s) / paths |
|---|---|---|---|
| `n3` | shadow rows `327:330`, XRP included; lean, bars `0`, persist `5000` + new ping | started `2026-08-11T12:30:25Z`; nominal end `12:55:25Z` | shadow `140770`, ping `140773`; `/data/experiments/dose_n3_20260811c` |
| `n10` | shadow rows `320:330`, XRP included; same settings + fresh ping | after clean n3 shutdown; nominal `25 min` | allocated by supervisor; `/data/experiments/dose_n10_20260811c` |
| `n≈337` | active production XRP lean ∩ a fresh matched ping; no N=330 shadow | after n10; nominal `25 min` | production PID `24505` remains untouched; `/data/experiments/dose_n337_prod_20260811c` |
| series | persistent supervisor with shutdown allowance | started `12:30:25Z`; ownership protection through `2026-08-11T14:00:25Z` | supervisor `140728`; `dose-n-supervisor-20260811c.service` |

`N≈337` is deliberately a confounded production endpoint, not an exact `N=330` shadow: production has bars
enabled and a different persist setting. The supervisor refuses that arm if production is inactive or if it cannot
see a current readable XRP parquet while `/data/live` is writable.

### Arm N=1 (series b — завершена)

| Поле | Значение |
|------|----------|
| Run id | `dose_n1_20260811b` |
| Start UTC | `2026-08-10T23:36:13Z` |
| Expected end UTC | `~2026-08-11T00:01:13Z` |
| Shadow PID | `32436` |
| Ping PID | `32439` |
| Smoke | `Loaded pairs: 1`; XRP OKX+Bybit subscribed; lean; `collect_bars=false` |
| Paths | `/data/experiments/dose_n1_20260811b/{live,spool}` · `/var/log/spread/dose_n1_20260811b_*` |

### ETA queue (b)

| Рука | Фактический итог |
|------|-----------------------------|
| n1 | `23:36:13Z` → `00:01:15Z`, `duration_elapsed`, valid |
| n3 | `00:01:21Z` → `00:21:55Z`, `signal`, `measurement_failed` |
| n10 | `00:22:01Z` → `00:22:10Z`, `signal`, `measurement_failed` |
| n337_prod | не запускалась |

### Abort note (a)

Первый запуск `dose_n1_20260811` получил `shutdown signal` / ping `stop_requested reason=signal` @ `23:34:12Z` при `remaining_sec≈752`. Похоже на session/group signal (не OOM). Релонч через `systemd-run` + `setsid`.

Systemd зафиксировал деактивацию `dose-n-supervisor.service` в `00:22:02Z`; дочерние процессы получили `signal`. Raw-артефакты сохранены. Другим агентам не изменять и не удалять их без отдельного согласования.

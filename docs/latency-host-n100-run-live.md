# Live record — `hostcmp_n100_20260811b`

## Владение и точный контракт

При фактическом одновременном старте только для каждой активной руки создаются:

```text
/var/log/spread/DO_NOT_TOUCH_LATENCY_HOSTCMP_N100_20260811B.txt
/data/experiments/hostcmp_n100_20260811b_{old,new}/DO_NOT_TOUCH.md
```

Маркер запрещает другим агентам Track (D) до указанного end UTC выполнять `kill`/restart,
truncate/rotate, delete/compact/reclaim её PID, логов, `live`, `spool` и root. Это отдельная
серия: артефакты предыдущего `hostcmp_n100_20260811` не используются.

Обязательный контракт обеих рук:

```text
RUN_ID=hostcmp_n100_20260811b_{old,new}
SPREAD_ROW_START=230
SPREAD_ROW_END=330
SPREAD_LEAN_SCHEMA=1
SPREAD_COLLECT_BARS=0
SPREAD_PERSIST_EVERY=5000
PING=validation/ping_okx_bybit_2h.py --duration-sec 1800
     --okx-inst XRP-USDT-SWAP --bybit-symbol XRPUSDT
RuntimeMaxSec >= 3000
```

`SPREAD_PARQUET_ROOT` и spool будут отдельными на каждом хосте и не пересекутся с `/data/live`.
Никаких production units, compactor, backup, retention или mount configuration этот запуск не
меняет.

## Предстартовая фиксация

Проверка `2026-08-11T15:55:52Z`:

| Arm | Время | Состояние |
|---|---|---|
| OLD `38.244.198.42` | `NTPSynchronized=yes`, chrony `Leap status: Normal` | **blocked_prelaunch**: `spread-compactor.service=activating`, PID `1294389` |
| NEW `38.180.94.108` | `NTPSynchronized=yes`, chrony `Leap status: Normal` | **blocked_prelaunch**: предыдущая NEW-only N=100 серия ещё active (`180270` / `180286` / `180362`) |

Backup services на OLD были inactive. Production collector на OLD remained inactive; production
collector на NEW active с PID `24505` и не был остановлен/перезапущен. Его coexistence — заранее
зафиксированный NEW-only confounder.

## Фактический запуск

После явного разрешения пользователя `spread-compactor.service` на OLD был gracefully остановлен.
Timer немедленно реактивировал service один раз; второй stop дал `MainPID=0` и `1.2 GiB`
available RAM. Перед фактическим launch service реактивировался ещё раз, поэтому выполнен
зафиксированный дополнительный graceful stop. Timer не был disable/mask, backup и production
units не менялись.

Transient services были созданы в `16:01:42Z` (OLD) и `16:01:43Z` (NEW), но оба supervisor
ждали общего epoch barrier. Реальная нагрузка на обеих руках началась одновременно:

| Arm | Unit | Supervisor PID | Shadow PID | Ping PID | Actual start | Expected end |
|---|---|---:|---:|---:|---|---|
| OLD | `hostcmp-n100-20260811b-old.service` | 1295203 | 1295550 | 1295551 | `2026-08-11T16:02:00Z` | `2026-08-11T16:32:00Z` |
| NEW | `hostcmp-n100-20260811b-new.service` | 183887 | 184247 | 184248 | `2026-08-11T16:02:00Z` | `2026-08-11T16:32:00Z` |

Actual start skew is `0 seconds` (within the required 60 seconds). Each supervisor has
`RuntimeMaxSec=50min`, separately owns its shadow and matched 1800-second XRP ping, then
terminates the shadow after ping completion.

Ownership is now materialized on both hosts:

```text
/data/experiments/hostcmp_n100_20260811b_{old,new}/
  DO_NOT_TOUCH.md
  ownership.env
  shadow.pid
  ping.pid
  live/
  spool/
/var/log/spread/DO_NOT_TOUCH_LATENCY_HOSTCMP_N100_20260811B.txt
```

## Требуемый smoke после разблокировки

Smoke at `2026-08-11T16:04:43Z` passed for both arms. Both shadow and ping processes remained
active at age 164 seconds; each collector heartbeat reported `pairs=100`, `schema_mode=lean`,
`collect_bars=false`, zero failures, zero spool files, and publications under its own
`.../live/base_coin=XRP/...` path. Each matched ping had 1075 OKX and 1887–1888 Bybit samples at
120 seconds. OLD had `1.0 GiB` available RAM; NEW had `14 GiB`.

The early SSH check at about 40 seconds timed out on both hosts, while the next read-only smoke
completed successfully. This is an observation to retain for analysis, not a failure conclusion.
Only after both full 1800-second windows are complete may a read-only steady (warmup excluded)
analysis compute p50/p95/p99/max and `S/P`.

## Завершение (историческая запись)

Обе временные units завершились штатно с `Result=success`; matched XRP ping на каждой руке
завершился по `duration_elapsed`, а runtime записал `shutdown_flush_done`. Общий
steady-overlap после исключения пяти минут warmup составляет `25.030` минуты. Raw roots и
логи были скопированы read-only; production NEW, compactor, backup и timers в рамках
завершения серии не изменялись.

Полный dashboard с p50/p95/p99/max, `S/P`, dual-spike, проверками валидности, источниками
и границами вывода: [`latency-host-n100-results-20260811.md`](latency-host-n100-results-20260811.md).


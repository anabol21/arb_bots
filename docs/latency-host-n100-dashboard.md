# Dashboard — `hostcmp_n100_20260811b`

## Цель и симметричный дизайн

Новая чистая серия должна одновременно (разница старта не более 60 секунд) запустить:

| Arm | Host | Run ID |
|---|---|---|
| OLD | `root@38.244.198.42` | `hostcmp_n100_20260811b_old` |
| NEW | `root@38.180.94.108` | `hostcmp_n100_20260811b_new` |

У каждой руки: shadow `app/screaner_b_o.py`, `rows[230:330]` (ровно 100 пар, включая XRP),
lean schema, `SPREAD_COLLECT_BARS=0`, `SPREAD_PERSIST_EVERY=5000`, изолированные `live`/`spool`
и отдельный fresh XRP ping (`OKX XRP-USDT-SWAP` + `Bybit XRPUSDT`) на 1800 секунд. Анализирует
только steady-интервал: 25 минут после исключения первых 5 минут.

Временный service каждой руки должен иметь `RuntimeMaxSec >= 3000`, отдельные PID и логи. До
старта создаются `/var/log/spread/DO_NOT_TOUCH_LATENCY_HOSTCMP_N100_20260811B.txt` и
`DO_NOT_TOUCH.md` в каждом experiment root; до end UTC другим Track-D агентам запрещены
`kill`/restart, truncate/rotate, delete/compact/reclaim этих артефактов.

## Проверка перед стартом — `2026-08-11T15:55:52Z`

| Проверка | OLD | NEW |
|---|---|---|
| `timedatectl` | `NTPSynchronized=yes` | `NTPSynchronized=yes` |
| `chronyc tracking` | `Leap status: Normal`, system offset `+0.009 ms` | `Leap status: Normal`, system offset `+0.000007 ms` |
| Production collector | `inactive`, PID `0` | `active`, PID `24505`; не трогать |
| Целевые процессы/roots `...20260811b...` | отсутствуют | отсутствуют |
| Предыдущий NEW-only N=100 | n/a | всё ещё active: supervisor `180270`, shadow `180286`, ping `180362` |
| Compactor/backup | `spread-compactor.service` **activating**, PID `1294389`; backup units inactive | не применимо |

## Статус

**Завершено и валидно для ограниченного host comparison.** Обе руки штатно завершились:
`systemd Result=success`, matched XRP ping закончил 1800-секундное окно, XRP parquet
читается, spool пуст, и после warmup есть общий steady `25.030` минуты. Полный verdict,
таблицы и ограничения причинного вывода: [`latency-host-n100-results-20260811.md`](latency-host-n100-results-20260811.md).

Историческая запись: перед запуском OLD compactor был gracefully stopped по явному
разрешению пользователя. Его timer кратко реактивировал service в prelaunch, после чего
service снова был gracefully stopped без disable/mask timer. OLD production оставался
inactive. NEW production продолжал работать и остался documented NEW-only confounder;
после эксперимента этот dashboard не предписывает менять любой из этих services.

Both arms use a common epoch barrier and recorded the actual workload start as
`2026-08-11T16:02:00Z` — zero-second skew:

| Arm | Unit | Supervisor / shadow / ping PID | Isolated root |
|---|---|---|---|
| OLD | `hostcmp-n100-20260811b-old.service` | `1295203` / `1295550` / `1295551` | `/data/experiments/hostcmp_n100_20260811b_old` |
| NEW | `hostcmp-n100-20260811b-new.service` | `183887` / `184247` / `184248` | `/data/experiments/hostcmp_n100_20260811b_new` |

Each root has `live`, `spool`, `DO_NOT_TOUCH.md`, `ownership.env`, and recorded PIDs; the shared
host-level marker is `/var/log/spread/DO_NOT_TOUCH_LATENCY_HOSTCMP_N100_20260811B.txt`.

Two-minute smoke at `16:04:43Z`: both units and child processes were active, both collectors
reported 100 lean pairs with bars disabled and no failures/spool backlog, both fresh XRP pings had
samples on OKX and Bybit, and each root had XRP parquet (`18` files at observation). Clock
preflight: both `Leap status: Normal`; OLD system time `+0.009 ms`, NEW `+0.000006 ms`.

## Исторические конфаундеры этой серии

- NEW production `spread-collector` PID `24505` остаётся active и является NEW-only confounder.
- OLD compactor был неактивен в течение испытания; это делает OLD легче обычного состояния.
- Валидация после завершения подтвердила `Loaded pairs: 100`, XRP в обеих подписках,
  читаемый parquet и полный 1800-second ping на обеих руках.


# L1 window 2 live — `l1_n337_write_20260816w2`

> ## ВЛАДЕЛЕЦ / OWNER — Track (D) latency
>
> Окно 2 **завершено** (`ping_exit=0` в `20:33:40Z`). Итог:
> [результаты w2](latency-l1-w2-results.md), [вердикт L1](latency-l1-verdict.md).
> Сборщик PID 613941 ещё active — это не третье окно.

| Поле | Значение |
|------|----------|
| Трек | (D) latency; не B |
| Профиль | L1: N=337 lean+bars, тики backup, бары local, v2 |
| Хост | `root@38.180.94.108` |
| Collector | PID **613941**, `NRestarts=0` (после stop PID 607951) |
| W1 stop / flush | `19:19:35Z` `shutdown_flush_done` published_rows=3727558 |
| Collector start | `2026-08-16T19:19:36Z` |
| Subscribe-ready | `2026-08-16T19:23:40Z` (`ws_subscribe_ok=1005`) |
| Ping start | `2026-08-16T19:23:40Z` |
| Warmup end | `2026-08-16T19:33:40Z` |
| Steady / ping end | `2026-08-16T20:33:40Z` |
| Experiment root | `/data/experiments/l1_n337_write_20260816w2/` |
| Unit | `latency-l1-20260816w2.service` |

Premature ping в `19:19:36Z` снова схватил старый heartbeat; остановлен.
Рабочий ping после cut line `474233`. Сборщик не перезапускали.

Рынок окна 1 (все монеты): [market w1](latency-l1-market-w1.md).

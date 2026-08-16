# L1 window 2 results — `l1_n337_write_20260816w2`

Трек: (D) latency. **Не Track B.** Тот же профиль, что окно 1: N=337
lean+bars, тики backup, бары local, reconnect v2, compact/backup on.
Процесс **независимый**: PID **613941** после stop PID 607951.

| Поле | Значение |
|------|----------|
| Вердикт окна | быстрый отказ пройден; вместе с окном 1 профиль `L1` принят |
| Steady | `2026-08-16T19:33:40Z` → `20:33:40Z` |
| Collector | PID 613941, `NRestarts=0` |
| Ping | `ping_exit=0`, 19682 OKX / 31852 Bybit samples за 4200 s |
| S | XRP в compacted 19:30–20:35 |
| Planned reconnect | 16 / 16; unplanned=0; wave Bybit max 9 (не gate) |

## XRP pooled

| Серия | n | p50 | p95 | p99 | max | S/P p99 |
|-------|--:|----:|----:|----:|----:|--------:|
| S OKX | 16528 | 30 | 34 | **48** | 619 | **1.30×** |
| P OKX | 16530 | 30 | 33 | 37 | 241 | — |
| S Bybit | 26762 | 18 | 22 | **38** | 785 | **1.52×** |
| P Bybit | 26780 | 18 | 20 | 25 | 249 | — |

Dual минуты: 61. `dual>500` = **1** (`20:01Z`, S max 619 / 785, ping p99 43 / 30). `dual>1000` = 0.

## Рынок окна 2

2 355 900 тиков, 336 монет.

| | OKX | Bybit |
|--|----:|------:|
| все тики p50 / p99 / max | 31 / **55** / 811 | 18 / **39** / 794 |
| p50 монет (медиана / max) | 31 / 37 | 18 / 22 |
| p99 монет (медиана / max) | 55 / 239 POET | 38 / 314 ORDER |

## Gates окна 2

| Gate | Факт | Вердикт |
|------|------|---------|
| pooled p99 < 100 | 48 / 38 | pass |
| S/P ≤ 2.0× | 1.30× / 1.52× | pass |
| dual>500 ≤1; >1000 =0 | 1 / 0 | pass |
| unplanned / protocol / unrecovered | 0 / 0 / 0 | pass |
| backpressure / fail / spool | 0 / 0 / 0 | pass |
| ping overlap | весь steady, ping reconnect 0 | pass |
| loop lag / 1s CPU RSS FD | не снимались | нет артефакта |

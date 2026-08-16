# L1 results — `l1_n337_write_20260816` window 1

Трек: (D) latency. **Не Track B.** Профиль: N=337 lean+bars, тики → backup,
бары VPS-local, reconnect v2, compact/backup включены.

| Поле | Значение |
|------|----------|
| Вердикт окна | быстрый отказ пройден; профиль позже принят после окна 2 |
| Приёмка профиля | см. [`latency-l1-verdict.md`](latency-l1-verdict.md); задержка цикла и посекундные ресурсы не снимались |
| Хост | `root@38.180.94.108`, PID **607951**, `NRestarts=0` |
| Steady | `2026-08-16T18:05:27Z` → `19:05:27Z` |
| S источник | XRP в `compacted/sent` + хвост `19:05–19:10` |
| P источник | `/data/experiments/l1_n337_write_20260816/ping_xrp.jsonl` |

## Pooled (не p99 от минутных p99)

| Серия | n | p50 | p95 | p99 | max | S/P p99 |
|-------|--:|----:|----:|----:|----:|--------:|
| S OKX | 18484 | 30 | 37 | **80** | 618 | **1.74×** |
| P OKX | 18484 | 29 | 35 | 46 | 394 | — |
| S Bybit | 28883 | 18 | 24 | **61** | 610 | **1.75×** |
| P Bybit | 28883 | 17 | 22 | 35 | 241 | — |

Dual минуты с обеими ногами: **61**. `dual>500` = **1** (`18:18Z`, S max 567 / 536 vs ping p99 39 / 30). `dual>1000` = **0**.

## Gates этого окна

| Gate | Результат |
|------|-----------|
| pooled p99 < 100 ms | pass (80 / 61) |
| S/P p99 ≤ 2.0× | pass (1.74× / 1.75×) |
| dual>500 ≤ 1 / dual>1000 = 0 | pass (1 / 0) |
| unplanned / protocol / unrecovered | pass (все 0; wave 0) |
| backpressure / failures / spool | pass (queue_depth 0, failures 0, spool 0) |
| ping overlap на весь steady | pass; ping reconnect 0 |
| loop lag p99 < 20 ms | **не измерялся** |
| CPU/RSS/FD 1s snapshots | **не измерялись** |

Сравнение с production 11.08 (тот же N≈337, другой бинарник): p99 тогда 612 / 676 ms, S/P 13–17×, dual>500 в 25/26 мин. Это **не** тот же профиль: сейчас v2 + fail-closed + scheduler.

## Что не утверждаем

- Не `L1 accepted` и не `B READY`.
- Не причина хвоста 11.08.
- Минутный p99 ≥ 100 в 26 минутах не ломает контракт: gate — pooled p99.
- Следующий час того же процесса **нельзя** считать вторым окном.

Дашборд: [L1 window 1](/Users/mishatrubik/.cursor/projects/Users-mishatrubik-Desktop-spread/canvases/latency-l1-window1.canvas.tsx).
Дизайн: [лестница](latency-profile-ladder-design.md).

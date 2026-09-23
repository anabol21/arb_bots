# Результаты controlled B↔C — `wsfanout_bc_20260812r1`

## Итоговый вердикт

**`B=valid`; `C=measurement_failed`; причинный вердикт B↔C не допускается.**
Обе руки штатно завершили `probe` и fresh XRP ping с кодом `0`, supervisor
зафиксировал `supervisor_finished` в `2026-08-12T18:45:07Z`, а systemd service
имеет `Result=success`. Но в steady-окне `C` получены `6` незапланированных
reconnect на OKX и `91` на Bybit (лимит `≤1` на биржу), а максимальная волна
достигла `4` и `25` за 60 s соответственно (лимит `≤3`). По заранее
объявленному контракту это invalidates всю B↔C causal calculation.

Это standalone shadow опыт Track `(D)`: persistence, parquet, publisher,
spool и bars выключены. Результат не валидирует `app/screaner_b_o.py`, mounted
storage либо production profile и не открывает Track `(B)`.

## Provenance и воспроизводимость

| Поле | Наблюдение |
|---|---|
| VPS / service | NEW VPS `root@38.180.94.108`; `wsfanout-bc-20260812r1.service` |
| Порядок / профиль | `B → C`; `N=300` с XRP; `600` expected connections и subscription sends |
| Неизменные факторы | `websockets 16.1`; batch `30 pairs / 3 s`; retry `10 s`; library-default `max_queue`; `3600/600/3000 s` wall/warmup/steady |
| Единственный плановый фактор | `B` raw-drain/discard non-XRP; `C` full in-memory decode/quote/spread non-XRP |
| Фактические окна | B `16:45:05.282Z–17:45:05.707Z` (`3600.425 s`); C `17:45:06.526Z–18:45:06.750Z` (`3600.224 s`) |
| Локальная копия | `output/wsfanout_bc_20260812r1/raw_vps/`; SHA-256 совпали для всех `22/22` VPS artifacts |
| Повторный расчёт | `output/wsfanout_bc_20260812r1/analyze_bc.py` → `output/wsfanout_bc_20260812r1/analysis/analysis.json` |

Фон записан до и после обеих рук; production `spread-collector.service` был
active (PID `24505`), compactor timer — active, сам compactor service — inactive.
Фон не переключался и не был отдельным исследуемым фактором.

## Gates валидности

| Gate | B | C | Серийный вывод |
|---|---:|---:|---|
| Initial opens/subscriptions до steady | `600/600` | `600/600` | pass |
| Active / pending в последнем pre-shutdown snapshot | `600 / 0` | `600 / 0` | pass |
| `unplanned_reconnect` OKX / Bybit (≤1) | `0 / 0` | `6 / 91` | **C fail** |
| Max reconnect wave 60 s (≤3) OKX / Bybit | `0 / 0` | `4 / 25` | **C fail** |
| `connection_error` / `unplanned_close` OKX / Bybit | `0 / 0` | `6 / 91` | C forensic failure |
| Raw XRP delivery, loop-lag, matched ping post-warmup | complete | complete | pass |
| Ping `connection_error` OKX / Bybit | `0 / 0` | `0 / 0` | pass |
| `safety_abort` | `0` | `0` | pass |
| Arm status | `valid` | `measurement_failed` | **matrix запрещена** |

У C все connection были восстановлены к pre-shutdown snapshot, но recovery не
исправляет превышение заранее заданных reconnect/wave limits. `697` total
subscription sends C — следствие 97 повторных подключений, а не недостача
initial `600/600`.

## Exact pooled raw steady metrics

Первые `600 s` исключены. `S` — XRP delivery probe, `P` — matched XRP ping той
же биржи. Percentiles рассчитаны по объединённым raw samples, не по minute
percentiles; строки C описательны и не являются evidence для matrix.

| Arm / leg | S n; p50 / p95 / p99 / max, ms | P n; p50 / p95 / p99 / max, ms | S/P p99 |
|---|---:|---:|---:|
| `B` OKX | 20,007; 30 / 72 / 131 / 541 | 20,007; 31 / 74 / 130 / 411 | 1.008× |
| `B` Bybit | 25,966; 18 / 57 / 102 / 333 | 25,966; 18 / 57 / 102 / 414 | 1.000× |
| `C` OKX* | 18,977; 31 / 50 / 86 / 585 | 18,978; 30 / 47 / 85 / 388 | 1.012× |
| `C` Bybit* | 23,034; 20 / 36 / 60 / 360 | 23,035; 21 / 35 / 58 / 450 | 1.034× |

\* C нарушила reconnect/wave gates; низкие raw percentiles не разрешают
приписывать разницу full parse/calc.

| Arm | Loop n; p50 / p95 / p99 / max, ms | Loop `>200 / >500 / >1000 ms` | Dual delivery `>500 / >1000 ms` |
|---|---:|---:|---:|
| `B` | 15,001; 0.567 / 1.168 / 1.514 / 23.898 | 0 / 0 / 0 | 0 / 0 из 50 |
| `C`* | 15,001; 0.446 / 1.141 / 2.126 / 100.113 | 0 / 0 / 0 | 0 / 0 из 50 |

## Ресурсы, поток и reconnect-forensics

| Arm | CPU p50 / p95 / max, % одного core | RSS p50 / p95 / max, MiB | FD p50 / p95 / max | Frames/s OKX / Bybit | Bytes/s OKX / Bybit |
|---|---:|---:|---:|---:|---:|
| `B` | 17.97 / 23.97 / 45.92 | 106.89 / 107.15 / 107.15 | 610 / 610 / 610 | 579.8 / 617.4 | 239.8 / 118.6 KiB/s |
| `C` | 20.95 / 27.94 / 72.82 | 111.38 / 114.22 / 114.48 | 610 / 610 / 610 | 551.5 / 604.6 | 227.9 / 116.1 KiB/s |

Минимум доступной памяти — B `13.54 GiB`, C `13.56 GiB`; load-1 p95/max —
B `1.229/1.757`, C `1.312/1.531`. Все safety resource budgets выполнены.
Однако C потребляла дополнительно около `6.97 MiB` RSS p95 и `3.97 pp` CPU
p95; это описательное наблюдение, а не объяснение reconnect incident.

B приняла и отбросила non-XRP raw frames: OKX `1,718,796`, Bybit `1,825,505`;
`json_loads` в steady — только XRP (`20,001` / `25,960`). C полностью
обработала non-XRP: `json_loads` OKX/Bybit `1,654,158 / 1,813,326`,
`spread_calculations` `1,654,152 / 1,811,410`; protocol errors в обеих руках
равны нулю. Это подтверждает назначенное различие workload, но не обходит
quality gate.

## B↔C решение и контекст Cdiag

Решение: **`not assessed`** для «full parse/calc dominant», «connection fan-out
remains dominant», «mixed» и «neither reproduced». Для любого из этих исходов
должны быть valid обе руки; `C=measurement_failed`.

`Cdiag/r1` остаётся только контекстом: он показал full parse/calc с `0/0`
reconnect на изменённом batching/retry/queue profile. Его нельзя смешивать с
этим B↔C расчётом: другой временной интервал и одновременно изменённые факторы
не создают paired control.

## Следующий шаг по принятой лестнице

P1 остаётся минимальным: повторить controlled B↔C с тем же immutable profile
только после отдельной проверки причины C reconnect/wave incident и с заранее
сохранёнными raw artifacts. Лишь после valid B и C применять B↔C matrix; затем
P2 — randomised controlled background off/on, затем P3 — два независимых
production-like 60-min окна с bars/persistence по acceptance contract.

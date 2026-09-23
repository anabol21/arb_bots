# Live record — `wsfanout_cplus_20260813r1`

## Статус завершения

**Серия завершена.** `supervisor_finished` в `2026-08-13T20:38:14Z`.
`probe_status=0`, `ping_status=0`, `clean_shutdown=true`. Transient unit к
моменту разбора уже отсутствует. Итоговый вердикт:
[результаты](latency-ws-fanout-cplus-results.md) — `C+=measurement_failed`;
причинное сравнение с валидной C запрещено из-за reconnect/wave на Bybit.

Smoke ниже — историческая запись хода серии, не финальный расчёт. Raw root на
VPS сохранён; блобы `pickle` на VPS не удалять. Другие работы Track D могут
читать experiment root, но нельзя его удалять, сжимать, архивировать или
изменять. Production collector, `/data/live`, spool и mount не трогать.

## Блок конвейера и границы

Трек `(D)`, standalone shadow: WebSocket fan-out → drain-path → full
in-memory decode/quote/spread → **off-drain** batch pickle write.

Сравнение после окна: C+ против валидной C `wsfanout_bc_20260813r2` по
`S/P p99` и dual **не допускается** — рука `measurement_failed`. Итог:
[результаты](latency-ws-fanout-cplus-results.md).

## Выбор формата

Выбран **pickle**, не parquet.

- C обрабатывает порядка `1100` тиков/с. Parquet потребовал бы сбор
  DataFrame/Table и pyarrow на пути записи.
- `pickle.dumps` компактных кортежей не тянет pandas/pyarrow на горячий путь
  и держит GIL короче, чем конвертация колонок.
- Эксперимент измеряет лишнюю нагрузку записи, а не удобство чтения.
- Fsync на каждую строку не делается.

## Путь записи

- Каталог: `/data/experiments/wsfanout_cplus_20260813r1/arm_Cplus/writes/`
- Не `/data/live`, не production spool.
- После `full_handle` запись только `put_nowait` в bounded `queue.Queue`
  (`20000`). Drain-task сокета не пишет на диск.
- Worker-thread `cplus-pickle-writer`: flush при `1000` записях **или** `1.0 s`
  (что раньше). На потоке C это около одного файла в секунду.
- Нет fsync. Файл: `part-NNNNNN.pkl` через tmp+replace.
- Backpressure: очередь полна → drop, счётчики `dropped` /
  `backpressure_hits`.
- Телеметрия: `write_metrics.csv`, `writes/write_flush.csv`, поле `write` в
  `runtime.jsonl`.

## Что совпадает с валидной C

Неизменные knobs: хост NEW `root@38.180.94.108`, `N=300` с XRP, `600`
сокетов, batch `30 pairs / 3 s`, retry `10 s`, omitted `max_queue`,
`receive_mode=immediate_drain_unbounded_app_queue`, `3600/600/3000 s`,
свежий matched XRP ping, без bars / spool / remote backup / изменений
compaction.

Единственный новый фактор: локальная durable-ish запись обработанных
записей.

## Gates

Рука `measurement_failed`, сравнение с C не допускается, если:

- до steady нет `600/600` opens и subscription sends;
- больше одного unplanned reconnect на биржу, любой unrecovered, либо wave
  `>3` за rolling `60 s`;
- нет полных raw XRP delivery, loop-lag и fresh ping на steady;
- есть `safety_abort`.

Gates не ослаблялись.

## Ожидаемые артефакты

```text
/data/experiments/wsfanout_cplus_20260813r1/
  DO_NOT_TOUCH.md
  run_manifest.json
  supervisor.status
  universe_300.json
  arm_Cplus/
    arm_manifest.json
    pids.env
    runtime.jsonl
    ping_xrp.log
    xrp_delivery_okx.csv
    xrp_delivery_bybit.csv
    loop_lag.csv
    write_metrics.csv
    writes/
      write_flush.csv
      part-*.pkl
```

Markers:

```text
/data/experiments/wsfanout_cplus_20260813r1/DO_NOT_TOUCH.md
/var/log/spread/DO_NOT_TOUCH_WSFANOUT_wsfanout_cplus_20260813r1.txt
```

## Запуск

Series запущена `2026-08-13T19:38:13Z` как persistent transient service
`wsfanout-cplus-20260813r1.service`: supervisor PID `375584`, probe PID
`375653`, fresh matched-ping PID `375652`. Manifest ожидает окончание
`2026-08-13T20:38:13Z`; `RuntimeMaxSec=75min`. Фактическое завершение —
`supervisor_finished` в `2026-08-13T20:38:14Z`; вердикт валидности — в
[результатах](latency-ws-fanout-cplus-results.md).

Production `spread-collector.service` остаётся `active`, PID `24505`.

## Smoke

Read-only smoke в `2026-08-13T19:41:19Z` (`185.693 s`) подтверждает:

- service `active`, `NRestarts=0`; supervisor/probe/ping PIDs
  `375584` / `375653` / `375652`;
- `600/600` active connections, по `300/300` opens и subscription sends на
  OKX/Bybit; pending recovery `0`;
- `receive_mode=immediate_drain_unbounded_app_queue`;
  `recv_pending_max` OKX `2`, Bybit `7`;
- raw XRP delivery: `1101` OKX и `1301` Bybit строк (включая header);
  loop-lag `901` строк; ping `2528` JSONL;
- pickle write: `241` файлов, `230752` записанных записей, `18.6 МиБ`,
  `dropped=0`, `queue_depth=1` (max `107`), `last_write_latency_ms=1.479`,
  `write_errors=0`;
- отсутствуют `unplanned_reconnect`, `connection_error`, `unplanned_close` и
  `safety_abort`;
- probe CPU `25.95%`, RSS `112.2 MiB`, FD `611`, load-1 `0.67`, available
  memory `14.0 GiB`;
- production collector по-прежнему `active`, PID `24505`.

Это warmup-only operational smoke, а не latency или causal verdict против C.
Эти standalone artifacts не доказывают production safety или
mounted-storage correctness.

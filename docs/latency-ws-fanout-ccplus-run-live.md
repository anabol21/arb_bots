# Live record — `wsfanout_ccplus_20260813r1`

## Статус завершения

**Серия завершена.** `supervisor_finished` в `2026-08-13T23:36:51Z`.
Обе руки `probe_status=0`, `ping_status=0`, `clean_shutdown=true`. Transient
unit к моменту разбора уже отсутствует. Итоговый вердикт:
[результаты](latency-ws-fanout-ccplus-results.md) — `C=measurement_failed`,
`C+=measurement_failed`; гипотеза локальной записи **не оценивается**.

Smoke ниже — историческая запись хода серии, не финальный расчёт. Raw root на
VPS сохранён; блобы `pickle` на VPS не удалять. Другие работы Track D могут
читать experiment root, но нельзя его удалять, сжимать, архивировать или
изменять. Production collector, `/data/live`, spool и mount не трогать.
Предыдущие корни `wsfanout_cplus_20260813r1` и `wsfanout_bc_20260813r2` не
трогать.

## Блок конвейера и границы

Трек `(D)`, standalone shadow: WebSocket fan-out → drain-path → full
in-memory decode/quote/spread. Рука `C` на этом останавливается. Рука `C+`
добавляет **off-drain** пакетную запись `pickle` существующим
`LocalPickleWriter` из `validation/ws_fanout_three_arm.py`. Второй путь записи
не вводился.

## Единственный различающий фактор

Неизменные knobs обеих рук: хост NEW `root@38.180.94.108`, `N=300` с XRP,
`600` сокетов, batch `30 pairs / 3 s`, retry `10 s`, omitted `max_queue`,
`receive_mode=immediate_drain_unbounded_app_queue`, `3600/600/3000 s`, свежий
matched XRP ping на каждую руку, без bars / spool / remote backup / изменений
compaction.

Единственный различающий фактор:

- `C`: полной обработки non-XRP достаточно; локальной записи market-data нет;
- `C+`: та же обработка плюс существующий поток `cplus-pickle-writer`, flush
  при `1000` записях **или** `1.0 s`, изолированный каталог, без fsync на
  строку.

## Правило решения (заранее)

Gates **не ослабляются**. Если любая рука нарушает gate, серия —
`measurement_failed`, причинный вывод запрещён.

- `C` валидна и тихая **и** `C+` валидна и хуже по reconnect/`1011` либо по
  `S/P p99` / dual / loop → гипотеза локальной записи **поддерживается**.
- обе валидны и `C≈C+` по reconnect/`1011` и по `S/P p99` / dual / loop →
  гипотеза **ослаблена**.
- любая рука невалидна → гипотеза **не оценивается**.

Итог после окна: обе руки `measurement_failed`, гипотеза **не оценивается**.
См. [результаты](latency-ws-fanout-ccplus-results.md).

## Порядок и seed

Seed `wsfanout_ccplus_20260813r1_order` детерминировал порядок `C → C+`.
Факт записан в `run_manifest.json`. Фактические start/end и PID фиксируются в
`arm_*/arm_manifest.json`, `arm_*/pids.env` и `supervisor.status`.

## Gates

Рука `measurement_failed`, сравнение запрещено, если:

- до steady нет `600/600` opens и subscription sends;
- больше одного unplanned reconnect на биржу, любой unrecovered, либо wave
  `>3` за rolling `60 s`;
- нет полных raw XRP delivery, loop-lag и fresh ping на steady;
- есть `safety_abort`.

## Ожидаемые артефакты

```text
/data/experiments/wsfanout_ccplus_20260813r1/
  DO_NOT_TOUCH.md
  run_manifest.json
  supervisor.status
  universe_300.json
  arm_C/
    arm_manifest.json
    pids.env
    runtime.jsonl
    ping_xrp.log
    xrp_delivery_okx.csv
    xrp_delivery_bybit.csv
    loop_lag.csv
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
/data/experiments/wsfanout_ccplus_20260813r1/DO_NOT_TOUCH.md
/var/log/spread/DO_NOT_TOUCH_WSFANOUT_wsfanout_ccplus_20260813r1.txt
```

## Запуск

Series запущена `2026-08-13T21:36:49Z` как persistent transient service
`wsfanout-ccplus-20260813r1.service`: supervisor PID `389153`, первая рука
`C`, probe PID `389228`, fresh matched-ping PID `389227`. Seed
`wsfanout_ccplus_20260813r1_order` детерминировал порядок `C → C+`. Manifest
ожидал окончание серии около `2026-08-13T23:36:49Z`; `RuntimeMaxSec=150min`.
Фактическое завершение — `supervisor_finished` в `2026-08-13T23:36:51Z`;
C+ probe/ping `395079` / `395078`. Вердикт валидности — в
[результатах](latency-ws-fanout-ccplus-results.md).

Production `spread-collector.service` остаётся `active`, PID `24505`.

## Smoke

Read-only smoke в `2026-08-13T21:39:58Z` (`~189 s` C, затем снимок метрик
`201.641 s`) подтверждает:

- service `active`, `NRestarts=0`; supervisor/probe/ping PIDs
  `389153` / `389228` / `389227`;
- `600/600` active connections, по `300/300` opens и subscription sends на
  OKX/Bybit; pending recovery `0`;
- `receive_mode=immediate_drain_unbounded_app_queue`;
  `recv_pending_max` OKX `3`, Bybit `10`;
- raw XRP delivery: `901` OKX и `1101` Bybit строк (включая header);
  loop-lag `901` строк; ping `2128` JSONL;
- `write=null`, каталога `arm_C/writes/` нет — первая рука без локальной
  записи; каталога `arm_Cplus/` ещё нет;
- отсутствуют `unplanned_reconnect`, `connection_error`, `unplanned_close` и
  `safety_abort`;
- probe CPU `16.97%`, RSS `110.7 MiB`, FD `610`, load-1 `0.85`, available
  memory `14.0 GiB`;
- production collector по-прежнему `active`, PID `24505`.

Это warmup-only operational smoke первой руки, а не latency или causal
verdict C↔C+. Эти standalone artifacts не доказывают production safety или
mounted-storage correctness.

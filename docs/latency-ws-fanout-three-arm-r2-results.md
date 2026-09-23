# Результаты A/B/C repeat — `wsfanout_abc_20260812r2`

## Вердикт

**`A=valid`, `B=valid`, `C=measurement_failed`; весь latency gate остаётся
`NOT FINAL`.** В `C` за steady-окно зафиксированы `6` unplanned reconnect на
OKX и `94` на Bybit при лимите `≤1` на биржу. Поэтому A/B/C matrix не может
классифицировать connection/FD и parse/calc факторы. Кроме того, r2 — это
standalone shadow без bars/persistence; controlled background off/on и два
production-like окна ещё отсутствуют. Track (B) остаётся закрытым.

Read-only разбор signature и ограничений атрибуции: [forensics C reconnect](latency-ws-fanout-c-reconnect-forensics.md).
Standalone C diagnostic repeat с отдельным reconnect validity contract:
[live record](latency-ws-fanout-cdiag-run-live.md) и
[Cdiag results](latency-ws-fanout-cdiag-results.md): valid `0/0` reconnect
(OKX/Bybit) after changed batching/retry/queue profile; this does not repair
the time-separated r2 B↔C causal limitation.

Сводная русская панель фактов и оставшегося causal gap:
[A/B/C evidence dashboard](latency-ws-fanout-abc-dashboard.md).

## 1. Provenance, completion и inventory

С NEW VPS `root@38.180.94.108` read-only скопирован полный experiment root
`/data/experiments/wsfanout_abc_20260812r2/` в
`output/wsfanout_abc_20260812r2/raw_vps/`, а также marker-log
`/var/log/spread/DO_NOT_TOUCH_WSFANOUT_wsfanout_abc_20260812r2.txt`.
SHA-256 всех 31 файлов experiment root на VPS совпали с локальной копией.

`run_manifest.json` фиксирует порядок `A → B → C`, 3600 s wall, 600 s warmup
и 3000 s steady. `supervisor.status` подтверждает `probe_status=0` и
`ping_status=0` для каждой arm, а также `supervisor_finished` в
`2026-08-12T01:16:23Z`. Все arm manifests содержат тот же `run_id` и
`probe_exit_status=ping_exit_status=0`.

Полный inventory: root manifests (`run_manifest.json`, `supervisor.status`,
`universe_300.json`, `DO_NOT_TOUCH.md`), и для каждой `A/B/C` —
`arm_manifest.json`, `pids.env`, `runtime.jsonl`, `ping_xrp.log`,
`xrp_delivery_okx.csv`, `xrp_delivery_bybit.csv`, `loop_lag.csv`, плюс пустые
`probe_stdout.log` и `ping_stdout.log`. Raw CSV/log files присутствуют во всех
трёх руках.

## 2. Validity gates

| Arm | Wall / steady | Active / subscriptions | Reconnect / unrecovered | Resource abort | Статус |
|---|---:|---:|---:|---:|---|
| `A` | 3600.057 / 3000.057 s | 2 / 2 | OKX 0, Bybit 0 / 0 | 0 | `valid` |
| `B` | 3600.258 / 3000.258 s | 600 / 600 | OKX 0, Bybit 0 / 0 | 0 | `valid` |
| `C` | 3600.285 / 3000.285 s | 600 / 700 | OKX 6, Bybit 94 / 0 | 0 | `measurement_failed` |

`C` восстановила все connections, но это не отменяет заранее объявленный
failure gate: больше одного unplanned reconnect на любой бирже делает
измерение невалидным. У Bybit ping в `C` также один `connection_error`;
поэтому её S/P — описательная метрика, не evidence для matrix.

## 3. Exact pooled raw metrics после warmup

Анализ выполнен:

```bash
python3 validation/ws_fanout_three_arm_analyze.py \
  --root output/wsfanout_abc_20260812r2/raw_vps \
  --output-dir output/wsfanout_abc_20260812r2/analysis \
  --warmup-sec 600
```

Все значения ниже — pooled raw samples, ms; `S/P` — отношение p99 delivery к
matched XRP ping p99.

| Arm / leg | S n | S p50 / p95 / p99 / max | P n | P p50 / p95 / p99 / max | S/P p99 |
|---|---:|---:|---:|---:|---:|
| `A` OKX | 18,425 | 30 / 34 / 43 / 237 | 18,425 | 29 / 33 / 43 / 256 | 1.000× |
| `A` Bybit | 26,518 | 17 / 21 / 30 / 392 | 26,518 | 18 / 23 / 32 / 242 | 0.938× |
| `B` OKX | 19,307 | 30 / 36 / 46 / 555 | 19,307 | 29 / 33 / 43 / 241 | 1.070× |
| `B` Bybit | 30,034 | 20 / 24 / 35 / 395 | 30,034 | 20 / 24 / 34 / 365 | 1.029× |
| `C` OKX | 19,304 | 29 / 33 / 38 / 345 | 19,304 | 31 / 33 / 38 / 345 | 1.000× |
| `C` Bybit | 29,403 | 17 / 19 / 25 / 246 | 29,380 | 17 / 19 / 24 / 261 | 1.042× |

| Arm | Loop n | Loop p50 / p95 / p99 / max, ms | `>200 / >500 / >1000 ms` | Dual `>500 / >1000 ms` |
|---|---:|---:|---:|---:|
| `A` | 15,001 | 0.782928 / 1.508603 / 2.094236 / 28.564563 | 0 / 0 / 0 | 0 / 0 из 50 |
| `B` | 15,001 | 0.587198 / 1.116500 / 1.430641 / 45.150245 | 0 / 0 / 0 | 0 / 0 из 50 |
| `C` | 15,001 | 0.645933 / 1.134460 / 1.309610 / 32.541071 | 0 / 0 / 0 | 0 / 0 из 50 |

## 4. Resource gates в steady-окне

Лимиты manifest: load-1 `≤8`, available memory `≥4096 MiB`, RSS `≤2048 MiB`,
FD `≤4000`, CPU `≤95%` одного logical CPU. Все arms соблюли resource budgets.

| Arm | CPU p95 / max | min available memory | RSS p99 / max | FD max | load-1 max |
|---|---:|---:|---:|---:|---:|
| `A` | 2.00 / 3.00% | 14,094 MiB | 27.88 / 27.88 MiB | 12 | 1.132 |
| `B` | 23.94 / 58.84% | 14,069 MiB | 106.94 / 106.94 MiB | 610 | 1.317 |
| `C` | 24.95 / 38.86% | 14,071 MiB | 114.28 / 114.28 MiB | 610 | 1.397 |

## 5. Ограничение вывода и следующий шаг

`A` и `B` тихие по raw delivery, S/P, dual и loop-lag, но `C` не прошла
reconnect gate. Нельзя трактовать тихий `B` как доказательство, что
connection/FD factor отсутствует, и нельзя сравнивать `B` с невалидной `C`
для parse/calc. Даже полностью валидная A/B/C серия не доказала бы
production-safety `N=300`, поскольку bars, publisher, parquet и spool были
выключены, а co-resident background не был controlled.

Минимальный следующий сценарий: повторить только `C` с тем же immutable
profile и reconnect gate; после valid A/B/C выполнить отдельный randomised
shadow experiment background off/on. Затем для любого production-like профиля
нужны два независимых 60-min окна с bars/persistence по
[контракту](latency-production-acceptance-contract.md).

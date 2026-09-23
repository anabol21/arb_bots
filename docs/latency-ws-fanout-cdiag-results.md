# Результаты Cdiag — `wsfanout_cdiag_20260812r1`

## Вердикт

**`valid` для заранее объявленного reconnect-контракта.** За полный 60-min
run нет ни одного `connection_error`, `unplanned_close` или
`unplanned_reconnect` на OKX, Bybit либо отдельном XRP ping. Максимальная
волна равна `0/10 s` и `0/60 s`; последний snapshot до штатного shutdown
фиксирует `600/600` active и `0` pending recovery. Поэтому этот C repeat
устраняет конкретный failure r2 (`6` OKX + `94` Bybit reconnect), но сам по
себе не делает A/B/C причинный verdict окончательным.

Systemd-unit имеет `Result=exit-code`, `ExecMainStatus=1`, хотя probe и ping
завершились `0` и `safety_abort` отсутствует. Это **ошибка финального
инвертированного shell-check** в runner (`! python ... any(safety_abort)`):
при нулевом числе safety abort он возвращает `1`. Это не runtime/reconnect
failure данного измерения, но делает systemd completion сигналом непригодным
для будущих runs, пока runner не будет исправлен и повторно проверен.

## 1. Provenance, завершение и полнота raw

| Поле | Наблюдение |
|---|---|
| VPS / run | NEW VPS `root@38.180.94.108`, `wsfanout_cdiag_20260812r1` |
| Фактическое окно probe | `2026-08-12T14:36:17.228Z` → `15:36:17.397Z`, `3600.169 s` |
| Плановое окно | `14:36:17Z` → `15:36:16Z`; штатный `stop_requested=duration_elapsed` |
| Supervisor | `probe_status=0`, `ping_status=0`, end `15:36:17Z` |
| Топология / профиль | C-only, `300` pairs, `600` sockets, full decode/quote/spread, без parquet/publisher/spool/bars |
| Параметры Cdiag | batch `30 pairs / 3 s`, retry `10 s`, omitted `max_queue`, `websockets 16.1` |
| Подписки | `600/600`; последняя initial subscription в `27.605 s`, batch `9` |
| Raw local copy | `output/wsfanout_cdiag_20260812r1/raw_vps/`; SHA-256 8 существенных artifacts совпали с VPS |
| Raw inventory | `4,854` runtime JSONL, `72,846` ping JSONL, delivery: OKX `28,808`, Bybit `44,031`, loop `18,000` rows (без заголовков) |

Последний pre-shutdown `metrics_1s` (`15:36:16.610Z`) показывает
`active_connections=600`, `pending_recoveries=0`, по `300` opens/subscriptions
на биржу. Поле `active_connections=0` у события `finished` ожидаемо после
cancel штатного shutdown и не является unrecovered connection.

## 2. Reconnect-contract и диагностические поля

| Проверка | OKX | Bybit | Вердикт |
|---|---:|---:|---|
| `unplanned_reconnect` (лимит `≤1`) | 0 | 0 | pass |
| `connection_error` / `unplanned_close` | 0 / 0 | 0 / 0 | pass |
| max wave 10 s / 60 s (60 s лимит `≤3`) | 0 / 0 | 0 / 0 | pass |
| unrecovered в pre-shutdown snapshot | \- | `0` total | pass |
| matched XRP ping `connection_error` | 0 | 0 | pass |

Следовательно, распределения close code/reason/class, connection age и
recovery latency **пусты по причине отсутствия событий**: не было ни clean,
ни abrupt close, ни exception, ни повторного socket sequence. Это
положительный результат наблюдаемости, но не доказательство того, что
endpoint/path никогда не завершает соединения.

## 3. Steady raw latency, loop и ресурсы

Warmup — первые `600 s`; ниже exact pooled raw samples, а не median minute
percentiles. `S` — XRP delivery из C, `P` — same-window matched XRP ping.

| Leg | S n | S p50 / p95 / p99 / max, ms | P n | P p50 / p95 / p99 / max, ms | S/P p99 |
|---|---:|---:|---:|---:|---:|
| OKX | 23,825 | 30 / 41 / 60 / 444 | 23,825 | 29 / 34 / 46 / 341 | 1.304× |
| Bybit | 36,134 | 17 / 24 / 36 / 247 | 36,132 | 17 / 23 / 33 / 258 | 1.091× |

| Метрика steady | Значение |
|---|---:|
| Loop lag n; p50 / p95 / p99 / max | 15,001; 0.414 / 2.007 / 5.020 / 37.623 ms |
| Loop `>200 / >500 / >1000 ms` | 0 / 0 / 0 |
| Dual delivery `>500 / >1000 ms` | 0 / 0 из 50 minute windows |
| Message rate OKX / Bybit | 781.2 / 924.8 frames/s; 322.7 / 177.6 kB/s |
| CPU p50 / p95 / max | 25.96 / 35.94 / 58.84% одного core |
| RSS p50 / p95 / max | 107.7 / 108.0 / 108.0 MiB |
| FD p50 / p95 / max | 610 / 610 / 610 |
| Host load-1 p50 / p95 / max | 1.071 / 1.604 / 1.924 |
| Available memory minimum | 14.06 GiB |

Все resource budgets выполнены; `safety_abort=0`. Это подтверждает headroom
именно для standalone Cdiag на данном VPS, не production collector и не
durable storage.

## 4. Сравнение с r2 C

| Свойство | r2 C | Cdiag | Интерпретация |
|---|---:|---:|---|
| Reconnect OKX / Bybit | 6 / 94 | 0 / 0 | Результат reliability существенно лучше; только Cdiag проходит gate |
| Max wave 60 s | минимум 58 Bybit в соседние минуты | 0 | Cdiag не воспроизвёл path/endpoint incident r2 |
| Retry | 3 s | 10 s | Не мог предотвратить первый drop, но уменьшил бы плотность retry после него |
| Subscription ramp | 1 pair / 50 ms | 30 pairs / 3 s | Изменилась форма initial load; Cdiag закончил initial subscribe за 27.6 s вместо ~15 s r2 |
| Receive queue | explicit `max_queue=None` | library default (`websockets 16.1`) | Изменена backpressure семантика; это правдоподобный фактор, но не изолированный |
| C raw delivery p99 OKX / Bybit | 38 / 25 ms | 60 / 36 ms | Новые p99 выше, но окна разнесены по времени; нельзя называть это регрессией параметров |
| Loop p99 / max | 1.310 / 32.541 ms | 5.020 / 37.623 ms | Нет budget breach; временно-разнесённое сравнение не атрибутирует разницу |

Управляемо изменились сразу три параметра, а окно и внешний endpoint/path
другие. Поэтому корректная формулировка: новая комбинация **совместима с**
устранением r2 reconnect symptom и заслуживает дальнейшей проверки; она не
доказывает, какой именно параметр устранил failure, и не исключает
невоспроизведённый transient exchange/network incident.

## 5. Влияние на H1 и границы вывода

H1 о том, что C-r2 drops были неизбежным следствием full parse/calc при
`N=300`, **ослаблена**: Cdiag сохраняет full in-memory parse/calc и не имеет
reconnect при нормальных CPU/loop/FD. Но H1 не опровергнута причинно:
изменены batching, retry и queue semantics, а повтор не был simultaneous
randomized control.

Это делает C arm пригодной как отдельное валидное C observation, но **не
делает A/B/C causal verdict valid**. B r2 — time-separated baseline из
предыдущего окна с иной subscription/queue конфигурацией; к тому же A/B/C
не были randomised/co-timed и не контролировали co-resident background.
Persistence/bars/publisher/spool остаются выключены.

## 6. Reproducibility и следующий пробел

Локальные неизменяемые raw и вычисленный dashboard:

- `output/wsfanout_cdiag_20260812r1/raw_vps/`
- `output/wsfanout_cdiag_20260812r1/analysis/analysis.json`

Следующий минимальный шаг — сначала исправить и локально проверить inversion
в финальном service-status check, затем выполнить **новую controlled,
randomised/co-timed B↔C серию** с единым batching, retry и `max_queue`
профилем; менять ровно один исследуемый фактор. Только после valid
reconnect-gate в обеих руках сравнивать parse/calc causally. Для
production-like safety по-прежнему требуются отдельные окна с
bars/persistence по acceptance contract.

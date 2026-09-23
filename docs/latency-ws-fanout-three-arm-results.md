# Результаты A/B/C fan-out: `wsfanout_abc_20260811r1`

## Вердикт

**Тяжёлый XRP-хвост в этом единственном окне не воспроизведён ни в `B`, ни в
`C`: dual `>500` / `>1000` = `0 / 0 из 50` steady-минут во всех руках.**
Однако строгий выбор между connection/FD- и processing-dominant по матрице
дизайна **недопустим**: shadow delivery latency сохранена только как
поминутные сводки, поэтому точные arm-wide `S p50/p95/p99` не восстановить; у
`C` также были `229` reconnect/error events. Итоговая классификация:
**`inconclusive / measurement-limited`, H1a/H1b не выбрана.**

Направленное наблюдение, не причинный вывод: при 600 book-соединениях полная
обработка `C` не дала heavy-tail signature в этом окне, даже с заметным
reconnect churn. Это не переносится на production `N≈337`: там одновременно
есть bars, persistence и соседний production runtime, которых в probe нет.
Track (B) остаётся закрытым; policy по `N` и production patch не предлагаются.
Будущие production-gates, требуемые evidence и порядок следующих опытов
зафиксированы отдельно в [контракте приёмки](latency-production-acceptance-contract.md).

## 1. Блок, среда и артефакты

- **Трек / блок:** `(D) collection reliability`; WS frame → `asyncio` loop →
  raw discard либо in-memory parse/quote/spread. Persistence, bars, spool,
  publisher, parquet и mounted durable output выключены.
- **VPS:** NEW `root@38.180.94.108`; production `spread-collector` PID `24505`
  не останавливался и не перезапускался.
- **Первичная материализация / durable target:** только локальные structured
  logs experiment root; durable market-data target отсутствует по дизайну.
- **Источники:** `/data/experiments/wsfanout_abc_20260811r1/`; неизменяемая
  локальная копия — `output/wsfanout_abc_20260811r1/raw_vps/`.
- **Воспроизводимый расчёт:** `validation/ws_fanout_three_arm_analyze.py`;
  результаты — `output/wsfanout_abc_20260811r1/analysis/{analysis.json,summary.csv}`.

Supervisor завершился штатно: `B → C → A`, соответственно `17:40:50Z →
18:40:51Z`, `18:40:51Z → 19:40:51Z`, `19:40:51Z → 20:40:52Z`;
`supervisor_finished` записан в `20:40:52Z`. Все probe/ping exit status равны
нулю.

## 2. Валидность рук и окна

Warmup — предписанные 10 минут. Steady-интервал каждой руки — приблизительно
50.0 минут. `P` — raw matched ping: OKX `latency_ms`, Bybit `age_ts_ms`.
`S` — trigger-leg `delivery_latency_ms`.

| Рука | Фактическое probe-окно UTC | Steady | Connections / subscribe sends | Ping steady samples OKX / Bybit | Lifecycle и качество | Статус |
|---|---|---:|---:|---:|---|---|
| `B` | `17:40:50.811` → `18:40:51.133` | 3000.3 с | 600/600; 605 | 22 990 / 30 511 | 3 592 `metrics_1s`, 50 minute buckets, `safety_abort=0`, clean shutdown | `valid_with_aggregation_limit` |
| `C` | `18:40:51.454` → `19:40:51.613` | 3000.2 с | 600/600; 829 | 21 027 / 27 253 | 3 592 / 50, `safety_abort=0`, clean shutdown; 229 reconnect/error events | `degraded_with_aggregation_limit` |
| `A` | `19:40:52.009` → `20:40:52.072` | 3000.1 с | 2/2; 3 | 23 047 / 33 042 | 3 594 / 50, `safety_abort=0`, clean shutdown; 1 reconnect/error event | `valid_with_aggregation_limit` |

Во всех руках ping запущен одновременно с probe, завершился спустя около
3600 с, имеет нулевые ping `connection_error`, и остаётся непустым после
warmup. `active_connections` достигал ожидаемых 600/600 в `B/C` и 2/2 в `A`.
`C` не объявлен `measurement_failure`: connections были восстановлены и
coverage полна, но rate limit для reconnect не был заранее зафиксирован.
Поэтому его не используют для строгого causal verdict.

## 3. Количественный dashboard S/P

### Что можно посчитать точно

`P` сохранён как raw samples, поэтому `n`, p50/p95/p99/max ниже точны.
`S` не сохранён по sample: runtime оставил только `n`, p50/p95/p99/max на
каждую минуту. В строках S указаны **медиана 50 поминутных p99** и максимум
поминутного p99, а не arm-wide p99; `S max` — максимум минутных максимумов.
Именно это ограничение блокирует буквальное применение порогов матрицы.

| Рука | Серия | n | p50 | p95 | p99 | max | Отношение |
|---|---|---:|---:|---:|---:|---:|---:|
| `A` | S OKX: `median(P99min)` / `max(P99min)` | 23 093 | — | — | 38 / 187 | 387 | 0.52× median-min / P p99 |
|  | P OKX | 23 047 | 30 | 36 | 73 | 388 | — |
|  | S Bybit: `median(P99min)` / `max(P99min)` | 33 180 | — | — | 29 / 197 | 385 | 0.53× median-min / P p99 |
|  | P Bybit | 33 042 | 18 | 23 | 55 | 1 010 | — |
| `B` | S OKX: `median(P99min)` / `max(P99min)` | 22 952 | — | — | 43 / 119 | 267 | 0.93× median-min / P p99 |
|  | P OKX | 22 990 | 30 | 35 | 46 | 240 | — |
|  | S Bybit: `median(P99min)` / `max(P99min)` | 30 452 | — | — | 30 / 230 | 266 | 0.94× median-min / P p99 |
|  | P Bybit | 30 511 | 17 | 21 | 32 | 1 810 | — |
| `C` | S OKX: `median(P99min)` / `max(P99min)` | 21 011 | — | — | 43 / 138 | 446 | 1.02× median-min / P p99 |
|  | P OKX | 21 027 | 29 | 33 | 42 | 430 | — |
|  | S Bybit: `median(P99min)` / `max(P99min)` | 27 220 | — | — | 26 / 49 | 247 | 0.93× median-min / P p99 |
|  | P Bybit | 27 253 | 17 | 20 | 28 | 251 | — |

Все latency S имеют `negative_n=0`. S/P ratio здесь диагностический только
для median-minute p99; его нельзя выдавать за design-defined arm-wide
`S/P p99`.

| Рука | Dual `>500` | Dual `>1000` | Знаменатель |
|---|---:|---:|---:|
| `A` | 0 | 0 | 50 |
| `B` | 0 | 0 | 50 |
| `C` | 0 | 0 | 50 |

## 4. Нагрузка, loop и ошибки (steady)

CPU — средний процессорный расход одной logical CPU по разнице CPU time.
RSS/FD — p50/p95/p99/max по 1-секундным snapshots. Lag — только поминутная
сводка: `median(P99min)` и maximum minute max, не pooled global percentile.

| Рука | CPU | RSS p50 / p95 / max MiB | FD p50 / p95 / max | Loop `median(P99min)` / max ms | Lag >200 / >500 / >1000 |
|---|---:|---:|---:|---:|---:|
| `A` | 1.00% | 27.6 / 27.9 / 27.9 | 9 / 9 / 9 | 2.18 / 18.89 | 0 / 0 / 0 |
| `B` | 18.56% | 107.0 / 108.4 / 108.4 | 607 / 607 / 607 | 1.39 / 263.05 | 1 / 0 / 0 |
| `C` | 21.49% | 121.3 / 145.5 / 145.5 | 607 / 607 / 607 | 1.32 / 27.19 | 0 / 0 / 0 |

| Рука | Exchange | Frames/s | MiB/s | non-XRP frames | Raw discard | `json.loads` | Reconnect/errors | Protocol errors |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `B` | OKX | 598.85 | 0.236 | 1 773 121 | 1 773 116 | 22 984 | 5 | 0 |
| `B` | Bybit | 621.32 | 0.114 | 1 833 021 | 1 833 021 | 30 479 | 0 | 0 |
| `C` | OKX | 584.40 | 0.230 | 1 731 677 | — | 1 752 698 | 209 | 0 |
| `C` | Bybit | 640.24 | 0.117 | 1 892 914 | — | 1 920 153 | 20 | 0 |
| `A` | OKX | 7.67 | 0.003 | 0 | — | 23 009 | 1 | 0 |
| `A` | Bybit | 11.01 | 0.002 | 0 | — | 33 027 | 0 | 0 |

`B` подтверждает intended discard semantics: почти все non-XRP frames
учтены как raw discards; 5 OKX control frames возникли при reconnect. Его
`json.loads` почти точно равен XRP frames, а не миллионам non-XRP frames.
`C` ожидаемо выполняет decode каждого frame, но его 229 reconnects —
отдельный confounder, а не признак latency tail.

## 5. Сопоставление с матрицей решения

| Паттерн дизайна | Наблюдение r1 | Решение |
|---|---|---|
| `A≈B`, `C` heavy | `C` не показывает dual tail, но имеет reconnect churn; точный S p99 отсутствует | Не подтверждено |
| `A<B≈C` heavy | `B/C` не имеют dual tail; exact S p99 отсутствует | Не подтверждено |
| `A<B<C` graded | Нет устойчивой градации minute-summary S p99 или dual; `C` p99min не хуже `B` | Не подтверждено |
| Все руки тихие при valid coverage | Directionally да: 0/50 dual во всех руках | Только **не воспроизведено в этом окне**, не опровержение H1 |
| Invalid/missing | Не хватает raw S/lag для прописанных aggregate quantiles; `C` degraded | **Strict matrix verdict: inconclusive / measurement-limited** |

## 6. Факты отдельно от вывода

**Факты**

1. Supervisor и все шесть arm processes завершились с exit `0`; safety abort,
   FD exhaustion, OOM и protocol errors не зафиксированы.
2. `B/C` достигли 600 active connections и имеют 50 минут с samples обеих
   XRP-ног и matched ping.
3. В `B` 3.61 млн non-XRP data frames были drained/discarded без domain
   handling; `C` обработала ≈3.62 млн non-XRP frames полностью.
4. Ни одна рука не имела dual minute `>500` или `>1000` мс.
5. `C` перенесла 229 reconnect/error events, преимущественно OKX; exact
   budget для допуска не был зафиксирован до запуска.

**Вывод с ограниченной силой**

Этот один sequential randomized run не наблюдает heavy-tail signature от
600 connections в raw-discard `B` и не наблюдает её от full handling `C`.
Но это недостаточно для выбора H1a/H1b или для causal statement: отсутствуют
raw S samples для arm-wide p99, `C` degraded, а дизайн требует двух
согласованных randomized runs для сильного verdict.

## 7. Риски, не-утверждения и будущая N-policy

- Это **не** доказательство безопасного N=300/600, отсутствия FD/OS/server
  rate-limit эффекта или достаточности текущего process topology.
- Это **не** сравнимый количественно replacement prior production `N≈337`:
  probe не выполняет bars/persistence/publisher/spool и не сосуществует с
  полным production data path. Сравнение допустимо только качественно.
- Ping нормализует XRP network floor, но не контролирует exchange message rate,
  routing, market regime и server-side treatment of 600 connections.
- Max без устойчивого p99/dual pattern не является heavy-tail evidence;
  одиночный ping max 1 810 мс в `B` Bybit не меняет p99 и dual result.
- Policy N, Track (B), production ingest/parsing/spread/trading и persistence
  не менялись и не открываются этим отчётом.

## 8. Один следующий эксперимент

**Повторить один A/B/C run в другой time-class после узкого measurement
hardening:** сохранять raw XRP delivery samples и raw loop-lag samples (или
mergeable fixed histograms) и заранее задать допустимый reconnect/drop budget.
Оставить `N=300`, same manifest, 60/10-minute window, fresh matched ping и
полную isolation. Только два valid randomized repetitions с согласованным
pattern могут быть входом в будущую N-policy review.

# Эксперименты: корневая причина latency скринера vs ping

**Трек:** collection / storage reliability (наблюдаемость / root-cause), не стратегия.  
**Статус:** design-only. Прод-коллектор не стопать. Фиксы не внедрять до подтверждения гипотезы.  
**Базовый результат:** [`latency-screener-vs-ping-20260805-result.md`](latency-screener-vs-ping-20260805-result.md) — вердикт **B**.  
**Код:** `app/screaner_b_o.py` (`main()` task graph), `app/storage/writer.py` (`ParquetPublisher`).

Дата дизайна: 2026-08-06.

---

## 1. Pipeline block и task graph

```text
                    ┌─────────────────────────────────────────┐
                    │  asyncio event loop (один процесс)      │
                    │                                         │
  OKX books5 ×N ──►│  okx_listener ×N                        │
  Bybit ob.1 ×N ──►│  bybit_listener ×N                       │
  OKX candle5m×N ─►│  okx_candle5m_listener ×N  (если bars)   │
  Bybit kline ×N ─►│  bybit_kline5m_listener ×N (опц.)        │
                    │  heartbeat @30s                         │
                    │  mount_failure_monitor                  │
                    │                                         │
                    │  на каждое book-сообщение (sync на loop):│
                    │    local_recv = now()                   │
                    │    json.loads → quotes → calc → buffer  │
                    │    if len(buf)>=PERSIST_EVERY_N:        │
                    │      list(buf) + enqueue put_nowait     │
                    └───────────────┬─────────────────────────┘
                                    │ raw batches
                    ┌───────────────▼─────────────────────────┐
                    │  tick-publisher Thread                  │
                    │  normalize → DataFrame → parquet write  │
                    │  (+ bars-publisher Thread, если bars)   │
                    │  SpoolRecoveryWorker Thread(s)          │
                    └─────────────────────────────────────────┘
```

Ключевые константы (env-переопределяемые):

| Параметр | Default | Роль |
|----------|---------|------|
| `SPREAD_ROW_END` | 337 | число пар → ~674 book-WS task |
| `SUBSCRIBE_BATCH_SIZE` / pause | 30 / 3s | шторм подписок при старте |
| `SPREAD_PERSIST_EVERY` | 100000 | flush ticks с loop |
| `SPREAD_COLLECT_BARS` | off/on | +N candle listeners + 2-й publisher |
| `PUBLISHER_MAX_QUEUE` | 4 | backpressure на enqueue |

Семантика latency (канон, не менять):

```text
delivery_latency_ms = local_recv_ts_ms - exchange_ts   # штамп в начале callback
freshness_ms        = calc_local_ts_ms - trigger_recv  # в matched-ране ≈ 0
```

`local_recv` берётся **когда loop дошёл до сообщения**. Задержка планирования loop раздувает latency, но не freshness.

---

## 2. Что доказано (B) и что НЕ доказано

### Доказано (matched XRP, 6h, 2026-08-05)

| Факт | Доказательство |
|------|----------------|
| Прод-скринер имеет **тяжёлый хвост** vs ping на том же символе | S p99 ~1.2–1.4s vs P p99 ~37–46 ms |
| Floor/p50 близки | OKX p50 40 vs 29; Bybit 18 vs 17 |
| Dual-spike обеих ног скринера при **тихом** ping | 42 мин с dual>1s, во всех ping тих |
| Паразит **до calc** | freshness p95≈0 при пиках latency |
| Не чистый Bybit exchange-path | ping Bybit тих в dual-минуты |
| Не clock/cts mismatch | негативных latency нет; сравнение на `ts`/`age_ts` |

### НЕ доказано

1. Что именно из двух пользовательских веток доминирует: **(H1) N подписок** vs **(H2) async-работа beyond WS**.
2. Доля вклада parquet/normalize/GIL vs чистого scheduling loop.
3. Что flush/`persist_every` *вызывает* спайки (vs просто коррелирует по времени).
4. Что bars-коллектор необходим/достаточен для хвоста.
5. Что prod write path дропает худшие события (survivor bias) — хвост в parquet может быть **занижен**.
6. Переносимость на BTC / другие монеты (эксперимент был XRP-only).
7. Что «починить N» или «вынести IO» достаточно для production SLO — только класс фикса после подтверждения.

---

## 3. Каталог гипотез

Пользовательские якоря: **H1** = много WS-подписок; **H2** = много async-работы beyond WS. Остальные — из кода.

### H1 — Fan-out подписок (N book WebSockets на одном loop)

| | |
|--|--|
| **Claim** | Хвост latency растёт с числом одновременных book-listeners; при N≈1 скринер ≈ ping. |
| **Mechanism** | ~674 coroutine делят один event loop. Сообщение ждёт в WS-буфере, пока loop крутит чужие `json.loads`/calc → поздний `local_recv` → dual-spike на «тихих» биржах. |
| **Predicted signature** | Dual-spike обеих ног; корреляция с N; freshness≈0; ping тих; хуже на активных минутах рынка (больше msg/s). |
| **Falsifier** | Screener N=1 (XRP+XRP) имеет p95/p99 ≈ ping; **или** полный N без book-обработки (только connect) уже даёт хвост без calc — тогда не «обработка N», а FD/OS. |
| **Minimal experiment** | E2: отдельный процесс `SPREAD_ROW_START/END` на 1 пару XRP, отдельный `parquet_root`/log; рядом ping; 1–2h. Прод не трогать. |

### H2 — Async-работа beyond book WS (bars, heartbeat, monitor, subscribe batching)

| | |
|--|--|
| **Claim** | Даже при фиксированном N book-WS хвост создаёт *доп.* task graph: candle listeners, bars persist, startup subscribe storms, heartbeat I/O. |
| **Mechanism** | `main()` при `collect_bars` добавляет +N (или +2N) listeners и второй publisher; subscribe batch sleep/create_task волны; heartbeat читает metrics + sync logging. |
| **Predicted signature** | N=337 books **без bars** заметно лучше, чем N=337 **с bars**; dual-spikes совпадают с bar persist / subscribe-фаза только на старте. |
| **Falsifier** | Выключение bars + стабильный steady-state (после subscribe) не меняет p95/p99 vs текущего prod; heartbeat-off не меняет хвост. |
| **Minimal experiment** | E4: shadow-процесс N=полный, `SPREAD_COLLECT_BARS=0`, сравнить с prod (bars on) за то же wall-clock окно; или A/B 2h каждый. |

### H3 — GIL / CPU-насыщение (json.loads × msg/s + publisher normalize/DataFrame)

| | |
|--|--|
| **Claim** | Даже «правильный» thread-publisher конкурирует за GIL с loop; CPU bound раздувает scheduling delay. |
| **Mechanism** | Hot path: sync `json.loads` + calc на loop. Publisher thread: `normalize_*` → pandas/pyarrow. GIL → loop не получает CPU → поздний `local_recv`. |
| **Predicted signature** | Loop-lag probe совпадает с CPU% process; dual-spike при высоком `msg/s` и/или `published_jobs`; publisher `write_latency_ms` пики **предшествуют или совпадают** с dual; отключение persist (null publisher / огромный PERSIST) **частично** снижает хвост, но не до ping, если json.loads×N остаётся. |
| **Falsifier** | CPU низкий (<50% одного ядра) во время dual-spikes; или lag есть при N=337 и полностью отключённом publisher/normalize. |
| **Minimal experiment** | E3 + E5: N=полный, persist disabled (или `/dev/null` path / in-memory drop после enqueue stub); параллельно `pidstat`/`py-spy` top; loop-lag histogram. |

### H4 — Backpressure / рост in-memory buffer

| | |
|--|--|
| **Claim** | Когда publisher queue полна (`max_queue=4`), buffer растёт; копирование/`ready_for_enqueue` на loop удлиняет stalls. |
| **Mechanism** | `persist_opportunities`: retain-in-buffer при full queue; `list(buffer)` на 100k rows; heartbeat показывает `buffer_size`, `queue_depth`, `backpressure_hit`. |
| **Predicted signature** | Dual-spike минуты совпадают с ростом `buffer_size` / `backpressure_hit` / `queue_depth>0`; latency хвост хуже после mount latency spike. |
| **Falsifier** | Dual-spikes при стабильно пустом buffer и `queue_depth=0`. |
| **Minimal experiment** | E0 (read-only): склеить dual-spike минуты из lean с heartbeat snapshot'ами runtime.log за то же окно. |

### H5 — Периодический flush / `PERSIST_EVERY_N` на loop

| | |
|--|--|
| **Claim** | Сам акт `list(100k dicts) + enqueue` на loop создаёт регулярные stalls ~каждые N событий. |
| **Mechanism** | `write_spread_record` → при `len>=PERSIST_EVERY_N` вызывается sync persist с loop thread. |
| **Predicted signature** | Периодичность спайков ≈ время набора 100k ticks (~universe-wide); корреляция 1:1 с логами `enqueued`/`published`; уменьшение `PERSIST_EVERY` учащает мелкие спайки; увеличение/`inf` убирает периодический паттерн. |
| **Falsifier** | Спайки равномерны/случайны без привязки к enqueue; при `PERSIST_EVERY` → очень большое значение периодичность не исчезает. |
| **Minimal experiment** | E5: shadow N=полный, `SPREAD_PERSIST_EVERY=10**9` (почти без flush) vs default; сравнить dual-rate. |

### H6 — Bars WS + второй publisher как отдельный усилитель H2/H3

| | |
|--|--|
| **Claim** | Bars path (лишние WS + `bars-publisher` normalize/write) — доминирующий *добавочный* фактор поверх book N. |
| **Mechanism** | См. H2; плюс конкуренция двух publisher threads за GIL/disk. |
| **Predicted signature** | Выключение только Bybit bars / только OKX candles снижает dual-rate пропорционально; disk util пики совпадают с bar writes. |
| **Falsifier** | Bars off не меняет хвост book-latency (тогда bars не root cause для tick latency). |
| **Minimal experiment** | Часть E4; при необходимости дробить: books+okx bars / books only. |

### H7 — Subscribe batching storms (старт / reconnect)

| | |
|--|--|
| **Claim** | Хвост в основном на старте/reconnect: batch create_task + subscribe flood, не steady-state. |
| **Mechanism** | `chunked(pairs, 30)` + 3s pause; при reconnect одного listener — локальный шторм меньше. |
| **Predicted signature** | Dual-spikes кластер в первые 15–30 мин после start; steady-state p99 близок к ping. |
| **Falsifier** | Dual-spikes равномерно по всем 6h matched-рана (уже похоже на текущий результат → H7 слаба как *единственная* причина). |
| **Minimal experiment** | E0: heatmap dual-spike по минутам уже есть; если равномерно — H7 downrank. Переподписка mid-run как controlled stress — только на shadow. |

### H8 — Logging / heartbeat I/O

| | |
|--|--|
| **Claim** | Sync `FileHandler` logging с loop (каждый subscribe info, errors, heartbeat) блокирует loop. |
| **Mechanism** | `runtime_logger` → file+console; на 337×2 subscribe строки + 30s heartbeat. |
| **Predicted signature** | Спайки каждые ~30s; или диск log fill; отключение file handler на shadow снимает периодичность. |
| **Falsifier** | Dual-spikes не кратны 30s; shadow без file log сохраняет хвост. |
| **Minimal experiment** | Низкий приоритет после E0 (проверить 30s periodicity). Shadow: `NullHandler` / log level WARNING. |

### H9 — OS scheduling / FD count / softirq

| | |
|--|--|
| **Claim** | Лимит FD, context-switch, NIC softirq — вне Python loop. |
| **Mechanism** | ~1000 TCP FD; `ulimit -n`; run-queue latency. |
| **Predicted signature** | Высокий `nr_open`/FD; `schedstat` latency; ping в *том же* процессе-хосте тоже страдает — но ping был тих → **ослабляет** чистый NIC/host-wide claim; остаётся per-process FD/cgroup. |
| **Falsifier** | FD далеко от лимита; N=1 в том же cgroup ≈ ping; host loadavg низкий в dual-минуты. |
| **Minimal experiment** | E0: `ls /proc/<pid>/fd | wc`, `ulimit`, loadavg на dual-минутах; сравнить с ping pid. |

### H10 — Clock (маловероятно)

| | |
|--|--|
| **Claim** | NTP step раздувает latency. |
| **Mechanism** | Скачок `time.time()`. |
| **Predicted signature** | Отрицательные latency; синхронный сдвиг S и P; freshness аномалии. |
| **Falsifier** | Уже: neg=0, ping тих при dual S, freshness≈0 → **H10 отвергнута** как основная. Держать как sentinel в инструментации. |
| **Minimal experiment** | Не нужен; мониторить `neg_latency_frac` в любом новом прогоне. |

### H11 — Survivor bias write path

| | |
|--|--|
| **Claim** | При перегрузе худшие события не попадают в parquet → наблюдаемый хвост *недооценён*; root cause ещё злее. |
| **Mechanism** | backpressure retain; mount_dead reject; quarantine. |
| **Predicted signature** | Рост `backpressure_hit` / gaps в `event_local_ts` / падение row-rate в dual-минуты. |
| **Falsifier** | row-rate и accepted стабильны в dual-минуты; buffer не растёт. |
| **Minimal experiment** | E0 + loop-lag: если lag probe видит 2s stall, а parquet max latency << lag — bias подтверждён. |

---

## 4. Дерево гипотез (сжато)

```text
Вердикт B: local stall до calc (freshness≈0), ping тих
│
├─ H1 N book subscriptions ─────────── prior ★★★★★
├─ H3 GIL/CPU (loads + publisher) ──── prior ★★★★☆
├─ H2 async beyond WS (bars/tasks) ─── prior ★★★★☆
├─ H5 persist_every on loop ────────── prior ★★★☆☆
├─ H4 queue backpressure/buffer ────── prior ★★★☆☆
├─ H6 bars publisher amplifier ─────── prior ★★★☆☆ (подмножество H2/H3)
├─ H9 OS/FD ────────────────────────── prior ★★☆☆☆
├─ H8 logging/heartbeat ────────────── prior ★★☆☆☆
├─ H7 subscribe storms only ────────── prior ★☆☆☆☆ (6h равномерность против)
├─ H11 survivor bias ───────────────── prior ★★☆☆☆ (искажает величину)
└─ H10 clock ───────────────────────── prior ✗ отвергнута
```

---

## 5. Лестница экспериментов (дешёвое → дорогое)

**Инвариант:** прод-коллектор (`systemd` / текущий pid) **не останавливать**. Shadow-процессы только в отдельный `parquet_root`, `runtime_log`, universe slice. Не удалять `/data/live`.

| # | Эксперимент | Длительность | Стоимость | Что разделяет | Риск для prod |
|---|-------------|--------------|-----------|---------------|---------------|
| **E0** | Read-only: dual-spike минуты × heartbeat (`buffer_size`, `queue_depth`, `published_*`) × loadavg/FD — дашборд: [`latency-e0-dashboard.md`](latency-e0-dashboard.md) | 2–4h анализа | нулевая | H4, H5?, H7, H8, H9, H11 | нет |
| **E1** | Уже сделано: ping matched XRP | 6h | низкая | baseline сети | нет |
| **I1** | Минимальная инструментация loop-lag + dual marker (см. §6) на **shadow** сначала; на prod — только если 10–20 строк и opt-in env | 0.5–1d | низкая | все H* измерения | низкий, если флаг off по умолчанию |
| **E2** | Shadow screener **N=1 XRP**, persist on, bars off; vs ping 1–2h | 1–2h | низкая | **H1 vs rest** | CPU/сеть умеренно |
| **E3** | Shadow **N=полный**, persist **off** (или drop после calc, без publisher start) | 1–2h | средняя | H3/H5 vs чистый WS+calc | CPU как prod |
| **E4** | Shadow N=полный, persist on, **bars off** vs prod bars on (или shadow bars on в другой root) | 2h+2h | средняя | **H2/H6** | CPU×2 если параллельно prod |
| **E5** | Shadow N=полный, bars off, `PERSIST_EVERY` extreme large vs default | 2×1–2h | средняя | **H5** | CPU×2 |
| **E6** | Shadow N∈{1,10,50,150,337} dose-response, bars off, persist on | 5×1h | дороже | количественный **H1** | CPU |
| **E7** | Только если E2≈ping и E3 всё ещё плох: py-spy / cprofile publisher vs loop | 1–2h | дороже | H3 детали | низкий на shadow |
| **E8** | Production fix canary — **после** выбора класса фикса | отдельно | высокая | validation | отдельно согласовать |

### Рекомендуемый порядок на 1–2 дня (не ломая накопление)

```text
День 0 вечер:  E0 (логи уже есть) → уточнить prior H4/H5/H7/H8
День 1 утро:   I1 на shadow (loop-lag) + E2 (N=1)
               если E2 ≈ ping → H1↑; если E2 всё ещё плох → искать процессный оверхед/баг
День 1 день:   E3 (N=full, no persist) и/или E4 (no bars)
День 1–2:      E5 или E6 по тому, что осталось неразделённым
День 2:        зафиксировать победившую H* → класс фикса (§7); canary не начинать без этого
```

Параллельность: E2 можно гонять одновременно с prod (мало ресурсов). E3/E4/E6 — осторожно с CPU; лучше последовательно или ночью.

---

## 6. Минимальная инструментация (design; внедрять opt-in)

Цель: измерять **loop lag** отдельно от exchange age. Не менять ingest/parse/spread formula.

### 6.1 Event-loop lag probe

```text
каждые 100–200 ms: scheduled callback
lag_ms = (now_monotonic - expected) * 1000
лог/метрика: p50/p95/p99/max за минуту; spike event если lag_ms > 200/500/1000
```

Реализация-идея: `loop.call_later` / asyncio task с `await sleep` и monotonic. Env: `SPREAD_LOOP_LAG_PROBE=1`. Default **off**.

### 6.2 Hot-path timestamps (только shadow или sampled)

На 1 из K сообщений или на XRP only:

| Метка | Где |
|-------|-----|
| `t_local_recv` | уже есть |
| `t_after_json` | после `json.loads` |
| `t_after_calc` | после `calc_and_store_spread` / `calc_local` уже есть |
| `t_persist_begin/end` | вокруг `persist_opportunities` когда срабатывает |

Агрегировать в runtime log structured: `hotpath_sample | coin=XRP | json_ms=… | calc_ms=…`.

### 6.3 Dual-spike marker

В heartbeat или отдельном task раз в 1s:

```text
если для probe-coin (XRP): okx_delivery>T и bybit_delivery>T
  → log dual_spike_marker | lag_ms=… | buffer_size=… | queue_depth=… | published_rows=…
```

Связывает parquet dual-минуты с live loop state без остановки коллектора.

### 6.4 Что не инструментировать сейчас

- Смену схемы parquet (новые колонки в lean) — только если shadow; в prod schema freeze.
- Изменение `local_recv` позиции (сломает сравнимость с ping).

---

## 7. Success criteria → класс production-fix

| Если подтверждено | Класс фикса (ещё не делать) | Не делать вместо этого |
|-------------------|----------------------------|-------------------------|
| **H1** (dose-response N) | Шардирование коллекторов (N/k процессов); или уменьшение universe; или multiplex меньше сокетов на exchange API если доступно | «Оптимизация» spread formula |
| **H2/H6** (bars) | Вынести bars в отдельный процесс/loop; или отключить Bybit bars; отдельный приоритет tick vs bar | Резать tick-пары без нужды |
| **H3** (GIL/CPU) | Вынести json/calc batching; `orjson`; уменьшить работу на msg; publisher уже в thread — проверить, что normalize не душит; возможен process-level shard | Крутить `PERSIST_EVERY` как единственный фикс |
| **H5** (persist on loop) | Перенести snapshot/enqueue в `call_soon_threadsafe` осторожно / уменьшать copy; flush из фоновой task; не блокировать listener | Отключить persist в prod |
| **H4** (backpressure) | Ускорить writer/mount; увеличить очередь только с учётом RAM; алерт на buffer age | Молча дропать события |
| **H8** | Async/non-blocking logging; снизить subscribe info spam | Игнорировать как «косметику» если 30s pattern есть |
| **H9** | ulimit/FD budget; cgroup CPU; NIC interrupts | Шардирование «на всякий» без FD evidence |
| **H11** | Считать runtime lag source of truth; чинить stall, не «хвост parquet» | Доверять только p99 из parquet |

Смешанный исход нормален (например H1+H3): шарды снижают N *и* msg/s на процесс.

---

## 8. Чего НЕ заключать из каждого эксперимента

| Эксперимент | Запрещённый вывод |
|-------------|-------------------|
| E0 корреляция flush↔spike | «flush причинa» без E5 (корреляция ≠ causation) |
| E1 ping тих | «скрипнер всегда виноват на любой монете» — только matched символ |
| E2 N=1 ≈ ping | «prod можно оставить как есть» — наоборот, подтверждает tax от N |
| E2 N=1 всё ещё плох | «подписки ни при чём» — возможен баг shadow-конфига / общий host factor; проверить FD/CPU |
| E3 no-persist лучше | «можно не писать на диск» как prod-решение |
| E4 bars off лучше | «bars бесполезны» — только что они бьют по tick latency |
| E6 линейный dose | экстраполяция за N=337 без проверки; нелинейность от GIL возможна |
| Любой shadow success | «prod исправлен» без canary на боевом unit |
| Parquet p99 alone | игнор H11: отсутствие строк ≠ отсутствие stalls |

---

## 9. Операционные правила

1. Прод-коллектор не stop/restart ради этих опытов (кроме явно согласованного окна).
2. Shadow: отдельные `SPREAD_PARQUET_ROOT` / runtime log / row range; не писать в `/data/live` prod.
3. Не truncate runtime.log; не delete backlog.
4. Frozen: websocket subscribe semantics, parse, spread math — не «упрощать» ради latency test.
5. Каждый прогон: hostname, NTP/chronyc brief, `ROW_START/END`, bars flags, `PERSIST_EVERY`, pid, loadavg start/end.
6. Вердикт по гипотезе — только с фальсификатором из таблицы; иначе «неразделено».

---

## 10. Краткий вердикт дизайна

Разделить **H1 (N подписок)** и **H2 (async beyond WS)** нельзя одним ping-сравнением — нужен ladder E0→E2→E3/E4.  
Наибольший prior: **H1**, затем **H3/H2**.  
Первый информативный прогон после E0: **E2 shadow N=1 XRP** при живом ping — если хвост исчезает, пользовательская ветка (1) подтверждена как необходимый фактор; дальше E4/E5 уточняют, нужен ли ещё (2).

Для отдельного разделения connection-count и обработки N потоков см. [`latency-ws-fanout-three-arm-design.md`](latency-ws-fanout-three-arm-design.md); это design-only, без открытия Track (B).

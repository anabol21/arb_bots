# Результат: screener latency vs ping_dual (matched XRP)

**Дата анализа:** 2026-08-06 (локально)  
**Окно эксперимента:** 2026-08-05 `13:00:27Z` → `19:00:27Z` (ping finished)  
**Трек:** collection / storage reliability (наблюдаемость задержки)  
**Дизайн:** [`latency-screener-vs-ping-experiment.md`](latency-screener-vs-ping-experiment.md)  
**Notebook:** [`research/latency_screener_vs_ping.ipynb`](../research/latency_screener_vs_ping.ipynb)

---

## 1. Pipeline block

| Слой | Путь |
|------|------|
| Ping (VPS) | `/var/log/spread/ping_dual_6h_xrp.log` → локально `output/ping_dual_6h_xrp.log` |
| Screener lean | stamps из compacted backup `backup1tb:spread-compacted` (5-min windows 13:00–19:10 UTC), фильтр `base_coin=XRP` → `output/latency_exp_xrp_live/` |
| Метрика | trigger-leg: `local_recv − exchange_ts`; Bybit ping = `age_ts_ms` |
| Коллектор | не останавливали |

**Важно про источник lean:** active `/data/live/base_coin=XRP/event_date=2026-08-05/` на VPS пуст (в `archived/` только хвост ~23:15Z+). Для overlap с ping взяты уже compacted+backed-up окна; схема lean stamps сохранена.

Ping meta: `okx_inst=XRP-USDT-SWAP`, `bybit_symbol=XRPUSDT`, samples OKX=146997 / Bybit=256801, `event=finished`.

---

## 2. Ключевые квантили (ms)

Overlap: `2026-08-05 13:00:27.674Z` → `19:00:27.335Z`.

| Серия | n | p01 | p50 | p95 | p99 | max |
|-------|--:|----:|----:|----:|----:|----:|
| S_okx (XRP trigger) | 146996 | 36 | 40 | 121 | 1258 | 3485 |
| P_okx ping | 146997 | 27 | 29 | 32 | 37 | 218 |
| S_bybit (XRP trigger) | 256500 | 14 | 18 | 195 | 1350 | 3479 |
| P_bybit age_ts | 256801 | 15 | 17 | 23 | 46 | 208 |
| S freshness (okx / bybit) | — | — | 0 / 0 | 0 / 0 | 1 / 1 | 15 / 11 |

Минутные Δ (S−P):

| Нога | median Δp50 | median Δp95 | доля минут Δp95>50 | >200 |
|------|------------:|------------:|-------------------:|-----:|
| OKX | +10 | +36 | 38% | 20% |
| Bybit | 0 | +24 | 37% | 25% |

Dual-spike (оба `*_latency_ms` на одной строке XRP):

| Порог | минут dual | из них ping обеих ног < порога |
|------:|-----------:|-------------------------------:|
| 500 ms | 171 / 361 | **171** |
| 1000 ms | 42 / 361 | **42** |

Отрицательных latency: **0**.

---

## 3. Вердикт: **B** (с мягким floor-смещением на OKX)

| Исход | Вердикт | Почему |
|-------|---------|--------|
| **A** | нет | p95/p99 S ≫ P на обеих ногах |
| **B** | **да (основной)** | floor/p50 близки; хвост S тяжелее; dual-spike >1s в 42 минутах при полностью тихом ping |
| **C** | нет / слабо | median Δp50 OKX всего +10 ms, Bybit 0 — не систематические десятки–сотни на p50 |
| **D** | нет | Bybit ping p99=46 ms — биржевые Bybit-пики **не** объясняют screener dual 1000+ |
| **E** | нет | нет отрицательных latency; freshness≈0 при пиках S → паразит **до** calc (loop/scheduling), не clock/cts mismatch |

Интерпретация: matched-XRP снимает symbol-confounder. Тихий ping при screener dual-spikes указывает на **локальный stall** event-loop / I/O / flush / scheduling под полной universe-нагрузкой, а не на «биржевую» задержку одной ноги.

---

## 4. Артефакты

- `output/ping_dual_6h_xrp.log`
- `output/latency_exp_xrp_live/base_coin=XRP/event_date=2026-08-05/batch_000000000.parquet` (+ flat `xrp_lean_20260805_1300_1900.parquet`)
- `output/latency_screener_vs_ping_summary.csv`
- `output/latency_screener_vs_ping_minute_delta.csv`
- `output/latency_screener_vs_ping_dual_spike.csv`
- `output/latency_screener_vs_ping_verdict.csv`
- `output/latency_screener_vs_ping_timeseries_p95.png`
- `output/latency_screener_vs_ping_scatter_p95.png`
- `output/latency_screener_vs_ping_ecdf.png`
- `output/latency_screener_vs_ping_dual_heatstrip.png`
- executed: `research/latency_screener_vs_ping.executed.ipynb`

---

## 5. Остаточные confounds

1. **Нагрузка процесса:** screener ≈ сотни WS на одном asyncio loop; ping = 2 thread × 1 символ. Сравнение показывает паразит *скринера как системы*, не «стоимость одной XRP-подписки».
2. **Источник parquet:** lean для окна взят из compacted backup, не из live batches (live Aug-5 XRP уже ушёл в compaction). Stamps lean сохранены; survivor bias при дропе на write path не исключён полностью.
3. **Clock source:** screener `time.time()*1000` vs ping `time.time_ns()//1e6` — обычно эквивалентно; негативов нет.
4. **Non-trigger latency** на dual-spike строках — снимки последних доставок обеих ног; для сравнения с ping использовался только trigger-leg.
5. Авто-флаг E в notebook heuristic срабатывает на «свежесть мала + хвост S тяжёлый» — это как раз паттерн **B**, не clock; в этом отчёте E отвергнут вручную.

---

## 6. Рекомендуемый следующий шаг

Не стопать коллектор. Если нужна локализация B: коррелировать dual-spike минуты с runtime flush/write/backlog метриками и loadavg за то же окно; опционально короткий matched BTC ping для проверки переносимости.

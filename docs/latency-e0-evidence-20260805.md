# E0 evidence — dual-spike × heartbeat (2026-08-05)

**Трек:** (D) latency gate · Validation Agent  
**Окно:** `2026-08-05T13:00:27Z` → `19:00:27Z` (matched XRP)  
**Дашборд:** [`latency-e0-dashboard.md`](latency-e0-dashboard.md)  
**Вердикт:** **H4 ALIVE** (механический OR-порог дашборда; `buffer_size>0` baseline всего окна — не дискриминатор dual); классический backpressure-сигнал **не** виден.

---

## 1. Pipeline block

| Слой | Путь |
|------|------|
| Dual-минуты | lean stamps `output/latency_exp_xrp_live/xrp_lean_20260805_1300_1900.parquet` |
| Quiet ping | `output/ping_dual_6h_xrp.log` |
| Heartbeat / backpressure | VPS `/var/log/spread/runtime.log.5.gz` (ротация `2026-08-06 00:00`) → локальная копия `output/e0_logs/runtime.log.5.gz` |
| Склейка | nearest `heartbeat` к mid-minute ±45 с; `backpressure_hit` в той же календарной минуте |
| Коллектор | не останавливали; только чтение |

Формат времени в `runtime.log`: `YY-MM-DD HH:MM:SS` (не ISO `2026-08-05T…`).

---

## 2. Files / logs used

| Артефакт | Назначение |
|----------|------------|
| `output/latency_exp_xrp_live/xrp_lean_20260805_1300_1900.parquet` | обе ноги latency на строке |
| `output/ping_dual_6h_xrp.log` | quiet-ping проверка |
| `output/e0_logs/runtime.log.5.gz` | heartbeat за окно |
| `output/e0_logs/e0_dual_minutes_1000.csv` | список 42 минут |
| `output/e0_logs/e0_heartbeat_overlap.txt` | 722 heartbeat-строк (13:00–19:01 UTC) |
| `output/e0_logs/e0_dual_heartbeat_join.csv` | таблица склейки |

`output/latency_screener_vs_ping_dual_spike.csv` — только сводка (порог→count); список минут восстановлен заново.

---

## 3. Table summary + key counts

### Dual >1s (rebuild)

| Метрика | Значение |
|---------|----------|
| Минут в overlap | 361 |
| Dual >1000 мс (обе ноги на строке) | **42** |
| Из них ping обеих ног <1000 мс | **42 / 42** |

### Heartbeat overlap

| Метрика | Значение |
|---------|----------|
| `heartbeat` в окне | **722** (~каждые 30 с) |
| `backpressure_hit` в окне | **0** |
| `queue_depth>0` (все heartbeat) | **0 / 722** |
| `buffer_size=0` (все heartbeat) | **0 / 722** |

### Join dual × nearest heartbeat (±45 s)

| Метрика | Значение |
|---------|----------|
| Joinable минут | **42 / 42** (`hb_delta_s` median 9 с, max 15 с к mid-minute; все ≪ ±45 с) |
| Quiet publish path (`buffer_size=0` ∧ `queue_depth=0` ∧ ¬`backpressure_hit`) | **0 / 42 (0%)** |
| Loaded publish path (`buffer_size>0` ∨ `queue_depth>0` ∨ `backpressure_hit`) | **42 / 42 (100%)** |
| `queue_depth>0` на dual | 0 |
| `backpressure_hit` на dual | 0 |
| `buffer_size` на dual | median **45007**, mean **44730**, min **19**, max **96353** |
| `last_write_latency_ms` на dual | median **8.8**, p95 ~**19**, max **89.5** |

Контекст (не критерий вердикта): на **всех** 361 минутах с heartbeat `buffer_size>0`; dual vs non-dual median buffer **45007 vs 51404** (dual не выше).

---

## 4. Verdict on H4

| Критерий дашборда | Факт | Исход |
|-------------------|------|-------|
| H4 REJECTED: ≥70% quiet; n≥30 | quiet **0%**, n=42 | нет |
| H4 ALIVE: ≥70% loaded | loaded **100%** | **да** |
| Measurement FAILED: n_join&lt;30 или нет heartbeat | n_join=42, heartbeat есть | нет |

**Вердикт: H4 ALIVE** — falsifier «dual при пустом publish path» не выполнен.

Уточнение к сигнатуре H4 из каталога: `queue_depth>0` и `backpressure_hit` за окно **отсутствуют**; «loaded» здесь = ненулевой in-memory `buffer_size` (накопление до `PERSIST_EVERY`), не заполненная publisher-очередь.

---

## 5. Explicit non-claims

- Не утверждаем, что flush / `list(buffer)` / write **вызывают** dual-spike (только совпадение по времени с ненулевым buffer).
- Не утверждаем, что backpressure (full queue) объясняет хвост: `backpressure_hit=0`, `queue_depth=0` всюду.
- Не закрываем `gate #1`; не разделяем H1 vs H3.
- Локальный rebuild dual ≠ повторный VPS-сбор lean; источник lean — прежний compacted backup (как в результате 2026-08-05).

# Результаты E2b: полный 2h теневой скринер N=1 XRP vs ping (OLD VPS)

**Вывод сверху.** Формальный вердикт по §4 [`latency-e2-dashboard.md`](latency-e2-dashboard.md) — **H1 поддержана**. Полное окно ping ≈ **120.0 мин** (`duration_elapsed`), устойчивый участок после отброса первых 5 мин ≈ **115.0 мин** (≥60). На устойчивом участке trigger-leg p99 shadow/ping: OKX **37 / 34 мс (≈1.09×)**, Bybit **22 / 21 мс (≈1.05×)**; минут dual>500 и dual>1000 мс = **0 / 116**. Паттерн совпадает с укороченным abort-E2 и с hostcap-c на новом хосте: при N=1 хвост скринера ≈ ping. **`gate #1` не закрыт** — E2b говорит только про H1 (fan-out N), не про достаточность шардирования / прод-архитектуру.

| Поле | Значение |
|------|----------|
| Трек | (D) сбор / хранение — подзадача задержки |
| Run id | `e2_n1_xrp_20260810b` (`e2b`) |
| Дизайн | [`latency-e2-dashboard.md`](latency-e2-dashboard.md) |
| Канон | [`latency-root-cause-experiments.md`](latency-root-cause-experiments.md) §H1 / E2 |
| Host | OLD VPS `root@38.244.198.42` (не путать с prod `38.180.94.108`) |
| База сравнения | abort E2 [`latency-e2-results-20260810.md`](latency-e2-results-20260810.md); matched prod 2026-08-05; hostcap-c [`latency-host-compare-20260810.md`](latency-host-compare-20260810.md) |
| Анализ | локально, read-only копии; метод = E2 short / host compare |
| Дата отчёта | 2026-08-10 (finalize ~22:37Z) |

---

## 1. Статус прогона (процессы / окно)

| Поле | Факт |
|------|------|
| Плановое окно | `2026-08-10T19:41:36Z` → `21:41:36Z` (2 ч) |
| Фактическое окно ping | `19:41:36.398Z` → `21:41:37.923Z` (**≈120.0 мин**) |
| Причина конца ping | `event=stop_requested reason=duration_elapsed` → `event=finished` |
| Ping PID `1264183` | умер штатно после `finished`; samples OKX `41224` / Bybit `58329` |
| Shadow PID `1264180` | жил до post-window `TERM` Validation @ `22:37:10Z`; `shutdown_flush_done` @ `22:37:11Z` (`published_rows=168291`, `published_files=34`, `failures=0`) |
| Перекрытие S∩P | ≈120.0 мин (≥45 ✓) |
| Устойчивый участок (−5 мин) | ≈115.0 мин (≥60 ✓) |
| Universe | `Loaded pairs: 1`; XRP books5 + `orderbook.1.XRPUSDT` |
| Heartbeat | `pairs=1`, `collect_bars=false`, `schema_mode=lean`; `queue_depth` max=0; `buffer_size` пила до ~4992 при `PERSIST_EVERY=5000` |
| Prod `spread-collector` | **`inactive`** на всём окне E2b (left as-is; не stop/start) |
| Первая материализация | `/data/experiments/e2_n1_xrp_20260810b/live` (VPS local disk) |
| Durable | нет — артефакт эксперимента |

### Пути

| Роль | VPS | Локально |
|------|-----|----------|
| Журнал shadow | `/var/log/spread/e2b_n1_xrp_runtime.log` | `output/e2b_n1_xrp/logs/e2b_n1_xrp_runtime.log` |
| Ping dual | `/var/log/spread/e2b_n1_xrp_ping_dual.log` | `output/e2b_n1_xrp/logs/e2b_n1_xrp_ping_dual.log` |
| Lean parquet | `/data/experiments/.../live/base_coin=XRP/event_date=2026-08-10/*.parquet` (34 файла) | `output/e2b_n1_xrp/live/...` |
| CSV / meta | — | `e2b_n1_xrp_summary.csv`, `e2b_n1_xrp_minute_delta.csv`, `e2b_n1_xrp_dual_spike.csv`, `e2b_n1_xrp_analysis_meta.json` |

Метрика (без смены семантики): `delivery_latency_ms = local_recv_ts_ms − exchange_ts` на ноге-триггере. Для Bybit ping — `age_ts_ms`. Не путать с `freshness_ms`.

Окно анализа = границы ping `start`/`finished`. Строки shadow после `21:41:37Z` (до TERM) **не** входят в S vs P (нет matched ping).

---

## 2. Квантили trigger-leg (мс) — устойчивый участок

Отрицательных задержек: **0**.

| Серия | n | p01 | p50 | p95 | p99 | max | S/P p99 |
|-------|--:|----:|----:|----:|----:|----:|--------:|
| `S_okx` | 39508 | 29 | 31 | 33 | **37** | 54 | **≈1.09×** |
| `P_okx` | 39500 | 29 | 31 | 33 | **34** | 689* | — |
| `S_bybit` | 55798 | 17 | 18 | 21 | **22** | 109 | **≈1.05×** |
| `P_bybit` (`age_ts`) | 56081 | 17 | 18 | 19 | **21** | 59 | — |
| `freshness_ms` OKX / Bybit | — | 0 / 0 | 0 / 0 | 1 / 1 | 1 / 1 | 13 / 7 | — |

\*одиночный выброс на ping OKX; p99 остаётся 34. Якорь §4 («shadow p99 ≲80–100 мс при ping≈40») выполнен с запасом на обеих ногах.

Полное перекрытие (включая первые 5 мин) — те же порядки (OKX p99 36 vs 34; Bybit 22 vs 21) — см. `e2b_n1_xrp_summary.csv`.

### Минутные Δ (S−P), устойчивый участок

| Нога | минут | median Δp50 | median Δp95 | доля минут Δp95>50 | >200 |
|------|------:|------------:|------------:|-------------------:|-----:|
| OKX | 116 | 0 | 0 | **0%** | **0%** |
| Bybit | 116 | 0 | +1 | **0%** | **0%** |

---

## 3. Двойные всплески

| Порог | минут dual (устойчивый) | из них ping обеих ног < порога | доля минут |
|------:|------------------------:|-------------------------------:|-----------:|
| 500 мс | **0** / 116 | 0 | 0% |
| 1000 мс | **0** / 116 | 0 | 0% |

На полном перекрытии (121 минутная корзина): dual>500 и dual>1000 тоже **0**.

Prod matched XRP 6 ч (2026-08-05): dual>1 с = **42 / 361**.

---

## 4. Применение светофора H1 (§4)

| Исход §4 | Порог | Наблюдение | Итог |
|----------|-------|------------|------|
| **H1 поддержана** | p99 S ≤ ~2× P на обеих ногах **и** dual>1 с ≈ 0; ≥60 мин steady | OKX 1.09×, Bybit 1.05×; dual=0; steady **115 мин** | **да — формальный вердикт** |
| H1 ослаблена | dual>1 с при тихом ping **или** p99 S ≳ 500 мс при P≲50 | нет | не применимо |
| Измерение провалено | pairs≠1 / пустой parquet / ping≈0 / overlap <45 мин | pairs=1; parquet+ping полны; overlap 120 мин | нет |

**Частичная поддержка** (p99 200–400 без dual) не нужна: shadow остаётся в зоне десятков мс рядом с ping.

---

## 5. Сравнение с prior runs

| Метрика | Prod matched 2026-08-05 (full N) | E2 abort short (OLD) | **E2b full (OLD)** | hostcap-c (NEW) |
|---------|----------------------------------|----------------------|--------------------|-----------------|
| Статус | complete | `measurement_failed` (signal, ~44 мин) | **`done` / H1↑** | complete (короткий ~20 мин) |
| OKX p99 S vs P | 1258 vs 37 (~34×) | 42 vs 34 (~1.24×) | **37 vs 34 (~1.09×)** | 33 vs 33 (1.00×) |
| Bybit p99 S vs P | 1350 vs 46 (~29×) | 25 vs 25 (1×) | **22 vs 21 (~1.05×)** | 24.9 vs 24 (~1.04×) |
| Dual>1000 | 42 / 361 | 0 / 41 | **0 / 116** | 0 / 16 |
| Steady мин | ~6 ч | ~39 | **~115** | ~15 |
| Host | OLD | OLD | **OLD** | NEW |

**hostcap-c (NEW):** уже сравнён в [`latency-host-compare-20260810.md`](latency-host-compare-20260810.md) — **complete**; H_host как объяснение prod-хвостов при N=1 **ослаблена**. Не смешивать с вердиктом E2b/H1.

**Что изменилось при N=1 на полном 2h:** секундный prod-хвост отсутствует; shadow ≈ ping на OLD. Это формально поддерживает H1 как необходимый фактор хвоста; **не** доказывает, что одного шардирования достаточно для целевого SLA в prod.

---

## 6. Обновление гипотез

| ID | После E2b |
|----|-----------|
| **H1** (fan-out N) | **поддержана** на полном окне (≥60 мин steady, p99≲2×, dual≈0) |
| **H_process** | на N=1 за 115 мин steady **ослаблена** (нет dual>1 с / p99≫ping) |
| Путь записи / H5 | при `PERSIST_EVERY=5000` flush ~каждые несколько минут; `write_latency_ms` в логе десятки мс; dual=0 — **не** объясняет prod-хвост 1 с на этом срезе; **не** утверждаем causation flush→хвост |
| H_host | отдельно ослаблена hostcap-c; не подменяет H1 |
| `gate #1` | **остаётся open** (запрет §4) |

---

## 7. Риски и оговорки (Review Critic self-check)

1. **`gate #1` не закрыт** — E2b только про H1 vs process на N=1; не «prod можно не шардировать» и не live-bot readiness.
2. **`PERSIST_EVERY=5000` ≠ prod `100000`:** влияет на частоту flush (H5), не на число WS. Вердикт H1 обязан это отметить; dual всё равно 0 на полном окне.
3. **Prod `inactive` во время E2b:** валидно для shadow vs ping; не измеряет налог соседнего полного коллектора.
4. **Свечи выкл.** (`collect_bars=false`) — канон E2; вклад candle-listeners не разделён.
5. **Shadow TERM после окна:** ping кончился штатно @ `21:41:37Z`; shadow оставлен жить ~55 мин (лишние rows вне overlap), затем Validation `TERM` → `shutdown_flush_done`. Это штатный post-window stop из дашборда, не abort/`signal` как у short E2.
6. **Не утверждаем** flush causation, правку ingest, достаточность шардирования, переносимость на другие монеты.
7. Одиночный ping OKX max=689 не меняет p99 и не создаёт dual.

---

## 8. Явные не-утверждения

- `gate #1` **не** закрыт.
- Приём WS / parse / spread **не** правились и **не** рекомендованы к правке по E2b.
- Успех N=1 shadow ≠ доказательство поведения prod при полном N.
- H_host / смена машины **не** заменяют вердикт H1.
- Трек (B) не открывался.

---

## 9. Артефакты

```text
output/e2b_n1_xrp/
  logs/e2b_n1_xrp_runtime.log
  logs/e2b_n1_xrp_ping_dual.log
  live/base_coin=XRP/event_date=2026-08-10/*.parquet   # 34 files
  e2b_n1_xrp_summary.csv
  e2b_n1_xrp_minute_delta.csv
  e2b_n1_xrp_dual_spike.csv
  e2b_n1_xrp_analysis_meta.json
  # prior partial snapshots kept as *_PARTIAL.*
```

VPS markers обновлены: `/var/log/spread/E2_LATENCY_OWNED.txt`, `/data/experiments/e2_n1_xrp_20260810b/DO_NOT_TOUCH.md` → **finished**.

---

## 10. Следующий шаг (один)

По лестнице [`latency-root-cause-experiments.md`](latency-root-cause-experiments.md): **E4/E6** (dose-response по N на shadow, bars off, persist on) — количественно подтвердить, что хвост растёт с N; **не** правка приёма. Review Critic: сила H1↑ не закрывает `gate #1`.

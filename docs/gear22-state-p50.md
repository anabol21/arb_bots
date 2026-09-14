# State p50 (gear 2.2 observation)

**Трек:** 2 Historical model. **Гир:** 2.2 (viz / causal features).  
**Статус:** три метрики «текущего спреда» зафиксированы для наблюдения. Не торговый порог, не live, не alpha, не гейт симулятора.

Код: [`research/gear22_quiet_regime_viz/quantiles.py`](../research/gear22_quiet_regime_viz/quantiles.py) (`rolling_tw_p50`, тот же hold→next, что `tw_p50`) и [`state_p50.py`](../research/gear22_quiet_regime_viz/state_p50.py).  
Формула флора **не** меняется: [`gear22-floor-metric.md`](gear22-floor-metric.md).

## Зачем

Нужен устойчивый к шуму трекер текущего спреда. Закрытая UTC-свеча 5m уже даёт TW-p50 (`tw_p50`), но только на close. Rolling 1m / rolling 5m считают то же TW-p50 в скользящем окне, заканчивающемся в `t`, без выравнивания на границу свечи.

Long и short независимы (`spread_long` / `spread_short`).

## Три метрики

Одинаковое TW: тики по `event_local_ts_ms`; вес тика `i` = время до следующего; последний тик в окне держится до **конца окна**; дыра до первого тика в окне **без массы**; линейной интерполяции нет. Длинный межтиковый gap по-прежнему даёт hold-массу предыдущему тику (как у закрытой свечи). Causal: только `ts ≤ t`.

| Имя | Окно | Когда известно | Ось x на графике |
|-----|------|----------------|------------------|
| `p50_bar5m` | закрытая UTC-свеча `[bar_start, bar_end)` = существующий `tw_p50` | на `bar_end` | `bar_end`, step `hv` до следующего close |
| `p50_roll_1m` | `[t − 60s, t]` | в любой `t` | eval-сетка (sample HTML: 1s) |
| `p50_roll_5m` | `[t − 300s, t]` | в любой `t` | та же сетка; **не** UTC-свеча |

`p50_roll_5m` ≠ `p50_bar5m`: trailing window vs выровненный бар; last-hold → `t` vs → `bar_end`.

### Cadence

Формула определена в каждом `t` (в том числе на каждом тике). Sample HTML считает rolling на сетке **1s** (`eval_step_ms=1000`) и рисует эту сетку — это cadence отображения, не подмена 5m-close. Downsample легенды не подменяет формулу.

### NaN

NaN, если в окне **меньше 2** конечных тиков с положительным hold **или** суммарная TW-масса **< 20% длины окна** (12s для 1m, 60s для 5m). Пустое окно / только нулевые веса → NaN. Дыры не заполняются.

## HTML

Новая страница, **не** перезапись `gear22_viz_sept`:

`output/gear22_state_p50/gear22_state_p50_SOL.html`

Открыть через `file://` вместе с соседним `plotly.min.js`. Один график, два ряда (long / short): faint ticks + три p50.

Те же три серии рисуются на 5-минутной tick-странице (`research/tick_window_viz/plot.py`, ноутбук `research/tick_window_5m.ipynb`) поверх сырых `spread_long` / `spread_short`. SMA / флор туда не переносятся. Sample: `output/gear22_state_p50/tick_window_SOL.html`.

Пересборка:

```bash
PYTHONPATH=. python -m research.gear22_quiet_regime_viz.state_p50 \
  --data-root output/lean_ticks \
  --coins SOL \
  --since 2026-08-18T00:00:00Z \
  --until 2026-08-18T03:00:00Z \
  --out-dir output/gear22_state_p50
```

## Стоимость (все данные — оценка, не прогон)

Пробы 2026-09-08, read-only. Backup **не** скачивался. Полная история **не** считалась.

| Источник | Объём | Спан |
|----------|-------|------|
| local `output/lean_ticks` | 6567 parquet | 2026-07-22 → 2026-08-27 |
| VPS `/data/experiments/gear22_ticks_sept` | 1986 файлов, **14G** | 2026-09-01 → 2026-09-07 |
| VPS `/data/compacted/sent` | 144 файла | ~2026-09-07 19:20 → 09-08 07:20 UTC |
| backup `backup1tb:spread-compacted` | **9615** объектов, **105.947 GiB** (`rclone size`) | не копировать на Mac |

Тики (файл 2026-08-18 11:55Z: ровно **400000** строк — похоже на cap; 336 монет): SOL 6333 / 5m ≈ **1.82e6 тиков/день**; все монеты в файле ≈ **1.15e8 строк/день**. viz_sept: 190 HTML.

Замер на этой машине, SOL 3h `2026-08-18 00:00–03:00 UTC`, 149708 тиков, сетка 1s, одна сторона:

| Шаг | Стена |
|-----|-------|
| чтение overlapping parquet | 8.8 s |
| `p50_bar5m` (36 баров) | **0.05 s** |
| `p50_roll_1m` (10800 eval) | 0.44 s |
| `p50_roll_5m` | 2.08 s |
| HTML целиком (long+short+plotly) | 16 s |

| Задача | `p50_bar5m` | rolling 1m+5m (сетка 1s, 2 стороны) |
|--------|-------------|-------------------------------------|
| Алгоритм | 1 TW-p50 на закрытый бар (уже в `build_5m_bucket_stats`) | two-pointer + sort окна; TW-median не O(1) |
| 1 монета / 1 день | ≪ 1 s | **~40 s** compute (8× трёхчасовой замер) + I/O ~288 файлов |
| Все монеты / 1 день | секунды, если бары уже есть | ~336 × 40 s ≈ **3.7 h** sequential Python + ~4 GB parquet/день |
| Весь backup (~33 сут. 5m-слотов) | дешево относительно тиков | порядка **5 CPU-дней** sequential; **считать на VPS**, не копировать 106 GiB |
| Cadence каждый тик (SOL) | — | ~21× тяжелее 1s-сетки |

Дешево сейчас: `p50_bar5m`. Rolling нужен доступ к тикам. Sample HTML = 1 монета × 3 часа. Полный backup — отдельная VPS-работа.

## Что это не закрывает

- Ветки C/D, occupancy, honest holes гира 2.2.
- Гейт симулятора, порог входа, live bot.
- Утверждение, что 1m p50 — лучший признак для обучения.

# Backtest feature table (gear 2.2 observation)

**Трек:** 2 Historical model. **Гир:** 2.2 (наблюдение / вход в бектест).  
**Статус:** схема `gear22_bt_features_v1` зафиксирована. Не гейт симулятора, не live, не alpha, не политика размера.

Код: [`research/gear22_feature_day_probe.py`](../research/gear22_feature_day_probe.py).  
Формулы не меняются: флор — [`gear22-floor-metric.md`](gear22-floor-metric.md); state — [`gear22-state-p50.md`](gear22-state-p50.md) (`p50_roll_1m`, 1 Hz, TW hold→next).

## Зачем

Один проход по тикам даёт wide-таблицу `(coin, ts_s)` для replay кандидатов «theta» и «абсолютный спред». Дыры и NaN сохраняются: отсутствие наблюдения ≠ «нет сигнала».

## Строка и партиции

- Одна строка = `(coin, ts_s)` — UTC-секунда, **wide** (long и short в одной строке).
- Сетка 1 Hz, полуинтервал `[since, until)`.
- Hive (producer / VPS): `output/gear22_backtest_features/coin=<SYM>/event_date=<YYYY-MM-DD>/part-000.parquet`, zstd. Неполный день может лежать рядом как `part-001.parquet` (например VPS `08-27` 12:00–24:00).
- Sibling для viz / мультимонетного скана: `output/gear22_backtest_features_by_date/event_date=<YYYY-MM-DD>/part-000.parquet` — те же строки, сортировка `(ts_s, coin)` в порядке canary. Конвертер: [`research/gear22_repartition_features_by_date.py`](../research/gear22_repartition_features_by_date.py). Как читает галерея: [`research/gear22_backtest_features_by_date.md`](../research/gear22_backtest_features_by_date.md).
- `event_date` — UTC-дата из `ts_s`. `coin` дублируется в теле файла.
- NaN не интерполируется и не заполняется нулём.

## Колонки v1

| column | dtype | meaning |
|---|---|---|
| `ts_s` | int64 | unix seconds UTC |
| `coin` | dictionary/string | символ canary top-30 |
| `p50_1m_long`, `p50_1m_short` | float32 | rolling TW-p50, окно 60s, hold→next |
| `floor_long`, `floor_short` | float32 | locked floor; step с последнего закрытого 5m бара (`bar_end ≤ t`, hv) |
| `theta_1m_long`, `theta_1m_short` | float32 | `p50 − floor`; хранится, не только выводится |
| `gapfrac5m_long`, `gapfrac5m_short` | float32 | `gap_fraction` того же закрытого 5m бара |
| `cov_1m_long`, `cov_1m_short` | float32 | TW-масса окна 60s / 60000 ms (пустое окно = 0) |
| `n_ticks_1m_long`, `n_ticks_1m_short` | uint16 | сырой счётчик тиков в `[t−60s, t]` (clip 65535) |
| `usable_long`, `usable_short` | bool | finite p50 **и** finite floor **и** `cov_1m ≥ 0.20` (тот же min-mass, что у канона p50) |
| `spread_last_long`, `spread_last_short` | float32 | последний тик с `ts ≤ t` (каузальный last) |
| `spread_min_long`, `spread_min_short` | float32 | min сырого спреда в `(t−1s, t]`; NaN если тиков в секунде нет |
| `spread_max_long`, `spread_max_short` | float32 | max сырого спреда в `(t−1s, t]` |
| `dp50_60_long`, `dp50_60_short` | float32 | `p50(t) − p50(t−60s)`; NaN если любой конец NaN |
| `occ60_long`, `occ60_short` | float32 | TW-доля минуты, где сырой спред `> floor + 0.30` |

### `occ60` и константа 0.30

`0.30` — это **процентные пункты** в тех же единицах, что `spread_*` (`× 100`):

```
4 legs × 0.00075 × 100 = 0.30
```

Это документированный порог occupancy для экспериментов «спред над флором + round-trip fee», **не** колонка `fee_rate` и не «spread minus fees». Порог не заморожен в симуляторе: сменить fee-assumption можно, не пересчитывая всю таблицу, если потребитель вычитает иначе; для готового `occ60` константа именно 0.30.

Occupancy = (hold→next масса, где `y > floor(t) + 0.30`) / 60000. Дыра до первого тика в окне без массы (как у p50). NaN, если положительной hold-массы нет **или** `floor` неконечен.

## Флор и warmup

`s_t = SMA_12(close)_t` на UTC 5m барах (`causal_sma` по last-tick close, пустые бары = NaN, без интерполяции), затем `tf-select α25` как в `floors.compute_chosen_floor`. На сетке 1 Hz флор — ступенька с последнего бара с `bar_end ≤ t`.

На каждый выходной день D тики читаются с `D − 13h` (SMA-12 + trim 12h). Список parquet расширяется на ±1 слот 5m: имя файла может отставать от содержимого ~40s (`load.list_compacted_overlapping`).

## Вселенная и окно прогона

Canary top-30: [`_floor_canary_coins_html30.txt`](../_floor_canary_coins_html30.txt) (TRUST→GIGGLE), сверка с [`research/data/universe_spread_std_august.csv`](../research/data/universe_spread_std_august.csv).

Считаемый span (usable August на локальном `output/lean_ticks`): **`2026-08-10T00:00:00Z` → `2026-08-27T12:00:00Z`**. Это покрывает A-блоки `08-10T21→08-14T11`, `08-20T22→08-23T07`, `08-23T17→08-25T08`, `08-25T19→08-27T05` и дни между ними; intervening days не объявляются прошедшими A-критерий — фильтр у потребителя.

Чекпоинт: `2026-08-12T00:00:00Z` → `2026-08-13T00:00:00Z` (warmup флора с `2026-08-11T11:00:00Z`).

## NaN

- p50: &lt; 2 конечных тиков с положительным hold **или** масса &lt; 20% окна.
- floor: warmup / дыры 5m close (канон флора).
- theta / dp50: NaN, если любой операнд NaN.
- `spread_min` / `spread_max`: NaN, если в секунде нет конечных тиков.
- Нуль не подставляется вместо NaN. `cov_1m = 0` у пустого окна — измеренная нулевая масса, не fill.

## Что не входит в v1

- Колонки L1 size / `size_ratio_*` (номинал 100 USDT): **опущены**, чтобы не раздувать slim-read parquet (8 size-полей) в ночном прогоне. Не молчаливый skip — отдельный проход, если понадобятся.
- `fee_rate`, «spread минус комиссии», денормализованный `ctVal` — не замораживать в таблице.
- Это не гейт симулятора и не утверждение прибыльности.

## Манифест

`output/gear22_backtest_features/MANIFEST.json`: coins, time range, `schema_version`, git HEAD (если есть), правило NaN, row counts, какие колонки опущены.

## Dummy policy (pass-1)

Чистая функция `research/gear22_backtest.decide` по строке `FeatureSnapshot`. Open и close **разделены**.

**Сторона (locked):** long-сделка (open long **или** close short) → поля `*_long`. Short-сделка (open short **или** close long) → `*_short`. Закрытие лонга — unwind short-стороны.

**Open:** `usable` + включённые гейты (`theta > theta_open`, `p50_1m > p50_open`, `spread_last >= min_spread_open`). `None` = гейт выкл. Прибыль не смотрит. Оба qualify → long. Fill-прокси: `spread_last` открытой стороны в п.п.; NaN → не открывать. Caller кладёт снимок на `PolicyState`: `position_side`, `held_coin`, `opened_ts_s`, `fill_spread_pp`.

**Close:** dual-leg unwind, канон гира 2 при qty=100:

```
potential_pp(t) = fill_spread_pp + spread_last_opposite(t) - fee_round_trip_pp
```

`fee_round_trip_pp = 0.30` (4×0.00075×100). Close когда включённые close-гейты проходят: `potential_pp >= min_profit_pp` (default 0.0; `None` = выкл.) и опционально `theta_1m_*` **close-стороны** `> min_theta_close` (`None` = выкл.). Opposite NaN / `usable=False` → hold. Same-type overlap: в позиции S, если close прошёл бы и `qualify_open(S)` — hold (`hold_open_overlap`), не close-then-reopen. K=1: в том же вызове другую сторону не открывать. Это 1 Hz `spread_last`, не Trade_Lat 100ms. Replay — отдельный шаг.

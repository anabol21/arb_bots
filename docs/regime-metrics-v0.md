# Метрики режима v0 (гир 1.5, **закрыт**)

Дизайн признаков **более волатильного кластера** по барам объёма `5m` и амплитуде mid из L1. Гир 1.5 **закрыт** как скринер Top‑N / кластера, не как булев гейт `regime_on`. Пороги **экспертные**, без подбора под PnL (поиск — гир 3). Лестница: [`strategy-gears.md`](strategy-gears.md). Источники данных: [`model-data-sources.md`](model-data-sources.md).

Код формул: [`research/regime_metrics.py`](../research/regime_metrics.py).  
Композитный score / скринер: [`research/regime_composite.py`](../research/regime_composite.py) (`z_rank`) и [`research/regime_ma_ratio.py`](../research/regime_ma_ratio.py) (`ma_ratio`); CLI [`research/rank_volatile_coins.py`](../research/rank_volatile_coins.py).  
Интерактив: heatmap `f(coin,t)` + график перехода в top‑1 — [`research/regime_composite_heatmap.ipynb`](../research/regime_composite_heatmap.ipynb).

---

## Назначение

| | |
|--|--|
| Вход | ряд `volume` по монете (`bar_5m`); опционально mid из тиков L1 в тех же окнах `5m` |
| Выход (канон 1.5) | числовой score и Top‑N / кластер более волатильных монет на срезе `t` |
| Выход (опция позже) | `regime_on` (bool) и вспомогательные колонки (`z_vol`, `z_vol_smooth`, …) — **не** критерий закрытия гира 1.5 |
| Использование | пометить более волатильные монеты, чтобы искать возможности позиции в этом кластере (гир 2) |
| Не входит | жёсткий булев гейт как долг 1.5; размер позиции от z (гир 2.5); оптимизация `Z`/`W` под метрику сделки (гир 3); попарно «выше score ⇒ строго волатильнее»; живая торговля |

---

## Канон расчёта volume z-score

Порядок операций (causal, без look-ahead на будущее):

1. На закрытых барах `volume_t` по одной монете:
   - `μ_t = rolling_mean(volume, W)`
   - `σ_t = rolling_std(volume, W)` (нужно ≥ `W_min` точек; иначе NaN → не режим)
2. `z_t = (volume_t − μ_t) / σ_t` при `σ_t > ε`
3. Сглаживание: `z_vol_smooth_t = rolling_mean(z_t, W_s)` (эквивалент «скользящей средней по z-score»; EMA — допустимая замена с тем же смыслом)
4. Гистерезис:
   - включение: `z_vol_smooth ≥ Z_enter` (и при необходимости `vol_persistence ≥ P`)
   - выключение: `z_vol_smooth < Z_exit`

**Запрет v0:** z относительно mean/std по всему датасету вперёд/назад (look-ahead).

### Параметры по умолчанию (ручные)

| Параметр | Default | Смысл |
|----------|---------|--------|
| `W` | `48` | окно фона (~4 ч при барах `5m`) |
| `W_min` | `W` | минимум точек для валидного z |
| `W_s` | `3` | сглаживание z (~15 мин) |
| `ε` | `1e-12` | защита деления |
| `Z_enter` | `2.0` | порог включения |
| `Z_exit` | `1.0` | порог выключения (гистерезис) |
| `K_persist` | `6` | окно persistence (~30 мин) |
| `P_persist` | `0.5` | доля баров с сырым `z ≥ Z_enter` в окне `K` (опциональный фильтр) |
| `Z_amp` | `2.0` | порог z амплитуды mid (если включён AND) |

Дополнительно для санитарии (порог режима на них **не** ставить в v0, только смотреть):

- `vol / median(vol, W)`
- `pct_rank(vol, W)`

---

## Амплитуда mid (`5m` из L1)

Объём ≠ ценовая волатильность.

```text
mid = (bid_price + ask_price) / 2   # опорная биржа: OKX
amp_5m = (mid_high − mid_low) / mid_open   # за то же окно bar_start..bar_end
z_amp — тот же rolling z-score с окном W
```

### Политика `regime_on` в v0 (опция, не закрытие 1.5)

Булев флаг ниже — справочная политика v0 и возможный вход гира 2. **Канон закрытого гира 1.5** — score / Top‑N / кластер, не обязательный `regime_on`.

**Базовый (volume-only):** гистерезис по `z_vol_smooth` (+ опционально persistence).

**Строже (AND):** `(volume regime) AND (z_amp ≥ Z_amp)` — меньше ложных «горячих» меток.

**Шире (OR):** не использовать в v0 по умолчанию (больше шума).

Подтверждение спредом (`spread_*` выше порога) — **не** часть определения режима; это слой «возможность» на уже hot-окне (см. ниже).

---

## Другие метрики (справочник)

### A. Объём

| Метрика | Смысл |
|---------|--------|
| `z_vol` / `z_vol_smooth` | ядро режима |
| `vol / median(vol, W)` | робастный множитель |
| `pct_rank(vol, W)` | «топ X%» |
| `vol_accel` = `z_t − z_{t−k}` | начало всплеска |
| `vol_persistence` | режим vs одиночный шип |
| z по `log1p(vol)` | сравнимость тяжёлых хвостов |

### B. Mid

| Метрика | Смысл |
|---------|--------|
| `amp_5m`, `z_amp` | ценовой стресс |
| дивергенция hot_vol / cold_amp | разные типы режимов |

### C. Подтверждение возможности (только при `regime_on`)

| Метрика | Смысл |
|---------|--------|
| max \|spread\| / time-above-threshold в эпизоде | материал для гира 1 |
| длительность эпизода режима | среднесрочность |

---

## Типы логики (как метрика вмешивается в торговлю)

1. **Ранг / Top‑N / кластер** (канон закрытого гира 1.5): score помечает более волатильные монеты; гир 2 ищет возможности в этом кластере.
2. **Жёсткий гейт входа** (опция позже, не критерий 1.5): `allow_open = regime_on`; модель 1.0 без изменений.
3. **Гистерезис / min duration** — режет пилу на пороге, если булев флаг всё же используют.
4. **Двухключевой замок** — режим ∧ уже расширенный спред.
5. **Раздельные роли** — volume / amp / spread confirm не смешивать в один порог без явной политики.

Отложить: булев `regime_on` как обязательный гейт; размер от z (2.5); подбор Z/W под PnL (3); «вход только на нарастании z».

---

## Санитария

- На спокойном участке доля баров с `regime_on` мала.
- На хвосте — **кластеры** баров, не одиночные точки (иначе снизить шум: ↑`W_s` / persistence).
- Не смешивать look-ahead статистики.
- Multi-coin скринер требует покрытие `bar_5m` по рынку; один `0G` — только калибровка формул.

---

## Исторические бары для калибровки (offline)

Для подбора/санитарии метрик режима (не live collector) — REST-дампы **обеих** бирж с OHLC:

| Биржа | Скрипт | Путь |
|-------|--------|------|
| OKX | [`research/download_okx_bar5m_hist.py`](../research/download_okx_bar5m_hist.py) | `output/okx_bar5m_hist_regime/` |
| Bybit | [`research/download_bybit_bar5m_hist.py`](../research/download_bybit_bar5m_hist.py) | `output/bybit_bar5m_hist_regime/` |

**Окно калибровки (по умолчанию):** UTC `[2026-07-08, 2026-08-08)` — ~1 календарный месяц, заканчивающийся на vacation-окно. Скрипты **идемпотентны**: уже скачанные `event_date=*/part.parquet` пропускаются (флаг `--force` перезаписывает).

Общая схема колонок: `bar_start_ts_ms`, `volume`, `open`/`high`/`low`/`close`, `amp_ohlc=(h−l)/o`, `ret_close`, `ref_exchange`.

**Зачем две биржи:** отличать односторонний всплеск объёма (крупный поток на одной площадке) от **совместного** стресса по монете (объём/амплитуда растут на OKX и Bybit). Амплитуды волатильности цены — из OHLC (`amp_ohlc`, `|ret_close|`), не только из L1 mid.

Это **дополнение** к live `bar_5m` на backup.

Пример докачки:

```bash
python3 research/download_okx_bar5m_hist.py --start 2026-07-08 --end 2026-08-08 --workers 3
python3 research/download_bybit_bar5m_hist.py --start 2026-07-08 --end 2026-08-08 --workers 3
```

---

## Композитный score волатильности (скринер топ‑K)

Один числовой score из **двух** кусков на барах `5m` (hist OHLC+volume). Нужен для ранжирования «самых волатильных» монет в момент `t` (гир 1.5 → очередь капитала гира 2), **не** для подбора порогов под PnL.

### Признаки (own-history, causal)

| # | Признак | Формула / правило |
|---|---------|-------------------|
| 1 | `z_vol` | Rolling z объёма из канона (`W=48`, `W_min=W`, `ε`). |
| 2 | `z_amp` | Тот же rolling z по `amp_ohlc` (или короткому среднему `amp`, default mean bars = 1; fallback `amp_5m`). **То же окно `W`**, что у `z_vol`. |

`ε = 1e-12`. До накопления `W` точек — NaN.

**Не входят в дефолтный composite** (остаются в коде как диагностики / washout):

- `delta_vol = (v_t − v_{t−k}) / v_{t−k}`, `k=6` — функция и колонка есть; в score только с `--include-delta-vol`.
- own-history `pct(z_vol)` / `pct(amp)` с lookback `N=288` — опциональные колонки + `local_score` для washout.

### Скринер: cross-sectional combine

Чтобы сравнивать монеты **в один момент `t`**:

1. На срезе рынка взять две компоненты: `z_vol`, `z_amp`.
2. Лёгкий **winsorize** каждой по срезу в квантили `[0.01, 0.99]`.
3. По каждой — **cross-sectional** percentile rank по монетам в `t` → `r_z_vol`, `r_z_amp` ∈ (0, 1].
4. **`composite = mean(r_z_vol, r_z_amp)`** (равные веса; NaN-компонента → composite NaN; монета не в топе).
5. Ранг скринера = сортировка `composite` по убыванию.

Сопоставимость между монетами даёт **cross-rank** на срезе. Флаг `include_delta_vol` / CLI `--include-delta-vol` добавляет третий ранг winsorized `delta_vol` (эксперимент / legacy).

### Данные и биржи

| Режим | Поведение |
|-------|-----------|
| Default | только **OKX** `output/okx_bar5m_hist_regime/` |
| Опционально | OKX+Bybit: для каждой биржи свой `composite`, затем **`mean`** или **`min`** по монете (`--combine mean\|min`). `min` строже (нужен совместный стресс). |

Время среза по умолчанию — **последний общий** `bar_start_ts_ms` в загруженном warmup-окне дампа; иначе `--ts` (UTC ISO или epoch ms, floor к сетке 5m).

### Параметры по умолчанию

| Параметр | Default |
|----------|---------|
| `W` (`z_vol` / `z_amp`) | `48` (из `RegimeParams`) |
| `amp_mean_bars` | `1` |
| winsor (cross-section) | `0.01` / `0.99` |
| `k` / `N` | только диагностика (`6` / `288`) |
| combine бирж | OKX only; else `mean` |

### Риск washout на длинных режимах

**Было (3-way с `delta_vol`):** в затяжном `regime_on` уровень объёма может оставаться высоким, но **`delta_vol → 0`**, потому что `v_t ≈ v_{t−k}` — вклад прироста в score затухал. Own-history `pct(z_vol)` тоже мог уйти к середине окна.

**Сейчас (2-way `z_vol`+`z_amp`):** score опирается на абсолютные rolling z, поэтому классический washout через `delta_vol` **не входит** в дефолтный composite. Долгий высокий z по-прежнему может «усредниться» в cross-section, если весь рынок горячий — это другой эффект.

Диагностика старого пути: `--washout` в `rank_volatile_coins.py` (decay `local_score` / `delta_vol` на длинных эпизодах). **Гипотеза по спредам** (флаг на позже): в длинных режимах спреды могут сужаться после импульса — нужна отдельная стыковка duration ↔ opportunity.

### Запуск

```bash
./venv/bin/python research/rank_volatile_coins.py --top 10
./venv/bin/python research/rank_volatile_coins.py --top 20 --csv --washout
./venv/bin/python research/rank_volatile_coins.py --exchanges okx,bybit --combine min --top 10
./venv/bin/python research/rank_volatile_coins.py --ts 2026-08-07T12:00:00Z
# legacy 3-way (добавляет rank delta_vol):
./venv/bin/python research/rank_volatile_coins.py --include-delta-vol --top 10
```

Ноутбук (heatmap + Top‑10, один канонический blend-score): [`research/regime_composite_heatmap.ipynb`](../research/regime_composite_heatmap.ipynb) — дефолт `ASSET_CLASS=crypto`, `NUMERATOR=blend`, `BLEND_ALPHA≈0.75`; полный месяц/`MAX_COINS=0` тяжелее. См. § MA-ratio.

---

## MA-ratio composites (канон гира 1.5)

Код: [`research/regime_ma_ratio.py`](../research/regime_ma_ratio.py). Режим скринера: `score_mode="ma_ratio"` (CLI `--score-mode ma_ratio`). Старый путь остаётся `score_mode="z_rank"` (дефолт CLI).

### Идея

Сырые **отношения короткого short-leg к длинной SMA** (causal, без own-history percentile как основной метрики).

**Канон (зафиксирован для heatmap / Top‑N):** soft short blend

```text
r_* = (α · EMA_short + (1 − α) · MA_short) / MA_long
α ≈ 0.75   # BLEND_ALPHA / --blend-alpha

r_vol = short_blend(volume) / MA_long(volume)
r_atr = short_blend(ATR)    / MA_long(ATR)
composite = √(r_vol · r_atr)   # default variant = geom
```

Числитель: `numerator="blend"` (default в `MaRatioParams`). Альтернативы для ablation: `"ema"` (чистый EMA) / `"ma"` (чистый SMA short).  
Знаменатель всегда SMA/`MA_long` (floor `ε` на `|MA_long| > ε`). Пока short-leg держится выше long MA, отношение остаётся > 1 — **плато повышенного объёма/ATR не «смывается»**, в отличие от `delta_vol`.

**ATR:** `TR_t = max(H−L, |H−C_{t−1}|, |L−C_{t−1}|)`, затем `atr_series = SMA(TR, atr_n)` (`atr_n=1` → сырой TR). Fallback при отсутствии OHLC: `amp_ohlc` / `amp_5m`.

### Как читать score

- **Задача метрики** — найти Top‑N / кластер «горячих» монет на срезе `t`, не утверждать попарно, что A «строго волатильнее» B.
- **Интегрально** среднее по панели / heatmap mean — индикатор **рыночной** волатильности в точке времени.
- Дефолт вселенной ноутбука: `ASSET_CLASS="crypto"`. Для equity нужна отдельная доработка (сессии, неоднородность объёма/амплитуды).

### Варианты composite (сырые ratio, не percentile)

| Variant | Формула |
|---------|---------|
| `geom` | `sqrt(r_vol * r_atr)` (равный log-вес) |
| `mean` | `0.5 * (r_vol + r_atr)` |
| `min` | `min(r_vol, r_atr)` — dual confirmation |
| `log_mean` | `0.5 * (log(max(r_vol,ε)) + log(max(r_atr,ε)))` (= `log(geom)` при r>0) |
| `vol_only` / `atr_only` | ablation |

Параметры по умолчанию: `SHORT=6`, `LONG=48` (~30 мин / ~4 ч на барах 5m); в heatmap-ноутбуке часто `LONG=288` (~1 сутки). Второй long — через `--long 288` или `extra_longs=(288,)` / суффикс колонок `_L288`.

### Heatmap / Top‑N

- **Z heatmap** = **raw composite** выбранного варианта (сопоставимо как «кратность собственного фона»).
- Опционально `rank_xs` = cross-sectional percentile raw composite на срезе `t` (для ранжирования).
- Top‑N @ timestamp = сортировка raw composite (тот же канон blend).

### Запуск

```bash
# MA-ratio screener (канон blend)
./venv/bin/python research/rank_volatile_coins.py --score-mode ma_ratio --numerator blend --blend-alpha 0.75 --variant geom --top 10
./venv/bin/python research/rank_volatile_coins.py --score-mode ma_ratio --variant geom --long 288 --limit-coins 40

# ablation: чистый EMA / MA short
./venv/bin/python research/rank_volatile_coins.py --score-mode ma_ratio --numerator ema --variant geom
./venv/bin/python research/rank_volatile_coins.py --score-mode ma_ratio --numerator ma --variant geom

# прежний z-rank (дефолт CLI, без флага)
./venv/bin/python research/rank_volatile_coins.py --score-mode z_rank --top 10
```

В ноутбуке [`research/regime_composite_heatmap.ipynb`](../research/regime_composite_heatmap.ipynb): один CONFIG, `NUMERATOR="blend"`, `BLEND_ALPHA≈0.75`, heatmap + Top‑10.

---

## Версия

- **v0** — формулы и ручные пороги зафиксированы; реализация в `research/regime_metrics.py` + скрипты санитарии/прототипа.
- **v0 + hist** — offline дампы `output/okx_bar5m_hist_regime` и `output/bybit_bar5m_hist_regime` (OHLC + volume) для калибровки на 336 монетах и сравнения бирж; окно по умолчанию `[2026-07-08, 2026-08-08)`.
- **v0 + composite** — default: cross-sectional mean rank of `z_vol` + `z_amp` (равные веса, то же `W`); `delta_vol` / own-history pct — диагностики; CLI топ‑K (`--include-delta-vol` = legacy 3-way).
- **v0 + composite heatmap** — панель `composite(coin,t)`, plotly heatmap и график OHLC вокруг перехода в top‑1: [`research/regime_composite_heatmap.ipynb`](../research/regime_composite_heatmap.ipynb).
- **v0 + ma_ratio** — канон **закрытого** гира 1.5 (скринер кластера, не `regime_on`): causal `(α·EMA+(1−α)·MA)/MA_long` (α≈0.75, `numerator=blend`) по volume и ATR/TR; варианты composite `geom|mean|min|log_mean|vol_only|atr_only`; heatmap / Top‑N по raw composite; CLI `--score-mode ma_ratio --numerator blend --blend-alpha 0.75`. Ablation: `--numerator ema|ma`. `regime_on` остаётся опцией позже.
- Валидация глазом: [`research/regime_anomaly_validation.ipynb`](../research/regime_anomaly_validation.ipynb) — **scrollable** TradingView Lightweight Charts (`lightweight-charts` / `JupyterChart`): pan/zoom, volume, `z_vol_smooth`, заливка `regime_on` (OKX/Bybit). Ядро — `venv` репозитория.

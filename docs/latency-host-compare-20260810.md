# Сравнение latency: новый хост vs старый VPS (N=1 XRP)

**Вердикт сверху.** На N=1 после chrony новый жирный хост **не быстрее** старого по сетевому полу и **не снимает** screener-налог — его и так почти нет: shadow ≈ ping на **обоих** хостах (p99 S/P ≈ 1.0×). Гипотеза **H_host** как объяснение prod-хвостов секундного порядка при N=1 **ослаблена**; для полного N (как в prod) этот прогон **не доказывает** ни «хост спасёт», ни «хост ни при чём». Направленный сигнал по-прежнему ближе к **H1** (fan-out N).

| Поле | Значение |
|------|----------|
| Трек | (D) latency · Validation Agent |
| Дата | 2026-08-10 |
| NEW | `root@38.180.94.108` · run `hostcap_n1_xrp_20260810c` (**complete**) |
| OLD | `root@38.244.198.42` · E2b `e2_n1_xrp_20260810b` (**finished**, H1↑ — [`latency-e2b-results-20260810.md`](latency-e2b-results-20260810.md)) + prior E2 short |
| Метрика | trigger-leg `delivery_latency_ms = local_recv − exchange_ts`; Bybit ping = `age_ts_ms` |
| Канон | [`latency-host-capacity-trial.md`](latency-host-capacity-trial.md) · [`latency-e2-results-20260810.md`](latency-e2-results-20260810.md) |
| Локальные копии | `output/hostcap_c/`, `output/e2b_n1_xrp/`, `output/e2_n1_xrp/` |

---

## 1. Окна данных

| Серия | Хост | Статус | Wall-clock | Устойчивый участок (−5 мин) | Lean rows (steady) |
|-------|------|--------|------------|-----------------------------|--------------------|
| **hostcap-c** | NEW | **complete** (`duration_elapsed` + shadow `shutdown_flush_done`) | `20:48:20Z`→`21:08:21Z` (~20.0 мин) | ~15.0 мин | 13631 (S OKX 5216 / Bybit 8415) |
| **E2b** | OLD | **partial** (не kill; end `21:41:36Z`) | start `19:41:36Z` → copy cutoff `~21:13:33Z` (~92 мин) | ~87 мин | 74995 total lean; steady S OKX 29384 / Bybit 41650 |
| **E2 short** | OLD | complete abort (`signal`) | ~44.4 мин overlap | ~39.4 мин | см. [`latency-e2-results-20260810.md`](latency-e2-results-20260810.md) |

**Dirty / не для абсолютных мс:** hostcap **a/b** (pre-chrony). В таблицах ниже — только **c** для NEW.

Оговорка NEW: рядом крутился full-N prod `spread-collector` (PID `5276`) — coexistence, не «тихий» fat host.

---

## 2. Ping floor: сеть / DC (без screener)

Устойчивый участок, мс.

| Хост / run | Нога | n | p50 | p95 | p99 | max |
|------------|------|--:|----:|----:|----:|----:|
| **NEW hostcap-c** | P_okx | 5206 | **29** | 31 | 33 | 130 |
| **NEW hostcap-c** | P_bybit | 8404 | **20** | 21 | 24 | 46 |
| **OLD E2b partial** | P_okx | 30210 | **31** | 33 | 35 | 689* |
| **OLD E2b partial** | P_bybit | 43337 | **18** | 19 | 22 | 59 |
| OLD E2 short (ref) | P_okx | 15060 | 31 | 33 | 34 | 48 |
| OLD E2 short (ref) | P_bybit | 20119 | 21 | 22 | 25 | 117 |
| NEW hostcap-b pre-chrony (dirty) | P_okx | 582 | 69 | 71 | 73 | 99 |
| NEW hostcap-b pre-chrony (dirty) | P_bybit | 1106 | 61 | 62 | 63 | 122 |

\*одиночный выброс на ping OKX; p99 остаётся 35.

**Вывод по полу.** После chrony NEW ≈ OLD: OKX p50 29 vs 31, Bybit 20 vs 18. Разница порядка **1–2 мс**, не «×2 лучше маршрут». Высокий floor на a/b (~60–70 мс) — в основном **clock confound**, не DC.

---

## 3. Screener N=1: S vs P (steady)

Метрика trigger-leg; устойчивый участок после отброса первых 5 мин.

### 3.1 NEW — hostcap-c (complete)

| Серия | n | p50 | p95 | p99 | max | S/P p99 |
|-------|--:|----:|----:|----:|----:|--------:|
| S_okx | 5216 | 29 | 31 | **33** | 130 | **1.00×** |
| P_okx | 5206 | 29 | 31 | **33** | 130 | — |
| S_bybit | 8415 | 21 | 22 | **24.9** | 44 | **1.04×** |
| P_bybit | 8404 | 20 | 21 | **24** | 46 | — |

Dual>500 / >1000 мс: **0 / 16** минут. Median minute Δ(S−P) p50/p95: OKX 0/0, Bybit +1/+1 мс. Neg latencies: 0. Max OKX 130 одинаков у S и P → сетевой выброс, не screener tax.

### 3.2 OLD — E2b partial (~87 мин steady; run ещё жив)

| Серия | n | p50 | p95 | p99 | max | S/P p99 |
|-------|--:|----:|----:|----:|----:|--------:|
| S_okx | 29384 | 31 | 33 | **37** | 51 | **1.06×** |
| P_okx | 30210 | 31 | 33 | **35** | 689 | — |
| S_bybit | 41650 | 18 | 21 | **22** | 109 | **1.00×** |
| P_bybit | 43337 | 18 | 19 | **22** | 59 | — |

Dual>500 / >1000: **0 / 86** минут.

### 3.3 OLD — E2 short (ref; formal measurement_failed по длине)

| Серия | n | p50 | p95 | p99 | max | S/P p99 |
|-------|--:|----:|----:|----:|----:|--------:|
| S_okx | 15036 | 39 | 41 | **42** | 60 | **1.24×** |
| P_okx | 15060 | 31 | 33 | **34** | 48 | — |
| S_bybit | 20092 | 22 | 23 | **25** | 73 | **1.00×** |
| P_bybit | 20119 | 21 | 22 | **25** | 117 | — |

На E2 short был мягкий сдвиг пола OKX S (+~8 мс); на E2b partial и hostcap-c этого сдвига почти нет (S p50 = P p50).

### 3.4 Сводка old vs new (главная таблица)

| Метрика | NEW hostcap-c | OLD E2b partial | Δ (NEW−OLD) |
|---------|---------------|-----------------|-------------|
| P_okx p50 / p99 | 29 / 33 | 31 / 35 | −2 / −2 |
| P_bybit p50 / p99 | 20 / 24 | 18 / 22 | +2 / +2 |
| S_okx p50 / p99 | 29 / 33 | 31 / 37 | −2 / −4 |
| S_bybit p50 / p99 | 21 / 24.9 | 18 / 22 | +3 / +2.9 |
| S/P p99 OKX | 1.00× | 1.06× | ≈ паритет |
| S/P p99 Bybit | 1.04× | 1.00× | ≈ паритет |
| Dual>1000 мин | 0/16 | 0/86 | оба чисто |

Абсолютные S на NEW не «на порядок лучше» OLD; обе машины на N=1 сидят у ping-пола.

---

## 4. Интерпретация H_host / H1

| Гипотеза | Вердикт по факту | Почему |
|----------|------------------|--------|
| **H_host** (класс машины → prod-хвосты) | **Ослаблена для N=1** | NEW не даёт существенно меньший floor и не меняет картину S≈P; оба хоста «здоровы» при N=1 |
| **H1** (fan-out N) | **Не закрыта**, направленно жива | Контраст с prod full-N (p99 S сотни–тысячи мс при тихом ping) остаётся; N=1 на old и new одинаково близок к ping |
| Clock confound на NEW a/b | **Подтверждён как риск** | Pre-chrony ping ~60–70 → post-chrony ~20/29 |

Фальсификатор trial §5 п.1 (shadow на fat host всё ещё ≫ ping) — **не сработал**: shadow не ≫ ping. П.2 (оба хоста ≈ ping при N=1 → нужен full-N на NEW) — **это текущий статус**.

**Не утверждаем:** что миграция на fat host устранит prod-хвосты; что H_host мертва при полном universe; что `gate #1` закрыт.

---

## 5. Риски и оговорки

1. **Только N=1** — налог мощности / steal / IO при 337 pairs этим прогоном не измерен.
2. **Prod coexistence на NEW** — shadow делил CPU/сеть с full collector; если что, это делает NEW *хуже* идеального fat host, и всё равно S≈P.
3. **Короткое окно hostcap-c** (~15 мин steady) vs E2b (~87 мин partial). Для dual-spike минут мало; для квантилей p50–p99 выборка достаточна (тысячи событий).
4. **E2b incomplete** — числа OLD помечены partial; E2 short — отдельный abort-референс.
5. **Chrony** на NEW offset ≲1 мс на старте/конце c; OLD clock отдельно не аудировали в этом отчёте (исторически E2/E2b стабильны около тех же ping-полов).
6. `PERSIST_EVERY=5000` на shadow ≠ типичный prod `100000` — влияет на write path, не на число WS.

---

## 6. Файлы

| Артефакт | Путь |
|----------|------|
| Этот отчёт | `docs/latency-host-compare-20260810.md` |
| Trial (статус) | `docs/latency-host-capacity-trial.md` |
| NEW logs/parquet/CSV | `output/hostcap_c/` |
| OLD E2b partial | `output/e2b_n1_xrp/` (`*_PARTIAL.*`) |
| OLD E2 short | `output/e2_n1_xrp/` |

---

## 7. Следующий шаг

1. Дождаться конца E2b (`21:41:36Z`) и при желании пересчитать OLD финалом (ожидаемо те же порядки).
2. Для H_host при полной нагрузке: **matched XRP из prod `/data/live` на NEW** vs тот же ping — или full-N shadow на NEW без чужого kill.
3. Не закрывать `gate #1` / H1 только из hostcap-c.

# Гир 2.2 · material latency change-point после onset аномалии

Трек **M**, research only. Канон / `VARIATION` / `HYPER` / `Trade_Lat=100` / fees /
`model_gear2` / `gear2_backtest` **не** менялись. Сетка \(L\) — только
`analysis_lag_ms`.

**Статус:** `unclassified anomaly mixture` (isolated/common_shock в manifest не
заданы). Панель: все 13 монет / 15 эпизодов (≤24 → без стратифицированного
сэмпла).

**Вердикт:**

> `quote-update cadence artifact`

Нет доказанного единого material latency change-point. Не интерпретировать как
latency чужих алгоритмов.

---

## 1. Pipeline block

Track M · gear 2.2 exploratory: после локального onset \(z>4\) ищем ли
воспроизводимый \(\tau_*\), где резко растёт hazard **материального**
схлопывания спреда (не любой quote update).

Единица статистики — эпизод / shock-cluster (равные веса), не тик.

## 2. Files

| Путь | Роль |
|------|------|
| `research/gear22_material_latency_changepoint_lib.py` | протокол |
| `research/gear22_material_latency_changepoint.ipynb` | scratch |
| `research/gear22_material_latency_changepoint.md` | этот отчёт |
| `research/output/gear22_material_latency_changepoint/` | manifest, audit, hazards, kfold, verdict |

Источник: `output/lean_ticks` (read-only). Пол: past-only median 5 мин (≥30),
как в prior gear 2.2. Primary `state_at_L`; secondary `fill_contract_L`.

## 3. Candidates / interpretations

1. **Единый material \(\tau_*\)** после onset positive anomaly.
2. **Плавный decay** / нет одного излома.
3. **Cadence / sampling artifact** (10 ms сетка → редкие L1-состояния).
4. **Fill-contract only** artifact (здесь T\* со state-path).
5. **Stale-leg catch-up**.
6. **Симметричный mean reversion** (negative-tail control).
7. **Heterogeneous / underpowered** эпизоды.

Manifest (UTC, END exclusive; direction выведен как сторона с earliest \(z>4\)):
2Z, ACU, BICO, CAP, ESP, KAITO, KMNO, LA×2, MUBARAK, RVN, TRUST, WAL, ZBT×2.

## 4. Risks

- Нет pre-quiet внутри user intervals → onset часто у левого края окна
  (dwell \(z\le4\) ≥1 s всё же потребован внутри интервала + lookback до старта).
- **2Z**, **WAL** — contaminated: calendar/operator holes (2Z: Aug14
  12:25–12:30 + intra gap; WAL: 34 missing 5m slots + Aug16 operator stop +
  7× ≥6 min silence). Onset/holes замаскированы; дыры не трактовались как
  тихий рынок.
- `episode_class` отсутствует → нельзя primary=`isolated`.
- N=15 эпизодов → K=5 held-out хрупкий (3 test / fold).
- Spearman/PnL не используются; \(C_{\mathrm{fee}}=0.3\%\) только diagnostic
  `T_edge_kill`.

## 5. Experiment / patch

Заморожено до просмотра кривых:

- \(h_{\min}\): min consecutive \(|\Delta s|>0\) в \([t_0-5\mathrm{m},t_0)\), else \(10^{-4}\)
- \(h_0=\max(1\sigma_0,\,0.25|x_0|,\,h_{\min})\); sens \(\kappa\in\{0.5,1,2\}\),
  \(r\in\{0.1,0.25,0.5\}\)
- dwell 1000 ms (sens 500 ms не выбирался по красоте)
- grid 10…300 + 500/1000; highlight 50/100/200
- bootstrap по эпизодам; LOO episode/coin/cluster; quiet ≤5 anchors / onset

### Artifact audit

| Episode | Status |
|---------|--------|
| 2Z_20260814_0200 | contaminated (aug14 slot + intra ≥6m) |
| WAL_20260815_0900 | contaminated (34×5m miss + operator stop) |
| остальные 13 | ok на уровне file/intra audit |

Fail-closed skew/age применяется через `prepare_lean_ticks`.

### Power gate (до интерпретации curves)

- primary \(z>4\) onsets: **15 / 15**
- secondary \(z>2\): 15
- negative \(z<-4\): 15
- quiet controls: 70
- shock-clusters: 15 (нет общей cluster-разметки)

### Resolution audit

- \(T_{\mathrm{any}}\) p50 / p90 ≈ **10 / 57** ms
- доля соседних \(L\) с тем же fill state ≈ **0.84**
- mean unique fill states на сетке ≈ **5.9**
- \(effective-L\) p50 ≈ 20 ms  

→ точность \(\tau_*\) на 10 ms сетке **не** поддерживается реальным cadence.

### Hazards (positive primary, N=15)

- Point \(\tau_*\approx 70\) ms (argmax jump), но discrete \(h^-\) на 50/100/200
  часто **0** (события уже произошли раньше; at-risk пустеет).
- CDF material return: P(T≤50)≈0.60, P(T≤100)≈0.80
- CDF any-move: P(T≤50)≈0.80 — material часто совпадает с первым update
- Quiet \(\tau_*\approx230\); negative \(\tau_*\approx120\) — **не** тот же излом
- `fresh_both` (n=10): \(\tau_*\approx110\) — не только stale
- sens \(h(\kappa,r)\): все \(\tau_*=70\) (устойчиво к порогу, не к validation)

### K=5 / bootstrap / LOO

- held-out CP лучше smooth: **1 / 5** folds
- \(\tau_*\) по folds: 110, 110, 110, 70, 20 (разброс по сетке)
- episode bootstrap \(\tau_*\) mode 70, p025–p975 ≈ 40–110; unique {20,40,70,110}
- LOO episode: почти всегда 70 (2× 110) — point estimate стабилен, но
  **не** проходит held-out success gate

## 6. Historical validation

Покрытие: августовские lean ticks + user windows. Confirmatory на
непросмотренном post-window **не** запускался (отдельное решение).

Success criteria (все сразу) — **не** выполнены:

1. CP held-out ≥4/5 — **fail** (1/5)
2. одинаковый знак jump — **fail**
3. \(\tau_*\) сосредоточен — **fail** (20…110)
4. виден в state path — да (но cadence-limited)
5. `fresh_both` — \(\tau_*\) есть, но не спасает validation
6. LOO не убивает point \(\tau_*\) — да
7. не равен quiet \(\tau_*\) — да (70≠230)
8. устойчивость к \(h(\kappa,r)\) — да
9. continuation без симметричного скачка — условно да (мало at-risk)

## 7. Success criteria → verdict

**`quote-update cadence artifact`**: 10 ms analysis grid в основном попадает в
одно и то же L1-состояние; material collapse часто ≈ первый quote change
(\(T_{\mathrm{any}}\approx T^-_{\mathrm{cum}}\)). One-change-point model не
улучшает held-out log loss в ≥4 folds.

Дополнительно: выборка **underpowered** для стабильного K=5; статус
**unclassified anomaly mixture**.

Не доказано: точная latency конкурирующих arb-алгоритмов; positive-specific
ускоренное схлопывание относительно negative control как единый \(\tau_*\).

## 8. Next step / stop

**Стоп.** Канон и handoff в правила входа не трогать.

Возможные отдельные (только по решению пользователя):

1. разметить `isolated` vs `common_shock` и direction в manifest;
2. onset от **явного pre-quiet** вне user window, если появится разметка;
3. coarsen grid до реального cadence (например 50–100 ms) **до** просмотра;
4. confirmatory на новом непросмотренном покрытии той же панели.

Без (1)+(4) не переводить в entry rules.

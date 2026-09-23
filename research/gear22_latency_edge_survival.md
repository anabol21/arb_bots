# Гир 2.2 · latency edge survival (signal→fill)

Трек **M**, research only. Канон / `VARIATION` / `HYPER` / `Trade_Lat=100` /
`fee_rate` / `model_gear2` / `gear2_backtest` **не** менялись.
\(L\in\{50,100,200\}\) — diagnostic horizons, не retune.

**Вердикт:** `underpowered`

`change-point deferred: underpowered` (gate ≥50 clean episodes не выполнен).
Candidate region 40–110 ms — только annotation, не оценка latency конкурентов.

---

## 1. Pipeline block

Оценка survival экономического края после локального onset \(z>4\) на
manifest аномальных эпизодов: доля gross edge \(R_L\), net edge после
frozen RT costs, \(p_{\mathrm{survive}}(L)\), точные \(T_{25}/T_{50}/T_{\mathrm{edge\,kill}}\).

Единица: один onset на episode; bootstrap по temporal `shock_cluster_id`
(зависимость, не causal claim).

## 2. Files

| Путь | Роль |
|------|------|
| `research/output/gear22_latency_edge_survival/manifest_user.csv` | frozen user manifest |
| `research/gear22_latency_edge_survival_lib.py` | протокол |
| `research/gear22_latency_edge_survival.ipynb` | scratch |
| `research/gear22_latency_edge_survival.md` | этот отчёт |
| `research/output/gear22_latency_edge_survival/` | power, summaries, bootstrap, KM, verdict |

Источник: `output/lean_ticks` read-only. Пол: past-only 5m median (≥30), как prior gear 2.2.
\(C_{\mathrm{fee}}=4\cdot100\cdot0.00075=0.3\) п.п. (diagnostic copy).

## 3. Candidates / interpretations

1. Край в основном surviving при 50/100/200.
2. Край в основном killed.
3. Partial / heterogeneous.
4. Sampling-contract sensitive (`state_at_L` vs `fill_contract_L`).
5. Underpowered / большинство onset экономически невалидны (\(E_{\mathrm{signal}}\le0\)).

Direction (заморожено до outcomes):

\[
t_0=\min\{t:s_d(t)>0\land z_d(t)>4\}
\]

после ≥1 s ниже \(z=4\); tie → больший \(x\); иначе `ambiguous` → exclude.

## 4. Risks

- Manifest: **все** `prequiet_verified=false`, `episode_class` пуст →
  канонический primary (`isolated` ∧ prequiet ∧ ¬contam) = **N=0**.
- Operational panel: ¬contam ∧ runtime prequiet ∧ derived direction
  (exploratory относительно канон-фильтра).
- 2Z, WAL excluded из primary (`contaminated=true`).
- Shock clusters — temporal overlap blocks; эпизоды в одном блоке
  **не** независимые causal shocks.
- N экономических onset мал → bootstrap CI почти \([0,1]\).
- Первый \(z>4\) часто с \(x_0<C_{\mathrm{fee}}\) → статистический выброс ≠
  положительный плановый net edge.

## 5. Experiment / patch

### Power gate

| Panel | N |
|-------|---|
| Manifest rows | 15 |
| Canon isolated+prequiet | **0** |
| Operational clean (runtime prequiet) | 13 |
| Contaminated (audit only) | 2 |
| **Economic primary** \(E_{\mathrm{signal}}>0\) | **3** |
| Clusters (economic) | 3 |
| Nonpositive \(E_{\mathrm{signal}}\) (clean) | 10 |

Economic primary episodes: **ACU** (short, \(E=0.75\)), **CAP** (short, \(E=0.61\)),
**TRUST** (long, \(E=0.067\)). Бины: `>0.50`×2, `(0,0.10]`×1; `(0.10,0.25]` и
`(0.25,0.50]` **пусты**.

### Point estimates (`state_at_L`, equal-episode, N=3)

| L ms | median \(R_L\) | \(p_{\mathrm{survive}}\) | P(\(R\ge0.75\)) | bootstrap 95% CI \(p_{\mathrm{survive}}\) |
|------|----------------|--------------------------|-----------------|------------------------------------------|
| 50 | 0.90 | 0.67 | 0.67 | [0.00, 1.00] |
| 100 | −0.07 | 0.33 | 0.33 | [0.00, 1.00] |
| 200 | −0.08 | 0.00 | 0.00 | [0.00, 0.00] |

`fill_contract_L`: те же point \(p_{\mathrm{survive}}\) (нет qualitative disagreement
на этой крошечной выборке).

Exact events (KM read at report L): к 100 ms уже 2/3 потеряли ≥50% gross /
убили net edge; к 200 ms — все трое.

Candidate CP band 40–110 ms: на plot annotation only — внутри неё как раз
переход surviving→killed у этих 3, но **не** валидированный change-point.

## 6. Historical validation

Lean ticks + user windows с 30m pre-roll. Runtime prequiet: ≥1 s \(z\le4\)
до пересечения + наличие below-threshold в pre-roll.

Методологический чеклист:

1. Canon clean/isolated/prequiet primary — **нет** (N=0); operational proxy задокументирован.
2. Bootstrap CI для 50/100/200 — да (бессмысленно широкие).
3. state vs fill — сравнены.
4. LOO при N=3 нестабилен по определению → underpowered, не «устойчивый эффект».
5. Gap/censor fail-closed — да.
6. Отчёт в п.п., \(R_L\), \(p_{\mathrm{survive}}\) — да.
7. Недостаточная выборка названа **underpowered**, не alpha.

## 7. Success criteria → verdict

Эксперимент **методологически** доведён до отчёта; **научный** verdict:

> **underpowered**

Дополнительный факт (audit, не primary survival): у 10/13 clean onset
\(E_{\mathrm{signal}}\le0\) при первом \(z>4\) — большинство «аномальных»
пересечений **не** имеют положительного планового края после frozen RT costs.
Это ограничение популяции для latency-survival, не доказательство kill-rate
на крупных дислокациях.

## 8. Next step / stop

**Стоп.** Канон и entry rules не трогать.

Чтобы снять underpowered:

1. выставить в manifest `prequiet_verified=true` только после ручной/скриптовой проверки;
2. расширить каталог **крупных** дислокаций с \(E_{\mathrm{signal}}>0.25\) п.п.;
3. не смешивать nonpositive-\(E\) onset в economic survival;
4. confirmatory на новом окне после набора ≥50 clean economic episodes.

Без этого handoff в правила входа запрещён.

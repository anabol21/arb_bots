# Гир 2.2 · random-anchor latency/persistence baseline

Трек **M**, scratch only. Канон / `VARIATION` / `HYPER` / `Trade_Lat=100` /
fees / ingest / `model_gear2` / `gear2_backtest` **не** менялись.

Не продолжает underpowered `candidate CP 40–110 ms` из edge-survival.

**Вердикт:** `cadence artifact`

(с оговоркой: на срезе \(L=100\) есть слабая направленная асимметрия,
растущая с \(z\) и не исчезающая при \(W=50/100\), но форма кривых по \(L\)
не защищена от fill-cadence; `z_gt_4` underpowered для CP.)

---

## 1. Pipeline block

Условная random-market baseline:

\[
P\bigl(s_{\mathrm{fill}}(L)-s_{\mathrm{signal}}\mid z_{\mathrm{hold}},W\bigr)
\]

на wall-clock anchors (не тик-сэмпл), с persistence-arms
\(W\in\{0,20,50,100\}\) и time-weighted \(Q_{25}\) по длительности L1-state.
Цель — отделить однотиковый шум от устойчивого signal→fill degradation.
Не оценка latency конкурентов.

## 2. Files

| Путь | Роль |
|------|------|
| `research/gear22_random_anchor_persistence_lib.py` | протокол |
| `research/gear22_random_anchor_persistence.ipynb` | scratch + plots |
| `research/gear22_random_anchor_persistence.md` | отчёт |
| `research/output/gear22_random_anchor_persistence/` | frozen days/anchors, summaries, bootstrap, verdict |

Источник: `output/lean_ticks` read-only. Панель: 13 монет manifest (без отбора
по anomaly intervals). Fill: первый тик \(ts\ge t_0+L\) (simulator contract).

## 3. Candidates / interpretations

1. **noise-compatible** — эффект только при \(W=0/20\)
2. **persistent degradation candidate** — \(A>0\) при \(W=50/100\), растёт с \(z\)
3. **no directional effect** — \(A\approx0\)
4. **cadence artifact** — излом/форма следует `actual_fill_delay`
5. **underpowered** — редкий `z_gt_4` / нет gate

## 4. Risks

- Публичный L1 cadence ≫ 10 ms → diagnostic grid \(L\) не равен рыночному времени.
- `z_gt_4` редок при random sampling (не добирали post-hoc).
- Arms \(W\) вложены → не независимы.
- Bootstrap = 30-min wall-clock blocks (все монеты/стороны вместе).
- 2Z/WAL не исключались целиком; fail-closed + file/intra holes режут окна.

## 5. Experiment / patch

### Freeze (до outcomes)

- Seed: `20260827`
- Candidate full days (документированные 288/288 в 6–18 Aug, без Aug10/16–18 outage/sparse):  
  6,7,8,9,11,12,13,15 → выбраны **5**:  
  `2026-08-07, 08, 11, 12, 15`
- Anchors: 1000/день wall-clock, min gap 1 s, общие для 13 монет → **5000**
- Accepted persistence cells (после DQ + \(s_{\mathrm{hold}}>0\)): **165 897**
  anchor×side×W (quality_counters)

### Primary read @ \(L=100\), \(h=1\) (sides pooled)

| W | z_bin | n | blocks | A | p− | p+ | p_approx | gate |
|---|-------|---|--------|---|----|----|----------|------|
| 0 | z_le_1 | 21237 | 240 | −0.005 | 0.029 | 0.034 | 0.94 | yes |
| 100 | z_le_1 | 21336 | 240 | −0.002 | 0.031 | 0.034 | 0.93 | yes |
| 0 | z_1_2 | 4373 | 240 | +0.039 | 0.054 | 0.015 | 0.93 | yes |
| 100 | z_1_2 | 4242 | 240 | +0.032 | 0.050 | 0.018 | 0.93 | yes |
| 0 | z_2_4 | 1753 | 239 | +0.092 | 0.106 | 0.014 | 0.88 | yes (long) |
| 100 | z_2_4 | 1655 | 238 | +0.062 | 0.077 | 0.015 | 0.91 | yes (long) |
| 0 | z_gt_4 | 486 | 201 | +0.095 | 0.117 | 0.023 | 0.86 | **no** (<500) |
| 100 | z_gt_4 | 459 | 198 | +0.061 | 0.087 | 0.026 | 0.89 | **no** |

`median_u≈0`: большинство fills ≈ signal в единицах \(\sigma_0\).

### Cadence diagnostic

- `actual_fill_delay` p50 при **requested L=10 ≈ 159 ms**
- при L=100 p50 ≈ 200–385 ms в зависимости от страты
- → 10 ms grid **не** резолвит рыночное время; CP по \(L\) запрещён/незащищён

### Persistence vs noise

Эффект **не** исчезает при \(W=50/100\) для `z_2_4`/`z_gt_4` → не чистый
однотиковый noise-compatible. Но `z_gt_4` underpowered; форма \(A(L)\)
сомнительна из-за cadence → итоговый класс **cadence artifact**.

## 6. Historical validation

Lean ticks; fail-closed skew/age на read; gap/slack reject; holes fail-closed.
Cluster bootstrap 2000× по 30-min blocks; LOO day/coin записаны.
Change-point **не** оценивался: для `z_gt_4` gate не выполнен; даже при
pass на `z_2_4` protocol + cadence → CP deferred.

## 7. Success criteria → verdict

Методологически done (freeze → wall-clock → W arms → block bootstrap →
cadence check → power gate → classification).

**Научный verdict: `cadence artifact`.**

Не доказано: latency конкурентов; пригодность \(L\)-излома как market τ\*.
Не handoff в entry rules.

## 8. Next step / stop

**STOP.** Канон не менять.

Возможные отдельные pre-registered follow-ups (только по решению):

1. coarsen analysis к реальному fill cadence (например report-only 50/100/200);
2. отдельный oversampling rare \(z>4\) **до** outcomes;
3. state_at_L vs fill_contract paired design на том же frozen anchor set.

Без этого — не переносить в правила входа.

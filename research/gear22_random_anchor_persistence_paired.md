# Гир 2.2 · paired state vs fill (frozen random anchors)

Минимальный follow-up к `gear22_random_anchor_persistence`.  
Те же frozen days/anchors. Нового sampling и oversampling \(z>4\) нет.  
Канон / `Trade_Lat` / fees не менялись.

**Вердикт:** `market dynamics + cadence amplification`

> Высокий устойчивый \(z\) связан с более частым последующим схлопыванием  
> уже в публичном L1 (`state_at_L`); fill-контракт усиливает асимметрию  
> (`u_{\mathrm{cadence}}=u_{\mathrm{fill}}-u_{\mathrm{state}}\)).  
> Время схлопывания по fill-контракту по-прежнему **не** идентифицировано.

---

## 1. Pipeline block

Paired на каждом accepted anchor/side/W/L:

\[
u_{\mathrm{state}}(L)=\frac{s_{\mathrm{state}}(t_0+L)-s(t_0)}{\sigma_0},\quad
u_{\mathrm{fill}}(L)=\frac{s_{\mathrm{fill}}(L)-s(t_0)}{\sigma_0},\quad
u_{\mathrm{cadence}}=u_{\mathrm{fill}}-u_{\mathrm{state}}.
\]

Асимметрии \(A=p_--p_+\) (\(h=1\)) считаются отдельно для state / fill / cadence.  
Long/short раздельно; все \(W\); block bootstrap + LOO.

## 2. Files

| Путь | Роль |
|------|------|
| `research/gear22_random_anchor_persistence_paired_lib.py` | протокол |
| `research/gear22_random_anchor_persistence_paired.ipynb` | scratch |
| `research/gear22_random_anchor_persistence_paired.md` | отчёт |
| `research/output/gear22_random_anchor_persistence_paired/` | summary, bootstrap, LOO, verdict |

Freeze source: `research/output/gear22_random_anchor_persistence/{frozen_days,frozen_anchors}.csv`.

## 3. Candidates

1. Асимметрия уже в `state_at_L` → динамика публичного L1  
2. Только в `fill_contract_L` → wait-for-next-tick  
3. Обе направлены одинаково, fill сильнее → рынок + cadence amplification  

## 4. Risks

- Paired sample требует оба валидны → чуть меньше n, чем fill-only  
- `z_gt_4` всё ещё underpowered (без oversample)  
- Cadence amplification ≠ идентификация \(\tau_*\)

## 5. Experiment / results

Freeze: дни `2026-08-07,08,11,12,15`; 5000 anchors; те же 13 монет / DQ.

### High-z pool @ L=100 (`z_2_4`+`z_gt_4`, sides×W)

| Metric | Point |
|--------|------:|
| \(A_{\mathrm{state}}\) | **+0.037** |
| \(A_{\mathrm{fill}}\) | **+0.078** |
| \(A_{\mathrm{cadence}}\) | **+0.042** |
| \(A_{\mathrm{state}}\) @ W=50 / 100 | +0.032 / +0.021 |
| \(A_{\mathrm{fill}}\) @ W=50 / 100 | +0.072 / +0.062 |

### По стратам @ L=100 (pooled sides×W)

| z_bin | \(A_{\mathrm{state}}\) | \(A_{\mathrm{fill}}\) | \(A_{\mathrm{cadence}}\) | n |
|-------|----------------------:|---------------------:|------------------------:|--:|
| z_le_1 | −0.001 | −0.004 | −0.002 | 85128 |
| z_1_2 | +0.016 | +0.035 | +0.020 | 17236 |
| z_2_4 | +0.039 | +0.077 | +0.040 | 6836 |
| z_gt_4 | +0.026 | +0.079 | +0.049 | 1890 |

`z_2_4` long: bootstrap CI для \(A_{\mathrm{state}}\) и \(A_{\mathrm{fill}}\) целиком >0 на W=0…100.  
`z_gt_4`: CI шире; при W=100 long \(A_{\mathrm{state}}\) CI пересекает 0.

Пример `z_2_4` long W=100:

| L | \(A_{\mathrm{state}}\) | \(A_{\mathrm{fill}}\) | \(A_{\mathrm{cadence}}\) |
|---|----------------------:|---------------------:|------------------------:|
| 50 | 0.014 | 0.056 | 0.041 |
| 100 | 0.025 | 0.062 | 0.042 |
| 200 | 0.038 | 0.076 | 0.034 |

## 6. Historical validation

Тот же lean read-only path; 30-min block bootstrap; LOO day/coin в output.  
Не новый universe.

## 7. Success → verdict

Паттерн **3**: state уже даёт \(A>0\) на высоком \(z\) (в т.ч. W=50/100 для `z_2_4`);  
fill сильнее на ту же величину порядка \(A_{\mathrm{cadence}}\approx0.04\).

**Не** «только fill-wait». **Не** доказанный latency change-point.

## 8. Next step / stop

**STOP.** Канон и entry rules не трогать.

Итог ветки: высокий устойчивый \(z\) → чаще collapse в L1;  
fill-контракт амплифицирует; timing по текущему fill **не** ID.

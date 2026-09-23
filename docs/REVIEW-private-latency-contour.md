# Review: private latency contour (local only)

Дата: 2026-09-04. Сейчас на локальном `main` (merge `e205ca6`). Ветка-сборка была `review/private-latency-contour`. В `origin` **не** пушили.
База: `origin/dev` @ `da8fc44` + merge PR #14 (hot-path) + PR #15 (Warm-Lat).

Цель просмотра: новый контур должен быть грамотнее старого `else/bybit_ws.py`, но на критическом пути send не медленнее формы `queue → ws.send`.

**Не сделано здесь:** мерж в `origin/dev` / `main`, выкат на VPS, live-ордера.

---

## Эталон скорости

Файл: `else/bybit_ws.py` (gear 1).

- public + private WS с старта, один asyncio-цикл
- ордер = JSON → `cmd_queue.put` → long-lived sender → `ws.send`
- без lease / approval / journal / `local_prepare` на send


Живые fills после warm PR #12 всё ещё ~5–6 с signal→done; биржа ~0.1–1.2 с; остальное — pre-send.

---

## PR #14 — hot-path dual-leg

Remote: https://github.com/anabol21/arb_bots/pull/14  
Ветка: `cursor/dual-leg-hot-path-dfd0`

### Идея
Prepare (journal / approval / lease / profile) **до** enqueue; на send — почти только dispatch на тёплые trade WS (как `bybit_ws`).

### Ключевые файлы
| Файл | Зачем смотреть |
|------|----------------|
| `app/bot/private/ws_dual_hot.py` | prefetch dual-leg hot context |
| `app/bot/private/order_sender.py` | `prepare_approved` vs `dispatch_prepared`, `TradeSendQueue` |
| `app/bot/private/order_preflight.py` | TTL/preflight кэши |
| `app/bot/private/order_approval.py` | индекс approval вместо полного rescan |
| `app/bot/private/journal_v1.py` | hot indexes, без full-tree validate на send |
| `app/bot/private/ws_w6_dual_leg.py` | W6/W7: prepare на caller → parallel dispatch |
| `tests/test_dual_leg_hot_path.py` | тесты hot-path |
| `docs/b-private-status.md` | статусная заметка |

### Gap
`app/bot/private/live_broker.py` у тебя локально **untracked** / на VPS — в этих PR его нет. Выкат потом точечный: вызвать prefetch + prepare→enqueue из live_broker, не оверлеить весь git.

### Как гонять тесты локально
```bash
cd ~/Desktop/spread
PYTHONPATH=. python3 -m unittest tests.test_dual_leg_hot_path -v
```

---

## PR #15 — Warm-Lat experiments

Remote: https://github.com/anabol21/arb_bots/pull/15  
Ветка: `cursor/warm-latency-experiments-ab43`

### Идея
Замер place-path **после** `warm ready=True`: Path A ≈ queue→send, Path B ≈ текущий prepare. Dry по умолчанию; live — только явный gate.

### Ключевые файлы
| Файл | Зачем смотреть |
|------|----------------|
| `docs/b-private-warm-latency-experiments.md` | полный протокол + VPS recipe |
| `app/bot/private/ws_warm_latency.py` | CLI/harness |
| `app/bot/private/warm_latency_stages.py` | стадии тайминга |
| `app/bot/private/ws_gates.py` | флаги gate |
| `tests/test_warm_latency_stages.py` | hermetic tests |

### Dry / рецепт
```bash
PYTHONPATH=. python3 -m app.bot.private --ws-warm-latency --warm-lat-print-vps-recipe
```

Live на VPS (когда скажешь): TRUMP ~$6–8/нога, `n` с 1, результаты `/data/bbot-gear2/private/warm_lat/`, collector не рестартить, без full-repo overlay.

### Тесты
```bash
PYTHONPATH=. python3 -m unittest tests.test_warm_latency_stages -v
```

---

## Как смотреть diff

```bash
cd ~/Desktop/spread
git status -sb   # должна быть review/private-latency-contour

# всё относительно текущего origin/dev (до этих двух PR)
git diff --stat origin/dev...HEAD
git diff origin/dev...HEAD -- app/bot/private/

# только hot-path
git log --oneline origin/dev..origin/cursor/dual-leg-hot-path-dfd0

# только Warm-Lat
git log --oneline origin/dev..origin/cursor/warm-latency-experiments-ab43
```

Вернуться на main:
```bash
git checkout main
```

Ветка review локальная; удалить после просмотра:
```bash
git branch -D review/private-latency-contour
```

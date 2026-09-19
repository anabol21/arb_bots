# Human contour diagrams

This folder is the **human** map of running (or coded) process shapes. It is not the agent snapshot in root `architecture.md` (merged from PR #55). It is not application code.

Six contour views. No seventh. Compact/backup live **inside** Live prices. There is no paper view. Click a process block to open that process’s **code** view (modules and file paths).

Terms belong in [`GLOSSARY.md`](GLOSSARY.md). Diagram titles use plain words: сбор цен, решение, журнал, отправка ордера. If a contour page must use a term of art, it says «см. глоссарий: term» on first mention.

## Open Structurizr Lite

From the **repository root** (Docker required):

```bash
docker run --rm -it -p 8080:8080 \
  -v "$(pwd)/docs/human/c4:/usr/local/structurizr" \
  structurizr/lite:2025.11.08
```

Then open [http://localhost:8080](http://localhost:8080) and pick a view by its **title** (the six contour names below). Model file: [`c4/workspace.dsl`](c4/workspace.dsl).

### Click a block to see code

On a **Contour — …** view, click a **process** box (blue rectangle: сбор цен, уплотнение, копия, решение, поиск, прогон). Lite opens the matching **Code — …** view (same human name as the block): modules inside that process, file paths under each box, solid = sync / dashed = async.

Do **not** expect a drill-down from:

- cylinders (files / journals)
- pipes (in-process queues)
- grey externals (exchanges, remote copy)
- the person

Those have no component view. The writer or sender for a cylinder lives on the process you click. Mermaid in [`exports/`](exports/) is Level 2 only — no click-through.

Pin **`structurizr/lite:2025.11.08`**. Untagged `structurizr/lite` (`latest`) only prints a vNext deprecation banner and exits; it does not serve diagrams.

This environment had **no Docker**; Lite GUI was not smoked here. Structurizr CLI parsed the DSL (22 views). Verify click-through locally with the pinned image. See [`NOTES.md`](NOTES.md).

Without Docker, GitHub can render the mermaid overviews in [`exports/`](exports/).

## Reading order

1. This README (table + how to open Lite + how to click through).
2. Skim [`GLOSSARY.md`](GLOSSARY.md) — do not hunt terms in the diagrams.
3. Pick **one** contour page in [`contours/`](contours/).
4. Open that contour in Lite, then click a process block (or open the **Code — …** title from the view list).

Do not overlay Live prices and Canary prices. Do not treat architecture.md’s name “Contour B” as the human view **Contour — Canary B**.

## Contours

| View | contour-id | status | Where the shape is defined | Page |
|---|---|---|---|---|
| Contour — Live prices | d-live | live | `main` `deploy/systemd/spread-collector.service` + compact/backup units | [contours/d-live.md](contours/d-live.md) |
| Contour — Canary prices | d-hotadd-canary | canary | PR #53 `cursor/hot-add-new-coins-58c9` canary collector + discovery units (**absent on main**) | [contours/d-hotadd-canary.md](contours/d-hotadd-canary.md) |
| Contour — Stub B | b-stub | legacy | `main` `spread-bbot.service`, `spread-bbot-gear2.service` | [contours/b-stub.md](contours/b-stub.md) |
| Contour — Canary B | b-theta-would-send | canary | `main` profile/docs; **no unit file in git**; VPS name `spread-bbot-theta-k1-canary` | [contours/b-theta-would-send.md](contours/b-theta-would-send.md) |
| Contour — Live send | b-live-send | canary | `main` `spread-bbot-canary-wal-eden.service`, `spread-bbot-gear22-live-canary.service` (templates) | [contours/b-live-send.md](contours/b-live-send.md) |
| Contour — Simulator | m-sim | sim | `main` `model.ipynb`, `research/gear22_backtest/replay.py` (no systemd) | [contours/m-sim.md](contours/m-sim.md) |

Comparison is this table (plus [`exports/comparison.md`](exports/comparison.md)). There is no mixed-topology container view: that would look like one machine doing everything at once.

## Code views (click-through)

Titles are `Code —` plus the process block’s human name. Keys are stable (`code-live-collector`, …).

| Code view | Opens from (contour) | Process block |
|---|---|---|
| Code — Сбор цен | Contour — Live prices | Сбор цен |
| Code — Уплотнение тиков | Contour — Live prices | Уплотнение тиков |
| Code — Копия тиков | Contour — Live prices | Копия тиков |
| Code — Уплотнение баров | Contour — Live prices | Уплотнение баров |
| Code — Копия баров | Contour — Live prices | Копия баров |
| Code — Поиск новых пар | Contour — Canary prices | Поиск новых пар |
| Code — Сбор цен (изолированный) | Contour — Canary prices | Сбор цен (изолированный) |
| Code — Решение без отправки | Contour — Stub B | Решение без отправки |
| Code — Копия журнала | Contour — Stub B | Копия журнала |
| Code — Решение без отправки (4 монеты) | Contour — Stub B | Решение без отправки (4 монеты) |
| Code — Копия журнала (4 монеты) | Contour — Stub B | Копия журнала (4 монеты) |
| Code — Решение раз в секунду, без отправки | Contour — Canary B | Решение раз в секунду, без отправки |
| Code — Решение и отправка (2 монеты) | Contour — Live send | Решение и отправка (2 монеты) |
| Code — Решение и отправка (30 монет) | Contour — Live send | Решение и отправка (30 монет) |
| Code — Прогон истории | Contour — Simulator | Прогон истории |

## Legend (all views)

- **Solid** line = sync (REST, file write, in-process call that waits).
- **Dashed** line = async / event / queue / timer / WebSocket.
- Cylinder = journal or file store.
- Pipe = queue that exists in code (not a separate systemd unit).

## Terms only in GLOSSARY.md

Diagrams and view titles stay in plain words. Jargon (`would_send`, `gear`, `journal`, `contour`, live send, canary, `theta`, …) lives in [`GLOSSARY.md`](GLOSSARY.md). Contour pages may name a term once, as «см. глоссарий: …».

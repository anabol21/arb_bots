# Human contour diagrams

This folder is the **human** map of running (or coded) process shapes. It is not the agent snapshot in `architecture.md` (PR #55, root file, still missing on `main`). It is not application code.

Six views. No seventh. Compact/backup live **inside** Live prices. There is no paper view.

Terms belong in [`GLOSSARY.md`](GLOSSARY.md). Diagram titles use plain words: сбор цен, решение, журнал, отправка ордера. If a contour page must use a term of art, it says «см. глоссарий: term» on first mention.

## Open Structurizr Lite

From the **repository root** (Docker required):

```bash
docker run --rm -it -p 8080:8080 \
  -v "$(pwd)/docs/human/c4:/usr/local/structurizr" \
  structurizr/lite
```

Then open [http://localhost:8080](http://localhost:8080) and pick a view by its **title** (the six names below). Model file: [`c4/workspace.dsl`](c4/workspace.dsl).

This environment had **no Docker**; Lite was not smoked here. Verify locally. See [`NOTES.md`](NOTES.md).

Without Docker, GitHub can render the mermaid overviews in [`exports/`](exports/).

## Reading order

1. This README (table + how to open Lite).
2. Skim [`GLOSSARY.md`](GLOSSARY.md) — do not hunt terms in the diagrams.
3. Pick **one** contour page in [`contours/`](contours/).
4. Open that view in Lite (or the matching mermaid in `exports/`).

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

## Legend (all views)

- **Solid** line = sync (REST, file write, in-process call that waits).
- **Dashed** line = async / event / queue / timer / WebSocket.
- Cylinder = journal or file store.
- Pipe = queue that exists in code (not a separate systemd unit).

## Terms only in GLOSSARY.md

Diagrams and view titles stay in plain words. Jargon (`would_send`, `gear`, `journal`, `contour`, live send, canary, `theta`, …) lives in [`GLOSSARY.md`](GLOSSARY.md). Contour pages may name a term once, as «см. глоссарий: …».

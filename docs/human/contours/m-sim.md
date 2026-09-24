# Contour — Simulator

**contour-id:** `m-sim`  
**Status:** sim

## Why

Replay history on disk to test decision rules. No VPS unit, no public sockets in this view, no order send. Closing a model step here is not live-bot readiness.

## Git / config

- Branch: `main`
- Docs: `docs/strategy-gears.md`
- Entries (offline): `model.ipynb`; `research/gear22_backtest/replay.py`
- Fill in the 1 Hz dummy replay = that second’s `spread_last`, **not** stub `Trade_Lat`
- Gear 2.2 observation contour is **closed**; not size policy (2.5), not search (3), not live-ready
- Reads historical parquet/feature tables from disk. Not drawn as a client of the live collector **process**

## How to open the Structurizr view

Lite → title **Contour — Simulator** (key `m-sim`).  
Click **Прогон истории** for the code view. Mermaid: [`../exports/m-sim.md`](../exports/m-sim.md) (no click-through).

Code views from this contour:

- **Code — Прогон истории**

Historical-tick / feature / trade tables have no code view.

## Legend

1. Solid = the person starts a notebook/script; the script reads files and writes run tables.
2. No dashed exchange lines on this view (history is already on disk).
3. Cylinders = historical ticks, feature table, simulated trades.
4. No systemd box.
5. No «приём ордеров».

## What’s special vs the others

- Only offline view.
- Does not journal `would_send` on the VPS and does not send.
- Files often **originated** from Live prices parquet in the past; that is a data dependency, not a running edge to `spread-collector`.
- Gear numbers live here as model steps (см. глоссарий: gear). Bot units that reuse a gear **name** are other views.

First terms: «см. глоссарий: sim», «см. глоссарий: gear», «см. глоссарий: spread_last», «см. глоссарий: Trade_Lat».

## Unknown / risks

- Which parquet snapshot a given notebook run used is a run-local fact, not a systemd path.
- Do not claim profitability from a short historical run.
- Do not treat a closed gear as permission for Live send.

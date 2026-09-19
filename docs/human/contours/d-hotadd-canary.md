# Contour — Canary prices

**contour-id:** `d-hotadd-canary`  
**Status:** canary

## Why

A thin, isolated collector that can subscribe extra coins **without** writing production ticks. Used to wait for a real new listing (or a timed trial) while the live collector stays untouched.

## Git / config

- **Not on `main`.** Shape taken from PR #53 branch `cursor/hot-add-new-coins-58c9` @ `0ce5da6` only. Do not mix this topology onto Live prices.
- Units: `deploy/systemd/spread-collector-hotadd-canary.service` + `spread-discovery-hotadd-canary.{service,timer}`
- Collector: same `app/screaner_b_o.py`, clone working directory `/root/spread_hotadd_canary`
- Discovery: oneshot `python -m app.discovery` on a 30-minute timer (REST lists, writes a delta file, does not rewrite the universe CSV)
- Isolated files: `/data/live-hotadd-canary`, `/data/spool-hotadd-canary`, `/data/gaps-hotadd-canary`
- Unit denies `/data/live`. Extra-coin subscribe is **on** for this unit only. Universe: full production copy + backfill, then exactly 10 `take=yes` crypto pairs. Drop file is the same process, not another view.
- No compact/backup units for this tree in that branch.
- Recon: VPS started 2026-09-19 13:50 UTC; idle `pairs=10`, `delta_rows=0` at 19:32. Production collector is a **different** process.

## How to open the Structurizr view

Lite → title **Contour — Canary prices** (key `d-hotadd-canary`).  
Click a process box for the code view. Mermaid: [`../exports/d-hotadd-canary.md`](../exports/d-hotadd-canary.md) (no click-through).

Code views from this contour:

- **Code — Поиск новых пар** (PR #53 `app/discovery/**`, not on `main`)
- **Code — Сбор цен (изолированный)** (same collector script + PR #53 `app/utils/hot_add.py`)

Cylinders (delta, drop, isolated ticks/spool/gaps) and the pipe have no code view.

## Legend

1. Solid = REST instrument lists, parquet write.
2. Dashed = WebSocket books, timer discovery, poll of delta/drop files.
3. Pipe = in-process publish queue inside the isolated collector.
4. Cylinders = isolated ticks/spool/gaps + delta/drop files.
5. No remote-copy box — none in that branch’s units.

## What’s special vs the others

- Only view whose code lives on PR #53, not `main`.
- Writes a **separate** tick tree; production `/data/live` is unreachable to this process.
- Has a **search-new-pairs** sidecar; Live prices does not.
- Still **prices only** — no decision, no order send, no bot fan-out.

First terms: «см. глоссарий: canary», «см. глоссарий: discovery», «см. глоссарий: hot-add», «см. глоссарий: take=yes».

## Unknown / risks

- Whether a listing event happened after 19:32 UTC: unknown.
- Merging #53 does not by itself change production `spread-collector`.
- Do not start a second writer on `/data/live`.
- Do not draw this collector on the Live prices view.

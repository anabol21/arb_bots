# Contour — Live send

**contour-id:** `b-live-send`  
**Status:** canary

## Why

The only coded shape that may put a dual-leg order on the private trading channel. Two **templates**, same send path, **must not share one process** (different decision clocks).

## Git / config

- Branch: `main`
- Units (templates; comments: do not enable by creating the file):
  - `deploy/systemd/spread-bbot-canary-wal-eden.service` — 2 coins, market-tick decide, data `/data/bbot-canary-wal-eden`
  - `deploy/systemd/spread-bbot-gear22-live-canary.service` — 30 coins, 1 Hz theta, data `/data/bbot-gear22-live-canary`
- Entry: `python -m app.bot` with broker `private_live`. Send only if `VENUE=live` **and** `LIVE_ORDERS=1` (см. глоссарий: live send). Missing env file → fail-closed.
- Send path in code: in-process `asyncio.Queue` then venue send. Architecture.md calls this path “Contour B” — not the human view **Contour — Canary B**.
- 30-coin unit sets a private data root under its own tree. WAL/EDEN does **not** set that variable and denies `/data/bbot` — private/wire location for WAL/EDEN is unknown.
- No backup units for these roots in git.
- Recon 2026-09-18: both **inactive/disabled**. Later enablement unknown. No paper mode.

## How to open the Structurizr view

Lite → title **Contour — Live send** (key `b-live-send`).  
Mermaid: [`../exports/b-live-send.md`](../exports/b-live-send.md).

## Legend

1. Solid = journal / wire append after the process has an outcome.
2. Dashed = public books, order queue, send, ACK.
3. Two pipes = two in-process queues (one per unit). Do not draw a shared queue.
4. «Приём ордеров» appears **only** on this view.
5. Two decision boxes on one diagram = two templates of the **same** send class, not permission to run both in one OS process.

## What’s special vs the others

- Only view with **отправка ордера**.
- Canary B journals the 1 Hz intent and stops; the 30-coin unit here can send at that same clock.
- WAL/EDEN decides on **market ticks**, 2 coins — different clock than the 30-coin unit.
- Collector trees are denied. Do not confuse with price canaries.

First terms: «см. глоссарий: live send», «см. глоссарий: Contour B», «см. глоссарий: theta», «см. глоссарий: wire».

## Unknown / risks

- Re-check `systemctl` before calling this “running”.
- WAL/EDEN private journal path vs denied `/data/bbot`: unknown.
- No git backup for these data roots.
- Optional W6 send path is **off** on these units; do not draw it.
- `python -m app.bot.private` is a CLI harness, not this loop.

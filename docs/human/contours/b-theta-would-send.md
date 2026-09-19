# Contour — Canary B

**contour-id:** `b-theta-would-send`  
**Status:** canary

## Why

Watch and journal 1 Hz decisions on public books **without** sending orders. Same bot entrypoint as Stub B, different profile and data root. This is **not** architecture.md’s “Contour B” (that name is the live send path).

## Git / config

- Branch: `main` **describes** the profile (`BBOT_PROFILE=gear22_would_send`), docs `docs/gear22-theta-k1-would-send.md`, `docs/gear22-live-canary.md`
- **No systemd unit in any git branch.** VPS name `spread-bbot-theta-k1-canary` — body unknown until committed
- Process (as coded): `python -m app.bot` with stub broker; in-process floor / tw_p50 / theta observers; journal `theta_trades`
- Data root claimed in docs: `/data/bbot-theta-k1-canary`
- No private send. Floor watcher is **in-process** (do not invent `spread-bbot-floor-canary` as a view; that name is not in git)
- Recon 2026-09-18/19: this VPS unit **active**. Treat as VPS-only evidence, not a file in this repo.

## How to open the Structurizr view

Lite → title **Contour — Canary B** (key `b-theta-would-send`).  
Mermaid: [`../exports/b-theta-would-send.md`](../exports/b-theta-would-send.md).

## Legend

1. Solid = intent journal write.
2. Dashed = public WebSocket, metrics flush.
3. One process (not a sidecar observer).
4. Cylinders = metrics jsonl + intent jsonl.
5. No order queue, no trading-channel boxes, no remote-copy unit in git.

## What’s special vs the others

- Decision clock is **once per second**, not every market tick (the 2-coin live-send unit uses a tick clock — do not merge processes).
- Still **no** order send; intents only (см. глоссарий: would_send, см. глоссарий: theta).
- Not Stub B’s `/data/bbot` or `/data/bbot-gear2`.
- Not Live send, even though the 30-coin send unit copies the same frozen knobs.

First terms: «см. глоссарий: canary», «см. глоссарий: theta», «см. глоссарий: would_send», «см. глоссарий: gear».

## Unknown / risks

- Unit file contents, `Environment=` list, and backup: **unknown** (not in tree).
- Do not fan-out this canary onto new listings from Canary prices.
- GREEN would_send ≠ permission to send.

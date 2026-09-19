# Contour — Stub B

**contour-id:** `b-stub`  
**Status:** legacy

## Why

The original live asyncio bot: its **own** public books, a decision, a journal of intents, **no** private sockets and **no** order send. Isolated from collector trees.

## Git / config

- Branch: `main`
- Units (templates; comments say they are not enabled merely by existing in git):
  - `deploy/systemd/spread-bbot.service` — `python -m app.bot`, probe, data `/data/bbot`
  - `deploy/systemd/spread-bbot-gear2.service` — same entry, policy profile, data `/data/bbot-gear2`
- Backup oneshots: `spread-bbot-backup-transfer`, `spread-bbot-gear2-backup-transfer` → remote prefixes `spread-bbot` / `spread-bbot-gear2`
- Units deny collector paths (`/data/live`, `/data/bars`, `/data/compacted`, `/data/spool`)
- Recon 2026-09-18: both **inactive**. Gear2 journal GREEN 2026-08-30 is historical, not “running now”.

## How to open the Structurizr view

Lite → title **Contour — Stub B** (key `b-stub`).  
Mermaid: [`../exports/b-stub.md`](../exports/b-stub.md).

## Legend

1. Solid = journal append, backup process body.
2. Dashed = public WebSocket books, backup timer.
3. Two **separate** processes (probe vs 4-coin policy) — same shape, different data roots.
4. Cylinders = intent journals.
5. No «приём ордеров» boxes.

## What’s special vs the others

- Public books only. Nothing is sent to the trading channel.
- Journal says “would have sent” and `send=false` (см. глоссарий: would_send).
- Not the 1 Hz canary (that is **Contour — Canary B**).
- Not live send (that is **Contour — Live send**).
- Collector parquet is unreachable on purpose.

First terms: «см. глоссарий: stub», «см. глоссарий: journal», «см. глоссарий: would_send», «см. глоссарий: legacy».

## Unknown / risks

- Later enablement after 2026-09-18: unknown.
- `private_testnet` broker is **not** this view (no unit; same would_send topology).
- Do not `BindsTo=` the collector. Do not stop the collector to work on this bot.

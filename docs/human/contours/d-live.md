# Contour — Live prices

**contour-id:** `d-live`  
**Status:** live

## Why

This is the production price collector: public books → files on the VPS → compact → remote copy. It does not decide trades and does not send orders.

## Git / config

- Branch: `main`
- Unit: `deploy/systemd/spread-collector.service` → `app/screaner_b_o.py`
- Companions on the **same** data plane (not extra views): `spread-compactor.{service,timer}`, `spread-backup-transfer.{service,timer}`, bars compact/backup timers
- Working directory in the unit: `/root/spread_staging`
- First write: `/data/live` (ticks). Spool `/data/spool`. Gaps `/data/gaps`. Compacted `/data/compacted`
- Recon (2026-09-18/19): this unit **active**, ~188 pairs. Production flag for extra-coin subscribe is **off** here.

## How to open the Structurizr view

Lite → title **Contour — Live prices** (key `d-live`).  
Click a process box for the code view. Mermaid: [`../exports/d-live.md`](../exports/d-live.md) (no click-through).

Code views from this contour:

- **Code — Сбор цен**
- **Code — Уплотнение тиков**
- **Code — Копия тиков**
- **Code — Уплотнение баров**
- **Code — Копия баров**

Cylinders (тики, запас, пропуски, уплотнённые, бары) and the pipe (очередь записи) have no code view.

## Legend

1. Solid = sync (file write, oneshot body).
2. Dashed = async (WebSocket, publisher queue, timers).
3. Pipe = in-process publish queue (`queue.Queue` in the collector).
4. Cylinders = tick/spool/gap/compacted files.
5. Bars compact/backup sit on this diagram because they share the live host data plane; **who writes `/data/bars` is unknown** while the collector has bars collection off.

## What’s special vs the others

- Only view that is the **production** price store (`/data/live`).
- Canary prices uses the same script in **another** directory and **must not** appear here.
- No decision box, no order send.
- Compact and backup belong **here**, not as their own contours.

First terms: «см. глоссарий: contour», «см. глоссарий: journal», «см. глоссарий: spool», «см. глоссарий: parquet».

## Unknown / risks

- VPS enablement of companion timers not re-checked in this docs change.
- Bars writer vs collector bars=off: unknown (same as PR #55).
- Compactor cadence conflict (timer 2 min / cron 5 min / `--interval 300`): unknown which is installed.
- Local lean collector is **not** this view.

# Glossary

Plain definitions for terms of art. Diagrams and view titles do **not** use these as labels. Contour pages may mention a term once: «см. глоссарий: term».

Grouped, then alphabetical inside the group.

## Contours and status

**canary** — Isolated copy of a process + its own data directory, so a trial cannot write the production tree. Why: prove a change without touching live ticks or the legacy stub journal. Views: **Contour — Canary prices**, **Contour — Canary B**, **Contour — Live send**.

**contour** — One process shape: who talks to exchanges, where files land, whether an order is sent. Why: git branches are not contours; extra timers on the same data plane are not extra contours. All six views.

**legacy** — Coded and still in git, not the active VPS decision process in the 2026-09-18/19 snapshots. Why: keep the old stub units visible without calling them “the live bot”. View: **Contour — Stub B**.

**live** (status) — Production data plane for public prices on the VPS. Why: not the same word as “live send”. View: **Contour — Live prices**.

**paper** — **Not a coded process.** Venue is `testnet` or `live` only. Do not add a seventh view.

**sim** — Offline historical run. No systemd, no order socket. View: **Contour — Simulator**.

## People and externals (Level 1)

**arb_bots** — This repository as one software system. Why: Level 1 shows exchanges / human / remote copy around it; Level 2 splits that system into six container views.

**remote copy** — rclone SFTP copy of compacted files. Why: first write is on the VPS disk; the copy is the durable off-box duplicate. Views: **Contour — Live prices**, **Contour — Stub B**. Not wired for Canary prices, Canary B, or Live send in git.

## Collection (D)

**discovery** — Oneshot REST sidecar that diffs exchange instrument lists against a universe CSV and writes a delta file. Why: the collector must not call that REST itself. View: **Contour — Canary prices** only (PR #53). Absent on `main`.

**hive** — Directory layout `base_coin=*/event_date=*`. Why: ticks and bars are files in that tree, not a SQL database. Views: Live prices, Canary prices, Simulator (reads history).

**hot-add** — In-process subscribe of extra coins from a delta file, default **off** on production. Why: Canary prices turns it on; Live prices unit does not. View: **Contour — Canary prices**.

**lean** — Tick parquet without precomputed spread columns; spread is derived when reading. Why: production collector sets this on. Views: Live prices, Canary prices.

**parquet** — Columnar files for ticks/bars. Why: this is the collector journal, not the bot jsonl. Views: Live prices, Canary prices, Simulator.

**spool** — Local durable staging when the primary parquet publish fails; a recovery worker retries. Why: do not confuse with the bot journal. Views: Live prices, Canary prices.

**take=yes** — Row flag in the universe CSV that is the live pair screen. Why: Canary prices uses a 10-row `take=yes` slice on a full copy; Live prices uses the production CSV. Views: Live prices, Canary prices.

## Decision, send, journals (B)

**Contour B** — Name in `architecture.md` (PR #55) for the default live path: in-process queue then `ws.send`. Why: **not** the human view **Contour — Canary B**. View: **Contour — Live send**.

**gear** — Maturity step of the **historical model** (1.0 → 1.5 → 2 → 2.2 → 2.5 blocked → 3). Why: closing a gear in the notebook is not live-bot readiness. Views: Simulator; bot units may reuse a gear name as a profile.

**journal** — Append-only files the process writes (parquet hive or jsonl). Why: first materialization on disk; not “the database product”. Every contour except that Simulator trades are tables in a run, not a VPS unit.

**live send** — Actual venue order transport. Requires broker `private_live`, `VENUE=live`, and `LIVE_ORDERS=1` together. Missing env file → process fails closed. Why: distinct from journaling an intent. View: **Contour — Live send**.

**send** — Flag / act of putting an order on the private socket. Stub journals cannot flip this to true. Views: Stub B and Canary B stay `send=false`; Live send may set true after the gate.

**stub** — Bot broker that never opens private sockets. Fills are local (`Trade_Lat`). Why: public books only. View: **Contour — Stub B** (and Canary B uses the same broker class).

**theta** — 1 Hz observer plus trade manager used by gear-2.2 profiles. Why: Canary B journals intents at that clock; Live send’s 30-coin unit uses the same clock **and** may send. Do not put both clocks in one process. Views: **Contour — Canary B**, **Contour — Live send**.

**Trade_Lat** — Stub fill delay after the signal tick (local, not an exchange ACK). Why: Simulator gear 2.2 does **not** use this; it fills on `spread_last`. Views: Stub B, Canary B; contrast Simulator.

**would_send** — Journaled dual-leg intent that **would** have been sent. On stub paths `would_send=true` and `send=false`. Why: a GREEN would_send run is not permission to send. Views: **Contour — Stub B**, **Contour — Canary B**; Live send also writes intents but may send.

**wire** — Redacted append-only private transcript (`wire.jsonl`) after live transport. Why: operators read this plus theta rows; stub `legs.jsonl` will not show `send=true`. View: **Contour — Live send** (30-coin unit sets a private data root; WAL/EDEN private path is unknown).

## Simulator

**spread_last** — Fill price in the 1 Hz dummy replay: that second’s spread, no delay model. Why: not `Trade_Lat`. View: **Contour — Simulator**.

## What is not a view

**B-private CLI** — `python -m app.bot.private`. Oneshot harness; default asserts no transport / no WS.

**local lean** — `app/screaner_local_lean.py`. Laptop experiment; refuses `/data/*`.

**private_testnet broker** — Reuses stub journal topology; refuses live + `LIVE_ORDERS=1`. No systemd unit.

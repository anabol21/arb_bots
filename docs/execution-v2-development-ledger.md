# Execution v2 development ledger

This file is the reviewed, Git-backed handoff for Codex, Cursor agents and Grok
bots. It records decisions and evidence links, not live account state. Runtime
status is served from the WAL/PostgreSQL projection through MCP.

## Current status — 2026-09-17

Phase: architecture and forensic baseline

Production deployment: not started

Live-order authority: disabled for the new contour

### Verified repository facts

- Canonical base: GitHub `main` at `fb5a6e3` (PR #52).
- GitHub `dev` has no unique commits and is behind `main`; it is not currently
  functioning as the intended integration branch.
- The separate local checkout at `Desktop/spread` has 11 local-only commits,
  many untracked artifacts and an accidental `BotRunrutime` change. It is a
  recovery source, not a safe implementation base.
- Targeted private-contour tests passed: dual-leg send/ACK, warm single loop,
  lease close and Sentry integration (50 tests).
- Existing clean canary evidence shows roughly 1–3 ms signal-to-send samples,
  but the current topology has thread/loop crossings and large historical
  outliers; p99 is not proven.

### Verified VPS facts

- Host: current documented 16 GiB production VPS; audit was read-only.
- `spread-collector.service`: active, healthy heartbeat, no restarts, ample RAM
  and disk at audit time.
- `spread-bbot-theta-k1-canary.service`: active `would_sent` contour; current
  heartbeat showed no held/pending position.
- Private/live trading units: inactive.
- `/root/spread_staging`: unversioned copied tree; key deployed files differ
  from GitHub `main`.
- `/root/spread_gear22_live_canary`: clean checkout at `fb5a6e3`.
- Older floor/theta canary checkouts contain local modifications or untracked
  operational artifacts and must not be treated as reproducible releases.

### Locked decisions

- Venues: Bybit + OKX, same existing accounts/keys for bounded canary.
- Topology: strategy and executor in one Python process/event loop; Rust only if
  target-VPS profiling proves Python cannot meet the SLO.
- Latency SLO: signal to underlying socket write, `p50 <= 1 ms`, `p99 <= 3 ms`.
- State: fills/positions are authoritative; ACK is request state only.
- One-leg policy: reconcile, then reduce-only flatten to zero; never chase the
  peer leg to preserve the trade.
- Persistence: local append-only WAL plus asynchronous PostgreSQL projection.
- Canary: >=20 continuous hours of target-VPS shadow stability; eligible live
  signals are all compared but not used as a duration substitute. A separate
  deterministic replay must cover >=300 eligible signals before 1 and 20 live
  round-trips at `$20` per leg and `K_live=1`.
- Production promotion: explicit user approval.
- Sentry: preserve existing event semantics for Grok compatibility, move
  delivery off the hot loop.
- Grok: Git ledger + custom MCP; only emergency pause is writable.

## Work queue

| ID | Task | Owner | Gate | State |
|---|---|---|---|---|
| EV2-00 | Preserve and classify divergent local checkout | Codex + reviewer | Recovery manifest reviewed | queued |
| EV2-01 | Normalize `dev` and branch protections | Codex | `dev == main`, old ref preserved | queued |
| EV2-02 | Intent/event schemas and fill-authoritative FSM | Cursor runtime agent | unit + property + crash matrix | queued |
| EV2-03 | Same-loop dual-venue warm runtime | Cursor private agent | 10k target-VPS benchmark | blocked by EV2-02 |
| EV2-04 | WAL writer and Postgres projector | Cursor persistence agent | replay/idempotency tests | blocked by EV2-02 |
| EV2-05 | Reconcile and one-leg recovery | Cursor recovery agent | fault-injection matrix | blocked by EV2-02/04 |
| EV2-06 | Async Sentry compatibility exporter | Cursor observability agent | legacy event contract tests | blocked by EV2-04 |
| EV2-07 | Redacted MCP + pause-only control | Cursor control-plane agent | auth/audit/abuse tests | blocked by EV2-04/05 |
| EV2-08 | Shadow and live canary | Codex/operator | staged gates | blocked by all above |

## Required update format

Every merged task appends one ledger entry with:

- timestamp, task/PR, base and merge SHA;
- behavior changed and invariants preserved;
- tests/evidence and target environment;
- schema/migration impact;
- open risks, rollback and next unblocked task.

Do not paste raw exchange responses, credentials, account/order identifiers or
private URLs into this ledger.

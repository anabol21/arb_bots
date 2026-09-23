# EV2-12 → Gear 2.2 private production: gated bridge

Status: plan only, 2026-09-23. No live-order approval, VPS mutation, or
production promotion is implied by this document. Keep the current collector
(`spread-collector-next.service`) and `spread-bbot-theta-k1-canary.service`
running independently. All implementation commits stay on
`codex/execution-v2-dev-2026-09-18` and are pushed after review.

## Pipeline block

```text
frozen Gear 2.2 30-coin policy / would_sent reference
  -> one K=1 intent with stable trade_id and per-leg order IDs
  -> durable intent/WAL -> two-venue readiness + ownership lease
  -> dual-leg order write -> ACK (accepted, NOT filled)
  -> private order/position events + signed REST reconciliation
  -> durable actual per-leg exposure -> manager slot / trade history
  -> close/reduce-only until both legs confirmed flat
```

No transition to OPEN or FLAT is justified by a pair of order ACKs alone.
The venue order-status and filled-quantity streams, with REST recovery after
gaps/restarts, are the authority for actual exposure. An unresolved state
blocks *new opens*, but must preserve a controlled close/reduce-only path.

## Existing modules and evidence

- `app/bot/theta_trade_manager.py`, `app/bot/runtime.py`: policy lifecycle,
  K=1 journal replay, startup reconciliation, and shadow adapter.
- `app/bot/private/live_broker.py`: current Contour B live dual-leg send.
  It documents ACKs as distinct from fills, yet currently updates its local
  position to OPEN/FLAT immediately after dual ACK. This is a live gate blocker.
- `app/bot/private/position_reconcile.py`: signed four-query startup check.
  It compares coin/side and open-order emptiness, not actual leg quantities;
  Bybit position pagination is not explicitly resolved. Account ownership,
  position mode, and snapshot freshness still need an explicit proof.
- `app/bot/execution/wal.py`, `app/bot/execution/readiness.py`: execution
  primitives exist; their production runtime integration with the current
  Contour B broker must be demonstrated end to end, not inferred from isolated
  unit tests or the shadow path.
- `docs/task-packets/EV2-11-restart-state-recovery.md`: two-hour no-order
  restart canary passed with 37 synthetic opens, 36 closes, replay of the
  same trade ID, zero orders, and clean parity. However 15/36 synthetic
  CLOSE rows had `fill_size_ok=false` while the simulation became FLAT.
  This validates journal replay, not live close executability.
- `docs/gear22-live-canary.md`: older operator template; its ACK-aware local
  slot and "flat on abort" description is not evidence of confirmed flatness.
- The current synthetic-policy gate requires `BBOT_BROKER=stub` and
  `LIVE_ORDERS=0`. EV2-13C therefore needs a separately reviewed,
  explicitly armed policy-source-only mode; changing only `policy.py` or
  bypassing the gate is not sufficient.

Venue protocol references: [Bybit trade WebSocket acknowledgement](https://bybit-exchange.github.io/docs/v5/websocket/trade/guideline),
[Bybit private order status](https://bybit-exchange.github.io/docs/v5/websocket/private/order),
and [OKX order channel states](https://www.okx.com/docs-v5/en/).

## Candidate designs

1. Reuse current ACK-based broker unchanged: smallest diff, but an accepted
   order may remain unfilled/partially filled; local K=1 state can contradict
   exchange exposure. Rejected.
2. Add periodic REST checks around the ACK-based broker: improves detection,
   but leaves an ACK-to-journal crash and asynchronous fill window. Not enough.
3. Keep the reviewed policy, but put a durable execution/exposure state
   machine between its intent and its K=1 committed position. ACK, partial
   fill, terminal fill/cancel/reject, reconnection, and restart each have
   explicit transitions. Selected, implemented in narrow patches.

## Patch sequence and exit gates

| Patch | Scope and required evidence | Gate to advance |
| --- | --- | --- |
| EV2-12A — exposure semantics | Separate `intent`, `accepted`, `partially_filled`, `filled`, `closing`, `flat`, and `unknown` in the broker/manager contract. Persist actual per-leg quantities and IDs before publishing K=1 state. Preserve an unfillable or unconfirmed synthetic close as OPEN/closing, never silently FLAT. Replay an unresolved close after restart. | Deterministic tests for ACK-without-fill, partial fill, reject, cancel, late fill, 15/36-style size failures, and restart at each boundary; zero duplicate opens and no fabricated flatness. |
| EV2-12B — durable live adapter | Wire the policy intent through the execution WAL/readiness lease to the actual Contour B sender and private order/position handlers. Give each leg stable idempotency/correlation IDs. The send and journal protocol must define recovery for crash before write, between legs, after ACK, after one fill, and before fsync. No blind retry while outcome is unknown. | Fault-injection and replay matrix passes; every wire attempt is explainable by WAL, private events, and signed REST. Demonstrate that current runtime uses this path, not merely shadow code. |
| EV2-12C — two-venue reconciliation and stop policy | Compare instrument, side, **quantity**, open orders, position mode, account ownership, and freshness on both venues. Resolve pagination and reconnect gaps. A stale/missing private channel, mismatched or unknown exposure, lost lease, or unowned account blocks new opens. Close/reduce-only recovery is separately gated and observable. | Both-flat and one-open-position fixtures pass; mismatch, stale data, extra order, unknown quantity, and pagination fail closed; reconnect/reseed and restart tests prove no new open until restored. |
| EV2-12D — immutable release / observability | Freeze commit SHA, 30-coin universe and policy/risk hashes; isolated unit, data/log roots, secrets, account, and order capability. Expose read-only state and idempotent pause-new-opens. Emit WAL/position/parity/Sentry events and a versioned status handoff for all three Grok bots. Dry-run rollback. | Release manifest, tests, deployment and rollback rehearsal reviewed; no unit is started merely because it exists. |

Each patch has a task packet, scoped Cursor implementation, Codex review,
separate critic PASS, test evidence, and one pushed commit. Preserve collector
and `would_sent`; no changes to frozen policy knobs or 30-coin membership
without a separately reviewed manifest diff.

## Canary ladder (each stage needs explicit user approval)

1. **EV2-13A — read-only production preflight:** read-only credentials with
   order capability disabled; verify exact account ownership, initial flatness,
   position mode, 30-coin metadata, private auth/subscription/reseed, trade
   socket policy, WAL/data durability, lease, and pause. Run a no-order shadow
   through the *same* decision/pre-send adapter. Report signal-to-prepared-
   write separately from any actual order-write/ACK/fill latency.
2. **EV2-13B — isolated real round trip:** after a fresh go/no-go, one coin,
   one K=1 round trip at the previously proposed `$20/leg` ceiling, outside
   the 30-coin continuing contour. Prove both exchange positions and open
   orders flat independently, including actual fills/fees, per-leg quantities,
   WAL-to-private-event parity, Sentry/Grok status, and recovery from a
   controlled restart. Stop new opens on any ambiguity; do not auto-declare
   success merely because close ACKs arrived.
3. **EV2-13C — bounded live synthetic policy:** a separate cap on round trips,
   notional, runtime, and loss; global K=1; same production execution path,
   synthetic policy only at the signal source. Require complete actual open →
   close cycles and deliberately exercised reconnect/restart recovery. Do not
   run controlled faults while an uncontrolled position is open.
4. **EV2-13D — frozen real Gear 2.2 policy, 30 coins:** use the manifest matched
   to `would_sent`; start with the same K=1 and `$20/leg` ceiling. Keep
   collector and would_sent parallel. Let the observation window be driven
   by sufficient real signals and complete round trips, not by two quiet
   hours. Compare eligible signals and decisions, but distinguish synthetic
   fills from venue fills. Review p50/p99/p999 by phase (decision→write,
   write→ACK, ACK→fill) and safety counters before increasing scope.
5. **EV2-14 — private production promotion:** only after a reviewed soak,
   independent exposure audit, demonstrated operator pause/recovery, Grok
   handoff, runbook, and explicit separate approval. Promotion is not the
   same as starting the 30-coin canary; retain rollback and no-new-open mode.

## Stop / recovery rules

Immediately inhibit *new opens* for unknown/unilateral exposure, any private
channel not fully authenticated/reseed/matched, stale status or lost trade
socket/lease, uncorrelated late fill, WAL or trade-history fsync failure,
duplicate ID, reconciliation mismatch, risk-cap breach, latency gate alert,
or collector/`would_sent` regression. A disconnect alone need not reset a
long observation window if recovery is prompt and the event gap is reconciled;
it *does* revoke readiness while unresolved. Never assume flat on timeout,
cancel request, dual ACK, process exit, or service auto-stop. Preserve logs,
WAL, IDs, and recovery access. Operator review chooses flatten/cancel/restart
from independently verified exchange state.

## VPS / storage validation and success criteria

Local edit/test site: this Git worktree. Execution site for canaries: isolated
release on the target VPS. Initial durable write: dedicated local data root
with fsynced WAL and trade history; report/export storage must not be treated
as execution authority. Logs: dedicated systemd journal and release log root.
An exact release manifest records these paths before deployment.

For each stage save the release SHA, config hash, account fingerprint (no
secrets), unit start/restart history, run ID, 30-coin universe hash, WAL
sequence, intent/order/fill IDs, reconciled per-leg quantities, private
channel gaps and recovery times, parity, phase-separated latency, and an
independent flatness snapshot. The gate passes only when no unexplained
order/exposure remains, no safety counter or data gap is waived, and
collector/`would_sent` remain healthy. A sub-millisecond shadow/pre-send
measurement is not proof of exchange order or fill latency.

## Immediate next step

Write and review the EV2-12A task packet and tests for ACK-versus-fill
semantics and unresolved synthetic CLOSE. Do not start EV2-13 or arm
`LIVE_ORDERS` until EV2-12A–D are implemented and reviewed.

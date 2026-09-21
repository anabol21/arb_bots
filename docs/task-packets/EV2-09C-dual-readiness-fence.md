# EV2-09C task packet: dual-readiness lease and final send fence

Owner: Codex/operator

## Goal

Prevent a normal two-venue OPEN or CLOSE from scheduling either order when any
Bybit/OKX trade socket or authenticated private stream is not ready, and prevent
a readiness snapshot from surviving a disconnect/reconnect generation change.

## Pipeline

```text
venue status publishers
        |
        v
DualReadinessFence(snapshot, revision, connectivity_revision)
        |
        +-- admission lease
        |
        +-- final pre-send validation after frame finalization
                    |
                    v
          schedule Bybit + OKX writes without an intervening await
```

## Invariants

1. OPEN requires trade-ready and private-ready on both venues, plus pause and
   kill-switch clear.
2. Normal CLOSE requires the four connectivity signals but ignores pause and
   kill-switch changes.
3. Any connectivity boolean or venue generation change invalidates every old
   lease, including a false→true reconnect cycle.
4. Identical health refreshes do not churn lease revisions.
5. A final guard runs after both frames are finalized and immediately before
   either socket task is scheduled.
6. If the final guard fails or raises, both legs remain `not_attempted`.
7. A blocked normal CLOSE commits recovery-required state and performs zero
   dual-leg writes. Later reconciliation/reduce-only recovery is a separate,
   single-venue path.
8. `REQUEST_SENT.stream_generation` comes from the admitted lease, not from a
   newer readiness snapshot observed after dispatch.

## Files

- `app/bot/execution/engine.py` — readiness snapshot, fence, lease and normal
  lifecycle behavior;
- `app/bot/execution/transport.py` — last-moment same-loop guard;
- `tests/test_execution_engine.py` — generation, OPEN/CLOSE and in-flight
  publication cases;
- `tests/test_execution_transport.py` — zero-write final-guard cases.

## Exclusions

This patch does not create a live trade socket, wire venue runtime callbacks,
deploy to the VPS, restart a service or send an order. Venue runtime wiring and
controlled disconnect injection belong to EV2-09D.

## Gate

- scoped engine/transport/recovery suites pass;
- the full execution-v2 suite passes;
- disconnect during frame finalization produces zero writes;
- private-down normal CLOSE produces recovery-required state and zero writes;
- readiness publication is not starved by an in-flight submit;
- a generation update during in-flight writes cannot relabel request evidence.

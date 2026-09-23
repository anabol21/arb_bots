# EV2-12B3c — read-only companion readiness handoff

Status: local code and hermetic tests only. No VPS deployment, runtime audit
adapter, service change, order send, or two-hour canary in this patch.

## Pipeline block

Bybit/OKX private read-only processes → separate atomic status files →
`snapshot_from_readonly_companions()` → future no-order `audit_intent()`.
The snapshot always has both trade-socket flags false; it is never an
authorization for a live order.

## Contract and failure modes

Each opted-in `--ws-readonly` process may receive its own
`--status-path=<dedicated-data-root>/<exchange>.json`. It publishes unready
before connecting, ready only after auth, subscription and matched REST
reseed, refreshes while receiving, and revokes readiness on exit. Files are
atomically replaced and fsynced. The reader requires a fresh monotonic
timestamp, matching Linux boot ID and process start ticks, plus the writer's
private-ready/no-trade/no-orders assertion. Missing, malformed, stale, mismatched,
symlinked and dead-process files fail closed.

The consumer **must resample immediately before each no-order audit**.
Freshness cannot prove instantaneous socket liveness during a blocked receive;
the configured maximum age must account for the companion receive timeout,
and disconnect/reseed fault tests must measure this detection window. The
status is not a substitute for same-loop readiness on the eventual live
execution path.

## Next gate

Wire the two-file sampler into a separately gated no-order runtime adapter,
including policy intent mapping, metadata/plan cache, WAL drain/fsync and
replay evidence. Exercise disconnect, process death, reconnect and restart
locally. Only then review an isolated 30-coin, two-hour target-VPS canary;
keep `LIVE_ORDERS=0`, `BBOT_BROKER=stub` and the live-start block intact.

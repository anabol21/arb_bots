# EV2-12B3a — no-order prewrite audit seam

Status: local transport primitive and tests only. No VPS changes, network
connections, order sends, trade socket, K=1 publication or live-start unlock.
The deployable runtime still raises `ev2_live_execution_adapter_not_integrated`.

## Pipeline block and existing modules

```text
prepared two-leg intent -> owner-loop / metadata-freshness checks
  -> same frame finalizer as dispatch -> last readiness guard
  -> prewrite-only audit result; no asend tasks, ACK or fill
```

`app/bot/execution/transport.py` now factors the synchronous prewrite section
out of `ExecutionTransport.dispatch`. `audit_prewrite` invokes that same
section but requires both sockets to be `NoOrderTradeSocket` sentinels and a
callable final guard. The sentinels own no host, credentials, connection or
trade channel; an accidental `asend` raises and increments an attempt
counter. The audit rejects any prior attempt. It returns a distinct
`PrewriteAuditResult`, never a `DispatchResult` or `REQUEST_SENT` event.
The public result contains sizes and `signal_to_prewrite_ns`, never the
finalized text, signature or a claimed signal-to-*write* latency.

## Design choice and failure modes

The existing `NullTradeSink` deliberately accepts in-memory writes; that
is useful for shadow transport measurements but cannot establish an
order-free final write boundary. Here the transport's finalizer and guard
are shared without scheduling either write task. A real socket cannot be
passed to `audit_prewrite`; missing/failed readiness, stale metadata,
finalizer failure, clock regression or sentinel write attempts fail closed.

This patch does **not** prove that the live policy, WAL, ownership lease,
manager and private channels use this path. The injected guard can be a
test stub, and the local unsigned finalizer is not a production signer.
The next integration must bind the guard to the owned warm-session readiness
lease and use the reviewed production finalizer. No-order diagnostics must
never synthesize ACK/fill, OPEN/FLAT, or release an uncertain live K=1 slot.

## Validation and next experiment

Tests verify zero `asend` calls, no exposure/dispatch result, finalizer and
guard execution, stale/readiness/finalizer/clock failures, refusal of real
sockets, and an accidental dispatch attempt staying network-incapable.
Existing transport tests must still pass unchanged. Run everything locally;
no target-VPS evidence is claimed.

Next: wire this seam behind a dedicated no-order runtime mode through the
same policy intent, risk gate, durable WAL and readiness lease used before
live dispatch. Then test restart/disconnect at the prewrite boundary and
review an isolated EV2-13A canary. Only a later explicitly approved real
round trip can test ACKs, actual fills and confirmed flatness.

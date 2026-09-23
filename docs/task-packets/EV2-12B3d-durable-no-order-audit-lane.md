# EV2-12B3d — durable no-order audit lane

Status: local primitive and hermetic tests. Not connected to the bot runtime;
no VPS deployment, service change, trade socket or order authorization.

The exclusive `NoOrderAuditLane` takes an already-built policy `TradeIntent`
and a fresh no-order `ReadinessSnapshot` source. For each attempt it samples
both private companions, rejects any trade-ready source, calls
`ExecutionEngine.audit_intent()`, drains/fsyncs the WAL off the event loop,
then replays and verifies the exact accepted/rejected audit pair. An audit
passes only when prewrite, fsync and replay all pass. WAL/replay uncertainty
latches the lane shut; no attempt can become a success from queue admission
alone. No `REQUEST_SENT` may exist in the replay.

Remaining runtime bridge: map the frozen Gear 2.2 policy decision to this
lane's intent, construct a fresh 30-coin instrument/plan cache from reviewed
metadata (including OKX `instIdCode`), acquire an isolated ownership fence,
and define startup WAL/reconciliation and status-file paths. Then exercise
private disconnect/reseed, process death, restart and WAL-failure fixtures.
The target-VPS two-hour no-order canary starts only after that bridge is
reviewed and proven to use the real runtime path. Live orders remain blocked.

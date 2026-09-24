# EV2-13C3: durable private evidence ingestion (not armed)

Pipeline: already adapted private order/fill batch → EV2 FSM fold → WAL enqueue
→ exact-tail fsync proof under one engine lock. The new
`ingest_adapter_batch_durable` entrypoint is separate from the existing
asynchronous/no-order ingest API and does not itself receive WS frames.

Adapter issues, transition/capacity failure or uncertain WAL proof latch an
in-process kill switch. Later warm-session readiness notifications cannot
clear it. A new engine process must replay WAL and reconcile both venues
before opening again. The in-memory state may be ahead of durable state on a
writer failure; it is intentionally not treated as position proof.

Validation: acceptance proves the fill as the durable WAL tail before return;
writer fault leaves no new OPEN permission; readiness republish cannot clear
the latch. The 449 execution tests pass locally.

This patch does **not** grant live order capability. Remaining integration:
private frame decoding/adaptation, intent registration across OPEN and CLOSE,
signed REST exposure reconciliation, owner-loop wiring, experiment budget and
no-order production-path canary. The runtime live guard remains active.

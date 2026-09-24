# EV2-13C2: owner-loop private frame fanout (not armed)

Pipeline: warm private WS listen → legacy private parser → optional EV2
observer(raw frame, receive monotonic ns) → future `PrivateEventAdapter` and
`ExecutionEngine.ingest_adapter_batch`. The new observer hook does not bind
itself, start a socket or grant send permission. Trade ACK frames stay on the
existing trade channel, separate from private order/fill observations.

The hook runs on the sole warm socket owner loop after the legacy parser. If
it fails, `_pump` raises into the existing disconnect/reconnect path; it must
not silently discard private fill evidence. The reconnect path blocks sends
until auth, subscriptions and REST reseed are matched again.

This is a transport seam, not a fill-driven live adapter. Remaining work:
register durable EV2 intent/leg IDs, decode and adapt private order/execution
frames (including multi-row frames), persist resulting EV2 events, reconcile
positions and open orders, and prove the six-submission budget across restart.
Until then, the runtime live guard remains active and no orders are sent.

Validation: hermetic observer-order/clock/failure tests; warm-session tests.
Production validation must use an isolated no-order run before any live arm.

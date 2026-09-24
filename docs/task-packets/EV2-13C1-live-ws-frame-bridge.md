# EV2-13C1: live trade-WS frame bridge (not armed)

Pipeline: frozen EV2 `LegPlan` → `FrozenStaticFrame` → late Bybit/OKX trade-WS
message finalizer → (future) warm owner-loop `asend`. This patch adds only the
middle conversion. It does not bind sockets, remove the runtime live guard,
send orders, or interpret ACK as fill.

Existing code reused: `app/bot/execution/transport.py` freezes plan fields and
timestamps local `asend`; `app/bot/private/ws_messages.py` constructs the
Bybit signed `order.create` and OKX authenticated-socket `order` formats.
`app/bot/execution/live_ws_frames.py` now adapts EV2 frozen fields to those
builders without constructing a legacy `OrderPlan` or its approval lease.

Risk decisions:

- Only market frames are exposed by the EV2 finalizer. Reduce-only is copied
  exactly from the durable EV2 plan, never inferred from ACK or local state.
- Bybit auth material remains in memory and in the outbound frame only;
  `repr(finalizer)` redacts it. Never log the returned raw text.
- OKX `instIdCode` and venue-safe client/request ID must be present. The frame
  omits `posSide`, so a future live arm must independently prove OKX net mode.
- A valid frame is **not** permission to send. The existing
  `ev2_live_execution_adapter_not_integrated` guard remains in force.

Validation: hermetic EV2 frame tests, 447 execution tests, W2 frame tests and
62 W4–W7 tests passed locally. No target-VPS release or live WS check yet.

Next: connect the four warm sockets and private event stream to the EV2 owner
loop, add fill/REST reconciliation and durable experiment budget, then run an
isolated no-order production-path canary. Keep the live guard until those
steps pass end-to-end.

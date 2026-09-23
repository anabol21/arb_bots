# EV2-11 — Restart-safe trade state recovery

## Pipeline block

```text
theta lifecycle decision
  -> venue ACK (live) / synthetic fill (no-order)
  -> append + fsync theta_trades JSONL
  -> publish K=1 slot state in memory

process restart
  -> strict replay of all theta_trades partitions
  -> restore K=1 slot and EV2 shadow FSM/context
  -> live only: start authenticated private session
  -> signed read-only REST: both positions + both open-order sets
  -> exact journal/exchange match
  -> rewrite durable local broker cache
  -> enable live decisions
```

## Existing files/modules involved

- `app/bot/theta_trade_manager.py`: authoritative append-only trade lifecycle and K=1 slot.
- `app/bot/private/position_reconcile.py`: signed read-only restart reconciliation.
- `app/bot/stub_broker.py`: atomic local position cache.
- `app/bot/execution/shadow_runtime.py`: restored shadow FSM and open-trade context.
- `app/bot/runtime.py`: startup ordering and fail-closed live gate.

## Candidate designs considered

1. Restore from `position.json` only. Small, but the mutable cache can diverge from the
   append-only trade history and from exchange exposure.
2. Replay the trade history only. Correct for synthetic/no-order restarts, but cannot
   prove live exchange state after a crash between venue ACK and journal commit.
3. Replay history, then reconcile both exchanges before live decisions. Selected: it
   keeps the journal as the expected-state source and treats the venues as the live
   exposure proof.

## Key risks and failure modes

- A torn, malformed, overlapping, orphaned, or policy-incompatible history fails startup.
- A crash after exchange ACK but before trade-history fsync creates an exchange/journal
  mismatch. EV2-11 blocks; it does not guess or automatically flatten.
- Any pool open order, REST error, pagination ambiguity, wrong coin, wrong side, or extra
  position prevents live decisions.
- A pending local broker intent prevents cache repair.
- Private WebSocket readiness remains a separate gate; REST reconciliation does not
  replace the two-venue connection-health gate on each send.

## Minimal patch / experiment

- Replay committed open/close rows into one K=1 slot.
- Fsync lifecycle rows before publishing the new in-memory state.
- Atomically persist and directory-fsync the local broker position cache.
- Seed the EV2 shadow FSM/context with a restored open position.
- Require signed four-query reconciliation before enabling live manager decisions.

## VPS/storage validation plan

1. Deploy to a new release directory; do not mutate the EV2-10 evidence directory.
2. Run no-order restart tests with a deliberately open synthetic lifecycle and confirm
   the same `trade_id`, coin, and side are restored and later closed.
3. Run live read-only reconciliation with `LIVE_ORDERS=0` as a diagnostic harness for
   flat and controlled mismatch fixtures.
4. Before any real-order canary, prove that a mismatch and an inconclusive response both
   stop before public signal tasks start.
5. During the real-order restart experiment, use one small isolated position and verify
   journal, both venue legs, local cache, and restored shadow state agree.

## Success criteria

- Restart with a committed open lifecycle restores the same trade identity.
- Restart after a committed close restores FLAT.
- No live decision can run before reconciliation is confirmed.
- Both exchange positions match the expected inverse legs on the same coin.
- Both exchange open-order sets are empty.
- All malformed or ambiguous states fail closed without an order send.

## Recommended next step

Deploy EV2-11 as a no-order restart canary first. Then run a separate, explicitly armed
small real-order restart experiment. Keep auto-repair/auto-flatten out of scope until the
ambiguous ACK-to-fsync crash case is covered by the execution WAL protocol.

## VPS canary result — 2026-09-23

- Release commit: `8579dd0`; release path: `/root/spread_ev2_11`.
- Isolated data: `/data/bbot-ev2-11-restart`; dedicated main and two private read-only units.
- First start: 09:47:09 UTC. Planned restart: 09:50:02 UTC. Current shadow run:
  `shadow_3c09db2c0b49458ea943c8542be2243e`.
- Before restart, the journal committed a synthetic `OPEN` for RVN short with
  `trade_id=a35759e7-b84b-4e5e-9e0a-2e886c4415a9`, `send=false`.
- After restart, manager replay and shadow FSM restored the same trade; the
  second process committed its `CLOSE` with that same trade ID at 09:51:59 UTC.
  Shadow mirror transitioned to FLAT.
- The second run ended at exactly 11:50:02 UTC via `RuntimeMaxSec=7200`.
  `systemd` reports `Result=timeout` as expected for that bounded stop and
  `ExecMainStatus=0`; the private companions stopped with it. Wall time from
  first start to final stop was 2 h 2 m 53 s, including the planned restart.
- The history contains 37 synthetic OPEN and 36 CLOSE rows; no skip audit
  rows were recorded. Slot-busy holds are not individually journaled. All
  lifecycle rows have `would_send=true`, `send=false`; `orders_sent=0` and
  `trade_socket_bound=false` throughout the shadow reports.
- Shutdown occurred while one synthetic position remained open: KAITO short,
  `trade_id=65dee06a-4261-4978-8c37-bb818d56fc8f`. Strict replay after stop
  reconstructed that same open position; the manager did not invent a close.
- The second shadow run emitted 6,467 parity ticks, 72 mirror transitions,
  zero divergences, zero lifecycle/journal drops, and zero unknown states. Its
  final FSM state was OPEN, matching the journal. Its target-VPS latency
  report had 10,072 valid samples, passed p50/p99 with no p999 alert, and
  recorded zero invalid clocks, missing samples, or rejected-before-write.
- `spread-collector-next.service` and the continuing
  `spread-bbot-theta-k1-canary.service` stayed active with zero restarts at
  the final check. The EV2-11 main/private units had no unplanned restarts.
- Both private companions recorded auth success and matched REST reconciliation
  on both starts. OKX also recorded two known misclassified auth failure frames
  per start (four total); track any increase as a new anomaly.
- Important simulation limit: 15 of 36 virtual CLOSE rows had
  `fill_size_ok=false` (14 also had `signal_size_ok=false`). The manager still
  marked those synthetic positions closed. This does not invalidate the
  restart-recovery proof, but it prevents treating every synthetic close as
  evidence of a fillable real exit. A live exit must remain exposure-aware
  until venue confirmation; do not infer flatness from this fill model.
- Verdict: EV2-11 no-order restart recovery passed. Real-order restart safety
  and exit execution remain separate gates for the subsequent live ladder.

# EV2-12A2 — fill-authoritative EV2 → K=1 publication contract

Status: pure contract and tests, no runtime wiring or live order capability.
The deployable live route remains blocked by EV2-12A1. This patch does **not**
claim EV2-12 is canary-ready.

## Pipeline block

```text
EV2 event state (ACK / partial / fill / position / reconciliation)
  -> assert EV2 FSM invariants
  -> manager projection: none | proven OPEN | proven CLOSE
  -> credential-free per-leg journal evidence
  -> [EV2-12B: WAL + fsync lifecycle + publish K=1 slot]
```

## Existing files/modules involved

- `app/bot/execution/contracts.py`: `SpreadState`, `LegState`, client IDs,
  filled/observed quantities.
- `app/bot/execution/state_machine.py`: fill-authoritative OPEN and proven
  FLAT invariants. Dual ACK and partial inventory cannot satisfy them.
- `app/bot/execution/manager_projection.py`: new pure proposed-publication
  contract. It does not mutate the manager, journal, or broker.
- `app/bot/theta_trade_manager.py`: future durable K=1 publication consumer;
  deliberately unchanged in this patch.
- `tests/test_execution_manager_projection.py`: event-sequence tests using
  the same FSM fixtures as the established execution test suite.

## Candidate designs considered

1. Let manager infer OPEN/FLAT from two trade ACKs: rejected; acceptance is
   not execution and the legacy broker already demonstrates this hazard.
2. Duplicate venue fill logic inside the manager: rejected; two competing
   exposure authorities would make restart reconciliation ambiguous.
3. Project only *proven* EV2 FSM states to a proposed K=1 lifecycle row,
   carrying both legs' stable IDs, fills and observed positions. Selected.

## Risks and failure modes

- The projection is a proposal, not a durable transition. Calling code must
  persist it through the EV2 WAL/trade journal before publishing K=1 state.
- `IDLE` is not interpreted as exchange-confirmed flatness; startup signed
  read-only reconciliation remains required.
- `CLOSING`, partial fill, cancel, ACK-only and unknown exposure hold K=1;
  no close row or fresh open can be inferred from them.
- A manager trade ID different from `state.open_intent_id` raises instead of
  silently replacing the position.
- Exact account ownership, lot/contract conversion and private-stream
  freshness are still separate EV2-12B/C gates. This contract does not
  authorize order sends.

## Minimal patch / experiment

Given an immutable `SpreadState` and the committed journal trade ID, compute
one of `none/open/close`. For a proposed OPEN, include each venue's client ID,
leg ID, filled quantity, observed position and effective open quantity. For
a proposed CLOSE, require EV2-proven flatness and keep the slot held until
the durable close row is written. Test state serialization/replay, dual ACK,
partial fill + cancel, mismatched IDs, proven OPEN and proven FLAT.
An unobserved venue position is exported as `null`, never as an invented zero.

## VPS/storage validation plan

No VPS deployment for this pure patch. EV2-12B will run fault-injection
around WAL append, both order writes, private events, signed REST and manager
fsync on a new isolated data root. The first target-VPS run remains no-order;
collector and continuing `would_sent` are untouched.

## Success criteria and next step

- ACK-only and partial/recovering states propose no lifecycle row.
- OPEN publication contains two positive proven leg quantities and IDs.
- CLOSE publication requires independently proven zero exposure and open
  orders, with no K=1 release before journal commit.
- Wrong trade identity fails closed; pure module has no network or file I/O.

Next: EV2-12B wires this contract into a durable manager adapter and removes
the runtime live-start block **only after** the WAL/private-event/restart
matrix and separate review pass. EV2-12C strengthens signed two-venue
quantity/account reconciliation.

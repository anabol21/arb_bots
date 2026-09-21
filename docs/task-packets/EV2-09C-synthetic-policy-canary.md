# EV2-09C task packet: reproducible synthetic-signal canary

Owner: Codex/operator

## Goal

Exercise the 30-coin Gear 2.2 trade manager, K=1 occupancy, strategy bridge,
execution-v2 shadow FSM and latency path without waiting for rare alpha
signals and without exposing an order surface.

## Policy

`BBOT_POLICY_MODE=synthetic_roll_v1` selects one deterministic 1..100 roll
per UTC second for the entire contour. The coin is deliberately absent from
the roll key, so the configured top-30 order and K=1 slot choose at most one
candidate in a second.

- roll `17` while flat: open the first complete/size-eligible coin;
- roll `32` while open: close the held coin;
- all other rolls: hold;
- open direction is deterministically split long/short to exercise both leg
  mappings;
- `BBOT_SYNTHETIC_ROLL_SEED` is recorded so the run is exactly replayable.

The synthetic policy replaces only alpha eligibility. Book completeness,
coin ordering, size checks, pending/slot behavior, fill modelling, strategy
bridge, FSM and transport gates remain unchanged.

## Safety boundary

The mode fails closed unless all of the following are true:

- `BBOT_BROKER=stub`;
- `LIVE_ORDERS=0`;
- `BBOT_THETA_LIVE_SEND=0`;
- profile is not `gear22_live_canary`;
- EV2 shadow transport remains `NullTradeSink` and no trade websocket exists.

The frozen `gear22_frozen_v1` path is still the default. The synthetic mode is
never selected by a missing or unknown environment value.

## Evidence gates

- the same second and seed produce the same roll in live observation and
  replay;
- one roll is shared by all 30 coins;
- `17` produces one K=1 OPEN and `32` produces one CLOSE when data and size are
  eligible;
- legacy manager and execution-v2 bridge remain exact parity;
- journal and intent rows are labelled `gear22_synthetic_roll_v1`;
- zero real order sends and no trade websocket binding;
- later readiness-fence fault injection proves disconnect-before-dispatch
  produces zero writes.

## Deployment note

This patch prepares the policy and no-order shadow wiring only. It does not
modify or restart the active target-VPS EV2-09B run. A separate experiment
unit/data root must be reviewed after EV2-09B evidence is closed.

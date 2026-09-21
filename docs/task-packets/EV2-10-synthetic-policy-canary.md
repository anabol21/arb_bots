# EV2-10 task packet: reproducible synthetic-signal readiness canary

Owner: Codex/operator

## Goal

Exercise the 30-coin Gear 2.2 trade manager, K=1 occupancy, strategy bridge,
execution-v2 FSM and the EV2-09C/09D dual-readiness path without waiting for
rare alpha signals and without exposing an order surface.

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
bridge, FSM, readiness fence and transport gates remain unchanged.

## Safety boundary

The mode fails closed unless all of the following are true:

- `BBOT_BROKER=stub`;
- `LIVE_ORDERS=0`;
- `BBOT_THETA_LIVE_SEND=0`;
- profile is not `gear22_live_canary`;
- EV2 shadow transport remains `NullTradeSink` and no trade websocket exists.

The frozen `gear22_frozen_v1` path is still the default. The synthetic mode is
never selected by a missing or unknown environment value.

## Initial experiment

Run a separate target-VPS unit for two hours with its own run id, data root and
log root. Keep the collector, the current `would_sent` contour and completed
EV2-09 evidence untouched. Inject controlled private disconnect/reconnect
events only after the readiness publisher and fault matrix are green.

## Evidence gates

- the same second and seed produce the same roll in live observation and
  replay;
- one roll is shared by all 30 coins;
- `17` produces one K=1 OPEN and `32` produces one CLOSE when data and size are
  eligible;
- legacy manager and execution-v2 bridge remain exact parity;
- journal and intent rows are labelled `gear22_synthetic_roll_v1`;
- zero real order sends and no trade websocket binding;
- disconnect-before-dispatch produces zero writes and invalidates the old
  readiness lease;
- reconnect does not reopen the gate until auth/subscription/reseed is proven
  for both venues in their current generations;
- disconnect after send start is classified as uncertain and enters bounded
  reconciliation rather than attempting a blind second lifecycle action.

## Deployment note

The bounded deployment uses:

- `spread-bbot-ev2-10-synthetic.service`;
- `spread-bbot-ev2-10-private@bybit.service`;
- `spread-bbot-ev2-10-private@okx.service`;
- `/root/spread_ev2_10` at the reviewed EV2 commit;
- `/data/bbot-ev2-10-synthetic` and dedicated log files;
- seed `7`, `RuntimeMaxSec=7200`, `Restart=no` for the main unit.

The two private companions remain authenticated read-only processes and expose
no trade socket. Consequently, the two-hour VPS run is evidence for synthetic
manager/parity/shadow-FSM behavior and private reconnect observation. The
EV2-09D final-send readiness fence is covered by its deterministic fault matrix,
not by a real trade-WebSocket send in this no-order deployment.

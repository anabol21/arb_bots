# EV2-09B task packet: target-VPS no-order shadow

Owner: Codex/operator

## Goal

Run the frozen Gear 2.2 30-coin public pipeline and execution-v2 parity/latency
observer on the production VPS for at least 20 continuous hours. Authenticated
private order/position streams are connected read-only. Real order submission
must be structurally unavailable.

## Safety boundary

- main runtime: `BBOT_BROKER=stub`, `BBOT_THETA_LIVE_SEND=0`,
  `LIVE_ORDERS=0`;
- execution-v2 transport terminates only in `NullTradeSink`;
- private companions use `app.bot.private --ws-readonly` and create no trade
  websocket;
- no unit may read or write collector, legacy `would_sent`, or live-canary
  data roots;
- the existing collector and `spread-bbot-theta-k1-canary.service` are not
  restarted or modified.

## Qualification split

1. Target-VPS soak: at least 20 continuous hours. A quiet day with no eligible
   signal is valid stability evidence but not signal-path coverage.
2. Live parity: compare every decision tick and every eligible signal observed
   during the soak.
3. Historical replay: at least 300 eligible signals with exact decision parity.
4. Target-loop transport probe: 100 warm-up plus 10,000 counted no-order
   dispatches.

## Gates

- zero order sends and no trade websocket binding;
- zero unclassified decision divergence;
- zero lifecycle/journal drops and unknown state;
- private auth, subscribe, heartbeat/reconnect and REST reseed remain healthy;
- collector heartbeat/restarts/load do not regress;
- signal-to-first-write `p50 <= 1 ms`, `p99 <= 3 ms`, with `p99.9 > 10 ms`
  treated as an alert;
- exact canonical top-30 order, including `ICX`.

## Runtime artifacts

- `/data/bbot-ev2-shadow/execution-v2-shadow.jsonl`;
- `/data/bbot-ev2-shadow/private-bybit/`;
- `/data/bbot-ev2-shadow/private-okx/`;
- `/var/log/spread/bbot-ev2-shadow.log`;
- `/var/log/spread/bbot-ev2-private-{bybit,okx}.log`.

This task does not authorize any live order or modification of the active
legacy `would_sent` unit.

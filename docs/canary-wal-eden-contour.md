# WAL/EDEN canary contour (Track 3)

Glue / B-private. Isolated from D, from `/data/bbot`, and from the Gear-2
would_send stub (`/data/bbot-gear2`). **Not enabled or started on the VPS
by this change.** Gear2 stays stopped.

Approved shape (Mikhail): coins **WAL, EDEN**; **$10 USDT per leg** matched
dual; decision strategy = `gear2_would_send` / `gear2_market_manager` +
`trade_manager` gates; **k=1**; variation **thresh_open/close long/short = 0.1**;
**open_frac=close_frac=0.7**; **avg_window_sec=10**. Send is **Contour B**
(`LiveBroker.default_live_send_pair` → queue→`ws.send`). W6 is not on the
hot path.

This is not a profitability claim and not a Gear-2 close stamp.

## Profile contract

| Field | Canary | Gear2 would_send (unchanged) |
|-------|--------|------------------------------|
| `BBOT_PROFILE` | `canary_wal_eden` (alias `canary`) | `gear2_would_send` |
| Coins | WAL, EDEN | BTC, ETH, SOL, XRP (stub); LIVE_SIZE SOL, XRP |
| Thresholds | all four `0.1` (strict `>`) | all four `0.02` |
| `open_frac` / `close_frac` | 0.7 / 0.7 | 0.7 / 0.7 |
| `avg_window_sec` | 10 | 10 |
| `k` | 1 | 1 |
| Notional | 10 USDT / leg | 100 default (stub) |
| Arm | A (Top-N ignored) | A |
| L1 depth gate | **on**, pre-signal | off (`Check_l1_depth=False`) |
| Broker | `private_live` → Contour B | stub `would_send` |

Constants: `CANARY_WAL_EDEN_*` and `LIVE_SIZE_COINS` in
[`app/policy/trade_manager.py`](../app/policy/trade_manager.py).
Private LIVE_SIZE / canary symbols:
`resolve_live_size_futures_symbol` in
[`app/bot/private/order_symbols.py`](../app/bot/private/order_symbols.py)
(W6 BTC/TRUMP allowlist is unchanged).

## Depth gate is a model/policy gate

L1 depth is another **decide-time** gate, same class as thresh / frac /
`avg_window`. It runs inside `_pass_gates` **before** an open/close intent
is emitted.

- Planned coin qty per venue = `notional / execution_price` (buy→ask, sell→bid).
  Books already reach policy on `TickView` (`*_bid_size` / `*_ask_size` and
  prices). The missing input was planned qty; it uses HYPER `position_size`
  (runtime overlays `BBOT_NOTIONAL_USDT`).
- If execution L1 size on **either** venue is missing, non-finite, or `<`
  planned qty → hold/skip, reason `gate_l1_depth`.
- **Not** placed between signal and Contour B send.
- **Not** owned by a post-intent `app.bot.sizing` helper. Pure helpers
  (`planned_coin_qty`, `l1_depth_covers_planned`) live in the policy module.

## Isolation

| | Canary | Gear2 | D |
|--|--------|-------|---|
| Unit (template only) | `spread-bbot-canary-wal-eden.service` | `spread-bbot-gear2.service` | `spread-collector` |
| Data | `/data/bbot-canary-wal-eden` | `/data/bbot-gear2` | `/data/live`, `/data/bars`, … |
| Log | `/var/log/spread/bbot-canary-wal-eden.log` | `/var/log/spread/bbot-gear2.log` | `runtime.log` |
| Secrets | `/etc/spread/bbot-canary-wal-eden.env` (mode 600, not in git) | stub / none | — |

Do not write D trees. Do not start/stop collector. Do not start gear2.
Do not `systemctl enable` this unit from the repo file alone.

## How to run later (operator; not done here)

Create `/data/bbot-canary-wal-eden` and the secret env file on the VPS, then
start **only** the canary unit after an explicit live go. Example process env:

```text
BBOT_MODE=policy
BBOT_PROFILE=canary_wal_eden
BBOT_COINS=WAL,EDEN
BBOT_NOTIONAL_USDT=10
BBOT_DATA_ROOT=/data/bbot-canary-wal-eden
BBOT_LOG_PATH=/var/log/spread/bbot-canary-wal-eden.log
BBOT_BROKER=private_live
VENUE=live
LIVE_ORDERS=1
# BBOT_PRIVATE_SEND_PATH unset or trivial  → Contour B
# BBOT_OKX_INST_ID_CODES=WAL-USDT-SWAP:…,EDEN-USDT-SWAP:…
# keys only in the env file
```

Confirm `spread-bbot-gear2.service` is inactive before any canary start.

Local tests:

```bash
PYTHONPATH=. python3 -m unittest tests.test_canary_wal_eden tests.test_bbot_gear2 -v
```

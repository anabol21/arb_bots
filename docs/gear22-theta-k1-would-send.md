# Gear 2.2 θ K=1 would_send contour

**Track:** 3 Glue / B-bot. **Gear:** 2.2 observation → would_send (NO real orders).  
**Status:** single BotRuntime profile stacked on live floor + TW-p50 + theta. Not private send.

Code:

- [`app/bot/theta_trade_manager.py`](../app/bot/theta_trade_manager.py) — K=1 decide + fill model + journal
- [`app/bot/floor_warm.py`](../app/bot/floor_warm.py) — warm pickle (standard for this profile)
- [`app/bot/theta_trade_plot.py`](../app/bot/theta_trade_plot.py) — offline PNG CLI
- Wired from [`app/bot/runtime.py`](../app/bot/runtime.py)

Depends on: [`docs/gear22-live-floor-watcher.md`](gear22-live-floor-watcher.md), [`docs/gear22-tw-p50-watcher.md`](gear22-tw-p50-watcher.md), [`docs/gear22-theta-screener.md`](gear22-theta-screener.md).

## Design (locked)

One process / one canary unit journals **all** of:

| Stream | Path |
|--------|------|
| floor | `{BBOT_DATA_ROOT}/floor/event_date=…/metrics.jsonl` |
| tw_p50 | `{BBOT_DATA_ROOT}/tw_p50/…/metrics.jsonl` |
| theta | `{BBOT_DATA_ROOT}/theta/…/metrics.jsonl` |
| **theta_trades** | `{BBOT_DATA_ROOT}/theta_trades/…/trades.jsonl` |

No second observer process. No Contour B / collector / WAL-EDEN / private path changes.

### Strategy K=1

- **Global** single slot across all coins (not per-coin).
- **Entry:** `theta_1m(side) ≥ θ_thr` (default **0.2**), slot free, book size OK.
- **Exit:** opposite side `theta_1m ≥ θ_thr` (same 0.2). No hysteresis / min-hold in v1.
- While in position: ignore new entries; log `slot_busy` skips.

### Fill model

- `signal_ts` = decision time (θ tick / emit).
- `fill_ts` = **`signal_ts + 70ms` exactly** (`BBOT_FILL_DELAY_MS=70`).
- Capture L1 (and configured depth) Bybit+OKX books at signal and again at fill.
- would_send prices from the legs that compose the arb spread for that side
  (`open_long`: buy OKX ask / sell Bybit bid; `open_short`: buy Bybit ask / sell OKX bid).

### Book size gate

- Notional (`BBOT_NOTIONAL_USDT`, default 100) must fit available size on chosen legs
  (L1 min; optional `BBOT_BOOK_DEPTH` for top-N when depth lists are present).
- Signal insufficient → skip, log `reject_reason=insufficient_size` + available sizes.
- Signal OK but fill size bad → still log would_fill with `fill_size_ok=false` (do not erase).

### Slip metric

On every fill-bearing open/close row:

```
slip_spread = signal_spread − fill_spread
```

- Spreads are the live edge % for the traded side (`compute_spreads`).
- **Positive = worse for us** (edge compressed between signal and fill).
- Equivalent to `-(fill − signal)` on the edge metric.
- `slip_leg_bps.okx` / `.bybit`: per-leg bps; buy pays up / sell fills down → positive.

### PnL (close only, would_send)

- `pnl_spread ≈ open_fill_spread − close_fill_spread` (pct points).
- `pnl_usdt_approx ≈ pnl_spread/100 * notional` (proxy only; not venue PnL).

## Schema `bbot.theta_trade.v1`

Path: `{BBOT_DATA_ROOT}/theta_trades/event_date=YYYY-MM-DD/trades.jsonl`  
(refuses D paths like other journals).

Minimum fields on open/close:

| Field | Notes |
|-------|--------|
| `schema_version` | `bbot.theta_trade.v1` |
| `trade_id` | UUID shared by open+close |
| `base_coin` / `side` | Uppercase; `long` \| `short` |
| `event` | `open` \| `close` (skips use `skip`) |
| `reason` | `theta_entry` \| `theta_exit_opposite` \| … |
| `signal_ts_ms` / `fill_ts_ms` / `latency_ms` | fill = signal + cfg delay |
| `theta_1m` / `theta_5m` / `floor` / `p50_1m` / `p50_5m` / `opposite_theta_1m` | from emit |
| `leg_buy_ex` / `leg_sell_ex` | okx \| bybit |
| `spread_signal` / `spread_fill` | edge % |
| `slip_spread` / `slip_leg_bps` | see above |
| size fields | `signal_size_ok`, `fill_size_ok`, available / planned qty |
| `notional_usdt` | |
| `book_signal` / `book_fill` | structured okx/bybit bid/ask + size |
| flat `signal_*` / `fill_*` bid/ask/size | convenience |
| close PnL | `pnl_spread`, `pnl_usdt_approx`, open timestamps |

No tick files / parquet WAL from this manager.

## Warm-start (required / standard)

Finite floor (hence finite θ) ASAP after start:

1. **Build pickle** (offline, B paths only):

```bash
# From a prior B floor journal (recommended after any long canary):
PYTHONPATH=. python -m app.bot.floor_warm \
  --from-journal /data/bbot-gear2 \
  --out /data/bbot-gear22/state/floor_warm.pkl \
  --coins BTC,ETH,SOL,...

# From compacted: the live unit cannot read /data/compacted
# (systemd InaccessiblePaths). On a host/copy that *can* read compacted:
#   1) run an offline floor oneshot → write floor metrics under a B data root
#   2) then --from-journal that root into floor_warm.pkl
#   3) copy the pickle onto the VPS under BBOT_DATA_ROOT/state/
```

2. **Runtime load** (default for `gear22_would_send`):

| Variable | Meaning |
|----------|---------|
| `BBOT_FLOOR_WARM_PATH` | Pickle path (default `{BBOT_DATA_ROOT}/state/floor_warm.pkl`) |
| `BBOT_FLOOR_WARM` | `1` force expect warm; `0` skip even if file exists for non-gear22 |

On stop, gear22 (or a previously loaded warm) re-exports the pickle for the next start.

API: `LiveFloorObserver.export_warm_state` / `apply_warm_state` / `seed_last_floor`.

## Env / profile

Prefer **`BBOT_PROFILE=gear22_would_send`** (alias `gear22`).  
`gear2_would_send` + `BBOT_THETA_TRADE=1` also works (tick-path policy is suppressed while theta trade is on).

| Variable | Default | Meaning |
|----------|---------|---------|
| `BBOT_THETA_TRADE` | on for gear22 | enable manager |
| `BBOT_THETA_THR` | `0.2` | entry/exit threshold |
| `BBOT_FILL_DELAY_MS` | `70` | fill_ts − signal_ts |
| `BBOT_SLOT_K` | `1` | global slots |
| `BBOT_NOTIONAL_USDT` | `100` | size gate + PnL proxy |
| `BBOT_BOOK_DEPTH` | `1` | L1; >1 uses depth lists if present |
| `BBOT_FLOOR_WATCH` | on | keep on |
| `BBOT_TW_P50_WATCH` | on | keep on |
| `BBOT_THETA_WATCH` | on | keep on |
| `BBOT_BROKER` | `stub` | stub only for canary |
| `BBOT_COINS` | **set to August-std HTML top30** | same as prior canary |
| `SENTRY_DSN` | *(unset)* | Sentry DSN; when set, emits trade events + errors |
| `SENTRY_ENVIRONMENT` | `gear22-would-send-canary` | Sentry environment label |
| `SENTRY_RELEASE` | *(unset)* | Optional release/version tag |

## Canary recipe (stub)

```bash
export BBOT_MODE=policy
export BBOT_PROFILE=gear22_would_send
export BBOT_BROKER=stub
export BBOT_COINS=<August-std HTML top30 CSV>
export BBOT_DATA_ROOT=/data/bbot-gear22
export BBOT_LOG_PATH=/var/log/spread/bbot-gear22.log
export BBOT_FLOOR_WATCH=1
export BBOT_TW_P50_WATCH=1
export BBOT_THETA_WATCH=1
export BBOT_THETA_TRADE=1
export BBOT_THETA_THR=0.2
export BBOT_FILL_DELAY_MS=70
export BBOT_SLOT_K=1
export BBOT_NOTIONAL_USDT=100
# Warm (standard):
# export BBOT_FLOOR_WARM_PATH=$BBOT_DATA_ROOT/state/floor_warm.pkl

# Sentry (optional; set DSN from Cursor Dashboard or systemd EnvironmentFile):
# export SENTRY_DSN=https://...@...sentry.io/...
# export SENTRY_ENVIRONMENT=gear22-would-send-canary

PYTHONPATH=. python -m app.bot
# Expect under $BBOT_DATA_ROOT:
#   floor/  tw_p50/  theta/  theta_trades/  state/floor_warm.pkl
```

PNG (offline):

```bash
PYTHONPATH=. python -m app.bot.theta_trade_plot \
  --data-root /data/bbot-gear22 \
  --trade-id <uuid> \
  --out /tmp/theta-trade.png
```

Unit tests:

```bash
python -m unittest tests.test_bbot_theta_trade_k1 -v
```

## Isolation

- Own tree under BBOT data root only.
- Never `/data/live`, `/data/bars`, `/data/compacted`, D backup prefixes.
- No private broker imports in theta trade / floor warm / plot modules.
- No collector / Contour B / WAL-EDEN edits in this contour.

## Sentry integration

**Enabled when `SENTRY_DSN` is set.** Emits:

- **Trade lifecycle events** (open, close, reject) as Sentry messages (level=warning).
  - Stable fingerprint: `["theta_k1", trade_id, event]` → duplicates collapse per trade step.
  - Tags: `contour=gear22_theta_k1`, `profile`, `event`, `coin`, `side`, `trade_id`.
  - Extras: signal/fill timestamps, spreads, theta, floor, PnL (close only), slip, size check results.
- **Uncaught exceptions** in policy / trade manager paths via `capture_exception`.

**No secrets in git.** DSN, org, project stay in environment only (systemd `EnvironmentFile`, Cursor Dashboard secrets, or VPS-local `.env`).

**Local/CI tests:** run with `SENTRY_DSN` unset (no network) or mock `sentry_sdk`.

**Example canary unit snippet:**

```ini
[Service]
Environment="SENTRY_DSN=https://...@...sentry.io/..."
Environment="SENTRY_ENVIRONMENT=gear22-would-send-canary"
```

SDK: official Python `sentry-sdk` (added to `requirements.txt`).

## Out of scope

- Real orders / private WS / live send.
- Hysteresis / min-hold / K>1.
- Changing floor / TW-p50 / theta formulas.
- Starting VPS processes from this agent.

# Gear 2.2 dummy backtest (policy + replay)

Pass-1 `decide()` over `FeatureSnapshot` rows lives in `policy.py` (the
manager). The run — hive I/O, `ts_s` clock, K=1 slots — lives beside it in
`replay.py`. Replay does **not** contain trading rules.

**Locked: open and close are separate.** Open does not look at profit. Close
does not reuse open p50. Optional close theta is **close-side** `theta_1m_*`,
not the open dummy.

**Locked side rule:** a **long trade** (open long **or** close short) uses
`*_long`. A **short trade** (open short **or** close long) uses `*_short`.
Closing a long is a short-side unwind (`theta_1m_short`, `spread_last_short`).
Closing a short uses long fields. `potential_pp` already uses
`spread_last_opposite` = that close-side spread.

`None` on an optional gate **disables** it. NaN on an **enabled** gate is
fail-closed (do not open / do not close).

| param | default | `None` = off | side |
| --- | --- | --- | --- |
| `theta_open` | `0.0` | yes | open side (`theta_1m_*` of the trade) |
| `p50_open` | `0.30` | yes | open side (`p50_1m_*`) |
| `min_spread_open` | `None` | yes | open side, **unified** threshold (`spread_last_* >=`) |
| `min_profit_pp` | `0.0` | yes | close: `potential_pp` (close-side opposite spread) |
| `min_theta_close` | `None` | yes | **close side** (`theta_1m_short` if closing a long) |
| `fee_round_trip_pp` | `0.30` | n/a (formula, not a gate) | close-side spread in `potential_pp` |

`PolicyParams` is the dataclass; `DummyParams` is an alias. Every field
above is wired. There are no leftover unused knobs.

## Open

`usable` + enabled open gates. If both sides qualify while flat, prefer long.
Does not look at profit.

**Fill snapshot** (replay v0 = prices we *would* have as fill on that second).
Pass-1 uses `spread_last_*` of the opened side, percentage points, same units
as the feature table. Caller stores on `PolicyState`:

- `position_side`, `held_coin`, `opened_ts_s`
- `fill_spread_pp` — `spread_last` of the opened side at the open row

If that `spread_last` is NaN: fail-closed, **do not open** (fill), even when
`min_spread_open` is off.

## Close (potential profit + optional theta)

Opened long captured `fill_spread_pp` on the long legs; flattening executes the
**short** book (`spread_last_short`, `theta_1m_short`). Opened short: opposite
is long.

Round-trip fee default **0.30 pp** (4 taker legs × 0.00075 × 100), occupancy /
fee canon (`docs/gear-2-private-params.md`, `_fee_cost_pct` at qty=100).

**Locked formula (dual-leg unwind; unchanged):**

```
potential_pp(t) = fill_spread_pp + spread_last_opposite(t) - fee_round_trip_pp
```

Same as gear-2 `_closed_trade` PnL at qty=100:
`(open_price + close_price) * (qty/100) - fees`.

Close when enabled close gates pass (`potential_pp >= min_profit_pp` if that
gate is on; close-side `theta_1m_* > min_theta_close` if that gate is on). If
opposite `spread_last` is NaN or the close side is not usable: **hold**
(missing ≠ close). K=1: do not open the other side in the same call.

## Same-type overlap (locked)

In position on side S: if close gates would pass **and** `_qualify_open(S)`
(open gates only: usable, `theta_open`, `p50_open`, `min_spread_open`) →
**hold** (`hold_open_overlap`). Do not close-then-reopen the same type on the
same tick. Opening the **other** side (flip) is not this rule.

**Footgun:** fill proxy is 1 Hz `spread_last`, not Trade_Lat 100ms fills.

```bash
PYTHONPATH=. ./venv/bin/python -m unittest tests.test_gear22_backtest_policy
PYTHONPATH=. ./venv/bin/python -m unittest tests.test_gear22_backtest_replay
PYTHONPATH=. ./venv/bin/python -m unittest tests.test_gear22_backtest_plot_trades
```

## Replay (one UTC day)

`replay_frame(df, params, slot_mode)` is the run. `replay_hive(root, coins=, dates=)`
reads the by_date hive (`dates=None` = all UTC days). `replay_path` is the same
with a single `event_date`. One call over the full table — do **not** loop
`for coin in COINS: replay(...)` (that resets K=1 state each coin).

Returns `ReplayResult(closed, open_positions)`. Closed round-trips are
`ClosedTrade`. A position still open at end of scan is `OpenPosition` (not a
trade). `coins="KAITO"` is one coin; a character-iterated string filter is
wrong — use `None` or `["KAITO"]`.

Until gear **2.5**, K=1 is **sequential across all coins**. `slot_mode` default
is **`global`**: at most one open position worldwide. `replay_frame` always
sorts by `(ts_s, coin)` before scanning (time-major; lexicographic coin at
the same second). While in position, close uses the **held coin's** row at
that timestamp; if that coin is missing, hold (do not close on another coin's
spreads). Concatenating hive parts in coin-major or date-per-coin
order cannot hold coin A all day then open B.

`per_coin` is an explicit opt-in: independent K=1 slot per coin. Research-only
/ gear-2.5-ish. **Not** the live contour (canary-30 independent slots is
wrong for gear-2 K=1).

Pass-1: fill = that second's `spread_last` of the opened side; no Trade_Lat.
Prints trade count only — not a PnL claim.

Thin Jupyter driver (frozen knobs, censoring table, trade Plotly):
`research/gear22_backtest/replay.ipynb`. Graphs: `plot_trades.py` writes
`output/gear22_backtest_trade_plots.html` (open in a browser; not `fig.show`).
1 Hz `theta` / `p50` / `spread_last`, 15 min pad, dropdown + slider.
`PYTHONPATH=. jupyter notebook research/gear22_backtest/replay.ipynb`.
Keep `SLOT_MODE = "global"` and one `replay_hive` over the hive.

## Observation candidate (not a plateau, not live)

Frozen knobs: `DEFAULT_OBSERVE_PARAMS` in `params_frozen.py` (also
`FROZEN`). Notebook cell 2 imports that object. `PolicyParams()` defaults
stay the pass-1 test values.

    theta_open=0.50  p50_open=0.60  min_profit_pp=0.20
    min_theta_close=0.05  min_spread_open=None  fee_round_trip_pp=0.30

This is a **ridge candidate** after a cheap 3-day grid around the old
notebook knobs found no 20–30% plateau. Do not lower `p50_open` to 0.50
(IS occupancy doubles). Do not raise `min_profit_pp` to 0.30 (OOS slot
sticks). `min_theta_close=None` vs `0` are different experiments.

How to observe replay vs live approximations, without wiring send:

1. Re-run `replay.ipynb` / `sweep.py --window is|oos` on these knobs.
   Record `n_closed`, unclosed `last_potential_pp`, `duty_cycle`,
   `close_gate_silent_s` separately; do not fold closed+unclosed into one
   PnL claim.
2. Compare those counters to a would_send / stub journal that uses the
   **same** `decide()` on 1 Hz snapshots. Divergence here is a clock /
   feature bug, not slippage.
3. Then compare that 1 Hz fill to a delayed fill (`Trade_Lat` 100 ms or a
   stochastic tick). If `duty_cycle` jumps toward the `min_profit_pp=0.30`
   stuck-slot cell while knobs stay frozen, the approximation is the
   story. If neighbours on `p50_open`/`theta_open` already swing occupancy
   by 2×, that is knob instability, not fill coarseness.
4. Gear 3 search is only after that split. Gear 2.5 size policy stays
   blocked. Live send stays blocked until an explicit user phrase.

There is no gear-2.2 dummy profile in `app/bot/**` yet. Portable policy is
`policy.decide`. Stub runtime still owns `app/bot/**` and still runs
gear-1.0 / `gear2_would_send` via `app/policy/trade_manager.py`. Next glue
step (not this patch): a stub `gear22_observe` profile that calls `decide`
with `DEFAULT_OBSERVE_PARAMS` and logs would_send only. Do not touch
`app/bot/private/**`.

```bash
# one day, all coins, default slot_mode=global (K=1 sequential)
PYTHONPATH=. ./venv/bin/python -m research.gear22_backtest.replay \
  --hive output/gear22_backtest_features_by_date \
  --event-date 2026-08-12

# one coin still uses the global slot (only that coin can open)
PYTHONPATH=. ./venv/bin/python -m research.gear22_backtest.replay \
  --hive output/gear22_backtest_features_by_date \
  --event-date 2026-08-12 \
  --coin KAITO

# research-only: independent slots per coin (not live contour)
PYTHONPATH=. ./venv/bin/python -m research.gear22_backtest.replay \
  --hive output/gear22_backtest_features_by_date \
  --event-date 2026-08-12 \
  --slot-mode per_coin
```

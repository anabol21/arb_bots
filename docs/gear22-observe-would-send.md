# Gear 2.2 Observe Would-Send Integration

**Status:** Policy-driven would_send contour using `research.gear22_backtest.policy.decide()`.  
**Profile:** `gear22_would_send` (and `gear22`).  
**Track:** 3 Glue / B-bot observation.

## Overview

This integration wires the **gear 2.2 research policy** (`research/gear22_backtest/policy.py`) into the live stub bot's `theta_trade_manager.py` as the trader block. The bot journals would_send trades under frozen observation knobs — **NO real orders**.

The policy replaced the previous simple `theta_1m >= 0.2` entry/exit logic with the full gear 2.2 gates:

- **Open gates:** `usable` + `theta > theta_open` + `p50_1m > p50_open` + optional `spread_last >= min_spread_open`
- **Close gates:** `potential_profit_pp >= min_profit_pp` + optional `min_theta_close` on opposite side

## Policy Source

**Policy module:** `research.gear22_backtest.policy` (pure function, no I/O)

Key functions imported:
- `decide(row: FeatureSnapshot, state: PolicyState, params: PolicyParams) -> Decision`
- `potential_profit_pp(row, state, fee_round_trip_pp) -> float | None`
- `FeatureSnapshot`, `PolicyState`, `PolicyParams`, `Decision`

**Frozen parameters:** `research.gear22_backtest.params_frozen.DEFAULT_OBSERVE_PARAMS`

```python
theta_open        = 0.50
p50_open          = 0.60
min_profit_pp     = 0.20
fee_round_trip_pp = 0.30
min_spread_open   = None      # gate off
min_theta_close   = 0.05
```

These are **observation knobs** from the ridge evidence (not a 20-30% plateau; not a profitability claim; see `params_frozen.py` docstring).

## Integration Architecture

### 1. Adapter: `build_feature_snapshot()`

Converts live data → `FeatureSnapshot` for `policy.decide()`:

**Inputs:**
- `ThetaSnapshot` pairs (long/short) from `theta_screener`
- Live book quotes (OKX + Bybit)

**Outputs:**
- `FeatureSnapshot` with per-side fields:
  - `p50_1m_long`, `p50_1m_short` (from `ThetaSnapshot.p50_1m`)
  - `floor_long`, `floor_short` (from `ThetaSnapshot.floor_tf_select_a25`)
  - `theta_1m_long`, `theta_1m_short` (from `ThetaSnapshot.theta_1m`)
  - `spread_last_long`, `spread_last_short` (computed from live books)
  - `usable_long`, `usable_short` (finite floor + p50 + theta + spread)

Returns `None` if data is incomplete (fail-closed).

### 2. Decision Flow

**When slot occupied (in position):**
- Build `FeatureSnapshot` for held coin
- Build `PolicyState` with `position_side`, `held_coin`, `opened_ts_s`, `fill_spread_pp`
- Call `policy.decide(feat, state, FROZEN_PARAMS)`
- If `action == "close"` and size OK → journal close with `potential_pp` and policy reason
- Else hold (map policy hold reasons → skip/slot_busy audit)

**When flat:**
- Iterate `coin_order` (global K=1 priority)
- Build `FeatureSnapshot` per coin
- Call `policy.decide(feat, flat_state, FROZEN_PARAMS)`
- First `open_long`/`open_short` that passes size_check wins
- Policy already prefers long if both sides qualify

**Policy reasons journaled:**
- Open: `open_long`, `open_short`
- Close: `close_min_profit` (unified min-profit gate)
- Hold: `hold_not_usable`, `hold_below_threshold`, `hold_nan`, `hold_below_min_profit`, `hold_below_min_theta`, `hold_open_overlap`

### 3. Configuration

**Env vars** (default to FROZEN params):

| Variable | Default (FROZEN) | Meaning |
|----------|------------------|---------|
| `BBOT_THETA_OPEN` | `0.50` | Open gate: theta_1m threshold |
| `BBOT_P50_OPEN` | `0.60` | Open gate: p50_1m threshold |
| `BBOT_MIN_PROFIT_PP` | `0.20` | Close gate: min potential profit (pp) |
| `BBOT_MIN_THETA_CLOSE` | `0.05` | Close gate: opposite theta_1m threshold |
| `BBOT_FEE_RT_PP` | `0.30` | Fee (4 taker × 0.00075 × 100) |

**Deprecated:** `BBOT_THETA_THR` (old 0.2 entry/exit) is no longer used when policy is active.

**Note:** `min_spread_open` is hardcoded to `None` (gate off) in live integration.

### 4. Journal Schema Additions

**Open row:**
- `policy_id = "gear22_frozen_v1"` (implicit marker)
- `reason = "open_long" | "open_short"` (from policy)

**Close row:**
- `policy_id = "gear22_frozen_v1"`
- `reason = "close_min_profit"` (unified policy close)
- `potential_pp` — policy's live dual-leg unwind PnL estimate (pp)
- Existing `pnl_spread` — manager's proxy: `open_fill_spread - close_fill_spread`

Both measures are would_send approximations (1 Hz `spread_last` fills, not Trade_Lat).

### 5. Execution Shell (Unchanged)

Still K=1 global slot, `fill_delay=70ms`, size_check, book capture, Sentry emit, stub broker (`would_send=true`, `send=false`). No private APIs, no real orders.

## Code Changes

**Modified files:**
- `app/bot/theta_trade_manager.py`
  - Import `policy.decide`, `FeatureSnapshot`, `PolicyState`, `PolicyParams`, `potential_profit_pp`
  - Add `build_feature_snapshot()` adapter
  - Replace `decide_theta_k1()` logic to call `policy.decide()`
  - Add `policy_params` to `ThetaTradeConfig`
  - Add `fill_spread_pp` to `OpenPosition` (for `PolicyState`)
  - Add `potential_pp`, `policy_decision` to `ThetaDecision`
  - Update journal row builder to include policy fields

- `research/gear22_backtest/__init__.py`
  - Make `replay` imports optional (avoid pandas dependency in live bot tests)

- `tests/test_bbot_theta_trade_k1.py`
  - Update tests to use `PolicyParams` and new decision logic
  - Adjust test fixtures for policy gates

## Validation

**Unit tests:** `tests/test_bbot_theta_trade_k1.py` — all pass (16 tests)
- Policy-driven entry with open gates
- Policy-driven exit with min_profit + min_theta_close
- Size rejection still works
- Slot busy / hold overlap

**Policy tests:** `tests/test_gear22_backtest_policy.py` — unchanged, all pass (33 tests)

## Deployment Notes

1. **VPS environment:**
   - Ensure `PYTHONPATH` includes repo root (already standard for `app.bot.runtime`)
   - `research.gear22_backtest.policy` is pure Python (no pandas in policy.py itself)

2. **Env vars:**
   - Set explicit overrides only if deviating from FROZEN
   - Default behavior uses `DEFAULT_OBSERVE_PARAMS` from `params_frozen.py`

3. **Profile:**
   - `BBOT_PROFILE=gear22_would_send` automatically enables `BBOT_THETA_TRADE=1`

4. **Observation status:**
   - This is the **observe contour**: would_send only, no live send
   - Journal: `/data/bbot-gear22/theta_trades/event_date=YYYY-MM-DD/trades.jsonl`
   - Policy ID marker: `policy_id = "gear22_frozen_v1"` on close rows

## Out of Scope

- Live orders / private send (B-private)
- Changing research policy math (import only, don't reimplement)
- Trade_Lat fill model (still 1 Hz `spread_last`)
- Hysteresis / min-hold
- K>1 slots
- Parameter search (frozen knobs only)

## References

- Research policy: [`research/gear22_backtest/policy.py`](../research/gear22_backtest/policy.py)
- Frozen params & evidence: [`research/gear22_backtest/params_frozen.py`](../research/gear22_backtest/params_frozen.py)
- Trade manager: [`app/bot/theta_trade_manager.py`](../app/bot/theta_trade_manager.py)
- Tests: [`tests/test_bbot_theta_trade_k1.py`](../tests/test_bbot_theta_trade_k1.py)
- Previous doc: [`docs/gear22-theta-k1-would-send.md`](gear22-theta-k1-would-send.md) (execution shell unchanged)

"""Frozen gear-2.2 observation parameters and the evidence behind them.

Observation only. These numbers rank parameter sets against each other under a
zero-slippage 1 Hz fill; they are not a profit estimate and not a live-readiness
signal. See "What this does not show" below.

## Chosen set (conservative ridge candidate)

    theta_open        = 0.50
    p50_open          = 0.60
    min_profit_pp     = 0.20
    min_theta_close   = 0.05
    min_spread_open   = None      (gate off)
    fee_round_trip_pp = 0.30      (4 x 0.00075 x 100, unchanged)

Not a 20–30% plateau on every enabled axis. The cheap grid around the old
notebook knobs (`theta_open=0.30`, `p50_open=0.50`, `min_profit_pp=0.30`,
`min_theta_close=0`) has no plateau: on 3-day OOS those knobs leave the K=1
slot stuck (`duty_cycle ≈ 0.99`, `n_closed=2`). Expanding toward stricter
open gates finds a **ridge**, not a flat interior:

    p50_open        keep ≥ 0.60   (0.50 doubles IS occupancy)
    min_profit_pp   stay in [0.15, 0.25]   (0.30 sticks OOS duty)
    min_theta_close 0 / 0.05 / 0.10 are interchangeable
    theta_open      0.40–0.60 moves duty/mark by ≲ 26%

`min_profit_pp=0.20` is the conservative point on that ridge: further from the
close-gate stuck-slot at 0.30 (the confounder for a 1 Hz vs Trade_Lat observe
pass) than 0.25, with more OOS round-trips (71 vs 58). Combined mark of
one-step neighbours stays inside ~17%. The binding occupancy cliff is
lowering `p50_open` to 0.50, not raising it to 0.70.

`None` vs `0` on `min_theta_close` are different experiments (`0` enables
strict `>` and fail-closed NaN). `None` is not on the ridge (IS duty jumps).

Objective is full-accounting PnL:

    combined_mark = sum(closed potential_pp) + mark-to-market of still-open

because a closed trade satisfies `potential_pp >= min_profit_pp` by
construction and a closed-only sum rewards pushing losses into
`open_positions`.

## Evidence

Harness: `research/gear22_backtest/sweep.py` (parity-locked vs `replay_frame`).
`SLOT_MODE=global`. `min_spread_open=None`. Canary-30 universe selected on
`std_spread` ~2026-08-03…08-27, so OOS starts 2026-08-28.

### Cheap grid (no plateau)

3-day IS `2026-08-12..14` + 3-day OOS `2026-09-02..04`, 192 cells
(`theta` 0.0/0.2/0.3/0.4 × `p50` 0.30/0.40/0.50/0.60 × `min_profit`
0.0/0.15/0.30/0.45 × `min_theta_close` None/0/0.2). **0 / 192** cells
passed a 30% one-step bound on both `duty_cycle` and `combined_mark` on
both windows. Notebook knobs:

                        3d IS     3d OOS
    n_closed              17          2
    combined_mark      17.92       0.20
    duty_cycle         0.609      0.993
    unclosed_mark      +0.11      -0.50

### Expanded 7-day grid (ridge appears)

7-day IS `2026-08-12..18` + 7-day OOS `2026-09-01..07`, 192 cells
(`theta` 0.30–0.60 × `p50` 0.50–0.70 × `min_profit` 0.15–0.30 ×
`min_theta_close` None/0/0.05/0.10). 44 / 192 cells passed 30% on that
shorter window; the only **interior** one was
`(0.50, 0.60, 0.25, 0.05)`. That cell fails the same bound on the **full**
OOS window (min_profit → 0.30 raises duty 0.389 → 0.556, +43%).

### Full-window one-step neighbours of the frozen cell

IS `2026-08-10..27` (46 656 000 rows, span 1 555 200 s),
OOS `2026-08-28..09-13` (42 741 000 rows, span 1 424 700 s).

Frozen `(0.50, 0.60, 0.20, 0.05)`:

                        IS         OOS
    n_closed              37         71
    n_open                 0          1
    closed_sum         26.511     21.206
    closed_min          0.200      0.202
    unclosed_mark       0.000     -0.490
    combined_mark      26.511     20.716
    exposure_s        373 475    498 407
    duty_cycle          0.240      0.350
    close_gate_silent_s  373 438    498 336

One-step neighbours (relative change vs frozen):

    axis / to          IS duty   IS mark   OOS duty  OOS mark  IS n_cl  OOS n_cl
    theta 0.40           +8%      +3%       +6%      +11%        42       79
    theta 0.60          -17%      -6%      -26%      -12%        34       58
    p50 0.50           +103%     +11%      +27%       -1%        39       75   ← cliff
    p50 0.70            +8%      -7%      -21%      -11%        33       63
    min_profit 0.15     -20%      -4%      -12%      -17%        42       76
    min_profit 0.25      +1%      +4%      +11%       -6%        37       58
    min_theta 0.00       +3%      +3%       +0%       -1%        39       71
    min_theta 0.10       -1%      -1%       +1%       -1%        36       70

Nearby alternative `(0.50, 0.60, 0.25, 0.05)`: IS mark 27.694 / duty 0.244,
OOS mark 19.500 / duty 0.389 / n_closed 58. Stabler on `p50_open±0.10`
(IS duty +10% at 0.50), worse on `min_profit_pp→0.30` (OOS duty +43%).

Old notebook knobs on the same full windows: IS 28 closed, mark 21.669,
duty 0.782; OOS 32 closed, mark 11.964, duty 0.856, unclosed −0.651.

## What this does not show

- **Not profitability.** Fill is that second's `spread_last` with zero
  slippage and no `Trade_Lat`; gear-2 fills the first tick at
  `signal + 100 ms` instead. A surface that is positive almost everywhere
  is evidence that this fill approximation is uniformly favourable.
- **Not live-ready.** No order routing, no venue, no size policy (gear 2.5
  is blocked), no latency model. Stub bot still runs gear-1.0 /
  `gear2_would_send` in `app/policy/trade_manager.py`, not this dummy.
- **Not a 20–30% plateau.** Do not read the frozen cell as locally flat on
  occupancy. Do not lower `p50_open` below 0.60; do not raise
  `min_profit_pp` to 0.30.
- **Thin sample.** Tens of closed trades per window do not support
  distributional claims.
- **Still capital-bound.** `duty_cycle` 0.24 IS / 0.35 OOS: the single K=1
  slot is occupied a quarter to a third of wall-clock; mean holds of hours.
- **One censored position per run.** Global K=1 ⇒ at most one open-at-end
  mark. Closed-trade distribution remains left-censored at `min_profit_pp`.
- **Same-second capital reuse.** After a close, a lexicographically earlier
  coin may open on the same `ts_s`.
"""

from __future__ import annotations

from research.gear22_backtest.policy import PolicyParams

FROZEN = PolicyParams(
    theta_open=0.50,
    p50_open=0.60,
    min_profit_pp=0.20,
    fee_round_trip_pp=0.30,
    min_spread_open=None,
    min_theta_close=0.05,
)

# Alias used by the notebook and by any later 2.2 observe profile.
DEFAULT_OBSERVE_PARAMS = FROZEN

PREVIOUS = PolicyParams(
    theta_open=0.30,
    p50_open=0.50,
    min_profit_pp=0.30,
    fee_round_trip_pp=0.30,
    min_spread_open=None,
    min_theta_close=0,
)

IS_DATES: tuple[str, ...] = tuple(f"2026-08-{d:02d}" for d in range(10, 28))
OOS_DATES: tuple[str, ...] = tuple(
    [f"2026-08-{d:02d}" for d in (28, 29, 30, 31)]
    + [f"2026-09-{d:02d}" for d in range(1, 14)]
)

# First cheap grid around PREVIOUS (no plateau).
CHEAP_GRID: dict[str, tuple] = {
    "theta_open": (0.0, 0.2, 0.3, 0.4),
    "p50_open": (0.30, 0.40, 0.50, 0.60),
    "min_profit_pp": (0.0, 0.15, 0.30, 0.45),
    "min_theta_close": (None, 0.0, 0.2),
}

# Refined grid around the high-p50 ridge.
RIDGE_GRID: dict[str, tuple] = {
    "theta_open": (0.40, 0.50, 0.60),
    "p50_open": (0.50, 0.60, 0.70),
    "min_profit_pp": (0.15, 0.20, 0.25, 0.30),
    "min_theta_close": (None, 0.0, 0.05, 0.10),
}

# Metrics observed for FROZEN on a re-run of sweep.run_combo (float32 store).
FROZEN_EVIDENCE: dict[str, dict[str, float]] = {
    "is": {
        "total_pp": 26.511466,
        "n_closed": 37,
        "n_open": 0,
        "duty_cycle": 0.240146,
        "worst_one_step_rel_duty": 1.027351,
        "worst_one_step_rel_mark": 0.110884,
    },
    "oos": {
        "total_pp": 20.716198,
        "n_closed": 71,
        "n_open": 1,
        "duty_cycle": 0.349833,
        "worst_one_step_rel_duty": 0.269743,
        "worst_one_step_rel_mark": 0.171353,
    },
}

__all__ = [
    "CHEAP_GRID",
    "DEFAULT_OBSERVE_PARAMS",
    "FROZEN",
    "FROZEN_EVIDENCE",
    "IS_DATES",
    "OOS_DATES",
    "PREVIOUS",
    "RIDGE_GRID",
]

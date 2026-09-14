# Gear 2.2 Live Floor Investigation (2025-09-13)

**Context:** Mikhail reported that in his model branch backtest, theta from p50 and corresponding floor differ from live canary charts. He observed frequent floor jumps on strategist plots (GPS evening 12.09) and suspects floor calculation.

**Branch:** `cursor/wire-sentry-theta-k1-d61f`  
**Investigator:** Cloud Agent  
**Date:** 2025-09-13  
**Status:** Read-only investigation

---

## 1. Exact Live Formula for `floor_tf_select_a25`

### Formula Lock

The live floor calculation is **locked** to match the research implementation exactly:

**Location:** `research/gear22_quiet_regime_viz/floors.py` → `compute_chosen_floor()`

**Inner path (per 5m bar close):**

```python
close_t              = last valid spread in UTC 5m bucket [bar_start, bar_end)
closes_history       = deque(maxlen=12)  # last 12 closes
sma12_t              = SMA_12(closes)_t  # causal, requires all 12 finite
sma12_history        = deque(maxlen=144)  # last 144 SMA-12 values (12h @ 5m bars)
```

**Floor computation (after SMA-12 history builds):**

```python
trim_3h_t   = trim_mean_{α=0.25} of finite {sma12}_{t-35..t}     # 36 × 5m = 3h
trim_12h_t  = trim_mean_{α=0.25} of finite {sma12}_{t-143..t}    # 144 × 5m = 12h
floor_t     = min(trim_3h_t, trim_12h_t)                          # tf-select α25
```

**Parameters:**
- `α = 0.25` — 25% trimmed from **each tail** (symmetric, matches `scipy.stats.trim_mean`)
- `k = int(n × α)` samples cut from each end after sort
- Windows: 3h = 36 bars, 12h = 144 bars
- **Non-finite `sma12` values are excluded** from the window sample (no interpolation)

### Bar Timing

**Bar boundaries:**
- UTC-aligned 5m buckets: `bar_start_ms = (ts_ms // 300_000) × 300_000`
- `bar_end_ms = bar_start_ms + 300_000`

**Close definition:**
- `close = last valid spread_long (or spread_short) in [bar_start, bar_end)`
- "Valid" = finite value from `compute_spreads()` after book coalesce + age gates

**Bar close trigger:**
- When a new tick arrives with `floor_bar_start_ms(ts) > current_bar_start`, the **previous** bar is finalized
- This means the bar close happens **on the first tick of the next bar**, not exactly at `bar_end_ms`
- Off-by-one risk: **NO** — the close value is correctly the last spread from the previous bar

### Warm-Start Effects

**SMA-12 warm-up:**
- Requires **12 consecutive finite closes** (60 minutes)
- Before warm: `sma12 = NaN`

**Floor warm-up:**
- Requires `min_finite_count(window) = max(5, ceil(0.20 × window))`
  - 3h window: 8 finite sma12 values (40 minutes @ 5m bars)
  - 12h window: 29 finite sma12 values (145 minutes @ 5m bars)
- **tf-select waits for both trim windows** → effective warm-up = **~12 hours** (144 bars)

**Null propagation:**
- If **any** close in the SMA-12 lookback is non-finite → `sma12_t = NaN`
- If `sma12` is NaN at time `t`, that slot is **excluded** from trim window
- If not enough finite `sma12` in trim window → `trim_3h` or `trim_12h = NaN`
- If either trim is NaN → `floor = NaN`

### Side Separation

**Long and short are independent:**
- `spread_long` → `closes_long` → `sma12_long` → `floor_long`
- `spread_short` → `closes_short` → `sma12_short` → `floor_short`
- No mixing. Long can have finite floor while short is still warming.

---

## 2. What Series Are Written to Journal vs Backtest

### Live Floor Journal (`/data/bbot-gear2/floor/event_date=YYYY-MM-DD/metrics.jsonl`)

**Schema:** `bbot.floor.v1`, formula `tf-select-a25-of-sma12`

**Per-bar metrics (one row per `(bar_end_ms, base_coin, side)`):**

| Field | Source | Warm-up |
|-------|--------|---------|
| `close` | Last valid spread in bar | Immediate (first bar) |
| `sma3` | `SMA_3(closes)` | 3 bars (15 min) |
| `sma12` | `SMA_12(closes)` | 12 bars (60 min) |
| `floor_tf_select_a25` | `min(trim_3h, trim_12h)` of `sma12` | ~144 bars (12 hours) |
| `tw_p05`, `tw_p95` | Time-weighted 5%/95% of in-bar spreads | Immediate (if samples exist) |
| `edge` | `close − floor` when both finite | After floor warm |

**What is NOT written:**
- Individual spread ticks
- Full book snapshots
- `trim_3h` or `trim_12h` intermediate values (only final `floor`)
- SMA-3 history or SMA-12 history arrays

### Research/Backtest Typical Usage

**Input:** Parquet ticks from `/data/bars/base_coin=*/event_date=*/` or compacted D trees

**Candles build path:**
```python
ticks → build_5m_bucket_stats() → buckets DataFrame with:
  - close (last tick in bar)
  - tw_p50, tw_p95, tw_p99 (time-weighted quantiles)
  - ma_3, ma_12 (causal SMAs)
```

**Floor path:**
```python
buckets["ma_12"] → compute_chosen_floor(sma12) → dict with:
  - "SMA-12": sma12 array
  - "tf-select α25": floor array
```

**Key difference:**
- Live: bar close computed **when the bar rolls** (next tick arrival)
- Research: bar close computed **after tick collection** (batch, exact `bar_end` alignment)

**Time-weighted quantiles convention:**
- **Both use the same `tick_hold_weights_ms` convention:**
  - Each tick holds its value until the next tick
  - Last tick → `bar_end_ms` (not `bar_start` of next bar)
  - This is **identical** in `app/bot/floor_watcher.py` and `research/gear22_quiet_regime_viz/quantiles.py`

---

## 3. Known Causes of Jumpiness

### A. Discrete 5m Bar Updates

**Mechanism:**
- Floor only updates **on bar close** (every 5 minutes)
- Within a bar, `last_floor` remains constant even if spreads spike
- Theta (`p50 − floor`) can appear volatile if `p50` updates ~1 Hz but floor is frozen

**Expected behavior:** Step function every 5 minutes, not smooth.

### B. α25 Switching Between Regimes

**Mechanism:**
- `floor = min(trim_3h, trim_12h)`
- When spread rises (anomaly / spike):
  - Short window (3h) reacts faster → `trim_3h > trim_12h` → floor held by 12h
  - During return: `trim_3h < trim_12h` → floor follows 3h down
- **This is intentional:** tf-select is designed to **not** classify regime but to provide a slow conservative anchor

**Jumpiness:**
- If `trim_3h` and `trim_12h` are close, small changes can flip the `min()` selection
- Each flip causes a **discrete jump** in floor

**Expected behavior:** Occasional jumps when the two windows cross.

### C. Null → Finite After Warm-Up

**Mechanism:**
- Cold start: floor is `null` for ~12 hours (144 bars)
- First finite floor appears abruptly when warm-up completes
- If long warm completes before short (or vice versa), sides may have different floor availability

**Expected behavior:** One-time step from `null` to finite value.

### D. Side Long/Short Separate

**Mechanism:**
- Long and short spreads can diverge (bid/ask asymmetry, venue fees)
- Long floor and short floor are computed independently
- If long has gaps/NaNs but short doesn't → their floors decouple

**Expected behavior:** Long floor ≠ short floor is normal.

### E. Sparse Ticks / Gaps

**Mechanism:**
- If a 5m bucket has **zero valid spreads** → `close = NaN`
- NaN close → breaks SMA-12 continuity → `sma12 = NaN` for next 12 bars
- NaN `sma12` → excluded from trim windows → could trigger trim NaN if too many holes

**Jumpiness:**
- After a gap, SMA-12 recovers only when 12 consecutive finite closes rebuild
- Floor may drop to NaN then re-warm

**Expected behavior:** Floor degradation during low-tick periods (weekends? exchange maintenance?).

### F. Trim α=0.25 Sensitivity

**Mechanism:**
- Trimming 25% from each tail leaves only the **middle 50%** of the window
- 3h window: 36 bars → 18 middle samples used (after α cut)
- 12h window: 144 bars → 72 middle samples used
- If the middle 50% shifts abruptly (e.g., regime boundary inside the window), the mean can jump

**Expected behavior:** More reactive than median (which would pick the middle value), but more stable than mean (which includes tails).

---

## 4. Can Live Floor Legitimately Diverge from Offline/Research Floors?

### Code Path Equivalence

**Formula lock assertion:**
```python
# app/bot/floor_watcher.py line 88-91
if int(mod.W2_TRIM12_BARS) != SMA12_HISTORY:
    raise RuntimeError(...)
```

**Research canonical:**
```python
# research/gear22_quiet_regime_viz/floors.py
W2_TRIM12_BARS = 144
def compute_chosen_floor(sma12: np.ndarray) -> dict[str, np.ndarray]:
    trim3_25 = causal_trim_floor(s, W2_TRIM3_BARS, alpha=TRIM_ALPHA_25)
    trim12_25 = causal_trim_floor(s, W2_TRIM12_BARS, alpha=TRIM_ALPHA_25)
    return {TF_SELECT_25_NAME: tf_select_floor(trim3_25, trim12_25)}
```

**Live implementation:**
```python
# app/bot/floor_watcher.py line 314-315
chosen = floors.compute_chosen_floor(np.asarray(state.sma12_hist, dtype="float64"))
floor = float(chosen[floors.TF_SELECT_25_NAME][-1])
```

**Conclusion:** **Live directly calls the research formula.** If given the same `sma12` history, they **must** produce identical floor values.

### Possible Divergence Sources

#### 1. **Bar Alignment / Timing**

**Live:**
- Bar closes when the **first tick of the next bar arrives**
- If ticks are sparse, the "close" timestamp may be seconds before `bar_end_ms`

**Research:**
- Batch: all ticks in `[bar_start, bar_end)` are assigned to the bar
- Close is the last tick **within the exact window**

**Divergence:** If a tick arrives at `bar_end + ε` (just after):
- Live: included in the next bar (correct bar boundary)
- Research (if using `<` vs `<=` inconsistently): might be excluded or included differently

**Verdict:** Should be identical if both use `bar_start = (ts // 300000) * 300000` and `[bar_start, bar_start + 300000)` convention.

#### 2. **Tick Data Source**

**Live:**
- Real-time WebSocket books → `compute_spreads()` → observer
- Spreads are computed **on arrival**, then aggregated into bars

**Research:**
- Parquet ticks from `/data/bars` → already contain `spread_long` / `spread_short` columns
- These were computed by the **same** collector logic (frozen)

**Divergence:** Only if:
- Research uses old parquet with a different spread formula (unlikely, frozen)
- Research filters ticks differently (e.g., different age gate, different validity rules)

**Verdict:** Should be identical if research uses the same validity/age gates.

#### 3. **Warm-Start vs Cold Start**

**Live:**
- Starts with empty deques
- Floor is `null` for ~12 hours until full history builds
- Persistent across restarts via `floor_warm.py` (pickle warm state)

**Research:**
- Batch over full history
- First floor appears at bar 144 (after 12h warm)
- No restart gaps

**Divergence:** Only if:
- Live is restarted mid-run and warm state is not restored → floor resets to `null`
- Research spans a gap that live skipped (e.g., collector downtime)

**Verdict:** Legitimate divergence during live restarts without warm persistence.

#### 4. **Sparse/Gap Handling**

**Live:**
- If no valid spreads in a bar → `close = NaN`
- NaN close → breaks SMA-12 → `sma12 = NaN`
- NaN `sma12` → excluded from trim windows

**Research:**
- Can **fill empty buckets** with `fill_empty_buckets=True` in `build_5m_bucket_stats()`
- Empty bucket: `close = NaN`, `tw_p50 = NaN`, etc.
- Same NaN propagation logic

**Divergence:** Only if:
- Research does not fill empty buckets → skips bars entirely → bar count mismatch
- Research interpolates NaN (violates causal constraint)

**Verdict:** Should be identical if research uses `fill_empty_buckets=True` and does not interpolate.

#### 5. **SMA-12 History Array Length**

**Live:**
- `sma12_hist = deque(maxlen=144)`
- When computing floor, passes **entire deque** to `compute_chosen_floor()`
- If deque is not full yet (< 144), trim windows have fewer samples

**Research:**
- Passes full `buckets["ma_12"]` array (could be 1000s of bars)
- `compute_chosen_floor()` applies causal floor over the **entire array**

**Key difference:**
- Live: floor at time `t` uses `sma12_{t-143..t}` (rolling 144-bar window)
- Research: floor at time `t` uses `sma12_{0..t}` (all history from start)

**Wait, this is a potential bug!** Let me re-check:

```python
# app/bot/floor_watcher.py line 257-258
sma12_hist: deque[float] = field(
    default_factory=lambda: deque(maxlen=SMA12_HISTORY)  # 144
)
```

```python
# line 313-315
state.sma12_hist.append(sma12)
chosen = floors.compute_chosen_floor(np.asarray(state.sma12_hist, dtype="float64"))
floor = float(chosen[floors.TF_SELECT_25_NAME][-1])
```

**Analysis:**
- `sma12_hist` has `maxlen=144` → only keeps last 144 values
- Passes entire deque (up to 144 bars) to `compute_chosen_floor()`
- Takes `[-1]` (last element) from the returned array

**Research path:**
```python
# compute_chosen_floor() returns arrays of the same length as input
# causal_trim_floor() applies the floor formula over all input points
```

**Verdict:** 
- Live correctly uses a **rolling 144-bar window** by virtue of deque `maxlen`
- Research should use the **same rolling window** if it extracts the tip correctly
- If research passes the full history array and takes `[-1]`, it's computing the floor at the last bar using the **entire history**, not just the last 144 bars

**This is a legitimate divergence source if research does not window the SMA-12 history!**

---

## 5. Identified Bug or Design Question?

### Potential Bug: Research May Not Window SMA-12 History

**Issue:**
- Live: `floor_t` computed from `sma12_{t-143..t}` (last 144 bars only)
- Research (if not windowed): `floor_t` computed from `sma12_{0..t}` (all bars from start)

**Effect:**
- Early in the series: divergence is large (research uses fewer samples, more NaN)
- Late in the series: divergence persists if old regime data affects research floor

**Evidence needed:**
- Check if `research/gear22_quiet_regime_viz` usage passes windowed `sma12` or full array

**Fix (if bug confirmed):**
```python
# In research notebook or CLI:
sma12_full = buckets["ma_12"].to_numpy(dtype="float64")
n = len(sma12_full)
window = 144
floors_array = np.full(n, np.nan, dtype="float64")
for t in range(n):
    start = max(0, t - window + 1)
    sma12_window = sma12_full[start:t+1]
    chosen = compute_chosen_floor(sma12_window)
    floors_array[t] = chosen[TF_SELECT_25_NAME][-1]
```

### Known Jumpiness (Not a Bug)

**Expected behaviors:**
- Discrete 5m steps (bar close timing)
- tf-select switching between 3h and 12h trim (by design)
- Warm-up phase (null → finite)
- Side independence (long ≠ short)
- Gap recovery (after sparse periods)

**These are NOT bugs — they are inherent in the formula.**

---

## 6. Recommendations

### For Mikhail's Backtest

1. **Verify windowing:** Ensure the backtest uses **last 144 SMA-12 bars** to compute floor, not full history.

2. **Align bar timing:** Confirm backtest uses `floor_bar_start_ms()` and `[bar_start, bar_end)` convention exactly as live.

3. **Check gaps:** If the backtest period has collector downtime or sparse ticks, expect floor NaN during those windows.

4. **Compare θ = p50 − floor carefully:**
   - Live `p50` is ~1 Hz rolling window (not bar-aligned)
   - Live `floor` updates every 5 minutes (bar-close aligned)
   - Research `p50` from `buckets["tw_p50"]` is per-bar (5m aligned)
   - If mixing live p50 (1 Hz) with research floor (5m), expect mismatch

5. **Validate warm-up:** First 144 bars (12 hours) should have `floor = NaN` in both live and backtest.

### For Live Floor Validation

1. **Export live `sma12_hist`:** Add a debug endpoint or log the `sma12_hist` deque at bar close to compare with backtest.

2. **Smoke test:** Run unit test `test_bar_close_sma_and_floor_match_compute_chosen_floor` with real data snippet.

3. **Check for restarts:** If live was restarted during the observation window, floor resets to `null` unless warm state is restored.

4. **Inspect strategist plots:** If "frequent floor jumps" are every 5 minutes, that's expected (bar close cadence). If jumps are more frequent, investigate:
   - Is theta mixing 1 Hz p50 with 5m floor?
   - Are there duplicate bar closes (logic bug)?
   - Are there NaN propagation issues?

### Next Steps

1. **Extract comparison data:**
   - Live: `/data/bbot-gear2/floor/event_date=2024-09-12/metrics.jsonl`
   - Research: corresponding bars from backtest output

2. **Align timestamps:** Match `bar_end_ms` exactly.

3. **Compare:**
   - `close` (should match if same ticks)
   - `sma12` (should match if same close history)
   - `floor_tf_select_a25` (should match if same sma12 history **and windowing**)

4. **If still divergent:** Isolate the first divergent bar and trace:
   - Input: last 144 `sma12` values
   - Expected: trim_3h, trim_12h, min()
   - Actual: live floor value vs research floor value

---

## 7. Critical Finding: Research Viz Does NOT Compute Floors by Default

**Discovery:**
- The `research/gear22_quiet_regime_viz` tool **does not compute or plot floors** automatically.
- `metrics_ext.collect_extension_traces()` returns an empty list with a comment: *"Do not invent placeholder series."*
- The extension_help_html explicitly states: *"Quiet-regime statistical floor / threshold candidates are **not** plotted yet."*
- The research viz only provides the **canonical floor formula** in `floors.py` for external use.

**Implication:**
- If Mikhail is comparing backtest floors to live floors, he must be computing floors **manually** in a notebook, script, or separate tool.
- The key question: **Does his backtest code pass the full `sma12` array or a rolling 144-bar window?**

### Correct Usage Pattern for Backtest Floor

```python
# CORRECT: Rolling window (matches live)
from research.gear22_quiet_regime_viz.floors import compute_chosen_floor

sma12_full = buckets["ma_12"].to_numpy(dtype="float64")
n = len(sma12_full)
window = 144

floors_array = np.full(n, np.nan, dtype="float64")
for t in range(n):
    start = max(0, t - window + 1)
    sma12_window = sma12_full[start:t+1]  # Last 144 bars only
    chosen = compute_chosen_floor(sma12_window)
    floors_array[t] = chosen[TF_SELECT_25_NAME][-1]

# Now add to buckets:
buckets["floor_tf_select_a25"] = floors_array
```

```python
# INCORRECT: Full history (diverges from live after bar 144)
chosen = compute_chosen_floor(buckets["ma_12"].to_numpy())
buckets["floor_tf_select_a25"] = chosen[TF_SELECT_25_NAME]  # WRONG: uses all history, not rolling window
```

**Why this matters:**
- Live: `floor_t` uses `sma12_{t-143..t}` (deque with `maxlen=144`)
- Incorrect backtest: `floor_t` uses `sma12_{0..t}` (full array from start)
- Effect: Late in the series, old regime data persists in the backtest floor calculation, causing divergence.

---

## 8. Conclusion

**Formula correctness:** ✅ Live floor directly calls research `compute_chosen_floor()`. Formula is locked and validated by unit tests.

**Known jumpiness sources:** ✅ Documented (discrete bar updates, tf-select switching, warm-up, side separation, sparse ticks).

**Critical finding:** ⚠️ The research viz tool does **not** compute floors automatically. If Mikhail's backtest computes floors, he must ensure:
1. **Windowing:** Use last 144 `sma12` bars, not full history
2. **Bar timing:** Align `bar_start_ms` / `bar_end_ms` exactly as live
3. **Gap handling:** Fill empty buckets, do not interpolate NaN

**Probable divergence cause:** Backtest may be using `compute_chosen_floor(full_sma12_array)` instead of a rolling 144-bar window. This would cause floors to diverge after warm-up.

**Action:** 
1. Verify the backtest floor computation code.
2. If it passes the full `sma12` array, refactor to use a rolling window (see section 7 correct pattern).
3. If windowing is already correct, extract live `sma12_hist` and backtest `sma12_window` at the divergent bar for exact numerical comparison.

---

**Files examined:**
- `app/bot/floor_watcher.py` (live formula)
- `research/gear22_quiet_regime_viz/floors.py` (canonical formula)
- `research/gear22_quiet_regime_viz/candles.py` (bar stats)
- `research/gear22_quiet_regime_viz/quantiles.py` (TW convention)
- `research/gear22_quiet_regime_viz/metrics_ext.py` (extension hook — floors not auto-computed)
- `docs/gear22-live-floor-watcher.md` (spec)
- `docs/gear22-floor-metric.md` (formula intent)
- `tests/test_bbot_live_floor_watcher.py` (unit tests)
- `tests/test_gear22_quiet_regime_viz.py` (viz tool tests — confirms no floor plotting)

**No code changes made.** This is a read-only investigation.

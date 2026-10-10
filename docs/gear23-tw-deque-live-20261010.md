# Gear 2.3 TW deque live restart — 2026-10-10

## Change

Based on B2.3 commit `63ee7a4`, this runtime selects `BBOT_TW_P50_1M_ENGINE=deque`
and publishes the one-minute-only TW/theta snapshot fields. The 5-minute
snapshot metrics were removed from the watcher, screener, theta journal, and
their tests. Floor, theta decision policy, order sizing, terminal-private
execution, and the Sentry event policy were carried over unchanged.

Hot-add leverage setup now accepts either Bybit position mode if the target is
flat and has no open orders. It sends equal `buyLeverage=1` and `sellLeverage=1`
and confirms both readbacks; any exposure, open order, unsupported position
index, HTTP failure, or non-1x readback still blocks that extra. The old helper
incorrectly rejected every hedge-mode symbol before sending a valid 1x request.
Bybit documents equal buy/sell leverage for cross margin in hedge mode and
one-way mode: [Set Leverage API](https://bybit-exchange.github.io/docs/v5/position/leverage).

## Validation

- VPS Python 3.10 syntax check passed.
- The hedge-mode mock first failed on the old preflight, reproducing the cause.
- After the patch, the focused B2.3 suite passed: 123 tests, including TW
  watcher parity cases, unchanged private flat guards, hot-add leverage, and
  Sentry events.
- The 10-minute 360-message/s deque stress comparison is in
  [`would-send-cpu-canary-20261010.md`](would-send-cpu-canary-20261010.md):
  23.331 CPU-seconds vs 364.2 for legacy, with no reference-oracle mismatches.
- Startup of the updated B2.3 contour confirmed no positions or open orders on
  its full 61-instrument pool before signal tasks began.
- During startup hot-add leverage preparation, all 32 extras failed: 31 logged
  `leverage_preflight_not_flat` and one logged `leverage_preflight_failed`;
  no setter was called for those symbols. The runtime remains fail-closed, so
  those extras are present in the public pool but are not trade-eligible until
  verified 1x is established. The cause is unresolved: the full-pool startup
  flat check passed immediately beforehand, and the per-symbol helper only
  emits the combined category. No duplicate diagnostic REST requests were made.

## Live configuration and files

The process is standalone `python -m app.bot`, using source at
`/root/b-private-b-exp/response-manager-code/gear23-b2.3-tw-deque-20261010/`.
It shares the existing Gear 2.3 run root to preserve Grok/Sentry monitoring:
`/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun/`.
The public pool is the 29 fixed base coins plus 32 rows in the current cumulative
would-send delta CSV. New delta rows are still gated and prepared at most one
coin per second; the 32 current extras remain blocked by the preflight result
above. The process has cycle cap `0` and no open-window deadline.

The runtime journal and logs are first written to VPS-local storage under the
run root. The source and rollout note are edited locally in a branch based on
B2.3. No remote-storage or off-host backup check was part of this rollout.

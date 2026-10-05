# Gear 2.2 live canary — 2026-10-05

## Status at 2026-10-05 14:11:54 UTC

The standalone background `python -m app.bot` process is still running under PID `2153180`. Startup completed, the account-wide flat check passed, and the Gear 2.2 manager opened one RVN long position. At the latest heartbeat (14:11:25.949 UTC), the manager reported `pending=False`, RVN long still open, with no cycle-flat marker and no halt marker. The campaign cap is ten completed open → terminal close → REST-flat cycles. Close remains policy-driven; there is no timed or forced close. The process was intentionally left running under that cap.

This is an in-progress canary, not a completed campaign or a result about strategy performance.

## Policy and effective configuration

The active VPS `spread-bbot-would-send-prod` process used `gear22_would_send`, `BBOT_MODE=policy`, K=1, and enabled floor, TW-p50, and theta watchers. Its ordered 29-coin pool was:

`KAITO, HOME, WAL, RVN, ONT, 2Z, BICO, HMSTR, CAP, BLEND, EDEN, KMNO, GPS, ME, ZBT, MOVE, COAI, AZTEC, APR, YB, AT, H, MUBARAK, ACU, LA, BEAT, PARTI, SIGN, GIGGLE`.

The terminal-private Gear 2.2 canary used that same ordered pool and manager policy. `ThetaTradeManager` used `research.gear22_backtest.policy.decide` with the frozen parameters: `theta_open=0.50`, `p50_open=0.60`, `min_profit_pp=0.20`, `fee_round_trip_pp=0.30`, and `min_theta_close=0.05`. These source files matched the active would-send research fallback by SHA-256:

- `research/gear22_backtest/policy.py`: `c7f04f33e25e708ec4628d304f9bb249fed1c1cf819ba9ebe1a50b4de095612d`
- `research/gear22_backtest/params_frozen.py`: `c881fe1d95050b860e5c37d5d59c6b10b63df3eaa9dc432b063407c4ba4bad4b`

The intentional sizing difference is the canary's $10 target notional versus $20 in the would-send process. Runtime guards enforce the $7–$15 admissible band; confirmed 1x leverage is required for all 29 coins; K remains 1. Live routing uses the existing warmed private dual sender inside the common `BotRuntime`. The policy selector is Gear 2.2 (`BBOT_THETA_POLICY=gear22`), not the synthetic selector.

The run used local source commit `35b0045643173b2d630cefcfc7cb6fcddacfdd46`. The two root-reviewed, log-only files copied into the isolated VPS code tree had these hashes:

- `app/bot/runtime.py`: `84723aaa27aaf3e09510919253a859552eb0c0141dfb14d45df5fc802dd9d7ff`
- `app/bot/theta_trade_manager.py`: `1c005d79121368377614e1e8e140dc6dd8a56904bd46271875e329055df4ec7f`

No policy or execution logic was changed for this run.

## Startup and first intent

The process started at `2026-10-05 14:08:36 UTC` from `/root/b-private-b-exp/response-manager-code/response-handler-20261005/`. It loaded the existing would-send floor warm pickle into its own runroot (`touched=204`), reported the warmed private session ready, prefetched OKX metadata for all 29 coins, and passed the built-in account-wide Bybit linear USDT / OKX SWAP flat check before signals were admitted. Confirmed 1x leverage was supplied from the existing preparation manifest; no leverage setters or readbacks were repeated.

The first selected Gear 2.2 intent was RVN long at `2026-10-05 14:08:57.965 UTC`. Its terminal journal recorded matching filled quantities of 374 OKX contracts and 3,740 Bybit units, with the local terminal fill completion at `14:08:58.042 UTC`. This 77 ms difference is a local signal-to-terminal-completion interval, not an exchange execution-latency measurement. Per-leg average prices and exchange `execTime`/`fillTime` values were not captured in this bounded status read, so no venue-time latency or price claim is made here.

The existing `StepChrono` markers show signal decision monotonic time `4816560747965106 ns` and the generic `ws_send` stage-enter time `4816560759303172 ns`, 11.338 ms later. This is a stage-boundary measurement, not the physical owner `ws.send` start. Queue-enqueue and owner-send markers were not extracted.

## Run boundary and artifacts

- VPS runroot: `/root/b-private-b-exp/response-manager/20261005T140836Z-gear22-canary/`
- Public runtime data and logs first materialize under that runroot; private journals and wire logs are under its `private/` subdirectory.
- The warm-pickle source `/data/bbot-would-send-prod/state/floor_warm.pkl` was copied read-only into this runroot.
- The existing would-send service and production D services were not changed or restarted.
- At the latest observation, the process was alive, with one RVN long position, no pending intent, zero completed flat cycles, and no recorded halt. The ten-cycle cap stops the process only after cycle ten is confirmed REST-flat.
- This run made no claim about mounted or remote-storage durability. No later account-wide REST audit was made.

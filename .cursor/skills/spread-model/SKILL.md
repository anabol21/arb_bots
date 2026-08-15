---
name: spread-model
description: Develops and validates the historical cross-exchange spread simulator and its gear progression. Use for model.ipynb, run_backtest, Trade_Lat, execution assumptions, regime screening, multi-coin simulation, position sizing, parameter search, or docs/strategy-gears.md.
---

# Spread Historical Model

Use `.cursor/agents/model-simulator-agent.md` for implementation and `.cursor/agents/integration-validator-agent.md` for independent regression.

## 1. Name one gear

- 1.0: closed fixed-vector simulator for one coin.
- 1.5: closed; fixed expert volatility screener (Top-N / cluster); no PnL optimization; `regime_on` is not a close criterion.
- 2: multi-coin simulation using 1.0 + 1.5, limit `K`, equal capital per slot.
- 2.5: separate position-size policy.
- 3: parameter search after 2–2.5 and an anomaly-episode catalog.

Do not skip gears or implement live trading in this workflow.

## 2. Freeze the baseline

Before editing, record one reproducible configuration, input period, result metric, trade count, and relevant counters.

When new behavior is disabled:

- preserve the baseline;
- preserve `VARIATION` versus `HYPER` separation;
- preserve trade/metric/plot consistency.

Any intentional baseline change requires an explicit simulator-contract change.

## 3. Implement one capability

Keep signal, pending state, fill, fees, and open-at-end positions explicit.

For `Trade_Lat > 0`, use fill time and fill price consistently. Do not open duplicate trades while a fill is pending.

For gear 1.5+, verify data coverage and the number of independent anomaly episodes before interpreting results.

For gear 3, keep selection data separate from final test data.

## 4. Validate independently

Integration Validator Agent checks:

- baseline with new flags off;
- open/close markers versus trades;
- pending/fill behavior;
- `metric`, `closed`, and `open_at_end`;
- required size fields when volume gates are enabled;
- clean notebook state from configuration to report.

Review Critic Agent joins when execution assumptions, metrics, or gear order change.

## 5. Report honestly

Separate:

- code correctness;
- simulator approximation quality;
- historical result;
- evidence of generalization.

A short or rare-event sample can validate mechanics but cannot establish a trading advantage.

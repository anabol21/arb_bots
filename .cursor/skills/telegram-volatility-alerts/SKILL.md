---
name: telegram-volatility-alerts
description: Builds and validates an alert-only Telegram service for anomalous crypto volatility from published OKX and Bybit data. Use for alert episode labeling, regime replay, persistence shadow models, alert outbox, Telegram delivery, or the alerts/ package. It excludes trading, order routing, private APIs, and collector ingest changes.
---

# Telegram Volatility Alerts

Read `docs/telegram-volatility-alerts-spec.md` and route through
`.cursor/agents/telegram-alert-orchestrator.md`.

## Scope

The service is an alert-only consumer of published data:

- Complete L1 ticks produce causal closed 5m mid-OHLC.
- `bar_5m` provides volume.
- The score remains compatible with the M 1.5 regime contract.
- Telegram delivers information, not investment advice.

Do not modify collector ingest, exchange parsing, spread calculations,
storage lifecycle, `model.ipynb`, private APIs, or order routing.

## Required sequence

1. **Specification:** version `t0`, labels, features, split/embargo,
   false-positive budget and message semantics.
2. **Offline rule baseline:** replay only data available at each closed bar.
3. **ML shadow:** predict `persistence_30m`; never control delivery yet.
4. **Contracts:** version observation, candidate and outbox.
5. **Delivery shadow:** outbox without user messages, then a closed canary.
6. **Rule-only production v1:** limited universe and observation window.

## Label discipline

Keep labels separate:

- `manual_anomaly_label`: expert episode assessment;
- `persistence_30m`: future target, true when 3 of next 6 bars remain in
  regime.

Do not derive ground truth from the threshold being tested. Do not use future
bars in features. Split by time and independent episode, with embargo at least
as long as the target horizon.

## Rule-only v1

Use closed 5m bars, 48-bar warmup, threshold hysteresis, volume and
ATR-like confirmation, cross-exchange confirmation, cooldown, and a Top-N
limit. Record `missing_data` and suppression reasons explicitly.

Primary measures: event precision/recall, PR-AUC, false alerts per asset-day
and universe-day, time-to-detect and coverage. Do not use PnL, accuracy or
ROC-AUC as primary success evidence.

## Delivery discipline

- Generate `alert_id = metric_version:base_coin:t0`.
- Store an append-only outbox with `queued`, `sent` and `failed` states.
- Respect Telegram rate limits and `retry_after`.
- Log candidate/queued/sent/failed/retried counters.
- Test retry, restart and Telegram API outage.
- Measure p95 closed-bar-to-send separately; target ≤45 seconds in canary.

## Handoffs

1. Model Simulator defines replay baseline and shadow ML.
2. Schema Contract defines versioned data structures.
3. Alert Runtime & Delivery implements only `alerts/`.
4. Integration Validator verifies replay/baseline.
5. Validation verifies canary delivery.
6. Review Critic checks leakage, false positives and delivery loss.

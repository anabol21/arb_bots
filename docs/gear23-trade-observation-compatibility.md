# Gear2.3 trade-observation compatibility

This note documents the offline, completed-trade observation path in the VPS source baseline. It does not describe or authorize changes to the trading policy, order routing, launcher, or live process.

## Execution identity

New OKX history facts retain the `clOrdId` read from the fills-history response and record `correlation_basis=client_order_id`. The collector only emits the fact after matching that value to the trade observation. The reader rejects any present but mismatched client ID.

Older facts have no `client_order_id`, but the old collector persisted them only after its fills-history `clOrdId` filter succeeded. The reader accepts that legacy evidence only when the fact also has the expected fills-history source endpoint, a nonempty exchange `order_id`, and the matched intent/event/instrument. A Bybit fill with no `orderLinkId` is accepted only when the collector recorded `correlation_basis=known_order_id` and a nonempty `order_id`; unrelated or contradictory identities remain excluded.

## Quantity basis

Each new completed-trade observation keeps separate values for:

- `target_notional_usdt` — configured target notional;
- `planned_qty` / `unrounded_computed_coin_qty_by_venue` — the existing pre-rounding signal-price estimate;
- `planned_submitted_coin_qty` and its per-venue map — the common rounded coin quantity actually passed to the sender;
- `planned_submitted_qty_by_venue` — rounded native units sent for OKX contracts and Bybit base-coin quantity;
- `actual_terminal_filled_qty_by_venue` — venue-native quantities confirmed by the completed sender result.

The main `theoretical_coin_quantity`, `gross_theory`, and `pnl_theory` use the open event's planned submitted coin quantity when present. New output also retains `target_notional_coin_quantity`, `gross_theory_target_notional`, and `pnl_theory_target_notional`, so previous target-notional theory remains available. Old observations without submitted quantity retain their prior target-notional/reference-price basis and are labeled `target_notional_reference_price_legacy`.

Actual execution quantities continue to come from deduplicated exchange fills. The planned quantity never substitutes for an actual fill. This allows equal full fills to yield a zero quantity effect and partial fills to show the difference between submitted plan and actual fills in the PnL decomposition.

## Timestamp coverage

Coverage timestamp fields are normalized only at the reconciliation boundary. Integral numeric values and integral numeric strings—including integral floating-point values produced by nullable tabular conversions—map to Unix milliseconds. Null, non-finite, fractional, or malformed values remain invalid and cannot establish complete coverage. Page status and cursor-chain completeness are still required independently.

This source tree does not contain the deployed Gear2.3 metrics JSONL-to-Parquet compactor. The regression tests exercise the exact record shape that a nullable Parquet conversion hands to `reconcile_records`; deployment-side compactor behavior still needs separate validation if the compactor itself changes.

## Fees and funding

Fee and funding attribution behavior is unchanged. The theoretical fee calculation still derives per-venue rates from matched execution fees, so it is not an independent pre-trade fee estimate. Account-level funding remains unassigned unless evidence uniquely attributes it; this patch does not infer that attribution.

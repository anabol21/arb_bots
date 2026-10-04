# Response-manager live experiment result — 2026-10-05

**Outcome:** stopped before any order request. The existing OKX liquidity gate compares derivative contract counts from `books5` with planned base-coin quantity. The latest run therefore found no eligible candidate; the experiment did not validate live fills or close handling. No order/place frames were sent, all REST position and open-order checks were flat, and the campaign completed zero cycles.

## 1. Pipeline block and files involved

The path is OKX `books5` → `parse_okx_books5_message` → runtime quote cache → `ThetaTradeManager.size_check`. The parser retains the second bid/ask level field as `bid_size`/`ask_size` without conversion ([`ws_books.py`](../app/bot/ws_books.py)); the manager compares that value directly to `$10 / price`, a base-coin quantity ([`theta_trade_manager.py`](../app/bot/theta_trade_manager.py)). Runtime prefetches each OKX instrument's `ctVal` for order placement, but the liquidity check does not receive or use it ([`runtime.py`](../app/bot/runtime.py), [`okx_ct_val.py`](../app/bot/private/okx_ct_val.py)).

OKX's [API guide](https://app.okx.com/docs-v5/en) states that the second order-book level value is quantity in number of contracts for derivatives. The same guide defines derivative order notional as `sz × ctVal × markPx` for linear contracts. Thus `contracts × ctVal` is the base quantity that can be compared with the manager's planned base quantity. The parser itself need not change.

## 2. Read-only unit audit and last observed candidate gate

The approved set was 2Z, HOME, and LA. Public instrument metadata returned `ctVal` 10, 100, and 10 base units per contract, respectively. The final fresh WS short/open prefilter at 2026-10-04 22:01:45 UTC observed:

| Coin | OKX best bid size | `ctVal` | OKX available base qty | Planned OKX base qty | Bybit best ask / planned qty | Gate impact |
|---|---:|---:|---:|---:|---:|---|
| 2Z | 123 contracts | 10 | 1,230 | 223.914 | 351 / 223.614 | OKX raw comparison rejected; converted OKX size passes. Bybit size passes. |
| HOME | 8 contracts | 100 | 800 | 1,769.285 | 12,140 / 1,769.285 | Still fails OKX after conversion. |
| LA | 76 contracts | 10 | 760 | 149.388 | 1 / 149.187 | Still fails Bybit. |

These are a single changing-market snapshot, not a claim that a strategy signal or the remaining trading gates would pass. The last runner correctly stopped with zero intents when its unchanged size gate found no eligible coin. Earlier WS snapshots differed, which is why the gate used fresh books before deciding.

## 3. Runs, account state, and validation

Code was edited in isolated checkout `/private/tmp/arb_bots-exchange-response-20261004`; the validation runner ran on the VPS. The six user-approved leverage-setting POSTs (three instruments × two venues) were a separate setup run, each read back as 1x. They are not trade orders. No leverage setters were repeated afterward.

The bounded runner attempts were:

- `20261004T213416Z-response`: public probe path aborted before place; no order frame.
- `20261004T214558Z-response`: runner import error before a trade decision; no order frame.
- `20261004T214741Z-response`: actual manager rejected the first size check; no order frame.
- `20261004T220139Z-response`: fresh bounded prefilter found no eligible coin; zero cycles and no order frame.

The latest sanitized result is `/root/b-private-b-exp/response-manager/20261004T220139Z-response/result.json`; the run artifacts were materialized under `/root/b-private-b-exp/response-manager/20261004T220139Z-response/` on the VPS. Final fresh REST checks showed no positions and no open orders. The VPS-local files are not evidence of mounted-remote-storage durability.

Offline scoped response-handler tests, the runner no-send self-test, Python compilation, and whitespace validation passed locally; the runner compilation and no-send self-test also passed on the VPS. The runner self-test covers manager-path control and the bounded gate, not real fills. No fill, exact-close, raw-fill replay, or flat-after-close claim can be made because no place was sent.

## 4. Risks, scope, and next step

This is a unit mismatch in the existing liquidity comparison, not evidence that market depth is absent. Do not change the websocket parser, order quantity mapper, nominal target, or exchange gate policy as part of this result. A minimal follow-up should pass explicit positive `ctVal` into the private manager's OKX depth comparison, normalize contract counts to base quantity (including requested depth levels), and fail closed when metadata is absent. It must cover both open and close paths; the close check must be reconciled with the actual filled position quantity. That patch and any new live attempt require separate review after fixing and testing the dimensional comparison.

No service or would-send process was modified or restarted. No code was committed or pushed. No architecture topology changed, so `architecture.md` did not need an additional update for this report.

# Canary trade chronometry dashboard

Track 3 / B-private. After each **successful** Contour B dual-leg (both
trade ACKs accepted) on the WAL/EDEN canary, the runtime writes a
self-contained HTML page plus JSON under the canary data root:

```text
{BBOT_DATA_ROOT}/reports/trades/<intent_id>/chronometry.json
{BBOT_DATA_ROOT}/reports/trades/<intent_id>/dashboard.html
```

Example: `/data/bbot-canary-wal-eden/reports/trades/<intent_id>/dashboard.html`.

This is not a VPS deploy. The next successful canary trade emits the page
once the updated unit is running. Overnight EDEN
(`signal_ts_ms=1788710004563`) cannot get a full public tape: the process
did not keep an L1 ring, and `/data/live` EDEN parquet that day starts
~10 minutes after the open. Wire + fills still exist; this change does
**not** invent ticks for that trade.

## What is recorded

1. **Rolling public L1 ring** (canary coins, default 60s / 16384 ticks).
   Each accepted public book update stores wall/mono, venue, bid/ask/size,
   `event_local_ts`. Append is on the public book path **before** coalesce,
   not on signal→send.
2. **Signal-time book snapshot** at `place` (Bybit bid/ask, OKX bid/ask,
   `spread_long_pct` / `spread_short_pct`). Survives ring wrap.
3. After dual ACK: freeze `[signal − lookback, last marker + lookahead]`,
   optionally wait off-thread for private fill prices
   (`BBOT_CHRONOMETRY_FILL_WAIT_SEC`, default 8s), then write JSON+HTML.

No fill-wait is added to the Contour B send hot path.

## Dashboard

- Sell-venue **bid** tape (open_long: Bybit bid) with signal / ack / fill
- Buy-venue **ask** tape (open_long: OKX ask) with the same markers
- Matching **spread** series (long or short)
- Table: signal→send, send→ack, send→fill, signal→fill per venue, `fill_delivery`
  when the wire has venue_ts. Fill markers prefer venue `execTime` / `fillTime`
  / OKX orders `uTime` over local recv when present. OKX does **not** subscribe
  the VIP `fills` channel; `orders` already carries `fillPx` / `avgPx` + `uTime`.
- **Signal spread** (book at signal) vs **fill spread** (exec/avg prices,
  same policy formula)

Policy:

```text
spread_long_pct  = (bybit_bid − okx_ask) / bybit_bid × 100
spread_short_pct = (okx_bid − bybit_ask) / okx_bid × 100
```

open_long sells Bybit (bid) and buys OKX (ask). Fill spread uses the
same slots with venue exec prices.

## Env

| Variable | Default | Meaning |
|----------|---------|---------|
| `BBOT_PROFILE=canary_wal_eden` | — | enables ring + dashboard |
| `BBOT_CHRONOMETRY` | on for canary | `0` disables |
| `BBOT_L1_RING_SEC` | 60 | ring age |
| `BBOT_L1_RING_MAX_TICKS` | 16384 | ring cap |
| `BBOT_CHRONOMETRY_LOOKBACK_SEC` | 30 | freeze before signal |
| `BBOT_CHRONOMETRY_LOOKAHEAD_SEC` | 15 | freeze after last marker |
| `BBOT_CHRONOMETRY_FILL_WAIT_SEC` | 8 | post-ack only; `0` writes immediately |
| `BBOT_CHRONOMETRY_SYNC` | off | tests: write on the place thread |

## Intent ↔ venue client-id match

OKX `clOrdId` is `o` + the first **31** hex of
`intent_id` without hyphens (32-char venue cap). Bybit `orderLinkId` is
`b` + the 32-hex id (fits in 36). Flatten uses `f` + venue letter, then
the same caps. Chronometry therefore matches client ids by **containment
both ways** on the compact hex and on the tail after a 1–2 letter prefix,
with a minimum prefix of 12 characters. A full-string
`dual_leg_id in clOrdId` check is not enough: the last hex digit is
missing on OKX.

Fill markers still require `fillPx` / `avgPx` / `execPrice` > 0. A
`state=live` row with an empty price is collected but is not a fill.
The first priced `partially_filled` / `filled` row wins.

## Rebuild an existing report (offline)

After a matcher fix, re-collect wire and rewrite JSON/HTML. Reuse
`signal_book` and `ticks` from the old `chronometry.json` if the live L1
ring is gone. Example:

```bash
PYTHONPATH=. python3 - <<'PY'
from pathlib import Path
import json
from app.bot.private.chronometry import (
    ChronometryContext,
    build_chronometry_artifact,
    write_chronometry_files,
)
from app.bot.private.l1_tick_ring import L1Tick, SignalBookSnapshot

root = Path("/data/bbot-canary-wal-eden")  # canary data root, not D trees
intent = "95790b7c-8b21-4e82-bae2-1832ddc5bee1"
old = json.loads((root / "reports/trades" / intent / "chronometry.json").read_text())
book = old.get("signal_book") or {}
snap = SignalBookSnapshot(
    bybit_bid=book.get("bybit_bid"),
    bybit_ask=book.get("bybit_ask"),
    okx_bid=book.get("okx_bid"),
    okx_ask=book.get("okx_ask"),
    spread_long_pct=book.get("spread_long_pct"),
    spread_short_pct=book.get("spread_short_pct"),
) if book else None
ticks = [L1Tick(**row) for row in (old.get("ticks") or [])]
ctx = ChronometryContext(
    intent_id=intent,
    base_coin=old["base_coin"],
    spread_side=old["spread_side"],
    phase=old.get("phase") or "open",
    signal_ts_ms=int(old["signal_ts_ms"]),
    data_root=root,
    open_spread_side=old.get("open_spread_side"),
    dual_leg_id=intent.replace("-", "")[:32],
    signal_book=snap,
    ticks=ticks,
)
write_chronometry_files(build_chronometry_artifact(ctx), data_root=root)
PY
```

`collect_wire_events` runs when `wire_events` is omitted. This is a local
rebuild of an existing report. It is not a VPS deploy.

## Tests

```bash
PYTHONPATH=. python3 -m unittest \
  tests.test_l1_tick_ring tests.test_chronometry_dashboard \
  tests.test_okx_ws_message_id tests.test_dual_leg_ack tests.test_canary_wal_eden -v
```

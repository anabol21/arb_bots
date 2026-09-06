# Private wire transcript (Contour B / canary)

Track 3 / B-private. Permanent, not an experiment flag. Every successful
send and recv on the **Bybit and OKX private + trade** sockets used by warm
Contour B is appended with wall and monotonic timestamps so chronometry
can be derived **after** the fact. The hot path still does not wait for
ack or fill.

## Layout

Under the private data root (never D trees):

```text
{BBOT_PRIVATE_DATA_ROOT}/wire/event_date=YYYY-MM-DD/wire.jsonl
```

Canary isolation: point the private root at the canary tree, e.g.

```text
BBOT_DATA_ROOT=/data/bbot-canary-wal-eden
BBOT_PRIVATE_DATA_ROOT=/data/bbot-canary-wal-eden/private
```

which is `{BBOT_DATA_ROOT}/private/wire/…`. Default without override remains
`/data/bbot/private/wire/`. Local fallback follows `resolve_data_root`.

One JSONL stream for all four sockets. Each row is one I/O. Schema
`bbot.private.wire.v1`. This file is **not** `bbot.private.journal.v1`.

## Who writes

`PrivateWarmSession` wraps the four sockets at bind / reconnect
(`WireTranscriptSocket`). Warm handshake, keepalive ping/pong, inbound
order/exec, and Contour B `queue → ws.send` all go through the same
`send_text` / `recv_text`. Hook runs **after** I/O succeeds. Timeouts and
failed sends do not write a row. Transcript failures never raise into the
send path.

A background append thread writes JSONL (`flush`, no `fsync`) so the
second leg is not gated on disk. Short `wire_out` / `wire_in` lines go to
the normal `bbot` logger with the same timestamps and no secrets.

## Row fields

| Field | Meaning |
|-------|---------|
| `wall_ms`, `mono_ns` | epoch ms and process monotonic ns at I/O success |
| `dir` | `out` or `in` |
| `venue` | `bybit` or `okx` |
| `socket` | `private` or `trade` |
| `run_id` | warm-session journal run |
| `reconnect_generation` | from `PrivateStreamRuntime` when available |
| `seq` | append order in this writer |
| `op` | venue `op` / `event` when JSON |
| `req_id` | Bybit `reqId` or OKX `id` |
| `intent_id`, `dual_leg_id`, `signal_ts_ms` | bound on place **before** `ws.send` |
| `venue_ts_ms` | first inbound `execTime` / `fillTime` / `uTime` / `cTime` / … |
| `payload` | **redacted** structured JSON |

## Redaction

Strip or replace: API keys, secrets, passphrases, signatures
(`sign`, `X-BAPI-SIGN`, `X-BAPI-API-KEY`), tokens. Bybit `auth` args
`[key, expires, sign]` keep only `expires`. Unparseable text is stored as
`{unparsed, payload_bytes}` — never the raw string.

Keep for chronometry and correlation: `op`, `reqId`/`id`, `orderLinkId`,
`clOrdId`, symbol, side, qty, retCode/code, and venue fill timestamps.

Overnight canary (2026-09-05, EDEN intent `653fc03f-…`): OKX inbound
`{"event":"error","msg":"Parameter id error","code":"60033"}` on outbound
`"id":"req_c08a00f2…"`. Cause is the message `id` underscore from
`new_opaque_id("req")`, not `clOrdId`. Fixed at the OKX frame boundary
(`sanitize_okx_ws_id` / `assert_okx_ws_message_id` in `ws_messages.py`).

## Derive the AB intervals (offline)

Join outbound place rows (`dir=out`, trade socket, `op` in
`order.create` / `order`) to later inbound rows on the same `req_id`
(ack) or `orderLinkId` / `clOrdId` / `intent_id` (fill on the **private**
stream).

| Interval | From the transcript |
|----------|---------------------|
| signal→send | `out.wall_ms − out.signal_ts_ms` (also stored as `signal_to_send_ms`) |
| send→ack RTT | `ack.in.mono_ns − out.mono_ns` (same `req_id`; this is RTT, not one-way) |
| `fill_delivery` | `in.wall_ms − in.venue_ts_ms` when the inbound fill carries Bybit `execTime` or OKX `fillTime` / `uTime` (also stored as `fill_delivery_ms` on that row) |

Fills appear on the private stream after send. Contour B does **not** wait
for them before marking position; it waits only for trade-socket **ACK**
(accept/reject/timeout). Read fill rows in the transcript (or `wire_in`)
once they arrive.

## Tests

```bash
PYTHONPATH=. python3 -m unittest tests.test_okx_ws_message_id tests.test_wire_transcript tests.test_trivial_dual_leg tests.test_dual_leg_ack tests.test_canary_wal_eden tests.test_bbot_gear2 -v
```

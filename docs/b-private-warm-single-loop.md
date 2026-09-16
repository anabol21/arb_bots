# Contour B private warm — single asyncio loop

Track: 3 Glue / B-private. Code only. **No VPS or live deploy in this change.**

Public L1 already uses one bot asyncio loop and concurrent
`async with websockets.connect` tasks (`app/bot/ws_books.py` `_listen_loop`).
Private warm did not.

## Why

VPS gear22 live-canary @ `3036cf35` / PR #51 still dropped OKX private
~every 30s after #50 (silence activity) and #51 (post-handshake app ping,
10s HB, single-venue disconnect):

```text
ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout;
no close frame received
```

That is the **websockets library** protocol ping. OKX private expects
application text `"ping"` / `"pong"`. `WebsocketsClientSocket` called
`websockets.connect(url, max_size=...)` with the **default**
`ping_interval`. Live effect: hundreds of `live_abort`
(`warm_session_not_ready`, `trivial_send_failed:okx`) while would_send
still opened.

A one-line `ping_interval=None` on the old thread wrapper would unstick
the canary but would leave thread-per-socket + `run_coroutine_threadsafe`
under an I/O lock — the shape that previously put ~513ms of keepalive
`recv_text` on signal→send. Mikhail asked for the public-books form:
**one asyncio cycle**, queue → immediate `ws.send`, no lease/journal on
the critical path.

## Architecture

```text
PrivateWarmLoop  (one thread, one asyncio loop)
  listen bybit private  ─┐
  listen bybit trade    ─┼─ async with websockets.connect(
  listen okx private    ─┤     ping_interval=None, ping_timeout=None)
  listen okx trade      ─┘
       │
       ├─ pump: async for message → inbound queue / handle_inbound
       ├─ app heartbeat (text ping) after handshake_done
       ├─ OKX literal ping → pong on the same task
       └─ reconnect with bounded backoff (public-style)

WarmConnector (Contour B live place)
  ready()            → session.is_ready()
  send_trade()       → ws.send on the owner loop; wait only for send
  recv_trade()       → inbound queue (listen owns ws.recv)
  place_io_section() → defers reconnect/teardown only; not a pre-send gate
```

Place path stays: strategy filters → signed frames →
`TrivialDualSender.enqueue_dual` → `WarmConnector.send_trade` → `ws.send`.
No new waits/gates. ACK wait remains **after** both sends.

Silence 45s and #50 activity-on-successful-heartbeat-send stay. #51
immediate post-handshake app ping stays (`_handshake_both` /
per-venue loop reconnect).

Public `ws_books.py` is unchanged (it still uses library ping=20, which
is correct for public books).

## Migration from `PrivateWarmSession` + `WebsocketsClientSocket`

| Before | After |
|--------|--------|
| 4× dedicated asyncio loop threads (`WebsocketsClientSocket`) | 1× `PrivateWarmLoop` |
| Keepalive thread polls `recv_text(0.2s)` | Listen task owns `ws.recv` |
| Default lib ping | `PRIVATE_WS_CONNECT_KWARGS` (`ping_interval=None`) |
| `place_io_section` paused keepalive recv so it would not steal ACKs | Listen queues trade inbound; counter only defers reconnect |
| `warm_trade_send_fn` → `sock.send_text` | `WarmConnector.send_trade` (same send, no recv lock) |

**Compat shim.** `WebsocketsClientSocket` remains for W4/W6 harnesses that
bind `WebsocketsSocketFactory`. It now connects with the same
`ping_interval=None` kwargs so a leftover wrapper cannot revive the 1011
storm. Hermetic tests that inject `FakePrivateWsSocket` still use the
polling keepalive thread. Live canary (`start_warm_private_for_bot_process`
without a test provider) takes the new loop.

**Not rewritten.** Dual-leg live send, chronometry/wire, Sentry tagging,
top30 coin pool, $20 notional, public L1, collector ingest/parse/spread.

## How Contour B attaches

1. `BotRuntime.run()` calls `start_warm_private_for_bot_process` **before**
   public book tasks (same as today).
2. Production provider `ensure_process_warm_loop()` + four `loop.open(...)`.
3. `PrivateWarmSession.start()` waits for connect, runs existing
   `_handshake_private_and_trade` + post-handshake app ping, sets
   `handshake_done` so loop heartbeat/watchdog start.
4. `LiveBroker._send_via_trivial` uses `get_process_warm_connector()`:
   `ready()` / `place_io_section()` / `warm_trade_send_fn` → `send_trade`.
5. `recv_trade_ack` pops the listen-owned inbound queue (plus stash).

## VPS verify (gear22 live-canary, after deploy — not this PR)

Watch `/var/log/spread/bbot-gear22-live-canary.log` (and private journal)
for many minutes with would_send opens:

- `handshake_count` stays **flat** while sockets are healthy
- no 1011 / `keepalive ping timeout` storm
- `live_abort` near zero when would_send opens (`warm_session_not_ready`
  / `trivial_send_failed:okx` should disappear)
- signal→send stays ms-class (not ~500ms keepalive-lock)
- OKX app ping/pong (`ws_heartbeat exchange=okx` and/or
  `ws_okx_pong_reply`); quiet Bybit + outbound ping still must not
  `ws_silence_timeout` (#50)
- real dead sockets must still reconnect (bounded backoff)

Do not treat this PR as VPS evidence. Local tests do not prove mounted
storage or live venue behaviour.

## Tests

```bash
PYTHONPATH=. python3 -m unittest tests.test_warm_single_loop tests.test_warm_ws_place_threadsafe -v
PYTHONPATH=. python3 -m unittest app.bot.private.selftest.WarmPrivateSessionTests -q
```

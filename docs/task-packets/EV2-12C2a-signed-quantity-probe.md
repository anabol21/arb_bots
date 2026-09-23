# EV2-12C2a — signed read-only quantity probe bound to EV2 WAL

Status: local callable and injected-response tests, **not runtime-wired**.
No target-VPS GET was executed. `publication_ready` is always false; live
startup remains blocked. This patch does not complete EV2-12C2.

## Pipeline block and existing files

```text
durable EV2 WAL + candidate (B1) -> re-inspect exact WAL state
  -> bind instrument/client ID/side to two REQUEST_SENT records
  -> four allowlisted signed read-only REST GETs
  -> bounded wall-time and Bybit server-time checks
  -> C1 native-quantity comparison
  -> non-publishing diagnostic result
```

`app/bot/execution/signed_quantity_probe.py` uses the existing allowlisted
signers in `app/bot/private/ws_w4_baseline.py`; it never constructs a place,
cancel or amend request. Its read endpoints are pinned to the live REST
hosts. The candidate must be recreated from the same healthy durable WAL
prefix, and the two request-send records must agree with the candidate's
intent, client IDs, sides and opening quantities. Credential-key hashes are
checked against a release-supplied manifest, but a key hash is **not** an
exchange account UID or ownership proof.

Bybit's [position endpoint](https://bybit-exchange.github.io/docs/v5/position)
and [open-order endpoint](https://bybit-exchange.github.io/docs/v5/order/open-order)
expose pagination cursors; C1 rejects any unread Bybit page. OKX's
[positions and pending-order APIs](https://www.okx.com/docs-v5/en/) are read
through the existing signed allowlist. This patch does not establish
complete OKX pagination, cross-request atomicity, or private-channel gap
closure; the result explicitly retains those required gates.

## Design choice, risks, and experiment

Accepting a caller-supplied JSON object as signed proof was rejected.
Reusing the ACK-based legacy broker's categorical side match was also
insufficient. The selected small adapter performs signed GETs only after
WAL/plan binding, then delegates native-quantity checks to C1. Both a
comparison mismatch and an inconclusive read deny publication. Even a
perfect comparison reports `publication_ready=false` because the four
requests are not one atomic exchange snapshot, Bybit server time alone does
not prove OKX freshness, and account UID/private reseed are still unknown.

The code is edited and tested locally; test responses are injected and no
network call is made. Tests cover the four signed allowlisted paths, WAL
tampering, wrong credential fingerprint, wrong endpoint, stale Bybit time,
slow request window, unread page, quantity mismatch, and a durable CLOSE
with both venues flat. Before VPS use,
run read-only against an isolated release with explicit secret handling and
capture only redacted metadata, WAL sequence/hash, elapsed time and result.
Do not log headers, API keys, signatures or raw responses. Collector and
continuing `would_sent` must remain independent.

Next: EV2-12C2b account UID/mode, complete OKX pagination, private
auth/reseed continuity and a coherent recheck protocol. Only after those
gates can B2c resolve the pending manager row and publish K=1 through a
fsynced lifecycle. No live order permission follows from C2a.

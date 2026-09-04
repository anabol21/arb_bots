# B-private — A/B send-path experiment (W6/manager vs queue→send)

**Read this first** before any VPS/live run. Track: glue / B-private.
Agents must not SSH, deploy, or send live orders. Humans only, after this
PR is in `dev`.

Print the live recipe anytime (no network):

```bash
python -m app.bot.private --ab-send-path --ab-print-vps-recipe
```

Related (not this experiment): Warm-Lat PR #15 (open, not on `dev`) measures
remaining place-path delay after warm. Dual-leg hot-path PR #14 (open, not
on `dev`) rewrites production prepare→enqueue. This PR **does not merge
those stacks**. It reuses **W6 + `ws_warm_session` already on `dev`**
(PR #12 warm, merged) and adds a thin Contour B module.

`else/bybit_ws.py` is **not** on this branch. Historic shape is
`a1ba2b1:bybit_ws.py` (repo-root `bybit_ws.py` at that commit).

---

## 1. What exactly is being tested

**Hypothesis.** After private+trade WS is already warm, the full W6
deal-manager stack adds **X ms** between an artificial dual-leg signal and
the first `ws.send`, versus the historic primitive:

```text
queue.put(bybit_order); queue.put(okx_order)
# long-lived sender: item = await queue.get(); await ws.send(...)
```

This isolates **handler / manager overhead**, not strategy.

| Question | Answer |
|----------|--------|
| Signal source | Artificial. No gear2 spread/MA. No collector ticks. |
| Clock start | `signal` — immediately after the 5 s warm hold |
| Primary metric | p50/p95 of `signal_to_first_request_sent` (A − B = manager X ms) |
| Secondary | p50/p95 of `signal_to_terminal_flat` (includes venue RTT + 5 s hold + close) |
| Not claimed | Profitability, `Trade_Lat` rewrite, live-bot readiness. Do not interpret A vs B as sequential vs parallel send. |

`Trade_Lat` in gear 1.0 stays **100 ms**. One VPS packet does not change it.

**Both contours place in parallel.** Classic sequential W6 (Bybit fill, then OKX) is **not** Contour A. Production live gear2 already calls `run_w6_dual_leg(..., parallel_open=True, parallel_flatten=True)`. Contour A must match that or the AB is unfair versus the bot the operator runs and versus Contour B’s dual `queue.put`. The primary metric `signal_to_first_request_sent` measures manager overhead **on that parallel path**.

---

## 2. What is held identical vs what differs

### Identical (fair AB)

| Piece | Value |
|-------|--------|
| Venues | Bybit live + OKX live |
| Symbols / qty | W6 TRUMP: `TRUMPUSDT` qty `4.0` buy; `TRUMP-USDT-SWAP` qty `40` sell |
| Notional class | ≈ $6–8 / leg, ≪ 100 USD/venue, `K_live=1` |
| Warm sockets | `PrivateWarmSession` private+trade both venues (PR #12) |
| Protocol | connect → wait 5 s → dual-leg **open** → wait 5 s → dual-leg **close** → shutdown → fresh session next trial |
| N | 5 trials A, 5 trials B (live start with **n=1**) |
| Public WS | optional / unused (no strategy) |
| Place shape | **Parallel place** of both legs at the same moment. A: `parallel_open=True` + `parallel_flatten=True` (same as `live_broker.default_live_send_pair`). B: dual `queue.put` in one `enqueue_dual`. Not classic sequential W6. |

### Differs (the independent variable)

| | Contour A — full manager | Contour B — primitive send |
|--|--------------------------|----------------------------|
| After `signal` | `_recover_inflight_w6` → `ApprovalVault.issue` → `ApprovalBoundSender.send_approved` (lease `assert_can_send`, preflight, revalidate, consume, `order_prepared` journal fsync) → `_WsTradePlaceTransport` / `send_trade_place` | `build_w6_dual_payloads` → `PrimitiveDualSender.enqueue_dual` (`asyncio.Queue.put` both) → long-lived sender `ws.send` / dry dump |
| Journal prepare on send path | Yes (`request_sent` fsync before transport) | No |
| Recover / leftover scan | Yes, on the critical path | Skipped |
| Operator approval | Required (`--ab-approve-one-shot` live) | Live still requires the CLI approve flag as a process gate; **not** on the send path |

Dry run (`--ab-send=false`, default): A exercises the real journal/vault/lease/prepare with `dispatch_transport=False` (would-send). Both venues are issued before prepare; would-send stamps for a wave share one monotonic instant (parallel-place analogue — journal is not assumed thread-safe, so dry does not spawn two W6 threads). B runs the real queue→sender against a fake `send_fn`. No sockets. Dry A `signal_to_first_request_sent` is local manager cost on the parallel-intent path, not VPS proof.

---

## 3. Scripts / modules (audit list)

### Shared

| Path | What it contains / critical-path functions |
|------|--------------------------------------------|
| `app/bot/private/ws_ab_send_path.py` | CLI + protocol. `parse_ab_send_path_cli_args`, `main_ab_send_path`, `run_contour_a_dry_trial`, `run_contour_b_dry_trial`, `_run_contour_a_live`, `_run_contour_b_live`. Live A calls `run_w6_dual_leg(..., parallel_open=True, parallel_flatten=True, hold_after_open_sec=5)` via `CONTOUR_A_LIVE_W6_KWARGS` (same intent as `live_broker.default_live_send_pair`). |
| `app/bot/private/ab_send_path_stages.py` | Stage labels, `StageTrace.mark`, p50/p95, JSON/CSV. No network. |
| `app/bot/private/ws_warm_session.py` | Process-lifetime private+trade. `start_warm_private_session`, `ensure_ready`, `place_io_section`. **Off** the post-signal path once warm. |
| `app/bot/private/ws_socket.py` | `PrivateWsSocket.send_text` / owner-loop (PR #12). Contour B live sender calls this. |
| `app/bot/private/ws_gates.py` | `assert_ws_ab_send_path_gates` — fail-closed before any live socket. |
| `app/bot/private/harness_readonly.py` | Dispatches `--ab-send-path` → `main_ab_send_path`. Default CLI still read-only. |
| `app/bot/private/ws_w6_dual_leg.py` | W6 profile + `hold_after_open_sec` (default `0`) + optional `parallel_flatten` (default `False` so classic W6/W7 CLI flatten stays sequential). Experiment-only kwargs; production W6 CLI unchanged. |

### Contour A only (critical path after `signal`)

| Path | Functions on the send path |
|------|----------------------------|
| `app/bot/private/ws_w6_dual_leg.py` | `run_w6_dual_leg` → `_recover_inflight_w6` → `vault.issue` → `_place_pair_parallel` → `sender.send_approved` → `_flatten_pair_parallel` |
| `app/bot/private/order_sender.py` | `ApprovalBoundSender.send_approved` — preflight, lease, consume, `_journal_prepared`, `_journal_request_sent`, `transport()` |
| `app/bot/private/order_approval.py` | `ApprovalVault.issue` / `consume` (journal `operator_approval`) |
| `app/bot/private/order_lease.py` | `LeaseSupervisor.assert_can_send` / reconstruct |
| `app/bot/private/order_preflight.py` | `assert_preflight_ready` (metadata + position mode) |
| `app/bot/private/ws_w5_market.py` | `_WsTradePlaceTransport.__call__` → `runtime.send_trade_place` → `recv_trade_ack` |

### Contour B only

| Path | Functions on the send path |
|------|----------------------------|
| `app/bot/private/ws_ab_primitive_send.py` | `build_w6_dual_payloads`, `PrimitiveDualSender.enqueue_dual`, async `_sender` (`queue.get` → `send_fn`). Historic analogue: `trade_manager` + `sender()` in `a1ba2b1:bybit_ws.py`. |

B may use warm `trade_socket.send_text` as `send_fn`. It does **not** call `send_approved`, vault, or recover on the send path. Live B `enqueue_dual` puts both legs inside one `place_io_section` with no sequential venue wait (no Bybit-fill-then-OKX).

---

## 4. Metrics / output schema

`schema_version`: `ab_send_path_results.v1`

Files under `--ab-out`:

| File | Contents |
|------|----------|
| `ab_send_path_results.json` | Full report (stdout mirrors this) |
| `ab_send_path_trials.csv` | Long-form: trial × interval |
| `ab_send_path_summary.csv` | contour × interval with p50/p95 |

### Stage labels (monotonic ns)

`warm_ready`, `signal`, `recover`, `operator_approval`, `lease`,
`order_prepared`, `first_request_sent`, `second_request_sent`, `first_ack`,
`second_ack`, `terminal_fill`, `close_signal`, `close_first_request_sent`,
`close_second_request_sent`, `terminal_flat`.

Contour B **skips** `recover` / `operator_approval` / `lease` / `order_prepared`.

### How to compare 5+5

1. Run A n=5 and B n=5 (or merge two JSON reports).
2. Read `summary.contour_A` and `summary.contour_B`.
3. Primary: `signal_to_first_request_sent` p50 and p95 (A − B = manager X ms on the **parallel** place path).
4. `contour_ab_delta_ms.A_minus_B_p50_ms` on that interval is **X ms**.
5. Also report `signal_to_terminal_flat` p50/p95 (venue + hold + close; do not treat as manager-only). Parallel place means both venues can accept at the same moment; secondary still waits for **both** legs to reach a terminal flatten state.

```bash
python3 - <<'PY'
import json
from pathlib import Path
for p in Path(".").rglob("ab_send_path_results.json"):
    d = json.loads(p.read_text())
    print(p, d["status"], d.get("notes", {}).get("flat_after"))
    for bucket, intervals in sorted(d.get("summary", {}).items()):
        for name in ("signal_to_first_request_sent", "signal_to_terminal_flat"):
            s = intervals.get(name) or {}
            if s.get("n"):
                print(f"  {bucket} {name}: p50={s['p50']:.3f} p95={s['p95']:.3f} n={s['n']}")
PY
```

Dry A vs dry B can be run in one working tree (`--ab-contour=A` then `--ab-contour=B`).
`contour_ab_delta_ms` is populated only when **both** contours are in the same
JSON (this CLI runs one contour per process — merge trials if needed).

---

## 5. Live gate recipe (human / VPS later)

**Environment claim:** VPS runtime `root@38.180.94.108`, code
`/root/spread_staging`, results `/data/bbot-gear2/private/ab_send_path/`.
This agent does **not** execute this. A local dry run is not VPS proof.

### Flags

| Gate | Value |
|------|--------|
| CLI | `--ab-send-path --ab-contour=A\|B --ab-n=1 --ab-send=true --ab-hold-sec=5 --ab-approve-one-shot` |
| Env | `VENUE=live` `LIVE_ORDERS=1` `BBOT_PRIVATE_AB_SEND=1` |
| Contour A also | `BBOT_PRIVATE_W6=1` (W6 recover/approval/lease path) |
| Secrets | `/etc/spread/bbot-private-live.env` mode 600, not git |
| Coin / notional | W6 TRUMP ~$6–8/leg — **do not** swap SOL/XRP gear2 sizes |
| n safety | **n=1 first**. Protocol N=5 only if n=1 is `status=ok` and `flat_after=true`. Hard cap live n≤5. |

Exact commands: `--ab-print-vps-recipe` or section 6 of that printer.

### Abort

If `status!=ok` or `notes.flat_after!=true`: **STOP**. Confirm both venues
flat (W6 baseline / UI). Do not raise n. Do not leave a one-leg position.

### Isolation

Write `/data/bbot-gear2/private/` only. Never `/data/live`, `/data/bars`,
`/data/compacted`, `/data/spool`. Do not restart `spread-collector`.

---

## 6. Out of scope

- Gear2 strategy (spread/MA, Top-N, size policy)
- Collector / ingest / parquet / D trees
- Permanently changing the live gear2 unit or default W6 CLI
- Merging PR #14 hot-path or PR #15 Warm-Lat
- Copying secrets from historic `bybit_ws.py` / `config.json`
- Host Ops agent
- Changing gear-1.0 `Trade_Lat`

---

## 7. Local dry (this PR)

```bash
python -m app.bot.private --ab-send-path --ab-contour=A --ab-n=5 --ab-send=false --ab-out=/tmp/ab_A
python -m app.bot.private --ab-send-path --ab-contour=B --ab-n=5 --ab-send=false --ab-out=/tmp/ab_B
python3 -m unittest tests.test_ab_send_path -v
```

Default hold is **0** when `--ab-send=false` so CI does not sleep 10 s/trial.
Live default hold is **5** (protocol).

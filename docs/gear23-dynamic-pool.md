# Gear 2.3 dynamic pool — Patch A

Gear 2.3 reuses would-send's cumulative hot-add CSV contract for the Gear 2.2 public market-data contour. The fixed configured pool remains the base. A delta snapshot can append candidates, starting one OKX books5 and one Bybit orderbook.1 public stream per accepted coin. Metadata is resolved through the same fail-closed lot/tick gate as would-send.

Patch A is public-only. It leaves every added coin out of the trade manager's coin order; a candidate can be observed and warmed but cannot be opened. It does not add private subscriptions, establish leverage, verify account settings, or enable order sending. A later patch must explicitly add readiness and eligibility gates before any candidate can reach the manager.

## Opt-in configuration

`BBOT_HOT_ADD=1` is off by default. Patch A accepts it only for a Gear 2.2 profile with `BBOT_BROKER=stub`, `LIVE_ORDERS` false, `BBOT_THETA_LIVE_SEND` false, and non-terminal-private execution. For a manual experiment use an isolated `BBOT_DATA_ROOT`, a separate `BBOT_HOT_ADD_DELTA` snapshot, and an environment without exchange credentials. Do not point it at the production would-send delta or share a live canary run root.

| Variable | Default | Meaning |
|---|---:|---|
| `BBOT_HOT_ADD` | off | Enable the public-only poller. |
| `BBOT_HOT_ADD_DELTA` | `hot_add_delta.csv` | Snapshot path; relative paths resolve under `BBOT_DATA_ROOT`. |
| `BBOT_HOT_ADD_MAX_EXTRA` | 8 | Maximum added coins. Set to the would-send configured cap for comparisons. |
| `BBOT_HOT_ADD_POLL_SEC` | 30 | Snapshot poll interval. |
| `BBOT_HOT_ADD_WARM` | on | Attempt read-only observer warm from available history. |
| `BBOT_HOT_ADD_HISTORY_ROOT` | data root | Optional alternate read-only warm-history root. |

The snapshot uses the existing `app/utils/universe_delta.py` columns. A candidate absent from the loaded universe must carry positive lot/tick/minimum metadata for both venues. Invalid metadata is logged and skipped. Missing snapshots do not remove the base pool. The production parser is cumulative and idempotent; Patch A is append-only and deliberately has no drop file behavior.

## Patch A checks

Offline checks are `python3 -m unittest tests.test_gear23_hot_add tests.test_hot_add_supervisor` and `python3 -m py_compile app/bot/runtime.py app/bot/hot_add.py app/bot/hot_add_warm.py app/utils/task_supervisor.py`. The bounded runtime experiment, after review, should use a dedicated run directory and a manually written CSV with one new candidate, then a second candidate. Confirm two public subscriptions per accepted candidate, duplicate snapshot no-ops, invalid metadata skip, explicit observer warm result, and unchanged trade-eligible base pool. Keep `LIVE_ORDERS=0`; no private session or exchange order is part of A.

The runner `validation/gear23_public_stub_experiment.py` reads three selected metadata rows from the would-send snapshot without changing it, waits until the original public pool is ready, then writes manual snapshots only under a new Gear 2.3 data root. It starts the bot with a scrubbed public/stub-only environment, executes the one/duplicate/two-additional/invalid sequence, and stops only its own child PID. A `PASS` requires subscribe ACKs and accepted ticks on both added symbols, usable theta rows for all three, duplicate idempotence, invalid metadata rejection, private/send paths skipped, no trade journal, clean task drain, and at least one successful history warm. If mechanics pass but no candidate fully warms, the result is `PARTIAL`, not `PASS`. Example:

```bash
/root/venv/bin/python validation/gear23_public_stub_experiment.py \
  --data-root /data/bbot-gear23-patch-a-<new-run-id> \
  --source-delta /data/bbot-would-send-prod/hot_add_delta.csv \
  --coins CT,AEON
```

Patch B must separately validate private subscription readiness, candidate metadata/1x preparation, fresh public generations, observer warm state, held/pending coin retention, and trade eligibility. It must reuse the active would-send selector's existing cumulative snapshot rather than run a second selector.

## Patch A VPS result (2026-10-06)

The bounded public/stub experiment passed. It read real metadata for CT, AEON,
and ARX from the active would-send delta snapshot, then applied a manual
cumulative CSV in an isolated Gear 2.3 data root. After the 29-coin base
runtime was ready, the runner added CT, rewrote the same snapshot to test
idempotence, then added AEON and ARX. Each coin received Bybit and OKX public
subscription acknowledgements, accepted TW rows (CT 26, AEON 6, ARX 6), and
usable theta rows (26, 6, 6). History warm succeeded for all three. Each was
registered once and remained `trade_eligible=false`. A malformed G23BAD lot
step was rejected and never added.

The validation child disabled only the order-entry callbacks. Private warm was
skipped, no simulated order or trade-journal row was recorded, and the process
stopped on SIGTERM with all supervised tasks drained. No private credentials or
exchange order APIs were used. The current live canary and would-send process
were not changed.

Artifacts are under `/data/bbot-gear23-patch-a-20261006-35d0f3c-r3/`; code is
under `/root/b-private-b-exp/response-manager-code/response-handler-20261005/gear23-patch-a-35d0f3c-r3/`.
The r1 packaging attempt omitted the tracked non-crypto denylist and failed
before runtime startup. r2 waited for an incorrect log prefix
(`bbot_hot_add_delta_read` instead of `hot_add_delta_read`), so it never wrote
candidate rows. Those are validation setup failures, not hot-add runtime
failures. r2's probe-mode stub fill stayed in its isolated data root and was
not an exchange order.

## Patch B implementation boundary

Patch B extends the same cumulative snapshot into the existing private-session
owner. It does not create per-coin sockets, another recv loop, or a second
selector. OKX additions require current-generation `orders` and `positions`
subscription acknowledgements matching instrument and channel. Bybit's
account-wide private subscription plus the updated symbol allowlist covers new
coins. Reconnect invalidates readiness until the new generation is authenticated,
subscribed, and reseeded.

An extra coin remains entry-ineligible until valid instrument metadata, current
public books, required history warmup, both exchanges' private readiness, and
existing preconfirmed 1x evidence are all present. Missing 1x confirmation for
an extra does not stop the base contour; dynamic refresh never sets leverage.
A held or pending coin stays available for close management. Gear 2.2 policy,
quantity calculation, K1 slot and halt behavior, order parsing, and the validated
send path remain unchanged.

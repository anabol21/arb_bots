# Gear 2.3 dynamic pool — Patches A and B

Gear 2.3 reuses would-send's cumulative hot-add CSV contract for the Gear 2.2 public market-data contour. The fixed configured pool remains the base. A delta snapshot can append candidates, starting one OKX books5 and one Bybit orderbook.1 public stream per accepted coin. Metadata is resolved through the same fail-closed lot/tick gate as would-send.

Patch A added public book tasks and watcher state. Patch B reuses the existing private session: Bybit's account-wide subscription gains the added symbol in its inbound allowlist, and OKX subscribes `orders` and `positions` for each added instrument on the current owner socket. No extra socket, recv task, selector, leverage request, or exchange setting change is introduced. New entries require valid metadata, a successful history warm, fresh books in both current public generations, current private readiness (including per-instrument OKX ACKs), and prep-confirmed 1x on both venues. A missing 1x confirmation blocks only that candidate. Existing held coins remain in the manager's pool for close handling.

## Opt-in configuration

`BBOT_HOT_ADD=1` is off by default. Public/stub mode accepts a Gear 2.2 profile with `BBOT_BROKER=stub` and live gates off. Private mode is limited to the existing fully gated Gear 2.2 private-live contour. The enabled run reads the complete cumulative snapshot before private startup, then polls that same snapshot for additions. For a manual public experiment use an isolated `BBOT_DATA_ROOT` and delta snapshot. The private readonly experiment uses the approved VPS live environment only on that host; it must not invoke an order path or alter leverage/account settings.

| Variable | Default | Meaning |
|---|---:|---|
| `BBOT_HOT_ADD` | off | Enable the dynamic coin pool. |
| `BBOT_HOT_ADD_DELTA` | `hot_add_delta.csv` | Snapshot path; relative paths resolve under `BBOT_DATA_ROOT`. |
| `BBOT_HOT_ADD_MAX_EXTRA` | 8 | Maximum added coins. The active would-send configuration uses a 48-extra cap; use that value for the bounded VPS comparison. |
| `BBOT_HOT_ADD_POLL_SEC` | 30 | Snapshot poll interval. |
| `BBOT_HOT_ADD_WARM` | on | Attempt read-only observer warm from available history. |
| `BBOT_HOT_ADD_HISTORY_ROOT` | data root | Optional alternate read-only warm-history root. |

The snapshot uses the existing `app/utils/universe_delta.py` columns. A candidate absent from the loaded universe must carry positive lot/tick/minimum metadata for both venues. Invalid metadata is logged and skipped. Missing snapshots do not remove the base pool. The production parser is cumulative and idempotent; Patch A is append-only and deliberately has no drop file behavior.

## Offline checks

Offline checks are `python3 -m unittest tests.test_gear23_hot_add tests.test_hot_add_supervisor` and `python3 -m py_compile app/bot/runtime.py app/bot/hot_add.py app/bot/hot_add_warm.py app/utils/task_supervisor.py`. The bounded runtime experiment, after review, should use a dedicated run directory and a manually written CSV with one new candidate, then a second candidate. Confirm two public subscriptions per accepted candidate, duplicate snapshot no-ops, invalid metadata skip, explicit observer warm result, and unchanged trade-eligible base pool. Keep `LIVE_ORDERS=0`; no private session or exchange order is part of A.

The runner `validation/gear23_public_stub_experiment.py` reads three selected metadata rows from the would-send snapshot without changing it, waits until the original public pool is ready, then writes manual snapshots only under a new Gear 2.3 data root. It starts the bot with a scrubbed public/stub-only environment, executes the one/duplicate/two-additional/invalid sequence, and stops only its own child PID. A `PASS` requires subscribe ACKs and accepted ticks on both added symbols, usable theta rows for all three, duplicate idempotence, invalid metadata rejection, private/send paths skipped, no trade journal, clean task drain, and at least one successful history warm. If mechanics pass but no candidate fully warms, the result is `PARTIAL`, not `PASS`. Example:

```bash
/root/venv/bin/python validation/gear23_public_stub_experiment.py \
  --data-root /data/bbot-gear23-patch-a-<new-run-id> \
  --source-delta /data/bbot-would-send-prod/hot_add_delta.csv \
  --coins CT,AEON,ARX
```

Patch B tests exercise per-instrument OKX ACK matching through the actual owner receive loop, candidate admission gates, held coin retention, and unchanged default Gear 2.2 pool behavior. The readonly VPS experiment reuses the active would-send cumulative snapshot after manually writing the prescribed test additions in an isolated run root; it does not start a second selector. Local code and tests live in the isolated checkout. VPS execution writes logs and runtime artifacts under the dedicated `/data/bbot-gear23-patch-b-*` run root; that VPS-local directory is the experiment's materialization and durability boundary, with no remote upload claim.

## Patch B VPS result (2026-10-06)

The private-readiness experiment completed the ordered CT, duplicate CT,
AEON+ARX, and invalid G23BAD snapshots against the active would-send CSV as a
read-only metadata source. All three candidates received both public ACKs,
successful observer warm, positive TW/theta rows (CT 36; AEON and ARX 16 each),
and current-generation OKX `orders` and `positions` ACKs matching their
instrument and unique request ID. Private readiness was true for all three;
all remained blocked by the expected missing prep-confirmed 1x. No candidate
was reported eligible. G23BAD failed closed and was not added.

The child used the existing private warm startup and its mandatory signed,
read-only REST reseed: 29 reads each for six account/position/instrument
endpoints (174 GETs total). These are the warm session's existing per-coin
balance, position, and instrument reads; no account-mode or leverage GET and no
setting request was made. The send-path entry manager and callbacks were disabled; socket
guards recorded zero blocked operations, the stub broker saw zero place
attempts, and no REST mutation transport was available. The process received
SIGTERM and drained all supervised tasks. Logs show one auth/login per venue,
generation 0 throughout, and the OKX private subscription count increased from
one startup subscribe to four total (three added instruments). These support
reuse of the current owner path. The run's late socket-identity sample occurred
after runtime cleanup had cleared socket references (`id(None)`), so that sample
does not independently prove object identity; a later harness change captures
socket identities immediately before cleanup and requires that check for PASS.
No second private run was made because it would repeat the mandatory account
reseed.

This run is functionally PASS with the socket-identity measurement limitation
above. Summary SHA-256: `f31c13131e538aaeb6b5cef395632369552c532471e926aa7555fd8afb7c381b`.
VPS log timestamps are UTC (the host timezone), so 11:23–11:24 UTC corresponds
to 14:23–14:24 MSK. VPS data/logs are under
`/data/bbot-gear23-patch-b-20261006-luna/`; the isolated
source copy executed at
`/root/b-private-b-exp/response-manager-code/response-handler-20261005/gear23-patch-b-20261006-luna/gear23/`.
These are VPS-local artifacts; remote durability/upload was not tested. The
Gear 2.2 live process and would-send production CSV were not modified.

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
A held position remains pinned for close management. Gear 2.2 policy, quantity
calculation, global K=1 pending/halt behavior, order parsing, and the validated
send path remain unchanged.

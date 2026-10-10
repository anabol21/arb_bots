# Gear 2.3 dynamic pool — Patches A and B

Gear 2.3 reuses would-send's cumulative hot-add CSV contract for the Gear 2.2 public market-data contour. The fixed configured pool remains the base. A delta snapshot can append candidates, starting one OKX books5 and one Bybit orderbook.1 public stream per accepted coin. Metadata is resolved through the same fail-closed lot/tick gate as would-send.

Patch A added public book tasks and watcher state. Patch B reuses the existing private session: Bybit's account-wide subscription gains the added symbol in its inbound allowlist, and OKX subscribes `orders` and `positions` for each added instrument on the current owner socket. No extra socket, recv task, or selector is introduced. By default, dynamic refresh does not change leverage. The optional `BBOT_HOT_ADD_SET_LEVERAGE=1` path schedules one supervised task for each newly added extra; it requires instrument-scoped flat positions and no open orders on both venues before setting only 1x, then requires an instrument-matched OKX cross leverage-info readback and Bybit position readback at 1x before marking the coin confirmed. Bybit position and open-order checks follow `nextPageCursor` through all pages (bounded at 100); an unresolved or cyclic cursor fails closed, while a cursor alone does not mean exposure. Any failed or incomplete preflight/readback leaves that candidate blocked without a setting POST on preflight failure. New entries also require valid metadata, a successful history warm, fresh books in both current public generations, and current private readiness (including per-instrument OKX ACKs). Existing held coins remain in the manager's pool for close handling.

## Opt-in configuration

`BBOT_HOT_ADD=1` is off by default. Public/stub mode accepts a Gear 2.2 profile with `BBOT_BROKER=stub` and live gates off. Private mode is limited to the existing fully gated Gear 2.2 private-live contour. The enabled run reads the complete cumulative snapshot before private startup, then polls that same snapshot for additions. `BBOT_HOT_ADD_SET_LEVERAGE` defaults off and is accepted only with Gear 2.3 `terminal_private`; when enabled, its per-extra setter/readback worker runs off the bot loop and does not retry failed setup. For a manual public experiment use an isolated `BBOT_DATA_ROOT` and delta snapshot. The private readonly experiments described below did not enable this setting.

| Variable | Default | Meaning |
|---|---:|---|
| `BBOT_HOT_ADD` | off | Enable the dynamic coin pool. |
| `BBOT_HOT_ADD_SET_LEVERAGE` | off | For newly hot-added private extras only, set leverage to 1 and require both readbacks before admission. |
| `BBOT_HOT_ADD_DELTA` | `hot_add_delta.csv` | Snapshot path; relative paths resolve under `BBOT_DATA_ROOT`. |
| `BBOT_HOT_ADD_MAX_EXTRA` | 8 | Maximum added coins. The active would-send configuration uses a 48-extra cap; use that value for the bounded VPS comparison. |
| `BBOT_HOT_ADD_POLL_SEC` | 30 | Snapshot poll interval. |
| `BBOT_HOT_ADD_WARM` | on | Attempt read-only observer warm from available history. |
| `BBOT_HOT_ADD_HISTORY_ROOT` | data root | Optional alternate read-only warm-history root. |

The snapshot uses the existing `app/utils/universe_delta.py` columns. A candidate absent from the loaded universe must carry positive lot/tick/minimum metadata for both venues. Invalid metadata is logged and skipped. Missing snapshots do not remove the base pool. The production parser is cumulative and idempotent; Patch A is append-only and deliberately has no drop file behavior.

## Offline checks

Offline checks include `python3 -m unittest tests.test_gear23_hot_add tests.test_gear23_hot_add_leverage tests.test_hot_add_supervisor` and scoped leverage-preparation tests. The bounded public runtime experiment, after review, should use a dedicated run directory and a manually written CSV with new candidates. Confirm public subscriptions, duplicate snapshot no-ops, invalid metadata skip, explicit observer warm result, and unchanged base pool. Keep `LIVE_ORDERS=0`; no private session or exchange order is part of A.

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
confirmed 1x evidence are all present. Missing 1x confirmation for an extra
does not stop the base contour. Optional dynamic 1x preparation applies only to
new extras, runs once in a supervised worker, and marks confirmation only after
both venue readbacks match; it is disabled by default. A held position remains
pinned for close management. Gear 2.2 policy, quantity calculation, global K=1
pending/halt behavior, order parsing, and the validated send path remain
unchanged.

## B2.3 branch and runtime boundary

The B2.3 branch is based on the validated preB2.2 bot plus Gear 2.3 patches A/B
and the reviewed close-PnL correction (`170f832`). Its runtime is the
Gear 2.3-enabled canary implementation. Production would-send and the
expand-only daily selector continue running from the separate main contour;
B2.3 reads that contour's cumulative CSV as an input and does not deploy or
start another selector. Main's collector and storage changes remain on main
and are not part of this canary code archive.

The active would-send snapshot contains 29 base coins and 30 unique extras
(59 total) under a 48-extra cap. B2.3 consumes the complete snapshot at
startup and polls every 30 seconds, warming extras from
`/data/bbot-would-send-prod-history`. Extras stay blocked without confirmed
1x on both venues. Dynamic refresh does not change leverage by default; the
opt-in `BBOT_HOT_ADD_SET_LEVERAGE=1` path prepares unconfirmed extras after
flat/no-open-order checks. On 2026-10-08, five extras were newly confirmed at
1x on both venues during the canary restart. The current canary window began
flat at 2026-10-06 12:27:02 UTC and ends at 2026-10-09 12:27:04 UTC. Full
launch and validation chronology is in
[`docs/gear23-live-canary-2026-10-06.md`](gear23-live-canary-2026-10-06.md).

The standalone live process is started by the guarded launcher in `/tmp` and
retains its Sentry env-file policy. Daily compaction runs separately at 00:15
UTC against only that run's `data/` event-date partitions for `theta`,
`tw_p50`, `floor`, and `theta_trades`. The current compactor writes Parquet
after the 12-hour age gate and keeps the source JSONL; the sibling `private/`
journal and the process text log are outside its scope.

## TW deque and unlimited live restart (2026-10-10)

B2.3 now uses the verified `deque` implementation for TW-p50(1m), with the
one-minute-only snapshot schema. The B2.3 contour drops the unused five-minute
snapshot fields while leaving floor policy, theta policy, and private send
unchanged. A hedge-mode-safe hot-add leverage preflight still requires both
venues flat and without open orders; Bybit readback accepts `positionIdx` 0, 1,
or 2 only when all matching rows have zero size and 1x buy/sell leverage. On
the 2026-10-10 live restart, Bybit returned a second same-symbol cursor row with
only `size=0`, `positionIdx=0`, and stale `leverage=10`; `positionStatus` and
`updatedTime` were empty. The leverage readback treats rows with both fields
populated as current, requires at least one, and checks all such rows for 1x.
The flat/open-order preflight still examines every page and row. The set request
uses equal buy/sell leverage of 1, as required for cross margin in both Bybit
position modes.

The refreshed process reads the 29 fixed base coins plus the live would-send
cumulative delta (32 extras at launch; 61 total) from
`/data/bbot-would-send-prod/hot_add_delta.csv`. It retains the existing live
Sentry env-file setup and VPS data root. `BBOT_CANARY_MAX_CYCLES=0` and an
unset `BBOT_CANARY_OPEN_WINDOW_HOURS` remove both stop limits. Startup must
confirm flatness for the full 61-coin set before public signal tasks start.

Focused validation ran 123 tests on VPS Python 3.10, including exact TW
watcher checks, Gear 2.3 hot-add/leverage, private flat guards, and Sentry
events. The 10-minute 360-message/s deque comparison is recorded in
[`docs/would-send-cpu-canary-20261010.md`](would-send-cpu-canary-20261010.md):
exact reference parity, 23.331 CPU-seconds vs 364.2 for the legacy watcher.
The 1m snapshot-schema change is covered by B2.3 tests.

Runtime source and test code are staged under
`/root/b-private-b-exp/response-manager-code/gear23-b2.3-tw-deque-20261010/`.
The standalone process uses the existing run root
`/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun/` so
Grok/Sentry continue to watch the established logs and state. Logs and runtime
files are VPS-local; remote backup durability was not revalidated.

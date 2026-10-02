# Notes — snapshot limits

Date of this docs layer: 2026-09-19. Branch from `main` @ `bfc5a29`. No application code.

Lite was **not** run in the agent VM (no Docker). **Verify Structurizr Lite locally** with the **pinned** command in [`README.md`](README.md) before treating the DSL as rendered-proof. Mac 2026-09-19: untagged `structurizr/lite` (`latest`) printed a deprecation banner and exited; `structurizr/lite:2025.11.08` served diagrams.

## What this snapshot is

- Human Level 1 (system context) + six Level 2 container views + **15 Level 3 component views** (one per clickable process).
- Topology source: store recon `internal/human-contours-recon.md` (do not invent extra contours).
- Live prices / Stub B / Live send / Simulator / Canary B **description**: `main` `deploy/systemd/` + `app/bot/**` + `app/storage/**` + `app/screaner_b_o.py` hooks.
- Canary prices **only**: PR #53 `cursor/hot-add-new-coins-58c9` (discovery + hot-add). Inspected via `git show`; **not** mixed onto Live prices. Those files are still absent on this branch/`main`.
- Cross-check: root `architecture.md` on `main` (merged PR #55). Human **Canary B** ≠ architecture name “Contour B”.

## Unknown / do not over-claim

- This change did not SSH. VPS enablement after the recon snapshots is unknown.
- 2026-09-18 ~14:04 UTC (recon): `spread-collector` + `spread-bbot-theta-k1-canary` active; stub / gear2 / WAL-EDEN / gear22-live **inactive**.
- 2026-09-19 13:50–19:32 UTC (recon): hot-add canary + discovery timer also up; prod still 188 pairs. Later listing event: unknown.
- `spread-bbot-theta-k1-canary` **unit body not in git**. Label Canary B as VPS-only until the unit is committed.
- WAL/EDEN unit does not set `BBOT_PRIVATE_DATA_ROOT` and denies `/data/bbot`. Where that canary writes private/wire files at runtime: **unknown**.
- Production collector has bars collection off; bars compact/backup units still exist. Who writes `/data/bars`: **unknown** (same TODO as architecture.md).
- Compactor cadence: timer 2 min vs cron snippet 5 min vs service `--interval 300`. Which is installed: **unknown**.
- No backup units in git for Live send data roots or Canary B.
- Git unit files are templates. Presence in `deploy/systemd/` is not `systemctl enable`.
- Secrets and env **values** are out of scope (names of gates only).

## Not views (on purpose)

Paper process, B-private CLI, `private_testnet` broker, local lean collector, Telegram alerts, execution-v2 library (not imported by runtime), compact/backup as separate views, VPS `spread-bbot-floor-canary` (not in git; floor watcher is in-process on Canary B).

## Diff vs `architecture.md` (PR #55)

| | `architecture.md` (agent snapshot) | `docs/human/` (this layer) |
|---|---|---|
| Audience | Agents; read before non-trivial work | Humans; Lite + mermaid |
| Location | Repo **root** on `main` (merged #55) | `docs/human/` on this PR |
| Mix with PR #53 | Explicitly **no** (no `app/discovery/`) | Canary prices is a **separate** view from #53 only |
| Process map | One mermaid with D + several B units together | One container view **per** contour; no mixed topology |
| Name “Contour B” | Default live `queue → ws.send` path | Human view **Contour — Live send**. Human **Contour — Canary B** is would_send, no send |
| Paper | None | None |
| Compact/backup | Inside D data flow | Inside **Contour — Live prices** only |
| B-private CLI | Not a live contour | No view |
| VPS active/inactive | TODO verify | Cites recon dates; still not a live `systemctl` from this agent |
| Secrets | Names/paths only | Same |
| Structurizr | No | `c4/workspace.dsl` (L1 + L2 + L3 code views) |
| AGENTS.md | Pointer to `architecture.md` | One line: human diagrams → `docs/human/` |

Do not merge this PR with #53. #55 is already on `main` (agent snapshot). When topology changes (new long-running process, data root, or send class), update **both** layers in their own changes: agent snapshot and these views.

## DSL / Lite caveats

- Pin **`structurizr/lite:2025.11.08`**. Do not use untagged `structurizr/lite` (`latest`): that image is a stub (deprecation banner, exit 0, no HTTP server).
- Lite **2025.11.08 rejects `containerDb` inside `group`** (21 stores on this model). Journals stay **in** contour groups as `container` + `tags "Database"` so they still draw as cylinders. Do not restore `containerDb` in groups. No local overlay should be required after this file.
- Contour view **titles** are the six human names. Contour **keys** are contour-ids (`d-live`, …). Code view titles are `Code — <process block name>`. Code **keys** are `code-*`.
- Level 1 is the **union** of capabilities (including «отправка ордера»). It does not mean every contour sends.
- Pipe shape = in-process queue, not a second systemd unit. Queues have **no** component view.
- Simulator reads historical files; it is not drawn as a client of the live collector process.
- Bars boxes on Live prices are companions; the missing writer is an unknown, not a hidden seventh contour. The collector **code** for bars exists (`SPREAD_COLLECT_BARS`) and is drawn on **Code — Сбор цен** as «запись баров (флаг выключен)». Production unit still has the flag off.

## Click-through coverage

Lite drills down when a **component view** exists for that container.

**Have drill-down (15 process blocks):**

| Contour | Clickable block | Code view |
|---|---|---|
| Live prices | Сбор цен | Code — Сбор цен |
| Live prices | Уплотнение тиков | Code — Уплотнение тиков |
| Live prices | Копия тиков | Code — Копия тиков |
| Live prices | Уплотнение баров | Code — Уплотнение баров |
| Live prices | Копия баров | Code — Копия баров |
| Canary prices | Поиск новых пар | Code — Поиск новых пар |
| Canary prices | Сбор цен (изолированный) | Code — Сбор цен (изолированный) |
| Stub B | Решение без отправки | Code — Решение без отправки |
| Stub B | Копия журнала | Code — Копия журнала |
| Stub B | Решение без отправки (4 монеты) | Code — Решение без отправки (4 монеты) |
| Stub B | Копия журнала (4 монеты) | Code — Копия журнала (4 монеты) |
| Canary B | Решение раз в секунду, без отправки | Code — Решение раз в секунду, без отправки |
| Live send | Решение и отправка (2 монеты) | Code — Решение и отправка (2 монеты) |
| Live send | Решение и отправка (30 монет) | Code — Решение и отправка (30 монет) |
| Simulator | Прогон истории | Code — Прогон истории |

**No drill-down (on purpose):**

- All cylinders (ticks, spool, gaps, compacted, bars, journals, metrics, wire, delta/drop, historical tables).
- All pipes (publish queues, order queues). The queue **class** is on the process code view (`queue.Queue` in `app/storage/writer.py`; `asyncio.Queue` in `app/bot/private/ws_trivial_dual_leg.py`).
- Externals and the person.
- **Бары (писатель неизвестен)** — store only; writer on VPS still unknown. Collector bars code is on **Code — Сбор цен**, flag off.

**Not drawn as code boxes (unknown or out of scope):**

- Canary B **unit file** (not in git) — process box cites VPS name only.
- WAL/EDEN **private/wire path** — no «журнал провода» component on the 2-coin code view.
- Optional W6 send path — **off** on these units; not a box on Live send code views.
- `floor_warm` pickle, Sentry, B-private CLI, local lean collector.
- Discovery/hot-add files exist only on PR #53; this branch documents them, it does not copy that tree.

This VM: no Docker daemon, Lite GUI **not** re-run. CLI v2025.11.09 `export -format json` of this DSL: 22 views (1 context + 6 container + 15 component). Click-through in the GUI still needs a local Lite check.

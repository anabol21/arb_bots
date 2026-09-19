# Notes — snapshot limits

Date of this docs layer: 2026-09-19. Branch from `main` @ `bfc5a29`. No application code.

Lite was **not** run in the agent VM (no Docker). **Verify Structurizr Lite locally** with the **pinned** command in [`README.md`](README.md) before treating the DSL as rendered-proof. Mac 2026-09-19: untagged `structurizr/lite` (`latest`) printed a deprecation banner and exited; `structurizr/lite:2025.11.08` served diagrams.

## What this snapshot is

- Human Level 1 (system context) + six Level 2 container views.
- Topology source: store recon `internal/human-contours-recon.md` (do not invent extra contours).
- Live prices / Stub B / Live send / Simulator / Canary B **description**: `main` `deploy/systemd/` + `app/bot/**`.
- Canary prices **only**: PR #53 `cursor/hot-add-new-coins-58c9` @ `0ce5da6`. That view must not be copied onto the Live prices diagram.
- Cross-check: `architecture.md` on PR #55 `cursor/architecture-snapshot-c20d` @ `de59153` (root file; empty/missing on `main`).

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
| Location | Repo **root**; **not on main** | `docs/human/` on this PR |
| Mix with PR #53 | Explicitly **no** (no `app/discovery/`) | Canary prices is a **separate** view from #53 only |
| Process map | One mermaid with D + several B units together | One container view **per** contour; no mixed topology |
| Name “Contour B” | Default live `queue → ws.send` path | Human view **Contour — Live send**. Human **Contour — Canary B** is would_send, no send |
| Paper | None | None |
| Compact/backup | Inside D data flow | Inside **Contour — Live prices** only |
| B-private CLI | Not a live contour | No view |
| VPS active/inactive | TODO verify | Cites recon dates; still not a live `systemctl` from this agent |
| Secrets | Names/paths only | Same |
| Structurizr | No | `c4/workspace.dsl` |
| AGENTS.md | Pointer to `architecture.md` (on #55) | One line: human diagrams → `docs/human/` |

Do not merge this PR with #53 or #55. When topology changes (new long-running process, data root, or send class), update **both** layers in their own changes: agent snapshot and these views.

## DSL / Lite caveats

- Pin **`structurizr/lite:2025.11.08`**. Do not use untagged `structurizr/lite` (`latest`): that image is a stub (deprecation banner, exit 0, no HTTP server).
- Lite **2025.11.08 rejects `containerDb` inside `group`** (21 stores on this model). Journals stay **in** contour groups as `container` + `tags "Database"` so they still draw as cylinders. Do not restore `containerDb` in groups. No local overlay should be required after this file.
- View **titles** are the six human names. View **keys** are contour-ids (`d-live`, …) for stable URLs.
- Level 1 is the **union** of capabilities (including «отправка ордера»). It does not mean every contour sends.
- Pipe shape = in-process queue, not a second systemd unit.
- Simulator reads historical files; it is not drawn as a client of the live collector process.
- Bars boxes on Live prices are companions; the missing writer is an unknown, not a hidden seventh contour.

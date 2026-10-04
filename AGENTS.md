# AGENTS.md

## Overview & Scope
This repository contains a crypto-arbitrage spread collection and data-engineering pipeline.

The main runtime entrypoint is:

- `app/screaner_b_o.py`

The same runtime may be used in different contexts:

- local development
- VPS runtime
- mounted remote storage reachable from the VPS

Agents must always distinguish these contexts explicitly when reasoning about bugs, validation, and runtime behavior.

## Architecture context (required)
Before non-trivial work, read:
1. `architecture.md` (canon: contours, journals, live gates, glossary)
2. `.cursor/rules/` — especially `00-project-focus.mdc`, `70-b-private.mdc` when the task touches B
3. Then the specific module files linked from Module Map / Critical Paths

`docs/architecture.md` is only a pointer to the root file. Do not maintain a second map there.

When you change topology (new process, send path, journal layout, contour boundary):
update `architecture.md` in the same change.
Do not put secrets in docs.

## Current Priority
The repository is three contours, not a storage-only project. Storage work is contour **D**. It is not the sole goal of the repo.

- **D** — public collector and persistence (`app/screaner_b_o.py`, `/data/live`). Inside D, reliability still matters: VPS stability, write/flush/save observability, restart safety, reproducibility for later replay. The storage design inside D is not finally decided. The coded path is hybrid (local hive, spool on failure, compaction, rclone). Compare candidates only when that boundary is actually open (see `.cursor/rules/10-storage-scope.mdc`).
- **M** — historical simulation only (`docs/strategy-gears.md`). Closing a gear is not live readiness and is not a D task.
- **B** — glue. Contour B (queue → `ws.send`) is unlocked in `app/bot/private/**`. Live send stays fail-closed.

Do not widen a D persistence task into ingest, model search, or live send.

## Frozen Areas
Unless the user explicitly unlocks them, treat these areas as frozen:
- websocket ingest logic
- exchange parsing
- spread calculation
- signal/trading logic inside `app/screaner_b_o.py`
- live order routing outside `app/bot/private/**` (Contour B is unlocked there; testnet/demo first; live send only behind the env gates below)
- unrelated strategy experimentation

Do not modify frozen areas just because storage behavior is problematic.

## Agent Role
Act as a skeptical data-engineering and reliability assistant.

Optimize for:
- correctness
- explicit invariants
- observability
- failure visibility
- restart safety
- reproducibility
- small reviewable diffs

Do NOT optimize for:
- large rewrites
- speculative abstractions
- architecture astronautics
- “clever” but untestable solutions

## Core Working Principles
- Prefer explicit failure over silent corruption.
- Prefer small experiments over early architectural commitment.
- Prefer instrumentation before optimization.
- Prefer local, narrow refactors over broad repo-wide rewrites.
- Prefer evidence from runtime behavior over intuition.
- Never confuse local success with VPS success.
- Never confuse VPS success with confirmed remote-storage correctness.

## Environment Model
Every storage-related task must explicitly name:

1. where the code is edited
2. where the script is executed
3. where runtime logs are emitted
4. where files are first materialized
5. where files are considered durably stored

If any of these are unclear, the task is under-specified and the agent should say so.

## Build, Test & Validation Commands
Use fast, scoped, non-destructive commands first. Run them from the repository root (whatever checkout you have). Do not assume a fixed desktop path.

Python syntax check:
```bash
python3 -m py_compile app/screaner_b_o.py
```

Mount validation:
```bash
python3 validation/check_mount.py
```

Lifecycle validation:
```bash
python3 validation/check_file_lifecycle.py
```

Use these as starting points. Add more targeted commands only when required by the task.

## Conventions & Patterns
- Main runtime script: `app/screaner_b_o.py`
- Storage-related helpers should gradually move into:
  - `app/storage/`
  - `app/schema/`
  - `app/utils/`
- Validation logic belongs in `validation/`
- Documentation and operational instructions belong in `docs/`
- Historical model-gear ladder: `docs/strategy-gears.md`. Current project vector: `roadmap.md`. Host snapshot: `NOW.md`
- Research and offline analysis belong in `research/`
- Keep runtime code and offline research code separate
- Prefer structured logs over ad-hoc print debugging
- Keep path handling explicit and centralized when possible

## Development tracks (lines of work, not necessarily git branches)
Same D / M / B split as `architecture.md` §3. Not three competing goals, and not "storage is the whole repo".

1. **D — collection / storage** — VPS, persistence (`app/screaner_b_o.py`). Reliability priority **of this contour only**.
2. **M — model** — simulated historical runs only. Closed history in `docs/strategy-gears.md`: **1.0** → **1.5** → **2** (contour; 2.2 out of scope) → **2.2** observation (1 Hz dummy replay in `research/gear22_backtest/`, frozen knobs, `spread_last` not `Trade_Lat`). That close is not the current plan. Forward patches (would_send canary, 2.2 bot contour, 2.3, 2.4, 2.5, 2.7, 3) are `roadmap.md`. An async live bot stays **out of scope** for M.
3. **B — glue** — joins collection, model, and trades. Spec: `docs/b-v0-block-diagram.md`. Stub `would_send` lives in `app/bot/**` (not `private/`) and must not touch D trees. **Contour B is unlocked** in `app/bot/private/**` (2026-08-18): testnet/demo first; live `ws.send` only with `BBOT_BROKER=private_live` and `VENUE=live` and `LIVE_ORDERS=1`. Private APIs stay out of the collector. Prod would_send ops: `docs/would-send-prod-status.md`. `docs/b-bot-starter-prompt.md` is a HISTORY stub-chat prompt, not the current prod unit.

Gear closure in M is simulator-only. It does not replace D reliability work and it does not authorize live send.

## Architectural Decision Discipline
For storage architecture tasks, do not jump directly to implementation.

First compare up to three candidate designs, such as:
1. direct write to mounted storage
2. local staging + background uploader
3. hybrid / fallback model

For each candidate, reason about:
- data integrity risk
- runtime blocking risk
- restart recovery
- mount dependency
- observability
- operational complexity
- silent-loss risk

Then recommend the next experiment or implementation step.

## Dos and Don’ts

Do:
- inspect runtime/logging/storage assumptions before changing code
- name the suspected failure mode before patching
- ask whether the issue belongs to runtime, storage, validation, or schema
- add observability before adding concurrency or retries
- propose minimal experiments to reduce uncertainty
- separate architecture exploration from code implementation
- keep code changes incremental and reviewable

Do not:
- rewrite ingest because of storage bugs
- commit to a storage pattern without comparison
- claim local-only success as proof of production correctness
- mix research/report code into runtime modules
- introduce broad abstractions without evidence they are needed
- silently swallow exceptions in persistence paths
- make destructive operational assumptions about VPS or storage

## Glossary (short)
Full table: `architecture.md` §11. Rules: `.cursor/rules/`.

| Term | Meaning here |
|------|----------------|
| D | Public collector contour (`screaner_b_o.py`, `/data/live`) |
| M | Historical model and gears. Not a VPS order process |
| B / B-private | `app/bot/**` / `app/bot/private/**` |
| Contour B | Default live send path: queue → `ws.send` |
| would_send | Journaled intent. Stub rows keep `send=false` |
| live send | Fail-closed. Needs `VENUE=live` and `LIVE_ORDERS=1` (and `BBOT_BROKER=private_live`) |
| paper | Not a coded process mode. Do not invent one |

## Safety & Guardrails
Live send is not the default. Do not flip a stub or would_send unit into a sender. Do not set `VENUE=live` or `LIVE_ORDERS=1` unless the task is an explicit live-send step on Contour B. Missing either flag must fail closed. GREEN would_send is not live permission. No secrets in git, docs, chat, or logs. Risk cap on live ≈ 100 USD per exchange.

Do not stop, restart, or disable these units without an explicit ask (templates exist in `deploy/systemd/`):

- `spread-collector-next.service` — live D writer
- `spread-bbot-would-send-prod.service` — prod would_send stub
- `spread-bbot-gear22-live-canary.service` — gear 2.2 live canary

Do not enable `spread-collector.service` over the next writer.

Never do without explicit approval:
- delete or bulk-move datasets
- truncate runtime logs
- kill unrelated VPS processes
- edit SSH credentials, keys, secrets, or mount configs
- modify remote storage contents destructively
- change service/process manager configuration
- mass-rename runtime paths that may affect production behavior

Safe operations:
- read-only inspection of paths, logs, mount state, and file states
- syntax checks
- creation of validation scripts
- addition of structured logging
- local refactors confined to storage/reliability scope
- design comparison documents and experiment plans

## Required Output Format
For every substantial task, answer in this structure:

1. Pipeline block
2. Existing files/modules involved
3. Candidate interpretations or candidate designs
4. Key risks and failure modes
5. Minimal patch or experiment plan
6. VPS/storage validation plan
7. Success criteria
8. Recommended next step

## Git & PR Rules
- One task, one focused diff.
- Keep schema, writer/uploader, and validation changes logically separated when possible.
- Every storage-related fix must include a validation plan.
- Prefer reviewable incremental changes over full rewrites.
- If architecture is still uncertain, propose the experiment before the refactor.
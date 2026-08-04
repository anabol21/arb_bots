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

## Current Priority
Current repository priority is data-engineering reliability, not strategy expansion.

Primary focus:
- reliable persistence of spread data
- runtime stability on VPS
- correctness of storage behavior on mounted remote storage
- observability of write/flush/save behavior
- validation of alternative storage designs
- reproducibility for later replay, backtest, and research

The storage architecture is NOT considered finally decided.

Agents may evaluate multiple persistence strategies, including:
- direct writes to mounted storage
- local staging plus background upload
- hybrid or fallback-based approaches

The goal is not to defend one preferred design.
The goal is to identify the most reliable design under realistic runtime constraints.

## Frozen Areas
Unless the user explicitly unlocks them, treat these areas as frozen:
- websocket ingest logic
- exchange parsing
- spread calculation
- signal/trading logic
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
Use fast, scoped, non-destructive commands first.

Repository inspection:
```bash
cd /Users/mishatrubik/Desktop/spread && find . -maxdepth 4 -type f | sort
```

Python syntax check:
```bash
cd /Users/mishatrubik/Desktop/spread && python3 -m py_compile app/screaner_b_o.py
```

Mount validation:
```bash
cd /Users/mishatrubik/Desktop/spread && python3 validation/check_mount.py
```

Lifecycle validation:
```bash
cd /Users/mishatrubik/Desktop/spread && python3 validation/check_file_lifecycle.py
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
- Strategy gear roadmap: `docs/strategy-gears.md`
- Research and offline analysis belong in `research/`
- Keep runtime code and offline research code separate
- Prefer structured logs over ad-hoc print debugging
- Keep path handling explicit and centralized when possible

## Development tracks (lines of work, not necessarily git branches)
1. **Data collection / storage reliability** — VPS, mount, persistence (`app/screaner_b_o.py`). Current reliability priority.
2. **Model** — build and validate the strategy on **simulated historical runs** only (`model.ipynb`, gear ladder in `docs/strategy-gears.md`: **1.0** closed → **1.5** regime screener → **2** multi-coin fixed model → **2.5** size policy → **3** parameter search on anomaly episodes). Validation remains **backtest / historical simulation**, not live trading. An async live trading bot is **out of scope** for the model track.
3. **Glue (future)** — one architecture joining collection, training history, live model metrics, and trades.

Strategy documentation does not replace the storage-reliability priority. Live-bot integration belongs to track 3; gear 1.0 closure was simulator-only.

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

## Safety & Guardrails
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
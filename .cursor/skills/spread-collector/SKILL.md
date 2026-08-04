---
name: spread-collector
description: Safely changes or inspects the spread collector runtime while protecting frozen WebSocket ingest, exchange parsing, spread calculation, and strategy logic. Use for app/screaner_b_o.py, app/screaner_local_lean.py, collector startup, runtime flags, logging, or collector-to-storage integration.
---

# Spread Collector Workflow

## 1. Classify the requested block

Separate:

- process startup and configuration;
- WebSocket ingest;
- exchange parsing;
- spread calculation;
- event construction;
- persistence handoff;
- runtime observability and shutdown.

WebSocket ingest, parsing, spread calculation, and trading logic are frozen unless the user explicitly unlocks the exact block.

## 2. Identify the environment

- `app/screaner_b_o.py` is the production VPS entrypoint.
- `app/screaner_local_lean.py` is a local lean/bars experiment.
- Local `output/lean_*` results do not validate production paths or mounted storage.

Record the runtime command, configuration source, log path, output root, enabled flags, and expected shutdown behavior.

## 3. Route the change

- Persistence hook or lifecycle: use `spread-storage` and Runtime Storage Agent.
- Event fields or parquet layout: use `spread-schema-handoff` and Schema Contract Agent.
- Runtime-only startup/logging: keep the patch inside the collector boundary.
- Model request: document the data contract; do not embed model logic in the collector.

## 4. Keep the patch narrow

- Preserve exchange subscriptions and parser semantics.
- Preserve spread formulas unless explicitly unlocked.
- Keep production flags default-safe.
- Keep local lean paths isolated from production paths.
- Add structured logs at boundaries rather than ad-hoc prints.
- Make shutdown and error propagation explicit.

## 5. Verify

Run a syntax check and the smallest scoped test. For storage behavior, complete the `spread-storage` validation handoff.

Report:

1. collector block;
2. environment and entrypoint;
3. frozen boundaries;
4. changed configuration or integration point;
5. scoped verification;
6. remaining VPS/storage validation.

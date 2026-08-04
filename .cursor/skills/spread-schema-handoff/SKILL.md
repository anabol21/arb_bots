---
name: spread-schema-handoff
description: Defines and validates parquet schema handoffs between the collector, writer, validation scripts, and historical model. Use for app/schema, storage-contract, data-format documents, v1 or lean ticks, bar_5m, partitioning, field compatibility, or model input changes.
---

# Spread Schema Handoff

Use `.cursor/agents/schema-contract-agent.md` as the semantic owner.

## 1. Map the contract

Identify:

- producer;
- writer;
- dataset root and partition layout;
- downstream validation;
- model or research consumers;
- current version and target version.

For every changed field record its name, type, unit, timestamp semantics, source, requirement level, and whether it is stored or computed when reading.

## 2. Protect dataset boundaries

- Ticks use `base_coin=<COIN>/event_date=<YYYY-MM-DD>/`.
- Lean ticks and `bar_5m` remain separate datasets.
- Full L1 permits model-side spread calculation.
- `volume` semantics must come from the collector/source contract.
- Missing required fields fail explicitly.

## 3. Decide compatibility

Choose one:

- backward-compatible extension;
- explicit new version;
- migration with a bounded compatibility reader;
- explicit rejection of old data.

Do not silently fill semantically missing data.

## 4. Split implementation

1. Schema Contract Agent fixes the contract and docs.
2. Runtime Storage Agent changes writer behavior.
3. Validation Agent checks a real published parquet sample.
4. Integration Validator Agent smoke-reads the sample when model input changes.

Do not change schema, persistence architecture, and strategy behavior in one step.

## 5. Acceptance

Require:

- exact column names and types;
- correct hive partitions;
- readable parquet after publication;
- detection of old, new, and incompatible inputs;
- no accidental v1/lean/bars mixing;
- successful consumer read or an explicit expected error.

Report the contract diff, compatibility decision, producer/consumer impact, handoff owners, sample-based checks, and migration or rollback path.

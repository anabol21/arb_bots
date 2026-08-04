---
name: spread-storage
description: Diagnoses and changes the spread collector persistence pipeline, including writer, spool, recovery, compaction, backup, shutdown, backlog, VPS, and mounted storage. Use for storage reliability work in app/storage, deploy, validation, or persistence hooks in app/screaner_b_o.py.
---

# Spread Storage Workflow

Use `.cursor/agents/runtime-storage-agent.md` for ownership and `.cursor/agents/validation-agent.md` for independent evidence.

## 1. Locate the stage

Name one stage:

- in-memory queue or batch;
- parquet conversion;
- writer/publisher;
- target mount publication;
- local spool;
- recovery;
- compaction;
- backup transfer;
- shutdown or restart.

State the exact failure mode and affected environment: local, VPS, or mounted storage.

## 2. State invariants

At minimum define:

- accounting: accepted = published + spooled + quarantined + pending for the observation window;
- the durable boundary;
- legal temporary and final file states;
- queue/backlog limits;
- shutdown deadline and restart behavior.

Do not treat enqueue or attempted write as durable success.

## 3. Inspect before editing

Read the relevant module and existing validation script. Identify:

- runtime command and log path;
- first materialization path and durable destination;
- file-state transitions;
- timeouts, retry limits, atomic rename, and exception paths;
- backlog count, size, and age signals.

Keep ingest, parsing, spread calculation, and strategy frozen.

## 4. Choose the smallest action

If the boundary is known, make a narrow patch to the existing hybrid path.

If it is genuinely undecided, compare at most:

1. direct mounted write;
2. local staging plus delayed transfer;
3. hybrid fallback.

Compare silent-loss risk, blocking, restart recovery, observability, mount dependency, and VPS burden. Prefer a falsifiable experiment when evidence is missing.

## 5. Validate

Run the smallest relevant existing check from `validation/`, then define VPS/mount evidence proportional to the claim.

Always test the relevant subset of:

- mount latency or disappearance;
- Ctrl+C and process crash;
- restart recovery;
- bounded backlog and drain;
- parquet readability;
- compaction and backup confirmation;
- duplicate and missing-data detection.

## 6. Handoff

1. Runtime Storage Agent implements.
2. Review Critic Agent reviews failure modes.
3. Validation Agent executes the repeatable scenario.

Report the pipeline stage, invariant, patch or experiment, environment, evidence, untested modes, and verdict.
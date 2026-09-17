# Grok handoff contract: Git ledger + MCP

## Purpose

Keep the three existing Grok bots aligned with Codex/Cursor development and
canary operations without using chat memory as the source of truth.

Git contains reviewed architecture, task/PR history and release manifests. MCP
contains current runtime health, canary state and redacted event projections.
Sentry remains compatible with existing routines. Group chat contains concise
notifications and decisions requiring the operator.

## Bot routing

| Bot role | Reads | Receives | May change |
|---|---|---|---|
| Collector/VPS/roadmap | collector health, capacity, release/status ledger, cross-contour incidents | daily digest, collector regression, blocked release | emergency pause of execution canary only |
| `would_sent` | signal counts, suppression reasons, policy version, shadow/live decision comparison | policy drift, missing/stale signal data, release notes affecting strategy contract | nothing |
| Private contour | socket health, intent/order/fill FSM, reconciliation, latency, exposure, canary gate | lifecycle anomaly, SLO breach, recovery action, canary stage | emergency pause of execution canary only |

No bot may resume a paused contour, deploy code, raise notional/caps, rotate
secrets, edit production systemd or place arbitrary orders through MCP.

## MCP tools

Read-only:

- `get_project_status()` — current Git ledger revision and release SHA;
- `get_collector_health()` — heartbeat age, restart count, resource summary;
- `get_would_sent_status(window)` — eligible/suppressed signals and policy rev;
- `get_execution_status()` — readiness, pause latch, socket generations, active
  intent and redacted position state;
- `get_canary_gate()` — current stage, evidence counts and failed conditions;
- `get_trade_timeline(intent_id)` — redacted lifecycle events;
- `get_latency_summary(window)` — per-venue/dual p50/p95/p99/p99.9;
- `get_recent_anomalies(window)` — unresolved recovery/SLO/WAL/projector faults.

Write-only:

- `pause_canary(reason, idempotency_key)` — latch no-new-opens. The response
  includes the audit event id and current executor state. Repeated keys are
  idempotent.

## Notification rules

Send a group-chat update only for:

- reviewed architecture decision or merged patch;
- new immutable canary release;
- canary start, gate pass/fail or stop;
- SLO breach, unknown exposure, failed reconciliation or WAL failure;
- operator decision needed.

Routine heartbeats and unchanged state stay in MCP/digests. Each notification
links the Git ledger entry or MCP event id and states environment, release SHA,
impact, evidence, rollback and whether operator action is required.

## Authentication and data policy

- Expose Streamable HTTP through stable TLS; Grok requires a publicly reachable
  custom MCP endpoint.
- Use separate scoped credentials per bot, rotation and server-side rate limits.
- PostgreSQL remains private/loopback; MCP is the only query boundary.
- Redact secrets, signatures, balances, raw account payloads, venue order ids,
  private URLs and machine credentials.
- Store MCP calls and pause actions in an append-only audit stream.
- A stale PostgreSQL projection is returned as stale with its watermark; it is
  never presented as current truth.

## Migration from existing Grok/Sentry automation

1. Inventory current bot descriptions, skills, routines, Sentry fingerprints
   and notification destinations without changing them.
2. Add GitHub access to this ledger and test read-only MCP tools with fixtures.
3. Mirror the current Sentry lifecycle semantics through the async exporter and
   compare old/new counts and fingerprints.
4. Enable bot routines in observation mode; require source links and stale-data
   handling.
5. Enable `pause_canary` only after a test against a non-trading fixture proves
   idempotency, auditability and that no resume/order tool exists.

Direct messages from Codex to Grok are optional notifications. They never carry
the only copy of a patch description, canary state or operational decision.

Official integration references: [Grok custom MCP connectors](https://docs.x.ai/grok/connectors),
[MCP server configuration](https://docs.x.ai/build/features/mcp-servers) and
[Grok Bot skills/routines](https://docs.x.ai/grok-bot/skills-routines-and-automations).

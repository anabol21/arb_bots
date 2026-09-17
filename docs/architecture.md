# Architecture index

The production collector remains the repository reliability priority and is
not part of the execution refactor.

Current architecture documents:

- [`execution-v2-architecture.md`](execution-v2-architecture.md) — target
  OKX/Bybit private execution contour, recovery model, latency contract and
  staged rollout.
- [`execution-v2-development-ledger.md`](execution-v2-development-ledger.md) —
  current audit facts, decisions, phases and evidence links.
- [`grok-handoff-contract.md`](grok-handoff-contract.md) — Git + MCP contract
  for the collector, `would_sent` and private-contour Grok bots.

The canonical runtime state is the exchange account state reconciled through
private streams and signed read-only REST. Git documents reviewed decisions
and releases; it is not the source of real-time position state.

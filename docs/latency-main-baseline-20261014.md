# Latency work baseline and measurement contract

Baseline for this branch: repository `main` at `ab02ba65b467f34a6de20a32e8422e48c2058261` (2026-10-14). The branch starts at that exact commit. The live Gear 2.3 path recorded in `NOW.md` is a directory copy without `.git`; its exact source SHA is not established. This document identifies the code baseline for research and does not claim deployment parity.

## Scope and invariants

- Work applies to an isolated, no-send latency/research path. It does not authorize deployment, restart, order sending, a live campaign, or changing limits.
- Keep the D path (`/data/live` → existing compaction → existing backup) unchanged.
- Keep the active trading B contour and its policy/decision semantics unchanged. No trading logic, order path, quote validity gate, candidate order, or sizing rule is changed by the latency trace.
- The 1 Hz TW emit cadence is unchanged in the first instrumentation patch. Wakeup/cadence changes require a separate experiment and explicit parity review.
- Main has `ThetaSnapshot.coverage_1m` and `n_1m`. They remain part of the existing JSON contract; the accuracy test on this branch includes both fields.

## Measurement contract

The opt-in `BBOT_LATENCY_TRACE` instrumentation records monotonic durations for the existing await boundaries: TW calculation, TW journal helper, theta calculation, theta journal helper, and manager invocation. It retains at most 128 immutable cycles in RAM. It does not retain ticks, quotes, metric rows, decisions, exceptions, or worker payloads, and it performs no disk writes.

A span includes event-loop scheduling and worker queueing around that await. A completed journal helper return does not prove durable storage. Cancellation of an `asyncio.to_thread` await does not prove its thread worker stopped. Manager invocation timing is not the full causal raw-WebSocket-event-to-policy latency. These measurements are stage diagnostics, not the ≤50 ms p50 acceptance result.

When tracing is disabled, runtime does not create the trace collector, call the trace clock, or allocate cycle/span records. The scheduler, payloads, and control-flow order remain as before.

## Accuracy and open source-baseline items

The uncapped duration-histogram oracle specifies the mathematical TW-p50 window behavior: event-time hold durations clipped to the window, `left < ts <= now` count semantics, carry-in, equal-timestamp stable order, coverage, and lower-median equality. The bounded production reservoir is approximate after overflow; exact parity is not claimed for capped streams.

A live source/config/universe manifest still requires a read-only source checkout on the host. The repository snapshot does not establish the dirty live Gear 2.3 tree's exact SHA or current effective runtime configuration. Do not mark P00 complete until this is obtained from an authorized read-only inspection. Storage mode comparisons and any 5-minute close load measurements remain later experiments; no storage or D behavior is changed here.

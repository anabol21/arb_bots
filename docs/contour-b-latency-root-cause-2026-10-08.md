# Contour B signal-to-send latency: evidence and patch

Scope: local code review and a local source patch against B2.3 `63ee7a4`. The
attached dataset identifies its measured tree as `f230345` plus in-place
patch `3c719bc`; selected hot-path files match this branch for the files
inspected. No live process, VPS, or exchange API was used for this patch.

## Finding: the 112.57 ms pre-process interval is not yet attributed

The TRUST open row `ce305e9a` measures `signal_ts_ms` to `preprocess.enter` at
112.57 ms. Existing chronometry has no event between those boundaries, so it
cannot establish which substep consumed that time. The synchronous size/depth
gate in `ThetaTradeManager._execute_decision_async` runs after `signal_mono_ns`
and before `_schedule_terminal_private_place`; that interval also belongs in
the next measurement.

Relevant path in the B2.3 source (line references are for the source tree named
in the dataset; they can shift slightly with this patch):

- `theta_trade_manager.py:2000-2037`: `on_theta_snapshots_async` computes the
decision, records the signal wall/monotonic stamps, then enters async decision
execution. In the TRUST sample, theta-done to signal was 123 ms, which is
before `signal_ts_ms` and therefore outside the observed 112.57 ms.
- `theta_trade_manager.py:2039-2132`: `_execute_decision_async` does synchronous
size/depth checks before scheduling terminal placement. These are candidates
for part of the interval.
- `theta_trade_manager.py:1878-1943`: `_schedule_terminal_private_place`
creates a task; when it runs, it creates a second task around
`asyncio.to_thread(_execute_injected_place)`. Each task needs event-loop
service. The worker also waits for a default-executor worker; queueing there
is possible when other `to_thread` work is active. GIL scheduling can delay
Python portions even after a worker starts.
- `theta_trade_manager.py:1721-1835`: the worker calls `_meta_fn`, then the
pre-send guard, then the injected place function. Meta is a cached lookup in
this contour; the guard checks fresh books, cached gates, private readiness,
instrument metadata, leverage, and hot-added coin readiness
(`runtime.py:866-923`). These checks are synchronous but the dataset has no
individual durations for them.
- `runtime.py:1746-1835`: `_synthetic_live_place` repeats cached metadata and
leverage checks, then enters `session.place_io_section()` before `place_live`.
- `ws_warm_session.py:220-243`: `place_io_section` takes the session `RLock`
only to increment/decrement `_place_inflight`; it does not hold it across the
place/ACK wait. A delay here would require contention while keepalive or
`add_coin` holds that lock, which is possible but unmeasured.
- `place_send.py:546-570`: `StepChrono` is constructed before
`chrono.enter("preprocess")`. Construction resolves the data-root path and
creates in-memory state. The new `chrono_created` marker follows construction,
so the gap to it includes constructor time; no claim is made that this stage
has zero filesystem metadata activity.

The dataset reports 59 coins, about 334 ticks/s, a 2.804 s TW emit cycle, and
66–78% main-thread CPU (about 93% process CPU in the captured VPS sample).
Those observations make event-loop delay and GIL contention plausible
contributors, but they do not prove that either caused the 112.57 ms. The
attached dataset cannot break that interval into causes; the added monotonic
markers are intended to do so on the next permitted observation.

## Evidence: synchronous callbacks serialized the legs

The TRUST row records `legs_sequential=true`: Bybit's callback lasted 26.33 ms;
OKX waited 26.43 ms in its sender queue, then started 31.69 ms after Bybit.
Within Bybit, callback-to-owner-loop-start was 5.47 ms, `ws.send` duration was
15.59 ms, and owner-return-to-callback-return was about 5.3 ms. In code,
`TrivialDualSender._sender` called synchronous `send_fn` directly on the one
shared sender loop. The production callback
`warm_trade_send_fn` → `WarmConnector.send_trade_timed` →
`LoopOwnedSocket.send_text_timed` submits `ws.send` with
`run_coroutine_threadsafe(...).result()` (`ws_warm_loop.py:199-248`). That
blocking wait prevents the shared sender loop from dequeuing the other venue.

The 15.59 ms marker measures elapsed time around the awaited `ws.send`; it does
not identify a single cause. WebSocket flow control / transport backpressure,
owner-loop scheduling, and GIL competition can contribute. The 5–10 ms
cross-thread intervals similarly show elapsed handoff time, not a uniquely
identified bottleneck.

The patch gives each venue its own FIFO queue and sender loop/thread. Both
callbacks can now progress without one callback blocking the other's consumer.
The owner loop still owns each socket and the existing per-send 30 s timeout
remains. `enqueue_dual` still waits up to 5 s for each sender completion;
`LiveBroker` still owns ACK classification, timeout, and one-leg error handling.
This removes sender-loop serialization; the socket owner loop, OS scheduling,
GIL, and actual venue/network timing can still create skew.

The recorded owner-send-returned to exchange `inTime` was about 28.5 ms for
both venues in this row. Exchange and VPS clock offset was not measured, so
this mixed-clock subtraction does not prove absolute network latency. The
reported baseline is useful only as a relative observed comparison with that
uncertainty retained.

## Added in-memory timestamps

The place flow adds monotonic nanosecond fields to the existing
`pre_send_timing` StepChrono row. Capturing them only mutates the shared
in-memory map; normal `StepChrono.flush()` serializes it after `ws_send` exits.
The markers are:

- `decision_gates_done`, `task_scheduled`, `task_started`: separate synchronous
gates and event-loop task wait from worker startup.
- `worker_thread_started`, `meta_done`, `guard_done`: split worker startup,
metadata lookup, and pre-send gate. `task_started` → `worker_thread_started`
combines the nested task's loop wait, default-executor wait, and GIL scheduling.
- `place_io_requested`, `place_io_acquired`, `chrono_created`: separate runtime
checks from session-lock acquisition and StepChrono constructor completion.

The order is monotonic-ns order, not wall-clock conversion. Missing stages stay
null. The map is per K=1 intent and is not logged before the send.

## Off-path delay and follow-up

The TRUST data also shows 55.8 ms between `journal_pending.exit` and
`wait_fill.enter`. `place_send.py:661-665` performs `chrono.flush()` in that
gap; `StepChrono.flush()` calls `os.fsync`. This occurs after both send
callbacks have returned, so it cannot explain signal-to-send, but it delays
fill processing. Preserve current durability semantics in this patch. A
separate experiment can measure buffered append plus deferred fsync against
the current synchronous fsync, with crash/restart recovery validation before
changing semantics.

TW snapshot calculation already runs via `asyncio.to_thread` at
`runtime.py:2650-2652`; TW journal append is also off-loop at `2622-2628`.
Theta compute and journal use the same default-executor pattern at
`2721` and `2820-2825`. Python-heavy calculations in threads can still
contend for the GIL, and executor queueing can delay the private-place worker.
There is a concrete lock/CPU candidate in `tw_p50_watcher.py`: tick-side
`note_spreads` holds `_lock` while `_append_sample` calls `_prune_ring`, which
sorts the full ring and scans it (`235-300`); `compute_snapshots` takes the same
lock while pruning every ring (`309-327`). With 59 coins, these operations
can contend with tick handling and execute Python under the GIL. This is a
code-based risk, not a measured cause of the 112.57 ms. Measure per-lock wait
and hold time, plus prune duration, before considering an algorithm change;
incremental prune/sort needs policy-preserving validation.

The 2.804 s emit cycle is therefore a load signal, not evidence that emit work
occupied the main event loop for 2.804 s. The theta journal flush is awaited
before `_run_theta_trade` (`runtime.py:2753-2765`), which can explain time from
theta computation to signal but is outside the signal-to-preprocess interval
because `signal_ts_ms` is stamped later in `theta_trade_manager.py:2019-2020`.
A follow-up should time ring-lock wait/hold, prune CPU, compute, row
construction, journal submit, and executor wait separately. If CPU-bound
Python work is confirmed, benchmark vectorized/native compute or a dedicated
process pool; keep journal I/O off the public event loop. No emit or fsync
behavior is changed here.

## Local validation

The targeted sender tests cover the two independent FIFO consumers and assert
that OKX's callback begins while a blocked Bybit callback is still waiting.
Stamp tests exercise manager → runtime place boundary and StepChrono
serialization. No exchange socket is opened by these tests.

Local Python 3.14 validation (not a Python 3.10/VPS claim):

```sh
python3 -m unittest tests.test_trivial_dual_leg.TrivialSenderTests tests.test_bbot_theta_trade_k1.TerminalExecutionModeTests tests.test_synthetic_send_timing.SyntheticSendTimingTests.test_pre_send_stamps_are_persisted_as_monotonic_only
```

Result: 12 tests, OK. This includes partial thread-start cleanup and refusing
sends after close. Reintroducing callback serialization with a shared lock
makes the parallel-send regression fail; the mutation was restored afterward.

The broader command `python3 -m unittest tests.test_trivial_dual_leg
tests.test_synthetic_send_timing tests.test_bbot_theta_trade_k1` ran 66 tests:
6 failure records and 1 skip. The same 6 failure records were reproduced on
clean `63ee7a4` (27 sender/timing tests). They are pre-existing `fill_timeout`
fixture failures in `SyntheticSendTimingTests`:

- `test_absent_sender_and_sender_without_timing_keep_existing_behavior`
- `test_both_venue_timings_persist_after_send_for_open_and_close` (three
subtests: `open_long`, `open_short`, `close`)
- `test_real_dual_queue_markers_survive_place_and_serialize`
- `test_returned_sender_error_does_not_change_existing_send_outcome`

The full suite is therefore not green. Importing `test_synthetic_startup_prewarm`
standalone also requires the unavailable local `websockets` dependency; no
package installation or live connectivity was attempted.

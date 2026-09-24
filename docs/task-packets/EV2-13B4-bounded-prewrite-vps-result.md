# EV2-13B4 bounded prewrite: target-VPS no-order result

Measured 2026-09-24 12:07:11 UTC on VPS `a845945761.local`, checkout
`8cc7759`, Python 3.10.12. Isolated `nice -n 10` benchmark; 100 independent
fresh-WAL exact-engine samples, 300 separate growing-WAL no-order audits, then
one exact-engine sample against that grown WAL. No network-capable trade socket,
private readiness simulated, `orders_sent=0`. These values are **not** exchange
WS write, ACK, fill, or a live latency gate.

| Boundary | n | p50 | p99 |
| --- | ---: | ---: | ---: |
| Fresh WAL accepted-event fsync/tail proof | 100 | 0.663 ms | 1.140 ms |
| Signal timestamp → first in-memory `asend` entry | 100 | 2.867 ms | 4.034 ms |
| Signal timestamp → slowest in-memory `asend` return | 100 | 2.884 ms | 4.051 ms |
| Separate growing-WAL audit, full replay each attempt | 300 | 629.905 ms | 2072.373 ms |

At 300 audit attempts / 511,746 prior WAL bytes, the *single* exact-engine
sample had a 1.092 ms fsync/tail proof but 25.851 ms signal→first memory
`asend`. This single observation cannot establish a percentile or isolate the
cause. It does show that removing replay from the proof alone is insufficient
to certify the complete signal→send path with accumulated history. The FSM
still carries and validates historical event-ID/hash collections, so that is
a candidate for focused profiling before real-order enablement.

The prior fresh-WAL VPS probe at `a64d897` measured 2.340 ms p50 / 3.524 ms
p99 for the replay-based fence and 4.542 ms p50 / 6.785 ms p99 to first memory
`asend` (100 samples). The new measurements are lower under comparable
no-order conditions, but neither probe measures a real WS write or fill.

`spread-collector-next.service` and `spread-bbot-theta-k1-canary.service`
remained active with `NRestarts=0`. The isolated benchmark checkout was clean.

Decision: retain EV2 live-send guard. Profile and bound the full growing-WAL
signal→send path, then integrate fill-driven EV2 transport and measure actual
local WS write → exchange fill → private ACK per venue. Compare per-leg
signal→send with send→fill numerically; ~1 ms is an order-of-magnitude aim,
not a strict pass/fail threshold.

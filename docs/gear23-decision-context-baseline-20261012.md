# Gear 2.3 decision-context baseline — 2026-10-12

The reviewed Gear 2.3 source records the decision inputs beside each synthetic
trade. `bbot.trade_decision_context.v1` captures the selected action and side,
policy outcome, theta and potential-profit values, 1-minute observer state,
selected and alternate spreads, and the signal-time L1 books. The existing
`pending` and terminal `open`/`closed` trade rows carry the same context, so a
trade can be reconstructed from its journal without matching a separate metric
stream.

Theta snapshots now retain the 1-minute TW coverage and sample count and pass
them through the theta journal and decision context. These fields describe the
1-minute observation already used by the contour; they do not change the
decision rule, sizing, fill handling, or readiness gates.

This is a selective baseline from the reviewed deployed files. Current `main`
remains authoritative for the size gate, filled-quantity close sizing,
hot-add readiness, terminal lifecycle handling, Sentry ops events, and closed-
day metric compression. No runtime launcher, environment file, credentials,
or Sentry DSN is part of this source change.

Validation: 127 scoped local tests passed without skips (115 manager/private/
theta/Sentry tests plus 12 startup/terminal-worker tests), using Python 3.12
with real NumPy and WebSockets 16.1. Syntax and `git diff --check` passed.
Stale test hosts were completed with the existing runtime interface; production
runtime behavior was not changed by those fixture corrections.

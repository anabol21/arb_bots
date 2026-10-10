# Gear 2.3 source baseline — 2026-10-06

The isolated Gear 2.3 worktree starts from commit `6fb106a364c0555a5de276216b26b5ef41d21c7a` (`codex/canary29-terminal-policy`). The shared workspace was dirty and was not used or changed.

The running standalone Gear 2.2 canary used code under `/root/b-private-b-exp/response-manager-code/response-handler-20261006-sentry` (PID 2224799 observed 2026-10-06 08:22:57 UTC). That directory is not a Git checkout. To preserve its deployed source baseline, `app/bot/runtime.py` was copied read-only from that directory into this isolated worktree before Gear 2.3 changes.

Imported runtime SHA-256:

```text
ff252821750d8aa896e904b7cc909e0740de50555655f1cc94b4d3ff1641be7e
```

The copied runtime differed from the isolated commit by the already-deployed terminal-private Sentry outcome and chronometry reporting code. This commit records source provenance only; it does not claim to have validated those Sentry changes. The matching baseline hashes for `theta_trade_manager.py`, `private/ws_warm_session.py`, `private/ws_private.py`, and `synthetic_policy.py` were verified against the running canary and are unchanged here.

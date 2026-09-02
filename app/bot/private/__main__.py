"""python -m app.bot.private — read-only harness, --selftest, or explicit WS flags.

R3 order transport is unreachable. Default path has no WS. LIVE_ORDERS=1 alone
must not open a WS socket. ``--ws-readonly`` requires VENUE=live and LIVE_ORDERS=0.
``--ws-warm-session`` starts process-lifetime private WS (no send) when live
send gates pass; dual-leg CLIs reuse that warm session.
"""

from __future__ import annotations

from app.bot.private.harness_readonly import main
from app.bot.private.order_sender import assert_default_entrypoint_cannot_transport
from app.bot.private.ws_private import assert_default_cli_has_no_ws


if __name__ == "__main__":
    assert_default_entrypoint_cannot_transport()
    assert_default_cli_has_no_ws()
    raise SystemExit(main())

"""W7 bounded parallel WS dual-leg market samples.

Same immutable TRUMP profiles as W6 (~$6–8, ≪ 100 USD/venue).
Open legs: Bybit BUY and OKX SELL market dispatched after a barrier so both
trade sockets send together. Flatten remains sequential reduce-only after both
opens. One-leg leftover is flattened on both venues; n stops.

Requires VENUE=live, LIVE_ORDERS=1, BBOT_PRIVATE_W7=1, ``--w7-n=1..20``,
and ``--w7-approve-one-shot``. Default / W3 / W4 / W5 / W6 CLI never binds this.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping, Optional, Sequence

from app.bot.private.order_preflight import PreflightError
from app.bot.private.order_sender import assert_default_entrypoint_cannot_transport
from app.bot.private.ws_gates import WsProfileGateError, assert_ws_w7_send_gates
from app.bot.private.ws_socket import (
    assert_no_default_ws_socket,
    unbind_socket_factory,
)
from app.bot.private.ws_w4_baseline import BaselineError
from app.bot.private.ws_w6_dual_leg import (
    W6ProfileError,
    W6Report,
    W6RuntimeBindings,
    assert_w6_n,
    open_w6_production_bindings,
    run_w6_dual_leg,
)


def parse_w7_cli_args(argv: Sequence[str]) -> tuple[Optional[int], bool]:
    n: Optional[int] = None
    approve = False
    for arg in argv:
        if arg.startswith("--w7-n="):
            raw = arg.split("=", 1)[1].strip()
            try:
                n = int(raw)
            except ValueError:
                n = -1
        elif arg == "--w7-approve-one-shot":
            approve = True
    return n, approve


def run_w7_parallel_dual_leg(**kwargs: Any) -> W6Report:
    kwargs["parallel_open"] = True
    kwargs["send_gate"] = assert_ws_w7_send_gates
    return run_w6_dual_leg(**kwargs)


def main_ws_w7_parallel_dual_leg(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    bindings: Optional[W6RuntimeBindings] = None,
) -> int:
    """CLI entry for ``--ws-w7-parallel-dual-leg --w7-n=N --w7-approve-one-shot``."""
    argv = list(argv or [])
    e = dict(env if env is not None else os.environ)
    n, approve_one_shot = parse_w7_cli_args(argv)

    def _print(report: W6Report) -> None:
        print(json.dumps(report.as_public_dict(), ensure_ascii=False, indent=2, sort_keys=True))

    try:
        assert_ws_w7_send_gates(e)
        if n is None:
            raise W6ProfileError("W7 requires --w7-n=1..20")
        assert_w6_n(n)
    except (WsProfileGateError, W6ProfileError):
        _print(
            W6Report(
                status="rejected_before_socket",
                n_requested=n if isinstance(n, int) and n > 0 else 0,
                error_code="invalid_request",
                open_mode="parallel",
            )
        )
        return 1

    if not approve_one_shot:
        _print(
            W6Report(
                status="approval_required",
                n_requested=n,
                error_code="invalid_request",
                open_mode="parallel",
            )
        )
        return 1

    assert_default_entrypoint_cannot_transport()

    owned = bindings is None
    active: Optional[W6RuntimeBindings] = bindings
    try:
        if active is None:
            try:
                assert_no_default_ws_socket()
            except RuntimeError:
                _print(
                    W6Report(
                        status="rejected_before_socket",
                        n_requested=n,
                        error_code="transport_error",
                        open_mode="parallel",
                    )
                )
                return 2
            try:
                active = open_w6_production_bindings(env=e)
            except (
                BaselineError,
                W6ProfileError,
                PreflightError,
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
            ):
                unbind_socket_factory()
                _print(
                    W6Report(
                        status="bind_failed",
                        n_requested=n,
                        error_code="transport_error",
                        open_mode="parallel",
                    )
                )
                return 2

        report = run_w7_parallel_dual_leg(
            n=n,
            env=e,
            metadata_provider=active.metadata_provider,
            position_mode_provider=active.position_mode_provider,
            baseline=active.baseline,
            bybit_private_socket=active.bybit_private_socket,
            bybit_trade_socket=active.bybit_trade_socket,
            okx_private_socket=active.okx_private_socket,
            okx_trade_socket=active.okx_trade_socket,
            bybit_credentials=active.bybit_credentials,
            okx_credentials=active.okx_credentials,
            load_secrets=False,
            issue_approval=True,
            rest_order_recon=active.rest_order_recon,
        )
        _print(report)
        if report.status == "ok":
            return 0
        if report.status in {
            "secrets_unavailable",
            "approval_required",
            "rejected_before_socket",
        }:
            return 1
        return 2
    finally:
        if owned:
            unbind_socket_factory()
            assert_default_entrypoint_cannot_transport()

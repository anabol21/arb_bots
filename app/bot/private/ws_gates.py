"""Fail-closed profile/flag gates for private WS (before any socket open)."""

from __future__ import annotations

from typing import Mapping, Optional

from app.bot.private.secrets import resolve_private_profile
from app.bot.private.venue import (
    assert_live_readonly,
    live_orders_enabled,
    resolve_venue,
)


class WsProfileGateError(RuntimeError):
    """Wrong VENUE/LIVE_ORDERS/profile for private WS."""


def assert_ws_runtime_profile_gate(
    env: Optional[Mapping[str, str]],
    *,
    environment: str,
) -> None:
    """Refuse testnet/wrong-profile/LIVE_ORDERS=1 before any socket open.

    W3 private WS preflight is live + LIVE_ORDERS=0 only. Must run before any
    socket factory open or bind. Does not open network.
    """
    if env is None:
        raise WsProfileGateError("private WS requires an explicit env mapping")
    try:
        venue = resolve_venue(env)
    except ValueError as exc:
        raise WsProfileGateError(str(exc)) from exc
    if venue != "live":
        raise WsProfileGateError(
            f"private WS refuses VENUE={venue!r}; only live profile may connect"
        )
    if live_orders_enabled(env):
        raise WsProfileGateError(
            "private WS preflight refuses LIVE_ORDERS=1; require LIVE_ORDERS=0"
        )
    profile = resolve_private_profile(env)
    if profile.name != "live":
        raise WsProfileGateError(
            f"private WS refuses credential profile {profile.name!r}"
        )
    if environment != "live":
        raise WsProfileGateError(
            f"private WS refuses environment={environment!r}; require live"
        )


def assert_ws_readonly_cli_gates(env: Optional[Mapping[str, str]] = None) -> str:
    """CLI ``--ws-readonly``: VENUE=live AND LIVE_ORDERS=0 AND live profile.

    Returns venue string ``live`` on success. Never opens a socket.
    """
    try:
        venue = assert_live_readonly(env)
    except RuntimeError as exc:
        raise WsProfileGateError(str(exc)) from exc
    if live_orders_enabled(env):
        raise WsProfileGateError(
            "WS read-only refuses LIVE_ORDERS=1; use LIVE_ORDERS=0"
        )
    profile = resolve_private_profile(env)
    if profile.name != "live":
        raise WsProfileGateError(
            f"WS read-only refuses credential profile {profile.name!r}"
        )
    if not profile.readonly:
        raise WsProfileGateError("WS read-only requires readonly live profile")
    if profile.orders_surface:
        raise WsProfileGateError("WS read-only refuses orders_surface profile")
    assert_ws_runtime_profile_gate(env, environment="live")
    return venue


def _w4_opt_in(env: Mapping[str, str]) -> bool:
    raw = (env.get("BBOT_PRIVATE_W4") or env.get("W4_POST_ONLY") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def assert_ws_w4_send_gates(env: Optional[Mapping[str, str]] = None) -> str:
    """W4 live-send WS gate: VENUE=live, LIVE_ORDERS=1, live profile, W4 opt-in.

    Separate from W3 read-only gate. Never opens a socket by itself.
    """
    if env is None:
        raise WsProfileGateError("W4 WS requires an explicit env mapping")
    try:
        venue = resolve_venue(env)
    except ValueError as exc:
        raise WsProfileGateError(str(exc)) from exc
    if venue != "live":
        raise WsProfileGateError(f"W4 refuses VENUE={venue!r}; require live")
    if not live_orders_enabled(env):
        raise WsProfileGateError("W4 requires LIVE_ORDERS=1")
    if not _w4_opt_in(env):
        raise WsProfileGateError(
            "W4 requires explicit opt-in BBOT_PRIVATE_W4=1 (plus CLI --ws-w4-post-only)"
        )
    profile = resolve_private_profile(env)
    if profile.name != "live":
        raise WsProfileGateError(
            f"W4 refuses credential profile {profile.name!r}; require live"
        )
    if not profile.live_orders_flag:
        raise WsProfileGateError("W4 requires live profile LIVE_ORDERS flag")
    return venue


def _w5_opt_in(env: Mapping[str, str]) -> bool:
    raw = (env.get("BBOT_PRIVATE_W5") or env.get("W5_MARKET") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def assert_ws_w5_send_gates(env: Optional[Mapping[str, str]] = None) -> str:
    """W5 live-send WS gate: VENUE=live, LIVE_ORDERS=1, live profile, W5 opt-in.

    Separate from W3/W4. Never opens a socket by itself. Does not imply W4.
    """
    if env is None:
        raise WsProfileGateError("W5 WS requires an explicit env mapping")
    try:
        venue = resolve_venue(env)
    except ValueError as exc:
        raise WsProfileGateError(str(exc)) from exc
    if venue != "live":
        raise WsProfileGateError(f"W5 refuses VENUE={venue!r}; require live")
    if not live_orders_enabled(env):
        raise WsProfileGateError("W5 requires LIVE_ORDERS=1")
    if not _w5_opt_in(env):
        raise WsProfileGateError(
            "W5 requires explicit opt-in BBOT_PRIVATE_W5=1 (plus CLI --ws-w5-market)"
        )
    profile = resolve_private_profile(env)
    if profile.name != "live":
        raise WsProfileGateError(
            f"W5 refuses credential profile {profile.name!r}; require live"
        )
    if not profile.live_orders_flag:
        raise WsProfileGateError("W5 requires live profile LIVE_ORDERS flag")
    return venue


def _w6_opt_in(env: Mapping[str, str]) -> bool:
    raw = (env.get("BBOT_PRIVATE_W6") or env.get("W6_DUAL_LEG") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def assert_ws_w6_send_gates(env: Optional[Mapping[str, str]] = None) -> str:
    """W6 live-send WS gate: VENUE=live, LIVE_ORDERS=1, live profile, W6 opt-in.

    Separate from W3/W4/W5. Never opens a socket by itself. Does not imply W5.
    """
    if env is None:
        raise WsProfileGateError("W6 WS requires an explicit env mapping")
    try:
        venue = resolve_venue(env)
    except ValueError as exc:
        raise WsProfileGateError(str(exc)) from exc
    if venue != "live":
        raise WsProfileGateError(f"W6 refuses VENUE={venue!r}; require live")
    if not live_orders_enabled(env):
        raise WsProfileGateError("W6 requires LIVE_ORDERS=1")
    if not _w6_opt_in(env):
        raise WsProfileGateError(
            "W6 requires explicit opt-in BBOT_PRIVATE_W6=1 (plus CLI --ws-w6-dual-leg)"
        )
    profile = resolve_private_profile(env)
    if profile.name != "live":
        raise WsProfileGateError(
            f"W6 refuses credential profile {profile.name!r}; require live"
        )
    if not profile.live_orders_flag:
        raise WsProfileGateError("W6 requires live profile LIVE_ORDERS flag")
    return venue


def _w7_opt_in(env: Mapping[str, str]) -> bool:
    raw = (env.get("BBOT_PRIVATE_W7") or env.get("W7_PARALLEL_DUAL_LEG") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def assert_ws_w7_send_gates(env: Optional[Mapping[str, str]] = None) -> str:
    """W7 live-send WS gate: VENUE=live, LIVE_ORDERS=1, live profile, W7 opt-in.

    Separate from W3/W4/W5/W6. Never opens a socket by itself. Does not imply W6.
    """
    if env is None:
        raise WsProfileGateError("W7 WS requires an explicit env mapping")
    try:
        venue = resolve_venue(env)
    except ValueError as exc:
        raise WsProfileGateError(str(exc)) from exc
    if venue != "live":
        raise WsProfileGateError(f"W7 refuses VENUE={venue!r}; require live")
    if not live_orders_enabled(env):
        raise WsProfileGateError("W7 requires LIVE_ORDERS=1")
    if not _w7_opt_in(env):
        raise WsProfileGateError(
            "W7 requires explicit opt-in BBOT_PRIVATE_W7=1 (plus CLI --ws-w7-parallel-dual-leg)"
        )
    profile = resolve_private_profile(env)
    if profile.name != "live":
        raise WsProfileGateError(
            f"W7 refuses credential profile {profile.name!r}; require live"
        )
    if not profile.live_orders_flag:
        raise WsProfileGateError("W7 requires live profile LIVE_ORDERS flag")
    return venue


def assert_ws_warm_private_gates(env: Optional[Mapping[str, str]] = None) -> str:
    """Warm private WS at private-live startup: VENUE=live + LIVE_ORDERS=1.

    Connects only when ``start_warm_private_session`` (or equivalent) is called
    explicitly — ``LIVE_ORDERS=1`` alone never opens a socket. No separate
    warm opt-in flag; private sockets are on by default once the warm
    supervisor is started for a private-live unit.
    """
    if env is None:
        raise WsProfileGateError("warm private WS requires an explicit env mapping")
    try:
        venue = resolve_venue(env)
    except ValueError as exc:
        raise WsProfileGateError(str(exc)) from exc
    if venue != "live":
        raise WsProfileGateError(f"warm private refuses VENUE={venue!r}; require live")
    if not live_orders_enabled(env):
        raise WsProfileGateError("warm private requires LIVE_ORDERS=1")
    profile = resolve_private_profile(env)
    if profile.name != "live":
        raise WsProfileGateError(
            f"warm private refuses credential profile {profile.name!r}; require live"
        )
    if not profile.live_orders_flag:
        raise WsProfileGateError("warm private requires live profile LIVE_ORDERS flag")
    return venue


def _warm_lat_opt_in(env: Mapping[str, str]) -> bool:
    raw = (
        env.get("BBOT_PRIVATE_WARM_LAT") or env.get("WARM_LAT") or ""
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def assert_ws_warm_lat_gates(env: Optional[Mapping[str, str]] = None) -> str:
    """Warm-Lat live-send gate: VENUE=live, LIVE_ORDERS=1, live profile, opt-in.

    Separate from W3–W7. Never opens a socket by itself. Dry
    ``--ws-warm-latency`` with ``--warm-lat-send=false`` does not use this gate.
    """
    if env is None:
        raise WsProfileGateError("Warm-Lat requires an explicit env mapping")
    try:
        venue = resolve_venue(env)
    except ValueError as exc:
        raise WsProfileGateError(str(exc)) from exc
    if venue != "live":
        raise WsProfileGateError(f"Warm-Lat refuses VENUE={venue!r}; require live")
    if not live_orders_enabled(env):
        raise WsProfileGateError("Warm-Lat requires LIVE_ORDERS=1")
    if not _warm_lat_opt_in(env):
        raise WsProfileGateError(
            "Warm-Lat requires explicit opt-in BBOT_PRIVATE_WARM_LAT=1 "
            "(plus CLI --ws-warm-latency --warm-lat-approve-one-shot)"
        )
    profile = resolve_private_profile(env)
    if profile.name != "live":
        raise WsProfileGateError(
            f"Warm-Lat refuses credential profile {profile.name!r}; require live"
        )
    if not profile.live_orders_flag:
        raise WsProfileGateError("Warm-Lat requires live profile LIVE_ORDERS flag")
    return venue


def is_live_send_ws_profile_gate(gate: object) -> bool:
    """True for W4/W5/W6/W7/warm/Warm-Lat send gates (env only; no environment=)."""
    return (
        gate is assert_ws_w4_send_gates
        or gate is assert_ws_w5_send_gates
        or gate is assert_ws_w6_send_gates
        or gate is assert_ws_w7_send_gates
        or gate is assert_ws_warm_private_gates
        or gate is assert_ws_warm_lat_gates
    )

"""Sentry SDK setup for B-bot. Init only when SENTRY_DSN is set."""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional

_sentry_enabled = False
_sentry_sdk: Any = None


def _try_import_sentry():
    global _sentry_sdk
    if _sentry_sdk is not None:
        return _sentry_sdk
    try:
        import sentry_sdk
        _sentry_sdk = sentry_sdk
        return _sentry_sdk
    except ImportError:
        return None


def init_sentry(*, profile: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """Initialize Sentry SDK if SENTRY_DSN is set. Returns True if enabled."""
    global _sentry_enabled
    
    e = env if env is not None else os.environ
    dsn = str(e.get("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    
    sentry = _try_import_sentry()
    if sentry is None:
        logging.warning("SENTRY_DSN set but sentry_sdk not installed; skipping")
        return False
    
    environment = str(e.get("SENTRY_ENVIRONMENT") or "gear22-would-send-canary").strip()
    release = str(e.get("SENTRY_RELEASE") or "").strip() or None
    
    sentry.init(
        dsn=dsn,
        environment=environment,
        release=release,
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
    )
    
    sentry.set_tag("contour", "gear22_theta_k1")
    sentry.set_tag("profile", profile)
    
    _sentry_enabled = True
    return True


def sentry_enabled() -> bool:
    """Check if Sentry is enabled."""
    return _sentry_enabled


def capture_trade_event(
    *,
    event: str,
    trade_id: str,
    coin: str,
    side: str,
    extras: Optional[Mapping[str, Any]] = None,
    level: str = "warning",
) -> None:
    """Emit a Sentry event for a trade lifecycle step (open/fill/close/reject).
    
    Args:
        event: open | fill | close | reject
        trade_id: UUID
        coin: base coin
        side: long | short
        extras: Additional context (spread, theta, pnl, etc.)
        level: warning | error | info
    """
    if not _sentry_enabled:
        return
    
    sentry = _try_import_sentry()
    if sentry is None:
        return
    
    message = f"theta_k1 trade {event} coin={coin} side={side} trade_id={trade_id}"
    
    with sentry.push_scope() as scope:
        scope.set_tag("event", event)
        scope.set_tag("coin", coin)
        scope.set_tag("side", side)
        scope.set_tag("trade_id", trade_id)
        scope.set_tag("contour", "gear22_theta_k1")
        
        scope.fingerprint = ["theta_k1", trade_id, event]
        
        if extras:
            for key, value in extras.items():
                if value is not None:
                    scope.set_extra(key, value)
        
        sentry.capture_message(message, level=level)


def capture_exception(exc: Exception, *, extras: Optional[Mapping[str, Any]] = None) -> None:
    """Capture an exception in Sentry."""
    if not _sentry_enabled:
        return
    
    sentry = _try_import_sentry()
    if sentry is None:
        return
    
    with sentry.push_scope() as scope:
        if extras:
            for key, value in extras.items():
                if value is not None:
                    scope.set_extra(key, value)
        sentry.capture_exception(exc)

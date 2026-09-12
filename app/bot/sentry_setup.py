"""Sentry SDK setup for B-bot. Init only when SENTRY_DSN is set."""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional

_sentry_enabled = False
_sentry_sdk: Any = None
_init_attempted = False

_log = logging.getLogger("bbot.sentry")


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
    global _sentry_enabled, _init_attempted
    
    _init_attempted = True
    e = env if env is not None else os.environ
    dsn = str(e.get("SENTRY_DSN") or "").strip()
    if not dsn:
        _log.info("sentry_init | status=skipped | reason=no_dsn")
        return False
    
    sentry = _try_import_sentry()
    if sentry is None:
        _log.warning("sentry_init | status=skipped | reason=sdk_not_installed")
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
    _log.info(f"sentry_init | status=enabled | environment={environment} | profile={profile}")
    return True


def sentry_enabled() -> bool:
    """Check if Sentry is enabled."""
    return _sentry_enabled


def _lazy_init_if_needed() -> bool:
    """Lazy re-init in capture path if DSN is set but init not attempted."""
    global _init_attempted
    if _init_attempted:
        return _sentry_enabled
    if not os.environ.get("SENTRY_DSN"):
        _init_attempted = True
        return False
    # DSN is set but init was never called; try now.
    _log.info("sentry_lazy_init | attempting init from capture path")
    return init_sentry(profile=os.environ.get("BBOT_PROFILE", "unknown"))


def capture_trade_event(
    *,
    event: str,
    trade_id: str,
    coin: str,
    side: str,
    extras: Optional[Mapping[str, Any]] = None,
    level: str = "error",
) -> None:
    """Emit a Sentry event for a trade lifecycle step (open/close).
    
    Args:
        event: open | close (rejects are journal-only)
        trade_id: UUID
        coin: base coin
        side: long | short
        extras: Additional context (spread, theta, pnl, etc.)
        level: error (default for Issues + issueCreated) | warning | info
    """
    if not _lazy_init_if_needed():
        _log.debug(f"sentry_trade_emit | status=skipped | trade_id={trade_id} | event={event}")
        return
    
    sentry = _try_import_sentry()
    if sentry is None:
        _log.warning(f"sentry_trade_emit | status=skipped | reason=no_sdk | trade_id={trade_id}")
        return
    
    message = f"theta_k1 trade {event} coin={coin} side={side} trade_id={trade_id}"
    
    try:
        # Prefer new_scope (Sentry SDK 2.x), fallback to push_scope.
        scope_fn = getattr(sentry, "new_scope", None) or sentry.push_scope
        with scope_fn() as scope:
            scope.set_tag("event", event)
            scope.set_tag("coin", coin)
            scope.set_tag("side", side)
            scope.set_tag("trade_id", trade_id)
            scope.set_tag("contour", "gear22_theta_k1")
            scope.set_tag("kind", "trade")
            
            scope.fingerprint = ["theta_k1", trade_id, event]
            
            if extras:
                for key, value in extras.items():
                    if value is not None:
                        scope.set_extra(key, value)
            
            sentry.capture_message(message, level=level)
        
        sentry.flush(timeout=5)
        _log.info(f"sentry_trade_emit | status=ok | trade_id={trade_id} | event={event} | coin={coin}")
    except Exception as e:
        _log.error(f"sentry_trade_emit | status=fail | trade_id={trade_id} | error={e}")


def capture_exception(exc: Exception, *, extras: Optional[Mapping[str, Any]] = None) -> None:
    """Capture an exception in Sentry."""
    if not _lazy_init_if_needed():
        return
    
    sentry = _try_import_sentry()
    if sentry is None:
        return
    
    try:
        scope_fn = getattr(sentry, "new_scope", None) or sentry.push_scope
        with scope_fn() as scope:
            if extras:
                for key, value in extras.items():
                    if value is not None:
                        scope.set_extra(key, value)
            sentry.capture_exception(exc)
        
        sentry.flush(timeout=5)
        _log.info(f"sentry_exception | status=ok | exc={type(exc).__name__}")
    except Exception as e:
        _log.error(f"sentry_exception | status=fail | error={e}")

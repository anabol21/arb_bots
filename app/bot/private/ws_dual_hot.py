"""Dual-leg hot path: prefetch/reuse before parallel place on warm WS.

VPS ``live_broker`` (not always present in git) and W6/W7 should call these
helpers so operator_approval index, lease check, profile gate, and live
metadata/position-mode are not redone from scratch on every leg when a warm
private session is already ready.

Does not change recovery / leftover / flatten_only sample-cap policy — callers
keep those gates. Parallel place still uses the PR #12 ``place_io_section`` +
barrier path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from app.bot.private.order_approval import ApprovalToken, ApprovalVault
from app.bot.private.order_lease import LeaseSupervisor
from app.bot.private.order_plan import OrderPlan
from app.bot.private.order_preflight import (
    PositionModeProvider,
    TtlCachingMetadataProvider,
    TtlCachingPositionModeProvider,
    assert_preflight_ready,
)
from app.bot.private.order_metadata import MetadataProvider
from app.bot.private.order_sender import assert_send_gates
from app.bot.private.secrets import resolve_private_profile


@dataclass(frozen=True)
class DualLegHotContext:
    """Prefetched dual-leg send context for one open or close pair."""

    profile_name: str
    metadata_provider: MetadataProvider
    position_mode_provider: PositionModeProvider
    vault: ApprovalVault
    lease_supervisor: LeaseSupervisor
    meta_fetch_count: int
    position_fetch_count: int


def wrap_hot_providers(
    *,
    metadata_provider: MetadataProvider,
    position_mode_provider: PositionModeProvider,
    metadata_ttl_ns: int = 2_000_000_000,
    position_ttl_ns: int = 60_000_000_000,
) -> tuple[MetadataProvider, PositionModeProvider]:
    """Wrap live HTTP providers with TTL caches when not already wrapped."""
    meta: MetadataProvider
    if isinstance(metadata_provider, TtlCachingMetadataProvider):
        meta = metadata_provider
    else:
        meta = TtlCachingMetadataProvider(
            inner=metadata_provider, ttl_ns=metadata_ttl_ns
        )
    pos: PositionModeProvider
    if isinstance(position_mode_provider, TtlCachingPositionModeProvider):
        pos = position_mode_provider
    else:
        pos = TtlCachingPositionModeProvider(
            inner=position_mode_provider, ttl_ns=position_ttl_ns
        )
    return meta, pos


def prefetch_dual_leg_hot_context(
    *,
    env: Mapping[str, str],
    vault: ApprovalVault,
    lease_supervisor: LeaseSupervisor,
    metadata_provider: MetadataProvider,
    position_mode_provider: PositionModeProvider,
    plans: Sequence[OrderPlan],
    now_mono_ns: Optional[int] = None,
) -> DualLegHotContext:
    """Move profile / lease / approval-index / preflight off the place critical path.

    Call once per dual-leg signal **before** issuing parallel place workers.
    Safe to call repeatedly; TTL caches and vault index stay warm.
    """
    profile = resolve_private_profile(env)
    if profile.name != "live":
        raise RuntimeError(f"dual-leg hot path requires live profile, got {profile.name!r}")
    lease_supervisor.assert_can_send(now_mono_ns=now_mono_ns)
    vault.prefetch_index()

    meta, pos = wrap_hot_providers(
        metadata_provider=metadata_provider,
        position_mode_provider=position_mode_provider,
    )
    for plan in plans:
        assert_send_gates(env, plan)
        assert_preflight_ready(
            metadata_provider=meta,
            position_mode_provider=pos,
            venue=plan.venue,
            symbol=plan.symbol,
            now_mono_ns=now_mono_ns,
        )

    meta_count = int(getattr(meta, "fetch_count", 0) or 0)
    pos_count = int(getattr(pos, "fetch_count", 0) or 0)
    return DualLegHotContext(
        profile_name=profile.name,
        metadata_provider=meta,
        position_mode_provider=pos,
        vault=vault,
        lease_supervisor=lease_supervisor,
        meta_fetch_count=meta_count,
        position_fetch_count=pos_count,
    )


def issue_dual_leg_approvals(
    vault: ApprovalVault, plans: Sequence[OrderPlan]
) -> list[ApprovalToken]:
    """Issue plan-bound approvals for both legs (serial under vault lock; cached index)."""
    return [vault.issue(plan) for plan in plans]


def place_pair_parallel(
    *,
    place_pair_fn: Any,
    bybit_kw: Mapping[str, Any],
    okx_kw: Mapping[str, Any],
    warm_session: Any = None,
) -> tuple[Any, Any]:
    """Thin alias: delegate to W6 ``_place_pair_parallel`` (keeps place_io + barrier)."""
    return place_pair_fn(
        bybit_kw=bybit_kw, okx_kw=okx_kw, warm_session=warm_session
    )

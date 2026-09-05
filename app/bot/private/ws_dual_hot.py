"""Dual-leg hot path: old ``bybit_ws.py`` queue→send shape in the private stack.

Speed reference (historic gear-1 ``bybit_ws.py`` / ``a1ba2b1``):
- One asyncio loop; private WS auth once at startup.
- Long-lived ``sender`` task per venue: ``order_msg = await cmd_queue.get()``
  then ``await ws.send(json.dumps(order_msg))`` — that *is* the send path.
- ``trade_manager`` on signal only builds two JSON payloads and
  ``await queue.put`` on both queues (near-parallel).
- No per-dual auth/reseed, no lease/approval/journal on the send critical path.

This module keeps journal / approval / recovery / warm keepalive **correct**,
but moves them off or ahead of the venue-send step:

| Old ``bybit_ws.py`` | New hot path |
|---------------------|--------------|
| Startup auth+connect | ``PrivateWarmSession`` + keepalive (PR #9/#12) |
| ``cmd_queue.put(order_json)`` | ``TradeSendQueue.put_text`` / ``dispatch_prepared`` |
| ``sender``: ``ws.send`` only | Owner-loop ``socket.send_text`` (thread-safe) |
| Build two JSONs on signal | ``prepare_approved`` (durable) then enqueue both |
| No journal | Journal/approval amortized before enqueue; fsync residual |

Residual latency after a warm session is ready is expected to be
**max(leg venue RTT) + thin journal fsync**, not full per-leg prepare storms.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
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
from app.bot.private.order_sender import (
    ApprovalBoundSender,
    PreparedDispatch,
    SendResult,
    assert_send_gates,
)
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
    """Amortize profile / lease / approval-index / preflight before place.

    Call once per dual-leg signal **before** ``prepare_approved`` / enqueue.
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


@dataclass
class _SendCmd:
    text: str
    done: threading.Event = field(default_factory=threading.Event)
    error: list[BaseException] = field(default_factory=list)


class TradeSendQueue:
    """Long-lived sender: ``get`` → ``socket.send_text`` only (old bybit_ws shape).

    PR #12 sockets already own an asyncio loop thread; this queue keeps the
    *logical* critical path as enqueue→send without redoing prepare work.
    """

    def __init__(self, socket: Any, *, name: str = "trade-send") -> None:
        self._socket = socket
        self._q: queue.Queue[Optional[_SendCmd]] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"bbot-{name}", daemon=True
        )
        self._started = False
        self.send_count = 0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)
        if self._started:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            cmd = self._q.get()
            if cmd is None:
                return
            try:
                self._socket.send_text(cmd.text)
                self.send_count += 1
            except BaseException as exc:  # noqa: BLE001
                cmd.error.append(exc)
            finally:
                cmd.done.set()

    def put_text(self, text: str, *, timeout_sec: float = 5.0) -> None:
        """Enqueue one outbound frame and wait until ``send_text`` returns.

        Mirrors ``await cmd_queue.put`` + sender ``ws.send`` completion.
        """
        if not self._started:
            self.start()
        cmd = _SendCmd(text=text)
        self._q.put(cmd)
        if not cmd.done.wait(timeout=float(timeout_sec)):
            raise TimeoutError("trade send queue timed out")
        if cmd.error:
            raise cmd.error[0]


def prepare_dual_legs(
    *,
    bybit_sender: ApprovalBoundSender,
    okx_sender: ApprovalBoundSender,
    bybit_plan: OrderPlan,
    okx_plan: OrderPlan,
    bybit_token: ApprovalToken,
    okx_token: ApprovalToken,
    bybit_credentials: Any,
    okx_credentials: Any,
    env: Mapping[str, str],
    bybit_reconnect_generation: Optional[int] = None,
    okx_reconnect_generation: Optional[int] = None,
    hot_ready: bool = True,
) -> tuple[PreparedDispatch | SendResult, PreparedDispatch | SendResult]:
    """Durable prepare both legs on the caller thread (before enqueue/send)."""
    left = bybit_sender.prepare_approved(
        bybit_plan,
        bybit_token,
        bybit_credentials,
        env,
        journal_transport="ws_trade",
        reconnect_generation=bybit_reconnect_generation,
        hot_ready=hot_ready,
    )
    right = okx_sender.prepare_approved(
        okx_plan,
        okx_token,
        okx_credentials,
        env,
        journal_transport="ws_trade",
        reconnect_generation=okx_reconnect_generation,
        hot_ready=hot_ready,
    )
    return left, right


def enqueue_dual_dispatch(
    *,
    bybit_sender: ApprovalBoundSender,
    okx_sender: ApprovalBoundSender,
    bybit_prepared: PreparedDispatch,
    okx_prepared: PreparedDispatch,
    bybit_transport: Any,
    okx_transport: Any,
    warm_session: Any = None,
) -> tuple[SendResult, SendResult]:
    """Near-parallel venue send after both prepares — old dual ``queue.put`` shape."""
    import contextlib

    barrier = threading.Barrier(2)
    out: dict[str, SendResult] = {}

    def worker(key: str, sender: ApprovalBoundSender, prepared: PreparedDispatch, transport: Any) -> None:
        try:
            out[key] = sender.dispatch_prepared(
                prepared, dispatch_barrier=barrier, transport=transport
            )
        except Exception:  # noqa: BLE001
            try:
                barrier.abort()
            except Exception:  # noqa: BLE001
                pass
            out[key] = SendResult(
                status="rejected",
                plan_summary=prepared.summary,
                journal_ok=False,
                transport_invoked=False,
                error_code="internal_error",
            )

    guard = (
        warm_session.place_io_section()
        if warm_session is not None
        else contextlib.nullcontext()
    )
    with guard:
        t_b = threading.Thread(
            target=worker,
            args=("bybit", bybit_sender, bybit_prepared, bybit_transport),
            daemon=False,
        )
        t_o = threading.Thread(
            target=worker,
            args=("okx", okx_sender, okx_prepared, okx_transport),
            daemon=False,
        )
        t_b.start()
        t_o.start()
        t_b.join()
        t_o.join()
    return (
        out.get(
            "bybit",
            SendResult(
                status="rejected",
                plan_summary=bybit_prepared.summary,
                journal_ok=False,
                transport_invoked=False,
                error_code="internal_error",
            ),
        ),
        out.get(
            "okx",
            SendResult(
                status="rejected",
                plan_summary=okx_prepared.summary,
                journal_ok=False,
                transport_invoked=False,
                error_code="internal_error",
            ),
        ),
    )

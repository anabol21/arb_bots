"""Contour B post-send dual-leg trade ACK (not fill, not a pre-send gate).

After both ``ws.send``s, observe Bybit ``order.create`` ``retCode`` and OKX
order ``id`` / ``event=error``. Local position updates only when both accept.
Timeout or reject → stay flat and flatten any accepted (or timed-out) open
leg. Reuses warm ``recv_trade_ack``; does not wait for fill_delivery.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

DEFAULT_ACK_TIMEOUT_SEC = 2.0
ENV_ACK_TIMEOUT_SEC = "BBOT_PRIVATE_ACK_TIMEOUT_SEC"

WaitOneFn = Callable[[str, str, float], "AckOutcome"]


@dataclass
class AckOutcome:
    """One venue place ACK. ``accepted`` is trade-socket accept, not a fill."""

    venue: str
    req_id: str
    accepted: bool
    timed_out: bool
    venue_code: Optional[str] = None
    recv_ns: Optional[int] = None
    error: Optional[str] = None

    @property
    def status(self) -> str:
        if self.timed_out:
            return "timeout"
        if self.accepted:
            return "accepted"
        return "rejected"


@dataclass
class DualAckResult:
    """Paired Bybit + OKX ACK after both sends."""

    bybit: AckOutcome
    okx: AckOutcome
    recv_started_ns: int
    recv_finished_ns: int

    @property
    def both_accepted(self) -> bool:
        return (
            self.bybit.accepted
            and self.okx.accepted
            and not self.bybit.timed_out
            and not self.okx.timed_out
        )

    @property
    def any_timeout(self) -> bool:
        return self.bybit.timed_out or self.okx.timed_out

    @property
    def any_reject(self) -> bool:
        return (not self.bybit.accepted and not self.bybit.timed_out) or (
            not self.okx.accepted and not self.okx.timed_out
        )


@dataclass
class FlattenAttempt:
    """Record of a reduce-only flatten send for one accepted/timeout open leg."""

    venue: str
    req_id: Optional[str]
    reason: str
    sent_ns: Optional[int] = None
    error: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


def resolve_ack_timeout_sec(env: Optional[Mapping[str, str]] = None) -> float:
    """Short post-send ACK budget. Not a fill wait. Default 2s."""
    e = env if env is not None else os.environ
    raw = e.get(ENV_ACK_TIMEOUT_SEC)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_ACK_TIMEOUT_SEC
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{ENV_ACK_TIMEOUT_SEC} must be a positive number") from exc
    if val <= 0 or val > 30:
        raise ValueError(f"{ENV_ACK_TIMEOUT_SEC} must be in (0, 30], got {val}")
    return val


def abort_string(result: DualAckResult) -> str:
    """Fail-closed abort token for ``LiveBroker.place``."""
    parts: list[str] = []
    for outcome in (result.bybit, result.okx):
        if outcome.timed_out:
            parts.append(outcome.venue)
        elif not outcome.accepted:
            code = outcome.venue_code or outcome.error or "reject"
            parts.append(f"{outcome.venue}:{code}")
    detail = ",".join(parts) if parts else "unknown"
    if result.any_timeout and not result.any_reject:
        return f"dual_ack_timeout:{detail}"
    return f"dual_ack_rejected:{detail}"


def flatten_venues(*, result: DualAckResult, phase: str) -> list[str]:
    """Open-phase only: flatten accepted or timed-out legs. Never flatten a reject.

    Timeout is fail-closed (ack unknown; venue may have the fill). Close-phase
    accepted legs are already reduce-only — do not reverse them.
    """
    if phase != "open":
        return []
    out: list[str] = []
    for outcome in (result.bybit, result.okx):
        if outcome.accepted or outcome.timed_out:
            out.append(outcome.venue)
    return out


def _timeout_outcome(venue: str, req_id: str, *, error: str = "timeout") -> AckOutcome:
    return AckOutcome(
        venue=venue,
        req_id=req_id,
        accepted=False,
        timed_out=True,
        recv_ns=time.monotonic_ns(),
        error=error,
    )


def recv_one_trade_ack(
    *,
    venue: str,
    req_id: str,
    timeout_sec: float,
    runtime: Any = None,
) -> AckOutcome:
    """One-leg ``recv_trade_ack``. Missing runtime → timeout (fail-closed)."""
    if runtime is None:
        return _timeout_outcome(venue, req_id, error="no_trade_runtime")
    try:
        obs = runtime.recv_trade_ack(
            expect_req_id=req_id, timeout_sec=float(timeout_sec)
        )
    except TimeoutError:
        return _timeout_outcome(venue, req_id, error="timeout")
    except Exception as exc:  # noqa: BLE001 — never log payload/secrets
        return AckOutcome(
            venue=venue,
            req_id=req_id,
            accepted=False,
            timed_out=False,
            recv_ns=time.monotonic_ns(),
            error=type(exc).__name__,
        )
    accepted = bool(getattr(obs, "accepted", False))
    return AckOutcome(
        venue=venue,
        req_id=str(getattr(obs, "req_id", req_id) or req_id),
        accepted=accepted,
        timed_out=False,
        venue_code=getattr(obs, "venue_code", None),
        recv_ns=time.monotonic_ns(),
    )


def wait_dual_place_acks(
    *,
    bybit_req_id: str,
    okx_req_id: str,
    timeout_sec: float,
    bybit_runtime: Any = None,
    okx_runtime: Any = None,
    wait_one: Optional[WaitOneFn] = None,
) -> DualAckResult:
    """Wait both venue ACKs in parallel after both sends. Not a fill wait.

    ``wait_one(venue, req_id, timeout_sec)`` is the test injection point.
    Production uses warm ``PrivateStreamRuntime.recv_trade_ack``.
    """
    started = time.monotonic_ns()

    def _call(venue: str, req_id: str) -> AckOutcome:
        if wait_one is not None:
            return wait_one(venue, req_id, float(timeout_sec))
        runtime = bybit_runtime if venue == "bybit" else okx_runtime
        return recv_one_trade_ack(
            venue=venue,
            req_id=req_id,
            timeout_sec=float(timeout_sec),
            runtime=runtime,
        )

    bybit: Optional[AckOutcome] = None
    okx: Optional[AckOutcome] = None
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dual-ack") as pool:
        futs = {
            pool.submit(_call, "bybit", bybit_req_id): "bybit",
            pool.submit(_call, "okx", okx_req_id): "okx",
        }
        for fut in as_completed(futs, timeout=max(0.05, float(timeout_sec) + 0.5)):
            venue = futs[fut]
            try:
                outcome = fut.result()
            except Exception as exc:  # noqa: BLE001
                outcome = AckOutcome(
                    venue=venue,
                    req_id=bybit_req_id if venue == "bybit" else okx_req_id,
                    accepted=False,
                    timed_out=False,
                    recv_ns=time.monotonic_ns(),
                    error=type(exc).__name__,
                )
            if venue == "bybit":
                bybit = outcome
            else:
                okx = outcome
    if bybit is None:
        bybit = _timeout_outcome("bybit", bybit_req_id, error="join_timeout")
    if okx is None:
        okx = _timeout_outcome("okx", okx_req_id, error="join_timeout")
    return DualAckResult(
        bybit=bybit,
        okx=okx,
        recv_started_ns=started,
        recv_finished_ns=time.monotonic_ns(),
    )

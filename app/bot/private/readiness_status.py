"""Fail-closed, same-host status for a read-only private WS companion.

This is diagnostic input for a *no-order* audit, never a live-send lease.
The writer owns one file per venue; consumers must re-read it before every
audit. A process exit, reboot, stale heartbeat, or malformed file is unready.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA_VERSION = "bbot.private.readiness_status.v1"
MAX_STATUS_BYTES = 4096


def _linux_identity(pid: int) -> Optional[tuple[str, str]]:
    """Boot ID and /proc start ticks defeat stale files and PID reuse."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # comm may contain spaces or parentheses; fields after its final ')'
        # begin with field 3. starttime is field 22, index 19 of this suffix.
        start = stat.rsplit(") ", 1)[1].split()[19]
        if boot and start.isdecimal():
            return boot, start
    except (OSError, IndexError, ValueError):
        pass
    return None


class ReadonlyStatusWriter:
    """Atomically publish one companion's current readiness, with fsync."""

    def __init__(
        self,
        path: Path,
        exchange: str,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if exchange not in {"bybit", "okx"}:
            raise ValueError("invalid_exchange")
        identity = _linux_identity(os.getpid())
        if identity is None:
            raise RuntimeError("same_host_process_identity_unavailable")
        self.path = Path(path)
        self.exchange = exchange
        self.pid = os.getpid()
        self.boot_id, self.start_ticks = identity
        self.clock_ns = clock_ns
        self.sequence = 0

    def publish(self, runtime: Any, *, ready: bool = False) -> None:
        self.sequence += 1
        socket = getattr(runtime, "private_socket", None)
        healthy = bool(
            ready
            and socket is not None
            and getattr(socket, "connected", False) is True
            and getattr(runtime, "trade_socket", None) is None
            and getattr(runtime, "authenticated", False) is True
            and getattr(getattr(runtime, "subscription_readiness", None), "value", None) == "ready"
            and getattr(getattr(runtime, "sequence_state", None), "value", None) == "healthy"
            and getattr(runtime, "reseed_required", True) is False
            and getattr(runtime, "sends_blocked", True) is False
        )
        generation = getattr(runtime, "reconnect_generation", 0)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            healthy = False
            generation = 0
        document = {
            "schema_version": SCHEMA_VERSION,
            "exchange": self.exchange,
            "pid": self.pid,
            "boot_id": self.boot_id,
            "start_ticks": self.start_ticks,
            "sequence": self.sequence,
            "generation": generation,
            "monotonic_ns": self.clock_ns(),
            "ready": healthy,
            "trade_socket_bound": False,
            "orders_sent": 0,
        }
        raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        if len(raw) > MAX_STATUS_BYTES:
            raise RuntimeError("status_too_large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def read_readonly_status(
    path: Path,
    exchange: str,
    *,
    max_age_ns: int,
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> tuple[bool, int]:
    """Return (ready, generation); any uncertainty returns (False, 0)."""
    if exchange not in {"bybit", "okx"} or max_age_ns <= 0:
        return False, 0
    try:
        path = Path(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            mode = os.fstat(fd)
            if not stat.S_ISREG(mode.st_mode) or mode.st_size > MAX_STATUS_BYTES:
                return False, 0
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(MAX_STATUS_BYTES + 1)
            if len(raw) > MAX_STATUS_BYTES:
                return False, 0
        finally:
            os.close(fd)
        record = json.loads(raw.decode("ascii"))
        if not isinstance(record, dict):
            return False, 0
        if record.get("schema_version") != SCHEMA_VERSION or record.get("exchange") != exchange:
            return False, 0
        if record.get("ready") is not True or record.get("trade_socket_bound") is not False:
            return False, 0
        if type(record.get("orders_sent")) is not int or record["orders_sent"] != 0:
            return False, 0
        pid, generation, stamped = (record.get(key) for key in ("pid", "generation", "monotonic_ns"))
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (pid, generation, stamped)):
            return False, 0
        if pid <= 0 or generation < 0 or stamped <= 0:
            return False, 0
        age = clock_ns() - stamped
        if age < 0 or age > max_age_ns:
            return False, 0
        if _linux_identity(pid) != (record.get("boot_id"), record.get("start_ticks")):
            return False, 0
        return True, generation
    except (OSError, UnicodeError, ValueError, TypeError):
        return False, 0

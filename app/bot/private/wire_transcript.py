"""Permanent append-only wire transcript for live private + trade WS.

Standing contract for Contour B / canary: every successful ``ws.send`` and
``recv`` on Bybit/OKX private and trade sockets is stamped and written.
This is not an experiment harness flag.

Hooks run **after** I/O succeeds. They must not add waits or gates on
signal → first/second ``ws.send``. Times are captured on the I/O thread;
the JSONL append is queued off that path (no fsync).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from app.bot.private.paths import (
    _is_under_denied,
    resolve_data_root,
    wire_jsonl_path,
)

SCHEMA_VERSION = "bbot.private.wire.v1"
REDACTED = "[redacted]"

VENUES = frozenset({"bybit", "okx"})
SOCKETS = frozenset({"private", "trade"})
DIRS = frozenset({"out", "in"})

# Secret / signing material — never persist values. Keys compared after
# lowercasing and replacing ``-`` with ``_``.
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "secret",
        "passphrase",
        "password",
        "sign",
        "signature",
        "x_bapi_api_key",
        "x_bapi_sign",
        "authorization",
        "cookie",
        "set_cookie",
        "access_token",
        "refresh_token",
        "bearer_token",
        "private_key",
        "client_secret",
    }
)

# Venue timestamps used for fill_delivery = local_recv_ms − venue_ts.
_VENUE_TS_KEYS = (
    "exectime",
    "filltime",
    "utime",
    "ctime",
    "updatedtime",
    "createdtime",
    "creationtime",
    "ts",
    "e",  # some Bybit private payloads use ``E``/``e`` as event ms
)

_REQ_ID_KEYS = ("reqid", "req_id", "id")

LOG = logging.getLogger("bbot")
_WIRE_LOG = logging.getLogger("bbot.private.wire")

_PROCESS_WIRE: Optional["WireTranscript"] = None
_PROCESS_LOCK = threading.Lock()


def get_process_wire_transcript() -> Optional["WireTranscript"]:
    return _PROCESS_WIRE


def attach_process_wire_transcript(transcript: "WireTranscript") -> "WireTranscript":
    global _PROCESS_WIRE
    with _PROCESS_LOCK:
        _PROCESS_WIRE = transcript
    return transcript


def clear_process_wire_transcript(*, close: bool = True) -> None:
    global _PROCESS_WIRE
    with _PROCESS_LOCK:
        current = _PROCESS_WIRE
        _PROCESS_WIRE = None
    if close and current is not None:
        current.close()


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _is_secret_key(name: object) -> bool:
    return _norm_key(name) in _SECRET_KEYS


def redact_payload(obj: Any, *, op: Optional[str] = None) -> Any:
    """Return a JSON-safe copy with secrets stripped.

    Keeps order / ack / fill body fields needed for chronometry
    (``reqId``, ``op``, ``orderLinkId``, ``clOrdId``, ``execTime``, …).
    """
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if _is_secret_key(key):
                out[str(key)] = REDACTED
                continue
            out[str(key)] = redact_payload(value, op=op)
        return out
    if isinstance(obj, list):
        if str(op or "").lower() == "auth" and len(obj) == 3:
            # Bybit private auth: [apiKey, expires, sign]
            return [REDACTED, redact_payload(obj[1], op=op), REDACTED]
        return [redact_payload(item, op=op) for item in obj]
    if isinstance(obj, tuple):
        return [redact_payload(item, op=op) for item in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(type(obj).__name__)


def parse_wire_text(text: str) -> tuple[Optional[dict[str, Any]], Optional[str], str]:
    """Parse a WS text frame.

    Returns ``(obj_or_none, op, payload_kind)``. Non-JSON ping/pong become
    a structured literal; unparseable text is never echoed (may hold secrets).
    """
    if text in {"ping", "pong"}:
        return {"literal": text}, text, "literal"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, None, "unparsed"
    if not isinstance(data, dict):
        return None, None, "unparsed"
    op = data.get("op") or data.get("event")
    op_s = str(op) if op is not None else None
    return data, op_s, "json"


def extract_req_id(obj: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not isinstance(obj, Mapping):
        return None
    for key in obj:
        if _norm_key(key) in _REQ_ID_KEYS:
            val = obj[key]
            if val is None or isinstance(val, bool):
                continue
            text = str(val).strip()
            if text:
                return text
    return None


def extract_venue_ts_ms(obj: Any) -> Optional[int]:
    """First recognizable venue timestamp (ms) in a parsed frame."""
    found: list[int] = []

    def _walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                nk = _norm_key(key)
                if nk in _VENUE_TS_KEYS:
                    ms = _as_epoch_ms(value)
                    if ms is not None:
                        found.append(ms)
                _walk(value)
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return found[0] if found else None


def _as_epoch_ms(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
    else:
        return None
    # Seconds → ms if it looks like a 10-digit epoch.
    if 1_000_000_000 <= n < 10_000_000_000:
        return n * 1000
    if 1_000_000_000_000 <= n < 10_000_000_000_000:
        return n
    return None


def derive_fill_delivery_ms(
    *,
    local_recv_ms: int,
    venue_ts_ms: Optional[int],
) -> Optional[int]:
    """AB-style ``fill_delivery = local_recv − venue_ts`` when both exist."""
    if venue_ts_ms is None:
        return None
    return int(local_recv_ms) - int(venue_ts_ms)


def event_date_from_wall_ms(wall_ms: int) -> str:
    return datetime.fromtimestamp(wall_ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )


def read_wire_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read complete JSONL rows. A truncated last line is ignored, not repaired."""
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            raw = line.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
    return events


def iter_wire_files(data_root: Path) -> list[Path]:
    wire_root = data_root / "wire"
    if not wire_root.is_dir():
        return []
    return sorted(wire_root.glob("event_date=*/wire.jsonl"))


def scan_all_wire_events(data_root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in iter_wire_files(data_root):
        out.extend(read_wire_jsonl(path))
    return out


class WireTranscript:
    """Process-scoped append-only wire writer + correlation map."""

    def __init__(
        self,
        data_root: Optional[Path] = None,
        *,
        run_id: str,
        env: Optional[Mapping[str, str]] = None,
        async_write: bool = False,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        root = (
            data_root
            if data_root is not None
            else resolve_data_root(dict(env) if env is not None else None)
        )
        if _is_under_denied(root):
            raise RuntimeError(f"refusing wire transcript under denied path: {root}")
        (root / "wire").mkdir(parents=True, exist_ok=True)
        self.data_root = root
        self.run_id = str(run_id)
        # Default sync: append+flush, no fsync. After I/O only; does not gate
        # the other Contour B leg. Async is optional (tests use TemporaryDirectory).
        self._async = bool(async_write)
        self._log = logger or LOG
        self._seq = 0
        self._write_lock = threading.Lock()
        self._corr: dict[str, dict[str, Any]] = {}
        self._corr_lock = threading.Lock()
        self._handles: dict[str, Any] = {}
        self._queue: queue.Queue[Optional[dict[str, Any]]] = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if self._async:
            t = threading.Thread(
                target=self._writer_loop,
                name=f"bbot-wire-{self.run_id[:12]}",
                daemon=True,
            )
            self._thread = t
            t.start()

    def bind_place_correlation(
        self,
        *,
        req_id: str,
        intent_id: Optional[str] = None,
        dual_leg_id: Optional[str] = None,
        signal_ts_ms: Optional[int] = None,
        venue: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> None:
        """Associate an outbound place ``req_id`` with live-broker ids.

        Call **before** ``ws.send``. Dict write only — no I/O, no wait.
        """
        rid = str(req_id).strip()
        if not rid:
            return
        rec: dict[str, Any] = {}
        if intent_id:
            rec["intent_id"] = str(intent_id)
        if dual_leg_id:
            rec["dual_leg_id"] = str(dual_leg_id)
        if signal_ts_ms is not None:
            rec["signal_ts_ms"] = int(signal_ts_ms)
        if venue:
            rec["venue"] = str(venue)
        if phase:
            rec["phase"] = str(phase)
        if not rec:
            return
        with self._corr_lock:
            prev = self._corr.get(rid, {})
            prev.update(rec)
            self._corr[rid] = prev

    def lookup_correlation(self, req_id: Optional[str]) -> dict[str, Any]:
        if not req_id:
            return {}
        with self._corr_lock:
            return dict(self._corr.get(str(req_id), {}))

    def record_io(
        self,
        *,
        direction: str,
        venue: str,
        socket: str,
        text: str,
        wall_ms: Optional[int] = None,
        mono_ns: Optional[int] = None,
        reconnect_generation: Optional[int] = None,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Stamp and enqueue one send/recv. Safe to call after I/O succeeds."""
        if direction not in DIRS:
            raise ValueError(f"dir must be out|in, got {direction!r}")
        v = str(venue).strip().lower()
        if v.endswith("_live"):
            v = v[: -len("_live")]
        if v not in VENUES:
            raise ValueError(f"venue must be bybit|okx, got {venue!r}")
        sock = str(socket).strip().lower()
        if sock in {"private_stream", "private"}:
            sock = "private"
        elif sock != "trade":
            raise ValueError(f"socket must be private|trade, got {socket!r}")

        wall = int(wall_ms if wall_ms is not None else time.time() * 1000)
        mono = int(mono_ns if mono_ns is not None else time.monotonic_ns())
        parsed, op, kind = parse_wire_text(text)
        req_id = extract_req_id(parsed)
        venue_ts = extract_venue_ts_ms(parsed)
        corr = self.lookup_correlation(req_id)
        payload: Any
        if kind == "json" and parsed is not None:
            payload = redact_payload(parsed, op=op)
        elif kind == "literal" and parsed is not None:
            payload = parsed
        else:
            payload = {"unparsed": True, "payload_bytes": len(text.encode("utf-8"))}

        event: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "wall_ms": wall,
            "mono_ns": mono,
            "dir": direction,
            "venue": v,
            "socket": sock,
            "run_id": str(run_id or self.run_id),
            "op": op,
            "req_id": req_id,
            "payload_kind": kind,
            "payload": payload,
        }
        if reconnect_generation is not None:
            event["reconnect_generation"] = int(reconnect_generation)
        if venue_ts is not None:
            event["venue_ts_ms"] = venue_ts
        if direction == "in" and venue_ts is not None:
            event["fill_delivery_ms"] = derive_fill_delivery_ms(
                local_recv_ms=wall, venue_ts_ms=venue_ts
            )
        for key in ("intent_id", "dual_leg_id", "signal_ts_ms", "phase"):
            if key in corr:
                event[key] = corr[key]
        if direction == "out" and event.get("signal_ts_ms") is not None:
            event["signal_to_send_ms"] = int(wall) - int(event["signal_ts_ms"])

        self._emit_log_line(event)
        if self._async:
            self._queue.put(event)
        else:
            self._append(event)
        return event

    def _emit_log_line(self, event: Mapping[str, Any]) -> None:
        tag = "wire_out" if event["dir"] == "out" else "wire_in"
        parts = [
            tag,
            f"venue={event['venue']}",
            f"socket={event['socket']}",
            f"wall_ms={event['wall_ms']}",
            f"mono_ns={event['mono_ns']}",
        ]
        if event.get("op"):
            parts.append(f"op={event['op']}")
        if event.get("req_id"):
            parts.append(f"req_id={event['req_id']}")
        if event.get("intent_id"):
            parts.append(f"intent_id={event['intent_id']}")
        if event.get("dual_leg_id"):
            parts.append(f"dual_leg_id={event['dual_leg_id']}")
        if event.get("reconnect_generation") is not None:
            parts.append(f"gen={event['reconnect_generation']}")
        if event.get("venue_ts_ms") is not None:
            parts.append(f"venue_ts_ms={event['venue_ts_ms']}")
        if event.get("fill_delivery_ms") is not None:
            parts.append(f"fill_delivery_ms={event['fill_delivery_ms']}")
        line = " | ".join(parts)
        try:
            self._log.info(line)
        except Exception:  # noqa: BLE001
            _WIRE_LOG.info(line)

    def _writer_loop(self) -> None:
        while True:
            ev = self._queue.get()
            try:
                if ev is None:
                    return
                self._append(ev)
            finally:
                self._queue.task_done()

    def _append(self, event: dict[str, Any]) -> None:
        if self._stop.is_set():
            return
        with self._write_lock:
            if self._stop.is_set():
                return
            self._seq += 1
            event["seq"] = self._seq
            date = event_date_from_wall_ms(int(event["wall_ms"]))
            event["event_date"] = date
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            try:
                fh = self._handles.get(date)
                if fh is None:
                    if not self.data_root.exists():
                        return
                    path = wire_jsonl_path(self.data_root, date)
                    if _is_under_denied(path):
                        raise RuntimeError(
                            f"refusing wire append under denied path: {path}"
                        )
                    fh = path.open("a", encoding="utf-8")
                    self._handles[date] = fh
                fh.write(line + "\n")
                fh.flush()
            except OSError:
                if self._stop.is_set() or not self.data_root.exists():
                    return
                raise

    def flush(self, timeout_sec: float = 2.0) -> None:
        if not self._async:
            return
        del timeout_sec
        self._queue.join()

    def close(self) -> None:
        already = self._stop.is_set()
        self._stop.set()
        if self._async and not already:
            self._queue.put(None)
            t = self._thread
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=2.0)
            self._thread = None
        with self._write_lock:
            for fh in self._handles.values():
                try:
                    fh.close()
                except OSError:
                    pass
            self._handles.clear()


class WireTranscriptSocket:
    """Delegating socket: record after successful send/recv. Never blocks I/O.

    Timeouts and send failures propagate unchanged and do not write a row.
    Transcript errors after a successful I/O are swallowed.
    """

    def __init__(
        self,
        inner: Any,
        *,
        transcript: WireTranscript,
        venue: str,
        socket: str,
        run_id: Optional[str] = None,
        generation_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_transcript", transcript)
        object.__setattr__(self, "_venue", venue)
        object.__setattr__(self, "_socket", socket)
        object.__setattr__(self, "_run_id", run_id or transcript.run_id)
        object.__setattr__(self, "_generation_fn", generation_fn)

    def send_text(self, text: str) -> None:
        self._inner.send_text(text)
        self._safe_record("out", text)

    def recv_text(self, *, timeout_sec: Optional[float] = None) -> str:
        text = self._inner.recv_text(timeout_sec=timeout_sec)
        self._safe_record("in", text)
        return text

    def close(self) -> None:
        self._inner.close()

    @property
    def connected(self) -> bool:
        return bool(getattr(self._inner, "connected", False))

    def _generation(self) -> Optional[int]:
        fn = self._generation_fn
        if fn is None:
            return None
        try:
            return int(fn())
        except Exception:  # noqa: BLE001
            return None

    def _safe_record(self, direction: str, text: str) -> None:
        try:
            wall_ms = int(time.time() * 1000)
            mono_ns = time.monotonic_ns()
            self._transcript.record_io(
                direction=direction,
                venue=self._venue,
                socket=self._socket,
                text=text,
                wall_ms=wall_ms,
                mono_ns=mono_ns,
                reconnect_generation=self._generation(),
                run_id=self._run_id,
            )
        except Exception:  # noqa: BLE001 — never fail the live I/O path
            _WIRE_LOG.exception("wire_transcript_record_failed")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap_socket(
    sock: Any,
    *,
    transcript: WireTranscript,
    venue: str,
    socket: str,
    run_id: Optional[str] = None,
    generation_fn: Optional[Callable[[], int]] = None,
) -> Any:
    """Hook send/recv on ``sock`` in place so ``isinstance`` stays valid.

    Warm-session tests (and production) keep the concrete socket type
    (``FakePrivateWsSocket`` / ``WebsocketsClientSocket``). A delegating
    wrapper is still available as ``WireTranscriptSocket`` for unit tests.
    """
    if isinstance(sock, WireTranscriptSocket):
        return sock
    if getattr(sock, "_wire_transcript", None) is not None:
        return sock
    recorder = WireTranscriptSocket(
        sock,
        transcript=transcript,
        venue=venue,
        socket=socket,
        run_id=run_id,
        generation_fn=generation_fn,
    )
    orig_send = sock.send_text
    orig_recv = sock.recv_text

    def send_text(text: str) -> None:
        orig_send(text)
        recorder._safe_record("out", text)  # noqa: SLF001

    def recv_text(*, timeout_sec: Optional[float] = None) -> str:
        text = orig_recv(timeout_sec=timeout_sec)
        recorder._safe_record("in", text)  # noqa: SLF001
        return text

    sock.send_text = send_text  # type: ignore[method-assign]
    sock.recv_text = recv_text  # type: ignore[method-assign]
    sock._wire_transcript = transcript
    return sock


def wrap_warm_bundle(
    bundle: Any,
    *,
    transcript: WireTranscript,
    run_id: str,
    bybit_generation_fn: Optional[Callable[[], int]] = None,
    okx_generation_fn: Optional[Callable[[], int]] = None,
) -> Any:
    """Wrap the four warm sockets. ``bundle`` is a ``WarmSocketBundle``."""
    return type(bundle)(
        bybit_private=wrap_socket(
            bundle.bybit_private,
            transcript=transcript,
            venue="bybit",
            socket="private",
            run_id=run_id,
            generation_fn=bybit_generation_fn,
        ),
        bybit_trade=wrap_socket(
            bundle.bybit_trade,
            transcript=transcript,
            venue="bybit",
            socket="trade",
            run_id=run_id,
            generation_fn=bybit_generation_fn,
        ),
        okx_private=wrap_socket(
            bundle.okx_private,
            transcript=transcript,
            venue="okx",
            socket="private",
            run_id=run_id,
            generation_fn=okx_generation_fn,
        ),
        okx_trade=wrap_socket(
            bundle.okx_trade,
            transcript=transcript,
            venue="okx",
            socket="trade",
            run_id=run_id,
            generation_fn=okx_generation_fn,
        ),
    )


def bind_place_on_process_transcript(
    *,
    req_ids: Sequence[tuple[str, str]],
    intent_id: Optional[str] = None,
    dual_leg_id: Optional[str] = None,
    signal_ts_ms: Optional[int] = None,
    phase: Optional[str] = None,
) -> None:
    """No-op when no process transcript is attached.

    ``req_ids`` is ``((venue, req_id), ...)``.
    """
    tr = get_process_wire_transcript()
    if tr is None:
        return
    for venue, req_id in req_ids:
        tr.bind_place_correlation(
            req_id=req_id,
            intent_id=intent_id,
            dual_leg_id=dual_leg_id,
            signal_ts_ms=signal_ts_ms,
            venue=venue,
            phase=phase,
        )

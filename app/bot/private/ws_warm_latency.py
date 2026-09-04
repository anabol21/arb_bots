"""Warm-Lat: trade-latency experiments on an already-warm private+trade WS.

Measures stage timestamps after ``warm ready=True`` so remaining delay is
isolated from cold auth/subscribe/reseed.

Paths
-----
A — minimal queue→send shape (inspired by legacy ``else/bybit_ws.py``, which
    is **not** on this branch): pre-built JSON → ``asyncio.Queue`` → long-lived
    sender that only ``ws.send``; no approval/lease/journal on the critical path.
B — production W6/W7 prepare path on the same warm sockets (approval → lease →
    profile/preflight → order_prepared → request_sent → ack → terminal).

Modes
-----
* dry / ``send=false`` (default): times everything up to would-send; Path A
  uses an in-process fake warm socket; Path B exercises real journal/approval/
  lease/preflight with stub metadata (no venue network).
* live / ``send=true``: requires ``VENUE=live`` + ``LIVE_ORDERS=1`` +
  ``BBOT_PRIVATE_WARM_LAT=1`` + ``--warm-lat-approve-one-shot`` (same pattern
  as W6/W7). Agents must not run live; humans gate tiny notional on VPS.

CLI: ``--ws-warm-latency`` (see ``docs/b-private-warm-latency-experiments.md``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
from app.bot.private.order_approval import ApprovalVault
from app.bot.private.order_lease import LeaseSupervisor
from app.bot.private.order_metadata import InstrumentMetadata, StaticMetadataProvider
from app.bot.private.order_plan import build_order_plan
from app.bot.private.order_preflight import (
    StaticVerifiedPositionModeProvider,
    assert_preflight_ready,
)
from app.bot.private.order_sender import assert_default_entrypoint_cannot_transport
from app.bot.private.paths import resolve_data_root
from app.bot.private.warm_latency_stages import (
    PATH_A_SKIPPED_STAGES,
    STAGE_LABELS,
    StageTrace,
    WarmLatencyReport,
    write_results_csv,
    write_results_json,
    write_summary_csv,
)
from app.bot.private.ws_gates import WsProfileGateError, assert_ws_warm_lat_gates

LOG = logging.getLogger("bbot.private.ws_warm_latency")

WARM_LAT_N_MIN = 1
WARM_LAT_N_MAX = 50
# Live send is intentionally tighter than dry (same risk discipline as W6/W7).
WARM_LAT_LIVE_N_MAX = 10
TRADE_LAT_MODEL_MS = 100.0

# Immutable matched dual-leg profile reused from W6/W7 (clears Bybit minNotional).
# Do not substitute SOL/XRP here — W6 harness rejects non-TRUMP plans.
WARM_LAT_LIVE_PROFILE = {
    "alias": "TRUMP-USDT-PERP",
    "bybit_symbol": "TRUMPUSDT",
    "okx_symbol": "TRUMP-USDT-SWAP",
    "bybit_qty": "4.0",
    "okx_qty": "40",
    "approx_notional_usd_per_leg": "6-8",
    "risk_cap_usd_per_venue": 100,
    "why_not_sol_xrp": (
        "W6/W7 immutable matched TRUMP profile clears Bybit minNotional≈5 USDT "
        "with aligned OKX ctVal lots; SOL/XRP gear2 sizes are not wired into "
        "the private dual-leg harness."
    ),
}

VPS_HOST = "root@38.180.94.108"
VPS_STAGING = "/root/spread_staging"
VPS_RESULTS_DIR = "/data/bbot-gear2/private/warm_lat"
VPS_DATA_ROOT = "/data/bbot-gear2/private"
VPS_SECRETS = "/etc/spread/bbot-private-live.env"

# Surgical copy set for Warm-Lat only (never full-repo overlay).
VPS_SURGICAL_FILES: tuple[str, ...] = (
    "app/bot/private/warm_latency_stages.py",
    "app/bot/private/ws_warm_latency.py",
    "app/bot/private/ws_gates.py",
    "app/bot/private/harness_readonly.py",
)


class WarmLatProfileError(ValueError):
    """Invalid Warm-Lat CLI / profile."""


def print_vps_live_recipe() -> str:
    """Exact human recipe for VPS live Warm-Lat (no network from this helper)."""
    files = " ".join(VPS_SURGICAL_FILES)
    text = f"""# Warm-Lat live VPS recipe (human only; agents must not SSH/run live)
# Host: {VPS_HOST}   Staging: {VPS_STAGING}
# Profile: W6/W7 TRUMP dual-leg (~$6–8/leg, ≪ $100/venue). Not SOL/XRP.

# --- 0) Preflight (read-only) ---
# ssh {VPS_HOST} 'systemctl is-active spread-collector; systemctl show -p MainPID --value spread-collector'
# Do NOT restart collector. Do NOT touch /data/live /data/bars /data/compacted.

# --- 1) Surgical deploy (copy these files only; preserve VPS flatten/aplace/sample-cap) ---
# From repo root on a machine with the PR tree (paths relative to repo):
#   for f in {files}; do
#     scp "$f" {VPS_HOST}:{VPS_STAGING}/$f
#   done
# NEVER: git reset --hard / full-repo rsync over staging
# (would wipe VPS-only flatten extras, aplace, sample-cap close-skip patches).

# Files:
#   - app/bot/private/warm_latency_stages.py
#   - app/bot/private/ws_warm_latency.py
#   - app/bot/private/ws_gates.py
#   - app/bot/private/harness_readonly.py

# --- 2) Env + live Path B (start n=1, then n<=5; hard cap n<={WARM_LAT_LIVE_N_MAX}) ---
# ssh {VPS_HOST}
set -a
source {VPS_SECRETS}   # mode 600; not git
set +a
export VENUE=live
export LIVE_ORDERS=1
export BBOT_PRIVATE_WARM_LAT=1
export BBOT_PRIVATE_DATA_ROOT={VPS_DATA_ROOT}
mkdir -p {VPS_RESULTS_DIR}

cd {VPS_STAGING}
/root/venv/bin/python -m app.bot.private --ws-warm-latency \\
  --warm-lat-n=1 \\
  --warm-lat-path=B \\
  --warm-lat-mode=parallel \\
  --warm-lat-venue=dual \\
  --warm-lat-send=true \\
  --warm-lat-approve-one-shot \\
  --warm-lat-out={VPS_RESULTS_DIR}

# --- 3) Parse p50/p95 ---
# python3 -c "import json; d=json.load(open('{VPS_RESULTS_DIR}/warm_lat_results.json'));
# print(d['schema_version'], d['status'], d['notes'].get('w6_flat_after'));
# print(json.dumps(d['summary'], indent=2))"
# Expect notes.w6_status=ok and notes.w6_flat_after=true before trusting latency.

# --- 4) Half-leg / abort ---
# If status!=ok or flat_after!=true: STOP further N. Confirm both venues flat via
# W6 baseline / exchange UI. Do not raise n. Do not leave leftover exposure.
"""
    return text


@dataclass(frozen=True)
class WarmLatCli:
    n: int
    path: str  # A | B | AB
    open_mode: str  # serial | parallel | single
    send_enabled: bool
    approve_one_shot: bool
    venue: str  # dual | bybit | okx
    out_dir: Optional[Path]
    attach_warm: bool


def assert_warm_lat_n(n: int, *, send_enabled: bool = False) -> int:
    if not isinstance(n, int) or n < WARM_LAT_N_MIN or n > WARM_LAT_N_MAX:
        raise WarmLatProfileError(
            f"Warm-Lat requires --warm-lat-n={WARM_LAT_N_MIN}..{WARM_LAT_N_MAX}"
        )
    if send_enabled and n > WARM_LAT_LIVE_N_MAX:
        raise WarmLatProfileError(
            f"Warm-Lat live send requires --warm-lat-n={WARM_LAT_N_MIN}..{WARM_LAT_LIVE_N_MAX} "
            f"(start with 1; recommend ≤5)"
        )
    return n


def parse_warm_lat_cli_args(argv: Sequence[str]) -> WarmLatCli:
    n: Optional[int] = None
    path = "AB"
    open_mode = "serial"
    send_enabled = False
    approve = False
    venue = "dual"
    out_dir: Optional[Path] = None
    attach_warm = False
    for arg in argv:
        if arg.startswith("--warm-lat-n="):
            raw = arg.split("=", 1)[1].strip()
            try:
                n = int(raw)
            except ValueError:
                n = -1
        elif arg.startswith("--warm-lat-path="):
            path = arg.split("=", 1)[1].strip().upper()
        elif arg.startswith("--warm-lat-mode="):
            open_mode = arg.split("=", 1)[1].strip().lower()
        elif arg.startswith("--warm-lat-send="):
            raw = arg.split("=", 1)[1].strip().lower()
            send_enabled = raw in {"1", "true", "yes", "on"}
        elif arg.startswith("--warm-lat-venue="):
            venue = arg.split("=", 1)[1].strip().lower()
        elif arg.startswith("--warm-lat-out="):
            out_dir = Path(arg.split("=", 1)[1].strip())
        elif arg == "--warm-lat-approve-one-shot":
            approve = True
        elif arg == "--warm-lat-attach-warm":
            attach_warm = True
        elif arg in {"--warm-lat-print-vps-recipe", "--warm-lat-help-vps"}:
            # Handled by main before full parse requires n.
            continue
    if n is None:
        raise WarmLatProfileError("Warm-Lat requires --warm-lat-n=1..50")
    assert_warm_lat_n(n, send_enabled=send_enabled)
    if path not in {"A", "B", "AB"}:
        raise WarmLatProfileError("--warm-lat-path must be A, B, or AB")
    if open_mode not in {"serial", "parallel", "single"}:
        raise WarmLatProfileError("--warm-lat-mode must be serial|parallel|single")
    if venue not in {"dual", "bybit", "okx"}:
        raise WarmLatProfileError("--warm-lat-venue must be dual|bybit|okx")
    if open_mode == "single" and venue == "dual":
        venue = "bybit"
    return WarmLatCli(
        n=n,
        path=path,
        open_mode=open_mode,
        send_enabled=send_enabled,
        approve_one_shot=approve,
        venue=venue,
        out_dir=out_dir,
        attach_warm=attach_warm,
    )


def _trump_meta() -> StaticMetadataProvider:
    from decimal import Decimal

    asof = time.monotonic_ns()
    return StaticMetadataProvider(
        {
            ("bybit_live", "TRUMPUSDT"): InstrumentMetadata(
                venue="bybit_live",
                symbol="TRUMPUSDT",
                min_qty=Decimal("0.1"),
                qty_step=Decimal("0.1"),
                tick_size=Decimal("0.001"),
                contract_multiplier=Decimal("1"),
                contract_value_ccy="USDT",
                notional_unit="usdt_per_coin",
                mark_price_usdt=Decimal("1.70"),
                mark_asof_monotonic_ns=asof,
                mark_max_age_ns=60_000_000_000,
            ),
            ("okx_live", "TRUMP-USDT-SWAP"): InstrumentMetadata(
                venue="okx_live",
                symbol="TRUMP-USDT-SWAP",
                min_qty=Decimal("1"),
                qty_step=Decimal("1"),
                tick_size=Decimal("0.001"),
                contract_multiplier=Decimal("0.1"),
                contract_value_ccy="USDT",
                notional_unit="usdt_per_contract",
                mark_price_usdt=Decimal("1.70"),
                mark_asof_monotonic_ns=asof,
                mark_max_age_ns=60_000_000_000,
                inst_id_code=193761,
            ),
        }
    )


def _position() -> StaticVerifiedPositionModeProvider:
    return StaticVerifiedPositionModeProvider(
        mode_by_venue={
            "bybit_live": "one_way",
            "okx_live": "one_way",
        }
    )


def _dry_live_env(td: Path) -> dict[str, str]:
    """Minimal live profile env for Path B dry (fake secrets; no network)."""
    from app.bot.private.secrets import LIVE_KEY_NAMES

    td.mkdir(parents=True, exist_ok=True)
    live_env = td / "bbot-private-live.env"
    live_env.write_text(
        "\n".join(f"{n}=dry{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
        encoding="utf-8",
    )
    data = td / "data"
    data.mkdir(parents=True, exist_ok=True)
    return {
        "VENUE": "live",
        "LIVE_ORDERS": "1",
        "BBOT_PRIVATE_WARM_LAT": "1",
        "BBOT_PRIVATE_ENV_FILE": str(live_env),
        "BBOT_PRIVATE_DATA_ROOT": str(data),
    }


class _FakeWarmTradeSocket:
    """In-process stand-in for a warm trade WS (Path A dry / would-send)."""

    def __init__(self) -> None:
        self.connected = True
        self.sent: list[str] = []
        self._lock = threading.Lock()

    def send_text(self, payload: str) -> None:
        with self._lock:
            if not self.connected:
                raise RuntimeError("fake warm socket closed")
            self.sent.append(payload)

    def close(self) -> None:
        self.connected = False


async def _path_a_queue_send_cycle(
    *,
    sock: _FakeWarmTradeSocket,
    payload: str,
    queue: "asyncio.Queue[Optional[str]]",
    sender_started: asyncio.Event,
) -> tuple[int, int]:
    """Enqueue then await long-lived sender; return (enqueue_ns, send_ns)."""
    await sender_started.wait()
    enqueue_ns = time.monotonic_ns()
    await queue.put(payload)

    # Wait until sender has drained this payload (sent list grows).
    before = len(sock.sent)
    for _ in range(10_000):
        if len(sock.sent) > before:
            # send_ns approximated as last mark inside sender via note on payload id
            break
        await asyncio.sleep(0.00005)
    send_ns = time.monotonic_ns()
    return enqueue_ns, send_ns


async def _path_a_sender_loop(
    sock: _FakeWarmTradeSocket,
    queue: "asyncio.Queue[Optional[str]]",
    send_marks: list[int],
    started: asyncio.Event,
) -> None:
    started.set()
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return
        send_ns = time.monotonic_ns()
        sock.send_text(item)
        send_marks.append(send_ns)
        queue.task_done()


def run_path_a_dry_cycles(
    *,
    n: int,
    venue: str,
    open_mode: str,
    report: WarmLatencyReport,
    warm_ready_ns: int,
) -> None:
    """Path A dry: queue→send on fake warm socket; framework stages skipped."""

    async def _run() -> None:
        sock = _FakeWarmTradeSocket()
        queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        send_marks: list[int] = []
        started = asyncio.Event()
        sender = asyncio.create_task(
            _path_a_sender_loop(sock, queue, send_marks, started)
        )
        venues = (
            ["bybit", "okx"]
            if venue == "dual"
            else [venue]
        )
        for i in range(n):
            for v in venues:
                trace = StageTrace(
                    cycle_id=i + 1,
                    path="A",
                    venue=v,
                    open_mode=open_mode if venue == "dual" else "single",
                    send_enabled=False,
                )
                trace.mark("warm_ready", mono_ns=warm_ready_ns)
                for skip in PATH_A_SKIPPED_STAGES:
                    trace.mark_skipped(skip)
                payload = json.dumps(
                    {
                        "op": "order.create",
                        "path": "A",
                        "venue": v,
                        "cycle": i + 1,
                        "symbol": "TRUMPUSDT" if v == "bybit" else "TRUMP-USDT-SWAP",
                    },
                    separators=(",", ":"),
                )
                before_marks = len(send_marks)
                intent_ns, _approx_send = await _path_a_queue_send_cycle(
                    sock=sock,
                    payload=payload,
                    queue=queue,
                    sender_started=started,
                )
                trace.mark("intent", mono_ns=intent_ns)
                # Wait for sender mark.
                for _ in range(10_000):
                    if len(send_marks) > before_marks:
                        break
                    await asyncio.sleep(0.00005)
                send_ns = send_marks[before_marks] if len(send_marks) > before_marks else time.monotonic_ns()
                trace.mark("request_sent", mono_ns=send_ns)
                # Simulated immediate local ack/terminal for dry shape (not venue RTT).
                ack_ns = time.monotonic_ns()
                trace.mark("ack", mono_ns=ack_ns)
                trace.mark("terminal", mono_ns=ack_ns)
                trace.notes["would_send"] = True
                trace.notes["payload_bytes"] = len(payload.encode("utf-8"))
                report.add_cycle(trace)
        await queue.put(None)
        await sender

    asyncio.run(_run())


def run_path_b_dry_cycles(
    *,
    n: int,
    venue: str,
    open_mode: str,
    report: WarmLatencyReport,
    warm_ready_ns: int,
    data_root: Path,
) -> None:
    """Path B dry: real approval/lease/profile/prepare; stop at would-send."""
    meta = _trump_meta()
    pos = _position()
    venues = ["bybit", "okx"] if venue == "dual" else [venue]
    env = _dry_live_env(data_root)

    for i in range(n):
        # Fresh journal per cycle keeps lease reconstruction cheap & isolated.
        run_id = new_opaque_id("warmlat")
        journal = PrivateJournalWriter(data_root / f"cycle_{i+1}", run_id=run_id)
        vault = ApprovalVault(journal=journal, venue="bybit", environment="live")
        lease_sup = LeaseSupervisor(journal=journal, data_root=journal.data_root)

        for v in venues:
            # Refresh mark asof so preflight never sees stale marks across cycles.
            meta = _trump_meta()
            trace = StageTrace(
                cycle_id=i + 1,
                path="B",
                venue=v,
                open_mode=open_mode if venue == "dual" else "single",
                send_enabled=False,
            )
            trace.mark("warm_ready", mono_ns=warm_ready_ns)
            trace.mark("intent")

            plan = build_order_plan(
                venue="bybit_live" if v == "bybit" else "okx_live",
                symbol="TRUMPUSDT" if v == "bybit" else "TRUMP-USDT-SWAP",
                side="buy" if v == "bybit" else "sell",
                mode="market",
                metadata_provider=meta,
                qty="4.0" if v == "bybit" else "40",
            )

            token = vault.issue(plan)
            trace.mark("approval")

            lease_sup.assert_can_send(now_mono_ns=time.monotonic_ns())
            trace.mark("lease")

            assert_preflight_ready(
                metadata_provider=meta,
                position_mode_provider=pos,
                venue=plan.venue,
                symbol=plan.symbol,
                now_mono_ns=time.monotonic_ns(),
            )
            trace.mark("profile")

            from app.bot.private.order_sender import ApprovalBoundSender

            sender = ApprovalBoundSender(
                journal=journal,
                approval_vault=vault,
                metadata_provider=meta,
                position_mode_provider=pos,
                transport=None,
                lease_supervisor=lease_sup,
            )
            res = sender.send_approved(
                plan,
                token,
                credentials=_dummy_creds(v),
                env=env,
                dispatch_transport=False,
            )
            trace.mark("order_prepared")
            # Dry stop: would-send — no request_sent/ack/terminal.
            trace.mark_skipped("request_sent")
            trace.mark_skipped("ack")
            trace.mark_skipped("terminal")
            trace.notes["would_send"] = True
            trace.notes["send_result_status"] = res.status
            trace.notes["order_attempt_id"] = plan.order_attempt_id
            report.add_cycle(trace)


def _dummy_creds(venue: str):
    from app.bot.private.order_sign import LiveCredentials

    if venue == "okx":
        return LiveCredentials(
            api_key="dry-okx-key",
            api_secret="dry-okx-secret",
            passphrase="dry-pass",
        )
    return LiveCredentials(api_key="dry-bybit-key", api_secret="dry-bybit-secret")


def _derive_path_b_live_traces_from_journal(
    *,
    journal: PrivateJournalWriter,
    n: int,
    open_mode: str,
    warm_ready_ns: int,
    report: WarmLatencyReport,
) -> None:
    """Build Path B traces from journal events after a live W6/W7 run."""
    from app.bot.private.journal_v1 import scan_all_journal_events

    events = [
        e
        for e in scan_all_journal_events(journal.data_root)
        if str(e.get("run_id") or "") == str(journal.run_id)
    ]
    reduce_ops: set[str] = set()
    for ev in events:
        if ev.get("event_type") == "order_prepared" and ev.get("reduce_only"):
            op = str(ev.get("operation_id") or "")
            if op:
                reduce_ops.add(op)

    by_op: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for ev in events:
        op = str(ev.get("operation_id") or "")
        if not op or op in reduce_ops:
            continue
        et = str(ev.get("event_type") or "")
        slot = by_op.setdefault(op, {"venue": str(ev.get("venue") or "bybit")})
        if op not in order and et == "order_prepared":
            order.append(op)
        if et == "order_prepared" and isinstance(ev.get("event_monotonic_ns"), int):
            slot["order_prepared"] = int(ev["event_monotonic_ns"])
        if et == "request_sent" and isinstance(ev.get("send_monotonic_ns"), int):
            slot["request_sent"] = int(ev["send_monotonic_ns"])
        if et == "ack_received" and isinstance(ev.get("receive_monotonic_ns"), int):
            slot["ack"] = int(ev["receive_monotonic_ns"])
        if et == "terminal_update" and isinstance(ev.get("receive_monotonic_ns"), int):
            slot["terminal"] = int(ev["receive_monotonic_ns"])

    # Dual-leg: 2 open ops per cycle. Cap at n cycles.
    max_ops = n * 2
    for idx, op in enumerate(order[:max_ops]):
        slot = by_op[op]
        prep = slot.get("order_prepared")
        if prep is None:
            continue
        trace = StageTrace(
            cycle_id=(idx // 2) + 1,
            path="B",
            venue=str(slot.get("venue") or "bybit"),
            open_mode=open_mode,
            send_enabled=True,
        )
        trace.mark("warm_ready", mono_ns=warm_ready_ns)
        # W6 does not expose discrete approval/lease/profile stamps; collapse.
        trace.mark("intent", mono_ns=int(prep))
        trace.mark_skipped("approval")
        trace.mark_skipped("lease")
        trace.mark_skipped("profile")
        trace.mark("order_prepared", mono_ns=int(prep))
        trace.notes["framework_stages"] = "collapsed_into_intent_eq_order_prepared"
        if "request_sent" in slot:
            trace.mark("request_sent", mono_ns=int(slot["request_sent"]))
        if "ack" in slot:
            trace.mark("ack", mono_ns=int(slot["ack"]))
        if "terminal" in slot:
            trace.mark("terminal", mono_ns=int(slot["terminal"]))
        report.add_cycle(trace)


def run_warm_latency_experiment(
    *,
    cli: WarmLatCli,
    env: Optional[Mapping[str, str]] = None,
    data_root: Optional[Path] = None,
    warm_ready: bool = True,
    warm_ready_ns: Optional[int] = None,
) -> WarmLatencyReport:
    """Run dry A/B experiment (default). Live send is handled by CLI main."""
    e = dict(env if env is not None else os.environ)
    report = WarmLatencyReport(
        status="ok",
        n_requested=cli.n,
        path=cli.path,
        open_mode=cli.open_mode,
        send_enabled=cli.send_enabled,
        dry_run=not cli.send_enabled,
        warm_ready=bool(warm_ready),
        trade_lat_model_ms=TRADE_LAT_MODEL_MS,
        notes={
            "stage_labels": list(STAGE_LABELS),
            "else_bybit_ws_on_branch": False,
            "success_metric": (
                f"compare warm_ready→terminal / request_sent→ack vs "
                f"Trade_Lat={TRADE_LAT_MODEL_MS:.0f}ms model assumption"
            ),
        },
    )
    if not warm_ready:
        report.status = "warm_not_ready"
        report.error_code = "not_ready"
        return report

    ready_ns = int(warm_ready_ns if warm_ready_ns is not None else time.monotonic_ns())
    root = data_root if data_root is not None else Path(
        tempfile.mkdtemp(prefix="warmlat-")
    )

    if cli.send_enabled:
        report.status = "live_requires_cli_main"
        report.error_code = "invalid_request"
        report.notes["hint"] = (
            "Use main_ws_warm_latency with --warm-lat-send=true and "
            "--warm-lat-approve-one-shot under live gates; do not call this "
            "helper for live send."
        )
        return report

    paths = ["A", "B"] if cli.path == "AB" else [cli.path]
    if "A" in paths:
        run_path_a_dry_cycles(
            n=cli.n,
            venue=cli.venue,
            open_mode=cli.open_mode,
            report=report,
            warm_ready_ns=ready_ns,
        )
    if "B" in paths:
        run_path_b_dry_cycles(
            n=cli.n,
            venue=cli.venue,
            open_mode=cli.open_mode,
            report=report,
            warm_ready_ns=ready_ns,
            data_root=root / "path_b",
        )
    return report


def _write_outputs(report: WarmLatencyReport, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j = write_results_json(report, out_dir / "warm_lat_results.json")
    c = write_results_csv(report, out_dir / "warm_lat_cycles.csv")
    s = write_summary_csv(report, out_dir / "warm_lat_summary.csv")
    return {
        "json": str(j),
        "cycles_csv": str(c),
        "summary_csv": str(s),
    }


def main_ws_warm_latency(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """CLI entry for ``--ws-warm-latency``."""
    argv = list(argv or [])
    e = dict(env if env is not None else os.environ)

    def _print(report: WarmLatencyReport) -> None:
        print(json.dumps(report.as_public_dict(), ensure_ascii=False, indent=2, sort_keys=True))

    if "--warm-lat-print-vps-recipe" in argv or "--warm-lat-help-vps" in argv:
        print(print_vps_live_recipe())
        return 0

    try:
        cli = parse_warm_lat_cli_args(argv)
    except WarmLatProfileError as exc:
        rep = WarmLatencyReport(
            status="rejected_before_socket",
            n_requested=0,
            error_code="invalid_request",
            notes={"error": str(exc)},
        )
        _print(rep)
        return 1

    # Dry path: no live gates / no sockets. Safe for CI and local.
    if not cli.send_enabled:
        root = cli.out_dir or resolve_data_root(e) / "warm_lat"
        # Optional: if attach_warm requested without send, still allow dry.
        report = run_warm_latency_experiment(cli=cli, env=e, data_root=root / "work")
        paths = _write_outputs(report, root)
        report.notes["output_paths"] = paths
        report.notes["live_profile"] = dict(WARM_LAT_LIVE_PROFILE)
        _print(report)
        return 0 if report.status == "ok" else 2

    # Live send path — same explicit pattern as W6/W7.
    try:
        assert_ws_warm_lat_gates(e)
    except WsProfileGateError:
        rep = WarmLatencyReport(
            status="rejected_before_socket",
            n_requested=cli.n,
            send_enabled=True,
            dry_run=False,
            error_code="invalid_request",
        )
        _print(rep)
        return 1

    if not cli.approve_one_shot:
        rep = WarmLatencyReport(
            status="approval_required",
            n_requested=cli.n,
            send_enabled=True,
            dry_run=False,
            error_code="invalid_request",
            notes={"hint": "pass --warm-lat-approve-one-shot"},
        )
        _print(rep)
        return 1

    assert_default_entrypoint_cannot_transport()

    # Live: warm once, then Path B via W6/W7; Path A live is refused (measure-only
    # stubs — do not bypass venue protocol on a live trade socket).
    if cli.path == "A":
        rep = WarmLatencyReport(
            status="path_a_live_unsupported",
            n_requested=cli.n,
            send_enabled=True,
            dry_run=False,
            error_code="invalid_request",
            notes={
                "hint": (
                    "Path A is dry/stub-only (queue→send shape). "
                    "Use --warm-lat-path=B or AB with --warm-lat-send=false, "
                    "or live Path B under W6/W7 gates."
                )
            },
        )
        _print(rep)
        return 1

    from app.bot.private.ws_warm_session import (
        WarmSocketBundle,
        clear_process_warm_session,
        start_warm_private_session,
    )
    from app.bot.private.ws_w6_dual_leg import (
        open_w6_production_bindings,
        resolve_w6_leg,
        run_w6_dual_leg,
    )
    from app.bot.private.ws_socket import assert_no_default_ws_socket, unbind_socket_factory
    from app.bot.private.venue import endpoints_for_venue

    report = WarmLatencyReport(
        status="ok",
        n_requested=cli.n,
        path=cli.path,
        open_mode=cli.open_mode,
        send_enabled=True,
        dry_run=False,
        trade_lat_model_ms=TRADE_LAT_MODEL_MS,
        notes={
            "live_profile": dict(WARM_LAT_LIVE_PROFILE),
            "safety": {
                "expect_flat_after": True,
                "live_n_max": WARM_LAT_LIVE_N_MAX,
                "recommend_first_n": 1,
                "half_leg": (
                    "On abort/leftover W6 flattens both venues then stops n; "
                    "if flat_after!=true STOP and verify flat before any retry"
                ),
                "results_dir_recommended": VPS_RESULTS_DIR,
                "data_root_recommended": VPS_DATA_ROOT,
                "never_write": [
                    "/data/live",
                    "/data/bars",
                    "/data/compacted",
                    "/data/spool",
                ],
            },
        },
    )

    try:
        assert_no_default_ws_socket()
    except RuntimeError:
        report.status = "rejected_before_socket"
        report.error_code = "transport_error"
        _print(report)
        return 2

    try:
        bindings = open_w6_production_bindings(env=e)
    except Exception:  # noqa: BLE001
        unbind_socket_factory()
        report.status = "bind_failed"
        report.error_code = "transport_error"
        _print(report)
        return 2

    bybit_p = resolve_w6_leg("bybit")
    okx_p = resolve_w6_leg("okx")
    initial = WarmSocketBundle(
        bybit_private=bindings.bybit_private_socket,
        bybit_trade=bindings.bybit_trade_socket,
        okx_private=bindings.okx_private_socket,
        okx_trade=bindings.okx_trade_socket,
    )
    held: dict[str, Optional[WarmSocketBundle]] = {"bundle": initial}

    def _provider() -> WarmSocketBundle:
        if held["bundle"] is not None:
            out = held["bundle"]
            held["bundle"] = None
            return out
        from app.bot.private.ws_private import trade_ws_url_for_exchange
        from app.bot.private.ws_socket import open_private_socket

        ep = endpoints_for_venue("live")
        return WarmSocketBundle(
            bybit_private=open_private_socket(ep.bybit_private_ws),
            bybit_trade=open_private_socket(trade_ws_url_for_exchange("bybit", ep)),
            okx_private=open_private_socket(ep.okx_private_ws),
            okx_trade=open_private_socket(trade_ws_url_for_exchange("okx", ep)),
        )

    try:
        warm = start_warm_private_session(
            env=e,
            bybit_credentials=bindings.bybit_credentials,
            okx_credentials=bindings.okx_credentials,
            socket_provider=_provider,
            bybit_symbol=bybit_p["symbol"],
            okx_symbol=okx_p["symbol"],
            profile_gate=assert_ws_warm_lat_gates,
            attach=True,
            keepalive=True,
        )
    except Exception:  # noqa: BLE001
        clear_process_warm_session(stop=True)
        unbind_socket_factory()
        report.status = "handshake_failed"
        report.error_code = "auth_failed"
        _print(report)
        return 2

    if not warm.is_ready():
        clear_process_warm_session(stop=True)
        unbind_socket_factory()
        report.status = "warm_not_ready"
        report.error_code = "not_ready"
        _print(report)
        return 2

    report.warm_ready = True
    warm_ready_ns = time.monotonic_ns()

    # Optional dry Path A baseline in the same process (no live send for A).
    if cli.path == "AB":
        run_path_a_dry_cycles(
            n=cli.n,
            venue=cli.venue,
            open_mode=cli.open_mode,
            report=report,
            warm_ready_ns=warm_ready_ns,
        )

    runner_kwargs: dict[str, Any] = {
        "n": cli.n,
        "env": e,
        "metadata_provider": bindings.metadata_provider,
        "position_mode_provider": bindings.position_mode_provider,
        "baseline": bindings.baseline,
        "bybit_credentials": bindings.bybit_credentials,
        "okx_credentials": bindings.okx_credentials,
        "load_secrets": False,
        "issue_approval": True,
        "warm_session": warm,
        "rest_order_recon": bindings.rest_order_recon,
        "send_gate": assert_ws_warm_lat_gates,
        "parallel_open": cli.open_mode == "parallel",
    }
    try:
        w6_report = run_w6_dual_leg(**runner_kwargs)
        report.notes["w6_status"] = w6_report.status
        report.notes["w6_orders_sent"] = w6_report.orders_sent
        report.notes["w6_flat_after"] = w6_report.flat_after
        report.notes["w6_n_completed"] = w6_report.n_completed
        report.notes["w6_n_aborted"] = w6_report.n_aborted
        report.notes["safety_ok"] = bool(
            w6_report.status == "ok" and w6_report.flat_after
        )
        if w6_report.status != "ok" or not w6_report.flat_after:
            report.status = "live_path_b_failed"
            report.error_code = w6_report.error_code or "transport_error"
            report.notes["abort_action"] = (
                "STOP: do not raise --warm-lat-n. Verify both venues flat "
                "(exchange UI / W6 baseline). Resolve leftover before any retry."
            )
        else:
            _derive_path_b_live_traces_from_journal(
                journal=warm.journal,
                n=cli.n,
                open_mode=cli.open_mode,
                warm_ready_ns=warm_ready_ns,
                report=report,
            )
    finally:
        clear_process_warm_session(stop=True)
        unbind_socket_factory()

    if cli.out_dir is not None:
        out = cli.out_dir
    else:
        try:
            out = resolve_data_root(e) / "warm_lat"
        except RuntimeError:
            out = Path(VPS_RESULTS_DIR)
    paths = _write_outputs(report, out)
    report.notes["output_paths"] = paths
    _print(report)
    return 0 if report.status == "ok" and report.notes.get("safety_ok", True) else 2

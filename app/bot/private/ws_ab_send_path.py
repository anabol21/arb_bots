"""A/B send-path experiment: W6/manager (A) vs primitive queue→send (B).

CLI: ``python -m app.bot.private --ab-send-path --ab-contour=A|B --ab-n=5``

Dry default (``--ab-send=false``): no sockets, no secrets, no venue I/O.
Live: ``VENUE=live`` + ``LIVE_ORDERS=1`` + ``BBOT_PRIVATE_AB_SEND=1`` +
``--ab-approve-one-shot``. Agents must not run live.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.bot.private.ab_send_path_stages import (
    AbSendPathReport,
    StageTrace,
    apply_journal_monotonic,
    assert_contour,
    write_results_csv,
    write_results_json,
    write_summary_csv,
)
from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id, scan_all_journal_events
from app.bot.private.order_approval import ApprovalVault
from app.bot.private.order_metadata import InstrumentMetadata, StaticMetadataProvider
from app.bot.private.order_plan import build_order_plan
from app.bot.private.order_preflight import StaticVerifiedPositionModeProvider
from app.bot.private.order_sender import ApprovalBoundSender, assert_default_entrypoint_cannot_transport
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.paths import resolve_data_root
from app.bot.private.secrets import LIVE_KEY_NAMES
from app.bot.private.ws_ab_primitive_send import (
    PrimitiveDualSender,
    build_w6_dual_payloads,
)
from app.bot.private.ws_gates import WsProfileGateError, assert_ws_ab_send_path_gates
from app.bot.private.ws_w6_dual_leg import resolve_w6_leg

AB_N_MIN = 1
AB_N_MAX = 20
AB_LIVE_N_MAX = 5
AB_PROTOCOL_HOLD_SEC = 5.0
TRADE_LAT_MODEL_MS = 100.0

# Match VPS-local live_broker.default_live_send_pair intent (not in git):
# parallel open + parallel flatten on the same warm session. Default W6 CLI
# stays sequential; only this experiment passes these kwargs.
CONTOUR_A_LIVE_W6_KWARGS: dict[str, Any] = {
    "parallel_open": True,
    "parallel_flatten": True,
}

VPS_HOST = "root@38.180.94.108"
VPS_STAGING = "/root/spread_staging"
VPS_RESULTS_DIR = "/data/bbot-gear2/private/ab_send_path"
VPS_DATA_ROOT = "/data/bbot-gear2/private"
VPS_SECRETS = "/etc/spread/bbot-private-live.env"

VPS_SURGICAL_FILES: tuple[str, ...] = (
    "app/bot/private/ab_send_path_stages.py",
    "app/bot/private/ws_ab_primitive_send.py",
    "app/bot/private/ws_ab_send_path.py",
    "app/bot/private/ws_gates.py",
    "app/bot/private/harness_readonly.py",
    "app/bot/private/ws_w6_dual_leg.py",
    "docs/b-private-ab-send-path-experiment.md",
)


class AbSendPathError(ValueError):
    """Invalid A/B send-path CLI / profile."""


def print_vps_live_recipe() -> str:
    """Exact human recipe for VPS live A/B (no network from this helper)."""
    files = " ".join(VPS_SURGICAL_FILES)
    return f"""# A/B send-path live VPS recipe (human only; agents must not SSH/run live)
# Read docs/b-private-ab-send-path-experiment.md FIRST.
# Host: {VPS_HOST}  Staging: {VPS_STAGING}
# Profile: W6 TRUMP dual-leg (~$6–8/leg, ≪ $100/venue). Not SOL/XRP. Not gear2 strategy.
# Place: both contours parallel (A: parallel_open+parallel_flatten like live_broker.default_live_send_pair).

# --- 0) Preflight (read-only) ---
# ssh {VPS_HOST} 'systemctl is-active spread-collector; systemctl show -p MainPID --value spread-collector'
# Do NOT restart collector. Do NOT touch /data/live /data/bars /data/compacted.

# --- 1) Surgical deploy (copy these files only; preserve VPS flatten/aplace/sample-cap) ---
# for f in {files}; do
#   scp "$f" {VPS_HOST}:{VPS_STAGING}/$f
# done
# NEVER: git reset --hard / full-repo rsync over staging.

# --- 2) Safety first: n=1, Contour A ---
# ssh {VPS_HOST}
set -a
source {VPS_SECRETS}   # mode 600; not git
set +a
export VENUE=live
export LIVE_ORDERS=1
export BBOT_PRIVATE_AB_SEND=1
export BBOT_PRIVATE_W6=1
export BBOT_PRIVATE_DATA_ROOT={VPS_DATA_ROOT}
mkdir -p {VPS_RESULTS_DIR}

cd {VPS_STAGING}
# n=1 safety shot (Contour A = full W6 manager)
/root/venv/bin/python -m app.bot.private --ab-send-path \\
  --ab-contour=A --ab-n=1 --ab-send=true --ab-hold-sec=5 \\
  --ab-approve-one-shot --ab-out={VPS_RESULTS_DIR}/A_n1

# If status=ok and notes.flat_after=true, optionally n=5 (fresh session per trial):
# /root/venv/bin/python -m app.bot.private --ab-send-path \\
#   --ab-contour=A --ab-n=5 --ab-send=true --ab-hold-sec=5 \\
#   --ab-approve-one-shot --ab-out={VPS_RESULTS_DIR}/A_n5

# Contour B (primitive queue→send) — only after A n=1 is flat:
# /root/venv/bin/python -m app.bot.private --ab-send-path \\
#   --ab-contour=B --ab-n=1 --ab-send=true --ab-hold-sec=5 \\
#   --ab-approve-one-shot --ab-out={VPS_RESULTS_DIR}/B_n1

# True process restart per trial (optional; harness already reconnects per trial):
# for i in 1 2 3 4 5; do
#   /root/venv/bin/python -m app.bot.private --ab-send-path \\
#     --ab-contour=A --ab-n=1 --ab-send=true --ab-hold-sec=5 \\
#     --ab-approve-one-shot --ab-out={VPS_RESULTS_DIR}/A_proc_$i
# done

# --- 3) Compare p50/p95 ---
# python3 -c "import json; from pathlib import Path
# for p in Path('{VPS_RESULTS_DIR}').rglob('ab_send_path_results.json'):
#   d=json.loads(p.read_text()); print(p, d['status'], d['summary'])"

# Expect notes.flat_after=true before trusting latency. STOP if leftover.
"""


@dataclass(frozen=True)
class AbSendPathCli:
    n: int
    contour: str  # A | B
    send_enabled: bool
    approve_one_shot: bool
    hold_sec: float
    out_dir: Optional[Path]
    print_recipe: bool


def assert_ab_n(n: int, *, send_enabled: bool = False) -> int:
    if not isinstance(n, int) or n < AB_N_MIN or n > AB_N_MAX:
        raise AbSendPathError(f"AB send-path requires --ab-n={AB_N_MIN}..{AB_N_MAX}")
    if send_enabled and n > AB_LIVE_N_MAX:
        raise AbSendPathError(
            f"AB send-path live requires --ab-n={AB_N_MIN}..{AB_LIVE_N_MAX} "
            "(start with 1; protocol N=5)"
        )
    return n


def parse_ab_send_path_cli_args(argv: Sequence[str]) -> AbSendPathCli:
    n: Optional[int] = None
    contour = "A"
    send_enabled = False
    approve = False
    hold_raw: Optional[float] = None
    out_dir: Optional[Path] = None
    print_recipe = False
    for arg in argv:
        if arg.startswith("--ab-n="):
            raw = arg.split("=", 1)[1].strip()
            try:
                n = int(raw)
            except ValueError:
                n = -1
        elif arg.startswith("--ab-contour="):
            contour = arg.split("=", 1)[1].strip()
        elif arg.startswith("--ab-send="):
            send_enabled = arg.split("=", 1)[1].strip().lower() in {"1", "true", "yes", "on"}
        elif arg == "--ab-approve-one-shot":
            approve = True
        elif arg.startswith("--ab-hold-sec="):
            raw = arg.split("=", 1)[1].strip()
            try:
                hold_raw = float(raw)
            except ValueError:
                hold_raw = -1.0
        elif arg.startswith("--ab-out="):
            out_dir = Path(arg.split("=", 1)[1].strip())
        elif arg == "--ab-print-vps-recipe":
            print_recipe = True
    if n is None:
        n = 5 if not print_recipe else 1
    if hold_raw is None:
        hold_sec = AB_PROTOCOL_HOLD_SEC if send_enabled else 0.0
    else:
        hold_sec = hold_raw
    if hold_sec < 0:
        raise AbSendPathError("--ab-hold-sec must be >= 0")
    return AbSendPathCli(
        n=assert_ab_n(n, send_enabled=send_enabled),
        contour=assert_contour(contour),
        send_enabled=send_enabled,
        approve_one_shot=approve,
        hold_sec=hold_sec,
        out_dir=out_dir,
        print_recipe=print_recipe,
    )


def trump_metadata_provider(*, mark_max_age_ns: int = 60_000_000_000) -> StaticMetadataProvider:
    now = time.monotonic_ns()
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
                mark_asof_monotonic_ns=now,
                mark_max_age_ns=mark_max_age_ns,
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
                mark_asof_monotonic_ns=now,
                mark_max_age_ns=mark_max_age_ns,
                inst_id_code=193761,
            ),
        }
    )


def _dry_live_env(data_root: Path) -> dict[str, str]:
    live_env = data_root / "bbot-private-live.env"
    if not live_env.exists():
        live_env.write_text(
            "\n".join(f"{n}=dry{i}" for i, n in enumerate(LIVE_KEY_NAMES)) + "\n",
            encoding="utf-8",
        )
    return {
        "VENUE": "live",
        "LIVE_ORDERS": "1",
        "BBOT_PRIVATE_W6": "1",
        "BBOT_PRIVATE_AB_SEND": "1",
        "BBOT_PRIVATE_ENV_FILE": str(live_env),
        "BBOT_PRIVATE_DATA_ROOT": str(data_root),
    }


def _dummy_creds() -> LiveCredentials:
    return LiveCredentials(api_key="k", api_secret="s", passphrase="p")


def _sleep_hold(hold_sec: float) -> None:
    if hold_sec > 0:
        time.sleep(float(hold_sec))


def run_contour_a_dry_trial(
    *,
    trial_id: int,
    data_root: Path,
    hold_sec: float = 0.0,
) -> StageTrace:
    """Contour A dry: real journal/vault/lease/prepare; no ws.send.

    Parallel-intent: both open plans are issued before any prepare; would-send
    stamps fire together after both prepares (no Bybit-fill-then-OKX wait).
    Same for flatten. Live uses ``parallel_open`` / ``parallel_flatten``.
    """
    trace = StageTrace(trial_id=trial_id, contour="A", send_enabled=False)
    env = _dry_live_env(data_root)
    journal = PrivateJournalWriter(data_root, run_id=new_opaque_id("run"))
    meta = trump_metadata_provider()
    pos = StaticVerifiedPositionModeProvider(
        {"bybit_live": "one_way", "okx_live": "one_way"}
    )
    vault = ApprovalVault(journal=journal, venue="bybit", environment="live")
    trace.mark("warm_ready")
    _sleep_hold(hold_sec)
    trace.mark("signal")
    trace.mark("recover")  # ApprovalBoundSender.__init__ reconstructs leases
    sender = ApprovalBoundSender(
        journal=journal,
        approval_vault=vault,
        metadata_provider=meta,
        position_mode_provider=pos,
        transport=None,
        data_root=data_root,
    )
    bybit_p = resolve_w6_leg("bybit")
    okx_p = resolve_w6_leg("okx")
    dual_id = new_opaque_id("dual")
    bybit_open = build_order_plan(
        venue=bybit_p["venue"],
        symbol=bybit_p["symbol"],
        side=bybit_p["open_side"],
        mode="market",
        metadata_provider=meta,
        qty=bybit_p["qty"],
        reduce_only=False,
        dual_leg_id=dual_id,
        expires_in_sec=60,
    )
    okx_open = build_order_plan(
        venue=okx_p["venue"],
        symbol=okx_p["symbol"],
        side=okx_p["open_side"],
        mode="market",
        metadata_provider=meta,
        qty=okx_p["qty"],
        reduce_only=False,
        dual_leg_id=dual_id,
        expires_in_sec=60,
    )
    creds = _dummy_creds()
    trace.mark("operator_approval")
    b_tok = vault.issue(bybit_open)
    o_tok = vault.issue(okx_open)
    trace.mark("lease")
    b_res = sender.send_approved(
        bybit_open,
        b_tok,
        creds,
        env,
        dispatch_transport=False,
        journal_transport="ws_trade",
    )
    trace.mark("order_prepared")
    sender.send_approved(
        okx_open,
        o_tok,
        creds,
        env,
        dispatch_transport=False,
        journal_transport="ws_trade",
    )
    # Would-send together after both prepares (parallel-intent, no fill wait).
    sent_ns = time.monotonic_ns()
    trace.mark("first_request_sent", mono_ns=sent_ns)
    trace.mark("second_request_sent", mono_ns=sent_ns)
    trace.mark("first_ack")
    trace.mark("second_ack")
    trace.mark("terminal_fill")
    _sleep_hold(hold_sec)
    trace.mark("close_signal")
    bybit_flat = build_order_plan(
        venue=bybit_p["venue"],
        symbol=bybit_p["symbol"],
        side=bybit_p["flatten_side"],
        mode="market",
        metadata_provider=meta,
        qty=bybit_p["qty"],
        reduce_only=True,
        dual_leg_id=dual_id,
        expires_in_sec=60,
    )
    okx_flat = build_order_plan(
        venue=okx_p["venue"],
        symbol=okx_p["symbol"],
        side=okx_p["flatten_side"],
        mode="market",
        metadata_provider=meta,
        qty=okx_p["qty"],
        reduce_only=True,
        dual_leg_id=dual_id,
        expires_in_sec=60,
    )
    fb = vault.issue(bybit_flat)
    fo = vault.issue(okx_flat)
    sender.send_approved(
        bybit_flat, fb, creds, env, dispatch_transport=False, journal_transport="ws_trade"
    )
    sender.send_approved(
        okx_flat, fo, creds, env, dispatch_transport=False, journal_transport="ws_trade"
    )
    close_ns = time.monotonic_ns()
    trace.mark("close_first_request_sent", mono_ns=close_ns)
    trace.mark("close_second_request_sent", mono_ns=close_ns)
    trace.mark("terminal_flat")
    trace.notes["send_result_status"] = b_res.status
    trace.notes["dry_dispatch"] = False
    trace.notes["parallel_open"] = True
    trace.notes["parallel_flatten"] = True
    return trace


def run_contour_b_dry_trial(
    *,
    trial_id: int,
    hold_sec: float = 0.0,
    sender: Optional[PrimitiveDualSender] = None,
) -> StageTrace:
    """Contour B dry: queue.put both legs → long-lived sender (fake ws.send)."""
    trace = StageTrace(trial_id=trial_id, contour="B", send_enabled=False)
    owned = sender is None
    loop = sender if sender is not None else PrimitiveDualSender()
    try:
        trace.mark("warm_ready")
        _sleep_hold(hold_sec)
        trace.mark("signal")
        trace.apply_contour_skips()
        b_pay, o_pay, b_req, o_req = build_w6_dual_payloads(phase="open")
        opened = loop.enqueue_dual(
            bybit_payload=b_pay,
            okx_payload=o_pay,
            bybit_req_id=b_req,
            okx_req_id=o_req,
            phase="open",
        )
        if opened.first_sent_ns is not None:
            trace.mark("first_request_sent", mono_ns=opened.first_sent_ns)
        else:
            trace.mark("first_request_sent")
        if opened.second_sent_ns is not None:
            trace.mark("second_request_sent", mono_ns=opened.second_sent_ns)
        else:
            trace.mark("second_request_sent")
        trace.mark("first_ack")
        trace.mark("second_ack")
        trace.mark("terminal_fill")
        _sleep_hold(hold_sec)
        trace.mark("close_signal")
        cb, co, crb, cro = build_w6_dual_payloads(phase="close")
        closed = loop.enqueue_dual(
            bybit_payload=cb,
            okx_payload=co,
            bybit_req_id=crb,
            okx_req_id=cro,
            phase="close",
        )
        if closed.first_sent_ns is not None:
            trace.mark("close_first_request_sent", mono_ns=closed.first_sent_ns)
        else:
            trace.mark("close_first_request_sent")
        if closed.second_sent_ns is not None:
            trace.mark("close_second_request_sent", mono_ns=closed.second_sent_ns)
        else:
            trace.mark("close_second_request_sent")
        trace.mark("terminal_flat")
        trace.notes["primitive_open_error"] = opened.error
        trace.notes["historic_shape"] = "queue.put both → sender ws.send"
        return trace
    finally:
        if owned:
            loop.close()


def run_ab_send_path_dry(
    *,
    cli: AbSendPathCli,
    data_root: Path,
) -> AbSendPathReport:
    report = AbSendPathReport(
        status="ok",
        n_requested=cli.n,
        contour=cli.contour,
        send_enabled=False,
        dry_run=True,
        warm_ready=True,
        hold_sec=cli.hold_sec,
    )
    report.notes["protocol"] = (
        "per trial: warm_ready → hold → signal → open → hold → close → shutdown"
    )
    report.notes["trade_lat_model_ms"] = TRADE_LAT_MODEL_MS
    sender: Optional[PrimitiveDualSender] = None
    if cli.contour == "B":
        sender = PrimitiveDualSender()
    try:
        for i in range(1, cli.n + 1):
            if cli.contour == "A":
                trial_root = data_root / f"trial_{i}"
                trial_root.mkdir(parents=True, exist_ok=True)
                report.add_trial(
                    run_contour_a_dry_trial(
                        trial_id=i, data_root=trial_root, hold_sec=cli.hold_sec
                    )
                )
            else:
                report.add_trial(
                    run_contour_b_dry_trial(
                        trial_id=i, hold_sec=cli.hold_sec, sender=sender
                    )
                )
    finally:
        if sender is not None:
            sender.close()
    return report


def _run_contour_a_live(
    *,
    cli: AbSendPathCli,
    env: Mapping[str, str],
    report: AbSendPathReport,
) -> AbSendPathReport:
    """Live Contour A: warm session + W6 full manager, hold between open/close."""
    from app.bot.private.ws_w6_dual_leg import (
        W6RuntimeBindings,
        open_w6_production_bindings,
        run_w6_dual_leg,
    )
    from app.bot.private.ws_warm_session import (
        WarmSocketBundle,
        clear_process_warm_session,
        start_warm_private_session,
    )
    from app.bot.private.ws_gates import assert_ws_w6_send_gates
    from app.bot.private.ws_socket import unbind_socket_factory
    from app.bot.private.ws_w4_baseline import BaselineError
    from app.bot.private.order_preflight import PreflightError

    e = dict(env)
    try:
        assert_ws_w6_send_gates(e)
    except WsProfileGateError:
        report.status = "rejected_before_socket"
        report.error_code = "invalid_request"
        report.notes["hint"] = "Contour A live also needs BBOT_PRIVATE_W6=1"
        return report

    for i in range(1, cli.n + 1):
        active: Optional[W6RuntimeBindings] = None
        try:
            active = open_w6_production_bindings(env=e)
        except (
            BaselineError,
            PreflightError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
        ):
            unbind_socket_factory()
            report.status = "bind_failed"
            report.error_code = "transport_error"
            return report

        bybit_p = resolve_w6_leg("bybit")
        okx_p = resolve_w6_leg("okx")
        initial = WarmSocketBundle(
            bybit_private=active.bybit_private_socket,
            bybit_trade=active.bybit_trade_socket,
            okx_private=active.okx_private_socket,
            okx_trade=active.okx_trade_socket,
        )
        held: dict[str, Optional[WarmSocketBundle]] = {"bundle": initial}

        def _provider() -> WarmSocketBundle:
            if held["bundle"] is not None:
                out = held["bundle"]
                held["bundle"] = None
                return out
            from app.bot.private.ws_private import trade_ws_url_for_exchange
            from app.bot.private.ws_socket import open_private_socket
            from app.bot.private.venue import endpoints_for_venue

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
                bybit_credentials=active.bybit_credentials,
                okx_credentials=active.okx_credentials,
                socket_provider=_provider,
                bybit_symbol=bybit_p["symbol"],
                okx_symbol=okx_p["symbol"],
                profile_gate=assert_ws_ab_send_path_gates,
                attach=True,
            )
        except (RuntimeError, WsProfileGateError, OSError, ValueError, TypeError):
            report.status = "handshake_failed"
            report.error_code = "auth_failed"
            clear_process_warm_session(stop=True)
            unbind_socket_factory()
            return report

        trace = StageTrace(trial_id=i, contour="A", send_enabled=True)
        trace.mark("warm_ready")
        report.warm_ready = True
        _sleep_hold(cli.hold_sec)
        events_before = scan_all_journal_events(warm.journal.data_root)
        before_ids = {id(ev) for ev in events_before}
        trace.mark("signal")
        trace.mark("recover")
        w6 = run_w6_dual_leg(
            n=1,
            env=e,
            metadata_provider=active.metadata_provider,
            position_mode_provider=active.position_mode_provider,
            baseline=active.baseline,
            bybit_credentials=active.bybit_credentials,
            okx_credentials=active.okx_credentials,
            load_secrets=False,
            issue_approval=True,
            rest_order_recon=active.rest_order_recon,
            warm_session=warm,
            send_gate=assert_ws_ab_send_path_gates,
            hold_after_open_sec=cli.hold_sec,
            **CONTOUR_A_LIVE_W6_KWARGS,
        )
        events_after = [
            ev
            for ev in scan_all_journal_events(warm.journal.data_root)
            if id(ev) not in before_ids
        ]
        apply_journal_monotonic(trace, events_after, phase="open")
        # Close stamps: flatten events after the two opens. Split by reduce_only
        # is not always on request_sent; use remaining place events beyond 2.
        open_places = [
            ev
            for ev in events_after
            if ev.get("event_type") == "request_sent" and ev.get("request_kind") == "place"
        ]
        if len(open_places) > 2:
            apply_journal_monotonic(trace, open_places[2:], phase="close")
        if "terminal_flat" not in trace.stamps_ns:
            trace.mark("terminal_flat")
        if "close_signal" not in trace.stamps_ns:
            # Approximate: after open terminals, before flatten sends.
            if "terminal_fill" in trace.stamps_ns:
                trace.mark("close_signal", mono_ns=trace.stamps_ns["terminal_fill"])
            else:
                trace.mark("close_signal")
        trace.notes["w6_status"] = w6.status
        trace.notes["w6_flat_after"] = w6.flat_after
        trace.notes["w6_orders_sent"] = w6.orders_sent
        trace.notes["parallel_open"] = True
        trace.notes["parallel_flatten"] = True
        trace.notes["open_mode"] = w6.open_mode
        report.add_trial(trace)
        report.notes["w6_status"] = w6.status
        report.notes["flat_after"] = w6.flat_after
        report.notes["safety_ok"] = bool(w6.status == "ok" and w6.flat_after)
        clear_process_warm_session(stop=True)
        unbind_socket_factory()
        if w6.status != "ok" or not w6.flat_after:
            report.status = w6.status if w6.status != "ok" else "flatten_incomplete"
            report.error_code = w6.error_code or "unknown"
            return report
    report.status = "ok"
    return report


def _run_contour_b_live(
    *,
    cli: AbSendPathCli,
    env: Mapping[str, str],
    report: AbSendPathReport,
) -> AbSendPathReport:
    """Live Contour B: warm sockets + primitive queue→send (no W6 manager)."""
    from app.bot.private.ws_w6_dual_leg import open_w6_production_bindings
    from app.bot.private.ws_warm_session import (
        WarmSocketBundle,
        clear_process_warm_session,
        start_warm_private_session,
    )
    from app.bot.private.ws_socket import unbind_socket_factory
    from app.bot.private.ws_w4_baseline import BaselineError
    from app.bot.private.order_preflight import PreflightError

    e = dict(env)
    for i in range(1, cli.n + 1):
        try:
            active = open_w6_production_bindings(env=e)
        except (
            BaselineError,
            PreflightError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
        ):
            unbind_socket_factory()
            report.status = "bind_failed"
            report.error_code = "transport_error"
            return report

        bybit_p = resolve_w6_leg("bybit")
        okx_p = resolve_w6_leg("okx")
        initial = WarmSocketBundle(
            bybit_private=active.bybit_private_socket,
            bybit_trade=active.bybit_trade_socket,
            okx_private=active.okx_private_socket,
            okx_trade=active.okx_trade_socket,
        )
        held: dict[str, Optional[WarmSocketBundle]] = {"bundle": initial}

        def _provider() -> WarmSocketBundle:
            if held["bundle"] is not None:
                out = held["bundle"]
                held["bundle"] = None
                return out
            from app.bot.private.ws_private import trade_ws_url_for_exchange
            from app.bot.private.ws_socket import open_private_socket
            from app.bot.private.venue import endpoints_for_venue

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
                bybit_credentials=active.bybit_credentials,
                okx_credentials=active.okx_credentials,
                socket_provider=_provider,
                bybit_symbol=bybit_p["symbol"],
                okx_symbol=okx_p["symbol"],
                profile_gate=assert_ws_ab_send_path_gates,
                attach=True,
            )
        except (RuntimeError, WsProfileGateError, OSError, ValueError, TypeError):
            report.status = "handshake_failed"
            report.error_code = "auth_failed"
            clear_process_warm_session(stop=True)
            unbind_socket_factory()
            return report

        def _send_item(item: Any) -> None:
            sock = (
                warm.bybit_runtime.trade_socket
                if item.venue == "bybit"
                else warm.okx_runtime.trade_socket
            )
            if sock is None:
                raise RuntimeError("trade socket missing")
            text = item.payload if isinstance(item.payload, str) else json.dumps(
                item.payload, separators=(",", ":")
            )
            sock.send_text(text)

        loop = PrimitiveDualSender(send_fn=_send_item)
        trace = StageTrace(trial_id=i, contour="B", send_enabled=True)
        try:
            trace.mark("warm_ready")
            report.warm_ready = True
            _sleep_hold(cli.hold_sec)
            trace.mark("signal")
            trace.apply_contour_skips()
            with warm.place_io_section():
                b_pay, o_pay, b_req, o_req = build_w6_dual_payloads(phase="open")
                opened = loop.enqueue_dual(
                    bybit_payload=b_pay,
                    okx_payload=o_pay,
                    bybit_req_id=b_req,
                    okx_req_id=o_req,
                    phase="open",
                )
            if opened.first_sent_ns:
                trace.mark("first_request_sent", mono_ns=opened.first_sent_ns)
            else:
                trace.mark("first_request_sent")
            if opened.second_sent_ns:
                trace.mark("second_request_sent", mono_ns=opened.second_sent_ns)
            else:
                trace.mark("second_request_sent")
            try:
                warm.bybit_runtime.recv_trade_ack(expect_req_id=b_req, timeout_sec=5.0)
                trace.mark("first_ack")
            except Exception:  # noqa: BLE001
                trace.notes["bybit_ack"] = "timeout_or_mismatch"
                trace.mark("first_ack")
            try:
                warm.okx_runtime.recv_trade_ack(expect_req_id=o_req, timeout_sec=5.0)
                trace.mark("second_ack")
            except Exception:  # noqa: BLE001
                trace.notes["okx_ack"] = "timeout_or_mismatch"
                trace.mark("second_ack")
            trace.mark("terminal_fill")
            _sleep_hold(cli.hold_sec)
            trace.mark("close_signal")
            with warm.place_io_section():
                cb, co, crb, cro = build_w6_dual_payloads(phase="close")
                closed = loop.enqueue_dual(
                    bybit_payload=cb,
                    okx_payload=co,
                    bybit_req_id=crb,
                    okx_req_id=cro,
                    phase="close",
                )
            if closed.first_sent_ns:
                trace.mark("close_first_request_sent", mono_ns=closed.first_sent_ns)
            else:
                trace.mark("close_first_request_sent")
            if closed.second_sent_ns:
                trace.mark("close_second_request_sent", mono_ns=closed.second_sent_ns)
            else:
                trace.mark("close_second_request_sent")
            try:
                after_b = active.baseline.check(
                    exchange="bybit", symbol=bybit_p["symbol"]
                )
                after_o = active.baseline.check(exchange="okx", symbol=okx_p["symbol"])
                flat = bool(after_b.ok and after_o.ok)
            except Exception:  # noqa: BLE001
                flat = False
            trace.mark("terminal_flat")
            trace.notes["flat_after"] = flat
            report.add_trial(trace)
            report.notes["flat_after"] = flat
            report.notes["safety_ok"] = flat
            if not flat:
                report.status = "flatten_incomplete"
                report.error_code = "unknown"
                return report
        finally:
            loop.close()
            clear_process_warm_session(stop=True)
            unbind_socket_factory()
    report.status = "ok"
    return report


def run_ab_send_path_experiment(
    *,
    cli: AbSendPathCli,
    env: Optional[Mapping[str, str]] = None,
    data_root: Optional[Path] = None,
) -> AbSendPathReport:
    """Dry runner. Live send must go through ``main_ab_send_path`` gates."""
    if cli.send_enabled:
        return AbSendPathReport(
            status="live_requires_cli_main",
            n_requested=cli.n,
            contour=cli.contour,
            send_enabled=True,
            dry_run=False,
            error_code="invalid_request",
        )
    root = data_root if data_root is not None else resolve_data_root(env)
    return run_ab_send_path_dry(cli=cli, data_root=root)


def _write_outputs(report: AbSendPathReport, out_dir: Path) -> dict[str, str]:
    j = write_results_json(report, out_dir / "ab_send_path_results.json")
    c = write_results_csv(report, out_dir / "ab_send_path_trials.csv")
    s = write_summary_csv(report, out_dir / "ab_send_path_summary.csv")
    return {
        "json": str(j),
        "trials_csv": str(c),
        "summary_csv": str(s),
    }


def main_ab_send_path(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """CLI entry for ``--ab-send-path``."""
    argv = list(argv or [])
    e = dict(env if env is not None else os.environ)

    def _print(payload: Mapping[str, Any]) -> None:
        print(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True))

    try:
        cli = parse_ab_send_path_cli_args(argv)
    except (AbSendPathError, ValueError) as exc:
        _print(
            {
                "status": "rejected_before_socket",
                "error_code": "invalid_request",
                "experiment": "ab_send_path",
                "detail": str(exc),
            }
        )
        return 1

    if cli.print_recipe:
        print(print_vps_live_recipe())
        return 0

    assert_default_entrypoint_cannot_transport()

    if not cli.send_enabled:
        root = Path(cli.out_dir) if cli.out_dir is not None else Path(
            tempfile.mkdtemp(prefix="ab_send_path_")
        )
        root.mkdir(parents=True, exist_ok=True)
        report = run_ab_send_path_dry(cli=cli, data_root=root)
        report.notes["output_paths"] = _write_outputs(report, root)
        _print(report.as_public_dict())
        return 0 if report.status == "ok" else 2

    try:
        assert_ws_ab_send_path_gates(e)
    except WsProfileGateError:
        _print(
            {
                "status": "rejected_before_socket",
                "error_code": "invalid_request",
                "experiment": "ab_send_path",
                "hint": "live needs VENUE=live LIVE_ORDERS=1 BBOT_PRIVATE_AB_SEND=1",
            }
        )
        return 1

    if not cli.approve_one_shot:
        _print(
            {
                "status": "approval_required",
                "error_code": "invalid_request",
                "experiment": "ab_send_path",
                "n_requested": cli.n,
            }
        )
        return 1

    out = cli.out_dir or (resolve_data_root(e) / "ab_send_path")
    out.mkdir(parents=True, exist_ok=True)
    report = AbSendPathReport(
        status="incomplete",
        n_requested=cli.n,
        contour=cli.contour,
        send_enabled=True,
        dry_run=False,
        hold_sec=cli.hold_sec,
    )
    report.notes["trade_lat_model_ms"] = TRADE_LAT_MODEL_MS
    if cli.contour == "A":
        report = _run_contour_a_live(cli=cli, env=e, report=report)
    else:
        report = _run_contour_b_live(cli=cli, env=e, report=report)
    report.notes["output_paths"] = _write_outputs(report, out)
    _print(report.as_public_dict())
    if report.status == "ok" and report.notes.get("safety_ok", True):
        return 0
    if report.status in {"rejected_before_socket", "approval_required"}:
        return 1
    return 2

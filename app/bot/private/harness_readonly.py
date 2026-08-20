"""Read-only auth harness: testnet/demo route or live read-only route.

Never sends/cancels orders. Never loads live env when VENUE=testnet.
VENUE=live requires LIVE_ORDERS=0; LIVE_ORDERS!=0 rejects before secrets/network.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union

from app.bot.private.paths import (
    auth_probe_jsonl_path,
    resolve_data_root,
    resolve_log_path,
)
from app.bot.private.rest_readonly import (
    NORMALIZED_OUTCOMES,
    assert_no_order_methods,
    probe_bybit_wallet,
    probe_okx_balance,
    scrub_public_record,
)
from app.bot.private.journal_v1 import (
    PrivateJournalWriter,
    assert_no_order_surface,
    map_probe_outcome_to_error_code,
)
from app.bot.private.secrets import (
    LiveSecrets,
    TestnetSecrets,
    load_live_secrets,
    load_testnet_secrets,
    resolve_private_profile,
)
from app.bot.private.venue import (
    assert_live_readonly,
    assert_stage1_venue,
    endpoints_for_venue,
    live_orders_enabled,
    resolve_venue,
    send_allowed,
)


LOG = logging.getLogger("bbot.private.harness_readonly")

SecretsT = Union[TestnetSecrets, LiveSecrets]


def _setup_logging(log_path: Path) -> None:
    LOG.handlers.clear()
    LOG.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [bbot-private] %(message)s"
    )
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    LOG.addHandler(sh)
    try:
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError as exc:
        LOG.warning("file log unavailable path_basename=%s err=%s", log_path.name, type(exc).__name__)


def _utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(record), ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def public_config_snapshot(env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """Public config only — no secret paths or env-file overrides."""
    from app.bot.private.order_sender import (
        get_runtime_transport,
        orders_code_present,
        orders_runtime_armed,
    )

    e = dict(env) if env is not None else dict(os.environ)
    venue = resolve_venue(e)
    profile_name = "live" if venue == "live" else "testnet"
    try:
        profile = resolve_private_profile(e)
        profile_name = profile.name
        readonly = profile.readonly
    except Exception:  # noqa: BLE001
        readonly = True
    armed = orders_runtime_armed(e)
    return {
        "VENUE": venue,
        "LIVE_ORDERS": e.get("LIVE_ORDERS", "0"),
        "live_orders_enabled": live_orders_enabled(e),
        "send_allowed": send_allowed(e),
        "credential_profile": profile_name,
        # R3: code may be present while runtime transport remains unbound.
        "orders_code_present": orders_code_present(),
        "orders_runtime_armed": armed,
        "orders_surface": armed,
        "orders_transport_bound": get_runtime_transport() is not None,
        "readonly": readonly and not armed,
        "orders_sent": 0,
        "private_ws": False,
        "market_subscriptions": 0,
        "symbol_subscriptions": [],
    }


# Backward-compatible alias used by older callers/tests.
def config_snapshot(env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    return public_config_snapshot(env)


def _resolve_readonly_route(env: Mapping[str, str]) -> str:
    """Return 'testnet' or 'live'. Reject live+orders before secrets/network."""
    venue = resolve_venue(env)
    if venue == "live":
        # Fail closed: LIVE_ORDERS!=0 never reaches secret load or HTTP.
        assert_live_readonly(env)
        return "live"
    return assert_stage1_venue(env)


def _probe_skipped(exchange: str, venue_label: str, outcome: str = "unknown_error") -> dict[str, Any]:
    if outcome not in NORMALIZED_OUTCOMES:
        outcome = "unknown_error"
    return {
        "exchange": exchange,
        "venue": venue_label,
        "ok": False,
        "outcome": outcome,
        "http_status": None,
        "exchange_code": None,
        "orders_sent": 0,
        "skipped": True,
        "equity_present": False,
    }


def _run_exchange_probes(
    *,
    secrets: SecretsT,
    endpoints: Any,
    venue: str,
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    venue_label_okx = "demo" if endpoints.okx_simulated_trading else venue

    if secrets.bybit_api_key and secrets.bybit_api_secret:
        try:
            bybit = probe_bybit_wallet(
                api_key=secrets.bybit_api_key,
                api_secret=secrets.bybit_api_secret,
                endpoints=endpoints,
            )
            probes.append(bybit.as_public_dict())
            LOG.info(
                "bybit probe ok=%s http=%s code=%s outcome=%s equity_present=%s orders_sent=%s",
                bybit.ok,
                bybit.http_status,
                bybit.exchange_code,
                bybit.outcome,
                bybit.equity_usdt is not None,
                bybit.orders_sent,
            )
        except Exception:  # noqa: BLE001 — never log exception text
            probes.append(
                {
                    "exchange": "bybit",
                    "venue": venue,
                    "endpoint": endpoints.bybit_rest,
                    "ok": False,
                    "outcome": "unknown_error",
                    "http_status": None,
                    "exchange_code": None,
                    "orders_sent": 0,
                    "equity_present": False,
                }
            )
            LOG.error("bybit probe error outcome=unknown_error")
    else:
        probes.append(_probe_skipped("bybit", venue))
        LOG.warning("bybit keys missing; skip network probe")

    if secrets.okx_api_key and secrets.okx_api_secret and secrets.okx_passphrase:
        try:
            okx = probe_okx_balance(
                api_key=secrets.okx_api_key,
                api_secret=secrets.okx_api_secret,
                passphrase=secrets.okx_passphrase,
                endpoints=endpoints,
            )
            probes.append(okx.as_public_dict())
            LOG.info(
                "okx probe ok=%s http=%s code=%s outcome=%s simulated=%s equity_present=%s orders_sent=%s",
                okx.ok,
                okx.http_status,
                okx.exchange_code,
                okx.outcome,
                endpoints.okx_simulated_trading,
                okx.equity_usdt is not None,
                okx.orders_sent,
            )
        except Exception:  # noqa: BLE001
            probes.append(
                {
                    "exchange": "okx",
                    "venue": venue_label_okx,
                    "endpoint": endpoints.okx_rest,
                    "ok": False,
                    "outcome": "unknown_error",
                    "http_status": None,
                    "exchange_code": None,
                    "orders_sent": 0,
                    "equity_present": False,
                    "okx_simulated_trading": endpoints.okx_simulated_trading,
                }
            )
            LOG.error("okx probe error outcome=unknown_error")
    else:
        probes.append(_probe_skipped("okx", venue_label_okx))
        LOG.warning("okx keys missing; skip network probe")

    return probes


def _okx_environment(endpoints: Any, venue: str) -> str:
    return "demo" if endpoints.okx_simulated_trading else venue


def _credential_presence(secrets: SecretsT, exchange: str) -> dict[str, bool]:
    """Contract shape: exactly ``{\"credentials_configured\": bool}``."""
    if exchange == "bybit":
        configured = bool(secrets.bybit_api_key) and bool(secrets.bybit_api_secret)
    else:
        configured = (
            bool(secrets.okx_api_key)
            and bool(secrets.okx_api_secret)
            and bool(secrets.okx_passphrase)
        )
    return {"credentials_configured": configured}


def _credentials_complete(secrets: SecretsT, exchange: str) -> bool:
    return bool(_credential_presence(secrets, exchange)["credentials_configured"])


def _auth_error_for_probe(outcome: str, *, credentials_complete: bool) -> str:
    if not credentials_complete:
        return "auth_unavailable"
    mapped = {
        "auth_rejected": "auth_failed",
        "auth_forbidden": "auth_failed",
        "network_error": "network_error",
        "malformed_response": "invalid_request",
        "unknown_error": "unknown",
    }
    return mapped.get(outcome, "auth_failed")


def _append_v1_auth_and_account_reads(
    *,
    writer: PrivateJournalWriter,
    secrets: SecretsT,
    endpoints: Any,
    venue: str,
    probes: Sequence[Mapping[str, Any]],
) -> None:
    """Append contract ``auth`` + ``account_read`` from signed probe results.

    Credential presence alone is never recorded as auth success.
    """
    okx_env = _okx_environment(endpoints, venue)
    by_exchange = {
        str(p.get("exchange")): p for p in probes if p.get("exchange") in {"bybit", "okx"}
    }

    for exchange in ("bybit", "okx"):
        env_label = okx_env if exchange == "okx" else venue
        presence = _credential_presence(secrets, exchange)
        complete = _credentials_complete(secrets, exchange)
        probe = by_exchange.get(exchange)
        scope = "wallet" if exchange == "bybit" else "balance"

        if probe is None:
            # No probe row — treat as unavailable local auth observation.
            writer.append_auth(
                venue=exchange,
                environment=env_label,
                outcome="failure",
                auth_method="hmac",
                credential_presence=presence,
                error_code="auth_unavailable",
            )
            continue

        if probe.get("skipped") or not complete:
            writer.append_auth(
                venue=exchange,
                environment=env_label,
                outcome="failure",
                auth_method="hmac",
                credential_presence=presence,
                error_code="auth_unavailable",
            )
            continue

        if probe.get("ok"):
            # Signed venue probe succeeded → auth success, then account_read.
            writer.append_auth(
                venue=exchange,
                environment=env_label,
                outcome="success",
                auth_method="hmac",
                credential_presence=presence,
            )
            writer.append_account_read(
                venue=exchange,
                environment=env_label,
                outcome="success",
                account_scope=scope,
            )
        else:
            probe_outcome = str(probe.get("outcome") or "unknown_error")
            writer.append_auth(
                venue=exchange,
                environment=env_label,
                outcome="failure",
                auth_method="hmac",
                credential_presence=presence,
                error_code=_auth_error_for_probe(
                    probe_outcome, credentials_complete=complete
                ),
            )
            writer.append_account_read(
                venue=exchange,
                environment=env_label,
                outcome="failure",
                account_scope=scope,
                error_code=map_probe_outcome_to_error_code(probe_outcome),
            )


def run_readonly_harness(
    env: Optional[Mapping[str, str]] = None,
    *,
    allow_missing_secrets: bool = True,
) -> dict[str, Any]:
    """Execute read-only probes. Returns a public report dict (no secrets)."""
    e = dict(env) if env is not None else dict(os.environ)
    e.setdefault("VENUE", "testnet")
    e.setdefault("LIVE_ORDERS", "0")

    assert_no_order_methods()
    assert_no_order_surface()

    # Route gate before secrets or network.
    try:
        venue = _resolve_readonly_route(e)
    except RuntimeError as exc:
        # Live+LIVE_ORDERS!=0 (or other gate): reject before secret/network.
        data_root = resolve_data_root(e)
        log_path = resolve_log_path(data_root, e)
        _setup_logging(log_path)
        report = scrub_public_record(
            {
                "stage": "readonly_auth",
                "ts_ms": int(time.time() * 1000),
                "venue": resolve_venue(e),
                "credential_profile": (
                    "live" if resolve_venue(e) == "live" else "testnet"
                ),
                "LIVE_ORDERS": e.get("LIVE_ORDERS", "0"),
                "send_allowed": send_allowed(e),
                "live_env_opened": False,
                "orders_sent": 0,
                "probes": [],
                "status": "rejected_before_network",
                "gate": type(exc).__name__,
                "config": public_config_snapshot(e),
            }
        )
        LOG.error(
            "readonly gate rejected venue=%s LIVE_ORDERS=%s before secrets/network",
            report.get("venue"),
            e.get("LIVE_ORDERS", "0"),
        )
        _write_probe_record(data_root, report)
        return report

    endpoints = endpoints_for_venue(venue)
    credential_profile = "live" if venue == "live" else "testnet"

    data_root = resolve_data_root(e)
    log_path = resolve_log_path(data_root, e)
    _setup_logging(log_path)

    report: dict[str, Any] = {
        "stage": "readonly_auth",
        "ts_ms": int(time.time() * 1000),
        "venue": venue,
        "credential_profile": credential_profile,
        "LIVE_ORDERS": e.get("LIVE_ORDERS", "0"),
        "send_allowed": send_allowed(e),
        "endpoints": {
            "bybit_rest": endpoints.bybit_rest,
            "okx_rest": endpoints.okx_rest,
            "okx_simulated_trading": endpoints.okx_simulated_trading,
            "bybit_account_path": endpoints.bybit_account_path,
            "okx_account_path": endpoints.okx_account_path,
        },
        "paths": {
            "data_root": str(data_root),
            "log_basename": log_path.name,
        },
        "config": public_config_snapshot(e),
        "live_env_opened": False,
        "orders_sent": 0,
        "probes": [],
        "status": "incomplete",
        "journal_v1_ok": False,
    }

    LOG.info(
        "readonly start venue=%s profile=%s LIVE_ORDERS=%s send_allowed=%s bybit=%s okx=%s simulated=%s",
        venue,
        credential_profile,
        e.get("LIVE_ORDERS", "0"),
        send_allowed(e),
        endpoints.bybit_rest,
        endpoints.okx_rest,
        endpoints.okx_simulated_trading,
    )

    try:
        if venue == "live":
            secrets: SecretsT = load_live_secrets(e, require_complete=False)
        else:
            secrets = load_testnet_secrets(e, require_complete=False)
    except FileNotFoundError:
        report["status"] = "secrets_unavailable"
        LOG.warning("secrets unavailable profile=%s", credential_profile)
        if not allow_missing_secrets:
            raise
        _write_probe_record(data_root, report)
        return scrub_public_record(report)
    except RuntimeError:
        report["status"] = "secrets_misconfigured"
        LOG.error("secrets misconfigured profile=%s", credential_profile)
        _write_probe_record(data_root, report)
        return scrub_public_record(report)

    # Profile path sanity without logging the path.
    src_name = Path(secrets.source_path).name.lower()
    if venue == "testnet" and "live" in src_name:
        report["status"] = "secrets_misconfigured"
        report["live_env_opened"] = False
        LOG.error("refused live-named secret basename on testnet route")
        _write_probe_record(data_root, report)
        return scrub_public_record(report)
    if venue == "live" and "live" not in src_name:
        report["status"] = "secrets_misconfigured"
        report["live_env_opened"] = False
        LOG.error("refused non-live secret basename on live route")
        _write_probe_record(data_root, report)
        return scrub_public_record(report)

    if venue == "live":
        report["live_env_opened"] = True
        report["credential_profile"] = "live"
    else:
        report["live_env_opened"] = False
        report["credential_profile"] = "testnet"

    report["key_presence"] = secrets.presence()
    LOG.info(
        "secrets loaded profile=%s live_env_opened=%s presence=%s",
        report["credential_profile"],
        report["live_env_opened"],
        secrets.presence(),
    )

    probes = _run_exchange_probes(secrets=secrets, endpoints=endpoints, venue=venue)
    report["probes"] = probes
    report["orders_sent"] = sum(int(p.get("orders_sent") or 0) for p in probes)
    successes = [p for p in probes if p.get("ok")]
    if report["orders_sent"] != 0:
        report["status"] = "invariant_violation"
    elif len(successes) == 2:
        report["status"] = "ok"
    elif any(p.get("ok") for p in probes):
        report["status"] = "partial"
    elif all(p.get("skipped") for p in probes):
        report["status"] = "secrets_incomplete"
    else:
        report["status"] = "auth_failed"

    # v1 journal is fail-closed: append/validation errors must not yield status=ok.
    try:
        v1_writer = PrivateJournalWriter(data_root)
        _append_v1_auth_and_account_reads(
            writer=v1_writer,
            secrets=secrets,
            endpoints=endpoints,
            venue=venue,
            probes=probes,
        )
        report["journal_v1_ok"] = True
    except Exception:  # noqa: BLE001 — never log exception text / secrets
        LOG.error("v1 journal append failed outcome=journal_write_failed")
        report["journal_v1_ok"] = False
        # Prefer explicit journal failure over a false probe "ok".
        report["status"] = "journal_write_failed"

    public = scrub_public_record(report)
    _write_probe_record(data_root, public)
    LOG.info(
        "readonly done status=%s profile=%s orders_sent=%s live_env_opened=%s journal_v1_ok=%s",
        public.get("status"),
        public.get("credential_profile"),
        public.get("orders_sent"),
        public.get("live_env_opened"),
        public.get("journal_v1_ok"),
    )
    return public


def _write_probe_record(data_root: Path, report: Mapping[str, Any]) -> None:
    path = auth_probe_jsonl_path(data_root, _utc_date())
    public = scrub_public_record(
        {
            "schema": "bbot.private.auth_probe.v0",
            "event_date": _utc_date(),
            **dict(report),
        }
    )
    _append_jsonl(path, public)
    LOG.info("auth_probe appended probes_path=%s", path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        from app.bot.private.selftest import run_selftest

        ok = run_selftest()
        return 0 if ok else 2

    if "--ws-readonly" in argv:
        # Explicit opt-in only. Default CLI path never reaches here without the flag.
        from app.bot.private.ws_readonly import main_ws_readonly

        return main_ws_readonly(argv)

    if "--ws-w4-post-only" in argv:
        from app.bot.private.ws_w4_postonly import main_ws_w4_post_only

        return main_ws_w4_post_only(argv)

    if "--ws-w5-market" in argv:
        from app.bot.private.ws_w5_market import main_ws_w5_market

        return main_ws_w5_market(argv)

    if "--ws-w6-dual-leg" in argv:
        from app.bot.private.ws_w6_dual_leg import main_ws_w6_dual_leg

        return main_ws_w6_dual_leg(argv)

    if "--ws-w7-parallel-dual-leg" in argv:
        from app.bot.private.ws_w7_parallel_dual_leg import main_ws_w7_parallel_dual_leg

        return main_ws_w7_parallel_dual_leg(argv)

    report = run_readonly_harness()
    print(json.dumps(scrub_public_record(report), ensure_ascii=False, indent=2, sort_keys=True))
    if report.get("status") == "ok":
        return 0
    if report.get("status") in {"secrets_unavailable", "secrets_incomplete"}:
        return 1
    # journal_write_failed, auth_failed, rejected_before_network, …
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Safely prepare 1x leverage for the exact current Gear 2.3 extra pool.

This command performs symbol-scoped flat/open-order checks for every target
before any setting call. It only sets leverage to 1 and writes a confirmation
env file after both venues report 1 for every target.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

from app.bot.private.leverage_one import LeverageTarget, set_leverage_one
from app.bot.private.rest_readonly import build_okx_readonly_headers
from app.bot.private.venue import endpoints_for_venue
from app.bot.private.ws_w4_baseline import (
    _BYBIT_OPEN,
    _BYBIT_POS,
    _OKX_OPEN,
    _OKX_POS,
    _bybit_open_orders_flat,
    _bybit_position_flat,
    _bybit_signed_get,
    _okx_open_orders_flat,
    _okx_position_flat,
    _okx_signed_get,
    _http_get_json,
)
from app.bot.hot_add import resolve_hot_add_meta
from app.bot.synthetic_policy import CANARY29_COINS


SOURCE_DEFAULT = Path("/data/bbot-would-send-prod/hot_add_delta.csv")
ENV_DEFAULT = Path("/etc/spread/bbot-private-live.env")
EXPECTED_EXTRAS = 25
BASE_COINS = frozenset(CANARY29_COINS)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def snapshot_targets(path: Path) -> tuple[list[LeverageTarget], str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    names = [str(row.get("base_coin") or "").strip().upper() for row in rows]
    if not names or any(not coin for coin in names) or len(names) != len(set(names)):
        raise RuntimeError("snapshot_coin_rows_invalid")
    if BASE_COINS.intersection(names):
        raise RuntimeError("snapshot_contains_base_coin")
    if len(names) != EXPECTED_EXTRAS:
        raise RuntimeError("snapshot_extra_count_changed")
    targets = []
    for row in rows:
        meta, reason = resolve_hot_add_meta(row, {})
        if meta is None or reason is not None:
            raise RuntimeError(f"snapshot_metadata_invalid:{reason or 'unknown'}")
        targets.append(
            LeverageTarget(meta.base_coin, meta.okx_symbol, meta.bybit_symbol)
        )
    return sorted(targets, key=lambda target: target.coin), digest


def _bybit_position_idx_supported(data: Mapping[str, Any], symbol: str) -> bool:
    result = data.get("result") or {}
    rows = result.get("list") or [] if isinstance(result, Mapping) else []
    if not isinstance(rows, list):
        return False
    return all(
        row.get("positionIdx") in (None, "", 0, "0")
        for row in rows
        if isinstance(row, Mapping) and str(row.get("symbol") or "") == symbol
    )


def _okx_cross_one(data: Mapping[str, Any], symbol: str) -> bool:
    rows = data.get("data") or []
    return (
        str(data.get("code")) == "0"
        and isinstance(rows, list)
        and bool(rows)
        and isinstance(rows[0], Mapping)
        and str(rows[0].get("instId")) == symbol
        and str(rows[0].get("lever")) == "1"
        and str(rows[0].get("mgnMode")) == "cross"
    )


def _preflight_target(
    target: LeverageTarget,
    *,
    bybit: Any,
    okx: Any,
    bybit_base: str,
    okx_base: str,
) -> None:
    from validation.run_response_manager_experiment import _bybit_pages

    for page in _bybit_pages(
        bybit,
        bybit_base,
        _BYBIT_POS,
        f"category=linear&symbol={target.bybit_symbol}&settleCoin=USDT&limit=200",
    ):
        if not _bybit_position_flat(page, target.bybit_symbol):
            raise RuntimeError(f"position_not_flat:bybit:{target.coin}")
        if not _bybit_position_idx_supported(page, target.bybit_symbol):
            raise RuntimeError(f"position_mode_unsupported:bybit:{target.coin}")
    for page in _bybit_pages(
        bybit,
        bybit_base,
        _BYBIT_OPEN,
        f"category=linear&symbol={target.bybit_symbol}&openOnly=0&limit=50",
    ):
        if not _bybit_open_orders_flat(page, target.bybit_symbol):
            raise RuntimeError(f"open_orders_not_flat:bybit:{target.coin}")

    op = _okx_signed_get(
        credentials=okx,
        base=okx_base,
        path_with_query=f"{_OKX_POS}?{urlencode({'instId': target.okx_symbol, 'instType': 'SWAP'})}",
    )
    if not _okx_position_flat(op, target.okx_symbol):
        raise RuntimeError(f"position_not_flat:okx:{target.coin}")
    oo = _okx_signed_get(
        credentials=okx,
        base=okx_base,
        path_with_query=f"{_OKX_OPEN}?{urlencode({'instId': target.okx_symbol, 'instType': 'SWAP'})}",
    )
    if not _okx_open_orders_flat(oo, target.okx_symbol):
        raise RuntimeError(f"open_orders_not_flat:okx:{target.coin}")


def _readback_bybit(target: LeverageTarget, *, bybit: Any, endpoints: Any) -> bool:
    bdata = _bybit_signed_get(
        credentials=bybit,
        base=endpoints.bybit_rest,
        path=_BYBIT_POS,
        query=f"category=linear&symbol={target.bybit_symbol}&settleCoin=USDT&limit=200",
    )
    brows = ((bdata.get("result") or {}).get("list")) or []
    matching = [
        row for row in brows
        if isinstance(row, Mapping) and str(row.get("symbol") or "") == target.bybit_symbol
    ]
    bvals = {
        str(row.get(field))
        for row in matching
        for field in ("leverage", "buyLeverage", "sellLeverage")
        if row.get(field) not in (None, "")
    }
    return (
        str(bdata.get("retCode")) == "0"
        and bool(matching)
        and bvals == {"1"}
        and _bybit_position_flat(bdata, target.bybit_symbol)
        and _bybit_position_idx_supported(bdata, target.bybit_symbol)
    )

def _readback_okx(target: LeverageTarget, *, okx: Any, endpoints: Any) -> bool:
    path = f"/api/v5/account/leverage-info?{urlencode({'instId': target.okx_symbol, 'mgnMode': 'cross'})}"
    headers = build_okx_readonly_headers(
        api_key=okx.api_key,
        api_secret=okx.api_secret,
        passphrase=okx.passphrase or "",
        path=path,
        simulated_trading=False,
    )
    odata = _http_get_json(
        f"{endpoints.okx_rest}{path}", headers, timeout_sec=15.0
    )
    return _okx_cross_one(odata, target.okx_symbol)


def prepare(
    *,
    source: Path,
    output_dir: Path,
    env_file: Path = ENV_DEFAULT,
) -> dict[str, Any]:
    from app.bot.private.secrets import load_live_secrets
    from app.bot.private.ws_warm_session import _creds_from_live_secrets

    targets, digest = snapshot_targets(source)
    coins = [target.coin for target in targets]
    if output_dir.exists() or output_dir.is_symlink():
        raise RuntimeError("output_dir_already_exists")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.environ["BBOT_PRIVATE_ENV_FILE"] = str(env_file)
    secrets = load_live_secrets(dict(os.environ), require_complete=True)
    bybit = _creds_from_live_secrets(secrets, "bybit")
    okx = _creds_from_live_secrets(secrets, "okx")
    endpoints = endpoints_for_venue("live")

    # Complete every symbol safety check before the first setting request.
    try:
        for target in targets:
            _preflight_target(
                target,
                bybit=bybit,
                okx=okx,
                bybit_base=endpoints.bybit_rest,
                okx_base=endpoints.okx_rest,
            )
    except Exception as exc:
        _write_json(output_dir / "result.json", {
                "schema": "gear23.1x.preparation.v1",
                "status": "preflight_failed",
                "error_type": type(exc).__name__,
                "error_code": str(exc).split(":", 1)[0],
                "snapshot_sha256": digest,
                "targets": [{"coin": target.coin, "okx_symbol": target.okx_symbol, "bybit_symbol": target.bybit_symbol} for target in targets],
                "settings_posts": 0,
                "orders_sent": 0,
            })
        raise
    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        _write_json(output_dir / "result.json", {
                "schema": "gear23.1x.preparation.v1",
                "status": "snapshot_changed_during_preflight",
                "snapshot_sha256": digest,
                "settings_posts": 0,
                "orders_sent": 0,
            })
        raise RuntimeError("snapshot_changed_during_preflight")

    setter_confirmed = set_leverage_one(
        targets,
        okx_credentials=okx,
        bybit_credentials=bybit,
        endpoints=endpoints,
    )
    results = [
        {
            "coin": target.coin,
            "okx_symbol": target.okx_symbol,
            "bybit_symbol": target.bybit_symbol,
            "setter_ack": {
                "bybit": setter_confirmed.get(("bybit", target.bybit_symbol)) == "1",
                "okx": setter_confirmed.get(("okx", target.okx_symbol)) == "1",
            },
            "readback_1x": {"bybit": False, "okx": False},
        }
        for target in targets
    ]
    _write_json(output_dir / "result.json", {
            "schema": "gear23.1x.preparation.v1",
            "status": "readback_in_progress",
            "snapshot_sha256": digest,
            "targets": results,
            "settings_posts": 2 * len(targets),
            "orders_sent": 0,
        })
    all_readbacks_ok = True
    for target, result in zip(targets, results):
        try:
            bybit_ok = _readback_bybit(target, bybit=bybit, endpoints=endpoints)
        except Exception as exc:
            bybit_ok = False
            result["bybit_readback_error_type"] = type(exc).__name__
        try:
            okx_ok = _readback_okx(target, okx=okx, endpoints=endpoints)
        except Exception as exc:
            okx_ok = False
            result["okx_readback_error_type"] = type(exc).__name__
        bybit_set = setter_confirmed.get(("bybit", target.bybit_symbol)) == "1"
        okx_set = setter_confirmed.get(("okx", target.okx_symbol)) == "1"
        all_readbacks_ok = all_readbacks_ok and bybit_set and okx_set and bybit_ok and okx_ok
        result["readback_1x"] = {"bybit": bybit_ok, "okx": okx_ok}
        _write_json(output_dir / "result.json", {
                "schema": "gear23.1x.preparation.v1",
                "status": "readback_in_progress",
                "snapshot_sha256": digest,
                "targets": results,
                "settings_posts": 2 * len(targets),
                "orders_sent": 0,
            })

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report: dict[str, Any] = {
        "schema": "gear23.1x.preparation.v1",
        "completed_at_utc": now,
        "status": "confirmed_1x" if all_readbacks_ok else "incomplete_readback",
        "snapshot_path": str(source),
        "snapshot_sha256": digest,
        "base_coins_unchanged": sorted(BASE_COINS),
        "targets": results,
        "settings_posts": 2 * len(targets),
        "orders_sent": 0,
    }
    _write_json(output_dir / "result.json", report)
    if all_readbacks_ok:
        (output_dir / "confirmed_1x.env").write_text(
            "BBOT_COINS=" + ",".join(CANARY29_COINS) + "\n"
            + "BBOT_CONFIRMED_1X_COINS=" + ",".join((*CANARY29_COINS, *coins)) + "\n",
            encoding="utf-8",
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-csv", type=Path, default=SOURCE_DEFAULT)
    parser.add_argument("--private-env-file", type=Path, default=ENV_DEFAULT)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = prepare(
        source=args.source_csv,
        output_dir=args.output_dir,
        env_file=args.private_env_file,
    )
    print(json.dumps({
        "status": report["status"],
        "target_count": len(report["targets"]),
        "settings_posts": report["settings_posts"],
        "result_path": str(args.output_dir / "result.json"),
        "confirmed_env_path": str(args.output_dir / "confirmed_1x.env") if report["status"] == "confirmed_1x" else None,
    }, sort_keys=True))
    return 0 if report["status"] == "confirmed_1x" else 2


if __name__ == "__main__":
    raise SystemExit(main())

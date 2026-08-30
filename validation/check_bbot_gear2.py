#!/usr/bin/env python3
"""Read-only Gear2 would_send journal + D-isolation check.

Does not start/stop services, write D trees, or claim profitability.
Zero intents is not a mechanical FAIL (soak may observe no trades).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.policy.trade_manager import (
    GEAR2_WOULD_SEND_COINS,
    hyper_for_profile,
    variation_for_profile,
)
from validation.check_bbot_isolation import find_bbot_in_d_trees


ALLOWED_COINS = {c.upper() for c in GEAR2_WOULD_SEND_COINS}
PROFILE = "gear2_would_send"


def _load_legs(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    truncated = 0
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
        raw = line.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            if i == len(text.splitlines()):
                truncated += 1
                continue
            raise
        if not isinstance(rec, dict):
            raise ValueError(f"{path}:{i} not a JSON object")
        rows.append(rec)
    return rows, truncated


def _pairs(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in rows:
        by[str(rec["intent_id"])].append(rec)
    return by


def check_gear2_journal(data_root: Path) -> list[str]:
    errors: list[str] = []
    journal = data_root / "journal"
    files = sorted(journal.glob("event_date=*/legs.jsonl")) if journal.is_dir() else []
    variation = variation_for_profile(PROFILE)
    hyper = hyper_for_profile(PROFILE)
    thresh_ol = float(variation["thresh_open_long"])
    thresh_os = float(variation["thresh_open_short"])
    thresh_cl = float(variation["thresh_close_long"])
    thresh_cs = float(variation["thresh_close_short"])
    expected_avg = float(hyper["avg_window_sec"] or 0)
    max_okx = hyper.get("max_latency_okx_ms")
    max_bybit = hyper.get("max_latency_bybit_ms")

    n_intents = 0
    n_with_context = 0
    occupancy_events: list[tuple[int, str, str, str]] = []

    for path in files:
        rows, truncated = _load_legs(path)
        if truncated:
            errors.append(f"truncated_tail | {path}")
        for intent_id, legs in _pairs(rows).items():
            n_intents += 1
            if len(legs) != 2:
                errors.append(f"intent {intent_id}: expected 2 legs, got {len(legs)}")
                continue
            sides = {str(x["exchange"]) for x in legs}
            if sides != {"okx", "bybit"}:
                errors.append(f"intent {intent_id}: exchanges {sorted(sides)}")
            a, b = legs[0], legs[1]
            for rec in legs:
                if rec.get("would_send") is not True or rec.get("send") is not False:
                    errors.append(f"intent {intent_id}: would_send/send invariant")
                if rec.get("k_live") not in (1, 1.0):
                    errors.append(f"intent {intent_id}: k_live != 1")
                coin = str(rec.get("base_coin") or "").upper()
                if coin and coin not in ALLOWED_COINS:
                    errors.append(f"intent {intent_id}: coin {coin} not in {sorted(ALLOWED_COINS)}")
                if rec.get("status") == "filled":
                    if rec.get("fill_ts_ms") is None:
                        errors.append(f"intent {intent_id}: filled without fill_ts_ms")
                    elif int(rec["fill_ts_ms"]) < int(rec["signal_ts_ms"]) + int(
                        rec["Trade_Lat_ms"]
                    ):
                        errors.append(f"intent {intent_id}: fill before Trade_Lat")
                    if rec.get("tick_valid") is not True:
                        errors.append(f"intent {intent_id}: filled tick_valid!=true")
            if float(a["notional"]) != float(b["notional"]):
                errors.append(f"intent {intent_id}: notional mismatch")

            sample = a
            spread_side = str(sample.get("spread_side"))
            status = str(sample.get("status"))
            coin = str(sample.get("base_coin") or "").upper()
            ts = int(sample.get("fill_ts_ms") or sample.get("signal_ts_ms") or 0)
            if status == "filled" and spread_side in ("open_long", "open_short"):
                occupancy_events.append((ts, "open", intent_id, coin))
            elif status == "filled" and spread_side == "close":
                occupancy_events.append((ts, "close", intent_id, coin))

            has_ctx = "spread_long" in sample and "spread_short" in sample
            if not has_ctx:
                continue
            n_with_context += 1
            profile = str(sample.get("bbot_profile") or "")
            if profile and profile != PROFILE:
                errors.append(f"intent {intent_id}: bbot_profile={profile!r}")
            for key, expected in (
                ("thresh_open_long", thresh_ol),
                ("thresh_open_short", thresh_os),
                ("thresh_close_long", thresh_cl),
                ("thresh_close_short", thresh_cs),
            ):
                if key in sample and float(sample[key]) != expected:
                    errors.append(
                        f"intent {intent_id}: {key}={sample[key]} != {expected}"
                    )
            if "avg_window_sec" in sample and float(sample["avg_window_sec"]) != expected_avg:
                errors.append(
                    f"intent {intent_id}: avg_window_sec={sample['avg_window_sec']} != {expected_avg}"
                )
            sl = float(sample["spread_long"])
            ss = float(sample["spread_short"])
            if spread_side == "open_long" and sl <= thresh_ol:
                errors.append(f"intent {intent_id}: open_long spread_long={sl} <= {thresh_ol}")
            elif spread_side == "open_short" and ss <= thresh_os:
                errors.append(f"intent {intent_id}: open_short spread_short={ss} <= {thresh_os}")
            elif spread_side == "close":
                if not (ss > thresh_cl or sl > thresh_cs):
                    errors.append(
                        f"intent {intent_id}: close spreads {sl}/{ss} below close thresh"
                    )
            if max_okx is not None and sample.get("okx_latency_ms") is not None:
                if float(sample["okx_latency_ms"]) > float(max_okx):
                    errors.append(f"intent {intent_id}: okx latency above cap")
            if max_bybit is not None and sample.get("bybit_latency_ms") is not None:
                if float(sample["bybit_latency_ms"]) > float(max_bybit):
                    errors.append(f"intent {intent_id}: bybit latency above cap")

    occupancy_events.sort(key=lambda item: (item[0], 0 if item[1] == "close" else 1))
    held: str | None = None
    for _ts, kind, intent_id, coin in occupancy_events:
        if kind == "open":
            if held is not None:
                errors.append(
                    f"overlap: open {intent_id} coin={coin} while held={held}"
                )
            held = coin
        else:
            if held is None:
                errors.append(f"close_without_open: {intent_id} coin={coin}")
            elif held != coin:
                errors.append(
                    f"close_not_held: {intent_id} coin={coin} held={held}"
                )
            else:
                held = None

    print(
        f"intents={n_intents} with_signal_context={n_with_context} "
        f"profile={PROFILE} thresh=0.02/0.02/0.02/0.02 avg_window_sec={expected_avg}"
    )
    if n_intents > 0 and n_with_context == 0:
        errors.append("no_signal_context_fields")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="/data/bbot-gear2")
    parser.add_argument("--skip-d-trees", action="store_true")
    args = parser.parse_args()
    errors = check_gear2_journal(Path(args.data_root))
    if not args.skip_d_trees:
        find_bbot_in_d_trees(errors)
    if errors:
        for item in errors:
            print(f"ERROR: {item}")
        print(f"verdict=FAIL errors={len(errors)}")
        return 1
    print("OK: gear2 would_send journal invariants hold (zero trades is not FAIL)")
    print("not_claimed=PnL, alpha, live-send readiness, frozen gear-2 close stamp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

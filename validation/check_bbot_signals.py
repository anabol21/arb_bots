#!/usr/bin/env python3
"""Read-only check that stub journal intents match the test/gear-1 gates.

Does not start services, write D trees, or claim profitability.
Requires optional extra fields written by BBOT_PROFILE=signal_test:
spread_long, spread_short, ma_*, latency, thresh_*.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.policy.trade_manager import DEFAULT_HYPER, variation_for_profile


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
            if i == text.count("\n") or i == len(text.splitlines()):
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


def check_signals(
    data_root: Path,
    *,
    profile: str = "signal_test",
) -> list[str]:
    errors: list[str] = []
    journal = data_root / "journal"
    files = sorted(journal.glob("event_date=*/legs.jsonl")) if journal.is_dir() else []
    if not files:
        errors.append("journal_absent")
        return errors

    variation = variation_for_profile(profile)
    hyper = DEFAULT_HYPER
    thresh_ol = float(variation["thresh_open_long"])
    thresh_os = float(variation["thresh_open_short"])
    thresh_cl = float(variation["thresh_close_long"])
    thresh_cs = float(variation["thresh_close_short"])
    open_frac = float(variation["open_frac"])
    close_frac = float(variation["close_frac"])
    max_okx = hyper.get("max_latency_okx_ms")
    max_bybit = hyper.get("max_latency_bybit_ms")

    n_intents = 0
    n_with_context = 0
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
            has_ctx = "spread_long" in sample and "spread_short" in sample
            if not has_ctx:
                continue
            n_with_context += 1
            sl = float(sample["spread_long"])
            ss = float(sample["spread_short"])
            ma_l = sample.get("ma_long")
            ma_s = sample.get("ma_short")
            if spread_side == "open_long":
                if sl <= thresh_ol:
                    errors.append(
                        f"intent {intent_id}: open_long spread_long={sl} <= {thresh_ol}"
                    )
                if ma_l is not None and float(ma_l) < open_frac * thresh_ol:
                    errors.append(
                        f"intent {intent_id}: open_long ma_long={ma_l} < Gate B"
                    )
            elif spread_side == "open_short":
                if ss <= thresh_os:
                    errors.append(
                        f"intent {intent_id}: open_short spread_short={ss} <= {thresh_os}"
                    )
                if ma_s is not None and float(ma_s) < open_frac * thresh_os:
                    errors.append(
                        f"intent {intent_id}: open_short ma_short={ma_s} < Gate B"
                    )
            elif spread_side == "close":
                # Close long uses spread_short; close short uses spread_long.
                # Extra does not record position; accept either side clearing.
                close_ok = ss > thresh_cl or sl > thresh_cs
                if not close_ok:
                    errors.append(
                        f"intent {intent_id}: close spreads {sl}/{ss} below close thresh"
                    )
                if ma_s is not None and ma_l is not None:
                    b_ok = (float(ma_s) >= close_frac * thresh_cl) or (
                        float(ma_l) >= close_frac * thresh_cs
                    )
                    if not b_ok:
                        errors.append(f"intent {intent_id}: close Gate B failed")
            if max_okx is not None and sample.get("okx_latency_ms") is not None:
                if float(sample["okx_latency_ms"]) > float(max_okx):
                    errors.append(f"intent {intent_id}: okx latency above cap")
            if max_bybit is not None and sample.get("bybit_latency_ms") is not None:
                if float(sample["bybit_latency_ms"]) > float(max_bybit):
                    errors.append(f"intent {intent_id}: bybit latency above cap")

    print(
        f"intents={n_intents} with_signal_context={n_with_context} "
        f"profile={profile} thresh_open={thresh_ol}"
    )
    if n_intents == 0:
        errors.append("no_intents")
    elif n_with_context == 0:
        errors.append("no_signal_context_fields")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="/data/bbot")
    parser.add_argument("--profile", default="signal_test")
    args = parser.parse_args()
    errors = check_signals(Path(args.data_root), profile=args.profile)
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        return 1
    print("OK: stub signals match recorded context and Trade_Lat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

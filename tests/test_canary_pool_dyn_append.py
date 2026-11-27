"""Canary-owned gear23 pool-dyn sidecar: append-only top-10 expansion."""

from __future__ import annotations

import csv
import fcntl
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.bot.canary_pool_dyn_append import (  # noqa: E402
    BASE,
    FIELDS,
    MSK,
    SEED,
    DeltaPathError,
    Paths,
    Settings,
    assert_delta_writable,
    current_pool,
    parse_top10,
    plan_additions,
    run,
    stamp_matches_today,
    validate_own_delta,
)


SEED_COINS = list(SEED)
BASE_COINS = list(BASE)


def _delta_row(coin: str, **overrides: str) -> dict[str, str]:
    row = {
        "base_coin": coin,
        "okx_symbol": f"{coin}-USDT-SWAP",
        "bybit_symbol": f"{coin}USDT",
        "okx_tick_size": "0.01",
        "okx_lot_size": "1",
        "okx_min_size": "1",
        "bybit_tick_size": "0.01",
        "bybit_qty_step": "1",
        "bybit_min_order_qty": "1",
        "bybit_min_notional_value": "5",
        "discovered_at_utc": "2026-10-22T00:00:00Z",
    }
    row.update(overrides)
    return row


def _write_csv(path: Path, rows: list[dict[str, str]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields or FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _seed_rows() -> list[dict[str, str]]:
    return [_delta_row(c) for c in SEED_COINS]


class _Harness:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pool_dir = self.root / "state" / "pool_dyn"
        self.pool_dir.mkdir(parents=True)
        self.delta = self.pool_dir / "hot_add_delta_dyn.csv"
        self.shared_delta = self.root / "shared" / "hot_add_delta.csv"
        self.shared_universe = self.root / "shared" / "bybit_okx_universe.csv"
        self.discovery = self.root / "discovery" / "hot_add_delta.csv"
        self.top10 = self.root / "top10_volatile_spread.json"
        self.last_launcher = self.root / "state" / "last_launcher.txt"
        self.last_launcher.parent.mkdir(parents=True, exist_ok=True)
        _write_csv(self.delta, _seed_rows())
        _write_csv(self.shared_delta, [])
        _write_csv(self.shared_universe, [])
        _write_csv(self.discovery, [])
        self.last_launcher.write_text("launcher=/tmp/gear23-pool-dyn-launch.sh\n", encoding="utf-8")
        self.paths = Paths(
            pool_dir=self.pool_dir,
            delta=self.delta,
            status=self.pool_dir / "last_append.json",
            log=self.pool_dir / "append.log",
            events=self.pool_dir / "append_events.jsonl",
            last_launcher=self.last_launcher,
            top10_candidates=(self.top10, self.root / "missing.json"),
            meta_sources=(
                ("shared_delta", self.shared_delta),
                ("shared_universe", self.shared_universe),
                ("code_universe", self.root / "missing_universe.csv"),
                ("discovery_delta", self.discovery),
            ),
        )
        self.now = datetime(2026, 10, 24, 10, 10, tzinfo=MSK)

    def close(self) -> None:
        self.tmp.cleanup()

    def write_top10(
        self,
        coins: list[str],
        *,
        stamp: str = "2026-10-24T09:16:00+03:00",
        stds: dict[str, float] | None = None,
        extra: dict | None = None,
    ) -> None:
        rows = []
        for coin in coins:
            item: dict = {"base_coin": coin}
            if stds and coin in stds:
                item["std_spread_long_bps"] = stds[coin]
            rows.append(item)
        payload = {"stamp": stamp, "window_end_msk": stamp, "top10": rows}
        if extra:
            payload.update(extra)
        self.top10.write_text(json.dumps(payload), encoding="utf-8")

    def put_meta(self, *coins: str, source: str = "shared_delta", **overrides: str) -> None:
        path = {
            "shared_delta": self.shared_delta,
            "shared_universe": self.shared_universe,
            "discovery_delta": self.discovery,
        }[source]
        existing = []
        if path.exists():
            with path.open(encoding="utf-8", newline="") as fh:
                existing = list(csv.DictReader(fh))
        have = {str(r.get("base_coin", "")).upper() for r in existing}
        for coin in coins:
            if coin.upper() in have:
                continue
            existing.append(_delta_row(coin.upper(), **overrides))
        _write_csv(path, existing)

    def extras(self) -> list[str]:
        return current_pool(self.delta.read_text(encoding="utf-8"))

    def run(self, *argv: str, crypto=None) -> int:
        return run(list(argv), paths=self.paths, now=self.now, is_crypto_fn=crypto or (lambda _c: True))


class ParseTop10Tests(unittest.TestCase):
    def test_object_list_base_coin(self) -> None:
        coins, stds, _means = parse_top10(
            {
                "top10": [
                    {"base_coin": "irys", "std_spread_long_bps": 96.1},
                    {"base_coin": "BLEND"},
                ]
            }
        )
        self.assertEqual(coins, ["IRYS", "BLEND"])
        self.assertAlmostEqual(stds["IRYS"] or 0.0, 96.1)

    def test_string_list_and_dedup(self) -> None:
        coins, _stds, _means = parse_top10({"top10": ["WAL", "wal", "APR"]})
        self.assertEqual(coins, ["WAL", "APR"])

    def test_empty_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            parse_top10({"top10": []})


class StampTests(unittest.TestCase):
    def test_iso_and_compact(self) -> None:
        self.assertTrue(stamp_matches_today({"stamp": "2026-10-24T09:16:00+03:00"}, "2026-10-24"))
        self.assertTrue(stamp_matches_today({"window_end_msk": "20261024T091000"}, "2026-10-24"))
        self.assertFalse(stamp_matches_today({"stamp": "2026-10-23T09:16:00+03:00"}, "2026-10-24"))
        self.assertFalse(stamp_matches_today({}, "2026-10-24"))


class PlanTests(unittest.TestCase):
    def test_does_not_skip_would_send_base(self) -> None:
        h = _Harness()
        try:
            h.put_meta("BLEND", "APR")
            added, skipped, already = plan_additions(
                top10=["WAL", "BLEND", "APR"],
                pool=BASE_COINS + SEED_COINS,
                settings=Settings(max_total=30),
                meta_sources=h.paths.meta_sources,
                is_crypto_fn=lambda _c: True,
                discovered_at="2026-10-24T07:10:00Z",
            )
            self.assertEqual(already, ["WAL"])
            self.assertEqual([r["base_coin"] for r in added], ["BLEND", "APR"])
            self.assertEqual(skipped, [])
        finally:
            h.close()


class SidecarJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _Harness()

    def tearDown(self) -> None:
        self.h.close()

    def test_dry_run_default_does_not_write(self) -> None:
        before = self.h.delta.read_text(encoding="utf-8")
        self.h.put_meta("IRYS")
        self.h.write_top10(["IRYS"])
        self.assertEqual(self.h.run(), 0)
        self.assertEqual(self.h.delta.read_text(encoding="utf-8"), before)
        status = json.loads(self.h.paths.status.read_text(encoding="utf-8"))
        self.assertEqual(status["mode"], "DRY-RUN")
        self.assertEqual(status["would_add"], ["IRYS"])
        self.assertEqual(status["added"], [])

    def test_explicit_dry_run_flag(self) -> None:
        before = self.h.delta.read_text(encoding="utf-8")
        self.h.put_meta("IRYS")
        self.h.write_top10(["IRYS"])
        self.assertEqual(self.h.run("--dry-run"), 0)
        self.assertEqual(self.h.delta.read_text(encoding="utf-8"), before)

    def test_dedup_vs_base_and_existing_delta(self) -> None:
        self.h.put_meta("API3", "WAL", "IRYS")
        self.h.write_top10(["WAL", "API3", "IRYS", "IRYS"])
        self.assertEqual(self.h.run("--apply"), 0)
        self.assertEqual(self.h.extras(), SEED_COINS + ["IRYS"])

    def test_append_only_preserves_existing_bytes(self) -> None:
        before = self.h.delta.read_text(encoding="utf-8")
        self.h.put_meta("IRYS")
        self.h.write_top10(["IRYS"])
        self.assertEqual(self.h.run("--apply"), 0)
        after = self.h.delta.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before))
        self.assertIn("IRYS,IRYS-USDT-SWAP,IRYSUSDT,", after)
        self.assertTrue((self.h.pool_dir / "hot_add_delta_dyn.csv.tmp").exists() is False)
        backups = list(self.h.pool_dir.glob("hot_add_delta_dyn.csv.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), before)

    def test_idempotent_second_apply(self) -> None:
        self.h.put_meta("IRYS")
        self.h.write_top10(["IRYS"])
        self.assertEqual(self.h.run("--apply"), 0)
        once = self.h.delta.read_text(encoding="utf-8")
        self.assertEqual(self.h.run("--apply"), 0)
        self.assertEqual(self.h.delta.read_text(encoding="utf-8"), once)
        self.assertEqual(self.h.extras().count("IRYS"), 1)

    def test_cap_skips_overflow(self) -> None:
        self.h.put_meta("N1", "N2", "N3")
        self.h.write_top10(["N1", "N2", "N3"])
        # base 3 + seed 9 = 12; max_total 13 → one extra only
        self.assertEqual(self.h.run("--apply", "--max-total", "13"), 0)
        self.assertEqual(self.h.extras(), SEED_COINS + ["N1"])
        status = json.loads(self.h.paths.status.read_text(encoding="utf-8"))
        reasons = {row["coin"]: row["reason"] for row in status["skipped"]}
        self.assertEqual(reasons["N2"], "cap_total_13")
        self.assertEqual(reasons["N3"], "cap_total_13")

    def test_stale_digest_exits_nonzero_no_write(self) -> None:
        before = self.h.delta.read_text(encoding="utf-8")
        self.h.put_meta("IRYS")
        self.h.write_top10(["IRYS"], stamp="2026-10-23T09:16:00+03:00")
        self.assertEqual(self.h.run("--apply"), 3)
        self.assertEqual(self.h.delta.read_text(encoding="utf-8"), before)
        status = json.loads(self.h.paths.status.read_text(encoding="utf-8"))
        self.assertEqual(status["reason"], "stale_digest")

    def test_allow_stale_digest(self) -> None:
        self.h.put_meta("IRYS")
        self.h.write_top10(["IRYS"], stamp="2026-10-23T09:16:00+03:00")
        self.assertEqual(self.h.run("--apply", "--allow-stale-digest"), 0)
        self.assertIn("IRYS", self.h.extras())

    def test_missing_metadata_skipped(self) -> None:
        self.h.put_meta("OKCOIN")
        self.h.write_top10(["NOMETA", "OKCOIN"])
        self.assertEqual(self.h.run("--apply"), 0)
        self.assertEqual(self.h.extras(), SEED_COINS + ["OKCOIN"])
        status = json.loads(self.h.paths.status.read_text(encoding="utf-8"))
        self.assertEqual(status["skipped"], [{"coin": "NOMETA", "reason": "no_complete_meta"}])

    def test_ticker_mismatch_skipped(self) -> None:
        self.h.put_meta("AXIS", okx_symbol="AXIS-USDC-SWAP")
        self.h.write_top10(["AXIS"])
        self.assertEqual(self.h.run("--apply"), 0)
        self.assertNotIn("AXIS", self.h.extras())

    def test_incomplete_lot_tick_skipped(self) -> None:
        self.h.put_meta("BTW", okx_lot_size="")
        self.h.write_top10(["BTW"])
        self.assertEqual(self.h.run("--apply"), 0)
        self.assertNotIn("BTW", self.h.extras())

    def test_metadata_from_discovery(self) -> None:
        self.h.put_meta("NEWC", source="discovery_delta")
        self.h.write_top10(["NEWC"])
        self.assertEqual(self.h.run("--apply"), 0)
        self.assertIn("NEWC", self.h.extras())

    def test_launcher_gate_blocks_apply(self) -> None:
        self.h.last_launcher.write_text("launcher=/tmp/gear23-pool12-launch.sh\n", encoding="utf-8")
        before = self.h.delta.read_text(encoding="utf-8")
        self.h.put_meta("IRYS")
        self.h.write_top10(["IRYS"])
        self.assertEqual(self.h.run("--apply"), 0)
        self.assertEqual(self.h.delta.read_text(encoding="utf-8"), before)
        status = json.loads(self.h.paths.status.read_text(encoding="utf-8"))
        self.assertEqual(status["reason"], "bot_not_on_dyn_launcher")
        self.assertEqual(status["would_add"], ["IRYS"])
        self.assertEqual(status["added"], [])

    def test_force_overrides_gate(self) -> None:
        self.h.last_launcher.write_text("launcher=/tmp/gear23-pool12-launch.sh\n", encoding="utf-8")
        self.h.put_meta("IRYS")
        self.h.write_top10(["IRYS"])
        self.assertEqual(self.h.run("--apply", "--force"), 0)
        self.assertIn("IRYS", self.h.extras())

    def test_missing_delta_aborts(self) -> None:
        self.h.delta.unlink()
        self.h.write_top10(["IRYS"])
        self.assertEqual(self.h.run("--apply"), 2)

    def test_missing_seed_aborts(self) -> None:
        _write_csv(self.h.delta, [_delta_row(c) for c in SEED_COINS if c != "TRUST"])
        self.h.write_top10(["IRYS"])
        self.assertEqual(self.h.run("--apply"), 2)
        self.assertNotIn("IRYS", current_pool(self.h.delta.read_text(encoding="utf-8")))

    def test_bad_header_aborts(self) -> None:
        self.h.delta.write_text("coin,x\nAPI3,1\n", encoding="utf-8")
        self.h.write_top10(["IRYS"])
        self.assertEqual(self.h.run("--apply"), 2)

    def test_missing_top10_exits_3(self) -> None:
        self.assertEqual(self.h.run("--apply"), 3)

    def test_optional_std_guard_default_off(self) -> None:
        self.h.put_meta("IRYS", "AXIS")
        self.h.write_top10(["IRYS", "AXIS"], stds={"IRYS": 120.0, "AXIS": 99.0})
        self.assertEqual(self.h.run("--apply"), 0)
        self.assertEqual(self.h.extras()[-2:], ["IRYS", "AXIS"])

    def test_optional_std_guard_skips_high_and_missing(self) -> None:
        self.h.put_meta("IRYS", "OKC", "NOS")
        self.h.write_top10(["IRYS", "OKC", "NOS"], stds={"IRYS": 96.0, "OKC": 10.0})
        self.assertEqual(self.h.run("--apply", "--max-std-bps", "50"), 0)
        self.assertEqual(self.h.extras(), SEED_COINS + ["OKC"])
        status = json.loads(self.h.paths.status.read_text(encoding="utf-8"))
        reasons = {row["coin"]: row["reason"] for row in status["skipped"]}
        self.assertTrue(reasons["IRYS"].startswith("std_bps_"))
        self.assertEqual(reasons["NOS"], "std_missing")

    def test_non_crypto_skipped(self) -> None:
        self.h.put_meta("HUT")
        self.h.write_top10(["HUT"])
        self.assertEqual(self.h.run("--apply", crypto=lambda c: c != "HUT"), 0)
        self.assertNotIn("HUT", self.h.extras())

    def test_lock_held_exits_4(self) -> None:
        self.h.write_top10(["IRYS"])
        lock = (self.h.pool_dir / "append.lock").open("w", encoding="utf-8")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.h.run("--apply"), 4)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def test_validate_rejects_base_in_delta(self) -> None:
        raw = self.h.delta.read_text(encoding="utf-8")
        raw += "WAL,WAL-USDT-SWAP,WALUSDT,0.01,1,1,0.01,1,1,5,2026-10-22T00:00:00Z\n"
        with self.assertRaises(ValueError):
            validate_own_delta(raw, Settings())

    def test_assert_delta_writable_refuses_shared_prefix(self) -> None:
        with self.assertRaises(DeltaPathError):
            assert_delta_writable(Path("/data/bbot-would-send-prod/hot_add_delta.csv"))
        with self.assertRaises(DeltaPathError):
            assert_delta_writable(Path("/data/live/hot_add_delta_dyn.csv"))


if __name__ == "__main__":
    unittest.main()

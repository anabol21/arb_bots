from __future__ import annotations

import csv
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from validation.prepare_gear23_1x import (
    BASE_COINS,
    EXPECTED_EXTRAS,
    _bybit_position_idx_supported,
    _okx_cross_one,
    prepare,
    snapshot_targets,
)


class Gear23OneXPreparationTests(unittest.TestCase):
    def _snapshot(self, coins: list[str]) -> Path:
        tmp = tempfile.NamedTemporaryFile(mode="w", newline="", delete=False)
        self.addCleanup(Path(tmp.name).unlink, missing_ok=True)
        writer = csv.DictWriter(tmp, fieldnames=[
            "base_coin", "okx_symbol", "bybit_symbol", "okx_tick_size",
            "okx_lot_size", "okx_min_size", "bybit_tick_size",
            "bybit_qty_step", "bybit_min_order_qty",
        ])
        writer.writeheader()
        writer.writerows({
            "base_coin": coin,
            "okx_symbol": f"{coin}-USDT-SWAP",
            "bybit_symbol": f"{coin}USDT",
            "okx_tick_size": "0.1",
            "okx_lot_size": "0.1",
            "okx_min_size": "0.1",
            "bybit_tick_size": "0.1",
            "bybit_qty_step": "0.1",
            "bybit_min_order_qty": "0.1",
        } for coin in coins)
        tmp.close()
        return Path(tmp.name)

    def test_snapshot_is_exact_unique_extra_pool(self) -> None:
        coins = [f"EXTRA{i}" for i in range(EXPECTED_EXTRAS)]
        parsed, digest = snapshot_targets(self._snapshot(coins))
        self.assertEqual([target.coin for target in parsed], sorted(coins))
        self.assertEqual(parsed[0].okx_symbol, f"{sorted(coins)[0]}-USDT-SWAP")
        self.assertEqual(parsed[0].bybit_symbol, f"{sorted(coins)[0]}USDT")
        self.assertEqual(len(digest), 64)

    def test_snapshot_rejects_duplicate_and_base_coin(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "snapshot_coin_rows_invalid"):
            snapshot_targets(self._snapshot(["A", "A"]))
        with self.assertRaisesRegex(RuntimeError, "snapshot_contains_base_coin"):
            snapshot_targets(self._snapshot([next(iter(BASE_COINS))]))

    def test_preparation_gates_reject_hedge_and_wrong_okx_readback(self) -> None:
        self.assertTrue(_bybit_position_idx_supported(
            {"result": {"list": [{"symbol": "X", "positionIdx": 0}]}}, "X"
        ))
        self.assertFalse(_bybit_position_idx_supported(
            {"result": {"list": [{"symbol": "X", "positionIdx": 1}]}}, "X"
        ))
        self.assertTrue(_okx_cross_one(
            {"code": "0", "data": [{"instId": "X-SWAP", "lever": "1", "mgnMode": "cross"}]},
            "X-SWAP",
        ))
        self.assertFalse(_okx_cross_one(
            {"code": "0", "data": [{"instId": "X-SWAP", "lever": "1", "mgnMode": "isolated"}]},
            "X-SWAP",
        ))
        self.assertFalse(_okx_cross_one(
            {"code": "0", "data": [{"instId": "OTHER-SWAP", "lever": "1", "mgnMode": "cross"}]},
            "X-SWAP",
        ))

    def test_no_setting_call_until_every_extra_passes_flat_preflight(self) -> None:
        coins = sorted(f"EXTRA{i}" for i in range(EXPECTED_EXTRAS))
        source = self._snapshot(coins)
        output = Path(tempfile.mkdtemp()) / "prep-result"
        self.addCleanup(shutil.rmtree, output.parent, ignore_errors=True)
        checked: list[str] = []

        def preflight(target, **_kwargs):
            checked.append(target.coin)
            if target.coin == coins[4]:
                raise RuntimeError("position_not_flat")

        with (
            patch("app.bot.private.secrets.load_live_secrets", return_value=object()),
            patch.dict(sys.modules, {
                "app.bot.private.ws_warm_session": SimpleNamespace(
                    _creds_from_live_secrets=lambda *_: (object(), object())
                )
            }),
            patch("validation.prepare_gear23_1x.endpoints_for_venue", return_value=SimpleNamespace(venue="live", bybit_rest="bybit", okx_rest="okx")),
            patch("validation.prepare_gear23_1x._preflight_target", side_effect=preflight),
            patch("validation.prepare_gear23_1x.set_leverage_one") as setter,
            patch.dict("os.environ", {}, clear=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "position_not_flat"):
                prepare(source=source, output_dir=output)
        setter.assert_not_called()
        self.assertEqual(checked, coins[:5])


if __name__ == "__main__":
    unittest.main()

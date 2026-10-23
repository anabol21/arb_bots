import json
import tempfile
import unittest
from pathlib import Path

from app.bot.paths import theta_trades_jsonl_path
from app.bot.theta_trade_manager import restore_synthetic_slot
from app.bot.theta_trade_plot import find_trade_events


class TradeObservationReaderCompatibilityTests(unittest.TestCase):
    def test_plot_and_synthetic_restore_ignore_observation_rows(self) -> None:
        trade_id = "shared-id"
        legacy = {
            "schema_version": "bbot.synthetic_roll.v1",
            "intent_id": trade_id,
            "trade_id": trade_id,
            "status": "open",
            "event": "open",
            "base_coin": "BTC",
            "side": "long",
            "signal_ts_ms": 1_800_000_000_000,
            "fill_ts_ms": 1_800_000_000_010,
            "coin_qty": "0.01",
        }
        observation = {
            "schema_version": "bbot.trade_observation.v1",
            "record_type": "trade_observation",
            "intent_id": trade_id,
            "trade_id": trade_id,
            "status": "completed",
            "event": "close",
            "signal_ts_ms": 1_800_000_000_100,
        }
        untyped_observation = {**observation, "record_type": None}

        with tempfile.TemporaryDirectory() as tmp:
            journal = theta_trades_jsonl_path(Path(tmp), "2027-01-15")
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(
                "".join(
                    json.dumps(row) + "\n"
                    for row in (legacy, observation, untyped_observation)
                ),
                encoding="utf-8",
            )

            rows = [legacy, observation, untyped_observation]
            plotted = find_trade_events(rows, trade_id)
            restored, pending, coin, side = restore_synthetic_slot(
                Path(tmp), notional_usdt=10.0
            )

        self.assertEqual(plotted, [legacy])
        self.assertFalse(pending)
        self.assertEqual((coin, side), ("BTC", "long"))
        self.assertIsNotNone(restored)
        self.assertEqual(restored.trade_id, trade_id)


if __name__ == "__main__":
    unittest.main()

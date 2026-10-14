"""Observer is called at policy formation, before simulated execution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.bot.theta_trade_manager import ThetaDecision, ThetaTradeManager


class ThetaPolicyObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_async_entry_reports_policy_decision_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            observed = []
            manager = ThetaTradeManager(
                data_root=Path(directory),
                decide_fn=lambda **_kwargs: ThetaDecision(
                    action="skip", base_coin="", side="", reason="no_signal"
                ),
                decision_observer=lambda decision, decided_ns: observed.append(
                    (decision.action, decided_ns)
                ),
            )
            execution = AsyncMock(return_value=[])
            manager._execute_decision_async = execution

            await manager.on_theta_snapshots_async([], quotes={}, coin_order=[])

            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][0], "skip")
            self.assertGreater(observed[0][1], 0)
            execution.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

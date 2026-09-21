"""Static safety contract for the bounded EV2-10 VPS units."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


class Ev210SystemdSafetyTests(unittest.TestCase):
    def test_main_unit_is_bounded_synthetic_and_no_order(self) -> None:
        text = (SYSTEMD / "spread-bbot-ev2-10-synthetic.service").read_text()
        for required in (
            "Environment=LIVE_ORDERS=0",
            "Environment=BBOT_BROKER=stub",
            "Environment=BBOT_POLICY_MODE=synthetic_roll_v1",
            "Environment=BBOT_SYNTHETIC_ROLL_SEED=7",
            "Environment=BBOT_THETA_LIVE_SEND=0",
            "Environment=BBOT_EV2_SHADOW=1",
            "Environment=BBOT_SLOT_K=1",
            "RuntimeMaxSec=7200",
            "Restart=no",
            "WorkingDirectory=/root/spread_ev2_10",
            "BBOT_DATA_ROOT=/data/bbot-ev2-10-synthetic",
        ):
            self.assertIn(required, text)
        self.assertNotIn("LIVE_ORDERS=1", text)
        self.assertNotIn("BBOT_BROKER=private_live", text)
        self.assertNotIn("EnvironmentFile=", text)

    def test_private_companion_is_read_only_and_isolated(self) -> None:
        text = (SYSTEMD / "spread-bbot-ev2-10-private@.service").read_text()
        self.assertIn("Environment=LIVE_ORDERS=0", text)
        self.assertIn("--ws-readonly", text)
        self.assertIn("PartOf=spread-bbot-ev2-10-synthetic.service", text)
        self.assertIn("/data/bbot-ev2-10-synthetic/private-%i", text)
        self.assertIn("InaccessiblePaths=", text)
        self.assertNotIn("--ws-place", text)
        self.assertNotIn("--ws-cancel", text)
        self.assertNotIn("LIVE_ORDERS=1", text)


if __name__ == "__main__":
    unittest.main()

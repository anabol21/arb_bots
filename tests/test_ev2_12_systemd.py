"""Static safety contract for the bounded EV2-12 prewrite canary."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


class Ev212SystemdTests(unittest.TestCase):
    def test_main_unit_is_isolated_bounded_synthetic_and_no_order(self) -> None:
        text = (SYSTEMD / "spread-bbot-ev2-12-prewrite.service").read_text()
        for required in (
            "WorkingDirectory=/root/spread_ev2_12",
            "Environment=LIVE_ORDERS=0",
            "Environment=BBOT_BROKER=stub",
            "Environment=BBOT_POLICY_MODE=synthetic_roll_v1",
            "Environment=BBOT_THETA_LIVE_SEND=0",
            "Environment=BBOT_EV2_SHADOW=1",
            "Environment=BBOT_EV2_AUDIT=1",
            "Environment=BBOT_PRIVATE_STATUS_DIR=/data/bbot-ev2-12-prewrite/private-status",
            "EnvironmentFile=/etc/spread/bbot-ev2-12-no-order.env",
            "RuntimeMaxSec=7200",
            "Restart=no",
            "InaccessiblePaths=",
        ):
            self.assertIn(required, text)
        self.assertNotIn("LIVE_ORDERS=1", text)
        self.assertNotIn("private_live", text)
        self.assertNotIn("BBOT_PRIVATE_SEND_PATH", text)

    def test_private_companions_are_readonly_and_bound_to_main(self) -> None:
        text = (SYSTEMD / "spread-bbot-ev2-12-private@.service").read_text()
        self.assertIn("Environment=LIVE_ORDERS=0", text)
        self.assertIn("--ws-readonly", text)
        self.assertIn("--status-path=/data/bbot-ev2-12-prewrite/private-status/%i.json", text)
        self.assertIn("PartOf=spread-bbot-ev2-12-prewrite.service", text)
        self.assertIn("BindsTo=spread-bbot-ev2-12-prewrite.service", text)
        self.assertNotIn("LIVE_ORDERS=1", text)
        self.assertNotIn("--w4-approve-one-shot", text)
        self.assertNotIn("BBOT_PRIVATE_SEND_PATH", text)

"""Static safety contract for the bounded EV2-11 restart canary units."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_main_unit_is_isolated_bounded_and_no_order() -> None:
    text = (SYSTEMD / "spread-bbot-ev2-11-restart.service").read_text()
    for required in (
        "WorkingDirectory=/root/spread_ev2_11",
        "Environment=LIVE_ORDERS=0",
        "Environment=BBOT_BROKER=stub",
        "Environment=BBOT_POLICY_MODE=synthetic_roll_v1",
        "Environment=BBOT_THETA_LIVE_SEND=0",
        "Environment=BBOT_EV2_SHADOW=1",
        "Environment=BBOT_DATA_ROOT=/data/bbot-ev2-11-restart",
        "RuntimeMaxSec=7200",
        "Restart=no",
        "InaccessiblePaths=",
    ):
        assert required in text
    assert "LIVE_ORDERS=1" not in text
    assert "private_live" not in text
    assert "trade_socket" not in text


def test_private_companions_are_read_only_and_bound_to_main() -> None:
    text = (SYSTEMD / "spread-bbot-ev2-11-private@.service").read_text()
    assert "Environment=LIVE_ORDERS=0" in text
    assert "--ws-readonly" in text
    assert "PartOf=spread-bbot-ev2-11-restart.service" in text
    assert "BindsTo=spread-bbot-ev2-11-restart.service" in text
    assert "/data/bbot-ev2-11-restart/private-%i" in text
    assert "LIVE_ORDERS=1" not in text
    assert "--w4-approve-one-shot" not in text
    assert "BBOT_PRIVATE_SEND_PATH" not in text

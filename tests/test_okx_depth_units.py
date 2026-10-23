from app.bot.theta_trade_manager import (
    ThetaTradeConfig,
    size_check,
    size_gate_enabled,
)


def _books():
    return (
        {"bid_price": 10, "ask_price": 10, "bid_size": 2, "ask_size": 2},
        {"bid_price": 10, "ask_price": 10, "bid_size": 20, "ask_size": 20},
    )


def test_no_ct_val_keeps_raw_l1_would_send_stub():
    """would_send stub: no ct_val / private gate → raw L1 size."""
    okx, bybit = _books()
    result = size_check(
        okx=okx, bybit=bybit, side="short", event="open", notional_usdt=1
    )
    assert result["okx_available_size"] == 2
    assert result["okx_available_contracts"] == 2
    assert result["okx_ct_val"] is None
    assert result["size_ok"]


def test_with_ct_val_converts_contracts_to_base():
    okx, bybit = _books()
    okx.update(_private_size_gate=True, ct_val=10)
    result = size_check(
        okx=okx, bybit=bybit, side="short", event="open", notional_usdt=15
    )
    # ask_size=2 contracts × ct_val=10 → 20 base; planned = 15/10 = 1.5
    assert result["okx_available_contracts"] == 2
    assert result["okx_ct_val"] == 10
    assert result["okx_available_size"] == 20
    assert result["size_ok"]


def test_okx_ct_val_kwarg_enables_private_gate_without_flag():
    okx, bybit = _books()
    result = size_check(
        okx=okx,
        bybit=bybit,
        side="short",
        event="open",
        notional_usdt=15,
        okx_ct_val=10,
    )
    assert result["okx_available_size"] == 20
    assert result["okx_ct_val"] == 10
    assert result["okx_available_contracts"] == 2
    assert result["size_ok"]


def test_insufficient_size_with_ct_val():
    okx, bybit = _books()
    # long open buys OKX ask — thin ask_size fails the gate
    okx.update(_private_size_gate=True, ct_val=10, ask_size=0.05)
    # available base = 0.05 * 10 = 0.5; planned = 15/10 = 1.5 → fail
    result = size_check(
        okx=okx, bybit=bybit, side="long", event="open", notional_usdt=15
    )
    assert result["okx_available_size"] == 0.5
    assert result["okx_available_contracts"] == 0.05
    assert result["okx_ct_val"] == 10
    assert not result["size_ok"]


def test_required_okx_contracts_for_close_from_fill():
    okx, bybit = _books()
    okx.update(_private_size_gate=True, ct_val=10)
    close_ok = size_check(
        okx=okx,
        bybit=bybit,
        side="short",
        event="close",
        notional_usdt=100,
        required_okx_contracts=1.5,
        required_bybit_qty=15,
    )
    assert close_ok["okx_planned_qty"] == 15.0  # 1.5 * 10
    assert close_ok["bybit_planned_qty"] == 15
    assert close_ok["size_ok"]

    okx["ask_size"] = 1
    insufficient_close = size_check(
        okx=okx,
        bybit=bybit,
        side="short",
        event="close",
        notional_usdt=100,
        required_okx_contracts=2,
        required_bybit_qty=2,
    )
    assert insufficient_close["okx_available_size"] == 10
    assert insufficient_close["okx_planned_qty"] == 20
    assert not insufficient_close["size_ok"]


def test_private_okx_depth_fails_closed_without_valid_multiplier():
    for ct_val in (None, 0, -1, "invalid"):
        okx, bybit = _books()
        okx.update(_private_size_gate=True, ct_val=ct_val)
        result = size_check(
            okx=okx, bybit=bybit, side="short", event="open", notional_usdt=1
        )
        assert result["okx_available_size"] is None
        assert result["okx_ct_val"] is None
        assert not result["size_ok"]


def test_non_private_okx_depth_keeps_existing_contract_behavior():
    okx, bybit = _books()
    result = size_check(
        okx=okx, bybit=bybit, side="short", event="open", notional_usdt=1
    )
    assert result["okx_available_size"] == 2
    assert result["okx_ct_val"] is None


def test_size_gate_disabled_forces_size_ok_keeps_sizes():
    """BBOT_SIZE_GATE=0 / size_gate=False: always size_ok, sizes still logged."""
    okx, bybit = _books()
    # Thin book would fail the live gate.
    okx = {**okx, "ask_size": 0.01, "bid_size": 0.01}
    bybit = {**bybit, "ask_size": 0.01, "bid_size": 0.01}
    blocked = size_check(
        okx=okx, bybit=bybit, side="long", event="open", notional_usdt=20
    )
    assert not blocked["size_ok"]
    assert blocked["size_gate"] is True
    assert blocked["size_ok_raw"] is False

    forced = size_check(
        okx=okx,
        bybit=bybit,
        side="long",
        event="open",
        notional_usdt=20,
        size_gate=False,
    )
    assert forced["size_ok"] is True
    assert forced["size_gate"] is False
    assert forced["size_ok_raw"] is False
    assert forced["okx_available_size"] == blocked["okx_available_size"]
    assert forced["bybit_available_size"] == blocked["bybit_available_size"]
    assert forced["okx_planned_qty"] == blocked["okx_planned_qty"]


def test_size_gate_enabled_env_flags():
    assert size_gate_enabled({}) is True
    assert size_gate_enabled({"BBOT_SIZE_GATE": "1"}) is True
    assert size_gate_enabled({"BBOT_SIZE_GATE": "0"}) is False
    assert size_gate_enabled({"BBOT_SKIP_SIZE_CHECK": "1"}) is False
    assert size_gate_enabled({"BBOT_SKIP_SIZE_CHECK": "0"}) is True
    # SKIP=1 wins over SIZE_GATE=1
    assert size_gate_enabled({"BBOT_SIZE_GATE": "1", "BBOT_SKIP_SIZE_CHECK": "1"}) is False
    cfg = ThetaTradeConfig.from_env({"BBOT_SIZE_GATE": "0", "BBOT_NOTIONAL_USDT": "20"})
    assert cfg.size_gate is False
    assert cfg.notional_usdt == 20.0
    cfg_live = ThetaTradeConfig.from_env({})
    assert cfg_live.size_gate is True

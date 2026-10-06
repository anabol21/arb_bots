from app.bot.theta_trade_manager import size_check


def _books():
    return (
        {"bid_price": 10, "ask_price": 10, "bid_size": 2, "ask_size": 2},
        {"bid_price": 10, "ask_price": 10, "bid_size": 20, "ask_size": 20},
    )


def test_private_okx_depth_converts_contracts_for_open_and_close():
    okx, bybit = _books()
    okx.update(_private_size_gate=True, ct_val=10)
    open_result = size_check(
        okx=okx, bybit=bybit, side="short", event="open", notional_usdt=15
    )
    close_result = size_check(
        okx=okx, bybit=bybit, side="short", event="close", notional_usdt=100,
        required_okx_contracts=1.5, required_bybit_qty=15,
    )
    assert open_result["okx_available_size"] == 20
    assert open_result["size_ok"]
    assert close_result["size_ok"]

    okx["ask_size"] = 1
    insufficient_close = size_check(
        okx=okx, bybit=bybit, side="short", event="close", notional_usdt=100,
        required_okx_contracts=2, required_bybit_qty=2,
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
        assert not result["size_ok"]


def test_non_private_okx_depth_keeps_existing_contract_behavior():
    okx, bybit = _books()
    result = size_check(
        okx=okx, bybit=bybit, side="short", event="open", notional_usdt=1
    )
    assert result["okx_available_size"] == 2

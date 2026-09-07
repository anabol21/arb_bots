"""Test for SOL close recovery bug.

This test reproduces the production issue where a filled non-reduce-only
dual-leg for SOL creates a synthetic exposure lease with the wrong symbol,
causing flatten_only close to abort with recovery_blocked.

Before the fix:
- _market_stub_plan hardcoded BTCUSDT / BTC-USDT-SWAP
- _profile_for_plan hardcoded TRUMPUSDT / TRUMP-USDT-SWAP
- SOL close plan vs BTC/TRUMP profile → symbol mismatch → recovery_blocked

After the fix:
- _market_stub_plan uses actual symbol from journal
- _profile_for_plan uses actual symbol from plan
- SOL close plan vs SOL profile → symbols match → recovery proceeds
"""

from __future__ import annotations

from app.bot.private.order_lease import ReconstructedLeg


def test_market_stub_plan_uses_sol_symbol_from_leg():
    """_market_stub_plan should use actual symbol from leg, not hardcoded BTC."""
    from app.bot.private.order_lease import LeaseSupervisor
    from pathlib import Path
    import tempfile
    from app.bot.private.journal_v1 import PrivateJournalWriter, new_opaque_id
    
    with tempfile.TemporaryDirectory() as tmpdir:
        data_root = Path(tmpdir)
        run_id = new_opaque_id("run")
        journal = PrivateJournalWriter(data_root, run_id=run_id)
        supervisor = LeaseSupervisor(journal=journal, data_root=data_root)
        
        # Create a SOL leg
        sol_leg = ReconstructedLeg(
            operation_id="op_sol",
            venue="bybit",
            environment="live",
            dual_leg_id="dual_sol",
            leg_id="leg_sol",
            request_fingerprint="fp_sol",
            post_only=False,
            ttl_bucket=None,
            acked=True,
            terminal=True,
            cancel_requested=False,
            dispatch_ambiguous=False,
            ttl_recovery_inconclusive=False,
            reduce_only=False,
            order_kind="market",
            request_sent=True,
            terminal_state="filled",
            side="buy",
            symbol="SOLUSDT",
        )
        
        # Call _market_stub_plan with SOL leg
        stub_plan = supervisor._market_stub_plan(sol_leg, op_id="op_sol")
        
        # KEY ASSERTION: stub should use SOL symbol, not BTC
        assert stub_plan.symbol == "SOLUSDT", \
            f"Stub plan should use SOL symbol from leg, not hardcoded BTC. Got: {stub_plan.symbol}"
        
        print("✓ _market_stub_plan correctly uses SOL symbol from leg")


def test_sol_close_flatten_only_profile_symbol_matches():
    """Flatten_only close with SOL symbols gets SOL profile, not TRUMP profile."""
    from app.bot.private.ws_w6_dual_leg import _profile_for_plan
    
    # Create a mock SOL plan
    class MockPlan:
        venue = "bybit_live"
        symbol = "SOLUSDT"
    
    plan = MockPlan()  # type: ignore[assignment]
    profile = _profile_for_plan(plan)
    
    # KEY ASSERTION: profile should match plan's SOL symbol, not hardcoded TRUMP
    assert profile["symbol"] == "SOLUSDT", \
        f"Profile should use plan's SOL symbol, not hardcoded TRUMP. Got: {profile['symbol']}"


def test_sol_close_recovery_not_blocked_by_symbol_mismatch():
    """SOL close recovery should not be blocked by symbol mismatch after the fix.
    
    This is the full scenario that reproduces the production bug:
    1. Filled non-reduce-only SOL dual-leg creates exposure lease with SOL symbol (after fix)
    2. Flatten_only close attempt with SOL symbol gets SOL profile (after fix)
    3. Recovery checks symbol match: SOL plan vs SOL profile → match → proceeds
    4. Before fix: SOL plan vs TRUMP profile → mismatch → recovery_blocked
    """
    from app.bot.private.ws_w6_dual_leg import _profile_for_plan
    
    # Simulate the close plan that live_broker would create for SOL flatten
    class MockSOLClosePlan:
        venue = "bybit_live"
        symbol = "SOLUSDT"
        dual_leg_id = "dual_sol_test"
    
    close_plan = MockSOLClosePlan()  # type: ignore[assignment]
    profile = _profile_for_plan(close_plan)
    
    # KEY ASSERTION: symbol match check (the one that was blocking in production)
    same_symbol = str(close_plan.symbol) == str(profile["symbol"])
    assert same_symbol, \
        f"Close plan symbol ({close_plan.symbol}) must match profile symbol ({profile['symbol']}) " \
        f"to avoid recovery_blocked. Before fix: close_plan=SOL vs profile=TRUMP → blocked. " \
        f"After fix: both should be SOL → not blocked."
    
    # Also verify OKX
    class MockOKXSOLClosePlan:
        venue = "okx_live"
        symbol = "SOL-USDT-SWAP"
        dual_leg_id = "dual_sol_test"
    
    okx_close_plan = MockOKXSOLClosePlan()  # type: ignore[assignment]
    okx_profile = _profile_for_plan(okx_close_plan)
    
    okx_same_symbol = str(okx_close_plan.symbol) == str(okx_profile["symbol"])
    assert okx_same_symbol, \
        f"OKX close plan symbol ({okx_close_plan.symbol}) must match profile symbol ({okx_profile['symbol']})"
    
    print("✓ SOL close recovery NOT blocked by symbol mismatch")


if __name__ == "__main__":
    test_market_stub_plan_uses_sol_symbol_from_leg()
    test_sol_close_flatten_only_profile_symbol_matches()
    test_sol_close_recovery_not_blocked_by_symbol_mismatch()
    print("\n✓ All tests passed!")

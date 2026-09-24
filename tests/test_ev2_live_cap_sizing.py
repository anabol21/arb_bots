"""CAP experiment sizing never rounds one live leg above the $10 cap."""

from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from app.bot.execution.live_cap_sizing import (
    CapOpenSizingInput, CapSizingError, plan_cap_matched_open,
)


def _current_shape() -> CapOpenSizingInput:
    # Deterministic fixture shaped like the 2026-09-24 public metadata.
    return CapOpenSizingInput(
        bybit_executable_price=Decimal("0.05014"),
        okx_executable_price=Decimal("0.05016"),
        bybit_l1_base_qty=Decimal("100"),
        okx_l1_contract_qty=Decimal("29"),
        okx_base_per_contract=Decimal("100"),
        okx_min_contract_qty=Decimal("1"),
        okx_contract_step=Decimal("1"),
        bybit_min_base_qty=Decimal("10"),
        bybit_base_step=Decimal("10"),
        bybit_min_notional_usdt=Decimal("5"),
    )


class CapSizingTests(unittest.TestCase):
    def test_two_contracts_exceed_cap_and_one_exactly_matches(self) -> None:
        plan = plan_cap_matched_open(_current_shape())
        self.assertEqual(plan.okx_contract_qty, Decimal("1"))
        self.assertEqual(plan.bybit_base_qty, Decimal("100"))
        self.assertEqual(plan.okx_intended_notional_usdt, Decimal("5.01600"))
        self.assertEqual(plan.bybit_intended_notional_usdt, Decimal("5.01400"))

    def test_two_contracts_when_both_sides_fit_under_ten(self) -> None:
        plan = plan_cap_matched_open(replace(
            _current_shape(),
            bybit_executable_price=Decimal("0.0499"),
            okx_executable_price=Decimal("0.0499"),
            bybit_l1_base_qty=Decimal("200"),
        ))
        self.assertEqual(plan.okx_contract_qty, Decimal("2"))
        self.assertEqual(plan.matched_base_qty, Decimal("200"))

    def test_thin_l1_never_rounds_up(self) -> None:
        with self.assertRaisesRegex(CapSizingError, "no_contract_within_cap_and_l1"):
            plan_cap_matched_open(replace(_current_shape(), bybit_l1_base_qty=Decimal("99")))

    def test_bybit_minimum_never_solved_by_unmatched_size(self) -> None:
        with self.assertRaisesRegex(CapSizingError, "bybit_min_notional_not_met"):
            plan_cap_matched_open(replace(
                _current_shape(),
                bybit_executable_price=Decimal("0.049"),
            ))

    def test_unknown_or_changed_steps_fail_closed(self) -> None:
        with self.assertRaisesRegex(CapSizingError, "bybit_size_not_exactly_matchable"):
            plan_cap_matched_open(replace(_current_shape(), bybit_base_step=Decimal("30")))
        with self.assertRaisesRegex(CapSizingError, "invalid_instrument_or_price"):
            replace(_current_shape(), okx_base_per_contract=Decimal("NaN"))


if __name__ == "__main__":
    unittest.main()

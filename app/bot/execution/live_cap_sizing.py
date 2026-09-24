"""Pure matched CAP size gate for the bounded EV2 live experiment.

This module does not fetch metadata, read credentials or send orders. Inputs
must come from fresh venue instrument metadata and executable L1 sides at the
decision boundary. The result is an *intended* notional cap, not a guarantee
about an unprotected market order's eventual fill price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR


class CapSizingError(ValueError):
    """No exactly matched CAP quantity satisfies both venues' constraints."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class CapOpenSizingInput:
    bybit_executable_price: Decimal
    okx_executable_price: Decimal
    bybit_l1_base_qty: Decimal
    okx_l1_contract_qty: Decimal
    okx_base_per_contract: Decimal
    okx_min_contract_qty: Decimal
    okx_contract_step: Decimal
    bybit_min_base_qty: Decimal
    bybit_base_step: Decimal
    bybit_min_notional_usdt: Decimal
    max_intended_notional_usdt: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        for name in (
            "bybit_executable_price", "okx_executable_price",
            "okx_base_per_contract", "okx_min_contract_qty",
            "okx_contract_step", "bybit_min_base_qty", "bybit_base_step",
            "bybit_min_notional_usdt", "max_intended_notional_usdt",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise CapSizingError("invalid_instrument_or_price")
        for name in ("bybit_l1_base_qty", "okx_l1_contract_qty"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise CapSizingError("invalid_l1_depth")
        if self.max_intended_notional_usdt != Decimal("10"):
            raise CapSizingError("ten_usdt_cap_required")


@dataclass(frozen=True)
class CapMatchedOpenSize:
    bybit_base_qty: Decimal
    okx_contract_qty: Decimal
    matched_base_qty: Decimal
    bybit_intended_notional_usdt: Decimal
    okx_intended_notional_usdt: Decimal


def plan_cap_matched_open(inp: CapOpenSizingInput) -> CapMatchedOpenSize:
    """Floor to an exact two-venue base quantity; never round either leg up."""
    if not isinstance(inp, CapOpenSizingInput):
        raise CapSizingError("invalid_sizing_input")
    max_contracts = min(
        inp.max_intended_notional_usdt
        / (inp.okx_base_per_contract * inp.bybit_executable_price),
        inp.max_intended_notional_usdt
        / (inp.okx_base_per_contract * inp.okx_executable_price),
        inp.bybit_l1_base_qty / inp.okx_base_per_contract,
        inp.okx_l1_contract_qty,
    )
    contracts = (
        (max_contracts / inp.okx_contract_step).to_integral_value(rounding=ROUND_FLOOR)
        * inp.okx_contract_step
    )
    if contracts < inp.okx_min_contract_qty:
        raise CapSizingError("no_contract_within_cap_and_l1")
    base = contracts * inp.okx_base_per_contract
    if base < inp.bybit_min_base_qty or base % inp.bybit_base_step != 0:
        raise CapSizingError("bybit_size_not_exactly_matchable")
    bybit_notional = base * inp.bybit_executable_price
    okx_notional = base * inp.okx_executable_price
    if bybit_notional < inp.bybit_min_notional_usdt:
        raise CapSizingError("bybit_min_notional_not_met")
    if (
        bybit_notional > inp.max_intended_notional_usdt
        or okx_notional > inp.max_intended_notional_usdt
    ):
        raise CapSizingError("intended_notional_exceeds_cap")
    return CapMatchedOpenSize(
        bybit_base_qty=base,
        okx_contract_qty=contracts,
        matched_base_qty=base,
        bybit_intended_notional_usdt=bybit_notional,
        okx_intended_notional_usdt=okx_notional,
    )

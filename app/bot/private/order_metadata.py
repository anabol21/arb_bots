"""Injectable instrument metadata for R3 sizing (min lot / tick / mark).

No hardcoded order quantities. Market notional uses explicit mark + contract
unit semantics — never min-notional/min-qty inference.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional, Protocol


class MetadataError(ValueError):
    """Instrument metadata missing or inconsistent."""


NotionalUnit = Literal["usdt_per_coin", "usdt_per_contract"]


def parse_inst_id_code(raw: object) -> Optional[int]:
    """Parse OKX ``instIdCode`` to a positive int, else None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        if not raw.is_integer():
            return None
        v = int(raw)
        return v if v > 0 else None
    s = str(raw).strip()
    if s.isdigit():
        v = int(s)
        return v if v > 0 else None
    return None


@dataclass(frozen=True)
class InstrumentMetadata:
    """Validated live futures instrument parameters + current mark.

    Unit semantics:
    - Bybit linear USDT: qty in coins; notional = qty * mark_price_usdt
      (contract_multiplier=1, notional_unit=usdt_per_coin).
    - OKX USDT SWAP: qty in contracts; notional =
      qty * contract_multiplier(ctVal) * mark_price_usdt when ctValCcy in
      {USD, USDT} (notional_unit=usdt_per_contract).
    """

    venue: str
    symbol: str
    min_qty: Decimal
    qty_step: Decimal
    tick_size: Decimal
    contract_multiplier: Decimal
    contract_value_ccy: str  # USDT | USD
    notional_unit: NotionalUnit
    mark_price_usdt: Decimal
    mark_asof_monotonic_ns: int
    mark_max_age_ns: int = 5_000_000_000
    # OKX public instruments instIdCode — required for okx_live W4 WS place/cancel.
    inst_id_code: Optional[int] = None

    def __post_init__(self) -> None:
        for name in (
            "min_qty",
            "qty_step",
            "tick_size",
            "contract_multiplier",
            "mark_price_usdt",
        ):
            val = getattr(self, name)
            if not isinstance(val, Decimal):
                raise MetadataError(f"{name} must be Decimal")
            if val <= 0:
                raise MetadataError(f"{name} must be > 0")
        ccy = str(self.contract_value_ccy).upper()
        if ccy not in {"USDT", "USD"}:
            raise MetadataError("contract_value_ccy must be USDT or USD")
        if self.notional_unit not in {"usdt_per_coin", "usdt_per_contract"}:
            raise MetadataError("invalid notional_unit")
        if int(self.mark_asof_monotonic_ns) < 0:
            raise MetadataError("invalid mark_asof_monotonic_ns")
        if int(self.mark_max_age_ns) <= 0:
            raise MetadataError("mark_max_age_ns must be > 0")
        if self.inst_id_code is not None:
            if (
                not isinstance(self.inst_id_code, int)
                or isinstance(self.inst_id_code, bool)
                or self.inst_id_code <= 0
            ):
                raise MetadataError("inst_id_code must be a positive int when set")

    def assert_mark_fresh(self, *, now_mono_ns: int | None = None) -> None:
        now = now_mono_ns if now_mono_ns is not None else time.monotonic_ns()
        age = now - int(self.mark_asof_monotonic_ns)
        # Negative age can occur with injected test clocks older than mark as-of.
        if age > int(self.mark_max_age_ns):
            raise MetadataError("mark price stale or incomplete")

    def market_notional_usdt(self, qty: Decimal) -> Decimal:
        """Explicit mark/contract notional — never infers from min notional."""
        if qty <= 0:
            raise MetadataError("qty must be positive for notional")
        if self.notional_unit == "usdt_per_coin":
            return qty * self.mark_price_usdt
        return qty * self.contract_multiplier * self.mark_price_usdt


class MetadataProvider(Protocol):
    def get(self, venue: str, symbol: str) -> InstrumentMetadata:
        """Return live metadata for an allowlisted futures symbol."""


@dataclass(frozen=True)
class StaticMetadataProvider:
    """Test/dev provider: explicit table only — never invents sizes."""

    table: dict[tuple[str, str], InstrumentMetadata]

    def get(self, venue: str, symbol: str) -> InstrumentMetadata:
        key = (venue, symbol)
        if key not in self.table:
            raise MetadataError(f"no metadata for {venue}/{symbol}")
        meta = self.table[key]
        meta.assert_mark_fresh()
        return meta


def parse_decimal(raw: str | Decimal | None, *, field: str) -> Decimal:
    if raw is None:
        raise MetadataError(f"missing decimal for {field}")
    try:
        if isinstance(raw, Decimal):
            val = raw
        else:
            val = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise MetadataError(f"invalid decimal for {field}") from exc
    if val.is_nan() or val.is_infinite():
        raise MetadataError(f"invalid decimal for {field}")
    return val

"""EV2 frozen-frame to authenticated trade-WS text; no socket or order I/O.

This adapter only constructs messages. The live runtime guard remains armed
until fill-driven ingestion, reconciliation and bounded experiment ownership
are integrated. OKX here supports net position mode only; account-mode
preflight must prove that before any caller may use the returned frame.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.bot.execution.contracts import Venue, validate_client_id
from app.bot.execution.transport import FrozenStaticFrame, TransportError
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.ws_messages import (
    build_bybit_trade_place_fields,
    build_okx_trade_place_fields,
)


class Ev2LiveWsFinalizer:
    """Injectable ExecutionTransport finalizer, deliberately not an arm gate."""

    __slots__ = ("_bybit_credentials",)

    def __init__(self, *, bybit_credentials: LiveCredentials) -> None:
        if not isinstance(bybit_credentials, LiveCredentials):
            raise TransportError("rejected_before_write")
        self._bybit_credentials = bybit_credentials

    def __repr__(self) -> str:
        return "Ev2LiveWsFinalizer(credentials=redacted)"

    def __call__(
        self,
        static: FrozenStaticFrame,
        *,
        timestamp_ms: int,
        request_id: str,
        client_id: str,
    ) -> str:
        if not isinstance(static, FrozenStaticFrame):
            raise TransportError("rejected_before_write")
        if (
            not isinstance(timestamp_ms, int)
            or isinstance(timestamp_ms, bool)
            or timestamp_ms <= 0
        ):
            raise TransportError("clock_regression")
        if request_id != client_id or client_id != static.client_id:
            raise TransportError("client_id_mismatch")
        if static.side not in {"buy", "sell"} or not isinstance(static.reduce_only, bool):
            raise TransportError("invalid_leg_set")
        try:
            quantity = Decimal(static.quantity)
        except (InvalidOperation, TypeError, ValueError):
            raise TransportError("invalid_leg_set") from None
        if not quantity.is_finite() or quantity <= 0:
            raise TransportError("invalid_leg_set")
        try:
            validate_client_id(client_id, static.venue)
            if static.venue is Venue.BYBIT:
                return build_bybit_trade_place_fields(
                    symbol=static.instrument,
                    side=static.side,
                    quantity=static.quantity,
                    client_id=client_id,
                    mode="market",
                    price=None,
                    reduce_only=static.reduce_only,
                    credentials=self._bybit_credentials,
                    req_id=request_id,
                    timestamp_ms=timestamp_ms,
                ).text
            if static.venue is Venue.OKX:
                return build_okx_trade_place_fields(
                    symbol=static.instrument,
                    side=static.side,
                    quantity=static.quantity,
                    client_id=client_id,
                    mode="market",
                    price=None,
                    reduce_only=static.reduce_only,
                    position_side=None,
                    req_id=request_id,
                    inst_id_code=static.inst_id_code,
                ).text
        except ValueError:
            raise TransportError("rejected_before_write") from None
        raise TransportError("invalid_leg_set")

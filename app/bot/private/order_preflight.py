"""Live instrument metadata + position-mode preflight (fail-closed).

Default/test code must not invoke LiveHttpMetadataProvider. Tests use
StaticMetadataProvider with explicit mark/contract units.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from app.bot.private.order_metadata import (
    InstrumentMetadata,
    MetadataError,
    MetadataProvider,
    parse_decimal,
    parse_inst_id_code,
)
from app.bot.private.order_symbols import assert_order_venue, resolve_allowed_futures_symbol


class PreflightError(RuntimeError):
    """Position-mode / preflight verification failed."""


@dataclass(frozen=True)
class PositionModeSnapshot:
    venue: str
    mode: str  # e.g. one_way | hedge
    verified: bool


class PositionModeProvider(Protocol):
    def get(self, venue: str) -> PositionModeSnapshot:
        ...


@dataclass(frozen=True)
class FailClosedPositionModeProvider:
    """Default: never claims a verified position mode."""

    def get(self, venue: str) -> PositionModeSnapshot:
        assert_order_venue(venue)
        raise PreflightError(
            "position mode not verified; refuse without verified provider"
        )


@dataclass(frozen=True)
class StaticVerifiedPositionModeProvider:
    """Test-only verified mode provider."""

    mode_by_venue: Mapping[str, str]

    def get(self, venue: str) -> PositionModeSnapshot:
        assert_order_venue(venue)
        mode = self.mode_by_venue.get(venue)
        if mode is None:
            raise PreflightError("position mode missing for venue")
        return PositionModeSnapshot(venue=venue, mode=mode, verified=True)


HttpGetJson = Callable[[str, Mapping[str, str]], Mapping[str, Any]]


@dataclass(frozen=True)
class LiveHttpMetadataProvider:
    """Read-only live instrument + mark via injected HTTP getter.

    Never constructed by default CLI/tests. Validates Bybit linear USDT and
    OKX SWAP USDT with explicit ctVal/ctValCcy and mark — never min-notional
    inference.
    """

    http_get_json: HttpGetJson
    bybit_base: str = "https://api.bybit.com"
    okx_base: str = "https://www.okx.com"
    mark_max_age_ns: int = 5_000_000_000

    def get(self, venue: str, symbol: str) -> InstrumentMetadata:
        allowed = resolve_allowed_futures_symbol(venue, symbol)
        if allowed.venue == "bybit_live":
            return self._bybit_linear(allowed.symbol)
        if allowed.venue == "okx_live":
            return self._okx_swap_usdt(allowed.symbol)
        raise MetadataError(f"unsupported venue {venue}")

    def _bybit_linear(self, symbol: str) -> InstrumentMetadata:
        info_url = (
            f"{self.bybit_base}/v5/market/instruments-info"
            f"?category=linear&symbol={symbol}"
        )
        data = self.http_get_json(info_url, {"Accept": "application/json"})
        if data.get("retCode") != 0 and str(data.get("retCode")) != "0":
            raise MetadataError("bybit instruments-info rejected")
        rows = ((data.get("result") or {}).get("list")) or []
        if not rows:
            raise MetadataError("bybit instruments-info empty")
        row = rows[0]
        if str(row.get("symbol")) != symbol:
            raise MetadataError("bybit symbol mismatch")
        settle = str(row.get("settleCoin") or row.get("quoteCoin") or "").upper()
        if settle != "USDT":
            raise MetadataError("bybit instrument must settle USDT")
        status = str(row.get("status") or "").lower()
        if status and status not in {"trading", "listed"}:
            raise MetadataError("bybit instrument not trading")
        lot = row.get("lotSizeFilter") or {}
        price = row.get("priceFilter") or {}
        min_qty = parse_decimal(lot.get("minOrderQty") or lot.get("minTradingQty"), field="min_qty")
        step = parse_decimal(lot.get("qtyStep"), field="qty_step")
        tick = parse_decimal(price.get("tickSize"), field="tick_size")

        ticker_url = (
            f"{self.bybit_base}/v5/market/tickers?category=linear&symbol={symbol}"
        )
        tdata = self.http_get_json(ticker_url, {"Accept": "application/json"})
        if tdata.get("retCode") != 0 and str(tdata.get("retCode")) != "0":
            raise MetadataError("bybit ticker rejected")
        trows = ((tdata.get("result") or {}).get("list")) or []
        if not trows:
            raise MetadataError("bybit ticker empty")
        mark = parse_decimal(trows[0].get("markPrice"), field="mark_price_usdt")
        now = time.monotonic_ns()
        return InstrumentMetadata(
            venue="bybit_live",
            symbol=symbol,
            min_qty=min_qty,
            qty_step=step,
            tick_size=tick,
            contract_multiplier=parse_decimal("1", field="contract_multiplier"),
            contract_value_ccy="USDT",
            notional_unit="usdt_per_coin",
            mark_price_usdt=mark,
            mark_asof_monotonic_ns=now,
            mark_max_age_ns=self.mark_max_age_ns,
        )

    def _okx_swap_usdt(self, symbol: str) -> InstrumentMetadata:
        from app.bot.private.rest_readonly import okx_public_rest_headers

        pub = okx_public_rest_headers()
        url = f"{self.okx_base}/api/v5/public/instruments?instType=SWAP&instId={symbol}"
        data = self.http_get_json(url, pub)
        if str(data.get("code")) != "0":
            raise MetadataError("okx instruments rejected")
        rows = data.get("data") or []
        if not rows:
            raise MetadataError("okx instruments empty")
        row = rows[0]
        if str(row.get("instId")) != symbol:
            raise MetadataError("okx instId mismatch")
        if str(row.get("instType")) != "SWAP":
            raise MetadataError("okx instrument must be SWAP")
        settle = str(row.get("settleCcy") or "").upper()
        if settle != "USDT":
            raise MetadataError("okx SWAP must settle USDT")
        # USDT-settled linear SWAP: ctVal is base-coin size per contract (often BTC);
        # notional USDT = qty * ctVal * mark. Store settle ccy as contract_value_ccy.
        if "ctVal" not in row or row.get("ctVal") in (None, ""):
            raise MetadataError("okx ctVal missing")
        ct_val = parse_decimal(row.get("ctVal"), field="ct_val")
        min_qty = parse_decimal(row.get("minSz"), field="min_qty")
        step = parse_decimal(row.get("lotSz") or row.get("minSz"), field="qty_step")
        tick = parse_decimal(row.get("tickSz"), field="tick_size")

        turl = f"{self.okx_base}/api/v5/market/ticker?instId={symbol}"
        tdata = self.http_get_json(turl, pub)
        if str(tdata.get("code")) != "0":
            raise MetadataError("okx ticker rejected")
        trows = tdata.get("data") or []
        if not trows:
            raise MetadataError("okx ticker empty")
        mark_raw = trows[0].get("markPx")
        if mark_raw in (None, ""):
            # Live ticker may omit markPx; public mark-price is authoritative.
            murl = (
                f"{self.okx_base}/api/v5/public/mark-price"
                f"?instType=SWAP&instId={symbol}"
            )
            mdata = self.http_get_json(murl, pub)
            if str(mdata.get("code")) != "0":
                raise MetadataError("okx mark-price rejected")
            mrows = mdata.get("data") or []
            if not mrows or mrows[0].get("markPx") in (None, ""):
                raise MetadataError("okx mark price missing")
            mark_raw = mrows[0].get("markPx")
        mark = parse_decimal(mark_raw, field="mark_price_usdt")
        now = time.monotonic_ns()
        inst_code = parse_inst_id_code(row.get("instIdCode"))
        return InstrumentMetadata(
            venue="okx_live",
            symbol=symbol,
            min_qty=min_qty,
            qty_step=step,
            tick_size=tick,
            contract_multiplier=ct_val,
            contract_value_ccy="USDT",
            notional_unit="usdt_per_contract",
            mark_price_usdt=mark,
            mark_asof_monotonic_ns=now,
            mark_max_age_ns=self.mark_max_age_ns,
            inst_id_code=inst_code,
        )


@dataclass(frozen=True)
class LiveSignedPositionModeProvider:
    """W4-only: verify position mode via signed GET (never constructed by default CLI).

    OKX: ``/api/v5/account/config`` ``posMode``.
    Bybit: ``/v5/position/list`` positionIdx → one_way vs hedge.
    """

    exchange: str  # bybit | okx
    credentials: Any  # LiveCredentials — avoid circular import at type time
    bybit_base: str = "https://api.bybit.com"
    okx_base: str = "https://www.okx.com"
    symbol: str = "BTCUSDT"

    def get(self, venue: str) -> PositionModeSnapshot:
        assert_order_venue(venue)
        if self.exchange == "bybit" and venue == "bybit_live":
            return self._bybit()
        if self.exchange == "okx" and venue == "okx_live":
            return self._okx()
        raise PreflightError("position mode venue/exchange mismatch")

    def _bybit(self) -> PositionModeSnapshot:
        import hashlib
        import hmac
        import json
        import time
        import urllib.request

        path = "/v5/position/list"
        query = f"category=linear&symbol={self.symbol}&settleCoin=USDT"
        ts = str(int(time.time() * 1000))
        recv = "5000"
        payload = f"{ts}{self.credentials.api_key}{recv}{query}"
        sign = hmac.new(
            self.credentials.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{self.bybit_base}{path}?{query}"
        req = urllib.request.Request(
            url,
            headers={
                "X-BAPI-API-KEY": self.credentials.api_key,
                "X-BAPI-SIGN": sign,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": recv,
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15.0) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("retCode") not in (0, "0"):
            raise PreflightError("bybit position mode GET rejected")
        rows = ((data.get("result") or {}).get("list")) or []
        hedge = False
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                idx = str(row.get("positionIdx", "0"))
                if idx in {"1", "2"}:
                    hedge = True
                    break
        mode = "hedge" if hedge else "one_way"
        return PositionModeSnapshot(venue="bybit_live", mode=mode, verified=True)

    def _okx(self) -> PositionModeSnapshot:
        import json
        import urllib.request

        from app.bot.private.rest_readonly import (
            assert_okx_headers_for_venue,
            build_okx_readonly_headers,
        )

        if not getattr(self.credentials, "passphrase", None):
            raise PreflightError("okx position mode requires passphrase")
        path = "/api/v5/account/config"
        headers = build_okx_readonly_headers(
            api_key=self.credentials.api_key,
            api_secret=self.credentials.api_secret,
            passphrase=self.credentials.passphrase,
            path=path,
            simulated_trading=False,
        )
        assert_okx_headers_for_venue(headers, "live")
        req = urllib.request.Request(
            f"{self.okx_base}{path}",
            headers=headers,
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15.0) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        if str(data.get("code", "")) != "0":
            raise PreflightError("okx position mode GET rejected")
        rows = data.get("data") or []
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise PreflightError("okx position mode missing")
        pos_mode = str(rows[0].get("posMode") or "").lower()
        if pos_mode in {"net_mode", "net"}:
            mode = "one_way"
        elif pos_mode in {"long_short_mode", "long_short", "hedge"}:
            mode = "hedge"
        else:
            raise PreflightError("okx position mode unrecognized")
        return PositionModeSnapshot(venue="okx_live", mode=mode, verified=True)


def assert_preflight_ready(
    *,
    metadata_provider: MetadataProvider,
    position_mode_provider: PositionModeProvider,
    venue: str,
    symbol: str,
    now_mono_ns: int | None = None,
) -> InstrumentMetadata:
    """Fail-closed unless metadata + verified position mode are available."""
    if metadata_provider is None or position_mode_provider is None:
        raise PreflightError("metadata/position providers required")
    try:
        meta = metadata_provider.get(venue, symbol)
    except MetadataError as exc:
        raise PreflightError(str(exc)) from exc
    try:
        meta.assert_mark_fresh(now_mono_ns=now_mono_ns)
    except MetadataError as exc:
        raise PreflightError(str(exc)) from exc
    if meta.mark_price_usdt <= 0 or meta.contract_multiplier <= 0:
        raise PreflightError("incomplete instrument mark/contract value")
    mode = position_mode_provider.get(venue)
    if not mode.verified:
        raise PreflightError("position mode snapshot not verified")
    return meta

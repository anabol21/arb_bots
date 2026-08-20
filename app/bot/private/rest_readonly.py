"""Read-only REST account probes for Bybit and OKX (testnet/demo or live).

No place/cancel/amend methods. No private WS. GET only. Stdlib only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.request import Request

from app.bot.private.venue import VenueEndpoints

# Hard deny: if a caller somehow builds these paths, refuse.
_FORBIDDEN_PATH_FRAGMENTS = (
    "/order",
    "/orders",
    "place-order",
    "cancel",
    "amend",
    "batch-order",
    "position/set",
    "trade/",
)

NORMALIZED_OUTCOMES = frozenset(
    {
        "ok",
        "auth_rejected",
        "auth_forbidden",
        "network_error",
        "malformed_response",
        "unknown_error",
    }
)

# Secret-like header names must never appear in public records.
_SECRET_HEADER_NAMES = frozenset(
    {
        "OK-ACCESS-KEY",
        "OK-ACCESS-SIGN",
        "OK-ACCESS-PASSPHRASE",
        "OK-ACCESS-TIMESTAMP",
        "X-BAPI-API-KEY",
        "X-BAPI-SIGN",
        "X-BAPI-TIMESTAMP",
        "X-BAPI-RECV-WINDOW",
        "Authorization",
    }
)

# OKX edge rejects urllib default / missing UA with bare HTTP 403 (non-JSON).
# Fixed benign identity only — never logs request/response bodies.
OKX_REST_USER_AGENT = "spread-bbot-private/0"
OKX_REST_ACCEPT = "application/json"


def okx_public_rest_headers() -> dict[str, str]:
    """Benign public OKX GET headers — same UA/Accept as R1 live signed path."""
    return {
        "Accept": OKX_REST_ACCEPT,
        "User-Agent": OKX_REST_USER_AGENT,
    }


@dataclass(frozen=True)
class AccountProbeResult:
    exchange: str
    venue: str
    endpoint: str
    path: str
    ok: bool
    http_status: Optional[int]
    exchange_code: Optional[str]
    outcome: str
    equity_usdt: Optional[float]
    orders_sent: int = 0
    okx_simulated_trading: Optional[bool] = None

    def as_public_dict(self) -> dict[str, Any]:
        """Public probe fields only — no raw message, headers, or body."""
        if self.outcome not in NORMALIZED_OUTCOMES:
            raise RuntimeError(f"invalid normalized outcome: {self.outcome!r}")
        out: dict[str, Any] = {
            "exchange": self.exchange,
            "venue": self.venue,
            "endpoint": self.endpoint,
            "path": self.path,
            "ok": self.ok,
            "http_status": self.http_status,
            "exchange_code": self.exchange_code,
            "outcome": self.outcome,
            "orders_sent": self.orders_sent,
            # Presence only — never the equity value in journal (account shape).
            "equity_present": self.equity_usdt is not None,
        }
        if self.okx_simulated_trading is not None:
            out["okx_simulated_trading"] = self.okx_simulated_trading
        return out


def _assert_path_readonly(path: str) -> None:
    lowered = path.lower()
    for frag in _FORBIDDEN_PATH_FRAGMENTS:
        if frag in lowered:
            raise RuntimeError(f"refusing non-readonly path fragment {frag!r} in {path}")


def normalize_http_outcome(
    *,
    http_status: Optional[int],
    exchange_code: Optional[str],
    ok: bool,
) -> str:
    """Map status/code to the public allowlist. Never embeds raw text."""
    if ok:
        return "ok"
    if http_status in {401, 407}:
        return "auth_rejected"
    if http_status == 403:
        return "auth_forbidden"
    # Common venue auth codes (Bybit / OKX) without echoing messages.
    code = (exchange_code or "").strip()
    if code in {"10003", "10004", "10005", "50111", "50113", "50114", "50119"}:
        return "auth_rejected"
    if code in {"10006", "10018", "50110"}:
        return "auth_forbidden"
    if http_status is not None and http_status >= 500:
        return "network_error"
    if http_status is not None and http_status >= 400:
        return "auth_rejected"
    return "unknown_error"


def _http_get(
    url: str,
    headers: Mapping[str, str],
    *,
    timeout_sec: float = 15.0,
) -> tuple[int, dict[str, Any]]:
    # Defense: never allow mutating methods from this helper.
    req = Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", None) or resp.getcode()
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError as exc:
                raise ValueError("malformed_response") from exc
            if not isinstance(data, dict):
                raise ValueError("malformed_response")
            return int(status), data
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return int(exc.code), data


def build_okx_readonly_headers(
    *,
    api_key: str,
    api_secret: str,
    passphrase: str,
    path: str,
    simulated_trading: bool,
    timestamp: Optional[str] = None,
) -> dict[str, str]:
    """Build OKX GET headers. Live must NOT set x-simulated-trading."""
    ts = timestamp or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    sign = _okx_sign(
        secret=api_secret,
        timestamp=ts,
        method="GET",
        request_path=path,
    )
    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "Accept": OKX_REST_ACCEPT,
        "User-Agent": OKX_REST_USER_AGENT,
    }
    if simulated_trading:
        headers["x-simulated-trading"] = "1"
    return headers


def assert_okx_headers_for_venue(headers: Mapping[str, str], venue: str) -> None:
    """Fail-closed header routing: demo header only on testnet."""
    sim_present = any(k.lower() == "x-simulated-trading" for k in headers)
    if venue == "live" and sim_present:
        raise RuntimeError("live OKX headers must not include x-simulated-trading")
    if venue == "testnet" and not sim_present:
        raise RuntimeError("testnet/demo OKX headers require x-simulated-trading")


def probe_bybit_wallet(
    *,
    api_key: str,
    api_secret: str,
    endpoints: VenueEndpoints,
    recv_window: int = 5000,
    timeout_sec: float = 15.0,
) -> AccountProbeResult:
    """GET wallet balance on Bybit (testnet or mainnet per endpoints.venue)."""
    if endpoints.venue not in {"testnet", "live"}:
        raise RuntimeError(f"bybit probe refuses venue={endpoints.venue!r}")
    if endpoints.venue == "live" and "testnet" in endpoints.bybit_rest:
        raise RuntimeError("live bybit probe refuses testnet REST base")
    if endpoints.venue == "testnet" and endpoints.bybit_rest == "https://api.bybit.com":
        raise RuntimeError("testnet bybit probe refuses live REST base")
    path = endpoints.bybit_account_path
    _assert_path_readonly(path)
    query = "accountType=UNIFIED"
    ts = str(int(time.time() * 1000))
    payload = f"{ts}{api_key}{recv_window}{query}"
    sign = hmac.new(
        api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = f"{endpoints.bybit_rest}{path}?{query}"
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": str(recv_window),
        "Content-Type": "application/json",
    }
    try:
        status, data = _http_get(url, headers, timeout_sec=timeout_sec)
    except ValueError:
        return AccountProbeResult(
            exchange="bybit",
            venue=endpoints.venue,
            endpoint=endpoints.bybit_rest,
            path=f"{path}?accountType=UNIFIED",
            ok=False,
            http_status=None,
            exchange_code=None,
            outcome="malformed_response",
            equity_usdt=None,
            orders_sent=0,
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        return AccountProbeResult(
            exchange="bybit",
            venue=endpoints.venue,
            endpoint=endpoints.bybit_rest,
            path=f"{path}?accountType=UNIFIED",
            ok=False,
            http_status=None,
            exchange_code=None,
            outcome="network_error",
            equity_usdt=None,
            orders_sent=0,
        )
    ret_code = data.get("retCode")
    code_s = None if ret_code is None else str(ret_code)
    ok = status == 200 and ret_code == 0
    equity: Optional[float] = None
    if ok:
        try:
            lst = (data.get("result") or {}).get("list") or []
            if lst:
                equity = float(lst[0].get("totalEquity") or 0.0)
        except (TypeError, ValueError, IndexError, AttributeError):
            equity = None
    return AccountProbeResult(
        exchange="bybit",
        venue=endpoints.venue,
        endpoint=endpoints.bybit_rest,
        path=f"{path}?accountType=UNIFIED",
        ok=ok,
        http_status=status,
        exchange_code=code_s,
        outcome=normalize_http_outcome(
            http_status=status, exchange_code=code_s, ok=ok
        ),
        equity_usdt=equity,
        orders_sent=0,
    )


def _okx_sign(
    *,
    secret: str,
    timestamp: str,
    method: str,
    request_path: str,
    body: str = "",
) -> str:
    import base64

    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(
        secret.encode("utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def probe_okx_balance(
    *,
    api_key: str,
    api_secret: str,
    passphrase: str,
    endpoints: VenueEndpoints,
    timeout_sec: float = 15.0,
) -> AccountProbeResult:
    """GET account balance on OKX demo (simulated) or live (no simulated header)."""
    if endpoints.venue not in {"testnet", "live"}:
        raise RuntimeError(f"okx probe refuses venue={endpoints.venue!r}")
    if endpoints.venue == "testnet" and not endpoints.okx_simulated_trading:
        raise RuntimeError("testnet/demo OKX probe requires x-simulated-trading flag")
    if endpoints.venue == "live" and endpoints.okx_simulated_trading:
        raise RuntimeError("live OKX probe refuses simulated-trading flag")
    path = endpoints.okx_account_path
    _assert_path_readonly(path)
    headers = build_okx_readonly_headers(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        path=path,
        simulated_trading=endpoints.okx_simulated_trading,
    )
    assert_okx_headers_for_venue(headers, endpoints.venue)
    url = f"{endpoints.okx_rest}{path}"
    try:
        status, data = _http_get(url, headers, timeout_sec=timeout_sec)
    except ValueError:
        return AccountProbeResult(
            exchange="okx",
            venue="demo" if endpoints.okx_simulated_trading else endpoints.venue,
            endpoint=endpoints.okx_rest,
            path=path,
            ok=False,
            http_status=None,
            exchange_code=None,
            outcome="malformed_response",
            equity_usdt=None,
            orders_sent=0,
            okx_simulated_trading=endpoints.okx_simulated_trading,
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        return AccountProbeResult(
            exchange="okx",
            venue="demo" if endpoints.okx_simulated_trading else endpoints.venue,
            endpoint=endpoints.okx_rest,
            path=path,
            ok=False,
            http_status=None,
            exchange_code=None,
            outcome="network_error",
            equity_usdt=None,
            orders_sent=0,
            okx_simulated_trading=endpoints.okx_simulated_trading,
        )
    code = data.get("code")
    code_s = None if code is None else str(code)
    ok = status == 200 and str(code) == "0"
    equity: Optional[float] = None
    if ok:
        try:
            details = (data.get("data") or [{}])[0]
            total_eq = details.get("totalEq")
            if total_eq is not None and str(total_eq) != "":
                equity = float(total_eq)
        except (TypeError, ValueError, IndexError, AttributeError):
            equity = None
    return AccountProbeResult(
        exchange="okx",
        venue="demo" if endpoints.okx_simulated_trading else endpoints.venue,
        endpoint=endpoints.okx_rest,
        path=path,
        ok=ok,
        http_status=status,
        exchange_code=code_s,
        outcome=normalize_http_outcome(
            http_status=status, exchange_code=code_s, ok=ok
        ),
        equity_usdt=equity,
        orders_sent=0,
        okx_simulated_trading=endpoints.okx_simulated_trading,
    )


def assert_no_order_methods() -> None:
    """Self-check: this module must not expose order helpers."""
    forbidden_names = {
        "place_order",
        "cancel_order",
        "amend_order",
        "send_order",
        "create_order",
    }
    present = forbidden_names.intersection(globals())
    if present:
        raise RuntimeError(f"order helpers must not exist: {sorted(present)}")


def assert_get_only_http_helper() -> None:
    """Ensure _http_get hard-codes GET (no POST/PUT/DELETE surface)."""
    import inspect

    src = inspect.getsource(_http_get)
    if 'method="GET"' not in src and "method='GET'" not in src:
        raise RuntimeError("_http_get must hard-code GET")
    for bad in ('"POST"', "'POST'", '"PUT"', "'PUT'", '"DELETE"', "'DELETE'"):
        if bad in src:
            raise RuntimeError(f"_http_get must not reference {bad}")


def scrub_public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Drop forbidden keys from a public journal/report dict (shallow+nested)."""
    forbidden = {
        "secret_source",
        "secret_file",
        "BBOT_PRIVATE_ENV_FILE",
        "source_path",
        "api_key",
        "api_secret",
        "passphrase",
        "message",
        "error",
        "raw",
        "headers",
        "authorization",
        "signature",
        "sign",
        "account_id",
        "uid",
        "equity_usdt",
    }
    forbidden_lower = {k.lower() for k in forbidden} | {
        h.lower() for h in _SECRET_HEADER_NAMES
    }

    def _walk(obj: Any) -> Any:
        if isinstance(obj, Mapping):
            out: dict[str, Any] = {}
            for k, v in obj.items():
                if str(k).lower() in forbidden_lower:
                    continue
                if str(k).lower().endswith(("_path", "_file")) and "secret" in str(k).lower():
                    continue
                out[str(k)] = _walk(v)
            return out
        if isinstance(obj, list):
            return [_walk(x) for x in obj]
        return obj

    return _walk(dict(record))

"""In-memory private-leg readiness. Missing key is down (fail closed).

Writers are the private socket lifecycle (login + subscription). Readers are
``send_long`` / ``send_short``. No readiness RPC and no journal read.
"""

from __future__ import annotations

import threading
from typing import Sequence

_LOCK = threading.Lock()
_UP: dict[tuple[str, str], bool] = {}


def _key(exchange: str, coin: str) -> tuple[str, str]:
    return (str(exchange).strip().lower(), str(coin).strip().upper())


def leg_up(exchange: str, coin: str) -> bool:
    """True only when this process has marked ``(exchange, coin)`` up."""
    with _LOCK:
        return bool(_UP.get(_key(exchange, coin), False))


def set_exchange_coins(exchange: str, coins: Sequence[str], up: bool) -> None:
    """Set every listed coin on ``exchange``. Down clears the whole exchange."""
    ex = str(exchange).strip().lower()
    wanted = {str(c).strip().upper() for c in coins if str(c).strip()}
    with _LOCK:
        if not up:
            for key in list(_UP):
                if key[0] == ex:
                    _UP[key] = False
            for coin in wanted:
                _UP[(ex, coin)] = False
            return
        for key in list(_UP):
            if key[0] == ex and key[1] not in wanted:
                _UP[key] = False
        for coin in wanted:
            _UP[(ex, coin)] = True


def clear_all() -> None:
    """Test helper. Production lifecycle uses ``set_exchange_coins``."""
    with _LOCK:
        _UP.clear()

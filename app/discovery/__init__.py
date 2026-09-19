"""D0 universe discovery sidecar: REST intersection → atomic delta.

Not imported by websocket ingest. Collector hot-add reads the delta file.
"""

from .intersection import (  # noqa: F401
    build_intersection_rows,
    fetch_bybit_linear_instruments,
    fetch_okx_swap_instruments,
    filter_bybit_live_usdt_linear,
    filter_okx_live_usdt_swap,
    run_discovery,
)

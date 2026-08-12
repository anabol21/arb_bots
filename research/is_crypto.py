"""Classify base_coin as crypto vs equity/ETF/commodity-like perpetual.

Offline helper for universe screening (research / notebooks). Not wired into
the collector runtime.

Rule: ``is_crypto`` is True unless ``base_coin`` is in the curated denylist
(``research/data/non_crypto_base_coins.txt``), optionally overridden by an
allowlist. Default-True means unknown new equity tickers stay crypto until
added to the denylist — see false-positive/negative notes in that file.

Usage (from repo root)::

  from research.is_crypto import is_crypto
  is_crypto("BTC")   # True
  is_crypto("AAPL")  # False

  ./venv/bin/python research/is_crypto.py --base-coin BTC
  ./venv/bin/python research/is_crypto.py --universe bybit_okx_universe.csv --counts
  ./venv/bin/python research/is_crypto.py --universe bybit_okx_universe.csv --crypto-only
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DENYLIST_PATH = Path(__file__).resolve().parent / "data" / "non_crypto_base_coins.txt"
DEFAULT_UNIVERSE_PATH = REPO / "bybit_okx_universe.csv"

# Hard overrides: always crypto even if present in denylist (collision guard).
DEFAULT_CRYPTO_ALLOWLIST = frozenset(
    {
        "BTC",
        "ETH",
        "SOL",
        "BNB",
        "XRP",
        "DOGE",
        "ADA",
        "AVAX",
        "DOT",
        "LINK",
        "LTC",
        "BCH",
        "ATOM",
        "NEAR",
        "APT",
        "ARB",
        "OP",
        "SUI",
        "TRX",
        "TON",
        "FIL",
        "UNI",
        "AAVE",
        "COMP",  # Compound — do not confuse with equity tickers
        "MKR",
        "CRV",
        "SNX",
        "DASH",
        "ZEC",
        "XLM",
        "ETC",
        "ICP",
        "HBAR",
        "VET",
        "ALGO",
        "EOS",
        "XTZ",
        "EGLD",
        "FLOW",
        "MANA",
        "SAND",
        "AXS",
        "THETA",
        "IMX",
        "INJ",
        "SEI",
        "TIA",
        "STX",
        "RUNE",
        "PEPE",
        "WIF",
        "BONK",
        "SHIB",
    }
)

_denylist_cache: Optional[frozenset[str]] = None
_denylist_cache_path: Optional[Path] = None


def normalize_base_coin(base_coin: str) -> str:
    return str(base_coin).strip().upper()


def load_non_crypto_denylist(path: Optional[Union[str, Path]] = None) -> frozenset[str]:
    """Load curated non-crypto tickers (equity/ETF/metal proxies)."""
    global _denylist_cache, _denylist_cache_path
    p = Path(path) if path is not None else DEFAULT_DENYLIST_PATH
    if _denylist_cache is not None and _denylist_cache_path == p:
        return _denylist_cache
    if not p.is_file():
        raise FileNotFoundError(f"non-crypto denylist not found: {p}")
    out: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.add(normalize_base_coin(s.split()[0]))
    _denylist_cache = frozenset(out)
    _denylist_cache_path = p
    return _denylist_cache


def clear_denylist_cache() -> None:
    global _denylist_cache, _denylist_cache_path
    _denylist_cache = None
    _denylist_cache_path = None


def is_crypto(
    base_coin: str,
    *,
    denylist: Optional[Iterable[str]] = None,
    allowlist: Optional[Iterable[str]] = None,
    denylist_path: Optional[Union[str, Path]] = None,
) -> bool:
    """Return True if ``base_coin`` should be treated as a crypto perpetual.

    Parameters
    ----------
    base_coin:
        Universe ticker (e.g. ``BTC``, ``AAPL``).
    denylist:
        Optional override set of non-crypto tickers. When omitted, loads
        ``research/data/non_crypto_base_coins.txt``.
    allowlist:
        Optional crypto overrides (win over denylist). Defaults to major
        crypto tickers that must never be classified as equity.
    denylist_path:
        Alternate denylist file when ``denylist`` is omitted.
    """
    coin = normalize_base_coin(base_coin)
    if not coin:
        return False

    allow = (
        frozenset(normalize_base_coin(x) for x in allowlist)
        if allowlist is not None
        else DEFAULT_CRYPTO_ALLOWLIST
    )
    if coin in allow:
        return True

    deny = (
        frozenset(normalize_base_coin(x) for x in denylist)
        if denylist is not None
        else load_non_crypto_denylist(denylist_path)
    )
    return coin not in deny


def classify_base_coins(
    base_coins: Sequence[str],
    *,
    denylist: Optional[Iterable[str]] = None,
    allowlist: Optional[Iterable[str]] = None,
    denylist_path: Optional[Union[str, Path]] = None,
) -> list[dict]:
    """Batch helper: list of ``{"base_coin": ..., "is_crypto": bool}``."""
    return [
        {
            "base_coin": normalize_base_coin(c),
            "is_crypto": is_crypto(
                c,
                denylist=denylist,
                allowlist=allowlist,
                denylist_path=denylist_path,
            ),
        }
        for c in base_coins
    ]


def filter_crypto_coins(
    base_coins: Sequence[str],
    *,
    crypto: bool = True,
    denylist: Optional[Iterable[str]] = None,
    allowlist: Optional[Iterable[str]] = None,
    denylist_path: Optional[Union[str, Path]] = None,
) -> list[str]:
    """Return base_coins filtered to crypto (default) or non-crypto."""
    out: list[str] = []
    for c in base_coins:
        coin = normalize_base_coin(c)
        ok = is_crypto(
            coin,
            denylist=denylist,
            allowlist=allowlist,
            denylist_path=denylist_path,
        )
        if ok == crypto:
            out.append(coin)
    return out


def filter_crypto_dataframe(
    df,
    *,
    column: str = "base_coin",
    crypto: bool = True,
    denylist: Optional[Iterable[str]] = None,
    allowlist: Optional[Iterable[str]] = None,
    denylist_path: Optional[Union[str, Path]] = None,
):
    """Filter a pandas DataFrame by crypto / non-crypto ``base_coin``."""
    import pandas as pd  # local import: CLI --counts path may not need pandas

    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame")
    if column not in df.columns:
        raise KeyError(f"column {column!r} not in DataFrame")
    mask = df[column].map(
        lambda c: is_crypto(
            c,
            denylist=denylist,
            allowlist=allowlist,
            denylist_path=denylist_path,
        )
    )
    return df.loc[mask if crypto else ~mask].copy()


def read_universe_base_coins(universe_path: Union[str, Path]) -> list[str]:
    path = Path(universe_path)
    coins: list[str] = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "base_coin" not in reader.fieldnames:
            raise ValueError(f"universe CSV missing base_coin column: {path}")
        for row in reader:
            coins.append(normalize_base_coin(row["base_coin"]))
    return coins


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Classify base_coin as crypto vs equity/ETF/commodity-like."
    )
    p.add_argument(
        "--base-coin",
        action="append",
        dest="base_coins",
        metavar="COIN",
        help="Classify one coin (repeatable). Prints JSON.",
    )
    p.add_argument(
        "--universe",
        type=Path,
        default=None,
        help=f"Universe CSV with base_coin (default for --counts: {DEFAULT_UNIVERSE_PATH.name})",
    )
    p.add_argument(
        "--denylist",
        type=Path,
        default=DEFAULT_DENYLIST_PATH,
        help="Path to non-crypto denylist file",
    )
    p.add_argument(
        "--counts",
        action="store_true",
        help="Print crypto vs non-crypto counts for universe",
    )
    p.add_argument(
        "--crypto-only",
        action="store_true",
        help="Print crypto base_coins from universe (one per line)",
    )
    p.add_argument(
        "--non-crypto-only",
        action="store_true",
        help="Print non-crypto base_coins from universe (one per line)",
    )
    p.add_argument(
        "--json-all",
        action="store_true",
        help="Print full JSON classification for universe",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    denylist_path = args.denylist

    if args.base_coins:
        rows = classify_base_coins(args.base_coins, denylist_path=denylist_path)
        if len(rows) == 1:
            print(json.dumps(rows[0], separators=(",", ":")))
        else:
            print(json.dumps(rows, separators=(",", ":")))
        return 0

    need_universe = args.counts or args.crypto_only or args.non_crypto_only or args.json_all
    if not need_universe:
        _build_parser().print_help()
        return 0

    universe = args.universe or DEFAULT_UNIVERSE_PATH
    coins = read_universe_base_coins(universe)
    rows = classify_base_coins(coins, denylist_path=denylist_path)

    if args.counts:
        n_crypto = sum(1 for r in rows if r["is_crypto"])
        n_non = len(rows) - n_crypto
        print(
            json.dumps(
                {
                    "universe": str(universe),
                    "total": len(rows),
                    "crypto": n_crypto,
                    "non_crypto": n_non,
                    "denylist_path": str(denylist_path),
                    "denylist_size": len(load_non_crypto_denylist(denylist_path)),
                },
                separators=(",", ":"),
            )
        )

    if args.crypto_only:
        for r in rows:
            if r["is_crypto"]:
                print(r["base_coin"])

    if args.non_crypto_only:
        for r in rows:
            if not r["is_crypto"]:
                print(r["base_coin"])

    if args.json_all:
        print(json.dumps(rows, separators=(",", ":")))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Order coins for gear22 HTML index / nav by August spread std (СКО)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUGUST_STD_CSV = REPO_ROOT / "research" / "data" / "universe_spread_std_august.csv"

COIN_COL_CANDIDATES = ("base_coin", "coin", "symbol", "ticker")
STD_COL = "std_spread"
TAKE_YES = frozenset({"yes", "y", "true", "1"})
STATUS_OK = frozenset({"ok", "okay", "good"})


def _norm_coin(value: Any) -> str:
    return str(value).strip().upper()


def _norm_flag(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip().lower()


def _coin_column(columns: Sequence[str]) -> Optional[str]:
    lower = {str(c).strip().lower(): c for c in columns}
    for name in COIN_COL_CANDIDATES:
        if name in lower:
            return lower[name]
    return None


def load_august_std_table(path: Path | None = None) -> pd.DataFrame:
    """Load August ``std_spread`` table.

    Missing file / missing required columns → empty frame (callers fall back
    to alphabetical order). Duplicate coins keep the ``status=ok`` row when
    present, else the first row.
    """
    csv_path = Path(path) if path is not None else DEFAULT_AUGUST_STD_CSV
    if not csv_path.is_file():
        return pd.DataFrame(columns=["coin", STD_COL, "status", "take"])
    df = pd.read_csv(csv_path)
    coin_col = _coin_column(list(df.columns))
    std_lookup = {str(c).strip().lower(): c for c in df.columns}
    std_col = std_lookup.get(STD_COL)
    if coin_col is None or std_col is None:
        return pd.DataFrame(columns=["coin", STD_COL, "status", "take"])

    out = pd.DataFrame(
        {
            "coin": df[coin_col].map(_norm_coin),
            STD_COL: pd.to_numeric(df[std_col], errors="coerce"),
        }
    )
    if "status" in std_lookup:
        out["status"] = df[std_lookup["status"]].map(_norm_flag)
    else:
        out["status"] = "ok"
    if "take" in std_lookup:
        out["take"] = df[std_lookup["take"]].map(_norm_flag)
    else:
        out["take"] = ""

    out = out.loc[out["coin"] != ""]
    if out.empty:
        return out.reset_index(drop=True)

    out["_ok"] = out["status"].isin(STATUS_OK)
    out = out.sort_values(["coin", "_ok"], ascending=[True, False], kind="mergesort")
    out = out.drop_duplicates("coin", keep="first")
    return out.drop(columns=["_ok"]).reset_index(drop=True)


def std_lookup(table: pd.DataFrame) -> dict[str, float]:
    """Finite ``std_spread`` by coin (uppercase)."""
    if table is None or table.empty or STD_COL not in table.columns:
        return {}
    out: dict[str, float] = {}
    for row in table.itertuples(index=False):
        coin = _norm_coin(getattr(row, "coin", ""))
        std = getattr(row, STD_COL)
        if not coin or pd.isna(std):
            continue
        out[coin] = float(std)
    return out


def _take_yes(table: pd.DataFrame, coin: str) -> bool:
    if table.empty or "take" not in table.columns:
        return True
    hit = table.loc[table["coin"] == coin]
    if hit.empty:
        return True
    return _norm_flag(hit.iloc[0]["take"]) in TAKE_YES


def sort_coins_by_august_std(
    coins: Sequence[str],
    *,
    std_csv: Path | None = None,
    table: Optional[pd.DataFrame] = None,
    take_yes_only: bool = False,
) -> list[str]:
    """Sort coins by August ``std_spread`` descending.

    Coins present in the std table with a finite value come first (largest
    СКО first). Coins missing from the table, or with non-finite std, go last
    in alphabetical order. When ``take_yes_only`` and a ``take`` column exists,
    coins with an explicit non-yes ``take`` are dropped — unless that would
    empty the list (then the unfiltered sort is kept).
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in coins:
        coin = _norm_coin(raw)
        if not coin or coin in seen:
            continue
        seen.add(coin)
        ordered.append(coin)

    if table is None:
        table = load_august_std_table(std_csv)

    working = list(ordered)
    if take_yes_only and not table.empty and "take" in table.columns:
        if table["take"].map(_norm_flag).isin(TAKE_YES).any():
            filtered = [c for c in working if _take_yes(table, c)]
            if filtered:
                working = filtered

    lookup = table.set_index("coin") if not table.empty else pd.DataFrame()

    def sort_key(coin: str) -> tuple[int, float, str]:
        if lookup.empty or coin not in lookup.index:
            return (1, 0.0, coin)
        std = lookup.loc[coin, STD_COL]
        if pd.isna(std):
            return (1, 0.0, coin)
        return (0, -float(std), coin)

    return sorted(working, key=sort_key)


def order_meta(
    coins: Sequence[str],
    *,
    table: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    """JSON-friendly sort metadata for ``coins.json``."""
    lookup = std_lookup(table if table is not None else pd.DataFrame())
    stds = {c: lookup[c] for c in coins if c in lookup}
    return {
        "order": "august_std_spread_desc",
        "std_spread": stds,
    }

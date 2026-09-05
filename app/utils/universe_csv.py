"""Universe CSV helpers. Live pair screen is take=yes."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

TAKE_COLUMN = "take"
TAKE_YES = "yes"


class MissingTakeColumnError(ValueError):
    """Universe CSV has no take column; refuse to silently widen the live set."""


def missing_take_message(path: object | None = None) -> str:
    where = f" ({path})" if path is not None else ""
    return (
        "Universe CSV missing required 'take' column"
        f"{where}. Live pair screen is take=yes; "
        "refusing to load so a file without the column cannot silently widen."
    )


def require_take_column(
    fieldnames: Optional[Iterable[str]],
    *,
    path: object | None = None,
) -> None:
    names = list(fieldnames or [])
    if TAKE_COLUMN not in names:
        raise MissingTakeColumnError(missing_take_message(path))


def row_is_take_yes(row: Mapping[str, Any]) -> bool:
    return str(row.get(TAKE_COLUMN, "")).strip().lower() == TAKE_YES


def filter_take_yes_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    fieldnames: Optional[Iterable[str]] = None,
    path: object | None = None,
) -> list[Mapping[str, Any]]:
    """Keep take=yes rows only. Fail loud if the take column is absent."""
    names = fieldnames
    if names is None and rows:
        names = list(rows[0].keys())
    require_take_column(names, path=path)
    return [row for row in rows if row_is_take_yes(row)]

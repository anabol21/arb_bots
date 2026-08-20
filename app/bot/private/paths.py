"""B-private path resolution. Never write D collector trees."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

_D_DENY_PREFIXES = (
    "/data/live",
    "/data/bars",
    "/data/compacted",
    "/data/spool",
)

# Stub journal tree — private writer must never materialize here.
_STUB_JOURNAL_PREFIX = "/data/bbot/journal"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DATA_ROOT = Path("/data/bbot/private")
_FALLBACK_DATA_ROOT = _REPO_ROOT / "output" / "bbot" / "private"
_DEFAULT_LOG_PATH = Path("/var/log/spread/bbot-private.log")


def _is_under_denied(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    text = str(resolved)
    for prefix in _D_DENY_PREFIXES:
        if text == prefix or text.startswith(prefix + os.sep):
            return True
    if text == _STUB_JOURNAL_PREFIX or text.startswith(_STUB_JOURNAL_PREFIX + os.sep):
        return True
    return False


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".bbot_private_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def resolve_data_root(env: Optional[dict] = None) -> Path:
    """Default ``/data/bbot/private``; local fallback ``<repo>/output/bbot/private``."""
    e = env if env is not None else os.environ
    raw = (e.get("BBOT_PRIVATE_DATA_ROOT") or "").strip()
    if raw:
        root = Path(raw)
    else:
        root = _DEFAULT_DATA_ROOT
        if not _writable_dir(root):
            root = _FALLBACK_DATA_ROOT

    if _is_under_denied(root):
        raise RuntimeError(
            f"BBOT_PRIVATE_DATA_ROOT refuses D collector path: {root} "
            f"(denied prefixes: {_D_DENY_PREFIXES})"
        )
    # Must stay under /data/bbot/private or explicit local fallback / override.
    root.mkdir(parents=True, exist_ok=True)
    (root / "journal").mkdir(parents=True, exist_ok=True)
    (root / "probes").mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / ".tmp").mkdir(parents=True, exist_ok=True)
    return root.resolve()


def journal_dir(data_root: Path, event_date: str) -> Path:
    if _is_under_denied(data_root):
        raise RuntimeError(f"refusing journal under denied path: {data_root}")
    path = data_root / "journal" / f"event_date={event_date}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def probes_dir(data_root: Path, event_date: str) -> Path:
    """Non-journal private probes tree (legacy auth_probe JSONL lives here)."""
    if _is_under_denied(data_root):
        raise RuntimeError(f"refusing probes under denied path: {data_root}")
    path = data_root / "probes" / f"event_date={event_date}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def auth_probe_jsonl_path(data_root: Path, event_date: str) -> Path:
    """Legacy auth_probe.jsonl — outside canonical journal/ tree."""
    return probes_dir(data_root, event_date) / "auth_probe.jsonl"


def events_jsonl_path(data_root: Path, event_date: str) -> Path:
    """Append-only path for ``bbot.private.journal.v1`` (``events.jsonl``)."""
    return journal_dir(data_root, event_date) / "events.jsonl"


def _assert_log_path_allowed(path: Path, *, label: str = "BBOT_PRIVATE_LOG_PATH") -> None:
    """Fail-closed: refuse D collector trees and collector/stub log names."""
    name = path.name
    if name in {"runtime.log", "bbot.log"}:
        raise RuntimeError(
            f"{label} must not be {name} "
            "(collector or stub log); use bbot-private.log"
        )
    if _is_under_denied(path):
        raise RuntimeError(
            f"{label} refuses D collector path: {path} "
            f"(denied prefixes: {_D_DENY_PREFIXES})"
        )


def resolve_log_path(data_root: Optional[Path] = None, env: Optional[dict] = None) -> Path:
    """Default ``/var/log/spread/bbot-private.log``; private data-root fallback.

    Override via ``BBOT_PRIVATE_LOG_PATH`` is fail-closed against D roots
    (``/data/live``, ``/data/bars``, ``/data/compacted``, ``/data/spool``) and
    against collector/stub log basenames.
    """
    e = env if env is not None else os.environ
    raw = (e.get("BBOT_PRIVATE_LOG_PATH") or "").strip()
    preferred = Path(raw) if raw else _DEFAULT_LOG_PATH
    _assert_log_path_allowed(preferred)

    def _try(path: Path) -> Optional[Path]:
        _assert_log_path_allowed(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write("")
            return path
        except OSError:
            return None

    ok = _try(preferred)
    if ok is not None:
        return ok

    root = data_root if data_root is not None else resolve_data_root(e)
    if _is_under_denied(root):
        raise RuntimeError(
            f"BBOT_PRIVATE_LOG_PATH fallback refuses D collector data root: {root}"
        )
    fallback = root / "bbot-private.log"
    ok2 = _try(fallback)
    if ok2 is not None:
        return ok2
    _assert_log_path_allowed(preferred)
    return preferred


def repo_root() -> Path:
    return _REPO_ROOT


def ensure_repo_on_syspath() -> None:
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

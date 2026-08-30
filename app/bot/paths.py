"""BBOT_* path resolution only. Never import app.storage.paths."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# Forbidden D collector trees — bot must never resolve writes here.
_D_DENY_PREFIXES = (
    "/data/live",
    "/data/bars",
    "/data/compacted",
    "/data/spool",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_ROOT = Path("/data/bbot")
_FALLBACK_DATA_ROOT = _REPO_ROOT / "output" / "bbot"
_DEFAULT_LOG_PATH = Path("/var/log/spread/bbot.log")
_DEFAULT_BACKUP_LOCK = Path("/run/spread-bbot-backup.lock")


def _is_under_denied(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    text = str(resolved)
    for prefix in _D_DENY_PREFIXES:
        if text == prefix or text.startswith(prefix + os.sep):
            return True
    return False


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".bbot_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def resolve_data_root(env: Optional[dict] = None) -> Path:
    """Resolve BBOT_DATA_ROOT.

    Default ``/data/bbot``. If unset and that path is not writable, fall back to
    ``<repo>/output/bbot``. Never ``/data/live``.
    """
    e = env if env is not None else os.environ
    raw = (e.get("BBOT_DATA_ROOT") or "").strip()
    if raw:
        root = Path(raw)
    else:
        root = _DEFAULT_DATA_ROOT
        if not _writable_dir(root):
            root = _FALLBACK_DATA_ROOT

    if _is_under_denied(root):
        raise RuntimeError(
            f"BBOT_DATA_ROOT refuses D collector path: {root} "
            f"(denied prefixes: {_D_DENY_PREFIXES})"
        )
    root.mkdir(parents=True, exist_ok=True)
    (root / "journal").mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / ".tmp").mkdir(parents=True, exist_ok=True)
    return root.resolve()


def journal_dir(data_root: Path, event_date: str) -> Path:
    """Return ``{data_root}/journal/event_date=YYYY-MM-DD`` (create if needed)."""
    if _is_under_denied(data_root):
        raise RuntimeError(f"refusing journal under denied path: {data_root}")
    path = data_root / "journal" / f"event_date={event_date}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def legs_jsonl_path(data_root: Path, event_date: str) -> Path:
    return journal_dir(data_root, event_date) / "legs.jsonl"


def state_dir(data_root: Path) -> Path:
    if _is_under_denied(data_root):
        raise RuntimeError(f"refusing state under denied path: {data_root}")
    path = data_root / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pending_state_path(data_root: Path) -> Path:
    return state_dir(data_root) / "pending.json"


def resolve_log_path(data_root: Optional[Path] = None, env: Optional[dict] = None) -> Path:
    """BBOT_LOG_PATH default ``/var/log/spread/bbot.log``.

    If not writable: caller should also mirror to stderr; file falls back to
    ``{data_root}/bbot.log``. Never ``runtime.log``.
    """
    e = env if env is not None else os.environ
    raw = (e.get("BBOT_LOG_PATH") or "").strip()
    preferred = Path(raw) if raw else _DEFAULT_LOG_PATH
    if preferred.name == "runtime.log":
        raise RuntimeError("BBOT_LOG_PATH must not be runtime.log (collector log)")

    def _try(path: Path) -> Optional[Path]:
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
    fallback = root / "bbot.log"
    ok2 = _try(fallback)
    if ok2 is not None:
        return ok2
    # Last resort: still return preferred for logger setup; writes may fail to stderr only.
    return preferred


def resolve_backup_lock(env: Optional[dict] = None) -> Path:
    e = env if env is not None else os.environ
    raw = (e.get("BBOT_BACKUP_LOCK") or "").strip()
    path = Path(raw) if raw else _DEFAULT_BACKUP_LOCK
    if path.name == "spread-backup.lock" or str(path) == "/run/spread-backup.lock":
        raise RuntimeError(
            "BBOT_BACKUP_LOCK must not reuse D lock /run/spread-backup.lock"
        )
    return path


def resolve_rclone_bin(env: Optional[dict] = None) -> str:
    e = env if env is not None else os.environ
    return (e.get("BBOT_RCLONE_BIN") or "/opt/rclone-1.74.4/rclone").strip()


def resolve_rclone_remote(env: Optional[dict] = None) -> str:
    e = env if env is not None else os.environ
    return (e.get("BBOT_RCLONE_REMOTE") or "backup1tb").strip().rstrip(":")


def resolve_rclone_path(env: Optional[dict] = None) -> str:
    e = env if env is not None else os.environ
    return (e.get("BBOT_RCLONE_PATH") or "spread-bbot").strip().strip("/")


def repo_root() -> Path:
    return _REPO_ROOT


def ensure_repo_on_syspath() -> None:
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)

"""Cross-process execution-v2 ownership fence.

Stdlib only. Acquire and release are explicit. Submit-path assert is
in-memory and performs no filesystem call.
"""

from __future__ import annotations

import fcntl
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "bbot.execution.ownership.v1"

_OWNED_PATHS: dict[str, str] = {}

_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "passphrase",
        "password",
        "authorization",
        "signature",
        "sign",
        "access_token",
        "refresh_token",
        "bearer_token",
        "private_key",
        "client_secret",
        "raw_payload",
        "raw_frame",
        "account_id",
        "uid",
        "wallet_address",
        "exchange_order_id",
        "order_id",
        "balance",
        "equity",
    }
)


def _norm_key(name: object) -> str:
    return str(name).strip().lower().replace("-", "_")


def _assert_public(node: object) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if _norm_key(key) in _FORBIDDEN_PUBLIC_KEYS:
                raise OwnershipError("forbidden_field")
            _assert_public(value)


def _path_identity(path: Path) -> str:
    parent = path.parent
    if parent.exists():
        return str(parent.resolve() / path.name)
    return str(Path(os.path.abspath(str(path))))


class OwnershipError(ValueError):
    """Fail-closed ownership fence error. Public view is redacted."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code)
        super().__init__(self.reason_code)

    def to_public_dict(self) -> dict[str, Any]:
        out = {"schema_version": SCHEMA_VERSION, "reason_code": self.reason_code}
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return f"OwnershipError(reason_code={self.reason_code!r})"


@dataclass
class FileOwnershipFence:
    """Nonblocking exclusive flock plus in-process path registry."""

    def __init__(self, path: Any, *, owner_token: Optional[str] = None) -> None:
        if path is None:
            raise OwnershipError("invalid_path")
        self._path = Path(path)
        if not str(self._path):
            raise OwnershipError("invalid_path")
        token = owner_token if owner_token is not None else uuid.uuid4().hex
        if not isinstance(token, str) or not token:
            raise OwnershipError("invalid_token")
        self._token = token
        self._owned = False
        self._identity: Optional[str] = None
        self._fh: Optional[Any] = None
        self._owner_pid: Optional[int] = None
        self._engine_claim: Optional[str] = None
        self._claimed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def owner_token(self) -> str:
        return self._token

    @property
    def owned(self) -> bool:
        return self._owned

    def _inherited_from_parent(self) -> bool:
        return self._owner_pid is not None and self._owner_pid != os.getpid()

    def _detach_inherited_fd(self) -> None:
        """Close a forked fd without LOCK_UN so the parent keeps the lock."""
        handle = self._fh
        identity = self._identity
        token = self._token
        self._owned = False
        self._identity = None
        self._fh = None
        self._owner_pid = None
        self._engine_claim = None
        self._claimed = False
        if identity is not None and _OWNED_PATHS.get(identity) == token:
            del _OWNED_PATHS[identity]
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def acquire(self) -> None:
        if self._inherited_from_parent():
            self._detach_inherited_fd()
        elif self._owned:
            raise OwnershipError("already_owned")
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        identity = _path_identity(self._path)
        if identity in _OWNED_PATHS:
            raise OwnershipError("already_owned")
        handle = open(self._path, "a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            raise OwnershipError("lock_held") from None
        if identity in _OWNED_PATHS:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            raise OwnershipError("already_owned")
        _OWNED_PATHS[identity] = self._token
        self._fh = handle
        self._identity = identity
        self._owned = True
        self._owner_pid = os.getpid()
        self._engine_claim = uuid.uuid4().hex
        self._claimed = False

    def claim_engine(self) -> str:
        """Consume the single engine claim token created by acquire()."""
        if self._inherited_from_parent() or self._owner_pid != os.getpid():
            raise OwnershipError("not_owned")
        if not self._owned or self._engine_claim is None:
            raise OwnershipError("not_owned")
        if self._claimed:
            raise OwnershipError("already_owned")
        self._claimed = True
        return self._engine_claim

    def assert_owned(self, claim_token: Optional[str] = None) -> None:
        if self._inherited_from_parent() or self._owner_pid != os.getpid():
            raise OwnershipError("not_owned")
        if not self._owned or self._identity is None:
            raise OwnershipError("not_owned")
        if _OWNED_PATHS.get(self._identity) != self._token:
            raise OwnershipError("not_owned")
        if claim_token is not None:
            if not self._claimed or claim_token != self._engine_claim:
                raise OwnershipError("not_owned")
            return
        if self._claimed:
            raise OwnershipError("not_owned")

    def release(self) -> None:
        if self._inherited_from_parent():
            self._detach_inherited_fd()
            return
        identity = self._identity
        handle = self._fh
        self._owned = False
        self._identity = None
        self._fh = None
        self._owner_pid = None
        self._engine_claim = None
        self._claimed = False
        if identity is not None and _OWNED_PATHS.get(identity) == self._token:
            del _OWNED_PATHS[identity]
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def to_public_dict(self) -> dict[str, Any]:
        out = {
            "schema_version": SCHEMA_VERSION,
            "owned": self._owned,
            "owner_token": self._token,
        }
        _assert_public(out)
        return out

    def __repr__(self) -> str:
        return (
            "FileOwnershipFence("
            f"owned={self._owned}, owner_token={self._token!r})"
        )

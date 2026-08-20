"""Load B-private secrets from disk. Never log secret values.

Fail-closed credential profiles:
- testnet → testnet/demo env only (never a live-named path or LIVE_* keys)
- live → live-named env only (never a testnet-named path or TESTNET/DEMO keys)
- VENUE=live + LIVE_ORDERS=0 → live credentials may load; no order send surface
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Optional, Union

from app.bot.private.venue import (
    default_secret_file_for_venue,
    live_orders_enabled,
    resolve_venue,
    send_allowed,
    testnet_alias_secret_file,
)

_SECRET_SUFFIXES = ("_API_SECRET", "_SECRET", "_PASSPHRASE", "_PASSWORD")
_KEY_SUFFIXES = ("_API_KEY", "_KEY")

CredentialProfileName = Literal["testnet", "live"]

TESTNET_KEY_NAMES = (
    "BYBIT_TESTNET_API_KEY",
    "BYBIT_TESTNET_API_SECRET",
    "OKX_DEMO_API_KEY",
    "OKX_DEMO_API_SECRET",
    "OKX_DEMO_PASSPHRASE",
)

LIVE_KEY_NAMES = (
    "BYBIT_LIVE_API_KEY",
    "BYBIT_LIVE_API_SECRET",
    "OKX_LIVE_API_KEY",
    "OKX_LIVE_API_SECRET",
    "OKX_LIVE_PASSPHRASE",
)


@dataclass(frozen=True)
class TestnetSecrets:
    """Demo/testnet credentials only. Presence flags for logging."""

    bybit_api_key: str
    bybit_api_secret: str
    okx_api_key: str
    okx_api_secret: str
    okx_passphrase: str
    source_path: str

    def presence(self) -> dict[str, bool]:
        return {
            "bybit_api_key_present": bool(self.bybit_api_key),
            "bybit_api_secret_present": bool(self.bybit_api_secret),
            "okx_api_key_present": bool(self.okx_api_key),
            "okx_api_secret_present": bool(self.okx_api_secret),
            "okx_passphrase_present": bool(self.okx_passphrase),
        }

    def mask_key_prefix(self, value: str, *, keep: int = 4) -> str:
        if not value:
            return ""
        if len(value) <= keep:
            return "…"
        return value[:keep] + "…"


@dataclass(frozen=True)
class LiveSecrets:
    """Live credentials only. Presence flags for logging — never values."""

    bybit_api_key: str
    bybit_api_secret: str
    okx_api_key: str
    okx_api_secret: str
    okx_passphrase: str
    source_path: str

    def presence(self) -> dict[str, bool]:
        return {
            "bybit_api_key_present": bool(self.bybit_api_key),
            "bybit_api_secret_present": bool(self.bybit_api_secret),
            "okx_api_key_present": bool(self.okx_api_key),
            "okx_api_secret_present": bool(self.okx_api_secret),
            "okx_passphrase_present": bool(self.okx_passphrase),
        }

    def mask_key_prefix(self, value: str, *, keep: int = 4) -> str:
        if not value:
            return ""
        if len(value) <= keep:
            return "…"
        return value[:keep] + "…"


@dataclass(frozen=True)
class PrivateProfile:
    """Resolved credential profile. Fail-closed; no order methods attached."""

    name: CredentialProfileName
    venue: str
    secret_path: Path
    live_orders_flag: bool
    send_allowed: bool
    orders_surface: bool
    readonly: bool

    def as_public_dict(self) -> dict[str, object]:
        return {
            "credential_profile": self.name,
            "VENUE": self.venue,
            "live_orders_flag": self.live_orders_flag,
            "send_allowed": self.send_allowed,
            "orders_surface": self.orders_surface,
            "readonly": self.readonly,
            # Basename only — never log full path with secrets context.
            "secret_file_basename": self.secret_path.name,
        }


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. No shell expansion. Skip comments/blank."""
    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{lineno}: expected KEY=VALUE")
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if not key:
            raise ValueError(f"{path}:{lineno}: empty key")
        out[key] = val
    return out


def _path_looks_like_live(path: Path) -> bool:
    return "live" in path.name.lower()


def _path_looks_like_testnet(path: Path) -> bool:
    name = path.name.lower()
    return "testnet" in name or name in {"bbot-private.env"}


def resolve_secret_file(
    venue: str,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Pick secret file for venue. Profiles never cross-load."""
    e = env if env is not None else os.environ
    override = (e.get("BBOT_PRIVATE_ENV_FILE") or "").strip()
    if override:
        path = Path(override)
        if venue == "testnet" and _path_looks_like_live(path):
            raise RuntimeError(
                f"VENUE=testnet refuses secret file with 'live' in name: {path}"
            )
        if venue == "live" and not _path_looks_like_live(path):
            raise RuntimeError(
                f"VENUE=live refuses non-live-named secret file: {path}"
            )
        if venue == "live" and _path_looks_like_testnet(path):
            raise RuntimeError(
                f"VENUE=live refuses testnet-named secret file: {path}"
            )
        return path

    preferred = Path(default_secret_file_for_venue(venue))
    if venue == "testnet":
        if preferred.is_file():
            if _path_looks_like_live(preferred):
                raise RuntimeError(f"refusing live-named testnet default: {preferred}")
            return preferred
        alias = Path(testnet_alias_secret_file())
        if alias.is_file():
            if _path_looks_like_live(alias):
                raise RuntimeError(f"refusing live-named alias: {alias}")
            return alias
        return preferred

    if venue == "live":
        if not _path_looks_like_live(preferred):
            raise RuntimeError(f"live secret path must be live-named: {preferred}")
        if _path_looks_like_testnet(preferred):
            raise RuntimeError(f"live secret path must not be testnet-named: {preferred}")
        return preferred

    raise ValueError(f"unknown venue for secret resolve: {venue!r}")


def resolve_private_profile(
    env: Optional[Mapping[str, str]] = None,
) -> PrivateProfile:
    """Fail-closed profile selection from VENUE / LIVE_ORDERS.

    - testnet → testnet profile; never live path
    - live + LIVE_ORDERS=0 → live profile, readonly, orders_surface=False
    - live + LIVE_ORDERS=1 → live profile; send_allowed may be True, but this
      module still exposes no order endpoints (send surface stays False until
      a later R3 approval-bound sender exists)
    """
    e = env if env is not None else os.environ
    venue = resolve_venue(e)
    live_flag = live_orders_enabled(e)
    allowed = send_allowed(e)

    if venue == "testnet":
        if live_flag:
            # Keep existing stage-1 refuse semantics for callers that check
            # profile before harness; profile itself is still testnet-only.
            pass
        path = resolve_secret_file("testnet", e)
        if _path_looks_like_live(path):
            raise RuntimeError(f"testnet profile refused live path: {path}")
        return PrivateProfile(
            name="testnet",
            venue="testnet",
            secret_path=path,
            live_orders_flag=live_flag,
            send_allowed=False,
            orders_surface=False,
            readonly=True,
        )

    if venue == "live":
        path = resolve_secret_file("live", e)
        if not _path_looks_like_live(path):
            raise RuntimeError(f"live profile refused non-live path: {path}")
        # R0/R1: no order send surface even if LIVE_ORDERS=1; approval-bound
        # sender arrives later. LIVE_ORDERS=0 is explicitly read-only.
        return PrivateProfile(
            name="live",
            venue="live",
            secret_path=path,
            live_orders_flag=live_flag,
            send_allowed=allowed,
            orders_surface=False,
            readonly=not live_flag,
        )

    raise RuntimeError(f"unsupported venue for profile: {venue!r}")


def load_testnet_secrets(
    env: Optional[Mapping[str, str]] = None,
    *,
    require_complete: bool = False,
) -> TestnetSecrets:
    """Load testnet/demo secrets. Never reads live env file."""
    e = env if env is not None else os.environ
    venue = resolve_venue(e)
    if venue != "testnet":
        raise RuntimeError(
            f"load_testnet_secrets requires VENUE=testnet (got {venue!r})"
        )
    profile = resolve_private_profile(e)
    if profile.name != "testnet":
        raise RuntimeError(f"expected testnet profile, got {profile.name!r}")
    path = profile.secret_path
    if not path.is_file():
        raise FileNotFoundError(
            f"testnet/demo secret file missing: {path} "
            "(create mode 600 outside git; see docs/b-private-secrets-manifest.md)"
        )
    if _path_looks_like_live(path):
        raise RuntimeError(f"testnet loader refused live-named file: {path}")

    raw = parse_env_file(path)
    # Refuse live-named keys inside a testnet file (misconfiguration).
    for key in raw:
        if re.search(r"(^|_)LIVE(_|$)", key, flags=re.IGNORECASE):
            raise RuntimeError(
                f"testnet env file {path} contains live-named variable {key!r}; "
                "move live keys to bbot-private-live.env"
            )

    secrets = TestnetSecrets(
        bybit_api_key=raw.get("BYBIT_TESTNET_API_KEY", "").strip(),
        bybit_api_secret=raw.get("BYBIT_TESTNET_API_SECRET", "").strip(),
        okx_api_key=raw.get("OKX_DEMO_API_KEY", "").strip(),
        okx_api_secret=raw.get("OKX_DEMO_API_SECRET", "").strip(),
        okx_passphrase=raw.get("OKX_DEMO_PASSPHRASE", "").strip(),
        source_path=str(path),
    )
    if require_complete:
        missing = [k for k, ok in secrets.presence().items() if not ok]
        if missing:
            raise RuntimeError(f"incomplete testnet secrets ({path}): {missing}")
    return secrets


def load_live_secrets(
    env: Optional[Mapping[str, str]] = None,
    *,
    require_complete: bool = False,
) -> LiveSecrets:
    """Load live secrets. Only when VENUE=live; never opens testnet-named file.

    LIVE_ORDERS may be 0 (read-only). This loader does not create an order
    send surface.
    """
    e = env if env is not None else os.environ
    venue = resolve_venue(e)
    if venue != "live":
        raise RuntimeError(
            f"load_live_secrets requires VENUE=live (got {venue!r}); "
            "testnet must never load live credentials"
        )
    profile = resolve_private_profile(e)
    if profile.name != "live":
        raise RuntimeError(f"expected live profile, got {profile.name!r}")
    if profile.orders_surface:
        raise RuntimeError(
            "live secrets loader refused: orders_surface unexpectedly True "
            "(R0 has no order endpoints)"
        )
    path = profile.secret_path
    if not path.is_file():
        raise FileNotFoundError(
            f"live secret file missing: {path} "
            "(create mode 600 outside git from bbot-private-live.env.template)"
        )
    if not _path_looks_like_live(path):
        raise RuntimeError(f"live loader refused non-live-named file: {path}")
    if _path_looks_like_testnet(path):
        raise RuntimeError(f"live loader refused testnet-named file: {path}")

    raw = parse_env_file(path)
    for key in raw:
        upper = key.upper()
        if upper in {"VENUE", "LIVE_ORDERS"}:
            raise RuntimeError(
                f"live credential file {path} must not contain {key!r}; "
                "venue/flags belong in process env, not the credential file"
            )
        if re.search(r"(TESTNET|DEMO)", upper):
            raise RuntimeError(
                f"live env file {path} contains testnet/demo variable {key!r}; "
                "use BYBIT_LIVE_* / OKX_LIVE_* only"
            )

    secrets = LiveSecrets(
        bybit_api_key=raw.get("BYBIT_LIVE_API_KEY", "").strip(),
        bybit_api_secret=raw.get("BYBIT_LIVE_API_SECRET", "").strip(),
        okx_api_key=raw.get("OKX_LIVE_API_KEY", "").strip(),
        okx_api_secret=raw.get("OKX_LIVE_API_SECRET", "").strip(),
        okx_passphrase=raw.get("OKX_LIVE_PASSPHRASE", "").strip(),
        source_path=str(path),
    )
    if require_complete:
        missing = [k for k, ok in secrets.presence().items() if not ok]
        if missing:
            raise RuntimeError(f"incomplete live secrets ({path}): {missing}")
    return secrets


def load_secrets_for_profile(
    env: Optional[Mapping[str, str]] = None,
    *,
    require_complete: bool = False,
) -> Union[TestnetSecrets, LiveSecrets]:
    """Dispatch by resolved profile. Fail-closed cross-load prevention."""
    profile = resolve_private_profile(env)
    if profile.name == "testnet":
        return load_testnet_secrets(env, require_complete=require_complete)
    if profile.name == "live":
        return load_live_secrets(env, require_complete=require_complete)
    raise RuntimeError(f"unknown profile: {profile.name!r}")


def redact_for_log(text: str, secrets: Mapping[str, str]) -> str:
    """Replace any known secret substrings in text before logging."""
    out = text
    for val in secrets.values():
        if val and len(val) >= 4:
            out = out.replace(val, "***")
    return out


def is_secret_env_name(name: str) -> bool:
    upper = name.upper()
    return upper.endswith(_SECRET_SUFFIXES) or upper.endswith(_KEY_SUFFIXES)

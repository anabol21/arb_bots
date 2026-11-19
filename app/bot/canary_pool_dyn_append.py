#!/usr/bin/env python3
"""Contour-B canary (gear23 B2.3) daily pool expansion sidecar.

Pool model: base (WAL,RVN,LA) + append-only own delta file (starts with the 9
seed rows API3,BLUR,CT,DOOD,IOST,MEW,RSR,SAND,TRUST). Each morning (timer
~10:10 MSK, after the 09:10 top-10-volatile render and the 10:00 shared
would_send rotate) every coin of the top-10 volatile list that is NOT yet in
base+delta is appended to the canary's OWN delta file. Never removes coins.
Hard cap on total pool size.

This is the VPS sidecar from /root/pool12/dyn/canary_pool_dyn_append.py,
integrated as an importable module. File semantics stay compatible:
state/pool_dyn/hot_add_delta_dyn.csv, last_launcher.txt gate, --apply.

Safety: DEFAULT IS DRY-RUN; --apply is required to write. Never writes
anything under /data/bbot-would-send-prod* (read-only sources). Atomic write
(tmp+fsync+rename), flock by the caller (systemd) and an in-process flock.
No exchange REST. No trading logic / filters touched: the bot's own hot-add
gates still apply.

Unlike the shared would_send rotator (bbot_top10_canary_rotate.py /
bbot_would_send_prod_rotate.py), this job does NOT skip that rotator's BASE
(BLEND, APR, …). Only WAL,RVN,LA are excluded as this contour's base.

Install on VPS (do not enable from this checkout):
  cp app/bot/canary_pool_dyn_append.py /root/pool12/dyn/canary_pool_dyn_append.py
"""
from __future__ import annotations

import argparse
import csv
import fcntl
import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

RUNROOT = Path("/root/b-private-b-exp/response-manager/20261005T194200Z-gear22-longrun")
CODE = Path(
    "/root/b-private-b-exp/response-manager-code/"
    "gear23-b2.3-tw-deque-20261010-tradeobs-20261018T1741Z"
)

# Default VPS locations. Tests / local runs override via Paths or env.
DEFAULT_POOL_DIR = RUNROOT / "state" / "pool_dyn"
DEFAULT_DELTA_NAME = "hot_add_delta_dyn.csv"
DEFAULT_LAST_LAUNCHER = RUNROOT / "state" / "last_launcher.txt"
DEFAULT_TOP10_JSON = Path("/var/log/spread/top10_volatile_spread.json")
DEFAULT_TOP10_JSON_ALT = Path("/tmp/ops_digest_charts/top10_volatile_spread.json")
DEFAULT_SHARED_DELTA = Path("/data/bbot-would-send-prod/hot_add_delta.csv")
DEFAULT_SHARED_UNIVERSE = Path("/data/bbot-would-send-prod/bybit_okx_universe.csv")
DEFAULT_CODE_UNIVERSE = CODE / "bybit_okx_universe.csv"
DEFAULT_DISCOVERY_DELTA = Path("/root/spread_staging/hot_add_delta.csv")
LAUNCHER_MARK = "gear23-pool-dyn-launch"

BASE = ("WAL", "RVN", "LA")
SEED = ("API3", "BLUR", "CT", "DOOD", "IOST", "MEW", "RSR", "SAND", "TRUST")
DEFAULT_MAX_TOTAL = 30

FIELDS = [
    "base_coin",
    "okx_symbol",
    "bybit_symbol",
    "okx_tick_size",
    "okx_lot_size",
    "okx_min_size",
    "bybit_tick_size",
    "bybit_qty_step",
    "bybit_min_order_qty",
    "bybit_min_notional_value",
    "discovered_at_utc",
]
LOT_TICK = (
    "okx_tick_size",
    "okx_lot_size",
    "okx_min_size",
    "bybit_tick_size",
    "bybit_qty_step",
    "bybit_min_order_qty",
)

# render_top10_volatile_spread.py is a VPS ops script (spread-top10-volatile-spread.service),
# not present in this git tree. The would_send rotator and the VPS sidecar both read
# data["top10"][*]["base_coin"] plus stamp / window_end_msk. Extra std/mean keys are
# accepted for the optional, default-off sanity guard.
TOP10_LIST_KEYS = ("top10", "top_10")
TOP10_COIN_KEYS = ("base_coin", "coin")
TOP10_STAMP_KEYS = ("stamp", "window_end_msk", "generated_at_msk", "generated_at")
STD_BPS_KEYS = (
    "std_spread_long_bps",
    "std_long_bps",
    "std_spread_bps",
    "spread_long_std_bps",
    "std_bps",
    "std",
)
MEAN_BPS_KEYS = (
    "mean_spread_long_bps",
    "mean_long_bps",
    "mean_spread_bps",
    "mean_bps",
    "mean",
)

FORBIDDEN_WRITE_PREFIXES = (
    "/data/bbot-would-send-prod",
    "/data/live",
    "/data/bars",
    "/data/compacted",
    "/data/spool",
)

MSK = timezone(timedelta(hours=3))

CryptoFn = Callable[[str], bool]


class DeltaPathError(ValueError):
    """Refusing to write a canary delta on a shared/D path."""


@dataclass(frozen=True)
class Paths:
    pool_dir: Path
    delta: Path
    status: Path
    log: Path
    events: Path
    last_launcher: Path
    top10_candidates: tuple[Path, ...]
    meta_sources: tuple[tuple[str, Path], ...]


@dataclass(frozen=True)
class Settings:
    apply: bool = False
    force: bool = False
    max_total: int = DEFAULT_MAX_TOTAL
    allow_stale_digest: bool = False
    max_std_bps: Optional[float] = None
    launcher_mark: str = LAUNCHER_MARK
    base: tuple[str, ...] = BASE
    seed: tuple[str, ...] = SEED


def now_msk(now: Optional[datetime] = None) -> str:
    clock = now if now is not None else datetime.now(MSK)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=MSK)
    return clock.astimezone(MSK).isoformat(timespec="seconds")


def utc_iso(now: Optional[datetime] = None) -> str:
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_msk(now: Optional[datetime] = None) -> str:
    clock = now if now is not None else datetime.now(MSK)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=MSK)
    return clock.astimezone(MSK).strftime("%Y-%m-%d")


def pos(v: object) -> bool:
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return False
    return f == f and f > 0


def _first_float(row: Mapping[str, object], keys: Sequence[str]) -> Optional[float]:
    for key in keys:
        if key not in row or row[key] is None:
            continue
        text = str(row[key]).strip()
        if not text:
            continue
        try:
            value = float(text)
        except (TypeError, ValueError):
            continue
        if value == value:
            return value
    return None


def paths_from_env(
    environ: Optional[Mapping[str, str]] = None,
    *,
    pool_dir: Optional[Path] = None,
) -> Paths:
    env = os.environ if environ is None else environ
    root = Path(env.get("POOL_DYN_RUNROOT") or RUNROOT)
    code = Path(env.get("POOL_DYN_CODE") or CODE)
    directory = Path(env.get("POOL_DYN_DIR") or pool_dir or (root / "state" / "pool_dyn"))
    delta = Path(env.get("POOL_DYN_DELTA") or (directory / DEFAULT_DELTA_NAME))
    top10 = Path(env.get("POOL_DYN_TOP10") or DEFAULT_TOP10_JSON)
    top10_alt = Path(env.get("POOL_DYN_TOP10_ALT") or DEFAULT_TOP10_JSON_ALT)
    shared_delta = Path(env.get("POOL_DYN_SHARED_DELTA") or DEFAULT_SHARED_DELTA)
    shared_universe = Path(env.get("POOL_DYN_SHARED_UNIVERSE") or DEFAULT_SHARED_UNIVERSE)
    code_universe = Path(env.get("POOL_DYN_CODE_UNIVERSE") or (code / "bybit_okx_universe.csv"))
    discovery = Path(env.get("POOL_DYN_DISCOVERY_DELTA") or DEFAULT_DISCOVERY_DELTA)
    last_launcher = Path(env.get("POOL_DYN_LAST_LAUNCHER") or (root / "state" / "last_launcher.txt"))
    return Paths(
        pool_dir=directory,
        delta=delta,
        status=directory / "last_append.json",
        log=directory / "append.log",
        events=directory / "append_events.jsonl",
        last_launcher=last_launcher,
        top10_candidates=(top10, top10_alt),
        meta_sources=(
            ("shared_delta", shared_delta),
            ("shared_universe", shared_universe),
            ("code_universe", code_universe),
            ("discovery_delta", discovery),
        ),
    )


def assert_delta_writable(delta: Path) -> Path:
    """Refuse the shared would_send file and D collector trees."""
    try:
        resolved = delta.expanduser().resolve()
    except OSError:
        resolved = delta.expanduser().absolute()
    text = str(resolved)
    for prefix in FORBIDDEN_WRITE_PREFIXES:
        if text == prefix or text.startswith(prefix + os.sep):
            raise DeltaPathError(f"refusing to write canary delta under {prefix}: {resolved}")
    if resolved.name == "hot_add_delta.csv" and "bbot-would-send-prod" in text:
        raise DeltaPathError(f"refusing to write the shared would_send delta: {resolved}")
    return delta


def log(msg: str, paths: Optional[Paths] = None) -> None:
    line = f"{now_msk()} | {msg}"
    print(line, flush=True)
    if paths is None:
        return
    try:
        paths.log.parent.mkdir(parents=True, exist_ok=True)
        with paths.log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def is_crypto(coin: str, code_root: Optional[Path] = None) -> bool:
    """Same predicate the bot applies on hot-add (non-crypto rows are skipped anyway)."""
    try:
        from research.is_crypto import is_hot_add_crypto  # type: ignore

        return bool(is_hot_add_crypto(coin))
    except Exception:
        pass
    root = code_root if code_root is not None else CODE
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from research.is_crypto import is_hot_add_crypto  # type: ignore

        return bool(is_hot_add_crypto(coin))
    except Exception as exc:  # noqa: BLE001
        log(f"WARN is_hot_add_crypto unavailable ({type(exc).__name__}); not filtering {coin}")
        return True


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [{k: (v if v is not None else "") for k, v in row.items()} for row in csv.DictReader(fh)]


def parse_top10(data: Mapping[str, object]) -> tuple[list[str], dict[str, Optional[float]], dict[str, Optional[float]]]:
    """Coins in digest order plus optional std/mean bps keyed by coin.

    Matches deploy/scripts/bbot_would_send_prod_rotate.py (data['top10'][*]['base_coin'])
    and also accepts a list of strings or a 'coin' key. Duplicates keep the first row.
    """
    raw: object = None
    for key in TOP10_LIST_KEYS:
        if data.get(key):
            raw = data.get(key)
            break
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise RuntimeError("top10 is not a list")
    coins: list[str] = []
    stds: dict[str, Optional[float]] = {}
    means: dict[str, Optional[float]] = {}
    seen: set[str] = set()
    for item in raw:
        extra: Mapping[str, object] = {}
        if isinstance(item, str):
            coin = item.strip().upper()
        elif isinstance(item, Mapping):
            extra = item
            coin = ""
            for key in TOP10_COIN_KEYS:
                coin = str(item.get(key, "")).strip().upper()
                if coin:
                    break
        else:
            continue
        if not coin or coin in seen:
            continue
        seen.add(coin)
        coins.append(coin)
        stds[coin] = _first_float(extra, STD_BPS_KEYS) if extra else None
        means[coin] = _first_float(extra, MEAN_BPS_KEYS) if extra else None
    if not coins:
        raise RuntimeError("empty top10")
    return coins, stds, means


def stamp_matches_today(meta: Mapping[str, object], today: str) -> bool:
    """True when stamp or window_end_msk is today's MSK calendar date.

    Accepts 'YYYY-MM-DD…' (VPS sidecar) and compact 'YYYYMMDD…'.
    """
    compact = today.replace("-", "")
    for key in TOP10_STAMP_KEYS:
        raw = meta.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        if text.startswith(today) or text.startswith(compact):
            return True
    return False


def load_top10(paths: Paths) -> tuple[list[str], dict, Path, dict[str, Optional[float]], dict[str, Optional[float]]]:
    last_exc: Optional[BaseException] = None
    for path in paths.top10_candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise RuntimeError(f"top10 json is not an object: {path}")
            coins, stds, means = parse_top10(data)
            return coins, data, path, stds, means
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise RuntimeError(f"no usable top10 json: {last_exc}")


def find_meta(coin: str, sources: Sequence[tuple[str, Path]]) -> tuple[Optional[dict[str, str]], str]:
    """Row with exchange metadata, taken from existing local artifacts only (no REST)."""
    coin_u = coin.upper()
    expected_okx = f"{coin_u}-USDT-SWAP"
    expected_bybit = f"{coin_u}USDT"
    for src, path in sources:
        try:
            rows = read_csv_rows(path)
        except OSError:
            continue
        except csv.Error:
            continue
        for raw in rows:
            if str(raw.get("base_coin", "")).strip().upper() != coin_u:
                continue
            row = {k: str(raw.get(k, "")).strip() for k in FIELDS if k != "discovered_at_utc"}
            row["base_coin"] = coin_u
            if row.get("okx_symbol") != expected_okx or row.get("bybit_symbol") != expected_bybit:
                continue
            if not all(pos(row.get(k, "")) for k in LOT_TICK):
                continue
            return row, src
    return None, "no_complete_meta"


def current_pool(delta_text: str) -> list[str]:
    rows = list(csv.DictReader(io.StringIO(delta_text)))
    return [str(r.get("base_coin", "")).strip().upper() for r in rows if r.get("base_coin")]


def header_fields(first_line: str) -> list[str]:
    return [part.strip().lstrip("\ufeff") for part in first_line.strip().split(",")]


def launcher_is_dyn(paths: Paths, mark: str = LAUNCHER_MARK) -> bool:
    try:
        return mark in paths.last_launcher.read_text(encoding="utf-8")
    except OSError:
        return False


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_status(
    paths: Paths,
    *,
    mode: str,
    top10: Sequence[str],
    stamp: object,
    new_rows: Sequence[Mapping[str, str]],
    skipped: Sequence[Mapping[str, object]],
    pool: Sequence[str],
    reason: Optional[str],
) -> None:
    applied = (
        mode == "APPLY"
        and bool(new_rows)
        and str(reason or "")
        not in {"nothing_to_add", "bot_not_on_dyn_launcher", "stale_digest"}
    )
    try:
        cur = list(BASE) + current_pool(paths.delta.read_text(encoding="utf-8"))
    except OSError:
        cur = list(pool)
    payload = {
        "ts_msk": now_msk(),
        "mode": mode,
        "stamp": stamp,
        "top10": list(top10),
        "added": [r["base_coin"] for r in new_rows] if applied else [],
        "would_add": [r["base_coin"] for r in new_rows],
        "skipped": list(skipped),
        "reason": reason,
        "expected_pool_in_delta_file": cur,
        "expected_pool_count": len(cur),
        "pool_mode": "base12+daily_top10_append",
    }
    _atomic_write_text(paths.status, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def validate_own_delta(raw: str, settings: Settings) -> tuple[str, list[str]]:
    if not raw:
        raise ValueError("own delta is empty")
    first = raw.splitlines()[0] if raw else ""
    if header_fields(first) != FIELDS:
        raise ValueError(f"unexpected header: {first[:120]}")
    if not raw.endswith("\n"):
        raw += "\n"
    extras = current_pool(raw)
    if len(extras) != len(set(extras)) or set(extras) & set(settings.base):
        raise ValueError(f"dup/base overlap in own delta: {extras}")
    missing_seed = [c for c in settings.seed if c not in extras]
    if missing_seed:
        raise ValueError(f"seed coins missing from own delta (never-remove invariant): {missing_seed}")
    return raw, extras


def plan_additions(
    *,
    top10: Sequence[str],
    pool: Sequence[str],
    settings: Settings,
    meta_sources: Sequence[tuple[str, Path]],
    stds: Optional[Mapping[str, Optional[float]]] = None,
    is_crypto_fn: Optional[CryptoFn] = None,
    discovered_at: Optional[str] = None,
) -> tuple[list[dict[str, str]], list[dict[str, object]], list[str]]:
    crypto = is_crypto_fn or is_crypto
    std_map = stds or {}
    new_rows: list[dict[str, str]] = []
    skipped: list[dict[str, object]] = []
    already: list[str] = []
    cap_room = max(0, settings.max_total - len(pool))
    planned: set[str] = set()
    for coin in top10:
        if coin in pool or coin in planned:
            already.append(coin)
            continue
        if not crypto(coin):
            skipped.append({"coin": coin, "reason": "non_crypto"})
            continue
        if settings.max_std_bps is not None:
            std = std_map.get(coin)
            if std is None:
                skipped.append({"coin": coin, "reason": "std_missing"})
                continue
            if std > settings.max_std_bps:
                skipped.append(
                    {
                        "coin": coin,
                        "reason": f"std_bps_{std}>max_{settings.max_std_bps}",
                    }
                )
                continue
        row, msrc = find_meta(coin, meta_sources)
        if row is None:
            skipped.append({"coin": coin, "reason": msrc})
            continue
        if len(new_rows) >= cap_room:
            skipped.append({"coin": coin, "reason": f"cap_total_{settings.max_total}"})
            continue
        material = dict(row)
        material["discovered_at_utc"] = discovered_at or utc_iso()
        material["_meta_src"] = msrc
        new_rows.append(material)
        planned.add(coin)
    return new_rows, skipped, already


def append_delta_atomic(paths: Paths, raw: str, new_rows: Sequence[Mapping[str, str]]) -> str:
    """Append-only: previous bytes are a prefix of the new file. tmp+fsync+rename."""
    assert_delta_writable(paths.delta)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
    for row in new_rows:
        writer.writerow(row)
    new_text = raw + buf.getvalue()
    if not new_text.startswith(raw):
        raise RuntimeError("append-only invariant broken")
    tmp = paths.delta.with_suffix(".csv.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
            fh.flush()
            os.fsync(fh.fileno())
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = paths.pool_dir / f"{paths.delta.name}.bak-{stamp}"
        _atomic_write_text(bak, raw)
        os.replace(tmp, paths.delta)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return new_text


def _parse_args(argv: Optional[Sequence[str]]) -> Settings:
    parser = argparse.ArgumentParser(
        description="Append morning top-10 volatile coins to the gear23 canary-owned delta CSV."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the delta file (default: dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit dry-run (default). Ignored when --apply is set.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="append even if the running bot is not on the dyn launcher",
    )
    parser.add_argument(
        "--max-total",
        type=int,
        default=int(os.environ.get("POOL_DYN_MAX_TOTAL", DEFAULT_MAX_TOTAL)),
        help="hard cap on TOTAL pool size (base+extras)",
    )
    parser.add_argument("--allow-stale-digest", action="store_true")
    parser.add_argument(
        "--max-std-bps",
        type=float,
        default=(
            float(os.environ["POOL_DYN_MAX_STD_BPS"])
            if os.environ.get("POOL_DYN_MAX_STD_BPS", "").strip()
            else None
        ),
        help="optional default-off sanity guard: skip a top-10 coin whose std bps exceeds this",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.max_total < len(BASE) + len(SEED):
        parser.error(
            f"--max-total must be >= {len(BASE) + len(SEED)} (base+seed), got {args.max_total}"
        )
    if args.max_std_bps is not None and args.max_std_bps <= 0:
        parser.error("--max-std-bps must be > 0 when set")
    return Settings(
        apply=bool(args.apply),
        force=bool(args.force),
        max_total=int(args.max_total),
        allow_stale_digest=bool(args.allow_stale_digest),
        max_std_bps=args.max_std_bps,
    )


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    paths: Optional[Paths] = None,
    now: Optional[datetime] = None,
    is_crypto_fn: Optional[CryptoFn] = None,
) -> int:
    settings = _parse_args(argv)
    resolved = paths if paths is not None else paths_from_env()
    mode = "APPLY" if settings.apply else "DRY-RUN"
    resolved.pool_dir.mkdir(parents=True, exist_ok=True)

    try:
        assert_delta_writable(resolved.delta)
    except DeltaPathError as exc:
        log(f"ABORT {exc}", resolved)
        return 2

    lock_path = resolved.pool_dir / "append.lock"
    lockf = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another instance holds the lock; exit", resolved)
        lockf.close()
        return 4

    log(f"start mode={mode} max_total={settings.max_total}", resolved)
    try:
        try:
            raw = resolved.delta.read_text(encoding="utf-8")
        except OSError as exc:
            log(f"ABORT own delta missing/unreadable ({exc}); will not recreate it", resolved)
            return 2
        try:
            raw, extras = validate_own_delta(raw, settings)
        except ValueError as exc:
            log(f"ABORT {exc}", resolved)
            return 2
        pool = list(settings.base) + extras

        try:
            top10, meta, src, stds, _means = load_top10(resolved)
        except RuntimeError as exc:
            log(f"ABORT {exc}", resolved)
            return 3
        stamp = meta.get("stamp")
        today = today_msk(now)
        stale = not stamp_matches_today(meta, today)
        log(
            f"top10 src={src} stamp={stamp} window_end={meta.get('window_end_msk')} top10={top10}",
            resolved,
        )
        if stale and not settings.allow_stale_digest:
            log(
                f"SKIP stale digest (stamp {stamp} != today {today} MSK); nothing appended",
                resolved,
            )
            write_status(
                resolved,
                mode=mode,
                top10=top10,
                stamp=stamp,
                new_rows=[],
                skipped=[],
                pool=pool,
                reason="stale_digest",
            )
            return 3

        new_rows, skipped, already = plan_additions(
            top10=top10,
            pool=pool,
            settings=settings,
            meta_sources=resolved.meta_sources,
            stds=stds,
            is_crypto_fn=is_crypto_fn,
            discovered_at=utc_iso(now),
        )
        log(f"already_in_pool={already}", resolved)
        log(
            f"WOULD_ADD={[r['base_coin'] for r in new_rows]} (meta src: "
            f"{ {r['base_coin']: r.get('_meta_src') for r in new_rows} }) skipped={skipped} "
            f"pool_now={len(pool)} pool_after={len(pool) + len(new_rows)} cap={settings.max_total}",
            resolved,
        )

        reason: Optional[str] = None
        if settings.apply and not new_rows:
            reason = "nothing_to_add"
        elif settings.apply and not launcher_is_dyn(resolved, settings.launcher_mark) and not settings.force:
            reason = "bot_not_on_dyn_launcher"
            log(
                "SKIP apply: running bot is not on the pool-dyn launcher (last_launcher.txt); "
                "would append once it is (use --force to override)",
                resolved,
            )
        elif settings.apply:
            append_delta_atomic(resolved, raw, new_rows)
            pool = pool + [r["base_coin"] for r in new_rows]
            log(
                f"APPENDED {[r['base_coin'] for r in new_rows]} -> {resolved.delta} "
                f"(pool_total={len(pool)})",
                resolved,
            )
            try:
                with resolved.events.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "ts_msk": now_msk(now),
                                "event": "append",
                                "added": [r["base_coin"] for r in new_rows],
                                "pool_total": len(pool),
                                "top10": top10,
                                "stamp": stamp,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except OSError:
                pass
        write_status(
            resolved,
            mode=mode,
            top10=top10,
            stamp=stamp,
            new_rows=new_rows,
            skipped=skipped,
            pool=pool,
            reason=reason,
        )
        return 0
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
        except OSError:
            pass
        lockf.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

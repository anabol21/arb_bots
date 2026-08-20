#!/usr/bin/env python3
"""Read-only validator for ``bbot.private.journal.v1`` JSONL journals.

This command never opens a network connection, loads environment files, or
writes journal/state data.  It intentionally reports aggregate diagnostics
only: event payloads and validation exception text are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional


DEFAULT_JOURNAL_ROOT = Path("/data/bbot/private/journal")
STUB_JOURNAL_ROOT = Path("/data/bbot/journal")
D_DENY_ROOTS = (
    Path("/data/live"),
    Path("/data/bars"),
    Path("/data/compacted"),
    Path("/data/spool"),
)
SCHEMA_VERSION = "bbot.private.journal.v1"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UTC_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return Path(os.path.abspath(path))


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_denied(path: Path) -> bool:
    return _is_under(path, STUB_JOURNAL_ROOT) or any(
        _is_under(path, root) for root in D_DENY_ROOTS
    )


def _load_contract_validator() -> Optional[tuple[Any, Any]]:
    """Import the authoritative validator when this checkout provides it."""
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from app.bot.private.journal_v1 import read_events_jsonl, validate_event_stream
    except (ImportError, ModuleNotFoundError):
        return None
    return read_events_jsonl, validate_event_stream


def _journal_root(target: Path) -> Optional[Path]:
    if target.name == "journal":
        return target
    if target.name == "events.jsonl" and target.parent.name.startswith("event_date="):
        return target.parent.parent
    return None


def _journal_files(root: Path) -> Iterable[Path]:
    """Yield canonical v1 event files from a validated journal root."""
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith("event_date="):
            continue
        candidate = child / "events.jsonl"
        if candidate.is_file() and not candidate.is_symlink():
            yield candidate


def _layout_diagnostics(root: Path) -> Counter[str]:
    """Validate R3's canonical tree without reading any event payloads."""
    diagnostics: Counter[str] = Counter()
    if root.name != "journal" or not root.is_dir() or root.is_symlink():
        return Counter({"path_layout": 1})
    try:
        root_entries = list(root.iterdir())
    except OSError:
        return Counter({"unreadable_layout": 1})
    for entry in root_entries:
        if entry.name == ".approval.lock":
            if not entry.is_file() or entry.is_symlink():
                diagnostics["invalid_lock_artifact"] += 1
            continue
        if not entry.is_dir() or entry.is_symlink() or not entry.name.startswith("event_date="):
            if entry.suffix == ".jsonl":
                diagnostics["unapproved_jsonl_sidecar"] += 1
            else:
                diagnostics["unapproved_root_artifact"] += 1
            continue
        try:
            partition_entries = list(entry.iterdir())
        except OSError:
            diagnostics["unreadable_partition"] += 1
            continue
        for candidate in partition_entries:
            if (
                candidate.name == "events.jsonl"
                and candidate.is_file()
                and not candidate.is_symlink()
            ):
                continue
            if candidate.suffix == ".jsonl":
                diagnostics["unapproved_jsonl_sidecar"] += 1
            else:
                diagnostics["unapproved_partition_artifact"] += 1
        if not any(
            item.name == "events.jsonl" and item.is_file() and not item.is_symlink()
            for item in partition_entries
        ):
            diagnostics["missing_events_jsonl"] += 1
    return diagnostics


def _auth_presence_is_compatible(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    current = {"credentials_configured"}
    legacy = {"api_key_present", "api_secret_present", "passphrase_present"}
    keys = set(value)
    return (
        keys == current or keys == legacy
    ) and all(isinstance(item, bool) for item in value.values())


def _normalize_for_imported_contract(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Adapt documented compatibility-only forms for the older importable checker."""
    if event.get("event_type") == "pre_send_gate":
        return None
    normalized = dict(event)
    presence = normalized.get("credential_presence")
    if normalized.get("event_type") == "auth" and isinstance(presence, dict):
        legacy = {"api_key_present", "api_secret_present", "passphrase_present"}
        if set(presence) == legacy and all(isinstance(item, bool) for item in presence.values()):
            normalized["credential_presence"] = {
                "credentials_configured": all(presence.values())
            }
    return normalized


def _pre_send_gate_is_canonical(event: dict[str, Any]) -> bool:
    common = {
        "schema_version",
        "event_id",
        "event_type",
        "event_date",
        "event_ts_utc",
        "event_monotonic_ns",
        "run_id",
        "operation_id",
        "event_seq",
        "venue",
        "environment",
        "outcome",
    }
    return (
        set(event) == common | {"gate_kind", "gate_decision"}
        and event.get("schema_version") == SCHEMA_VERSION
        and event.get("event_type") == "pre_send_gate"
        and event.get("outcome") == "observed"
        and event.get("gate_kind") in {"rest", "price"}
        and event.get("gate_decision") == "blocked"
        and all(event.get(key) is not None for key in common)
        and all(
            isinstance(event.get(key), str) and event[key]
            for key in ("event_id", "run_id", "operation_id")
        )
        and isinstance(event.get("event_seq"), int)
        and not isinstance(event.get("event_seq"), bool)
        and event["event_seq"] >= 1
        and isinstance(event.get("event_monotonic_ns"), int)
        and not isinstance(event.get("event_monotonic_ns"), bool)
        and event["event_monotonic_ns"] >= 0
        and isinstance(event.get("event_date"), str)
        and _DATE_RE.fullmatch(event["event_date"]) is not None
        and isinstance(event.get("event_ts_utc"), str)
        and _UTC_TS_RE.fullmatch(event["event_ts_utc"]) is not None
        and event["event_ts_utc"][:10] == event["event_date"]
        and event.get("venue") in {"bybit", "okx"}
        and event.get("environment") in {"testnet", "demo", "live"}
    )


def _is_legacy_pre_send_pair(
    operation_events: list[tuple[int, dict[str, Any]]],
) -> bool:
    """Recognize only the documented read-only legacy pre-dispatch pair."""
    if len(operation_events) != 2:
        return False
    _, reject = operation_events[0]
    _, reconciliation = operation_events[1]
    common = {
        "schema_version",
        "event_id",
        "event_type",
        "event_date",
        "event_ts_utc",
        "event_monotonic_ns",
        "run_id",
        "operation_id",
        "event_seq",
        "venue",
        "environment",
        "outcome",
    }
    reject_allowed = common | {
        "dual_leg_id",
        "leg_id",
        "request_kind",
        "request_fingerprint",
        "reject_stage",
        "error_code",
    }
    reconciliation_allowed = common | {
        "dual_leg_id",
        "leg_id",
        "reconciliation_scope",
        "reconciliation_state",
        "mismatch_fields",
        "error_code",
    }
    order_id_keys = {
        "order_id",
        "exchange_order_id",
        "client_order_id",
        "clordid",
    }

    def _common_shape(event: dict[str, Any]) -> bool:
        return (
            event.get("schema_version") == SCHEMA_VERSION
            and all(event.get(key) is not None for key in common)
            and all(
                isinstance(event.get(key), str) and event[key]
                for key in ("event_id", "run_id", "operation_id")
            )
            and isinstance(event.get("event_seq"), int)
            and not isinstance(event.get("event_seq"), bool)
            and event["event_seq"] >= 1
            and isinstance(event.get("event_monotonic_ns"), int)
            and not isinstance(event.get("event_monotonic_ns"), bool)
            and event["event_monotonic_ns"] >= 0
            and isinstance(event.get("event_date"), str)
            and _DATE_RE.fullmatch(event["event_date"]) is not None
            and isinstance(event.get("event_ts_utc"), str)
            and _UTC_TS_RE.fullmatch(event["event_ts_utc"]) is not None
            and event["event_ts_utc"][:10] == event["event_date"]
            and event.get("venue") in {"bybit", "okx"}
            and event.get("environment") in {"testnet", "demo", "live"}
        )

    return (
        set(reject).issubset(reject_allowed)
        and set(reconciliation).issubset(reconciliation_allowed)
        and not (set(reject) | set(reconciliation)) & order_id_keys
        and _common_shape(reject)
        and _common_shape(reconciliation)
        and reject.get("event_type") == "reject"
        and reject.get("outcome") == "failure"
        and reject.get("reject_stage") == "auth"
        and reject.get("error_code") == "invalid_request"
        and reconciliation.get("event_type") == "reconciliation"
        and reconciliation.get("outcome") == "observed"
        and reconciliation.get("reconciliation_scope") == "order_state"
        and reconciliation.get("reconciliation_state") == "inconclusive"
    )


def _legacy_pre_send_indices(events: list[dict[str, Any]]) -> set[int]:
    """Return only exact legacy-pair records to supersede in memory."""
    operations: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, event in enumerate(events):
        operations.setdefault(str(event.get("operation_id", "")), []).append((index, event))
    return {
        index
        for operation_events in operations.values()
        if _is_legacy_pre_send_pair(operation_events)
        for index, _ in operation_events
    }


def _normalize_stream_for_imported_contract(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply only documented read-only compatibility before strict validation."""
    superseded = _legacy_pre_send_indices(events)
    return [
        normalized
        for index, event in enumerate(events)
        if index not in superseded
        if (normalized := _normalize_for_imported_contract(event)) is not None
    ]


def _r3_stream_diagnostics(events: list[dict[str, Any]]) -> Counter[str]:
    """Check R4 compatibility, gate isolation, approval, and recovery semantics."""
    diagnostics: Counter[str] = Counter()
    operations: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    seen_event_ids: set[str] = set()

    for position, event in enumerate(events):
        if event.get("event_type") == "auth" and not _auth_presence_is_compatible(
            event.get("credential_presence")
        ):
            diagnostics["auth_presence_compatibility"] += 1
        if (
            event.get("event_type") == "pre_send_gate"
            and not _pre_send_gate_is_canonical(event)
        ):
            diagnostics["pre_send_gate_shape"] += 1
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in seen_event_ids:
            diagnostics["event_identity"] += 1
        else:
            seen_event_ids.add(event_id)
        operation_id = str(event.get("operation_id", ""))
        operations.setdefault(operation_id, []).append((position, event))

    for operation_events in operations.values():
        legacy_pre_send = _is_legacy_pre_send_pair(operation_events)
        has_auth_reject = any(
            event.get("event_type") == "reject"
            and event.get("reject_stage") == "auth"
            for _, event in operation_events
        )
        has_reconciliation = any(
            event.get("event_type") == "reconciliation"
            for _, event in operation_events
        )
        has_order_prepared = any(
            event.get("event_type") == "order_prepared"
            for _, event in operation_events
        )
        has_gate = any(
            event.get("event_type") == "pre_send_gate" for _, event in operation_events
        )
        if has_gate and any(
            event.get("event_type")
            not in {"operator_approval", "pre_send_gate"}
            for _, event in operation_events
        ):
            diagnostics["pre_send_gate_lifecycle"] += 1
        has_request_sent = any(
            event.get("event_type") == "request_sent" for _, event in operation_events
        )
        if (
            has_auth_reject
            and has_reconciliation
            and not has_order_prepared
            and not has_request_sent
            and not legacy_pre_send
        ):
            diagnostics["legacy_pre_send_variation"] += 1
        if (
            not legacy_pre_send
            and not has_request_sent
            and any(
                event.get("event_type") == "reconciliation"
                and event.get("reconciliation_scope") == "order_state"
                and event.get("reconciliation_state") == "inconclusive"
                for _, event in operation_events
            )
        ):
            diagnostics["pre_dispatch_reconciliation"] += 1
        for index, (_, event) in enumerate(operation_events):
            if (
                event.get("event_type") == "order_prepared"
                and event.get("environment") == "live"
                and (
                    index == 0
                    or operation_events[index - 1][1].get("event_type")
                    != "operator_approval"
                    or operation_events[index - 1][1].get("approval_action") != "consumed"
                )
            ):
                diagnostics["live_approval_ordering"] += 1
        prepared = next(
            (event for _, event in operation_events if event.get("event_type") == "order_prepared"),
            None,
        )
        is_post_only = bool(prepared and prepared.get("post_only"))
        sent_positions = [
            index
            for index, (_, event) in enumerate(operation_events)
            if event.get("event_type") == "request_sent"
        ]
        for sent_index in sent_positions:
            following = [event for _, event in operation_events[sent_index + 1 :]]
            resolved = any(
                event.get("event_type") in {"ack_received", "reject", "terminal_update"}
                for event in following
            )
            ambiguity_recovery = any(
                event.get("event_type") == "reconciliation"
                and event.get("reconciliation_scope") == "post_dispatch_ambiguity"
                and event.get("reconciliation_state") == "inconclusive"
                for event in following
            )
            if not resolved and not ambiguity_recovery:
                diagnostics["open_dispatch_requires_recovery"] += 1

        ttl_cancel_positions = [
            index
            for index, (_, event) in enumerate(operation_events)
            if event.get("event_type") == "cancel_requested"
            and event.get("cancel_reason") == "post_only_ttl_expired"
        ]
        for cancel_index in ttl_cancel_positions:
            following = [event for _, event in operation_events[cancel_index + 1 :]]
            has_terminal_path = any(
                event.get("event_type") in {"cancel_ack", "terminal_update"}
                for event in following
            )
            recovery = [
                event
                for event in following
                if event.get("event_type") == "reconciliation"
                and event.get("reconciliation_scope") == "post_only_ttl_recovery"
            ]
            if not is_post_only or not has_terminal_path or not recovery:
                diagnostics["post_only_recovery"] += 1
                continue
            if any(
                event.get("reconciliation_state") == "mismatch"
                and (
                    event.get("outcome") != "failure"
                    or event.get("error_code") != "reconciliation_mismatch"
                )
                for event in recovery
            ):
                diagnostics["post_only_recovery"] += 1
    return diagnostics


def _fallback_validate_file(path: Path) -> tuple[int, Counter[str]]:
    """Fail-closed minimum checks if the importable contract is unavailable."""
    failures: Counter[str] = Counter()
    try:
        raw = path.read_bytes()
    except OSError:
        return 0, Counter({"unreadable": 1})
    if raw and not raw.endswith(b"\n"):
        failures["incomplete_line"] += 1
        return 0, failures
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return 0, Counter({"invalid_utf8": 1})

    events = 0
    previous_by_run: dict[str, tuple[int, int, str]] = {}
    seen_ids: set[str] = set()
    for line in lines:
        if not line.strip():
            failures["blank_line"] += 1
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            failures["invalid_json"] += 1
            continue
        if not isinstance(event, dict):
            failures["non_object"] += 1
            continue
        if event.get("schema_version") != SCHEMA_VERSION:
            failures["invalid_version"] += 1
            continue
        required = {
            "event_id",
            "event_type",
            "event_date",
            "event_ts_utc",
            "event_monotonic_ns",
            "run_id",
            "operation_id",
            "event_seq",
            "venue",
            "environment",
            "outcome",
        }
        if not required.issubset(event) or any(event[key] is None for key in required):
            failures["missing_common_field"] += 1
            continue
        if _contains_redaction_name(event):
            failures["redaction"] += 1
            continue
        event_id, run_id = event["event_id"], event["run_id"]
        seq, mono, ts = (
            event["event_seq"],
            event["event_monotonic_ns"],
            event["event_ts_utc"],
        )
        if (
            not isinstance(event_id, str)
            or event_id in seen_ids
            or not isinstance(run_id, str)
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or not isinstance(mono, int)
            or isinstance(mono, bool)
            or not isinstance(ts, str)
            or not ts.endswith("Z")
        ):
            failures["identity_or_chronology"] += 1
            continue
        prior = previous_by_run.get(run_id)
        if prior is not None and (seq <= prior[0] or mono <= prior[1] or ts < prior[2]):
            failures["chronology"] += 1
            continue
        seen_ids.add(event_id)
        previous_by_run[run_id] = (seq, mono, ts)
        events += 1
    return events, failures


def _contains_redaction_name(value: Any) -> bool:
    """Conservative key-only fallback; full contract validation is preferred."""
    denied = {
        "api_key",
        "api_secret",
        "secret",
        "passphrase",
        "password",
        "authorization",
        "cookie",
        "signature",
        "access_token",
        "refresh_token",
        "private_key",
        "account_id",
        "order_id",
        "balance",
        "price",
        "quantity",
        "notional",
        "fee",
    }
    if isinstance(value, dict):
        return any(
            str(key).lower().replace("-", "_") in denied
            or _contains_redaction_name(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_redaction_name(item) for item in value)
    return False


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only aggregate validator for bbot.private.journal.v1.",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_JOURNAL_ROOT,
        help="private journal root or canonical events.jsonl path "
        "(default: /data/bbot/private/journal)",
    )
    parser.add_argument(
        "--allow-local-fixture",
        action="store_true",
        help="allow an explicit non-VPS, non-secret fixture path; never use for VPS journals",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    target = _resolve(args.path)
    default = _resolve(DEFAULT_JOURNAL_ROOT)
    is_default = target == default

    if _is_denied(target):
        print("FAIL path_isolation=denied_root")
        return 2
    if (
        not _is_under(target, default)
        and not is_default
        and not args.allow_local_fixture
    ):
        print("FAIL path_isolation=explicit_path_requires_allow_local_fixture")
        return 2
    journal_root = _journal_root(target)
    if journal_root is None or not target.exists() or not journal_root.exists():
        print("FAIL path_layout=not_canonical_private_v1")
        return 2

    failures = _layout_diagnostics(journal_root)
    if failures:
        details = ",".join(f"{name}:{count}" for name, count in sorted(failures.items()))
        print(f"FAIL files=0 events=0 contract=not_run diagnostics={details}")
        return 2

    files = list(_journal_files(journal_root))
    if not files:
        print("PASS files=0 events=0 contract=available_or_not_required diagnostics=none")
        return 0

    contract = _load_contract_validator()
    totals: Counter[str] = Counter()
    all_events: list[dict[str, Any]] = []
    events_by_file: list[tuple[Path, list[dict[str, Any]]]] = []
    for file_path in files:
        if contract is not None:
            try:
                events = contract[0](file_path)
            except Exception:
                failures["contract_validation"] += 1
            else:
                totals["events"] += len(events)
                all_events.extend(events)
                events_by_file.append((file_path, events))
        else:
            events, file_failures = _fallback_validate_file(file_path)
            totals["events"] += events
            failures.update(file_failures)

    if contract is not None and not failures:
        try:
            all_contract_events = _normalize_stream_for_imported_contract(all_events)
            for file_path, events in events_by_file:
                partition = file_path.parent.name.split("=", 1)[1]
                if any(event.get("event_date") != partition for event in events):
                    raise ValueError("partition date mismatch")
            contract[1](all_contract_events)
        except Exception:
            failures["cross_file_contract"] += 1
    elif contract is None:
        # R3 approval and recovery semantics require the authoritative contract.
        failures["contract_unavailable"] += 1

    if contract is not None and not failures:
        failures.update(_r3_stream_diagnostics(all_events))
        requires_lock = any(
            event.get("event_type") == "operator_approval"
            or (
                event.get("event_type") == "order_prepared"
                and event.get("environment") == "live"
            )
            for event in all_events
        )
        if requires_lock and not (journal_root / ".approval.lock").is_file():
            failures["approval_lock_missing"] += 1

    contract_status = "imported" if contract is not None else "fallback"
    if failures:
        details = ",".join(f"{name}:{count}" for name, count in sorted(failures.items()))
        print(
            f"FAIL files={len(files)} events={totals['events']} "
            f"contract={contract_status} diagnostics={details}"
        )
        return 1
    print(
        f"PASS files={len(files)} events={totals['events']} "
        f"contract={contract_status} diagnostics=none"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

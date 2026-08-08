#!/usr/bin/env python3
"""Freeze and audit the denominator of a live-forward football cohort.

The scope is immutable.  While the cohort is active, every user-requested fixture is
registered in an append-only, hash-chained event log before analysis starts.  At closure
the log is reconciled against the immutable record manifest: every requested fixture must
either have exactly one archived record or an explicit terminal unavailable disposition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCOPE_SCHEMA_VERSION = "forward-cohort-scope/1.0.0"
EVENT_SCHEMA_VERSION = "forward-cohort-denominator-event/1.0.0"
DENOMINATOR_SCHEMA_VERSION = "forward-cohort-denominator/1.0.0"
ESTIMAND = "distinct_user_requested_fixtures"
TERMINAL_UNAVAILABLE_REASONS = frozenset(
    {
        "fixture_not_found",
        "kickoff_already_started",
        "source_unavailable",
        "fixture_identity_conflict",
        "competition_out_of_scope",
        "postponed_without_replacement",
        "cancelled",
    }
)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class CohortScopeError(ValueError):
    """Raised when a cohort denominator cannot be reproduced."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _require_hash(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise CohortScopeError(f"{label} must be a lowercase SHA-256 identity")
    return text


def _require_token(value: Any, label: str) -> str:
    text = str(value or "")
    if not _TOKEN.fullmatch(text) or text in {".", ".."} or ".." in text:
        raise CohortScopeError(f"{label} is not a safe portable token")
    return text


def _aware(value: Any, label: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CohortScopeError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CohortScopeError(f"{label} must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def scope_directory(base_dir: str | Path) -> Path:
    return Path(base_dir).resolve() / ".codex" / "soccer-predict" / "forward-scopes"


def denominator_event_path(base_dir: str | Path, cohort_id: str) -> Path:
    return (
        scope_directory(base_dir)
        / f"{_require_token(cohort_id, 'cohort_id')}-events.ndjson"
    )


@contextmanager
def _event_log_lock(path: Path):
    """Serialize the complete read/validate/append event-log transaction."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_scope(
    *,
    scope_id: str,
    competition_keys: Sequence[str],
    starts_at: str | datetime,
    ends_at: str | datetime | None = None,
) -> dict[str, Any]:
    clean_scope_id = _require_token(scope_id, "scope_id")
    competitions = sorted(
        {_require_token(item, "competition_key") for item in competition_keys}
    )
    if not competitions:
        raise CohortScopeError("scope requires at least one competition_key")
    started = _aware(starts_at, "starts_at")
    ended = _aware(ends_at, "ends_at") if ends_at is not None else None
    if ended is not None and ended <= started:
        raise CohortScopeError("scope ends_at must be later than starts_at")
    value: dict[str, Any] = {
        "schema_version": SCOPE_SCHEMA_VERSION,
        "artifact_type": "soccer_live_forward_cohort_scope",
        "scope_id": clean_scope_id,
        "estimand": ESTIMAND,
        "competition_keys": competitions,
        "starts_at": started.replace(microsecond=0).isoformat(),
        "ends_at": ended.replace(microsecond=0).isoformat() if ended else None,
        "inclusion_policy": {
            "request_must_precede_analysis_archive": True,
            "request_must_precede_kickoff": True,
            "one_denominator_entry_per_fixture": True,
            "all_requests_require_record_or_terminal_unavailable": True,
            "postponed_fixture_keeps_identity_until_cancelled_or_replaced": True,
        },
    }
    value["scope_hash"] = _hash_json(value)
    return value


def validate_scope(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CohortScopeError("cohort scope must be an object")
    value = deepcopy(dict(raw))
    supplied = value.pop("scope_hash", None)
    if supplied != _hash_json(value):
        raise CohortScopeError("cohort scope hash is invalid")
    required = {
        "schema_version",
        "artifact_type",
        "scope_id",
        "estimand",
        "competition_keys",
        "starts_at",
        "ends_at",
        "inclusion_policy",
    }
    if set(value) != required:
        raise CohortScopeError("cohort scope fields are incomplete")
    if value.get("schema_version") != SCOPE_SCHEMA_VERSION:
        raise CohortScopeError("unsupported cohort scope schema_version")
    if value.get("artifact_type") != "soccer_live_forward_cohort_scope":
        raise CohortScopeError("cohort scope artifact_type is invalid")
    _require_token(value.get("scope_id"), "scope_id")
    if value.get("estimand") != ESTIMAND:
        raise CohortScopeError("cohort scope estimand is unsupported")
    competitions = value.get("competition_keys")
    if not isinstance(competitions, list) or not competitions:
        raise CohortScopeError("cohort scope competition_keys are missing")
    normalized = [_require_token(item, "competition_key") for item in competitions]
    if normalized != sorted(set(normalized)):
        raise CohortScopeError("cohort scope competition_keys are not canonical")
    started = _aware(value.get("starts_at"), "scope.starts_at")
    ended = (
        _aware(value.get("ends_at"), "scope.ends_at") if value.get("ends_at") else None
    )
    if ended is not None and ended <= started:
        raise CohortScopeError("cohort scope ends before it starts")
    expected_policy = build_scope(
        scope_id=str(value["scope_id"]),
        competition_keys=normalized,
        starts_at=started,
        ends_at=ended,
    )["inclusion_policy"]
    if value.get("inclusion_policy") != expected_policy:
        raise CohortScopeError("cohort scope inclusion_policy is not canonical")
    value["scope_hash"] = _require_hash(supplied, "scope_hash")
    return value


def load_scope(path: str | Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CohortScopeError("cohort scope file is unavailable or invalid") from exc
    return validate_scope(raw)


def _event_without_hash(
    *,
    event_type: str,
    cohort_id: str,
    scope: Mapping[str, Any],
    fixture_id: str,
    competition_key: str,
    home_team: str,
    away_team: str,
    kickoff: str | datetime,
    occurred_at: str | datetime,
    previous_event_hash: str | None,
    reason: str | None = None,
) -> dict[str, Any]:
    if event_type not in {"requested", "unavailable"}:
        raise CohortScopeError("denominator event_type is invalid")
    fixture = _require_token(fixture_id, "fixture_id")
    competition = _require_token(competition_key, "competition_key")
    frozen = validate_scope(scope)
    if competition not in frozen["competition_keys"]:
        raise CohortScopeError("fixture competition is outside the frozen scope")
    kickoff_at = _aware(kickoff, "kickoff")
    event_at = _aware(occurred_at, "occurred_at")
    if event_at >= kickoff_at:
        raise CohortScopeError("denominator event must be recorded before kickoff")
    starts_at = _aware(frozen["starts_at"], "scope.starts_at")
    ends_at = (
        _aware(frozen["ends_at"], "scope.ends_at") if frozen.get("ends_at") else None
    )
    if event_at < starts_at or (ends_at is not None and event_at >= ends_at):
        raise CohortScopeError("denominator event is outside the frozen scope window")
    clean_reason = None
    if event_type == "unavailable":
        clean_reason = str(reason or "").strip()
        if clean_reason not in TERMINAL_UNAVAILABLE_REASONS:
            raise CohortScopeError("terminal unavailable reason is not registered")
    elif reason is not None:
        raise CohortScopeError("requested events cannot carry an unavailable reason")
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_type": event_type,
        "cohort_id": _require_token(cohort_id, "cohort_id"),
        "scope_id": frozen["scope_id"],
        "scope_hash": frozen["scope_hash"],
        "fixture": {
            "fixture_id": fixture,
            "competition_key": competition,
            "home_team": " ".join(str(home_team or "").split()),
            "away_team": " ".join(str(away_team or "").split()),
            "kickoff": kickoff_at.replace(microsecond=0).isoformat(),
        },
        "occurred_at": event_at.replace(microsecond=0).isoformat(),
        "reason": clean_reason,
        "previous_event_hash": previous_event_hash,
    }


def validate_events(
    raw_events: Sequence[Any], *, scope: Mapping[str, Any], cohort_id: str
) -> list[dict[str, Any]]:
    frozen = validate_scope(scope)
    clean_cohort_id = _require_token(cohort_id, "cohort_id")
    normalized: list[dict[str, Any]] = []
    previous_hash: str | None = None
    requested: dict[str, dict[str, Any]] = {}
    unavailable: set[str] = set()
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, Mapping):
            raise CohortScopeError(f"denominator event {index} is invalid")
        value = deepcopy(dict(raw))
        supplied = value.pop("event_hash", None)
        if supplied != _hash_json(value):
            raise CohortScopeError(f"denominator event {index} hash is invalid")
        fixture = value.get("fixture")
        if not isinstance(fixture, Mapping):
            raise CohortScopeError(f"denominator event {index} fixture is invalid")
        expected = _event_without_hash(
            event_type=str(value.get("event_type") or ""),
            cohort_id=clean_cohort_id,
            scope=frozen,
            fixture_id=str(fixture.get("fixture_id") or ""),
            competition_key=str(fixture.get("competition_key") or ""),
            home_team=str(fixture.get("home_team") or ""),
            away_team=str(fixture.get("away_team") or ""),
            kickoff=str(fixture.get("kickoff") or ""),
            occurred_at=str(value.get("occurred_at") or ""),
            previous_event_hash=previous_hash,
            reason=value.get("reason"),
        )
        if value != expected:
            raise CohortScopeError(f"denominator event {index} does not replay")
        fixture_id = expected["fixture"]["fixture_id"]
        if expected["event_type"] == "requested":
            if fixture_id in requested:
                raise CohortScopeError("fixture was requested more than once")
            requested[fixture_id] = expected["fixture"]
        else:
            if fixture_id not in requested:
                raise CohortScopeError("unavailable disposition has no prior request")
            if (
                requested[fixture_id] != expected["fixture"]
                or fixture_id in unavailable
            ):
                raise CohortScopeError(
                    "unavailable disposition conflicts with its request"
                )
            unavailable.add(fixture_id)
        expected["event_hash"] = _require_hash(supplied, "event_hash")
        normalized.append(expected)
        previous_hash = supplied
    return normalized


def load_events(
    base_dir: str | Path, cohort_id: str, *, scope: Mapping[str, Any]
) -> list[dict[str, Any]]:
    path = denominator_event_path(base_dir, cohort_id)
    if not path.exists():
        return []
    raw_events: list[Any] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                raw_events.append(json.loads(line))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CohortScopeError(
            "denominator event log is unavailable or invalid"
        ) from exc
    return validate_events(raw_events, scope=scope, cohort_id=cohort_id)


def append_event(
    *,
    base_dir: str | Path,
    cohort_id: str,
    scope: Mapping[str, Any],
    event_type: str,
    fixture_id: str,
    competition_key: str,
    home_team: str,
    away_team: str,
    kickoff: str | datetime,
    occurred_at: str | datetime,
    reason: str | None = None,
) -> dict[str, Any]:
    path = denominator_event_path(base_dir, cohort_id)
    with _event_log_lock(path):
        events = load_events(base_dir, cohort_id, scope=scope)
        value = _event_without_hash(
            event_type=event_type,
            cohort_id=cohort_id,
            scope=scope,
            fixture_id=fixture_id,
            competition_key=competition_key,
            home_team=home_team,
            away_team=away_team,
            kickoff=kickoff,
            occurred_at=occurred_at,
            previous_event_hash=events[-1]["event_hash"] if events else None,
            reason=reason,
        )
        value["event_hash"] = _hash_json(value)
        validate_events([*events, value], scope=scope, cohort_id=cohort_id)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            handle.flush()
    return value


def request_binding(
    *, base_dir: str | Path, cohort_id: str, scope: Mapping[str, Any], fixture_id: str
) -> dict[str, Any]:
    events = load_events(base_dir, cohort_id, scope=scope)
    matches = [
        event for event in events if event["fixture"]["fixture_id"] == str(fixture_id)
    ]
    requests = [event for event in matches if event["event_type"] == "requested"]
    if len(requests) != 1:
        raise CohortScopeError(
            "fixture must have exactly one pre-analysis request event"
        )
    if any(event["event_type"] == "unavailable" for event in matches):
        raise CohortScopeError("terminally unavailable fixture cannot be archived")
    request = requests[0]
    return {
        "schema_version": "forward-cohort-request-binding/1.0.0",
        "scope_id": request["scope_id"],
        "scope_hash": request["scope_hash"],
        "fixture_id": request["fixture"]["fixture_id"],
        "request_event_hash": request["event_hash"],
        "requested_at": request["occurred_at"],
    }


def build_denominator(
    *,
    scope: Mapping[str, Any],
    cohort_id: str,
    events: Sequence[Any],
    record_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    frozen = validate_scope(scope)
    normalized = validate_events(events, scope=frozen, cohort_id=cohort_id)
    records = record_manifest.get("records")
    if not isinstance(records, list):
        raise CohortScopeError("record manifest records are missing")
    record_ids = [
        str(item.get("fixture_id") or "")
        for item in records
        if isinstance(item, Mapping)
    ]
    if len(record_ids) != len(records) or len(set(record_ids)) != len(record_ids):
        raise CohortScopeError("record manifest fixture identities are invalid")
    requests = {
        event["fixture"]["fixture_id"]: event
        for event in normalized
        if event["event_type"] == "requested"
    }
    unavailable = {
        event["fixture"]["fixture_id"]: event
        for event in normalized
        if event["event_type"] == "unavailable"
    }
    if set(record_ids) - set(requests):
        raise CohortScopeError("archived records exist outside the request denominator")
    for item in records:
        if not isinstance(item, Mapping):
            raise CohortScopeError("record manifest fixture identities are invalid")
        fixture_id = str(item.get("fixture_id") or "")
        if item.get("request_event_hash") != requests[fixture_id]["event_hash"]:
            raise CohortScopeError(
                "archived record request binding does not match the event log"
            )
    if set(record_ids) & set(unavailable):
        raise CohortScopeError("fixture cannot be both archived and unavailable")
    unresolved = sorted(set(requests) - set(record_ids) - set(unavailable))
    if unresolved:
        raise CohortScopeError(
            f"cohort denominator has unresolved fixtures: {unresolved}"
        )
    entries = []
    for fixture_id in sorted(requests):
        request = requests[fixture_id]
        disposition = "recorded" if fixture_id in record_ids else "unavailable"
        entries.append(
            {
                "fixture_id": fixture_id,
                "request_event_hash": request["event_hash"],
                "requested_at": request["occurred_at"],
                "disposition": disposition,
                "unavailable_event_hash": unavailable[fixture_id]["event_hash"]
                if disposition == "unavailable"
                else None,
                "unavailable_reason": unavailable[fixture_id]["reason"]
                if disposition == "unavailable"
                else None,
            }
        )
    value: dict[str, Any] = {
        "schema_version": DENOMINATOR_SCHEMA_VERSION,
        "artifact_type": "soccer_live_forward_cohort_denominator",
        "cohort_id": _require_token(cohort_id, "cohort_id"),
        "scope_id": frozen["scope_id"],
        "scope_hash": frozen["scope_hash"],
        "event_count": len(normalized),
        "last_event_hash": normalized[-1]["event_hash"] if normalized else None,
        "requested_fixture_count": len(entries),
        "recorded_fixture_count": len(record_ids),
        "unavailable_fixture_count": len(unavailable),
        "entries": entries,
        "complete": True,
    }
    value["denominator_hash"] = _hash_json(value)
    return value


def validate_denominator(
    raw: Any, *, scope: Mapping[str, Any], cohort_id: str
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CohortScopeError("cohort denominator must be an object")
    value = deepcopy(dict(raw))
    supplied = value.pop("denominator_hash", None)
    if supplied != _hash_json(value):
        raise CohortScopeError("cohort denominator hash is invalid")
    if (
        value.get("schema_version") != DENOMINATOR_SCHEMA_VERSION
        or value.get("complete") is not True
    ):
        raise CohortScopeError("cohort denominator is unsupported or incomplete")
    frozen = validate_scope(scope)
    if (
        value.get("cohort_id") != _require_token(cohort_id, "cohort_id")
        or value.get("scope_id") != frozen["scope_id"]
        or value.get("scope_hash") != frozen["scope_hash"]
    ):
        raise CohortScopeError("cohort denominator does not bind its scope/cohort")
    entries = value.get("entries")
    if not isinstance(entries, list) or [
        str(item.get("fixture_id") or "")
        for item in entries
        if isinstance(item, Mapping)
    ] != sorted(
        str(item.get("fixture_id") or "")
        for item in entries
        if isinstance(item, Mapping)
    ):
        raise CohortScopeError("cohort denominator entries are invalid")
    value["denominator_hash"] = _require_hash(supplied, "denominator_hash")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--scope-id", required=True)
    build.add_argument("--competition-key", action="append", required=True)
    build.add_argument("--starts-at", required=True)
    build.add_argument("--ends-at")
    build.add_argument("--output", required=True)
    for name in ("request", "unavailable"):
        command = sub.add_parser(name)
        command.add_argument("--scope-file", required=True)
        command.add_argument("--cohort-id", required=True)
        command.add_argument("--fixture-id", required=True)
        command.add_argument("--competition-key", required=True)
        command.add_argument("--home-team", required=True)
        command.add_argument("--away-team", required=True)
        command.add_argument("--kickoff", required=True)
        command.add_argument("--occurred-at", required=True)
        if name == "unavailable":
            command.add_argument(
                "--reason", required=True, choices=sorted(TERMINAL_UNAVAILABLE_REASONS)
            )
    verify = sub.add_parser("verify")
    verify.add_argument("--scope-file", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "build":
            artifact = build_scope(
                scope_id=args.scope_id,
                competition_keys=args.competition_key,
                starts_at=args.starts_at,
                ends_at=args.ends_at,
            )
            path = Path(args.output).resolve()
            if path.exists():
                if load_scope(path) != artifact:
                    raise CohortScopeError(
                        "scope output already contains different content"
                    )
            else:
                _atomic_json(path, artifact)
        elif args.command == "verify":
            path = Path(args.scope_file).resolve()
            artifact = load_scope(path)
        else:
            path = denominator_event_path(args.base_dir, args.cohort_id)
            artifact = append_event(
                base_dir=args.base_dir,
                cohort_id=args.cohort_id,
                scope=load_scope(args.scope_file),
                event_type="requested" if args.command == "request" else "unavailable",
                fixture_id=args.fixture_id,
                competition_key=args.competition_key,
                home_team=args.home_team,
                away_team=args.away_team,
                kickoff=args.kickoff,
                occurred_at=args.occurred_at,
                reason=getattr(args, "reason", None),
            )
        print(
            json.dumps(
                {"ok": True, "path": str(path), "artifact": artifact},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (CohortScopeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

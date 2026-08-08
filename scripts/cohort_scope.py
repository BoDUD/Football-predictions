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

SCOPE_SCHEMA_VERSION = "forward-cohort-scope/1.1.0"
PREVIOUS_SCOPE_SCHEMA_VERSION = "forward-cohort-scope/1.0.0"
EVENT_SCHEMA_VERSION = "forward-cohort-denominator-event/2.0.0"
PREVIOUS_EVENT_SCHEMA_VERSION = "forward-cohort-denominator-event/1.0.0"
PREVIOUS_FULL_REQUEST_BINDING_SCHEMA_VERSION = "forward-cohort-request-binding/2.0.0"
REQUEST_BINDING_SCHEMA_VERSION = "forward-cohort-request-binding/2.1.0"
PREVIOUS_REQUEST_BINDING_SCHEMA_VERSION = "forward-cohort-request-binding/1.0.0"
PREVIOUS_DENOMINATOR_SCHEMA_VERSION = "forward-cohort-denominator/2.0.0"
DENOMINATOR_SCHEMA_VERSION = "forward-cohort-denominator/2.1.0"
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
        "independent_model_unavailable",
        "archive_deadline_missed",
        "archive_pipeline_incompatible",
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
            "events_require_active_cohort_binding": True,
            "reschedules_and_replacements_are_explicit": True,
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
    schema_version = value.get("schema_version")
    if schema_version not in {PREVIOUS_SCOPE_SCHEMA_VERSION, SCOPE_SCHEMA_VERSION}:
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
    if schema_version == PREVIOUS_SCOPE_SCHEMA_VERSION:
        expected_policy = {
            key: expected_policy[key]
            for key in (
                "request_must_precede_analysis_archive",
                "request_must_precede_kickoff",
                "one_denominator_entry_per_fixture",
                "all_requests_require_record_or_terminal_unavailable",
                "postponed_fixture_keeps_identity_until_cancelled_or_replaced",
            )
        }
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


def _active_cohort_path(base_dir: str | Path) -> Path:
    return (
        Path(base_dir).resolve()
        / ".codex"
        / "soccer-predict"
        / "active-forward-cohort.json"
    )


def _load_active_event_binding(
    base_dir: str | Path,
    *,
    cohort_id: str,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    """Load the live pointer and bind an append to its frozen policy.

    This module deliberately performs a small self-contained validation instead of
    importing ``forward_policy`` (which imports this module).  The full policy is still
    validated by the caller before cohort start and again at record/closure time.
    """

    path = _active_cohort_path(base_dir)
    try:
        cohort = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CohortScopeError("active cohort is unavailable or invalid") from exc
    if not isinstance(cohort, Mapping):
        raise CohortScopeError("active cohort is invalid")
    value = deepcopy(dict(cohort))
    supplied_hash = value.pop("cohort_hash", None)
    if supplied_hash != _hash_json(value):
        raise CohortScopeError("active cohort hash is invalid")
    frozen = validate_scope(scope)
    if value.get("status") != "active":
        raise CohortScopeError("denominator events require an active cohort")
    if value.get("cohort_id") != _require_token(cohort_id, "cohort_id"):
        raise CohortScopeError("denominator event cohort_id is not active")
    if (
        value.get("scope_id") != frozen["scope_id"]
        or value.get("scope_hash") != frozen["scope_hash"]
    ):
        raise CohortScopeError("active cohort does not bind the supplied scope")
    policy_id = _require_token(value.get("policy_id"), "policy_id")
    policy_hash = _require_hash(value.get("policy_hash"), "policy_hash")
    policy_path = Path(str(value.get("policy_file") or ""))
    expected_root = (
        Path(base_dir).resolve() / ".codex" / "soccer-predict" / "forward-policies"
    )
    try:
        resolved_policy = policy_path.resolve(strict=True)
        resolved_policy.relative_to(expected_root)
        raw_policy = json.loads(resolved_policy.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise CohortScopeError(
            "active cohort policy is unavailable or non-canonical"
        ) from exc
    if (
        not isinstance(raw_policy, Mapping)
        or raw_policy.get("policy_id") != policy_id
        or raw_policy.get("policy_hash") != policy_hash
    ):
        raise CohortScopeError("active cohort policy binding is invalid")
    return {
        "cohort_hash": _require_hash(supplied_hash, "cohort_hash"),
        "policy_id": policy_id,
        "policy_hash": policy_hash,
        "starts_at": _aware(value.get("starts_at"), "cohort.starts_at").isoformat(),
        "policy_snapshot": deepcopy(dict(raw_policy)),
    }


def _event_without_hash(
    *,
    schema_version: str,
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
    cohort_binding: Mapping[str, Any] | None = None,
    replacement_fixture: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if schema_version not in {PREVIOUS_EVENT_SCHEMA_VERSION, EVENT_SCHEMA_VERSION}:
        raise CohortScopeError("denominator event schema_version is invalid")
    if schema_version == PREVIOUS_EVENT_SCHEMA_VERSION:
        allowed_types = {"requested", "unavailable"}
    else:
        allowed_types = {"requested", "unavailable", "rescheduled", "replaced"}
    if event_type not in allowed_types:
        raise CohortScopeError("denominator event_type is invalid")
    fixture = _require_token(fixture_id, "fixture_id")
    competition = _require_token(competition_key, "competition_key")
    frozen = validate_scope(scope)
    if competition not in frozen["competition_keys"]:
        raise CohortScopeError("fixture competition is outside the frozen scope")
    kickoff_at = _aware(kickoff, "kickoff")
    event_at = _aware(occurred_at, "occurred_at")
    if event_type == "requested" and event_at >= kickoff_at:
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
        raise CohortScopeError("non-terminal events cannot carry an unavailable reason")
    value: dict[str, Any] = {
        "schema_version": schema_version,
        "event_type": event_type,
        "cohort_id": _require_token(cohort_id, "cohort_id"),
        "scope_id": frozen["scope_id"],
        "scope_hash": frozen["scope_hash"],
        "fixture": {
            "fixture_id": fixture,
            "competition_key": competition,
            "home_team": " ".join(str(home_team or "").split()),
            "away_team": " ".join(str(away_team or "").split()),
            "kickoff": kickoff_at.isoformat(),
        },
        "occurred_at": event_at.isoformat(),
        "reason": clean_reason,
        "previous_event_hash": previous_event_hash,
    }
    if schema_version == EVENT_SCHEMA_VERSION:
        if not isinstance(cohort_binding, Mapping):
            raise CohortScopeError(
                "current denominator event lacks active cohort binding"
            )
        started = _aware(cohort_binding.get("starts_at"), "cohort.starts_at")
        if event_at < started:
            raise CohortScopeError("denominator event predates active cohort start")
        value.update(
            {
                "cohort_hash": _require_hash(
                    cohort_binding.get("cohort_hash"), "cohort_hash"
                ),
                "policy_id": _require_token(
                    cohort_binding.get("policy_id"), "policy_id"
                ),
                "policy_hash": _require_hash(
                    cohort_binding.get("policy_hash"), "policy_hash"
                ),
            }
        )
        if event_type == "replaced":
            if not isinstance(replacement_fixture, Mapping):
                raise CohortScopeError("replaced event requires replacement_fixture")
            replacement_kickoff = _aware(
                replacement_fixture.get("kickoff"), "replacement_fixture.kickoff"
            )
            if event_at >= replacement_kickoff:
                raise CohortScopeError(
                    "replacement must be registered before its kickoff"
                )
            replacement_competition = _require_token(
                replacement_fixture.get("competition_key"),
                "replacement_fixture.competition_key",
            )
            if replacement_competition not in frozen["competition_keys"]:
                raise CohortScopeError(
                    "replacement fixture is outside the frozen scope"
                )
            value["replacement_fixture"] = {
                "fixture_id": _require_token(
                    replacement_fixture.get("fixture_id"),
                    "replacement_fixture.fixture_id",
                ),
                "competition_key": replacement_competition,
                "home_team": " ".join(
                    str(replacement_fixture.get("home_team") or "").split()
                ),
                "away_team": " ".join(
                    str(replacement_fixture.get("away_team") or "").split()
                ),
                "kickoff": replacement_kickoff.isoformat(),
            }
            if (
                not value["replacement_fixture"]["home_team"]
                or not value["replacement_fixture"]["away_team"]
            ):
                raise CohortScopeError("replacement fixture teams are required")
        elif replacement_fixture is not None:
            raise CohortScopeError("only replaced events can carry replacement_fixture")
        if event_type == "rescheduled" and event_at >= kickoff_at:
            raise CohortScopeError(
                "reschedule must be registered before the rescheduled kickoff"
            )
    elif cohort_binding is not None or replacement_fixture is not None:
        raise CohortScopeError(
            "historical denominator events cannot carry current fields"
        )
    return value


def validate_events(
    raw_events: Sequence[Any],
    *,
    scope: Mapping[str, Any],
    cohort_id: str,
    cohort_binding: Mapping[str, Any] | None = None,
    required_schema_version: str | None = None,
) -> list[dict[str, Any]]:
    frozen = validate_scope(scope)
    clean_cohort_id = _require_token(cohort_id, "cohort_id")
    normalized: list[dict[str, Any]] = []
    previous_hash: str | None = None
    requests: dict[str, dict[str, Any]] = {}
    current_to_root: dict[str, str] = {}
    previous_occurred_at: datetime | None = None
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
        schema_version = str(value.get("schema_version") or "")
        if (
            required_schema_version is not None
            and schema_version != required_schema_version
        ):
            raise CohortScopeError(
                "denominator event schema_version does not match the frozen release contract"
            )
        effective_binding = (
            cohort_binding if schema_version == EVENT_SCHEMA_VERSION else None
        )
        if schema_version == EVENT_SCHEMA_VERSION and effective_binding is None:
            raise CohortScopeError(
                "current denominator events require the immutable cohort binding"
            )
        expected = _event_without_hash(
            schema_version=schema_version,
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
            cohort_binding=effective_binding,
            replacement_fixture=value.get("replacement_fixture"),
        )
        if value != expected:
            raise CohortScopeError(f"denominator event {index} does not replay")
        occurred_at = _aware(expected["occurred_at"], "occurred_at")
        if previous_occurred_at is not None and occurred_at < previous_occurred_at:
            raise CohortScopeError("denominator event timestamps cannot move backwards")
        previous_occurred_at = occurred_at
        fixture_id = expected["fixture"]["fixture_id"]
        event_type = expected["event_type"]
        if event_type == "requested":
            if fixture_id in requests or fixture_id in current_to_root:
                raise CohortScopeError("fixture was requested more than once")
            requests[fixture_id] = {
                "request_event": expected,
                "current_fixture": expected["fixture"],
                "fixture_event_hash": supplied,
                "fixture_event_at": expected["occurred_at"],
                "terminal": False,
            }
            current_to_root[fixture_id] = fixture_id
        elif event_type == "rescheduled":
            root = current_to_root.get(fixture_id)
            if root is None or requests[root]["terminal"]:
                raise CohortScopeError("rescheduled event has no active request")
            current = requests[root]["current_fixture"]
            if any(
                expected["fixture"][field] != current[field]
                for field in ("fixture_id", "competition_key", "home_team", "away_team")
            ):
                raise CohortScopeError("rescheduled event changes fixture identity")
            if expected["fixture"]["kickoff"] == current["kickoff"]:
                raise CohortScopeError("rescheduled event does not change kickoff")
            requests[root]["current_fixture"] = expected["fixture"]
            requests[root]["fixture_event_hash"] = supplied
            requests[root]["fixture_event_at"] = expected["occurred_at"]
        elif event_type == "replaced":
            root = current_to_root.get(fixture_id)
            if root is None or requests[root]["terminal"]:
                raise CohortScopeError("replaced event has no active request")
            if expected["fixture"] != requests[root]["current_fixture"]:
                raise CohortScopeError(
                    "replaced event does not bind the current fixture"
                )
            replacement = expected["replacement_fixture"]
            replacement_id = replacement["fixture_id"]
            if replacement_id in requests or replacement_id in current_to_root:
                raise CohortScopeError("replacement fixture identity is already in use")
            current_to_root.pop(fixture_id)
            current_to_root[replacement_id] = root
            requests[root]["current_fixture"] = replacement
            requests[root]["fixture_event_hash"] = supplied
            requests[root]["fixture_event_at"] = expected["occurred_at"]
        else:
            root = current_to_root.get(fixture_id)
            if root is None:
                raise CohortScopeError("unavailable disposition has no prior request")
            if (
                requests[root]["terminal"]
                or requests[root]["current_fixture"] != expected["fixture"]
            ):
                raise CohortScopeError(
                    "unavailable disposition conflicts with its request"
                )
            requests[root]["terminal"] = True
            requests[root]["terminal_event"] = expected
        expected["event_hash"] = _require_hash(supplied, "event_hash")
        normalized.append(expected)
        previous_hash = supplied
    return normalized


def load_events(
    base_dir: str | Path,
    cohort_id: str,
    *,
    scope: Mapping[str, Any],
    cohort_binding: Mapping[str, Any] | None = None,
    required_schema_version: str | None = None,
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
    return validate_events(
        raw_events,
        scope=scope,
        cohort_id=cohort_id,
        cohort_binding=cohort_binding,
        required_schema_version=required_schema_version,
    )


def _request_states(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    current_to_root: dict[str, str] = {}
    for event in events:
        event_type = str(event["event_type"])
        fixture = deepcopy(dict(event["fixture"]))
        fixture_id = str(fixture["fixture_id"])
        if event_type == "requested":
            states[fixture_id] = {
                "request_event": event,
                "request_fixture": fixture,
                "current_fixture": fixture,
                "fixture_event_hash": event["event_hash"],
                "fixture_event_at": event["occurred_at"],
                "terminal_event": None,
            }
            current_to_root[fixture_id] = fixture_id
        elif event_type == "rescheduled":
            root = current_to_root[fixture_id]
            states[root]["current_fixture"] = fixture
            states[root]["fixture_event_hash"] = event["event_hash"]
            states[root]["fixture_event_at"] = event["occurred_at"]
        elif event_type == "replaced":
            root = current_to_root.pop(fixture_id)
            replacement = deepcopy(dict(event["replacement_fixture"]))
            current_to_root[str(replacement["fixture_id"])] = root
            states[root]["current_fixture"] = replacement
            states[root]["fixture_event_hash"] = event["event_hash"]
            states[root]["fixture_event_at"] = event["occurred_at"]
        else:
            root = current_to_root[fixture_id]
            states[root]["terminal_event"] = event
    return states


def _validate_independent_model_unavailable(
    *,
    active_policy: Mapping[str, Any],
    competition_key: str,
) -> None:
    lineage = active_policy.get("artifact_lineage")
    registries = (
        lineage.get("model_registries") if isinstance(lineage, Mapping) else None
    )
    football = (
        registries.get("football_htft") if isinstance(registries, Mapping) else None
    )
    registered = (
        football.get("registered_models") if isinstance(football, Mapping) else None
    )
    if not isinstance(registered, Mapping):
        raise CohortScopeError("active policy football model registry is unavailable")
    if competition_key in registered:
        raise CohortScopeError(
            "independent_model_unavailable contradicts the frozen football registry"
        )


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
    replacement_fixture: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = denominator_event_path(base_dir, cohort_id)
    with _event_log_lock(path):
        active_binding = _load_active_event_binding(
            base_dir, cohort_id=cohort_id, scope=scope
        )
        events = load_events(
            base_dir,
            cohort_id,
            scope=scope,
            cohort_binding=active_binding,
            required_schema_version=EVENT_SCHEMA_VERSION,
        )
        if reason == "independent_model_unavailable":
            _validate_independent_model_unavailable(
                active_policy=active_binding["policy_snapshot"],
                competition_key=competition_key,
            )
        value = _event_without_hash(
            schema_version=EVENT_SCHEMA_VERSION,
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
            cohort_binding=active_binding,
            replacement_fixture=replacement_fixture,
        )
        value["event_hash"] = _hash_json(value)
        validate_events(
            [*events, value],
            scope=scope,
            cohort_id=cohort_id,
            cohort_binding=active_binding,
            required_schema_version=EVENT_SCHEMA_VERSION,
        )
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            handle.flush()
    return value


def request_binding(
    *,
    base_dir: str | Path,
    cohort_id: str,
    scope: Mapping[str, Any],
    fixture_id: str,
    expected_fixture: Mapping[str, Any] | None = None,
    cohort_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if cohort_binding is None:
        cohort_binding = _load_active_event_binding(
            base_dir, cohort_id=cohort_id, scope=scope
        )
    events = load_events(
        base_dir,
        cohort_id,
        scope=scope,
        cohort_binding=cohort_binding,
        required_schema_version=EVENT_SCHEMA_VERSION,
    )
    states = _request_states(events)
    matches = [
        state
        for state in states.values()
        if state["current_fixture"]["fixture_id"] == str(fixture_id)
    ]
    if len(matches) != 1:
        raise CohortScopeError(
            "fixture must have exactly one pre-analysis request event"
        )
    state = matches[0]
    if state["terminal_event"] is not None:
        raise CohortScopeError("terminally unavailable fixture cannot be archived")
    fixture = deepcopy(state["current_fixture"])
    if expected_fixture is not None and dict(expected_fixture) != fixture:
        raise CohortScopeError(
            "archived record fixture does not match its frozen request snapshot"
        )
    request = state["request_event"]
    return {
        "schema_version": REQUEST_BINDING_SCHEMA_VERSION,
        "scope_id": request["scope_id"],
        "scope_hash": request["scope_hash"],
        "request_fixture_id": str(request["fixture"]["fixture_id"]),
        "fixture": fixture,
        "request_event_hash": request["event_hash"],
        "fixture_event_hash": state["fixture_event_hash"],
        "fixture_event_at": state["fixture_event_at"],
        "requested_at": request["occurred_at"],
    }


def build_denominator(
    *,
    scope: Mapping[str, Any],
    cohort_id: str,
    events: Sequence[Any],
    record_manifest: Mapping[str, Any],
    cohort_binding: Mapping[str, Any] | None = None,
    schema_version: str = DENOMINATOR_SCHEMA_VERSION,
    required_event_schema_version: str | None = None,
) -> dict[str, Any]:
    frozen = validate_scope(scope)
    if schema_version not in {
        PREVIOUS_DENOMINATOR_SCHEMA_VERSION,
        DENOMINATOR_SCHEMA_VERSION,
    }:
        raise CohortScopeError("cohort denominator schema_version is unsupported")
    normalized = validate_events(
        events,
        scope=frozen,
        cohort_id=cohort_id,
        cohort_binding=cohort_binding,
        required_schema_version=required_event_schema_version,
    )
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
    states = _request_states(normalized)
    by_current_fixture = {
        str(state["current_fixture"]["fixture_id"]): state for state in states.values()
    }
    if set(record_ids) - set(by_current_fixture):
        raise CohortScopeError("archived records exist outside the request denominator")
    for item in records:
        if not isinstance(item, Mapping):
            raise CohortScopeError("record manifest fixture identities are invalid")
        fixture_id = str(item.get("fixture_id") or "")
        state = by_current_fixture[fixture_id]
        if item.get("request_event_hash") != state["request_event"]["event_hash"]:
            raise CohortScopeError(
                "archived record request binding does not match the event log"
            )
        if (
            item.get("fixture") is not None
            and item.get("fixture") != state["current_fixture"]
        ):
            raise CohortScopeError(
                "archived record fixture does not match the request event log"
            )
        if (
            item.get("fixture_event_hash") is not None
            and item.get("fixture_event_hash") != state["fixture_event_hash"]
        ):
            raise CohortScopeError(
                "archived record fixture transition binding does not match the event log"
            )
        if state["terminal_event"] is not None:
            raise CohortScopeError("fixture cannot be both archived and unavailable")
    unresolved = sorted(
        str(state["current_fixture"]["fixture_id"])
        for state in states.values()
        if state["terminal_event"] is None
        and str(state["current_fixture"]["fixture_id"]) not in record_ids
    )
    if unresolved:
        raise CohortScopeError(
            f"cohort denominator has unresolved fixtures: {unresolved}"
        )
    entries = []
    for request_fixture_id in sorted(states):
        state = states[request_fixture_id]
        request = state["request_event"]
        fixture = state["current_fixture"]
        fixture_id = str(fixture["fixture_id"])
        terminal = state["terminal_event"]
        disposition = "recorded" if fixture_id in record_ids else "unavailable"
        entries.append(
            {
                "request_fixture_id": request_fixture_id,
                "fixture_id": fixture_id,
                "fixture": deepcopy(fixture),
                "request_event_hash": request["event_hash"],
                "fixture_event_hash": state["fixture_event_hash"],
                **(
                    {"fixture_event_at": state["fixture_event_at"]}
                    if schema_version == DENOMINATOR_SCHEMA_VERSION
                    else {}
                ),
                "requested_at": request["occurred_at"],
                "disposition": disposition,
                "unavailable_event_hash": terminal["event_hash"]
                if disposition == "unavailable"
                else None,
                "unavailable_reason": terminal["reason"]
                if disposition == "unavailable"
                else None,
            }
        )
    value: dict[str, Any] = {
        "schema_version": schema_version,
        "artifact_type": "soccer_live_forward_cohort_denominator",
        "cohort_id": _require_token(cohort_id, "cohort_id"),
        "scope_id": frozen["scope_id"],
        "scope_hash": frozen["scope_hash"],
        "event_count": len(normalized),
        "last_event_hash": normalized[-1]["event_hash"] if normalized else None,
        "requested_fixture_count": len(entries),
        "recorded_fixture_count": len(record_ids),
        "unavailable_fixture_count": sum(
            state["terminal_event"] is not None for state in states.values()
        ),
        "entries": entries,
        "complete": True,
    }
    value["denominator_hash"] = _hash_json(value)
    return value


def validate_denominator(
    raw: Any,
    *,
    scope: Mapping[str, Any],
    cohort_id: str,
    allowed_schema_versions: Sequence[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CohortScopeError("cohort denominator must be an object")
    value = deepcopy(dict(raw))
    supplied = value.pop("denominator_hash", None)
    if supplied != _hash_json(value):
        raise CohortScopeError("cohort denominator hash is invalid")
    allowed = set(allowed_schema_versions or (DENOMINATOR_SCHEMA_VERSION,))
    if value.get("schema_version") not in allowed or value.get("complete") is not True:
        raise CohortScopeError("cohort denominator is unsupported or incomplete")
    frozen = validate_scope(scope)
    if (
        value.get("cohort_id") != _require_token(cohort_id, "cohort_id")
        or value.get("scope_id") != frozen["scope_id"]
        or value.get("scope_hash") != frozen["scope_hash"]
    ):
        raise CohortScopeError("cohort denominator does not bind its scope/cohort")
    entries = value.get("entries")
    entry_fields = {
        "request_fixture_id",
        "fixture_id",
        "fixture",
        "request_event_hash",
        "fixture_event_hash",
        "requested_at",
        "disposition",
        "unavailable_event_hash",
        "unavailable_reason",
    }
    if value.get("schema_version") == DENOMINATOR_SCHEMA_VERSION:
        entry_fields.add("fixture_event_at")
    if (
        not isinstance(entries, list)
        or any(
            not isinstance(item, Mapping) or set(item) != entry_fields
            for item in entries
        )
        or [str(item.get("request_fixture_id") or "") for item in entries]
        != sorted(str(item.get("request_fixture_id") or "") for item in entries)
    ):
        raise CohortScopeError("cohort denominator entries are invalid")
    for item in entries:
        _require_token(item.get("request_fixture_id"), "request_fixture_id")
        _require_token(item.get("fixture_id"), "fixture_id")
        _require_hash(item.get("request_event_hash"), "request_event_hash")
        _require_hash(item.get("fixture_event_hash"), "fixture_event_hash")
        requested_at = _aware(item.get("requested_at"), "requested_at")
        if value.get("schema_version") == DENOMINATOR_SCHEMA_VERSION:
            fixture_event_at = _aware(item.get("fixture_event_at"), "fixture_event_at")
            if fixture_event_at < requested_at:
                raise CohortScopeError("fixture_event_at predates its request")
        fixture = item.get("fixture")
        if not isinstance(fixture, Mapping) or str(
            fixture.get("fixture_id") or ""
        ) != item.get("fixture_id"):
            raise CohortScopeError("cohort denominator fixture binding is invalid")
        disposition = item.get("disposition")
        if disposition not in {"recorded", "unavailable"}:
            raise CohortScopeError("cohort denominator disposition is invalid")
        if disposition == "recorded":
            if (
                item.get("unavailable_event_hash") is not None
                or item.get("unavailable_reason") is not None
            ):
                raise CohortScopeError("recorded denominator entry is contradictory")
        else:
            _require_hash(item.get("unavailable_event_hash"), "unavailable_event_hash")
            if item.get("unavailable_reason") not in TERMINAL_UNAVAILABLE_REASONS:
                raise CohortScopeError("unavailable denominator reason is invalid")
    recorded_count = sum(item.get("disposition") == "recorded" for item in entries)
    unavailable_count = len(entries) - recorded_count
    if (
        value.get("requested_fixture_count") != len(entries)
        or value.get("recorded_fixture_count") != recorded_count
        or value.get("unavailable_fixture_count") != unavailable_count
    ):
        raise CohortScopeError("cohort denominator counts do not reproduce")
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
    for name in ("request", "unavailable", "rescheduled", "replaced"):
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
        if name == "replaced":
            command.add_argument("--replacement-fixture-id", required=True)
            command.add_argument("--replacement-competition-key", required=True)
            command.add_argument("--replacement-home-team", required=True)
            command.add_argument("--replacement-away-team", required=True)
            command.add_argument("--replacement-kickoff", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--scope-file", required=True)
    return parser


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
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
            event_type = {
                "request": "requested",
                "unavailable": "unavailable",
                "rescheduled": "rescheduled",
                "replaced": "replaced",
            }[args.command]
            replacement_fixture = None
            if args.command == "replaced":
                replacement_fixture = {
                    "fixture_id": args.replacement_fixture_id,
                    "competition_key": args.replacement_competition_key,
                    "home_team": args.replacement_home_team,
                    "away_team": args.replacement_away_team,
                    "kickoff": args.replacement_kickoff,
                }
            artifact = append_event(
                base_dir=args.base_dir,
                cohort_id=args.cohort_id,
                scope=load_scope(args.scope_file),
                event_type=event_type,
                fixture_id=args.fixture_id,
                competition_key=args.competition_key,
                home_team=args.home_team,
                away_team=args.away_team,
                kickoff=args.kickoff,
                occurred_at=args.occurred_at,
                reason=getattr(args, "reason", None),
                replacement_fixture=replacement_fixture,
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

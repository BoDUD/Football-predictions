#!/usr/bin/env python3
"""Persistent, retry-safe scheduler state for soccer-predict lineup checks."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_USER_TIMEZONE = "Asia/Tokyo"
DEFAULT_SOURCE_TIMEZONE = "Asia/Shanghai"
RETRY_MINUTES = (30, 25, 20, 15, 10, 5, 2)
TERMINAL_STATUSES = {
    "completed",
    "expired",
    "started",
    "finished",
    "cancelled",
    "postponed",
}
RESULT_DELIVERY_STATUSES = {"not_ready", "pending", "delivered"}
RESULT_METADATA_GRACE = timedelta(minutes=10)
EXACT_SCHEDULE_SPEC_VERSION = "soccer-exact-utc-once/1.0.0"


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def state_path(base_dir: str | None) -> Path:
    base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
    return base / ".codex" / "soccer-predict" / "lineup_tasks.json"


def history_path(base_dir: str | None) -> Path:
    return state_path(base_dir).with_name("history.json")


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Datetime must include timezone: {value}")
    return parsed.astimezone(timezone.utc)


def named_timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        fixed = {
            "Asia/Tokyo": timezone(timedelta(hours=9), "Asia/Tokyo"),
            "Asia/Shanghai": timezone(timedelta(hours=8), "Asia/Shanghai"),
        }
        if name in fixed:
            return fixed[name]
        raise ValueError(f"Timezone data unavailable for {name}") from None


def iso_seconds(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def empty_state() -> dict[str, Any]:
    return {"version": 1, "tasks": {}}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(path)


@contextmanager
def locked_state(path: Path) -> Iterator[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            state = load_json(path, empty_state())
            if not isinstance(state, dict) or not isinstance(state.get("tasks"), dict):
                raise ValueError(f"Invalid lineup scheduler state: {path}")
            yield state
            save_state(path, state)
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def history_record(base_dir: str | None, match_id: str) -> dict[str, Any] | None:
    history = load_json(history_path(base_dir), [])
    if not isinstance(history, list):
        raise ValueError("history.json must contain an array")
    return next(
        (item for item in history if str(item.get("match_id")) == str(match_id)), None
    )


def retry_plan(kickoff: datetime, local_zone) -> list[dict[str, Any]]:
    plan = []
    for minutes in RETRY_MINUTES:
        run_at = kickoff - timedelta(minutes=minutes)
        run_at_utc = run_at.astimezone(timezone.utc)
        automation_rrule = codex_rrule_utc(run_at_utc)
        plan.append(
            {
                "minutes_before_kickoff": minutes,
                "run_at": iso_seconds(run_at.astimezone(local_zone)),
                "run_at_utc": iso_seconds(run_at_utc),
                "automation_timezone": "UTC",
                "automation_rrule": automation_rrule,
                "automation_schedule_spec": exact_utc_schedule_spec(
                    run_at_utc, automation_rrule=automation_rrule
                ),
                "label": "T-30" if minutes == 30 else f"retry-T-{minutes}",
            }
        )
    return plan


def codex_rrule_utc(run_at: datetime) -> str:
    """Return the executor-compatible RRULE portion of an exact schedule spec.

    Codex immediate automation creation rejects DTSTART. Its local executor treats
    an unanchored BYHOUR/BYMINUTE rule as UTC, so converting the absolute instant
    here prevents callers from accidentally scheduling a Japan wall-clock hour as
    a UTC hour.  The date is bound separately by :func:`exact_utc_schedule_spec`
    and may be confirmed against the platform's returned next-run timestamp.
    """
    utc_run_at = run_at.astimezone(timezone.utc)
    return (
        "RRULE:FREQ=DAILY;"
        f"BYHOUR={utc_run_at.hour};BYMINUTE={utc_run_at.minute};COUNT=1"
    )


def exact_utc_schedule_spec(
    run_at: datetime, *, automation_rrule: str | None = None
) -> dict[str, Any]:
    """Return a date-bound, machine-verifiable one-time UTC schedule contract."""

    run_at_utc = run_at.astimezone(timezone.utc).replace(microsecond=0)
    rrule = automation_rrule or codex_rrule_utc(run_at_utc)
    timestamp = iso_seconds(run_at_utc)
    return {
        "schema_version": EXACT_SCHEDULE_SPEC_VERSION,
        "kind": "one_time",
        "timezone": "UTC",
        "run_at_utc": timestamp,
        "dtstart_utc": timestamp,
        "until_utc": timestamp,
        "count": 1,
        "automation_rrule": rrule,
        "platform_next_run_must_equal_run_at_utc": True,
    }


def ensure_retry_plan(task: dict[str, Any]) -> None:
    """Backfill machine-readable automation fields on compatible old tasks."""
    kickoff = parse_datetime(str(task["kickoff"]))
    local_zone = named_timezone(str(task.get("user_timezone") or DEFAULT_USER_TIMEZONE))
    expected = retry_plan(kickoff, local_zone)
    current = task.get("retry_plan")
    if not isinstance(current, list) or any(
        not isinstance(item, dict)
        or not item.get("run_at_utc")
        or not item.get("automation_rrule")
        for item in current
    ):
        task["retry_plan"] = expected
        return
    for item in current:
        run_at_utc = parse_datetime(str(item["run_at_utc"]))
        item["automation_schedule_spec"] = exact_utc_schedule_spec(
            run_at_utc, automation_rrule=str(item["automation_rrule"])
        )
    attempts_by_label = {
        str(item.get("label")): item for item in current if isinstance(item, dict)
    }
    for ref in task.get("automation_refs", []):
        if not isinstance(ref, dict):
            continue
        attempt = attempts_by_label.get(str(ref.get("attempt_label")))
        if not attempt:
            continue
        ref["automation_schedule_spec"] = dict(attempt["automation_schedule_spec"])
        ref.setdefault("platform_next_run_utc", None)
        ref.setdefault("platform_next_run_verified", False)


def ensure_result_delivery(task: dict[str, Any]) -> dict[str, Any]:
    """Backfill result-delivery state without invalidating legacy task files."""
    existing = task.get("result_delivery")
    cleanup_completed_at = task.get("cleanup_completed_at")
    task_status = str(task.get("status") or "")
    legacy_inferred = not isinstance(existing, dict)

    if isinstance(existing, dict):
        delivery = existing
    else:
        legacy_status = (
            existing if isinstance(existing, str) else task.get("delivery_status")
        )
        if cleanup_completed_at:
            status = "delivered"
        elif task_status in TERMINAL_STATUSES:
            status = "delivered" if legacy_status == "delivered" else "pending"
        elif legacy_status in RESULT_DELIVERY_STATUSES:
            status = legacy_status
        else:
            status = "not_ready"
        delivery = {
            "delivery_status": status,
            "thread_id": task.get("thread_id"),
            "result_artifact": task.get("result_artifact"),
            "delivered_at": cleanup_completed_at if status == "delivered" else None,
        }
        task["result_delivery"] = delivery

    status = str(
        delivery.get("delivery_status")
        or delivery.pop("status", None)
        or task.get("delivery_status")
        or ""
    )
    if status not in RESULT_DELIVERY_STATUSES:
        status = (
            "delivered"
            if cleanup_completed_at
            else ("pending" if task_status in TERMINAL_STATUSES else "not_ready")
        )
    if cleanup_completed_at and status != "delivered":
        status = "delivered"
        legacy_inferred = True
    delivery["delivery_status"] = status
    task["delivery_status"] = status

    thread_id = delivery.get("thread_id") or task.get("thread_id")
    delivery["thread_id"] = thread_id
    if thread_id:
        task["thread_id"] = thread_id
    result_artifact = delivery.get("result_artifact") or task.get("result_artifact")
    delivery["result_artifact"] = result_artifact
    if result_artifact:
        task["result_artifact"] = result_artifact
    if (
        status == "delivered"
        and cleanup_completed_at
        and not delivery.get("delivered_at")
    ):
        delivery["delivered_at"] = cleanup_completed_at
    delivery.setdefault("delivered_at", None)
    if legacy_inferred:
        delivery["legacy_inferred"] = True
    return delivery


def result_delivery_status(delivery: dict[str, Any]) -> str:
    return str(delivery.get("delivery_status") or "")


def result_metadata_grace_active(task: dict[str, Any], current: datetime) -> bool:
    """Allow the finishing worker time to persist its thread and artifact tuple."""
    status = str(task.get("status") or "")
    fields = (
        ("completed_at", "updated_at")
        if status == "completed"
        else ("terminal_at", "updated_at")
    )
    for field in fields:
        value = task.get(field)
        if not value:
            continue
        try:
            terminal_baseline = parse_datetime(str(value))
        except (TypeError, ValueError):
            continue
        return current < terminal_baseline + RESULT_METADATA_GRACE
    return False


def result_tuple_is_duplicate(
    task: dict[str, Any],
    *,
    status: str,
    reason: str,
    thread_id: str,
    result_artifact: str,
) -> bool:
    """Reject attempts to replace a result tuple once delivery is pending."""
    delivery = ensure_result_delivery(task)
    if result_delivery_status(delivery) not in {"pending", "delivered"}:
        return False

    known_thread = str(delivery.get("thread_id") or task.get("thread_id") or "").strip()
    known_artifact = str(
        delivery.get("result_artifact") or task.get("result_artifact") or ""
    ).strip()
    known_status = str(task.get("status") or "").strip()
    known_reason = str(task.get("terminal_reason") or "").strip()
    tuple_complete = bool(known_thread and known_artifact)

    conflicts = (
        (known_thread and known_thread != thread_id)
        or (known_artifact and known_artifact != result_artifact)
        or (known_status in TERMINAL_STATUSES and known_status != status)
        or (tuple_complete and known_reason and known_reason != reason)
    )
    if conflicts:
        raise ValueError("Lineup result tuple is already recorded for another result")

    # Auto-sync may create a pending delivery before the child task has produced
    # its thread and artifact. The first complete tuple is allowed to fill it.
    return tuple_complete


def set_result_delivery_pending(
    task: dict[str, Any],
    thread_id: str | None = None,
    result_artifact: str | None = None,
) -> dict[str, Any]:
    delivery = ensure_result_delivery(task)
    if result_delivery_status(delivery) != "delivered":
        delivery["delivery_status"] = "pending"
        task["delivery_status"] = "pending"
        delivery["thread_id"] = (
            thread_id or delivery.get("thread_id") or task.get("thread_id")
        )
        delivery["result_artifact"] = (
            result_artifact
            or delivery.get("result_artifact")
            or task.get("result_artifact")
        )
        delivery["delivered_at"] = None
        delivery.pop("legacy_inferred", None)
        if delivery.get("thread_id"):
            task["thread_id"] = delivery["thread_id"]
        if delivery.get("result_artifact"):
            task["result_artifact"] = delivery["result_artifact"]
    return delivery


def resolve_result_artifact(base_dir: str | None, value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Cannot complete lineup task without --result-artifact")
    artifact = Path(raw).expanduser()
    if not artifact.is_absolute():
        base = (
            Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
        )
        artifact = base / artifact
    artifact = artifact.resolve()
    if not artifact.exists():
        raise ValueError(f"Result artifact does not exist: {artifact}")
    if not artifact.is_file():
        raise ValueError(f"Result artifact is not a file: {artifact}")
    if artifact.stat().st_size <= 0:
        raise ValueError(f"Result artifact is empty: {artifact}")
    return str(artifact)


def sync_terminal(
    task: dict[str, Any], record: dict[str, Any] | None, current: datetime
) -> None:
    ensure_result_delivery(task)
    if task.get("status") in TERMINAL_STATUSES:
        return
    if record and record.get("lineup_rechecked_at"):
        task["status"] = "completed"
        task["completed_at"] = record["lineup_rechecked_at"]
        task["lease_until"] = None
        task["terminal_reason"] = "lineup_revision_archived"
        set_result_delivery_pending(task)
        return
    kickoff = parse_datetime(str(task["kickoff"]))
    if current >= kickoff:
        task["status"] = "expired"
        task["terminal_reason"] = "kickoff_reached_without_lineup_revision"
        task["terminal_at"] = iso_seconds(current)
        task["lease_until"] = None
        set_result_delivery_pending(task)


def task_result(path: Path, task: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = {"ok": True, "path": str(path), "task": task}
    result.update(extra)
    return result


def cmd_register(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    record = history_record(args.base_dir, args.match_id)
    if not record and not args.kickoff:
        raise ValueError(
            f"No archived prediction for match {args.match_id}; pass --kickoff explicitly"
        )
    kickoff_text = args.kickoff or str(record.get("kickoff", ""))
    kickoff = parse_datetime(kickoff_text)
    local_zone = named_timezone(args.user_timezone)
    source_zone = named_timezone(args.source_timezone)
    local_kickoff = kickoff.astimezone(local_zone)
    source_kickoff = kickoff.astimezone(source_zone)
    scheduled = kickoff - timedelta(minutes=30)
    created_at = iso_seconds(now_utc())
    with locked_state(path) as state:
        tasks = state["tasks"]
        existing = tasks.get(str(args.match_id))
        if existing:
            same = (
                parse_datetime(str(existing.get("kickoff"))) == kickoff
                and existing.get("user_timezone") == args.user_timezone
                and existing.get("source_timezone") == args.source_timezone
                and existing.get("source_kickoff") == iso_seconds(source_kickoff)
            )
            if same:
                ensure_retry_plan(existing)
                ensure_result_delivery(existing)
                return task_result(path, existing, duplicate_ignored=True)
            if existing.get("status") in TERMINAL_STATUSES:
                raise ValueError(
                    f"Refusing to replace terminal lineup task for match {args.match_id}"
                )
        task = {
            "match_id": str(args.match_id),
            "home_team": args.home_team or (record or {}).get("home_team"),
            "away_team": args.away_team or (record or {}).get("away_team"),
            "source_timezone": args.source_timezone,
            "source_kickoff": iso_seconds(source_kickoff),
            "user_timezone": args.user_timezone,
            "kickoff": iso_seconds(local_kickoff),
            "scheduled_for": iso_seconds(scheduled.astimezone(local_zone)),
            "retry_plan": retry_plan(kickoff, local_zone),
            "status": "scheduled",
            "attempts": [],
            "lease_until": None,
            "automation_refs": [],
            "created_at": created_at,
            "updated_at": created_at,
            "cleanup_completed_at": None,
            "delivery_status": "not_ready",
            "thread_id": None,
            "result_artifact": None,
            "result_delivery": {
                "delivery_status": "not_ready",
                "thread_id": None,
                "result_artifact": None,
                "delivered_at": None,
            },
        }
        tasks[str(args.match_id)] = task
        return task_result(path, task, duplicate_ignored=False)


def cmd_sync_pending(args: argparse.Namespace) -> dict[str, Any]:
    """Idempotently register future pending records missed by the calling agent."""
    history = load_json(history_path(args.base_dir), [])
    if not isinstance(history, list):
        raise ValueError("history.json must contain an array")
    current = parse_datetime(args.now) if args.now else now_utc()
    registered: list[str] = []
    duplicate_ignored: list[str] = []
    skipped_rechecked: list[str] = []
    skipped_invalid: list[dict[str, str]] = []
    for record in history:
        if not isinstance(record, dict) or record.get("mode") != "prematch":
            continue
        match_id = str(record.get("match_id") or "").strip()
        if str(record.get("status") or "").strip().lower() != "pending":
            continue
        if record.get("lineup_rechecked_at"):
            if match_id:
                skipped_rechecked.append(match_id)
            continue
        kickoff_text = str(record.get("kickoff") or "").strip()
        if not match_id:
            skipped_invalid.append({"match_id": "", "reason": "missing_match_id"})
            continue
        try:
            kickoff = parse_datetime(kickoff_text)
        except (TypeError, ValueError):
            skipped_invalid.append(
                {"match_id": match_id, "reason": "kickoff_requires_explicit_offset"}
            )
            continue
        if kickoff <= current:
            skipped_invalid.append({"match_id": match_id, "reason": "kickoff_reached"})
            continue
        try:
            result = cmd_register(
                argparse.Namespace(
                    base_dir=args.base_dir,
                    match_id=match_id,
                    kickoff=kickoff_text,
                    source_timezone=args.source_timezone,
                    user_timezone=args.user_timezone,
                    home_team=record.get("home_team"),
                    away_team=record.get("away_team"),
                )
            )
        except ValueError as exc:
            skipped_invalid.append({"match_id": match_id, "reason": str(exc)})
            continue
        if result.get("duplicate_ignored"):
            duplicate_ignored.append(match_id)
        else:
            registered.append(match_id)
    return {
        "ok": True,
        "path": str(state_path(args.base_dir)),
        "checked_at": iso_seconds(current),
        "registered": registered,
        "duplicate_ignored": duplicate_ignored,
        "skipped_rechecked": skipped_rechecked,
        "skipped_invalid": skipped_invalid,
    }


def get_task(state: dict[str, Any], match_id: str) -> dict[str, Any]:
    task = state["tasks"].get(str(match_id))
    if not task:
        raise ValueError(f"No lineup task registered for match {match_id}")
    ensure_result_delivery(task)
    return task


def cmd_attach_automation(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        if task.get("status") in TERMINAL_STATUSES or task.get("cleanup_completed_at"):
            raise ValueError("Cannot attach an automation to a terminal lineup task")
        ensure_retry_plan(task)
        attempt = next(
            (
                item
                for item in task["retry_plan"]
                if item["label"] == args.attempt_label
            ),
            None,
        )
        if not attempt:
            raise ValueError(f"Unknown attempt label: {args.attempt_label}")
        if args.automation_rrule != attempt["automation_rrule"]:
            raise ValueError(
                "Automation RRULE does not match the expected UTC rule for "
                f"{args.attempt_label}: {attempt['automation_rrule']}"
            )
        expected_run_at = parse_datetime(str(attempt["run_at_utc"]))
        platform_next_run = str(getattr(args, "platform_next_run", "") or "").strip()
        if platform_next_run:
            parsed_platform_next_run = parse_datetime(platform_next_run)
            if parsed_platform_next_run != expected_run_at:
                raise ValueError(
                    "Platform next-run does not exactly match run_at_utc: "
                    f"expected {iso_seconds(expected_run_at)}"
                )
            platform_next_run_utc = iso_seconds(parsed_platform_next_run)
        else:
            platform_next_run_utc = None
        existing = next(
            (
                ref
                for ref in task.setdefault("automation_refs", [])
                if ref.get("attempt_label") == args.attempt_label
            ),
            None,
        )
        if existing:
            same = (
                existing.get("id") == args.automation_id
                and existing.get("automation_rrule") == args.automation_rrule
            )
            if same:
                if platform_next_run_utc is not None:
                    existing["platform_next_run_utc"] = platform_next_run_utc
                    existing["platform_next_run_verified"] = True
                    existing["automation_schedule_spec"] = exact_utc_schedule_spec(
                        expected_run_at, automation_rrule=args.automation_rrule
                    )
                return task_result(path, task, duplicate_ignored=True)
            raise ValueError(f"Attempt {args.attempt_label} already has an automation")
        ref = {
            "id": args.automation_id,
            "name": args.automation_name,
            "attempt_label": args.attempt_label,
            "run_at": attempt["run_at"],
            "run_at_utc": attempt["run_at_utc"],
            "automation_rrule": args.automation_rrule,
            "schedule_verified": True,
            "automation_schedule_spec": exact_utc_schedule_spec(
                expected_run_at, automation_rrule=args.automation_rrule
            ),
            "platform_next_run_utc": platform_next_run_utc,
            "platform_next_run_verified": platform_next_run_utc is not None,
        }
        task["automation_refs"].append(ref)
        task["updated_at"] = iso_seconds(now_utc())
        return task_result(path, task, duplicate_ignored=False)


def cmd_claim(args: argparse.Namespace) -> dict[str, Any]:
    if args.lease_minutes <= 0:
        raise ValueError("--lease-minutes must be positive")
    thread_id = str(getattr(args, "thread_id", "") or "").strip()
    if not thread_id:
        raise ValueError("Cannot claim lineup task without a non-empty --thread-id")
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    record = history_record(args.base_dir, args.match_id)
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        sync_terminal(task, record, current)
        scheduled = parse_datetime(str(task["scheduled_for"]))
        kickoff = parse_datetime(str(task["kickoff"]))
        if task.get("status") in TERMINAL_STATUSES:
            return task_result(path, task, claimed=False, reason=task["status"])
        if current < scheduled:
            return task_result(path, task, claimed=False, reason="too_early")
        if current >= kickoff:
            sync_terminal(task, record, current)
            return task_result(path, task, claimed=False, reason=task["status"])
        lease_until_text = task.get("lease_until")
        if lease_until_text and parse_datetime(str(lease_until_text)) > current:
            return task_result(path, task, claimed=False, reason="active_lease")
        lease_until = min(kickoff, current + timedelta(minutes=args.lease_minutes))
        attempt = {
            "number": len(task.setdefault("attempts", [])) + 1,
            "claimed_at": iso_seconds(current),
            "lease_until": iso_seconds(lease_until),
            "catch_up": current > scheduled + timedelta(seconds=60),
            "thread_id": thread_id,
        }
        task["attempts"].append(attempt)
        task["status"] = "claimed"
        task["lease_until"] = attempt["lease_until"]
        task["claimed_thread_id"] = thread_id
        task["updated_at"] = iso_seconds(current)
        return task_result(
            path,
            task,
            claimed=True,
            catch_up=attempt["catch_up"],
            minutes_to_kickoff=round((kickoff - current).total_seconds() / 60, 1),
            cleanup_automation_refs=task.get("automation_refs", []),
        )


def cmd_release(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    record = history_record(args.base_dir, args.match_id)
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        sync_terminal(task, record, current)
        if task.get("status") not in TERMINAL_STATUSES:
            task["status"] = "scheduled"
            task["lease_until"] = None
            task["last_error"] = args.reason
            task["last_failed_at"] = iso_seconds(current)
            if task.get("attempts"):
                task["attempts"][-1]["failed_at"] = iso_seconds(current)
                task["attempts"][-1]["error"] = args.reason
        task["updated_at"] = iso_seconds(current)
        return task_result(path, task, released=task.get("status") == "scheduled")


def cmd_complete(args: argparse.Namespace) -> dict[str, Any]:
    thread_id = str(getattr(args, "thread_id", "") or "").strip()
    if not thread_id:
        raise ValueError("Cannot complete lineup task without a non-empty --thread-id")
    result_artifact = resolve_result_artifact(
        args.base_dir, getattr(args, "result_artifact", None)
    )
    path = state_path(args.base_dir)
    record = history_record(args.base_dir, args.match_id)
    if not record or not record.get("lineup_rechecked_at"):
        raise ValueError(
            "Cannot complete lineup task before a lineup-check revision is archived"
        )
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        claimed_thread_id = str(task.get("claimed_thread_id") or "").strip()
        if claimed_thread_id and claimed_thread_id != thread_id:
            raise ValueError("Completing thread id does not match the active claim")
        duplicate = result_tuple_is_duplicate(
            task,
            status="completed",
            reason="lineup_revision_archived",
            thread_id=thread_id,
            result_artifact=result_artifact,
        )
        if duplicate:
            return task_result(
                path,
                task,
                cleanup_automation_refs=task.get("automation_refs", []),
                duplicate_ignored=True,
            )
        task["status"] = "completed"
        task["completed_at"] = record["lineup_rechecked_at"]
        task["terminal_reason"] = "lineup_revision_archived"
        task["lease_until"] = None
        task["thread_id"] = thread_id
        task["result_artifact"] = result_artifact
        set_result_delivery_pending(task, thread_id, result_artifact)
        task["updated_at"] = iso_seconds(current)
        return task_result(
            path, task, cleanup_automation_refs=task.get("automation_refs", [])
        )


def cmd_terminal(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        thread_id = str(getattr(args, "thread_id", "") or "").strip()
        if not thread_id:
            raise ValueError(
                "Cannot finish lineup terminal state without a non-empty --thread-id"
            )
        result_artifact = resolve_result_artifact(
            args.base_dir, getattr(args, "result_artifact", None)
        )
        duplicate = result_tuple_is_duplicate(
            task,
            status=args.reason,
            reason=args.reason,
            thread_id=thread_id,
            result_artifact=result_artifact,
        )
        if duplicate:
            return task_result(
                path,
                task,
                cleanup_automation_refs=task.get("automation_refs", []),
                duplicate_ignored=True,
            )
        task["status"] = args.reason
        task["terminal_reason"] = args.reason
        task["terminal_at"] = iso_seconds(current)
        task["lease_until"] = None
        task["thread_id"] = thread_id
        task["result_artifact"] = result_artifact
        set_result_delivery_pending(task, thread_id, result_artifact)
        task["updated_at"] = iso_seconds(current)
        return task_result(
            path, task, cleanup_automation_refs=task.get("automation_refs", [])
        )


def cmd_mark_delivered(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    supplied_thread_id = str(getattr(args, "thread_id", "") or "").strip()
    if not supplied_thread_id:
        raise ValueError("--thread-id is required to mark a lineup result delivered")
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        if task.get("status") not in TERMINAL_STATUSES:
            raise ValueError(
                "Cannot mark result delivered before the lineup task is terminal"
            )
        delivery = ensure_result_delivery(task)
        expected_thread_id = str(delivery.get("thread_id") or "").strip()
        if not expected_thread_id or supplied_thread_id != expected_thread_id:
            raise ValueError(
                "Delivered thread id does not match the archived lineup task"
            )
        if delivery.get("result_artifact"):
            resolved_artifact = resolve_result_artifact(
                args.base_dir, delivery.get("result_artifact")
            )
        elif delivery.get("legacy_inferred"):
            resolved_artifact = None
        else:
            raise ValueError("The lineup result artifact is missing")
        if result_delivery_status(delivery) == "delivered":
            return task_result(path, task, duplicate_ignored=True)
        delivery["delivery_status"] = "delivered"
        task["delivery_status"] = "delivered"
        if resolved_artifact:
            delivery["result_artifact"] = resolved_artifact
            task["result_artifact"] = resolved_artifact
        delivery["delivered_at"] = iso_seconds(current)
        delivery.pop("legacy_inferred", None)
        task["updated_at"] = iso_seconds(current)
        return task_result(path, task, duplicate_ignored=False)


def cmd_mark_cleaned(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        delivery = ensure_result_delivery(task)
        if result_delivery_status(delivery) != "delivered":
            raise ValueError(
                "Cannot mark automations cleaned before the lineup result is marked delivered"
            )
        known = {str(ref.get("id")) for ref in task.get("automation_refs", [])}
        supplied = {str(value) for value in (args.automation_id or [])}
        unknown = sorted(supplied - known)
        missing = sorted(known - supplied)
        if unknown:
            raise ValueError(f"Unknown automation id(s): {', '.join(unknown)}")
        if missing:
            raise ValueError(
                f"Automation id(s) still require cleanup: {', '.join(missing)}"
            )
        task["cleaned_automation_ids"] = sorted(supplied)
        task["cleanup_completed_at"] = iso_seconds(current)
        task["updated_at"] = iso_seconds(current)
        return task_result(path, task)


def cmd_due(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    due: list[dict[str, Any]] = []
    with locked_state(path) as state:
        for task in state["tasks"].values():
            ensure_retry_plan(task)
            record = history_record(args.base_dir, str(task["match_id"]))
            sync_terminal(task, record, current)
            if task.get("status") in TERMINAL_STATUSES:
                continue
            scheduled = parse_datetime(str(task["scheduled_for"]))
            kickoff = parse_datetime(str(task["kickoff"]))
            lease = (
                parse_datetime(str(task["lease_until"]))
                if task.get("lease_until")
                else None
            )
            if scheduled <= current < kickoff and (not lease or lease <= current):
                item = dict(task)
                item["catch_up"] = current > scheduled + timedelta(seconds=60)
                item["minutes_to_kickoff"] = round(
                    (kickoff - current).total_seconds() / 60, 1
                )
                due.append(item)
        due.sort(key=lambda item: item["kickoff"])
        return {
            "ok": True,
            "path": str(path),
            "checked_at": iso_seconds(current),
            "due": due,
        }


def cmd_cleanup_due(args: argparse.Namespace) -> dict[str, Any]:
    """List terminal tasks awaiting delivery verification or automation cleanup."""
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    due: list[dict[str, Any]] = []
    requested_match_id = str(args.match_id) if getattr(args, "match_id", None) else None
    with locked_state(path) as state:
        for task in state["tasks"].values():
            if requested_match_id and str(task.get("match_id")) != requested_match_id:
                continue
            ensure_retry_plan(task)
            record = history_record(args.base_dir, str(task["match_id"]))
            sync_terminal(task, record, current)
            if task.get("status") not in TERMINAL_STATUSES:
                continue
            if task.get("cleanup_completed_at"):
                continue
            delivery = ensure_result_delivery(task)
            delivery_status = result_delivery_status(delivery)
            if delivery_status == "pending":
                has_thread = bool(str(delivery.get("thread_id") or "").strip())
                has_artifact = bool(str(delivery.get("result_artifact") or "").strip())
                metadata_complete = has_thread and (
                    has_artifact or delivery.get("legacy_inferred")
                )
                if not metadata_complete and result_metadata_grace_active(
                    task, current
                ):
                    continue
                next_action = (
                    "verify_delivery"
                    if metadata_complete
                    else "await_complete_metadata"
                )
            elif delivery_status == "delivered":
                next_action = "cleanup_automations"
            else:
                continue
            due.append(
                {
                    "match_id": str(task["match_id"]),
                    "status": task.get("status"),
                    "delivery_status": delivery_status,
                    "thread_id": delivery.get("thread_id"),
                    "result_artifact": delivery.get("result_artifact"),
                    "delivery_pending": delivery_status == "pending",
                    "cleanup_pending": True,
                    "next_action": next_action,
                    "cleanup_automation_refs": task.get("automation_refs", []),
                }
            )
        due.sort(key=lambda item: item["match_id"])
        return {
            "ok": True,
            "path": str(path),
            "checked_at": iso_seconds(current),
            "due": due,
        }


def cmd_automation_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Return only safe future Codex creates plus an explicit catch-up signal."""
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        ensure_retry_plan(task)
        sync_terminal(task, history_record(args.base_dir, args.match_id), current)
        kickoff = parse_datetime(str(task["kickoff"]))
        scheduled = parse_datetime(str(task["scheduled_for"]))
        terminal = task.get("status") in TERMINAL_STATUSES
        lease = (
            parse_datetime(str(task["lease_until"]))
            if task.get("lease_until")
            else None
        )
        future_attempts = []
        if not terminal and current < kickoff:
            for item in task["retry_plan"]:
                run_at = parse_datetime(str(item["run_at_utc"]))
                if current < run_at < kickoff:
                    future_attempts.append(dict(item))
        return {
            "ok": True,
            "path": str(path),
            "checked_at": iso_seconds(current),
            "match_id": str(task["match_id"]),
            "status": task.get("status"),
            "catch_up_required": (
                not terminal
                and scheduled <= current < kickoff
                and (not lease or lease <= current)
            ),
            "create_mode": "create",
            "rrule_timezone": "UTC",
            "future_attempts": future_attempts,
        }


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        if args.match_id:
            task = get_task(state, args.match_id)
            ensure_retry_plan(task)
            sync_terminal(task, history_record(args.base_dir, args.match_id), current)
            return task_result(path, task)
        for task in state["tasks"].values():
            ensure_retry_plan(task)
            sync_terminal(
                task, history_record(args.base_dir, str(task["match_id"])), current
            )
        return {"ok": True, "path": str(path), "tasks": list(state["tasks"].values())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir", help="Workspace root; defaults to current directory"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser(
        "register", help="Register an idempotent T-30 schedule and retry plan"
    )
    register.add_argument("--match-id", required=True)
    register.add_argument(
        "--kickoff",
        help="Kickoff with explicit UTC offset; defaults to archived record",
    )
    register.add_argument("--source-timezone", default=DEFAULT_SOURCE_TIMEZONE)
    register.add_argument("--user-timezone", default=DEFAULT_USER_TIMEZONE)
    register.add_argument("--home-team")
    register.add_argument("--away-team")

    sync_pending = sub.add_parser(
        "sync-pending",
        aliases=["bootstrap"],
        help="Idempotently register future pending pre-match records",
    )
    sync_pending.add_argument("--source-timezone", default=DEFAULT_SOURCE_TIMEZONE)
    sync_pending.add_argument("--user-timezone", default=DEFAULT_USER_TIMEZONE)
    sync_pending.add_argument("--now")

    attach = sub.add_parser(
        "attach-automation", help="Attach a Codex automation id for later cleanup"
    )
    attach.add_argument("--match-id", required=True)
    attach.add_argument("--automation-id", required=True)
    attach.add_argument("--automation-name", required=True)
    attach.add_argument("--attempt-label", required=True)
    attach.add_argument("--automation-rrule", required=True)
    attach.add_argument(
        "--platform-next-run",
        "--platform-next-run-utc",
        dest="platform_next_run",
        help="Platform-reported next run; when supplied it must exactly equal run_at_utc",
    )

    claim = sub.add_parser("claim", help="Atomically claim a due prematch lineup check")
    claim.add_argument("--match-id", required=True)
    claim.add_argument("--thread-id", required=True)
    claim.add_argument(
        "--now", help="ISO datetime with offset, for deterministic checks"
    )
    claim.add_argument("--lease-minutes", type=float, default=4.0)

    release = sub.add_parser(
        "release", help="Release a failed claim so the next retry can run"
    )
    release.add_argument("--match-id", required=True)
    release.add_argument("--reason", required=True)
    release.add_argument("--now")

    complete = sub.add_parser(
        "complete", help="Complete only after the lineup revision is archived"
    )
    complete.add_argument("--match-id", required=True)
    complete.add_argument("--thread-id", required=True)
    complete.add_argument(
        "--result-artifact",
        required=True,
        help="Existing non-empty file containing the complete user-facing lineup result",
    )
    complete.add_argument("--now")

    terminal = sub.add_parser(
        "terminal", help="Stop retries for an explicit terminal match state"
    )
    terminal.add_argument("--match-id", required=True)
    terminal.add_argument(
        "--reason",
        choices=("started", "finished", "cancelled", "postponed", "expired"),
        required=True,
    )
    terminal.add_argument("--thread-id", required=True)
    terminal.add_argument(
        "--result-artifact",
        required=True,
        help="Existing non-empty file containing the complete terminal-state notice",
    )
    terminal.add_argument("--now")

    delivered = sub.add_parser(
        "mark-delivered",
        help="Record that the terminal lineup result was delivered in its Codex task",
    )
    delivered.add_argument("--match-id", required=True)
    delivered.add_argument("--thread-id", required=True)
    delivered.add_argument("--now")

    cleaned = sub.add_parser(
        "mark-cleaned", help="Record deletion/disablement of attached automations"
    )
    cleaned.add_argument("--match-id", required=True)
    cleaned.add_argument("--automation-id", action="append")
    cleaned.add_argument("--now")

    due = sub.add_parser("due", help="List due or missed-but-still-prematch checks")
    due.add_argument("--now")

    cleanup_due = sub.add_parser(
        "cleanup-due",
        help="List terminal tasks awaiting result-delivery verification or automation cleanup",
    )
    cleanup_due.add_argument("--match-id")
    cleanup_due.add_argument("--now")

    automation_plan = sub.add_parser(
        "automation-plan",
        help="Return UTC RRULEs for safe future Codex creates and any catch-up requirement",
    )
    automation_plan.add_argument("--match-id", required=True)
    automation_plan.add_argument("--now")

    status = sub.add_parser("status", help="Show persisted lineup task state")
    status.add_argument("--match-id")
    status.add_argument("--now")
    return parser


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        handlers = {
            "register": cmd_register,
            "sync-pending": cmd_sync_pending,
            "bootstrap": cmd_sync_pending,
            "attach-automation": cmd_attach_automation,
            "claim": cmd_claim,
            "release": cmd_release,
            "complete": cmd_complete,
            "terminal": cmd_terminal,
            "mark-delivered": cmd_mark_delivered,
            "mark-cleaned": cmd_mark_cleaned,
            "due": cmd_due,
            "cleanup-due": cmd_cleanup_due,
            "automation-plan": cmd_automation_plan,
            "status": cmd_status,
        }
        result = handlers[args.command](args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

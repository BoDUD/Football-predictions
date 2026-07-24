#!/usr/bin/env python3
"""Persistent one-match scheduler state for soccer-predict post-match reviews."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_USER_TIMEZONE = "Asia/Tokyo"
INITIAL_DELAY_HOURS = 3
FOLLOW_UP_MINUTES = 30
ACTIVE_STATUSES = {"scheduled", "claimed", "waiting"}
FINAL_STATUSES = {"completed", "terminal"}
RESULT_DELIVERY_STATUSES = {"not_ready", "pending", "delivered"}
RESULT_METADATA_GRACE = timedelta(minutes=10)
ADMIN_TERMINAL_STATUSES = {
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "postponed": "postponed",
    "abandoned": "abandoned",
}


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def state_path(base_dir: str | None) -> Path:
    base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
    return base / ".codex" / "soccer-predict" / "review_tasks.json"


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
        if name == DEFAULT_USER_TIMEZONE:
            return timezone(timedelta(hours=9), DEFAULT_USER_TIMEZONE)
        raise ValueError(f"Timezone data unavailable for {name}") from None


def iso_seconds(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def codex_rrule_utc(run_at: datetime) -> str:
    utc_run_at = run_at.astimezone(timezone.utc)
    return (
        "RRULE:FREQ=DAILY;"
        f"BYHOUR={utc_run_at.hour};BYMINUTE={utc_run_at.minute};COUNT=1"
    )


def empty_state() -> dict[str, Any]:
    return {"version": 1, "tasks": {}}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
                raise ValueError(f"Invalid review scheduler state: {path}")
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
    return next((item for item in history if str(item.get("match_id")) == str(match_id)), None)


def make_attempt(
    number: int,
    run_at: datetime,
    local_zone,
    *,
    kind: str,
    reason: str | None = None,
) -> dict[str, Any]:
    run_at_utc = run_at.astimezone(timezone.utc)
    return {
        "number": number,
        "attempt_id": f"review-{number}",
        "kind": kind,
        "reason": reason,
        "run_at": iso_seconds(run_at.astimezone(local_zone)),
        "run_at_utc": iso_seconds(run_at_utc),
        "automation_timezone": "UTC",
        "automation_rrule": codex_rrule_utc(run_at_utc),
        "outcome": "planned",
        "claims": [],
        "automation_ref": None,
    }


def current_attempt(task: dict[str, Any]) -> dict[str, Any]:
    wanted = int(task.get("current_attempt", 0))
    attempt = next(
        (
            item
            for item in task.get("attempts", [])
            if isinstance(item, dict) and int(item.get("number", 0)) == wanted
        ),
        None,
    )
    if not attempt:
        raise ValueError(f"Review task {task.get('match_id')} has no current attempt")
    return attempt


def ensure_result_delivery(task: dict[str, Any]) -> dict[str, Any]:
    """Backfill two-phase delivery state for compatible legacy tasks."""
    existing = task.get("result_delivery")
    cleanup_completed_at = task.get("cleanup_completed_at")
    task_status = str(task.get("status") or "")
    legacy_inferred = not isinstance(existing, dict)

    if isinstance(existing, dict):
        delivery = existing
    else:
        legacy_delivered = bool(task.get("delivered"))
        if cleanup_completed_at or legacy_delivered:
            status = "delivered"
        elif task_status in FINAL_STATUSES:
            status = "pending"
        else:
            status = "not_ready"
        delivery = {
            "delivery_status": status,
            "thread_id": task.get("thread_id"),
            "result_artifact": task.get("result_artifact"),
            "delivered_at": task.get("delivered_at") if status == "delivered" else None,
        }
        task["result_delivery"] = delivery

    status = str(
        delivery.get("delivery_status")
        or task.get("delivery_status")
        or ""
    )
    if status not in RESULT_DELIVERY_STATUSES:
        status = (
            "delivered"
            if cleanup_completed_at or task.get("delivered")
            else "pending"
            if task_status in FINAL_STATUSES
            else "not_ready"
        )
    if cleanup_completed_at and status != "delivered":
        status = "delivered"
        legacy_inferred = True

    thread_id = delivery.get("thread_id") or task.get("thread_id")
    artifact = delivery.get("result_artifact") or task.get("result_artifact")
    delivered_at = delivery.get("delivered_at") or task.get("delivered_at")
    delivery.update(
        {
            "delivery_status": status,
            "thread_id": thread_id,
            "result_artifact": artifact,
            "delivered_at": delivered_at if status == "delivered" else None,
        }
    )
    if legacy_inferred:
        delivery["legacy_inferred"] = True
    task["delivery_status"] = status
    task["delivered"] = status == "delivered"
    task["thread_id"] = thread_id
    task["result_artifact"] = artifact
    task["delivered_at"] = delivery["delivered_at"]
    return delivery


def set_result_delivery_pending(
    task: dict[str, Any],
    thread_id: str | None = None,
    result_artifact: str | None = None,
) -> dict[str, Any]:
    delivery = ensure_result_delivery(task)
    if delivery.get("delivery_status") != "delivered":
        delivery.update(
            {
                "delivery_status": "pending",
                "thread_id": thread_id or delivery.get("thread_id"),
                "result_artifact": result_artifact or delivery.get("result_artifact"),
                "delivered_at": None,
            }
        )
        delivery.pop("legacy_inferred", None)
        task["delivery_status"] = "pending"
        task["delivered"] = False
        task["thread_id"] = delivery.get("thread_id")
        task["result_artifact"] = delivery.get("result_artifact")
        task["delivered_at"] = None
    return delivery


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
    if delivery.get("delivery_status") not in {"pending", "delivered"}:
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
        or (known_status in FINAL_STATUSES and known_status != status)
        or (tuple_complete and known_reason and known_reason != reason)
    )
    if conflicts:
        raise ValueError("Review result tuple is already recorded for another result")

    # Auto-sync may finalize history before a child task has supplied its result
    # metadata. Only that first metadata fill remains mutable.
    return tuple_complete


def resolve_result_artifact(base_dir: str | None, value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Cannot complete review task without --result-artifact")
    artifact = Path(raw).expanduser()
    if not artifact.is_absolute():
        base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
        artifact = base / artifact
    artifact = artifact.resolve()
    if not artifact.is_file():
        raise ValueError(f"Result artifact does not exist: {artifact}")
    if artifact.stat().st_size <= 0:
        raise ValueError(f"Result artifact is empty: {artifact}")
    return str(artifact)


def get_task(state: dict[str, Any], match_id: str) -> dict[str, Any]:
    task = state["tasks"].get(str(match_id))
    if not task:
        raise ValueError(f"No review task registered for match {match_id}")
    ensure_result_delivery(task)
    return task


def task_result(path: Path, task: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = {"ok": True, "path": str(path), "task": task}
    result.update(extra)
    return result


def normalized_history_time(value: Any, fallback: datetime) -> str:
    if value:
        try:
            return iso_seconds(parse_datetime(str(value)))
        except (TypeError, ValueError):
            pass
    return iso_seconds(fallback)


def sync_history_terminal(
    task: dict[str, Any],
    record: dict[str, Any] | None,
    current: datetime,
) -> None:
    ensure_result_delivery(task)
    if task.get("status") in FINAL_STATUSES or not record:
        return
    record_status = str(record.get("status") or "").strip().lower()
    attempt = current_attempt(task)
    if record_status == "reviewed":
        task["status"] = "completed"
        task["terminal_reason"] = "history_reviewed"
        task["completed_at"] = normalized_history_time(record.get("reviewed_at"), current)
        task["lease_until"] = None
        task["resume_status"] = None
        attempt["outcome"] = "reviewed"
        attempt["finished_at"] = task["completed_at"]
        set_result_delivery_pending(task)
    elif record_status in ADMIN_TERMINAL_STATUSES:
        reason = ADMIN_TERMINAL_STATUSES[record_status]
        task["status"] = "terminal"
        task["terminal_reason"] = reason
        task["terminal_at"] = iso_seconds(current)
        task["lease_until"] = None
        task["resume_status"] = None
        attempt["outcome"] = "terminal"
        attempt["terminal_reason"] = reason
        attempt["finished_at"] = task["terminal_at"]
        set_result_delivery_pending(task)


def cmd_register(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    record = history_record(args.base_dir, args.match_id)
    if not record:
        raise ValueError(f"No archived pre-match prediction for match {args.match_id}")
    if record.get("mode") != "prematch":
        raise ValueError(f"Match {args.match_id} is not an archived pre-match prediction")
    record_status = str(record.get("status") or "").lower()
    if record_status == "reviewed" or record_status in ADMIN_TERMINAL_STATUSES:
        raise ValueError(f"Match {args.match_id} is already terminal: {record_status}")

    kickoff = parse_datetime(str(args.kickoff or record.get("kickoff", "")))
    user_timezone = str(args.user_timezone or DEFAULT_USER_TIMEZONE)
    local_zone = named_timezone(user_timezone)
    local_kickoff = kickoff.astimezone(local_zone)
    first_run = kickoff + timedelta(hours=INITIAL_DELAY_HOURS)
    created_at = iso_seconds(now_utc())
    attempt = make_attempt(1, first_run, local_zone, kind="initial")

    with locked_state(path) as state:
        tasks = state["tasks"]
        existing = tasks.get(str(args.match_id))
        if existing:
            same = (
                parse_datetime(str(existing.get("kickoff"))) == kickoff
                and existing.get("user_timezone") == user_timezone
                and int(existing.get("initial_delay_hours", INITIAL_DELAY_HOURS))
                == INITIAL_DELAY_HOURS
                and int(existing.get("follow_up_minutes", FOLLOW_UP_MINUTES))
                == FOLLOW_UP_MINUTES
            )
            if same:
                return task_result(path, existing, duplicate_ignored=True)
            raise ValueError(f"Review task for match {args.match_id} is already registered")

        task = {
            "match_id": str(args.match_id),
            "home_team": args.home_team or record.get("home_team"),
            "away_team": args.away_team or record.get("away_team"),
            "user_timezone": user_timezone,
            "kickoff": iso_seconds(local_kickoff),
            "initial_delay_hours": INITIAL_DELAY_HOURS,
            "follow_up_minutes": FOLLOW_UP_MINUTES,
            "status": "scheduled",
            "current_attempt": 1,
            "attempts": [attempt],
            "lease_until": None,
            "resume_status": None,
            "automation_refs": [],
            "thread_id": None,
            "result_artifact": None,
            "delivery_status": "not_ready",
            "result_delivery": {
                "delivery_status": "not_ready",
                "thread_id": None,
                "result_artifact": None,
                "delivered_at": None,
            },
            "delivered": False,
            "delivered_at": None,
            "cleaned": False,
            "cleaned_automation_ids": [],
            "cleanup_completed_at": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        tasks[str(args.match_id)] = task
        return task_result(path, task, duplicate_ignored=False)


def cmd_sync_pending(args: argparse.Namespace) -> dict[str, Any]:
    history = load_json(history_path(args.base_dir), [])
    if not isinstance(history, list):
        raise ValueError("history.json must contain an array")
    registered: list[str] = []
    duplicate_ignored: list[str] = []
    skipped_reviewed: list[str] = []
    skipped_invalid: list[dict[str, str]] = []
    for record in history:
        if not isinstance(record, dict) or record.get("mode") != "prematch":
            continue
        match_id = str(record.get("match_id") or "").strip()
        status = str(record.get("status") or "").strip().lower()
        if status == "reviewed":
            if match_id:
                skipped_reviewed.append(match_id)
            continue
        if status != "pending":
            continue
        kickoff = str(record.get("kickoff") or "").strip()
        if not match_id:
            skipped_invalid.append({"match_id": "", "reason": "missing_match_id"})
            continue
        try:
            parse_datetime(kickoff)
        except (TypeError, ValueError):
            skipped_invalid.append(
                {"match_id": match_id, "reason": "kickoff_requires_explicit_offset"}
            )
            continue
        try:
            result = cmd_register(
                argparse.Namespace(
                    base_dir=args.base_dir,
                    match_id=match_id,
                    kickoff=kickoff,
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
        "registered": registered,
        "duplicate_ignored": duplicate_ignored,
        "skipped_reviewed": skipped_reviewed,
        "skipped_invalid": skipped_invalid,
    }


def cmd_attach_automation(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        if task.get("status") in FINAL_STATUSES or task.get("cleanup_completed_at"):
            raise ValueError("Cannot attach an automation to a terminal review task")
        attempt = current_attempt(task)
        if args.attempt_id != attempt["attempt_id"]:
            raise ValueError(
                f"Automation attempt must be the current attempt {attempt['attempt_id']}"
            )
        if args.automation_rrule != attempt["automation_rrule"]:
            raise ValueError(
                "Automation RRULE does not match the expected UTC rule: "
                f"{attempt['automation_rrule']}"
            )
        existing = attempt.get("automation_ref")
        if existing:
            same = (
                existing.get("id") == args.automation_id
                and existing.get("automation_rrule") == args.automation_rrule
            )
            if same:
                return task_result(path, task, duplicate_ignored=True)
            raise ValueError(f"Attempt {attempt['attempt_id']} already has an automation")
        ref = {
            "id": args.automation_id,
            "name": args.automation_name,
            "attempt_id": attempt["attempt_id"],
            "run_at": attempt["run_at"],
            "run_at_utc": attempt["run_at_utc"],
            "automation_rrule": args.automation_rrule,
            "schedule_verified": True,
        }
        attempt["automation_ref"] = ref
        task.setdefault("automation_refs", []).append(ref)
        task["updated_at"] = iso_seconds(now_utc())
        return task_result(path, task, duplicate_ignored=False)


def cmd_claim(args: argparse.Namespace) -> dict[str, Any]:
    if args.lease_minutes <= 0:
        raise ValueError("--lease-minutes must be positive")
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    record = history_record(args.base_dir, args.match_id)
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        sync_history_terminal(task, record, current)
        if task.get("status") in FINAL_STATUSES:
            return task_result(path, task, claimed=False, reason=task["status"])
        attempt = current_attempt(task)
        run_at = parse_datetime(str(attempt["run_at_utc"]))
        if current < run_at:
            return task_result(path, task, claimed=False, reason="too_early")
        lease_until = (
            parse_datetime(str(task["lease_until"])) if task.get("lease_until") else None
        )
        if task.get("status") == "claimed" and lease_until and lease_until > current:
            return task_result(path, task, claimed=False, reason="active_lease")

        resume_status = task.get("status")
        if resume_status == "claimed":
            resume_status = task.get("resume_status") or (
                "scheduled" if int(attempt["number"]) == 1 else "waiting"
            )
        lease_until = current + timedelta(minutes=args.lease_minutes)
        claim = {
            "number": len(attempt.setdefault("claims", [])) + 1,
            "claimed_at": iso_seconds(current),
            "lease_until": iso_seconds(lease_until),
            "catch_up": current > run_at + timedelta(seconds=60),
        }
        attempt["claims"].append(claim)
        attempt["outcome"] = "claimed"
        task["resume_status"] = resume_status
        task["status"] = "claimed"
        task["lease_until"] = claim["lease_until"]
        task["updated_at"] = iso_seconds(current)
        return task_result(
            path,
            task,
            claimed=True,
            catch_up=claim["catch_up"],
            attempt_id=attempt["attempt_id"],
        )


def cmd_release(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        if task.get("status") in FINAL_STATUSES:
            return task_result(path, task, released=False, reason=task["status"])
        if task.get("status") != "claimed":
            return task_result(path, task, released=False, reason="not_claimed")
        attempt = current_attempt(task)
        restored = task.get("resume_status") or (
            "scheduled" if int(attempt["number"]) == 1 else "waiting"
        )
        task["status"] = restored
        task["lease_until"] = None
        task["resume_status"] = None
        task["last_error"] = args.reason
        task["last_failed_at"] = iso_seconds(current)
        attempt["outcome"] = "released"
        if attempt.get("claims"):
            attempt["claims"][-1]["failed_at"] = iso_seconds(current)
            attempt["claims"][-1]["error"] = args.reason
        task["updated_at"] = iso_seconds(current)
        return task_result(path, task, released=True)


def cmd_wait(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        if task.get("status") in FINAL_STATUSES:
            return task_result(path, task, follow_up_created=False, reason=task["status"])
        if task.get("status") == "waiting":
            return task_result(
                path,
                task,
                follow_up_created=False,
                duplicate_ignored=True,
                follow_up=current_attempt(task),
            )
        if task.get("status") != "claimed":
            raise ValueError("A review attempt must be claimed before scheduling a follow-up")

        previous = current_attempt(task)
        previous["outcome"] = "not_terminal"
        previous["status_reason"] = args.reason
        previous["finished_at"] = iso_seconds(current)
        number = int(previous["number"]) + 1
        local_zone = named_timezone(str(task["user_timezone"]))
        run_at = current + timedelta(minutes=FOLLOW_UP_MINUTES)
        follow_up = make_attempt(
            number,
            run_at,
            local_zone,
            kind="follow-up",
            reason=args.reason,
        )
        task["attempts"].append(follow_up)
        task["current_attempt"] = number
        task["status"] = "waiting"
        task["lease_until"] = None
        task["resume_status"] = None
        task["updated_at"] = iso_seconds(current)
        return task_result(
            path,
            task,
            follow_up_created=True,
            duplicate_ignored=False,
            follow_up=follow_up,
        )


def cmd_complete(args: argparse.Namespace) -> dict[str, Any]:
    thread_id = str(getattr(args, "thread_id", "") or "").strip()
    if not thread_id:
        raise ValueError("Cannot complete review task without a non-empty --thread-id")
    result_artifact = resolve_result_artifact(
        args.base_dir, getattr(args, "result_artifact", None)
    )
    record = history_record(args.base_dir, args.match_id)
    if not record or str(record.get("status") or "").lower() != "reviewed":
        raise ValueError("Cannot complete a review task before history.json is reviewed")
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        duplicate = result_tuple_is_duplicate(
            task,
            status="completed",
            reason="history_reviewed",
            thread_id=thread_id,
            result_artifact=result_artifact,
        )
        if duplicate:
            return task_result(
                path,
                task,
                duplicate_ignored=True,
                cleanup_automation_refs=task.get("automation_refs", []),
            )
        attempt = current_attempt(task)
        attempt["outcome"] = "reviewed"
        attempt["finished_at"] = normalized_history_time(record.get("reviewed_at"), current)
        task["status"] = "completed"
        task["terminal_reason"] = "history_reviewed"
        task["completed_at"] = attempt["finished_at"]
        task["lease_until"] = None
        task["resume_status"] = None
        task["thread_id"] = thread_id
        task["result_artifact"] = result_artifact
        set_result_delivery_pending(task, thread_id, result_artifact)
        task["updated_at"] = iso_seconds(current)
        return task_result(
            path,
            task,
            duplicate_ignored=False,
            cleanup_automation_refs=task.get("automation_refs", []),
        )


def cmd_terminal(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        thread_id = str(getattr(args, "thread_id", "") or "").strip()
        if not thread_id:
            raise ValueError(
                "Cannot finish review terminal state without a non-empty --thread-id"
            )
        result_artifact = resolve_result_artifact(
            args.base_dir, getattr(args, "result_artifact", None)
        )
        duplicate = result_tuple_is_duplicate(
            task,
            status="terminal",
            reason=args.reason,
            thread_id=thread_id,
            result_artifact=result_artifact,
        )
        if duplicate:
            return task_result(
                path,
                task,
                duplicate_ignored=True,
                cleanup_automation_refs=task.get("automation_refs", []),
            )
        attempt = current_attempt(task)
        attempt["outcome"] = "terminal"
        attempt["terminal_reason"] = args.reason
        attempt["finished_at"] = iso_seconds(current)
        task["status"] = "terminal"
        task["terminal_reason"] = args.reason
        task["terminal_at"] = iso_seconds(current)
        task["lease_until"] = None
        task["resume_status"] = None
        task["thread_id"] = thread_id
        task["result_artifact"] = result_artifact
        set_result_delivery_pending(task, thread_id, result_artifact)
        task["updated_at"] = iso_seconds(current)
        return task_result(
            path,
            task,
            duplicate_ignored=False,
            cleanup_automation_refs=task.get("automation_refs", []),
        )


def cmd_mark_delivered(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    supplied_thread_id = str(getattr(args, "thread_id", "") or "").strip()
    if not supplied_thread_id:
        raise ValueError("--thread-id is required to mark a review result delivered")
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        if task.get("status") not in FINAL_STATUSES:
            raise ValueError("Cannot mark review result delivered before the task is terminal")
        delivery = ensure_result_delivery(task)
        expected_thread_id = str(delivery.get("thread_id") or "").strip()
        if not expected_thread_id or supplied_thread_id != expected_thread_id:
            raise ValueError("Delivered thread id does not match the archived review task")
        if delivery.get("result_artifact"):
            resolved_artifact = resolve_result_artifact(
                args.base_dir, delivery.get("result_artifact")
            )
        elif delivery.get("legacy_inferred"):
            resolved_artifact = None
        else:
            raise ValueError("The review result artifact is missing")
        if delivery.get("delivery_status") == "delivered":
            return task_result(path, task, duplicate_ignored=True)
        delivery.update(
            {
                "delivery_status": "delivered",
                "delivered_at": iso_seconds(current),
            }
        )
        if resolved_artifact:
            delivery["result_artifact"] = resolved_artifact
        delivery.pop("legacy_inferred", None)
        task["delivery_status"] = "delivered"
        task["delivered"] = True
        if resolved_artifact:
            task["result_artifact"] = resolved_artifact
        task["delivered_at"] = delivery["delivered_at"]
        task["updated_at"] = iso_seconds(current)
        return task_result(path, task, duplicate_ignored=False)


def cmd_mark_cleaned(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        if task.get("status") not in FINAL_STATUSES:
            raise ValueError("Only completed or terminal review tasks can be cleaned")
        delivery = ensure_result_delivery(task)
        if delivery.get("delivery_status") != "delivered":
            raise ValueError("The review result must be delivered before cleanup")
        known = {str(ref.get("id")) for ref in task.get("automation_refs", [])}
        supplied = {str(value) for value in (args.automation_id or [])}
        unknown = sorted(supplied - known)
        missing = sorted(known - supplied)
        if unknown:
            raise ValueError(f"Unknown automation id(s): {', '.join(unknown)}")
        if missing:
            raise ValueError(f"Automation id(s) still require cleanup: {', '.join(missing)}")
        if task.get("cleaned"):
            return task_result(path, task, duplicate_ignored=True)
        task["cleaned"] = True
        task["cleaned_automation_ids"] = sorted(supplied)
        task["cleanup_completed_at"] = iso_seconds(current)
        task["updated_at"] = iso_seconds(current)
        return task_result(path, task, duplicate_ignored=False)


def cmd_cleanup_due(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    due: list[dict[str, Any]] = []
    requested_match_id = str(args.match_id) if getattr(args, "match_id", None) else None
    with locked_state(path) as state:
        for task in state["tasks"].values():
            if requested_match_id and str(task.get("match_id")) != requested_match_id:
                continue
            sync_history_terminal(
                task,
                history_record(args.base_dir, str(task["match_id"])),
                current,
            )
            if task.get("status") not in FINAL_STATUSES or task.get("cleanup_completed_at"):
                continue
            delivery = ensure_result_delivery(task)
            delivery_status = str(delivery.get("delivery_status") or "")
            if delivery_status == "pending":
                has_thread = bool(str(delivery.get("thread_id") or "").strip())
                has_artifact = bool(str(delivery.get("result_artifact") or "").strip())
                metadata_complete = has_thread and (
                    has_artifact or delivery.get("legacy_inferred")
                )
                if not metadata_complete and result_metadata_grace_active(task, current):
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


def cmd_due(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    due: list[dict[str, Any]] = []
    with locked_state(path) as state:
        for task in state["tasks"].values():
            record = history_record(args.base_dir, str(task["match_id"]))
            sync_history_terminal(task, record, current)
            if task.get("status") in FINAL_STATUSES:
                continue
            attempt = current_attempt(task)
            run_at = parse_datetime(str(attempt["run_at_utc"]))
            lease_until = (
                parse_datetime(str(task["lease_until"])) if task.get("lease_until") else None
            )
            if current >= run_at and (not lease_until or lease_until <= current):
                item = dict(task)
                item["attempt"] = dict(attempt)
                item["catch_up"] = current > run_at + timedelta(seconds=60)
                due.append(item)
        due.sort(key=lambda item: item["attempt"]["run_at_utc"])
        return {
            "ok": True,
            "path": str(path),
            "checked_at": iso_seconds(current),
            "due": due,
        }


def cmd_automation_plan(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        sync_history_terminal(
            task,
            history_record(args.base_dir, args.match_id),
            current,
        )
        future_attempts: list[dict[str, Any]] = []
        catch_up_required = False
        if task.get("status") not in FINAL_STATUSES:
            attempt = current_attempt(task)
            run_at = parse_datetime(str(attempt["run_at_utc"]))
            lease_until = (
                parse_datetime(str(task["lease_until"])) if task.get("lease_until") else None
            )
            active_lease = task.get("status") == "claimed" and lease_until and lease_until > current
            if not active_lease:
                if current < run_at:
                    if not attempt.get("automation_ref"):
                        future_attempts.append(dict(attempt))
                else:
                    catch_up_required = True
        return {
            "ok": True,
            "path": str(path),
            "checked_at": iso_seconds(current),
            "match_id": str(task["match_id"]),
            "status": task.get("status"),
            "catch_up_required": catch_up_required,
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
            sync_history_terminal(
                task,
                history_record(args.base_dir, args.match_id),
                current,
            )
            return task_result(path, task)
        for task in state["tasks"].values():
            sync_history_terminal(
                task,
                history_record(args.base_dir, str(task["match_id"])),
                current,
            )
        return {"ok": True, "path": str(path), "tasks": list(state["tasks"].values())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", help="Workspace root; defaults to current directory")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser(
        "register",
        help="Register one idempotent post-match review check at kickoff plus three hours",
    )
    register.add_argument("--match-id", required=True)
    register.add_argument("--kickoff", help="Kickoff with explicit offset; defaults to history.json")
    register.add_argument("--user-timezone", default=DEFAULT_USER_TIMEZONE)
    register.add_argument("--home-team")
    register.add_argument("--away-team")

    sync_pending = sub.add_parser(
        "sync-pending",
        aliases=["bootstrap"],
        help="Idempotently register every offset-aware pending pre-match record",
    )
    sync_pending.add_argument("--user-timezone", default=DEFAULT_USER_TIMEZONE)
    sync_pending.add_argument(
        "--now",
        help="Accepted for watchdog snapshot consistency; registration remains idempotent",
    )

    attach = sub.add_parser(
        "attach-automation",
        help="Attach the single Codex automation for the current attempt",
    )
    attach.add_argument("--match-id", required=True)
    attach.add_argument("--attempt-id", required=True)
    attach.add_argument("--automation-id", required=True)
    attach.add_argument("--automation-name", required=True)
    attach.add_argument("--automation-rrule", required=True)

    claim = sub.add_parser("claim", help="Atomically claim a due review status check")
    claim.add_argument("--match-id", required=True)
    claim.add_argument("--now", help="ISO datetime with offset, for deterministic checks")
    claim.add_argument("--lease-minutes", type=float, default=10.0)

    release = sub.add_parser("release", help="Release a failed review claim")
    release.add_argument("--match-id", required=True)
    release.add_argument("--reason", required=True)
    release.add_argument("--now")

    wait = sub.add_parser(
        "wait",
        help="Record a non-terminal match and create one follow-up thirty minutes later",
    )
    wait.add_argument("--match-id", required=True)
    wait.add_argument(
        "--reason",
        choices=(
            "prematch",
            "live",
            "half-time",
            "extra-time",
            "penalties",
            "interrupted",
            "unknown",
        ),
        required=True,
    )
    wait.add_argument("--now")

    complete = sub.add_parser(
        "complete",
        help="Stage a reviewed result for delivery in its own Codex task",
    )
    complete.add_argument("--match-id", required=True)
    complete.add_argument("--thread-id", required=True)
    complete.add_argument("--result-artifact", required=True)
    complete.add_argument("--now")

    terminal = sub.add_parser(
        "terminal",
        help="Stop retries for a cancelled, postponed, or abandoned match",
    )
    terminal.add_argument("--match-id", required=True)
    terminal.add_argument(
        "--reason",
        choices=("cancelled", "postponed", "abandoned"),
        required=True,
    )
    terminal.add_argument("--thread-id", required=True)
    terminal.add_argument("--result-artifact", required=True)
    terminal.add_argument("--now")

    delivered = sub.add_parser(
        "mark-delivered",
        help="Record delivery only after the Codex task has a completed final answer",
    )
    delivered.add_argument("--match-id", required=True)
    delivered.add_argument("--thread-id", required=True)
    delivered.add_argument("--now")

    cleaned = sub.add_parser(
        "mark-cleaned",
        help="Confirm every attached automation was deleted or disabled",
    )
    cleaned.add_argument("--match-id", required=True)
    cleaned.add_argument("--automation-id", action="append")
    cleaned.add_argument("--now")

    due = sub.add_parser("due", help="List due checks, including executor catch-up work")
    due.add_argument("--now")

    cleanup_due = sub.add_parser(
        "cleanup-due",
        help="List terminal review tasks awaiting delivery verification or cleanup",
    )
    cleanup_due.add_argument("--match-id")
    cleanup_due.add_argument("--now")

    automation_plan = sub.add_parser(
        "automation-plan",
        help="Return at most one safe future Codex automation attempt",
    )
    automation_plan.add_argument("--match-id", required=True)
    automation_plan.add_argument("--now")

    status = sub.add_parser("status", help="Show persisted review task state")
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
            "wait": cmd_wait,
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
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

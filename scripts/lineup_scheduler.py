#!/usr/bin/env python3
"""Persistent, retry-safe scheduler state for soccer-predict lineup checks."""

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
DEFAULT_SOURCE_TIMEZONE = "Asia/Shanghai"
RETRY_MINUTES = (30, 25, 20, 15, 10, 5, 2)
TERMINAL_STATUSES = {"completed", "expired", "started", "finished", "cancelled", "postponed"}


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
    return next((item for item in history if str(item.get("match_id")) == str(match_id)), None)


def retry_plan(kickoff: datetime, local_zone) -> list[dict[str, Any]]:
    return [
        {
            "minutes_before_kickoff": minutes,
            "run_at": iso_seconds((kickoff - timedelta(minutes=minutes)).astimezone(local_zone)),
            "label": "T-30" if minutes == 30 else f"retry-T-{minutes}",
        }
        for minutes in RETRY_MINUTES
    ]


def sync_terminal(task: dict[str, Any], record: dict[str, Any] | None, current: datetime) -> None:
    if task.get("status") in TERMINAL_STATUSES:
        return
    if record and record.get("lineup_rechecked_at"):
        task["status"] = "completed"
        task["completed_at"] = record["lineup_rechecked_at"]
        task["lease_until"] = None
        task["terminal_reason"] = "lineup_revision_archived"
        return
    kickoff = parse_datetime(str(task["kickoff"]))
    if current >= kickoff:
        task["status"] = "expired"
        task["terminal_reason"] = "kickoff_reached_without_lineup_revision"
        task["terminal_at"] = iso_seconds(current)
        task["lease_until"] = None


def task_result(path: Path, task: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = {"ok": True, "path": str(path), "task": task}
    result.update(extra)
    return result


def cmd_register(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    record = history_record(args.base_dir, args.match_id)
    if not record and not args.kickoff:
        raise ValueError(f"No archived prediction for match {args.match_id}; pass --kickoff explicitly")
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
            )
            if same:
                return task_result(path, existing, duplicate_ignored=True)
            if existing.get("status") in TERMINAL_STATUSES:
                raise ValueError(f"Refusing to replace terminal lineup task for match {args.match_id}")
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
        }
        tasks[str(args.match_id)] = task
        return task_result(path, task, duplicate_ignored=False)


def get_task(state: dict[str, Any], match_id: str) -> dict[str, Any]:
    task = state["tasks"].get(str(match_id))
    if not task:
        raise ValueError(f"No lineup task registered for match {match_id}")
    return task


def cmd_attach_automation(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        ref = {"id": args.automation_id, "name": args.automation_name}
        refs = task.setdefault("automation_refs", [])
        if ref not in refs:
            refs.append(ref)
        task["updated_at"] = iso_seconds(now_utc())
        return task_result(path, task)


def cmd_claim(args: argparse.Namespace) -> dict[str, Any]:
    if args.lease_minutes <= 0:
        raise ValueError("--lease-minutes must be positive")
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
        }
        task["attempts"].append(attempt)
        task["status"] = "claimed"
        task["lease_until"] = attempt["lease_until"]
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
    path = state_path(args.base_dir)
    record = history_record(args.base_dir, args.match_id)
    if not record or not record.get("lineup_rechecked_at"):
        raise ValueError("Cannot complete lineup task before a lineup-check revision is archived")
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        task["status"] = "completed"
        task["completed_at"] = record["lineup_rechecked_at"]
        task["terminal_reason"] = "lineup_revision_archived"
        task["lease_until"] = None
        task["thread_id"] = args.thread_id
        task["updated_at"] = iso_seconds(current)
        return task_result(path, task, cleanup_automation_refs=task.get("automation_refs", []))


def cmd_terminal(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        task["status"] = args.reason
        task["terminal_reason"] = args.reason
        task["terminal_at"] = iso_seconds(current)
        task["lease_until"] = None
        task["updated_at"] = iso_seconds(current)
        return task_result(path, task, cleanup_automation_refs=task.get("automation_refs", []))


def cmd_mark_cleaned(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        task = get_task(state, args.match_id)
        known = {ref.get("id") for ref in task.get("automation_refs", [])}
        unknown = sorted(set(args.automation_id or []) - known)
        if unknown:
            raise ValueError(f"Unknown automation id(s): {', '.join(unknown)}")
        task["cleaned_automation_ids"] = sorted(set(args.automation_id or []))
        task["cleanup_completed_at"] = iso_seconds(current)
        task["updated_at"] = iso_seconds(current)
        return task_result(path, task)


def cmd_due(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    due: list[dict[str, Any]] = []
    with locked_state(path) as state:
        for task in state["tasks"].values():
            record = history_record(args.base_dir, str(task["match_id"]))
            sync_terminal(task, record, current)
            if task.get("status") in TERMINAL_STATUSES:
                continue
            scheduled = parse_datetime(str(task["scheduled_for"]))
            kickoff = parse_datetime(str(task["kickoff"]))
            lease = parse_datetime(str(task["lease_until"])) if task.get("lease_until") else None
            if scheduled <= current < kickoff and (not lease or lease <= current):
                item = dict(task)
                item["catch_up"] = current > scheduled + timedelta(seconds=60)
                item["minutes_to_kickoff"] = round((kickoff - current).total_seconds() / 60, 1)
                due.append(item)
        due.sort(key=lambda item: item["kickoff"])
        return {"ok": True, "path": str(path), "checked_at": iso_seconds(current), "due": due}


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    path = state_path(args.base_dir)
    current = parse_datetime(args.now) if args.now else now_utc()
    with locked_state(path) as state:
        if args.match_id:
            task = get_task(state, args.match_id)
            sync_terminal(task, history_record(args.base_dir, args.match_id), current)
            return task_result(path, task)
        for task in state["tasks"].values():
            sync_terminal(task, history_record(args.base_dir, str(task["match_id"])), current)
        return {"ok": True, "path": str(path), "tasks": list(state["tasks"].values())}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", help="Workspace root; defaults to current directory")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="Register an idempotent T-30 schedule and retry plan")
    register.add_argument("--match-id", required=True)
    register.add_argument("--kickoff", help="Kickoff with explicit UTC offset; defaults to archived record")
    register.add_argument("--source-timezone", default=DEFAULT_SOURCE_TIMEZONE)
    register.add_argument("--user-timezone", default=DEFAULT_USER_TIMEZONE)
    register.add_argument("--home-team")
    register.add_argument("--away-team")

    attach = sub.add_parser("attach-automation", help="Attach a Codex automation id for later cleanup")
    attach.add_argument("--match-id", required=True)
    attach.add_argument("--automation-id", required=True)
    attach.add_argument("--automation-name", required=True)

    claim = sub.add_parser("claim", help="Atomically claim a due prematch lineup check")
    claim.add_argument("--match-id", required=True)
    claim.add_argument("--now", help="ISO datetime with offset, for deterministic checks")
    claim.add_argument("--lease-minutes", type=float, default=4.0)

    release = sub.add_parser("release", help="Release a failed claim so the next retry can run")
    release.add_argument("--match-id", required=True)
    release.add_argument("--reason", required=True)
    release.add_argument("--now")

    complete = sub.add_parser("complete", help="Complete only after the lineup revision is archived")
    complete.add_argument("--match-id", required=True)
    complete.add_argument("--thread-id")
    complete.add_argument("--now")

    terminal = sub.add_parser("terminal", help="Stop retries for an explicit terminal match state")
    terminal.add_argument("--match-id", required=True)
    terminal.add_argument("--reason", choices=("started", "finished", "cancelled", "postponed", "expired"), required=True)
    terminal.add_argument("--now")

    cleaned = sub.add_parser("mark-cleaned", help="Record deletion/disablement of attached automations")
    cleaned.add_argument("--match-id", required=True)
    cleaned.add_argument("--automation-id", action="append")
    cleaned.add_argument("--now")

    due = sub.add_parser("due", help="List due or missed-but-still-prematch checks")
    due.add_argument("--now")

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
            "attach-automation": cmd_attach_automation,
            "claim": cmd_claim,
            "release": cmd_release,
            "complete": cmd_complete,
            "terminal": cmd_terminal,
            "mark-cleaned": cmd_mark_cleaned,
            "due": cmd_due,
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

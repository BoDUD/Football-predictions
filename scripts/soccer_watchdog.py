#!/usr/bin/env python3
"""Poll soccer-predict schedulers and persist due work without fabricating analysis.

This process is deliberately a small bridge, not an analysis agent. It
synchronizes registrations, runs lineup/review due and cleanup queries, writes
each item verbatim to a workspace-local outbox, and opens the verified local
Codex Store app when work is due. A paired recurring Codex dispatcher consumes
the outbox and creates visible match-specific tasks; package activation alone
does not consume or analyze an event.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterator


SCHEMA_VERSION = 1
DISPATCH_COOLDOWN = timedelta(minutes=10)
OUTBOX_RELATIVE = Path(".codex") / "soccer-predict" / "outbox"
PROCESSED_RELATIVE = Path(".codex") / "soccer-predict" / "processed"
STATUS_RELATIVE = Path(".codex") / "soccer-predict" / "watchdog_status.json"
LOCK_RELATIVE = Path(".codex") / "soccer-predict" / "watchdog.lock"
CODEX_WAKE_LIMITATION = (
    "The Windows watchdog only persists due scheduler events and opens Codex. "
    "A separately configured recurring Codex dispatcher must consume the outbox "
    "and create visible match-specific tasks; package activation alone does not "
    "consume or analyze events."
)
CODEX_PACKAGE_NAME = "OpenAI.Codex"
CODEX_PUBLISHER_ID = "2p2nqsd0c76g0"
CODEX_FALLBACK_AUMID = f"{CODEX_PACKAGE_NAME}_{CODEX_PUBLISHER_ID}!App"


@dataclass(frozen=True)
class SchedulerSpec:
    name: str
    filename: str
    event_type: str
    command: str = "due"
    produces_due: bool = True


PREPARATIONS = (
    SchedulerSpec("lineup-sync", "lineup_scheduler.py", "", "sync-pending", False),
    SchedulerSpec("review-sync", "review_scheduler.py", "", "sync-pending", False),
)
SCHEDULERS = (
    SchedulerSpec("lineup", "lineup_scheduler.py", "lineup-check-due"),
    SchedulerSpec("review", "review_scheduler.py", "post-match-review-due"),
    SchedulerSpec(
        "lineup-cleanup",
        "lineup_scheduler.py",
        "lineup-result-cleanup-due",
        "cleanup-due",
    ),
    SchedulerSpec(
        "review-cleanup",
        "review_scheduler.py",
        "review-result-cleanup-due",
        "cleanup-due",
    ),
)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def iso_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def default_workspace() -> Path:
    # Repository layout: <workspace>/Football-predictions/scripts/this-file.
    # This is explicit and deterministic; it never depends on HOME or cwd.
    return default_skill_dir().parent.resolve()


def resolve_existing_directory(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value).resolve()
    if not path.is_dir():
        raise ValueError(f"{label} is not an existing directory: {path}")
    return path


def scheduler_command(
    python_executable: Path,
    script: Path,
    workspace: Path,
    now: str | None,
    command_name: str,
) -> list[str]:
    command = [
        str(python_executable),
        str(script),
        "--base-dir",
        str(workspace),
        command_name,
    ]
    if now:
        command.extend(["--now", now])
    return command


def parse_scheduler_output(
    stdout: str, name: str, *, produces_due: bool
) -> dict[str, Any]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} scheduler returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} scheduler JSON must be an object")
    if payload.get("ok") is not True:
        raise ValueError(
            f"{name} scheduler reported failure: {payload.get('error', 'unknown error')}"
        )
    if produces_due:
        due = payload.get("due")
        if not isinstance(due, list) or any(not isinstance(item, dict) for item in due):
            raise ValueError(f"{name} scheduler JSON must contain a due object array")
    return payload


def run_scheduler(
    spec: SchedulerSpec,
    *,
    skill_dir: Path,
    workspace: Path,
    python_executable: Path,
    timeout_seconds: float,
    now: str | None = None,
) -> dict[str, Any]:
    script = (skill_dir / "scripts" / spec.filename).resolve()
    result: dict[str, Any] = {
        "scheduler": spec.name,
        "script": str(script),
        "attempted": True,
        "ok": False,
        "due": [],
    }
    if not script.is_file():
        result["error"] = f"scheduler script is missing: {script}"
        return result

    command = scheduler_command(
        python_executable, script, workspace, now, spec.command
    )
    result["command"] = command
    try:
        completed = subprocess.run(
            command,
            cwd=str(skill_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = f"scheduler execution failed: {exc}"
        return result

    result["returncode"] = completed.returncode
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        result["error"] = f"scheduler exited {completed.returncode}: {detail}"
        return result
    try:
        payload = parse_scheduler_output(
            completed.stdout, spec.name, produces_due=spec.produces_due
        )
    except ValueError as exc:
        result["error"] = str(exc)
        return result

    result["ok"] = True
    result["checked_at"] = payload.get("checked_at")
    result["due"] = payload.get("due", [])
    result["payload"] = payload
    return result


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return parsed


def event_identity(spec: SchedulerSpec, item: dict[str, Any]) -> str:
    match_id = item.get("match_id")
    attempt = item.get("attempt") if isinstance(item.get("attempt"), dict) else {}
    boundary = (
        attempt.get("attempt_id")
        or attempt.get("run_at_utc")
        or next(
            (
                item.get(field)
                for field in (
                    "scheduled_for",
                    "review_due_at",
                    "review_after",
                    "kickoff",
                    "finished_at",
                )
                if item.get(field)
            ),
            None,
        )
    )
    if match_id is not None:
        source = canonical_json(
            {
                "scheduler": spec.name,
                "match_id": str(match_id),
                "boundary": boundary,
            }
        )
    else:
        source = canonical_json({"scheduler": spec.name, "due": item})
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
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
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def queue_due_item(
    workspace: Path,
    spec: SchedulerSpec,
    item: dict[str, Any],
    detected_at: str,
) -> tuple[Path, bool]:
    event_id = event_identity(spec, item)
    due_fingerprint = payload_fingerprint(item)
    outbox = workspace / OUTBOX_RELATIVE
    event_path = outbox / f"{spec.name}-{event_id}.json"
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": spec.event_type,
        "scheduler": spec.name,
        "detected_at": detected_at,
        "workspace": str(workspace),
        "analysis_state": "not_started",
        "delivery_state": "pending",
        "due": item,
        "due_fingerprint": due_fingerprint,
        "consumer_contract": (
            "Claim this exact scheduler item and run the soccer-predict Skill in a "
            "visible Codex task. This file is not an analysis or recommendation."
        ),
    }
    with exclusive_lock(workspace / LOCK_RELATIVE):
        if event_path.exists():
            try:
                existing = json.loads(event_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if not isinstance(existing, dict):
                existing = {}
            # Keep dispatcher-owned audit fields, but always refresh scheduler
            # payload fields atomically. Cleanup items can retain one event id
            # while advancing from await_complete_metadata to verify_delivery.
            refreshed = dict(existing)
            refreshed.update(event)
            refreshed["event_id"] = event_id
            refreshed["delivery_state"] = "pending"
            atomic_write_json(event_path, refreshed)
            return event_path, False

        now = parse_aware_datetime(detected_at, "detected_at")
        processed = workspace / PROCESSED_RELATIVE
        for processed_path in processed.glob(f"{spec.name}-{event_id}-*.json"):
            try:
                previous = json.loads(processed_path.read_text(encoding="utf-8"))
                dispatched_at = parse_aware_datetime(
                    str(previous["dispatched_at"]), "processed dispatched_at"
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            previous_fingerprint = previous.get("due_fingerprint")
            if not previous_fingerprint and isinstance(previous.get("due"), dict):
                previous_fingerprint = payload_fingerprint(previous["due"])
            if (
                previous_fingerprint == due_fingerprint
                and now < dispatched_at + DISPATCH_COOLDOWN
            ):
                return event_path, False
        atomic_write_json(event_path, event)
        return event_path, True


def list_pending_events(workspace: Path) -> list[dict[str, Any]]:
    outbox = workspace / OUTBOX_RELATIVE
    if not outbox.exists():
        return []
    events: list[dict[str, Any]] = []
    for path in sorted(outbox.glob("*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(event, dict)
            and event.get("delivery_state") == "pending"
            and event.get("event_id")
            and event.get("scheduler")
        ):
            event["event_path"] = str(path)
            events.append(event)
    return events


def acknowledge_event(
    workspace: Path,
    *,
    scheduler: str,
    event_id: str,
    thread_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    valid_schedulers = {spec.name for spec in SCHEDULERS}
    if scheduler not in valid_schedulers:
        raise ValueError(f"Unknown scheduler: {scheduler}")
    if not re.fullmatch(r"[0-9a-f]{64}", event_id):
        raise ValueError("event id must be a 64-character lowercase SHA-256 value")
    thread_id = thread_id.strip()
    if not thread_id:
        raise ValueError("thread id is required to acknowledge an event")
    outbox = workspace / OUTBOX_RELATIVE
    source = outbox / f"{scheduler}-{event_id}.json"
    processed = workspace / PROCESSED_RELATIVE
    with exclusive_lock(workspace / LOCK_RELATIVE):
        if not source.is_file():
            raise ValueError(f"Pending event does not exist: {source}")
        event = json.loads(source.read_text(encoding="utf-8"))
        if (
            event.get("event_id") != event_id
            or event.get("scheduler") != scheduler
        ):
            raise ValueError("Pending event identity does not match its file name")
        dispatched_at = now or iso_utc_now()
        parse_aware_datetime(dispatched_at, "dispatch time")
        event["delivery_state"] = "dispatched"
        event["dispatched_at"] = dispatched_at
        event["dispatched_thread_id"] = thread_id
        processed.mkdir(parents=True, exist_ok=True)
        suffix = hashlib.sha256(
            f"{thread_id}|{dispatched_at}".encode("utf-8")
        ).hexdigest()[:12]
        destination = processed / f"{scheduler}-{event_id}-{suffix}.json"
        atomic_write_json(destination, event)
        source.unlink()
    return {
        "ok": True,
        "event_id": event_id,
        "scheduler": scheduler,
        "thread_id": thread_id,
        "processed_path": str(destination),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_codex_executable(
    executable: str | None,
    expected_sha256: str | None,
) -> Path | None:
    if not executable and not expected_sha256:
        return None
    if not executable or not expected_sha256:
        raise ValueError(
            "--codex-executable and --codex-sha256 must be supplied together"
        )
    raw = Path(executable)
    if not raw.is_absolute():
        raise ValueError("Codex executable must be an absolute path")
    if str(raw).startswith("\\\\"):
        raise ValueError("Codex executable must be on a local drive, not a UNC path")
    if raw.is_symlink():
        raise ValueError("Codex executable must not be a symbolic link")
    path = raw.resolve()
    if not path.is_file() or path.suffix.lower() != ".exe":
        raise ValueError(f"Codex executable is not a local .exe file: {path}")
    if path.name.lower() != "codex.exe":
        raise ValueError("Codex executable filename must be Codex.exe")
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise ValueError("--codex-sha256 must be a 64-character hexadecimal digest")
    actual = sha256_file(path)
    if not hmac.compare_digest(actual, expected):
        raise ValueError(
            f"Codex executable hash mismatch; expected {expected}, found {actual}"
        )
    return path


def windows_system_executable(filename: str) -> Path:
    if os.name != "nt":
        raise OSError("Windows system executables are available only on Windows")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if length <= 0 or length >= len(buffer):
        raise OSError("GetWindowsDirectoryW failed")
    windows = Path(buffer.value)
    candidates = (
        (windows / filename).resolve(),
        (windows / "System32" / filename).resolve(),
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise OSError(
            "Windows system executable is missing: "
            + ", ".join(str(candidate) for candidate in candidates)
        )
    return path


def discover_codex_package() -> tuple[dict[str, str] | None, str | None]:
    """Discover the registered Store package and manifest application id.

    A fixed, non-interpolated PowerShell command queries the current user's
    package registration.  All returned identity fields are validated before an
    AUMID is constructed.
    """
    if os.name != "nt":
        return None, "Codex package discovery is supported only on Windows"
    try:
        powershell = windows_system_executable(
            r"WindowsPowerShell\v1.0\powershell.exe"
        )
        command = (
            "$pkg = Get-AppxPackage -Name 'OpenAI.Codex' | "
            "Sort-Object Version -Descending | Select-Object -First 1; "
            "if ($null -eq $pkg) { exit 3 }; "
            "$manifest = Get-AppxPackageManifest -Package $pkg; "
            "$app = @($manifest.Package.Applications.Application)[0]; "
            "[pscustomobject]@{"
            "Name=[string]$pkg.Name;"
            "PackageFamilyName=[string]$pkg.PackageFamilyName;"
            "PublisherId=[string]$pkg.PublisherId;"
            "SignatureKind=[string]$pkg.SignatureKind;"
            "ApplicationId=[string]$app.Id"
            "} | ConvertTo-Json -Compress"
        )
        completed = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"Codex package discovery failed: {exc}"
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        return None, f"Codex package was not dynamically discovered: {detail}"
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return None, f"Codex package discovery returned invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "Codex package discovery JSON was not an object"
    family = str(payload.get("PackageFamilyName") or "")
    publisher_id = str(payload.get("PublisherId") or "")
    application_id = str(payload.get("ApplicationId") or "")
    expected_family = f"{CODEX_PACKAGE_NAME}_{CODEX_PUBLISHER_ID}"
    if (
        payload.get("Name") != CODEX_PACKAGE_NAME
        or publisher_id != CODEX_PUBLISHER_ID
        or family != expected_family
        or not re.fullmatch(r"[A-Za-z0-9._-]+", application_id)
    ):
        return None, "Discovered Codex package identity did not pass validation"
    return (
        {
            "aumid": f"{family}!{application_id}",
            "package_family_name": family,
            "application_id": application_id,
            "signature_kind": str(payload.get("SignatureKind") or ""),
            "source": "dynamic-package-discovery",
        },
        None,
    )


def codex_process_running(process_name: str = "Codex.exe") -> bool:
    if os.name != "nt":
        return False
    try:
        tasklist = windows_system_executable("tasklist.exe")
        completed = subprocess.run(
            [
                str(tasklist),
                "/FI",
                f"IMAGENAME eq {process_name}",
                "/FO",
                "CSV",
                "/NH",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    rows = list(csv.reader(io.StringIO(completed.stdout)))
    return any(row and row[0].strip().lower() == process_name.lower() for row in rows)


def launch_codex_aumid(aumid: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+![A-Za-z0-9._-]+", aumid):
        raise ValueError(f"Refusing invalid Codex AUMID: {aumid}")
    explorer = windows_system_executable("explorer.exe")
    subprocess.Popen(
        [str(explorer), rf"shell:AppsFolder\{aumid}"],
        cwd=str(explorer.parent),
        close_fds=True,
    )


def launch_pinned_codex_executable(executable: Path) -> None:
    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    subprocess.Popen(
        [str(executable)],
        cwd=str(executable.parent),
        close_fds=True,
        creationflags=creation_flags,
    )


def wait_for_codex_process(timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if codex_process_running():
            return True
        time.sleep(0.25)
    return codex_process_running()


def activate_codex(executable: Path | None, has_due: bool) -> dict[str, Any]:
    base = {
        "limitation": CODEX_WAKE_LIMITATION,
        "outbox_consumer_required": True,
        "opened": False,
    }
    if not has_due:
        return {**base, "mode": "not-needed"}
    if os.name != "nt":
        return {
            **base,
            "mode": "outbox-only",
            "error": "Codex package UI activation is supported only on Windows",
        }
    if codex_process_running():
        return {**base, "mode": "already-running"}

    package, discovery_error = discover_codex_package()
    if package is None:
        package = {
            "aumid": CODEX_FALLBACK_AUMID,
            "package_family_name": f"{CODEX_PACKAGE_NAME}_{CODEX_PUBLISHER_ID}",
            "application_id": "App",
            "signature_kind": "",
            "source": "known-official-aumid-fallback",
        }
    try:
        launch_codex_aumid(package["aumid"])
    except (OSError, ValueError) as package_exc:
        if executable is not None:
            try:
                launch_pinned_codex_executable(executable)
            except OSError as executable_exc:
                return {
                    **base,
                    "mode": "outbox-only",
                    "error": (
                        f"Codex package activation failed: {package_exc}; "
                        f"pinned executable activation failed: {executable_exc}"
                    ),
                }
            if not wait_for_codex_process():
                return {
                    **base,
                    "mode": "outbox-only",
                    "executable": str(executable),
                    "error": (
                        "Pinned Codex executable was started but Codex.exe was "
                        "not observed within the verification window"
                    ),
                }
            return {
                **base,
                "mode": "opened-pinned-ui",
                "opened": True,
                "executable": str(executable),
                "package_activation_error": str(package_exc),
            }
        return {
            **base,
            "mode": "outbox-only",
            "error": f"Could not request Codex package activation: {package_exc}",
        }
    if not wait_for_codex_process():
        return {
            **base,
            "mode": "outbox-only",
            "aumid": package["aumid"],
            "activation_source": package["source"],
            "dynamic_discovery_error": discovery_error,
            "error": (
                "Codex package activation was requested but Codex.exe was not "
                "observed within the verification window"
            ),
        }
    return {
        **base,
        "mode": "package-launch-requested",
        "opened": True,
        "launch_requested": True,
        "aumid": package["aumid"],
        "activation_source": package["source"],
        "dynamic_discovery_error": discovery_error,
    }


def run_watchdog(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = resolve_existing_directory(args.workspace, "workspace")
    skill_dir = resolve_existing_directory(args.skill_dir, "skill directory")
    python_executable = Path(args.python_executable).resolve()
    if not python_executable.is_file():
        raise ValueError(f"Python executable does not exist: {python_executable}")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    codex_executable = validate_codex_executable(
        args.codex_executable,
        args.codex_sha256,
    )

    detected_at = args.now or iso_utc_now()
    preparation_results = []
    for spec in PREPARATIONS:
        preparation_results.append(
            run_scheduler(
                spec,
                skill_dir=skill_dir,
                workspace=workspace,
                python_executable=python_executable,
                timeout_seconds=args.timeout_seconds,
                now=args.now,
            )
        )
    scheduler_results = []
    queued = []
    for spec in SCHEDULERS:
        scheduler_result = run_scheduler(
            spec,
            skill_dir=skill_dir,
            workspace=workspace,
            python_executable=python_executable,
            timeout_seconds=args.timeout_seconds,
            now=args.now,
        )
        scheduler_results.append(scheduler_result)
        if scheduler_result["ok"]:
            for item in scheduler_result["due"]:
                path, created = queue_due_item(workspace, spec, item, detected_at)
                queued.append(
                    {
                        "scheduler": spec.name,
                        "event_path": str(path),
                        "created": created,
                    }
                )

    due_count = sum(
        len(result["due"]) for result in scheduler_results if result["ok"]
    )
    activation = activate_codex(codex_executable, due_count > 0)
    schedulers_ok = all(
        result["ok"] for result in preparation_results + scheduler_results
    )
    activation_ok = "error" not in activation
    report = {
        "schema_version": SCHEMA_VERSION,
        "ok": schedulers_ok and activation_ok,
        "checked_at": detected_at,
        "workspace": str(workspace),
        "skill_dir": str(skill_dir),
        "outbox": str(workspace / OUTBOX_RELATIVE),
        "due_count": due_count,
        "new_event_count": sum(1 for item in queued if item["created"]),
        "queued": queued,
        "preparations": preparation_results,
        "schedulers": scheduler_results,
        "codex_activation": activation,
    }
    with exclusive_lock(workspace / LOCK_RELATIVE):
        atomic_write_json(workspace / STATUS_RELATIVE, report)
    return report, 0 if report["ok"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=str(default_workspace()),
        help=(
            "Workspace containing .codex/soccer-predict state; defaults to the "
            "directory containing the Football-predictions repository"
        ),
    )
    parser.add_argument(
        "--skill-dir",
        default=str(default_skill_dir()),
        help="Local Football-predictions Skill directory",
    )
    parser.add_argument(
        "--python-executable",
        default=sys.executable,
        help="Exact Python executable used for both scheduler commands",
    )
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--now",
        help="Optional timezone-aware ISO time forwarded to both due commands",
    )
    parser.add_argument(
        "--codex-executable",
        help=(
            "Optional absolute Codex.exe path. Requires --codex-sha256 and only "
            "opens the verified UI; it never creates or fabricates a task."
        ),
    )
    parser.add_argument(
        "--codex-sha256",
        help="Required SHA-256 pin when --codex-executable is supplied",
    )
    parser.add_argument(
        "--list-events",
        action="store_true",
        help="List pending outbox events without polling schedulers",
    )
    parser.add_argument(
        "--ack-event",
        help="Acknowledge one exact pending event after a Codex task is created",
    )
    parser.add_argument("--scheduler", help="Scheduler name for --ack-event")
    parser.add_argument("--thread-id", help="Created Codex thread id for --ack-event")
    return parser


def main() -> int:
    configure_stdio()
    args = build_parser().parse_args()
    try:
        workspace = resolve_existing_directory(args.workspace, "workspace")
        if args.list_events:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "workspace": str(workspace),
                        "events": list_pending_events(workspace),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.ack_event:
            if not args.scheduler or not args.thread_id:
                raise ValueError(
                    "--ack-event requires --scheduler and --thread-id"
                )
            result = acknowledge_event(
                workspace,
                scheduler=args.scheduler,
                event_id=args.ack_event,
                thread_id=args.thread_id,
                now=args.now,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        report, returncode = run_watchdog(args)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return returncode
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

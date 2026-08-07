"""Read-only-by-default local environment diagnostics.

The only filesystem mutation is a short-lived write probe that is removed immediately.
No process, scheduled task, registry entry, model, or network resource is changed.
Network access is disabled unless ``--network`` is supplied explicitly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import socket
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Literal
from urllib import parse, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__

Status = Literal["pass", "warn", "fail", "skip"]
DEFAULT_NETWORK_URL = "https://zq.titan007.com/info/index_cn.htm"
MAX_NETWORK_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    summary: str
    details: dict[str, Any]


def _result(
    name: str,
    status: Status,
    summary: str,
    **details: Any,
) -> CheckResult:
    return CheckResult(name=name, status=status, summary=summary, details=details)


def _distribution_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _check_python() -> CheckResult:
    supported = sys.version_info >= (3, 11)
    return _result(
        "python",
        "pass" if supported else "fail",
        f"Python {platform.python_version()} ({platform.system()})",
        executable=sys.executable,
        minimum="3.11",
    )


def _check_package_version(workspace: Path) -> CheckResult:
    distribution_version = _distribution_version("soccer-predict")
    project_path = workspace / "pyproject.toml"
    project_version: str | None = None
    if project_path.is_file():
        try:
            project = tomllib.loads(project_path.read_text(encoding="utf-8")).get(
                "project"
            )
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            return _result(
                "package_version",
                "fail",
                f"Workspace pyproject.toml is unreadable: {exc}",
                package_version=__version__,
                distribution_version=distribution_version,
                project_version=None,
                project_path=str(project_path),
            )
        if not isinstance(project, dict) or not isinstance(project.get("version"), str):
            return _result(
                "package_version",
                "fail",
                "Workspace pyproject.toml is missing project.version",
                package_version=__version__,
                distribution_version=distribution_version,
                project_version=None,
                project_path=str(project_path),
            )
        project_version = project["version"]

    mismatches = {
        source: version
        for source, version in (
            ("distribution", distribution_version),
            ("pyproject", project_version),
        )
        if version is not None and version != __version__
    }
    details = {
        "package_version": __version__,
        "distribution_version": distribution_version,
        "project_version": project_version,
        "project_path": str(project_path) if project_path.is_file() else None,
    }
    if mismatches:
        return _result(
            "package_version",
            "fail",
            "Package release version metadata is inconsistent",
            mismatches=mismatches,
            **details,
        )
    if distribution_version is None:
        return _result(
            "package_version",
            "warn",
            "Distribution metadata is unavailable; source version is internally consistent",
            **details,
        )
    return _result(
        "package_version",
        "pass",
        f"Package and distribution versions agree at {__version__}",
        **details,
    )


def _check_pillow() -> CheckResult:
    version = _distribution_version("Pillow")
    if version is None:
        return _result(
            "pillow",
            "warn",
            "Pillow is not installed; SVG remains available but PNG/JPEG rendering is disabled",
            install_hint="python -m pip install 'soccer-predict[render]'",
        )
    return _result("pillow", "pass", f"Pillow {version} is installed", version=version)


def _font_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    windows_dir = os.environ.get("WINDIR")
    if windows_dir:
        roots.append(Path(windows_dir) / "Fonts")
    roots.extend(
        [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path("/System/Library/Fonts"),
            Path.home() / ".fonts",
        ]
    )
    return tuple(dict.fromkeys(roots))


def _check_chinese_font() -> CheckResult:
    preferred_names = {
        "msyh.ttc",
        "msyhbd.ttc",
        "simhei.ttf",
        "simsun.ttc",
        "notosanscjk-regular.ttc",
        "notosanssc-regular.otf",
        "notoserifcjk-regular.ttc",
        "pingfang.ttc",
    }
    matches: list[str] = []
    checked: list[str] = []
    for root in _font_roots():
        if not root.is_dir():
            continue
        checked.append(str(root))
        try:
            for path in root.rglob("*"):
                if path.is_file() and path.name.casefold() in preferred_names:
                    matches.append(str(path))
                    if len(matches) == 3:
                        break
        except OSError:
            continue
        if len(matches) == 3:
            break
    if not matches:
        return _result(
            "chinese_font",
            "warn",
            "No known Chinese font was found; raster cards may fail or render tofu glyphs",
            searched=checked,
            install_hint="Install Microsoft YaHei/SimHei or Noto Sans CJK",
        )
    return _result(
        "chinese_font",
        "pass",
        f"Chinese font found: {Path(matches[0]).name}",
        matches=matches,
    )


def _check_timezone_data() -> CheckResult:
    zones = ("Asia/Shanghai", "Asia/Tokyo")
    try:
        for zone in zones:
            ZoneInfo(zone)
    except ZoneInfoNotFoundError as exc:
        return _result(
            "timezone_data",
            "fail",
            f"Required IANA timezone is unavailable: {exc}",
            tzdata_version=_distribution_version("tzdata"),
            zones=zones,
        )
    package_version = _distribution_version("tzdata")
    provider = (
        f"tzdata {package_version}" if package_version else "system timezone database"
    )
    return _result(
        "timezone_data",
        "pass",
        f"Required timezones resolve via {provider}",
        tzdata_version=package_version,
        zones=zones,
    )


def _write_probe(directory: Path) -> tuple[bool, str | None]:
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".soccer-predict-doctor-",
            dir=directory,
            delete=False,
        ) as handle:
            probe = Path(handle.name)
            handle.write("write-probe\n")
        probe.unlink()
    except OSError as exc:
        return False, str(exc)
    return True, None


def _check_workspace(workspace: Path) -> CheckResult:
    if not workspace.exists():
        return _result(
            "workspace",
            "fail",
            "Workspace does not exist",
            path=str(workspace),
        )
    if not workspace.is_dir():
        return _result(
            "workspace",
            "fail",
            "Workspace path is not a directory",
            path=str(workspace),
        )
    writable, error = _write_probe(workspace)
    return _result(
        "workspace",
        "pass" if writable else "fail",
        "Workspace write probe passed" if writable else "Workspace is not writable",
        path=str(workspace),
        probe_removed=writable,
        error=error,
    )


def _load_json(path: Path) -> tuple[bool, str | None]:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, str(exc)
    return True, None


def _check_codex_state(workspace: Path) -> CheckResult:
    state_dir = workspace / ".codex" / "soccer-predict"
    if not state_dir.exists():
        return _result(
            "codex_state",
            "warn",
            "Workspace state has not been initialized",
            path=str(state_dir),
        )
    if not state_dir.is_dir():
        return _result(
            "codex_state",
            "fail",
            "Workspace state path is not a directory",
            path=str(state_dir),
        )
    writable, error = _write_probe(state_dir)
    return _result(
        "codex_state",
        "pass" if writable else "fail",
        "Workspace state is readable and writable"
        if writable
        else "Workspace state is not writable",
        path=str(state_dir),
        probe_removed=writable,
        error=error,
    )


def _registry_manager_script(workspace: Path, registry_kind: str) -> Path:
    filenames = {
        "htft": "league_model_manager.py",
        "corner": "corner_model_manager.py",
    }
    try:
        filename = filenames[registry_kind]
    except KeyError as exc:  # Defensive: registry kinds are defined locally below.
        raise ValueError(f"unsupported registry kind: {registry_kind}") from exc
    return workspace / "scripts" / filename


def _semantic_registry_validation(
    workspace: Path, registry_path: Path, registry_kind: str
) -> dict[str, Any]:
    """Run the matching manager's bounded, read-only semantic validation."""

    manager = _registry_manager_script(workspace, registry_kind)
    manager_command = "verify-integrity"
    result: dict[str, Any] = {
        "path": str(registry_path),
        "kind": registry_kind,
        "validator": str(manager),
        "manager_command": manager_command,
    }
    if not manager.is_file():
        result.update(
            {
                "status": "fail",
                "error": (
                    "semantic registry validator is unavailable; run doctor with "
                    "--workspace pointing to a repository checkout that contains "
                    f"scripts/{manager.name}"
                ),
            }
        )
        return result

    command = [
        sys.executable,
        "-B",
        "-X",
        "utf8",
        str(manager),
        manager_command,
        "--model-dir",
        str(registry_path.parent),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        result.update(
            {
                "status": "fail",
                "error": "semantic registry validation timed out after 60 seconds",
            }
        )
        return result
    except OSError as exc:
        result.update(
            {
                "status": "fail",
                "error": f"semantic registry validator could not start: {exc}",
            }
        )
        return result

    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout).strip()
        if len(diagnostic) > 2000:
            diagnostic = diagnostic[-2000:]
        result.update(
            {
                "status": "fail",
                "returncode": completed.returncode,
                "error": diagnostic or "semantic registry validation failed",
            }
        )
        return result
    result["status"] = "pass"
    return result


def _check_registry(workspace: Path) -> CheckResult:
    model_root = workspace / ".codex" / "soccer-predict" / "models"
    active_specs = (
        ("htft", model_root / "league-history-expanded" / "registry.json"),
        (
            "corner",
            model_root / "corner-history-expanded" / "corner-registry.json",
        ),
    )
    # Historical experiments and backups can retain registry-shaped files, but
    # they are not runtime authority. Only the two documented canonical model
    # trees are semantic doctor targets.
    registry_specs = [(kind, path) for kind, path in active_specs if path.is_file()]
    if not registry_specs:
        return _result(
            "model_registry",
            "warn",
            "No model registry is installed in this workspace",
            path=str(model_root),
        )
    missing_active_registries = [
        str(path) for _kind, path in active_specs if not path.is_file()
    ]
    invalid: list[dict[str, str]] = []
    missing_models: list[str] = []
    hash_mismatches: list[str] = []
    semantic_validations: list[dict[str, Any]] = []
    model_count = 0
    for registry_kind, path in registry_specs:
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            payload = None
            error = str(exc)
            invalid.append({"path": str(path), "error": error or "unknown error"})
            continue
        if not isinstance(payload, dict) or not isinstance(
            payload.get("leagues"), list
        ):
            invalid.append(
                {"path": str(path), "error": "registry must contain a leagues list"}
            )
            continue
        for entry in payload["leagues"]:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("model_file"), str
            ):
                invalid.append(
                    {
                        "path": str(path),
                        "error": "registry league is missing model_file",
                    }
                )
                continue
            model_path = (path.parent / entry["model_file"]).resolve()
            if (
                not model_path.is_relative_to(path.parent.resolve())
                or not model_path.is_file()
            ):
                missing_models.append(str(model_path))
                continue
            model_count += 1
            expected_hash = entry.get("model_file_sha256")
            if isinstance(expected_hash, str) and expected_hash.startswith("sha256:"):
                actual_hash = (
                    "sha256:" + hashlib.sha256(model_path.read_bytes()).hexdigest()
                )
                if actual_hash != expected_hash:
                    hash_mismatches.append(str(model_path))
        semantic_validations.append(
            _semantic_registry_validation(workspace, path, registry_kind)
        )
    semantic_failures = [
        validation
        for validation in semantic_validations
        if validation["status"] != "pass"
    ]
    registry_paths = [str(path) for _kind, path in registry_specs]
    if (
        invalid
        or missing_models
        or hash_mismatches
        or semantic_failures
        or missing_active_registries
    ):
        return _result(
            "model_registry",
            "fail",
            "One or more model registries or model files failed validation",
            registries=registry_paths,
            invalid=invalid,
            missing_models=missing_models,
            hash_mismatches=hash_mismatches,
            semantic_validations=semantic_validations,
            semantic_failures=semantic_failures,
            missing_active_registries=missing_active_registries,
        )
    return _result(
        "model_registry",
        "pass",
        f"Validated {len(registry_specs)} registry file(s) and {model_count} model artifact(s)",
        registries=registry_paths,
        model_count=model_count,
        semantic_validations=semantic_validations,
        missing_active_registries=[],
    )


def _check_runtime_files(workspace: Path) -> CheckResult:
    scripts_dir = workspace / "scripts"
    required = (
        "lineup_scheduler.py",
        "review_scheduler.py",
        "soccer_watchdog.py",
    )
    missing = [name for name in required if not (scripts_dir / name).is_file()]
    if (
        platform.system() == "Windows"
        and not (scripts_dir / "install_windows_watchdog.ps1").is_file()
    ):
        missing.append("install_windows_watchdog.ps1")
    state_dir = workspace / ".codex" / "soccer-predict"
    states: dict[str, str] = {}
    invalid_states: dict[str, str] = {}
    for name in ("lineup_tasks.json", "review_tasks.json", "watchdog_status.json"):
        path = state_dir / name
        if not path.exists():
            states[name] = "absent"
            continue
        valid, error = _load_json(path)
        states[name] = "valid" if valid else "invalid"
        if not valid:
            invalid_states[name] = error or "unknown error"
    if missing or invalid_states:
        return _result(
            "scheduler_watchdog",
            "fail",
            "Scheduler/watchdog installation is incomplete or state JSON is invalid",
            missing=missing,
            states=states,
            invalid_states=invalid_states,
        )
    return _result(
        "scheduler_watchdog",
        "pass",
        "Scheduler/watchdog entry points and local state are valid; OS tasks were not inspected or changed",
        scripts=[str(scripts_dir / name) for name in required],
        states=states,
        recurring_watchdog_checked=False,
    )


def _check_network(url: str, timeout: float) -> CheckResult:
    if not math.isfinite(timeout):
        return _result(
            "network",
            "fail",
            "Opt-in connectivity timeout must be a finite number",
            url=url,
            timeout_seconds=timeout,
        )
    timeout = min(max(timeout, 0.1), MAX_NETWORK_TIMEOUT_SECONDS)
    try:
        scheme = parse.urlsplit(url).scheme.casefold()
    except ValueError as exc:
        return _result(
            "network",
            "fail",
            f"Opt-in connectivity URL is invalid: {exc}",
            url=url,
            timeout_seconds=timeout,
        )
    if scheme not in {"http", "https"}:
        return _result(
            "network",
            "fail",
            "Opt-in connectivity URL must use HTTP or HTTPS",
            url=url,
            timeout_seconds=timeout,
        )
    headers = {
        "User-Agent": f"soccer-predict-doctor/{__version__} (+connectivity-check)",
        "Range": "bytes=0-0",
    }
    try:
        with request.urlopen(
            request.Request(url, headers=headers), timeout=timeout
        ) as response:
            status = getattr(response, "status", None)
            response.read(1)
    except (OSError, TimeoutError, ValueError, socket.timeout) as exc:
        return _result(
            "network",
            "fail",
            f"Opt-in connectivity check failed: {exc}",
            url=url,
            timeout_seconds=timeout,
        )
    return _result(
        "network",
        "pass",
        f"Opt-in connectivity check returned HTTP {status}",
        url=url,
        timeout_seconds=timeout,
        http_status=status,
    )


def run_checks(
    workspace: Path,
    *,
    network: bool = False,
    network_url: str = DEFAULT_NETWORK_URL,
    network_timeout: float = 3.0,
) -> list[CheckResult]:
    resolved = workspace.expanduser().resolve()
    checks = [
        _check_python(),
        _check_package_version(resolved),
        _check_pillow(),
        _check_chinese_font(),
        _check_timezone_data(),
        _check_workspace(resolved),
        _check_codex_state(resolved),
        _check_registry(resolved),
        _check_runtime_files(resolved),
    ]
    checks.append(
        _check_network(network_url, network_timeout)
        if network
        else _result(
            "network",
            "skip",
            "Network check disabled (pass --network to opt in)",
        )
    )
    return checks


def add_doctor_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "doctor",
        help="check the local Python, rendering, state, model, and scheduler environment",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="workspace to inspect (default: current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return a failure status for warnings as well as failures",
    )
    parser.add_argument(
        "--network",
        action="store_true",
        help="explicitly opt in to a bounded external connectivity check",
    )
    parser.add_argument(
        "--network-url",
        default=DEFAULT_NETWORK_URL,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--network-timeout",
        type=float,
        default=3.0,
        metavar="SECONDS",
        help=f"network timeout when opted in (0.1-{MAX_NETWORK_TIMEOUT_SECONDS:g}; default: 3)",
    )
    return parser


def _payload(workspace: Path, checks: list[CheckResult]) -> dict[str, Any]:
    counts = {status: 0 for status in ("pass", "warn", "fail", "skip")}
    for check in checks:
        counts[check.status] += 1
    return {
        "schema_version": "soccer-predict-doctor/1.0.0",
        "workspace": str(workspace.expanduser().resolve()),
        "counts": counts,
        "checks": [asdict(check) for check in checks],
    }


def run_doctor_command(args: argparse.Namespace) -> int:
    checks = run_checks(
        args.workspace,
        network=args.network,
        network_url=args.network_url,
        network_timeout=args.network_timeout,
    )
    payload = _payload(args.workspace, checks)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for check in checks:
            print(f"[{check.status.upper():4}] {check.name}: {check.summary}")
        counts = payload["counts"]
        print(
            "Summary: "
            + ", ".join(
                f"{name}={counts[name]}" for name in ("pass", "warn", "fail", "skip")
            )
        )
    if payload["counts"]["fail"]:
        return 1
    if args.strict and payload["counts"]["warn"]:
        return 1
    return 0

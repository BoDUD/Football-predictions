from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from soccer_predict import cli, doctor


class DoctorTests(unittest.TestCase):
    def test_parser_exports_doctor_without_running_checks(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["doctor", "--json"])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.json)
        self.assertFalse(args.network)

    def test_default_checks_never_call_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            scripts = workspace / "scripts"
            scripts.mkdir()
            for name in (
                "lineup_scheduler.py",
                "review_scheduler.py",
                "soccer_watchdog.py",
                "install_windows_watchdog.ps1",
            ):
                (scripts / name).touch()
            with mock.patch.object(
                doctor,
                "_check_network",
                side_effect=AssertionError("network access was not opt-in"),
            ):
                checks = doctor.run_checks(workspace)
        network = next(check for check in checks if check.name == "network")
        self.assertEqual(network.status, "skip")

    def test_write_probe_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            result = doctor._check_workspace(workspace)
            leftovers = list(workspace.glob(".soccer-predict-doctor-*"))
        self.assertEqual(result.status, "pass")
        self.assertEqual(leftovers, [])

    def test_missing_workspace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            result = doctor._check_workspace(missing)
        self.assertEqual(result.status, "fail")

    def test_package_version_check_reports_matching_source_and_distribution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "pyproject.toml").write_text(
                '[project]\nversion = "3.7.0"\n',
                encoding="utf-8",
            )
            with mock.patch.object(
                doctor,
                "_distribution_version",
                return_value=doctor.__version__,
            ):
                result = doctor._check_package_version(workspace)
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.details["package_version"], "3.7.0")
        self.assertEqual(result.details["distribution_version"], "3.7.0")
        self.assertEqual(result.details["project_version"], "3.7.0")

    def test_package_version_check_fails_on_distribution_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                doctor,
                "_distribution_version",
                return_value="0.0.0",
            ):
                result = doctor._check_package_version(Path(temporary))
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.details["package_version"], "3.7.0")
        self.assertEqual(result.details["mismatches"], {"distribution": "0.0.0"})

    def test_registry_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            registry = (
                workspace
                / ".codex"
                / "soccer-predict"
                / "models"
                / "league-history-expanded"
                / "registry.json"
            )
            registry.parent.mkdir(parents=True)
            registry.write_text("{invalid", encoding="utf-8")
            result = doctor._check_registry(workspace)
        self.assertEqual(result.status, "fail")
        self.assertEqual(len(result.details["invalid"]), 1)

    def test_registry_requires_its_model_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            registry = (
                workspace
                / ".codex"
                / "soccer-predict"
                / "models"
                / "league-history-expanded"
                / "registry.json"
            )
            registry.parent.mkdir(parents=True)
            registry.write_text(
                json.dumps({"leagues": [{"model_file": "missing-model.json"}]}),
                encoding="utf-8",
            )
            result = doctor._check_registry(workspace)
        self.assertEqual(result.status, "fail")
        self.assertEqual(len(result.details["missing_models"]), 1)

    def test_registry_validates_model_file_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            scripts = workspace / "scripts"
            scripts.mkdir()
            (scripts / "league_model_manager.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8"
            )
            (scripts / "corner_model_manager.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8"
            )
            model_dir = (
                workspace
                / ".codex"
                / "soccer-predict"
                / "models"
                / "league-history-expanded"
            )
            model_dir.mkdir(parents=True)
            model = model_dir / "model.json"
            model.write_bytes(b"{}")
            digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
            (model_dir / "registry.json").write_text(
                json.dumps(
                    {
                        "leagues": [
                            {
                                "model_file": model.name,
                                "model_file_sha256": digest,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            corner_dir = (
                workspace
                / ".codex"
                / "soccer-predict"
                / "models"
                / "corner-history-expanded"
            )
            corner_dir.mkdir(parents=True)
            corner_model = corner_dir / "corner-model.json"
            corner_model.write_bytes(b"{}")
            (corner_dir / "corner-registry.json").write_text(
                json.dumps(
                    {
                        "leagues": [
                            {
                                "model_file": corner_model.name,
                                "model_file_sha256": digest,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = doctor._check_registry(workspace)
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.details["model_count"], 2)

    def test_registry_requires_both_canonical_model_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            scripts = workspace / "scripts"
            scripts.mkdir()
            (scripts / "league_model_manager.py").write_text(
                "raise SystemExit(0)\n", encoding="utf-8"
            )
            model_dir = (
                workspace
                / ".codex"
                / "soccer-predict"
                / "models"
                / "league-history-expanded"
            )
            model_dir.mkdir(parents=True)
            model = model_dir / "model.json"
            model.write_bytes(b"{}")
            digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
            (model_dir / "registry.json").write_text(
                json.dumps(
                    {
                        "leagues": [
                            {
                                "model_file": model.name,
                                "model_file_sha256": digest,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = doctor._check_registry(workspace)

        self.assertEqual(result.status, "fail")
        self.assertEqual(
            result.details["missing_active_registries"],
            [
                str(
                    workspace
                    / ".codex"
                    / "soccer-predict"
                    / "models"
                    / "corner-history-expanded"
                    / "corner-registry.json"
                )
            ],
        )

    def test_registry_discovers_htft_and_corner_and_calls_matching_managers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            scripts = workspace / "scripts"
            scripts.mkdir()
            for name in ("league_model_manager.py", "corner_model_manager.py"):
                (scripts / name).touch()
            model_root = workspace / ".codex" / "soccer-predict" / "models"
            for directory, registry_name in (
                ("league-history-expanded", "registry.json"),
                ("corner-history-expanded", "corner-registry.json"),
            ):
                model_dir = model_root / directory
                model_dir.mkdir(parents=True)
                model = model_dir / "model.json"
                model.write_bytes(b"{}")
                digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
                (model_dir / registry_name).write_text(
                    json.dumps(
                        {
                            "leagues": [
                                {
                                    "model_file": model.name,
                                    "model_file_sha256": digest,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
            historical = model_root / "league-history-expanded-pre-3.5.0"
            historical.mkdir()
            (historical / "registry.json").write_text(
                "{intentionally-invalid-historical-registry",
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
            with mock.patch.object(
                doctor.subprocess, "run", return_value=completed
            ) as run:
                result = doctor._check_registry(workspace)

        self.assertEqual(result.status, "pass")
        self.assertEqual(len(result.details["semantic_validations"]), 2)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(
            any(
                "league_model_manager.py" in str(part)
                for command in commands
                for part in command
            )
        )
        self.assertTrue(
            any(
                "corner_model_manager.py" in str(part)
                for command in commands
                for part in command
            )
        )
        htft_command = next(
            command
            for command in commands
            if any("league_model_manager.py" in str(part) for part in command)
        )
        corner_command = next(
            command
            for command in commands
            if any("corner_model_manager.py" in str(part) for part in command)
        )
        self.assertIn("verify-integrity", htft_command)
        self.assertIn("verify-integrity", corner_command)

    def test_registry_reports_semantic_manager_failure_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            scripts = workspace / "scripts"
            scripts.mkdir()
            (scripts / "corner_model_manager.py").touch()
            model_dir = (
                workspace
                / ".codex"
                / "soccer-predict"
                / "models"
                / "corner-history-expanded"
            )
            model_dir.mkdir(parents=True)
            model = model_dir / "model.json"
            model.write_bytes(b"{}")
            digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
            registry = model_dir / "corner-registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "leagues": [
                            {
                                "model_file": model.name,
                                "model_file_sha256": digest,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            before = registry.read_bytes()
            completed = subprocess.CompletedProcess(
                [], 1, stdout="", stderr="unsupported corner manager version"
            )
            with mock.patch.object(doctor.subprocess, "run", return_value=completed):
                result = doctor._check_registry(workspace)
            after = registry.read_bytes()

        self.assertEqual(result.status, "fail")
        self.assertEqual(before, after)
        failure = result.details["semantic_failures"][0]
        self.assertEqual(failure["kind"], "corner")
        self.assertIn("unsupported corner manager version", failure["error"])

    def test_registry_missing_runtime_manager_is_explicit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            model_dir = (
                workspace
                / ".codex"
                / "soccer-predict"
                / "models"
                / "league-history-expanded"
            )
            model_dir.mkdir(parents=True)
            model = model_dir / "model.json"
            model.write_bytes(b"{}")
            digest = "sha256:" + hashlib.sha256(b"{}").hexdigest()
            (model_dir / "registry.json").write_text(
                json.dumps(
                    {
                        "leagues": [
                            {
                                "model_file": model.name,
                                "model_file_sha256": digest,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = doctor._check_registry(workspace)

        self.assertEqual(result.status, "fail")
        failure = result.details["semantic_failures"][0]
        self.assertEqual(failure["kind"], "htft")
        self.assertIn("semantic registry validator is unavailable", failure["error"])
        self.assertTrue(failure["validator"].endswith("league_model_manager.py"))

    def test_network_timeout_is_bounded_when_explicitly_enabled(self) -> None:
        response = mock.MagicMock()
        response.status = 206
        response.__enter__.return_value = response
        with mock.patch.object(
            doctor.request, "urlopen", return_value=response
        ) as urlopen:
            result = doctor._check_network("https://example.invalid/", 999)
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.details["timeout_seconds"], 10.0)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 10.0)
        network_request = urlopen.call_args.args[0]
        self.assertEqual(
            network_request.get_header("User-agent"),
            f"soccer-predict-doctor/{doctor.__version__} (+connectivity-check)",
        )

    def test_network_check_rejects_local_file_urls(self) -> None:
        with mock.patch.object(
            doctor.request,
            "urlopen",
            side_effect=AssertionError("local URL should not be opened"),
        ):
            result = doctor._check_network("file:///etc/passwd", 1)
        self.assertEqual(result.status, "fail")

    def test_network_check_rejects_non_finite_timeout(self) -> None:
        with mock.patch.object(
            doctor.request,
            "urlopen",
            side_effect=AssertionError("invalid timeout should not reach the network"),
        ):
            result = doctor._check_network("https://example.invalid/", float("nan"))
        self.assertEqual(result.status, "fail")

    @pytest.mark.schema_contract
    def test_json_command_has_stable_schema_and_exit_status(self) -> None:
        checks = [
            doctor.CheckResult("python", "pass", "ok", {}),
            doctor.CheckResult("pillow", "warn", "optional", {}),
            doctor.CheckResult("network", "skip", "disabled", {}),
        ]
        args = argparse.Namespace(
            workspace=Path("."),
            network=False,
            network_url=doctor.DEFAULT_NETWORK_URL,
            network_timeout=3.0,
            json=True,
            strict=False,
        )
        output = io.StringIO()
        with (
            mock.patch.object(doctor, "run_checks", return_value=checks),
            redirect_stdout(output),
        ):
            status = doctor.run_doctor_command(args)
        payload = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(payload["schema_version"], "soccer-predict-doctor/1.0.0")
        self.assertEqual(
            payload["counts"], {"fail": 0, "pass": 1, "skip": 1, "warn": 1}
        )

    def test_strict_mode_fails_on_warning(self) -> None:
        args = argparse.Namespace(
            workspace=Path("."),
            network=False,
            network_url=doctor.DEFAULT_NETWORK_URL,
            network_timeout=3.0,
            json=False,
            strict=True,
        )
        with (
            mock.patch.object(
                doctor,
                "run_checks",
                return_value=[doctor.CheckResult("pillow", "warn", "optional", {})],
            ),
            redirect_stdout(io.StringIO()),
        ):
            status = doctor.run_doctor_command(args)
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()

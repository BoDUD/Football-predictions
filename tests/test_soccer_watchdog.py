import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "soccer_watchdog.py"
REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("soccer_watchdog", SCRIPT)
soccer_watchdog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = soccer_watchdog
SPEC.loader.exec_module(soccer_watchdog)


FAKE_SCHEDULER = """\
import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument("--base-dir", required=True)
sub = parser.add_subparsers(dest="command", required=True)
due = sub.add_parser("due")
due.add_argument("--now")
cleanup = sub.add_parser("cleanup-due")
cleanup.add_argument("--now")
sync = sub.add_parser("sync-pending")
sync.add_argument("--now")
args = parser.parse_args()
if args.command == "due":
    due_items = DUE_ITEMS
elif args.command == "cleanup-due":
    due_items = CLEANUP_ITEMS
else:
    due_items = None
payload = {"ok": True, "checked_at": args.now or "2026-07-24T00:00:00+00:00"}
if due_items is not None:
    payload["due"] = due_items
else:
    payload["registered"] = []
print(json.dumps(payload))
"""


class SoccerWatchdogTests(unittest.TestCase):
    def make_layout(
        self,
        lineup_due=None,
        review_due=None,
        lineup_cleanup=None,
        review_cleanup=None,
    ):
        temp = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        root = Path(temp.name)
        workspace = root / "workspace"
        skill_dir = root / "skill"
        scripts = skill_dir / "scripts"
        workspace.mkdir()
        scripts.mkdir(parents=True)
        for filename, items, cleanup_items in (
            ("lineup_scheduler.py", lineup_due or [], lineup_cleanup or []),
            ("review_scheduler.py", review_due or [], review_cleanup or []),
        ):
            body = FAKE_SCHEDULER.replace(
                "DUE_ITEMS",
                repr(items),
            ).replace(
                "CLEANUP_ITEMS",
                repr(cleanup_items),
            )
            (scripts / filename).write_text(body, encoding="utf-8")
        return temp, workspace, skill_dir

    def args(self, workspace, skill_dir, **overrides):
        values = {
            "workspace": str(workspace),
            "skill_dir": str(skill_dir),
            "python_executable": sys.executable,
            "timeout_seconds": 10.0,
            "now": "2026-07-24T00:00:00+00:00",
            "codex_executable": None,
            "codex_sha256": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_runs_both_schedulers_and_persists_verbatim_due_items(self):
        lineup = {
            "match_id": "42",
            "scheduled_for": "2026-07-24T09:30:00+09:00",
            "primary_pick": "home -0.25",
        }
        review = {
            "match_id": "43",
            "review_due_at": "2026-07-24T00:00:00+00:00",
            "final_score": "2-1",
        }
        temp, workspace, skill_dir = self.make_layout([lineup], [review])
        self.addCleanup(temp.cleanup)

        report, returncode = soccer_watchdog.run_watchdog(
            self.args(workspace, skill_dir)
        )

        self.assertEqual(returncode, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["due_count"], 2)
        self.assertEqual(report["new_event_count"], 2)
        self.assertEqual(
            [item["scheduler"] for item in report["schedulers"]],
            ["lineup", "review", "lineup-cleanup", "review-cleanup"],
        )
        self.assertEqual(
            [item["scheduler"] for item in report["preparations"]],
            ["lineup-sync", "review-sync"],
        )
        event_files = sorted(
            (workspace / soccer_watchdog.OUTBOX_RELATIVE).glob("*.json")
        )
        self.assertEqual(len(event_files), 2)
        events = [
            json.loads(path.read_text(encoding="utf-8")) for path in event_files
        ]
        self.assertEqual(
            {event["scheduler"]: event["due"] for event in events},
            {"lineup": lineup, "review": review},
        )
        self.assertTrue(
            all(event["analysis_state"] == "not_started" for event in events)
        )
        self.assertTrue(
            all("not an analysis" in event["consumer_contract"] for event in events)
        )

    def test_repeated_poll_deduplicates_the_same_pending_events(self):
        item = {
            "match_id": "99",
            "scheduled_for": "2026-07-24T09:30:00+09:00",
        }
        temp, workspace, skill_dir = self.make_layout([item], [])
        self.addCleanup(temp.cleanup)

        first, first_code = soccer_watchdog.run_watchdog(
            self.args(workspace, skill_dir)
        )
        second, second_code = soccer_watchdog.run_watchdog(
            self.args(workspace, skill_dir)
        )

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first["new_event_count"], 1)
        self.assertEqual(second["new_event_count"], 0)
        self.assertEqual(
            len(list((workspace / soccer_watchdog.OUTBOX_RELATIVE).glob("*.json"))),
            1,
        )

    def test_existing_pending_cleanup_event_refreshes_dynamic_payload(self):
        waiting = {
            "match_id": "42",
            "thread_id": "thread-42",
            "next_action": "await_complete_metadata",
            "delivery_status": "pending",
        }
        ready = {
            "match_id": "42",
            "thread_id": "thread-42",
            "next_action": "verify_delivery",
            "delivery_status": "pending",
            "result_artifact": "result-42.json",
        }
        temp, workspace, skill_dir = self.make_layout(lineup_cleanup=[waiting])
        self.addCleanup(temp.cleanup)

        first, first_code = soccer_watchdog.run_watchdog(
            self.args(workspace, skill_dir)
        )
        self.assertEqual(first_code, 0)
        event_path = Path(first["queued"][0]["event_path"])
        original = json.loads(event_path.read_text(encoding="utf-8"))
        original["dispatcher_note"] = "preserve this audit field"
        soccer_watchdog.atomic_write_json(event_path, original)

        body = FAKE_SCHEDULER.replace("DUE_ITEMS", repr([])).replace(
            "CLEANUP_ITEMS", repr([ready])
        )
        (skill_dir / "scripts" / "lineup_scheduler.py").write_text(
            body, encoding="utf-8"
        )
        second, second_code = soccer_watchdog.run_watchdog(
            self.args(
                workspace,
                skill_dir,
                now="2026-07-24T00:05:00+00:00",
            )
        )

        self.assertEqual(second_code, 0)
        self.assertEqual(second["new_event_count"], 0)
        refreshed = json.loads(event_path.read_text(encoding="utf-8"))
        self.assertEqual(refreshed["due"], ready)
        self.assertEqual(refreshed["event_type"], "lineup-result-cleanup-due")
        self.assertEqual(refreshed["detected_at"], "2026-07-24T00:05:00+00:00")
        self.assertEqual(refreshed["delivery_state"], "pending")
        self.assertEqual(refreshed["event_id"], original["event_id"])
        self.assertEqual(
            refreshed["dispatcher_note"], "preserve this audit field"
        )

    def test_missing_review_scheduler_does_not_discard_lineup_due(self):
        item = {
            "match_id": "100",
            "scheduled_for": "2026-07-24T09:30:00+09:00",
        }
        temp, workspace, skill_dir = self.make_layout([item], [])
        self.addCleanup(temp.cleanup)
        (skill_dir / "scripts" / "review_scheduler.py").unlink()

        report, returncode = soccer_watchdog.run_watchdog(
            self.args(workspace, skill_dir)
        )

        self.assertEqual(returncode, 2)
        self.assertFalse(report["ok"])
        self.assertTrue(report["schedulers"][0]["ok"])
        review_results = [
            item
            for item in report["schedulers"]
            if item["scheduler"].startswith("review")
        ]
        self.assertTrue(all(not item["ok"] for item in review_results))
        self.assertIn("missing", review_results[0]["error"])
        self.assertEqual(report["new_event_count"], 1)
        status = json.loads(
            (workspace / soccer_watchdog.STATUS_RELATIVE).read_text(encoding="utf-8")
        )
        self.assertFalse(status["ok"])

    def test_codex_ui_requires_an_exact_executable_hash_pin(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp:
            executable = Path(temp) / "Codex.exe"
            executable.write_bytes(b"pinned test executable")
            expected = hashlib.sha256(executable.read_bytes()).hexdigest()

            resolved = soccer_watchdog.validate_codex_executable(
                str(executable),
                expected,
            )
            self.assertEqual(resolved, executable.resolve())
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                soccer_watchdog.validate_codex_executable(
                    str(executable),
                    "0" * 64,
                )
            with self.assertRaisesRegex(ValueError, "supplied together"):
                soccer_watchdog.validate_codex_executable(str(executable), None)

    def test_no_codex_cli_or_ui_is_invoked_in_default_outbox_mode(self):
        with (
            mock.patch.object(soccer_watchdog, "codex_process_running", return_value=False),
            mock.patch.object(
                soccer_watchdog,
                "discover_codex_package",
                return_value=(
                    {
                        "aumid": soccer_watchdog.CODEX_FALLBACK_AUMID,
                        "source": "dynamic-package-discovery",
                    },
                    None,
                ),
            ),
            mock.patch.object(soccer_watchdog, "launch_codex_aumid") as launch,
            mock.patch.object(
                soccer_watchdog,
                "wait_for_codex_process",
                return_value=True,
            ),
        ):
            result = soccer_watchdog.activate_codex(None, has_due=True)

        launch.assert_called_once_with(soccer_watchdog.CODEX_FALLBACK_AUMID)
        self.assertEqual(result["mode"], "package-launch-requested")
        self.assertTrue(result["outbox_consumer_required"])
        self.assertIn("recurring Codex dispatcher", result["limitation"])

    def test_dynamic_codex_package_is_preferred_over_known_aumid_fallback(self):
        discovered = {
            "aumid": "OpenAI.Codex_2p2nqsd0c76g0!DifferentManifestId",
            "source": "dynamic-package-discovery",
        }
        with (
            mock.patch.object(soccer_watchdog, "codex_process_running", return_value=False),
            mock.patch.object(
                soccer_watchdog,
                "discover_codex_package",
                return_value=(discovered, None),
            ),
            mock.patch.object(soccer_watchdog, "launch_codex_aumid") as launch,
            mock.patch.object(
                soccer_watchdog,
                "wait_for_codex_process",
                return_value=True,
            ),
        ):
            result = soccer_watchdog.activate_codex(None, has_due=True)

        launch.assert_called_once_with(discovered["aumid"])
        self.assertEqual(result["activation_source"], "dynamic-package-discovery")

    def test_review_follow_up_attempts_receive_distinct_event_ids(self):
        first = {
            "match_id": "88",
            "kickoff": "2026-07-24T09:00:00+09:00",
            "attempt": {
                "attempt_id": "review-1",
                "run_at_utc": "2026-07-24T03:00:00+00:00",
            },
        }
        second = {
            "match_id": "88",
            "kickoff": "2026-07-24T09:00:00+09:00",
            "attempt": {
                "attempt_id": "review-2",
                "run_at_utc": "2026-07-24T03:30:00+00:00",
            },
        }
        spec = next(
            item for item in soccer_watchdog.SCHEDULERS if item.name == "review"
        )

        self.assertNotEqual(
            soccer_watchdog.event_identity(spec, first),
            soccer_watchdog.event_identity(spec, second),
        )

    def test_cleanup_events_are_queued_and_acknowledged_exactly(self):
        cleanup = {
            "match_id": "42",
            "thread_id": "thread-42",
            "next_action": "verify_delivery",
            "delivery_status": "pending",
        }
        temp, workspace, skill_dir = self.make_layout(
            lineup_cleanup=[cleanup]
        )
        self.addCleanup(temp.cleanup)

        report, returncode = soccer_watchdog.run_watchdog(
            self.args(workspace, skill_dir)
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(report["due_count"], 1)
        events = soccer_watchdog.list_pending_events(workspace)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["scheduler"], "lineup-cleanup")

        acknowledged = soccer_watchdog.acknowledge_event(
            workspace,
            scheduler="lineup-cleanup",
            event_id=events[0]["event_id"],
            thread_id="dispatcher-thread-1",
            now="2026-07-24T00:00:00+00:00",
        )
        self.assertTrue(Path(acknowledged["processed_path"]).is_file())
        self.assertEqual(soccer_watchdog.list_pending_events(workspace), [])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            soccer_watchdog.acknowledge_event(
                workspace,
                scheduler="lineup-cleanup",
                event_id=events[0]["event_id"],
                thread_id="dispatcher-thread-2",
            )

    def test_acknowledged_payload_has_finite_dispatch_cooldown(self):
        cleanup = {
            "match_id": "42",
            "thread_id": "thread-42",
            "next_action": "await_complete_metadata",
            "delivery_status": "pending",
        }
        temp, workspace, skill_dir = self.make_layout(
            lineup_cleanup=[cleanup]
        )
        self.addCleanup(temp.cleanup)
        first, first_code = soccer_watchdog.run_watchdog(
            self.args(workspace, skill_dir)
        )
        self.assertEqual(first_code, 0)
        event = soccer_watchdog.list_pending_events(workspace)[0]
        soccer_watchdog.acknowledge_event(
            workspace,
            scheduler="lineup-cleanup",
            event_id=event["event_id"],
            thread_id="dispatcher-thread-1",
            now="2026-07-24T00:00:00+00:00",
        )

        cooldown, cooldown_code = soccer_watchdog.run_watchdog(
            self.args(
                workspace,
                skill_dir,
                now="2026-07-24T00:09:59+00:00",
            )
        )
        self.assertEqual(cooldown_code, 0)
        self.assertEqual(cooldown["new_event_count"], 0)
        self.assertEqual(soccer_watchdog.list_pending_events(workspace), [])

        expired, expired_code = soccer_watchdog.run_watchdog(
            self.args(
                workspace,
                skill_dir,
                now="2026-07-24T00:10:00+00:00",
            )
        )
        self.assertEqual(expired_code, 0)
        self.assertEqual(expired["new_event_count"], 1)
        self.assertEqual(len(soccer_watchdog.list_pending_events(workspace)), 1)

    def test_changed_payload_bypasses_dispatch_cooldown(self):
        waiting = {
            "match_id": "42",
            "thread_id": "thread-42",
            "next_action": "await_complete_metadata",
            "delivery_status": "pending",
        }
        ready = {
            **waiting,
            "next_action": "verify_delivery",
            "result_artifact": "result-42.json",
        }
        temp, workspace, skill_dir = self.make_layout(
            lineup_cleanup=[waiting]
        )
        self.addCleanup(temp.cleanup)
        soccer_watchdog.run_watchdog(self.args(workspace, skill_dir))
        event = soccer_watchdog.list_pending_events(workspace)[0]
        soccer_watchdog.acknowledge_event(
            workspace,
            scheduler="lineup-cleanup",
            event_id=event["event_id"],
            thread_id="dispatcher-thread-1",
            now="2026-07-24T00:00:00+00:00",
        )
        body = FAKE_SCHEDULER.replace("DUE_ITEMS", repr([])).replace(
            "CLEANUP_ITEMS", repr([ready])
        )
        (skill_dir / "scripts" / "lineup_scheduler.py").write_text(
            body, encoding="utf-8"
        )

        report, returncode = soccer_watchdog.run_watchdog(
            self.args(
                workspace,
                skill_dir,
                now="2026-07-24T00:01:00+00:00",
            )
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(report["new_event_count"], 1)
        pending = soccer_watchdog.list_pending_events(workspace)
        self.assertEqual(pending[0]["due"], ready)
        self.assertEqual(pending[0]["event_id"], event["event_id"])

    def test_windows_installer_requires_workspace_and_has_ten_minute_limit(self):
        installer = (
            REPO_ROOT / "scripts" / "install_windows_watchdog.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'throw "install requires an explicit -Workspace path"',
            installer,
        )
        self.assertNotIn("$Workspace = Split-Path -Parent $skillRoot", installer)
        self.assertIn(
            "-ExecutionTimeLimit (New-TimeSpan -Minutes 10)",
            installer,
        )


if __name__ == "__main__":
    unittest.main()

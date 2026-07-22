from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lineup_scheduler.py"
SPEC = importlib.util.spec_from_file_location("soccer_lineup_scheduler", SCRIPT)
assert SPEC and SPEC.loader
lineup_scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lineup_scheduler)


def write_history(base: str, *, lineup_rechecked_at=None) -> None:
    path = lineup_scheduler.history_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "match_id": "42",
                    "mode": "prematch",
                    "status": "pending",
                    "kickoff": "2026-07-22T19:30:00+09:00",
                    "home_team": "主队",
                    "away_team": "客队",
                    "lineup_rechecked_at": lineup_rechecked_at,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def register_args(base: str):
    return SimpleNamespace(
        base_dir=base,
        match_id="42",
        kickoff=None,
        source_timezone="Asia/Shanghai",
        user_timezone="Asia/Tokyo",
        home_team=None,
        away_team=None,
    )


class LineupSchedulerTests(unittest.TestCase):
    def test_register_persists_tokyo_schedule_and_bounded_retries(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            result = lineup_scheduler.cmd_register(register_args(base))
            task = result["task"]
            self.assertEqual(task["user_timezone"], "Asia/Tokyo")
            self.assertEqual(task["source_timezone"], "Asia/Shanghai")
            self.assertEqual(task["source_kickoff"], "2026-07-22T18:30:00+08:00")
            self.assertEqual(task["kickoff"], "2026-07-22T19:30:00+09:00")
            self.assertEqual(task["scheduled_for"], "2026-07-22T19:00:00+09:00")
            self.assertEqual(task["retry_plan"][0]["run_at_utc"], "2026-07-22T10:00:00+00:00")
            self.assertEqual(task["retry_plan"][0]["automation_timezone"], "UTC")
            self.assertEqual(
                task["retry_plan"][0]["automation_rrule"],
                "RRULE:FREQ=DAILY;BYHOUR=10;BYMINUTE=0;COUNT=1",
            )
            self.assertEqual(
                [item["minutes_before_kickoff"] for item in task["retry_plan"]],
                [30, 25, 20, 15, 10, 5, 2],
            )
            duplicate = lineup_scheduler.cmd_register(register_args(base))
            self.assertTrue(duplicate["duplicate_ignored"])

    def test_automation_plan_uses_utc_and_excludes_missed_attempts(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            before = lineup_scheduler.cmd_automation_plan(
                SimpleNamespace(base_dir=base, match_id="42", now="2026-07-22T18:50:00+09:00")
            )
            self.assertFalse(before["catch_up_required"])
            self.assertEqual(len(before["future_attempts"]), 7)
            self.assertEqual(before["rrule_timezone"], "UTC")
            self.assertEqual(before["future_attempts"][0]["run_at"], "2026-07-22T19:00:00+09:00")
            self.assertEqual(before["future_attempts"][0]["run_at_utc"], "2026-07-22T10:00:00+00:00")

            catch_up = lineup_scheduler.cmd_automation_plan(
                SimpleNamespace(base_dir=base, match_id="42", now="2026-07-22T19:12:00+09:00")
            )
            self.assertTrue(catch_up["catch_up_required"])
            self.assertEqual(
                [item["minutes_before_kickoff"] for item in catch_up["future_attempts"]],
                [15, 10, 5, 2],
            )

    def test_automation_plan_handles_tokyo_midnight_without_date_slip(self):
        with tempfile.TemporaryDirectory() as base:
            args = register_args(base)
            args.kickoff = "2026-07-23T00:20:00+09:00"
            args.home_team = "Home"
            args.away_team = "Away"
            result = lineup_scheduler.cmd_register(args)
            first = result["task"]["retry_plan"][0]
            self.assertEqual(first["run_at"], "2026-07-22T23:50:00+09:00")
            self.assertEqual(first["run_at_utc"], "2026-07-22T14:50:00+00:00")
            self.assertEqual(
                first["automation_rrule"],
                "RRULE:FREQ=DAILY;BYHOUR=14;BYMINUTE=50;COUNT=1",
            )

    def test_duplicate_registration_backfills_old_retry_plan(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            path = lineup_scheduler.state_path(base)
            state = json.loads(path.read_text(encoding="utf-8"))
            for item in state["tasks"]["42"]["retry_plan"]:
                item.pop("run_at_utc", None)
                item.pop("automation_timezone", None)
                item.pop("automation_rrule", None)
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

            duplicate = lineup_scheduler.cmd_register(register_args(base))
            self.assertTrue(duplicate["duplicate_ignored"])
            self.assertEqual(
                duplicate["task"]["retry_plan"][0]["automation_rrule"],
                "RRULE:FREQ=DAILY;BYHOUR=10;BYMINUTE=0;COUNT=1",
            )

    def test_attach_rejects_wrong_timezone_rule_and_persists_verified_schedule(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            common = {
                "base_dir": base,
                "match_id": "42",
                "automation_id": "auto-main",
                "automation_name": "Soccer Predict lineup 42",
                "attempt_label": "T-30",
            }
            with self.assertRaisesRegex(ValueError, "expected UTC rule"):
                lineup_scheduler.cmd_attach_automation(
                    SimpleNamespace(
                        **common,
                        automation_rrule="RRULE:FREQ=DAILY;BYHOUR=19;BYMINUTE=0;COUNT=1",
                    )
                )

            attached = lineup_scheduler.cmd_attach_automation(
                SimpleNamespace(
                    **common,
                    automation_rrule="RRULE:FREQ=DAILY;BYHOUR=10;BYMINUTE=0;COUNT=1",
                )
            )
            ref = attached["task"]["automation_refs"][0]
            self.assertTrue(ref["schedule_verified"])
            self.assertEqual(ref["run_at"], "2026-07-22T19:00:00+09:00")
            self.assertEqual(ref["run_at_utc"], "2026-07-22T10:00:00+00:00")

    def test_claim_enforces_t30_lease_release_and_catch_up(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            early = lineup_scheduler.cmd_claim(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T18:59:00+09:00",
                    lease_minutes=4,
                )
            )
            self.assertFalse(early["claimed"])
            self.assertEqual(early["reason"], "too_early")

            claimed = lineup_scheduler.cmd_claim(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T19:00:00+09:00",
                    lease_minutes=4,
                )
            )
            self.assertTrue(claimed["claimed"])
            self.assertFalse(claimed["catch_up"])

            duplicate = lineup_scheduler.cmd_claim(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T19:01:00+09:00",
                    lease_minutes=4,
                )
            )
            self.assertFalse(duplicate["claimed"])
            self.assertEqual(duplicate["reason"], "active_lease")

            released = lineup_scheduler.cmd_release(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T19:02:00+09:00",
                    reason="browser disconnected",
                )
            )
            self.assertTrue(released["released"])
            catch_up = lineup_scheduler.cmd_claim(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T19:05:00+09:00",
                    lease_minutes=4,
                )
            )
            self.assertTrue(catch_up["claimed"])
            self.assertTrue(catch_up["catch_up"])
            self.assertEqual(len(catch_up["task"]["attempts"]), 2)

    def test_due_catches_up_before_kickoff_and_expires_afterward(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            before = lineup_scheduler.cmd_due(
                SimpleNamespace(base_dir=base, now="2026-07-22T18:59:59+09:00")
            )
            self.assertEqual(before["due"], [])
            due = lineup_scheduler.cmd_due(
                SimpleNamespace(base_dir=base, now="2026-07-22T19:17:00+09:00")
            )
            self.assertEqual([item["match_id"] for item in due["due"]], ["42"])
            self.assertTrue(due["due"][0]["catch_up"])
            expired = lineup_scheduler.cmd_status(
                SimpleNamespace(base_dir=base, match_id="42", now="2026-07-22T19:30:00+09:00")
            )
            self.assertEqual(expired["task"]["status"], "expired")

    def test_completion_requires_archived_revision_and_tracks_cleanup(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            lineup_scheduler.cmd_attach_automation(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    automation_id="auto-main",
                    automation_name="Soccer Predict 临场复查 42",
                    attempt_label="T-30",
                    automation_rrule="RRULE:FREQ=DAILY;BYHOUR=10;BYMINUTE=0;COUNT=1",
                )
            )
            with self.assertRaisesRegex(ValueError, "before a lineup-check revision"):
                lineup_scheduler.cmd_complete(
                    SimpleNamespace(base_dir=base, match_id="42", thread_id="thread-1", now=None)
                )

            write_history(base, lineup_rechecked_at="2026-07-22T10:01:00+00:00")
            completed = lineup_scheduler.cmd_complete(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-1",
                    now="2026-07-22T19:02:00+09:00",
                )
            )
            self.assertEqual(completed["task"]["status"], "completed")
            self.assertEqual(completed["task"]["thread_id"], "thread-1")
            self.assertEqual(completed["cleanup_automation_refs"][0]["id"], "auto-main")

            cleaned = lineup_scheduler.cmd_mark_cleaned(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    automation_id=["auto-main"],
                    now="2026-07-22T19:03:00+09:00",
                )
            )
            self.assertEqual(cleaned["task"]["cleaned_automation_ids"], ["auto-main"])
            self.assertIsNotNone(cleaned["task"]["cleanup_completed_at"])


if __name__ == "__main__":
    unittest.main()

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


def write_result_artifact(base: str, content: str = "完整临场复查输出") -> str:
    path = Path(base) / "lineup-result.txt"
    path.write_text(content, encoding="utf-8")
    return str(path)


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

    def test_sync_pending_bootstraps_only_future_unchecked_records(self):
        with tempfile.TemporaryDirectory() as base:
            path = lineup_scheduler.history_path(base)
            path.parent.mkdir(parents=True, exist_ok=True)
            records = [
                {
                    "match_id": "41",
                    "mode": "prematch",
                    "status": "pending",
                    "kickoff": "2026-07-24T07:30:00+09:00",
                    "home_team": "A",
                    "away_team": "B",
                },
                {
                    "match_id": "42",
                    "mode": "prematch",
                    "status": "pending",
                    "kickoff": "2026-07-24T08:30:00+09:00",
                    "home_team": "C",
                    "away_team": "D",
                    "lineup_rechecked_at": "2026-07-23T23:00:00+00:00",
                },
                {
                    "match_id": "43",
                    "mode": "prematch",
                    "status": "pending",
                    "kickoff": "2026-07-24T05:30:00+09:00",
                    "home_team": "E",
                    "away_team": "F",
                },
            ]
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            args = SimpleNamespace(
                base_dir=base,
                source_timezone="Asia/Shanghai",
                user_timezone="Asia/Tokyo",
                now="2026-07-24T06:00:00+09:00",
            )
            first = lineup_scheduler.cmd_sync_pending(args)
            self.assertEqual(first["registered"], ["41"])
            self.assertEqual(first["skipped_rechecked"], ["42"])
            self.assertEqual(
                first["skipped_invalid"], [{"match_id": "43", "reason": "kickoff_reached"}]
            )
            second = lineup_scheduler.cmd_sync_pending(args)
            self.assertEqual(second["registered"], [])
            self.assertEqual(second["duplicate_ignored"], ["41"])

    def test_titan_chinese_wall_time_converts_to_tokyo_before_t30(self):
        with tempfile.TemporaryDirectory() as base:
            args = register_args(base)
            args.kickoff = "2026-07-24T06:30:00+08:00"
            args.home_team = "Home"
            args.away_team = "Away"
            task = lineup_scheduler.cmd_register(args)["task"]
            self.assertEqual(task["source_kickoff"], "2026-07-24T06:30:00+08:00")
            self.assertEqual(task["kickoff"], "2026-07-24T07:30:00+09:00")
            self.assertEqual(task["scheduled_for"], "2026-07-24T07:00:00+09:00")
            self.assertEqual(task["retry_plan"][0]["run_at_utc"], "2026-07-23T22:00:00+00:00")
            self.assertEqual(
                task["retry_plan"][0]["automation_rrule"],
                "RRULE:FREQ=DAILY;BYHOUR=22;BYMINUTE=0;COUNT=1",
            )

    def test_reregister_corrects_source_timezone_for_same_instant(self):
        with tempfile.TemporaryDirectory() as base:
            wrong = register_args(base)
            wrong.kickoff = "2026-07-24T07:30:00+09:00"
            wrong.source_timezone = "Asia/Tokyo"
            wrong.home_team = "Home"
            wrong.away_team = "Away"
            first = lineup_scheduler.cmd_register(wrong)
            self.assertEqual(first["task"]["source_kickoff"], "2026-07-24T07:30:00+09:00")

            corrected = register_args(base)
            corrected.kickoff = "2026-07-24T07:30:00+09:00"
            corrected.home_team = "Home"
            corrected.away_team = "Away"
            result = lineup_scheduler.cmd_register(corrected)
            self.assertFalse(result["duplicate_ignored"])
            self.assertEqual(result["task"]["source_timezone"], "Asia/Shanghai")
            self.assertEqual(result["task"]["source_kickoff"], "2026-07-24T06:30:00+08:00")

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
            result_artifact = write_result_artifact(base)
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
            lineup_scheduler.cmd_attach_automation(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    automation_id="auto-retry",
                    automation_name="Soccer Predict 临场复查 42 补跑 T-25",
                    attempt_label="retry-T-25",
                    automation_rrule="RRULE:FREQ=DAILY;BYHOUR=10;BYMINUTE=5;COUNT=1",
                )
            )
            with self.assertRaisesRegex(ValueError, "before a lineup-check revision"):
                lineup_scheduler.cmd_complete(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        thread_id="thread-1",
                        result_artifact=result_artifact,
                        now=None,
                    )
                )

            write_history(base, lineup_rechecked_at="2026-07-22T10:01:00+00:00")
            completed = lineup_scheduler.cmd_complete(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-1",
                    result_artifact=result_artifact,
                    now="2026-07-22T19:02:00+09:00",
                )
            )
            self.assertEqual(completed["task"]["status"], "completed")
            self.assertEqual(completed["task"]["thread_id"], "thread-1")
            self.assertEqual(completed["task"]["delivery_status"], "pending")
            self.assertEqual(
                completed["task"]["result_delivery"]["delivery_status"], "pending"
            )
            self.assertEqual(
                completed["task"]["result_delivery"]["thread_id"], "thread-1"
            )
            self.assertEqual(
                completed["task"]["result_delivery"]["result_artifact"],
                str(Path(result_artifact).resolve()),
            )
            self.assertIsNone(completed["task"]["result_delivery"]["delivered_at"])
            self.assertEqual(completed["cleanup_automation_refs"][0]["id"], "auto-main")

            delivery_due = lineup_scheduler.cmd_cleanup_due(
                SimpleNamespace(
                    base_dir=base,
                    match_id=None,
                    now="2026-07-22T19:02:30+09:00",
                )
            )
            self.assertEqual(len(delivery_due["due"]), 1)
            self.assertEqual(delivery_due["due"][0]["next_action"], "verify_delivery")
            self.assertTrue(delivery_due["due"][0]["delivery_pending"])

            with self.assertRaisesRegex(ValueError, "before the lineup result is marked delivered"):
                lineup_scheduler.cmd_mark_cleaned(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        automation_id=["auto-main", "auto-retry"],
                        now="2026-07-22T19:03:00+09:00",
                    )
                )

            delivered = lineup_scheduler.cmd_mark_delivered(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-1",
                    now="2026-07-22T19:03:00+09:00",
                )
            )
            self.assertEqual(delivered["task"]["delivery_status"], "delivered")
            self.assertEqual(
                delivered["task"]["result_delivery"]["delivery_status"], "delivered"
            )
            self.assertEqual(
                delivered["task"]["result_delivery"]["delivered_at"],
                "2026-07-22T10:03:00+00:00",
            )

            cleanup_due = lineup_scheduler.cmd_cleanup_due(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T19:03:30+09:00",
                )
            )
            self.assertEqual(len(cleanup_due["due"]), 1)
            self.assertEqual(
                cleanup_due["due"][0]["next_action"], "cleanup_automations"
            )
            self.assertFalse(cleanup_due["due"][0]["delivery_pending"])
            self.assertTrue(cleanup_due["due"][0]["cleanup_pending"])

            with self.assertRaisesRegex(ValueError, "still require cleanup: auto-retry"):
                lineup_scheduler.cmd_mark_cleaned(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        automation_id=["auto-main"],
                        now="2026-07-22T19:03:45+09:00",
                    )
                )
            with self.assertRaisesRegex(ValueError, "Unknown automation id"):
                lineup_scheduler.cmd_mark_cleaned(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        automation_id=["auto-main", "auto-retry", "auto-unknown"],
                        now="2026-07-22T19:03:50+09:00",
                    )
                )

            cleaned = lineup_scheduler.cmd_mark_cleaned(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    automation_id=["auto-main", "auto-retry"],
                    now="2026-07-22T19:04:00+09:00",
                )
            )
            self.assertEqual(
                cleaned["task"]["cleaned_automation_ids"],
                ["auto-main", "auto-retry"],
            )
            self.assertIsNotNone(cleaned["task"]["cleanup_completed_at"])
            nothing_due = lineup_scheduler.cmd_cleanup_due(
                SimpleNamespace(
                    base_dir=base,
                    match_id=None,
                    now="2026-07-22T19:05:00+09:00",
                )
            )
            self.assertEqual(nothing_due["due"], [])

    def test_complete_requires_non_empty_thread_id(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base, lineup_rechecked_at="2026-07-22T10:01:00+00:00")
            result_artifact = write_result_artifact(base)
            lineup_scheduler.cmd_register(register_args(base))
            for thread_id in (None, "", "   "):
                with self.subTest(thread_id=thread_id):
                    with self.assertRaisesRegex(ValueError, "non-empty --thread-id"):
                        lineup_scheduler.cmd_complete(
                            SimpleNamespace(
                                base_dir=base,
                                match_id="42",
                                thread_id=thread_id,
                                result_artifact=result_artifact,
                                now="2026-07-22T19:02:00+09:00",
                            )
                        )

    def test_complete_requires_existing_non_empty_result_artifact(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base, lineup_rechecked_at="2026-07-22T10:01:00+00:00")
            lineup_scheduler.cmd_register(register_args(base))
            with self.assertRaisesRegex(ValueError, "without --result-artifact"):
                lineup_scheduler.cmd_complete(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        thread_id="thread-1",
                        result_artifact=None,
                        now="2026-07-22T19:02:00+09:00",
                    )
                )
            with self.assertRaisesRegex(ValueError, "does not exist"):
                lineup_scheduler.cmd_complete(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        thread_id="thread-1",
                        result_artifact="missing-result.txt",
                        now="2026-07-22T19:02:00+09:00",
                    )
                )
            empty_artifact = write_result_artifact(base, "")
            with self.assertRaisesRegex(ValueError, "is empty"):
                lineup_scheduler.cmd_complete(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        thread_id="thread-1",
                        result_artifact=empty_artifact,
                        now="2026-07-22T19:02:00+09:00",
                    )
                )

    def test_cleanup_recovery_waits_for_metadata_grace_but_complete_tuple_is_immediate(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            write_history(base, lineup_rechecked_at="2026-07-22T10:01:00+00:00")

            within_grace = lineup_scheduler.cmd_cleanup_due(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T10:10:59+00:00",
                )
            )
            self.assertEqual(within_grace["due"], [])

            after_grace = lineup_scheduler.cmd_cleanup_due(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T10:11:01+00:00",
                )
            )
            self.assertEqual(len(after_grace["due"]), 1)
            self.assertEqual(
                after_grace["due"][0]["next_action"], "await_complete_metadata"
            )

        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            artifact = write_result_artifact(base)
            lineup_scheduler.cmd_terminal(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    reason="started",
                    thread_id="thread-terminal-42",
                    result_artifact=artifact,
                    now="2026-07-22T10:02:00+00:00",
                )
            )
            immediate = lineup_scheduler.cmd_cleanup_due(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T10:02:01+00:00",
                )
            )
            self.assertEqual(len(immediate["due"]), 1)
            self.assertEqual(immediate["due"][0]["next_action"], "verify_delivery")

    def test_completed_task_cannot_be_overwritten_by_retry_terminal(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base, lineup_rechecked_at="2026-07-22T10:01:00+00:00")
            result_artifact = write_result_artifact(base)
            lineup_scheduler.cmd_register(register_args(base))
            lineup_scheduler.cmd_complete(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-1",
                    result_artifact=result_artifact,
                    now="2026-07-22T19:02:00+09:00",
                )
            )

            with self.assertRaisesRegex(ValueError, "result tuple"):
                lineup_scheduler.cmd_terminal(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        reason="started",
                        thread_id="thread-1",
                        result_artifact=result_artifact,
                        now="2026-07-22T19:30:00+09:00",
                    )
                )
            status = lineup_scheduler.cmd_status(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T19:30:00+09:00",
                )
            )["task"]
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["terminal_reason"], "lineup_revision_archived")
            self.assertEqual(status["result_delivery"]["delivery_status"], "pending")

    def test_pending_result_tuple_only_allows_exact_idempotent_replay(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            write_history(base, lineup_rechecked_at="2026-07-22T10:01:00+00:00")
            synced = lineup_scheduler.cmd_status(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T19:02:00+09:00",
                )
            )["task"]
            self.assertEqual(synced["delivery_status"], "pending")
            self.assertIsNone(synced["thread_id"])
            self.assertIsNone(synced["result_artifact"])

            artifact = write_result_artifact(base)
            exact = SimpleNamespace(
                base_dir=base,
                match_id="42",
                thread_id="thread-1",
                result_artifact=artifact,
                now="2026-07-22T19:03:00+09:00",
            )
            first = lineup_scheduler.cmd_complete(exact)
            self.assertFalse(first.get("duplicate_ignored", False))
            replay = lineup_scheduler.cmd_complete(exact)
            self.assertTrue(replay["duplicate_ignored"])

            with self.assertRaisesRegex(ValueError, "result tuple"):
                lineup_scheduler.cmd_complete(
                    SimpleNamespace(**{**vars(exact), "thread_id": "thread-2"})
                )
            other_artifact = Path(base) / "other-lineup-result.txt"
            other_artifact.write_text("另一份完整临场结果", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "result tuple"):
                lineup_scheduler.cmd_complete(
                    SimpleNamespace(
                        **{**vars(exact), "result_artifact": str(other_artifact)}
                    )
                )

    def test_attach_automation_is_idempotent_while_active_and_rejected_after_final(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            attach = SimpleNamespace(
                base_dir=base,
                match_id="42",
                automation_id="auto-main",
                automation_name="Soccer lineup 42",
                attempt_label="T-30",
                automation_rrule="RRULE:FREQ=DAILY;BYHOUR=10;BYMINUTE=0;COUNT=1",
            )
            first = lineup_scheduler.cmd_attach_automation(attach)
            self.assertFalse(first["duplicate_ignored"])
            duplicate = lineup_scheduler.cmd_attach_automation(attach)
            self.assertTrue(duplicate["duplicate_ignored"])
            with self.assertRaisesRegex(ValueError, "already has an automation"):
                lineup_scheduler.cmd_attach_automation(
                    SimpleNamespace(
                        **{**vars(attach), "automation_id": "auto-racing-writer"}
                    )
                )

            write_history(base, lineup_rechecked_at="2026-07-22T10:01:00+00:00")
            lineup_scheduler.cmd_complete(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-1",
                    result_artifact=write_result_artifact(base),
                    now="2026-07-22T19:02:00+09:00",
                )
            )
            with self.assertRaisesRegex(ValueError, "terminal lineup task"):
                lineup_scheduler.cmd_attach_automation(attach)

    def test_auto_synced_terminal_allows_first_tuple_then_locks_it(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            lineup_scheduler.cmd_register(register_args(base))
            synced = lineup_scheduler.cmd_status(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T19:30:00+09:00",
                )
            )["task"]
            self.assertEqual(synced["status"], "expired")
            self.assertEqual(synced["delivery_status"], "pending")
            self.assertIsNone(synced["thread_id"])

            artifact = write_result_artifact(base)
            terminal = SimpleNamespace(
                base_dir=base,
                match_id="42",
                reason="expired",
                thread_id="thread-expired-42",
                result_artifact=artifact,
                now="2026-07-22T19:31:00+09:00",
            )
            first = lineup_scheduler.cmd_terminal(terminal)
            self.assertFalse(first.get("duplicate_ignored", False))
            replay = lineup_scheduler.cmd_terminal(terminal)
            self.assertTrue(replay["duplicate_ignored"])
            with self.assertRaisesRegex(ValueError, "result tuple"):
                lineup_scheduler.cmd_terminal(
                    SimpleNamespace(**{**vars(terminal), "reason": "started"})
                )

    def test_old_task_states_backfill_result_delivery(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base, lineup_rechecked_at="2026-07-22T10:01:00+00:00")
            lineup_scheduler.cmd_register(register_args(base))
            path = lineup_scheduler.state_path(base)
            state = json.loads(path.read_text(encoding="utf-8"))
            task = state["tasks"]["42"]
            task["status"] = "completed"
            task["thread_id"] = "legacy-thread"
            task["completed_at"] = "2026-07-22T10:01:00+00:00"
            task.pop("result_delivery")
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

            pending = lineup_scheduler.cmd_status(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T19:02:00+09:00",
                )
            )
            self.assertEqual(
                pending["task"]["result_delivery"]["delivery_status"], "pending"
            )
            self.assertEqual(
                pending["task"]["result_delivery"]["thread_id"], "legacy-thread"
            )

            delivered = lineup_scheduler.cmd_mark_delivered(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="legacy-thread",
                    now="2026-07-22T19:03:00+09:00",
                )
            )
            self.assertEqual(
                delivered["task"]["result_delivery"]["delivery_status"], "delivered"
            )

        with tempfile.TemporaryDirectory() as base:
            write_history(base, lineup_rechecked_at="2026-07-22T10:01:00+00:00")
            lineup_scheduler.cmd_register(register_args(base))
            path = lineup_scheduler.state_path(base)
            state = json.loads(path.read_text(encoding="utf-8"))
            task = state["tasks"]["42"]
            task["status"] = "completed"
            task["thread_id"] = None
            task["cleanup_completed_at"] = "2026-07-22T10:05:00+00:00"
            task.pop("result_delivery")
            path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

            legacy_cleaned = lineup_scheduler.cmd_status(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T19:06:00+09:00",
                )
            )
            delivery = legacy_cleaned["task"]["result_delivery"]
            self.assertEqual(delivery["delivery_status"], "delivered")
            self.assertEqual(
                delivery["delivered_at"], "2026-07-22T10:05:00+00:00"
            )
            self.assertTrue(delivery["legacy_inferred"])


if __name__ == "__main__":
    unittest.main()

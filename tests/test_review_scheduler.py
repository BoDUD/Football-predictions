from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_scheduler.py"
SPEC = importlib.util.spec_from_file_location("soccer_review_scheduler", SCRIPT)
assert SPEC and SPEC.loader
review_scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_scheduler)


class FoldAwareEastern(tzinfo):
    """Minimal deterministic DST fold used without relying on host tzdata."""

    def utcoffset(self, value):
        return timedelta(hours=-5 if value is not None and value.fold else -4)

    def dst(self, value):
        return timedelta(0 if value is not None and value.fold else 1)

    def tzname(self, value):
        return "EST" if value is not None and value.fold else "EDT"


def archived_record(
    match_id: str = "42",
    *,
    status: str = "pending",
    kickoff: str = "2026-07-22T19:30:00+09:00",
) -> dict:
    record = {
        "match_id": match_id,
        "mode": "prematch",
        "status": status,
        "kickoff": kickoff,
        "home_team": f"主队{match_id}",
        "away_team": f"客队{match_id}",
    }
    if status == "reviewed":
        record["reviewed_at"] = "2026-07-22T14:00:00+00:00"
        record["final_score"] = "2-1"
    return record


def write_history(base: str, records: list[dict] | None = None) -> None:
    path = review_scheduler.history_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(records or [archived_record()], ensure_ascii=False),
        encoding="utf-8",
    )


def set_history_status(base: str, status: str) -> None:
    path = review_scheduler.history_path(base)
    records = json.loads(path.read_text(encoding="utf-8"))
    records[0]["status"] = status
    if status == "reviewed":
        records[0]["reviewed_at"] = "2026-07-22T14:00:00+00:00"
        records[0]["final_score"] = "2-1"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def write_result_artifact(
    base: str,
    content: str = (
        "# 赛后复盘 42\n"
        "终场比分：2-1\n"
        "主推结算与关键学习均已完整记录，等待在结果任务中交付。"
    ),
) -> str:
    path = Path(base) / "review-result.txt"
    path.write_text(content, encoding="utf-8")
    return str(path)


def register_args(base: str, **overrides):
    values = {
        "base_dir": base,
        "match_id": "42",
        "kickoff": None,
        "user_timezone": "Asia/Tokyo",
        "home_team": None,
        "away_team": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def claim_args(
    base: str,
    now: str,
    lease_minutes: float = 10,
    thread_id: str = "thread-review-42",
):
    return SimpleNamespace(
        base_dir=base,
        match_id="42",
        thread_id=thread_id,
        now=now,
        lease_minutes=lease_minutes,
    )


class ReviewSchedulerTests(unittest.TestCase):
    def test_register_defaults_to_kickoff_plus_three_hours_in_tokyo(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(
                base,
                [archived_record(kickoff="2026-07-22T18:30:00+08:00")],
            )
            result = review_scheduler.cmd_register(register_args(base))
            task = result["task"]
            attempt = task["attempts"][0]

            self.assertEqual(task["status"], "scheduled")
            self.assertEqual(task["user_timezone"], "Asia/Tokyo")
            self.assertEqual(task["kickoff"], "2026-07-22T19:30:00+09:00")
            self.assertEqual(task["initial_delay_hours"], 3)
            self.assertEqual(task["follow_up_minutes"], 30)
            self.assertEqual(attempt["run_at"], "2026-07-22T22:30:00+09:00")
            self.assertEqual(attempt["run_at_utc"], "2026-07-22T13:30:00+00:00")
            self.assertEqual(
                attempt["automation_rrule"],
                "RRULE:FREQ=DAILY;BYHOUR=13;BYMINUTE=30;COUNT=1",
            )
            schedule_spec = attempt["automation_schedule_spec"]
            self.assertEqual(
                schedule_spec["schema_version"],
                review_scheduler.EXACT_SCHEDULE_SPEC_VERSION,
            )
            self.assertEqual(schedule_spec["run_at_utc"], attempt["run_at_utc"])
            self.assertEqual(schedule_spec["dtstart_utc"], schedule_spec["until_utc"])
            self.assertEqual(schedule_spec["count"], 1)

            duplicate = review_scheduler.cmd_register(register_args(base))
            self.assertTrue(duplicate["duplicate_ignored"])
            self.assertEqual(len(duplicate["task"]["attempts"]), 1)

    def test_sync_pending_bootstraps_valid_records_idempotently(self):
        records = [
            archived_record("42", kickoff="2026-07-24T06:30:00+08:00"),
            archived_record("43", kickoff="2026-07-24T08:00:00+09:00"),
            archived_record("44", status="reviewed"),
            archived_record("45", kickoff="2026-07-24T08:00:00"),
        ]
        with tempfile.TemporaryDirectory() as base:
            write_history(base, records)
            args = SimpleNamespace(base_dir=base, user_timezone="Asia/Tokyo")
            first = review_scheduler.cmd_sync_pending(args)
            self.assertEqual(first["registered"], ["42", "43"])
            self.assertEqual(first["skipped_reviewed"], ["44"])
            self.assertEqual(first["skipped_invalid"][0]["match_id"], "45")

            second = review_scheduler.cmd_sync_pending(args)
            self.assertEqual(second["registered"], [])
            self.assertEqual(second["duplicate_ignored"], ["42", "43"])
            state = json.loads(
                review_scheduler.state_path(base).read_text(encoding="utf-8")
            )
            self.assertEqual(sorted(state["tasks"]), ["42", "43"])
            self.assertEqual(
                state["tasks"]["42"]["attempts"][0]["run_at"],
                "2026-07-24T10:30:00+09:00",
            )

            parsed = review_scheduler.build_parser().parse_args(["bootstrap"])
            self.assertEqual(parsed.command, "bootstrap")
            self.assertEqual(parsed.user_timezone, "Asia/Tokyo")
            parsed_with_now = review_scheduler.build_parser().parse_args(
                ["sync-pending", "--now", "2026-07-24T07:00:00+09:00"]
            )
            self.assertEqual(parsed_with_now.now, "2026-07-24T07:00:00+09:00")

    def test_automation_plan_exposes_only_one_verified_future_attempt(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            task = review_scheduler.cmd_register(register_args(base))["task"]
            attempt = task["attempts"][0]
            plan = review_scheduler.cmd_automation_plan(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T22:00:00+09:00",
                )
            )
            self.assertFalse(plan["catch_up_required"])
            self.assertEqual(len(plan["future_attempts"]), 1)
            self.assertEqual(plan["future_attempts"][0]["attempt_id"], "review-1")
            self.assertEqual(plan["rrule_timezone"], "UTC")

            common = {
                "base_dir": base,
                "match_id": "42",
                "attempt_id": "review-1",
                "automation_id": "review-auto-1",
                "automation_name": "Soccer review 42 attempt 1",
            }
            with self.assertRaisesRegex(ValueError, "expected UTC rule"):
                review_scheduler.cmd_attach_automation(
                    SimpleNamespace(
                        **common,
                        automation_rrule="RRULE:FREQ=DAILY;BYHOUR=22;BYMINUTE=30;COUNT=1",
                    )
                )
            attached = review_scheduler.cmd_attach_automation(
                SimpleNamespace(
                    **common,
                    automation_rrule=attempt["automation_rrule"],
                    platform_next_run="2026-07-22T22:30:00+09:00",
                )
            )
            attached_ref = attached["task"]["automation_refs"][0]
            self.assertTrue(attached_ref["schedule_verified"])
            self.assertTrue(attached_ref["platform_next_run_verified"])
            self.assertEqual(
                attached_ref["platform_next_run_utc"], "2026-07-22T13:30:00+00:00"
            )

            duplicate_plan = review_scheduler.cmd_automation_plan(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T22:05:00+09:00",
                )
            )
            self.assertEqual(duplicate_plan["future_attempts"], [])
            with self.assertRaisesRegex(ValueError, "already has an automation"):
                review_scheduler.cmd_attach_automation(
                    SimpleNamespace(
                        **{**common, "automation_id": "review-auto-other"},
                        automation_rrule=attempt["automation_rrule"],
                    )
                )
            duplicate = review_scheduler.cmd_attach_automation(
                SimpleNamespace(
                    **common,
                    automation_rrule=attempt["automation_rrule"],
                )
            )
            self.assertTrue(duplicate["duplicate_ignored"])

    def test_exact_schedule_spec_preserves_dst_fold_as_one_utc_instant(self):
        local = datetime(
            2026,
            11,
            1,
            1,
            30,
            tzinfo=FoldAwareEastern(),
            fold=1,
        )
        spec = review_scheduler.exact_utc_schedule_spec(local)
        self.assertEqual(spec["run_at_utc"], "2026-11-01T06:30:00+00:00")
        self.assertEqual(spec["dtstart_utc"], spec["until_utc"])
        self.assertEqual(spec["count"], 1)

    def test_attach_rejects_platform_next_run_after_missed_hour(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            task = review_scheduler.cmd_register(register_args(base))["task"]
            attempt = task["attempts"][0]
            with self.assertRaisesRegex(
                ValueError, "does not exactly match run_at_utc"
            ):
                review_scheduler.cmd_attach_automation(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        attempt_id="review-1",
                        automation_id="review-auto-1",
                        automation_name="Soccer review 42 attempt 1",
                        automation_rrule=attempt["automation_rrule"],
                        platform_next_run="2026-07-23T13:30:00+00:00",
                    )
                )

    def test_claim_lease_and_due_support_executor_catch_up(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            early = review_scheduler.cmd_due(
                SimpleNamespace(base_dir=base, now="2026-07-22T22:29:59+09:00")
            )
            self.assertEqual(early["due"], [])

            claimed = review_scheduler.cmd_claim(
                claim_args(base, "2026-07-22T22:30:00+09:00")
            )
            self.assertTrue(claimed["claimed"])
            self.assertFalse(claimed["catch_up"])
            duplicate = review_scheduler.cmd_claim(
                claim_args(base, "2026-07-22T22:35:00+09:00")
            )
            self.assertFalse(duplicate["claimed"])
            self.assertEqual(duplicate["reason"], "active_lease")
            during_lease = review_scheduler.cmd_due(
                SimpleNamespace(base_dir=base, now="2026-07-22T22:39:00+09:00")
            )
            self.assertEqual(during_lease["due"], [])

            recovered = review_scheduler.cmd_due(
                SimpleNamespace(base_dir=base, now="2026-07-22T22:41:00+09:00")
            )
            self.assertEqual([item["match_id"] for item in recovered["due"]], ["42"])
            self.assertTrue(recovered["due"][0]["catch_up"])
            reclaimed = review_scheduler.cmd_claim(
                claim_args(base, "2026-07-22T22:41:00+09:00")
            )
            self.assertTrue(reclaimed["claimed"])
            self.assertTrue(reclaimed["catch_up"])
            self.assertEqual(
                len(reclaimed["task"]["attempts"][0]["claims"]),
                2,
            )

            released = review_scheduler.cmd_release(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-review-42",
                    reason="executor disconnected",
                    now="2026-07-22T22:42:00+09:00",
                )
            )
            self.assertTrue(released["released"])
            self.assertEqual(released["task"]["status"], "scheduled")
            due_again = review_scheduler.cmd_due(
                SimpleNamespace(base_dir=base, now="2026-07-22T22:43:00+09:00")
            )
            self.assertEqual([item["match_id"] for item in due_again["due"]], ["42"])

    def test_claim_and_fail_require_the_claiming_thread(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            for thread_id in (None, "", "   "):
                with self.subTest(thread_id=thread_id):
                    with self.assertRaisesRegex(ValueError, "non-empty --thread-id"):
                        review_scheduler.cmd_claim(
                            SimpleNamespace(
                                base_dir=base,
                                match_id="42",
                                thread_id=thread_id,
                                now="2026-07-22T22:30:00+09:00",
                                lease_minutes=10,
                            )
                        )

            claimed = review_scheduler.cmd_claim(
                claim_args(
                    base,
                    "2026-07-22T22:30:00+09:00",
                    thread_id="thread-owner",
                )
            )
            self.assertEqual(claimed["task"]["claimed_thread_id"], "thread-owner")
            self.assertEqual(
                claimed["task"]["attempts"][0]["claims"][0]["thread_id"],
                "thread-owner",
            )

            with self.assertRaisesRegex(ValueError, "does not match the active claim"):
                review_scheduler.cmd_release(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        thread_id="thread-other",
                        reason="wrong worker",
                        now="2026-07-22T22:31:00+09:00",
                    )
                )
            released = review_scheduler.cmd_release(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-owner",
                    reason="worker failed",
                    now="2026-07-22T22:31:00+09:00",
                )
            )
            self.assertTrue(released["released"])
            self.assertIsNone(released["task"]["claimed_thread_id"])

            parsed = review_scheduler.build_parser().parse_args(
                [
                    "fail",
                    "--match-id",
                    "42",
                    "--thread-id",
                    "thread-owner",
                    "--reason",
                    "worker failed",
                ]
            )
            self.assertEqual(parsed.command, "fail")

    def test_reclaimed_attempt_can_only_be_completed_by_latest_owner(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            review_scheduler.cmd_claim(
                claim_args(
                    base,
                    "2026-07-22T22:30:00+09:00",
                    lease_minutes=5,
                    thread_id="thread-first",
                )
            )
            reclaimed = review_scheduler.cmd_claim(
                claim_args(
                    base,
                    "2026-07-22T22:36:00+09:00",
                    thread_id="thread-latest",
                )
            )
            self.assertTrue(reclaimed["claimed"])
            self.assertEqual(reclaimed["task"]["claimed_thread_id"], "thread-latest")

            set_history_status(base, "reviewed")
            result_artifact = write_result_artifact(base)
            with self.assertRaisesRegex(ValueError, "does not match the active claim"):
                review_scheduler.cmd_complete(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        thread_id="thread-first",
                        result_artifact=result_artifact,
                        now="2026-07-22T22:37:00+09:00",
                    )
                )
            completed = review_scheduler.cmd_complete(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-latest",
                    result_artifact=result_artifact,
                    now="2026-07-22T22:37:00+09:00",
                )
            )
            self.assertEqual(completed["task"]["thread_id"], "thread-latest")

    def test_complete_rejects_placeholder_or_damaged_artifacts(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            review_scheduler.cmd_claim(claim_args(base, "2026-07-22T22:30:00+09:00"))
            set_history_status(base, "reviewed")
            artifact = Path(base) / "review-result.txt"
            args = SimpleNamespace(
                base_dir=base,
                match_id="42",
                thread_id="thread-review-42",
                result_artifact=str(artifact),
                now="2026-07-22T23:02:00+09:00",
            )

            artifact.write_text("42 2-1", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "too short"):
                review_scheduler.cmd_complete(args)

            artifact.write_bytes(b"42 2-1\x00" + b"broken" * 12)
            with self.assertRaisesRegex(ValueError, "invalid text data"):
                review_scheduler.cmd_complete(args)

            artifact.write_text(
                "# 赛后复盘\n终场比分：2-1\n这是一份足够长但没有比赛编号的完整结果。",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "does not identify match 42"):
                review_scheduler.cmd_complete(args)

            artifact.write_text(
                "# 赛后复盘 42\n终场信息缺失\n这是一份足够长但没有最终比分的完整结果。",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "does not contain final score 2-1"):
                review_scheduler.cmd_complete(args)

            artifact.write_text(
                "# 赛后复盘 42\n"
                "终场比分：2–1\n"
                "主推结算、累计统计、关键学习和风险说明均已完整保存并等待交付。",
                encoding="utf-8",
            )
            completed = review_scheduler.cmd_complete(args)
            self.assertFalse(completed["duplicate_ignored"])

    def test_complete_and_terminal_reject_tasks_that_were_never_claimed(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            set_history_status(base, "reviewed")
            artifact = write_result_artifact(base)
            with self.assertRaisesRegex(ValueError, "claimed by a thread"):
                review_scheduler.cmd_complete(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        thread_id="thread-review-42",
                        result_artifact=artifact,
                        now="2026-07-22T23:02:00+09:00",
                    )
                )

        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            artifact = write_result_artifact(
                base,
                "# 复盘终态通知 42\n"
                "行政状态：postponed\n"
                "该场比赛不再重试，终态说明已保存并等待交付。",
            )
            with self.assertRaisesRegex(ValueError, "claimed by a thread"):
                review_scheduler.cmd_terminal(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        reason="postponed",
                        thread_id="thread-admin-42",
                        result_artifact=artifact,
                        now="2026-07-22T22:35:00+09:00",
                    )
                )

    def test_each_nonterminal_check_creates_one_nonduplicate_follow_up(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            review_scheduler.cmd_claim(claim_args(base, "2026-07-22T22:30:00+09:00"))
            first = review_scheduler.cmd_wait(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-review-42",
                    reason="live",
                    now="2026-07-22T22:35:00+09:00",
                )
            )
            self.assertTrue(first["follow_up_created"])
            self.assertEqual(first["task"]["status"], "waiting")
            self.assertEqual(first["follow_up"]["attempt_id"], "review-2")
            self.assertEqual(first["follow_up"]["run_at"], "2026-07-22T23:05:00+09:00")
            self.assertEqual(len(first["task"]["attempts"]), 2)

            duplicate = review_scheduler.cmd_wait(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-review-42",
                    reason="live",
                    now="2026-07-22T22:36:00+09:00",
                )
            )
            self.assertTrue(duplicate["duplicate_ignored"])
            self.assertEqual(len(duplicate["task"]["attempts"]), 2)
            plan = review_scheduler.cmd_automation_plan(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T22:40:00+09:00",
                )
            )
            self.assertEqual(
                [item["attempt_id"] for item in plan["future_attempts"]],
                ["review-2"],
            )

            review_scheduler.cmd_claim(claim_args(base, "2026-07-22T23:05:00+09:00"))
            second = review_scheduler.cmd_wait(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-review-42",
                    reason="extra-time",
                    now="2026-07-22T23:10:00+09:00",
                )
            )
            self.assertEqual(second["follow_up"]["attempt_id"], "review-3")
            self.assertEqual(second["follow_up"]["run_at"], "2026-07-22T23:40:00+09:00")
            self.assertEqual(len(second["task"]["attempts"]), 3)

    def test_reviewed_completion_stages_delivery_before_exact_cleanup(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            task = review_scheduler.cmd_register(register_args(base))["task"]
            attempt = task["attempts"][0]
            review_scheduler.cmd_attach_automation(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    attempt_id="review-1",
                    automation_id="review-auto-1",
                    automation_name="Soccer review 42",
                    automation_rrule=attempt["automation_rrule"],
                )
            )
            review_scheduler.cmd_claim(claim_args(base, "2026-07-22T22:30:00+09:00"))
            follow_up = review_scheduler.cmd_wait(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-review-42",
                    reason="live",
                    now="2026-07-22T22:35:00+09:00",
                )
            )["follow_up"]
            review_scheduler.cmd_attach_automation(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    attempt_id="review-2",
                    automation_id="review-auto-2",
                    automation_name="Soccer review 42 follow-up",
                    automation_rrule=follow_up["automation_rrule"],
                )
            )
            review_scheduler.cmd_claim(claim_args(base, "2026-07-22T23:05:00+09:00"))
            set_history_status(base, "reviewed")

            synced = review_scheduler.cmd_due(
                SimpleNamespace(base_dir=base, now="2026-07-22T23:06:00+09:00")
            )
            self.assertEqual(synced["due"], [])
            status = review_scheduler.cmd_status(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T23:06:00+09:00",
                )
            )["task"]
            self.assertEqual(status["status"], "completed")
            self.assertFalse(status["delivered"])
            with self.assertRaisesRegex(ValueError, "must be delivered"):
                review_scheduler.cmd_mark_cleaned(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        automation_id=["review-auto-1"],
                        now="2026-07-22T23:01:00+09:00",
                    )
                )
            with self.assertRaisesRegex(ValueError, "--result-artifact"):
                review_scheduler.cmd_complete(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        thread_id="thread-review-42",
                        result_artifact=None,
                        now="2026-07-22T23:02:00+09:00",
                    )
                )

            result_artifact = write_result_artifact(base)
            completed = review_scheduler.cmd_complete(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-review-42",
                    result_artifact=result_artifact,
                    now="2026-07-22T23:02:00+09:00",
                )
            )
            self.assertEqual(completed["task"]["thread_id"], "thread-review-42")
            self.assertFalse(completed["task"]["delivered"])
            self.assertEqual(completed["task"]["delivery_status"], "pending")
            self.assertEqual(
                [ref["id"] for ref in completed["cleanup_automation_refs"]],
                ["review-auto-1", "review-auto-2"],
            )
            cleanup_due = review_scheduler.cmd_cleanup_due(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T23:02:30+09:00",
                )
            )
            self.assertEqual(cleanup_due["due"][0]["next_action"], "verify_delivery")
            with self.assertRaisesRegex(ValueError, "must be delivered"):
                review_scheduler.cmd_mark_cleaned(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        automation_id=["review-auto-1", "review-auto-2"],
                        now="2026-07-22T23:02:40+09:00",
                    )
                )
            delivered = review_scheduler.cmd_mark_delivered(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-review-42",
                    now="2026-07-22T23:02:50+09:00",
                )
            )
            self.assertTrue(delivered["task"]["delivered"])
            self.assertEqual(delivered["task"]["delivery_status"], "delivered")
            with self.assertRaisesRegex(ValueError, "still require cleanup"):
                review_scheduler.cmd_mark_cleaned(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        automation_id=["review-auto-1"],
                        now="2026-07-22T23:03:00+09:00",
                    )
                )
            cleaned = review_scheduler.cmd_mark_cleaned(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    automation_id=["review-auto-1", "review-auto-2"],
                    now="2026-07-22T23:03:00+09:00",
                )
            )
            self.assertTrue(cleaned["task"]["cleaned"])
            self.assertEqual(
                cleaned["task"]["cleaned_automation_ids"],
                ["review-auto-1", "review-auto-2"],
            )
            with self.assertRaisesRegex(ValueError, "terminal review task"):
                review_scheduler.cmd_attach_automation(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        attempt_id="review-2",
                        automation_id="late-review-auto",
                        automation_name="Late review automation",
                        automation_rrule=follow_up["automation_rrule"],
                    )
                )

    def test_pending_review_result_tuple_only_allows_exact_replay(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            review_scheduler.cmd_claim(claim_args(base, "2026-07-22T22:30:00+09:00"))
            state_file = review_scheduler.state_path(base)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["tasks"]["42"].pop("claimed_thread_id")
            state_file.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
            set_history_status(base, "reviewed")
            synced = review_scheduler.cmd_status(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T23:00:00+09:00",
                )
            )["task"]
            self.assertEqual(synced["delivery_status"], "pending")
            self.assertIsNone(synced["thread_id"])
            self.assertIsNone(synced["result_artifact"])

            artifact = write_result_artifact(base)
            exact = SimpleNamespace(
                base_dir=base,
                match_id="42",
                thread_id="thread-review-42",
                result_artifact=artifact,
                now="2026-07-22T23:02:00+09:00",
            )
            first = review_scheduler.cmd_complete(exact)
            self.assertFalse(first["duplicate_ignored"])
            replay = review_scheduler.cmd_complete(exact)
            self.assertTrue(replay["duplicate_ignored"])

            with self.assertRaisesRegex(ValueError, "does not match the active claim"):
                review_scheduler.cmd_complete(
                    SimpleNamespace(
                        **{**vars(exact), "thread_id": "thread-review-other"}
                    )
                )
            other_artifact = Path(base) / "other-review-result.txt"
            other_artifact.write_text(
                "# 赛后复盘 42\n"
                "终场比分：2-1\n"
                "这是另一份结构完整但路径不同的赛后复盘结果。",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "result tuple"):
                review_scheduler.cmd_complete(
                    SimpleNamespace(
                        **{**vars(exact), "result_artifact": str(other_artifact)}
                    )
                )
            with self.assertRaisesRegex(ValueError, "result tuple"):
                review_scheduler.cmd_terminal(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        reason="postponed",
                        thread_id="thread-review-42",
                        result_artifact=artifact,
                        now="2026-07-22T23:03:00+09:00",
                    )
                )

    def test_cleanup_recovery_waits_for_metadata_grace_but_complete_tuple_is_immediate(
        self,
    ):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            review_scheduler.cmd_claim(claim_args(base, "2026-07-22T22:30:00+09:00"))
            set_history_status(base, "reviewed")

            within_grace = review_scheduler.cmd_cleanup_due(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T14:09:59+00:00",
                )
            )
            self.assertEqual(within_grace["due"], [])

            after_grace = review_scheduler.cmd_cleanup_due(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T14:10:01+00:00",
                )
            )
            self.assertEqual(len(after_grace["due"]), 1)
            self.assertEqual(
                after_grace["due"][0]["next_action"], "await_complete_metadata"
            )

        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            review_scheduler.cmd_claim(claim_args(base, "2026-07-22T22:30:00+09:00"))
            set_history_status(base, "reviewed")
            artifact = write_result_artifact(base)
            review_scheduler.cmd_complete(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    thread_id="thread-review-42",
                    result_artifact=artifact,
                    now="2026-07-22T14:02:00+00:00",
                )
            )
            immediate = review_scheduler.cmd_cleanup_due(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T14:02:01+00:00",
                )
            )
            self.assertEqual(len(immediate["due"]), 1)
            self.assertEqual(immediate["due"][0]["next_action"], "verify_delivery")

    def test_pending_terminal_reason_is_immutable(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base)
            review_scheduler.cmd_register(register_args(base))
            review_scheduler.cmd_claim(
                claim_args(
                    base,
                    "2026-07-22T22:30:00+09:00",
                    thread_id="thread-admin-42",
                )
            )
            set_history_status(base, "postponed")
            synced = review_scheduler.cmd_status(
                SimpleNamespace(
                    base_dir=base,
                    match_id="42",
                    now="2026-07-22T22:34:00+09:00",
                )
            )["task"]
            self.assertEqual(synced["status"], "terminal")
            self.assertEqual(synced["delivery_status"], "pending")
            self.assertIsNone(synced["thread_id"])
            artifact = write_result_artifact(base)
            terminal = SimpleNamespace(
                base_dir=base,
                match_id="42",
                reason="postponed",
                thread_id="thread-admin-42",
                result_artifact=artifact,
                now="2026-07-22T22:35:00+09:00",
            )
            first = review_scheduler.cmd_terminal(terminal)
            self.assertFalse(first["duplicate_ignored"])
            replay = review_scheduler.cmd_terminal(terminal)
            self.assertTrue(replay["duplicate_ignored"])
            with self.assertRaisesRegex(ValueError, "result tuple"):
                review_scheduler.cmd_terminal(
                    SimpleNamespace(**{**vars(terminal), "reason": "cancelled"})
                )

    def test_administrative_terminal_states_deliver_before_cleanup(self):
        for reason in ("cancelled", "postponed", "abandoned"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as base:
                write_history(base)
                review_scheduler.cmd_register(register_args(base))
                review_scheduler.cmd_claim(
                    claim_args(
                        base,
                        "2026-07-22T22:30:00+09:00",
                        thread_id="thread-admin-42",
                    )
                )
                with self.assertRaisesRegex(ValueError, "--result-artifact"):
                    review_scheduler.cmd_terminal(
                        SimpleNamespace(
                            base_dir=base,
                            match_id="42",
                            reason=reason,
                            thread_id="thread-admin-42",
                            result_artifact=None,
                            now="2026-07-22T22:35:00+09:00",
                        )
                    )
                result_artifact = write_result_artifact(
                    base,
                    "# 复盘终态通知 42\n"
                    f"行政状态：{reason}\n"
                    "该场比赛不再重试，终态说明已保存并等待交付。",
                )
                terminal = review_scheduler.cmd_terminal(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        reason=reason,
                        thread_id="thread-admin-42",
                        result_artifact=result_artifact,
                        now="2026-07-22T22:35:00+09:00",
                    )
                )
                self.assertEqual(terminal["task"]["status"], "terminal")
                self.assertEqual(terminal["task"]["terminal_reason"], reason)
                self.assertEqual(terminal["task"]["delivery_status"], "pending")
                due = review_scheduler.cmd_due(
                    SimpleNamespace(base_dir=base, now="2026-07-23T00:00:00+09:00")
                )
                self.assertEqual(due["due"], [])
                with self.assertRaisesRegex(ValueError, "must be delivered"):
                    review_scheduler.cmd_mark_cleaned(
                        SimpleNamespace(
                            base_dir=base,
                            match_id="42",
                            automation_id=None,
                            now="2026-07-22T22:35:30+09:00",
                        )
                    )
                review_scheduler.cmd_mark_delivered(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        thread_id="thread-admin-42",
                        now="2026-07-22T22:35:45+09:00",
                    )
                )
                cleaned = review_scheduler.cmd_mark_cleaned(
                    SimpleNamespace(
                        base_dir=base,
                        match_id="42",
                        automation_id=None,
                        now="2026-07-22T22:36:00+09:00",
                    )
                )
                self.assertTrue(cleaned["task"]["cleaned"])


if __name__ == "__main__":
    unittest.main()

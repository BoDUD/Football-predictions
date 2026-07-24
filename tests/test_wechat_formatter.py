from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wechat_formatter.py"
SPEC = importlib.util.spec_from_file_location("soccer_wechat_formatter", SCRIPT)
assert SPEC and SPEC.loader
formatter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(formatter)


def base_record() -> dict:
    primary = {
        "market": "total",
        "side": "under",
        "line": 2.5,
        "odds": 0.92,
        "probability": 0.58,
        "ev": 0.08,
        "role": "primary",
    }
    return {
        "match_id": "42",
        "mode": "prematch",
        "status": "pending",
        "analysis_stage": "initial",
        "league": "2026芬超第16轮",
        "league_key": "芬超",
        "kickoff": "2026-07-23T19:30:00+09:00",
        "home_team": "主队",
        "away_team": "客队",
        "recommendation": "小球方向更稳",
        "notes": "阵容仍有不确定性",
        "predicted_score": "1-0",
        "exact_score_picks": [
            {"score": "1-0", "probability": 0.20, "rank": 1},
            {"score": "1-1", "probability": 0.16, "rank": 2},
        ],
        "asian_pick": {
            "side": "away",
            "line": 0.25,
            "odds": 0.86,
            "probability": 0.55,
            "ev": 0.05,
            "role": "secondary",
        },
        "total_pick": dict(primary),
        "half_time_pick": None,
        "htft_picks": [],
        "primary_market": "total",
        "primary_pick": dict(primary),
        "primary_change": {"status": "initial"},
        "revisions": [],
        "created_at": "2026-07-22T09:00:00+00:00",
        "updated_at": "2026-07-22T09:00:00+00:00",
        "lineup_rechecked_at": None,
    }


def write_history(base: str, records: list[dict]) -> None:
    path = formatter.memory_store.data_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


class WeChatFormatterTests(unittest.TestCase):
    def assert_plain(self, text: str) -> None:
        self.assertNotRegex(text, r"(?:^|\n)(?:#|[-*+] |```)")
        self.assertNotIn("<table", text.lower())
        self.assertLessEqual(len(text.splitlines()), 18)

    def test_initial_copy_is_complete_plain_text(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [base_record()])
            text = formatter.render(base, "42", "initial")
            self.assertTrue(text.startswith("【初盘分析｜42】\n"))
            for field in ("赛事：芬超", "比赛：主队 vs 客队", "开赛：", "主推：小2.5 @0.92", "次选：", "比分参考："):
                self.assertIn(field, text)
            self.assert_plain(text)

    def test_initial_copy_does_not_truncate_long_fields(self):
        record = base_record()
        record["recommendation"] = "R" * 220
        record["notes"] = "N" * 500
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "initial")
            self.assertIn(record["recommendation"], text)
            self.assertIn(record["notes"], text)
            self.assertNotIn("…", text)
            self.assert_plain(text)

    def test_lineup_copy_states_change_and_active_primary(self):
        record = base_record()
        record["revisions"] = [{
            key: record.get(key)
            for key in (
                "analysis_stage", "recommendation", "notes", "predicted_score",
                "exact_score_picks", "asian_pick", "total_pick", "half_time_pick",
                "htft_picks", "primary_market", "primary_pick", "primary_change",
            )
        }]
        record.update({
            "analysis_stage": "lineup-check",
            "lineup_rechecked_at": "2026-07-23T10:02:00+00:00",
            "primary_market": "asian",
            "primary_pick": dict(record["asian_pick"], market="asian", role="primary"),
            "primary_change": {"status": "changed"},
        })
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "lineup-check")
            self.assertTrue(text.startswith("【临场分析｜42】\n"))
            self.assertIn("主推变更：小2.5 @0.92 → 客队 +0.25 @0.86", text)
            self.assertIn("当前主推：客队 +0.25 @0.86", text)
            self.assertIn("检查时间：2026-07-23 19:02（日本时间）", text)
            self.assert_plain(text)

    def test_no_primary_lineup_and_review_are_explicitly_not_settled(self):
        record = base_record()
        record["revisions"] = [{
            key: record.get(key)
            for key in (
                "analysis_stage", "recommendation", "notes", "predicted_score",
                "exact_score_picks", "asian_pick", "total_pick", "half_time_pick",
                "htft_picks", "primary_market", "primary_pick", "primary_change",
            )
        }]
        record.update({
            "analysis_stage": "lineup-check",
            "lineup_rechecked_at": "2026-07-23T10:02:00+00:00",
            "asian_pick": None,
            "total_pick": None,
            "half_time_pick": None,
            "htft_picks": [],
            "primary_market": None,
            "primary_pick": None,
            "primary_change": {
                "status": "changed",
                "decision": "cancelled_to_none",
            },
        })
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            lineup_text = formatter.render(base, "42", "lineup-check")
            self.assertIn("主推取消：小2.5 @0.92 → 不下注", lineup_text)
            self.assertIn("当前主推：无正式推荐", lineup_text)
            self.assert_plain(lineup_text)

            record.update({
                "status": "reviewed",
                "half_time_score": "0-0",
                "final_score": "1-0",
                "primary_result": None,
                "key_learning": "旧方向失效后没有强行寻找替代主推",
                "reviewed_at": "2026-07-23T13:00:00+00:00",
                "settlement_basis": {
                    "policy": "latest_active_prematch_version",
                    "analysis_stage": "lineup-check",
                    "primary_market": None,
                    "primary_pick": None,
                    "formal_picks": {
                        "asian": None,
                        "total": None,
                        "half_time": None,
                        "htft": [],
                    },
                },
            })
            write_history(base, [record])
            review_text = formatter.render(base, "42", "review")
            self.assertIn("主推：无正式推荐（不结算、不计战绩）", review_text)
            self.assertNotIn("无正式推荐＝未结算", review_text)
            self.assert_plain(review_text)

    def test_review_copy_uses_settlement_basis_and_statistics(self):
        record = base_record()
        record.update({
            "status": "reviewed",
            "analysis_stage": "lineup-check",
            "lineup_rechecked_at": "2026-07-23T10:02:00+00:00",
            "half_time_score": "0-0",
            "final_score": "1-0",
            "total_result": "win",
            "primary_result": "win",
            "exact_score_hit_rank": 1,
            "key_learning": "临场低节奏判断得到验证",
            "reviewed_at": "2026-07-23T13:00:00+00:00",
            "settlement_basis": {
                "policy": "latest_active_prematch_version",
                "analysis_stage": "lineup-check",
                "primary_market": "total",
                "primary_pick": dict(record["primary_pick"]),
                "formal_picks": {
                    "asian": record["asian_pick"],
                    "total": record["total_pick"],
                    "half_time": None,
                    "htft": [],
                },
            },
        })
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "review")
            self.assertTrue(text.startswith("【赛后复盘｜芬超｜42】\n"))
            self.assertIn("结算依据：临场版最终有效推荐", text)
            self.assertIn("主推：小2.5 @0.92＝红", text)
            self.assertIn("次选参考：客队 +0.25 @0.86（不结算、不计战绩）", text)
            self.assertNotIn("客队 +0.25 @0.86＝", text)
            self.assertIn("芬超主推：1场1胜0负0走", text)
            self.assertIn("累计主推：1场1胜0负0走", text)
            self.assert_plain(text)


if __name__ == "__main__":
    unittest.main()

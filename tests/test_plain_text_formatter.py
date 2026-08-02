from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plain_text_formatter.py"
SPEC = importlib.util.spec_from_file_location("soccer_plain_text_formatter", SCRIPT)
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
        "zero_zero_audit": {
            "score": "0-0",
            "probability": 0.12,
            "rank": 4,
            "included_in_top2": False,
            "status": "analyzed_not_top_two",
            "odds": 12.0,
            "ev": 0.44,
        },
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


class PlainTextFormatterTests(unittest.TestCase):
    def test_no_automatic_external_message_delivery_surface(self):
        root = Path(__file__).resolve().parents[1]
        removed_paths = (
            "scripts/wechat_push.py",
            "scripts/wechat_push.ps1",
            "references/wechat-delivery.md",
            "tests/test_wechat_push.py",
        )
        for relative_path in removed_paths:
            with self.subTest(relative_path=relative_path):
                self.assertFalse((root / relative_path).exists())

        active_guidance = "\n".join(
            (root / relative_path).read_text(encoding="utf-8")
            for relative_path in (
                "SKILL.md",
                "README.md",
                "references/plain-text-output.md",
                "references/review-framework.md",
            )
        ).lower()
        for forbidden in (
            "wechat_push",
            "wechat-delivery",
            "pywechat",
            "pyweixin",
            "verify-draft",
            "--send",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, active_guidance)

    def assert_plain(self, text: str) -> None:
        self.assertNotRegex(text, r"(?:^|\n)(?:#|[-*+] |```)")
        self.assertNotIn("<table", text.lower())
        self.assertLessEqual(len(text.splitlines()), 18)

    def test_initial_plain_text_is_complete(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [base_record()])
            text = formatter.render(base, "42", "initial")
            self.assertTrue(text.startswith("【初盘分析｜42】\n"))
            for field in ("赛事：芬超", "比赛：主队 vs 客队", "开赛：", "主推：小2.5 @0.92", "次选参考：", "比分参考："):
                self.assertIn(field, text)
            self.assertNotIn("0-0核验：", text)
            self.assertNotIn("0-0（12.0%）", text)
            self.assert_plain(text)

    def test_legacy_half_time_primary_uses_executable_submarket(self):
        with tempfile.TemporaryDirectory() as base:
            record = base_record()
            half_pick = {
                "market": "total",
                "side": "over",
                "line": 1.0,
                "odds": 0.98,
                "probability": 0.33,
                "ev": 0.016,
                "role": "primary",
            }
            record["primary_market"] = "half_time"
            record["half_time_pick"] = dict(half_pick)
            record["primary_pick"] = {
                **half_pick,
                "market": "half_time",
            }
            write_history(base, [record])

            text = formatter.render(base, "42", "initial")

            self.assertIn("主推：半场大1 @0.98", text)
            self.assertIn("次选参考：客队 +0.25 @0.86、小2.5 @0.92", text)
            self.assertNotIn("次选参考：半场大1", text)
            self.assertNotIn("半场 客队 +1", text)
            self.assert_plain(text)

    def test_primary_conditioned_scores_are_used_in_user_facing_plain_text(self):
        record = base_record()
        record["primary_pick"]["side"] = "over"
        record["total_pick"]["side"] = "over"
        record["display_predicted_score"] = "2-1"
        record["display_exact_score_picks"] = [
            {
                "score": "2-1",
                "probability": 0.0986,
                "conditional_probability": 0.1802,
                "display_rank": 1,
                "unconditional_rank": 3,
            },
            {
                "score": "3-1",
                "probability": 0.0598,
                "conditional_probability": 0.1093,
                "display_rank": 2,
                "unconditional_rank": 5,
            },
        ]
        record["display_exact_score_basis"] = {
            "basis": "primary_total_net_profit",
            "market": "total",
            "side": "over",
            "line": 2.5,
            "event_probability": 0.5471,
        }

        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "initial")
            self.assertIn(
                "比分参考：2-1（全场9.9%，主推成立时18.0%）、"
                "3-1（全场6.0%，主推成立时10.9%）",
                text,
            )
            self.assertNotIn("比分参考：1-0", text)
            self.assert_plain(text)

    def test_format_pick_supports_expanded_markets(self):
        record = base_record()
        cases = (
            (
                "goal_range",
                {"selection": "2-3", "odds": 2.10},
                "总进球2-3球 @2.10",
            ),
            (
                "goal_range",
                {"minimum_goals": 7, "maximum_goals": None, "odds": 6.50},
                "总进球7+球 @6.50",
            ),
            (
                "goal_range",
                {"min_goals": 2, "max_goals": 3, "odds": 2.10},
                "总进球2-3球 @2.10",
            ),
            (
                "btts",
                {"side": "yes", "odds": 1.80},
                "双方进球-是 @1.80",
            ),
            (
                "corner_total",
                {"side": "over", "line": 10.5, "odds": 0.90},
                "角球大10.5 @0.90",
            ),
            (
                "corner_handicap",
                {"side": "home", "line": -1.5, "odds": 0.95},
                "主队角球-1.5 @0.95",
            ),
        )
        for market, pick, expected in cases:
            with self.subTest(market=market, pick=pick):
                self.assertEqual(formatter.format_pick(market, pick, record), expected)

    def test_initial_and_lineup_plain_text_include_expanded_market_picks(self):
        record = base_record()
        goal_range = {
            "selection": "2-3",
            "minimum_goals": 2,
            "maximum_goals": 3,
            "odds": 2.10,
            "probability": 0.55,
            "ev": 0.155,
            "role": "primary",
        }
        record.update({
            "goal_range_pick": dict(goal_range),
            "btts_pick": {
                "side": "yes",
                "odds": 1.80,
                "probability": 0.61,
                "ev": 0.098,
                "role": "secondary",
            },
            "corner_total_pick": {
                "side": "over",
                "line": 10.5,
                "odds": 0.90,
                "probability": 0.58,
                "ev": 0.102,
                "role": "secondary",
            },
            "primary_market": "goal_range",
            "primary_pick": dict(goal_range),
        })
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            initial_text = formatter.render(base, "42", "initial")
            self.assertIn("主推：总进球2-3球 @2.10", initial_text)
            self.assertIn(
                "次选参考：客队 +0.25 @0.86、小2.5 @0.92、双方进球-是 @1.80、角球大10.5 @0.90"
                "（不结算、不计战绩、不计金额）",
                initial_text,
            )
            self.assert_plain(initial_text)

            record["revisions"] = [{
                key: record.get(key)
                for key in (
                    "analysis_stage", "recommendation", "notes", "predicted_score",
                    "exact_score_picks", "zero_zero_audit", "asian_pick", "total_pick", "half_time_pick",
                    "htft_picks", "goal_range_pick", "btts_pick", "corner_total_pick",
                    "corner_handicap_pick", "primary_market", "primary_pick", "primary_change",
                )
            }]
            corner_handicap = {
                "side": "home",
                "line": -1.5,
                "odds": 0.95,
                "probability": 0.62,
                "ev": 0.209,
                "role": "primary",
            }
            record.update({
                "analysis_stage": "lineup-check",
                "lineup_rechecked_at": "2026-07-23T10:02:00+00:00",
                "corner_handicap_pick": dict(corner_handicap),
                "primary_market": "corner_handicap",
                "primary_pick": dict(corner_handicap),
                "primary_change": {"status": "changed"},
            })
            write_history(base, [record])
            lineup_text = formatter.render(base, "42", "lineup-check")
            self.assertIn(
                "主推变更：总进球2-3球 @2.10 → 主队角球-1.5 @0.95",
                lineup_text,
            )
            self.assertIn("当前主推：主队角球-1.5 @0.95", lineup_text)
            self.assert_plain(lineup_text)

    def test_zero_zero_is_displayed_only_when_it_ranks_in_top_two(self):
        record = base_record()
        record["predicted_score"] = "0-0"
        record["exact_score_picks"] = [
            {"score": "0-0", "probability": 0.24, "rank": 1},
            {"score": "1-0", "probability": 0.19, "rank": 2},
        ]
        record["zero_zero_audit"] = {
            "score": "0-0",
            "probability": 0.24,
            "rank": 1,
            "included_in_top2": True,
            "status": "top_two",
            "odds": 7.0,
            "ev": 0.68,
        }
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "initial")
            self.assertIn("比分参考：0-0（24.0%）、1-0（19.0%）", text)
            self.assertNotIn("0-0核验：", text)
            self.assert_plain(text)

    def test_initial_plain_text_does_not_truncate_long_fields(self):
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

    def test_hidden_zero_zero_audit_does_not_leak_through_user_facing_prose(self):
        record = base_record()
        record["recommendation"] = "小球方向更稳；0-0核验未进前二。赔率12，EV44%。低节奏判断保留。"
        record["notes"] = "阵容仍有不确定性；概率12.0%，全分布第4。对应比分0-0。赔率12，EV44%。其他风险保留。"
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "initial")
            self.assertIn("核心判断：小球方向更稳；低节奏判断保留。", text)
            self.assertIn("风险：阵容仍有不确定性；其他风险保留。", text)
            for hidden in ("0-0核验", "对应比分0-0", "概率12.0%", "赔率12", "EV44%"):
                self.assertNotIn(hidden, text)
            self.assert_plain(text)

    def test_lineup_plain_text_states_change_and_active_primary(self):
        record = base_record()
        record["revisions"] = [{
            key: record.get(key)
            for key in (
                "analysis_stage", "recommendation", "notes", "predicted_score",
                "exact_score_picks", "zero_zero_audit", "asian_pick", "total_pick", "half_time_pick",
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
            self.assertNotIn("0-0核验：", text)
            self.assert_plain(text)

    def test_no_primary_lineup_and_review_are_explicitly_not_settled(self):
        record = base_record()
        record["revisions"] = [{
            key: record.get(key)
            for key in (
                "analysis_stage", "recommendation", "notes", "predicted_score",
                "exact_score_picks", "zero_zero_audit", "asian_pick", "total_pick", "half_time_pick",
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
            self.assertIn(
                "学习归档：无主推观察样本（只用于规则与数据质量复核）",
                review_text,
            )
            self.assertNotIn("无正式推荐＝未结算", review_text)
            self.assert_plain(review_text)

    def test_review_plain_text_uses_settlement_basis_and_statistics(self):
        record = base_record()
        record.update({
            "status": "reviewed",
            "evaluation_eligibility": {
                "policy_version": "strict-oos-market-policy-v1",
                "strict_forward_oos": True,
                "reason": "validated_score_model_provenance",
            },
            "analysis_stage": "lineup-check",
            "lineup_rechecked_at": "2026-07-23T10:02:00+00:00",
            "half_time_score": "0-0",
            "final_score": "1-0",
            "total_result": "win",
            "primary_result": "win",
            "exact_score_hit_rank": 1,
            "key_learning": "临场低节奏判断得到验证；0-0全分布第4。赔率12，EV44%。保留小样本观察。",
            "reviewed_at": "2026-07-23T13:00:00+00:00",
            "settlement_basis": {
                "policy": "latest_active_prematch_version",
                "analysis_stage": "lineup-check",
                "evaluation_eligibility": {
                    "policy_version": "strict-oos-market-policy-v1",
                    "strict_forward_oos": True,
                    "reason": "validated_score_model_provenance",
                },
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
            self.assertIn("次选参考：客队 +0.25 @0.86（不结算、不计战绩、不计金额）", text)
            self.assertNotIn("客队 +0.25 @0.86＝", text)
            self.assertIn("芬超主推：1场1胜0负0走", text)
            self.assertIn("累计主推：1场1胜0负0走", text)
            self.assertIn("本场关键：临场低节奏判断得到验证；保留小样本观察。", text)
            self.assertNotIn("赔率12", text)
            self.assertNotIn("0-0核验：", text)
            self.assert_plain(text)

    def test_review_plain_text_lists_all_expanded_secondary_markets_without_results(self):
        record = base_record()
        record.update({
            "status": "reviewed",
            "half_time_score": "1-0",
            "final_score": "2-1",
            "primary_result": "win",
            "key_learning": "多市场候选池按统一门槛筛选",
            "reviewed_at": "2026-07-23T13:00:00+00:00",
            "settlement_basis": {
                "policy": "latest_active_prematch_version",
                "analysis_stage": "initial",
                "primary_market": "total",
                "primary_pick": dict(record["primary_pick"]),
                "formal_picks": {
                    "asian": record["asian_pick"],
                    "total": record["total_pick"],
                    "half_time": None,
                    "htft": [],
                    "goal_range": {
                        "selection": "2-3",
                        "minimum_goals": 2,
                        "maximum_goals": 3,
                        "odds": 2.10,
                    },
                    "btts": {"side": "yes", "odds": 1.80},
                    "corner_total": {"side": "over", "line": 10.5, "odds": 0.90},
                    "corner_handicap": {"side": "home", "line": -1.5, "odds": 0.95},
                },
            },
        })
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "review")
            self.assertIn("总进球2-3球 @2.10", text)
            self.assertIn("双方进球-是 @1.80", text)
            self.assertIn("角球大10.5 @0.90", text)
            self.assertIn("主队角球-1.5 @0.95", text)
            self.assertIn("（不结算、不计战绩、不计金额）", text)
            for market_text in ("总进球2-3球 @2.10", "双方进球-是 @1.80", "角球大10.5 @0.90", "主队角球-1.5 @0.95"):
                self.assertNotIn(f"{market_text}＝", text)
            self.assert_plain(text)


if __name__ == "__main__":
    unittest.main()

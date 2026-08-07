from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


def joint_artifact() -> dict:
    base_audit = {
        "provenance": "validated_joint_cells",
        "probability_mode": "model_only",
        "status": "model_probability_reference",
        "recommendation_eligible": False,
        "template_fallback_allowed": False,
    }
    audits = {
        field: dict(base_audit)
        for field in ("htft_marginal", "one_x_two", "goal_ranges", "btts")
    }
    audits["joint_top_two"] = {
        "provenance": "validated_joint_cells_probability_ranking",
        "probability_mode": "model_only",
        "status": "high_variance_reference",
        "recommendation_eligible": False,
        "template_fallback_allowed": False,
    }
    leading = [
        ("DD", "1-1", 0.058),
        ("AA", "1-2", 0.045),
    ]
    branch_support = [
        ("DA", "0-1", 0.021),
        ("DH", "1-0", 0.020),
    ]
    all_values = leading + branch_support
    used = {(htft, score) for htft, score, _probability in all_values}
    fillers: list[tuple[str, str]] = []
    for home_goals in range(7):
        for away_goals in range(7):
            full = (
                "H"
                if home_goals > away_goals
                else "A"
                if home_goals < away_goals
                else "D"
            )
            score = f"{home_goals}-{away_goals}"
            for half in ("H", "D", "A"):
                key = (half + full, score)
                if key not in used:
                    fillers.append(key)
            if len(fillers) >= 60:
                break
        if len(fillers) >= 60:
            break
    filler_probability = (1.0 - sum(item[2] for item in all_values)) / len(fillers)
    joint_cells = []
    for htft, score, probability_value in all_values:
        home_goals, away_goals = (int(item) for item in score.split("-"))
        joint_cells.append(
            {
                "htft": htft,
                "score": score,
                "home_goals": home_goals,
                "away_goals": away_goals,
                "probability": probability_value,
            }
        )
    for htft, score in fillers:
        home_goals, away_goals = (int(item) for item in score.split("-"))
        joint_cells.append(
            {
                "htft": htft,
                "score": score,
                "home_goals": home_goals,
                "away_goals": away_goals,
                "probability": filler_probability,
            }
        )

    return {
        "schema_version": formatter.public_market_outlook.joint_scenario_model.LEGACY_SCHEMA_VERSION,
        "model_version": formatter.public_market_outlook.joint_scenario_model.LEGACY_MODEL_VERSION,
        "prediction_hash": "sha256:" + "a" * 64,
        "probability_mode": "model_only",
        "formal_eligible": False,
        "htft_marginal": {
            "half_time_result_probabilities": {"H": 0.31, "D": 0.44, "A": 0.25},
            "full_time_result_probabilities": {"H": 0.29, "D": 0.25, "A": 0.46},
            "code_probabilities": {
                "HH": 0.11,
                "HD": 0.04,
                "HA": 0.16,
                "DH": 0.11,
                "DD": 0.17,
                "DA": 0.16,
                "AH": 0.07,
                "AD": 0.04,
                "AA": 0.14,
            },
        },
        "derived": {
            "one_x_two": {"home": 0.29, "draw": 0.25, "away": 0.46},
            "goal_ranges": {"0-1": 0.14, "2-3": 0.41, "4-6": 0.39, "7+": 0.06},
            "btts": {"yes": 0.62, "no": 0.38},
        },
        "joint_top_two": [
            {
                "slot": 1,
                "htft": "DD",
                "score": "1-1",
                "home_goals": 1,
                "away_goals": 1,
                "probability": 0.058,
                "status": "high_variance_reference",
                "recommendation_eligible": False,
                "counts_toward_primary_record": False,
                "odds_available": False,
            },
            {
                "slot": 2,
                "htft": "AA",
                "score": "1-2",
                "home_goals": 1,
                "away_goals": 2,
                "probability": 0.045,
                "status": "high_variance_reference",
                "recommendation_eligible": False,
                "counts_toward_primary_record": False,
                "odds_available": False,
            },
        ],
        "joint_cells": joint_cells,
        "derived_field_audits": audits,
    }


def competition_snapshot(record: dict, collected_at: str) -> dict:
    return {
        "source_url": record["source_url"],
        "response_url": record["source_url"],
        "page_sha256": "sha256:" + "b" * 64,
        "etag": "",
        "last_modified": "",
        "collected_at": collected_at,
        "header": {
            "home_team": record["home_team"],
            "away_team": record["away_team"],
            "competition_label": "巴西杯",
            "competition_id": "186",
            "competition_locator": "//info.titan007.com/cup_match/2026-2027/cupmatch_vs/cupmatch_186.htm",
        },
    }


class PlainTextFormatterTests(unittest.TestCase):
    def setUp(self) -> None:
        validator = patch.object(
            formatter.public_market_outlook.joint_scenario_model,
            "validate_prediction",
        )
        validator.start()
        self.addCleanup(validator.stop)

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

    def test_all_registered_model_league_keys_have_chinese_display_labels(self):
        expected = {
            "brazil_serie_a": "巴甲",
            "brazil_cup": "巴西杯",
            "norway_eliteserien": "挪超",
            "japan_j1": "日职",
            "usa_mls": "美职联",
            "england_premier_league": "英超",
            "england_league_cup": "英联杯",
            "netherlands_eerste_divisie": "荷乙",
            "france_ligue_1": "法甲",
            "spain_la_liga": "西甲",
            "germany_bundesliga": "德甲",
            "italy_serie_a": "意甲",
            "portugal_primeira_liga": "葡超",
            "korea_k_league_1": "韩K联",
            "sweden_allsvenskan": "瑞典超",
            "finland_veikkausliiga": "芬超",
            "uefa_champions_league": "欧冠",
            "uefa_nations_league": "欧国联",
            "afc_champions_league": "亚冠",
        }
        for league_key, display in expected.items():
            with self.subTest(league_key=league_key):
                self.assertEqual(
                    formatter.league_display_name(
                        {"league": league_key, "league_key": league_key}
                    ),
                    display,
                )

    def test_unknown_ascii_league_key_never_leaks_to_user_output(self):
        for league_key in ("unknown_ascii_league", "brazil_serie_b", "some-league"):
            with self.subTest(league_key=league_key):
                display = formatter.league_display_name(
                    {"league": league_key, "league_key": league_key}
                )
                self.assertEqual(display, "赛事待核验")
                self.assertNotIn(league_key, display)

    def test_unsafe_chinese_league_label_never_leaks_to_user_output(self):
        for label in (
            "主推大2.5",
            "大2.5赛事",
            "角球大10.5 @0.99",
            "伪赛事…",
            "超" * 40,
        ):
            with self.subTest(label=label):
                self.assertEqual(
                    formatter.league_display_name(
                        {"league": label, "league_key": "custom"}
                    ),
                    "赛事待核验",
                )

    def test_market_shape_filter_does_not_block_ordinary_team_or_age_text(self):
        for label in ("大阪钢巴", "大田韩亚市民", "青年U21联赛"):
            with self.subTest(label=label):
                self.assertEqual(
                    formatter.league_display_name(
                        {"league": label, "league_key": "custom"}
                    ),
                    label,
                )

    def test_brazil_cup_stage_label_renders_as_chinese_competition(self):
        self.assertEqual(
            formatter.league_display_name({"league": "2026巴西杯16强次回合"}),
            "巴西杯",
        )

    def test_efl_cup_stage_label_renders_as_chinese_competition(self):
        self.assertEqual(
            formatter.league_display_name({"league": "2025英联杯半决赛次回合"}),
            "英联杯",
        )

    def test_verified_competition_evidence_overrides_proxy_model_league(self):
        record = base_record()
        record.update(
            {
                "league": "brazil_serie_a",
                "league_key": "brazil_serie_a",
                "source_url": "https://zq.titan007.com/analysis/42cn.htm",
            }
        )
        record["competition_evidence"] = (
            formatter.memory_store.build_competition_evidence(
                record,
                competition_key="brazil_cup",
                competition_label="巴西杯",
                competition_id="186",
                verification_source=record["source_url"],
                source_locator="//info.titan007.com/cup_match/2026-2027/cupmatch_vs/cupmatch_186.htm",
                collected_at="2026-07-22T18:00:00+09:00",
                _source_snapshot=competition_snapshot(
                    record, "2026-07-22T18:00:00+09:00"
                ),
            )
        )
        self.assertEqual(formatter.league_display_name(record), "巴西杯")

    def test_initial_plain_text_is_complete(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [base_record()])
            text = formatter.render(base, "42", "initial")
            self.assertTrue(text.startswith("【初盘分析｜42】\n"))
            for field in (
                "赛事：芬超",
                "比赛：主队 vs 客队",
                "开赛：",
                "主推：小2.5 @0.92",
                "次选参考：",
                "胜平负：数据不足",
                "联合情景：数据不足",
            ):
                self.assertIn(field, text)
            self.assertNotIn("比分参考：", text)
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

    def test_legacy_independent_scores_never_leak_into_prematch_text(self):
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
            self.assertIn("联合情景：数据不足", text)
            self.assertNotIn("2-1（全场9.9%", text)
            self.assertNotIn("比分参考：", text)
            self.assert_plain(text)

    def test_every_descriptive_field_comes_from_one_validated_joint_artifact(self):
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [base_record()])
            with patch.object(
                formatter.memory_store,
                "validated_joint_scenario_audit",
                return_value=joint_artifact(),
            ):
                text = formatter.render(base, "42", "initial")

            self.assertIn(
                "半场倾向：平44.0% / 主胜31.0%（较明确，前二差13.0个百分点）",
                text,
            )
            self.assertIn(
                "胜平负：客胜46.0% / 主胜29.0%（较明确，前二差17.0个百分点）",
                text,
            )
            self.assertIn(
                "联合首选情景总球：2-3球（冻结联合第1名比分映射）｜"
                "总进球边际第一：2-3球 41.0%"
                "（边际分布第一，仅审计，不替代联合首选情景）",
                text,
            )
            self.assertIn(
                "双方进球：是62.0% / 否38.0%（较明确，前二差24.0个百分点）",
                text,
            )
            self.assertIn(
                "联合情景：联合事件 Top 2（半全场＋波胆逐行同源，"
                "按联合概率排序，高方差，不作推荐）："
                "平平 + 1-1 5.8% / 负负 + 1-2 4.5%",
                text,
            )
            self.assertIn(
                "Top2累计10.3%｜其他情景89.7%｜不确定度高（归一化熵98.6%，政策v1）",
                text,
            )
            self.assertIn("纯模型（未混入过期盘口）", text)
            self.assertNotIn("比分参考：", text)
            self.assertNotIn("半全场：", text)
            self.assert_plain(text)

    def test_no_primary_plain_text_does_not_expose_a_marginal_model_leader(self):
        record = base_record()
        record["primary_market"] = None
        record["primary_pick"] = None
        record["total_pick"] = None
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            with patch.object(
                formatter.memory_store,
                "validated_joint_scenario_audit",
                return_value=joint_artifact(),
            ):
                text = formatter.render(base, "42", "initial")

        self.assertIn("主推：无正式推荐", text)
        self.assertNotIn("◇ 模型首选", text)
        self.assert_plain(text)

    def test_qualified_observation_precedes_joint_model_leader_in_plain_text(self):
        record = base_record()
        record["primary_market"] = None
        record["primary_pick"] = None
        record["total_pick"] = None
        record["candidate_audits"] = [
            {
                "kind": formatter.memory_store.CORNER_OBSERVATION_KIND,
                "best_observation": {
                    "market": "corner_total",
                    "side": "over",
                    "line": 9.5,
                    "odds": 0.91,
                    "diagnostic_qualification_status": "qualified",
                },
            }
        ]
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            with (
                patch.object(
                    formatter.memory_store,
                    "validated_joint_scenario_audit",
                    return_value=joint_artifact(),
                ),
                patch.object(
                    formatter.memory_store,
                    "validated_observation_audit",
                    return_value=True,
                ),
            ):
                text = formatter.render(base, "42", "initial")

        self.assertIn(
            "◇ 观察方向：角球大9.5 @0.91（不计主推、不计战绩）",
            text,
        )
        self.assertNotIn("◇ 模型首选", text)
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
        record.update(
            {
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
            }
        )
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

            record["revisions"] = [
                {
                    key: record.get(key)
                    for key in (
                        "analysis_stage",
                        "recommendation",
                        "notes",
                        "predicted_score",
                        "exact_score_picks",
                        "zero_zero_audit",
                        "asian_pick",
                        "total_pick",
                        "half_time_pick",
                        "htft_picks",
                        "goal_range_pick",
                        "btts_pick",
                        "corner_total_pick",
                        "corner_handicap_pick",
                        "primary_market",
                        "primary_pick",
                        "primary_change",
                    )
                }
            ]
            corner_handicap = {
                "side": "home",
                "line": -1.5,
                "odds": 0.95,
                "probability": 0.62,
                "ev": 0.209,
                "role": "primary",
            }
            record.update(
                {
                    "analysis_stage": "lineup-check",
                    "lineup_rechecked_at": "2026-07-23T10:02:00+00:00",
                    "corner_handicap_pick": dict(corner_handicap),
                    "primary_market": "corner_handicap",
                    "primary_pick": dict(corner_handicap),
                    "primary_change": {"status": "changed"},
                }
            )
            write_history(base, [record])
            lineup_text = formatter.render(base, "42", "lineup-check")
            self.assertIn(
                "主推变更：总进球2-3球 @2.10 → 主队角球-1.5 @0.95",
                lineup_text,
            )
            self.assertIn("当前主推：主队角球-1.5 @0.95", lineup_text)
            self.assert_plain(lineup_text)

    def test_initial_text_uses_its_frozen_fixture_not_later_lineup_metadata(self):
        record = base_record()
        initial = copy.deepcopy(record)
        initial.update(
            {
                "analysis_stage": "initial",
                "league": "finland_veikkausliiga",
                "league_key": "finland_veikkausliiga",
                "home_team": "初盘主队",
                "away_team": "初盘客队",
                "kickoff": "2026-07-23T12:00:00+09:00",
                "revisions": [],
            }
        )
        record.update(
            {
                "analysis_stage": "lineup-check",
                "league": "england_premier_league",
                "league_key": "england_premier_league",
                "home_team": "临场主队",
                "away_team": "临场客队",
                "kickoff": "2026-07-23T13:00:00+09:00",
                "lineup_rechecked_at": "2026-07-23T12:35:00+09:00",
                "primary_change": {"status": "maintained"},
                "revisions": [initial],
            }
        )

        text = formatter.render_initial(record)

        self.assertIn("赛事：芬超", text)
        self.assertIn("比赛：初盘主队 vs 初盘客队", text)
        self.assertIn("开赛：2026-07-23 12:00（日本时间）", text)
        self.assertNotIn("英超", text)
        self.assertNotIn("临场主队", text)
        self.assertNotIn("13:00（日本时间）", text)

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
            self.assertIn("联合情景：数据不足", text)
            self.assertNotIn("比分参考：0-0", text)
            self.assertNotIn("0-0核验：", text)
            self.assert_plain(text)

    def test_free_text_cannot_bypass_structured_prediction_gates(self):
        record = base_record()
        record["recommendation"] = "R" * 220
        record["notes"] = "N" * 500
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "initial")
            self.assertNotIn(record["recommendation"], text)
            self.assertNotIn(record["notes"], text)
            self.assertIn("模型说明：正式主推来自归档门控", text)
            self.assertIn("证据状态：数据质量未知", text)
            self.assert_plain(text)

    def test_hidden_zero_zero_audit_does_not_leak_through_user_facing_prose(self):
        record = base_record()
        record["recommendation"] = (
            "小球方向更稳；0-0核验未进前二。赔率12，EV44%。低节奏判断保留。"
        )
        record["notes"] = (
            "阵容仍有不确定性；概率12.0%，全分布第4。对应比分0-0。赔率12，EV44%。其他风险保留。"
        )
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "initial")
            self.assertIn("模型说明：正式主推来自归档门控", text)
            self.assertIn("证据状态：数据质量未知", text)
            for hidden in (
                "小球方向更稳",
                "低节奏判断保留",
                "阵容仍有不确定性",
                "其他风险保留",
                "0-0核验",
                "对应比分0-0",
                "概率12.0%",
                "赔率12",
                "EV44%",
            ):
                self.assertNotIn(hidden, text)
            self.assert_plain(text)

    def test_lineup_plain_text_states_change_and_active_primary(self):
        record = base_record()
        record["revisions"] = [
            {
                key: record.get(key)
                for key in (
                    "analysis_stage",
                    "recommendation",
                    "notes",
                    "predicted_score",
                    "exact_score_picks",
                    "zero_zero_audit",
                    "asian_pick",
                    "total_pick",
                    "half_time_pick",
                    "htft_picks",
                    "primary_market",
                    "primary_pick",
                    "primary_change",
                )
            }
        ]
        record.update(
            {
                "analysis_stage": "lineup-check",
                "lineup_rechecked_at": "2026-07-23T10:02:00+00:00",
                "primary_market": "asian",
                "primary_pick": dict(
                    record["asian_pick"], market="asian", role="primary"
                ),
                "primary_change": {"status": "changed"},
            }
        )
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
        record["revisions"] = [
            {
                key: record.get(key)
                for key in (
                    "analysis_stage",
                    "recommendation",
                    "notes",
                    "predicted_score",
                    "exact_score_picks",
                    "zero_zero_audit",
                    "asian_pick",
                    "total_pick",
                    "half_time_pick",
                    "htft_picks",
                    "primary_market",
                    "primary_pick",
                    "primary_change",
                )
            }
        ]
        record.update(
            {
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
                "candidate_audits": [
                    {
                        "market": "htft",
                        "pair_probability_mass": 0.50,
                        "top_two": [
                            {
                                "selection": "HH",
                                "probability": 0.30,
                                "odds": None,
                                "gates": [
                                    {
                                        "gate": "market_policy_enabled",
                                        "passed": False,
                                        "reasons": ["paused"],
                                    },
                                    {
                                        "gate": "complete_current_market",
                                        "passed": False,
                                        "reasons": ["missing odds"],
                                    },
                                ],
                            },
                            {
                                "selection": "DH",
                                "probability": 0.20,
                                "odds": None,
                                "gates": [
                                    {
                                        "gate": "market_policy_enabled",
                                        "passed": False,
                                        "reasons": ["paused"],
                                    },
                                    {
                                        "gate": "complete_current_market",
                                        "passed": False,
                                        "reasons": ["missing odds"],
                                    },
                                ],
                            },
                        ],
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            lineup_text = formatter.render(base, "42", "lineup-check")
            self.assertIn("主推取消：小2.5 @0.92 → 不下注", lineup_text)
            self.assertIn("当前主推：无正式推荐", lineup_text)
            self.assertIn("联合情景：数据不足", lineup_text)
            self.assertNotIn("半全场：观察", lineup_text)
            self.assert_plain(lineup_text)

            record.update(
                {
                    "status": "reviewed",
                    "half_time_score": "0-0",
                    "final_score": "1-0",
                    "primary_result": None,
                    "key_learning": "旧方向失效后没有强行寻找替代主推",
                    "reviewed_at": "2026-07-23T13:00:00+00:00",
                    "observation_diagnostics": [
                        {
                            "market": "htft",
                            "status": "graded_observation",
                            "actual_selection": "DH",
                            "top1_hit": False,
                            "top2_hit": True,
                        }
                    ],
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
                }
            )
            write_history(base, [record])
            with patch.object(
                formatter.memory_store,
                "validated_joint_scenario_audit",
                return_value=joint_artifact(),
            ):
                review_text = formatter.render(base, "42", "review")
            self.assertIn("主推：无正式推荐（不结算、不计战绩）", review_text)
            self.assertIn(
                "学习归档：无主推观察样本（只用于规则与数据质量复核）",
                review_text,
            )
            self.assertIn(
                "联合情景：联合事件 Top 2（半全场＋波胆逐行同源，"
                "按联合概率排序，高方差，不作推荐）："
                "平平 + 1-1 5.8% / 负负 + 1-2 4.5%",
                review_text,
            )
            self.assertIn("总进球边际第一：2-3球 41.0%", review_text)
            self.assertIn("Top2累计10.3%｜其他情景89.7%", review_text)
            self.assertNotIn("半全场观察诊断", review_text)
            self.assertNotIn("比分参考：", review_text)
            self.assertNotIn("命中排名：", review_text)
            self.assertNotIn("无正式推荐＝未结算", review_text)
            self.assert_plain(review_text)

    def test_review_plain_text_uses_settlement_basis_and_statistics(self):
        record = base_record()
        record.update(
            {
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
                    "source_url": record.get("source_url"),
                    "league": record["league"],
                    "league_key": record["league_key"],
                    "competition_evidence": None,
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
            }
        )
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "review")
            self.assertTrue(text.startswith("【赛后复盘｜芬超｜42】\n"))
            self.assertIn("结算依据：临场版最终有效推荐", text)
            self.assertIn("主推：小2.5 @0.92＝红", text)
            self.assertIn(
                "次选参考：客队 +0.25 @0.86（不结算、不计战绩、不计金额）", text
            )
            self.assertNotIn("客队 +0.25 @0.86＝", text)
            self.assertIn("芬超主推：1场1胜0负0走", text)
            self.assertIn("累计主推：1场1胜0负0走", text)
            self.assertIn("本场关键：临场低节奏判断得到验证；保留小样本观察。", text)
            self.assertNotIn("赔率12", text)
            self.assertNotIn("0-0核验：", text)
            self.assert_plain(text)

    def test_review_plain_text_lists_all_expanded_secondary_markets_without_results(
        self,
    ):
        record = base_record()
        record.update(
            {
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
            }
        )
        with tempfile.TemporaryDirectory() as base:
            write_history(base, [record])
            text = formatter.render(base, "42", "review")
            self.assertIn("总进球2-3球 @2.10", text)
            self.assertIn("双方进球-是 @1.80", text)
            self.assertIn("角球大10.5 @0.90", text)
            self.assertIn("主队角球-1.5 @0.95", text)
            self.assertIn("（不结算、不计战绩、不计金额）", text)
            for market_text in (
                "总进球2-3球 @2.10",
                "双方进球-是 @1.80",
                "角球大10.5 @0.90",
                "主队角球-1.5 @0.95",
            ):
                self.assertNotIn(f"{market_text}＝", text)
            self.assert_plain(text)


if __name__ == "__main__":
    unittest.main()

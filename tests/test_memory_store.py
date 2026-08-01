from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_store.py"
SPEC = importlib.util.spec_from_file_location("soccer_memory_store", SCRIPT)
assert SPEC and SPEC.loader
memory_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(memory_store)


def record_args(base_dir: str, match_id: str = "1", **overrides):
    values = {
        "base_dir": base_dir,
        "match_id": match_id,
        "analysis_stage": "initial",
        "league": "测试联赛",
        "kickoff": "2026-07-21T19:30:00+09:00",
        "home_team": "主队",
        "away_team": "客队",
        "predicted_score": "1-0",
        "exact_score_pick": ["1-0:0.20", "2-0:0.15"],
        "zero_zero_probability": 0.10,
        "zero_zero_rank": 4,
        "zero_zero_odds": None,
        "zero_zero_ev": None,
        "recommendation": "测试",
        "source_url": "https://example.test/match",
        "notes": "",
        "data_quality": "medium",
        "lineup_confirmed": True,
        "fundamental_evidence": True,
        "chance_quality_evidence": True,
        "attack_configuration_evidence": True,
        "corner_profile_evidence": True,
        "opponent_tail_risk_checked": True,
        "injury_evidence_status": "fresh",
        "primary_change_reason": "",
        "previous_primary_invalidated": False,
        "previous_primary_current_ev": None,
        "previous_primary_current_confidence": None,
        "accept_worse_line": False,
        "primary_htft_edge_pp": None,
        "primary_htft_firm_count": None,
        "home_win_probability": 0.5,
        "draw_probability": 0.25,
        "away_win_probability": 0.25,
        "primary_market": "total",
        "primary_htft_selection": None,
        "asian_side": "home",
        "asian_line": -0.25,
        "asian_odds": 0.9,
        "asian_odds_format": None,
        "asian_probability": 0.54,
        "asian_ev": 0.09,
        "asian_edge_pp": 4.5,
        "asian_firm_count": 8,
        "asian_cover_probability": 0.55,
        "asian_cover_distribution_validated": True,
        "asian_market_signal": "aligned",
        "total_side": "under",
        "total_line": 2.5,
        "total_odds": 0.9,
        "total_odds_format": None,
        "total_probability": 0.55,
        "total_ev": 0.09,
        "total_edge_pp": 4.5,
        "total_firm_count": 8,
        "total_market_signal": "aligned",
        "half_market": None,
        "half_side": None,
        "half_line": None,
        "half_odds": None,
        "half_odds_format": None,
        "half_probability": None,
        "half_ev": None,
        "half_edge_pp": None,
        "half_firm_count": None,
        "half_market_signal": "unknown",
        "htft_pick": None,
        "htft_odds_format": None,
        "goal_range_selection": None,
        "goal_range_odds": None,
        "goal_range_odds_format": None,
        "goal_range_probability": None,
        "goal_range_ev": None,
        "goal_range_edge_pp": None,
        "goal_range_firm_count": None,
        "goal_range_market_signal": "unknown",
        "goal_range_market_complete": False,
        "goal_range_market_probability": 0.53,
        "goal_range_market_source": "https://example.test/goal-range",
        "goal_range_market_collected_at": "2026-07-21T19:00:00+09:00",
        "goal_range_price_basis": "consensus",
        "btts_side": None,
        "btts_odds": None,
        "btts_odds_format": None,
        "btts_probability": None,
        "btts_ev": None,
        "btts_edge_pp": None,
        "btts_firm_count": None,
        "btts_market_signal": "unknown",
        "btts_market_complete": False,
        "btts_market_probability": 0.52,
        "btts_market_source": "https://example.test/btts",
        "btts_market_collected_at": "2026-07-21T19:00:00+09:00",
        "btts_price_basis": "median",
        "corner_total_side": None,
        "corner_total_line": None,
        "corner_total_odds": None,
        "corner_total_odds_format": None,
        "corner_total_probability": None,
        "corner_total_ev": None,
        "corner_total_edge_pp": None,
        "corner_total_firm_count": None,
        "corner_total_market_signal": "unknown",
        "corner_total_market_complete": False,
        "corner_total_market_probability": 0.515,
        "corner_total_market_source": "https://example.test/corner-total",
        "corner_total_market_collected_at": "2026-07-21T19:00:00+09:00",
        "corner_total_price_basis": "consensus",
        "corner_total_full_win_probability": 0.56,
        "corner_total_half_win_probability": 0.0,
        "corner_total_push_probability": 0.0,
        "corner_total_half_loss_probability": 0.0,
        "corner_total_loss_probability": 0.44,
        "corner_handicap_side": None,
        "corner_handicap_line": None,
        "corner_handicap_odds": None,
        "corner_handicap_odds_format": None,
        "corner_handicap_probability": None,
        "corner_handicap_ev": None,
        "corner_handicap_edge_pp": None,
        "corner_handicap_firm_count": None,
        "corner_handicap_market_signal": "unknown",
        "corner_handicap_market_complete": False,
        "corner_handicap_market_probability": 0.515,
        "corner_handicap_market_source": "https://example.test/corner-handicap",
        "corner_handicap_market_collected_at": "2026-07-21T19:00:00+09:00",
        "corner_handicap_price_basis": "median",
        "corner_handicap_full_win_probability": 0.56,
        "corner_handicap_half_win_probability": 0.0,
        "corner_handicap_push_probability": 0.10,
        "corner_handicap_half_loss_probability": 0.0,
        "corner_handicap_loss_probability": 0.34,
        "force": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def reviewed_record(match_id, asian=None, asian_result=None, total=None, total_result=None, half=None, half_result=None):
    return {
        "match_id": match_id,
        "mode": "prematch",
        "status": "reviewed",
        "league": "测试联赛",
        "revisions": [{"analysis_stage": "initial", "sentinel": match_id}],
        "asian_pick": asian,
        "asian_result": asian_result,
        "total_pick": total,
        "total_result": total_result,
        "half_time_pick": half,
        "half_time_result": half_result,
        "htft_picks": [],
        "htft_results": [],
        "key_learning": "具体学习",
    }


class MemoryStoreTests(unittest.TestCase):
    def test_two_exact_scores_are_ranked_archived_and_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as base:
            created = memory_store.cmd_record(
                record_args(base, exact_score_pick=["2-0:0.15", "1-0:0.20"])
            )["record"]
            self.assertEqual(
                [(pick["rank"], pick["score"]) for pick in created["exact_score_picks"]],
                [(1, "1-0"), (2, "2-0")],
            )
            self.assertEqual(created["league_key"], "测试联赛")
            self.assertTrue(all(pick["status"] == "scenario_only" for pick in created["exact_score_picks"]))

            reviewed = memory_store.cmd_review(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    match_id="1",
                    home_score=2,
                    away_score=0,
                    half_home_score=1,
                    half_away_score=0,
                    key_learning="第二波胆覆盖了主队扩大优势的比赛形态",
                )
            )
            self.assertFalse(reviewed["record"]["score_exact"])
            self.assertEqual(reviewed["record"]["exact_score_hit_rank"], 2)
            self.assertTrue(reviewed["record"]["exact_score_any_hit"])
            self.assertEqual(reviewed["stats"]["exact_score_top1_hits"], 0)
            self.assertEqual(reviewed["stats"]["exact_score_top2_hits"], 1)
            self.assertEqual(reviewed["stats"]["primary"]["matches"], 1)

        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "exactly two"):
                memory_store.cmd_record(record_args(base, exact_score_pick=["1-0:0.20"]))
            with self.assertRaisesRegex(ValueError, "highest-probability"):
                memory_store.cmd_record(
                    record_args(base, predicted_score="2-0", exact_score_pick=["1-0:0.20", "2-0:0.15"])
                )

    def test_primary_conditioned_display_scores_are_separate_and_reviewed(self):
        with tempfile.TemporaryDirectory() as base:
            created = memory_store.cmd_record(
                record_args(
                    base,
                    predicted_score="1-1",
                    exact_score_pick=["1-1:0.20", "1-0:0.15"],
                    zero_zero_probability=0.10,
                    zero_zero_rank=4,
                    total_side="over",
                    total_line=2.5,
                    display_exact_score_pick=[
                        "2-1:0.14:3",
                        "3-1:0.10:5",
                    ],
                    display_exact_score_event_probability=0.55,
                )
            )["record"]

            self.assertEqual(
                [pick["score"] for pick in created["exact_score_picks"]],
                ["1-1", "1-0"],
            )
            self.assertEqual(
                [pick["score"] for pick in created["display_exact_score_picks"]],
                ["2-1", "3-1"],
            )
            self.assertEqual(created["display_predicted_score"], "2-1")
            self.assertEqual(
                created["display_exact_score_basis"]["basis"],
                "primary_total_net_profit",
            )
            self.assertAlmostEqual(
                created["display_exact_score_picks"][0]["conditional_probability"],
                0.14 / 0.55,
            )

            reviewed = memory_store.cmd_review(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    match_id="1",
                    home_score=2,
                    away_score=1,
                    half_home_score=1,
                    half_away_score=0,
                    home_corners=None,
                    away_corners=None,
                    key_learning="主推成立时的条件波胆第一项命中",
                )
            )
            self.assertIsNone(reviewed["record"]["exact_score_hit_rank"])
            self.assertEqual(reviewed["record"]["display_exact_score_hit_rank"], 1)
            self.assertEqual(reviewed["stats"]["exact_score_top1_hits"], 0)
            self.assertEqual(reviewed["stats"]["display_exact_score_top1_hits"], 1)

        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "does not support"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        predicted_score="1-1",
                        exact_score_pick=["1-1:0.20", "1-0:0.15"],
                        zero_zero_probability=0.10,
                        zero_zero_rank=4,
                        total_side="over",
                        total_line=2.5,
                        display_exact_score_pick=[
                            "1-1:0.20:1",
                            "2-1:0.14:3",
                        ],
                        display_exact_score_event_probability=0.55,
                    )
                )

    def test_zero_zero_audit_is_required_consistent_and_revisioned(self):
        with tempfile.TemporaryDirectory() as base:
            top_one = memory_store.cmd_record(
                record_args(
                    base,
                    predicted_score="0-0",
                    exact_score_pick=["1-0:0.20", "0-0:0.30"],
                    zero_zero_probability=0.30,
                    zero_zero_rank=1,
                    zero_zero_odds=12.0,
                    zero_zero_ev=2.60,
                )
            )["record"]
            self.assertEqual(top_one["exact_score_picks"][0]["score"], "0-0")
            self.assertEqual(
                top_one["zero_zero_audit"],
                {
                    "score": "0-0",
                    "probability": 0.30,
                    "rank": 1,
                    "included_in_top2": True,
                    "included_in_display_top2": True,
                    "status": "top_two",
                    "odds": 12.0,
                    "ev": 2.60,
                },
            )

            lineup = memory_store.cmd_record(
                record_args(
                    base,
                    analysis_stage="lineup-check",
                    zero_zero_probability=0.11,
                    zero_zero_rank=4,
                )
            )["record"]
            self.assertEqual(lineup["zero_zero_audit"]["rank"], 4)
            self.assertEqual(
                lineup["revisions"][-1]["zero_zero_audit"]["rank"],
                1,
            )

        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "requires --zero-zero"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        zero_zero_probability=None,
                        zero_zero_rank=None,
                    )
                )
            with self.assertRaisesRegex(ValueError, "must be an exact-score pick"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        zero_zero_probability=0.18,
                        zero_zero_rank=2,
                    )
                )
            with self.assertRaisesRegex(ValueError, "exceeds the archived second-ranked"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        zero_zero_probability=0.18,
                        zero_zero_rank=3,
                    )
                )

    def test_unique_primary_and_lineup_change(self):
        with tempfile.TemporaryDirectory() as base:
            initial = memory_store.cmd_record(record_args(base))
            self.assertEqual(initial["record"]["primary_market"], "total")
            self.assertEqual(initial["record"]["total_pick"]["role"], "primary")
            self.assertEqual(initial["record"]["asian_pick"]["role"], "secondary")

            maintained = memory_store.cmd_record(
                record_args(base, analysis_stage="lineup-check", total_odds=0.86)
            )
            self.assertEqual(maintained["record"]["primary_change"]["status"], "maintained")

            changed = memory_store.cmd_record(
                record_args(
                    base,
                    analysis_stage="lineup-check",
                    primary_market="asian",
                    asian_odds=0.95,
                    asian_probability=0.60,
                    data_quality="high",
                    primary_change_reason="确认首发直接否定原大小球逻辑",
                    previous_primary_invalidated=True,
                    previous_primary_current_ev=0.04,
                )
            )
            self.assertEqual(changed["record"]["primary_change"]["status"], "changed")
            self.assertEqual(changed["record"]["asian_pick"]["role"], "primary")
            self.assertEqual(changed["record"]["total_pick"]["role"], "secondary")
            self.assertGreaterEqual(len(changed["record"]["revisions"]), 2)

            with self.assertRaisesRegex(ValueError, "valid only when there are no formal picks"):
                memory_store.cmd_record(record_args(base, match_id="2", primary_market="none"))
            with self.assertRaisesRegex(ValueError, "is not present"):
                memory_store.cmd_record(record_args(base, match_id="3", primary_market="half_time"))

    def test_review_persists_primary_result(self):
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(record_args(base, asian_side=None, primary_market="total"))
            result = memory_store.cmd_review(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    match_id="1",
                    home_score=0,
                    away_score=0,
                    half_home_score=0,
                    half_away_score=0,
                    key_learning="低节奏假设得到验证",
                )
            )
            self.assertEqual(result["record"]["primary_result"], "win")
            self.assertIsNone(result["record"]["asian_result"])
            self.assertEqual(result["record"]["total_result"], "win")
            self.assertEqual(result["record"]["settlement_basis"]["grading_scope"], "primary_only")
            self.assertEqual(result["record"]["settlement_basis"]["analysis_stage"], "initial")
            self.assertEqual(
                result["record"]["settlement_basis"]["policy"],
                "latest_active_prematch_version",
            )
            self.assertEqual(result["league_key"], "测试联赛")
            self.assertEqual(result["league_stats"]["reviewed_matches"], 1)

    def test_review_settles_lineup_check_instead_of_initial_revision(self):
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(
                    base,
                    asian_side=None,
                    primary_market="total",
                    total_side="under",
                    total_line=2.5,
                )
            )
            lineup = memory_store.cmd_record(
                record_args(
                    base,
                    analysis_stage="lineup-check",
                    asian_side=None,
                    primary_market="total",
                    total_side="over",
                    total_line=2.5,
                    total_odds=0.92,
                    data_quality="high",
                    primary_change_reason="确认首发提升进攻配置并否定原小球逻辑",
                    previous_primary_invalidated=True,
                    previous_primary_current_ev=0.04,
                )
            )["record"]
            self.assertEqual(lineup["total_pick"]["side"], "over")
            self.assertEqual(lineup["revisions"][-1]["total_pick"]["side"], "under")

            reviewed = memory_store.cmd_review(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    match_id="1",
                    home_score=3,
                    away_score=0,
                    half_home_score=1,
                    half_away_score=0,
                    key_learning="临场升盘后的大球方向得到验证",
                )
            )
            record = reviewed["record"]
            self.assertEqual(record["total_result"], "win")
            self.assertEqual(record["primary_result"], "win")
            self.assertEqual(record["settlement_basis"]["analysis_stage"], "lineup-check")
            self.assertEqual(
                record["settlement_basis"]["formal_picks"]["total"]["side"],
                "over",
            )
            self.assertEqual(reviewed["stats"]["primary"]["wins"], 1)

    def test_settlement_basis_migration_preserves_results_and_revisions(self):
        total = {
            "side": "under",
            "line": 2.5,
            "odds": 0.88,
            "ev": 0.05,
            "market_signal": "aligned",
        }
        record = reviewed_record("201", total=total, total_result="win")
        record.update({
            "analysis_stage": "lineup-check",
            "lineup_rechecked_at": "2026-07-21T10:00:00+00:00",
            "updated_at": "2026-07-21T10:00:00+00:00",
            "primary_market": "total",
            "primary_pick": dict(total, market="total", role="primary"),
            "primary_result": "win",
            "final_score": "0-0",
        })
        with tempfile.TemporaryDirectory() as base:
            path = memory_store.data_path(base)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")
            before = memory_store.load_history(path)[0]
            migrated = memory_store.cmd_migrate_settlement_basis(
                SimpleNamespace(base_dir=base, write=True)
            )
            self.assertEqual(migrated["changed_match_ids"], ["201"])
            saved = memory_store.load_history(path)[0]
            self.assertEqual(saved["settlement_basis"]["analysis_stage"], "lineup-check")
            self.assertEqual(saved["primary_result"], before["primary_result"])
            self.assertEqual(saved["total_result"], before["total_result"])
            self.assertEqual(saved["revisions"], before["revisions"])

    def test_league_normalization_grouped_stats_migration_and_calibration(self):
        self.assertEqual(memory_store.normalize_league_name("2026芬超第16轮"), "芬超")
        self.assertEqual(memory_store.normalize_league_name("韩K联 第19轮"), "韩K联")
        self.assertEqual(memory_store.normalize_league_name("2026世界杯决赛"), "世界杯")

        total_win = {
            "side": "over",
            "line": 2.5,
            "odds": 0.90,
            "ev": 0.06,
            "market_signal": "aligned",
        }
        total_loss = {
            "side": "under",
            "line": 2.5,
            "odds": 0.88,
            "ev": 0.05,
            "market_signal": "neutral",
        }
        first = reviewed_record("101", total=total_win, total_result="win")
        first.update({
            "league": "2026芬超第16轮",
            "primary_market": "total",
            "primary_pick": dict(total_win, market="total", role="primary"),
            "primary_result": "win",
        })
        second = reviewed_record("102", total=total_loss, total_result="loss")
        second.update({
            "league": "芬超",
            "primary_market": "total",
            "primary_pick": dict(total_loss, market="total", role="primary"),
            "primary_result": "loss",
        })
        history = [first, second]

        stats = memory_store.calculate_stats(history)
        self.assertEqual(list(stats["leagues"]), ["芬超"])
        league = stats["leagues"]["芬超"]
        self.assertEqual(league["source_labels"], ["2026芬超第16轮", "芬超"])
        self.assertEqual(league["reviewed_matches"], 2)
        self.assertEqual(league["primary"]["wins"], 1)
        self.assertEqual(league["primary"]["losses"], 1)
        self.assertEqual(league["primary_by_market"]["combined"]["matches"], 2)
        self.assertEqual(len(league["recent_learnings"]), 2)
        self.assertEqual(
            {item["learning_scope"] for item in league["recent_learnings"]},
            {"primary"},
        )
        self.assertTrue(
            all(
                item["counts_toward_primary_record"]
                for item in league["recent_learnings"]
            )
        )

        with tempfile.TemporaryDirectory() as base:
            path = memory_store.data_path(base)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
            revisions_before = {item["match_id"]: item["revisions"] for item in history}
            migrated = memory_store.cmd_migrate_leagues(
                SimpleNamespace(base_dir=base, write=True)
            )
            self.assertEqual(migrated["changed_match_ids"], ["101", "102"])
            saved = memory_store.load_history(path)
            self.assertTrue(all(item["league_key"] == "芬超" for item in saved))
            self.assertEqual(
                {item["match_id"]: item["revisions"] for item in saved},
                revisions_before,
            )

            calibration = memory_store.cmd_calibrate(
                SimpleNamespace(base_dir=base, guardrail=None, minimum_graded=20, write=True)
            )["calibration"]
            profile = calibration["league_profiles"]["芬超"]
            self.assertEqual(profile["sample_tier"], "anecdotal")
            self.assertEqual(profile["decision"], "hold_weights_insufficient_league_sample")
            self.assertEqual(profile["active_weight_adjustments"], {})
            self.assertIn("按1个联赛归类", calibration["summary"])

    def test_learning_scope_migration_backfills_old_records_without_regrading(self):
        primary = reviewed_record(
            "primary-learning",
            total={
                "side": "over",
                "line": 2.5,
                "odds": 0.9,
                "ev": 0.08,
                "role": "primary",
            },
            total_result="win",
        )
        primary.update({
            "primary_market": "total",
            "primary_pick": {
                "market": "total",
                "side": "over",
                "line": 2.5,
                "odds": 0.9,
                "ev": 0.08,
                "role": "primary",
            },
            "primary_result": "win",
        })
        no_primary = reviewed_record("no-primary-learning")
        no_primary.update({
            "primary_market": None,
            "primary_pick": None,
            "primary_result": None,
            "settlement_basis": {
                "policy": "latest_active_prematch_version",
                "analysis_stage": "lineup-check",
                "primary_market": None,
                "primary_pick": None,
                "formal_picks": {},
            },
        })
        history = [primary, no_primary]
        with tempfile.TemporaryDirectory() as base:
            path = memory_store.data_path(base)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(history, ensure_ascii=False),
                encoding="utf-8",
            )
            revisions_before = {
                record["match_id"]: record["revisions"] for record in history
            }
            migrated = memory_store.cmd_migrate_learning_scopes(
                SimpleNamespace(base_dir=base, write=True)
            )

            self.assertEqual(
                migrated["changed_match_ids"],
                ["primary-learning", "no-primary-learning"],
            )
            self.assertEqual(migrated["missing_learning_match_ids"], [])
            self.assertEqual(migrated["stats"]["reviewed_matches"], 2)
            self.assertEqual(migrated["stats"]["primary"]["matches"], 1)
            self.assertEqual(
                migrated["stats"]["learning_samples"],
                {
                    "total": 2,
                    "primary": 1,
                    "no_primary_observation": 1,
                },
            )
            saved = memory_store.load_history(path)
            by_id = {record["match_id"]: record for record in saved}
            self.assertEqual(
                by_id["primary-learning"]["learning_scope"], "primary"
            )
            self.assertTrue(
                by_id["primary-learning"]["counts_toward_primary_record"]
            )
            self.assertEqual(
                by_id["no-primary-learning"]["learning_scope"],
                "no_primary_observation",
            )
            self.assertFalse(
                by_id["no-primary-learning"]["counts_toward_primary_record"]
            )
            self.assertEqual(
                {record["match_id"]: record["revisions"] for record in saved},
                revisions_before,
            )
            calibration = memory_store.cmd_calibrate(
                SimpleNamespace(
                    base_dir=base,
                    guardrail=None,
                    minimum_graded=20,
                    write=False,
                )
            )["calibration"]
            self.assertEqual(calibration["primary_record_matches"], 1)
            self.assertEqual(calibration["no_primary_reviewed_matches"], 1)
            self.assertEqual(
                calibration["learning_samples"]["no_primary_observation"], 1
            )
            self.assertIn(
                "无主推1场不计战绩并作为学习样本",
                calibration["summary"],
            )

    def test_lineup_check_is_not_due_before_t_minus_30(self):
        with tempfile.TemporaryDirectory() as base:
            defaults = memory_store.build_parser().parse_args(["due-lineup-check"])
            self.assertEqual((defaults.min_minutes, defaults.max_minutes), (0.0, 30.0))
            memory_store.cmd_record(record_args(base, asian_side=None, primary_market="total"))
            early = memory_store.cmd_due_lineup_check(
                SimpleNamespace(base_dir=base, now="2026-07-21T18:45:00+09:00", min_minutes=0, max_minutes=30)
            )
            due = memory_store.cmd_due_lineup_check(
                SimpleNamespace(base_dir=base, now="2026-07-21T19:00:00+09:00", min_minutes=0, max_minutes=30)
            )
            self.assertEqual(early["due"], [])
            self.assertEqual([item["match_id"] for item in due["due"]], ["1"])

    def test_legacy_migration_primary_roi_all_formal_and_calibration(self):
        asian = lambda odds: {"side": "home", "line": 0.0, "odds": odds, "ev": 0.06, "market_signal": "aligned"}
        total = lambda odds: {"side": "under", "line": 2.5, "odds": odds, "ev": 0.06, "market_signal": "aligned"}
        half = {"market": "total", "side": "under", "line": 1.0, "odds": 1.06, "ev": 0.03, "market_signal": "unknown"}
        history = [
            reviewed_record("2907406", asian(0.98), "half_win", total(0.86), "win"),
            reviewed_record("2913667", asian(1.07), "loss", total(0.95), "win"),
            reviewed_record("2913668", asian(0.83), "loss", total(1.04), "loss"),
            reviewed_record("2912210", asian(0.93), "win", total(0.89), "win", half, "loss"),
            reviewed_record("2924601", asian(1.07), "win", total(1.06), "win"),
            reviewed_record("2929664", None, None, total(0.87), "loss"),
        ]
        assignments = [
            "2907406:total",
            "2913667:asian",
            "2913668:asian",
            "2912210:asian",
            "2924601:total",
            "2929664:total",
        ]
        with tempfile.TemporaryDirectory() as base:
            path = memory_store.data_path(base)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
            before_revisions = {r["match_id"]: r["revisions"] for r in history}

            migrated = memory_store.cmd_migrate_primary(
                SimpleNamespace(base_dir=base, primary=assignments, write=True)
            )
            stats = migrated["stats"]
            self.assertEqual(stats["primary"]["matches"], 6)
            self.assertEqual(stats["primary"]["wins"], 3)
            self.assertEqual(stats["primary"]["losses"], 3)
            self.assertEqual(stats["primary"]["pushes"], 0)
            self.assertEqual(stats["primary"]["accuracy"], 0.5)
            self.assertEqual(stats["primary"]["profit_units"], -0.15)
            self.assertEqual(stats["primary"]["roi"], -0.025)
            self.assertEqual(stats["primary_by_market"]["combined"]["matches"], 6)
            self.assertEqual(stats["primary_by_market"]["combined"]["wins"], 3)
            self.assertEqual(stats["primary_by_market"]["combined"]["losses"], 3)
            self.assertEqual(stats["all_formal"]["combined"]["monetary_scope"], "not_tracked")
            self.assertIsNone(stats["all_formal"]["combined"]["stake_units"])
            self.assertIsNone(stats["all_formal"]["combined"]["profit_units"])
            self.assertIsNone(stats["all_formal"]["combined"]["roi"])
            self.assertEqual(stats["combined"], stats["all_formal"]["combined"])

            saved = memory_store.load_history(path)
            self.assertEqual({r["match_id"]: r["revisions"] for r in saved}, before_revisions)
            for record in saved:
                roles = [pick.get("role") for _, pick in memory_store.formal_picks(record)]
                self.assertEqual(roles.count("primary"), 1)

            calibration = memory_store.cmd_calibrate(
                SimpleNamespace(base_dir=base, guardrail=None, minimum_graded=20, write=True)
            )["calibration"]
            self.assertEqual(calibration["reviewed_matches"], 6)
            self.assertIn("主推6场3胜3负0走", calibration["summary"])
            self.assertIn("收益-0.15u，ROI -2.50%", calibration["summary"])
            self.assertIn("主推分市场统计6项3胜3负0走", calibration["summary"])
            self.assertIn("次推仅作赛前参考，不结算、不计命中率或金额", calibration["summary"])
            self.assertTrue(
                any(
                    item.startswith("stability-v1")
                    for item in calibration["guardrails"]
                )
            )
            self.assertTrue(
                any("唯一rank=1可作主推" in item for item in calibration["guardrails"])
            )
            self.assertTrue(
                any("新方向至少高5分" in item for item in calibration["guardrails"])
            )
            self.assertFalse(
                any(
                    "所有正式方向（主推和正式次推）都必须满足EV>=8%" in item
                    for item in calibration["guardrails"]
                )
            )
            self.assertFalse(
                any(
                    "亚洲盘和大小球正式方向必须满足EV>=8%" in item
                    for item in calibration["guardrails"]
                )
            )
            self.assertEqual(calibration["active_weight_adjustments"], {})
            self.assertTrue(all(value is False for value in calibration["weight_change_eligible"].values()))

    def test_secondary_pick_is_ignored_by_all_statistics(self):
        primary = {"side": "under", "line": 2.5, "odds": 0.90, "ev": 0.06, "role": "primary"}
        secondary = {"side": "home", "line": 0.0, "odds": 0.84, "ev": 0.05, "role": "secondary"}
        record = reviewed_record("secondary-no-money", secondary, "loss", primary, "win")
        record.update({
            "primary_market": "total",
            "primary_pick": dict(primary, market="total"),
            "primary_result": "win",
        })

        stats = memory_store.calculate_stats([record])

        self.assertEqual(stats["primary"]["profit_units"], 0.9)
        self.assertEqual(stats["primary"]["roi"], 0.9)
        self.assertEqual(stats["all_formal"]["combined"]["matches"], 1)
        self.assertEqual(stats["all_formal"]["combined"]["wins"], 1)
        self.assertEqual(stats["all_formal"]["combined"]["losses"], 0)
        self.assertIsNone(stats["all_formal"]["combined"]["profit_units"])
        self.assertIsNone(stats["all_formal"]["asian"]["profit_units"])
        self.assertIsNone(stats["all_formal"]["totals"]["profit_units"])

    def test_small_sample_gate_boundaries_and_no_primary(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "total EV must be greater than 0"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="ev-zero",
                        asian_side=None,
                        total_ev=0.0,
                    )
                )
            with self.assertRaisesRegex(ValueError, "edge .* greater than 0"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="edge-zero",
                        asian_side=None,
                        total_edge_pp=0.0,
                    )
                )
            with self.assertRaisesRegex(ValueError, "medium or high"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="quality-low",
                        asian_side=None,
                        data_quality="low",
                    )
                )

            sub_eight = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="sub-eight",
                    asian_side=None,
                    total_ev=0.03,
                    total_edge_pp=1.5,
                )
            )["record"]
            self.assertEqual(sub_eight["primary_pick"]["ev"], 0.03)
            self.assertEqual(sub_eight["primary_pick"]["edge_pp"], 1.5)
            self.assertEqual(sub_eight["primary_pick"]["confidence_rank"], 1)
            self.assertEqual(
                sub_eight["primary_selection_basis"],
                "highest_stability_adjusted_confidence",
            )
            self.assertEqual(
                sub_eight["confidence_ranking_version"], "stability-v1"
            )

            no_pick = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="none",
                    asian_side=None,
                    total_side=None,
                    primary_market="none",
                )
            )["record"]
            self.assertIsNone(no_pick["primary_market"])
            self.assertIsNone(no_pick["primary_pick"])

            half_overrides = {
                "asian_side": None,
                "total_side": None,
                "half_market": "total",
                "half_side": "under",
                "half_line": 1.0,
                "half_odds": 0.9,
                "half_probability": 0.55,
                "half_edge_pp": 1.0,
                "half_market_signal": "aligned",
                "half_firm_count": 5,
                "primary_market": "half_time",
            }
            with self.assertRaisesRegex(ValueError, "half_time EV must be greater than 0"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="half-low",
                        half_ev=0.0,
                        **half_overrides,
                    )
                )
            half = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="half-boundary",
                    half_ev=0.025,
                    **half_overrides,
                )
            )["record"]
            self.assertEqual(half["primary_market"], "half_time")
            self.assertEqual(half["primary_pick"]["primary_market"], "half_time")
            self.assertEqual(half["primary_pick"]["market"], "total")
            self.assertEqual(half["primary_pick"]["confidence_rank"], 1)

            htft_overrides = {
                "asian_side": None,
                "total_side": None,
                "htft_pick": ["DD:3.40:0.31:0.054"],
                "primary_market": "htft",
                "primary_htft_firm_count": 5,
            }
            with self.assertRaisesRegex(ValueError, "htft .* edge .* greater than 0"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="htft-edge-low",
                        primary_htft_edge_pp=0.0,
                        **htft_overrides,
                    )
                )
            htft = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="htft-boundary",
                    primary_htft_edge_pp=1.2,
                    **htft_overrides,
                )
            )["record"]
            self.assertEqual(htft["primary_market"], "htft")

    def test_stability_ranking_beats_raw_ev_and_rejects_non_rank_one_primary(self):
        candidates = {
            "asian_probability": 0.48,
            "asian_ev": 0.12,
            "asian_edge_pp": 5.0,
            "asian_firm_count": 1,
            "asian_market_signal": "neutral",
            "total_probability": 0.65,
            "total_ev": 0.03,
            "total_edge_pp": 1.5,
            "total_firm_count": 8,
            "total_market_signal": "aligned",
        }
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(
                ValueError, "unique stability-v1 confidence rank 1"
            ):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="wrong-rank",
                        primary_market="asian",
                        **candidates,
                    )
                )

            accepted = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="right-rank",
                    primary_market="total",
                    **candidates,
                )
            )["record"]
            self.assertGreater(
                accepted["asian_pick"]["ev"], accepted["total_pick"]["ev"]
            )
            self.assertEqual(accepted["total_pick"]["confidence_rank"], 1)
            self.assertEqual(accepted["asian_pick"]["confidence_rank"], 2)
            self.assertEqual(accepted["primary_market"], "total")
            self.assertEqual(
                accepted["primary_pick"]["confidence_components"]["safety_source"],
                "model_probability_fallback",
            )

    def test_against_deep_favorite_and_total_evidence_gates(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "adverse-signal EV must be at least 0.08"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="against-ev",
                        asian_side=None,
                        total_market_signal="against",
                        total_ev=0.079,
                    )
                )
            with self.assertRaisesRegex(ValueError, "adverse-signal .* at least 4"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="against-edge",
                        asian_side=None,
                        total_market_signal="against",
                        total_edge_pp=3.9,
                    )
                )
            with self.assertRaisesRegex(ValueError, "bookmaker count must be at least 5"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="against-firms",
                        asian_side=None,
                        total_market_signal="against",
                        total_firm_count=4,
                    )
                )
            with self.assertRaisesRegex(ValueError, "independent lineup or fundamental"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="against-evidence",
                        asian_side=None,
                        total_market_signal="against",
                        lineup_confirmed=False,
                        fundamental_evidence=False,
                    )
                )
            with self.assertRaisesRegex(ValueError, "chance-quality evidence"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="total-evidence",
                        asian_side=None,
                        lineup_confirmed=False,
                        chance_quality_evidence=False,
                        attack_configuration_evidence=True,
                    )
                )

            deep_defaults = {
                "asian_side": "home",
                "asian_line": -0.75,
                "total_side": None,
                "primary_market": "asian",
                "data_quality": "high",
            }
            with self.assertRaisesRegex(ValueError, "confirmed lineups"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="deep-lineup",
                        lineup_confirmed=False,
                        **deep_defaults,
                    )
                )
            with self.assertRaisesRegex(ValueError, "chance-quality evidence"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="deep-quality",
                        chance_quality_evidence=False,
                        fundamental_evidence=False,
                        attack_configuration_evidence=False,
                        **deep_defaults,
                    )
                )
            with self.assertRaisesRegex(ValueError, "cover distribution"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="deep-cover",
                        asian_cover_distribution_validated=False,
                        **deep_defaults,
                    )
                )
            with self.assertRaisesRegex(ValueError, "tail-risk check"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="deep-tail",
                        opponent_tail_risk_checked=False,
                        **deep_defaults,
                    )
                )

            deep = memory_store.cmd_record(
                record_args(base, match_id="deep-pass", **deep_defaults)
            )["record"]
            self.assertTrue(deep["primary_pick"]["cover_distribution_validated"])
            deep_consensus = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="deep-consensus-pass",
                    asian_ev=0.03,
                    asian_edge_pp=1.5,
                    chance_quality_evidence=False,
                    **deep_defaults,
                )
            )["record"]
            self.assertEqual(deep_consensus["primary_pick"]["confidence_rank"], 1)
            self.assertLess(deep_consensus["primary_pick"]["ev"], 0.08)
            underdog = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="underdog",
                    asian_side="away",
                    asian_line=1.25,
                    total_side=None,
                    primary_market="asian",
                )
            )["record"]
            self.assertEqual(underdog["primary_pick"]["line"], 1.25)

    def test_lineup_change_hysteresis_cancellation_and_no_bet_review(self):
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(base, asian_side=None, primary_market="total")
            )
            changed_args = {
                "analysis_stage": "lineup-check",
                "primary_market": "asian",
                "total_side": None,
                "data_quality": "high",
                "primary_change_reason": "确认首发直接证伪原大小球逻辑",
                "previous_primary_current_ev": 0.05,
            }
            with self.assertRaisesRegex(
                ValueError, "previous-primary-current-confidence"
            ):
                memory_store.cmd_record(record_args(base, **changed_args))
            with self.assertRaisesRegex(ValueError, "at least 5 points"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        previous_primary_current_confidence=72.75,
                        **changed_args,
                    )
                )

            changed = memory_store.cmd_record(
                record_args(
                    base,
                    previous_primary_invalidated=True,
                    asian_ev=0.03,
                    **changed_args,
                )
            )["record"]
            self.assertEqual(changed["primary_change"]["status"], "changed")
            self.assertEqual(changed["primary_change"]["decision"], "strict_replacement")
            self.assertAlmostEqual(changed["primary_change"]["ev_improvement"], -0.02)
            self.assertIsNone(
                changed["primary_change"]["confidence_improvement"]
            )
            self.assertTrue(changed["primary_change"]["previous_invalidated"])
            self.assertTrue(changed["primary_change"]["guardrail_passed"])
            self.assertEqual(changed["revisions"][-1]["primary_market"], "total")

        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(base, asian_side=None, primary_market="total")
            )
            improved = memory_store.cmd_record(
                record_args(
                    base,
                    analysis_stage="lineup-check",
                    primary_market="asian",
                    total_side=None,
                    data_quality="high",
                    primary_change_reason="新方向综合稳定性显著提升",
                    previous_primary_current_confidence=60.0,
                )
            )["record"]
            self.assertGreaterEqual(
                improved["primary_change"]["confidence_improvement"], 5.0
            )
            self.assertFalse(improved["primary_change"]["previous_invalidated"])

        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(base, asian_side=None, primary_market="total")
            )
            cancelled = memory_store.cmd_record(
                record_args(
                    base,
                    analysis_stage="lineup-check",
                    asian_side=None,
                    total_side=None,
                    primary_market="none",
                    primary_change_reason="确认首发后原主推失效且无替代方向过门槛",
                )
            )["record"]
            self.assertEqual(cancelled["primary_change"]["decision"], "cancelled_to_none")
            self.assertIsNone(cancelled["primary_pick"])
            self.assertEqual(len(cancelled["revisions"]), 1)

            reviewed = memory_store.cmd_review(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    match_id="1",
                    home_score=1,
                    away_score=0,
                    half_home_score=0,
                    half_away_score=0,
                    key_learning="临场硬信息否定旧方向后正确选择不下注",
                )
            )
            self.assertIsNone(reviewed["record"]["primary_result"])
            self.assertIsNone(
                reviewed["record"]["settlement_basis"]["primary_market"]
            )
            self.assertEqual(reviewed["stats"]["reviewed_matches"], 1)
            self.assertEqual(reviewed["stats"]["primary"]["matches"], 0)
            self.assertEqual(
                reviewed["record"]["learning_scope"],
                "no_primary_observation",
            )
            self.assertFalse(
                reviewed["record"]["counts_toward_primary_record"]
            )
            self.assertEqual(
                reviewed["record"]["learning_sample"]["scope"],
                "no_primary_observation",
            )
            self.assertEqual(reviewed["stats"]["primary_record_matches"], 0)
            self.assertEqual(
                reviewed["stats"]["no_primary_reviewed_matches"], 1
            )
            self.assertEqual(
                reviewed["stats"]["learning_samples"],
                {
                    "total": 1,
                    "primary": 0,
                    "no_primary_observation": 1,
                },
            )
            self.assertEqual(reviewed["stats"]["primary"]["stake_units"], 0)
            self.assertEqual(reviewed["stats"]["primary"]["profit_units"], 0)
            self.assertIsNone(reviewed["stats"]["primary"]["roi"])

        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(
                    base,
                    asian_side=None,
                    total_side=None,
                    primary_market="none",
                )
            )
            maintained_none = memory_store.cmd_record(
                record_args(
                    base,
                    analysis_stage="lineup-check",
                    asian_side=None,
                    total_side=None,
                    primary_market="none",
                )
            )["record"]
            self.assertEqual(
                maintained_none["primary_change"]["status"], "maintained"
            )
            newly_qualified = memory_store.cmd_record(
                record_args(
                    base,
                    analysis_stage="lineup-check",
                    asian_side=None,
                    primary_market="total",
                    data_quality="high",
                    primary_change_reason="确认首发后大小球方向首次达到正式门槛",
                )
            )["record"]
            self.assertEqual(
                newly_qualified["primary_change"]["decision"], "newly_qualified"
            )

    def test_worse_line_requires_explicit_strict_replacement(self):
        with tempfile.TemporaryDirectory() as base:
            initial = record_args(
                base,
                asian_line=-0.75,
                total_side=None,
                primary_market="asian",
                data_quality="high",
            )
            memory_store.cmd_record(initial)
            replacement = {
                "analysis_stage": "lineup-check",
                "asian_line": -1.0,
                "asian_ev": 0.13,
                "total_side": None,
                "primary_market": "asian",
                "data_quality": "high",
                "primary_change_reason": "确认首发和机会质量证据直接提升穿盘分布",
                "previous_primary_invalidated": True,
                "previous_primary_current_ev": 0.08,
            }
            with self.assertRaisesRegex(ValueError, "accept-worse-line"):
                memory_store.cmd_record(record_args(base, **replacement))

            accepted = memory_store.cmd_record(
                record_args(base, accept_worse_line=True, **replacement)
            )["record"]
            self.assertEqual(
                accepted["primary_change"]["decision"], "worse_line_replaced"
            )
            self.assertTrue(accepted["primary_change"]["worse_line"])
            self.assertEqual(accepted["revisions"][-1]["primary_pick"]["line"], -0.75)

    def test_new_formal_market_guardrails_and_complete_odds(self):
        goal = {
            "asian_side": None,
            "total_side": None,
            "primary_market": "goal_range",
            "goal_range_selection": "2-3",
            "goal_range_odds": 1.90,
            "goal_range_odds_format": "decimal",
            "goal_range_probability": 0.58,
            "goal_range_ev": 0.102,
            "goal_range_edge_pp": 5.0,
            "goal_range_firm_count": 4,
            "goal_range_market_signal": "aligned",
            "goal_range_market_complete": True,
        }
        with tempfile.TemporaryDirectory() as base:
            incomplete = dict(goal)
            incomplete["goal_range_market_complete"] = False
            with self.assertRaisesRegex(ValueError, "market_complete=true"):
                memory_store.cmd_record(
                    record_args(base, match_id="goal-incomplete", **incomplete)
                )
            missing_format = dict(goal)
            missing_format["goal_range_odds_format"] = None
            with self.assertRaisesRegex(ValueError, "explicit odds_format"):
                memory_store.cmd_record(
                    record_args(base, match_id="goal-format", **missing_format)
                )
            missing_source = dict(goal)
            missing_source["goal_range_market_source"] = ""
            with self.assertRaisesRegex(ValueError, "market_source is required"):
                memory_store.cmd_record(
                    record_args(base, match_id="goal-source", **missing_source)
                )
            naive_time = dict(goal)
            naive_time["goal_range_market_collected_at"] = "2026-07-21T19:00:00"
            with self.assertRaisesRegex(ValueError, "datetime with timezone"):
                memory_store.cmd_record(
                    record_args(base, match_id="goal-timezone", **naive_time)
                )
            with self.assertRaisesRegex(ValueError, "EV must be greater than 0"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="goal-ev-zero",
                        goal_range_odds=2.0,
                        goal_range_probability=0.50,
                        goal_range_ev=0.0,
                        goal_range_edge_pp=1.0,
                        goal_range_market_probability=0.49,
                        **{
                            key: value
                            for key, value in goal.items()
                            if key
                            not in {
                                "goal_range_odds",
                                "goal_range_probability",
                                "goal_range_ev",
                                "goal_range_edge_pp",
                            }
                        },
                    )
                )
            with self.assertRaisesRegex(ValueError, "edge .* greater than 0"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="goal-edge-zero",
                        goal_range_edge_pp=0.0,
                        goal_range_market_probability=0.58,
                        **{
                            key: value
                            for key, value in goal.items()
                            if key != "goal_range_edge_pp"
                        },
                    )
                )
            sub_eight_goal = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="goal-sub-eight",
                    goal_range_probability=0.54,
                    goal_range_ev=0.026,
                    goal_range_edge_pp=2.0,
                    goal_range_market_probability=0.52,
                    **{
                        key: value
                        for key, value in goal.items()
                        if key
                        not in {
                            "goal_range_probability",
                            "goal_range_ev",
                            "goal_range_edge_pp",
                        }
                    },
                )
            )["record"]
            self.assertEqual(sub_eight_goal["primary_pick"]["confidence_rank"], 1)
            self.assertLess(sub_eight_goal["primary_pick"]["ev"], 0.08)
            with self.assertRaisesRegex(ValueError, "medium or high"):
                memory_store.cmd_record(
                    record_args(
                        base, match_id="goal-quality", data_quality="low", **goal
                    )
                )
            with self.assertRaisesRegex(ValueError, "attacking-configuration"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="goal-evidence",
                        lineup_confirmed=False,
                        chance_quality_evidence=False,
                        attack_configuration_evidence=True,
                        **goal,
                    )
                )
            with self.assertRaisesRegex(ValueError, "Stale injury evidence"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="goal-stale-injury",
                        injury_evidence_status="stale_conflict",
                        **goal,
                    )
                )

            created = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="goal-ok",
                    btts_side="yes",
                    btts_odds=1.95,
                    btts_odds_format="decimal",
                    btts_probability=0.57,
                    btts_ev=0.1115,
                    btts_edge_pp=5.0,
                    btts_firm_count=4,
                    btts_market_signal="neutral",
                    btts_market_complete=True,
                    **goal,
                )
            )["record"]
            self.assertEqual(created["primary_market"], "goal_range")
            self.assertEqual(created["primary_pick"]["selection"], "2-3")
            self.assertEqual(created["primary_pick"]["odds_format"], "decimal")
            self.assertTrue(created["primary_pick"]["market_complete"])
            self.assertEqual(created["primary_pick"]["market_probability"], 0.53)
            self.assertEqual(
                created["primary_pick"]["market_collected_at"],
                "2026-07-21T19:00:00+09:00",
            )
            self.assertEqual(created["primary_pick"]["price_basis"], "consensus")
            self.assertEqual(created["goal_range_pick"]["role"], "primary")
            self.assertEqual(created["btts_pick"]["role"], "secondary")
            self.assertTrue(created["btts_pick"]["market_complete"])
            self.assertEqual(
                sum(pick["role"] == "primary" for _, pick in memory_store.formal_picks(created)),
                1,
            )

            hong_kong = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="btts-hk",
                    asian_side=None,
                    total_side=None,
                    primary_market="btts",
                    btts_side="yes",
                    btts_odds=0.95,
                    btts_odds_format="hong_kong",
                    btts_probability=0.57,
                    btts_ev=0.1115,
                    btts_edge_pp=5.0,
                    btts_firm_count=4,
                    btts_market_signal="aligned",
                    btts_market_complete=True,
                )
            )["record"]
            self.assertEqual(
                hong_kong["primary_pick"]["odds_format"], "hong_kong"
            )
            with self.assertRaisesRegex(ValueError, "EV does not match"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="btts-hk-fake-ev",
                        asian_side=None,
                        total_side=None,
                        primary_market="btts",
                        btts_side="yes",
                        btts_odds=0.95,
                        btts_odds_format="hong_kong",
                        btts_probability=0.57,
                        btts_ev=0.20,
                        btts_edge_pp=5.0,
                        btts_firm_count=4,
                        btts_market_signal="aligned",
                        btts_market_complete=True,
                    )
                )

        corner = {
            "asian_side": None,
            "total_side": None,
            "primary_market": "corner_total",
            "corner_total_side": "over",
            "corner_total_line": 9.5,
            "corner_total_odds": 1.0,
            "corner_total_odds_format": "hong_kong",
            "corner_total_probability": 0.56,
            "corner_total_ev": 0.12,
            "corner_total_edge_pp": 4.5,
            "corner_total_firm_count": 3,
            "corner_total_market_signal": "aligned",
            "corner_total_market_complete": True,
        }
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(
                ValueError, "line 9.5 cannot produce settlement states: half_win"
            ):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-half-line-state",
                        corner_total_full_win_probability=0.55,
                        corner_total_half_win_probability=0.01,
                        **corner,
                    )
                )
            with self.assertRaisesRegex(
                ValueError, "line 10 cannot produce settlement states: half_loss"
            ):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-integer-state",
                        corner_total_line=10.0,
                        corner_total_half_loss_probability=0.01,
                        corner_total_loss_probability=0.43,
                        **{
                            key: value
                            for key, value in corner.items()
                            if key != "corner_total_line"
                        },
                    )
                )
            with self.assertRaisesRegex(
                ValueError, "line 10.75 cannot produce settlement states: push"
            ):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-quarter-state",
                        corner_total_line=10.75,
                        corner_total_push_probability=0.01,
                        corner_total_loss_probability=0.43,
                        **{
                            key: value
                            for key, value in corner.items()
                            if key != "corner_total_line"
                        },
                    )
                )
            missing_distribution = dict(corner)
            missing_distribution["corner_total_full_win_probability"] = None
            with self.assertRaisesRegex(ValueError, "full_win is required"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-distribution",
                        **missing_distribution,
                    )
                )
            with self.assertRaisesRegex(ValueError, "probabilities must sum to 1"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-distribution-sum",
                        corner_total_loss_probability=0.31,
                        **corner,
                    )
                )
            with self.assertRaisesRegex(ValueError, "EV does not match"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-fake-ev",
                        corner_total_ev=0.20,
                        **{
                            key: value
                            for key, value in corner.items()
                            if key != "corner_total_ev"
                        },
                    )
                )
            with self.assertRaisesRegex(ValueError, "corner_total EV must be greater than 0"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-ev-zero",
                        corner_total_odds=0.7857142857,
                        corner_total_ev=0.0,
                        **{
                            key: value
                            for key, value in corner.items()
                            if key
                            not in {
                                "corner_total_odds",
                                "corner_total_ev",
                            }
                        },
                    )
                )
            sub_eight_corner = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="corner-sub-eight",
                    corner_total_odds=0.8,
                    corner_total_ev=0.008,
                    **{
                        key: value
                        for key, value in corner.items()
                        if key not in {"corner_total_odds", "corner_total_ev"}
                    },
                )
            )["record"]
            self.assertLess(sub_eight_corner["primary_pick"]["ev"], 0.08)
            self.assertEqual(
                sub_eight_corner["primary_pick"]["confidence_rank"], 1
            )
            with self.assertRaisesRegex(ValueError, "corner-profile evidence"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-evidence",
                        corner_profile_evidence=False,
                        **corner,
                    )
                )
            with self.assertRaisesRegex(ValueError, "bookmaker count must be at least 3"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-firms",
                        corner_total_firm_count=2,
                        **{
                            key: value
                            for key, value in corner.items()
                            if key != "corner_total_firm_count"
                        },
                    )
                )
            with self.assertRaisesRegex(ValueError, "adverse-signal .* at least 5"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-against",
                        corner_total_market_signal="against",
                        corner_total_firm_count=4,
                        **{
                            key: value
                            for key, value in corner.items()
                            if key not in {
                                "corner_total_market_signal",
                                "corner_total_firm_count",
                            }
                        },
                    )
                )
            accepted = memory_store.cmd_record(
                record_args(base, match_id="corner-ok", **corner)
            )["record"]
            self.assertEqual(accepted["primary_pick"]["line"], 9.5)
            self.assertTrue(accepted["primary_pick"]["market_complete"])

    def test_new_market_settlement_stats_and_corner_score_requirement(self):
        def review(base, match_id, home, away, **extra):
            values = {
                "base_dir": base,
                "verified_finished": True,
                "match_id": match_id,
                "home_score": home,
                "away_score": away,
                "half_home_score": None,
                "half_away_score": None,
                "home_corners": None,
                "away_corners": None,
                "key_learning": "验证新市场结算",
            }
            values.update(extra)
            return memory_store.cmd_review(SimpleNamespace(**values))

        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(
                    base,
                    match_id="goal",
                    asian_side=None,
                    total_side=None,
                    primary_market="goal_range",
                    goal_range_selection="2-3",
                    goal_range_odds=1.90,
                    goal_range_odds_format="decimal",
                    goal_range_probability=0.58,
                    goal_range_ev=0.102,
                    goal_range_edge_pp=5.0,
                    goal_range_firm_count=4,
                    goal_range_market_signal="aligned",
                    goal_range_market_complete=True,
                    btts_side="yes",
                    btts_odds=1.95,
                    btts_odds_format="decimal",
                    btts_probability=0.57,
                    btts_ev=0.1115,
                    btts_edge_pp=5.0,
                    btts_firm_count=4,
                    btts_market_signal="aligned",
                    btts_market_complete=True,
                )
            )
            goal_review = review(base, "goal", 1, 2)
            self.assertEqual(goal_review["record"]["primary_result"], "win")
            self.assertEqual(
                goal_review["record"]["settlement_basis"]["formal_picks"][
                    "goal_range"
                ]["selection"],
                "2-3",
            )
            self.assertIsNone(goal_review["record"]["btts_result"])
            self.assertEqual(goal_review["stats"]["primary"]["profit_units"], 0.9)
            self.assertEqual(
                goal_review["stats"]["primary_by_market"]["btts"]["matches"], 0
            )

            memory_store.cmd_record(
                record_args(
                    base,
                    match_id="btts",
                    asian_side=None,
                    total_side=None,
                    primary_market="btts",
                    btts_side="no",
                    btts_odds=2.10,
                    btts_odds_format="decimal",
                    btts_probability=0.54,
                    btts_ev=0.134,
                    btts_edge_pp=5.0,
                    btts_market_probability=0.49,
                    btts_firm_count=4,
                    btts_market_signal="neutral",
                    btts_market_complete=True,
                )
            )
            self.assertEqual(review(base, "btts", 2, 0)["record"]["btts_result"], "win")

            memory_store.cmd_record(
                record_args(
                    base,
                    match_id="corner-total",
                    asian_side=None,
                    total_side=None,
                    primary_market="corner_total",
                    corner_total_side="over",
                    corner_total_line=10.75,
                    corner_total_odds=1.20,
                    corner_total_odds_format="hong_kong",
                    corner_total_probability=0.56,
                    corner_total_ev=0.186,
                    corner_total_edge_pp=4.5,
                    corner_total_firm_count=3,
                    corner_total_market_signal="aligned",
                    corner_total_market_complete=True,
                    corner_total_full_win_probability=0.45,
                    corner_total_half_win_probability=0.11,
                    corner_total_push_probability=0.0,
                    corner_total_half_loss_probability=0.04,
                    corner_total_loss_probability=0.40,
                )
            )
            with self.assertRaisesRegex(ValueError, "corner primary requires verified"):
                review(base, "corner-total", 1, 0)
            pending = memory_store.find_record(
                memory_store.load_history(memory_store.data_path(base)),
                "corner-total",
            )
            self.assertEqual(pending["status"], "pending")
            corner_total_review = review(
                base,
                "corner-total",
                1,
                0,
                home_corners=6,
                away_corners=5,
            )
            self.assertEqual(
                corner_total_review["record"]["corner_total_result"], "half_win"
            )
            self.assertEqual(corner_total_review["record"]["corner_score"], "6-5")
            self.assertEqual(
                corner_total_review["record"]["primary_pick"][
                    "settlement_probabilities"
                ]["half_win"],
                0.11,
            )

            memory_store.cmd_record(
                record_args(
                    base,
                    match_id="corner-handicap",
                    asian_side=None,
                    total_side=None,
                    primary_market="corner_handicap",
                    corner_handicap_side="home",
                    corner_handicap_line=-2.0,
                    corner_handicap_odds=1.95,
                    corner_handicap_odds_format="decimal",
                    corner_handicap_probability=0.56,
                    corner_handicap_ev=0.192,
                    corner_handicap_edge_pp=4.5,
                    corner_handicap_firm_count=3,
                    corner_handicap_market_signal="aligned",
                    corner_handicap_market_complete=True,
                )
            )
            corner_handicap_review = review(
                base,
                "corner-handicap",
                0,
                0,
                home_corners=7,
                away_corners=5,
            )
            self.assertEqual(
                corner_handicap_review["record"]["corner_handicap_result"], "push"
            )

            stats = corner_handicap_review["stats"]
            self.assertEqual(stats["primary"]["matches"], 4)
            self.assertEqual(stats["primary"]["profit_units"], 2.6)
            self.assertEqual(stats["primary"]["roi"], 0.65)
            self.assertEqual(stats["primary_by_market"]["goal_range"]["matches"], 1)
            self.assertEqual(stats["primary_by_market"]["btts"]["matches"], 1)
            self.assertEqual(stats["primary_by_market"]["corner_total"]["matches"], 1)
            self.assertEqual(
                stats["primary_by_market"]["corner_handicap"]["matches"], 1
            )
            self.assertEqual(stats["all_formal"]["btts"]["matches"], 1)
            calibration = memory_store.cmd_calibrate(
                SimpleNamespace(
                    base_dir=base,
                    guardrail=None,
                    minimum_graded=20,
                    write=False,
                )
            )["calibration"]
            for market in (
                "goal_range",
                "btts",
                "corner_total",
                "corner_handicap",
            ):
                self.assertIn(market, calibration["weight_change_eligible"])
                self.assertIn(market, calibration["stats"]["primary_by_market"])

        self.assertEqual(
            memory_store.settle_goal_range({"selection": "7+"}, 4, 3), "win"
        )
        self.assertEqual(
            memory_store.settle_goal_range({"selection": "2-3"}, 1, 2), "win"
        )
        self.assertEqual(
            memory_store.settle_goal_range({"selection": "2-3"}, 2, 2), "loss"
        )
        self.assertEqual(memory_store.settle_btts({"side": "yes"}, 1, 1), "win")
        self.assertEqual(memory_store.settle_btts({"side": "no"}, 2, 0), "win")
        self.assertEqual(memory_store.settle_btts({"side": "no"}, 1, 1), "loss")
        self.assertEqual(
            memory_store.settle_corner_total(
                {"side": "over", "line": 10.75}, 6, 5
            ),
            "half_win",
        )
        self.assertEqual(
            memory_store.settle_corner_handicap(
                {"side": "home", "line": -2.0}, 7, 5
            ),
            "push",
        )
        self.assertEqual(memory_store.settlement_profit("win", 0.90), 0.90)
        self.assertEqual(
            memory_store.settlement_profit("half_win", 0.92, "hong_kong"),
            0.46,
        )
        self.assertEqual(
            memory_store.settlement_profit("push", 1.95, "decimal"), 0.0
        )
        self.assertAlmostEqual(
            memory_store.settlement_profit("win", 1.90, "decimal"), 0.90
        )

    def test_new_market_lineup_identity_worse_line_and_revisions(self):
        initial = {
            "asian_side": None,
            "total_side": None,
            "primary_market": "corner_total",
            "corner_total_side": "over",
            "corner_total_line": 9.5,
            "corner_total_odds": 2.0,
            "corner_total_odds_format": "decimal",
            "corner_total_probability": 0.56,
            "corner_total_ev": 0.12,
            "corner_total_edge_pp": 4.5,
            "corner_total_firm_count": 3,
            "corner_total_market_signal": "aligned",
            "corner_total_market_complete": True,
        }
        with tempfile.TemporaryDirectory() as base:
            first = memory_store.cmd_record(
                record_args(base, match_id="lineup-corners", **initial)
            )["record"]
            self.assertEqual(
                memory_store.active_primary_identity(first),
                ("corner_total", "over", 9.5),
            )
            replacement = dict(initial)
            replacement.update(
                {
                    "analysis_stage": "lineup-check",
                    "data_quality": "high",
                    "corner_total_line": 10.0,
                    "corner_total_odds": 2.0,
                    "corner_total_ev": 0.22,
                    "corner_total_full_win_probability": 0.56,
                    "corner_total_half_win_probability": 0.0,
                    "corner_total_push_probability": 0.10,
                    "corner_total_half_loss_probability": 0.0,
                    "corner_total_loss_probability": 0.34,
                    "primary_change_reason": "确认首发改变角球强度",
                    "previous_primary_invalidated": True,
                    "previous_primary_current_ev": 0.12,
                }
            )
            with self.assertRaisesRegex(ValueError, "accept-worse-line"):
                memory_store.cmd_record(
                    record_args(base, match_id="lineup-corners", **replacement)
                )
            accepted = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="lineup-corners",
                    accept_worse_line=True,
                    **replacement,
                )
            )["record"]
            self.assertEqual(
                accepted["primary_change"]["decision"], "worse_line_replaced"
            )
            self.assertEqual(len(accepted["revisions"]), 1)
            self.assertEqual(
                accepted["revisions"][0]["corner_total_pick"]["line"], 9.5
            )
            self.assertIn("goal_range_pick", accepted["revisions"][0])
            self.assertIn(
                "corner_handicap",
                memory_store.settlement_basis_for_record(accepted)["formal_picks"],
            )
            self.assertEqual(
                memory_store.pick_identity(
                    "goal_range", {"selection": "2-3"}
                ),
                ("goal_range", "2-3"),
            )

    def test_half_time_primary_review_requires_verified_half_score(self):
        def review_args(base, match_id):
            return SimpleNamespace(
                base_dir=base,
                verified_finished=True,
                match_id=match_id,
                home_score=1,
                away_score=0,
                half_home_score=None,
                half_away_score=None,
                home_corners=None,
                away_corners=None,
                key_learning="半场比分必须可验证",
            )

        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(
                    base,
                    match_id="half-primary",
                    asian_side=None,
                    total_side=None,
                    half_market="total",
                    half_side="under",
                    half_line=1.0,
                    half_odds=0.90,
                    half_probability=0.55,
                    half_ev=0.08,
                    half_edge_pp=4.0,
                    half_firm_count=5,
                    half_market_signal="aligned",
                    primary_market="half_time",
                )
            )
            with self.assertRaisesRegex(ValueError, "half-time or HT/FT primary"):
                memory_store.cmd_review(review_args(base, "half-primary"))
            pending = memory_store.find_record(
                memory_store.load_history(memory_store.data_path(base)),
                "half-primary",
            )
            self.assertEqual(pending["status"], "pending")

            memory_store.cmd_record(
                record_args(
                    base,
                    match_id="htft-primary",
                    asian_side=None,
                    total_side=None,
                    htft_pick=["DD:3.40:0.31:0.08"],
                    primary_market="htft",
                    primary_htft_edge_pp=4.0,
                    primary_htft_firm_count=5,
                )
            )
            with self.assertRaisesRegex(ValueError, "half-time or HT/FT primary"):
                memory_store.cmd_review(review_args(base, "htft-primary"))
            pending = memory_store.find_record(
                memory_store.load_history(memory_store.data_path(base)),
                "htft-primary",
            )
            self.assertEqual(pending["status"], "pending")

    def test_reviewed_stats_prefer_frozen_settlement_basis_over_top_level_drift(self):
        frozen_total = {
            "side": "under",
            "line": 2.5,
            "odds": 0.90,
            "ev": 0.09,
            "role": "primary",
        }
        drifted_asian = {
            "side": "away",
            "line": 0.5,
            "odds": 5.0,
            "ev": 0.50,
            "role": "primary",
        }
        record = reviewed_record(
            "basis-drift",
            asian=drifted_asian,
            asian_result="loss",
            total=frozen_total,
            total_result="win",
        )
        record.update(
            {
                "primary_market": "asian",
                "primary_pick": drifted_asian,
                "primary_result": "loss",
                "settlement_basis": {
                    "policy": "latest_active_prematch_version",
                    "primary_market": "total",
                    "primary_pick": frozen_total,
                    "formal_picks": {
                        "asian": None,
                        "total": frozen_total,
                        "half_time": None,
                        "htft": [],
                        "goal_range": None,
                        "btts": None,
                        "corner_total": None,
                        "corner_handicap": None,
                    },
                },
            }
        )

        stats = memory_store.calculate_stats([record])

        self.assertEqual(stats["primary"]["matches"], 1)
        self.assertEqual(stats["primary"]["profit_units"], 0.9)
        self.assertEqual(stats["primary_by_market"]["totals"]["matches"], 1)
        self.assertEqual(stats["primary_by_market"]["asian"]["matches"], 0)
        self.assertEqual(
            memory_store.primary_snapshot_for_stats(record),
            ("total", frozen_total),
        )

        legacy = reviewed_record(
            "legacy-no-basis",
            asian=drifted_asian,
            asian_result="loss",
        )
        legacy.update(
            {
                "primary_market": "asian",
                "primary_pick": drifted_asian,
                "primary_result": "loss",
            }
        )
        legacy_stats = memory_store.calculate_stats([legacy])
        self.assertEqual(legacy_stats["primary_by_market"]["asian"]["matches"], 1)
        self.assertEqual(legacy_stats["primary"]["profit_units"], -1.0)


if __name__ == "__main__":
    unittest.main()

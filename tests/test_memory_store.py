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
        "recommendation": "测试",
        "source_url": "https://example.test/match",
        "notes": "",
        "data_quality": "medium",
        "lineup_confirmed": True,
        "fundamental_evidence": True,
        "chance_quality_evidence": True,
        "attack_configuration_evidence": True,
        "opponent_tail_risk_checked": True,
        "injury_evidence_status": "fresh",
        "primary_change_reason": "",
        "previous_primary_invalidated": False,
        "previous_primary_current_ev": None,
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
        "asian_probability": 0.55,
        "asian_ev": 0.09,
        "asian_edge_pp": 4.5,
        "asian_firm_count": 8,
        "asian_cover_probability": 0.55,
        "asian_cover_distribution_validated": True,
        "asian_market_signal": "aligned",
        "total_side": "under",
        "total_line": 2.5,
        "total_odds": 0.9,
        "total_probability": 0.55,
        "total_ev": 0.09,
        "total_edge_pp": 4.5,
        "total_firm_count": 8,
        "total_market_signal": "aligned",
        "half_market": None,
        "half_side": None,
        "half_line": None,
        "half_odds": None,
        "half_probability": None,
        "half_ev": None,
        "half_edge_pp": None,
        "half_firm_count": None,
        "half_market_signal": "unknown",
        "htft_pick": None,
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
            self.assertTrue(all("主推或全部正式方向" not in item for item in calibration["guardrails"]))
            self.assertTrue(
                any(
                    "所有市场主推必须满足EV>=8%" in item
                    for item in calibration["guardrails"]
                )
            )
            self.assertTrue(
                any(
                    "当前EV至少比旧方向高4pp" in item
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
            with self.assertRaisesRegex(ValueError, "total EV must be at least 0.08"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="ev-low",
                        asian_side=None,
                        total_ev=0.079999,
                    )
                )
            with self.assertRaisesRegex(ValueError, "edge .* must be at least 4"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="edge-low",
                        asian_side=None,
                        total_edge_pp=3.999,
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

            boundary = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="boundary",
                    asian_side=None,
                    total_ev=0.08,
                    total_edge_pp=4.0,
                )
            )["record"]
            self.assertEqual(boundary["primary_pick"]["ev"], 0.08)
            self.assertEqual(boundary["primary_pick"]["edge_pp"], 4.0)

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
                "half_edge_pp": 4.0,
                "primary_market": "half_time",
            }
            with self.assertRaisesRegex(ValueError, "half_time primary EV"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="half-low",
                        half_ev=0.079,
                        **half_overrides,
                    )
                )
            half = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="half-boundary",
                    half_ev=0.08,
                    **half_overrides,
                )
            )["record"]
            self.assertEqual(half["primary_market"], "half_time")

            htft_overrides = {
                "asian_side": None,
                "total_side": None,
                "htft_pick": ["DD:3.40:0.31:0.08"],
                "primary_market": "htft",
                "primary_htft_firm_count": 5,
            }
            with self.assertRaisesRegex(ValueError, "htft primary .* edge"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="htft-edge-low",
                        primary_htft_edge_pp=3.9,
                        **htft_overrides,
                    )
                )
            htft = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="htft-boundary",
                    primary_htft_edge_pp=4.0,
                    **htft_overrides,
                )
            )["record"]
            self.assertEqual(htft["primary_market"], "htft")

    def test_against_deep_favorite_and_total_evidence_gates(self):
        with tempfile.TemporaryDirectory() as base:
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
                "data_quality": "high",
                "primary_change_reason": "确认首发直接证伪原大小球逻辑",
                "previous_primary_current_ev": 0.05,
            }
            with self.assertRaisesRegex(ValueError, "previous-primary-invalidated"):
                memory_store.cmd_record(record_args(base, **changed_args))
            with self.assertRaisesRegex(ValueError, "at least 4 percentage points"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        previous_primary_invalidated=True,
                        asian_ev=0.089,
                        **changed_args,
                    )
                )

            changed = memory_store.cmd_record(
                record_args(
                    base,
                    previous_primary_invalidated=True,
                    asian_ev=0.09,
                    **changed_args,
                )
            )["record"]
            self.assertEqual(changed["primary_change"]["status"], "changed")
            self.assertEqual(changed["primary_change"]["decision"], "strict_replacement")
            self.assertAlmostEqual(changed["primary_change"]["ev_improvement"], 0.04)
            self.assertTrue(changed["primary_change"]["guardrail_passed"])
            self.assertEqual(changed["revisions"][-1]["primary_market"], "total")

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


if __name__ == "__main__":
    unittest.main()

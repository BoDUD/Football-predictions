from __future__ import annotations

import importlib.util
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory_store.py"
SPEC = importlib.util.spec_from_file_location("soccer_memory_store", SCRIPT)
assert SPEC and SPEC.loader
memory_store = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(memory_store)


DEFAULT_SCORE_MATRIX = [
    [0.10, 0.05, 0.0, 0.10],
    [0.20, 0.05, 0.0, 0.10],
    [0.15, 0.0, 0.10, 0.0],
    [0.15, 0.0, 0.0, 0.0],
]


def write_score_prediction(
    base_dir: str,
    match_id: str,
    *,
    matrix=None,
    home_team="主队",
    away_team="客队",
    kickoff="2026-07-21T19:30:00+09:00",
):
    artifact = {
        "artifact_type": "soccer_score_prediction",
        "schema_version": "1.0.0",
        "model_version": "dixon-coles-time-decay/1.0.0",
        "model_hash": "sha256:" + "1" * 64,
        "generated_at": "2026-07-21T18:55:00+09:00",
        "fixture": {
            "home_team": home_team,
            "away_team": away_team,
            "kickoff": kickoff,
            "unknown_team_policy": "error",
        },
        "provenance": {
            "model_schema_version": "1.0.0",
            "training": {
                "source_data_hash": "sha256:" + "2" * 64,
                "match_count": 100,
                "start_date": "2025-01-01",
                "end_date": "2026-07-20",
            },
            "training_cutoff_date": "2026-07-20",
            "strictly_before_kickoff_utc_date": True,
            "generated_before_kickoff": True,
        },
        "score_matrix": {"probabilities": matrix or DEFAULT_SCORE_MATRIX},
        "tail_mass": {
            "tolerance_met": True,
            "raw_omitted_probability": 0.0,
            "tolerance": 1e-8,
        },
        "one_x_two": {"home": 0.5, "draw": 0.25, "away": 0.25},
        "totals": {},
        "asian_handicaps": {},
    }
    path = Path(base_dir) / f"score-prediction-{match_id}.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    return str(path)


def synthesize_market_odds(values, prefix, outcomes, selected):
    odds = values.get(f"{prefix}_odds")
    market_probability = values.get(f"{prefix}_market_probability")
    odds_format = values.get(f"{prefix}_odds_format")
    if odds is None or market_probability is None or not selected or odds_format is None:
        return None
    selected_raw = 1.0 / (float(odds) if odds_format == "decimal" else 1.0 + float(odds))
    scale = selected_raw / float(market_probability)
    remaining = (1.0 - float(market_probability)) / (len(outcomes) - 1)
    result = []
    for outcome in outcomes:
        probability = float(market_probability) if outcome == selected else remaining
        raw = scale * probability
        price = 1.0 / raw if odds_format == "decimal" else 1.0 / raw - 1.0
        result.append(f"{outcome}:{price:.12f}")
    return result


def review_command(args):
    previous = memory_store.utc_now
    memory_store.utc_now = lambda: datetime(
        2026, 7, 21, 13, 0, tzinfo=timezone.utc
    )
    try:
        return memory_store.cmd_review(args)
    finally:
        memory_store.utc_now = previous


def record_args(base_dir: str, match_id: str = "1", **overrides):
    values = {
        "base_dir": base_dir,
        "match_id": match_id,
        "analysis_stage": "initial",
        "league": "测试联赛",
        "kickoff": "2026-07-21T19:30:00+09:00",
        "page_status": "prematch",
        "source_kickoff": "2026-07-21T18:30:00+08:00",
        "source_timezone": "Asia/Shanghai",
        "user_local_kickoff": "2026-07-21T19:30:00+09:00",
        "user_timezone": "Asia/Tokyo",
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
        "model_version": "dixon-coles-time-decay/1.0.0",
        "score_model_file": None,
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
        "asian_side": None,
        "asian_line": -0.5,
        "asian_odds": 0.9,
        "asian_odds_format": "hong_kong",
        "asian_probability": 0.54,
        "asian_ev": 0.026,
        "asian_edge_pp": 4.5,
        "asian_firm_count": 8,
        "asian_market_complete": True,
        "asian_market_odds": None,
        "asian_market_probability": 0.495,
        "asian_market_source": "https://example.test/asian",
        "asian_market_collected_at": "2026-07-21T19:00:00+09:00",
        "asian_price_basis": "consensus",
        "asian_full_win_probability": 0.54,
        "asian_half_win_probability": 0.0,
        "asian_push_probability": 0.0,
        "asian_half_loss_probability": 0.0,
        "asian_loss_probability": 0.46,
        "asian_cover_probability": 0.55,
        "asian_cover_distribution_validated": True,
        "asian_market_signal": "aligned",
        "total_side": "under",
        "total_line": 2.5,
        "total_odds": 0.9,
        "total_odds_format": "hong_kong",
        "total_probability": 0.55,
        "total_ev": 0.045,
        "total_edge_pp": 4.5,
        "total_firm_count": 8,
        "total_market_complete": True,
        "total_market_odds": None,
        "total_market_probability": 0.505,
        "total_market_source": "https://example.test/total",
        "total_market_collected_at": "2026-07-21T19:00:00+09:00",
        "total_price_basis": "consensus",
        "total_full_win_probability": 0.55,
        "total_half_win_probability": 0.0,
        "total_push_probability": 0.0,
        "total_half_loss_probability": 0.0,
        "total_loss_probability": 0.45,
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
        "half_market_complete": False,
        "half_market_odds": None,
        "half_market_probability": None,
        "half_market_source": "https://example.test/half",
        "half_market_collected_at": "2026-07-21T19:00:00+09:00",
        "half_price_basis": "consensus",
        "half_full_win_probability": None,
        "half_half_win_probability": None,
        "half_push_probability": None,
        "half_half_loss_probability": None,
        "half_loss_probability": None,
        "half_market_signal": "unknown",
        "htft_pick": None,
        "htft_odds_format": None,
        "htft_market_complete": False,
        "htft_market_odds": None,
        "htft_market_probability": None,
        "htft_edge_pp": None,
        "htft_market_source": "https://example.test/htft",
        "htft_market_collected_at": "2026-07-21T19:00:00+09:00",
        "htft_price_basis": "consensus",
        "htft_firm_count": None,
        "htft_market_signal": "neutral",
        "goal_range_selection": None,
        "goal_range_odds": None,
        "goal_range_odds_format": None,
        "goal_range_probability": None,
        "goal_range_ev": None,
        "goal_range_edge_pp": None,
        "goal_range_firm_count": None,
        "goal_range_market_signal": "unknown",
        "goal_range_market_complete": False,
        "goal_range_market_odds": None,
        "goal_range_market_probability": 0.40,
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
        "btts_market_odds": None,
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
        "corner_total_market_odds": None,
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
        "corner_handicap_market_odds": None,
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
    if values["score_model_file"] is None:
        values["score_model_file"] = write_score_prediction(
            base_dir,
            match_id,
            home_team=values["home_team"],
            away_team=values["away_team"],
            kickoff=values["kickoff"],
        )
    if (
        values.get("primary_market") == "total"
        and values.get("total_side") in {"over", "under"}
        and "display_exact_score_pick" not in overrides
    ):
        artifact = json.loads(
            Path(values["score_model_file"]).read_text(encoding="utf-8")
        )
        matrix = artifact["score_matrix"]["probabilities"]
        ranked = sorted(
            (
                {
                    "score": f"{home}-{away}",
                    "probability": float(probability),
                    "home": home,
                    "away": away,
                }
                for home, row in enumerate(matrix)
                for away, probability in enumerate(row)
            ),
            key=lambda item: (-item["probability"], item["home"], item["away"]),
        )
        unconditional_ranks = {
            item["score"]: rank for rank, item in enumerate(ranked, start=1)
        }
        total_pick = {
            "side": values["total_side"],
            "line": values["total_line"],
        }
        branch = [
            item
            for item in ranked
            if item["probability"] > 0.0
            and memory_store.settle_total(
                total_pick, item["home"], item["away"]
            )
            in {"win", "half_win"}
        ]
        event_probability = sum(
            float(probability)
            for home, row in enumerate(matrix)
            for away, probability in enumerate(row)
            if memory_store.settle_total(total_pick, home, away)
            in {"win", "half_win"}
        )
        values["display_exact_score_pick"] = [
            f"{item['score']}:{item['probability']:.12g}:"
            f"{unconditional_ranks[item['score']]}"
            for item in branch[:2]
        ]
        values["display_exact_score_event_probability"] = event_probability
    market_specs = {
        "asian": (["home", "away"], values.get("asian_side")),
        "total": (["over", "under"], values.get("total_side")),
        "half": (
            ["home", "draw", "away"]
            if values.get("half_market") == "1x2"
            else ["home", "away"]
            if values.get("half_market") == "asian"
            else ["over", "under"],
            values.get("half_side"),
        ),
        "btts": (["yes", "no"], values.get("btts_side")),
        "corner_total": (["over", "under"], values.get("corner_total_side")),
        "corner_handicap": (["home", "away"], values.get("corner_handicap_side")),
    }
    for prefix, (outcomes, selected) in market_specs.items():
        key = f"{prefix}_market_odds"
        if values.get(key) is None and selected:
            values[key] = synthesize_market_odds(values, prefix, outcomes, selected)
    if values.get("goal_range_market_odds") is None and values.get("goal_range_selection"):
        values["goal_range_market_odds"] = synthesize_market_odds(
            values,
            "goal_range",
            ["0-1", "2-3", "4-6", "7+"],
            values["goal_range_selection"],
        )
    if values.get("htft_market_odds") is None and values.get("htft_pick"):
        selected = values["htft_pick"][0].split(":", 1)[0].upper()
        probabilities = {
            item.split(":", 1)[0].upper(): float(item.split(":", 1)[1])
            for item in values.get("htft_market_probability") or []
        }
        if selected in probabilities:
            values["htft_market_probability"] = [
                f"{key}:{value}" for key, value in probabilities.items()
            ]
            selected_odds = float(values["htft_pick"][0].split(":")[1])
            selected_raw = 1.0 / (
                selected_odds
                if values.get("htft_odds_format") == "decimal"
                else 1.0 + selected_odds
            )
            scale = selected_raw / probabilities[selected]
            values["htft_market_odds"] = []
            for outcome, probability in probabilities.items():
                raw = scale * probability
                price = (
                    1.0 / raw
                    if values.get("htft_odds_format") == "decimal"
                    else 1.0 / raw - 1.0
                )
                values["htft_market_odds"].append(f"{outcome}:{price:.12f}")
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


def strict_metric_record(
    match_id,
    *,
    probabilities,
    final_score,
    selected,
    validated_model=True,
    league="Strict Test League",
    exact_score_hit_rank=None,
):
    primary = (
        {
            "market": "total",
            "side": "under",
            "line": 2.5,
            "odds": 0.9,
            "role": "primary",
        }
        if selected
        else None
    )
    record = reviewed_record(
        match_id,
        total=primary,
        total_result="win" if selected else None,
    )
    record.update(
        {
            "league": league,
            "probabilities": probabilities,
            "final_score": final_score,
            "evaluation_eligibility": {
                "policy_version": memory_store.STRICT_OOS_POLICY_VERSION,
                "strict_forward_oos": True,
                "reason": (
                    "validated_score_model_provenance"
                    if validated_model
                    else "no_formal_core_pick"
                ),
            },
            "score_model_provenance": (
                {
                    "model_hash": "sha256:" + "1" * 64,
                    "artifact_sha256": "sha256:" + "2" * 64,
                    "snapshot": {
                        "artifact_type": "soccer_score_prediction",
                        "score_matrix": {"probabilities": [[1.0]]},
                    },
                    "score_matrix": [[1.0]],
                }
                if validated_model
                else None
            ),
            "primary_market": "total" if selected else None,
            "primary_pick": primary,
            "primary_result": "win" if selected else None,
            "learning_scope": (
                "primary" if selected else "no_primary_observation"
            ),
            "exact_score_hit_rank": exact_score_hit_rank,
            "score_exact": exact_score_hit_rank == 1,
        }
    )
    return record


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self._real_utc_now = memory_store.utc_now
        memory_store.utc_now = lambda: datetime(
            2026, 7, 21, 10, 0, tzinfo=timezone.utc
        )

    def tearDown(self):
        memory_store.utc_now = self._real_utc_now

    def test_archive_time_state_is_strict_and_forward_only(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "page_status=prematch"):
                memory_store.cmd_record(
                    record_args(base, match_id="live-page", page_status="live")
                )

            with self.assertRaisesRegex(ValueError, "at or after kickoff"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="at-kickoff",
                        kickoff="2026-07-21T19:00:00+09:00",
                        source_kickoff="2026-07-21T18:00:00+08:00",
                        user_local_kickoff="2026-07-21T19:00:00+09:00",
                    )
                )

            with self.assertRaisesRegex(ValueError, "T-30"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="early-lineup",
                        analysis_stage="lineup-check",
                        kickoff="2026-07-21T20:00:00+09:00",
                        source_kickoff="2026-07-21T19:00:00+08:00",
                        user_local_kickoff="2026-07-21T20:00:00+09:00",
                    )
                )

            with self.assertRaisesRegex(ValueError, "same instant"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="zone-mismatch",
                        user_local_kickoff="2026-07-21T19:31:00+09:00",
                        kickoff="2026-07-21T19:31:00+09:00",
                    )
                )

            with self.assertRaisesRegex(ValueError, "cannot be in the future"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="future-market",
                        total_market_collected_at="2026-07-21T19:05:00+09:00",
                    )
                )

            with self.assertRaisesRegex(ValueError, "force is disabled"):
                memory_store.cmd_record(
                    record_args(base, match_id="force-disabled", force=True)
                )

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

            reviewed = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
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
            self.assertEqual(
                reviewed["stats"]["excluded_from_strict_forward"]["matches"], 0
            )

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
                    zero_zero_probability=0.10,
                    zero_zero_rank=4,
                    total_side="over",
                    total_line=2.5,
                    total_odds=1.30,
                    total_probability=0.45,
                    total_market_probability=0.40,
                    total_edge_pp=5.0,
                    total_full_win_probability=0.45,
                    total_loss_probability=0.55,
                    total_ev=0.035,
                    display_exact_score_pick=[
                        "3-0:0.15:3",
                        "0-3:0.10:5",
                    ],
                    display_exact_score_event_probability=0.45,
                )
            )["record"]

            self.assertEqual(
                [pick["score"] for pick in created["exact_score_picks"]],
                ["1-0", "2-0"],
            )
            self.assertEqual(
                [pick["score"] for pick in created["display_exact_score_picks"]],
                ["3-0", "0-3"],
            )
            self.assertEqual(created["display_predicted_score"], "3-0")
            self.assertEqual(
                created["display_exact_score_basis"]["basis"],
                "primary_total_net_profit",
            )
            self.assertAlmostEqual(
                created["display_exact_score_picks"][0]["conditional_probability"],
                0.15 / 0.45,
            )

            reviewed = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
                    match_id="1",
                    home_score=3,
                    away_score=0,
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

        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(
                ValueError, "requires canonical primary-conditioned display scores"
            ):
                memory_store.cmd_record(
                    record_args(
                        base,
                        display_exact_score_pick=[],
                        display_exact_score_event_probability=None,
                    )
                )

        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(
                ValueError, "conditioned display score rank 1"
            ):
                memory_store.cmd_record(
                    record_args(
                        base,
                        display_exact_score_pick=[
                            "1-0:0.19:1",
                            "2-0:0.15:2",
                        ],
                        display_exact_score_event_probability=0.55,
                    )
                )

        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "canonical branch Top 2"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        display_exact_score_pick=[
                            "1-0:0.20:1",
                            "2-0:0.15:3",
                        ],
                        display_exact_score_event_probability=0.55,
                    )
                )

    def test_zero_zero_audit_is_required_consistent_and_revisioned(self):
        with tempfile.TemporaryDirectory() as base:
            initial = memory_store.cmd_record(record_args(base))["record"]
            self.assertEqual(initial["zero_zero_audit"]["rank"], 4)
            self.assertEqual(initial["zero_zero_audit"]["probability"], 0.10)
            self.assertFalse(initial["zero_zero_audit"]["included_in_top2"])

            lineup = memory_store.cmd_record(
                record_args(
                    base,
                    analysis_stage="lineup-check",
                    zero_zero_probability=0.10,
                    zero_zero_rank=4,
                )
            )["record"]
            self.assertEqual(lineup["zero_zero_audit"]["rank"], 4)
            self.assertEqual(
                lineup["revisions"][-1]["zero_zero_audit"]["rank"],
                4,
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
            self.assertIsNone(initial["record"]["asian_pick"])

            maintained = memory_store.cmd_record(
                record_args(
                    base,
                    analysis_stage="lineup-check",
                    total_odds=0.86,
                    total_ev=0.023,
                )
            )
            self.assertEqual(maintained["record"]["primary_change"]["status"], "maintained")

            with self.assertRaisesRegex(ValueError, "immutable"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        analysis_stage="lineup-check",
                        total_odds=0.84,
                        total_ev=0.012,
                    )
                )

            with self.assertRaisesRegex(ValueError, "valid only when there are no formal picks"):
                memory_store.cmd_record(record_args(base, match_id="2", primary_market="none"))
            with self.assertRaisesRegex(ValueError, "is not present"):
                memory_store.cmd_record(record_args(base, match_id="3", primary_market="half_time"))

    def test_review_persists_primary_result(self):
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(record_args(base, asian_side=None, primary_market="total"))
            result = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
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
                    total_odds=1.30,
                    total_probability=0.45,
                    total_market_probability=0.40,
                    total_edge_pp=5.0,
                    total_full_win_probability=0.45,
                    total_loss_probability=0.55,
                    total_ev=0.035,
                    data_quality="high",
                    primary_change_reason="确认首发提升进攻配置并否定原小球逻辑",
                    previous_primary_invalidated=True,
                    previous_primary_current_ev=0.04,
                )
            )["record"]
            self.assertEqual(lineup["total_pick"]["side"], "over")
            self.assertEqual(lineup["revisions"][-1]["total_pick"]["side"], "under")

            reviewed = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
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
        self.assertEqual(league["primary"]["wins"], 0)
        self.assertEqual(league["primary"]["losses"], 0)
        self.assertEqual(league["primary_by_market"]["combined"]["matches"], 0)
        self.assertEqual(league["excluded_from_strict_forward"], 2)
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
            self.assertEqual(
                profile["decision"], "hold_insufficient_strict_league_sample"
            )
            self.assertFalse(profile["parameter_change_authorized"])
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
            self.assertEqual(migrated["stats"]["primary"]["matches"], 0)
            self.assertEqual(
                migrated["stats"]["legacy_or_quarantined"]["primary"]["matches"],
                1,
            )
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
            self.assertEqual(calibration["primary_record_matches"], 0)
            self.assertEqual(
                calibration["stats"]["legacy_or_quarantined"]["primary"]["matches"],
                1,
            )
            self.assertEqual(calibration["no_primary_reviewed_matches"], 1)
            self.assertEqual(
                calibration["learning_samples"]["no_primary_observation"], 1
            )
            self.assertIn(
                "无主推学习样本1场，不计战绩",
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
            self.assertEqual(stats["primary"]["matches"], 0)
            self.assertEqual(stats["primary"]["wins"], 0)
            self.assertEqual(stats["primary"]["losses"], 0)
            self.assertEqual(stats["primary"]["pushes"], 0)
            self.assertIsNone(stats["primary"]["accuracy"])
            self.assertEqual(stats["primary"]["profit_units"], 0)
            self.assertIsNone(stats["primary"]["roi"])
            self.assertEqual(stats["primary_by_market"]["combined"]["matches"], 0)
            self.assertEqual(stats["excluded_from_strict_forward"]["matches"], 6)
            self.assertEqual(stats["all_formal"]["combined"]["monetary_scope"], "primary_only")
            self.assertEqual(stats["all_formal"]["combined"]["stake_units"], 0)
            self.assertEqual(stats["all_formal"]["combined"]["profit_units"], 0)
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
            self.assertEqual(calibration["primary_record_matches"], 0)
            self.assertEqual(
                calibration["stats"]["excluded_from_strict_forward"]["matches"], 6
            )
            self.assertEqual(
                calibration["market_status"]["asian"]["status"],
                "observation_only",
            )
            self.assertTrue(
                any(
                    item.startswith("stability-v2")
                    for item in calibration["guardrails"]
                )
            )
            self.assertTrue(
                any(
                    "EV and no-vig edge are positive eligibility gates only"
                    in item
                    for item in calibration["guardrails"]
                )
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

        self.assertEqual(stats["primary"]["profit_units"], 0)
        self.assertIsNone(stats["primary"]["roi"])
        self.assertEqual(stats["all_formal"]["combined"]["matches"], 0)
        self.assertEqual(stats["all_formal"]["combined"]["wins"], 0)
        self.assertEqual(stats["all_formal"]["combined"]["losses"], 0)
        self.assertEqual(stats["all_formal"]["combined"]["profit_units"], 0)
        self.assertIsNone(stats["all_formal"]["combined"]["roi"])
        self.assertEqual(stats["all_formal"]["asian"]["profit_units"], 0)
        self.assertEqual(stats["all_formal"]["totals"]["profit_units"], 0)
        self.assertEqual(
            stats["legacy_or_quarantined"]["primary"]["profit_units"], 0.9
        )

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
                    total_odds=0.8727272727,
                    total_edge_pp=1.5,
                    total_market_probability=0.535,
                )
            )["record"]
            self.assertAlmostEqual(sub_eight["primary_pick"]["ev"], 0.03)
            self.assertAlmostEqual(sub_eight["primary_pick"]["edge_pp"], 1.5)
            self.assertEqual(sub_eight["primary_pick"]["confidence_rank"], 1)
            self.assertEqual(
                sub_eight["primary_selection_basis"],
                "highest_independent_settlement_risk_confidence",
            )
            self.assertEqual(
                sub_eight["confidence_ranking_version"], "stability-v2"
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
                "half_odds_format": "hong_kong",
                "half_probability": 0.55,
                "half_edge_pp": 1.0,
                "half_market_signal": "aligned",
                "half_firm_count": 5,
                "half_market_complete": True,
                "half_market_probability": 0.54,
                "half_full_win_probability": 0.55,
                "half_half_win_probability": 0.0,
                "half_push_probability": 0.0,
                "half_half_loss_probability": 0.0,
                "half_loss_probability": 0.45,
                "primary_market": "half_time",
            }
            with self.assertRaisesRegex(ValueError, "observation_only"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="half-low",
                        half_ev=0.0,
                        **half_overrides,
                    )
                )
            with self.assertRaisesRegex(ValueError, "observation_only"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="half-boundary",
                        half_ev=0.045,
                        **half_overrides,
                    )
                )

            htft_overrides = {
                "asian_side": None,
                "total_side": None,
                "htft_pick": ["DD:3.40:0.31:0.054"],
                "htft_odds_format": "decimal",
                "htft_market_complete": True,
                "htft_market_probability": [
                    "HH:0.08775", "HD:0.08775", "HA:0.08775",
                    "DH:0.08775", "DD:0.298", "DA:0.08775",
                    "AH:0.08775", "AD:0.08775", "AA:0.08775",
                ],
                "htft_market_source": "https://example.test/htft",
                "htft_market_collected_at": "2026-07-21T19:00:00+09:00",
                "htft_price_basis": "consensus",
                "htft_firm_count": 5,
                "htft_market_signal": "neutral",
                "primary_market": "htft",
                "primary_htft_firm_count": 5,
            }
            with self.assertRaisesRegex(ValueError, "observation_only"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="htft-edge-low",
                        primary_htft_edge_pp=0.0,
                        **{
                            **htft_overrides,
                            "htft_market_probability": [
                                "HH:0.08625", "HD:0.08625", "HA:0.08625",
                                "DH:0.08625", "DD:0.31", "DA:0.08625",
                                "AH:0.08625", "AD:0.08625", "AA:0.08625",
                            ],
                        },
                    )
                )
            with self.assertRaisesRegex(ValueError, "observation_only"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="htft-boundary",
                        primary_htft_edge_pp=1.2,
                        **htft_overrides,
                    )
                )

    def test_stability_ranking_beats_raw_ev_and_rejects_non_rank_one_primary(self):
        candidates_v2 = {
            "total_side": None,
            "goal_range_selection": "2-3",
            "goal_range_odds": 3.0,
            "goal_range_odds_format": "decimal",
            "goal_range_probability": 0.45,
            "goal_range_market_probability": 0.40,
            "goal_range_ev": 0.35,
            "goal_range_edge_pp": 5.0,
            "goal_range_firm_count": 1,
            "goal_range_market_signal": "neutral",
            "goal_range_market_complete": True,
            "btts_side": "yes",
            "btts_odds": 4.5,
            "btts_odds_format": "decimal",
            "btts_probability": 0.25,
            "btts_market_probability": 0.23,
            "btts_ev": 0.125,
            "btts_edge_pp": 2.0,
            "btts_firm_count": 8,
            "btts_market_signal": "aligned",
            "btts_market_complete": True,
        }
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "stability-v2"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="wrong-rank-v2",
                        primary_market="goal_range",
                        **candidates_v2,
                    )
                )
            accepted = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="right-rank-v2",
                    primary_market="btts",
                    **candidates_v2,
                )
            )["record"]
            self.assertGreater(accepted["goal_range_pick"]["ev"], accepted["btts_pick"]["ev"])
            self.assertEqual(accepted["btts_pick"]["confidence_rank"], 1)
            gates = accepted["primary_pick"]["confidence_components"]["eligibility_gates"]
            self.assertFalse(gates["contributes_to_score"])
            self.assertEqual(accepted["confidence_ranking_version"], "stability-v2")

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
                        total_ev=0.08,
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
                        total_ev=0.08,
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
                        total_ev=0.08,
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
            with self.assertRaisesRegex(ValueError, "observation_only"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="asian-paused",
                        **deep_defaults,
                    )
                )

    def test_lineup_change_hysteresis_cancellation_and_no_bet_review(self):
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(record_args(base))
            cancelled = memory_store.cmd_record(
                record_args(
                    base,
                    analysis_stage="lineup-check",
                    total_side=None,
                    primary_market="none",
                    primary_change_reason="confirmed information invalidated the only formal direction",
                )
            )["record"]
            self.assertEqual(cancelled["primary_change"]["decision"], "cancelled_to_none")
            with self.assertRaisesRegex(ValueError, "immutable"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        analysis_stage="lineup-check",
                        total_side=None,
                        primary_market="none",
                        notes="different second lineup payload",
                    )
                )

            reviewed = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
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

    def test_paused_asian_worse_line_cannot_be_archived(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "observation_only"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        asian_side="home",
                        asian_line=-0.75,
                        total_side=None,
                        primary_market="asian",
                    )
                )

    def test_new_formal_market_guardrails_and_complete_odds(self):
        goal = {
            "asian_side": None,
            "total_side": None,
            "primary_market": "goal_range",
            "goal_range_selection": "2-3",
            "goal_range_odds": 2.50,
            "goal_range_odds_format": "decimal",
            "goal_range_probability": 0.45,
            "goal_range_ev": 0.125,
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
                    goal_range_market_probability=0.45,
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
                    goal_range_odds=2.30,
                    goal_range_probability=0.45,
                    goal_range_ev=0.035,
                    goal_range_edge_pp=2.0,
                    goal_range_market_probability=0.43,
                    **{
                        key: value
                        for key, value in goal.items()
                        if key
                        not in {
                            "goal_range_probability",
                            "goal_range_odds",
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
                    btts_odds=4.50,
                    btts_odds_format="decimal",
                    btts_probability=0.25,
                    btts_market_probability=0.20,
                    btts_ev=0.125,
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
            self.assertAlmostEqual(created["primary_pick"]["market_probability"], 0.40)
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
                    btts_odds=3.50,
                    btts_odds_format="hong_kong",
                    btts_probability=0.25,
                    btts_market_probability=0.20,
                    btts_ev=0.125,
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
                        btts_odds=3.50,
                        btts_odds_format="hong_kong",
                        btts_probability=0.25,
                        btts_market_probability=0.20,
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
                "verification_source": "https://example.test/final",
                "verification_collected_at": "2026-07-21T21:00:00+09:00",
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
            return review_command(SimpleNamespace(**values))

        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(
                    base,
                    match_id="goal",
                    asian_side=None,
                    total_side=None,
                    primary_market="goal_range",
                    goal_range_selection="2-3",
                    goal_range_odds=2.50,
                    goal_range_odds_format="decimal",
                    goal_range_probability=0.45,
                    goal_range_market_probability=0.40,
                    goal_range_ev=0.125,
                    goal_range_edge_pp=5.0,
                    goal_range_firm_count=5,
                    goal_range_market_signal="aligned",
                    goal_range_market_complete=True,
                    btts_side="yes",
                    btts_odds=4.50,
                    btts_odds_format="decimal",
                    btts_probability=0.25,
                    btts_market_probability=0.20,
                    btts_ev=0.125,
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
            self.assertEqual(goal_review["stats"]["primary"]["profit_units"], 1.5)
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
                    btts_odds=1.50,
                    btts_odds_format="decimal",
                    btts_probability=0.75,
                    btts_ev=0.125,
                    btts_edge_pp=5.0,
                    btts_market_probability=0.70,
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

    def test_paused_markets_cannot_be_formal_or_bypass_with_missing_provenance(self):
        with tempfile.TemporaryDirectory() as base:
            cases = (
                {
                    "match_id": "paused-asian",
                    "asian_side": "home",
                    "total_side": None,
                    "primary_market": "asian",
                },
                {
                    "match_id": "paused-half",
                    "total_side": None,
                    "half_market": "total",
                    "half_side": "under",
                    "half_line": 1.0,
                    "primary_market": "half_time",
                },
                {
                    "match_id": "paused-htft",
                    "total_side": None,
                    "htft_pick": ["DD:3.40:0.31:0.054"],
                    "primary_market": "htft",
                },
            )
            for case in cases:
                with self.subTest(case=case["match_id"]):
                    with self.assertRaisesRegex(ValueError, "observation_only"):
                        memory_store.cmd_record(
                            record_args(
                                base,
                                score_model_file="",
                                model_version=None,
                                **case,
                            )
                        )

    def test_strict_selection_policy_and_one_x_two_proper_scores(self):
        selected = strict_metric_record(
            "strict-selected",
            probabilities={"home_win": 0.6, "draw": 0.25, "away_win": 0.15},
            final_score="1-0",
            selected=True,
            exact_score_hit_rank=1,
        )
        abstained = strict_metric_record(
            "strict-abstained",
            probabilities={"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
            final_score="0-0",
            selected=False,
            exact_score_hit_rank=2,
        )
        quarantined = strict_metric_record(
            "quarantined",
            probabilities={"home_win": 0.01, "draw": 0.01, "away_win": 0.98},
            final_score="0-1",
            selected=True,
            exact_score_hit_rank=1,
        )
        quarantined["evaluation_eligibility"] = {
            "policy_version": memory_store.STRICT_OOS_POLICY_VERSION,
            "strict_forward_oos": False,
            "reason": "legacy_or_backfill",
        }

        stats = memory_store.calculate_stats([selected, abstained, quarantined])

        policy = stats["selection_policy"]
        self.assertEqual(policy["eligible_reviewed_matches"], 2)
        self.assertEqual(policy["selected_primary_matches"], 1)
        self.assertEqual(policy["abstained_matches"], 1)
        self.assertEqual(policy["coverage"], 0.5)
        self.assertEqual(policy["abstention_rate"], 0.5)
        league_key = memory_store.normalize_league_name("Strict Test League")
        league_policy = stats["leagues"][league_key]["selection_policy"]
        self.assertEqual(league_policy["eligible_reviewed_matches"], 2)
        self.assertEqual(league_policy["coverage"], 0.5)
        self.assertEqual(league_policy["abstention_rate"], 0.5)

        metrics = stats["one_x_two_metrics"]
        expected_log_loss = (-math.log(0.6) - math.log(0.3)) / 2
        self.assertEqual(metrics["strict_reviewed_matches"], 2)
        self.assertEqual(metrics["sample_count"], 2)
        self.assertEqual(metrics["excluded_count"], 0)
        self.assertEqual(metrics["excluded_reasons"], {})
        self.assertAlmostEqual(
            metrics["one_x_two_multiclass_brier"], 0.5125
        )
        self.assertAlmostEqual(
            metrics["one_x_two_multiclass_log_loss"], expected_log_loss
        )
        league_metrics = stats["leagues"][league_key]["one_x_two_metrics"]
        self.assertEqual(league_metrics["sample_count"], 2)
        self.assertAlmostEqual(
            league_metrics["one_x_two_multiclass_brier"], 0.5125
        )
        self.assertEqual(stats["excluded_from_strict_forward"]["matches"], 1)
        self.assertEqual(stats["exact_score_diagnostics"]["samples"], 2)
        self.assertEqual(stats["exact_score_diagnostics"]["excluded"], 0)

    def test_no_model_abstention_counts_coverage_but_not_model_metrics(self):
        no_model = strict_metric_record(
            "strict-no-model-abstention",
            probabilities={"home_win": 0.9, "draw": 0.05, "away_win": 0.05},
            final_score="1-0",
            selected=False,
            validated_model=False,
            exact_score_hit_rank=1,
        )
        # A trusted-looking label without the immutable artifact is not enough.
        no_model["evaluation_eligibility"]["reason"] = (
            "validated_score_model_provenance"
        )

        stats = memory_store.calculate_stats([no_model])

        self.assertEqual(
            stats["selection_policy"],
            {
                "evaluation_scope": "strict_forward_oos_reviewed",
                "policy_version": memory_store.CONFIDENCE_POLICY_VERSION,
                "selection_basis": memory_store.PRIMARY_SELECTION_BASIS,
                "eligible_reviewed_matches": 1,
                "selected_primary_matches": 0,
                "abstained_matches": 1,
                "coverage": 0.0,
                "abstention_rate": 1.0,
            },
        )
        metrics = stats["one_x_two_metrics"]
        self.assertEqual(metrics["sample_count"], 0)
        self.assertEqual(metrics["excluded_count"], 1)
        self.assertEqual(
            metrics["excluded_reasons"],
            {"missing_validated_score_model_provenance": 1},
        )
        self.assertIsNone(metrics["one_x_two_multiclass_brier"])
        self.assertIsNone(metrics["one_x_two_multiclass_log_loss"])
        self.assertEqual(stats["exact_score_diagnostics"]["samples"], 0)
        self.assertEqual(stats["exact_score_diagnostics"]["excluded"], 1)
        self.assertEqual(stats["exact_score_top1_hits"], 0)

    def test_frozen_evaluation_eligibility_controls_reviewed_cohort(self):
        frozen = strict_metric_record(
            "frozen-eligibility",
            probabilities={"home_win": 0.6, "draw": 0.25, "away_win": 0.15},
            final_score="1-0",
            selected=True,
        )
        frozen["settlement_basis"] = {
            "evaluation_eligibility": dict(frozen["evaluation_eligibility"]),
            "score_model_provenance": frozen["score_model_provenance"],
            "primary_market": "total",
            "primary_pick": frozen["primary_pick"],
        }
        frozen["evaluation_eligibility"] = {
            "strict_forward_oos": False,
            "reason": "top_level_drift_must_not_win",
        }
        frozen_stats = memory_store.calculate_stats([frozen])
        self.assertEqual(frozen_stats["strict_forward_reviewed_matches"], 1)
        self.assertEqual(frozen_stats["primary"]["matches"], 1)
        self.assertEqual(frozen_stats["one_x_two_metrics"]["sample_count"], 1)

        missing_frozen_metadata = strict_metric_record(
            "missing-frozen-eligibility",
            probabilities={"home_win": 0.6, "draw": 0.25, "away_win": 0.15},
            final_score="1-0",
            selected=True,
        )
        missing_frozen_metadata["settlement_basis"] = {
            "primary_market": "total",
            "primary_pick": missing_frozen_metadata["primary_pick"],
        }
        missing_stats = memory_store.calculate_stats([missing_frozen_metadata])
        self.assertEqual(missing_stats["strict_forward_reviewed_matches"], 0)
        self.assertEqual(missing_stats["excluded_from_strict_forward"]["matches"], 1)

    def test_one_x_two_metrics_quarantine_bad_or_missing_strict_rows(self):
        missing_probabilities = strict_metric_record(
            "missing-probabilities",
            probabilities=None,
            final_score="1-0",
            selected=False,
        )
        invalid_score = strict_metric_record(
            "invalid-score",
            probabilities={"home_win": 0.6, "draw": 0.25, "away_win": 0.15},
            final_score="not-a-score",
            selected=False,
        )
        non_normalized = strict_metric_record(
            "non-normalized",
            probabilities={"home_win": 0.6, "draw": 0.3, "away_win": 0.2},
            final_score="1-0",
            selected=False,
        )

        metrics = memory_store.calculate_stats(
            [missing_probabilities, invalid_score, non_normalized]
        )["one_x_two_metrics"]

        self.assertEqual(metrics["sample_count"], 0)
        self.assertEqual(metrics["excluded_count"], 3)
        self.assertEqual(
            metrics["excluded_reasons"],
            {
                "invalid_final_score": 1,
                "missing_1x2_probabilities": 1,
                "non_normalized_1x2_probabilities": 1,
            },
        )
        self.assertIsNone(metrics["one_x_two_multiclass_brier"])
        self.assertIsNone(metrics["one_x_two_multiclass_log_loss"])

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

        self.assertEqual(stats["primary"]["matches"], 0)
        self.assertEqual(stats["primary"]["profit_units"], 0)
        self.assertEqual(stats["primary_by_market"]["totals"]["matches"], 0)
        self.assertEqual(stats["primary_by_market"]["asian"]["matches"], 0)
        self.assertEqual(
            stats["legacy_or_quarantined"]["primary_by_market"]["totals"]["matches"],
            1,
        )
        self.assertEqual(
            stats["legacy_or_quarantined"]["primary"]["profit_units"], 0.9
        )
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
        self.assertEqual(legacy_stats["primary_by_market"]["asian"]["matches"], 0)
        self.assertEqual(legacy_stats["primary"]["profit_units"], 0)
        self.assertEqual(
            legacy_stats["legacy_or_quarantined"]["primary_by_market"]["asian"]["matches"],
            1,
        )
        self.assertEqual(
            legacy_stats["legacy_or_quarantined"]["primary"]["profit_units"],
            -1.0,
        )


if __name__ == "__main__":
    unittest.main()

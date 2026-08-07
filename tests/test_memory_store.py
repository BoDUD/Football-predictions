from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _corner_source_fixture import build_source_bound_dataset

from scripts import (
    htft_model,
    joint_scenario_model,
    prediction_card_renderer,
    review_card_renderer,
    score_model,
    source_evidence,
)

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

HTFT_OBSERVATION_MATRIX = {
    "HH": 0.30,
    "HD": 0.05,
    "HA": 0.02,
    "DH": 0.20,
    "DD": 0.15,
    "DA": 0.08,
    "AH": 0.03,
    "AD": 0.05,
    "AA": 0.12,
}

CORNER_TEAMS = ("A", "B", "C", "D")

JOINT_SAMPLE_ROWS = [
    ("2025-01-01", "Alpha", "Bravo", 2, 0, 1, 0),
    ("2025-01-08", "Charlie", "Delta", 1, 1, 0, 1),
    ("2025-01-15", "Bravo", "Charlie", 0, 1, 0, 0),
    ("2025-01-22", "Delta", "Alpha", 1, 3, 1, 1),
    ("2025-02-01", "Alpha", "Charlie", 2, 1, 1, 0),
    ("2025-02-08", "Bravo", "Delta", 1, 0, 0, 0),
    ("2025-02-15", "Charlie", "Alpha", 2, 2, 1, 2),
    ("2025-02-22", "Delta", "Bravo", 0, 0, 0, 0),
    ("2025-03-01", "Alpha", "Delta", 3, 1, 2, 0),
    ("2025-03-08", "Charlie", "Bravo", 1, 0, 0, 0),
    ("2025-03-15", "Bravo", "Alpha", 1, 2, 1, 1),
    ("2025-03-22", "Delta", "Charlie", 2, 1, 0, 1),
]
JOINT_FIXTURE_ID = "joint-fixture"
JOINT_INPUT_GENERATED_AT = "2026-07-21T09:54:00Z"
JOINT_GENERATED_AT = "2026-07-21T09:55:00Z"
JOINT_KICKOFF = "2026-07-21T10:30:00Z"


def write_corner_history(path: Path, *, days: int = 16) -> None:
    schedules = (
        (("A", "B"), ("C", "D")),
        (("A", "C"), ("D", "B")),
        (("A", "D"), ("B", "C")),
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["date", "home_team", "away_team", "home_corners", "away_corners"]
        )
        for day in range(days):
            match_date = date(2025, 1, 1) + timedelta(days=day)
            for home, away in schedules[day % len(schedules)]:
                writer.writerow(
                    [
                        match_date.isoformat(),
                        home,
                        away,
                        3 + (2 * CORNER_TEAMS.index(home) + day) % 7,
                        2 + (CORNER_TEAMS.index(away) + 2 * day) % 6,
                    ]
                )


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


def write_htft_observation_files(
    base_dir: str,
    match_id: str,
    *,
    matrix=None,
    half_time_anchor=None,
    drop_ranker_anchor=False,
):
    probabilities = dict(matrix or HTFT_OBSERVATION_MATRIX)
    half = {
        result: math.fsum(
            probabilities[f"{result}{full_result}"] for full_result in "HDA"
        )
        for result in "HDA"
    }
    full = {
        result: math.fsum(
            probabilities[f"{half_result}{result}"] for half_result in "HDA"
        )
        for result in "HDA"
    }

    def named_marginal(values):
        return {
            "home": values["H"],
            "draw": values["D"],
            "away": values["A"],
        }

    model = {
        "artifact_type": "soccer_htft_prediction",
        "schema_version": "1.0.0",
        "model_version": "htft-dixon-coles-ipf/1.0.0",
        "model_hash": "sha256:" + "3" * 64,
        "generated_at": "2026-07-21T18:55:00+09:00",
        "fixture": {
            "home_team": "主队",
            "away_team": "客队",
            "kickoff": "2026-07-21T19:30:00+09:00",
            "unknown_team_policy": "error",
        },
        "provenance": {
            "generated_before_kickoff": True,
            "strictly_before_kickoff_utc_date": True,
            "training_cutoff_date": "2026-07-20",
            "external_anchor_enabled": half_time_anchor is not None,
            "training": {
                "competition_key": "test_league",
                "source_data_hash": "sha256:" + "5" * 64,
                "dataset_manifest_hash": "sha256:" + "6" * 64,
                "start_date": "2020-01-01",
                "end_date": "2026-07-20",
            },
            "marginal_targets": {
                "half_time": (
                    {
                        "origin": "external_de_vigged_anchor",
                        "de_vigged": True,
                        "source": half_time_anchor["source"],
                        "captured_at": half_time_anchor["captured_at"],
                        "probabilities": named_marginal(half),
                    }
                    if half_time_anchor is not None
                    else {
                        "origin": "model_component",
                        "probabilities": named_marginal(half),
                    }
                ),
                "full_time": {
                    "origin": "model_component",
                    "probabilities": named_marginal(full),
                },
            },
        },
        "htft": {"code_probabilities": probabilities},
    }
    model["prediction_hash"] = memory_store.canonical_prediction_hash(model)
    ranker = memory_store.htft_ranker.rank_htft(
        probabilities,
        half,
        full,
        league_key="test_league",
        model_hash=model["model_hash"],
        anchor_context=(None if drop_ranker_anchor else half_time_anchor),
    )
    model_path = Path(base_dir) / f"htft-model-{match_id}.json"
    ranker_path = Path(base_dir) / f"htft-ranker-{match_id}.json"
    model_path.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    ranker_path.write_text(json.dumps(ranker, ensure_ascii=False), encoding="utf-8")
    return str(model_path), str(ranker_path)


def write_corner_observation_files(
    base_dir: str,
    match_id: str,
    model_dir: Path,
    prediction: dict,
    ranking: dict,
) -> tuple[str, str, str]:
    prediction_path = Path(base_dir) / f"corner-prediction-{match_id}.json"
    ranking_path = Path(base_dir) / f"corner-ranking-{match_id}.json"
    prediction_path.write_text(
        json.dumps(prediction, ensure_ascii=False), encoding="utf-8"
    )
    ranking_path.write_text(json.dumps(ranking, ensure_ascii=False), encoding="utf-8")
    return str(model_dir), str(prediction_path), str(ranking_path)


def synthesize_market_odds(values, prefix, outcomes, selected):
    odds = values.get(f"{prefix}_odds")
    market_probability = values.get(f"{prefix}_market_probability")
    odds_format = values.get(f"{prefix}_odds_format")
    if (
        odds is None
        or market_probability is None
        or not selected
        or odds_format is None
    ):
        return None
    selected_raw = 1.0 / (
        float(odds) if odds_format == "decimal" else 1.0 + float(odds)
    )
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
    memory_store.utc_now = lambda: datetime(2026, 7, 21, 13, 0, tzinfo=timezone.utc)
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
        "competition_key": None,
        "competition_label": None,
        "competition_id": None,
        "competition_verification_source": None,
        "competition_source_locator": None,
        "competition_collected_at": None,
        "notes": "",
        "model_version": "dixon-coles-time-decay/1.0.0",
        "score_model_file": None,
        "joint_scenario_file": None,
        "require_complete_analysis": False,
        "candidate_evaluation_file": None,
        "require_candidate_evaluations": False,
        "htft_observation_model_file": None,
        "htft_observation_ranker_file": None,
        "corner_observation_model_dir": None,
        "corner_observation_prediction_file": None,
        "corner_observation_ranker_file": None,
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
            and memory_store.settle_total(total_pick, item["home"], item["away"])
            in {"win", "half_win"}
        ]
        event_probability = sum(
            float(probability)
            for home, row in enumerate(matrix)
            for away, probability in enumerate(row)
            if memory_store.settle_total(total_pick, home, away) in {"win", "half_win"}
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
    if values.get("goal_range_market_odds") is None and values.get(
        "goal_range_selection"
    ):
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


def competition_evidence_values(match_id: str = "2991125") -> dict[str, str]:
    source_url = f"https://zq.titan007.com/analysis/{match_id}cn.htm"
    return {
        "competition_key": "brazil_cup",
        "competition_label": "巴西杯",
        "competition_id": "186",
        "verification_source": source_url,
        "source_locator": "//info.titan007.com/cup_match/2026-2027/cupmatch_vs/cupmatch_186.htm",
        "collected_at": "2026-07-21T19:00:00+09:00",
    }


def fake_competition_snapshot(record: dict) -> dict:
    source_url = str(record.get("source_url") or "")
    return {
        "source_url": source_url,
        "response_url": source_url,
        "page_sha256": "sha256:" + "b" * 64,
        "etag": 'W/"fixture"',
        "last_modified": "Tue, 21 Jul 2026 10:00:00 GMT",
        "collected_at": memory_store.utc_now().isoformat(),
        "header": {
            "home_team": str(record.get("home_team") or ""),
            "away_team": str(record.get("away_team") or ""),
            "competition_label": "巴西杯",
            "competition_id": "186",
            "competition_locator": "//info.titan007.com/cup_match/2026-2027/cupmatch_vs/cupmatch_186.htm",
        },
    }


def attach_competition_args(
    base_dir: str, match_id: str = "2991125", **overrides
) -> SimpleNamespace:
    values = {
        "base_dir": base_dir,
        "match_id": match_id,
        **competition_evidence_values(match_id),
        "write": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def reviewed_record(
    match_id,
    asian=None,
    asian_result=None,
    total=None,
    total_result=None,
    half=None,
    half_result=None,
):
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
            "learning_scope": ("primary" if selected else "no_primary_observation"),
            "exact_score_hit_rank": exact_score_hit_rank,
            "score_exact": exact_score_hit_rank == 1,
        }
    )
    return record


class MemoryStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corner_temporary = tempfile.TemporaryDirectory()
        cls.corner_base = Path(cls.corner_temporary.name)
        joint_history = cls.corner_base / "joint-history.csv"
        with joint_history.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(
                [
                    "date",
                    "home_team",
                    "away_team",
                    "home_goals",
                    "away_goals",
                    "half_home_goals",
                    "half_away_goals",
                ]
            )
            writer.writerows(JOINT_SAMPLE_ROWS)
        cls.joint_model = htft_model.fit_model(
            joint_history,
            iterations=30,
            learning_rate=0.025,
            regularization=0.03,
            half_time_half_life_days=180.0,
            second_half_half_life_days=180.0,
            full_time_half_life_days=180.0,
            competition_key="test_league",
            dataset_manifest_hash="sha256:" + "a" * 64,
        )
        cls.joint_model["generated_at"] = "2026-07-20T00:00:00Z"
        for component in cls.joint_model["components"].values():
            component["generated_at"] = "2026-07-20T00:00:00Z"
        htft_model.validate_model(cls.joint_model)
        cls.joint_htft_prediction = htft_model.predict_model(
            cls.joint_model,
            "Alpha",
            "Bravo",
            kickoff=JOINT_KICKOFF,
            generated_at=JOINT_INPUT_GENERATED_AT,
            max_goals=12,
            hard_max_goals=30,
        )
        cls.joint_score_prediction = score_model.predict_model(
            cls.joint_model["components"]["full_time"],
            "Alpha",
            "Bravo",
            kickoff=JOINT_KICKOFF,
            generated_at=JOINT_INPUT_GENERATED_AT,
            max_goals=12,
            hard_max_goals=30,
        )
        cls.joint_prediction = joint_scenario_model.predict_joint_scenarios(
            cls.joint_model,
            cls.joint_score_prediction,
            cls.joint_htft_prediction,
            generated_at=JOINT_GENERATED_AT,
            expected_match_id=JOINT_FIXTURE_ID,
        )
        matrix = cls.joint_htft_prediction["htft"]["code_probabilities"]
        half = {
            result: math.fsum(matrix[f"{result}{full}"] for full in "HDA")
            for result in "HDA"
        }
        full = {
            result: math.fsum(matrix[f"{half_result}{result}"] for half_result in "HDA")
            for result in "HDA"
        }
        cls.joint_htft_ranking = memory_store.htft_ranker.rank_htft(
            matrix,
            half,
            full,
            league_key="test_league",
            model_hash=cls.joint_htft_prediction["model_hash"],
        )
        source_dir = cls.corner_base / "source"
        source_dir.mkdir()
        cls.corner_history, _manifest = build_source_bound_dataset(
            source_dir,
            target_league_key="korea_k_league_1",
        )
        cls.corner_model_dir = cls.corner_base / "models"
        manager = memory_store.corner_ranker.corner_model_manager
        manager.train_registered_model(
            cls.corner_history,
            cls.corner_model_dir,
            league_key="korea_k_league_1",
            generated_at="2026-07-20T00:00:00Z",
            half_life_days=120.0,
            iterations=20,
            learning_rate=0.025,
            regularization=0.03,
            min_train_matches=8,
            test_block_size=5,
            hard_max_corners=70,
        )
        cls.corner_prediction = manager.predict_registered_model(
            cls.corner_model_dir,
            "korea_k_league_1",
            "A",
            "B",
            kickoff="2026-07-21T10:30:00Z",
            generated_at="2026-07-21T09:30:00Z",
            total_markets=(("over", 8.5), ("under", 8.5)),
            corner_handicaps=(("home", -0.5), ("away", 0.5)),
        )
        cls.corner_ranking = memory_store.corner_ranker.rank_corner_markets(
            cls.corner_prediction,
            [
                {
                    "market": "corner_total",
                    "line": 8.5,
                    "odds_format": "decimal",
                    "complete_market_odds": {"over": 2.10, "under": 2.10},
                    "firm_count": 3,
                    "market_complete": True,
                    "market_source": "Titan007 consensus",
                    "market_collected_at": "2026-07-21T09:45:00Z",
                    "price_basis": "consensus",
                    "market_signal": "neutral",
                },
                {
                    "market": "corner_handicap",
                    "line": -0.5,
                    "odds_format": "decimal",
                    "complete_market_odds": {"home": 2.10, "away": 2.10},
                    "firm_count": 3,
                    "market_complete": True,
                    "market_source": "Titan007 consensus",
                    "market_collected_at": "2026-07-21T09:45:00Z",
                    "price_basis": "median",
                    "market_signal": "aligned",
                },
            ],
            model_dir=cls.corner_model_dir,
            generated_at="2026-07-21T09:55:00Z",
            data_quality="high",
            corner_profile_evidence={
                "available": True,
                "independent_from_goal_model": True,
                "source": "audited corner profile feed",
                "collected_at": "2026-07-21T09:40:00Z",
                "summary": "home/away corner rates and width profile agree",
                "components": [
                    "home_away_corners_for_against",
                    "width_crossing",
                ],
            },
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.corner_temporary.cleanup()

    def corner_record_args(
        self,
        base: str,
        match_id: str,
        *,
        prediction: dict | None = None,
        ranking: dict | None = None,
        **overrides,
    ) -> SimpleNamespace:
        model_dir, prediction_file, ranking_file = write_corner_observation_files(
            base,
            match_id,
            self.corner_model_dir,
            copy.deepcopy(prediction or self.corner_prediction),
            copy.deepcopy(ranking or self.corner_ranking),
        )
        return record_args(
            base,
            match_id=match_id,
            league="korea_k_league_1",
            home_team="A",
            away_team="B",
            asian_side=None,
            total_side=None,
            primary_market="none",
            model_version=None,
            score_model_file="",
            corner_observation_model_dir=model_dir,
            corner_observation_prediction_file=prediction_file,
            corner_observation_ranker_file=ranking_file,
            **overrides,
        )

    def joint_record_args(
        self,
        base: str,
        *,
        joint_prediction: dict | None = None,
        score_prediction: dict | None = None,
        htft_prediction: dict | None = None,
        htft_ranking: dict | None = None,
        **overrides,
    ) -> SimpleNamespace:
        score = copy.deepcopy(score_prediction or self.joint_score_prediction)
        htft = copy.deepcopy(htft_prediction or self.joint_htft_prediction)
        ranking = copy.deepcopy(htft_ranking or self.joint_htft_ranking)
        joint = copy.deepcopy(joint_prediction or self.joint_prediction)
        paths = {
            "score_model_file": Path(base) / "joint-score.json",
            "htft_observation_model_file": Path(base) / "joint-htft.json",
            "htft_observation_ranker_file": Path(base) / "joint-htft-ranker.json",
            "joint_scenario_file": Path(base) / "joint-scenario.json",
        }
        for key, payload in (
            ("score_model_file", score),
            ("htft_observation_model_file", htft),
            ("htft_observation_ranker_file", ranking),
            ("joint_scenario_file", joint),
        ):
            paths[key].write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

        matrix = score["score_matrix"]["probabilities"]
        ranked = sorted(
            (
                (float(probability), home, away)
                for home, row in enumerate(matrix)
                for away, probability in enumerate(row)
            ),
            key=lambda item: (-item[0], item[1], item[2]),
        )
        top_two = ranked[:2]
        zero_zero_rank = next(
            rank
            for rank, (_probability, home, away) in enumerate(ranked, start=1)
            if home == 0 and away == 0
        )
        one_x_two = score["one_x_two"]
        return record_args(
            base,
            match_id=JOINT_FIXTURE_ID,
            league="test_league",
            home_team="Alpha",
            away_team="Bravo",
            predicted_score=f"{top_two[0][1]}-{top_two[0][2]}",
            exact_score_pick=[
                f"{home}-{away}:{probability:.17g}"
                for probability, home, away in top_two
            ],
            zero_zero_probability=float(matrix[0][0]),
            zero_zero_rank=zero_zero_rank,
            home_win_probability=float(one_x_two["home"]),
            draw_probability=float(one_x_two["draw"]),
            away_win_probability=float(one_x_two["away"]),
            primary_market="none",
            asian_side=None,
            total_side=None,
            model_version=score["model_version"],
            **{key: str(path) for key, path in paths.items()},
            **overrides,
        )

    def write_candidate_evaluation_file(
        self,
        base: str,
        *,
        probability_offset: float = 0.0,
    ) -> str:
        matrix = self.joint_prediction["full_time_score_marginal"]["probabilities"]
        distributions = {
            side: memory_store.matrix_settlement_distribution(
                matrix, "asian", {"market": "asian", "side": side, "line": 0.0}
            )
            for side in ("home", "away")
        }
        side = max(
            distributions,
            key=lambda value: distributions[value]["full_win"],
        )
        other = "away" if side == "home" else "home"
        distribution = distributions[side]
        market_identity = {
            "family": "asian",
            "period": "full_time",
            "line": 0.0,
            "price_outcomes": ["home", "away"],
        }
        payload = {
            "artifact_type": memory_store.CANDIDATE_EVALUATION_ARTIFACT_TYPE,
            "schema_version": memory_store.CANDIDATE_EVALUATION_SCHEMA_VERSION,
            "policy_version": memory_store.STRICT_OOS_POLICY_VERSION,
            "selection_policy_version": memory_store.CONFIDENCE_POLICY_VERSION,
            "generated_at": "2026-07-21T09:56:00Z",
            "fixture": {
                "match_id": JOINT_FIXTURE_ID,
                "home_team": "Alpha",
                "away_team": "Bravo",
                "kickoff": JOINT_KICKOFF,
            },
            "market_manifest": [
                {
                    "market": market,
                    "status": "evaluated" if market == "asian" else "unavailable",
                    "reasons": [] if market == "asian" else ["not_collected_for_test"],
                }
                for market in memory_store.PRIMARY_MARKETS
            ],
            "candidates": [
                {
                    "market": "asian",
                    "side": side,
                    "line": 0.0,
                    "market_identity": market_identity,
                    "market_identity_hash": source_evidence.market_identity_hash(
                        market_identity
                    ),
                    "settlement_reference_outcome": side,
                    "probability": distribution["full_win"]
                    + distribution["half_win"]
                    + probability_offset,
                    "settlement_probabilities": distribution,
                    "odds": 4.0,
                    "odds_format": "decimal",
                    "market_complete": True,
                    "complete_market_odds": {side: 4.0, other: 1.10},
                    "market_source": "Titan007 consensus",
                    "market_collected_at": "2026-07-21T09:50:00Z",
                    "price_basis": "consensus",
                    "firm_count": 5,
                    "market_signal": "aligned",
                }
            ],
        }
        path = Path(base) / "candidate-evaluation.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_record_parser_exposes_joint_scenario_file(self):
        arguments = [
            "record",
            "--match-id",
            "fixture",
            "--league",
            "test_league",
            "--kickoff",
            "2026-07-21T19:30:00+09:00",
            "--page-status",
            "prematch",
            "--source-kickoff",
            "2026-07-21T18:30:00+08:00",
            "--source-timezone",
            "Asia/Shanghai",
            "--user-local-kickoff",
            "2026-07-21T19:30:00+09:00",
            "--user-timezone",
            "Asia/Tokyo",
            "--home-team",
            "Alpha",
            "--away-team",
            "Bravo",
            "--predicted-score",
            "1-0",
            "--zero-zero-probability",
            "0.1",
            "--zero-zero-rank",
            "4",
            "--primary-market",
            "none",
            "--joint-scenario-file",
            "joint.json",
            "--require-complete-analysis",
        ]
        parsed = memory_store.build_parser().parse_args(arguments)
        self.assertEqual(parsed.joint_scenario_file, "joint.json")
        self.assertTrue(parsed.require_complete_analysis)
        self.assertIsNone(parsed.candidate_evaluation_file)
        self.assertFalse(parsed.require_candidate_evaluations)

        defaulted = memory_store.build_parser().parse_args(arguments[:-1])
        self.assertTrue(defaulted.require_complete_analysis)

    def test_candidate_evaluation_v3_archives_shadow_without_formal_pick(self):
        with tempfile.TemporaryDirectory() as base:
            artifact = self.write_candidate_evaluation_file(base)
            created = memory_store.cmd_record(
                self.joint_record_args(
                    base,
                    candidate_evaluation_file=artifact,
                    require_candidate_evaluations=True,
                )
            )["record"]

            audit = next(
                item
                for item in created["candidate_audits"]
                if item.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
            )
            self.assertTrue(
                memory_store.validated_candidate_evaluation_audit(audit, created)
            )
            self.assertIsNone(created["primary_market"])
            self.assertIsNone(created["primary_pick"])
            candidate = audit["candidates"][0]
            self.assertTrue(candidate["counterfactual_eligible"])
            self.assertFalse(candidate["formal_eligible"])
            self.assertTrue(candidate["shadow_selected"])
            self.assertEqual(candidate["shadow_rank"], 1)
            self.assertIn("market_policy_enabled", candidate["release_blockers"])
            self.assertEqual(
                audit["shadow_selections"]["asian"], candidate["candidate_id"]
            )

    def test_candidate_evaluation_v3_rejects_missing_or_tampered_input(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(
                ValueError, "requires --candidate-evaluation-file"
            ):
                memory_store.cmd_record(
                    self.joint_record_args(
                        base,
                        require_candidate_evaluations=True,
                        candidate_evaluation_file=None,
                    )
                )

        with tempfile.TemporaryDirectory() as base:
            artifact = self.write_candidate_evaluation_file(
                base, probability_offset=0.01
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                memory_store.cmd_record(
                    self.joint_record_args(
                        base,
                        candidate_evaluation_file=artifact,
                        require_candidate_evaluations=True,
                    )
                )

        with tempfile.TemporaryDirectory() as base:
            artifact = self.write_candidate_evaluation_file(base)
            payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
            payload["market_manifest"].pop()
            Path(artifact).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "every supported market"):
                memory_store.cmd_record(
                    self.joint_record_args(
                        base,
                        candidate_evaluation_file=artifact,
                        require_candidate_evaluations=True,
                    )
                )

    def test_candidate_evaluation_v3_settles_shadow_and_triggers_review_only(self):
        with tempfile.TemporaryDirectory() as base:
            artifact = self.write_candidate_evaluation_file(base)
            memory_store.cmd_record(
                self.joint_record_args(
                    base,
                    candidate_evaluation_file=artifact,
                    require_candidate_evaluations=True,
                )
            )
            reviewed = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
                    match_id=JOINT_FIXTURE_ID,
                    home_score=1,
                    away_score=0,
                    half_home_score=0,
                    half_away_score=0,
                    home_corners=None,
                    away_corners=None,
                    key_learning="candidate evaluation shadow settlement test",
                )
            )
            record = reviewed["record"]
            diagnostic = next(
                item
                for item in record["observation_diagnostics"]
                if item.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
            )
            self.assertEqual(diagnostic["status"], "graded_observation")
            self.assertEqual(len(diagnostic["candidate_results"]), 1)
            self.assertIsNotNone(
                diagnostic["candidate_results"][0]["settlement_result"]
            )
            self.assertEqual(reviewed["stats"]["primary"]["matches"], 0)
            self.assertEqual(reviewed["stats"]["primary"]["profit_units"], 0)
            shadow = reviewed["stats"]["shadow_selection_by_market"]["markets"]["asian"]
            self.assertEqual(shadow["shadow_selected"], 1)
            self.assertEqual(shadow["graded_shadow_selections"], 1)
            release = reviewed["stats"]["release_blocker_funnel"]["markets"]["asian"]
            self.assertEqual(
                release["release_gates"]["market_policy_enabled"]["failed"], 1
            )

            seed = copy.deepcopy(record)
            mismatched = copy.deepcopy(seed)
            mismatched["match_id"] = "candidate-shadow-mismatched"
            mismatched_stats = memory_store.calculate_stats([mismatched])
            self.assertEqual(
                mismatched_stats["shadow_selection_by_market"]["markets"]["asian"][
                    "graded_shadow_selections"
                ],
                0,
            )
            duplicate_stats = memory_store.calculate_stats([seed, copy.deepcopy(seed)])
            self.assertEqual(
                duplicate_stats["shadow_selection_by_market"]["markets"]["asian"][
                    "graded_shadow_selections"
                ],
                1,
            )

            def rebound_clone(index: int) -> dict:
                clone = copy.deepcopy(seed)
                match_id = f"candidate-shadow-{index}"
                clone["match_id"] = match_id
                basis = clone["settlement_basis"]
                audit = next(
                    item
                    for item in basis["candidate_audits"]
                    if item.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
                )
                old_to_new: dict[str, str] = {}
                artifact_sha = (
                    "sha256:" + hashlib.sha256(match_id.encode("utf-8")).hexdigest()
                )
                audit["fixture"]["match_id"] = match_id
                audit["artifact"]["artifact_sha256"] = artifact_sha
                audit["observation_id"] = artifact_sha
                for candidate in audit["candidates"]:
                    old_id = candidate["candidate_id"]
                    new_id = (
                        "sha256:"
                        + hashlib.sha256(
                            (
                                f"{artifact_sha}:{candidate['source_index']}:"
                                f"{candidate['identity']}"
                            ).encode("utf-8")
                        ).hexdigest()
                    )
                    candidate["candidate_id"] = new_id
                    old_to_new[old_id] = new_id
                audit["shadow_selections"] = {
                    market: old_to_new[candidate_id]
                    for market, candidate_id in audit["shadow_selections"].items()
                }
                audit["audit_hash"] = (
                    memory_store.calculate_candidate_evaluation_audit_hash(audit)
                )
                diagnostic = next(
                    item
                    for item in basis["observation_diagnostics"]
                    if item.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
                )
                diagnostic["observation_id"] = artifact_sha
                for result in diagnostic["candidate_results"]:
                    result["candidate_id"] = old_to_new[result["candidate_id"]]
                return clone

            forged = [rebound_clone(index) for index in range(20)]
            forged_stats = memory_store.calculate_stats(forged)
            self.assertEqual(
                forged_stats["shadow_selection_by_market"]["markets"]["asian"][
                    "graded_shadow_selections"
                ],
                0,
            )

            shadow_summary = {
                "markets": {
                    market: {"graded_shadow_selections": 20 if market == "asian" else 0}
                    for market in memory_store.PRIMARY_MARKETS
                }
            }
            threshold = memory_store.shadow_review_trigger_by_market(shadow_summary, 20)
            self.assertTrue(threshold["asian"])
            self.assertFalse(threshold["total"])

    def test_candidate_evaluation_v3_enforces_temporal_causality(self):
        cases = (
            (
                "market-after-candidate",
                {"market_collected_at": "2026-07-21T09:57:00Z"},
                None,
                "cannot precede market_collected_at",
            ),
            (
                "candidate-before-model",
                {},
                "2026-07-21T09:54:00Z",
                "cannot precede its upstream model",
            ),
        )
        for label, candidate_changes, generated_at, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as base:
                artifact = self.write_candidate_evaluation_file(base)
                payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
                payload["candidates"][0].update(candidate_changes)
                if generated_at is not None:
                    payload["generated_at"] = generated_at
                Path(artifact).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, expected):
                    memory_store.cmd_record(
                        self.joint_record_args(
                            base,
                            candidate_evaluation_file=artifact,
                            require_candidate_evaluations=True,
                        )
                    )

        with tempfile.TemporaryDirectory() as base:
            artifact = self.write_candidate_evaluation_file(base)
            payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
            payload["generated_at"] = "2026-07-21T09:55:00Z"
            Path(artifact).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            created = memory_store.cmd_record(
                self.joint_record_args(
                    base,
                    candidate_evaluation_file=artifact,
                    require_candidate_evaluations=True,
                )
            )["record"]
            audit = next(
                item
                for item in created["candidate_audits"]
                if item.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
            )
            self.assertTrue(
                memory_store.validated_candidate_evaluation_audit(audit, created)
            )

    def test_candidate_evaluation_v3_uses_five_state_quarter_line_edge(self):
        matrix = self.joint_prediction["full_time_score_marginal"]["probabilities"]
        for line in (-0.25, -0.75):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as base:
                artifact = self.write_candidate_evaluation_file(base)
                payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
                raw = payload["candidates"][0]
                side = raw["side"]
                other = "away" if side == "home" else "home"
                distribution = memory_store.matrix_settlement_distribution(
                    matrix,
                    "asian",
                    {"market": "asian", "side": side, "line": line},
                )
                raw.update(
                    {
                        "line": line,
                        "probability": distribution["full_win"]
                        + distribution["half_win"],
                        "settlement_probabilities": distribution,
                        "odds": 2.0,
                        "complete_market_odds": {side: 2.0, other: 2.0},
                        "cover_distribution_validated": True,
                    }
                )
                raw["market_identity"]["line"] = line if side == "home" else -line
                raw["market_identity_hash"] = source_evidence.market_identity_hash(
                    raw["market_identity"]
                )
                Path(artifact).write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
                created = memory_store.cmd_record(
                    self.joint_record_args(
                        base,
                        candidate_evaluation_file=artifact,
                        require_candidate_evaluations=True,
                    )
                )["record"]
                audit = next(
                    item
                    for item in created["candidate_audits"]
                    if item.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
                )
                candidate = audit["candidates"][0]
                edge_probability = memory_store.effective_settlement_win_probability(
                    distribution, "test distribution"
                )
                self.assertIsNotNone(edge_probability)
                self.assertAlmostEqual(candidate["edge_probability"], edge_probability)
                self.assertAlmostEqual(
                    candidate["edge_pp"], (edge_probability - 0.5) * 100.0
                )
                old_binary_edge = (candidate["probability"] - 0.5) * 100.0
                self.assertNotAlmostEqual(candidate["edge_pp"], old_binary_edge)
        self.assertIsNone(
            memory_store.effective_settlement_win_probability(
                {
                    "full_win": 0.0,
                    "half_win": 0.0,
                    "push": 1.0,
                    "half_loss": 0.0,
                    "loss": 0.0,
                },
                "all-push test distribution",
            )
        )

    def test_candidate_evaluation_rejects_home_distribution_bound_to_away_quote(
        self,
    ) -> None:
        matrix = self.joint_prediction["full_time_score_marginal"]["probabilities"]
        home_distribution = memory_store.matrix_settlement_distribution(
            matrix,
            "asian",
            {"market": "asian", "side": "home", "line": -0.75},
        )
        away_distribution = memory_store.matrix_settlement_distribution(
            matrix,
            "asian",
            {"market": "asian", "side": "away", "line": 0.75},
        )
        self.assertNotEqual(home_distribution, away_distribution)
        with tempfile.TemporaryDirectory() as base:
            artifact = self.write_candidate_evaluation_file(base)
            payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
            raw = payload["candidates"][0]
            identity = {
                "family": "asian",
                "period": "full_time",
                "line": -0.75,
                "price_outcomes": ["home", "away"],
            }
            raw.update(
                {
                    "side": "away",
                    "line": 0.75,
                    "market_identity": identity,
                    "market_identity_hash": source_evidence.market_identity_hash(
                        identity
                    ),
                    "settlement_reference_outcome": "away",
                    "probability": home_distribution["full_win"]
                    + home_distribution["half_win"],
                    "settlement_probabilities": home_distribution,
                    "complete_market_odds": {"home": 2.0, "away": 2.0},
                    "odds": 2.0,
                }
            )
            Path(artifact).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                ValueError, "probability does not match the canonical model"
            ):
                memory_store.cmd_record(
                    self.joint_record_args(
                        base,
                        candidate_evaluation_file=artifact,
                        require_candidate_evaluations=True,
                    )
                )

    def test_candidate_evaluation_v3_replays_derived_fields_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as base:
            artifact = self.write_candidate_evaluation_file(base)
            memory_store.cmd_record(
                self.joint_record_args(
                    base,
                    candidate_evaluation_file=artifact,
                    require_candidate_evaluations=True,
                )
            )
            reviewed = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
                    match_id=JOINT_FIXTURE_ID,
                    home_score=1,
                    away_score=0,
                    half_home_score=0,
                    half_away_score=0,
                    home_corners=None,
                    away_corners=None,
                    key_learning="candidate replay tamper test",
                )
            )["record"]
            original = copy.deepcopy(reviewed)
            audit = next(
                item
                for item in reviewed["settlement_basis"]["candidate_audits"]
                if item.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
            )
            self.assertTrue(
                memory_store.validated_candidate_evaluation_audit(audit, reviewed)
            )
            audit["candidates"][0]["ev"] += 1.0
            audit["audit_hash"] = (
                memory_store.calculate_candidate_evaluation_audit_hash(audit)
            )
            self.assertFalse(
                memory_store.validated_candidate_evaluation_audit(audit, reviewed)
            )
            self.assertEqual(
                memory_store.calculate_stats([reviewed])["shadow_selection_by_market"][
                    "markets"
                ]["asian"]["graded_shadow_selections"],
                0,
            )

            diagnostic_tamper = copy.deepcopy(original)
            diagnostic = next(
                item
                for item in diagnostic_tamper["settlement_basis"][
                    "observation_diagnostics"
                ]
                if item.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
            )
            diagnostic["candidate_results"][0]["settlement_result"] = "loss"
            self.assertEqual(
                memory_store.calculate_stats([diagnostic_tamper])[
                    "shadow_selection_by_market"
                ]["markets"]["asian"]["graded_shadow_selections"],
                0,
            )

    def test_candidate_evaluation_v3_deduplicates_same_match_market_across_artifact_hashes(
        self,
    ):
        records = []
        observation_ids = []
        raw_artifact_hashes = []
        for indent in (None, 2):
            with tempfile.TemporaryDirectory() as base:
                artifact = self.write_candidate_evaluation_file(base)
                payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
                Path(artifact).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=indent),
                    encoding="utf-8",
                )
                memory_store.cmd_record(
                    self.joint_record_args(
                        base,
                        candidate_evaluation_file=artifact,
                        require_candidate_evaluations=True,
                    )
                )
                record = review_command(
                    SimpleNamespace(
                        base_dir=base,
                        verified_finished=True,
                        verification_source="https://example.test/final",
                        verification_collected_at="2026-07-21T21:00:00+09:00",
                        match_id=JOINT_FIXTURE_ID,
                        home_score=1,
                        away_score=0,
                        half_home_score=0,
                        half_away_score=0,
                        home_corners=None,
                        away_corners=None,
                        key_learning="candidate dedupe test",
                    )
                )["record"]
                records.append(copy.deepcopy(record))
                audit = next(
                    item
                    for item in record["settlement_basis"]["candidate_audits"]
                    if item.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
                )
                observation_ids.append(audit["observation_id"])
                raw_artifact_hashes.append(audit["artifact"]["raw_artifact_sha256"])
        self.assertEqual(observation_ids[0], observation_ids[1])
        self.assertNotEqual(raw_artifact_hashes[0], raw_artifact_hashes[1])
        stats = memory_store.calculate_stats(records)
        self.assertEqual(
            stats["shadow_selection_by_market"]["markets"]["asian"][
                "graded_shadow_selections"
            ],
            1,
        )
        self.assertEqual(
            stats["release_blocker_funnel"]["markets"]["asian"][
                "counterfactual_candidates"
            ],
            1,
        )

    def test_complete_analysis_guard_rejects_missing_joint_artifact(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(
                ValueError, "complete analysis requires a valid --joint-scenario-file"
            ):
                memory_store.cmd_record(
                    record_args(base, require_complete_analysis=True)
                )

            history_path = memory_store.data_path(base)
            self.assertFalse(history_path.exists())

    def test_complete_analysis_guard_defaults_on_for_direct_callers(self):
        with tempfile.TemporaryDirectory() as base:
            args = record_args(base)
            delattr(args, "require_complete_analysis")
            with self.assertRaisesRegex(
                ValueError, "complete analysis requires a valid --joint-scenario-file"
            ):
                memory_store.cmd_record(args)

    def test_complete_analysis_guard_accepts_valid_joint_artifact(self):
        with tempfile.TemporaryDirectory() as base:
            created = memory_store.cmd_record(
                self.joint_record_args(base, require_complete_analysis=True)
            )["record"]

            self.assertIsNotNone(memory_store.validated_joint_scenario_audit(created))

    def test_real_complete_archive_renders_no_primary_and_joint_pairs_end_to_end(self):
        with tempfile.TemporaryDirectory() as base:
            created = memory_store.cmd_record(
                self.joint_record_args(base, require_complete_analysis=True)
            )["record"]
            payload = {
                "rows": [
                    {
                        "id": JOINT_FIXTURE_ID,
                        "archive_match_id": JOINT_FIXTURE_ID,
                        "archive_stage": "initial",
                        "archive_version_hash": prediction_card_renderer.archive_version_hash(
                            created
                        ),
                        "time": "19:30",
                        "league": "test_league",
                        "home_team": "Alpha",
                        "away_team": "Bravo",
                        "status": "no_bet",
                    }
                ]
            }

            card = prediction_card_renderer.validate_payload(
                payload, {JOINT_FIXTURE_ID: created}
            )

            self.assertEqual(card.rows[0].primary, "无正式主推")
            self.assertNotEqual(card.rows[0].total_goals, "数据不足")
            total_goal_lines = card.rows[0].total_goals.splitlines()
            self.assertEqual(len(total_goal_lines), 4)
            self.assertTrue(total_goal_lines[1].startswith("Top2"))
            self.assertNotIn("...", card.rows[0].total_goals)
            self.assertNotIn("…", card.rows[0].total_goals)
            self.assertEqual(len(card.rows[0].htft.splitlines()), 2)
            self.assertEqual(
                len(card.rows[0].htft.splitlines()),
                len(card.rows[0].scores.splitlines()),
            )

    def test_joint_scenario_artifact_is_archived_and_publicly_validated(self):
        with tempfile.TemporaryDirectory() as base:
            created = memory_store.cmd_record(self.joint_record_args(base))["record"]

            audit = created["joint_scenario_audit"]
            self.assertEqual(
                audit["schema_version"],
                memory_store.JOINT_SCENARIO_AUDIT_SCHEMA_VERSION,
            )
            self.assertEqual(audit["status"], "validated_diagnostic")
            self.assertFalse(audit["formal_eligible"])
            self.assertEqual(audit["fixture_binding"]["fixture_id"], JOINT_FIXTURE_ID)
            self.assertEqual(audit["snapshot"], self.joint_prediction)
            self.assertEqual(
                audit["joint_top_two"], self.joint_prediction["joint_top_two"]
            )
            self.assertEqual(audit["derived"], self.joint_prediction["derived"])
            binding = audit["active_version_binding"]
            self.assertEqual(
                set(binding),
                {
                    "analysis_stage",
                    "version_archived_at",
                    "fixture_id",
                    "canonical_score_content_hash",
                    "htft_prediction_content_hash",
                    "htft_prediction_hash",
                    "registered_model_hash",
                    "dataset_manifest_hash",
                },
            )
            self.assertEqual(binding["analysis_stage"], "initial")
            self.assertEqual(binding["version_archived_at"], created["updated_at"])
            self.assertEqual(binding["fixture_id"], JOINT_FIXTURE_ID)
            self.assertEqual(
                memory_store.validated_joint_scenario_audit(created),
                self.joint_prediction,
            )
            self.assertEqual(
                memory_store.revision_snapshot(created)["joint_scenario_audit"],
                audit,
            )
            self.assertEqual(
                memory_store.settlement_basis_for_record(created)[
                    "joint_scenario_audit"
                ],
                audit,
            )

    def test_joint_scenario_has_no_legacy_or_top_level_fallback(self):
        legacy = {
            "match_id": JOINT_FIXTURE_ID,
            "exact_score_picks": [{"score": "1-0", "probability": 0.2}],
            "htft_picks": [{"selection": "HH", "probability": 0.3}],
            "predicted_score": "1-0",
        }
        self.assertIsNone(memory_store.validated_joint_scenario_audit(legacy))

        with tempfile.TemporaryDirectory() as base:
            created = memory_store.cmd_record(self.joint_record_args(base))["record"]
            frozen = copy.deepcopy(created)
            frozen["settlement_basis"] = memory_store.settlement_basis_for_record(
                created
            )
            frozen["status"] = "reviewed"
            frozen["joint_scenario_audit"]["snapshot"] = {}
            frozen["score_model_provenance"] = None
            frozen["candidate_audits"] = []
            self.assertEqual(
                memory_store.validated_joint_scenario_audit(frozen),
                self.joint_prediction,
            )
            frozen["settlement_basis"].pop("joint_scenario_audit")
            self.assertIsNone(memory_store.validated_joint_scenario_audit(frozen))

            reviewed_without_basis = copy.deepcopy(created)
            reviewed_without_basis["status"] = "reviewed"
            self.assertIsNone(
                memory_store.validated_joint_scenario_audit(reviewed_without_basis)
            )

    def test_joint_scenario_generated_at_archive_and_kickoff_boundaries(self):
        accepted = copy.deepcopy(self.joint_prediction)
        accepted["generated_at"] = "2026-07-21T10:00:00.000000Z"
        accepted["prediction_hash"] = joint_scenario_model.calculate_prediction_hash(
            accepted
        )
        with tempfile.TemporaryDirectory() as base:
            created = memory_store.cmd_record(
                self.joint_record_args(base, joint_prediction=accepted)
            )["record"]
            self.assertEqual(
                memory_store.validated_joint_scenario_audit(created)["generated_at"],
                "2026-07-21T10:00:00.000000Z",
            )

        after_archive = copy.deepcopy(self.joint_prediction)
        after_archive["generated_at"] = "2026-07-21T10:01:00.000000Z"
        after_archive["prediction_hash"] = (
            joint_scenario_model.calculate_prediction_hash(after_archive)
        )
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "after archive time"):
                memory_store.cmd_record(
                    self.joint_record_args(base, joint_prediction=after_archive)
                )

        at_kickoff = copy.deepcopy(self.joint_prediction)
        at_kickoff["generated_at"] = JOINT_KICKOFF
        at_kickoff["prediction_hash"] = joint_scenario_model.calculate_prediction_hash(
            at_kickoff
        )
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "invalid joint scenario artifact"):
                memory_store.cmd_record(
                    self.joint_record_args(base, joint_prediction=at_kickoff)
                )

    def test_joint_scenario_fixture_binding_rejects_record_drift(self):
        with tempfile.TemporaryDirectory() as base:
            args = self.joint_record_args(base)
            created = memory_store.cmd_record(args)["record"]
            cases = (
                ("match_id", "wrong-fixture", "fixture_id"),
                ("home_team", "Other", "home_team"),
                ("away_team", "Other", "away_team"),
                ("kickoff", "2026-07-21T19:31:00+09:00", "kickoff"),
            )
            for field, value, message in cases:
                with self.subTest(field=field):
                    drifted = copy.deepcopy(created)
                    drifted[field] = value
                    with self.assertRaisesRegex(ValueError, message):
                        memory_store.load_joint_scenario_audit(args, drifted)

        missing_id = joint_scenario_model.predict_joint_scenarios(
            self.joint_model,
            self.joint_score_prediction,
            self.joint_htft_prediction,
            generated_at=JOINT_GENERATED_AT,
        )
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "requires fixture_id or match_id"):
                memory_store.cmd_record(
                    self.joint_record_args(base, joint_prediction=missing_id)
                )

    def test_joint_scenario_rejects_bound_input_hash_and_lineage_tampering(self):
        cases = []
        score_hash = copy.deepcopy(self.joint_prediction)
        score_hash["inputs"]["canonical_score_prediction"]["content_hash"] = (
            "sha256:" + "f" * 64
        )
        cases.append((score_hash, "canonical score input hash"))

        htft_hash = copy.deepcopy(self.joint_prediction)
        htft_hash["inputs"]["htft_prediction"]["content_hash"] = "sha256:" + "e" * 64
        cases.append((htft_hash, "HT/FT input hash"))

        model_lineage = copy.deepcopy(self.joint_prediction)
        model_lineage["inputs"]["registered_htft_model"]["model_hash"] = (
            "sha256:" + "d" * 64
        )
        cases.append((model_lineage, "lineage do not match"))

        for index, (artifact, message) in enumerate(cases):
            artifact["prediction_hash"] = (
                joint_scenario_model.calculate_prediction_hash(artifact)
            )
            with self.subTest(index=index), tempfile.TemporaryDirectory() as base:
                with self.assertRaisesRegex(ValueError, message):
                    memory_store.cmd_record(
                        self.joint_record_args(base, joint_prediction=artifact)
                    )

    def test_validated_joint_scenario_audit_fails_closed_on_wrapper_tampering(self):
        with tempfile.TemporaryDirectory() as base:
            created = memory_store.cmd_record(self.joint_record_args(base))["record"]
            tampered = copy.deepcopy(created)
            tampered["joint_scenario_audit"]["derived"] = {"forged": True}
            tampered["joint_scenario_audit"]["audit_hash"] = (
                memory_store.calculate_joint_scenario_audit_hash(
                    tampered["joint_scenario_audit"]
                )
            )
            self.assertIsNone(memory_store.validated_joint_scenario_audit(tampered))

            tampered = copy.deepcopy(created)
            tampered["joint_scenario_audit"]["snapshot"]["joint_top_two"] = []
            tampered["joint_scenario_audit"]["snapshot_hash"] = (
                joint_scenario_model.content_hash(
                    tampered["joint_scenario_audit"]["snapshot"]
                )
            )
            tampered["joint_scenario_audit"]["audit_hash"] = (
                memory_store.calculate_joint_scenario_audit_hash(
                    tampered["joint_scenario_audit"]
                )
            )
            self.assertIsNone(memory_store.validated_joint_scenario_audit(tampered))

    def test_joint_scenario_revision_preserves_the_previous_immutable_wrapper(self):
        with tempfile.TemporaryDirectory() as base:
            initial = memory_store.cmd_record(self.joint_record_args(base))["record"]
            initial_audit = copy.deepcopy(initial["joint_scenario_audit"])

            forged_lineup_context = copy.deepcopy(initial)
            forged_lineup_context["analysis_stage"] = "lineup-check"
            self.assertIsNone(
                memory_store.validated_joint_scenario_audit(forged_lineup_context)
            )

            updated = memory_store.cmd_record(
                self.joint_record_args(base, analysis_stage="lineup-check")
            )["record"]

            self.assertEqual(len(updated["revisions"]), 1)
            self.assertEqual(
                updated["revisions"][0]["joint_scenario_audit"], initial_audit
            )
            self.assertEqual(
                updated["joint_scenario_audit"]["active_version_binding"][
                    "analysis_stage"
                ],
                "lineup-check",
            )
            self.assertNotEqual(updated["joint_scenario_audit"], initial_audit)
            self.assertEqual(
                memory_store.validated_joint_scenario_audit(updated),
                self.joint_prediction,
            )

            revision = copy.deepcopy(updated["revisions"][0])
            self.assertEqual(
                memory_store.validated_joint_scenario_audit(revision),
                self.joint_prediction,
            )
            revision["joint_scenario_audit"]["active_version_binding"][
                "canonical_score_content_hash"
            ] = "sha256:" + "f" * 64
            revision["joint_scenario_audit"]["audit_hash"] = (
                memory_store.calculate_joint_scenario_audit_hash(
                    revision["joint_scenario_audit"]
                )
            )
            self.assertIsNone(memory_store.validated_joint_scenario_audit(revision))

            reused_initial = copy.deepcopy(updated)
            reused_initial["joint_scenario_audit"] = initial_audit
            self.assertIsNone(
                memory_store.validated_joint_scenario_audit(reused_initial)
            )

    def test_joint_scenario_identical_rearchive_ignores_new_archive_timestamp(self):
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(self.joint_record_args(base))
            previous_now = memory_store.utc_now
            memory_store.utc_now = lambda: datetime(
                2026, 7, 21, 10, 0, 1, tzinfo=timezone.utc
            )
            try:
                duplicate = memory_store.cmd_record(self.joint_record_args(base))
            finally:
                memory_store.utc_now = previous_now

            self.assertTrue(duplicate["duplicate_ignored"])
            self.assertEqual(duplicate["record"]["revisions"], [])

    def enable_corner_formal_for_settlement_unit_test(self) -> None:
        """Exercise settlement mechanics without weakening the production policy."""
        saved = dict(memory_store.STRICT_OOS_MARKET_STATUS)

        def restore() -> None:
            memory_store.STRICT_OOS_MARKET_STATUS.clear()
            memory_store.STRICT_OOS_MARKET_STATUS.update(saved)

        self.addCleanup(restore)
        memory_store.STRICT_OOS_MARKET_STATUS.pop("corner_total", None)
        memory_store.STRICT_OOS_MARKET_STATUS.pop("corner_handicap", None)

    def test_corner_formal_is_fail_closed_without_forward_registry_evidence(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "corner_total is observation_only"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-model-gate",
                        asian_side=None,
                        total_side=None,
                        primary_market="corner_total",
                        corner_total_side="over",
                        corner_total_line=9.5,
                        corner_total_odds=1.0,
                        corner_total_odds_format="hong_kong",
                        corner_total_probability=0.56,
                        corner_total_ev=0.12,
                        corner_total_edge_pp=4.5,
                        corner_total_firm_count=3,
                        corner_total_market_signal="aligned",
                        corner_total_market_complete=True,
                    )
                )

    def test_corner_observation_archives_every_candidate_without_a_formal_pick(self):
        with tempfile.TemporaryDirectory() as base:
            created = memory_store.cmd_record(
                self.corner_record_args(base, "corner-observation")
            )["record"]

            self.assertIsNone(created["primary_market"])
            self.assertIsNone(created["primary_pick"])
            self.assertIsNone(created["corner_total_pick"])
            self.assertIsNone(created["corner_handicap_pick"])
            self.assertEqual(len(created["candidate_audits"]), 1)
            audit = created["candidate_audits"][0]
            self.assertEqual(audit["kind"], "corner_market_observation")
            self.assertEqual(audit["status"], "observation_only")
            self.assertFalse(audit["counts_toward_primary_record"])
            self.assertEqual(audit["monetary_scope"], "none")
            self.assertEqual(
                audit["candidate_count"], len(self.corner_ranking["candidates"])
            )
            self.assertEqual(
                audit["best_observation"]["candidate_id"],
                audit["candidates"][0]["candidate_id"],
            )
            self.assertEqual(
                audit["audit_hash"],
                memory_store.calculate_corner_observation_audit_hash(audit),
            )
            for candidate in audit["candidates"]:
                self.assertEqual(
                    set(candidate["settlement_probabilities"]),
                    set(memory_store.corner_ranker.SETTLEMENT_STATES),
                )
                self.assertIn(candidate["market"], {"corner_total", "corner_handicap"})
                self.assertIn("ev", candidate)
                self.assertIn("edge_pp", candidate)
                self.assertEqual(
                    [gate["gate"] for gate in candidate["gates"]],
                    list(memory_store.CORNER_OBSERVATION_GATE_ORDER),
                )
                self.assertEqual(candidate["lineage"], audit["lineage"])
                self.assertFalse(candidate["formal_eligible"])

    def test_corner_observation_requires_the_complete_artifact_trio_and_files(self):
        with tempfile.TemporaryDirectory() as base:
            incomplete = record_args(
                base,
                match_id="corner-observation-incomplete",
                asian_side=None,
                total_side=None,
                primary_market="none",
                model_version=None,
                score_model_file="",
                corner_observation_model_dir=str(self.corner_model_dir),
            )
            with self.assertRaisesRegex(ValueError, "requires all three"):
                memory_store.cmd_record(incomplete)

            missing = record_args(
                base,
                match_id="corner-observation-missing",
                asian_side=None,
                total_side=None,
                primary_market="none",
                model_version=None,
                score_model_file="",
                corner_observation_model_dir=str(self.corner_model_dir),
                corner_observation_prediction_file=str(
                    Path(base) / "missing-prediction.json"
                ),
                corner_observation_ranker_file=str(Path(base) / "missing-ranking.json"),
            )
            with self.assertRaisesRegex(ValueError, "does not exist"):
                memory_store.cmd_record(missing)

    def test_corner_observation_rejects_a_valid_artifact_for_another_fixture(self):
        with tempfile.TemporaryDirectory() as base:
            manager = memory_store.corner_ranker.corner_model_manager
            other_prediction = manager.predict_registered_model(
                self.corner_model_dir,
                "korea_k_league_1",
                "C",
                "B",
                kickoff="2026-07-21T10:30:00Z",
                generated_at="2026-07-21T09:30:00Z",
                total_markets=(("over", 8.5), ("under", 8.5)),
                corner_handicaps=(("home", -0.5), ("away", 0.5)),
            )
            other_ranking = memory_store.corner_ranker.rank_corner_markets(
                other_prediction,
                copy.deepcopy(self.corner_ranking["input_audit"]["markets"]),
                model_dir=self.corner_model_dir,
                generated_at="2026-07-21T09:55:00Z",
                data_quality=self.corner_ranking["input_audit"]["data_quality"],
                corner_profile_evidence=copy.deepcopy(
                    self.corner_ranking["input_audit"]["corner_profile_evidence"]
                ),
            )
            with self.assertRaisesRegex(ValueError, "home_team must match"):
                memory_store.cmd_record(
                    self.corner_record_args(
                        base,
                        "corner-observation-fixture",
                        prediction=other_prediction,
                        ranking=other_ranking,
                    )
                )

    def test_corner_observation_rejects_rehashed_prediction_tampering(self):
        with tempfile.TemporaryDirectory() as base:
            prediction = copy.deepcopy(self.corner_prediction)
            prediction["formal_corner_total_eligible"] = True
            prediction["prediction_hash"] = (
                memory_store.corner_ranker.corner_model.calculate_prediction_hash(
                    prediction
                )
            )
            ranking = copy.deepcopy(self.corner_ranking)
            ranking["prediction_binding"]["prediction_hash"] = prediction[
                "prediction_hash"
            ]
            ranking["ranking_hash"] = memory_store.corner_ranker.calculate_ranking_hash(
                ranking
            )
            with self.assertRaisesRegex(ValueError, "ranking is invalid"):
                memory_store.cmd_record(
                    self.corner_record_args(
                        base,
                        "corner-observation-prediction-tamper",
                        prediction=prediction,
                        ranking=ranking,
                    )
                )

    def test_corner_observation_rejects_rehashed_ranker_tampering(self):
        with tempfile.TemporaryDirectory() as base:
            ranking = copy.deepcopy(self.corner_ranking)
            ranking["candidates"][0]["ev"] += 0.01
            ranking["ranking_hash"] = memory_store.corner_ranker.calculate_ranking_hash(
                ranking
            )
            with self.assertRaisesRegex(ValueError, "does not reproduce"):
                memory_store.cmd_record(
                    self.corner_record_args(
                        base,
                        "corner-observation-ranker-tamper",
                        ranking=ranking,
                    )
                )

    def test_corner_observation_review_settles_diagnostics_but_not_primary_roi(self):
        with tempfile.TemporaryDirectory() as base:
            created = memory_store.cmd_record(
                self.corner_record_args(base, "corner-observation-review")
            )["record"]
            reviewed = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
                    match_id="corner-observation-review",
                    home_score=1,
                    away_score=0,
                    half_home_score=None,
                    half_away_score=None,
                    home_corners=6,
                    away_corners=3,
                    key_learning="角球观察只做赛后诊断，不计入主推收益",
                )
            )
            record = reviewed["record"]
            diagnostic = record["observation_diagnostics"][0]
            self.assertEqual(diagnostic["status"], "graded_observation")
            self.assertFalse(diagnostic["counts_toward_primary_record"])
            self.assertEqual(diagnostic["monetary_scope"], "none")
            self.assertEqual(len(diagnostic["candidate_results"]), 4)
            result_by_id = {
                item["candidate_id"]: item["settlement_result"]
                for item in diagnostic["candidate_results"]
            }
            for candidate in created["candidate_audits"][0]["candidates"]:
                if candidate["market"] == "corner_total":
                    expected = memory_store.settle_corner_total(candidate, 6, 3)
                else:
                    expected = memory_store.settle_corner_handicap(candidate, 6, 3)
                expected = "full_win" if expected == "win" else expected
                self.assertEqual(result_by_id[candidate["candidate_id"]], expected)
            best_id = created["candidate_audits"][0]["best_observation"]["candidate_id"]
            self.assertEqual(
                diagnostic["best_observation_result"], result_by_id[best_id]
            )
            self.assertIsNone(record["primary_result"])
            self.assertFalse(record["counts_toward_primary_record"])
            self.assertEqual(reviewed["stats"]["primary"]["matches"], 0)
            self.assertEqual(reviewed["stats"]["primary"]["stake_units"], 0)
            self.assertEqual(reviewed["stats"]["primary"]["profit_units"], 0)
            funnel = reviewed["stats"]["observation_gate_funnel"]
            self.assertEqual(funnel["reviewed_matches_with_observations"], 1)
            for market in ("corner_total", "corner_handicap"):
                market_funnel = funnel["markets"][market]
                self.assertEqual(market_funnel["candidate_count"], 2)
                self.assertEqual(market_funnel["diagnostics"]["graded_observations"], 1)
                self.assertEqual(market_funnel["diagnostics"]["graded_candidates"], 2)
                self.assertFalse(
                    market_funnel["diagnostics"]["counts_toward_primary_record"]
                )

    def test_corner_observation_without_corner_score_remains_ungraded(self):
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                self.corner_record_args(base, "corner-observation-no-score")
            )
            reviewed = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
                    match_id="corner-observation-no-score",
                    home_score=1,
                    away_score=0,
                    half_home_score=None,
                    half_away_score=None,
                    key_learning="缺少角球数时仍完成比分复盘，角球观察不评级",
                )
            )
            record = reviewed["record"]
            self.assertIsNone(record["corner_score"])
            self.assertEqual(
                record["observation_diagnostics"][0]["status"],
                "ungraded_missing_corner_score",
            )
            self.assertIsNone(
                record["observation_diagnostics"][0]["best_observation_result"]
            )
            self.assertEqual(reviewed["stats"]["primary"]["matches"], 0)
            for market in ("corner_total", "corner_handicap"):
                diagnostics = reviewed["stats"]["observation_gate_funnel"]["markets"][
                    market
                ]["diagnostics"]
                self.assertEqual(diagnostics["ungraded_observations"], 1)
                self.assertEqual(diagnostics["ungraded_candidates"], 2)
                self.assertEqual(diagnostics["graded_candidates"], 0)

    def setUp(self):
        self._real_utc_now = memory_store.utc_now
        self._real_competition_fetch = memory_store.fetch_titan_competition_snapshot
        memory_store.utc_now = lambda: datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
        memory_store.fetch_titan_competition_snapshot = fake_competition_snapshot

    def tearDown(self):
        memory_store.utc_now = self._real_utc_now
        memory_store.fetch_titan_competition_snapshot = self._real_competition_fetch

    def test_market_evidence_freshness_is_stage_aware_and_recursive(self):
        archive_time = "2026-07-21T10:00:00Z"
        initial = {
            "analysis_stage": "initial",
            "updated_at": archive_time,
            "candidate_audits": [
                {"candidates": [{"market_collected_at": "2026-07-21T09:00:00Z"}]}
            ],
        }
        memory_store.validate_candidate_audit_freshness(initial)
        initial["candidate_audits"][0]["candidates"][0]["market_collected_at"] = (
            "2026-07-21T08:59:59Z"
        )
        with self.assertRaisesRegex(ValueError, "stale for initial"):
            memory_store.validate_candidate_audit_freshness(initial)

        lineup = {
            "analysis_stage": "lineup-check",
            "updated_at": archive_time,
            "candidate_audits": [
                {"best_observation": {"market_collected_at": "2026-07-21T09:30:00Z"}}
            ],
        }
        memory_store.validate_candidate_audit_freshness(lineup)
        lineup["candidate_audits"][0]["best_observation"]["market_collected_at"] = (
            "2026-07-21T09:29:59Z"
        )
        with self.assertRaisesRegex(ValueError, "stale for lineup-check"):
            memory_store.validate_candidate_audit_freshness(lineup)

    def test_formal_market_evidence_older_than_initial_ttl_is_rejected(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(ValueError, "stale for initial"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="stale-initial-market",
                        total_market_collected_at="2026-07-21T17:59:59+09:00",
                    )
                )

    def test_joint_external_and_attached_evidence_use_stage_ttl(self):
        base_snapshot = copy.deepcopy(self.joint_prediction)
        model_only_context = {
            "analysis_stage": "lineup-check",
            "updated_at": "2026-07-21T10:00:00Z",
        }
        # A true model-only artifact has no external evidence and remains valid.
        memory_store._validate_joint_market_evidence_freshness(
            base_snapshot, model_only_context
        )

        anchored = copy.deepcopy(base_snapshot)
        anchored["external_anchor_audit"].update(
            {"enabled": True, "captured_at": "2026-07-21T09:29:59Z"}
        )
        with self.assertRaisesRegex(ValueError, "stale for lineup-check"):
            memory_store._validate_joint_market_evidence_freshness(
                anchored, model_only_context
            )

        attached = copy.deepcopy(base_snapshot)
        attached["market_evidence"].update(
            {"provided": True, "captured_at": "2026-07-21T09:29:59Z"}
        )
        with self.assertRaisesRegex(ValueError, "stale for lineup-check"):
            memory_store._validate_joint_market_evidence_freshness(
                attached, model_only_context
            )

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
                [
                    (pick["rank"], pick["score"])
                    for pick in created["exact_score_picks"]
                ],
                [(1, "1-0"), (2, "2-0")],
            )
            self.assertEqual(created["league_key"], "测试联赛")
            self.assertTrue(
                all(
                    pick["status"] == "scenario_only"
                    for pick in created["exact_score_picks"]
                )
            )

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
                memory_store.cmd_record(
                    record_args(base, exact_score_pick=["1-0:0.20"])
                )
            with self.assertRaisesRegex(ValueError, "highest-probability"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        predicted_score="2-0",
                        exact_score_pick=["1-0:0.20", "2-0:0.15"],
                    )
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
            with self.assertRaisesRegex(ValueError, "conditioned display score rank 1"):
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
            with self.assertRaisesRegex(
                ValueError, "exceeds the archived second-ranked"
            ):
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
            self.assertEqual(
                maintained["record"]["primary_change"]["status"], "maintained"
            )

            with self.assertRaisesRegex(ValueError, "immutable"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        analysis_stage="lineup-check",
                        total_odds=0.84,
                        total_ev=0.012,
                    )
                )

            with self.assertRaisesRegex(
                ValueError, "valid only when there are no formal picks"
            ):
                memory_store.cmd_record(
                    record_args(base, match_id="2", primary_market="none")
                )
            with self.assertRaisesRegex(ValueError, "is not present"):
                memory_store.cmd_record(
                    record_args(base, match_id="3", primary_market="half_time")
                )

    def test_review_persists_primary_result(self):
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(base, asian_side=None, primary_market="total")
            )
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
            self.assertEqual(
                result["record"]["settlement_basis"]["grading_scope"], "primary_only"
            )
            self.assertEqual(
                result["record"]["settlement_basis"]["analysis_stage"], "initial"
            )
            self.assertEqual(
                result["record"]["settlement_basis"]["policy"],
                "latest_active_prematch_version",
            )
            self.assertEqual(result["league_key"], "测试联赛")
            self.assertEqual(result["league_stats"]["reviewed_matches"], 1)

    def test_review_rejects_half_time_goals_exceeding_full_time_by_team(self):
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(base, primary_market="none", total_side=None)
            )
            for half_home, half_away in ((2, 0), (1, 1)):
                with self.subTest(
                    half_home=half_home,
                    half_away=half_away,
                ):
                    with self.assertRaisesRegex(
                        ValueError, "cannot exceed the corresponding full-time"
                    ):
                        review_command(
                            SimpleNamespace(
                                base_dir=base,
                                verified_finished=True,
                                verification_source="https://example.test/final",
                                verification_collected_at=("2026-07-21T21:00:00+09:00"),
                                match_id="1",
                                home_score=1,
                                away_score=0,
                                half_home_score=half_home,
                                half_away_score=half_away,
                                key_learning="reject impossible period score",
                            )
                        )

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
            self.assertEqual(
                record["settlement_basis"]["analysis_stage"], "lineup-check"
            )
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
        record.update(
            {
                "analysis_stage": "lineup-check",
                "lineup_rechecked_at": "2026-07-21T10:00:00+00:00",
                "updated_at": "2026-07-21T10:00:00+00:00",
                "primary_market": "total",
                "primary_pick": dict(total, market="total", role="primary"),
                "primary_result": "win",
                "final_score": "0-0",
            }
        )
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
            self.assertEqual(
                saved["settlement_basis"]["analysis_stage"], "lineup-check"
            )
            self.assertEqual(saved["primary_result"], before["primary_result"])
            self.assertEqual(saved["total_result"], before["total_result"])
            self.assertEqual(saved["revisions"], before["revisions"])

    def test_legacy_settlement_identity_migration_freezes_review_and_stats(self):
        record = strict_metric_record(
            "legacy-2913681",
            probabilities={"home_win": 0.35, "draw": 0.40, "away_win": 0.25},
            final_score="1-1",
            selected=False,
            league="finland_veikkausliiga",
        )
        record.update(
            {
                "league_key": "finland_veikkausliiga",
                "source_url": "https://zq.titan007.com/analysis/2913681cn.htm",
                "competition_evidence": {"untrusted": "attached-after-review"},
                "analysis_stage": "lineup-check",
                "home_team": "KuPS",
                "away_team": "HJK",
                "kickoff": "2026-08-04T01:00:00+09:00",
                "half_time_score": "0-0",
                "reviewed_at": "2026-08-03T17:05:00+00:00",
                "counts_toward_primary_record": False,
                # This mirrors the legacy on-disk basis: settlement fields were
                # frozen, but fixture/competition identity fields were absent.
                "settlement_basis": {
                    "policy": "latest_active_prematch_version",
                    "grading_scope": "primary_only",
                    "analysis_stage": "lineup-check",
                    "version_archived_at": "2026-08-03T15:33:45+00:00",
                    "lineup_rechecked_at": "2026-08-03T15:33:45+00:00",
                    "primary_market": None,
                    "primary_pick": None,
                    "primary_result": None,
                    "formal_picks": {},
                    "candidate_audits": [],
                    "counts_toward_primary_record": False,
                    "revision_count": 1,
                },
            }
        )

        with tempfile.TemporaryDirectory() as base:
            path = memory_store.data_path(base)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")

            migrated = memory_store.cmd_migrate_settlement_basis(
                SimpleNamespace(base_dir=base, write=True)
            )
            self.assertEqual(migrated["changed_match_ids"], ["legacy-2913681"])
            saved = memory_store.load_history(path)[0]
            basis = saved["settlement_basis"]
            self.assertEqual(basis["league"], "finland_veikkausliiga")
            self.assertEqual(basis["league_key"], "finland_veikkausliiga")
            self.assertEqual(
                basis["source_url"],
                "https://zq.titan007.com/analysis/2913681cn.htm",
            )
            self.assertIsNone(basis["competition_evidence"])
            self.assertEqual(
                basis["competition_identity_migration"]["competition_evidence_status"],
                "unavailable_in_legacy_settlement_basis",
            )

            card_before = review_card_renderer.build_card(saved)
            stats_before = memory_store.calculate_stats([saved])
            self.assertEqual(card_before.league, "芬超")
            self.assertIn("finland_veikkausliiga", stats_before["leagues"])
            self.assertEqual(
                card_before.settlement_hash,
                memory_store.canonical_prediction_hash(basis),
            )

            tampered = copy.deepcopy(saved)
            tampered["league"] = "england_premier_league"
            tampered["league_key"] = "england_premier_league"
            tampered["source_url"] = "https://example.test/tampered"
            tampered["competition_evidence"] = None
            card_after = review_card_renderer.build_card(tampered)
            stats_after = memory_store.calculate_stats([tampered])

            self.assertEqual(card_after.league, card_before.league)
            self.assertEqual(card_after.settlement_hash, card_before.settlement_hash)
            self.assertEqual(stats_after["leagues"], stats_before["leagues"])
            self.assertNotIn("england_premier_league", stats_after["leagues"])

            repeated = memory_store.cmd_migrate_settlement_basis(
                SimpleNamespace(base_dir=base, write=False)
            )
            self.assertEqual(repeated["changed_match_ids"], [])

    def test_brazil_cup_stage_labels_normalize_to_one_competition_key(self):
        labels = (
            "巴西杯",
            "2026巴西杯16强次回合",
            "巴西杯1/8决赛首回合",
            "巴西杯四分之一决赛次回合",
            "2026巴西杯决赛",
        )
        for label in labels:
            with self.subTest(label=label):
                self.assertEqual(
                    memory_store.normalize_league_name(label), "brazil_cup"
                )

    def test_efl_cup_stage_labels_normalize_to_one_competition_key(self):
        labels = (
            "英联杯",
            "2025英联杯半决赛次回合",
            "英格兰联赛杯决赛",
            "Carabao Cup",
        )
        for label in labels:
            with self.subTest(label=label):
                self.assertEqual(
                    memory_store.normalize_league_name(label),
                    "england_league_cup",
                )

    def test_primeira_liga_aliases_normalize_to_chinese_label(self):
        for label in ("葡超", "2025葡超第10轮", "葡萄牙超级联赛", "Liga Portugal"):
            with self.subTest(label=label):
                self.assertEqual(memory_store.normalize_league_name(label), "葡超")

    def test_eerste_divisie_aliases_normalize_to_chinese_label(self):
        for label in (
            "荷乙",
            "2025荷乙第10轮",
            "Eerste Divisie",
            "Keuken Kampioen Divisie",
            "Netherlands Eerste Divisie",
        ):
            with self.subTest(label=label):
                self.assertEqual(memory_store.normalize_league_name(label), "荷乙")

    def test_competition_evidence_build_validate_and_tamper_fail_closed(self):
        match_id = "2991125"
        values = competition_evidence_values(match_id)
        record = {
            "match_id": match_id,
            "home_team": "瑞模贝雷",
            "away_team": "桑托斯",
            "kickoff": "2026-08-05T09:30:00+09:00",
            "source_url": values["verification_source"],
        }

        evidence = memory_store.build_competition_evidence(record, **values)
        record["competition_evidence"] = evidence
        self.assertEqual(memory_store.validated_competition_evidence(record), evidence)
        self.assertEqual(evidence["competition"]["key"], "brazil_cup")
        self.assertEqual(evidence["competition"]["label"], "巴西杯")
        self.assertRegex(evidence["evidence_hash"], r"^sha256:[0-9a-f]{64}$")

        changed_without_hash = copy.deepcopy(record)
        changed_without_hash["competition_evidence"]["competition"]["label"] = "巴甲"
        self.assertIsNone(
            memory_store.validated_competition_evidence(changed_without_hash)
        )

        rebound_tamper = copy.deepcopy(record)
        rebound_tamper["competition_evidence"]["fixture"]["home_team"] = "伪造主队"
        rebound_tamper["competition_evidence"]["evidence_hash"] = (
            memory_store.calculate_competition_evidence_hash(
                rebound_tamper["competition_evidence"]
            )
        )
        self.assertIsNone(memory_store.validated_competition_evidence(rebound_tamper))

        with self.assertRaisesRegex(
            ValueError, "not registered|do not match the registry"
        ):
            memory_store.build_competition_evidence(
                record,
                **{
                    **values,
                    "competition_key": "england_premier_league",
                    "competition_label": "英超",
                    "competition_id": "999",
                    "source_locator": "//info.titan007.com/fake/competition_999.htm",
                },
            )
        with self.assertRaisesRegex(ValueError, "cannot be in the future"):
            with mock.patch.object(
                memory_store,
                "fetch_titan_competition_snapshot",
                return_value={
                    **fake_competition_snapshot(record),
                    "collected_at": "2099-01-01T00:00:00+09:00",
                },
            ):
                memory_store.build_competition_evidence(
                    record,
                    **{
                        **values,
                        "collected_at": "2099-01-01T00:00:00+09:00",
                    },
                )

    def test_competition_evidence_is_derived_from_matching_titan_header_html(self):
        values = competition_evidence_values("2991125")
        record = {
            "match_id": "2991125",
            "home_team": "瑞模贝雷",
            "away_team": "桑托斯",
            "kickoff": "2026-08-05T09:30:00+09:00",
            "source_url": values["verification_source"],
        }
        raw_html = b"""
        <a href='//zq.titan007.com/cn/team/Summary/1964.html'>\xe7\x91\x9e\xe6\xa8\xa1\xe8\xb4\x9d\xe9\x9b\xb7(\xe4\xb8\xbb)</a>
        <a class='LName' href='//info.titan007.com/cup_match/2026-2027/cupmatch_vs/cupmatch_186.htm'>\xe5\xb7\xb4\xe8\xa5\xbf\xe6\x9d\xaf</a>
        <a href='//zq.titan007.com/cn/team/Summary/337.html'>\xe6\xa1\x91\xe6\x89\x98\xe6\x96\xaf</a>
        """
        snapshot = memory_store.extract_titan_competition_snapshot(
            raw_html,
            source_url=record["source_url"],
            response_url=record["source_url"],
            record=record,
            collected_at=memory_store.utc_now(),
        )
        evidence = memory_store.build_competition_evidence(
            record, **values, _source_snapshot=snapshot
        )
        self.assertEqual(evidence["source"]["header"]["competition_label"], "巴西杯")
        self.assertRegex(evidence["source"]["page_sha256"], r"^sha256:[0-9a-f]{64}$")

        wrong_fixture = {**record, "home_team": "塞伊奈约基", "away_team": "赫尔辛基"}
        with self.assertRaisesRegex(ValueError, "teams do not match"):
            memory_store.build_competition_evidence(
                wrong_fixture, **values, _source_snapshot=snapshot
            )

    def test_real_competition_stats_are_separate_from_proxy_model_league(self):
        match_id = "2991125"
        values = competition_evidence_values(match_id)
        record = strict_metric_record(
            match_id,
            probabilities={"home_win": 0.6, "draw": 0.2, "away_win": 0.2},
            final_score="1-0",
            selected=True,
            league="brazil_serie_a",
        )
        record.update(
            {
                "league_key": "brazil_serie_a",
                "home_team": "瑞模贝雷",
                "away_team": "桑托斯",
                "kickoff": "2026-08-05T09:30:00+09:00",
                "source_url": values["verification_source"],
            }
        )
        record["competition_evidence"] = memory_store.build_competition_evidence(
            record, **values
        )

        stats = memory_store.calculate_stats([record])
        self.assertIn("brazil_cup", stats["leagues"])
        self.assertNotIn("brazil_serie_a", stats["leagues"])
        self.assertEqual(stats["leagues"]["brazil_cup"]["primary"]["matches"], 1)

    def test_reviewed_stats_use_settlement_frozen_competition_identity(self):
        match_id = "2991125"
        values = competition_evidence_values(match_id)
        record = strict_metric_record(
            match_id,
            probabilities={"home_win": 0.6, "draw": 0.2, "away_win": 0.2},
            final_score="1-0",
            selected=True,
            league="brazil_serie_a",
        )
        record.update(
            {
                "league_key": "brazil_serie_a",
                "home_team": "瑞模贝雷",
                "away_team": "桑托斯",
                "kickoff": "2026-08-05T09:30:00+09:00",
                "source_url": values["verification_source"],
            }
        )
        record["competition_evidence"] = memory_store.build_competition_evidence(
            record, **values
        )
        record["settlement_basis"] = {
            "match_id": record["match_id"],
            "home_team": record["home_team"],
            "away_team": record["away_team"],
            "kickoff": record["kickoff"],
            "source_url": record["source_url"],
            "league": record["league"],
            "league_key": record["league_key"],
            "competition_evidence": copy.deepcopy(record["competition_evidence"]),
            "primary_result": "win",
        }

        record["league"] = "england_premier_league"
        record["league_key"] = "england_premier_league"
        record["competition_evidence"] = None

        self.assertEqual(memory_store.competition_key_for_record(record), "brazil_cup")
        stats = memory_store.calculate_stats([record])
        self.assertIn("brazil_cup", stats["leagues"])
        self.assertNotIn("england_premier_league", stats["leagues"])

    def test_lineup_cannot_replace_invalid_existing_competition_evidence(self):
        match_id = "2991125"
        values = competition_evidence_values(match_id)
        cli_values = {
            "competition_key": values["competition_key"],
            "competition_label": values["competition_label"],
            "competition_id": values["competition_id"],
            "competition_verification_source": values["verification_source"],
            "competition_source_locator": values["source_locator"],
            "competition_collected_at": values["collected_at"],
        }
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(
                    base,
                    match_id=match_id,
                    source_url=values["verification_source"],
                    **cli_values,
                )
            )
            path = memory_store.data_path(base)
            history = memory_store.load_history(path)
            history[0]["competition_evidence"]["competition"]["label"] = "伪造赛事"
            memory_store.save_history(path, history)

            with self.assertRaisesRegex(
                ValueError, "existing competition evidence is invalid"
            ):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id=match_id,
                        analysis_stage="lineup-check",
                        source_url=values["verification_source"],
                        **cli_values,
                    )
                )

    def test_lineup_kickoff_change_requires_and_accepts_fresh_competition_evidence(
        self,
    ):
        match_id = "2991125"
        values = competition_evidence_values(match_id)
        cli_values = {
            "competition_key": values["competition_key"],
            "competition_label": values["competition_label"],
            "competition_id": values["competition_id"],
            "competition_verification_source": values["verification_source"],
            "competition_source_locator": values["source_locator"],
            "competition_collected_at": values["collected_at"],
        }
        changed_time = {
            "source_kickoff": "2026-07-21T18:25:00+08:00",
            "user_local_kickoff": "2026-07-21T19:25:00+09:00",
            "kickoff": "2026-07-21T19:25:00+09:00",
        }
        with tempfile.TemporaryDirectory() as base:
            initial = memory_store.cmd_record(
                record_args(
                    base,
                    match_id=match_id,
                    source_url=values["verification_source"],
                    **cli_values,
                )
            )["record"]
            self.assertIsNotNone(memory_store.validated_competition_evidence(initial))

            with self.assertRaisesRegex(ValueError, "supply fresh source-verified"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id=match_id,
                        analysis_stage="lineup-check",
                        source_url=values["verification_source"],
                        **changed_time,
                    )
                )

            lineup = memory_store.cmd_record(
                record_args(
                    base,
                    match_id=match_id,
                    analysis_stage="lineup-check",
                    source_url=values["verification_source"],
                    **changed_time,
                    **cli_values,
                )
            )["record"]
            self.assertIsNotNone(memory_store.validated_competition_evidence(lineup))
            self.assertEqual(
                lineup["competition_evidence"]["fixture"]["kickoff"],
                "2026-07-21T19:25:00+09:00",
            )

    def test_attach_competition_evidence_preserves_prediction_and_is_idempotent(self):
        match_id = "2991125"
        values = competition_evidence_values(match_id)
        with tempfile.TemporaryDirectory() as base:
            memory_store.cmd_record(
                record_args(
                    base,
                    match_id=match_id,
                    source_url=values["verification_source"],
                )
            )
            path = memory_store.data_path(base)
            history = memory_store.load_history(path)
            history[0]["revisions"] = [
                {"analysis_stage": "initial", "sentinel": "keep-revision"}
            ]
            history[0]["settlement_basis"] = {
                "policy": "test-frozen-basis",
                "sentinel": "keep-settlement",
            }
            memory_store.save_history(path, history)

            before = memory_store.load_history(path)[0]
            before_snapshot = memory_store.snapshot_payload(
                memory_store.revision_snapshot(before)
            )
            before_hash = memory_store.canonical_prediction_hash(before_snapshot)
            before_revisions = copy.deepcopy(before["revisions"])
            before_settlement = copy.deepcopy(before["settlement_basis"])

            attached = memory_store.cmd_attach_competition_evidence(
                attach_competition_args(base, match_id)
            )
            self.assertTrue(attached["written"])
            self.assertFalse(attached["duplicate_ignored"])
            self.assertTrue(attached["prediction_fields_unchanged"])
            self.assertTrue(attached["archive_version_hash_changed"])

            after = memory_store.load_history(path)[0]
            after_snapshot = memory_store.snapshot_payload(
                memory_store.revision_snapshot(after)
            )
            self.assertNotEqual(after_snapshot, before_snapshot)
            self.assertNotEqual(
                memory_store.canonical_prediction_hash(after_snapshot), before_hash
            )
            self.assertEqual(after["revisions"], before_revisions)
            self.assertEqual(after["settlement_basis"], before_settlement)
            self.assertEqual(len(after["metadata_revisions"]), 1)
            self.assertIsNotNone(memory_store.validated_competition_evidence(after))

            bytes_after_first_attach = path.read_bytes()
            duplicate = memory_store.cmd_attach_competition_evidence(
                attach_competition_args(base, match_id)
            )
            self.assertTrue(duplicate["duplicate_ignored"])
            self.assertFalse(duplicate["written"])
            self.assertEqual(path.read_bytes(), bytes_after_first_attach)

            conflicting = attach_competition_args(
                base,
                match_id,
                competition_key="brazil_serie_a",
                competition_label="巴甲",
                competition_id="4",
                source_locator="//zq.titan007.com/cn/league.aspx?sclassid=4",
            )
            with self.assertRaisesRegex(ValueError, "not registered"):
                memory_store.cmd_attach_competition_evidence(conflicting)
            self.assertEqual(path.read_bytes(), bytes_after_first_attach)

    def test_reviewed_archive_rejects_competition_attachment(self):
        with tempfile.TemporaryDirectory() as base:
            path = memory_store.data_path(base)
            path.parent.mkdir(parents=True)
            record = reviewed_record("2991125")
            record.update(
                {
                    "home_team": "瑞模贝雷",
                    "away_team": "桑托斯",
                    "kickoff": "2026-08-05T09:30:00+09:00",
                    "source_url": competition_evidence_values()["verification_source"],
                }
            )
            memory_store.save_history(path, [record])

            with self.assertRaisesRegex(ValueError, "only to a pending prematch"):
                memory_store.cmd_attach_competition_evidence(
                    attach_competition_args(base)
                )

    def test_record_rejects_every_partial_competition_evidence_shape(self):
        full = competition_evidence_values("2991125")
        cli_names = {
            "competition_key": full["competition_key"],
            "competition_label": full["competition_label"],
            "competition_id": full["competition_id"],
            "competition_verification_source": full["verification_source"],
            "competition_source_locator": full["source_locator"],
            "competition_collected_at": full["collected_at"],
        }
        with tempfile.TemporaryDirectory() as base:
            for index, missing in enumerate(cli_names):
                partial = dict(cli_names)
                partial[missing] = None
                match_id = f"partial-competition-{index}"
                partial["competition_verification_source"] = (
                    f"https://zq.titan007.com/analysis/{match_id}cn.htm"
                    if missing != "competition_verification_source"
                    else None
                )
                with self.subTest(missing=missing):
                    with self.assertRaisesRegex(
                        ValueError,
                        "competition evidence requires every --competition-\\* field",
                    ):
                        memory_store.cmd_record(
                            record_args(
                                base,
                                match_id=match_id,
                                source_url=(
                                    partial["competition_verification_source"]
                                    or "https://example.test/match"
                                ),
                                **partial,
                            )
                        )

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
        first.update(
            {
                "league": "2026芬超第16轮",
                "primary_market": "total",
                "primary_pick": dict(total_win, market="total", role="primary"),
                "primary_result": "win",
            }
        )
        second = reviewed_record("102", total=total_loss, total_result="loss")
        second.update(
            {
                "league": "芬超",
                "primary_market": "total",
                "primary_pick": dict(total_loss, market="total", role="primary"),
                "primary_result": "loss",
            }
        )
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
                SimpleNamespace(
                    base_dir=base, guardrail=None, minimum_graded=20, write=True
                )
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
        primary.update(
            {
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
            }
        )
        no_primary = reviewed_record("no-primary-learning")
        no_primary.update(
            {
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
            }
        )
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
            self.assertEqual(by_id["primary-learning"]["learning_scope"], "primary")
            self.assertTrue(by_id["primary-learning"]["counts_toward_primary_record"])
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
            stats_defaults = memory_store.build_parser().parse_args(["stats"])
            gate_defaults = memory_store.build_parser().parse_args(["gate-stats"])
            calibration_defaults = memory_store.build_parser().parse_args(["calibrate"])
            self.assertEqual(stats_defaults.gate_windows, [50, 100])
            self.assertEqual(gate_defaults.windows, [50, 100])
            self.assertEqual(calibration_defaults.gate_windows, [50, 100])
            memory_store.cmd_record(
                record_args(base, asian_side=None, primary_market="total")
            )
            early = memory_store.cmd_due_lineup_check(
                SimpleNamespace(
                    base_dir=base,
                    now="2026-07-21T18:45:00+09:00",
                    min_minutes=0,
                    max_minutes=30,
                )
            )
            due = memory_store.cmd_due_lineup_check(
                SimpleNamespace(
                    base_dir=base,
                    now="2026-07-21T19:00:00+09:00",
                    min_minutes=0,
                    max_minutes=30,
                )
            )
            self.assertEqual(early["due"], [])
            self.assertEqual([item["match_id"] for item in due["due"]], ["1"])

    def test_legacy_migration_primary_roi_all_formal_and_calibration(self):
        def asian(odds):
            return {
                "side": "home",
                "line": 0.0,
                "odds": odds,
                "ev": 0.06,
                "market_signal": "aligned",
            }

        def total(odds):
            return {
                "side": "under",
                "line": 2.5,
                "odds": odds,
                "ev": 0.06,
                "market_signal": "aligned",
            }

        half = {
            "market": "total",
            "side": "under",
            "line": 1.0,
            "odds": 1.06,
            "ev": 0.03,
            "market_signal": "unknown",
        }
        history = [
            reviewed_record("2907406", asian(0.98), "half_win", total(0.86), "win"),
            reviewed_record("2913667", asian(1.07), "loss", total(0.95), "win"),
            reviewed_record("2913668", asian(0.83), "loss", total(1.04), "loss"),
            reviewed_record(
                "2912210", asian(0.93), "win", total(0.89), "win", half, "loss"
            ),
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
            self.assertEqual(
                stats["all_formal"]["combined"]["monetary_scope"], "primary_only"
            )
            self.assertEqual(stats["all_formal"]["combined"]["stake_units"], 0)
            self.assertEqual(stats["all_formal"]["combined"]["profit_units"], 0)
            self.assertIsNone(stats["all_formal"]["combined"]["roi"])
            self.assertEqual(stats["combined"], stats["all_formal"]["combined"])

            saved = memory_store.load_history(path)
            self.assertEqual(
                {r["match_id"]: r["revisions"] for r in saved}, before_revisions
            )
            for record in saved:
                roles = [
                    pick.get("role") for _, pick in memory_store.formal_picks(record)
                ]
                self.assertEqual(roles.count("primary"), 1)

            calibration = memory_store.cmd_calibrate(
                SimpleNamespace(
                    base_dir=base, guardrail=None, minimum_graded=20, write=True
                )
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
                    "EV and no-vig edge are positive eligibility gates only" in item
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
            self.assertTrue(
                all(
                    value is False
                    for value in calibration["weight_change_eligible"].values()
                )
            )

    def test_secondary_pick_is_ignored_by_all_statistics(self):
        primary = {
            "side": "under",
            "line": 2.5,
            "odds": 0.90,
            "ev": 0.06,
            "role": "primary",
        }
        secondary = {
            "side": "home",
            "line": 0.0,
            "odds": 0.84,
            "ev": 0.05,
            "role": "secondary",
        }
        record = reviewed_record(
            "secondary-no-money", secondary, "loss", primary, "win"
        )
        record.update(
            {
                "primary_market": "total",
                "primary_pick": dict(primary, market="total"),
                "primary_result": "win",
            }
        )

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
        self.assertEqual(stats["legacy_or_quarantined"]["primary"]["profit_units"], 0.9)

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
            self.assertEqual(sub_eight["confidence_ranking_version"], "stability-v2")

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
                    "HH:0.08775",
                    "HD:0.08775",
                    "HA:0.08775",
                    "DH:0.08775",
                    "DD:0.298",
                    "DA:0.08775",
                    "AH:0.08775",
                    "AD:0.08775",
                    "AA:0.08775",
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
                                "HH:0.08625",
                                "HD:0.08625",
                                "HA:0.08625",
                                "DH:0.08625",
                                "DD:0.31",
                                "DA:0.08625",
                                "AH:0.08625",
                                "AD:0.08625",
                                "AA:0.08625",
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
            self.assertGreater(
                accepted["goal_range_pick"]["ev"], accepted["btts_pick"]["ev"]
            )
            self.assertEqual(accepted["btts_pick"]["confidence_rank"], 1)
            gates = accepted["primary_pick"]["confidence_components"][
                "eligibility_gates"
            ]
            self.assertFalse(gates["contributes_to_score"])
            self.assertEqual(accepted["confidence_ranking_version"], "stability-v2")

    def test_against_deep_favorite_and_total_evidence_gates(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaisesRegex(
                ValueError, "adverse-signal EV must be at least 0.08"
            ):
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
            with self.assertRaisesRegex(
                ValueError, "bookmaker count must be at least 5"
            ):
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
            with self.assertRaisesRegex(
                ValueError, "independent lineup or fundamental"
            ):
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
            self.assertEqual(
                cancelled["primary_change"]["decision"], "cancelled_to_none"
            )
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
            self.assertIsNone(reviewed["record"]["settlement_basis"]["primary_market"])
            self.assertEqual(reviewed["stats"]["reviewed_matches"], 1)
            self.assertEqual(reviewed["stats"]["primary"]["matches"], 0)
            self.assertEqual(
                reviewed["record"]["learning_scope"],
                "no_primary_observation",
            )
            self.assertFalse(reviewed["record"]["counts_toward_primary_record"])
            self.assertEqual(
                reviewed["record"]["learning_sample"]["scope"],
                "no_primary_observation",
            )
            self.assertEqual(reviewed["stats"]["primary_record_matches"], 0)
            self.assertEqual(reviewed["stats"]["no_primary_reviewed_matches"], 1)
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
        self.enable_corner_formal_for_settlement_unit_test()
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
                sum(
                    pick["role"] == "primary"
                    for _, pick in memory_store.formal_picks(created)
                ),
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
            self.assertEqual(hong_kong["primary_pick"]["odds_format"], "hong_kong")
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
            with self.assertRaisesRegex(
                ValueError, "corner_total EV must be greater than 0"
            ):
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
            self.assertEqual(sub_eight_corner["primary_pick"]["confidence_rank"], 1)
            with self.assertRaisesRegex(ValueError, "corner-profile evidence"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="corner-evidence",
                        corner_profile_evidence=False,
                        **corner,
                    )
                )
            with self.assertRaisesRegex(
                ValueError, "bookmaker count must be at least 3"
            ):
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
                            if key
                            not in {
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
        self.enable_corner_formal_for_settlement_unit_test()

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
                goal_review["record"]["settlement_basis"]["formal_picks"]["goal_range"][
                    "selection"
                ],
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
                    corner_total_edge_pp=3.0945945946,
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
                    corner_handicap_edge_pp=10.7222222222,
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
        self.assertEqual(
            memory_store._forward_observed_settlement_state(
                {"final_score": "2-1", "half_time_score": "1-0"},
                {
                    "market_identity": {
                        "family": "goal_range",
                        "period": "full_time",
                        "line": None,
                        "price_outcomes": ["0-1", "2-3", "4-6", "7+"],
                    },
                    "settlement_reference_outcome": None,
                },
                {
                    "settlement_states": ["0-1", "2-3", "4-6", "7+"],
                    "settlement_semantics": "categorical",
                },
            ),
            "2-3",
        )
        self.assertEqual(memory_store.settle_btts({"side": "yes"}, 1, 1), "win")
        self.assertEqual(memory_store.settle_btts({"side": "no"}, 2, 0), "win")
        self.assertEqual(memory_store.settle_btts({"side": "no"}, 1, 1), "loss")
        self.assertEqual(
            memory_store.settle_corner_total({"side": "over", "line": 10.75}, 6, 5),
            "half_win",
        )
        self.assertEqual(
            memory_store.settle_corner_handicap({"side": "home", "line": -2.0}, 7, 5),
            "push",
        )
        self.assertEqual(memory_store.settlement_profit("win", 0.90), 0.90)
        self.assertEqual(
            memory_store.settlement_profit("half_win", 0.92, "hong_kong"),
            0.46,
        )
        self.assertEqual(memory_store.settlement_profit("push", 1.95, "decimal"), 0.0)
        self.assertAlmostEqual(
            memory_store.settlement_profit("win", 1.90, "decimal"), 0.90
        )

    def test_new_market_lineup_identity_worse_line_and_revisions(self):
        self.enable_corner_formal_for_settlement_unit_test()
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
                    "corner_total_edge_pp": 10.7222222222,
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
            self.assertEqual(accepted["revisions"][0]["corner_total_pick"]["line"], 9.5)
            self.assertIn("goal_range_pick", accepted["revisions"][0])
            self.assertIn(
                "corner_handicap",
                memory_store.settlement_basis_for_record(accepted)["formal_picks"],
            )
            self.assertEqual(
                memory_store.pick_identity("goal_range", {"selection": "2-3"}),
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

    def test_paused_htft_observation_has_audited_review_and_gate_funnel(self):
        with tempfile.TemporaryDirectory() as base:
            model_file, ranker_file = write_htft_observation_files(
                base, "htft-observation"
            )
            created = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="htft-observation",
                    asian_side=None,
                    total_side=None,
                    primary_market="none",
                    model_version=None,
                    score_model_file=None,
                    htft_observation_model_file=model_file,
                    htft_observation_ranker_file=ranker_file,
                )
            )["record"]

            self.assertIsNone(created["primary_market"])
            self.assertIsNone(created["primary_pick"])
            self.assertEqual(created["htft_picks"], [])
            self.assertEqual(len(created["candidate_audits"]), 1)
            audit = created["candidate_audits"][0]
            self.assertEqual(audit["market"], "htft")
            self.assertFalse(audit["counts_toward_primary_record"])
            self.assertEqual(audit["monetary_scope"], "none")
            self.assertRegex(audit["model"]["matrix_hash"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(
                [item["selection"] for item in audit["top_two"]],
                ["HH", "DH"],
            )
            self.assertAlmostEqual(audit["pair_probability_mass"], 0.50)
            policy_gate = next(
                gate
                for gate in audit["top_two"][0]["gates"]
                if gate["gate"] == "market_policy_enabled"
            )
            self.assertFalse(policy_gate["passed"])

            reviewed = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
                    match_id="htft-observation",
                    home_score=1,
                    away_score=0,
                    half_home_score=0,
                    half_away_score=0,
                    key_learning="半全场观察只进入概率诊断，不追认主推",
                )
            )
            record = reviewed["record"]
            diagnostic = record["observation_diagnostics"][0]
            self.assertEqual(diagnostic["actual_selection"], "DH")
            self.assertFalse(diagnostic["top1_hit"])
            self.assertTrue(diagnostic["top2_hit"])
            self.assertFalse(diagnostic["counts_toward_primary_record"])
            self.assertEqual(diagnostic["monetary_scope"], "none")
            self.assertEqual(reviewed["stats"]["primary"]["matches"], 0)
            self.assertEqual(reviewed["stats"]["primary"]["profit_units"], 0)

            funnel = reviewed["stats"]["observation_gate_funnel"]
            self.assertEqual(funnel["reviewed_matches_with_observations"], 1)
            htft = funnel["markets"]["htft"]
            self.assertEqual(htft["candidate_count"], 2)
            self.assertEqual(htft["diagnostics"]["graded_observations"], 1)
            self.assertEqual(htft["diagnostics"]["top1_hits"], 0)
            self.assertEqual(htft["diagnostics"]["top2_hits"], 1)
            self.assertEqual(htft["gate_funnel"]["market_policy_enabled"]["failed"], 2)
            self.assertEqual(htft["gate_funnel"]["scenario_stability"]["passed"], 2)
            league_key = memory_store.normalize_league_name("测试联赛")
            self.assertEqual(
                reviewed["stats"]["leagues"][league_key]["observation_gate_funnel"][
                    "markets"
                ]["htft"]["candidate_count"],
                2,
            )

            calibration = memory_store.cmd_calibrate(
                SimpleNamespace(
                    base_dir=base,
                    guardrail=None,
                    minimum_graded=20,
                    write=False,
                )
            )["calibration"]
            self.assertEqual(
                calibration["observation_gate_funnel"]["markets"]["htft"][
                    "diagnostics"
                ]["top2_hits"],
                1,
            )
            self.assertEqual(
                calibration["league_profiles"][league_key]["observation_gate_funnel"][
                    "markets"
                ]["htft"]["candidate_count"],
                2,
            )
            self.assertFalse(calibration["parameter_change_authorized"])

    def test_htft_observation_rejects_ranker_that_does_not_match_model_top_two(self):
        with tempfile.TemporaryDirectory() as base:
            model_file, ranker_file = write_htft_observation_files(base, "htft-tamper")
            ranker = json.loads(Path(ranker_file).read_text(encoding="utf-8"))
            ranker["scenarios"][0]["selection"] = "DD"
            Path(ranker_file).write_text(
                json.dumps(ranker, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "does not reproduce"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="htft-tamper",
                        asian_side=None,
                        total_side=None,
                        primary_market="none",
                        model_version=None,
                        score_model_file=None,
                        htft_observation_model_file=model_file,
                        htft_observation_ranker_file=ranker_file,
                    )
                )

    def test_htft_observation_rejects_rehashed_self_reported_ev_and_gates(self):
        with tempfile.TemporaryDirectory() as base:
            model_file, ranker_file = write_htft_observation_files(
                base, "htft-gate-tamper"
            )
            ranker = json.loads(Path(ranker_file).read_text(encoding="utf-8"))
            scenario = ranker["scenarios"][0]
            scenario["ev"] = 0.99
            scenario["edge_pp"] = 99.0
            scenario["diagnostic_failed_thresholds"] = []
            scenario["failed_thresholds"] = []
            Path(ranker_file).write_text(
                json.dumps(ranker, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "does not reproduce"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="htft-gate-tamper",
                        asian_side=None,
                        total_side=None,
                        primary_market="none",
                        model_version=None,
                        score_model_file=None,
                        htft_observation_model_file=model_file,
                        htft_observation_ranker_file=ranker_file,
                    )
                )

    def test_htft_observation_rejects_prediction_content_hash_tampering(self):
        with tempfile.TemporaryDirectory() as base:
            model_file, ranker_file = write_htft_observation_files(
                base, "htft-hash-tamper"
            )
            model = json.loads(Path(model_file).read_text(encoding="utf-8"))
            model["provenance"]["training_cutoff_date"] = "2026-07-19"
            Path(model_file).write_text(
                json.dumps(model, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                ValueError, "prediction_hash does not match prediction contents"
            ):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="htft-hash-tamper",
                        asian_side=None,
                        total_side=None,
                        primary_market="none",
                        model_version=None,
                        score_model_file=None,
                        htft_observation_model_file=model_file,
                        htft_observation_ranker_file=ranker_file,
                    )
                )

    def test_htft_observation_requires_ranker_to_preserve_external_anchor(self):
        with tempfile.TemporaryDirectory() as base:
            anchor = {
                "kind": "half_time_current_market",
                "complete": True,
                "de_vigged": True,
                "source": "https://example.test/half-time-odds",
                "captured_at": "2026-07-21T18:50:00+09:00",
                "production_pair_mass_gate_validated": False,
            }
            model_file, ranker_file = write_htft_observation_files(
                base,
                "htft-anchor-drop",
                half_time_anchor=anchor,
                drop_ranker_anchor=True,
            )
            with self.assertRaisesRegex(ValueError, "requires matching anchor_context"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="htft-anchor-drop",
                        asian_side=None,
                        total_side=None,
                        primary_market="none",
                        model_version=None,
                        score_model_file=None,
                        htft_observation_model_file=model_file,
                        htft_observation_ranker_file=ranker_file,
                    )
                )

    def test_htft_observation_matches_external_anchor_source_and_absolute_time(self):
        anchor = {
            "kind": "half_time_current_market",
            "complete": True,
            "de_vigged": True,
            "source": "https://example.test/half-time-odds",
            "captured_at": "2026-07-21T18:50:00+09:00",
            "production_pair_mass_gate_validated": False,
        }
        with tempfile.TemporaryDirectory() as base:
            model_file, ranker_file = write_htft_observation_files(
                base,
                "htft-anchor-match",
                half_time_anchor=anchor,
            )
            model = json.loads(Path(model_file).read_text(encoding="utf-8"))
            model["provenance"]["marginal_targets"]["half_time"]["captured_at"] = (
                "2026-07-21T09:50:00Z"
            )
            model["prediction_hash"] = memory_store.canonical_prediction_hash(model)
            Path(model_file).write_text(
                json.dumps(model, ensure_ascii=False), encoding="utf-8"
            )

            created = memory_store.cmd_record(
                record_args(
                    base,
                    match_id="htft-anchor-match",
                    asian_side=None,
                    total_side=None,
                    primary_market="none",
                    model_version=None,
                    score_model_file=None,
                    htft_observation_model_file=model_file,
                    htft_observation_ranker_file=ranker_file,
                )
            )["record"]
            ranker_audit = created["candidate_audits"][0]["ranker"]
            self.assertEqual(
                ranker_audit["matrix_mode"], "half_time_market_anchor_unvalidated"
            )
            self.assertEqual(ranker_audit["anchor_context"]["source"], anchor["source"])

        with tempfile.TemporaryDirectory() as base:
            model_file, ranker_file = write_htft_observation_files(
                base,
                "htft-anchor-source-mismatch",
                half_time_anchor=anchor,
            )
            model = json.loads(Path(model_file).read_text(encoding="utf-8"))
            model["provenance"]["marginal_targets"]["half_time"]["source"] = (
                "https://example.test/different-source"
            )
            model["prediction_hash"] = memory_store.canonical_prediction_hash(model)
            Path(model_file).write_text(
                json.dumps(model, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "does not match model marginal"):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id="htft-anchor-source-mismatch",
                        asian_side=None,
                        total_side=None,
                        primary_market="none",
                        model_version=None,
                        score_model_file=None,
                        htft_observation_model_file=model_file,
                        htft_observation_ranker_file=ranker_file,
                    )
                )

    def _assert_htft_anchor_time_rejected(
        self,
        *,
        match_id: str,
        captured_at: str,
        message: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as base:
            anchor = {
                "kind": "half_time_current_market",
                "complete": True,
                "de_vigged": True,
                "source": "https://example.test/half-time-odds",
                "captured_at": captured_at,
                "production_pair_mass_gate_validated": False,
            }
            model_file, ranker_file = write_htft_observation_files(
                base,
                match_id,
                half_time_anchor=anchor,
            )
            with self.assertRaisesRegex(ValueError, message):
                memory_store.cmd_record(
                    record_args(
                        base,
                        match_id=match_id,
                        asian_side=None,
                        total_side=None,
                        primary_market="none",
                        model_version=None,
                        score_model_file=None,
                        htft_observation_model_file=model_file,
                        htft_observation_ranker_file=ranker_file,
                    )
                )

    def test_htft_observation_rejects_anchor_after_model_generation(self):
        self._assert_htft_anchor_time_rejected(
            match_id="htft-anchor-after-generation",
            captured_at="2026-07-21T18:56:00+09:00",
            message="cannot be after model generated_at",
        )

    def test_htft_observation_rejects_anchor_after_archive_time(self):
        self._assert_htft_anchor_time_rejected(
            match_id="htft-anchor-after-archive",
            captured_at="2026-07-21T19:01:00+09:00",
            message="cannot be after archive time",
        )

    def test_htft_observation_rejects_anchor_at_or_after_kickoff(self):
        for label, captured_at in (
            ("at-kickoff", "2026-07-21T19:30:00+09:00"),
            ("after-kickoff", "2026-07-21T19:31:00+09:00"),
        ):
            with self.subTest(label=label):
                self._assert_htft_anchor_time_rejected(
                    match_id=f"htft-anchor-{label}",
                    captured_at=captured_at,
                    message="must be strictly before kickoff",
                )

    def test_htft_observation_without_half_score_stays_ungraded_and_never_blocks_review(
        self,
    ):
        with tempfile.TemporaryDirectory() as base:
            model_file, ranker_file = write_htft_observation_files(base, "htft-no-half")
            memory_store.cmd_record(
                record_args(
                    base,
                    match_id="htft-no-half",
                    asian_side=None,
                    total_side=None,
                    primary_market="none",
                    model_version=None,
                    score_model_file=None,
                    htft_observation_model_file=model_file,
                    htft_observation_ranker_file=ranker_file,
                )
            )
            reviewed = review_command(
                SimpleNamespace(
                    base_dir=base,
                    verified_finished=True,
                    verification_source="https://example.test/final",
                    verification_collected_at="2026-07-21T21:00:00+09:00",
                    match_id="htft-no-half",
                    home_score=1,
                    away_score=0,
                    half_home_score=None,
                    half_away_score=None,
                    key_learning="缺少半场比分时观察样本不评级也不阻塞全场复盘",
                )
            )
            diagnostic = reviewed["record"]["observation_diagnostics"][0]
            self.assertEqual(diagnostic["status"], "ungraded_missing_half_time_score")
            self.assertIsNone(diagnostic["top2_hit"])
            self.assertEqual(reviewed["stats"]["primary"]["matches"], 0)
            self.assertEqual(
                reviewed["stats"]["observation_gate_funnel"]["markets"]["htft"][
                    "diagnostics"
                ]["ungraded_observations"],
                1,
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
        self.assertAlmostEqual(metrics["one_x_two_multiclass_brier"], 0.5125)
        self.assertAlmostEqual(
            metrics["one_x_two_multiclass_log_loss"], expected_log_loss
        )
        league_metrics = stats["leagues"][league_key]["one_x_two_metrics"]
        self.assertEqual(league_metrics["sample_count"], 2)
        self.assertAlmostEqual(league_metrics["one_x_two_multiclass_brier"], 0.5125)
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
        self.assertEqual(stats["legacy_or_quarantined"]["primary"]["profit_units"], 0.9)
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
            legacy_stats["legacy_or_quarantined"]["primary_by_market"]["asian"][
                "matches"
            ],
            1,
        )
        self.assertEqual(
            legacy_stats["legacy_or_quarantined"]["primary"]["profit_units"],
            -1.0,
        )

    def test_forward_bound_review_rejects_missing_replayable_source_evidence(self):
        with tempfile.TemporaryDirectory() as base:
            recorded = memory_store.cmd_record(record_args(base, match_id="source-gap"))
            record = recorded["record"]
            policy = {
                "schema_version": memory_store.forward_policy.LEGACY_POLICY_SCHEMA_VERSION,
                "artifact_type": "soccer_prediction_policy_freeze",
                "created_at": "2026-07-20T00:00:00+00:00",
                "code": {
                    "commit": "a" * 40,
                    "protected_files": {"SKILL.md": "sha256:" + "1" * 64},
                },
                "data": {
                    "manifest_path": "manifest.json",
                    "file_sha256": "sha256:" + "2" * 64,
                    "declared_manifest_hash": "sha256:" + "3" * 64,
                },
                "models": {
                    "registry_path": "registry.json",
                    "file_sha256": "sha256:" + "4" * 64,
                    "declared_registry_hash": "sha256:" + "5" * 64,
                },
                "policy": {
                    "market_policy_version": "test",
                    "market_status": {"total": "formal"},
                    "selector": {"version": "test"},
                    "release_thresholds": {"minimum_firms": 1},
                    "candidate_evaluation": {"schema_version": "test"},
                    "display_policy": {"joint_event_count": 2},
                },
                "confirmation_contract": {
                    "retrospective_records_allowed": False,
                    "parameter_or_threshold_changes_allowed": False,
                    "prediction_affecting_bugfix_starts_new_cohort": True,
                    "all_candidates_abstentions_and_unavailable_markets_required": True,
                    "executable_timestamped_prices_required_for_market_comparison": True,
                    "promotion_is_manual": True,
                    "promotion_requirements": ["proper_scores"],
                },
            }
            policy["policy_hash"] = memory_store.forward_policy._hash_json(policy)
            policy["policy_id"] = (
                "untouched-live-forward-" + policy["policy_hash"].split(":", 1)[1][:16]
            )
            binding = {
                "schema_version": memory_store.forward_policy.RECORD_BINDING_SCHEMA_VERSION,
                "cohort_id": "test-cohort",
                "cohort_hash": "sha256:" + "6" * 64,
                "cohort_starts_at": "2026-07-20T00:01:00+00:00",
                "policy_id": policy["policy_id"],
                "policy_hash": policy["policy_hash"],
                "policy_snapshot": policy,
                "recorded_code_commit": "a" * 40,
                "archived_at": record["updated_at"],
                "untouched_confirmation_eligible": True,
            }
            binding["binding_hash"] = memory_store.forward_policy._hash_json(binding)
            record["forward_policy_binding"] = binding
            record["source_evidence_audit"] = None
            memory_store.save_history(memory_store.data_path(base), [record])

            with self.assertRaisesRegex(
                ValueError, "require replayable archived source evidence"
            ):
                review_command(
                    SimpleNamespace(
                        base_dir=base,
                        verified_finished=True,
                        verification_source="https://example.test/final",
                        verification_collected_at="2026-07-21T21:00:00+09:00",
                        match_id="source-gap",
                        home_score=1,
                        away_score=0,
                        half_home_score=0,
                        half_away_score=0,
                        home_corners=None,
                        away_corners=None,
                        key_learning="missing source evidence must fail closed",
                    )
                )


if __name__ == "__main__":
    unittest.main()

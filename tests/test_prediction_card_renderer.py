from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prediction_card_renderer.py"
SPEC = importlib.util.spec_from_file_location("prediction_card_renderer", SCRIPT)
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = renderer
SPEC.loader.exec_module(renderer)


def _payload() -> dict:
    payload = {
        "rows": [
            {
                "id": "周一001",
                "archive_match_id": "9001",
                "archive_stage": "initial",
                "time": "18:30",
                "league": "韩K联",
                "home_team": "主队A",
                "away_team": "客队B",
                "status": "formal_primary",
            },
            {
                "id": "周一002",
                "archive_match_id": "9002",
                "archive_stage": "initial",
                "time": "20:00",
                "league": "瑞典超",
                "home_team": "观察队",
                "away_team": "样本队",
                "status": "observation",
            },
            {
                "id": "周一003",
                "archive_match_id": "9003",
                "archive_stage": "initial",
                "time": "22:00",
                "league": "英超",
                "home_team": "待定队",
                "away_team": "不追队",
                "status": "no_bet",
            },
        ],
    }
    history = _history_index()
    for row in payload["rows"]:
        row["archive_version_hash"] = renderer.archive_version_hash(
            history[row["archive_match_id"]]
        )
    return payload


def _joint_artifact(
    *,
    home_team: str = "主队A",
    away_team: str = "客队B",
    first: tuple[str, str, float] = ("DD", "1-1", 0.058),
    second: tuple[str, str, float] = ("AA", "1-2", 0.045),
    third: tuple[str, str, float] | None = None,
) -> dict:
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

    def event(slot: int, value: tuple[str, str, float]) -> dict:
        home_goals, away_goals = (int(item) for item in value[1].split("-"))
        return {
            "slot": slot,
            "htft": value[0],
            "score": value[1],
            "home_goals": home_goals,
            "away_goals": away_goals,
            "probability": value[2],
            "status": "high_variance_reference",
            "recommendation_eligible": False,
            "counts_toward_primary_record": False,
            "odds_available": False,
        }

    ranked_values = [first, second] + ([third] if third is not None else [])
    used = {(value[0], value[1]) for value in ranked_values}
    branch_support = [
        value
        for value in (
            ("DD", "0-0", 0.018),
            ("DA", "0-1", 0.021),
            ("DH", "1-0", 0.020),
        )
        if (value[0], value[1]) not in used
    ]
    all_values = ranked_values + branch_support
    used = {(value[0], value[1]) for value in all_values}
    filler_keys: list[tuple[str, str]] = []
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
                    filler_keys.append(key)
            if len(filler_keys) >= 60:
                break
        if len(filler_keys) >= 60:
            break
    remainder = 1.0 - sum(value[2] for value in all_values)
    assert remainder > 0.0
    filler_probability = remainder / len(filler_keys)
    assert filler_probability < second[2]

    joint_cells = []
    for value in all_values:
        home_goals, away_goals = (int(item) for item in value[1].split("-"))
        joint_cells.append(
            {
                "htft": value[0],
                "score": value[1],
                "home_goals": home_goals,
                "away_goals": away_goals,
                "probability": value[2],
            }
        )
    for htft, score in filler_keys:
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
        "schema_version": renderer.public_market_outlook.joint_scenario_model.LEGACY_SCHEMA_VERSION,
        "model_version": renderer.public_market_outlook.joint_scenario_model.LEGACY_MODEL_VERSION,
        "prediction_hash": "sha256:" + "a" * 64,
        "probability_mode": "model_only",
        "formal_eligible": False,
        "fixture": {
            "home_team": home_team,
            "away_team": away_team,
            "kickoff": "2026-08-03T09:30:00Z",
        },
        "joint_top_two": [event(1, first), event(2, second)],
        "joint_cells": joint_cells,
        "htft_marginal": {
            "half_time_result_probabilities": {"H": 0.35, "D": 0.40, "A": 0.25},
            "full_time_result_probabilities": {"H": 0.42, "D": 0.31, "A": 0.27},
            "code_probabilities": {
                "HH": 0.20,
                "HD": 0.10,
                "HA": 0.05,
                "DH": 0.11,
                "DD": 0.16,
                "DA": 0.13,
                "AH": 0.11,
                "AD": 0.05,
                "AA": 0.09,
            },
        },
        "derived": {
            "one_x_two": {"home": 0.42, "draw": 0.31, "away": 0.27},
            "total_goals_distribution": {
                "0": 0.05,
                "1": 0.15,
                "2": 0.32,
                "3": 0.28,
                "4": 0.20,
            },
            "goal_ranges": {"0-1": 0.20, "2-3": 0.60, "4-6": 0.20, "7+": 0.0},
            "btts": {"yes": 0.47, "no": 0.53},
        },
        "derived_field_audits": audits,
    }


def _valid_corner_observation_audit() -> dict:
    return {
        "_renderer_test_valid": True,
        "kind": renderer.memory_store.CANDIDATE_EVALUATION_KIND,
        "schema_version": renderer.memory_store.CANDIDATE_EVALUATION_SCHEMA_VERSION,
        "candidates": [
            {
                "candidate_id": "sha256:" + "c" * 64,
                "market": "corner_total",
                "identity": "corner_total:over:9.5",
                "side": "over",
                "line": 9.5,
                "odds": 0.91,
                "odds_format": "hk",
                "probability": 0.56,
                "ev": 0.042,
                "edge_pp": 2.1,
                "market_probability": 0.539,
                "market_signal": "aligned",
                "firm_count": 5,
                "counterfactual_eligible": True,
                "formal_eligible": False,
                "shadow_selected": True,
                "shadow_rank": 1,
                "shadow_confidence": {
                    "score": 72.0,
                    "settlement_safety_probability": 0.64,
                    "firm_count": 5,
                },
                "gates": [
                    {
                        "gate": "market_policy_enabled",
                        "category": "release",
                        "passed": False,
                        "reasons": ["market_observation_only_under_active_policy"],
                    }
                ],
            }
        ],
        "market_manifest": [
            {
                "market": market,
                "status": "evaluated" if market == "corner_total" else "unavailable",
                "reasons": [] if market == "corner_total" else ["not_collected"],
            }
            for market in renderer.memory_store.PRIMARY_MARKETS
        ],
    }


def _legacy_htft_observation_audit() -> dict:
    return {
        "_renderer_test_valid": True,
        "market": "htft",
        "top_two": [
            {"selection": "DA", "probability": 0.30},
            {"selection": "DD", "probability": 0.20},
        ],
    }


def _history() -> list[dict]:
    return [
        {
            "match_id": "9001",
            "mode": "prematch",
            "status": "pending",
            "analysis_stage": "initial",
            "created_at": "2026-08-03T00:00:00Z",
            "updated_at": "2026-08-03T00:10:00Z",
            "kickoff": "2026-08-03T09:30:00Z",
            "user_timezone": "Asia/Tokyo",
            "league_key": "韩K联",
            "home_team": "主队A",
            "away_team": "客队B",
            "primary_market": "total",
            "primary_pick": {"side": "under", "line": 2.5, "odds": 0.92},
            "candidate_audits": [_valid_corner_observation_audit()],
            "display_exact_score_picks": [{"score": "9-9"}, {"score": "8-8"}],
            "_validated_joint_artifact": _joint_artifact(),
        },
        {
            "match_id": "9002",
            "mode": "prematch",
            "status": "pending",
            "analysis_stage": "initial",
            "created_at": "2026-08-03T00:00:00Z",
            "updated_at": "2026-08-03T00:10:00Z",
            "kickoff": "2026-08-03T11:00:00Z",
            "user_timezone": "Asia/Tokyo",
            "league_key": "瑞典超",
            "home_team": "观察队",
            "away_team": "样本队",
            "primary_market": None,
            "primary_pick": None,
            "candidate_audits": [_valid_corner_observation_audit()],
            "display_exact_score_picks": [{"score": "7-7"}, {"score": "6-6"}],
            "_validated_joint_artifact": _joint_artifact(
                home_team="观察队", away_team="样本队"
            ),
        },
        {
            "match_id": "9003",
            "mode": "prematch",
            "status": "pending",
            "analysis_stage": "initial",
            "created_at": "2026-08-03T00:00:00Z",
            "updated_at": "2026-08-03T00:10:00Z",
            "kickoff": "2026-08-03T13:00:00Z",
            "user_timezone": "Asia/Tokyo",
            "league_key": "英超",
            "home_team": "待定队",
            "away_team": "不追队",
            "primary_market": None,
            "primary_pick": None,
            "candidate_audits": [],
            "display_exact_score_picks": [{"score": "5-5"}, {"score": "4-4"}],
            "_validated_joint_artifact": _joint_artifact(
                home_team="待定队", away_team="不追队"
            ),
        },
    ]


def _history_index() -> dict[str, dict]:
    return {record["match_id"]: record for record in _history()}


def _rebind_row(payload: dict, row_index: int, history: dict[str, dict]) -> None:
    row = payload["rows"][row_index]
    record = history[row["archive_match_id"]]
    row["archive_version_hash"] = renderer.archive_version_hash(record)


def _validated_joint_scenario_audit(record: dict) -> dict | None:
    artifact = record.get("_validated_joint_artifact")
    return artifact if isinstance(artifact, dict) else None


class PredictionCardRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            renderer.memory_store,
            "validated_joint_scenario_audit",
            side_effect=_validated_joint_scenario_audit,
            create=True,
        )
        self.validated_joint = patcher.start()
        self.addCleanup(patcher.stop)
        observation_patcher = mock.patch.object(
            renderer.plain_text_formatter.publication_outlook.memory_store,
            "validated_candidate_evaluation_audit",
            side_effect=lambda audit, version: (
                audit.get("_renderer_test_valid") is True
            ),
        )
        self.validated_observation = observation_patcher.start()
        self.addCleanup(observation_patcher.stop)
        joint_validator_patcher = mock.patch.object(
            renderer.public_market_outlook.joint_scenario_model,
            "validate_prediction",
        )
        self.joint_validator = joint_validator_patcher.start()
        self.addCleanup(joint_validator_patcher.stop)

    def test_star_is_archive_derived_and_cannot_be_supplied(self) -> None:
        payload = _payload()
        payload["rows"][0]["star"] = True
        with self.assertRaisesRegex(ValueError, "must not be supplied"):
            renderer.validate_payload(payload, _history_index())

        card = renderer.validate_payload(_payload(), _history_index())
        self.assertTrue(card.rows[0].star)
        self.assertFalse(card.rows[1].star)
        self.assertFalse(card.rows[2].star)
        self.assertEqual(card.rows[0].primary, "小2.5 @0.92")

    def test_no_bet_never_turns_a_marginal_leader_into_a_primary(self) -> None:
        payload = _payload()
        payload["rows"] = payload["rows"][2:]
        card = renderer.validate_payload(payload, _history_index())
        row = card.rows[0]

        self.assertEqual(row.primary, "无正式主推")
        self.assertEqual(row.status, "no_bet")
        self.assertFalse(row.star)
        svg = renderer.render_svg(card)
        self.assertIn("正式主推 0 场", svg)
        self.assertNotIn("42.0% ★", "".join(ET.fromstring(svg).itertext()))

    def test_caller_cannot_hide_an_archived_observation_as_no_bet(self) -> None:
        payload = _payload()
        payload["rows"] = payload["rows"][1:2]
        payload["rows"][0]["status"] = "no_bet"
        with self.assertRaisesRegex(ValueError, "archive-derived status"):
            renderer.validate_payload(payload, _history_index())

    def test_duplicate_archived_match_is_rejected(self) -> None:
        payload = _payload()
        payload["rows"] = [
            copy.deepcopy(payload["rows"][0]),
            copy.deepcopy(payload["rows"][0]),
        ]
        with self.assertRaisesRegex(ValueError, "duplicates an archived match"):
            renderer.validate_payload(payload, _history_index())

    def test_header_is_archive_derived_and_identifier_cannot_carry_pick_markers(
        self,
    ) -> None:
        for field, value in (
            ("date", "2099-12-31"),
            ("title", "大2.5 @0.95"),
            ("subtitle", "临场复查 胜胜 / 2-1"),
        ):
            with self.subTest(field=field):
                payload = _payload()
                payload[field] = value
                with self.assertRaisesRegex(
                    ValueError, "header metadata is archive-derived"
                ):
                    renderer.validate_payload(payload, _history_index())

        payload = _payload()
        payload["rows"][0]["id"] = "周一001◇"
        with self.assertRaisesRegex(ValueError, "recommendation markers"):
            renderer.validate_payload(payload, _history_index())

        card = renderer.validate_payload(_payload(), _history_index())
        self.assertEqual(card.date, "2026-08-03")
        self.assertEqual(card.title, "今日足球扫盘")
        self.assertEqual(card.subtitle, "初盘分析｜3场")

    def test_card_cannot_mix_archive_stages_or_local_kickoff_dates(self) -> None:
        history = _history_index()
        history["9002"]["analysis_stage"] = "lineup-check"
        payload = _payload()
        payload["rows"][1]["archive_stage"] = "lineup-check"
        _rebind_row(payload, 1, history)
        with self.assertRaisesRegex(ValueError, "cannot mix initial and lineup-check"):
            renderer.validate_payload(payload, history)

        history = _history_index()
        history["9002"]["kickoff"] = "2026-08-04T11:00:00Z"
        payload = _payload()
        payload["rows"][1]["time"] = "20:00"
        _rebind_row(payload, 1, history)
        with self.assertRaisesRegex(ValueError, "cannot mix local kickoff dates"):
            renderer.validate_payload(payload, history)

    def test_reviewed_archive_can_rerender_its_immutable_prematch_version(self) -> None:
        history = _history_index()
        history["9003"]["status"] = "reviewed"
        payload = _payload()
        payload["rows"] = payload["rows"][2:]
        _rebind_row(payload, 0, history)

        card = renderer.validate_payload(payload, history)
        self.assertEqual(card.rows[0].league, "英超")
        self.assertFalse(card.rows[0].star)

    def test_formal_status_requires_matching_active_archive_primary(self) -> None:
        payload = _payload()
        history = _history_index()
        payload["rows"][0]["archive_match_id"] = "9002"
        payload["rows"][0]["home_team"] = "观察队"
        payload["rows"][0]["away_team"] = "样本队"
        payload["rows"][0]["time"] = "20:00"
        payload["rows"][0]["league"] = "瑞典超"
        _rebind_row(payload, 0, history)
        with self.assertRaisesRegex(ValueError, "without an archived primary_pick"):
            renderer.validate_payload(payload, history)

        payload = _payload()
        payload["rows"][0]["status"] = "observation"
        payload["rows"][0]["primary"] = "观察方向"
        with self.assertRaisesRegex(
            ValueError, "conflicts with an archived active formal primary"
        ):
            renderer.validate_payload(payload, _history_index())

    def test_observation_requires_a_validated_candidate_audit(self) -> None:
        payload = _payload()
        history = _history_index()
        history["9002"].pop("candidate_audits")
        _rebind_row(payload, 1, history)
        with self.assertRaisesRegex(ValueError, "without validated candidate_audits"):
            renderer.validate_payload(payload, history)

        payload = _payload()
        history = _history_index()
        history["9002"]["candidate_audits"] = [_legacy_htft_observation_audit()]
        _rebind_row(payload, 1, history)
        with self.assertRaisesRegex(ValueError, "without validated candidate_audits"):
            renderer.validate_payload(payload, history)

    def test_observation_does_not_occupy_the_formal_primary_column(self) -> None:
        payload = _payload()
        payload["rows"][1]["primary"] = "角球小9.5 @0.91"
        card = renderer.validate_payload(payload, _history_index())
        self.assertEqual(card.rows[1].primary, "无正式主推")
        self.assertFalse(card.rows[1].star)
        self.assertNotIn("角球小9.5", renderer.render_svg(card))
        self.assertIn("角球大9.5", renderer.render_svg(card))
        self.assertTrue(
            any("角球大9.5" in line for line in card.publication_rows[1].lines)
        )

    def test_publication_panel_shows_all_three_states_without_changing_columns(
        self,
    ) -> None:
        card = renderer.validate_payload(_payload(), _history_index())
        self.assertEqual(
            tuple((label, key, width) for label, key, width in renderer.COLUMNS),
            (
                ("编号", "id", 100),
                ("时间", "time", 90),
                ("赛事", "league", 110),
                ("主队 vs 客队", "match", 320),
                ("主推", "primary", 250),
                ("联合首选情景总球", "total_goals", 170),
                ("半全场", "htft", 220),
                ("波胆", "scores", 180),
            ),
        )
        self.assertEqual(
            tuple(row.state for row in card.publication_rows),
            ("formal_primary", "observation_primary", "no_usable_direction"),
        )
        self.assertEqual(card.rows[1].primary, "无正式主推")
        observation = "\n".join(card.publication_rows[1].lines)
        self.assertIn("◇ 观察首选：角球大9.5 @0.91", observation)
        self.assertIn("模型 EV +4.2%", observation)
        self.assertIn("相对无水边际 +2.1pp", observation)
        self.assertIn("政策阻断：该市场当前仅允许观察", observation)
        self.assertIn("不下注、不计战绩", observation)
        no_direction = "\n".join(card.publication_rows[2].lines)
        self.assertIn("— 无可用方向", no_direction)
        self.assertIn("数据阻断：候选评估不可用", no_direction)
        for publication_row in card.publication_rows:
            self.assertIn(
                "初盘结论待 T−30 复核首发与即时盘口",
                "\n".join(publication_row.lines),
            )
            self.assertNotIn("…", "\n".join(publication_row.lines))
            self.assertNotIn("...", "\n".join(publication_row.lines))

    def test_lineup_panel_reports_maintained_observation_and_formal_upgrade(
        self,
    ) -> None:
        history = _history_index()
        initial = copy.deepcopy(history["9002"])
        history["9002"]["revisions"] = [initial]
        history["9002"]["analysis_stage"] = "lineup-check"
        payload = _payload()
        payload["rows"] = payload["rows"][1:2]
        payload["rows"][0]["archive_stage"] = "lineup-check"
        _rebind_row(payload, 0, history)

        maintained = renderer.validate_payload(payload, history)
        self.assertIn(
            "临场仍受政策阻断，观察首选维持",
            "\n".join(maintained.publication_rows[0].lines),
        )

        history["9002"]["primary_market"] = "corner_total"
        history["9002"]["primary_pick"] = {
            "side": "over",
            "line": 9.5,
            "odds": 0.91,
            "probability": 0.56,
            "ev": 0.042,
        }
        payload["rows"][0]["status"] = "formal_primary"
        _rebind_row(payload, 0, history)
        upgraded = renderer.validate_payload(payload, history)
        self.assertEqual(upgraded.rows[0].primary, "角球大9.5 @0.91")
        self.assertIn(
            "临场已从观察首选升级为正式主推",
            "\n".join(upgraded.publication_rows[0].lines),
        )

    def test_publication_panel_geometry_grows_and_fits_svg_and_pillow(self) -> None:
        payload = _payload()
        payload["rows"] = payload["rows"][1:2]
        history = _history_index()
        baseline = renderer.validate_payload(payload, history)
        candidate = history["9002"]["candidate_audits"][0]["candidates"][0]
        candidate["gates"].extend(
            {
                "gate": f"long_policy_gate_{index}",
                "category": "release",
                "passed": False,
                "reasons": [f"long_machine_reason_{index}_with_complete_context"],
            }
            for index in range(10)
        )
        _rebind_row(payload, 0, history)
        card = renderer.validate_payload(payload, history)
        self.assertGreater(card.publication_height, baseline.publication_height)
        table_bottom = (
            renderer.TITLE_HEIGHT
            + renderer.HEADER_HEIGHT
            + len(card.rows) * renderer.ROW_HEIGHT
        )
        _header_top, blocks, publication_bottom = renderer._publication_geometry(
            card, table_bottom
        )
        self.assertEqual(
            publication_bottom - table_bottom,
            card.publication_height,
        )

        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is not installed")
        image = Image.new("RGB", (renderer.WIDTH, card.height))
        draw = ImageDraw.Draw(image)
        for publication_row, top, block_height in blocks:
            for line_index, line in enumerate(publication_row.lines):
                font = renderer._load_font(
                    renderer.PUBLICATION_FONT_SIZE, bold=line_index == 0
                )
                x = 74
                y = (
                    top
                    + renderer.PUBLICATION_BLOCK_PADDING
                    + line_index * renderer.PUBLICATION_LINE_HEIGHT
                )
                bounds = draw.textbbox((x, y), line, font=font)
                self.assertLessEqual(bounds[2], renderer.WIDTH - 58)
                self.assertLessEqual(bounds[3], top + block_height)
        svg = renderer.render_svg(card)
        self.assertNotIn("…", svg)
        self.assertNotIn("...", svg)

    def test_unqualified_corner_candidate_cannot_be_promoted_to_observation(
        self,
    ) -> None:
        payload = _payload()
        history = _history_index()
        history["9002"]["candidate_audits"][0]["candidates"][0][
            "counterfactual_eligible"
        ] = False
        _rebind_row(payload, 1, history)
        with self.assertRaisesRegex(ValueError, "without validated candidate_audits"):
            renderer.validate_payload(payload, history)

    def test_row_is_bound_to_exact_archive_stage_hash_time_and_league(self) -> None:
        payload = _payload()
        payload["rows"] = payload["rows"][:1]
        history = _history_index()

        tampered = copy.deepcopy(payload)
        tampered["rows"][0]["archive_version_hash"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(
            ValueError, "does not match the selected archived version"
        ):
            renderer.validate_payload(tampered, history)

        wrong_time = copy.deepcopy(payload)
        wrong_time["rows"][0]["time"] = "18:31"
        with self.assertRaisesRegex(ValueError, "time does not match"):
            renderer.validate_payload(wrong_time, history)

        wrong_league = copy.deepcopy(payload)
        wrong_league["rows"][0]["league"] = "芬超"
        with self.assertRaisesRegex(ValueError, "league does not match"):
            renderer.validate_payload(wrong_league, history)

    def test_archive_version_hash_binds_ordinary_league_identity(self) -> None:
        record = _history_index()["9003"]
        original = renderer.archive_version_hash(record)
        changed = copy.deepcopy(record)
        changed["league"] = "england_premier_league"
        changed["league_key"] = "england_premier_league"
        self.assertNotEqual(renderer.archive_version_hash(changed), original)

    def test_archived_raw_or_key_league_is_accepted_but_only_resolved_chinese_is_rendered(
        self,
    ) -> None:
        history = _history_index()
        archived = history["9003"]
        archived["league"] = "巴西杯16强次回合"
        archived["league_key"] = "brazil_serie_a"

        for supplied in (archived["league"], archived["league_key"], "巴西杯"):
            with (
                self.subTest(supplied=supplied),
                mock.patch.object(
                    renderer.plain_text_formatter,
                    "league_display_name",
                    return_value="巴西杯",
                ),
            ):
                payload = _payload()
                payload["rows"] = payload["rows"][2:]
                payload["rows"][0]["league"] = supplied
                payload["rows"][0]["archive_version_hash"] = (
                    renderer.archive_version_hash(archived)
                )

                card = renderer.validate_payload(payload, history)
                self.assertEqual(card.rows[0].league, "巴西杯")
                svg = renderer.render_svg(card)
                self.assertIn("巴西杯", svg)
                self.assertNotIn("brazil_serie_a", svg)
                self.assertNotIn("巴西杯16强次回合", svg)

        with mock.patch.object(
            renderer.plain_text_formatter,
            "league_display_name",
            return_value="巴西杯",
        ):
            forged = _payload()
            forged["rows"] = forged["rows"][2:]
            forged["rows"][0]["league"] = "巴甲"
            forged["rows"][0]["archive_version_hash"] = renderer.archive_version_hash(
                archived
            )
            with self.assertRaisesRegex(ValueError, "league does not match"):
                renderer.validate_payload(forged, history)

    def test_verified_competition_metadata_requires_its_independent_hash_binding(
        self,
    ) -> None:
        history = _history_index()
        archived = history["9003"]
        archived.update(
            {
                "league": "brazil_serie_a",
                "league_key": "brazil_serie_a",
                "source_url": "https://zq.titan007.com/analysis/9003cn.htm",
            }
        )
        evidence = renderer.memory_store.build_competition_evidence(
            archived,
            competition_key="brazil_cup",
            competition_label="巴西杯",
            competition_id="186",
            verification_source=archived["source_url"],
            source_locator="//info.titan007.com/cup_match/2026-2027/cupmatch_vs/cupmatch_186.htm",
            collected_at="2026-08-03T00:00:00Z",
            _source_snapshot={
                "source_url": archived["source_url"],
                "response_url": archived["source_url"],
                "page_sha256": "sha256:" + "b" * 64,
                "etag": "",
                "last_modified": "",
                "collected_at": "2026-08-03T00:00:00Z",
                "header": {
                    "home_team": archived["home_team"],
                    "away_team": archived["away_team"],
                    "competition_label": "巴西杯",
                    "competition_id": "186",
                    "competition_locator": "//info.titan007.com/cup_match/2026-2027/cupmatch_vs/cupmatch_186.htm",
                },
            },
        )
        archived["competition_evidence"] = evidence
        payload = _payload()
        payload["rows"] = payload["rows"][2:]
        payload["rows"][0]["league"] = "brazil_serie_a"
        _rebind_row(payload, 0, history)

        with self.assertRaisesRegex(ValueError, "competition_evidence_hash"):
            renderer.validate_payload(payload, history)
        payload["rows"][0]["competition_evidence_hash"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "competition_evidence_hash"):
            renderer.validate_payload(payload, history)

        payload["rows"][0]["competition_evidence_hash"] = evidence["evidence_hash"]
        card = renderer.validate_payload(payload, history)
        self.assertEqual(card.rows[0].league, "巴西杯")
        self.assertNotIn("brazil_serie_a", renderer.render_svg(card))

    def test_initial_card_cannot_leak_active_lineup_version(self) -> None:
        history = _history_index()
        active = history["9001"]
        initial = renderer.memory_store.revision_snapshot(active)
        initial["_validated_joint_artifact"] = copy.deepcopy(
            active["_validated_joint_artifact"]
        )
        active["revisions"] = [initial]
        active["analysis_stage"] = "lineup-check"
        active["updated_at"] = "2026-08-03T09:10:00Z"
        active["lineup_rechecked_at"] = "2026-08-03T09:10:00Z"
        active["primary_market"] = "asian"
        active["primary_pick"] = {
            "side": "away",
            "line": 0.5,
            "odds": 0.95,
            "role": "primary",
        }
        active["_validated_joint_artifact"] = _joint_artifact(
            first=("HH", "2-0", 0.20),
            second=("HD", "1-1", 0.10),
        )

        payload = _payload()
        payload["rows"] = payload["rows"][:1]
        payload["rows"][0]["archive_stage"] = "initial"
        initial_version = renderer._select_archived_version(
            active, "initial", "rows[0]"
        )
        payload["rows"][0]["archive_version_hash"] = renderer.archive_version_hash(
            initial_version
        )
        card = renderer.validate_payload(payload, history)
        self.assertEqual(card.rows[0].primary, "小2.5 @0.92")
        self.assertIn("平平", card.rows[0].htft)
        self.assertIn("1-1 5.8%", card.rows[0].scores)
        self.assertNotIn("胜胜", card.rows[0].htft)
        self.assertNotIn("2-0", card.rows[0].scores)

    def test_payload_cannot_supply_archive_derived_market_fields(self) -> None:
        forbidden_values = {
            "total_goals": "2球/3球",
            "htft": ["平平", "负负"],
            "scores": ["1-1", "1-2"],
            "one_x_two": "胜/平/负",
            "joint_scenarios": ["平平·1-1", "负负·1-2"],
            "top2_cumulative_probability": 0.99,
            "other_scenarios_probability": 0.01,
            "joint_uncertainty": {"level": "低"},
        }
        for field, value in forbidden_values.items():
            with self.subTest(field=field):
                payload = _payload()
                payload["rows"][0][field] = value
                with self.assertRaisesRegex(ValueError, "must not be supplied"):
                    renderer.validate_payload(payload, _history_index())

    def test_joint_market_columns_come_only_from_validated_artifact(self) -> None:
        history = _history_index()
        card = renderer.validate_payload(_payload(), history)
        first = card.rows[0]
        self.assertEqual(
            first.total_goals.splitlines(),
            ["2-3球", "Top2累计 10.3%", "其他情景 89.7%", "不确定度 高(v1)"],
        )
        self.assertEqual(first.htft, "平平\n负负")
        self.assertEqual(first.scores, "1-1 5.8%\n1-2 4.5%")
        self.assertEqual(self.validated_joint.call_count, 3)

        svg = renderer.render_svg(card)
        for label in ("主推", "联合首选情景总球", "半全场", "波胆"):
            self.assertIn(label, svg)
        self.assertIn("平平", svg)
        self.assertIn("1-1 5.8%", svg)
        self.assertIn("负负", svg)
        self.assertNotIn("胜平负", svg)
        self.assertNotIn("BTTS", svg)
        self.assertNotIn("9-9", svg)

    def test_goal_range_uses_joint_rank_one_while_htft_and_score_keep_top_two(
        self,
    ) -> None:
        history = _history_index()
        history["9001"]["_validated_joint_artifact"] = _joint_artifact(
            first=("DD", "0-0", 0.1086),
            second=("DA", "0-1", 0.0684),
            third=("HH", "2-0", 0.0500),
        )
        payload = _payload()
        _rebind_row(payload, 0, history)

        row = renderer.validate_payload(payload, history).rows[0]

        self.assertEqual(row.total_goals.splitlines()[0], "0-1球")
        self.assertIn("Top2累计 17.7%", row.total_goals)
        self.assertIn("其他情景 82.3%", row.total_goals)
        self.assertIn("不确定度 高(v1)", row.total_goals)
        self.assertEqual(row.htft.splitlines(), ["平平", "平负"])
        self.assertEqual(row.scores.splitlines(), ["0-0 10.9%", "0-1 6.8%"])
        self.assertNotIn("2-3球", row.total_goals)

    def test_rank_two_goal_range_is_validated_but_not_displayed(self) -> None:
        history = _history_index()
        history["9001"]["_validated_joint_artifact"] = _joint_artifact(
            first=("DD", "0-0", 0.1086),
            second=("DD", "1-1", 0.0684),
            third=("HH", "2-0", 0.0500),
        )
        payload = _payload()
        _rebind_row(payload, 0, history)

        row = renderer.validate_payload(payload, history).rows[0]

        self.assertEqual(row.total_goals.splitlines()[0], "0-1球")
        self.assertEqual(row.htft.splitlines(), ["平平", "平平"])
        self.assertEqual(row.scores.splitlines(), ["0-0 10.9%", "1-1 6.8%"])
        self.assertNotIn("2-3球", row.total_goals)

    def test_joint_distribution_displays_only_frozen_global_top_two(self) -> None:
        history = _history_index()
        history["9001"]["_validated_joint_artifact"] = _joint_artifact(
            first=("DD", "1-1", 0.058),
            second=("HH", "2-1", 0.045),
            third=("DA", "1-2", 0.040),
        )
        _rebind_row(_payload(), 0, history)
        payload = _payload()
        _rebind_row(payload, 0, history)
        row = renderer.validate_payload(payload, history).rows[0]
        self.assertEqual(row.htft.splitlines(), ["平平", "胜胜"])
        self.assertEqual(row.scores.splitlines(), ["1-1 5.8%", "2-1 4.5%"])
        self.assertNotIn("1-2 4.0%", row.scores)

    def test_global_top_two_with_same_htft_are_not_deduplicated(self) -> None:
        history = _history_index()
        history["9001"]["_validated_joint_artifact"] = _joint_artifact(
            first=("DD", "0-0", 0.065),
            second=("DD", "1-1", 0.061),
            third=("DH", "1-0", 0.059),
        )
        payload = _payload()
        _rebind_row(payload, 0, history)
        row = renderer.validate_payload(payload, history).rows[0]
        self.assertEqual(row.htft.splitlines(), ["平平", "平平"])
        self.assertEqual(
            row.scores.splitlines(),
            ["0-0 6.5%", "1-1 6.1%"],
        )
        self.assertEqual(row.htft.count("平平"), 2)
        self.assertNotRegex(row.htft + row.scores, "[①②③]")
        self.assertNotIn("1-0 5.9%", row.scores)

    def test_mixed_global_top_two_remain_probability_ranked(self) -> None:
        history = _history_index()
        history["9001"]["_validated_joint_artifact"] = _joint_artifact(
            first=("DD", "0-0", 0.065),
            second=("HH", "1-0", 0.061),
            third=("DD", "1-1", 0.059),
        )
        payload = _payload()
        _rebind_row(payload, 0, history)
        row = renderer.validate_payload(payload, history).rows[0]
        self.assertEqual(row.htft.splitlines(), ["平平", "胜胜"])
        self.assertEqual(
            row.scores.splitlines(),
            ["0-0 6.5%", "1-0 6.1%"],
        )

    def test_duplicate_public_branch_output_fails_closed(self) -> None:
        public = renderer.public_market_outlook.build_public_market_outlook(
            _joint_artifact()
        )
        public["joint_scenarios"]["items"][1]["htft"] = "DD"
        with mock.patch.object(
            renderer.public_market_outlook,
            "build_public_market_outlook",
            return_value=public,
        ):
            row = renderer.validate_payload(_payload(), _history_index()).rows[0]
        self.assertEqual(row.total_goals, "数据不足")
        self.assertEqual(row.htft, "数据不足")
        self.assertEqual(row.scores, "数据不足")

    def test_caller_or_renderer_cannot_override_recomputed_joint_concentration(
        self,
    ) -> None:
        public = renderer.public_market_outlook.build_public_market_outlook(
            _joint_artifact()
        )
        public["joint_scenarios"]["top2_cumulative_probability"] = 0.99
        public["joint_scenarios"]["other_scenarios_probability"] = 0.01
        public["joint_scenarios"]["uncertainty"]["label_zh"] = "低"
        with mock.patch.object(
            renderer.public_market_outlook,
            "build_public_market_outlook",
            return_value=public,
        ):
            row = renderer.validate_payload(_payload(), _history_index()).rows[0]
        self.assertEqual(
            (row.total_goals, row.htft, row.scores),
            ("数据不足", "数据不足", "数据不足"),
        )

    def test_missing_or_malformed_joint_artifact_fails_closed(self) -> None:
        for malformed in (None, {}, {"joint_top_two": []}):
            with self.subTest(malformed=malformed):
                history = _history_index()
                history["9003"]["_validated_joint_artifact"] = malformed
                card = renderer.validate_payload(_payload(), history)
                row = card.rows[2]
                self.assertEqual(row.primary, "无正式主推")
                self.assertEqual(row.total_goals, "数据不足")
                self.assertEqual(row.htft, "数据不足")
                self.assertEqual(row.scores, "数据不足")
                self.assertNotIn("模型首选：", renderer.render_svg(card))
                self.assertNotIn("5-5", renderer.render_svg(card))

    def test_mismatched_derived_probabilities_fail_closed_as_one_unit(self) -> None:
        history = _history_index()
        artifact = copy.deepcopy(history["9003"]["_validated_joint_artifact"])
        artifact["derived"]["one_x_two"]["home"] = 0.99
        history["9003"]["_validated_joint_artifact"] = artifact
        card = renderer.validate_payload(_payload(), history)
        self.assertEqual(
            (
                card.rows[2].primary,
                card.rows[2].total_goals,
                card.rows[2].htft,
                card.rows[2].scores,
            ),
            ("无正式主推", "数据不足", "数据不足", "数据不足"),
        )

    def test_history_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "prediction history is required"):
            renderer.validate_payload(_payload())

    def test_status_allowlist_is_strict(self) -> None:
        payload = _payload()
        payload["rows"][0]["status"] = "best_bet"
        with self.assertRaisesRegex(ValueError, "status must be one of"):
            renderer.validate_payload(payload, _history_index())

    def test_svg_escapes_xml_and_exposes_guarded_markers(self) -> None:
        payload = _payload()
        payload["rows"][0]["home_team"] = "红&蓝<队>"
        history = _history_index()
        history["9001"]["home_team"] = "红&蓝<队>"
        _rebind_row(payload, 0, history)
        card = renderer.validate_payload(payload, history)
        svg = renderer.render_svg(card)

        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn("红&amp;蓝&lt;队&gt;", svg)
        self.assertIn("小2.5 @0.92 ★", svg)
        self.assertIn("★", svg)
        rendered_text = "".join(root.itertext())
        self.assertEqual(rendered_text.count("无正式主推"), 3)
        self.assertNotIn("模型首选", rendered_text)

    def test_svg_file_is_written_and_height_tracks_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.json"
            history_path = root / "history.json"
            output_path = root / "card.svg"
            input_path.write_text(
                json.dumps(_payload(), ensure_ascii=False), encoding="utf-8"
            )
            history_path.write_text(
                json.dumps(_history(), ensure_ascii=False), encoding="utf-8"
            )

            returned = renderer.render_file(input_path, output_path, history_path)
            self.assertEqual(returned, output_path)
            self.assertTrue(output_path.is_file())
            xml = ET.parse(output_path).getroot()
            expected = (
                renderer.TITLE_HEIGHT
                + renderer.HEADER_HEIGHT
                + 3 * renderer.ROW_HEIGHT
                + renderer.validate_payload(
                    _payload(), _history_index()
                ).publication_height
                + renderer.FOOTER_HEIGHT
            )
            self.assertEqual(int(xml.attrib["height"]), expected)

    def test_svg_footer_explains_pairing_and_stays_inside_panel(self) -> None:
        card = renderer.validate_payload(_payload(), _history_index())
        root = ET.fromstring(renderer.render_svg(card))
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        panel = next(
            node
            for node in root.findall("svg:rect", namespace)
            if node.attrib.get("x") == str(renderer.SIDE_MARGIN)
            and node.attrib.get("y") == str(renderer.PANEL_VERTICAL_MARGIN)
        )
        footnote = next(
            node
            for node in root.findall("svg:text", namespace)
            if "联合首选情景总球取冻结联合第1名比分映射" in (node.text or "")
        )
        self.assertIn("Top2累计与不确定度由完整归档联合分布重算", footnote.text or "")
        self.assertIn("边际第一仅在文字审计", footnote.text or "")
        panel_bottom = int(panel.attrib["y"]) + int(panel.attrib["height"])
        self.assertGreaterEqual(panel_bottom - int(footnote.attrib["y"]), 30)

    def test_long_content_wraps_without_any_ellipsis(self) -> None:
        payload = _payload()
        history = _history_index()
        long_home = "非常非常长而且必须完整显示的主队名称足球俱乐部"
        payload["rows"][0]["home_team"] = long_home
        history["9001"]["home_team"] = long_home
        _rebind_row(payload, 0, history)
        svg = renderer.render_svg(renderer.validate_payload(payload, history))
        self.assertIn(long_home, "".join(ET.fromstring(svg).itertext()))
        self.assertNotIn("…", svg)
        self.assertNotIn("...", svg)

    def test_unsafe_derived_league_falls_back_without_rendering_injected_text(
        self,
    ) -> None:
        for league in (
            "主推大2.5",
            "大2.5赛事",
            "角球大10.5 @0.99",
            "伪赛事…",
            "超" * 40,
        ):
            with self.subTest(league=league):
                history = _history_index()
                history["9003"]["league_key"] = "custom"
                history["9003"]["league"] = league
                payload = _payload()
                payload["rows"] = payload["rows"][2:]
                payload["rows"][0]["league"] = league
                _rebind_row(payload, 0, history)
                card = renderer.validate_payload(payload, history)
                svg = renderer.render_svg(card)
                self.assertEqual(card.rows[0].league, "赛事待核验")
                self.assertNotIn(league, svg)

    def test_team_market_direction_shape_is_rejected_without_blocking_real_names(
        self,
    ) -> None:
        for injected in ("大2.5 @0.95", "角球大10.5", "受让+0.5"):
            with self.subTest(injected=injected):
                history = _history_index()
                payload = _payload()
                payload["rows"] = payload["rows"][:1]
                history["9001"]["home_team"] = injected
                payload["rows"][0]["home_team"] = injected
                _rebind_row(payload, 0, history)
                with self.assertRaisesRegex(ValueError, "recommendation markers"):
                    renderer.validate_payload(payload, history)

        for team in ("大阪钢巴", "大田韩亚市民", "Academy U21"):
            with self.subTest(team=team):
                history = _history_index()
                payload = _payload()
                payload["rows"] = payload["rows"][:1]
                history["9001"]["home_team"] = team
                payload["rows"][0]["home_team"] = team
                _rebind_row(payload, 0, history)
                card = renderer.validate_payload(payload, history)
                self.assertIn(team, card.rows[0].match)

    def test_team_width_limit_prevents_fixed_row_overlap(self) -> None:
        for too_wide in ("超" * 31, "W" * 48):
            with self.subTest(too_wide=too_wide[:4]):
                history = _history_index()
                payload = _payload()
                payload["rows"] = payload["rows"][:1]
                history["9001"]["home_team"] = too_wide
                payload["rows"][0]["home_team"] = too_wide
                _rebind_row(payload, 0, history)
                with self.assertRaisesRegex(ValueError, "too wide"):
                    renderer.validate_payload(payload, history)

        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is not installed")
        history = _history_index()
        payload = _payload()
        payload["rows"] = payload["rows"][:1]
        maximum = "超" * 30
        history["9001"]["home_team"] = maximum
        history["9001"]["away_team"] = maximum
        payload["rows"][0]["home_team"] = maximum
        payload["rows"][0]["away_team"] = maximum
        _rebind_row(payload, 0, history)
        row = renderer.validate_payload(payload, history).rows[0]
        lines = renderer._cell_lines(row.match, renderer.CELL_WRAP_UNITS[3])
        image = Image.new("RGB", (renderer.COLUMNS[3][2], renderer.ROW_HEIGHT))
        draw = ImageDraw.Draw(image)
        box = (0, 0, renderer.COLUMNS[3][2], renderer.ROW_HEIGHT)
        font = renderer._fit_pil_font(draw, lines, box, 21)
        bounds = draw.multiline_textbbox(
            (0, 0), "\n".join(lines), font=font, spacing=5, align="center"
        )
        self.assertLessEqual(bounds[2] - bounds[0], box[2] - 14)
        self.assertLessEqual(bounds[3] - bounds[1], box[3] - 14)

    def test_numeric_titan_match_id_stays_on_one_line_and_fits(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is not installed")
        history = _history_index()
        payload = _payload()
        payload["rows"] = payload["rows"][:1]
        payload["rows"][0]["id"] = "2991125"
        row = renderer.validate_payload(payload, history).rows[0]
        lines = renderer._cell_lines(row.identifier, renderer.CELL_WRAP_UNITS[0])
        self.assertEqual(lines, ("2991125",))

        image = Image.new("RGB", (renderer.COLUMNS[0][2], renderer.ROW_HEIGHT))
        draw = ImageDraw.Draw(image)
        box = (0, 0, renderer.COLUMNS[0][2], renderer.ROW_HEIGHT)
        font = renderer._fit_pil_font(draw, lines, box, 21)
        bounds = draw.textbbox((0, 0), lines[0], font=font)
        self.assertLessEqual(bounds[2] - bounds[0], box[2] - 14)

    def test_png_output_when_pillow_is_available(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.json"
            history_path = root / "history.json"
            output_path = root / "card.png"
            input_path.write_text(
                json.dumps(_payload(), ensure_ascii=False), encoding="utf-8"
            )
            history_path.write_text(
                json.dumps(_history(), ensure_ascii=False), encoding="utf-8"
            )

            renderer.render_file(input_path, output_path, history_path)
            with Image.open(output_path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(
                    image.size,
                    (
                        renderer.WIDTH,
                        renderer.validate_payload(_payload(), _history_index()).height,
                    ),
                )

    def test_png_footer_text_stays_inside_panel_for_short_and_multirow_cards(
        self,
    ) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")

        for row_count in (1, 3):
            with self.subTest(row_count=row_count):
                payload = _payload()
                payload["rows"] = payload["rows"][:row_count]
                card = renderer.validate_payload(payload, _history_index())
                with Image.open(BytesIO(renderer.render_raster(card, "PNG"))) as image:
                    pixels = image.load()
                    table_bottom = (
                        renderer.TITLE_HEIGHT
                        + renderer.HEADER_HEIGHT
                        + row_count * renderer.ROW_HEIGHT
                    )
                    footer_start = table_bottom + card.publication_height
                    footer_y = footer_start + renderer.FOOTER_CONTENT_OFFSET
                    muted = tuple(
                        int(renderer.COLORS["muted"][offset : offset + 2], 16)
                        for offset in (1, 3, 5)
                    )
                    warning = tuple(
                        int(renderer.COLORS["warning"][offset : offset + 2], 16)
                        for offset in (1, 3, 5)
                    )
                    muted_rows = [
                        y
                        for y in range(footer_y + 80, card.height)
                        if any(
                            pixels[x, y] == muted
                            for x in range(72, renderer.WIDTH - 72)
                        )
                    ]
                    warning_rows = [
                        y
                        for y in range(footer_y + 45, footer_y + 90)
                        if any(
                            pixels[x, y] == warning
                            for x in range(72, renderer.WIDTH - 72)
                        )
                    ]

                self.assertTrue(muted_rows)
                self.assertTrue(warning_rows)
                panel_bottom = card.height - renderer.PANEL_VERTICAL_MARGIN
                self.assertGreaterEqual(panel_bottom - max(muted_rows), 20)
                self.assertGreaterEqual(min(muted_rows) - max(warning_rows), 10)


if __name__ == "__main__":
    unittest.main()

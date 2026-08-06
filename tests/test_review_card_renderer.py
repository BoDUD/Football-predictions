from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from scripts import review_card_renderer as renderer


def _artifact() -> dict:
    return {
        "artifact_type": "test-validated-joint",
        "prediction_hash": "sha256:" + "a" * 64,
    }


def _display_item(
    rank: int,
    htft: str,
    score: str,
    probability: float,
) -> dict:
    home, away = (int(value) for value in score.split("-"))
    total_goals = home + away
    goal_range = (
        "0-1"
        if total_goals <= 1
        else "2-3"
        if total_goals <= 3
        else "4-6"
        if total_goals <= 6
        else "7+"
    )
    return {
        "slot": rank,
        "htft": htft,
        "score": score,
        "home_goals": home,
        "away_goals": away,
        "total_goals": total_goals,
        "goal_range_code": goal_range,
        "goal_range_label": f"{goal_range}球",
        "probability": probability,
        "percentage": probability * 100.0,
        "selection_role": "global_joint_probability_top_two",
        "status": "high_variance_reference",
        "recommendation_eligible": False,
        "counts_toward_primary_record": False,
        "odds_available": False,
        "counts_as_primary": False,
        "requires_bookmaker_odds": False,
    }


def _outlook(count: int = 2) -> dict:
    items = [
        _display_item(1, "DD", "1-1", 0.0562),
        _display_item(2, "DH", "1-0", 0.0431),
    ][:count]
    marginal_top1 = {
        "code": "2-3",
        "label": "2-3球",
        "probability": 0.41,
        "percentage": 41.0,
    }
    return {
        "markets": {
            "goal_ranges": {
                "top1": copy.deepcopy(marginal_top1),
            }
        },
        "goal_range_marginal_audit": {
            "label": "总进球边际第一",
            "top1": copy.deepcopy(marginal_top1),
            "role": "marginal_distribution_audit_only",
            "replaces_joint_scenario": False,
        },
        "joint_scenarios": {
            "items": copy.deepcopy(items),
            "display_items": items,
            "display_count": len(items),
            "display_reason": "global_joint_probability_top_two_with_rank1_goal_range",
            "display_policy": "global_joint_probability_top_two_with_rank1_goal_range_v2",
            "ranking_basis": "global_joint_event_probability_descending",
            "pairing_basis": "rank1_goal_range_plus_same_validated_joint_event_htft_score_top_two",
            "ranking_source": "validated_joint_paths",
            "status": "high_variance_reference",
            "recommendation_eligible": False,
            "counts_as_primary": False,
            "requires_bookmaker_odds": False,
            "top2_cumulative_probability": 0.0993,
            "top2_cumulative_percentage": 9.93,
            "other_scenarios_probability": 0.9007,
            "other_scenarios_percentage": 90.07,
            "uncertainty": {
                "schema_version": renderer.public_market_outlook.JOINT_UNCERTAINTY_SCHEMA_VERSION,
                "policy": renderer.public_market_outlook.JOINT_UNCERTAINTY_POLICY,
                "level": "high",
                "label_zh": "高",
                "normalized_entropy": 0.986,
            },
        },
    }


def _record(*, primary: bool = False) -> dict:
    primary_market = "total" if primary else None
    primary_pick = (
        {
            "market": "total",
            "side": "under",
            "line": 2.5,
            "odds": 0.92,
            "role": "primary",
        }
        if primary
        else None
    )
    primary_result = "win" if primary else None
    basis = {
        "policy": "latest_active_prematch_version",
        "grading_scope": "primary_only",
        "analysis_stage": "lineup-check",
        "version_archived_at": "2026-08-03T15:33:45+00:00",
        "fixture_id": "2913681",
        "match_id": "2913681",
        "home_team": "塞伊奈约基",
        "away_team": "赫尔辛基",
        "kickoff": "2026-08-04T01:00:00+09:00",
        "primary_market": primary_market,
        "primary_pick": copy.deepcopy(primary_pick),
        "formal_picks": {},
        "candidate_audits": [],
        "joint_scenario_audit": {"test_state": "valid"},
        # Bait values must never supply the two reference columns.
        "display_exact_score_picks": [{"score": "9-9"}, {"score": "8-8"}],
        "htft_picks": [{"selection": "AA"}],
        "primary_result": primary_result,
        "counts_toward_primary_record": primary,
    }
    return {
        "match_id": "2913681",
        "mode": "prematch",
        "status": "reviewed",
        "analysis_stage": "lineup-check",
        "league": "芬超",
        "league_key": "芬超",
        "kickoff": "2026-08-04T01:00:00+09:00",
        "home_team": "塞伊奈约基",
        "away_team": "赫尔辛基",
        "half_time_score": "1-0",
        "final_score": "2-1",
        "reviewed_at": "2026-08-03T17:05:00+00:00",
        "primary_market": primary_market,
        "primary_pick": primary_pick,
        "primary_result": primary_result,
        "counts_toward_primary_record": primary,
        "settlement_basis": basis,
        # More bait outside settlement_basis.
        "predicted_score": "7-7",
        "display_exact_score_picks": [{"score": "7-7"}],
        "htft_picks": [{"selection": "AA"}],
    }


class ReviewCardRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        def validated_joint(record: dict):
            basis = record.get("settlement_basis")
            audit = (
                basis.get("joint_scenario_audit") if isinstance(basis, dict) else None
            )
            return (
                _artifact()
                if isinstance(audit, dict) and audit.get("test_state") == "valid"
                else None
            )

        validator = mock.patch.object(
            renderer.memory_store,
            "validated_joint_scenario_audit",
            side_effect=validated_joint,
        )
        self.joint_validator = validator.start()
        self.addCleanup(validator.stop)
        builder = mock.patch.object(
            renderer.public_market_outlook,
            "build_public_market_outlook",
            side_effect=lambda artifact: _outlook(2),
        )
        self.public_builder = builder.start()
        self.addCleanup(builder.stop)

    def test_no_primary_wording_is_exact_and_all_references_share_joint_order(
        self,
    ) -> None:
        card = renderer.build_card(_record())

        self.assertEqual(
            card.primary_settlement.replace("\n", ""),
            "主推：无正式推荐（不结算、不计战绩）",
        )
        self.assertEqual(
            [(event.htft, event.score) for event in card.events],
            [("DD", "1-1"), ("DH", "1-0")],
        )
        self.assertEqual(
            card.htft_reference.splitlines()[1:],
            ["1. 平/平 5.6%", "2. 平/胜 4.3%"],
        )
        self.assertEqual(
            card.score_reference.splitlines(),
            [
                "联合首选情景总球 2-3球",
                "总进球边际第一 2-3球 41.0%",
                "仅审计·不替代联合",
                "Top2累计 9.9%",
                "其他情景 90.1%",
                "不确定度 高(v1)",
                "1. 1-1 5.6%",
                "2. 1-0 4.3%",
            ],
        )
        visible = "\n".join(renderer.visible_text(card))
        self.assertNotIn("9-9", visible)
        self.assertNotIn("8-8", visible)
        self.assertNotIn("7-7", visible)
        self.assertNotIn("AA+", visible)
        self.assertNotIn("…", visible)
        self.assertNotIn("...", visible)
        self.assertIn("联合Top2 9.9%｜其他90.1%｜不确定度高", visible)
        self.assertIn("归一化熵98.6%，v1", visible)
        self.assertIn("总进球边际第一 2-3球 41.0%", visible)
        self.assertIn("仅审计·不替代联合", visible)
        self.public_builder.assert_called_once_with(_artifact())

    def test_formal_primary_and_result_are_read_only_from_settlement_basis(
        self,
    ) -> None:
        card = renderer.build_card(_record(primary=True))
        self.assertEqual(card.primary_settlement, "主推：小2.5 @0.92\n结算：红")
        self.assertEqual(card.primary_result, "win")

    def test_review_uses_settlement_frozen_league_identity(self) -> None:
        record = _record()
        record["settlement_basis"]["league"] = "芬超"
        record["settlement_basis"]["league_key"] = "finland_veikkausliiga"
        record["settlement_basis"]["competition_evidence"] = None
        record["league"] = "英超"
        record["league_key"] = "england_premier_league"

        card = renderer.build_card(record)

        self.assertEqual(card.league, "芬超")

    def test_tampered_primary_binding_is_rejected(self) -> None:
        record = _record()
        record["primary_market"] = "total"
        record["primary_pick"] = {"side": "over", "line": 3.5}
        record["counts_toward_primary_record"] = True
        with self.assertRaisesRegex(renderer.ReviewCardError, "primary conflicts"):
            renderer.build_card(record)

        record = _record(primary=True)
        record["settlement_basis"]["primary_result"] = "loss"
        with self.assertRaisesRegex(renderer.ReviewCardError, "result conflicts"):
            renderer.build_card(record)

    def test_invalid_or_missing_joint_artifact_fails_closed(self) -> None:
        record = _record()
        record["settlement_basis"]["joint_scenario_audit"] = None
        card = renderer.build_card(record)
        self.assertEqual(card.events, ())
        self.assertIsNone(card.joint_summary)
        self.assertEqual(card.htft_reference, "数据不足")
        self.assertEqual(card.score_reference, "数据不足")
        visible = "\n".join(renderer.visible_text(card))
        self.assertNotIn("9-9", visible)
        self.assertIn("冻结结算依据未包含可验证联合路径", visible)
        self.assertNotIn("联合Top2 9.9%", visible)

    def test_invalid_public_display_fails_closed_without_independent_fallback(
        self,
    ) -> None:
        bad = _outlook(2)
        bad["joint_scenarios"]["display_items"][1]["counts_as_primary"] = True
        self.public_builder.side_effect = lambda artifact: bad
        card = renderer.build_card(_record())
        self.assertEqual(card.events, ())
        self.assertEqual(card.joint_status, "data_insufficient")

    def test_score_direction_conflicting_with_htft_fails_closed(self) -> None:
        bad = _outlook(2)
        bad["joint_scenarios"]["display_items"][1]["score"] = "0-1"
        bad["joint_scenarios"]["display_items"][1]["home_goals"] = 0
        bad["joint_scenarios"]["display_items"][1]["away_goals"] = 1
        self.public_builder.side_effect = lambda artifact: bad

        card = renderer.build_card(_record())

        self.assertEqual(card.events, ())
        self.assertEqual(card.joint_status, "data_insufficient")

    def test_goal_range_conflicting_with_score_fails_closed(self) -> None:
        bad = _outlook(2)
        bad["joint_scenarios"]["display_items"][0]["goal_range_code"] = "0-1"
        bad["joint_scenarios"]["display_items"][0]["goal_range_label"] = "0-1球"
        self.public_builder.side_effect = lambda artifact: bad

        card = renderer.build_card(_record())

        self.assertEqual(card.events, ())
        self.assertEqual(card.joint_status, "data_insufficient")

    def test_tampered_concentration_marginal_or_uncertainty_fails_closed(self) -> None:
        mutations = (
            (
                "top2",
                lambda value: value["joint_scenarios"].update(
                    {
                        "top2_cumulative_probability": 0.99,
                        "other_scenarios_probability": 0.01,
                    }
                ),
            ),
            (
                "marginal",
                lambda value: value["goal_range_marginal_audit"].update(
                    {"replaces_joint_scenario": True}
                ),
            ),
            (
                "uncertainty",
                lambda value: value["joint_scenarios"]["uncertainty"].update(
                    {"level": "low", "label_zh": "低"}
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                bad = _outlook(2)
                mutate(bad)
                self.public_builder.side_effect = lambda artifact, value=bad: value
                card = renderer.build_card(_record())
                self.assertEqual(card.events, ())
                self.assertIsNone(card.joint_summary)
                self.assertEqual(card.joint_status, "data_insufficient")

    def test_half_time_score_cannot_exceed_full_time_score(self) -> None:
        record = _record()
        record["half_time_score"] = "3-0"
        with self.assertRaisesRegex(renderer.ReviewCardError, "cannot exceed"):
            renderer.build_card(record)

    def test_missing_half_time_score_is_rendered_as_unavailable(self) -> None:
        record = _record()
        record["half_time_score"] = None

        card = renderer.build_card(record)

        self.assertEqual(card.half_time_score, "未取得")
        self.assertEqual(card.final_score, "2-1")
        self.assertIn("未取得", renderer.render_svg(card))

    def test_svg_has_eight_columns_exactly_two_events_and_no_ellipsis(self) -> None:
        card = renderer.build_card(_record())
        svg = renderer.render_svg(card)
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        for heading in (
            "编号",
            "赛事",
            "比赛",
            "半场",
            "全场",
            "主推结算",
            "半全场参考",
            "联合首选情景总球/波胆",
        ):
            self.assertIn(heading, svg)
        for value in ("平/平", "平/胜", "1-1", "1-0"):
            self.assertIn(value, svg)
        self.assertNotIn("平/负", svg)
        rendered_text = "".join(root.itertext())
        self.assertIn("联合首选情景总球 2-3球", rendered_text)
        self.assertIn("总进球边际第一 2-3球 41.0%", rendered_text)
        self.assertIn("Top2累计 9.9%", svg)
        self.assertIn("其他情景 90.1%", svg)
        self.assertIn("不确定度 高(v1)", svg)
        self.assertNotIn("0-1球·1-0", svg)
        self.assertIn("无正式推荐（不结算、不计战绩）", "".join(root.itertext()))
        self.assertNotIn("…", svg)
        self.assertNotIn("...", svg)

    def test_incomplete_one_event_public_output_fails_closed(self) -> None:
        self.public_builder.side_effect = lambda artifact: _outlook(1)
        card = renderer.build_card(_record())
        self.assertEqual(card.events, ())
        self.assertEqual(card.htft_reference, "数据不足")
        self.assertEqual(card.score_reference, "数据不足")

    def test_two_distinct_scores_with_same_htft_are_both_preserved(self) -> None:
        outlook = _outlook(2)
        outlook["joint_scenarios"]["items"] = [
            _display_item(1, "DD", "0-0", 0.065),
            _display_item(2, "DD", "1-1", 0.061),
        ]
        outlook["joint_scenarios"]["display_items"] = copy.deepcopy(
            outlook["joint_scenarios"]["items"]
        )
        outlook["joint_scenarios"]["top2_cumulative_probability"] = 0.126
        outlook["joint_scenarios"]["top2_cumulative_percentage"] = 12.6
        outlook["joint_scenarios"]["other_scenarios_probability"] = 0.874
        outlook["joint_scenarios"]["other_scenarios_percentage"] = 87.4
        self.public_builder.side_effect = lambda artifact: outlook

        card = renderer.build_card(_record())

        self.assertEqual(
            [(event.htft, event.score) for event in card.events],
            [("DD", "0-0"), ("DD", "1-1")],
        )

    def test_png_and_svg_files_are_generated(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.json"
            history.write_text(
                json.dumps([_record()], ensure_ascii=False), encoding="utf-8"
            )
            svg_path = root / "review.svg"
            png_path = root / "review.png"

            renderer.render_file(history, "2913681", svg_path)
            renderer.render_file(history, "2913681", png_path)

            self.assertTrue(svg_path.is_file())
            with Image.open(png_path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, renderer.WIDTH)
                self.assertEqual(
                    image.height, renderer._card_height(renderer.build_card(_record()))
                )

    def test_wide_latin_team_name_wraps_inside_review_match_cell(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is not installed")
        record = _record()
        record["home_team"] = "W" * 20
        record["settlement_basis"]["home_team"] = "W" * 20
        card = renderer.build_card(record)
        cells = renderer._wrapped_cells(card)
        match_lines = cells[2]
        self.assertGreaterEqual(len(match_lines), 3)
        self.assertEqual(cells[0], ("2913681",))

        image = Image.new("RGB", (renderer.COLUMNS[2][2], renderer._row_height(card)))
        draw = ImageDraw.Draw(image)
        font = renderer._font(renderer.CELL_FONT_SIZES[2])
        for line in match_lines:
            bounds = draw.textbbox((0, 0), line or " ", font=font)
            self.assertLessEqual(bounds[2] - bounds[0], renderer.COLUMNS[2][2] - 18)

    def test_new_joint_summary_and_full_header_fit_without_clipping(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is not installed")
        card = renderer.build_card(_record())
        cells = renderer._wrapped_cells(card)
        image = Image.new("RGB", (renderer.COLUMNS[7][2], renderer._row_height(card)))
        draw = ImageDraw.Draw(image)
        cell_font = renderer._font(renderer.CELL_FONT_SIZES[7], bold=True)
        for line in cells[7]:
            bounds = draw.textbbox((0, 0), line or " ", font=cell_font)
            self.assertLessEqual(bounds[2] - bounds[0], renderer.COLUMNS[7][2] - 18)
        header_font = renderer._font(24, bold=True)
        header_bounds = draw.textbbox((0, 0), renderer.COLUMNS[7][0], font=header_font)
        self.assertLessEqual(
            header_bounds[2] - header_bounds[0], renderer.COLUMNS[7][2] - 8
        )
        footer_font = renderer._font(23, bold=True)
        footer_bounds = draw.textbbox(
            (0, 0), renderer.joint_reference_note(card), font=footer_font
        )
        self.assertLessEqual(
            footer_bounds[2] - footer_bounds[0], renderer.TABLE_WIDTH - 60
        )
        self.assertNotIn("…", "\n".join(cells[7]))
        self.assertNotIn("...", "\n".join(cells[7]))

    def test_cli_does_not_accept_user_supplied_prediction_content(self) -> None:
        parser = renderer.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "--history",
                    "history.json",
                    "--match-id",
                    "2913681",
                    "--output",
                    "review.svg",
                    "--primary",
                    "伪造主推",
                ]
            )


if __name__ == "__main__":
    unittest.main()

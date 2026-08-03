from __future__ import annotations

import copy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from xml.etree import ElementTree as ET

from scripts import review_card_renderer as renderer


def _artifact() -> dict:
    return {"artifact_type": "test-validated-joint", "prediction_hash": "sha256:" + "a" * 64}


def _display_item(rank: int, htft: str, score: str, probability: float) -> dict:
    home, away = (int(value) for value in score.split("-"))
    return {
        "slot": rank,
        "htft": htft,
        "score": score,
        "home_goals": home,
        "away_goals": away,
        "probability": probability,
        "percentage": probability * 100.0,
        "status": "high_variance_reference",
        "recommendation_eligible": False,
        "counts_toward_primary_record": False,
        "odds_available": False,
        "counts_as_primary": False,
        "requires_bookmaker_odds": False,
    }


def _outlook(count: int = 3) -> dict:
    items = [
        _display_item(1, "DD", "1-1", 0.0562),
        _display_item(2, "HH", "2-1", 0.0431),
        _display_item(3, "DA", "1-2", 0.0366),
    ][:count]
    return {
        "joint_scenarios": {
            "items": copy.deepcopy(items),
            "display_items": items,
            "display_count": len(items),
            "display_reason": "complex_top_three" if count == 3 else "default_top_two",
            "ranking_source": "validated_joint_paths",
            "status": "high_variance_reference",
            "recommendation_eligible": False,
            "counts_as_primary": False,
            "requires_bookmaker_odds": False,
        }
    }


def _record(*, primary: bool = False) -> dict:
    primary_market = "total" if primary else None
    primary_pick = (
        {"market": "total", "side": "under", "line": 2.5, "odds": 0.92, "role": "primary"}
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
            audit = basis.get("joint_scenario_audit") if isinstance(basis, dict) else None
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
            side_effect=lambda artifact: _outlook(3),
        )
        self.public_builder = builder.start()
        self.addCleanup(builder.stop)

    def test_no_primary_wording_is_exact_and_all_references_share_joint_order(self) -> None:
        card = renderer.build_card(_record())

        self.assertEqual(
            card.primary_settlement.replace("\n", ""),
            "主推：无正式推荐（不结算、不计战绩）",
        )
        self.assertEqual(
            [(event.htft, event.score) for event in card.events],
            [("DD", "1-1"), ("HH", "2-1"), ("DA", "1-2")],
        )
        self.assertEqual(
            card.htft_reference.splitlines()[1:],
            ["1. 平/平 5.6%", "2. 胜/胜 4.3%", "3. 平/负 3.7%"],
        )
        self.assertEqual(
            card.score_reference.splitlines()[1:],
            ["1. 1-1 5.6%", "2. 2-1 4.3%", "3. 1-2 3.7%"],
        )
        visible = "\n".join(renderer.visible_text(card))
        self.assertNotIn("9-9", visible)
        self.assertNotIn("8-8", visible)
        self.assertNotIn("7-7", visible)
        self.assertNotIn("AA+", visible)
        self.assertNotIn("…", visible)
        self.assertNotIn("...", visible)
        self.public_builder.assert_called_once_with(_artifact())

    def test_formal_primary_and_result_are_read_only_from_settlement_basis(self) -> None:
        card = renderer.build_card(_record(primary=True))
        self.assertEqual(card.primary_settlement, "主推：小2.5 @0.92\n结算：红")
        self.assertEqual(card.primary_result, "win")

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
        self.assertEqual(card.htft_reference, "数据不足")
        self.assertEqual(card.score_reference, "数据不足")
        self.assertNotIn("9-9", "\n".join(renderer.visible_text(card)))

    def test_invalid_public_display_fails_closed_without_independent_fallback(self) -> None:
        bad = _outlook(3)
        bad["joint_scenarios"]["display_items"][1]["counts_as_primary"] = True
        self.public_builder.side_effect = lambda artifact: bad
        card = renderer.build_card(_record())
        self.assertEqual(card.events, ())
        self.assertEqual(card.joint_status, "data_insufficient")

    def test_half_time_score_cannot_exceed_full_time_score(self) -> None:
        record = _record()
        record["half_time_score"] = "3-0"
        with self.assertRaisesRegex(renderer.ReviewCardError, "cannot exceed"):
            renderer.build_card(record)

    def test_svg_has_eight_columns_dynamic_three_events_and_no_ellipsis(self) -> None:
        card = renderer.build_card(_record())
        svg = renderer.render_svg(card)
        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        for heading in ("编号", "赛事", "比赛", "半场", "全场", "主推结算", "半全场参考", "波胆参考"):
            self.assertIn(heading, svg)
        for value in ("平/平", "胜/胜", "平/负", "1-1", "2-1", "1-2"):
            self.assertIn(value, svg)
        self.assertIn("无正式推荐（不结算、不计战绩）", "".join(root.itertext()))
        self.assertNotIn("…", svg)
        self.assertNotIn("...", svg)

    def test_dynamic_two_events_are_rendered_without_a_fixed_top_three(self) -> None:
        self.public_builder.side_effect = lambda artifact: _outlook(2)
        card = renderer.build_card(_record())
        self.assertEqual(len(card.events), 2)
        self.assertNotIn("平/负", card.htft_reference)
        self.assertNotIn("1-2", card.score_reference)

    def test_png_and_svg_files_are_generated(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.json"
            history.write_text(json.dumps([_record()], ensure_ascii=False), encoding="utf-8")
            svg_path = root / "review.svg"
            png_path = root / "review.png"

            renderer.render_file(history, "2913681", svg_path)
            renderer.render_file(history, "2913681", png_path)

            self.assertTrue(svg_path.is_file())
            with Image.open(png_path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, renderer.WIDTH)
                self.assertEqual(image.height, renderer._card_height(renderer.build_card(_record())))

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

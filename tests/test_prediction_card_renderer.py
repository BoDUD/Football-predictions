from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prediction_card_renderer.py"
SPEC = importlib.util.spec_from_file_location("prediction_card_renderer", SCRIPT)
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = renderer
SPEC.loader.exec_module(renderer)


def _payload() -> dict:
    return {
        "date": "2026-08-03",
        "title": "今日足球扫盘",
        "subtitle": "赛前综合分析版",
        "rows": [
            {
                "id": "周一001",
                "archive_match_id": "9001",
                "time": "18:30",
                "league": "韩K联",
                "home_team": "主队A",
                "away_team": "客队B",
                "total_goals": "2球/3球",
                "htft": ["胜胜", "平胜"],
                "scores": ["2:0", "2:1"],
                "status": "formal_primary",
            },
            {
                "id": "周一002",
                "archive_match_id": "9002",
                "time": "20:00",
                "league": "瑞典超",
                "home_team": "观察队",
                "away_team": "样本队",
                "primary": "平/负",
                "total_goals": "1球/2球",
                "htft": ["平平", "平负"],
                "scores": ["1:1", "1:2"],
                "status": "observation",
            },
            {
                "id": "周一003",
                "archive_match_id": "9003",
                "time": "22:00",
                "league": "英超",
                "home_team": "待定队",
                "away_team": "不追队",
                "primary": "输入内容不会冒充主推",
                "total_goals": "2球/3球",
                "htft": ["平平", "胜平"],
                "scores": ["1:1", "2:1"],
                "status": "no_bet",
            },
        ],
    }


def _valid_htft_observation_audit() -> dict:
    matrix = {
        "HH": 0.10,
        "HD": 0.05,
        "HA": 0.05,
        "DH": 0.10,
        "DD": 0.20,
        "DA": 0.30,
        "AH": 0.05,
        "AD": 0.05,
        "AA": 0.10,
    }
    matrix_bytes = json.dumps(
        matrix,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return {
        "schema_version": renderer.memory_store.OBSERVATION_SCHEMA_VERSION,
        "market": "htft",
        "status": "observation_only",
        "counts_toward_primary_record": False,
        "monetary_scope": "none",
        "model": {
            "model_hash": "sha256:" + "1" * 64,
            "prediction_hash": "sha256:" + "2" * 64,
            "artifact_sha256": "sha256:" + "3" * 64,
            "matrix_hash": "sha256:" + hashlib.sha256(matrix_bytes).hexdigest(),
        },
        "matrix": matrix,
        "top_two": [
            {"selection": "DA", "probability": 0.30, "gates": []},
            {"selection": "DD", "probability": 0.20, "gates": []},
        ],
        "provenance": {
            "strict_forward_oos": True,
            "fixture_validated": True,
            "generated_before_kickoff": True,
            "training_cutoff_before_kickoff": True,
        },
    }


def _history() -> list[dict]:
    return [
        {
            "match_id": "9001",
            "mode": "prematch",
            "status": "pending",
            "home_team": "主队A",
            "away_team": "客队B",
            "primary_market": "total",
            "primary_pick": {"side": "under", "line": 2.5, "odds": 0.92},
        },
        {
            "match_id": "9002",
            "mode": "prematch",
            "status": "pending",
            "home_team": "观察队",
            "away_team": "样本队",
            "primary_market": None,
            "primary_pick": None,
            "candidate_audits": [_valid_htft_observation_audit()],
        },
        {
            "match_id": "9003",
            "mode": "prematch",
            "status": "pending",
            "home_team": "待定队",
            "away_team": "不追队",
            "primary_market": None,
            "primary_pick": None,
        },
    ]


def _history_index() -> dict[str, dict]:
    return {record["match_id"]: record for record in _history()}


class PredictionCardRendererTests(unittest.TestCase):
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

    def test_formal_status_requires_matching_active_archive_primary(self) -> None:
        payload = _payload()
        payload["rows"][0]["archive_match_id"] = "9002"
        payload["rows"][0]["home_team"] = "观察队"
        payload["rows"][0]["away_team"] = "样本队"
        with self.assertRaisesRegex(ValueError, "without an archived primary_pick"):
            renderer.validate_payload(payload, _history_index())

        payload = _payload()
        payload["rows"][0]["status"] = "observation"
        payload["rows"][0]["primary"] = "观察方向"
        with self.assertRaisesRegex(ValueError, "conflicts with an archived active formal primary"):
            renderer.validate_payload(payload, _history_index())

    def test_observation_requires_a_validated_candidate_audit(self) -> None:
        payload = _payload()
        history = _history_index()
        history["9002"].pop("candidate_audits")
        with self.assertRaisesRegex(ValueError, "without validated candidate_audits"):
            renderer.validate_payload(payload, history)

        history = _history_index()
        history["9002"]["candidate_audits"] = [
            {
                "market": "htft",
                "status": "observation_only",
                "top_two": [{"selection": "DA"}],
            }
        ]
        with self.assertRaisesRegex(ValueError, "without validated candidate_audits"):
            renderer.validate_payload(payload, history)

    def test_observation_text_must_match_the_archived_best_candidate(self) -> None:
        payload = _payload()
        payload["rows"][1]["primary"] = "主/客"
        with self.assertRaisesRegex(ValueError, "archived best observation"):
            renderer.validate_payload(payload, _history_index())

        card = renderer.validate_payload(_payload(), _history_index())
        self.assertEqual(card.rows[1].primary, "◇ 平/负")
        self.assertFalse(card.rows[1].star)

    def test_history_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "prediction history is required"):
            renderer.validate_payload(_payload())

    def test_htft_and_scores_require_exactly_two_values(self) -> None:
        for field in ("htft", "scores"):
            for values in (["only-one"], ["one", "two", "three"], "one/two"):
                with self.subTest(field=field, values=values):
                    payload = _payload()
                    payload["rows"][0][field] = values
                    with self.assertRaisesRegex(ValueError, "exactly two"):
                        renderer.validate_payload(payload, _history_index())

    def test_status_allowlist_is_strict(self) -> None:
        payload = _payload()
        payload["rows"][0]["status"] = "best_bet"
        with self.assertRaisesRegex(ValueError, "status must be one of"):
            renderer.validate_payload(payload, _history_index())

    def test_svg_escapes_xml_and_exposes_guarded_markers(self) -> None:
        payload = _payload()
        payload["title"] = "今日 A&B <精选>"
        payload["rows"][0]["home_team"] = "红&蓝<队>"
        history = _history_index()
        history["9001"]["home_team"] = "红&蓝<队>"
        card = renderer.validate_payload(payload, history)
        svg = renderer.render_svg(card)

        root = ET.fromstring(svg)
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn("A&amp;B &lt;精选&gt;", svg)
        self.assertIn("红&amp;蓝&lt;队&gt;", svg)
        self.assertIn("小2.5 @0.92<tspan", svg)
        self.assertIn("★", svg)
        self.assertIn("◇ 平/负", svg)
        self.assertIn("无正式推荐", svg)
        self.assertNotIn("输入内容不会冒充主推", svg)

    def test_svg_file_is_written_and_height_tracks_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.json"
            history_path = root / "history.json"
            output_path = root / "card.svg"
            input_path.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
            history_path.write_text(json.dumps(_history(), ensure_ascii=False), encoding="utf-8")

            returned = renderer.render_file(input_path, output_path, history_path)
            self.assertEqual(returned, output_path)
            self.assertTrue(output_path.is_file())
            xml = ET.parse(output_path).getroot()
            expected = (
                renderer.TITLE_HEIGHT
                + renderer.HEADER_HEIGHT
                + 3 * renderer.ROW_HEIGHT
                + renderer.FOOTER_HEIGHT
            )
            self.assertEqual(int(xml.attrib["height"]), expected)

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
            input_path.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
            history_path.write_text(json.dumps(_history(), ensure_ascii=False), encoding="utf-8")

            renderer.render_file(input_path, output_path, history_path)
            with Image.open(output_path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(
                    image.size,
                    (renderer.WIDTH, renderer.validate_payload(_payload(), _history_index()).height),
                )


if __name__ == "__main__":
    unittest.main()

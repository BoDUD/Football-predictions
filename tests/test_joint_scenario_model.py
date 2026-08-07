from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts import htft_model, joint_path_kernel, joint_scenario_model, score_model

SAMPLE_ROWS = [
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
MANIFEST_HASH = "sha256:" + "a" * 64
INPUT_GENERATED_AT = "2098-12-31T00:00:00Z"
JOINT_GENERATED_AT = "2098-12-31T00:01:00Z"
KICKOFF = "2099-01-01T12:00:00Z"


class JointScenarioModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        cls.csv_path = cls.base / "matches.csv"
        with cls.csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
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
            writer.writerows(SAMPLE_ROWS)
        cls.model = htft_model.fit_model(
            cls.csv_path,
            iterations=30,
            learning_rate=0.025,
            regularization=0.03,
            half_time_half_life_days=180.0,
            second_half_half_life_days=180.0,
            full_time_half_life_days=180.0,
            competition_key="test_league",
            dataset_manifest_hash=MANIFEST_HASH,
        )
        cls.htft_prediction = htft_model.predict_model(
            cls.model,
            "Alpha",
            "Bravo",
            kickoff=KICKOFF,
            generated_at=INPUT_GENERATED_AT,
            max_goals=12,
            hard_max_goals=30,
        )
        cls.score_prediction = score_model.predict_model(
            cls.model["components"]["full_time"],
            "Alpha",
            "Bravo",
            kickoff=KICKOFF,
            generated_at=INPUT_GENERATED_AT,
            max_goals=12,
            hard_max_goals=30,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def _predict(
        self,
        market_evidence=None,
        *,
        expected_match_id=None,
        htft_prediction=None,
    ):
        return joint_scenario_model.predict_joint_scenarios(
            self.model,
            self.score_prediction,
            htft_prediction or self.htft_prediction,
            generated_at=JOINT_GENERATED_AT,
            market_evidence=market_evidence,
            expected_match_id=expected_match_id,
        )

    def _anchored_htft_prediction(self):
        return htft_model.predict_model(
            self.model,
            "Alpha",
            "Bravo",
            kickoff=KICKOFF,
            generated_at=INPUT_GENERATED_AT,
            max_goals=12,
            hard_max_goals=30,
            half_time_anchor={
                "probabilities": {"home": 0.20, "draw": 0.50, "away": 0.30},
                "source": "https://example.test/half-time-consensus",
                "captured_at": "2098-12-30T23:59:00Z",
                "de_vigged": True,
            },
        )

    @staticmethod
    def _current_market_bundle():
        return {
            "artifact_type": "soccer_prematch_market_evidence",
            "schema_version": "1.0.0",
            "match_id": "2913681",
            "captured_at": "2098-12-31T00:00:30Z",
            "fixture": {
                "competition_key": "test_league",
                "home_team": "Alpha",
                "away_team": "Bravo",
                # Same instant as KICKOFF, expressed in Tokyo time.
                "kickoff": "2099-01-01T21:00:00+09:00",
                "fixture_id": "2913681",
            },
            "sources": {
                "analysis": "https://zq.titan007.com/analysis/2913681cn.htm",
                "one_x_two": "https://1x2.titan007.com/oddslist/2913681.htm",
                "asian_handicap": "https://vip.titan007.com/asian?id=2913681",
                "totals": "https://vip.titan007.com/totals?id=2913681",
            },
            "one_x_two": {
                "odds_format": "decimal",
                "stored_complete_firm_count": 1,
                "rows": [{"bookmaker": "Firm", "home": 2.3, "draw": 3.2, "away": 2.9}],
            },
            "asian_handicap": {
                "odds_format": "hong_kong",
                "stored_complete_firm_count": 1,
                "rows": [
                    {
                        "bookmaker": "Firm",
                        "opening": {
                            "home_price": 0.9,
                            "home_line": -0.25,
                            "away_price": 0.95,
                        },
                        "current": {
                            "home_price": 0.92,
                            "home_line": -0.25,
                            "away_price": 0.93,
                        },
                    }
                ],
            },
            "totals": {
                "odds_format": "hong_kong",
                "stored_complete_firm_count": 1,
                "rows": [
                    {
                        "bookmaker": "Firm",
                        "opening": {
                            "over_price": 0.9,
                            "line": 2.5,
                            "under_price": 0.95,
                        },
                        "current": {
                            "over_price": 0.92,
                            "line": 2.5,
                            "under_price": 0.93,
                        },
                    }
                ],
            },
            "quality": {"status": "complete"},
        }

    def test_model_only_artifact_is_normalized_and_matches_both_inputs(self):
        prediction = self._predict()
        joint_scenario_model.validate_prediction(prediction)
        joint_scenario_model.validate_prediction_inputs(
            prediction,
            self.model,
            self.score_prediction,
            self.htft_prediction,
        )

        self.assertEqual(prediction["probability_mode"], "model_only")
        self.assertFalse(prediction["market_conditioning_enabled"])
        self.assertFalse(prediction["research_market_informed"])
        self.assertFalse(prediction["formal_eligible"])
        self.assertFalse(prediction["market_evidence"]["provided"])
        self.assertFalse(prediction["external_anchor_audit"]["enabled"])
        self.assertEqual(
            prediction["derived_field_audits"],
            joint_scenario_model._derived_field_audits("model_only"),
        )
        self.assertNotIn("joint_cells", prediction)
        rebuilt = joint_path_kernel.validate_kernel(prediction["path_kernel"])
        self.assertAlmostEqual(rebuilt["total_probability"], 1.0, places=12)
        self.assertTrue(prediction["support_feasibility_audit"]["feasible"])
        self.assertTrue(prediction["support_feasibility_audit"]["performed_before_ipf"])
        for actual_row, expected_row in zip(
            prediction["full_time_score_marginal"]["probabilities"],
            self.score_prediction["score_matrix"]["probabilities"],
            strict=True,
        ):
            for actual, expected in zip(actual_row, expected_row, strict=True):
                self.assertAlmostEqual(actual, expected, places=10)
        for code, probability in self.htft_prediction["htft"][
            "code_probabilities"
        ].items():
            self.assertAlmostEqual(
                prediction["htft_marginal"]["code_probabilities"][code],
                probability,
                places=10,
            )

    def test_impossible_paths_stay_zero_and_top_two_is_global_probability_rank(self):
        prediction = self._predict()
        rebuilt = joint_path_kernel.validate_kernel(prediction["path_kernel"])
        planes = rebuilt["event_planes"]
        self.assertGreater(planes[1][0][0], 0.0)
        self.assertEqual(planes[0][0][0], 0.0)
        self.assertEqual(planes[2][0][0], 0.0)
        self.assertEqual(
            prediction["joint_top_two"],
            joint_scenario_model._top_two_from_event_planes(planes),
        )
        for item in prediction["joint_top_two"]:
            self.assertEqual(item["status"], "high_variance_reference")
            self.assertFalse(item["recommendation_eligible"])
            self.assertFalse(item["counts_toward_primary_record"])
            self.assertFalse(item["odds_available"])

    def test_artifact_only_validator_rejects_tampering_even_after_rehash(self):
        prediction = self._predict()
        tampered = copy.deepcopy(prediction)
        tampered["joint_top_two"] = list(reversed(tampered["joint_top_two"]))
        tampered["prediction_hash"] = joint_scenario_model.calculate_prediction_hash(
            tampered
        )
        with self.assertRaisesRegex(
            joint_scenario_model.JointScenarioError, "joint_top_two"
        ):
            joint_scenario_model.validate_prediction(tampered)

    def test_kernel_feasibility_and_path_marginal_tampering_fail_closed(self):
        prediction = self._predict()
        mutations = (
            (
                "kernel scale",
                lambda item: item["path_kernel"]["group_scales"]["entries"][
                    0
                ].__setitem__(
                    3, item["path_kernel"]["group_scales"]["entries"][0][3] * 1.01
                ),
            ),
            (
                "kernel Hall audit",
                lambda item: item["path_kernel"]["hall_audit"].__setitem__(
                    "minimum_subset_slack_probability",
                    item["path_kernel"]["hall_audit"][
                        "minimum_subset_slack_probability"
                    ]
                    + 0.001,
                ),
            ),
            (
                "pre-IPF feasibility binding",
                lambda item: item["support_feasibility_audit"].__setitem__(
                    "performed_before_ipf", False
                ),
            ),
            (
                "half-time path marginal",
                lambda item: item["half_time_score_marginal"]["probabilities"][
                    0
                ].__setitem__(
                    0, item["half_time_score_marginal"]["probabilities"][0][0] + 0.01
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                tampered = copy.deepcopy(prediction)
                mutate(tampered)
                tampered["prediction_hash"] = (
                    joint_scenario_model.calculate_prediction_hash(tampered)
                )
                with self.assertRaises(joint_scenario_model.JointScenarioError):
                    joint_scenario_model.validate_prediction(tampered)

    def test_frozen_legacy_1_1_cell_artifact_remains_readable(self):
        current = self._predict()
        rebuilt = joint_path_kernel.validate_kernel(current["path_kernel"])
        components = current["path_kernel"]["components"]
        seed, seed_tail = joint_scenario_model.build_feasible_joint_seed(
            components["half_time"]["conditional_score_matrix"],
            components["second_half"]["conditional_score_matrix"],
            full_home_goals_max=current["full_time_score_marginal"]["home_goals_max"],
            full_away_goals_max=current["full_time_score_marginal"]["away_goals_max"],
        )
        legacy = copy.deepcopy(current)
        legacy["schema_version"] = joint_scenario_model.LEGACY_SCHEMA_VERSION
        legacy["model_version"] = joint_scenario_model.LEGACY_MODEL_VERSION
        legacy["joint_cells"] = joint_scenario_model._joint_cells(
            seed,
            rebuilt["event_planes"],
            seed_mass=seed_tail["retained_probability"],
        )
        legacy_score, legacy_htft = joint_scenario_model._marginals_from_cells(
            legacy["joint_cells"],
            home_goals_max=current["full_time_score_marginal"]["home_goals_max"],
            away_goals_max=current["full_time_score_marginal"]["away_goals_max"],
        )
        legacy["full_time_score_marginal"]["probabilities"] = legacy_score
        legacy["htft_marginal"]["code_probabilities"] = legacy_htft
        legacy["htft_marginal"]["half_time_result_probabilities"] = (
            joint_scenario_model._htft_row_marginal(legacy_htft)
        )
        legacy["htft_marginal"]["full_time_result_probabilities"] = (
            joint_scenario_model._htft_column_marginal(legacy_htft)
        )
        legacy["joint_top_two"] = joint_scenario_model._top_two(legacy["joint_cells"])
        legacy["joint_top_two_probability_mass"] = sum(
            item["probability"] for item in legacy["joint_top_two"]
        )
        legacy["derived"] = joint_scenario_model._derived_full_time_fields(legacy_score)
        for field in (
            "path_kernel",
            "support_feasibility_audit",
            "half_time_score_marginal",
            "second_half_score_marginal",
        ):
            legacy.pop(field)
        legacy["prediction_hash"] = joint_scenario_model.calculate_prediction_hash(
            legacy
        )

        joint_scenario_model.validate_prediction(legacy)

    def test_market_evidence_is_diagnostic_only_and_enforces_timing(self):
        evidence = {
            "source": "historical-research-only",
            "captured_at": JOINT_GENERATED_AT,
            "quality": "low",
            "opening_1x2": [2.3, 3.1, 2.9],
        }
        prediction = self._predict(evidence)
        self.assertTrue(prediction["market_evidence"]["provided"])
        self.assertFalse(prediction["market_evidence"]["used_for_probability"])
        self.assertEqual(prediction["market_evidence"]["conditioning_weight"], 0.0)
        self.assertFalse(prediction["market_evidence"]["ev_comparison_eligible"])
        self.assertFalse(prediction["market_evidence"]["recommendation_eligible"])
        self.assertFalse(prediction["market_conditioning_enabled"])
        joint_scenario_model.validate_prediction_inputs(
            prediction,
            self.model,
            self.score_prediction,
            self.htft_prediction,
            market_evidence=evidence,
        )

        late = dict(evidence, captured_at="2098-12-31T00:01:01Z")
        with self.assertRaisesRegex(
            joint_scenario_model.JointScenarioError, "after generated_at"
        ):
            self._predict(late)

    def test_structured_market_evidence_bundle_is_accepted_but_never_conditions(self):
        bundle = {
            "sources": [
                {
                    "name": "titan007",
                    "markets": ["opening_1x2", "asian_handicap"],
                },
                {"name": "weather", "status": "available"},
            ],
            "quality": {
                "status": "complete",
                "timestamped_pre_kickoff": True,
                "coverage": 1.0,
            },
            "captured_at": "2098-12-31T00:00:30Z",
            "fixture_id": "2913681",
        }
        baseline = self._predict()
        prediction = self._predict(bundle)
        audit = prediction["market_evidence"]
        self.assertEqual(audit["input_format"], "structured_bundle")
        self.assertEqual(audit["source"], "structured_bundle")
        self.assertEqual(audit["sources"], bundle["sources"])
        self.assertEqual(audit["quality"], "complete")
        self.assertEqual(audit["effective_quality"], "audit_unverified")
        self.assertEqual(audit["bundle_validation_status"], "audit_unverified")
        self.assertTrue(audit["bundle_validation_errors"])
        self.assertEqual(audit["quality_detail"], bundle["quality"])
        self.assertEqual(audit["payload"], bundle)
        self.assertEqual(audit["limitations"], ["market_evidence_fixture_missing"])
        self.assertEqual(
            audit["fixture_binding"]["status"], "unverified_missing_fixture"
        )
        self.assertEqual(prediction["fixture"]["match_id"], "2913681")
        self.assertEqual(
            joint_path_kernel.validate_kernel(prediction["path_kernel"])[
                "event_planes"
            ],
            joint_path_kernel.validate_kernel(baseline["path_kernel"])["event_planes"],
        )
        joint_scenario_model.validate_prediction_inputs(
            prediction,
            self.model,
            self.score_prediction,
            self.htft_prediction,
            market_evidence=bundle,
        )

        tampered = copy.deepcopy(prediction)
        tampered["market_evidence"]["payload"]["fixture_id"] = "forged"
        tampered["prediction_hash"] = joint_scenario_model.calculate_prediction_hash(
            tampered
        )
        with self.assertRaisesRegex(
            joint_scenario_model.JointScenarioError,
            "does not reproduce|does not match expected_match_id",
        ):
            joint_scenario_model.validate_prediction(tampered)

    def test_structured_evidence_fixture_and_match_id_are_strictly_isolated(self):
        bundle = self._current_market_bundle()
        prediction = self._predict(bundle, expected_match_id="2913681")
        audit = prediction["market_evidence"]
        self.assertEqual(prediction["fixture"]["match_id"], "2913681")
        self.assertEqual(audit["fixture_binding"]["status"], "verified")
        self.assertEqual(audit["limitations"], [])
        self.assertEqual(audit["effective_quality"], "complete")
        self.assertEqual(audit["bundle_validation_status"], "validated_current_schema")
        joint_scenario_model.validate_prediction_inputs(
            prediction,
            self.model,
            self.score_prediction,
            self.htft_prediction,
            market_evidence=bundle,
            expected_match_id="2913681",
        )

        wrong_team = copy.deepcopy(bundle)
        wrong_team["fixture"]["home_team"] = "\ufffdlpha"
        with self.assertRaisesRegex(
            joint_scenario_model.JointScenarioError,
            "replacement characters|does not match",
        ):
            self._predict(wrong_team, expected_match_id="2913681")

        with self.assertRaisesRegex(
            joint_scenario_model.JointScenarioError,
            "does not match expected_match_id",
        ):
            self._predict(bundle, expected_match_id="other-match")

    def test_upstream_half_time_anchor_is_disclosed_but_attached_bundle_stays_zero_weight(
        self,
    ):
        anchored_htft = self._anchored_htft_prediction()
        bundle = self._current_market_bundle()
        prediction = self._predict(
            bundle,
            expected_match_id="2913681",
            htft_prediction=anchored_htft,
        )
        self.assertEqual(
            prediction["probability_mode"],
            "upstream_half_time_market_anchor",
        )
        self.assertTrue(prediction["market_conditioning_enabled"])
        self.assertTrue(prediction["upstream_anchor_conditioning_enabled"])
        self.assertTrue(prediction["research_market_informed"])
        self.assertFalse(prediction["formal_eligible"])
        self.assertFalse(prediction["attached_market_evidence_conditioning_enabled"])
        anchor = prediction["external_anchor_audit"]
        self.assertTrue(anchor["enabled"])
        self.assertEqual(anchor["component"], "half_time")
        self.assertTrue(anchor["de_vigged"])
        self.assertTrue(anchor["used_for_probability"])
        self.assertFalse(anchor["same_price_independent_ev_authorized"])
        self.assertEqual(prediction["market_evidence"]["conditioning_weight"], 0.0)
        self.assertFalse(prediction["market_evidence"]["used_for_probability"])
        self.assertFalse(prediction["policy"]["same_price_independent_ev_authorized"])
        for result, expected in {"H": 0.20, "D": 0.50, "A": 0.30}.items():
            self.assertAlmostEqual(
                prediction["htft_marginal"]["half_time_result_probabilities"][result],
                expected,
                places=12,
            )
        joint_scenario_model.validate_prediction_inputs(
            prediction,
            self.model,
            self.score_prediction,
            anchored_htft,
            market_evidence=bundle,
            expected_match_id="2913681",
        )

    def test_external_full_time_anchor_is_rejected_by_joint_model(self):
        anchored_htft = htft_model.predict_model(
            self.model,
            "Alpha",
            "Bravo",
            kickoff=KICKOFF,
            generated_at=INPUT_GENERATED_AT,
            max_goals=12,
            hard_max_goals=30,
            full_time_anchor={
                "probabilities": {"home": 0.40, "draw": 0.30, "away": 0.30},
                "source": "https://example.test/full-time-consensus",
                "captured_at": "2098-12-30T23:59:00Z",
                "de_vigged": True,
            },
        )
        with self.assertRaisesRegex(
            joint_scenario_model.JointScenarioError,
            "external full-time anchors are unsupported",
        ):
            self._predict(htft_prediction=anchored_htft)

    def test_policy_generated_at_and_derived_audits_are_strict_after_rehash(self):
        prediction = self._predict()
        mutations = (
            (
                "policy",
                lambda item: item["policy"].__setitem__(
                    "template_fallback_allowed", True
                ),
            ),
            (
                "generated_at",
                lambda item: item.__setitem__(
                    "generated_at", "2098-12-31T09:01:00+09:00"
                ),
            ),
            (
                "derived-field",
                lambda item: item["derived_field_audits"]["one_x_two"].__setitem__(
                    "recommendation_eligible", True
                ),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                tampered = copy.deepcopy(prediction)
                mutate(tampered)
                tampered["prediction_hash"] = (
                    joint_scenario_model.calculate_prediction_hash(tampered)
                )
                with self.assertRaises(joint_scenario_model.JointScenarioError):
                    joint_scenario_model.validate_prediction(tampered)

    def test_market_bundle_claim_fields_and_limits_are_rejected(self):
        forbidden = self._current_market_bundle()
        forbidden["recommendation"] = "home"
        with self.assertRaisesRegex(
            joint_scenario_model.JointScenarioError, "claim field is forbidden"
        ):
            self._predict(forbidden, expected_match_id="2913681")

        oversized = self._current_market_bundle()
        oversized["padding"] = "x" * (
            joint_scenario_model.MARKET_EVIDENCE_MAX_BYTES + 1
        )
        with self.assertRaisesRegex(
            joint_scenario_model.JointScenarioError, "canonical bytes"
        ):
            self._predict(oversized, expected_match_id="2913681")

    def test_cli_predict_and_validate(self):
        model_path = self.base / "model.json"
        score_path = self.base / "score.json"
        htft_path = self.base / "htft.json"
        output_path = self.base / "joint.json"
        for path, value in (
            (model_path, self.model),
            (score_path, self.score_prediction),
            (htft_path, self.htft_prediction),
        ):
            path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(
            joint_scenario_model.main(
                [
                    "predict",
                    "--model",
                    str(model_path),
                    "--score-prediction",
                    str(score_path),
                    "--htft-prediction",
                    str(htft_path),
                    "--generated-at",
                    JOINT_GENERATED_AT,
                    "--expected-match-id",
                    "2913681",
                    "--output",
                    str(output_path),
                ]
            ),
            0,
        )
        self.assertTrue(output_path.exists())
        saved = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["fixture"]["match_id"], "2913681")
        self.assertEqual(
            joint_scenario_model.main(["validate", "--prediction", str(output_path)]),
            0,
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import math
import unittest
from unittest import mock

from scripts import joint_scenario_model, public_market_outlook


class PublicMarketOutlookTests(unittest.TestCase):
    @staticmethod
    def _artifact():
        probability_mode = "model_only"
        base_audit = {
            "provenance": "validated_joint_cells",
            "probability_mode": probability_mode,
            "status": "model_probability_reference",
            "recommendation_eligible": False,
            "template_fallback_allowed": False,
        }
        audits = {
            field: dict(base_audit)
            for field in (
                "htft_marginal",
                "one_x_two",
                "goal_ranges",
                "btts",
            )
        }
        audits["joint_top_two"] = {
            "provenance": "validated_joint_cells_probability_ranking",
            "probability_mode": probability_mode,
            "status": "high_variance_reference",
            "recommendation_eligible": False,
            "template_fallback_allowed": False,
        }
        joint_top_two = [
            {
                "slot": 1,
                "htft": "DD",
                "score": "1-1",
                "home_goals": 1,
                "away_goals": 1,
                "probability": 0.056,
                "status": "high_variance_reference",
                "recommendation_eligible": False,
                "counts_toward_primary_record": False,
                "odds_available": False,
            },
            {
                "slot": 2,
                "htft": "HH",
                "score": "2-1",
                "home_goals": 2,
                "away_goals": 1,
                "probability": 0.043,
                "status": "high_variance_reference",
                "recommendation_eligible": False,
                "counts_toward_primary_record": False,
                "odds_available": False,
            },
        ]
        joint_cells = [
            {
                "htft": "DD",
                "score": "1-1",
                "home_goals": 1,
                "away_goals": 1,
                "probability": 0.056,
            },
            {
                "htft": "HH",
                "score": "2-1",
                "home_goals": 2,
                "away_goals": 1,
                "probability": 0.043,
            },
            {
                "htft": "DA",
                "score": "1-2",
                "home_goals": 1,
                "away_goals": 2,
                "probability": 0.036,
            },
            {
                "htft": "DH",
                "score": "1-0",
                "home_goals": 1,
                "away_goals": 0,
                "probability": 0.035,
            },
        ]
        filler_probability = (1.0 - 0.056 - 0.043 - 0.036 - 0.035) / 40.0
        joint_cells.extend(
            {
                "htft": "HH",
                "score": f"{home_goals}-0",
                "home_goals": home_goals,
                "away_goals": 0,
                "probability": filler_probability,
            }
            for home_goals in range(3, 43)
        )
        return {
            "prediction_hash": "sha256:" + "a" * 64,
            "schema_version": joint_scenario_model.LEGACY_SCHEMA_VERSION,
            "model_version": joint_scenario_model.LEGACY_MODEL_VERSION,
            "probability_mode": probability_mode,
            "formal_eligible": False,
            "htft_marginal": {
                "half_time_result_probabilities": {
                    "H": 0.35,
                    "D": 0.45,
                    "A": 0.20,
                },
                "full_time_result_probabilities": {
                    "H": 0.36,
                    "D": 0.24,
                    "A": 0.40,
                },
                "code_probabilities": {
                    "HH": 0.126,
                    "HD": 0.084,
                    "HA": 0.140,
                    "DH": 0.162,
                    "DD": 0.108,
                    "DA": 0.180,
                    "AH": 0.072,
                    "AD": 0.048,
                    "AA": 0.080,
                },
            },
            "derived": {
                "one_x_two": {"home": 0.36, "draw": 0.24, "away": 0.40},
                "goal_ranges": {
                    "0-1": 0.10,
                    "2-3": 0.44,
                    "4-6": 0.38,
                    "7+": 0.08,
                },
                "btts": {"yes": 0.61, "no": 0.39},
            },
            "derived_field_audits": audits,
            "joint_top_two": joint_top_two,
            "joint_cells": joint_cells,
        }

    def _build(self, artifact):
        with mock.patch.object(
            public_market_outlook.joint_scenario_model, "validate_prediction"
        ) as validator:
            result = public_market_outlook.build_public_market_outlook(artifact)
        validator.assert_called_once_with(artifact)
        return result

    def test_builds_one_complete_ranked_structure_without_mutating_input(self):
        artifact = self._artifact()
        original = copy.deepcopy(artifact)

        result = self._build(artifact)

        self.assertEqual(artifact, original)
        self.assertEqual(
            list(result["markets"]),
            ["half_time", "one_x_two", "goal_ranges", "btts"],
        )
        self.assertEqual(
            [item["code"] for item in result["markets"]["half_time"]["distribution"]],
            ["H", "D", "A"],
        )
        self.assertEqual(
            [item["code"] for item in result["markets"]["one_x_two"]["distribution"]],
            ["home", "draw", "away"],
        )
        self.assertEqual(
            [item["code"] for item in result["markets"]["goal_ranges"]["distribution"]],
            ["0-1", "2-3", "4-6", "7+"],
        )
        self.assertEqual(
            [item["code"] for item in result["markets"]["btts"]["distribution"]],
            ["yes", "no"],
        )
        for market in result["markets"].values():
            self.assertAlmostEqual(
                sum(item["probability"] for item in market["distribution"]), 1.0
            )
            self.assertFalse(market["recommendation_eligible"])
            self.assertEqual(market["display_count"], len(market["display_items"]))
            self.assertTrue(
                all(item in market["distribution"] for item in market["display_items"])
            )

        half = result["markets"]["half_time"]
        self.assertEqual((half["top1"]["code"], half["top2"]["code"]), ("D", "H"))
        self.assertAlmostEqual(half["gap_percentage_points"], 10.0)
        self.assertEqual(half["clarity"], "clear")
        self.assertEqual(half["display_count"], 2)
        self.assertEqual(half["display_reason"], "ordinary_market_top_two_reference")

        one_x_two = result["markets"]["one_x_two"]
        self.assertEqual(
            (one_x_two["top1"]["code"], one_x_two["top2"]["code"]),
            ("away", "home"),
        )
        self.assertAlmostEqual(one_x_two["gap_percentage_points"], 4.0)
        self.assertEqual(one_x_two["clarity"], "divided")
        self.assertEqual(one_x_two["display_count"], 2)
        self.assertEqual(
            one_x_two["display_reason"], "ordinary_market_top_two_reference"
        )

        goal_ranges = result["markets"]["goal_ranges"]
        self.assertAlmostEqual(goal_ranges["gap_percentage_points"], 6.0)
        self.assertEqual(goal_ranges["clarity"], "divided")
        self.assertEqual(goal_ranges["display_count"], 1)
        self.assertEqual(goal_ranges["display_reason"], "goal_ranges_public_top_one")
        self.assertEqual(goal_ranges["display_items"], [goal_ranges["top1"]])

        btts = result["markets"]["btts"]
        self.assertAlmostEqual(btts["gap_percentage_points"], 22.0)
        self.assertEqual(btts["clarity"], "clear")
        self.assertEqual(btts["display_count"], 2)
        self.assertEqual(btts["display_reason"], "binary_distribution")

    def test_current_like_only_real_joint_events_display_global_top_two(self):
        artifact = self._artifact()
        artifact["htft_marginal"]["half_time_result_probabilities"] = {
            "H": 0.3371228490880407,
            "D": 0.3865648771276534,
            "A": 0.2763122737843061,
        }
        artifact["htft_marginal"]["full_time_result_probabilities"] = {
            "H": 0.354738298472385,
            "D": 0.22555141620120955,
            "A": 0.41971028532640564,
        }
        artifact["htft_marginal"]["code_probabilities"] = {
            "HH": 0.1200000000000000,
            "HD": 0.0800000000000000,
            "HA": 0.1371228490880407,
            "DH": 0.1105648771276534,
            "DD": 0.1500000000000000,
            "DA": 0.1260000000000000,
            "AH": 0.0700000000000000,
            "AD": 0.0500000000000000,
            "AA": 0.1563122737843061,
        }
        artifact["derived"]["one_x_two"] = {
            "home": 0.354738298472385,
            "draw": 0.22555141620120955,
            "away": 0.41971028532640564,
        }
        artifact["derived"]["goal_ranges"] = {
            "0-1": 0.13958211147101007,
            "2-3": 0.40639451745554256,
            "4-6": 0.392012470201751,
            "7+": 0.06201090087169662,
        }

        result = self._build(artifact)
        markets = result["markets"]

        self.assertEqual(markets["half_time"]["display_count"], 2)
        self.assertEqual(
            [item["code"] for item in markets["half_time"]["display_items"]],
            ["D", "H"],
        )
        self.assertEqual(markets["one_x_two"]["display_count"], 2)
        self.assertEqual(
            [item["code"] for item in markets["one_x_two"]["display_items"]],
            ["away", "home"],
        )
        self.assertEqual(markets["goal_ranges"]["display_count"], 1)
        self.assertEqual(
            [item["code"] for item in markets["goal_ranges"]["display_items"]],
            ["2-3"],
        )
        self.assertEqual(
            markets["goal_ranges"]["display_reason"],
            "goal_ranges_public_top_one",
        )
        self.assertEqual(markets["btts"]["display_count"], 2)
        scenarios = result["joint_scenarios"]
        self.assertEqual(scenarios["display_count"], 2)
        self.assertEqual(
            [(item["htft"], item["score"]) for item in scenarios["display_items"]],
            [("DD", "1-1"), ("HH", "2-1")],
        )
        self.assertEqual(scenarios["items"], scenarios["display_items"])
        self.assertEqual(
            scenarios["display_policy"],
            "global_joint_probability_top_two_v1",
        )
        self.assertNotIn("third_probability", scenarios)
        self.assertNotIn("top2_top3_gap_percentage_points", scenarios)

    def test_global_top_two_with_same_htft_are_both_preserved(self):
        artifact = self._artifact()
        duplicate = {
            "htft": "DD",
            "score": "0-0",
            "home_goals": 0,
            "away_goals": 0,
            "probability": 0.052,
        }
        filler_cells = [
            item
            for item in artifact["joint_cells"]
            if item["htft"] == "HH" and item["home_goals"] >= 3
        ]
        for item in filler_cells:
            item["probability"] -= 0.052 / len(filler_cells)
        artifact["joint_cells"].append(duplicate)
        artifact["joint_top_two"][1] = {
            "slot": 2,
            **duplicate,
            "status": "high_variance_reference",
            "recommendation_eligible": False,
            "counts_toward_primary_record": False,
            "odds_available": False,
        }
        artifact["htft_marginal"] = {
            "half_time_result_probabilities": {
                "H": 0.32910261,
                "D": 0.41423911,
                "A": 0.25665828,
            },
            "full_time_result_probabilities": {
                "H": 0.39305642,
                "D": 0.24467945,
                "A": 0.36226413,
            },
            "code_probabilities": {
                "HH": 0.24957969,
                "HD": 0.04817649,
                "HA": 0.03134643,
                "DH": 0.12353129,
                "DD": 0.15567954,
                "DA": 0.13502828,
                "AH": 0.01994544,
                "AD": 0.04082342,
                "AA": 0.19588942,
            },
        }
        artifact["derived"]["one_x_two"] = {
            "home": 0.39305642,
            "draw": 0.24467945,
            "away": 0.36226413,
        }

        scenarios = self._build(artifact)["joint_scenarios"]

        self.assertEqual(
            [(item["htft"], item["score"]) for item in scenarios["items"]],
            [("DD", "1-1"), ("DD", "0-0")],
        )
        self.assertEqual([item["slot"] for item in scenarios["items"]], [1, 2])
        self.assertTrue(scenarios["artifact_top_two_exact_match"])
        self.assertEqual(
            scenarios["display_policy"],
            public_market_outlook.JOINT_DISPLAY_POLICY,
        )

    def test_clarity_thresholds_are_inclusive_and_ties_use_source_order(self):
        artifact = self._artifact()
        artifact["htft_marginal"]["half_time_result_probabilities"] = {
            "H": 0.32,
            "D": 0.40,
            "A": 0.28,
        }
        artifact["htft_marginal"]["code_probabilities"] = {
            "HH": 0.12,
            "HD": 0.08,
            "HA": 0.12,
            "DH": 0.16,
            "DD": 0.10,
            "DA": 0.14,
            "AH": 0.08,
            "AD": 0.06,
            "AA": 0.14,
        }
        artifact["derived"]["goal_ranges"] = {
            "0-1": 0.21,
            "2-3": 0.29,
            "4-6": 0.29,
            "7+": 0.21,
        }
        artifact["derived"]["btts"] = {"yes": 0.55, "no": 0.45}

        result = self._build(artifact)

        self.assertEqual(result["markets"]["half_time"]["clarity"], "clear")
        self.assertEqual(result["markets"]["goal_ranges"]["clarity"], "divided")
        self.assertEqual(
            result["markets"]["goal_ranges"]["top1"]["code"], "2-3"
        )
        self.assertEqual(
            result["markets"]["goal_ranges"]["top2"]["code"], "4-6"
        )
        self.assertEqual(result["markets"]["btts"]["clarity"], "clear")

    def test_joint_scenarios_remain_high_variance_non_recommendations(self):
        artifact = self._artifact()
        for item in artifact["joint_top_two"]:
            item["counts_as_primary"] = False
            item["requires_bookmaker_odds"] = False

        scenarios = self._build(artifact)["joint_scenarios"]

        self.assertEqual(scenarios["status"], "high_variance_reference")
        self.assertFalse(scenarios["recommendation_eligible"])
        self.assertFalse(scenarios["counts_as_primary"])
        self.assertFalse(scenarios["requires_bookmaker_odds"])
        self.assertEqual(
            scenarios["warning"], public_market_outlook.JOINT_SCENARIO_WARNING
        )
        self.assertEqual(scenarios["display_count"], 2)
        self.assertEqual(scenarios["ranking_source"], "legacy_joint_cells")
        self.assertTrue(scenarios["artifact_top_two_exact_match"])
        for item in scenarios["items"]:
            self.assertEqual(item["status"], "high_variance_reference")
            self.assertFalse(item["recommendation_eligible"])
            self.assertFalse(item["counts_toward_primary_record"])
            self.assertFalse(item["odds_available"])
            self.assertFalse(item["counts_as_primary"])
            self.assertFalse(item["requires_bookmaker_odds"])

    def test_schema_two_reconstructs_ranking_from_validated_path_kernel_planes(self):
        artifact = self._artifact()
        artifact["schema_version"] = joint_scenario_model.SCHEMA_VERSION
        artifact["model_version"] = joint_scenario_model.MODEL_VERSION
        artifact.pop("joint_cells")
        artifact["path_kernel"] = {"opaque": "validated-by-kernel"}

        filler_probability = (1.0 - 0.056 - 0.043 - 0.036) / 72.0
        planes = [
            [[filler_probability for _away in range(5)] for _home in range(5)]
            for _half in range(3)
        ]
        planes[1][1][1] = 0.056
        planes[0][2][1] = 0.043
        planes[1][1][2] = 0.036

        with mock.patch.object(
            public_market_outlook.joint_path_kernel,
            "validate_kernel",
            return_value={"event_planes": planes},
        ) as kernel_validator:
            scenarios = self._build(artifact)["joint_scenarios"]

        kernel_validator.assert_called_once_with(artifact["path_kernel"])
        self.assertEqual(
            scenarios["ranking_source"], "validated_path_kernel_event_planes"
        )
        self.assertEqual(scenarios["display_count"], 2)
        self.assertEqual(
            [(item["htft"], item["score"]) for item in scenarios["display_items"]],
            [("DD", "1-1"), ("HH", "2-1")],
        )

    def test_reconstructed_top_two_must_exactly_match_frozen_artifact(self):
        artifact = self._artifact()
        artifact["joint_top_two"][1]["probability"] = 0.042

        with self.assertRaisesRegex(
            public_market_outlook.PublicMarketOutlookError,
            "does not exactly match",
        ):
            self._build(artifact)

    def test_public_joint_top_two_does_not_expose_independent_htft_ranking(self):
        artifact = self._artifact()
        artifact["htft_marginal"]["code_probabilities"].pop("DH")

        scenarios = self._build(artifact)["joint_scenarios"]

        self.assertEqual(
            [(item["htft"], item["score"]) for item in scenarios["items"]],
            [("DD", "1-1"), ("HH", "2-1")],
        )

    def test_each_joint_scenario_safety_field_is_mandatory(self):
        mutations = {
            "status": "formal_pick",
            "recommendation_eligible": True,
            "counts_toward_primary_record": True,
            "odds_available": True,
        }
        for field, unsafe_value in mutations.items():
            with self.subTest(field=field):
                artifact = self._artifact()
                artifact["joint_top_two"][0][field] = unsafe_value
                with self.assertRaisesRegex(
                    public_market_outlook.PublicMarketOutlookError,
                    "high-variance safety policy",
                ):
                    self._build(artifact)

        for alias in ("counts_as_primary", "requires_bookmaker_odds"):
            with self.subTest(alias=alias):
                artifact = self._artifact()
                artifact["joint_top_two"][0][alias] = True
                with self.assertRaisesRegex(
                    public_market_outlook.PublicMarketOutlookError,
                    "unsafe policy alias",
                ):
                    self._build(artifact)

    def test_rejects_incomplete_nonfinite_or_unnormalized_distributions(self):
        cases = []
        missing = self._artifact()
        del missing["derived"]["goal_ranges"]["7+"]
        cases.append(("missing", missing, "must contain exactly"))
        nonfinite = self._artifact()
        nonfinite["derived"]["btts"]["yes"] = math.nan
        cases.append(("nonfinite", nonfinite, "between zero and one"))
        wrong_sum = self._artifact()
        wrong_sum["htft_marginal"]["half_time_result_probabilities"]["A"] = 0.10
        cases.append(("wrong_sum", wrong_sum, "must sum to one"))

        for label, artifact, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    public_market_outlook.PublicMarketOutlookError, message
                ):
                    self._build(artifact)

    def test_rejects_1x2_conflict_or_unsafe_derived_audit(self):
        conflict = self._artifact()
        conflict["derived"]["one_x_two"] = {
            "home": 0.40,
            "draw": 0.24,
            "away": 0.36,
        }
        with self.assertRaisesRegex(
            public_market_outlook.PublicMarketOutlookError, "conflicts"
        ):
            self._build(conflict)

        unsafe = self._artifact()
        unsafe["derived_field_audits"]["btts"]["recommendation_eligible"] = True
        with self.assertRaisesRegex(
            public_market_outlook.PublicMarketOutlookError, "not safe"
        ):
            self._build(unsafe)

    def test_wraps_canonical_validator_failure_and_never_returns_output(self):
        artifact = self._artifact()
        with mock.patch.object(
            public_market_outlook.joint_scenario_model,
            "validate_prediction",
            side_effect=joint_scenario_model.JointScenarioError("tampered"),
        ):
            with self.assertRaisesRegex(
                public_market_outlook.PublicMarketOutlookError,
                "validation failed: tampered",
            ):
                public_market_outlook.build_public_market_outlook(artifact)


if __name__ == "__main__":
    unittest.main()

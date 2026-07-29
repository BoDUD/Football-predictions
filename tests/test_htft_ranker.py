from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "htft_ranker.py"
SPEC = importlib.util.spec_from_file_location("soccer_htft_ranker", SCRIPT)
assert SPEC and SPEC.loader
ranker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ranker)


MATRIX = {
    "HH": 0.28411078497,
    "HD": 0.05196738607,
    "HA": 0.02302166019,
    "DH": 0.16395092725,
    "DD": 0.12713650265,
    "DA": 0.10814619665,
    "AH": 0.03292272544,
    "AD": 0.05108238646,
    "AA": 0.15765934557,
}
HALF = {"H": 0.35909983123, "D": 0.39923362655, "A": 0.24166445747}
FULL = {"H": 0.48098443766, "D": 0.23018627518, "A": 0.28882720241}
ODDS = {
    "HH": 1.725,
    "HD": 13.0,
    "HA": 28.5,
    "DH": 4.25,
    "DD": 5.8,
    "DA": 8.0,
    "AH": 19.5,
    "AD": 13.5,
    "AA": 4.9,
}


def market_with_scenario_edges() -> dict[str, float]:
    market = dict(MATRIX)
    market["HH"] -= 0.05
    market["AA"] -= 0.04
    market["DD"] += 0.09
    return market


class HtftRankerTests(unittest.TestCase):
    def test_stability_selection_rejects_mechanical_joint_top_two(self):
        result = ranker.rank_htft(
            MATRIX,
            HALF,
            FULL,
            odds=ODDS,
            firm_count=6,
            data_quality="medium",
        )

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["HH", "AA"],
        )
        self.assertEqual(result["selection_basis"], "scenario_stability_v1")
        self.assertNotIn("rank", result["scenarios"][0])
        self.assertEqual(result["ranking_basis"], result["selection_basis"])
        self.assertEqual(
            [item["selection"] for item in result["top_two"]],
            ["HH", "AA"],
        )
        self.assertEqual(
            [item["rank"] for item in result["top_two"]],
            [1, 2],
        )
        self.assertGreater(
            result["scenarios"][1]["conditional_stability"],
            MATRIX["DH"] / HALF["D"],
        )
        self.assertTrue(result["marginal_validation"]["passed"])
        self.assertEqual(result["formal_count"], 0)
        self.assertTrue(
            all(item["status"] == "observation" for item in result["scenarios"])
        )

    def test_transition_path_can_be_selected_when_follow_through_is_strong(self):
        matrix = {
            "HH": 0.20,
            "HD": 0.03,
            "HA": 0.02,
            "DH": 0.30,
            "DD": 0.05,
            "DA": 0.05,
            "AH": 0.20,
            "AD": 0.05,
            "AA": 0.10,
        }
        half = {"H": 0.25, "D": 0.40, "A": 0.35}
        full = {"H": 0.70, "D": 0.13, "A": 0.17}

        result = ranker.rank_htft(matrix, half, full)

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["HH", "DH"],
        )
        self.assertFalse(result["scenarios"][1]["state_continuity"])

    def test_second_slot_is_explicit_when_stability_evidence_is_insufficient(self):
        matrix = {
            "HH": 0.64,
            "HD": 0.08,
            "HA": 0.08,
            "DH": 0.08,
            "DD": 0.01,
            "DA": 0.01,
            "AH": 0.08,
            "AD": 0.01,
            "AA": 0.01,
        }
        half = {"H": 0.80, "D": 0.10, "A": 0.10}
        full = {"H": 0.80, "D": 0.10, "A": 0.10}

        result = ranker.rank_htft(matrix, half, full)

        self.assertEqual(len(result["scenarios"]), 2)
        self.assertEqual(result["scenarios"][0]["stability_status"], "supported")
        self.assertEqual(result["scenarios"][1]["stability_status"], "insufficient")
        self.assertEqual(result["scenarios"][1]["status"], "observation")
        self.assertIn(
            "below threshold",
            result["scenarios"][1]["selection_reason"],
        )

    def test_inconsistent_full_time_marginal_is_rejected(self):
        bad_full = {"H": 0.40, "D": 0.30, "A": 0.30}

        with self.assertRaisesRegex(ValueError, "matrix marginals"):
            ranker.rank_htft(MATRIX, HALF, bad_full)

    def test_inconsistent_half_time_marginal_is_rejected(self):
        bad_half = {"H": 0.30, "D": 0.45, "A": 0.25}

        with self.assertRaisesRegex(ValueError, "matrix marginals"):
            ranker.rank_htft(MATRIX, bad_half, FULL)

    def test_tolerance_cannot_exceed_half_a_percentage_point(self):
        with self.assertRaisesRegex(ValueError, r"within \[0, 0.5\]"):
            ranker.rank_htft(MATRIX, HALF, FULL, tolerance_pp=20)

    def test_non_finite_and_out_of_range_values_are_rejected(self):
        nan_matrix = {**MATRIX, "HH": float("nan")}
        high_matrix = {**MATRIX, "HH": 1.1}

        with self.assertRaisesRegex(ValueError, "finite"):
            ranker.rank_htft(nan_matrix, HALF, FULL)
        with self.assertRaisesRegex(ValueError, r"within \[0, 1\]"):
            ranker.rank_htft(high_matrix, HALF, FULL)
        with self.assertRaisesRegex(ValueError, "finite"):
            ranker.rank_htft(
                MATRIX,
                HALF,
                FULL,
                odds={**ODDS, "HH": float("inf")},
            )

    def test_supplied_market_probabilities_must_match_complete_odds(self):
        inconsistent_market = {key: 1 / 9 for key in ranker.OUTCOMES}

        with self.assertRaisesRegex(ValueError, "disagree with odds-derived"):
            ranker.rank_htft(
                MATRIX,
                HALF,
                FULL,
                odds=ODDS,
                market_probabilities=inconsistent_market,
            )

    def test_ev_can_qualify_but_cannot_choose_the_shape(self):
        value_odds = {key: 10.0 for key in ranker.OUTCOMES}
        value_odds["HH"] = 4.0
        result = ranker.rank_htft(
            MATRIX,
            HALF,
            FULL,
            odds=value_odds,
            firm_count=6,
            data_quality="high",
        )

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["HH", "AA"],
        )
        self.assertTrue(
            all(item["status"] == "formal" for item in result["scenarios"])
        )
        self.assertGreaterEqual(result["scenarios"][0]["ev"], 0.08)
        self.assertGreaterEqual(result["scenarios"][0]["edge_pp"], 4.0)
        self.assertIn(
            "DH",
            [item["selection"] for item in result["value_anomalies"]],
        )

    def test_sub_eight_percent_positive_ev_can_qualify(self):
        odds = {
            "HH": 1.05 / MATRIX["HH"],
            "AA": 1.04 / MATRIX["AA"],
        }
        result = ranker.rank_htft(
            MATRIX,
            HALF,
            FULL,
            odds=odds,
            market_probabilities=market_with_scenario_edges(),
            firm_count=6,
            data_quality="high",
        )

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["HH", "AA"],
        )
        self.assertTrue(
            all(item["status"] == "formal" for item in result["scenarios"])
        )
        self.assertTrue(
            all(0 < item["ev"] < 0.08 for item in result["scenarios"])
        )

    def test_zero_and_negative_ev_do_not_qualify(self):
        odds = {
            "HH": 1.0 / MATRIX["HH"],
            "AA": 0.99 / MATRIX["AA"],
        }
        result = ranker.rank_htft(
            MATRIX,
            HALF,
            FULL,
            odds=odds,
            market_probabilities=market_with_scenario_edges(),
            firm_count=6,
            data_quality="high",
        )

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["HH", "AA"],
        )
        self.assertTrue(
            all(item["status"] == "observation" for item in result["scenarios"])
        )
        self.assertTrue(
            all(
                any("is not positive" in failure for failure in item["failed_thresholds"])
                for item in result["scenarios"]
            )
        )

    def test_zero_market_edge_does_not_qualify(self):
        odds = {
            "HH": 1.05 / MATRIX["HH"],
            "AA": 1.04 / MATRIX["AA"],
        }
        result = ranker.rank_htft(
            MATRIX,
            HALF,
            FULL,
            odds=odds,
            market_probabilities=dict(MATRIX),
            firm_count=6,
            data_quality="high",
        )

        self.assertTrue(
            all(item["status"] == "observation" for item in result["scenarios"])
        )
        self.assertTrue(
            all(
                any("edge" in failure and "not positive" in failure
                    for failure in item["failed_thresholds"])
                for item in result["scenarios"]
            )
        )

    def test_market_depth_and_data_quality_protections_still_apply(self):
        odds = {
            "HH": 1.05 / MATRIX["HH"],
            "AA": 1.04 / MATRIX["AA"],
        }
        market = market_with_scenario_edges()

        shallow = ranker.rank_htft(
            MATRIX,
            HALF,
            FULL,
            odds=odds,
            market_probabilities=market,
            firm_count=4,
            data_quality="high",
        )
        low_quality = ranker.rank_htft(
            MATRIX,
            HALF,
            FULL,
            odds=odds,
            market_probabilities=market,
            firm_count=6,
            data_quality="low",
        )

        self.assertTrue(
            all(item["status"] == "observation" for item in shallow["scenarios"])
        )
        self.assertTrue(
            all(item["status"] == "observation" for item in low_quality["scenarios"])
        )
        self.assertTrue(
            all(
                "firm count 4 < 5" in item["failed_thresholds"]
                for item in shallow["scenarios"]
            )
        )
        self.assertTrue(
            all(
                "data quality low" in item["failed_thresholds"]
                for item in low_quality["scenarios"]
            )
        )

    def test_missing_odds_uses_probability_and_marks_observation(self):
        result = ranker.rank_htft(MATRIX, HALF, FULL)

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["HH", "AA"],
        )
        self.assertTrue(
            all(item["status"] == "observation" for item in result["scenarios"])
        )
        self.assertIn(
            "current odds unavailable",
            result["scenarios"][0]["failed_thresholds"],
        )


if __name__ == "__main__":
    unittest.main()

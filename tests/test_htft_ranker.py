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
ODDS_CONTEXT = {
    "kind": "current_htft_nine_way_market",
    "complete": True,
    "source": "verified current nine-way consensus",
    "firm_count": 6,
    "captured_at": "2026-08-02T12:00:00Z",
    "kickoff": "2026-08-02T13:00:00Z",
}
MODEL_HASH = "sha256:" + "1" * 64


def league_evidence(
    league_key: str,
    *,
    eligible: int,
    covered: int,
    hits: int,
    deployment_status: str = "candidate",
    regime_warning: str | None = None,
) -> dict[str, object]:
    return {
        "version": ranker.LEAGUE_PAIR_GATE_EVIDENCE_VERSION,
        "dataset_manifest_hash": "sha256:" + "2" * 64,
        "evaluation_hash": "sha256:" + "3" * 64,
        "model_hash": MODEL_HASH,
        "league_key": league_key,
        "source_role": "historical_post_selection_development_evidence",
        "threshold": ranker.MODEL_ONLY_PAIR_MASS_THRESHOLD,
        "eligible_sample_count": eligible,
        "covered_count": covered,
        "hit_count": hits,
        "deployment_status": deployment_status,
        "regime_warning": regime_warning,
        "formal_htft_eligible": False,
        "production_confidence_eligible": False,
    }


def market_with_scenario_edges() -> dict[str, float]:
    market = dict(MATRIX)
    market["HH"] -= 0.05
    market["AA"] -= 0.04
    market["DD"] += 0.09
    return market


class HtftRankerTests(unittest.TestCase):
    def test_v3_selects_raw_joint_probability_top_two(self):
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
            ["HH", "DH"],
        )
        self.assertEqual(
            result["selection_basis"],
            "probability_top2_v3_post_selection",
        )
        self.assertNotIn("rank", result["scenarios"][0])
        self.assertEqual(result["ranking_basis"], result["selection_basis"])
        self.assertEqual(
            [item["selection"] for item in result["top_two"]],
            ["HH", "DH"],
        )
        self.assertEqual(
            [item["rank"] for item in result["top_two"]],
            [1, 2],
        )
        self.assertAlmostEqual(
            result["scenarios"][1]["conditional_stability"],
            MATRIX["DH"] / HALF["D"],
        )
        self.assertTrue(result["marginal_validation"]["passed"])
        self.assertAlmostEqual(
            result["pair_probability_mass"],
            MATRIX["HH"] + MATRIX["DH"],
        )
        self.assertEqual(result["pair_mass_threshold"], 0.46)
        self.assertFalse(result["pair_mass_gate_passed"])
        self.assertEqual(result["confidence_status"], "low_pair_probability_mass")
        self.assertEqual(result["formal_count"], 0)
        self.assertTrue(
            all(item["status"] == "observation" for item in result["scenarios"])
        )

    def test_coherence_is_audit_only_and_cannot_replace_a_probability_slot(self):
        matrix = {
            "HH": 0.2886997689,
            "HD": 0.0446901288,
            "HA": 0.0108984219,
            "DH": 0.1769962122,
            "DD": 0.2006526807,
            "DA": 0.0979046617,
            "AH": 0.0176019081,
            "AD": 0.0416661843,
            "AA": 0.1208899510,
        }
        half = {
            "H": 0.3442883195,
            "D": 0.4755535546,
            "A": 0.1801580434,
        }
        full = {
            "H": 0.4832978892,
            "D": 0.2870089938,
            "A": 0.2296930346,
        }

        result = ranker.rank_htft(
            matrix,
            half,
            full,
        )

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["HH", "DD"],
        )
        self.assertEqual(
            result["coherence_policy"]["allowed_terminal_results"],
            ["H", "D"],
        )
        self.assertTrue(
            all(
                item["coherence_status"] == "on_thesis"
                for item in result["scenarios"]
            )
        )

    def test_exact_score_results_are_audit_only(self):
        matrix = {
            "HH": 0.2886997689,
            "HD": 0.0446901288,
            "HA": 0.0108984219,
            "DH": 0.1769962122,
            "DD": 0.2006526807,
            "DA": 0.0979046617,
            "AH": 0.0176019081,
            "AD": 0.0416661843,
            "AA": 0.1208899510,
        }
        half = {
            "H": 0.3442883195,
            "D": 0.4755535546,
            "A": 0.1801580434,
        }
        full = {
            "H": 0.4832978892,
            "D": 0.2870089938,
            "A": 0.2296930346,
        }

        result = ranker.rank_htft(
            matrix,
            half,
            full,
            exact_score_results=["home", "away"],
        )

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["HH", "DD"],
        )
        self.assertTrue(result["coherence_policy"]["exact_score_audit_only"])
        self.assertEqual(
            [
                item["exact_score_result_aligned"]
                for item in result["scenarios"]
            ],
            [True, False],
        )

    def test_second_place_full_time_tie_does_not_arbitrarily_exclude_a_result(self):
        matrix = {
            "HH": 0.30,
            "HD": 0.02,
            "HA": 0.03,
            "DH": 0.15,
            "DD": 0.16,
            "DA": 0.14,
            "AH": 0.05,
            "AD": 0.07,
            "AA": 0.08,
        }
        half = {"H": 0.35, "D": 0.45, "A": 0.20}
        full = {"H": 0.50, "D": 0.25, "A": 0.25}

        result = ranker.rank_htft(matrix, half, full)

        self.assertEqual(
            result["coherence_policy"]["allowed_terminal_results"],
            ["H", "D", "A"],
        )

    def test_off_thesis_joint_top_two_path_remains_in_its_slot(self):
        matrix = {
            "HH": 0.038068613135096914,
            "HD": 0.1081065306610073,
            "HA": 0.0030697434075369462,
            "DH": 0.07019623096012545,
            "DD": 0.051244833813134404,
            "DA": 0.18027816059764792,
            "AH": 0.37027203941340314,
            "AD": 0.13304601045625775,
            "AA": 0.04571783755579024,
        }
        half = {
            result: sum(matrix[f"{result}{full}"] for full in ranker.FULL_RESULTS)
            for result in ranker.HALF_RESULTS
        }
        full = {
            result: sum(matrix[f"{half}{result}"] for half in ranker.HALF_RESULTS)
            for result in ranker.FULL_RESULTS
        }

        result = ranker.rank_htft(matrix, half, full)

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["AH", "DA"],
        )
        self.assertEqual(
            result["scenarios"][1]["coherence_status"],
            "off_thesis_fallback",
        )
        self.assertEqual(result["scenarios"][1]["stability_status"], "supported")

    def test_conditional_follow_through_cannot_replace_probability_top_two(self):
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

        result = ranker.rank_htft(
            matrix,
            half,
            full,
            exact_score_results=["home", "home"],
        )

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["DH", "HH"],
        )
        self.assertFalse(result["scenarios"][0]["state_continuity"])
        self.assertTrue(result["coherence_policy"]["exact_score_audit_only"])
        self.assertTrue(
            all(
                item["exact_score_result_aligned"]
                for item in result["scenarios"]
            )
        )

    def test_exact_score_result_audit_requires_exactly_two_valid_results(self):
        with self.assertRaisesRegex(ValueError, "exactly two"):
            ranker.rank_htft(
                MATRIX,
                HALF,
                FULL,
                exact_score_results=["home"],
            )
        with self.assertRaisesRegex(ValueError, "home/H"):
            ranker.rank_htft(
                MATRIX,
                HALF,
                FULL,
                exact_score_results=["home", "unknown"],
            )

    def test_second_slot_is_still_shown_when_pair_mass_is_below_gate(self):
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
        self.assertEqual(
            result["scenarios"][1]["confidence_status"],
            "league_context_required",
        )
        self.assertTrue(result["pair_mass_threshold_crossed"])
        self.assertFalse(result["pair_mass_gate_passed"])
        self.assertIn(
            "stability evidence is below",
            result["scenarios"][1]["selection_reason"],
        )

    def test_probability_ties_use_canonical_outcome_order(self):
        matrix = {selection: 1 / 9 for selection in ranker.OUTCOMES}
        marginals = {result: 1 / 3 for result in ranker.HALF_RESULTS}

        result = ranker.rank_htft(matrix, marginals, marginals)

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["HH", "HD"],
        )
        self.assertEqual(
            result["selection_policy"]["canonical_outcome_order"],
            list(ranker.OUTCOMES),
        )

    def test_policy_pause_and_anchor_provenance_cannot_be_bypassed(self):
        matrix = {
            "HH": 0.26,
            "HD": 0.04,
            "HA": 0.03,
            "DH": 0.10,
            "DD": 0.22,
            "DA": 0.08,
            "AH": 0.08,
            "AD": 0.07,
            "AA": 0.12,
        }
        half = {"H": 0.33, "D": 0.40, "A": 0.27}
        full = {"H": 0.44, "D": 0.33, "A": 0.23}
        market = dict(matrix)
        market["HH"] -= 0.04
        market["DD"] -= 0.04
        market["AA"] += 0.08
        odds = {selection: 1.0 / probability for selection, probability in market.items()}

        model_only = ranker.rank_htft(
            matrix,
            half,
            full,
            odds=odds,
            market_probabilities=market,
            firm_count=6,
            data_quality="high",
            league_key="brazil_serie_a",
            league_evidence=league_evidence(
                "brazil_serie_a", eligible=380, covered=125, hits=60
            ),
            model_hash=MODEL_HASH,
        )
        self.assertAlmostEqual(model_only["pair_mass"], 0.48)
        self.assertEqual(model_only["pair_mass_threshold"], 0.46)
        self.assertTrue(model_only["pair_mass_threshold_crossed"])
        self.assertFalse(model_only["pair_mass_gate_passed"])
        self.assertTrue(
            all(item["status"] == "observation" for item in model_only["scenarios"])
        )
        self.assertEqual(model_only["formal_count"], 0)
        self.assertEqual(model_only["diagnostically_qualified_count"], 0)
        self.assertEqual(
            model_only["league_gate_evidence"]["status"],
            "league_gate_lower_bound_not_above_chance",
        )
        self.assertTrue(
            all(
                any(
                    "odds provenance unavailable" in failure
                    for failure in item["diagnostic_failed_thresholds"]
                )
                for item in model_only["scenarios"]
            )
        )
        self.assertFalse(model_only["market_policy"]["htft_formal_enabled"])
        self.assertTrue(
            all(
                "pauses HT/FT formal picks" in item["failed_thresholds"][-1]
                for item in model_only["scenarios"]
            )
        )

        with self.assertRaisesRegex(ValueError, "research-only"):
            ranker.rank_htft(
                matrix,
                half,
                full,
                odds=odds,
                market_probabilities=market,
                firm_count=6,
                data_quality="high",
                market_anchored=True,
            )

        anchored = ranker.rank_htft(
            matrix,
            half,
            full,
            odds=odds,
            market_probabilities=market,
            firm_count=6,
            data_quality="high",
            anchor_context={
                "kind": "half_time_current_market",
                "complete": True,
                "de_vigged": True,
                "source": "verified current first-half consensus",
                "captured_at": "2026-08-02T12:00:00Z",
            },
        )
        self.assertEqual(
            anchored["matrix_mode"], "half_time_market_anchor_unvalidated"
        )
        self.assertIsNone(anchored["pair_mass_threshold"])
        self.assertFalse(anchored["pair_mass_gate_passed"])
        self.assertEqual(anchored["confidence_status"], "anchor_gate_unvalidated")
        self.assertTrue(
            all(
                item["diagnostic_qualification_status"] == "unqualified"
                and item["status"] == "observation"
                for item in anchored["scenarios"]
            )
        )

    def test_league_evidence_prevents_global_gate_from_being_generalized(self):
        matrix = {
            "HH": 0.26,
            "HD": 0.04,
            "HA": 0.03,
            "DH": 0.10,
            "DD": 0.22,
            "DA": 0.08,
            "AH": 0.08,
            "AD": 0.07,
            "AA": 0.12,
        }
        half = {"H": 0.33, "D": 0.40, "A": 0.27}
        full = {"H": 0.44, "D": 0.33, "A": 0.23}

        japan = ranker.rank_htft(
            matrix,
            half,
            full,
            league_key="japan_j1",
            league_evidence=league_evidence(
                "japan_j1",
                eligible=380,
                covered=66,
                hits=32,
                regime_warning="special-season regime shift is unconfirmed",
            ),
            model_hash=MODEL_HASH,
        )
        korea = ranker.rank_htft(
            matrix,
            half,
            full,
            league_key="korea_k_league_1",
            league_evidence=league_evidence(
                "korea_k_league_1",
                eligible=228,
                covered=29,
                hits=10,
                deployment_status="shadow",
                regime_warning="historical model did not clear the deployment gate",
            ),
            model_hash=MODEL_HASH,
        )
        spain = ranker.rank_htft(
            matrix,
            half,
            full,
            league_key="spain_la_liga",
            league_evidence=league_evidence(
                "spain_la_liga", eligible=380, covered=143, hits=94
            ),
            model_hash=MODEL_HASH,
        )

        self.assertTrue(japan["pair_mass_threshold_crossed"])
        self.assertFalse(japan["pair_mass_gate_passed"])
        self.assertEqual(
            japan["confidence_status"],
            "competition_regime_shift_unconfirmed",
        )
        self.assertAlmostEqual(
            japan["league_gate_evidence"]["hit_rate_when_covered"],
            32 / 66,
        )
        self.assertEqual(
            korea["confidence_status"],
            "shadow_model_live_forward_unconfirmed",
        )
        self.assertEqual(korea["league_gate_evidence"]["deployment_status"], "shadow")
        self.assertAlmostEqual(
            korea["league_gate_evidence"]["hit_rate_when_covered"], 10 / 29
        )
        self.assertFalse(korea["league_gate_evidence"]["historical_component_signal"])
        self.assertEqual(
            spain["league_gate_evidence"]["status"],
            "historical_component_signal_live_forward_unconfirmed",
        )
        self.assertTrue(spain["league_gate_evidence"]["historical_component_signal"])
        self.assertFalse(spain["league_gate_evidence"]["production_confidence_eligible"])
        self.assertFalse(spain["pair_mass_gate_passed"])
        self.assertEqual(korea["formal_count"], 0)

    def test_odds_context_requires_pre_kickoff_provenance(self):
        with self.assertRaisesRegex(ValueError, "strictly before kickoff"):
            ranker.rank_htft(
                MATRIX,
                HALF,
                FULL,
                odds=ODDS,
                firm_count=6,
                odds_context={
                    **ODDS_CONTEXT,
                    "captured_at": ODDS_CONTEXT["kickoff"],
                },
            )
        with self.assertRaisesRegex(ValueError, "must match firm_count"):
            ranker.rank_htft(
                MATRIX,
                HALF,
                FULL,
                odds=ODDS,
                firm_count=5,
                odds_context=ODDS_CONTEXT,
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
            odds_context=ODDS_CONTEXT,
        )

        self.assertEqual(
            [item["selection"] for item in result["scenarios"]],
            ["HH", "DH"],
        )
        self.assertTrue(all(item["status"] == "observation" for item in result["scenarios"]))
        self.assertTrue(all(not item["pair_mass_gate_passed"] for item in result["scenarios"]))
        self.assertGreaterEqual(result["scenarios"][0]["ev"], 0.08)
        self.assertGreaterEqual(result["scenarios"][0]["edge_pp"], 4.0)
        self.assertIn(
            "AA",
            [item["selection"] for item in result["value_anomalies"]],
        )

    def test_sub_eight_percent_positive_ev_still_needs_pair_mass_gate(self):
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
            ["HH", "DH"],
        )
        self.assertFalse(result["pair_mass_gate_passed"])
        self.assertTrue(all(item["status"] == "observation" for item in result["scenarios"]))
        self.assertTrue(
            all(
                "complete current 9-way HT/FT odds unavailable"
                in item["diagnostic_failed_thresholds"]
                for item in result["scenarios"]
            )
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
            ["HH", "DH"],
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
            ["HH", "DH"],
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

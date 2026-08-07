from __future__ import annotations

import copy
import csv
import importlib.util
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "score_model.py"
SPEC = importlib.util.spec_from_file_location("soccer_score_model", SCRIPT)
assert SPEC and SPEC.loader
score_model = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(score_model)


SAMPLE_ROWS = [
    ("2025-01-01", "Alpha", "Bravo", 2, 0),
    ("2025-01-08", "Charlie", "Delta", 1, 1),
    ("2025-01-15", "Bravo", "Charlie", 0, 1),
    ("2025-01-22", "Delta", "Alpha", 1, 3),
    ("2025-02-01", "Alpha", "Charlie", 2, 1),
    ("2025-02-08", "Bravo", "Delta", 1, 0),
    ("2025-02-15", "Charlie", "Alpha", 2, 2),
    ("2025-02-22", "Delta", "Bravo", 0, 0),
    ("2025-03-01", "Alpha", "Delta", 3, 1),
    ("2025-03-08", "Charlie", "Bravo", 1, 0),
    ("2025-03-15", "Bravo", "Alpha", 1, 2),
    ("2025-03-22", "Delta", "Charlie", 2, 1),
]
PREDICTION_GENERATED_AT = "2098-12-31T00:00:00Z"
PREDICTION_KICKOFF = "2099-01-01T12:00:00Z"


class ScoreModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.csv_path = self.base / "matches.csv"
        self._write_csv(self.csv_path, SAMPLE_ROWS)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write_csv(path: Path, rows):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["date", "home_team", "away_team", "home_goals", "away_goals"]
            )
            writer.writerows(rows)

    def _fit(self):
        return score_model.fit_model(
            self.csv_path,
            iterations=180,
            learning_rate=0.025,
            regularization=0.03,
            half_life_days=180.0,
        )

    def test_fit_and_prediction_are_numerically_deterministic(self):
        first = self._fit()
        second = self._fit()
        reversed_path = self.base / "matches-reversed.csv"
        self._write_csv(reversed_path, reversed(SAMPLE_ROWS))
        reordered = score_model.fit_model(
            reversed_path,
            iterations=180,
            learning_rate=0.025,
            regularization=0.03,
            half_life_days=180.0,
        )

        self.assertEqual(first["model_hash"], second["model_hash"])
        self.assertEqual(first["model_hash"], reordered["model_hash"])
        self.assertEqual(first["parameters"], second["parameters"])
        self.assertEqual(first["parameters"], reordered["parameters"])
        self.assertEqual(
            first["training"]["source_data_hash"],
            second["training"]["source_data_hash"],
        )

        prediction_one = score_model.predict_model(
            first,
            "Alpha",
            "Bravo",
            kickoff=PREDICTION_KICKOFF,
            generated_at=PREDICTION_GENERATED_AT,
        )
        prediction_two = score_model.predict_model(
            second,
            "Alpha",
            "Bravo",
            kickoff=PREDICTION_KICKOFF,
            generated_at=PREDICTION_GENERATED_AT,
        )
        self.assertEqual(prediction_one, prediction_two)

    def test_joint_optimizer_reports_reproducible_convergence_diagnostics(self):
        model = self._fit()
        fit = model["fit"]
        self.assertEqual(fit["optimizer"], "deterministic_projected_joint_adam")
        self.assertEqual(fit["optimizer_version"], score_model.JOINT_OPTIMIZER_VERSION)
        self.assertIsInstance(fit["converged"], bool)
        self.assertGreaterEqual(fit["iterations"], 1)
        self.assertLessEqual(fit["iterations"], model["config"]["iterations"])
        self.assertLessEqual(fit["final_objective"], fit["initial_objective"] + 1e-12)
        self.assertGreaterEqual(fit["gradient_norm"], 0.0)
        self.assertEqual(
            fit["converged"],
            fit["gradient_norm"] <= fit["convergence"]["projected_gradient_tolerance"],
        )
        self.assertEqual(
            fit["cross_check"]["method"],
            "conditional_rho_gradient_bisection",
        )
        full_cross_check = fit["cross_check"]["full_parameter"]
        self.assertEqual(
            full_cross_check["method"],
            "full_parameter_projected_gradient_armijo",
        )
        self.assertEqual(
            full_cross_check["initialization"],
            "shared_deterministic_baseline",
        )
        self.assertLessEqual(
            full_cross_check["objective"],
            full_cross_check["initial_objective"],
        )
        self.assertGreaterEqual(full_cross_check["iterations"], 1)
        self.assertGreaterEqual(full_cross_check["backtracking_evaluations"], 1)
        self.assertGreaterEqual(full_cross_check["gradient_norm"], 0.0)
        self.assertGreaterEqual(
            full_cross_check["maximum_absolute_parameter_delta"], 0.0
        )
        self.assertGreaterEqual(full_cross_check["l2_parameter_delta"], 0.0)
        self.assertGreater(
            full_cross_check["adoption_minimum_objective_improvement"], 0.0
        )
        self.assertIs(full_cross_check["adoption_requires_convergence"], True)
        self.assertEqual(
            full_cross_check["converged"],
            full_cross_check["gradient_norm"]
            <= fit["convergence"]["projected_gradient_tolerance"],
        )
        if full_cross_check["adopted"]:
            self.assertEqual(
                fit["cross_check"]["selected_solver"],
                "full_parameter_projected_gradient_armijo",
            )
            self.assertAlmostEqual(
                fit["final_objective"], full_cross_check["objective"]
            )
        else:
            self.assertTrue(
                not full_cross_check["converged"]
                or full_cross_check["objective"]
                >= fit["cross_check"]["primary_objective"]
                - full_cross_check["adoption_minimum_objective_improvement"]
            )
        self.assertEqual(
            fit["cross_check"]["legacy_grid"]["step"],
            model["config"]["rho_grid"]["step"],
        )
        self.assertTrue(
            math.isfinite(
                fit["cross_check"]["legacy_grid"]["objective_minus_bisection"]
            )
        )

        parameters = model["parameters"]
        for home in parameters["attack"]:
            for away in parameters["attack"]:
                if home == away:
                    continue
                home_rate, away_rate, _ = score_model.expected_rates(model, home, away)
                for home_goals, away_goals in ((0, 0), (0, 1), (1, 0), (1, 1)):
                    self.assertGreater(
                        score_model._dc_tau(
                            home_goals,
                            away_goals,
                            home_rate,
                            away_rate,
                            parameters["rho"],
                        ),
                        0.0,
                    )

        # Frozen pre-upgrade artifacts did not contain optimizer diagnostics.
        legacy = copy.deepcopy(model)
        legacy["fit"] = {
            "objective": "weighted_negative_log_likelihood",
            "poisson_nll": fit["poisson_nll"],
            "dixon_coles_nll": fit["dixon_coles_nll"],
            "optimizer": "deterministic_adam_then_rho_grid",
        }
        legacy["model_hash"] = score_model.calculate_model_hash(legacy)
        score_model.validate_model(legacy)

        tampered = copy.deepcopy(model)
        tampered["fit"]["gradient_norm"] = -1.0
        tampered["model_hash"] = score_model.calculate_model_hash(tampered)
        with self.assertRaisesRegex(score_model.ScoreModelError, "gradient_norm"):
            score_model.validate_model(tampered)

        tampered = copy.deepcopy(model)
        tampered["fit"]["converged"] = not tampered["fit"]["converged"]
        tampered["model_hash"] = score_model.calculate_model_hash(tampered)
        with self.assertRaisesRegex(score_model.ScoreModelError, "converged conflicts"):
            score_model.validate_model(tampered)

        tampered = copy.deepcopy(model)
        tampered["fit"]["cross_check"]["legacy_grid"]["objective_minus_bisection"] += (
            0.1
        )
        tampered["model_hash"] = score_model.calculate_model_hash(tampered)
        with self.assertRaisesRegex(score_model.ScoreModelError, "legacy_grid"):
            score_model.validate_model(tampered)

        tampered = copy.deepcopy(model)
        tampered["fit"]["cross_check"]["full_parameter"][
            "objective_delta_vs_primary"
        ] += 0.1
        tampered["model_hash"] = score_model.calculate_model_hash(tampered)
        with self.assertRaisesRegex(score_model.ScoreModelError, "full-parameter"):
            score_model.validate_model(tampered)

    def test_joint_dixon_coles_analytic_gradient_matches_finite_difference(self):
        records = score_model.load_training_csv(self.csv_path)
        teams = sorted(
            {row["home_team"] for row in records}
            | {row["away_team"] for row in records}
        )
        values = [0.1, 0.08] + [0.01 * (index - 3) for index in range(8)]
        weights = [1.0 - index * 0.02 for index in range(len(records))]
        rho = -0.04
        regularization = 0.03
        _, gradient, rho_gradient = score_model._dc_nll_and_gradient(
            records, weights, teams, values, rho, regularization
        )
        epsilon = 1e-6
        for index in range(len(values)):
            lower = list(values)
            upper = list(values)
            lower[index] -= epsilon
            upper[index] += epsilon
            lower_loss = score_model._dc_nll_and_gradient(
                records, weights, teams, lower, rho, regularization
            )[0]
            upper_loss = score_model._dc_nll_and_gradient(
                records, weights, teams, upper, rho, regularization
            )[0]
            numeric = (upper_loss - lower_loss) / (2.0 * epsilon)
            self.assertAlmostEqual(gradient[index], numeric, places=6)
        lower_loss = score_model._dc_nll_and_gradient(
            records, weights, teams, values, rho - epsilon, regularization
        )[0]
        upper_loss = score_model._dc_nll_and_gradient(
            records, weights, teams, values, rho + epsilon, regularization
        )[0]
        numeric_rho = (upper_loss - lower_loss) / (2.0 * epsilon)
        self.assertAlmostEqual(rho_gradient, numeric_rho, places=6)

    def test_unfinished_full_parameter_cross_check_cannot_replace_primary(self):
        model = score_model.fit_model(
            self.csv_path,
            iterations=1,
            learning_rate=0.025,
            regularization=0.03,
            half_life_days=180.0,
        )
        fit = model["fit"]
        full_cross_check = fit["cross_check"]["full_parameter"]
        self.assertFalse(full_cross_check["converged"])
        self.assertFalse(full_cross_check["adopted"])
        self.assertNotEqual(
            fit["cross_check"]["selected_solver"],
            "full_parameter_projected_gradient_armijo",
        )
        self.assertEqual(
            fit["converged"],
            fit["gradient_norm"] <= fit["convergence"]["projected_gradient_tolerance"],
        )

    def test_prediction_matrix_is_normalized_and_all_markets_are_consistent(self):
        prediction = score_model.predict_model(
            self._fit(),
            "Alpha",
            "Bravo",
            kickoff=PREDICTION_KICKOFF,
            generated_at=PREDICTION_GENERATED_AT,
            total_markets=[("over", 2.5), ("under", 2.5)],
            asian_handicaps=[("home", -0.5), ("away", 0.5)],
        )
        matrix = prediction["score_matrix"]["probabilities"]
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.0, places=12)
        self.assertTrue(prediction["tail_mass"]["tolerance_met"])
        self.assertLessEqual(
            prediction["tail_mass"]["raw_omitted_probability"],
            prediction["tail_mass"]["tolerance"],
        )

        one_x_two = prediction["one_x_two"]
        self.assertAlmostEqual(sum(one_x_two.values()), 1.0, places=12)
        self.assertAlmostEqual(
            one_x_two["home"],
            prediction["asian_handicaps"]["home_-0.5"]["probabilities"]["full_win"],
            places=12,
        )
        self.assertAlmostEqual(
            one_x_two["away"] + one_x_two["draw"],
            prediction["asian_handicaps"]["away_+0.5"]["probabilities"]["full_win"],
            places=12,
        )

        over = prediction["totals"]["over_+2.5"]["probabilities"]
        under = prediction["totals"]["under_+2.5"]["probabilities"]
        self.assertAlmostEqual(over["full_win"] + under["full_win"], 1.0, places=12)
        self.assertAlmostEqual(sum(prediction["btts"].values()), 1.0, places=12)
        self.assertAlmostEqual(sum(prediction["goal_ranges"].values()), 1.0, places=12)
        self.assertEqual(
            prediction["exact_scores"]["top_two"][0]["score"],
            prediction["exact_scores"]["ranked"][0]["score"],
        )
        self.assertAlmostEqual(
            prediction["exact_scores"]["zero_zero_audit"]["probability"],
            matrix[0][0],
            places=15,
        )

    def test_prediction_requires_strict_timezone_aware_forward_cutoff(self):
        model = self._fit()
        prediction = score_model.predict_model(
            model,
            "Alpha",
            "Bravo",
            kickoff="2099-01-02T09:00:00+09:00",
            generated_at="2099-01-01T23:00:00Z",
        )
        self.assertEqual(prediction["fixture"]["kickoff"], "2099-01-02T00:00:00Z")
        self.assertEqual(prediction["generated_at"], "2099-01-01T23:00:00Z")
        self.assertEqual(prediction["provenance"]["training_cutoff_date"], "2025-03-22")
        self.assertEqual(
            prediction["provenance"]["model_schema_version"],
            model["schema_version"],
        )
        self.assertEqual(
            prediction["provenance"]["training"],
            {
                "source_data_hash": model["training"]["source_data_hash"],
                "match_count": model["training"]["match_count"],
                "start_date": model["training"]["start_date"],
                "end_date": model["training"]["end_date"],
            },
        )

        with self.assertRaisesRegex(score_model.ScoreModelError, "explicit UTC offset"):
            score_model.predict_model(
                model,
                "Alpha",
                "Bravo",
                kickoff="2099-01-02T09:00:00",
                generated_at="2099-01-01T23:00:00Z",
            )
        with self.assertRaisesRegex(score_model.ScoreModelError, "before kickoff"):
            score_model.predict_model(
                model,
                "Alpha",
                "Bravo",
                kickoff="2099-01-02T00:00:00Z",
                generated_at="2099-01-02T00:00:00Z",
            )
        with self.assertRaisesRegex(score_model.ScoreModelError, "end_date"):
            score_model.predict_model(
                model,
                "Alpha",
                "Bravo",
                kickoff="2025-03-22T23:59:59Z",
                generated_at="2025-03-21T00:00:00Z",
            )

        future_generated_model = copy.deepcopy(model)
        future_generated_model["generated_at"] = "2099-01-01T00:00:00Z"
        with self.assertRaisesRegex(score_model.ScoreModelError, "model.generated_at"):
            score_model.predict_model(
                future_generated_model,
                "Alpha",
                "Bravo",
                kickoff="2099-01-02T12:00:00Z",
                generated_at="2098-12-31T23:59:59Z",
            )

    def test_rho_grid_rejects_invalid_unobserved_low_score_cells(self):
        records = [
            {
                "home_team": "Alpha",
                "away_team": "Bravo",
                "home_goals": 0,
                "away_goals": 1,
            },
            {
                "home_team": "Bravo",
                "away_team": "Alpha",
                "home_goals": 3,
                "away_goals": 3,
            },
        ]
        # Both fixtures have lambda_home=lambda_away=3.  The 0-1 observation
        # makes the old observed-cell-only search choose rho=0.2 even though
        # tau(0,0)=1-3*3*0.2=-0.8.
        values = [math.log(3.0), 0.0, 0.0, 0.0, 0.0, 0.0]
        rho, _ = score_model._select_rho(
            records,
            [1.0, 1.0],
            ["Alpha", "Bravo"],
            values,
            rho_min=-0.2,
            rho_max=0.2,
            rho_step=0.01,
        )
        self.assertLess(rho, 1.0 / 9.0)
        matrix, tail = score_model.build_score_matrix(3.0, 3.0, rho)
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.0, places=12)
        self.assertTrue(tail["tolerance_met"])

    def test_rho_grid_is_safe_for_unseen_known_team_pairings(self):
        records = [
            {
                "home_team": "Alpha",
                "away_team": "Bravo",
                "home_goals": 0,
                "away_goals": 1,
            },
            {
                "home_team": "Charlie",
                "away_team": "Bravo",
                "home_goals": 0,
                "away_goals": 1,
            },
        ]
        # Observed Alpha-Bravo and Charlie-Bravo both have rates 1/1, so rho
        # 0.2 is locally safe.  The unseen known pairing Alpha-Charlie has both
        # rates 2.5, making its 0-0 correction negative at rho 0.2.
        strength = math.log(2.5) / 2.0
        values = [
            0.0,
            0.0,
            strength,
            -strength,
            strength,
            strength,
            -strength,
            strength,
        ]
        unseen_rate = 2.5
        self.assertLess(
            score_model._dc_tau(0, 0, unseen_rate, unseen_rate, 0.2),
            0.0,
        )
        rho, _ = score_model._select_rho(
            records,
            [1.0, 1.0],
            ["Alpha", "Bravo", "Charlie"],
            values,
            rho_min=-0.2,
            rho_max=0.2,
            rho_step=0.01,
        )
        self.assertGreater(
            score_model._dc_tau(0, 0, unseen_rate, unseen_rate, rho),
            0.0,
        )
        matrix, tail = score_model.build_score_matrix(unseen_rate, unseen_rate, rho)
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.0, places=12)
        self.assertTrue(tail["tolerance_met"])

    def test_quarter_total_has_only_reachable_five_state_probabilities(self):
        matrix, _ = score_model.build_score_matrix(1.55, 1.10, -0.08)
        over_225 = score_model.aggregate_total(matrix, "over", 2.25)
        probabilities = over_225["probabilities"]
        self.assertEqual(over_225["split_lines"], [2.0, 2.5])
        self.assertGreater(probabilities["full_win"], 0.0)
        self.assertEqual(probabilities["half_win"], 0.0)
        self.assertEqual(probabilities["push"], 0.0)
        self.assertGreater(probabilities["half_loss"], 0.0)
        self.assertGreater(probabilities["full_loss"], 0.0)
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=12)

        under_275 = score_model.aggregate_total(matrix, "under", 2.75)
        probabilities = under_275["probabilities"]
        self.assertEqual(under_275["split_lines"], [2.5, 3.0])
        self.assertEqual(probabilities["half_win"], 0.0)
        self.assertEqual(probabilities["push"], 0.0)
        self.assertGreater(probabilities["half_loss"], 0.0)

        over_275 = score_model.aggregate_total(matrix, "over", 2.75)
        self.assertGreater(over_275["probabilities"]["half_win"], 0.0)
        self.assertEqual(over_275["probabilities"]["half_loss"], 0.0)

    def test_quarter_asian_handicap_maps_margin_to_half_win_and_half_loss(self):
        matrix, _ = score_model.build_score_matrix(1.7, 1.0, -0.05)
        home_minus_075 = score_model.aggregate_asian_handicap(matrix, "home", -0.75)
        probabilities = home_minus_075["probabilities"]
        self.assertEqual(home_minus_075["split_lines"], [-1.0, -0.5])
        self.assertGreater(probabilities["full_win"], 0.0)
        self.assertGreater(probabilities["half_win"], 0.0)
        self.assertEqual(probabilities["push"], 0.0)
        self.assertEqual(probabilities["half_loss"], 0.0)
        self.assertGreater(probabilities["full_loss"], 0.0)

        away_plus_025 = score_model.aggregate_asian_handicap(matrix, "away", 0.25)
        probabilities = away_plus_025["probabilities"]
        self.assertGreater(probabilities["half_win"], 0.0)
        self.assertEqual(probabilities["push"], 0.0)
        self.assertEqual(probabilities["half_loss"], 0.0)

        home_minus_025 = score_model.aggregate_asian_handicap(matrix, "home", -0.25)
        probabilities = home_minus_025["probabilities"]
        self.assertEqual(probabilities["half_win"], 0.0)
        self.assertEqual(probabilities["push"], 0.0)
        self.assertGreater(probabilities["half_loss"], 0.0)

    def test_integer_and_half_lines_have_only_reachable_states(self):
        matrix, _ = score_model.build_score_matrix(1.4, 1.2, 0.0)
        integer_total = score_model.aggregate_total(matrix, "over", 2.0)[
            "probabilities"
        ]
        self.assertGreater(integer_total["push"], 0.0)
        self.assertEqual(integer_total["half_win"], 0.0)
        self.assertEqual(integer_total["half_loss"], 0.0)

        half_total = score_model.aggregate_total(matrix, "under", 2.5)["probabilities"]
        self.assertEqual(half_total["push"], 0.0)
        self.assertEqual(half_total["half_win"], 0.0)
        self.assertEqual(half_total["half_loss"], 0.0)

    def test_unknown_team_is_rejected_unless_fallback_is_explicit(self):
        model = self._fit()
        with self.assertRaisesRegex(score_model.ScoreModelError, "unknown team"):
            score_model.predict_model(
                model,
                "New Club",
                "Bravo",
                kickoff=PREDICTION_KICKOFF,
                generated_at=PREDICTION_GENERATED_AT,
            )

        prediction = score_model.predict_model(
            model,
            "New Club",
            "Bravo",
            kickoff=PREDICTION_KICKOFF,
            generated_at=PREDICTION_GENERATED_AT,
            unknown_team_policy="league_average",
        )
        self.assertTrue(prediction["warnings"])
        self.assertEqual(prediction["fixture"]["unknown_team_policy"], "league_average")

    def test_rejects_nonfinite_negative_and_tampered_inputs(self):
        with self.assertRaises(score_model.ScoreModelError):
            score_model.build_score_matrix(math.nan, 1.0, 0.0)
        with self.assertRaises(score_model.ScoreModelError):
            score_model.build_score_matrix(-0.1, 1.0, 0.0)
        with self.assertRaises(score_model.ScoreModelError):
            score_model.aggregate_total([[1.0]], "over", 2.1)

        tampered = copy.deepcopy(self._fit())
        tampered["parameters"]["attack"]["Alpha"] = float("inf")
        with self.assertRaises(score_model.ScoreModelError):
            score_model.validate_model(tampered)

        tampered = copy.deepcopy(self._fit())
        tampered["parameters"]["attack"]["Alpha"] += 0.01
        with self.assertRaisesRegex(score_model.ScoreModelError, "model_hash"):
            score_model.validate_model(tampered)

    def test_csv_validation_rejects_negative_goals_and_naive_datetime(self):
        bad = self.base / "bad.csv"
        self._write_csv(
            bad,
            [
                ("2025-01-01T18:00:00", "Alpha", "Bravo", 1, 0),
                ("2025-01-02", "Bravo", "Alpha", -1, 0),
            ],
        )
        with self.assertRaises(score_model.ScoreModelError):
            score_model.fit_model(bad, iterations=5)

    def test_csv_rejects_duplicate_and_conflicting_fixtures(self):
        duplicate = self.base / "duplicate.csv"
        self._write_csv(
            duplicate,
            [
                ("2025-01-01", "Alpha", "Bravo", 1, 0),
                ("2025-01-01", "Alpha", "Bravo", 1, 0),
            ],
        )
        with self.assertRaisesRegex(score_model.ScoreModelError, "duplicate score"):
            score_model.load_training_csv(duplicate)

        conflict = self.base / "conflict.csv"
        self._write_csv(
            conflict,
            [
                ("2025-01-01", "Alpha", "Bravo", 1, 0),
                ("2025-01-01", "Alpha", "Bravo", 2, 0),
            ],
        )
        with self.assertRaisesRegex(score_model.ScoreModelError, "conflicting score"):
            score_model.load_training_csv(conflict)

    def test_csv_rejects_disconnected_fixture_graph(self):
        disconnected = self.base / "disconnected.csv"
        self._write_csv(
            disconnected,
            [
                ("2025-01-01", "Alpha", "Bravo", 1, 0),
                ("2025-01-02", "Bravo", "Alpha", 0, 0),
                ("2025-01-03", "Charlie", "Delta", 1, 1),
                ("2025-01-04", "Delta", "Charlie", 2, 1),
            ],
        )
        with self.assertRaisesRegex(score_model.ScoreModelError, "disconnected"):
            score_model.load_training_csv(disconnected)

    def test_model_validation_checks_training_and_config_metadata(self):
        model = self._fit()

        invalid_window = copy.deepcopy(model)
        invalid_window["training"]["start_date"] = "2025-12-31"
        with self.assertRaisesRegex(score_model.ScoreModelError, "start_date"):
            score_model.validate_model(invalid_window)

        invalid_config = copy.deepcopy(model)
        invalid_config["config"]["half_life_days"] = float("nan")
        with self.assertRaisesRegex(score_model.ScoreModelError, "finite"):
            score_model.validate_model(invalid_config)

        invalid_count = copy.deepcopy(model)
        invalid_count["training"]["team_count"] += 1
        with self.assertRaisesRegex(score_model.ScoreModelError, "team_count"):
            score_model.validate_model(invalid_count)

        invalid_time = copy.deepcopy(model)
        invalid_time["generated_at"] = "2026-01-01T00:00:00"
        with self.assertRaisesRegex(score_model.ScoreModelError, "explicit UTC offset"):
            score_model.validate_model(invalid_time)

    def test_large_tail_is_rejected_or_explicitly_reported(self):
        with self.assertRaisesRegex(score_model.ScoreModelError, "tail exceeds"):
            score_model.build_score_matrix(
                9.0,
                8.0,
                0.0,
                max_goals=2,
                hard_max_goals=2,
                tail_tolerance=1e-8,
            )

        matrix, tail = score_model.build_score_matrix(
            9.0,
            8.0,
            0.0,
            max_goals=2,
            hard_max_goals=2,
            tail_tolerance=1e-8,
            allow_large_tail=True,
        )
        self.assertFalse(tail["tolerance_met"])
        self.assertGreater(tail["raw_omitted_probability"], 0.5)
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.0, places=12)

    def test_walk_forward_backtest_is_deterministic_and_has_strict_cutoffs(self):
        arguments = {
            "min_train_matches": 6,
            "test_block_size": 2,
            "iterations": 35,
            "learning_rate": 0.025,
            "regularization": 0.03,
            "half_life_days": 180.0,
            "max_goals": 8,
        }
        first = score_model.backtest_model(self.csv_path, **arguments)
        second = score_model.backtest_model(self.csv_path, **arguments)
        self.assertEqual(first, second)
        self.assertEqual(first["artifact_type"], "soccer_score_backtest")
        self.assertFalse(first["split_policy"]["random_split"])
        self.assertFalse(first["split_policy"]["same_date_split_allowed"])
        self.assertEqual(first["metrics"]["sample_count"], 6)
        self.assertEqual(len(first["predictions"]), 6)

        for prediction in first["predictions"]:
            self.assertLess(prediction["training_cutoff_date"], prediction["date"])
            self.assertTrue(prediction["model_hash"].startswith("sha256:"))
            self.assertTrue(
                prediction["prediction"]["score_matrix_hash"].startswith("sha256:")
            )
        for name, value in first["metrics"].items():
            if name not in {"sample_count", "definitions"}:
                self.assertTrue(math.isfinite(value), name)

    def test_walk_forward_never_splits_matches_from_the_same_date(self):
        grouped_rows = list(SAMPLE_ROWS)
        grouped_rows[7] = (
            grouped_rows[6][0],
            grouped_rows[7][1],
            grouped_rows[7][2],
            grouped_rows[7][3],
            grouped_rows[7][4],
        )
        grouped_path = self.base / "grouped-dates.csv"
        self._write_csv(grouped_path, grouped_rows)
        artifact = score_model.backtest_model(
            grouped_path,
            min_train_matches=6,
            test_block_size=1,
            iterations=20,
        )
        first_block = artifact["blocks"][0]
        self.assertEqual(first_block["test_start_date"], "2025-02-15")
        self.assertEqual(first_block["test_end_date"], "2025-02-15")
        self.assertEqual(first_block["test_match_count"], 2)
        first_block_rows = [row for row in artifact["predictions"] if row["block"] == 1]
        self.assertEqual(len(first_block_rows), 2)
        self.assertEqual({row["date"] for row in first_block_rows}, {"2025-02-15"})

    def test_cli_fit_and_predict_write_auditable_artifacts(self):
        model_path = self.base / "model.json"
        prediction_path = self.base / "prediction.json"
        backtest_path = self.base / "backtest.json"
        fit = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "fit",
                "--input",
                str(self.csv_path),
                "--output",
                str(model_path),
                "--iterations",
                "100",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(fit.returncode, 0, fit.stderr)
        model = json.loads(model_path.read_text(encoding="utf-8"))
        self.assertEqual(model["artifact_type"], "soccer_score_model")
        self.assertTrue(model["model_hash"].startswith("sha256:"))

        predict = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "predict",
                "--model",
                str(model_path),
                "--home-team",
                "Alpha",
                "--away-team",
                "Bravo",
                "--kickoff",
                PREDICTION_KICKOFF,
                "--total",
                "over:2.25",
                "--asian",
                "home:-0.75",
                "--output",
                str(prediction_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(predict.returncode, 0, predict.stderr)
        prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
        self.assertEqual(prediction["model_hash"], model["model_hash"])
        self.assertIn("over_+2.25", prediction["totals"])
        self.assertIn("home_-0.75", prediction["asian_handicaps"])
        self.assertEqual(
            prediction["score_matrix"]["home_goals_max"],
            prediction["tail_mass"]["truncated_at_home_goals"],
        )
        self.assertEqual(prediction["fixture"]["kickoff"], PREDICTION_KICKOFF)

        backtest = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "backtest",
                "--input",
                str(self.csv_path),
                "--output",
                str(backtest_path),
                "--min-train-matches",
                "6",
                "--test-block-size",
                "3",
                "--iterations",
                "25",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(backtest.returncode, 0, backtest.stderr)
        backtest_artifact = json.loads(backtest_path.read_text(encoding="utf-8"))
        self.assertEqual(backtest_artifact["metrics"]["sample_count"], 6)
        self.assertTrue(backtest_artifact["backtest_hash"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()

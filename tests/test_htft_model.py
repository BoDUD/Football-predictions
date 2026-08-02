from __future__ import annotations

import copy
import csv
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "htft_model.py"
SPEC = importlib.util.spec_from_file_location("soccer_htft_model", SCRIPT)
assert SPEC and SPEC.loader
htft_model = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(htft_model)

RANKER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "htft_ranker.py"
RANKER_SPEC = importlib.util.spec_from_file_location("soccer_htft_ranker", RANKER_SCRIPT)
assert RANKER_SPEC and RANKER_SPEC.loader
htft_ranker = importlib.util.module_from_spec(RANKER_SPEC)
RANKER_SPEC.loader.exec_module(htft_ranker)


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
PREDICTION_GENERATED_AT = "2098-12-31T00:00:00Z"
PREDICTION_KICKOFF = "2099-01-01T12:00:00Z"
MANIFEST_HASH = "sha256:" + "a" * 64


class HTFTModelTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.csv_path = self.base / "matches.csv"
        self._write_csv(self.csv_path, SAMPLE_ROWS)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write_csv(path: Path, rows) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
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
            writer.writerows(rows)

    def _fit(self):
        return htft_model.fit_model(
            self.csv_path,
            iterations=30,
            learning_rate=0.025,
            regularization=0.03,
            half_time_half_life_days=180.0,
            second_half_half_life_days=180.0,
            full_time_half_life_days=180.0,
            competition_key="test_league",
            dataset_manifest_hash=MANIFEST_HASH,
        )

    @staticmethod
    def _refresh_prediction_htft_mirrors(prediction):
        matrix = prediction["htft"]["joint_matrix"]
        probabilities = {
            f"{half}_{full}": matrix[row_index][column_index]
            for row_index, half in enumerate(htft_model.RESULTS)
            for column_index, full in enumerate(htft_model.RESULTS)
        }
        code_probabilities = {
            htft_model.RESULT_CODES[half] + htft_model.RESULT_CODES[full]: (
                matrix[row_index][column_index]
            )
            for row_index, half in enumerate(htft_model.RESULTS)
            for column_index, full in enumerate(htft_model.RESULTS)
        }
        half_marginal, full_marginal = htft_model._matrix_marginals(matrix)
        class_index = {
            name: index for index, name in enumerate(htft_model.HTFT_CLASSES)
        }
        ranked_values = sorted(
            probabilities.items(), key=lambda item: (-item[1], class_index[item[0]])
        )
        ranked = [
            {
                "class": class_name,
                "code": (
                    htft_model.RESULT_CODES[class_name.split("_", 1)[0]]
                    + htft_model.RESULT_CODES[class_name.split("_", 1)[1]]
                ),
                "probability": probability,
            }
            for class_name, probability in ranked_values
        ]
        prediction["htft"].update(
            {
                "probabilities": probabilities,
                "code_probabilities": code_probabilities,
                "half_time_marginal": half_marginal,
                "full_time_marginal": full_marginal,
                "half_time_code_probabilities": {
                    htft_model.RESULT_CODES[result]: half_marginal[result]
                    for result in htft_model.RESULTS
                },
                "full_time_code_probabilities": {
                    htft_model.RESULT_CODES[result]: full_marginal[result]
                    for result in htft_model.RESULTS
                },
                "ranked": ranked,
                "top_one": ranked[0],
                "top_two": ranked[:2],
            }
        )

    def test_fit_is_deterministic_and_contains_three_verified_components(self):
        first = self._fit()
        second = self._fit()
        reversed_path = self.base / "reversed.csv"
        self._write_csv(reversed_path, reversed(SAMPLE_ROWS))
        reordered = htft_model.fit_model(
            reversed_path,
            iterations=30,
            learning_rate=0.025,
            regularization=0.03,
            half_time_half_life_days=180.0,
            second_half_half_life_days=180.0,
            full_time_half_life_days=180.0,
            competition_key="test_league",
            dataset_manifest_hash=MANIFEST_HASH,
        )

        self.assertEqual(first["model_hash"], second["model_hash"])
        self.assertEqual(first["model_hash"], reordered["model_hash"])
        self.assertEqual(
            set(first["components"]), {"half_time", "second_half", "full_time"}
        )
        self.assertEqual(first["training"]["match_count"], len(SAMPLE_ROWS))
        self.assertEqual(first["training"]["scope"], "single_league")
        self.assertEqual(first["training"]["competition_key"], "test_league")
        self.assertEqual(first["training"]["dataset_manifest_hash"], MANIFEST_HASH)
        self.assertEqual(first["empirical_association"]["smoothing_alpha"], 0.5)
        self.assertEqual(first["empirical_association"]["power"], 1.0)
        self.assertEqual(
            first["construction"]["default_seed"], "empirical_association_ipf"
        )
        htft_model.validate_model(first)

        tampered = copy.deepcopy(first)
        tampered["components"]["half_time"]["parameters"]["rho"] += 0.001
        with self.assertRaisesRegex(htft_model.HTFTModelError, "component"):
            htft_model.validate_model(tampered)

        tampered = copy.deepcopy(first)
        tampered["empirical_association"]["lift"][0][0] += 0.01
        tampered["model_hash"] = htft_model.calculate_model_hash(tampered)
        with self.assertRaisesRegex(htft_model.HTFTModelError, "lift"):
            htft_model.validate_model(tampered)

        tampered = copy.deepcopy(first)
        tampered["empirical_association"]["seed_joint"][0][0] += 0.001
        tampered["empirical_association"]["seed_joint"][2][2] -= 0.001
        tampered["model_hash"] = htft_model.calculate_model_hash(tampered)
        with self.assertRaisesRegex(htft_model.HTFTModelError, "counts"):
            htft_model.validate_model(tampered)

    def test_prediction_joint_is_normalized_and_matches_both_marginals(self):
        model = self._fit()
        prediction = htft_model.predict_model(
            model,
            "Alpha",
            "Bravo",
            kickoff=PREDICTION_KICKOFF,
            generated_at=PREDICTION_GENERATED_AT,
        )
        matrix = prediction["htft"]["joint_matrix"]
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.0, places=12)
        self.assertEqual(
            set(prediction["htft"]["probabilities"]),
            set(htft_model.HTFT_CLASSES),
        )
        self.assertEqual(len(prediction["htft"]["ranked"]), 9)
        self.assertEqual(len(prediction["htft"]["top_two"]), 2)
        self.assertEqual(
            set(prediction["htft"]["code_probabilities"]),
            {"HH", "HD", "HA", "DH", "DD", "DA", "AH", "AD", "AA"},
        )
        self.assertTrue(all("code" in item for item in prediction["htft"]["top_two"]))
        self.assertFalse(prediction["provenance"]["external_anchor_enabled"])
        self.assertEqual(
            prediction["joint_construction"]["seed_method"],
            "empirical_association",
        )
        self.assertEqual(
            prediction["provenance"]["training"]["dataset_manifest_hash"],
            MANIFEST_HASH,
        )

        half_target = prediction["components"]["half_time"]["one_x_two"]
        full_target = prediction["components"]["full_time"]["one_x_two"]
        for result in htft_model.RESULTS:
            self.assertAlmostEqual(
                prediction["htft"]["half_time_marginal"][result],
                half_target[result],
                places=10,
            )
            self.assertAlmostEqual(
                prediction["htft"]["full_time_marginal"][result],
                full_target[result],
                places=10,
            )
        self.assertLessEqual(
            prediction["joint_construction"]["ipf"]["maximum_marginal_error"],
            prediction["joint_construction"]["ipf"]["tolerance"],
        )
        htft_model.validate_prediction(prediction, model=model)

        tampered = copy.deepcopy(prediction)
        tampered["htft"]["probabilities"]["home_home"] += 0.01
        with self.assertRaises(htft_model.HTFTModelError):
            htft_model.validate_prediction(tampered)

        tampered = copy.deepcopy(prediction)
        tampered["htft"]["full_time_marginal"] = {
            "home": 0.34,
            "draw": 0.33,
            "away": 0.33,
        }
        tampered["prediction_hash"] = htft_model.calculate_prediction_hash(tampered)
        with self.assertRaisesRegex(htft_model.HTFTModelError, "matrix columns"):
            htft_model.validate_prediction(tampered)

        tampered = copy.deepcopy(prediction)
        tampered["htft"]["top_two"] = list(reversed(tampered["htft"]["top_two"]))
        tampered["prediction_hash"] = htft_model.calculate_prediction_hash(tampered)
        with self.assertRaisesRegex(htft_model.HTFTModelError, "Top-1/Top-2"):
            htft_model.validate_prediction(tampered)

    def test_prediction_rebuild_rejects_rehashed_joint_with_same_marginals(self):
        model = self._fit()
        prediction = htft_model.predict_model(
            model,
            "Alpha",
            "Bravo",
            kickoff=PREDICTION_KICKOFF,
            generated_at=PREDICTION_GENERATED_AT,
        )
        tampered = copy.deepcopy(prediction)
        matrix = tampered["htft"]["joint_matrix"]
        delta = min(matrix[0][1], matrix[1][0]) / 10.0
        matrix[0][0] += delta
        matrix[0][1] -= delta
        matrix[1][0] -= delta
        matrix[1][1] += delta
        self._refresh_prediction_htft_mirrors(tampered)
        tampered["prediction_hash"] = htft_model.calculate_prediction_hash(tampered)

        with self.assertRaisesRegex(
            htft_model.HTFTModelError, "IPF reconstruction"
        ):
            htft_model.validate_prediction(tampered)

    def test_prediction_model_binding_rejects_rehashed_association_seed(self):
        model = self._fit()
        prediction = htft_model.predict_model(
            model,
            "Alpha",
            "Bravo",
            kickoff=PREDICTION_KICKOFF,
            generated_at=PREDICTION_GENERATED_AT,
        )
        tampered = copy.deepcopy(prediction)
        raw_joint = tampered["joint_construction"]["raw_joint"]
        delta = min(raw_joint[0][1], raw_joint[1][0]) / 10.0
        raw_joint[0][0] += delta
        raw_joint[0][1] -= delta
        raw_joint[1][0] -= delta
        raw_joint[1][1] += delta
        rebuilt, audit = htft_model.iterative_proportional_fit(
            raw_joint,
            tampered["htft"]["half_time_marginal"],
            tampered["htft"]["full_time_marginal"],
            tolerance=tampered["joint_construction"]["ipf"]["tolerance"],
            max_iterations=tampered["joint_construction"]["ipf"]["max_iterations"],
        )
        audit["max_iterations"] = tampered["joint_construction"]["ipf"][
            "max_iterations"
        ]
        tampered["joint_construction"]["ipf"] = audit
        tampered["htft"]["joint_matrix"] = rebuilt
        self._refresh_prediction_htft_mirrors(tampered)
        tampered["prediction_hash"] = htft_model.calculate_prediction_hash(tampered)

        htft_model.validate_prediction(tampered)
        with self.assertRaisesRegex(
            htft_model.HTFTModelError, "association seed"
        ):
            htft_model.validate_prediction(tampered, model=model)

    def test_external_de_vigged_anchors_are_opt_in_complete_and_audited(self):
        half_anchor = {
            "probabilities": {"home": 0.40, "draw": 0.35, "away": 0.25},
            "source": "verified pre-kickoff consensus",
            "captured_at": "2098-12-30T23:50:00Z",
            "de_vigged": True,
        }
        full_anchor = {
            "probabilities": {"home": 0.50, "draw": 0.30, "away": 0.20},
            "source": "verified pre-kickoff consensus",
            "captured_at": "2098-12-30T23:50:00Z",
            "de_vigged": True,
        }
        prediction = htft_model.predict_model(
            self._fit(),
            "Alpha",
            "Bravo",
            kickoff=PREDICTION_KICKOFF,
            generated_at=PREDICTION_GENERATED_AT,
            half_time_anchor=half_anchor,
            full_time_anchor=full_anchor,
        )
        self.assertTrue(prediction["provenance"]["external_anchor_enabled"])
        self.assertEqual(
            prediction["provenance"]["marginal_targets"]["half_time"]["origin"],
            "external_de_vigged_anchor",
        )
        for result in htft_model.RESULTS:
            self.assertAlmostEqual(
                prediction["htft"]["half_time_marginal"][result],
                half_anchor["probabilities"][result],
                places=10,
            )
            self.assertAlmostEqual(
                prediction["htft"]["full_time_marginal"][result],
                full_anchor["probabilities"][result],
                places=10,
            )

        incomplete = copy.deepcopy(half_anchor)
        del incomplete["probabilities"]["away"]
        with self.assertRaisesRegex(htft_model.HTFTModelError, "exactly"):
            htft_model.predict_model(
                self._fit(),
                "Alpha",
                "Bravo",
                kickoff=PREDICTION_KICKOFF,
                generated_at=PREDICTION_GENERATED_AT,
                half_time_anchor=incomplete,
            )
        not_devigged = copy.deepcopy(half_anchor)
        not_devigged["de_vigged"] = False
        with self.assertRaisesRegex(htft_model.HTFTModelError, "de_vigged"):
            htft_model.predict_model(
                self._fit(),
                "Alpha",
                "Bravo",
                kickoff=PREDICTION_KICKOFF,
                generated_at=PREDICTION_GENERATED_AT,
                half_time_anchor=not_devigged,
            )

    def test_code_payload_is_directly_accepted_by_existing_ranker(self):
        prediction = htft_model.predict_model(
            self._fit(),
            "Alpha",
            "Bravo",
            kickoff=PREDICTION_KICKOFF,
            generated_at=PREDICTION_GENERATED_AT,
        )
        payload = prediction["htft"]
        ranked = htft_ranker.rank_htft(
            payload["code_probabilities"],
            payload["half_time_code_probabilities"],
            payload["full_time_code_probabilities"],
        )
        self.assertTrue(ranked["marginal_validation"]["passed"])

    def test_cutoff_and_unknown_team_fallback_are_explicit(self):
        model = self._fit()
        with self.assertRaisesRegex(htft_model.HTFTModelError, "unknown team"):
            htft_model.predict_model(
                model,
                "New Club",
                "Bravo",
                kickoff=PREDICTION_KICKOFF,
                generated_at=PREDICTION_GENERATED_AT,
            )
        prediction = htft_model.predict_model(
            model,
            "New Club",
            "Bravo",
            kickoff=PREDICTION_KICKOFF,
            generated_at=PREDICTION_GENERATED_AT,
            unknown_team_policy="league_average",
        )
        self.assertEqual(
            prediction["fixture"]["unknown_team_policy"], "league_average"
        )
        self.assertTrue(prediction["warnings"])

        with self.assertRaisesRegex(htft_model.HTFTModelError, "explicit UTC offset"):
            htft_model.predict_model(
                model,
                "Alpha",
                "Bravo",
                kickoff="2099-01-01T12:00:00",
                generated_at=PREDICTION_GENERATED_AT,
            )
        with self.assertRaisesRegex(htft_model.HTFTModelError, "strictly before"):
            htft_model.predict_model(
                model,
                "Alpha",
                "Bravo",
                kickoff="2025-03-22T23:59:59Z",
                generated_at="2025-03-21T00:00:00Z",
            )

    def test_csv_validation_rejects_impossible_and_duplicate_scores(self):
        impossible = self.base / "impossible.csv"
        self._write_csv(
            impossible,
            [
                ("2025-01-01", "Alpha", "Bravo", 1, 0, 2, 0),
                ("2025-01-02", "Bravo", "Alpha", 0, 0, 0, 0),
            ],
        )
        with self.assertRaisesRegex(htft_model.HTFTModelError, "cannot exceed"):
            htft_model.load_training_csv(impossible)

        duplicate = self.base / "duplicate.csv"
        self._write_csv(
            duplicate,
            [
                ("2025-01-01", "Alpha", "Bravo", 1, 0, 0, 0),
                ("2025-01-01", "Alpha", "Bravo", 1, 0, 0, 0),
            ],
        )
        with self.assertRaisesRegex(htft_model.HTFTModelError, "duplicate"):
            htft_model.load_training_csv(duplicate)

    def test_walk_forward_metrics_and_cutoffs_are_auditable(self):
        arguments = {
            "min_train_matches": 6,
            "test_block_size": 3,
            "iterations": 12,
            "learning_rate": 0.025,
            "regularization": 0.03,
            "half_time_half_life_days": 180.0,
            "second_half_half_life_days": 180.0,
            "full_time_half_life_days": 180.0,
            "competition_key": "test_league",
            "dataset_manifest_hash": MANIFEST_HASH,
            "unknown_team_policy": "league_average",
            "max_goals": 7,
        }
        first = htft_model.backtest_model(self.csv_path, **arguments)
        second = htft_model.backtest_model(self.csv_path, **arguments)
        self.assertEqual(first, second)
        self.assertEqual(first["metrics"]["sample_count"], 6)
        self.assertFalse(first["split_policy"]["random_split"])
        self.assertFalse(first["split_policy"]["same_date_split_allowed"])
        self.assertFalse(first["fit_config"]["external_anchor_enabled"])
        self.assertLessEqual(
            first["metrics"]["top_one_accuracy"],
            first["metrics"]["top_two_accuracy"],
        )
        for name in (
            "nine_class_log_loss",
            "nine_class_brier",
            "top_one_accuracy",
            "top_two_accuracy",
        ):
            self.assertTrue(math.isfinite(first["metrics"][name]), name)
        for forecast in first["predictions"]:
            self.assertLess(forecast["training_cutoff_date"], forecast["date"])
            self.assertTrue(forecast["prediction_hash"].startswith("sha256:"))
            self.assertEqual(
                set(forecast["probabilities"]), set(htft_model.HTFT_CLASSES)
            )
        htft_model.validate_backtest(first)

        tampered = copy.deepcopy(first)
        tampered["metrics"]["top_two_accuracy"] += 0.001
        tampered["backtest_hash"] = htft_model.calculate_backtest_hash(tampered)
        with self.assertRaisesRegex(htft_model.HTFTModelError, "does not recompute"):
            htft_model.validate_backtest(tampered)

        tampered = copy.deepcopy(first)
        forecast = tampered["predictions"][0]
        alternatives = [
            name
            for name in htft_model.HTFT_CLASSES
            if name != forecast["actual_class"]
        ]
        receiver = alternatives[0]
        donor = max(alternatives[1:], key=forecast["probabilities"].get)
        delta = min(1e-4, forecast["probabilities"][donor] / 10.0)
        forecast["probabilities"][receiver] += delta
        forecast["probabilities"][donor] -= delta
        tampered["backtest_hash"] = htft_model.calculate_backtest_hash(tampered)
        with self.assertRaisesRegex(
            htft_model.HTFTModelError,
            "Top-2|does not recompute|actual-class probability",
        ):
            htft_model.validate_backtest(tampered)

        tampered = copy.deepcopy(first)
        tampered["predictions"][0]["training_cutoff_date"] = tampered[
            "predictions"
        ][0]["date"]
        tampered["backtest_hash"] = htft_model.calculate_backtest_hash(tampered)
        with self.assertRaisesRegex(htft_model.HTFTModelError, "strictly before"):
            htft_model.validate_backtest(tampered)

        tampered = copy.deepcopy(first)
        tampered["predictions"][0]["model_hash"] = "sha256:" + "b" * 64
        tampered["backtest_hash"] = htft_model.calculate_backtest_hash(tampered)
        with self.assertRaisesRegex(htft_model.HTFTModelError, "model_hash"):
            htft_model.validate_backtest(tampered)

    def test_cli_fit_and_predict_write_verified_artifacts(self):
        model_path = self.base / "model.json"
        prediction_path = self.base / "prediction.json"
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
                "15",
                "--competition-key",
                "test_league",
                "--dataset-manifest-hash",
                MANIFEST_HASH,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(fit.returncode, 0, fit.stderr)
        model = json.loads(model_path.read_text(encoding="utf-8"))
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
                "--generated-at",
                PREDICTION_GENERATED_AT,
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
        self.assertTrue(prediction["prediction_hash"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()

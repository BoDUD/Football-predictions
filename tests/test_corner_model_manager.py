from __future__ import annotations

import copy
import csv
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from _corner_source_fixture import build_source_bound_dataset

from scripts import corner_history_dataset_builder, corner_model, corner_model_manager

TEAMS = ("A", "B", "C", "D")


def write_history(
    path: Path,
    *,
    days: int = 16,
    start: date = date(2023, 1, 1),
    result_offset: int = 0,
) -> None:
    schedules = (
        (("A", "B"), ("C", "D")),
        (("A", "C"), ("D", "B")),
        (("A", "D"), ("B", "C")),
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            ["date", "home_team", "away_team", "home_corners", "away_corners"]
        )
        for day in range(days):
            match_date = start + timedelta(days=day)
            for home, away in schedules[day % len(schedules)]:
                home_index = TEAMS.index(home)
                away_index = TEAMS.index(away)
                writer.writerow(
                    [
                        match_date.isoformat(),
                        home,
                        away,
                        3 + (2 * home_index + day + result_offset) % 7,
                        2 + (away_index + 2 * day + result_offset) % 6,
                    ]
                )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class CornerModelManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.seed_temporary = tempfile.TemporaryDirectory()
        cls.seed = Path(cls.seed_temporary.name)
        first_source = cls.seed / "first-source"
        second_source = cls.seed / "second-source"
        first_source.mkdir()
        second_source.mkdir()
        cls.first_history, cls.first_manifest = build_source_bound_dataset(
            first_source,
            target_league_key="korea_k_league_1",
        )
        cls.second_history, cls.second_manifest = build_source_bound_dataset(
            second_source,
            target_league_key="england_premier_league",
            result_offset=2,
        )
        cls.seed_models = cls.seed / "models"
        corner_model_manager.train_registered_model(
            cls.first_history,
            cls.seed_models,
            league_key="korea_k_league_1",
            aliases=("K League 1",),
            generated_at="2024-01-01T00:00:00Z",
            half_life_days=120.0,
            iterations=20,
            learning_rate=0.025,
            regularization=0.03,
            min_train_matches=8,
            test_block_size=5,
            hard_max_corners=70,
        )
        corner_model_manager.train_registered_model(
            cls.second_history,
            cls.seed_models,
            league_key="england_premier_league",
            aliases=("Premier League",),
            generated_at="2024-01-01T00:00:00Z",
            half_life_days=120.0,
            iterations=20,
            learning_rate=0.025,
            regularization=0.03,
            min_train_matches=8,
            test_block_size=5,
            hard_max_corners=70,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.seed_temporary.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.model_dir = self.base / "models"
        shutil.copytree(self.seed_models, self.model_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def registry_path(self) -> Path:
        return self.model_dir / corner_model_manager.REGISTRY_FILENAME

    def entry(self, registry: dict, league_key: str = "korea_k_league_1") -> dict:
        return next(
            item for item in registry["leagues"] if item["league_key"] == league_key
        )

    def test_registry_binds_dataset_model_evaluation_and_candidate_gate(self):
        registry = corner_model_manager.load_registry(self.model_dir)
        self.assertEqual(
            registry["registry_hash"],
            corner_model_manager.calculate_registry_hash(registry),
        )
        self.assertEqual(
            list(registry["dataset_hashes"]),
            ["england_premier_league", "korea_k_league_1"],
        )
        entry = self.entry(registry)
        self.assertEqual(
            entry["lineage_hash"],
            corner_model_manager.calculate_lineage_hash(entry),
        )
        self.assertEqual(
            entry["dataset_hash"], registry["dataset_hashes"]["korea_k_league_1"]
        )
        self.assertRegex(entry["model_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(entry["evaluation_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(entry["backtest_hash"], entry["evaluation_hash"])
        self.assertEqual(entry["deployment_status"], "shadow")
        self.assertFalse(entry["deployment"]["gate"]["passed"])
        self.assertTrue(
            any(
                not passed
                for name, passed in entry["deployment"]["gate"]["checks"].items()
                if name.startswith("better_than_")
            )
        )
        self.assertFalse(entry["deployment"]["production_eligible"])
        self.assertFalse(entry["formal_corner_total_eligible"])
        self.assertFalse(entry["formal_corner_handicap_eligible"])
        evaluation = read_json(self.model_dir / entry["evaluation_file"])
        corner_model_manager.validate_backtest(
            evaluation,
            dataset_hash=entry["dataset_hash"],
            dataset_rows=entry["dataset_rows"],
            dataset_path=self.model_dir / entry["dataset_file"],
            source_lineage=entry["source_lineage"],
            expected_config=entry["evaluation_config"],
        )

    def test_daily_loader_replays_only_selected_league_and_caches_same_bytes(self):
        original = corner_history_dataset_builder.build_dataset
        with mock.patch.object(
            corner_history_dataset_builder,
            "build_dataset",
            wraps=original,
        ) as replay:
            registry, entry = corner_model_manager.load_registered_league(
                self.model_dir, "K League 1"
            )
            self.assertEqual(entry["league_key"], "korea_k_league_1")
            self.assertEqual(
                registry["registry_hash"],
                read_json(self.registry_path())["registry_hash"],
            )
            self.assertEqual(replay.call_count, 1)
            self.assertEqual(
                replay.call_args.kwargs.get("league_keys"),
                ["korea_k_league_1"],
            )

            corner_model_manager.load_registered_league(
                self.model_dir, "korea_k_league_1"
            )
            self.assertEqual(replay.call_count, 1)

    def test_daily_selected_league_isolated_but_deep_audit_finds_other_damage(self):
        registry = read_json(self.registry_path())
        other = self.entry(registry, "england_premier_league")
        other_dataset = self.model_dir / other["dataset_file"]
        other_dataset.write_bytes(other_dataset.read_bytes() + b"\n")

        _registry, selected = corner_model_manager.load_registered_league(
            self.model_dir, "korea_k_league_1"
        )
        self.assertEqual(selected["league_key"], "korea_k_league_1")
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError, "dataset CSV hash"
        ):
            corner_model_manager.load_registry(self.model_dir, force_full_replay=True)

    def test_deployment_is_evaluation_derived_not_league_name_hardcoded(self):
        registry = corner_model_manager.load_registry(self.model_dir)
        # Status is evidence-derived: sample size alone cannot pass when the
        # paired empirical/NB improvement confidence gates fail.
        self.assertEqual(self.entry(registry)["deployment_status"], "shadow")

        short_source = self.base / "short-source"
        short_source.mkdir()
        short_history, _short_manifest = build_source_bound_dataset(
            short_source,
            target_league_key="england_premier_league",
            days=6,
        )
        short_dir = self.base / "short-models"
        short_registry = corner_model_manager.train_registered_model(
            short_history,
            short_dir,
            league_key="england_premier_league",
            generated_at="2024-01-01T00:00:00Z",
            half_life_days=120.0,
            iterations=15,
            learning_rate=0.025,
            regularization=0.03,
            min_train_matches=8,
            test_block_size=5,
            hard_max_corners=70,
        )
        short_entry = short_registry["leagues"][0]
        self.assertEqual(short_entry["deployment_status"], "shadow")
        self.assertFalse(short_entry["deployment"]["gate"]["passed"])
        self.assertFalse(
            short_entry["deployment"]["gate"]["checks"]["minimum_predictions"]
        )
        self.assertFalse(short_entry["deployment"]["production_eligible"])

    def test_registered_prediction_preserves_fail_closed_formal_flags(self):
        registry = corner_model_manager.load_registry(self.model_dir)
        entry = self.entry(registry)
        model = corner_model.load_model(self.model_dir / entry["model_file"])
        prediction = corner_model_manager.predict_registered_model(
            self.model_dir,
            "K League 1",
            "A",
            "B",
            kickoff="2024-02-01T12:00:00Z",
            generated_at="2024-01-15T00:00:00Z",
            total_markets=(("over", 8.5),),
            corner_handicaps=(("home", -0.5),),
        )
        self.assertEqual(prediction["deployment_status"], "shadow")
        self.assertFalse(prediction["formal_corner_total_eligible"])
        self.assertFalse(prediction["formal_corner_handicap_eligible"])
        self.assertFalse(prediction["usage_policy"]["formal_corner_total_eligible"])
        self.assertFalse(prediction["usage_policy"]["formal_corner_handicap_eligible"])
        self.assertEqual(
            prediction["usage_policy"]["status"],
            "registered_model_distribution",
        )
        self.assertTrue(prediction["usage_policy"]["source_bound_manager_verified"])
        self.assertTrue(prediction["usage_policy"]["eligible_for_formal_model_input"])
        self.assertEqual(prediction["fixture"]["league_key"], "korea_k_league_1")
        self.assertEqual(
            prediction["registry_binding"]["evaluation_hash"],
            self.entry(registry)["evaluation_hash"],
        )
        self.assertEqual(
            prediction["registry_binding"]["fixture_graph_hash"],
            self.entry(registry)["dataset_profile"]["fixture_graph"]["components_hash"],
        )
        corner_model_manager.validate_registered_prediction(
            prediction, registry, model=model
        )

        fallback = corner_model_manager.predict_registered_model(
            self.model_dir,
            "韩K联",
            "A",
            "Promoted FC",
            kickoff="2024-02-01T12:00:00Z",
            generated_at="2024-01-15T00:00:00Z",
            unknown_team_policy="league_average",
        )
        self.assertEqual(fallback["usage_policy"]["status"], "observation_only")
        self.assertFalse(fallback["formal_corner_total_eligible"])
        corner_model_manager.validate_registered_prediction(
            fallback, registry, model=model
        )
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError,
            "requires the bound registered model",
        ):
            corner_model_manager.validate_registered_prediction(fallback, registry)

        forged_known_fallback = copy.deepcopy(fallback)
        forged_known_fallback["fixture"]["away_team"] = "B"
        forged_known_fallback["fixture"]["unknown_teams"] = ["B"]
        forged_known_fallback["fixture"]["away_training_component_id"] = (
            forged_known_fallback["fixture"]["home_training_component_id"]
        )
        forged_known_fallback["fixture"]["same_training_component"] = None
        forged_known_fallback["prediction_hash"] = (
            corner_model.calculate_prediction_hash(forged_known_fallback)
        )
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError,
            "unknown_teams do not match the training fixture graph",
        ):
            corner_model_manager.validate_registered_prediction(
                forged_known_fallback, registry, model=model
            )

    def test_registry_and_prediction_tampering_are_rejected(self):
        registry = read_json(self.registry_path())
        self.entry(registry)["deployment_status"] = "production"
        write_json(self.registry_path(), registry)
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError, "registry_hash"
        ):
            corner_model_manager.load_registered_league(
                self.model_dir, "korea_k_league_1"
            )

        # Re-hashing the forged production flag still cannot exceed the current
        # historical-only deployment authority.
        registry["registry_hash"] = corner_model_manager.calculate_registry_hash(
            registry
        )
        write_json(self.registry_path(), registry)
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError,
            "historical-only|deployment_status",
        ):
            corner_model_manager.load_registered_league(
                self.model_dir, "korea_k_league_1"
            )

        # Restore the pristine registry and forge only a prediction binding.
        shutil.rmtree(self.model_dir)
        shutil.copytree(self.seed_models, self.model_dir)
        registry = corner_model_manager.load_registry(self.model_dir)
        model = corner_model.load_model(
            self.model_dir / self.entry(registry)["model_file"]
        )
        prediction = corner_model_manager.predict_registered_model(
            self.model_dir,
            "korea_k_league_1",
            "A",
            "B",
            kickoff="2024-02-01T12:00:00Z",
            generated_at="2024-01-15T00:00:00Z",
        )
        prediction["registry_binding"]["evaluation_hash"] = "sha256:" + "0" * 64
        prediction["prediction_hash"] = corner_model.calculate_prediction_hash(
            prediction
        )
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError, "registry binding"
        ):
            corner_model_manager.validate_registered_prediction(
                prediction, registry, model=model
            )

        graph_forgery = corner_model_manager.predict_registered_model(
            self.model_dir,
            "korea_k_league_1",
            "A",
            "B",
            kickoff="2024-02-01T12:00:00Z",
            generated_at="2024-01-15T00:00:00Z",
        )
        graph_forgery["registry_binding"]["fixture_graph_hash"] = "sha256:" + "f" * 64
        graph_forgery["prediction_hash"] = corner_model.calculate_prediction_hash(
            graph_forgery
        )
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError, "registry binding"
        ):
            corner_model_manager.validate_registered_prediction(
                graph_forgery, registry, model=model
            )

    def test_rehashed_metric_forgery_is_rejected_by_semantic_validation(self):
        registry = read_json(self.registry_path())
        entry = self.entry(registry)
        evaluation_path = self.model_dir / entry["evaluation_file"]
        evaluation = read_json(evaluation_path)
        evaluation["metrics"]["total_mae"] += 1.0
        evaluation["backtest_hash"] = corner_model.calculate_backtest_hash(evaluation)
        write_json(evaluation_path, evaluation)

        entry["evaluation_hash"] = evaluation["backtest_hash"]
        entry["evaluation_file_sha256"] = file_hash(evaluation_path)
        entry["lineage_hash"] = corner_model_manager.calculate_lineage_hash(entry)
        registry["registry_hash"] = corner_model_manager.calculate_registry_hash(
            registry
        )
        write_json(self.registry_path(), registry)
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError,
            "prediction-level replay",
        ):
            corner_model_manager.load_registry(self.model_dir)

    def test_rehashed_untouched_holdout_forgery_is_rejected_by_replay(self):
        registry = read_json(self.registry_path())
        entry = self.entry(registry)
        evaluation_path = self.model_dir / entry["evaluation_file"]
        evaluation = read_json(evaluation_path)
        audit = evaluation["untouched_holdout"]["prediction_audit"]
        self.assertTrue(audit)
        audit[0]["total_mae"] = 0.0
        evaluation["backtest_hash"] = corner_model.calculate_backtest_hash(evaluation)
        write_json(evaluation_path, evaluation)
        entry["evaluation_hash"] = evaluation["backtest_hash"]
        entry["backtest_hash"] = evaluation["backtest_hash"]
        entry["evaluation_file_sha256"] = file_hash(evaluation_path)
        entry["lineage_hash"] = corner_model_manager.calculate_lineage_hash(entry)
        registry["registry_hash"] = corner_model_manager.calculate_registry_hash(
            registry
        )
        write_json(self.registry_path(), registry)
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError,
            "deterministic prediction-level replay",
        ):
            corner_model_manager.load_registered_league(
                self.model_dir, "korea_k_league_1"
            )

    def test_coordinated_all_zero_backtest_rehash_is_rejected(self):
        registry = read_json(self.registry_path())
        entry = self.entry(registry)
        evaluation_path = self.model_dir / entry["evaluation_file"]
        evaluation = read_json(evaluation_path)
        for prediction in evaluation["predictions"]:
            for field in corner_model_manager.METRIC_FIELDS:
                prediction[field] = 0.0
        for field in corner_model_manager.METRIC_FIELDS:
            evaluation["metrics"][field] = 0.0
        evaluation["backtest_hash"] = corner_model.calculate_backtest_hash(evaluation)
        write_json(evaluation_path, evaluation)
        entry["evaluation_hash"] = evaluation["backtest_hash"]
        entry["backtest_hash"] = evaluation["backtest_hash"]
        entry["evaluation_file_sha256"] = file_hash(evaluation_path)
        entry["lineage_hash"] = corner_model_manager.calculate_lineage_hash(entry)
        registry["registry_hash"] = corner_model_manager.calculate_registry_hash(
            registry
        )
        write_json(self.registry_path(), registry)
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError,
            "deterministic prediction-level replay",
        ):
            corner_model_manager.load_registered_league(
                self.model_dir, "korea_k_league_1"
            )

    def test_model_parameter_rehash_is_rejected_by_deterministic_refit(self):
        registry = read_json(self.registry_path())
        entry = self.entry(registry)
        model_path = self.model_dir / entry["model_file"]
        model = read_json(model_path)
        model["parameters"]["home_intercept"] += 1.0
        model["model_hash"] = corner_model.calculate_model_hash(model)
        write_json(model_path, model)
        entry["model_hash"] = model["model_hash"]
        entry["model_file_sha256"] = file_hash(model_path)
        entry["lineage_hash"] = corner_model_manager.calculate_lineage_hash(entry)
        registry["registry_hash"] = corner_model_manager.calculate_registry_hash(
            registry
        )
        write_json(self.registry_path(), registry)
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError,
            "deterministic source-bound refit",
        ):
            corner_model_manager.load_registered_league(
                self.model_dir, "korea_k_league_1"
            )

    def test_candidate_requires_both_baselines_with_positive_confidence_bound(self):
        comparisons = {}
        for baseline in corner_model.BASELINE_NAMES:
            comparisons[baseline] = {}
            for metric in corner_model.COMPARISON_METRICS:
                comparisons[baseline][metric] = {
                    "predictions": 100,
                    "independent_units": 5,
                    "uncertainty_unit": "walk_forward_block_cluster",
                    "model_mean": 1.0,
                    "baseline_mean": 1.2,
                    "mean_improvement": 0.2,
                    "relative_improvement": 1.0 / 6.0,
                    "sample_standard_error": 0.02,
                    "one_sided_95_lower_bound": (
                        0.2 - corner_model.ONE_SIDED_95_Z * 0.02
                    ),
                    "uncertainty_estimable": True,
                }
        backtest = {
            "sample": {
                "predictions": 100,
                "blocks": 5,
                "excluded_unknown_team_matches": 0,
                "excluded_component_incomparable_matches": 0,
            },
            "metrics": {
                "joint_log_loss": 3.0,
                "total_crps": 2.0,
                "margin_crps": 2.0,
                "total_mae": 3.0,
            },
            "comparisons": comparisons,
            "untouched_holdout": {
                "policy_version": corner_model.HOLDOUT_POLICY_VERSION,
                "status": "available",
                "development_only": False,
                "not_used_in_candidate_metric_thresholds": True,
            },
        }
        self.assertEqual(
            corner_model_manager.derive_historical_deployment(backtest)[
                "deployment_status"
            ],
            "candidate",
        )
        backtest["untouched_holdout"]["status"] = "insufficient_history"
        backtest["untouched_holdout"]["development_only"] = True
        insufficient = corner_model_manager.derive_historical_deployment(backtest)
        self.assertEqual(insufficient["deployment_status"], "shadow")
        self.assertFalse(insufficient["gate"]["checks"]["untouched_holdout_available"])
        backtest["untouched_holdout"]["status"] = "available"
        backtest["untouched_holdout"]["development_only"] = False
        comparisons["league_nb"]["margin_crps"]["sample_standard_error"] = 0.2
        comparisons["league_nb"]["margin_crps"]["one_sided_95_lower_bound"] = (
            0.2 - corner_model.ONE_SIDED_95_Z * 0.2
        )
        deployment = corner_model_manager.derive_historical_deployment(backtest)
        self.assertEqual(deployment["deployment_status"], "shadow")
        self.assertFalse(
            deployment["gate"]["checks"][
                "better_than_league_nb_margin_crps_uncertainty"
            ]
        )

    def test_unbound_legacy_csv_is_rejected(self):
        legacy = self.base / "legacy.csv"
        write_history(legacy, days=8)
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError,
            "dataset manifest|source-bound v2",
        ):
            corner_model_manager.train_registered_model(
                legacy,
                self.base / "legacy-models",
                league_key="korea_k_league_1",
                generated_at="2024-01-01T00:00:00Z",
                min_train_matches=8,
            )

    def test_rehashed_fixture_forgery_is_rejected_against_reopened_dataset(self):
        registry = read_json(self.registry_path())
        entry = self.entry(registry)
        evaluation_path = self.model_dir / entry["evaluation_file"]
        evaluation = read_json(evaluation_path)
        evaluation["predictions"][0]["home_team"] = "Forged FC"
        evaluation["backtest_hash"] = corner_model.calculate_backtest_hash(evaluation)
        write_json(evaluation_path, evaluation)

        entry["evaluation_hash"] = evaluation["backtest_hash"]
        entry["backtest_hash"] = evaluation["backtest_hash"]
        entry["evaluation_file_sha256"] = file_hash(evaluation_path)
        entry["lineage_hash"] = corner_model_manager.calculate_lineage_hash(entry)
        registry["registry_hash"] = corner_model_manager.calculate_registry_hash(
            registry
        )
        write_json(self.registry_path(), registry)
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError,
            "prediction-level replay",
        ):
            corner_model_manager.load_registry(self.model_dir)

    def test_cross_dataset_evaluation_swap_is_rejected_even_when_rehashed(self):
        registry = read_json(self.registry_path())
        first = self.entry(registry, "korea_k_league_1")
        second = self.entry(registry, "england_premier_league")
        for field in (
            "evaluation_file",
            "evaluation_file_sha256",
            "evaluation_hash",
            "backtest_hash",
            "evaluation_config",
        ):
            first[field] = copy.deepcopy(second[field])
        first["lineage_hash"] = corner_model_manager.calculate_lineage_hash(first)
        registry["registry_hash"] = corner_model_manager.calculate_registry_hash(
            registry
        )
        write_json(self.registry_path(), registry)
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError,
            "source_data_hash|registered dataset",
        ):
            corner_model_manager.load_registry(self.model_dir)

    def test_dataset_and_model_file_hashes_are_reopened_on_load(self):
        registry = read_json(self.registry_path())
        entry = self.entry(registry)
        dataset_path = self.model_dir / entry["dataset_file"]
        dataset_path.write_bytes(dataset_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError, "dataset CSV hash"
        ):
            corner_model_manager.load_registry(self.model_dir)

        shutil.rmtree(self.model_dir)
        shutil.copytree(self.seed_models, self.model_dir)
        registry = read_json(self.registry_path())
        entry = self.entry(registry)
        model_path = self.model_dir / entry["model_file"]
        model_path.write_bytes(model_path.read_bytes() + b" ")
        with self.assertRaisesRegex(
            corner_model_manager.CornerModelManagerError, "model file hash"
        ):
            corner_model_manager.load_registry(self.model_dir)


if __name__ == "__main__":
    unittest.main()

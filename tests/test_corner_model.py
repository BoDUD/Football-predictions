from __future__ import annotations

import copy
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
import tempfile
import unittest

from scripts import corner_model


TEAMS = ("A", "B", "C", "D")


def write_history(path: Path, *, days: int = 36, start: date = date(2023, 1, 1)) -> None:
    schedules = (
        (("A", "B"), ("C", "D")),
        (("A", "C"), ("D", "B")),
        (("A", "D"), ("B", "C")),
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(corner_model.TRAINING_COLUMNS)
        match_number = 1000000
        for day in range(days):
            match_date = start + timedelta(days=day)
            for fixture_index, (home, away) in enumerate(
                schedules[day % len(schedules)]
            ):
                kickoff = datetime(
                    match_date.year,
                    match_date.month,
                    match_date.day,
                    10 + fixture_index,
                    tzinfo=timezone.utc,
                )
                home_index = TEAMS.index(home)
                away_index = TEAMS.index(away)
                home_corners = 3 + (2 * home_index + day) % 7
                away_corners = 2 + (away_index + 2 * day) % 6
                match_number += 1
                fixture_hash = "sha256:" + hashlib.sha256(
                    f"fixture:{match_number}".encode()
                ).hexdigest()
                response_hash = "sha256:" + hashlib.sha256(
                    f"response:{match_number}".encode()
                ).hexdigest()
                writer.writerow(
                    [
                        match_date.isoformat(),
                        kickoff.isoformat().replace("+00:00", "Z"),
                        int(kickoff.timestamp()),
                        "test_league",
                        home,
                        away,
                        home_corners,
                        away_corners,
                        str(match_number),
                        "2023",
                        "regular_season",
                        "regular",
                        fixture_hash,
                        f"https://example.test/{match_number}",
                        (kickoff + timedelta(hours=3)).isoformat().replace(
                            "+00:00", "Z"
                        ),
                        response_hash,
                    ]
                )


class CornerModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.history = self.base / "corners.csv"
        write_history(self.history)
        self.model = corner_model.fit_model(
            self.history,
            half_life_days=180.0,
            iterations=80,
            learning_rate=0.025,
            regularization=0.03,
            generated_at="2024-01-01T00:00:00Z",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prediction(self, **overrides):
        values = {
            "kickoff": "2024-02-01T12:00:00Z",
            "generated_at": "2024-01-15T00:00:00Z",
            "tail_tolerance": 1e-8,
            "hard_max_corners": 80,
            "total_markets": (
                ("over", 8.0),
                ("over", 8.5),
                ("over", 8.25),
                ("over", 8.75),
            ),
            "corner_handicaps": (
                ("home", 0.0),
                ("home", -0.5),
                ("home", -0.25),
                ("home", -0.75),
            ),
        }
        values.update(overrides)
        return corner_model.predict_model(self.model, "A", "B", **values)

    def test_nb2_pmf_normalization_tail_and_mean(self):
        result = corner_model.nb2_distribution(
            6.25,
            2.5,
            tail_tolerance=1e-10,
            hard_max_corners=120,
        )
        probabilities = result["probabilities"]
        self.assertLessEqual(result["raw_omitted_probability"], 1e-10)
        self.assertAlmostEqual(
            math.fsum(probabilities) + result["raw_omitted_probability"],
            1.0,
            places=12,
        )
        mean = math.fsum(index * value for index, value in enumerate(probabilities))
        self.assertAlmostEqual(mean, 6.25, places=7)
        variance = math.fsum(
            (index - mean) ** 2 * value
            for index, value in enumerate(probabilities)
        )
        self.assertAlmostEqual(variance, 6.25 + 6.25**2 / 2.5, places=5)
        self.assertAlmostEqual(
            corner_model.nb2_pmf(4, 6.25, 2.5),
            math.exp(corner_model.nb2_log_pmf(4, 6.25, 2.5)),
            places=15,
        )

    def test_empirical_baseline_smoothing_does_not_inject_fifty_fake_matches(self):
        probabilities = corner_model._weighted_empirical_distribution(
            [5] * 200, [1.0] * 200
        )
        mean = math.fsum(index * value for index, value in enumerate(probabilities))
        self.assertLess(mean, 5.3)
        self.assertLess(math.fsum(probabilities[21:]), 0.005)

        small_sample = corner_model._weighted_empirical_distribution([5, 5], [1.0, 1.0])
        small_mean = math.fsum(
            index * value for index, value in enumerate(small_sample)
        )
        self.assertAlmostEqual(small_mean, 5.0, places=6)
        self.assertLess(math.fsum(small_sample[21:]), 0.01)

    def test_fit_is_deterministic_and_artifact_is_explicitly_independent(self):
        second = corner_model.fit_model(
            self.history,
            half_life_days=180.0,
            iterations=80,
            learning_rate=0.025,
            regularization=0.03,
            generated_at="2024-01-01T00:00:00Z",
        )
        self.assertEqual(self.model, second)
        self.assertEqual(self.model["model_hash"], second["model_hash"])
        self.assertEqual(
            self.model["dependence"],
            {
                "model": "independent_nb",
                "assumption": "home and away NB2 marginals are independent",
                "fitted_correlation": False,
            },
        )
        self.assertEqual(
            set(self.model["parameters"]["attack"]), set(TEAMS)
        )
        self.assertEqual(
            set(self.model["parameters"]["concession"]), set(TEAMS)
        )
        self.assertEqual(self.model["config"]["half_life_days"], 180.0)
        corner_model.validate_model(self.model)

    def test_disconnected_fixture_components_are_retained_audited_and_fail_cross_component(self):
        disconnected = self.base / "disconnected.csv"
        with self.history.open("r", encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        for index, row in enumerate(rows):
            first, second = (("A", "B") if index % 2 == 0 else ("C", "D"))
            if index % 4 >= 2:
                first, second = second, first
            row["home_team"] = first
            row["away_team"] = second
        with disconnected.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(
                target, fieldnames=corner_model.TRAINING_COLUMNS, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)

        records = corner_model.load_training_csv(disconnected)
        profile = corner_model.training_dataset_profile(records)
        graph = profile["fixture_graph"]
        self.assertFalse(graph["connected"])
        self.assertEqual(graph["component_count"], 2)
        self.assertEqual(
            sorted(item["team_count"] for item in graph["components"]), [2, 2]
        )
        self.assertEqual(
            sum(item["match_count"] for item in graph["components"]), len(rows)
        )

        model = corner_model.fit_model(
            disconnected,
            iterations=20,
            regularization=0.03,
            generated_at="2024-01-01T00:00:00Z",
        )
        self.assertEqual(model["training"]["matches"], len(rows))
        same_component = corner_model.predict_model(
            model,
            "A",
            "B",
            kickoff="2024-02-01T12:00:00Z",
            generated_at="2024-01-15T00:00:00Z",
        )
        self.assertTrue(same_component["fixture"]["same_training_component"])
        self.assertEqual(
            same_component["fixture"]["home_training_component_id"],
            same_component["fixture"]["away_training_component_id"],
        )
        with self.assertRaisesRegex(
            corner_model.CornerModelError, "cross-component fixture.*fails closed"
        ):
            corner_model.predict_model(
                model,
                "A",
                "C",
                kickoff="2024-02-01T12:00:00Z",
                generated_at="2024-01-15T00:00:00Z",
            )

        fallback = corner_model.predict_model(
            model,
            "A",
            "Promoted FC",
            kickoff="2024-02-01T12:00:00Z",
            generated_at="2024-01-15T00:00:00Z",
            unknown_team_policy="league_average",
        )
        forged_cross_component = copy.deepcopy(fallback)
        forged_cross_component["fixture"]["away_team"] = "C"
        forged_cross_component["fixture"]["unknown_teams"] = ["C"]
        component_map = corner_model._fixture_graph_team_components(graph)
        forged_cross_component["fixture"]["away_training_component_id"] = (
            component_map["C"]
        )
        forged_cross_component["fixture"]["same_training_component"] = None
        forged_cross_component["prediction_hash"] = (
            corner_model.calculate_prediction_hash(forged_cross_component)
        )
        with self.assertRaisesRegex(
            corner_model.CornerModelError,
            "unknown_teams do not match the training fixture graph",
        ):
            corner_model.validate_prediction(forged_cross_component)

        with self.assertRaisesRegex(
            corner_model.CornerModelError, "regularization must be positive"
        ):
            corner_model.fit_model(
                disconnected,
                iterations=2,
                regularization=0.0,
                generated_at="2024-01-01T00:00:00Z",
            )

        forged = copy.deepcopy(model)
        forged["training"]["dataset_profile"]["fixture_graph"][
            "components_hash"
        ] = "sha256:" + "0" * 64
        forged["model_hash"] = corner_model.calculate_model_hash(forged)
        with self.assertRaisesRegex(
            corner_model.CornerModelError, "fixture graph aggregate audit"
        ):
            corner_model.validate_model(forged)

    def test_walk_forward_explicitly_counts_first_component_bridge_exclusions(self):
        bridging = self.base / "bridging.csv"
        with self.history.open("r", encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        for index in range(8):
            home, away = (("A", "B") if index % 2 == 0 else ("C", "D"))
            rows[index]["home_team"] = home
            rows[index]["away_team"] = away
        rows[8]["home_team"], rows[8]["away_team"] = "A", "C"
        rows[9]["home_team"], rows[9]["away_team"] = "B", "D"
        with bridging.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(
                target, fieldnames=corner_model.TRAINING_COLUMNS, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)

        result = corner_model.backtest_model(
            bridging,
            min_train_matches=8,
            test_block_size=2,
            iterations=3,
            regularization=0.03,
            hard_max_corners=70,
        )
        self.assertGreaterEqual(
            result["sample"]["excluded_component_incomparable_matches"], 2
        )
        self.assertEqual(
            sum(
                block["excluded_component_incomparable_matches"]
                for block in result["blocks"]
            ),
            result["sample"]["excluded_component_incomparable_matches"],
        )
        for block in result["blocks"]:
            self.assertEqual(
                block["forecast_matches"]
                + block["excluded_unknown_team_matches"]
                + block["excluded_component_incomparable_matches"],
                block["test_matches"],
            )

    def test_known_league_noneligible_regime_requires_research_opt_in(self):
        research = self.base / "known-league-research.csv"
        with self.history.open("r", encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        for row in rows:
            row["league_key"] = "korea_k_league_1"
            row["competition_regime"] = "2026_vision_regional"
        with research.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(
                target, fieldnames=corner_model.TRAINING_COLUMNS, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)

        with self.assertRaisesRegex(
            corner_model.CornerModelError, "outside the installed eligible policy"
        ):
            corner_model.fit_model(
                research,
                iterations=2,
                generated_at="2024-01-01T00:00:00Z",
            )
        research_model = corner_model.fit_model(
            research,
            iterations=2,
            generated_at="2024-01-01T00:00:00Z",
            allow_research_cohorts=True,
        )
        self.assertTrue(research_model["authority"]["research_cohort_opt_in"])
        research_prediction = corner_model.predict_model(
            research_model,
            "A",
            "B",
            kickoff="2024-02-01T12:00:00Z",
            generated_at="2024-01-15T00:00:00Z",
        )
        self.assertFalse(research_prediction["formal_eligible"])
        self.assertFalse(
            research_prediction["usage_policy"]["eligible_for_formal_model_input"]
        )

    def test_j1_2026_hard_exclusion_cannot_hide_behind_regular_label(self):
        research = self.base / "j1-2026-research.csv"
        with self.history.open("r", encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        for row in rows:
            kickoff = datetime.fromisoformat(
                row["kickoff_utc"].replace("Z", "+00:00")
            ).replace(year=2026)
            row.update(
                {
                    "date": kickoff.date().isoformat(),
                    "kickoff_utc": kickoff.isoformat().replace("+00:00", "Z"),
                    "kickoff_epoch": str(int(kickoff.timestamp())),
                    "league_key": "japan_j1",
                    "season": "2026",
                    "competition_regime": "regular",
                    "phase": "regular_season",
                    "source_collected_at": (
                        kickoff + timedelta(hours=3)
                    ).isoformat().replace("+00:00", "Z"),
                }
            )
        with research.open("w", encoding="utf-8", newline="") as target:
            writer = csv.DictWriter(
                target, fieldnames=corner_model.TRAINING_COLUMNS, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaisesRegex(corner_model.CornerModelError, "hard_exclusions"):
            corner_model.fit_model(
                research,
                iterations=2,
                generated_at="2027-01-01T00:00:00Z",
            )
        model = corner_model.fit_model(
            research,
            iterations=2,
            generated_at="2027-01-01T00:00:00Z",
            allow_research_cohorts=True,
        )
        self.assertTrue(model["authority"]["research_cohort_opt_in"])
        self.assertFalse(model["authority"]["formal_eligible"])

    def test_prediction_matrix_normalization_tail_mean_and_determinism(self):
        prediction = self.prediction()
        second = self.prediction()
        self.assertEqual(prediction, second)
        matrix = prediction["joint_corner_matrix"]["probabilities"]
        self.assertAlmostEqual(
            math.fsum(math.fsum(row) for row in matrix), 1.0, places=12
        )
        self.assertLessEqual(
            prediction["tail_mass"]["raw_omitted_probability"], 1e-8
        )
        self.assertTrue(prediction["tail_mass"]["tolerance_met"])
        parameters = prediction["distribution_parameters"]
        expected = prediction["expected_corners"]
        # The matrix is normalized only after a sub-1e-8 tail truncation.
        self.assertAlmostEqual(expected["home"], parameters["home_mean"], places=6)
        self.assertAlmostEqual(expected["away"], parameters["away_mean"], places=6)
        self.assertAlmostEqual(
            prediction["dependence"]["matrix_covariance"], 0.0, places=9
        )
        self.assertEqual(prediction["dependence"]["model"], "independent_nb")
        self.assertEqual(prediction["fixture"]["league_key"], "test_league")
        self.assertEqual(prediction["usage_policy"]["status"], "observation_only")
        self.assertTrue(prediction["usage_policy"]["known_team_model_input"])
        self.assertFalse(
            prediction["usage_policy"]["source_bound_manager_verified"]
        )
        self.assertFalse(
            prediction["usage_policy"]["eligible_for_formal_model_input"]
        )
        corner_model.validate_prediction(prediction, model=self.model)

        forged_authority = copy.deepcopy(prediction)
        forged_authority["usage_policy"].update(
            {
                "status": "registered_model_distribution",
                "source_bound_manager_verified": True,
                "eligible_for_formal_model_input": True,
            }
        )
        forged_authority["prediction_hash"] = corner_model.calculate_prediction_hash(
            forged_authority
        )
        with self.assertRaisesRegex(
            corner_model.CornerModelError, "cannot grant manager"
        ):
            corner_model.validate_prediction(forged_authority, model=self.model)

    def test_integer_half_and_quarter_lines_have_correct_five_state_support(self):
        prediction = self.prediction()
        totals = {item["line"]: item for item in prediction["corner_totals"]}
        handicaps = {
            abs(item["line"]): item for item in prediction["corner_handicaps"]
        }
        for item in prediction["corner_totals"] + prediction["corner_handicaps"]:
            self.assertEqual(
                set(item["probabilities"]), set(corner_model.SETTLEMENT_STATES)
            )
            self.assertAlmostEqual(
                math.fsum(item["probabilities"].values()), 1.0, places=12
            )

        # Integer lines can push but can never half win/loss.
        for item in (totals[8.0], handicaps[0.0]):
            self.assertGreater(item["probabilities"]["push"], 0.0)
            self.assertEqual(item["probabilities"]["half_win"], 0.0)
            self.assertEqual(item["probabilities"]["half_loss"], 0.0)

        # Half lines have only full wins and full losses.
        for item in (totals[8.5], handicaps[0.5]):
            self.assertEqual(item["probabilities"]["push"], 0.0)
            self.assertEqual(item["probabilities"]["half_win"], 0.0)
            self.assertEqual(item["probabilities"]["half_loss"], 0.0)

        # x.25 creates a half loss for the selected over/home side, x.75 a
        # half win, and neither quarter line has a true push state.
        for item in (totals[8.25], handicaps[0.25]):
            self.assertGreater(item["probabilities"]["half_loss"], 0.0)
            self.assertEqual(item["probabilities"]["push"], 0.0)
        for item in (totals[8.75], handicaps[0.75]):
            self.assertGreater(item["probabilities"]["half_win"], 0.0)
            self.assertEqual(item["probabilities"]["push"], 0.0)

    def test_unknown_team_fails_closed_or_is_observation_only(self):
        with self.assertRaisesRegex(corner_model.CornerModelError, "unknown team"):
            corner_model.predict_model(
                self.model,
                "A",
                "Promoted FC",
                kickoff="2024-02-01T12:00:00Z",
                generated_at="2024-01-15T00:00:00Z",
            )
        fallback = corner_model.predict_model(
            self.model,
            "A",
            "Promoted FC",
            kickoff="2024-02-01T12:00:00Z",
            generated_at="2024-01-15T00:00:00Z",
            unknown_team_policy="league_average",
        )
        self.assertEqual(fallback["fixture"]["unknown_teams"], ["Promoted FC"])
        self.assertEqual(fallback["usage_policy"]["status"], "observation_only")
        self.assertFalse(
            fallback["usage_policy"]["eligible_for_formal_model_input"]
        )
        self.assertTrue(fallback["warnings"])
        corner_model.validate_prediction(fallback, model=self.model)

    def test_training_cutoff_and_generation_timing_fail_closed(self):
        training_end = self.model["training"]["end_date"]
        # A valid model itself is generated after its day-level cutoff, so a
        # fixture on that cutoff day is rejected even earlier by the model-time
        # boundary.  Either gate is a safe fail-closed result.
        with self.assertRaisesRegex(
            corner_model.CornerModelError, "predate model|training cutoff"
        ):
            corner_model.predict_model(
                self.model,
                "A",
                "B",
                kickoff=f"{training_end}T12:00:00Z",
                generated_at=f"{training_end}T01:00:00Z",
            )
        with self.assertRaisesRegex(corner_model.CornerModelError, "before kickoff"):
            corner_model.predict_model(
                self.model,
                "A",
                "B",
                kickoff="2024-02-01T12:00:00Z",
                generated_at="2024-02-01T12:00:00Z",
            )
        with self.assertRaisesRegex(corner_model.CornerModelError, "predate model"):
            corner_model.predict_model(
                self.model,
                "A",
                "B",
                kickoff="2024-02-01T12:00:00Z",
                generated_at="2023-12-31T23:59:59Z",
            )

    def test_model_and_prediction_hash_tampering_is_rejected(self):
        tampered_model = copy.deepcopy(self.model)
        tampered_model["parameters"]["home_intercept"] += 0.01
        with self.assertRaisesRegex(corner_model.CornerModelError, "model_hash"):
            corner_model.validate_model(tampered_model)

        prediction = self.prediction()
        tampered_prediction = copy.deepcopy(prediction)
        tampered_prediction["joint_corner_matrix"]["probabilities"][0][0] += 1e-5
        with self.assertRaisesRegex(corner_model.CornerModelError, "prediction_hash"):
            corner_model.validate_prediction(tampered_prediction, model=self.model)

        # Re-hashing a forged matrix is not enough: semantic validation rebuilds
        # every cell from the stored NB2 marginals.
        tampered_prediction["prediction_hash"] = corner_model.calculate_prediction_hash(
            tampered_prediction
        )
        with self.assertRaisesRegex(
            corner_model.CornerModelError, "matrix probabilities|canonical distribution"
        ):
            corner_model.validate_prediction(tampered_prediction, model=self.model)

        wrong_league = copy.deepcopy(prediction)
        wrong_league["fixture"]["league_key"] = "wrong_league"
        wrong_league["prediction_hash"] = corner_model.calculate_prediction_hash(
            wrong_league
        )
        wrong_league_path = self.base / "wrong-league-prediction.json"
        corner_model.save_json(wrong_league, wrong_league_path)
        with self.assertRaisesRegex(
            corner_model.CornerModelError, "does not match its dataset profile"
        ):
            corner_model.load_prediction(wrong_league_path)

    def test_walk_forward_keeps_complete_dates_together(self):
        short_history = self.base / "short.csv"
        write_history(short_history, days=14, start=date(2023, 1, 1))
        result = corner_model.backtest_model(
            short_history,
            min_train_matches=8,
            test_block_size=5,
            half_life_days=120.0,
            iterations=25,
            learning_rate=0.025,
            regularization=0.03,
            hard_max_corners=70,
        )
        self.assertTrue(
            result["evaluation_policy"]["same_date_groups_kept_together"]
        )
        self.assertGreater(result["sample"]["predictions"], 0)
        test_date_to_block = {}
        for block in result["blocks"]:
            cutoff = date.fromisoformat(block["training_cutoff_date"])
            self.assertTrue(block["test_dates"])
            for raw_date in block["test_dates"]:
                test_date = date.fromisoformat(raw_date)
                self.assertLess(cutoff, test_date)
                self.assertNotIn(test_date, test_date_to_block)
                test_date_to_block[test_date] = block["block"]
        for forecast in result["predictions"]:
            forecast_date = date.fromisoformat(forecast["date"])
            self.assertIn(forecast_date, test_date_to_block)
            kickoff = datetime.fromisoformat(
                forecast["kickoff_utc"].replace("Z", "+00:00")
            )
            self.assertEqual(forecast["kickoff_epoch"], int(kickoff.timestamp()))
            self.assertIn(kickoff.hour, {10, 11})
            self.assertLess(
                date.fromisoformat(forecast["training_cutoff_date"]), forecast_date
            )
            for diagnostic in forecast["settlement_diagnostics"].values():
                self.assertEqual(
                    set(diagnostic["probabilities"]),
                    set(corner_model.SETTLEMENT_STATES),
                )
            self.assertEqual(
                set(forecast["baselines"]), set(corner_model.BASELINE_NAMES)
            )
        self.assertEqual(set(result["baselines"]), set(corner_model.BASELINE_NAMES))
        self.assertEqual(set(result["comparisons"]), set(corner_model.BASELINE_NAMES))
        self.assertEqual(
            result["backtest_hash"], corner_model.calculate_backtest_hash(result)
        )
        second = corner_model.backtest_model(
            short_history,
            min_train_matches=8,
            test_block_size=5,
            half_life_days=120.0,
            iterations=25,
            learning_rate=0.025,
            regularization=0.03,
            hard_max_corners=70,
        )
        self.assertEqual(result, second)

    def test_fixed_latest_holdout_is_untouched_and_separate_from_development(self):
        history = self.base / "holdout.csv"
        write_history(history, days=60, start=date(2022, 1, 1))
        result = corner_model.backtest_model(
            history,
            min_train_matches=8,
            test_block_size=5,
            iterations=5,
            hard_max_corners=70,
        )
        holdout = result["untouched_holdout"]
        self.assertEqual(holdout["status"], "available")
        self.assertFalse(holdout["development_only"])
        self.assertTrue(holdout["not_used_in_candidate_metric_thresholds"])
        self.assertGreaterEqual(holdout["matches"], corner_model.MIN_HOLDOUT_MATCHES)
        development_end = max(
            datetime.fromisoformat(row["kickoff_utc"].replace("Z", "+00:00"))
            for row in result["predictions"]
        )
        holdout_start = min(
            datetime.fromisoformat(row["kickoff_utc"].replace("Z", "+00:00"))
            for row in holdout["prediction_audit"]
        )
        self.assertLess(development_end, holdout_start)
        self.assertTrue(holdout["metrics"])
        self.assertEqual(set(holdout["comparisons"]), set(corner_model.BASELINE_NAMES))
        self.assertEqual(
            result["backtest_hash"], corner_model.calculate_backtest_hash(result)
        )

    def test_legacy_csv_without_real_kickoff_and_lineage_columns_is_rejected(self):
        legacy = self.base / "legacy.csv"
        legacy.write_text(
            "date,home_team,away_team,home_corners,away_corners\n"
            "2023-01-01,A,B,4,3\n"
            "2023-01-02,B,A,5,2\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            corner_model.CornerModelError, "exact source-bound v2 columns"
        ):
            corner_model.load_training_csv(legacy)


if __name__ == "__main__":
    unittest.main()

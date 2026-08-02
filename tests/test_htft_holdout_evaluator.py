from __future__ import annotations

import csv
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "htft_holdout_evaluator.py"
)
SPEC = importlib.util.spec_from_file_location("soccer_htft_holdout_evaluator", SCRIPT)
assert SPEC and SPEC.loader
evaluator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluator)


SCORE_FIELDS = tuple(evaluator.history_importer.SCORE_FIELDS)
MARKET_FIELDS = tuple(evaluator.history_importer.MARKET_FIELDS)


def _result_code(home: int, away: int) -> str:
    return "H" if home > away else "A" if home < away else "D"


def _row(
    league_key: str,
    season: int,
    month_day: str,
    home: str,
    away: str,
    full: tuple[int, int],
    half: tuple[int, int],
    *,
    competition_regime: str = "regular",
) -> dict[str, str | int]:
    match_date = f"{season}-{month_day}"
    half_result = _result_code(*half)
    full_result = _result_code(*full)
    return {
        "date": match_date,
        "home_team": home,
        "away_team": away,
        "home_goals": full[0],
        "away_goals": full[1],
        "half_home_goals": half[0],
        "half_away_goals": half[1],
        "half_result": half_result,
        "full_result": full_result,
        "htft_result": half_result + full_result,
        "league_key": league_key,
        "league": league_key.upper(),
        "season": season,
        "competition_regime": competition_regime,
        "round": "1",
        "source_row": "2",
        "source_kickoff": match_date + "T12:00:00+00:00",
        "source_timezone": "UTC",
        "kickoff_utc": match_date + "T12:00:00Z",
    }


def _league_rows(league_key: str, extra_per_holdout: int = 0) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        _row(league_key, 2022, "02-01", "A", "B", (1, 0), (0, 0)),
        _row(league_key, 2022, "03-01", "B", "C", (2, 1), (1, 1)),
        _row(league_key, 2022, "04-01", "C", "A", (0, 0), (0, 0)),
        _row(league_key, 2023, "02-01", "A", "C", (2, 0), (1, 0)),
        _row(league_key, 2023, "03-01", "B", "A", (1, 1), (0, 1)),
        _row(league_key, 2023, "04-01", "C", "B", (1, 2), (0, 1)),
    ]
    holdouts = {
        2024: [
            ("02-01", "A", "B", (1, 1), (0, 0)),
            ("03-01", "X", "A", (0, 2), (0, 1)),
        ],
        2025: [
            ("02-01", "C", "X", (2, 1), (1, 0)),
            ("03-01", "Y", "B", (1, 0), (0, 0)),
        ],
        2026: [
            ("02-01", "Y", "A", (0, 0), (0, 0)),
            ("03-01", "Z", "C", (1, 2), (1, 1)),
        ],
    }
    for season, fixtures in holdouts.items():
        for fixture in fixtures:
            rows.append(_row(league_key, season, *fixture))
        for offset in range(extra_per_holdout):
            day = 10 + offset
            rows.append(
                _row(
                    league_key,
                    season,
                    f"04-{day:02d}",
                    "A",
                    "C",
                    (1 + offset % 2, offset % 2),
                    (0, 0),
                )
            )
    return rows


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _market_rows(score_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for score in score_rows:
        for bookmaker, odds in (
            ("alpha", (2.20, 3.30, 3.10)),
            ("beta", (2.30, 3.20, 3.00)),
            ("gamma", (2.25, 3.25, 3.05)),
            ("delta", (2.28, 3.22, 3.02)),
        ):
            rows.append(
                {
                    "league_key": score["league_key"],
                    "league": score["league"],
                    "season": score["season"],
                    "competition_regime": score["competition_regime"],
                    "round": score["round"],
                    "source_row": score["source_row"],
                    "source_kickoff": score["source_kickoff"],
                    "source_timezone": score["source_timezone"],
                    "kickoff_utc": score["kickoff_utc"],
                    "home_team": score["home_team"],
                    "away_team": score["away_team"],
                    "bookmaker": bookmaker,
                    "home_odds": odds[0],
                    "draw_odds": odds[1],
                    "away_odds": odds[2],
                    "asian_home_price": "",
                    "asian_line": "",
                    "asian_away_price": "",
                    "total_over_price": "",
                    "total_line": "",
                    "total_under_price": "",
                    "opening_1x2_complete": "true",
                    "opening_asian_complete": "false",
                    "opening_total_complete": "false",
                }
            )
    return rows


def _build_bundle(
    root: Path,
    league_specs: list[tuple[str, int]],
    *,
    leak_training_date: bool = False,
    include_japan_regime_transition: bool = False,
) -> Path:
    leagues: list[dict[str, object]] = []
    for league_key, extra_per_holdout in league_specs:
        score_rows = _league_rows(league_key, extra_per_holdout)
        if include_japan_regime_transition and league_key == "japan_j1":
            for row in score_rows:
                if int(row["season"]) == 2026:
                    if row["date"] == "2026-02-01":
                        row["date"] = "2026-02-06"
                        row["source_kickoff"] = "2026-02-06T12:00:00+00:00"
                        row["kickoff_utc"] = "2026-02-06T12:00:00Z"
                    row["competition_regime"] = (
                        evaluator.history_importer.JAPAN_J1_VISION_REGIME
                    )
            score_rows.append(
                _row(
                    league_key,
                    2026,
                    "08-07",
                    "A",
                    "B",
                    (1, 0),
                    (0, 0),
                )
            )
        if leak_training_date:
            score_rows[5]["date"] = "2024-12-01"
            score_rows[5]["kickoff_utc"] = "2024-12-01T12:00:00Z"
        score_name = f"{league_key}-scores.csv"
        market_name = f"{league_key}-opening-markets.csv"
        score_path = root / score_name
        market_path = root / market_name
        _write_csv(score_path, SCORE_FIELDS, score_rows)
        market_rows = _market_rows(score_rows)
        _write_csv(market_path, MARKET_FIELDS, market_rows)
        seasons: dict[str, int] = {}
        regimes: dict[str, dict[str, int]] = {}
        for row in score_rows:
            season = str(row["season"])
            regime = str(row["competition_regime"])
            seasons[season] = seasons.get(season, 0) + 1
            by_regime = regimes.setdefault(season, {})
            by_regime[regime] = by_regime.get(regime, 0) + 1
        leagues.append(
            {
                "league_key": league_key,
                "league": league_key.upper(),
                "rows": len(score_rows),
                "seasons": dict(sorted(seasons.items(), key=lambda item: int(item[0]))),
                "competition_regimes": {
                    season: dict(sorted(counts.items()))
                    for season, counts in sorted(
                        regimes.items(), key=lambda item: int(item[0])
                    )
                },
                "score_dataset": {
                    "file": score_name,
                    "sha256": evaluator._file_hash(score_path),
                    "rows": len(score_rows),
                },
                "opening_market_research": {
                    "file": market_name,
                    "sha256": evaluator._file_hash(market_path),
                    "rows": len(market_rows),
                    "policy": evaluator.RESEARCH_MARKET_POLICY,
                },
            }
        )
    manifest: dict[str, object] = {
        "artifact_type": "soccer_history_dataset_bundle",
        "schema_version": evaluator.history_importer.DATASET_SCHEMA_VERSION,
        "importer_version": evaluator.history_importer.IMPORTER_VERSION,
        "source_timezone": "UTC",
        "leagues": leagues,
    }
    manifest["competition_regime_counts"] = [
        {
            "league_key": league["league_key"],
            "season": int(season),
            "competition_regime": regime,
            "rows": count,
        }
        for league in sorted(leagues, key=lambda item: str(item["league_key"]))
        for season, counts in sorted(
            league["competition_regimes"].items(), key=lambda item: int(item[0])
        )
        for regime, count in sorted(counts.items())
    ]
    manifest["bundle_hash"] = evaluator._canonical_hash(manifest)
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _rewrite_summaries_from_forecasts(evaluation: dict[str, object]) -> None:
    repetitions = evaluation["bootstrap_config"]["repetitions"]
    seed = evaluation["bootstrap_config"]["seed"]
    all_accumulators: list[dict[str, object]] = []
    by_split: dict[str, list[dict[str, object]]] = {
        split["split_id"]: [] for split in evaluation["split_policy"]["splits"]
    }
    for league in evaluation["leagues"]:
        league_accumulators: list[dict[str, object]] = []
        for split in league["splits"]:
            model = evaluator._cohort_accumulators(
                evaluator.MODEL_PAIR_MASS_THRESHOLD
            )
            market = evaluator._cohort_accumulators(
                evaluator.MARKET_PAIR_MASS_THRESHOLD
            )
            baseline = evaluator._new_accumulator(
                evaluator.MODEL_PAIR_MASS_THRESHOLD
            )
            paired = evaluator._new_paired_accumulator()
            for forecast in split["forecasts"]:
                used_fallback = forecast["used_league_average_fallback"]
                evaluator._add_cohort_score(
                    model,
                    forecast["model_probabilities"],
                    forecast["actual_class"],
                    used_fallback=used_fallback,
                )
                evaluator._add_score(
                    baseline,
                    forecast["empirical_baseline_probabilities"],
                    forecast["actual_class"],
                )
                evaluator._add_paired_score(
                    paired,
                    forecast["model_probabilities"],
                    forecast["empirical_baseline_probabilities"],
                    forecast["actual_class"],
                    group=league["league_key"],
                )
            split["fallback_fixture_count"] = sum(
                int(item["used_league_average_fallback"])
                for item in split["forecasts"]
            )
            split["model_only"] = evaluator._finalize_cohorts(model)
            split["league_empirical_frequency_baseline"]["metrics"] = (
                evaluator._finalize_accumulator(baseline)["metrics"]
            )
            split["model_minus_empirical_baseline"] = (
                evaluator._finalize_paired_accumulator(
                    paired,
                    bootstrap_repetitions=repetitions,
                    bootstrap_seed=seed,
                )
            )
            accumulators = {
                "model": model,
                "market": market,
                "baseline": baseline,
                "paired": paired,
            }
            league_accumulators.append(accumulators)
            all_accumulators.append(accumulators)
            by_split[split["split_id"]].append(accumulators)
        league["summary"] = evaluator._aggregate_split_cohorts(
            league_accumulators,
            bootstrap_repetitions=repetitions,
            bootstrap_seed=seed,
        )
    evaluation["summary"] = {
        "by_split": {
            split_id: evaluator._aggregate_split_cohorts(
                accumulators,
                bootstrap_repetitions=repetitions,
                bootstrap_seed=seed,
            )
            for split_id, accumulators in by_split.items()
        },
        "all_splits": evaluator._aggregate_split_cohorts(
            all_accumulators,
            bootstrap_repetitions=repetitions,
            bootstrap_seed=seed,
        ),
    }


class HtftHoldoutEvaluatorTests(unittest.TestCase):
    def test_special_regime_is_counted_but_not_fit_scored_or_aggregated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(
                root,
                [("japan_j1", 0)],
                include_japan_regime_transition=True,
            )

            result = evaluator.evaluate_bundle(
                dataset_dir=root,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=50,
                experimental_override=True,
            )

            self.assertEqual(
                result["competition_regime_policy"],
                evaluator.COMPETITION_REGIME_POLICY,
            )
            self.assertTrue(
                result["promotion"][
                    "competition_regime_policy_matches_registered_manager"
                ]
            )
            shadow = result["leagues"][0]["splits"][2]
            self.assertEqual(shadow["test_match_count"], 1)
            self.assertEqual(shadow["excluded_test_match_count"], 2)
            self.assertEqual(
                shadow["excluded_test_competition_regime_counts"],
                {"2026_vision_regional": 2},
            )
            self.assertEqual(
                shadow["model_only"]["overall"]["metrics"]["sample_count"],
                1,
            )
            self.assertTrue(
                all(
                    forecast["competition_regime"] == "regular"
                    for forecast in shadow["forecasts"]
                )
            )
            self.assertFalse(
                evaluator._promotion_metadata(
                    fit_configuration_matches_promoted=True,
                    bootstrap_configuration_matches_promoted=True,
                    competition_regime_policy_matches_manager=False,
                )["registered_manager_compatible"]
            )

    def test_fixed_splits_are_leak_free_and_fallback_is_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(root, [("brazil_serie_a", 0)])
            output = root / "evaluation.json"

            result = evaluator.evaluate_bundle(
                dataset_dir=root,
                output_path=output,
                include_opening_market=True,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=50,
                experimental_override=True,
            )

            self.assertEqual(
                [split["split_id"] for split in result["leagues"][0]["splits"]],
                ["validation_2024", "fixed_holdout_2025", "shadow_2026"],
            )
            self.assertEqual(
                result["promotion"]["configuration_status"],
                "experimental_override_not_promotion_evidence",
            )
            self.assertFalse(result["promotion"]["final_selector_untouched"])
            self.assertFalse(result["promotion"]["end_to_end_promotion_eligible"])
            self.assertTrue(
                all(
                    "experiment" in split["role"]
                    for split in result["leagues"][0]["splits"]
                )
            )
            for split in result["leagues"][0]["splits"]:
                self.assertTrue(split["strict_cutoff_verified"])
                self.assertLess(
                    split["training_cutoff_date"], split["test_date_start"]
                )
                self.assertEqual(split["unknown_team_policy"], "league_average")
                self.assertGreater(split["fallback_fixture_count"], 0)
                self.assertGreater(
                    split["model_only"]["league_average_fallback"]["metrics"][
                        "sample_count"
                    ],
                    0,
                )
                self.assertRegex(split["model_hash"], r"^sha256:[0-9a-f]{64}$")
                self.assertEqual(len(split["forecasts"]), split["test_match_count"])
                forecast = split["forecasts"][0]
                self.assertEqual(len(forecast["model_probabilities"]), 9)
                self.assertEqual(len(forecast["model_top_two"]), 2)
                self.assertGreater(forecast["model_pair_mass"], 0.0)
                self.assertEqual(
                    forecast["training_cutoff_date"], split["training_cutoff_date"]
                )
                self.assertEqual(forecast["model_hash"], split["model_hash"])
                baseline = split["league_empirical_frequency_baseline"]
                model_metrics = split["model_only"]["overall"]["metrics"]
                paired = split["model_minus_empirical_baseline"]
                self.assertAlmostEqual(
                    paired["nine_class_log_loss"]["mean_delta"],
                    model_metrics["nine_class_log_loss"]
                    - baseline["metrics"]["nine_class_log_loss"],
                )
                self.assertEqual(paired["bootstrap"]["seed"], evaluator.BOOTSTRAP_SEED)
                self.assertEqual(paired["bootstrap"]["repetitions"], 50)
                self.assertLessEqual(
                    paired["nine_class_log_loss"]["ci95_low"],
                    paired["nine_class_log_loss"]["ci95_high"],
                )
                self.assertEqual(
                    sum(
                        item["support"]
                        for item in model_metrics["per_class"].values()
                    ),
                    split["test_match_count"],
                )
            self.assertEqual(
                result["fit_config"]["half_time_half_life_days"], 730.0
            )
            self.assertEqual(
                result["fit_config"]["full_time_half_life_days"], 365.0
            )
            self.assertEqual(result["fit_config"]["seed_method"], "empirical_association")
            self.assertTrue(output.is_file())
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(persisted["evaluation_hash"], result["evaluation_hash"])
            self.assertEqual(
                evaluator.calculate_evaluation_hash(persisted),
                persisted["evaluation_hash"],
            )
            evaluator.validate_evaluation(persisted, dataset_dir=root)

    def test_summary_metrics_are_match_count_weighted_across_leagues(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = _build_bundle(
                root,
                [("brazil_serie_a", 0), ("usa_mls", 1)],
            )

            result = evaluator.evaluate_bundle(
                manifest_path=manifest,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=50,
                experimental_override=True,
            )

            league_metrics = [
                league["summary"]["model_only"]["overall"]["metrics"]
                for league in result["leagues"]
            ]
            total = sum(item["sample_count"] for item in league_metrics)
            weighted_log_loss = sum(
                item["nine_class_log_loss"] * item["sample_count"]
                for item in league_metrics
            ) / total
            summary = result["summary"]["all_splits"]["model_only"]["overall"]
            self.assertEqual(summary["metrics"]["sample_count"], total)
            self.assertAlmostEqual(
                summary["metrics"]["nine_class_log_loss"], weighted_log_loss
            )
            weighted_top_two_hits = sum(item["top_two_hits"] for item in league_metrics)
            self.assertEqual(
                summary["metrics"]["top_two_hits"], weighted_top_two_hits
            )
            self.assertAlmostEqual(
                summary["metrics"]["top_two_accuracy"],
                weighted_top_two_hits / total,
            )

    def test_opening_market_is_explicitly_untimestamped_research_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(root, [("brazil_serie_a", 0)])

            result = evaluator.evaluate_bundle(
                dataset_dir=root,
                include_opening_market=True,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=50,
                experimental_override=True,
            )

            policy = result["market_research_policy"]
            self.assertTrue(policy["research_only"])
            self.assertFalse(policy["official_anchor_interface_used"])
            self.assertEqual(
                policy["collection_time_status"], "unavailable_in_source"
            )
            self.assertEqual(
                policy["policy"], evaluator.RESEARCH_MARKET_POLICY
            )
            serialized = json.dumps(result, sort_keys=True)
            self.assertNotIn("captured_at", serialized)
            split = result["leagues"][0]["splits"][0]
            research = split["research_opening_market"]
            self.assertEqual(research["anchor_availability"], 1.0)
            self.assertEqual(research["bookmaker_count"]["minimum"], 4)
            self.assertEqual(
                research["cohorts"]["overall"]["metrics"]["sample_count"],
                split["test_match_count"],
            )
            self.assertEqual(
                research["cohorts"]["overall"]["pair_mass_gate"]["threshold"],
                0.50,
            )
            self.assertEqual(
                split["model_only"]["overall"]["pair_mass_gate"]["threshold"],
                0.46,
            )

    def test_calendar_overlap_is_rejected_before_model_fit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(
                root,
                [("brazil_serie_a", 0)],
                leak_training_date=True,
            )

            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "training date overlaps holdout",
            ):
                evaluator.evaluate_bundle(
                    dataset_dir=root,
                    iterations=2,
                    experimental_override=True,
                )

    def test_non_promoted_settings_require_explicit_experiment_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(root, [("brazil_serie_a", 0)])

            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "experimental_override",
            ):
                evaluator.evaluate_bundle(
                    dataset_dir=root,
                    iterations=2,
                    bootstrap_repetitions=50,
                )

    def test_promoted_claim_cannot_be_forged_by_rehashing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(root, [("brazil_serie_a", 0)])
            result = evaluator.evaluate_bundle(
                dataset_dir=root,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=50,
                experimental_override=True,
            )
            tampered = copy.deepcopy(result)
            tampered["promotion"] = evaluator._promotion_metadata(
                fit_configuration_matches_promoted=True,
                bootstrap_configuration_matches_promoted=True,
            )
            tampered["evaluation_hash"] = evaluator.calculate_evaluation_hash(tampered)

            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "promotion metadata",
            ):
                evaluator.validate_evaluation(tampered)

    def test_source_binding_rejects_rehashed_fake_dataset_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(root, [("brazil_serie_a", 0)])
            result = evaluator.evaluate_bundle(
                dataset_dir=root,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=50,
                experimental_override=True,
            )
            tampered = copy.deepcopy(result)
            tampered["leagues"][0]["score_dataset"]["sha256"] = (
                "sha256:" + "0" * 64
            )
            tampered["evaluation_hash"] = evaluator.calculate_evaluation_hash(tampered)

            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "score source binding",
            ):
                evaluator.validate_evaluation(tampered, dataset_dir=root)

    def test_source_binding_rejects_rehashed_fallback_and_rebuilt_summaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(root, [("brazil_serie_a", 0)])
            result = evaluator.evaluate_bundle(
                dataset_dir=root,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=50,
                experimental_override=True,
            )
            tampered = copy.deepcopy(result)
            forecast = next(
                item
                for split in tampered["leagues"][0]["splits"]
                for item in split["forecasts"]
                if item["used_league_average_fallback"]
            )
            forecast["used_league_average_fallback"] = False
            _rewrite_summaries_from_forecasts(tampered)
            tampered["evaluation_hash"] = evaluator.calculate_evaluation_hash(tampered)

            evaluator.validate_evaluation(tampered)
            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "source outcomes",
            ):
                evaluator.validate_evaluation(tampered, dataset_dir=root)

    def test_evaluator_rejects_semantically_tampered_regime_after_rehash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = _build_bundle(root, [("brazil_serie_a", 0)])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            score_path = root / manifest["leagues"][0]["score_dataset"]["file"]
            with score_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["competition_regime"] = "2026_vision_regional"
            _write_csv(score_path, SCORE_FIELDS, rows)
            manifest["leagues"][0]["score_dataset"]["sha256"] = (
                evaluator._file_hash(score_path)
            )
            manifest["leagues"][0]["competition_regimes"]["2022"] = {
                "2026_vision_regional": 1,
                "regular": 2,
            }
            manifest["competition_regime_counts"] = [
                {
                    "league_key": "brazil_serie_a",
                    "season": int(season),
                    "competition_regime": regime,
                    "rows": count,
                }
                for season, counts in sorted(
                    manifest["leagues"][0]["competition_regimes"].items(),
                    key=lambda item: int(item[0]),
                )
                for regime, count in sorted(counts.items())
            ]
            manifest["bundle_hash"] = evaluator._canonical_hash(
                {
                    key: value
                    for key, value in manifest.items()
                    if key != "bundle_hash"
                }
            )
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

            for kwargs in (
                {"dataset_dir": root},
                {"manifest_path": manifest_path},
            ):
                with self.subTest(**kwargs), self.assertRaisesRegex(
                    evaluator.HoldoutEvaluationError,
                    "semantic validation.*competition_regime",
                ):
                    evaluator.evaluate_bundle(
                        **kwargs,
                        iterations=2,
                        experimental_override=True,
                    )

    def test_semantic_validation_rejects_rehashed_metric_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(root, [("brazil_serie_a", 0)])
            result = evaluator.evaluate_bundle(
                dataset_dir=root,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=50,
                experimental_override=True,
            )
            tampered = copy.deepcopy(result)
            metrics = tampered["leagues"][0]["splits"][0]["model_only"][
                "overall"
            ]["metrics"]
            metrics["top_two_hits"] += 1
            tampered["evaluation_hash"] = evaluator.calculate_evaluation_hash(tampered)

            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "does not match forecast evidence",
            ):
                evaluator.validate_evaluation(tampered)


if __name__ == "__main__":
    unittest.main()

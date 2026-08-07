from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "league_model_manager.py"
SPEC = importlib.util.spec_from_file_location("soccer_league_model_manager", SCRIPT)
assert SPEC and SPEC.loader
league_model_manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(league_model_manager)


def _hash_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _fake_model(league_key: str, bundle_hash: str, *, rows: int = 1):
    promoted = league_model_manager.VALIDATED_TRAINING_CONFIG
    return {
        "artifact_type": "soccer_htft_model",
        "schema_version": "1.0.0",
        "model_version": "htft-dixon-coles-ipf/1.0.0",
        "model_hash": _hash_bytes(f"model:{league_key}".encode()),
        "training": {
            "source_data_hash": _hash_bytes(f"training:{league_key}".encode()),
            "match_count": rows,
            "start_date": "2020-01-01",
            "end_date": "2025-12-31",
            "competition_key": league_key,
            "dataset_manifest_hash": bundle_hash,
        },
        "config": league_model_manager._expected_htft_model_config(promoted),
        "components": {
            "full_time": {"model_hash": _hash_bytes(f"full-time:{league_key}".encode())}
        },
        "empirical_association": {
            "smoothing_alpha": promoted["association_smoothing_alpha"],
            "power": promoted["association_power"],
            "time_decay": {
                "mode": "exponential_half_life",
                "half_life_days": promoted["association_half_life_days"],
            },
        },
        "construction": {
            "validated_configuration": {
                "half_time_half_life_days": promoted["half_time_half_life_days"],
                "full_time_half_life_days": promoted["full_time_half_life_days"],
                "association_power": promoted["association_power"],
            }
        },
    }


class LeagueModelManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.dataset_dir = self.base / "datasets"
        self.model_dir = self.base / "models"
        self.evaluation_path = self.base / "evaluation.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manager_and_evaluator_share_the_exact_competition_regime_policy(self):
        self.assertEqual(
            league_model_manager._expected_evaluator_regime_policy(),
            league_model_manager.htft_holdout_evaluator.COMPETITION_REGIME_POLICY,
        )
        tampered = copy.deepcopy(
            league_model_manager.htft_holdout_evaluator.COMPETITION_REGIME_POLICY
        )
        tampered["allowed_regimes_by_league"]["uefa_nations_league"] = ["regular"]
        with (
            mock.patch.object(
                league_model_manager.htft_holdout_evaluator,
                "COMPETITION_REGIME_POLICY",
                tampered,
            ),
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "does not match the registered manager",
            ),
        ):
            league_model_manager._assert_evaluator_regime_policy_matches_manager()

    def test_manager_and_evaluator_share_the_exact_fit_policy(self):
        league_model_manager._assert_evaluator_fit_policy_matches_manager()
        tampered = copy.deepcopy(
            league_model_manager.htft_holdout_evaluator.PROMOTED_FIT_CONFIG
        )
        tampered["association_half_life_days"] = 180.0
        with (
            mock.patch.object(
                league_model_manager.htft_holdout_evaluator,
                "PROMOTED_FIT_CONFIG",
                tampered,
            ),
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "fit policy does not match",
            ),
        ):
            league_model_manager._assert_evaluator_fit_policy_matches_manager()

    def test_promoted_model_rejects_uniform_or_wrong_association_decay(self):
        promoted = league_model_manager.VALIDATED_TRAINING_CONFIG
        model = _fake_model("brazil_serie_a", _hash_bytes(b"bundle"))
        league_model_manager._validate_promoted_model_config(model, promoted)

        for mode, half_life in (("none", None), ("exponential_half_life", 180.0)):
            with self.subTest(mode=mode, half_life=half_life):
                tampered = copy.deepcopy(model)
                tampered["empirical_association"]["time_decay"] = {
                    "mode": mode,
                    "half_life_days": half_life,
                }
                with self.assertRaisesRegex(
                    league_model_manager.LeagueModelManagerError,
                    "empirical association config",
                ):
                    league_model_manager._validate_promoted_model_config(
                        tampered, promoted
                    )

    @staticmethod
    def _score_row(
        league_key,
        league_name,
        *,
        match_date="2020-01-01",
        home_team="Alpha",
        away_team="Bravo",
        home_goals=1,
        away_goals=0,
        half_home_goals=0,
        half_away_goals=0,
        season=None,
        competition_regime=None,
        source_row=3,
        round_label=None,
    ):
        season = int(match_date[:4]) if season is None else season
        history_importer = league_model_manager.history_importer
        round_value = str(source_row - 2) if round_label is None else round_label
        if competition_regime is None:
            competition_regime = history_importer._competition_regime(
                league_key,
                season,
                date.fromisoformat(match_date),
                round_value,
            )

        def result(home, away):
            return "H" if home > away else "A" if home < away else "D"

        half_result = result(half_home_goals, half_away_goals)
        full_result = result(home_goals, away_goals)
        return {
            "match_id": str(9000000 + source_row),
            "date": match_date,
            "home_team": home_team,
            "away_team": away_team,
            "home_goals": home_goals,
            "away_goals": away_goals,
            "half_home_goals": half_home_goals,
            "half_away_goals": half_away_goals,
            "half_result": half_result,
            "full_result": full_result,
            "htft_result": half_result + full_result,
            "league_key": league_key,
            "league": league_name,
            "season": season,
            "competition_regime": competition_regime,
            "format_version": history_importer._format_version(league_key, season),
            "phase_group": history_importer._phase_group(league_key, round_value),
            "season_status": "",
            "round": round_value,
            "source_row": source_row,
            "source_kickoff": f"{match_date}T12:00+08:00",
            "source_timezone": "Asia/Shanghai",
            "kickoff_utc": f"{match_date}T04:00Z",
        }

    def _replace_league_rows(self, manifest, league_index, rows):
        history_importer = league_model_manager.history_importer
        summary = manifest["leagues"][league_index]
        rows = [dict(row) for row in rows]
        season_counts = {}
        for row in rows:
            season_key = str(row["season"])
            season_counts[season_key] = season_counts.get(season_key, 0) + 1
        season_completeness = {
            season: history_importer._season_completeness(
                summary["league_key"],
                int(season),
                count,
                manifest["as_of_date"],
            )
            for season, count in sorted(
                season_counts.items(), key=lambda item: int(item[0])
            )
        }
        for row in rows:
            row["season_status"] = season_completeness[str(row["season"])]["status"]

        score_path = self.dataset_dir / summary["score_dataset"]["file"]
        with score_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=history_importer.SCORE_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        market_rows = []
        for row in rows:
            for bookmaker, _label in history_importer.BOOKMAKERS:
                market_rows.append(
                    {
                        "match_id": row["match_id"],
                        "league_key": row["league_key"],
                        "league": row["league"],
                        "season": row["season"],
                        "competition_regime": row["competition_regime"],
                        "format_version": row["format_version"],
                        "phase_group": row["phase_group"],
                        "season_status": row["season_status"],
                        "round": row["round"],
                        "source_row": row["source_row"],
                        "source_kickoff": row["source_kickoff"],
                        "source_timezone": row["source_timezone"],
                        "kickoff_utc": row["kickoff_utc"],
                        "home_team": row["home_team"],
                        "away_team": row["away_team"],
                        "bookmaker": bookmaker,
                        "home_odds": "",
                        "draw_odds": "",
                        "away_odds": "",
                        "asian_home_price": "",
                        "asian_line": "",
                        "asian_away_price": "",
                        "total_over_price": "",
                        "total_line": "",
                        "total_under_price": "",
                        "opening_1x2_complete": "false",
                        "opening_asian_complete": "false",
                        "opening_total_complete": "false",
                    }
                )
        market_path = self.dataset_dir / summary["opening_market_research"]["file"]
        with market_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=history_importer.MARKET_FIELDS)
            writer.writeheader()
            writer.writerows(market_rows)

        nested_counts = {
            "competition_regimes": {},
            "format_versions": {},
            "phase_groups": {},
            "season_statuses": {},
        }
        row_fields = {
            "competition_regimes": "competition_regime",
            "format_versions": "format_version",
            "phase_groups": "phase_group",
            "season_statuses": "season_status",
        }
        for row in rows:
            season_key = str(row["season"])
            for summary_field, row_field in row_fields.items():
                by_value = nested_counts[summary_field].setdefault(season_key, {})
                value = row[row_field]
                by_value[value] = by_value.get(value, 0) + 1
        summary.update(
            {
                "rows": len(rows),
                "seasons": dict(
                    sorted(season_counts.items(), key=lambda item: int(item[0]))
                ),
                "season_completeness": season_completeness,
                "utc_date_start": min(row["date"] for row in rows),
                "utc_date_end": max(row["date"] for row in rows),
                "bookmaker_opening_completeness": {
                    bookmaker: {
                        market: {"rows": 0, "rate": 0.0}
                        for market in (
                            "opening_1x2",
                            "opening_asian",
                            "opening_total",
                        )
                    }
                    for bookmaker in sorted(
                        book for book, _label in history_importer.BOOKMAKERS
                    )
                },
            }
        )
        for summary_field, counts in nested_counts.items():
            summary[summary_field] = {
                season: dict(sorted(values.items()))
                for season, values in sorted(
                    counts.items(), key=lambda item: int(item[0])
                )
            }
        summary["score_dataset"].update(
            {"sha256": _hash_bytes(score_path.read_bytes()), "rows": len(rows)}
        )
        summary["opening_market_research"].update(
            {"sha256": _hash_bytes(market_path.read_bytes()), "rows": len(market_rows)}
        )
        manifest["competition_regime_counts"] = history_importer._flat_regime_counts(
            manifest["leagues"]
        )
        manifest["bundle_hash"] = history_importer._canonical_manifest_hash(manifest)
        (self.dataset_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return manifest

    def _write_dataset_bundle(
        self, *, all_leagues: bool = True, only_league: str | None = None
    ):
        self.dataset_dir.mkdir(parents=True)
        league_names = dict(league_model_manager.LEAGUE_NAMES)
        if only_league is not None:
            league_names = {only_league: league_names[only_league]}
        elif not all_leagues:
            league_names = {"brazil_serie_a": league_names["brazil_serie_a"]}
        specifications = {
            spec["league_key"]: (league_name, spec)
            for league_name, spec in league_model_manager.history_importer.LEAGUE_SPECS.items()
        }
        leagues = []
        for league_key, league_name in league_names.items():
            source_league_name, specification = specifications[league_key]
            self.assertEqual(source_league_name, league_name)
            output_stem = specification["filename"]
            leagues.append(
                {
                    "league_key": league_key,
                    "league": league_name,
                    "aliases": list(
                        specification.get("aliases", (league_key, league_name))
                    ),
                    "output_stem": output_stem,
                    "source_file": f"{league_key}.xlsx",
                    "source_sha256": _hash_bytes(league_key.encode()),
                    "rows": 0,
                    "seasons": {},
                    "competition_regimes": {},
                    "utc_date_start": "2020-01-01",
                    "utc_date_end": "2020-01-01",
                    "calendar_rollovers": [],
                    "bookmaker_opening_completeness": {},
                    "score_dataset": {
                        "file": f"{output_stem}-scores.csv",
                        "sha256": _hash_bytes(b""),
                        "rows": 0,
                    },
                    "opening_market_research": {
                        "file": f"{output_stem}-opening-markets.csv",
                        "sha256": _hash_bytes(b""),
                        "rows": 0,
                        "policy": "research_only_untimestamped_opening_snapshot",
                    },
                }
            )
        manifest = {
            "artifact_type": "soccer_history_dataset_bundle",
            "schema_version": league_model_manager.history_importer.DATASET_SCHEMA_VERSION,
            "importer_version": league_model_manager.history_importer.IMPORTER_VERSION,
            "as_of_date": "2026-08-03",
            "season_completeness_policy": dict(
                league_model_manager.history_importer.SEASON_COMPLETENESS_POLICY
            ),
            "administrative_result_exclusion_policy": (
                league_model_manager.history_importer._administrative_result_exclusion_policy()
            ),
            "source_timezone": "Asia/Shanghai",
            "leagues": leagues,
            "competition_regime_counts": [],
        }
        for index, summary in enumerate(leagues):
            self._replace_league_rows(
                manifest,
                index,
                [self._score_row(summary["league_key"], summary["league"])],
            )
        return manifest

    def _write_evaluation_artifact(
        self,
        manifest,
        *,
        metric_deltas=None,
        metric_ci_highs=None,
        pair_counts=None,
        omitted_leagues=(),
    ):
        metric_deltas = dict(metric_deltas or {})
        metric_ci_highs = dict(metric_ci_highs or {})
        pair_counts = dict(pair_counts or {})
        leagues = []
        for summary in manifest["leagues"]:
            league_key = summary["league_key"]
            if league_key in omitted_leagues:
                continue
            default_deltas = (
                (0.01, 0.01) if league_key == "korea_k_league_1" else (-0.01, -0.005)
            )
            log_loss_delta, brier_delta = metric_deltas.get(league_key, default_deltas)
            log_loss_ci_high, brier_ci_high = metric_ci_highs.get(
                league_key,
                (
                    log_loss_delta / 2
                    if log_loss_delta < 0
                    else log_loss_delta + 0.001,
                    brier_delta / 2 if brier_delta < 0 else brier_delta + 0.001,
                ),
            )
            eligible, covered, hits = pair_counts.get(league_key, (100, 50, 30))
            leagues.append(
                {
                    "league_key": league_key,
                    "splits": [
                        {
                            "split_id": league_model_manager.FIXED_HOLDOUT_SPLIT_ID,
                            "role": league_model_manager.FIXED_HOLDOUT_ROLE,
                            "status": "evaluated",
                            "model_minus_empirical_baseline": {
                                "sample_count": eligible,
                                "nine_class_log_loss": {
                                    "mean_delta": log_loss_delta,
                                    "ci95_high": log_loss_ci_high,
                                },
                                "nine_class_brier": {
                                    "mean_delta": brier_delta,
                                    "ci95_high": brier_ci_high,
                                },
                            },
                            "model_minus_empirical_baseline_by_team_availability": {
                                "known_teams": {
                                    "sample_count": eligible,
                                    "nine_class_log_loss": {
                                        "mean_delta": log_loss_delta,
                                        "ci95_high": log_loss_ci_high,
                                    },
                                    "nine_class_brier": {
                                        "mean_delta": brier_delta,
                                        "ci95_high": brier_ci_high,
                                    },
                                }
                            },
                            "model_only": {
                                "overall": {
                                    "pair_mass_gate": {
                                        "threshold": 0.46,
                                        "eligible_sample_count": eligible,
                                        "covered_count": covered,
                                        "hit_count": hits,
                                    }
                                },
                                "known_teams": {
                                    "pair_mass_gate": {
                                        "threshold": 0.46,
                                        "eligible_sample_count": eligible,
                                        "covered_count": covered,
                                        "hit_count": hits,
                                    }
                                },
                            },
                        }
                    ],
                }
            )
        evaluation = {
            "artifact_type": league_model_manager.htft_holdout_evaluator.ARTIFACT_TYPE,
            "schema_version": league_model_manager.htft_holdout_evaluator.SCHEMA_VERSION,
            "dataset": {"manifest_bundle_hash": manifest["bundle_hash"]},
            "fit_config": dict(
                league_model_manager.htft_holdout_evaluator.PROMOTED_FIT_CONFIG
            ),
            "promotion": {"registered_manager_compatible": True},
            "leagues": leagues,
        }
        evaluation["evaluation_hash"] = (
            league_model_manager.htft_holdout_evaluator.calculate_evaluation_hash(
                evaluation
            )
        )
        self.evaluation_path.write_text(
            json.dumps(evaluation, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.last_evaluation = evaluation
        return evaluation

    def _train_with_fake_models(
        self,
        *,
        all_leagues: bool = True,
        only_league: str | None = None,
        metric_deltas=None,
        metric_ci_highs=None,
        pair_counts=None,
    ):
        manifest = self._write_dataset_bundle(
            all_leagues=all_leagues, only_league=only_league
        )
        self._write_evaluation_artifact(
            manifest,
            metric_deltas=metric_deltas,
            metric_ci_highs=metric_ci_highs,
            pair_counts=pair_counts,
        )

        def fit_model(_path, **kwargs):
            return _fake_model(
                kwargs["competition_key"], manifest["bundle_hash"], rows=1
            )

        with (
            mock.patch.object(
                league_model_manager.htft_model,
                "fit_model",
                side_effect=fit_model,
            ) as fitted,
            mock.patch.object(league_model_manager.htft_model, "validate_model"),
            mock.patch.object(
                league_model_manager.htft_holdout_evaluator,
                "validate_evaluation",
            ),
        ):
            registry = league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )
        return manifest, registry, fitted.call_args_list

    def test_train_registers_all_supported_competitions_with_validated_defaults(self):
        manifest, registry, calls = self._train_with_fake_models()

        expected_count = len(league_model_manager.LEAGUE_NAMES)
        self.assertEqual(len(calls), expected_count)
        self.assertEqual(len(registry["leagues"]), expected_count)
        self.assertEqual(registry["dataset_manifest_hash"], manifest["bundle_hash"])
        self.assertEqual(
            registry["evaluation_hash"], self.last_evaluation["evaluation_hash"]
        )
        self.assertEqual(
            registry["registry_hash"],
            league_model_manager.calculate_registry_hash(registry),
        )
        self.assertEqual(
            registry["deployment_policy"],
            league_model_manager.DEPLOYMENT_POLICY,
        )
        self.assertTrue((self.model_dir / "registry.json").is_file())
        self.assertEqual(league_model_manager.load_registry(self.model_dir), registry)
        self.assertEqual(
            {call.kwargs["competition_key"] for call in calls},
            set(league_model_manager.LEAGUE_NAMES),
        )
        for call in calls:
            kwargs = call.kwargs
            self.assertEqual(kwargs["half_time_half_life_days"], 730.0)
            self.assertEqual(kwargs["second_half_half_life_days"], 365.0)
            self.assertEqual(kwargs["full_time_half_life_days"], 365.0)
            self.assertEqual(kwargs["association_smoothing_alpha"], 0.5)
            self.assertEqual(kwargs["association_power"], 1.0)
            self.assertEqual(kwargs["association_half_life_days"], 365.0)
            self.assertEqual(kwargs["iterations"], 1200)
            self.assertEqual(kwargs["dataset_manifest_hash"], manifest["bundle_hash"])
        for entry in registry["leagues"]:
            self.assertIn(entry["league_key"], entry["aliases"])
            self.assertIn(entry["league"], entry["aliases"])
            self.assertTrue((self.model_dir / entry["model_file"]).is_file())
            self.assertRegex(entry["dataset_file_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(entry["model_hash"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(entry["training_cutoff"], "2025-12-31")
            self.assertEqual(
                entry["deployment_status"],
                "shadow" if entry["league_key"] == "korea_k_league_1" else "candidate",
            )
            self.assertIs(entry["formal_htft_eligible"], False)
            self.assertEqual(
                entry["formal_htft_ineligible_reason"],
                league_model_manager.FORMAL_HTFT_INELIGIBLE_REASON,
            )
            self.assertEqual(
                entry["deployment_policy_version"],
                league_model_manager.DEPLOYMENT_POLICY_VERSION,
            )
            expected_regimes = league_model_manager._production_training_regimes(
                entry["league_key"]
            )
            self.assertEqual(
                entry["competition_regime_policy"]["allowed_regimes"],
                list(expected_regimes),
            )
            self.assertEqual(
                set(entry["competition_regime_policy"]["included_regime_counts"]),
                set(expected_regimes),
            )
            self.assertEqual(
                entry["competition_regime_policy"]["excluded_regime_counts"],
                {},
            )
            evidence = entry["league_pair_gate_evidence"]
            self.assertEqual(
                set(evidence),
                league_model_manager.LEAGUE_PAIR_GATE_EVIDENCE_FIELDS,
            )
            self.assertEqual(evidence["dataset_manifest_hash"], manifest["bundle_hash"])
            self.assertEqual(
                evidence["evaluation_hash"], self.last_evaluation["evaluation_hash"]
            )
            self.assertEqual(evidence["model_hash"], entry["model_hash"])
            self.assertEqual(evidence["deployment_status"], entry["deployment_status"])
            self.assertEqual(evidence["threshold"], 0.46)
            self.assertEqual(evidence["eligible_sample_count"], 100)
            self.assertEqual(evidence["covered_count"], 50)
            self.assertEqual(evidence["hit_count"], 30)
            self.assertIs(evidence["formal_htft_eligible"], False)
            self.assertIs(evidence["production_confidence_eligible"], False)

        finland = next(
            entry
            for entry in registry["leagues"]
            if entry["league_key"] == "finland_veikkausliiga"
        )
        self.assertEqual(finland["league"], "芬超")
        self.assertIn("Veikkausliiga", finland["aliases"])

    def test_train_reopens_evaluation_sources_against_the_dataset_directory(self):
        manifest = self._write_dataset_bundle(all_leagues=False)
        evaluation = self._write_evaluation_artifact(manifest)

        def fit_model(_path, **kwargs):
            return _fake_model(
                kwargs["competition_key"], manifest["bundle_hash"], rows=1
            )

        with (
            mock.patch.object(
                league_model_manager.htft_holdout_evaluator,
                "validate_evaluation",
            ) as validated,
            mock.patch.object(
                league_model_manager.htft_model,
                "fit_model",
                side_effect=fit_model,
            ),
            mock.patch.object(league_model_manager.htft_model, "validate_model"),
        ):
            registry = league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )

        validated.assert_called_once_with(
            evaluation,
            dataset_dir=self.dataset_dir.resolve(),
        )
        self.assertEqual(registry["evaluation_hash"], evaluation["evaluation_hash"])

    def test_invalid_source_bound_evaluation_is_rejected_before_model_fit(self):
        manifest = self._write_dataset_bundle(all_leagues=False)
        self._write_evaluation_artifact(manifest)

        with (
            mock.patch.object(
                league_model_manager.htft_holdout_evaluator,
                "validate_evaluation",
                side_effect=league_model_manager.htft_holdout_evaluator.HoldoutEvaluationError(
                    "source fixture hash mismatch"
                ),
            ),
            mock.patch.object(league_model_manager.htft_model, "fit_model") as fitted,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "source-bound HT/FT evaluation is invalid",
            ),
        ):
            league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )
        fitted.assert_not_called()

    def test_evaluation_manifest_hash_and_league_coverage_must_match(self):
        manifest = self._write_dataset_bundle(all_leagues=False)
        evaluation = self._write_evaluation_artifact(manifest)
        evaluation["dataset"]["manifest_bundle_hash"] = _hash_bytes(b"wrong")
        evaluation["evaluation_hash"] = (
            league_model_manager.htft_holdout_evaluator.calculate_evaluation_hash(
                evaluation
            )
        )
        self.evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
        with (
            mock.patch.object(
                league_model_manager.htft_holdout_evaluator,
                "validate_evaluation",
            ),
            mock.patch.object(league_model_manager.htft_model, "fit_model") as fitted,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "manifest hash does not match",
            ),
        ):
            league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )
        fitted.assert_not_called()

        evaluation = self._write_evaluation_artifact(
            manifest, omitted_leagues={"brazil_serie_a"}
        )
        with (
            mock.patch.object(
                league_model_manager.htft_holdout_evaluator,
                "validate_evaluation",
            ),
            mock.patch.object(league_model_manager.htft_model, "fit_model") as fitted,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "league coverage does not match",
            ),
        ):
            league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )
        fitted.assert_not_called()

    def test_deployment_status_depends_on_metrics_not_league_name(self):
        _manifest, registry, _calls = self._train_with_fake_models(
            metric_deltas={
                "afc_champions_league": (0.001, 0.001),
                "korea_k_league_1": (-0.001, -0.001),
            }
        )
        statuses = {
            entry["league_key"]: entry["deployment_status"]
            for entry in registry["leagues"]
        }
        self.assertEqual(statuses["afc_champions_league"], "shadow")
        self.assertEqual(statuses["korea_k_league_1"], "candidate")

    def test_candidate_requires_negative_ci_upper_bounds_and_minimum_sample(self):
        _manifest, registry, _calls = self._train_with_fake_models(
            all_leagues=False,
            metric_deltas={"brazil_serie_a": (-0.02, -0.01)},
            metric_ci_highs={"brazil_serie_a": (0.001, -0.001)},
        )
        self.assertEqual(registry["leagues"][0]["deployment_status"], "shadow")

        decision = copy.deepcopy(registry["leagues"][0]["fixed_holdout_evaluation"])
        decision["sample_count"] = 99
        decision["nine_class_log_loss_ci95_high"] = -0.001
        decision["nine_class_brier_ci95_high"] = -0.001
        self.assertEqual(
            league_model_manager._deployment_status_from_fixed_holdout(decision),
            "shadow",
        )

    def test_fixed_holdout_decision_uses_known_team_cohort_not_overall(self):
        manifest = self._write_dataset_bundle(all_leagues=False)
        evaluation = self._write_evaluation_artifact(
            manifest,
            metric_deltas={"brazil_serie_a": (-0.02, -0.01)},
            metric_ci_highs={"brazil_serie_a": (-0.001, -0.001)},
        )
        split = evaluation["leagues"][0]["splits"][0]
        split["model_minus_empirical_baseline"]["nine_class_log_loss"] = {
            "mean_delta": 1.0,
            "ci95_high": 1.0,
        }
        split["model_minus_empirical_baseline"]["nine_class_brier"] = {
            "mean_delta": 1.0,
            "ci95_high": 1.0,
        }
        decision, _counts = league_model_manager._fixed_holdout_evidence(
            evaluation["leagues"][0], league_key="brazil_serie_a"
        )
        self.assertEqual(
            league_model_manager._deployment_status_from_fixed_holdout(decision),
            "candidate",
        )

        known = split["model_minus_empirical_baseline_by_team_availability"][
            "known_teams"
        ]
        known["nine_class_log_loss"] = {
            "mean_delta": 0.01,
            "ci95_high": 0.02,
        }
        decision, _counts = league_model_manager._fixed_holdout_evidence(
            evaluation["leagues"][0], league_key="brazil_serie_a"
        )
        self.assertEqual(
            league_model_manager._deployment_status_from_fixed_holdout(decision),
            "shadow",
        )

    def test_train_cli_requires_a_source_evaluation_artifact(self):
        parser = league_model_manager.build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parser.parse_args(
                [
                    "train",
                    "--dataset-dir",
                    str(self.dataset_dir),
                    "--model-dir",
                    str(self.model_dir),
                ]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_registry_rejects_old_missing_or_self_promoted_deployment_state(self):
        _manifest, registry, _calls = self._train_with_fake_models(all_leagues=False)

        old_registry = copy.deepcopy(registry)
        old_registry["schema_version"] = "1.1.0"
        old_registry["registry_hash"] = league_model_manager.calculate_registry_hash(
            old_registry
        )
        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "unsupported registry schema_version",
        ):
            league_model_manager.validate_registry(old_registry)

        missing_status = copy.deepcopy(registry)
        missing_status["leagues"][0].pop("deployment_status")
        missing_status["registry_hash"] = league_model_manager.calculate_registry_hash(
            missing_status
        )
        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "deployment_status does not match fixed holdout evidence",
        ):
            league_model_manager.validate_registry(missing_status)

        self_promoted = copy.deepcopy(registry)
        self_promoted["leagues"][0]["formal_htft_eligible"] = True
        self_promoted["registry_hash"] = league_model_manager.calculate_registry_hash(
            self_promoted
        )
        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "formal_htft_eligible does not satisfy the deployment gate",
        ):
            league_model_manager.validate_registry(self_promoted)

        self_promoted_status = copy.deepcopy(registry)
        entry = self_promoted_status["leagues"][0]
        entry["deployment_status"] = "shadow"
        entry["league_pair_gate_evidence"]["deployment_status"] = "shadow"
        self_promoted_status["registry_hash"] = (
            league_model_manager.calculate_registry_hash(self_promoted_status)
        )
        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "deployment_status does not match fixed holdout evidence",
        ):
            league_model_manager.validate_registry(self_promoted_status)

        extra_evidence_field = copy.deepcopy(registry)
        extra_evidence_field["leagues"][0]["league_pair_gate_evidence"][
            "unbound_claim"
        ] = True
        extra_evidence_field["registry_hash"] = (
            league_model_manager.calculate_registry_hash(extra_evidence_field)
        )
        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "fields do not match the registry-bound evidence contract",
        ):
            league_model_manager.validate_registry(extra_evidence_field)

    def test_inspect_propagates_evaluation_derived_deployment_gates(self):
        _manifest, registry, _calls = self._train_with_fake_models()

        inspection = league_model_manager.inspect_registry(self.model_dir)
        league_model_manager.validate_registry_inspection(inspection, registry=registry)
        self.assertEqual(inspection["model_count"], len(registry["leagues"]))
        self.assertEqual(
            inspection["inspection_hash"],
            league_model_manager.calculate_registry_inspection_hash(inspection),
        )
        statuses = {
            item["league_key"]: item["deployment_status"]
            for item in inspection["models"]
        }
        self.assertEqual(statuses["korea_k_league_1"], "shadow")
        self.assertEqual(statuses["afc_champions_league"], "candidate")
        self.assertTrue(
            all(item["formal_htft_eligible"] is False for item in inspection["models"])
        )

        korea = league_model_manager.inspect_registry(self.model_dir, "韩K联")
        self.assertEqual(korea["model_count"], 1)
        self.assertEqual(korea["models"][0]["deployment_status"], "shadow")

        tampered = copy.deepcopy(korea)
        tampered["models"][0]["formal_htft_eligible"] = True
        tampered["inspection_hash"] = (
            league_model_manager.calculate_registry_inspection_hash(tampered)
        )
        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "formal_htft_eligible does not satisfy the deployment gate",
        ):
            league_model_manager.validate_registry_inspection(tampered)

    def test_cli_inspect_writes_the_validated_deployment_artifact(self):
        _manifest, registry, _calls = self._train_with_fake_models(
            only_league="afc_champions_league"
        )
        output_path = self.base / "afc-inspection.json"
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = league_model_manager.main(
                [
                    "inspect",
                    "--model-dir",
                    str(self.model_dir),
                    "--league",
                    "亚冠",
                    "--output",
                    str(output_path),
                ]
            )

        self.assertEqual(exit_code, 0)
        saved = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(saved, json.loads(stdout.getvalue()))
        self.assertEqual(saved["models"][0]["deployment_status"], "candidate")
        self.assertIs(saved["models"][0]["formal_htft_eligible"], False)
        league_model_manager.validate_registry_inspection(saved, registry=registry)

    def test_special_competition_regime_is_excluded_from_registered_training(self):
        manifest = self._write_dataset_bundle(all_leagues=False, only_league="japan_j1")
        league = manifest["leagues"][0]
        rows = [
            self._score_row(
                "japan_j1",
                league["league"],
                match_date="2025-12-06",
                competition_regime="regular",
                source_row=3,
            ),
            self._score_row(
                "japan_j1",
                league["league"],
                match_date="2026-02-06",
                season=2026,
                competition_regime="2026_vision_regional",
                source_row=4,
            ),
        ]
        self._replace_league_rows(manifest, 0, rows)
        self._write_evaluation_artifact(manifest)
        observed_training_rows = []

        def fit_model(path, **kwargs):
            with Path(path).open("r", encoding="utf-8", newline="") as handle:
                observed_training_rows.extend(csv.DictReader(handle))
            return _fake_model(
                kwargs["competition_key"], manifest["bundle_hash"], rows=1
            )

        with (
            mock.patch.object(
                league_model_manager.htft_model,
                "fit_model",
                side_effect=fit_model,
            ),
            mock.patch.object(league_model_manager.htft_model, "validate_model"),
            mock.patch.object(
                league_model_manager.htft_holdout_evaluator,
                "validate_evaluation",
            ),
        ):
            registry = league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )

        self.assertEqual(len(observed_training_rows), 1)
        self.assertEqual(observed_training_rows[0]["competition_regime"], "regular")
        entry = registry["leagues"][0]
        self.assertEqual(entry["dataset_rows"], 2)
        self.assertEqual(entry["training_rows"], 1)
        self.assertEqual(entry["excluded_training_rows"], 1)
        self.assertEqual(
            entry["competition_regime_policy"]["excluded_regime_counts"],
            {"2026_vision_regional": 1},
        )
        self.assertEqual(
            entry["league_pair_gate_evidence"]["regime_warning"],
            league_model_manager.SPECIAL_REGIME_WARNING,
        )

    def test_norway_relegation_playoff_is_excluded_from_registered_training(self):
        manifest = self._write_dataset_bundle(
            all_leagues=False, only_league="norway_eliteserien"
        )
        league = manifest["leagues"][0]
        rows = [
            self._score_row(
                "norway_eliteserien",
                league["league"],
                match_date="2025-11-30",
                source_row=3,
                round_label="30",
            ),
            self._score_row(
                "norway_eliteserien",
                league["league"],
                match_date="2025-12-07",
                source_row=4,
                round_label="保级附加赛 第1轮",
            ),
        ]
        self.assertEqual(rows[1]["competition_regime"], "relegation_playoff")
        self.assertEqual(rows[1]["phase_group"], "relegation_playoff")
        self._replace_league_rows(manifest, 0, rows)
        self._write_evaluation_artifact(manifest)
        observed_training_rows = []

        def fit_model(path, **kwargs):
            with Path(path).open("r", encoding="utf-8", newline="") as handle:
                observed_training_rows.extend(csv.DictReader(handle))
            return _fake_model(
                kwargs["competition_key"], manifest["bundle_hash"], rows=1
            )

        with (
            mock.patch.object(
                league_model_manager.htft_model,
                "fit_model",
                side_effect=fit_model,
            ),
            mock.patch.object(league_model_manager.htft_model, "validate_model"),
            mock.patch.object(
                league_model_manager.htft_holdout_evaluator,
                "validate_evaluation",
            ),
        ):
            registry = league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )

        self.assertEqual(len(observed_training_rows), 1)
        self.assertEqual(observed_training_rows[0]["round"], "30")
        entry = registry["leagues"][0]
        self.assertEqual(entry["training_rows"], 1)
        self.assertEqual(entry["excluded_training_rows"], 1)
        self.assertEqual(
            entry["competition_regime_policy"]["excluded_regime_counts"],
            {"relegation_playoff": 1},
        )

    def test_rehashed_norway_playoff_regime_tampering_is_rejected(self):
        manifest = self._write_dataset_bundle(
            all_leagues=False, only_league="norway_eliteserien"
        )
        league = manifest["leagues"][0]
        tampered_rows = [
            self._score_row(
                "norway_eliteserien",
                league["league"],
                match_date="2025-12-07",
                competition_regime="regular",
                round_label="保级附加赛 第1轮",
            )
        ]
        self._replace_league_rows(manifest, 0, tampered_rows)
        self._write_evaluation_artifact(manifest)

        with (
            mock.patch.object(league_model_manager.htft_model, "fit_model") as fitted,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "competition_regime must be relegation_playoff",
            ),
        ):
            league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )
        fitted.assert_not_called()

    def test_modified_dataset_is_rejected_before_training(self):
        manifest = self._write_dataset_bundle(all_leagues=False)
        self._write_evaluation_artifact(manifest)
        dataset = next(self.dataset_dir.glob("*-scores.csv"))
        dataset.write_text("tampered\n", encoding="utf-8")

        with (
            mock.patch.object(league_model_manager.htft_model, "fit_model") as fitted,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "score dataset hash mismatch",
            ),
        ):
            league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )
        fitted.assert_not_called()

    def test_rehashed_regime_tampering_is_rejected_before_training(self):
        manifest = self._write_dataset_bundle(all_leagues=False, only_league="japan_j1")
        league = manifest["leagues"][0]
        tampered_rows = [
            self._score_row(
                "japan_j1",
                league["league"],
                match_date="2026-02-06",
                season=2026,
                competition_regime="regular",
            )
        ]
        # Rebuild both CSV hashes, all summary counts and the manifest hash.
        # Only the importer's source-date policy can detect this tampering.
        self._replace_league_rows(manifest, 0, tampered_rows)
        self._write_evaluation_artifact(manifest)

        with (
            mock.patch.object(league_model_manager.htft_model, "fit_model") as fitted,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "competition_regime must be 2026_vision_regional",
            ),
        ):
            league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )
        fitted.assert_not_called()

    def test_registered_training_rejects_unpromoted_hyperparameters(self):
        self._write_dataset_bundle(all_leagues=False)
        with (
            mock.patch.object(league_model_manager.htft_model, "fit_model") as fitted,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "locked to the promoted",
            ),
        ):
            league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
                iterations=7,
            )
        fitted.assert_not_called()

    def test_registration_rejects_model_with_unapproved_actual_config(self):
        manifest = self._write_dataset_bundle(all_leagues=False)
        self._write_evaluation_artifact(manifest)

        def fit_model(_path, **kwargs):
            model = _fake_model(
                kwargs["competition_key"], manifest["bundle_hash"], rows=1
            )
            model["config"]["score_models"]["second_half"]["iterations"] = 7
            return model

        with (
            mock.patch.object(
                league_model_manager.htft_model,
                "fit_model",
                side_effect=fit_model,
            ),
            mock.patch.object(league_model_manager.htft_model, "validate_model"),
            mock.patch.object(
                league_model_manager.htft_holdout_evaluator,
                "validate_evaluation",
            ),
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "actual training config is not the approved",
            ),
        ):
            league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )

    def test_loading_rejects_rehashed_model_with_unapproved_actual_config(self):
        manifest, registry, _calls = self._train_with_fake_models(all_leagues=False)
        entry = registry["leagues"][0]
        model = _fake_model("brazil_serie_a", manifest["bundle_hash"])
        model["config"]["score_models"]["full_time"]["regularization"] = 0.5
        model["model_hash"] = league_model_manager.htft_model.calculate_model_hash(
            model
        )
        model_path = self.model_dir / entry["model_file"]
        model_path.write_bytes(league_model_manager._json_bytes(model))
        entry["model_hash"] = model["model_hash"]
        entry["league_pair_gate_evidence"]["model_hash"] = model["model_hash"]
        entry["model_file_sha256"] = league_model_manager._file_hash(model_path)
        registry["registry_hash"] = league_model_manager.calculate_registry_hash(
            registry
        )
        (self.model_dir / "registry.json").write_text(
            json.dumps(registry, ensure_ascii=False), encoding="utf-8"
        )

        with (
            mock.patch.object(
                league_model_manager.htft_model,
                "load_model",
                return_value=model,
            ),
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "actual training config is not the approved",
            ),
        ):
            league_model_manager.predict_registered_model(
                self.model_dir,
                "brazil_serie_a",
                "Alpha",
                "Bravo",
                kickoff="2030-01-01T12:00:00Z",
                generated_at="2029-12-31T12:00:00Z",
            )

    def test_modified_registry_hash_is_rejected(self):
        _manifest, registry, _calls = self._train_with_fake_models(all_leagues=False)
        registry["leagues"][0]["aliases"].append("伪别名")
        (self.model_dir / "registry.json").write_text(
            json.dumps(registry, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "registry_hash does not match",
        ):
            league_model_manager.load_registry(self.model_dir)

    def test_modified_model_file_hash_is_rejected_before_loading(self):
        _manifest, registry, _calls = self._train_with_fake_models(all_leagues=False)
        model_path = self.model_dir / registry["leagues"][0]["model_file"]
        model_path.write_bytes(model_path.read_bytes() + b" ")

        with (
            mock.patch.object(league_model_manager.htft_model, "load_model") as loaded,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "registered model file hash does not match",
            ),
        ):
            league_model_manager.predict_registered_model(
                self.model_dir,
                "巴甲",
                "甲",
                "乙",
                kickoff="2030-01-01T12:00:00Z",
                generated_at="2029-12-31T12:00:00Z",
            )
        loaded.assert_not_called()

    def test_model_with_the_wrong_competition_is_rejected(self):
        manifest, registry, _calls = self._train_with_fake_models(all_leagues=False)
        entry = registry["leagues"][0]
        wrong_model = _fake_model("japan_j1", manifest["bundle_hash"])
        wrong_model["model_hash"] = entry["model_hash"]

        with (
            mock.patch.object(
                league_model_manager.htft_model,
                "load_model",
                return_value=wrong_model,
            ),
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "competition_key does not match",
            ),
        ):
            league_model_manager.predict_registered_model(
                self.model_dir,
                "巴甲",
                "甲",
                "乙",
                kickoff="2030-01-01T12:00:00Z",
                generated_at="2029-12-31T12:00:00Z",
            )

        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "league is not registered",
        ):
            league_model_manager.predict_registered_model(
                self.model_dir,
                "韩K联",
                "甲",
                "乙",
                kickoff="2030-01-01T12:00:00Z",
                generated_at="2029-12-31T12:00:00Z",
            )

    def test_half_time_anchor_and_full_market_requests_are_forwarded(self):
        manifest, registry, _calls = self._train_with_fake_models(
            only_league="korea_k_league_1"
        )
        model = _fake_model("korea_k_league_1", manifest["bundle_hash"])
        probabilities = {"home": 0.44, "draw": 0.31, "away": 0.25}
        half_anchor = {
            "probabilities": {"home": 0.30, "draw": 0.45, "away": 0.25},
            "source": "verified-current-market",
            "captured_at": "2029-12-31T10:00:00Z",
            "de_vigged": True,
        }
        htft_prediction = {
            "artifact_type": "soccer_htft_prediction",
            "schema_version": "1.0.0",
            "model_version": "htft-dixon-coles-ipf/1.0.0",
            "model_hash": model["model_hash"],
            "generated_at": "2029-12-31T12:00:00Z",
            "fixture": {
                "home_team": "甲",
                "away_team": "乙",
                "kickoff": "2030-01-01T12:00:00Z",
                "competition_key": "korea_k_league_1",
                "unknown_team_policy": "error",
            },
            "provenance": {"training_cutoff_date": "2025-12-31"},
            "components": {
                "full_time": {
                    "model_hash": model["components"]["full_time"]["model_hash"],
                    "one_x_two": probabilities,
                }
            },
            "htft": {"full_time_marginal": probabilities},
        }
        htft_prediction["prediction_hash"] = (
            league_model_manager.htft_model.calculate_prediction_hash(htft_prediction)
        )
        score_prediction = {
            "artifact_type": "soccer_score_prediction",
            "model_hash": model["components"]["full_time"]["model_hash"],
            "generated_at": "2029-12-31T12:00:00Z",
            "fixture": {
                "home_team": "甲",
                "away_team": "乙",
                "kickoff": "2030-01-01T12:00:00Z",
                "unknown_team_policy": "error",
            },
            "provenance": {"training_cutoff_date": "2025-12-31"},
            "one_x_two": probabilities,
        }

        with (
            mock.patch.object(
                league_model_manager.htft_model,
                "load_model",
                return_value=model,
            ),
            mock.patch.object(
                league_model_manager.htft_model,
                "predict_model",
                return_value=htft_prediction,
            ) as htft_predict,
            mock.patch.object(
                league_model_manager.score_model,
                "predict_model",
                return_value=score_prediction,
            ) as score_predict,
            mock.patch.object(league_model_manager.htft_model, "validate_prediction"),
            mock.patch.object(league_model_manager, "validate_score_prediction"),
        ):
            bundle = league_model_manager.predict_registered_model(
                self.model_dir,
                "韩K联",
                "甲",
                "乙",
                kickoff="2030-01-01T12:00:00Z",
                generated_at="2029-12-31T12:00:00Z",
                half_time_anchor=half_anchor,
                total_markets=(("over", 2.5),),
                asian_handicaps=(("home", -0.5),),
            )

        self.assertIs(htft_predict.call_args.kwargs["half_time_anchor"], half_anchor)
        self.assertIsNone(htft_predict.call_args.kwargs["full_time_anchor"])
        self.assertEqual(
            score_predict.call_args.kwargs["total_markets"], (("over", 2.5),)
        )
        self.assertEqual(
            score_predict.call_args.kwargs["asian_handicaps"], (("home", -0.5),)
        )
        self.assertIs(score_predict.call_args.args[0], model["components"]["full_time"])
        self.assertIs(bundle["htft_prediction"], htft_prediction)
        self.assertIs(bundle["score_prediction"], score_prediction)
        output_manifest = bundle["manifest"]
        self.assertTrue(output_manifest["full_time_probability_consistency"]["checked"])
        self.assertEqual(
            output_manifest["external_anchors"],
            {"half_time": True, "full_time": False},
        )
        self.assertEqual(output_manifest["registry_hash"], registry["registry_hash"])
        for artifact in (
            output_manifest,
            bundle["htft_prediction"],
            bundle["score_prediction"],
        ):
            self.assertEqual(artifact["deployment_status"], "shadow")
            self.assertIs(artifact["formal_htft_eligible"], False)
            self.assertEqual(
                artifact["formal_htft_ineligible_reason"],
                league_model_manager.FORMAL_HTFT_INELIGIBLE_REASON,
            )
            self.assertEqual(
                artifact["deployment_policy_version"],
                league_model_manager.DEPLOYMENT_POLICY_VERSION,
            )
        self.assertEqual(
            output_manifest["prediction_bundle_hash"],
            league_model_manager.calculate_prediction_bundle_hash(output_manifest),
        )
        self.assertRegex(
            output_manifest["artifacts"]["canonical_score"]["artifact_hash"],
            r"^sha256:[0-9a-f]{64}$",
        )
        tampered_score = copy.deepcopy(score_prediction)
        tampered_score["tampered"] = True
        with (
            mock.patch.object(league_model_manager.htft_model, "validate_prediction"),
            mock.patch.object(league_model_manager, "validate_score_prediction"),
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "manifest score artifact hash does not match",
            ),
        ):
            league_model_manager.validate_prediction_bundle(
                output_manifest,
                htft_prediction,
                tampered_score,
                registry=registry,
            )

        self_promoted_manifest = copy.deepcopy(output_manifest)
        self_promoted_manifest["formal_htft_eligible"] = True
        self_promoted_manifest["prediction_bundle_hash"] = (
            league_model_manager.calculate_prediction_bundle_hash(
                self_promoted_manifest
            )
        )
        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "formal_htft_eligible does not satisfy the deployment gate",
        ):
            league_model_manager.validate_prediction_bundle(
                self_promoted_manifest,
                htft_prediction,
                score_prediction,
                registry=registry,
            )

    def test_full_time_anchor_is_rejected_to_preserve_one_matrix(self):
        full_anchor = {
            "probabilities": {"home": 0.50, "draw": 0.30, "away": 0.20},
            "source": "market",
            "captured_at": "2029-12-31T10:00:00Z",
            "de_vigged": True,
        }
        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "canonical score matrix",
        ):
            league_model_manager.predict_registered_model(
                self.model_dir,
                "巴甲",
                "甲",
                "乙",
                kickoff="2030-01-01T12:00:00Z",
                full_time_anchor=full_anchor,
            )

    def test_prediction_rejects_full_time_probability_divergence(self):
        manifest, _registry, _calls = self._train_with_fake_models(all_leagues=False)
        model = _fake_model("brazil_serie_a", manifest["bundle_hash"])
        htft_prediction = {
            "artifact_type": "soccer_htft_prediction",
            "prediction_hash": _hash_bytes(b"htft"),
            "generated_at": "2029-12-31T12:00:00Z",
            "fixture": {"competition_key": "brazil_serie_a"},
            "components": {
                "full_time": {"one_x_two": {"home": 0.4, "draw": 0.3, "away": 0.3}}
            },
            "htft": {
                "full_time_marginal": {
                    "home": 0.4,
                    "draw": 0.3,
                    "away": 0.3,
                }
            },
        }
        score_prediction = {
            "artifact_type": "soccer_score_prediction",
            "one_x_two": {"home": 0.5, "draw": 0.25, "away": 0.25},
        }
        with (
            mock.patch.object(
                league_model_manager.htft_model,
                "load_model",
                return_value=model,
            ),
            mock.patch.object(
                league_model_manager.htft_model,
                "predict_model",
                return_value=htft_prediction,
            ),
            mock.patch.object(
                league_model_manager.score_model,
                "predict_model",
                return_value=score_prediction,
            ),
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "probabilities disagree",
            ),
        ):
            league_model_manager.predict_registered_model(
                self.model_dir,
                "巴甲",
                "甲",
                "乙",
                kickoff="2030-01-01T12:00:00Z",
                generated_at="2029-12-31T12:00:00Z",
            )

    def test_registered_cutoff_must_be_strictly_before_kickoff(self):
        self._train_with_fake_models(all_leagues=False)
        with (
            mock.patch.object(league_model_manager.htft_model, "load_model") as loaded,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "training cutoff must be strictly before kickoff",
            ),
        ):
            league_model_manager.predict_registered_model(
                self.model_dir,
                "巴甲",
                "甲",
                "乙",
                kickoff="2025-12-31T23:00:00Z",
                generated_at="2025-12-30T12:00:00Z",
            )
        loaded.assert_not_called()

    def test_unknown_team_failure_is_preserved_by_the_manager(self):
        manifest, _registry, _calls = self._train_with_fake_models(all_leagues=False)
        model = _fake_model("brazil_serie_a", manifest["bundle_hash"])
        with (
            mock.patch.object(
                league_model_manager.htft_model,
                "load_model",
                return_value=model,
            ),
            mock.patch.object(
                league_model_manager.htft_model,
                "predict_model",
                side_effect=league_model_manager.htft_model.HTFTModelError(
                    "unknown team: 升班马"
                ),
            ) as predicted,
            mock.patch.object(
                league_model_manager.score_model, "predict_model"
            ) as score_predict,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "unknown team: 升班马",
            ),
        ):
            league_model_manager.predict_registered_model(
                self.model_dir,
                "巴甲",
                "升班马",
                "乙",
                kickoff="2030-01-01T12:00:00Z",
                generated_at="2029-12-31T12:00:00Z",
                unknown_team_policy="error",
            )
        self.assertEqual(predicted.call_args.kwargs["unknown_team_policy"], "error")
        score_predict.assert_not_called()

    def test_registered_predictions_reject_experimental_seed(self):
        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "require empirical_association",
        ):
            league_model_manager.predict_registered_model(
                self.model_dir,
                "巴甲",
                "甲",
                "乙",
                kickoff="2030-01-01T12:00:00Z",
                seed_method="experimental_score_convolution",
            )

    def test_cli_score_output_requires_a_bundle_manifest(self):
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            league_model_manager.main(
                [
                    "predict",
                    "--model-dir",
                    str(self.model_dir),
                    "--league",
                    "巴甲",
                    "--home-team",
                    "甲",
                    "--away-team",
                    "乙",
                    "--kickoff",
                    "2030-01-01T12:00:00Z",
                    "--output",
                    str(self.base / "htft.json"),
                    "--score-output",
                    str(self.base / "score.json"),
                ]
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--score-output requires --manifest-output", error.getvalue())

    def test_small_real_model_round_trip_validates_both_prediction_artifacts(self):
        manifest = self._write_dataset_bundle(all_leagues=False)
        rows = [
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
        league = manifest["leagues"][0]
        score_rows = [
            self._score_row(
                "brazil_serie_a",
                league["league"],
                match_date=row[0],
                home_team=row[1],
                away_team=row[2],
                home_goals=row[3],
                away_goals=row[4],
                half_home_goals=row[5],
                half_away_goals=row[6],
                source_row=index + 3,
            )
            for index, row in enumerate(rows)
        ]
        self._replace_league_rows(manifest, 0, score_rows)
        self._write_evaluation_artifact(
            manifest,
            pair_counts={"brazil_serie_a": (12, 6, 4)},
        )

        with mock.patch.object(
            league_model_manager.htft_holdout_evaluator,
            "validate_evaluation",
        ):
            registry = league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                evaluation_artifact=self.evaluation_path,
            )
        bundle = league_model_manager.predict_registered_model(
            self.model_dir,
            "巴甲",
            "Alpha",
            "Bravo",
            kickoff="2099-01-01T12:00:00Z",
            generated_at="2098-12-31T00:00:00Z",
            total_markets=(("over", 2.5),),
            asian_handicaps=(("home", -0.5),),
        )

        league_model_manager.validate_prediction_bundle(
            bundle["manifest"],
            bundle["htft_prediction"],
            bundle["score_prediction"],
            registry=registry,
        )
        for result in ("home", "draw", "away"):
            self.assertAlmostEqual(
                bundle["htft_prediction"]["htft"]["full_time_marginal"][result],
                bundle["score_prediction"]["one_x_two"][result],
                places=12,
            )
        self.assertIn("over_+2.5", bundle["score_prediction"]["totals"])
        self.assertIn("home_-0.5", bundle["score_prediction"]["asian_handicaps"])

        tampered_score = copy.deepcopy(bundle["score_prediction"])
        tampered_score["goal_ranges"]["0-1"] += 0.01
        tampered_manifest = copy.deepcopy(bundle["manifest"])
        score_record = tampered_manifest["artifacts"]["canonical_score"]
        score_record["artifact_hash"] = league_model_manager._canonical_hash(
            tampered_score
        )
        score_record["file_sha256"] = league_model_manager._serialized_file_hash(
            tampered_score
        )
        tampered_manifest["prediction_bundle_hash"] = (
            league_model_manager.calculate_prediction_bundle_hash(tampered_manifest)
        )
        with self.assertRaisesRegex(
            league_model_manager.LeagueModelManagerError,
            "goal_ranges",
        ):
            league_model_manager.validate_prediction_bundle(
                tampered_manifest,
                bundle["htft_prediction"],
                tampered_score,
                registry=registry,
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
from contextlib import redirect_stderr
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "league_model_manager.py"
)
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
            "full_time": {
                "model_hash": _hash_bytes(f"full-time:{league_key}".encode())
            }
        },
        "empirical_association": {
            "smoothing_alpha": promoted["association_smoothing_alpha"],
            "power": promoted["association_power"],
        },
        "construction": {
            "validated_configuration": {
                "half_time_half_life_days": promoted[
                    "half_time_half_life_days"
                ],
                "full_time_half_life_days": promoted[
                    "full_time_half_life_days"
                ],
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

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
        competition_regime="regular",
        source_row=3,
    ):
        season = int(match_date[:4]) if season is None else season

        def result(home, away):
            return "H" if home > away else "A" if home < away else "D"

        half_result = result(half_home_goals, half_away_goals)
        full_result = result(home_goals, away_goals)
        return {
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
            "round": str(source_row - 2),
            "source_row": source_row,
            "source_kickoff": f"{match_date}T12:00+08:00",
            "source_timezone": "Asia/Shanghai",
            "kickoff_utc": f"{match_date}T04:00Z",
        }

    def _replace_league_rows(self, manifest, league_index, rows):
        history_importer = league_model_manager.history_importer
        summary = manifest["leagues"][league_index]
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
                        "league_key": row["league_key"],
                        "league": row["league"],
                        "season": row["season"],
                        "competition_regime": row["competition_regime"],
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

        season_counts = {}
        regime_counts = {}
        for row in rows:
            season_key = str(row["season"])
            season_counts[season_key] = season_counts.get(season_key, 0) + 1
            by_regime = regime_counts.setdefault(season_key, {})
            regime = row["competition_regime"]
            by_regime[regime] = by_regime.get(regime, 0) + 1
        summary.update(
            {
                "rows": len(rows),
                "seasons": dict(
                    sorted(season_counts.items(), key=lambda item: int(item[0]))
                ),
                "competition_regimes": {
                    season: dict(sorted(regimes.items()))
                    for season, regimes in sorted(
                        regime_counts.items(), key=lambda item: int(item[0])
                    )
                },
                "utc_date_start": min(row["date"] for row in rows),
                "utc_date_end": max(row["date"] for row in rows),
            }
        )
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
        leagues = []
        for league_key, league_name in league_names.items():
            leagues.append(
                {
                    "league_key": league_key,
                    "league": league_name,
                    "output_stem": league_key,
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
                        "file": f"{league_key}-scores.csv",
                        "sha256": _hash_bytes(b""),
                        "rows": 0,
                    },
                    "opening_market_research": {
                        "file": f"{league_key}-opening-markets.csv",
                        "sha256": _hash_bytes(b""),
                        "rows": 0,
                        "policy": "research_only_untimestamped_opening_snapshot",
                    },
                }
            )
        manifest = {
            "artifact_type": "soccer_history_dataset_bundle",
            "schema_version": "1.0.0",
            "importer_version": league_model_manager.history_importer.IMPORTER_VERSION,
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

    def _train_with_fake_models(self, *, all_leagues: bool = True):
        manifest = self._write_dataset_bundle(all_leagues=all_leagues)

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
            mock.patch.object(
                league_model_manager.htft_model, "validate_model"
            ),
        ):
            registry = league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
            )
        return manifest, registry, fitted.call_args_list

    def test_train_registers_all_four_leagues_with_validated_defaults(self):
        manifest, registry, calls = self._train_with_fake_models()

        self.assertEqual(len(calls), 4)
        self.assertEqual(len(registry["leagues"]), 4)
        self.assertEqual(
            registry["dataset_manifest_hash"], manifest["bundle_hash"]
        )
        self.assertEqual(
            registry["registry_hash"],
            league_model_manager.calculate_registry_hash(registry),
        )
        self.assertTrue((self.model_dir / "registry.json").is_file())
        self.assertEqual(
            league_model_manager.load_registry(self.model_dir), registry
        )
        for call in calls:
            kwargs = call.kwargs
            self.assertEqual(kwargs["half_time_half_life_days"], 730.0)
            self.assertEqual(kwargs["second_half_half_life_days"], 365.0)
            self.assertEqual(kwargs["full_time_half_life_days"], 365.0)
            self.assertEqual(kwargs["association_smoothing_alpha"], 0.5)
            self.assertEqual(kwargs["association_power"], 1.0)
            self.assertEqual(kwargs["iterations"], 1200)
            self.assertEqual(
                kwargs["dataset_manifest_hash"], manifest["bundle_hash"]
            )
        for entry in registry["leagues"]:
            self.assertEqual(
                entry["aliases"], [entry["league_key"], entry["league"]]
            )
            self.assertTrue((self.model_dir / entry["model_file"]).is_file())
            self.assertRegex(entry["dataset_file_sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(entry["model_hash"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(entry["training_cutoff"], "2025-12-31")

    def test_special_competition_regime_is_excluded_from_registered_training(self):
        manifest = self._write_dataset_bundle(
            all_leagues=False, only_league="japan_j1"
        )
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
        ):
            registry = league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
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

    def test_modified_dataset_is_rejected_before_training(self):
        self._write_dataset_bundle(all_leagues=False)
        dataset = next(self.dataset_dir.glob("*-scores.csv"))
        dataset.write_text("tampered\n", encoding="utf-8")

        with (
            mock.patch.object(
                league_model_manager.htft_model, "fit_model"
            ) as fitted,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "score dataset hash mismatch",
            ),
        ):
            league_model_manager.train_models(self.dataset_dir, self.model_dir)
        fitted.assert_not_called()

    def test_rehashed_regime_tampering_is_rejected_before_training(self):
        manifest = self._write_dataset_bundle(
            all_leagues=False, only_league="japan_j1"
        )
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

        with (
            mock.patch.object(
                league_model_manager.htft_model, "fit_model"
            ) as fitted,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "competition_regime must be 2026_vision_regional",
            ),
        ):
            league_model_manager.train_models(self.dataset_dir, self.model_dir)
        fitted.assert_not_called()

    def test_registered_training_rejects_unpromoted_hyperparameters(self):
        self._write_dataset_bundle(all_leagues=False)
        with (
            mock.patch.object(
                league_model_manager.htft_model, "fit_model"
            ) as fitted,
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "locked to the promoted",
            ),
        ):
            league_model_manager.train_models(
                self.dataset_dir,
                self.model_dir,
                iterations=7,
            )
        fitted.assert_not_called()

    def test_registration_rejects_model_with_unapproved_actual_config(self):
        manifest = self._write_dataset_bundle(all_leagues=False)

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
            self.assertRaisesRegex(
                league_model_manager.LeagueModelManagerError,
                "actual training config is not the approved",
            ),
        ):
            league_model_manager.train_models(self.dataset_dir, self.model_dir)

    def test_loading_rejects_rehashed_model_with_unapproved_actual_config(self):
        manifest, registry, _calls = self._train_with_fake_models(
            all_leagues=False
        )
        entry = registry["leagues"][0]
        model = _fake_model("brazil_serie_a", manifest["bundle_hash"])
        model["config"]["score_models"]["full_time"]["regularization"] = 0.5
        model["model_hash"] = league_model_manager.htft_model.calculate_model_hash(
            model
        )
        model_path = self.model_dir / entry["model_file"]
        model_path.write_bytes(league_model_manager._json_bytes(model))
        entry["model_hash"] = model["model_hash"]
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
        _manifest, registry, _calls = self._train_with_fake_models(
            all_leagues=False
        )
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
        _manifest, registry, _calls = self._train_with_fake_models(
            all_leagues=False
        )
        model_path = self.model_dir / registry["leagues"][0]["model_file"]
        model_path.write_bytes(model_path.read_bytes() + b" ")

        with (
            mock.patch.object(
                league_model_manager.htft_model, "load_model"
            ) as loaded,
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
        manifest, registry, _calls = self._train_with_fake_models(
            all_leagues=False
        )
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
            all_leagues=False
        )
        model = _fake_model("brazil_serie_a", manifest["bundle_hash"])
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
                "competition_key": "brazil_serie_a",
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
            league_model_manager.htft_model.calculate_prediction_hash(
                htft_prediction
            )
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
            mock.patch.object(
                league_model_manager.htft_model, "validate_prediction"
            ),
            mock.patch.object(
                league_model_manager, "validate_score_prediction"
            ),
        ):
            bundle = league_model_manager.predict_registered_model(
                self.model_dir,
                "巴甲",
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
        self.assertIs(
            score_predict.call_args.args[0], model["components"]["full_time"]
        )
        self.assertIs(bundle["htft_prediction"], htft_prediction)
        self.assertIs(bundle["score_prediction"], score_prediction)
        output_manifest = bundle["manifest"]
        self.assertTrue(
            output_manifest["full_time_probability_consistency"]["checked"]
        )
        self.assertEqual(
            output_manifest["external_anchors"],
            {"half_time": True, "full_time": False},
        )
        self.assertEqual(
            output_manifest["registry_hash"], registry["registry_hash"]
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
            mock.patch.object(
                league_model_manager.htft_model, "validate_prediction"
            ),
            mock.patch.object(
                league_model_manager, "validate_score_prediction"
            ),
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
        manifest, _registry, _calls = self._train_with_fake_models(
            all_leagues=False
        )
        model = _fake_model("brazil_serie_a", manifest["bundle_hash"])
        htft_prediction = {
            "artifact_type": "soccer_htft_prediction",
            "prediction_hash": _hash_bytes(b"htft"),
            "generated_at": "2029-12-31T12:00:00Z",
            "fixture": {"competition_key": "brazil_serie_a"},
            "components": {
                "full_time": {
                    "one_x_two": {"home": 0.4, "draw": 0.3, "away": 0.3}
                }
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
            mock.patch.object(
                league_model_manager.htft_model, "load_model"
            ) as loaded,
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
        manifest, _registry, _calls = self._train_with_fake_models(
            all_leagues=False
        )
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

        registry = league_model_manager.train_models(
            self.dataset_dir,
            self.model_dir,
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

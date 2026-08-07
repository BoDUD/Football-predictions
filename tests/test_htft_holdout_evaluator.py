from __future__ import annotations

import copy
import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "htft_holdout_evaluator.py"
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
    format_version: str | None = None,
    phase_group: str | None = None,
    season_status: str | None = None,
    round_label: str = "1",
) -> dict[str, str | int]:
    match_date = f"{season}-{month_day}"
    half_result = _result_code(*half)
    full_result = _result_code(*full)
    league_name, _spec = next(
        (name, spec)
        for name, spec in evaluator.history_importer.LEAGUE_SPECS.items()
        if spec["league_key"] == league_key
    )
    return {
        "match_id": (
            f"{season}{month_day.replace('-', '')}"
            f"{sum(ord(char) for char in home + away) % 10000:04d}"
        ),
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
        "league": league_name,
        "season": season,
        "competition_regime": competition_regime,
        "format_version": format_version
        or evaluator.history_importer._format_version(league_key, season),
        "phase_group": phase_group
        or evaluator.history_importer._phase_group(league_key, round_label),
        # The complete/partial label is source-count dependent and is finalized
        # by _build_bundle after every synthetic fixture has been assembled.
        "season_status": season_status or "pending_test_bundle_status",
        "round": round_label,
        "source_row": "3",
        "source_kickoff": match_date + "T12:00+00:00",
        "source_timezone": "UTC",
        "kickoff_utc": match_date + "T12:00Z",
    }


def _league_rows(
    league_key: str, extra_per_holdout: int = 0
) -> list[dict[str, object]]:
    if league_key == "uefa_nations_league":
        regime = "national_team_league_and_knockout"
        return [
            _row(
                league_key,
                2020,
                "09-03",
                "A",
                "B",
                (1, 0),
                (0, 0),
                competition_regime=regime,
                round_label="A联赛 第1轮",
            ),
            _row(
                league_key,
                2020,
                "11-18",
                "B",
                "C",
                (1, 1),
                (0, 1),
                competition_regime=regime,
                round_label="B联赛 第6轮",
            ),
            _row(
                league_key,
                2022,
                "06-03",
                "A",
                "C",
                (2, 1),
                (1, 0),
                competition_regime=regime,
                round_label="A联赛 第1轮",
            ),
            _row(
                league_key,
                2022,
                "09-27",
                "C",
                "B",
                (0, 1),
                (0, 0),
                competition_regime=regime,
                round_label="B联赛 第6轮",
            ),
            _row(
                league_key,
                2024,
                "09-05",
                "A",
                "B",
                (1, 1),
                (0, 0),
                competition_regime=regime,
                round_label="A联赛 第1轮",
            ),
            _row(
                league_key,
                2024,
                "11-18",
                "C",
                "A",
                (0, 2),
                (0, 1),
                competition_regime=regime,
                round_label="B联赛 第6轮",
            ),
        ]
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


def _write_csv(
    path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _market_rows(score_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for score in score_rows:
        for (bookmaker, _label), odds in zip(
            evaluator.history_importer.BOOKMAKERS,
            (
                (2.20, 3.30, 3.10),
                (2.30, 3.20, 3.00),
                (2.25, 3.25, 3.05),
                (2.28, 3.22, 3.02),
            ),
        ):
            rows.append(
                {
                    "match_id": score["match_id"],
                    "league_key": score["league_key"],
                    "league": score["league"],
                    "season": score["season"],
                    "competition_regime": score["competition_regime"],
                    "format_version": score["format_version"],
                    "phase_group": score["phase_group"],
                    "season_status": score["season_status"],
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
                        row["source_kickoff"] = "2026-02-06T12:00+00:00"
                        row["kickoff_utc"] = "2026-02-06T12:00Z"
                    row["competition_regime"] = (
                        evaluator.history_importer.JAPAN_J1_VISION_REGIME
                    )
            score_rows.append(
                _row(
                    league_key,
                    2026,
                    "06-07",
                    "A",
                    "B",
                    (1, 0),
                    (0, 0),
                )
            )
        if leak_training_date:
            score_rows[5]["date"] = "2024-12-01"
            score_rows[5]["source_kickoff"] = "2024-12-01T12:00+00:00"
            score_rows[5]["kickoff_utc"] = "2024-12-01T12:00Z"

        season_counts: dict[int, int] = {}
        for row in score_rows:
            season = int(row["season"])
            season_counts[season] = season_counts.get(season, 0) + 1
        # Synthetic fixtures deliberately use compact seasons. Register their
        # audited schedule totals for this isolated evaluator module so that
        # 2022-2025 are complete while the as-of 2026 cohort remains partial.
        registered_counts = (
            evaluator.history_importer.EXPECTED_SEASON_MATCH_COUNTS.setdefault(
                league_key, {}
            )
        )
        for season, count in season_counts.items():
            registered_counts[season] = count + (1 if season == 2026 else 0)
        for row in score_rows:
            season = int(row["season"])
            row["season_status"] = evaluator.history_importer._season_status(
                league_key,
                season,
                season_counts[season],
                "2026-08-03",
            )
        score_name = f"{league_key}-scores.csv"
        market_name = f"{league_key}-opening-markets.csv"
        score_path = root / score_name
        market_path = root / market_name
        _write_csv(score_path, SCORE_FIELDS, score_rows)
        market_rows = _market_rows(score_rows)
        _write_csv(market_path, MARKET_FIELDS, market_rows)
        seasons: dict[str, int] = {}
        regimes: dict[str, dict[str, int]] = {}
        formats: dict[str, dict[str, int]] = {}
        phases: dict[str, dict[str, int]] = {}
        statuses: dict[str, dict[str, int]] = {}
        for row in score_rows:
            season = str(row["season"])
            regime = str(row["competition_regime"])
            format_version = str(row["format_version"])
            phase_group = str(row["phase_group"])
            season_status = str(row["season_status"])
            seasons[season] = seasons.get(season, 0) + 1
            by_regime = regimes.setdefault(season, {})
            by_regime[regime] = by_regime.get(regime, 0) + 1
            by_format = formats.setdefault(season, {})
            by_format[format_version] = by_format.get(format_version, 0) + 1
            by_phase = phases.setdefault(season, {})
            by_phase[phase_group] = by_phase.get(phase_group, 0) + 1
            by_status = statuses.setdefault(season, {})
            by_status[season_status] = by_status.get(season_status, 0) + 1
        league_name, spec = next(
            (name, item)
            for name, item in evaluator.history_importer.LEAGUE_SPECS.items()
            if item["league_key"] == league_key
        )
        season_completeness = {
            season: evaluator.history_importer._season_completeness(
                league_key,
                int(season),
                count,
                "2026-08-03",
            )
            for season, count in sorted(seasons.items(), key=lambda item: int(item[0]))
        }
        bookmaker_completeness = {
            bookmaker: {
                "opening_1x2": {"rows": len(score_rows), "rate": 1.0},
                "opening_asian": {"rows": 0, "rate": 0.0},
                "opening_total": {"rows": 0, "rate": 0.0},
            }
            for bookmaker, _label in evaluator.history_importer.BOOKMAKERS
        }
        leagues.append(
            {
                "league_key": league_key,
                "league": league_name,
                "aliases": list(spec.get("aliases", (league_key, league_name))),
                "output_stem": spec["filename"],
                "rows": len(score_rows),
                "seasons": dict(sorted(seasons.items(), key=lambda item: int(item[0]))),
                "season_completeness": season_completeness,
                "competition_regimes": {
                    season: dict(sorted(counts.items()))
                    for season, counts in sorted(
                        regimes.items(), key=lambda item: int(item[0])
                    )
                },
                "format_versions": {
                    season: dict(sorted(counts.items()))
                    for season, counts in sorted(
                        formats.items(), key=lambda item: int(item[0])
                    )
                },
                "phase_groups": {
                    season: dict(sorted(counts.items()))
                    for season, counts in sorted(
                        phases.items(), key=lambda item: int(item[0])
                    )
                },
                "season_statuses": {
                    season: dict(sorted(counts.items()))
                    for season, counts in sorted(
                        statuses.items(), key=lambda item: int(item[0])
                    )
                },
                "utc_date_start": min(str(row["date"]) for row in score_rows),
                "utc_date_end": max(str(row["date"]) for row in score_rows),
                "calendar_rollovers": [],
                "bookmaker_opening_completeness": bookmaker_completeness,
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
        if "titan_competition_id" in spec:
            leagues[-1]["titan_source"] = {
                "competition_id": spec["titan_competition_id"],
                "source_kind": spec["titan_source_kind"],
            }
    manifest: dict[str, object] = {
        "artifact_type": "soccer_history_dataset_bundle",
        "schema_version": evaluator.history_importer.DATASET_SCHEMA_VERSION,
        "importer_version": evaluator.history_importer.IMPORTER_VERSION,
        "as_of_date": "2026-08-03",
        "season_completeness_policy": dict(
            evaluator.history_importer.SEASON_COMPLETENESS_POLICY
        ),
        "immutable_result_exclusion_policy": (
            evaluator.history_importer._immutable_result_exclusion_policy()
        ),
        "source_timezone": "UTC",
        "kickoff_year_policy": (
            "explicit per-competition calendar policy; cross-year rollover is "
            "allowed once only for autumn-to-spring competitions and the documented "
            "Brazil 2020/AFC 2022 exceptions"
        ),
        "training_feature_whitelist": ["date", "home_team", "away_team"],
        "outcome_label_fields": [
            "home_goals",
            "away_goals",
            "half_home_goals",
            "half_away_goals",
            "half_result",
            "full_result",
            "htft_result",
        ],
        "research_only_fields": [
            "opening 1X2",
            "opening Asian handicap",
            "opening goal total",
        ],
        "quarantined_fields": [
            "all closing prices",
            "rankings",
            "total-goals label",
            "half-time/full-time result label",
            "win/draw/loss result label",
        ],
        "caveats": [
            "The workbooks do not contain the exact collection timestamp for opening prices.",
            "Opening prices may be used only as a research baseline until pre-kickoff provenance is verified.",
            "Source kickoff timezone is supplied explicitly by the importer operator.",
            "Rows labelled partial_as_of_* are right-censored snapshots and cannot support model promotion.",
            "Competition format_version and phase_group labels must be evaluated as separate cohorts when material.",
        ],
        "leagues": sorted(leagues, key=lambda item: str(item["league_key"])),
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
    manifest["bundle_hash"] = evaluator.history_importer._canonical_manifest_hash(
        manifest
    )
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
    promotion_eligible_accumulators: list[dict[str, object]] = []
    promotion_eligible_cohorts: list[dict[str, str]] = []
    research_shadow_cohorts: list[dict[str, str]] = []
    for league in evaluation["leagues"]:
        league_accumulators: list[dict[str, object]] = []
        league_promotion_accumulators: list[dict[str, object]] = []
        league_promotion_cohorts: list[dict[str, str]] = []
        league_research_shadow_cohorts: list[dict[str, str]] = []
        for split in league["splits"]:
            model = evaluator._cohort_accumulators(evaluator.MODEL_PAIR_MASS_THRESHOLD)
            market = evaluator._cohort_accumulators(
                evaluator.MARKET_PAIR_MASS_THRESHOLD
            )
            baseline = evaluator._new_accumulator(evaluator.MODEL_PAIR_MASS_THRESHOLD)
            paired_cohorts = evaluator._new_paired_cohorts()
            paired = paired_cohorts["overall"]
            context_slices = evaluator._new_context_slice_accumulators()
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
                cohort = "league_average_fallback" if used_fallback else "known_teams"
                evaluator._add_paired_score(
                    paired_cohorts[cohort],
                    forecast["model_probabilities"],
                    forecast["empirical_baseline_probabilities"],
                    forecast["actual_class"],
                    group=league["league_key"],
                )
                evaluator._add_context_slice_score(
                    context_slices,
                    format_version=forecast["format_version"],
                    phase_group=forecast["phase_group"],
                    model_probabilities=forecast["model_probabilities"],
                    baseline_probabilities=forecast["empirical_baseline_probabilities"],
                    actual_class=forecast["actual_class"],
                    used_fallback=used_fallback,
                    group=league["league_key"],
                )
            split["fallback_fixture_count"] = sum(
                int(item["used_league_average_fallback"]) for item in split["forecasts"]
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
            split["model_minus_empirical_baseline_by_team_availability"] = (
                evaluator._finalize_paired_cohorts(
                    paired_cohorts,
                    bootstrap_repetitions=repetitions,
                    bootstrap_seed=seed,
                )
            )
            split["context_slices"] = evaluator._finalize_context_slices(
                context_slices,
                bootstrap_repetitions=repetitions,
                bootstrap_seed=seed,
            )
            accumulators = {
                "model": model,
                "market": market,
                "baseline": baseline,
                "paired": paired,
                "context_slices": context_slices,
            }
            league_accumulators.append(accumulators)
            all_accumulators.append(accumulators)
            by_split[split["split_id"]].append(accumulators)
            cohort = {
                "league_key": league["league_key"],
                "split_id": split["split_id"],
            }
            if split["evaluation_scope"]["component_promotion_evidence_included"]:
                league_promotion_accumulators.append(accumulators)
                league_promotion_cohorts.append(cohort)
                promotion_eligible_accumulators.append(accumulators)
                promotion_eligible_cohorts.append(cohort)
            else:
                league_research_shadow_cohorts.append(cohort)
                research_shadow_cohorts.append(cohort)
        league["summary"] = evaluator._aggregate_split_cohorts(
            league_accumulators,
            bootstrap_repetitions=repetitions,
            bootstrap_seed=seed,
        )
        league["promotion_evidence"] = evaluator._promotion_evidence_summary(
            eligible_cohorts=league_promotion_cohorts,
            research_shadow_cohorts=league_research_shadow_cohorts,
            eligible_accumulators=league_promotion_accumulators,
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
    evaluation["promotion_evidence"] = evaluator._promotion_evidence_summary(
        eligible_cohorts=promotion_eligible_cohorts,
        research_shadow_cohorts=research_shadow_cohorts,
        eligible_accumulators=promotion_eligible_accumulators,
        bootstrap_repetitions=repetitions,
        bootstrap_seed=seed,
    )


class HtftHoldoutEvaluatorTests(unittest.TestCase):
    def test_competition_specific_regime_partition_keeps_cups_separate(self):
        brazil_rows = [
            {"competition_regime": "national_knockout_cup"},
            {"competition_regime": "regular"},
        ]
        included, excluded = evaluator._partition_formal_regimes(
            brazil_rows, league_key="brazil_cup"
        )
        self.assertEqual(included, brazil_rows[:1])
        self.assertEqual(excluded, brazil_rows[1:])

        efl_rows = [
            {"competition_regime": "national_knockout_cup"},
            {"competition_regime": "regular"},
        ]
        included, excluded = evaluator._partition_formal_regimes(
            efl_rows, league_key="england_league_cup"
        )
        self.assertEqual(included, efl_rows[:1])
        self.assertEqual(excluded, efl_rows[1:])

        nations_rows = [
            {"competition_regime": "national_team_league_and_knockout"},
            {"competition_regime": "regular"},
        ]
        included, excluded = evaluator._partition_formal_regimes(
            nations_rows, league_key="uefa_nations_league"
        )
        self.assertEqual(included, nations_rows[:1])
        self.assertEqual(excluded, nations_rows[1:])

        ordinary_rows = [
            {"competition_regime": "regular"},
            {"competition_regime": "national_knockout_cup"},
        ]
        included, excluded = evaluator._partition_formal_regimes(
            ordinary_rows, league_key="brazil_serie_a"
        )
        self.assertEqual(included, ordinary_rows[:1])
        self.assertEqual(excluded, ordinary_rows[1:])

        included, excluded = evaluator._partition_formal_regimes(
            ordinary_rows, league_key="netherlands_eerste_divisie"
        )
        self.assertEqual(included, ordinary_rows[:1])
        self.assertEqual(excluded, ordinary_rows[1:])

        norway_rows = [
            {"competition_regime": "regular"},
            {"competition_regime": "relegation_playoff"},
        ]
        included, excluded = evaluator._partition_formal_regimes(
            norway_rows, league_key="norway_eliteserien"
        )
        self.assertEqual(included, norway_rows[:1])
        self.assertEqual(excluded, norway_rows[1:])

    def test_nations_biennial_history_uses_2024_validation_without_fake_2025(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(root, [("uefa_nations_league", 0)])

            result = evaluator.evaluate_bundle(
                dataset_dir=root,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=20,
                experimental_override=True,
            )

        league = result["leagues"][0]
        self.assertEqual(league["league_key"], "uefa_nations_league")
        by_split = {item["split_id"]: item for item in league["splits"]}
        validation = by_split["validation_2024"]
        self.assertEqual(validation["status"], "evaluated")
        self.assertEqual(validation["training_match_count"], 4)
        self.assertEqual(validation["test_match_count"], 2)
        self.assertEqual(
            validation["training_competition_regime_counts"],
            {"national_team_league_and_knockout": 4},
        )
        self.assertEqual(
            validation["test_competition_regime_counts"],
            {"national_team_league_and_knockout": 2},
        )
        self.assertLess(
            validation["training_cutoff_date"], validation["test_date_start"]
        )
        for split_id in ("fixed_holdout_2025", "shadow_2026"):
            self.assertEqual(by_split[split_id]["status"], "not_available")
            self.assertEqual(by_split[split_id]["test_match_count"], 0)

    def setUp(self) -> None:
        self._expected_season_counts = copy.deepcopy(
            evaluator.history_importer.EXPECTED_SEASON_MATCH_COUNTS
        )

    def tearDown(self) -> None:
        evaluator.history_importer.EXPECTED_SEASON_MATCH_COUNTS.clear()
        evaluator.history_importer.EXPECTED_SEASON_MATCH_COUNTS.update(
            self._expected_season_counts
        )

    def test_missing_context_columns_use_conservative_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            score_path = root / "legacy-scores.csv"
            legacy_fields = tuple(
                field
                for field in SCORE_FIELDS
                if field not in evaluator.OPTIONAL_CONTEXT_COLUMNS
            )
            source_row = _row(
                "brazil_serie_a",
                2024,
                "02-01",
                "A",
                "B",
                (1, 0),
                (0, 0),
            )
            _write_csv(
                score_path,
                legacy_fields,
                [{field: source_row[field] for field in legacy_fields}],
            )

            rows = evaluator._load_score_rows(
                score_path,
                league_key="brazil_serie_a",
                expected_hash=evaluator._file_hash(score_path),
                expected_rows=1,
            )

            self.assertEqual(rows[0]["season_status"], evaluator.LEGACY_SEASON_STATUS)
            self.assertEqual(rows[0]["format_version"], evaluator.LEGACY_FORMAT_VERSION)
            self.assertEqual(rows[0]["phase_group"], evaluator.LEGACY_PHASE_GROUP)
            scope = evaluator._split_evaluation_scope(rows, evaluator.SPLITS[0])
            self.assertEqual(
                scope["evidence_role"],
                "research_shadow_unverified_season_status",
            )
            self.assertFalse(scope["component_promotion_evidence_included"])

    def test_partial_2026_is_shadow_only_and_excluded_from_promotion_summary(self):
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

            shadow = result["leagues"][0]["splits"][2]
            scope = shadow["evaluation_scope"]
            self.assertEqual(
                scope["season_status_counts"],
                {"partial_as_of_2026-08-03": shadow["test_match_count"]},
            )
            self.assertTrue(scope["partial_test_season"])
            self.assertFalse(scope["complete_test_season_status_verified"])
            self.assertEqual(scope["evidence_role"], "research_shadow_partial_season")
            self.assertFalse(scope["component_promotion_evidence_included"])
            self.assertFalse(scope["promotion_ready"])
            promotion_evidence = result["promotion_evidence"]
            self.assertEqual(
                promotion_evidence["research_shadow_cohorts"],
                [{"league_key": "brazil_serie_a", "split_id": "shadow_2026"}],
            )
            self.assertNotIn(
                "shadow_2026",
                {
                    cohort["split_id"]
                    for cohort in promotion_evidence["eligible_cohorts"]
                },
            )
            self.assertEqual(
                promotion_evidence["eligible_classification_summary"]["model_only"][
                    "overall"
                ]["metrics"]["sample_count"],
                4,
            )
            self.assertFalse(result["formal_htft_eligible"])
            self.assertFalse(result["complete_prekickoff_nine_way_htft_odds_available"])
            self.assertFalse(result["ev_roi_evaluation_available"])
            self.assertEqual(
                result["evaluation_scope"], "nine_class_probability_accuracy_only"
            )

    def test_context_slices_are_emitted_and_reconcile_to_total_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(
                root,
                [("brazil_serie_a", 0), ("france_ligue_1", 0)],
            )

            result = evaluator.evaluate_bundle(
                dataset_dir=root,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=50,
                experimental_override=True,
            )

            all_splits = result["summary"]["all_splits"]
            total = all_splits["model_only"]["overall"]["metrics"]["sample_count"]
            slices = all_splits["context_slices"]
            self.assertEqual(
                set(slices["format_version"]),
                {"standard_league_format", "ligue1_18_team"},
            )
            self.assertEqual(set(slices["phase_group"]), {"regular_season"})
            for dimension in ("format_version", "phase_group"):
                self.assertEqual(
                    sum(item["sample_count"] for item in slices[dimension].values()),
                    total,
                )
                for item in slices[dimension].values():
                    self.assertEqual(
                        item["sample_count"],
                        item["model_only"]["overall"]["metrics"]["sample_count"],
                    )
                    self.assertEqual(
                        item["sample_count"],
                        item["league_empirical_frequency_baseline"]["metrics"][
                            "sample_count"
                        ],
                    )
                    self.assertIn(
                        "nine_class_log_loss",
                        item["model_minus_empirical_baseline"],
                    )

    def test_partial_scope_cannot_be_promoted_by_rehashing(self):
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
            scope = tampered["leagues"][0]["splits"][2]["evaluation_scope"]
            scope["component_promotion_evidence_included"] = True
            scope["evidence_role"] = "configuration_experiment_shadow"
            tampered["evaluation_hash"] = evaluator.calculate_evaluation_hash(tampered)

            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "evaluation scope does not match forecast evidence",
            ):
                evaluator.validate_evaluation(tampered)

    def test_existing_cli_arguments_remain_accepted(self):
        args = evaluator.build_parser().parse_args(
            [
                "--dataset-dir",
                "dataset",
                "--output",
                "evaluation.json",
                "--include-opening-market",
                "--iterations",
                "2",
                "--learning-rate",
                "0.005",
                "--experimental-override",
            ]
        )

        self.assertEqual(args.dataset_dir, "dataset")
        self.assertEqual(args.output, "evaluation.json")
        self.assertTrue(args.include_opening_market)
        self.assertTrue(args.experimental_override)

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
                self.assertLess(split["training_cutoff_date"], split["test_date_start"])
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
                        item["support"] for item in model_metrics["per_class"].values()
                    ),
                    split["test_match_count"],
                )
            self.assertEqual(result["fit_config"]["half_time_half_life_days"], 730.0)
            self.assertEqual(result["fit_config"]["full_time_half_life_days"], 365.0)
            self.assertEqual(
                result["fit_config"]["association_half_life_days"],
                result["fit_config"]["full_time_half_life_days"],
            )
            self.assertEqual(
                result["fit_config"]["seed_method"], "empirical_association"
            )
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
            weighted_log_loss = (
                sum(
                    item["nine_class_log_loss"] * item["sample_count"]
                    for item in league_metrics
                )
                / total
            )
            summary = result["summary"]["all_splits"]["model_only"]["overall"]
            self.assertEqual(summary["metrics"]["sample_count"], total)
            self.assertAlmostEqual(
                summary["metrics"]["nine_class_log_loss"], weighted_log_loss
            )
            weighted_top_two_hits = sum(item["top_two_hits"] for item in league_metrics)
            self.assertEqual(summary["metrics"]["top_two_hits"], weighted_top_two_hits)
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
            self.assertEqual(policy["collection_time_status"], "unavailable_in_source")
            self.assertEqual(policy["policy"], evaluator.RESEARCH_MARKET_POLICY)
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

    def test_association_half_life_is_fitted_and_not_display_only_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _build_bundle(root, [("brazil_serie_a", 0)])

            frozen = evaluator.evaluate_bundle(
                dataset_dir=root,
                iterations=2,
                learning_rate=0.005,
                bootstrap_repetitions=50,
                experimental_override=True,
            )
            short_decay = evaluator.evaluate_bundle(
                dataset_dir=root,
                iterations=2,
                learning_rate=0.005,
                association_half_life_days=30.0,
                bootstrap_repetitions=50,
                experimental_override=True,
            )

            self.assertEqual(frozen["fit_config"]["association_half_life_days"], 365.0)
            self.assertEqual(
                short_decay["fit_config"]["association_half_life_days"], 30.0
            )
            self.assertNotEqual(
                frozen["leagues"][0]["splits"][0]["model_hash"],
                short_decay["leagues"][0]["splits"][0]["model_hash"],
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

    def test_rehashed_nonpositive_association_half_life_is_rejected(self):
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
            tampered["fit_config"]["association_half_life_days"] = 0.0
            tampered["promotion"] = evaluator._promotion_metadata(
                fit_configuration_matches_promoted=False,
                bootstrap_configuration_matches_promoted=False,
            )
            tampered["evaluation_hash"] = evaluator.calculate_evaluation_hash(tampered)

            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "association_half_life_days must be positive",
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
            tampered["leagues"][0]["score_dataset"]["sha256"] = "sha256:" + "0" * 64
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

    def test_rehashed_known_team_paired_metrics_are_recomputed_from_forecasts(self):
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
            split = tampered["leagues"][0]["splits"][0]
            known = split["model_minus_empirical_baseline_by_team_availability"][
                "known_teams"
            ]
            known["nine_class_log_loss"]["mean_delta"] = -999.0
            tampered["evaluation_hash"] = evaluator.calculate_evaluation_hash(tampered)

            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "paired cohort deltas",
            ):
                evaluator.validate_evaluation(tampered)

    def test_source_binding_refits_and_rejects_rehashed_fake_probabilities(self):
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
            forecast = tampered["leagues"][0]["splits"][0]["forecasts"][0]
            probabilities = forecast["model_probabilities"]
            first, last = (
                evaluator.htft_model.HTFT_CLASSES[0],
                evaluator.htft_model.HTFT_CLASSES[-1],
            )
            probabilities[first], probabilities[last] = (
                probabilities[last],
                probabilities[first],
            )
            ranked = sorted(
                evaluator.htft_model.HTFT_CLASSES,
                key=lambda name: (
                    -probabilities[name],
                    evaluator.htft_model.HTFT_CLASSES.index(name),
                ),
            )
            forecast["model_top_two"] = [
                {"class": name, "probability": probabilities[name]}
                for name in ranked[:2]
            ]
            forecast["model_pair_mass"] = sum(
                probabilities[name] for name in ranked[:2]
            )
            _rewrite_summaries_from_forecasts(tampered)
            tampered["evaluation_hash"] = evaluator.calculate_evaluation_hash(tampered)

            # The internal summaries are deliberately self-consistent.  Only
            # validation against the source bundle can prove the forecast came
            # from the declared deterministic training run.
            evaluator.validate_evaluation(tampered)
            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "model probabilities",
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
            manifest["leagues"][0]["score_dataset"]["sha256"] = evaluator._file_hash(
                score_path
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
                {key: value for key, value in manifest.items() if key != "bundle_hash"}
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
                with (
                    self.subTest(**kwargs),
                    self.assertRaisesRegex(
                        evaluator.HoldoutEvaluationError,
                        "semantic validation.*competition_regime",
                    ),
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
            metrics = tampered["leagues"][0]["splits"][0]["model_only"]["overall"][
                "metrics"
            ]
            metrics["top_two_hits"] += 1
            tampered["evaluation_hash"] = evaluator.calculate_evaluation_hash(tampered)

            with self.assertRaisesRegex(
                evaluator.HoldoutEvaluationError,
                "does not match forecast evidence",
            ):
                evaluator.validate_evaluation(tampered)


if __name__ == "__main__":
    unittest.main()

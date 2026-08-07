from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import corner_history_dataset_builder as builder


def _record(
    source_key: str,
    match_id: int,
    *,
    status: str = "complete",
    period: str = "regulation_90",
    kickoff: str = "2024-01-01 12:00",
    home_corners: int | None = 6,
    away_corners: int | None = 4,
    phase: str | None = None,
    round_value: str | None = None,
    regime: str | None = None,
    season_year: int | None = None,
) -> dict:
    total = (
        None
        if home_corners is None or away_corners is None
        else home_corners + away_corners
    )
    local = datetime.fromisoformat(kickoff).replace(tzinfo=timezone(timedelta(hours=8)))
    kickoff_utc = local.astimezone(timezone.utc)
    year = season_year if season_year is not None else local.year
    if phase is None:
        if source_key in {"uefa-champions-league", "afc-champions-league"}:
            phase = "group_stage"
        elif source_key == "brazil-cup":
            phase = "knockout"
        elif source_key == "uefa-nations-league":
            phase = "league_phase"
        else:
            phase = "regular"
    if regime is None:
        eligible_regimes = builder.ELIGIBLE_REGIMES_BY_COMPETITION[source_key]
        regime = "standard" if eligible_regimes == ("regular",) else eligible_regimes[0]
    record = {
        "schema_version": "1.0.0",
        "collector_version": "titan-corner-history/1.0.0",
        "competition_key": source_key,
        "competition_regime": regime,
        "season_label": str(year),
        "season_start_year": year,
        "phase": phase,
        "round": round_value,
        "kickoff": kickoff,
        "kickoff_utc": kickoff_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "kickoff_epoch": int(kickoff_utc.timestamp()),
        "source_timezone": "Asia/Shanghai",
        "match_id": str(match_id),
        "home_team_id": match_id * 2,
        "away_team_id": match_id * 2 + 1,
        "home_team": f"H{match_id}",
        "away_team": f"A{match_id}",
        "home_goals": 1,
        "away_goals": 0,
        "home_corners": home_corners,
        "away_corners": away_corners,
        "total_corners": total,
        "half_home_corners": None,
        "half_away_corners": None,
        "half_total_corners": None,
        "corner_data_status": status,
        "corner_period": period,
        "corner_exclusion_reasons": [] if status == "complete" else [status],
        "source_url": f"https://example.test/{match_id}",
        "source_collected_at": "2026-08-05T00:00:00Z",
        "source_response_sha256": "sha256:" + f"{match_id:064x}"[-64:],
    }
    record["schedule_fixture_sha256"] = builder.calculate_fixture_fingerprint(record)
    return record


def _source() -> dict:
    matches = []
    match_id = 1
    for source_key in builder.COMPETITIONS:
        matches.append(_record(source_key, match_id, kickoff="2024-01-01 12:00"))
        match_id += 1
        matches.append(_record(source_key, match_id, kickoff="2024-01-02 12:00"))
        match_id += 1
    source = {
        "schema_version": builder.SOURCE_SCHEMA_VERSION,
        "collector_version": builder.SOURCE_COLLECTOR_VERSION,
        "generated_at": "2026-08-03T00:00:00Z",
        "source": "https://example.test",
        "qa": {},
        "matches": matches,
    }
    source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
    return source


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class CornerHistoryDatasetBuilderTests(unittest.TestCase):
    def test_targeted_replay_by_league_key_matches_full_manifest_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            source_path = root / "corner_history.json"
            _write(source_path, source)

            full = builder.build_dataset(
                source_path, root / "full", as_of_date="2026-08-03"
            )
            targeted = builder.build_dataset(
                source_path,
                root / "targeted",
                as_of_date="2026-08-03",
                league_keys=["korea_k_league_1"],
            )

            expected = next(
                entry
                for entry in full["leagues"]
                if entry["league_key"] == "korea_k_league_1"
            )
            self.assertEqual(targeted["leagues"], [expected])
            self.assertEqual(targeted["selection_policy"], full["selection_policy"])
            self.assertEqual(targeted["source_bundle_hash"], full["source_bundle_hash"])
            self.assertEqual(targeted["source_file_sha256"], full["source_file_sha256"])
            self.assertTrue(
                (root / "targeted" / "korea_k_league_1-corners.csv").is_file()
            )
            self.assertFalse(
                (root / "targeted" / "england_premier_league-corners.csv").exists()
            )
            self.assertEqual(
                targeted["bundle_hash"], builder.calculate_manifest_hash(targeted)
            )

    def test_targeted_replay_accepts_competition_keys_and_rejects_bad_filters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path = root / "corner_history.json"
            _write(source_path, _source())

            manifest = builder.build_dataset(
                source_path,
                root / "targeted",
                as_of_date="2026-08-03",
                competition_keys=["sweden-allsvenskan"],
            )
            self.assertEqual(
                [entry["league_key"] for entry in manifest["leagues"]],
                ["sweden_allsvenskan"],
            )
            with self.assertRaisesRegex(
                builder.CornerDatasetError, "mutually exclusive"
            ):
                builder.build_dataset(
                    source_path,
                    root / "invalid-both",
                    as_of_date="2026-08-03",
                    league_keys=["sweden_allsvenskan"],
                    competition_keys=["sweden-allsvenskan"],
                )
            with self.assertRaisesRegex(
                builder.CornerDatasetError, "unsupported league_keys"
            ):
                builder.build_dataset(
                    source_path,
                    root / "invalid-key",
                    as_of_date="2026-08-03",
                    league_keys=["not_a_league"],
                )

    def test_builds_sixteen_bound_csvs_and_excludes_unsafe_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            source["matches"].append(
                _record(
                    "finland-veikkausliiga",
                    999,
                    status="extra_time_ambiguous",
                    period="unverified",
                    kickoff="2024-01-03 12:00",
                )
            )
            source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
            source_path = root / "corner_history.json"
            _write(source_path, source)

            manifest = builder.build_dataset(
                source_path, root / "dataset", as_of_date="2026-08-03"
            )

            self.assertEqual(len(manifest["leagues"]), 16)
            self.assertEqual(
                manifest["bundle_hash"], builder.calculate_manifest_hash(manifest)
            )
            self.assertEqual(manifest["schema_version"], "2.1.0")
            self.assertEqual(
                manifest["selection_policy"]["version"],
                "regulation-corner-training-selection/2.1.0",
            )
            copied_source = root / "dataset" / manifest["source_file"]
            self.assertTrue(copied_source.is_file())
            self.assertEqual(
                builder.load_source(copied_source)["bundle_hash"],
                source["bundle_hash"],
            )
            finland = next(
                row
                for row in manifest["leagues"]
                if row["league_key"] == "finland_veikkausliiga"
            )
            self.assertEqual(finland["rows"], 2)
            self.assertEqual(
                finland["qa"]["excluded_reasons"],
                {"extra_time_ambiguous": 1},
            )
            csv_path = root / "dataset" / finland["dataset_file"]
            with csv_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertEqual(set(builder.CSV_FIELDS), set(rows[0]))
            self.assertRegex(finland["fixture_set_hash"], r"^sha256:[0-9a-f]{64}$")
            self.assertRegex(finland["response_set_hash"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(finland["regimes"], {"regular": 2})
            self.assertEqual(finland["phases"], {"regular_season": 2})

    def test_rejects_rehashed_semantic_corner_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            source["matches"][0]["total_corners"] = 99
            source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
            source_path = root / "corner_history.json"
            _write(source_path, source)
            with self.assertRaisesRegex(
                builder.CornerDatasetError, "does not reconcile"
            ):
                builder.build_dataset(
                    source_path, root / "dataset", as_of_date="2026-08-03"
                )

    def test_rejects_forged_source_bundle_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            source["matches"][0]["home_team"] = "tampered"
            source_path = root / "corner_history.json"
            _write(source_path, source)
            with self.assertRaisesRegex(builder.CornerDatasetError, "bundle_hash"):
                builder.load_source(source_path)

    def test_complete_half_corner_tuple_must_reconcile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            source["matches"][0].update(
                {
                    "half_home_corners": 5,
                    "half_away_corners": 4,
                    "half_total_corners": 8,
                }
            )
            source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
            source_path = root / "corner_history.json"
            _write(source_path, source)
            with self.assertRaisesRegex(builder.CornerDatasetError, "half corners"):
                builder.build_dataset(
                    source_path, root / "dataset", as_of_date="2026-08-03"
                )

    def test_post_as_of_rows_are_quarantined_not_trained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            source["matches"].append(
                _record(
                    "finland-veikkausliiga",
                    998,
                    kickoff="2026-08-04 12:00",
                )
            )
            source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
            source_path = root / "corner_history.json"
            _write(source_path, source)
            manifest = builder.build_dataset(
                source_path, root / "dataset", as_of_date="2026-08-03"
            )
            finland = next(
                row
                for row in manifest["leagues"]
                if row["league_key"] == "finland_veikkausliiga"
            )
            self.assertEqual(finland["qa"]["excluded_reasons"], {"post_as_of_date": 1})

    def test_j1_2026_is_hard_excluded_even_when_mislabeled_standard(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            source["matches"].append(
                _record(
                    "japan-j1",
                    997,
                    kickoff="2026-07-01 19:00",
                    regime="standard",
                    season_year=2026,
                )
            )
            source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
            source_path = root / "corner_history.json"
            _write(source_path, source)
            manifest = builder.build_dataset(
                source_path, root / "dataset", as_of_date="2026-08-03"
            )
            japan = next(
                row for row in manifest["leagues"] if row["league_key"] == "japan_j1"
            )
            self.assertEqual(japan["rows"], 2)
            self.assertEqual(
                japan["qa"]["excluded_reasons"],
                {"competition_regime_not_training_eligible": 1},
            )
            self.assertEqual(
                japan["qa"]["excluded_cohorts"], {"regime:japan-j1:2026": 1}
            )

    def test_versioned_competition_regimes_are_allowed_only_in_their_league(self):
        additions = {
            "brazil-cup": [("national-knockout-cup", "knockout")],
            "france-ligue-1": [("20-team", "regular")],
            "south-korea-k-league-1": [("covid-27-round", "regular")],
            "uefa-champions-league": [("36-team-league-phase", "league_phase")],
            "uefa-nations-league": [
                ("national-team-league-and-knockout", "league_phase")
            ],
            "afc-champions-league": [
                ("cross-year-acl", "group_stage"),
                ("24-team-acl-elite", "group_stage"),
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            next_id = 970
            for competition, regimes in additions.items():
                for regime, phase in regimes:
                    source["matches"].append(
                        _record(
                            competition,
                            next_id,
                            kickoff="2025-04-01 19:00",
                            regime=regime,
                            phase=phase,
                        )
                    )
                    next_id += 1
                source["matches"].append(
                    _record(
                        competition,
                        next_id,
                        kickoff="2025-04-02 19:00",
                        regime="unknown-format",
                        phase=regimes[0][1],
                    )
                )
                next_id += 1
            source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
            source_path = root / "corner_history.json"
            _write(source_path, source)
            manifest = builder.build_dataset(
                source_path, root / "dataset", as_of_date="2026-08-03"
            )
            by_source = {
                row["source_competition_key"]: row for row in manifest["leagues"]
            }
            for competition, regimes in additions.items():
                with self.subTest(competition=competition):
                    entry = by_source[competition]
                    self.assertEqual(entry["rows"], 2 + len(regimes))
                    self.assertEqual(
                        entry["qa"]["excluded_reasons"],
                        {"competition_regime_not_training_eligible": 1},
                    )
                    self.assertEqual(
                        entry["qa"]["excluded_cohorts"],
                        {"regime:unknown-format": 1},
                    )

    def test_extra_time_capable_phases_are_excluded_as_whole_cohorts(self):
        excluded = {
            "finland-veikkausliiga": ("regular", "欧战决赛", "european_playoff"),
            "uefa-champions-league": ("group_stage", "第一圈", "qualifying"),
            "uefa-nations-league": (
                "league_phase",
                "A联半决赛",
                "knockout",
            ),
            "afc-champions-league": ("group_stage", "淘汰赛", "knockout"),
            "usa-mls": ("regular", "Playoffs", "playoffs"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            for offset, (competition, values) in enumerate(excluded.items(), start=990):
                phase, round_value, _expected = values
                source["matches"].append(
                    _record(
                        competition,
                        offset,
                        kickoff="2025-05-01 19:00",
                        phase=phase,
                        round_value=round_value,
                    )
                )
            source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
            source_path = root / "corner_history.json"
            _write(source_path, source)
            manifest = builder.build_dataset(
                source_path, root / "dataset", as_of_date="2026-08-03"
            )
            by_source = {
                row["source_competition_key"]: row for row in manifest["leagues"]
            }
            for competition, (_phase, _round, expected_phase) in excluded.items():
                with self.subTest(competition=competition):
                    entry = by_source[competition]
                    self.assertEqual(entry["rows"], 2)
                    self.assertEqual(
                        entry["qa"]["excluded_reasons"],
                        {"phase_not_training_eligible": 1},
                    )
                    self.assertEqual(
                        entry["qa"]["excluded_cohorts"],
                        {f"phase:{expected_phase}": 1},
                    )

    def test_brazil_cup_extra_time_or_non_regulation_corners_never_train(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            source["matches"].extend(
                [
                    _record(
                        "brazil-cup",
                        997,
                        kickoff="2025-05-01 19:00",
                        regime="national-knockout-cup",
                        phase="knockout",
                        status="extra_time_ambiguous",
                        period="unverified",
                    ),
                    _record(
                        "brazil-cup",
                        998,
                        kickoff="2025-05-02 19:00",
                        regime="national-knockout-cup",
                        phase="knockout",
                        status="complete",
                        period="unverified",
                    ),
                ]
            )
            source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
            source_path = root / "corner_history.json"
            _write(source_path, source)
            manifest = builder.build_dataset(
                source_path, root / "dataset", as_of_date="2026-08-03"
            )
            brazil = next(
                row
                for row in manifest["leagues"]
                if row["source_competition_key"] == "brazil-cup"
            )
            self.assertEqual(brazil["rows"], 2)
            self.assertEqual(
                brazil["qa"]["excluded_reasons"],
                {
                    "extra_time_ambiguous": 1,
                    "non_regulation_corner_period": 1,
                },
            )

    def test_nations_corner_training_contains_only_league_phase(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            source["matches"].extend(
                [
                    _record(
                        "uefa-nations-league",
                        997,
                        kickoff="2025-05-01 19:00",
                        regime="national-team-league-and-knockout",
                        phase="A联赛",
                        round_value="A联赛 第1轮",
                    ),
                    _record(
                        "uefa-nations-league",
                        998,
                        kickoff="2025-05-02 19:00",
                        regime="national-team-league-and-knockout",
                        phase="A联赛",
                        round_value="A联决赛",
                    ),
                ]
            )
            source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
            source_path = root / "corner_history.json"
            _write(source_path, source)
            manifest = builder.build_dataset(
                source_path, root / "dataset", as_of_date="2026-08-03"
            )
            nations = next(
                row
                for row in manifest["leagues"]
                if row["source_competition_key"] == "uefa-nations-league"
            )
            self.assertEqual(nations["rows"], 3)
            self.assertEqual(nations["phases"], {"league_phase": 3})
            self.assertEqual(
                nations["qa"]["excluded_reasons"],
                {"phase_not_training_eligible": 1},
            )
            self.assertEqual(nations["qa"]["excluded_cohorts"], {"phase:knockout": 1})

    def test_rehashed_fixture_identity_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _source()
            source["matches"][0]["home_team"] = "forged team"
            source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
            source_path = root / "corner_history.json"
            _write(source_path, source)
            with self.assertRaisesRegex(
                builder.CornerDatasetError, "schedule_fixture_sha256"
            ):
                builder.build_dataset(
                    source_path, root / "dataset", as_of_date="2026-08-03"
                )


if __name__ == "__main__":
    unittest.main()

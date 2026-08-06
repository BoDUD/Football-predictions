from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "titan_corner_history_collector.py"
)
SPEC = importlib.util.spec_from_file_location("titan_corner_history_collector", SCRIPT)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def fixture(**updates):
    value = {
        "match_id": 2929679,
        "competition_key": "korea-k-league-1",
        "competition_name": "韩K联",
        "competition_id": 15,
        "season_label": "2026",
        "season_start_year": 2026,
        "competition_regime": "regular",
        "phase": "常规赛",
        "round": "第20轮",
        "kickoff": "2026-08-02 14:30",
        "kickoff_utc": "2026-08-02T06:30:00Z",
        "kickoff_epoch": 1785652200,
        "source_timezone": "Asia/Shanghai",
        "home_team_id": 4075,
        "away_team_id": 497,
        "home_team": "济州SK",
        "away_team": "仁川联队",
        "home_goals": 3,
        "away_goals": 3,
        "half_home_goals": 2,
        "half_away_goals": 2,
        "completed": True,
        "training_eligible": True,
        "raw_tail": [],
    }
    value.update(updates)
    return value


def checkpoint_record(value, status="complete"):
    return {
        "schema_version": collector.SCHEMA_VERSION,
        "collector_version": collector.COLLECTOR_VERSION,
        "match_id": str(value["match_id"]),
        **collector._fixture_binding(value),
        "corner_data_status": status,
    }


def header(*, home_corners="5", away_corners="5", match_id="2929679") -> bytes:
    fields = [""] * 73
    fields[0] = "济州SK"
    fields[1] = "仁川联队"
    fields[4] = "-1"
    fields[5] = "202608021430"
    fields[10] = "3"
    fields[11] = "3"
    fields[17] = "4075"
    fields[18] = "497"
    fields[26] = "2"
    fields[27] = "2"
    fields[49] = home_corners
    fields[50] = away_corners
    fields[72] = match_id
    return "^".join(fields).encode("utf-8")


class TitanCornerHistoryCollectorTests(unittest.TestCase):
    def test_offline_schedule_normalizer_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedules.json"
            j1 = fixture(
                match_id=1,
                competition_key="japan-j1",
                competition_regime="standard",
            )
            regular = fixture(match_id=2, competition_regime="standard")
            for row in (j1, regular):
                row.pop("kickoff_utc")
                row.pop("kickoff_epoch")
                row.pop("source_timezone")
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "source_timezone": "Asia/Shanghai",
                        "matches": [j1, regular],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = collector.normalize_schedule_snapshot(path)
            self.assertTrue(report["written"])
            self.assertEqual(report["matches"], 2)
            self.assertEqual(report["changed_rows"], 2)
            self.assertEqual(report["japan_j1_2026_regime_normalized"], 1)
            normalized = json.loads(path.read_text(encoding="utf-8"))
            by_id = {str(row["match_id"]): row for row in normalized["matches"]}
            self.assertEqual(by_id["1"]["competition_regime"], "2026_vision_regional")
            self.assertEqual(by_id["2"]["competition_regime"], "regular")
            for row in by_id.values():
                self.assertEqual(row["source_timezone"], "Asia/Shanghai")
                self.assertEqual(row["kickoff_utc"], "2026-08-02T06:30:00Z")
                self.assertEqual(row["kickoff_epoch"], 1785652200)
            self.assertEqual(len(collector.load_schedule_files([path])), 2)

            stable_bytes = path.read_bytes()
            stable = collector.normalize_schedule_snapshot(path)
            self.assertFalse(stable["written"])
            self.assertEqual(stable["changed_rows"], 0)
            self.assertEqual(path.read_bytes(), stable_bytes)
            self.assertEqual(
                stable["source_file_sha256_before"],
                stable["source_file_sha256_after"],
            )

    def test_offline_schedule_normalizer_rejects_conflicts_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedules.json"
            value = fixture(kickoff_utc="2026-08-02T07:30:00Z")
            path.write_text(
                json.dumps(
                    {
                        "source_timezone": "Asia/Shanghai",
                        "matches": [value],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            original = path.read_bytes()
            with self.assertRaisesRegex(
                collector.CornerCollectionError, "kickoff_utc conflicts"
            ):
                collector.normalize_schedule_snapshot(path)
            self.assertEqual(path.read_bytes(), original)

            missing_timezone = Path(directory) / "missing-timezone.json"
            missing_timezone.write_text(
                json.dumps({"matches": [fixture()]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                collector.CornerCollectionError, "source_timezone must be"
            ):
                collector.normalize_schedule_snapshot(missing_timezone)

    def test_analysis_header_is_identity_bound_and_parses_full_corners(self):
        record = collector.parse_analysis_header(
            fixture(), header(), "2026-08-03T00:00:00Z"
        )
        self.assertEqual(record["corner_data_status"], "complete")
        self.assertEqual((record["home_corners"], record["away_corners"]), (5, 5))
        self.assertEqual(record["total_corners"], 10)
        self.assertEqual(record["corner_period"], "regulation_90")
        self.assertRegex(record["source_response_sha256"], r"^sha256:[0-9a-f]{64}$")

        with self.assertRaisesRegex(collector.CornerCollectionError, "match_id"):
            collector.parse_analysis_header(
                fixture(), header(match_id="1"), "2026-08-03T00:00:00Z"
            )

    def test_missing_corner_fields_remain_null_and_extra_time_is_quarantined(self):
        missing = collector.parse_analysis_header(
            fixture(),
            header(home_corners="", away_corners=""),
            "2026-08-03T00:00:00Z",
        )
        self.assertEqual(missing["corner_data_status"], "missing")
        self.assertIsNone(missing["home_corners"])
        self.assertIsNone(missing["away_corners"])

        extra = collector.parse_analysis_header(
            fixture(raw_tail=["点球"]), header(), "2026-08-03T00:00:00Z"
        )
        self.assertEqual(extra["corner_data_status"], "extra_time_ambiguous")
        self.assertEqual(extra["corner_period"], "unverified")

    def test_handicap_fallback_is_bound_and_never_invents_half_corners(self):
        payload = {
            "Sche": {
                "ScheduleID": 2929679,
                "MatchState": -1,
                "HomeTeamID": 4075,
                "AwayTeamID": 497,
                "HomeTeam": "济州SK",
                "AwayTeam": "仁川联队",
                "HomeCorner": 5,
                "AwayCorner": 5,
            }
        }
        record = collector.parse_handicap_result(
            fixture(),
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "2026-08-03T00:00:00Z",
        )
        self.assertEqual(record["total_corners"], 10)
        self.assertIsNone(record["half_home_corners"])
        self.assertIsNone(record["half_away_corners"])
        self.assertEqual(record["source_fallback"], "HandicapDataInterface.Sche")

        payload["Sche"]["HomeTeamID"] = 999
        with self.assertRaisesRegex(collector.CornerCollectionError, "HomeTeamID"):
            collector.parse_handicap_result(
                fixture(),
                json.dumps(payload).encode("utf-8"),
                "2026-08-03T00:00:00Z",
            )

    def test_schedule_loader_deduplicates_exact_ids_and_rejects_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(
                json.dumps({"matches": [fixture()]}, ensure_ascii=False),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps({"matches": [fixture()]}, ensure_ascii=False),
                encoding="utf-8",
            )
            self.assertEqual(len(collector.load_schedule_files([first, second])), 1)

            second.write_text(
                json.dumps(
                    {"matches": [fixture(away_team="错误客队")]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(collector.CornerCollectionError, "conflicts"):
                collector.load_schedule_files([first, second])

    def test_checkpoint_retries_fetch_errors_but_not_terminal_qa_outcomes(self):
        fixtures = [fixture(match_id=1), fixture(match_id=2), fixture(match_id=3)]
        checkpoint = {
            "1": checkpoint_record(fixtures[0]),
            "2": checkpoint_record(fixtures[1], "fetch_error"),
            "3": checkpoint_record(fixtures[2], "extra_time_ambiguous"),
        }
        pending = collector.pending_fixtures(fixtures, checkpoint)
        self.assertEqual([row["match_id"] for row in pending], [2])

    def test_checkpoint_is_fixture_bound_and_final_excludes_foreign_ids(self):
        fixtures = [fixture(match_id=1), fixture(match_id=2)]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            forged = checkpoint_record(fixtures[0])
            forged["home_team"] = "forged home"
            valid = checkpoint_record(fixtures[1])
            foreign = checkpoint_record(fixture(match_id=999))
            checkpoint_path = output / "corner_history.partial.ndjson"
            checkpoint_path.write_text(
                "\n".join(json.dumps(row) for row in (forged, valid, foreign)) + "\n",
                encoding="utf-8",
            )

            fetched_ids = []

            def fake_fetch(value, _limiter):
                fetched_ids.append(value["match_id"])
                return {
                    **checkpoint_record(value),
                    "kickoff": value["kickoff"],
                    "corner_odds": [],
                }

            with mock.patch.object(collector, "fetch_fixture", side_effect=fake_fetch):
                final_path, _qa_path = collector.collect(
                    fixtures,
                    output,
                    workers=1,
                    requests_per_second=100.0,
                )
            final = json.loads(final_path.read_text(encoding="utf-8"))
            self.assertEqual(fetched_ids, [1])
            self.assertEqual({row["match_id"] for row in final["matches"]}, {"1", "2"})
            self.assertNotIn(
                "forged home", {row.get("home_team") for row in final["matches"]}
            )

    def test_j1_2026_regime_is_normalized_before_checkpoint_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            path.write_text(
                json.dumps(
                    {
                        "matches": [
                            fixture(
                                competition_key="japan-j1",
                                competition_regime="standard",
                            )
                        ]
                    }
                ),
                encoding="utf-8",
            )
            loaded = collector.load_schedule_files([path])[0]
            self.assertEqual(loaded["competition_regime"], "2026_vision_regional")
            old = checkpoint_record(
                fixture(
                    competition_key="japan-j1",
                    competition_regime="standard",
                )
            )
            self.assertFalse(collector.checkpoint_matches_fixture(old, loaded))

    def test_legacy_checkpoint_migrates_per_fixture_but_j1_2026_refetches(self):
        scheduled = fixture(competition_regime="regular")
        legacy = checkpoint_record(scheduled)
        for field in (
            "kickoff_utc",
            "kickoff_epoch",
            "source_timezone",
            "schedule_fixture_sha256",
        ):
            legacy.pop(field)
        legacy["competition_regime"] = "standard"
        legacy["source_url"] = collector.header_url(str(scheduled["match_id"]))
        legacy["source_response_sha256"] = "sha256:" + "a" * 64
        migrated = collector.upgrade_legacy_checkpoint_record(legacy, scheduled)
        self.assertIsNotNone(migrated)
        assert migrated is not None
        self.assertTrue(collector.checkpoint_matches_fixture(migrated, scheduled))

        j1 = fixture(
            competition_key="japan-j1",
            competition_regime="2026_vision_regional",
        )
        legacy_j1 = dict(legacy)
        legacy_j1.update(
            {
                "competition_key": "japan-j1",
                "competition_regime": "standard",
                "source_url": collector.header_url(str(j1["match_id"])),
            }
        )
        self.assertIsNone(collector.upgrade_legacy_checkpoint_record(legacy_j1, j1))

    def test_one_sided_header_corner_uses_identity_bound_fallback(self):
        fallback_payload = {
            "Sche": {
                "ScheduleID": 2929679,
                "MatchState": -1,
                "HomeTeamID": 4075,
                "AwayTeamID": 497,
                "HomeTeam": "home",
                "AwayTeam": "away",
                "HomeCorner": 7,
                "AwayCorner": 2,
            }
        }

        class Response:
            def __init__(self, payload: bytes):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        responses = [
            Response(header(home_corners="7", away_corners="")),
            Response(json.dumps(fallback_payload).encode("utf-8")),
        ]
        limiter = mock.Mock()
        with mock.patch.object(collector, "urlopen", side_effect=responses):
            record = collector.fetch_fixture(fixture(), limiter, attempts=1)
        self.assertEqual(record["corner_data_status"], "complete")
        self.assertEqual((record["home_corners"], record["away_corners"]), (7, 2))
        self.assertEqual(record["source_fallback"], "HandicapDataInterface.Sche")
        self.assertIn("only one corner count", record["analysis_header_parse_error"])
        self.assertEqual(limiter.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()

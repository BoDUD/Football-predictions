from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import titan_corner_odds_collector as collector


KICKOFF = 1_700_000_000


def fixture(match_id: str = "2929679") -> dict[str, object]:
    return {
        "match_id": match_id,
        "competition_key": "kor-k1",
        "season_label": "2026",
        "kickoff_epoch": KICKOFF,
        "kickoff": "2023-11-14T22:13:20Z",
        "kickoff_utc": "2023-11-14T22:13:20Z",
        "source_timezone": "UTC",
        "home_team_id": 4075,
        "away_team_id": 497,
        "home_team": "济州SK",
        "away_team": "仁川联队",
    }


def request_item(
    spec: collector.MarketSpec = collector.FULL_TOTAL,
    *,
    match_id: str = "2929679",
    company_id: int = 8,
) -> dict[str, object]:
    row = fixture(match_id)
    return collector.build_request_plan(
        [row], [company_id], include_half_total=spec is collector.HALF_TOTAL
    )[
        2 if spec is collector.HALF_TOTAL else (1 if spec is collector.FULL_HANDICAP else 0)
    ]


def detail(
    kind: str,
    modify_time: object,
    *,
    happen_time: object = "",
    score: object = "",
    home_odds: object = 0.9,
    line: object = 10.5,
    away_odds: object = 0.9,
    is_close: object = False,
) -> dict[str, object]:
    return {
        "HappenTime": happen_time,
        "Score": score,
        "HomeOdds": home_odds,
        "DrawOdds": line,
        "AwayOdds": away_odds,
        "ModifyTime": str(modify_time),
        "IsClose": is_close,
        "Kind": kind,
    }


def response_bytes(
    rows: list[object],
    *,
    match_id: int = 2929679,
    match_time: object = KICKOFF,
) -> bytes:
    return json.dumps(
        {
            "Sche": {
                "ScheduleID": match_id,
                "MatchState": -1,
                "HomeTeamID": 4075,
                "AwayTeamID": 497,
                "HomeCorner": 5,
                "AwayCorner": 5,
                "MatchTime": str(match_time),
                "HomeTeam": "济州SK",
                "AwayTeam": "仁川联队",
            },
            "DetailList": rows,
        },
        ensure_ascii=False,
    ).encode("utf-8")


class TitanCornerOddsCollectorTests(unittest.TestCase):
    def test_strict_filter_rejects_every_in_play_leakage_signal(self) -> None:
        rows = [
            detail("INITIAL", KICKOFF - 500, line=9.5),
            detail("EARLY", KICKOFF - 200, line=10.0),
            detail("REAL", KICKOFF - 10, line=10.5),
            detail("REAL", KICKOFF - 10, line=10.5),  # exact duplicate
            detail("RUNNING", KICKOFF - 1000, line=8.5),
            detail("REAL", KICKOFF, line=11.0),
            detail("REAL", KICKOFF - 20, happen_time="1", line=8.5),
            detail("REAL", KICKOFF - 20, score="0-0", line=8.5),
            detail("UNKNOWN", KICKOFF - 20, line=8.5),
            detail("REAL", "not-an-epoch", line=8.5),
            detail("REAL", KICKOFF - 20, home_odds=0, line=8.5),
            detail("REAL", KICKOFF - 20, line=0),
        ]
        snapshots, qa, flags = collector.select_verified_prematch_snapshots(
            rows,
            kickoff_epoch=KICKOFF,
            spec=collector.FULL_TOTAL,
        )

        self.assertEqual([row["kind"] for row in snapshots], ["INITIAL", "EARLY", "REAL"])
        self.assertEqual(snapshots[-1]["seconds_before_kickoff"], 10)
        self.assertEqual(qa["verified_unique_snapshots"], 3)
        self.assertEqual(qa["duplicate_verified_records_removed"], 1)
        reasons = qa["rejection_reason_counts"]
        self.assertEqual(reasons["running_kind"], 1)
        self.assertEqual(reasons["at_or_after_kickoff"], 1)
        self.assertEqual(reasons["happen_time_present"], 1)
        self.assertEqual(reasons["score_present"], 1)
        self.assertEqual(reasons["kind_not_verified_prematch"], 1)
        self.assertEqual(reasons["modify_time_invalid"], 1)
        self.assertEqual(reasons["price_not_positive"], 1)
        self.assertEqual(reasons["total_line_not_positive"], 1)
        self.assertEqual(flags, [])

    def test_handicap_zero_line_is_valid_and_closed_latest_is_flagged(self) -> None:
        rows = [
            detail("INITIAL", KICKOFF - 100, line=0),
            detail("REAL", KICKOFF - 5, line=-0.5, is_close=True),
        ]
        snapshots, qa, flags = collector.select_verified_prematch_snapshots(
            rows,
            kickoff_epoch=KICKOFF,
            spec=collector.FULL_HANDICAP,
        )
        self.assertEqual([row["line"] for row in snapshots], [0.0, -0.5])
        self.assertEqual(qa["verified_unique_snapshots"], 2)
        self.assertIn("latest_verified_market_is_closed", flags)

    def test_parse_response_binds_source_and_exposes_opening_and_latest(self) -> None:
        raw = response_bytes(
            [
                detail("RUNNING", KICKOFF + 60, happen_time="1", score="0-0"),
                detail("REAL", KICKOFF - 30, line=10.5),
                detail("INITIAL", KICKOFF - 3600, line=9.5),
            ]
        )
        request = request_item()
        record = collector.parse_source_response(
            request, raw, "2026-08-03T00:00:00Z"
        )

        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["market_key"], "full_total")
        self.assertEqual(record["price_semantics"]["home_odds"], "over_odds")
        self.assertEqual(record["opening_snapshot"]["line"], 9.5)
        self.assertEqual(record["pre_kickoff_snapshot"]["line"], 10.5)
        self.assertEqual(
            record["source_response_sha256"],
            "sha256:" + hashlib.sha256(raw).hexdigest(),
        )
        self.assertIn("type=4", record["source_url"])
        self.assertIn("oddskind=2", record["source_url"])
        self.assertIn("companyid=8", record["source_url"])
        self.assertIn("isHalf=0", record["source_url"])
        self.assertTrue(record["selection_policy"]["modify_time_strictly_before_match_time"])

    def test_effective_cutoff_is_earlier_of_schedule_and_source_kickoff(self) -> None:
        quote = detail("REAL", KICKOFF - 30, line=10.5)
        source_earlier = collector.parse_source_response(
            request_item(),
            response_bytes([quote], match_time=KICKOFF - 60),
            "2026-08-03T00:00:00Z",
        )
        self.assertEqual(source_earlier["status"], "no_verified_snapshot")
        self.assertEqual(
            source_earlier["effective_prematch_cutoff_epoch"], KICKOFF - 60
        )

        earlier_fixture = fixture()
        earlier_fixture.update(
            {
                "kickoff_epoch": KICKOFF - 60,
                "kickoff": "2023-11-14T22:12:20Z",
                "kickoff_utc": "2023-11-14T22:12:20Z",
            }
        )
        request = collector.build_request_plan(
            [earlier_fixture], [8], include_half_total=False
        )[0]
        schedule_earlier = collector.parse_source_response(
            request,
            response_bytes([quote], match_time=KICKOFF),
            "2026-08-03T00:00:00Z",
        )
        self.assertEqual(schedule_earlier["status"], "no_verified_snapshot")
        self.assertEqual(
            schedule_earlier["effective_prematch_cutoff_epoch"], KICKOFF - 60
        )

    def test_timezone_less_times_use_explicit_source_timezone(self) -> None:
        row = fixture()
        row["kickoff"] = "2023-11-14 22:13:20"
        request = collector.build_request_plan([row], [8], include_half_total=False)[0]
        record = collector.parse_source_response(
            request,
            response_bytes(
                [detail("INITIAL", KICKOFF - 10)],
                match_time="2023-11-14 22:13:20",
            ),
            "2026-08-03T00:00:00Z",
        )
        self.assertEqual(record["fixture_kickoff_epoch"], KICKOFF)
        self.assertEqual(record["source_match_time_epoch"], KICKOFF)
        self.assertEqual(record["status"], "complete")

    def test_old_schedule_without_kickoff_epoch_fails_before_request_plan(self) -> None:
        row = fixture()
        row.pop("kickoff_epoch")
        with self.assertRaisesRegex(
            collector.CornerOddsCollectionError, "kickoff_epoch is required"
        ):
            collector.build_request_plan([row], [8], include_half_total=False)

    def test_parse_response_rejects_schedule_or_team_mismatch(self) -> None:
        request = request_item()
        with self.assertRaisesRegex(
            collector.CornerOddsCollectionError, "ScheduleID"
        ):
            collector.parse_source_response(
                request,
                response_bytes([], match_id=1234567),
                "2026-08-03T00:00:00Z",
            )

        payload = json.loads(response_bytes([]))
        payload["Sche"]["HomeTeamID"] = 999
        with self.assertRaisesRegex(
            collector.CornerOddsCollectionError, "HomeTeamID"
        ):
            collector.parse_source_response(
                request,
                json.dumps(payload).encode("utf-8"),
                "2026-08-03T00:00:00Z",
            )

    def test_no_safe_rows_is_explicit_null_not_running_fallback(self) -> None:
        raw = response_bytes(
            [detail("RUNNING", KICKOFF + 20, happen_time="1", score="1-0")]
        )
        record = collector.parse_source_response(
            request_item(), raw, "2026-08-03T00:00:00Z"
        )
        self.assertEqual(record["status"], "no_verified_snapshot")
        self.assertEqual(record["snapshots"], [])
        self.assertIsNone(record["opening_snapshot"])
        self.assertIsNone(record["pre_kickoff_snapshot"])
        self.assertEqual(
            record["filtering_qa"]["rejection_reason_counts"]["running_kind"],
            1,
        )

    def test_request_plan_uses_type4_market_mapping(self) -> None:
        plan = collector.build_request_plan(
            [fixture()], [3, 8], include_half_total=True
        )
        self.assertEqual(len(plan), 6)
        keys = {(row["spec"].key, row["company_id"]): row for row in plan}
        self.assertIn("oddskind=2", keys[("full_total", 3)]["source_url"])
        self.assertIn("isHalf=0", keys[("full_total", 3)]["source_url"])
        self.assertIn("oddskind=1", keys[("full_handicap", 3)]["source_url"])
        self.assertIn("isHalf=0", keys[("full_handicap", 3)]["source_url"])
        self.assertIn("oddskind=2", keys[("half_total", 3)]["source_url"])
        self.assertIn("isHalf=1", keys[("half_total", 3)]["source_url"])

    def test_schedule_loader_accepts_multiple_shapes_and_rejects_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = base / "first.json"
            second = base / "second.json"
            first.write_text(
                json.dumps({"matches": [fixture()]}, ensure_ascii=False),
                encoding="utf-8",
            )
            extra = fixture("1234567")
            second.write_text(json.dumps([extra], ensure_ascii=False), encoding="utf-8")
            loaded = collector.load_schedule_files([first, second])
            self.assertEqual({row["match_id"] for row in loaded}, {"2929679", "1234567"})

            conflicting = fixture()
            conflicting["home_team_id"] = 999
            second.write_text(
                json.dumps([conflicting], ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                collector.CornerOddsCollectionError, "conflicts"
            ):
                collector.load_schedule_files([first, second])

    def test_company_ids_support_repeat_and_commas(self) -> None:
        self.assertEqual(collector.parse_company_ids(None), [3, 8, 47])
        self.assertEqual(
            collector.parse_company_ids(["8", "47,3", "8"]), [3, 8, 47]
        )
        with self.assertRaises(collector.CornerOddsCollectionError):
            collector.parse_company_ids(["3,"])

    def test_collection_checkpoint_resumes_per_market_and_company(self) -> None:
        rows = [detail("INITIAL", KICKOFF - 300, line=10.0)]

        def fake_fetch(request, _limiter, **kwargs):
            raw = response_bytes(rows)
            record = collector.parse_source_response(
                request, raw, "2026-08-03T00:00:00Z"
            )
            record["attempts_used"] = 1
            record["source_response_path"] = collector._store_raw(
                kwargs["raw_dir"], raw
            )
            return record

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with mock.patch.object(
                collector, "fetch_request", side_effect=fake_fetch
            ) as first_fetch:
                final_path, qa_path, checkpoint_path = collector.collect(
                    [fixture()],
                    output,
                    company_ids=[8],
                    include_half_total=False,
                    workers=1,
                    requests_per_second=10.0,
                    attempts=1,
                    timeout=1.0,
                    keep_raw=False,
                )
            self.assertEqual(first_fetch.call_count, 2)
            self.assertEqual(len(checkpoint_path.read_text(encoding="utf-8").splitlines()), 2)

            with mock.patch.object(
                collector,
                "fetch_request",
                side_effect=AssertionError("resumed requests must not be fetched"),
            ) as second_fetch:
                collector.collect(
                    [fixture()],
                    output,
                    company_ids=[8],
                    include_half_total=False,
                    workers=1,
                    requests_per_second=10.0,
                    attempts=1,
                    timeout=1.0,
                    keep_raw=False,
                )
            second_fetch.assert_not_called()
            payload = json.loads(final_path.read_text(encoding="utf-8"))
            qa = json.loads(qa_path.read_text(encoding="utf-8"))
            self.assertEqual(qa["resumed_requests"], 2)
            self.assertEqual(qa["fetched_requests"], 0)
            self.assertEqual(qa["match_market_coverage"], 1.0)
            self.assertEqual(
                set(payload["matches"][0]["markets"]),
                {"full_total", "full_handicap"},
            )

    def test_checkpoint_repairs_only_an_incomplete_final_line(self) -> None:
        request = request_item()
        record = collector.parse_source_response(
            request,
            response_bytes([detail("INITIAL", KICKOFF - 10)]),
            "2026-08-03T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.ndjson"
            valid_line = collector._canonical_json(record) + "\n"
            path.write_bytes(valid_line.encode("utf-8") + b'{"request_key":')
            loaded = collector.load_checkpoint(path, repair_truncated_tail=True)
            self.assertEqual(set(loaded), {record["request_key"]})
            self.assertEqual(path.read_text(encoding="utf-8"), valid_line)

            path.write_text(valid_line + "not-json\n", encoding="utf-8")
            with self.assertRaisesRegex(
                collector.CornerOddsCollectionError, "invalid JSON"
            ):
                collector.load_checkpoint(path, repair_truncated_tail=True)

    def test_checkpoint_resume_requires_exact_identity_and_raw_replay(self) -> None:
        request = request_item()
        raw = response_bytes([detail("INITIAL", KICKOFF - 10)])
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            record = collector.parse_source_response(
                request, raw, "2026-08-03T00:00:00Z"
            )
            record["attempts_used"] = 1
            record["source_response_path"] = collector._store_raw(
                output / "raw", raw
            )
            self.assertTrue(collector._can_resume(record, request, output))

            for field, value in (
                ("market_key", "full_handicap"),
                ("match_id", "1234567"),
            ):
                forged = dict(record)
                forged[field] = value
                self.assertFalse(collector._can_resume(forged, request, output))

            forged_quote = json.loads(json.dumps(record))
            forged_quote["snapshots"][0]["line"] = 99.0
            forged_quote["opening_snapshot"]["line"] = 99.0
            forged_quote["pre_kickoff_snapshot"]["line"] = 99.0
            self.assertFalse(collector._can_resume(forged_quote, request, output))

            no_raw = dict(record)
            no_raw.pop("source_response_path")
            self.assertFalse(collector._can_resume(no_raw, request, output))


if __name__ == "__main__":
    unittest.main()

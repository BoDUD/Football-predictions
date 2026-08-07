from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import memory_store, source_evidence


def raw_snapshot(*, collected_at: str = "2026-08-06T09:30:00Z") -> dict:
    return {
        "schema_version": source_evidence.RAW_SCHEMA_VERSION,
        "source_url": "https://zq.titan007.com/analysis/123cn.htm",
        "collected_at": collected_at,
        "http_metadata": {"date": "Thu, 06 Aug 2026 09:30:00 GMT"},
        "fixture": {
            "match_id": "123",
            "home_team": "Alpha",
            "away_team": "Bravo",
            "kickoff": "2026-08-06T10:00:00Z",
        },
        "markets": [
            {
                "market_identity": {
                    "family": "total",
                    "period": "full_time",
                    "line": 2.5,
                    "price_outcomes": ["over", "under"],
                },
                "odds_format": "decimal",
                "firms": [
                    {"name": "A", "outcomes": {"over": 2.0, "under": 1.9}},
                    {"name": "B", "outcomes": {"over": 2.2, "under": 1.8}},
                    {"name": "C", "outcomes": {"over": 2.1, "under": 1.85}},
                ],
            }
        ],
    }


class SourceEvidenceTests(unittest.TestCase):
    def test_v2_fixture_token_and_output_path_are_cross_platform_fail_closed(
        self,
    ) -> None:
        unsafe_tokens = (
            "../../escape",
            r"..\escape",
            "/absolute/escape",
            r"C:\escape",
            r"\\server\share",
            ".",
            "..",
            "CON",
        )
        for token in unsafe_tokens:
            with self.subTest(token=token):
                snapshot = raw_snapshot()
                snapshot["fixture"]["match_id"] = token
                with self.assertRaisesRegex(
                    source_evidence.SourceEvidenceError,
                    "portable ASCII fixture token",
                ):
                    source_evidence.parse_raw_snapshot(json.dumps(snapshot).encode())

        legacy = raw_snapshot()
        legacy["schema_version"] = source_evidence.LEGACY_RAW_SCHEMA_VERSION
        legacy["fixture"]["match_id"] = "../escaped"
        legacy_market = legacy["markets"][0]
        legacy_market["market"] = "total"
        legacy_market.pop("market_identity")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            raw_file = base / "legacy-traversal.json"
            raw_file.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(
                source_evidence.SourceEvidenceError, "output path escapes"
            ):
                source_evidence.build_evidence([raw_file], output_dir=base / "evidence")
            self.assertFalse((base / "escaped-source-evidence.json").exists())

    def test_repository_visible_snapshot_fixture_replays(self) -> None:
        fixture = (
            Path(__file__).resolve().parents[1]
            / "analysis"
            / "fixtures"
            / "visible_market_snapshot.json"
        )
        parsed = source_evidence.parse_raw_snapshot(fixture.read_bytes())
        self.assertEqual(parsed["fixture"]["match_id"], "123")
        self.assertEqual(parsed["http_metadata"]["status_code"], 200)
        self.assertEqual(parsed["request_metadata"]["host"], "zq.titan007.com")

    def test_build_replay_and_candidate_price_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            raw_file = base / "visible.json"
            raw_file.write_text(
                json.dumps(raw_snapshot(), ensure_ascii=False), encoding="utf-8"
            )
            evidence_file, evidence = source_evidence.build_evidence(
                [raw_file], output_dir=base / "evidence"
            )
            replayed = source_evidence.validate_evidence_file(evidence_file)
            self.assertEqual(replayed, evidence)
            binding = source_evidence.match_candidate(
                replayed,
                {
                    "market_identity": {
                        "family": "total",
                        "period": "full_time",
                        "line": 2.5,
                        "price_outcomes": ["over", "under"],
                    },
                    "market_identity_hash": source_evidence.market_identity_hash(
                        {
                            "family": "total",
                            "period": "full_time",
                            "line": 2.5,
                            "price_outcomes": ["over", "under"],
                        }
                    ),
                    "market_collected_at": "2026-08-06T09:30:00Z",
                    "odds_format": "decimal",
                    "price_basis": "median",
                    "market_source": "https://zq.titan007.com/analysis/123cn.htm",
                    "firm_count": 3,
                    "complete_market_odds": {
                        "over": 2.1,
                        "under": 1.85,
                    },
                },
            )
            self.assertEqual(binding["evidence_hash"], evidence["evidence_hash"])
            self.assertEqual(binding["firm_count"], 3)
            self.assertEqual(binding["market_identity"]["line"], 2.5)

            with self.assertRaises(source_evidence.SourceEvidenceError):
                source_evidence.match_candidate(
                    replayed,
                    {
                        "market_identity": binding["market_identity"],
                        "market_identity_hash": binding["market_identity_hash"],
                        "market_collected_at": "2026-08-06T09:30:00Z",
                        "odds_format": "decimal",
                        "price_basis": "median",
                        "market_source": "https://zq.titan007.com/analysis/123cn.htm",
                        "firm_count": 99,
                        "complete_market_odds": {
                            "over": 2.1,
                            "under": 1.85,
                        },
                    },
                )

    def test_evidence_bundle_is_append_only_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            original_file = base / "original.json"
            original_file.write_text(json.dumps(raw_snapshot()), encoding="utf-8")
            output, evidence = source_evidence.build_evidence(
                [original_file], output_dir=base / "evidence"
            )
            original_bytes = output.read_bytes()

            repeated_output, repeated_evidence = source_evidence.build_evidence(
                [original_file], output_dir=base / "evidence"
            )
            self.assertEqual(repeated_output, output)
            self.assertEqual(repeated_evidence, evidence)
            self.assertEqual(output.read_bytes(), original_bytes)

            changed_file = base / "changed.json"
            changed = raw_snapshot(collected_at="2026-08-06T09:20:00Z")
            changed["markets"][0]["firms"][0]["outcomes"]["over"] = 2.4
            changed_file.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(
                source_evidence.SourceEvidenceError,
                "different source evidence already exists",
            ):
                source_evidence.build_evidence(
                    [changed_file], output_dir=base / "evidence"
                )
            self.assertEqual(output.read_bytes(), original_bytes)
            self.assertEqual(source_evidence.validate_evidence_file(output), evidence)

    def test_raw_tamper_breaks_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            raw_file = base / "visible.json"
            raw_file.write_text(json.dumps(raw_snapshot()), encoding="utf-8")
            evidence_file, evidence = source_evidence.build_evidence(
                [raw_file], output_dir=base / "evidence"
            )
            stored = evidence_file.parent / evidence["sources"][0]["raw_response_path"]
            stored.write_text(
                json.dumps(raw_snapshot(collected_at="2026-08-06T09:20:00Z")),
                encoding="utf-8",
            )
            with self.assertRaises(source_evidence.SourceEvidenceError):
                source_evidence.validate_evidence_file(evidence_file)

    def test_replay_rejects_absolute_traversal_and_noncanonical_raw_paths(self) -> None:
        for path_kind in ("absolute", "traversal", "alias"):
            with (
                self.subTest(path_kind=path_kind),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary)
                raw_file = base / "visible.json"
                raw_file.write_text(json.dumps(raw_snapshot()), encoding="utf-8")
                evidence_file, evidence = source_evidence.build_evidence(
                    [raw_file], output_dir=base / "evidence"
                )
                stored = (
                    evidence_file.parent / evidence["sources"][0]["raw_response_path"]
                )
                if path_kind == "absolute":
                    replacement = str(stored.resolve())
                elif path_kind == "traversal":
                    outside = evidence_file.parent / "outside.json"
                    outside.write_bytes(stored.read_bytes())
                    replacement = "../evidence/outside.json"
                else:
                    alias = evidence_file.parent / "raw" / "alias.json"
                    alias.write_bytes(stored.read_bytes())
                    replacement = "raw/alias.json"
                evidence["sources"][0]["raw_response_path"] = replacement
                evidence.pop("evidence_hash")
                evidence["evidence_hash"] = source_evidence._hash_json(evidence)
                evidence_file.write_text(json.dumps(evidence), encoding="utf-8")
                with self.assertRaisesRegex(
                    source_evidence.SourceEvidenceError,
                    "raw source path|content-addressed path",
                ):
                    source_evidence.validate_evidence_file(evidence_file)

    def test_rejects_post_kickoff_and_incomplete_market(self) -> None:
        after = raw_snapshot(collected_at="2026-08-06T10:00:00Z")
        with self.assertRaisesRegex(
            source_evidence.SourceEvidenceError, "before kickoff"
        ):
            source_evidence.parse_raw_snapshot(json.dumps(after).encode())

        incomplete = raw_snapshot()
        incomplete["markets"][0]["firms"][0]["outcomes"] = {"over": 2.0}
        with self.assertRaises(source_evidence.SourceEvidenceError):
            source_evidence.parse_raw_snapshot(json.dumps(incomplete).encode())

        unavailable = raw_snapshot()
        unavailable["availability_status"] = "unavailable"
        unavailable["unavailable_reasons"] = ["provider market table absent"]
        unavailable["markets"] = []
        parsed = source_evidence.parse_raw_snapshot(json.dumps(unavailable).encode())
        self.assertEqual(parsed["availability_status"], "unavailable")
        self.assertEqual(parsed["markets"], [])

    def test_v2_rejects_unsafe_period_negative_total_and_impossible_decimal_price(
        self,
    ) -> None:
        second_half = raw_snapshot()
        second_half["markets"][0]["market_identity"]["period"] = "second_half"
        with self.assertRaisesRegex(
            source_evidence.SourceEvidenceError, "not settlement-safe"
        ):
            source_evidence.parse_raw_snapshot(json.dumps(second_half).encode())

        first_half_corners = raw_snapshot()
        corner_identity = first_half_corners["markets"][0]["market_identity"]
        corner_identity.update(
            {
                "family": "corner_total",
                "period": "first_half",
                "line": 9.5,
            }
        )
        with self.assertRaisesRegex(
            source_evidence.SourceEvidenceError, "not settlement-safe"
        ):
            source_evidence.parse_raw_snapshot(json.dumps(first_half_corners).encode())

        negative_total = raw_snapshot()
        negative_total["markets"][0]["market_identity"]["line"] = -2.25
        with self.assertRaisesRegex(
            source_evidence.SourceEvidenceError, "cannot be negative"
        ):
            source_evidence.parse_raw_snapshot(json.dumps(negative_total).encode())

        invalid_decimal = raw_snapshot()
        invalid_decimal["markets"][0]["firms"][0]["outcomes"]["over"] = 0.9
        with self.assertRaisesRegex(
            source_evidence.SourceEvidenceError,
            "decimal odds must be greater than 1",
        ):
            source_evidence.parse_raw_snapshot(json.dumps(invalid_decimal).encode())

        hong_kong = raw_snapshot()
        hong_kong["markets"][0]["odds_format"] = "hong_kong"
        for firm in hong_kong["markets"][0]["firms"]:
            firm["outcomes"] = {"over": 0.9, "under": 0.95}
        parsed = source_evidence.parse_raw_snapshot(json.dumps(hong_kong).encode())
        self.assertEqual(parsed["markets"][0]["odds_format"], "hong_kong")

        legacy_decimal = raw_snapshot()
        legacy_decimal["schema_version"] = source_evidence.LEGACY_RAW_SCHEMA_VERSION
        legacy_market = legacy_decimal["markets"][0]
        legacy_market["market"] = "total"
        legacy_market.pop("market_identity")
        legacy_market["firms"][0]["outcomes"]["over"] = 0.9
        parsed_legacy = source_evidence.parse_raw_snapshot(
            json.dumps(legacy_decimal).encode()
        )
        self.assertEqual(
            parsed_legacy["parser_version"], source_evidence.LEGACY_PARSER_VERSION
        )

    def test_same_family_multiple_lines_are_indexed_and_matched_by_identity(
        self,
    ) -> None:
        snapshot = raw_snapshot()
        second = json.loads(json.dumps(snapshot["markets"][0]))
        second["market_identity"]["line"] = 2.25
        second["firms"] = [
            {"name": "A", "outcomes": {"over": 1.8, "under": 2.1}},
            {"name": "B", "outcomes": {"over": 1.9, "under": 2.0}},
            {"name": "C", "outcomes": {"over": 1.85, "under": 2.05}},
        ]
        snapshot["markets"].append(second)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            raw_file = base / "visible.json"
            raw_file.write_text(json.dumps(snapshot), encoding="utf-8")
            evidence_file, evidence = source_evidence.build_evidence(
                [raw_file], output_dir=base / "evidence"
            )
            replayed = source_evidence.validate_evidence_file(evidence_file)
            identity = {
                "family": "total",
                "period": "full_time",
                "line": 2.25,
                "price_outcomes": ["over", "under"],
            }
            identity_hash = source_evidence.market_identity_hash(identity)
            binding = source_evidence.match_candidate(
                replayed,
                {
                    "market_identity": identity,
                    "market_identity_hash": identity_hash,
                    "market_collected_at": "2026-08-06T09:30:00Z",
                    "odds_format": "decimal",
                    "price_basis": "median",
                    "market_source": "https://zq.titan007.com/analysis/123cn.htm",
                    "firm_count": 3,
                    "complete_market_odds": {"over": 1.85, "under": 2.05},
                },
            )
        self.assertEqual(len(evidence["market_index"]), 2)
        self.assertEqual(binding["market_identity_hash"], identity_hash)
        self.assertEqual(binding["prices"], {"over": 1.85, "under": 2.05})

    def test_goal_range_identity_requires_one_canonical_gap_free_partition(
        self,
    ) -> None:
        valid_outcomes = ["0-1", "2-3", "4-6", "7+"]
        snapshot = raw_snapshot()
        market = snapshot["markets"][0]
        market["market_identity"] = {
            "family": "goal_range",
            "period": "full_time",
            "line": None,
            "price_outcomes": valid_outcomes,
        }
        for firm in market["firms"]:
            firm["outcomes"] = {
                outcome: 4.0 + index for index, outcome in enumerate(valid_outcomes)
            }
        parsed = source_evidence.parse_raw_snapshot(json.dumps(snapshot).encode())
        self.assertEqual(
            parsed["markets"][0]["market_identity"]["price_outcomes"],
            valid_outcomes,
        )

        invalid_partitions = (
            ["0 to 1", "2-3", "4+"],
            ["0-2", "2-3", "4+"],
            ["0-1", "3-4", "5+"],
            ["2-3", "0-1", "4+"],
            ["0-1", "2-3", "4-6"],
            ["00-1", "2-3", "4+"],
        )
        for outcomes in invalid_partitions:
            with self.subTest(outcomes=outcomes):
                invalid = json.loads(json.dumps(snapshot))
                invalid["markets"][0]["market_identity"]["price_outcomes"] = list(
                    outcomes
                )
                for firm in invalid["markets"][0]["firms"]:
                    firm["outcomes"] = {
                        outcome: 4.0 + index for index, outcome in enumerate(outcomes)
                    }
                with self.assertRaises(source_evidence.SourceEvidenceError):
                    source_evidence.parse_raw_snapshot(json.dumps(invalid).encode())

    def test_v2_firm_identity_uses_nfkc_casefold_but_v1_replay_stays_exact(
        self,
    ) -> None:
        duplicate = raw_snapshot()
        duplicate["markets"][0]["firms"][0]["name"] = "Pinnacle"
        duplicate["markets"][0]["firms"][1]["name"] = "ｐｉｎｎａｃｌｅ"
        with self.assertRaisesRegex(
            source_evidence.SourceEvidenceError, "duplicated firm"
        ):
            source_evidence.parse_raw_snapshot(json.dumps(duplicate).encode())

        legacy = json.loads(json.dumps(duplicate))
        legacy["schema_version"] = source_evidence.LEGACY_RAW_SCHEMA_VERSION
        legacy["markets"][0]["market"] = "total"
        legacy["markets"][0].pop("market_identity")
        parsed = source_evidence.parse_raw_snapshot(json.dumps(legacy).encode())
        self.assertEqual(parsed["markets"][0]["firm_count"], 3)

    def test_v2_half_time_1x2_has_one_canonical_family_and_legacy_alias_is_read_only(
        self,
    ) -> None:
        canonical = raw_snapshot()
        market = canonical["markets"][0]
        market["market_identity"] = {
            "family": "1x2",
            "period": "first_half",
            "line": None,
            "price_outcomes": ["H", "D", "A"],
        }
        for firm in market["firms"]:
            firm["outcomes"] = {"H": 2.2, "D": 3.0, "A": 3.4}
        parsed = source_evidence.parse_raw_snapshot(json.dumps(canonical).encode())
        self.assertEqual(
            parsed["markets"][0]["market_identity"],
            market["market_identity"],
        )

        ambiguous = json.loads(json.dumps(canonical))
        ambiguous["markets"][0]["market_identity"]["family"] = "half_time"
        with self.assertRaisesRegex(
            source_evidence.SourceEvidenceError,
            "legacy-only.*family=1x2.*period=first_half",
        ):
            source_evidence.parse_raw_snapshot(json.dumps(ambiguous).encode())

        legacy = json.loads(json.dumps(canonical))
        legacy["schema_version"] = source_evidence.LEGACY_RAW_SCHEMA_VERSION
        legacy["markets"][0]["market"] = "half_time"
        legacy["markets"][0].pop("market_identity")
        parsed_legacy = source_evidence.parse_raw_snapshot(json.dumps(legacy).encode())
        self.assertEqual(parsed_legacy["markets"][0]["market"], "half_time")
        self.assertEqual(
            parsed_legacy["parser_version"], source_evidence.LEGACY_PARSER_VERSION
        )

    def test_v1_family_only_evidence_replays_as_quarantined_read_only(self) -> None:
        legacy = raw_snapshot()
        legacy["schema_version"] = source_evidence.LEGACY_RAW_SCHEMA_VERSION
        legacy_market = legacy["markets"][0]
        legacy_market["market"] = "total"
        legacy_market.pop("market_identity")
        for firm in legacy_market["firms"]:
            firm["outcomes"] = {
                "over:2.5": firm["outcomes"].pop("over"),
                "under:2.5": firm["outcomes"].pop("under"),
            }
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            raw_file = base / "legacy.json"
            raw_file.write_text(json.dumps(legacy), encoding="utf-8")
            evidence_file, evidence = source_evidence.build_evidence(
                [raw_file], output_dir=base / "evidence"
            )
            replayed = source_evidence.validate_evidence_file(evidence_file)
            binding = source_evidence.match_candidate(
                replayed,
                {
                    "market": "total",
                    "market_collected_at": "2026-08-06T09:30:00Z",
                    "odds_format": "decimal",
                    "price_basis": "median",
                    "market_source": "https://zq.titan007.com/analysis/123cn.htm",
                    "firm_count": 3,
                    "complete_market_odds": {
                        "over:2.5": 2.1,
                        "under:2.5": 1.85,
                    },
                },
            )
        self.assertEqual(
            evidence["schema_version"], source_evidence.LEGACY_EVIDENCE_SCHEMA_VERSION
        )
        self.assertEqual(replayed, evidence)
        self.assertEqual(binding["evidence_scope"], "legacy_read_only_quarantined")

    def test_memory_archive_binding_replays_and_enters_revision_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            raw_file = base / "visible.json"
            raw_file.write_text(json.dumps(raw_snapshot()), encoding="utf-8")
            evidence_file, _evidence = source_evidence.build_evidence(
                [raw_file], output_dir=base / "evidence"
            )
            record = {
                "match_id": "123",
                "home_team": "Alpha",
                "away_team": "Bravo",
                "kickoff": "2026-08-06T10:00:00Z",
                "updated_at": "2026-08-06T09:40:00Z",
                "forward_policy_binding": None,
            }
            audit = memory_store.load_source_evidence_audit(
                SimpleNamespace(source_evidence_file=str(evidence_file)), record
            )
            self.assertIsNotNone(audit)
            record["source_evidence_audit"] = audit
            self.assertIsNotNone(memory_store.validated_source_evidence_audit(record))
            self.assertEqual(
                memory_store.revision_snapshot(record)["source_evidence_audit"], audit
            )

            late_context = {
                **record,
                "settlement_basis": {
                    "match_id": record["match_id"],
                    "home_team": record["home_team"],
                    "away_team": record["away_team"],
                    "kickoff": record["kickoff"],
                    "version_archived_at": "2026-08-06T09:20:00Z",
                    "source_evidence_audit": audit,
                },
            }
            self.assertIsNone(
                memory_store.validated_source_evidence_audit(late_context)
            )


if __name__ == "__main__":
    unittest.main()

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
                "market": "total",
                "odds_format": "decimal",
                "firms": [
                    {"name": "A", "outcomes": {"over:2.5": 2.0, "under:2.5": 1.9}},
                    {"name": "B", "outcomes": {"over:2.5": 2.2, "under:2.5": 1.8}},
                    {"name": "C", "outcomes": {"over:2.5": 2.1, "under:2.5": 1.85}},
                ],
            }
        ],
    }


class SourceEvidenceTests(unittest.TestCase):
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
            self.assertEqual(binding["evidence_hash"], evidence["evidence_hash"])
            self.assertEqual(binding["firm_count"], 3)

            with self.assertRaises(source_evidence.SourceEvidenceError):
                source_evidence.match_candidate(
                    replayed,
                    {
                        "market": "total",
                        "market_collected_at": "2026-08-06T09:30:00Z",
                        "odds_format": "decimal",
                        "price_basis": "median",
                        "market_source": "https://zq.titan007.com/analysis/123cn.htm",
                        "firm_count": 99,
                        "complete_market_odds": {
                            "over:2.5": 2.1,
                            "under:2.5": 1.85,
                        },
                    },
                )

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
        incomplete["markets"][0]["firms"][0]["outcomes"] = {"over:2.5": 2.0}
        with self.assertRaises(source_evidence.SourceEvidenceError):
            source_evidence.parse_raw_snapshot(json.dumps(incomplete).encode())

        unavailable = raw_snapshot()
        unavailable["availability_status"] = "unavailable"
        unavailable["unavailable_reasons"] = ["provider market table absent"]
        unavailable["markets"] = []
        parsed = source_evidence.parse_raw_snapshot(json.dumps(unavailable).encode())
        self.assertEqual(parsed["availability_status"], "unavailable")
        self.assertEqual(parsed["markets"], [])

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

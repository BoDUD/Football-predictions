from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import fundamental_evidence


class FundamentalEvidenceTests(unittest.TestCase):
    @staticmethod
    def _snapshot() -> dict:
        players = [f"Player {index}" for index in range(1, 12)]
        return {
            "schema_version": fundamental_evidence.RAW_SCHEMA_VERSION,
            "source_url": "https://zq.titan007.com/analysis/2910001cn.htm",
            "collected_at": "2026-08-08T10:00:00Z",
            "fixture": {
                "match_id": "2910001",
                "home_team": "Home",
                "away_team": "Away",
                "kickoff": "2026-08-08T11:00:00Z",
            },
            "confirmed_lineups": {
                "home": players,
                "away": [f"Away {index}" for index in range(1, 12)],
            },
            "fundamentals": {
                side: {
                    "sample_matches": 10,
                    "goals_for_per_match": 1.5,
                    "goals_against_per_match": 1.0,
                }
                for side in ("home", "away")
            },
            "chance_quality": {
                side: {
                    "sample_matches": 8,
                    "xg_per_match": 1.4,
                    "xga_per_match": 1.1,
                }
                for side in ("home", "away")
            },
            "attack_configuration": {
                side: {
                    "formation": "4-3-3",
                    "recognized_attackers": [f"{side} striker"],
                }
                for side in ("home", "away")
            },
            "opponent_tail_risk": {
                "checked": True,
                "notes": "High-score tail and missing defenders reviewed.",
            },
            "corner_profile": {
                side: {
                    "sample_matches": 10,
                    "corners_for_per_match": 5.2,
                    "corners_against_per_match": 4.4,
                }
                for side in ("home", "away")
            },
        }

    def test_claims_are_derived_from_replayable_content_addressed_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.json"
            source.write_text(
                json.dumps(self._snapshot(), ensure_ascii=False), encoding="utf-8"
            )
            path, evidence = fundamental_evidence.build_evidence(
                [source], output_dir=base / "evidence"
            )
            self.assertTrue(all(evidence["derived_claims"].values()))
            self.assertEqual(
                fundamental_evidence.validate_evidence_file(path), evidence
            )
            raw = Path(path).parent / evidence["sources"][0]["raw_response_path"]
            raw.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                fundamental_evidence.FundamentalEvidenceError,
                "replay|source|schema_version",
            ):
                fundamental_evidence.validate_evidence_file(path)

    def test_claims_cannot_be_self_reported_without_supporting_fields(self) -> None:
        snapshot = self._snapshot()
        snapshot.pop("chance_quality")
        parsed = fundamental_evidence.parse_snapshot(
            json.dumps(snapshot).encode("utf-8")
        )
        self.assertFalse(parsed["derived_claims"]["chance_quality_supported"])
        self.assertTrue(parsed["derived_claims"]["lineup_confirmed"])

    def test_post_kickoff_or_incomplete_lineup_fails_closed(self) -> None:
        post = self._snapshot()
        post["collected_at"] = post["fixture"]["kickoff"]
        with self.assertRaisesRegex(
            fundamental_evidence.FundamentalEvidenceError, "before kickoff"
        ):
            fundamental_evidence.parse_snapshot(json.dumps(post).encode("utf-8"))
        incomplete = self._snapshot()
        incomplete["confirmed_lineups"]["home"].pop()
        with self.assertRaisesRegex(
            fundamental_evidence.FundamentalEvidenceError, "11 unique"
        ):
            fundamental_evidence.parse_snapshot(json.dumps(incomplete).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()

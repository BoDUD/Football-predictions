from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import fundamental_evidence, source_evidence


class FundamentalEvidenceTests(unittest.TestCase):
    @staticmethod
    def _snapshot() -> dict:
        players = [f"Player {index}" for index in range(1, 12)]
        return {
            "schema_version": fundamental_evidence.RAW_SCHEMA_VERSION,
            "source_class": "official_confirmed",
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
                    "recognized_attackers": [
                        "Player 1" if side == "home" else "Away 1"
                    ],
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
            self.assertTrue(all(evidence["availability_claims"].values()))
            self.assertEqual(evidence["candidate_support"], {})
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

    def test_predicted_lineup_and_nonstarter_attacker_cannot_confirm_support(
        self,
    ) -> None:
        predicted = self._snapshot()
        predicted["source_class"] = "predicted_lineup"
        with self.assertRaisesRegex(
            fundamental_evidence.FundamentalEvidenceError,
            "can confirm lineups",
        ):
            fundamental_evidence.parse_snapshot(json.dumps(predicted).encode("utf-8"))

        nonstarter = self._snapshot()
        nonstarter["attack_configuration"]["home"]["recognized_attackers"] = [
            "Not In Starting XI"
        ]
        with self.assertRaisesRegex(
            fundamental_evidence.FundamentalEvidenceError,
            "must be confirmed starters",
        ):
            fundamental_evidence.parse_snapshot(json.dumps(nonstarter).encode("utf-8"))

    def test_conflicting_confirmed_lineups_fail_closed_across_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = self._snapshot()
            second = self._snapshot()
            second["source_url"] = "https://provider.example/fixture/2910001"
            second["collected_at"] = "2026-08-08T10:01:00Z"
            second["confirmed_lineups"]["home"][-1] = "Different Starter"
            first_path = base / "first.json"
            second_path = base / "second.json"
            first_path.write_text(json.dumps(first), encoding="utf-8")
            second_path.write_text(json.dumps(second), encoding="utf-8")
            with self.assertRaisesRegex(
                fundamental_evidence.FundamentalEvidenceError,
                "confirmed lineup sources conflict",
            ):
                fundamental_evidence.build_evidence(
                    [first_path, second_path], output_dir=base / "evidence"
                )

    def test_candidate_direction_is_evaluated_for_exact_identity_and_selection(
        self,
    ) -> None:
        identity = {
            "family": "total",
            "period": "full_time",
            "line": 3.5,
            "price_outcomes": ["over", "under"],
        }
        identity_hash = source_evidence.market_identity_hash(identity)
        snapshot = self._snapshot()
        snapshot["candidate_support_requests"] = [
            {
                "market": "total",
                "selection": selection,
                "market_identity": identity,
                "market_identity_hash": identity_hash,
            }
            for selection in ("over", "under")
        ]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.json"
            source.write_text(json.dumps(snapshot), encoding="utf-8")
            _path, evidence = fundamental_evidence.build_evidence(
                [source], output_dir=base / "evidence"
            )
        support = evidence["candidate_support"]
        self.assertFalse(support[f"{identity_hash}:over"]["directionally_supported"])
        self.assertTrue(support[f"{identity_hash}:under"]["directionally_supported"])
        self.assertFalse(support[f"{identity_hash}:under"]["formal_gate_eligible"])
        self.assertEqual(
            support[f"{identity_hash}:under"]["release_status"],
            "shadow_only_pending_forward_validation",
        )

    def test_deep_asian_direction_requires_tail_risk_and_goal_margin(self) -> None:
        identity = {
            "family": "asian",
            "period": "full_time",
            "line": -0.75,
            "price_outcomes": ["home", "away"],
        }
        identity_hash = source_evidence.market_identity_hash(identity)
        snapshot = self._snapshot()
        snapshot["chance_quality"]["home"].update(xg_per_match=2.2, xga_per_match=0.6)
        snapshot["chance_quality"]["away"].update(xg_per_match=0.6, xga_per_match=1.8)
        snapshot["candidate_support_requests"] = [
            {
                "market": "asian",
                "selection": "home",
                "market_identity": identity,
                "market_identity_hash": identity_hash,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.json"
            source.write_text(json.dumps(snapshot), encoding="utf-8")
            _path, evidence = fundamental_evidence.build_evidence(
                [source], output_dir=base / "evidence"
            )
        item = evidence["candidate_support"][f"{identity_hash}:home"]
        self.assertTrue(item["directionally_supported"])
        self.assertTrue(
            item["source_evaluations"][0]["metrics"]["opponent_tail_risk_checked"]
        )

        without_tail = self._snapshot()
        without_tail.pop("opponent_tail_risk")
        without_tail["candidate_support_requests"] = snapshot[
            "candidate_support_requests"
        ]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source.json"
            source.write_text(json.dumps(without_tail), encoding="utf-8")
            _path, evidence = fundamental_evidence.build_evidence(
                [source], output_dir=base / "evidence"
            )
        item = evidence["candidate_support"][f"{identity_hash}:home"]
        self.assertFalse(item["directionally_supported"])
        self.assertEqual(item["source_evaluations"], [])


if __name__ == "__main__":
    unittest.main()

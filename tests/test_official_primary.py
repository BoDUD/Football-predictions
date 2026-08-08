from __future__ import annotations

import unittest

from scripts import official_primary, plain_text_formatter


def candidate(
    candidate_id: str,
    *,
    probability: float,
    formal: bool = False,
    counterfactual: bool = False,
    shadow: bool = False,
    integrity: bool = True,
    risk: bool = True,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "market": "total",
        "identity": f"total:{candidate_id}:2.5",
        "side": "over",
        "line": 2.5,
        "probability": probability,
        "ev": probability - 0.5,
        "edge_pp": (probability - 0.5) * 100,
        "firm_count": 6,
        "formal_eligible": formal,
        "counterfactual_eligible": counterfactual,
        "shadow_selected": shadow,
        "gates": [
            {"category": "integrity", "passed": integrity},
            {"category": "risk", "passed": risk},
            {"category": "value", "passed": counterfactual or formal},
            {"category": "release", "passed": formal},
        ],
    }


class OfficialPrimaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = {
            "probabilities": {"home_win": 0.45, "draw": 0.25, "away_win": 0.30},
            "score_model_provenance": {"model_hash": "sha256:" + "a" * 64},
            "primary_market": None,
            "primary_pick": None,
        }

    def test_selection_ladder_and_replay(self) -> None:
        audit = {
            "audit_hash": "sha256:" + "b" * 64,
            "observation_id": "sha256:" + "c" * 64,
            "candidates": [
                candidate("forced", probability=0.70),
                candidate("shadow", probability=0.56, counterfactual=True, shadow=True),
            ],
        }
        selected = official_primary.select_official_primary(self.record, audit)
        self.assertEqual(selected["tier"], "counterfactual_shadow")
        self.assertEqual(selected["candidate_id"], "shadow")
        self.assertEqual(selected["recommended_stake_units"], 0.0)
        self.assertTrue(
            official_primary.validate_official_primary(selected, self.record, audit)
        )
        selected["side"] = "under"
        self.assertFalse(
            official_primary.validate_official_primary(selected, self.record, audit)
        )

    def test_forced_then_model_only_fallback(self) -> None:
        audit = {
            "audit_hash": "sha256:" + "b" * 64,
            "observation_id": "sha256:" + "c" * 64,
            "candidates": [candidate("forced", probability=0.52)],
        }
        forced = official_primary.select_official_primary(self.record, audit)
        self.assertEqual(forced["tier"], "forced_executable")
        fallback = official_primary.select_official_primary(
            self.record,
            {
                "audit_hash": "sha256:" + "b" * 64,
                "observation_id": "sha256:" + "c" * 64,
                "candidates": [candidate("blocked", probability=0.80, integrity=False)],
            },
        )
        self.assertEqual(fallback["tier"], "model_only_1x2")
        self.assertEqual(fallback["selection"], "H")
        self.assertFalse(fallback["counts_toward_betting_record"])

    def test_settlement_is_non_monetary(self) -> None:
        primary = official_primary.select_official_primary(
            self.record,
            {"candidates": [], "audit_hash": None, "observation_id": None},
        )
        settlement = official_primary.settle_official_primary(
            primary, {}, full_time_code="H"
        )
        self.assertTrue(settlement["hit"])
        self.assertEqual(settlement["monetary_scope"], "none")
        self.assertFalse(settlement["counts_toward_betting_record"])

    def test_new_archive_text_shows_evaluation_and_keeps_formal_empty(self) -> None:
        version = {
            **self.record,
            "analysis_stage": "initial",
            "home_team": "Alpha",
            "away_team": "Bravo",
            "candidate_audits": [],
        }
        version["official_primary"] = official_primary.select_official_primary(
            version, {"candidates": []}
        )
        lines = plain_text_formatter.publication_text_lines(version)
        self.assertIn("评测主推：Alpha胜", lines[0])
        self.assertIn("每场必选", lines[1])
        self.assertIn("正式主推：无", lines)


if __name__ == "__main__":
    unittest.main()

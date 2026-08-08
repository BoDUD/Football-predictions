from __future__ import annotations

import unittest
from copy import deepcopy

from scripts import forward_policy, official_primary, review_training_export


class ReviewTrainingExportTests(unittest.TestCase):
    def _sample(self) -> dict:
        evaluation_context = {
            "probabilities": {"home_win": 0.6, "draw": 0.2, "away_win": 0.2},
            "score_model_provenance": {"model_hash": "sha256:" + "d" * 64},
            "primary_market": None,
            "primary_pick": None,
        }
        evaluation_primary = official_primary.select_official_primary(
            evaluation_context, {"candidates": []}
        )
        sample = {
            "schema_version": "review-training-sample/1.0.0",
            "fixture": {
                "match_id": "1",
                "league_key": "test",
                "kickoff": "2026-08-09T01:00:00+00:00",
                "home_team": "A",
                "away_team": "B",
            },
            "archive_version_hash": "sha256:" + "a" * 64,
            "cohort_id": "cohort-a",
            "reviewed_at": "2026-08-09T03:00:00+00:00",
            "result_evidence": {
                "verified_finished": True,
                "source": "https://example.test/result",
                "collected_at": "2026-08-09T03:00:00+00:00",
            },
            "actual": {
                "full_time_score": "2-1",
                "half_time_score": "1-0",
                "home_corners": 5,
                "away_corners": 4,
                "full_time_1x2": "H",
            },
            "official_primary": evaluation_primary,
            "official_primary_settlement": {
                "schema_version": "official-primary-settlement/1.0.0",
                "result": "win",
            },
            "betting_primary": {"market": None, "pick": None, "result": None},
            "candidate_evaluation": None,
            "training_scope": "next_closed_cohort_only",
            "mutates_active_models": False,
        }
        sample["official_primary_settlement"]["settlement_hash"] = (
            forward_policy._hash_json(sample["official_primary_settlement"])
        )
        sample["sample_hash"] = forward_policy._hash_json(sample)
        return sample

    def test_sample_validation_rejects_post_result_mutation(self) -> None:
        sample = self._sample()
        record = {
            "match_id": "1",
            "final_score": "2-1",
            "reviewed_at": sample["reviewed_at"],
            "result_verification": deepcopy(sample["result_evidence"]),
            "official_primary": deepcopy(sample["official_primary"]),
            "official_primary_settlement": deepcopy(
                sample["official_primary_settlement"]
            ),
            "probabilities": {"home_win": 0.6, "draw": 0.2, "away_win": 0.2},
            "score_model_provenance": {"model_hash": "sha256:" + "d" * 64},
            "primary_market": None,
            "primary_pick": None,
            "candidate_audits": [],
        }
        self.assertEqual(review_training_export.validate_sample(sample, record), sample)
        record["final_score"] = "1-1"
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            review_training_export.validate_sample(sample, record)


if __name__ == "__main__":
    unittest.main()

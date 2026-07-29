from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "exact_score_ranker.py"
SPEC = importlib.util.spec_from_file_location("soccer_exact_score_ranker", SCRIPT)
assert SPEC and SPEC.loader
ranker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ranker)


class ExactScoreRankerTests(unittest.TestCase):
    def test_zero_zero_enters_top_two_when_probability_requires_it(self):
        result = ranker.rank_exact_scores(0.65, 0.55)

        self.assertEqual(result["top_two"][0]["score"], "0-0")
        self.assertEqual(result["zero_zero_audit"]["rank"], 1)
        self.assertTrue(result["zero_zero_audit"]["included_in_top2"])
        self.assertEqual(result["zero_zero_audit"]["status"], "top_two")

    def test_zero_zero_audit_is_retained_when_outside_top_two(self):
        result = ranker.rank_exact_scores(
            1.35,
            1.05,
            odds={"0-0": 12.5},
        )

        self.assertEqual(
            [pick["score"] for pick in result["top_two"]],
            ["1-1", "1-0"],
        )
        audit = result["zero_zero_audit"]
        self.assertEqual(audit["score"], "0-0")
        self.assertGreater(audit["rank"], 2)
        self.assertFalse(audit["included_in_top2"])
        self.assertEqual(audit["status"], "analyzed_not_top_two")
        self.assertAlmostEqual(
            audit["ev"],
            audit["probability"] * 12.5 - 1,
        )

    def test_preferred_result_breaks_equal_probability_tie(self):
        result = ranker.rank_exact_scores(
            1.0,
            1.0,
            preferred_result="home",
        )

        scores = [pick["score"] for pick in result["top_two"]]
        self.assertEqual(scores[0], "1-0")
        self.assertEqual(scores[1], "1-1")

    def test_distribution_is_normalized(self):
        result = ranker.rank_exact_scores(1.8, 1.2, max_goals=10)

        self.assertGreater(result["captured_mass"], 0.999)
        self.assertLessEqual(result["captured_mass"], 1.0 + 1e-12)

    def test_total_primary_uses_conditioned_display_scores_without_rewriting_distribution(self):
        result = ranker.rank_exact_scores(
            1.82,
            1.05,
            display_total_side="over",
            display_total_line=2.5,
        )

        self.assertEqual(
            [pick["score"] for pick in result["top_two"]],
            ["1-1", "1-0"],
        )
        self.assertEqual(
            [pick["score"] for pick in result["display_top_two"]],
            ["2-1", "3-1"],
        )
        self.assertEqual(
            [pick["unconditional_rank"] for pick in result["display_top_two"]],
            [3, 5],
        )
        self.assertAlmostEqual(
            result["display_selection"]["event_probability"],
            0.5470618833421336,
        )
        first = result["display_top_two"][0]
        self.assertAlmostEqual(first["probability"], 0.0986003435271168)
        self.assertAlmostEqual(
            first["conditional_probability"],
            first["probability"] / result["display_selection"]["event_probability"],
        )
        self.assertEqual(result["zero_zero_audit"]["rank"], 9)
        self.assertFalse(result["zero_zero_audit"]["included_in_display_top2"])

    def test_integer_total_display_excludes_push_scores(self):
        over = ranker.rank_exact_scores(
            1.8,
            1.2,
            display_total_side="over",
            display_total_line=3.0,
        )
        under = ranker.rank_exact_scores(
            1.8,
            1.2,
            display_total_side="under",
            display_total_line=3.0,
        )

        self.assertTrue(
            all(
                sum(int(part) for part in pick["score"].split("-")) > 3
                for pick in over["display_top_two"]
            )
        )
        self.assertTrue(
            all(
                sum(int(part) for part in pick["score"].split("-")) < 3
                for pick in under["display_top_two"]
            )
        )

    def test_total_display_arguments_must_be_paired(self):
        with self.assertRaisesRegex(ValueError, "must be supplied together"):
            ranker.rank_exact_scores(1.4, 1.1, display_total_side="over")


if __name__ == "__main__":
    unittest.main()

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

    def test_zero_zero_remains_visible_when_outside_top_two(self):
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


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LineupSourceContractTests(unittest.TestCase):
    def test_skill_routes_missing_titan_lineups_to_fallback_reference(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("references/lineup-sources.md", skill)
        self.assertIn("official-site → ESPN → Sofascore", skill)
        self.assertIn(
            "Never treat a predicted, probable, expected, or stale lineup as confirmed",
            skill,
        )

    def test_reference_requires_fixture_binding_and_two_complete_xis(self):
        reference = (ROOT / "references" / "lineup-sources.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("exactly 11 starters are present for each team", reference)
        self.assertIn("allowing at most 15 minutes", reference)
        self.assertIn("ESPN and Sofascore", reference)
        self.assertIn("Do not automate FotMob, Flashscore, or Soccerway", reference)
        self.assertIn("stable event/game ID", reference)
        self.assertIn("predicted", reference)
        self.assertIn("lineup_confirmed=false", reference)

    def test_collection_and_scheduler_do_not_stop_at_empty_titan_section(self):
        collection = (ROOT / "references" / "data-collection.md").read_text(
            encoding="utf-8"
        )
        scheduling = (ROOT / "references" / "lineup-scheduling.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("do not stop after an empty Titan lineup section", collection)
        self.assertIn(
            "An empty Titan lineup section is not a retryable execution failure",
            scheduling,
        )
        self.assertIn("Never call `release` solely", scheduling)

    def test_prediction_framework_keeps_unconfirmed_lineups_out_of_gates(self):
        framework = (ROOT / "references" / "prediction-framework.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("official-site → ESPN → Sofascore fallback", framework)
        self.assertIn("lineup_confirmed=false", framework)
        self.assertIn("predicted/probable XI", framework)


if __name__ == "__main__":
    unittest.main()

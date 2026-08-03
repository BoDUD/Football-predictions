from __future__ import annotations

import copy
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
import unittest

from scripts import corner_model, corner_model_manager, corner_ranker
from _corner_source_fixture import build_source_bound_dataset


TEAMS = ("A", "B", "C", "D")


def write_history(path: Path, *, days: int = 16) -> None:
    schedules = (
        (("A", "B"), ("C", "D")),
        (("A", "C"), ("D", "B")),
        (("A", "D"), ("B", "C")),
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(corner_model.TRAINING_COLUMNS)
        match_id = 1
        for day in range(days):
            match_date = date(2023, 1, 1) + timedelta(days=day)
            for home, away in schedules[day % len(schedules)]:
                kickoff = datetime.combine(
                    match_date, datetime.min.time(), tzinfo=timezone.utc
                )
                fixture_hash = "sha256:" + hashlib.sha256(
                    f"fixture:{match_id}".encode()
                ).hexdigest()
                response_hash = "sha256:" + hashlib.sha256(
                    f"response:{match_id}".encode()
                ).hexdigest()
                writer.writerow(
                    [
                        match_date.isoformat(),
                        kickoff.isoformat().replace("+00:00", "Z"),
                        int(kickoff.timestamp()),
                        "korea_k_league_1",
                        home,
                        away,
                        3 + (2 * TEAMS.index(home) + day) % 7,
                        2 + (TEAMS.index(away) + 2 * day) % 6,
                        match_id,
                        "2023",
                        "regular_season",
                        "regular",
                        fixture_hash,
                        f"https://example.test/{match_id}",
                        "2024-01-01T00:00:00Z",
                        response_hash,
                    ]
                )
                match_id += 1


class CornerRankerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        source_dir = cls.base / "source"
        source_dir.mkdir()
        cls.history, _manifest = build_source_bound_dataset(
            source_dir,
            target_league_key="korea_k_league_1",
            days=60,
            strong_signal=True,
        )
        cls.model_dir = cls.base / "models"
        corner_model_manager.train_registered_model(
            cls.history,
            cls.model_dir,
            league_key="korea_k_league_1",
            generated_at="2024-01-01T00:00:00Z",
            half_life_days=120.0,
            iterations=20,
            learning_rate=0.025,
            regularization=0.03,
            min_train_matches=8,
            test_block_size=5,
            hard_max_corners=70,
        )
        cls.prediction = corner_model_manager.predict_registered_model(
            cls.model_dir,
            "korea_k_league_1",
            "A",
            "B",
            kickoff="2024-02-01T12:00:00Z",
            generated_at="2024-01-15T00:00:00Z",
            total_markets=(
                ("over", 8.5),
                ("under", 8.5),
                ("over", 8.25),
                ("under", 8.25),
            ),
            corner_handicaps=(("home", -0.5), ("away", 0.5)),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def evidence(self, **overrides) -> dict:
        value = {
            "available": True,
            "independent_from_goal_model": True,
            "source": "audited corner profile feed",
            "collected_at": "2024-01-15T10:00:00Z",
            "summary": "home/away corner rates and width/crossing profile agree",
            "components": [
                "home_away_corners_for_against",
                "width_crossing",
            ],
        }
        value.update(overrides)
        return value

    def total_market(self, **overrides) -> dict:
        value = {
            "market": "corner_total",
            "line": 8.5,
            "odds_format": "decimal",
            "complete_market_odds": {"over": 2.10, "under": 2.10},
            "firm_count": 3,
            "market_complete": True,
            "market_source": "Titan007 consensus",
            "market_collected_at": "2024-01-15T11:00:00Z",
            "price_basis": "consensus",
            "market_signal": "neutral",
        }
        value.update(overrides)
        return value

    def handicap_market(self, **overrides) -> dict:
        value = {
            "market": "corner_handicap",
            "line": -0.5,
            "odds_format": "decimal",
            "complete_market_odds": {"home": 2.10, "away": 2.10},
            "firm_count": 3,
            "market_complete": True,
            "market_source": "Titan007 consensus",
            "market_collected_at": "2024-01-15T11:00:00Z",
            "price_basis": "median",
            "market_signal": "aligned",
        }
        value.update(overrides)
        return value

    def rank(self, markets=None, **overrides) -> dict:
        values = {
            "model_dir": self.model_dir,
            "generated_at": "2024-01-15T12:00:00Z",
            "data_quality": "high",
            "corner_profile_evidence": self.evidence(),
        }
        values.update(overrides)
        return corner_ranker.rank_corner_markets(
            self.prediction,
            markets or [self.total_market(), self.handicap_market()],
            **values,
        )

    def test_five_state_ev_and_no_vig_edge_are_recalculated(self):
        ranking = self.rank()
        self.assertEqual(
            ranking["ranking_hash"], corner_ranker.calculate_ranking_hash(ranking)
        )
        self.assertEqual(len(ranking["candidates"]), 4)
        self.assertGreater(
            ranking["market_policy"]["diagnostically_qualified_count"], 0
        )
        for candidate in ranking["candidates"]:
            probabilities = candidate["settlement_probabilities"]
            expected_ev = (
                probabilities["full_win"] * 1.10
                + probabilities["half_win"] * 0.55
                - probabilities["half_loss"] * 0.5
                - probabilities["loss"]
            )
            self.assertAlmostEqual(candidate["ev"], expected_ev, places=12)
            self.assertAlmostEqual(candidate["market_probability"], 0.5, places=12)
            win_mass = probabilities["full_win"] + 0.5 * probabilities["half_win"]
            loss_mass = probabilities["loss"] + 0.5 * probabilities["half_loss"]
            expected_probability = win_mass / (win_mass + loss_mass)
            self.assertAlmostEqual(candidate["probability"], expected_probability, places=12)
            self.assertEqual(
                candidate["probability_basis"],
                "half_stake_weighted_directional_mass_excluding_push",
            )
            self.assertAlmostEqual(candidate["equivalent_win_mass"], win_mass, places=12)
            self.assertAlmostEqual(candidate["equivalent_loss_mass"], loss_mass, places=12)
            self.assertAlmostEqual(
                candidate["edge_pp"], (expected_probability - 0.5) * 100, places=12
            )
            self.assertAlmostEqual(
                sum(probabilities.values()), 1.0, places=12
            )
        corner_ranker.validate_ranking(
            ranking, self.prediction, model_dir=self.model_dir
        )

    def test_registered_false_flags_force_every_candidate_to_observation(self):
        ranking = self.rank()
        self.assertFalse(
            ranking["upstream_policy"]["formal_corner_total_eligible"]
        )
        self.assertFalse(
            ranking["upstream_policy"]["formal_corner_handicap_eligible"]
        )
        self.assertEqual(ranking["formal_count"], 0)
        self.assertIsNone(ranking["primary"])
        self.assertEqual(ranking["market_policy"]["status"], "observation_only")
        self.assertTrue(
            ranking["market_policy"]["diagnostic_qualification_cannot_override_upstream_policy"]
        )
        self.assertTrue(
            any(
                candidate["diagnostic_qualification_status"] == "qualified"
                for candidate in ranking["candidates"]
            )
        )
        for candidate in ranking["candidates"]:
            self.assertEqual(candidate["status"], "observation")
            self.assertEqual(candidate["role"], "observation")
            self.assertFalse(candidate["formal_eligible"])
            self.assertFalse(candidate["upstream_formal_eligible"])
            self.assertTrue(candidate["policy_failed_thresholds"])

    def test_hong_kong_odds_use_net_price_in_split_settlement_ev(self):
        market = self.total_market(
            odds_format="hong_kong",
            complete_market_odds={"over": 1.10, "under": 1.10},
        )
        ranking = self.rank([market])
        for candidate in ranking["candidates"]:
            self.assertAlmostEqual(candidate["decimal_odds"], 2.10, places=12)
            self.assertAlmostEqual(candidate["net_win_odds"], 1.10, places=12)
            probabilities = candidate["settlement_probabilities"]
            expected = (
                probabilities["full_win"] * 1.10
                + probabilities["half_win"] * 0.55
                - probabilities["half_loss"] * 0.5
                - probabilities["loss"]
            )
            self.assertAlmostEqual(candidate["ev"], expected, places=12)

    def test_quarter_line_half_win_and_half_loss_enter_ev_at_half_stake(self):
        ranking = self.rank([self.total_market(line=8.25)])
        self.assertTrue(
            any(
                item["settlement_probabilities"]["half_win"] > 0.0
                or item["settlement_probabilities"]["half_loss"] > 0.0
                for item in ranking["candidates"]
            )
        )
        for candidate in ranking["candidates"]:
            probabilities = candidate["settlement_probabilities"]
            expected = (
                probabilities["full_win"] * 1.10
                + probabilities["half_win"] * 0.55
                - probabilities["half_loss"] * 0.5
                - probabilities["loss"]
            )
            self.assertAlmostEqual(candidate["ev"], expected, places=12)

    def test_quarter_line_edge_uses_half_stake_mass_and_opposites_sign_flip(self):
        ranking = self.rank([self.total_market(line=8.25)])
        by_side = {item["side"]: item for item in ranking["candidates"]}
        over = by_side["over"]
        under = by_side["under"]
        self.assertAlmostEqual(over["probability"] + under["probability"], 1.0, places=12)
        self.assertAlmostEqual(over["edge_pp"], -under["edge_pp"], places=12)
        self.assertAlmostEqual(
            over["equivalent_win_mass"], under["equivalent_loss_mass"], places=12
        )
        self.assertAlmostEqual(
            over["equivalent_loss_mass"], under["equivalent_win_mass"], places=12
        )

        synthetic = {
            "full_win": 0.10,
            "half_win": 0.40,
            "push": 0.10,
            "half_loss": 0.10,
            "loss": 0.30,
        }
        probability, win_mass, loss_mass = (
            corner_ranker._settlement_equivalent_probability(synthetic)
        )
        self.assertAlmostEqual(win_mass, 0.30, places=12)
        self.assertAlmostEqual(loss_mass, 0.35, places=12)
        self.assertAlmostEqual(probability, 0.30 / 0.65, places=12)
        self.assertNotAlmostEqual(
            probability, synthetic["full_win"] + synthetic["half_win"], places=12
        )

    def test_incomplete_market_is_observation_without_no_vig_edge(self):
        market = self.total_market(
            market_complete=False,
            complete_market_odds={"over": 1.95},
        )
        ranking = self.rank([market])
        self.assertEqual(ranking["formal_count"], 0)
        for candidate in ranking["candidates"]:
            self.assertIsNone(candidate["market_probability"])
            self.assertIsNone(candidate["edge_pp"])
            self.assertIn(
                "complete current two-way corner market unavailable",
                candidate["diagnostic_failed_thresholds"],
            )
        under = next(item for item in ranking["candidates"] if item["side"] == "under")
        self.assertIsNone(under["odds"])
        self.assertIsNone(under["ev"])

    def test_firm_data_quality_signal_and_independent_evidence_gates(self):
        ranking = self.rank(
            [
                self.total_market(
                    firm_count=2,
                    market_signal="unknown",
                )
            ],
            data_quality="low",
            corner_profile_evidence={"available": False},
        )
        for candidate in ranking["candidates"]:
            failures = " | ".join(candidate["diagnostic_failed_thresholds"])
            self.assertIn("firm count 2 < 3", failures)
            self.assertIn("data quality low", failures)
            self.assertIn("corner-profile evidence unavailable", failures)
            self.assertIn("market signal is unknown", failures)
            self.assertEqual(candidate["diagnostic_qualification_status"], "unqualified")

    def test_adverse_signal_stricter_gate_and_corroboration(self):
        uncorroborated = self.rank(
            [self.total_market(market_signal="against", firm_count=3)]
        )
        for candidate in uncorroborated["candidates"]:
            failures = " | ".join(candidate["diagnostic_failed_thresholds"])
            self.assertIn("adverse-signal firm count 3 < 5", failures)
            self.assertIn("lacks independent", failures)

        model_items = {
            item["side"]: item for item in self.prediction["corner_totals"]
        }
        strongest_side = max(
            ("over", "under"),
            key=lambda side: model_items[side]["probabilities"]["full_win"]
            + model_items[side]["probabilities"]["half_win"],
        )
        other_side = "under" if strongest_side == "over" else "over"
        prices = {strongest_side: 3.0, other_side: 1.5}
        corroboration = {
            "available": True,
            "kind": "confirmed_lineup",
            "source": "confirmed XI and roles",
            "collected_at": "2024-01-15T11:30:00Z",
            "summary": "wide roles and corner takers confirmed",
        }
        corroborated = self.rank(
            [
                self.total_market(
                    market_signal="against",
                    firm_count=5,
                    complete_market_odds=prices,
                    adverse_signal_corroboration=corroboration,
                )
            ]
        )
        strongest = next(
            item for item in corroborated["candidates"] if item["side"] == strongest_side
        )
        self.assertEqual(strongest["diagnostic_qualification_status"], "qualified")
        self.assertEqual(strongest["status"], "observation")
        self.assertGreaterEqual(strongest["ev"], 0.08)
        self.assertGreaterEqual(strongest["edge_pp"], 4.0)

    def test_handicap_snapshot_line_maps_to_opposite_away_line(self):
        ranking = self.rank([self.handicap_market()])
        by_side = {item["side"]: item for item in ranking["candidates"]}
        self.assertEqual(by_side["home"]["line"], -0.5)
        self.assertEqual(by_side["away"]["line"], 0.5)
        self.assertEqual(by_side["home"]["snapshot_line"], -0.5)
        self.assertEqual(by_side["away"]["snapshot_line"], -0.5)

    def test_market_and_evidence_must_be_strictly_pre_kickoff(self):
        with self.assertRaisesRegex(corner_ranker.CornerRankerError, "before kickoff"):
            self.rank(
                [
                    self.total_market(
                        market_collected_at="2024-02-01T12:00:00Z"
                    )
                ]
            )
        with self.assertRaisesRegex(corner_ranker.CornerRankerError, "before kickoff"):
            self.rank(generated_at="2024-02-01T12:00:00Z")
        with self.assertRaisesRegex(corner_ranker.CornerRankerError, "before kickoff"):
            self.rank(
                corner_profile_evidence=self.evidence(
                    collected_at="2024-02-01T12:00:00Z"
                )
            )

    def test_ranking_tampering_and_upstream_flag_forgery_are_rejected(self):
        ranking = self.rank()
        tampered = copy.deepcopy(ranking)
        tampered["candidates"][0]["ev"] += 0.01
        with self.assertRaisesRegex(corner_ranker.CornerRankerError, "ranking_hash"):
            corner_ranker.validate_ranking(
                tampered, self.prediction, model_dir=self.model_dir
            )
        tampered["ranking_hash"] = corner_ranker.calculate_ranking_hash(tampered)
        with self.assertRaisesRegex(corner_ranker.CornerRankerError, "does not reproduce"):
            corner_ranker.validate_ranking(
                tampered, self.prediction, model_dir=self.model_dir
            )

        forged_prediction = copy.deepcopy(self.prediction)
        forged_prediction["formal_corner_total_eligible"] = True
        forged_prediction["prediction_hash"] = corner_model.calculate_prediction_hash(
            forged_prediction
        )
        with self.assertRaisesRegex(
            corner_ranker.CornerRankerError, "registered prediction is invalid"
        ):
            corner_ranker.rank_corner_markets(
                forged_prediction,
                [self.total_market()],
                model_dir=self.model_dir,
                generated_at="2024-01-15T12:00:00Z",
                data_quality="high",
                corner_profile_evidence=self.evidence(),
            )

    def test_prediction_must_contain_both_requested_five_state_directions(self):
        incomplete_prediction = corner_model_manager.predict_registered_model(
            self.model_dir,
            "korea_k_league_1",
            "A",
            "B",
            kickoff="2024-02-01T12:00:00Z",
            generated_at="2024-01-15T00:00:00Z",
            total_markets=(("over", 8.5),),
        )
        with self.assertRaisesRegex(
            corner_ranker.CornerRankerError, "exactly one five-state"
        ):
            corner_ranker.rank_corner_markets(
                incomplete_prediction,
                [self.total_market()],
                model_dir=self.model_dir,
                generated_at="2024-01-15T12:00:00Z",
                data_quality="high",
                corner_profile_evidence=self.evidence(),
            )


if __name__ == "__main__":
    unittest.main()

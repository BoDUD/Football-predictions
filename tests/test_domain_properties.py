from __future__ import annotations

import math

import pytest

from soccer_predict.domain.probabilities import (
    matrix_1x2,
    matrix_settlement_distribution,
    validate_probability_matrix,
)
from soccer_predict.domain.settlement import settle_asian, settle_total

pytestmark = pytest.mark.property

INVERSE_RESULT = {
    "win": "loss",
    "half_win": "half_loss",
    "push": "push",
    "half_loss": "half_win",
    "loss": "win",
}


def test_asian_settlement_is_symmetric_when_teams_and_side_are_swapped() -> None:
    lines = [quarter / 4 for quarter in range(-8, 9)]
    for home_goals in range(7):
        for away_goals in range(7):
            for line in lines:
                home_result = settle_asian(
                    {"side": "home", "line": line}, home_goals, away_goals
                )
                swapped_result = settle_asian(
                    {"side": "away", "line": line}, away_goals, home_goals
                )
                assert home_result == swapped_result


def test_over_and_under_settlements_are_exact_complements() -> None:
    lines = [quarter / 4 for quarter in range(0, 21)]
    for home_goals in range(7):
        for away_goals in range(7):
            for line in lines:
                over = settle_total(
                    {"side": "over", "line": line}, home_goals, away_goals
                )
                under = settle_total(
                    {"side": "under", "line": line}, home_goals, away_goals
                )
                assert under == INVERSE_RESULT[str(over)]


@pytest.mark.parametrize(
    "raw_matrix",
    [
        [[1.0]],
        [[0.05, 0.10], [0.25, 0.60]],
        [[0.02, 0.03, 0.05], [0.10, 0.20, 0.10], [0.15, 0.15, 0.20]],
    ],
)
def test_score_matrix_aggregations_preserve_probability_mass(
    raw_matrix: list[list[float]],
) -> None:
    matrix = validate_probability_matrix(raw_matrix, "matrix")
    one_x_two = matrix_1x2(matrix)
    assert math.fsum(one_x_two.values()) == pytest.approx(1.0)

    for market, pick in (
        ("asian", {"side": "home", "line": -0.25}),
        ("total", {"side": "over", "line": 2.25}),
        ("half_time", {"market": "1x2", "side": "draw"}),
    ):
        distribution = matrix_settlement_distribution(matrix, market, pick)
        assert all(probability >= 0.0 for probability in distribution.values())
        assert math.fsum(distribution.values()) == pytest.approx(1.0)

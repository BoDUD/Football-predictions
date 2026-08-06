from __future__ import annotations

import math

import pytest

from soccer_predict.domain.probabilities import (
    effective_settlement_win_probability,
    matrix_1x2,
    matrix_settlement_distribution,
    validate_probability_matrix,
)
from soccer_predict.domain.settlement import (
    parse_goal_range_selection,
    settle_asian,
    settle_btts,
    settle_corner_handicap,
    settle_corner_total,
    settle_goal_range,
    settle_half_time,
    settle_htft,
    settle_total,
    split_line,
)


def test_quarter_line_settlement_rules_are_preserved() -> None:
    assert split_line(-0.25) == (-0.5, 0.0)
    assert split_line(2.25) == (2.0, 2.5)
    with pytest.raises(ValueError, match="quarter-goal"):
        split_line(0.1)

    assert settle_asian({"side": "home", "line": -0.25}, 1, 1) == "half_loss"
    assert settle_asian({"side": "away", "line": 1.25}, 2, 1) == "half_win"
    assert settle_total({"side": "over", "line": 2.25}, 1, 1) == "half_loss"
    assert settle_total({"side": "under", "line": 2.75}, 1, 1) == "win"


def test_discrete_and_corner_market_settlement_rules() -> None:
    assert parse_goal_range_selection(" 2 - 3 ") == {
        "selection": "2-3",
        "minimum_goals": 2,
        "maximum_goals": 3,
    }
    assert parse_goal_range_selection("7+")["maximum_goals"] is None
    with pytest.raises(ValueError, match="lower bound"):
        parse_goal_range_selection("4-2")

    assert settle_goal_range({"selection": "2-3"}, 1, 2) == "win"
    assert settle_goal_range({"selection": "2-3"}, 2, 2) == "loss"
    assert settle_btts({"side": "yes"}, 1, 1) == "win"
    assert settle_btts({"side": "no"}, 2, 0) == "win"
    assert settle_corner_total({"side": "over", "line": 8.5}, 6, 3) == "win"
    assert settle_corner_handicap({"side": "home", "line": -2.5}, 6, 3) == "win"


def test_half_time_and_htft_settlement_rules() -> None:
    assert settle_half_time({"market": "1x2", "side": "draw"}, 0, 0) == "win"
    assert (
        settle_half_time({"market": "asian", "side": "home", "line": 0}, 1, 0) == "win"
    )
    assert (
        settle_half_time({"market": "total", "side": "under", "line": 1.5}, 0, 1)
        == "win"
    )
    assert settle_half_time({"market": "unsupported"}, 0, 0) is None
    assert settle_htft([{"selection": "DH"}, {"selection": "DD"}], 0, 0, 1, 0) == [
        "win",
        "loss",
    ]


def test_probability_matrix_validation_and_aggregation() -> None:
    matrix = validate_probability_matrix(
        [[0.1, 0.2], [0.3, 0.4]], "full_time_score_matrix"
    )
    assert matrix_1x2(matrix) == {
        "home_win": pytest.approx(0.3),
        "draw": pytest.approx(0.5),
        "away_win": pytest.approx(0.2),
    }
    distribution = matrix_settlement_distribution(
        matrix, "asian", {"side": "home", "line": 0.0}
    )
    assert distribution == {
        "full_win": pytest.approx(0.3),
        "half_win": 0.0,
        "push": pytest.approx(0.5),
        "half_loss": 0.0,
        "loss": pytest.approx(0.2),
    }
    assert effective_settlement_win_probability(distribution, "asian") == pytest.approx(
        0.6
    )


@pytest.mark.parametrize(
    "value,error",
    [
        ([], "non-empty"),
        ([[0.5], [0.25, 0.25]], "equal length"),
        ([[0.5, -0.5], [0.5, 0.5]], "non-negative"),
        ([[0.2, 0.2], [0.2, 0.2]], "sum to 1"),
    ],
)
def test_probability_matrix_validation_rejects_invalid_artifacts(
    value: object, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        validate_probability_matrix(value, "matrix")


def test_effective_probability_rejects_bad_distribution_and_handles_push() -> None:
    all_push = {
        "full_win": 0.0,
        "half_win": 0.0,
        "push": 1.0,
        "half_loss": 0.0,
        "loss": 0.0,
    }
    assert effective_settlement_win_probability(all_push, "market") is None

    invalid = dict(all_push)
    invalid["loss"] = math.nan
    with pytest.raises(ValueError, match="finite"):
        effective_settlement_win_probability(invalid, "market")

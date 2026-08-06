"""Pure probability validation and score-matrix aggregation functions."""

from __future__ import annotations

import math
from typing import Any, Mapping

from soccer_predict.domain.settlement import (
    settle_asian,
    settle_half_time,
    settle_total,
)

PROBABILITY_AUDIT_TOLERANCE = 1e-4
SETTLEMENT_STATES = ("full_win", "half_win", "push", "half_loss", "loss")


def validate_probability_matrix(value: Any, label: str) -> list[list[float]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty two-dimensional array")
    matrix: list[list[float]] = []
    width: int | None = None
    for row_index, row in enumerate(value):
        if not isinstance(row, list) or not row:
            raise ValueError(f"{label} row {row_index} must be a non-empty array")
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise ValueError(f"{label} rows must have equal length")
        converted: list[float] = []
        for column_index, item in enumerate(row):
            if isinstance(item, bool):
                raise ValueError(
                    f"{label}[{row_index}][{column_index}] must be a finite non-negative number"
                )
            try:
                number = float(item)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{label}[{row_index}][{column_index}] must be numeric"
                ) from exc
            if not math.isfinite(number) or number < 0.0:
                raise ValueError(
                    f"{label}[{row_index}][{column_index}] must be finite and non-negative"
                )
            converted.append(number)
        matrix.append(converted)
    total = math.fsum(math.fsum(row) for row in matrix)
    if abs(total - 1.0) > PROBABILITY_AUDIT_TOLERANCE:
        raise ValueError(f"{label} probabilities must sum to 1")
    return matrix


def matrix_1x2(matrix: list[list[float]]) -> dict[str, float]:
    home = draw = away = 0.0
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            if home_goals > away_goals:
                home += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away += probability
    return {"home_win": home, "draw": draw, "away_win": away}


def matrix_settlement_distribution(
    matrix: list[list[float]], market: str, pick: Mapping[str, Any]
) -> dict[str, float]:
    values = {state: 0.0 for state in SETTLEMENT_STATES}
    result_to_state = {
        "win": "full_win",
        "half_win": "half_win",
        "push": "push",
        "half_loss": "half_loss",
        "loss": "loss",
    }
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            if market == "asian":
                result = settle_asian(pick, home_goals, away_goals)
            elif market == "total":
                result = settle_total(pick, home_goals, away_goals)
            elif market == "half_time":
                result = settle_half_time(pick, home_goals, away_goals)
            else:
                raise ValueError(f"Cannot derive settlement distribution for {market}")
            values[result_to_state[str(result)]] += probability
    return values


def effective_settlement_win_probability(
    distribution: Mapping[str, Any], label: str
) -> float | None:
    if not isinstance(distribution, Mapping) or set(distribution) != set(
        SETTLEMENT_STATES
    ):
        raise ValueError(f"{label} must contain exactly the five settlement states")
    values: dict[str, float] = {}
    for state in SETTLEMENT_STATES:
        raw = distribution.get(state)
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
            or not 0.0 <= float(raw) <= 1.0
        ):
            raise ValueError(f"{label}.{state} must be finite and between 0 and 1")
        values[state] = float(raw)
    if abs(math.fsum(values.values()) - 1.0) > PROBABILITY_AUDIT_TOLERANCE:
        raise ValueError(f"{label} must sum to 1")
    win_mass = values["full_win"] + values["half_win"] / 2.0
    loss_mass = values["loss"] + values["half_loss"] / 2.0
    active_mass = win_mass + loss_mass
    if active_mass <= PROBABILITY_AUDIT_TOLERANCE:
        return None
    return win_mass / active_mass

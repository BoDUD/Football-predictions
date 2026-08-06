"""Pure regulation-time settlement functions for supported football markets."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Sequence


def split_line(line: float) -> tuple[float, float]:
    rounded = round(line * 4)
    if not math.isclose(line * 4, rounded, abs_tol=1e-7):
        raise ValueError(f"Line must use quarter-goal increments: {line}")
    if abs(rounded) % 2 == 1:
        return line - 0.25, line + 0.25
    return line, line


def label_result(value: float) -> str:
    if math.isclose(value, 1.0):
        return "win"
    if math.isclose(value, 0.5):
        return "half_win"
    if math.isclose(value, 0.0):
        return "push"
    if math.isclose(value, -0.5):
        return "half_loss"
    return "loss"


def settle_components(values: tuple[float, float]) -> str:
    score = (
        sum(
            1.0 if value > 0 else 0.0 if math.isclose(value, 0.0) else -1.0
            for value in values
        )
        / 2
    )
    return label_result(score)


def settle_asian(pick: Mapping[str, Any] | None, home: int, away: int) -> str | None:
    if not pick:
        return None
    side = pick["side"]
    margin = home - away if side == "home" else away - home
    first, second = split_line(float(pick["line"]))
    return settle_components((margin + first, margin + second))


def settle_total(pick: Mapping[str, Any] | None, home: int, away: int) -> str | None:
    if not pick:
        return None
    total = home + away
    first, second = split_line(float(pick["line"]))
    if pick["side"] == "over":
        return settle_components((total - first, total - second))
    return settle_components((first - total, second - total))


def parse_goal_range_selection(value: str) -> dict[str, Any]:
    selection = re.sub(r"\s+", "", str(value or ""))
    inclusive = re.fullmatch(r"(\d+)-(\d+)", selection)
    if inclusive:
        minimum, maximum = (int(part) for part in inclusive.groups())
        if minimum > maximum:
            raise ValueError("Goal-range lower bound cannot exceed its upper bound")
        return {
            "selection": f"{minimum}-{maximum}",
            "minimum_goals": minimum,
            "maximum_goals": maximum,
        }
    or_more = re.fullmatch(r"(\d+)\+", selection)
    if or_more:
        minimum = int(or_more.group(1))
        return {
            "selection": f"{minimum}+",
            "minimum_goals": minimum,
            "maximum_goals": None,
        }
    raise ValueError(
        "Goal range must be inclusive MIN-MAX or N+, for example 2-3 or 7+"
    )


def settle_goal_range(
    pick: Mapping[str, Any] | None, home: int, away: int
) -> str | None:
    if not pick:
        return None
    total = home + away
    minimum = pick.get("minimum_goals")
    maximum = pick.get("maximum_goals")
    if minimum is None:
        parsed = parse_goal_range_selection(str(pick.get("selection", "")))
        minimum = parsed["minimum_goals"]
        maximum = parsed["maximum_goals"]
    hit = total >= int(minimum) and (maximum is None or total <= int(maximum))
    return "win" if hit else "loss"


def settle_btts(pick: Mapping[str, Any] | None, home: int, away: int) -> str | None:
    if not pick:
        return None
    actual = home > 0 and away > 0
    expected = str(pick.get("side", "")).lower() == "yes"
    return "win" if actual == expected else "loss"


def settle_corner_total(
    pick: Mapping[str, Any] | None, home_corners: int, away_corners: int
) -> str | None:
    return settle_total(pick, home_corners, away_corners)


def settle_corner_handicap(
    pick: Mapping[str, Any] | None, home_corners: int, away_corners: int
) -> str | None:
    return settle_asian(pick, home_corners, away_corners)


def result_code(home: int, away: int) -> str:
    if home > away:
        return "H"
    if home < away:
        return "A"
    return "D"


def settle_half_time(
    pick: Mapping[str, Any] | None, home: int, away: int
) -> str | None:
    if not pick:
        return None
    market = pick.get("market")
    if market == "1x2":
        expected = {"home": "H", "draw": "D", "away": "A"}.get(
            str(pick.get("side") or "")
        )
        return "win" if expected == result_code(home, away) else "loss"
    if market == "asian":
        return settle_asian(pick, home, away)
    if market == "total":
        return settle_total(pick, home, away)
    return None


def settle_htft(
    picks: Sequence[Mapping[str, Any]] | None,
    half_home: int,
    half_away: int,
    home: int,
    away: int,
) -> list[str]:
    actual = result_code(half_home, half_away) + result_code(home, away)
    return [
        "win" if str(pick.get("selection", "")).upper() == actual else "loss"
        for pick in (picks or [])
    ]

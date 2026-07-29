"""Deterministically rank regulation-time exact scores and audit 0-0."""

from __future__ import annotations

import argparse
import json
import math
import re
from typing import Any


SCORE_RE = re.compile(r"^(\d+)-(\d+)$")


def poisson_probability(rate: float, goals: int) -> float:
    if rate < 0:
        raise ValueError("Expected goals must be non-negative")
    if goals < 0:
        raise ValueError("Goals must be non-negative")
    return math.exp(-rate) * (rate**goals) / math.factorial(goals)


def result_for_score(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"


def parse_odds(values: list[str] | None) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError("Exact-score odds must use SCORE=DECIMAL, for example 0-0=12.0")
        score, raw_odds = (part.strip() for part in value.split("=", 1))
        if not SCORE_RE.fullmatch(score):
            raise ValueError(f"Invalid exact score: {score}")
        odds = float(raw_odds)
        if odds <= 1:
            raise ValueError("Exact-score decimal odds must be greater than 1")
        parsed[score] = odds
    return parsed


def rank_exact_scores(
    home_xg: float,
    away_xg: float,
    *,
    max_goals: int = 10,
    preferred_result: str | None = None,
    display_total_side: str | None = None,
    display_total_line: float | None = None,
    odds: dict[str, float] | None = None,
) -> dict[str, Any]:
    if home_xg < 0 or away_xg < 0:
        raise ValueError("Expected goals must be non-negative")
    if max_goals < 3:
        raise ValueError("max_goals must be at least 3")
    if preferred_result not in {None, "home", "draw", "away"}:
        raise ValueError("preferred_result must be home, draw, away, or None")
    if (display_total_side is None) != (display_total_line is None):
        raise ValueError(
            "display_total_side and display_total_line must be supplied together"
        )
    if display_total_side not in {None, "over", "under"}:
        raise ValueError("display_total_side must be over, under, or None")
    if display_total_line is not None and display_total_line < 0:
        raise ValueError("display_total_line must be non-negative")

    raw: list[dict[str, Any]] = []
    for home_goals in range(max_goals + 1):
        home_probability = poisson_probability(home_xg, home_goals)
        for away_goals in range(max_goals + 1):
            probability = home_probability * poisson_probability(away_xg, away_goals)
            raw.append(
                {
                    "score": f"{home_goals}-{away_goals}",
                    "home_goals": home_goals,
                    "away_goals": away_goals,
                    "result": result_for_score(home_goals, away_goals),
                    "probability": probability,
                }
            )

    captured_mass = sum(item["probability"] for item in raw)
    if captured_mass <= 0:
        raise ValueError("Score grid has zero probability mass")

    expected_total = home_xg + away_xg
    for item in raw:
        item["probability"] /= captured_mass
        market_odds = (odds or {}).get(item["score"])
        item["odds"] = market_odds
        item["ev"] = (
            item["probability"] * market_odds - 1
            if market_odds is not None
            else None
        )

    def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        result_penalty = (
            0
            if preferred_result is None or item["result"] == preferred_result
            else 1
        )
        return (
            -item["probability"],
            result_penalty,
            abs((item["home_goals"] + item["away_goals"]) - expected_total),
            item["home_goals"],
            item["away_goals"],
        )

    ranked = sorted(raw, key=sort_key)
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index

    top_two = [
        {
            key: item[key]
            for key in ("rank", "score", "probability", "result", "odds", "ev")
        }
        for item in ranked[:2]
    ]

    display_selection: dict[str, Any]
    if display_total_side is None:
        display_pool = ranked
        event_probability = 1.0
        display_selection = {
            "basis": "unconditional_top_two",
            "market": None,
            "side": None,
            "line": None,
            "event": None,
            "event_probability": 1.0,
        }
    else:
        assert display_total_line is not None

        def supports_total_primary(item: dict[str, Any]) -> bool:
            total_goals = item["home_goals"] + item["away_goals"]
            if display_total_side == "over":
                return total_goals > display_total_line
            return total_goals < display_total_line

        display_pool = [item for item in ranked if supports_total_primary(item)]
        event_probability = sum(item["probability"] for item in display_pool)
        if len(display_pool) < 2 or event_probability <= 0:
            raise ValueError("Primary-conditioned score pool has fewer than two outcomes")
        display_selection = {
            "basis": "primary_total_net_profit",
            "market": "total",
            "side": display_total_side,
            "line": display_total_line,
            "event": "net_profit",
            "event_probability": event_probability,
        }

    display_top_two: list[dict[str, Any]] = []
    for display_rank, item in enumerate(display_pool[:2], start=1):
        display_top_two.append(
            {
                "display_rank": display_rank,
                "unconditional_rank": item["rank"],
                "score": item["score"],
                "probability": item["probability"],
                "conditional_probability": (
                    item["probability"] / event_probability
                    if event_probability > 0
                    else None
                ),
                "result": item["result"],
                "odds": item["odds"],
                "ev": item["ev"],
            }
        )

    zero_zero = next(item for item in ranked if item["score"] == "0-0")
    zero_zero_audit = {
        key: zero_zero[key]
        for key in ("rank", "score", "probability", "odds", "ev")
    }
    zero_zero_audit["included_in_top2"] = zero_zero["rank"] <= 2
    zero_zero_audit["included_in_display_top2"] = any(
        item["score"] == "0-0" for item in display_top_two
    )
    zero_zero_audit["status"] = (
        "top_two" if zero_zero["rank"] <= 2 else "analyzed_not_top_two"
    )
    zero_zero_audit["gap_to_second_pp"] = (
        zero_zero["probability"] - ranked[1]["probability"]
    ) * 100

    return {
        "model": "independent_poisson",
        "home_xg": home_xg,
        "away_xg": away_xg,
        "total_xg": expected_total,
        "max_goals": max_goals,
        "captured_mass": captured_mass,
        "top_two": top_two,
        "display_selection": display_selection,
        "display_top_two": display_top_two,
        "zero_zero_audit": zero_zero_audit,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home-xg", type=float, required=True)
    parser.add_argument("--away-xg", type=float, required=True)
    parser.add_argument("--max-goals", type=int, default=10)
    parser.add_argument("--preferred-result", choices=("home", "draw", "away"))
    parser.add_argument(
        "--display-total-side",
        choices=("over", "under"),
        help=(
            "Select the two user-facing score scenarios from the net-profit "
            "branch of a formal total-goals primary"
        ),
    )
    parser.add_argument(
        "--display-total-line",
        type=float,
        help="Formal total-goals primary line used with --display-total-side",
    )
    parser.add_argument(
        "--odds",
        action="append",
        help="Optional exact-score decimal odds as SCORE=DECIMAL; repeat as needed",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = rank_exact_scores(
        args.home_xg,
        args.away_xg,
        max_goals=args.max_goals,
        preferred_result=args.preferred_result,
        display_total_side=args.display_total_side,
        display_total_line=args.display_total_line,
        odds=parse_odds(args.odds),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

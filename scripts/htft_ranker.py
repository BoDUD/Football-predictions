"""Validate an HT/FT matrix and rank coherent model paths."""

from __future__ import annotations

import argparse
import json
import math
from typing import Any


HALF_RESULTS = ("H", "D", "A")
FULL_RESULTS = ("H", "D", "A")
OUTCOMES = tuple(f"{half}{full}" for half in HALF_RESULTS for full in FULL_RESULTS)


def parse_assignments(
    values: list[str] | None,
    *,
    allowed: tuple[str, ...],
    label: str,
) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"{label} values must use KEY=NUMBER")
        raw_key, raw_number = (part.strip() for part in value.split("=", 1))
        key = raw_key.upper()
        if key not in allowed:
            raise ValueError(f"Invalid {label} key: {raw_key}")
        number = float(raw_number)
        if not math.isfinite(number):
            raise ValueError(f"{label} values must be finite")
        if number < 0:
            raise ValueError(f"{label} values must be non-negative")
        parsed[key] = number
    return parsed


def _require_keys(
    values: dict[str, float],
    expected: tuple[str, ...],
    label: str,
) -> None:
    missing = [key for key in expected if key not in values]
    extra = [key for key in values if key not in expected]
    if missing or extra:
        raise ValueError(
            f"{label} must contain exactly {', '.join(expected)}; "
            f"missing={missing or 'none'}, extra={extra or 'none'}"
        )


def _validate_probability_total(
    values: dict[str, float],
    *,
    label: str,
    tolerance: float,
) -> None:
    if any(not math.isfinite(value) or value < 0 or value > 1 for value in values.values()):
        raise ValueError(f"{label} probabilities must be finite and within [0, 1]")
    total = sum(values.values())
    if abs(total - 1.0) > tolerance:
        raise ValueError(
            f"{label} probabilities sum to {total:.6f}, outside tolerance "
            f"{tolerance:.6f}"
        )


def _market_probabilities_from_odds(
    odds: dict[str, float],
) -> dict[str, float] | None:
    if any(key not in odds for key in OUTCOMES):
        return None
    if any(not math.isfinite(odds[key]) or odds[key] <= 1 for key in OUTCOMES):
        raise ValueError("HT/FT decimal odds must be finite and greater than 1")
    inverse = {key: 1.0 / odds[key] for key in OUTCOMES}
    overround = sum(inverse.values())
    return {key: inverse[key] / overround for key in OUTCOMES}


def rank_htft(
    matrix: dict[str, float],
    half_probabilities: dict[str, float],
    full_probabilities: dict[str, float],
    *,
    odds: dict[str, float] | None = None,
    market_probabilities: dict[str, float] | None = None,
    firm_count: int = 0,
    data_quality: str = "unknown",
    tolerance_pp: float = 0.5,
    edge_threshold_pp: float = 0.0,
    minimum_firms: int = 5,
) -> dict[str, Any]:
    _require_keys(matrix, OUTCOMES, "matrix")
    _require_keys(half_probabilities, HALF_RESULTS, "half probabilities")
    _require_keys(full_probabilities, FULL_RESULTS, "full probabilities")
    if data_quality not in {"high", "medium", "low", "unknown"}:
        raise ValueError("data_quality must be high, medium, low, or unknown")
    if firm_count < 0:
        raise ValueError("firm_count must be non-negative")
    if not math.isfinite(tolerance_pp) or tolerance_pp < 0 or tolerance_pp > 0.5:
        raise ValueError("tolerance_pp must be finite and within [0, 0.5]")
    if not math.isfinite(edge_threshold_pp) or edge_threshold_pp < 0:
        raise ValueError("edge_threshold_pp must be finite and non-negative")
    if minimum_firms < 0:
        raise ValueError("minimum_firms must be non-negative")

    tolerance = tolerance_pp / 100
    _validate_probability_total(matrix, label="matrix", tolerance=tolerance)
    _validate_probability_total(
        half_probabilities,
        label="half-time",
        tolerance=tolerance,
    )
    _validate_probability_total(
        full_probabilities,
        label="full-time",
        tolerance=tolerance,
    )

    row_marginals = {
        half: sum(matrix[f"{half}{full}"] for full in FULL_RESULTS)
        for half in HALF_RESULTS
    }
    column_marginals = {
        full: sum(matrix[f"{half}{full}"] for half in HALF_RESULTS)
        for full in FULL_RESULTS
    }
    row_differences_pp = {
        half: (row_marginals[half] - half_probabilities[half]) * 100
        for half in HALF_RESULTS
    }
    column_differences_pp = {
        full: (column_marginals[full] - full_probabilities[full]) * 100
        for full in FULL_RESULTS
    }
    max_difference_pp = max(
        abs(value)
        for value in (*row_differences_pp.values(), *column_differences_pp.values())
    )
    if max_difference_pp > tolerance_pp:
        raise ValueError(
            "HT/FT matrix marginals do not match the half/full model: "
            f"max difference {max_difference_pp:.3f}pp exceeds "
            f"{tolerance_pp:.3f}pp"
        )

    supplied_odds = odds or {}
    for selection, decimal_odds in supplied_odds.items():
        if selection not in OUTCOMES:
            raise ValueError(f"Invalid HT/FT odds key: {selection}")
        if not math.isfinite(decimal_odds) or decimal_odds <= 1:
            raise ValueError("HT/FT decimal odds must be finite and greater than 1")

    odds_market = _market_probabilities_from_odds(supplied_odds)
    if market_probabilities is not None:
        _require_keys(market_probabilities, OUTCOMES, "market probabilities")
        _validate_probability_total(
            market_probabilities,
            label="market",
            tolerance=tolerance,
        )
        if odds_market is not None:
            maximum_market_difference_pp = max(
                abs(market_probabilities[key] - odds_market[key]) * 100
                for key in OUTCOMES
            )
            if maximum_market_difference_pp > tolerance_pp:
                raise ValueError(
                    "Supplied market probabilities disagree with odds-derived "
                    f"no-vig probabilities by {maximum_market_difference_pp:.3f}pp"
                )
            no_vig_market = odds_market
        else:
            no_vig_market = market_probabilities
    else:
        no_vig_market = odds_market

    candidates: list[dict[str, Any]] = []
    for selection in OUTCOMES:
        probability = matrix[selection]
        decimal_odds = supplied_odds.get(selection)
        market_probability = (
            no_vig_market.get(selection) if no_vig_market is not None else None
        )
        ev = probability * decimal_odds - 1 if decimal_odds is not None else None
        edge_pp = (
            (probability - market_probability) * 100
            if market_probability is not None
            else None
        )
        failed: list[str] = []
        if ev is None:
            failed.append("current odds unavailable")
        elif ev <= 0:
            failed.append(f"EV {ev * 100:.1f}% is not positive")
        if edge_pp is None:
            failed.append("no-vig market probability unavailable")
        elif edge_pp <= 0:
            failed.append(f"edge {edge_pp:.1f}pp is not positive")
        elif edge_pp < edge_threshold_pp:
            failed.append(f"edge {edge_pp:.1f}pp < {edge_threshold_pp:.1f}pp")
        if firm_count < minimum_firms:
            failed.append(f"firm count {firm_count} < {minimum_firms}")
        if data_quality not in {"medium", "high"}:
            failed.append(f"data quality {data_quality}")

        candidates.append(
            {
                "selection": selection,
                "probability": probability,
                "full_time_probability": full_probabilities[selection[1]],
                "half_time_probability": half_probabilities[selection[0]],
                "odds": decimal_odds,
                "market_probability": market_probability,
                "edge_pp": edge_pp,
                "ev": ev,
                "status": "formal" if not failed else "observation",
                "failed_thresholds": failed,
            }
        )

    # Model shape is primary. Odds and EV may qualify a selected path, but they
    # must never promote a low-probability path over a more likely joint path.
    candidates.sort(
        key=lambda item: (
            -item["probability"],
            -item["full_time_probability"],
            -item["half_time_probability"],
            -(item["ev"] if item["ev"] is not None else float("-inf")),
            item["selection"],
        )
    )
    for rank, item in enumerate(candidates, start=1):
        item["rank"] = rank

    top_two = candidates[:2]
    top_two_keys = {item["selection"] for item in top_two}
    value_anomalies = [
        {
            **item,
            "status": "recheck_not_promoted",
        }
        for item in candidates[2:]
        if item["status"] == "formal" and item["selection"] not in top_two_keys
    ]

    return {
        "ranking_basis": "joint_probability_first_ev_gate_only",
        "marginal_validation": {
            "passed": True,
            "tolerance_pp": tolerance_pp,
            "row_marginals": row_marginals,
            "expected_half_time": half_probabilities,
            "row_differences_pp": row_differences_pp,
            "column_marginals": column_marginals,
            "expected_full_time": full_probabilities,
            "column_differences_pp": column_differences_pp,
            "max_difference_pp": max_difference_pp,
        },
        "top_two": top_two,
        "formal_count": sum(item["status"] == "formal" for item in top_two),
        "value_anomalies": value_anomalies,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prob",
        action="append",
        required=True,
        help="HT/FT joint probability as HH=0.25; repeat for all nine outcomes",
    )
    parser.add_argument(
        "--half",
        action="append",
        required=True,
        help="Half-time H/D/A probability as H=0.40; repeat three times",
    )
    parser.add_argument(
        "--full",
        action="append",
        required=True,
        help="Full-time H/D/A probability as H=0.50; repeat three times",
    )
    parser.add_argument(
        "--odds",
        action="append",
        help="Optional current decimal odds as HH=3.20; repeat for all outcomes",
    )
    parser.add_argument(
        "--market-prob",
        action="append",
        help="Optional no-vig market probability as HH=0.20; repeat for all outcomes",
    )
    parser.add_argument("--firm-count", type=int, default=0)
    parser.add_argument(
        "--data-quality",
        choices=("high", "medium", "low", "unknown"),
        default="unknown",
    )
    parser.add_argument("--tolerance-pp", type=float, default=0.5)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = rank_htft(
        parse_assignments(args.prob, allowed=OUTCOMES, label="matrix"),
        parse_assignments(args.half, allowed=HALF_RESULTS, label="half-time"),
        parse_assignments(args.full, allowed=FULL_RESULTS, label="full-time"),
        odds=parse_assignments(args.odds, allowed=OUTCOMES, label="odds"),
        market_probabilities=(
            parse_assignments(
                args.market_prob,
                allowed=OUTCOMES,
                label="market probability",
            )
            if args.market_prob
            else None
        ),
        firm_count=args.firm_count,
        data_quality=args.data_quality,
        tolerance_pp=args.tolerance_pp,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

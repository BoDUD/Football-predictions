"""Validate an HT/FT matrix and select its two largest joint-probability scenarios."""

from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime
from typing import Any, Mapping

HALF_RESULTS = ("H", "D", "A")
FULL_RESULTS = ("H", "D", "A")
OUTCOMES = tuple(f"{half}{full}" for half in HALF_RESULTS for full in FULL_RESULTS)
SELECTION_BASIS = "probability_top2_v3_post_selection"
ACTIVE_MARKET_POLICY = "strict-oos-market-policy-v1"
HTFT_FORMAL_ENABLED = False
FULL_TIME_TIE_TOLERANCE = 1e-9
MODEL_ONLY_PAIR_MASS_THRESHOLD = 0.46
# The 0.50 threshold belongs only to the evaluator's untimestamped full-time
# opening-market research cohort. It is intentionally not a production gate.
RESEARCH_ONLY_FULL_TIME_OPENING_PAIR_MASS_THRESHOLD = 0.50
PAIR_MASS_THRESHOLDS = {
    "model_only": MODEL_ONLY_PAIR_MASS_THRESHOLD,
}
LEAGUE_PAIR_GATE_EVIDENCE_VERSION = "registry-bound-htft-evidence/1.0.0"
MIN_LEAGUE_GATE_SAMPLE = 100

STABILITY_WEIGHTS = {
    "conditional_follow_through": 0.45,
    "joint_support": 0.30,
    "full_time_support": 0.15,
    "state_continuity": 0.10,
}
MIN_SCENARIO_HALF_PROBABILITY = 0.15
MIN_SCENARIO_JOINT_PROBABILITY = 0.05
MIN_SCENARIO_CONDITIONAL_STABILITY = 0.25
EXACT_RESULT_ALIASES = {
    "H": "H",
    "HOME": "H",
    "D": "D",
    "DRAW": "D",
    "A": "A",
    "AWAY": "A",
}


def _wilson_lower_bound(hits: int, sample_count: int) -> float | None:
    if sample_count < 1:
        return None
    z = 1.959963984540054
    rate = hits / sample_count
    denominator = 1 + z * z / sample_count
    center = rate + z * z / (2 * sample_count)
    radius = z * math.sqrt(
        (rate * (1 - rate) + z * z / (4 * sample_count)) / sample_count
    )
    return (center - radius) / denominator


def _league_pair_gate_evidence(
    league_key: str | None,
    evidence: Mapping[str, Any] | None = None,
    *,
    model_hash: str | None = None,
) -> dict[str, Any]:
    if league_key is None or not league_key.strip():
        return {
            "version": LEAGUE_PAIR_GATE_EVIDENCE_VERSION,
            "league_key": None,
            "status": "missing_league_context",
            "production_confidence_eligible": False,
        }
    normalized = league_key.strip().casefold()
    if evidence is None:
        return {
            "version": LEAGUE_PAIR_GATE_EVIDENCE_VERSION,
            "league_key": normalized,
            "status": "registry_evidence_required",
            "production_confidence_eligible": False,
        }
    if not isinstance(evidence, Mapping):
        raise ValueError("league_evidence must be an object")
    required = {
        "version",
        "dataset_manifest_hash",
        "evaluation_hash",
        "model_hash",
        "league_key",
        "source_role",
        "threshold",
        "eligible_sample_count",
        "covered_count",
        "hit_count",
        "deployment_status",
        "regime_warning",
        "formal_htft_eligible",
        "production_confidence_eligible",
    }
    if set(evidence) != required:
        raise ValueError("league_evidence fields do not match the registry contract")
    if evidence.get("version") != LEAGUE_PAIR_GATE_EVIDENCE_VERSION:
        raise ValueError("league_evidence version is unsupported")
    if evidence.get("league_key") != normalized:
        raise ValueError("league_evidence league_key does not match league context")
    hash_pattern = r"sha256:[0-9a-f]{64}"
    for name in ("dataset_manifest_hash", "evaluation_hash", "model_hash"):
        value = evidence.get(name)
        if not isinstance(value, str) or not re.fullmatch(hash_pattern, value):
            raise ValueError(f"league_evidence.{name} must be a SHA-256 hash")
    if model_hash is None or evidence.get("model_hash") != model_hash:
        raise ValueError(
            "league_evidence model_hash does not match the prediction model"
        )
    if evidence.get("source_role") != "historical_post_selection_development_evidence":
        raise ValueError("league_evidence source_role is invalid")
    threshold = evidence.get("threshold")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or abs(float(threshold) - MODEL_ONLY_PAIR_MASS_THRESHOLD) > 1e-12
    ):
        raise ValueError("league_evidence threshold is not the registered threshold")
    counts: dict[str, int] = {}
    for name in ("eligible_sample_count", "covered_count", "hit_count"):
        value = evidence.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"league_evidence.{name} must be a non-negative integer")
        counts[name] = value
    eligible = counts["eligible_sample_count"]
    covered = counts["covered_count"]
    hits = counts["hit_count"]
    if covered > eligible or hits > covered:
        raise ValueError("league_evidence cohort counts are inconsistent")
    deployment_status = evidence.get("deployment_status")
    if deployment_status not in {"candidate", "shadow"}:
        raise ValueError(
            "league_evidence deployment_status must be candidate or shadow"
        )
    regime_warning = evidence.get("regime_warning")
    if regime_warning is not None and (
        not isinstance(regime_warning, str) or not regime_warning.strip()
    ):
        raise ValueError(
            "league_evidence regime_warning must be null or non-empty text"
        )
    if (
        evidence.get("formal_htft_eligible") is not False
        or evidence.get("production_confidence_eligible") is not False
    ):
        raise ValueError("historical league_evidence cannot authorize formal HT/FT")
    hit_rate = hits / covered if covered else None
    lower_bound = _wilson_lower_bound(hits, covered)
    sample_sufficient = covered >= MIN_LEAGUE_GATE_SAMPLE
    lower_bound_above_chance = lower_bound is not None and lower_bound > 0.5
    historical_component_signal = (
        deployment_status == "candidate"
        and sample_sufficient
        and lower_bound_above_chance
        and regime_warning is None
    )
    if deployment_status == "shadow":
        status = "shadow_model_live_forward_unconfirmed"
    elif regime_warning is not None:
        status = "competition_regime_shift_unconfirmed"
    elif not sample_sufficient:
        status = "league_gate_sample_too_small"
    elif not lower_bound_above_chance:
        status = "league_gate_lower_bound_not_above_chance"
    else:
        status = "historical_component_signal_live_forward_unconfirmed"
    return {
        "version": LEAGUE_PAIR_GATE_EVIDENCE_VERSION,
        "dataset_manifest_hash": evidence["dataset_manifest_hash"],
        "evaluation_hash": evidence["evaluation_hash"],
        "model_hash": evidence["model_hash"],
        "league_key": normalized,
        "status": status,
        "source_role": evidence["source_role"],
        "threshold": MODEL_ONLY_PAIR_MASS_THRESHOLD,
        "eligible_sample_count": eligible,
        "covered_count": covered,
        "hit_count": hits,
        "coverage": covered / eligible if eligible else None,
        "hit_rate_when_covered": hit_rate,
        "wilson_95_lower_bound": lower_bound,
        "minimum_sample": MIN_LEAGUE_GATE_SAMPLE,
        "sample_sufficient": sample_sufficient,
        "lower_bound_above_chance": lower_bound_above_chance,
        "historical_component_signal": historical_component_signal,
        "deployment_status": deployment_status,
        "regime_warning": regime_warning,
        "formal_htft_eligible": False,
        "production_confidence_eligible": False,
    }


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
    if any(
        not math.isfinite(value) or value < 0 or value > 1 for value in values.values()
    ):
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


def _normalize_exact_score_results(
    values: list[str] | tuple[str, ...] | None,
) -> list[str]:
    if values is None:
        return []
    if len(values) != 2:
        raise ValueError(
            "exact_score_results must contain exactly two Top-2 result labels"
        )
    normalized: list[str] = []
    for value in values:
        result = EXACT_RESULT_ALIASES.get(str(value).strip().upper())
        if result is None:
            raise ValueError(
                "exact_score_results values must be home/H, draw/D, or away/A"
            )
        normalized.append(result)
    return normalized


def _validate_anchor_context(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("anchor_context must be an object")
    if value.get("kind") != "half_time_current_market":
        raise ValueError(
            "registered ranking supports only half_time_current_market anchor context"
        )
    if value.get("complete") is not True or value.get("de_vigged") is not True:
        raise ValueError("anchor_context must be complete and explicitly de_vigged")
    source = value.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("anchor_context.source is required")
    captured_at = value.get("captured_at")
    if not isinstance(captured_at, str) or not captured_at.strip():
        raise ValueError("anchor_context.captured_at is required")
    try:
        parsed = datetime.fromisoformat(captured_at.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("anchor_context.captured_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("anchor_context.captured_at needs an explicit UTC offset")
    return {
        "kind": "half_time_current_market",
        "complete": True,
        "de_vigged": True,
        "source": source.strip(),
        "captured_at": parsed.isoformat(),
        "production_pair_mass_gate_validated": False,
    }


def _validate_odds_context(
    value: Mapping[str, Any] | None,
    *,
    firm_count: int,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("odds_context must be an object")
    if value.get("kind") != "current_htft_nine_way_market":
        raise ValueError("odds_context.kind must be current_htft_nine_way_market")
    if value.get("complete") is not True:
        raise ValueError("odds_context must explicitly declare complete=true")
    source = value.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("odds_context.source is required")
    context_firm_count = value.get("firm_count")
    if (
        isinstance(context_firm_count, bool)
        or not isinstance(context_firm_count, int)
        or context_firm_count < 1
        or context_firm_count != firm_count
    ):
        raise ValueError("odds_context.firm_count must match firm_count")

    parsed_times: dict[str, datetime] = {}
    for name in ("captured_at", "kickoff"):
        raw = value.get(name)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"odds_context.{name} is required")
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"odds_context.{name} must be ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"odds_context.{name} needs an explicit UTC offset")
        parsed_times[name] = parsed
    if parsed_times["captured_at"] >= parsed_times["kickoff"]:
        raise ValueError("odds_context.captured_at must be strictly before kickoff")
    return {
        "kind": "current_htft_nine_way_market",
        "complete": True,
        "source": source.strip(),
        "firm_count": context_firm_count,
        "captured_at": parsed_times["captured_at"].isoformat(),
        "kickoff": parsed_times["kickoff"].isoformat(),
        "pre_kickoff_verified": True,
    }


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
    exact_score_results: list[str] | tuple[str, ...] | None = None,
    anchor_context: Mapping[str, Any] | None = None,
    odds_context: Mapping[str, Any] | None = None,
    market_anchored: bool | None = None,
    league_key: str | None = None,
    league_evidence: Mapping[str, Any] | None = None,
    model_hash: str | None = None,
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
    if market_anchored not in {None, False}:
        raise ValueError(
            "boolean market_anchored is unsupported; pass audited anchor_context. "
            "The 0.50 full-time opening threshold is research-only"
        )
    normalized_anchor_context = _validate_anchor_context(anchor_context)
    normalized_odds_context = _validate_odds_context(
        odds_context,
        firm_count=firm_count,
    )
    league_gate_evidence = _league_pair_gate_evidence(
        league_key,
        league_evidence,
        model_hash=model_hash,
    )
    normalized_exact_results = _normalize_exact_score_results(exact_score_results)

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

    full_time_order = sorted(
        FULL_RESULTS,
        key=lambda result: (
            -full_probabilities[result],
            FULL_RESULTS.index(result),
        ),
    )
    second_highest_full_probability = sorted(
        full_probabilities.values(),
        reverse=True,
    )[1]
    coherent_full_time_results = [
        result
        for result in full_time_order
        if (
            full_probabilities[result]
            >= second_highest_full_probability - FULL_TIME_TIE_TOLERANCE
        )
    ]
    coherent_full_time_set = set(coherent_full_time_results)
    distinct_exact_results = list(dict.fromkeys(normalized_exact_results))
    exact_result_set = set(distinct_exact_results)

    supplied_odds = odds or {}
    for selection, decimal_odds in supplied_odds.items():
        if selection not in OUTCOMES:
            raise ValueError(f"Invalid HT/FT odds key: {selection}")
        if not math.isfinite(decimal_odds) or decimal_odds <= 1:
            raise ValueError("HT/FT decimal odds must be finite and greater than 1")

    odds_market = _market_probabilities_from_odds(supplied_odds)
    complete_executable_market = set(supplied_odds) == set(OUTCOMES)
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
        half_result, full_result = selection
        half_probability = half_probabilities[half_result]
        full_probability = full_probabilities[full_result]
        conditional_stability = (
            probability / half_probability if half_probability > 0 else 0.0
        )
        state_continuity = half_result == full_result
        full_time_thesis_rank = full_time_order.index(full_result) + 1
        coherence_gate_passed = full_result in coherent_full_time_set
        exact_score_result_aligned = (
            full_result in exact_result_set if normalized_exact_results else None
        )
        coherence_failures: list[str] = []
        if not coherence_gate_passed:
            coherence_failures.append(
                "terminal result "
                f"{full_result} ranks {full_time_thesis_rank} in the "
                "aggregate full-time model"
            )
        stability_components = {
            "conditional_follow_through": (
                STABILITY_WEIGHTS["conditional_follow_through"] * conditional_stability
            ),
            "joint_support": STABILITY_WEIGHTS["joint_support"] * probability,
            "full_time_support": (
                STABILITY_WEIGHTS["full_time_support"] * full_probability
            ),
            "state_continuity": (
                STABILITY_WEIGHTS["state_continuity"] if state_continuity else 0.0
            ),
        }
        stability_score = sum(stability_components.values()) * 100
        stability_failures: list[str] = []
        if half_probability < MIN_SCENARIO_HALF_PROBABILITY:
            stability_failures.append(
                "half-time state support "
                f"{half_probability * 100:.1f}% < "
                f"{MIN_SCENARIO_HALF_PROBABILITY * 100:.1f}%"
            )
        if probability < MIN_SCENARIO_JOINT_PROBABILITY:
            stability_failures.append(
                f"joint support {probability * 100:.1f}% < "
                f"{MIN_SCENARIO_JOINT_PROBABILITY * 100:.1f}%"
            )
        if conditional_stability < MIN_SCENARIO_CONDITIONAL_STABILITY:
            stability_failures.append(
                "conditional stability "
                f"{conditional_stability * 100:.1f}% < "
                f"{MIN_SCENARIO_CONDITIONAL_STABILITY * 100:.1f}%"
            )
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
        if not complete_executable_market:
            failed.append("complete current 9-way HT/FT odds unavailable")
        if normalized_odds_context is None:
            failed.append("audited pre-kickoff HT/FT odds provenance unavailable")
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
        failed.extend(
            f"scenario stability: {failure}" for failure in stability_failures
        )
        failed.extend(
            f"scenario coherence: {failure}" for failure in coherence_failures
        )

        candidates.append(
            {
                "selection": selection,
                "probability": probability,
                "full_time_probability": full_probability,
                "half_time_probability": half_probability,
                "conditional_stability": conditional_stability,
                "state_continuity": state_continuity,
                "full_time_thesis_rank": full_time_thesis_rank,
                "coherence_gate_passed": coherence_gate_passed,
                "coherence_gate_failures": coherence_failures,
                "exact_score_result_aligned": exact_score_result_aligned,
                "stability_score": round(stability_score, 4),
                "stability_components": {
                    key: round(value * 100, 4)
                    for key, value in stability_components.items()
                },
                "stability_gate_passed": not stability_failures,
                "stability_gate_failures": stability_failures,
                "odds": decimal_odds,
                "market_probability": market_probability,
                "edge_pp": edge_pp,
                "ev": ev,
                "diagnostic_qualification_status": (
                    "qualified" if not failed else "unqualified"
                ),
                "diagnostic_failed_thresholds": failed,
                "policy_status": "paused_observation_only",
                "status": "observation",
                "failed_thresholds": [
                    *failed,
                    f"market policy {ACTIVE_MARKET_POLICY} pauses HT/FT formal picks",
                ],
            }
        )

    # Strict OOS tests showed that raw joint-probability Top 2 beat the previous
    # scenario-stability selector. Select solely on the nine-cell distribution;
    # the canonical OUTCOMES order is the deterministic tie-break. Conditional
    # stability, state continuity, terminal-result coherence, exact-score
    # alignment, odds and EV remain audits/qualification gates and never move a
    # different path into either display slot.
    candidates.sort(
        key=lambda item: (
            -item["probability"],
            OUTCOMES.index(item["selection"]),
        )
    )
    selected = candidates[:2]
    pair_probability_mass = sum(item["probability"] for item in selected)
    matrix_mode = (
        "half_time_market_anchor_unvalidated"
        if normalized_anchor_context is not None
        else "model_only"
    )
    pair_mass_threshold = PAIR_MASS_THRESHOLDS.get(matrix_mode)
    pair_mass_threshold_crossed = (
        pair_mass_threshold is not None and pair_probability_mass >= pair_mass_threshold
    )
    pair_mass_gate_passed = (
        pair_mass_threshold_crossed
        and league_gate_evidence["production_confidence_eligible"] is True
    )
    if pair_mass_threshold is None:
        pair_confidence_status = "anchor_gate_unvalidated"
    elif not pair_mass_threshold_crossed:
        pair_confidence_status = "low_pair_probability_mass"
    elif league_gate_evidence["status"] == "missing_league_context":
        pair_confidence_status = "league_context_required"
    elif league_gate_evidence["status"] == "registry_evidence_required":
        pair_confidence_status = "registry_evidence_required"
    elif league_gate_evidence["status"] == "competition_regime_shift_unconfirmed":
        pair_confidence_status = "competition_regime_shift_unconfirmed"
    elif league_gate_evidence["status"] == "shadow_model_live_forward_unconfirmed":
        pair_confidence_status = "shadow_model_live_forward_unconfirmed"
    elif not league_gate_evidence["production_confidence_eligible"]:
        pair_confidence_status = "league_cohort_not_forward_confirmed"
    else:
        pair_confidence_status = "league_gate_forward_confirmed"

    slots = (
        ("main_stable_scenario", "main_probability_scenario"),
        ("alternate_stable_scenario", "alternate_probability_scenario"),
    )
    scenarios: list[dict[str, Any]] = []
    for (legacy_slot, probability_slot), item in zip(slots, selected, strict=True):
        scenario = dict(item)
        scenario["slot"] = legacy_slot
        scenario["probability_slot"] = probability_slot
        scenario["pair_probability_mass"] = pair_probability_mass
        scenario["pair_mass_threshold"] = pair_mass_threshold
        scenario["pair_mass_threshold_crossed"] = pair_mass_threshold_crossed
        scenario["pair_mass_gate_passed"] = pair_mass_gate_passed
        scenario["confidence_status"] = pair_confidence_status
        diagnostic_failures = list(scenario["diagnostic_failed_thresholds"])
        if pair_mass_threshold is None:
            diagnostic_failures.append(
                "no promoted pair-mass gate exists for a half-time market anchor"
            )
        elif not pair_mass_threshold_crossed:
            diagnostic_failures.append(
                "pair probability mass "
                f"{pair_probability_mass * 100:.1f}% < "
                f"{pair_mass_threshold * 100:.1f}% for {matrix_mode}"
            )
        if league_gate_evidence["production_confidence_eligible"] is not True:
            diagnostic_failures.append(
                "league gate evidence is not forward-confirmed: "
                f"{league_gate_evidence['status']}"
            )
        scenario["diagnostic_failed_thresholds"] = diagnostic_failures
        scenario["diagnostic_qualification_status"] = (
            "qualified" if not diagnostic_failures else "unqualified"
        )
        scenario["status"] = "observation"
        scenario["failed_thresholds"] = [
            *diagnostic_failures,
            f"market policy {ACTIVE_MARKET_POLICY} pauses HT/FT formal picks",
        ]
        scenario["stability_status"] = (
            "supported" if item["stability_gate_passed"] else "insufficient"
        )
        scenario["coherence_status"] = (
            "on_thesis" if item["coherence_gate_passed"] else "off_thesis_fallback"
        )
        if pair_mass_threshold is None:
            scenario["selection_reason"] = (
                "joint-probability Top-2 display slot; the verified half-time "
                "anchor has no promoted pair-mass confidence gate"
            )
        elif not pair_mass_threshold_crossed:
            scenario["selection_reason"] = (
                "joint-probability Top-2 display slot; pair mass is below the "
                f"{matrix_mode} confidence gate"
            )
        elif not pair_mass_gate_passed:
            scenario["selection_reason"] = (
                "joint-probability Top-2 display slot; the global descriptive "
                "threshold is crossed but league evidence is not forward-confirmed"
                + (
                    "; scenario stability evidence is below its qualification threshold"
                    if not item["stability_gate_passed"]
                    else ""
                )
            )
        elif not item["stability_gate_passed"]:
            scenario["selection_reason"] = (
                "joint-probability Top-2 display slot; scenario stability "
                "evidence is below its qualification threshold"
            )
        elif not item["coherence_gate_passed"]:
            scenario["selection_reason"] = (
                "joint-probability Top-2 display slot; terminal result is "
                "outside the aggregate full-time Top 2"
            )
        elif item["exact_score_result_aligned"] is True:
            scenario["selection_reason"] = (
                "joint-probability Top-2 display slot; terminal result also "
                "appears in the exact-score Top 2"
            )
        else:
            scenario["selection_reason"] = (
                "joint-probability Top-2 display slot selected from the "
                "canonical nine-outcome matrix"
            )
        scenarios.append(scenario)

    selected_keys = {item["selection"] for item in scenarios}
    value_anomalies = [
        {
            **item,
            "status": "recheck_not_promoted",
        }
        for item in candidates
        if item["diagnostic_qualification_status"] == "qualified"
        and item["selection"] not in selected_keys
    ]
    legacy_top_two = [
        {
            **scenario,
            "rank": rank,
        }
        for rank, scenario in enumerate(scenarios, start=1)
    ]

    return {
        "input_audit": {
            "odds": dict(sorted(supplied_odds.items())),
            "market_probabilities": (
                dict(sorted(market_probabilities.items()))
                if market_probabilities is not None
                else None
            ),
            "firm_count": firm_count,
            "data_quality": data_quality,
            "tolerance_pp": tolerance_pp,
            "edge_threshold_pp": edge_threshold_pp,
            "minimum_firms": minimum_firms,
            "exact_score_results": (
                list(normalized_exact_results)
                if exact_score_results is not None
                else None
            ),
            "anchor_context": normalized_anchor_context,
            "odds_context": normalized_odds_context,
            "league_key": (
                league_key.strip().casefold()
                if isinstance(league_key, str) and league_key.strip()
                else None
            ),
            "league_evidence": (
                dict(league_evidence) if league_evidence is not None else None
            ),
            "model_hash": model_hash,
        },
        "selection_basis": SELECTION_BASIS,
        "ranking_basis": SELECTION_BASIS,
        "selection_policy": {
            "version": SELECTION_BASIS,
            "primary_sort": "joint_probability_descending",
            "tie_break": "canonical_outcome_order",
            "canonical_outcome_order": list(OUTCOMES),
            "audit_fields_do_not_change_slots": True,
        },
        "matrix_mode": matrix_mode,
        "anchor_context": normalized_anchor_context,
        "odds_context": normalized_odds_context,
        "pair_probability_mass": pair_probability_mass,
        "pair_mass": pair_probability_mass,
        "pair_mass_threshold": pair_mass_threshold,
        "pair_mass_threshold_crossed": pair_mass_threshold_crossed,
        "pair_mass_gate_passed": pair_mass_gate_passed,
        "confidence_status": pair_confidence_status,
        "league_gate_evidence": league_gate_evidence,
        "pair_mass_policy": {
            "version": SELECTION_BASIS,
            "matrix_mode": matrix_mode,
            "probability_mass": pair_probability_mass,
            "threshold": pair_mass_threshold,
            "threshold_crossed": pair_mass_threshold_crossed,
            "passed": pair_mass_gate_passed,
            "confidence_status": pair_confidence_status,
            "thresholds": PAIR_MASS_THRESHOLDS,
            "league_evidence_required": True,
            "post_selection_evidence_not_promotion_proof": True,
            "research_only_full_time_opening_threshold": (
                RESEARCH_ONLY_FULL_TIME_OPENING_PAIR_MASS_THRESHOLD
            ),
        },
        "market_policy": {
            "version": ACTIVE_MARKET_POLICY,
            "htft_formal_enabled": HTFT_FORMAL_ENABLED,
            "status": "observation_only",
            "diagnostic_qualification_cannot_override_policy": True,
        },
        "stability_weights": STABILITY_WEIGHTS,
        "coherence_policy": {
            "version": SELECTION_BASIS,
            "aggregate_full_time_order": full_time_order,
            "allowed_terminal_results": coherent_full_time_results,
            "exact_score_top_two_results": normalized_exact_results,
            "exact_score_distinct_results": distinct_exact_results,
            "exact_score_audit_only": True,
        },
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
        "scenarios": scenarios,
        "top_two": legacy_top_two,
        "formal_count": 0,
        "diagnostically_qualified_count": sum(
            item["diagnostic_qualification_status"] == "qualified" for item in scenarios
        ),
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
    parser.add_argument(
        "--exact-result",
        action="append",
        choices=("home", "draw", "away"),
        help=(
            "Audit-only result class of one unconditional exact-score Top-2 "
            "candidate; supply exactly twice when used"
        ),
    )
    parser.add_argument("--firm-count", type=int, default=0)
    parser.add_argument("--odds-source")
    parser.add_argument("--odds-captured-at")
    parser.add_argument("--kickoff")
    parser.add_argument(
        "--league-key",
        help=(
            "canonical league key used to attach league-specific gate evidence; "
            "unsupported or omitted leagues cannot receive a confidence label"
        ),
    )
    parser.add_argument(
        "--league-evidence-file",
        help="Registry-exported league_pair_gate_evidence JSON object",
    )
    parser.add_argument(
        "--model-hash",
        help="Prediction model hash; required when league evidence is supplied",
    )
    parser.add_argument(
        "--data-quality",
        choices=("high", "medium", "low", "unknown"),
        default="unknown",
    )
    parser.add_argument("--tolerance-pp", type=float, default=0.5)
    parser.add_argument(
        "--market-anchored",
        action="store_true",
        help=(
            "Deprecated and rejected: boolean anchor claims are unaudited and the "
            "50%% full-time opening threshold is research-only"
        ),
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    league_evidence = None
    if args.league_evidence_file:
        try:
            with open(args.league_evidence_file, "r", encoding="utf-8") as handle:
                league_evidence = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"cannot read --league-evidence-file: {exc}") from exc
        if not isinstance(league_evidence, dict):
            raise SystemExit("--league-evidence-file must contain a JSON object")
    odds_context_values = (
        args.odds_source,
        args.odds_captured_at,
        args.kickoff,
    )
    if any(value is not None for value in odds_context_values) and not all(
        isinstance(value, str) and value.strip() for value in odds_context_values
    ):
        raise SystemExit(
            "--odds-source, --odds-captured-at, and --kickoff must be supplied together"
        )
    odds_context = (
        {
            "kind": "current_htft_nine_way_market",
            "complete": True,
            "source": args.odds_source,
            "firm_count": args.firm_count,
            "captured_at": args.odds_captured_at,
            "kickoff": args.kickoff,
        }
        if args.odds_source is not None
        else None
    )
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
        exact_score_results=args.exact_result,
        odds_context=odds_context,
        market_anchored=args.market_anchored,
        league_key=args.league_key,
        league_evidence=league_evidence,
        model_hash=args.model_hash,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

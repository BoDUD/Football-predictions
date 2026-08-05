#!/usr/bin/env python3
"""Build one strict public outlook from a validated joint prediction.

This module is deliberately presentation-neutral.  Both the plain-text and
image renderers can consume the same structure, so they cannot independently
pick a different winner, total-goal range, or HT/FT-score scenario.

The returned ranks describe probability concentration only.  In particular,
the joint HT/FT + exact-score scenarios are always high-variance references;
this layer cannot promote them to a formal recommendation.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

try:  # Imported as ``scripts.public_market_outlook`` from the repository root.
    from scripts import joint_path_kernel, joint_scenario_model
except ImportError:  # Invoked directly from the ``scripts`` directory.
    import joint_path_kernel  # type: ignore[no-redef]
    import joint_scenario_model  # type: ignore[no-redef]


ARTIFACT_TYPE = "soccer_public_market_outlook"
SCHEMA_VERSION = "1.1.0"
PROBABILITY_TOLERANCE = 1e-9
THREE_WAY_CLARITY_GAP_PP = 8.0
GOAL_RANGE_CLARITY_GAP_PP = 8.0
BTTS_CLARITY_GAP_PP = 10.0
JOINT_DISPLAY_COUNT = 3
JOINT_DISPLAY_POLICY = "dominant_half_time_three_way_branches_v1"
JOINT_SCENARIO_WARNING = (
    "联合情景属于高方差概率参考，不构成主推或正式推荐。"
)

_HALF_TIME_ITEMS = (("H", "胜"), ("D", "平"), ("A", "负"))
_ONE_X_TWO_ITEMS = (("home", "胜"), ("draw", "平"), ("away", "负"))
_GOAL_RANGE_ITEMS = (("0-1", "0-1球"), ("2-3", "2-3球"), ("4-6", "4-6球"), ("7+", "7+球"))
_BTTS_ITEMS = (("yes", "是"), ("no", "否"))
_HTFT_CODES = frozenset(
    half + full for half in ("H", "D", "A") for full in ("H", "D", "A")
)
_HTFT_ORDER = tuple(
    half + full for half in ("H", "D", "A") for full in ("H", "D", "A")
)


class PublicMarketOutlookError(ValueError):
    """Raised when an artifact cannot be exposed as a safe public outlook."""


def _finite_probability(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise PublicMarketOutlookError(f"{name} must be a finite probability")
    try:
        probability = float(value)
    except (TypeError, ValueError) as exc:
        raise PublicMarketOutlookError(
            f"{name} must be a finite probability"
        ) from exc
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise PublicMarketOutlookError(f"{name} must be between zero and one")
    return probability


def _distribution(
    value: Any,
    *,
    name: str,
    item_order: Sequence[tuple[str, str]],
    clarity_gap_pp: float,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicMarketOutlookError(f"{name} distribution is missing")
    expected_keys = {code for code, _label in item_order}
    if set(value) != expected_keys:
        raise PublicMarketOutlookError(
            f"{name} distribution must contain exactly {sorted(expected_keys)}"
        )
    items = [
        {
            "code": code,
            "label": label,
            "probability": _finite_probability(value[code], f"{name}.{code}"),
            "percentage": _finite_probability(
                value[code], f"{name}.{code}"
            )
            * 100.0,
        }
        for code, label in item_order
    ]
    probability_sum = math.fsum(item["probability"] for item in items)
    if abs(probability_sum - 1.0) > PROBABILITY_TOLERANCE:
        raise PublicMarketOutlookError(f"{name} probabilities must sum to one")

    # Stable source order is the deterministic tie-breaker.
    ranked = sorted(
        enumerate(items), key=lambda pair: (-pair[1]["probability"], pair[0])
    )
    top_one = dict(ranked[0][1])
    top_two = dict(ranked[1][1])
    gap_pp = (top_one["probability"] - top_two["probability"]) * 100.0
    if gap_pp < 0.0 and abs(gap_pp) <= 1e-12:
        gap_pp = 0.0
    clarity = "clear" if gap_pp >= clarity_gap_pp else "divided"
    ranked_items = [dict(item) for _index, item in ranked]
    if name == "goal_ranges":
        display_count = 1
        display_reason = "goal_ranges_public_top_one"
    elif len(ranked_items) == 2:
        display_count = 2
        display_reason = "binary_distribution"
    else:
        display_count = 2
        display_reason = "ordinary_market_top_two_reference"
    return {
        "distribution": items,
        "top1": top_one,
        "top2": top_two,
        "gap_percentage_points": gap_pp,
        "clarity_threshold_percentage_points": float(clarity_gap_pp),
        "clarity": clarity,
        "display_count": display_count,
        "display_items": ranked_items[:display_count],
        "display_reason": display_reason,
        "recommendation_eligible": False,
    }


def _require_safe_derived_audit(
    audits: Any,
    *,
    field: str,
    probability_mode: str,
) -> None:
    if not isinstance(audits, Mapping):
        raise PublicMarketOutlookError("derived_field_audits is missing")
    audit = audits.get(field)
    if not isinstance(audit, Mapping):
        raise PublicMarketOutlookError(f"derived_field_audits.{field} is missing")
    if (
        audit.get("provenance") != "validated_joint_cells"
        or audit.get("probability_mode") != probability_mode
        or audit.get("recommendation_eligible") is not False
        or audit.get("template_fallback_allowed") is not False
    ):
        raise PublicMarketOutlookError(
            f"derived_field_audits.{field} is not safe for public output"
        )


def _probabilities_match(left: float, right: float) -> bool:
    return abs(left - right) <= PROBABILITY_TOLERANCE


def _validate_one_x_two_consistency(
    one_x_two: Mapping[str, Any],
    full_time_result: Any,
) -> None:
    if not isinstance(full_time_result, Mapping):
        raise PublicMarketOutlookError(
            "htft_marginal.full_time_result_probabilities is missing"
        )
    mapping = {"home": "H", "draw": "D", "away": "A"}
    if set(full_time_result) != set(mapping.values()):
        raise PublicMarketOutlookError(
            "HT/FT full-time marginal must contain exactly H, D, and A"
        )
    for public_code, htft_code in mapping.items():
        public_probability = _finite_probability(
            one_x_two[public_code], f"derived.one_x_two.{public_code}"
        )
        htft_probability = _finite_probability(
            full_time_result[htft_code],
            f"htft_marginal.full_time_result_probabilities.{htft_code}",
        )
        if not _probabilities_match(public_probability, htft_probability):
            raise PublicMarketOutlookError(
                "1X2 distribution conflicts with the HT/FT full-time marginal"
            )


def _canonical_scenario_item(
    slot: int,
    *,
    htft: str,
    home_goals: int,
    away_goals: int,
    probability: float,
) -> dict[str, Any]:
    return {
        "slot": slot,
        "htft": htft,
        "score": f"{home_goals}-{away_goals}",
        "home_goals": home_goals,
        "away_goals": away_goals,
        "probability": probability,
        "status": "high_variance_reference",
        "recommendation_eligible": False,
        "counts_toward_primary_record": False,
        "odds_available": False,
    }


def _public_scenario_item(item: Mapping[str, Any]) -> dict[str, Any]:
    probability = float(item["probability"])
    return {
        **dict(item),
        "percentage": probability * 100.0,
        "counts_as_primary": False,
        "requires_bookmaker_odds": False,
    }


def _full_result_code(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "H"
    if home_goals < away_goals:
        return "A"
    return "D"


def _validate_saved_joint_top_two(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 2:
        raise PublicMarketOutlookError("joint_top_two must contain exactly two items")
    result: list[dict[str, Any]] = []
    previous_probability: float | None = None
    for index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise PublicMarketOutlookError(f"joint_top_two[{index - 1}] is invalid")
        if (
            item.get("slot") != index
            or item.get("status") != "high_variance_reference"
            or item.get("recommendation_eligible") is not False
            or item.get("counts_toward_primary_record") is not False
            or item.get("odds_available") is not False
        ):
            raise PublicMarketOutlookError(
                f"joint_top_two[{index - 1}] violates high-variance safety policy"
            )
        if (
            "counts_as_primary" in item
            and item.get("counts_as_primary") is not False
        ) or (
            "requires_bookmaker_odds" in item
            and item.get("requires_bookmaker_odds") is not False
        ):
            raise PublicMarketOutlookError(
                f"joint_top_two[{index - 1}] contains an unsafe policy alias"
            )
        htft = item.get("htft")
        score = item.get("score")
        home_goals = item.get("home_goals")
        away_goals = item.get("away_goals")
        if htft not in _HTFT_CODES:
            raise PublicMarketOutlookError(
                f"joint_top_two[{index - 1}].htft is invalid"
            )
        if (
            isinstance(home_goals, bool)
            or not isinstance(home_goals, int)
            or home_goals < 0
            or isinstance(away_goals, bool)
            or not isinstance(away_goals, int)
            or away_goals < 0
            or score != f"{home_goals}-{away_goals}"
            or str(htft)[1] != _full_result_code(home_goals, away_goals)
        ):
            raise PublicMarketOutlookError(
                f"joint_top_two[{index - 1}] score fields are inconsistent"
            )
        probability = _finite_probability(
            item.get("probability"), f"joint_top_two[{index - 1}].probability"
        )
        if probability <= 0.0:
            raise PublicMarketOutlookError("joint scenario probabilities must be positive")
        if previous_probability is not None and probability > previous_probability:
            raise PublicMarketOutlookError("joint_top_two is not probability-ranked")
        previous_probability = probability
        result.append(
            _canonical_scenario_item(
                index,
                htft=str(htft),
                home_goals=home_goals,
                away_goals=away_goals,
                probability=probability,
            )
        )
    return result


def _rank_legacy_joint_cells(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PublicMarketOutlookError("legacy joint_cells is missing")
    ranked: list[tuple[float, int, int, int, str]] = []
    total_terms: list[float] = []
    for index, cell in enumerate(value):
        if not isinstance(cell, Mapping):
            raise PublicMarketOutlookError(f"joint_cells[{index}] is invalid")
        probability = _finite_probability(
            cell.get("probability"), f"joint_cells[{index}].probability"
        )
        total_terms.append(probability)
        if probability <= 0.0:
            continue
        htft = cell.get("htft")
        home_goals = cell.get("home_goals")
        away_goals = cell.get("away_goals")
        if (
            htft not in _HTFT_CODES
            or isinstance(home_goals, bool)
            or not isinstance(home_goals, int)
            or home_goals < 0
            or isinstance(away_goals, bool)
            or not isinstance(away_goals, int)
            or away_goals < 0
            or cell.get("score") != f"{home_goals}-{away_goals}"
            or str(htft)[1] != _full_result_code(home_goals, away_goals)
        ):
            raise PublicMarketOutlookError(
                f"joint_cells[{index}] contains an invalid positive event"
            )
        ranked.append(
            (
                probability,
                _HTFT_ORDER.index(str(htft)),
                home_goals,
                away_goals,
                str(htft),
            )
        )
    if abs(math.fsum(total_terms) - 1.0) > PROBABILITY_TOLERANCE:
        raise PublicMarketOutlookError("legacy joint_cells probabilities must sum to one")
    ranked.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    if len(ranked) < 2:
        raise PublicMarketOutlookError(
            "legacy joint distribution has fewer than two positive events"
        )
    return [
        _canonical_scenario_item(
            slot,
            htft=htft,
            home_goals=home_goals,
            away_goals=away_goals,
            probability=probability,
        )
        for slot, (probability, _code_index, home_goals, away_goals, htft) in enumerate(
            ranked, start=1
        )
    ]


def _rank_kernel_event_planes(path_kernel: Any) -> list[dict[str, Any]]:
    if not isinstance(path_kernel, Mapping):
        raise PublicMarketOutlookError("path_kernel is missing")
    try:
        reconstructed = joint_path_kernel.validate_kernel(path_kernel)
    except joint_path_kernel.PathKernelError as exc:
        raise PublicMarketOutlookError(
            f"path_kernel reconstruction failed: {exc}"
        ) from exc
    planes = reconstructed.get("event_planes")
    if not isinstance(planes, Sequence) or len(planes) != 3:
        raise PublicMarketOutlookError(
            "path_kernel event_planes must contain H, D, and A"
        )
    ranked: list[tuple[float, int, int, int, str]] = []
    total_terms: list[float] = []
    shape: tuple[int, int] | None = None
    for half_index, plane in enumerate(planes):
        if not isinstance(plane, Sequence) or not plane:
            raise PublicMarketOutlookError(
                f"path_kernel event_planes[{half_index}] is invalid"
            )
        rows: list[list[float]] = []
        for home_goals, raw_row in enumerate(plane):
            if not isinstance(raw_row, Sequence) or not raw_row:
                raise PublicMarketOutlookError(
                    f"path_kernel event_planes[{half_index}][{home_goals}] is invalid"
                )
            row = [
                _finite_probability(
                    probability,
                    f"path_kernel event_planes[{half_index}]"
                    f"[{home_goals}][{away_goals}]",
                )
                for away_goals, probability in enumerate(raw_row)
            ]
            rows.append(row)
            total_terms.extend(row)
        plane_shape = (len(rows), len(rows[0]))
        if any(len(row) != plane_shape[1] for row in rows):
            raise PublicMarketOutlookError("path_kernel event planes must be rectangular")
        if shape is None:
            shape = plane_shape
        elif plane_shape != shape:
            raise PublicMarketOutlookError("path_kernel event planes must share one grid")
        half_result = ("H", "D", "A")[half_index]
        for home_goals, row in enumerate(rows):
            for away_goals, probability in enumerate(row):
                if probability <= 0.0:
                    continue
                htft = half_result + _full_result_code(home_goals, away_goals)
                ranked.append(
                    (
                        probability,
                        _HTFT_ORDER.index(htft),
                        home_goals,
                        away_goals,
                        htft,
                    )
                )
    if abs(math.fsum(total_terms) - 1.0) > PROBABILITY_TOLERANCE:
        raise PublicMarketOutlookError("path_kernel event planes must sum to one")
    ranked.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    if len(ranked) < 2:
        raise PublicMarketOutlookError(
            "path_kernel joint distribution has fewer than two positive events"
        )
    return [
        _canonical_scenario_item(
            slot,
            htft=htft,
            home_goals=home_goals,
            away_goals=away_goals,
            probability=probability,
        )
        for slot, (probability, _code_index, home_goals, away_goals, htft) in enumerate(
            ranked, start=1
        )
    ]


def _reconstruct_joint_ranking(
    joint_artifact: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    version_pair = (
        joint_artifact.get("schema_version"),
        joint_artifact.get("model_version"),
    )
    if version_pair == (
        joint_scenario_model.LEGACY_SCHEMA_VERSION,
        joint_scenario_model.LEGACY_MODEL_VERSION,
    ):
        return _rank_legacy_joint_cells(joint_artifact.get("joint_cells")), (
            "legacy_joint_cells"
        )
    if version_pair == (
        joint_scenario_model.SCHEMA_VERSION,
        joint_scenario_model.MODEL_VERSION,
    ):
        return _rank_kernel_event_planes(joint_artifact.get("path_kernel")), (
            "validated_path_kernel_event_planes"
        )
    raise PublicMarketOutlookError(
        "unsupported schema/model pair for public joint reconstruction"
    )


def _safe_joint_scenarios(
    joint_artifact: Mapping[str, Any],
    *,
    audits: Any,
    probability_mode: str,
    half_time_market: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(audits, Mapping):
        raise PublicMarketOutlookError("derived_field_audits is missing")
    audit = audits.get("joint_top_two")
    if not isinstance(audit, Mapping) or (
        audit.get("provenance")
        != "validated_joint_cells_probability_ranking"
        or audit.get("probability_mode") != probability_mode
        or audit.get("status") != "high_variance_reference"
        or audit.get("recommendation_eligible") is not False
        or audit.get("template_fallback_allowed") is not False
    ):
        raise PublicMarketOutlookError(
            "derived_field_audits.joint_top_two is not a safe reference"
        )
    saved_top_two = _validate_saved_joint_top_two(
        joint_artifact.get("joint_top_two")
    )
    reconstructed, ranking_source = _reconstruct_joint_ranking(joint_artifact)
    if reconstructed[:2] != saved_top_two:
        raise PublicMarketOutlookError(
            "reconstructed joint Top2 does not exactly match artifact joint_top_two"
        )

    half_time_top1 = half_time_market.get("top1")
    if not isinstance(half_time_top1, Mapping):
        raise PublicMarketOutlookError("half-time public leader is missing")
    selected_half_time = str(half_time_top1.get("code") or "")
    if selected_half_time not in ("H", "D", "A"):
        raise PublicMarketOutlookError("half-time public leader is invalid")
    selected_half_time_probability = _finite_probability(
        half_time_top1.get("probability"), "half_time.top1.probability"
    )
    if selected_half_time_probability <= 0.0:
        raise PublicMarketOutlookError("half-time public leader must have positive mass")

    htft_payload = joint_artifact.get("htft_marginal")
    if not isinstance(htft_payload, Mapping):
        raise PublicMarketOutlookError("htft_marginal is missing")
    raw_code_probabilities = htft_payload.get("code_probabilities")
    if not isinstance(raw_code_probabilities, Mapping) or set(
        raw_code_probabilities
    ) != set(_HTFT_ORDER):
        raise PublicMarketOutlookError(
            "htft_marginal.code_probabilities must contain all nine branches"
        )
    code_probabilities = {
        code: _finite_probability(
            raw_code_probabilities[code], f"htft_marginal.code_probabilities.{code}"
        )
        for code in _HTFT_ORDER
    }
    selected_row_probability = math.fsum(
        code_probabilities[selected_half_time + full] for full in ("H", "D", "A")
    )
    if not _probabilities_match(
        selected_row_probability, selected_half_time_probability
    ):
        raise PublicMarketOutlookError(
            "selected HT/FT row does not match the half-time public leader"
        )

    representatives: dict[str, dict[str, Any]] = {}
    for item in reconstructed:
        htft = str(item["htft"])
        if htft[0] == selected_half_time and htft[1] not in representatives:
            representatives[htft[1]] = dict(item)
    if set(representatives) != {"H", "D", "A"}:
        raise PublicMarketOutlookError(
            "selected half-time state does not have all three full-time branches"
        )

    # Enumerate one coherent three-way tree: continuity first, then the other
    # full-time H/D/A outcomes in canonical order.  Conditional probabilities
    # remain explicit, so the stable structural order is never mistaken for a
    # probability ranking.  The archived global joint Top 2 remains an
    # immutable component audit and is not reused as the public scenario list.
    full_result_order = {code: index for index, code in enumerate(("H", "D", "A"))}
    probability_ranked_full_results = sorted(
        ("H", "D", "A"),
        key=lambda full: (
            -code_probabilities[selected_half_time + full],
            full_result_order[full],
        ),
    )
    probability_rank = {
        full: rank
        for rank, full in enumerate(probability_ranked_full_results, start=1)
    }
    display_full_results = (
        selected_half_time,
        *(full for full in ("H", "D", "A") if full != selected_half_time),
    )
    display_items: list[dict[str, Any]] = []
    for slot, full_result in enumerate(display_full_results, start=1):
        code = selected_half_time + full_result
        representative = _public_scenario_item(representatives[full_result])
        representative["raw_rank"] = representative["slot"]
        representative["slot"] = slot
        representative["branch_probability"] = code_probabilities[code]
        representative["branch_percentage"] = code_probabilities[code] * 100.0
        representative["conditional_probability"] = (
            code_probabilities[code] / selected_half_time_probability
        )
        representative["conditional_percentage"] = (
            representative["conditional_probability"] * 100.0
        )
        representative["branch_probability_rank"] = probability_rank[full_result]
        representative["selection_role"] = (
            "highest_joint_score_path_within_htft_branch"
        )
        display_items.append(representative)

    top_one_top_two_gap_pp = (
        reconstructed[0]["probability"] - reconstructed[1]["probability"]
    ) * 100.0
    top_two_top_three_gap_pp = (
        (reconstructed[1]["probability"] - reconstructed[2]["probability"])
        * 100.0
        if len(reconstructed) >= 3
        else None
    )
    third_probability = (
        float(reconstructed[2]["probability"])
        if len(reconstructed) >= 3
        else None
    )
    return {
        "items": display_items,
        "display_count": JOINT_DISPLAY_COUNT,
        "display_items": [dict(item) for item in display_items],
        "display_reason": (
            "dominant_half_time_three_way_branch_coverage_continuity_first"
        ),
        "display_policy": JOINT_DISPLAY_POLICY,
        "selected_half_time_result": selected_half_time,
        "selected_half_time_probability": selected_half_time_probability,
        "branch_order_basis": "continuity_first_then_canonical_full_result_order",
        "branch_order_is_probability_ranked": False,
        "branch_probability_basis": "conditional_full_time_given_selected_half_time",
        "representative_score_basis": (
            "highest_joint_probability_score_path_within_each_htft_branch"
        ),
        "ranking_source": ranking_source,
        "reconstructed_positive_event_count": len(reconstructed),
        "artifact_top_two_exact_match": True,
        "top1_top2_gap_percentage_points": top_one_top_two_gap_pp,
        "top2_top3_gap_percentage_points": top_two_top_three_gap_pp,
        "third_probability": third_probability,
        "status": "high_variance_reference",
        "recommendation_eligible": False,
        "counts_as_primary": False,
        "requires_bookmaker_odds": False,
        "warning": JOINT_SCENARIO_WARNING,
    }


def build_public_market_outlook(joint_artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Return complete, ranked public-market distributions from one artifact.

    ``joint_scenario_model.validate_prediction`` is always called first.  The
    public layer then performs its own cross-market and deployment-policy checks
    before returning a newly constructed mapping; the input is never mutated.
    """

    if not isinstance(joint_artifact, Mapping):
        raise PublicMarketOutlookError("joint artifact must be an object")
    try:
        joint_scenario_model.validate_prediction(joint_artifact)
    except (joint_scenario_model.JointScenarioError, TypeError, ValueError) as exc:
        raise PublicMarketOutlookError(
            f"joint artifact validation failed: {exc}"
        ) from exc

    probability_mode = joint_artifact.get("probability_mode")
    if probability_mode not in joint_scenario_model.PROBABILITY_MODES:
        raise PublicMarketOutlookError("unsupported joint probability mode")
    if joint_artifact.get("formal_eligible") is not False:
        raise PublicMarketOutlookError(
            "joint artifact cannot be formally eligible in this output layer"
        )

    htft = joint_artifact.get("htft_marginal")
    derived = joint_artifact.get("derived")
    audits = joint_artifact.get("derived_field_audits")
    if not isinstance(htft, Mapping):
        raise PublicMarketOutlookError("htft_marginal is missing")
    if not isinstance(derived, Mapping):
        raise PublicMarketOutlookError("derived markets are missing")

    for field in ("htft_marginal", "one_x_two", "goal_ranges", "btts"):
        _require_safe_derived_audit(
            audits, field=field, probability_mode=str(probability_mode)
        )

    half_time = _distribution(
        htft.get("half_time_result_probabilities"),
        name="half_time",
        item_order=_HALF_TIME_ITEMS,
        clarity_gap_pp=THREE_WAY_CLARITY_GAP_PP,
    )
    one_x_two_raw = derived.get("one_x_two")
    if not isinstance(one_x_two_raw, Mapping):
        raise PublicMarketOutlookError("derived.one_x_two is missing")
    _validate_one_x_two_consistency(
        one_x_two_raw, htft.get("full_time_result_probabilities")
    )
    one_x_two = _distribution(
        one_x_two_raw,
        name="one_x_two",
        item_order=_ONE_X_TWO_ITEMS,
        clarity_gap_pp=THREE_WAY_CLARITY_GAP_PP,
    )
    goal_ranges = _distribution(
        derived.get("goal_ranges"),
        name="goal_ranges",
        item_order=_GOAL_RANGE_ITEMS,
        clarity_gap_pp=GOAL_RANGE_CLARITY_GAP_PP,
    )
    btts = _distribution(
        derived.get("btts"),
        name="btts",
        item_order=_BTTS_ITEMS,
        clarity_gap_pp=BTTS_CLARITY_GAP_PP,
    )
    joint_scenarios = _safe_joint_scenarios(
        joint_artifact,
        audits=audits,
        probability_mode=str(probability_mode),
        half_time_market=half_time,
    )

    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "source_prediction_hash": joint_artifact.get("prediction_hash"),
        "probability_mode": probability_mode,
        "markets": {
            "half_time": half_time,
            "one_x_two": one_x_two,
            "goal_ranges": goal_ranges,
            "btts": btts,
        },
        "joint_scenarios": joint_scenarios,
        "warning": JOINT_SCENARIO_WARNING,
        "formal_recommendation_generated": False,
    }


__all__ = [
    "ARTIFACT_TYPE",
    "BTTS_CLARITY_GAP_PP",
    "GOAL_RANGE_CLARITY_GAP_PP",
    "JOINT_DISPLAY_COUNT",
    "JOINT_DISPLAY_POLICY",
    "JOINT_SCENARIO_WARNING",
    "PublicMarketOutlookError",
    "SCHEMA_VERSION",
    "THREE_WAY_CLARITY_GAP_PP",
    "build_public_market_outlook",
]

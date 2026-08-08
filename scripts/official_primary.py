#!/usr/bin/env python3
"""Deterministically select one non-monetary evaluation primary per archive.

The evaluation primary is deliberately separate from the strict betting primary.  It
never changes candidate gates, formal eligibility, stake, settlement accounting or
ROI.  Callers must validate the candidate-evaluation audit before using this module.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any, Mapping

OFFICIAL_PRIMARY_SCHEMA_VERSION = "official-primary/1.0.0"
OFFICIAL_PRIMARY_SELECTION_POLICY = "mandatory-evaluation-primary-v1"
OFFICIAL_PRIMARY_TIERS = (
    "strict_formal",
    "counterfactual_shadow",
    "forced_executable",
    "model_only_1x2",
)

_COPY_FIELDS = (
    "candidate_id",
    "market",
    "identity",
    "market_identity",
    "market_identity_hash",
    "settlement_reference_outcome",
    "side",
    "selection",
    "submarket",
    "line",
    "minimum_goals",
    "maximum_goals",
    "odds",
    "odds_format",
    "probability",
    "settlement_probabilities",
    "ev",
    "edge_pp",
    "market_probability",
    "firm_count",
    "market_signal",
    "counterfactual_eligible",
    "formal_eligible",
    "shadow_selected",
    "shadow_rank",
    "source_evidence_binding",
    "model_binding",
)


class OfficialPrimaryError(ValueError):
    """Raised when a mandatory evaluation primary cannot be reproduced."""


def _hash_json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _number(value: Any, default: float = float("-inf")) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _confidence(candidate: Mapping[str, Any]) -> float:
    value = candidate.get("shadow_confidence")
    return _number(value.get("score")) if isinstance(value, Mapping) else float("-inf")


def _sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    """Stable cross-market order; frozen confidence wins when it exists."""

    return (
        -_confidence(candidate),
        -_number(candidate.get("probability"), 0.0),
        -_number(candidate.get("ev")),
        -_number(candidate.get("edge_pp")),
        -_number(candidate.get("firm_count"), 0.0),
        str(candidate.get("market") or ""),
        str(candidate.get("identity") or ""),
        str(candidate.get("candidate_id") or ""),
    )


def _gates_pass(candidate: Mapping[str, Any], categories: set[str]) -> bool:
    gates = candidate.get("gates")
    return isinstance(gates, list) and all(
        not isinstance(gate, Mapping)
        or str(gate.get("category") or "") not in categories
        or gate.get("passed") is True
        for gate in gates
    )


def _candidate_view(
    candidate: Mapping[str, Any], *, tier: str, audit: Mapping[str, Any]
) -> dict[str, Any]:
    value = {
        field: deepcopy(candidate[field])
        for field in _COPY_FIELDS
        if field in candidate
    }
    value.update(
        {
            "schema_version": OFFICIAL_PRIMARY_SCHEMA_VERSION,
            "selection_policy": OFFICIAL_PRIMARY_SELECTION_POLICY,
            "tier": tier,
            "source": "candidate_evaluation",
            "candidate_evaluation_audit_hash": audit.get("audit_hash"),
            "candidate_evaluation_observation_id": audit.get("observation_id"),
            "counts_toward_official_accuracy": True,
            "counts_toward_betting_record": tier == "strict_formal",
            "monetary_scope": "betting_primary_only"
            if tier == "strict_formal"
            else "none",
            "recommended_stake_units": 0.0,
        }
    )
    value["official_primary_hash"] = _hash_json(value)
    return value


def _model_only_1x2(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("probabilities")
    if not isinstance(raw, Mapping):
        raise OfficialPrimaryError("canonical full-time 1X2 probabilities are missing")
    labels = ("home_win", "draw", "away_win")
    probabilities = {label: _number(raw.get(label), -1.0) for label in labels}
    if any(
        value < 0.0 or value > 1.0 for value in probabilities.values()
    ) or not math.isclose(
        math.fsum(probabilities.values()), 1.0, rel_tol=0.0, abs_tol=1e-4
    ):
        raise OfficialPrimaryError("canonical full-time 1X2 probabilities are invalid")
    selected = min(
        labels, key=lambda label: (-probabilities[label], labels.index(label))
    )
    side = {"home_win": "home", "draw": "draw", "away_win": "away"}[selected]
    joint = record.get("joint_scenario_audit")
    model_binding = deepcopy(record.get("score_model_provenance"))
    if not isinstance(model_binding, Mapping) and isinstance(joint, Mapping):
        model_binding = {
            "source": "validated_joint_scenario",
            "joint_scenario_audit_hash": joint.get("audit_hash"),
            "joint_prediction_hash": joint.get("joint_prediction_hash"),
        }
    value: dict[str, Any] = {
        "schema_version": OFFICIAL_PRIMARY_SCHEMA_VERSION,
        "selection_policy": OFFICIAL_PRIMARY_SELECTION_POLICY,
        "tier": "model_only_1x2",
        "source": "canonical_score_model",
        "market": "full_time_1x2",
        "side": side,
        "selection": {"home": "H", "draw": "D", "away": "A"}[side],
        "identity": f"full_time_1x2:{side}",
        "probability": probabilities[selected],
        "categorical_probabilities": {
            "H": probabilities["home_win"],
            "D": probabilities["draw"],
            "A": probabilities["away_win"],
        },
        "model_binding": model_binding,
        "counts_toward_official_accuracy": True,
        "counts_toward_betting_record": False,
        "monetary_scope": "none",
        "recommended_stake_units": 0.0,
    }
    value["official_primary_hash"] = _hash_json(value)
    return value


def select_official_primary(
    record: Mapping[str, Any], audit: Mapping[str, Any]
) -> dict[str, Any]:
    """Select exactly one evaluation primary from a validated frozen archive."""

    candidates = [
        candidate
        for candidate in audit.get("candidates", [])
        if isinstance(candidate, Mapping)
    ]
    formal = [
        candidate
        for candidate in candidates
        if candidate.get("formal_eligible") is True
    ]
    if formal:
        primary_market = record.get("primary_market")
        primary_pick = record.get("primary_pick")
        matching = [
            candidate
            for candidate in formal
            if candidate.get("market") == primary_market
            and (
                not isinstance(primary_pick, Mapping)
                or candidate.get("side") == primary_pick.get("side")
                and candidate.get("selection") == primary_pick.get("selection")
                and candidate.get("line") == primary_pick.get("line")
            )
        ]
        return _candidate_view(
            min(matching or formal, key=_sort_key), tier="strict_formal", audit=audit
        )

    counterfactual = [
        candidate
        for candidate in candidates
        if candidate.get("counterfactual_eligible") is True
        and candidate.get("shadow_selected") is True
    ]
    if counterfactual:
        return _candidate_view(
            min(counterfactual, key=_sort_key),
            tier="counterfactual_shadow",
            audit=audit,
        )

    executable = [
        candidate
        for candidate in candidates
        if _gates_pass(candidate, {"integrity", "risk"})
    ]
    if executable:
        return _candidate_view(
            min(executable, key=_sort_key), tier="forced_executable", audit=audit
        )
    return _model_only_1x2(record)


def validate_official_primary(
    value: Mapping[str, Any], record: Mapping[str, Any], audit: Mapping[str, Any]
) -> bool:
    if value.get("schema_version") != OFFICIAL_PRIMARY_SCHEMA_VERSION:
        return False
    supplied = deepcopy(dict(value))
    supplied_hash = supplied.pop("official_primary_hash", None)
    if supplied_hash != _hash_json(supplied):
        return False
    try:
        expected = select_official_primary(record, audit)
    except (OfficialPrimaryError, TypeError, ValueError):
        return False
    return dict(value) == expected


def settle_official_primary(
    primary: Mapping[str, Any],
    candidate_results: Mapping[str, str | None],
    *,
    full_time_code: str,
) -> dict[str, Any]:
    if primary.get("source") == "candidate_evaluation":
        result = candidate_results.get(str(primary.get("candidate_id") or ""))
        hit = result in {"full_win", "half_win"} if result is not None else None
    else:
        selection = str(primary.get("selection") or "")
        result = "win" if selection == full_time_code else "loss"
        hit = result == "win"
    value = {
        "schema_version": "official-primary-settlement/1.0.0",
        "official_primary_hash": primary.get("official_primary_hash"),
        "tier": primary.get("tier"),
        "market": primary.get("market"),
        "identity": primary.get("identity"),
        "result": result,
        "hit": hit,
        "counts_toward_official_accuracy": result is not None,
        "counts_toward_betting_record": False,
        "monetary_scope": "none",
    }
    value["settlement_hash"] = _hash_json(value)
    return value

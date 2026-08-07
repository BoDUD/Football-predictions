#!/usr/bin/env python3
"""Derive a safe publication view from one frozen prediction version.

This module is deliberately presentation-only.  It never writes history, changes a
candidate gate, promotes a shadow, or contributes to settlement.  A public observation
can come only from the unique replay-valid ``candidate-evaluation/3.0.0`` audit bound to
the supplied archived version.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Mapping

try:  # Imported as ``scripts.publication_outlook`` from the repository root.
    from scripts import memory_store
except ImportError:  # Imported by a renderer after adding ``scripts`` to sys.path.
    import memory_store  # type: ignore[no-redef]

PUBLICATION_OUTLOOK_SCHEMA_VERSION = "publication-outlook/1.0.0"
OBSERVATION_TRANSITION_SCHEMA_VERSION = "observation-transition/1.0.0"
OBSERVATION_PRIMARY_SELECTION_POLICY = "cross-market-shadow-confidence-v1"

BLOCKER_TYPES = ("data", "value", "policy")

_DATA_GATES = frozenset(
    {
        "canonical_model_binding",
        "odds_provenance",
        "complete_current_market",
        "bookmaker_depth",
        "data_quality",
        "market_signal_classified",
        "market_specific_evidence",
    }
)
_VALUE_GATES = frozenset({"positive_ev", "positive_edge"})
_POLICY_GATES = frozenset(
    {
        "market_policy_enabled",
        "league_forward_evidence",
        "upstream_formal_policy",
    }
)
_SAFETY_GATES = frozenset({"adverse_signal_gate"})

_OBSERVATION_FIELDS = (
    "candidate_id",
    "market",
    "identity",
    "side",
    "selection",
    "submarket",
    "line",
    "minimum_goals",
    "maximum_goals",
    "odds",
    "odds_format",
    "probability",
    "ev",
    "edge_pp",
    "market_probability",
    "market_signal",
    "firm_count",
    "shadow_rank",
)


class PublicationOutlookError(ValueError):
    """Raised when an archived formal publication state is internally inconsistent."""


def _finite_number(value: Any, *, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _formal_primary(version: Mapping[str, Any]) -> dict[str, Any] | None:
    market = version.get("primary_market")
    pick = version.get("primary_pick")
    no_market = market in {None, "", "none"}
    if no_market and pick is None:
        return None
    if no_market or not isinstance(market, str) or not isinstance(pick, dict):
        raise PublicationOutlookError(
            "archived primary_market and primary_pick must either both be present or both be absent"
        )
    resolved_pick = pick
    if market == "half_time" and pick.get("market") not in {
        "1x2",
        "asian",
        "total",
    }:
        # Older archives sometimes wrote ``market=half_time`` into primary_pick
        # while retaining the executable submarket in half_time_pick. Preserve
        # that read compatibility without changing the frozen archive.
        half_time_pick = version.get("half_time_pick")
        if not isinstance(half_time_pick, dict) or half_time_pick.get("market") not in {
            "1x2",
            "asian",
            "total",
        }:
            raise PublicationOutlookError(
                "archived half_time primary has no executable submarket"
            )
        resolved_pick = half_time_pick
    return {
        "market": market,
        "pick": deepcopy(resolved_pick),
        "identity": _formal_direction_identity(market, resolved_pick),
    }


def _candidate_audit(
    version: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    audits = [
        audit
        for audit in memory_store.frozen_candidate_audits(version)
        if audit.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
        and audit.get("schema_version")
        == memory_store.CANDIDATE_EVALUATION_SCHEMA_VERSION
    ]
    if not audits:
        legacy_present = any(
            audit.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
            for audit in memory_store.frozen_candidate_audits(version)
        )
        return None, "legacy_only" if legacy_present else "missing"
    if len(audits) != 1:
        return None, "ambiguous"
    audit = audits[0]
    try:
        valid = memory_store.validated_candidate_evaluation_audit(audit, version)
    except (TypeError, ValueError):
        valid = False
    return (audit, "valid") if valid else (None, "invalid")


def _observation_sort_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    confidence = candidate.get("shadow_confidence")
    if not isinstance(confidence, Mapping):
        confidence = {}
    return (
        -_finite_number(confidence.get("score")),
        -_finite_number(confidence.get("settlement_safety_probability")),
        -_finite_number(confidence.get("firm_count")),
        str(candidate.get("market") or ""),
        str(candidate.get("identity") or ""),
        str(candidate.get("candidate_id") or ""),
    )


def _observation_view(candidate: Mapping[str, Any]) -> dict[str, Any]:
    confidence = candidate.get("shadow_confidence")
    view = {
        field: deepcopy(candidate[field])
        for field in _OBSERVATION_FIELDS
        if field in candidate
    }
    view.update(
        {
            "confidence_score": (
                _finite_number(confidence.get("score"))
                if isinstance(confidence, Mapping)
                else None
            ),
            "selection_policy": OBSERVATION_PRIMARY_SELECTION_POLICY,
            "status": "observation_only",
            "counts_toward_primary_record": False,
            "monetary_scope": "none",
        }
    )
    return view


def _select_observation_primary(
    audit: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, Mapping[str, Any] | None]:
    if not isinstance(audit, Mapping):
        return None, None
    candidates = [
        candidate
        for candidate in audit.get("candidates", [])
        if isinstance(candidate, Mapping)
        and candidate.get("counterfactual_eligible") is True
        and candidate.get("formal_eligible") is False
        and candidate.get("shadow_selected") is True
    ]
    if not candidates:
        return None, None
    selected = min(candidates, key=_observation_sort_key)
    return _observation_view(selected), selected


def _blocker(
    blocker_type: str,
    gate: str,
    reasons: Any,
    *,
    market: str | None = None,
) -> dict[str, Any]:
    normalized_reasons = (
        [str(reason).strip() for reason in reasons if str(reason).strip()]
        if isinstance(reasons, (list, tuple))
        else []
    )
    value: dict[str, Any] = {
        "type": blocker_type,
        "gate": gate,
        "reasons": normalized_reasons,
    }
    if market:
        value["market"] = market
    return value


def _classify_failed_gate(
    gate: Mapping[str, Any], *, market: str | None
) -> tuple[str, dict[str, Any]]:
    name = str(gate.get("gate") or "unknown_gate")
    category = str(gate.get("category") or "")
    if name in _DATA_GATES:
        blocker_type = "data"
    elif name in _VALUE_GATES:
        blocker_type = "value"
    elif name in _POLICY_GATES or category == "release":
        blocker_type = "policy"
    elif name in _SAFETY_GATES:
        blocker_type = "safety"
    else:
        # Unknown risk gates must not be mislabeled as a value or policy failure.
        # Keeping them in the separate safety channel is the fail-closed choice.
        blocker_type = "safety"
    return blocker_type, _blocker(
        blocker_type,
        name,
        gate.get("reasons"),
        market=market,
    )


def _deduplicate_blockers(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for value in values:
        key = (
            value.get("type"),
            value.get("gate"),
            value.get("market"),
            tuple(value.get("reasons", [])),
        )
        if key not in seen:
            seen.add(key)
            output.append(value)
    return output


def _candidate_blockers(
    candidate: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    safety: list[dict[str, Any]] = []
    market = str(candidate.get("market") or "") or None
    for gate in candidate.get("gates", []):
        if not isinstance(gate, Mapping) or gate.get("passed") is True:
            continue
        blocker_type, value = _classify_failed_gate(gate, market=market)
        (safety if blocker_type == "safety" else blockers).append(value)
    return blockers, safety


def _aggregate_blockers(
    audit: Mapping[str, Any] | None,
    selected: Mapping[str, Any] | None,
    *,
    audit_status: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    safety: list[dict[str, Any]] = []
    if selected is not None:
        selected_blockers, selected_safety = _candidate_blockers(selected)
        blockers.extend(selected_blockers)
        safety.extend(selected_safety)
    elif isinstance(audit, Mapping):
        for candidate in audit.get("candidates", []):
            if not isinstance(candidate, Mapping):
                continue
            candidate_values, candidate_safety = _candidate_blockers(candidate)
            blockers.extend(candidate_values)
            safety.extend(candidate_safety)
        for entry in audit.get("market_manifest", []):
            if not isinstance(entry, Mapping) or entry.get("status") != "unavailable":
                continue
            blockers.append(
                _blocker(
                    "data",
                    "market_unavailable",
                    entry.get("reasons"),
                    market=str(entry.get("market") or "") or None,
                )
            )
    else:
        blockers.append(
            _blocker(
                "data",
                "candidate_evaluation_unavailable",
                [f"candidate_evaluation_{audit_status}"],
            )
        )
    blockers = _deduplicate_blockers(blockers)
    safety = _deduplicate_blockers(safety)
    by_type = {
        blocker_type: [value for value in blockers if value.get("type") == blocker_type]
        for blocker_type in BLOCKER_TYPES
    }
    return by_type, safety


def publication_summary(version: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic, non-settling publication view for one version."""

    context = dict(version)
    stage = str(context.get("analysis_stage") or "initial")
    if stage not in {"initial", "lineup-check"}:
        raise PublicationOutlookError("analysis_stage must be initial or lineup-check")
    formal = _formal_primary(context)
    audit, audit_status = _candidate_audit(context)
    observation, selected_candidate = _select_observation_primary(audit)

    if formal is not None:
        state = "formal_primary"
        # There is exactly one public leading direction. Frozen shadows remain in
        # the audit, but a formal publication supersedes the derived observation.
        observation = None
        selected_candidate = None
        blocker_groups = {blocker_type: [] for blocker_type in BLOCKER_TYPES}
        safety_blockers: list[dict[str, Any]] = []
    else:
        state = (
            "observation_primary" if observation is not None else "no_usable_direction"
        )
        blocker_groups, safety_blockers = _aggregate_blockers(
            audit,
            selected_candidate,
            audit_status=audit_status,
        )
    formal_blockers = [
        value
        for blocker_type in BLOCKER_TYPES
        for value in blocker_groups[blocker_type]
    ]
    stage_name = "initial" if stage == "initial" else "lineup"
    return {
        "schema_version": PUBLICATION_OUTLOOK_SCHEMA_VERSION,
        "stage": stage,
        "stage_text": "初盘" if stage == "initial" else "临场",
        "stage_status": f"{stage_name}_{state}",
        "state": state,
        "formal_primary": formal,
        "observation_primary": observation,
        "formal_blockers": formal_blockers,
        "blockers": blocker_groups,
        "safety_blockers": safety_blockers,
        "candidate_evaluation_status": audit_status,
        "counts_toward_primary_record": formal is not None,
        "observation_counts_toward_primary_record": False,
        "observation_monetary_scope": "none",
    }


def _line_value(value: Any) -> str:
    if value is None:
        return ""
    return f"{float(value):.12g}"


def _candidate_direction_identity(candidate: Mapping[str, Any] | None) -> str | None:
    if not isinstance(candidate, Mapping):
        return None
    identity = str(candidate.get("identity") or "").strip()
    if identity:
        return identity
    market = str(candidate.get("market") or "")
    if market == "half_time":
        return ":".join(
            (
                market,
                str(candidate.get("submarket") or ""),
                str(candidate.get("side") or ""),
                _line_value(candidate.get("line")),
            )
        )
    if market in {"htft", "goal_range"}:
        return f"{market}:{candidate.get('selection') or ''}"
    if market == "btts":
        return f"{market}:{candidate.get('side') or ''}"
    return ":".join(
        (
            market,
            str(candidate.get("side") or ""),
            _line_value(candidate.get("line")),
        )
    )


def _formal_direction_identity(market: str, pick: Mapping[str, Any]) -> str:
    if market == "half_time":
        return ":".join(
            (
                market,
                str(pick.get("market") or ""),
                str(pick.get("side") or ""),
                _line_value(pick.get("line")),
            )
        )
    if market in {"htft", "goal_range"}:
        return f"{market}:{pick.get('selection') or ''}"
    if market == "btts":
        return f"{market}:{pick.get('side') or ''}"
    return ":".join(
        (market, str(pick.get("side") or ""), _line_value(pick.get("line")))
    )


def _as_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") == PUBLICATION_OUTLOOK_SCHEMA_VERSION:
        return deepcopy(dict(value))
    return publication_summary(value)


def observation_transition(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe a stage change without altering ``primary_change`` or settlement."""

    before = _as_summary(previous)
    after = _as_summary(current)
    previous_observation = _candidate_direction_identity(
        before.get("observation_primary")
    )
    current_observation = _candidate_direction_identity(
        after.get("observation_primary")
    )
    previous_formal = (
        before.get("formal_primary", {}).get("identity")
        if isinstance(before.get("formal_primary"), Mapping)
        else None
    )
    current_formal = (
        after.get("formal_primary", {}).get("identity")
        if isinstance(after.get("formal_primary"), Mapping)
        else None
    )

    if previous_observation is not None and current_formal is not None:
        status = (
            "upgraded_to_formal"
            if previous_observation == current_formal
            else "formalized_other_direction"
        )
    elif previous_formal is not None and current_formal is not None:
        status = (
            "formal_maintained"
            if previous_formal == current_formal
            else "formal_changed"
        )
    elif previous_formal is not None and current_observation is not None:
        status = "formal_cancelled_to_observation"
    elif previous_formal is not None:
        status = "formal_cancelled_to_none"
    elif current_formal is not None:
        status = "new_formal_without_previous_observation"
    elif previous_observation is not None and current_observation is not None:
        status = (
            "maintained" if previous_observation == current_observation else "changed"
        )
    elif previous_observation is not None:
        status = "disappeared"
    elif current_observation is not None:
        status = "appeared"
    else:
        status = "unchanged_no_usable_direction"

    return {
        "schema_version": OBSERVATION_TRANSITION_SCHEMA_VERSION,
        "status": status,
        "from_stage": before.get("stage"),
        "to_stage": after.get("stage"),
        "previous_state": before.get("state"),
        "current_state": after.get("state"),
        "previous_observation_identity": previous_observation,
        "current_observation_identity": current_observation,
        "previous_formal_identity": previous_formal,
        "current_formal_identity": current_formal,
        "counts_toward_primary_record": False,
        "monetary_scope": "none",
    }


__all__ = [
    "BLOCKER_TYPES",
    "OBSERVATION_PRIMARY_SELECTION_POLICY",
    "OBSERVATION_TRANSITION_SCHEMA_VERSION",
    "PUBLICATION_OUTLOOK_SCHEMA_VERSION",
    "PublicationOutlookError",
    "observation_transition",
    "publication_summary",
]

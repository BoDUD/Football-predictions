#!/usr/bin/env python3
"""Build an auditable joint HT/FT and exact-score scenario distribution.

The registered HT/FT model already contains separate half-time and second-half
score components.  This module convolves those components into feasible match
paths, aggregates the paths by half-time result and full-time exact score, and
uses iterative proportional fitting (IPF) to match both existing canonical
targets:

* every cell of the canonical full-time score matrix; and
* every cell of the registered HT/FT 3x3 matrix.

The result is a minimum-KL projection of the feasible path seed.  It is not a
heuristic pairing of two independent rankings.  A de-vigged half-time anchor
already used by the registered upstream HT/FT prediction is disclosed as an
upstream probability input.  Separately attached market evidence remains a
zero-weight diagnostic and never authorizes EV claims in this schema.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

try:  # Imported as ``scripts.joint_scenario_model`` from the repository root.
    from scripts import htft_model, joint_path_kernel, league_model_manager, score_model
except ImportError:  # Invoked directly as ``python scripts/joint_scenario_model.py``.
    import htft_model  # type: ignore[no-redef]
    import joint_path_kernel  # type: ignore[no-redef]
    import league_model_manager  # type: ignore[no-redef]
    import score_model  # type: ignore[no-redef]


ARTIFACT_TYPE = "soccer_joint_scenario_prediction"
SCHEMA_VERSION = "2.0.0"
MODEL_VERSION = "feasible-score-path-ipf/2.0.0"
LEGACY_SCHEMA_VERSION = "1.1.0"
LEGACY_MODEL_VERSION = "feasible-score-path-ipf/1.1.0"
SUPPORTED_VERSION_PAIRS = frozenset(
    {
        (SCHEMA_VERSION, MODEL_VERSION),
        (LEGACY_SCHEMA_VERSION, LEGACY_MODEL_VERSION),
    }
)
MODEL_ONLY_MODE = "model_only"
UPSTREAM_HALF_TIME_ANCHOR_MODE = "upstream_half_time_market_anchor"
PROBABILITY_MODES = frozenset({MODEL_ONLY_MODE, UPSTREAM_HALF_TIME_ANCHOR_MODE})
IPF_TOLERANCE = 1e-12
IPF_MAX_ITERATIONS = 10_000
PATH_TAIL_TOLERANCE = 1e-8
VALID_EVIDENCE_QUALITIES = frozenset({"high", "medium", "low", "unknown"})
MARKET_EVIDENCE_ARTIFACT_TYPE = "soccer_prematch_market_evidence"
MARKET_EVIDENCE_SCHEMA_VERSION = "1.0.0"
MARKET_EVIDENCE_MAX_BYTES = 256 * 1024
MARKET_EVIDENCE_MAX_DEPTH = 12
MARKET_EVIDENCE_MAX_CONTAINER_ITEMS = 2_048
MARKET_EVIDENCE_MAX_ROWS_PER_MARKET = 250
MARKET_EVIDENCE_MAX_TOTAL_ROWS = 750
RESULT_CODES = ("H", "D", "A")
HTFT_CODE_ORDER = tuple(half + full for half in RESULT_CODES for full in RESULT_CODES)
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class JointScenarioError(ValueError):
    """Raised when an input or derived joint scenario artifact is unsafe."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JointScenarioError("artifact must be canonical finite JSON") from exc


def content_hash(value: Any) -> str:
    """Return the semantic SHA-256 used to bind JSON inputs and predictions."""

    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def calculate_prediction_hash(prediction: Mapping[str, Any]) -> str:
    payload = dict(prediction)
    payload.pop("prediction_hash", None)
    return content_hash(payload)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise JointScenarioError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise JointScenarioError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise JointScenarioError(f"{name} must be finite")
    return result


def _parse_aware_datetime(value: Any, name: str) -> tuple[datetime, str]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise JointScenarioError(f"{name} must be an ISO-8601 datetime") from exc
    else:
        raise JointScenarioError(f"{name} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JointScenarioError(f"{name} needs an explicit UTC offset")
    utc_value = parsed.astimezone(timezone.utc)
    return utc_value, utc_value.isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _normalize_match_id(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise JointScenarioError(f"{name} must be a non-empty string or integer")
    normalized = str(value).strip()
    if not normalized:
        raise JointScenarioError(f"{name} must be a non-empty string or integer")
    return normalized


def _evidence_match_id(evidence: Mapping[str, Any] | None) -> str | None:
    if evidence is None:
        return None
    candidates: list[tuple[str, str]] = []
    for key in ("match_id", "fixture_id"):
        if key in evidence and evidence.get(key) is not None:
            candidates.append(
                (
                    f"market evidence.{key}",
                    _normalize_match_id(evidence[key], key) or "",
                )
            )
    evidence_fixture = evidence.get("fixture")
    if isinstance(evidence_fixture, Mapping):
        for key in ("match_id", "fixture_id"):
            if key in evidence_fixture and evidence_fixture.get(key) is not None:
                candidates.append(
                    (
                        f"market evidence.fixture.{key}",
                        _normalize_match_id(evidence_fixture[key], key) or "",
                    )
                )
    if not candidates:
        return None
    unique = {value for _name, value in candidates}
    if len(unique) != 1:
        names = ", ".join(name for name, _value in candidates)
        raise JointScenarioError(f"conflicting market evidence match IDs: {names}")
    return candidates[0][1]


def _fixture_identity(fixture: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "competition_key": fixture.get("competition_key"),
        "home_team": fixture.get("home_team"),
        "away_team": fixture.get("away_team"),
        "kickoff": fixture.get("kickoff"),
        "match_id": fixture.get("match_id"),
    }


def _validate_evidence_fixture(
    evidence_fixture: Any,
    joint_fixture: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(evidence_fixture, Mapping):
        raise JointScenarioError("market evidence.fixture must be an object")
    normalized: dict[str, Any] = {}
    for field in ("competition_key", "home_team", "away_team"):
        value = evidence_fixture.get(field)
        if not isinstance(value, str) or not value.strip():
            raise JointScenarioError(f"market evidence.fixture.{field} is required")
        value = value.strip()
        if "\ufffd" in value:
            raise JointScenarioError(
                f"market evidence.fixture.{field} contains replacement characters"
            )
        if value != joint_fixture.get(field):
            raise JointScenarioError(
                f"market evidence fixture {field} does not match joint fixture"
            )
        normalized[field] = value
    evidence_kickoff, canonical_evidence_kickoff = _parse_aware_datetime(
        evidence_fixture.get("kickoff"), "market evidence.fixture.kickoff"
    )
    joint_kickoff, _ = _parse_aware_datetime(
        joint_fixture.get("kickoff"), "joint fixture.kickoff"
    )
    if evidence_kickoff != joint_kickoff:
        raise JointScenarioError(
            "market evidence fixture kickoff does not match joint fixture"
        )
    normalized["kickoff"] = canonical_evidence_kickoff
    normalized["match_id"] = _evidence_match_id({"fixture": evidence_fixture})
    return normalized


def _validate_market_evidence_payload_limits(evidence: Mapping[str, Any]) -> None:
    payload_size = len(_canonical_bytes(evidence))
    if payload_size > MARKET_EVIDENCE_MAX_BYTES:
        raise JointScenarioError(
            f"market evidence exceeds {MARKET_EVIDENCE_MAX_BYTES} canonical bytes"
        )
    stack: list[tuple[Any, int]] = [(evidence, 1)]
    total_items = 0
    while stack:
        value, depth = stack.pop()
        if depth > MARKET_EVIDENCE_MAX_DEPTH:
            raise JointScenarioError(
                f"market evidence exceeds maximum JSON depth {MARKET_EVIDENCE_MAX_DEPTH}"
            )
        if isinstance(value, Mapping):
            total_items += len(value)
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            total_items += len(value)
            stack.extend((item, depth + 1) for item in value)
        if total_items > MARKET_EVIDENCE_MAX_CONTAINER_ITEMS:
            raise JointScenarioError(
                "market evidence exceeds maximum JSON container item count"
            )


def _has_forbidden_market_claim_key(evidence: Mapping[str, Any]) -> str | None:
    for key in evidence:
        normalized = str(key).strip().lower()
        if (
            normalized == "ev"
            or normalized.startswith("ev_")
            or normalized.endswith("_ev")
            or "expected_value" in normalized
            or "recommendation" in normalized
            or normalized == "formal"
            or normalized.startswith("formal_")
        ):
            return str(key)
    return None


def _valid_source_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _number_in_range(value: Any, minimum: float, maximum: float) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and minimum <= number <= maximum


def _quarter_line(value: Any) -> bool:
    if not _number_in_range(value, -20.0, 20.0):
        return False
    return abs(float(value) * 4.0 - round(float(value) * 4.0)) <= 1e-9


def _market_rows(
    section: Any,
    *,
    name: str,
    odds_format: str,
    row_validator: Any,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(section, Mapping):
        return [f"{name} section is missing"]
    if section.get("odds_format") != odds_format:
        errors.append(f"{name}.odds_format must be {odds_format}")
    rows = section.get("rows")
    if (
        not isinstance(rows, list)
        or not rows
        or len(rows) > MARKET_EVIDENCE_MAX_ROWS_PER_MARKET
    ):
        errors.append(f"{name}.rows count is invalid")
        return errors
    firm_count = section.get("stored_complete_firm_count")
    if (
        isinstance(firm_count, bool)
        or not isinstance(firm_count, int)
        or firm_count != len(rows)
    ):
        errors.append(f"{name}.stored_complete_firm_count does not match rows")
    for index, row in enumerate(rows):
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("bookmaker"), str)
            or not row["bookmaker"].strip()
        ):
            errors.append(f"{name}.rows[{index}] bookmaker is invalid")
            break
        if not row_validator(row):
            errors.append(f"{name}.rows[{index}] prices or line are invalid")
            break
    return errors


def _current_market_bundle_errors(evidence: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if evidence.get("artifact_type") != MARKET_EVIDENCE_ARTIFACT_TYPE:
        errors.append("unsupported market evidence artifact_type")
    if evidence.get("schema_version") != MARKET_EVIDENCE_SCHEMA_VERSION:
        errors.append("unsupported market evidence schema_version")
    sources = evidence.get("sources")
    required_sources = ("analysis", "one_x_two", "asian_handicap", "totals")
    if not isinstance(sources, Mapping) or any(
        not _valid_source_url(sources.get(name)) for name in required_sources
    ):
        errors.append("market evidence source URLs are incomplete or invalid")

    def one_x_two_row(row: Mapping[str, Any]) -> bool:
        return all(
            _number_in_range(row.get(label), 1.000001, 1_000.0)
            for label in ("home", "draw", "away")
        )

    def asian_row(row: Mapping[str, Any]) -> bool:
        for snapshot_name in ("opening", "current"):
            snapshot = row.get(snapshot_name)
            if not isinstance(snapshot, Mapping) or not (
                _number_in_range(snapshot.get("home_price"), 0.000001, 100.0)
                and _number_in_range(snapshot.get("away_price"), 0.000001, 100.0)
                and _quarter_line(snapshot.get("home_line"))
            ):
                return False
        return True

    def totals_row(row: Mapping[str, Any]) -> bool:
        for snapshot_name in ("opening", "current"):
            snapshot = row.get(snapshot_name)
            if not isinstance(snapshot, Mapping) or not (
                _number_in_range(snapshot.get("over_price"), 0.000001, 100.0)
                and _number_in_range(snapshot.get("under_price"), 0.000001, 100.0)
                and _quarter_line(snapshot.get("line"))
            ):
                return False
        return True

    errors.extend(
        _market_rows(
            evidence.get("one_x_two"),
            name="one_x_two",
            odds_format="decimal",
            row_validator=one_x_two_row,
        )
    )
    errors.extend(
        _market_rows(
            evidence.get("asian_handicap"),
            name="asian_handicap",
            odds_format="hong_kong",
            row_validator=asian_row,
        )
    )
    errors.extend(
        _market_rows(
            evidence.get("totals"),
            name="totals",
            odds_format="hong_kong",
            row_validator=totals_row,
        )
    )
    total_rows = math.fsum(
        len(evidence.get(name, {}).get("rows", []))
        if isinstance(evidence.get(name), Mapping)
        and isinstance(evidence[name].get("rows"), list)
        else 0
        for name in ("one_x_two", "asian_handicap", "totals")
    )
    if total_rows > MARKET_EVIDENCE_MAX_TOTAL_ROWS:
        errors.append("market evidence total row count exceeds the limit")
    quality = evidence.get("quality")
    if not isinstance(quality, Mapping) or quality.get("status") not in {
        "partial",
        "complete",
    }:
        errors.append("market evidence quality.status is not partial or complete")
    return errors


def _result_code(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "H"
    if home_goals < away_goals:
        return "A"
    return "D"


def _matrix_copy(value: Any, name: str) -> list[list[float]]:
    try:
        score_model._validate_matrix(value)
    except score_model.ScoreModelError as exc:
        raise JointScenarioError(f"{name} is invalid: {exc}") from exc
    return [[float(cell) for cell in row] for row in value]


def _validate_htft_probabilities(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(HTFT_CODE_ORDER):
        raise JointScenarioError(
            "HT/FT probabilities must contain exactly all nine codes"
        )
    probabilities = {
        code: _finite(value[code], f"HT/FT probability {code}")
        for code in HTFT_CODE_ORDER
    }
    if any(probability < 0.0 for probability in probabilities.values()):
        raise JointScenarioError("HT/FT probabilities cannot be negative")
    if abs(math.fsum(probabilities.values()) - 1.0) > 1e-9:
        raise JointScenarioError("HT/FT probabilities must sum to one")
    return probabilities


def _score_result_marginal(matrix: Sequence[Sequence[float]]) -> dict[str, float]:
    return {
        code: math.fsum(
            probability
            for home_goals, row in enumerate(matrix)
            for away_goals, probability in enumerate(row)
            if _result_code(home_goals, away_goals) == code
        )
        for code in RESULT_CODES
    }


def _htft_column_marginal(probabilities: Mapping[str, float]) -> dict[str, float]:
    return {
        full: math.fsum(probabilities[half + full] for half in RESULT_CODES)
        for full in RESULT_CODES
    }


def _htft_row_marginal(probabilities: Mapping[str, float]) -> dict[str, float]:
    return {
        half: math.fsum(probabilities[half + full] for full in RESULT_CODES)
        for half in RESULT_CODES
    }


def _validate_result_probabilities(value: Any, name: str) -> dict[str, float]:
    labels = ("home", "draw", "away")
    if not isinstance(value, Mapping) or set(value) != set(labels):
        raise JointScenarioError(f"{name} must contain exactly home, draw, and away")
    probabilities = {
        label: _finite(value[label], f"{name}.{label}") for label in labels
    }
    if any(probability < 0.0 for probability in probabilities.values()):
        raise JointScenarioError(f"{name} probabilities cannot be negative")
    if abs(math.fsum(probabilities.values()) - 1.0) > 1e-9:
        raise JointScenarioError(f"{name} probabilities must sum to one")
    return probabilities


def _resolve_external_anchor_mode(
    htft_prediction: Mapping[str, Any],
    *,
    generated_at: datetime,
    kickoff: datetime,
) -> tuple[str, dict[str, Any]]:
    provenance = htft_prediction.get("provenance")
    if not isinstance(provenance, Mapping):
        raise JointScenarioError("HT/FT prediction provenance is missing")
    targets = provenance.get("marginal_targets")
    if not isinstance(targets, Mapping):
        raise JointScenarioError("HT/FT marginal target provenance is missing")
    half_target = targets.get("half_time")
    full_target = targets.get("full_time")
    if not isinstance(half_target, Mapping) or not isinstance(full_target, Mapping):
        raise JointScenarioError("HT/FT half/full marginal targets are missing")
    external_enabled = provenance.get("external_anchor_enabled")
    if not isinstance(external_enabled, bool):
        raise JointScenarioError("HT/FT external_anchor_enabled must be boolean")
    half_origin = half_target.get("origin")
    full_origin = full_target.get("origin")
    if full_origin != "model_component":
        raise JointScenarioError(
            "external full-time anchors are unsupported because the canonical score "
            "matrix must remain the full-time probability source"
        )
    upstream_generated, canonical_upstream_generated = _parse_aware_datetime(
        htft_prediction.get("generated_at"), "HT/FT prediction generated_at"
    )
    if upstream_generated > generated_at or upstream_generated >= kickoff:
        raise JointScenarioError(
            "HT/FT prediction timing is invalid for joint generation"
        )
    upstream_prediction_hash = htft_prediction.get("prediction_hash")
    if not isinstance(upstream_prediction_hash, str) or not HASH_RE.fullmatch(
        upstream_prediction_hash
    ):
        raise JointScenarioError("HT/FT prediction_hash is required for anchor audit")

    no_anchor_audit = {
        "enabled": False,
        "role": "no_external_probability_input",
        "component": None,
        "origin": None,
        "source": None,
        "captured_at": None,
        "upstream_prediction_generated_at": canonical_upstream_generated,
        "de_vigged": None,
        "probabilities": None,
        "target_hash": None,
        "upstream_prediction_hash": upstream_prediction_hash,
        "used_for_probability": False,
        "same_price_independent_ev_authorized": False,
    }
    if not external_enabled:
        if half_origin != "model_component":
            raise JointScenarioError(
                "HT/FT half-time target origin conflicts with external-anchor flag"
            )
        return MODEL_ONLY_MODE, no_anchor_audit
    if half_origin != "external_de_vigged_anchor":
        raise JointScenarioError(
            "only an external_de_vigged_anchor half-time target is supported"
        )
    source = half_target.get("source")
    if not isinstance(source, str) or not source.strip():
        raise JointScenarioError("external half-time anchor source is required")
    if half_target.get("de_vigged") is not True:
        raise JointScenarioError("external half-time anchor must be de-vigged")
    captured_at, canonical_captured_at = _parse_aware_datetime(
        half_target.get("captured_at"), "external half-time anchor captured_at"
    )
    if captured_at > upstream_generated or captured_at > generated_at:
        raise JointScenarioError(
            "external half-time anchor cannot be captured after prediction generation"
        )
    if captured_at >= kickoff:
        raise JointScenarioError(
            "external half-time anchor must be captured strictly before kickoff"
        )
    probabilities = _validate_result_probabilities(
        half_target.get("probabilities"), "external half-time anchor probabilities"
    )
    htft_payload = htft_prediction.get("htft")
    if not isinstance(htft_payload, Mapping):
        raise JointScenarioError("HT/FT prediction matrix is missing")
    htft_probabilities = _validate_htft_probabilities(
        htft_payload.get("code_probabilities")
    )
    half_codes = _htft_row_marginal(htft_probabilities)
    for result, label in (("H", "home"), ("D", "draw"), ("A", "away")):
        if abs(half_codes[result] - probabilities[label]) > IPF_TOLERANCE:
            raise JointScenarioError(
                "external half-time anchor does not reproduce the HT/FT row marginal"
            )
    target_material = {
        "origin": "external_de_vigged_anchor",
        "source": source.strip(),
        "captured_at": canonical_captured_at,
        "de_vigged": True,
        "probabilities": probabilities,
    }
    audit = {
        "enabled": True,
        "role": "upstream_probability_input",
        "component": "half_time",
        **target_material,
        "upstream_prediction_generated_at": canonical_upstream_generated,
        "target_hash": content_hash(target_material),
        "upstream_prediction_hash": upstream_prediction_hash,
        "used_for_probability": True,
        "same_price_independent_ev_authorized": False,
    }
    return UPSTREAM_HALF_TIME_ANCHOR_MODE, audit


def _policy_for_mode(probability_mode: str) -> dict[str, Any]:
    if probability_mode not in PROBABILITY_MODES:
        raise JointScenarioError("unsupported joint scenario probability mode")
    anchored = probability_mode == UPSTREAM_HALF_TIME_ANCHOR_MODE
    return {
        "status": "diagnostic_only",
        "probability_mode": probability_mode,
        "upstream_half_time_anchor_changes_probabilities": anchored,
        "attached_market_evidence_changes_probabilities": False,
        "ev_claims_authorized": False,
        "same_price_independent_ev_authorized": False,
        "independent_ev_policy": (
            "a price or source used by the upstream half-time anchor cannot be "
            "reused as independent EV or edge evidence"
        ),
        "ranking_basis": "joint_htft_exact_score_probability",
        "top_two_diversity_constraint": False,
        "manual_scenario_override_allowed": False,
        "template_fallback_allowed": False,
        "top_two_tie_break": [
            "probability_descending",
            "canonical_htft_code_order",
            "home_goals_ascending",
            "away_goals_ascending",
        ],
        "canonical_htft_code_order": list(HTFT_CODE_ORDER),
    }


def _derived_field_audits(probability_mode: str) -> dict[str, dict[str, Any]]:
    if probability_mode not in PROBABILITY_MODES:
        raise JointScenarioError("unsupported probability mode for derived-field audit")
    base_status = (
        "research_market_informed_reference"
        if probability_mode == UPSTREAM_HALF_TIME_ANCHOR_MODE
        else "model_probability_reference"
    )
    result: dict[str, dict[str, Any]] = {}
    for field in (
        "one_x_two",
        "total_goals_distribution",
        "goal_ranges",
        "btts",
        "full_time_score_marginal",
        "htft_marginal",
    ):
        result[field] = {
            "provenance": "validated_joint_cells",
            "probability_mode": probability_mode,
            "status": base_status,
            "recommendation_eligible": False,
            "template_fallback_allowed": False,
        }
    result["joint_top_two"] = {
        "provenance": "validated_joint_cells_probability_ranking",
        "probability_mode": probability_mode,
        "status": "high_variance_reference",
        "recommendation_eligible": False,
        "template_fallback_allowed": False,
    }
    return result


def build_feasible_joint_seed(
    half_time_matrix: Sequence[Sequence[float]],
    second_half_matrix: Sequence[Sequence[float]],
    *,
    full_home_goals_max: int,
    full_away_goals_max: int,
) -> tuple[list[list[list[float]]], dict[str, float]]:
    """Aggregate feasible HT-score + second-half paths by HT result and FT score."""

    half = _matrix_copy(half_time_matrix, "half_time_matrix")
    second = _matrix_copy(second_half_matrix, "second_half_matrix")
    if (
        isinstance(full_home_goals_max, bool)
        or not isinstance(full_home_goals_max, int)
        or full_home_goals_max < 0
        or isinstance(full_away_goals_max, bool)
        or not isinstance(full_away_goals_max, int)
        or full_away_goals_max < 0
    ):
        raise JointScenarioError("full-time score bounds must be non-negative integers")
    seed = [
        [
            [0.0 for _away in range(full_away_goals_max + 1)]
            for _home in range(full_home_goals_max + 1)
        ]
        for _half_result in RESULT_CODES
    ]
    code_index = {code: index for index, code in enumerate(RESULT_CODES)}
    retained_terms: list[float] = []
    for half_home, half_row in enumerate(half):
        for half_away, half_probability in enumerate(half_row):
            half_index = code_index[_result_code(half_home, half_away)]
            for second_home, second_row in enumerate(second):
                full_home = half_home + second_home
                for second_away, second_probability in enumerate(second_row):
                    full_away = half_away + second_away
                    path_probability = half_probability * second_probability
                    if (
                        full_home <= full_home_goals_max
                        and full_away <= full_away_goals_max
                    ):
                        seed[half_index][full_home][full_away] += path_probability
                        retained_terms.append(path_probability)
    retained = math.fsum(retained_terms)
    omitted = max(0.0, 1.0 - retained)
    if retained <= 0.0 or retained > 1.0 + 1e-9:
        raise JointScenarioError("feasible path seed has invalid retained mass")
    return seed, {
        "retained_probability": retained,
        "omitted_probability": omitted,
    }


def _joint_constraint_errors(
    aligned: Sequence[Sequence[Sequence[float]]],
    score_target: Sequence[Sequence[float]],
    htft_target: Mapping[str, float],
) -> tuple[float, float]:
    score_error = max(
        abs(
            math.fsum(aligned[index][home][away] for index in range(3))
            - score_target[home][away]
        )
        for home, row in enumerate(score_target)
        for away, _probability in enumerate(row)
    )
    htft_error = 0.0
    for half_index, half in enumerate(RESULT_CODES):
        for full in RESULT_CODES:
            actual = math.fsum(
                aligned[half_index][home][away]
                for home, row in enumerate(score_target)
                for away, _probability in enumerate(row)
                if _result_code(home, away) == full
            )
            htft_error = max(htft_error, abs(actual - htft_target[half + full]))
    return score_error, htft_error


def align_joint_distribution(
    seed: Sequence[Sequence[Sequence[float]]],
    full_time_score_matrix: Sequence[Sequence[float]],
    htft_probabilities: Mapping[str, float],
    *,
    tolerance: float = IPF_TOLERANCE,
    max_iterations: int = IPF_MAX_ITERATIONS,
) -> tuple[list[list[list[float]]], dict[str, Any]]:
    """Return the minimum-KL joint matching exact-score and HT/FT targets."""

    score_target = _matrix_copy(full_time_score_matrix, "full_time_score_matrix")
    htft_target = _validate_htft_probabilities(htft_probabilities)
    tolerance = _finite(tolerance, "tolerance")
    if not 0.0 < tolerance < 1.0:
        raise JointScenarioError("tolerance must be between zero and one")
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or max_iterations < 1
    ):
        raise JointScenarioError("max_iterations must be a positive integer")
    if not isinstance(seed, Sequence) or len(seed) != 3:
        raise JointScenarioError(
            "joint seed must contain three half-time-result planes"
        )
    home_count = len(score_target)
    away_count = len(score_target[0])
    aligned: list[list[list[float]]] = []
    for half_index, plane in enumerate(seed):
        if not isinstance(plane, Sequence) or len(plane) != home_count:
            raise JointScenarioError(
                "joint seed score bounds do not match target matrix"
            )
        normalized_plane: list[list[float]] = []
        for home, row in enumerate(plane):
            if not isinstance(row, Sequence) or len(row) != away_count:
                raise JointScenarioError(
                    "joint seed score bounds do not match target matrix"
                )
            normalized_row = [
                _finite(value, f"seed[{half_index}][{home}][{away}]")
                for away, value in enumerate(row)
            ]
            if any(value < 0.0 for value in normalized_row):
                raise JointScenarioError("joint seed probabilities cannot be negative")
            normalized_plane.append(normalized_row)
        aligned.append(normalized_plane)

    score_result_target = _score_result_marginal(score_target)
    htft_column_target = _htft_column_marginal(htft_target)
    if any(
        abs(score_result_target[code] - htft_column_target[code]) > tolerance
        for code in RESULT_CODES
    ):
        raise JointScenarioError(
            "canonical score and HT/FT full-time result marginals are inconsistent"
        )

    for iteration in range(1, max_iterations + 1):
        # Exact-score constraints.
        for home, row in enumerate(score_target):
            for away, target in enumerate(row):
                current = math.fsum(aligned[index][home][away] for index in range(3))
                if target <= 0.0:
                    for index in range(3):
                        aligned[index][home][away] = 0.0
                elif current <= 0.0:
                    raise JointScenarioError(
                        f"score {home}-{away} has positive target but no feasible path"
                    )
                else:
                    factor = target / current
                    for index in range(3):
                        aligned[index][home][away] *= factor

        # HT/FT 3x3 constraints.  Structural zero cells stay zero under scaling.
        for half_index, half in enumerate(RESULT_CODES):
            for full in RESULT_CODES:
                target = htft_target[half + full]
                cells = [
                    (home, away)
                    for home, row in enumerate(score_target)
                    for away, _probability in enumerate(row)
                    if _result_code(home, away) == full
                ]
                current = math.fsum(
                    aligned[half_index][home][away] for home, away in cells
                )
                if target <= 0.0:
                    for home, away in cells:
                        aligned[half_index][home][away] = 0.0
                elif current <= 0.0:
                    raise JointScenarioError(
                        f"HT/FT {half + full} has positive target but no feasible path"
                    )
                else:
                    factor = target / current
                    for home, away in cells:
                        aligned[half_index][home][away] *= factor

        score_error, htft_error = _joint_constraint_errors(
            aligned, score_target, htft_target
        )
        maximum_error = max(score_error, htft_error)
        if maximum_error <= tolerance:
            break
    else:
        raise JointScenarioError("joint-scenario IPF did not converge")

    total = math.fsum(
        probability for plane in aligned for row in plane for probability in row
    )
    if abs(total - 1.0) > tolerance:
        raise JointScenarioError("aligned joint distribution does not sum to one")
    return aligned, {
        "method": "aggregated_feasible_path_ipf_minimum_kl",
        "converged": True,
        "iterations": iteration,
        "tolerance": tolerance,
        "max_iterations": max_iterations,
        "maximum_constraint_error": maximum_error,
        "maximum_score_marginal_error": score_error,
        "maximum_htft_marginal_error": htft_error,
        "structural_zeros_preserved": True,
        "constraints": [
            "canonical_full_time_exact_score_matrix",
            "registered_htft_3x3_matrix",
        ],
    }


def _reconstruct_component_matrix(
    model: Mapping[str, Any],
    htft_prediction: Mapping[str, Any],
    component_name: str,
    *,
    home_team: str,
    away_team: str,
    unknown_team_policy: str,
) -> tuple[list[list[float]], dict[str, Any]]:
    component_audit = htft_prediction.get("components", {}).get(component_name)
    if not isinstance(component_audit, Mapping):
        raise JointScenarioError(f"HT/FT prediction omits {component_name} component")
    tail = component_audit.get("tail_mass")
    if not isinstance(tail, Mapping) or tail.get("tolerance_met") is not True:
        raise JointScenarioError(f"{component_name} tail audit is missing or unsafe")
    home_max = tail.get("truncated_at_home_goals")
    away_max = tail.get("truncated_at_away_goals")
    if (
        isinstance(home_max, bool)
        or not isinstance(home_max, int)
        or isinstance(away_max, bool)
        or not isinstance(away_max, int)
        or home_max < 0
        or away_max != home_max
    ):
        raise JointScenarioError(
            f"{component_name} component requires equal non-negative score bounds"
        )
    tolerance = _finite(tail.get("tolerance"), f"{component_name} tail tolerance")
    try:
        output = htft_model._component_prediction(
            model["components"][component_name],
            home_team,
            away_team,
            max_goals=home_max,
            hard_max_goals=home_max,
            tail_tolerance=tolerance,
            allow_large_tail=False,
            unknown_team_policy=unknown_team_policy,
        )
    except (KeyError, TypeError, htft_model.HTFTModelError) as exc:
        raise JointScenarioError(
            f"cannot reconstruct {component_name} score component: {exc}"
        ) from exc
    if output.get("score_matrix_hash") != component_audit.get("score_matrix_hash"):
        raise JointScenarioError(
            f"reconstructed {component_name} matrix hash does not match prediction"
        )
    return _matrix_copy(output["matrix"], f"{component_name} matrix"), copy.deepcopy(
        output["tail_mass"]
    )


def _normalize_market_evidence(
    evidence: Mapping[str, Any] | None,
    *,
    generated_at: datetime,
    kickoff: datetime,
    fixture: Mapping[str, Any],
    expected_match_id: Any = None,
) -> dict[str, Any]:
    normalized_expected_id = _normalize_match_id(expected_match_id, "expected_match_id")
    evidence_match_id = _evidence_match_id(evidence)
    if (
        normalized_expected_id is not None
        and evidence_match_id is not None
        and normalized_expected_id != evidence_match_id
    ):
        raise JointScenarioError(
            "market evidence match_id does not match expected_match_id"
        )
    bound_match_id = normalized_expected_id or evidence_match_id
    joint_identity = _fixture_identity(fixture)
    joint_identity["match_id"] = bound_match_id
    fixture_binding: dict[str, Any] = {
        "status": "no_evidence",
        "joint_fixture_hash": content_hash(joint_identity),
        "bound_match_id": bound_match_id,
        "evidence_match_id": evidence_match_id,
        "fields_checked": [],
        "evidence_fixture": None,
    }
    if evidence is None:
        return {
            "provided": False,
            "role": "attached_diagnostic_bundle",
            "input_format": "none",
            "source": None,
            "sources": None,
            "captured_at": None,
            "quality": "unavailable",
            "effective_quality": "unavailable",
            "quality_detail": None,
            "limitations": [],
            "bundle_validation_status": "no_evidence",
            "bundle_validation_errors": [],
            "fixture_binding": fixture_binding,
            "content_hash": None,
            "payload": None,
            "diagnostic_only": True,
            "used_for_probability": False,
            "conditioning_weight": 0.0,
            "ev_comparison_eligible": False,
            "recommendation_eligible": False,
            "same_price_independent_ev_authorized": False,
        }
    if not isinstance(evidence, Mapping):
        raise JointScenarioError("market evidence must be a JSON object")
    _validate_market_evidence_payload_limits(evidence)
    forbidden_key = _has_forbidden_market_claim_key(evidence)
    if forbidden_key is not None:
        raise JointScenarioError(
            f"market evidence top-level claim field is forbidden: {forbidden_key}"
        )
    # Two input contracts are supported.  The compact contract is convenient
    # for hand-authored evidence; the structured bundle is what the current
    # evidence collector emits.  Both remain diagnostic-only in this schema.
    quality_payload = evidence.get("quality")
    structured = "sources" in evidence or isinstance(quality_payload, Mapping)
    if structured:
        sources = evidence.get("sources")
        if isinstance(sources, Mapping):
            if not sources or any(not isinstance(key, str) for key in sources):
                raise JointScenarioError(
                    "market evidence.sources object needs string source names"
                )
        elif isinstance(sources, Sequence) and not isinstance(
            sources, (str, bytes, bytearray)
        ):
            if not sources:
                raise JointScenarioError("market evidence.sources cannot be empty")
        else:
            raise JointScenarioError(
                "market evidence.sources must be a non-empty array or object"
            )
        if not isinstance(quality_payload, Mapping):
            raise JointScenarioError(
                "structured market evidence.quality must be an object"
            )
        quality = quality_payload.get("status")
        if not isinstance(quality, str) or not quality.strip():
            raise JointScenarioError(
                "structured market evidence.quality.status is required"
            )
        input_format = "structured_bundle"
        source = "structured_bundle"
        normalized_sources: Any = copy.deepcopy(sources)
        quality_detail: Any = copy.deepcopy(dict(quality_payload))
        quality = quality.strip()
    else:
        source = evidence.get("source")
        quality = quality_payload
        if not isinstance(source, str) or not source.strip():
            raise JointScenarioError("market evidence.source is required")
        if quality not in VALID_EVIDENCE_QUALITIES:
            raise JointScenarioError(
                "market evidence.quality must be high, medium, low, or unknown"
            )
        input_format = "flat"
        source = source.strip()
        normalized_sources = None
        quality_detail = None
    limitations: list[str] = []
    if "fixture" in evidence:
        normalized_evidence_fixture = _validate_evidence_fixture(
            evidence.get("fixture"), joint_identity
        )
        fixture_binding.update(
            {
                "status": "verified",
                "fields_checked": [
                    "competition_key",
                    "home_team",
                    "away_team",
                    "kickoff",
                ],
                "evidence_fixture": normalized_evidence_fixture,
            }
        )
    else:
        limitations.append("market_evidence_fixture_missing")
        fixture_binding["status"] = "unverified_missing_fixture"
    bundle_errors = _current_market_bundle_errors(evidence)
    if limitations:
        bundle_errors.append("market evidence fixture binding is incomplete")
    bundle_valid = not bundle_errors
    captured_at, canonical_captured_at = _parse_aware_datetime(
        evidence.get("captured_at"), "market evidence.captured_at"
    )
    if captured_at > generated_at:
        raise JointScenarioError(
            "market evidence.captured_at cannot be after generated_at"
        )
    if captured_at >= kickoff:
        raise JointScenarioError(
            "market evidence.captured_at must be strictly before kickoff"
        )
    return {
        "provided": True,
        "role": "attached_diagnostic_bundle",
        "input_format": input_format,
        "source": source,
        "sources": normalized_sources,
        "captured_at": canonical_captured_at,
        "quality": str(quality),
        "effective_quality": str(quality) if bundle_valid else "audit_unverified",
        "quality_detail": quality_detail,
        "limitations": limitations,
        "bundle_validation_status": (
            "validated_current_schema" if bundle_valid else "audit_unverified"
        ),
        "bundle_validation_errors": bundle_errors,
        "fixture_binding": fixture_binding,
        "content_hash": content_hash(evidence),
        "payload": copy.deepcopy(dict(evidence)),
        "diagnostic_only": True,
        "used_for_probability": False,
        "conditioning_weight": 0.0,
        "ev_comparison_eligible": False,
        "recommendation_eligible": False,
        "same_price_independent_ev_authorized": False,
    }


def _total_goals_distribution(matrix: Sequence[Sequence[float]]) -> dict[str, float]:
    maximum = len(matrix) - 1 + len(matrix[0]) - 1
    return {
        str(total): math.fsum(
            probability
            for home, row in enumerate(matrix)
            for away, probability in enumerate(row)
            if home + away == total
        )
        for total in range(maximum + 1)
    }


def _derived_full_time_fields(matrix: Sequence[Sequence[float]]) -> dict[str, Any]:
    return {
        "one_x_two": score_model.aggregate_one_x_two(matrix),
        "total_goals_distribution": _total_goals_distribution(matrix),
        "goal_ranges": score_model.aggregate_goal_ranges(matrix),
        "btts": score_model.aggregate_btts(matrix),
    }


def _joint_cells(
    seed: Sequence[Sequence[Sequence[float]]],
    aligned: Sequence[Sequence[Sequence[float]]],
    *,
    seed_mass: float,
) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    home_count = len(aligned[0])
    away_count = len(aligned[0][0])
    half_index = {code: index for index, code in enumerate(RESULT_CODES)}
    for code in HTFT_CODE_ORDER:
        half, full = code
        index = half_index[half]
        for home in range(home_count):
            for away in range(away_count):
                score_full = _result_code(home, away)
                raw_seed = seed[index][home][away] if score_full == full else 0.0
                probability = aligned[index][home][away] if score_full == full else 0.0
                cells.append(
                    {
                        "htft": code,
                        "score": f"{home}-{away}",
                        "home_goals": home,
                        "away_goals": away,
                        "structurally_feasible": raw_seed > 0.0,
                        "prior_probability": raw_seed / seed_mass
                        if raw_seed > 0.0
                        else 0.0,
                        "probability": probability,
                    }
                )
    return cells


def _top_two(cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    code_index = {code: index for index, code in enumerate(HTFT_CODE_ORDER)}
    ranked = sorted(
        (cell for cell in cells if float(cell["probability"]) > 0.0),
        key=lambda cell: (
            -float(cell["probability"]),
            code_index[str(cell["htft"])],
            int(cell["home_goals"]),
            int(cell["away_goals"]),
        ),
    )
    if len(ranked) < 2:
        raise JointScenarioError("joint distribution has fewer than two positive cells")
    return [
        {
            "slot": slot,
            "htft": str(cell["htft"]),
            "score": str(cell["score"]),
            "home_goals": int(cell["home_goals"]),
            "away_goals": int(cell["away_goals"]),
            "probability": float(cell["probability"]),
            "status": "high_variance_reference",
            "recommendation_eligible": False,
            "counts_toward_primary_record": False,
            "odds_available": False,
        }
        for slot, cell in enumerate(ranked[:2], start=1)
    ]


def _top_two_from_event_planes(
    planes: Sequence[Sequence[Sequence[float]]],
) -> list[dict[str, Any]]:
    """Rank the compact kernel's reconstructed HT-result x FT-score planes."""

    if not isinstance(planes, Sequence) or len(planes) != len(RESULT_CODES):
        raise JointScenarioError("event planes must contain H/D/A half-result planes")
    ranked: list[tuple[float, int, int, int, str]] = []
    shape: tuple[int, int] | None = None
    total_terms: list[float] = []
    for half_index, raw_plane in enumerate(planes):
        if not isinstance(raw_plane, Sequence) or not raw_plane:
            raise JointScenarioError(f"event_planes[{half_index}] is invalid")
        plane: list[list[float]] = []
        for home, raw_row in enumerate(raw_plane):
            if not isinstance(raw_row, Sequence) or not raw_row:
                raise JointScenarioError(
                    f"event_planes[{half_index}][{home}] is invalid"
                )
            row = [
                _finite(value, f"event_planes[{half_index}][{home}][{away}]")
                for away, value in enumerate(raw_row)
            ]
            if any(value < 0.0 for value in row):
                raise JointScenarioError("event plane probabilities cannot be negative")
            plane.append(row)
            total_terms.extend(row)
        plane_shape = (len(plane), len(plane[0]))
        if any(len(row) != plane_shape[1] for row in plane):
            raise JointScenarioError("event planes must be rectangular")
        if shape is None:
            shape = plane_shape
        elif plane_shape != shape:
            raise JointScenarioError("event planes must share one score grid")
        for home, row in enumerate(plane):
            for away, probability in enumerate(row):
                if probability <= 0.0:
                    continue
                full_result = _result_code(home, away)
                ranked.append(
                    (
                        probability,
                        HTFT_CODE_ORDER.index(RESULT_CODES[half_index] + full_result),
                        home,
                        away,
                        RESULT_CODES[half_index] + full_result,
                    )
                )
    if abs(math.fsum(total_terms) - 1.0) > 1e-9:
        raise JointScenarioError("event plane probabilities must sum to one")
    ranked.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    if len(ranked) < 2:
        raise JointScenarioError("joint distribution has fewer than two positive cells")
    return [
        {
            "slot": slot,
            "htft": code,
            "score": f"{home}-{away}",
            "home_goals": home,
            "away_goals": away,
            "probability": probability,
            "status": "high_variance_reference",
            "recommendation_eligible": False,
            "counts_toward_primary_record": False,
            "odds_available": False,
        }
        for slot, (probability, _code_index, home, away, code) in enumerate(
            ranked[:2], start=1
        )
    ]


def _score_marginal_payload(matrix: Sequence[Sequence[float]]) -> dict[str, Any]:
    copied = _matrix_copy(matrix, "score marginal")
    return {
        "home_goals_max": len(copied) - 1,
        "away_goals_max": len(copied[0]) - 1,
        "probabilities": copied,
    }


def _validate_score_marginal_payload(value: Any, name: str) -> list[list[float]]:
    if not isinstance(value, Mapping):
        raise JointScenarioError(f"{name} is missing")
    matrix = _matrix_copy(value.get("probabilities"), f"{name}.probabilities")
    if (
        value.get("home_goals_max") != len(matrix) - 1
        or value.get("away_goals_max") != len(matrix[0]) - 1
    ):
        raise JointScenarioError(f"{name} bounds are inconsistent")
    return matrix


def _support_feasibility_binding(audit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "method": audit.get("method"),
        "performed_before_ipf": True,
        "feasible": audit.get("feasible"),
        "subset_count_per_block": audit.get("subset_count_per_block"),
        "minimum_subset_slack_probability": audit.get(
            "minimum_subset_slack_probability"
        ),
        "full_audit_hash": content_hash(audit),
    }


def _marginals_from_cells(
    cells: Sequence[Mapping[str, Any]],
    *,
    home_goals_max: int,
    away_goals_max: int,
) -> tuple[list[list[float]], dict[str, float]]:
    score_matrix = [
        [0.0 for _away in range(away_goals_max + 1)]
        for _home in range(home_goals_max + 1)
    ]
    htft = {code: 0.0 for code in HTFT_CODE_ORDER}
    seen: set[tuple[str, int, int]] = set()
    for index, cell in enumerate(cells):
        if not isinstance(cell, Mapping):
            raise JointScenarioError(f"joint_cells[{index}] must be an object")
        code = cell.get("htft")
        home = cell.get("home_goals")
        away = cell.get("away_goals")
        score = cell.get("score")
        if code not in HTFT_CODE_ORDER:
            raise JointScenarioError(f"joint_cells[{index}].htft is invalid")
        if (
            isinstance(home, bool)
            or not isinstance(home, int)
            or not 0 <= home <= home_goals_max
            or isinstance(away, bool)
            or not isinstance(away, int)
            or not 0 <= away <= away_goals_max
            or score != f"{home}-{away}"
        ):
            raise JointScenarioError(f"joint_cells[{index}] score identity is invalid")
        identity = (str(code), home, away)
        if identity in seen:
            raise JointScenarioError("joint_cells contain duplicate identities")
        seen.add(identity)
        probability = _finite(
            cell.get("probability"), f"joint_cells[{index}].probability"
        )
        prior = _finite(
            cell.get("prior_probability"),
            f"joint_cells[{index}].prior_probability",
        )
        if probability < 0.0 or prior < 0.0:
            raise JointScenarioError("joint cell probabilities cannot be negative")
        feasible = cell.get("structurally_feasible")
        if not isinstance(feasible, bool):
            raise JointScenarioError(
                f"joint_cells[{index}].structurally_feasible must be boolean"
            )
        logically_compatible = str(code)[1] == _result_code(home, away)
        if not logically_compatible and (feasible or probability > 0.0 or prior > 0.0):
            raise JointScenarioError(
                "a joint cell whose HT/FT terminal result conflicts with its score must be zero"
            )
        if feasible is not (prior > 0.0):
            raise JointScenarioError(
                "joint cell feasibility must agree with positive path-prior support"
            )
        if not feasible and probability > 0.0:
            raise JointScenarioError(
                "structurally impossible joint paths must remain zero"
            )
        score_matrix[home][away] += probability
        htft[str(code)] += probability
    expected_count = len(HTFT_CODE_ORDER) * (home_goals_max + 1) * (away_goals_max + 1)
    if len(seen) != expected_count:
        raise JointScenarioError("joint_cells are not an exhaustive HT/FT x score grid")
    if abs(math.fsum(htft.values()) - 1.0) > 1e-9:
        raise JointScenarioError("joint cell probabilities must sum to one")
    prior_total = math.fsum(float(cell["prior_probability"]) for cell in cells)
    if abs(prior_total - 1.0) > 1e-9:
        raise JointScenarioError("joint path-prior probabilities must sum to one")
    return score_matrix, htft


def _aligned_planes_from_cells(
    cells: Sequence[Mapping[str, Any]],
    *,
    home_goals_max: int,
    away_goals_max: int,
    field: str,
) -> list[list[list[float]]]:
    planes = [
        [
            [0.0 for _away in range(away_goals_max + 1)]
            for _home in range(home_goals_max + 1)
        ]
        for _half in RESULT_CODES
    ]
    half_index = {code: index for index, code in enumerate(RESULT_CODES)}
    for cell in cells:
        code = str(cell["htft"])
        home = int(cell["home_goals"])
        away = int(cell["away_goals"])
        if code[1] == _result_code(home, away):
            planes[half_index[code[0]]][home][away] = float(cell[field])
    return planes


def _validate_registered_inputs(
    model: Mapping[str, Any],
    score_prediction: Mapping[str, Any],
    htft_prediction: Mapping[str, Any],
    *,
    generated_at: Any,
) -> dict[str, Any]:
    try:
        htft_model.validate_model(model)
        htft_model.validate_prediction(htft_prediction, model=model)
        league_model_manager.validate_score_prediction(score_prediction)
    except (
        htft_model.HTFTModelError,
        league_model_manager.LeagueModelManagerError,
    ) as exc:
        raise JointScenarioError(f"invalid registered input: {exc}") from exc
    training = model.get("training")
    if (
        not isinstance(training, Mapping)
        or not isinstance(training.get("dataset_manifest_hash"), str)
        or not HASH_RE.fullmatch(str(training.get("dataset_manifest_hash")))
    ):
        raise JointScenarioError(
            "registered HT/FT model requires a dataset_manifest_hash binding"
        )
    full_component = model.get("components", {}).get("full_time")
    if not isinstance(full_component, Mapping) or score_prediction.get(
        "model_hash"
    ) != full_component.get("model_hash"):
        raise JointScenarioError(
            "canonical score prediction is not bound to the HT/FT full-time component"
        )
    htft_fixture = htft_prediction.get("fixture")
    score_fixture = score_prediction.get("fixture")
    if not isinstance(htft_fixture, Mapping) or not isinstance(score_fixture, Mapping):
        raise JointScenarioError("prediction fixtures are missing")
    for field in ("home_team", "away_team", "unknown_team_policy"):
        if htft_fixture.get(field) != score_fixture.get(field):
            raise JointScenarioError(f"input fixture {field} values do not match")
    kickoff, canonical_kickoff = _parse_aware_datetime(
        htft_fixture.get("kickoff"), "fixture.kickoff"
    )
    score_kickoff, _ = _parse_aware_datetime(
        score_fixture.get("kickoff"), "canonical score fixture.kickoff"
    )
    if score_kickoff != kickoff:
        raise JointScenarioError("input fixture kickoff values do not match")
    generation, canonical_generated_at = _parse_aware_datetime(
        generated_at, "generated_at"
    )
    if generation >= kickoff:
        raise JointScenarioError("generated_at must be strictly before kickoff")
    for label, prediction in (
        ("canonical score", score_prediction),
        ("HT/FT", htft_prediction),
    ):
        input_generation, _ = _parse_aware_datetime(
            prediction.get("generated_at"), f"{label} generated_at"
        )
        if input_generation > generation:
            raise JointScenarioError(
                f"generated_at cannot precede the {label} prediction"
            )
    probability_mode, external_anchor_audit = _resolve_external_anchor_mode(
        htft_prediction,
        generated_at=generation,
        kickoff=kickoff,
    )
    competition_key = htft_fixture.get("competition_key")
    if competition_key != training.get("competition_key"):
        raise JointScenarioError(
            "HT/FT fixture competition_key does not match registered model"
        )
    return {
        "generation": generation,
        "generated_at": canonical_generated_at,
        "kickoff": kickoff,
        "canonical_kickoff": canonical_kickoff,
        "probability_mode": probability_mode,
        "external_anchor_audit": external_anchor_audit,
        "fixture": {
            "home_team": str(htft_fixture["home_team"]),
            "away_team": str(htft_fixture["away_team"]),
            "kickoff": canonical_kickoff,
            "competition_key": str(competition_key),
            "unknown_team_policy": str(htft_fixture["unknown_team_policy"]),
        },
    }


def _build_prediction(
    model: Mapping[str, Any],
    score_prediction: Mapping[str, Any],
    htft_prediction: Mapping[str, Any],
    *,
    generated_at: Any,
    market_evidence: Mapping[str, Any] | None,
    expected_match_id: Any = None,
) -> dict[str, Any]:
    context = _validate_registered_inputs(
        model,
        score_prediction,
        htft_prediction,
        generated_at=generated_at,
    )
    fixture = copy.deepcopy(context["fixture"])
    probability_mode = str(context["probability_mode"])
    anchored = probability_mode == UPSTREAM_HALF_TIME_ANCHOR_MODE
    evidence = _normalize_market_evidence(
        market_evidence,
        generated_at=context["generation"],
        kickoff=context["kickoff"],
        fixture=fixture,
        expected_match_id=expected_match_id,
    )
    fixture["match_id"] = evidence["fixture_binding"]["bound_match_id"]
    score_payload = score_prediction.get("score_matrix")
    if not isinstance(score_payload, Mapping):
        raise JointScenarioError("canonical score prediction has no score_matrix")
    score_matrix = _matrix_copy(
        score_payload.get("probabilities"), "canonical score matrix"
    )
    if (
        score_payload.get("home_goals_max") != len(score_matrix) - 1
        or score_payload.get("away_goals_max") != len(score_matrix[0]) - 1
    ):
        raise JointScenarioError("canonical score matrix bounds are inconsistent")
    htft_payload = htft_prediction.get("htft")
    if not isinstance(htft_payload, Mapping):
        raise JointScenarioError("HT/FT prediction has no htft payload")
    htft_probabilities = _validate_htft_probabilities(
        htft_payload.get("code_probabilities")
    )
    half_matrix, half_tail = _reconstruct_component_matrix(
        model,
        htft_prediction,
        "half_time",
        home_team=fixture["home_team"],
        away_team=fixture["away_team"],
        unknown_team_policy=fixture["unknown_team_policy"],
    )
    second_matrix, second_tail = _reconstruct_component_matrix(
        model,
        htft_prediction,
        "second_half",
        home_team=fixture["home_team"],
        away_team=fixture["away_team"],
        unknown_team_policy=fixture["unknown_team_policy"],
    )
    # Canonicalize inputs and prove fractional Hall support before the IPF
    # solver is allowed to run.  This pre-solver kernel uses deterministic
    # transport only for the audit; its posterior is never published.
    try:
        feasibility_kernel = joint_path_kernel.build_compact_kernel(
            half_matrix,
            second_matrix,
            score_matrix,
            htft_target=htft_probabilities,
            half_raw_omitted=float(half_tail["raw_omitted_probability"]),
            second_raw_omitted=float(second_tail["raw_omitted_probability"]),
        )
    except joint_path_kernel.PathKernelError as exc:
        raise JointScenarioError(
            f"path support feasibility audit failed: {exc}"
        ) from exc
    pre_ipf_feasibility = feasibility_kernel["hall_audit"]
    if pre_ipf_feasibility.get("feasible") is not True:
        raise JointScenarioError(
            "canonical score and HT/FT targets fail fractional Hall support feasibility"
        )
    half_matrix = feasibility_kernel["components"]["half_time"][
        "conditional_score_matrix"
    ]
    second_matrix = feasibility_kernel["components"]["second_half"][
        "conditional_score_matrix"
    ]
    score_matrix = feasibility_kernel["targets"]["full_score"]
    htft_probabilities = feasibility_kernel["targets"]["htft"]
    seed, seed_tail = build_feasible_joint_seed(
        half_matrix,
        second_matrix,
        full_home_goals_max=len(score_matrix) - 1,
        full_away_goals_max=len(score_matrix[0]) - 1,
    )
    seed_tail.update(
        {
            "tolerance": PATH_TAIL_TOLERANCE,
            "tolerance_met": (seed_tail["omitted_probability"] <= PATH_TAIL_TOLERANCE),
        }
    )
    if seed_tail["tolerance_met"] is not True:
        raise JointScenarioError(
            "feasible path convolution omitted mass exceeds the production tolerance"
        )
    aligned, solver = align_joint_distribution(
        seed,
        score_matrix,
        htft_probabilities,
        tolerance=IPF_TOLERANCE,
        max_iterations=IPF_MAX_ITERATIONS,
    )
    try:
        path_kernel = joint_path_kernel.build_compact_kernel(
            half_matrix,
            second_matrix,
            score_matrix,
            htft_target=htft_probabilities,
            aligned_event_planes=aligned,
            half_raw_omitted=float(half_tail["raw_omitted_probability"]),
            second_raw_omitted=float(second_tail["raw_omitted_probability"]),
        )
        reconstructed = joint_path_kernel.validate_kernel(path_kernel)
    except joint_path_kernel.PathKernelError as exc:
        raise JointScenarioError(f"compact path kernel is invalid: {exc}") from exc
    if content_hash(path_kernel["hall_audit"]) != content_hash(pre_ipf_feasibility):
        raise JointScenarioError(
            "post-IPF path kernel does not preserve the pre-IPF feasibility audit"
        )
    aligned_score = reconstructed["full_score_marginal"]
    aligned_htft = reconstructed["htft_marginal"]
    top_two = _top_two_from_event_planes(reconstructed["event_planes"])
    prediction: dict[str, Any] = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at": context["generated_at"],
        "fixture": fixture,
        "probability_mode": probability_mode,
        "market_conditioning_enabled": anchored,
        "upstream_anchor_conditioning_enabled": anchored,
        "attached_market_evidence_conditioning_enabled": False,
        "research_market_informed": anchored,
        "formal_eligible": False,
        "external_anchor_audit": copy.deepcopy(context["external_anchor_audit"]),
        "policy": _policy_for_mode(probability_mode),
        "inputs": {
            "fixture_identity": {
                "content_hash": content_hash(_fixture_identity(fixture)),
                "match_id": fixture["match_id"],
            },
            "registered_htft_model": {
                "content_hash": content_hash(model),
                "model_hash": model.get("model_hash"),
                "dataset_manifest_hash": model["training"]["dataset_manifest_hash"],
            },
            "canonical_score_prediction": {
                "content_hash": content_hash(score_prediction),
                "model_hash": score_prediction.get("model_hash"),
            },
            "htft_prediction": {
                "content_hash": content_hash(htft_prediction),
                "prediction_hash": htft_prediction.get("prediction_hash"),
                "model_hash": htft_prediction.get("model_hash"),
            },
            "market_evidence": {
                "content_hash": evidence["content_hash"],
            },
        },
        "market_evidence": evidence,
        "solver": solver,
        "support_feasibility_audit": _support_feasibility_binding(pre_ipf_feasibility),
        "path_kernel": path_kernel,
        "tail_mass": {
            "canonical_full_time": copy.deepcopy(score_prediction.get("tail_mass")),
            "half_time_component": half_tail,
            "second_half_component": second_tail,
            "feasible_path_convolution": seed_tail,
        },
        "full_time_score_marginal": {
            "home_goals_max": len(aligned_score) - 1,
            "away_goals_max": len(aligned_score[0]) - 1,
            "probabilities": aligned_score,
        },
        "half_time_score_marginal": _score_marginal_payload(
            reconstructed["half_time_score_marginal"]
        ),
        "second_half_score_marginal": _score_marginal_payload(
            reconstructed["second_half_score_marginal"]
        ),
        "htft_marginal": {
            "class_order": list(HTFT_CODE_ORDER),
            "code_probabilities": aligned_htft,
            "half_time_result_probabilities": _htft_row_marginal(aligned_htft),
            "full_time_result_probabilities": _htft_column_marginal(aligned_htft),
        },
        "joint_top_two": top_two,
        "joint_top_two_probability_mass": math.fsum(
            item["probability"] for item in top_two
        ),
        "derived": _derived_full_time_fields(aligned_score),
        "derived_field_audits": _derived_field_audits(probability_mode),
    }
    prediction["prediction_hash"] = calculate_prediction_hash(prediction)
    return prediction


def predict_joint_scenarios(
    model: Mapping[str, Any],
    score_prediction: Mapping[str, Any],
    htft_prediction: Mapping[str, Any],
    *,
    generated_at: Any,
    market_evidence: Mapping[str, Any] | None = None,
    expected_match_id: Any = None,
) -> dict[str, Any]:
    """Create and self-validate one joint scenario prediction."""

    prediction = _build_prediction(
        model,
        score_prediction,
        htft_prediction,
        generated_at=generated_at,
        market_evidence=market_evidence,
        expected_match_id=expected_match_id,
    )
    validate_prediction(prediction)
    return prediction


def _validate_external_anchor_artifact(
    audit: Any,
    *,
    probability_mode: str,
    generated_at: datetime,
    kickoff: datetime,
    htft_input_binding: Mapping[str, Any],
) -> dict[str, float] | None:
    if not isinstance(audit, Mapping):
        raise JointScenarioError("external_anchor_audit is missing")
    expected_keys = {
        "enabled",
        "role",
        "component",
        "origin",
        "source",
        "captured_at",
        "upstream_prediction_generated_at",
        "de_vigged",
        "probabilities",
        "target_hash",
        "upstream_prediction_hash",
        "used_for_probability",
        "same_price_independent_ev_authorized",
    }
    if set(audit) != expected_keys:
        raise JointScenarioError("external_anchor_audit fields are invalid")
    upstream_generated, canonical_upstream_generated = _parse_aware_datetime(
        audit.get("upstream_prediction_generated_at"),
        "external_anchor_audit.upstream_prediction_generated_at",
    )
    if audit.get("upstream_prediction_generated_at") != canonical_upstream_generated:
        raise JointScenarioError(
            "external anchor upstream prediction time must use canonical UTC"
        )
    if upstream_generated > generated_at or upstream_generated >= kickoff:
        raise JointScenarioError(
            "external anchor upstream prediction timing is invalid"
        )
    if audit.get("upstream_prediction_hash") != htft_input_binding.get(
        "prediction_hash"
    ):
        raise JointScenarioError("external anchor HT/FT prediction hash does not match")
    if audit.get("same_price_independent_ev_authorized") is not False:
        raise JointScenarioError(
            "external anchor price cannot authorize independent EV"
        )
    if probability_mode == MODEL_ONLY_MODE:
        expected = {
            "enabled": False,
            "role": "no_external_probability_input",
            "component": None,
            "origin": None,
            "source": None,
            "captured_at": None,
            "upstream_prediction_generated_at": canonical_upstream_generated,
            "de_vigged": None,
            "probabilities": None,
            "target_hash": None,
            "upstream_prediction_hash": audit.get("upstream_prediction_hash"),
            "used_for_probability": False,
            "same_price_independent_ev_authorized": False,
        }
        if _canonical_bytes(audit) != _canonical_bytes(expected):
            raise JointScenarioError("model-only external anchor audit is malformed")
        return None
    if probability_mode != UPSTREAM_HALF_TIME_ANCHOR_MODE:
        raise JointScenarioError(
            "unsupported probability mode for external anchor audit"
        )
    if (
        audit.get("enabled") is not True
        or audit.get("role") != "upstream_probability_input"
        or audit.get("component") != "half_time"
        or audit.get("origin") != "external_de_vigged_anchor"
        or audit.get("de_vigged") is not True
        or audit.get("used_for_probability") is not True
    ):
        raise JointScenarioError("upstream half-time anchor audit is malformed")
    source = audit.get("source")
    if not isinstance(source, str) or not source.strip() or source != source.strip():
        raise JointScenarioError("external anchor source is invalid")
    captured_at, canonical_captured_at = _parse_aware_datetime(
        audit.get("captured_at"), "external_anchor_audit.captured_at"
    )
    if audit.get("captured_at") != canonical_captured_at:
        raise JointScenarioError("external anchor captured_at must use canonical UTC")
    if (
        captured_at > upstream_generated
        or captured_at > generated_at
        or captured_at >= kickoff
    ):
        raise JointScenarioError("external half-time anchor timing is invalid")
    probabilities = _validate_result_probabilities(
        audit.get("probabilities"), "external_anchor_audit.probabilities"
    )
    target_material = {
        "origin": "external_de_vigged_anchor",
        "source": source,
        "captured_at": canonical_captured_at,
        "de_vigged": True,
        "probabilities": probabilities,
    }
    if audit.get("target_hash") != content_hash(target_material):
        raise JointScenarioError("external anchor target hash does not reproduce")
    return {
        "H": probabilities["home"],
        "D": probabilities["draw"],
        "A": probabilities["away"],
    }


def validate_prediction(payload: Mapping[str, Any]) -> None:
    """Recalculate a joint artifact using its embedded path representation.

    This self-contained validator is intended for the renderer and memory store;
    neither consumer needs to reopen the registered model.  Generation-time
    input hashes remain available for a higher-level bundle validator.  Frozen
    1.1.0 artifacts retain their original exhaustive-cell validation path;
    newly generated 2.0.0 artifacts use the compact four-axis path kernel.
    """

    if not isinstance(payload, Mapping):
        raise JointScenarioError("joint scenario prediction must be a JSON object")
    if payload.get("artifact_type") != ARTIFACT_TYPE:
        raise JointScenarioError("unexpected joint scenario artifact_type")
    version_pair = (payload.get("schema_version"), payload.get("model_version"))
    if version_pair not in SUPPORTED_VERSION_PAIRS:
        raise JointScenarioError("unsupported joint scenario schema/model version pair")
    legacy_cells = version_pair == (LEGACY_SCHEMA_VERSION, LEGACY_MODEL_VERSION)
    stored_hash = payload.get("prediction_hash")
    if not isinstance(stored_hash, str) or not HASH_RE.fullmatch(stored_hash):
        raise JointScenarioError("prediction_hash must be a SHA-256 hash")
    if stored_hash != calculate_prediction_hash(payload):
        raise JointScenarioError("prediction_hash does not match prediction contents")
    generated_at, canonical_generated_at = _parse_aware_datetime(
        payload.get("generated_at"), "generated_at"
    )
    if payload.get("generated_at") != canonical_generated_at:
        raise JointScenarioError("generated_at must use canonical UTC representation")
    fixture = payload.get("fixture")
    if not isinstance(fixture, Mapping):
        raise JointScenarioError("fixture is missing")
    kickoff, canonical_kickoff = _parse_aware_datetime(
        fixture.get("kickoff"), "fixture.kickoff"
    )
    if fixture.get("kickoff") != canonical_kickoff:
        raise JointScenarioError(
            "fixture.kickoff must use canonical UTC representation"
        )
    if generated_at >= kickoff:
        raise JointScenarioError("generated_at must be strictly before kickoff")
    for field in ("home_team", "away_team", "competition_key", "unknown_team_policy"):
        if not isinstance(fixture.get(field), str) or not str(fixture[field]).strip():
            raise JointScenarioError(f"fixture.{field} is required")
    if fixture["home_team"] == fixture["away_team"]:
        raise JointScenarioError("fixture teams must differ")
    fixture_match_id = _normalize_match_id(fixture.get("match_id"), "fixture.match_id")
    if fixture.get("match_id") != fixture_match_id:
        raise JointScenarioError("fixture.match_id must use its canonical string form")
    probability_mode = payload.get("probability_mode")
    if probability_mode not in PROBABILITY_MODES:
        raise JointScenarioError("joint scenario probability mode is unsupported")
    anchored = probability_mode == UPSTREAM_HALF_TIME_ANCHOR_MODE
    if (
        payload.get("market_conditioning_enabled") is not anchored
        or payload.get("upstream_anchor_conditioning_enabled") is not anchored
        or payload.get("attached_market_evidence_conditioning_enabled") is not False
        or payload.get("research_market_informed") is not anchored
        or payload.get("formal_eligible") is not False
    ):
        raise JointScenarioError("joint scenario probability/deployment mode is unsafe")
    policy = payload.get("policy")
    if not isinstance(policy, Mapping) or _canonical_bytes(policy) != _canonical_bytes(
        _policy_for_mode(str(probability_mode))
    ):
        raise JointScenarioError("joint scenario policy is invalid")
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        raise JointScenarioError("input hash bindings are missing")
    for name in (
        "fixture_identity",
        "registered_htft_model",
        "canonical_score_prediction",
        "htft_prediction",
        "market_evidence",
    ):
        binding = inputs.get(name)
        if not isinstance(binding, Mapping):
            raise JointScenarioError(f"inputs.{name} binding is missing")
        for key, value in binding.items():
            if (
                value is not None
                and key.endswith("hash")
                and (not isinstance(value, str) or not HASH_RE.fullmatch(value))
            ):
                raise JointScenarioError(f"inputs.{name}.{key} must be a SHA-256 hash")
    expected_fixture_hash = content_hash(_fixture_identity(fixture))
    if (
        inputs["fixture_identity"].get("content_hash") != expected_fixture_hash
        or inputs["fixture_identity"].get("match_id") != fixture_match_id
    ):
        raise JointScenarioError("fixture identity hash binding does not reproduce")
    anchor_half_marginal = _validate_external_anchor_artifact(
        payload.get("external_anchor_audit"),
        probability_mode=str(probability_mode),
        generated_at=generated_at,
        kickoff=kickoff,
        htft_input_binding=inputs["htft_prediction"],
    )

    evidence = payload.get("market_evidence")
    if not isinstance(evidence, Mapping):
        raise JointScenarioError("market_evidence audit is missing")
    if (
        evidence.get("role") != "attached_diagnostic_bundle"
        or evidence.get("diagnostic_only") is not True
        or evidence.get("used_for_probability") is not False
        or evidence.get("conditioning_weight") != 0.0
        or evidence.get("ev_comparison_eligible") is not False
        or evidence.get("recommendation_eligible") is not False
        or evidence.get("same_price_independent_ev_authorized") is not False
    ):
        raise JointScenarioError("market evidence cannot condition this schema")
    if evidence.get("provided") is True:
        raw_evidence = evidence.get("payload")
        if not isinstance(raw_evidence, Mapping):
            raise JointScenarioError(
                "market_evidence.payload must preserve its input bundle"
            )
        expected_evidence = _normalize_market_evidence(
            raw_evidence,
            generated_at=generated_at,
            kickoff=kickoff,
            fixture=fixture,
            expected_match_id=fixture_match_id,
        )
    elif evidence.get("provided") is False:
        expected_evidence = _normalize_market_evidence(
            None,
            generated_at=generated_at,
            kickoff=kickoff,
            fixture=fixture,
            expected_match_id=fixture_match_id,
        )
    else:
        raise JointScenarioError("market_evidence.provided must be boolean")
    if _canonical_bytes(evidence) != _canonical_bytes(expected_evidence):
        raise JointScenarioError(
            "market evidence audit does not reproduce from its payload"
        )
    if inputs["market_evidence"].get("content_hash") != evidence.get("content_hash"):
        raise JointScenarioError("market evidence hash bindings do not match")

    score_matrix = _validate_score_marginal_payload(
        payload.get("full_time_score_marginal"), "full_time_score_marginal"
    )
    home_max = len(score_matrix) - 1
    away_max = len(score_matrix[0]) - 1
    htft_payload = payload.get("htft_marginal")
    if not isinstance(htft_payload, Mapping) or htft_payload.get("class_order") != list(
        HTFT_CODE_ORDER
    ):
        raise JointScenarioError("HT/FT marginal metadata is invalid")
    htft_probabilities = _validate_htft_probabilities(
        htft_payload.get("code_probabilities")
    )

    if legacy_cells:
        cells = payload.get("joint_cells")
        if not isinstance(cells, list):
            raise JointScenarioError("legacy joint_cells must be an array")
        calculated_score, calculated_htft = _marginals_from_cells(
            cells,
            home_goals_max=home_max,
            away_goals_max=away_max,
        )
        seed_planes = _aligned_planes_from_cells(
            cells,
            home_goals_max=home_max,
            away_goals_max=away_max,
            field="prior_probability",
        )
        saved_planes = _aligned_planes_from_cells(
            cells,
            home_goals_max=home_max,
            away_goals_max=away_max,
            field="probability",
        )
        expected_top_two = _top_two(cells)
        reconstructed_kernel: Mapping[str, Any] | None = None
    else:
        if "joint_cells" in payload:
            raise JointScenarioError(
                "schema 2.0 compact artifacts cannot retain duplicate joint_cells"
            )
        path_kernel = payload.get("path_kernel")
        if not isinstance(path_kernel, Mapping):
            raise JointScenarioError("path_kernel is missing")
        if path_kernel.get("alignment_mode") != "provided_aligned_event_planes":
            raise JointScenarioError(
                "path_kernel must bind the minimum-KL aligned event planes"
            )
        try:
            reconstructed_kernel = joint_path_kernel.validate_kernel(path_kernel)
        except joint_path_kernel.PathKernelError as exc:
            raise JointScenarioError(f"path_kernel validation failed: {exc}") from exc
        calculated_score = reconstructed_kernel["full_score_marginal"]
        calculated_htft = reconstructed_kernel["htft_marginal"]
        saved_planes = reconstructed_kernel["event_planes"]
        components = path_kernel.get("components")
        if not isinstance(components, Mapping):
            raise JointScenarioError("path_kernel components are missing")
        half_component = components.get("half_time")
        second_component = components.get("second_half")
        if not isinstance(half_component, Mapping) or not isinstance(
            second_component, Mapping
        ):
            raise JointScenarioError("path_kernel component matrices are missing")
        half_prior = _matrix_copy(
            half_component.get("conditional_score_matrix"),
            "path_kernel half-time component",
        )
        second_prior = _matrix_copy(
            second_component.get("conditional_score_matrix"),
            "path_kernel second-half component",
        )
        seed_planes, kernel_seed_tail = build_feasible_joint_seed(
            half_prior,
            second_prior,
            full_home_goals_max=home_max,
            full_away_goals_max=away_max,
        )
        kernel_tail = reconstructed_kernel["tail_mass"]
        if (
            abs(
                kernel_seed_tail["retained_probability"]
                - float(kernel_tail["conditional_convolution_retained_probability"])
            )
            > 1e-12
        ):
            raise JointScenarioError(
                "path_kernel conditional convolution tail does not reproduce"
            )
        stored_half = _validate_score_marginal_payload(
            payload.get("half_time_score_marginal"),
            "half_time_score_marginal",
        )
        stored_second = _validate_score_marginal_payload(
            payload.get("second_half_score_marginal"),
            "second_half_score_marginal",
        )
        if content_hash(stored_half) != content_hash(
            reconstructed_kernel["half_time_score_marginal"]
        ):
            raise JointScenarioError(
                "path kernel does not reproduce half-time score marginal"
            )
        if content_hash(stored_second) != content_hash(
            reconstructed_kernel["second_half_score_marginal"]
        ):
            raise JointScenarioError(
                "path kernel does not reproduce second-half score marginal"
            )
        rebuilt_half_results = _score_result_marginal(stored_half)
        rebuilt_htft_rows = _htft_row_marginal(calculated_htft)
        if any(
            abs(rebuilt_half_results[result] - rebuilt_htft_rows[result])
            > IPF_TOLERANCE
            for result in RESULT_CODES
        ):
            raise JointScenarioError(
                "half-time score and HT/FT row marginals are inconsistent"
            )
        expected_feasibility = _support_feasibility_binding(
            reconstructed_kernel["hall_audit"]
        )
        if payload.get("support_feasibility_audit") != expected_feasibility:
            raise JointScenarioError(
                "support feasibility audit does not reproduce from path_kernel"
            )
        expected_top_two = _top_two_from_event_planes(saved_planes)

    if content_hash(calculated_score) != content_hash(score_matrix):
        raise JointScenarioError(
            "joint path representation does not reproduce full-time score marginal"
        )
    if any(
        abs(calculated_htft[code] - htft_probabilities[code]) > IPF_TOLERANCE
        for code in HTFT_CODE_ORDER
    ):
        raise JointScenarioError(
            "joint path representation does not reproduce HT/FT marginal"
        )
    if htft_payload.get("half_time_result_probabilities") != _htft_row_marginal(
        calculated_htft
    ) or htft_payload.get("full_time_result_probabilities") != _htft_column_marginal(
        calculated_htft
    ):
        raise JointScenarioError("HT/FT row or column marginals are inconsistent")
    if anchor_half_marginal is not None:
        calculated_half = _htft_row_marginal(calculated_htft)
        if any(
            abs(calculated_half[result] - anchor_half_marginal[result]) > IPF_TOLERANCE
            for result in RESULT_CODES
        ):
            raise JointScenarioError(
                "joint HT/FT row marginal does not reproduce the upstream half-time anchor"
            )

    # Re-run IPF from the embedded path prior.  This proves that the saved
    # posterior is the deterministic minimum-KL projection, not an arbitrary
    # joint distribution with matching marginals.  Legacy artifacts recover the
    # prior from their exhaustive cells; schema 2.0 recovers it from HT/SH
    # component matrices in the compact four-axis path kernel.
    solver = payload.get("solver")
    if (
        not isinstance(solver, Mapping)
        or solver.get("method") != "aggregated_feasible_path_ipf_minimum_kl"
        or solver.get("tolerance") != IPF_TOLERANCE
        or solver.get("max_iterations") != IPF_MAX_ITERATIONS
        or solver.get("structural_zeros_preserved") is not True
    ):
        raise JointScenarioError("solver audit is invalid")
    reproduced, reproduced_solver = align_joint_distribution(
        seed_planes,
        score_matrix,
        htft_probabilities,
        tolerance=IPF_TOLERANCE,
        max_iterations=IPF_MAX_ITERATIONS,
    )
    maximum_cell_delta = max(
        abs(reproduced[index][home][away] - saved_planes[index][home][away])
        for index in range(3)
        for home in range(home_max + 1)
        for away in range(away_max + 1)
    )
    if maximum_cell_delta > IPF_TOLERANCE:
        raise JointScenarioError(
            "joint path representation is not the minimum-KL IPF projection"
        )
    for field in (
        "converged",
        "iterations",
        "tolerance",
        "max_iterations",
        "maximum_constraint_error",
        "maximum_score_marginal_error",
        "maximum_htft_marginal_error",
        "structural_zeros_preserved",
        "constraints",
        "method",
    ):
        actual = solver.get(field)
        expected = reproduced_solver.get(field)
        if isinstance(expected, float):
            if (
                not math.isfinite(float(actual))
                or abs(float(actual) - expected) > 1e-15
            ):
                raise JointScenarioError(f"solver.{field} does not reproduce")
        elif actual != expected:
            raise JointScenarioError(f"solver.{field} does not reproduce")

    if payload.get("joint_top_two") != expected_top_two:
        raise JointScenarioError(
            "joint_top_two does not match joint probability ranking"
        )
    expected_pair_mass = math.fsum(item["probability"] for item in expected_top_two)
    if (
        abs(
            _finite(
                payload.get("joint_top_two_probability_mass"),
                "joint_top_two_probability_mass",
            )
            - expected_pair_mass
        )
        > 1e-15
    ):
        raise JointScenarioError("joint_top_two_probability_mass is inconsistent")
    if payload.get("derived") != _derived_full_time_fields(calculated_score):
        raise JointScenarioError("derived football probabilities do not reproduce")
    if payload.get("derived_field_audits") != _derived_field_audits(
        str(probability_mode)
    ):
        raise JointScenarioError(
            "derived-field provenance and safety audits are invalid"
        )
    tail = payload.get("tail_mass")
    path_tail = (
        tail.get("feasible_path_convolution") if isinstance(tail, Mapping) else None
    )
    if (
        not isinstance(path_tail, Mapping)
        or path_tail.get("tolerance") != PATH_TAIL_TOLERANCE
        or path_tail.get("tolerance_met") is not True
        or _finite(path_tail.get("omitted_probability"), "path omitted probability")
        > PATH_TAIL_TOLERANCE
        or abs(
            _finite(path_tail.get("retained_probability"), "path retained probability")
            + _finite(path_tail.get("omitted_probability"), "path omitted probability")
            - 1.0
        )
        > 1e-12
    ):
        raise JointScenarioError("feasible path tail audit is unsafe")
    if not legacy_cells:
        assert reconstructed_kernel is not None
        kernel_tail = reconstructed_kernel["tail_mass"]
        if (
            abs(
                _finite(
                    path_tail.get("retained_probability"),
                    "path retained probability",
                )
                - float(kernel_tail["conditional_convolution_retained_probability"])
            )
            > 1e-12
            or abs(
                _finite(
                    path_tail.get("omitted_probability"),
                    "path omitted probability",
                )
                - float(kernel_tail["conditional_convolution_omitted_probability"])
            )
            > 1e-12
        ):
            raise JointScenarioError(
                "top-level feasible path tail does not match path_kernel"
            )
        for component_name, kernel_key in (
            ("half_time_component", "half_raw_omitted_probability"),
            ("second_half_component", "second_raw_omitted_probability"),
        ):
            component_tail = (
                tail.get(component_name) if isinstance(tail, Mapping) else None
            )
            if (
                not isinstance(component_tail, Mapping)
                or abs(
                    _finite(
                        component_tail.get("raw_omitted_probability"),
                        f"{component_name} raw omitted probability",
                    )
                    - float(kernel_tail[kernel_key])
                )
                > 1e-15
            ):
                raise JointScenarioError(
                    f"{component_name} tail does not match path_kernel"
                )


def validate_prediction_inputs(
    payload: Mapping[str, Any],
    model: Mapping[str, Any],
    score_prediction: Mapping[str, Any],
    htft_prediction: Mapping[str, Any],
    *,
    market_evidence: Mapping[str, Any] | None = None,
    expected_match_id: Any = None,
) -> None:
    """Optionally reproduce the artifact against the original bound inputs."""

    validate_prediction(payload)
    expected = _build_prediction(
        model,
        score_prediction,
        htft_prediction,
        generated_at=payload.get("generated_at"),
        market_evidence=market_evidence,
        expected_match_id=(
            expected_match_id
            if expected_match_id is not None
            else payload.get("fixture", {}).get("match_id")
        ),
    )
    if _canonical_bytes(payload) != _canonical_bytes(expected):
        raise JointScenarioError("prediction does not reproduce from its bound inputs")


def load_json(path: str | Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JointScenarioError(f"cannot read {name} JSON: {path}") from exc
    if not isinstance(value, dict):
        raise JointScenarioError(f"{name} JSON must contain an object")
    return value


def save_prediction(prediction: Mapping[str, Any], path: str | Path) -> Path:
    """Atomically save a self-validated joint prediction artifact."""

    validate_prediction(prediction)
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = (
        json.dumps(
            prediction, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        + "\n"
    )
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=destination.parent,
        suffix=".tmp",
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    temporary.replace(destination)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or validate a joint HT/FT + exact-score prediction"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    predict = subparsers.add_parser("predict", help="build a joint prediction")
    predict.add_argument("--model", required=True, help="registered HT/FT model JSON")
    predict.add_argument(
        "--score-prediction", required=True, help="canonical score prediction JSON"
    )
    predict.add_argument(
        "--htft-prediction", required=True, help="registered HT/FT prediction JSON"
    )
    predict.add_argument("--market-evidence", help="optional diagnostic evidence JSON")
    predict.add_argument(
        "--expected-match-id",
        help="optional fixture ID that market evidence must match",
    )
    predict.add_argument(
        "--generated-at", required=True, help="timezone-aware ISO time"
    )
    predict.add_argument("--output", required=True, help="output prediction JSON")

    validate = subparsers.add_parser(
        "validate", help="self-validate a saved joint prediction"
    )
    validate.add_argument("--prediction", required=True, help="joint prediction JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "predict":
            model = load_json(arguments.model, "registered HT/FT model")
            score_prediction = load_json(
                arguments.score_prediction, "canonical score prediction"
            )
            htft_prediction = load_json(arguments.htft_prediction, "HT/FT prediction")
            evidence = (
                load_json(arguments.market_evidence, "market evidence")
                if arguments.market_evidence
                else None
            )
            prediction = predict_joint_scenarios(
                model,
                score_prediction,
                htft_prediction,
                generated_at=arguments.generated_at,
                market_evidence=evidence,
                expected_match_id=arguments.expected_match_id,
            )
            destination = save_prediction(prediction, arguments.output)
            print(
                json.dumps(
                    {
                        "output": str(destination),
                        "prediction_hash": prediction["prediction_hash"],
                        "joint_top_two": prediction["joint_top_two"],
                        "probability_mode": prediction["probability_mode"],
                        "market_conditioning_enabled": prediction[
                            "market_conditioning_enabled"
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            prediction = load_json(arguments.prediction, "joint prediction")
            validate_prediction(prediction)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "prediction_hash": prediction["prediction_hash"],
                    },
                    sort_keys=True,
                )
            )
    except JointScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

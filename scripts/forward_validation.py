#!/usr/bin/env python3
"""Evaluate a frozen live-forward cohort against explicit probability baselines.

The input must include every eligible fixture, including abstentions and unavailable model
outputs.  Proper-score comparisons are paired on the same timestamped observations and
uncertainty is resampled by kickoff week so repeated teams and short-term conditions are not
treated as independent single matches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence

try:
    from scripts import (
        cohort_scope,
        execution_evidence,
        forward_policy,
        source_evidence,
    )
except ImportError:  # Direct execution from scripts/.
    script_directory = str(Path(__file__).resolve().parent)
    if script_directory not in sys.path:
        sys.path.insert(0, script_directory)
    import cohort_scope  # type: ignore[no-redef]
    import execution_evidence  # type: ignore[no-redef]
    import forward_policy  # type: ignore[no-redef]
    import source_evidence  # type: ignore[no-redef]

LEGACY_INPUT_SCHEMA_VERSION = "forward-observations/1.0.0"
PREVIOUS_INPUT_SCHEMA_VERSION = "forward-observations/2.0.0"
INPUT_SCHEMA_VERSION = "forward-observations/3.0.0"
QUEUE_SCHEMA_VERSION = "forward-eligibility-queue/2.0.0"
COMMITMENT_SCHEMA_VERSION = "forward-observation-commitment/2.0.0"
SETTLEMENT_SCHEMA_VERSION = "forward-observation-settlement/2.0.0"
PREVIOUS_HISTORY_LEDGER_BINDING_SCHEMA_VERSION = (
    "memory-forward-history-ledger-binding/2.0.0"
)
HISTORY_LEDGER_BINDING_SCHEMA_VERSION = "memory-forward-history-ledger-binding/3.0.0"
PREVIOUS_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION = "memory-forward-record-receipt/1.0.0"
HISTORY_RECORD_RECEIPT_SCHEMA_VERSION = "memory-forward-record-receipt/2.0.0"
HISTORY_AGGREGATE_ARTIFACT_TYPE = "memory_store_forward_validation_cohort_export"
REPORT_SCHEMA_VERSION = "forward-validation/3.0.0"
BASELINE_NAMES = (
    "historical_frequency",
    "independent_htft",
    "simple_poisson_dc",
    "bookmaker_no_vig",
)
MODEL_SPACE_BASELINE_NAMES = BASELINE_NAMES[:-1]
FIVE_STATE_SETTLEMENT_STATES = (
    "full_win",
    "half_win",
    "push",
    "half_loss",
    "loss",
)
CATEGORICAL_PROPER_SCORE_SPACE = "categorical_same_outcome_space"
FIVE_STATE_PROPER_SCORE_SPACE = "five_state_return"
PROPER_SCORE_BASELINES_BY_SPACE = {
    CATEGORICAL_PROPER_SCORE_SPACE: ("bookmaker_no_vig",),
    FIVE_STATE_PROPER_SCORE_SPACE: MODEL_SPACE_BASELINE_NAMES,
}
EPSILON = 1e-15
SAME_TIME_BASELINE_TOLERANCE_MINUTES = 5.0


class ForwardValidationError(ValueError):
    """Raised when evidence is incomplete, leaked, or internally inconsistent."""


def _sha256(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise ForwardValidationError(f"{label} must be a lowercase SHA-256 identity")
    return text


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _aware(value: Any, label: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ForwardValidationError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardValidationError(f"{label} must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _probabilities(value: Any, outcomes: Sequence[str], label: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(outcomes):
        raise ForwardValidationError(
            f"{label} must contain exactly: {', '.join(outcomes)}"
        )
    converted: dict[str, float] = {}
    for outcome in outcomes:
        raw = value[outcome]
        if isinstance(raw, bool):
            raise ForwardValidationError(f"{label}.{outcome} must be numeric")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ForwardValidationError(f"{label}.{outcome} must be numeric") from exc
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise ForwardValidationError(
                f"{label}.{outcome} must be finite and between zero and one"
            )
        converted[outcome] = number
    if not math.isclose(math.fsum(converted.values()), 1.0, abs_tol=1e-8):
        raise ForwardValidationError(f"{label} must sum to one")
    return converted


def _losses(probabilities: Mapping[str, float], actual: str) -> tuple[float, float]:
    log_loss = -math.log(max(float(probabilities[actual]), EPSILON))
    brier = math.fsum(
        (float(probability) - (1.0 if outcome == actual else 0.0)) ** 2
        for outcome, probability in probabilities.items()
    )
    return log_loss, brier


def _observed_state(row: Mapping[str, Any]) -> Any:
    """Read v3 settlement state while keeping legacy v1 reports read-only."""

    if "observed_settlement_state" in row:
        return row.get("observed_settlement_state")
    return row.get("observed_outcome")


def _lead_bucket(minutes: float) -> str:
    if minutes <= 30:
        return "0-30m"
    if minutes <= 60:
        return "31-60m"
    if minutes <= 180:
        return "61-180m"
    return "181m+"


def _validate_legacy_input(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ForwardValidationError("forward observations must be a JSON object")
    value = dict(payload)
    if value.get("schema_version") != LEGACY_INPUT_SCHEMA_VERSION:
        raise ForwardValidationError("unsupported forward-observations schema_version")
    try:
        policy = forward_policy.validate_policy_manifest(
            value.get("policy_manifest") or {}
        )
        cohort = forward_policy.validate_cohort(value.get("cohort_manifest") or {})
    except forward_policy.ForwardPolicyError as exc:
        raise ForwardValidationError(
            "frozen policy/cohort manifest is invalid"
        ) from exc
    for field in ("cohort_id", "policy_id", "policy_hash"):
        if not str(value.get(field) or "").strip():
            raise ForwardValidationError(f"{field} is required")
    if (
        value["cohort_id"] != cohort["cohort_id"]
        or value["policy_id"] != policy["policy_id"]
        or value["policy_hash"] != policy["policy_hash"]
        or cohort["policy_id"] != policy["policy_id"]
        or cohort["policy_hash"] != policy["policy_hash"]
    ):
        raise ForwardValidationError(
            "top-level policy/cohort assertions do not match the frozen manifests"
        )
    outcomes = value.get("outcomes")
    if (
        not isinstance(outcomes, list)
        or len(outcomes) < 2
        or any(not isinstance(item, str) or not item for item in outcomes)
        or len(set(outcomes)) != len(outcomes)
    ):
        raise ForwardValidationError("outcomes must be a unique string array")
    rows = value.get("records")
    if not isinstance(rows, list) or not rows:
        raise ForwardValidationError("records must be a non-empty array")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        label = f"records[{index}]"
        if not isinstance(raw, Mapping):
            raise ForwardValidationError(f"{label} must be an object")
        row = dict(raw)
        fixture_id = str(row.get("fixture_id") or "").strip()
        observation_id = str(row.get("observation_id") or "").strip()
        market = str(row.get("market") or "").strip()
        if not fixture_id:
            raise ForwardValidationError(f"{label}.fixture_id is required")
        if not observation_id or observation_id in seen:
            raise ForwardValidationError(
                f"{label}.observation_id is missing or duplicated"
            )
        if not market:
            raise ForwardValidationError(f"{label}.market is required")
        seen.add(observation_id)
        kickoff = _aware(row.get("kickoff"), f"{label}.kickoff")
        generated = _aware(row.get("generated_at"), f"{label}.generated_at")
        if generated >= kickoff:
            raise ForwardValidationError(
                f"{label} was not generated strictly before kickoff"
            )
        status = str(row.get("status") or "").strip()
        if status not in {"predicted", "abstained", "unavailable"}:
            raise ForwardValidationError(
                f"{label}.status must be predicted, abstained, or unavailable"
            )
        actual = row.get("observed_outcome")
        if actual is not None and actual not in outcomes:
            raise ForwardValidationError(f"{label}.observed_outcome is invalid")
        model = row.get("model_probabilities")
        if status in {"predicted", "abstained"}:
            model = _probabilities(model, outcomes, f"{label}.model_probabilities")
        elif model is not None:
            raise ForwardValidationError(
                f"{label} cannot carry model probabilities when status={status}"
            )
        try:
            binding = forward_policy.validate_record_binding(
                row.get("forward_policy_binding")
            )
        except forward_policy.ForwardPolicyError as exc:
            raise ForwardValidationError(
                f"{label}.forward_policy_binding is invalid"
            ) from exc
        if binding is None:
            raise ForwardValidationError(f"{label}.forward_policy_binding is required")
        if (
            binding["cohort_id"] != cohort["cohort_id"]
            or binding["cohort_hash"] != cohort["cohort_hash"]
            or binding["policy_id"] != policy["policy_id"]
            or binding["policy_hash"] != policy["policy_hash"]
            or binding["policy_snapshot"] != policy
        ):
            raise ForwardValidationError(
                f"{label}.forward_policy_binding does not match the report cohort"
            )
        archived = _aware(binding.get("archived_at"), f"{label}.binding.archived_at")
        if archived < generated or archived >= kickoff:
            raise ForwardValidationError(
                f"{label}.binding.archived_at must be after generation and before kickoff"
            )
        baselines = row.get("baselines") or {}
        if not isinstance(baselines, Mapping):
            raise ForwardValidationError(f"{label}.baselines must be an object")
        unknown_baselines = set(baselines) - set(BASELINE_NAMES)
        if unknown_baselines:
            raise ForwardValidationError(
                f"{label}.baselines contains unsupported names: {sorted(unknown_baselines)}"
            )
        converted_baselines = {
            name: _probabilities(probabilities, outcomes, f"{label}.baselines.{name}")
            for name, probabilities in baselines.items()
        }
        bookmaker_snapshot = row.get("bookmaker_snapshot")
        if "bookmaker_no_vig" in converted_baselines:
            if not isinstance(bookmaker_snapshot, Mapping):
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot is required for bookmaker_no_vig"
                )
            bookmaker_collected = _aware(
                bookmaker_snapshot.get("collected_at"),
                f"{label}.bookmaker_snapshot.collected_at",
            )
            if bookmaker_collected >= kickoff:
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot must be pre-kickoff"
                )
            difference = abs((bookmaker_collected - generated).total_seconds()) / 60.0
            if difference > SAME_TIME_BASELINE_TOLERANCE_MINUTES:
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot is not a same-time baseline"
                )
            bookmaker_snapshot = {
                "collected_at": bookmaker_collected.isoformat(),
                "source_evidence_hash": _sha256(
                    bookmaker_snapshot.get("source_evidence_hash"),
                    f"{label}.bookmaker_snapshot.source_evidence_hash",
                ),
            }
        elif bookmaker_snapshot is not None:
            raise ForwardValidationError(
                f"{label}.bookmaker_snapshot requires bookmaker_no_vig probabilities"
            )
        lead = row.get("lead_time_minutes")
        if isinstance(lead, bool):
            raise ForwardValidationError(f"{label}.lead_time_minutes must be numeric")
        try:
            lead_minutes = float(lead)
        except (TypeError, ValueError) as exc:
            raise ForwardValidationError(
                f"{label}.lead_time_minutes must be numeric"
            ) from exc
        actual_lead = (kickoff - generated).total_seconds() / 60.0
        if lead_minutes < 0 or not math.isclose(lead_minutes, actual_lead, abs_tol=1.0):
            raise ForwardValidationError(
                f"{label}.lead_time_minutes does not match generated_at and kickoff"
            )
        execution = row.get("execution")
        if execution is not None:
            if not isinstance(execution, Mapping):
                raise ForwardValidationError(f"{label}.execution must be an object")
            selection = execution.get("selection")
            if selection not in outcomes:
                raise ForwardValidationError(f"{label}.execution.selection is invalid")
            if status != "predicted":
                raise ForwardValidationError(
                    f"{label}.execution is allowed only for a released prediction"
                )
            prices: dict[str, float] = {}
            for field in ("entry_decimal_odds", "closing_decimal_odds"):
                raw_price = execution.get(field)
                try:
                    price = float(raw_price)
                except (TypeError, ValueError) as exc:
                    raise ForwardValidationError(
                        f"{label}.execution.{field} must be numeric"
                    ) from exc
                if not math.isfinite(price) or price <= 1.0:
                    raise ForwardValidationError(
                        f"{label}.execution.{field} must exceed 1.0"
                    )
                prices[field] = price
            entry_collected = _aware(
                execution.get("entry_collected_at"),
                f"{label}.execution.entry_collected_at",
            )
            closing_collected = _aware(
                execution.get("closing_collected_at"),
                f"{label}.execution.closing_collected_at",
            )
            if (
                abs((entry_collected - generated).total_seconds()) / 60.0
                > SAME_TIME_BASELINE_TOLERANCE_MINUTES
                or entry_collected > closing_collected
                or closing_collected >= kickoff
            ):
                raise ForwardValidationError(
                    f"{label}.execution timestamps are not a valid pre-kickoff price sequence"
                )
            if execution.get("entry_price_kind") != "executable_after_slippage":
                raise ForwardValidationError(
                    f"{label}.execution.entry_price_kind must be executable_after_slippage"
                )
            if execution.get("limit_verified") is not True:
                raise ForwardValidationError(
                    f"{label}.execution must verify the applicable stake limit"
                )
            raw_stake = execution.get("stake_units")
            try:
                stake_units = float(raw_stake)
            except (TypeError, ValueError) as exc:
                raise ForwardValidationError(
                    f"{label}.execution.stake_units must be numeric"
                ) from exc
            if not math.isfinite(stake_units) or stake_units <= 0.0:
                raise ForwardValidationError(
                    f"{label}.execution.stake_units must be finite and positive"
                )
            execution = {
                "selection": selection,
                **prices,
                "entry_collected_at": entry_collected.isoformat(),
                "closing_collected_at": closing_collected.isoformat(),
                "entry_source_evidence_hash": _sha256(
                    execution.get("entry_source_evidence_hash"),
                    f"{label}.execution.entry_source_evidence_hash",
                ),
                "closing_source_evidence_hash": _sha256(
                    execution.get("closing_source_evidence_hash"),
                    f"{label}.execution.closing_source_evidence_hash",
                ),
                "entry_price_kind": "executable_after_slippage",
                "limit_verified": True,
                "stake_units": stake_units,
            }
        normalized.append(
            {
                **row,
                "fixture_id": fixture_id,
                "observation_id": observation_id,
                "market": market,
                "kickoff": kickoff.isoformat(),
                "generated_at": generated.isoformat(),
                "status": status,
                "observed_outcome": actual,
                "model_probabilities": model,
                "baselines": converted_baselines,
                "bookmaker_snapshot": bookmaker_snapshot,
                "forward_policy_binding": binding,
                "lead_time_minutes": lead_minutes,
                "lead_time_bucket": _lead_bucket(lead_minutes),
                "kickoff_week": f"{kickoff.isocalendar().year}-W{kickoff.isocalendar().week:02d}",
                "execution": execution,
            }
        )
    value["records"] = normalized
    value["outcomes"] = list(outcomes)
    value["policy_manifest"] = policy
    value["cohort_manifest"] = cohort
    value["evidence_contract"] = "legacy_uncommitted_read_only"
    value["legacy_uncommitted"] = True
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ForwardValidationError(
            f"{label} fields are not canonical (missing={missing}, extra={extra})"
        )


def _queue_key(cohort_id: str, fixture_id: str, market_identity_hash: str) -> str:
    return _hash(
        {
            "cohort_id": cohort_id,
            "fixture_id": fixture_id,
            "market_identity_hash": market_identity_hash,
        }
    )


def _observation_id(queue_key: str) -> str:
    return _hash({"forward_queue_key": queue_key})


def _positive_decimal_price(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ForwardValidationError(f"{label} must be numeric")
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ForwardValidationError(f"{label} must be numeric") from exc
    if not math.isfinite(price) or price <= 1.0:
        raise ForwardValidationError(f"{label} must be finite and exceed 1.0")
    return price


def _no_vig_from_complete_decimal_odds(
    odds: Any, outcomes: Sequence[str], label: str
) -> tuple[dict[str, float], dict[str, float]]:
    if not isinstance(odds, Mapping) or set(odds) != set(outcomes):
        raise ForwardValidationError(
            f"{label} must contain one decimal price for every market outcome"
        )
    converted = {
        outcome: _positive_decimal_price(odds[outcome], f"{label}.{outcome}")
        for outcome in outcomes
    }
    raw = {outcome: 1.0 / converted[outcome] for outcome in outcomes}
    overround = math.fsum(raw.values())
    if not math.isfinite(overround) or overround <= 0.0:
        raise ForwardValidationError(f"{label} has an invalid overround")
    return converted, {outcome: raw[outcome] / overround for outcome in outcomes}


def _market_identity(
    value: Any, identity_hash: Any, label: str
) -> tuple[dict[str, Any], str]:
    try:
        identity = source_evidence.canonical_market_identity(value, label=label)
        expected_hash = source_evidence.market_identity_hash(identity)
    except source_evidence.SourceEvidenceError as exc:
        raise ForwardValidationError(f"{label} is invalid") from exc
    supplied_hash = _sha256(identity_hash, f"{label}_hash")
    if supplied_hash != expected_hash:
        raise ForwardValidationError(f"{label}_hash does not bind {label}")
    return identity, supplied_hash


def _model_expected_return(
    probabilities: Mapping[str, float],
    *,
    selection: str,
    decimal_odds: float,
    settlement_semantics: str,
) -> float:
    win_profit = decimal_odds - 1.0
    if settlement_semantics == "categorical":
        win_probability = float(probabilities[selection])
        return win_probability * win_profit - (1.0 - win_probability)
    returns = {
        "full_win": win_profit,
        "half_win": win_profit / 2.0,
        "push": 0.0,
        "half_loss": -0.5,
        "loss": -1.0,
    }
    return math.fsum(
        float(probabilities[state]) * value for state, value in returns.items()
    )


def _validation_protocol(policy: Mapping[str, Any]) -> dict[str, Any]:
    runtime = policy.get("policy")
    protocol = (
        runtime.get("validation_protocol") if isinstance(runtime, Mapping) else None
    )
    if not isinstance(protocol, Mapping):
        raise ForwardValidationError(
            "v3 forward observations require a validation protocol frozen in policy"
        )
    required = {
        "schema_version",
        "bootstrap_repetitions",
        "bootstrap_seed",
        "minimum_confirmation_samples",
        "minimum_iso_week_clusters",
        "minimum_segment_samples",
        "minimum_segment_clusters",
        "same_time_tolerance_minutes",
        "maximum_calibration_error",
        "cluster_unit",
        "required_model_space_baselines",
        "bookmaker_price_baseline",
        "bookmaker_proper_score_scope",
        "five_state_evaluation_scope",
        "queue_contract",
        "external_timestamp_anchor_required_for_promotion",
    }
    _exact_keys(protocol, required, "policy.validation_protocol")
    if protocol.get("schema_version") != "forward-validation-protocol/2.0.0":
        raise ForwardValidationError("unsupported frozen validation protocol")
    integers: dict[str, int] = {}
    for field, minimum in (
        ("bootstrap_repetitions", 100),
        ("minimum_confirmation_samples", 1),
        ("minimum_iso_week_clusters", 2),
        ("minimum_segment_samples", 1),
        ("minimum_segment_clusters", 2),
    ):
        raw = protocol.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
            raise ForwardValidationError(
                f"policy.validation_protocol.{field} must be an integer >= {minimum}"
            )
        integers[field] = raw
    seed = protocol.get("bootstrap_seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ForwardValidationError(
            "policy.validation_protocol.bootstrap_seed must be an integer"
        )
    try:
        tolerance = float(protocol.get("same_time_tolerance_minutes"))
        maximum_ece = float(protocol.get("maximum_calibration_error"))
    except (TypeError, ValueError) as exc:
        raise ForwardValidationError(
            "frozen validation tolerances must be numeric"
        ) from exc
    if not math.isfinite(tolerance) or tolerance < 0.0 or tolerance > 10.0:
        raise ForwardValidationError("frozen same-time tolerance is invalid")
    if not math.isfinite(maximum_ece) or not 0.0 <= maximum_ece <= 0.1:
        raise ForwardValidationError("frozen calibration threshold is invalid")
    if protocol.get("cluster_unit") != "kickoff_iso_week":
        raise ForwardValidationError("unsupported frozen cluster unit")
    if protocol.get("queue_contract") != "frozen_fixture_market_manifest":
        raise ForwardValidationError("unsupported frozen queue contract")
    if protocol.get("external_timestamp_anchor_required_for_promotion") is not True:
        raise ForwardValidationError(
            "frozen protocol must require an external timestamp anchor for promotion"
        )
    required_baselines = protocol.get("required_model_space_baselines")
    if (
        not isinstance(required_baselines, list)
        or tuple(required_baselines) != MODEL_SPACE_BASELINE_NAMES
    ):
        raise ForwardValidationError(
            "frozen validation protocol does not require the canonical model-space baselines"
        )
    if protocol.get("bookmaker_price_baseline") != "bookmaker_no_vig":
        raise ForwardValidationError("frozen bookmaker price baseline is unsupported")
    if (
        protocol.get("bookmaker_proper_score_scope")
        != "categorical_same_outcome_space_only"
    ):
        raise ForwardValidationError(
            "frozen bookmaker proper-score scope is unsupported"
        )
    if (
        protocol.get("five_state_evaluation_scope")
        != "settlement_state_scores_ev_roi_plus_price_space_clv"
    ):
        raise ForwardValidationError(
            "frozen five-state evaluation scope is unsupported"
        )
    return {
        **dict(protocol),
        **integers,
        "bootstrap_seed": seed,
        "same_time_tolerance_minutes": tolerance,
        "maximum_calibration_error": maximum_ece,
    }


def _market_schemas(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or not value:
        raise ForwardValidationError("market_schemas must be a non-empty object")
    normalized: dict[str, dict[str, Any]] = {}
    for raw_market, raw_schema in value.items():
        market = str(raw_market).strip().lower()
        if not market or market != raw_market or market in normalized:
            raise ForwardValidationError(
                "market_schemas keys must be canonical and unique"
            )
        if not isinstance(raw_schema, Mapping):
            raise ForwardValidationError(f"market_schemas.{market} must be an object")
        _exact_keys(
            raw_schema,
            {"settlement_states", "settlement_semantics"},
            f"market_schemas.{market}",
        )
        settlement_states = raw_schema.get("settlement_states")
        if (
            not isinstance(settlement_states, list)
            or len(settlement_states) < 2
            or any(not isinstance(item, str) or not item for item in settlement_states)
            or len(set(settlement_states)) != len(settlement_states)
        ):
            raise ForwardValidationError(
                f"market_schemas.{market}.settlement_states must be unique strings"
            )
        semantics = str(raw_schema.get("settlement_semantics") or "")
        if semantics not in {"categorical", "five_state_return"}:
            raise ForwardValidationError(
                f"market_schemas.{market}.settlement_semantics is unsupported"
            )
        if semantics == "five_state_return" and tuple(settlement_states) != (
            FIVE_STATE_SETTLEMENT_STATES
        ):
            raise ForwardValidationError(
                f"market_schemas.{market} five-state settlement_states are not canonical"
            )
        normalized[market] = {
            "settlement_states": list(settlement_states),
            "settlement_semantics": semantics,
        }
    return normalized


def _prematch_ledger_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(payload))
    historical = value.get("schema_version") == PREVIOUS_INPUT_SCHEMA_VERSION
    value.pop("history_ledger_binding", None)
    value["cohort_closure"] = None
    for settlement in value.get("settlements", []):
        if not isinstance(settlement, dict):
            continue
        settlement.update(
            {
                "status": "pending",
                (
                    "observed_outcome" if historical else "observed_settlement_state"
                ): None,
                "result_collected_at": None,
                "result_source_evidence_hash": None,
            }
        )
        settlement.pop("settlement_hash", None)
        settlement["settlement_hash"] = _hash(settlement)
    return value


def _market_commitment_identities(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    historical = payload.get("schema_version") == PREVIOUS_INPUT_SCHEMA_VERSION
    identities: list[dict[str, str]] = []
    raw_commitments = payload.get("commitments")
    if not isinstance(raw_commitments, list):
        return identities
    for raw in raw_commitments:
        if not isinstance(raw, Mapping) or not isinstance(
            raw.get("prediction_payload"), Mapping
        ):
            continue
        prediction = raw["prediction_payload"]
        binding = raw.get("forward_policy_binding")
        identity = {
            "observation_id": str(prediction.get("observation_id") or ""),
            "prediction_hash": _hash(prediction),
            "commitment_hash": str(raw.get("commitment_hash") or ""),
            "binding_hash": str(
                binding.get("binding_hash") if isinstance(binding, Mapping) else ""
            ),
        }
        if historical:
            identity["market"] = str(prediction.get("market") or "").lower()
        else:
            identity.update(
                {
                    "family": str(
                        prediction.get("market_identity", {}).get("family")
                        if isinstance(prediction.get("market_identity"), Mapping)
                        else ""
                    ).lower(),
                    "market_identity_hash": str(
                        prediction.get("market_identity_hash") or ""
                    ),
                }
            )
        identities.append(identity)
    identities.sort(
        key=(
            (lambda item: (item["market"], item["observation_id"]))
            if historical
            else (lambda item: (item["market_identity_hash"], item["observation_id"]))
        )
    )
    return identities


def _require_repository_commit(commit: str) -> str:
    """Require the claimed policy commit to exist in this repository.

    The policy freeze still supplies the reviewed final-merge SHA.  This independent
    evaluation check prevents a self-contained payload from satisfying that contract by
    merely putting the same invented 40-hex string in both commit fields.
    """

    checked = forward_policy._require_git_commit(commit, "history policy Git commit")
    repository = Path(__file__).resolve().parents[1]
    try:
        forward_policy._git(repository, "cat-file", "-e", f"{checked}^{{commit}}")
    except forward_policy.ForwardPolicyError as exc:
        raise ForwardValidationError(
            "history policy Git commit does not exist in the evaluation repository"
        ) from exc
    return checked


def _validate_history_record_receipt(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ForwardValidationError("history record receipt must be an object")
    value = deepcopy(dict(raw))
    required = {
        "schema_version",
        "fixture_id",
        "record_archived_at",
        "archive_version_hash",
        "record_commitment_hash",
        "record_binding_hash",
        "prematch_ledger_hash",
        "ledger_payload_hash",
        "market_commitments",
        "ledger_payload",
        "archive_snapshot_payload",
        "receipt_hash",
    }
    _exact_keys(value, required, "history record receipt")
    receipt_schema = value.get("schema_version")
    if receipt_schema not in {
        PREVIOUS_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION,
        HISTORY_RECORD_RECEIPT_SCHEMA_VERSION,
    }:
        raise ForwardValidationError("history record receipt schema is unsupported")
    historical = receipt_schema == PREVIOUS_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION
    supplied_receipt_hash = value.pop("receipt_hash", None)
    if supplied_receipt_hash != _hash(value):
        raise ForwardValidationError("history record receipt hash is invalid")

    fixture_id = str(value.get("fixture_id") or "").strip()
    if not fixture_id:
        raise ForwardValidationError("history record receipt fixture_id is missing")
    archived_at = _aware(value.get("record_archived_at"), "receipt record_archived_at")
    archive_version_hash = _sha256(
        value.get("archive_version_hash"), "receipt archive_version_hash"
    )
    record_commitment_hash = _sha256(
        value.get("record_commitment_hash"), "receipt record_commitment_hash"
    )
    record_binding_hash = _sha256(
        value.get("record_binding_hash"), "receipt record_binding_hash"
    )
    prematch_ledger_hash = _sha256(
        value.get("prematch_ledger_hash"), "receipt prematch_ledger_hash"
    )
    ledger_payload_hash = _sha256(
        value.get("ledger_payload_hash"), "receipt ledger_payload_hash"
    )

    ledger_payload = value.get("ledger_payload")
    if (
        not isinstance(ledger_payload, Mapping)
        or _hash(ledger_payload) != ledger_payload_hash
    ):
        raise ForwardValidationError("history receipt ledger payload hash is invalid")
    fixture_ids = sorted(
        {
            str(item.get("prediction_payload", {}).get("fixture_id") or "")
            for item in ledger_payload.get("commitments", [])
            if isinstance(item, Mapping)
            and isinstance(item.get("prediction_payload"), Mapping)
        }
    )
    if fixture_ids != [fixture_id]:
        raise ForwardValidationError(
            "history record receipt must replay exactly one fixture"
        )
    market_commitments = _market_commitment_identities(ledger_payload)
    if value.get("market_commitments") != market_commitments:
        raise ForwardValidationError(
            "history record receipt market commitments do not replay"
        )

    snapshot = value.get("archive_snapshot_payload")
    if not isinstance(snapshot, Mapping) or _hash(snapshot) != archive_version_hash:
        raise ForwardValidationError("history record archive snapshot hash is invalid")
    record_commitment = snapshot.get("forward_prediction_commitment")
    if not isinstance(record_commitment, Mapping):
        raise ForwardValidationError("history record commitment is missing")
    commitment = deepcopy(dict(record_commitment))
    expected_commitment_fields = {
        "schema_version",
        "prediction_payload",
        "prediction_hash",
        "forward_policy_binding",
        "commitment_hash",
    }
    _exact_keys(commitment, expected_commitment_fields, "history record commitment")
    supplied_commitment_hash = commitment.pop("commitment_hash", None)
    expected_commitment_schema = (
        "memory-forward-commitment/1.0.0"
        if historical
        else "memory-forward-commitment/2.0.0"
    )
    if (
        record_commitment.get("schema_version") != expected_commitment_schema
        or supplied_commitment_hash != record_commitment_hash
        or supplied_commitment_hash != _hash(commitment)
    ):
        raise ForwardValidationError("history record commitment hash is invalid")
    prediction_payload = record_commitment.get("prediction_payload")
    if not isinstance(prediction_payload, Mapping):
        raise ForwardValidationError("history record prediction payload is missing")
    expected_prediction_schema = (
        "memory-forward-prediction/1.0.0"
        if historical
        else "memory-forward-prediction/2.0.0"
    )
    if prediction_payload.get("schema_version") != expected_prediction_schema:
        raise ForwardValidationError(
            "history record prediction schema does not match its receipt"
        )
    prediction_hash = _hash(prediction_payload)
    if record_commitment.get("prediction_hash") != prediction_hash:
        raise ForwardValidationError("history record prediction hash is invalid")
    try:
        record_binding = forward_policy.validate_record_binding(
            record_commitment.get("forward_policy_binding")
        )
    except forward_policy.ForwardPolicyError as exc:
        raise ForwardValidationError(
            "history record policy binding is invalid"
        ) from exc
    expected_binding_schema = (
        forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
        if historical
        else forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
    )
    if (
        record_binding is None
        or record_binding.get("schema_version") != expected_binding_schema
        or record_binding.get("binding_hash") != record_binding_hash
        or record_binding.get("observation_commitment_hash") != prediction_hash
        or snapshot.get("forward_policy_binding") != record_binding
    ):
        raise ForwardValidationError(
            "history record does not preserve its committed policy binding"
        )
    policy_snapshot = record_binding.get("policy_snapshot")
    if not isinstance(policy_snapshot, Mapping):
        raise ForwardValidationError("history record policy snapshot is missing")
    claimed_commit = str(policy_snapshot.get("code", {}).get("commit") or "")
    expected_commit = str(
        policy_snapshot.get("code", {}).get("expected_final_merge_commit") or ""
    )
    if _require_repository_commit(claimed_commit) != expected_commit:
        raise ForwardValidationError(
            "history record is not bound to the reviewed final merge commit"
        )
    if (
        _aware(record_binding.get("archived_at"), "history binding archived_at")
        != archived_at
        or _aware(
            prediction_payload.get("archived_at"), "history prediction archived_at"
        )
        != archived_at
    ):
        raise ForwardValidationError(
            "history record archived_at does not match its committed binding"
        )

    archive = snapshot.get("forward_validation_ledger")
    if not isinstance(archive, Mapping):
        raise ForwardValidationError("history record ledger archive is missing")
    archive_without_hash = deepcopy(dict(archive))
    supplied_archive_hash = archive_without_hash.pop("archive_hash", None)
    archived_ledger = archive.get("ledger_payload")
    expected_archive_schema = (
        "memory-forward-ledger-archive/1.0.0"
        if historical
        else "memory-forward-ledger-archive/2.0.0"
    )
    if (
        archive.get("schema_version") != expected_archive_schema
        or supplied_archive_hash != _hash(archive_without_hash)
        or str(archive.get("fixture_id") or "") != fixture_id
        or not isinstance(archived_ledger, Mapping)
        or archive.get("ledger_hash") != _hash(archived_ledger)
        or archive.get("ledger_hash") != prematch_ledger_hash
        or archive.get("market_commitments")
        != _market_commitment_identities(archived_ledger)
        or _prematch_ledger_view(ledger_payload) != archived_ledger
    ):
        raise ForwardValidationError(
            "history record receipt does not reproduce its archived pre-match ledger"
        )
    prediction_ledger = prediction_payload.get("ledger")
    if (
        not isinstance(prediction_ledger, Mapping)
        or prediction_ledger.get("ledger_hash") != prematch_ledger_hash
        or prediction_ledger.get("archive_hash") != supplied_archive_hash
        or prediction_ledger.get("market_commitments") != market_commitments
    ):
        raise ForwardValidationError(
            "history record prediction does not commit the replayed ledger"
        )

    value["record_archived_at"] = archived_at.isoformat()
    value["archive_version_hash"] = archive_version_hash
    value["record_commitment_hash"] = record_commitment_hash
    value["record_binding_hash"] = record_binding_hash
    value["prematch_ledger_hash"] = prematch_ledger_hash
    value["ledger_payload_hash"] = ledger_payload_hash
    value["receipt_hash"] = supplied_receipt_hash
    return value


def _validate_history_ledger_binding(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ForwardValidationError(
            "v3 evaluation requires a memory-store cohort aggregate binding"
        )
    value = deepcopy(dict(raw))
    required = {
        "schema_version",
        "artifact_type",
        "cohort_id",
        "policy_id",
        "policy_hash",
        "fixture_ids",
        "receipts",
        "binding_hash",
    }
    _exact_keys(value, required, "history_ledger_binding")
    binding_schema = value.get("schema_version")
    if (
        binding_schema
        not in {
            PREVIOUS_HISTORY_LEDGER_BINDING_SCHEMA_VERSION,
            HISTORY_LEDGER_BINDING_SCHEMA_VERSION,
        }
        or value.get("artifact_type") != HISTORY_AGGREGATE_ARTIFACT_TYPE
    ):
        raise ForwardValidationError("history_ledger_binding schema is unsupported")
    historical = binding_schema == PREVIOUS_HISTORY_LEDGER_BINDING_SCHEMA_VERSION
    supplied_hash = value.pop("binding_hash", None)
    if supplied_hash != _hash(value):
        raise ForwardValidationError("history_ledger_binding hash is invalid")
    raw_receipts = value.get("receipts")
    if not isinstance(raw_receipts, list) or not raw_receipts:
        raise ForwardValidationError("history_ledger_binding receipts are missing")
    receipts = [_validate_history_record_receipt(item) for item in raw_receipts]
    expected_receipt_schema = (
        PREVIOUS_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION
        if historical
        else HISTORY_RECORD_RECEIPT_SCHEMA_VERSION
    )
    if any(
        receipt.get("schema_version") != expected_receipt_schema for receipt in receipts
    ):
        raise ForwardValidationError(
            "history_ledger_binding mixes incompatible receipt schemas"
        )
    fixture_ids = [receipt["fixture_id"] for receipt in receipts]
    if (
        fixture_ids != sorted(fixture_ids)
        or len(set(fixture_ids)) != len(fixture_ids)
        or value.get("fixture_ids") != fixture_ids
    ):
        raise ForwardValidationError(
            "history record receipts must be unique and canonically ordered by fixture_id"
        )
    value["receipts"] = receipts
    value["binding_hash"] = supplied_hash
    return value


def _validate_v3_input(
    payload: Any, *, require_history_ledger_binding: bool = True
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ForwardValidationError("forward observations must be a JSON object")
    value = dict(payload)
    if value.get("schema_version") != INPUT_SCHEMA_VERSION:
        raise ForwardValidationError("unsupported forward-observations schema_version")
    if "records" in value or "outcomes" in value:
        raise ForwardValidationError(
            "v3 separates pre-match commitments from settlements and uses market_schemas"
        )
    try:
        policy = forward_policy.validate_policy_manifest(
            value.get("policy_manifest") or {}
        )
        cohort = forward_policy.validate_cohort(value.get("cohort_manifest") or {})
    except forward_policy.ForwardPolicyError as exc:
        raise ForwardValidationError(
            "frozen policy/cohort manifest is invalid"
        ) from exc
    if cohort.get("status") != "active" or cohort.get("closed_at") is not None:
        raise ForwardValidationError(
            "v3 requires the original immutable active cohort manifest, "
            "not a rewritten closed pointer"
        )
    raw_closure = value.get("cohort_closure")
    closure = None
    if raw_closure is not None:
        if not isinstance(raw_closure, Mapping):
            raise ForwardValidationError("cohort_closure must be an object")
        try:
            closure = forward_policy.validate_closure(raw_closure, cohort=cohort)
        except forward_policy.ForwardPolicyError as exc:
            raise ForwardValidationError("cohort_closure is invalid") from exc
    for field in ("cohort_id", "policy_id", "policy_hash"):
        if not str(value.get(field) or "").strip():
            raise ForwardValidationError(f"{field} is required")
    if (
        value["cohort_id"] != cohort["cohort_id"]
        or value["policy_id"] != policy["policy_id"]
        or value["policy_hash"] != policy["policy_hash"]
        or cohort["policy_id"] != policy["policy_id"]
        or cohort["policy_hash"] != policy["policy_hash"]
    ):
        raise ForwardValidationError(
            "top-level policy/cohort assertions do not match the frozen manifests"
        )
    cohort_kind = str(cohort.get("kind") or "")
    policy_confirmation = policy.get("confirmation_contract")
    policy_cohort_kind = (
        str(policy_confirmation.get("cohort_kind") or "")
        if isinstance(policy_confirmation, Mapping)
        else ""
    )
    if (
        cohort.get("schema_version") != forward_policy.COHORT_SCHEMA_VERSION
        or not cohort_kind
        or cohort_kind != policy_cohort_kind
    ):
        raise ForwardValidationError(
            "v3 forward observations require matching explicit policy/cohort kinds"
        )
    protocol = _validation_protocol(policy)
    firm_execution_required = forward_policy._release_at_least(
        policy["software"]["package_version"], (3, 7, 0)
    )
    firm_execution_v2_required = forward_policy._release_at_least(
        policy["software"]["package_version"], (3, 8, 0)
    )
    schemas = _market_schemas(value.get("market_schemas"))
    queue = value.get("queue_manifest")
    if not isinstance(queue, Mapping):
        raise ForwardValidationError("queue_manifest is required")
    queue_fields = {
        "schema_version",
        "artifact_type",
        "queue_id",
        "cohort_id",
        "policy_id",
        "policy_hash",
        "frozen_at",
        "entries",
        "integrity_assurance",
        "queue_hash",
    }
    _exact_keys(queue, queue_fields, "queue_manifest")
    queue_without_hash = dict(queue)
    supplied_queue_hash = queue_without_hash.pop("queue_hash", None)
    if supplied_queue_hash != _hash(queue_without_hash):
        raise ForwardValidationError("queue_manifest hash is invalid")
    if (
        queue.get("schema_version") != QUEUE_SCHEMA_VERSION
        or queue.get("artifact_type") != "soccer_forward_eligibility_queue"
        or queue.get("integrity_assurance")
        != "local_content_hash_only_no_external_timestamp"
    ):
        raise ForwardValidationError("queue_manifest contract is invalid")
    if (
        queue.get("cohort_id") != cohort["cohort_id"]
        or queue.get("policy_id") != policy["policy_id"]
        or queue.get("policy_hash") != policy["policy_hash"]
    ):
        raise ForwardValidationError("queue_manifest does not bind the frozen cohort")
    if not str(queue.get("queue_id") or "").strip():
        raise ForwardValidationError("queue_manifest.queue_id is required")
    frozen_at = _aware(queue.get("frozen_at"), "queue_manifest.frozen_at")
    cohort_started = _aware(cohort.get("starts_at"), "cohort.starts_at")
    if frozen_at < cohort_started:
        raise ForwardValidationError("queue_manifest predates the cohort")
    raw_entries = queue.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ForwardValidationError("queue_manifest.entries must be non-empty")
    entries: dict[str, dict[str, Any]] = {}
    seen_fixture_market_identity: set[tuple[str, str]] = set()
    for index, raw_entry in enumerate(raw_entries):
        label = f"queue_manifest.entries[{index}]"
        if not isinstance(raw_entry, Mapping):
            raise ForwardValidationError(f"{label} must be an object")
        _exact_keys(
            raw_entry,
            {
                "fixture_id",
                "home_team",
                "away_team",
                "league",
                "market_identity",
                "market_identity_hash",
                "kickoff",
                "queue_key",
            },
            label,
        )
        fixture_id = str(raw_entry.get("fixture_id") or "").strip()
        home_team = str(raw_entry.get("home_team") or "").strip()
        away_team = str(raw_entry.get("away_team") or "").strip()
        league = str(raw_entry.get("league") or "").strip()
        identity, identity_hash = _market_identity(
            raw_entry.get("market_identity"),
            raw_entry.get("market_identity_hash"),
            f"{label}.market_identity",
        )
        market = identity["family"]
        if (
            not fixture_id
            or not home_team
            or not away_team
            or not league
            or not market
            or market not in schemas
        ):
            raise ForwardValidationError(f"{label} fixture/league/market is invalid")
        pair = (fixture_id, identity_hash)
        if pair in seen_fixture_market_identity:
            raise ForwardValidationError(
                "queue contains a duplicated fixture+market identity"
            )
        seen_fixture_market_identity.add(pair)
        expected_key = _queue_key(cohort["cohort_id"], fixture_id, identity_hash)
        if raw_entry.get("queue_key") != expected_key or expected_key in entries:
            raise ForwardValidationError(f"{label}.queue_key is invalid or duplicated")
        kickoff = _aware(raw_entry.get("kickoff"), f"{label}.kickoff")
        if frozen_at >= kickoff:
            raise ForwardValidationError(
                "eligibility queue must be frozen before kickoff"
            )
        entries[expected_key] = {
            "fixture_id": fixture_id,
            "home_team": home_team,
            "away_team": away_team,
            "league": league,
            "market": market,
            "market_identity": identity,
            "market_identity_hash": identity_hash,
            "kickoff": kickoff.isoformat(),
            "queue_key": expected_key,
        }

    commitments = value.get("commitments")
    if not isinstance(commitments, list) or not commitments:
        raise ForwardValidationError("commitments must be a non-empty array")
    by_observation: dict[str, dict[str, Any]] = {}
    committed_queue_keys: set[str] = set()
    execution_receipt_hashes: set[str] = set()
    rows: list[dict[str, Any]] = []
    expected_commitment_fields = {
        "schema_version",
        "prediction_payload",
        "forward_policy_binding",
        "commitment_hash",
    }
    expected_prediction_fields = {
        "queue_hash",
        "queue_key",
        "fixture_id",
        "home_team",
        "away_team",
        "observation_id",
        "league",
        "market_identity",
        "market_identity_hash",
        "kickoff",
        "generated_at",
        "lead_time_minutes",
        "status",
        "settlement_reference_outcome",
        "model_probabilities",
        "baselines",
        "baseline_lineage",
        "bookmaker_snapshot",
        "execution_entry",
        "unavailable_reasons",
        "provenance_binding",
    }
    for index, raw_commitment in enumerate(commitments):
        label = f"commitments[{index}]"
        if not isinstance(raw_commitment, Mapping):
            raise ForwardValidationError(f"{label} must be an object")
        _exact_keys(raw_commitment, expected_commitment_fields, label)
        commitment_without_hash = dict(raw_commitment)
        supplied_commitment_hash = commitment_without_hash.pop("commitment_hash", None)
        if supplied_commitment_hash != _hash(commitment_without_hash):
            raise ForwardValidationError(f"{label}.commitment_hash is invalid")
        if raw_commitment.get("schema_version") != COMMITMENT_SCHEMA_VERSION:
            raise ForwardValidationError(f"{label}.schema_version is unsupported")
        prediction = raw_commitment.get("prediction_payload")
        if not isinstance(prediction, Mapping):
            raise ForwardValidationError(f"{label}.prediction_payload is required")
        _exact_keys(
            prediction, expected_prediction_fields, f"{label}.prediction_payload"
        )
        prediction_hash = _hash(prediction)
        try:
            binding = forward_policy.validate_record_binding(
                raw_commitment.get("forward_policy_binding")
            )
        except forward_policy.ForwardPolicyError as exc:
            raise ForwardValidationError(
                f"{label}.forward_policy_binding is invalid"
            ) from exc
        if (
            binding is None
            or binding.get("schema_version")
            != forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
            or binding.get("observation_commitment_hash") != prediction_hash
            or binding.get("cohort_id") != cohort["cohort_id"]
            or binding.get("cohort_hash") != cohort["cohort_hash"]
            or binding.get("policy_id") != policy["policy_id"]
            or binding.get("policy_hash") != policy["policy_hash"]
            or binding.get("policy_snapshot") != policy
        ):
            raise ForwardValidationError(
                f"{label}.forward_policy_binding does not commit this prediction payload"
            )
        try:
            provenance_binding = forward_policy.validate_provenance_binding(
                prediction.get("provenance_binding"),
                policy_manifest=policy,
                cohort_id=str(cohort["cohort_id"]),
            )
        except forward_policy.ForwardPolicyError as exc:
            raise ForwardValidationError(
                f"{label}.prediction_payload provenance is invalid"
            ) from exc
        if provenance_binding != binding.get("provenance_binding"):
            raise ForwardValidationError(
                f"{label}.prediction_payload provenance does not match its record binding"
            )
        if forward_policy._policy_uses_role_aware_lineage(policy):
            request_binding = binding.get("cohort_request_binding")
            request_fixture = (
                request_binding.get("fixture")
                if isinstance(request_binding, Mapping)
                else None
            )
            if (
                isinstance(request_binding, Mapping)
                and request_binding.get("schema_version")
                == cohort_scope.REQUEST_BINDING_SCHEMA_VERSION
            ):
                expected_request_fixture = {
                    "fixture_id": str(prediction.get("fixture_id") or ""),
                    "competition_key": str(prediction.get("league") or ""),
                    "home_team": str(prediction.get("home_team") or "").strip(),
                    "away_team": str(prediction.get("away_team") or "").strip(),
                    "kickoff": _aware(
                        prediction.get("kickoff"), f"{label}.prediction_payload.kickoff"
                    ).isoformat(),
                }
                request_matches = request_fixture == expected_request_fixture
            elif isinstance(request_binding, Mapping):
                request_matches = request_binding.get("fixture_id") == str(
                    prediction.get("fixture_id") or ""
                )
            else:
                request_matches = False
            if not isinstance(request_binding, Mapping) or not request_matches:
                raise ForwardValidationError(
                    f"{label}.forward_policy_binding does not bind the requested fixture"
                )
        queue_key = str(prediction.get("queue_key") or "")
        if (
            prediction.get("queue_hash") != supplied_queue_hash
            or queue_key not in entries
        ):
            raise ForwardValidationError(
                f"{label} is not in the frozen eligibility queue"
            )
        if queue_key in committed_queue_keys:
            raise ForwardValidationError(
                "more than one commitment uses a fixture+market key"
            )
        committed_queue_keys.add(queue_key)
        entry = entries[queue_key]
        for field in (
            "fixture_id",
            "home_team",
            "away_team",
            "league",
            "kickoff",
        ):
            expected = entry[field]
            actual = prediction.get(field)
            if field == "kickoff":
                actual = _aware(
                    actual, f"{label}.prediction_payload.kickoff"
                ).isoformat()
            else:
                actual = str(actual or "").strip()
            if actual != expected:
                raise ForwardValidationError(
                    f"{label} does not match queue field {field}"
                )
        prediction_identity, prediction_identity_hash = _market_identity(
            prediction.get("market_identity"),
            prediction.get("market_identity_hash"),
            f"{label}.prediction_payload.market_identity",
        )
        if (
            prediction_identity != entry["market_identity"]
            or prediction_identity_hash != entry["market_identity_hash"]
        ):
            raise ForwardValidationError(
                f"{label} does not match queue market identity"
            )
        observation_id = str(prediction.get("observation_id") or "")
        if (
            observation_id != _observation_id(queue_key)
            or observation_id in by_observation
        ):
            raise ForwardValidationError(f"{label}.observation_id is not canonical")
        market = entry["market"]
        market_schema = schemas[market]
        settlement_states = market_schema["settlement_states"]
        price_outcomes = prediction_identity["price_outcomes"]
        if market_schema["settlement_semantics"] == "categorical" and set(
            settlement_states
        ) != set(price_outcomes):
            raise ForwardValidationError(
                f"{label} categorical settlement states must exactly match quoted price outcome keys"
            )
        bookmaker_proper_score_comparable = bool(
            market_schema["settlement_semantics"] == "categorical"
            and set(settlement_states) == set(price_outcomes)
        )
        kickoff = _aware(entry["kickoff"], f"{label}.kickoff")
        generated = _aware(prediction.get("generated_at"), f"{label}.generated_at")
        archived = _aware(binding.get("archived_at"), f"{label}.binding.archived_at")
        if generated < frozen_at or generated < cohort_started:
            raise ForwardValidationError(f"{label} predates the frozen cohort queue")
        if generated > archived or archived >= kickoff:
            raise ForwardValidationError(
                f"{label} requires generated_at <= archived_at < kickoff"
            )
        raw_lead = prediction.get("lead_time_minutes")
        if isinstance(raw_lead, bool):
            raise ForwardValidationError(f"{label}.lead_time_minutes must be numeric")
        try:
            lead = float(raw_lead)
        except (TypeError, ValueError) as exc:
            raise ForwardValidationError(
                f"{label}.lead_time_minutes must be numeric"
            ) from exc
        actual_lead = (kickoff - generated).total_seconds() / 60.0
        if lead < 0 or not math.isclose(lead, actual_lead, abs_tol=1.0):
            raise ForwardValidationError(
                f"{label}.lead_time_minutes does not match timestamps"
            )
        status = str(prediction.get("status") or "")
        if status not in {"predicted", "abstained", "unavailable"}:
            raise ForwardValidationError(f"{label}.status is invalid")
        reasons = prediction.get("unavailable_reasons")
        if not isinstance(reasons, list) or any(
            not str(item).strip() for item in reasons
        ):
            raise ForwardValidationError(f"{label}.unavailable_reasons must be strings")
        model = prediction.get("model_probabilities")
        raw_reference = prediction.get("settlement_reference_outcome")
        if market_schema["settlement_semantics"] == "five_state_return":
            if status == "unavailable":
                if raw_reference is not None:
                    raise ForwardValidationError(
                        f"{label} unavailable status cannot carry a settlement reference"
                    )
                settlement_reference_outcome = None
            else:
                settlement_reference_outcome = str(raw_reference or "").strip()
                if settlement_reference_outcome not in price_outcomes:
                    raise ForwardValidationError(
                        f"{label}.settlement_reference_outcome must be a quoted price outcome"
                    )
        elif raw_reference is not None:
            raise ForwardValidationError(
                f"{label} categorical market cannot carry a settlement reference"
            )
        else:
            settlement_reference_outcome = None
        baselines_raw = prediction.get("baselines")
        lineage_raw = prediction.get("baseline_lineage")
        bookmaker_raw = prediction.get("bookmaker_snapshot")
        if status == "unavailable":
            if not reasons or model is not None or baselines_raw not in ({}, None):
                raise ForwardValidationError(
                    f"{label} unavailable status must carry reasons and no probabilities"
                )
            if lineage_raw not in ({}, None) or bookmaker_raw is not None:
                raise ForwardValidationError(
                    f"{label} unavailable status cannot carry baseline evidence"
                )
            model_probabilities = None
            baselines: dict[str, dict[str, float]] = {}
            lineage: dict[str, Any] = {}
            bookmaker_snapshot = None
        else:
            if reasons:
                raise ForwardValidationError(
                    f"{label} modeled status cannot carry unavailable reasons"
                )
            model_probabilities = _probabilities(
                model, settlement_states, f"{label}.model_probabilities"
            )
            proper_score_baselines = (
                BASELINE_NAMES
                if bookmaker_proper_score_comparable
                else MODEL_SPACE_BASELINE_NAMES
            )
            if not isinstance(baselines_raw, Mapping) or set(baselines_raw) != set(
                proper_score_baselines
            ):
                raise ForwardValidationError(
                    f"{label}.baselines must contain every compatible proper-score baseline"
                )
            baselines = {
                name: _probabilities(
                    baselines_raw[name],
                    settlement_states,
                    f"{label}.baselines.{name}",
                )
                for name in proper_score_baselines
            }
            if not isinstance(lineage_raw, Mapping) or set(lineage_raw) != set(
                proper_score_baselines
            ):
                raise ForwardValidationError(
                    f"{label}.baseline_lineage must bind every compatible proper-score baseline"
                )
            lineage = {}
            for name in proper_score_baselines:
                raw_lineage = lineage_raw[name]
                if not isinstance(raw_lineage, Mapping):
                    raise ForwardValidationError(
                        f"{label}.baseline_lineage.{name} is invalid"
                    )
                _exact_keys(
                    raw_lineage,
                    {"kind", "generated_at", "training_cutoff", "artifact_hash"},
                    f"{label}.baseline_lineage.{name}",
                )
                lineage_generated = _aware(
                    raw_lineage.get("generated_at"),
                    f"{label}.baseline_lineage.{name}.generated_at",
                )
                cutoff = _aware(
                    raw_lineage.get("training_cutoff"),
                    f"{label}.baseline_lineage.{name}.training_cutoff",
                )
                if cutoff > lineage_generated or lineage_generated > archived:
                    raise ForwardValidationError(
                        f"{label}.baseline_lineage.{name} violates temporal causality"
                    )
                lineage[name] = {
                    "kind": str(raw_lineage.get("kind") or ""),
                    "generated_at": lineage_generated.isoformat(),
                    "training_cutoff": cutoff.isoformat(),
                    "artifact_hash": _sha256(
                        raw_lineage.get("artifact_hash"),
                        f"{label}.baseline_lineage.{name}.artifact_hash",
                    ),
                }
                if not lineage[name]["kind"]:
                    raise ForwardValidationError(
                        f"{label}.baseline_lineage.{name}.kind is required"
                    )
            if not isinstance(bookmaker_raw, Mapping):
                raise ForwardValidationError(f"{label}.bookmaker_snapshot is required")
            _exact_keys(
                bookmaker_raw,
                {
                    "collected_at",
                    "source_evidence_file",
                    "source_evidence_hash",
                    "source_url",
                    "firm_count",
                    "price_basis",
                    "odds_format",
                    "complete_market_odds",
                    "no_vig_method",
                },
                f"{label}.bookmaker_snapshot",
            )
            if bookmaker_raw.get("odds_format") != "decimal":
                raise ForwardValidationError(
                    "v3 bookmaker snapshots require decimal odds"
                )
            if bookmaker_raw.get("no_vig_method") != "multiplicative_normalization":
                raise ForwardValidationError("unsupported bookmaker no-vig method")
            bookmaker_collected = _aware(
                bookmaker_raw.get("collected_at"),
                f"{label}.bookmaker_snapshot.collected_at",
            )
            difference = abs((bookmaker_collected - generated).total_seconds()) / 60.0
            if (
                bookmaker_collected > archived
                or bookmaker_collected >= kickoff
                or difference > protocol["same_time_tolerance_minutes"]
            ):
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot is not available at the archived decision time"
                )
            complete_odds, derived_no_vig = _no_vig_from_complete_decimal_odds(
                bookmaker_raw.get("complete_market_odds"),
                price_outcomes,
                f"{label}.bookmaker_snapshot.complete_market_odds",
            )
            if bookmaker_proper_score_comparable:
                if any(
                    not math.isclose(
                        baselines["bookmaker_no_vig"][outcome],
                        derived_no_vig[outcome],
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                    for outcome in price_outcomes
                ):
                    raise ForwardValidationError(
                        f"{label}.bookmaker_no_vig does not recompute from complete odds"
                    )
            elif "bookmaker_no_vig" in baselines:
                raise ForwardValidationError(
                    f"{label}.bookmaker_no_vig cannot score a different outcome space"
                )
            source_hash = _sha256(
                bookmaker_raw.get("source_evidence_hash"),
                f"{label}.bookmaker_snapshot.source_evidence_hash",
            )
            evidence_file = str(bookmaker_raw.get("source_evidence_file") or "").strip()
            if not evidence_file:
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot.source_evidence_file is required"
                )
            try:
                replayed_evidence = source_evidence.validate_evidence_file(
                    evidence_file
                )
            except (source_evidence.SourceEvidenceError, OSError) as exc:
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot source evidence cannot be replayed"
                ) from exc
            if (
                replayed_evidence.get("schema_version")
                != source_evidence.EVIDENCE_SCHEMA_VERSION
            ):
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot requires canonical source-evidence/2.0.0"
                )
            if replayed_evidence.get("evidence_hash") != source_hash:
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot evidence hash does not match"
                )
            evidence_fixture = replayed_evidence.get("fixture")
            if not isinstance(evidence_fixture, Mapping) or any(
                str(evidence_fixture.get(field) or "") != str(entry[field])
                for field in ("home_team", "away_team")
            ):
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot evidence fixture does not match"
                )
            if (
                str(evidence_fixture.get("match_id") or "") != entry["fixture_id"]
                or _aware(
                    evidence_fixture.get("kickoff"),
                    f"{label}.bookmaker evidence kickoff",
                )
                != kickoff
                or _aware(
                    replayed_evidence.get("generated_at"),
                    f"{label}.bookmaker evidence generated_at",
                )
                > archived
            ):
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot evidence violates fixture/time binding"
                )
            try:
                source_binding = source_evidence.match_candidate(
                    replayed_evidence,
                    {
                        "market_identity": prediction_identity,
                        "market_identity_hash": prediction_identity_hash,
                        "market_collected_at": bookmaker_collected.isoformat(),
                        "odds_format": "decimal",
                        "price_basis": bookmaker_raw.get("price_basis"),
                        "market_source": bookmaker_raw.get("source_url"),
                        "firm_count": bookmaker_raw.get("firm_count"),
                        "complete_market_odds": complete_odds,
                    },
                )
            except source_evidence.SourceEvidenceError as exc:
                raise ForwardValidationError(
                    f"{label}.bookmaker_snapshot prices do not replay from evidence"
                ) from exc
            if bookmaker_proper_score_comparable:
                if (
                    lineage["bookmaker_no_vig"]["artifact_hash"] != source_hash
                    or _aware(
                        lineage["bookmaker_no_vig"]["generated_at"],
                        f"{label}.bookmaker lineage generated_at",
                    )
                    != bookmaker_collected
                ):
                    raise ForwardValidationError(
                        f"{label}.bookmaker baseline lineage does not bind its snapshot"
                    )
            bookmaker_snapshot = {
                "market_identity": prediction_identity,
                "market_identity_hash": prediction_identity_hash,
                "market_period": prediction_identity["period"],
                "market_family_period": (
                    f"{prediction_identity['family']}:{prediction_identity['period']}"
                ),
                "collected_at": bookmaker_collected.isoformat(),
                "source_evidence_hash": source_hash,
                "source_evidence_file": str(Path(evidence_file).resolve()),
                "source_binding": source_binding,
                "odds_format": "decimal",
                "complete_market_odds": complete_odds,
                "derived_no_vig": derived_no_vig,
                "no_vig_method": "multiplicative_normalization",
                "proper_score_status": (
                    "available_same_outcome_space"
                    if bookmaker_proper_score_comparable
                    else "unavailable_price_and_settlement_spaces_differ"
                ),
            }
        execution_entry_raw = prediction.get("execution_entry")
        execution_entry = None
        if execution_entry_raw is not None:
            if status != "predicted" or not isinstance(execution_entry_raw, Mapping):
                raise ForwardValidationError(
                    f"{label}.execution_entry is invalid for status"
                )
            expected_execution_fields = (
                {
                    "selection",
                    "firm_accepted_decimal_odds",
                    "accepted_at",
                    "entry_price_kind",
                    "limit_verified",
                    "stake_units",
                }
                if firm_execution_v2_required
                else {
                    "selection",
                    "entry_decimal_odds",
                    "entry_complete_market_odds",
                    "entry_collected_at",
                    "entry_source_evidence_hash",
                    "entry_price_kind",
                    "limit_verified",
                    "stake_units",
                }
            )
            if firm_execution_required:
                expected_execution_fields.update(
                    {"execution_evidence_file", "execution_evidence_hash"}
                )
            _exact_keys(
                execution_entry_raw,
                expected_execution_fields,
                f"{label}.execution_entry",
            )
            selection = str(execution_entry_raw.get("selection") or "").strip()
            if not selection:
                raise ForwardValidationError(
                    f"{label}.execution_entry.selection is required"
                )
            if selection not in price_outcomes:
                raise ForwardValidationError(
                    f"{label}.execution_entry.selection is invalid"
                )
            if (
                market_schema["settlement_semantics"] == "five_state_return"
                and selection != settlement_reference_outcome
            ):
                raise ForwardValidationError(
                    f"{label}.execution_entry.selection must equal the settlement reference outcome"
                )
            entry_collected = _aware(
                execution_entry_raw.get(
                    "accepted_at"
                    if firm_execution_v2_required
                    else "entry_collected_at"
                ),
                f"{label}.execution_entry.accepted_at",
            )
            if (
                entry_collected > archived
                or entry_collected >= kickoff
                or abs((entry_collected - generated).total_seconds()) / 60.0
                > protocol["same_time_tolerance_minutes"]
            ):
                raise ForwardValidationError(
                    f"{label}.execution entry was not available at archive time"
                )
            raw_stake = execution_entry_raw.get("stake_units")
            try:
                stake = float(raw_stake)
            except (TypeError, ValueError) as exc:
                raise ForwardValidationError(
                    f"{label}.stake_units must be numeric"
                ) from exc
            if not math.isfinite(stake) or stake <= 0.0:
                raise ForwardValidationError(f"{label}.stake_units must be positive")
            expected_price_kind = (
                "firm_accepted_offer"
                if firm_execution_required
                else "executable_after_slippage"
            )
            if execution_entry_raw.get("entry_price_kind") != expected_price_kind:
                raise ForwardValidationError(f"{label}.entry_price_kind is invalid")
            if execution_entry_raw.get("limit_verified") is not True:
                raise ForwardValidationError(f"{label}.entry limit must be verified")
            entry_complete_odds = None
            entry_no_vig = None
            if firm_execution_v2_required:
                entry_price = _positive_decimal_price(
                    execution_entry_raw.get("firm_accepted_decimal_odds"),
                    f"{label}.execution_entry.firm_accepted_decimal_odds",
                )
                entry_source_hash = None
            else:
                entry_complete_odds, entry_no_vig = _no_vig_from_complete_decimal_odds(
                    execution_entry_raw.get("entry_complete_market_odds"),
                    price_outcomes,
                    f"{label}.execution_entry.entry_complete_market_odds",
                )
                entry_price = _positive_decimal_price(
                    execution_entry_raw.get("entry_decimal_odds"),
                    f"{label}.execution_entry.entry_decimal_odds",
                )
                if not math.isclose(
                    entry_price,
                    entry_complete_odds[selection],
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    raise ForwardValidationError(
                        f"{label}.entry price is not in complete market odds"
                    )
                entry_source_hash = _sha256(
                    execution_entry_raw.get("entry_source_evidence_hash"),
                    f"{label}.execution_entry.entry_source_evidence_hash",
                )
                if entry_source_hash != source_hash:
                    raise ForwardValidationError(
                        f"{label}.execution_entry source evidence does not bind the replayed bookmaker snapshot"
                    )
                if entry_collected != bookmaker_collected:
                    raise ForwardValidationError(
                        f"{label}.execution_entry time does not bind the replayed bookmaker snapshot"
                    )
                for outcome in price_outcomes:
                    quoted_price = complete_odds[outcome]
                    executable_price = entry_complete_odds[outcome]
                    if outcome == selection:
                        if executable_price > quoted_price + 1e-12:
                            raise ForwardValidationError(
                                f"{label}.execution_entry cannot improve the selected replayed price"
                            )
                    elif not math.isclose(
                        executable_price,
                        quoted_price,
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    ):
                        raise ForwardValidationError(
                            f"{label}.execution_entry may only apply conservative slippage to the selected price"
                        )
            execution_binding = None
            if firm_execution_required:
                execution_evidence_file = str(
                    execution_entry_raw.get("execution_evidence_file") or ""
                ).strip()
                try:
                    replayed_offer = execution_evidence.validate_evidence_file(
                        execution_evidence_file
                    )
                except (execution_evidence.ExecutionEvidenceError, OSError) as exc:
                    raise ForwardValidationError(
                        f"{label}.execution_entry firm offer cannot be replayed"
                    ) from exc
                execution_evidence_hash = _sha256(
                    execution_entry_raw.get("execution_evidence_hash"),
                    f"{label}.execution_entry.execution_evidence_hash",
                )
                if replayed_offer.get("evidence_hash") != execution_evidence_hash:
                    raise ForwardValidationError(
                        f"{label}.execution_entry evidence hash does not match"
                    )
                try:
                    execution_binding = execution_evidence.match_offer(
                        replayed_offer,
                        fixture={
                            "match_id": entry["fixture_id"],
                            "home_team": entry["home_team"],
                            "away_team": entry["away_team"],
                            "kickoff": kickoff.isoformat(),
                        },
                        market_identity=prediction_identity,
                        selection=selection,
                        accepted_at=entry_collected.isoformat(),
                        accepted_decimal_odds=entry_price,
                        stake_units=stake,
                    )
                except execution_evidence.ExecutionEvidenceError as exc:
                    raise ForwardValidationError(
                        f"{label}.execution_entry does not match its accepted firm offer"
                    ) from exc
                receipt_hash = _sha256(
                    execution_binding.get("receipt_identity_hash"),
                    f"{label}.execution_entry receipt identity hash",
                )
                if receipt_hash in execution_receipt_hashes:
                    raise ForwardValidationError(
                        "one firm/account/receipt identity cannot fund multiple cohort entries"
                    )
                execution_receipt_hashes.add(receipt_hash)
                accepted_at = _aware(
                    replayed_offer.get("accepted_at"),
                    f"{label}.execution_entry accepted_at",
                )
                if accepted_at > archived or accepted_at >= kickoff:
                    raise ForwardValidationError(
                        f"{label}.execution_entry accepted offer is not pre-archive"
                    )
            execution_entry = {
                "selection": selection,
                "model_expected_roi": _model_expected_return(
                    model_probabilities,
                    selection=selection,
                    decimal_odds=entry_price,
                    settlement_semantics=market_schema["settlement_semantics"],
                ),
                "entry_price_kind": expected_price_kind,
                "limit_verified": True,
                "stake_units": stake,
            }
            if firm_execution_v2_required:
                execution_entry.update(
                    {
                        "firm_accepted_decimal_odds": entry_price,
                        "accepted_at": entry_collected.isoformat(),
                        "decision_consensus_no_vig_probability": derived_no_vig[
                            selection
                        ],
                        "firm_complete_market_no_vig_probability": None,
                        "firm_complete_market_status": "unavailable_not_captured",
                    }
                )
            else:
                execution_entry.update(
                    {
                        "entry_decimal_odds": entry_price,
                        "entry_complete_market_odds": entry_complete_odds,
                        "entry_no_vig_probability": entry_no_vig[selection],
                        "entry_collected_at": entry_collected.isoformat(),
                        "entry_source_evidence_hash": entry_source_hash,
                    }
                )
            if execution_binding is not None:
                execution_entry.update(
                    {
                        "execution_evidence_file": str(
                            Path(execution_evidence_file).resolve()
                        ),
                        "execution_evidence_hash": execution_evidence_hash,
                        "execution_binding": execution_binding,
                    }
                )
        by_observation[observation_id] = {
            "commitment_hash": supplied_commitment_hash,
            "entry": entry,
            "market_schema": market_schema,
            "market_identity": prediction_identity,
            "market_identity_hash": prediction_identity_hash,
            "settlement_reference_outcome": settlement_reference_outcome,
            "bookmaker_proper_score_comparable": bookmaker_proper_score_comparable,
            "execution_entry": execution_entry,
            "kickoff": kickoff,
        }
        rows.append(
            {
                "fixture_id": entry["fixture_id"],
                "home_team": entry["home_team"],
                "away_team": entry["away_team"],
                "observation_id": observation_id,
                "league": entry["league"],
                "market": market,
                "market_identity": prediction_identity,
                "market_identity_hash": prediction_identity_hash,
                "market_period": prediction_identity["period"],
                "market_family_period": (
                    f"{prediction_identity['family']}:{prediction_identity['period']}"
                ),
                "market_semantics": market_schema["settlement_semantics"],
                "settlement_states": settlement_states,
                "settlement_reference_outcome": settlement_reference_outcome,
                "bookmaker_proper_score_status": (
                    "available_same_outcome_space"
                    if bookmaker_proper_score_comparable
                    else "unavailable_price_and_settlement_spaces_differ"
                ),
                "kickoff": kickoff.isoformat(),
                "generated_at": generated.isoformat(),
                "lead_time_minutes": lead,
                "lead_time_bucket": _lead_bucket(lead),
                "kickoff_week": f"{kickoff.isocalendar().year}-W{kickoff.isocalendar().week:02d}",
                "status": status,
                "model_probabilities": model_probabilities,
                "baselines": baselines,
                "baseline_lineage": lineage,
                "bookmaker_snapshot": bookmaker_snapshot,
                "forward_policy_binding": binding,
                "provenance_binding": provenance_binding,
                "execution_entry": execution_entry,
                "execution": None,
                "observed_settlement_state": None,
                "unverified_observed_settlement_state": None,
                "settlement_status": "pending",
                "formal_evaluation_eligible": False,
                "formal_evaluation_blockers": [],
                "unverified_execution_diagnostic": None,
            }
        )
    if committed_queue_keys != set(entries):
        missing = sorted(set(entries) - committed_queue_keys)
        extra = sorted(committed_queue_keys - set(entries))
        raise ForwardValidationError(
            f"commitments do not exactly cover the frozen queue (missing={missing}, extra={extra})"
        )

    settlements = value.get("settlements")
    if not isinstance(settlements, list):
        raise ForwardValidationError("settlements must be an array")
    rows_by_id = {row["observation_id"]: row for row in rows}
    settled_ids: set[str] = set()
    settlement_fields = {
        "schema_version",
        "observation_id",
        "commitment_hash",
        "status",
        "observed_settlement_state",
        "result_collected_at",
        "result_source_evidence_hash",
        "closing_snapshot",
        "settlement_hash",
    }
    for index, raw_settlement in enumerate(settlements):
        label = f"settlements[{index}]"
        if not isinstance(raw_settlement, Mapping):
            raise ForwardValidationError(f"{label} must be an object")
        _exact_keys(raw_settlement, settlement_fields, label)
        without_hash = dict(raw_settlement)
        supplied_hash = without_hash.pop("settlement_hash", None)
        if supplied_hash != _hash(without_hash):
            raise ForwardValidationError(f"{label}.settlement_hash is invalid")
        if raw_settlement.get("schema_version") != SETTLEMENT_SCHEMA_VERSION:
            raise ForwardValidationError(f"{label}.schema_version is unsupported")
        observation_id = str(raw_settlement.get("observation_id") or "")
        if observation_id not in by_observation or observation_id in settled_ids:
            raise ForwardValidationError(
                f"{label}.observation_id is invalid or duplicated"
            )
        settled_ids.add(observation_id)
        metadata = by_observation[observation_id]
        if raw_settlement.get("commitment_hash") != metadata["commitment_hash"]:
            raise ForwardValidationError(
                f"{label} does not bind its pre-match commitment"
            )
        row = rows_by_id[observation_id]
        status = str(raw_settlement.get("status") or "")
        actual = raw_settlement.get("observed_settlement_state")
        result_collected_raw = raw_settlement.get("result_collected_at")
        result_hash_raw = raw_settlement.get("result_source_evidence_hash")
        if status == "pending":
            if (
                actual is not None
                or result_collected_raw is not None
                or result_hash_raw is not None
            ):
                raise ForwardValidationError(
                    f"{label} pending settlement carries a result"
                )
        elif status == "settled":
            settlement_states = metadata["market_schema"]["settlement_states"]
            if actual not in settlement_states:
                raise ForwardValidationError(
                    f"{label}.observed_settlement_state is invalid"
                )
            result_collected = _aware(
                result_collected_raw, f"{label}.result_collected_at"
            )
            if result_collected < metadata["kickoff"]:
                raise ForwardValidationError(
                    f"{label} result was collected before kickoff"
                )
            _sha256(result_hash_raw, f"{label}.result_source_evidence_hash")
            row["unverified_observed_settlement_state"] = actual
            row["settlement_status"] = "quarantined_unreplayable_result"
            row["formal_evaluation_blockers"].append(
                "result_evidence_replay_unavailable"
            )
        else:
            raise ForwardValidationError(f"{label}.status must be pending or settled")
        closing_raw = raw_settlement.get("closing_snapshot")
        entry = metadata["execution_entry"]
        if closing_raw is not None:
            if entry is None or not isinstance(closing_raw, Mapping):
                raise ForwardValidationError(
                    f"{label}.closing_snapshot has no committed entry"
                )
            _exact_keys(
                closing_raw,
                {"collected_at", "source_evidence_hash", "complete_market_odds"},
                f"{label}.closing_snapshot",
            )
            price_outcomes = list(metadata["market_identity"]["price_outcomes"])
            closing_odds, closing_no_vig = _no_vig_from_complete_decimal_odds(
                closing_raw.get("complete_market_odds"),
                price_outcomes,
                f"{label}.closing_snapshot.complete_market_odds",
            )
            closing_collected = _aware(
                closing_raw.get("collected_at"),
                f"{label}.closing_snapshot.collected_at",
            )
            entry_collected = _aware(
                entry.get("accepted_at", entry.get("entry_collected_at")),
                f"{label}.entry_collected_at",
            )
            if (
                closing_collected < entry_collected
                or closing_collected >= metadata["kickoff"]
            ):
                raise ForwardValidationError(
                    f"{label}.closing_snapshot time sequence is invalid"
                )
            selection = entry["selection"]
            settlement_state = None
            if status == "settled":
                if metadata["market_schema"]["settlement_semantics"] == "categorical":
                    settlement_state = "full_win" if selection == actual else "loss"
                else:
                    settlement_state = str(actual)
            row["unverified_execution_diagnostic"] = {
                **entry,
                "closing_decimal_odds": closing_odds[selection],
                "closing_complete_market_odds": closing_odds,
                "closing_no_vig_probability": closing_no_vig[selection],
                "closing_collected_at": closing_collected.isoformat(),
                "closing_source_evidence_hash": _sha256(
                    closing_raw.get("source_evidence_hash"),
                    f"{label}.closing_snapshot.source_evidence_hash",
                ),
                "settlement_state": settlement_state,
            }
            row["formal_evaluation_blockers"].append(
                "closing_evidence_replay_unavailable"
            )
    if settled_ids != set(by_observation):
        missing = sorted(set(by_observation) - settled_ids)
        raise ForwardValidationError(
            f"settlements must explicitly include pending rows (missing={missing})"
        )
    if require_history_ledger_binding:
        raise ForwardValidationError(
            "formal v3 evaluation requires a memory-store cohort aggregate export"
        )
    value["policy_manifest"] = policy
    value["cohort_manifest"] = cohort
    value["cohort_closure"] = closure
    value["cohort_closed"] = closure is not None
    value["queue_manifest"] = dict(queue)
    value["market_schemas"] = schemas
    value["records"] = rows
    value["validation_protocol"] = protocol
    value["provenance_binding"] = rows[0]["provenance_binding"]
    value["history_ledger_binding"] = None
    value["evidence_contract"] = (
        "v3_pre_match_replay_with_learning_only_unreplayable_result_quarantine"
    )
    value["legacy_uncommitted"] = False
    value["external_timestamp_anchor"] = False
    value["baseline_artifact_replay_complete"] = False
    value["execution_price_source_replay_complete"] = all(
        metadata.get("execution_entry") is None
        or isinstance(
            metadata.get("execution_entry", {}).get("execution_binding"), Mapping
        )
        for metadata in by_observation.values()
    )
    value["result_source_replay_complete"] = False
    value["closing_price_source_replay_complete"] = False
    return value


def _validate_aggregate_input(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ForwardValidationError("forward observations must be a JSON object")
    value = deepcopy(dict(payload))
    required = {
        "schema_version",
        "artifact_type",
        "cohort_id",
        "policy_id",
        "policy_hash",
        "policy_manifest",
        "cohort_manifest",
        "cohort_closure",
        "history_ledger_binding",
    }
    _exact_keys(value, required, "forward cohort aggregate")
    if (
        value.get("schema_version") != INPUT_SCHEMA_VERSION
        or value.get("artifact_type") != HISTORY_AGGREGATE_ARTIFACT_TYPE
    ):
        raise ForwardValidationError(
            "formal v3 evaluation requires a memory-store cohort aggregate export"
        )
    raw_closure = value.get("cohort_closure")
    if not isinstance(raw_closure, Mapping):
        raise ForwardValidationError(
            "formal v3 evaluation requires a closed cohort with a complete record manifest"
        )
    try:
        policy = forward_policy.validate_policy_manifest(
            value.get("policy_manifest") or {}
        )
        cohort = forward_policy.validate_cohort(value.get("cohort_manifest") or {})
        if (
            value.get("policy_id") != policy.get("policy_id")
            or value.get("policy_hash") != policy.get("policy_hash")
            or cohort.get("policy_id") != policy.get("policy_id")
            or cohort.get("policy_hash") != policy.get("policy_hash")
            or value.get("cohort_id") != cohort.get("cohort_id")
        ):
            raise forward_policy.ForwardPolicyError(
                "forward cohort aggregate policy/cohort binding is invalid"
            )
        schema_contract = forward_policy.closure_schema_contract(
            policy["software"]["package_version"]
        )
        closure = forward_policy.validate_closure(
            raw_closure,
            cohort=cohort,
            require_record_manifest=True,
            required_closure_schema=schema_contract["closure"],
            required_record_manifest_schema=schema_contract["record_manifest"],
            required_denominator_schema=schema_contract["denominator"],
        )
    except forward_policy.ForwardPolicyError as exc:
        raise ForwardValidationError(
            "formal v3 cohort closure or complete record manifest is invalid"
        ) from exc
    value["cohort_closure"] = closure
    binding = _validate_history_ledger_binding(value.get("history_ledger_binding"))
    for field in ("cohort_id", "policy_id", "policy_hash"):
        if value.get(field) != binding.get(field):
            raise ForwardValidationError(
                f"forward cohort aggregate {field} does not match its history binding"
            )
    manifest_entries = closure["record_manifest"]["records"]
    if [entry.get("fixture_id") for entry in manifest_entries] != binding.get(
        "fixture_ids"
    ):
        raise ForwardValidationError(
            "history record receipts do not exactly cover the closed cohort record manifest"
        )
    prevalidated_ledgers = [
        _validate_v3_input(
            receipt["ledger_payload"], require_history_ledger_binding=False
        )
        for receipt in binding["receipts"]
    ]
    receipt_entries = []
    for receipt, normalized_receipt in zip(
        binding["receipts"], prevalidated_ledgers, strict=True
    ):
        receipt_entry = {
            "fixture_id": receipt["fixture_id"],
            "archive_version_hash": receipt["archive_version_hash"],
            "record_commitment_hash": receipt["record_commitment_hash"],
            "record_binding_hash": receipt["record_binding_hash"],
            "prematch_ledger_hash": receipt["prematch_ledger_hash"],
        }
        snapshot = receipt.get("archive_snapshot_payload")
        record_binding = (
            snapshot.get("forward_policy_binding")
            if isinstance(snapshot, Mapping)
            else None
        )
        request = (
            record_binding.get("cohort_request_binding")
            if isinstance(record_binding, Mapping)
            else None
        )
        if request is not None:
            receipt_entry["request_event_hash"] = request.get("request_event_hash")
            manifest_schema = closure["record_manifest"].get("schema_version")
            if manifest_schema in {
                forward_policy.PREVIOUS_FULL_RECORD_MANIFEST_SCHEMA_VERSION,
                forward_policy.PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
                forward_policy.RECORD_MANIFEST_SCHEMA_VERSION,
            }:
                receipt_entry.update(
                    {
                        "request_fixture_id": request.get("request_fixture_id"),
                        "fixture": deepcopy(request.get("fixture")),
                        "fixture_event_hash": request.get("fixture_event_hash"),
                        "execution_receipt_hashes": sorted(
                            str(binding_item["receipt_identity_hash"])
                            for row in normalized_receipt["records"]
                            if isinstance(row.get("execution_entry"), Mapping)
                            and isinstance(
                                binding_item := row["execution_entry"].get(
                                    "execution_binding"
                                ),
                                Mapping,
                            )
                        ),
                    }
                )
                if manifest_schema in {
                    forward_policy.PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
                    forward_policy.RECORD_MANIFEST_SCHEMA_VERSION,
                }:
                    receipt_entry["fixture_event_at"] = request.get("fixture_event_at")
                if manifest_schema == forward_policy.RECORD_MANIFEST_SCHEMA_VERSION:
                    receipt_entry["record_archived_at"] = receipt.get(
                        "record_archived_at"
                    )
        receipt_entries.append(receipt_entry)
    if receipt_entries != manifest_entries:
        raise ForwardValidationError(
            "history record receipts do not exactly cover the closed cohort record manifest"
        )

    normalized_ledgers: list[dict[str, Any]] = []
    for receipt, normalized in zip(
        binding["receipts"], prevalidated_ledgers, strict=True
    ):
        if (
            normalized["cohort_id"] != value["cohort_id"]
            or normalized["policy_id"] != value["policy_id"]
            or normalized["policy_hash"] != value["policy_hash"]
            or normalized["policy_manifest"] != value["policy_manifest"]
            or normalized["cohort_manifest"] != value["cohort_manifest"]
            or normalized["cohort_closure"] != value["cohort_closure"]
        ):
            raise ForwardValidationError(
                "history record receipt does not match the aggregate cohort/policy"
            )
        if any(
            row.get("fixture_id") != receipt["fixture_id"]
            for row in normalized["records"]
        ):
            raise ForwardValidationError(
                "history record receipt contains rows for another fixture"
            )
        normalized_ledgers.append(normalized)

    cohort_receipt_hashes: set[str] = set()
    for normalized in normalized_ledgers:
        for row in normalized["records"]:
            entry = row.get("execution_entry")
            execution_binding = (
                entry.get("execution_binding") if isinstance(entry, Mapping) else None
            )
            receipt_hash = (
                execution_binding.get("receipt_identity_hash")
                if isinstance(execution_binding, Mapping)
                else None
            )
            if receipt_hash is None:
                continue
            normalized_hash = _sha256(
                receipt_hash, "cohort execution receipt identity hash"
            )
            if normalized_hash in cohort_receipt_hashes:
                raise ForwardValidationError(
                    "cohort reuses one firm/account/receipt identity across records"
                )
            cohort_receipt_hashes.add(normalized_hash)

    records: list[dict[str, Any]] = []
    market_schemas: dict[str, dict[str, Any]] = {}
    queue_manifests: list[dict[str, Any]] = []
    observation_ids: set[str] = set()
    provenance_binding: dict[str, Any] | None = None
    validation_protocol: dict[str, Any] | None = None
    for normalized in normalized_ledgers:
        for market, schema in normalized["market_schemas"].items():
            existing = market_schemas.get(market)
            if existing is not None and existing != schema:
                raise ForwardValidationError(
                    f"aggregate market schema differs across receipts: {market}"
                )
            market_schemas[market] = deepcopy(schema)
        queue_manifests.append(deepcopy(normalized["queue_manifest"]))
        if provenance_binding is None:
            provenance_binding = deepcopy(normalized["provenance_binding"])
            validation_protocol = deepcopy(normalized["validation_protocol"])
        elif (
            provenance_binding != normalized["provenance_binding"]
            or validation_protocol != normalized["validation_protocol"]
        ):
            raise ForwardValidationError(
                "aggregate receipts do not share one frozen provenance/configuration"
            )
        for row in normalized["records"]:
            observation_id = str(row.get("observation_id") or "")
            if not observation_id or observation_id in observation_ids:
                raise ForwardValidationError(
                    "aggregate receipts contain duplicate observation IDs"
                )
            observation_ids.add(observation_id)
            records.append(deepcopy(row))

    records.sort(
        key=lambda row: (
            str(row.get("fixture_id") or ""),
            str(row.get("market") or ""),
            str(row.get("observation_id") or ""),
        )
    )
    value["history_ledger_binding"] = binding
    value["market_schemas"] = market_schemas
    value["queue_manifest"] = None
    value["queue_manifests"] = queue_manifests
    value["records"] = records
    value["validation_protocol"] = validation_protocol
    value["provenance_binding"] = provenance_binding
    value["cohort_closed"] = True
    value["evidence_contract"] = (
        "v3_memory_store_receipts_with_learning_only_result_quarantine"
    )
    value["legacy_uncommitted"] = False
    value["external_timestamp_anchor"] = False
    value["baseline_artifact_replay_complete"] = all(
        item.get("baseline_artifact_replay_complete") is True
        for item in normalized_ledgers
    )
    value["execution_price_source_replay_complete"] = all(
        item.get("execution_price_source_replay_complete") is True
        for item in normalized_ledgers
    )
    value["result_source_replay_complete"] = all(
        item.get("result_source_replay_complete") is True for item in normalized_ledgers
    )
    value["closing_price_source_replay_complete"] = all(
        item.get("closing_price_source_replay_complete") is True
        for item in normalized_ledgers
    )
    return value


def validate_input(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ForwardValidationError("forward observations must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version == INPUT_SCHEMA_VERSION:
        return _validate_aggregate_input(payload)
    if schema_version == LEGACY_INPUT_SCHEMA_VERSION:
        return _validate_legacy_input(payload)
    raise ForwardValidationError("unsupported forward-observations schema_version")


def validate_prematch_input(payload: Any) -> dict[str, Any]:
    """Validate an un-settled v3 ledger before it is atomically archived.

    This entry point is intentionally narrower than :func:`validate_input`: every queue
    key must already have a content-addressed commitment and an explicit pending
    settlement, and no cohort closure or result may be present.  ``memory_store`` uses it
    before writing the immutable archive; it is not an evaluation shortcut.
    """

    if isinstance(payload, Mapping) and "history_ledger_binding" in payload:
        raise ForwardValidationError(
            "pre-match ledger cannot carry a post-archive history binding"
        )
    raw_settlements = (
        payload.get("settlements") if isinstance(payload, Mapping) else None
    )
    if not isinstance(raw_settlements, list) or any(
        not isinstance(item, Mapping) or item.get("closing_snapshot") is not None
        for item in raw_settlements
    ):
        raise ForwardValidationError(
            "pre-match ledger cannot carry a later closing snapshot"
        )
    normalized = _validate_v3_input(payload, require_history_ledger_binding=False)
    if normalized.get("cohort_closure") is not None:
        raise ForwardValidationError("pre-match ledger cannot carry a cohort closure")
    if any(
        row.get("settlement_status") != "pending" or _observed_state(row) is not None
        for row in normalized.get("records", [])
    ):
        raise ForwardValidationError("pre-match ledger must contain only pending rows")
    return normalized


def _percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _paired_deltas(
    rows: Sequence[Mapping[str, Any]], baseline: str
) -> list[tuple[str, float, float]]:
    values: list[tuple[str, float, float]] = []
    for row in rows:
        actual = _observed_state(row)
        model = row.get("model_probabilities")
        baseline_probabilities = row.get("baselines", {}).get(baseline)
        if actual is None or model is None or baseline_probabilities is None:
            continue
        model_log, model_brier = _losses(model, str(actual))
        baseline_log, baseline_brier = _losses(baseline_probabilities, str(actual))
        values.append(
            (
                str(row["kickoff_week"]),
                model_log - baseline_log,
                model_brier - baseline_brier,
            )
        )
    return values


def _cluster_bootstrap(
    values: Sequence[tuple[str, float, float]], *, repetitions: int, seed: int
) -> dict[str, Any]:
    if not values:
        return {
            "sample_count": 0,
            "cluster_count": 0,
            "log_loss_delta": None,
            "brier_delta": None,
        }
    clusters: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for cluster, log_delta, brier_delta in values:
        clusters[cluster].append((log_delta, brier_delta))
    names = sorted(clusters)
    rng = random.Random(seed)
    log_bootstrap: list[float] = []
    brier_bootstrap: list[float] = []
    for _ in range(repetitions):
        sampled: list[tuple[float, float]] = []
        for _cluster in names:
            selected = names[rng.randrange(len(names))]
            sampled.extend(clusters[selected])
        log_bootstrap.append(mean(item[0] for item in sampled))
        brier_bootstrap.append(mean(item[1] for item in sampled))
    log_values = [item[1] for item in values]
    brier_values = [item[2] for item in values]

    def block(items: Sequence[float], boot: Sequence[float]) -> dict[str, float | None]:
        return {
            "mean": mean(items),
            "ci95_low": _percentile(boot, 0.025),
            "ci95_high": _percentile(boot, 0.975),
        }

    return {
        "sample_count": len(values),
        "cluster_count": len(names),
        "cluster_unit": "kickoff_iso_week",
        "bootstrap_repetitions": repetitions,
        "bootstrap_seed": seed,
        "log_loss_delta": block(log_values, log_bootstrap),
        "brier_delta": block(brier_values, brier_bootstrap),
    }


def _calibration(rows: Sequence[Mapping[str, Any]], bins: int = 10) -> dict[str, Any]:
    values: list[tuple[str, float, float]] = []
    for row in rows:
        actual = _observed_state(row)
        probabilities = row.get("model_probabilities")
        if actual is None or not isinstance(probabilities, Mapping):
            continue
        for outcome, probability in probabilities.items():
            values.append(
                (str(outcome), float(probability), 1.0 if outcome == actual else 0.0)
            )

    def calibration_block(
        selected_values: Sequence[tuple[str, float, float]],
    ) -> dict[str, Any]:
        output: list[dict[str, Any]] = []
        weighted_gap = 0.0
        for index in range(bins):
            lower = index / bins
            upper = (index + 1) / bins
            selected = [
                item
                for item in selected_values
                if lower <= item[1] < upper or (index == bins - 1 and item[1] == 1.0)
            ]
            if not selected:
                continue
            expected = mean(item[1] for item in selected)
            observed = mean(item[2] for item in selected)
            gap = observed - expected
            weighted_gap += len(selected) * abs(gap)
            output.append(
                {
                    "lower": lower,
                    "upper": upper,
                    "count": len(selected),
                    "expected": expected,
                    "observed": observed,
                    "gap": gap,
                }
            )
        return {
            "bins": output,
            "expected_calibration_error": (
                weighted_gap / len(selected_values) if selected_values else None
            ),
            "sampled_class_probabilities": len(selected_values),
        }

    overall = calibration_block(values)
    outcomes = sorted({item[0] for item in values})
    return {
        "method": "classwise_equal_width_bins",
        **overall,
        "by_outcome": {
            outcome: calibration_block([item for item in values if item[0] == outcome])
            for outcome in outcomes
        },
    }


def _execution(
    rows: Sequence[Mapping[str, Any]], *, repetitions: int, seed: int
) -> dict[str, Any]:
    observations: list[tuple[str, float, float, float]] = []
    for row in rows:
        execution = row.get("execution")
        if not isinstance(execution, Mapping):
            continue
        entry = float(
            execution.get(
                "firm_accepted_decimal_odds", execution.get("entry_decimal_odds")
            )
        )
        stake = float(execution["stake_units"])
        settlement_state = execution.get("settlement_state")
        win_profit = entry - 1.0
        profit = {
            "full_win": win_profit,
            "half_win": win_profit / 2.0,
            "push": 0.0,
            "half_loss": -0.5,
            "loss": -1.0,
        }.get(str(settlement_state))
        if profit is None:
            continue
        clv_probability_points = 100.0 * (
            float(execution["closing_no_vig_probability"])
            - float(
                execution.get(
                    "decision_consensus_no_vig_probability",
                    execution.get("entry_no_vig_probability"),
                )
            )
        )
        observations.append(
            (str(row["kickoff_week"]), profit, clv_probability_points, stake)
        )
    if not observations:
        return {
            "sample_count": 0,
            "flat_stake_roi": None,
            "realized_stake_weighted_roi": None,
            "mean_clv_probability_points": None,
            "roi_ci95": None,
        }
    grouped: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for cluster, profit, clv, stake in observations:
        grouped[cluster].append((profit, clv, stake))
    names = sorted(grouped)
    rng = random.Random(seed)
    roi_boot: list[float] = []
    realized_roi_boot: list[float] = []
    clv_boot: list[float] = []
    for _ in range(repetitions):
        sample: list[tuple[float, float, float]] = []
        for _cluster in names:
            sample.extend(grouped[names[rng.randrange(len(names))]])
        roi_boot.append(mean(item[0] for item in sample))
        clv_boot.append(mean(item[1] for item in sample))
        realized_roi_boot.append(
            math.fsum(item[0] * item[2] for item in sample)
            / math.fsum(item[2] for item in sample)
        )
    profits = [item[1] for item in observations]
    clvs = [item[2] for item in observations]
    realized_profit = math.fsum(item[1] * item[3] for item in observations)
    total_stake = math.fsum(item[3] for item in observations)
    return {
        "sample_count": len(observations),
        "cluster_count": len(names),
        "flat_stake_roi": mean(profits),
        "realized_stake_weighted_roi": realized_profit / total_stake,
        "total_stake_units": total_stake,
        "mean_clv_probability_points": mean(clvs),
        "roi_ci95": {
            "low": _percentile(roi_boot, 0.025),
            "high": _percentile(roi_boot, 0.975),
        },
        "clv_ci95": {
            "low": _percentile(clv_boot, 0.025),
            "high": _percentile(clv_boot, 0.975),
        },
        "realized_roi_ci95": {
            "low": _percentile(realized_roi_boot, 0.025),
            "high": _percentile(realized_roi_boot, 0.975),
        },
    }


def _proper_score_space_rows(
    graded: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    """Partition graded rows into outcome spaces that can share proper scores.

    A bookmaker quote remains useful for price-space EV and CLV on a split-line
    market, but its two quoted outcomes are not the same random variable as the
    model's five settlement states.  Keeping the spaces explicit prevents a
    missing bookmaker proper score from becoming a permanent statistical blocker
    for Asian-handicap, total, or corner cohorts.
    """

    return {
        CATEGORICAL_PROPER_SCORE_SPACE: [
            row
            for row in graded
            if row.get("market_semantics") == "categorical"
            and row.get("bookmaker_proper_score_status")
            == "available_same_outcome_space"
        ],
        FIVE_STATE_PROPER_SCORE_SPACE: [
            row for row in graded if row.get("market_semantics") == "five_state_return"
        ],
    }


def _proper_score_gate_report(
    graded: Sequence[Mapping[str, Any]], *, repetitions: int, seed: int
) -> dict[str, Any]:
    rows_by_space = _proper_score_space_rows(graded)
    report: dict[str, Any] = {}
    for space_index, (space, required_baselines) in enumerate(
        PROPER_SCORE_BASELINES_BY_SPACE.items()
    ):
        eligible = rows_by_space[space]
        comparisons = {
            baseline: _cluster_bootstrap(
                _paired_deltas(eligible, baseline),
                repetitions=repetitions,
                seed=seed + space_index * 10007 + baseline_index * 101,
            )
            for baseline_index, baseline in enumerate(required_baselines)
        }
        missing = {
            baseline: [
                str(row.get("observation_id"))
                for row in eligible
                if row.get("baselines", {}).get(baseline) is None
            ]
            for baseline in required_baselines
        }
        if not eligible:
            status = "unavailable_no_eligible_outcome_space"
        elif any(missing.values()):
            status = "incomplete_required_baselines"
        else:
            status = "available"
        report[space] = {
            "status": status,
            "eligible_graded_outputs": len(eligible),
            "eligible_observation_ids": [
                str(row.get("observation_id")) for row in eligible
            ],
            "required_baselines": list(required_baselines),
            "missing_baseline_ids": missing,
            "comparisons": comparisons,
            "gate_scope": (
                "bookmaker proper score on the identical categorical outcome space"
                if space == CATEGORICAL_PROPER_SCORE_SPACE
                else "model-space proper scores against frozen five-state baselines"
            ),
            "promotion_gate": False,
            "role": "pooled_descriptive_summary_only",
        }
    return report


def _proper_score_outcome_space_signature(row: Mapping[str, Any]) -> dict[str, Any]:
    semantics = str(row.get("market_semantics") or "")
    states = sorted(str(item) for item in row.get("settlement_states") or [])
    payload = {"settlement_semantics": semantics, "settlement_states": states}
    return {**payload, "signature_hash": _hash(payload)}


def _proper_score_identity_gate_report(
    graded: Sequence[Mapping[str, Any]], *, repetitions: int, seed: int
) -> dict[str, Any]:
    """Build promotion gates per exact canonical market identity.

    Identity-level grouping is intentionally stricter than an outcome-space pool:
    a strong 1X2 cohort cannot subsidize HT/FT or goal-range evidence, and one
    split line/family cannot subsidize another.
    """

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in graded:
        identity_hash = str(row.get("market_identity_hash") or "")
        identity = row.get("market_identity")
        if identity_hash and isinstance(identity, Mapping):
            grouped[identity_hash].append(row)
    report: dict[str, Any] = {}
    for identity_index, (identity_hash, eligible) in enumerate(sorted(grouped.items())):
        first = eligible[0]
        space = (
            CATEGORICAL_PROPER_SCORE_SPACE
            if first.get("market_semantics") == "categorical"
            else FIVE_STATE_PROPER_SCORE_SPACE
        )
        required_baselines = PROPER_SCORE_BASELINES_BY_SPACE[space]
        comparisons = {
            baseline: _cluster_bootstrap(
                _paired_deltas(eligible, baseline),
                repetitions=repetitions,
                seed=seed + identity_index * 10007 + baseline_index * 101,
            )
            for baseline_index, baseline in enumerate(required_baselines)
        }
        missing = {
            baseline: [
                str(row.get("observation_id"))
                for row in eligible
                if row.get("baselines", {}).get(baseline) is None
            ]
            for baseline in required_baselines
        }
        status = (
            "incomplete_required_baselines" if any(missing.values()) else "available"
        )
        report[identity_hash] = {
            "status": status,
            "promotion_gate": True,
            "market_identity_hash": identity_hash,
            "market_identity": dict(first["market_identity"]),
            "market_family_period": str(first.get("market_family_period") or "unknown"),
            "outcome_space_kind": space,
            "outcome_space_signature": _proper_score_outcome_space_signature(first),
            "eligible_graded_outputs": len(eligible),
            "eligible_observation_ids": [
                str(row.get("observation_id")) for row in eligible
            ],
            "required_baselines": list(required_baselines),
            "missing_baseline_ids": missing,
            "comparisons": comparisons,
            "calibration": _calibration(eligible),
            "execution": _execution(
                [
                    row
                    for row in eligible
                    if row.get("status") == "predicted"
                    and isinstance(row.get("execution"), Mapping)
                ],
                repetitions=repetitions,
                seed=seed + identity_index * 10007 + 7901,
            ),
        }
    return report


def _assess_identity_proper_score_gates(
    identity_gates: Mapping[str, Any],
    *,
    minimum_samples: int,
    minimum_clusters: int,
    maximum_calibration_error: float,
) -> tuple[list[str], list[str]]:
    statistical: list[str] = []
    integrity: list[str] = []
    for identity_hash, raw_gate in identity_gates.items():
        gate = raw_gate
        eligible_count = int(gate["eligible_graded_outputs"])
        gate_blockers: list[str] = []
        if gate["status"] != "available":
            gate["statistical_gate_available"] = False
            gate["statistical_gate_passed"] = None
            gate["statistical_blockers"] = []
            continue
        gate["statistical_gate_available"] = True
        short_identity = identity_hash.split(":", 1)[-1][:16]
        space = str(gate["outcome_space_kind"])
        for baseline, comparison in gate["comparisons"].items():
            if comparison.get("sample_count") != eligible_count:
                integrity.append(
                    f"identity_{short_identity}_{baseline}_pairing_is_incomplete"
                )
            if comparison["sample_count"] < minimum_samples:
                gate_blockers.append(
                    f"identity_{short_identity}_{baseline}_samples_are_insufficient"
                )
                if space == CATEGORICAL_PROPER_SCORE_SPACE:
                    statistical.append(
                        "insufficient_paired_same_time_bookmaker_samples"
                    )
            if comparison.get("cluster_count", 0) < minimum_clusters:
                gate_blockers.append(
                    f"identity_{short_identity}_{baseline}_iso_week_clusters_are_insufficient"
                )
                if space == CATEGORICAL_PROPER_SCORE_SPACE:
                    statistical.append("insufficient_independent_iso_week_clusters")
            for metric in ("log_loss_delta", "brier_delta"):
                block = comparison.get(metric)
                if not isinstance(block, Mapping) or block.get("ci95_high") is None:
                    gate_blockers.append(
                        f"identity_{short_identity}_{baseline}_{metric}_ci_is_unavailable"
                    )
                elif float(block["ci95_high"]) >= 0.0:
                    gate_blockers.append(
                        f"identity_{short_identity}_{baseline}_{metric}_ci_does_not_improve"
                    )
        calibration = gate["calibration"]
        calibration_error = calibration.get("expected_calibration_error")
        if (
            calibration_error is None
            or float(calibration_error) > maximum_calibration_error
        ):
            gate_blockers.append(
                f"identity_{short_identity}_calibration_error_exceeds_threshold"
            )
        outcome_calibration = calibration.get("by_outcome")
        if (
            not isinstance(outcome_calibration, Mapping)
            or not outcome_calibration
            or any(
                not isinstance(block, Mapping)
                or block.get("expected_calibration_error") is None
                or float(block["expected_calibration_error"])
                > maximum_calibration_error
                for block in outcome_calibration.values()
            )
        ):
            gate_blockers.append(
                f"identity_{short_identity}_classwise_calibration_exceeds_threshold"
            )
        execution = gate["execution"]
        if execution.get("sample_count", 0) < minimum_samples:
            gate_blockers.append(
                f"identity_{short_identity}_executable_price_samples_are_insufficient"
            )
        for field, blocker_suffix in (
            ("roi_ci95", "roi_ci_is_not_positive"),
            ("realized_roi_ci95", "realized_roi_ci_is_not_positive"),
            ("clv_ci95", "clv_ci_is_not_positive"),
        ):
            interval = execution.get(field)
            if (
                not isinstance(interval, Mapping)
                or interval.get("low") is None
                or float(interval["low"]) <= 0.0
            ):
                gate_blockers.append(f"identity_{short_identity}_{blocker_suffix}")
        gate["statistical_blockers"] = gate_blockers
        gate["statistical_gate_passed"] = not gate_blockers
        statistical.extend(gate_blockers)
    return list(dict.fromkeys(statistical)), list(dict.fromkeys(integrity))


def _segment_report(
    rows: Sequence[Mapping[str, Any]], *, repetitions: int, seed: int
) -> dict[str, Any]:
    predicted = [row for row in rows if row.get("status") == "predicted"]
    formal_rows = [
        row for row in rows if row.get("formal_evaluation_eligible", True) is True
    ]
    modeled = [
        row
        for row in rows
        if row.get("status") in {"predicted", "abstained"}
        and row.get("model_probabilities") is not None
    ]
    graded = [
        row
        for row in formal_rows
        if row.get("status") in {"predicted", "abstained"}
        and row.get("model_probabilities") is not None
        and _observed_state(row) is not None
    ]
    executed_graded = [
        row
        for row in formal_rows
        if row.get("status") == "predicted"
        if _observed_state(row) is not None
        and isinstance(row.get("execution"), Mapping)
    ]
    missing_settlement_state_ids = [
        str(row.get("observation_id")) for row in rows if _observed_state(row) is None
    ]
    quarantined_observation_ids = [
        str(row.get("observation_id"))
        for row in rows
        if row.get("formal_evaluation_blockers")
    ]
    replay_unavailable_blockers = list(
        dict.fromkeys(
            str(blocker)
            for row in rows
            for blocker in row.get("formal_evaluation_blockers", [])
            if str(blocker).strip()
        )
    )
    baseline_eligible_rows = {
        baseline: [
            row
            for row in graded
            if baseline != "bookmaker_no_vig"
            or row.get("bookmaker_proper_score_status")
            == "available_same_outcome_space"
        ]
        for baseline in BASELINE_NAMES
    }
    missing_baseline_ids = {
        baseline: [
            str(row.get("observation_id"))
            for row in baseline_eligible_rows[baseline]
            if row.get("baselines", {}).get(baseline) is None
        ]
        for baseline in BASELINE_NAMES
    }
    missing_execution_ids = [
        str(row.get("observation_id"))
        for row in predicted
        if _observed_state(row) is not None
        and not isinstance(row.get("execution"), Mapping)
    ]
    comparisons = {
        baseline: _cluster_bootstrap(
            _paired_deltas(graded, baseline),
            repetitions=repetitions,
            seed=seed + index * 101,
        )
        for index, baseline in enumerate(BASELINE_NAMES)
    }
    proper_score_gates = _proper_score_gate_report(
        graded, repetitions=repetitions, seed=seed + 1703
    )
    proper_score_identity_gates = _proper_score_identity_gate_report(
        graded, repetitions=repetitions, seed=seed + 3203
    )
    return {
        "eligible_fixtures": len(rows),
        "formal_evaluation_eligible_fixtures": len(formal_rows),
        "quarantined_observation_ids": quarantined_observation_ids,
        "replay_unavailable_blockers": replay_unavailable_blockers,
        "predicted_fixtures": len(predicted),
        "model_available_fixtures": len(modeled),
        "graded_model_outputs": len(graded),
        "graded_predictions": sum(
            _observed_state(row) is not None for row in predicted
        ),
        "settled_fixtures": len(rows) - len(missing_settlement_state_ids),
        "missing_settlement_state_ids": missing_settlement_state_ids,
        "missing_baseline_ids": missing_baseline_ids,
        "proper_score_availability": {
            baseline: {
                "eligible_graded_outputs": len(baseline_eligible_rows[baseline]),
                "unavailable_graded_outputs": len(graded)
                - len(baseline_eligible_rows[baseline]),
                "unavailable_observation_ids": [
                    str(row.get("observation_id"))
                    for row in graded
                    if row not in baseline_eligible_rows[baseline]
                ],
                "status": (
                    "available"
                    if baseline_eligible_rows[baseline]
                    else "unavailable_no_compatible_outcome_space"
                ),
            }
            for baseline in BASELINE_NAMES
        },
        "baseline_paired_coverage": {
            baseline: (
                (
                    len(baseline_eligible_rows[baseline])
                    - len(missing_baseline_ids[baseline])
                )
                / len(baseline_eligible_rows[baseline])
                if baseline_eligible_rows[baseline]
                else None
            )
            for baseline in BASELINE_NAMES
        },
        "missing_execution_ids": missing_execution_ids,
        "coverage": len(predicted) / len(rows) if rows else None,
        "model_availability_rate": len(modeled) / len(rows) if rows else None,
        "abstention_rate": (
            sum(row.get("status") == "abstained" for row in rows) / len(rows)
            if rows
            else None
        ),
        "unavailable_rate": (
            sum(row.get("status") == "unavailable" for row in rows) / len(rows)
            if rows
            else None
        ),
        "comparisons": comparisons,
        "proper_score_gates": proper_score_gates,
        "proper_score_identity_gates": proper_score_identity_gates,
        "calibration": _calibration(graded),
        "execution": _execution(
            executed_graded, repetitions=repetitions, seed=seed + 701
        ),
    }


def evaluate(
    payload: Any,
    *,
    bootstrap_repetitions: int | None = None,
    bootstrap_seed: int | None = None,
    minimum_confirmation_samples: int | None = None,
    minimum_iso_week_clusters: int | None = None,
) -> dict[str, Any]:
    data = validate_input(payload)
    legacy = bool(data.get("legacy_uncommitted"))
    frozen_protocol = data.get("validation_protocol")
    if isinstance(frozen_protocol, Mapping):
        frozen_repetitions = int(frozen_protocol["bootstrap_repetitions"])
        frozen_seed = int(frozen_protocol["bootstrap_seed"])
        frozen_samples = int(frozen_protocol["minimum_confirmation_samples"])
        frozen_clusters = int(frozen_protocol["minimum_iso_week_clusters"])
        minimum_segment_samples = int(frozen_protocol["minimum_segment_samples"])
        minimum_segment_clusters = int(frozen_protocol["minimum_segment_clusters"])
        maximum_calibration_error = float(frozen_protocol["maximum_calibration_error"])
    else:
        frozen_repetitions = 2000
        frozen_seed = 20260806
        frozen_samples = 200
        frozen_clusters = 20
        minimum_segment_samples = 40
        minimum_segment_clusters = 5
        maximum_calibration_error = 0.05
    requested = {
        "bootstrap_repetitions": bootstrap_repetitions,
        "bootstrap_seed": bootstrap_seed,
        "minimum_confirmation_samples": minimum_confirmation_samples,
        "minimum_iso_week_clusters": minimum_iso_week_clusters,
    }
    frozen = {
        "bootstrap_repetitions": frozen_repetitions,
        "bootstrap_seed": frozen_seed,
        "minimum_confirmation_samples": frozen_samples,
        "minimum_iso_week_clusters": frozen_clusters,
    }
    protocol_overrides = {
        field: {"frozen": frozen[field], "requested": supplied}
        for field, supplied in requested.items()
        if supplied is not None and supplied != frozen[field]
    }
    bootstrap_repetitions = int(
        bootstrap_repetitions
        if bootstrap_repetitions is not None
        else frozen_repetitions
    )
    bootstrap_seed = int(bootstrap_seed if bootstrap_seed is not None else frozen_seed)
    minimum_confirmation_samples = int(
        minimum_confirmation_samples
        if minimum_confirmation_samples is not None
        else frozen_samples
    )
    minimum_iso_week_clusters = int(
        minimum_iso_week_clusters
        if minimum_iso_week_clusters is not None
        else frozen_clusters
    )
    if bootstrap_repetitions < 100:
        raise ForwardValidationError("bootstrap_repetitions must be at least 100")
    if minimum_confirmation_samples < 1:
        raise ForwardValidationError("minimum_confirmation_samples must be positive")
    if minimum_iso_week_clusters < 2:
        raise ForwardValidationError("minimum_iso_week_clusters must be at least two")
    rows = data["records"]
    overall = _segment_report(
        rows, repetitions=bootstrap_repetitions, seed=bootstrap_seed
    )
    segments: dict[str, Any] = {}
    for field in ("league", "market_family_period", "lead_time_bucket"):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row.get(field) or "unknown")].append(row)
        segments[field] = {
            key: _segment_report(
                subset,
                repetitions=bootstrap_repetitions,
                seed=bootstrap_seed + index * 1009,
            )
            for index, (key, subset) in enumerate(sorted(grouped.items()))
        }
    calibration_error = overall["calibration"]["expected_calibration_error"]
    execution = overall["execution"]
    segment_stability: dict[str, Any] = {}
    for dimension, blocks in segments.items():
        identity_blocks: dict[str, Any] = {}
        active_identities = sorted(overall["proper_score_identity_gates"])
        for identity_hash in active_identities:
            overall_gate = overall["proper_score_identity_gates"][identity_hash]
            eligible_blocks: dict[str, Any] = {}
            failures: list[str] = []
            for key, block in blocks.items():
                gate = block["proper_score_identity_gates"].get(identity_hash)
                if not isinstance(gate, Mapping):
                    continue
                if gate["status"] != "available":
                    continue
                comparisons = gate["comparisons"]
                if any(
                    comparison["sample_count"] < minimum_segment_samples
                    or comparison.get("cluster_count", 0) < minimum_segment_clusters
                    for comparison in comparisons.values()
                ):
                    continue
                eligible_blocks[key] = comparisons
                for baseline, comparison in comparisons.items():
                    for metric in ("log_loss_delta", "brier_delta"):
                        interval = comparison.get(metric)
                        if (
                            not isinstance(interval, Mapping)
                            or interval.get("ci95_high") is None
                            or float(interval["ci95_high"]) >= 0.0
                        ):
                            failures.append(f"{key}:{baseline}:{metric}")
                calibration = gate["calibration"]
                if (
                    calibration.get("expected_calibration_error") is None
                    or float(calibration["expected_calibration_error"])
                    > maximum_calibration_error
                ):
                    failures.append(f"{key}:calibration")
                outcome_calibration = calibration.get("by_outcome")
                if (
                    not isinstance(outcome_calibration, Mapping)
                    or not outcome_calibration
                    or any(
                        not isinstance(outcome_block, Mapping)
                        or outcome_block.get("expected_calibration_error") is None
                        or float(outcome_block["expected_calibration_error"])
                        > maximum_calibration_error
                        for outcome_block in outcome_calibration.values()
                    )
                ):
                    failures.append(f"{key}:classwise_calibration")
                execution_block = gate["execution"]
                if execution_block.get("sample_count", 0) < minimum_segment_samples:
                    failures.append(f"{key}:execution_samples")
                for interval_name in (
                    "roi_ci95",
                    "realized_roi_ci95",
                    "clv_ci95",
                ):
                    interval = execution_block.get(interval_name)
                    if (
                        not isinstance(interval, Mapping)
                        or interval.get("low") is None
                        or float(interval["low"]) <= 0.0
                    ):
                        failures.append(f"{key}:{interval_name}")
            identity_blocks[identity_hash] = {
                "status": overall_gate["status"],
                "market_identity": deepcopy(overall_gate["market_identity"]),
                "market_family_period": overall_gate["market_family_period"],
                "outcome_space_signature": deepcopy(
                    overall_gate["outcome_space_signature"]
                ),
                "required_baselines": list(overall_gate["required_baselines"]),
                "eligible_segments": sorted(eligible_blocks),
                "failed_segments": failures,
                "demonstrated": bool(eligible_blocks) and not failures,
            }
        segment_stability[dimension] = {
            "minimum_paired_samples": minimum_segment_samples,
            "minimum_iso_week_clusters": minimum_segment_clusters,
            "active_market_identities": active_identities,
            "market_identities": identity_blocks,
            "eligible_segments": sorted(
                {
                    key
                    for block in identity_blocks.values()
                    for key in block["eligible_segments"]
                }
            ),
            "failed_segments": [
                f"{identity_hash}:{failure}"
                for identity_hash, block in identity_blocks.items()
                for failure in block["failed_segments"]
            ],
            "demonstrated": bool(active_identities)
            and all(block["demonstrated"] for block in identity_blocks.values()),
        }
    statistical_blockers: list[str] = []
    integrity_blockers: list[str] = []
    assurance_blockers: list[str] = []
    manual_blockers = ["manual_independent_review_required_even_when_gates_pass"]
    if legacy:
        integrity_blockers.append("legacy_uncommitted_observations_are_read_only")
    if protocol_overrides:
        integrity_blockers.append("validation_protocol_override_is_experimental")
    if data.get("external_timestamp_anchor") is not True:
        assurance_blockers.append("external_timestamp_anchor_not_configured")
    if data.get("baseline_artifact_replay_complete") is not True:
        assurance_blockers.append("baseline_artifact_replay_not_demonstrated")
    if data.get("execution_price_source_replay_complete") is not True:
        assurance_blockers.append("execution_price_source_replay_not_demonstrated")
    if data.get("result_source_replay_complete") is not True:
        assurance_blockers.append("result_source_replay_not_demonstrated")
    if data.get("closing_price_source_replay_complete") is not True:
        assurance_blockers.append("closing_price_source_replay_not_demonstrated")
    if data.get("cohort_closed") is not True:
        integrity_blockers.append("forward_cohort_is_not_closed")
    integrity_blockers.extend(overall.get("replay_unavailable_blockers", []))
    if overall["missing_settlement_state_ids"]:
        integrity_blockers.append("incomplete_settlement_states")
    if any(overall["missing_baseline_ids"].values()):
        integrity_blockers.append("incomplete_paired_baselines")
    if overall["missing_execution_ids"]:
        integrity_blockers.append("incomplete_executable_price_evidence")
    identity_statistical, identity_integrity = _assess_identity_proper_score_gates(
        overall["proper_score_identity_gates"],
        minimum_samples=minimum_confirmation_samples,
        minimum_clusters=minimum_iso_week_clusters,
        maximum_calibration_error=maximum_calibration_error,
    )
    statistical_blockers.extend(identity_statistical)
    integrity_blockers.extend(identity_integrity)
    if (
        calibration_error is None
        or float(calibration_error) > maximum_calibration_error
    ):
        statistical_blockers.append(
            "calibration_error_exceeds_5_percent_or_is_unavailable"
        )
    outcome_calibration = overall["calibration"].get("by_outcome", {})
    if (
        not isinstance(outcome_calibration, Mapping)
        or not outcome_calibration
        or any(
            not isinstance(block, Mapping)
            or block.get("expected_calibration_error") is None
            or float(block["expected_calibration_error"]) > maximum_calibration_error
            for block in outcome_calibration.values()
        )
    ):
        statistical_blockers.append(
            "classwise_calibration_error_exceeds_5_percent_or_is_unavailable"
        )
    if overall["coverage"] is None:
        statistical_blockers.append("coverage_unavailable")
    if execution["sample_count"] < minimum_confirmation_samples:
        statistical_blockers.append("insufficient_executable_price_samples")
    roi_ci = execution.get("roi_ci95")
    if (
        not isinstance(roi_ci, Mapping)
        or roi_ci.get("low") is None
        or float(roi_ci["low"]) <= 0
    ):
        statistical_blockers.append("executable_price_roi_ci95_not_positive")
    realized_roi_ci = execution.get("realized_roi_ci95")
    if (
        not isinstance(realized_roi_ci, Mapping)
        or realized_roi_ci.get("low") is None
        or float(realized_roi_ci["low"]) <= 0
    ):
        statistical_blockers.append("realized_stake_weighted_roi_ci95_not_positive")
    clv_ci = execution.get("clv_ci95")
    if (
        not isinstance(clv_ci, Mapping)
        or clv_ci.get("low") is None
        or float(clv_ci["low"]) <= 0
    ):
        statistical_blockers.append("clv_ci95_not_positive")
    for dimension, block in segment_stability.items():
        if not block["demonstrated"]:
            statistical_blockers.append(f"{dimension}_stability_not_demonstrated")
    cohort_kind = str(data.get("cohort_manifest", {}).get("kind") or "")
    local_shadow_kind = getattr(
        forward_policy, "LOCAL_INTEGRITY_SHADOW_KIND", "local-integrity-shadow-v2"
    )
    promotable_kind = getattr(
        forward_policy, "PROMOTABLE_CONFIRMATION_KIND", "promotable-confirmation-v2"
    )
    if cohort_kind == local_shadow_kind:
        assurance_blockers.append("local_integrity_shadow_has_no_external_assurance")
    elif not cohort_kind and not legacy:
        assurance_blockers.append("cohort_kind_is_unspecified")
    blocker_categories = {
        "statistical": statistical_blockers,
        "integrity": integrity_blockers,
        "assurance": assurance_blockers,
        "manual": manual_blockers,
    }
    flattened_blockers = [
        blocker
        for category in ("statistical", "integrity", "assurance")
        for blocker in blocker_categories[category]
    ]
    identity_audit: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity_hash = str(row.get("market_identity_hash") or "")
        identity = row.get("market_identity")
        if not identity_hash or not isinstance(identity, Mapping):
            continue
        gate_space = (
            CATEGORICAL_PROPER_SCORE_SPACE
            if row.get("market_semantics") == "categorical"
            else FIVE_STATE_PROPER_SCORE_SPACE
        )
        identity_gate = overall["proper_score_identity_gates"].get(identity_hash)
        item = identity_audit.setdefault(
            identity_hash,
            {
                "market_identity_hash": identity_hash,
                "market_identity": dict(identity),
                "market_semantics": row.get("market_semantics"),
                "proper_score_gate_space": gate_space,
                "required_proper_score_baselines": list(
                    PROPER_SCORE_BASELINES_BY_SPACE[gate_space]
                ),
                "outcome_space_signature": _proper_score_outcome_space_signature(row),
                "identity_gate_status": (
                    identity_gate["status"]
                    if isinstance(identity_gate, Mapping)
                    else "unavailable_no_graded_model_output"
                ),
                "identity_statistical_gate_available": (
                    identity_gate.get("statistical_gate_available")
                    if isinstance(identity_gate, Mapping)
                    else False
                ),
                "identity_statistical_gate_passed": (
                    identity_gate.get("statistical_gate_passed")
                    if isinstance(identity_gate, Mapping)
                    else None
                ),
                "identity_statistical_blockers": (
                    list(identity_gate.get("statistical_blockers") or [])
                    if isinstance(identity_gate, Mapping)
                    else []
                ),
                "bookmaker_proper_score_status": row.get(
                    "bookmaker_proper_score_status"
                ),
                "settlement_states": list(row.get("settlement_states") or []),
                "observation_count": 0,
                "settlement_reference_outcomes": set(),
            },
        )
        item["observation_count"] += 1
        reference = row.get("settlement_reference_outcome")
        if reference is not None:
            item["settlement_reference_outcomes"].add(str(reference))
    market_identity_audit = []
    for identity_hash in sorted(identity_audit):
        item = identity_audit[identity_hash]
        item["settlement_reference_outcomes"] = sorted(
            item["settlement_reference_outcomes"]
        )
        market_identity_audit.append(item)
    statistical_gate_passed = not statistical_blockers
    local_integrity_gate_passed = not integrity_blockers
    assurance_gate_passed = not assurance_blockers
    promotion_eligible = bool(
        cohort_kind == promotable_kind
        and statistical_gate_passed
        and local_integrity_gate_passed
        and assurance_gate_passed
    )
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "artifact_type": "soccer_untouched_forward_validation",
        "cohort_id": data["cohort_id"],
        "cohort_hash": data["cohort_manifest"]["cohort_hash"],
        "cohort_kind": cohort_kind or None,
        "policy_id": data["policy_id"],
        "policy_hash": data["policy_hash"],
        "input_hash": _hash(data),
        "outcomes": data.get("outcomes"),
        "market_schemas": data.get("market_schemas"),
        "market_identity_audit": market_identity_audit,
        "proper_score_gate_contract": {
            "categorical_same_outcome_space": {
                "required_baselines": ["bookmaker_no_vig"],
                "bookmaker_proper_score": "available_only_when_price_and_model_outcomes_match",
            },
            "five_state_return": {
                "required_baselines": list(MODEL_SPACE_BASELINE_NAMES),
                "bookmaker_proper_score": "unavailable_not_a_statistical_failure",
                "bookmaker_price_use": "execution_ev_and_clv_only",
            },
        },
        "evidence_contract": data.get("evidence_contract"),
        "result_source_replay_complete": data.get("result_source_replay_complete"),
        "closing_price_source_replay_complete": data.get(
            "closing_price_source_replay_complete"
        ),
        "provenance_binding": data.get("provenance_binding"),
        "history_ledger_binding": data.get("history_ledger_binding"),
        "queue_hash": (
            data.get("queue_manifest", {}).get("queue_hash")
            if isinstance(data.get("queue_manifest"), Mapping)
            else None
        ),
        "cohort_closure_hash": (
            data.get("cohort_closure", {}).get("closure_hash")
            if isinstance(data.get("cohort_closure"), Mapping)
            else None
        ),
        "uncertainty": {
            "method": "paired_cluster_bootstrap",
            "cluster_unit": "kickoff_iso_week",
            "repetitions": bootstrap_repetitions,
            "seed": bootstrap_seed,
        },
        "overall": overall,
        "segments": segments,
        "segment_stability": segment_stability,
        "minimum_confirmation_samples": minimum_confirmation_samples,
        "minimum_iso_week_clusters": minimum_iso_week_clusters,
        "frozen_validation_protocol": dict(frozen_protocol)
        if isinstance(frozen_protocol, Mapping)
        else None,
        "protocol_overrides": protocol_overrides,
        "statistical_gate_passed": statistical_gate_passed,
        "local_integrity_gate_passed": local_integrity_gate_passed,
        "assurance_gate_passed": assurance_gate_passed,
        "blocker_categories": blocker_categories,
        "promotion_eligible": promotion_eligible,
        "parameter_change_authorized": False,
        "manual_review_required": True,
        "promotion_blockers": flattened_blockers or manual_blockers,
    }
    report["report_hash"] = _hash(report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--bootstrap-repetitions",
        type=int,
        help="experimental override; frozen policy value is used by default",
    )
    parser.add_argument(
        "--bootstrap-seed",
        type=int,
        help="experimental override; frozen policy value is used by default",
    )
    parser.add_argument(
        "--minimum-confirmation-samples",
        type=int,
        help="experimental override; frozen policy value is used by default",
    )
    parser.add_argument(
        "--minimum-iso-week-clusters",
        type=int,
        help="experimental override; frozen policy value is used by default",
    )
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        payload = json.loads(Path(arguments.input).read_text(encoding="utf-8"))
        report = evaluate(
            payload,
            bootstrap_repetitions=arguments.bootstrap_repetitions,
            bootstrap_seed=arguments.bootstrap_seed,
            minimum_confirmation_samples=arguments.minimum_confirmation_samples,
            minimum_iso_week_clusters=arguments.minimum_iso_week_clusters,
        )
        output = Path(arguments.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(output)
        print(
            json.dumps(
                {"ok": True, "path": str(output), "report_hash": report["report_hash"]}
            )
        )
        return 0
    except (ForwardValidationError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

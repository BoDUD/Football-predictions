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
    from scripts import forward_policy, source_evidence
except ImportError:  # Direct execution from scripts/.
    script_directory = str(Path(__file__).resolve().parent)
    if script_directory not in sys.path:
        sys.path.insert(0, script_directory)
    import forward_policy  # type: ignore[no-redef]
    import source_evidence  # type: ignore[no-redef]

LEGACY_INPUT_SCHEMA_VERSION = "forward-observations/1.0.0"
INPUT_SCHEMA_VERSION = "forward-observations/2.0.0"
QUEUE_SCHEMA_VERSION = "forward-eligibility-queue/1.0.0"
COMMITMENT_SCHEMA_VERSION = "forward-observation-commitment/1.0.0"
SETTLEMENT_SCHEMA_VERSION = "forward-observation-settlement/1.0.0"
HISTORY_LEDGER_BINDING_SCHEMA_VERSION = "memory-forward-history-ledger-binding/1.0.0"
REPORT_SCHEMA_VERSION = "forward-validation/2.0.0"
BASELINE_NAMES = (
    "historical_frequency",
    "independent_htft",
    "simple_poisson_dc",
    "bookmaker_no_vig",
)
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


def _queue_key(cohort_id: str, fixture_id: str, market: str) -> str:
    return _hash(
        {
            "cohort_id": cohort_id,
            "fixture_id": fixture_id,
            "market": market,
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


def _validation_protocol(policy: Mapping[str, Any]) -> dict[str, Any]:
    runtime = policy.get("policy")
    protocol = (
        runtime.get("validation_protocol") if isinstance(runtime, Mapping) else None
    )
    if not isinstance(protocol, Mapping):
        raise ForwardValidationError(
            "v2 forward observations require a validation protocol frozen in policy"
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
        "required_baselines",
        "queue_contract",
        "external_timestamp_anchor_required_for_promotion",
    }
    _exact_keys(protocol, required, "policy.validation_protocol")
    if protocol.get("schema_version") != "forward-validation-protocol/1.0.0":
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
    required_baselines = protocol.get("required_baselines")
    if (
        not isinstance(required_baselines, list)
        or tuple(required_baselines) != BASELINE_NAMES
    ):
        raise ForwardValidationError(
            "frozen validation protocol does not require the canonical baselines"
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
            raw_schema, {"outcomes", "settlement_semantics"}, f"market_schemas.{market}"
        )
        outcomes = raw_schema.get("outcomes")
        if (
            not isinstance(outcomes, list)
            or len(outcomes) < 2
            or any(not isinstance(item, str) or not item for item in outcomes)
            or len(set(outcomes)) != len(outcomes)
        ):
            raise ForwardValidationError(
                f"market_schemas.{market}.outcomes must be unique strings"
            )
        semantics = str(raw_schema.get("settlement_semantics") or "")
        if semantics not in {"categorical", "five_state_return"}:
            raise ForwardValidationError(
                f"market_schemas.{market}.settlement_semantics is unsupported"
            )
        if semantics == "five_state_return" and set(outcomes) != {
            "win",
            "half_win",
            "push",
            "half_loss",
            "loss",
        }:
            raise ForwardValidationError(
                f"market_schemas.{market} five-state outcomes are incomplete"
            )
        normalized[market] = {
            "outcomes": list(outcomes),
            "settlement_semantics": semantics,
        }
    return normalized


def _prematch_ledger_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(payload))
    value.pop("history_ledger_binding", None)
    value["cohort_closure"] = None
    for settlement in value.get("settlements", []):
        if not isinstance(settlement, dict):
            continue
        settlement.update(
            {
                "status": "pending",
                "observed_outcome": None,
                "result_collected_at": None,
                "result_source_evidence_hash": None,
            }
        )
        settlement.pop("settlement_hash", None)
        settlement["settlement_hash"] = _hash(settlement)
    return value


def _market_commitment_identities(payload: Mapping[str, Any]) -> list[dict[str, str]]:
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
        identities.append(
            {
                "market": str(prediction.get("market") or "").lower(),
                "observation_id": str(prediction.get("observation_id") or ""),
                "prediction_hash": _hash(prediction),
                "commitment_hash": str(raw.get("commitment_hash") or ""),
                "binding_hash": str(
                    binding.get("binding_hash") if isinstance(binding, Mapping) else ""
                ),
            }
        )
    identities.sort(key=lambda item: (item["market"], item["observation_id"]))
    return identities


def build_history_ledger_binding(
    payload: Mapping[str, Any],
    *,
    archive_version_hash: str,
    record_commitment_hash: str,
) -> dict[str, Any]:
    """Build the content-addressed hand-off used only after memory-store archival."""

    fixture_ids = sorted(
        {
            str(item.get("prediction_payload", {}).get("fixture_id") or "")
            for item in payload.get("commitments", [])
            if isinstance(item, Mapping)
            and isinstance(item.get("prediction_payload"), Mapping)
        }
    )
    if not fixture_ids or "" in fixture_ids:
        raise ForwardValidationError("history ledger fixture IDs are missing")
    binding: dict[str, Any] = {
        "schema_version": HISTORY_LEDGER_BINDING_SCHEMA_VERSION,
        "fixture_ids": fixture_ids,
        "archive_version_hash": _sha256(
            archive_version_hash, "history ledger archive_version_hash"
        ),
        "record_commitment_hash": _sha256(
            record_commitment_hash, "history ledger record_commitment_hash"
        ),
        "prematch_ledger_hash": _hash(_prematch_ledger_view(payload)),
        "market_commitments": _market_commitment_identities(payload),
    }
    binding["binding_hash"] = _hash(binding)
    return binding


def _validate_history_ledger_binding(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("history_ledger_binding")
    if not isinstance(raw, Mapping):
        raise ForwardValidationError(
            "v2 evaluation requires a memory-store history ledger binding"
        )
    value = dict(raw)
    required = {
        "schema_version",
        "fixture_ids",
        "archive_version_hash",
        "record_commitment_hash",
        "prematch_ledger_hash",
        "market_commitments",
        "binding_hash",
    }
    _exact_keys(value, required, "history_ledger_binding")
    if value.get("schema_version") != HISTORY_LEDGER_BINDING_SCHEMA_VERSION:
        raise ForwardValidationError("history_ledger_binding schema is unsupported")
    supplied_hash = value.pop("binding_hash", None)
    if supplied_hash != _hash(value):
        raise ForwardValidationError("history_ledger_binding hash is invalid")
    for field in (
        "archive_version_hash",
        "record_commitment_hash",
        "prematch_ledger_hash",
    ):
        _sha256(value.get(field), f"history_ledger_binding.{field}")
    expected_fixture_ids = sorted(
        {
            str(item.get("prediction_payload", {}).get("fixture_id") or "")
            for item in payload.get("commitments", [])
            if isinstance(item, Mapping)
            and isinstance(item.get("prediction_payload"), Mapping)
        }
    )
    if value.get("fixture_ids") != expected_fixture_ids or "" in expected_fixture_ids:
        raise ForwardValidationError("history_ledger_binding fixture IDs do not replay")
    identities = _market_commitment_identities(payload)
    if value.get("market_commitments") != identities:
        raise ForwardValidationError(
            "history_ledger_binding market commitments do not replay"
        )
    for identity in identities:
        for field in ("prediction_hash", "commitment_hash", "binding_hash"):
            _sha256(identity.get(field), f"history ledger commitment {field}")
    if value.get("prematch_ledger_hash") != _hash(_prematch_ledger_view(payload)):
        raise ForwardValidationError(
            "history_ledger_binding does not reproduce the archived pre-match ledger"
        )
    value["binding_hash"] = supplied_hash
    return value


def _validate_v2_input(
    payload: Any, *, require_history_ledger_binding: bool = True
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ForwardValidationError("forward observations must be a JSON object")
    value = dict(payload)
    if value.get("schema_version") != INPUT_SCHEMA_VERSION:
        raise ForwardValidationError("unsupported forward-observations schema_version")
    if "records" in value or "outcomes" in value:
        raise ForwardValidationError(
            "v2 separates pre-match commitments from settlements and uses market_schemas"
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
            "v2 requires the original immutable active cohort manifest, "
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
    protocol = _validation_protocol(policy)
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
    seen_fixture_market: set[tuple[str, str]] = set()
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
                "market",
                "kickoff",
                "queue_key",
            },
            label,
        )
        fixture_id = str(raw_entry.get("fixture_id") or "").strip()
        home_team = str(raw_entry.get("home_team") or "").strip()
        away_team = str(raw_entry.get("away_team") or "").strip()
        league = str(raw_entry.get("league") or "").strip()
        market = str(raw_entry.get("market") or "").strip().lower()
        if (
            not fixture_id
            or not home_team
            or not away_team
            or not league
            or not market
            or market not in schemas
        ):
            raise ForwardValidationError(f"{label} fixture/league/market is invalid")
        pair = (fixture_id, market)
        if pair in seen_fixture_market:
            raise ForwardValidationError("queue contains a duplicated fixture+market")
        seen_fixture_market.add(pair)
        expected_key = _queue_key(cohort["cohort_id"], fixture_id, market)
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
            "kickoff": kickoff.isoformat(),
            "queue_key": expected_key,
        }

    commitments = value.get("commitments")
    if not isinstance(commitments, list) or not commitments:
        raise ForwardValidationError("commitments must be a non-empty array")
    by_observation: dict[str, dict[str, Any]] = {}
    committed_queue_keys: set[str] = set()
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
        "market",
        "kickoff",
        "generated_at",
        "lead_time_minutes",
        "status",
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
            "market",
            "kickoff",
        ):
            expected = entry[field]
            actual = prediction.get(field)
            if field == "kickoff":
                actual = _aware(
                    actual, f"{label}.prediction_payload.kickoff"
                ).isoformat()
            elif field == "market":
                actual = str(actual or "").strip().lower()
            else:
                actual = str(actual or "").strip()
            if actual != expected:
                raise ForwardValidationError(
                    f"{label} does not match queue field {field}"
                )
        observation_id = str(prediction.get("observation_id") or "")
        if (
            observation_id != _observation_id(queue_key)
            or observation_id in by_observation
        ):
            raise ForwardValidationError(f"{label}.observation_id is not canonical")
        market = entry["market"]
        market_schema = schemas[market]
        outcomes = market_schema["outcomes"]
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
                model, outcomes, f"{label}.model_probabilities"
            )
            if not isinstance(baselines_raw, Mapping) or set(baselines_raw) != set(
                BASELINE_NAMES
            ):
                raise ForwardValidationError(
                    f"{label}.baselines must contain every frozen required baseline"
                )
            baselines = {
                name: _probabilities(
                    baselines_raw[name], outcomes, f"{label}.baselines.{name}"
                )
                for name in BASELINE_NAMES
            }
            if not isinstance(lineage_raw, Mapping) or set(lineage_raw) != set(
                BASELINE_NAMES
            ):
                raise ForwardValidationError(
                    f"{label}.baseline_lineage must bind every required baseline"
                )
            lineage = {}
            for name in BASELINE_NAMES:
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
                    "v2 bookmaker snapshots require decimal odds"
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
                outcomes,
                f"{label}.bookmaker_snapshot.complete_market_odds",
            )
            if any(
                not math.isclose(
                    baselines["bookmaker_no_vig"][outcome],
                    derived_no_vig[outcome],
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                for outcome in outcomes
            ):
                raise ForwardValidationError(
                    f"{label}.bookmaker_no_vig does not recompute from complete odds"
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
                        "market": market,
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
                "collected_at": bookmaker_collected.isoformat(),
                "source_evidence_hash": source_hash,
                "source_evidence_file": str(Path(evidence_file).resolve()),
                "source_binding": source_binding,
                "odds_format": "decimal",
                "complete_market_odds": complete_odds,
                "derived_no_vig": derived_no_vig,
                "no_vig_method": "multiplicative_normalization",
            }
        execution_entry_raw = prediction.get("execution_entry")
        execution_entry = None
        if execution_entry_raw is not None:
            if status != "predicted" or not isinstance(execution_entry_raw, Mapping):
                raise ForwardValidationError(
                    f"{label}.execution_entry is invalid for status"
                )
            _exact_keys(
                execution_entry_raw,
                {
                    "selection",
                    "entry_decimal_odds",
                    "entry_complete_market_odds",
                    "entry_collected_at",
                    "entry_source_evidence_hash",
                    "entry_price_kind",
                    "limit_verified",
                    "stake_units",
                },
                f"{label}.execution_entry",
            )
            selection = str(execution_entry_raw.get("selection") or "").strip()
            if not selection:
                raise ForwardValidationError(
                    f"{label}.execution_entry.selection is required"
                )
            if (
                market_schema["settlement_semantics"] == "categorical"
                and selection not in outcomes
            ):
                raise ForwardValidationError(
                    f"{label}.execution_entry.selection is invalid"
                )
            entry_collected = _aware(
                execution_entry_raw.get("entry_collected_at"),
                f"{label}.execution_entry.entry_collected_at",
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
            if (
                execution_entry_raw.get("entry_price_kind")
                != "executable_after_slippage"
            ):
                raise ForwardValidationError(f"{label}.entry_price_kind is invalid")
            if execution_entry_raw.get("limit_verified") is not True:
                raise ForwardValidationError(f"{label}.entry limit must be verified")
            raw_entry_market = execution_entry_raw.get("entry_complete_market_odds")
            if market_schema["settlement_semantics"] == "categorical":
                price_outcomes = outcomes
            elif isinstance(raw_entry_market, Mapping):
                price_outcomes = list(raw_entry_market)
            else:
                price_outcomes = []
            if not price_outcomes or selection not in price_outcomes:
                raise ForwardValidationError(
                    f"{label}.execution entry market is incomplete"
                )
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
            execution_entry = {
                "selection": selection,
                "entry_decimal_odds": entry_price,
                "entry_complete_market_odds": entry_complete_odds,
                "entry_no_vig_probability": entry_no_vig[selection],
                "entry_collected_at": entry_collected.isoformat(),
                "entry_source_evidence_hash": _sha256(
                    execution_entry_raw.get("entry_source_evidence_hash"),
                    f"{label}.execution_entry.entry_source_evidence_hash",
                ),
                "entry_price_kind": "executable_after_slippage",
                "limit_verified": True,
                "stake_units": stake,
            }
        by_observation[observation_id] = {
            "commitment_hash": supplied_commitment_hash,
            "entry": entry,
            "market_schema": market_schema,
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
                "market_semantics": market_schema["settlement_semantics"],
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
                "observed_outcome": None,
                "settlement_status": "pending",
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
        "observed_outcome",
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
        actual = raw_settlement.get("observed_outcome")
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
            outcomes = metadata["market_schema"]["outcomes"]
            if actual not in outcomes:
                raise ForwardValidationError(f"{label}.observed_outcome is invalid")
            result_collected = _aware(
                result_collected_raw, f"{label}.result_collected_at"
            )
            if result_collected < metadata["kickoff"]:
                raise ForwardValidationError(
                    f"{label} result was collected before kickoff"
                )
            _sha256(result_hash_raw, f"{label}.result_source_evidence_hash")
            row["observed_outcome"] = actual
            row["settlement_status"] = "settled"
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
            price_outcomes = list(entry["entry_complete_market_odds"])
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
                entry["entry_collected_at"], f"{label}.entry_collected_at"
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
                    settlement_state = "win" if selection == actual else "loss"
                else:
                    settlement_state = str(actual)
            row["execution"] = {
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
    if settled_ids != set(by_observation):
        missing = sorted(set(by_observation) - settled_ids)
        raise ForwardValidationError(
            f"settlements must explicitly include pending rows (missing={missing})"
        )
    history_ledger_binding = (
        _validate_history_ledger_binding(value)
        if require_history_ledger_binding
        else None
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
    value["history_ledger_binding"] = history_ledger_binding
    value["evidence_contract"] = "v2_pre_match_commitment_and_post_match_settlement"
    value["legacy_uncommitted"] = False
    value["external_timestamp_anchor"] = False
    value["baseline_artifact_replay_complete"] = False
    value["execution_price_source_replay_complete"] = False
    return value


def validate_input(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ForwardValidationError("forward observations must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version == INPUT_SCHEMA_VERSION:
        return _validate_v2_input(payload)
    if schema_version == LEGACY_INPUT_SCHEMA_VERSION:
        return _validate_legacy_input(payload)
    raise ForwardValidationError("unsupported forward-observations schema_version")


def validate_prematch_input(payload: Any) -> dict[str, Any]:
    """Validate an un-settled v2 ledger before it is atomically archived.

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
    normalized = _validate_v2_input(payload, require_history_ledger_binding=False)
    if normalized.get("cohort_closure") is not None:
        raise ForwardValidationError("pre-match ledger cannot carry a cohort closure")
    if any(
        row.get("settlement_status") != "pending"
        or row.get("observed_outcome") is not None
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
        actual = row.get("observed_outcome")
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
        actual = row.get("observed_outcome")
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
        entry = float(execution["entry_decimal_odds"])
        stake = float(execution["stake_units"])
        settlement_state = execution.get("settlement_state")
        win_profit = entry - 1.0
        profit = {
            "win": win_profit,
            "half_win": win_profit / 2.0,
            "push": 0.0,
            "half_loss": -0.5,
            "loss": -1.0,
        }.get(str(settlement_state))
        if profit is None:
            continue
        clv_probability_points = 100.0 * (
            float(execution["closing_no_vig_probability"])
            - float(execution["entry_no_vig_probability"])
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


def _segment_report(
    rows: Sequence[Mapping[str, Any]], *, repetitions: int, seed: int
) -> dict[str, Any]:
    predicted = [row for row in rows if row.get("status") == "predicted"]
    modeled = [
        row
        for row in rows
        if row.get("status") in {"predicted", "abstained"}
        and row.get("model_probabilities") is not None
    ]
    graded = [row for row in modeled if row.get("observed_outcome") is not None]
    executed_graded = [
        row
        for row in predicted
        if row.get("observed_outcome") is not None
        and isinstance(row.get("execution"), Mapping)
    ]
    missing_outcome_ids = [
        str(row.get("observation_id"))
        for row in rows
        if row.get("observed_outcome") is None
    ]
    missing_baseline_ids = {
        baseline: [
            str(row.get("observation_id"))
            for row in graded
            if row.get("baselines", {}).get(baseline) is None
        ]
        for baseline in BASELINE_NAMES
    }
    missing_execution_ids = [
        str(row.get("observation_id"))
        for row in predicted
        if row.get("observed_outcome") is not None
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
    return {
        "eligible_fixtures": len(rows),
        "predicted_fixtures": len(predicted),
        "model_available_fixtures": len(modeled),
        "graded_model_outputs": len(graded),
        "graded_predictions": sum(
            row.get("observed_outcome") is not None for row in predicted
        ),
        "settled_fixtures": len(rows) - len(missing_outcome_ids),
        "missing_outcome_ids": missing_outcome_ids,
        "missing_baseline_ids": missing_baseline_ids,
        "baseline_paired_coverage": {
            baseline: (
                (len(graded) - len(missing_baseline_ids[baseline])) / len(graded)
                if graded
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
    for field in ("league", "market", "lead_time_bucket"):
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
    bookmaker = overall["comparisons"]["bookmaker_no_vig"]
    calibration_error = overall["calibration"]["expected_calibration_error"]
    execution = overall["execution"]
    segment_stability: dict[str, Any] = {}
    for dimension, blocks in segments.items():
        eligible_blocks: dict[str, Any] = {}
        failures: list[str] = []
        for key, block in blocks.items():
            comparison = block["comparisons"]["bookmaker_no_vig"]
            if (
                comparison["sample_count"] < minimum_segment_samples
                or comparison.get("cluster_count", 0) < minimum_segment_clusters
            ):
                continue
            eligible_blocks[key] = comparison
            for metric in ("log_loss_delta", "brier_delta"):
                interval = comparison.get(metric)
                if (
                    not isinstance(interval, Mapping)
                    or interval.get("ci95_high") is None
                    or float(interval["ci95_high"]) >= 0.0
                ):
                    failures.append(f"{key}:{metric}")
        segment_stability[dimension] = {
            "minimum_paired_samples": minimum_segment_samples,
            "minimum_iso_week_clusters": minimum_segment_clusters,
            "eligible_segments": sorted(eligible_blocks),
            "failed_segments": failures,
            "demonstrated": bool(eligible_blocks) and not failures,
        }
    blockers: list[str] = []
    if legacy:
        blockers.append("legacy_uncommitted_observations_are_read_only")
    if protocol_overrides:
        blockers.append("validation_protocol_override_is_experimental")
    if data.get("external_timestamp_anchor") is not True:
        blockers.append("external_timestamp_anchor_not_configured")
    if data.get("baseline_artifact_replay_complete") is not True:
        blockers.append("baseline_artifact_replay_not_demonstrated")
    if data.get("execution_price_source_replay_complete") is not True:
        blockers.append("execution_price_source_replay_not_demonstrated")
    if data.get("cohort_closed") is not True:
        blockers.append("forward_cohort_is_not_closed")
    if overall["missing_outcome_ids"]:
        blockers.append("incomplete_settlement_outcomes")
    if any(overall["missing_baseline_ids"].values()):
        blockers.append("incomplete_paired_baselines")
    if overall["missing_execution_ids"]:
        blockers.append("incomplete_executable_price_evidence")
    if bookmaker["sample_count"] < minimum_confirmation_samples:
        blockers.append("insufficient_paired_same_time_bookmaker_samples")
    if bookmaker.get("sample_count") != overall["graded_model_outputs"]:
        blockers.append("bookmaker_pairing_does_not_cover_every_graded_model_output")
    if bookmaker.get("cluster_count", 0) < minimum_iso_week_clusters:
        blockers.append("insufficient_independent_iso_week_clusters")
    for metric in ("log_loss_delta", "brier_delta"):
        block = bookmaker.get(metric)
        if not isinstance(block, Mapping) or block.get("ci95_high") is None:
            blockers.append(f"bookmaker_{metric}_confidence_interval_unavailable")
        elif float(block["ci95_high"]) >= 0.0:
            blockers.append(f"bookmaker_{metric}_ci95_does_not_support_improvement")
    if (
        calibration_error is None
        or float(calibration_error) > maximum_calibration_error
    ):
        blockers.append("calibration_error_exceeds_5_percent_or_is_unavailable")
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
        blockers.append(
            "classwise_calibration_error_exceeds_5_percent_or_is_unavailable"
        )
    if overall["coverage"] is None:
        blockers.append("coverage_unavailable")
    if execution["sample_count"] < minimum_confirmation_samples:
        blockers.append("insufficient_executable_price_samples")
    roi_ci = execution.get("roi_ci95")
    if (
        not isinstance(roi_ci, Mapping)
        or roi_ci.get("low") is None
        or float(roi_ci["low"]) <= 0
    ):
        blockers.append("executable_price_roi_ci95_not_positive")
    realized_roi_ci = execution.get("realized_roi_ci95")
    if (
        not isinstance(realized_roi_ci, Mapping)
        or realized_roi_ci.get("low") is None
        or float(realized_roi_ci["low"]) <= 0
    ):
        blockers.append("realized_stake_weighted_roi_ci95_not_positive")
    clv_ci = execution.get("clv_ci95")
    if (
        not isinstance(clv_ci, Mapping)
        or clv_ci.get("low") is None
        or float(clv_ci["low"]) <= 0
    ):
        blockers.append("clv_ci95_not_positive")
    for dimension, block in segment_stability.items():
        if not block["demonstrated"]:
            blockers.append(f"{dimension}_stability_not_demonstrated")
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "artifact_type": "soccer_untouched_forward_validation",
        "cohort_id": data["cohort_id"],
        "cohort_hash": data["cohort_manifest"]["cohort_hash"],
        "policy_id": data["policy_id"],
        "policy_hash": data["policy_hash"],
        "input_hash": _hash(data),
        "outcomes": data.get("outcomes"),
        "market_schemas": data.get("market_schemas"),
        "evidence_contract": data.get("evidence_contract"),
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
        "statistical_gate_passed": not blockers,
        "promotion_eligible": False,
        "parameter_change_authorized": False,
        "manual_review_required": True,
        "promotion_blockers": blockers
        or ["manual_independent_review_required_even_when_statistical_gates_pass"],
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

#!/usr/bin/env python3
"""Train and safely select local league-scoped football models.

The manager binds every HT/FT model to one audited ``history_importer`` bundle,
keeps model artifacts in an explicitly selected local directory, and writes a
hash-protected registry.  Prediction produces both the dedicated HT/FT artifact
and the canonical full-time score-model artifact from the embedded full-time
component so the existing market and archive pipeline can consume one matrix.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:  # Works when imported from the repository root.
    from scripts import (
        history_importer,
        htft_holdout_evaluator,
        htft_model,
        score_model,
    )
except ImportError:  # Works when invoked directly as scripts/league_model_manager.py.
    script_directory = str(Path(__file__).resolve().parent)
    if script_directory not in sys.path:
        sys.path.insert(0, script_directory)
    import history_importer  # type: ignore[no-redef]
    import htft_holdout_evaluator  # type: ignore[no-redef]
    import htft_model  # type: ignore[no-redef]
    import score_model  # type: ignore[no-redef]


REGISTRY_ARTIFACT_TYPE = "soccer_league_model_registry"
REGISTRY_SCHEMA_VERSION = "1.4.0"
PREDICTION_BUNDLE_ARTIFACT_TYPE = "soccer_league_prediction_bundle"
PREDICTION_BUNDLE_SCHEMA_VERSION = "1.2.0"
REGISTRY_INSPECTION_ARTIFACT_TYPE = "soccer_league_model_registry_inspection"
REGISTRY_INSPECTION_SCHEMA_VERSION = "1.1.0"
REGISTRY_FILENAME = "registry.json"
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

LEAGUE_NAMES = {
    "brazil_serie_a": "巴甲",
    "norway_eliteserien": "挪超",
    "japan_j1": "日职",
    "usa_mls": "美职联",
    "england_premier_league": "英超",
    "france_ligue_1": "法甲",
    "spain_la_liga": "西甲",
    "germany_bundesliga": "德甲",
    "italy_serie_a": "意甲",
    "korea_k_league_1": "韩K联",
    "sweden_allsvenskan": "瑞典超",
    "finland_veikkausliiga": "芬超",
    "uefa_champions_league": "欧冠",
    "afc_champions_league": "亚冠",
}

DEPLOYMENT_POLICY_VERSION = "htft-deployment-gate/1.1.0"
DEPLOYMENT_STATUSES = ("candidate", "shadow")
FIXED_HOLDOUT_SPLIT_ID = "fixed_holdout_2025"
FIXED_HOLDOUT_ROLE = "model_fit_holdout_selector_development"
FIXED_HOLDOUT_MINIMUM_SAMPLE = 100
FIXED_HOLDOUT_DECISION_RULE = (
    "candidate iff fixed_holdout_2025 has at least 100 known-team samples and the mean "
    "and paired-bootstrap 95% CI upper bound for both nine-class log-loss and "
    "Brier deltas versus the training-only empirical frequency baseline are "
    "negative; otherwise shadow"
)
LEAGUE_PAIR_GATE_EVIDENCE_VERSION = "registry-bound-htft-evidence/1.0.0"
LEAGUE_PAIR_GATE_SOURCE_ROLE = "historical_post_selection_development_evidence"
MODEL_ONLY_PAIR_MASS_THRESHOLD = 0.46
SPECIAL_REGIME_WARNING = (
    "special competition regimes were excluded from production training; "
    "current-regime transfer is unconfirmed"
)
FORMAL_HTFT_INELIGIBLE_REASON = (
    "formal HT/FT promotion is blocked because complete current nine-way HT/FT "
    "odds and clean live-forward validation evidence are unavailable"
)
DEPLOYMENT_POLICY = {
    "version": DEPLOYMENT_POLICY_VERSION,
    "allowed_statuses": list(DEPLOYMENT_STATUSES),
    "formal_htft_eligible": False,
    "formal_htft_ineligible_reason": FORMAL_HTFT_INELIGIBLE_REASON,
}
DEPLOYMENT_FIELD_NAMES = (
    "deployment_status",
    "formal_htft_eligible",
    "formal_htft_ineligible_reason",
    "deployment_policy_version",
    "league_pair_gate_evidence",
)

LEAGUE_PAIR_GATE_EVIDENCE_FIELDS = frozenset(
    {
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
)
FIXED_HOLDOUT_EVIDENCE_FIELDS = frozenset(
    {
        "split_id",
        "role",
        "status",
        "evidence_cohort",
        "sample_count",
        "minimum_sample_count",
        "nine_class_log_loss_mean_delta",
        "nine_class_log_loss_ci95_high",
        "nine_class_brier_mean_delta",
        "nine_class_brier_ci95_high",
        "decision_rule",
    }
)

VALIDATED_TRAINING_CONFIG = {
    "half_time_half_life_days": 730.0,
    "second_half_half_life_days": 365.0,
    "full_time_half_life_days": 365.0,
    "iterations": 1200,
    "learning_rate": 0.03,
    "regularization": 0.02,
    "rho_min": -0.20,
    "rho_max": 0.20,
    "rho_step": 0.01,
    "ipf_tolerance": 1e-12,
    "ipf_max_iterations": 1000,
    "association_smoothing_alpha": 0.5,
    "association_power": 1.0,
}
TRAINING_REGIME_POLICY_VERSION = "regular-only-production-v1"
PRODUCTION_TRAINING_REGIMES = ("regular",)


class LeagueModelManagerError(ValueError):
    """Raised when local datasets, registry entries, or models are unsafe."""


def _deployment_status_from_fixed_holdout(
    fixed_holdout: Mapping[str, Any],
) -> str:
    sample_count = fixed_holdout.get("sample_count")
    log_loss_delta = fixed_holdout.get("nine_class_log_loss_mean_delta")
    log_loss_ci_high = fixed_holdout.get("nine_class_log_loss_ci95_high")
    brier_delta = fixed_holdout.get("nine_class_brier_mean_delta")
    brier_ci_high = fixed_holdout.get("nine_class_brier_ci95_high")
    if (
        not isinstance(sample_count, bool)
        and isinstance(sample_count, int)
        and sample_count >= FIXED_HOLDOUT_MINIMUM_SAMPLE
        and fixed_holdout.get("minimum_sample_count") == FIXED_HOLDOUT_MINIMUM_SAMPLE
        and not isinstance(log_loss_delta, bool)
        and isinstance(log_loss_delta, (int, float))
        and math.isfinite(float(log_loss_delta))
        and float(log_loss_delta) < 0.0
        and not isinstance(log_loss_ci_high, bool)
        and isinstance(log_loss_ci_high, (int, float))
        and math.isfinite(float(log_loss_ci_high))
        and float(log_loss_ci_high) < 0.0
        and not isinstance(brier_delta, bool)
        and isinstance(brier_delta, (int, float))
        and math.isfinite(float(brier_delta))
        and float(brier_delta) < 0.0
        and not isinstance(brier_ci_high, bool)
        and isinstance(brier_ci_high, (int, float))
        and math.isfinite(float(brier_ci_high))
        and float(brier_ci_high) < 0.0
    ):
        return "candidate"
    return "shadow"


def _regime_warning(regime_policy: Mapping[str, Any]) -> str | None:
    excluded_rows = regime_policy.get("excluded_rows")
    if isinstance(excluded_rows, bool) or not isinstance(excluded_rows, int):
        raise LeagueModelManagerError("competition regime excluded_rows is invalid")
    return SPECIAL_REGIME_WARNING if excluded_rows > 0 else None


def _validate_fixed_holdout_decision(decision: Any, *, name: str) -> str:
    if not isinstance(decision, Mapping):
        raise LeagueModelManagerError(f"{name} must be an object")
    if set(decision) != FIXED_HOLDOUT_EVIDENCE_FIELDS:
        raise LeagueModelManagerError(f"{name} fields are invalid")
    if (
        decision.get("split_id") != FIXED_HOLDOUT_SPLIT_ID
        or decision.get("role") != FIXED_HOLDOUT_ROLE
        or decision.get("decision_rule") != FIXED_HOLDOUT_DECISION_RULE
        or decision.get("evidence_cohort") != "known_teams"
        or decision.get("minimum_sample_count") != FIXED_HOLDOUT_MINIMUM_SAMPLE
    ):
        raise LeagueModelManagerError(f"{name} policy is invalid")
    sample_count = decision.get("sample_count")
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count < 0
    ):
        raise LeagueModelManagerError(f"{name}.sample_count is invalid")
    status = decision.get("status")
    if not isinstance(status, str) or not status:
        raise LeagueModelManagerError(f"{name}.status is invalid")
    deltas = (
        decision.get("nine_class_log_loss_mean_delta"),
        decision.get("nine_class_log_loss_ci95_high"),
        decision.get("nine_class_brier_mean_delta"),
        decision.get("nine_class_brier_ci95_high"),
    )
    if status == "evaluated":
        if sample_count < 1 or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in deltas
        ):
            raise LeagueModelManagerError(f"{name} evaluated metrics are invalid")
    elif sample_count != 0 or any(value is not None for value in deltas):
        raise LeagueModelManagerError(f"{name} unevaluated metrics must be empty")
    return _deployment_status_from_fixed_holdout(decision)


def _validate_pair_gate_evidence(
    evidence: Any,
    *,
    league_key: str,
    name: str,
    dataset_manifest_hash: str | None = None,
    evaluation_hash: str | None = None,
    model_hash: str | None = None,
    deployment_status: str | None = None,
    regime_warning: str | None | object = ...,
) -> None:
    if not isinstance(evidence, Mapping):
        raise LeagueModelManagerError(f"{name} must be an object")
    if set(evidence) != LEAGUE_PAIR_GATE_EVIDENCE_FIELDS:
        raise LeagueModelManagerError(
            f"{name} fields do not match the registry-bound evidence contract"
        )
    if evidence.get("version") != LEAGUE_PAIR_GATE_EVIDENCE_VERSION:
        raise LeagueModelManagerError(f"{name}.version is unsupported")
    if evidence.get("league_key") != league_key:
        raise LeagueModelManagerError(f"{name}.league_key does not match")
    if evidence.get("source_role") != LEAGUE_PAIR_GATE_SOURCE_ROLE:
        raise LeagueModelManagerError(f"{name}.source_role is invalid")
    threshold = evidence.get("threshold")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or abs(float(threshold) - MODEL_ONLY_PAIR_MASS_THRESHOLD) > 1e-12
    ):
        raise LeagueModelManagerError(f"{name}.threshold is invalid")
    counts: dict[str, int] = {}
    for field in ("eligible_sample_count", "covered_count", "hit_count"):
        value = evidence.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise LeagueModelManagerError(f"{name}.{field} is invalid")
        counts[field] = value
    if (
        counts["covered_count"] > counts["eligible_sample_count"]
        or counts["hit_count"] > counts["covered_count"]
    ):
        raise LeagueModelManagerError(f"{name} cohort counts are inconsistent")
    if evidence.get("deployment_status") not in DEPLOYMENT_STATUSES:
        raise LeagueModelManagerError(f"{name}.deployment_status is invalid")
    warning = evidence.get("regime_warning")
    if warning is not None and (not isinstance(warning, str) or not warning.strip()):
        raise LeagueModelManagerError(f"{name}.regime_warning is invalid")
    if (
        evidence.get("formal_htft_eligible") is not False
        or evidence.get("production_confidence_eligible") is not False
    ):
        raise LeagueModelManagerError(
            f"{name} cannot authorize formal or production HT/FT confidence"
        )
    for field, expected in (
        ("dataset_manifest_hash", dataset_manifest_hash),
        ("evaluation_hash", evaluation_hash),
        ("model_hash", model_hash),
    ):
        actual = _required_hash(evidence.get(field), f"{name}.{field}")
        if expected is not None and actual != expected:
            raise LeagueModelManagerError(f"{name}.{field} does not match")
    if (
        deployment_status is not None
        and evidence.get("deployment_status") != deployment_status
    ):
        raise LeagueModelManagerError(f"{name}.deployment_status does not match")
    if regime_warning is not ... and warning != regime_warning:
        raise LeagueModelManagerError(f"{name}.regime_warning does not match")


def _validate_deployment_fields(
    artifact: Mapping[str, Any],
    *,
    league_key: str,
    name: str,
    dataset_manifest_hash: str | None = None,
    evaluation_hash: str | None = None,
    model_hash: str | None = None,
    regime_warning: str | None | object = ...,
) -> None:
    deployment_status = artifact.get("deployment_status")
    if deployment_status not in DEPLOYMENT_STATUSES:
        raise LeagueModelManagerError(
            f"{name}.deployment_status does not satisfy the deployment gate"
        )
    expected = {
        "formal_htft_eligible": False,
        "formal_htft_ineligible_reason": FORMAL_HTFT_INELIGIBLE_REASON,
        "deployment_policy_version": DEPLOYMENT_POLICY_VERSION,
    }
    for field, expected_value in expected.items():
        if artifact.get(field) != expected_value:
            raise LeagueModelManagerError(
                f"{name}.{field} does not satisfy the deployment gate"
            )
    _validate_pair_gate_evidence(
        artifact.get("league_pair_gate_evidence"),
        league_key=league_key,
        name=f"{name}.league_pair_gate_evidence",
        dataset_manifest_hash=dataset_manifest_hash,
        evaluation_hash=evaluation_hash,
        model_hash=model_hash,
        deployment_status=str(deployment_status),
        regime_warning=regime_warning,
    )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_aware_datetime(value: str | datetime, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise LeagueModelManagerError(
                f"{name} must be an ISO-8601 datetime"
            ) from exc
    else:
        raise LeagueModelManagerError(f"{name} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LeagueModelManagerError(f"{name} needs an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def _json_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LeagueModelManagerError(
            "artifact contains values that cannot be serialized safely"
        ) from exc


def _canonical_hash(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LeagueModelManagerError("artifact contains non-canonical values") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _file_hash(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise LeagueModelManagerError(f"cannot read file: {path}") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _serialized_file_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json_bytes(value)).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(value)
    try:
        with tempfile.NamedTemporaryFile(
            "wb", delete=False, dir=path.parent, suffix=".tmp"
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        temporary.replace(path)
    except OSError as exc:
        raise LeagueModelManagerError(f"cannot write JSON artifact: {path}") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def calculate_registry_hash(registry: Mapping[str, Any]) -> str:
    payload = dict(registry)
    payload.pop("registry_hash", None)
    return _canonical_hash(payload)


def calculate_prediction_bundle_hash(manifest: Mapping[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("prediction_bundle_hash", None)
    return _canonical_hash(payload)


def calculate_registry_inspection_hash(inspection: Mapping[str, Any]) -> str:
    payload = dict(inspection)
    payload.pop("inspection_hash", None)
    return _canonical_hash(payload)


def _required_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise LeagueModelManagerError(f"{name} must be a SHA-256 hash")
    return value


def _safe_filename(value: Any, name: str, *, suffix: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LeagueModelManagerError(f"{name} is required")
    filename = value.strip()
    if (
        Path(filename).is_absolute()
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or Path(filename).suffix.lower() != suffix
    ):
        raise LeagueModelManagerError(f"{name} must be a local {suffix} filename")
    return filename


def _read_json(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LeagueModelManagerError(f"cannot read {name}: {path}") from exc
    if not isinstance(value, dict):
        raise LeagueModelManagerError(f"{name} must contain a JSON object")
    return value


def _validate_dataset_manifest_hash(manifest: Mapping[str, Any]) -> str:
    stored = _required_hash(manifest.get("bundle_hash"), "manifest.bundle_hash")
    payload = dict(manifest)
    payload.pop("bundle_hash", None)
    calculated = _canonical_hash(payload)
    if stored != calculated:
        raise LeagueModelManagerError(
            "manifest.bundle_hash does not match manifest contents"
        )
    return stored


def load_dataset_bundle(
    dataset_dir: str | Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate an imported dataset bundle and resolve its score CSV files."""

    directory = Path(dataset_dir).resolve()
    if not directory.is_dir():
        raise LeagueModelManagerError(f"dataset directory does not exist: {directory}")
    # Hash checks alone are not sufficient here: an attacker could change a
    # competition-regime label and then consistently re-hash the CSV and
    # manifest.  The importer owns the source-date-to-regime policy, so always
    # run its complete semantic validator before selecting production rows.
    try:
        manifest = history_importer.validate_bundle(directory)
    except history_importer.HistoryImportError as exc:
        raise LeagueModelManagerError(f"invalid history bundle: {exc}") from exc
    if manifest.get("artifact_type") != history_importer.DATASET_ARTIFACT_TYPE:
        raise LeagueModelManagerError("unexpected dataset manifest artifact_type")
    if manifest.get("schema_version") != history_importer.DATASET_SCHEMA_VERSION:
        raise LeagueModelManagerError("unsupported dataset manifest schema_version")
    bundle_hash = _validate_dataset_manifest_hash(manifest)
    raw_leagues = manifest.get("leagues")
    if not isinstance(raw_leagues, list) or not raw_leagues:
        raise LeagueModelManagerError("dataset manifest contains no leagues")

    resolved: list[dict[str, Any]] = []
    observed: set[str] = set()
    for index, raw in enumerate(raw_leagues):
        name = f"manifest.leagues[{index}]"
        if not isinstance(raw, Mapping):
            raise LeagueModelManagerError(f"{name} must be an object")
        league_key = raw.get("league_key")
        if league_key not in LEAGUE_NAMES:
            raise LeagueModelManagerError(f"{name}.league_key is unsupported")
        if league_key in observed:
            raise LeagueModelManagerError(f"duplicate league in manifest: {league_key}")
        observed.add(league_key)
        league_name = raw.get("league")
        if league_name != LEAGUE_NAMES[league_key]:
            raise LeagueModelManagerError(f"{name}.league does not match {league_key}")
        aliases = raw.get("aliases")
        if (
            not isinstance(aliases, list)
            or not aliases
            or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
        ):
            raise LeagueModelManagerError(f"{name}.aliases must be non-empty strings")
        score_dataset = raw.get("score_dataset")
        if not isinstance(score_dataset, Mapping):
            raise LeagueModelManagerError(f"{name}.score_dataset is missing")
        filename = _safe_filename(
            score_dataset.get("file"), f"{name}.score_dataset.file", suffix=".csv"
        )
        expected_hash = _required_hash(
            score_dataset.get("sha256"), f"{name}.score_dataset.sha256"
        )
        rows = score_dataset.get("rows")
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 1:
            raise LeagueModelManagerError(
                f"{name}.score_dataset.rows must be a positive integer"
            )
        source_path = directory / filename
        if not source_path.is_file():
            raise LeagueModelManagerError(
                f"score dataset does not exist: {source_path}"
            )
        actual_hash = _file_hash(source_path)
        if actual_hash != expected_hash:
            raise LeagueModelManagerError(
                f"score dataset hash mismatch for {league_key}"
            )
        resolved.append(
            {
                "league_key": league_key,
                "league": league_name,
                "aliases": list(aliases),
                "score_file": filename,
                "score_path": source_path,
                "score_sha256": expected_hash,
                "rows": rows,
                "bundle_hash": bundle_hash,
            }
        )
    resolved.sort(key=lambda item: item["league_key"])
    return manifest, resolved


def _load_evaluation_artifact(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.is_file():
        raise LeagueModelManagerError(f"evaluation artifact does not exist: {source}")
    try:
        evaluation = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LeagueModelManagerError(
            f"evaluation artifact is not valid UTF-8 JSON: {source}"
        ) from exc
    if not isinstance(evaluation, dict):
        raise LeagueModelManagerError("evaluation artifact must contain an object")
    return evaluation


def _fixed_holdout_evidence(
    league_evaluation: Mapping[str, Any], *, league_key: str
) -> tuple[dict[str, Any], dict[str, int]]:
    splits = league_evaluation.get("splits")
    if not isinstance(splits, list):
        raise LeagueModelManagerError(
            f"evaluation league {league_key} has no fixed splits"
        )
    matches = [
        split
        for split in splits
        if isinstance(split, Mapping)
        and split.get("split_id") == FIXED_HOLDOUT_SPLIT_ID
    ]
    if len(matches) != 1:
        raise LeagueModelManagerError(
            f"evaluation league {league_key} must contain exactly one "
            f"{FIXED_HOLDOUT_SPLIT_ID} split"
        )
    split = matches[0]
    if split.get("role") != FIXED_HOLDOUT_ROLE:
        raise LeagueModelManagerError(
            f"evaluation league {league_key} fixed holdout role is invalid"
        )
    if split.get("status") != "evaluated":
        return (
            {
                "split_id": FIXED_HOLDOUT_SPLIT_ID,
                "role": FIXED_HOLDOUT_ROLE,
                "status": str(split.get("status")),
                "evidence_cohort": "known_teams",
                "sample_count": 0,
                "minimum_sample_count": FIXED_HOLDOUT_MINIMUM_SAMPLE,
                "nine_class_log_loss_mean_delta": None,
                "nine_class_log_loss_ci95_high": None,
                "nine_class_brier_mean_delta": None,
                "nine_class_brier_ci95_high": None,
                "decision_rule": FIXED_HOLDOUT_DECISION_RULE,
            },
            {
                "eligible_sample_count": 0,
                "covered_count": 0,
                "hit_count": 0,
            },
        )
    try:
        comparison = split["model_minus_empirical_baseline_by_team_availability"][
            "known_teams"
        ]
        sample_count = comparison["sample_count"]
        log_loss_delta = comparison["nine_class_log_loss"]["mean_delta"]
        log_loss_ci_high = comparison["nine_class_log_loss"]["ci95_high"]
        brier_delta = comparison["nine_class_brier"]["mean_delta"]
        brier_ci_high = comparison["nine_class_brier"]["ci95_high"]
        pair_gate = split["model_only"]["known_teams"]["pair_mass_gate"]
    except (KeyError, TypeError) as exc:
        raise LeagueModelManagerError(
            f"evaluation league {league_key} fixed holdout evidence is incomplete"
        ) from exc
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count < 1
    ):
        raise LeagueModelManagerError(
            f"evaluation league {league_key} fixed holdout sample_count is invalid"
        )
    for metric_name, value in (
        ("nine-class log-loss delta", log_loss_delta),
        ("nine-class log-loss CI upper bound", log_loss_ci_high),
        ("nine-class Brier delta", brier_delta),
        ("nine-class Brier CI upper bound", brier_ci_high),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise LeagueModelManagerError(
                f"evaluation league {league_key} fixed holdout {metric_name} is invalid"
            )
    threshold = pair_gate.get("threshold")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or abs(float(threshold) - MODEL_ONLY_PAIR_MASS_THRESHOLD) > 1e-12
    ):
        raise LeagueModelManagerError(
            f"evaluation league {league_key} pair-mass threshold is invalid"
        )
    counts: dict[str, int] = {}
    for field in ("eligible_sample_count", "covered_count", "hit_count"):
        value = pair_gate.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise LeagueModelManagerError(
                f"evaluation league {league_key} pair-mass {field} is invalid"
            )
        counts[field] = value
    if (
        counts["eligible_sample_count"] != sample_count
        or counts["covered_count"] > counts["eligible_sample_count"]
        or counts["hit_count"] > counts["covered_count"]
    ):
        raise LeagueModelManagerError(
            f"evaluation league {league_key} pair-mass counts are inconsistent"
        )
    decision = {
        "split_id": FIXED_HOLDOUT_SPLIT_ID,
        "role": FIXED_HOLDOUT_ROLE,
        "status": "evaluated",
        "evidence_cohort": "known_teams",
        "sample_count": sample_count,
        "minimum_sample_count": FIXED_HOLDOUT_MINIMUM_SAMPLE,
        "nine_class_log_loss_mean_delta": float(log_loss_delta),
        "nine_class_log_loss_ci95_high": float(log_loss_ci_high),
        "nine_class_brier_mean_delta": float(brier_delta),
        "nine_class_brier_ci95_high": float(brier_ci_high),
        "decision_rule": FIXED_HOLDOUT_DECISION_RULE,
    }
    return decision, counts


def _validate_source_bound_evaluation(
    evaluation: Mapping[str, Any],
    *,
    dataset_dir: Path,
    dataset_manifest_hash: str,
    league_keys: set[str],
) -> tuple[str, dict[str, dict[str, Any]]]:
    try:
        htft_holdout_evaluator.validate_evaluation(
            evaluation,
            dataset_dir=dataset_dir,
        )
    except htft_holdout_evaluator.HoldoutEvaluationError as exc:
        raise LeagueModelManagerError(
            f"source-bound HT/FT evaluation is invalid: {exc}"
        ) from exc
    evaluation_hash = _required_hash(
        evaluation.get("evaluation_hash"), "evaluation.evaluation_hash"
    )
    if evaluation_hash != htft_holdout_evaluator.calculate_evaluation_hash(evaluation):
        raise LeagueModelManagerError(
            "evaluation_hash does not match evaluation contents"
        )
    dataset = evaluation.get("dataset")
    if (
        not isinstance(dataset, Mapping)
        or dataset.get("manifest_bundle_hash") != dataset_manifest_hash
    ):
        raise LeagueModelManagerError(
            "evaluation dataset manifest hash does not match the training bundle"
        )
    if evaluation.get("fit_config") != htft_holdout_evaluator.PROMOTED_FIT_CONFIG:
        raise LeagueModelManagerError(
            "evaluation fit_config is not the registered promoted configuration"
        )
    promotion = evaluation.get("promotion")
    if (
        not isinstance(promotion, Mapping)
        or promotion.get("registered_manager_compatible") is not True
    ):
        raise LeagueModelManagerError(
            "evaluation is not compatible with the registered manager"
        )
    raw_leagues = evaluation.get("leagues")
    if not isinstance(raw_leagues, list):
        raise LeagueModelManagerError("evaluation contains no league evidence")
    by_league: dict[str, dict[str, Any]] = {}
    for item in raw_leagues:
        if not isinstance(item, Mapping):
            raise LeagueModelManagerError(
                "evaluation league evidence must be an object"
            )
        league_key = item.get("league_key")
        if not isinstance(league_key, str) or league_key in by_league:
            raise LeagueModelManagerError(
                "evaluation league_key is missing or duplicated"
            )
        decision, pair_counts = _fixed_holdout_evidence(item, league_key=league_key)
        by_league[league_key] = {
            "fixed_holdout_evaluation": decision,
            "pair_counts": pair_counts,
            "deployment_status": _deployment_status_from_fixed_holdout(decision),
        }
    if set(by_league) != league_keys:
        missing = sorted(league_keys - set(by_league))
        extra = sorted(set(by_league) - league_keys)
        raise LeagueModelManagerError(
            "evaluation league coverage does not match the training bundle; "
            f"missing={missing}, extra={extra}"
        )
    return evaluation_hash, by_league


def _league_pair_gate_evidence(
    *,
    league_key: str,
    dataset_manifest_hash: str,
    evaluation_hash: str,
    model_hash: str,
    deployment_status: str,
    pair_counts: Mapping[str, int],
    regime_warning: str | None,
) -> dict[str, Any]:
    evidence = {
        "version": LEAGUE_PAIR_GATE_EVIDENCE_VERSION,
        "dataset_manifest_hash": dataset_manifest_hash,
        "evaluation_hash": evaluation_hash,
        "model_hash": model_hash,
        "league_key": league_key,
        "source_role": LEAGUE_PAIR_GATE_SOURCE_ROLE,
        "threshold": MODEL_ONLY_PAIR_MASS_THRESHOLD,
        "eligible_sample_count": pair_counts["eligible_sample_count"],
        "covered_count": pair_counts["covered_count"],
        "hit_count": pair_counts["hit_count"],
        "deployment_status": deployment_status,
        "regime_warning": regime_warning,
        "formal_htft_eligible": False,
        "production_confidence_eligible": False,
    }
    _validate_pair_gate_evidence(
        evidence,
        league_key=league_key,
        name="league_pair_gate_evidence",
        dataset_manifest_hash=dataset_manifest_hash,
        evaluation_hash=evaluation_hash,
        model_hash=model_hash,
        deployment_status=deployment_status,
        regime_warning=regime_warning,
    )
    return evidence


def _expected_htft_model_config(
    promoted_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate the flat promoted registry config to the model artifact shape."""

    def score_config(half_life_key: str) -> dict[str, Any]:
        return {
            "half_life_days": promoted_config[half_life_key],
            "iterations": promoted_config["iterations"],
            "learning_rate": promoted_config["learning_rate"],
            "regularization": promoted_config["regularization"],
            "rho_grid": {
                "minimum": promoted_config["rho_min"],
                "maximum": promoted_config["rho_max"],
                "step": promoted_config["rho_step"],
            },
        }

    return {
        "score_models": {
            "half_time": score_config("half_time_half_life_days"),
            "second_half": score_config("second_half_half_life_days"),
            "full_time": score_config("full_time_half_life_days"),
        },
        "ipf": {
            "tolerance": promoted_config["ipf_tolerance"],
            "max_iterations": promoted_config["ipf_max_iterations"],
        },
    }


def _validate_promoted_model_config(
    model: Mapping[str, Any], promoted_config: Mapping[str, Any]
) -> None:
    """Reject self-consistent models that were fitted outside the promoted config."""

    if dict(promoted_config) != VALIDATED_TRAINING_CONFIG:
        raise LeagueModelManagerError(
            "registry promoted training config is not the approved configuration"
        )
    expected_config = _expected_htft_model_config(promoted_config)
    if model.get("config") != expected_config:
        raise LeagueModelManagerError(
            "model actual training config is not the approved promoted configuration"
        )
    association = model.get("empirical_association")
    if not isinstance(association, Mapping) or (
        association.get("smoothing_alpha")
        != promoted_config["association_smoothing_alpha"]
        or association.get("power") != promoted_config["association_power"]
    ):
        raise LeagueModelManagerError(
            "model empirical association config is not the approved promoted configuration"
        )
    construction = model.get("construction")
    expected_validated = {
        "half_time_half_life_days": promoted_config["half_time_half_life_days"],
        "full_time_half_life_days": promoted_config["full_time_half_life_days"],
        "association_power": promoted_config["association_power"],
    }
    if (
        not isinstance(construction, Mapping)
        or construction.get("validated_configuration") != expected_validated
    ):
        raise LeagueModelManagerError(
            "model validated construction config is not the approved promoted configuration"
        )


def _prepare_production_training_csv(
    dataset: Mapping[str, Any], staging_directory: Path
) -> tuple[Path, dict[str, Any]]:
    """Create a deterministic regular-regime-only training view."""

    source_path = Path(dataset["score_path"])
    try:
        handle = source_path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise LeagueModelManagerError(
            f"cannot read score dataset for regime filtering: {source_path}"
        ) from exc
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "competition_regime" not in reader.fieldnames:
            raise LeagueModelManagerError(
                f"{dataset['league_key']} score dataset must contain the audited "
                "competition_regime column"
            )
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    if len(rows) != dataset["rows"]:
        raise LeagueModelManagerError(
            f"{dataset['league_key']} score row count changed during regime filtering"
        )

    source_counts: dict[str, int] = {}
    included_rows: list[dict[str, str]] = []
    for row_number, row in enumerate(rows, start=2):
        regime = (row.get("competition_regime") or "").strip()
        if not regime:
            raise LeagueModelManagerError(
                f"{dataset['league_key']} row {row_number}: competition_regime is required"
            )
        source_counts[regime] = source_counts.get(regime, 0) + 1
        if regime in PRODUCTION_TRAINING_REGIMES:
            included_rows.append(row)
    if len(included_rows) < 1:
        raise LeagueModelManagerError(
            f"{dataset['league_key']} has no production-eligible regular-regime rows"
        )

    training_path = staging_directory / f"{dataset['league_key']}-regular-training.csv"
    with training_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(included_rows)

    included_counts = {
        regime: count
        for regime, count in sorted(source_counts.items())
        if regime in PRODUCTION_TRAINING_REGIMES
    }
    excluded_counts = {
        regime: count
        for regime, count in sorted(source_counts.items())
        if regime not in PRODUCTION_TRAINING_REGIMES
    }
    policy = {
        "version": TRAINING_REGIME_POLICY_VERSION,
        "source_column": "competition_regime",
        "allowed_regimes": list(PRODUCTION_TRAINING_REGIMES),
        "source_regime_counts": dict(sorted(source_counts.items())),
        "included_regime_counts": included_counts,
        "excluded_regime_counts": excluded_counts,
        "source_rows": len(rows),
        "included_rows": len(included_rows),
        "excluded_rows": len(rows) - len(included_rows),
        "special_regimes_are_not_merged_into_regular_strengths": True,
    }
    return training_path, policy


def _validate_model_binding(
    model: Mapping[str, Any],
    *,
    league_key: str,
    bundle_hash: str,
    entry: Mapping[str, Any] | None = None,
    promoted_config: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    _validate_promoted_model_config(
        model,
        VALIDATED_TRAINING_CONFIG if promoted_config is None else promoted_config,
    )
    training = model.get("training")
    if not isinstance(training, Mapping):
        raise LeagueModelManagerError("model training metadata is missing")
    if training.get("competition_key") != league_key:
        raise LeagueModelManagerError(
            "model competition_key does not match the selected league"
        )
    if training.get("dataset_manifest_hash") != bundle_hash:
        raise LeagueModelManagerError(
            "model dataset manifest hash does not match the registry"
        )
    model_hash = _required_hash(model.get("model_hash"), "model.model_hash")
    source_hash = _required_hash(
        training.get("source_data_hash"), "model.training.source_data_hash"
    )
    cutoff = training.get("end_date")
    if not isinstance(cutoff, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cutoff):
        raise LeagueModelManagerError("model.training.end_date must be an ISO date")
    try:
        datetime.strptime(cutoff, "%Y-%m-%d")
    except ValueError as exc:
        raise LeagueModelManagerError(
            "model.training.end_date must be a valid date"
        ) from exc
    if entry is not None:
        comparisons = {
            "model_hash": model_hash,
            "source_data_hash": source_hash,
            "training_cutoff": cutoff,
            "competition_key": league_key,
            "dataset_manifest_hash": bundle_hash,
        }
        for field, actual in comparisons.items():
            if entry.get(field) != actual:
                raise LeagueModelManagerError(
                    f"registry {field} does not match the selected model"
                )
        if entry.get("training_rows") != training.get("match_count"):
            raise LeagueModelManagerError(
                "registry training_rows does not match model match_count"
            )
    return training


def train_models(
    dataset_dir: str | Path,
    model_dir: str | Path,
    *,
    evaluation_artifact: str | Path,
    iterations: int = 1200,
    learning_rate: float = 0.03,
    regularization: float = 0.02,
) -> dict[str, Any]:
    """Fit a source-validated bundle and bind every model to holdout evidence."""

    requested_fit = {
        "iterations": iterations,
        "learning_rate": learning_rate,
        "regularization": regularization,
    }
    promoted_fit = {key: VALIDATED_TRAINING_CONFIG[key] for key in requested_fit}
    if requested_fit != promoted_fit:
        raise LeagueModelManagerError(
            "registered training parameters are locked to the promoted fixed-season "
            "configuration; use htft_model directly for experiments"
        )

    dataset_directory = Path(dataset_dir).resolve()
    _manifest, datasets = load_dataset_bundle(dataset_directory)
    bundle_hash = datasets[0]["bundle_hash"]
    evaluation = _load_evaluation_artifact(evaluation_artifact)
    evaluation_hash, evaluation_by_league = _validate_source_bound_evaluation(
        evaluation,
        dataset_dir=dataset_directory,
        dataset_manifest_hash=bundle_hash,
        league_keys={dataset["league_key"] for dataset in datasets},
    )
    destination = Path(model_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for dataset in datasets:
        with tempfile.TemporaryDirectory(
            prefix=f"soccer-{dataset['league_key']}-regime-"
        ) as temporary:
            training_path, regime_policy = _prepare_production_training_csv(
                dataset, Path(temporary)
            )
            try:
                model = htft_model.fit_model(
                    training_path,
                    half_time_half_life_days=VALIDATED_TRAINING_CONFIG[
                        "half_time_half_life_days"
                    ],
                    second_half_half_life_days=VALIDATED_TRAINING_CONFIG[
                        "second_half_half_life_days"
                    ],
                    full_time_half_life_days=VALIDATED_TRAINING_CONFIG[
                        "full_time_half_life_days"
                    ],
                    iterations=iterations,
                    learning_rate=learning_rate,
                    regularization=regularization,
                    rho_min=VALIDATED_TRAINING_CONFIG["rho_min"],
                    rho_max=VALIDATED_TRAINING_CONFIG["rho_max"],
                    rho_step=VALIDATED_TRAINING_CONFIG["rho_step"],
                    ipf_tolerance=VALIDATED_TRAINING_CONFIG["ipf_tolerance"],
                    ipf_max_iterations=VALIDATED_TRAINING_CONFIG["ipf_max_iterations"],
                    association_smoothing_alpha=VALIDATED_TRAINING_CONFIG[
                        "association_smoothing_alpha"
                    ],
                    association_power=VALIDATED_TRAINING_CONFIG["association_power"],
                    competition_key=dataset["league_key"],
                    dataset_manifest_hash=bundle_hash,
                )
                htft_model.validate_model(model)
            except (htft_model.HTFTModelError, score_model.ScoreModelError) as exc:
                raise LeagueModelManagerError(
                    f"cannot fit {dataset['league_key']}: {exc}"
                ) from exc
        training = _validate_model_binding(
            model,
            league_key=dataset["league_key"],
            bundle_hash=bundle_hash,
        )
        if training.get("match_count") != regime_policy["included_rows"]:
            raise LeagueModelManagerError(
                f"model match_count does not match filtered training rows for "
                f"{dataset['league_key']}"
            )
        model_hash = model["model_hash"]
        model_filename = f"{dataset['league_key']}-htft-{model_hash.removeprefix('sha256:')[:16]}.json"
        model_path = destination / model_filename
        model_file_hash = _atomic_json(model_path, model)
        evaluation_record = evaluation_by_league[dataset["league_key"]]
        deployment_status = evaluation_record["deployment_status"]
        regime_warning = _regime_warning(regime_policy)
        pair_evidence = _league_pair_gate_evidence(
            league_key=dataset["league_key"],
            dataset_manifest_hash=bundle_hash,
            evaluation_hash=evaluation_hash,
            model_hash=model_hash,
            deployment_status=deployment_status,
            pair_counts=evaluation_record["pair_counts"],
            regime_warning=regime_warning,
        )
        entries.append(
            {
                "league_key": dataset["league_key"],
                "league": dataset["league"],
                "aliases": list(dataset["aliases"]),
                "competition_key": dataset["league_key"],
                "dataset_manifest_hash": bundle_hash,
                "evaluation_hash": evaluation_hash,
                "dataset_file": dataset["score_file"],
                "dataset_file_sha256": dataset["score_sha256"],
                "dataset_rows": dataset["rows"],
                "training_rows": regime_policy["included_rows"],
                "excluded_training_rows": regime_policy["excluded_rows"],
                "competition_regime_policy": regime_policy,
                "source_data_hash": training["source_data_hash"],
                "training_start": training.get("start_date"),
                "training_cutoff": training["end_date"],
                "model_file": model_filename,
                "model_file_sha256": model_file_hash,
                "model_hash": model_hash,
                "full_time_component_model_hash": model["components"]["full_time"][
                    "model_hash"
                ],
                "fixed_holdout_evaluation": copy.deepcopy(
                    evaluation_record["fixed_holdout_evaluation"]
                ),
                "deployment_status": deployment_status,
                "formal_htft_eligible": False,
                "formal_htft_ineligible_reason": FORMAL_HTFT_INELIGIBLE_REASON,
                "deployment_policy_version": DEPLOYMENT_POLICY_VERSION,
                "league_pair_gate_evidence": pair_evidence,
            }
        )

    registry: dict[str, Any] = {
        "artifact_type": REGISTRY_ARTIFACT_TYPE,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "dataset_manifest_hash": bundle_hash,
        "evaluation_hash": evaluation_hash,
        "validated_training_config": dict(VALIDATED_TRAINING_CONFIG),
        "deployment_policy": copy.deepcopy(DEPLOYMENT_POLICY),
        "leagues": sorted(entries, key=lambda item: item["league_key"]),
    }
    registry["registry_hash"] = calculate_registry_hash(registry)
    validate_registry(registry)
    _atomic_json(destination / REGISTRY_FILENAME, registry)
    return registry


def validate_registry(registry: Mapping[str, Any]) -> None:
    if not isinstance(registry, Mapping):
        raise LeagueModelManagerError("registry must be a JSON object")
    if registry.get("artifact_type") != REGISTRY_ARTIFACT_TYPE:
        raise LeagueModelManagerError("unexpected registry artifact_type")
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise LeagueModelManagerError("unsupported registry schema_version")
    stored_hash = _required_hash(registry.get("registry_hash"), "registry_hash")
    if stored_hash != calculate_registry_hash(registry):
        raise LeagueModelManagerError("registry_hash does not match registry contents")
    bundle_hash = _required_hash(
        registry.get("dataset_manifest_hash"), "registry.dataset_manifest_hash"
    )
    evaluation_hash = _required_hash(
        registry.get("evaluation_hash"), "registry.evaluation_hash"
    )
    if registry.get("validated_training_config") != VALIDATED_TRAINING_CONFIG:
        raise LeagueModelManagerError(
            "registry validated_training_config is not the promoted configuration"
        )
    if registry.get("deployment_policy") != DEPLOYMENT_POLICY:
        raise LeagueModelManagerError("registry deployment_policy is invalid")
    leagues = registry.get("leagues")
    if not isinstance(leagues, list) or not leagues:
        raise LeagueModelManagerError("registry contains no league models")
    observed_keys: set[str] = set()
    observed_aliases: set[str] = set()
    for index, entry in enumerate(leagues):
        name = f"registry.leagues[{index}]"
        if not isinstance(entry, Mapping):
            raise LeagueModelManagerError(f"{name} must be an object")
        league_key = entry.get("league_key")
        if (
            not isinstance(league_key, str)
            or league_key not in LEAGUE_NAMES
            or league_key in observed_keys
        ):
            raise LeagueModelManagerError(f"{name}.league_key is invalid or duplicated")
        observed_keys.add(league_key)
        if entry.get("league") != LEAGUE_NAMES[league_key]:
            raise LeagueModelManagerError(f"{name}.league does not match league_key")
        if entry.get("competition_key") != league_key:
            raise LeagueModelManagerError(
                f"{name}.competition_key does not match league_key"
            )
        if entry.get("dataset_manifest_hash") != bundle_hash:
            raise LeagueModelManagerError(
                f"{name}.dataset_manifest_hash does not match registry"
            )
        if entry.get("evaluation_hash") != evaluation_hash:
            raise LeagueModelManagerError(
                f"{name}.evaluation_hash does not match registry"
            )
        aliases = entry.get("aliases")
        if (
            not isinstance(aliases, list)
            or not aliases
            or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
        ):
            raise LeagueModelManagerError(f"{name}.aliases must be non-empty strings")
        required_aliases = {league_key.casefold(), LEAGUE_NAMES[league_key].casefold()}
        normalized_aliases = {alias.strip().casefold() for alias in aliases}
        if not required_aliases.issubset(normalized_aliases):
            raise LeagueModelManagerError(
                f"{name}.aliases omit the key or Chinese name"
            )
        if observed_aliases.intersection(normalized_aliases):
            raise LeagueModelManagerError("registry league aliases are ambiguous")
        observed_aliases.update(normalized_aliases)
        _safe_filename(entry.get("model_file"), f"{name}.model_file", suffix=".json")
        _safe_filename(entry.get("dataset_file"), f"{name}.dataset_file", suffix=".csv")
        for field in (
            "model_file_sha256",
            "model_hash",
            "full_time_component_model_hash",
            "dataset_file_sha256",
            "source_data_hash",
        ):
            _required_hash(entry.get(field), f"{name}.{field}")
        derived_deployment_status = _validate_fixed_holdout_decision(
            entry.get("fixed_holdout_evaluation"),
            name=f"{name}.fixed_holdout_evaluation",
        )
        if entry.get("deployment_status") != derived_deployment_status:
            raise LeagueModelManagerError(
                f"{name}.deployment_status does not match fixed holdout evidence"
            )
        cutoff = entry.get("training_cutoff")
        if not isinstance(cutoff, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", cutoff
        ):
            raise LeagueModelManagerError(f"{name}.training_cutoff must be an ISO date")
        regime_policy = entry.get("competition_regime_policy")
        if not isinstance(regime_policy, Mapping):
            raise LeagueModelManagerError(
                f"{name}.competition_regime_policy is missing"
            )
        if (
            regime_policy.get("version") != TRAINING_REGIME_POLICY_VERSION
            or regime_policy.get("source_column") != "competition_regime"
            or regime_policy.get("allowed_regimes") != list(PRODUCTION_TRAINING_REGIMES)
            or regime_policy.get(
                "special_regimes_are_not_merged_into_regular_strengths"
            )
            is not True
        ):
            raise LeagueModelManagerError(
                f"{name}.competition_regime_policy is invalid"
            )
        count_fields = (
            "source_regime_counts",
            "included_regime_counts",
            "excluded_regime_counts",
        )
        for field in count_fields:
            counts = regime_policy.get(field)
            if not isinstance(counts, Mapping) or any(
                not isinstance(regime, str)
                or not regime
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 1
                for regime, count in counts.items()
            ):
                raise LeagueModelManagerError(f"{name}.{field} is invalid")
        source_counts = regime_policy["source_regime_counts"]
        included_counts = regime_policy["included_regime_counts"]
        excluded_counts = regime_policy["excluded_regime_counts"]
        if set(included_counts) - set(PRODUCTION_TRAINING_REGIMES):
            raise LeagueModelManagerError(f"{name} includes an unapproved regime")
        if set(excluded_counts).intersection(PRODUCTION_TRAINING_REGIMES):
            raise LeagueModelManagerError(f"{name} excludes an approved regime")
        if dict(source_counts) != {**dict(included_counts), **dict(excluded_counts)}:
            raise LeagueModelManagerError(f"{name} regime count partitions changed")
        source_rows = sum(source_counts.values())
        included_rows = sum(included_counts.values())
        excluded_rows = sum(excluded_counts.values())
        if (
            regime_policy.get("source_rows") != source_rows
            or regime_policy.get("included_rows") != included_rows
            or regime_policy.get("excluded_rows") != excluded_rows
            or entry.get("dataset_rows") != source_rows
            or entry.get("training_rows") != included_rows
            or entry.get("excluded_training_rows") != excluded_rows
        ):
            raise LeagueModelManagerError(f"{name} regime row counts are inconsistent")
        _validate_deployment_fields(
            entry,
            league_key=league_key,
            name=name,
            dataset_manifest_hash=bundle_hash,
            evaluation_hash=evaluation_hash,
            model_hash=entry["model_hash"],
            regime_warning=_regime_warning(regime_policy),
        )
        if entry["league_pair_gate_evidence"].get("eligible_sample_count") != entry[
            "fixed_holdout_evaluation"
        ].get("sample_count"):
            raise LeagueModelManagerError(
                f"{name} fixed holdout and pair-gate sample counts disagree"
            )


def load_registry(model_dir: str | Path) -> dict[str, Any]:
    directory = Path(model_dir).resolve()
    if not directory.is_dir():
        raise LeagueModelManagerError(f"model directory does not exist: {directory}")
    registry = _read_json(directory / REGISTRY_FILENAME, "model registry")
    validate_registry(registry)
    return registry


def _resolve_registry_entry(
    registry: Mapping[str, Any], league: str
) -> Mapping[str, Any]:
    if not isinstance(league, str) or not league.strip():
        raise LeagueModelManagerError("league is required")
    requested = league.strip().casefold()
    matches = [
        entry
        for entry in registry["leagues"]
        if requested in {alias.strip().casefold() for alias in entry["aliases"]}
    ]
    if not matches:
        raise LeagueModelManagerError(f"league is not registered: {league}")
    if len(matches) != 1:
        raise LeagueModelManagerError(f"league alias is ambiguous: {league}")
    return matches[0]


def validate_registry_inspection(
    inspection: Mapping[str, Any], *, registry: Mapping[str, Any] | None = None
) -> None:
    if not isinstance(inspection, Mapping):
        raise LeagueModelManagerError("registry inspection must be a JSON object")
    if inspection.get("artifact_type") != REGISTRY_INSPECTION_ARTIFACT_TYPE:
        raise LeagueModelManagerError("unexpected registry inspection artifact_type")
    if inspection.get("schema_version") != REGISTRY_INSPECTION_SCHEMA_VERSION:
        raise LeagueModelManagerError("unsupported registry inspection schema_version")
    _parse_aware_datetime(inspection.get("generated_at"), "inspection.generated_at")
    stored_hash = _required_hash(
        inspection.get("inspection_hash"), "inspection.inspection_hash"
    )
    if stored_hash != calculate_registry_inspection_hash(inspection):
        raise LeagueModelManagerError(
            "inspection_hash does not match registry inspection contents"
        )
    if inspection.get("deployment_policy") != DEPLOYMENT_POLICY:
        raise LeagueModelManagerError(
            "registry inspection deployment_policy is invalid"
        )
    _required_hash(inspection.get("registry_hash"), "inspection.registry_hash")
    _required_hash(
        inspection.get("dataset_manifest_hash"),
        "inspection.dataset_manifest_hash",
    )
    _required_hash(inspection.get("evaluation_hash"), "inspection.evaluation_hash")
    models = inspection.get("models")
    if not isinstance(models, list) or not models:
        raise LeagueModelManagerError("registry inspection contains no models")
    model_count = inspection.get("model_count")
    if (
        isinstance(model_count, bool)
        or not isinstance(model_count, int)
        or model_count != len(models)
    ):
        raise LeagueModelManagerError("registry inspection model_count is inconsistent")
    observed_keys: set[str] = set()
    for index, item in enumerate(models):
        name = f"inspection.models[{index}]"
        if not isinstance(item, Mapping):
            raise LeagueModelManagerError(f"{name} must be an object")
        league_key = item.get("league_key")
        if (
            not isinstance(league_key, str)
            or league_key not in LEAGUE_NAMES
            or league_key in observed_keys
        ):
            raise LeagueModelManagerError(f"{name}.league_key is invalid or duplicated")
        observed_keys.add(league_key)
        if item.get("league") != LEAGUE_NAMES[league_key]:
            raise LeagueModelManagerError(f"{name}.league does not match league_key")
        _required_hash(item.get("model_hash"), f"{name}.model_hash")
        _required_hash(
            item.get("full_time_component_model_hash"),
            f"{name}.full_time_component_model_hash",
        )
        cutoff = item.get("training_cutoff")
        if not isinstance(cutoff, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", cutoff
        ):
            raise LeagueModelManagerError(f"{name}.training_cutoff must be an ISO date")
        derived_status = _validate_fixed_holdout_decision(
            item.get("fixed_holdout_evaluation"),
            name=f"{name}.fixed_holdout_evaluation",
        )
        if item.get("deployment_status") != derived_status:
            raise LeagueModelManagerError(
                f"{name}.deployment_status does not match fixed holdout evidence"
            )
        _validate_deployment_fields(
            item,
            league_key=league_key,
            name=name,
            dataset_manifest_hash=inspection["dataset_manifest_hash"],
            evaluation_hash=inspection["evaluation_hash"],
            model_hash=item["model_hash"],
        )

    if registry is not None:
        validate_registry(registry)
        if inspection.get("registry_hash") != registry.get("registry_hash"):
            raise LeagueModelManagerError(
                "registry inspection registry_hash does not match registry"
            )
        if inspection.get("dataset_manifest_hash") != registry.get(
            "dataset_manifest_hash"
        ):
            raise LeagueModelManagerError(
                "registry inspection dataset_manifest_hash does not match registry"
            )
        if inspection.get("evaluation_hash") != registry.get("evaluation_hash"):
            raise LeagueModelManagerError(
                "registry inspection evaluation_hash does not match registry"
            )
        for item in models:
            entry = _resolve_registry_entry(registry, item["league_key"])
            for field in (
                "league",
                "model_hash",
                "full_time_component_model_hash",
                "training_cutoff",
                "fixed_holdout_evaluation",
                *DEPLOYMENT_FIELD_NAMES,
            ):
                if item.get(field) != entry.get(field):
                    raise LeagueModelManagerError(
                        f"registry inspection {field} does not match registry entry"
                    )


def inspect_registry(
    model_dir: str | Path, league: str | None = None
) -> dict[str, Any]:
    """Return a hash-protected, read-only deployment view of registered models."""

    registry = load_registry(model_dir)
    entries = (
        [_resolve_registry_entry(registry, league)]
        if league is not None
        else list(registry["leagues"])
    )
    models = []
    for entry in entries:
        models.append(
            {
                "league_key": entry["league_key"],
                "league": entry["league"],
                "model_hash": entry["model_hash"],
                "full_time_component_model_hash": entry[
                    "full_time_component_model_hash"
                ],
                "training_cutoff": entry["training_cutoff"],
                "fixed_holdout_evaluation": copy.deepcopy(
                    entry["fixed_holdout_evaluation"]
                ),
                **{
                    field: copy.deepcopy(entry[field])
                    for field in DEPLOYMENT_FIELD_NAMES
                },
            }
        )
    inspection: dict[str, Any] = {
        "artifact_type": REGISTRY_INSPECTION_ARTIFACT_TYPE,
        "schema_version": REGISTRY_INSPECTION_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "registry_hash": registry["registry_hash"],
        "dataset_manifest_hash": registry["dataset_manifest_hash"],
        "evaluation_hash": registry["evaluation_hash"],
        "deployment_policy": copy.deepcopy(registry["deployment_policy"]),
        "model_count": len(models),
        "models": models,
    }
    inspection["inspection_hash"] = calculate_registry_inspection_hash(inspection)
    validate_registry_inspection(inspection, registry=registry)
    return inspection


def _validate_prediction_timing(
    entry: Mapping[str, Any],
    *,
    kickoff: str | datetime,
    generated_at: str | datetime,
) -> None:
    kickoff_time = _parse_aware_datetime(kickoff, "kickoff")
    prediction_time = _parse_aware_datetime(generated_at, "generated_at")
    cutoff = datetime.strptime(entry["training_cutoff"], "%Y-%m-%d").date()
    if cutoff >= kickoff_time.date():
        raise LeagueModelManagerError(
            "registered training cutoff must be strictly before kickoff's UTC date"
        )
    if prediction_time >= kickoff_time:
        raise LeagueModelManagerError("generated_at must be strictly before kickoff")
    if prediction_time.date() < cutoff:
        raise LeagueModelManagerError(
            "generated_at cannot be before the registered training cutoff"
        )


def _validate_probability_consistency(
    htft_prediction: Mapping[str, Any],
    score_prediction: Mapping[str, Any],
    *,
    tolerance: float,
) -> dict[str, Any]:
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise LeagueModelManagerError(
            "consistency_tolerance must be positive and finite"
        )
    try:
        component = htft_prediction["components"]["full_time"]["one_x_two"]
        final_marginal = htft_prediction["htft"]["full_time_marginal"]
        canonical = score_prediction["one_x_two"]
    except (KeyError, TypeError) as exc:
        raise LeagueModelManagerError(
            "prediction artifacts omit full-time 1X2 probabilities"
        ) from exc
    deltas: dict[str, float] = {}
    final_deltas: dict[str, float] = {}
    for result in ("home", "draw", "away"):
        try:
            component_value = float(component[result])
            final_value = float(final_marginal[result])
            canonical_value = float(canonical[result])
        except (KeyError, TypeError, ValueError) as exc:
            raise LeagueModelManagerError(
                "full-time 1X2 probabilities are malformed"
            ) from exc
        if not all(
            math.isfinite(value)
            for value in (component_value, final_value, canonical_value)
        ):
            raise LeagueModelManagerError("full-time 1X2 probabilities must be finite")
        deltas[result] = abs(component_value - canonical_value)
        final_deltas[result] = abs(final_value - canonical_value)
    maximum_delta = max(*deltas.values(), *final_deltas.values())
    if maximum_delta > tolerance:
        raise LeagueModelManagerError(
            "HT/FT and canonical score full-time 1X2 probabilities disagree"
        )
    return {
        "checked": True,
        "tolerance": tolerance,
        "maximum_absolute_delta": maximum_delta,
        "fields": ["home", "draw", "away"],
        "contract": (
            "score.one_x_two equals HTFT components.full_time.one_x_two and "
            "HTFT final full_time_marginal"
        ),
    }


def _assert_artifact_equivalent(
    actual: Any,
    expected: Any,
    name: str,
    *,
    tolerance: float = 1e-12,
) -> None:
    """Compare a derived artifact field while treating finite numbers numerically."""

    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping) or set(actual) != set(expected):
            raise LeagueModelManagerError(f"{name} has unexpected fields")
        for key in expected:
            _assert_artifact_equivalent(
                actual[key], expected[key], f"{name}.{key}", tolerance=tolerance
            )
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise LeagueModelManagerError(f"{name} has an unexpected length")
        for index, (actual_item, expected_item) in enumerate(
            zip(actual, expected, strict=True)
        ):
            _assert_artifact_equivalent(
                actual_item,
                expected_item,
                f"{name}[{index}]",
                tolerance=tolerance,
            )
        return
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        if actual != expected:
            raise LeagueModelManagerError(f"{name} does not match its score matrix")
        return
    if isinstance(expected, (int, float)):
        if isinstance(actual, bool):
            raise LeagueModelManagerError(f"{name} must be numeric")
        try:
            actual_number = float(actual)
            expected_number = float(expected)
        except (TypeError, ValueError) as exc:
            raise LeagueModelManagerError(f"{name} must be numeric") from exc
        if (
            not math.isfinite(actual_number)
            or not math.isfinite(expected_number)
            or abs(actual_number - expected_number) > tolerance
        ):
            raise LeagueModelManagerError(f"{name} does not match its score matrix")
        return
    if actual != expected:
        raise LeagueModelManagerError(f"{name} is inconsistent")


def validate_score_prediction(score_prediction: Mapping[str, Any]) -> None:
    """Recalculate every matrix-derived field in a canonical score artifact."""

    if not isinstance(score_prediction, Mapping):
        raise LeagueModelManagerError("canonical score prediction must be an object")
    if score_prediction.get("artifact_type") != score_model.PREDICTION_ARTIFACT_TYPE:
        raise LeagueModelManagerError("unexpected canonical score artifact_type")
    if score_prediction.get("schema_version") != score_model.PREDICTION_SCHEMA_VERSION:
        raise LeagueModelManagerError("unsupported canonical score schema_version")
    if score_prediction.get("model_version") != score_model.MODEL_VERSION:
        raise LeagueModelManagerError("unsupported canonical score model_version")
    _required_hash(score_prediction.get("model_hash"), "canonical score model_hash")

    generated_at = _parse_aware_datetime(
        score_prediction.get("generated_at"), "canonical score generated_at"
    )
    fixture = score_prediction.get("fixture")
    if not isinstance(fixture, Mapping):
        raise LeagueModelManagerError("canonical score fixture is missing")
    kickoff = _parse_aware_datetime(
        fixture.get("kickoff"), "canonical score fixture.kickoff"
    )
    if generated_at >= kickoff:
        raise LeagueModelManagerError(
            "canonical score prediction must be generated before kickoff"
        )
    if fixture.get("unknown_team_policy") not in {"error", "league_average"}:
        raise LeagueModelManagerError(
            "canonical score unknown_team_policy is unsupported"
        )
    for field in ("home_team", "away_team"):
        if not isinstance(fixture.get(field), str) or not fixture[field].strip():
            raise LeagueModelManagerError(
                f"canonical score fixture.{field} is required"
            )
    if fixture["home_team"] == fixture["away_team"]:
        raise LeagueModelManagerError("canonical score fixture teams must differ")

    provenance = score_prediction.get("provenance")
    if not isinstance(provenance, Mapping):
        raise LeagueModelManagerError("canonical score provenance is missing")
    training = provenance.get("training")
    if not isinstance(training, Mapping):
        raise LeagueModelManagerError("canonical score training provenance is missing")
    _required_hash(
        training.get("source_data_hash"),
        "canonical score training.source_data_hash",
    )
    cutoff = training.get("end_date")
    if not isinstance(cutoff, str):
        raise LeagueModelManagerError("canonical score training.end_date is required")
    try:
        cutoff_date = datetime.strptime(cutoff, "%Y-%m-%d").date()
    except ValueError as exc:
        raise LeagueModelManagerError(
            "canonical score training.end_date must be a valid ISO date"
        ) from exc
    if provenance.get("training_cutoff_date") != cutoff:
        raise LeagueModelManagerError(
            "canonical score training cutoff does not match training metadata"
        )
    if cutoff_date >= kickoff.date() or generated_at.date() < cutoff_date:
        raise LeagueModelManagerError("canonical score training timing is invalid")
    if (
        provenance.get("strictly_before_kickoff_utc_date") is not True
        or provenance.get("generated_before_kickoff") is not True
    ):
        raise LeagueModelManagerError(
            "canonical score provenance timing flags are invalid"
        )

    score_matrix = score_prediction.get("score_matrix")
    if not isinstance(score_matrix, Mapping):
        raise LeagueModelManagerError("canonical score matrix payload is missing")
    matrix = score_matrix.get("probabilities")
    try:
        score_model._validate_matrix(matrix)
    except score_model.ScoreModelError as exc:
        raise LeagueModelManagerError(f"invalid canonical score matrix: {exc}") from exc
    if (
        score_matrix.get("home_goals_max") != len(matrix) - 1
        or score_matrix.get("away_goals_max") != len(matrix[0]) - 1
    ):
        raise LeagueModelManagerError("canonical score matrix bounds are inconsistent")

    derived_fields = {
        "expected_goals": score_model._matrix_expected_goals(matrix),
        "one_x_two": score_model.aggregate_one_x_two(matrix),
        "btts": score_model.aggregate_btts(matrix),
        "goal_ranges": score_model.aggregate_goal_ranges(matrix),
        "exact_scores": score_model.rank_exact_scores(matrix),
    }
    for field, expected in derived_fields.items():
        _assert_artifact_equivalent(
            score_prediction.get(field), expected, f"canonical score {field}"
        )

    for field, aggregator in (
        ("totals", score_model.aggregate_total),
        ("asian_handicaps", score_model.aggregate_asian_handicap),
    ):
        markets = score_prediction.get(field)
        if not isinstance(markets, Mapping):
            raise LeagueModelManagerError(f"canonical score {field} must be an object")
        for key, market in markets.items():
            if not isinstance(key, str) or not isinstance(market, Mapping):
                raise LeagueModelManagerError(f"canonical score {field} is malformed")
            try:
                expected = aggregator(matrix, market.get("side"), market.get("line"))
            except (score_model.ScoreModelError, TypeError, ValueError) as exc:
                raise LeagueModelManagerError(
                    f"canonical score {field}.{key} is invalid"
                ) from exc
            expected_key = f"{expected['side']}_{expected['line']:+g}"
            if key != expected_key:
                raise LeagueModelManagerError(
                    f"canonical score {field}.{key} key is inconsistent"
                )
            _assert_artifact_equivalent(
                market, expected, f"canonical score {field}.{key}"
            )

    tail = score_prediction.get("tail_mass")
    if not isinstance(tail, Mapping):
        raise LeagueModelManagerError("canonical score tail audit is missing")
    try:
        raw_omitted = float(tail.get("raw_omitted_probability"))
        tail_tolerance = float(tail.get("tolerance"))
    except (TypeError, ValueError) as exc:
        raise LeagueModelManagerError(
            "canonical score tail audit is malformed"
        ) from exc
    if (
        not math.isfinite(raw_omitted)
        or not math.isfinite(tail_tolerance)
        or raw_omitted < 0.0
        or not 0.0 < tail_tolerance < 1.0
        or tail.get("tolerance_met") is not (raw_omitted <= tail_tolerance)
        or tail.get("truncated_at_home_goals") != len(matrix) - 1
        or tail.get("truncated_at_away_goals") != len(matrix[0]) - 1
        or tail.get("renormalized") is not True
    ):
        raise LeagueModelManagerError("canonical score tail audit is inconsistent")
    latent_rates = score_prediction.get("latent_rates")
    if not isinstance(latent_rates, Mapping) or set(latent_rates) != {"home", "away"}:
        raise LeagueModelManagerError("canonical score latent rates are missing")
    if any(
        not math.isfinite(float(value)) or float(value) <= 0.0
        for value in latent_rates.values()
    ):
        raise LeagueModelManagerError("canonical score latent rates must be positive")
    warnings = score_prediction.get("warnings")
    if not isinstance(warnings, list) or any(
        not isinstance(item, str) for item in warnings
    ):
        raise LeagueModelManagerError("canonical score warnings must be a string list")


def validate_prediction_bundle(
    manifest: Mapping[str, Any],
    htft_prediction: Mapping[str, Any],
    score_prediction: Mapping[str, Any],
    *,
    registry: Mapping[str, Any] | None = None,
    model: Mapping[str, Any] | None = None,
) -> None:
    """Recompute artifact hashes and cross-check every prediction binding."""

    if not isinstance(manifest, Mapping):
        raise LeagueModelManagerError("prediction bundle manifest must be an object")
    if manifest.get("artifact_type") != PREDICTION_BUNDLE_ARTIFACT_TYPE:
        raise LeagueModelManagerError("unexpected prediction bundle artifact_type")
    if manifest.get("schema_version") != PREDICTION_BUNDLE_SCHEMA_VERSION:
        raise LeagueModelManagerError("unsupported prediction bundle schema_version")
    stored_bundle_hash = _required_hash(
        manifest.get("prediction_bundle_hash"), "prediction_bundle_hash"
    )
    if stored_bundle_hash != calculate_prediction_bundle_hash(manifest):
        raise LeagueModelManagerError(
            "prediction_bundle_hash does not match manifest contents"
        )
    league_key = manifest.get("league_key")
    if not isinstance(league_key, str) or league_key not in LEAGUE_NAMES:
        raise LeagueModelManagerError("prediction bundle league_key is unsupported")
    bundle_dataset_hash = _required_hash(
        manifest.get("dataset_manifest_hash"),
        "prediction bundle dataset_manifest_hash",
    )
    bundle_evaluation_hash = _required_hash(
        manifest.get("evaluation_hash"), "prediction bundle evaluation_hash"
    )
    bundle_model_hash = _required_hash(
        manifest.get("model_hash"), "prediction bundle model_hash"
    )
    _validate_deployment_fields(
        manifest,
        league_key=league_key,
        name="prediction bundle",
        dataset_manifest_hash=bundle_dataset_hash,
        evaluation_hash=bundle_evaluation_hash,
        model_hash=bundle_model_hash,
    )
    try:
        htft_model.validate_prediction(htft_prediction, model=model)
    except htft_model.HTFTModelError as exc:
        raise LeagueModelManagerError(f"invalid HT/FT prediction: {exc}") from exc
    calculated_htft_hash = htft_model.calculate_prediction_hash(htft_prediction)
    if htft_prediction.get("prediction_hash") != calculated_htft_hash:
        raise LeagueModelManagerError("HT/FT prediction hash does not match contents")
    validate_score_prediction(score_prediction)
    for artifact_name, artifact in (
        ("HT/FT prediction", htft_prediction),
        ("canonical score prediction", score_prediction),
    ):
        _validate_deployment_fields(
            artifact,
            league_key=league_key,
            name=artifact_name,
            dataset_manifest_hash=bundle_dataset_hash,
            evaluation_hash=bundle_evaluation_hash,
            model_hash=bundle_model_hash,
        )
        for field in DEPLOYMENT_FIELD_NAMES:
            if artifact.get(field) != manifest.get(field):
                raise LeagueModelManagerError(
                    f"{artifact_name} {field} does not match bundle"
                )
    calculated_score_hash = _canonical_hash(score_prediction)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise LeagueModelManagerError("prediction bundle artifacts are missing")
    htft_record = artifacts.get("htft")
    score_record = artifacts.get("canonical_score")
    if not isinstance(htft_record, Mapping) or not isinstance(score_record, Mapping):
        raise LeagueModelManagerError("prediction bundle artifact records are missing")
    if htft_record.get("artifact_type") != htft_prediction.get("artifact_type"):
        raise LeagueModelManagerError("manifest HT/FT artifact_type does not match")
    if score_record.get("artifact_type") != score_prediction.get("artifact_type"):
        raise LeagueModelManagerError("manifest score artifact_type does not match")
    if htft_record.get("artifact_hash") != calculated_htft_hash:
        raise LeagueModelManagerError("manifest HT/FT artifact hash does not match")
    if score_record.get("artifact_hash") != calculated_score_hash:
        raise LeagueModelManagerError("manifest score artifact hash does not match")
    if htft_record.get("file_sha256") != _serialized_file_hash(htft_prediction):
        raise LeagueModelManagerError("manifest HT/FT file hash does not match")
    if score_record.get("file_sha256") != _serialized_file_hash(score_prediction):
        raise LeagueModelManagerError("manifest score file hash does not match")

    htft_fixture = htft_prediction.get("fixture")
    score_fixture = score_prediction.get("fixture")
    if not isinstance(htft_fixture, Mapping) or not isinstance(score_fixture, Mapping):
        raise LeagueModelManagerError("prediction fixture metadata is missing")
    if manifest.get("fixture") != htft_fixture:
        raise LeagueModelManagerError("manifest fixture does not match HT/FT artifact")
    for field in ("home_team", "away_team", "kickoff", "unknown_team_policy"):
        if score_fixture.get(field) != htft_fixture.get(field):
            raise LeagueModelManagerError(
                f"score and HT/FT fixture {field} do not match"
            )
    if htft_fixture.get("competition_key") != league_key:
        raise LeagueModelManagerError(
            "HT/FT fixture competition_key does not match bundle league"
        )
    model_hash = bundle_model_hash
    if htft_prediction.get("model_hash") != model_hash:
        raise LeagueModelManagerError("HT/FT model hash does not match bundle")
    try:
        component_hash = htft_prediction["components"]["full_time"]["model_hash"]
    except (KeyError, TypeError) as exc:
        raise LeagueModelManagerError(
            "HT/FT full-time component model hash is missing"
        ) from exc
    expected_component_hash = _required_hash(
        manifest.get("full_time_component_model_hash"),
        "bundle full_time_component_model_hash",
    )
    if component_hash != expected_component_hash:
        raise LeagueModelManagerError(
            "HT/FT full-time component hash does not match bundle"
        )
    if score_prediction.get("model_hash") != expected_component_hash:
        raise LeagueModelManagerError(
            "canonical score model hash does not match HT/FT full-time component"
        )
    if score_prediction.get("generated_at") != htft_prediction.get("generated_at"):
        raise LeagueModelManagerError("prediction generated_at values do not match")
    cutoff = manifest.get("training_cutoff")
    try:
        htft_cutoff = htft_prediction["provenance"]["training_cutoff_date"]
        score_cutoff = score_prediction["provenance"]["training_cutoff_date"]
    except (KeyError, TypeError) as exc:
        raise LeagueModelManagerError(
            "prediction training cutoff provenance is missing"
        ) from exc
    if cutoff != htft_cutoff or cutoff != score_cutoff:
        raise LeagueModelManagerError(
            "prediction training cutoffs do not match the registered cutoff"
        )
    consistency_record = manifest.get("full_time_probability_consistency")
    if (
        not isinstance(consistency_record, Mapping)
        or consistency_record.get("checked") is not True
    ):
        raise LeagueModelManagerError(
            "bundle full-time probability consistency audit is missing"
        )
    try:
        consistency_tolerance = float(consistency_record.get("tolerance"))
    except (TypeError, ValueError) as exc:
        raise LeagueModelManagerError(
            "bundle consistency tolerance must be numeric"
        ) from exc
    recalculated_consistency = _validate_probability_consistency(
        htft_prediction,
        score_prediction,
        tolerance=consistency_tolerance,
    )
    try:
        recorded_delta = float(consistency_record.get("maximum_absolute_delta"))
    except (TypeError, ValueError) as exc:
        raise LeagueModelManagerError(
            "bundle consistency maximum delta must be numeric"
        ) from exc
    if (
        not math.isfinite(recorded_delta)
        or abs(recorded_delta - recalculated_consistency["maximum_absolute_delta"])
        > 1e-15
    ):
        raise LeagueModelManagerError(
            "bundle consistency audit does not match prediction artifacts"
        )

    if registry is not None:
        validate_registry(registry)
        if manifest.get("registry_hash") != registry.get("registry_hash"):
            raise LeagueModelManagerError(
                "bundle registry_hash does not match registry"
            )
        if manifest.get("dataset_manifest_hash") != registry.get(
            "dataset_manifest_hash"
        ):
            raise LeagueModelManagerError(
                "bundle dataset manifest hash does not match registry"
            )
        if manifest.get("evaluation_hash") != registry.get("evaluation_hash"):
            raise LeagueModelManagerError(
                "bundle evaluation hash does not match registry"
            )
        entry = _resolve_registry_entry(registry, str(league_key))
        bindings = {
            "model_hash": model_hash,
            "full_time_component_model_hash": expected_component_hash,
            "training_cutoff": cutoff,
            "evaluation_hash": bundle_evaluation_hash,
            **{field: manifest.get(field) for field in DEPLOYMENT_FIELD_NAMES},
        }
        for field, actual in bindings.items():
            if entry.get(field) != actual:
                raise LeagueModelManagerError(
                    f"bundle {field} does not match registry entry"
                )


def predict_registered_model(
    model_dir: str | Path,
    league: str,
    home_team: str,
    away_team: str,
    *,
    kickoff: str | datetime,
    generated_at: str | datetime | None = None,
    max_goals: int = 8,
    hard_max_goals: int = 30,
    tail_tolerance: float = 1e-8,
    allow_large_tail: bool = False,
    unknown_team_policy: str = "error",
    seed_method: str = "empirical_association",
    half_time_anchor: Mapping[str, Any] | None = None,
    full_time_anchor: Mapping[str, Any] | None = None,
    total_markets: Iterable[tuple[str, float]] = (),
    asian_handicaps: Iterable[tuple[str, float]] = (),
    consistency_tolerance: float = 1e-12,
) -> dict[str, Any]:
    """Select a registered league model and return both canonical predictions."""

    if full_time_anchor is not None:
        raise LeagueModelManagerError(
            "full-time external anchors are not supported by the registered manager: "
            "a canonical score matrix with the same anchored marginal cannot yet be "
            "generated; use the research-only htft_model interface instead"
        )
    if seed_method != "empirical_association":
        raise LeagueModelManagerError(
            "registered predictions require empirical_association; experimental score "
            "convolution is available only through the research htft_model interface"
        )
    prediction_time = generated_at if generated_at is not None else _utc_now()
    registry = load_registry(model_dir)
    entry = _resolve_registry_entry(registry, league)
    _validate_prediction_timing(
        entry,
        kickoff=kickoff,
        generated_at=prediction_time,
    )
    directory = Path(model_dir).resolve()
    model_filename = _safe_filename(
        entry.get("model_file"), "registry model_file", suffix=".json"
    )
    model_path = directory / model_filename
    expected_file_hash = _required_hash(
        entry.get("model_file_sha256"), "registry model_file_sha256"
    )
    if _file_hash(model_path) != expected_file_hash:
        raise LeagueModelManagerError("registered model file hash does not match")
    try:
        model = htft_model.load_model(model_path)
    except htft_model.HTFTModelError as exc:
        raise LeagueModelManagerError(f"cannot load registered model: {exc}") from exc
    _validate_model_binding(
        model,
        league_key=entry["league_key"],
        bundle_hash=registry["dataset_manifest_hash"],
        entry=entry,
        promoted_config=registry["validated_training_config"],
    )

    total_markets = tuple(total_markets)
    asian_handicaps = tuple(asian_handicaps)
    try:
        htft_prediction = htft_model.predict_model(
            model,
            home_team,
            away_team,
            kickoff=kickoff,
            generated_at=prediction_time,
            max_goals=max_goals,
            hard_max_goals=hard_max_goals,
            tail_tolerance=tail_tolerance,
            allow_large_tail=allow_large_tail,
            unknown_team_policy=unknown_team_policy,
            half_time_anchor=half_time_anchor,
            full_time_anchor=None,
            seed_method=seed_method,
        )
        score_prediction = score_model.predict_model(
            model["components"]["full_time"],
            home_team,
            away_team,
            kickoff=kickoff,
            generated_at=prediction_time,
            total_markets=total_markets,
            asian_handicaps=asian_handicaps,
            max_goals=max_goals,
            hard_max_goals=hard_max_goals,
            tail_tolerance=tail_tolerance,
            allow_large_tail=allow_large_tail,
            unknown_team_policy=unknown_team_policy,
        )
    except (htft_model.HTFTModelError, score_model.ScoreModelError) as exc:
        raise LeagueModelManagerError(f"prediction failed: {exc}") from exc
    deployment_fields = {
        field: copy.deepcopy(entry[field]) for field in DEPLOYMENT_FIELD_NAMES
    }
    htft_prediction.update(copy.deepcopy(deployment_fields))
    score_prediction.update(copy.deepcopy(deployment_fields))
    htft_prediction["prediction_hash"] = htft_model.calculate_prediction_hash(
        htft_prediction
    )
    consistency = _validate_probability_consistency(
        htft_prediction,
        score_prediction,
        tolerance=consistency_tolerance,
    )
    htft_hash = _required_hash(
        htft_prediction.get("prediction_hash"), "HTFT prediction_hash"
    )
    score_hash = _canonical_hash(score_prediction)
    manifest: dict[str, Any] = {
        "artifact_type": PREDICTION_BUNDLE_ARTIFACT_TYPE,
        "schema_version": PREDICTION_BUNDLE_SCHEMA_VERSION,
        "generated_at": htft_prediction.get("generated_at"),
        "league_key": entry["league_key"],
        "league": entry["league"],
        "registry_hash": registry["registry_hash"],
        "dataset_manifest_hash": registry["dataset_manifest_hash"],
        "evaluation_hash": registry["evaluation_hash"],
        "model_hash": entry["model_hash"],
        "full_time_component_model_hash": entry["full_time_component_model_hash"],
        "training_cutoff": entry["training_cutoff"],
        **copy.deepcopy(deployment_fields),
        "fixture": copy.deepcopy(htft_prediction.get("fixture")),
        "external_anchors": {
            "half_time": half_time_anchor is not None,
            "full_time": False,
        },
        "full_time_probability_consistency": consistency,
        "artifacts": {
            "htft": {
                "artifact_type": htft_prediction.get("artifact_type"),
                "artifact_hash": htft_hash,
                "file_sha256": _serialized_file_hash(htft_prediction),
            },
            "canonical_score": {
                "artifact_type": score_prediction.get("artifact_type"),
                "artifact_hash": score_hash,
                "file_sha256": _serialized_file_hash(score_prediction),
            },
        },
    }
    manifest["prediction_bundle_hash"] = calculate_prediction_bundle_hash(manifest)
    validate_prediction_bundle(
        manifest,
        htft_prediction,
        score_prediction,
        registry=registry,
        model=model,
    )
    return {
        "htft_prediction": htft_prediction,
        "score_prediction": score_prediction,
        "manifest": manifest,
    }


def _parse_probability_triplet(raw: str, name: str) -> dict[str, float]:
    try:
        values = [float(part.strip()) for part in raw.split(",")]
    except ValueError as exc:
        raise LeagueModelManagerError(
            f"{name} must be HOME,DRAW,AWAY probabilities"
        ) from exc
    if len(values) != 3 or any(
        not math.isfinite(value) or value <= 0 for value in values
    ):
        raise LeagueModelManagerError(
            f"{name} must contain three strictly positive finite probabilities"
        )
    if abs(math.fsum(values) - 1.0) > 1e-9:
        raise LeagueModelManagerError(f"{name} probabilities must sum to one")
    return dict(zip(("home", "draw", "away"), values))


def _cli_anchor(
    marginal: str | None,
    source: str | None,
    captured_at: str | None,
    name: str,
) -> dict[str, Any] | None:
    supplied = [value is not None for value in (marginal, source, captured_at)]
    if not any(supplied):
        return None
    if not all(supplied):
        raise LeagueModelManagerError(
            f"{name} anchor requires marginal, source, and captured-at together"
        )
    return {
        "probabilities": _parse_probability_triplet(marginal or "", name),
        "source": source,
        "captured_at": captured_at,
        "de_vigged": True,
    }


def _parse_market(raw: str, name: str, sides: set[str]) -> tuple[str, float]:
    try:
        side, line = raw.split(":", 1)
        side = side.strip().lower()
        numeric_line = float(line)
    except (ValueError, AttributeError) as exc:
        raise LeagueModelManagerError(f"{name} must be SIDE:LINE") from exc
    if side not in sides or not math.isfinite(numeric_line):
        raise LeagueModelManagerError(f"invalid {name}: {raw}")
    return side, numeric_line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train and use audited local league model registries"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    train = subparsers.add_parser(
        "train", help="train every league in a dataset bundle"
    )
    train.add_argument("--dataset-dir", required=True)
    train.add_argument("--model-dir", required=True)
    train.add_argument(
        "--evaluation-artifact",
        "--evaluation",
        dest="evaluation_artifact",
        required=True,
        help=(
            "fixed-season HT/FT evaluation JSON; its source files are reopened "
            "and validated against --dataset-dir"
        ),
    )
    train.add_argument("--iterations", type=int, default=1200)
    train.add_argument("--learning-rate", type=float, default=0.03)
    train.add_argument("--regularization", type=float, default=0.02)

    inspect_command = subparsers.add_parser(
        "inspect", help="inspect model hashes and deployment eligibility"
    )
    inspect_command.add_argument("--model-dir", required=True)
    inspect_command.add_argument(
        "--league", help="optional league key or registered Chinese name"
    )
    inspect_command.add_argument("--output", help="optional inspection JSON artifact")

    predict = subparsers.add_parser(
        "predict", help="predict with a registered league model"
    )
    predict.add_argument("--model-dir", required=True)
    predict.add_argument(
        "--league", required=True, help="league key or registered Chinese name"
    )
    predict.add_argument("--home-team", required=True)
    predict.add_argument("--away-team", required=True)
    predict.add_argument("--kickoff", required=True)
    predict.add_argument("--generated-at")
    predict.add_argument("--output", required=True, help="HT/FT prediction JSON")
    predict.add_argument("--score-output", help="canonical full-time score JSON")
    predict.add_argument("--manifest-output", help="prediction bundle manifest JSON")
    predict.add_argument("--max-goals", type=int, default=8)
    predict.add_argument("--hard-max-goals", type=int, default=30)
    predict.add_argument("--tail-tolerance", type=float, default=1e-8)
    predict.add_argument("--allow-large-tail", action="store_true")
    predict.add_argument(
        "--unknown-team-policy",
        choices=("error", "league_average"),
        default="error",
    )
    predict.add_argument("--half-time-marginal")
    predict.add_argument("--half-time-anchor-source")
    predict.add_argument("--half-time-anchor-captured-at")
    predict.add_argument(
        "--full-time-marginal",
        help="research-only input; rejected because it cannot share the canonical score matrix",
    )
    predict.add_argument("--full-time-anchor-source")
    predict.add_argument("--full-time-anchor-captured-at")
    predict.add_argument("--total", action="append", default=[])
    predict.add_argument("--asian", action="append", default=[])
    return parser


def _output_manifest_with_files(
    manifest: Mapping[str, Any],
    *,
    htft_output: Path,
    score_output: Path | None,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(manifest))
    result["artifacts"]["htft"]["file"] = htft_output.name
    result["artifacts"]["htft"]["saved"] = True
    result["artifacts"]["canonical_score"]["file"] = (
        score_output.name if score_output is not None else None
    )
    result["artifacts"]["canonical_score"]["saved"] = score_output is not None
    result["prediction_bundle_hash"] = calculate_prediction_bundle_hash(result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    try:
        if arguments.command == "train":
            registry = train_models(
                arguments.dataset_dir,
                arguments.model_dir,
                evaluation_artifact=arguments.evaluation_artifact,
                iterations=arguments.iterations,
                learning_rate=arguments.learning_rate,
                regularization=arguments.regularization,
            )
            sys.stdout.write(_json_bytes(registry).decode("utf-8"))
            return 0

        if arguments.command == "inspect":
            inspection = inspect_registry(arguments.model_dir, arguments.league)
            if arguments.output:
                _atomic_json(Path(arguments.output).resolve(), inspection)
            sys.stdout.write(_json_bytes(inspection).decode("utf-8"))
            return 0

        if arguments.score_output and not arguments.manifest_output:
            raise LeagueModelManagerError(
                "--score-output requires --manifest-output so both artifacts remain auditable"
            )
        half_time_anchor = _cli_anchor(
            arguments.half_time_marginal,
            arguments.half_time_anchor_source,
            arguments.half_time_anchor_captured_at,
            "half_time",
        )
        full_time_anchor = _cli_anchor(
            arguments.full_time_marginal,
            arguments.full_time_anchor_source,
            arguments.full_time_anchor_captured_at,
            "full_time",
        )
        totals = tuple(
            _parse_market(value, "total", {"over", "under"})
            for value in arguments.total
        )
        handicaps = tuple(
            _parse_market(value, "Asian handicap", {"home", "away"})
            for value in arguments.asian
        )
        bundle = predict_registered_model(
            arguments.model_dir,
            arguments.league,
            arguments.home_team,
            arguments.away_team,
            kickoff=arguments.kickoff,
            generated_at=arguments.generated_at,
            max_goals=arguments.max_goals,
            hard_max_goals=arguments.hard_max_goals,
            tail_tolerance=arguments.tail_tolerance,
            allow_large_tail=arguments.allow_large_tail,
            unknown_team_policy=arguments.unknown_team_policy,
            half_time_anchor=half_time_anchor,
            full_time_anchor=full_time_anchor,
            total_markets=totals,
            asian_handicaps=handicaps,
        )
        htft_output = Path(arguments.output).resolve()
        score_output = (
            Path(arguments.score_output).resolve() if arguments.score_output else None
        )
        manifest_output = (
            Path(arguments.manifest_output).resolve()
            if arguments.manifest_output
            else None
        )
        output_paths = [htft_output]
        if score_output is not None:
            output_paths.append(score_output)
        if manifest_output is not None:
            output_paths.append(manifest_output)
        if len(set(output_paths)) != len(output_paths):
            raise LeagueModelManagerError("prediction output paths must be distinct")
        output_manifest = _output_manifest_with_files(
            bundle["manifest"],
            htft_output=htft_output,
            score_output=score_output,
        )
        validate_prediction_bundle(
            output_manifest,
            bundle["htft_prediction"],
            bundle["score_prediction"],
        )
        written_htft_hash = _atomic_json(htft_output, bundle["htft_prediction"])
        if written_htft_hash != output_manifest["artifacts"]["htft"]["file_sha256"]:
            raise LeagueModelManagerError("written HT/FT file hash does not match")
        if score_output is not None:
            written_score_hash = _atomic_json(score_output, bundle["score_prediction"])
            if (
                written_score_hash
                != output_manifest["artifacts"]["canonical_score"]["file_sha256"]
            ):
                raise LeagueModelManagerError("written score file hash does not match")
        if manifest_output is not None:
            _atomic_json(manifest_output, output_manifest)
        return 0
    except (
        LeagueModelManagerError,
        htft_model.HTFTModelError,
        score_model.ScoreModelError,
    ) as exc:
        parser.exit(2, f"league_model_manager: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

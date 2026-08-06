#!/usr/bin/env python3
"""Freeze and verify an untouched live-forward prediction policy.

This module deliberately does not decide whether a model is profitable.  It creates the
append-only evidence boundary needed to make a future claim testable: the data/model
lineage, selector, release gates, display policy, and prediction-affecting source files are
hashed before the first fixture enters a cohort.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from soccer_predict import __version__ as SOCCER_PREDICT_VERSION

LEGACY_POLICY_SCHEMA_VERSION = "forward-policy/1.0.0"
POLICY_SCHEMA_VERSION = "forward-policy/2.0.0"
COHORT_SCHEMA_VERSION = "live-forward-cohort/1.0.0"
CLOSURE_SCHEMA_VERSION = "live-forward-cohort-closure/1.0.0"
POLICY_ID_PREFIX = "untouched-live-forward"
ACTIVE_COHORT_NAME = "active-forward-cohort.json"
RECORD_BINDING_SCHEMA_VERSION = "forward-policy-binding/1.0.0"
COMMITTED_RECORD_BINDING_SCHEMA_VERSION = "forward-policy-binding/1.1.0"
PROVENANCE_RECORD_BINDING_SCHEMA_VERSION = "forward-policy-binding/2.0.0"
PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION = "forward-policy-binding/2.1.0"
PROVENANCE_SCHEMA_VERSION = "forward-provenance-binding/1.0.0"
DEFAULT_VALIDATION_PROTOCOL: dict[str, Any] = {
    "schema_version": "forward-validation-protocol/1.0.0",
    "bootstrap_repetitions": 2000,
    "bootstrap_seed": 20260806,
    "minimum_confirmation_samples": 200,
    "minimum_iso_week_clusters": 20,
    "minimum_segment_samples": 40,
    "minimum_segment_clusters": 5,
    "same_time_tolerance_minutes": 5.0,
    "maximum_calibration_error": 0.05,
    "cluster_unit": "kickoff_iso_week",
    "required_baselines": [
        "historical_frequency",
        "independent_htft",
        "simple_poisson_dc",
        "bookmaker_no_vig",
    ],
    "queue_contract": "frozen_fixture_market_manifest",
    "external_timestamp_anchor_required_for_promotion": True,
}

# Changing one of these files can change a probability, a released candidate, or a public
# scenario.  Such a change starts a new cohort; it is never disguised as a harmless fix.
DEFAULT_PROTECTED_FILES = (
    "SKILL.md",
    "scripts/score_model.py",
    "scripts/htft_model.py",
    "scripts/htft_ranker.py",
    "scripts/league_model_manager.py",
    "scripts/joint_scenario_model.py",
    "scripts/joint_path_kernel.py",
    "scripts/corner_model.py",
    "scripts/corner_model_manager.py",
    "scripts/corner_ranker.py",
    "scripts/memory_store.py",
    "scripts/public_market_outlook.py",
    "scripts/prediction_card_renderer.py",
    "scripts/review_card_renderer.py",
    "scripts/plain_text_formatter.py",
    "scripts/lineup_scheduler.py",
    "scripts/review_scheduler.py",
    "scripts/forward_policy.py",
    "scripts/forward_validation.py",
    "scripts/source_evidence.py",
    "soccer_predict/__init__.py",
    "soccer_predict/domain/settlement.py",
    "soccer_predict/domain/probabilities.py",
    "pyproject.toml",
    "clawhub.json",
    "references/model-validation.md",
    "references/prediction-framework.md",
    "references/expanded-markets.md",
    "references/half-time-full-time.md",
    "references/image-output.md",
)
REQUIRED_PROVENANCE_PROTECTED_FILES = {
    "scripts/forward_policy.py",
    "scripts/forward_validation.py",
    "scripts/memory_store.py",
    "scripts/public_market_outlook.py",
    "scripts/prediction_card_renderer.py",
    "scripts/review_card_renderer.py",
    "scripts/plain_text_formatter.py",
    "soccer_predict/__init__.py",
    "pyproject.toml",
    "clawhub.json",
}
RENDERER_POLICY_PROTECTED_FILES = (
    "scripts/public_market_outlook.py",
    "scripts/prediction_card_renderer.py",
    "scripts/review_card_renderer.py",
    "scripts/plain_text_formatter.py",
)


class ForwardPolicyError(ValueError):
    """Raised when a policy/cohort cannot be trusted."""


def _require_sha256(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise ForwardPolicyError(f"{label} must be a lowercase SHA-256 identity")
    return text


def _require_git_commit(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text):
        raise ForwardPolicyError(f"{label} must be a lowercase Git commit SHA")
    return text


def _require_package_version(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
        r"(?:[-+][0-9A-Za-z.-]+)?",
        text,
    ):
        raise ForwardPolicyError(f"{label} must be a semantic package version")
    return text


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _aware_datetime(value: str | datetime, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ForwardPolicyError(f"{label} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardPolicyError(f"{label} must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ForwardPolicyError(f"{label} is not readable UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ForwardPolicyError(f"{label} must be a JSON object: {path}")
    return value


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _git(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ForwardPolicyError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def _declared_artifact_hash(payload: Mapping[str, Any]) -> str | None:
    for field in ("bundle_hash", "manifest_hash", "registry_hash", "model_hash"):
        value = payload.get(field)
        if isinstance(value, str) and value.startswith("sha256:"):
            return value
    return None


def _protected_file_hashes(
    repo_root: Path, protected_files: Sequence[str]
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative in protected_files:
        normalized = Path(relative).as_posix()
        path = repo_root / normalized
        if not path.is_file():
            raise ForwardPolicyError(f"protected policy file is missing: {normalized}")
        hashes[normalized] = _hash_file(path)
    return hashes


def _runtime_policy() -> dict[str, Any]:
    # Imports are intentionally lazy so the policy helper remains usable by packaging and
    # doctor commands even when optional rendering dependencies are absent.
    from scripts import htft_ranker, memory_store, public_market_outlook

    return {
        "market_policy_version": memory_store.STRICT_OOS_POLICY_VERSION,
        "market_status": deepcopy(memory_store.STRICT_OOS_MARKET_STATUS),
        "selector": {
            "confidence_policy_version": memory_store.CONFIDENCE_POLICY_VERSION,
            "confidence_ranking_version": memory_store.CONFIDENCE_RANKING_VERSION,
            "primary_selection_basis": memory_store.PRIMARY_SELECTION_BASIS,
            "htft_component_selector": getattr(
                htft_ranker, "SELECTION_POLICY", "probability_top2_v3_post_selection"
            ),
            "one_primary_per_match": True,
            "marginal_leader_is_not_a_primary": True,
        },
        "release_thresholds": {
            "adverse_minimum_ev": memory_store.ADVERSE_FORMAL_MIN_EV,
            "adverse_minimum_edge_pp": memory_store.ADVERSE_FORMAL_MIN_EDGE_PP,
            "minimum_firms": memory_store.PROVISIONAL_MIN_FIRMS,
            "minimum_corner_firms": memory_store.PROVISIONAL_CORNER_MIN_FIRMS,
            "lineup_change_minimum_confidence_delta": (
                memory_store.LINEUP_CHANGE_MIN_CONFIDENCE_DELTA
            ),
            "market_evidence_ttl_minutes": deepcopy(
                memory_store.MARKET_EVIDENCE_MAX_AGE_MINUTES_BY_STAGE
            ),
        },
        "candidate_evaluation": {
            "schema_version": memory_store.CANDIDATE_EVALUATION_SCHEMA_VERSION,
            "markets": list(memory_store.PRIMARY_MARKETS),
            "gate_categories": list(memory_store.CANDIDATE_GATE_CATEGORIES),
            "at_most_one_shadow_per_match_market": True,
            "shadow_never_counts_as_primary_or_money": True,
        },
        "display_policy": {
            "public_joint_events": "frozen_global_joint_top2",
            "joint_event_count": 2,
            "total_column_semantics": (
                "joint_rank1_range_plus_recomputed_concentration_v1"
            ),
            "marginal_goal_range_reported_separately": True,
            "top2_mass_and_remainder_required": True,
            "uncertainty_schema_version": (
                public_market_outlook.JOINT_UNCERTAINTY_SCHEMA_VERSION
            ),
            "uncertainty_policy": public_market_outlook.JOINT_UNCERTAINTY_POLICY,
            "caller_may_not_reorder_or_replace": True,
            "ellipsis_forbidden": True,
        },
        "validation_protocol": deepcopy(DEFAULT_VALIDATION_PROTOCOL),
    }


def build_policy_manifest(
    *,
    repo_root: str | Path,
    dataset_manifest: str | Path,
    model_registry: str | Path,
    expected_final_merge_commit: str,
    created_at: str | datetime | None = None,
    code_commit: str | None = None,
    protected_files: Sequence[str] = DEFAULT_PROTECTED_FILES,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    dataset_path = Path(dataset_manifest).resolve()
    registry_path = Path(model_registry).resolve()
    dataset = _read_json(dataset_path, "dataset manifest")
    registry = _read_json(registry_path, "model registry")
    dataset_declared_hash = _declared_artifact_hash(dataset)
    registry_declared_hash = _declared_artifact_hash(registry)
    if dataset_declared_hash is None:
        raise ForwardPolicyError("dataset manifest has no declared SHA-256 identity")
    if registry_declared_hash is None:
        raise ForwardPolicyError("model registry has no declared SHA-256 identity")
    dataset_declared_hash = _require_sha256(
        dataset_declared_hash, "dataset declared manifest hash"
    )
    registry_declared_hash = _require_sha256(
        registry_declared_hash, "model registry declared hash"
    )
    registry_dataset_hash = registry.get("dataset_manifest_hash")
    if registry_dataset_hash != dataset_declared_hash:
        raise ForwardPolicyError(
            "model registry must declare the selected dataset manifest hash exactly"
        )
    created = _aware_datetime(created_at or _now_iso(), "created_at")
    expected_commit = _require_git_commit(
        expected_final_merge_commit, "expected final merge commit"
    )
    commit = _require_git_commit(
        code_commit or _git(root, "rev-parse", "HEAD"), "policy code commit"
    )
    if commit != expected_commit:
        raise ForwardPolicyError(
            "policy code commit does not match the explicitly confirmed final merge commit"
        )
    manifest: dict[str, Any] = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "artifact_type": "soccer_prediction_policy_freeze",
        "created_at": created.replace(microsecond=0).isoformat(),
        "software": {
            "package_name": "soccer-predict",
            "package_version": _require_package_version(
                SOCCER_PREDICT_VERSION, "soccer_predict.__version__"
            ),
            "version_source": "soccer_predict.__version__",
        },
        "code": {
            "commit": commit,
            "expected_final_merge_commit": expected_commit,
            "protected_files": _protected_file_hashes(root, protected_files),
        },
        "data": {
            "manifest_path": _relative_path(dataset_path, root),
            "file_sha256": _hash_file(dataset_path),
            "declared_manifest_hash": dataset_declared_hash,
            "schema_version": dataset.get("schema_version"),
            "as_of_date": dataset.get("as_of_date"),
        },
        "models": {
            "registry_path": _relative_path(registry_path, root),
            "file_sha256": _hash_file(registry_path),
            "declared_registry_hash": registry_declared_hash,
            "dataset_manifest_hash": registry_dataset_hash,
            "schema_version": registry.get("schema_version"),
            "validated_training_config": deepcopy(
                registry.get("validated_training_config")
            ),
        },
        "policy": _runtime_policy(),
        "confirmation_contract": {
            "cohort_type": "untouched_live_forward",
            "retrospective_records_allowed": False,
            "parameter_or_threshold_changes_allowed": False,
            "prediction_affecting_bugfix_starts_new_cohort": True,
            "non_prediction_affecting_fix_requires_audited_new_commit": True,
            "clean_head_required_at_freeze_and_cohort_start": True,
            "explicit_final_merge_commit_required": True,
            "all_candidates_abstentions_and_unavailable_markets_required": True,
            "executable_timestamped_prices_required_for_market_comparison": True,
            "promotion_is_manual": True,
            "promotion_requirements": [
                "proper_scores_vs_same_time_bookmaker_no_vig",
                "calibration_without_material_misfit",
                "coverage_and_abstention_reported",
                "league_market_and_lead_time_stability",
                "clustered_confidence_intervals_support_improvement",
                "positive_performance_at_executable_prices_after_slippage",
            ],
        },
    }
    manifest["policy_hash"] = _hash_json(manifest)
    manifest["policy_id"] = (
        f"{POLICY_ID_PREFIX}-{manifest['policy_hash'].split(':', 1)[1][:16]}"
    )
    return manifest


def validate_policy_manifest(
    manifest: Mapping[str, Any], *, repo_root: str | Path | None = None
) -> dict[str, Any]:
    value = deepcopy(dict(manifest))
    schema_version = value.get("schema_version")
    if schema_version not in {LEGACY_POLICY_SCHEMA_VERSION, POLICY_SCHEMA_VERSION}:
        raise ForwardPolicyError("unsupported forward policy schema_version")
    provenance_policy = schema_version == POLICY_SCHEMA_VERSION
    supplied_hash = value.pop("policy_hash", None)
    policy_id = value.pop("policy_id", None)
    expected_hash = _hash_json(value)
    if supplied_hash != expected_hash:
        raise ForwardPolicyError("forward policy hash is invalid")
    expected_id = f"{POLICY_ID_PREFIX}-{expected_hash.split(':', 1)[1][:16]}"
    if policy_id != expected_id:
        raise ForwardPolicyError("forward policy ID is invalid")
    value["policy_hash"] = supplied_hash
    value["policy_id"] = policy_id
    _aware_datetime(str(value.get("created_at") or ""), "created_at")
    if value.get("artifact_type") != "soccer_prediction_policy_freeze":
        raise ForwardPolicyError("forward policy artifact_type is invalid")
    code = value.get("code")
    if not isinstance(code, Mapping):
        raise ForwardPolicyError("forward policy code binding is missing")
    commit = _require_git_commit(code.get("commit"), "forward policy code commit")
    if provenance_policy:
        expected_commit = _require_git_commit(
            code.get("expected_final_merge_commit"),
            "forward policy expected_final_merge_commit",
        )
        if expected_commit != commit:
            raise ForwardPolicyError(
                "forward policy is not bound to its expected final merge commit"
            )
        software = value.get("software")
        if not isinstance(software, Mapping):
            raise ForwardPolicyError("forward policy software binding is missing")
        if (
            set(software) != {"package_name", "package_version", "version_source"}
            or software.get("package_name") != "soccer-predict"
            or software.get("version_source") != "soccer_predict.__version__"
        ):
            raise ForwardPolicyError("forward policy software binding is invalid")
        package_version = _require_package_version(
            software.get("package_version"), "forward policy package_version"
        )
        if package_version != SOCCER_PREDICT_VERSION:
            raise ForwardPolicyError(
                "forward policy package_version does not match soccer_predict.__version__"
            )
    protected = code.get("protected_files")
    if not isinstance(protected, Mapping) or not protected:
        raise ForwardPolicyError("forward policy protected_files are missing")
    if provenance_policy and not REQUIRED_PROVENANCE_PROTECTED_FILES.issubset(
        set(protected)
    ):
        missing = sorted(REQUIRED_PROVENANCE_PROTECTED_FILES - set(protected))
        raise ForwardPolicyError(
            f"forward policy omits required provenance-protected files: {missing}"
        )
    for relative, file_hash in protected.items():
        relative_path = Path(str(relative))
        if (
            not str(relative).strip()
            or relative_path.is_absolute()
            or ".." in relative_path.parts
        ):
            raise ForwardPolicyError(
                "protected policy paths must be non-empty and relative"
            )
        _require_sha256(file_hash, f"protected file {relative}")
    for section, required_fields in (
        (
            "data",
            ("manifest_path", "file_sha256", "declared_manifest_hash"),
        ),
        (
            "models",
            ("registry_path", "file_sha256", "declared_registry_hash"),
        ),
    ):
        block = value.get(section)
        if not isinstance(block, Mapping):
            raise ForwardPolicyError(f"forward policy {section} binding is missing")
        if not str(block.get(required_fields[0]) or "").strip():
            raise ForwardPolicyError(f"forward policy {section} path is missing")
        for field in required_fields[1:]:
            _require_sha256(block.get(field), f"forward policy {section}.{field}")
    if provenance_policy and value["models"].get("dataset_manifest_hash") != value[
        "data"
    ].get("declared_manifest_hash"):
        raise ForwardPolicyError(
            "forward policy model registry is not linked to the frozen dataset manifest"
        )
    runtime = value.get("policy")
    if not isinstance(runtime, Mapping):
        raise ForwardPolicyError("forward policy runtime policy is missing")
    for section in (
        "market_policy_version",
        "market_status",
        "selector",
        "release_thresholds",
        "candidate_evaluation",
        "display_policy",
        *(("validation_protocol",) if provenance_policy else ()),
    ):
        if runtime.get(section) in (None, "", {}, []):
            raise ForwardPolicyError(f"forward policy runtime {section} is missing")
    confirmation = value.get("confirmation_contract")
    if not isinstance(confirmation, Mapping):
        raise ForwardPolicyError("forward policy confirmation contract is missing")
    required_true = (
        "prediction_affecting_bugfix_starts_new_cohort",
        "all_candidates_abstentions_and_unavailable_markets_required",
        "executable_timestamped_prices_required_for_market_comparison",
        "promotion_is_manual",
        *(
            (
                "clean_head_required_at_freeze_and_cohort_start",
                "explicit_final_merge_commit_required",
            )
            if provenance_policy
            else ()
        ),
    )
    if any(confirmation.get(field) is not True for field in required_true):
        raise ForwardPolicyError(
            "forward policy confirmation safeguards are incomplete"
        )
    if (
        confirmation.get("retrospective_records_allowed") is not False
        or confirmation.get("parameter_or_threshold_changes_allowed") is not False
    ):
        raise ForwardPolicyError("forward policy permits retrospective changes")
    requirements = confirmation.get("promotion_requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ForwardPolicyError("forward policy promotion requirements are missing")
    if repo_root is not None:
        root = Path(repo_root).resolve()
        for relative, expected in protected.items():
            path = root / str(relative)
            if not path.is_file() or _hash_file(path) != expected:
                raise ForwardPolicyError(
                    f"prediction-affecting file differs from frozen policy: {relative}"
                )
        for section, path_field, hash_field in (
            ("data", "manifest_path", "file_sha256"),
            ("models", "registry_path", "file_sha256"),
        ):
            block = value.get(section)
            if not isinstance(block, dict):
                raise ForwardPolicyError(f"forward policy {section} binding is missing")
            artifact_path = Path(str(block.get(path_field) or ""))
            if not artifact_path.is_absolute():
                artifact_path = root / artifact_path
            if not artifact_path.is_file() or _hash_file(artifact_path) != block.get(
                hash_field
            ):
                raise ForwardPolicyError(
                    f"frozen {section} artifact is missing or changed: {artifact_path}"
                )
    return value


def build_provenance_binding(
    policy_manifest: Mapping[str, Any], *, cohort_id: str
) -> dict[str, Any]:
    policy = validate_policy_manifest(policy_manifest)
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ForwardPolicyError(
            "legacy forward policies cannot create provenance-complete bindings"
        )
    clean_cohort_id = str(cohort_id or "").strip()
    if not clean_cohort_id or any(character.isspace() for character in clean_cohort_id):
        raise ForwardPolicyError("provenance binding cohort_id is invalid")
    runtime = policy["policy"]
    binding: dict[str, Any] = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "package_version": _require_package_version(
            policy["software"]["package_version"], "provenance package_version"
        ),
        "git_commit_sha": _require_git_commit(
            policy["code"]["commit"], "provenance git_commit_sha"
        ),
        "policy_hash": _require_sha256(policy["policy_hash"], "provenance policy_hash"),
        "validation_config_hash": _hash_json(runtime["validation_protocol"]),
        "dataset_manifest_hash": _require_sha256(
            policy["data"]["declared_manifest_hash"],
            "provenance dataset_manifest_hash",
        ),
        "model_registry_hash": _require_sha256(
            policy["models"]["declared_registry_hash"],
            "provenance model_registry_hash",
        ),
        "renderer_policy_hash": _hash_json(
            {
                "display_policy": runtime["display_policy"],
                "protected_renderer_files": {
                    path: policy["code"]["protected_files"][path]
                    for path in RENDERER_POLICY_PROTECTED_FILES
                },
            }
        ),
        "cohort_id": clean_cohort_id,
    }
    binding["provenance_hash"] = _hash_json(binding)
    return binding


def validate_provenance_binding(
    binding: Any,
    *,
    policy_manifest: Mapping[str, Any],
    cohort_id: str,
) -> dict[str, Any]:
    if not isinstance(binding, Mapping):
        raise ForwardPolicyError("forward provenance binding must be an object")
    value = deepcopy(dict(binding))
    required = {
        "schema_version",
        "package_version",
        "git_commit_sha",
        "policy_hash",
        "validation_config_hash",
        "dataset_manifest_hash",
        "model_registry_hash",
        "renderer_policy_hash",
        "cohort_id",
        "provenance_hash",
    }
    if set(value) != required:
        raise ForwardPolicyError("forward provenance binding fields are incomplete")
    if value.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise ForwardPolicyError("unsupported forward provenance schema_version")
    _require_package_version(value.get("package_version"), "provenance package_version")
    _require_git_commit(value.get("git_commit_sha"), "provenance git_commit_sha")
    for field in (
        "policy_hash",
        "validation_config_hash",
        "dataset_manifest_hash",
        "model_registry_hash",
        "renderer_policy_hash",
        "provenance_hash",
    ):
        _require_sha256(value.get(field), f"provenance {field}")
    expected = build_provenance_binding(policy_manifest, cohort_id=cohort_id)
    if value != expected:
        raise ForwardPolicyError(
            "forward provenance binding does not reproduce from the frozen policy/cohort"
        )
    return value


def policy_directory(base_dir: str | Path) -> Path:
    return Path(base_dir).resolve() / ".codex" / "soccer-predict" / "forward-policies"


def cohort_directory(base_dir: str | Path) -> Path:
    return Path(base_dir).resolve() / ".codex" / "soccer-predict" / "forward-cohorts"


def cohort_manifest_path(base_dir: str | Path, cohort_id: str) -> Path:
    return cohort_directory(base_dir) / f"{cohort_id}.json"


def active_cohort_path(base_dir: str | Path) -> Path:
    return Path(base_dir).resolve() / ".codex" / "soccer-predict" / ACTIVE_COHORT_NAME


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def freeze_policy(
    *,
    base_dir: str | Path,
    repo_root: str | Path,
    dataset_manifest: str | Path,
    model_registry: str | Path,
    expected_final_merge_commit: str,
) -> tuple[Path, dict[str, Any]]:
    root = Path(repo_root).resolve()
    expected_commit = _require_git_commit(
        expected_final_merge_commit, "expected final merge commit"
    )
    head_commit = _require_git_commit(
        _git(root, "rev-parse", "HEAD"), "current Git HEAD"
    )
    if head_commit != expected_commit:
        raise ForwardPolicyError(
            "refusing to freeze: HEAD is not the explicitly confirmed final merge commit"
        )
    dirty = _git(root, "status", "--porcelain", "--untracked-files=normal")
    if dirty:
        raise ForwardPolicyError(
            "refusing to freeze an uncommitted policy; commit the reviewed code first"
        )
    manifest = build_policy_manifest(
        repo_root=root,
        dataset_manifest=dataset_manifest,
        model_registry=model_registry,
        expected_final_merge_commit=expected_commit,
        code_commit=head_commit,
    )
    path = policy_directory(base_dir) / f"{manifest['policy_id']}.json"
    if path.exists():
        existing = validate_policy_manifest(_read_json(path, "existing policy"))
        if existing != manifest:
            raise ForwardPolicyError("policy ID collision with different content")
        return path, existing
    _atomic_json(path, manifest)
    return path, manifest


def start_cohort(
    *,
    base_dir: str | Path,
    policy_file: str | Path,
    cohort_id: str,
    repo_root: str | Path,
    starts_at: str | datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    root = Path(repo_root).resolve()
    dirty = _git(root, "status", "--porcelain", "--untracked-files=normal")
    if dirty:
        raise ForwardPolicyError(
            "refusing to start a cohort from a dirty worktree; use the final clean merge commit"
        )
    active_path = active_cohort_path(base_dir)
    if active_path.exists():
        existing = _read_json(active_path, "active cohort")
        if existing.get("status") == "active":
            raise ForwardPolicyError(
                "an active live-forward cohort already exists; close it before starting another"
            )
    clean_id = str(cohort_id or "").strip()
    if not clean_id or any(character.isspace() for character in clean_id):
        raise ForwardPolicyError(
            "cohort_id must be a non-empty token without whitespace"
        )
    policy_path = Path(policy_file).resolve()
    policy = validate_policy_manifest(
        _read_json(policy_path, "policy file"), repo_root=root
    )
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ForwardPolicyError("legacy forward policies cannot start a new cohort")
    head_commit = _require_git_commit(
        _git(root, "rev-parse", "HEAD"), "current Git HEAD"
    )
    if (
        head_commit != policy["code"]["commit"]
        or head_commit != policy["code"]["expected_final_merge_commit"]
    ):
        raise ForwardPolicyError(
            "cohort start HEAD does not match the frozen final merge commit"
        )
    started = _aware_datetime(starts_at or _now_iso(), "starts_at")
    created = _aware_datetime(str(policy["created_at"]), "policy.created_at")
    if started < created:
        raise ForwardPolicyError("cohort cannot start before its policy was frozen")
    cohort: dict[str, Any] = {
        "schema_version": COHORT_SCHEMA_VERSION,
        "artifact_type": "soccer_untouched_live_forward_cohort",
        "cohort_id": clean_id,
        "status": "active",
        "starts_at": started.replace(microsecond=0).isoformat(),
        "policy_file": str(policy_path),
        "policy_id": policy["policy_id"],
        "policy_hash": policy["policy_hash"],
        "retrospective_records_allowed": False,
        "closed_at": None,
    }
    cohort["cohort_hash"] = _hash_json(cohort)
    immutable_path = cohort_manifest_path(base_dir, clean_id)
    if immutable_path.exists():
        raise ForwardPolicyError("live-forward cohort_id has already been used")
    _atomic_json(immutable_path, cohort)
    _atomic_json(active_path, cohort)
    return active_path, cohort


def close_cohort(
    *, base_dir: str | Path, closed_at: str | datetime | None = None
) -> tuple[Path, dict[str, Any]]:
    active_path = active_cohort_path(base_dir)
    cohort = validate_cohort(_read_json(active_path, "active cohort"))
    if cohort["status"] != "active":
        raise ForwardPolicyError("no active live-forward cohort is available to close")
    closed = _aware_datetime(closed_at or _now_iso(), "closed_at")
    started = _aware_datetime(str(cohort["starts_at"]), "cohort.starts_at")
    if closed < started:
        raise ForwardPolicyError("live-forward cohort cannot close before it started")
    closure: dict[str, Any] = {
        "schema_version": CLOSURE_SCHEMA_VERSION,
        "artifact_type": "soccer_untouched_live_forward_cohort_closure",
        "cohort_id": cohort["cohort_id"],
        "cohort_hash": cohort["cohort_hash"],
        "policy_id": cohort["policy_id"],
        "policy_hash": cohort["policy_hash"],
        "starts_at": cohort["starts_at"],
        "closed_at": closed.replace(microsecond=0).isoformat(),
        "reason": "explicit_policy_boundary",
    }
    closure["closure_hash"] = _hash_json(closure)
    closure_path = cohort_directory(base_dir) / f"{cohort['cohort_id']}-closure.json"
    if closure_path.exists():
        raise ForwardPolicyError("live-forward cohort closure already exists")
    _atomic_json(closure_path, closure)
    closed_pointer = deepcopy(cohort)
    closed_pointer.pop("cohort_hash", None)
    closed_pointer["status"] = "closed"
    closed_pointer["closed_at"] = closure["closed_at"]
    closed_pointer["cohort_hash"] = _hash_json(closed_pointer)
    _atomic_json(active_path, closed_pointer)
    return closure_path, closure


def validate_cohort(cohort: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(cohort))
    if value.get("schema_version") != COHORT_SCHEMA_VERSION:
        raise ForwardPolicyError("unsupported live-forward cohort schema_version")
    supplied_hash = value.pop("cohort_hash", None)
    if supplied_hash != _hash_json(value):
        raise ForwardPolicyError("live-forward cohort hash is invalid")
    value["cohort_hash"] = supplied_hash
    started = _aware_datetime(str(value.get("starts_at") or ""), "cohort.starts_at")
    if value.get("artifact_type") != "soccer_untouched_live_forward_cohort":
        raise ForwardPolicyError("live-forward cohort artifact_type is invalid")
    cohort_id = str(value.get("cohort_id") or "")
    if not cohort_id or any(character.isspace() for character in cohort_id):
        raise ForwardPolicyError("live-forward cohort_id is invalid")
    if not str(value.get("policy_file") or "").strip():
        raise ForwardPolicyError("live-forward cohort policy_file is missing")
    if not str(value.get("policy_id") or "").startswith(POLICY_ID_PREFIX + "-"):
        raise ForwardPolicyError("live-forward cohort policy_id is invalid")
    _require_sha256(value.get("policy_hash"), "live-forward cohort policy_hash")
    if value.get("status") not in {"active", "closed"}:
        raise ForwardPolicyError("live-forward cohort status is invalid")
    if value.get("retrospective_records_allowed") is not False:
        raise ForwardPolicyError("live-forward cohort permits retrospective records")
    closed_at = value.get("closed_at")
    if value["status"] == "active" and closed_at is not None:
        raise ForwardPolicyError("active live-forward cohort cannot have closed_at")
    if value["status"] == "closed":
        closed = _aware_datetime(str(closed_at or ""), "cohort.closed_at")
        if closed < started:
            raise ForwardPolicyError("live-forward cohort closed before it started")
    return value


def validate_closure(
    closure: Mapping[str, Any], *, cohort: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    value = deepcopy(dict(closure))
    if value.get("schema_version") != CLOSURE_SCHEMA_VERSION:
        raise ForwardPolicyError(
            "unsupported live-forward cohort closure schema_version"
        )
    supplied_hash = value.pop("closure_hash", None)
    if supplied_hash != _hash_json(value):
        raise ForwardPolicyError("live-forward cohort closure hash is invalid")
    value["closure_hash"] = supplied_hash
    if value.get("artifact_type") != "soccer_untouched_live_forward_cohort_closure":
        raise ForwardPolicyError("live-forward cohort closure artifact_type is invalid")
    if not str(value.get("cohort_id") or "").strip():
        raise ForwardPolicyError("live-forward cohort closure cohort_id is missing")
    _require_sha256(value.get("cohort_hash"), "live-forward closure cohort_hash")
    _require_sha256(value.get("policy_hash"), "live-forward closure policy_hash")
    if not str(value.get("policy_id") or "").startswith(POLICY_ID_PREFIX + "-"):
        raise ForwardPolicyError("live-forward cohort closure policy_id is invalid")
    started = _aware_datetime(str(value.get("starts_at") or ""), "closure.starts_at")
    closed = _aware_datetime(str(value.get("closed_at") or ""), "closure.closed_at")
    if closed < started:
        raise ForwardPolicyError("live-forward cohort closure predates its start")
    if value.get("reason") != "explicit_policy_boundary":
        raise ForwardPolicyError("live-forward cohort closure reason is invalid")
    if cohort is not None:
        frozen = validate_cohort(cohort)
        if any(
            value.get(field) != frozen.get(field)
            for field in (
                "cohort_id",
                "cohort_hash",
                "policy_id",
                "policy_hash",
                "starts_at",
            )
        ):
            raise ForwardPolicyError(
                "live-forward cohort closure does not bind the immutable cohort"
            )
    return value


def load_active_binding(
    *,
    base_dir: str | Path,
    repo_root: str | Path,
    archived_at: str | datetime,
    observation_commitment_hash: str | None = None,
) -> dict[str, Any] | None:
    path = active_cohort_path(base_dir)
    if not path.exists():
        return None
    cohort = validate_cohort(_read_json(path, "active cohort"))
    if cohort["status"] != "active":
        return None
    policy_file = Path(str(cohort.get("policy_file") or ""))
    policy = validate_policy_manifest(
        _read_json(policy_file, "active cohort policy"), repo_root=repo_root
    )
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ForwardPolicyError(
            "active cohort policy lacks the required provenance contract"
        )
    if policy["policy_id"] != cohort.get("policy_id") or policy[
        "policy_hash"
    ] != cohort.get("policy_hash"):
        raise ForwardPolicyError("active cohort does not bind its policy exactly")
    archived = _aware_datetime(archived_at, "archived_at")
    start = _aware_datetime(str(cohort["starts_at"]), "cohort.starts_at")
    if archived < start:
        raise ForwardPolicyError(
            "record predates the active untouched live-forward cohort"
        )
    root = Path(repo_root).resolve()
    current_commit = _require_git_commit(
        _git(root, "rev-parse", "HEAD"), "recorded code commit"
    )
    if (
        current_commit != policy["code"]["commit"]
        or current_commit != policy["code"]["expected_final_merge_commit"]
    ):
        raise ForwardPolicyError(
            "active record HEAD does not match the frozen final merge commit"
        )
    binding: dict[str, Any] = {
        "schema_version": (
            PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
            if observation_commitment_hash is not None
            else PROVENANCE_RECORD_BINDING_SCHEMA_VERSION
        ),
        "cohort_id": cohort["cohort_id"],
        "cohort_hash": cohort["cohort_hash"],
        "cohort_starts_at": cohort["starts_at"],
        "policy_id": policy["policy_id"],
        "policy_hash": policy["policy_hash"],
        "policy_snapshot": policy,
        "recorded_code_commit": current_commit,
        "archived_at": archived.replace(microsecond=0).isoformat(),
        "untouched_confirmation_eligible": True,
        "provenance_binding": build_provenance_binding(
            policy, cohort_id=str(cohort["cohort_id"])
        ),
    }
    if observation_commitment_hash is not None:
        binding["observation_commitment_hash"] = _require_sha256(
            observation_commitment_hash,
            "observation commitment hash",
        )
    binding["binding_hash"] = _hash_json(binding)
    return binding


def bind_observation_commitment(
    binding: Mapping[str, Any], observation_commitment_hash: str
) -> dict[str, Any]:
    """Bind a pre-kickoff prediction payload without putting its later result in it.

    The input binding must already be a valid base policy/cohort binding.  The returned
    committed binding commits only to the separately hashed pre-match prediction payload; a
    result/settlement is deliberately not part of this hash.
    """

    value = validate_record_binding(binding)
    if value is None:
        raise ForwardPolicyError("forward policy binding is required")
    commitment_hash = _require_sha256(
        observation_commitment_hash,
        "observation commitment hash",
    )
    existing = value.get("observation_commitment_hash")
    if existing is not None and existing != commitment_hash:
        raise ForwardPolicyError(
            "forward policy binding already commits to a different observation"
        )
    value.pop("binding_hash", None)
    provenance_complete = value.get("schema_version") in {
        PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
        PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
    }
    value["schema_version"] = (
        PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
        if provenance_complete
        else COMMITTED_RECORD_BINDING_SCHEMA_VERSION
    )
    value["observation_commitment_hash"] = commitment_hash
    value["binding_hash"] = _hash_json(value)
    return value


def validate_record_binding(binding: Any) -> dict[str, Any] | None:
    if binding is None:
        return None
    if not isinstance(binding, Mapping):
        raise ForwardPolicyError("forward policy binding must be an object")
    value = deepcopy(dict(binding))
    schema_version = value.get("schema_version")
    if schema_version not in {
        RECORD_BINDING_SCHEMA_VERSION,
        COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
        PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
        PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
    }:
        raise ForwardPolicyError("unsupported forward policy binding schema_version")
    supplied = value.pop("binding_hash", None)
    if supplied != _hash_json(value):
        raise ForwardPolicyError("forward policy binding hash is invalid")
    if not str(value.get("cohort_id") or "").strip():
        raise ForwardPolicyError("forward policy binding cohort_id is missing")
    _require_sha256(value.get("cohort_hash"), "forward policy binding cohort_hash")
    _require_sha256(value.get("policy_hash"), "forward policy binding policy_hash")
    recorded_commit = _require_git_commit(
        value.get("recorded_code_commit"), "forward policy binding code commit"
    )
    policy = validate_policy_manifest(value.get("policy_snapshot") or {})
    if (
        value.get("policy_id") != policy["policy_id"]
        or value.get("policy_hash") != policy["policy_hash"]
    ):
        raise ForwardPolicyError("record binding does not match its policy snapshot")
    provenance_complete = schema_version in {
        PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
        PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
    }
    if provenance_complete:
        if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
            raise ForwardPolicyError(
                "provenance-complete binding requires a current forward policy"
            )
        if recorded_commit != policy["code"]["commit"]:
            raise ForwardPolicyError(
                "recorded code commit does not match the frozen policy commit"
            )
        value["provenance_binding"] = validate_provenance_binding(
            value.get("provenance_binding"),
            policy_manifest=policy,
            cohort_id=str(value.get("cohort_id") or ""),
        )
    elif "provenance_binding" in value:
        raise ForwardPolicyError(
            "legacy forward policy binding cannot carry provenance fields"
        )
    archived = _aware_datetime(
        str(value.get("archived_at") or ""), "binding.archived_at"
    )
    started = _aware_datetime(
        str(value.get("cohort_starts_at") or ""), "binding.cohort_starts_at"
    )
    if archived < started:
        raise ForwardPolicyError("record binding predates its untouched cohort")
    if value.get("untouched_confirmation_eligible") is not True:
        raise ForwardPolicyError(
            "record binding is not untouched-confirmation eligible"
        )
    if schema_version in {
        COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
        PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
    }:
        _require_sha256(
            value.get("observation_commitment_hash"),
            "forward policy binding observation_commitment_hash",
        )
    elif "observation_commitment_hash" in value:
        raise ForwardPolicyError(
            "legacy forward policy binding cannot carry an observation commitment"
        )
    value["binding_hash"] = supplied
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", default=".", help="workspace root")
    parser.add_argument("--repo-root", default=".", help="Git repository root")
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser(
        "freeze", help="freeze the complete prediction policy"
    )
    freeze.add_argument("--dataset-manifest", required=True)
    freeze.add_argument("--model-registry", required=True)
    freeze.add_argument(
        "--expected-final-merge-commit",
        required=True,
        help=(
            "exact clean HEAD SHA of the reviewed final merge commit; intermediate "
            "feature-branch commits must not be frozen"
        ),
    )
    start = subparsers.add_parser(
        "start", help="start a new untouched live-forward cohort"
    )
    start.add_argument("--policy-file", required=True)
    start.add_argument("--cohort-id", required=True)
    start.add_argument("--starts-at")
    close = subparsers.add_parser(
        "close", help="close the active cohort at an explicit policy boundary"
    )
    close.add_argument("--closed-at")
    subparsers.add_parser("status", help="validate and print the active cohort")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        if arguments.command == "freeze":
            path, artifact = freeze_policy(
                base_dir=arguments.base_dir,
                repo_root=arguments.repo_root,
                dataset_manifest=arguments.dataset_manifest,
                model_registry=arguments.model_registry,
                expected_final_merge_commit=arguments.expected_final_merge_commit,
            )
        elif arguments.command == "start":
            path, artifact = start_cohort(
                base_dir=arguments.base_dir,
                repo_root=arguments.repo_root,
                policy_file=arguments.policy_file,
                cohort_id=arguments.cohort_id,
                starts_at=arguments.starts_at,
            )
        elif arguments.command == "close":
            path, artifact = close_cohort(
                base_dir=arguments.base_dir,
                closed_at=arguments.closed_at,
            )
        else:
            path = active_cohort_path(arguments.base_dir)
            artifact = validate_cohort(_read_json(path, "active cohort"))
        print(
            json.dumps(
                {"ok": True, "path": str(path), "artifact": artifact},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (ForwardPolicyError, OSError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

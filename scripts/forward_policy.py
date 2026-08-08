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

try:
    from scripts import artifact_lineage, cohort_scope
except ImportError:  # Direct execution from scripts/.
    import artifact_lineage  # type: ignore[no-redef]
    import cohort_scope  # type: ignore[no-redef]

LEGACY_POLICY_SCHEMA_VERSION = "forward-policy/1.0.0"
PREVIOUS_POLICY_SCHEMA_VERSION = "forward-policy/2.0.0"
POLICY_SCHEMA_VERSION = "forward-policy/3.0.0"
LEGACY_COHORT_SCHEMA_VERSION = "live-forward-cohort/1.0.0"
COHORT_SCHEMA_VERSION = "live-forward-cohort/2.0.0"
LEGACY_CLOSURE_SCHEMA_VERSION = "live-forward-cohort-closure/1.0.0"
PREVIOUS_FULL_CLOSURE_SCHEMA_VERSION = "live-forward-cohort-closure/2.0.0"
CLOSURE_SCHEMA_VERSION = "live-forward-cohort-closure/2.1.0"
PREVIOUS_RECORD_MANIFEST_SCHEMA_VERSION = "live-forward-record-manifest/1.0.0"
PREVIOUS_FULL_RECORD_MANIFEST_SCHEMA_VERSION = "live-forward-record-manifest/2.0.0"
PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION = (
    "live-forward-record-manifest/2.1.0"
)
RECORD_MANIFEST_SCHEMA_VERSION = "live-forward-record-manifest/2.2.0"
POLICY_ID_PREFIX = "untouched-live-forward"
ACTIVE_COHORT_NAME = "active-forward-cohort.json"
RECORD_BINDING_SCHEMA_VERSION = "forward-policy-binding/1.0.0"
COMMITTED_RECORD_BINDING_SCHEMA_VERSION = "forward-policy-binding/1.1.0"
PREVIOUS_PROVENANCE_RECORD_BINDING_SCHEMA_VERSION = "forward-policy-binding/2.0.0"
PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION = (
    "forward-policy-binding/2.1.0"
)
PROVENANCE_RECORD_BINDING_SCHEMA_VERSION = "forward-policy-binding/3.0.0"
PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION = "forward-policy-binding/3.1.0"
PREVIOUS_PROVENANCE_SCHEMA_VERSION = "forward-provenance-binding/1.0.0"
PROVENANCE_SCHEMA_VERSION = "forward-provenance-binding/2.0.0"
PROVENANCE_POLICY_SCHEMA_VERSIONS = frozenset(
    {PREVIOUS_POLICY_SCHEMA_VERSION, POLICY_SCHEMA_VERSION}
)
LOCAL_INTEGRITY_SHADOW_KIND = "local-integrity-shadow-v2"
PROMOTABLE_CONFIRMATION_KIND = "promotable-confirmation-v2"
UNTOUCHED_ELIGIBILITY_SCOPE = "pre_kickoff_integrity_only_not_promotion"
LOCAL_ASSURANCE_SCOPE = "local_integrity_only"
PROMOTABLE_ASSURANCE_SCOPE = "promotable_confirmation"
COHORT_KINDS = (
    LOCAL_INTEGRITY_SHADOW_KIND,
    PROMOTABLE_CONFIRMATION_KIND,
)
MISSING_PROMOTABLE_ADAPTERS = (
    "external_timestamp_anchor_adapter",
    "baseline_artifact_replay_adapter",
    "entry_price_source_replay_adapter",
    "closing_price_source_replay_adapter",
)
PROMOTION_REQUIREMENTS = (
    "proper_scores_within_same_canonical_outcome_space",
    "split_line_bookmaker_prices_limited_to_price_space_ev_and_clv",
    "calibration_without_material_misfit",
    "coverage_and_abstention_reported",
    "league_market_and_lead_time_stability",
    "clustered_confidence_intervals_support_improvement",
    "positive_performance_at_executable_prices_after_slippage",
)
DEFAULT_VALIDATION_PROTOCOL: dict[str, Any] = {
    "schema_version": "forward-validation-protocol/2.0.0",
    "bootstrap_repetitions": 2000,
    "bootstrap_seed": 20260806,
    "minimum_confirmation_samples": 200,
    "minimum_iso_week_clusters": 20,
    "minimum_segment_samples": 40,
    "minimum_segment_clusters": 5,
    "same_time_tolerance_minutes": 5.0,
    "maximum_calibration_error": 0.05,
    "cluster_unit": "kickoff_iso_week",
    "required_model_space_baselines": [
        "historical_frequency",
        "independent_htft",
        "simple_poisson_dc",
    ],
    "bookmaker_price_baseline": "bookmaker_no_vig",
    "bookmaker_proper_score_scope": "categorical_same_outcome_space_only",
    "five_state_evaluation_scope": (
        "settlement_state_scores_ev_roi_plus_price_space_clv"
    ),
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
    "scripts/publication_outlook.py",
    "scripts/official_primary.py",
    "scripts/gate_stats.py",
    "scripts/review_training_export.py",
    "scripts/prediction_card_renderer.py",
    "scripts/review_card_renderer.py",
    "scripts/plain_text_formatter.py",
    "scripts/lineup_scheduler.py",
    "scripts/review_scheduler.py",
    "scripts/forward_policy.py",
    "scripts/forward_validation.py",
    "scripts/source_evidence.py",
    "scripts/fundamental_evidence.py",
    "scripts/execution_evidence.py",
    "scripts/artifact_lineage.py",
    "scripts/cohort_scope.py",
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
    "references/plain-text-output.md",
    "references/review-framework.md",
)
REQUIRED_PROVENANCE_PROTECTED_FILES = {
    "scripts/forward_policy.py",
    "scripts/forward_validation.py",
    "scripts/artifact_lineage.py",
    "scripts/cohort_scope.py",
    "scripts/fundamental_evidence.py",
    "scripts/execution_evidence.py",
    "scripts/memory_store.py",
    "scripts/public_market_outlook.py",
    "scripts/publication_outlook.py",
    "scripts/official_primary.py",
    "scripts/gate_stats.py",
    "scripts/review_training_export.py",
    "scripts/prediction_card_renderer.py",
    "scripts/review_card_renderer.py",
    "scripts/plain_text_formatter.py",
    "soccer_predict/__init__.py",
    "pyproject.toml",
    "clawhub.json",
    "references/plain-text-output.md",
    "references/review-framework.md",
}
PRE_3_6_PROVENANCE_PROTECTED_FILES = frozenset(
    {
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
)
RENDERER_POLICY_PROTECTED_FILES = (
    "scripts/public_market_outlook.py",
    "scripts/publication_outlook.py",
    "scripts/official_primary.py",
    "scripts/prediction_card_renderer.py",
    "scripts/review_card_renderer.py",
    "scripts/plain_text_formatter.py",
)
PRE_3_6_RENDERER_POLICY_PROTECTED_FILES = (
    "scripts/public_market_outlook.py",
    "scripts/prediction_card_renderer.py",
    "scripts/review_card_renderer.py",
    "scripts/plain_text_formatter.py",
)
PRE_3_13_PROVENANCE_PROTECTED_FILES = frozenset(
    {
        "scripts/forward_policy.py",
        "scripts/forward_validation.py",
        "scripts/artifact_lineage.py",
        "scripts/cohort_scope.py",
        "scripts/fundamental_evidence.py",
        "scripts/execution_evidence.py",
        "scripts/memory_store.py",
        "scripts/public_market_outlook.py",
        "scripts/publication_outlook.py",
        "scripts/gate_stats.py",
        "scripts/prediction_card_renderer.py",
        "scripts/review_card_renderer.py",
        "scripts/plain_text_formatter.py",
        "soccer_predict/__init__.py",
        "pyproject.toml",
        "clawhub.json",
        "references/plain-text-output.md",
    }
)
PRE_3_13_RENDERER_POLICY_PROTECTED_FILES = (
    "scripts/public_market_outlook.py",
    "scripts/publication_outlook.py",
    "scripts/prediction_card_renderer.py",
    "scripts/review_card_renderer.py",
    "scripts/plain_text_formatter.py",
)
COHORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
POLICY_ID_PATTERN = re.compile(rf"^{re.escape(POLICY_ID_PREFIX)}-[0-9a-f]{{16}}$")
WINDOWS_RESERVED_FILE_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
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


def _policy_contract_for_package(
    package_version: Any,
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Return the immutable protected-file contract for a frozen package release."""

    version = _require_package_version(
        package_version, "forward policy package_version"
    )
    release = tuple(
        int(part) for part in re.split(r"[-+]", version, maxsplit=1)[0].split(".")
    )
    if release < (3, 6, 0):
        return (
            PRE_3_6_PROVENANCE_PROTECTED_FILES,
            PRE_3_6_RENDERER_POLICY_PROTECTED_FILES,
        )
    if release < (3, 13, 0):
        return (
            PRE_3_13_PROVENANCE_PROTECTED_FILES,
            PRE_3_13_RENDERER_POLICY_PROTECTED_FILES,
        )
    return (
        frozenset(REQUIRED_PROVENANCE_PROTECTED_FILES),
        RENDERER_POLICY_PROTECTED_FILES,
    )


def _release_at_least(package_version: Any, minimum: tuple[int, int, int]) -> bool:
    version = _require_package_version(package_version, "package_version")
    release = tuple(
        int(part) for part in re.split(r"[-+]", version, maxsplit=1)[0].split(".")
    )
    return release >= minimum


def closure_schema_contract(package_version: Any) -> dict[str, str | None]:
    """Return the exact closure schemas frozen by one package release."""

    if _release_at_least(package_version, (3, 10, 0)):
        return {
            "closure": CLOSURE_SCHEMA_VERSION,
            "record_manifest": RECORD_MANIFEST_SCHEMA_VERSION,
            "denominator": cohort_scope.DENOMINATOR_SCHEMA_VERSION,
            "event": cohort_scope.EVENT_SCHEMA_VERSION,
        }
    if _release_at_least(package_version, (3, 9, 0)):
        return {
            "closure": PREVIOUS_FULL_CLOSURE_SCHEMA_VERSION,
            "record_manifest": PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
            "denominator": cohort_scope.PREVIOUS_EVENT_BOUND_DENOMINATOR_SCHEMA_VERSION,
            "event": cohort_scope.EVENT_SCHEMA_VERSION,
        }
    if _release_at_least(package_version, (3, 8, 0)):
        return {
            "closure": PREVIOUS_FULL_CLOSURE_SCHEMA_VERSION,
            "record_manifest": PREVIOUS_FULL_RECORD_MANIFEST_SCHEMA_VERSION,
            "denominator": cohort_scope.PREVIOUS_DENOMINATOR_SCHEMA_VERSION,
            "event": cohort_scope.EVENT_SCHEMA_VERSION,
        }
    return {
        "closure": None,
        "record_manifest": None,
        "denominator": None,
        "event": None,
    }


def _policy_uses_role_aware_lineage(policy: Mapping[str, Any]) -> bool:
    software = policy.get("software")
    return bool(
        policy.get("schema_version") == POLICY_SCHEMA_VERSION
        and isinstance(software, Mapping)
        and _release_at_least(software.get("package_version"), (3, 7, 0))
    )


def _require_cohort_kind(value: Any, label: str) -> str:
    kind = str(value or "").strip()
    if kind not in COHORT_KINDS:
        raise ForwardPolicyError(f"{label} must be one of: {', '.join(COHORT_KINDS)}")
    return kind


def _require_cohort_id(value: Any, label: str = "cohort_id") -> str:
    if not isinstance(value, str) or not COHORT_ID_PATTERN.fullmatch(value):
        raise ForwardPolicyError(
            f"{label} must start with an ASCII letter or digit and contain only "
            "ASCII letters, digits, dot, underscore, or hyphen"
        )
    if value in {".", ".."} or ".." in value or value.endswith("."):
        raise ForwardPolicyError(f"{label} contains an unsafe dot segment")
    if value.split(".", 1)[0].upper() in WINDOWS_RESERVED_FILE_STEMS:
        raise ForwardPolicyError(f"{label} is a reserved Windows device name")
    return value


def _require_policy_id(value: Any, label: str = "policy_id") -> str:
    policy_id = str(value or "")
    if not POLICY_ID_PATTERN.fullmatch(policy_id):
        raise ForwardPolicyError(f"{label} is invalid")
    return policy_id


def _safe_child_path(directory: Path, filename: str, label: str) -> Path:
    root = directory.resolve()
    candidate = (root / filename).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ForwardPolicyError(f"{label} escapes its canonical directory") from exc
    if relative.parent != Path("."):
        raise ForwardPolicyError(f"{label} must be a direct canonical child")
    return candidate


def _require_available_cohort_kind(value: Any, label: str) -> str:
    kind = _require_cohort_kind(value, label)
    if kind == PROMOTABLE_CONFIRMATION_KIND:
        raise ForwardPolicyError(
            f"{PROMOTABLE_CONFIRMATION_KIND} is unavailable; missing required "
            f"adapters: {', '.join(MISSING_PROMOTABLE_ADAPTERS)}"
        )
    return kind


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
    return datetime.now(timezone.utc).isoformat()


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
    from scripts import (
        gate_stats,
        htft_ranker,
        memory_store,
        official_primary,
        public_market_outlook,
        publication_outlook,
    )

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
            "recent_gate_diagnostics_schema_version": gate_stats.SCHEMA_VERSION,
            "recent_distinct_match_windows": [50, 100],
            "recent_gate_diagnostics_are_not_performance": True,
            "mandatory_official_primary_schema_version": (
                official_primary.OFFICIAL_PRIMARY_SCHEMA_VERSION
            ),
            "mandatory_official_primary_selection_policy": (
                official_primary.OFFICIAL_PRIMARY_SELECTION_POLICY
            ),
            "betting_primary_definition_unchanged": True,
            "official_primary_is_non_monetary": True,
            "review_training_sample_schema_version": "review-training-sample/1.0.0",
            "same_active_cohort_model_update_allowed": False,
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
            "formal_primary_definition_unchanged": True,
            "observation_primary_schema_version": (
                publication_outlook.PUBLICATION_OUTLOOK_SCHEMA_VERSION
            ),
            "observation_primary_selection_policy": (
                publication_outlook.OBSERVATION_PRIMARY_SELECTION_POLICY
            ),
            "observation_requires_all_non_release_gates": True,
            "observation_never_occupies_primary_cell": True,
            "observation_never_counts_as_primary_or_money": True,
            "blocker_types": ["data", "value", "policy", "safety"],
            "initial_stage_requires_t_minus_30_followup_message": True,
            "lineup_stage_reports_observation_transition": True,
        },
        "validation_protocol": deepcopy(DEFAULT_VALIDATION_PROTOCOL),
    }


def _confirmation_contract(cohort_kind: str) -> dict[str, Any]:
    return {
        "cohort_type": "untouched_live_forward",
        "cohort_kind": _require_cohort_kind(cohort_kind, "policy cohort_kind"),
        "untouched_confirmation_eligible_scope": UNTOUCHED_ELIGIBILITY_SCOPE,
        "promotion_requires_cohort_kind": PROMOTABLE_CONFIRMATION_KIND,
        "local_shadow_promotion_eligible": False,
        "retrospective_records_allowed": False,
        "parameter_or_threshold_changes_allowed": False,
        "prediction_affecting_bugfix_starts_new_cohort": True,
        "non_prediction_affecting_fix_requires_audited_new_commit": True,
        "clean_head_required_at_freeze_and_cohort_start": True,
        "explicit_final_merge_commit_required": True,
        "all_candidates_abstentions_and_unavailable_markets_required": True,
        "executable_timestamped_prices_required_for_market_comparison": True,
        "promotion_is_manual": True,
        "promotion_requirements": list(PROMOTION_REQUIREMENTS),
    }


def build_policy_manifest(
    *,
    repo_root: str | Path,
    dataset_manifest: str | Path,
    model_registry: str | Path,
    corner_dataset_manifest: str | Path,
    corner_model_registry: str | Path,
    cohort_scope_file: str | Path,
    expected_final_merge_commit: str,
    cohort_kind: str,
    created_at: str | datetime | None = None,
    code_commit: str | None = None,
    protected_files: Sequence[str] = DEFAULT_PROTECTED_FILES,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    frozen_cohort_kind = _require_cohort_kind(cohort_kind, "policy cohort_kind")
    dataset_path = Path(dataset_manifest).resolve()
    registry_path = Path(model_registry).resolve()
    corner_dataset_path = Path(corner_dataset_manifest).resolve()
    corner_registry_path = Path(corner_model_registry).resolve()
    scope_path = Path(cohort_scope_file).resolve()
    dataset = _read_json(dataset_path, "dataset manifest")
    registry = _read_json(registry_path, "model registry")
    role_aware_release = _release_at_least(SOCCER_PREDICT_VERSION, (3, 7, 0))
    try:
        frozen_lineage = (
            artifact_lineage.build_lineage(
                repo_root=root,
                data_manifests={
                    "football_history": dataset_path,
                    "corner_history": corner_dataset_path,
                },
                model_registries={
                    "football_htft": registry_path,
                    "corner": corner_registry_path,
                },
            )
            if role_aware_release
            else None
        )
        frozen_scope = (
            cohort_scope.load_scope(scope_path) if role_aware_release else None
        )
    except (
        artifact_lineage.ArtifactLineageError,
        cohort_scope.CohortScopeError,
    ) as exc:
        raise ForwardPolicyError(
            "role-aware data/model lineage or cohort scope is invalid"
        ) from exc
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
        "confirmation_contract": _confirmation_contract(frozen_cohort_kind),
    }
    if role_aware_release:
        assert frozen_lineage is not None and frozen_scope is not None
        manifest["artifact_lineage"] = frozen_lineage
        manifest["cohort_scope"] = {
            "scope_path": _relative_path(scope_path, root),
            "file_sha256": _hash_file(scope_path),
            "scope_id": frozen_scope["scope_id"],
            "scope_hash": frozen_scope["scope_hash"],
            "scope_snapshot": frozen_scope,
        }
    manifest["policy_hash"] = _hash_json(manifest)
    manifest["policy_id"] = (
        f"{POLICY_ID_PREFIX}-{manifest['policy_hash'].split(':', 1)[1][:16]}"
    )
    return manifest


def validate_policy_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an immutable policy snapshot without consulting the current runtime.

    Historical policies must remain replayable after ``soccer-predict`` is upgraded.  This
    validator therefore checks the content-addressed identity, schema, semantic package
    version, and all internal lineage links, but deliberately does not require the frozen
    package version or files to equal the currently installed checkout.  Creation and active
    cohort paths use :func:`validate_active_runtime_policy_manifest` for those additional
    checks.
    """

    value = deepcopy(dict(manifest))
    schema_version = value.get("schema_version")
    if schema_version not in {
        LEGACY_POLICY_SCHEMA_VERSION,
        PREVIOUS_POLICY_SCHEMA_VERSION,
        POLICY_SCHEMA_VERSION,
    }:
        raise ForwardPolicyError("unsupported forward policy schema_version")
    provenance_policy = schema_version in {
        PREVIOUS_POLICY_SCHEMA_VERSION,
        POLICY_SCHEMA_VERSION,
    }
    current_policy = schema_version == POLICY_SCHEMA_VERSION
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
        frozen_package_version = _require_package_version(
            software.get("package_version"), "forward policy package_version"
        )
    else:
        frozen_package_version = None
    role_aware_release = bool(
        current_policy
        and frozen_package_version
        and _release_at_least(frozen_package_version, (3, 7, 0))
    )
    protected = code.get("protected_files")
    if not isinstance(protected, Mapping) or not protected:
        raise ForwardPolicyError("forward policy protected_files are missing")
    required_protected_files = (
        _policy_contract_for_package(frozen_package_version)[0]
        if provenance_policy
        else frozenset()
    )
    if provenance_policy and not required_protected_files.issubset(set(protected)):
        missing = sorted(required_protected_files - set(protected))
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
    if role_aware_release:
        try:
            value["artifact_lineage"] = artifact_lineage.validate_lineage(
                value.get("artifact_lineage")
            )
        except artifact_lineage.ArtifactLineageError as exc:
            raise ForwardPolicyError(
                "forward policy role-aware artifact lineage is invalid"
            ) from exc
        football_data = value["artifact_lineage"]["data_manifests"]["football_history"]
        football_models = value["artifact_lineage"]["model_registries"]["football_htft"]
        if football_data.get("declared_manifest_hash") != value["data"].get(
            "declared_manifest_hash"
        ) or football_models.get("declared_registry_hash") != value["models"].get(
            "declared_registry_hash"
        ):
            raise ForwardPolicyError(
                "legacy football aliases do not match the role-aware artifact lineage"
            )
        scope_block = value.get("cohort_scope")
        if not isinstance(scope_block, Mapping) or set(scope_block) != {
            "scope_path",
            "file_sha256",
            "scope_id",
            "scope_hash",
            "scope_snapshot",
        }:
            raise ForwardPolicyError("forward policy cohort_scope binding is missing")
        _require_sha256(scope_block.get("file_sha256"), "cohort scope file hash")
        try:
            frozen_scope = cohort_scope.validate_scope(scope_block["scope_snapshot"])
        except cohort_scope.CohortScopeError as exc:
            raise ForwardPolicyError("forward policy cohort scope is invalid") from exc
        if (
            scope_block.get("scope_id") != frozen_scope["scope_id"]
            or scope_block.get("scope_hash") != frozen_scope["scope_hash"]
        ):
            raise ForwardPolicyError(
                "forward policy cohort scope aliases do not match its snapshot"
            )
    elif "artifact_lineage" in value or "cohort_scope" in value:
        raise ForwardPolicyError(
            "pre-3.7 forward policies cannot carry role-aware lineage or cohort scope"
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
    if current_policy:
        frozen_cohort_kind = _require_cohort_kind(
            confirmation.get("cohort_kind"), "forward policy cohort_kind"
        )
        if dict(confirmation) != _confirmation_contract(frozen_cohort_kind):
            raise ForwardPolicyError(
                "forward-policy/3.0.0 confirmation contract does not match runtime"
            )
    elif "cohort_kind" in confirmation:
        raise ForwardPolicyError("pre-v3 forward policies cannot carry cohort_kind")
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
    return value


def validate_active_runtime_policy_manifest(
    manifest: Mapping[str, Any], *, repo_root: str | Path
) -> dict[str, Any]:
    """Validate a policy for creation/use by the current installed runtime.

    Unlike historical replay, an active cohort may only use the current package version and
    the exact frozen checkout, dataset, and model-registry files.
    """

    value = validate_policy_manifest(manifest)
    if value.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ForwardPolicyError(
            "legacy forward policies cannot be activated by the current runtime"
        )
    confirmation = value["confirmation_contract"]
    _require_available_cohort_kind(
        confirmation["cohort_kind"], "forward policy cohort_kind"
    )
    if value["policy"] != _runtime_policy():
        raise ForwardPolicyError(
            "forward policy runtime selectors, thresholds, market status, display, "
            "or validation protocol do not match the installed runtime"
        )
    package_version = _require_package_version(
        value["software"]["package_version"], "forward policy package_version"
    )
    current_version = _require_package_version(
        SOCCER_PREDICT_VERSION, "soccer_predict.__version__"
    )
    if package_version != current_version:
        raise ForwardPolicyError(
            "forward policy package_version does not match soccer_predict.__version__"
        )

    root = Path(repo_root).resolve()
    protected = value["code"]["protected_files"]
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
        block = value[section]
        artifact_path = Path(str(block.get(path_field) or ""))
        if not artifact_path.is_absolute():
            artifact_path = root / artifact_path
        if not artifact_path.is_file() or _hash_file(artifact_path) != block.get(
            hash_field
        ):
            raise ForwardPolicyError(
                f"frozen {section} artifact is missing or changed: {artifact_path}"
            )
    if _policy_uses_role_aware_lineage(value):
        if (
            value["artifact_lineage"].get("schema_version")
            != artifact_lineage.SCHEMA_VERSION
        ):
            raise ForwardPolicyError(
                "historical artifact lineage is read-only and cannot start or serve an active cohort"
            )
        try:
            artifact_lineage.verify_files(value["artifact_lineage"], repo_root=root)
        except artifact_lineage.ArtifactLineageError as exc:
            raise ForwardPolicyError(
                "frozen role-aware artifact lineage is missing or changed"
            ) from exc
        scope_block = value["cohort_scope"]
        scope_path = Path(str(scope_block.get("scope_path") or ""))
        if not scope_path.is_absolute():
            scope_path = root / scope_path
        if not scope_path.is_file() or _hash_file(scope_path) != scope_block.get(
            "file_sha256"
        ):
            raise ForwardPolicyError(
                "frozen cohort scope artifact is missing or changed"
            )
        try:
            disk_scope = cohort_scope.load_scope(scope_path)
        except cohort_scope.CohortScopeError as exc:
            raise ForwardPolicyError("frozen cohort scope artifact is invalid") from exc
        if disk_scope != scope_block.get("scope_snapshot"):
            raise ForwardPolicyError(
                "frozen cohort scope file does not reproduce its policy snapshot"
            )
    return value


def _reproduce_provenance_binding(
    policy_manifest: Mapping[str, Any], *, cohort_id: str
) -> dict[str, Any]:
    policy = validate_policy_manifest(policy_manifest)
    if policy.get("schema_version") not in PROVENANCE_POLICY_SCHEMA_VERSIONS:
        raise ForwardPolicyError(
            "forward-policy/1.0.0 cannot create provenance-complete bindings"
        )
    clean_cohort_id = _require_cohort_id(cohort_id, "provenance binding cohort_id")
    runtime = policy["policy"]
    current_policy = policy["schema_version"] == POLICY_SCHEMA_VERSION
    _, renderer_policy_files = _policy_contract_for_package(
        policy["software"]["package_version"]
    )
    binding: dict[str, Any] = {
        "schema_version": (
            PROVENANCE_SCHEMA_VERSION
            if current_policy
            else PREVIOUS_PROVENANCE_SCHEMA_VERSION
        ),
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
                    for path in renderer_policy_files
                },
            }
        ),
        "cohort_id": clean_cohort_id,
    }
    if current_policy:
        cohort_kind = _require_cohort_kind(
            policy["confirmation_contract"].get("cohort_kind"),
            "provenance cohort_kind",
        )
        binding.update(
            {
                "cohort_kind": cohort_kind,
                "assurance_scope": (
                    LOCAL_ASSURANCE_SCOPE
                    if cohort_kind == LOCAL_INTEGRITY_SHADOW_KIND
                    else PROMOTABLE_ASSURANCE_SCOPE
                ),
                "promotion_evidence_eligible": False,
            }
        )
    if _policy_uses_role_aware_lineage(policy):
        binding.update(
            {
                "artifact_lineage_hash": _require_sha256(
                    policy["artifact_lineage"]["lineage_hash"],
                    "provenance artifact_lineage_hash",
                ),
                "cohort_scope_hash": _require_sha256(
                    policy["cohort_scope"]["scope_hash"],
                    "provenance cohort_scope_hash",
                ),
            }
        )
    binding["provenance_hash"] = _hash_json(binding)
    return binding


def build_provenance_binding(
    policy_manifest: Mapping[str, Any], *, cohort_id: str
) -> dict[str, Any]:
    policy = validate_policy_manifest(policy_manifest)
    if policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ForwardPolicyError(
            "historical forward policies are read-only and cannot create a new "
            "provenance binding"
        )
    return _reproduce_provenance_binding(policy, cohort_id=cohort_id)


def validate_provenance_binding(
    binding: Any,
    *,
    policy_manifest: Mapping[str, Any],
    cohort_id: str,
) -> dict[str, Any]:
    if not isinstance(binding, Mapping):
        raise ForwardPolicyError("forward provenance binding must be an object")
    value = deepcopy(dict(binding))
    policy = validate_policy_manifest(policy_manifest)
    current_policy = policy["schema_version"] == POLICY_SCHEMA_VERSION
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
    if current_policy:
        required.update(
            {"cohort_kind", "assurance_scope", "promotion_evidence_eligible"}
        )
    if _policy_uses_role_aware_lineage(policy):
        required.update({"artifact_lineage_hash", "cohort_scope_hash"})
    if set(value) != required:
        raise ForwardPolicyError("forward provenance binding fields are incomplete")
    expected_schema = (
        PROVENANCE_SCHEMA_VERSION
        if current_policy
        else PREVIOUS_PROVENANCE_SCHEMA_VERSION
    )
    if value.get("schema_version") != expected_schema:
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
        *(
            ("artifact_lineage_hash", "cohort_scope_hash")
            if _policy_uses_role_aware_lineage(policy)
            else ()
        ),
    ):
        _require_sha256(value.get(field), f"provenance {field}")
    _require_cohort_id(value.get("cohort_id"), "provenance cohort_id")
    expected = _reproduce_provenance_binding(policy, cohort_id=cohort_id)
    if value != expected:
        raise ForwardPolicyError(
            "forward provenance binding does not reproduce from the frozen policy/cohort"
        )
    return value


def policy_directory(base_dir: str | Path) -> Path:
    return Path(base_dir).resolve() / ".codex" / "soccer-predict" / "forward-policies"


def policy_manifest_path(base_dir: str | Path, policy_id: str) -> Path:
    clean_policy_id = _require_policy_id(policy_id, "forward policy_id")
    return _safe_child_path(
        policy_directory(base_dir), f"{clean_policy_id}.json", "forward policy path"
    )


def _require_canonical_policy_file(
    base_dir: str | Path,
    policy_file: str | Path,
    *,
    policy_id: str | None = None,
) -> Path:
    candidate = Path(policy_file).resolve()
    root = policy_directory(base_dir).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ForwardPolicyError(
            "forward policy file is outside the canonical policy directory"
        ) from exc
    if relative.parent != Path(".") or candidate.suffix != ".json":
        raise ForwardPolicyError(
            "forward policy file must be a direct canonical JSON child"
        )
    filename_policy_id = _require_policy_id(
        candidate.stem, "forward policy filename policy_id"
    )
    if policy_id is not None and filename_policy_id != _require_policy_id(policy_id):
        raise ForwardPolicyError(
            "forward policy filename does not match its content-addressed policy_id"
        )
    return candidate


def cohort_directory(base_dir: str | Path) -> Path:
    return Path(base_dir).resolve() / ".codex" / "soccer-predict" / "forward-cohorts"


def cohort_manifest_path(base_dir: str | Path, cohort_id: str) -> Path:
    clean_cohort_id = _require_cohort_id(cohort_id)
    return _safe_child_path(
        cohort_directory(base_dir),
        f"{clean_cohort_id}.json",
        "live-forward cohort manifest path",
    )


def cohort_closure_path(base_dir: str | Path, cohort_id: str) -> Path:
    clean_cohort_id = _require_cohort_id(cohort_id)
    return _safe_child_path(
        cohort_directory(base_dir),
        f"{clean_cohort_id}-closure.json",
        "live-forward cohort closure path",
    )


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
    corner_dataset_manifest: str | Path,
    corner_model_registry: str | Path,
    cohort_scope_file: str | Path,
    expected_final_merge_commit: str,
    cohort_kind: str,
) -> tuple[Path, dict[str, Any]]:
    root = Path(repo_root).resolve()
    frozen_cohort_kind = _require_available_cohort_kind(
        cohort_kind, "freeze cohort_kind"
    )
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
    manifest = validate_active_runtime_policy_manifest(
        build_policy_manifest(
            repo_root=root,
            dataset_manifest=dataset_manifest,
            model_registry=model_registry,
            corner_dataset_manifest=corner_dataset_manifest,
            corner_model_registry=corner_model_registry,
            cohort_scope_file=cohort_scope_file,
            expected_final_merge_commit=expected_commit,
            cohort_kind=frozen_cohort_kind,
            code_commit=head_commit,
        ),
        repo_root=root,
    )
    path = policy_manifest_path(base_dir, str(manifest["policy_id"]))
    if path.exists():
        existing = validate_active_runtime_policy_manifest(
            _read_json(path, "existing policy"), repo_root=root
        )
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
    cohort_kind: str,
    repo_root: str | Path,
    starts_at: str | datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    root = Path(repo_root).resolve()
    requested_cohort_kind = _require_cohort_kind(
        cohort_kind, "cohort start cohort_kind"
    )
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
    clean_id = _require_cohort_id(cohort_id)
    if cohort_scope.denominator_event_path(base_dir, clean_id).exists():
        raise ForwardPolicyError(
            "cohort denominator log already exists before cohort start"
        )
    policy_path = _require_canonical_policy_file(base_dir, policy_file)
    raw_policy = _read_json(policy_path, "policy file")
    historical_policy = validate_policy_manifest(raw_policy)
    policy_path = _require_canonical_policy_file(
        base_dir,
        policy_path,
        policy_id=str(historical_policy["policy_id"]),
    )
    if historical_policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise ForwardPolicyError(
            "only forward-policy/3.0.0 can start a new active cohort; "
            "v1/v2 policies are historical read-only"
        )
    frozen_cohort_kind = historical_policy["confirmation_contract"].get("cohort_kind")
    frozen_cohort_kind = _require_cohort_kind(
        frozen_cohort_kind, "forward policy cohort_kind"
    )
    if requested_cohort_kind != frozen_cohort_kind:
        raise ForwardPolicyError(
            "cohort start cohort_kind does not match the frozen policy"
        )
    _require_available_cohort_kind(requested_cohort_kind, "cohort start cohort_kind")
    policy = validate_active_runtime_policy_manifest(raw_policy, repo_root=root)
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
        "kind": requested_cohort_kind,
        "status": "active",
        "starts_at": started.isoformat(),
        "policy_file": str(policy_path),
        "policy_id": policy["policy_id"],
        "policy_hash": policy["policy_hash"],
        "retrospective_records_allowed": False,
        "closed_at": None,
    }
    if _policy_uses_role_aware_lineage(policy):
        cohort["scope_id"] = policy["cohort_scope"]["scope_id"]
        cohort["scope_hash"] = policy["cohort_scope"]["scope_hash"]
    cohort["cohort_hash"] = _hash_json(cohort)
    immutable_path = cohort_manifest_path(base_dir, clean_id)
    if immutable_path.exists():
        raise ForwardPolicyError("live-forward cohort_id has already been used")
    _atomic_json(immutable_path, cohort)
    _atomic_json(active_path, cohort)
    return active_path, cohort


def validate_record_manifest(
    manifest: Mapping[str, Any],
    *,
    cohort: Mapping[str, Any] | None = None,
    required_schema_version: str | None = None,
    required_denominator_schema: str | None = None,
) -> dict[str, Any]:
    """Validate the complete immutable record index sealed at cohort closure."""

    if not isinstance(manifest, Mapping):
        raise ForwardPolicyError("live-forward record manifest must be an object")
    value = deepcopy(dict(manifest))
    required = {
        "schema_version",
        "artifact_type",
        "cohort_id",
        "cohort_hash",
        "policy_id",
        "policy_hash",
        "record_count",
        "records",
        "manifest_hash",
    }
    has_denominator = "denominator" in value or "denominator_hash" in value
    if required_denominator_schema is not None and not has_denominator:
        raise ForwardPolicyError(
            "live-forward record manifest is missing its frozen denominator contract"
        )
    if has_denominator:
        required.update({"denominator", "denominator_hash"})
    schema_version = value.get("schema_version")
    if schema_version not in {
        PREVIOUS_RECORD_MANIFEST_SCHEMA_VERSION,
        PREVIOUS_FULL_RECORD_MANIFEST_SCHEMA_VERSION,
        PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
        RECORD_MANIFEST_SCHEMA_VERSION,
    }:
        raise ForwardPolicyError(
            "unsupported live-forward record manifest schema_version"
        )
    if (
        required_schema_version is not None
        and schema_version != required_schema_version
    ):
        raise ForwardPolicyError(
            "live-forward record manifest schema_version does not match the frozen release contract"
        )
    if schema_version == RECORD_MANIFEST_SCHEMA_VERSION:
        required.add("max_record_archived_at")
    if set(value) != required:
        raise ForwardPolicyError("live-forward record manifest fields are incomplete")
    supplied_hash = value.pop("manifest_hash", None)
    if supplied_hash != _hash_json(value):
        raise ForwardPolicyError("live-forward record manifest hash is invalid")
    if value.get("artifact_type") != "soccer_untouched_live_forward_record_manifest":
        raise ForwardPolicyError(
            "live-forward record manifest artifact_type is invalid"
        )
    _require_cohort_id(value.get("cohort_id"), "live-forward record manifest cohort_id")
    _require_sha256(
        value.get("cohort_hash"), "live-forward record manifest cohort_hash"
    )
    _require_policy_id(value.get("policy_id"), "live-forward record manifest policy_id")
    _require_sha256(
        value.get("policy_hash"), "live-forward record manifest policy_hash"
    )

    raw_records = value.get("records")
    if not isinstance(raw_records, list):
        raise ForwardPolicyError("live-forward record manifest records must be a list")
    normalized_records: list[dict[str, Any]] = []
    fixture_ids: list[str] = []
    entry_fields = {
        "fixture_id",
        "archive_version_hash",
        "record_commitment_hash",
        "record_binding_hash",
        "prematch_ledger_hash",
    }
    if has_denominator:
        entry_fields.add("request_event_hash")
    if schema_version in {
        PREVIOUS_FULL_RECORD_MANIFEST_SCHEMA_VERSION,
        PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
        RECORD_MANIFEST_SCHEMA_VERSION,
    }:
        entry_fields.update(
            {
                "request_fixture_id",
                "fixture",
                "fixture_event_hash",
                "execution_receipt_hashes",
            }
        )
        if schema_version in {
            PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
            RECORD_MANIFEST_SCHEMA_VERSION,
        }:
            entry_fields.add("fixture_event_at")
        if schema_version == RECORD_MANIFEST_SCHEMA_VERSION:
            entry_fields.add("record_archived_at")
    for index, raw_entry in enumerate(raw_records):
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != entry_fields:
            raise ForwardPolicyError(
                f"live-forward record manifest records[{index}] fields are incomplete"
            )
        entry = deepcopy(dict(raw_entry))
        fixture_id = entry["fixture_id"]
        if not fixture_id:
            raise ForwardPolicyError(
                f"live-forward record manifest records[{index}].fixture_id is missing"
            )
        hash_fields = {
            "archive_version_hash",
            "record_commitment_hash",
            "record_binding_hash",
            "prematch_ledger_hash",
        }
        if has_denominator:
            hash_fields.add("request_event_hash")
        if schema_version in {
            PREVIOUS_FULL_RECORD_MANIFEST_SCHEMA_VERSION,
            PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
            RECORD_MANIFEST_SCHEMA_VERSION,
        }:
            hash_fields.add("fixture_event_hash")
        for field in hash_fields:
            _require_sha256(
                entry[field], f"live-forward record manifest records[{index}].{field}"
            )
        if schema_version in {
            PREVIOUS_FULL_RECORD_MANIFEST_SCHEMA_VERSION,
            PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
            RECORD_MANIFEST_SCHEMA_VERSION,
        }:
            if not str(entry.get("request_fixture_id") or ""):
                raise ForwardPolicyError(
                    f"live-forward record manifest records[{index}].request_fixture_id is missing"
                )
            fixture = entry.get("fixture")
            if not isinstance(fixture, Mapping) or set(fixture) != {
                "fixture_id",
                "competition_key",
                "home_team",
                "away_team",
                "kickoff",
            }:
                raise ForwardPolicyError(
                    f"live-forward record manifest records[{index}].fixture is invalid"
                )
            if str(fixture.get("fixture_id") or "") != fixture_id:
                raise ForwardPolicyError(
                    f"live-forward record manifest records[{index}].fixture_id does not bind fixture"
                )
            kickoff = _aware_datetime(
                fixture.get("kickoff"),
                f"live-forward record manifest records[{index}].fixture.kickoff",
            )
            competition_key = str(fixture.get("competition_key") or "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", competition_key):
                raise ForwardPolicyError(
                    f"live-forward record manifest records[{index}].fixture.competition_key is invalid"
                )
            home_team = " ".join(str(fixture.get("home_team") or "").split())
            away_team = " ".join(str(fixture.get("away_team") or "").split())
            if not home_team or not away_team:
                raise ForwardPolicyError(
                    f"live-forward record manifest records[{index}].fixture teams are missing"
                )
            entry["fixture"] = {
                "fixture_id": fixture_id,
                "competition_key": competition_key,
                "home_team": home_team,
                "away_team": away_team,
                "kickoff": kickoff.isoformat(),
            }
            raw_receipts = entry.get("execution_receipt_hashes")
            if not isinstance(raw_receipts, list):
                raise ForwardPolicyError(
                    f"live-forward record manifest records[{index}].execution_receipt_hashes is invalid"
                )
            receipt_hashes = [
                _require_sha256(
                    item,
                    f"live-forward record manifest records[{index}].execution_receipt_hashes",
                )
                for item in raw_receipts
            ]
            if receipt_hashes != sorted(set(receipt_hashes)):
                raise ForwardPolicyError(
                    "live-forward execution receipt identities must be unique and sorted"
                )
            entry["request_fixture_id"] = str(entry["request_fixture_id"])
            entry["execution_receipt_hashes"] = receipt_hashes
            if schema_version in {
                PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
                RECORD_MANIFEST_SCHEMA_VERSION,
            }:
                entry["fixture_event_at"] = _aware_datetime(
                    entry.get("fixture_event_at"),
                    f"live-forward record manifest records[{index}].fixture_event_at",
                ).isoformat()
            if schema_version == RECORD_MANIFEST_SCHEMA_VERSION:
                record_archived_at = _aware_datetime(
                    entry.get("record_archived_at"),
                    f"live-forward record manifest records[{index}].record_archived_at",
                )
                if record_archived_at <= _aware_datetime(
                    entry["fixture_event_at"],
                    f"live-forward record manifest records[{index}].fixture_event_at",
                ):
                    raise ForwardPolicyError(
                        "live-forward record manifest archive must follow its latest fixture event"
                    )
                if record_archived_at >= kickoff:
                    raise ForwardPolicyError(
                        "live-forward record manifest archive must precede kickoff"
                    )
                entry["record_archived_at"] = record_archived_at.isoformat()
        fixture_ids.append(fixture_id)
        normalized_records.append(entry)
    if fixture_ids != sorted(fixture_ids) or len(set(fixture_ids)) != len(fixture_ids):
        raise ForwardPolicyError(
            "live-forward record manifest records must be unique and canonically ordered "
            "by fixture_id"
        )
    record_count = value.get("record_count")
    if (
        isinstance(record_count, bool)
        or not isinstance(record_count, int)
        or record_count != len(normalized_records)
    ):
        raise ForwardPolicyError("live-forward record manifest record_count is invalid")
    value["records"] = normalized_records
    if schema_version == RECORD_MANIFEST_SCHEMA_VERSION:
        expected_max_archived_at = (
            max(
                _aware_datetime(
                    entry["record_archived_at"],
                    "live-forward record manifest record_archived_at",
                )
                for entry in normalized_records
            )
            if normalized_records
            else None
        )
        supplied_max_archived_at = value.get("max_record_archived_at")
        if expected_max_archived_at is None:
            if supplied_max_archived_at is not None:
                raise ForwardPolicyError(
                    "live-forward record manifest max_record_archived_at is invalid"
                )
        else:
            supplied_max = _aware_datetime(
                supplied_max_archived_at,
                "live-forward record manifest max_record_archived_at",
            )
            if supplied_max != expected_max_archived_at:
                raise ForwardPolicyError(
                    "live-forward record manifest max_record_archived_at is invalid"
                )
            value["max_record_archived_at"] = supplied_max.isoformat()
    if has_denominator:
        denominator = value.get("denominator")
        if not isinstance(denominator, Mapping):
            raise ForwardPolicyError(
                "live-forward record manifest denominator is invalid"
            )
        denominator_hash = _require_sha256(
            value.get("denominator_hash"),
            "live-forward record manifest denominator_hash",
        )
        if denominator.get("denominator_hash") != denominator_hash:
            raise ForwardPolicyError(
                "live-forward record manifest denominator hash is invalid"
            )
        if denominator.get("complete") is not True:
            raise ForwardPolicyError(
                "live-forward record manifest denominator is incomplete"
            )
        if (
            required_denominator_schema is not None
            and denominator.get("schema_version") != required_denominator_schema
        ):
            raise ForwardPolicyError(
                "live-forward record manifest denominator schema_version does not match the frozen release contract"
            )
        denominator_entries = denominator.get("entries")
        if not isinstance(denominator_entries, list):
            raise ForwardPolicyError(
                "live-forward record manifest denominator entries are missing"
            )
        recorded_ids = sorted(
            str(item.get("fixture_id") or "")
            for item in denominator_entries
            if isinstance(item, Mapping) and item.get("disposition") == "recorded"
        )
        if recorded_ids != fixture_ids:
            raise ForwardPolicyError(
                "record manifest does not exactly match its recorded denominator entries"
            )
        recorded_request_hashes = {
            str(item.get("fixture_id") or ""): _require_sha256(
                item.get("request_event_hash"),
                "live-forward denominator recorded request_event_hash",
            )
            for item in denominator_entries
            if isinstance(item, Mapping) and item.get("disposition") == "recorded"
        }
        if any(
            entry.get("request_event_hash")
            != recorded_request_hashes.get(entry["fixture_id"])
            for entry in normalized_records
        ):
            raise ForwardPolicyError(
                "record manifest request bindings do not match the denominator event log"
            )
        if schema_version in {
            PREVIOUS_FULL_RECORD_MANIFEST_SCHEMA_VERSION,
            PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
            RECORD_MANIFEST_SCHEMA_VERSION,
        }:
            recorded_entries = {
                str(item.get("fixture_id") or ""): item
                for item in denominator_entries
                if isinstance(item, Mapping) and item.get("disposition") == "recorded"
            }
            for entry in normalized_records:
                denominator_entry = recorded_entries[entry["fixture_id"]]
                if (
                    entry["request_fixture_id"]
                    != str(denominator_entry.get("request_fixture_id") or "")
                    or entry["fixture"] != denominator_entry.get("fixture")
                    or entry["fixture_event_hash"]
                    != denominator_entry.get("fixture_event_hash")
                    or (
                        schema_version
                        in {
                            PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
                            RECORD_MANIFEST_SCHEMA_VERSION,
                        }
                        and entry["fixture_event_at"]
                        != denominator_entry.get("fixture_event_at")
                    )
                ):
                    raise ForwardPolicyError(
                        "record manifest fixture binding does not match the denominator event log"
                    )
            receipt_hashes = [
                receipt_hash
                for entry in normalized_records
                for receipt_hash in entry["execution_receipt_hashes"]
            ]
            if len(receipt_hashes) != len(set(receipt_hashes)):
                raise ForwardPolicyError(
                    "cohort record manifest reuses one firm/account/receipt identity"
                )

    if cohort is not None:
        frozen = validate_cohort(cohort)
        if any(
            value.get(field) != frozen.get(field)
            for field in ("cohort_id", "cohort_hash", "policy_id", "policy_hash")
        ):
            raise ForwardPolicyError(
                "live-forward record manifest does not bind the immutable cohort"
            )
        if "scope_hash" in frozen:
            if not has_denominator:
                raise ForwardPolicyError(
                    "scoped live-forward cohort requires a complete denominator"
                )
            if (
                denominator.get("scope_id") != frozen.get("scope_id")
                or denominator.get("scope_hash") != frozen.get("scope_hash")
                or denominator.get("cohort_id") != frozen.get("cohort_id")
            ):
                raise ForwardPolicyError(
                    "record manifest denominator does not bind the cohort scope"
                )
    value["manifest_hash"] = supplied_hash
    return value


def close_cohort(
    *,
    base_dir: str | Path,
    record_manifest: Mapping[str, Any],
    closed_at: str | datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Close one cohort while excluding every concurrent event-log writer."""

    cohort_id = str(record_manifest.get("cohort_id") or "")
    try:
        cohort_scope.denominator_event_path(base_dir, cohort_id)
    except cohort_scope.CohortScopeError as exc:
        raise ForwardPolicyError("record manifest cohort_id is invalid") from exc
    with cohort_scope.event_log_transaction(base_dir, cohort_id):
        return _close_cohort_under_event_lock(
            base_dir=base_dir,
            record_manifest=record_manifest,
            closed_at=closed_at,
            expected_cohort_id=cohort_id,
        )


def _close_cohort_under_event_lock(
    *,
    base_dir: str | Path,
    record_manifest: Mapping[str, Any],
    closed_at: str | datetime | None,
    expected_cohort_id: str,
) -> tuple[Path, dict[str, Any]]:
    active_path = active_cohort_path(base_dir)
    pointer = validate_cohort(_read_json(active_path, "active cohort"))
    if pointer.get("cohort_id") != expected_cohort_id:
        raise ForwardPolicyError(
            "active live-forward cohort does not match the record manifest"
        )
    already_closed = pointer["status"] == "closed"
    if already_closed:
        immutable_path = cohort_manifest_path(base_dir, str(pointer["cohort_id"]))
        cohort = validate_cohort(
            _read_json(immutable_path, "immutable live-forward cohort")
        )
        expected_pointer = deepcopy(cohort)
        expected_pointer.pop("cohort_hash", None)
        expected_pointer["status"] = "closed"
        expected_pointer["closed_at"] = pointer["closed_at"]
        expected_pointer["cohort_hash"] = _hash_json(expected_pointer)
        if pointer != expected_pointer:
            raise ForwardPolicyError(
                "closed live-forward cohort pointer does not reproduce"
            )
    else:
        cohort = pointer
    policy_path = _require_canonical_policy_file(
        base_dir,
        str(cohort.get("policy_file") or ""),
        policy_id=str(cohort.get("policy_id") or ""),
    )
    policy = validate_policy_manifest(_read_json(policy_path, "cohort policy"))
    if policy.get("policy_id") != cohort.get("policy_id") or policy.get(
        "policy_hash"
    ) != cohort.get("policy_hash"):
        raise ForwardPolicyError(
            "live-forward cohort policy file does not match its immutable binding"
        )
    software = policy.get("software")
    package_version = (
        software.get("package_version") if isinstance(software, Mapping) else None
    )
    schema_contract = closure_schema_contract(package_version)
    required_closure_schema = schema_contract["closure"]
    required_manifest_schema = schema_contract["record_manifest"]
    required_denominator_schema = schema_contract["denominator"]
    required_event_schema = schema_contract["event"]
    manifest = validate_record_manifest(
        record_manifest,
        cohort=cohort,
        required_schema_version=required_manifest_schema,
        required_denominator_schema=required_denominator_schema,
    )
    events: list[dict[str, Any]] = []
    if "scope_hash" in cohort:
        event_binding = {
            "cohort_hash": cohort["cohort_hash"],
            "policy_id": cohort["policy_id"],
            "policy_hash": cohort["policy_hash"],
            "starts_at": cohort["starts_at"],
        }
        try:
            events = cohort_scope.load_events(
                base_dir,
                str(cohort["cohort_id"]),
                scope=policy["cohort_scope"]["scope_snapshot"],
                cohort_binding=event_binding,
                required_schema_version=required_event_schema,
            )
            reproduced_denominator = cohort_scope.build_denominator(
                scope=policy["cohort_scope"]["scope_snapshot"],
                cohort_id=str(cohort["cohort_id"]),
                events=events,
                record_manifest=manifest,
                cohort_binding=event_binding,
                schema_version=required_denominator_schema
                or cohort_scope.DENOMINATOR_SCHEMA_VERSION,
                required_event_schema_version=required_event_schema,
            )
            cohort_scope.validate_denominator(
                manifest.get("denominator"),
                scope=policy["cohort_scope"]["scope_snapshot"],
                cohort_id=str(cohort["cohort_id"]),
                allowed_schema_versions=(
                    (required_denominator_schema,)
                    if required_denominator_schema
                    else (
                        cohort_scope.PREVIOUS_DENOMINATOR_SCHEMA_VERSION,
                        cohort_scope.DENOMINATOR_SCHEMA_VERSION,
                    )
                ),
            )
        except cohort_scope.CohortScopeError as exc:
            raise ForwardPolicyError(
                "live-forward cohort denominator cannot be reproduced at closure"
            ) from exc
        if manifest.get("denominator") != reproduced_denominator:
            raise ForwardPolicyError(
                "live-forward cohort denominator does not reproduce from its event log"
            )
    closure_path = cohort_closure_path(base_dir, str(cohort["cohort_id"]))
    existing: dict[str, Any] | None = None
    if closure_path.exists():
        existing = validate_closure(
            _read_json(closure_path, "existing live-forward cohort closure"),
            cohort=cohort,
            require_record_manifest=True,
            required_closure_schema=required_closure_schema,
            required_record_manifest_schema=required_manifest_schema,
            required_denominator_schema=required_denominator_schema,
        )
        if already_closed and pointer["closed_at"] != existing["closed_at"]:
            raise ForwardPolicyError(
                "closed live-forward cohort pointer does not match immutable closure"
            )
    elif already_closed:
        raise ForwardPolicyError(
            "closed live-forward cohort pointer has no immutable closure"
        )
    observed = _aware_datetime(
        (
            str(existing["observed_at"])
            if existing is not None and "observed_at" in existing
            else _now_iso()
        ),
        "observed_at",
    )
    effective_closed_at: str | datetime = (
        str(existing["closed_at"])
        if closed_at is None and existing is not None
        else closed_at or observed
    )
    closed = _aware_datetime(effective_closed_at, "closed_at")
    started = _aware_datetime(str(cohort["starts_at"]), "cohort.starts_at")
    if closed < started:
        raise ForwardPolicyError("live-forward cohort cannot close before it started")
    if closed > observed:
        raise ForwardPolicyError("live-forward cohort cannot close in the future")
    last_event_at = (
        max(
            _aware_datetime(event["occurred_at"], "cohort event occurred_at")
            for event in events
        )
        if events
        else None
    )
    max_record_archived_at = (
        _aware_datetime(
            manifest["max_record_archived_at"], "record manifest max_record_archived_at"
        )
        if manifest.get("max_record_archived_at") is not None
        else None
    )
    if last_event_at is not None and closed < last_event_at:
        raise ForwardPolicyError("live-forward cohort closure predates its last event")
    if max_record_archived_at is not None and closed < max_record_archived_at:
        raise ForwardPolicyError("live-forward cohort closure predates its last record")
    closure: dict[str, Any] = {
        "schema_version": required_closure_schema or CLOSURE_SCHEMA_VERSION,
        "artifact_type": "soccer_untouched_live_forward_cohort_closure",
        "cohort_id": cohort["cohort_id"],
        "cohort_hash": cohort["cohort_hash"],
        "policy_id": cohort["policy_id"],
        "policy_hash": cohort["policy_hash"],
        "starts_at": cohort["starts_at"],
        "closed_at": closed.isoformat(),
        "reason": "explicit_policy_boundary",
        "record_manifest_hash": manifest["manifest_hash"],
        "record_manifest": manifest,
    }
    if closure["schema_version"] == CLOSURE_SCHEMA_VERSION:
        closure["observed_at"] = observed.isoformat()
    closure["closure_hash"] = _hash_json(closure)
    if existing is not None:
        if existing != closure:
            raise ForwardPolicyError(
                "live-forward cohort closure already exists with different content"
            )
        closure = existing
    else:
        _atomic_json(closure_path, closure)
    if already_closed:
        return closure_path, closure
    closed_pointer = deepcopy(cohort)
    closed_pointer.pop("cohort_hash", None)
    closed_pointer["status"] = "closed"
    closed_pointer["closed_at"] = closure["closed_at"]
    closed_pointer["cohort_hash"] = _hash_json(closed_pointer)
    _atomic_json(active_path, closed_pointer)
    return closure_path, closure


def validate_cohort(cohort: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(cohort))
    schema_version = value.get("schema_version")
    if schema_version not in {LEGACY_COHORT_SCHEMA_VERSION, COHORT_SCHEMA_VERSION}:
        raise ForwardPolicyError("unsupported live-forward cohort schema_version")
    supplied_hash = value.pop("cohort_hash", None)
    if supplied_hash != _hash_json(value):
        raise ForwardPolicyError("live-forward cohort hash is invalid")
    value["cohort_hash"] = supplied_hash
    started = _aware_datetime(str(value.get("starts_at") or ""), "cohort.starts_at")
    if value.get("artifact_type") != "soccer_untouched_live_forward_cohort":
        raise ForwardPolicyError("live-forward cohort artifact_type is invalid")
    _require_cohort_id(value.get("cohort_id"), "live-forward cohort_id")
    if schema_version == COHORT_SCHEMA_VERSION:
        _require_cohort_kind(value.get("kind"), "live-forward cohort kind")
        if ("scope_id" in value) != ("scope_hash" in value):
            raise ForwardPolicyError("live-forward cohort scope identity is incomplete")
        if "scope_id" in value:
            _require_cohort_id(value.get("scope_id"), "live-forward scope_id")
            _require_sha256(value.get("scope_hash"), "live-forward scope_hash")
    elif "kind" in value:
        raise ForwardPolicyError(
            "legacy live-forward cohorts cannot carry a cohort kind"
        )
    policy_file = str(value.get("policy_file") or "")
    if not policy_file:
        raise ForwardPolicyError("live-forward cohort policy_file is missing")
    policy_id = _require_policy_id(
        value.get("policy_id"), "live-forward cohort policy_id"
    )
    if schema_version == COHORT_SCHEMA_VERSION:
        policy_path = Path(policy_file)
        if not policy_path.is_absolute() or policy_path.name != f"{policy_id}.json":
            raise ForwardPolicyError(
                "current live-forward cohort policy_file is not canonical"
            )
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
    closure: Mapping[str, Any],
    *,
    cohort: Mapping[str, Any] | None = None,
    require_record_manifest: bool = False,
    required_closure_schema: str | None = None,
    required_record_manifest_schema: str | None = None,
    required_denominator_schema: str | None = None,
) -> dict[str, Any]:
    value = deepcopy(dict(closure))
    schema_version = value.get("schema_version")
    if schema_version not in {
        LEGACY_CLOSURE_SCHEMA_VERSION,
        PREVIOUS_FULL_CLOSURE_SCHEMA_VERSION,
        CLOSURE_SCHEMA_VERSION,
    }:
        raise ForwardPolicyError(
            "unsupported live-forward cohort closure schema_version"
        )
    if (
        required_closure_schema is not None
        and schema_version != required_closure_schema
    ):
        raise ForwardPolicyError(
            "live-forward cohort closure schema_version does not match the frozen release contract"
        )
    if require_record_manifest and schema_version == LEGACY_CLOSURE_SCHEMA_VERSION:
        raise ForwardPolicyError(
            "formal forward evaluation requires a record-manifest-bound cohort closure"
        )
    supplied_hash = value.pop("closure_hash", None)
    if supplied_hash != _hash_json(value):
        raise ForwardPolicyError("live-forward cohort closure hash is invalid")
    required = {
        "schema_version",
        "artifact_type",
        "cohort_id",
        "cohort_hash",
        "policy_id",
        "policy_hash",
        "starts_at",
        "closed_at",
        "reason",
    }
    if schema_version in {PREVIOUS_FULL_CLOSURE_SCHEMA_VERSION, CLOSURE_SCHEMA_VERSION}:
        required.update({"record_manifest_hash", "record_manifest"})
    if schema_version == CLOSURE_SCHEMA_VERSION:
        required.add("observed_at")
    if set(value) != required:
        raise ForwardPolicyError("live-forward cohort closure fields are incomplete")
    value["closure_hash"] = supplied_hash
    if value.get("artifact_type") != "soccer_untouched_live_forward_cohort_closure":
        raise ForwardPolicyError("live-forward cohort closure artifact_type is invalid")
    _require_cohort_id(value.get("cohort_id"), "live-forward cohort closure cohort_id")
    _require_sha256(value.get("cohort_hash"), "live-forward closure cohort_hash")
    _require_sha256(value.get("policy_hash"), "live-forward closure policy_hash")
    _require_policy_id(value.get("policy_id"), "live-forward cohort closure policy_id")
    started = _aware_datetime(str(value.get("starts_at") or ""), "closure.starts_at")
    closed = _aware_datetime(str(value.get("closed_at") or ""), "closure.closed_at")
    if closed < started:
        raise ForwardPolicyError("live-forward cohort closure predates its start")
    if value.get("reason") != "explicit_policy_boundary":
        raise ForwardPolicyError("live-forward cohort closure reason is invalid")
    if schema_version == CLOSURE_SCHEMA_VERSION:
        observed = _aware_datetime(
            str(value.get("observed_at") or ""), "closure.observed_at"
        )
        if closed > observed:
            raise ForwardPolicyError("live-forward cohort closure exceeds observed_at")
        if observed > datetime.now(timezone.utc):
            raise ForwardPolicyError(
                "live-forward cohort closure observed_at is future-dated"
            )
        value["observed_at"] = observed.isoformat()
    if schema_version in {PREVIOUS_FULL_CLOSURE_SCHEMA_VERSION, CLOSURE_SCHEMA_VERSION}:
        manifest = validate_record_manifest(
            value.get("record_manifest") or {},
            cohort=cohort,
            required_schema_version=required_record_manifest_schema,
            required_denominator_schema=required_denominator_schema,
        )
        if value.get("record_manifest_hash") != manifest["manifest_hash"]:
            raise ForwardPolicyError(
                "live-forward cohort closure record manifest hash is invalid"
            )
        if any(
            value.get(field) != manifest.get(field)
            for field in ("cohort_id", "cohort_hash", "policy_id", "policy_hash")
        ):
            raise ForwardPolicyError(
                "live-forward cohort closure does not bind its record manifest"
            )
        value["record_manifest"] = manifest
        if schema_version == CLOSURE_SCHEMA_VERSION:
            denominator = manifest.get("denominator")
            if isinstance(denominator, Mapping):
                event_count = denominator.get("event_count")
                last_event_at = denominator.get("last_event_at")
                if event_count and last_event_at is None:
                    raise ForwardPolicyError(
                        "live-forward cohort denominator omits its last event time"
                    )
                if event_count == 0 and last_event_at is not None:
                    raise ForwardPolicyError(
                        "empty live-forward cohort denominator invents a last event time"
                    )
                if last_event_at is not None:
                    parsed_last_event = _aware_datetime(
                        last_event_at, "denominator last_event_at"
                    )
                    if parsed_last_event < started or closed < parsed_last_event:
                        raise ForwardPolicyError(
                            "live-forward cohort closure predates its last event"
                        )
            max_record_archived_at = manifest.get("max_record_archived_at")
            if max_record_archived_at is not None and closed < _aware_datetime(
                max_record_archived_at, "record manifest max_record_archived_at"
            ):
                raise ForwardPolicyError(
                    "live-forward cohort closure predates its last record"
                )
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
    fixture_id: str | None = None,
    competition_key: str | None = None,
    home_team: str | None = None,
    away_team: str | None = None,
    kickoff: str | datetime | None = None,
    observation_commitment_hash: str | None = None,
) -> dict[str, Any] | None:
    path = active_cohort_path(base_dir)
    if not path.exists():
        return None
    cohort = validate_cohort(_read_json(path, "active cohort"))
    if cohort["status"] != "active":
        return None
    closure_path = cohort_closure_path(base_dir, str(cohort["cohort_id"]))
    if closure_path.exists() or closure_path.is_symlink():
        raise ForwardPolicyError(
            "immutable closure exists while pointer remains active; "
            "repair the pointer before accepting new events or records"
        )
    if cohort.get("schema_version") != COHORT_SCHEMA_VERSION:
        raise ForwardPolicyError(
            "active cohort without an explicit kind is historical read-only"
        )
    active_cohort_kind = _require_available_cohort_kind(
        cohort.get("kind"), "active cohort kind"
    )
    policy_file = _require_canonical_policy_file(
        base_dir,
        str(cohort.get("policy_file") or ""),
        policy_id=str(cohort.get("policy_id") or ""),
    )
    policy = validate_active_runtime_policy_manifest(
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
    if policy["confirmation_contract"].get("cohort_kind") != active_cohort_kind:
        raise ForwardPolicyError("active cohort kind does not match its frozen policy")
    if _policy_uses_role_aware_lineage(policy) and (
        cohort.get("scope_id") != policy["cohort_scope"]["scope_id"]
        or cohort.get("scope_hash") != policy["cohort_scope"]["scope_hash"]
    ):
        raise ForwardPolicyError(
            "active cohort does not bind its frozen denominator scope"
        )
    archived = _aware_datetime(archived_at, "archived_at")
    start = _aware_datetime(str(cohort["starts_at"]), "cohort.starts_at")
    if archived < start:
        raise ForwardPolicyError(
            "record predates the active untouched live-forward cohort"
        )
    expected_fixture = None
    if fixture_id is not None:
        if None in {competition_key, home_team, away_team, kickoff}:
            raise ForwardPolicyError(
                "denominator fixture binding requires competition, teams, and kickoff"
            )
        kickoff_time = _aware_datetime(kickoff, "fixture.kickoff")
        if archived >= kickoff_time:
            raise ForwardPolicyError(
                "record archive must be strictly earlier than fixture kickoff"
            )
        expected_fixture = {
            "fixture_id": str(fixture_id),
            "competition_key": str(competition_key),
            "home_team": " ".join(str(home_team or "").split()),
            "away_team": " ".join(str(away_team or "").split()),
            "kickoff": kickoff_time.isoformat(),
        }
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
        "archived_at": archived.isoformat(),
        "cohort_kind": active_cohort_kind,
        "assurance_scope": LOCAL_ASSURANCE_SCOPE,
        "promotion_evidence_eligible": False,
        "provenance_binding": build_provenance_binding(
            policy, cohort_id=str(cohort["cohort_id"])
        ),
    }
    if _policy_uses_role_aware_lineage(policy):
        if observation_commitment_hash is not None and fixture_id is None:
            raise ForwardPolicyError(
                "committed role-aware binding requires a denominator fixture_id"
            )
        if fixture_id is not None:
            try:
                binding["cohort_request_binding"] = cohort_scope.request_binding(
                    base_dir=base_dir,
                    cohort_id=str(cohort["cohort_id"]),
                    scope=policy["cohort_scope"]["scope_snapshot"],
                    fixture_id=str(fixture_id),
                    expected_fixture=expected_fixture,
                    cohort_binding={
                        "cohort_hash": cohort["cohort_hash"],
                        "policy_id": policy["policy_id"],
                        "policy_hash": policy["policy_hash"],
                        "starts_at": cohort["starts_at"],
                    },
                )
            except cohort_scope.CohortScopeError as exc:
                raise ForwardPolicyError(
                    "fixture is not registered in the frozen cohort denominator"
                ) from exc
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
    if value.get("schema_version") not in {
        PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
        PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
    }:
        raise ForwardPolicyError(
            "historical forward policy bindings are read-only and cannot receive "
            "a new observation commitment"
        )
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
    value["schema_version"] = PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
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
        PREVIOUS_PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
        PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
        PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
        PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
    }:
        raise ForwardPolicyError("unsupported forward policy binding schema_version")
    supplied = value.pop("binding_hash", None)
    if supplied != _hash_json(value):
        raise ForwardPolicyError("forward policy binding hash is invalid")
    _require_cohort_id(value.get("cohort_id"), "forward policy binding cohort_id")
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
    previous_provenance = schema_version in {
        PREVIOUS_PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
        PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
    }
    current_provenance = schema_version in {
        PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
        PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
    }
    provenance_complete = previous_provenance or current_provenance
    if provenance_complete:
        expected_policy_schema = (
            POLICY_SCHEMA_VERSION
            if current_provenance
            else PREVIOUS_POLICY_SCHEMA_VERSION
        )
        if policy.get("schema_version") != expected_policy_schema:
            raise ForwardPolicyError(
                "forward policy binding schema does not match its policy schema"
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
    current_assurance_fields = {
        "cohort_kind",
        "assurance_scope",
        "promotion_evidence_eligible",
    }
    if current_provenance:
        if "untouched_confirmation_eligible" in value:
            raise ForwardPolicyError(
                "current record binding cannot carry the legacy untouched "
                "confirmation flag"
            )
        cohort_kind = _require_cohort_kind(
            value.get("cohort_kind"), "forward policy binding cohort_kind"
        )
        if cohort_kind != policy["confirmation_contract"].get("cohort_kind"):
            raise ForwardPolicyError(
                "record binding cohort_kind does not match its policy snapshot"
            )
        expected_scope = (
            LOCAL_ASSURANCE_SCOPE
            if cohort_kind == LOCAL_INTEGRITY_SHADOW_KIND
            else PROMOTABLE_ASSURANCE_SCOPE
        )
        if (
            value.get("assurance_scope") != expected_scope
            or value.get("promotion_evidence_eligible") is not False
        ):
            raise ForwardPolicyError(
                "record binding assurance and promotion scope are invalid"
            )
        provenance = value["provenance_binding"]
        if any(
            provenance.get(field) != value.get(field)
            for field in current_assurance_fields
        ):
            raise ForwardPolicyError(
                "record binding assurance fields do not match provenance"
            )
        role_aware_release = _policy_uses_role_aware_lineage(policy)
        request = value.get("cohort_request_binding")
        committed_current = (
            schema_version == PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
        )
        if role_aware_release and (committed_current or request is not None):
            if not isinstance(request, Mapping):
                raise ForwardPolicyError(
                    "record binding cohort request identity is incomplete"
                )
            request_schema = request.get("schema_version")
            fixture_event_at: datetime | None = None
            expected_request_fields = (
                {
                    "schema_version",
                    "scope_id",
                    "scope_hash",
                    "fixture_id",
                    "request_event_hash",
                    "requested_at",
                }
                if request_schema
                == cohort_scope.PREVIOUS_REQUEST_BINDING_SCHEMA_VERSION
                else {
                    "schema_version",
                    "scope_id",
                    "scope_hash",
                    "request_fixture_id",
                    "fixture",
                    "request_event_hash",
                    "fixture_event_hash",
                    "requested_at",
                }
            )
            if request_schema == cohort_scope.REQUEST_BINDING_SCHEMA_VERSION:
                expected_request_fields.add("fixture_event_at")
            if set(request) != expected_request_fields:
                raise ForwardPolicyError(
                    "record binding cohort request identity is incomplete"
                )
            if request_schema not in {
                cohort_scope.PREVIOUS_REQUEST_BINDING_SCHEMA_VERSION,
                cohort_scope.PREVIOUS_FULL_REQUEST_BINDING_SCHEMA_VERSION,
                cohort_scope.REQUEST_BINDING_SCHEMA_VERSION,
            }:
                raise ForwardPolicyError(
                    "record binding cohort request schema is invalid"
                )
            package_version = policy["software"]["package_version"]
            expected_request_schema = (
                cohort_scope.REQUEST_BINDING_SCHEMA_VERSION
                if _release_at_least(package_version, (3, 9, 0))
                else cohort_scope.PREVIOUS_FULL_REQUEST_BINDING_SCHEMA_VERSION
            )
            if (
                _release_at_least(package_version, (3, 8, 0))
                and request_schema != expected_request_schema
            ):
                raise ForwardPolicyError(
                    "current runtime record binding requires full fixture request identity"
                )
            if (
                request.get("scope_id") != policy["cohort_scope"]["scope_id"]
                or request.get("scope_hash") != policy["cohort_scope"]["scope_hash"]
            ):
                raise ForwardPolicyError(
                    "record binding cohort request does not bind the frozen scope"
                )
            _require_sha256(
                request.get("request_event_hash"),
                "record binding cohort request_event_hash",
            )
            if request_schema in {
                cohort_scope.PREVIOUS_FULL_REQUEST_BINDING_SCHEMA_VERSION,
                cohort_scope.REQUEST_BINDING_SCHEMA_VERSION,
            }:
                _require_cohort_id(
                    request.get("request_fixture_id"),
                    "record binding cohort request_fixture_id",
                )
                _require_sha256(
                    request.get("fixture_event_hash"),
                    "record binding cohort fixture_event_hash",
                )
                fixture = request.get("fixture")
                if not isinstance(fixture, Mapping) or set(fixture) != {
                    "fixture_id",
                    "competition_key",
                    "home_team",
                    "away_team",
                    "kickoff",
                }:
                    raise ForwardPolicyError(
                        "record binding cohort request fixture is incomplete"
                    )
                _require_cohort_id(
                    fixture.get("fixture_id"), "record binding request fixture_id"
                )
                _require_cohort_id(
                    fixture.get("competition_key"),
                    "record binding request competition_key",
                )
                if (
                    not str(fixture.get("home_team") or "").strip()
                    or not str(fixture.get("away_team") or "").strip()
                ):
                    raise ForwardPolicyError(
                        "record binding request fixture teams are missing"
                    )
                kickoff = _aware_datetime(
                    str(fixture.get("kickoff") or ""),
                    "record binding request kickoff",
                )
                if archived >= kickoff:
                    raise ForwardPolicyError(
                        "record binding archive is not strictly before kickoff"
                    )
                if request_schema == cohort_scope.REQUEST_BINDING_SCHEMA_VERSION:
                    fixture_event_at = _aware_datetime(
                        str(request.get("fixture_event_at") or ""),
                        "record binding fixture_event_at",
                    )
                    if fixture_event_at >= archived:
                        raise ForwardPolicyError(
                            "latest fixture transition was not strictly before archive"
                        )
            requested_at = _aware_datetime(
                str(request.get("requested_at") or ""),
                "record binding requested_at",
            )
            if requested_at >= archived:
                raise ForwardPolicyError(
                    "record binding cohort request was not strictly before archive"
                )
            if requested_at < started:
                raise ForwardPolicyError(
                    "record binding cohort request predates its untouched cohort"
                )
            if fixture_event_at is not None and fixture_event_at < requested_at:
                raise ForwardPolicyError(
                    "latest fixture transition predates the original fixture request"
                )
        elif request is not None:
            raise ForwardPolicyError(
                "pre-3.7 record bindings cannot carry cohort request evidence"
            )
    else:
        if any(field in value for field in current_assurance_fields):
            raise ForwardPolicyError(
                "historical record binding cannot carry current assurance fields"
            )
        if value.get("untouched_confirmation_eligible") is not True:
            raise ForwardPolicyError(
                "historical record binding is not untouched-confirmation eligible"
            )
    if schema_version in {
        COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
        PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
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
    freeze.add_argument("--corner-dataset-manifest", required=True)
    freeze.add_argument("--corner-model-registry", required=True)
    freeze.add_argument("--cohort-scope-file", required=True)
    freeze.add_argument("--cohort-kind", required=True, choices=COHORT_KINDS)
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
    start.add_argument("--cohort-kind", required=True, choices=COHORT_KINDS)
    start.add_argument("--starts-at")
    close = subparsers.add_parser(
        "close", help="close the active cohort at an explicit policy boundary"
    )
    close.add_argument("--closed-at")
    close.add_argument(
        "--record-manifest-file",
        required=True,
        help="canonical complete record manifest exported from memory-store history",
    )
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
                corner_dataset_manifest=arguments.corner_dataset_manifest,
                corner_model_registry=arguments.corner_model_registry,
                cohort_scope_file=arguments.cohort_scope_file,
                expected_final_merge_commit=arguments.expected_final_merge_commit,
                cohort_kind=arguments.cohort_kind,
            )
        elif arguments.command == "start":
            path, artifact = start_cohort(
                base_dir=arguments.base_dir,
                repo_root=arguments.repo_root,
                policy_file=arguments.policy_file,
                cohort_id=arguments.cohort_id,
                cohort_kind=arguments.cohort_kind,
                starts_at=arguments.starts_at,
            )
        elif arguments.command == "close":
            path, artifact = close_cohort(
                base_dir=arguments.base_dir,
                record_manifest=_read_json(
                    Path(arguments.record_manifest_file).resolve(), "record manifest"
                ),
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

#!/usr/bin/env python3
"""Run reproducible, fixed-season HT/FT holdout evaluations.

The evaluator deliberately freezes three league-scoped experiments:

* seasons through 2023 train the 2024 validation cohort;
* seasons through 2024 train the fixed 2025 cohort; and
* seasons through 2025 train the 2026 shadow cohort.

The 2025 and 2026 samples were inspected while the end-to-end Top-2 selector
was being chosen.  They therefore remain valid model-component evidence but
are never described here as an untouched confirmation of the final selector.
Any non-promoted fit or bootstrap setting must also opt in as an experiment;
its split roles are changed so a quick parameter sweep cannot masquerade as
promotion evidence.

An optional opening-market experiment is research-only.  The source bundle
does not contain collection timestamps, so this module never calls the formal
timestamped anchor interface in :mod:`htft_model`.  It averages per-bookmaker
de-vigged opening 1X2 probabilities and applies that full-time marginal to the
model prediction's raw joint seed with IPF inside this evaluator.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import random
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # Repository-root imports.
    from scripts import history_importer, htft_model
except ImportError:  # Direct invocation as scripts/htft_holdout_evaluator.py.
    script_directory = str(Path(__file__).resolve().parent)
    if script_directory not in sys.path:
        sys.path.insert(0, script_directory)
    import history_importer  # type: ignore[no-redef]
    import htft_model  # type: ignore[no-redef]


ARTIFACT_TYPE = "soccer_htft_fixed_season_evaluation"
SCHEMA_VERSION = "1.5.0"
EVALUATOR_VERSION = "htft-fixed-season-holdout/1.6.0"
RESEARCH_MARKET_POLICY = "research_only_untimestamped_opening_snapshot"
OPTIONAL_CONTEXT_COLUMNS = ("season_status", "format_version", "phase_group")
LEGACY_SEASON_STATUS = "unlabeled_legacy"
LEGACY_FORMAT_VERSION = "competition_regime_legacy"
LEGACY_PHASE_GROUP = "unspecified"
PARTIAL_SEASON_STATUS_PREFIX = "partial_as_of_"
COMPETITION_REGIME_POLICY = {
    "version": "competition-specific-production-v3",
    "source_column": "competition_regime",
    "default_allowed_regimes": ["regular"],
    "allowed_regimes_by_league": {
        "brazil_cup": ["national_knockout_cup"],
        "england_league_cup": ["national_knockout_cup"],
        "uefa_nations_league": ["national_team_league_and_knockout"],
    },
    "excluded_regimes_usage": "counted_for_drift_audit_only_not_fit_or_scored",
    "special_regimes_are_not_merged_into_regular_strengths": True,
}


def _formal_competition_regimes(league_key: str) -> frozenset[str]:
    overrides = COMPETITION_REGIME_POLICY["allowed_regimes_by_league"]
    values = overrides.get(
        league_key, COMPETITION_REGIME_POLICY["default_allowed_regimes"]
    )
    return frozenset(values)


MODEL_PAIR_MASS_THRESHOLD = 0.46
MARKET_PAIR_MASS_THRESHOLD = 0.50
EPSILON = 1e-15
BOOTSTRAP_SEED = 20260802
DEFAULT_BOOTSTRAP_REPETITIONS = 2000
SPLITS = (
    {
        "split_id": "validation_2024",
        "role": "development_validation",
        "training_season_max": 2023,
        "test_season": 2024,
    },
    {
        "split_id": "fixed_holdout_2025",
        "role": "model_fit_holdout_selector_development",
        "training_season_max": 2024,
        "test_season": 2025,
    },
    {
        "split_id": "shadow_2026",
        "role": "shadow_monitoring_seen_during_development",
        "training_season_max": 2025,
        "test_season": 2026,
    },
)
EXPERIMENTAL_SPLIT_ROLES = {
    "validation_2024": "configuration_experiment_validation",
    "fixed_holdout_2025": "reused_holdout_configuration_experiment",
    "shadow_2026": "reused_shadow_configuration_experiment",
}
PROMOTED_FIT_CONFIG = {
    "half_time_half_life_days": 730.0,
    "second_half_half_life_days": 365.0,
    "full_time_half_life_days": 365.0,
    "iterations": 1200,
    "learning_rate": 0.03,
    "regularization": 0.02,
    "rho_min": -0.20,
    "rho_max": 0.20,
    "rho_step": 0.01,
    "association_smoothing_alpha": 0.5,
    "association_power": 1.0,
    # The nine-cell HT/FT association must age on the same time axis as the
    # full-time marginal it is coupled to.  Keeping this in the frozen config
    # makes an accidental return to lifetime-equal counts a schema-visible
    # experiment rather than a silent production change.
    "association_half_life_days": 365.0,
    "baseline_smoothing_alpha": 0.5,
    "seed_method": "empirical_association",
    "unknown_team_policy": "league_average",
}
SCORE_REQUIRED_COLUMNS = {
    "date",
    "home_team",
    "away_team",
    "home_goals",
    "away_goals",
    "half_home_goals",
    "half_away_goals",
    "league_key",
    "season",
    "kickoff_utc",
}
MARKET_REQUIRED_COLUMNS = {
    "league_key",
    "season",
    "kickoff_utc",
    "home_team",
    "away_team",
    "bookmaker",
    "home_odds",
    "draw_odds",
    "away_odds",
    "opening_1x2_complete",
}
HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")


class HoldoutEvaluationError(ValueError):
    """Raised when a fixed holdout cannot be evaluated without leakage."""


def _canonical_hash(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HoldoutEvaluationError(
            "evaluation contains non-canonical values"
        ) from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def calculate_evaluation_hash(evaluation: Mapping[str, Any]) -> str:
    payload = dict(evaluation)
    payload.pop("evaluation_hash", None)
    return _canonical_hash(payload)


def _fit_config_is_promoted(fit_config: Mapping[str, Any]) -> bool:
    return dict(fit_config) == PROMOTED_FIT_CONFIG


def _run_is_promoted(
    fit_config: Mapping[str, Any],
    *,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> bool:
    return (
        _fit_config_is_promoted(fit_config)
        and bootstrap_repetitions == DEFAULT_BOOTSTRAP_REPETITIONS
        and bootstrap_seed == BOOTSTRAP_SEED
    )


def _splits_for_run(*, promoted_run: bool) -> tuple[dict[str, Any], ...]:
    if promoted_run:
        return tuple(dict(split) for split in SPLITS)
    return tuple(
        {
            **split,
            "role": EXPERIMENTAL_SPLIT_ROLES[split["split_id"]],
        }
        for split in SPLITS
    )


def _promotion_metadata(
    *,
    fit_configuration_matches_promoted: bool,
    bootstrap_configuration_matches_promoted: bool,
    competition_regime_policy_matches_manager: bool = True,
) -> dict[str, Any]:
    promoted_run = (
        fit_configuration_matches_promoted and bootstrap_configuration_matches_promoted
    )
    return {
        "configuration_status": (
            "promoted_fixed_configuration"
            if promoted_run
            else "experimental_override_not_promotion_evidence"
        ),
        "fit_configuration_matches_promoted": fit_configuration_matches_promoted,
        "bootstrap_configuration_matches_promoted": (
            bootstrap_configuration_matches_promoted
        ),
        "competition_regime_policy_matches_registered_manager": (
            competition_regime_policy_matches_manager
        ),
        "registered_manager_compatible": (
            promoted_run and competition_regime_policy_matches_manager
        ),
        "model_component_evidence_only": True,
        "final_selector_untouched": False,
        "end_to_end_promotion_eligible": False,
        "promotion_ready": False,
        "formal_htft_eligible": False,
        "evaluation_scope": "nine_class_probability_accuracy_only",
        "complete_prekickoff_nine_way_htft_odds_available": False,
        "ev_roi_evaluation_available": False,
        "partial_test_seasons_excluded_from_promotion_evidence": True,
        "end_to_end_status": "future_live_forward_confirmation_required",
        "reason": (
            "2025 and 2026 were inspected during final Top-2 selector "
            "development, and the dataset has no complete timestamped pre-kickoff "
            "nine-way HT/FT prices; it can assess classification probabilities but "
            "cannot confirm executable EV, ROI, or the final end-to-end selector"
        ),
    }


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _require_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise HoldoutEvaluationError(f"{name} must be a SHA-256 hash")
    return value


def _parse_int(raw: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(raw, bool):
        raise HoldoutEvaluationError(f"{name} must be an integer")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise HoldoutEvaluationError(f"{name} must be an integer") from exc
    if str(value) != str(raw).strip() or value < minimum:
        raise HoldoutEvaluationError(f"{name} must be an integer >= {minimum}")
    return value


def _parse_date(raw: Any, name: str) -> date:
    if not isinstance(raw, str):
        raise HoldoutEvaluationError(f"{name} must be an ISO date")
    try:
        value = date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise HoldoutEvaluationError(f"{name} must be an ISO date") from exc
    if value.isoformat() != raw.strip():
        raise HoldoutEvaluationError(f"{name} must be YYYY-MM-DD")
    return value


def _canonical_utc(raw: Any, name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise HoldoutEvaluationError(f"{name} must be a timezone-aware datetime")
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HoldoutEvaluationError(
            f"{name} must be a timezone-aware datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HoldoutEvaluationError(f"{name} needs an explicit UTC offset")
    normalized = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _optional_context_value(raw: Any, *, default: str, name: str) -> str:
    """Normalize an optional pre-match context label without inventing detail."""

    if raw is None:
        return default
    if not isinstance(raw, str):
        raise HoldoutEvaluationError(f"{name} must be a string when present")
    value = raw.strip()
    return value or default


def _is_partial_season_status(value: str) -> bool:
    return value.startswith(PARTIAL_SEASON_STATUS_PREFIX)


def _status_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = row.get("season_status")
        if not isinstance(status, str) or not status:
            raise HoldoutEvaluationError("season_status is missing after normalization")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _split_evaluation_scope(
    rows: Sequence[Mapping[str, Any]], split: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe which evidence a split may contribute to promotion summaries."""

    statuses = _status_counts(rows)
    partial = any(_is_partial_season_status(status) for status in statuses)
    has_rows = bool(rows)
    complete_status_verified = has_rows and set(statuses) == {"complete"}
    if not has_rows:
        evidence_role = "not_available"
    elif partial:
        evidence_role = "research_shadow_partial_season"
    elif not complete_status_verified:
        evidence_role = "research_shadow_unverified_season_status"
    else:
        evidence_role = str(split["role"])
    blockers = [
        "complete timestamped pre-kickoff nine-way HT/FT odds unavailable",
        "final selector lacks untouched live-forward confirmation",
    ]
    if partial:
        blockers.insert(0, "partial test season is research/shadow only")
    elif has_rows and not complete_status_verified:
        blockers.insert(0, "test-season completeness is not explicitly verified")
    if not has_rows:
        blockers.insert(0, "no production-regime test fixtures available")
    return {
        "season_status_counts": statuses,
        "partial_test_season": partial,
        "complete_test_season_status_verified": complete_status_verified,
        "evidence_role": evidence_role,
        "component_promotion_evidence_included": complete_status_verified,
        "promotion_ready": False,
        "formal_htft_eligible": False,
        "classification_accuracy_only": True,
        "ev_roi_evaluation_available": False,
        "promotion_blockers": blockers,
    }


def _safe_bundle_file(root: Path, raw_name: Any, name: str) -> Path:
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise HoldoutEvaluationError(f"{name} is required")
    candidate = (root / raw_name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise HoldoutEvaluationError(f"{name} escapes the dataset directory") from exc
    if not candidate.is_file():
        raise HoldoutEvaluationError(f"{name} does not exist: {candidate}")
    return candidate


def _resolve_manifest(
    dataset_dir: str | Path | None,
    manifest_path: str | Path | None,
) -> tuple[Path, Path, dict[str, Any]]:
    if (dataset_dir is None) == (manifest_path is None):
        raise HoldoutEvaluationError(
            "provide exactly one of dataset_dir or manifest_path"
        )
    if manifest_path is not None:
        manifest_file = Path(manifest_path).resolve()
        root = manifest_file.parent
    else:
        root = Path(dataset_dir).resolve()  # type: ignore[arg-type]
        manifest_file = root / "manifest.json"
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HoldoutEvaluationError(
            f"cannot read dataset manifest: {manifest_file}"
        ) from exc
    if not isinstance(manifest, dict):
        raise HoldoutEvaluationError("dataset manifest must contain an object")
    if manifest.get("artifact_type") != "soccer_history_dataset_bundle":
        raise HoldoutEvaluationError("unexpected dataset manifest artifact_type")
    bundle_hash = _require_hash(manifest.get("bundle_hash"), "manifest.bundle_hash")
    hash_payload = dict(manifest)
    hash_payload.pop("bundle_hash", None)
    if bundle_hash != _canonical_hash(hash_payload):
        raise HoldoutEvaluationError("manifest.bundle_hash does not match contents")
    leagues = manifest.get("leagues")
    if not isinstance(leagues, list) or not leagues:
        raise HoldoutEvaluationError("dataset manifest contains no leagues")
    try:
        validated_manifest = history_importer.validate_bundle(root)
    except history_importer.HistoryImportError as exc:
        raise HoldoutEvaluationError(
            f"dataset bundle failed semantic validation: {exc}"
        ) from exc
    if _canonical_hash(manifest) != _canonical_hash(validated_manifest):
        raise HoldoutEvaluationError(
            "requested manifest does not match the semantically validated bundle"
        )
    return root, manifest_file, manifest


def _score_result(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"


def _load_score_rows(
    path: Path,
    *,
    league_key: str,
    expected_hash: str,
    expected_rows: int,
) -> list[dict[str, Any]]:
    if _file_hash(path) != expected_hash:
        raise HoldoutEvaluationError(f"score dataset hash mismatch: {path.name}")
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise HoldoutEvaluationError(f"cannot read score dataset: {path}") from exc
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise HoldoutEvaluationError(f"score dataset has no header: {path.name}")
        missing = sorted(SCORE_REQUIRED_COLUMNS - set(reader.fieldnames))
        if missing:
            raise HoldoutEvaluationError(
                f"score dataset missing columns: {', '.join(missing)}"
            )
        rows: list[dict[str, Any]] = []
        fixtures: set[tuple[str, str, str]] = set()
        for row_number, row in enumerate(reader, start=2):
            row_league = (row.get("league_key") or "").strip()
            if row_league != league_key:
                raise HoldoutEvaluationError(
                    f"{path.name} row {row_number}: league_key mismatch"
                )
            match_date = _parse_date(row.get("date"), f"row {row_number} date")
            kickoff = _canonical_utc(
                row.get("kickoff_utc"), f"row {row_number} kickoff_utc"
            )
            if (
                datetime.fromisoformat(kickoff.replace("Z", "+00:00")).date()
                != match_date
            ):
                raise HoldoutEvaluationError(
                    f"{path.name} row {row_number}: date and kickoff_utc disagree"
                )
            home_team = (row.get("home_team") or "").strip()
            away_team = (row.get("away_team") or "").strip()
            if not home_team or not away_team or home_team == away_team:
                raise HoldoutEvaluationError(
                    f"{path.name} row {row_number}: invalid teams"
                )
            goals = {
                name: _parse_int(row.get(name), f"row {row_number} {name}")
                for name in (
                    "home_goals",
                    "away_goals",
                    "half_home_goals",
                    "half_away_goals",
                )
            }
            if (
                goals["half_home_goals"] > goals["home_goals"]
                or goals["half_away_goals"] > goals["away_goals"]
            ):
                raise HoldoutEvaluationError(
                    f"{path.name} row {row_number}: half-time goals exceed full-time"
                )
            fixture = (kickoff, home_team, away_team)
            if fixture in fixtures:
                raise HoldoutEvaluationError(
                    f"{path.name} row {row_number}: duplicate fixture"
                )
            fixtures.add(fixture)
            half_result = _score_result(
                goals["half_home_goals"], goals["half_away_goals"]
            )
            full_result = _score_result(goals["home_goals"], goals["away_goals"])
            actual_class = f"{half_result}_{full_result}"
            source_code = (row.get("htft_result") or "").strip()
            expected_code = (
                htft_model.RESULT_CODES[half_result]
                + htft_model.RESULT_CODES[full_result]
            )
            if source_code and source_code != expected_code:
                raise HoldoutEvaluationError(
                    f"{path.name} row {row_number}: htft_result disagrees with scores"
                )
            rows.append(
                {
                    "date": match_date,
                    "kickoff_utc": kickoff,
                    "home_team": home_team,
                    "away_team": away_team,
                    "season": _parse_int(
                        row.get("season"), f"row {row_number} season", minimum=1900
                    ),
                    "competition_regime": (
                        (row.get("competition_regime") or "").strip()
                        or "unlabeled_legacy_source"
                    ),
                    "season_status": _optional_context_value(
                        row.get("season_status"),
                        default=LEGACY_SEASON_STATUS,
                        name=f"{path.name} row {row_number} season_status",
                    ),
                    "format_version": _optional_context_value(
                        row.get("format_version"),
                        default=LEGACY_FORMAT_VERSION,
                        name=f"{path.name} row {row_number} format_version",
                    ),
                    "phase_group": _optional_context_value(
                        row.get("phase_group"),
                        default=LEGACY_PHASE_GROUP,
                        name=f"{path.name} row {row_number} phase_group",
                    ),
                    "competition_key": league_key,
                    "actual_class": actual_class,
                    **goals,
                }
            )
    if len(rows) != expected_rows:
        raise HoldoutEvaluationError(
            f"{path.name} row count {len(rows)} does not match manifest {expected_rows}"
        )
    return sorted(
        rows,
        key=lambda row: (
            row["date"],
            row["kickoff_utc"],
            row["home_team"],
            row["away_team"],
        ),
    )


def _parse_decimal_odds(raw: Any, name: str) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise HoldoutEvaluationError(f"{name} must be decimal odds") from exc
    if not math.isfinite(value) or value <= 1.0:
        raise HoldoutEvaluationError(f"{name} must be finite and greater than 1")
    return value


def _fixture_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (row["kickoff_utc"], row["home_team"], row["away_team"])


def _load_opening_consensus(
    path: Path,
    *,
    league_key: str,
    expected_hash: str,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    if _file_hash(path) != expected_hash:
        raise HoldoutEvaluationError(f"opening-market hash mismatch: {path.name}")
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise HoldoutEvaluationError(f"cannot read opening markets: {path}") from exc
    by_fixture: dict[tuple[str, str, str], dict[str, dict[str, float]]] = {}
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise HoldoutEvaluationError(
                f"opening-market CSV has no header: {path.name}"
            )
        missing = sorted(MARKET_REQUIRED_COLUMNS - set(reader.fieldnames))
        if missing:
            raise HoldoutEvaluationError(
                f"opening-market CSV missing columns: {', '.join(missing)}"
            )
        for row_number, row in enumerate(reader, start=2):
            if (row.get("league_key") or "").strip() != league_key:
                raise HoldoutEvaluationError(
                    f"{path.name} row {row_number}: league_key mismatch"
                )
            complete = (row.get("opening_1x2_complete") or "").strip().lower()
            if complete not in {"true", "false"}:
                raise HoldoutEvaluationError(
                    f"{path.name} row {row_number}: opening_1x2_complete must be true/false"
                )
            if complete == "false":
                continue
            kickoff = _canonical_utc(
                row.get("kickoff_utc"), f"market row {row_number} kickoff_utc"
            )
            home_team = (row.get("home_team") or "").strip()
            away_team = (row.get("away_team") or "").strip()
            bookmaker = (row.get("bookmaker") or "").strip()
            if not home_team or not away_team or not bookmaker:
                raise HoldoutEvaluationError(
                    f"{path.name} row {row_number}: fixture and bookmaker are required"
                )
            odds = {
                result: _parse_decimal_odds(
                    row.get(f"{result}_odds"),
                    f"market row {row_number} {result}_odds",
                )
                for result in htft_model.RESULTS
            }
            inverse = {result: 1.0 / odds[result] for result in htft_model.RESULTS}
            overround = math.fsum(inverse.values())
            de_vigged = {
                result: inverse[result] / overround for result in htft_model.RESULTS
            }
            key = (kickoff, home_team, away_team)
            books = by_fixture.setdefault(key, {})
            if bookmaker in books:
                raise HoldoutEvaluationError(
                    f"{path.name} row {row_number}: duplicate bookmaker fixture"
                )
            books[bookmaker] = de_vigged

    consensus: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, books in by_fixture.items():
        book_names = sorted(books)
        probabilities = {
            result: math.fsum(books[book][result] for book in book_names)
            / len(book_names)
            for result in htft_model.RESULTS
        }
        total = math.fsum(probabilities.values())
        probabilities = {
            result: value / total for result, value in probabilities.items()
        }
        consensus[key] = {
            "probabilities": probabilities,
            "bookmaker_count": len(book_names),
            "bookmakers": book_names,
        }
    return consensus


def _new_accumulator(threshold: float) -> dict[str, Any]:
    return {
        "threshold": threshold,
        "sample_count": 0,
        "log_loss_sum": 0.0,
        "brier_sum": 0.0,
        "top_one_hits": 0,
        "top_two_hits": 0,
        "pair_covered_count": 0,
        "pair_hit_count": 0,
        "class_support": {name: 0 for name in htft_model.HTFT_CLASSES},
        "class_probability_sum": {name: 0.0 for name in htft_model.HTFT_CLASSES},
        "class_top_one_predictions": {name: 0 for name in htft_model.HTFT_CLASSES},
        "class_top_one_true_positives": {name: 0 for name in htft_model.HTFT_CLASSES},
    }


def _new_paired_accumulator() -> dict[str, Any]:
    return {"observations": []}


def _new_paired_cohorts() -> dict[str, dict[str, Any]]:
    return {
        name: _new_paired_accumulator()
        for name in ("overall", "known_teams", "league_average_fallback")
    }


def _add_paired_cohort_score(
    cohorts: dict[str, dict[str, Any]],
    candidate: Mapping[str, float],
    baseline: Mapping[str, float],
    actual_class: str,
    *,
    used_fallback: bool,
    group: str,
) -> None:
    _add_paired_score(
        cohorts["overall"], candidate, baseline, actual_class, group=group
    )
    cohort = "league_average_fallback" if used_fallback else "known_teams"
    _add_paired_score(cohorts[cohort], candidate, baseline, actual_class, group=group)


def _finalize_paired_cohorts(
    cohorts: Mapping[str, Mapping[str, Any]],
    *,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    return {
        name: _finalize_paired_accumulator(
            cohorts[name],
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        )
        for name in ("overall", "known_teams", "league_average_fallback")
    }


def _validate_probability_vector(
    probabilities: Mapping[str, float],
) -> dict[str, float]:
    if set(probabilities) != set(htft_model.HTFT_CLASSES):
        raise HoldoutEvaluationError(
            "prediction does not contain all nine HT/FT classes"
        )
    values = {name: float(probabilities[name]) for name in htft_model.HTFT_CLASSES}
    if any(not math.isfinite(value) or value < 0.0 for value in values.values()):
        raise HoldoutEvaluationError(
            "HT/FT probabilities must be finite and non-negative"
        )
    if abs(math.fsum(values.values()) - 1.0) > 1e-9:
        raise HoldoutEvaluationError("HT/FT probabilities must sum to one")
    return values


def _losses(
    probabilities: Mapping[str, float], actual_class: str
) -> tuple[float, float]:
    values = _validate_probability_vector(probabilities)
    if actual_class not in values:
        raise HoldoutEvaluationError("actual HT/FT class is invalid")
    log_loss = -math.log(max(values[actual_class], EPSILON))
    brier = math.fsum(
        (values[name] - (1.0 if name == actual_class else 0.0)) ** 2
        for name in htft_model.HTFT_CLASSES
    )
    return log_loss, brier


def _add_score(
    accumulator: dict[str, Any],
    probabilities: Mapping[str, float],
    actual_class: str,
) -> None:
    values = _validate_probability_vector(probabilities)
    log_loss, brier = _losses(values, actual_class)
    ranked = sorted(
        htft_model.HTFT_CLASSES,
        key=lambda name: (-values[name], htft_model.HTFT_CLASSES.index(name)),
    )
    top_two = ranked[:2]
    top_one_hit = ranked[0] == actual_class
    top_two_hit = actual_class in top_two
    pair_mass = values[top_two[0]] + values[top_two[1]]
    covered = pair_mass >= accumulator["threshold"]
    accumulator["sample_count"] += 1
    accumulator["log_loss_sum"] += log_loss
    accumulator["brier_sum"] += brier
    accumulator["top_one_hits"] += int(top_one_hit)
    accumulator["top_two_hits"] += int(top_two_hit)
    accumulator["pair_covered_count"] += int(covered)
    accumulator["pair_hit_count"] += int(covered and top_two_hit)
    accumulator["class_support"][actual_class] += 1
    accumulator["class_top_one_predictions"][ranked[0]] += 1
    accumulator["class_top_one_true_positives"][actual_class] += int(top_one_hit)
    for name in htft_model.HTFT_CLASSES:
        accumulator["class_probability_sum"][name] += values[name]


def _add_paired_score(
    accumulator: dict[str, Any],
    candidate: Mapping[str, float],
    baseline: Mapping[str, float],
    actual_class: str,
    *,
    group: str,
) -> None:
    candidate_log_loss, candidate_brier = _losses(candidate, actual_class)
    baseline_log_loss, baseline_brier = _losses(baseline, actual_class)
    accumulator["observations"].append(
        {
            "group": group,
            "nine_class_log_loss_delta": candidate_log_loss - baseline_log_loss,
            "nine_class_brier_delta": candidate_brier - baseline_brier,
        }
    )


def _merge_paired_accumulators(
    accumulators: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = _new_paired_accumulator()
    for accumulator in accumulators:
        result["observations"].extend(accumulator["observations"])
    return result


def _bootstrap_delta(
    observations: Sequence[Mapping[str, Any]],
    field: str,
    *,
    repetitions: int,
    seed: int,
) -> dict[str, Any]:
    if not observations:
        return {"mean_delta": None, "ci95_low": None, "ci95_high": None}
    groups: dict[str, list[float]] = {}
    for observation in observations:
        group = str(observation["group"])
        groups.setdefault(group, []).append(float(observation[field]))
    observed = math.fsum(value for values in groups.values() for value in values) / len(
        observations
    )
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(repetitions):
        total = 0.0
        count = 0
        for group in sorted(groups):
            values = groups[group]
            total += math.fsum(
                values[rng.randrange(len(values))] for _ in range(len(values))
            )
            count += len(values)
        samples.append(total / count)
    samples.sort()
    lower_index = min(repetitions - 1, int(0.025 * repetitions))
    upper_index = min(repetitions - 1, int(0.975 * repetitions))
    return {
        "mean_delta": observed,
        "ci95_low": samples[lower_index],
        "ci95_high": samples[upper_index],
    }


def _finalize_paired_accumulator(
    accumulator: Mapping[str, Any],
    *,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    observations = accumulator["observations"]
    return {
        "sample_count": len(observations),
        "interpretation": "negative delta favors the HT/FT model",
        "nine_class_log_loss": _bootstrap_delta(
            observations,
            "nine_class_log_loss_delta",
            repetitions=bootstrap_repetitions,
            seed=bootstrap_seed,
        ),
        "nine_class_brier": _bootstrap_delta(
            observations,
            "nine_class_brier_delta",
            repetitions=bootstrap_repetitions,
            seed=bootstrap_seed + 1,
        ),
        "bootstrap": {
            "method": "paired stratified resampling with replacement",
            "confidence_level": 0.95,
            "repetitions": bootstrap_repetitions,
            "seed": bootstrap_seed,
        },
    }


def _merge_accumulators(
    accumulators: Sequence[Mapping[str, Any]], *, threshold: float
) -> dict[str, Any]:
    result = _new_accumulator(threshold)
    for item in accumulators:
        if abs(float(item["threshold"]) - threshold) > 1e-15:
            raise HoldoutEvaluationError("cannot merge different pair-mass gates")
        for field in (
            "sample_count",
            "log_loss_sum",
            "brier_sum",
            "top_one_hits",
            "top_two_hits",
            "pair_covered_count",
            "pair_hit_count",
        ):
            result[field] += item[field]
        for field in (
            "class_support",
            "class_probability_sum",
            "class_top_one_predictions",
            "class_top_one_true_positives",
        ):
            for name in htft_model.HTFT_CLASSES:
                result[field][name] += item[field][name]
    return result


def _finalize_accumulator(accumulator: Mapping[str, Any]) -> dict[str, Any]:
    sample_count = int(accumulator["sample_count"])
    covered = int(accumulator["pair_covered_count"])
    metrics = {
        "sample_count": sample_count,
        "nine_class_log_loss": (
            accumulator["log_loss_sum"] / sample_count if sample_count else None
        ),
        "nine_class_brier": (
            accumulator["brier_sum"] / sample_count if sample_count else None
        ),
        "top_one_accuracy": (
            accumulator["top_one_hits"] / sample_count if sample_count else None
        ),
        "top_two_accuracy": (
            accumulator["top_two_hits"] / sample_count if sample_count else None
        ),
        "top_one_hits": int(accumulator["top_one_hits"]),
        "top_two_hits": int(accumulator["top_two_hits"]),
        "per_class": {
            name: {
                "support": int(accumulator["class_support"][name]),
                "observed_rate": (
                    accumulator["class_support"][name] / sample_count
                    if sample_count
                    else None
                ),
                "mean_predicted_probability": (
                    accumulator["class_probability_sum"][name] / sample_count
                    if sample_count
                    else None
                ),
                "calibration_error_pp": (
                    (
                        accumulator["class_probability_sum"][name] / sample_count
                        - accumulator["class_support"][name] / sample_count
                    )
                    * 100
                    if sample_count
                    else None
                ),
                "top_one_predictions": int(
                    accumulator["class_top_one_predictions"][name]
                ),
                "top_one_true_positives": int(
                    accumulator["class_top_one_true_positives"][name]
                ),
                "top_one_recall": (
                    accumulator["class_top_one_true_positives"][name]
                    / accumulator["class_support"][name]
                    if accumulator["class_support"][name]
                    else None
                ),
                "top_one_precision": (
                    accumulator["class_top_one_true_positives"][name]
                    / accumulator["class_top_one_predictions"][name]
                    if accumulator["class_top_one_predictions"][name]
                    else None
                ),
            }
            for name in htft_model.HTFT_CLASSES
        },
    }
    return {
        "metrics": metrics,
        "pair_mass_gate": {
            "threshold": float(accumulator["threshold"]),
            "eligible_sample_count": sample_count,
            "covered_count": covered,
            "coverage": covered / sample_count if sample_count else None,
            "hit_count": int(accumulator["pair_hit_count"]),
            "hit_rate_when_covered": (
                accumulator["pair_hit_count"] / covered if covered else None
            ),
            "selection": "joint_probability_top_two",
            "tie_break": "htft_model_class_order",
        },
    }


def _cohort_accumulators(threshold: float) -> dict[str, dict[str, Any]]:
    return {
        "overall": _new_accumulator(threshold),
        "known_teams": _new_accumulator(threshold),
        "league_average_fallback": _new_accumulator(threshold),
    }


def _add_cohort_score(
    cohorts: dict[str, dict[str, Any]],
    probabilities: Mapping[str, float],
    actual_class: str,
    *,
    used_fallback: bool,
) -> None:
    _add_score(cohorts["overall"], probabilities, actual_class)
    cohort = "league_average_fallback" if used_fallback else "known_teams"
    _add_score(cohorts[cohort], probabilities, actual_class)


def _merge_cohorts(
    cohorts: Sequence[Mapping[str, Mapping[str, Any]]], *, threshold: float
) -> dict[str, dict[str, Any]]:
    return {
        name: _merge_accumulators(
            [cohort[name] for cohort in cohorts], threshold=threshold
        )
        for name in ("overall", "known_teams", "league_average_fallback")
    }


def _finalize_cohorts(cohorts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {name: _finalize_accumulator(value) for name, value in cohorts.items()}


def _new_context_slice_accumulators() -> dict[str, dict[str, dict[str, Any]]]:
    return {name: {} for name in ("format_version", "phase_group")}


def _new_context_slice_accumulator() -> dict[str, Any]:
    return {
        "model": _cohort_accumulators(MODEL_PAIR_MASS_THRESHOLD),
        "baseline": _new_accumulator(MODEL_PAIR_MASS_THRESHOLD),
        "paired": _new_paired_accumulator(),
    }


def _add_context_slice_score(
    slices: dict[str, dict[str, dict[str, Any]]],
    *,
    format_version: str,
    phase_group: str,
    model_probabilities: Mapping[str, float],
    baseline_probabilities: Mapping[str, float],
    actual_class: str,
    used_fallback: bool,
    group: str,
) -> None:
    for dimension, value in (
        ("format_version", format_version),
        ("phase_group", phase_group),
    ):
        accumulator = slices[dimension].setdefault(
            value, _new_context_slice_accumulator()
        )
        _add_cohort_score(
            accumulator["model"],
            model_probabilities,
            actual_class,
            used_fallback=used_fallback,
        )
        _add_score(accumulator["baseline"], baseline_probabilities, actual_class)
        _add_paired_score(
            accumulator["paired"],
            model_probabilities,
            baseline_probabilities,
            actual_class,
            group=group,
        )


def _finalize_context_slice_accumulator(
    accumulator: Mapping[str, Any],
    *,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    model = _finalize_cohorts(accumulator["model"])
    return {
        "sample_count": model["overall"]["metrics"]["sample_count"],
        "model_only": model,
        "league_empirical_frequency_baseline": {
            "method": "dirichlet_smoothed_training_htft_frequency",
            "metrics": _finalize_accumulator(accumulator["baseline"])["metrics"],
        },
        "model_minus_empirical_baseline": _finalize_paired_accumulator(
            accumulator["paired"],
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
    }


def _finalize_context_slices(
    slices: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    return {
        dimension: {
            value: _finalize_context_slice_accumulator(
                accumulator,
                bootstrap_repetitions=bootstrap_repetitions,
                bootstrap_seed=bootstrap_seed,
            )
            for value, accumulator in sorted(values.items())
        }
        for dimension, values in (
            (name, slices.get(name, {})) for name in ("format_version", "phase_group")
        )
    }


def _merge_context_slices(
    split_accumulators: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    merged = _new_context_slice_accumulators()
    for dimension in merged:
        values = sorted(
            {
                value
                for item in split_accumulators
                for value in item.get("context_slices", {}).get(dimension, {})
            }
        )
        for value in values:
            sources = [
                item["context_slices"][dimension][value]
                for item in split_accumulators
                if value in item.get("context_slices", {}).get(dimension, {})
            ]
            merged[dimension][value] = {
                "model": _merge_cohorts(
                    [source["model"] for source in sources],
                    threshold=MODEL_PAIR_MASS_THRESHOLD,
                ),
                "baseline": _merge_accumulators(
                    [source["baseline"] for source in sources],
                    threshold=MODEL_PAIR_MASS_THRESHOLD,
                ),
                "paired": _merge_paired_accumulators(
                    [source["paired"] for source in sources]
                ),
            }
    return merged


def _write_training_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = (
        "date",
        "home_team",
        "away_team",
        "home_goals",
        "away_goals",
        "half_home_goals",
        "half_away_goals",
        "league_key",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "date": row["date"].isoformat(),
                    "home_team": row["home_team"],
                    "away_team": row["away_team"],
                    "home_goals": row["home_goals"],
                    "away_goals": row["away_goals"],
                    "half_home_goals": row["half_home_goals"],
                    "half_away_goals": row["half_away_goals"],
                    "league_key": row["competition_key"],
                }
            )


def _historical_model_timestamp(
    model: Mapping[str, Any], timestamp: str
) -> dict[str, Any]:
    result = copy.deepcopy(model)
    result["generated_at"] = timestamp
    for component in result["components"].values():
        component["generated_at"] = timestamp
    htft_model.validate_model(result)
    return result


def _research_market_probabilities(
    prediction: Mapping[str, Any],
    full_time_probabilities: Mapping[str, float],
) -> tuple[dict[str, float], dict[str, Any]]:
    """Rake a model prediction internally; never call the formal anchor API."""

    joint, ipf_audit = htft_model.iterative_proportional_fit(
        prediction["joint_construction"]["raw_joint"],
        prediction["htft"]["half_time_marginal"],
        full_time_probabilities,
        tolerance=1e-12,
        max_iterations=1000,
    )
    probabilities = {
        f"{half}_{full}": joint[row][column]
        for row, half in enumerate(htft_model.RESULTS)
        for column, full in enumerate(htft_model.RESULTS)
    }
    return probabilities, ipf_audit


def _empirical_frequency_baseline(
    training_rows: Sequence[Mapping[str, Any]], *, smoothing_alpha: float
) -> tuple[dict[str, float], dict[str, int]]:
    if not math.isfinite(smoothing_alpha) or smoothing_alpha <= 0.0:
        raise HoldoutEvaluationError("baseline smoothing_alpha must be positive")
    counts = {name: 0 for name in htft_model.HTFT_CLASSES}
    for row in training_rows:
        actual_class = row["actual_class"]
        if actual_class not in counts:
            raise HoldoutEvaluationError("training row has an invalid HT/FT class")
        counts[actual_class] += 1
    denominator = len(training_rows) + smoothing_alpha * len(counts)
    probabilities = {
        name: (counts[name] + smoothing_alpha) / denominator
        for name in htft_model.HTFT_CLASSES
    }
    _validate_probability_vector(probabilities)
    return probabilities, counts


def _competition_regime_counts(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        regime = row.get("competition_regime")
        if not isinstance(regime, str) or not regime:
            raise HoldoutEvaluationError("competition_regime is missing")
        counts[regime] = counts.get(regime, 0) + 1
    return dict(sorted(counts.items()))


def _partition_formal_regimes(
    rows: Sequence[Mapping[str, Any]],
    *,
    league_key: str,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    formal: list[Mapping[str, Any]] = []
    excluded: list[Mapping[str, Any]] = []
    allowed_regimes = _formal_competition_regimes(league_key)
    for row in rows:
        regime = row.get("competition_regime")
        if not isinstance(regime, str) or not regime:
            raise HoldoutEvaluationError("competition_regime is missing")
        (formal if regime in allowed_regimes else excluded).append(row)
    return formal, excluded


def _validated_declared_regime_counts(value: Any, name: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise HoldoutEvaluationError(f"{name} must be an object")
    counts: dict[str, int] = {}
    for regime, count in value.items():
        if (
            not isinstance(regime, str)
            or not regime
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            raise HoldoutEvaluationError(f"{name} contains an invalid count")
        counts[regime] = count
    return dict(sorted(counts.items()))


def _fit_and_score_split(
    rows: Sequence[Mapping[str, Any]],
    split: Mapping[str, Any],
    *,
    league_key: str,
    manifest_hash: str,
    opening_consensus: Mapping[tuple[str, str, str], Mapping[str, Any]] | None,
    fit_config: Mapping[str, Any],
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    training_candidates = [
        row for row in rows if row["season"] <= split["training_season_max"]
    ]
    test_candidates = [row for row in rows if row["season"] == split["test_season"]]
    training_rows, excluded_training_rows = _partition_formal_regimes(
        training_candidates, league_key=league_key
    )
    test_rows, excluded_test_rows = _partition_formal_regimes(
        test_candidates, league_key=league_key
    )
    model_cohorts = _cohort_accumulators(MODEL_PAIR_MASS_THRESHOLD)
    market_cohorts = _cohort_accumulators(MARKET_PAIR_MASS_THRESHOLD)
    baseline_accumulator = _new_accumulator(MODEL_PAIR_MASS_THRESHOLD)
    paired_cohorts = _new_paired_cohorts()
    paired_accumulator = paired_cohorts["overall"]
    context_slices = _new_context_slice_accumulators()
    evaluation_scope = _split_evaluation_scope(test_rows, split)
    if not test_rows:
        return (
            {
                **split,
                "status": "not_available",
                "evaluation_scope": evaluation_scope,
                "training_match_count": len(training_rows),
                "test_match_count": 0,
                "training_competition_regime_counts": (
                    _competition_regime_counts(training_rows)
                ),
                "test_competition_regime_counts": {},
                "excluded_training_match_count": len(excluded_training_rows),
                "excluded_test_match_count": len(excluded_test_rows),
                "excluded_training_competition_regime_counts": (
                    _competition_regime_counts(excluded_training_rows)
                ),
                "excluded_test_competition_regime_counts": (
                    _competition_regime_counts(excluded_test_rows)
                ),
                "model_hash": None,
                "model_only": _finalize_cohorts(model_cohorts),
                "league_empirical_frequency_baseline": {
                    "method": "dirichlet_smoothed_training_htft_frequency",
                    "smoothing_alpha": fit_config["baseline_smoothing_alpha"],
                    "training_class_counts": None,
                    "probabilities": None,
                    "metrics": _finalize_accumulator(baseline_accumulator)["metrics"],
                },
                "model_minus_empirical_baseline": _finalize_paired_accumulator(
                    paired_accumulator,
                    bootstrap_repetitions=bootstrap_repetitions,
                    bootstrap_seed=bootstrap_seed,
                ),
                "model_minus_empirical_baseline_by_team_availability": (
                    _finalize_paired_cohorts(
                        paired_cohorts,
                        bootstrap_repetitions=bootstrap_repetitions,
                        bootstrap_seed=bootstrap_seed,
                    )
                ),
                "context_slices": _finalize_context_slices(
                    context_slices,
                    bootstrap_repetitions=bootstrap_repetitions,
                    bootstrap_seed=bootstrap_seed,
                ),
                "research_opening_market": {
                    "policy": RESEARCH_MARKET_POLICY,
                    "research_only": True,
                    "collection_time_status": "unavailable_in_source",
                    "official_anchor_interface_used": False,
                    "anchor_available_count": 0,
                    "anchor_availability": None,
                    "cohorts": _finalize_cohorts(market_cohorts),
                },
                "forecasts": [],
            },
            {
                "model": model_cohorts,
                "market": market_cohorts,
                "baseline": baseline_accumulator,
                "paired": paired_accumulator,
                "context_slices": context_slices,
            },
        )
    if len(training_rows) < 2:
        raise HoldoutEvaluationError(
            f"{league_key} {split['split_id']}: fewer than two training matches"
        )
    training_end = max(row["date"] for row in training_rows)
    test_start = min(row["date"] for row in test_rows)
    if training_end >= test_start:
        raise HoldoutEvaluationError(
            f"{league_key} {split['split_id']}: training date overlaps holdout"
        )
    if any(row["season"] > split["training_season_max"] for row in training_rows):
        raise HoldoutEvaluationError("fixed-season training cutoff leaked")
    generated_at = training_end.isoformat() + "T23:59:59Z"
    baseline_probabilities, baseline_counts = _empirical_frequency_baseline(
        training_rows, smoothing_alpha=fit_config["baseline_smoothing_alpha"]
    )
    bookmaker_counts: list[int] = []
    fallback_fixtures = 0
    forecasts: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="soccer-htft-holdout-") as temporary:
        training_path = Path(temporary) / "training.csv"
        _write_training_csv(training_path, training_rows)
        try:
            model = htft_model.fit_model(
                training_path,
                half_time_half_life_days=fit_config["half_time_half_life_days"],
                second_half_half_life_days=fit_config["second_half_half_life_days"],
                full_time_half_life_days=fit_config["full_time_half_life_days"],
                iterations=fit_config["iterations"],
                learning_rate=fit_config["learning_rate"],
                regularization=fit_config["regularization"],
                rho_min=fit_config["rho_min"],
                rho_max=fit_config["rho_max"],
                rho_step=fit_config["rho_step"],
                association_smoothing_alpha=fit_config["association_smoothing_alpha"],
                association_power=fit_config["association_power"],
                association_half_life_days=fit_config["association_half_life_days"],
                competition_key=league_key,
                dataset_manifest_hash=manifest_hash,
            )
        except htft_model.HTFTModelError as exc:
            raise HoldoutEvaluationError(
                f"{league_key} {split['split_id']} cannot fit: {exc}"
            ) from exc
    model = _historical_model_timestamp(model, generated_at)
    training_teams = {row["home_team"] for row in training_rows} | {
        row["away_team"] for row in training_rows
    }
    for row in test_rows:
        kickoff = row["kickoff_utc"]
        if datetime.fromisoformat(
            kickoff.replace("Z", "+00:00")
        ) <= datetime.fromisoformat(generated_at.replace("Z", "+00:00")):
            raise HoldoutEvaluationError(
                f"{league_key} {split['split_id']}: prediction is not before kickoff"
            )
        used_fallback = (
            row["home_team"] not in training_teams
            or row["away_team"] not in training_teams
        )
        fallback_fixtures += int(used_fallback)
        try:
            prediction = htft_model.predict_model(
                model,
                row["home_team"],
                row["away_team"],
                kickoff=kickoff,
                generated_at=generated_at,
                unknown_team_policy="league_average",
                seed_method="empirical_association",
            )
        except htft_model.HTFTModelError as exc:
            raise HoldoutEvaluationError(
                f"{league_key} {split['split_id']} cannot predict: {exc}"
            ) from exc
        _add_cohort_score(
            model_cohorts,
            prediction["htft"]["probabilities"],
            row["actual_class"],
            used_fallback=used_fallback,
        )
        _add_score(
            baseline_accumulator,
            baseline_probabilities,
            row["actual_class"],
        )
        _add_paired_score(
            paired_accumulator,
            prediction["htft"]["probabilities"],
            baseline_probabilities,
            row["actual_class"],
            group=league_key,
        )
        cohort = "league_average_fallback" if used_fallback else "known_teams"
        _add_paired_score(
            paired_cohorts[cohort],
            prediction["htft"]["probabilities"],
            baseline_probabilities,
            row["actual_class"],
            group=league_key,
        )
        _add_context_slice_score(
            context_slices,
            format_version=row["format_version"],
            phase_group=row["phase_group"],
            model_probabilities=prediction["htft"]["probabilities"],
            baseline_probabilities=baseline_probabilities,
            actual_class=row["actual_class"],
            used_fallback=used_fallback,
            group=league_key,
        )
        research_forecast: dict[str, Any] | None = None
        if opening_consensus is not None:
            anchor = opening_consensus.get(_fixture_key(row))
            if anchor is not None:
                probabilities, _ipf_audit = _research_market_probabilities(
                    prediction, anchor["probabilities"]
                )
                _add_cohort_score(
                    market_cohorts,
                    probabilities,
                    row["actual_class"],
                    used_fallback=used_fallback,
                )
                bookmaker_counts.append(int(anchor["bookmaker_count"]))
                research_ranked = sorted(
                    htft_model.HTFT_CLASSES,
                    key=lambda name: (
                        -probabilities[name],
                        htft_model.HTFT_CLASSES.index(name),
                    ),
                )
                research_forecast = {
                    "policy": RESEARCH_MARKET_POLICY,
                    "probabilities": probabilities,
                    "top_two": research_ranked[:2],
                    "pair_mass": (
                        probabilities[research_ranked[0]]
                        + probabilities[research_ranked[1]]
                    ),
                    "bookmaker_count": int(anchor["bookmaker_count"]),
                    "bookmakers": list(anchor["bookmakers"]),
                }
        ranked = prediction["htft"]["ranked"]
        forecasts.append(
            {
                "season": row["season"],
                "date": row["date"].isoformat(),
                "kickoff_utc": kickoff,
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "actual_class": row["actual_class"],
                "competition_regime": row["competition_regime"],
                "season_status": row["season_status"],
                "format_version": row["format_version"],
                "phase_group": row["phase_group"],
                "used_league_average_fallback": used_fallback,
                "training_cutoff_date": training_end.isoformat(),
                "model_hash": model["model_hash"],
                "model_probabilities": prediction["htft"]["probabilities"],
                "model_top_two": [
                    {
                        "class": ranked[index]["class"],
                        "probability": ranked[index]["probability"],
                    }
                    for index in range(2)
                ],
                "model_pair_mass": (
                    ranked[0]["probability"] + ranked[1]["probability"]
                ),
                "empirical_baseline_probabilities": baseline_probabilities,
                "research_opening_market": research_forecast,
            }
        )

    model_final = _finalize_cohorts(model_cohorts)
    market_final = _finalize_cohorts(market_cohorts)
    split_result = {
        **split,
        "status": "evaluated",
        "evaluation_scope": evaluation_scope,
        "training_match_count": len(training_rows),
        "test_match_count": len(test_rows),
        "training_competition_regime_counts": (
            _competition_regime_counts(training_rows)
        ),
        "test_competition_regime_counts": _competition_regime_counts(test_rows),
        "excluded_training_match_count": len(excluded_training_rows),
        "excluded_test_match_count": len(excluded_test_rows),
        "excluded_training_competition_regime_counts": (
            _competition_regime_counts(excluded_training_rows)
        ),
        "excluded_test_competition_regime_counts": (
            _competition_regime_counts(excluded_test_rows)
        ),
        "training_date_start": min(row["date"] for row in training_rows).isoformat(),
        "training_cutoff_date": training_end.isoformat(),
        "test_date_start": test_start.isoformat(),
        "test_date_end": max(row["date"] for row in test_rows).isoformat(),
        "strict_cutoff_verified": training_end < test_start,
        "prediction_generated_at_policy": "training_cutoff_date_23:59:59Z",
        "model_hash": model["model_hash"],
        "model_training_data_hash": model["training"]["source_data_hash"],
        "unknown_team_policy": "league_average",
        "fallback_fixture_count": fallback_fixtures,
        "model_only": model_final,
        "league_empirical_frequency_baseline": {
            "method": "dirichlet_smoothed_training_htft_frequency",
            "smoothing_alpha": fit_config["baseline_smoothing_alpha"],
            "training_class_counts": baseline_counts,
            "probabilities": baseline_probabilities,
            "metrics": _finalize_accumulator(baseline_accumulator)["metrics"],
        },
        "model_minus_empirical_baseline": _finalize_paired_accumulator(
            paired_accumulator,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
        "model_minus_empirical_baseline_by_team_availability": (
            _finalize_paired_cohorts(
                paired_cohorts,
                bootstrap_repetitions=bootstrap_repetitions,
                bootstrap_seed=bootstrap_seed,
            )
        ),
        "context_slices": _finalize_context_slices(
            context_slices,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
        "research_opening_market": {
            "policy": RESEARCH_MARKET_POLICY,
            "research_only": True,
            "collection_time_status": "unavailable_in_source",
            "official_anchor_interface_used": False,
            "construction": (
                "arithmetic mean of per-bookmaker de-vigged opening 1X2; "
                "prediction raw joint seed plus evaluator-local IPF"
            ),
            "anchor_available_count": market_cohorts["overall"]["sample_count"],
            "anchor_availability": (
                market_cohorts["overall"]["sample_count"] / len(test_rows)
            ),
            "bookmaker_count": {
                "minimum": min(bookmaker_counts) if bookmaker_counts else None,
                "maximum": max(bookmaker_counts) if bookmaker_counts else None,
                "mean": (
                    math.fsum(bookmaker_counts) / len(bookmaker_counts)
                    if bookmaker_counts
                    else None
                ),
            },
            "cohorts": market_final,
        },
        "forecasts": forecasts,
    }
    return split_result, {
        "model": model_cohorts,
        "market": market_cohorts,
        "baseline": baseline_accumulator,
        "paired": paired_accumulator,
        "context_slices": context_slices,
    }


def _aggregate_split_cohorts(
    split_accumulators: Sequence[Mapping[str, Any]],
    *,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    model = _merge_cohorts(
        [item["model"] for item in split_accumulators],
        threshold=MODEL_PAIR_MASS_THRESHOLD,
    )
    market = _merge_cohorts(
        [item["market"] for item in split_accumulators],
        threshold=MARKET_PAIR_MASS_THRESHOLD,
    )
    baseline = _merge_accumulators(
        [item["baseline"] for item in split_accumulators],
        threshold=MODEL_PAIR_MASS_THRESHOLD,
    )
    paired = _merge_paired_accumulators([item["paired"] for item in split_accumulators])
    context_slices = _merge_context_slices(split_accumulators)
    return {
        "model_only": _finalize_cohorts(model),
        "league_empirical_frequency_baseline": {
            "method": "dirichlet_smoothed_training_htft_frequency",
            "metrics": _finalize_accumulator(baseline)["metrics"],
        },
        "model_minus_empirical_baseline": _finalize_paired_accumulator(
            paired,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
        "context_slices": _finalize_context_slices(
            context_slices,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
        "research_opening_market": {
            "policy": RESEARCH_MARKET_POLICY,
            "research_only": True,
            "collection_time_status": "unavailable_in_source",
            "official_anchor_interface_used": False,
            "cohorts": _finalize_cohorts(market),
        },
    }


def _promotion_evidence_summary(
    *,
    eligible_cohorts: Sequence[Mapping[str, str]],
    research_shadow_cohorts: Sequence[Mapping[str, str]],
    eligible_accumulators: Sequence[Mapping[str, Any]],
    bootstrap_repetitions: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    """Keep partial-season rows visible while excluding them from promotion evidence."""

    return {
        "policy": "complete-test-seasons-only-component-evidence-v1",
        "eligible_cohorts": [dict(item) for item in eligible_cohorts],
        "research_shadow_cohorts": [dict(item) for item in research_shadow_cohorts],
        "partial_test_seasons_excluded": True,
        "promotion_ready": False,
        "formal_htft_eligible": False,
        "classification_accuracy_only": True,
        "ev_roi_evaluation_available": False,
        "eligible_classification_summary": _aggregate_split_cohorts(
            eligible_accumulators,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
    }


def evaluate_bundle(
    *,
    dataset_dir: str | Path | None = None,
    manifest_path: str | Path | None = None,
    output_path: str | Path | None = None,
    include_opening_market: bool = False,
    iterations: int = 1200,
    learning_rate: float = 0.03,
    regularization: float = 0.02,
    rho_min: float = -0.20,
    rho_max: float = 0.20,
    rho_step: float = 0.01,
    association_smoothing_alpha: float = 0.5,
    association_power: float = 1.0,
    association_half_life_days: float = 365.0,
    bootstrap_repetitions: int = DEFAULT_BOOTSTRAP_REPETITIONS,
    bootstrap_seed: int = BOOTSTRAP_SEED,
    experimental_override: bool = False,
) -> dict[str, Any]:
    """Evaluate every league in an audited history-importer bundle."""

    root, manifest_file, manifest = _resolve_manifest(dataset_dir, manifest_path)
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations < 1
    ):
        raise HoldoutEvaluationError("iterations must be a positive integer")
    if (
        isinstance(bootstrap_repetitions, bool)
        or not isinstance(bootstrap_repetitions, int)
        or bootstrap_repetitions < 1
    ):
        raise HoldoutEvaluationError("bootstrap_repetitions must be a positive integer")
    if isinstance(bootstrap_seed, bool) or not isinstance(bootstrap_seed, int):
        raise HoldoutEvaluationError("bootstrap_seed must be an integer")
    if not isinstance(experimental_override, bool):
        raise HoldoutEvaluationError("experimental_override must be boolean")
    manifest_hash = manifest["bundle_hash"]
    fit_config = {
        "half_time_half_life_days": 730.0,
        "second_half_half_life_days": 365.0,
        "full_time_half_life_days": 365.0,
        "iterations": iterations,
        "learning_rate": learning_rate,
        "regularization": regularization,
        "rho_min": rho_min,
        "rho_max": rho_max,
        "rho_step": rho_step,
        "association_smoothing_alpha": association_smoothing_alpha,
        "association_power": association_power,
        "association_half_life_days": association_half_life_days,
        "baseline_smoothing_alpha": 0.5,
        "seed_method": "empirical_association",
        "unknown_team_policy": "league_average",
    }
    fit_configuration_matches_promoted = _fit_config_is_promoted(fit_config)
    bootstrap_configuration_matches_promoted = (
        bootstrap_repetitions == DEFAULT_BOOTSTRAP_REPETITIONS
        and bootstrap_seed == BOOTSTRAP_SEED
    )
    promoted_run = (
        fit_configuration_matches_promoted and bootstrap_configuration_matches_promoted
    )
    if not promoted_run and experimental_override is not True:
        raise HoldoutEvaluationError(
            "non-promoted fit or bootstrap settings require experimental_override=True"
        )
    run_splits = _splits_for_run(promoted_run=promoted_run)
    leagues_value = manifest["leagues"]
    league_keys: set[str] = set()
    league_results: list[dict[str, Any]] = []
    all_accumulators: list[dict[str, Any]] = []
    promotion_eligible_accumulators: list[dict[str, Any]] = []
    promotion_eligible_cohorts: list[dict[str, str]] = []
    research_shadow_cohorts: list[dict[str, str]] = []
    by_split: dict[str, list[dict[str, Any]]] = {
        split["split_id"]: [] for split in run_splits
    }
    for league_manifest in sorted(
        leagues_value, key=lambda item: str(item.get("league_key", ""))
    ):
        if not isinstance(league_manifest, Mapping):
            raise HoldoutEvaluationError("manifest league entry must be an object")
        league_key = league_manifest.get("league_key")
        if not isinstance(league_key, str) or not league_key.strip():
            raise HoldoutEvaluationError("manifest league_key is required")
        if league_key in league_keys:
            raise HoldoutEvaluationError(f"duplicate manifest league: {league_key}")
        league_keys.add(league_key)
        score_metadata = league_manifest.get("score_dataset")
        if not isinstance(score_metadata, Mapping):
            raise HoldoutEvaluationError(f"{league_key}: score_dataset is missing")
        score_path = _safe_bundle_file(
            root, score_metadata.get("file"), f"{league_key}.score_dataset.file"
        )
        score_hash = _require_hash(
            score_metadata.get("sha256"), f"{league_key}.score_dataset.sha256"
        )
        score_rows = _load_score_rows(
            score_path,
            league_key=league_key,
            expected_hash=score_hash,
            expected_rows=_parse_int(
                score_metadata.get("rows"), f"{league_key}.score_dataset.rows"
            ),
        )
        opening_consensus = None
        market_hash = None
        if include_opening_market:
            market_metadata = league_manifest.get("opening_market_research")
            if not isinstance(market_metadata, Mapping):
                raise HoldoutEvaluationError(
                    f"{league_key}: opening_market_research is missing"
                )
            if market_metadata.get("policy") != RESEARCH_MARKET_POLICY:
                raise HoldoutEvaluationError(
                    f"{league_key}: opening-market policy is not research-only"
                )
            market_path = _safe_bundle_file(
                root,
                market_metadata.get("file"),
                f"{league_key}.opening_market_research.file",
            )
            market_hash = _require_hash(
                market_metadata.get("sha256"),
                f"{league_key}.opening_market_research.sha256",
            )
            opening_consensus = _load_opening_consensus(
                market_path,
                league_key=league_key,
                expected_hash=market_hash,
            )

        split_results: list[dict[str, Any]] = []
        league_accumulators: list[dict[str, Any]] = []
        league_promotion_accumulators: list[dict[str, Any]] = []
        league_promotion_cohorts: list[dict[str, str]] = []
        league_research_shadow_cohorts: list[dict[str, str]] = []
        for split in run_splits:
            split_result, accumulators = _fit_and_score_split(
                score_rows,
                split,
                league_key=league_key,
                manifest_hash=manifest_hash,
                opening_consensus=opening_consensus,
                fit_config=fit_config,
                bootstrap_repetitions=bootstrap_repetitions,
                bootstrap_seed=bootstrap_seed,
            )
            split_results.append(split_result)
            league_accumulators.append(accumulators)
            all_accumulators.append(accumulators)
            by_split[split["split_id"]].append(accumulators)
            cohort = {"league_key": league_key, "split_id": split["split_id"]}
            if split_result["evaluation_scope"][
                "component_promotion_evidence_included"
            ]:
                league_promotion_accumulators.append(accumulators)
                league_promotion_cohorts.append(cohort)
                promotion_eligible_accumulators.append(accumulators)
                promotion_eligible_cohorts.append(cohort)
            else:
                league_research_shadow_cohorts.append(cohort)
                research_shadow_cohorts.append(cohort)
        league_results.append(
            {
                "league_key": league_key,
                "league": league_manifest.get("league"),
                "score_dataset": {
                    "file": score_metadata["file"],
                    "sha256": score_hash,
                    "rows": len(score_rows),
                },
                "opening_market_research": {
                    "enabled": include_opening_market,
                    "sha256": market_hash,
                    "policy": RESEARCH_MARKET_POLICY,
                    "collection_time_status": "unavailable_in_source",
                },
                "splits": split_results,
                "summary": _aggregate_split_cohorts(
                    league_accumulators,
                    bootstrap_repetitions=bootstrap_repetitions,
                    bootstrap_seed=bootstrap_seed,
                ),
                "promotion_evidence": _promotion_evidence_summary(
                    eligible_cohorts=league_promotion_cohorts,
                    research_shadow_cohorts=league_research_shadow_cohorts,
                    eligible_accumulators=league_promotion_accumulators,
                    bootstrap_repetitions=bootstrap_repetitions,
                    bootstrap_seed=bootstrap_seed,
                ),
            }
        )

    artifact: dict[str, Any] = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "formal_htft_eligible": False,
        "complete_prekickoff_nine_way_htft_odds_available": False,
        "ev_roi_evaluation_available": False,
        "evaluation_scope": "nine_class_probability_accuracy_only",
        "dataset": {
            "manifest_file": manifest_file.name,
            "manifest_bundle_hash": manifest_hash,
            "manifest_schema_version": manifest.get("schema_version"),
            "importer_version": manifest.get("importer_version"),
        },
        "split_policy": {
            "method": "fixed_league_season_holdouts",
            "random_split": False,
            "training_rule": "season <= training_season_max",
            "test_rule": "season == test_season",
            "competition_regime_rule": "competition-specific frozen allowlist",
            "strict_date_rule": "max(training date) < min(test date)",
            "splits": [dict(split) for split in run_splits],
        },
        "competition_regime_policy": copy.deepcopy(COMPETITION_REGIME_POLICY),
        "fit_config": fit_config,
        "promotion": _promotion_metadata(
            fit_configuration_matches_promoted=fit_configuration_matches_promoted,
            bootstrap_configuration_matches_promoted=(
                bootstrap_configuration_matches_promoted
            ),
            competition_regime_policy_matches_manager=True,
        ),
        "bootstrap_config": {
            "repetitions": bootstrap_repetitions,
            "seed": bootstrap_seed,
            "confidence_level": 0.95,
            "paired": True,
            "stratified_by_league": True,
        },
        "market_research_policy": {
            "enabled": include_opening_market,
            "policy": RESEARCH_MARKET_POLICY,
            "research_only": True,
            "collection_time_status": "unavailable_in_source",
            "official_anchor_interface_used": False,
            "per_bookmaker_de_vig": "normalized inverse decimal odds",
            "consensus": "arithmetic mean across complete bookmakers",
            "joint_update": "prediction raw joint seed plus evaluator-local IPF",
            "production_eligibility": False,
            "complete_prekickoff_nine_way_htft_odds_available": False,
            "ev_roi_evaluation_available": False,
        },
        "metric_definitions": {
            "nine_class_log_loss": "mean negative log probability of observed HT/FT class",
            "nine_class_brier": "mean sum of squared errors across all nine classes",
            "top_one_accuracy": "observed class equals deterministic probability rank 1",
            "top_two_accuracy": "observed class is in deterministic probability Top 2",
            "weighted_summary": "all means and rates use match-count denominators",
            "model_pair_gate": MODEL_PAIR_MASS_THRESHOLD,
            "research_market_pair_gate": MARKET_PAIR_MASS_THRESHOLD,
            "evaluation_scope": (
                "classification probability accuracy only; no executable HT/FT "
                "EV or ROI without complete timestamped pre-kickoff nine-way odds"
            ),
        },
        "leagues": league_results,
        "summary": {
            "by_split": {
                split_id: _aggregate_split_cohorts(
                    accumulators,
                    bootstrap_repetitions=bootstrap_repetitions,
                    bootstrap_seed=bootstrap_seed,
                )
                for split_id, accumulators in by_split.items()
            },
            "all_splits": _aggregate_split_cohorts(
                all_accumulators,
                bootstrap_repetitions=bootstrap_repetitions,
                bootstrap_seed=bootstrap_seed,
            ),
        },
        "promotion_evidence": _promotion_evidence_summary(
            eligible_cohorts=promotion_eligible_cohorts,
            research_shadow_cohorts=research_shadow_cohorts,
            eligible_accumulators=promotion_eligible_accumulators,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
    }
    artifact["evaluation_hash"] = calculate_evaluation_hash(artifact)
    validate_evaluation(artifact, manifest_path=manifest_file)
    if output_path is not None:
        save_evaluation(artifact, output_path)
    return artifact


def _require_same(actual: Any, expected: Any, name: str) -> None:
    if _canonical_hash(actual) != _canonical_hash(expected):
        raise HoldoutEvaluationError(f"{name} does not match forecast evidence")


def _contains_key(value: Any, forbidden: set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(
            key in forbidden or _contains_key(item, forbidden)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def _rebuild_split_evidence(
    split: Mapping[str, Any],
    *,
    league_key: str,
    bootstrap_repetitions: int,
    bootstrap_seed: int,
    expected_splits: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    forecasts = split.get("forecasts")
    if not isinstance(forecasts, list):
        raise HoldoutEvaluationError(f"{league_key} split forecasts must be a list")
    model_cohorts = _cohort_accumulators(MODEL_PAIR_MASS_THRESHOLD)
    market_cohorts = _cohort_accumulators(MARKET_PAIR_MASS_THRESHOLD)
    baseline_accumulator = _new_accumulator(MODEL_PAIR_MASS_THRESHOLD)
    paired_cohorts = _new_paired_cohorts()
    paired_accumulator = paired_cohorts["overall"]
    context_slices = _new_context_slice_accumulators()
    expected_split = next(
        (item for item in expected_splits if item["split_id"] == split.get("split_id")),
        None,
    )
    if expected_split is None:
        raise HoldoutEvaluationError(f"{league_key}: unknown split_id")
    for name, value in expected_split.items():
        if split.get(name) != value:
            raise HoldoutEvaluationError(
                f"{league_key} {expected_split['split_id']}: fixed split metadata changed"
            )
    if split.get("test_match_count") != len(forecasts):
        raise HoldoutEvaluationError(
            f"{league_key} {expected_split['split_id']}: test count mismatch"
        )
    training_regime_counts = _validated_declared_regime_counts(
        split.get("training_competition_regime_counts"),
        "training_competition_regime_counts",
    )
    formal_regimes = _formal_competition_regimes(league_key)
    if not set(training_regime_counts).issubset(formal_regimes):
        raise HoldoutEvaluationError(
            "formal training competition regime counts include an excluded regime"
        )
    if sum(training_regime_counts.values()) != split.get("training_match_count"):
        raise HoldoutEvaluationError("training competition regime counts changed")
    declared_test_regime_counts = split.get("test_competition_regime_counts")
    if declared_test_regime_counts == {}:
        test_regime_counts: dict[str, int] = {}
    else:
        test_regime_counts = _validated_declared_regime_counts(
            declared_test_regime_counts,
            "test_competition_regime_counts",
        )
    if sum(test_regime_counts.values()) != len(forecasts):
        raise HoldoutEvaluationError("test competition regime counts changed")
    if not set(test_regime_counts).issubset(formal_regimes):
        raise HoldoutEvaluationError(
            "formal test competition regime counts include an excluded regime"
        )
    excluded_training_regime_counts = _validated_declared_regime_counts(
        split.get("excluded_training_competition_regime_counts"),
        "excluded_training_competition_regime_counts",
    )
    excluded_test_regime_counts = _validated_declared_regime_counts(
        split.get("excluded_test_competition_regime_counts"),
        "excluded_test_competition_regime_counts",
    )
    if (
        set(excluded_training_regime_counts) & formal_regimes
        or set(excluded_test_regime_counts) & formal_regimes
    ):
        raise HoldoutEvaluationError(
            "excluded competition regime counts contain a formal regime"
        )
    if sum(excluded_training_regime_counts.values()) != split.get(
        "excluded_training_match_count"
    ):
        raise HoldoutEvaluationError("excluded training regime counts changed")
    if sum(excluded_test_regime_counts.values()) != split.get(
        "excluded_test_match_count"
    ):
        raise HoldoutEvaluationError("excluded test regime counts changed")
    if not forecasts:
        if split.get("status") != "not_available":
            raise HoldoutEvaluationError(
                f"{league_key} {expected_split['split_id']}: empty split status is invalid"
            )
        expected_baseline = {
            "method": "dirichlet_smoothed_training_htft_frequency",
            "smoothing_alpha": split["league_empirical_frequency_baseline"][
                "smoothing_alpha"
            ],
            "training_class_counts": None,
            "probabilities": None,
            "metrics": _finalize_accumulator(baseline_accumulator)["metrics"],
        }
        _require_same(
            split.get("model_only"),
            _finalize_cohorts(model_cohorts),
            f"{league_key} empty model metrics",
        )
        _require_same(
            split.get("league_empirical_frequency_baseline"),
            expected_baseline,
            f"{league_key} empty baseline metrics",
        )
        _require_same(
            split.get("model_minus_empirical_baseline"),
            _finalize_paired_accumulator(
                paired_accumulator,
                bootstrap_repetitions=bootstrap_repetitions,
                bootstrap_seed=bootstrap_seed,
            ),
            f"{league_key} empty paired metrics",
        )
        _require_same(
            split.get("model_minus_empirical_baseline_by_team_availability"),
            _finalize_paired_cohorts(
                paired_cohorts,
                bootstrap_repetitions=bootstrap_repetitions,
                bootstrap_seed=bootstrap_seed,
            ),
            f"{league_key} empty paired cohort metrics",
        )
        _require_same(
            split.get("evaluation_scope"),
            _split_evaluation_scope([], expected_split),
            f"{league_key} empty evaluation scope",
        )
        _require_same(
            split.get("context_slices"),
            _finalize_context_slices(
                context_slices,
                bootstrap_repetitions=bootstrap_repetitions,
                bootstrap_seed=bootstrap_seed,
            ),
            f"{league_key} empty context slices",
        )
        return {
            "model": model_cohorts,
            "market": market_cohorts,
            "baseline": baseline_accumulator,
            "paired": paired_accumulator,
            "context_slices": context_slices,
        }

    if split.get("status") != "evaluated":
        raise HoldoutEvaluationError(
            f"{league_key} {expected_split['split_id']}: non-empty split is not evaluated"
        )
    model_hash = _require_hash(split.get("model_hash"), "split.model_hash")
    _require_hash(
        split.get("model_training_data_hash"), "split.model_training_data_hash"
    )
    if split.get("unknown_team_policy") != "league_average":
        raise HoldoutEvaluationError("split unknown_team_policy must be league_average")
    training_cutoff = _parse_date(
        split.get("training_cutoff_date"), "split.training_cutoff_date"
    )
    baseline_metadata = split.get("league_empirical_frequency_baseline")
    if not isinstance(baseline_metadata, Mapping):
        raise HoldoutEvaluationError("split empirical baseline is missing")
    alpha = float(baseline_metadata.get("smoothing_alpha"))
    counts = baseline_metadata.get("training_class_counts")
    if not isinstance(counts, Mapping) or set(counts) != set(htft_model.HTFT_CLASSES):
        raise HoldoutEvaluationError("baseline training_class_counts are invalid")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    ):
        raise HoldoutEvaluationError(
            "baseline class counts must be non-negative integers"
        )
    if sum(counts.values()) != split.get("training_match_count"):
        raise HoldoutEvaluationError(
            "baseline class counts do not match training count"
        )
    denominator = sum(counts.values()) + alpha * len(htft_model.HTFT_CLASSES)
    expected_baseline_probabilities = {
        name: (counts[name] + alpha) / denominator for name in htft_model.HTFT_CLASSES
    }
    _require_same(
        baseline_metadata.get("probabilities"),
        expected_baseline_probabilities,
        f"{league_key} baseline probabilities",
    )
    fallback_count = 0
    bookmaker_counts: list[int] = []
    forecast_dates: list[date] = []
    observed_test_regimes: dict[str, int] = {}
    for index, forecast in enumerate(forecasts):
        if not isinstance(forecast, Mapping):
            raise HoldoutEvaluationError("forecast must contain an object")
        if forecast.get("season") != expected_split["test_season"]:
            raise HoldoutEvaluationError("forecast is outside its fixed test season")
        match_date = _parse_date(forecast.get("date"), "forecast.date")
        kickoff = _canonical_utc(forecast.get("kickoff_utc"), "forecast.kickoff_utc")
        if datetime.fromisoformat(kickoff.replace("Z", "+00:00")).date() != match_date:
            raise HoldoutEvaluationError("forecast date and kickoff disagree")
        if training_cutoff >= match_date:
            raise HoldoutEvaluationError("forecast training cutoff leaked into holdout")
        if forecast.get("training_cutoff_date") != training_cutoff.isoformat():
            raise HoldoutEvaluationError("forecast training cutoff metadata changed")
        if forecast.get("model_hash") != model_hash:
            raise HoldoutEvaluationError("forecast model hash changed within split")
        fallback = forecast.get("used_league_average_fallback")
        if not isinstance(fallback, bool):
            raise HoldoutEvaluationError("forecast fallback flag must be boolean")
        fallback_count += int(fallback)
        actual_class = forecast.get("actual_class")
        if actual_class not in htft_model.HTFT_CLASSES:
            raise HoldoutEvaluationError("forecast actual_class is invalid")
        competition_regime = forecast.get("competition_regime")
        if competition_regime not in formal_regimes:
            raise HoldoutEvaluationError(
                "forecast competition_regime is not production-eligible"
            )
        observed_test_regimes[competition_regime] = (
            observed_test_regimes.get(competition_regime, 0) + 1
        )
        _season_status = _optional_context_value(
            forecast.get("season_status"),
            default=LEGACY_SEASON_STATUS,
            name="forecast.season_status",
        )
        format_version = _optional_context_value(
            forecast.get("format_version"),
            default=LEGACY_FORMAT_VERSION,
            name="forecast.format_version",
        )
        phase_group = _optional_context_value(
            forecast.get("phase_group"),
            default=LEGACY_PHASE_GROUP,
            name="forecast.phase_group",
        )
        model_probabilities = _validate_probability_vector(
            forecast.get("model_probabilities")
        )
        baseline_probabilities = _validate_probability_vector(
            forecast.get("empirical_baseline_probabilities")
        )
        _require_same(
            baseline_probabilities,
            expected_baseline_probabilities,
            f"{league_key} forecast {index} baseline",
        )
        ranked = sorted(
            htft_model.HTFT_CLASSES,
            key=lambda name: (
                -model_probabilities[name],
                htft_model.HTFT_CLASSES.index(name),
            ),
        )
        expected_top_two = [
            {"class": name, "probability": model_probabilities[name]}
            for name in ranked[:2]
        ]
        _require_same(
            forecast.get("model_top_two"),
            expected_top_two,
            f"{league_key} forecast {index} Top 2",
        )
        expected_pair_mass = math.fsum(model_probabilities[name] for name in ranked[:2])
        if abs(float(forecast.get("model_pair_mass")) - expected_pair_mass) > 1e-12:
            raise HoldoutEvaluationError("forecast model_pair_mass is incorrect")
        _add_cohort_score(
            model_cohorts,
            model_probabilities,
            actual_class,
            used_fallback=fallback,
        )
        _add_score(baseline_accumulator, baseline_probabilities, actual_class)
        _add_paired_score(
            paired_accumulator,
            model_probabilities,
            baseline_probabilities,
            actual_class,
            group=league_key,
        )
        cohort = "league_average_fallback" if fallback else "known_teams"
        _add_paired_score(
            paired_cohorts[cohort],
            model_probabilities,
            baseline_probabilities,
            actual_class,
            group=league_key,
        )
        _add_context_slice_score(
            context_slices,
            format_version=format_version,
            phase_group=phase_group,
            model_probabilities=model_probabilities,
            baseline_probabilities=baseline_probabilities,
            actual_class=actual_class,
            used_fallback=fallback,
            group=league_key,
        )
        research = forecast.get("research_opening_market")
        if research is not None:
            if not isinstance(research, Mapping):
                raise HoldoutEvaluationError(
                    "research forecast must be an object or null"
                )
            if research.get("policy") != RESEARCH_MARKET_POLICY:
                raise HoldoutEvaluationError("research forecast policy is invalid")
            research_probabilities = _validate_probability_vector(
                research.get("probabilities")
            )
            research_ranked = sorted(
                htft_model.HTFT_CLASSES,
                key=lambda name: (
                    -research_probabilities[name],
                    htft_model.HTFT_CLASSES.index(name),
                ),
            )
            _require_same(
                research.get("top_two"),
                research_ranked[:2],
                f"{league_key} forecast {index} research Top 2",
            )
            expected_research_mass = math.fsum(
                research_probabilities[name] for name in research_ranked[:2]
            )
            if abs(float(research.get("pair_mass")) - expected_research_mass) > 1e-12:
                raise HoldoutEvaluationError("research pair_mass is incorrect")
            bookmaker_count = research.get("bookmaker_count")
            bookmakers = research.get("bookmakers")
            if (
                isinstance(bookmaker_count, bool)
                or not isinstance(bookmaker_count, int)
                or bookmaker_count < 1
                or not isinstance(bookmakers, list)
                or len(bookmakers) != bookmaker_count
            ):
                raise HoldoutEvaluationError("research bookmaker audit is invalid")
            bookmaker_counts.append(bookmaker_count)
            _add_cohort_score(
                market_cohorts,
                research_probabilities,
                actual_class,
                used_fallback=fallback,
            )
        forecast_dates.append(match_date)

    if fallback_count != split.get("fallback_fixture_count"):
        raise HoldoutEvaluationError("split fallback count does not match forecasts")
    if dict(sorted(observed_test_regimes.items())) != test_regime_counts:
        raise HoldoutEvaluationError("forecast competition regime counts changed")
    if min(forecast_dates).isoformat() != split.get("test_date_start"):
        raise HoldoutEvaluationError("split test_date_start does not match forecasts")
    if max(forecast_dates).isoformat() != split.get("test_date_end"):
        raise HoldoutEvaluationError("split test_date_end does not match forecasts")
    if split.get("strict_cutoff_verified") is not True:
        raise HoldoutEvaluationError("split strict_cutoff_verified must be true")
    _require_same(
        split.get("evaluation_scope"),
        _split_evaluation_scope(forecasts, expected_split),
        f"{league_key} {expected_split['split_id']} evaluation scope",
    )
    _require_same(
        split.get("model_only"),
        _finalize_cohorts(model_cohorts),
        f"{league_key} {expected_split['split_id']} model metrics",
    )
    expected_baseline_metrics = _finalize_accumulator(baseline_accumulator)["metrics"]
    _require_same(
        baseline_metadata.get("metrics"),
        expected_baseline_metrics,
        f"{league_key} {expected_split['split_id']} baseline metrics",
    )
    _require_same(
        split.get("model_minus_empirical_baseline"),
        _finalize_paired_accumulator(
            paired_accumulator,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
        f"{league_key} {expected_split['split_id']} paired deltas",
    )
    _require_same(
        split.get("model_minus_empirical_baseline_by_team_availability"),
        _finalize_paired_cohorts(
            paired_cohorts,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
        f"{league_key} {expected_split['split_id']} paired cohort deltas",
    )
    _require_same(
        split.get("context_slices"),
        _finalize_context_slices(
            context_slices,
            bootstrap_repetitions=bootstrap_repetitions,
            bootstrap_seed=bootstrap_seed,
        ),
        f"{league_key} {expected_split['split_id']} context slices",
    )
    research_summary = split.get("research_opening_market")
    if not isinstance(research_summary, Mapping):
        raise HoldoutEvaluationError("split research summary is missing")
    if (
        research_summary.get("policy") != RESEARCH_MARKET_POLICY
        or research_summary.get("research_only") is not True
        or research_summary.get("collection_time_status") != "unavailable_in_source"
        or research_summary.get("official_anchor_interface_used") is not False
    ):
        raise HoldoutEvaluationError("split research policy was weakened")
    _require_same(
        research_summary.get("cohorts"),
        _finalize_cohorts(market_cohorts),
        f"{league_key} {expected_split['split_id']} research metrics",
    )
    anchor_count = len(bookmaker_counts)
    if research_summary.get("anchor_available_count") != anchor_count:
        raise HoldoutEvaluationError("research anchor count does not match forecasts")
    expected_availability = anchor_count / len(forecasts)
    if (
        abs(float(research_summary.get("anchor_availability")) - expected_availability)
        > 1e-12
    ):
        raise HoldoutEvaluationError("research anchor availability is incorrect")
    expected_bookmaker_summary = {
        "minimum": min(bookmaker_counts) if bookmaker_counts else None,
        "maximum": max(bookmaker_counts) if bookmaker_counts else None,
        "mean": (
            math.fsum(bookmaker_counts) / len(bookmaker_counts)
            if bookmaker_counts
            else None
        ),
    }
    _require_same(
        research_summary.get("bookmaker_count"),
        expected_bookmaker_summary,
        f"{league_key} bookmaker summary",
    )
    return {
        "model": model_cohorts,
        "market": market_cohorts,
        "baseline": baseline_accumulator,
        "paired": paired_accumulator,
        "context_slices": context_slices,
    }


def _verify_source_binding(
    evaluation: Mapping[str, Any],
    *,
    dataset_dir: str | Path | None,
    manifest_path: str | Path | None,
    expected_splits: Sequence[Mapping[str, Any]],
) -> None:
    root, manifest_file, manifest = _resolve_manifest(dataset_dir, manifest_path)
    dataset = evaluation["dataset"]
    if dataset.get("manifest_file") != manifest_file.name:
        raise HoldoutEvaluationError("evaluation manifest filename binding changed")
    if dataset.get("manifest_bundle_hash") != manifest.get("bundle_hash"):
        raise HoldoutEvaluationError("evaluation is not bound to this manifest")
    if dataset.get("manifest_schema_version") != manifest.get("schema_version"):
        raise HoldoutEvaluationError("manifest schema version binding changed")
    if dataset.get("importer_version") != manifest.get("importer_version"):
        raise HoldoutEvaluationError("manifest importer version binding changed")

    manifest_leagues = {
        item.get("league_key"): item
        for item in manifest["leagues"]
        if isinstance(item, Mapping)
    }
    evaluation_leagues = {
        item.get("league_key"): item
        for item in evaluation["leagues"]
        if isinstance(item, Mapping)
    }
    if set(manifest_leagues) != set(evaluation_leagues):
        raise HoldoutEvaluationError("evaluation league set differs from manifest")

    for league_key in sorted(manifest_leagues):
        manifest_league = manifest_leagues[league_key]
        evaluation_league = evaluation_leagues[league_key]
        manifest_score = manifest_league.get("score_dataset")
        evaluation_score = evaluation_league.get("score_dataset")
        if not isinstance(manifest_score, Mapping) or not isinstance(
            evaluation_score, Mapping
        ):
            raise HoldoutEvaluationError("score dataset binding metadata is missing")
        expected_score_binding = {
            "file": manifest_score.get("file"),
            "sha256": manifest_score.get("sha256"),
            "rows": manifest_score.get("rows"),
        }
        _require_same(
            evaluation_score,
            expected_score_binding,
            f"{league_key} score source binding",
        )
        score_path = _safe_bundle_file(
            root,
            manifest_score.get("file"),
            f"{league_key}.score_dataset.file",
        )
        score_rows = _load_score_rows(
            score_path,
            league_key=str(league_key),
            expected_hash=_require_hash(
                manifest_score.get("sha256"),
                f"{league_key}.score_dataset.sha256",
            ),
            expected_rows=_parse_int(
                manifest_score.get("rows"),
                f"{league_key}.score_dataset.rows",
            ),
        )
        evaluation_splits = evaluation_league.get("splits")
        if not isinstance(evaluation_splits, list):
            raise HoldoutEvaluationError("evaluation split binding is missing")
        for expected_split, split in zip(
            expected_splits, evaluation_splits, strict=True
        ):
            training_candidates = [
                row
                for row in score_rows
                if row["season"] <= expected_split["training_season_max"]
            ]
            test_candidates = [
                row
                for row in score_rows
                if row["season"] == expected_split["test_season"]
            ]
            training_rows, excluded_training_rows = _partition_formal_regimes(
                training_candidates, league_key=str(league_key)
            )
            test_rows, excluded_test_rows = _partition_formal_regimes(
                test_candidates, league_key=str(league_key)
            )
            if split.get("training_match_count") != len(training_rows):
                raise HoldoutEvaluationError(
                    f"{league_key} source-bound training count changed"
                )
            if split.get("test_match_count") != len(test_rows):
                raise HoldoutEvaluationError(
                    f"{league_key} source-bound test count changed"
                )
            _require_same(
                split.get("training_competition_regime_counts"),
                _competition_regime_counts(training_rows),
                f"{league_key} source-bound training regimes",
            )
            _require_same(
                split.get("test_competition_regime_counts"),
                _competition_regime_counts(test_rows),
                f"{league_key} source-bound test regimes",
            )
            if split.get("excluded_training_match_count") != len(
                excluded_training_rows
            ):
                raise HoldoutEvaluationError(
                    f"{league_key} source-bound excluded training count changed"
                )
            if split.get("excluded_test_match_count") != len(excluded_test_rows):
                raise HoldoutEvaluationError(
                    f"{league_key} source-bound excluded test count changed"
                )
            _require_same(
                split.get("excluded_training_competition_regime_counts"),
                _competition_regime_counts(excluded_training_rows),
                f"{league_key} source-bound excluded training regimes",
            )
            _require_same(
                split.get("excluded_test_competition_regime_counts"),
                _competition_regime_counts(excluded_test_rows),
                f"{league_key} source-bound excluded test regimes",
            )
            expected_training_class_counts = {
                name: 0 for name in htft_model.HTFT_CLASSES
            }
            for row in training_rows:
                expected_training_class_counts[row["actual_class"]] += 1
            baseline = split.get("league_empirical_frequency_baseline")
            if not isinstance(baseline, Mapping):
                raise HoldoutEvaluationError(
                    "source-bound baseline metadata is missing"
                )
            if test_rows:
                _require_same(
                    baseline.get("training_class_counts"),
                    expected_training_class_counts,
                    f"{league_key} source-bound training class counts",
                )
            known_teams = {row["home_team"] for row in training_rows} | {
                row["away_team"] for row in training_rows
            }
            source_evidence = [
                {
                    "season": row["season"],
                    "date": row["date"].isoformat(),
                    "kickoff_utc": row["kickoff_utc"],
                    "home_team": row["home_team"],
                    "away_team": row["away_team"],
                    "actual_class": row["actual_class"],
                    "competition_regime": row["competition_regime"],
                    "season_status": row["season_status"],
                    "format_version": row["format_version"],
                    "phase_group": row["phase_group"],
                    "used_league_average_fallback": (
                        row["home_team"] not in known_teams
                        or row["away_team"] not in known_teams
                    ),
                }
                for row in test_rows
            ]
            forecast_evidence = [
                {
                    "season": forecast.get("season"),
                    "date": forecast.get("date"),
                    "kickoff_utc": forecast.get("kickoff_utc"),
                    "home_team": forecast.get("home_team"),
                    "away_team": forecast.get("away_team"),
                    "actual_class": forecast.get("actual_class"),
                    "competition_regime": forecast.get("competition_regime"),
                    "season_status": forecast.get("season_status"),
                    "format_version": forecast.get("format_version"),
                    "phase_group": forecast.get("phase_group"),
                    "used_league_average_fallback": forecast.get(
                        "used_league_average_fallback"
                    ),
                }
                for forecast in split.get("forecasts", [])
            ]
            _require_same(
                forecast_evidence,
                source_evidence,
                f"{league_key} {expected_split['split_id']} source outcomes",
            )

            # A self-consistent evaluation JSON is not sufficient evidence: an
            # attacker could replace every probability and then recalculate all
            # derived metrics and the outer hash.  Refit the deterministic model
            # from the hash-bound source rows and reproduce every holdout
            # probability before accepting source-bound validation.
            if test_rows:
                if len(training_rows) < 2:
                    raise HoldoutEvaluationError(
                        f"{league_key} source-bound split has too little training data"
                    )
                training_end = max(row["date"] for row in training_rows)
                generated_at = training_end.isoformat() + "T23:59:59Z"
                fit_config = evaluation["fit_config"]
                with tempfile.TemporaryDirectory(
                    prefix=f"soccer-htft-source-verify-{league_key}-"
                ) as temporary:
                    training_path = Path(temporary) / "training.csv"
                    _write_training_csv(training_path, training_rows)
                    try:
                        reproduced_model = htft_model.fit_model(
                            training_path,
                            half_time_half_life_days=fit_config[
                                "half_time_half_life_days"
                            ],
                            second_half_half_life_days=fit_config[
                                "second_half_half_life_days"
                            ],
                            full_time_half_life_days=fit_config[
                                "full_time_half_life_days"
                            ],
                            iterations=fit_config["iterations"],
                            learning_rate=fit_config["learning_rate"],
                            regularization=fit_config["regularization"],
                            rho_min=fit_config["rho_min"],
                            rho_max=fit_config["rho_max"],
                            rho_step=fit_config["rho_step"],
                            association_smoothing_alpha=fit_config[
                                "association_smoothing_alpha"
                            ],
                            association_power=fit_config["association_power"],
                            association_half_life_days=fit_config[
                                "association_half_life_days"
                            ],
                            competition_key=str(league_key),
                            dataset_manifest_hash=evaluation["dataset"][
                                "manifest_bundle_hash"
                            ],
                        )
                    except htft_model.HTFTModelError as exc:
                        raise HoldoutEvaluationError(
                            f"{league_key} source-bound model cannot be reproduced: {exc}"
                        ) from exc
                reproduced_model = _historical_model_timestamp(
                    reproduced_model, generated_at
                )
                if split.get("model_hash") != reproduced_model.get("model_hash"):
                    raise HoldoutEvaluationError(
                        f"{league_key} source-bound model hash does not reproduce"
                    )
                if (
                    split.get("model_training_data_hash")
                    != reproduced_model["training"]["source_data_hash"]
                ):
                    raise HoldoutEvaluationError(
                        f"{league_key} source-bound training data hash does not reproduce"
                    )
                forecasts = split.get("forecasts")
                if not isinstance(forecasts, list) or len(forecasts) != len(test_rows):
                    raise HoldoutEvaluationError(
                        f"{league_key} source-bound forecast count changed"
                    )
                for row, forecast in zip(test_rows, forecasts, strict=True):
                    try:
                        reproduced_prediction = htft_model.predict_model(
                            reproduced_model,
                            row["home_team"],
                            row["away_team"],
                            kickoff=row["kickoff_utc"],
                            generated_at=generated_at,
                            unknown_team_policy="league_average",
                            seed_method="empirical_association",
                        )
                    except htft_model.HTFTModelError as exc:
                        raise HoldoutEvaluationError(
                            f"{league_key} source-bound prediction cannot be reproduced: {exc}"
                        ) from exc
                    _require_same(
                        forecast.get("model_probabilities"),
                        reproduced_prediction["htft"]["probabilities"],
                        (
                            f"{league_key} {expected_split['split_id']} "
                            f"{row['home_team']} vs {row['away_team']} model probabilities"
                        ),
                    )

        if evaluation["market_research_policy"].get("enabled") is True:
            manifest_market = manifest_league.get("opening_market_research")
            evaluation_market = evaluation_league.get("opening_market_research")
            if not isinstance(manifest_market, Mapping) or not isinstance(
                evaluation_market, Mapping
            ):
                raise HoldoutEvaluationError("opening market binding is missing")
            market_path = _safe_bundle_file(
                root,
                manifest_market.get("file"),
                f"{league_key}.opening_market_research.file",
            )
            market_hash = _require_hash(
                manifest_market.get("sha256"),
                f"{league_key}.opening_market_research.sha256",
            )
            if _file_hash(market_path) != market_hash:
                raise HoldoutEvaluationError("opening market source hash changed")
            if evaluation_market.get("sha256") != market_hash:
                raise HoldoutEvaluationError("opening market artifact binding changed")


def validate_evaluation(
    evaluation: Mapping[str, Any],
    *,
    verify_hash: bool = True,
    dataset_dir: str | Path | None = None,
    manifest_path: str | Path | None = None,
) -> None:
    """Recalculate metrics from fixture-level forecasts and verify the artifact."""

    if not isinstance(evaluation, Mapping):
        raise HoldoutEvaluationError("evaluation must contain an object")
    if evaluation.get("artifact_type") != ARTIFACT_TYPE:
        raise HoldoutEvaluationError("unexpected evaluation artifact_type")
    if evaluation.get("schema_version") != SCHEMA_VERSION:
        raise HoldoutEvaluationError("unsupported evaluation schema_version")
    if (
        evaluation.get("formal_htft_eligible") is not False
        or evaluation.get("complete_prekickoff_nine_way_htft_odds_available")
        is not False
        or evaluation.get("ev_roi_evaluation_available") is not False
        or evaluation.get("evaluation_scope") != "nine_class_probability_accuracy_only"
    ):
        raise HoldoutEvaluationError("formal HT/FT eligibility scope is invalid")
    if evaluation.get("evaluator_version") != EVALUATOR_VERSION:
        raise HoldoutEvaluationError("unsupported evaluator_version")
    dataset = evaluation.get("dataset")
    if not isinstance(dataset, Mapping):
        raise HoldoutEvaluationError("evaluation dataset metadata is missing")
    _require_hash(dataset.get("manifest_bundle_hash"), "dataset manifest hash")
    bootstrap = evaluation.get("bootstrap_config")
    if not isinstance(bootstrap, Mapping):
        raise HoldoutEvaluationError("bootstrap_config is missing")
    repetitions = bootstrap.get("repetitions")
    seed = bootstrap.get("seed")
    if (
        isinstance(repetitions, bool)
        or not isinstance(repetitions, int)
        or repetitions < 1
    ):
        raise HoldoutEvaluationError("bootstrap repetitions are invalid")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise HoldoutEvaluationError("bootstrap seed is invalid")
    fit_config = evaluation.get("fit_config")
    if not isinstance(fit_config, Mapping) or set(fit_config) != set(
        PROMOTED_FIT_CONFIG
    ):
        raise HoldoutEvaluationError("fit_config keys are invalid")
    for name in (
        "half_time_half_life_days",
        "second_half_half_life_days",
        "full_time_half_life_days",
        "learning_rate",
        "regularization",
        "rho_min",
        "rho_max",
        "rho_step",
        "association_smoothing_alpha",
        "association_power",
        "association_half_life_days",
        "baseline_smoothing_alpha",
    ):
        value = fit_config.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise HoldoutEvaluationError(f"fit_config.{name} is invalid")
    iterations = fit_config.get("iterations")
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations < 1
    ):
        raise HoldoutEvaluationError("fit_config.iterations is invalid")
    if fit_config["association_half_life_days"] <= 0.0:
        raise HoldoutEvaluationError(
            "fit_config.association_half_life_days must be positive"
        )
    if fit_config.get("seed_method") != "empirical_association":
        raise HoldoutEvaluationError("fit_config seed_method is invalid")
    if fit_config.get("unknown_team_policy") != "league_average":
        raise HoldoutEvaluationError("fit_config unknown_team_policy is invalid")
    fit_configuration_matches_promoted = _fit_config_is_promoted(fit_config)
    bootstrap_configuration_matches_promoted = (
        repetitions == DEFAULT_BOOTSTRAP_REPETITIONS and seed == BOOTSTRAP_SEED
    )
    promoted_run = (
        fit_configuration_matches_promoted and bootstrap_configuration_matches_promoted
    )
    declared_regime_policy = evaluation.get("competition_regime_policy")
    competition_regime_policy_matches_manager = isinstance(
        declared_regime_policy, Mapping
    ) and _canonical_hash(declared_regime_policy) == _canonical_hash(
        COMPETITION_REGIME_POLICY
    )
    _require_same(
        declared_regime_policy,
        COMPETITION_REGIME_POLICY,
        "competition regime policy",
    )
    _require_same(
        evaluation.get("promotion"),
        _promotion_metadata(
            fit_configuration_matches_promoted=fit_configuration_matches_promoted,
            bootstrap_configuration_matches_promoted=(
                bootstrap_configuration_matches_promoted
            ),
            competition_regime_policy_matches_manager=(
                competition_regime_policy_matches_manager
            ),
        ),
        "promotion metadata",
    )
    expected_splits = _splits_for_run(promoted_run=promoted_run)
    policy = evaluation.get("market_research_policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("policy") != RESEARCH_MARKET_POLICY
        or policy.get("research_only") is not True
        or policy.get("collection_time_status") != "unavailable_in_source"
        or policy.get("official_anchor_interface_used") is not False
        or policy.get("production_eligibility") is not False
        or policy.get("complete_prekickoff_nine_way_htft_odds_available") is not False
        or policy.get("ev_roi_evaluation_available") is not False
    ):
        raise HoldoutEvaluationError("research market policy was weakened")
    if _contains_key(evaluation, {"captured_at", "anchor_timestamp"}):
        raise HoldoutEvaluationError(
            "untimestamped research data contains a fake timestamp"
        )
    split_policy = evaluation.get("split_policy")
    if (
        not isinstance(split_policy, Mapping)
        or split_policy.get("splits") != [dict(item) for item in expected_splits]
        or split_policy.get("random_split") is not False
        or split_policy.get("competition_regime_rule")
        != "competition-specific frozen allowlist"
    ):
        raise HoldoutEvaluationError("fixed split policy is invalid")
    leagues = evaluation.get("leagues")
    if not isinstance(leagues, list) or not leagues:
        raise HoldoutEvaluationError("evaluation contains no leagues")
    all_accumulators: list[dict[str, Any]] = []
    promotion_eligible_accumulators: list[dict[str, Any]] = []
    promotion_eligible_cohorts: list[dict[str, str]] = []
    research_shadow_cohorts: list[dict[str, str]] = []
    by_split: dict[str, list[dict[str, Any]]] = {
        split["split_id"]: [] for split in expected_splits
    }
    observed_leagues: set[str] = set()
    for league in leagues:
        if not isinstance(league, Mapping):
            raise HoldoutEvaluationError("league evaluation must be an object")
        league_key = league.get("league_key")
        if (
            not isinstance(league_key, str)
            or not league_key
            or league_key in observed_leagues
        ):
            raise HoldoutEvaluationError("league_key is missing or duplicated")
        observed_leagues.add(league_key)
        score_dataset = league.get("score_dataset")
        if not isinstance(score_dataset, Mapping):
            raise HoldoutEvaluationError("league score_dataset metadata is missing")
        _require_hash(score_dataset.get("sha256"), "league score dataset hash")
        splits = league.get("splits")
        if not isinstance(splits, list) or [
            item.get("split_id") for item in splits
        ] != [item["split_id"] for item in expected_splits]:
            raise HoldoutEvaluationError("league fixed splits are missing or reordered")
        league_accumulators: list[dict[str, Any]] = []
        league_promotion_accumulators: list[dict[str, Any]] = []
        league_promotion_cohorts: list[dict[str, str]] = []
        league_research_shadow_cohorts: list[dict[str, str]] = []
        for split in splits:
            accumulators = _rebuild_split_evidence(
                split,
                league_key=league_key,
                bootstrap_repetitions=repetitions,
                bootstrap_seed=seed,
                expected_splits=expected_splits,
            )
            league_accumulators.append(accumulators)
            all_accumulators.append(accumulators)
            by_split[split["split_id"]].append(accumulators)
            cohort = {"league_key": league_key, "split_id": split["split_id"]}
            scope = split.get("evaluation_scope")
            if not isinstance(scope, Mapping):
                raise HoldoutEvaluationError("split evaluation_scope is missing")
            if scope.get("component_promotion_evidence_included") is True:
                league_promotion_accumulators.append(accumulators)
                league_promotion_cohorts.append(cohort)
                promotion_eligible_accumulators.append(accumulators)
                promotion_eligible_cohorts.append(cohort)
            else:
                league_research_shadow_cohorts.append(cohort)
                research_shadow_cohorts.append(cohort)
        _require_same(
            league.get("summary"),
            _aggregate_split_cohorts(
                league_accumulators,
                bootstrap_repetitions=repetitions,
                bootstrap_seed=seed,
            ),
            f"{league_key} weighted summary",
        )
        _require_same(
            league.get("promotion_evidence"),
            _promotion_evidence_summary(
                eligible_cohorts=league_promotion_cohorts,
                research_shadow_cohorts=league_research_shadow_cohorts,
                eligible_accumulators=league_promotion_accumulators,
                bootstrap_repetitions=repetitions,
                bootstrap_seed=seed,
            ),
            f"{league_key} promotion evidence",
        )
    expected_summary = {
        "by_split": {
            split_id: _aggregate_split_cohorts(
                accumulators,
                bootstrap_repetitions=repetitions,
                bootstrap_seed=seed,
            )
            for split_id, accumulators in by_split.items()
        },
        "all_splits": _aggregate_split_cohorts(
            all_accumulators,
            bootstrap_repetitions=repetitions,
            bootstrap_seed=seed,
        ),
    }
    _require_same(
        evaluation.get("summary"), expected_summary, "global weighted summary"
    )
    _require_same(
        evaluation.get("promotion_evidence"),
        _promotion_evidence_summary(
            eligible_cohorts=promotion_eligible_cohorts,
            research_shadow_cohorts=research_shadow_cohorts,
            eligible_accumulators=promotion_eligible_accumulators,
            bootstrap_repetitions=repetitions,
            bootstrap_seed=seed,
        ),
        "global promotion evidence",
    )
    if dataset_dir is not None or manifest_path is not None:
        _verify_source_binding(
            evaluation,
            dataset_dir=dataset_dir,
            manifest_path=manifest_path,
            expected_splits=expected_splits,
        )
    if verify_hash:
        stored_hash = evaluation.get("evaluation_hash")
        if not isinstance(stored_hash, str) or stored_hash != calculate_evaluation_hash(
            evaluation
        ):
            raise HoldoutEvaluationError("evaluation_hash does not match contents")


def save_evaluation(evaluation: Mapping[str, Any], path: str | Path) -> None:
    validate_evaluation(evaluation)
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            evaluation,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
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
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset-dir")
    source.add_argument("--manifest")
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--include-opening-market",
        action="store_true",
        help="run the explicitly research-only untimestamped opening-market experiment",
    )
    parser.add_argument("--iterations", type=int, default=1200)
    parser.add_argument("--learning-rate", type=float, default=0.03)
    parser.add_argument("--regularization", type=float, default=0.02)
    parser.add_argument("--rho-min", type=float, default=-0.20)
    parser.add_argument("--rho-max", type=float, default=0.20)
    parser.add_argument("--rho-step", type=float, default=0.01)
    parser.add_argument("--association-smoothing-alpha", type=float, default=0.5)
    parser.add_argument("--association-power", type=float, default=1.0)
    parser.add_argument("--association-half-life-days", type=float, default=365.0)
    parser.add_argument(
        "--bootstrap-repetitions",
        type=int,
        default=DEFAULT_BOOTSTRAP_REPETITIONS,
    )
    parser.add_argument("--bootstrap-seed", type=int, default=BOOTSTRAP_SEED)
    parser.add_argument(
        "--experimental-override",
        action="store_true",
        help=(
            "allow non-promoted fit/bootstrap settings; output is relabeled "
            "as configuration-experiment evidence"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        artifact = evaluate_bundle(
            dataset_dir=args.dataset_dir,
            manifest_path=args.manifest,
            output_path=args.output,
            include_opening_market=args.include_opening_market,
            iterations=args.iterations,
            learning_rate=args.learning_rate,
            regularization=args.regularization,
            rho_min=args.rho_min,
            rho_max=args.rho_max,
            rho_step=args.rho_step,
            association_smoothing_alpha=args.association_smoothing_alpha,
            association_power=args.association_power,
            association_half_life_days=args.association_half_life_days,
            bootstrap_repetitions=args.bootstrap_repetitions,
            bootstrap_seed=args.bootstrap_seed,
            experimental_override=args.experimental_override,
        )
    except HoldoutEvaluationError as exc:
        parser.exit(2, f"htft_holdout_evaluator: error: {exc}\n")
    print(
        json.dumps(
            {
                "output": str(Path(args.output).resolve()),
                "evaluation_hash": artifact["evaluation_hash"],
                "league_count": len(artifact["leagues"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

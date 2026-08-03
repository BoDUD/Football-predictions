#!/usr/bin/env python3
"""Register, verify, and use league-scoped corner-count models.

This manager is deliberately stricter than :mod:`corner_model`'s research CLI.
Every registry entry binds three immutable artifacts:

* the exact corner-history CSV (raw SHA-256),
* the final fitted model (semantic model hash plus serialized-file hash), and
* its chronological backtest (semantic backtest hash plus serialized-file hash).

Historical expanding-window evidence can only produce ``candidate`` or
``shadow`` status.  It can never make corner totals or corner handicaps a
formal recommendation.  A future manager version may add a separately bound,
strict live-forward evaluation, but this version fails closed instead of
accepting a caller-supplied production flag.
"""

from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping, Sequence

try:  # Imported from the repository root.
    from scripts import corner_history_dataset_builder, corner_model
except ImportError:  # Invoked directly as scripts/corner_model_manager.py.
    import corner_history_dataset_builder  # type: ignore[no-redef]
    import corner_model  # type: ignore[no-redef]


REGISTRY_ARTIFACT_TYPE = "soccer_corner_model_registry"
REGISTRY_SCHEMA_VERSION = "2.1.0"
REGISTRY_FILENAME = "corner-registry.json"
MANAGER_VERSION = "corner-model-manager/2.1.0"
DEPLOYMENT_POLICY_VERSION = "corner-historical-deployment-gate/2.1.0"

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
LEAGUE_KEY_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

METRIC_FIELDS = (
    "joint_log_loss",
    "home_log_loss",
    "away_log_loss",
    "total_crps",
    "margin_crps",
    "home_mae",
    "away_mae",
    "total_mae",
)
FIT_CONFIG_FIELDS = (
    "half_life_days",
    "iterations",
    "learning_rate",
    "regularization",
    "tail_tolerance",
    "hard_max_corners",
    "min_train_matches",
    "test_block_size",
)
MODEL_CONFIG_BINDINGS = {
    "half_life_days": "half_life_days",
    "iterations": "iterations",
    "learning_rate": "learning_rate",
    "regularization": "regularization",
}

# These gates decide only whether a historical artifact is suitable for the
# next live-forward stage.  They do not claim profitability or authorize a bet.
CANDIDATE_GATE = {
    "minimum_predictions": 20,
    "minimum_blocks": 5,
    "required_untouched_holdout": True,
    "maximum_unknown_exclusion_fraction": 0.25,
    "maximum_component_incomparable_exclusion_fraction": 0.25,
    "maximum_metrics": {
        "joint_log_loss": 8.0,
        "total_crps": 6.0,
        "margin_crps": 6.0,
        "total_mae": 8.0,
    },
    "required_baselines": list(corner_model.BASELINE_NAMES),
    "required_comparison_metrics": list(corner_model.COMPARISON_METRICS),
    "minimum_absolute_improvement": 1e-6,
    "minimum_relative_improvement": 0.005,
    "minimum_one_sided_95_lower_bound": 0.0,
    "uncertainty_method": "paired_one_sided_95_normal_approximation",
}
FORMAL_CORNER_INELIGIBLE_REASON = (
    "historical walk-forward evidence is model-development evidence only; "
    "formal corner markets require separately bound strict live-forward "
    "validation plus complete executable current-market evidence"
)
DEPLOYMENT_POLICY = {
    "version": DEPLOYMENT_POLICY_VERSION,
    "historical_evidence_scope": (
        "bounded_expanding_window_complete_utc_date_groups_v2"
    ),
    "allowed_historical_statuses": ["candidate", "shadow"],
    "candidate_gate": copy.deepcopy(CANDIDATE_GATE),
    "historical_backtest_can_authorize_production": False,
    "production_requires_separately_verified_live_forward_evidence": True,
    "formal_corner_total_eligible": False,
    "formal_corner_handicap_eligible": False,
    "formal_corner_ineligible_reason": FORMAL_CORNER_INELIGIBLE_REASON,
}

# Full source/model/backtest verification is intentionally expensive.  This
# process-local cache is keyed by hashes of every referenced artifact, so it
# only avoids repeating an identical audit within one trusted process.  It is
# never serialized and therefore cannot become a forgeable on-disk receipt.
_VERIFIED_REGISTRY_CACHE: dict[str, None] = {}
_VERIFIED_ENTRY_CACHE: dict[str, None] = {}
_VERIFIED_REGISTRY_CACHE_LIMIT = 16


class CornerModelManagerError(ValueError):
    """Raised when a registered corner artifact cannot be trusted."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_aware_datetime(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CornerModelManagerError(
                f"{name} must be an ISO-8601 datetime"
            ) from exc
    else:
        raise CornerModelManagerError(f"{name} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CornerModelManagerError(f"{name} needs an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def _canonical_datetime(value: Any, name: str) -> str:
    return _parse_aware_datetime(value, name).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


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
        raise CornerModelManagerError(
            "artifact contains values that cannot be hashed safely"
        ) from exc


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
        raise CornerModelManagerError(
            "artifact contains values that cannot be serialized safely"
        ) from exc


def _canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_hash(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise CornerModelManagerError(f"cannot read file: {path}") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _required_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise CornerModelManagerError(f"{name} must be a SHA-256 hash")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise CornerModelManagerError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CornerModelManagerError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise CornerModelManagerError(f"{name} must be finite")
    return number


def _positive(value: Any, name: str) -> float:
    number = _finite(value, name)
    if number <= 0.0:
        raise CornerModelManagerError(f"{name} must be greater than zero")
    return number


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CornerModelManagerError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result < 1:
        raise CornerModelManagerError(f"{name} must be a positive integer")
    return result


def _iso_date(value: Any, name: str) -> date:
    if not isinstance(value, str):
        raise CornerModelManagerError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise CornerModelManagerError(f"{name} must be a valid ISO date") from exc
    if parsed.isoformat() != value:
        raise CornerModelManagerError(f"{name} must be a canonical ISO date")
    return parsed


def _safe_filename(value: Any, name: str, *, suffix: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CornerModelManagerError(f"{name} is required")
    filename = value.strip()
    candidate = Path(filename)
    if (
        candidate.is_absolute()
        or candidate.name != filename
        or "/" in filename
        or "\\" in filename
        or not filename.lower().endswith(suffix.lower())
    ):
        raise CornerModelManagerError(
            f"{name} must be a local {suffix} filename"
        )
    return filename


def _read_json(path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CornerModelManagerError(f"cannot read {name}: {path}") from exc
    if not isinstance(value, dict):
        raise CornerModelManagerError(f"{name} must contain a JSON object")
    return value


def _atomic_bytes(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            "wb", delete=False, dir=path.parent, suffix=".tmp"
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        temporary.replace(path)
    except OSError as exc:
        raise CornerModelManagerError(f"cannot write artifact: {path}") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> str:
    return _atomic_bytes(path, _json_bytes(value))


def calculate_registry_hash(registry: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(registry))
    payload.pop("registry_hash", None)
    return _canonical_hash(payload)


def calculate_lineage_hash(entry: Mapping[str, Any]) -> str:
    """Hash the exact dataset/model/evaluation relationship for one league."""

    payload = {
        "league_key": entry.get("league_key"),
        "dataset_manifest_file": entry.get("dataset_manifest_file"),
        "dataset_manifest_file_sha256": entry.get("dataset_manifest_file_sha256"),
        "source_bundle_file": entry.get("source_bundle_file"),
        "source_file_sha256": entry.get("source_file_sha256"),
        "source_lineage": entry.get("source_lineage"),
        "dataset_hash": entry.get("dataset_hash"),
        "dataset_rows": entry.get("dataset_rows"),
        "dataset_profile": entry.get("dataset_profile"),
        "model_hash": entry.get("model_hash"),
        "model_version": entry.get("model_version"),
        "model_config": entry.get("model_config"),
        "training_cutoff": entry.get("training_cutoff"),
        "evaluation_hash": entry.get("evaluation_hash"),
        "backtest_hash": entry.get("backtest_hash"),
        "evaluation_config": entry.get("evaluation_config"),
    }
    return _canonical_hash(payload)


def _normalize_aliases(
    league_key: str, league: str, aliases: Iterable[str]
) -> list[str]:
    values: list[str] = []
    observed: set[str] = set()
    for raw in (league_key, league, *aliases):
        if not isinstance(raw, str) or not raw.strip():
            raise CornerModelManagerError("league aliases must be non-empty strings")
        value = raw.strip()
        folded = value.casefold()
        if folded not in observed:
            values.append(value)
            observed.add(folded)
    return values


def _validate_fit_config(config: Any, name: str) -> dict[str, Any]:
    if not isinstance(config, Mapping) or set(config) != set(FIT_CONFIG_FIELDS):
        raise CornerModelManagerError(
            f"{name} must contain the exact registered backtest configuration"
        )
    validated = dict(config)
    _positive(validated["half_life_days"], f"{name}.half_life_days")
    _positive_int(validated["iterations"], f"{name}.iterations")
    _positive(validated["learning_rate"], f"{name}.learning_rate")
    if (
        _finite(validated["regularization"], f"{name}.regularization")
        < corner_model.MIN_COMPONENT_REGULARIZATION
    ):
        raise CornerModelManagerError(
            f"{name}.regularization must be positive and at least 1e-8 for "
            "fixture-component identification"
        )
    tolerance = _positive(validated["tail_tolerance"], f"{name}.tail_tolerance")
    if tolerance >= 1.0:
        raise CornerModelManagerError(f"{name}.tail_tolerance must be below one")
    _positive_int(validated["hard_max_corners"], f"{name}.hard_max_corners")
    if _positive_int(validated["min_train_matches"], f"{name}.min_train_matches") < 2:
        raise CornerModelManagerError(f"{name}.min_train_matches must be at least two")
    _positive_int(validated["test_block_size"], f"{name}.test_block_size")
    return validated


def _manifest_bundle_hash(manifest: Mapping[str, Any]) -> str:
    return corner_history_dataset_builder.calculate_manifest_hash(manifest)


def _league_manifest_entry(
    manifest: Mapping[str, Any], league_key: str
) -> dict[str, Any]:
    leagues = manifest.get("leagues")
    if not isinstance(leagues, list):
        raise CornerModelManagerError("dataset manifest leagues must be a list")
    matches = [
        dict(item)
        for item in leagues
        if isinstance(item, Mapping) and item.get("league_key") == league_key
    ]
    if len(matches) != 1:
        raise CornerModelManagerError(
            f"dataset manifest must contain exactly one {league_key!r} league entry"
        )
    return matches[0]


def _dataset_lineage(
    *,
    manifest: Mapping[str, Any],
    manifest_file_sha256: str,
    league_entry: Mapping[str, Any],
    dataset_hash: str,
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "manifest_bundle_hash": _required_hash(
            manifest.get("bundle_hash"), "dataset manifest bundle_hash"
        ),
        "manifest_file_sha256": _required_hash(
            manifest_file_sha256, "dataset manifest file hash"
        ),
        "builder_version": manifest.get("builder_version"),
        "source_file_sha256": _required_hash(
            manifest.get("source_file_sha256"), "dataset source_file_sha256"
        ),
        "source_bundle_hash": _required_hash(
            manifest.get("source_bundle_hash"), "dataset source_bundle_hash"
        ),
        "selection_policy_hash": _canonical_hash(manifest.get("selection_policy")),
        "league_manifest_hash": _canonical_hash(dict(league_entry)),
        "source_competition_key": league_entry.get("source_competition_key"),
        "league_key": league_entry.get("league_key"),
        "dataset_hash": _required_hash(dataset_hash, "dataset hash"),
        "dataset_rows": profile.get("rows"),
        "semantic_rows_hash": profile.get("semantic_rows_hash"),
        "fixture_set_hash": profile.get("fixture_set_hash"),
        "response_set_hash": profile.get("response_set_hash"),
        "fixture_graph_hash": (
            profile.get("fixture_graph", {}).get("components_hash")
            if isinstance(profile.get("fixture_graph"), Mapping)
            else None
        ),
        "kickoff_utc_start": profile.get("kickoff_utc_start"),
        "kickoff_utc_end": profile.get("kickoff_utc_end"),
        "competition_regimes": copy.deepcopy(profile.get("competition_regimes")),
        "phases": copy.deepcopy(profile.get("phases")),
    }


def _validate_source_bound_dataset(
    dataset_path: Path,
    *,
    league_key: str,
    manifest_path: Path,
    source_bundle_path: Path | None = None,
    require_canonical_dataset_name: bool = True,
    replay_cache: dict[str, dict[str, Any]] | None = None,
    replay_all_leagues: bool = True,
) -> dict[str, Any]:
    """Re-open and reproduce a v2 builder manifest, source bundle, and CSV."""

    dataset_path = dataset_path.resolve()
    manifest_path = manifest_path.resolve()
    manifest = _read_json(manifest_path, "corner dataset manifest")
    if (
        manifest.get("artifact_type")
        != corner_history_dataset_builder.ARTIFACT_TYPE
        or manifest.get("schema_version")
        != corner_history_dataset_builder.SCHEMA_VERSION
        or manifest.get("builder_version")
        != corner_history_dataset_builder.BUILDER_VERSION
    ):
        raise CornerModelManagerError("dataset manifest is not the supported v2 builder artifact")
    stored_manifest_hash = _required_hash(
        manifest.get("bundle_hash"), "dataset manifest bundle_hash"
    )
    if stored_manifest_hash != _manifest_bundle_hash(manifest):
        raise CornerModelManagerError("dataset manifest bundle_hash does not match contents")
    if manifest.get("selection_policy") != corner_history_dataset_builder.SELECTION_POLICY:
        raise CornerModelManagerError(
            "dataset manifest selection_policy is not the installed safe policy"
        )
    _iso_date(manifest.get("as_of_date"), "dataset manifest as_of_date")
    source_name = _safe_filename(
        manifest.get("source_file"), "dataset manifest source_file", suffix=".json"
    )
    if source_name != corner_history_dataset_builder.SOURCE_COPY_FILENAME:
        raise CornerModelManagerError("dataset manifest source_file is not the v2 evidence copy")
    actual_source_path = (
        source_bundle_path.resolve()
        if source_bundle_path is not None
        else (manifest_path.parent / source_name).resolve()
    )
    source_file_hash = _required_hash(
        manifest.get("source_file_sha256"), "dataset manifest source_file_sha256"
    )
    if _file_hash(actual_source_path) != source_file_hash:
        raise CornerModelManagerError("dataset source bundle file hash does not match manifest")
    try:
        source_bundle = corner_history_dataset_builder.load_source(actual_source_path)
    except corner_history_dataset_builder.CornerDatasetError as exc:
        raise CornerModelManagerError(f"dataset source bundle is invalid: {exc}") from exc
    if source_bundle.get("bundle_hash") != manifest.get("source_bundle_hash"):
        raise CornerModelManagerError("dataset source bundle lineage does not match manifest")

    league_entry = _league_manifest_entry(manifest, league_key)
    expected_dataset_name = _safe_filename(
        league_entry.get("dataset_file"),
        "dataset manifest league dataset_file",
        suffix=".csv",
    )
    # At initial registration the builder CSV has its canonical name.  Stored
    # registries use a content-addressed filename and are accepted through the
    # explicit path only after all hashes and replay output agree.
    if require_canonical_dataset_name and dataset_path.name != expected_dataset_name:
        raise CornerModelManagerError("input CSV filename does not match dataset manifest")
    dataset_hash = _required_hash(
        league_entry.get("dataset_sha256"), "dataset manifest dataset_sha256"
    )
    if _file_hash(dataset_path) != dataset_hash:
        raise CornerModelManagerError("dataset CSV hash does not match manifest")
    try:
        records = corner_model.load_training_csv(dataset_path)
    except corner_model.CornerModelError as exc:
        raise CornerModelManagerError(f"source-bound dataset CSV is invalid: {exc}") from exc
    profile = corner_model.training_dataset_profile(records)
    if profile["league_key"] != league_key:
        raise CornerModelManagerError("dataset CSV league_key does not match requested league")
    if league_entry.get("rows") != len(records):
        raise CornerModelManagerError("dataset manifest row count does not match CSV")
    for manifest_field, profile_field in (
        ("kickoff_utc_start", "kickoff_utc_start"),
        ("kickoff_utc_end", "kickoff_utc_end"),
        ("fixture_set_hash", "fixture_set_hash"),
        ("response_set_hash", "response_set_hash"),
    ):
        manifest_value = league_entry.get(manifest_field)
        profile_value = profile.get(profile_field)
        matches = (
            _parse_aware_datetime(manifest_value, manifest_field)
            == _parse_aware_datetime(profile_value, profile_field)
            if manifest_field.startswith("kickoff_utc_")
            else manifest_value == profile_value
        )
        if not matches:
            raise CornerModelManagerError(
                f"dataset manifest {manifest_field} does not match CSV semantics"
            )
    regime_counts: dict[str, int] = {}
    phase_counts: dict[str, int] = {}
    for row in records:
        regime = str(row["competition_regime"])
        phase = str(row["phase"])
        regime_counts[regime] = regime_counts.get(regime, 0) + 1
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
    if league_entry.get("regimes") != dict(sorted(regime_counts.items())):
        raise CornerModelManagerError("dataset manifest regimes do not match CSV")
    if league_entry.get("phases") != dict(sorted(phase_counts.items())):
        raise CornerModelManagerError("dataset manifest phases do not match CSV")
    source_competition_key = str(league_entry.get("source_competition_key") or "")
    if (
        source_competition_key
        not in corner_history_dataset_builder.ELIGIBLE_REGIMES_BY_COMPETITION
        or source_competition_key
        not in corner_history_dataset_builder.ELIGIBLE_PHASES_BY_COMPETITION
    ):
        raise CornerModelManagerError(
            "dataset league source_competition_key is unsupported"
        )
    if (
        league_entry.get("selection_policy_version")
        != corner_history_dataset_builder.SELECTION_POLICY["version"]
        or league_entry.get("allowed_competition_regimes")
        != list(
            corner_history_dataset_builder.ELIGIBLE_REGIMES_BY_COMPETITION[
                source_competition_key
            ]
        )
        or league_entry.get("allowed_phases")
        != list(
            corner_history_dataset_builder.ELIGIBLE_PHASES_BY_COMPETITION[
                source_competition_key
            ]
        )
    ):
        raise CornerModelManagerError("dataset league selection policy binding is invalid")

    # Re-run the installed builder from the copied source.  A coordinated
    # manifest/CSV re-hash cannot bypass the actual v2 selection implementation.
    replay_key = _canonical_hash(
        {
            "builder_version": corner_history_dataset_builder.BUILDER_VERSION,
            "source_file_sha256": source_file_hash,
            "source_bundle_hash": source_bundle.get("bundle_hash"),
            "as_of_date": manifest.get("as_of_date"),
            "selection_policy": corner_history_dataset_builder.SELECTION_POLICY,
            "replay_scope": "all" if replay_all_leagues else league_key,
        }
    )
    replay_result = replay_cache.get(replay_key) if replay_cache is not None else None
    if replay_result is None:
        with tempfile.TemporaryDirectory() as temporary:
            replay_dir = Path(temporary)
            try:
                replay_manifest = corner_history_dataset_builder.build_dataset(
                    actual_source_path,
                    replay_dir,
                    as_of_date=str(manifest["as_of_date"]),
                    league_keys=None if replay_all_leagues else [league_key],
                )
            except corner_history_dataset_builder.CornerDatasetError as exc:
                raise CornerModelManagerError(
                    f"dataset builder replay failed: {exc}"
                ) from exc
            replay_result = {
                "manifest": replay_manifest,
                "dataset_hashes": {
                    str(item["league_key"]): _file_hash(
                        replay_dir / str(item["dataset_file"])
                    )
                    for item in replay_manifest["leagues"]
                },
            }
        if replay_cache is not None:
            replay_cache[replay_key] = replay_result
    replay_manifest = replay_result["manifest"]
    replay_entry = _league_manifest_entry(replay_manifest, league_key)
    if replay_result["dataset_hashes"].get(league_key) != dataset_hash:
        raise CornerModelManagerError("dataset CSV does not match builder replay")
    if replay_entry != league_entry:
        raise CornerModelManagerError("dataset league manifest does not match builder replay")
    if replay_all_leagues and replay_manifest.get("leagues") != manifest.get("leagues"):
        raise CornerModelManagerError(
            "dataset manifest league inventory does not match builder replay"
        )
    for field in (
        "artifact_type",
        "schema_version",
        "builder_version",
        "as_of_date",
        "source_file",
        "source_file_sha256",
        "source_bundle_hash",
        "selection_policy",
    ):
        if replay_manifest.get(field) != manifest.get(field):
            raise CornerModelManagerError(
                f"dataset manifest {field} does not match builder replay"
            )

    manifest_file_hash = _file_hash(manifest_path)
    lineage = _dataset_lineage(
        manifest=manifest,
        manifest_file_sha256=manifest_file_hash,
        league_entry=league_entry,
        dataset_hash=dataset_hash,
        profile=profile,
    )
    return {
        "manifest": manifest,
        "manifest_file_sha256": manifest_file_hash,
        "league_entry": league_entry,
        "source_bundle": source_bundle,
        "source_bundle_path": actual_source_path,
        "source_file_sha256": source_file_hash,
        "dataset_path": dataset_path,
        "dataset_hash": dataset_hash,
        "records": records,
        "profile": profile,
        "lineage": lineage,
    }


def _validate_backtest_structure_v1(
    backtest: Mapping[str, Any],
    *,
    dataset_hash: str | None = None,
    dataset_rows: int | None = None,
    dataset_records: Sequence[Mapping[str, Any]] | None = None,
    expected_config: Mapping[str, Any] | None = None,
) -> None:
    """Validate a chronological backtest and recompute its aggregate evidence."""

    if not isinstance(backtest, Mapping):
        raise CornerModelManagerError("backtest must be a JSON object")
    if backtest.get("artifact_type") != corner_model.BACKTEST_ARTIFACT_TYPE:
        raise CornerModelManagerError("unexpected backtest artifact_type")
    if backtest.get("schema_version") != corner_model.BACKTEST_SCHEMA_VERSION:
        raise CornerModelManagerError("unsupported backtest schema_version")
    if backtest.get("model_version") != corner_model.MODEL_VERSION:
        raise CornerModelManagerError("unsupported backtest model_version")
    if backtest.get("dependence") != corner_model.DEPENDENCE_MODEL:
        raise CornerModelManagerError("backtest dependence must be independent_nb")
    stored_hash = _required_hash(backtest.get("backtest_hash"), "backtest_hash")
    if stored_hash != corner_model.calculate_backtest_hash(backtest):
        raise CornerModelManagerError(
            "backtest_hash does not match backtest contents"
        )
    source_hash = _required_hash(
        backtest.get("source_data_hash"), "backtest.source_data_hash"
    )
    if dataset_hash is not None and source_hash != _required_hash(
        dataset_hash, "dataset_hash"
    ):
        raise CornerModelManagerError(
            "backtest source_data_hash does not match the registered dataset"
        )

    policy = backtest.get("evaluation_policy")
    if not isinstance(policy, Mapping) or set(policy) != {
        "split",
        "same_date_groups_kept_together",
        "unknown_team_policy",
    }:
        raise CornerModelManagerError("backtest evaluation_policy is incomplete")
    if (
        policy.get("split") != "expanding_window_complete_utc_date_groups"
        or policy.get("same_date_groups_kept_together") is not True
        or policy.get("unknown_team_policy") not in {"error", "league_average"}
    ):
        raise CornerModelManagerError(
            "backtest must use the chronological complete-date evaluation policy"
        )

    config = _validate_fit_config(backtest.get("fit_config"), "backtest.fit_config")
    if expected_config is not None and dict(config) != dict(expected_config):
        raise CornerModelManagerError(
            "backtest fit_config does not match the registry binding"
        )

    sample = backtest.get("sample")
    if not isinstance(sample, Mapping) or set(sample) != {
        "input_matches",
        "predictions",
        "excluded_unknown_team_matches",
        "blocks",
    }:
        raise CornerModelManagerError("backtest sample metadata is incomplete")
    input_matches = _positive_int(sample["input_matches"], "sample.input_matches")
    predictions_count = _positive_int(sample["predictions"], "sample.predictions")
    excluded_count = _nonnegative_int(
        sample["excluded_unknown_team_matches"],
        "sample.excluded_unknown_team_matches",
    )
    blocks_count = _positive_int(sample["blocks"], "sample.blocks")
    if dataset_rows is not None and input_matches != dataset_rows:
        raise CornerModelManagerError(
            "backtest input_matches does not match registered dataset_rows"
        )
    if dataset_records is not None and input_matches != len(dataset_records):
        raise CornerModelManagerError(
            "backtest input_matches does not match reopened dataset records"
        )

    blocks = backtest.get("blocks")
    predictions = backtest.get("predictions")
    if not isinstance(blocks, list) or len(blocks) != blocks_count:
        raise CornerModelManagerError("backtest blocks do not match sample.blocks")
    if not isinstance(predictions, list) or len(predictions) != predictions_count:
        raise CornerModelManagerError(
            "backtest predictions do not match sample.predictions"
        )

    date_to_block: dict[str, Mapping[str, Any]] = {}
    previous_block: Mapping[str, Any] | None = None
    summed_excluded = 0
    summed_forecasts = 0
    for index, block in enumerate(blocks, start=1):
        name = f"backtest.blocks[{index - 1}]"
        if not isinstance(block, Mapping):
            raise CornerModelManagerError(f"{name} must be an object")
        if block.get("block") != index:
            raise CornerModelManagerError(f"{name}.block must be sequential")
        training_matches = _positive_int(
            block.get("training_matches"), f"{name}.training_matches"
        )
        if training_matches < int(config["min_train_matches"]):
            raise CornerModelManagerError(
                f"{name}.training_matches is below the initial training minimum"
            )
        cutoff = _iso_date(
            block.get("training_cutoff_date"), f"{name}.training_cutoff_date"
        )
        test_dates = block.get("test_dates")
        if not isinstance(test_dates, list) or not test_dates:
            raise CornerModelManagerError(f"{name}.test_dates must be non-empty")
        parsed_test_dates = [
            _iso_date(value, f"{name}.test_dates") for value in test_dates
        ]
        if parsed_test_dates != sorted(set(parsed_test_dates)):
            raise CornerModelManagerError(
                f"{name}.test_dates must be sorted and unique"
            )
        if cutoff >= parsed_test_dates[0]:
            raise CornerModelManagerError(
                f"{name} leaks its training cutoff into the test block"
            )
        for raw_date in test_dates:
            if raw_date in date_to_block:
                raise CornerModelManagerError(
                    "a complete UTC date group appears in more than one block"
                )
            date_to_block[raw_date] = block
        test_matches = _positive_int(block.get("test_matches"), f"{name}.test_matches")
        forecast_matches = _nonnegative_int(
            block.get("forecast_matches"), f"{name}.forecast_matches"
        )
        block_excluded = _nonnegative_int(
            block.get("excluded_unknown_team_matches"),
            f"{name}.excluded_unknown_team_matches",
        )
        if forecast_matches + block_excluded != test_matches:
            raise CornerModelManagerError(
                f"{name} forecast and exclusion counts do not cover its test rows"
            )
        if policy["unknown_team_policy"] == "league_average" and block_excluded:
            raise CornerModelManagerError(
                "league_average backtests cannot exclude unknown-team matches"
            )
        _required_hash(block.get("model_hash"), f"{name}.model_hash")
        if previous_block is not None:
            expected_training = int(previous_block["training_matches"]) + int(
                previous_block["test_matches"]
            )
            if training_matches != expected_training:
                raise CornerModelManagerError(
                    f"{name}.training_matches breaks the expanding window"
                )
            if cutoff <= _iso_date(
                previous_block.get("training_cutoff_date"),
                f"{name}.previous_training_cutoff_date",
            ):
                raise CornerModelManagerError(
                    f"{name}.training_cutoff_date did not advance"
                )
        previous_block = block
        summed_excluded += block_excluded
        summed_forecasts += forecast_matches

    dataset_fixtures: dict[tuple[str, str, str], tuple[int, int]] | None = None
    if dataset_records is not None:
        dataset_fixtures = {}
        rows_by_date: dict[str, int] = {}
        for row in dataset_records:
            raw_date = row.get("date")
            row_date = raw_date.isoformat() if isinstance(raw_date, date) else str(raw_date)
            fixture = (row_date, str(row.get("home_team")), str(row.get("away_team")))
            if fixture in dataset_fixtures:
                raise CornerModelManagerError(
                    "reopened dataset contains a duplicate fixture identity"
                )
            dataset_fixtures[fixture] = (
                int(row.get("home_corners")),
                int(row.get("away_corners")),
            )
            rows_by_date[row_date] = rows_by_date.get(row_date, 0) + 1
        dataset_dates = sorted(rows_by_date)
        registered_test_dates = sorted(date_to_block)
        if not registered_test_dates:
            raise CornerModelManagerError("backtest contains no registered test dates")
        try:
            first_test_index = dataset_dates.index(registered_test_dates[0])
        except ValueError as exc:
            raise CornerModelManagerError(
                "backtest first test date is absent from the registered dataset"
            ) from exc
        if dataset_dates[first_test_index:] != registered_test_dates:
            raise CornerModelManagerError(
                "backtest test dates are not the complete trailing dataset date groups"
            )
        if first_test_index < 1:
            raise CornerModelManagerError("backtest has no chronological training dates")
        if blocks[0].get("training_cutoff_date") != dataset_dates[first_test_index - 1]:
            raise CornerModelManagerError(
                "backtest initial cutoff does not match the reopened dataset"
            )
        for block_index, block in enumerate(blocks):
            block_dates = list(block["test_dates"])
            expected_test_matches = sum(rows_by_date[value] for value in block_dates)
            if block.get("test_matches") != expected_test_matches:
                raise CornerModelManagerError(
                    f"backtest.blocks[{block_index}].test_matches does not match "
                    "the reopened dataset date groups"
                )
            first_block_date = block_dates[0]
            expected_training_matches = sum(
                rows_by_date[value] for value in dataset_dates if value < first_block_date
            )
            if block.get("training_matches") != expected_training_matches:
                raise CornerModelManagerError(
                    f"backtest.blocks[{block_index}].training_matches does not match "
                    "the reopened expanding window"
                )

    if summed_excluded != excluded_count:
        raise CornerModelManagerError(
            "backtest block exclusions do not match the sample total"
        )
    if summed_forecasts != predictions_count:
        raise CornerModelManagerError(
            "backtest block forecasts do not match the prediction total"
        )

    block_prediction_counts = {index: 0 for index in range(1, blocks_count + 1)}
    observed_fixtures: set[tuple[str, str, str]] = set()
    metric_values = {field: [] for field in METRIC_FIELDS}
    previous_fixture: tuple[str, str, str] | None = None
    for index, prediction in enumerate(predictions):
        name = f"backtest.predictions[{index}]"
        if not isinstance(prediction, Mapping):
            raise CornerModelManagerError(f"{name} must be an object")
        raw_date = prediction.get("date")
        prediction_date = _iso_date(raw_date, f"{name}.date")
        block = date_to_block.get(str(raw_date))
        if block is None:
            raise CornerModelManagerError(
                f"{name}.date is not bound to a registered test block"
            )
        home = prediction.get("home_team")
        away = prediction.get("away_team")
        if (
            not isinstance(home, str)
            or not home.strip()
            or not isinstance(away, str)
            or not away.strip()
            or home == away
        ):
            raise CornerModelManagerError(f"{name} fixture teams are invalid")
        fixture = (str(raw_date), home, away)
        if fixture in observed_fixtures:
            raise CornerModelManagerError("backtest contains a duplicate prediction")
        if previous_fixture is not None and fixture < previous_fixture:
            raise CornerModelManagerError(
                "backtest predictions must retain chronological deterministic order"
            )
        observed_fixtures.add(fixture)
        previous_fixture = fixture
        actual_home = _nonnegative_int(
            prediction.get("actual_home_corners"),
            f"{name}.actual_home_corners",
        )
        actual_away = _nonnegative_int(
            prediction.get("actual_away_corners"),
            f"{name}.actual_away_corners",
        )
        if actual_home > 99 or actual_away > 99:
            raise CornerModelManagerError(f"{name} has implausible corner counts")
        if prediction.get("actual_total_corners") != actual_home + actual_away:
            raise CornerModelManagerError(f"{name}.actual_total_corners is inconsistent")
        if prediction.get("actual_corner_margin") != actual_home - actual_away:
            raise CornerModelManagerError(f"{name}.actual_corner_margin is inconsistent")
        if dataset_fixtures is not None:
            expected_result = dataset_fixtures.get(fixture)
            if expected_result is None:
                raise CornerModelManagerError(
                    f"{name} fixture is absent from the registered dataset"
                )
            if expected_result != (actual_home, actual_away):
                raise CornerModelManagerError(
                    f"{name} result does not match the registered dataset"
                )
        cutoff = _iso_date(
            prediction.get("training_cutoff_date"),
            f"{name}.training_cutoff_date",
        )
        if cutoff >= prediction_date:
            raise CornerModelManagerError(f"{name} leaks training into its fixture")
        if cutoff.isoformat() != block.get("training_cutoff_date"):
            raise CornerModelManagerError(
                f"{name}.training_cutoff_date does not match its block"
            )
        for field in ("model_hash", "prediction_hash"):
            _required_hash(prediction.get(field), f"{name}.{field}")
        if prediction.get("model_hash") != block.get("model_hash"):
            raise CornerModelManagerError(f"{name}.model_hash does not match its block")
        fallback = prediction.get("unknown_team_fallback_used")
        if not isinstance(fallback, bool):
            raise CornerModelManagerError(
                f"{name}.unknown_team_fallback_used must be boolean"
            )
        if policy["unknown_team_policy"] == "error" and fallback:
            raise CornerModelManagerError(
                "error-policy backtests cannot contain fallback predictions"
            )
        block_number = int(block["block"])
        block_prediction_counts[block_number] += 1
        for field in METRIC_FIELDS:
            number = _finite(prediction.get(field), f"{name}.{field}")
            if number < 0.0:
                raise CornerModelManagerError(f"{name}.{field} must be non-negative")
            metric_values[field].append(number)

    for index, block in enumerate(blocks, start=1):
        if block_prediction_counts[index] != block.get("forecast_matches"):
            raise CornerModelManagerError(
                f"backtest.blocks[{index - 1}] forecast count is inconsistent"
            )

    metrics = backtest.get("metrics")
    if not isinstance(metrics, Mapping) or set(metrics) != set(METRIC_FIELDS):
        raise CornerModelManagerError(
            "backtest metrics must contain the exact proper-score fields"
        )
    for field in METRIC_FIELDS:
        expected = math.fsum(metric_values[field]) / predictions_count
        supplied = _finite(metrics[field], f"backtest.metrics.{field}")
        if supplied < 0.0 or abs(supplied - expected) > 1e-12:
            raise CornerModelManagerError(
                f"backtest.metrics.{field} does not match prediction-level evidence"
            )


def validate_backtest(
    backtest: Mapping[str, Any],
    *,
    dataset_hash: str | None = None,
    dataset_rows: int | None = None,
    dataset_records: Sequence[Mapping[str, Any]] | None = None,
    dataset_path: str | Path | None = None,
    source_lineage: Mapping[str, Any] | None = None,
    expected_config: Mapping[str, Any] | None = None,
) -> None:
    """Reproduce every walk-forward fit, probability, score, and baseline.

    A semantic hash is only a corruption check.  Trust comes from reopening the
    source-bound CSV and deterministically regenerating the complete artifact,
    including the joint probabilities, five-state settlement diagnostics,
    proper scores, block models, empirical/NB baselines, comparisons and final
    aggregate hash.
    """

    if not isinstance(backtest, Mapping):
        raise CornerModelManagerError("backtest must be a JSON object")
    if backtest.get("artifact_type") != corner_model.BACKTEST_ARTIFACT_TYPE:
        raise CornerModelManagerError("unexpected backtest artifact_type")
    if backtest.get("schema_version") != corner_model.BACKTEST_SCHEMA_VERSION:
        raise CornerModelManagerError("unsupported backtest schema_version")
    if backtest.get("model_version") != corner_model.MODEL_VERSION:
        raise CornerModelManagerError("unsupported backtest model_version")
    stored_hash = _required_hash(backtest.get("backtest_hash"), "backtest_hash")
    if stored_hash != corner_model.calculate_backtest_hash(backtest):
        raise CornerModelManagerError("backtest_hash does not match backtest contents")
    if dataset_path is None:
        raise CornerModelManagerError(
            "backtest validation requires the reopened source-bound dataset path"
        )
    source = Path(dataset_path).resolve()
    actual_dataset_hash = _file_hash(source)
    if dataset_hash is not None and actual_dataset_hash != _required_hash(
        dataset_hash, "dataset_hash"
    ):
        raise CornerModelManagerError("reopened dataset hash does not match binding")
    if backtest.get("source_data_hash") != actual_dataset_hash:
        raise CornerModelManagerError(
            "backtest source_data_hash does not match reopened dataset"
        )
    try:
        reopened_records = corner_model.load_training_csv(source)
    except corner_model.CornerModelError as exc:
        raise CornerModelManagerError(f"backtest dataset is invalid: {exc}") from exc
    if dataset_records is not None and list(dataset_records) != reopened_records:
        raise CornerModelManagerError("supplied records do not match reopened dataset")
    if dataset_rows is not None and dataset_rows != len(reopened_records):
        raise CornerModelManagerError("backtest dataset_rows does not match CSV")
    if backtest.get("sample", {}).get("input_matches") != len(reopened_records):
        raise CornerModelManagerError("backtest input_matches does not match CSV")
    expected_profile = corner_model.training_dataset_profile(reopened_records)
    if backtest.get("dataset_profile") != expected_profile:
        raise CornerModelManagerError("backtest dataset_profile does not match CSV")
    normalized_lineage = corner_model._normalize_source_lineage(source_lineage)
    if normalized_lineage is None:
        raise CornerModelManagerError("backtest validation requires source_lineage")
    if backtest.get("source_lineage") != normalized_lineage:
        raise CornerModelManagerError("backtest source_lineage does not match binding")
    config = _validate_fit_config(backtest.get("fit_config"), "backtest.fit_config")
    if expected_config is not None and config != _validate_fit_config(
        expected_config, "expected_config"
    ):
        raise CornerModelManagerError("backtest fit_config does not match registry")
    policy = backtest.get("evaluation_policy")
    if not isinstance(policy, Mapping):
        raise CornerModelManagerError("backtest evaluation_policy is missing")
    unknown_team_policy = policy.get("unknown_team_policy")
    if unknown_team_policy not in {"error", "league_average"}:
        raise CornerModelManagerError("backtest unknown_team_policy is invalid")
    if policy.get("research_cohort_opt_in") is not False:
        raise CornerModelManagerError(
            "registered backtest cannot use research-only competition cohorts"
        )
    try:
        reproduced = corner_model.backtest_model(
            source,
            min_train_matches=int(config["min_train_matches"]),
            test_block_size=int(config["test_block_size"]),
            half_life_days=float(config["half_life_days"]),
            iterations=int(config["iterations"]),
            learning_rate=float(config["learning_rate"]),
            regularization=float(config["regularization"]),
            unknown_team_policy=str(unknown_team_policy),
            tail_tolerance=float(config["tail_tolerance"]),
            hard_max_corners=int(config["hard_max_corners"]),
            source_lineage=normalized_lineage,
        )
    except corner_model.CornerModelError as exc:
        raise CornerModelManagerError(f"backtest deterministic replay failed: {exc}") from exc
    if _canonical_bytes(dict(backtest)) != _canonical_bytes(reproduced):
        raise CornerModelManagerError(
            "backtest differs from deterministic prediction-level replay"
        )


def derive_historical_deployment(backtest: Mapping[str, Any]) -> dict[str, Any]:
    """Derive candidate/shadow status exclusively from validated evaluation data."""

    sample = backtest.get("sample")
    metrics = backtest.get("metrics")
    comparisons = backtest.get("comparisons")
    holdout = backtest.get("untouched_holdout")
    if not isinstance(sample, Mapping) or not isinstance(metrics, Mapping):
        raise CornerModelManagerError("deployment requires validated backtest evidence")
    if not isinstance(comparisons, Mapping):
        raise CornerModelManagerError("deployment requires paired baseline comparisons")
    if (
        not isinstance(holdout, Mapping)
        or holdout.get("policy_version") != corner_model.HOLDOUT_POLICY_VERSION
        or holdout.get("status") not in {"available", "insufficient_history"}
        or holdout.get("development_only")
        is not (holdout.get("status") != "available")
        or holdout.get("not_used_in_candidate_metric_thresholds") is not True
    ):
        raise CornerModelManagerError("deployment requires a valid untouched holdout report")
    predictions = int(sample["predictions"])
    excluded = int(sample["excluded_unknown_team_matches"])
    component_excluded = int(sample["excluded_component_incomparable_matches"])
    attempted = predictions + excluded + component_excluded
    exclusion_fraction = excluded / attempted if attempted else 0.0
    component_exclusion_fraction = component_excluded / attempted if attempted else 0.0
    checks: dict[str, bool] = {
        "minimum_predictions": predictions >= CANDIDATE_GATE["minimum_predictions"],
        "minimum_blocks": int(sample["blocks"]) >= CANDIDATE_GATE["minimum_blocks"],
        "untouched_holdout_available": holdout.get("status") == "available",
        "maximum_unknown_exclusion_fraction": exclusion_fraction
        <= CANDIDATE_GATE["maximum_unknown_exclusion_fraction"],
        "maximum_component_incomparable_exclusion_fraction": (
            component_exclusion_fraction
            <= CANDIDATE_GATE["maximum_component_incomparable_exclusion_fraction"]
        ),
    }
    for metric, maximum in CANDIDATE_GATE["maximum_metrics"].items():
        checks[f"maximum_{metric}"] = float(metrics[metric]) <= float(maximum)
    observed_comparisons: dict[str, Any] = {}
    for baseline in CANDIDATE_GATE["required_baselines"]:
        raw_baseline = comparisons.get(baseline)
        if not isinstance(raw_baseline, Mapping):
            raise CornerModelManagerError(
                f"deployment comparison is missing baseline {baseline}"
            )
        observed_comparisons[baseline] = {}
        for metric in CANDIDATE_GATE["required_comparison_metrics"]:
            comparison = raw_baseline.get(metric)
            if not isinstance(comparison, Mapping):
                raise CornerModelManagerError(
                    f"deployment comparison is missing {baseline}.{metric}"
                )
            if comparison.get("predictions") != predictions:
                raise CornerModelManagerError(
                    f"deployment comparison {baseline}.{metric} sample is inconsistent"
                )
            independent_units = comparison.get("independent_units")
            if (
                isinstance(independent_units, bool)
                or not isinstance(independent_units, int)
                or independent_units < 1
                or independent_units > int(sample["blocks"])
                or comparison.get("uncertainty_unit")
                != "walk_forward_block_cluster"
            ):
                raise CornerModelManagerError(
                    f"deployment comparison {baseline}.{metric} uncertainty units are invalid"
                )
            model_mean = _finite(
                comparison.get("model_mean"),
                f"comparisons.{baseline}.{metric}.model_mean",
            )
            baseline_mean = _finite(
                comparison.get("baseline_mean"),
                f"comparisons.{baseline}.{metric}.baseline_mean",
            )
            mean = _finite(
                comparison.get("mean_improvement"),
                f"comparisons.{baseline}.{metric}.mean_improvement",
            )
            relative_raw = comparison.get("relative_improvement")
            lower_raw = comparison.get("one_sided_95_lower_bound")
            standard_error_raw = comparison.get("sample_standard_error")
            relative = (
                _finite(
                    relative_raw,
                    f"comparisons.{baseline}.{metric}.relative_improvement",
                )
                if relative_raw is not None
                else None
            )
            lower = (
                _finite(
                    lower_raw,
                    f"comparisons.{baseline}.{metric}.one_sided_95_lower_bound",
                )
                if lower_raw is not None
                else None
            )
            standard_error = (
                _finite(
                    standard_error_raw,
                    f"comparisons.{baseline}.{metric}.sample_standard_error",
                )
                if standard_error_raw is not None
                else None
            )
            estimable = comparison.get("uncertainty_estimable") is True
            if abs(mean - (baseline_mean - model_mean)) > 1e-12:
                raise CornerModelManagerError(
                    f"deployment comparison {baseline}.{metric} mean is inconsistent"
                )
            expected_relative = (
                mean / baseline_mean if baseline_mean > 0.0 else None
            )
            if (
                (expected_relative is None) != (relative is None)
                or expected_relative is not None
                and relative is not None
                and abs(relative - expected_relative) > 1e-12
            ):
                raise CornerModelManagerError(
                    f"deployment comparison {baseline}.{metric} relative value is inconsistent"
                )
            if estimable is not (independent_units >= 2):
                raise CornerModelManagerError(
                    f"deployment comparison {baseline}.{metric} uncertainty flag is inconsistent"
                )
            if estimable:
                if standard_error is None or lower is None or standard_error < 0.0:
                    raise CornerModelManagerError(
                        f"deployment comparison {baseline}.{metric} uncertainty is incomplete"
                    )
                expected_lower = mean - corner_model.ONE_SIDED_95_Z * standard_error
                if abs(lower - expected_lower) > 1e-12:
                    raise CornerModelManagerError(
                        f"deployment comparison {baseline}.{metric} confidence bound is inconsistent"
                    )
            elif standard_error is not None or lower is not None:
                raise CornerModelManagerError(
                    f"deployment comparison {baseline}.{metric} uncertainty must be null"
                )
            prefix = f"better_than_{baseline}_{metric}"
            checks[f"{prefix}_absolute"] = (
                mean >= CANDIDATE_GATE["minimum_absolute_improvement"]
            )
            checks[f"{prefix}_relative"] = (
                relative is not None
                and relative >= CANDIDATE_GATE["minimum_relative_improvement"]
            )
            checks[f"{prefix}_uncertainty"] = (
                estimable
                and standard_error is not None
                and standard_error >= 0.0
                and lower is not None
                and lower > CANDIDATE_GATE[
                    "minimum_one_sided_95_lower_bound"
                ]
            )
            observed_comparisons[baseline][metric] = copy.deepcopy(dict(comparison))
    passed = all(checks.values())
    return {
        "deployment_status": "candidate" if passed else "shadow",
        "deployment_policy_version": DEPLOYMENT_POLICY_VERSION,
        "evidence_scope": (
            "historical_development_with_untouched_holdout"
            if holdout.get("status") == "available"
            else "historical_development_only"
        ),
        "gate": {
            "criteria": copy.deepcopy(CANDIDATE_GATE),
            "observed": {
                "predictions": predictions,
                "blocks": int(sample["blocks"]),
                "excluded_unknown_team_matches": excluded,
                "unknown_exclusion_fraction": exclusion_fraction,
                "excluded_component_incomparable_matches": component_excluded,
                "component_incomparable_exclusion_fraction": component_exclusion_fraction,
                "metrics": {
                    key: float(metrics[key])
                    for key in CANDIDATE_GATE["maximum_metrics"]
                },
                "comparisons": observed_comparisons,
                "untouched_holdout_status": holdout.get("status"),
                "development_only": holdout.get("development_only"),
            },
            "checks": checks,
            "passed": passed,
        },
        "live_forward_evidence_bound": False,
        "production_eligible": False,
        "formal_corner_total_eligible": False,
        "formal_corner_handicap_eligible": False,
        "formal_corner_ineligible_reason": FORMAL_CORNER_INELIGIBLE_REASON,
    }


def _validate_deployment(
    entry: Mapping[str, Any], backtest: Mapping[str, Any], name: str
) -> None:
    expected = derive_historical_deployment(backtest)
    supplied = entry.get("deployment")
    if supplied != expected:
        raise CornerModelManagerError(
            f"{name}.deployment is not derived from its verified evaluation"
        )
    # Duplicate the most important fail-closed fields at entry level so callers
    # cannot accidentally inspect only a nested status object.
    for field in (
        "deployment_status",
        "deployment_policy_version",
        "formal_corner_total_eligible",
        "formal_corner_handicap_eligible",
        "formal_corner_ineligible_reason",
    ):
        if entry.get(field) != expected[field]:
            raise CornerModelManagerError(
                f"{name}.{field} does not satisfy the historical deployment gate"
            )
    if entry.get("deployment_status") not in {"candidate", "shadow"}:
        raise CornerModelManagerError(
            f"{name}.deployment_status cannot be production without a new policy"
        )
    if (
        entry.get("formal_corner_total_eligible") is not False
        or entry.get("formal_corner_handicap_eligible") is not False
    ):
        raise CornerModelManagerError(
            f"{name} historical evidence cannot authorize formal corner markets"
        )


def _validate_stored_deployment_shape_v1(entry: Mapping[str, Any], name: str) -> None:
    """Fail closed on deployment claims even before evaluation files are opened."""

    deployment = entry.get("deployment")
    expected_fields = {
        "deployment_status",
        "deployment_policy_version",
        "evidence_scope",
        "gate",
        "live_forward_evidence_bound",
        "production_eligible",
        "formal_corner_total_eligible",
        "formal_corner_handicap_eligible",
        "formal_corner_ineligible_reason",
    }
    if not isinstance(deployment, Mapping) or set(deployment) != expected_fields:
        raise CornerModelManagerError(f"{name}.deployment has an invalid shape")
    if (
        deployment.get("deployment_policy_version") != DEPLOYMENT_POLICY_VERSION
        or deployment.get("evidence_scope")
        not in {
            "historical_development_with_untouched_holdout",
            "historical_development_only",
        }
        or deployment.get("live_forward_evidence_bound") is not False
        or deployment.get("production_eligible") is not False
        or deployment.get("formal_corner_total_eligible") is not False
        or deployment.get("formal_corner_handicap_eligible") is not False
        or deployment.get("formal_corner_ineligible_reason")
        != FORMAL_CORNER_INELIGIBLE_REASON
    ):
        raise CornerModelManagerError(
            f"{name}.deployment exceeds historical-only authority"
        )
    gate = deployment.get("gate")
    if not isinstance(gate, Mapping) or set(gate) != {
        "criteria",
        "observed",
        "checks",
        "passed",
    }:
        raise CornerModelManagerError(f"{name}.deployment.gate has an invalid shape")
    if gate.get("criteria") != CANDIDATE_GATE:
        raise CornerModelManagerError(
            f"{name}.deployment.gate criteria are not the versioned policy"
        )
    observed = gate.get("observed")
    if not isinstance(observed, Mapping) or set(observed) != {
        "predictions",
        "blocks",
        "unknown_exclusion_fraction",
        "metrics",
    }:
        raise CornerModelManagerError(
            f"{name}.deployment.gate observed evidence is invalid"
        )
    predictions = _positive_int(
        observed.get("predictions"), f"{name}.deployment.gate.predictions"
    )
    blocks = _positive_int(
        observed.get("blocks"), f"{name}.deployment.gate.blocks"
    )
    exclusion_fraction = _finite(
        observed.get("unknown_exclusion_fraction"),
        f"{name}.deployment.gate.unknown_exclusion_fraction",
    )
    if not 0.0 <= exclusion_fraction <= 1.0:
        raise CornerModelManagerError(
            f"{name}.deployment.gate.unknown_exclusion_fraction is invalid"
        )
    observed_metrics = observed.get("metrics")
    if not isinstance(observed_metrics, Mapping) or set(observed_metrics) != set(
        CANDIDATE_GATE["maximum_metrics"]
    ):
        raise CornerModelManagerError(
            f"{name}.deployment.gate metrics are incomplete"
        )
    recomputed_checks: dict[str, bool] = {
        "minimum_predictions": predictions >= CANDIDATE_GATE["minimum_predictions"],
        "minimum_blocks": blocks >= CANDIDATE_GATE["minimum_blocks"],
        "maximum_unknown_exclusion_fraction": exclusion_fraction
        <= CANDIDATE_GATE["maximum_unknown_exclusion_fraction"],
    }
    for metric, maximum in CANDIDATE_GATE["maximum_metrics"].items():
        value = _finite(
            observed_metrics.get(metric), f"{name}.deployment.gate.metrics.{metric}"
        )
        if value < 0.0:
            raise CornerModelManagerError(
                f"{name}.deployment.gate.metrics.{metric} must be non-negative"
            )
        recomputed_checks[f"maximum_{metric}"] = value <= float(maximum)
    passed = all(recomputed_checks.values())
    if gate.get("checks") != recomputed_checks or gate.get("passed") is not passed:
        raise CornerModelManagerError(
            f"{name}.deployment.gate is not derived from its observed evidence"
        )
    expected_status = "candidate" if passed else "shadow"
    if deployment.get("deployment_status") != expected_status:
        raise CornerModelManagerError(
            f"{name}.deployment_status is not derived from its gate"
        )
    for field in (
        "deployment_status",
        "deployment_policy_version",
        "formal_corner_total_eligible",
        "formal_corner_handicap_eligible",
        "formal_corner_ineligible_reason",
    ):
        if entry.get(field) != deployment.get(field):
            raise CornerModelManagerError(
                f"{name}.{field} does not match its deployment evidence"
            )


def _validate_stored_deployment_shape(entry: Mapping[str, Any], name: str) -> None:
    """Fail closed structurally and rederive the historical candidate gate."""

    deployment = entry.get("deployment")
    if not isinstance(deployment, Mapping):
        raise CornerModelManagerError(f"{name}.deployment is missing")
    if (
        deployment.get("deployment_policy_version") != DEPLOYMENT_POLICY_VERSION
        or deployment.get("evidence_scope")
        not in {
            "historical_development_with_untouched_holdout",
            "historical_development_only",
        }
        or deployment.get("live_forward_evidence_bound") is not False
        or deployment.get("production_eligible") is not False
        or deployment.get("formal_corner_total_eligible") is not False
        or deployment.get("formal_corner_handicap_eligible") is not False
        or deployment.get("formal_corner_ineligible_reason")
        != FORMAL_CORNER_INELIGIBLE_REASON
    ):
        raise CornerModelManagerError(
            f"{name}.deployment exceeds historical-only authority"
        )
    gate = deployment.get("gate")
    if not isinstance(gate, Mapping) or gate.get("criteria") != CANDIDATE_GATE:
        raise CornerModelManagerError(f"{name}.deployment.gate is not the v2 policy")
    observed = gate.get("observed")
    expected_observed_fields = {
        "predictions",
        "blocks",
        "excluded_unknown_team_matches",
        "unknown_exclusion_fraction",
        "excluded_component_incomparable_matches",
        "component_incomparable_exclusion_fraction",
        "metrics",
        "comparisons",
        "untouched_holdout_status",
        "development_only",
    }
    if not isinstance(observed, Mapping) or set(observed) != expected_observed_fields:
        raise CornerModelManagerError(f"{name}.deployment.gate observed evidence is invalid")
    predictions = _positive_int(
        observed.get("predictions"), f"{name}.deployment.gate.predictions"
    )
    blocks = _positive_int(observed.get("blocks"), f"{name}.deployment.gate.blocks")
    excluded = _nonnegative_int(
        observed.get("excluded_unknown_team_matches"),
        f"{name}.deployment.gate.excluded_unknown_team_matches",
    )
    component_excluded = _nonnegative_int(
        observed.get("excluded_component_incomparable_matches"),
        f"{name}.deployment.gate.excluded_component_incomparable_matches",
    )
    attempted = predictions + excluded + component_excluded
    expected_fraction = excluded / attempted if attempted else 0.0
    if abs(
        _finite(
            observed.get("unknown_exclusion_fraction"),
            f"{name}.deployment.gate.unknown_exclusion_fraction",
        )
        - expected_fraction
    ) > 1e-15:
        raise CornerModelManagerError(
            f"{name}.deployment unknown exclusion fraction is inconsistent"
        )
    expected_component_fraction = component_excluded / attempted if attempted else 0.0
    if abs(
        _finite(
            observed.get("component_incomparable_exclusion_fraction"),
            f"{name}.deployment.gate.component_incomparable_exclusion_fraction",
        )
        - expected_component_fraction
    ) > 1e-15:
        raise CornerModelManagerError(
            f"{name}.deployment component exclusion fraction is inconsistent"
        )
    synthetic = {
        "sample": {
            "predictions": predictions,
            "blocks": blocks,
            "excluded_unknown_team_matches": excluded,
            "excluded_component_incomparable_matches": component_excluded,
        },
        "metrics": observed.get("metrics"),
        "comparisons": observed.get("comparisons"),
        "untouched_holdout": {
            "policy_version": corner_model.HOLDOUT_POLICY_VERSION,
            "status": observed.get("untouched_holdout_status"),
            "development_only": observed.get("development_only"),
            "not_used_in_candidate_metric_thresholds": True,
        },
    }
    expected = derive_historical_deployment(synthetic)
    if dict(deployment) != expected:
        raise CornerModelManagerError(
            f"{name}.deployment is not derived from its baseline evidence"
        )
    for field in (
        "deployment_status",
        "deployment_policy_version",
        "formal_corner_total_eligible",
        "formal_corner_handicap_eligible",
        "formal_corner_ineligible_reason",
    ):
        if entry.get(field) != deployment.get(field):
            raise CornerModelManagerError(f"{name}.{field} does not match deployment")


def _validate_model_and_evaluation_binding(
    directory: Path,
    entry: Mapping[str, Any],
    name: str,
    *,
    replay_cache: dict[str, dict[str, Any]] | None = None,
    replay_all_leagues: bool = True,
) -> None:
    dataset_file = _safe_filename(
        entry.get("dataset_file"), f"{name}.dataset_file", suffix=".csv"
    )
    model_file = _safe_filename(
        entry.get("model_file"), f"{name}.model_file", suffix=".json"
    )
    evaluation_file = _safe_filename(
        entry.get("evaluation_file"), f"{name}.evaluation_file", suffix=".json"
    )
    manifest_file = _safe_filename(
        entry.get("dataset_manifest_file"),
        f"{name}.dataset_manifest_file",
        suffix=".json",
    )
    source_bundle_file = _safe_filename(
        entry.get("source_bundle_file"),
        f"{name}.source_bundle_file",
        suffix=".json",
    )
    dataset_path = directory / dataset_file
    model_path = directory / model_file
    evaluation_path = directory / evaluation_file
    manifest_path = directory / manifest_file
    source_bundle_path = directory / source_bundle_file
    dataset_hash = _required_hash(entry.get("dataset_hash"), f"{name}.dataset_hash")
    expected_manifest_file_hash = _required_hash(
        entry.get("dataset_manifest_file_sha256"),
        f"{name}.dataset_manifest_file_sha256",
    )
    expected_source_file_hash = _required_hash(
        entry.get("source_file_sha256"), f"{name}.source_file_sha256"
    )
    if _file_hash(manifest_path) != expected_manifest_file_hash:
        raise CornerModelManagerError(
            f"{name} registered dataset manifest file hash does not match"
        )
    if _file_hash(source_bundle_path) != expected_source_file_hash:
        raise CornerModelManagerError(
            f"{name} registered source bundle file hash does not match"
        )
    context = _validate_source_bound_dataset(
        dataset_path,
        league_key=str(entry.get("league_key") or ""),
        manifest_path=manifest_path,
        source_bundle_path=source_bundle_path,
        require_canonical_dataset_name=False,
        replay_cache=replay_cache,
        replay_all_leagues=replay_all_leagues,
    )
    if context["dataset_hash"] != dataset_hash:
        raise CornerModelManagerError(
            f"{name} registered dataset file hash does not match"
        )
    if entry.get("source_lineage") != context["lineage"]:
        raise CornerModelManagerError(f"{name}.source_lineage does not match replay")
    if entry.get("dataset_profile") != context["profile"]:
        raise CornerModelManagerError(f"{name}.dataset_profile does not match replay")
    records = context["records"]
    if entry.get("dataset_rows") != len(records):
        raise CornerModelManagerError(f"{name}.dataset_rows does not match the CSV")
    actual_start = min(row["date"] for row in records).isoformat()
    actual_cutoff = max(row["date"] for row in records).isoformat()
    if entry.get("training_start") != actual_start:
        raise CornerModelManagerError(f"{name}.training_start does not match the CSV")
    if entry.get("training_cutoff") != actual_cutoff:
        raise CornerModelManagerError(f"{name}.training_cutoff does not match the CSV")
    actual_start_kickoff = context["profile"]["kickoff_utc_start"]
    actual_cutoff_kickoff = context["profile"]["kickoff_utc_end"]
    if entry.get("training_start_kickoff_utc") != actual_start_kickoff:
        raise CornerModelManagerError(
            f"{name}.training_start_kickoff_utc does not match CSV"
        )
    if entry.get("training_cutoff_kickoff_utc") != actual_cutoff_kickoff:
        raise CornerModelManagerError(
            f"{name}.training_cutoff_kickoff_utc does not match CSV"
        )

    expected_model_file_hash = _required_hash(
        entry.get("model_file_sha256"), f"{name}.model_file_sha256"
    )
    if _file_hash(model_path) != expected_model_file_hash:
        raise CornerModelManagerError(f"{name} registered model file hash does not match")
    try:
        model = corner_model.load_model(model_path)
    except corner_model.CornerModelError as exc:
        raise CornerModelManagerError(f"{name} model validation failed: {exc}") from exc
    if entry.get("model_hash") != model.get("model_hash"):
        raise CornerModelManagerError(f"{name}.model_hash does not match model file")
    if entry.get("model_version") != model.get("model_version"):
        raise CornerModelManagerError(f"{name}.model_version does not match model file")
    if entry.get("model_config") != model.get("config"):
        raise CornerModelManagerError(f"{name}.model_config does not match model file")
    if model.get("fit", {}).get("historical_simulation") is not False:
        raise CornerModelManagerError(
            f"{name} registered final model cannot be a historical simulation"
        )
    if model.get("authority", {}).get("research_cohort_opt_in") is not False:
        raise CornerModelManagerError(
            f"{name} registered model cannot use research-only competition cohorts"
        )
    training = model["training"]
    if training.get("source_data_hash") != dataset_hash:
        raise CornerModelManagerError(
            f"{name} model source hash does not match registered dataset"
        )
    if training.get("source_file") != dataset_file:
        raise CornerModelManagerError(
            f"{name} model source filename does not match registered dataset"
        )
    if training.get("matches") != len(records):
        raise CornerModelManagerError(f"{name} model match count does not match dataset")
    if (
        training.get("source_lineage") != context["lineage"]
        or training.get("dataset_profile") != context["profile"]
    ):
        raise CornerModelManagerError(
            f"{name} model source-bound lineage does not match dataset"
        )
    if (
        training.get("start_date") != actual_start
        or training.get("end_date") != actual_cutoff
        or training.get("cutoff_date") != actual_cutoff
    ):
        raise CornerModelManagerError(f"{name} model training dates do not match dataset")
    if (
        training.get("start_kickoff_utc") != actual_start_kickoff
        or training.get("end_kickoff_utc") != actual_cutoff_kickoff
        or training.get("cutoff_kickoff_utc") != actual_cutoff_kickoff
    ):
        raise CornerModelManagerError(
            f"{name} model training kickoff range does not match dataset"
        )
    # A forged parameter vector plus coordinated hashes is rejected by
    # refitting the exact deterministic model configuration from the reopened
    # source-bound CSV.
    try:
        reproduced_model = corner_model.fit_model(
            dataset_path,
            half_life_days=float(model["config"]["half_life_days"]),
            iterations=int(model["config"]["iterations"]),
            learning_rate=float(model["config"]["learning_rate"]),
            regularization=float(model["config"]["regularization"]),
            generated_at=str(model["generated_at"]),
            source_lineage=context["lineage"],
        )
    except corner_model.CornerModelError as exc:
        raise CornerModelManagerError(f"{name} deterministic model refit failed: {exc}") from exc
    if _canonical_bytes(model) != _canonical_bytes(reproduced_model):
        raise CornerModelManagerError(
            f"{name} model differs from deterministic source-bound refit"
        )

    expected_evaluation_file_hash = _required_hash(
        entry.get("evaluation_file_sha256"), f"{name}.evaluation_file_sha256"
    )
    if _file_hash(evaluation_path) != expected_evaluation_file_hash:
        raise CornerModelManagerError(
            f"{name} registered evaluation file hash does not match"
        )
    backtest = _read_json(evaluation_path, f"{name} corner backtest")
    evaluation_config = entry.get("evaluation_config")
    _validate_fit_config(evaluation_config, f"{name}.evaluation_config")
    validate_backtest(
        backtest,
        dataset_hash=dataset_hash,
        dataset_rows=len(records),
        dataset_records=records,
        dataset_path=dataset_path,
        source_lineage=context["lineage"],
        expected_config=evaluation_config,
    )
    if (
        entry.get("evaluation_hash") != backtest.get("backtest_hash")
        or entry.get("backtest_hash") != backtest.get("backtest_hash")
    ):
        raise CornerModelManagerError(
            f"{name}.evaluation_hash does not match evaluation file"
        )
    for evaluation_field, model_field in MODEL_CONFIG_BINDINGS.items():
        if evaluation_config[evaluation_field] != model["config"][model_field]:
            raise CornerModelManagerError(
                f"{name} model and evaluation configurations are not the same"
            )
    _validate_deployment(entry, backtest, name)


def validate_registry(
    registry: Mapping[str, Any], *, model_dir: str | Path | None = None
) -> None:
    """Validate registry structure and, when supplied, every bound local file."""

    if not isinstance(registry, Mapping):
        raise CornerModelManagerError("registry must be a JSON object")
    if registry.get("artifact_type") != REGISTRY_ARTIFACT_TYPE:
        raise CornerModelManagerError("unexpected registry artifact_type")
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise CornerModelManagerError("unsupported registry schema_version")
    if registry.get("manager_version") != MANAGER_VERSION:
        raise CornerModelManagerError("unsupported corner manager version")
    _parse_aware_datetime(registry.get("generated_at"), "registry.generated_at")
    stored_hash = _required_hash(registry.get("registry_hash"), "registry_hash")
    if stored_hash != calculate_registry_hash(registry):
        raise CornerModelManagerError("registry_hash does not match registry contents")
    if registry.get("deployment_policy") != DEPLOYMENT_POLICY:
        raise CornerModelManagerError("registry deployment_policy is invalid")
    leagues = registry.get("leagues")
    if not isinstance(leagues, list) or not leagues:
        raise CornerModelManagerError("registry contains no league models")
    if any(not isinstance(entry, Mapping) for entry in leagues):
        raise CornerModelManagerError("registry league entries must be objects")
    ordered_keys = [entry.get("league_key") for entry in leagues]
    if any(not isinstance(value, str) for value in ordered_keys):
        raise CornerModelManagerError("registry league keys must be strings")
    if ordered_keys != sorted(ordered_keys):
        raise CornerModelManagerError("registry leagues must be sorted by league_key")

    observed_keys: set[str] = set()
    observed_aliases: set[str] = set()
    expected_dataset_hashes: dict[str, str] = {}
    directory = Path(model_dir).resolve() if model_dir is not None else None
    source_replay_cache: dict[str, dict[str, Any]] = {}
    if directory is not None and not directory.is_dir():
        raise CornerModelManagerError(f"model directory does not exist: {directory}")
    for index, entry in enumerate(leagues):
        name = f"registry.leagues[{index}]"
        if not isinstance(entry, Mapping):
            raise CornerModelManagerError(f"{name} must be an object")
        league_key = entry.get("league_key")
        if (
            not isinstance(league_key, str)
            or not LEAGUE_KEY_RE.fullmatch(league_key)
            or league_key in observed_keys
        ):
            raise CornerModelManagerError(f"{name}.league_key is invalid or duplicated")
        observed_keys.add(league_key)
        league = entry.get("league")
        if not isinstance(league, str) or not league.strip():
            raise CornerModelManagerError(f"{name}.league is required")
        aliases = entry.get("aliases")
        if not isinstance(aliases, list) or aliases != _normalize_aliases(
            league_key, league, aliases
        ):
            raise CornerModelManagerError(f"{name}.aliases are invalid or duplicated")
        normalized_aliases = {value.casefold() for value in aliases}
        if observed_aliases.intersection(normalized_aliases):
            raise CornerModelManagerError("registry league aliases are ambiguous")
        observed_aliases.update(normalized_aliases)
        dataset_hash = _required_hash(
            entry.get("dataset_hash"), f"{name}.dataset_hash"
        )
        expected_dataset_hashes[league_key] = dataset_hash
        _positive_int(entry.get("dataset_rows"), f"{name}.dataset_rows")
        training_start = _iso_date(
            entry.get("training_start"), f"{name}.training_start"
        )
        training_cutoff = _iso_date(
            entry.get("training_cutoff"), f"{name}.training_cutoff"
        )
        if training_start > training_cutoff:
            raise CornerModelManagerError(f"{name} training date range is invalid")
        for field in (
            "dataset_manifest_file_sha256",
            "source_file_sha256",
            "model_file_sha256",
            "model_hash",
            "evaluation_file_sha256",
            "evaluation_hash",
            "backtest_hash",
            "lineage_hash",
        ):
            _required_hash(entry.get(field), f"{name}.{field}")
        _safe_filename(entry.get("dataset_file"), f"{name}.dataset_file", suffix=".csv")
        _safe_filename(
            entry.get("dataset_manifest_file"),
            f"{name}.dataset_manifest_file",
            suffix=".json",
        )
        _safe_filename(
            entry.get("source_bundle_file"),
            f"{name}.source_bundle_file",
            suffix=".json",
        )
        _safe_filename(entry.get("model_file"), f"{name}.model_file", suffix=".json")
        _safe_filename(
            entry.get("evaluation_file"), f"{name}.evaluation_file", suffix=".json"
        )
        if entry.get("model_version") != corner_model.MODEL_VERSION:
            raise CornerModelManagerError(f"{name}.model_version is unsupported")
        model_config = entry.get("model_config")
        if not isinstance(model_config, Mapping):
            raise CornerModelManagerError(f"{name}.model_config is missing")
        source_lineage = entry.get("source_lineage")
        if (
            not isinstance(source_lineage, Mapping)
            or corner_model._normalize_source_lineage(source_lineage)
            != source_lineage
            or source_lineage.get("league_key") != league_key
            or source_lineage.get("dataset_hash") != dataset_hash
        ):
            raise CornerModelManagerError(f"{name}.source_lineage is invalid")
        dataset_profile = entry.get("dataset_profile")
        if (
            not isinstance(dataset_profile, Mapping)
            or dataset_profile.get("league_key") != league_key
            or dataset_profile.get("rows") != entry.get("dataset_rows")
        ):
            raise CornerModelManagerError(f"{name}.dataset_profile is invalid")
        for field in ("fixture_set_hash", "response_set_hash", "semantic_rows_hash"):
            _required_hash(dataset_profile.get(field), f"{name}.dataset_profile.{field}")
        fixture_graph = dataset_profile.get("fixture_graph")
        if not isinstance(fixture_graph, Mapping):
            raise CornerModelManagerError(f"{name}.dataset_profile.fixture_graph is missing")
        _required_hash(
            fixture_graph.get("components_hash"),
            f"{name}.dataset_profile.fixture_graph.components_hash",
        )
        for field in ("training_start_kickoff_utc", "training_cutoff_kickoff_utc"):
            _parse_aware_datetime(entry.get(field), f"{name}.{field}")
        _validate_fit_config(entry.get("evaluation_config"), f"{name}.evaluation_config")
        if entry.get("lineage_hash") != calculate_lineage_hash(entry):
            raise CornerModelManagerError(
                f"{name}.lineage_hash does not match dataset/model/evaluation binding"
            )
        # Structural validation cannot derive status without opening evaluation.
        # It still fails closed on every field that could imply production.
        _validate_stored_deployment_shape(entry, name)
        if directory is not None:
            _validate_model_and_evaluation_binding(
                directory,
                entry,
                name,
                replay_cache=source_replay_cache,
            )

    if registry.get("dataset_hashes") != expected_dataset_hashes:
        raise CornerModelManagerError(
            "registry dataset_hashes do not match its league entries"
        )


def _registry_content_cache_key(
    directory: Path, registry: Mapping[str, Any]
) -> str:
    files: set[str] = {REGISTRY_FILENAME}
    for index, entry in enumerate(registry["leagues"]):
        for field, suffix in (
            ("dataset_file", ".csv"),
            ("dataset_manifest_file", ".json"),
            ("source_bundle_file", ".json"),
            ("model_file", ".json"),
            ("evaluation_file", ".json"),
        ):
            files.add(
                _safe_filename(
                    entry.get(field),
                    f"registry.leagues[{index}].{field}",
                    suffix=suffix,
                )
            )
    return _canonical_hash(
        {
            "directory": str(directory),
            "manager_version": MANAGER_VERSION,
            "model_version": corner_model.MODEL_VERSION,
            "builder_version": corner_history_dataset_builder.BUILDER_VERSION,
            "deployment_policy_version": DEPLOYMENT_POLICY_VERSION,
            "files": [
                {"name": filename, "sha256": _file_hash(directory / filename)}
                for filename in sorted(files)
            ],
        }
    )


def _entry_content_cache_key(
    directory: Path,
    registry: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> str:
    """Bind one daily-use league audit to every byte it is allowed to trust."""

    league_key = str(entry.get("league_key") or "")
    files = {REGISTRY_FILENAME}
    for field, suffix in (
        ("dataset_file", ".csv"),
        ("dataset_manifest_file", ".json"),
        ("source_bundle_file", ".json"),
        ("model_file", ".json"),
        ("evaluation_file", ".json"),
    ):
        files.add(
            _safe_filename(
                entry.get(field),
                f"registry league {league_key}.{field}",
                suffix=suffix,
            )
        )
    return _canonical_hash(
        {
            "directory": str(directory),
            "registry_hash": registry.get("registry_hash"),
            "league_key": league_key,
            "manager_version": MANAGER_VERSION,
            "model_version": corner_model.MODEL_VERSION,
            "builder_version": corner_history_dataset_builder.BUILDER_VERSION,
            "deployment_policy_version": DEPLOYMENT_POLICY_VERSION,
            "files": [
                {"name": filename, "sha256": _file_hash(directory / filename)}
                for filename in sorted(files)
            ],
        }
    )


def _remember_verified_registry(cache_key: str) -> None:
    _VERIFIED_REGISTRY_CACHE[cache_key] = None
    while len(_VERIFIED_REGISTRY_CACHE) > _VERIFIED_REGISTRY_CACHE_LIMIT:
        oldest = next(iter(_VERIFIED_REGISTRY_CACHE))
        _VERIFIED_REGISTRY_CACHE.pop(oldest, None)


def _remember_verified_entry(cache_key: str) -> None:
    _VERIFIED_ENTRY_CACHE[cache_key] = None
    while len(_VERIFIED_ENTRY_CACHE) > _VERIFIED_REGISTRY_CACHE_LIMIT * 4:
        oldest = next(iter(_VERIFIED_ENTRY_CACHE))
        _VERIFIED_ENTRY_CACHE.pop(oldest, None)


def load_registry(
    model_dir: str | Path, *, force_full_replay: bool = False
) -> dict[str, Any]:
    """Load a registry, fully auditing each unique content state at least once.

    Repeated calls in the same process only re-hash the referenced files.  Any
    byte change creates a different cache key and forces source replay, final
    refit and walk-forward replay again.  ``force_full_replay`` is intended for
    explicit audits and CI.
    """

    directory = Path(model_dir).resolve()
    if not directory.is_dir():
        raise CornerModelManagerError(f"model directory does not exist: {directory}")
    registry = _read_json(directory / REGISTRY_FILENAME, "corner model registry")
    validate_registry(registry)
    cache_key = _registry_content_cache_key(directory, registry)
    if force_full_replay or cache_key not in _VERIFIED_REGISTRY_CACHE:
        validate_registry(registry, model_dir=directory)
        _remember_verified_registry(cache_key)
        for entry in registry["leagues"]:
            _remember_verified_entry(
                _entry_content_cache_key(directory, registry, entry)
            )
    return registry


def _resolve_registry_entry(
    registry: Mapping[str, Any], league: str
) -> Mapping[str, Any]:
    if not isinstance(league, str) or not league.strip():
        raise CornerModelManagerError("league is required")
    requested = league.strip().casefold()
    matches = [
        entry
        for entry in registry["leagues"]
        if requested in {alias.casefold() for alias in entry["aliases"]}
    ]
    if not matches:
        raise CornerModelManagerError(f"league is not registered: {league}")
    if len(matches) != 1:
        raise CornerModelManagerError(f"league alias is ambiguous: {league}")
    return matches[0]


def load_registered_league(
    model_dir: str | Path,
    league: str,
    *,
    force_deep_replay: bool = False,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Load and deeply verify only the league needed by a daily operation.

    Registry structure and its self-hash are checked on every call.  The first
    call for a new selected-league content state then replays that league's
    source selection, deterministic final fit, and complete walk-forward
    evaluation.  An in-process content-addressed cache prevents prediction and
    ranking from repeating that expensive replay; changing any selected source,
    dataset, model, evaluation, or registry byte forces it again.

    Full-registry publication/inspection continues to use :func:`load_registry`.
    The copied source bundle is the explicit local trust root: this detects
    coordinated downstream re-hashing while that source remains unchanged, not
    an administrator replacing the trust root itself.
    """

    directory = Path(model_dir).resolve()
    if not directory.is_dir():
        raise CornerModelManagerError(f"model directory does not exist: {directory}")
    registry = _read_json(directory / REGISTRY_FILENAME, "corner model registry")
    validate_registry(registry)
    entry = _resolve_registry_entry(registry, league)
    cache_key = _entry_content_cache_key(directory, registry, entry)
    if force_deep_replay or cache_key not in _VERIFIED_ENTRY_CACHE:
        index = registry["leagues"].index(entry)
        _validate_model_and_evaluation_binding(
            directory,
            entry,
            f"registry.leagues[{index}]",
            replay_cache={},
            replay_all_leagues=False,
        )
        _remember_verified_entry(cache_key)
    return registry, entry


def train_registered_model(
    input_csv: str | Path,
    model_dir: str | Path,
    *,
    league_key: str,
    league: str | None = None,
    aliases: Iterable[str] = (),
    generated_at: str | datetime | None = None,
    half_life_days: float = 365.0,
    iterations: int = 300,
    learning_rate: float = 0.03,
    regularization: float = 0.02,
    min_train_matches: int = 200,
    test_block_size: int = 50,
    unknown_team_policy: str = "error",
    tail_tolerance: float = 1e-8,
    hard_max_corners: int = 80,
    manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fit, evaluate, and atomically add or replace one registered league."""

    if not isinstance(league_key, str) or not LEAGUE_KEY_RE.fullmatch(league_key):
        raise CornerModelManagerError(
            "league_key must contain lowercase letters, digits, and underscores"
        )
    source = Path(input_csv).resolve()
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise CornerModelManagerError(f"cannot read corner dataset: {source}") from exc
    source_manifest = (
        Path(manifest_path).resolve()
        if manifest_path is not None
        else (source.parent / "manifest.json").resolve()
    )
    context = _validate_source_bound_dataset(
        source,
        league_key=league_key,
        manifest_path=source_manifest,
    )
    records = context["records"]
    dataset_hash = context["dataset_hash"]
    manifest_league = context["league_entry"]
    expected_display = str(manifest_league.get("league") or "").strip()
    if not expected_display:
        raise CornerModelManagerError("dataset manifest league display name is missing")
    if isinstance(league, str) and league.strip() and league.strip() != expected_display:
        raise CornerModelManagerError("requested league name does not match dataset manifest")
    manifest_aliases = manifest_league.get("aliases")
    if not isinstance(manifest_aliases, list):
        raise CornerModelManagerError("dataset manifest league aliases are invalid")
    allowed_aliases = {str(value).casefold() for value in manifest_aliases}
    if any(str(value).strip().casefold() not in allowed_aliases for value in aliases):
        raise CornerModelManagerError("requested league alias is not source-bound in manifest")
    display_name = expected_display
    normalized_aliases = _normalize_aliases(
        league_key, display_name, (str(value) for value in manifest_aliases)
    )
    destination = Path(model_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    registry_path = destination / REGISTRY_FILENAME
    existing_entries: list[dict[str, Any]] = []
    if registry_path.exists():
        existing_registry = load_registry(destination)
        existing_entries = [copy.deepcopy(entry) for entry in existing_registry["leagues"]]

    dataset_filename = (
        f"{league_key}-corners-{dataset_hash.removeprefix('sha256:')[:16]}.csv"
    )
    dataset_path = destination / dataset_filename
    if dataset_path.exists():
        if _file_hash(dataset_path) != dataset_hash:
            raise CornerModelManagerError(
                "versioned dataset filename exists with different contents"
            )
    else:
        _atomic_bytes(dataset_path, payload)

    manifest_payload = source_manifest.read_bytes()
    manifest_file_hash = context["manifest_file_sha256"]
    registered_manifest_filename = (
        "corner-dataset-manifest-"
        f"{manifest_file_hash.removeprefix('sha256:')[:16]}.json"
    )
    registered_manifest_path = destination / registered_manifest_filename
    if registered_manifest_path.exists():
        if _file_hash(registered_manifest_path) != manifest_file_hash:
            raise CornerModelManagerError(
                "versioned dataset manifest exists with different contents"
            )
    else:
        _atomic_bytes(registered_manifest_path, manifest_payload)
    source_bundle_payload = context["source_bundle_path"].read_bytes()
    source_file_hash = context["source_file_sha256"]
    registered_source_filename = (
        "corner-history-source-"
        f"{source_file_hash.removeprefix('sha256:')[:16]}.json"
    )
    registered_source_path = destination / registered_source_filename
    if registered_source_path.exists():
        if _file_hash(registered_source_path) != source_file_hash:
            raise CornerModelManagerError(
                "versioned source bundle exists with different contents"
            )
    else:
        _atomic_bytes(registered_source_path, source_bundle_payload)

    model_time = generated_at if generated_at is not None else _utc_now()
    try:
        model = corner_model.fit_model(
            dataset_path,
            half_life_days=half_life_days,
            iterations=iterations,
            learning_rate=learning_rate,
            regularization=regularization,
            generated_at=model_time,
            source_lineage=context["lineage"],
        )
        backtest = corner_model.backtest_model(
            dataset_path,
            min_train_matches=min_train_matches,
            test_block_size=test_block_size,
            half_life_days=half_life_days,
            iterations=iterations,
            learning_rate=learning_rate,
            regularization=regularization,
            unknown_team_policy=unknown_team_policy,
            tail_tolerance=tail_tolerance,
            hard_max_corners=hard_max_corners,
            source_lineage=context["lineage"],
        )
    except corner_model.CornerModelError as exc:
        raise CornerModelManagerError(f"corner training or backtest failed: {exc}") from exc

    evaluation_config = dict(backtest["fit_config"])
    validate_backtest(
        backtest,
        dataset_hash=dataset_hash,
        dataset_rows=len(records),
        dataset_records=records,
        dataset_path=dataset_path,
        source_lineage=context["lineage"],
        expected_config=evaluation_config,
    )
    for evaluation_field, model_field in MODEL_CONFIG_BINDINGS.items():
        if evaluation_config[evaluation_field] != model["config"][model_field]:
            raise CornerModelManagerError(
                "final model and chronological evaluation configurations diverged"
            )

    model_hash = str(model["model_hash"])
    evaluation_hash = str(backtest["backtest_hash"])
    model_filename = (
        f"{league_key}-corner-model-{model_hash.removeprefix('sha256:')[:16]}.json"
    )
    evaluation_filename = (
        f"{league_key}-corner-backtest-"
        f"{evaluation_hash.removeprefix('sha256:')[:16]}.json"
    )
    model_file_hash = _atomic_json(destination / model_filename, model)
    evaluation_file_hash = _atomic_json(destination / evaluation_filename, backtest)
    deployment = derive_historical_deployment(backtest)
    entry: dict[str, Any] = {
        "league_key": league_key,
        "league": display_name,
        "aliases": normalized_aliases,
        "dataset_manifest_file": registered_manifest_filename,
        "dataset_manifest_file_sha256": manifest_file_hash,
        "source_bundle_file": registered_source_filename,
        "source_file_sha256": source_file_hash,
        "source_lineage": copy.deepcopy(context["lineage"]),
        "dataset_file": dataset_filename,
        "dataset_hash": dataset_hash,
        "dataset_rows": len(records),
        "dataset_profile": copy.deepcopy(context["profile"]),
        "training_start": min(row["date"] for row in records).isoformat(),
        "training_cutoff": max(row["date"] for row in records).isoformat(),
        "training_start_kickoff_utc": context["profile"]["kickoff_utc_start"],
        "training_cutoff_kickoff_utc": context["profile"]["kickoff_utc_end"],
        "model_file": model_filename,
        "model_file_sha256": model_file_hash,
        "model_hash": model_hash,
        "model_version": model["model_version"],
        "model_config": copy.deepcopy(model["config"]),
        "evaluation_file": evaluation_filename,
        "evaluation_file_sha256": evaluation_file_hash,
        "evaluation_hash": evaluation_hash,
        "backtest_hash": evaluation_hash,
        "evaluation_config": evaluation_config,
        "deployment": deployment,
        "deployment_status": deployment["deployment_status"],
        "deployment_policy_version": deployment["deployment_policy_version"],
        "formal_corner_total_eligible": False,
        "formal_corner_handicap_eligible": False,
        "formal_corner_ineligible_reason": FORMAL_CORNER_INELIGIBLE_REASON,
    }
    entry["lineage_hash"] = calculate_lineage_hash(entry)
    entries = [item for item in existing_entries if item["league_key"] != league_key]
    entries.append(entry)
    entries.sort(key=lambda item: item["league_key"])
    registry: dict[str, Any] = {
        "artifact_type": REGISTRY_ARTIFACT_TYPE,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "manager_version": MANAGER_VERSION,
        "generated_at": _canonical_datetime(model_time, "generated_at"),
        "deployment_policy": copy.deepcopy(DEPLOYMENT_POLICY),
        "dataset_hashes": {item["league_key"]: item["dataset_hash"] for item in entries},
        "leagues": entries,
    }
    registry["registry_hash"] = calculate_registry_hash(registry)
    # Existing entries were fully verified by load_registry above; the new
    # entry was built from a replayed source bundle and its backtest was just
    # deterministically replayed.  Re-running every historical league here
    # would make sequential multi-league training quadratic.
    validate_registry(registry)
    _atomic_json(registry_path, registry)
    _remember_verified_registry(_registry_content_cache_key(destination, registry))
    _remember_verified_entry(_entry_content_cache_key(destination, registry, entry))
    return registry


def inspect_registry(
    model_dir: str | Path, league: str | None = None
) -> dict[str, Any]:
    """Return a validated, concise provenance and deployment view."""

    registry = load_registry(model_dir)
    entries = (
        [_resolve_registry_entry(registry, league)]
        if league is not None
        else registry["leagues"]
    )
    return {
        "artifact_type": "soccer_corner_model_registry_inspection",
        "schema_version": "1.0.0",
        "generated_at": _utc_now(),
        "registry_hash": registry["registry_hash"],
        "deployment_policy": copy.deepcopy(registry["deployment_policy"]),
        "leagues": [
            {
                key: copy.deepcopy(entry[key])
                for key in (
                    "league_key",
                    "league",
                    "source_lineage",
                    "dataset_hash",
                    "dataset_rows",
                    "dataset_profile",
                    "model_hash",
                    "evaluation_hash",
                    "backtest_hash",
                    "lineage_hash",
                    "training_cutoff",
                    "deployment_status",
                    "formal_corner_total_eligible",
                    "formal_corner_handicap_eligible",
                    "formal_corner_ineligible_reason",
                    "deployment",
                )
            }
            for entry in entries
        ],
    }


def _load_registered_model(
    model_dir: str | Path, entry: Mapping[str, Any]
) -> dict[str, Any]:
    directory = Path(model_dir).resolve()
    filename = _safe_filename(entry.get("model_file"), "model_file", suffix=".json")
    path = directory / filename
    if _file_hash(path) != entry.get("model_file_sha256"):
        raise CornerModelManagerError("registered model file hash does not match")
    try:
        model = corner_model.load_model(path)
    except corner_model.CornerModelError as exc:
        raise CornerModelManagerError(f"registered model is invalid: {exc}") from exc
    if (
        model.get("model_hash") != entry.get("model_hash")
        or model["training"].get("source_data_hash") != entry.get("dataset_hash")
        or model["training"].get("source_lineage") != entry.get("source_lineage")
        or model["training"].get("dataset_profile") != entry.get("dataset_profile")
        or model["training"].get("end_date") != entry.get("training_cutoff")
        or model["training"].get("cutoff_kickoff_utc")
        != entry.get("training_cutoff_kickoff_utc")
    ):
        raise CornerModelManagerError(
            "registered model no longer matches its dataset lineage"
        )
    return model


def validate_registered_prediction(
    prediction: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    model: Mapping[str, Any] | None = None,
) -> None:
    """Validate a prediction's model, registry, evaluation, and deployment binding."""

    if model is None:
        raise CornerModelManagerError(
            "registered prediction validation requires the bound registered model"
        )
    validate_registry(registry)
    binding = prediction.get("registry_binding")
    if not isinstance(binding, Mapping):
        raise CornerModelManagerError("prediction registry_binding is missing")
    league_key = binding.get("league_key")
    if not isinstance(league_key, str):
        raise CornerModelManagerError("prediction league_key is missing")
    entry = _resolve_registry_entry(registry, league_key)
    expected_binding = {
        "registry_hash": registry["registry_hash"],
        "league_key": entry["league_key"],
        "dataset_hash": entry["dataset_hash"],
        "source_lineage_hash": _canonical_hash(entry["source_lineage"]),
        "fixture_set_hash": entry["dataset_profile"]["fixture_set_hash"],
        "response_set_hash": entry["dataset_profile"]["response_set_hash"],
        "fixture_graph_hash": entry["dataset_profile"]["fixture_graph"][
            "components_hash"
        ],
        "model_hash": entry["model_hash"],
        "evaluation_hash": entry["evaluation_hash"],
        "backtest_hash": entry["backtest_hash"],
        "lineage_hash": entry["lineage_hash"],
        "training_cutoff": entry["training_cutoff"],
        "training_cutoff_kickoff_utc": entry["training_cutoff_kickoff_utc"],
    }
    if dict(binding) != expected_binding:
        raise CornerModelManagerError(
            "prediction registry binding does not match the selected lineage"
        )
    expected_deployment = copy.deepcopy(entry["deployment"])
    if prediction.get("registered_deployment") != expected_deployment:
        raise CornerModelManagerError(
            "prediction deployment state does not match verified evaluation"
        )
    for field in (
        "deployment_status",
        "deployment_policy_version",
        "formal_corner_total_eligible",
        "formal_corner_handicap_eligible",
        "formal_corner_ineligible_reason",
    ):
        if prediction.get(field) != entry.get(field):
            raise CornerModelManagerError(
                f"prediction {field} does not match the registry entry"
            )
    stored_prediction_hash = prediction.get("prediction_hash")
    if (
        not isinstance(stored_prediction_hash, str)
        or stored_prediction_hash != corner_model.calculate_prediction_hash(prediction)
    ):
        raise CornerModelManagerError(
            "registered corner prediction_hash does not match contents"
        )
    # The core validator deliberately has no API that can grant registered
    # authority.  Validate an observation-only semantic view there, then bind
    # the original authority fields exclusively against this verified registry.
    core_view = copy.deepcopy(dict(prediction))
    core_usage = core_view.get("usage_policy")
    if not isinstance(core_usage, dict):
        raise CornerModelManagerError("prediction usage_policy is missing")
    core_usage["source_bound_manager_verified"] = False
    core_usage["eligible_for_formal_model_input"] = False
    core_usage["status"] = "observation_only"
    core_usage["formal_ineligible_reason"] = (
        "standalone model output has not been verified against its registered "
        "source-bound dataset and evaluation"
    )
    core_view["prediction_hash"] = corner_model.calculate_prediction_hash(core_view)
    try:
        corner_model.validate_prediction(core_view, model=model)
    except corner_model.CornerModelError as exc:
        raise CornerModelManagerError(f"corner prediction is invalid: {exc}") from exc
    if prediction.get("model_hash") != entry.get("model_hash"):
        raise CornerModelManagerError("prediction model_hash does not match registry")
    provenance = prediction.get("provenance")
    fixture = prediction.get("fixture")
    if (
        not isinstance(provenance, Mapping)
        or not isinstance(fixture, Mapping)
        or fixture.get("league_key") != entry.get("league_key")
        or provenance.get("training_source_data_hash") != entry.get("dataset_hash")
        or provenance.get("training_source_lineage") != entry.get("source_lineage")
        or provenance.get("training_dataset_profile") != entry.get("dataset_profile")
        or provenance.get("training_cutoff_date") != entry.get("training_cutoff")
        or provenance.get("training_cutoff_kickoff_utc")
        != entry.get("training_cutoff_kickoff_utc")
    ):
        raise CornerModelManagerError(
            "prediction provenance does not match registered dataset lineage"
        )
    usage = prediction.get("usage_policy")
    if not isinstance(usage, Mapping):
        raise CornerModelManagerError("prediction usage_policy is missing")
    if (
        usage.get("formal_corner_total_eligible") is not False
        or usage.get("formal_corner_handicap_eligible") is not False
        or usage.get("formal_corner_ineligible_reason")
        != FORMAL_CORNER_INELIGIBLE_REASON
    ):
        raise CornerModelManagerError(
            "prediction usage policy exceeds historical deployment authority"
        )
    fixture = prediction.get("fixture")
    fallback_used = bool(
        fixture.get("unknown_teams") if isinstance(fixture, Mapping) else None
    )
    expected_status = (
        "observation_only" if fallback_used else "registered_model_distribution"
    )
    if (
        usage.get("source_bound_manager_verified") is not True
        or usage.get("known_team_model_input") is not (not fallback_used)
        or usage.get("eligible_for_formal_model_input") is not (not fallback_used)
        or usage.get("status") != expected_status
    ):
        raise CornerModelManagerError(
            "prediction usage policy is not bound to manager verification"
        )


def predict_registered_model(
    model_dir: str | Path,
    league: str,
    home_team: str,
    away_team: str,
    *,
    kickoff: str | datetime,
    generated_at: str | datetime | None = None,
    unknown_team_policy: str = "error",
    tail_tolerance: float = 1e-8,
    hard_max_corners: int = 80,
    total_markets: Iterable[tuple[str, float]] = (),
    corner_handicaps: Iterable[tuple[str, float]] = (),
) -> dict[str, Any]:
    """Predict with a fully verified registered corner model."""

    registry, entry = load_registered_league(model_dir, league)
    model = _load_registered_model(model_dir, entry)
    try:
        prediction = corner_model.predict_model(
            model,
            home_team,
            away_team,
            kickoff=kickoff,
            generated_at=generated_at,
            unknown_team_policy=unknown_team_policy,
            tail_tolerance=tail_tolerance,
            hard_max_corners=hard_max_corners,
            total_markets=total_markets,
            corner_handicaps=corner_handicaps,
        )
    except corner_model.CornerModelError as exc:
        raise CornerModelManagerError(f"registered corner prediction failed: {exc}") from exc
    prediction["registry_binding"] = {
        "registry_hash": registry["registry_hash"],
        "league_key": entry["league_key"],
        "dataset_hash": entry["dataset_hash"],
        "source_lineage_hash": _canonical_hash(entry["source_lineage"]),
        "fixture_set_hash": entry["dataset_profile"]["fixture_set_hash"],
        "response_set_hash": entry["dataset_profile"]["response_set_hash"],
        "fixture_graph_hash": entry["dataset_profile"]["fixture_graph"][
            "components_hash"
        ],
        "model_hash": entry["model_hash"],
        "evaluation_hash": entry["evaluation_hash"],
        "backtest_hash": entry["backtest_hash"],
        "lineage_hash": entry["lineage_hash"],
        "training_cutoff": entry["training_cutoff"],
        "training_cutoff_kickoff_utc": entry["training_cutoff_kickoff_utc"],
    }
    prediction["registered_deployment"] = copy.deepcopy(entry["deployment"])
    for field in (
        "deployment_status",
        "deployment_policy_version",
        "formal_corner_total_eligible",
        "formal_corner_handicap_eligible",
        "formal_corner_ineligible_reason",
    ):
        prediction[field] = copy.deepcopy(entry[field])
    prediction["usage_policy"]["formal_corner_total_eligible"] = False
    prediction["usage_policy"]["formal_corner_handicap_eligible"] = False
    prediction["usage_policy"]["formal_corner_ineligible_reason"] = (
        FORMAL_CORNER_INELIGIBLE_REASON
    )
    fallback_used = prediction["usage_policy"]["unknown_team_fallback_used"] is True
    prediction["usage_policy"]["source_bound_manager_verified"] = True
    prediction["usage_policy"]["known_team_model_input"] = not fallback_used
    prediction["usage_policy"]["eligible_for_formal_model_input"] = not fallback_used
    prediction["usage_policy"]["status"] = (
        "observation_only" if fallback_used else "registered_model_distribution"
    )
    prediction["usage_policy"]["formal_ineligible_reason"] = (
        FORMAL_CORNER_INELIGIBLE_REASON
    )
    prediction["prediction_hash"] = corner_model.calculate_prediction_hash(prediction)
    validate_registered_prediction(prediction, registry, model=model)
    return prediction


def _parse_market(raw: str, name: str, sides: set[str]) -> tuple[str, float]:
    try:
        side, raw_line = raw.split(":", 1)
        side = side.strip().lower()
        line = float(raw_line)
    except (AttributeError, ValueError) as exc:
        raise CornerModelManagerError(f"{name} must be SIDE:LINE") from exc
    if side not in sides or not math.isfinite(line):
        raise CornerModelManagerError(f"invalid {name}: {raw}")
    units = round(line * 4.0)
    if abs(line * 4.0 - units) > 1e-8:
        raise CornerModelManagerError(f"{name} line must be a multiple of 0.25")
    return side, line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    train = subparsers.add_parser(
        "train", help="fit, backtest, and register one league corner model"
    )
    train.add_argument("--input", required=True)
    train.add_argument(
        "--manifest",
        help="source-bound v2 manifest.json (defaults to the input CSV directory)",
    )
    train.add_argument("--model-dir", required=True)
    train.add_argument("--league-key", required=True)
    train.add_argument("--league")
    train.add_argument("--alias", action="append", default=[])
    train.add_argument("--generated-at")
    train.add_argument("--half-life-days", type=float, default=365.0)
    train.add_argument("--iterations", type=int, default=300)
    train.add_argument("--learning-rate", type=float, default=0.03)
    train.add_argument("--regularization", type=float, default=0.02)
    train.add_argument("--min-train-matches", type=int, default=200)
    train.add_argument("--test-block-size", type=int, default=50)
    train.add_argument(
        "--unknown-team-policy",
        choices=("error", "league_average"),
        default="error",
    )
    train.add_argument("--tail-tolerance", type=float, default=1e-8)
    train.add_argument("--hard-max-corners", type=int, default=80)

    inspect_command = subparsers.add_parser(
        "inspect", help="verify and display registered lineage and deployment"
    )
    inspect_command.add_argument("--model-dir", required=True)
    inspect_command.add_argument("--league")
    inspect_command.add_argument("--output")

    predict = subparsers.add_parser(
        "predict", help="predict with one verified registered corner model"
    )
    predict.add_argument("--model-dir", required=True)
    predict.add_argument("--league", required=True)
    predict.add_argument("--home-team", required=True)
    predict.add_argument("--away-team", required=True)
    predict.add_argument("--kickoff", required=True)
    predict.add_argument("--generated-at")
    predict.add_argument("--output", required=True)
    predict.add_argument(
        "--unknown-team-policy",
        choices=("error", "league_average"),
        default="error",
    )
    predict.add_argument("--tail-tolerance", type=float, default=1e-8)
    predict.add_argument("--hard-max-corners", type=int, default=80)
    predict.add_argument("--total", action="append", default=[])
    predict.add_argument("--handicap", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "train":
            registry = train_registered_model(
                args.input,
                args.model_dir,
                league_key=args.league_key,
                league=args.league,
                aliases=args.alias,
                generated_at=args.generated_at,
                half_life_days=args.half_life_days,
                iterations=args.iterations,
                learning_rate=args.learning_rate,
                regularization=args.regularization,
                min_train_matches=args.min_train_matches,
                test_block_size=args.test_block_size,
                unknown_team_policy=args.unknown_team_policy,
                tail_tolerance=args.tail_tolerance,
                hard_max_corners=args.hard_max_corners,
                manifest_path=args.manifest,
            )
            corner_model.save_json(registry, None)
            return 0
        if args.command == "inspect":
            inspection = inspect_registry(args.model_dir, args.league)
            corner_model.save_json(inspection, args.output)
            return 0
        totals = [
            _parse_market(value, "corner total", {"over", "under"})
            for value in args.total
        ]
        handicaps = [
            _parse_market(value, "corner handicap", {"home", "away"})
            for value in args.handicap
        ]
        prediction = predict_registered_model(
            args.model_dir,
            args.league,
            args.home_team,
            args.away_team,
            kickoff=args.kickoff,
            generated_at=args.generated_at,
            unknown_team_policy=args.unknown_team_policy,
            tail_tolerance=args.tail_tolerance,
            hard_max_corners=args.hard_max_corners,
            total_markets=totals,
            corner_handicaps=handicaps,
        )
        corner_model.save_json(prediction, args.output)
        return 0
    except CornerModelManagerError as exc:
        parser.exit(2, f"corner_model_manager: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

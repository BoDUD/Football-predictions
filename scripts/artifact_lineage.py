#!/usr/bin/env python3
"""Build the role-aware data/model lineage frozen into a forward policy."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

PREVIOUS_SCHEMA_VERSION = "forward-artifact-lineage/1.1.0"
SCHEMA_VERSION = "forward-artifact-lineage/1.2.0"
DATA_ROLES = ("football_history", "corner_history")
MODEL_ROLES = ("football_htft", "corner")
MODEL_DATA_ROLE = {
    "football_htft": "football_history",
    "corner": "corner_history",
}


class ArtifactLineageError(ValueError):
    """Raised when a model registry is not bound to its intended dataset role."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _require_hash(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise ArtifactLineageError(f"{label} must be a lowercase SHA-256 identity")
    return text


def _declared_hash(payload: Mapping[str, Any], label: str) -> str:
    for field in ("bundle_hash", "registry_hash", "manifest_hash", "dataset_hash"):
        if payload.get(field) is not None:
            return _require_hash(payload[field], f"{label}.{field}")
    raise ArtifactLineageError(f"{label} has no declared content identity")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactLineageError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise ArtifactLineageError(f"{label} must be a JSON object")
    return value


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _corner_manifest_dataset_hashes(
    manifest: Mapping[str, Any], *, schema_version: str
) -> dict[str, str]:
    leagues = manifest.get("leagues")
    if not isinstance(leagues, list) or not leagues:
        raise ArtifactLineageError("corner manifest leagues are missing")
    result: dict[str, str] = {}
    for index, item in enumerate(leagues):
        if not isinstance(item, Mapping):
            raise ArtifactLineageError(f"corner manifest leagues[{index}] is invalid")
        key = str(item.get("league_key") or "")
        field = (
            "dataset_hash"
            if schema_version == PREVIOUS_SCHEMA_VERSION
            else "dataset_sha256"
        )
        dataset_hash = _require_hash(
            item.get(field), f"corner manifest leagues[{index}].{field}"
        )
        if not key or key in result:
            raise ArtifactLineageError("corner manifest league keys are invalid")
        result[key] = dataset_hash
    return result


def _corner_registry_leagues(
    registry: Mapping[str, Any], *, schema_version: str
) -> dict[str, Mapping[str, Any]]:
    raw_leagues = registry.get("leagues")
    if schema_version == PREVIOUS_SCHEMA_VERSION:
        if not isinstance(raw_leagues, Mapping) or not raw_leagues:
            raise ArtifactLineageError("corner registry leagues are missing")
        result: dict[str, Mapping[str, Any]] = {}
        for raw_key, item in raw_leagues.items():
            key = str(raw_key or "")
            if not key or key in result or not isinstance(item, Mapping):
                raise ArtifactLineageError("corner registry league keys are invalid")
            result[key] = item
        return result
    if not isinstance(raw_leagues, list) or not raw_leagues:
        raise ArtifactLineageError("corner registry leagues are missing")
    result: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(raw_leagues):
        if not isinstance(item, Mapping):
            raise ArtifactLineageError(f"corner registry leagues[{index}] is invalid")
        key = str(item.get("league_key") or "")
        if not key or key in result:
            raise ArtifactLineageError("corner registry league keys are invalid")
        result[key] = item
    return result


def _validate_registry_dataset_link(
    role: str,
    registry: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    manifest_hash: str,
    schema_version: str,
) -> dict[str, Any]:
    if role == "football_htft":
        if registry.get("dataset_manifest_hash") != manifest_hash:
            raise ArtifactLineageError(
                "football HTFT registry does not bind the football history manifest"
            )
        return {"dataset_manifest_hash": manifest_hash}
    if role != "corner":
        raise ArtifactLineageError(f"unsupported model role: {role}")
    expected_dataset_hashes = _corner_manifest_dataset_hashes(
        manifest, schema_version=schema_version
    )
    raw_dataset_hashes = registry.get("dataset_hashes")
    if (
        not isinstance(raw_dataset_hashes, Mapping)
        or {str(key): str(value) for key, value in raw_dataset_hashes.items()}
        != expected_dataset_hashes
    ):
        raise ArtifactLineageError(
            "corner registry dataset_hashes do not reproduce the corner manifest"
        )
    leagues = _corner_registry_leagues(registry, schema_version=schema_version)
    if set(leagues) != set(expected_dataset_hashes):
        raise ArtifactLineageError("corner registry league coverage is incomplete")
    for key, item in leagues.items():
        lineage = item.get("source_lineage") if isinstance(item, Mapping) else None
        if (
            not isinstance(lineage, Mapping)
            or lineage.get("manifest_bundle_hash") != manifest_hash
        ):
            raise ArtifactLineageError(
                f"corner registry league {key} does not bind the corner manifest"
            )
        if lineage.get("dataset_hash") != expected_dataset_hashes[str(key)]:
            raise ArtifactLineageError(
                f"corner registry league {key} does not bind its dataset"
            )
    return {
        "dataset_manifest_hash": manifest_hash,
        "dataset_hashes": expected_dataset_hashes,
    }


def _registered_models(
    role: str, registry: Mapping[str, Any], *, schema_version: str
) -> dict[str, dict[str, str]]:
    raw_leagues = registry.get("leagues")
    if role == "football_htft":
        if not isinstance(raw_leagues, list) or not raw_leagues:
            raise ArtifactLineageError("football registry leagues are missing")
        iterable = []
        for index, item in enumerate(raw_leagues):
            if not isinstance(item, Mapping):
                raise ArtifactLineageError(
                    f"football registry leagues[{index}] is invalid"
                )
            iterable.append((str(item.get("league_key") or ""), item))
        required = ("model_hash", "full_time_component_model_hash")
    elif role == "corner":
        iterable = list(
            _corner_registry_leagues(registry, schema_version=schema_version).items()
        )
        required = ("model_hash", "dataset_hash")
    else:
        raise ArtifactLineageError(f"unsupported model role: {role}")
    result: dict[str, dict[str, str]] = {}
    for key, item in iterable:
        if not key or key in result or not isinstance(item, Mapping):
            raise ArtifactLineageError(f"{role} registered league keys are invalid")
        result[key] = {
            field: _require_hash(item.get(field), f"{role}.{key}.{field}")
            for field in required
        }
    return result


def _build_lineage(
    *,
    repo_root: str | Path,
    data_manifests: Mapping[str, str | Path],
    model_registries: Mapping[str, str | Path],
    schema_version: str,
) -> dict[str, Any]:
    if schema_version not in {PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise ArtifactLineageError("unsupported artifact lineage schema_version")
    root = Path(repo_root).resolve()
    if set(data_manifests) != set(DATA_ROLES):
        raise ArtifactLineageError(
            f"data manifest roles must be exactly {list(DATA_ROLES)}"
        )
    if set(model_registries) != set(MODEL_ROLES):
        raise ArtifactLineageError(
            f"model registry roles must be exactly {list(MODEL_ROLES)}"
        )
    data_blocks: dict[str, dict[str, Any]] = {}
    data_payloads: dict[str, dict[str, Any]] = {}
    for role in DATA_ROLES:
        path = Path(data_manifests[role]).resolve()
        payload = _read_json(path, f"{role} data manifest")
        declared = _declared_hash(payload, f"{role} data manifest")
        data_payloads[role] = payload
        data_blocks[role] = {
            "role": role,
            "manifest_path": _relative(path, root),
            "file_sha256": _hash_file(path),
            "declared_manifest_hash": declared,
            "schema_version": payload.get("schema_version"),
            "as_of_date": payload.get("as_of_date"),
        }
    model_blocks: dict[str, dict[str, Any]] = {}
    for role in MODEL_ROLES:
        path = Path(model_registries[role]).resolve()
        payload = _read_json(path, f"{role} model registry")
        declared = _declared_hash(payload, f"{role} model registry")
        data_role = MODEL_DATA_ROLE[role]
        links = _validate_registry_dataset_link(
            role,
            payload,
            manifest=data_payloads[data_role],
            manifest_hash=data_blocks[data_role]["declared_manifest_hash"],
            schema_version=schema_version,
        )
        model_blocks[role] = {
            "role": role,
            "dataset_role": data_role,
            "registry_path": _relative(path, root),
            "file_sha256": _hash_file(path),
            "declared_registry_hash": declared,
            "schema_version": payload.get("schema_version"),
            "validated_training_config": deepcopy(
                payload.get("validated_training_config")
            ),
            "registered_models": _registered_models(
                role, payload, schema_version=schema_version
            ),
            **links,
        }
    value: dict[str, Any] = {
        "schema_version": schema_version,
        "artifact_type": "soccer_forward_artifact_lineage",
        "data_manifests": data_blocks,
        "model_registries": model_blocks,
        "candidate_role_policy": {
            "score_model": ["football_history", "football_htft"],
            "htft": ["football_history", "football_htft"],
            "corner_total": ["corner_history", "corner"],
            "corner_handicap": ["corner_history", "corner"],
        },
    }
    value["lineage_hash"] = _hash_json(value)
    return value


def build_lineage(
    *,
    repo_root: str | Path,
    data_manifests: Mapping[str, str | Path],
    model_registries: Mapping[str, str | Path],
) -> dict[str, Any]:
    return _build_lineage(
        repo_root=repo_root,
        data_manifests=data_manifests,
        model_registries=model_registries,
        schema_version=SCHEMA_VERSION,
    )


def validate_lineage(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ArtifactLineageError("artifact lineage must be an object")
    value = deepcopy(dict(raw))
    supplied = value.pop("lineage_hash", None)
    if supplied != _hash_json(value):
        raise ArtifactLineageError("artifact lineage hash is invalid")
    if set(value) != {
        "schema_version",
        "artifact_type",
        "data_manifests",
        "model_registries",
        "candidate_role_policy",
    }:
        raise ArtifactLineageError("artifact lineage fields are incomplete")
    if value.get("schema_version") not in {PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise ArtifactLineageError("unsupported artifact lineage schema_version")
    if value.get("artifact_type") != "soccer_forward_artifact_lineage":
        raise ArtifactLineageError("artifact lineage artifact_type is invalid")
    data = value.get("data_manifests")
    models = value.get("model_registries")
    if not isinstance(data, Mapping) or set(data) != set(DATA_ROLES):
        raise ArtifactLineageError("artifact lineage data roles are incomplete")
    if not isinstance(models, Mapping) or set(models) != set(MODEL_ROLES):
        raise ArtifactLineageError("artifact lineage model roles are incomplete")
    for role, block in data.items():
        if not isinstance(block, Mapping) or block.get("role") != role:
            raise ArtifactLineageError(f"data lineage role {role} is invalid")
        _require_hash(block.get("file_sha256"), f"data {role}.file_sha256")
        _require_hash(
            block.get("declared_manifest_hash"),
            f"data {role}.declared_manifest_hash",
        )
    for role, block in models.items():
        if (
            not isinstance(block, Mapping)
            or block.get("role") != role
            or block.get("dataset_role") != MODEL_DATA_ROLE[role]
        ):
            raise ArtifactLineageError(f"model lineage role {role} is invalid")
        _require_hash(block.get("file_sha256"), f"model {role}.file_sha256")
        _require_hash(
            block.get("declared_registry_hash"),
            f"model {role}.declared_registry_hash",
        )
        if block.get("dataset_manifest_hash") != data[MODEL_DATA_ROLE[role]].get(
            "declared_manifest_hash"
        ):
            raise ArtifactLineageError(
                f"model role {role} is not linked to its data role"
            )
        registered = block.get("registered_models")
        if not isinstance(registered, Mapping) or not registered:
            raise ArtifactLineageError(
                f"model role {role} registered_models are missing"
            )
        required_model_fields = (
            ("model_hash", "full_time_component_model_hash")
            if role == "football_htft"
            else ("model_hash", "dataset_hash")
        )
        for league_key, model in registered.items():
            if not str(league_key) or not isinstance(model, Mapping):
                raise ArtifactLineageError(
                    f"model role {role} registered_models are invalid"
                )
            if set(model) != set(required_model_fields):
                raise ArtifactLineageError(
                    f"model role {role} registered model fields are invalid"
                )
            for field in required_model_fields:
                _require_hash(model.get(field), f"{role}.{league_key}.{field}")
    expected_role_policy = {
        "score_model": ["football_history", "football_htft"],
        "htft": ["football_history", "football_htft"],
        "corner_total": ["corner_history", "corner"],
        "corner_handicap": ["corner_history", "corner"],
    }
    if value.get("candidate_role_policy") != expected_role_policy:
        raise ArtifactLineageError("candidate artifact role policy is invalid")
    value["lineage_hash"] = _require_hash(supplied, "lineage_hash")
    return value


def verify_files(raw: Any, *, repo_root: str | Path) -> dict[str, Any]:
    value = validate_lineage(raw)
    root = Path(repo_root).resolve()
    resolved_data: dict[str, Path] = {}
    resolved_models: dict[str, Path] = {}
    for group, path_field in (
        ("data_manifests", "manifest_path"),
        ("model_registries", "registry_path"),
    ):
        for role, block in value[group].items():
            path = Path(str(block.get(path_field) or ""))
            if not path.is_absolute():
                path = root / path
            if not path.is_file() or _hash_file(path) != block.get("file_sha256"):
                raise ArtifactLineageError(
                    f"frozen {group}.{role} artifact is missing or changed"
                )
            target = resolved_data if group == "data_manifests" else resolved_models
            target[str(role)] = path.resolve()
    rebuilt = _build_lineage(
        repo_root=root,
        data_manifests=resolved_data,
        model_registries=resolved_models,
        schema_version=str(value["schema_version"]),
    )
    if rebuilt != value:
        raise ArtifactLineageError(
            "frozen artifact lineage does not reproduce from its registered files"
        )
    return value

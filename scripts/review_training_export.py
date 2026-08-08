#!/usr/bin/env python3
"""Validate and export immutable post-match samples for the next model cohort."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    from scripts import forward_policy, memory_store
except ImportError:  # Direct execution from scripts/.
    import forward_policy  # type: ignore[no-redef]
    import memory_store  # type: ignore[no-redef]

BUNDLE_SCHEMA_VERSION = "review-training-bundle/1.0.0"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sample_payload(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value) for key, value in sample.items() if key != "sample_hash"
    }


def validate_sample(sample: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    if sample.get("schema_version") != "review-training-sample/1.0.0":
        raise ValueError("review training sample schema is unsupported")
    if sample.get("sample_hash") != forward_policy._hash_json(_sample_payload(sample)):
        raise ValueError("review training sample hash is invalid")
    fixture = sample.get("fixture")
    actual = sample.get("actual")
    evidence = sample.get("result_evidence")
    if (
        not isinstance(fixture, dict)
        or not isinstance(actual, dict)
        or not isinstance(evidence, dict)
    ):
        raise ValueError("review training sample is incomplete")
    if (
        str(fixture.get("match_id") or "") != str(record.get("match_id") or "")
        or actual.get("full_time_score") != record.get("final_score")
        or evidence != record.get("result_verification")
        or sample.get("reviewed_at") != record.get("reviewed_at")
        or sample.get("official_primary") != record.get("official_primary")
        or sample.get("official_primary_settlement")
        != record.get("official_primary_settlement")
        or sample.get("training_scope") != "next_closed_cohort_only"
        or sample.get("mutates_active_models") is not False
    ):
        raise ValueError("review training sample no longer matches its reviewed record")
    if record.get("forward_policy_binding") is not None:
        memory_store.validate_forward_record_prediction_commitment(record)
    memory_store.validated_official_primary(record)
    official_settlement = sample.get("official_primary_settlement")
    if isinstance(official_settlement, dict):
        settlement_payload = {
            key: deepcopy(value)
            for key, value in official_settlement.items()
            if key != "settlement_hash"
        }
        if official_settlement.get("settlement_hash") != forward_policy._hash_json(
            settlement_payload
        ):
            raise ValueError("official primary settlement hash is invalid")
    frozen_audit = memory_store._validated_current_candidate_audit(record)
    sample_audit = sample.get("candidate_evaluation")
    if isinstance(frozen_audit, dict):
        if not isinstance(sample_audit, dict) or any(
            sample_audit.get(field) != frozen_audit.get(source_field)
            for field, source_field in (
                ("audit_hash", "audit_hash"),
                ("observation_id", "observation_id"),
                ("market_manifest", "market_manifest"),
                ("candidates", "candidates"),
            )
        ):
            raise ValueError(
                "review training candidate snapshot is not frozen audit data"
            )
    elif sample_audit is not None:
        raise ValueError("review training sample contains an unbound candidate audit")
    return sample


def build_bundle(
    history: list[dict[str, Any]], *, cohort_id: str, closure: dict[str, Any]
) -> dict[str, Any]:
    forward_policy.validate_closure(closure, require_record_manifest=True)
    if closure.get("cohort_id") != cohort_id:
        raise ValueError("cohort closure does not match requested cohort_id")
    samples: list[dict[str, Any]] = []
    fixture_ids: set[str] = set()
    for record in history:
        sample = record.get("review_training_sample")
        if not isinstance(sample, dict) or sample.get("cohort_id") != cohort_id:
            continue
        validate_sample(sample, record)
        fixture_id = str(sample.get("fixture", {}).get("match_id") or "")
        if not fixture_id or fixture_id in fixture_ids:
            raise ValueError("review training bundle contains a duplicate fixture")
        fixture_ids.add(fixture_id)
        samples.append(deepcopy(sample))
    samples.sort(
        key=lambda item: (
            str(item.get("fixture", {}).get("kickoff") or ""),
            str(item.get("fixture", {}).get("match_id") or ""),
        )
    )
    if not samples:
        raise ValueError("closed cohort has no validated review training samples")
    bundle: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "cohort_id": cohort_id,
        "cohort_closure_hash": closure.get("closure_hash"),
        "sample_count": len(samples),
        "fixture_ids": [str(item["fixture"]["match_id"]) for item in samples],
        "samples": samples,
        "training_policy": {
            "target": "next_model_version_only",
            "active_cohort_mutation_allowed": False,
            "selected_direction_only_training": False,
            "includes_all_candidate_distributions": True,
        },
    }
    bundle["bundle_hash"] = forward_policy._hash_json(bundle)
    return bundle


def _write_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError(f"refusing to overwrite existing training bundle: {path}")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--cohort-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    base_dir = Path(args.base_dir).resolve()
    history = memory_store.load_history(memory_store.data_path(base_dir))
    closure_path = forward_policy.cohort_closure_path(base_dir, args.cohort_id)
    if not closure_path.is_file():
        raise ValueError("training export requires an immutable closed cohort")
    active_path = forward_policy.active_cohort_path(base_dir)
    if active_path.is_file():
        pointer = _read_json(active_path)
        if (
            isinstance(pointer, dict)
            and pointer.get("cohort_id") == args.cohort_id
            and pointer.get("status") == "active"
        ):
            raise ValueError(
                "training export is blocked until the active cohort pointer is closed"
            )
    closure = _read_json(closure_path)
    bundle = build_bundle(history, cohort_id=args.cohort_id, closure=closure)
    _write_exclusive(Path(args.output).resolve(), bundle)
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(Path(args.output).resolve()),
                "sample_count": bundle["sample_count"],
                "bundle_hash": bundle["bundle_hash"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

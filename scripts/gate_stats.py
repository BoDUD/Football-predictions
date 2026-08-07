#!/usr/bin/env python3
"""Replay and summarize recent candidate-evaluation gate diagnostics.

This module is intentionally separate from performance statistics.  It includes
pending and reviewed pre-match archive versions, never settles a selection, and
does not change model, market-policy, calibration, or primary-record state.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:  # Imported as ``scripts.gate_stats`` from the repository root.
    from scripts import memory_store
except ImportError:  # Invoked directly as ``scripts/gate_stats.py``.
    import memory_store  # type: ignore[no-redef]


SCHEMA_VERSION = "recent-candidate-gate-funnels/1.0.0"
EVALUATION_SCOPE = "descriptive_prematch_gate_diagnostics_not_performance"
WINDOW_BASIS = "distinct_matches_by_latest_prematch_version_archived_at"
VERSION_UNIT = "immutable_prematch_archive_version"
MARKET_UNIT = "archive_version_market"
GATE_UNIT = "candidate_direction_gate"
STAGES = ("initial", "lineup-check")
OUTCOMES = (
    "formal_available",
    "observation_available",
    "nonrelease_blocked",
    "unavailable",
)
BLOCKER_CLASSES = ("data", "value", "policy", "safety")

_DATA_GATES = frozenset(
    {
        "canonical_model_binding",
        "odds_provenance",
        "complete_current_market",
        "bookmaker_depth",
        "data_quality",
        "market_signal_classified",
        "market_specific_evidence",
    }
)
_VALUE_GATES = frozenset({"positive_ev", "positive_edge"})
_SAFETY_GATES = frozenset({"adverse_signal_gate"})


def blocker_class_for_gate(gate: Mapping[str, Any]) -> str:
    """Map a replayed gate to a stable operational blocker class.

    The mapping is presentation-only.  It never changes the archived gate's
    original ``category`` or its eligibility semantics.
    """

    name = str(gate.get("gate") or "")
    category = str(gate.get("category") or "")
    if name in _DATA_GATES or category == "integrity":
        return "data"
    if name in _VALUE_GATES or category == "value":
        return "value"
    if category == "release":
        return "policy"
    if name in _SAFETY_GATES:
        return "safety"
    # Unknown future risk gates remain visible without silently classifying
    # them as missing data or market value.
    return "safety"


def _counter() -> Counter[str]:
    return Counter()


def _new_diagnostic_block() -> dict[str, Any]:
    return {
        "_observations": set(),
        "market_versions": 0,
        "evaluated_market_versions": 0,
        "unavailable_market_versions": 0,
        "candidates": 0,
        "outcomes": Counter(),
        "unavailable_reasons": Counter(),
        "gate_funnel": {},
        "blocker_classes": {
            blocker_class: {
                "failed_gate_evaluations": 0,
                "_candidate_ids": set(),
                "_market_versions": set(),
                "gates": Counter(),
                "failure_reasons": Counter(),
            }
            for blocker_class in BLOCKER_CLASSES
        },
    }


def _new_coverage_block() -> dict[str, Any]:
    return {
        "archive_versions_total": 0,
        "replayable_v3_versions": 0,
        "excluded_versions": 0,
        "excluded_by_reason": Counter(),
        "record_statuses": Counter(),
    }


def _version_stage(version: Mapping[str, Any]) -> str:
    basis = version.get("settlement_basis")
    raw = (
        basis.get("analysis_stage")
        if isinstance(basis, Mapping)
        else version.get("analysis_stage")
    )
    return str(raw or "initial")


def _version_archived_at(version: Mapping[str, Any]) -> datetime | None:
    basis = version.get("settlement_basis")
    raw = None
    if isinstance(basis, Mapping):
        raw = basis.get("version_archived_at") or basis.get("archived_at")
    raw = (
        raw
        or version.get("archived_at")
        or version.get("updated_at")
        or version.get("created_at")
    )
    try:
        return memory_store.parse_aware_datetime(
            str(raw or ""), "candidate gate archive version time"
        )
    except (TypeError, ValueError):
        return None


def _prematch_versions(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    if record.get("mode") != "prematch":
        return []
    versions = [
        item
        for item in record.get("revisions", [])
        if isinstance(item, dict) and _version_stage(item) in STAGES
    ]
    if _version_stage(record) in STAGES:
        versions.append(dict(record))
    return versions


def _candidate_evaluation_audits(
    version: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current: list[dict[str, Any]] = []
    legacy: list[dict[str, Any]] = []
    for audit in memory_store.frozen_candidate_audits(version):
        if audit.get("kind") != memory_store.CANDIDATE_EVALUATION_KIND:
            continue
        if (
            audit.get("schema_version")
            == memory_store.CANDIDATE_EVALUATION_SCHEMA_VERSION
        ):
            current.append(audit)
        else:
            legacy.append(audit)
    return current, legacy


def _add_gate(
    block: dict[str, Any],
    gate: Mapping[str, Any],
    *,
    candidate_id: str,
    market_version_key: str,
) -> None:
    name = str(gate.get("gate") or "")
    if not name:
        return
    raw = block["gate_funnel"].setdefault(
        name,
        {
            "categories": set(),
            "blocker_class": blocker_class_for_gate(gate),
            "evaluated": 0,
            "passed": 0,
            "failed": 0,
            "failure_reasons": Counter(),
        },
    )
    raw["categories"].add(str(gate.get("category") or "unknown"))
    raw["evaluated"] += 1
    if gate.get("passed") is True:
        raw["passed"] += 1
        return
    raw["failed"] += 1
    blocker_class = blocker_class_for_gate(gate)
    classified = block["blocker_classes"][blocker_class]
    classified["failed_gate_evaluations"] += 1
    classified["_candidate_ids"].add(candidate_id)
    classified["_market_versions"].add(market_version_key)
    classified["gates"][name] += 1
    for reason in gate.get("reasons", []):
        normalized = str(reason).strip()
        if not normalized:
            continue
        raw["failure_reasons"][normalized] += 1
        classified["failure_reasons"][normalized] += 1


def _market_outcome(candidates: Sequence[Mapping[str, Any]]) -> str:
    if any(item.get("formal_eligible") is True for item in candidates):
        return "formal_available"
    if any(item.get("counterfactual_eligible") is True for item in candidates):
        return "observation_available"
    return "nonrelease_blocked"


def _add_market_entry(
    block: dict[str, Any],
    *,
    observation_id: str,
    entry: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> None:
    market = str(entry.get("market") or "")
    status = str(entry.get("status") or "")
    market_version_key = f"{observation_id}:{market}"
    block["_observations"].add(observation_id)
    block["market_versions"] += 1
    if status == "unavailable":
        block["unavailable_market_versions"] += 1
        block["outcomes"]["unavailable"] += 1
        for reason in entry.get("reasons", []):
            normalized = str(reason).strip()
            if normalized:
                block["unavailable_reasons"][normalized] += 1
        return

    block["evaluated_market_versions"] += 1
    block["candidates"] += len(candidates)
    block["outcomes"][_market_outcome(candidates)] += 1
    for index, candidate in enumerate(candidates, start=1):
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id:
            candidate_id = f"{market_version_key}:candidate-{index}"
        for gate in candidate.get("gates", []):
            if isinstance(gate, Mapping):
                _add_gate(
                    block,
                    gate,
                    candidate_id=candidate_id,
                    market_version_key=market_version_key,
                )


def _finalize_diagnostic_block(block: dict[str, Any]) -> dict[str, Any]:
    gate_funnel: dict[str, Any] = {}
    combined_failure_reasons: Counter[str] = Counter()
    for name, values in sorted(block["gate_funnel"].items()):
        categories = sorted(values["categories"])
        combined_failure_reasons.update(values["failure_reasons"])
        gate_funnel[name] = {
            "category": categories[0] if len(categories) == 1 else categories,
            "blocker_class": values["blocker_class"],
            "evaluated": values["evaluated"],
            "passed": values["passed"],
            "failed": values["failed"],
            "failure_reasons": dict(sorted(values["failure_reasons"].items())),
        }
    top_failed_gates = [
        {
            "gate": name,
            "blocker_class": values["blocker_class"],
            "failed": values["failed"],
            "evaluated": values["evaluated"],
        }
        for name, values in sorted(
            gate_funnel.items(), key=lambda item: (-item[1]["failed"], item[0])
        )
        if values["failed"] > 0
    ]
    top_failure_reasons = [
        {"reason": reason, "failed": count}
        for reason, count in sorted(
            combined_failure_reasons.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    blocker_classes: dict[str, Any] = {}
    for blocker_class in BLOCKER_CLASSES:
        values = block["blocker_classes"][blocker_class]
        blocker_classes[blocker_class] = {
            "failed_gate_evaluations": values["failed_gate_evaluations"],
            "affected_candidates": len(values["_candidate_ids"]),
            "affected_market_versions": len(values["_market_versions"]),
            "gates": dict(sorted(values["gates"].items())),
            "failure_reasons": dict(sorted(values["failure_reasons"].items())),
        }
    return {
        "archive_versions": len(block["_observations"]),
        "market_versions": block["market_versions"],
        "evaluated_market_versions": block["evaluated_market_versions"],
        "unavailable_market_versions": block["unavailable_market_versions"],
        "candidates": block["candidates"],
        "outcomes": {name: int(block["outcomes"].get(name, 0)) for name in OUTCOMES},
        "unavailable_reasons": dict(sorted(block["unavailable_reasons"].items())),
        "gate_funnel": gate_funnel,
        "top_failed_gates": top_failed_gates,
        "top_failure_reasons": top_failure_reasons,
        "blocker_classes": blocker_classes,
    }


def _finalize_coverage_block(block: dict[str, Any]) -> dict[str, Any]:
    total = int(block["archive_versions_total"])
    replayable = int(block["replayable_v3_versions"])
    return {
        "archive_versions_total": total,
        "replayable_v3_versions": replayable,
        "excluded_versions": int(block["excluded_versions"]),
        "replay_rate": round(replayable / total, 4) if total else None,
        "replay_complete": total > 0 and replayable == total,
        "excluded_by_reason": dict(sorted(block["excluded_by_reason"].items())),
        "record_statuses": dict(sorted(block["record_statuses"].items())),
    }


def _increment_coverage(
    coverage: dict[str, Any], record_status: str, reason: str | None
) -> None:
    coverage["archive_versions_total"] += 1
    coverage["record_statuses"][record_status] += 1
    if reason is None:
        coverage["replayable_v3_versions"] += 1
    else:
        coverage["excluded_versions"] += 1
        coverage["excluded_by_reason"][reason] += 1


def _collect_match_entries(
    records: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], Counter[str], int]:
    grouped: dict[str, dict[str, Any]] = {}
    exclusions: Counter[str] = Counter()
    duplicate_rows = 0
    for record in records:
        if not isinstance(record, Mapping) or record.get("mode") != "prematch":
            continue
        match_id = str(record.get("match_id") or "").strip()
        if not match_id:
            exclusions["missing_match_id"] += 1
            continue
        versions = _prematch_versions(record)
        timed_versions = [
            (version, _version_archived_at(version)) for version in versions
        ]
        valid_times = [stamp for _version, stamp in timed_versions if stamp is not None]
        if not valid_times:
            exclusions["missing_or_invalid_prematch_version_time"] += 1
            continue
        entry = grouped.get(match_id)
        if entry is None:
            grouped[match_id] = {
                "match_id": match_id,
                "latest": max(valid_times),
                "versions": [
                    {
                        "version": version,
                        "archived_at": stamp,
                        "record_status": str(record.get("status") or "unknown"),
                    }
                    for version, stamp in timed_versions
                ],
            }
        else:
            duplicate_rows += 1
            entry["latest"] = max(entry["latest"], *valid_times)
            entry["versions"].extend(
                {
                    "version": version,
                    "archived_at": stamp,
                    "record_status": str(record.get("status") or "unknown"),
                }
                for version, stamp in timed_versions
            )
    ordered = sorted(
        grouped.values(), key=lambda item: (item["latest"], item["match_id"])
    )
    return ordered, exclusions, duplicate_rows


def _window_funnel(
    selected: Sequence[dict[str, Any]],
    *,
    requested_matches: int,
    available_matches: int,
) -> dict[str, Any]:
    aggregate = _new_diagnostic_block()
    by_stage = {stage: _new_diagnostic_block() for stage in STAGES}
    by_market = {
        market: _new_diagnostic_block() for market in memory_store.PRIMARY_MARKETS
    }
    coverage = _new_coverage_block()
    stage_coverage = {stage: _new_coverage_block() for stage in STAGES}
    seen_observation_ids: set[str] = set()

    for match in selected:
        versions = sorted(
            match["versions"],
            key=lambda item: (
                item["archived_at"] or datetime.min.replace(tzinfo=timezone.utc),
                _version_stage(item["version"]),
            ),
        )
        for item in versions:
            version = item["version"]
            stage = _version_stage(version)
            if stage not in STAGES:
                continue
            record_status = item["record_status"]
            current, legacy = _candidate_evaluation_audits(version)
            reason: str | None = None
            audit: dict[str, Any] | None = None
            if item["archived_at"] is None:
                reason = "missing_or_invalid_version_archived_at"
            elif not current:
                reason = (
                    "legacy_candidate_evaluation_only"
                    if legacy
                    else "missing_candidate_evaluation_v3"
                )
            elif len(current) != 1:
                reason = "ambiguous_candidate_evaluation_v3"
            elif not memory_store.validated_candidate_evaluation_audit(
                current[0], version
            ):
                reason = "invalid_candidate_evaluation_v3_replay"
            else:
                audit = current[0]
                observation_id = str(audit.get("observation_id") or "")
                if not observation_id:
                    reason = "missing_candidate_evaluation_observation_id"
                    audit = None
                elif observation_id in seen_observation_ids:
                    reason = "duplicate_candidate_evaluation_observation_id"
                    audit = None
                else:
                    seen_observation_ids.add(observation_id)

            _increment_coverage(coverage, record_status, reason)
            _increment_coverage(stage_coverage[stage], record_status, reason)
            if audit is None:
                continue

            observation_id = str(audit["observation_id"])
            grouped_candidates: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for candidate in audit.get("candidates", []):
                if isinstance(candidate, Mapping):
                    grouped_candidates[str(candidate.get("market") or "")].append(
                        candidate
                    )
            for entry in audit.get("market_manifest", []):
                if not isinstance(entry, Mapping):
                    continue
                market = str(entry.get("market") or "")
                if market not in by_market:
                    continue
                candidates = grouped_candidates.get(market, [])
                for block in (aggregate, by_stage[stage], by_market[market]):
                    _add_market_entry(
                        block,
                        observation_id=observation_id,
                        entry=entry,
                        candidates=candidates,
                    )

    finalized_coverage = _finalize_coverage_block(coverage)
    selected_count = len(selected)
    match_window_complete = selected_count == requested_matches
    diagnostic_complete = bool(finalized_coverage["replay_complete"])
    start = selected[0]["latest"].isoformat() if selected else None
    end = selected[-1]["latest"].isoformat() if selected else None
    return {
        "requested_matches": requested_matches,
        "available_distinct_matches": available_matches,
        "selected_matches": selected_count,
        "selected_match_ids": [item["match_id"] for item in selected],
        "window_start_at": start,
        "window_end_at": end,
        "match_window_complete": match_window_complete,
        "diagnostic_complete": diagnostic_complete,
        "complete": match_window_complete and diagnostic_complete,
        "coverage": finalized_coverage,
        "aggregate": _finalize_diagnostic_block(aggregate),
        "by_stage": {
            stage: {
                "coverage": _finalize_coverage_block(stage_coverage[stage]),
                **_finalize_diagnostic_block(by_stage[stage]),
            }
            for stage in STAGES
        },
        "by_market": {
            market: _finalize_diagnostic_block(by_market[market])
            for market in memory_store.PRIMARY_MARKETS
        },
    }


def recent_candidate_gate_funnels(
    records: Iterable[Mapping[str, Any]], windows: Sequence[int] = (50, 100)
) -> dict[str, Any]:
    """Return replay-only recent gate diagnostics for distinct-match windows.

    Match windows are selected by each fixture's latest immutable pre-match
    version time.  Once a match is selected, every initial and lineup-check
    revision plus the active version is considered.  Only strictly replayable
    ``candidate-evaluation/3.0.0`` audits enter gate counts; all other versions
    remain visible in coverage and are never synthesized or backfilled.
    """

    requested: list[int] = []
    for value in windows:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("candidate gate windows must contain positive integers")
        if value not in requested:
            requested.append(value)
    if not requested:
        raise ValueError("at least one candidate gate window is required")

    matches, exclusions, duplicate_rows = _collect_match_entries(records)
    available = len(matches)
    rendered: dict[str, Any] = {}
    for window in requested:
        count = min(window, available)
        selected = matches[-count:] if count else []
        rendered[str(window)] = _window_funnel(
            selected,
            requested_matches=window,
            available_matches=available,
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluation_scope": EVALUATION_SCOPE,
        "performance_metrics_included": False,
        "window_basis": WINDOW_BASIS,
        "version_unit": VERSION_UNIT,
        "market_unit": MARKET_UNIT,
        "gate_unit": GATE_UNIT,
        "counts_are_multilabel": True,
        "outcome_definitions": {
            "formal_available": "at least one candidate passes every gate",
            "observation_available": (
                "at least one candidate passes every non-release gate but no "
                "candidate passes every gate"
            ),
            "nonrelease_blocked": (
                "market was evaluated but no candidate passes every non-release gate"
            ),
            "unavailable": "market manifest contains no evaluated candidate",
        },
        "blocker_class_definitions": {
            "data": "integrity, depth, quality, classification, or evidence gates",
            "value": "positive EV and positive comparable edge gates",
            "policy": "release-only policy and forward-evidence gates",
            "safety": "composite adverse-signal and unknown future risk gates",
        },
        "eligible_distinct_matches": available,
        "excluded_matches_by_reason": dict(sorted(exclusions.items())),
        "duplicate_match_rows": duplicate_rows,
        "windows": rendered,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay recent candidate-evaluation gate diagnostics"
    )
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--windows", type=int, nargs="+", default=[50, 100])
    return parser


def main() -> int:
    args = build_parser().parse_args()
    history_path = memory_store.data_path(args.base_dir)
    records = memory_store.load_history(history_path)
    result = {
        "path": str(Path(history_path)),
        "recent_candidate_gate_funnels": recent_candidate_gate_funnels(
            records, windows=args.windows
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

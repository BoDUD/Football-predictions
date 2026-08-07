from __future__ import annotations

import unittest
from unittest import mock

import pytest

from scripts import gate_stats, memory_store

pytestmark = pytest.mark.unit


def candidate_gate(
    name: str, category: str, passed: bool, *reasons: str
) -> dict[str, object]:
    return {
        "gate": name,
        "category": category,
        "passed": passed,
        "reasons": [] if passed else list(reasons),
    }


def candidate(
    candidate_id: str,
    market: str,
    *,
    counterfactual: bool,
    formal: bool,
    gates: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "market": market,
        "counterfactual_eligible": counterfactual,
        "formal_eligible": formal,
        "gates": gates,
    }


def evaluation_audit(
    observation_id: str,
    candidates: list[dict[str, object]] | None = None,
    *,
    unavailable: dict[str, list[str]] | None = None,
    schema_version: str = memory_store.CANDIDATE_EVALUATION_SCHEMA_VERSION,
    valid: bool = True,
) -> dict[str, object]:
    candidates = candidates or []
    unavailable = unavailable or {}
    grouped: dict[str, list[dict[str, object]]] = {
        market: [] for market in memory_store.PRIMARY_MARKETS
    }
    for item in candidates:
        grouped[str(item["market"])].append(item)
    manifest = []
    for market in memory_store.PRIMARY_MARKETS:
        market_candidates = grouped[market]
        reasons = unavailable.get(market, [f"{market}_not_collected"])
        manifest.append(
            {
                "market": market,
                "status": "evaluated" if market_candidates else "unavailable",
                "reasons": [] if market_candidates else reasons,
                "candidate_count": len(market_candidates),
            }
        )
    return {
        "schema_version": schema_version,
        "kind": memory_store.CANDIDATE_EVALUATION_KIND,
        "observation_id": observation_id,
        "market_manifest": manifest,
        "candidates": candidates,
        "_test_valid": valid,
    }


def version(
    match_id: str,
    archived_at: str,
    stage: str,
    audit: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "match_id": match_id,
        "analysis_stage": stage,
        "archived_at": archived_at,
        "created_at": archived_at,
        "updated_at": archived_at,
        "candidate_audits": [] if audit is None else [audit],
    }


def record(
    match_id: str,
    archived_at: str,
    stage: str,
    audit: dict[str, object] | None,
    *,
    status: str = "pending",
    revisions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    active = version(match_id, archived_at, stage, audit)
    active.update(
        {
            "mode": "prematch",
            "status": status,
            "revisions": revisions or [],
        }
    )
    return active


def replay_from_test_flag(audit: dict[str, object], _version: object) -> bool:
    return audit.get("_test_valid") is True


class RecentCandidateGateFunnelTests(unittest.TestCase):
    @mock.patch.object(
        gate_stats.memory_store,
        "validated_candidate_evaluation_audit",
        side_effect=replay_from_test_flag,
    )
    def test_windows_use_distinct_match_latest_version_and_keep_stages(
        self, _replay: mock.Mock
    ) -> None:
        first = record(
            "a",
            "2026-01-01T00:00:00Z",
            "initial",
            evaluation_audit("obs-a"),
        )
        initial_b = version(
            "b",
            "2026-01-02T00:00:00Z",
            "initial",
            evaluation_audit("obs-b-initial"),
        )
        second = record(
            "b",
            "2026-01-03T00:00:00Z",
            "lineup-check",
            evaluation_audit("obs-b-lineup"),
            revisions=[initial_b],
        )
        third = record(
            "c",
            "2026-01-04T00:00:00Z",
            "initial",
            evaluation_audit("obs-c"),
        )

        result = gate_stats.recent_candidate_gate_funnels(
            [third, first, second], windows=(2, 5)
        )
        recent = result["windows"]["2"]
        self.assertEqual(recent["selected_match_ids"], ["b", "c"])
        self.assertEqual(recent["coverage"]["archive_versions_total"], 3)
        self.assertEqual(recent["coverage"]["replayable_v3_versions"], 3)
        self.assertEqual(
            recent["by_stage"]["initial"]["coverage"]["replayable_v3_versions"],
            2,
        )
        self.assertEqual(
            recent["by_stage"]["lineup-check"]["coverage"]["replayable_v3_versions"],
            1,
        )
        self.assertTrue(recent["match_window_complete"])
        self.assertTrue(recent["diagnostic_complete"])
        self.assertTrue(recent["complete"])

        oversized = result["windows"]["5"]
        self.assertEqual(oversized["selected_matches"], 3)
        self.assertEqual(oversized["coverage"]["archive_versions_total"], 4)
        self.assertFalse(oversized["match_window_complete"])
        self.assertFalse(oversized["complete"])

    @mock.patch.object(
        gate_stats.memory_store,
        "validated_candidate_evaluation_audit",
        side_effect=replay_from_test_flag,
    )
    def test_outcomes_unavailable_and_blocker_classes_are_separate(
        self, _replay: mock.Mock
    ) -> None:
        observation = candidate(
            "asian-observation",
            "asian",
            counterfactual=True,
            formal=False,
            gates=[
                candidate_gate("positive_ev", "value", True),
                candidate_gate(
                    "market_policy_enabled",
                    "release",
                    False,
                    "market_observation_only_under_active_policy",
                ),
            ],
        )
        blocked = candidate(
            "total-blocked",
            "total",
            counterfactual=False,
            formal=False,
            gates=[
                candidate_gate(
                    "positive_ev", "value", False, "current_ev_not_positive"
                ),
                candidate_gate(
                    "market_specific_evidence",
                    "risk",
                    False,
                    "attacking_or_chance_quality_evidence_required",
                ),
                candidate_gate(
                    "adverse_signal_gate",
                    "risk",
                    False,
                    "adverse_market_safety_thresholds_not_met",
                ),
            ],
        )
        formal = candidate(
            "range-formal",
            "goal_range",
            counterfactual=True,
            formal=True,
            gates=[
                candidate_gate("positive_ev", "value", True),
                candidate_gate("market_policy_enabled", "release", True),
            ],
        )
        audit = evaluation_audit(
            "obs-outcomes",
            [observation, blocked, formal],
            unavailable={"btts": ["source_market_identity_unavailable"]},
        )
        result = gate_stats.recent_candidate_gate_funnels(
            [record("fixture", "2026-01-01T00:00:00Z", "initial", audit)],
            windows=(1,),
        )["windows"]["1"]
        aggregate = result["aggregate"]

        self.assertEqual(
            aggregate["outcomes"],
            {
                "formal_available": 1,
                "observation_available": 1,
                "nonrelease_blocked": 1,
                "unavailable": 5,
            },
        )
        self.assertEqual(aggregate["evaluated_market_versions"], 3)
        self.assertEqual(aggregate["unavailable_market_versions"], 5)
        self.assertEqual(aggregate["candidates"], 3)
        self.assertEqual(aggregate["gate_funnel"]["positive_ev"]["failed"], 1)
        self.assertEqual(
            {item["gate"] for item in aggregate["top_failed_gates"]},
            {
                "adverse_signal_gate",
                "market_policy_enabled",
                "market_specific_evidence",
                "positive_ev",
            },
        )
        self.assertIn(
            {"reason": "current_ev_not_positive", "failed": 1},
            aggregate["top_failure_reasons"],
        )
        self.assertEqual(
            aggregate["blocker_classes"]["value"]["affected_candidates"], 1
        )
        self.assertEqual(
            aggregate["blocker_classes"]["data"]["gates"],
            {"market_specific_evidence": 1},
        )
        self.assertEqual(
            aggregate["blocker_classes"]["policy"]["gates"],
            {"market_policy_enabled": 1},
        )
        self.assertEqual(
            aggregate["blocker_classes"]["safety"]["gates"],
            {"adverse_signal_gate": 1},
        )
        self.assertEqual(
            result["by_market"]["btts"]["unavailable_reasons"],
            {"source_market_identity_unavailable": 1},
        )
        self.assertEqual(result["by_market"]["btts"]["candidates"], 0)

    @mock.patch.object(
        gate_stats.memory_store,
        "validated_candidate_evaluation_audit",
        side_effect=replay_from_test_flag,
    )
    def test_invalid_legacy_and_missing_versions_are_coverage_only(
        self, replay: mock.Mock
    ) -> None:
        valid = record(
            "valid",
            "2026-01-01T00:00:00Z",
            "initial",
            evaluation_audit("obs-valid"),
            status="reviewed",
        )
        invalid = record(
            "invalid",
            "2026-01-02T00:00:00Z",
            "initial",
            evaluation_audit("obs-invalid", valid=False),
        )
        legacy = record(
            "legacy",
            "2026-01-03T00:00:00Z",
            "initial",
            evaluation_audit(
                "obs-legacy",
                schema_version=memory_store.LEGACY_CANDIDATE_EVALUATION_SCHEMA_VERSION,
            ),
            status="reviewed",
        )
        missing = record("missing", "2026-01-04T00:00:00Z", "initial", None)

        result = gate_stats.recent_candidate_gate_funnels(
            [missing, valid, legacy, invalid], windows=(4,)
        )["windows"]["4"]
        coverage = result["coverage"]
        self.assertEqual(coverage["archive_versions_total"], 4)
        self.assertEqual(coverage["replayable_v3_versions"], 1)
        self.assertEqual(coverage["excluded_versions"], 3)
        self.assertEqual(
            coverage["excluded_by_reason"],
            {
                "invalid_candidate_evaluation_v3_replay": 1,
                "legacy_candidate_evaluation_only": 1,
                "missing_candidate_evaluation_v3": 1,
            },
        )
        self.assertEqual(coverage["record_statuses"], {"pending": 2, "reviewed": 2})
        self.assertTrue(result["match_window_complete"])
        self.assertFalse(result["diagnostic_complete"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["aggregate"]["archive_versions"], 1)
        self.assertEqual(replay.call_count, 2)

    def test_invalid_windows_fail_closed(self) -> None:
        for windows in ((), (0,), (-1,), (True,)):
            with self.subTest(windows=windows):
                with self.assertRaises(ValueError):
                    gate_stats.recent_candidate_gate_funnels([], windows=windows)

    def test_cli_defaults_to_requested_windows(self) -> None:
        args = gate_stats.build_parser().parse_args([])
        self.assertEqual(args.windows, [50, 100])


if __name__ == "__main__":
    unittest.main()

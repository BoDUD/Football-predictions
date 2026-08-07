from __future__ import annotations

from copy import deepcopy
from unittest import mock

import pytest

from scripts import memory_store, publication_outlook

pytestmark = pytest.mark.unit


def _candidate(
    market: str,
    identity: str,
    *,
    score: float,
    gates: list[dict[str, object]] | None = None,
    counterfactual: bool = True,
    formal: bool = False,
    selected: bool = True,
    **values: object,
) -> dict[str, object]:
    return {
        "candidate_id": f"sha256:{identity:0<64}"[:71],
        "market": market,
        "identity": identity,
        "side": values.pop("side", "over"),
        "line": values.pop("line", 2.5),
        "odds": values.pop("odds", 1.95),
        "odds_format": "decimal",
        "probability": 0.56,
        "ev": 0.092,
        "edge_pp": 4.1,
        "market_probability": 0.519,
        "market_signal": "aligned",
        "firm_count": 5,
        "counterfactual_eligible": counterfactual,
        "formal_eligible": formal,
        "shadow_selected": selected,
        "shadow_rank": 1 if selected else None,
        "shadow_confidence": {
            "score": score,
            "settlement_safety_probability": 0.64,
            "firm_count": 5,
        },
        "gates": gates
        if gates is not None
        else [
            {
                "gate": "market_policy_enabled",
                "category": "release",
                "passed": False,
                "reasons": ["market_observation_only_under_active_policy"],
            }
        ],
        **values,
    }


def _audit(
    *candidates: dict[str, object], schema: str | None = None
) -> dict[str, object]:
    markets = {str(candidate["market"]) for candidate in candidates}
    return {
        "kind": memory_store.CANDIDATE_EVALUATION_KIND,
        "schema_version": schema or memory_store.CANDIDATE_EVALUATION_SCHEMA_VERSION,
        "candidates": list(candidates),
        "market_manifest": [
            {
                "market": market,
                "status": "evaluated" if market in markets else "unavailable",
                "reasons": [] if market in markets else ["not_collected"],
            }
            for market in memory_store.PRIMARY_MARKETS
        ],
    }


def _version(*audits: dict[str, object], **values: object) -> dict[str, object]:
    return {
        "analysis_stage": values.pop("analysis_stage", "initial"),
        "primary_market": values.pop("primary_market", None),
        "primary_pick": values.pop("primary_pick", None),
        "candidate_audits": list(audits),
        **values,
    }


def _valid_audits():
    return mock.patch.object(
        publication_outlook.memory_store,
        "validated_candidate_evaluation_audit",
        return_value=True,
    )


def test_selects_one_cross_market_v3_shadow_without_promoting_it() -> None:
    total = _candidate("total", "total:over:2.5", score=72.0)
    half = _candidate(
        "half_time",
        "half_time:total:under:1",
        score=78.0,
        submarket="total",
        side="under",
        line=1.0,
    )
    version = _version(_audit(total, half))
    original = deepcopy(version)

    with _valid_audits():
        summary = publication_outlook.publication_summary(version)

    assert summary["state"] == "observation_primary"
    assert summary["stage_status"] == "initial_observation_primary"
    assert summary["formal_primary"] is None
    assert summary["observation_primary"]["identity"] == half["identity"]
    assert summary["observation_primary"]["submarket"] == "total"
    assert summary["observation_primary"]["counts_toward_primary_record"] is False
    assert summary["observation_primary"]["monetary_scope"] == "none"
    assert summary["blockers"]["data"] == []
    assert summary["blockers"]["value"] == []
    assert summary["blockers"]["policy"][0]["gate"] == "market_policy_enabled"
    assert version == original


def test_observation_requires_all_three_shadow_eligibility_flags() -> None:
    eligible = _candidate("total", "total:over:2.5", score=40.0)
    already_formal = _candidate(
        "asian",
        "asian:home:0",
        score=99.0,
        side="home",
        line=0,
        formal=True,
    )
    nonrelease_blocked = _candidate(
        "btts",
        "btts:yes",
        score=98.0,
        side="yes",
        line=None,
        counterfactual=False,
    )
    not_shadow_selected = _candidate(
        "goal_range",
        "goal_range:2-3",
        score=97.0,
        selection="2-3",
        minimum_goals=2,
        maximum_goals=3,
        selected=False,
    )
    version = _version(
        _audit(eligible, already_formal, nonrelease_blocked, not_shadow_selected)
    )

    with _valid_audits():
        summary = publication_outlook.publication_summary(version)

    assert summary["observation_primary"]["identity"] == eligible["identity"]
    assert summary["observation_primary"]["selection_policy"] == (
        publication_outlook.OBSERVATION_PRIMARY_SELECTION_POLICY
    )


def test_requires_exactly_one_valid_current_v3_audit() -> None:
    candidate = _candidate("asian", "asian:home:0", score=70.0, side="home", line=0)
    legacy = _audit(
        candidate,
        schema=memory_store.LEGACY_CANDIDATE_EVALUATION_SCHEMA_VERSION,
    )
    legacy_summary = publication_outlook.publication_summary(_version(legacy))
    assert legacy_summary["candidate_evaluation_status"] == "legacy_only"
    assert legacy_summary["observation_primary"] is None
    assert legacy_summary["formal_blockers"][0]["type"] == "data"

    current = _audit(candidate)
    with mock.patch.object(
        publication_outlook.memory_store,
        "validated_candidate_evaluation_audit",
        return_value=False,
    ):
        invalid = publication_outlook.publication_summary(_version(current))
    assert invalid["candidate_evaluation_status"] == "invalid"
    assert invalid["state"] == "no_usable_direction"

    with _valid_audits():
        ambiguous = publication_outlook.publication_summary(
            _version(current, deepcopy(current))
        )
    assert ambiguous["candidate_evaluation_status"] == "ambiguous"
    assert ambiguous["observation_primary"] is None


def test_blocker_taxonomy_keeps_adverse_gate_in_separate_safety_channel() -> None:
    failed = _candidate(
        "total",
        "total:over:2.5",
        score=10.0,
        counterfactual=False,
        selected=False,
        gates=[
            {
                "gate": "complete_current_market",
                "category": "integrity",
                "passed": False,
                "reasons": ["market_complete_false"],
            },
            {
                "gate": "positive_ev",
                "category": "value",
                "passed": False,
                "reasons": ["current_ev_not_positive"],
            },
            {
                "gate": "market_policy_enabled",
                "category": "release",
                "passed": False,
                "reasons": ["market_observation_only_under_active_policy"],
            },
            {
                "gate": "adverse_signal_gate",
                "category": "risk",
                "passed": False,
                "reasons": ["adverse_market_safety_thresholds_not_met"],
            },
        ],
    )
    audit = _audit(failed)
    with _valid_audits():
        summary = publication_outlook.publication_summary(_version(audit))

    assert summary["state"] == "no_usable_direction"
    assert {item["gate"] for item in summary["blockers"]["data"]} >= {
        "complete_current_market",
        "market_unavailable",
    }
    assert [item["gate"] for item in summary["blockers"]["value"]] == ["positive_ev"]
    assert [item["gate"] for item in summary["blockers"]["policy"]] == [
        "market_policy_enabled"
    ]
    assert [item["gate"] for item in summary["safety_blockers"]] == [
        "adverse_signal_gate"
    ]
    assert all(
        item["gate"] != "adverse_signal_gate"
        for blocker_type in publication_outlook.BLOCKER_TYPES
        for item in summary["blockers"][blocker_type]
    )


def test_formal_primary_supersedes_shadow_and_has_no_failure_claim() -> None:
    observation = _candidate("asian", "asian:home:0", score=80.0, side="home", line=0)
    version = _version(
        _audit(observation),
        analysis_stage="lineup-check",
        primary_market="total",
        primary_pick={"side": "under", "line": 2.5, "odds": 1.93},
    )
    with _valid_audits():
        summary = publication_outlook.publication_summary(version)

    assert summary["state"] == "formal_primary"
    assert summary["stage_status"] == "lineup_formal_primary"
    assert summary["formal_primary"]["identity"] == "total:under:2.5"
    assert summary["observation_primary"] is None
    assert summary["formal_blockers"] == []
    assert all(
        summary["blockers"][kind] == [] for kind in publication_outlook.BLOCKER_TYPES
    )


def test_legacy_half_time_formal_uses_executable_submarket_without_mutation() -> None:
    version = _version(
        primary_market="half_time",
        primary_pick={"market": "half_time", "side": "over", "line": 1.0},
        half_time_pick={"market": "total", "side": "over", "line": 1.0},
    )
    original = deepcopy(version)

    summary = publication_outlook.publication_summary(version)

    assert summary["formal_primary"]["pick"]["market"] == "total"
    assert summary["formal_primary"]["identity"] == "half_time:total:over:1"
    assert version == original


def test_observation_transition_detects_maintenance_change_and_upgrade() -> None:
    initial_candidate = _candidate("total", "total:over:2.5", score=75.0)
    changed_candidate = _candidate(
        "btts", "btts:yes", score=76.0, side="yes", line=None
    )
    initial = _version(_audit(initial_candidate), analysis_stage="initial")
    maintained = _version(
        _audit(deepcopy(initial_candidate)), analysis_stage="lineup-check"
    )
    changed = _version(_audit(changed_candidate), analysis_stage="lineup-check")
    upgraded = _version(
        _audit(deepcopy(initial_candidate)),
        analysis_stage="lineup-check",
        primary_market="total",
        primary_pick={"side": "over", "line": 2.5, "odds": 1.95},
    )

    with _valid_audits():
        assert (
            publication_outlook.observation_transition(initial, maintained)["status"]
            == "maintained"
        )
        assert (
            publication_outlook.observation_transition(initial, changed)["status"]
            == "changed"
        )
        transition = publication_outlook.observation_transition(initial, upgraded)

    assert transition["status"] == "upgraded_to_formal"
    assert transition["previous_observation_identity"] == "total:over:2.5"
    assert transition["current_formal_identity"] == "total:over:2.5"
    assert transition["counts_toward_primary_record"] is False


def test_inconsistent_formal_archive_fails_closed() -> None:
    with pytest.raises(publication_outlook.PublicationOutlookError):
        publication_outlook.publication_summary(
            _version(primary_market="total", primary_pick=None)
        )

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import forward_policy, forward_validation, source_evidence

OUTCOMES = ["H", "D", "A"]
TEST_HASH = "sha256:" + "1" * 64
REAL_GIT_COMMIT = forward_policy._git(
    Path(__file__).resolve().parents[1], "rev-parse", "HEAD"
)


ONE_X_TWO_IDENTITY = {
    "family": "1x2",
    "period": "full_time",
    "line": None,
    "price_outcomes": OUTCOMES,
}
ONE_X_TWO_IDENTITY_HASH = source_evidence.market_identity_hash(ONE_X_TWO_IDENTITY)


def policy_manifest(*, git_commit: str = REAL_GIT_COMMIT) -> dict:
    protocol = deepcopy(forward_policy.DEFAULT_VALIDATION_PROTOCOL)
    protocol.update(
        {
            "bootstrap_repetitions": 100,
            "minimum_confirmation_samples": 4,
            "minimum_iso_week_clusters": 2,
            "minimum_segment_samples": 2,
            "minimum_segment_clusters": 2,
        }
    )
    value = {
        "schema_version": forward_policy.POLICY_SCHEMA_VERSION,
        "artifact_type": "soccer_prediction_policy_freeze",
        "created_at": "2026-08-01T00:00:00+00:00",
        "software": {
            "package_name": "soccer-predict",
            "package_version": forward_policy.SOCCER_PREDICT_VERSION,
            "version_source": "soccer_predict.__version__",
        },
        "code": {
            "commit": git_commit,
            "expected_final_merge_commit": git_commit,
            "protected_files": {
                path: TEST_HASH
                for path in sorted(forward_policy.REQUIRED_PROVENANCE_PROTECTED_FILES)
            },
        },
        "data": {
            "manifest_path": "data/manifest.json",
            "file_sha256": TEST_HASH,
            "declared_manifest_hash": TEST_HASH,
        },
        "models": {
            "registry_path": "models/registry.json",
            "file_sha256": TEST_HASH,
            "declared_registry_hash": TEST_HASH,
            "dataset_manifest_hash": TEST_HASH,
        },
        "policy": {
            "market_policy_version": "test-policy",
            "market_status": {"1x2": "formal"},
            "selector": {"version": "test"},
            "release_thresholds": {"minimum_firms": 1},
            "candidate_evaluation": {"schema_version": "test"},
            "display_policy": {"joint_event_count": 2},
            "validation_protocol": protocol,
        },
        "confirmation_contract": forward_policy._confirmation_contract(
            forward_policy.LOCAL_INTEGRITY_SHADOW_KIND
        ),
    }
    value["policy_hash"] = forward_policy._hash_json(value)
    value["policy_id"] = (
        "untouched-live-forward-" + value["policy_hash"].split(":", 1)[1][:16]
    )
    return value


def cohort_manifest(policy: dict, *, status: str = "active") -> dict:
    value = {
        "schema_version": forward_policy.COHORT_SCHEMA_VERSION,
        "artifact_type": "soccer_untouched_live_forward_cohort",
        "cohort_id": "confirmation-a",
        "kind": forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
        "status": status,
        "starts_at": "2026-08-01T00:01:00+00:00",
        "policy_file": str(Path("C:/forward-policies") / f"{policy['policy_id']}.json"),
        "policy_id": policy["policy_id"],
        "policy_hash": policy["policy_hash"],
        "retrospective_records_allowed": False,
        "closed_at": "2027-01-01T00:00:00+00:00" if status == "closed" else None,
    }
    value["cohort_hash"] = forward_policy._hash_json(value)
    return value


def cohort_record_manifest(cohort: dict, records: list[dict] | None = None) -> dict:
    entries = deepcopy(records or [])
    value = {
        "schema_version": forward_policy.RECORD_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "soccer_untouched_live_forward_record_manifest",
        "cohort_id": cohort["cohort_id"],
        "cohort_hash": cohort["cohort_hash"],
        "policy_id": cohort["policy_id"],
        "policy_hash": cohort["policy_hash"],
        "record_count": len(entries),
        "records": entries,
    }
    value["manifest_hash"] = forward_policy._hash_json(value)
    return value


def cohort_closure(cohort: dict, record_manifest: dict | None = None) -> dict:
    manifest = deepcopy(record_manifest or cohort_record_manifest(cohort))
    value = {
        "schema_version": forward_policy.CLOSURE_SCHEMA_VERSION,
        "artifact_type": "soccer_untouched_live_forward_cohort_closure",
        "cohort_id": cohort["cohort_id"],
        "cohort_hash": cohort["cohort_hash"],
        "policy_id": cohort["policy_id"],
        "policy_hash": cohort["policy_hash"],
        "starts_at": cohort["starts_at"],
        "closed_at": "2027-01-01T00:00:00+00:00",
        "reason": "explicit_policy_boundary",
        "record_manifest_hash": manifest["manifest_hash"],
        "record_manifest": manifest,
    }
    value["closure_hash"] = forward_policy._hash_json(value)
    return value


def base_policy_binding(policy: dict, cohort: dict, archived_at: str) -> dict:
    value = {
        "schema_version": forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
        "cohort_id": cohort["cohort_id"],
        "cohort_hash": cohort["cohort_hash"],
        "cohort_starts_at": cohort["starts_at"],
        "policy_id": policy["policy_id"],
        "policy_hash": policy["policy_hash"],
        "policy_snapshot": policy,
        "recorded_code_commit": policy["code"]["commit"],
        "archived_at": archived_at,
        "cohort_kind": forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
        "assurance_scope": forward_policy.LOCAL_ASSURANCE_SCOPE,
        "promotion_evidence_eligible": False,
        "provenance_binding": forward_policy.build_provenance_binding(
            policy, cohort_id=cohort["cohort_id"]
        ),
    }
    value["binding_hash"] = forward_policy._hash_json(value)
    return value


def raw_snapshot(
    fixture_id: str,
    home_team: str,
    away_team: str,
    kickoff: str,
    collected_at: str,
    prices: dict[str, float],
) -> dict:
    return {
        "schema_version": source_evidence.RAW_SCHEMA_VERSION,
        "source_url": f"https://zq.titan007.com/analysis/{fixture_id}cn.htm",
        "collected_at": collected_at,
        "fixture": {
            "match_id": fixture_id,
            "home_team": home_team,
            "away_team": away_team,
            "kickoff": kickoff,
        },
        "markets": [
            {
                "market_identity": ONE_X_TWO_IDENTITY,
                "odds_format": "decimal",
                "firms": [
                    {"name": name, "outcomes": prices} for name in ("A", "B", "C")
                ],
            }
        ],
    }


def seal(value: dict, hash_field: str) -> dict:
    sealed = deepcopy(value)
    sealed[hash_field] = forward_validation._hash(sealed)
    return sealed


def build_payload(
    base: Path,
    count: int = 6,
    *,
    one_week: bool = False,
    start_index: int = 0,
    git_commit: str = REAL_GIT_COMMIT,
) -> dict:
    policy = policy_manifest(git_commit=git_commit)
    cohort = cohort_manifest(policy)
    market_schemas = {
        "1x2": {
            "settlement_states": OUTCOMES,
            "settlement_semantics": "categorical",
        }
    }
    frozen_at = datetime(2026, 8, 2, tzinfo=timezone.utc)
    entries: list[dict] = []
    raw_rows: list[dict] = []
    for offset in range(count):
        index = start_index + offset
        actual = OUTCOMES[index % 3]
        kickoff = datetime(2026, 9, 1, 10, tzinfo=timezone.utc) + timedelta(
            days=index if one_week else index * 7
        )
        generated = kickoff - timedelta(minutes=30)
        archived = generated + timedelta(minutes=1)
        fixture_id = str(1000 + index)
        home_team = f"Home {index}"
        away_team = f"Away {index}"
        queue_key = forward_validation._queue_key(
            cohort["cohort_id"], fixture_id, ONE_X_TWO_IDENTITY_HASH
        )
        entries.append(
            {
                "fixture_id": fixture_id,
                "home_team": home_team,
                "away_team": away_team,
                "league": "league-a" if index % 2 else "league-b",
                "market_identity": ONE_X_TWO_IDENTITY,
                "market_identity_hash": ONE_X_TWO_IDENTITY_HASH,
                "kickoff": kickoff.isoformat(),
                "queue_key": queue_key,
            }
        )
        bookmaker = {item: 4.0 for item in OUTCOMES}
        bookmaker[actual] = 2.0
        raw_file = base / f"raw-{index}.json"
        raw_file.write_text(
            json.dumps(
                raw_snapshot(
                    fixture_id,
                    home_team,
                    away_team,
                    kickoff.isoformat(),
                    generated.isoformat(),
                    bookmaker,
                )
            ),
            encoding="utf-8",
        )
        evidence_file, evidence = source_evidence.build_evidence(
            [raw_file], output_dir=base / f"evidence-{index}"
        )
        raw_rows.append(
            {
                "actual": actual,
                "kickoff": kickoff,
                "generated": generated,
                "archived": archived,
                "fixture_id": fixture_id,
                "home_team": home_team,
                "away_team": away_team,
                "queue_key": queue_key,
                "bookmaker": bookmaker,
                "evidence_file": evidence_file,
                "evidence": evidence,
            }
        )
    queue = seal(
        {
            "schema_version": forward_validation.QUEUE_SCHEMA_VERSION,
            "artifact_type": "soccer_forward_eligibility_queue",
            "queue_id": "queue-a",
            "cohort_id": cohort["cohort_id"],
            "policy_id": policy["policy_id"],
            "policy_hash": policy["policy_hash"],
            "frozen_at": frozen_at.isoformat(),
            "entries": entries,
            "integrity_assurance": "local_content_hash_only_no_external_timestamp",
        },
        "queue_hash",
    )
    commitments: list[dict] = []
    settlements: list[dict] = []
    for offset, raw in enumerate(raw_rows):
        actual = raw["actual"]
        model = {item: 0.1 for item in OUTCOMES}
        model[actual] = 0.8
        bookmaker_inverse = {
            key: 1.0 / value for key, value in raw["bookmaker"].items()
        }
        total_inverse = sum(bookmaker_inverse.values())
        bookmaker_no_vig = {
            key: value / total_inverse for key, value in bookmaker_inverse.items()
        }
        generated_text = raw["generated"].isoformat()
        lineage = {
            name: {
                "kind": "replayable_source_snapshot"
                if name == "bookmaker_no_vig"
                else "frozen_baseline_artifact",
                "generated_at": generated_text,
                "training_cutoff": generated_text,
                "artifact_hash": raw["evidence"]["evidence_hash"]
                if name == "bookmaker_no_vig"
                else TEST_HASH,
            }
            for name in forward_validation.BASELINE_NAMES
        }
        prediction = {
            "provenance_binding": forward_policy.build_provenance_binding(
                policy, cohort_id=cohort["cohort_id"]
            ),
            "queue_hash": queue["queue_hash"],
            "queue_key": raw["queue_key"],
            "fixture_id": raw["fixture_id"],
            "home_team": raw["home_team"],
            "away_team": raw["away_team"],
            "observation_id": forward_validation._observation_id(raw["queue_key"]),
            "league": entries[offset]["league"],
            "market_identity": ONE_X_TWO_IDENTITY,
            "market_identity_hash": ONE_X_TWO_IDENTITY_HASH,
            "kickoff": raw["kickoff"].isoformat(),
            "generated_at": generated_text,
            "lead_time_minutes": 30,
            "status": "predicted",
            "settlement_reference_outcome": None,
            "model_probabilities": model,
            "baselines": {
                "historical_frequency": {"H": 0.4, "D": 0.3, "A": 0.3},
                "independent_htft": {"H": 0.4, "D": 0.3, "A": 0.3},
                "simple_poisson_dc": {"H": 0.45, "D": 0.3, "A": 0.25},
                "bookmaker_no_vig": bookmaker_no_vig,
            },
            "baseline_lineage": lineage,
            "bookmaker_snapshot": {
                "collected_at": generated_text,
                "source_evidence_file": str(raw["evidence_file"]),
                "source_evidence_hash": raw["evidence"]["evidence_hash"],
                "source_url": f"https://zq.titan007.com/analysis/{raw['fixture_id']}cn.htm",
                "firm_count": 3,
                "price_basis": "median",
                "odds_format": "decimal",
                "complete_market_odds": raw["bookmaker"],
                "no_vig_method": "multiplicative_normalization",
            },
            "execution_entry": {
                "selection": actual,
                "entry_decimal_odds": 2.0,
                "entry_complete_market_odds": {
                    item: 2.0 if item == actual else 4.0 for item in OUTCOMES
                },
                "entry_collected_at": generated_text,
                "entry_source_evidence_hash": raw["evidence"]["evidence_hash"],
                "entry_price_kind": "executable_after_slippage",
                "limit_verified": True,
                "stake_units": 1.0,
            },
            "unavailable_reasons": [],
        }
        binding = forward_policy.bind_observation_commitment(
            base_policy_binding(policy, cohort, raw["archived"].isoformat()),
            forward_validation._hash(prediction),
        )
        commitment = seal(
            {
                "schema_version": forward_validation.COMMITMENT_SCHEMA_VERSION,
                "prediction_payload": prediction,
                "forward_policy_binding": binding,
            },
            "commitment_hash",
        )
        commitments.append(commitment)
        settlements.append(
            seal(
                {
                    "schema_version": forward_validation.SETTLEMENT_SCHEMA_VERSION,
                    "observation_id": prediction["observation_id"],
                    "commitment_hash": commitment["commitment_hash"],
                    "status": "settled",
                    "observed_settlement_state": actual,
                    "result_collected_at": (
                        raw["kickoff"] + timedelta(hours=2)
                    ).isoformat(),
                    "result_source_evidence_hash": TEST_HASH,
                    "closing_snapshot": {
                        "collected_at": (
                            raw["kickoff"] - timedelta(minutes=5)
                        ).isoformat(),
                        "source_evidence_hash": raw["evidence"]["evidence_hash"],
                        "complete_market_odds": {
                            item: 1.8 if item == actual else 4.0 for item in OUTCOMES
                        },
                    },
                },
                "settlement_hash",
            )
        )
    payload = {
        "schema_version": forward_validation.INPUT_SCHEMA_VERSION,
        "cohort_id": cohort["cohort_id"],
        "policy_id": policy["policy_id"],
        "policy_hash": policy["policy_hash"],
        "policy_manifest": policy,
        "cohort_manifest": cohort,
        "cohort_closure": cohort_closure(cohort),
        "market_schemas": market_schemas,
        "queue_manifest": queue,
        "commitments": commitments,
        "settlements": settlements,
    }
    return payload


def history_receipt_from_micro_ledger(payload: dict) -> dict:
    prematch = forward_validation._prematch_ledger_view(payload)
    market_commitments = forward_validation._market_commitment_identities(prematch)
    predictions = [item["prediction_payload"] for item in payload["commitments"]]
    fixture_ids = {str(item["fixture_id"]) for item in predictions}
    if len(fixture_ids) != 1:
        raise AssertionError("test receipt requires one fixture")
    fixture_id = fixture_ids.pop()
    archived_at = payload["commitments"][0]["forward_policy_binding"]["archived_at"]
    archive = {
        "schema_version": "memory-forward-ledger-archive/2.0.0",
        "fixture_id": fixture_id,
        "ledger_hash": forward_validation._hash(prematch),
        "ledger_payload": prematch,
        "market_commitments": market_commitments,
    }
    archive["archive_hash"] = forward_validation._hash(archive)

    committed_micro_binding = deepcopy(
        payload["commitments"][0]["forward_policy_binding"]
    )
    committed_micro_binding.pop("binding_hash")
    committed_micro_binding.pop("observation_commitment_hash")
    committed_micro_binding["schema_version"] = (
        forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION
    )
    committed_micro_binding["binding_hash"] = forward_validation._hash(
        committed_micro_binding
    )
    first_prediction = predictions[0]
    record_prediction = {
        "schema_version": "memory-forward-prediction/2.0.0",
        "archived_at": archived_at,
        "analysis_stage": "initial",
        "fixture": {
            "match_id": fixture_id,
            "league": first_prediction["league"],
            "kickoff": first_prediction["kickoff"],
            "home_team": first_prediction["home_team"],
            "away_team": first_prediction["away_team"],
        },
        "model_outputs": {},
        "ledger": {
            "ledger_hash": archive["ledger_hash"],
            "archive_hash": archive["archive_hash"],
            "market_commitments": market_commitments,
        },
        "provenance_binding": deepcopy(committed_micro_binding["provenance_binding"]),
    }
    prediction_hash = forward_validation._hash(record_prediction)
    record_binding = forward_policy.bind_observation_commitment(
        committed_micro_binding, prediction_hash
    )
    record_commitment = {
        "schema_version": "memory-forward-commitment/2.0.0",
        "prediction_payload": record_prediction,
        "prediction_hash": prediction_hash,
        "forward_policy_binding": record_binding,
    }
    record_commitment["commitment_hash"] = forward_validation._hash(record_commitment)
    snapshot = {
        "match_id": fixture_id,
        "forward_policy_binding": record_binding,
        "forward_validation_ledger": archive,
        "forward_prediction_commitment": record_commitment,
    }
    receipt = {
        "schema_version": forward_validation.HISTORY_RECORD_RECEIPT_SCHEMA_VERSION,
        "fixture_id": fixture_id,
        "record_archived_at": archived_at,
        "archive_version_hash": forward_validation._hash(snapshot),
        "record_commitment_hash": record_commitment["commitment_hash"],
        "record_binding_hash": record_binding["binding_hash"],
        "prematch_ledger_hash": archive["ledger_hash"],
        "ledger_payload_hash": forward_validation._hash(payload),
        "market_commitments": market_commitments,
        "ledger_payload": payload,
        "archive_snapshot_payload": snapshot,
    }
    receipt["receipt_hash"] = forward_validation._hash(receipt)
    return receipt


def aggregate_from_micro_ledgers(payloads: list[dict]) -> dict:
    if not payloads:
        raise AssertionError("aggregate test fixture requires payloads")
    first = payloads[0]
    preliminary_receipts = sorted(
        [history_receipt_from_micro_ledger(item) for item in payloads],
        key=lambda item: item["fixture_id"],
    )
    manifest = cohort_record_manifest(
        first["cohort_manifest"],
        [
            {
                "fixture_id": receipt["fixture_id"],
                "archive_version_hash": receipt["archive_version_hash"],
                "record_commitment_hash": receipt["record_commitment_hash"],
                "record_binding_hash": receipt["record_binding_hash"],
                "prematch_ledger_hash": receipt["prematch_ledger_hash"],
            }
            for receipt in preliminary_receipts
        ],
    )
    closure = cohort_closure(first["cohort_manifest"], manifest)
    for payload in payloads:
        payload["cohort_closure"] = deepcopy(closure)
    receipts = sorted(
        [history_receipt_from_micro_ledger(item) for item in payloads],
        key=lambda item: item["fixture_id"],
    )
    binding = {
        "schema_version": forward_validation.HISTORY_LEDGER_BINDING_SCHEMA_VERSION,
        "artifact_type": forward_validation.HISTORY_AGGREGATE_ARTIFACT_TYPE,
        "cohort_id": first["cohort_id"],
        "policy_id": first["policy_id"],
        "policy_hash": first["policy_hash"],
        "fixture_ids": [item["fixture_id"] for item in receipts],
        "receipts": receipts,
    }
    binding["binding_hash"] = forward_validation._hash(binding)
    return {
        "schema_version": forward_validation.INPUT_SCHEMA_VERSION,
        "artifact_type": forward_validation.HISTORY_AGGREGATE_ARTIFACT_TYPE,
        "cohort_id": first["cohort_id"],
        "policy_id": first["policy_id"],
        "policy_hash": first["policy_hash"],
        "policy_manifest": deepcopy(first["policy_manifest"]),
        "cohort_manifest": deepcopy(first["cohort_manifest"]),
        "cohort_closure": deepcopy(closure),
        "history_ledger_binding": binding,
    }


def build_aggregate_payload(
    base: Path,
    count: int = 6,
    *,
    one_week: bool = False,
    git_commit: str = REAL_GIT_COMMIT,
) -> dict:
    payloads: list[dict] = []
    for index in range(count):
        fixture_base = base / f"fixture-{index}"
        fixture_base.mkdir(parents=True, exist_ok=True)
        payloads.append(
            build_payload(
                fixture_base,
                1,
                one_week=one_week,
                start_index=index,
                git_commit=git_commit,
            )
        )
    return aggregate_from_micro_ledgers(payloads)


def pending_micro_ledger(base: Path, *, start_index: int = 0) -> dict:
    payload = build_payload(base, 1, start_index=start_index)
    payload.pop("history_ledger_binding", None)
    payload["cohort_closure"] = None
    prediction = payload["commitments"][0]["prediction_payload"]
    prediction["model_probabilities"] = {"H": 0.5, "D": 0.25, "A": 0.25}
    archived_at = (
        datetime.fromisoformat(prediction["generated_at"]) + timedelta(minutes=1)
    ).isoformat()
    binding = forward_policy.bind_observation_commitment(
        base_policy_binding(
            payload["policy_manifest"], payload["cohort_manifest"], archived_at
        ),
        forward_validation._hash(prediction),
    )
    payload["commitments"][0] = seal(
        {
            "schema_version": forward_validation.COMMITMENT_SCHEMA_VERSION,
            "prediction_payload": prediction,
            "forward_policy_binding": binding,
        },
        "commitment_hash",
    )
    settlement = payload["settlements"][0]
    settlement.update(
        {
            "commitment_hash": payload["commitments"][0]["commitment_hash"],
            "status": "pending",
            "observed_settlement_state": None,
            "result_collected_at": None,
            "result_source_evidence_hash": None,
            "closing_snapshot": None,
        }
    )
    settlement.pop("settlement_hash")
    payload["settlements"][0] = seal(settlement, "settlement_hash")
    forward_validation.validate_prematch_input(payload)
    return payload


def five_state_pending_micro_ledger(
    base: Path,
    *,
    start_index: int,
    family: str,
    line: float,
    reference_outcome: str,
    expected_state: str,
    additional_source_line: float | None = None,
) -> dict:
    """Build a canonical split-line ledger by resealing every affected artifact."""

    payload = build_payload(base, 1, start_index=start_index)
    payload["cohort_closure"] = None
    policy = payload["policy_manifest"]
    cohort = payload["cohort_manifest"]
    original_prediction = payload["commitments"][0]["prediction_payload"]
    kickoff = original_prediction["kickoff"]
    generated = original_prediction["generated_at"]
    fixture_id = original_prediction["fixture_id"]
    home_team = "Alpha"
    away_team = "Bravo"
    league = "test_league"
    price_outcomes = ["over", "under"] if family == "total" else ["home", "away"]
    identity = {
        "family": family,
        "period": "full_time",
        "line": line,
        "price_outcomes": price_outcomes,
    }
    identity_hash = source_evidence.market_identity_hash(identity)
    queue_key = forward_validation._queue_key(
        cohort["cohort_id"], fixture_id, identity_hash
    )
    prices = {
        outcome: 20.0 if outcome == reference_outcome else 1.01
        for outcome in price_outcomes
    }
    source_url = f"https://zq.titan007.com/analysis/{fixture_id}cn.htm"
    raw_file = base / f"raw-five-state-{fixture_id}.json"
    raw_markets = [
        {
            "market_identity": identity,
            "odds_format": "decimal",
            "firms": [
                {"name": name, "outcomes": prices} for name in ("A", "B", "C", "D", "E")
            ],
        }
    ]
    if additional_source_line is not None:
        additional_identity = {
            "family": family,
            "period": "full_time",
            "line": additional_source_line,
            "price_outcomes": price_outcomes,
        }
        additional_prices = {outcome: 2.0 for outcome in price_outcomes}
        raw_markets.append(
            {
                "market_identity": additional_identity,
                "odds_format": "decimal",
                "firms": [
                    {"name": name, "outcomes": additional_prices}
                    for name in ("A", "B", "C", "D", "E")
                ],
            }
        )
    raw_file.write_text(
        json.dumps(
            {
                "schema_version": source_evidence.RAW_SCHEMA_VERSION,
                "source_url": source_url,
                "collected_at": generated,
                "fixture": {
                    "match_id": fixture_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "kickoff": kickoff,
                },
                "markets": raw_markets,
            }
        ),
        encoding="utf-8",
    )
    evidence_file, evidence = source_evidence.build_evidence(
        [raw_file], output_dir=base / f"evidence-five-state-{fixture_id}"
    )
    queue = seal(
        {
            "schema_version": forward_validation.QUEUE_SCHEMA_VERSION,
            "artifact_type": "soccer_forward_eligibility_queue",
            "queue_id": "queue-a",
            "cohort_id": cohort["cohort_id"],
            "policy_id": policy["policy_id"],
            "policy_hash": policy["policy_hash"],
            "frozen_at": payload["queue_manifest"]["frozen_at"],
            "entries": [
                {
                    "fixture_id": fixture_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "league": league,
                    "market_identity": identity,
                    "market_identity_hash": identity_hash,
                    "kickoff": kickoff,
                    "queue_key": queue_key,
                }
            ],
            "integrity_assurance": "local_content_hash_only_no_external_timestamp",
        },
        "queue_hash",
    )
    probabilities = {
        state: 0.05 for state in forward_validation.FIVE_STATE_SETTLEMENT_STATES
    }
    probabilities[expected_state] = 0.8
    baseline = {
        state: 1.0 / len(forward_validation.FIVE_STATE_SETTLEMENT_STATES)
        for state in forward_validation.FIVE_STATE_SETTLEMENT_STATES
    }
    lineage = {
        name: {
            "kind": "frozen_baseline_artifact",
            "generated_at": generated,
            "training_cutoff": generated,
            "artifact_hash": TEST_HASH,
        }
        for name in forward_validation.MODEL_SPACE_BASELINE_NAMES
    }
    prediction = {
        **deepcopy(original_prediction),
        "home_team": home_team,
        "away_team": away_team,
        "league": league,
        "queue_hash": queue["queue_hash"],
        "queue_key": queue_key,
        "observation_id": forward_validation._observation_id(queue_key),
        "market_identity": identity,
        "market_identity_hash": identity_hash,
        "settlement_reference_outcome": reference_outcome,
        "model_probabilities": probabilities,
        "baselines": {
            name: deepcopy(baseline)
            for name in forward_validation.MODEL_SPACE_BASELINE_NAMES
        },
        "baseline_lineage": lineage,
        "bookmaker_snapshot": {
            "collected_at": generated,
            "source_evidence_file": str(evidence_file),
            "source_evidence_hash": evidence["evidence_hash"],
            "source_url": source_url,
            "firm_count": 5,
            "price_basis": "median",
            "odds_format": "decimal",
            "complete_market_odds": prices,
            "no_vig_method": "multiplicative_normalization",
        },
        "execution_entry": {
            "selection": reference_outcome,
            "entry_decimal_odds": prices[reference_outcome],
            "entry_complete_market_odds": deepcopy(prices),
            "entry_collected_at": generated,
            "entry_source_evidence_hash": evidence["evidence_hash"],
            "entry_price_kind": "executable_after_slippage",
            "limit_verified": True,
            "stake_units": 1.0,
        },
    }
    archived_at = payload["commitments"][0]["forward_policy_binding"]["archived_at"]
    binding = forward_policy.bind_observation_commitment(
        base_policy_binding(policy, cohort, archived_at),
        forward_validation._hash(prediction),
    )
    commitment = seal(
        {
            "schema_version": forward_validation.COMMITMENT_SCHEMA_VERSION,
            "prediction_payload": prediction,
            "forward_policy_binding": binding,
        },
        "commitment_hash",
    )
    payload.update(
        {
            "market_schemas": {
                family: {
                    "settlement_states": list(
                        forward_validation.FIVE_STATE_SETTLEMENT_STATES
                    ),
                    "settlement_semantics": "five_state_return",
                }
            },
            "queue_manifest": queue,
            "commitments": [commitment],
            "settlements": [
                seal(
                    {
                        "schema_version": forward_validation.SETTLEMENT_SCHEMA_VERSION,
                        "observation_id": prediction["observation_id"],
                        "commitment_hash": commitment["commitment_hash"],
                        "status": "pending",
                        "observed_settlement_state": None,
                        "result_collected_at": None,
                        "result_source_evidence_hash": None,
                        "closing_snapshot": None,
                    },
                    "settlement_hash",
                )
            ],
        }
    )
    forward_validation.validate_prematch_input(payload)
    return payload


def categorical_half_time_pending_micro_ledger(
    base: Path, *, start_index: int = 0, selection: str = "H"
) -> dict:
    payload = build_payload(base, 1, start_index=start_index)
    payload["cohort_closure"] = None
    policy = payload["policy_manifest"]
    cohort = payload["cohort_manifest"]
    original = payload["commitments"][0]["prediction_payload"]
    fixture_id = original["fixture_id"]
    kickoff = original["kickoff"]
    generated = original["generated_at"]
    home_team = "Alpha"
    away_team = "Bravo"
    league = "test_league"
    identity = {
        "family": "1x2",
        "period": "first_half",
        "line": None,
        "price_outcomes": ["H", "D", "A"],
    }
    identity_hash = source_evidence.market_identity_hash(identity)
    queue_key = forward_validation._queue_key(
        cohort["cohort_id"], fixture_id, identity_hash
    )
    prices = {outcome: 20.0 if outcome == selection else 1.01 for outcome in OUTCOMES}
    source_url = f"https://zq.titan007.com/analysis/{fixture_id}cn.htm"
    raw_file = base / f"raw-half-time-{fixture_id}.json"
    raw_file.write_text(
        json.dumps(
            {
                "schema_version": source_evidence.RAW_SCHEMA_VERSION,
                "source_url": source_url,
                "collected_at": generated,
                "fixture": {
                    "match_id": fixture_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "kickoff": kickoff,
                },
                "markets": [
                    {
                        "market_identity": identity,
                        "odds_format": "decimal",
                        "firms": [
                            {"name": name, "outcomes": prices}
                            for name in ("A", "B", "C", "D", "E")
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence_file, evidence = source_evidence.build_evidence(
        [raw_file], output_dir=base / f"evidence-half-time-{fixture_id}"
    )
    queue = seal(
        {
            "schema_version": forward_validation.QUEUE_SCHEMA_VERSION,
            "artifact_type": "soccer_forward_eligibility_queue",
            "queue_id": "queue-half-time",
            "cohort_id": cohort["cohort_id"],
            "policy_id": policy["policy_id"],
            "policy_hash": policy["policy_hash"],
            "frozen_at": payload["queue_manifest"]["frozen_at"],
            "entries": [
                {
                    "fixture_id": fixture_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "league": league,
                    "market_identity": identity,
                    "market_identity_hash": identity_hash,
                    "kickoff": kickoff,
                    "queue_key": queue_key,
                }
            ],
            "integrity_assurance": "local_content_hash_only_no_external_timestamp",
        },
        "queue_hash",
    )
    inverse = {outcome: 1.0 / price for outcome, price in prices.items()}
    inverse_total = sum(inverse.values())
    bookmaker_no_vig = {
        outcome: probability / inverse_total for outcome, probability in inverse.items()
    }
    lineage = {
        name: {
            "kind": (
                "replayable_source_snapshot"
                if name == "bookmaker_no_vig"
                else "frozen_baseline_artifact"
            ),
            "generated_at": generated,
            "training_cutoff": generated,
            "artifact_hash": (
                evidence["evidence_hash"] if name == "bookmaker_no_vig" else TEST_HASH
            ),
        }
        for name in forward_validation.BASELINE_NAMES
    }
    prediction = {
        **deepcopy(original),
        "queue_hash": queue["queue_hash"],
        "queue_key": queue_key,
        "home_team": home_team,
        "away_team": away_team,
        "league": league,
        "observation_id": forward_validation._observation_id(queue_key),
        "market_identity": identity,
        "market_identity_hash": identity_hash,
        "status": "predicted",
        "settlement_reference_outcome": None,
        "model_probabilities": {"H": 0.5, "D": 0.25, "A": 0.25},
        "baselines": {
            "historical_frequency": {"H": 0.4, "D": 0.3, "A": 0.3},
            "independent_htft": {"H": 0.4, "D": 0.3, "A": 0.3},
            "simple_poisson_dc": {"H": 0.45, "D": 0.3, "A": 0.25},
            "bookmaker_no_vig": bookmaker_no_vig,
        },
        "baseline_lineage": lineage,
        "bookmaker_snapshot": {
            "collected_at": generated,
            "source_evidence_file": str(evidence_file),
            "source_evidence_hash": evidence["evidence_hash"],
            "source_url": source_url,
            "firm_count": 5,
            "price_basis": "median",
            "odds_format": "decimal",
            "complete_market_odds": prices,
            "no_vig_method": "multiplicative_normalization",
        },
        "execution_entry": None,
        "unavailable_reasons": [],
    }
    archived_at = (datetime.fromisoformat(generated) + timedelta(minutes=1)).isoformat()
    binding = forward_policy.bind_observation_commitment(
        base_policy_binding(policy, cohort, archived_at),
        forward_validation._hash(prediction),
    )
    commitment = seal(
        {
            "schema_version": forward_validation.COMMITMENT_SCHEMA_VERSION,
            "prediction_payload": prediction,
            "forward_policy_binding": binding,
        },
        "commitment_hash",
    )
    settlement = seal(
        {
            "schema_version": forward_validation.SETTLEMENT_SCHEMA_VERSION,
            "observation_id": prediction["observation_id"],
            "commitment_hash": commitment["commitment_hash"],
            "status": "pending",
            "observed_settlement_state": None,
            "result_collected_at": None,
            "result_source_evidence_hash": None,
            "closing_snapshot": None,
        },
        "settlement_hash",
    )
    payload.update(
        {
            "market_schemas": {
                "1x2": {
                    "settlement_states": OUTCOMES,
                    "settlement_semantics": "categorical",
                }
            },
            "queue_manifest": queue,
            "commitments": [commitment],
            "settlements": [settlement],
        }
    )
    forward_validation.validate_prematch_input(payload)
    return payload


_FORWARD_MEMORY_JOINT_MODEL: dict | None = None


def active_record_args_for_five_state_ledger(
    base: Path,
    ledger: dict,
    ledger_file: Path,
    *,
    history_base: Path | None = None,
) -> SimpleNamespace:
    """Build the real model and candidate-v3 inputs consumed by ``cmd_record``."""

    from test_memory_store import JOINT_SAMPLE_ROWS, memory_store, record_args

    from scripts import htft_model, joint_scenario_model, score_model

    global _FORWARD_MEMORY_JOINT_MODEL
    if _FORWARD_MEMORY_JOINT_MODEL is None:
        history_file = base / "forward-joint-history.csv"
        with history_file.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(
                [
                    "date",
                    "home_team",
                    "away_team",
                    "home_goals",
                    "away_goals",
                    "half_home_goals",
                    "half_away_goals",
                ]
            )
            writer.writerows(JOINT_SAMPLE_ROWS)
        _FORWARD_MEMORY_JOINT_MODEL = htft_model.fit_model(
            history_file,
            iterations=30,
            learning_rate=0.025,
            regularization=0.03,
            half_time_half_life_days=180.0,
            second_half_half_life_days=180.0,
            full_time_half_life_days=180.0,
            competition_key="test_league",
            dataset_manifest_hash="sha256:" + "a" * 64,
        )
        _FORWARD_MEMORY_JOINT_MODEL["generated_at"] = "2026-07-20T00:00:00Z"
        for component in _FORWARD_MEMORY_JOINT_MODEL["components"].values():
            component["generated_at"] = "2026-07-20T00:00:00Z"
        htft_model.validate_model(_FORWARD_MEMORY_JOINT_MODEL)
    model = deepcopy(_FORWARD_MEMORY_JOINT_MODEL)
    prediction = ledger["commitments"][0]["prediction_payload"]
    kickoff = str(prediction["kickoff"])
    decision_time = datetime.fromisoformat(str(prediction["generated_at"]))
    upstream_time = (decision_time - timedelta(minutes=1)).isoformat()
    score = score_model.predict_model(
        model["components"]["full_time"],
        prediction["home_team"],
        prediction["away_team"],
        kickoff=kickoff,
        generated_at=upstream_time,
        max_goals=12,
        hard_max_goals=30,
    )
    htft = htft_model.predict_model(
        model,
        prediction["home_team"],
        prediction["away_team"],
        kickoff=kickoff,
        generated_at=upstream_time,
        max_goals=12,
        hard_max_goals=30,
    )
    joint = joint_scenario_model.predict_joint_scenarios(
        model,
        score,
        htft,
        generated_at=prediction["generated_at"],
        expected_match_id=prediction["fixture_id"],
    )
    htft_matrix = htft["htft"]["code_probabilities"]
    half = {
        result: sum(htft_matrix[f"{result}{full}"] for full in "HDA")
        for result in "HDA"
    }
    full = {
        result: sum(htft_matrix[f"{half_result}{result}"] for half_result in "HDA")
        for result in "HDA"
    }
    ranking = memory_store.htft_ranker.rank_htft(
        htft_matrix,
        half,
        full,
        league_key=prediction["league"],
        model_hash=htft["model_hash"],
    )
    artifact_paths = {
        "score_model_file": base / "active-score.json",
        "htft_observation_model_file": base / "active-htft.json",
        "htft_observation_ranker_file": base / "active-htft-ranker.json",
        "joint_scenario_file": base / "active-joint.json",
    }
    for name, value in (
        ("score_model_file", score),
        ("htft_observation_model_file", htft),
        ("htft_observation_ranker_file", ranking),
        ("joint_scenario_file", joint),
    ):
        artifact_paths[name].write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )

    identity = prediction["market_identity"]
    family = str(identity["family"])
    bookmaker = prediction["bookmaker_snapshot"]
    candidate_specs: list[dict] = []
    if family in {"asian", "total"}:
        candidate_market = family
        reference = str(prediction["settlement_reference_outcome"])
        for outcome in identity["price_outcomes"]:
            raw_line = float(identity["line"])
            if family == "asian" and outcome == "away":
                raw_line = -raw_line
            identity_fields = {
                "market": family,
                "side": outcome,
                "line": raw_line,
            }
            distribution = memory_store.matrix_settlement_distribution(
                joint["full_time_score_marginal"]["probabilities"],
                family,
                identity_fields,
            )
            candidate_specs.append(
                {
                    "identity_fields": identity_fields,
                    "reference": outcome,
                    "distribution": distribution,
                }
            )
        selected_spec = next(
            item for item in candidate_specs if item["reference"] == reference
        )
        prediction["model_probabilities"] = deepcopy(selected_spec["distribution"])
    elif family == "1x2" and identity["period"] == "first_half":
        candidate_market = "half_time"
        reference = max(
            identity["price_outcomes"],
            key=lambda outcome: bookmaker["complete_market_odds"][outcome],
        )
        half_matrix = joint["half_time_score_marginal"]["probabilities"]
        for outcome in identity["price_outcomes"]:
            side = {"H": "home", "D": "draw", "A": "away"}[outcome]
            identity_fields = {
                "market": "half_time",
                "submarket": "1x2",
                "side": side,
                "line": None,
            }
            distribution = memory_store.matrix_settlement_distribution(
                half_matrix,
                "half_time",
                {"market": "1x2", "side": side, "line": None},
            )
            candidate_specs.append(
                {
                    "identity_fields": identity_fields,
                    "reference": outcome,
                    "distribution": distribution,
                }
            )
        categorical = {"H": 0.0, "D": 0.0, "A": 0.0}
        for home, row in enumerate(half_matrix):
            for away, probability in enumerate(row):
                outcome = "H" if home > away else "D" if home == away else "A"
                categorical[outcome] += float(probability)
        prediction["model_probabilities"] = categorical
    else:
        raise AssertionError(
            "active candidate test helper received an unsupported identity"
        )
    previous_binding = ledger["commitments"][0]["forward_policy_binding"]
    base_binding = deepcopy(previous_binding)
    base_binding.pop("binding_hash")
    base_binding.pop("observation_commitment_hash")
    base_binding["schema_version"] = (
        forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION
    )
    base_binding["binding_hash"] = forward_policy._hash_json(base_binding)
    committed_binding = forward_policy.bind_observation_commitment(
        base_binding, forward_validation._hash(prediction)
    )
    ledger["commitments"][0] = seal(
        {
            "schema_version": forward_validation.COMMITMENT_SCHEMA_VERSION,
            "prediction_payload": prediction,
            "forward_policy_binding": committed_binding,
        },
        "commitment_hash",
    )
    settlement = deepcopy(ledger["settlements"][0])
    settlement.pop("settlement_hash")
    settlement["observation_id"] = prediction["observation_id"]
    settlement["commitment_hash"] = ledger["commitments"][0]["commitment_hash"]
    ledger["settlements"][0] = seal(settlement, "settlement_hash")
    forward_validation.validate_prematch_input(ledger)
    ledger_file.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    candidate_payload = {
        "artifact_type": memory_store.CANDIDATE_EVALUATION_ARTIFACT_TYPE,
        "schema_version": memory_store.CANDIDATE_EVALUATION_SCHEMA_VERSION,
        "policy_version": memory_store.STRICT_OOS_POLICY_VERSION,
        "selection_policy_version": memory_store.CONFIDENCE_POLICY_VERSION,
        "generated_at": prediction["generated_at"],
        "fixture": {
            "match_id": prediction["fixture_id"],
            "home_team": prediction["home_team"],
            "away_team": prediction["away_team"],
            "kickoff": prediction["kickoff"],
        },
        "market_manifest": [
            {
                "market": market,
                "status": "evaluated" if market == candidate_market else "unavailable",
                "reasons": []
                if market == candidate_market
                else [memory_store.ACTIVE_UNAVAILABLE_REASON_SOURCE_MISSING],
            }
            for market in memory_store.PRIMARY_MARKETS
        ],
        "candidates": [
            {
                **spec["identity_fields"],
                "market_identity": identity,
                "market_identity_hash": prediction["market_identity_hash"],
                "settlement_reference_outcome": spec["reference"],
                "probability": spec["distribution"]["full_win"]
                + spec["distribution"]["half_win"],
                "settlement_probabilities": spec["distribution"],
                "odds": bookmaker["complete_market_odds"][spec["reference"]],
                "odds_format": bookmaker["odds_format"],
                "market_complete": True,
                "complete_market_odds": bookmaker["complete_market_odds"],
                "market_source": bookmaker["source_url"],
                "market_collected_at": bookmaker["collected_at"],
                "price_basis": bookmaker["price_basis"],
                "firm_count": bookmaker["firm_count"],
                "market_signal": "aligned",
                "cover_distribution_validated": candidate_market == "asian",
            }
            for spec in candidate_specs
        ],
    }
    candidate_file = base / "candidate-evaluation.json"
    candidate_file.write_text(
        json.dumps(candidate_payload, ensure_ascii=False), encoding="utf-8"
    )

    matrix = score["score_matrix"]["probabilities"]
    ranked = sorted(
        (
            (float(probability), home, away)
            for home, row in enumerate(matrix)
            for away, probability in enumerate(row)
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    zero_zero_rank = next(
        rank
        for rank, (_probability, home, away) in enumerate(ranked, start=1)
        if home == 0 and away == 0
    )
    one_x_two = score["one_x_two"]
    args = record_args(
        str(history_base or base.parent),
        match_id=prediction["fixture_id"],
        analysis_stage="initial",
        league=prediction["league"],
        kickoff=prediction["kickoff"],
        source_kickoff=prediction["kickoff"],
        source_timezone="UTC",
        user_local_kickoff=prediction["kickoff"],
        user_timezone="UTC",
        home_team=prediction["home_team"],
        away_team=prediction["away_team"],
        predicted_score=f"{ranked[0][1]}-{ranked[0][2]}",
        exact_score_pick=[
            f"{home}-{away}:{probability:.17g}"
            for probability, home, away in ranked[:2]
        ],
        zero_zero_probability=float(matrix[0][0]),
        zero_zero_rank=zero_zero_rank,
        source_url=bookmaker["source_url"],
        source_evidence_file=bookmaker["source_evidence_file"],
        forward_validation_ledger=str(ledger_file),
        candidate_evaluation_file=str(candidate_file),
        require_candidate_evaluations=False,
        require_complete_analysis=True,
        data_quality="high",
        home_win_probability=float(one_x_two["home"]),
        draw_probability=float(one_x_two["draw"]),
        away_win_probability=float(one_x_two["away"]),
        primary_market="total" if candidate_market == "total" else None,
        total_side=reference if candidate_market == "total" else None,
        total_line=(float(identity["line"]) if candidate_market == "total" else 2.5),
        asian_side=None,
        model_version=score["model_version"],
        **{name: str(path) for name, path in artifact_paths.items()},
    )
    if candidate_market == "total":
        selected_spec = next(
            item for item in candidate_specs if item["reference"] == reference
        )
        distribution = selected_spec["distribution"]
        selected_odds = float(bookmaker["complete_market_odds"][reference])
        implied = {
            outcome: 1.0 / float(price)
            for outcome, price in bookmaker["complete_market_odds"].items()
        }
        implied_total = sum(implied.values())
        market_probability = implied[reference] / implied_total
        effective_probability = memory_store.effective_settlement_win_probability(
            distribution, "active total test candidate"
        )
        args.primary_market = "total"
        args.total_side = reference
        args.total_line = float(identity["line"])
        args.total_odds = selected_odds
        args.total_odds_format = "decimal"
        args.total_probability = distribution["full_win"] + distribution["half_win"]
        args.total_ev = (
            distribution["full_win"] * (selected_odds - 1.0)
            + distribution["half_win"] * (selected_odds - 1.0) / 2.0
            - distribution["half_loss"] / 2.0
            - distribution["loss"]
        )
        args.total_edge_pp = (float(effective_probability) - market_probability) * 100.0
        args.total_firm_count = bookmaker["firm_count"]
        args.total_market_complete = True
        args.total_market_odds = [
            f"{outcome}:{price}"
            for outcome, price in bookmaker["complete_market_odds"].items()
        ]
        args.total_market_probability = market_probability
        args.total_market_source = bookmaker["source_url"]
        args.total_market_collected_at = bookmaker["collected_at"]
        args.total_price_basis = bookmaker["price_basis"]
        args.total_full_win_probability = distribution["full_win"]
        args.total_half_win_probability = distribution["half_win"]
        args.total_push_probability = distribution["push"]
        args.total_half_loss_probability = distribution["half_loss"]
        args.total_loss_probability = distribution["loss"]
        args.total_market_signal = "aligned"
    return args


def reseal_single_commitment_ledger(ledger: dict) -> dict:
    """Reseal one current commitment after a deliberate test mutation."""

    prediction = ledger["commitments"][0]["prediction_payload"]
    previous_binding = ledger["commitments"][0]["forward_policy_binding"]
    base_binding = deepcopy(previous_binding)
    base_binding.pop("binding_hash")
    base_binding.pop("observation_commitment_hash")
    base_binding["schema_version"] = (
        forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION
    )
    base_binding["binding_hash"] = forward_policy._hash_json(base_binding)
    committed_binding = forward_policy.bind_observation_commitment(
        base_binding, forward_validation._hash(prediction)
    )
    ledger["commitments"][0] = seal(
        {
            "schema_version": forward_validation.COMMITMENT_SCHEMA_VERSION,
            "prediction_payload": prediction,
            "forward_policy_binding": committed_binding,
        },
        "commitment_hash",
    )
    settlement = deepcopy(ledger["settlements"][0])
    settlement.pop("settlement_hash")
    settlement["observation_id"] = prediction["observation_id"]
    settlement["commitment_hash"] = ledger["commitments"][0]["commitment_hash"]
    ledger["settlements"][0] = seal(settlement, "settlement_hash")
    forward_validation.validate_prematch_input(ledger)
    return base_binding


def all_unavailable_active_inputs(
    base: Path, *, start_index: int = 0, unavailable_source: bool = True
) -> tuple[dict, Path, SimpleNamespace, dict]:
    """Build an honest all-unavailable denominator with one fixture sentinel."""

    from test_memory_store import memory_store

    ledger = five_state_pending_micro_ledger(
        base,
        start_index=start_index,
        family="total",
        line=2.5,
        reference_outcome="over",
        expected_state="full_win",
    )
    ledger_file = base / "prematch-forward-ledger.json"
    ledger_file.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    args = active_record_args_for_five_state_ledger(
        base, ledger, ledger_file, history_base=base
    )
    prediction = ledger["commitments"][0]["prediction_payload"]
    source_url = str(args.source_url)
    if unavailable_source:
        unavailable_raw = base / "raw-all-unavailable.json"
        unavailable_raw.write_text(
            json.dumps(
                {
                    "schema_version": source_evidence.RAW_SCHEMA_VERSION,
                    "source_url": source_url,
                    "collected_at": prediction["generated_at"],
                    "fixture": {
                        "match_id": prediction["fixture_id"],
                        "home_team": prediction["home_team"],
                        "away_team": prediction["away_team"],
                        "kickoff": prediction["kickoff"],
                    },
                    "availability_status": "unavailable",
                    "unavailable_reasons": ["provider market tables absent"],
                    "markets": [],
                }
            ),
            encoding="utf-8",
        )
        evidence_file, _evidence = source_evidence.build_evidence(
            [unavailable_raw], output_dir=base / "evidence-all-unavailable"
        )
        args.source_evidence_file = str(evidence_file)

    candidate_file = Path(args.candidate_evaluation_file)
    candidate_payload = json.loads(candidate_file.read_text(encoding="utf-8"))
    candidate_payload["market_manifest"] = [
        {
            "market": market,
            "status": "unavailable",
            "reasons": [memory_store.ACTIVE_UNAVAILABLE_REASON_SOURCE_MISSING],
        }
        for market in memory_store.PRIMARY_MARKETS
    ]
    candidate_payload["candidates"] = []
    candidate_file.write_text(
        json.dumps(candidate_payload, ensure_ascii=False), encoding="utf-8"
    )
    args.total_side = None
    args.primary_market = None
    args.display_exact_score_pick = []
    args.display_exact_score_event_probability = None

    identity = {
        "family": "1x2",
        "period": "full_time",
        "line": None,
        "price_outcomes": ["H", "D", "A"],
    }
    identity_hash = source_evidence.market_identity_hash(identity)
    queue = deepcopy(ledger["queue_manifest"])
    queue.pop("queue_hash")
    queue_key = forward_validation._queue_key(
        queue["cohort_id"], prediction["fixture_id"], identity_hash
    )
    queue["entries"] = [
        {
            "fixture_id": prediction["fixture_id"],
            "home_team": prediction["home_team"],
            "away_team": prediction["away_team"],
            "league": prediction["league"],
            "market_identity": identity,
            "market_identity_hash": identity_hash,
            "kickoff": prediction["kickoff"],
            "queue_key": queue_key,
        }
    ]
    ledger["queue_manifest"] = seal(queue, "queue_hash")
    prediction.update(
        {
            "queue_hash": ledger["queue_manifest"]["queue_hash"],
            "queue_key": queue_key,
            "observation_id": forward_validation._observation_id(queue_key),
            "market_identity": identity,
            "market_identity_hash": identity_hash,
            "status": "unavailable",
            "settlement_reference_outcome": None,
            "model_probabilities": None,
            "baselines": {},
            "baseline_lineage": {},
            "bookmaker_snapshot": None,
            "execution_entry": None,
            "unavailable_reasons": [
                f"{market}:{memory_store.ACTIVE_UNAVAILABLE_REASON_SOURCE_MISSING}"
                for market in memory_store.PRIMARY_MARKETS
            ],
        }
    )
    ledger["market_schemas"] = {
        "1x2": {
            "settlement_states": ["H", "D", "A"],
            "settlement_semantics": "categorical",
        }
    }
    base_binding = reseal_single_commitment_ledger(ledger)
    ledger_file.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    return ledger, ledger_file, args, base_binding


def record_and_review_five_state_fixture(
    base: Path,
    *,
    start_index: int,
    family: str,
    line: float,
    reference_outcome: str,
    expected_state: str,
    home_score: int,
    away_score: int,
) -> dict:
    from test_memory_store import memory_store

    fixture_base = base / f"five-state-fixture-{start_index}"
    fixture_base.mkdir(parents=True, exist_ok=True)
    ledger = five_state_pending_micro_ledger(
        fixture_base,
        start_index=start_index,
        family=family,
        line=line,
        reference_outcome=reference_outcome,
        expected_state=expected_state,
    )
    ledger_file = fixture_base / "prematch-forward-ledger.json"
    ledger_file.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    prediction = ledger["commitments"][0]["prediction_payload"]
    committed = ledger["commitments"][0]["forward_policy_binding"]
    base_binding = deepcopy(committed)
    base_binding.pop("binding_hash")
    base_binding.pop("observation_commitment_hash")
    base_binding["schema_version"] = (
        forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION
    )
    base_binding["binding_hash"] = forward_policy._hash_json(base_binding)
    archived_at = datetime.fromisoformat(base_binding["archived_at"])
    args = active_record_args_for_five_state_ledger(fixture_base, ledger, ledger_file)
    with (
        mock.patch.object(
            memory_store.forward_policy,
            "load_active_binding",
            return_value=deepcopy(base_binding),
        ),
        mock.patch.object(memory_store, "utc_now", return_value=archived_at),
    ):
        memory_store.cmd_record(args)
    result_collected = datetime.fromisoformat(prediction["kickoff"]) + timedelta(
        hours=2
    )
    review_args = SimpleNamespace(
        base_dir=str(base),
        verified_finished=True,
        match_id=prediction["fixture_id"],
        home_score=home_score,
        away_score=away_score,
        half_home_score=0,
        half_away_score=0,
        home_corners=None,
        away_corners=None,
        key_learning="canonical five-state settlement replay",
        verification_source=f"https://example.test/final/{prediction['fixture_id']}",
        verification_collected_at=result_collected.isoformat(),
    )
    with mock.patch.object(
        memory_store, "utc_now", return_value=result_collected + timedelta(minutes=1)
    ):
        return memory_store.cmd_review(review_args)["record"]


def record_and_review_forward_fixture(base: Path, index: int) -> dict:
    from test_memory_store import memory_store

    fixture_base = base / f"real-fixture-{index}"
    fixture_base.mkdir(parents=True, exist_ok=True)
    ledger = five_state_pending_micro_ledger(
        fixture_base,
        start_index=index,
        family="total",
        line=2.5,
        reference_outcome="over",
        expected_state="full_win",
    )
    ledger_file = fixture_base / "prematch-forward-ledger.json"
    ledger_file.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    prediction = ledger["commitments"][0]["prediction_payload"]
    committed = ledger["commitments"][0]["forward_policy_binding"]
    base_binding = deepcopy(committed)
    base_binding.pop("binding_hash")
    base_binding.pop("observation_commitment_hash")
    base_binding["schema_version"] = (
        forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION
    )
    base_binding["binding_hash"] = forward_policy._hash_json(base_binding)
    archived_at = datetime.fromisoformat(base_binding["archived_at"])
    args = active_record_args_for_five_state_ledger(fixture_base, ledger, ledger_file)
    with (
        mock.patch.object(
            memory_store.forward_policy,
            "load_active_binding",
            return_value=deepcopy(base_binding),
        ),
        mock.patch.object(memory_store, "utc_now", return_value=archived_at),
    ):
        memory_store.cmd_record(args)
    result_collected = datetime.fromisoformat(prediction["kickoff"]) + timedelta(
        hours=2
    )
    review_args = SimpleNamespace(
        base_dir=str(base),
        verified_finished=True,
        match_id=prediction["fixture_id"],
        home_score=1,
        away_score=0,
        half_home_score=0,
        half_away_score=0,
        home_corners=None,
        away_corners=None,
        key_learning="content-addressed forward replay",
        verification_source=f"https://example.test/final/{prediction['fixture_id']}",
        verification_collected_at=result_collected.isoformat(),
    )
    with mock.patch.object(
        memory_store,
        "utc_now",
        return_value=result_collected + timedelta(minutes=1),
    ):
        return memory_store.cmd_review(review_args)["record"]


def closed_cohort_for_memory_records(records: list[dict]) -> tuple[dict, dict]:
    from test_memory_store import memory_store

    ledger = records[0]["forward_validation_ledger"]["ledger_payload"]
    cohort = deepcopy(ledger["cohort_manifest"])
    manifest = memory_store.forward_record_manifest_for_records(
        records, cohort_manifest=cohort
    )
    return cohort, cohort_closure(cohort, manifest)


class ForwardValidationTests(unittest.TestCase):
    def test_execution_entry_cannot_invent_better_price_or_unreplayable_source_hash(
        self,
    ) -> None:
        attacks = (
            (
                "unreplayable source hash",
                lambda entry: entry.update(
                    entry_source_evidence_hash="sha256:" + "f" * 64
                ),
                "source evidence does not bind the replayed bookmaker snapshot",
            ),
            (
                "invented better price",
                lambda entry: (
                    entry.update(entry_decimal_odds=100.0),
                    entry["entry_complete_market_odds"].update(over=100.0),
                ),
                "cannot improve the selected replayed price",
            ),
        )
        for label, mutate, expected_error in attacks:
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                ledger = five_state_pending_micro_ledger(
                    base,
                    start_index=430,
                    family="total",
                    line=2.5,
                    reference_outcome="over",
                    expected_state="full_win",
                )
                entry = ledger["commitments"][0]["prediction_payload"][
                    "execution_entry"
                ]
                mutate(entry)
                with self.assertRaisesRegex(
                    forward_validation.ForwardValidationError, expected_error
                ):
                    reseal_single_commitment_ledger(ledger)

    def test_active_categorical_shadow_replays_without_fake_execution(self) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = categorical_half_time_pending_micro_ledger(
                base, start_index=40, selection="H"
            )
            ledger_file = base / "prematch-forward-ledger.json"
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            args = active_record_args_for_five_state_ledger(
                base, ledger, ledger_file, history_base=base
            )
            base_binding = reseal_single_commitment_ledger(ledger)
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
            ):
                recorded = memory_store.cmd_record(args)["record"]

            candidate_audit = next(
                audit
                for audit in recorded["candidate_audits"]
                if audit.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
            )
            candidate = candidate_audit["candidates"][0]
            committed_prediction = recorded["forward_validation_ledger"][
                "ledger_payload"
            ]["commitments"][0]["prediction_payload"]
            self.assertTrue(candidate["shadow_selected"])
            self.assertEqual(committed_prediction["status"], "predicted")
            self.assertIsNone(committed_prediction["execution_entry"])
            memory_store.validate_forward_record_prediction_commitment(recorded)

            prediction = ledger["commitments"][0]["prediction_payload"]
            result_collected = datetime.fromisoformat(
                prediction["kickoff"]
            ) + timedelta(hours=2)
            review_args = SimpleNamespace(
                base_dir=str(base),
                verified_finished=True,
                match_id=prediction["fixture_id"],
                home_score=1,
                away_score=0,
                half_home_score=1,
                half_away_score=0,
                home_corners=None,
                away_corners=None,
                key_learning="categorical half-time shadow replay",
                verification_source=(
                    f"https://example.test/final/{prediction['fixture_id']}"
                ),
                verification_collected_at=result_collected.isoformat(),
            )
            with mock.patch.object(
                memory_store,
                "utc_now",
                return_value=result_collected + timedelta(minutes=1),
            ):
                reviewed = memory_store.cmd_review(review_args)["record"]
            _cohort, closure = closed_cohort_for_memory_records([reviewed])
            exported = memory_store.forward_validation_input_for_records(
                [reviewed], cohort_closure=closure
            )
            normalized = forward_validation.validate_input(exported)
            report = forward_validation.evaluate(exported)

        row = normalized["records"][0]
        self.assertEqual(row["market_identity"]["period"], "first_half")
        self.assertIsNone(row["observed_settlement_state"])
        self.assertEqual(row["unverified_observed_settlement_state"], "H")
        self.assertIsNone(row["execution_entry"])
        self.assertEqual(
            row["model_probabilities"], committed_prediction["model_probabilities"]
        )
        self.assertEqual(
            report["overall"]["proper_score_gates"][
                forward_validation.CATEGORICAL_PROPER_SCORE_SPACE
            ]["eligible_graded_outputs"],
            0,
        )
        self.assertIn(
            "result_evidence_replay_unavailable", report["promotion_blockers"]
        )

    def test_active_record_requires_current_candidate_artifact_unconditionally(
        self,
    ) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = five_state_pending_micro_ledger(
                base,
                start_index=41,
                family="total",
                line=2.5,
                reference_outcome="over",
                expected_state="full_win",
            )
            ledger_file = base / "prematch-forward-ledger.json"
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            args = active_record_args_for_five_state_ledger(
                base, ledger, ledger_file, history_base=base
            )
            args.candidate_evaluation_file = None
            args.require_candidate_evaluations = False
            base_binding = reseal_single_commitment_ledger(ledger)
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
                self.assertRaisesRegex(
                    ValueError, "current --candidate-evaluation-file"
                ),
            ):
                memory_store.cmd_record(args)

    def test_active_record_rejects_missing_manifest_market_and_shrunk_queue(
        self,
    ) -> None:
        from test_memory_store import memory_store

        with self.subTest("missing one of eight market families"):
            with tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                ledger = five_state_pending_micro_ledger(
                    base,
                    start_index=42,
                    family="total",
                    line=2.5,
                    reference_outcome="over",
                    expected_state="full_win",
                )
                ledger_file = base / "prematch-forward-ledger.json"
                ledger_file.write_text(
                    json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
                )
                args = active_record_args_for_five_state_ledger(
                    base, ledger, ledger_file, history_base=base
                )
                candidate_file = Path(args.candidate_evaluation_file)
                candidate_payload = json.loads(
                    candidate_file.read_text(encoding="utf-8")
                )
                candidate_payload["market_manifest"].pop()
                candidate_file.write_text(
                    json.dumps(candidate_payload, ensure_ascii=False), encoding="utf-8"
                )
                base_binding = reseal_single_commitment_ledger(ledger)
                ledger_file.write_text(
                    json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
                )
                archived_at = datetime.fromisoformat(base_binding["archived_at"])
                with (
                    mock.patch.object(
                        memory_store.forward_policy,
                        "load_active_binding",
                        return_value=deepcopy(base_binding),
                    ),
                    mock.patch.object(
                        memory_store, "utc_now", return_value=archived_at
                    ),
                    self.assertRaisesRegex(
                        ValueError, "requires every supported market"
                    ),
                ):
                    memory_store.cmd_record(args)

        with self.subTest("source has two concrete lines but queue/candidate has one"):
            with tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                ledger = five_state_pending_micro_ledger(
                    base,
                    start_index=43,
                    family="total",
                    line=2.5,
                    reference_outcome="over",
                    expected_state="full_win",
                    additional_source_line=3.0,
                )
                ledger_file = base / "prematch-forward-ledger.json"
                ledger_file.write_text(
                    json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
                )
                args = active_record_args_for_five_state_ledger(
                    base, ledger, ledger_file, history_base=base
                )
                base_binding = reseal_single_commitment_ledger(ledger)
                ledger_file.write_text(
                    json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
                )
                archived_at = datetime.fromisoformat(base_binding["archived_at"])
                with (
                    mock.patch.object(
                        memory_store.forward_policy,
                        "load_active_binding",
                        return_value=deepcopy(base_binding),
                    ),
                    mock.patch.object(
                        memory_store, "utc_now", return_value=archived_at
                    ),
                    self.assertRaisesRegex(
                        ValueError, "does not exactly cover source-visible identities"
                    ),
                ):
                    memory_store.cmd_record(args)

    def test_source_visible_replayable_identity_cannot_be_marked_unavailable(
        self,
    ) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = five_state_pending_micro_ledger(
                base,
                start_index=431,
                family="total",
                line=2.5,
                reference_outcome="over",
                expected_state="full_win",
            )
            ledger_file = base / "prematch-forward-ledger.json"
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            args = active_record_args_for_five_state_ledger(
                base, ledger, ledger_file, history_base=base
            )
            candidate_file = Path(args.candidate_evaluation_file)
            candidate_payload = json.loads(candidate_file.read_text(encoding="utf-8"))
            total_manifest = next(
                item
                for item in candidate_payload["market_manifest"]
                if item["market"] == "total"
            )
            total_manifest.update(
                status="unavailable", reasons=["claimed_model_unavailable"]
            )
            candidate_payload["candidates"] = []
            candidate_file.write_text(
                json.dumps(candidate_payload, ensure_ascii=False), encoding="utf-8"
            )
            args.total_side = None
            args.primary_market = None
            args.display_exact_score_pick = []
            args.display_exact_score_event_probability = None

            prediction = ledger["commitments"][0]["prediction_payload"]
            prediction.update(
                status="unavailable",
                settlement_reference_outcome=None,
                model_probabilities=None,
                baselines={},
                baseline_lineage={},
                bookmaker_snapshot=None,
                execution_entry=None,
                unavailable_reasons=["claimed_model_unavailable"],
            )
            evidence = source_evidence.validate_evidence_file(args.source_evidence_file)
            self.assertIn(prediction["market_identity_hash"], evidence["market_index"])
            self.assertTrue(Path(args.joint_scenario_file).is_file())

            base_binding = reseal_single_commitment_ledger(ledger)
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
                self.assertRaisesRegex(
                    ValueError,
                    "replayable canonical model and decision-time price must be evaluated",
                ),
            ):
                memory_store.cmd_record(args)

    def test_active_formal_primary_must_match_candidate_source_and_ledger(
        self,
    ) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = five_state_pending_micro_ledger(
                base,
                start_index=432,
                family="total",
                line=2.5,
                reference_outcome="over",
                expected_state="full_win",
            )
            ledger_file = base / "prematch-forward-ledger.json"
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            args = active_record_args_for_five_state_ledger(
                base, ledger, ledger_file, history_base=base
            )
            score = json.loads(Path(args.score_model_file).read_text(encoding="utf-8"))
            matrix = score["score_matrix"]["probabilities"]
            probability = sum(
                float(cell)
                for home, row in enumerate(matrix)
                for away, cell in enumerate(row)
                if home > 0 and away > 0
            )
            yes_odds, no_odds = 10.0, 1.10
            yes_implied, no_implied = 1.0 / yes_odds, 1.0 / no_odds
            market_probability = yes_implied / (yes_implied + no_implied)
            prediction = ledger["commitments"][0]["prediction_payload"]
            args.total_side = None
            args.primary_market = "btts"
            args.display_exact_score_pick = []
            args.display_exact_score_event_probability = None
            args.btts_side = "yes"
            args.btts_odds = yes_odds
            args.btts_odds_format = "decimal"
            args.btts_probability = probability
            args.btts_ev = probability * yes_odds - 1.0
            args.btts_edge_pp = (probability - market_probability) * 100.0
            args.btts_firm_count = 1
            args.btts_market_signal = "aligned"
            args.btts_market_complete = True
            args.btts_market_odds = [f"yes:{yes_odds}", f"no:{no_odds}"]
            args.btts_market_probability = market_probability
            args.btts_market_source = prediction["bookmaker_snapshot"]["source_url"]
            args.btts_market_collected_at = prediction["generated_at"]
            args.btts_price_basis = "median"

            base_binding = reseal_single_commitment_ledger(ledger)
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
                self.assertRaisesRegex(
                    ValueError,
                    "formal btts pick must match exactly one evaluated candidate",
                ),
            ):
                memory_store.cmd_record(args)

    def test_formal_pick_cannot_change_frozen_candidate_signal_or_confidence(
        self,
    ) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = five_state_pending_micro_ledger(
                base,
                start_index=434,
                family="total",
                line=2.5,
                reference_outcome="over",
                expected_state="full_win",
            )
            ledger_file = base / "prematch-forward-ledger.json"
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            args = active_record_args_for_five_state_ledger(
                base, ledger, ledger_file, history_base=base
            )
            args.total_market_signal = "neutral"
            base_binding = reseal_single_commitment_ledger(ledger)
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
                self.assertRaisesRegex(
                    ValueError,
                    "does not match its frozen candidate source/price/model",
                ),
            ):
                memory_store.cmd_record(args)

    def test_formal_eligible_selected_candidate_cannot_be_silently_suppressed(
        self,
    ) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = five_state_pending_micro_ledger(
                base,
                start_index=433,
                family="total",
                line=2.5,
                reference_outcome="over",
                expected_state="full_win",
            )
            ledger_file = base / "prematch-forward-ledger.json"
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            args = active_record_args_for_five_state_ledger(
                base, ledger, ledger_file, history_base=base
            )
            args.total_side = None
            args.primary_market = None
            args.display_exact_score_pick = []
            args.display_exact_score_event_probability = None
            base_binding = reseal_single_commitment_ledger(ledger)
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
                self.assertRaisesRegex(
                    ValueError,
                    "formal-eligible selected candidates and official formal picks must match exactly",
                ),
            ):
                memory_store.cmd_record(args)

    def test_active_record_rejects_resealed_model_probability_tamper(self) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = five_state_pending_micro_ledger(
                base,
                start_index=44,
                family="asian",
                line=-0.75,
                reference_outcome="home",
                expected_state="half_win",
            )
            ledger_file = base / "prematch-forward-ledger.json"
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            args = active_record_args_for_five_state_ledger(
                base, ledger, ledger_file, history_base=base
            )
            probabilities = ledger["commitments"][0]["prediction_payload"][
                "model_probabilities"
            ]
            probabilities["full_win"] -= 0.01
            probabilities["loss"] += 0.01
            base_binding = reseal_single_commitment_ledger(ledger)
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
                self.assertRaisesRegex(
                    ValueError, "model probabilities do not match the frozen candidate"
                ),
            ):
                memory_store.cmd_record(args)

    def test_candidate_cannot_bind_a_future_source_bundle_snapshot(self) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = five_state_pending_micro_ledger(
                base,
                start_index=45,
                family="total",
                line=2.5,
                reference_outcome="over",
                expected_state="full_win",
            )
            ledger_file = base / "prematch-forward-ledger.json"
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            args = active_record_args_for_five_state_ledger(
                base, ledger, ledger_file, history_base=base
            )
            original_evidence_file = Path(args.source_evidence_file)
            original_evidence = json.loads(
                original_evidence_file.read_text(encoding="utf-8")
            )
            raw_path = (
                original_evidence_file.parent
                / original_evidence["sources"][0]["raw_response_path"]
            )
            current_raw = json.loads(raw_path.read_text(encoding="utf-8"))
            future_raw = deepcopy(current_raw)
            future_raw["collected_at"] = (
                datetime.fromisoformat(current_raw["collected_at"])
                + timedelta(seconds=1)
            ).isoformat()
            current_file = base / "current-source.json"
            future_file = base / "future-source.json"
            current_file.write_text(json.dumps(current_raw), encoding="utf-8")
            future_file.write_text(json.dumps(future_raw), encoding="utf-8")
            evidence_file, evidence = source_evidence.build_evidence(
                [current_file, future_file], output_dir=base / "future-evidence"
            )
            args.source_evidence_file = str(evidence_file)
            prediction = ledger["commitments"][0]["prediction_payload"]
            prediction["bookmaker_snapshot"].update(
                {
                    "source_evidence_file": str(evidence_file),
                    "source_evidence_hash": evidence["evidence_hash"],
                }
            )
            prediction["execution_entry"]["entry_source_evidence_hash"] = evidence[
                "evidence_hash"
            ]
            base_binding = reseal_single_commitment_ledger(ledger)
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
                self.assertRaisesRegex(
                    ValueError, "source evidence collected after its generated_at"
                ),
            ):
                memory_store.cmd_record(args)

    def test_candidate_disposition_cannot_predate_the_frozen_queue(self) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = five_state_pending_micro_ledger(
                base,
                start_index=48,
                family="total",
                line=2.5,
                reference_outcome="over",
                expected_state="full_win",
            )
            ledger_file = base / "prematch-forward-ledger.json"
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            args = active_record_args_for_five_state_ledger(
                base, ledger, ledger_file, history_base=base
            )
            candidate_payload = json.loads(
                Path(args.candidate_evaluation_file).read_text(encoding="utf-8")
            )
            candidate_time = datetime.fromisoformat(candidate_payload["generated_at"])
            queue = deepcopy(ledger["queue_manifest"])
            queue.pop("queue_hash")
            queue["frozen_at"] = (candidate_time + timedelta(seconds=1)).isoformat()
            ledger["queue_manifest"] = seal(queue, "queue_hash")
            prediction = ledger["commitments"][0]["prediction_payload"]
            generated_at = candidate_time + timedelta(seconds=2)
            prediction["generated_at"] = generated_at.isoformat()
            prediction["lead_time_minutes"] = (
                datetime.fromisoformat(prediction["kickoff"]) - generated_at
            ).total_seconds() / 60.0
            prediction["queue_hash"] = ledger["queue_manifest"]["queue_hash"]
            base_binding = reseal_single_commitment_ledger(ledger)
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
                self.assertRaisesRegex(
                    ValueError, "cannot precede its frozen eligibility queue"
                ),
            ):
                memory_store.cmd_record(args)

    def test_all_unavailable_denominator_requires_honest_empty_source(self) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger, _ledger_file, args, base_binding = all_unavailable_active_inputs(
                base, start_index=46
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
            ):
                record = memory_store.cmd_record(args)["record"]
            prediction = record["forward_validation_ledger"]["ledger_payload"][
                "commitments"
            ][0]["prediction_payload"]
            candidate_audit = next(
                audit
                for audit in record["candidate_audits"]
                if audit.get("kind") == memory_store.CANDIDATE_EVALUATION_KIND
            )
            self.assertEqual(prediction["status"], "unavailable")
            self.assertIsNone(prediction["model_probabilities"])
            self.assertEqual(len(candidate_audit["market_manifest"]), 8)
            self.assertEqual(candidate_audit["candidates"], [])
            memory_store.validate_forward_record_prediction_commitment(record)
            self.assertEqual(
                ledger["queue_manifest"]["entries"][0]["market_identity"]["family"],
                "1x2",
            )

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _ledger, _ledger_file, args, base_binding = all_unavailable_active_inputs(
                base, start_index=47, unavailable_source=False
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
                self.assertRaisesRegex(
                    ValueError,
                    "replayable canonical model and decision-time price must be evaluated",
                ),
            ):
                memory_store.cmd_record(args)

    def test_identity_gates_prevent_cross_market_sample_subsidy(self) -> None:
        categorical_specs = (
            (
                ONE_X_TWO_IDENTITY,
                ["H", "D", "A"],
                "H",
                "1x2:full_time",
                4,
            ),
            (
                {
                    "family": "goal_range",
                    "period": "full_time",
                    "line": None,
                    "price_outcomes": ["0-1", "2-3", "4+"],
                },
                ["0-1", "2-3", "4+"],
                "0-1",
                "goal_range:full_time",
                1,
            ),
        )
        five_state_specs = (
            (
                {
                    "family": "total",
                    "period": "full_time",
                    "line": 2.25,
                    "price_outcomes": ["over", "under"],
                },
                "total:full_time",
                4,
            ),
            (
                {
                    "family": "asian",
                    "period": "full_time",
                    "line": -0.75,
                    "price_outcomes": ["home", "away"],
                },
                "asian:full_time",
                1,
            ),
        )
        rows: list[dict] = []
        week_index = 1
        categorical_hashes: list[str] = []
        for identity, states, actual, family_period, count in categorical_specs:
            identity_hash = source_evidence.market_identity_hash(identity)
            categorical_hashes.append(identity_hash)
            model = {state: 0.02 for state in states}
            model[actual] = 0.96
            baseline = {state: 0.3 for state in states}
            baseline[actual] = 0.4
            for sample_index in range(count):
                rows.append(
                    {
                        "observation_id": f"cat-{identity_hash[-6:]}-{sample_index}",
                        "status": "predicted",
                        "market_semantics": "categorical",
                        "market_identity": deepcopy(identity),
                        "market_identity_hash": identity_hash,
                        "market_family_period": family_period,
                        "settlement_states": list(states),
                        "bookmaker_proper_score_status": (
                            "available_same_outcome_space"
                        ),
                        "kickoff_week": f"2026-W{week_index:02d}",
                        "observed_settlement_state": actual,
                        "model_probabilities": deepcopy(model),
                        "baselines": {
                            name: deepcopy(baseline)
                            for name in forward_validation.BASELINE_NAMES
                        },
                        "execution": {
                            "entry_decimal_odds": 2.0,
                            "entry_no_vig_probability": 0.5,
                            "closing_no_vig_probability": 0.55,
                            "stake_units": 1.0,
                            "settlement_state": "full_win",
                        },
                    }
                )
                week_index += 1
        five_state_hashes: list[str] = []
        states = list(forward_validation.FIVE_STATE_SETTLEMENT_STATES)
        five_model = {state: 0.01 for state in states}
        five_model["full_win"] = 0.96
        five_baseline = {state: 0.2 for state in states}
        for identity, family_period, count in five_state_specs:
            identity_hash = source_evidence.market_identity_hash(identity)
            five_state_hashes.append(identity_hash)
            for sample_index in range(count):
                rows.append(
                    {
                        "observation_id": f"five-{identity_hash[-6:]}-{sample_index}",
                        "status": "predicted",
                        "market_semantics": "five_state_return",
                        "market_identity": deepcopy(identity),
                        "market_identity_hash": identity_hash,
                        "market_family_period": family_period,
                        "settlement_states": states,
                        "bookmaker_proper_score_status": (
                            "unavailable_price_and_settlement_spaces_differ"
                        ),
                        "kickoff_week": f"2026-W{week_index:02d}",
                        "observed_settlement_state": "full_win",
                        "model_probabilities": deepcopy(five_model),
                        "baselines": {
                            name: deepcopy(five_baseline)
                            for name in forward_validation.MODEL_SPACE_BASELINE_NAMES
                        },
                        "execution": {
                            "entry_decimal_odds": 2.0,
                            "entry_no_vig_probability": 0.5,
                            "closing_no_vig_probability": 0.55,
                            "stake_units": 1.0,
                            "settlement_state": "full_win",
                        },
                    }
                )
                week_index += 1

        block = forward_validation._segment_report(rows, repetitions=100, seed=7)
        statistical, integrity = forward_validation._assess_identity_proper_score_gates(
            block["proper_score_identity_gates"],
            minimum_samples=4,
            minimum_clusters=2,
            maximum_calibration_error=0.05,
        )
        gates = block["proper_score_identity_gates"]
        self.assertTrue(gates[categorical_hashes[0]]["statistical_gate_passed"])
        self.assertFalse(gates[categorical_hashes[1]]["statistical_gate_passed"])
        self.assertTrue(gates[five_state_hashes[0]]["statistical_gate_passed"])
        self.assertFalse(gates[five_state_hashes[1]]["statistical_gate_passed"])
        self.assertEqual(gates[categorical_hashes[1]]["eligible_graded_outputs"], 1)
        self.assertEqual(gates[five_state_hashes[1]]["eligible_graded_outputs"], 1)
        self.assertTrue(any("samples_are_insufficient" in item for item in statistical))
        self.assertEqual(integrity, [])

        strong_total_rows = [
            deepcopy(row)
            for row in rows
            if row["market_identity_hash"] == five_state_hashes[0]
        ]
        asian_template = next(
            row for row in rows if row["market_identity_hash"] == five_state_hashes[1]
        )
        bad_asian_rows: list[dict] = []
        bad_model = {state: 0.05 for state in states}
        bad_model["loss"] = 0.8
        for index in range(4):
            row = deepcopy(asian_template)
            row["observation_id"] = f"bad-asian-{index}"
            row["kickoff_week"] = f"2027-W{index + 1:02d}"
            row["observed_settlement_state"] = "loss"
            row["model_probabilities"] = deepcopy(bad_model)
            row["execution"].update(
                {
                    "closing_no_vig_probability": 0.45,
                    "settlement_state": "loss",
                }
            )
            bad_asian_rows.append(row)
        quality_block = forward_validation._segment_report(
            strong_total_rows + bad_asian_rows,
            repetitions=100,
            seed=17,
        )
        quality_statistical, quality_integrity = (
            forward_validation._assess_identity_proper_score_gates(
                quality_block["proper_score_identity_gates"],
                minimum_samples=4,
                minimum_clusters=2,
                maximum_calibration_error=0.05,
            )
        )
        quality_gates = quality_block["proper_score_identity_gates"]
        self.assertTrue(quality_gates[five_state_hashes[0]]["statistical_gate_passed"])
        self.assertFalse(quality_gates[five_state_hashes[1]]["statistical_gate_passed"])
        bad_blockers = quality_gates[five_state_hashes[1]]["statistical_blockers"]
        self.assertTrue(any("calibration" in item for item in bad_blockers))
        self.assertTrue(any("roi_ci_is_not_positive" in item for item in bad_blockers))
        self.assertTrue(any("clv_ci_is_not_positive" in item for item in bad_blockers))
        self.assertTrue(quality_statistical)
        self.assertEqual(quality_integrity, [])

    def test_split_line_markets_replay_both_reference_outcomes_end_to_end(self) -> None:
        from test_memory_store import memory_store

        specifications = [
            {
                "start_index": 8,
                "family": "total",
                "line": 2.25,
                "reference_outcome": "over",
                "expected_state": "half_loss",
                "home_score": 1,
                "away_score": 1,
            },
            {
                "start_index": 9,
                "family": "total",
                "line": 2.25,
                "reference_outcome": "under",
                "expected_state": "half_win",
                "home_score": 1,
                "away_score": 1,
            },
            {
                "start_index": 10,
                "family": "asian",
                "line": -0.75,
                "reference_outcome": "home",
                "expected_state": "half_win",
                "home_score": 1,
                "away_score": 0,
            },
            {
                "start_index": 11,
                "family": "asian",
                "line": -0.75,
                "reference_outcome": "away",
                "expected_state": "half_loss",
                "home_score": 1,
                "away_score": 0,
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            records = [
                record_and_review_five_state_fixture(base, **specification)
                for specification in specifications
            ]
            _cohort, closure = closed_cohort_for_memory_records(records)
            exported = memory_store.forward_validation_input_for_records(
                records, cohort_closure=closure
            )
            normalized = forward_validation.validate_input(exported)
            report = forward_validation.evaluate(exported)

        rows_by_reference = {
            (row["market_identity"]["family"], row["settlement_reference_outcome"]): row
            for row in normalized["records"]
        }
        expected = {
            ("total", "over"): "half_loss",
            ("total", "under"): "half_win",
            ("asian", "home"): "half_win",
            ("asian", "away"): "half_loss",
        }
        self.assertEqual(
            {
                key: row["unverified_observed_settlement_state"]
                for key, row in rows_by_reference.items()
            },
            expected,
        )
        self.assertTrue(
            all(
                row["observed_settlement_state"] is None
                for row in rows_by_reference.values()
            )
        )
        self.assertEqual(
            rows_by_reference[("asian", "home")]["market_identity"]["line"],
            -0.75,
        )
        self.assertEqual(
            rows_by_reference[("asian", "away")]["market_identity"]["line"],
            -0.75,
        )
        self.assertEqual(
            rows_by_reference[("asian", "away")]["execution_entry"]["selection"],
            "away",
        )
        self.assertAlmostEqual(
            rows_by_reference[("asian", "away")]["model_probabilities"]["half_loss"],
            rows_by_reference[("asian", "home")]["model_probabilities"]["half_win"],
        )
        self.assertAlmostEqual(
            rows_by_reference[("asian", "away")]["model_probabilities"]["half_win"],
            rows_by_reference[("asian", "home")]["model_probabilities"]["half_loss"],
        )
        five_state_gate = report["overall"]["proper_score_gates"][
            forward_validation.FIVE_STATE_PROPER_SCORE_SPACE
        ]
        categorical_gate = report["overall"]["proper_score_gates"][
            forward_validation.CATEGORICAL_PROPER_SCORE_SPACE
        ]
        self.assertEqual(five_state_gate["eligible_graded_outputs"], 0)
        self.assertEqual(
            five_state_gate["required_baselines"],
            list(forward_validation.MODEL_SPACE_BASELINE_NAMES),
        )
        self.assertFalse(five_state_gate["promotion_gate"])
        self.assertEqual(five_state_gate["role"], "pooled_descriptive_summary_only")
        identity_gates = report["overall"]["proper_score_identity_gates"]
        self.assertEqual(identity_gates, {})
        self.assertEqual(categorical_gate["eligible_graded_outputs"], 0)
        self.assertEqual(
            categorical_gate["status"], "unavailable_no_eligible_outcome_space"
        )
        self.assertFalse(
            any(
                blocker.startswith("bookmaker_")
                or blocker == "insufficient_paired_same_time_bookmaker_samples"
                for blocker in report["blocker_categories"]["statistical"]
            )
        )
        self.assertIn(
            "result_evidence_replay_unavailable", report["promotion_blockers"]
        )

    def test_two_real_memory_records_export_and_evaluate_as_one_cohort(self) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            records = [
                record_and_review_forward_fixture(base, index) for index in range(2)
            ]
            cohort, closure = closed_cohort_for_memory_records(records)
            exported = memory_store.forward_validation_input_for_records(
                records, cohort_closure=closure
            )
            normalized = forward_validation.validate_input(exported)
            report = forward_validation.evaluate(exported)
            cohort_path = forward_policy.cohort_manifest_path(base, cohort["cohort_id"])
            cohort_path.parent.mkdir(parents=True, exist_ok=True)
            cohort_path.write_text(
                json.dumps(cohort, ensure_ascii=False), encoding="utf-8"
            )
            closure_file = base / "cohort-closure.json"
            closure_file.write_text(
                json.dumps(closure, ensure_ascii=False), encoding="utf-8"
            )
            output = base / "cohort-forward-observations.json"
            cli_result = memory_store.cmd_export_forward_validation(
                SimpleNamespace(
                    base_dir=str(base),
                    output=str(output),
                    cohort_id=exported["cohort_id"],
                    cohort_closure_file=str(closure_file),
                )
            )
            cli_exported = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            exported["history_ledger_binding"]["fixture_ids"], ["1000", "1001"]
        )
        self.assertEqual(
            [
                entry["fixture_id"]
                for entry in exported["cohort_closure"]["record_manifest"]["records"]
            ],
            ["1000", "1001"],
        )
        self.assertEqual(len(normalized["records"]), 2)
        self.assertEqual(cli_result["receipt_count"], 2)
        self.assertEqual(cli_exported, exported)
        self.assertEqual(
            report["overall"]["comparisons"]["historical_frequency"]["sample_count"],
            0,
        )
        self.assertEqual(
            report["overall"]["comparisons"]["bookmaker_no_vig"]["sample_count"],
            0,
        )
        self.assertEqual(len(report["overall"]["quarantined_observation_ids"]), 2)
        self.assertIn(
            "result_evidence_replay_unavailable", report["promotion_blockers"]
        )

    def test_atomic_memory_store_close_seals_all_history_records(self) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            records = [
                record_and_review_forward_fixture(base, index) for index in range(2)
            ]
            cohort = deepcopy(
                records[0]["forward_validation_ledger"]["ledger_payload"][
                    "cohort_manifest"
                ]
            )
            immutable_path = forward_policy.cohort_manifest_path(
                base, cohort["cohort_id"]
            )
            immutable_path.parent.mkdir(parents=True, exist_ok=True)
            immutable_path.write_text(
                json.dumps(cohort, ensure_ascii=False), encoding="utf-8"
            )
            active_path = forward_policy.active_cohort_path(base)
            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.write_text(
                json.dumps(cohort, ensure_ascii=False), encoding="utf-8"
            )
            manifest_output = base / "record-manifest.json"
            closed = memory_store.cmd_close_forward_cohort(
                SimpleNamespace(
                    base_dir=str(base),
                    cohort_id=cohort["cohort_id"],
                    closed_at="2027-01-01T00:00:00+00:00",
                    record_manifest_output=str(manifest_output),
                )
            )
            closure = json.loads(
                Path(closed["closure_path"]).read_text(encoding="utf-8")
            )
            exported = memory_store.forward_validation_input_for_records(
                records, cohort_closure=closure
            )
            normalized = forward_validation.validate_input(exported)
            manifest_exists = manifest_output.is_file()
        self.assertEqual(closed["record_count"], 2)
        self.assertTrue(manifest_exists)
        self.assertEqual(closure["record_manifest_hash"], closed["manifest_hash"])
        self.assertEqual(len(normalized["records"]), 2)

    def test_memory_record_review_evaluate_replays_archived_micro_ledger(self) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = five_state_pending_micro_ledger(
                base,
                start_index=0,
                family="total",
                line=2.5,
                reference_outcome="over",
                expected_state="full_win",
            )
            ledger_file = base / "prematch-forward-ledger.json"
            ledger_file.write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            prediction = ledger["commitments"][0]["prediction_payload"]
            committed = ledger["commitments"][0]["forward_policy_binding"]
            base_binding = deepcopy(committed)
            base_binding.pop("binding_hash")
            base_binding.pop("observation_commitment_hash")
            base_binding["schema_version"] = (
                forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION
            )
            base_binding["binding_hash"] = forward_policy._hash_json(base_binding)
            self.assertIsNone(
                memory_store.forward_policy_binding_for_record(
                    {"forward_policy_binding": deepcopy(base_binding)}
                )
            )
            archived_at = datetime.fromisoformat(base_binding["archived_at"])
            args = active_record_args_for_five_state_ledger(
                base, ledger, ledger_file, history_base=base
            )
            missing_ledger_args = deepcopy(args)
            missing_ledger_args.forward_validation_ledger = None
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
            ):
                with self.assertRaisesRegex(
                    ValueError, "require --forward-validation-ledger"
                ):
                    memory_store.cmd_record(missing_ledger_args)
            with (
                mock.patch.object(
                    memory_store.forward_policy,
                    "load_active_binding",
                    return_value=deepcopy(base_binding),
                ),
                mock.patch.object(memory_store, "utc_now", return_value=archived_at),
            ):
                recorded = memory_store.cmd_record(args)
            record = recorded["record"]
            self.assertEqual(record["primary_market"], "total")
            self.assertEqual(record["total_pick"]["side"], "over")
            self.assertEqual(record["primary_pick"]["confidence_rank"], 1)
            self.assertEqual(
                record["forward_policy_binding"]["schema_version"],
                forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
            )
            memory_store.validate_forward_record_prediction_commitment(record)

            edited = deepcopy(record)
            edited["probabilities"]["home_win"] = 0.49
            with self.assertRaisesRegex(
                ValueError, "probabilities do not match|no longer matches"
            ):
                memory_store.validate_forward_record_prediction_commitment(edited)

            result_collected = datetime.fromisoformat(
                prediction["kickoff"]
            ) + timedelta(hours=2)
            review_args = SimpleNamespace(
                base_dir=str(base),
                verified_finished=True,
                match_id=prediction["fixture_id"],
                home_score=1,
                away_score=0,
                half_home_score=0,
                half_away_score=0,
                home_corners=None,
                away_corners=None,
                key_learning="content-addressed forward replay",
                verification_source="https://example.test/final/1000",
                verification_collected_at=result_collected.isoformat(),
            )
            with mock.patch.object(
                memory_store,
                "utc_now",
                return_value=result_collected + timedelta(minutes=1),
            ):
                reviewed = memory_store.cmd_review(review_args)["record"]
            _cohort, closure = closed_cohort_for_memory_records([reviewed])
            replay = memory_store.forward_validation_input_for_record(
                reviewed, cohort_closure=closure
            )
            report = forward_validation.evaluate(replay)
            self.assertEqual(
                report["overall"]["comparisons"]["historical_frequency"][
                    "sample_count"
                ],
                0,
            )
            self.assertEqual(
                report["overall"]["comparisons"]["bookmaker_no_vig"]["sample_count"],
                0,
            )
            self.assertEqual(
                report["overall"]["quarantined_observation_ids"],
                [prediction["observation_id"]],
            )
            self.assertIn(
                "result_evidence_replay_unavailable", report["promotion_blockers"]
            )
            self.assertEqual(
                report["history_ledger_binding"]["receipts"][0]["archive_version_hash"],
                record["archive_version_hash"],
            )

    def test_post_kickoff_fully_rehashed_commitment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = pending_micro_ledger(Path(temporary))
        prediction = value["commitments"][0]["prediction_payload"]
        kickoff = datetime.fromisoformat(prediction["kickoff"])
        prediction["generated_at"] = (kickoff + timedelta(minutes=1)).isoformat()
        prediction["lead_time_minutes"] = -1
        binding = forward_policy.bind_observation_commitment(
            base_policy_binding(
                value["policy_manifest"],
                value["cohort_manifest"],
                (kickoff + timedelta(minutes=2)).isoformat(),
            ),
            forward_validation._hash(prediction),
        )
        value["commitments"][0] = seal(
            {
                "schema_version": forward_validation.COMMITMENT_SCHEMA_VERSION,
                "prediction_payload": prediction,
                "forward_policy_binding": binding,
            },
            "commitment_hash",
        )
        settlement = value["settlements"][0]
        settlement["commitment_hash"] = value["commitments"][0]["commitment_hash"]
        settlement.pop("settlement_hash")
        value["settlements"][0] = seal(settlement, "settlement_hash")
        with self.assertRaisesRegex(
            forward_validation.ForwardValidationError,
            "archived_at < kickoff|timestamps|lead_time",
        ):
            forward_validation.validate_prematch_input(value)

    def test_unreplayable_results_and_closing_prices_are_quarantined_from_scores(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = forward_validation.evaluate(
                build_aggregate_payload(Path(temporary), 6)
            )
        bookmaker = report["overall"]["comparisons"]["bookmaker_no_vig"]
        self.assertEqual(bookmaker["sample_count"], 0)
        self.assertEqual(bookmaker["cluster_count"], 0)
        self.assertEqual(len(report["overall"]["quarantined_observation_ids"]), 6)
        self.assertIn(
            "result_evidence_replay_unavailable", report["promotion_blockers"]
        )
        self.assertIn(
            "closing_evidence_replay_unavailable", report["promotion_blockers"]
        )
        self.assertIn(
            "result_source_replay_not_demonstrated", report["promotion_blockers"]
        )
        self.assertIn(
            "closing_price_source_replay_not_demonstrated",
            report["promotion_blockers"],
        )
        self.assertFalse(report["statistical_gate_passed"])
        self.assertIn(
            "external_timestamp_anchor_not_configured", report["promotion_blockers"]
        )
        self.assertFalse(report["promotion_eligible"])

    def test_formal_v3_rejects_an_open_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_aggregate_payload(Path(temporary), 1)
        value["cohort_closure"] = None
        with self.assertRaisesRegex(
            forward_validation.ForwardValidationError, "requires a closed cohort"
        ):
            forward_validation.evaluate(value)

    def test_v3_evaluation_rejects_freely_constructed_unarchived_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_payload(Path(temporary), 2)
            with self.assertRaisesRegex(
                forward_validation.ForwardValidationError,
                "memory-store cohort aggregate export|forward cohort aggregate",
            ):
                forward_validation.validate_input(value)

    def test_arbitrary_sha_history_wrapper_cannot_enter_formal_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_payload(Path(temporary), 1)
        value["history_ledger_binding"] = {
            "schema_version": "memory-forward-history-ledger-binding/1.0.0",
            "fixture_ids": ["1000"],
            "archive_version_hash": "sha256:" + "a" * 64,
            "record_commitment_hash": "sha256:" + "b" * 64,
            "prematch_ledger_hash": forward_validation._hash(
                forward_validation._prematch_ledger_view(value)
            ),
            "market_commitments": forward_validation._market_commitment_identities(
                value
            ),
        }
        value["history_ledger_binding"]["binding_hash"] = forward_validation._hash(
            value["history_ledger_binding"]
        )
        self.assertFalse(hasattr(forward_validation, "build_history_ledger_binding"))
        with self.assertRaisesRegex(
            forward_validation.ForwardValidationError,
            "memory-store cohort aggregate export|forward cohort aggregate",
        ):
            forward_validation.validate_input(value)

    def test_equal_but_invented_final_merge_commit_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_aggregate_payload(Path(temporary), 1, git_commit="a" * 40)
        with self.assertRaisesRegex(
            forward_validation.ForwardValidationError,
            "Git commit does not exist",
        ):
            forward_validation.validate_input(value)

    def test_resealed_receipt_cannot_forge_record_archived_at(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_aggregate_payload(Path(temporary), 1)
        receipt = value["history_ledger_binding"]["receipts"][0]
        receipt["record_archived_at"] = (
            datetime.fromisoformat(receipt["record_archived_at"]) - timedelta(minutes=5)
        ).isoformat()
        receipt.pop("receipt_hash")
        receipt["receipt_hash"] = forward_validation._hash(receipt)
        value["history_ledger_binding"].pop("binding_hash")
        value["history_ledger_binding"]["binding_hash"] = forward_validation._hash(
            value["history_ledger_binding"]
        )
        with self.assertRaisesRegex(
            forward_validation.ForwardValidationError,
            "archived_at does not match",
        ):
            forward_validation.validate_input(value)

    def test_history_receipt_delete_reorder_copy_and_replace_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_aggregate_payload(Path(temporary), 2)
            deleted = deepcopy(value)
            deleted.pop("history_ledger_binding")
            with self.assertRaisesRegex(
                forward_validation.ForwardValidationError,
                "forward cohort aggregate fields",
            ):
                forward_validation.validate_input(deleted)

            attacks = {}
            truncated = deepcopy(value)
            truncated["history_ledger_binding"]["receipts"] = truncated[
                "history_ledger_binding"
            ]["receipts"][:-1]
            attacks["delete"] = truncated
            reordered = deepcopy(value)
            reordered["history_ledger_binding"]["receipts"].reverse()
            attacks["reorder"] = reordered
            copied = deepcopy(value)
            copied["history_ledger_binding"]["receipts"].append(
                deepcopy(copied["history_ledger_binding"]["receipts"][0])
            )
            attacks["copy"] = copied
            replaced = deepcopy(value)
            replaced_receipt = replaced["history_ledger_binding"]["receipts"][0]
            replaced_receipt["archive_version_hash"] = "sha256:" + "9" * 64
            replaced_receipt.pop("receipt_hash")
            replaced_receipt["receipt_hash"] = forward_validation._hash(
                replaced_receipt
            )
            attacks["replace"] = replaced
            for name, attacked in attacks.items():
                with self.subTest(name=name):
                    binding = attacked["history_ledger_binding"]
                    binding.pop("binding_hash")
                    binding["binding_hash"] = forward_validation._hash(binding)
                    with self.assertRaisesRegex(
                        forward_validation.ForwardValidationError,
                        "receipt|canonically ordered|archive snapshot",
                    ):
                        forward_validation.validate_input(attacked)

    def test_resealed_receipt_subset_cannot_shrink_closed_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_aggregate_payload(Path(temporary), 2)
        binding = value["history_ledger_binding"]
        binding["receipts"] = binding["receipts"][:1]
        binding["fixture_ids"] = [binding["receipts"][0]["fixture_id"]]
        binding.pop("binding_hash")
        binding["binding_hash"] = forward_validation._hash(binding)
        with self.assertRaisesRegex(
            forward_validation.ForwardValidationError,
            "exactly cover the closed cohort record manifest",
        ):
            forward_validation.evaluate(value)

    def test_formal_export_rejects_records_added_after_manifest_snapshot(self) -> None:
        from test_memory_store import memory_store

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            records = [
                record_and_review_forward_fixture(base, index) for index in range(2)
            ]
            ledger = records[0]["forward_validation_ledger"]["ledger_payload"]
            cohort = deepcopy(ledger["cohort_manifest"])
            stale_manifest = memory_store.forward_record_manifest_for_records(
                records[:1], cohort_manifest=cohort
            )
            stale_closure = cohort_closure(cohort, stale_manifest)
            with self.assertRaisesRegex(
                ValueError, "do not exactly cover the closed record manifest"
            ):
                memory_store.forward_validation_input_for_records(
                    records, cohort_closure=stale_closure
                )

    def test_formal_export_parser_has_no_fixture_subset_escape(self) -> None:
        from test_memory_store import memory_store

        with self.assertRaises(SystemExit):
            memory_store.build_parser().parse_args(
                [
                    "export-forward-validation",
                    "--cohort-id",
                    "confirmation-a",
                    "--cohort-closure-file",
                    "closure.json",
                    "--output",
                    "observations.json",
                    "--match-id",
                    "1000",
                ]
            )

    def test_same_cohort_id_replacement_and_late_queue_addition_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_payload(Path(temporary), 2)
            replaced = deepcopy(value)
            cohort = replaced["cohort_manifest"]
            cohort["starts_at"] = (
                datetime.fromisoformat(cohort["starts_at"]) + timedelta(seconds=1)
            ).isoformat()
            cohort.pop("cohort_hash")
            cohort["cohort_hash"] = forward_validation._hash(cohort)
            with self.assertRaisesRegex(
                forward_validation.ForwardValidationError,
                "cohort_closure is invalid|frozen cohort|does not commit this prediction payload",
            ):
                forward_validation._validate_v3_input(
                    replaced, require_history_ledger_binding=False
                )

            late = deepcopy(value)
            queue = late["queue_manifest"]
            added = deepcopy(queue["entries"][0])
            added["fixture_id"] = "late-added-fixture"
            added["queue_key"] = forward_validation._queue_key(
                late["cohort_id"],
                added["fixture_id"],
                added["market_identity_hash"],
            )
            queue["entries"].append(added)
            queue.pop("queue_hash")
            queue["queue_hash"] = forward_validation._hash(queue)
            with self.assertRaisesRegex(
                forward_validation.ForwardValidationError,
                "frozen eligibility queue|exactly cover",
            ):
                forward_validation._validate_v3_input(
                    late, require_history_ledger_binding=False
                )

    def test_fully_rehashed_cohort_kind_must_still_match_frozen_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_payload(Path(temporary), 1)
        value["cohort_closure"] = None
        cohort = value["cohort_manifest"]
        cohort["kind"] = forward_policy.PROMOTABLE_CONFIRMATION_KIND
        cohort.pop("cohort_hash")
        cohort["cohort_hash"] = forward_policy._hash_json(cohort)
        with self.assertRaisesRegex(
            forward_validation.ForwardValidationError,
            "matching explicit policy/cohort kinds",
        ):
            forward_validation._validate_v3_input(
                value, require_history_ledger_binding=False
            )

    def test_prediction_payload_tamper_cannot_be_hidden_in_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_payload(Path(temporary), 2)
            self.assertNotIn(
                "observed_outcome", value["commitments"][0]["prediction_payload"]
            )
            value["commitments"][0]["prediction_payload"]["model_probabilities"] = {
                "H": 0.98,
                "D": 0.01,
                "A": 0.01,
            }
            with self.assertRaisesRegex(
                forward_validation.ForwardValidationError, "commitment_hash"
            ):
                forward_validation._validate_v3_input(
                    value, require_history_ledger_binding=False
                )

    def test_bookmaker_no_vig_is_recomputed_from_replayed_complete_odds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_payload(Path(temporary), 2)
            prediction = value["commitments"][0]["prediction_payload"]
            prediction["baselines"]["bookmaker_no_vig"] = {
                "H": 0.98,
                "D": 0.01,
                "A": 0.01,
            }
            binding = forward_policy.bind_observation_commitment(
                base_policy_binding(
                    value["policy_manifest"],
                    value["cohort_manifest"],
                    prediction["generated_at"],
                ),
                forward_validation._hash(prediction),
            )
            value["commitments"][0] = seal(
                {
                    "schema_version": forward_validation.COMMITMENT_SCHEMA_VERSION,
                    "prediction_payload": prediction,
                    "forward_policy_binding": binding,
                },
                "commitment_hash",
            )
            value["settlements"][0]["commitment_hash"] = value["commitments"][0][
                "commitment_hash"
            ]
            value["settlements"][0].pop("settlement_hash")
            value["settlements"][0] = seal(value["settlements"][0], "settlement_hash")
            with self.assertRaisesRegex(
                forward_validation.ForwardValidationError, "does not recompute"
            ):
                forward_validation._validate_v3_input(
                    value, require_history_ledger_binding=False
                )

    def test_categorical_proper_score_comparability_is_key_based_not_order_based(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_payload(Path(temporary), 1)
            value["market_schemas"]["1x2"]["settlement_states"] = ["A", "D", "H"]
            normalized = forward_validation._validate_v3_input(
                value, require_history_ledger_binding=False
            )
        row = normalized["records"][0]
        self.assertEqual(
            row["bookmaker_proper_score_status"], "available_same_outcome_space"
        )
        self.assertIn("bookmaker_no_vig", row["baselines"])

        with tempfile.TemporaryDirectory() as temporary:
            mismatched = build_payload(Path(temporary), 1)
            mismatched["market_schemas"]["1x2"]["settlement_states"] = [
                "H",
                "D",
                "X",
            ]
            with self.assertRaisesRegex(
                forward_validation.ForwardValidationError,
                "categorical settlement states must exactly match",
            ):
                forward_validation._validate_v3_input(
                    mismatched, require_history_ledger_binding=False
                )

    def test_snapshot_after_archive_and_duplicate_fixture_market_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_payload(Path(temporary), 2)
            prediction = value["commitments"][0]["prediction_payload"]
            prediction["bookmaker_snapshot"]["collected_at"] = (
                datetime.fromisoformat(prediction["generated_at"])
                + timedelta(minutes=4)
            ).isoformat()
            prediction["baseline_lineage"]["bookmaker_no_vig"]["generated_at"] = (
                prediction["bookmaker_snapshot"]["collected_at"]
            )
            binding = forward_policy.bind_observation_commitment(
                base_policy_binding(
                    value["policy_manifest"],
                    value["cohort_manifest"],
                    (
                        datetime.fromisoformat(prediction["generated_at"])
                        + timedelta(minutes=1)
                    ).isoformat(),
                ),
                forward_validation._hash(prediction),
            )
            value["commitments"][0] = seal(
                {
                    "schema_version": forward_validation.COMMITMENT_SCHEMA_VERSION,
                    "prediction_payload": prediction,
                    "forward_policy_binding": binding,
                },
                "commitment_hash",
            )
            with self.assertRaisesRegex(
                forward_validation.ForwardValidationError,
                "temporal causality|archive time",
            ):
                forward_validation._validate_v3_input(
                    value, require_history_ledger_binding=False
                )

        with tempfile.TemporaryDirectory() as temporary:
            value = build_payload(Path(temporary), 2)
            duplicate = deepcopy(value["queue_manifest"]["entries"][0])
            value["queue_manifest"]["entries"].append(duplicate)
            value["queue_manifest"].pop("queue_hash")
            value["queue_manifest"] = seal(value["queue_manifest"], "queue_hash")
            with self.assertRaisesRegex(
                forward_validation.ForwardValidationError,
                r"duplicated fixture\+market identity",
            ):
                forward_validation._validate_v3_input(
                    value, require_history_ledger_binding=False
                )

    def test_missing_outcome_and_one_cluster_are_explicit_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_aggregate_payload(Path(temporary), 6, one_week=True)
            receipt = value["history_ledger_binding"]["receipts"][0]
            settlement = receipt["ledger_payload"]["settlements"][0]
            settlement.update(
                {
                    "status": "pending",
                    "observed_settlement_state": None,
                    "result_collected_at": None,
                    "result_source_evidence_hash": None,
                }
            )
            settlement.pop("settlement_hash")
            receipt["ledger_payload"]["settlements"][0] = seal(
                settlement, "settlement_hash"
            )
            receipt["ledger_payload_hash"] = forward_validation._hash(
                receipt["ledger_payload"]
            )
            receipt.pop("receipt_hash")
            receipt["receipt_hash"] = forward_validation._hash(receipt)
            value["history_ledger_binding"].pop("binding_hash")
            value["history_ledger_binding"]["binding_hash"] = forward_validation._hash(
                value["history_ledger_binding"]
            )
            report = forward_validation.evaluate(value)
        self.assertIn("incomplete_settlement_states", report["promotion_blockers"])
        self.assertIn(
            "result_evidence_replay_unavailable", report["promotion_blockers"]
        )
        self.assertIn(
            settlement["observation_id"],
            report["overall"]["missing_settlement_state_ids"],
        )

    def test_protocol_override_is_experimental_and_report_is_deterministic(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_aggregate_payload(Path(temporary), 4)
            first = forward_validation.evaluate(deepcopy(value))
            second = forward_validation.evaluate(deepcopy(value))
            override = forward_validation.evaluate(
                deepcopy(value), bootstrap_seed=12345
            )
        self.assertEqual(first, second)
        self.assertIn(
            "validation_protocol_override_is_experimental",
            override["promotion_blockers"],
        )

    def test_execution_uses_five_state_returns_and_complete_market_no_vig_clv(
        self,
    ) -> None:
        block = forward_validation._execution(
            [
                {
                    "kickoff_week": "2026-W40",
                    "observed_outcome": "half_win",
                    "execution": {
                        "entry_decimal_odds": 2.0,
                        "entry_no_vig_probability": 0.50,
                        "closing_no_vig_probability": 0.55,
                        "stake_units": 1.0,
                        "settlement_state": "half_win",
                    },
                },
                {
                    "kickoff_week": "2026-W41",
                    "observed_outcome": "push",
                    "execution": {
                        "entry_decimal_odds": 2.0,
                        "entry_no_vig_probability": 0.50,
                        "closing_no_vig_probability": 0.52,
                        "stake_units": 1.0,
                        "settlement_state": "push",
                    },
                },
                {
                    "kickoff_week": "2026-W42",
                    "observed_outcome": "half_loss",
                    "execution": {
                        "entry_decimal_odds": 2.0,
                        "entry_no_vig_probability": 0.50,
                        "closing_no_vig_probability": 0.51,
                        "stake_units": 1.0,
                        "settlement_state": "half_loss",
                    },
                },
            ],
            repetitions=100,
            seed=7,
        )
        self.assertAlmostEqual(block["flat_stake_roi"], 0.0)
        self.assertAlmostEqual(block["mean_clv_probability_points"], 8.0 / 3.0)

    def test_legacy_schema_is_read_only_and_cannot_receive_a_green_gate(self) -> None:
        policy = policy_manifest()
        cohort = cohort_manifest(policy, status="active")
        generated = "2026-09-01T09:30:00Z"
        legacy = {
            "schema_version": forward_validation.LEGACY_INPUT_SCHEMA_VERSION,
            "cohort_id": cohort["cohort_id"],
            "policy_id": policy["policy_id"],
            "policy_hash": policy["policy_hash"],
            "policy_manifest": policy,
            "cohort_manifest": cohort,
            "outcomes": OUTCOMES,
            "records": [
                {
                    "fixture_id": "legacy-1",
                    "observation_id": "legacy-1:1x2",
                    "league": "league-a",
                    "market": "1x2",
                    "kickoff": "2026-09-01T10:00:00Z",
                    "generated_at": generated,
                    "lead_time_minutes": 30,
                    "status": "abstained",
                    "observed_outcome": "H",
                    "model_probabilities": {"H": 0.8, "D": 0.1, "A": 0.1},
                    "baselines": {},
                    "forward_policy_binding": base_policy_binding(
                        policy, cohort, "2026-09-01T09:31:00Z"
                    ),
                }
            ],
        }
        report = forward_validation.evaluate(legacy, bootstrap_repetitions=100)
        self.assertFalse(report["statistical_gate_passed"])
        self.assertIn(
            "legacy_uncommitted_observations_are_read_only",
            report["promotion_blockers"],
        )


if __name__ == "__main__":
    unittest.main()

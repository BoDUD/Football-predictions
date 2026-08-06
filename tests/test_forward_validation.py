from __future__ import annotations

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
        "confirmation_contract": {
            "retrospective_records_allowed": False,
            "parameter_or_threshold_changes_allowed": False,
            "prediction_affecting_bugfix_starts_new_cohort": True,
            "clean_head_required_at_freeze_and_cohort_start": True,
            "explicit_final_merge_commit_required": True,
            "all_candidates_abstentions_and_unavailable_markets_required": True,
            "executable_timestamped_prices_required_for_market_comparison": True,
            "promotion_is_manual": True,
            "promotion_requirements": ["proper_scores"],
        },
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
        "status": status,
        "starts_at": "2026-08-01T00:01:00+00:00",
        "policy_file": "policy.json",
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
        "untouched_confirmation_eligible": True,
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
                "market": "1x2",
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
        "1x2": {"outcomes": OUTCOMES, "settlement_semantics": "categorical"}
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
        market = "1x2"
        queue_key = forward_validation._queue_key(
            cohort["cohort_id"], fixture_id, market
        )
        entries.append(
            {
                "fixture_id": fixture_id,
                "home_team": home_team,
                "away_team": away_team,
                "league": "league-a" if index % 2 else "league-b",
                "market": market,
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
            "market": "1x2",
            "kickoff": raw["kickoff"].isoformat(),
            "generated_at": generated_text,
            "lead_time_minutes": 30,
            "status": "predicted",
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
                "entry_decimal_odds": 2.2,
                "entry_complete_market_odds": {
                    item: 2.2 if item == actual else 4.0 for item in OUTCOMES
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
                    "observed_outcome": actual,
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
        "schema_version": "memory-forward-ledger-archive/1.0.0",
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
        "schema_version": "memory-forward-prediction/1.0.0",
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
        "schema_version": "memory-forward-commitment/1.0.0",
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
            "observed_outcome": None,
            "result_collected_at": None,
            "result_source_evidence_hash": None,
            "closing_snapshot": None,
        }
    )
    settlement.pop("settlement_hash")
    payload["settlements"][0] = seal(settlement, "settlement_hash")
    forward_validation.validate_prematch_input(payload)
    return payload


def record_and_review_forward_fixture(base: Path, index: int) -> dict:
    from test_memory_store import memory_store, record_args

    fixture_base = base / f"real-fixture-{index}"
    fixture_base.mkdir(parents=True, exist_ok=True)
    ledger = pending_micro_ledger(fixture_base, start_index=index)
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
    args = record_args(
        str(base),
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
        source_url=prediction["bookmaker_snapshot"]["source_url"],
        source_evidence_file=prediction["bookmaker_snapshot"]["source_evidence_file"],
        forward_validation_ledger=str(ledger_file),
        home_win_probability=0.5,
        draw_probability=0.25,
        away_win_probability=0.25,
        primary_market=None,
        total_side=None,
        asian_side=None,
    )
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
            report["overall"]["comparisons"]["bookmaker_no_vig"]["sample_count"],
            2,
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
        from test_memory_store import memory_store, record_args

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            ledger = pending_micro_ledger(base)
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
            evidence_file = prediction["bookmaker_snapshot"]["source_evidence_file"]
            args = record_args(
                str(base),
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
                source_url=prediction["bookmaker_snapshot"]["source_url"],
                source_evidence_file=evidence_file,
                forward_validation_ledger=str(ledger_file),
                home_win_probability=0.5,
                draw_probability=0.25,
                away_win_probability=0.25,
                primary_market=None,
                total_side=None,
                asian_side=None,
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
                report["overall"]["comparisons"]["bookmaker_no_vig"]["sample_count"],
                1,
            )
            self.assertEqual(report["overall"]["missing_outcome_ids"], [])
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

    def test_v2_scores_committed_rows_but_never_claims_external_time_anchor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = forward_validation.evaluate(
                build_aggregate_payload(Path(temporary), 6)
            )
        bookmaker = report["overall"]["comparisons"]["bookmaker_no_vig"]
        self.assertEqual(bookmaker["sample_count"], 6)
        self.assertGreaterEqual(bookmaker["cluster_count"], 2)
        self.assertEqual(report["overall"]["missing_outcome_ids"], [])
        self.assertFalse(report["statistical_gate_passed"])
        self.assertIn(
            "external_timestamp_anchor_not_configured", report["promotion_blockers"]
        )
        self.assertFalse(report["promotion_eligible"])

    def test_formal_v2_rejects_an_open_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            value = build_aggregate_payload(Path(temporary), 1)
        value["cohort_closure"] = None
        with self.assertRaisesRegex(
            forward_validation.ForwardValidationError, "requires a closed cohort"
        ):
            forward_validation.evaluate(value)

    def test_v2_evaluation_rejects_freely_constructed_unarchived_input(self) -> None:
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
                forward_validation._validate_v2_input(
                    replaced, require_history_ledger_binding=False
                )

            late = deepcopy(value)
            queue = late["queue_manifest"]
            added = deepcopy(queue["entries"][0])
            added["fixture_id"] = "late-added-fixture"
            added["queue_key"] = forward_validation._queue_key(
                late["cohort_id"], added["fixture_id"], added["market"]
            )
            queue["entries"].append(added)
            queue.pop("queue_hash")
            queue["queue_hash"] = forward_validation._hash(queue)
            with self.assertRaisesRegex(
                forward_validation.ForwardValidationError,
                "frozen eligibility queue|exactly cover",
            ):
                forward_validation._validate_v2_input(
                    late, require_history_ledger_binding=False
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
                forward_validation._validate_v2_input(
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
                forward_validation._validate_v2_input(
                    value, require_history_ledger_binding=False
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
                forward_validation._validate_v2_input(
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
                r"duplicated fixture\+market",
            ):
                forward_validation._validate_v2_input(
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
                    "observed_outcome": None,
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
        self.assertIn("incomplete_settlement_outcomes", report["promotion_blockers"])
        self.assertIn(
            "insufficient_independent_iso_week_clusters",
            report["promotion_blockers"],
        )
        self.assertEqual(
            report["overall"]["missing_outcome_ids"],
            [settlement["observation_id"]],
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

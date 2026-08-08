from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import forward_policy, forward_validation, memory_store


def empty_record_manifest(cohort: dict) -> dict:
    value = {
        "schema_version": forward_policy.RECORD_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "soccer_untouched_live_forward_record_manifest",
        "cohort_id": cohort["cohort_id"],
        "cohort_hash": cohort["cohort_hash"],
        "policy_id": cohort["policy_id"],
        "policy_hash": cohort["policy_hash"],
        "record_count": 0,
        "records": [],
    }
    if "scope_hash" in cohort:
        denominator = {
            "schema_version": forward_policy.cohort_scope.DENOMINATOR_SCHEMA_VERSION,
            "artifact_type": "soccer_live_forward_cohort_denominator",
            "cohort_id": cohort["cohort_id"],
            "scope_id": cohort["scope_id"],
            "scope_hash": cohort["scope_hash"],
            "event_count": 0,
            "last_event_hash": None,
            "requested_fixture_count": 0,
            "recorded_fixture_count": 0,
            "unavailable_fixture_count": 0,
            "entries": [],
            "complete": True,
        }
        denominator["denominator_hash"] = forward_policy._hash_json(denominator)
        value["denominator"] = denominator
        value["denominator_hash"] = denominator["denominator_hash"]
    value["manifest_hash"] = forward_policy._hash_json(value)
    return value


class ForwardPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]

    def test_documented_cli_uses_importable_module_entrypoint(self) -> None:
        for relative in ("README.md", "references/model-validation.md"):
            with self.subTest(relative=relative):
                text = (self.repo_root / relative).read_text(encoding="utf-8")
                self.assertIn("python -m scripts.forward_policy", text)
                self.assertNotIn("python scripts/forward_policy.py", text)

    def _artifacts(self, base: Path) -> tuple[Path, Path, Path, Path, Path]:
        dataset = base / "manifest.json"
        dataset.write_text(
            json.dumps(
                {
                    "schema_version": "test/1",
                    "as_of_date": "2026-08-06",
                    "bundle_hash": "sha256:" + "1" * 64,
                }
            ),
            encoding="utf-8",
        )
        registry = base / "registry.json"
        registry.write_text(
            json.dumps(
                {
                    "schema_version": "test/1",
                    "registry_hash": "sha256:" + "2" * 64,
                    "dataset_manifest_hash": "sha256:" + "1" * 64,
                    "validated_training_config": {"half_life_days": 365},
                    "leagues": [
                        {
                            "league_key": "test_league",
                            "model_hash": "sha256:" + "6" * 64,
                            "full_time_component_model_hash": "sha256:" + "7" * 64,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        corner_dataset = base / "corner-manifest.json"
        corner_dataset.write_text(
            json.dumps(
                {
                    "schema_version": "test/1",
                    "as_of_date": "2026-08-06",
                    "bundle_hash": "sha256:" + "3" * 64,
                    "leagues": [
                        {
                            "league_key": "test_league",
                            "dataset_sha256": "sha256:" + "4" * 64,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        corner_registry = base / "corner-registry.json"
        corner_registry.write_text(
            json.dumps(
                {
                    "schema_version": "test/1",
                    "registry_hash": "sha256:" + "5" * 64,
                    "dataset_hashes": {"test_league": "sha256:" + "4" * 64},
                    "leagues": [
                        {
                            "league_key": "test_league",
                            "model_hash": "sha256:" + "8" * 64,
                            "dataset_hash": "sha256:" + "4" * 64,
                            "source_lineage": {
                                "manifest_bundle_hash": "sha256:" + "3" * 64,
                                "dataset_hash": "sha256:" + "4" * 64,
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        scope_file = base / "scope.json"
        scope_file.write_text(
            json.dumps(
                forward_policy.cohort_scope.build_scope(
                    scope_id="test-requested-fixtures",
                    competition_keys=["test_league"],
                    starts_at="2026-08-06T00:00:00Z",
                )
            ),
            encoding="utf-8",
        )
        return dataset, registry, corner_dataset, corner_registry, scope_file

    def _policy(
        self,
        base: Path,
        *,
        cohort_kind: str = forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
    ) -> dict:
        dataset, registry, corner_dataset, corner_registry, scope_file = (
            self._artifacts(base)
        )
        return forward_policy.build_policy_manifest(
            repo_root=self.repo_root,
            dataset_manifest=dataset,
            model_registry=registry,
            corner_dataset_manifest=corner_dataset,
            corner_model_registry=corner_registry,
            cohort_scope_file=scope_file,
            expected_final_merge_commit="a" * 40,
            cohort_kind=cohort_kind,
            created_at="2026-08-06T01:00:00Z",
            code_commit="a" * 40,
            protected_files=tuple(
                sorted(forward_policy.REQUIRED_PROVENANCE_PROTECTED_FILES)
            ),
        )

    @staticmethod
    def _reseal_policy(policy: dict) -> dict:
        value = deepcopy(policy)
        value.pop("policy_hash", None)
        value.pop("policy_id", None)
        value["policy_hash"] = forward_policy._hash_json(value)
        value["policy_id"] = (
            "untouched-live-forward-" + value["policy_hash"].split(":", 1)[1][:16]
        )
        return value

    def _previous_policy(self, base: Path) -> dict:
        value = self._policy(base)
        value["schema_version"] = forward_policy.PREVIOUS_POLICY_SCHEMA_VERSION
        value.pop("artifact_lineage")
        value.pop("cohort_scope")
        confirmation = value["confirmation_contract"]
        for field in (
            "cohort_kind",
            "untouched_confirmation_eligible_scope",
            "promotion_requires_cohort_kind",
            "local_shadow_promotion_eligible",
        ):
            confirmation.pop(field)
        confirmation["promotion_requirements"] = [
            "proper_scores_vs_same_time_bookmaker_no_vig",
            "calibration_without_material_misfit",
            "coverage_and_abstention_reported",
            "league_market_and_lead_time_stability",
            "clustered_confidence_intervals_support_improvement",
            "positive_performance_at_executable_prices_after_slippage",
        ]
        value["policy"]["candidate_evaluation"]["schema_version"] = (
            "candidate-evaluation/2.0.0"
        )
        value["policy"]["validation_protocol"] = {
            "schema_version": "forward-validation-protocol/1.0.0",
            "bootstrap_repetitions": 2000,
            "bootstrap_seed": 20260806,
            "minimum_confirmation_samples": 200,
            "minimum_iso_week_clusters": 20,
            "minimum_segment_samples": 40,
            "minimum_segment_clusters": 5,
            "same_time_tolerance_minutes": 5.0,
            "maximum_calibration_error": 0.05,
            "cluster_unit": "kickoff_iso_week",
            "required_baselines": [
                "historical_frequency",
                "independent_htft",
                "simple_poisson_dc",
                "bookmaker_no_vig",
            ],
            "queue_contract": "frozen_fixture_market_manifest",
            "external_timestamp_anchor_required_for_promotion": True,
        }
        return self._reseal_policy(value)

    @staticmethod
    def _previous_provenance_binding(policy: dict, cohort_id: str) -> dict:
        runtime = policy["policy"]
        value = {
            "schema_version": forward_policy.PREVIOUS_PROVENANCE_SCHEMA_VERSION,
            "package_version": policy["software"]["package_version"],
            "git_commit_sha": policy["code"]["commit"],
            "policy_hash": policy["policy_hash"],
            "validation_config_hash": forward_policy._hash_json(
                runtime["validation_protocol"]
            ),
            "dataset_manifest_hash": policy["data"]["declared_manifest_hash"],
            "model_registry_hash": policy["models"]["declared_registry_hash"],
            "renderer_policy_hash": forward_policy._hash_json(
                {
                    "display_policy": runtime["display_policy"],
                    "protected_renderer_files": {
                        path: policy["code"]["protected_files"][path]
                        for path in forward_policy.RENDERER_POLICY_PROTECTED_FILES
                    },
                }
            ),
            "cohort_id": cohort_id,
        }
        value["provenance_hash"] = forward_policy._hash_json(value)
        return value

    @staticmethod
    def _write_policy(base: Path, policy: dict) -> Path:
        path = forward_policy.policy_manifest_path(base, policy["policy_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
        return path

    @staticmethod
    def _clean_final_head(_root: Path, *arguments: str) -> str:
        if arguments == ("status", "--porcelain", "--untracked-files=normal"):
            return ""
        if arguments == ("rev-parse", "HEAD"):
            return "a" * 40
        raise AssertionError(f"unexpected git invocation: {arguments!r}")

    def test_policy_freezes_data_model_selector_gates_and_display(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = self._policy(Path(temporary))
            validated = forward_policy.validate_policy_manifest(policy)
            active = forward_policy.validate_active_runtime_policy_manifest(
                policy, repo_root=self.repo_root
            )

        self.assertEqual(validated["policy_hash"], policy["policy_hash"])
        self.assertEqual(active, validated)
        self.assertEqual(validated["schema_version"], "forward-policy/3.0.0")
        self.assertEqual(
            validated["software"]["package_version"],
            forward_policy.SOCCER_PREDICT_VERSION,
        )
        self.assertEqual(validated["code"]["expected_final_merge_commit"], "a" * 40)
        self.assertEqual(
            validated["confirmation_contract"]["cohort_kind"],
            forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
        )
        self.assertEqual(
            validated["confirmation_contract"]["untouched_confirmation_eligible_scope"],
            forward_policy.UNTOUCHED_ELIGIBILITY_SCOPE,
        )
        self.assertFalse(
            validated["confirmation_contract"]["local_shadow_promotion_eligible"]
        )
        requirements = validated["confirmation_contract"]["promotion_requirements"]
        self.assertIn("proper_scores_within_same_canonical_outcome_space", requirements)
        self.assertIn(
            "split_line_bookmaker_prices_limited_to_price_space_ev_and_clv",
            requirements,
        )
        self.assertNotIn("proper_scores_vs_same_time_bookmaker_no_vig", requirements)
        self.assertTrue(
            forward_policy.REQUIRED_PROVENANCE_PROTECTED_FILES.issubset(
                validated["code"]["protected_files"]
            )
        )
        self.assertEqual(
            validated["policy"]["candidate_evaluation"]["schema_version"],
            "candidate-evaluation/3.0.0",
        )
        protocol = validated["policy"]["validation_protocol"]
        self.assertEqual(
            protocol["schema_version"], "forward-validation-protocol/2.0.0"
        )
        self.assertNotIn("required_baselines", protocol)
        self.assertEqual(
            protocol["required_model_space_baselines"],
            ["historical_frequency", "independent_htft", "simple_poisson_dc"],
        )
        self.assertEqual(protocol["bookmaker_price_baseline"], "bookmaker_no_vig")
        self.assertEqual(
            protocol["bookmaker_proper_score_scope"],
            "categorical_same_outcome_space_only",
        )
        self.assertEqual(
            protocol["five_state_evaluation_scope"],
            "settlement_state_scores_ev_roi_plus_price_space_clv",
        )
        self.assertTrue(
            validated["policy"]["display_policy"]["top2_mass_and_remainder_required"]
        )
        self.assertTrue(
            validated["policy"]["display_policy"]["formal_primary_definition_unchanged"]
        )
        self.assertEqual(
            validated["policy"]["display_policy"]["observation_primary_schema_version"],
            "publication-outlook/1.0.0",
        )
        self.assertTrue(
            validated["policy"]["display_policy"][
                "observation_never_occupies_primary_cell"
            ]
        )
        self.assertEqual(
            validated["policy"]["candidate_evaluation"][
                "recent_gate_diagnostics_schema_version"
            ],
            "recent-candidate-gate-funnels/1.0.0",
        )
        self.assertEqual(
            validated["policy"]["candidate_evaluation"][
                "recent_distinct_match_windows"
            ],
            [50, 100],
        )
        self.assertEqual(
            validated["policy"]["validation_protocol"]["minimum_iso_week_clusters"],
            20,
        )
        self.assertFalse(
            validated["confirmation_contract"]["retrospective_records_allowed"]
        )

    def test_policy_hash_and_protected_file_tampering_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = self._policy(Path(temporary))
            policy["policy"]["release_thresholds"]["adverse_minimum_ev"] = 0.0
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "policy hash"
            ):
                forward_policy.validate_policy_manifest(policy)

            stripped = self._policy(Path(temporary))
            stripped.pop("confirmation_contract")
            stripped.pop("policy_hash")
            stripped.pop("policy_id")
            stripped["policy_hash"] = forward_policy._hash_json(stripped)
            stripped["policy_id"] = (
                "untouched-live-forward-"
                + stripped["policy_hash"].split(":", 1)[1][:16]
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "confirmation contract"
            ):
                forward_policy.validate_policy_manifest(stripped)

            resealed_version = self._policy(Path(temporary))
            resealed_version["software"]["package_version"] = "9.9.9"
            resealed_version.pop("policy_hash")
            resealed_version.pop("policy_id")
            resealed_version["policy_hash"] = forward_policy._hash_json(
                resealed_version
            )
            resealed_version["policy_id"] = (
                "untouched-live-forward-"
                + resealed_version["policy_hash"].split(":", 1)[1][:16]
            )
            historical = forward_policy.validate_policy_manifest(resealed_version)
            self.assertEqual(historical["software"]["package_version"], "9.9.9")
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError,
                "does not match soccer_predict.__version__",
            ):
                forward_policy.validate_active_runtime_policy_manifest(
                    resealed_version, repo_root=self.repo_root
                )

            unlinked_registry = self._policy(Path(temporary))
            unlinked_registry["models"]["dataset_manifest_hash"] = None
            unlinked_registry.pop("policy_hash")
            unlinked_registry.pop("policy_id")
            unlinked_registry["policy_hash"] = forward_policy._hash_json(
                unlinked_registry
            )
            unlinked_registry["policy_id"] = (
                "untouched-live-forward-"
                + unlinked_registry["policy_hash"].split(":", 1)[1][:16]
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "not linked"
            ):
                forward_policy.validate_policy_manifest(unlinked_registry)

    def test_v2_policy_and_provenance_binding_are_read_only_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            policy = self._previous_policy(base)
            validated = forward_policy.validate_policy_manifest(policy)
            self.assertEqual(
                validated["schema_version"],
                forward_policy.PREVIOUS_POLICY_SCHEMA_VERSION,
            )
            self.assertNotIn("cohort_kind", validated["confirmation_contract"])
            self.assertEqual(validated["code"]["expected_final_merge_commit"], "a" * 40)
            self.assertEqual(
                validated["software"]["package_version"],
                forward_policy.SOCCER_PREDICT_VERSION,
            )
            self.assertEqual(
                validated["policy"]["validation_protocol"]["schema_version"],
                "forward-validation-protocol/1.0.0",
            )

            cohort_id = "historical-policy-v2"
            binding = {
                "schema_version": (
                    forward_policy.PREVIOUS_PROVENANCE_RECORD_BINDING_SCHEMA_VERSION
                ),
                "cohort_id": cohort_id,
                "cohort_hash": "sha256:" + "6" * 64,
                "cohort_starts_at": "2026-08-06T01:01:00+00:00",
                "policy_id": policy["policy_id"],
                "policy_hash": policy["policy_hash"],
                "policy_snapshot": policy,
                "recorded_code_commit": "a" * 40,
                "archived_at": "2026-08-06T01:02:00+00:00",
                "untouched_confirmation_eligible": True,
                "provenance_binding": self._previous_provenance_binding(
                    policy, cohort_id
                ),
            }
            binding["binding_hash"] = forward_policy._hash_json(binding)
            replayed = forward_policy.validate_record_binding(binding)
            assert replayed is not None
            self.assertEqual(
                replayed["policy_snapshot"]["schema_version"],
                forward_policy.PREVIOUS_POLICY_SCHEMA_VERSION,
            )
            with self.assertRaisesRegex(forward_policy.ForwardPolicyError, "read-only"):
                forward_policy.build_provenance_binding(policy, cohort_id=cohort_id)
            with self.assertRaisesRegex(forward_policy.ForwardPolicyError, "read-only"):
                forward_policy.bind_observation_commitment(
                    binding, "sha256:" + "9" * 64
                )
            committed = deepcopy(binding)
            committed.pop("binding_hash")
            committed["schema_version"] = (
                forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
            )
            committed["observation_commitment_hash"] = "sha256:" + "9" * 64
            committed["binding_hash"] = forward_policy._hash_json(committed)
            self.assertEqual(
                committed["schema_version"],
                forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
            )
            self.assertEqual(
                forward_policy.validate_record_binding(committed)[
                    "observation_commitment_hash"
                ],
                "sha256:" + "9" * 64,
            )

            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "cannot be activated"
            ):
                forward_policy.validate_active_runtime_policy_manifest(
                    policy, repo_root=self.repo_root
                )
            policy_file = self._write_policy(base / "active", policy)
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError,
                    "only forward-policy/3.0.0",
                ):
                    forward_policy.start_cohort(
                        base_dir=base / "active",
                        policy_file=policy_file,
                        cohort_id="must-not-reactivate-v2",
                        cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                        starts_at="2026-08-06T02:00:00Z",
                        repo_root=self.repo_root,
                    )

            cases = {
                "expected-final-merge": lambda item: item["code"].pop(
                    "expected_final_merge_commit"
                ),
                "software": lambda item: item.pop("software"),
                "protected-files": lambda item: item["code"]["protected_files"].pop(
                    "scripts/forward_policy.py"
                ),
                "validation-protocol": lambda item: item["policy"].pop(
                    "validation_protocol"
                ),
            }
            for label, mutate in cases.items():
                with self.subTest(label=label):
                    broken = deepcopy(policy)
                    mutate(broken)
                    broken = self._reseal_policy(broken)
                    with self.assertRaises(forward_policy.ForwardPolicyError):
                        forward_policy.validate_policy_manifest(broken)

            v2_with_kind = deepcopy(policy)
            v2_with_kind["confirmation_contract"]["cohort_kind"] = (
                forward_policy.LOCAL_INTEGRITY_SHADOW_KIND
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "pre-v3.*cohort_kind"
            ):
                forward_policy.validate_policy_manifest(
                    self._reseal_policy(v2_with_kind)
                )

            v3_without_kind = self._policy(base)
            v3_without_kind["confirmation_contract"].pop("cohort_kind")
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "cohort_kind must be one of"
            ):
                forward_policy.validate_policy_manifest(
                    self._reseal_policy(v3_without_kind)
                )

    def test_policy_build_rejects_registry_without_dataset_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dataset, registry, corner_dataset, corner_registry, scope_file = (
                self._artifacts(base)
            )
            registry_payload = json.loads(registry.read_text(encoding="utf-8"))
            registry_payload.pop("dataset_manifest_hash")
            registry.write_text(json.dumps(registry_payload), encoding="utf-8")
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "role-aware data/model lineage"
            ):
                forward_policy.build_policy_manifest(
                    repo_root=self.repo_root,
                    dataset_manifest=dataset,
                    model_registry=registry,
                    corner_dataset_manifest=corner_dataset,
                    corner_model_registry=corner_registry,
                    cohort_scope_file=scope_file,
                    expected_final_merge_commit="a" * 40,
                    cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                    created_at="2026-08-06T01:00:00Z",
                    code_commit="a" * 40,
                    protected_files=tuple(
                        sorted(forward_policy.REQUIRED_PROVENANCE_PROTECTED_FILES)
                    ),
                )

    def test_active_policy_rejects_resealed_runtime_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            policy = self._policy(base)
            cases = {
                "selector": lambda item: item["policy"]["selector"].update(
                    {"one_primary_per_match": False}
                ),
                "threshold": lambda item: item["policy"]["release_thresholds"].update(
                    {"minimum_firms": 1}
                ),
                "market-status": lambda item: item["policy"]["market_status"].update(
                    {"htft": "formal"}
                ),
                "display": lambda item: item["policy"]["display_policy"].update(
                    {"joint_event_count": 3}
                ),
                "protocol": lambda item: item["policy"]["validation_protocol"].update(
                    {"minimum_confirmation_samples": 1}
                ),
            }
            resealed: dict[str, dict] = {}
            for label, mutate in cases.items():
                with self.subTest(label=label):
                    candidate = deepcopy(policy)
                    mutate(candidate)
                    candidate = self._reseal_policy(candidate)
                    forward_policy.validate_policy_manifest(candidate)
                    with self.assertRaisesRegex(
                        forward_policy.ForwardPolicyError,
                        "do not match the installed runtime",
                    ):
                        forward_policy.validate_active_runtime_policy_manifest(
                            candidate, repo_root=self.repo_root
                        )
                    resealed[label] = candidate

            start_base = base / "start"
            candidate = resealed["selector"]
            policy_file = self._write_policy(start_base, candidate)
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError,
                    "do not match the installed runtime",
                ):
                    forward_policy.start_cohort(
                        base_dir=start_base,
                        policy_file=policy_file,
                        cohort_id="resealed-runtime",
                        cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                        starts_at="2026-08-06T02:00:00Z",
                        repo_root=self.repo_root,
                    )

    def test_head_v2_history_chain_is_replayable_but_defect_quarantined(self) -> None:
        """Golden replay for the exact history schemas emitted by the previous HEAD."""

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            policy = self._previous_policy(base)
            real_commit = forward_policy._git(self.repo_root, "rev-parse", "HEAD")
            policy["code"]["commit"] = real_commit
            policy["code"]["expected_final_merge_commit"] = real_commit
            policy = self._reseal_policy(policy)
            cohort = {
                "schema_version": forward_policy.LEGACY_COHORT_SCHEMA_VERSION,
                "artifact_type": "soccer_untouched_live_forward_cohort",
                "cohort_id": "head-v2-golden",
                "status": "active",
                "starts_at": "2026-08-01T00:01:00+00:00",
                "policy_file": "C:/historical/forward-policy.json",
                "policy_id": policy["policy_id"],
                "policy_hash": policy["policy_hash"],
                "retrospective_records_allowed": False,
                "closed_at": None,
            }
            cohort["cohort_hash"] = forward_policy._hash_json(cohort)
            queue_key = forward_validation._queue_key(
                cohort["cohort_id"], "1000", "1x2"
            )
            queue = {
                "schema_version": "forward-eligibility-queue/1.0.0",
                "artifact_type": "soccer_forward_eligibility_queue",
                "queue_id": "head-v2-queue",
                "cohort_id": cohort["cohort_id"],
                "policy_id": policy["policy_id"],
                "policy_hash": policy["policy_hash"],
                "frozen_at": "2026-08-01T00:02:00+00:00",
                "entries": [
                    {
                        "fixture_id": "1000",
                        "home_team": "Alpha",
                        "away_team": "Bravo",
                        "league": "league-a",
                        "market": "1x2",
                        "kickoff": "2026-08-01T01:00:00+00:00",
                        "queue_key": queue_key,
                    }
                ],
                "integrity_assurance": (
                    "local_content_hash_only_no_external_timestamp"
                ),
            }
            queue["queue_hash"] = forward_policy._hash_json(queue)
            provenance = self._previous_provenance_binding(policy, cohort["cohort_id"])
            prediction = {
                "provenance_binding": provenance,
                "queue_hash": queue["queue_hash"],
                "queue_key": queue_key,
                "fixture_id": "1000",
                "home_team": "Alpha",
                "away_team": "Bravo",
                "observation_id": forward_validation._observation_id(queue_key),
                "league": "league-a",
                "market": "1x2",
                "kickoff": "2026-08-01T01:00:00+00:00",
                "generated_at": "2026-08-01T00:29:00+00:00",
                "lead_time_minutes": 31,
                "status": "unavailable",
                "model_probabilities": None,
                "baselines": {},
                "baseline_lineage": {},
                "bookmaker_snapshot": None,
                "execution_entry": None,
                "unavailable_reasons": ["historical_golden_fixture"],
            }

            def committed_binding(observation_hash: str) -> dict:
                binding = {
                    "schema_version": (
                        forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
                    ),
                    "cohort_id": cohort["cohort_id"],
                    "cohort_hash": cohort["cohort_hash"],
                    "cohort_starts_at": cohort["starts_at"],
                    "policy_id": policy["policy_id"],
                    "policy_hash": policy["policy_hash"],
                    "policy_snapshot": policy,
                    "recorded_code_commit": real_commit,
                    "archived_at": "2026-08-01T00:30:00+00:00",
                    "untouched_confirmation_eligible": True,
                    "provenance_binding": provenance,
                    "observation_commitment_hash": observation_hash,
                }
                binding["binding_hash"] = forward_policy._hash_json(binding)
                return binding

            micro_binding = committed_binding(forward_policy._hash_json(prediction))
            commitment = {
                "schema_version": "forward-observation-commitment/1.0.0",
                "prediction_payload": prediction,
                "forward_policy_binding": micro_binding,
            }
            commitment["commitment_hash"] = forward_policy._hash_json(commitment)
            settlement = {
                "schema_version": "forward-observation-settlement/1.0.0",
                "observation_id": prediction["observation_id"],
                "commitment_hash": commitment["commitment_hash"],
                "status": "pending",
                "observed_outcome": None,
                "result_collected_at": None,
                "result_source_evidence_hash": None,
                "closing_snapshot": None,
            }
            settlement["settlement_hash"] = forward_policy._hash_json(settlement)
            ledger_payload = {
                "schema_version": "forward-observations/2.0.0",
                "cohort_id": cohort["cohort_id"],
                "policy_id": policy["policy_id"],
                "policy_hash": policy["policy_hash"],
                "policy_manifest": policy,
                "cohort_manifest": cohort,
                "cohort_closure": None,
                "market_schemas": {
                    "1x2": {
                        "outcomes": ["H", "D", "A"],
                        "settlement_semantics": "categorical",
                    }
                },
                "queue_manifest": queue,
                "commitments": [commitment],
                "settlements": [settlement],
            }
            market_commitments = memory_store._forward_market_commitments(
                ledger_payload
            )
            ledger_archive = {
                "schema_version": (
                    memory_store.PREVIOUS_FORWARD_LEDGER_ARCHIVE_SCHEMA_VERSION
                ),
                "fixture_id": "1000",
                "ledger_hash": forward_policy._hash_json(ledger_payload),
                "ledger_payload": ledger_payload,
                "market_commitments": market_commitments,
            }
            ledger_archive["archive_hash"] = forward_policy._hash_json(ledger_archive)
            record = {
                "match_id": "1000",
                "mode": "prematch",
                "created_at": "2026-08-01T00:30:00+00:00",
                "updated_at": "2026-08-01T00:30:00+00:00",
                "analysis_stage": "initial",
                "league": "league-a",
                "league_key": "league-a",
                "kickoff": "2026-08-01T01:00:00+00:00",
                "home_team": "Alpha",
                "away_team": "Bravo",
                "probabilities": {"home_win": 0.4, "draw": 0.3, "away_win": 0.3},
                "evaluation_eligibility": {"strict_forward_oos": True},
                "forward_validation_ledger": ledger_archive,
            }
            base_record_binding = deepcopy(micro_binding)
            base_record_binding.pop("binding_hash")
            base_record_binding.pop("observation_commitment_hash")
            base_record_binding["schema_version"] = (
                forward_policy.PREVIOUS_PROVENANCE_RECORD_BINDING_SCHEMA_VERSION
            )
            base_record_binding["binding_hash"] = forward_policy._hash_json(
                base_record_binding
            )
            record_prediction = (
                memory_store._canonical_forward_record_prediction_payload(
                    record,
                    ledger_archive,
                    provenance,
                    schema_version=(
                        memory_store.PREVIOUS_FORWARD_RECORD_PREDICTION_SCHEMA_VERSION
                    ),
                )
            )
            record_binding = committed_binding(
                forward_policy._hash_json(record_prediction)
            )
            record_commitment = {
                "schema_version": (
                    memory_store.PREVIOUS_FORWARD_RECORD_COMMITMENT_SCHEMA_VERSION
                ),
                "prediction_payload": record_prediction,
                "prediction_hash": forward_policy._hash_json(record_prediction),
                "forward_policy_binding": record_binding,
            }
            record_commitment["commitment_hash"] = forward_policy._hash_json(
                record_commitment
            )
            record["forward_policy_binding"] = record_binding
            record["forward_prediction_commitment"] = record_commitment
            record["archive_version_hash"] = forward_policy._hash_json(
                memory_store.snapshot_payload(memory_store.revision_snapshot(record))
            )

            validated = memory_store.validate_forward_record_prediction_commitment(
                record
            )
            assert validated is not None
            self.assertEqual(
                validated["schema_version"],
                memory_store.PREVIOUS_FORWARD_RECORD_COMMITMENT_SCHEMA_VERSION,
            )
            self.assertEqual(
                memory_store.forward_policy_binding_for_record(record)[
                    "schema_version"
                ],
                forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
            )
            summary = memory_store.untouched_live_forward_summary([record])
            self.assertEqual(summary["reviewed_matches"], 0)
            self.assertEqual(summary["historical_quarantine"]["record_count"], 1)
            self.assertFalse(summary["historical_quarantine"]["formal_export_eligible"])

            preliminary_receipt = memory_store._forward_history_record_receipt(record)
            record_manifest = empty_record_manifest(cohort)
            record_manifest["record_count"] = 1
            record_manifest["records"] = [
                memory_store._record_manifest_entry_from_receipt(preliminary_receipt)
            ]
            record_manifest.pop("manifest_hash")
            record_manifest["manifest_hash"] = forward_policy._hash_json(
                record_manifest
            )
            closure = {
                "schema_version": forward_policy.CLOSURE_SCHEMA_VERSION,
                "artifact_type": ("soccer_untouched_live_forward_cohort_closure"),
                "cohort_id": cohort["cohort_id"],
                "cohort_hash": cohort["cohort_hash"],
                "policy_id": cohort["policy_id"],
                "policy_hash": cohort["policy_hash"],
                "starts_at": cohort["starts_at"],
                "closed_at": "2026-08-02T00:00:00+00:00",
                "reason": "explicit_policy_boundary",
                "record_manifest_hash": record_manifest["manifest_hash"],
                "record_manifest": record_manifest,
            }
            closure["closure_hash"] = forward_policy._hash_json(closure)
            receipt = memory_store._forward_history_record_receipt(
                record, cohort_closure=closure
            )
            forward_validation._validate_history_record_receipt(receipt)
            history_binding = {
                "schema_version": (
                    forward_validation.PREVIOUS_HISTORY_LEDGER_BINDING_SCHEMA_VERSION
                ),
                "artifact_type": forward_validation.HISTORY_AGGREGATE_ARTIFACT_TYPE,
                "cohort_id": cohort["cohort_id"],
                "policy_id": policy["policy_id"],
                "policy_hash": policy["policy_hash"],
                "fixture_ids": ["1000"],
                "receipts": [receipt],
            }
            history_binding["binding_hash"] = forward_policy._hash_json(history_binding)
            forward_validation._validate_history_ledger_binding(history_binding)
            with self.assertRaisesRegex(ValueError, "defect-quarantined"):
                memory_store.forward_validation_input_for_records(
                    [record], cohort_closure=closure
                )
            with self.assertRaises(forward_validation.ForwardValidationError):
                forward_validation.validate_input(
                    {
                        "schema_version": "forward-observations/2.0.0",
                        "history_ledger_binding": history_binding,
                    }
                )

            review_record = deepcopy(record)
            review_record["status"] = "pending"
            memory_store.save_history(
                memory_store.data_path(str(base)), [review_record]
            )
            review_args = SimpleNamespace(
                base_dir=str(base),
                verified_finished=True,
                match_id="1000",
                home_score=1,
                away_score=0,
                half_home_score=0,
                half_away_score=0,
                home_corners=None,
                away_corners=None,
                key_learning="historical record remains reviewable in quarantine",
                verification_source="https://example.test/final/1000",
                verification_collected_at="2026-08-01T03:00:00+00:00",
            )
            with (
                mock.patch.object(
                    memory_store,
                    "validated_source_evidence_audit",
                    return_value={"historical_replay": True},
                ),
                mock.patch.object(
                    memory_store,
                    "utc_now",
                    return_value=datetime(2026, 8, 1, 3, 1, tzinfo=timezone.utc),
                ),
            ):
                reviewed = memory_store.cmd_review(review_args)["record"]
            self.assertEqual(reviewed["status"], "reviewed")
            self.assertEqual(reviewed["final_score"], "1-0")
            self.assertEqual(
                reviewed["settlement_basis"]["forward_policy_binding"][
                    "schema_version"
                ],
                forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
            )
            self.assertEqual(
                memory_store.untouched_live_forward_summary([reviewed])[
                    "historical_quarantine"
                ]["record_count"],
                1,
            )

    def test_policy_file_must_be_content_addressed_in_canonical_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            policy = self._policy(base)

            outside = base / "outside-policy.json"
            outside.write_text(json.dumps(policy), encoding="utf-8")
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError, "outside the canonical"
                ):
                    forward_policy.start_cohort(
                        base_dir=base / "outside",
                        policy_file=outside,
                        cohort_id="outside-policy",
                        cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                        starts_at="2026-08-06T02:00:00Z",
                        repo_root=self.repo_root,
                    )

            wrong_base = base / "wrong-name"
            wrong_id = "untouched-live-forward-" + "0" * 16
            wrong_name = forward_policy.policy_manifest_path(wrong_base, wrong_id)
            wrong_name.parent.mkdir(parents=True, exist_ok=True)
            wrong_name.write_text(json.dumps(policy), encoding="utf-8")
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError,
                    "filename does not match",
                ):
                    forward_policy.start_cohort(
                        base_dir=wrong_base,
                        policy_file=wrong_name,
                        cohort_id="wrong-policy-name",
                        cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                        starts_at="2026-08-06T02:00:00Z",
                        repo_root=self.repo_root,
                    )

            active_base = base / "active"
            canonical = self._write_policy(active_base, policy)
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                forward_policy.start_cohort(
                    base_dir=active_base,
                    policy_file=canonical,
                    cohort_id="canonical-policy",
                    cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                    starts_at="2026-08-06T02:00:00Z",
                    repo_root=self.repo_root,
                )
            outside_directory = base / "copied"
            outside_directory.mkdir(parents=True)
            copied = outside_directory / canonical.name
            copied.write_text(json.dumps(policy), encoding="utf-8")
            pointer_path = forward_policy.active_cohort_path(active_base)
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            pointer["policy_file"] = str(copied.resolve())
            pointer.pop("cohort_hash")
            pointer["cohort_hash"] = forward_policy._hash_json(pointer)
            forward_policy._atomic_json(pointer_path, pointer)
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "outside the canonical"
            ):
                forward_policy.load_active_binding(
                    base_dir=active_base,
                    repo_root=self.repo_root,
                    archived_at="2026-08-06T02:01:00Z",
                )

    def test_cohort_ids_reject_path_traversal_and_windows_devices(self) -> None:
        invalid_ids = (
            ".",
            "..",
            "../escape",
            r"..\escape",
            "nested/escape",
            r"nested\escape",
            "a..b",
            "trailing.",
            "has space",
            "équipe",
            "CON",
            "nul.txt",
            "LPT1.log",
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            policy = self._policy(base)
            policy_file = self._write_policy(base, policy)

            for cohort_id in invalid_ids:
                with self.subTest(cohort_id=cohort_id, entrypoint="path"):
                    with self.assertRaises(forward_policy.ForwardPolicyError):
                        forward_policy.cohort_manifest_path(base, cohort_id)
                with self.subTest(cohort_id=cohort_id, entrypoint="provenance"):
                    with self.assertRaises(forward_policy.ForwardPolicyError):
                        forward_policy.build_provenance_binding(
                            policy, cohort_id=cohort_id
                        )

            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                _path, cohort = forward_policy.start_cohort(
                    base_dir=base,
                    policy_file=policy_file,
                    cohort_id="safe-cohort",
                    cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                    starts_at="2026-08-06T02:00:00Z",
                    repo_root=self.repo_root,
                )
                binding = forward_policy.load_active_binding(
                    base_dir=base,
                    repo_root=self.repo_root,
                    archived_at="2026-08-06T02:01:00Z",
                )
            assert binding is not None
            _closure_path, closure = forward_policy.close_cohort(
                base_dir=base,
                record_manifest=empty_record_manifest(cohort),
                closed_at="2026-08-06T03:00:00Z",
            )

            attacked_manifest = empty_record_manifest(cohort)
            attacked_manifest["cohort_id"] = "../escape"
            attacked_manifest.pop("manifest_hash")
            attacked_manifest["manifest_hash"] = forward_policy._hash_json(
                attacked_manifest
            )
            with self.assertRaises(forward_policy.ForwardPolicyError):
                forward_policy.validate_record_manifest(attacked_manifest)

            attacked_closure = deepcopy(closure)
            attacked_closure["cohort_id"] = "CON"
            attacked_closure.pop("closure_hash")
            attacked_closure["closure_hash"] = forward_policy._hash_json(
                attacked_closure
            )
            with self.assertRaises(forward_policy.ForwardPolicyError):
                forward_policy.validate_closure(attacked_closure)

            attacked_binding = deepcopy(binding)
            attacked_binding["cohort_id"] = r"a\b"
            attacked_binding.pop("binding_hash")
            attacked_binding["binding_hash"] = forward_policy._hash_json(
                attacked_binding
            )
            with self.assertRaises(forward_policy.ForwardPolicyError):
                forward_policy.validate_record_binding(attacked_binding)

    def test_close_cohort_recovers_idempotently_after_pointer_replace_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            policy = self._policy(base)
            policy_file = self._write_policy(base, policy)
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                active_path, cohort = forward_policy.start_cohort(
                    base_dir=base,
                    policy_file=policy_file,
                    cohort_id="crash-recovery",
                    cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                    starts_at="2026-08-06T02:00:00Z",
                    repo_root=self.repo_root,
                )
            manifest = empty_record_manifest(cohort)
            manifest_path = base / "crash-recovery-record-manifest.json"
            memory_store._write_forward_record_manifest_once(
                manifest_path, manifest, cohort=cohort
            )
            original_manifest_bytes = manifest_path.read_bytes()
            memory_store._write_forward_record_manifest_once(
                manifest_path, manifest, cohort=cohort
            )
            self.assertEqual(manifest_path.read_bytes(), original_manifest_bytes)
            different_manifest = deepcopy(manifest)
            different_manifest["record_count"] = 1
            with self.assertRaisesRegex(ValueError, "differs"):
                memory_store._write_forward_record_manifest_once(
                    manifest_path, different_manifest, cohort=cohort
                )
            self.assertEqual(manifest_path.read_bytes(), original_manifest_bytes)
            original_atomic = forward_policy._atomic_json

            def fail_closed_pointer(path: Path, value: dict) -> None:
                if path == active_path and value.get("status") == "closed":
                    raise OSError("simulated pointer replace failure")
                original_atomic(path, value)

            with (
                mock.patch.object(
                    forward_policy, "_atomic_json", side_effect=fail_closed_pointer
                ),
                mock.patch.object(
                    forward_policy,
                    "_now_iso",
                    return_value="2026-08-06T03:00:00+00:00",
                ),
            ):
                with self.assertRaisesRegex(OSError, "simulated pointer"):
                    forward_policy.close_cohort(
                        base_dir=base,
                        record_manifest=manifest,
                    )

            closure_path = forward_policy.cohort_closure_path(base, cohort["cohort_id"])
            original_closure_bytes = closure_path.read_bytes()
            self.assertEqual(
                json.loads(active_path.read_text(encoding="utf-8"))["status"],
                "active",
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "different content"
            ):
                forward_policy.close_cohort(
                    base_dir=base,
                    record_manifest=manifest,
                    closed_at="2026-08-06T03:01:00Z",
                )
            self.assertEqual(closure_path.read_bytes(), original_closure_bytes)

            with mock.patch.object(
                forward_policy,
                "_now_iso",
                side_effect=AssertionError(
                    "crash recovery must reuse the frozen closure time"
                ),
            ):
                recovered_path, recovered = forward_policy.close_cohort(
                    base_dir=base,
                    record_manifest=manifest,
                )
            self.assertEqual(recovered_path, closure_path)
            self.assertEqual(closure_path.read_bytes(), original_closure_bytes)
            self.assertEqual(
                json.loads(active_path.read_text(encoding="utf-8"))["status"],
                "closed",
            )
            self.assertEqual(
                recovered,
                forward_policy.validate_closure(
                    json.loads(closure_path.read_text(encoding="utf-8")),
                    cohort=cohort,
                    require_record_manifest=True,
                ),
            )
            retried_path, retried = forward_policy.close_cohort(
                base_dir=base,
                record_manifest=manifest,
            )
            self.assertEqual(retried_path, closure_path)
            self.assertEqual(retried, recovered)
            self.assertEqual(closure_path.read_bytes(), original_closure_bytes)
            closed_pointer = json.loads(active_path.read_text(encoding="utf-8"))
            attacked_pointer = deepcopy(closed_pointer)
            attacked_pointer.pop("cohort_hash")
            attacked_pointer["closed_at"] = "2026-08-06T03:01:00+00:00"
            attacked_pointer["cohort_hash"] = forward_policy._hash_json(
                attacked_pointer
            )
            active_path.write_text(
                json.dumps(attacked_pointer, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "does not match immutable closure"
            ):
                forward_policy.close_cohort(
                    base_dir=base,
                    record_manifest=manifest,
                )
            active_path.write_text(
                json.dumps(closed_pointer, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "different content"
            ):
                forward_policy.close_cohort(
                    base_dir=base,
                    record_manifest=manifest,
                    closed_at="2026-08-06T03:01:00Z",
                )

    def test_cohort_rejects_retrospective_binding_and_preserves_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            policy = self._policy(base)
            policy_file = self._write_policy(base, policy)
            start = datetime(2026, 8, 6, 2, tzinfo=timezone.utc)
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                _path, cohort = forward_policy.start_cohort(
                    base_dir=base,
                    policy_file=policy_file,
                    cohort_id="confirmation-2026-08",
                    cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                    starts_at=start,
                    repo_root=self.repo_root,
                )
                forward_policy.cohort_scope.append_event(
                    base_dir=base,
                    cohort_id=cohort["cohort_id"],
                    scope=policy["cohort_scope"]["scope_snapshot"],
                    event_type="requested",
                    fixture_id="fixture-a",
                    competition_key="test_league",
                    home_team="Home",
                    away_team="Away",
                    kickoff="2026-08-06T03:00:00Z",
                    occurred_at="2026-08-06T02:00:00Z",
                )

                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError, "predates"
                ):
                    forward_policy.load_active_binding(
                        base_dir=base,
                        repo_root=self.repo_root,
                        archived_at=start - timedelta(seconds=1),
                    )

                binding = forward_policy.load_active_binding(
                    base_dir=base,
                    repo_root=self.repo_root,
                    archived_at=start + timedelta(seconds=1),
                    fixture_id="fixture-a",
                )
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(cohort["schema_version"], "live-forward-cohort/2.0.0")
            self.assertEqual(cohort["kind"], forward_policy.LOCAL_INTEGRITY_SHADOW_KIND)
            self.assertEqual(binding["cohort_hash"], cohort["cohort_hash"])
            self.assertEqual(
                binding["schema_version"],
                forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
            )
            self.assertNotIn("untouched_confirmation_eligible", binding)
            self.assertEqual(
                binding["cohort_kind"], forward_policy.LOCAL_INTEGRITY_SHADOW_KIND
            )
            self.assertEqual(
                binding["assurance_scope"], forward_policy.LOCAL_ASSURANCE_SCOPE
            )
            self.assertFalse(binding["promotion_evidence_eligible"])
            self.assertEqual(
                binding["provenance_binding"]["schema_version"],
                forward_policy.PROVENANCE_SCHEMA_VERSION,
            )
            self.assertEqual(
                binding["provenance_binding"]["assurance_scope"],
                forward_policy.LOCAL_ASSURANCE_SCOPE,
            )
            self.assertFalse(
                binding["provenance_binding"]["promotion_evidence_eligible"]
            )
            self.assertEqual(
                forward_policy.validate_record_binding(binding)["policy_hash"],
                policy["policy_hash"],
            )
            committed = forward_policy.bind_observation_commitment(
                binding, "sha256:" + "9" * 64
            )
            self.assertEqual(
                committed["schema_version"],
                forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
            )
            provenance = committed["provenance_binding"]
            self.assertEqual(
                provenance["package_version"], forward_policy.SOCCER_PREDICT_VERSION
            )
            self.assertEqual(provenance["git_commit_sha"], "a" * 40)
            self.assertEqual(provenance["policy_hash"], policy["policy_hash"])
            self.assertEqual(provenance["cohort_id"], "confirmation-2026-08")
            for field in (
                "validation_config_hash",
                "dataset_manifest_hash",
                "model_registry_hash",
                "renderer_policy_hash",
            ):
                self.assertRegex(provenance[field], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(
                forward_policy.validate_record_binding(committed)[
                    "observation_commitment_hash"
                ],
                "sha256:" + "9" * 64,
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "different observation"
            ):
                forward_policy.bind_observation_commitment(
                    committed, "sha256:" + "8" * 64
                )
            self.assertTrue(
                forward_policy.cohort_manifest_path(
                    base, "confirmation-2026-08"
                ).is_file()
            )

            tampered = json.loads(json.dumps(binding))
            tampered["policy_snapshot"]["policy"]["display_policy"][
                "joint_event_count"
            ] = 3
            with self.assertRaises(forward_policy.ForwardPolicyError):
                forward_policy.validate_record_binding(tampered)

            forward_policy.cohort_scope.append_event(
                base_dir=base,
                cohort_id=cohort["cohort_id"],
                scope=policy["cohort_scope"]["scope_snapshot"],
                event_type="unavailable",
                fixture_id="fixture-a",
                competition_key="test_league",
                home_team="Home",
                away_team="Away",
                kickoff="2026-08-06T03:00:00Z",
                occurred_at="2026-08-06T02:30:00Z",
                reason="source_unavailable",
            )
            close_manifest = empty_record_manifest(cohort)
            denominator = forward_policy.cohort_scope.build_denominator(
                scope=policy["cohort_scope"]["scope_snapshot"],
                cohort_id=cohort["cohort_id"],
                events=forward_policy.cohort_scope.load_events(
                    base,
                    cohort["cohort_id"],
                    scope=policy["cohort_scope"]["scope_snapshot"],
                ),
                record_manifest=close_manifest,
            )
            close_manifest.pop("manifest_hash")
            close_manifest["denominator"] = denominator
            close_manifest["denominator_hash"] = denominator["denominator_hash"]
            close_manifest["manifest_hash"] = forward_policy._hash_json(close_manifest)
            closure_path, closure = forward_policy.close_cohort(
                base_dir=base,
                record_manifest=close_manifest,
                closed_at=start + timedelta(hours=1),
            )
            self.assertTrue(closure_path.is_file())
            self.assertEqual(closure["cohort_hash"], cohort["cohort_hash"])
            self.assertEqual(
                forward_policy.validate_closure(
                    closure, cohort=cohort, require_record_manifest=True
                )["closure_hash"],
                closure["closure_hash"],
            )
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                self.assertIsNone(
                    forward_policy.load_active_binding(
                        base_dir=base,
                        repo_root=self.repo_root,
                        archived_at=start + timedelta(hours=2),
                    )
                )
                _new_path, new_cohort = forward_policy.start_cohort(
                    base_dir=base,
                    policy_file=policy_file,
                    cohort_id="confirmation-2026-09",
                    cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                    starts_at=start + timedelta(hours=2),
                    repo_root=self.repo_root,
                )
            self.assertEqual(new_cohort["cohort_id"], "confirmation-2026-09")

    def test_provenance_field_rehash_attacks_do_not_escape_policy_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = self._policy(Path(temporary))
        cohort_id = "confirmation-attack"
        binding = {
            "schema_version": forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
            "cohort_id": cohort_id,
            "cohort_hash": "sha256:" + "6" * 64,
            "cohort_starts_at": "2026-08-06T01:01:00+00:00",
            "policy_id": policy["policy_id"],
            "policy_hash": policy["policy_hash"],
            "policy_snapshot": policy,
            "recorded_code_commit": "a" * 40,
            "archived_at": "2026-08-06T01:02:00+00:00",
            "cohort_kind": forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
            "assurance_scope": forward_policy.LOCAL_ASSURANCE_SCOPE,
            "promotion_evidence_eligible": False,
            "provenance_binding": forward_policy.build_provenance_binding(
                policy, cohort_id=cohort_id
            ),
        }
        binding["binding_hash"] = forward_policy._hash_json(binding)
        forward_policy.validate_record_binding(binding)

        replacements = {
            "package_version": "9.9.9",
            "git_commit_sha": "b" * 40,
            "policy_hash": "sha256:" + "b" * 64,
            "validation_config_hash": "sha256:" + "b" * 64,
            "dataset_manifest_hash": "sha256:" + "b" * 64,
            "model_registry_hash": "sha256:" + "b" * 64,
            "renderer_policy_hash": "sha256:" + "b" * 64,
            "cohort_id": "different-cohort",
            "cohort_kind": forward_policy.PROMOTABLE_CONFIRMATION_KIND,
            "assurance_scope": forward_policy.PROMOTABLE_ASSURANCE_SCOPE,
            "promotion_evidence_eligible": True,
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                tampered = deepcopy(binding)
                provenance = tampered["provenance_binding"]
                provenance[field] = replacement
                provenance.pop("provenance_hash")
                provenance["provenance_hash"] = forward_policy._hash_json(provenance)
                tampered.pop("binding_hash")
                tampered["binding_hash"] = forward_policy._hash_json(tampered)
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError, "does not reproduce"
                ):
                    forward_policy.validate_record_binding(tampered)

    def test_previous_package_policy_and_binding_remain_historically_replayable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            with mock.patch.object(forward_policy, "SOCCER_PREDICT_VERSION", "3.4.0"):
                policy = self._policy(base)
                active_base = base / "active"
                policy_file = self._write_policy(active_base, policy)
                start = datetime(2026, 8, 6, 2, tzinfo=timezone.utc)
                with mock.patch.object(
                    forward_policy, "_git", side_effect=self._clean_final_head
                ):
                    forward_policy.start_cohort(
                        base_dir=active_base,
                        policy_file=policy_file,
                        cohort_id="historical-3-4",
                        cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                        starts_at=start,
                        repo_root=self.repo_root,
                    )
                    binding = forward_policy.load_active_binding(
                        base_dir=active_base,
                        repo_root=self.repo_root,
                        archived_at=start + timedelta(seconds=1),
                        observation_commitment_hash="sha256:" + "9" * 64,
                    )
            assert binding is not None

            with mock.patch.object(forward_policy, "SOCCER_PREDICT_VERSION", "3.5.0"):
                historical_policy = forward_policy.validate_policy_manifest(policy)
                historical_binding = forward_policy.validate_record_binding(binding)
                self.assertEqual(
                    historical_policy["software"]["package_version"], "3.4.0"
                )
                assert historical_binding is not None
                self.assertEqual(
                    historical_binding["provenance_binding"]["package_version"],
                    "3.4.0",
                )

                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError,
                    "does not match soccer_predict.__version__",
                ):
                    forward_policy.validate_active_runtime_policy_manifest(
                        policy, repo_root=self.repo_root
                    )
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError,
                    "does not match soccer_predict.__version__",
                ):
                    forward_policy.load_active_binding(
                        base_dir=active_base,
                        repo_root=self.repo_root,
                        archived_at=start + timedelta(seconds=2),
                    )
                with mock.patch.object(
                    forward_policy, "_git", side_effect=self._clean_final_head
                ):
                    with self.assertRaisesRegex(
                        forward_policy.ForwardPolicyError,
                        "does not match soccer_predict.__version__",
                    ):
                        forward_policy.start_cohort(
                            base_dir=base / "new",
                            policy_file=self._write_policy(base / "new", policy),
                            cohort_id="must-not-reactivate-3-4",
                            cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                            starts_at=start,
                            repo_root=self.repo_root,
                        )

            resealed = deepcopy(binding)
            snapshot = resealed["policy_snapshot"]
            snapshot["software"]["package_version"] = "3.3.0"
            snapshot.pop("policy_hash")
            snapshot.pop("policy_id")
            snapshot["policy_hash"] = forward_policy._hash_json(snapshot)
            snapshot["policy_id"] = (
                "untouched-live-forward-"
                + snapshot["policy_hash"].split(":", 1)[1][:16]
            )
            resealed["policy_hash"] = snapshot["policy_hash"]
            resealed["policy_id"] = snapshot["policy_id"]
            resealed.pop("binding_hash")
            resealed["binding_hash"] = forward_policy._hash_json(resealed)
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "does not reproduce"
            ):
                forward_policy.validate_record_binding(resealed)

    def test_frozen_3_5_protected_file_contract_replays_under_3_6(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dataset, registry, corner_dataset, corner_registry, scope_file = (
                self._artifacts(base)
            )
            historical_protected_files = (
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
                "scripts/prediction_card_renderer.py",
                "scripts/review_card_renderer.py",
                "scripts/plain_text_formatter.py",
                "scripts/lineup_scheduler.py",
                "scripts/review_scheduler.py",
                "scripts/forward_policy.py",
                "scripts/forward_validation.py",
                "scripts/source_evidence.py",
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
            )
            historical_renderer_files = (
                "scripts/public_market_outlook.py",
                "scripts/prediction_card_renderer.py",
                "scripts/review_card_renderer.py",
                "scripts/plain_text_formatter.py",
            )
            with mock.patch.object(forward_policy, "SOCCER_PREDICT_VERSION", "3.5.0"):
                policy = forward_policy.build_policy_manifest(
                    repo_root=self.repo_root,
                    dataset_manifest=dataset,
                    model_registry=registry,
                    corner_dataset_manifest=corner_dataset,
                    corner_model_registry=corner_registry,
                    cohort_scope_file=scope_file,
                    expected_final_merge_commit="a" * 40,
                    cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                    created_at="2026-08-07T16:19:59Z",
                    code_commit="a" * 40,
                    protected_files=historical_protected_files,
                )

            self.assertEqual(policy["software"]["package_version"], "3.5.0")
            self.assertEqual(len(policy["code"]["protected_files"]), 30)
            for later_file in (
                "scripts/publication_outlook.py",
                "scripts/gate_stats.py",
                "references/plain-text-output.md",
            ):
                self.assertNotIn(later_file, policy["code"]["protected_files"])

            cohort_id = "literal-3-5-policy"
            runtime = policy["policy"]
            provenance = {
                "schema_version": forward_policy.PROVENANCE_SCHEMA_VERSION,
                "package_version": "3.5.0",
                "git_commit_sha": policy["code"]["commit"],
                "policy_hash": policy["policy_hash"],
                "validation_config_hash": forward_policy._hash_json(
                    runtime["validation_protocol"]
                ),
                "dataset_manifest_hash": policy["data"]["declared_manifest_hash"],
                "model_registry_hash": policy["models"]["declared_registry_hash"],
                "renderer_policy_hash": forward_policy._hash_json(
                    {
                        "display_policy": runtime["display_policy"],
                        "protected_renderer_files": {
                            path: policy["code"]["protected_files"][path]
                            for path in historical_renderer_files
                        },
                    }
                ),
                "cohort_id": cohort_id,
                "cohort_kind": forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                "assurance_scope": forward_policy.LOCAL_ASSURANCE_SCOPE,
                "promotion_evidence_eligible": False,
            }
            provenance["provenance_hash"] = forward_policy._hash_json(provenance)
            binding = {
                "schema_version": (
                    forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
                ),
                "cohort_id": cohort_id,
                "cohort_hash": "sha256:" + "5" * 64,
                "cohort_starts_at": "2026-08-07T16:20:11+00:00",
                "policy_id": policy["policy_id"],
                "policy_hash": policy["policy_hash"],
                "policy_snapshot": policy,
                "recorded_code_commit": policy["code"]["commit"],
                "archived_at": "2026-08-07T17:16:24+00:00",
                "cohort_kind": forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                "assurance_scope": forward_policy.LOCAL_ASSURANCE_SCOPE,
                "promotion_evidence_eligible": False,
                "provenance_binding": provenance,
                "observation_commitment_hash": "sha256:" + "9" * 64,
            }
            binding["binding_hash"] = forward_policy._hash_json(binding)

            validated_policy = forward_policy.validate_policy_manifest(policy)
            validated_provenance = forward_policy.validate_provenance_binding(
                provenance,
                policy_manifest=policy,
                cohort_id=cohort_id,
            )
            validated_binding = forward_policy.validate_record_binding(binding)
            self.assertEqual(validated_policy["software"]["package_version"], "3.5.0")
            self.assertEqual(validated_provenance, provenance)
            self.assertEqual(validated_binding, binding)
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError,
                "does not match soccer_predict.__version__",
            ):
                forward_policy.validate_active_runtime_policy_manifest(
                    policy, repo_root=self.repo_root
                )

            current_base = base / "current"
            current_base.mkdir()
            current_policy = self._policy(current_base)
            current_policy["code"]["protected_files"].pop(
                "scripts/publication_outlook.py"
            )
            current_policy = self._reseal_policy(current_policy)
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError,
                "scripts/publication_outlook.py",
            ):
                forward_policy.validate_policy_manifest(current_policy)

    def test_promotable_kind_fails_closed_without_required_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dataset, registry, corner_dataset, corner_registry, scope_file = (
                self._artifacts(base)
            )
            policy = self._policy(
                base, cohort_kind=forward_policy.PROMOTABLE_CONFIRMATION_KIND
            )
            self.assertEqual(
                forward_policy.validate_policy_manifest(policy)[
                    "confirmation_contract"
                ]["cohort_kind"],
                forward_policy.PROMOTABLE_CONFIRMATION_KIND,
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError,
                "external_timestamp_anchor_adapter.*baseline_artifact_replay_adapter"
                ".*entry_price_source_replay_adapter"
                ".*closing_price_source_replay_adapter",
            ):
                forward_policy.validate_active_runtime_policy_manifest(
                    policy, repo_root=self.repo_root
                )

            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "missing required adapters"
            ):
                forward_policy.freeze_policy(
                    base_dir=base / "freeze",
                    repo_root=self.repo_root,
                    dataset_manifest=dataset,
                    model_registry=registry,
                    corner_dataset_manifest=corner_dataset,
                    corner_model_registry=corner_registry,
                    cohort_scope_file=scope_file,
                    expected_final_merge_commit="a" * 40,
                    cohort_kind=forward_policy.PROMOTABLE_CONFIRMATION_KIND,
                )
            self.assertFalse(forward_policy.policy_directory(base / "freeze").exists())

            policy_file = self._write_policy(base / "start", policy)
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError, "missing required adapters"
                ):
                    forward_policy.start_cohort(
                        base_dir=base / "start",
                        policy_file=policy_file,
                        cohort_id="not-yet-promotable",
                        cohort_kind=forward_policy.PROMOTABLE_CONFIRMATION_KIND,
                        starts_at="2026-08-06T02:00:00Z",
                        repo_root=self.repo_root,
                    )

            caller_claimed = deepcopy(policy)
            caller_claimed["confirmation_contract"].update(
                {
                    adapter: True
                    for adapter in forward_policy.MISSING_PROMOTABLE_ADAPTERS
                }
            )
            caller_claimed.pop("policy_hash")
            caller_claimed.pop("policy_id")
            caller_claimed["policy_hash"] = forward_policy._hash_json(caller_claimed)
            caller_claimed["policy_id"] = (
                "untouched-live-forward-"
                + caller_claimed["policy_hash"].split(":", 1)[1][:16]
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError,
                "confirmation contract does not match runtime",
            ):
                forward_policy.validate_policy_manifest(caller_claimed)
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError,
                "confirmation contract does not match runtime",
            ):
                forward_policy.validate_active_runtime_policy_manifest(
                    caller_claimed, repo_root=self.repo_root
                )

    def test_cohort_kind_must_match_policy_and_is_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            policy = self._policy(base)
            mismatch_file = self._write_policy(base / "mismatch", policy)
            valid_file = self._write_policy(base / "valid", policy)
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError,
                    "does not match the frozen policy",
                ):
                    forward_policy.start_cohort(
                        base_dir=base / "mismatch",
                        policy_file=mismatch_file,
                        cohort_id="mismatched-kind",
                        cohort_kind=forward_policy.PROMOTABLE_CONFIRMATION_KIND,
                        starts_at="2026-08-06T02:00:00Z",
                        repo_root=self.repo_root,
                    )
                _path, cohort = forward_policy.start_cohort(
                    base_dir=base / "valid",
                    policy_file=valid_file,
                    cohort_id="local-shadow",
                    cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                    starts_at="2026-08-06T02:00:00Z",
                    repo_root=self.repo_root,
                )
            tampered = deepcopy(cohort)
            tampered["kind"] = forward_policy.PROMOTABLE_CONFIRMATION_KIND
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "cohort hash"
            ):
                forward_policy.validate_cohort(tampered)

            tampered.pop("cohort_hash")
            tampered["cohort_hash"] = forward_policy._hash_json(tampered)
            forward_policy._atomic_json(
                forward_policy.active_cohort_path(base / "valid"), tampered
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "missing required adapters"
            ):
                forward_policy.load_active_binding(
                    base_dir=base / "valid",
                    repo_root=self.repo_root,
                    archived_at="2026-08-06T02:01:00Z",
                )

    def test_previous_schema_and_kindless_cohort_are_historical_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            policy = self._previous_policy(base)
            forward_policy.validate_policy_manifest(policy)
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "cannot be activated"
            ):
                forward_policy.validate_active_runtime_policy_manifest(
                    policy, repo_root=self.repo_root
                )

            policy_file = self._write_policy(base / "new", policy)
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError, "only forward-policy/3.0.0"
                ):
                    forward_policy.start_cohort(
                        base_dir=base / "new",
                        policy_file=policy_file,
                        cohort_id="cannot-reactivate",
                        cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                        starts_at="2026-08-06T02:00:00Z",
                        repo_root=self.repo_root,
                    )

            legacy_cohort = {
                "schema_version": forward_policy.LEGACY_COHORT_SCHEMA_VERSION,
                "artifact_type": "soccer_untouched_live_forward_cohort",
                "cohort_id": "legacy-no-kind",
                "status": "active",
                "starts_at": "2026-08-06T02:00:00+00:00",
                "policy_file": str(policy_file),
                "policy_id": policy["policy_id"],
                "policy_hash": policy["policy_hash"],
                "retrospective_records_allowed": False,
                "closed_at": None,
            }
            legacy_cohort["cohort_hash"] = forward_policy._hash_json(legacy_cohort)
            forward_policy.validate_cohort(legacy_cohort)
            forward_policy._atomic_json(
                forward_policy.active_cohort_path(base / "legacy"), legacy_cohort
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "historical read-only"
            ):
                forward_policy.load_active_binding(
                    base_dir=base / "legacy",
                    repo_root=self.repo_root,
                    archived_at="2026-08-06T02:01:00Z",
                )

            legacy_with_kind = deepcopy(legacy_cohort)
            legacy_with_kind.pop("cohort_hash")
            legacy_with_kind["kind"] = forward_policy.LOCAL_INTEGRITY_SHADOW_KIND
            legacy_with_kind["cohort_hash"] = forward_policy._hash_json(
                legacy_with_kind
            )
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "legacy.*cannot carry"
            ):
                forward_policy.validate_cohort(legacy_with_kind)

    def test_freeze_requires_caller_confirmed_clean_final_merge_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dataset, registry, corner_dataset, corner_registry, scope_file = (
                self._artifacts(base)
            )
            with mock.patch.object(
                forward_policy,
                "_git",
                return_value="b" * 40,
            ):
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError, "final merge commit"
                ):
                    forward_policy.freeze_policy(
                        base_dir=base,
                        repo_root=self.repo_root,
                        dataset_manifest=dataset,
                        model_registry=registry,
                        corner_dataset_manifest=corner_dataset,
                        corner_model_registry=corner_registry,
                        cohort_scope_file=scope_file,
                        expected_final_merge_commit="a" * 40,
                        cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                    )

            def dirty_final_head(_root: Path, *arguments: str) -> str:
                if arguments == ("rev-parse", "HEAD"):
                    return "a" * 40
                if arguments == (
                    "status",
                    "--porcelain",
                    "--untracked-files=normal",
                ):
                    return " M scripts/forward_policy.py"
                raise AssertionError(arguments)

            with mock.patch.object(
                forward_policy, "_git", side_effect=dirty_final_head
            ):
                with self.assertRaisesRegex(
                    forward_policy.ForwardPolicyError, "uncommitted policy"
                ):
                    forward_policy.freeze_policy(
                        base_dir=base,
                        repo_root=self.repo_root,
                        dataset_manifest=dataset,
                        model_registry=registry,
                        corner_dataset_manifest=corner_dataset,
                        corner_model_registry=corner_registry,
                        cohort_scope_file=scope_file,
                        expected_final_merge_commit="a" * 40,
                        cohort_kind=forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
                    )

        parser = forward_policy.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "freeze",
                    "--dataset-manifest",
                    "manifest.json",
                    "--model-registry",
                    "registry.json",
                    "--expected-final-merge-commit",
                    "a" * 40,
                ]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "start",
                    "--policy-file",
                    "policy.json",
                    "--cohort-id",
                    "explicit-kind-required",
                ]
            )
        parsed = parser.parse_args(
            [
                "freeze",
                "--dataset-manifest",
                "manifest.json",
                "--model-registry",
                "registry.json",
                "--corner-dataset-manifest",
                "corner-manifest.json",
                "--corner-model-registry",
                "corner-registry.json",
                "--cohort-scope-file",
                "scope.json",
                "--expected-final-merge-commit",
                "a" * 40,
                "--cohort-kind",
                forward_policy.LOCAL_INTEGRITY_SHADOW_KIND,
            ]
        )
        self.assertEqual(parsed.cohort_kind, forward_policy.LOCAL_INTEGRITY_SHADOW_KIND)
        with self.assertRaises(SystemExit):
            parser.parse_args(["close", "--closed-at", "2026-08-07T00:00:00Z"])


if __name__ == "__main__":
    unittest.main()

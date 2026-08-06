from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import forward_policy


class ForwardPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]

    def _artifacts(self, base: Path) -> tuple[Path, Path]:
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
                }
            ),
            encoding="utf-8",
        )
        return dataset, registry

    def _policy(self, base: Path) -> dict:
        dataset, registry = self._artifacts(base)
        return forward_policy.build_policy_manifest(
            repo_root=self.repo_root,
            dataset_manifest=dataset,
            model_registry=registry,
            expected_final_merge_commit="a" * 40,
            created_at="2026-08-06T01:00:00Z",
            code_commit="a" * 40,
            protected_files=tuple(
                sorted(forward_policy.REQUIRED_PROVENANCE_PROTECTED_FILES)
            ),
        )

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
        self.assertEqual(
            validated["software"]["package_version"],
            forward_policy.SOCCER_PREDICT_VERSION,
        )
        self.assertEqual(validated["code"]["expected_final_merge_commit"], "a" * 40)
        self.assertTrue(
            forward_policy.REQUIRED_PROVENANCE_PROTECTED_FILES.issubset(
                validated["code"]["protected_files"]
            )
        )
        self.assertEqual(
            validated["policy"]["candidate_evaluation"]["schema_version"],
            "candidate-evaluation/2.0.0",
        )
        self.assertTrue(
            validated["policy"]["display_policy"]["top2_mass_and_remainder_required"]
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

    def test_policy_build_rejects_registry_without_dataset_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dataset, registry = self._artifacts(base)
            registry_payload = json.loads(registry.read_text(encoding="utf-8"))
            registry_payload.pop("dataset_manifest_hash")
            registry.write_text(json.dumps(registry_payload), encoding="utf-8")
            with self.assertRaisesRegex(
                forward_policy.ForwardPolicyError, "declare the selected dataset"
            ):
                forward_policy.build_policy_manifest(
                    repo_root=self.repo_root,
                    dataset_manifest=dataset,
                    model_registry=registry,
                    expected_final_merge_commit="a" * 40,
                    created_at="2026-08-06T01:00:00Z",
                    code_commit="a" * 40,
                    protected_files=tuple(
                        sorted(forward_policy.REQUIRED_PROVENANCE_PROTECTED_FILES)
                    ),
                )

    def test_cohort_rejects_retrospective_binding_and_preserves_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            policy = self._policy(base)
            policy_file = base / "policy.json"
            policy_file.write_text(
                json.dumps(policy, ensure_ascii=False), encoding="utf-8"
            )
            start = datetime(2026, 8, 6, 2, tzinfo=timezone.utc)
            with mock.patch.object(
                forward_policy, "_git", side_effect=self._clean_final_head
            ):
                _path, cohort = forward_policy.start_cohort(
                    base_dir=base,
                    policy_file=policy_file,
                    cohort_id="confirmation-2026-08",
                    starts_at=start,
                    repo_root=self.repo_root,
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
                )
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding["cohort_hash"], cohort["cohort_hash"])
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

            closure_path, closure = forward_policy.close_cohort(
                base_dir=base,
                closed_at=start + timedelta(hours=1),
            )
            self.assertTrue(closure_path.is_file())
            self.assertEqual(closure["cohort_hash"], cohort["cohort_hash"])
            self.assertEqual(
                forward_policy.validate_closure(closure, cohort=cohort)["closure_hash"],
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
            "untouched_confirmation_eligible": True,
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
                policy_file = base / "policy.json"
                policy_file.write_text(
                    json.dumps(policy, ensure_ascii=False), encoding="utf-8"
                )
                start = datetime(2026, 8, 6, 2, tzinfo=timezone.utc)
                with mock.patch.object(
                    forward_policy, "_git", side_effect=self._clean_final_head
                ):
                    forward_policy.start_cohort(
                        base_dir=base / "active",
                        policy_file=policy_file,
                        cohort_id="historical-3-4",
                        starts_at=start,
                        repo_root=self.repo_root,
                    )
                    binding = forward_policy.load_active_binding(
                        base_dir=base / "active",
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
                        base_dir=base / "active",
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
                            policy_file=policy_file,
                            cohort_id="must-not-reactivate-3-4",
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

    def test_freeze_requires_caller_confirmed_clean_final_merge_head(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dataset, registry = self._artifacts(base)
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
                        expected_final_merge_commit="a" * 40,
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
                        expected_final_merge_commit="a" * 40,
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
                ]
            )


if __name__ == "__main__":
    unittest.main()

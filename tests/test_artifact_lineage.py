from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from scripts import artifact_lineage


class ArtifactLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.semantic = mock.patch.object(
            artifact_lineage,
            "_normalized_semantic_verification",
            side_effect=self._semantic_receipt,
        )
        self.semantic.start()
        self.addCleanup(self.semantic.stop)

    @staticmethod
    def _semantic_receipt(role: str, *, registry_path: Path):
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        leagues = registry.get("leagues")
        count = len(leagues) if isinstance(leagues, (list, dict)) else 0
        return (
            f"test-{role}-verifier/1.0.0",
            {
                "registry_hash": registry["registry_hash"],
                "model_count": count,
                "status": "pass",
            },
        )

    def _write_artifacts(self, base: Path, *, league_keys: list[str] | None = None):
        keys = league_keys or ["test_league"]
        football_manifest = base / "football-manifest.json"
        football_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "football/1",
                    "bundle_hash": "sha256:" + "1" * 64,
                    "as_of_date": "2026-08-08",
                }
            ),
            encoding="utf-8",
        )
        football_registry = base / "football-registry.json"
        football_registry.write_text(
            json.dumps(
                {
                    "schema_version": "football-registry/1",
                    "registry_hash": "sha256:" + "2" * 64,
                    "dataset_manifest_hash": "sha256:" + "1" * 64,
                    "leagues": [
                        {
                            "league_key": league_key,
                            "model_hash": "sha256:" + "6" * 64,
                            "full_time_component_model_hash": "sha256:" + "7" * 64,
                        }
                        for league_key in keys
                    ],
                }
            ),
            encoding="utf-8",
        )
        corner_manifest = base / "corner-manifest.json"
        corner_manifest.write_text(
            json.dumps(
                {
                    "schema_version": "corner/1",
                    "bundle_hash": "sha256:" + "3" * 64,
                    "as_of_date": "2026-08-08",
                    "leagues": [
                        {
                            "league_key": league_key,
                            "dataset_sha256": "sha256:" + "4" * 64,
                        }
                        for league_key in keys
                    ],
                }
            ),
            encoding="utf-8",
        )
        corner_registry = base / "corner-registry.json"
        corner_registry.write_text(
            json.dumps(
                {
                    "schema_version": "corner-registry/1",
                    "registry_hash": "sha256:" + "5" * 64,
                    "dataset_hashes": {
                        league_key: "sha256:" + "4" * 64 for league_key in keys
                    },
                    "leagues": [
                        {
                            "league_key": league_key,
                            "model_hash": "sha256:" + "8" * 64,
                            "dataset_hash": "sha256:" + "4" * 64,
                            "source_lineage": {
                                "manifest_bundle_hash": "sha256:" + "3" * 64,
                                "dataset_hash": "sha256:" + "4" * 64,
                            },
                        }
                        for league_key in keys
                    ],
                }
            ),
            encoding="utf-8",
        )
        return {
            "data": {
                "football_history": football_manifest,
                "corner_history": corner_manifest,
            },
            "models": {
                "football_htft": football_registry,
                "corner": corner_registry,
            },
        }

    def test_builds_role_aware_lineage_and_replays_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write_artifacts(Path(temporary))
            lineage = artifact_lineage.build_lineage(
                repo_root=temporary,
                data_manifests=paths["data"],
                model_registries=paths["models"],
            )
            self.assertEqual(artifact_lineage.validate_lineage(lineage), lineage)
            self.assertEqual(
                artifact_lineage.verify_files(lineage, repo_root=temporary),
                lineage,
            )
            self.assertEqual(
                lineage["model_registries"]["corner"]["dataset_role"],
                "corner_history",
            )

    def test_public_golden_replays_all_19_registered_leagues_and_receipts(
        self,
    ) -> None:
        golden_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "forward_lineage_19_leagues.json"
        )
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        league_keys = golden["league_keys"]
        self.assertEqual(len(league_keys), golden["expected_model_count"])
        self.assertEqual(league_keys, sorted(set(league_keys)))
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write_artifacts(Path(temporary), league_keys=league_keys)
            lineage = artifact_lineage.build_lineage(
                repo_root=temporary,
                data_manifests=paths["data"],
                model_registries=paths["models"],
            )
            for role in artifact_lineage.MODEL_ROLES:
                self.assertEqual(
                    sorted(lineage["model_registries"][role]["registered_models"]),
                    league_keys,
                )
                receipt = lineage["semantic_verification_receipts"][role]
                self.assertEqual(receipt["model_count"], 19)
                self.assertEqual(
                    receipt["registry_hash"],
                    lineage["model_registries"][role]["declared_registry_hash"],
                )
            self.assertEqual(
                artifact_lineage.verify_files(lineage, repo_root=temporary),
                lineage,
            )

    def test_historical_1_1_mapping_shape_remains_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write_artifacts(Path(temporary))
            manifest_path = paths["data"]["corner_history"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["leagues"][0]["dataset_hash"] = manifest["leagues"][0].pop(
                "dataset_sha256"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            registry_path = paths["models"]["corner"]
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            item = registry["leagues"][0]
            item.pop("league_key")
            registry["leagues"] = {"test_league": item}
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            lineage = artifact_lineage._build_lineage(
                repo_root=temporary,
                data_manifests=paths["data"],
                model_registries=paths["models"],
                schema_version=artifact_lineage.LEGACY_SCHEMA_VERSION,
            )
            self.assertEqual(
                artifact_lineage.verify_files(lineage, repo_root=temporary), lineage
            )

    def test_current_schema_rejects_synthetic_corner_shapes_and_duplicate_keys(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write_artifacts(Path(temporary))
            registry_path = paths["models"]["corner"]
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            item = registry["leagues"][0]
            registry["leagues"] = {"test_league": item}
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaisesRegex(
                artifact_lineage.ArtifactLineageError, "leagues are missing"
            ):
                artifact_lineage.build_lineage(
                    repo_root=temporary,
                    data_manifests=paths["data"],
                    model_registries=paths["models"],
                )

            registry["leagues"] = [item, deepcopy(item)]
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaisesRegex(
                artifact_lineage.ArtifactLineageError, "league keys are invalid"
            ):
                artifact_lineage.build_lineage(
                    repo_root=temporary,
                    data_manifests=paths["data"],
                    model_registries=paths["models"],
                )

    def test_missing_role_and_cross_registry_substitution_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write_artifacts(Path(temporary))
            incomplete = dict(paths["models"])
            incomplete.pop("corner")
            with self.assertRaisesRegex(artifact_lineage.ArtifactLineageError, "roles"):
                artifact_lineage.build_lineage(
                    repo_root=temporary,
                    data_manifests=paths["data"],
                    model_registries=incomplete,
                )
            substituted = dict(paths["models"])
            substituted["corner"] = substituted["football_htft"]
            with self.assertRaisesRegex(
                artifact_lineage.ArtifactLineageError, "dataset_hashes"
            ):
                artifact_lineage.build_lineage(
                    repo_root=temporary,
                    data_manifests=paths["data"],
                    model_registries=substituted,
                )

    def test_tampered_candidate_role_policy_is_rejected_even_if_resealed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write_artifacts(Path(temporary))
            lineage = artifact_lineage.build_lineage(
                repo_root=temporary,
                data_manifests=paths["data"],
                model_registries=paths["models"],
            )
            tampered = deepcopy(lineage)
            tampered["candidate_role_policy"]["corner_total"] = [
                "football_history",
                "football_htft",
            ]
            tampered.pop("lineage_hash")
            tampered["lineage_hash"] = artifact_lineage._hash_json(tampered)
            with self.assertRaisesRegex(
                artifact_lineage.ArtifactLineageError, "role policy"
            ):
                artifact_lineage.validate_lineage(tampered)

    def test_resealed_registered_model_map_must_reproduce_registry_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write_artifacts(Path(temporary))
            lineage = artifact_lineage.build_lineage(
                repo_root=temporary,
                data_manifests=paths["data"],
                model_registries=paths["models"],
            )
            attacked = deepcopy(lineage)
            attacked["model_registries"]["football_htft"]["registered_models"][
                "test_league"
            ]["model_hash"] = "sha256:" + "9" * 64
            attacked.pop("lineage_hash")
            attacked["lineage_hash"] = artifact_lineage._hash_json(attacked)
            self.assertEqual(
                artifact_lineage.validate_lineage(attacked),
                attacked,
            )
            with self.assertRaisesRegex(
                artifact_lineage.ArtifactLineageError,
                "does not reproduce",
            ):
                artifact_lineage.verify_files(attacked, repo_root=temporary)


if __name__ == "__main__":
    unittest.main()

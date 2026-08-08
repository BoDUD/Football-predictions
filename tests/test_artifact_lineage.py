from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts import artifact_lineage


class ArtifactLineageTests(unittest.TestCase):
    def _write_artifacts(self, base: Path):
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
                            "league_key": "test_league",
                            "model_hash": "sha256:" + "6" * 64,
                            "full_time_component_model_hash": "sha256:" + "7" * 64,
                        }
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
                            "league_key": "test_league",
                            "dataset_hash": "sha256:" + "4" * 64,
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
                    "schema_version": "corner-registry/1",
                    "registry_hash": "sha256:" + "5" * 64,
                    "dataset_hashes": {"test_league": "sha256:" + "4" * 64},
                    "leagues": {
                        "test_league": {
                            "model_hash": "sha256:" + "8" * 64,
                            "dataset_hash": "sha256:" + "4" * 64,
                            "source_lineage": {
                                "manifest_bundle_hash": "sha256:" + "3" * 64,
                                "dataset_hash": "sha256:" + "4" * 64,
                            },
                        }
                    },
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

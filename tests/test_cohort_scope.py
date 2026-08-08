from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from scripts import cohort_scope


class CohortScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope = cohort_scope.build_scope(
            scope_id="requested-fixtures-20260808",
            competition_keys=["england-premier-league", "korea-k1"],
            starts_at="2026-08-08T00:00:00Z",
        )

    def _append(self, base: Path, event_type: str, fixture_id: str, **kwargs):
        self._write_active_binding(base)
        return cohort_scope.append_event(
            base_dir=base,
            cohort_id="cohort-a",
            scope=self.scope,
            event_type=event_type,
            fixture_id=fixture_id,
            competition_key=kwargs.pop("competition_key", "korea-k1"),
            home_team=kwargs.pop("home_team", "Home"),
            away_team=kwargs.pop("away_team", "Away"),
            kickoff=kwargs.pop("kickoff", "2026-08-08T12:00:00Z"),
            occurred_at=kwargs.pop("occurred_at", "2026-08-08T01:00:00Z"),
            **kwargs,
        )

    def _write_active_binding(self, base: Path) -> None:
        policy_directory = base / ".codex" / "soccer-predict" / "forward-policies"
        policy_directory.mkdir(parents=True, exist_ok=True)
        policy = {
            "policy_id": "untouched-live-forward-0123456789abcdef",
            "policy_hash": "sha256:" + "a" * 64,
            "artifact_lineage": {
                "model_registries": {
                    "football_htft": {
                        "registered_models": {
                            "korea-k1": {
                                "model_hash": "sha256:" + "b" * 64,
                                "full_time_component_model_hash": "sha256:" + "c" * 64,
                            }
                        }
                    }
                }
            },
        }
        policy_path = policy_directory / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        cohort = {
            "schema_version": "live-forward-cohort/2.0.0",
            "artifact_type": "soccer_untouched_live_forward_cohort",
            "cohort_id": "cohort-a",
            "kind": "local-integrity-shadow-v2",
            "status": "active",
            "starts_at": "2026-08-08T00:00:00+00:00",
            "policy_file": str(policy_path.resolve()),
            "policy_id": policy["policy_id"],
            "policy_hash": policy["policy_hash"],
            "retrospective_records_allowed": False,
            "closed_at": None,
            "scope_id": self.scope["scope_id"],
            "scope_hash": self.scope["scope_hash"],
        }
        cohort["cohort_hash"] = cohort_scope._hash_json(cohort)
        active = base / ".codex" / "soccer-predict" / "active-forward-cohort.json"
        active.parent.mkdir(parents=True, exist_ok=True)
        if not active.exists():
            active.write_text(json.dumps(cohort), encoding="utf-8")

    def _cohort_binding(self, base: Path) -> dict:
        self._write_active_binding(base)
        active = json.loads(
            (
                base / ".codex" / "soccer-predict" / "active-forward-cohort.json"
            ).read_text(encoding="utf-8")
        )
        return {
            "cohort_hash": active["cohort_hash"],
            "policy_id": active["policy_id"],
            "policy_hash": active["policy_hash"],
            "starts_at": active["starts_at"],
        }

    @staticmethod
    def _record_manifest(
        *fixture_ids: str, request_hashes: dict[str, str] | None = None
    ) -> dict:
        hashes = request_hashes or {}
        return {
            "records": [
                {
                    "fixture_id": fixture_id,
                    "request_event_hash": hashes.get(fixture_id),
                }
                for fixture_id in fixture_ids
            ]
        }

    def test_scope_is_content_addressed_and_rejects_tampering(self) -> None:
        self.assertEqual(cohort_scope.validate_scope(self.scope), self.scope)
        tampered = deepcopy(self.scope)
        tampered["competition_keys"].append("spain-la-liga")
        with self.assertRaisesRegex(cohort_scope.CohortScopeError, "hash"):
            cohort_scope.validate_scope(tampered)

    def test_request_log_is_append_only_hash_chained_and_binds_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            request = self._append(base, "requested", "2910001")
            binding = cohort_scope.request_binding(
                base_dir=base,
                cohort_id="cohort-a",
                scope=self.scope,
                fixture_id="2910001",
            )
            self.assertEqual(binding["request_event_hash"], request["event_hash"])
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError, "more than once"
            ):
                self._append(
                    base,
                    "requested",
                    "2910001",
                    occurred_at="2026-08-08T01:01:00Z",
                )

    def test_closure_requires_record_or_explicit_unavailable_for_every_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            self._append(base, "requested", "2910001")
            self._append(
                base,
                "requested",
                "2910002",
                occurred_at="2026-08-08T01:01:00Z",
            )
            binding = self._cohort_binding(base)
            events = cohort_scope.load_events(
                base,
                "cohort-a",
                scope=self.scope,
                cohort_binding=binding,
            )
            request_hashes = {
                event["fixture"]["fixture_id"]: event["event_hash"]
                for event in events
                if event["event_type"] == "requested"
            }
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError, "request binding"
            ):
                cohort_scope.build_denominator(
                    scope=self.scope,
                    cohort_id="cohort-a",
                    events=events,
                    record_manifest=self._record_manifest(
                        "2910001",
                        request_hashes={"2910001": "sha256:" + "f" * 64},
                    ),
                    cohort_binding=binding,
                )
            with self.assertRaisesRegex(cohort_scope.CohortScopeError, "unresolved"):
                cohort_scope.build_denominator(
                    scope=self.scope,
                    cohort_id="cohort-a",
                    events=events,
                    record_manifest=self._record_manifest(
                        "2910001", request_hashes=request_hashes
                    ),
                    cohort_binding=binding,
                )
            unavailable = self._append(
                base,
                "unavailable",
                "2910002",
                occurred_at="2026-08-08T01:02:00Z",
                reason="source_unavailable",
            )
            denominator = cohort_scope.build_denominator(
                scope=self.scope,
                cohort_id="cohort-a",
                events=cohort_scope.load_events(
                    base,
                    "cohort-a",
                    scope=self.scope,
                    cohort_binding=binding,
                ),
                record_manifest=self._record_manifest(
                    "2910001", request_hashes=request_hashes
                ),
                cohort_binding=binding,
            )
            self.assertTrue(denominator["complete"])
            self.assertEqual(denominator["requested_fixture_count"], 2)
            self.assertEqual(denominator["recorded_fixture_count"], 1)
            self.assertEqual(denominator["unavailable_fixture_count"], 1)
            self.assertEqual(
                denominator["entries"][1]["unavailable_event_hash"],
                unavailable["event_hash"],
            )
            self.assertEqual(
                cohort_scope.validate_denominator(
                    denominator, scope=self.scope, cohort_id="cohort-a"
                ),
                denominator,
            )

    def test_unavailable_without_request_and_post_kickoff_event_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            with self.assertRaisesRegex(cohort_scope.CohortScopeError, "no prior"):
                self._append(
                    base,
                    "unavailable",
                    "2910001",
                    reason="fixture_not_found",
                )
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError, "before kickoff"
            ):
                self._append(
                    base,
                    "requested",
                    "2910001",
                    occurred_at="2026-08-08T12:00:00Z",
                )
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError,
                "outside the frozen scope window|predates the active cohort",
            ):
                self._append(
                    base,
                    "requested",
                    "2910002",
                    occurred_at="2026-08-07T23:59:59Z",
                )

    def test_request_binding_freezes_full_fixture_and_explicit_reschedule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            request = self._append(base, "requested", "2910001")
            original = deepcopy(request["fixture"])
            rescheduled = self._append(
                base,
                "rescheduled",
                "2910001",
                occurred_at="2026-08-08T01:02:00Z",
                kickoff="2026-08-08T13:00:00Z",
            )
            binding = cohort_scope.request_binding(
                base_dir=base,
                cohort_id="cohort-a",
                scope=self.scope,
                fixture_id="2910001",
                expected_fixture=rescheduled["fixture"],
            )
            self.assertEqual(binding["request_fixture_id"], "2910001")
            self.assertEqual(binding["request_event_hash"], request["event_hash"])
            self.assertEqual(binding["fixture_event_hash"], rescheduled["event_hash"])
            self.assertEqual(binding["fixture"], rescheduled["fixture"])
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError, "does not match"
            ):
                cohort_scope.request_binding(
                    base_dir=base,
                    cohort_id="cohort-a",
                    scope=self.scope,
                    fixture_id="2910001",
                    expected_fixture=original,
                )

    def test_replacement_preserves_request_root_and_requires_new_fixture_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            request = self._append(base, "requested", "2910001")
            replacement = {
                "fixture_id": "2910999",
                "competition_key": "korea-k1",
                "home_team": "New Home",
                "away_team": "New Away",
                "kickoff": "2026-08-08T14:00:00Z",
            }
            replaced = self._append(
                base,
                "replaced",
                "2910001",
                occurred_at="2026-08-08T01:03:00Z",
                replacement_fixture=replacement,
            )
            binding = cohort_scope.request_binding(
                base_dir=base,
                cohort_id="cohort-a",
                scope=self.scope,
                fixture_id="2910999",
                expected_fixture=replaced["replacement_fixture"],
            )
            self.assertEqual(binding["request_fixture_id"], "2910001")
            self.assertEqual(binding["request_event_hash"], request["event_hash"])
            self.assertEqual(binding["fixture_event_hash"], replaced["event_hash"])
            self.assertEqual(binding["fixture"], replaced["replacement_fixture"])
            with self.assertRaisesRegex(cohort_scope.CohortScopeError, "exactly one"):
                cohort_scope.request_binding(
                    base_dir=base,
                    cohort_id="cohort-a",
                    scope=self.scope,
                    fixture_id="2910001",
                )

    def test_independent_model_unavailable_must_match_frozen_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            self._append(base, "requested", "2910001")
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError, "contradicts the frozen"
            ):
                self._append(
                    base,
                    "unavailable",
                    "2910001",
                    occurred_at="2026-08-08T01:02:00Z",
                    reason="independent_model_unavailable",
                )

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            self._append(
                base,
                "requested",
                "2910002",
                competition_key="england-premier-league",
            )
            unavailable = self._append(
                base,
                "unavailable",
                "2910002",
                competition_key="england-premier-league",
                occurred_at="2026-08-08T01:02:00Z",
                reason="independent_model_unavailable",
            )
            self.assertEqual(unavailable["reason"], "independent_model_unavailable")
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError, "terminally unavailable"
            ):
                cohort_scope.request_binding(
                    base_dir=base,
                    cohort_id="cohort-a",
                    scope=self.scope,
                    fixture_id="2910002",
                )

    def test_current_events_cannot_self_bind_or_repackage_policy_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            self._append(base, "requested", "2910001")
            path = cohort_scope.denominator_event_path(base, "cohort-a")
            raw = [json.loads(line) for line in path.read_text().splitlines()]
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError, "immutable cohort binding"
            ):
                cohort_scope.validate_events(
                    raw, scope=self.scope, cohort_id="cohort-a"
                )

            attacked = deepcopy(raw)
            attacked[0]["policy_hash"] = "sha256:" + "f" * 64
            attacked[0].pop("event_hash")
            attacked[0]["event_hash"] = cohort_scope._hash_json(attacked[0])
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError, "does not replay"
            ):
                cohort_scope.validate_events(
                    attacked,
                    scope=self.scope,
                    cohort_id="cohort-a",
                    cohort_binding=self._cohort_binding(base),
                )

    def test_fixture_event_times_are_monotonic_and_precede_new_kickoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            self._append(
                base,
                "requested",
                "2910001",
                occurred_at="2026-08-08T01:00:00.123456Z",
            )
            binding = cohort_scope.request_binding(
                base_dir=base,
                cohort_id="cohort-a",
                scope=self.scope,
                fixture_id="2910001",
            )
            self.assertEqual(
                binding["fixture_event_at"], "2026-08-08T01:00:00.123456+00:00"
            )
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError, "cannot move backwards"
            ):
                self._append(
                    base,
                    "rescheduled",
                    "2910001",
                    occurred_at="2026-08-08T01:00:00.123455Z",
                    kickoff="2026-08-08T13:00:00Z",
                )
            with self.assertRaisesRegex(
                cohort_scope.CohortScopeError, "before the rescheduled kickoff"
            ):
                self._append(
                    base,
                    "rescheduled",
                    "2910001",
                    occurred_at="2026-08-08T13:00:00Z",
                    kickoff="2026-08-08T13:00:00Z",
                )

    def test_current_closure_contract_rejects_legacy_event_schema(self) -> None:
        historical = cohort_scope._event_without_hash(
            schema_version=cohort_scope.PREVIOUS_EVENT_SCHEMA_VERSION,
            event_type="requested",
            cohort_id="cohort-a",
            scope=self.scope,
            fixture_id="2910001",
            competition_key="korea-k1",
            home_team="Home",
            away_team="Away",
            kickoff="2026-08-08T12:00:00Z",
            occurred_at="2026-08-08T01:00:00Z",
            previous_event_hash=None,
        )
        historical["event_hash"] = cohort_scope._hash_json(historical)
        with self.assertRaisesRegex(
            cohort_scope.CohortScopeError, "frozen release contract"
        ):
            cohort_scope.validate_events(
                [historical],
                scope=self.scope,
                cohort_id="cohort-a",
                cohort_binding={
                    "cohort_hash": "sha256:" + "a" * 64,
                    "policy_id": "untouched-live-forward-0123456789abcdef",
                    "policy_hash": "sha256:" + "b" * 64,
                    "starts_at": "2026-08-08T00:00:00Z",
                },
                required_schema_version=cohort_scope.EVENT_SCHEMA_VERSION,
            )


if __name__ == "__main__":
    unittest.main()

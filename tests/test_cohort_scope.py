from __future__ import annotations

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
        return cohort_scope.append_event(
            base_dir=base,
            cohort_id="cohort-a",
            scope=self.scope,
            event_type=event_type,
            fixture_id=fixture_id,
            competition_key="korea-k1",
            home_team="Home",
            away_team="Away",
            kickoff="2026-08-08T12:00:00Z",
            occurred_at=kwargs.pop("occurred_at", "2026-08-08T01:00:00Z"),
            **kwargs,
        )

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
            events = cohort_scope.load_events(base, "cohort-a", scope=self.scope)
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
                )
            with self.assertRaisesRegex(cohort_scope.CohortScopeError, "unresolved"):
                cohort_scope.build_denominator(
                    scope=self.scope,
                    cohort_id="cohort-a",
                    events=events,
                    record_manifest=self._record_manifest(
                        "2910001", request_hashes=request_hashes
                    ),
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
                events=cohort_scope.load_events(base, "cohort-a", scope=self.scope),
                record_manifest=self._record_manifest(
                    "2910001", request_hashes=request_hashes
                ),
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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import execution_evidence, source_evidence


class ExecutionEvidenceTests(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        identity = {
            "family": "total",
            "period": "full_time",
            "line": 2.5,
            "price_outcomes": ["over", "under"],
        }
        source = root / "accepted-offer.json"
        source.write_text(
            json.dumps(
                {
                    "schema_version": execution_evidence.RAW_SCHEMA_VERSION,
                    "source_url": "https://bookmaker.example/betslip/receipt-1",
                    "receipt_id": "receipt-1",
                    "fixture": {
                        "match_id": "123",
                        "home_team": "Home",
                        "away_team": "Away",
                        "kickoff": "2026-08-08T12:00:00Z",
                    },
                    "market_identity": identity,
                    "market_identity_hash": source_evidence.market_identity_hash(
                        identity
                    ),
                    "selection": "over",
                    "firm": {
                        "firm_id": "bookmaker-x",
                        "firm_name": "Bookmaker X",
                        "account_region": "JP",
                    },
                    "quoted_at": "2026-08-08T11:00:00Z",
                    "accepted_at": "2026-08-08T11:01:00Z",
                    "quoted_decimal_odds": 1.95,
                    "accepted_decimal_odds": 1.93,
                    "max_stake_units": 5.0,
                    "stake_units": 1.0,
                }
            ),
            encoding="utf-8",
        )
        return source

    def test_build_replay_and_match_firm_offer(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path, evidence = execution_evidence.build_evidence(
                self._source(root), output_dir=root / "evidence"
            )
            self.assertEqual(evidence, execution_evidence.validate_evidence_file(path))
            matched = execution_evidence.match_offer(
                evidence,
                fixture=evidence["fixture"],
                market_identity=evidence["market_identity"],
                selection="over",
                accepted_at="2026-08-08T11:01:00Z",
                accepted_decimal_odds=1.93,
                stake_units=1.0,
            )
            self.assertEqual("bookmaker-x", matched["firm"]["firm_id"])

    def test_tampered_raw_offer_fails_replay(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path, evidence = execution_evidence.build_evidence(
                self._source(root), output_dir=root / "evidence"
            )
            raw_offer = path.parent / evidence["source"]["raw_offer_path"]
            value = json.loads(raw_offer.read_text(encoding="utf-8"))
            value["accepted_decimal_odds"] = 1.94
            raw_offer.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                execution_evidence.ExecutionEvidenceError, "replay|path|offer"
            ):
                execution_evidence.validate_evidence_file(path)

    def test_rejects_stake_above_firm_limit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._source(root)
            value = json.loads(source.read_text(encoding="utf-8"))
            value["stake_units"] = 6.0
            source.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(
                execution_evidence.ExecutionEvidenceError, "exceeds"
            ):
                execution_evidence.build_evidence(source, output_dir=root / "evidence")


if __name__ == "__main__":
    unittest.main()

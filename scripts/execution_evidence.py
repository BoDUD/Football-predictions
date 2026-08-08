#!/usr/bin/env python3
"""Build replayable firm-specific accepted-offer evidence for forward ROI."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

try:
    from scripts import source_evidence
except ImportError:  # Direct execution from scripts/.
    import source_evidence  # type: ignore[no-redef]

RAW_SCHEMA_VERSION = "execution-offer-capture/1.0.0"
EVIDENCE_SCHEMA_VERSION = "execution-offer-evidence/1.0.0"
PARSER_VERSION = "execution-offer-parser/1.0.0"


class ExecutionEvidenceError(ValueError):
    """Raised when a firm offer cannot be replayed exactly."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _hash_json(value: Any) -> str:
    return _hash_bytes(_canonical(value))


def _text(value: Any, label: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ExecutionEvidenceError(f"{label} is required")
    return text


def _token(value: Any, label: str) -> str:
    text = _text(value, label)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", text):
        raise ExecutionEvidenceError(f"{label} must be a portable identifier")
    return text


def _aware(value: Any, label: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ExecutionEvidenceError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionEvidenceError(f"{label} must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _positive(value: Any, label: str, *, above_one: bool = False) -> float:
    if isinstance(value, bool):
        raise ExecutionEvidenceError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionEvidenceError(f"{label} must be numeric") from exc
    minimum = 1.0 if above_one else 0.0
    if not math.isfinite(number) or number <= minimum:
        comparison = "exceed 1.0" if above_one else "be positive"
        raise ExecutionEvidenceError(f"{label} must {comparison}")
    return number


def _https(value: Any, label: str) -> str:
    text = _text(value, label)
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ExecutionEvidenceError(f"{label} must be an https URL")
    return text


def parse_capture(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExecutionEvidenceError("execution capture is not UTF-8 JSON") from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != RAW_SCHEMA_VERSION
    ):
        raise ExecutionEvidenceError("execution capture schema_version is unsupported")
    fixture = value.get("fixture")
    if not isinstance(fixture, Mapping):
        raise ExecutionEvidenceError("execution capture fixture is missing")
    kickoff = _aware(fixture.get("kickoff"), "fixture.kickoff")
    quoted_at = _aware(value.get("quoted_at"), "quoted_at")
    accepted_at = _aware(value.get("accepted_at"), "accepted_at")
    if quoted_at > accepted_at or accepted_at >= kickoff:
        raise ExecutionEvidenceError(
            "execution offer must be quoted, accepted, and frozen before kickoff"
        )
    identity = source_evidence.canonical_market_identity(
        value.get("market_identity"), label="market_identity"
    )
    identity_hash = source_evidence.market_identity_hash(identity)
    if value.get("market_identity_hash") != identity_hash:
        raise ExecutionEvidenceError("execution market_identity_hash is invalid")
    selection = _text(value.get("selection"), "selection")
    if selection not in identity["price_outcomes"]:
        raise ExecutionEvidenceError("execution selection is not in the market")
    quoted_price = _positive(
        value.get("quoted_decimal_odds"), "quoted_decimal_odds", above_one=True
    )
    accepted_price = _positive(
        value.get("accepted_decimal_odds"), "accepted_decimal_odds", above_one=True
    )
    if accepted_price > quoted_price + 1e-12:
        raise ExecutionEvidenceError(
            "accepted price cannot improve the frozen firm quote"
        )
    maximum = _positive(value.get("max_stake_units"), "max_stake_units")
    stake = _positive(value.get("stake_units"), "stake_units")
    if stake > maximum + 1e-12:
        raise ExecutionEvidenceError("accepted stake exceeds the frozen firm limit")
    firm = value.get("firm")
    if not isinstance(firm, Mapping):
        raise ExecutionEvidenceError("execution firm is missing")
    return {
        "schema_version": RAW_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "capture_kind": (
            "bookmaker_accepted_offer_or_betslip_export_local_integrity_only"
        ),
        "source_url": _https(value.get("source_url"), "source_url"),
        "receipt_id": _token(value.get("receipt_id"), "receipt_id"),
        "fixture": {
            "match_id": _token(fixture.get("match_id"), "fixture.match_id"),
            "home_team": _text(fixture.get("home_team"), "fixture.home_team"),
            "away_team": _text(fixture.get("away_team"), "fixture.away_team"),
            "kickoff": kickoff.isoformat(),
        },
        "market_identity": identity,
        "market_identity_hash": identity_hash,
        "selection": selection,
        "firm": {
            "firm_id": _token(firm.get("firm_id"), "firm.firm_id"),
            "firm_name": _text(firm.get("firm_name"), "firm.firm_name"),
            "account_region": _text(firm.get("account_region"), "firm.account_region"),
        },
        "quoted_at": quoted_at.isoformat(),
        "accepted_at": accepted_at.isoformat(),
        "quoted_decimal_odds": quoted_price,
        "accepted_decimal_odds": accepted_price,
        "max_stake_units": maximum,
        "stake_units": stake,
        "stake_unit": "u",
        "offer_status": "accepted",
        "limit_verified": True,
    }


def _entry(raw: bytes, relative_path: str) -> dict[str, Any]:
    return {
        "raw_offer_path": relative_path,
        "raw_offer_sha256": _hash_bytes(raw),
        "raw_offer_bytes": len(raw),
        "parsed": parse_capture(raw),
    }


def _rebuild(entry: Mapping[str, Any]) -> dict[str, Any]:
    parsed = entry["parsed"]
    value: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "artifact_type": "soccer_replayable_execution_offer",
        "parser_version": PARSER_VERSION,
        "fixture": parsed["fixture"],
        "market_identity": parsed["market_identity"],
        "market_identity_hash": parsed["market_identity_hash"],
        "selection": parsed["selection"],
        "accepted_at": parsed["accepted_at"],
        "firm": parsed["firm"],
        "offer": {
            "quoted_at": parsed["quoted_at"],
            "quoted_decimal_odds": parsed["quoted_decimal_odds"],
            "accepted_decimal_odds": parsed["accepted_decimal_odds"],
            "max_stake_units": parsed["max_stake_units"],
            "stake_units": parsed["stake_units"],
            "stake_unit": parsed["stake_unit"],
            "limit_verified": parsed["limit_verified"],
            "receipt_id": parsed["receipt_id"],
            "source_url": parsed["source_url"],
        },
        "source": dict(entry),
        "replay_policy": "reparse_content_addressed_firm_offer",
        "assurance_scope": "local_integrity_only_no_external_timestamp",
    }
    value["evidence_hash"] = _hash_json(value)
    return value


def build_evidence(
    source_file: str | Path, *, output_dir: str | Path
) -> tuple[Path, dict[str, Any]]:
    raw = Path(source_file).resolve().read_bytes()
    target = Path(output_dir).resolve()
    raw_dir = target / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    digest = _hash_bytes(raw).split(":", 1)[1]
    relative = f"raw/{digest}.json"
    raw_path = target / relative
    if raw_path.exists() and raw_path.read_bytes() != raw:
        raise ExecutionEvidenceError("content-addressed execution source collision")
    if not raw_path.exists():
        raw_path.write_bytes(raw)
    evidence = _rebuild(_entry(raw, relative))
    suffix = hashlib.sha256(
        f"{evidence['market_identity_hash']}:{evidence['selection']}".encode()
    ).hexdigest()[:16]
    output = target / f"{evidence['fixture']['match_id']}-{suffix}-execution.json"
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if output.exists():
        if validate_evidence_file(output) != evidence:
            raise ExecutionEvidenceError(
                "different execution evidence already exists for this identity"
            )
    else:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
    return output, evidence


def validate_evidence_file(path: str | Path) -> dict[str, Any]:
    evidence_path = Path(path).resolve()
    try:
        value = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExecutionEvidenceError(
            "execution evidence is unavailable or invalid"
        ) from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != EVIDENCE_SCHEMA_VERSION
    ):
        raise ExecutionEvidenceError("execution evidence schema_version is unsupported")
    supplied = value.get("evidence_hash")
    without_hash = dict(value)
    without_hash.pop("evidence_hash", None)
    if supplied != _hash_json(without_hash):
        raise ExecutionEvidenceError("execution evidence hash is invalid")
    source = value.get("source")
    if not isinstance(source, Mapping):
        raise ExecutionEvidenceError("execution evidence source is missing")
    relative = Path(str(source.get("raw_offer_path") or ""))
    if (
        relative.is_absolute()
        or relative.parts[:1] != ("raw",)
        or ".." in relative.parts
    ):
        raise ExecutionEvidenceError("execution raw offer path is unsafe")
    raw_root = (evidence_path.parent / "raw").resolve()
    raw_path = (evidence_path.parent / relative).resolve()
    try:
        raw_path.relative_to(raw_root)
        raw = raw_path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ExecutionEvidenceError("execution raw offer is unavailable") from exc
    digest = _hash_bytes(raw).split(":", 1)[1]
    expected = _entry(raw, f"raw/{digest}.json")
    if dict(source) != expected:
        raise ExecutionEvidenceError("execution raw offer does not replay")
    rebuilt = _rebuild(expected)
    if rebuilt != value:
        raise ExecutionEvidenceError("execution evidence bundle does not replay")
    return dict(value)


def match_offer(
    evidence: Mapping[str, Any],
    *,
    fixture: Mapping[str, Any],
    market_identity: Mapping[str, Any],
    selection: str,
    accepted_at: str,
    accepted_decimal_odds: float,
    stake_units: float,
) -> dict[str, Any]:
    identity = source_evidence.canonical_market_identity(market_identity)
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise ExecutionEvidenceError("execution offer schema is unsupported")
    if evidence.get("fixture") != dict(fixture):
        raise ExecutionEvidenceError("execution offer fixture does not match")
    if (
        evidence.get("market_identity") != identity
        or evidence.get("market_identity_hash")
        != source_evidence.market_identity_hash(identity)
        or evidence.get("selection") != selection
        or evidence.get("accepted_at") != _aware(accepted_at, "accepted_at").isoformat()
    ):
        raise ExecutionEvidenceError("execution offer identity does not match")
    offer = evidence.get("offer")
    if not isinstance(offer, Mapping):
        raise ExecutionEvidenceError("execution offer details are missing")
    if not math.isclose(
        float(offer.get("accepted_decimal_odds")),
        float(accepted_decimal_odds),
        rel_tol=0.0,
        abs_tol=1e-12,
    ) or not math.isclose(
        float(offer.get("stake_units")),
        float(stake_units),
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ExecutionEvidenceError("execution accepted price/stake does not match")
    return {
        "execution_evidence_hash": evidence["evidence_hash"],
        "firm": evidence["firm"],
        "offer": dict(offer),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--source-file", required=True)
    build.add_argument("--output-dir", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--evidence", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "build":
            path, artifact = build_evidence(
                args.source_file, output_dir=args.output_dir
            )
        else:
            path = Path(args.evidence).resolve()
            artifact = validate_evidence_file(path)
        print(
            json.dumps(
                {"ok": True, "path": str(path), "artifact": artifact},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (ExecutionEvidenceError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

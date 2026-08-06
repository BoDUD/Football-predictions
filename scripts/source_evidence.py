#!/usr/bin/env python3
"""Build replayable source evidence from visible pre-kickoff market snapshots.

The adapter consumes exported visible-page JSON rather than undocumented/private APIs.  It
stores each source content-addressed, replays the parser from those bytes, verifies fixture
and timing identity, and derives consensus/median prices from the frozen bookmaker rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

RAW_SCHEMA_VERSION = "visible-market-snapshot/1.0.0"
EVIDENCE_SCHEMA_VERSION = "source-evidence/1.0.0"
PARSER_VERSION = "visible-market-parser/1.0.0"
SUPPORTED_MARKETS = {
    "1x2",
    "asian",
    "total",
    "half_time",
    "htft",
    "goal_range",
    "btts",
    "corner_total",
    "corner_handicap",
}
ALLOWED_SOURCE_HOSTS = {
    "zq.titan007.com",
    "live.titan007.com",
    "m.titan007.com",
    "www.espn.com",
    "www.sofascore.com",
}


class SourceEvidenceError(ValueError):
    """Raised when a raw market snapshot is not safely replayable."""


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


def _aware(value: Any, label: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SourceEvidenceError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceEvidenceError(f"{label} must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _text(value: Any, label: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise SourceEvidenceError(f"{label} is required")
    return text


def _source_url(value: Any) -> str:
    url = _text(value, "source_url")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SourceEvidenceError("source_url must be an https URL")
    host = parsed.hostname.lower()
    if host not in ALLOWED_SOURCE_HOSTS and not host.endswith(".titan007.com"):
        raise SourceEvidenceError(f"source host is not registered: {host}")
    return url


def _finite_price(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise SourceEvidenceError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceEvidenceError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise SourceEvidenceError(f"{label} must be finite and positive")
    return number


def parse_raw_snapshot(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceEvidenceError("raw snapshot is not UTF-8 JSON") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != RAW_SCHEMA_VERSION
    ):
        raise SourceEvidenceError("raw snapshot schema_version is unsupported")
    fixture = payload.get("fixture")
    if not isinstance(fixture, Mapping):
        raise SourceEvidenceError("raw snapshot fixture is missing")
    kickoff = _aware(fixture.get("kickoff"), "fixture.kickoff")
    collected = _aware(payload.get("collected_at"), "collected_at")
    source_url = _source_url(payload.get("source_url"))
    parsed_url = urlparse(source_url)
    if collected >= kickoff:
        raise SourceEvidenceError("source snapshot was not collected before kickoff")
    normalized_fixture = {
        "match_id": _text(fixture.get("match_id"), "fixture.match_id"),
        "home_team": _text(fixture.get("home_team"), "fixture.home_team"),
        "away_team": _text(fixture.get("away_team"), "fixture.away_team"),
        "kickoff": kickoff.isoformat(),
    }
    availability_status = str(payload.get("availability_status") or "available")
    if availability_status not in {"available", "unavailable"}:
        raise SourceEvidenceError("availability_status is invalid")
    raw_unavailable_reasons = payload.get("unavailable_reasons") or []
    if not isinstance(raw_unavailable_reasons, list):
        raise SourceEvidenceError("unavailable_reasons must be an array")
    unavailable_reasons = [
        _text(reason, "unavailable_reasons item") for reason in raw_unavailable_reasons
    ]
    markets = payload.get("markets")
    if not isinstance(markets, list):
        raise SourceEvidenceError("raw snapshot markets must be an array")
    if availability_status == "available" and not markets:
        raise SourceEvidenceError("available raw snapshot markets must be non-empty")
    if availability_status == "unavailable" and (markets or not unavailable_reasons):
        raise SourceEvidenceError(
            "unavailable raw snapshot requires reasons and cannot contain markets"
        )
    parsed_markets: list[dict[str, Any]] = []
    seen_markets: set[str] = set()
    for market_index, raw_market in enumerate(markets):
        label = f"markets[{market_index}]"
        if not isinstance(raw_market, Mapping):
            raise SourceEvidenceError(f"{label} must be an object")
        market = _text(raw_market.get("market"), f"{label}.market")
        if market not in SUPPORTED_MARKETS or market in seen_markets:
            raise SourceEvidenceError(f"{label}.market is unsupported or duplicated")
        seen_markets.add(market)
        odds_format = _text(raw_market.get("odds_format"), f"{label}.odds_format")
        if odds_format not in {"decimal", "hong_kong"}:
            raise SourceEvidenceError(f"{label}.odds_format is invalid")
        firms = raw_market.get("firms")
        if not isinstance(firms, list) or not firms:
            raise SourceEvidenceError(f"{label}.firms must be non-empty")
        parsed_firms: list[dict[str, Any]] = []
        outcome_names: set[str] | None = None
        seen_firms: set[str] = set()
        for firm_index, raw_firm in enumerate(firms):
            firm_label = f"{label}.firms[{firm_index}]"
            if not isinstance(raw_firm, Mapping):
                raise SourceEvidenceError(f"{firm_label} must be an object")
            name = _text(raw_firm.get("name"), f"{firm_label}.name")
            if name in seen_firms:
                raise SourceEvidenceError(f"{label} contains a duplicated firm")
            seen_firms.add(name)
            outcomes = raw_firm.get("outcomes")
            if not isinstance(outcomes, Mapping) or len(outcomes) < 2:
                raise SourceEvidenceError(f"{firm_label}.outcomes is incomplete")
            names = {str(item) for item in outcomes}
            if outcome_names is None:
                outcome_names = names
            elif names != outcome_names:
                raise SourceEvidenceError(
                    f"{label} bookmaker rows do not share one complete outcome set"
                )
            parsed_firms.append(
                {
                    "name": name,
                    "outcomes": {
                        str(outcome): _finite_price(
                            price, f"{firm_label}.outcomes.{outcome}"
                        )
                        for outcome, price in sorted(outcomes.items())
                    },
                }
            )
        assert outcome_names is not None
        if market == "htft" and len(outcome_names) != 9:
            raise SourceEvidenceError("HT/FT evidence requires all nine outcomes")
        if market == "1x2" and len(outcome_names) != 3:
            raise SourceEvidenceError("1X2 evidence requires all three outcomes")
        if (
            market in {"asian", "total", "btts", "corner_total", "corner_handicap"}
            and len(outcome_names) != 2
        ):
            raise SourceEvidenceError(
                f"{market} evidence requires exactly two outcomes"
            )
        prices_by_outcome = {
            outcome: [firm["outcomes"][outcome] for firm in parsed_firms]
            for outcome in sorted(outcome_names)
        }
        parsed_markets.append(
            {
                "market": market,
                "odds_format": odds_format,
                "firm_count": len(parsed_firms),
                "firms": parsed_firms,
                "derived_prices": {
                    "median": {
                        outcome: median(values)
                        for outcome, values in prices_by_outcome.items()
                    },
                    "consensus": {
                        outcome: mean(values)
                        for outcome, values in prices_by_outcome.items()
                    },
                },
            }
        )
    parsed_markets.sort(key=lambda item: item["market"])
    raw_http_metadata = payload.get("http_metadata")
    if raw_http_metadata is None:
        raw_http_metadata = {}
    if not isinstance(raw_http_metadata, Mapping):
        raise SourceEvidenceError("http_metadata must be an object")
    status_code = raw_http_metadata.get("status_code")
    if status_code is not None and (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 100 <= status_code <= 599
    ):
        raise SourceEvidenceError("http_metadata.status_code is invalid")
    return {
        "schema_version": RAW_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "source_url": source_url,
        "request_metadata": {
            "scheme": parsed_url.scheme,
            "host": parsed_url.hostname,
            "path": parsed_url.path,
            "query": parsed_url.query,
        },
        "collected_at": collected.isoformat(),
        "availability_status": availability_status,
        "unavailable_reasons": unavailable_reasons,
        "http_metadata": {
            "status_code": status_code,
            **{
                field: (
                    " ".join(str(raw_http_metadata.get(field)).split())
                    if raw_http_metadata.get(field) is not None
                    else None
                )
                for field in ("date", "etag", "last_modified", "content_type")
            },
        },
        "fixture": normalized_fixture,
        "markets": parsed_markets,
    }


def _source_entry(raw: bytes, relative_path: str) -> dict[str, Any]:
    parsed = parse_raw_snapshot(raw)
    return {
        "raw_response_path": relative_path,
        "raw_response_sha256": _hash_bytes(raw),
        "raw_response_bytes": len(raw),
        "parser_version": PARSER_VERSION,
        "parsed": parsed,
    }


def build_evidence(
    source_files: Sequence[str | Path], *, output_dir: str | Path
) -> tuple[Path, dict[str, Any]]:
    if not source_files:
        raise SourceEvidenceError("at least one source file is required")
    target = Path(output_dir).resolve()
    raw_dir = target / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    fixture: dict[str, Any] | None = None
    for source in source_files:
        path = Path(source).resolve()
        raw = path.read_bytes()
        digest = _hash_bytes(raw).split(":", 1)[1]
        raw_path = raw_dir / f"{digest}.json"
        if raw_path.exists() and raw_path.read_bytes() != raw:
            raise SourceEvidenceError("content-addressed raw response collision")
        if not raw_path.exists():
            raw_path.write_bytes(raw)
        entry = _source_entry(raw, raw_path.relative_to(target).as_posix())
        current_fixture = entry["parsed"]["fixture"]
        if fixture is None:
            fixture = current_fixture
        elif current_fixture != fixture:
            raise SourceEvidenceError("source snapshots do not bind the same fixture")
        entries.append(entry)
    assert fixture is not None
    entries.sort(
        key=lambda item: (
            item["parsed"]["collected_at"],
            item["parsed"]["source_url"],
            item["raw_response_sha256"],
        )
    )
    generated = max(
        _aware(entry["parsed"]["collected_at"], "source.collected_at")
        for entry in entries
    )
    market_index: dict[str, list[dict[str, Any]]] = {}
    for source_index, entry in enumerate(entries):
        for market in entry["parsed"]["markets"]:
            market_index.setdefault(market["market"], []).append(
                {
                    "source_index": source_index,
                    "source_url": entry["parsed"]["source_url"],
                    "collected_at": entry["parsed"]["collected_at"],
                    "odds_format": market["odds_format"],
                    "firm_count": market["firm_count"],
                    "derived_prices": market["derived_prices"],
                }
            )
    evidence: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "artifact_type": "soccer_replayable_source_evidence",
        "parser_version": PARSER_VERSION,
        "fixture": fixture,
        "generated_at": generated.isoformat(),
        "sources": entries,
        "market_index": market_index,
        "replay_policy": "reparse_every_content_addressed_raw_source",
    }
    evidence["evidence_hash"] = _hash_json(evidence)
    output = target / f"{fixture['match_id']}-source-evidence.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    return output, evidence


def validate_evidence_file(path: str | Path) -> dict[str, Any]:
    evidence_path = Path(path).resolve()
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceEvidenceError("source evidence is not readable UTF-8 JSON") from exc
    if (
        not isinstance(evidence, Mapping)
        or evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
    ):
        raise SourceEvidenceError("source evidence schema_version is unsupported")
    supplied_hash = evidence.get("evidence_hash")
    without_hash = dict(evidence)
    without_hash.pop("evidence_hash", None)
    if supplied_hash != _hash_json(without_hash):
        raise SourceEvidenceError("source evidence hash is invalid")
    sources = evidence.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SourceEvidenceError("source evidence has no raw sources")
    replayed: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise SourceEvidenceError("source evidence entry is invalid")
        relative_text = str(source.get("raw_response_path") or "")
        relative_path = Path(relative_text)
        if (
            not relative_text
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_path.parts[:1] != ("raw",)
        ):
            raise SourceEvidenceError(
                "raw source path must stay inside the evidence raw directory"
            )
        raw_path = (evidence_path.parent / relative_path).resolve()
        raw_root = (evidence_path.parent / "raw").resolve()
        try:
            raw_path.relative_to(raw_root)
        except ValueError as exc:
            raise SourceEvidenceError(
                "raw source path escapes the evidence raw directory"
            ) from exc
        try:
            raw = raw_path.read_bytes()
        except OSError as exc:
            raise SourceEvidenceError(f"raw source is unavailable: {raw_path}") from exc
        digest = _hash_bytes(raw).split(":", 1)[1]
        expected_relative = f"raw/{digest}.json"
        if relative_path.as_posix() != expected_relative:
            raise SourceEvidenceError(
                "raw source path is not its canonical content-addressed path"
            )
        expected = _source_entry(raw, expected_relative)
        if expected != source:
            raise SourceEvidenceError(
                "raw source replay does not reproduce its archived parse"
            )
        replayed.append(expected)
    generated_path, rebuilt = _rebuild_from_entries(replayed)
    del generated_path
    if rebuilt != evidence:
        raise SourceEvidenceError(
            "source evidence replay does not reproduce the bundle"
        )
    return dict(evidence)


def _rebuild_from_entries(
    entries: Sequence[dict[str, Any]],
) -> tuple[None, dict[str, Any]]:
    fixture = entries[0]["parsed"]["fixture"]
    if any(entry["parsed"]["fixture"] != fixture for entry in entries):
        raise SourceEvidenceError("replayed sources bind different fixtures")
    sorted_entries = sorted(
        entries,
        key=lambda item: (
            item["parsed"]["collected_at"],
            item["parsed"]["source_url"],
            item["raw_response_sha256"],
        ),
    )
    market_index: dict[str, list[dict[str, Any]]] = {}
    for source_index, entry in enumerate(sorted_entries):
        for market in entry["parsed"]["markets"]:
            market_index.setdefault(market["market"], []).append(
                {
                    "source_index": source_index,
                    "source_url": entry["parsed"]["source_url"],
                    "collected_at": entry["parsed"]["collected_at"],
                    "odds_format": market["odds_format"],
                    "firm_count": market["firm_count"],
                    "derived_prices": market["derived_prices"],
                }
            )
    rebuilt: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "artifact_type": "soccer_replayable_source_evidence",
        "parser_version": PARSER_VERSION,
        "fixture": fixture,
        "generated_at": max(entry["parsed"]["collected_at"] for entry in entries),
        "sources": sorted_entries,
        "market_index": market_index,
        "replay_policy": "reparse_every_content_addressed_raw_source",
    }
    rebuilt["evidence_hash"] = _hash_json(rebuilt)
    return None, rebuilt


def match_candidate(
    evidence: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    market = str(candidate.get("market") or "")
    snapshots = evidence.get("market_index", {}).get(market)
    if not isinstance(snapshots, list) or not snapshots:
        raise SourceEvidenceError(f"no replayable source evidence for market {market}")
    collected = _aware(
        candidate.get("market_collected_at"), "candidate.market_collected_at"
    )
    odds_format = str(candidate.get("odds_format") or "")
    price_basis = str(candidate.get("price_basis") or "")
    if price_basis not in {"median", "consensus"}:
        raise SourceEvidenceError(
            "candidate price_basis must be median or consensus for source replay"
        )
    raw_prices = candidate.get("complete_market_odds")
    if not isinstance(raw_prices, Mapping):
        raise SourceEvidenceError("candidate complete_market_odds are missing")
    prices = {str(key): float(value) for key, value in raw_prices.items()}
    candidate_source = _source_url(candidate.get("market_source"))
    raw_firm_count = candidate.get("firm_count")
    if (
        isinstance(raw_firm_count, bool)
        or not isinstance(raw_firm_count, (int, float))
        or not math.isfinite(float(raw_firm_count))
        or int(float(raw_firm_count)) != float(raw_firm_count)
        or int(float(raw_firm_count)) < 1
    ):
        raise SourceEvidenceError(
            "candidate firm_count must be a positive integer for source replay"
        )
    candidate_firm_count = int(float(raw_firm_count))
    for snapshot in snapshots:
        if (
            _aware(snapshot["collected_at"], "snapshot.collected_at") == collected
            and snapshot["odds_format"] == odds_format
            and snapshot["source_url"] == candidate_source
            and int(snapshot["firm_count"]) == candidate_firm_count
        ):
            expected = snapshot["derived_prices"][price_basis]
            if set(expected) != set(prices):
                continue
            if all(
                math.isclose(float(expected[key]), prices[key], abs_tol=5e-7)
                for key in expected
            ):
                return {
                    "evidence_hash": evidence["evidence_hash"],
                    "source_index": snapshot["source_index"],
                    "source_url": snapshot["source_url"],
                    "collected_at": snapshot["collected_at"],
                    "market": market,
                    "price_basis": price_basis,
                    "odds_format": odds_format,
                    "firm_count": snapshot["firm_count"],
                    "prices": expected,
                }
    raise SourceEvidenceError(
        f"candidate {market} prices/time do not reproduce from raw source evidence"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build evidence from visible-page JSON")
    build.add_argument("--source-file", action="append", required=True)
    build.add_argument("--output-dir", required=True)
    verify = subparsers.add_parser("verify", help="replay an evidence bundle")
    verify.add_argument("--evidence", required=True)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    try:
        if arguments.command == "build":
            path, evidence = build_evidence(
                arguments.source_file, output_dir=arguments.output_dir
            )
        else:
            path = Path(arguments.evidence).resolve()
            evidence = validate_evidence_file(path)
        print(
            json.dumps(
                {
                    "ok": True,
                    "path": str(path),
                    "evidence_hash": evidence["evidence_hash"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (SourceEvidenceError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

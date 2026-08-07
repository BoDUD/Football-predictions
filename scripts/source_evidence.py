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
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

RAW_SCHEMA_VERSION = "visible-market-snapshot/2.0.0"
EVIDENCE_SCHEMA_VERSION = "source-evidence/2.0.0"
PARSER_VERSION = "visible-market-parser/2.0.0"
LEGACY_RAW_SCHEMA_VERSION = "visible-market-snapshot/1.0.0"
LEGACY_EVIDENCE_SCHEMA_VERSION = "source-evidence/1.0.0"
LEGACY_PARSER_VERSION = "visible-market-parser/1.0.0"
SUPPORTED_MARKETS = {
    "1x2",
    "asian",
    "total",
    "htft",
    "goal_range",
    "btts",
    "corner_total",
    "corner_handicap",
}
LEGACY_SUPPORTED_MARKETS = SUPPORTED_MARKETS | {"half_time"}
ALLOWED_SOURCE_HOSTS = {
    "zq.titan007.com",
    "live.titan007.com",
    "m.titan007.com",
    "www.espn.com",
    "www.sofascore.com",
}
SUPPORTED_PERIODS = {"full_time", "first_half", "second_half"}
LINE_MARKETS = {"asian", "total", "corner_total", "corner_handicap"}
SUPPORTED_PERIODS_BY_MARKET = {
    "1x2": {"full_time", "first_half"},
    "asian": {"full_time", "first_half"},
    "total": {"full_time", "first_half"},
    "htft": {"full_time"},
    "goal_range": {"full_time"},
    "btts": {"full_time"},
    "corner_total": {"full_time"},
    "corner_handicap": {"full_time"},
}
CANONICAL_PRICE_OUTCOMES = {
    "1x2": ("H", "D", "A"),
    "asian": ("home", "away"),
    "total": ("over", "under"),
    "htft": tuple(f"{half}{full}" for half in "HDA" for full in "HDA"),
    "btts": ("yes", "no"),
    "corner_total": ("over", "under"),
    "corner_handicap": ("home", "away"),
}
WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
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


def _fixture_token(value: Any, label: str) -> str:
    text = _text(value, label)
    if (
        unicodedata.normalize("NFKC", text) != text
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text)
        or text.upper() in WINDOWS_DEVICE_NAMES
    ):
        raise SourceEvidenceError(
            f"{label} must be a portable ASCII fixture token without path syntax"
        )
    return text


def _canonical_goal_range_outcomes(
    outcomes: Sequence[str], *, label: str
) -> tuple[str, ...]:
    """Require one unambiguous, gap-free partition of total goals from zero."""

    canonical_integer = r"(?:0|[1-9][0-9]*)"
    previous_max: int | None = None
    canonical: list[str] = []
    for index, outcome in enumerate(outcomes):
        closed = re.fullmatch(rf"({canonical_integer})-({canonical_integer})", outcome)
        open_ended = re.fullmatch(rf"({canonical_integer})\+", outcome)
        if open_ended:
            if index != len(outcomes) - 1:
                raise SourceEvidenceError(
                    f"{label} open-ended goal range must be the final outcome"
                )
            minimum = int(open_ended.group(1))
            if previous_max is None or minimum != previous_max + 1:
                raise SourceEvidenceError(
                    f"{label} goal ranges must form a gap-free partition from zero"
                )
            canonical.append(f"{minimum}+")
            previous_max = None
            continue
        if closed is None:
            raise SourceEvidenceError(
                f"{label} goal ranges must use canonical MIN-MAX or N+ syntax"
            )
        minimum, maximum = (int(item) for item in closed.groups())
        if minimum > maximum:
            raise SourceEvidenceError(f"{label} goal range lower bound exceeds upper")
        expected_minimum = 0 if previous_max is None else previous_max + 1
        if minimum != expected_minimum:
            raise SourceEvidenceError(
                f"{label} goal ranges must form a gap-free partition from zero"
            )
        canonical.append(f"{minimum}-{maximum}")
        previous_max = maximum
    if not canonical or previous_max is not None:
        raise SourceEvidenceError(
            f"{label} goal ranges require exactly one final open-ended N+ outcome"
        )
    return tuple(canonical)


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


def _quarter_line(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise SourceEvidenceError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceEvidenceError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or not math.isclose(
        number * 4.0, round(number * 4.0), abs_tol=1e-8
    ):
        raise SourceEvidenceError(f"{label} must use a finite quarter-line increment")
    return 0.0 if math.isclose(number, 0.0, abs_tol=1e-12) else number


def canonical_market_identity(
    value: Any, *, label: str = "market_identity"
) -> dict[str, Any]:
    """Validate the quoted market independently from its later settlement states."""

    if not isinstance(value, Mapping):
        raise SourceEvidenceError(f"{label} must be an object")
    expected_fields = {"family", "period", "line", "price_outcomes"}
    if set(value) != expected_fields:
        raise SourceEvidenceError(
            f"{label} fields must be exactly family, period, line, price_outcomes"
        )
    family = _text(value.get("family"), f"{label}.family").lower()
    if family == "half_time":
        raise SourceEvidenceError(
            f"{label}.family=half_time is legacy-only; use family=1x2 and period=first_half"
        )
    if family not in SUPPORTED_MARKETS:
        raise SourceEvidenceError(f"{label}.family is unsupported")
    period = _text(value.get("period"), f"{label}.period").lower()
    if period not in SUPPORTED_PERIODS:
        raise SourceEvidenceError(f"{label}.period is unsupported")
    if period not in SUPPORTED_PERIODS_BY_MARKET[family]:
        raise SourceEvidenceError(
            f"{label}.period={period} is not settlement-safe for family={family}"
        )
    raw_line = value.get("line")
    if family in LINE_MARKETS:
        line = _quarter_line(raw_line, f"{label}.line")
        if family in {"total", "corner_total"} and line < 0.0:
            raise SourceEvidenceError(f"{label}.line cannot be negative for {family}")
    elif raw_line is not None:
        raise SourceEvidenceError(f"{label}.line must be null for {family}")
    else:
        line = None
    raw_outcomes = value.get("price_outcomes")
    if (
        not isinstance(raw_outcomes, list)
        or len(raw_outcomes) < 2
        or any(not isinstance(item, str) or not item for item in raw_outcomes)
        or len(set(raw_outcomes)) != len(raw_outcomes)
    ):
        raise SourceEvidenceError(f"{label}.price_outcomes must be unique strings")
    price_outcomes = tuple(raw_outcomes)
    expected_outcomes = CANONICAL_PRICE_OUTCOMES.get(family)
    if expected_outcomes is not None and price_outcomes != expected_outcomes:
        raise SourceEvidenceError(
            f"{label}.price_outcomes must be {list(expected_outcomes)} for {family}"
        )
    if family == "goal_range":
        price_outcomes = _canonical_goal_range_outcomes(
            price_outcomes, label=f"{label}.price_outcomes"
        )
    elif expected_outcomes is None and list(price_outcomes) != sorted(price_outcomes):
        raise SourceEvidenceError(
            f"{label}.price_outcomes must be canonically sorted for {family}"
        )
    return {
        "family": family,
        "period": period,
        "line": line,
        "price_outcomes": list(price_outcomes),
    }


def market_identity_hash(identity: Mapping[str, Any]) -> str:
    return _hash_json(canonical_market_identity(identity))


def parse_raw_snapshot(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SourceEvidenceError("raw snapshot is not UTF-8 JSON") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") not in {
        RAW_SCHEMA_VERSION,
        LEGACY_RAW_SCHEMA_VERSION,
    }:
        raise SourceEvidenceError("raw snapshot schema_version is unsupported")
    legacy = payload.get("schema_version") == LEGACY_RAW_SCHEMA_VERSION
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
        "match_id": (
            _text(fixture.get("match_id"), "fixture.match_id")
            if legacy
            else _fixture_token(fixture.get("match_id"), "fixture.match_id")
        ),
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
    seen_market_identities: set[str] = set()
    seen_legacy_markets: set[str] = set()
    for market_index, raw_market in enumerate(markets):
        label = f"markets[{market_index}]"
        if not isinstance(raw_market, Mapping):
            raise SourceEvidenceError(f"{label} must be an object")
        if legacy:
            market = _text(raw_market.get("market"), f"{label}.market")
            if market not in LEGACY_SUPPORTED_MARKETS or market in seen_legacy_markets:
                raise SourceEvidenceError(
                    f"{label}.market is unsupported or duplicated"
                )
            seen_legacy_markets.add(market)
            identity = None
            identity_hash = None
        else:
            identity = canonical_market_identity(
                raw_market.get("market_identity"), label=f"{label}.market_identity"
            )
            identity_hash = market_identity_hash(identity)
            if identity_hash in seen_market_identities:
                raise SourceEvidenceError(f"{label}.market_identity is duplicated")
            seen_market_identities.add(identity_hash)
            market = identity["family"]
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
            firm_identity = (
                name if legacy else unicodedata.normalize("NFKC", name).casefold()
            )
            if firm_identity in seen_firms:
                raise SourceEvidenceError(f"{label} contains a duplicated firm")
            seen_firms.add(firm_identity)
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
            normalized_outcomes: dict[str, float] = {}
            for outcome, price in sorted(outcomes.items()):
                normalized_price = _finite_price(
                    price, f"{firm_label}.outcomes.{outcome}"
                )
                if not legacy and odds_format == "decimal" and normalized_price <= 1.0:
                    raise SourceEvidenceError(
                        f"{firm_label}.outcomes.{outcome} decimal odds must be greater than 1"
                    )
                normalized_outcomes[str(outcome)] = normalized_price
            parsed_firms.append({"name": name, "outcomes": normalized_outcomes})
        assert outcome_names is not None
        if legacy:
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
        elif outcome_names != set(identity["price_outcomes"]):
            raise SourceEvidenceError(
                f"{label} bookmaker outcomes do not match market_identity.price_outcomes"
            )
        prices_by_outcome = {
            outcome: [firm["outcomes"][outcome] for firm in parsed_firms]
            for outcome in sorted(outcome_names)
        }
        parsed_market = {
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
        if legacy:
            parsed_market["market"] = market
        else:
            parsed_market["market_identity"] = identity
            parsed_market["market_identity_hash"] = identity_hash
        parsed_markets.append(parsed_market)
    parsed_markets.sort(
        key=lambda item: item.get("market_identity_hash") or item.get("market")
    )
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
        "schema_version": (LEGACY_RAW_SCHEMA_VERSION if legacy else RAW_SCHEMA_VERSION),
        "parser_version": LEGACY_PARSER_VERSION if legacy else PARSER_VERSION,
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
        "parser_version": parsed["parser_version"],
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
    _unused, evidence = _rebuild_from_entries(entries)
    output = (target / f"{fixture['match_id']}-source-evidence.json").resolve()
    try:
        output.relative_to(target)
    except ValueError as exc:
        raise SourceEvidenceError(
            "source evidence output path escapes the requested output directory"
        ) from exc
    if output.parent != target:
        raise SourceEvidenceError(
            "source evidence output path must stay directly inside the output directory"
        )
    if output.exists():
        try:
            existing = validate_evidence_file(output)
        except (OSError, SourceEvidenceError) as exc:
            raise SourceEvidenceError(
                "existing source evidence is invalid and will not be overwritten"
            ) from exc
        if existing == evidence:
            return output, evidence
        raise SourceEvidenceError(
            "different source evidence already exists for this fixture; "
            "use a new output directory"
        )
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
    except FileExistsError as exc:
        raise SourceEvidenceError(
            "source evidence appeared concurrently and was not overwritten"
        ) from exc
    return output, evidence


def validate_evidence_file(path: str | Path) -> dict[str, Any]:
    evidence_path = Path(path).resolve()
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceEvidenceError("source evidence is not readable UTF-8 JSON") from exc
    if not isinstance(evidence, Mapping) or evidence.get("schema_version") not in {
        EVIDENCE_SCHEMA_VERSION,
        LEGACY_EVIDENCE_SCHEMA_VERSION,
    }:
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
    parser_versions = {entry.get("parser_version") for entry in sorted_entries}
    if parser_versions == {PARSER_VERSION}:
        legacy = False
        evidence_schema = EVIDENCE_SCHEMA_VERSION
        parser_version = PARSER_VERSION
    elif parser_versions == {LEGACY_PARSER_VERSION}:
        legacy = True
        evidence_schema = LEGACY_EVIDENCE_SCHEMA_VERSION
        parser_version = LEGACY_PARSER_VERSION
    else:
        raise SourceEvidenceError(
            "one evidence bundle cannot mix legacy and canonical market identities"
        )
    market_index: dict[str, list[dict[str, Any]]] = {}
    for source_index, entry in enumerate(sorted_entries):
        for market in entry["parsed"]["markets"]:
            key = market["market"] if legacy else market["market_identity_hash"]
            indexed = {
                "source_index": source_index,
                "source_url": entry["parsed"]["source_url"],
                "collected_at": entry["parsed"]["collected_at"],
                "odds_format": market["odds_format"],
                "firm_count": market["firm_count"],
                "derived_prices": market["derived_prices"],
            }
            if not legacy:
                indexed.update(
                    {
                        "market_identity": market["market_identity"],
                        "market_identity_hash": key,
                    }
                )
            market_index.setdefault(key, []).append(indexed)
    rebuilt: dict[str, Any] = {
        "schema_version": evidence_schema,
        "artifact_type": "soccer_replayable_source_evidence",
        "parser_version": parser_version,
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
    if evidence.get("schema_version") == LEGACY_EVIDENCE_SCHEMA_VERSION:
        return _match_legacy_candidate(evidence, candidate)
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise SourceEvidenceError("candidate evidence schema is unsupported")
    identity = canonical_market_identity(
        candidate.get("market_identity"), label="candidate.market_identity"
    )
    identity_hash = market_identity_hash(identity)
    supplied_identity_hash = str(candidate.get("market_identity_hash") or "")
    if supplied_identity_hash != identity_hash:
        raise SourceEvidenceError("candidate market_identity_hash is invalid")
    snapshots = evidence.get("market_index", {}).get(identity_hash)
    if not isinstance(snapshots, list) or not snapshots:
        raise SourceEvidenceError(
            f"no replayable source evidence for market identity {identity_hash}"
        )
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
    if set(raw_prices) != set(identity["price_outcomes"]):
        raise SourceEvidenceError(
            "candidate complete_market_odds do not match price_outcomes"
        )
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
            and snapshot["market_identity"] == identity
            and snapshot["market_identity_hash"] == identity_hash
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
                    "evidence_scope": "canonical_market_identity_forward_eligible",
                    "source_index": snapshot["source_index"],
                    "source_url": snapshot["source_url"],
                    "collected_at": snapshot["collected_at"],
                    "market_identity": identity,
                    "market_identity_hash": identity_hash,
                    "price_basis": price_basis,
                    "odds_format": odds_format,
                    "firm_count": snapshot["firm_count"],
                    "prices": expected,
                }
    raise SourceEvidenceError(
        f"candidate {identity_hash} prices/time do not reproduce from raw source evidence"
    )


def _match_legacy_candidate(
    evidence: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Replay v1 family-only evidence without admitting it to new confirmation."""

    market = str(candidate.get("market") or "")
    snapshots = evidence.get("market_index", {}).get(market)
    if not isinstance(snapshots, list) or not snapshots:
        raise SourceEvidenceError(f"no replayable legacy source evidence for {market}")
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
            if set(expected) == set(prices) and all(
                math.isclose(float(expected[key]), prices[key], abs_tol=5e-7)
                for key in expected
            ):
                return {
                    "evidence_hash": evidence["evidence_hash"],
                    "evidence_scope": "legacy_read_only_quarantined",
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
        f"legacy candidate {market} prices/time do not reproduce from source evidence"
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

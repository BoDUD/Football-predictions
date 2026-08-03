#!/usr/bin/env python3
"""Collect regulation-time corner results and historical corner prices from Titan.

The input is one or more schedule bundles produced from Titan's competition pages.
Every response is bound back to the source fixture and persisted with a SHA-256 hash.
The append-only NDJSON checkpoint makes the long-running collection resumable.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE = "https://m.titan007.com"
ENDPOINT = BASE + "/Common/CommonInterface.ashx?type=1&isall=0&scheid={match_id}&lang=0"
HEADER_BASE = "https://livestatic.titan007.com/phone/txt/analysisheader/cn"
HANDICAP_ENDPOINT = (
    BASE
    + "/HandicapDataInterface.ashx?scheid={match_id}&type=4&oddskind=2"
    + "&companyid=8&isHalf=0"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
)
SCHEMA_VERSION = "1.0.0"
COLLECTOR_VERSION = "titan-corner-history/1.0.0"
SCHEDULE_NORMALIZER_VERSION = "titan-schedule-snapshot-normalizer/1.0.0"
SOURCE_TIMEZONE = "Asia/Shanghai"
SOURCE_TIMEZONE_OFFSET = timezone(timedelta(hours=8))
RETRYABLE_HTTP = {408, 425, 429, 442, 500, 502, 503, 504}
CHECKPOINT_IDENTITY_FIELDS = (
    "competition_key",
    "competition_regime",
    "season_label",
    "season_start_year",
    "phase",
    "round",
    "kickoff",
    "kickoff_utc",
    "kickoff_epoch",
    "source_timezone",
    "home_team_id",
    "away_team_id",
    "home_team",
    "away_team",
    "home_goals",
    "away_goals",
)


class CornerCollectionError(ValueError):
    """Raised when a schedule or a fetched corner response is unsafe to use."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _season_year(fixture: Mapping[str, Any]) -> int | None:
    raw = fixture.get("season_start_year")
    if raw is not None and raw != "":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    match = re.search(r"\b(20\d{2})\b", str(fixture.get("season_label") or ""))
    return int(match.group(1)) if match else None


def normalize_competition_regime(fixture: Mapping[str, Any]) -> str:
    competition = str(fixture.get("competition_key") or "").strip().casefold()
    if competition in {"japan-j1", "japan_j1"} and _season_year(fixture) == 2026:
        return "2026_vision_regional"
    regime = str(fixture.get("competition_regime") or "").strip()
    return "regular" if regime == "standard" else regime


def schedule_fixture_sha256(fixture: Mapping[str, Any]) -> str:
    payload = {"match_id": str(fixture.get("match_id") or "")}
    payload.update({field: fixture.get(field) for field in CHECKPOINT_IDENTITY_FIELDS})
    return _hash_bytes(_canonical_json(payload).encode("utf-8"))


def _fixture_binding(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable schedule identity copied into every result row."""

    binding = {
        field: fixture.get(field) for field in CHECKPOINT_IDENTITY_FIELDS
    }
    binding["schedule_fixture_sha256"] = schedule_fixture_sha256(fixture)
    return binding


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _schedule_local_datetime(value: Any, source: str) -> datetime:
    """Parse one Titan schedule kickoff under the frozen Shanghai-time contract."""

    local_text = str(value or "").strip().replace("/", "-")
    try:
        if local_text.isdigit() and len(local_text) in {12, 14}:
            parsed = datetime.strptime(
                local_text,
                "%Y%m%d%H%M%S" if len(local_text) == 14 else "%Y%m%d%H%M",
            )
        else:
            parsed = datetime.fromisoformat(local_text)
    except ValueError as error:
        raise CornerCollectionError(f"{source} kickoff is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=SOURCE_TIMEZONE_OFFSET)
    if parsed.utcoffset() != SOURCE_TIMEZONE_OFFSET.utcoffset(None):
        raise CornerCollectionError(
            f"{source} kickoff offset conflicts with {SOURCE_TIMEZONE}"
        )
    return parsed


def _normalized_schedule_payload(
    payload: Any, *, source: str
) -> tuple[dict[str, Any], dict[str, int]]:
    """Return a validated, offline-normalized schedule payload and change counts."""

    if not isinstance(payload, dict):
        raise CornerCollectionError(f"schedule bundle {source} must be an object")
    if payload.get("source_timezone") != SOURCE_TIMEZONE:
        raise CornerCollectionError(
            f"schedule bundle {source} source_timezone must be {SOURCE_TIMEZONE}"
        )
    raw_matches = payload.get("matches")
    if not isinstance(raw_matches, list):
        raise CornerCollectionError(f"schedule bundle {source} must contain matches[]")

    normalized_payload = dict(payload)
    normalized_matches: list[dict[str, Any]] = []
    seen_match_ids: set[str] = set()
    counts = {
        "matches": len(raw_matches),
        "changed_rows": 0,
        "kickoff_utc_added_or_canonicalized": 0,
        "kickoff_epoch_added_or_canonicalized": 0,
        "source_timezone_added": 0,
        "competition_regime_normalized": 0,
        "japan_j1_2026_regime_normalized": 0,
    }
    for index, raw in enumerate(raw_matches):
        if not isinstance(raw, dict):
            raise CornerCollectionError(f"{source} matches[{index}] must be an object")
        match_id = str(raw.get("match_id") or "").strip()
        if not match_id.isdigit() or int(match_id) <= 0:
            raise CornerCollectionError(f"{source} matches[{index}] has invalid match_id")
        if match_id in seen_match_ids:
            raise CornerCollectionError(f"{source} has duplicate match_id {match_id}")
        seen_match_ids.add(match_id)
        row = dict(raw)
        row_source = f"{source} match {match_id}"
        local = _schedule_local_datetime(row.get("kickoff"), row_source)
        expected_epoch = int(local.timestamp())
        expected_utc = local.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")

        existing_timezone = row.get("source_timezone")
        if (
            existing_timezone is not None
            and existing_timezone != ""
            and existing_timezone != SOURCE_TIMEZONE
        ):
            raise CornerCollectionError(
                f"{row_source} source_timezone conflicts with bundle timezone"
            )
        if existing_timezone != SOURCE_TIMEZONE:
            row["source_timezone"] = SOURCE_TIMEZONE
            counts["source_timezone_added"] += 1

        existing_utc = str(row.get("kickoff_utc") or "").strip()
        if existing_utc:
            normalized_utc = (
                existing_utc[:-1] + "+00:00"
                if existing_utc.endswith("Z")
                else existing_utc
            )
            try:
                parsed_utc = datetime.fromisoformat(normalized_utc)
            except ValueError as error:
                raise CornerCollectionError(
                    f"{row_source} kickoff_utc is invalid"
                ) from error
            if parsed_utc.tzinfo is None or parsed_utc.utcoffset() is None:
                raise CornerCollectionError(
                    f"{row_source} kickoff_utc must include a timezone"
                )
            if int(parsed_utc.timestamp()) != expected_epoch:
                raise CornerCollectionError(
                    f"{row_source} kickoff_utc conflicts with frozen local kickoff"
                )
        if existing_utc != expected_utc:
            row["kickoff_utc"] = expected_utc
            counts["kickoff_utc_added_or_canonicalized"] += 1

        existing_epoch = row.get("kickoff_epoch")
        if existing_epoch is not None and existing_epoch != "":
            parsed_epoch = _as_nonnegative_int(
                existing_epoch, f"{row_source} kickoff_epoch"
            )
            if parsed_epoch != expected_epoch:
                raise CornerCollectionError(
                    f"{row_source} kickoff_epoch conflicts with frozen local kickoff"
                )
        if existing_epoch != expected_epoch:
            row["kickoff_epoch"] = expected_epoch
            counts["kickoff_epoch_added_or_canonicalized"] += 1

        prior_regime = str(row.get("competition_regime") or "").strip()
        normalized_regime = normalize_competition_regime(row)
        if not normalized_regime:
            raise CornerCollectionError(
                f"{row_source} competition_regime is missing"
            )
        if prior_regime != normalized_regime:
            row["competition_regime"] = normalized_regime
            counts["competition_regime_normalized"] += 1
            if (
                str(row.get("competition_key") or "").strip().casefold()
                in {"japan-j1", "japan_j1"}
                and _season_year(row) == 2026
            ):
                counts["japan_j1_2026_regime_normalized"] += 1

        _validate_schedule_kickoff(row, row_source)
        if row != raw:
            counts["changed_rows"] += 1
        normalized_matches.append(row)

    normalized_payload["matches"] = normalized_matches
    return normalized_payload, counts


def normalize_schedule_snapshot(
    path: str | Path, *, write: bool = True
) -> dict[str, Any]:
    """Upgrade one frozen schedule snapshot without any network access.

    Existing UTC/epoch values are treated as evidence and must agree with the
    original Shanghai-local kickoff; conflicts fail closed instead of being
    overwritten.  A changed file is replaced atomically, and a second run is
    a byte-preserving no-op.
    """

    schedule_path = Path(path).resolve()
    try:
        original_bytes = schedule_path.read_bytes()
        payload = json.loads(original_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CornerCollectionError(
            f"cannot read schedule bundle {schedule_path}: {error}"
        ) from error
    normalized, counts = _normalized_schedule_payload(
        payload, source=str(schedule_path)
    )
    normalized_bytes = (
        json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    changed = counts["changed_rows"] > 0
    if write and changed:
        _atomic_json(schedule_path, normalized)
        final_bytes = schedule_path.read_bytes()
        if final_bytes != normalized_bytes:
            raise CornerCollectionError(
                f"atomic schedule normalization verification failed: {schedule_path}"
            )
    else:
        final_bytes = original_bytes
    return {
        "normalizer_version": SCHEDULE_NORMALIZER_VERSION,
        "path": str(schedule_path),
        "source_timezone": SOURCE_TIMEZONE,
        **counts,
        "write_requested": write,
        "written": bool(write and changed),
        "source_file_sha256_before": _hash_bytes(original_bytes),
        "source_file_sha256_after": _hash_bytes(
            final_bytes if write else (normalized_bytes if changed else original_bytes)
        ),
    }


def _as_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CornerCollectionError(f"{field} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise CornerCollectionError(f"{field} must be a non-negative integer") from error
    if number < 0 or (isinstance(value, float) and value != number):
        raise CornerCollectionError(f"{field} must be a non-negative integer")
    return number


def _validate_schedule_kickoff(fixture: Mapping[str, Any], source: str) -> None:
    epoch = _as_nonnegative_int(fixture.get("kickoff_epoch"), f"{source} kickoff_epoch")
    utc_text = str(fixture.get("kickoff_utc") or "").strip()
    normalized_utc = utc_text[:-1] + "+00:00" if utc_text.endswith("Z") else utc_text
    try:
        utc_value = datetime.fromisoformat(normalized_utc)
    except ValueError as error:
        raise CornerCollectionError(f"{source} kickoff_utc is invalid") from error
    if utc_value.tzinfo is None or utc_value.utcoffset() is None:
        raise CornerCollectionError(f"{source} kickoff_utc must include a timezone")
    if int(utc_value.timestamp()) != epoch:
        raise CornerCollectionError(f"{source} kickoff_epoch does not match kickoff_utc")

    local_text = str(fixture.get("kickoff") or "").strip().replace("/", "-")
    try:
        if local_text.isdigit() and len(local_text) in {12, 14}:
            local_value = datetime.strptime(
                local_text, "%Y%m%d%H%M%S" if len(local_text) == 14 else "%Y%m%d%H%M"
            )
        else:
            local_value = datetime.fromisoformat(local_text)
    except ValueError as error:
        raise CornerCollectionError(f"{source} kickoff is invalid") from error
    if local_value.tzinfo is None:
        local_value = local_value.replace(tzinfo=timezone(timedelta(hours=8)))
    if int(local_value.timestamp()) != epoch:
        raise CornerCollectionError(f"{source} kickoff does not match kickoff_epoch")


def load_schedule_files(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Load completed fixtures and reject conflicting duplicate match IDs."""
    by_match: dict[str, dict[str, Any]] = {}
    identity_fields = CHECKPOINT_IDENTITY_FIELDS
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CornerCollectionError(f"cannot read schedule bundle {path}: {error}") from error
        rows = payload.get("matches") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise CornerCollectionError(f"schedule bundle {path} must contain matches[]")
        for index, raw in enumerate(rows):
            if not isinstance(raw, dict):
                raise CornerCollectionError(f"{path} matches[{index}] must be an object")
            if raw.get("training_eligible") is False or raw.get("completed") is False:
                continue
            raw = dict(raw)
            raw["competition_regime"] = normalize_competition_regime(raw)
            match_id = str(raw.get("match_id") or "").strip()
            if not match_id.isdigit():
                raise CornerCollectionError(f"{path} matches[{index}] has invalid match_id")
            required = (
                "competition_key",
                "competition_regime",
                "season_label",
                "kickoff",
                "kickoff_utc",
                "kickoff_epoch",
                "source_timezone",
                "home_team",
                "away_team",
            )
            if any(not str(raw.get(field) or "").strip() for field in required):
                raise CornerCollectionError(f"{path} match {match_id} has incomplete fixture metadata")
            _as_nonnegative_int(raw.get("home_goals"), f"{path} match {match_id} home_goals")
            _as_nonnegative_int(raw.get("away_goals"), f"{path} match {match_id} away_goals")
            if str(raw.get("source_timezone")) != "Asia/Shanghai":
                raise CornerCollectionError(
                    f"{path} match {match_id} source_timezone must be Asia/Shanghai"
                )
            _validate_schedule_kickoff(raw, f"{path} match {match_id}")
            current = by_match.get(match_id)
            if current is not None:
                conflicts = [
                    field
                    for field in identity_fields
                    if current.get(field) != raw.get(field)
                ]
                if conflicts:
                    raise CornerCollectionError(
                        f"match_id {match_id} conflicts across schedules: {', '.join(conflicts)}"
                    )
                continue
            by_match[match_id] = dict(raw)
    return sorted(
        by_match.values(),
        key=lambda row: (str(row["kickoff"]), int(row["match_id"])),
    )


def _tech_item(items: Any, kind: str) -> tuple[int, int] | None:
    if not isinstance(items, list):
        return None
    matches = [item for item in items if isinstance(item, dict) and item.get("kind") == kind]
    if len(matches) > 1:
        raise CornerCollectionError(f"techStat contains duplicate {kind} entries")
    if not matches:
        return None
    item = matches[0]
    home = item.get("home")
    away = item.get("away")
    if not isinstance(home, dict) or not isinstance(away, dict):
        raise CornerCollectionError(f"techStat {kind} entry is malformed")
    return (
        _as_nonnegative_int(home.get("value"), f"{kind}.home"),
        _as_nonnegative_int(away.get("value"), f"{kind}.away"),
    )


def _contains_extra_time(value: Any) -> bool:
    needles = ("extratime", "extra_time", "penalty", "加时", "点球")
    text = _canonical_json(value).lower()
    return any(needle in text for needle in needles)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value in {None, ""}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def parse_corner_odds(value: Any) -> list[dict[str, Any]]:
    """Flatten Titan's retained corner-market opening/latest records."""
    companies = value.get("odds") if isinstance(value, dict) else None
    if not isinstance(companies, list):
        return []
    output: list[dict[str, Any]] = []
    for company in companies:
        if not isinstance(company, dict):
            continue
        company_id = company.get("companyId")
        company_name = str(company.get("company") or "").strip()
        for market in company.get("oddsList", []):
            if not isinstance(market, dict):
                continue
            market_type = str(market.get("type") or "").upper()
            period = str(market.get("kind") or "").upper()
            if market_type not in {"ASIAN", "OU", "EURO"} or period not in {"FULL", "HALF"}:
                continue
            records = market.get("records")
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                first = record.get("firstOdds") if isinstance(record.get("firstOdds"), dict) else {}
                latest = record.get("runOdds") if isinstance(record.get("runOdds"), dict) else {}
                output.append(
                    {
                        "company_id": company_id,
                        "company": company_name,
                        "market_type": market_type,
                        "period": period,
                        "opening_home": _number(first.get("home")),
                        "opening_line": _number(first.get("draw")),
                        "opening_away": _number(first.get("away")),
                        "retained_latest_home": _number(latest.get("home")),
                        "retained_latest_line": _number(latest.get("draw")),
                        "retained_latest_away": _number(latest.get("away")),
                        "retained_latest_modify_epoch": str(record.get("modifyTime") or "") or None,
                        "historical_price_scope": "research_only_untimestamped_opening",
                    }
                )
    output.sort(
        key=lambda row: (
            str(row["company_id"]),
            row["market_type"],
            row["period"],
        )
    )
    return output


def parse_response(fixture: Mapping[str, Any], raw: bytes, collected_at: str) -> dict[str, Any]:
    """Parse one response and reconcile full/half corner sources."""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CornerCollectionError("Titan response is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise CornerCollectionError("Titan response must be a JSON object")
    tech = payload.get("techStat")
    tech = tech if isinstance(tech, dict) else {}
    full = _tech_item(tech.get("itemList"), "CORNER")
    half_direct = _tech_item(tech.get("itemList"), "HALF_CORNER")
    half_list = _tech_item(tech.get("firstHalfList"), "CORNER")
    conflict_reasons: list[str] = []
    if half_direct is not None and half_list is not None and half_direct != half_list:
        conflict_reasons.append("half_corner_sources_disagree")
    half = half_direct if half_direct is not None else half_list
    if full is not None and half is not None and (half[0] > full[0] or half[1] > full[1]):
        conflict_reasons.append("half_corners_exceed_full_corners")
    extra_time = _contains_extra_time(payload.get("events"))
    if extra_time:
        conflict_reasons.append("extra_time_or_penalty_period_detected")

    if full is None:
        quality_status = "missing"
    elif conflict_reasons:
        quality_status = "extra_time_ambiguous" if extra_time else "conflicting"
    else:
        quality_status = "complete"

    home_full, away_full = full if full is not None else (None, None)
    home_half, away_half = half if half is not None else (None, None)
    source_url = ENDPOINT.format(match_id=fixture["match_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "match_id": str(fixture["match_id"]),
        **_fixture_binding(fixture),
        "competition_key": fixture.get("competition_key"),
        "competition_name": fixture.get("competition_name"),
        "competition_id": fixture.get("competition_id"),
        "season_label": fixture.get("season_label"),
        "season_start_year": fixture.get("season_start_year"),
        "competition_regime": fixture.get("competition_regime"),
        "phase": fixture.get("phase"),
        "round": fixture.get("round"),
        "kickoff": fixture.get("kickoff"),
        "home_team": fixture.get("home_team"),
        "away_team": fixture.get("away_team"),
        "home_goals": fixture.get("home_goals"),
        "away_goals": fixture.get("away_goals"),
        "home_corners": home_full,
        "away_corners": away_full,
        "total_corners": (
            home_full + away_full if home_full is not None and away_full is not None else None
        ),
        "half_home_corners": home_half,
        "half_away_corners": away_half,
        "half_total_corners": (
            home_half + away_half if home_half is not None and away_half is not None else None
        ),
        "corner_period": "regulation_90" if quality_status == "complete" else "unverified",
        "corner_data_status": quality_status,
        "corner_exclusion_reasons": conflict_reasons,
        "corner_odds": parse_corner_odds(payload.get("cornerOdds")),
        "source_url": source_url,
        "source_collected_at": collected_at,
        "source_response_sha256": _hash_bytes(raw),
    }


def header_url(match_id: str) -> str:
    if not match_id.isdigit() or len(match_id) < 3:
        raise CornerCollectionError("match_id is invalid for the analysis-header path")
    return f"{HEADER_BASE}/{match_id[0]}/{match_id[1:3]}/{match_id}.txt"


def _fixture_extra_time(fixture: Mapping[str, Any]) -> bool:
    """Titan cup rows retain regulation/aggregate/penalty detail in raw_tail."""
    text = _canonical_json(fixture.get("raw_tail", []))
    return any(token in text.lower() for token in ("extra", "penalty", "加时", "点球")) or bool(
        re.search(r"\b90,\d+\s*-\s*\d+", text)
    )


def parse_analysis_header(
    fixture: Mapping[str, Any], raw: bytes, collected_at: str
) -> dict[str, Any]:
    """Parse Titan's light analysis-header feed and bind it to the schedule row."""
    try:
        source = raw.decode("utf-8-sig").strip()
    except UnicodeError as error:
        raise CornerCollectionError("analysis header is not valid UTF-8") from error
    fields = source.split("^")
    if len(fields) < 73:
        raise CornerCollectionError(
            f"analysis header has {len(fields)} fields; at least 73 are required"
        )
    match_id = str(fixture["match_id"])
    if fields[72].strip() != match_id:
        raise CornerCollectionError("analysis header match_id does not match fixture")
    if fields[4].strip() != "-1":
        raise CornerCollectionError("analysis header is not in the finished state")

    expected_kickoff = "".join(char for char in str(fixture.get("kickoff") or "") if char.isdigit())
    if expected_kickoff and fields[5].strip()[:12] != expected_kickoff[:12]:
        raise CornerCollectionError("analysis header kickoff does not match fixture")
    expected_home_id = fixture.get("home_team_id")
    expected_away_id = fixture.get("away_team_id")
    if expected_home_id is not None and str(expected_home_id) != fields[17].strip():
        raise CornerCollectionError("analysis header home team id does not match fixture")
    if expected_away_id is not None and str(expected_away_id) != fields[18].strip():
        raise CornerCollectionError("analysis header away team id does not match fixture")

    for position, key in ((10, "home_goals"), (11, "away_goals")):
        expected = fixture.get(key)
        if expected is not None and _as_nonnegative_int(fields[position], f"header[{position}]") != int(expected):
            raise CornerCollectionError(f"analysis header {key} does not match fixture")
    for position, key in ((26, "half_home_goals"), (27, "half_away_goals")):
        expected = fixture.get(key)
        if expected is not None and _as_nonnegative_int(fields[position], f"header[{position}]") != int(expected):
            raise CornerCollectionError(f"analysis header {key} does not match fixture")

    corner_values: tuple[int, int] | None
    if not fields[49].strip() and not fields[50].strip():
        corner_values = None
    elif not fields[49].strip() or not fields[50].strip():
        raise CornerCollectionError("analysis header contains only one corner count")
    else:
        corner_values = (
            _as_nonnegative_int(fields[49], "header.home_corners"),
            _as_nonnegative_int(fields[50], "header.away_corners"),
        )
    extra_time = _fixture_extra_time(fixture)
    home_full, away_full = corner_values if corner_values is not None else (None, None)
    if corner_values is None:
        status = "missing"
        exclusions = ["corner_fields_missing"]
    elif extra_time:
        status = "extra_time_ambiguous"
        exclusions = ["schedule_indicates_extra_time_or_penalties"]
    else:
        status = "complete"
        exclusions = []
    source_url = header_url(match_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "match_id": match_id,
        **_fixture_binding(fixture),
        "competition_key": fixture.get("competition_key"),
        "competition_name": fixture.get("competition_name"),
        "competition_id": fixture.get("competition_id"),
        "season_label": fixture.get("season_label"),
        "season_start_year": fixture.get("season_start_year"),
        "competition_regime": fixture.get("competition_regime"),
        "phase": fixture.get("phase"),
        "round": fixture.get("round"),
        "kickoff": fixture.get("kickoff"),
        "home_team_id": fixture.get("home_team_id"),
        "away_team_id": fixture.get("away_team_id"),
        "home_team": fixture.get("home_team"),
        "away_team": fixture.get("away_team"),
        "source_home_team": fields[0].strip(),
        "source_away_team": fields[1].strip(),
        "home_goals": fixture.get("home_goals"),
        "away_goals": fixture.get("away_goals"),
        "half_home_goals": fixture.get("half_home_goals"),
        "half_away_goals": fixture.get("half_away_goals"),
        "home_corners": home_full,
        "away_corners": away_full,
        "total_corners": (
            home_full + away_full if home_full is not None and away_full is not None else None
        ),
        "half_home_corners": None,
        "half_away_corners": None,
        "half_total_corners": None,
        "corner_period": "regulation_90" if status == "complete" else "unverified",
        "corner_data_status": status,
        "corner_exclusion_reasons": exclusions,
        "corner_odds": [],
        "source_url": source_url,
        "source_collected_at": collected_at,
        "source_response_sha256": _hash_bytes(raw),
    }


def parse_handicap_result(
    fixture: Mapping[str, Any], raw: bytes, collected_at: str
) -> dict[str, Any]:
    """Parse the explicit Sche corner result used when the TXT feed is incomplete."""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CornerCollectionError("handicap fallback is not valid UTF-8 JSON") from error
    sche = payload.get("Sche") if isinstance(payload, dict) else None
    if not isinstance(sche, dict):
        raise CornerCollectionError("handicap fallback has no Sche object")
    match_id = str(fixture["match_id"])
    if str(sche.get("ScheduleID") or "") != match_id:
        raise CornerCollectionError("handicap fallback match_id does not match fixture")
    if int(sche.get("MatchState", 0)) != -1:
        raise CornerCollectionError("handicap fallback is not in the finished state")
    for source_key, fixture_key in (("HomeTeamID", "home_team_id"), ("AwayTeamID", "away_team_id")):
        expected = fixture.get(fixture_key)
        if expected is not None and str(sche.get(source_key)) != str(expected):
            raise CornerCollectionError(f"handicap fallback {source_key} does not match fixture")
    home = _as_nonnegative_int(sche.get("HomeCorner"), "Sche.HomeCorner")
    away = _as_nonnegative_int(sche.get("AwayCorner"), "Sche.AwayCorner")
    extra_time = _fixture_extra_time(fixture)
    status = "extra_time_ambiguous" if extra_time else "complete"
    url = HANDICAP_ENDPOINT.format(match_id=match_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "match_id": match_id,
        **_fixture_binding(fixture),
        "competition_key": fixture.get("competition_key"),
        "competition_name": fixture.get("competition_name"),
        "competition_id": fixture.get("competition_id"),
        "season_label": fixture.get("season_label"),
        "season_start_year": fixture.get("season_start_year"),
        "competition_regime": fixture.get("competition_regime"),
        "phase": fixture.get("phase"),
        "round": fixture.get("round"),
        "kickoff": fixture.get("kickoff"),
        "home_team_id": fixture.get("home_team_id"),
        "away_team_id": fixture.get("away_team_id"),
        "home_team": fixture.get("home_team"),
        "away_team": fixture.get("away_team"),
        "source_home_team": sche.get("HomeTeam"),
        "source_away_team": sche.get("AwayTeam"),
        "home_goals": fixture.get("home_goals"),
        "away_goals": fixture.get("away_goals"),
        "half_home_goals": fixture.get("half_home_goals"),
        "half_away_goals": fixture.get("half_away_goals"),
        "home_corners": home,
        "away_corners": away,
        "total_corners": home + away,
        "half_home_corners": None,
        "half_away_corners": None,
        "half_total_corners": None,
        "corner_period": "regulation_90" if status == "complete" else "unverified",
        "corner_data_status": status,
        "corner_exclusion_reasons": (
            ["schedule_indicates_extra_time_or_penalties"] if extra_time else []
        ),
        "corner_odds": [],
        "source_url": url,
        "source_collected_at": collected_at,
        "source_response_sha256": _hash_bytes(raw),
        "source_fallback": "HandicapDataInterface.Sche",
    }


class StartRateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1.0 / requests_per_second
        self.lock = threading.Lock()
        self.next_start = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_start - now)
            self.next_start = max(now, self.next_start) + self.interval
        if delay:
            time.sleep(delay)


def fetch_fixture(
    fixture: Mapping[str, Any],
    limiter: StartRateLimiter,
    *,
    attempts: int = 6,
) -> dict[str, Any]:
    match_id = str(fixture["match_id"])
    url = header_url(match_id)
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"{BASE}/analy/shijian/{match_id}.htm",
        "Accept": "text/plain,*/*",
        "Accept-Encoding": "identity",
        "X-Requested-With": "XMLHttpRequest",
    }
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        limiter.wait()
        try:
            with urlopen(Request(url, headers=headers), timeout=45) as response:
                raw = response.read()
            if not raw:
                raise CornerCollectionError("Titan returned an empty response")
            analysis_error: CornerCollectionError | None = None
            try:
                parsed = parse_analysis_header(fixture, raw, _utc_now())
            except CornerCollectionError as error:
                # The lightweight TXT feed occasionally has a one-sided corner
                # field or stale identity metadata.  The fallback is still safe
                # because it independently binds ScheduleID, both team IDs and
                # the terminal match state before exposing corner counts.
                analysis_error = error
                parsed = None
            if parsed is not None and parsed["corner_data_status"] != "missing":
                return parsed
            fallback_url = HANDICAP_ENDPOINT.format(match_id=match_id)
            limiter.wait()
            with urlopen(Request(fallback_url, headers=headers), timeout=45) as response:
                fallback_raw = response.read()
            fallback = parse_handicap_result(fixture, fallback_raw, _utc_now())
            fallback["analysis_header_source_url"] = url
            fallback["analysis_header_response_sha256"] = _hash_bytes(raw)
            fallback["analysis_header_parse_error"] = (
                f"{type(analysis_error).__name__}: {analysis_error}"
                if analysis_error is not None
                else None
            )
            return fallback
        except HTTPError as error:
            last_error = error
            if error.code not in RETRYABLE_HTTP:
                break
        except (URLError, TimeoutError, OSError, CornerCollectionError) as error:
            last_error = error
        if attempt < attempts:
            time.sleep(min(45.0, 1.5 * (2 ** (attempt - 1))) + random.uniform(0.2, 1.0))
    assert last_error is not None
    return {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "match_id": match_id,
        **_fixture_binding(fixture),
        "competition_key": fixture.get("competition_key"),
        "competition_name": fixture.get("competition_name"),
        "season_label": fixture.get("season_label"),
        "kickoff": fixture.get("kickoff"),
        "home_team": fixture.get("home_team"),
        "away_team": fixture.get("away_team"),
        "home_goals": fixture.get("home_goals"),
        "away_goals": fixture.get("away_goals"),
        "home_corners": None,
        "away_corners": None,
        "total_corners": None,
        "half_home_corners": None,
        "half_away_corners": None,
        "half_total_corners": None,
        "corner_period": "unverified",
        "corner_data_status": "fetch_error",
        "corner_exclusion_reasons": [f"{type(last_error).__name__}: {last_error}"],
        "corner_odds": [],
        "source_url": url,
        "source_collected_at": _utc_now(),
        "source_response_sha256": None,
    }


def load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise CornerCollectionError(
                    f"checkpoint {path} line {line_number} is invalid JSON"
                ) from error
            if not isinstance(record, dict):
                raise CornerCollectionError(
                    f"checkpoint {path} line {line_number} must be an object"
                )
            match_id = str(record.get("match_id") or "")
            if not match_id.isdigit():
                raise CornerCollectionError(
                    f"checkpoint {path} line {line_number} has invalid match_id"
                )
            records[match_id] = record
    return records


def checkpoint_matches_fixture(
    record: Mapping[str, Any], fixture: Mapping[str, Any]
) -> bool:
    """Require a checkpoint row to bind to the complete scheduled fixture identity."""
    if str(record.get("match_id") or "") != str(fixture.get("match_id") or ""):
        return False
    if record.get("schedule_fixture_sha256") != schedule_fixture_sha256(fixture):
        return False
    for field in CHECKPOINT_IDENTITY_FIELDS:
        expected = fixture.get(field)
        actual = record.get(field)
        if field in {"season_start_year", "home_team_id", "away_team_id", "home_goals", "away_goals"}:
            if expected is None or actual is None:
                if expected is not actual:
                    return False
                continue
            if str(actual) != str(expected):
                return False
        elif str(actual or "").strip() != str(expected or "").strip():
            return False
    return True


def upgrade_legacy_checkpoint_record(
    record: Mapping[str, Any], fixture: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Bind a pre-v2 row to new schedule time fields without refetching it.

    The migration is deliberately narrow: it applies only when all fields that
    existed in the legacy row match this fixture, the new binding fields are
    wholly absent, and the old source evidence has the expected collector
    shape.  A 2026 J1 row labelled ``standard`` does not match the regenerated
    special-regime schedule and is therefore fetched again.
    """

    if checkpoint_matches_fixture(record, fixture):
        return dict(record)
    new_fields = (
        "kickoff_utc",
        "kickoff_epoch",
        "source_timezone",
        "schedule_fixture_sha256",
    )
    if any(field in record for field in new_fields):
        return None
    if (
        record.get("schema_version") != SCHEMA_VERSION
        or record.get("collector_version") != COLLECTOR_VERSION
        or str(record.get("match_id") or "") != str(fixture.get("match_id") or "")
    ):
        return None
    status = str(record.get("corner_data_status") or "")
    if status not in {
        "complete",
        "missing",
        "conflicting",
        "extra_time_ambiguous",
        "fetch_error",
    }:
        return None
    if status != "fetch_error":
        source_hash = record.get("source_response_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", source_hash
        ):
            return None
    source_url = str(record.get("source_url") or "")
    allowed_source_urls = {
        header_url(str(fixture["match_id"])),
        HANDICAP_ENDPOINT.format(match_id=str(fixture["match_id"])),
    }
    if source_url not in allowed_source_urls:
        return None

    numeric_fields = {
        "season_start_year",
        "home_team_id",
        "away_team_id",
        "home_goals",
        "away_goals",
    }
    for field in CHECKPOINT_IDENTITY_FIELDS:
        if field in {"kickoff_utc", "kickoff_epoch", "source_timezone"}:
            continue
        expected = fixture.get(field)
        actual = record.get(field)
        if field == "competition_regime":
            actual_regime = str(actual or "").strip()
            if actual_regime == "standard":
                actual_regime = "regular"
            if actual_regime != str(expected or "").strip():
                return None
        elif field in numeric_fields:
            if expected is None or actual is None:
                if expected is not actual:
                    return None
            elif str(actual) != str(expected):
                return None
        elif str(actual or "").strip() != str(expected or "").strip():
            return None

    upgraded = dict(record)
    upgraded.update(_fixture_binding(fixture))
    return upgraded if checkpoint_matches_fixture(upgraded, fixture) else None


def pending_fixtures(
    fixtures: Sequence[Mapping[str, Any]],
    checkpoint: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Retry transient fetch failures while retaining terminal QA outcomes."""

    return [
        fixture
        for fixture in fixtures
        if (
            str(fixture["match_id"]) not in checkpoint
            or not checkpoint_matches_fixture(
                checkpoint[str(fixture["match_id"])], fixture
            )
            or checkpoint[str(fixture["match_id"])].get("corner_data_status")
            == "fetch_error"
        )
    ]


def _qa(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_competition: dict[str, dict[str, int]] = {}
    complete_with_odds = 0
    for record in records:
        status = str(record.get("corner_data_status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        competition = str(record.get("competition_key") or "unknown")
        block = by_competition.setdefault(competition, {"total": 0, "complete": 0})
        block["total"] += 1
        if status == "complete":
            block["complete"] += 1
            complete_with_odds += bool(record.get("corner_odds"))
    for block in by_competition.values():
        block["coverage"] = (
            round(block["complete"] / block["total"], 8) if block["total"] else 0.0
        )
    return {
        "matches": len(records),
        "status_counts": dict(sorted(by_status.items())),
        "complete_with_corner_odds": complete_with_odds,
        "by_competition": dict(sorted(by_competition.items())),
    }


def collect(
    fixtures: Sequence[dict[str, Any]],
    output_dir: Path,
    *,
    workers: int,
    requests_per_second: float,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "corner_history.partial.ndjson"
    final_path = output_dir / "corner_history.json"
    qa_path = output_dir / "corner_history_qa.json"
    loaded_checkpoint = load_checkpoint(checkpoint_path)
    fixture_by_id = {str(fixture["match_id"]): fixture for fixture in fixtures}
    if len(fixture_by_id) != len(fixtures):
        raise CornerCollectionError("fixtures contain duplicate match_id values")
    completed: dict[str, dict[str, Any]] = {}
    migrated_legacy_records = 0
    for match_id, record in loaded_checkpoint.items():
        fixture = fixture_by_id.get(match_id)
        if fixture is None:
            continue
        upgraded = upgrade_legacy_checkpoint_record(record, fixture)
        if upgraded is None:
            continue
        completed[match_id] = upgraded
        migrated_legacy_records += not checkpoint_matches_fixture(record, fixture)
    pending = pending_fixtures(fixtures, completed)
    print(
        _canonical_json(
            {
                "event": "corner-collection-start",
                "fixtures": len(fixtures),
                "resumed": len(completed),
                "ignored_checkpoint_records": len(loaded_checkpoint) - len(completed),
                "migrated_legacy_records": migrated_legacy_records,
                "pending": len(pending),
                "workers": workers,
            }
        ),
        flush=True,
    )
    limiter = StartRateLimiter(requests_per_second)
    with checkpoint_path.open("a", encoding="utf-8", newline="\n") as checkpoint:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(fetch_fixture, fixture, limiter): fixture
                for fixture in pending
            }
            since_flush = 0
            for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                record = future.result()
                fixture = futures[future]
                if not checkpoint_matches_fixture(record, fixture):
                    raise CornerCollectionError(
                        f"fetched record {record.get('match_id')} does not match scheduled fixture identity"
                    )
                completed[str(record["match_id"])] = record
                checkpoint.write(_canonical_json(record) + "\n")
                since_flush += 1
                if since_flush >= 25:
                    checkpoint.flush()
                    os.fsync(checkpoint.fileno())
                    since_flush = 0
                if index % 100 == 0 or index == len(pending):
                    print(
                        _canonical_json(
                            {
                                "event": "corner-collection-progress",
                                "completed_this_run": index,
                                "pending_this_run": len(pending),
                                "total_saved": len(completed),
                            }
                        ),
                        flush=True,
                    )
        checkpoint.flush()
        os.fsync(checkpoint.fileno())

    current_records = [
        completed[match_id]
        for match_id in fixture_by_id
        if match_id in completed
        and checkpoint_matches_fixture(completed[match_id], fixture_by_id[match_id])
    ]
    if len(current_records) != len(fixtures):
        raise CornerCollectionError(
            "final corner history does not cover the exact current schedule ID set"
        )
    ordered = sorted(
        current_records,
        key=lambda row: (str(row.get("kickoff") or ""), int(row["match_id"])),
    )
    qa = _qa(ordered)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "generated_at": _utc_now(),
        "source": HEADER_BASE,
        "schedule_fixture_set_sha256": _hash_bytes(
            _canonical_json(
                [
                    {
                        "match_id": str(fixture["match_id"]),
                        "schedule_fixture_sha256": schedule_fixture_sha256(fixture),
                    }
                    for fixture in sorted(
                        fixtures,
                        key=lambda item: (
                            str(item.get("kickoff_utc") or ""),
                            int(item["match_id"]),
                        ),
                    )
                ]
            ).encode("utf-8")
        ),
        "historical_corner_price_policy": "research_only_without_verified_collection_timestamp",
        "qa": qa,
        "matches": ordered,
    }
    payload["bundle_hash"] = _hash_bytes(_canonical_json(payload).encode("utf-8"))
    _atomic_json(final_path, payload)
    _atomic_json(qa_path, qa)
    print(_canonical_json({"event": "corner-collection-complete", **qa}), flush=True)
    return final_path, qa_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect historical Titan corner data")
    parser.add_argument("--schedule", action="append", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--requests-per-second", type=float, default=6.0)
    parser.add_argument("--limit", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.workers <= 32:
        raise SystemExit("error: --workers must be between 1 and 32")
    if not 0.1 <= args.requests_per_second <= 20.0:
        raise SystemExit("error: --requests-per-second must be between 0.1 and 20")
    try:
        fixtures = load_schedule_files([path.resolve() for path in args.schedule])
        if args.limit is not None:
            if args.limit < 1:
                raise CornerCollectionError("--limit must be positive")
            fixtures = fixtures[: args.limit]
        collect(
            fixtures,
            args.output_dir.resolve(),
            workers=args.workers,
            requests_per_second=args.requests_per_second,
        )
    except CornerCollectionError as error:
        raise SystemExit(f"error: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

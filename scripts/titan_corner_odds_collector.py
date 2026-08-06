#!/usr/bin/env python3
"""Collect timestamp-proven pre-kickoff corner prices from Titan.

Titan's type=4 history endpoint mixes opening, early, real-time pre-match and
running prices in one response.  This collector deliberately keeps only rows
whose kind and timestamps prove that they existed before kickoff and whose
in-play fields are blank.  The append-only request checkpoint makes a large
match/company collection resumable without treating a partial match as done.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import random
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_URL = "https://m.titan007.com"
ENDPOINT = BASE_URL + "/HandicapDataInterface.ashx"
SCHEMA_VERSION = "1.0.0"
COLLECTOR_VERSION = "titan-corner-odds/1.0.0"
DEFAULT_COMPANY_IDS = (3, 8, 47)
SAFE_KINDS = frozenset({"INITIAL", "EARLY", "REAL"})
RETRYABLE_HTTP = frozenset({408, 425, 429, 442, 500, 502, 503, 504})
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0 Safari/537.36"
)


class CornerOddsCollectionError(ValueError):
    """Raised when input or source data cannot be used safely."""


@dataclass(frozen=True)
class MarketSpec:
    key: str
    period: str
    market_type: str
    oddskind: int
    is_half: int
    home_price_label: str
    line_label: str
    away_price_label: str


FULL_TOTAL = MarketSpec(
    key="full_total",
    period="full_match",
    market_type="corner_total",
    oddskind=2,
    is_half=0,
    home_price_label="over_odds",
    line_label="total_corners_line",
    away_price_label="under_odds",
)
FULL_HANDICAP = MarketSpec(
    key="full_handicap",
    period="full_match",
    market_type="corner_handicap",
    oddskind=1,
    is_half=0,
    home_price_label="home_odds",
    line_label="home_corner_handicap",
    away_price_label="away_odds",
)
HALF_TOTAL = MarketSpec(
    key="half_total",
    period="first_half",
    market_type="corner_total",
    oddskind=2,
    is_half=1,
    home_price_label="over_odds",
    line_label="first_half_total_corners_line",
    away_price_label="under_odds",
)
MARKET_ORDER = {
    FULL_TOTAL.key: 0,
    FULL_HANDICAP.key: 1,
    HALF_TOTAL.key: 2,
}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _atomic_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, (_canonical_json(value) + "\n").encode("utf-8"))


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CornerOddsCollectionError(f"{field} must be a positive integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise CornerOddsCollectionError(f"{field} must be a positive integer")
    if number <= 0:
        raise CornerOddsCollectionError(f"{field} must be a positive integer")
    return number


def _epoch(value: Any, field: str) -> int:
    return _positive_int(value, field)


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or value is None or value == "":
        raise CornerOddsCollectionError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise CornerOddsCollectionError(f"{field} must be a finite number") from error
    if not math.isfinite(number):
        raise CornerOddsCollectionError(f"{field} must be a finite number")
    return number


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return None


def _epoch_iso(value: int) -> str:
    try:
        return (
            datetime.fromtimestamp(value, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError) as error:
        raise CornerOddsCollectionError(
            "epoch is outside the supported range"
        ) from error


def _match_id(raw: Mapping[str, Any], source: str) -> str:
    value = raw.get("match_id")
    if value is None:
        value = raw.get("schedule_id")
    if value is None:
        value = raw.get("ScheduleID")
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0:
        raise CornerOddsCollectionError(f"{source} has an invalid match_id")
    return text


def _fixture_fingerprint(fixture: Mapping[str, Any]) -> str:
    return _hash_bytes(_canonical_json(dict(fixture)).encode("utf-8"))


def _fixture_summary(fixture: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "match_id",
        "competition_key",
        "competition_name",
        "competition_id",
        "season_label",
        "season_start_year",
        "competition_regime",
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
        "half_home_goals",
        "half_away_goals",
        "completed",
    )
    return {key: fixture.get(key) for key in fields if key in fixture}


def load_schedule_files(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Load schedule JSON files and reject conflicting duplicate match IDs."""
    fixtures: dict[str, dict[str, Any]] = {}
    identity_fields = (
        "competition_key",
        "season_label",
        "season_start_year",
        "phase",
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
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CornerOddsCollectionError(
                f"cannot read schedule JSON {path}: {error}"
            ) from error
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("matches")
        else:
            rows = None
        if not isinstance(rows, list):
            raise CornerOddsCollectionError(
                f"schedule JSON {path} must be a list or contain matches[]"
            )
        for index, raw in enumerate(rows):
            source = f"{path} matches[{index}]"
            if not isinstance(raw, dict):
                raise CornerOddsCollectionError(f"{source} must be an object")
            fixture = dict(raw)
            match_id = _match_id(fixture, source)
            fixture["match_id"] = match_id
            previous = fixtures.get(match_id)
            if previous is not None:
                conflicts = [
                    field
                    for field in identity_fields
                    if previous.get(field) is not None
                    and fixture.get(field) is not None
                    and previous.get(field) != fixture.get(field)
                ]
                if conflicts:
                    raise CornerOddsCollectionError(
                        f"match_id {match_id} conflicts across schedules: "
                        + ", ".join(conflicts)
                    )
                merged = dict(previous)
                for key, value in fixture.items():
                    if key not in merged or merged[key] is None:
                        merged[key] = value
                fixtures[match_id] = merged
            else:
                fixtures[match_id] = fixture
    return sorted(
        fixtures.values(),
        key=lambda item: (str(item.get("kickoff") or ""), int(item["match_id"])),
    )


def parse_company_ids(values: Sequence[str] | None) -> list[int]:
    """Parse repeatable and comma-separated --company-id arguments."""
    if not values:
        return list(DEFAULT_COMPANY_IDS)
    company_ids: set[int] = set()
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if not token:
                raise CornerOddsCollectionError("--company-id contains an empty value")
            company_ids.add(_positive_int(token, "company_id"))
    return sorted(company_ids)


def source_url(match_id: str, spec: MarketSpec, company_id: int) -> str:
    query = urlencode(
        (
            ("scheid", match_id),
            ("type", "4"),
            ("oddskind", str(spec.oddskind)),
            ("companyid", str(company_id)),
            ("isHalf", str(spec.is_half)),
        )
    )
    return f"{ENDPOINT}?{query}"


def _request_key(match_id: str, spec: MarketSpec, company_id: int) -> str:
    return f"{match_id}:{spec.key}:{company_id}"


def build_request_plan(
    fixtures: Sequence[Mapping[str, Any]],
    company_ids: Sequence[int],
    *,
    include_half_total: bool,
) -> list[dict[str, Any]]:
    specs = [FULL_TOTAL, FULL_HANDICAP]
    if include_half_total:
        specs.append(HALF_TOTAL)
    normalized_company_ids = [
        _positive_int(value, "company_id") for value in company_ids
    ]
    if len(set(normalized_company_ids)) != len(normalized_company_ids):
        raise CornerOddsCollectionError("company_ids contains duplicates")
    plan: list[dict[str, Any]] = []
    seen_match_ids: set[str] = set()
    for fixture in fixtures:
        # Fail before any network request when an old/ambiguous schedule is
        # supplied.  parse_source_response repeats this validation defensively.
        _fixture_kickoff_epoch(fixture)
        match_id = str(fixture["match_id"])
        if match_id in seen_match_ids:
            raise CornerOddsCollectionError(
                "fixtures contain duplicate match_id values"
            )
        seen_match_ids.add(match_id)
        fingerprint = _fixture_fingerprint(fixture)
        for spec in specs:
            for company_id in normalized_company_ids:
                plan.append(
                    {
                        "request_key": _request_key(match_id, spec, company_id),
                        "fixture": dict(fixture),
                        "fixture_fingerprint": fingerprint,
                        "match_id": match_id,
                        "company_id": int(company_id),
                        "spec": spec,
                        "source_url": source_url(match_id, spec, int(company_id)),
                    }
                )
    return plan


def _source_zone(fixture: Mapping[str, Any]) -> tzinfo:
    name = str(fixture.get("source_timezone") or "").strip()
    if not name:
        raise CornerOddsCollectionError(
            "fixture.source_timezone is required for timezone-less kickoff values"
        )
    # Windows Python installations may not ship the IANA tzdata package.  The
    # source contracts used here are UTC and modern China Standard Time (China
    # has had no DST during the 2020+ collection window), so keep those two
    # deterministic without an optional runtime dependency.
    if name in {"UTC", "Etc/UTC", "Z"}:
        return timezone.utc
    if name == "Asia/Shanghai":
        return timezone(timedelta(hours=8), name)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise CornerOddsCollectionError(
            f"fixture.source_timezone is invalid: {name}"
        ) from error


def _time_epoch(
    value: Any,
    field: str,
    *,
    fixture: Mapping[str, Any],
    numeric_epoch_only: bool = False,
) -> int:
    if isinstance(value, bool) or value is None or value == "":
        raise CornerOddsCollectionError(f"{field} is missing")
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 10
    ):
        return _epoch(value, field)
    if numeric_epoch_only:
        raise CornerOddsCollectionError(f"{field} must be a Unix epoch")
    text = str(value).strip()
    parsed: datetime
    if text.isdigit() and len(text) in {12, 14}:
        try:
            parsed = datetime.strptime(
                text, "%Y%m%d%H%M%S" if len(text) == 14 else "%Y%m%d%H%M"
            )
        except ValueError as error:
            raise CornerOddsCollectionError(f"{field} is invalid") from error
    else:
        normalized = text.replace("/", "-")
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise CornerOddsCollectionError(f"{field} is invalid") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_source_zone(fixture))
    return int(parsed.timestamp())


def _fixture_kickoff_epoch(fixture: Mapping[str, Any]) -> int:
    # ``kickoff_epoch`` is the schedule crawler's unambiguous contract.  Older
    # schedules that only carried a wall-clock string must be regenerated; a
    # collector must never guess their timezone after fetching market data.
    explicit = fixture.get("kickoff_epoch")
    if explicit is None or explicit == "":
        raise CornerOddsCollectionError(
            "fixture.kickoff_epoch is required; regenerate the schedule bundle"
        )
    _source_zone(fixture)
    candidates = [
        _time_epoch(
            explicit,
            "fixture.kickoff_epoch",
            fixture=fixture,
            numeric_epoch_only=True,
        )
    ]
    for field in ("kickoff_utc", "kickoff"):
        value = fixture.get(field)
        if value is not None and str(value).strip():
            candidates.append(_time_epoch(value, f"fixture.{field}", fixture=fixture))
    return min(candidates)


def select_verified_prematch_snapshots(
    detail_list: Any,
    *,
    kickoff_epoch: int,
    spec: MarketSpec,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Return only records proven to be non-running and strictly pre-kickoff."""
    if not isinstance(detail_list, list):
        raise CornerOddsCollectionError("DetailList must be an array")
    snapshots: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    duplicate_count = 0
    seen: set[tuple[Any, ...]] = set()

    def reject(reason: str) -> None:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    for index, raw in enumerate(detail_list):
        if not isinstance(raw, dict):
            reject("record_not_object")
            continue
        reasons: list[str] = []
        kind = str(raw.get("Kind") or "").strip().upper()
        if kind == "RUNNING":
            reasons.append("running_kind")
        elif kind not in SAFE_KINDS:
            reasons.append("kind_not_verified_prematch")
        if not _blank(raw.get("HappenTime")):
            reasons.append("happen_time_present")
        if not _blank(raw.get("Score")):
            reasons.append("score_present")
        try:
            modify_epoch = _epoch(raw.get("ModifyTime"), "DetailList.ModifyTime")
        except CornerOddsCollectionError:
            modify_epoch = None
            reasons.append("modify_time_invalid")
        if modify_epoch is not None and modify_epoch >= kickoff_epoch:
            reasons.append("at_or_after_kickoff")

        home_odds: float | None = None
        line: float | None = None
        away_odds: float | None = None
        try:
            home_odds = _finite_number(raw.get("HomeOdds"), "HomeOdds")
            line = _finite_number(raw.get("DrawOdds"), "DrawOdds")
            away_odds = _finite_number(raw.get("AwayOdds"), "AwayOdds")
        except CornerOddsCollectionError:
            reasons.append("quote_not_finite")
        if home_odds is not None and away_odds is not None:
            if home_odds <= 0.0 or away_odds <= 0.0:
                reasons.append("price_not_positive")
        if line is not None and spec.market_type == "corner_total" and line <= 0.0:
            reasons.append("total_line_not_positive")

        if reasons:
            for reason in set(reasons):
                reject(reason)
            continue
        assert modify_epoch is not None
        assert home_odds is not None and line is not None and away_odds is not None
        is_close = _optional_bool(raw.get("IsClose"))
        identity = (modify_epoch, kind, home_odds, line, away_odds, is_close)
        if identity in seen:
            duplicate_count += 1
            continue
        seen.add(identity)
        snapshot = {
            "kind": kind,
            "modify_time_epoch": modify_epoch,
            "modify_time_utc": _epoch_iso(modify_epoch),
            "seconds_before_kickoff": kickoff_epoch - modify_epoch,
            "home_odds": home_odds,
            "line": line,
            "away_odds": away_odds,
            "is_close": is_close,
            "source_record_index": index,
        }
        snapshots.append(snapshot)

    snapshots.sort(
        key=lambda item: (
            item["modify_time_epoch"],
            item["kind"],
            item["home_odds"],
            item["line"],
            item["away_odds"],
        )
    )
    flags: list[str] = []
    if snapshots:
        latest_epoch = snapshots[-1]["modify_time_epoch"]
        latest_rows = [
            item for item in snapshots if item["modify_time_epoch"] == latest_epoch
        ]
        if len(latest_rows) > 1:
            flags.append("latest_timestamp_has_multiple_quotes")
        if snapshots[-1]["is_close"] is True:
            flags.append("latest_verified_market_is_closed")
    stats = {
        "source_records": len(detail_list),
        "verified_source_records": len(snapshots) + duplicate_count,
        "verified_unique_snapshots": len(snapshots),
        "duplicate_verified_records_removed": duplicate_count,
        "rejected_source_records": len(detail_list) - len(snapshots) - duplicate_count,
        "rejection_reason_counts": dict(sorted(reason_counts.items())),
    }
    return snapshots, stats, flags


def parse_source_response(
    request_item: Mapping[str, Any], raw: bytes, collected_at: str
) -> dict[str, Any]:
    """Validate one type=4 response and retain only safe pre-match prices."""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CornerOddsCollectionError(
            "Titan response is not valid UTF-8 JSON"
        ) from error
    if not isinstance(payload, dict):
        raise CornerOddsCollectionError("Titan response must be a JSON object")
    sche = payload.get("Sche")
    if not isinstance(sche, dict):
        raise CornerOddsCollectionError("Titan response has no Sche object")
    match_id = str(request_item["match_id"])
    if str(sche.get("ScheduleID") or "") != match_id:
        raise CornerOddsCollectionError("Sche.ScheduleID does not match the request")
    fixture = request_item["fixture"]
    if not isinstance(fixture, Mapping):
        raise CornerOddsCollectionError("request fixture is invalid")
    for source_key, fixture_key in (
        ("HomeTeamID", "home_team_id"),
        ("AwayTeamID", "away_team_id"),
    ):
        expected = fixture.get(fixture_key)
        if expected not in {None, ""} and str(sche.get(source_key)) != str(expected):
            raise CornerOddsCollectionError(
                f"Sche.{source_key} does not match the schedule fixture"
            )
    fixture_epoch = _fixture_kickoff_epoch(fixture)
    source_kickoff_epoch = _time_epoch(
        sche.get("MatchTime"), "Sche.MatchTime", fixture=fixture
    )
    effective_kickoff_epoch = min(fixture_epoch, source_kickoff_epoch)
    spec = request_item["spec"]
    if not isinstance(spec, MarketSpec):
        raise CornerOddsCollectionError("request market spec is invalid")
    snapshots, filtering, flags = select_verified_prematch_snapshots(
        payload.get("DetailList"),
        kickoff_epoch=effective_kickoff_epoch,
        spec=spec,
    )
    kickoff_delta_seconds = source_kickoff_epoch - fixture_epoch
    if abs(kickoff_delta_seconds) > 300:
        flags.append("fixture_kickoff_differs_from_source_by_more_than_300_seconds")
    source_hash = _hash_bytes(raw)
    status = "complete" if snapshots else "no_verified_snapshot"
    return {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "request_key": request_item["request_key"],
        "fixture_fingerprint": request_item["fixture_fingerprint"],
        "fixture": _fixture_summary(fixture),
        "match_id": match_id,
        "company_id": int(request_item["company_id"]),
        "market_key": spec.key,
        "period": spec.period,
        "market_type": spec.market_type,
        "oddskind": spec.oddskind,
        "is_half": bool(spec.is_half),
        "price_semantics": {
            "home_odds": spec.home_price_label,
            "line": spec.line_label,
            "away_odds": spec.away_price_label,
        },
        "status": status,
        "selection_policy": {
            "allowed_kinds": sorted(SAFE_KINDS),
            "modify_time_strictly_before_match_time": True,
            "modify_time_strictly_before_schedule_kickoff": True,
            "effective_cutoff_uses_earlier_kickoff": True,
            "happen_time_must_be_blank": True,
            "score_must_be_blank": True,
            "running_rows_allowed": False,
        },
        "source_match_time_epoch": source_kickoff_epoch,
        "source_match_time_utc": _epoch_iso(source_kickoff_epoch),
        "fixture_kickoff_epoch": fixture_epoch,
        "fixture_kickoff_utc": _epoch_iso(fixture_epoch),
        "effective_prematch_cutoff_epoch": effective_kickoff_epoch,
        "effective_prematch_cutoff_utc": _epoch_iso(effective_kickoff_epoch),
        "fixture_kickoff_delta_seconds": kickoff_delta_seconds,
        "source_match_state": sche.get("MatchState"),
        "source_home_team_id": sche.get("HomeTeamID"),
        "source_away_team_id": sche.get("AwayTeamID"),
        "source_home_team": sche.get("HomeTeam"),
        "source_away_team": sche.get("AwayTeam"),
        "snapshots": snapshots,
        "opening_snapshot": snapshots[0] if snapshots else None,
        "pre_kickoff_snapshot": snapshots[-1] if snapshots else None,
        "filtering_qa": filtering,
        "qa_flags": sorted(set(flags)),
        "source_url": request_item["source_url"],
        "source_collected_at": collected_at,
        "source_response_sha256": source_hash,
        "source_response_bytes": len(raw),
        "error": None,
    }


def _failure_record(
    request_item: Mapping[str, Any],
    error: BaseException,
    *,
    attempts_used: int,
    raw: bytes | None,
) -> dict[str, Any]:
    spec = request_item["spec"]
    fixture = request_item["fixture"]
    assert isinstance(spec, MarketSpec)
    assert isinstance(fixture, Mapping)
    return {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "request_key": request_item["request_key"],
        "fixture_fingerprint": request_item["fixture_fingerprint"],
        "fixture": _fixture_summary(fixture),
        "match_id": str(request_item["match_id"]),
        "company_id": int(request_item["company_id"]),
        "market_key": spec.key,
        "period": spec.period,
        "market_type": spec.market_type,
        "oddskind": spec.oddskind,
        "is_half": bool(spec.is_half),
        "price_semantics": {
            "home_odds": spec.home_price_label,
            "line": spec.line_label,
            "away_odds": spec.away_price_label,
        },
        "status": "fetch_error",
        "snapshots": [],
        "opening_snapshot": None,
        "pre_kickoff_snapshot": None,
        "filtering_qa": None,
        "qa_flags": ["request_failed_after_retries"],
        "source_url": request_item["source_url"],
        "source_collected_at": _utc_now(),
        "source_response_sha256": _hash_bytes(raw) if raw is not None else None,
        "source_response_bytes": len(raw) if raw is not None else None,
        "attempts_used": attempts_used,
        "error": {"type": type(error).__name__, "message": str(error)},
    }


class StartRateLimiter:
    """Thread-safe limiter for request start times, shared across all workers."""

    def __init__(self, requests_per_second: float) -> None:
        if not math.isfinite(requests_per_second) or requests_per_second <= 0.0:
            raise CornerOddsCollectionError("requests_per_second must be positive")
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


def _store_raw(raw_dir: Path, raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    path = raw_dir / "sha256" / digest[:2] / f"{digest}.json"
    if not path.is_file():
        _atomic_bytes(path, raw)
    return path.relative_to(raw_dir.parent).as_posix()


def fetch_request(
    request_item: Mapping[str, Any],
    limiter: StartRateLimiter,
    *,
    attempts: int,
    timeout: float,
    raw_dir: Path | None,
) -> dict[str, Any]:
    """Fetch and parse one match/market/company request with bounded retries."""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": f"{BASE_URL}/corner/{request_item['match_id']}.htm",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Encoding": "identity",
        "X-Requested-With": "XMLHttpRequest",
    }
    last_error: BaseException | None = None
    last_raw: bytes | None = None
    for attempt in range(1, attempts + 1):
        last_raw = None
        limiter.wait()
        try:
            with urlopen(
                Request(str(request_item["source_url"]), headers=headers),
                timeout=timeout,
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            last_raw = raw
            if not raw:
                raise CornerOddsCollectionError("Titan returned an empty response")
            if len(raw) > MAX_RESPONSE_BYTES:
                raise CornerOddsCollectionError(
                    "Titan response exceeds the safety limit"
                )
            result = parse_source_response(request_item, raw, _utc_now())
            result["attempts_used"] = attempt
            if raw_dir is not None:
                result["source_response_path"] = _store_raw(raw_dir, raw)
            return result
        except HTTPError as error:
            last_error = error
            if error.code not in RETRYABLE_HTTP:
                break
        except (URLError, TimeoutError, OSError, CornerOddsCollectionError) as error:
            last_error = error
        if attempt < attempts:
            delay = min(45.0, 1.5 * (2 ** (attempt - 1)))
            time.sleep(delay + random.uniform(0.2, 1.0))
    assert last_error is not None
    return _failure_record(
        request_item,
        last_error,
        attempts_used=attempt,
        raw=last_raw,
    )


def load_checkpoint(
    path: Path, *, repair_truncated_tail: bool = False
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return records
    try:
        raw_checkpoint = path.read_bytes()
    except OSError as error:
        raise CornerOddsCollectionError(
            f"cannot read checkpoint {path}: {error}"
        ) from error
    lines = raw_checkpoint.splitlines(keepends=True)
    valid_bytes = 0
    for line_number, raw_line in enumerate(lines, start=1):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeError as error:
            is_truncated_tail = line_number == len(
                lines
            ) and not raw_checkpoint.endswith((b"\n", b"\r"))
            if repair_truncated_tail and is_truncated_tail:
                with path.open("r+b") as handle:
                    handle.truncate(valid_bytes)
                break
            raise CornerOddsCollectionError(
                f"checkpoint {path} line {line_number} is invalid UTF-8"
            ) from error
        if not line.strip():
            valid_bytes += len(raw_line)
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            is_truncated_tail = line_number == len(
                lines
            ) and not raw_checkpoint.endswith((b"\n", b"\r"))
            if repair_truncated_tail and is_truncated_tail:
                with path.open("r+b") as handle:
                    handle.truncate(valid_bytes)
                break
            raise CornerOddsCollectionError(
                f"checkpoint {path} line {line_number} is invalid JSON"
            ) from error
        if not isinstance(record, dict):
            raise CornerOddsCollectionError(
                f"checkpoint {path} line {line_number} is not an object"
            )
        key = str(record.get("request_key") or "")
        if key.count(":") != 2:
            raise CornerOddsCollectionError(
                f"checkpoint {path} line {line_number} has invalid request_key"
            )
        records[key] = record
        valid_bytes += len(raw_line)
    return records


def _record_matches_request_identity(
    record: Mapping[str, Any], request_item: Mapping[str, Any]
) -> bool:
    spec = request_item.get("spec")
    fixture = request_item.get("fixture")
    if not isinstance(spec, MarketSpec) or not isinstance(fixture, Mapping):
        return False
    return (
        record.get("schema_version") == SCHEMA_VERSION
        and record.get("collector_version") == COLLECTOR_VERSION
        and record.get("request_key") == request_item.get("request_key")
        and record.get("fixture_fingerprint") == request_item.get("fixture_fingerprint")
        and record.get("fixture") == _fixture_summary(fixture)
        and str(record.get("match_id") or "") == str(request_item.get("match_id") or "")
        and record.get("company_id") == int(request_item["company_id"])
        and record.get("market_key") == spec.key
        and record.get("period") == spec.period
        and record.get("market_type") == spec.market_type
        and record.get("oddskind") == spec.oddskind
        and record.get("is_half") is bool(spec.is_half)
        and record.get("source_url") == request_item.get("source_url")
    )


def _raw_response_path(output_dir: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    relative = Path(value)
    if relative.is_absolute():
        return None
    root = (output_dir / "raw").resolve()
    candidate = (output_dir / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _can_resume(
    record: Mapping[str, Any],
    request_item: Mapping[str, Any],
    output_dir: Path,
) -> bool:
    """Resume only a source-replayable record bound to this exact request."""

    if not _record_matches_request_identity(record, request_item) or record.get(
        "status"
    ) not in {"complete", "no_verified_snapshot"}:
        return False
    raw_path = _raw_response_path(output_dir, record.get("source_response_path"))
    if raw_path is None or not raw_path.is_file():
        return False
    try:
        raw = raw_path.read_bytes()
        attempts_used = record.get("attempts_used")
        if (
            isinstance(attempts_used, bool)
            or not isinstance(attempts_used, int)
            or attempts_used <= 0
        ):
            return False
        if not raw or len(raw) > MAX_RESPONSE_BYTES:
            return False
        if record.get("source_response_sha256") != _hash_bytes(raw):
            return False
        if record.get("source_response_bytes") != len(raw):
            return False
        digest = hashlib.sha256(raw).hexdigest()
        expected_relative = f"raw/sha256/{digest[:2]}/{digest}.json"
        if record.get("source_response_path") != expected_relative:
            return False
        collected_at = record.get("source_collected_at")
        if not isinstance(collected_at, str) or not collected_at:
            return False
        normalized_time = (
            collected_at[:-1] + "+00:00" if collected_at.endswith("Z") else collected_at
        )
        parsed_time = datetime.fromisoformat(normalized_time)
        if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
            return False
        replayed = parse_source_response(request_item, raw, collected_at)
        replayed["attempts_used"] = attempts_used
        replayed["source_response_path"] = record.get("source_response_path")
        return _canonical_json(replayed) == _canonical_json(dict(record))
    except (OSError, CornerOddsCollectionError, TypeError, ValueError):
        return False


def _record_sort_key(record: Mapping[str, Any]) -> tuple[int, int, int]:
    return (
        int(record["match_id"]),
        MARKET_ORDER[str(record["market_key"])],
        int(record["company_id"]),
    )


def build_qa(
    records: Sequence[Mapping[str, Any]],
    *,
    fixtures: int,
    resumed_requests: int,
    fetched_requests: int,
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    market_counts: dict[str, dict[str, int]] = {}
    company_counts: dict[str, dict[str, int]] = {}
    rejection_reasons: dict[str, int] = {}
    verified_snapshots = 0
    source_records = 0
    covered_match_markets: set[tuple[str, str]] = set()
    planned_match_markets: set[tuple[str, str]] = set()
    for record in records:
        status = str(record.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        market = str(record.get("market_key") or "unknown")
        market_block = market_counts.setdefault(market, {"requests": 0})
        market_block["requests"] += 1
        market_block[status] = market_block.get(status, 0) + 1
        company = str(record.get("company_id") or "unknown")
        company_block = company_counts.setdefault(company, {"requests": 0})
        company_block["requests"] += 1
        company_block[status] = company_block.get(status, 0) + 1
        pair = (str(record.get("match_id")), market)
        planned_match_markets.add(pair)
        if status == "complete":
            covered_match_markets.add(pair)
        filtering = record.get("filtering_qa")
        if isinstance(filtering, Mapping):
            verified_snapshots += int(filtering.get("verified_unique_snapshots") or 0)
            source_records += int(filtering.get("source_records") or 0)
            reasons = filtering.get("rejection_reason_counts")
            if isinstance(reasons, Mapping):
                for reason, count in reasons.items():
                    rejection_reasons[str(reason)] = rejection_reasons.get(
                        str(reason), 0
                    ) + int(count)
    return {
        "fixtures": fixtures,
        "planned_requests": len(records),
        "resumed_requests": resumed_requests,
        "fetched_requests": fetched_requests,
        "status_counts": dict(sorted(status_counts.items())),
        "market_counts": dict(sorted(market_counts.items())),
        "company_counts": dict(
            sorted(company_counts.items(), key=lambda item: int(item[0]))
        ),
        "match_market_pairs": len(planned_match_markets),
        "match_market_pairs_with_at_least_one_verified_company": len(
            covered_match_markets
        ),
        "match_market_coverage": (
            round(len(covered_match_markets) / len(planned_match_markets), 8)
            if planned_match_markets
            else 0.0
        ),
        "source_detail_records_examined": source_records,
        "verified_unique_snapshots": verified_snapshots,
        "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
    }


def _normalized_matches(
    fixtures: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    by_match: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        by_match.setdefault(str(record["match_id"]), []).append(record)
    output: list[dict[str, Any]] = []
    for fixture in fixtures:
        match_id = str(fixture["match_id"])
        market_map: dict[str, list[Mapping[str, Any]]] = {}
        for record in sorted(by_match.get(match_id, []), key=_record_sort_key):
            market_map.setdefault(str(record["market_key"]), []).append(record)
        output.append(
            {
                "match_id": match_id,
                "fixture_fingerprint": _fixture_fingerprint(fixture),
                "fixture": _fixture_summary(fixture),
                "markets": dict(
                    sorted(market_map.items(), key=lambda item: MARKET_ORDER[item[0]])
                ),
            }
        )
    return output


def collect(
    fixtures: Sequence[dict[str, Any]],
    output_dir: Path,
    *,
    company_ids: Sequence[int],
    include_half_total: bool,
    workers: int,
    requests_per_second: float,
    attempts: int,
    timeout: float,
    keep_raw: bool,
) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / "corner_odds.partial.ndjson"
    final_path = output_dir / "corner_odds.json"
    qa_path = output_dir / "corner_odds_qa.json"
    # Raw successful responses are mandatory: checkpoint reuse is permitted
    # only after replaying the parser against this content-addressed evidence.
    # ``keep_raw`` remains accepted for CLI compatibility.
    raw_dir = output_dir / "raw"
    plan = build_request_plan(
        fixtures, company_ids, include_half_total=include_half_total
    )
    checkpoint_records = load_checkpoint(checkpoint_path, repair_truncated_tail=True)
    current: dict[str, dict[str, Any]] = {}
    pending: list[dict[str, Any]] = []
    for request_item in plan:
        key = str(request_item["request_key"])
        existing = checkpoint_records.get(key)
        if existing is not None and _can_resume(existing, request_item, output_dir):
            current[key] = existing
        else:
            pending.append(request_item)
    resumed_requests = len(current)
    print(
        _canonical_json(
            {
                "event": "corner-odds-collection-start",
                "fixtures": len(fixtures),
                "planned_requests": len(plan),
                "resumed_requests": resumed_requests,
                "pending_requests": len(pending),
                "workers": workers,
                "requests_per_second": requests_per_second,
            }
        ),
        flush=True,
    )
    limiter = StartRateLimiter(requests_per_second)
    with checkpoint_path.open("a", encoding="utf-8", newline="\n") as checkpoint:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    fetch_request,
                    request_item,
                    limiter,
                    attempts=attempts,
                    timeout=timeout,
                    raw_dir=raw_dir,
                ): request_item
                for request_item in pending
            }
            since_fsync = 0
            for index, future in enumerate(
                concurrent.futures.as_completed(futures), start=1
            ):
                record = future.result()
                request_item = futures[future]
                if not _record_matches_request_identity(record, request_item):
                    raise CornerOddsCollectionError(
                        f"fetched record {record.get('request_key')} does not match request identity"
                    )
                if record.get("status") in {
                    "complete",
                    "no_verified_snapshot",
                } and not _can_resume(record, request_item, output_dir):
                    raise CornerOddsCollectionError(
                        f"fetched record {record.get('request_key')} cannot be replayed from raw evidence"
                    )
                current[str(record["request_key"])] = record
                checkpoint.write(_canonical_json(record) + "\n")
                checkpoint.flush()
                since_fsync += 1
                if since_fsync >= 10:
                    os.fsync(checkpoint.fileno())
                    since_fsync = 0
                if index % 100 == 0 or index == len(pending):
                    print(
                        _canonical_json(
                            {
                                "event": "corner-odds-collection-progress",
                                "completed_this_run": index,
                                "pending_this_run": len(pending),
                                "total_current": len(current),
                            }
                        ),
                        flush=True,
                    )
        checkpoint.flush()
        os.fsync(checkpoint.fileno())

    ordered = sorted(
        (current[str(item["request_key"])] for item in plan),
        key=_record_sort_key,
    )
    qa = build_qa(
        ordered,
        fixtures=len(fixtures),
        resumed_requests=resumed_requests,
        fetched_requests=len(pending),
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "generated_at": _utc_now(),
        "source_endpoint": ENDPOINT,
        "source_endpoint_type": 4,
        "company_ids": list(company_ids),
        "include_half_total": include_half_total,
        "data_scope": "timestamp_verified_pre_kickoff_corner_prices_only",
        "checkpoint_replay_policy": "successful_rows_require_exact_raw_response_replay",
        "qa": qa,
        "matches": _normalized_matches(fixtures, ordered),
    }
    payload["bundle_hash"] = _hash_bytes(_canonical_json(payload).encode("utf-8"))
    _atomic_json(final_path, payload)
    _atomic_json(qa_path, qa)
    print(
        _canonical_json({"event": "corner-odds-collection-complete", **qa}),
        flush=True,
    )
    return final_path, qa_path, checkpoint_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect verified pre-kickoff Titan corner odds histories"
    )
    parser.add_argument(
        "--schedule",
        action="append",
        required=True,
        type=Path,
        help="schedule JSON; repeat for multiple files",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--company-id",
        "--company-ids",
        action="append",
        help="company ID; repeat or use comma-separated values (default: 3,8,47)",
    )
    parser.add_argument(
        "--include-half-total",
        action="store_true",
        help="also fetch first-half corner total histories",
    )
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--requests-per-second", type=float, default=2.0)
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument(
        "--keep-raw",
        action="store_true",
        help=(
            "compatibility flag; successful responses are always retained in "
            "content-addressed raw/ storage for checkpoint replay"
        ),
    )
    parser.add_argument(
        "--limit", type=int, help="limit input fixtures for a smoke run"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.workers <= 8:
        raise SystemExit("error: --workers must be between 1 and 8")
    if not 0.1 <= args.requests_per_second <= 10.0:
        raise SystemExit("error: --requests-per-second must be between 0.1 and 10")
    if not 1 <= args.attempts <= 10:
        raise SystemExit("error: --attempts must be between 1 and 10")
    if not 1.0 <= args.timeout <= 120.0:
        raise SystemExit("error: --timeout must be between 1 and 120 seconds")
    try:
        company_ids = parse_company_ids(args.company_id)
        fixtures = load_schedule_files([path.resolve() for path in args.schedule])
        if args.limit is not None:
            if args.limit <= 0:
                raise CornerOddsCollectionError("--limit must be positive")
            fixtures = fixtures[: args.limit]
        if not fixtures:
            raise CornerOddsCollectionError("schedule input contains no fixtures")
        collect(
            fixtures,
            args.output_dir.resolve(),
            company_ids=company_ids,
            include_half_total=args.include_half_total,
            workers=args.workers,
            requests_per_second=args.requests_per_second,
            attempts=args.attempts,
            timeout=args.timeout,
            keep_raw=args.keep_raw,
        )
    except CornerOddsCollectionError as error:
        raise SystemExit(f"error: {error}") from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

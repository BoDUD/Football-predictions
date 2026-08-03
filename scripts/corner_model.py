#!/usr/bin/env python3
"""Train, validate, and evaluate an independent NB2 corner-count model.

The model deliberately uses a separate distribution from the football-goal
model.  Home and away corner counts have NB2 marginals with time-decayed team
attack and concession effects.  The first production-safe version keeps those
marginals independent; its artifacts say ``dependence=independent_nb`` and do
not imply that a copula or another correlation model has been fitted.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence


MODEL_ARTIFACT_TYPE = "soccer_corner_count_model"
PREDICTION_ARTIFACT_TYPE = "soccer_corner_prediction"
BACKTEST_ARTIFACT_TYPE = "soccer_corner_backtest"
MODEL_SCHEMA_VERSION = "2.1.0"
PREDICTION_SCHEMA_VERSION = "2.1.0"
BACKTEST_SCHEMA_VERSION = "2.1.0"
MODEL_VERSION = "corner-nb2-independent-time-decay/2.1.0"
DEPENDENCE_MODEL = "independent_nb"
FIXTURE_GRAPH_POLICY_VERSION = "undirected-team-fixture-components/1.0.0"
CROSS_COMPONENT_PREDICTION_POLICY = "fail_closed"
COMPONENT_IDENTIFICATION_METHOD = (
    "shared_league_intercepts_global_zero_centering_positive_l2_regularization"
)
MIN_COMPONENT_REGULARIZATION = 1e-8
TRAINING_COLUMNS = (
    "date",
    "kickoff_utc",
    "kickoff_epoch",
    "league_key",
    "home_team",
    "away_team",
    "home_corners",
    "away_corners",
    "match_id",
    "season",
    "phase",
    "competition_regime",
    "fixture_fingerprint",
    "source_url",
    "source_collected_at",
    "source_response_sha256",
)
REQUIRED_COLUMNS = set(TRAINING_COLUMNS)
SETTLEMENT_STATES = (
    "full_win",
    "half_win",
    "push",
    "half_loss",
    "loss",
)
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
LEAGUE_KEY_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
LOG_LOSS_FLOOR = 1e-15
COMPARISON_METRICS = ("joint_log_loss", "total_crps", "margin_crps")
BASELINE_NAMES = ("league_empirical", "league_nb")
ONE_SIDED_95_Z = 1.6448536269514722
MAX_WALK_FORWARD_BLOCKS = 12
HOLDOUT_POLICY_VERSION = "latest-date-groups-20pct/1.0.0"
HOLDOUT_FRACTION = 0.20
MIN_HOLDOUT_MATCHES = 20
MIN_HOLDOUT_DATE_GROUPS = 5


class CornerModelError(ValueError):
    """Raised when corner data, artifacts, or predictions are unsafe."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CornerModelError("artifact contains non-canonical values") from exc


def _canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def calculate_model_hash(model: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(model))
    payload.pop("model_hash", None)
    return _canonical_hash(payload)


def calculate_prediction_hash(prediction: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(prediction))
    payload.pop("prediction_hash", None)
    return _canonical_hash(payload)


def calculate_backtest_hash(backtest: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(backtest))
    payload.pop("backtest_hash", None)
    return _canonical_hash(payload)


def _require_finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise CornerModelError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CornerModelError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise CornerModelError(f"{name} must be finite")
    return number


def _require_positive(value: Any, name: str) -> float:
    number = _require_finite(value, name)
    if number <= 0.0:
        raise CornerModelError(f"{name} must be greater than zero")
    return number


def _parse_aware_datetime(value: str | datetime, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CornerModelError(f"{name} must be an ISO-8601 datetime") from exc
    else:
        raise CornerModelError(f"{name} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CornerModelError(f"{name} needs an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def _canonical_datetime(value: str | datetime, name: str) -> str:
    return _parse_aware_datetime(value, name).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_match_date(raw: str, row_number: int) -> date:
    value = (raw or "").strip()
    if not value:
        raise CornerModelError(f"row {row_number}: date is required")
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CornerModelError(f"row {row_number}: invalid date") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CornerModelError(
            f"row {row_number}: timestamp dates require an explicit UTC offset"
        )
    return parsed.astimezone(timezone.utc).date()


def _parse_match_datetime(raw: str, field: str, row_number: int) -> datetime:
    try:
        parsed = _parse_aware_datetime(raw, f"row {row_number}: {field}")
    except CornerModelError as exc:
        raise CornerModelError(str(exc)) from exc
    if parsed.microsecond:
        raise CornerModelError(
            f"row {row_number}: {field} must use whole-second precision"
        )
    return parsed


def _parse_epoch(raw: str, row_number: int) -> int:
    value = (raw or "").strip()
    if not re.fullmatch(r"-?\d+", value):
        raise CornerModelError(f"row {row_number}: kickoff_epoch must be an integer")
    return int(value)


def _required_text(raw: str | None, field: str, row_number: int) -> str:
    value = (raw or "").strip()
    if not value:
        raise CornerModelError(f"row {row_number}: {field} is required")
    return value


def _required_row_hash(raw: str | None, field: str, row_number: int) -> str:
    value = _required_text(raw, field, row_number)
    if not HASH_RE.fullmatch(value):
        raise CornerModelError(f"row {row_number}: {field} must be a SHA-256 hash")
    return value


def _parse_count(raw: str, field: str, row_number: int) -> int:
    value = (raw or "").strip()
    if not re.fullmatch(r"\d+", value):
        raise CornerModelError(
            f"row {row_number}: {field} must be a non-negative integer"
        )
    result = int(value)
    if result > 99:
        raise CornerModelError(f"row {row_number}: {field} is implausibly large")
    return result


def _fixture_graph_profile(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a deterministic, hash-bound audit of fixture connectivity.

    A disconnected competition is not automatically invalid.  AFC group/league
    phases, for example, contain legitimate East and West graphs that may never
    meet in the selected regulation-time cohort.  Positive L2 regularization
    makes the shared-intercept fit deterministic, while this profile prevents
    the resulting component membership from being hidden.  Predictions between
    two different known components are rejected separately.
    """

    teams = sorted(
        {str(row["home_team"]) for row in records}
        | {str(row["away_team"]) for row in records}
    )
    if len(teams) < 2:
        raise CornerModelError("training CSV needs at least two teams")
    adjacency = {team: set() for team in teams}
    for row in records:
        home = str(row["home_team"])
        away = str(row["away_team"])
        adjacency[home].add(away)
        adjacency[away].add(home)
    remaining = set(teams)
    raw_components: list[list[str]] = []
    while remaining:
        seed = min(remaining)
        visited = {seed}
        pending = [seed]
        while pending:
            current = pending.pop()
            for neighbor in sorted(adjacency[current]):
                if neighbor not in visited:
                    visited.add(neighbor)
                    pending.append(neighbor)
        component_teams = sorted(visited)
        remaining.difference_update(visited)
        raw_components.append(component_teams)

    components: list[dict[str, Any]] = []
    for component_teams in raw_components:
        member_set = set(component_teams)
        component_rows = [
            row
            for row in records
            if str(row["home_team"]) in member_set
            and str(row["away_team"]) in member_set
        ]
        if not component_rows:
            raise CornerModelError("fixture graph component contains no matches")
        fixture_bindings = sorted(
            (
                {
                    "match_id": str(row["match_id"]),
                    "fixture_fingerprint": str(row["fixture_fingerprint"]),
                }
                for row in component_rows
            ),
            key=lambda item: (int(item["match_id"]), item["fixture_fingerprint"]),
        )
        components.append(
            {
                "component_id": _canonical_hash({"teams": component_teams}),
                "team_count": len(component_teams),
                "match_count": len(component_rows),
                "teams": component_teams,
                "kickoff_utc_start": _canonical_datetime(
                    min(row["kickoff_utc"] for row in component_rows),
                    "fixture_graph.kickoff_utc_start",
                ),
                "kickoff_utc_end": _canonical_datetime(
                    max(row["kickoff_utc"] for row in component_rows),
                    "fixture_graph.kickoff_utc_end",
                ),
                "fixture_set_hash": _canonical_hash(fixture_bindings),
            }
        )
    components.sort(key=lambda item: str(item["component_id"]))
    team_components = {
        team: str(component["component_id"])
        for component in components
        for team in component["teams"]
    }
    return {
        "policy_version": FIXTURE_GRAPH_POLICY_VERSION,
        "component_count": len(components),
        "connected": len(components) == 1,
        "team_count": len(teams),
        "components_hash": _canonical_hash(components),
        "team_component_hash": _canonical_hash(team_components),
        "cross_component_prediction_policy": CROSS_COMPONENT_PREDICTION_POLICY,
        "identification_method": COMPONENT_IDENTIFICATION_METHOD,
        "components": components,
    }


def _fixture_graph_team_components(graph: Mapping[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    raw_components = graph.get("components")
    if not isinstance(raw_components, list):
        raise CornerModelError("fixture graph components are missing")
    for component in raw_components:
        if not isinstance(component, Mapping):
            raise CornerModelError("fixture graph component is invalid")
        component_id = str(component.get("component_id") or "")
        teams = component.get("teams")
        if not HASH_RE.fullmatch(component_id) or not isinstance(teams, list):
            raise CornerModelError("fixture graph component identity is invalid")
        for team in teams:
            name = str(team)
            if name in mapping:
                raise CornerModelError("fixture graph team appears in multiple components")
            mapping[name] = component_id
    return mapping


def _validate_fixture_graph_profile(
    graph: Any,
    *,
    teams: Sequence[str] | None = None,
    matches: int | None = None,
) -> dict[str, str]:
    if not isinstance(graph, Mapping):
        raise CornerModelError("fixture graph audit is missing")
    expected_fields = {
        "policy_version",
        "component_count",
        "connected",
        "team_count",
        "components_hash",
        "team_component_hash",
        "cross_component_prediction_policy",
        "identification_method",
        "components",
    }
    if set(graph) != expected_fields:
        raise CornerModelError("fixture graph audit fields are incomplete")
    if (
        graph.get("policy_version") != FIXTURE_GRAPH_POLICY_VERSION
        or graph.get("cross_component_prediction_policy")
        != CROSS_COMPONENT_PREDICTION_POLICY
        or graph.get("identification_method") != COMPONENT_IDENTIFICATION_METHOD
    ):
        raise CornerModelError("fixture graph policy is unsupported")
    components = graph.get("components")
    if not isinstance(components, list) or not components:
        raise CornerModelError("fixture graph needs at least one component")
    if components != sorted(components, key=lambda item: str(item.get("component_id"))):
        raise CornerModelError("fixture graph components are not canonically ordered")
    expected_component_fields = {
        "component_id",
        "team_count",
        "match_count",
        "teams",
        "kickoff_utc_start",
        "kickoff_utc_end",
        "fixture_set_hash",
    }
    total_matches = 0
    for index, component in enumerate(components):
        if not isinstance(component, Mapping) or set(component) != expected_component_fields:
            raise CornerModelError(f"fixture graph component {index} is incomplete")
        component_teams = component.get("teams")
        if (
            not isinstance(component_teams, list)
            or len(component_teams) < 2
            or component_teams != sorted(set(str(team) for team in component_teams))
            or any(not str(team).strip() for team in component_teams)
            or component.get("team_count") != len(component_teams)
        ):
            raise CornerModelError(f"fixture graph component {index} teams are invalid")
        if component.get("component_id") != _canonical_hash({"teams": component_teams}):
            raise CornerModelError(f"fixture graph component {index} id is invalid")
        component_matches = component.get("match_count")
        if (
            isinstance(component_matches, bool)
            or not isinstance(component_matches, int)
            or component_matches < 1
        ):
            raise CornerModelError(f"fixture graph component {index} matches are invalid")
        total_matches += component_matches
        start = _parse_aware_datetime(
            str(component.get("kickoff_utc_start") or ""),
            f"fixture_graph.components[{index}].kickoff_utc_start",
        )
        end = _parse_aware_datetime(
            str(component.get("kickoff_utc_end") or ""),
            f"fixture_graph.components[{index}].kickoff_utc_end",
        )
        if start > end or not HASH_RE.fullmatch(
            str(component.get("fixture_set_hash") or "")
        ):
            raise CornerModelError(f"fixture graph component {index} audit is invalid")
    mapping = _fixture_graph_team_components(graph)
    if (
        graph.get("component_count") != len(components)
        or graph.get("connected") is not (len(components) == 1)
        or graph.get("team_count") != len(mapping)
        or graph.get("components_hash") != _canonical_hash(components)
        or graph.get("team_component_hash") != _canonical_hash(mapping)
    ):
        raise CornerModelError("fixture graph aggregate audit is inconsistent")
    if teams is not None and sorted(mapping) != sorted(str(team) for team in teams):
        raise CornerModelError("fixture graph teams do not match model training teams")
    if matches is not None and total_matches != matches:
        raise CornerModelError("fixture graph match counts do not match training matches")
    return mapping


def _installed_cohort_policy(
    league_key: str,
) -> tuple[set[str], set[str]] | None:
    """Return the builder's versioned eligible cohorts for a known league."""

    try:
        from scripts import corner_history_dataset_builder as builder
    except ImportError:  # pragma: no cover - direct script execution fallback
        import corner_history_dataset_builder as builder  # type: ignore
    matches = [
        source_key
        for source_key, (registered_key, _name, _aliases) in builder.COMPETITIONS.items()
        if registered_key == league_key
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise CornerModelError("installed league cohort policy is ambiguous")
    source_key = matches[0]
    return (
        set(builder.ELIGIBLE_REGIMES_BY_COMPETITION[source_key]),
        set(builder.ELIGIBLE_PHASES_BY_COMPETITION[source_key]),
    )


def load_training_csv(
    path: str | Path, *, allow_research_cohorts: bool = False
) -> list[dict[str, Any]]:
    """Load source-bound, regulation-time home/away corner counts.

    Version 2 intentionally rejects the legacy five-column research CSV.  A
    usable row must carry its real UTC kickoff, fixture identity, league and
    regime metadata, and the hash of the response from which the corner result
    was parsed.  The registry manager additionally replays the v2 dataset
    manifest against the copied source bundle.
    """

    source = Path(path)
    try:
        handle = source.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise CornerModelError(f"cannot read training CSV: {source}") from exc
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CornerModelError("training CSV has no header")
        if tuple(reader.fieldnames) != TRAINING_COLUMNS:
            missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames))
            unexpected = sorted(set(reader.fieldnames) - REQUIRED_COLUMNS)
            details: list[str] = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unexpected:
                details.append("unexpected " + ", ".join(unexpected))
            if not details:
                details.append("columns are out of canonical order")
            raise CornerModelError(
                "training CSV must use the exact source-bound v2 columns: "
                + "; ".join(details)
            )
        records: list[dict[str, Any]] = []
        fixtures: dict[tuple[int, str, str], tuple[int, int]] = {}
        match_ids: set[str] = set()
        fingerprints: set[str] = set()
        observed_league: str | None = None
        for row_number, row in enumerate(reader, start=2):
            home = _required_text(row.get("home_team"), "home_team", row_number)
            away = _required_text(row.get("away_team"), "away_team", row_number)
            if home == away:
                raise CornerModelError(
                    f"row {row_number}: home_team and away_team must differ"
                )
            match_date = _parse_match_date(row.get("date") or "", row_number)
            kickoff = _parse_match_datetime(
                row.get("kickoff_utc") or "", "kickoff_utc", row_number
            )
            kickoff_epoch = _parse_epoch(row.get("kickoff_epoch") or "", row_number)
            if kickoff_epoch != int(kickoff.timestamp()):
                raise CornerModelError(
                    f"row {row_number}: kickoff_epoch does not match kickoff_utc"
                )
            if match_date != kickoff.date():
                raise CornerModelError(
                    f"row {row_number}: date does not match kickoff_utc UTC date"
                )
            league_key = _required_text(
                row.get("league_key"), "league_key", row_number
            )
            if not LEAGUE_KEY_RE.fullmatch(league_key):
                raise CornerModelError(f"row {row_number}: league_key is invalid")
            if observed_league is None:
                observed_league = league_key
            elif league_key != observed_league:
                raise CornerModelError("training CSV must contain exactly one league_key")
            home_corners = _parse_count(
                row.get("home_corners") or "", "home_corners", row_number
            )
            away_corners = _parse_count(
                row.get("away_corners") or "", "away_corners", row_number
            )
            match_id = _required_text(row.get("match_id"), "match_id", row_number)
            if not match_id.isdigit() or int(match_id) <= 0:
                raise CornerModelError(f"row {row_number}: match_id must be positive digits")
            if match_id in match_ids:
                raise CornerModelError(f"row {row_number}: duplicate match_id {match_id}")
            match_ids.add(match_id)
            fixture_fingerprint = _required_row_hash(
                row.get("fixture_fingerprint"), "fixture_fingerprint", row_number
            )
            if fixture_fingerprint in fingerprints:
                raise CornerModelError(
                    f"row {row_number}: duplicate fixture_fingerprint"
                )
            fingerprints.add(fixture_fingerprint)
            source_response_sha256 = _required_row_hash(
                row.get("source_response_sha256"),
                "source_response_sha256",
                row_number,
            )
            source_url = _required_text(row.get("source_url"), "source_url", row_number)
            if not source_url.startswith("https://"):
                raise CornerModelError(f"row {row_number}: source_url must use HTTPS")
            source_collected_at = _parse_match_datetime(
                row.get("source_collected_at") or "",
                "source_collected_at",
                row_number,
            )
            if source_collected_at < kickoff:
                raise CornerModelError(
                    f"row {row_number}: source_collected_at predates kickoff"
                )
            season = _required_text(row.get("season"), "season", row_number)
            phase = _required_text(row.get("phase"), "phase", row_number)
            competition_regime = _required_text(
                row.get("competition_regime"), "competition_regime", row_number
            )
            key = (kickoff_epoch, home, away)
            result = (home_corners, away_corners)
            if key in fixtures:
                status = "duplicate" if fixtures[key] == result else "conflicting"
                raise CornerModelError(
                    f"row {row_number}: {status} corner count for "
                    f"{kickoff.isoformat()} {home} vs {away}"
                )
            fixtures[key] = result
            records.append(
                {
                    "date": match_date,
                    "kickoff_utc": kickoff,
                    "kickoff_epoch": kickoff_epoch,
                    "league_key": league_key,
                    "home_team": home,
                    "away_team": away,
                    "home_corners": home_corners,
                    "away_corners": away_corners,
                    "match_id": match_id,
                    "season": season,
                    "phase": phase,
                    "competition_regime": competition_regime,
                    "fixture_fingerprint": fixture_fingerprint,
                    "source_url": source_url,
                    "source_collected_at": source_collected_at,
                    "source_response_sha256": source_response_sha256,
                }
            )
    if len(records) < 2:
        raise CornerModelError("training CSV needs at least two matches")
    records.sort(
        key=lambda row: (
            row["kickoff_utc"],
            int(row["match_id"]),
            row["home_team"],
            row["away_team"],
        )
    )
    league_key = str(records[0]["league_key"])
    installed_policy = _installed_cohort_policy(league_key)
    if installed_policy is not None:
        eligible_regimes, eligible_phases = installed_policy
        observed_regimes = {str(row["competition_regime"]) for row in records}
        observed_phases = {str(row["phase"]) for row in records}
        invalid_regimes = sorted(observed_regimes - eligible_regimes)
        invalid_phases = sorted(observed_phases - eligible_phases)
        hard_excluded_cohorts: list[str] = []
        if league_key == "japan_j1" and any(
            str(row["season"]) == "2026"
            or row["kickoff_utc"].year == 2026
            for row in records
        ):
            hard_excluded_cohorts.append("japan_j1:2026")
        if (
            invalid_regimes or invalid_phases or hard_excluded_cohorts
        ) and not allow_research_cohorts:
            details: list[str] = []
            if invalid_regimes:
                details.append("regimes=" + ",".join(invalid_regimes))
            if invalid_phases:
                details.append("phases=" + ",".join(invalid_phases))
            if hard_excluded_cohorts:
                details.append(
                    "hard_exclusions=" + ",".join(hard_excluded_cohorts)
                )
            raise CornerModelError(
                "training CSV contains cohorts outside the installed eligible "
                f"policy for {league_key}: {'; '.join(details)}"
            )
    _fixture_graph_profile(records)
    return records


def training_dataset_profile(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the canonical semantic identity of already validated CSV rows."""

    if len(records) < 2:
        raise CornerModelError("training dataset profile needs at least two matches")
    league_keys = sorted({str(row.get("league_key") or "") for row in records})
    if len(league_keys) != 1 or not LEAGUE_KEY_RE.fullmatch(league_keys[0]):
        raise CornerModelError("training dataset profile requires exactly one league")
    ordered = sorted(
        records,
        key=lambda row: (
            int(row["kickoff_epoch"]),
            int(str(row["match_id"])),
        ),
    )
    fixture_bindings = [
        {
            "match_id": str(row["match_id"]),
            "fixture_fingerprint": str(row["fixture_fingerprint"]),
        }
        for row in ordered
    ]
    response_bindings = [
        {
            "match_id": str(row["match_id"]),
            "source_response_sha256": str(row["source_response_sha256"]),
        }
        for row in ordered
    ]
    semantic_rows = [
        {
            "date": row["date"].isoformat(),
            "kickoff_utc": _canonical_datetime(row["kickoff_utc"], "kickoff_utc"),
            "kickoff_epoch": int(row["kickoff_epoch"]),
            "league_key": str(row["league_key"]),
            "home_team": str(row["home_team"]),
            "away_team": str(row["away_team"]),
            "home_corners": int(row["home_corners"]),
            "away_corners": int(row["away_corners"]),
            "match_id": str(row["match_id"]),
            "season": str(row["season"]),
            "phase": str(row["phase"]),
            "competition_regime": str(row["competition_regime"]),
            "fixture_fingerprint": str(row["fixture_fingerprint"]),
            "source_url": str(row["source_url"]),
            "source_collected_at": _canonical_datetime(
                row["source_collected_at"], "source_collected_at"
            ),
            "source_response_sha256": str(row["source_response_sha256"]),
        }
        for row in ordered
    ]
    first = ordered[0]
    last = ordered[-1]
    fixture_graph = _fixture_graph_profile(ordered)
    return {
        "league_key": league_keys[0],
        "rows": len(ordered),
        "kickoff_utc_start": _canonical_datetime(
            first["kickoff_utc"], "kickoff_utc_start"
        ),
        "kickoff_utc_end": _canonical_datetime(
            last["kickoff_utc"], "kickoff_utc_end"
        ),
        "kickoff_epoch_start": int(first["kickoff_epoch"]),
        "kickoff_epoch_end": int(last["kickoff_epoch"]),
        "seasons": sorted({str(row["season"]) for row in ordered}),
        "phases": sorted({str(row["phase"]) for row in ordered}),
        "competition_regimes": sorted(
            {str(row["competition_regime"]) for row in ordered}
        ),
        "fixture_set_hash": _canonical_hash(fixture_bindings),
        "response_set_hash": _canonical_hash(response_bindings),
        "semantic_rows_hash": _canonical_hash(semantic_rows),
        "fixture_graph": fixture_graph,
    }


def nb2_log_pmf(count: int, mean: float, dispersion: float) -> float:
    """Return log P(X=count) for NB2 with variance mean + mean^2/dispersion."""

    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise CornerModelError("NB2 count must be a non-negative integer")
    mean = _require_positive(mean, "NB2 mean")
    dispersion = _require_positive(dispersion, "NB2 dispersion")
    return (
        math.lgamma(count + dispersion)
        - math.lgamma(dispersion)
        - math.lgamma(count + 1)
        + dispersion * (math.log(dispersion) - math.log(dispersion + mean))
        + count * (math.log(mean) - math.log(dispersion + mean))
    )


def nb2_pmf(count: int, mean: float, dispersion: float) -> float:
    return math.exp(nb2_log_pmf(count, mean, dispersion))


def _nb2_prefix(mean: float, dispersion: float, maximum: int) -> list[float]:
    mean = _require_positive(mean, "NB2 mean")
    dispersion = _require_positive(dispersion, "NB2 dispersion")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 0:
        raise CornerModelError("maximum corner count must be a non-negative integer")
    first = math.exp(
        dispersion * (math.log(dispersion) - math.log(dispersion + mean))
    )
    probabilities = [first]
    ratio = mean / (dispersion + mean)
    for count in range(maximum):
        probabilities.append(
            probabilities[-1]
            * (count + dispersion)
            / (count + 1.0)
            * ratio
        )
    return probabilities


def nb2_distribution(
    mean: float,
    dispersion: float,
    *,
    tail_tolerance: float = 1e-8,
    hard_max_corners: int = 80,
) -> dict[str, Any]:
    """Build an adaptive NB2 marginal and retain its unnormalized tail audit."""

    tolerance = _require_positive(tail_tolerance, "tail_tolerance")
    if tolerance >= 1.0:
        raise CornerModelError("tail_tolerance must be less than one")
    if (
        isinstance(hard_max_corners, bool)
        or not isinstance(hard_max_corners, int)
        or hard_max_corners < 1
    ):
        raise CornerModelError("hard_max_corners must be a positive integer")
    probabilities = _nb2_prefix(mean, dispersion, hard_max_corners)
    retained = 0.0
    selected: list[float] = []
    for probability in probabilities:
        selected.append(probability)
        retained = math.fsum(selected)
        tail = max(0.0, 1.0 - retained)
        if tail <= tolerance:
            return {
                "probabilities": selected,
                "raw_retained_probability": retained,
                "raw_omitted_probability": tail,
                "maximum": len(selected) - 1,
            }
    raise CornerModelError(
        "NB2 corner tail exceeds tolerance at hard_max_corners; increase the hard maximum"
    )


def _time_weights(
    records: Sequence[Mapping[str, Any]], half_life_days: float
) -> list[float]:
    half_life = _require_positive(half_life_days, "half_life_days")
    reference = max(row["kickoff_utc"] for row in records)
    return [
        math.exp(
            -math.log(2.0)
            * (reference - row["kickoff_utc"]).total_seconds()
            / 86400.0
            / half_life
        )
        for row in records
    ]


def _weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    total = math.fsum(weights)
    if total <= 0.0:
        raise CornerModelError("time weights have zero mass")
    return math.fsum(value * weight for value, weight in zip(values, weights)) / total


def _moment_dispersion(values: Sequence[int], weights: Sequence[float]) -> float:
    mean = max(_weighted_mean([float(value) for value in values], weights), 1e-6)
    variance = _weighted_mean(
        [(float(value) - mean) ** 2 for value in values], weights
    )
    if variance <= mean + 1e-9:
        return 256.0
    return max(0.25, min(256.0, mean * mean / (variance - mean)))


def _rates(
    records: Sequence[Mapping[str, Any]], parameters: Mapping[str, Any]
) -> tuple[list[float], list[float]]:
    attack = parameters["attack"]
    concession = parameters["concession"]
    home_intercept = float(parameters["home_intercept"])
    away_intercept = float(parameters["away_intercept"])
    home_rates: list[float] = []
    away_rates: list[float] = []
    for row in records:
        home = str(row["home_team"])
        away = str(row["away_team"])
        home_eta = home_intercept + float(attack[home]) + float(concession[away])
        away_eta = away_intercept + float(attack[away]) + float(concession[home])
        home_rates.append(math.exp(max(-6.0, min(6.0, home_eta))))
        away_rates.append(math.exp(max(-6.0, min(6.0, away_eta))))
    return home_rates, away_rates


def _mean_objective(
    records: Sequence[Mapping[str, Any]],
    weights: Sequence[float],
    parameters: Mapping[str, Any],
    home_dispersion: float,
    away_dispersion: float,
    regularization: float,
) -> float:
    home_rates, away_rates = _rates(records, parameters)
    total_weight = math.fsum(weights)
    loss = math.fsum(
        weight
        * (
            -nb2_log_pmf(int(row["home_corners"]), home_rate, home_dispersion)
            - nb2_log_pmf(int(row["away_corners"]), away_rate, away_dispersion)
        )
        for row, weight, home_rate, away_rate in zip(
            records, weights, home_rates, away_rates
        )
    ) / total_weight
    effects = list(parameters["attack"].values()) + list(
        parameters["concession"].values()
    )
    penalty = regularization * math.fsum(float(value) ** 2 for value in effects) / max(
        1, len(effects)
    )
    return loss + penalty


def _fit_mean_parameters(
    records: Sequence[Mapping[str, Any]],
    teams: Sequence[str],
    weights: Sequence[float],
    parameters: Mapping[str, Any],
    *,
    home_dispersion: float,
    away_dispersion: float,
    iterations: int,
    learning_rate: float,
    regularization: float,
) -> dict[str, Any]:
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        raise CornerModelError("iterations must be a positive integer")
    learning_rate = _require_positive(learning_rate, "learning_rate")
    regularization = _require_finite(regularization, "regularization")
    if regularization < MIN_COMPONENT_REGULARIZATION:
        raise CornerModelError(
            "regularization must be positive and at least 1e-8 to identify "
            "disconnected fixture components"
        )
    result = {
        "home_intercept": float(parameters["home_intercept"]),
        "away_intercept": float(parameters["away_intercept"]),
        "attack": {team: float(parameters["attack"][team]) for team in teams},
        "concession": {
            team: float(parameters["concession"][team]) for team in teams
        },
    }
    names = ["home_intercept", "away_intercept"] + [
        f"attack:{team}" for team in teams
    ] + [f"concession:{team}" for team in teams]
    first_moment = {name: 0.0 for name in names}
    second_moment = {name: 0.0 for name in names}
    beta1, beta2, epsilon = 0.9, 0.999, 1e-8
    total_weight = math.fsum(weights)

    def parameter_value(name: str) -> float:
        if ":" not in name:
            return float(result[name])
        group, team = name.split(":", 1)
        return float(result[group][team])

    def set_parameter(name: str, value: float) -> None:
        if ":" not in name:
            result[name] = value
        else:
            group, team = name.split(":", 1)
            result[group][team] = value

    for step in range(1, iterations + 1):
        gradients = {name: 0.0 for name in names}
        home_rates, away_rates = _rates(records, result)
        for row, weight, home_rate, away_rate in zip(
            records, weights, home_rates, away_rates
        ):
            home_error = (
                weight
                * home_dispersion
                * (home_rate - int(row["home_corners"]))
                / (home_dispersion + home_rate)
                / total_weight
            )
            away_error = (
                weight
                * away_dispersion
                * (away_rate - int(row["away_corners"]))
                / (away_dispersion + away_rate)
                / total_weight
            )
            home = str(row["home_team"])
            away = str(row["away_team"])
            gradients["home_intercept"] += home_error
            gradients["away_intercept"] += away_error
            gradients[f"attack:{home}"] += home_error
            gradients[f"concession:{away}"] += home_error
            gradients[f"attack:{away}"] += away_error
            gradients[f"concession:{home}"] += away_error
        denominator = max(1, 2 * len(teams))
        for team in teams:
            gradients[f"attack:{team}"] += (
                2.0 * regularization * float(result["attack"][team]) / denominator
            )
            gradients[f"concession:{team}"] += (
                2.0
                * regularization
                * float(result["concession"][team])
                / denominator
            )
        for name in names:
            gradient = gradients[name]
            first_moment[name] = beta1 * first_moment[name] + (1.0 - beta1) * gradient
            second_moment[name] = (
                beta2 * second_moment[name] + (1.0 - beta2) * gradient * gradient
            )
            corrected_first = first_moment[name] / (1.0 - beta1**step)
            corrected_second = second_moment[name] / (1.0 - beta2**step)
            updated = parameter_value(name) - learning_rate * corrected_first / (
                math.sqrt(corrected_second) + epsilon
            )
            if name in {"home_intercept", "away_intercept"}:
                updated = max(-2.5, min(4.5, updated))
            else:
                updated = max(-3.0, min(3.0, updated))
            set_parameter(name, updated)

        # Remove the attack/defence location indeterminacy while preserving all
        # fitted log means through the two intercepts.
        attack_mean = math.fsum(result["attack"].values()) / len(teams)
        concession_mean = math.fsum(result["concession"].values()) / len(teams)
        for team in teams:
            result["attack"][team] -= attack_mean
            result["concession"][team] -= concession_mean
        adjustment = attack_mean + concession_mean
        result["home_intercept"] += adjustment
        result["away_intercept"] += adjustment
    return result


def _select_dispersion(
    counts: Sequence[int], means: Sequence[float], weights: Sequence[float], seed: float
) -> float:
    candidates = {
        0.25,
        0.5,
        0.75,
        1.0,
        1.5,
        2.0,
        3.0,
        4.0,
        6.0,
        8.0,
        12.0,
        16.0,
        24.0,
        32.0,
        48.0,
        64.0,
        96.0,
        128.0,
        192.0,
        256.0,
        max(0.25, min(256.0, seed)),
    }
    total_weight = math.fsum(weights)

    def loss(shape: float) -> float:
        return math.fsum(
            weight * -nb2_log_pmf(count, mean, shape)
            for count, mean, weight in zip(counts, means, weights)
        ) / total_weight

    return min(sorted(candidates), key=lambda shape: (loss(shape), shape))


def _source_hash(path: Path) -> str:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CornerModelError(f"cannot hash training CSV: {path}") from exc


def _normalize_source_lineage(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not value:
        raise CornerModelError("source_lineage must be a non-empty object")
    try:
        normalized = json.loads(_canonical_bytes(dict(value)).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
        raise CornerModelError("source_lineage is not canonical JSON") from exc
    if not isinstance(normalized, dict):
        raise CornerModelError("source_lineage must be an object")
    return normalized


def _fit_records(
    records: Sequence[Mapping[str, Any]],
    *,
    source_data_hash: str,
    source_name: str,
    half_life_days: float,
    iterations: int,
    learning_rate: float,
    regularization: float,
    generated_at: str | datetime,
    source_lineage: Mapping[str, Any] | None = None,
    historical_simulation: bool = False,
    research_cohort_opt_in: bool = False,
) -> dict[str, Any]:
    if not HASH_RE.fullmatch(source_data_hash):
        raise CornerModelError("source_data_hash must be a SHA-256 hash")
    records = sorted(
        (dict(row) for row in records),
        key=lambda row: (
            row["kickoff_utc"],
            int(str(row["match_id"])),
            row["home_team"],
            row["away_team"],
        ),
    )
    graph = _fixture_graph_profile(records)
    teams = sorted(_fixture_graph_team_components(graph))
    generated = _parse_aware_datetime(generated_at, "generated_at")
    training_start = min(row["date"] for row in records)
    training_end = max(row["date"] for row in records)
    training_start_time = min(row["kickoff_utc"] for row in records)
    training_end_time = max(row["kickoff_utc"] for row in records)
    latest_collection_time = max(row["source_collected_at"] for row in records)
    if generated <= training_end_time:
        raise CornerModelError(
            "model generated_at must be after the final training kickoff"
        )
    if not historical_simulation and generated < latest_collection_time:
        raise CornerModelError(
            "model generated_at cannot predate collection of its source evidence"
        )
    dataset_profile = training_dataset_profile(records)
    normalized_lineage = _normalize_source_lineage(source_lineage)
    weights = _time_weights(records, half_life_days)
    home_values = [int(row["home_corners"]) for row in records]
    away_values = [int(row["away_corners"]) for row in records]
    home_mean = max(_weighted_mean([float(v) for v in home_values], weights), 1e-3)
    away_mean = max(_weighted_mean([float(v) for v in away_values], weights), 1e-3)
    home_dispersion = _moment_dispersion(home_values, weights)
    away_dispersion = _moment_dispersion(away_values, weights)
    parameters: dict[str, Any] = {
        "home_intercept": math.log(home_mean),
        "away_intercept": math.log(away_mean),
        "attack": {team: 0.0 for team in teams},
        "concession": {team: 0.0 for team in teams},
    }
    parameters = _fit_mean_parameters(
        records,
        teams,
        weights,
        parameters,
        home_dispersion=home_dispersion,
        away_dispersion=away_dispersion,
        iterations=max(1, iterations // 2),
        learning_rate=learning_rate,
        regularization=regularization,
    )
    fitted_home, fitted_away = _rates(records, parameters)
    home_dispersion = _select_dispersion(
        home_values, fitted_home, weights, home_dispersion
    )
    away_dispersion = _select_dispersion(
        away_values, fitted_away, weights, away_dispersion
    )
    parameters = _fit_mean_parameters(
        records,
        teams,
        weights,
        parameters,
        home_dispersion=home_dispersion,
        away_dispersion=away_dispersion,
        iterations=iterations,
        learning_rate=learning_rate,
        regularization=regularization,
    )
    parameters["home_dispersion"] = home_dispersion
    parameters["away_dispersion"] = away_dispersion
    objective = _mean_objective(
        records,
        weights,
        parameters,
        home_dispersion,
        away_dispersion,
        regularization,
    )
    model: dict[str, Any] = {
        "artifact_type": MODEL_ARTIFACT_TYPE,
        "schema_version": MODEL_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "authority": {
            "formal_eligible": False,
            "scope": "research_observation_only",
            "manager_source_replay_required": True,
            "source_lineage_is_claim_only_until_manager_replay": True,
            "research_cohort_opt_in": bool(research_cohort_opt_in),
        },
        "generated_at": generated.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "dependence": {
            "model": DEPENDENCE_MODEL,
            "assumption": "home and away NB2 marginals are independent",
            "fitted_correlation": False,
        },
        "training": {
            "source_file": source_name,
            "source_data_hash": source_data_hash,
            "source_lineage": normalized_lineage,
            "dataset_profile": dataset_profile,
            "start_date": training_start.isoformat(),
            "end_date": training_end.isoformat(),
            "cutoff_date": training_end.isoformat(),
            "start_kickoff_utc": _canonical_datetime(
                training_start_time, "training_start_kickoff"
            ),
            "end_kickoff_utc": _canonical_datetime(
                training_end_time, "training_end_kickoff"
            ),
            "cutoff_kickoff_utc": _canonical_datetime(
                training_end_time, "training_cutoff_kickoff"
            ),
            "cutoff_kickoff_epoch": int(training_end_time.timestamp()),
            "latest_source_collected_at": _canonical_datetime(
                latest_collection_time, "latest_source_collected_at"
            ),
            "matches": len(records),
            "teams": teams,
        },
        "config": {
            "half_life_days": float(half_life_days),
            "iterations": int(iterations),
            "learning_rate": float(learning_rate),
            "regularization": float(regularization),
            "fixture_graph_identification": COMPONENT_IDENTIFICATION_METHOD,
            "dispersion_selection": "weighted_marginal_nll_grid",
        },
        "parameters": parameters,
        "fit": {
            "objective": "time_weighted_independent_nb2_negative_log_likelihood",
            "optimizer": "deterministic_adam_with_dispersion_grid",
            "penalized_mean_nll": objective,
            "effective_weight": math.fsum(weights),
            "historical_simulation": bool(historical_simulation),
        },
    }
    model["model_hash"] = calculate_model_hash(model)
    validate_model(model)
    return model


def fit_model(
    csv_path: str | Path,
    *,
    half_life_days: float = 365.0,
    iterations: int = 600,
    learning_rate: float = 0.03,
    regularization: float = 0.02,
    generated_at: str | datetime | None = None,
    source_lineage: Mapping[str, Any] | None = None,
    allow_research_cohorts: bool = False,
) -> dict[str, Any]:
    source = Path(csv_path).resolve()
    records = load_training_csv(
        source, allow_research_cohorts=allow_research_cohorts
    )
    generated = generated_at if generated_at is not None else _utc_now()
    return _fit_records(
        records,
        source_data_hash=_source_hash(source),
        source_name=source.name,
        half_life_days=half_life_days,
        iterations=iterations,
        learning_rate=learning_rate,
        regularization=regularization,
        generated_at=generated,
        source_lineage=source_lineage,
        historical_simulation=False,
        research_cohort_opt_in=allow_research_cohorts,
    )


def _validate_effect_map(value: Any, teams: Sequence[str], name: str) -> None:
    if not isinstance(value, Mapping) or set(value) != set(teams):
        raise CornerModelError(f"parameters.{name} must contain every training team")
    for team in teams:
        number = _require_finite(value[team], f"parameters.{name}.{team}")
        if abs(number) > 3.0000001:
            raise CornerModelError(f"parameters.{name}.{team} is outside fitted bounds")
    if abs(math.fsum(float(value[team]) for team in teams)) > 1e-7:
        raise CornerModelError(f"parameters.{name} effects must be zero-centred")


def validate_model(model: Mapping[str, Any]) -> None:
    if not isinstance(model, Mapping):
        raise CornerModelError("model must be an object")
    if model.get("artifact_type") != MODEL_ARTIFACT_TYPE:
        raise CornerModelError("unexpected model artifact_type")
    if model.get("schema_version") != MODEL_SCHEMA_VERSION:
        raise CornerModelError("unsupported model schema_version")
    if model.get("model_version") != MODEL_VERSION:
        raise CornerModelError("unsupported model_version")
    authority = model.get("authority")
    if (
        not isinstance(authority, Mapping)
        or set(authority)
        != {
            "formal_eligible",
            "scope",
            "manager_source_replay_required",
            "source_lineage_is_claim_only_until_manager_replay",
            "research_cohort_opt_in",
        }
        or authority.get("formal_eligible") is not False
        or authority.get("scope") != "research_observation_only"
        or authority.get("manager_source_replay_required") is not True
        or authority.get("source_lineage_is_claim_only_until_manager_replay") is not True
        or not isinstance(authority.get("research_cohort_opt_in"), bool)
    ):
        raise CornerModelError("model authority must remain research-observation-only")
    stored_hash = model.get("model_hash")
    if not isinstance(stored_hash, str) or not HASH_RE.fullmatch(stored_hash):
        raise CornerModelError("model_hash must be a SHA-256 hash")
    if stored_hash != calculate_model_hash(model):
        raise CornerModelError("model_hash does not match model contents")
    generated = _parse_aware_datetime(str(model.get("generated_at") or ""), "generated_at")
    dependence = model.get("dependence")
    if not isinstance(dependence, Mapping) or dependence.get("model") != DEPENDENCE_MODEL:
        raise CornerModelError("model dependence must be independent_nb")
    if dependence.get("fitted_correlation") is not False:
        raise CornerModelError("independent NB model cannot claim fitted correlation")
    training = model.get("training")
    if not isinstance(training, Mapping):
        raise CornerModelError("model training metadata is missing")
    try:
        start = date.fromisoformat(str(training.get("start_date") or ""))
        end = date.fromisoformat(str(training.get("end_date") or ""))
        cutoff = date.fromisoformat(str(training.get("cutoff_date") or ""))
    except ValueError as exc:
        raise CornerModelError("training dates must be valid ISO dates") from exc
    if start > end or cutoff != end:
        raise CornerModelError("training date range or cutoff is inconsistent")
    start_kickoff = _parse_aware_datetime(
        str(training.get("start_kickoff_utc") or ""),
        "training.start_kickoff_utc",
    )
    end_kickoff = _parse_aware_datetime(
        str(training.get("end_kickoff_utc") or ""),
        "training.end_kickoff_utc",
    )
    cutoff_kickoff = _parse_aware_datetime(
        str(training.get("cutoff_kickoff_utc") or ""),
        "training.cutoff_kickoff_utc",
    )
    cutoff_epoch = training.get("cutoff_kickoff_epoch")
    if (
        start_kickoff > end_kickoff
        or cutoff_kickoff != end_kickoff
        or start_kickoff.date() != start
        or end_kickoff.date() != end
        or isinstance(cutoff_epoch, bool)
        or not isinstance(cutoff_epoch, int)
        or cutoff_epoch != int(cutoff_kickoff.timestamp())
    ):
        raise CornerModelError("training kickoff range or epoch is inconsistent")
    if generated <= cutoff_kickoff:
        raise CornerModelError("model generated_at must be after training cutoff kickoff")
    latest_collection = _parse_aware_datetime(
        str(training.get("latest_source_collected_at") or ""),
        "training.latest_source_collected_at",
    )
    if not HASH_RE.fullmatch(str(training.get("source_data_hash") or "")):
        raise CornerModelError("training source_data_hash must be a SHA-256 hash")
    lineage = training.get("source_lineage")
    if lineage is not None and _normalize_source_lineage(lineage) != lineage:
        raise CornerModelError("training source_lineage is not canonical")
    profile = training.get("dataset_profile")
    expected_profile_fields = {
        "league_key",
        "rows",
        "kickoff_utc_start",
        "kickoff_utc_end",
        "kickoff_epoch_start",
        "kickoff_epoch_end",
        "seasons",
        "phases",
        "competition_regimes",
        "fixture_set_hash",
        "response_set_hash",
        "semantic_rows_hash",
        "fixture_graph",
    }
    if not isinstance(profile, Mapping) or set(profile) != expected_profile_fields:
        raise CornerModelError("training dataset_profile is incomplete")
    if profile.get("league_key") is None or not LEAGUE_KEY_RE.fullmatch(
        str(profile.get("league_key"))
    ):
        raise CornerModelError("training dataset_profile league_key is invalid")
    for field in ("fixture_set_hash", "response_set_hash", "semantic_rows_hash"):
        if not HASH_RE.fullmatch(str(profile.get(field) or "")):
            raise CornerModelError(f"training dataset_profile.{field} is invalid")
    _validate_fixture_graph_profile(
        profile.get("fixture_graph"),
        teams=training.get("teams") if isinstance(training.get("teams"), list) else None,
        matches=training.get("matches") if isinstance(training.get("matches"), int) else None,
    )
    for field in ("seasons", "phases", "competition_regimes"):
        values = profile.get(field)
        if (
            not isinstance(values, list)
            or not values
            or values != sorted(set(str(value) for value in values))
            or any(not str(value).strip() for value in values)
        ):
            raise CornerModelError(f"training dataset_profile.{field} is invalid")
    profile_start = _parse_aware_datetime(
        str(profile.get("kickoff_utc_start") or ""),
        "training.dataset_profile.kickoff_utc_start",
    )
    profile_end = _parse_aware_datetime(
        str(profile.get("kickoff_utc_end") or ""),
        "training.dataset_profile.kickoff_utc_end",
    )
    if (
        profile_start != start_kickoff
        or profile_end != end_kickoff
        or profile.get("kickoff_epoch_start") != int(start_kickoff.timestamp())
        or profile.get("kickoff_epoch_end") != int(end_kickoff.timestamp())
    ):
        raise CornerModelError("training dataset_profile kickoff range is inconsistent")
    teams = training.get("teams")
    if (
        not isinstance(teams, list)
        or len(teams) < 2
        or teams != sorted(set(str(team) for team in teams))
    ):
        raise CornerModelError("training teams must be a sorted unique list")
    matches = training.get("matches")
    if isinstance(matches, bool) or not isinstance(matches, int) or matches < 2:
        raise CornerModelError("training matches must be at least two")
    if profile.get("rows") != matches:
        raise CornerModelError("training dataset_profile rows do not match training matches")
    config = model.get("config")
    if not isinstance(config, Mapping):
        raise CornerModelError("model config is missing")
    _require_positive(config.get("half_life_days"), "config.half_life_days")
    _require_positive(config.get("learning_rate"), "config.learning_rate")
    regularization = _require_finite(
        config.get("regularization"), "config.regularization"
    )
    if regularization < MIN_COMPONENT_REGULARIZATION:
        raise CornerModelError(
            "config.regularization must be positive and at least 1e-8 for "
            "fixture-component identification"
        )
    if config.get("fixture_graph_identification") != COMPONENT_IDENTIFICATION_METHOD:
        raise CornerModelError("config fixture graph identification is unsupported")
    iterations = config.get("iterations")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        raise CornerModelError("config.iterations must be a positive integer")
    parameters = model.get("parameters")
    if not isinstance(parameters, Mapping):
        raise CornerModelError("model parameters are missing")
    for name in ("home_intercept", "away_intercept"):
        value = _require_finite(parameters.get(name), f"parameters.{name}")
        if not -2.500001 <= value <= 4.500001:
            raise CornerModelError(f"parameters.{name} is outside fitted bounds")
    for name in ("home_dispersion", "away_dispersion"):
        value = _require_positive(parameters.get(name), f"parameters.{name}")
        if not 0.249999 <= value <= 256.000001:
            raise CornerModelError(f"parameters.{name} is outside fitted bounds")
    _validate_effect_map(parameters.get("attack"), teams, "attack")
    _validate_effect_map(parameters.get("concession"), teams, "concession")
    fit = model.get("fit")
    if not isinstance(fit, Mapping):
        raise CornerModelError("model fit metadata is missing")
    if fit.get("objective") != "time_weighted_independent_nb2_negative_log_likelihood":
        raise CornerModelError("fit objective is unsupported")
    _require_finite(fit.get("penalized_mean_nll"), "fit.penalized_mean_nll")
    _require_positive(fit.get("effective_weight"), "fit.effective_weight")
    if not isinstance(fit.get("historical_simulation"), bool):
        raise CornerModelError("fit.historical_simulation must be boolean")
    if fit.get("historical_simulation") is False and generated < latest_collection:
        raise CornerModelError("model predates collection of its source evidence")


def _split_quarter_line(line: float) -> list[float]:
    number = _require_finite(line, "line")
    units = round(number * 4.0)
    if abs(number * 4.0 - units) > 1e-8:
        raise CornerModelError("line must be a multiple of 0.25")
    if units % 2 == 0:
        return [units / 4.0]
    return [(units - 1) / 4.0, (units + 1) / 4.0]


def _component_outcome(value: float) -> str:
    if value > 1e-12:
        return "win"
    if value < -1e-12:
        return "loss"
    return "push"


def _combined_settlement_state(outcomes: Sequence[str]) -> str:
    if len(outcomes) == 1:
        return {"win": "full_win", "push": "push", "loss": "loss"}[
            outcomes[0]
        ]
    key = tuple(sorted(outcomes))
    mapping = {
        ("win", "win"): "full_win",
        ("push", "win"): "half_win",
        ("push", "push"): "push",
        ("loss", "push"): "half_loss",
        ("loss", "loss"): "loss",
    }
    if key not in mapping:
        raise CornerModelError("split line produced contradictory outcomes")
    return mapping[key]


def _validate_matrix(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    if not isinstance(matrix, Sequence) or isinstance(matrix, (str, bytes)) or not matrix:
        raise CornerModelError("joint corner matrix must be non-empty")
    result: list[list[float]] = []
    width: int | None = None
    for row_index, row in enumerate(matrix):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or not row:
            raise CornerModelError(f"joint corner matrix row {row_index} is invalid")
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise CornerModelError("joint corner matrix must be rectangular")
        converted = []
        for column_index, value in enumerate(row):
            number = _require_finite(
                value, f"joint corner matrix[{row_index}][{column_index}]"
            )
            if number < 0.0:
                raise CornerModelError("joint corner probabilities cannot be negative")
            converted.append(number)
        result.append(converted)
    if abs(math.fsum(math.fsum(row) for row in result) - 1.0) > 1e-9:
        raise CornerModelError("joint corner matrix probabilities must sum to one")
    return result


def _settlement_distribution(
    matrix: Sequence[Sequence[float]],
    line: float,
    value_for_score: Any,
) -> tuple[list[float], dict[str, float]]:
    validated = _validate_matrix(matrix)
    split_lines = _split_quarter_line(line)
    probabilities = {state: 0.0 for state in SETTLEMENT_STATES}
    for home, row in enumerate(validated):
        for away, probability in enumerate(row):
            outcomes = [
                _component_outcome(value_for_score(home, away, part))
                for part in split_lines
            ]
            probabilities[_combined_settlement_state(outcomes)] += probability
    if abs(math.fsum(probabilities.values()) - 1.0) > 1e-9:
        raise CornerModelError("settlement probabilities do not sum to one")
    return split_lines, probabilities


def _settlement_summary(
    side: str, line: float, split_lines: list[float], probabilities: dict[str, float]
) -> dict[str, Any]:
    win_equivalent = probabilities["full_win"] + probabilities["half_win"] / 2.0
    loss_equivalent = probabilities["loss"] + probabilities["half_loss"] / 2.0
    fair_hk_odds = loss_equivalent / win_equivalent if win_equivalent > 0.0 else None
    return {
        "side": side,
        "line": float(line),
        "split_lines": split_lines,
        "probabilities": probabilities,
        "positive_state_probability": probabilities["full_win"]
        + probabilities["half_win"],
        "fair_hong_kong_odds": fair_hk_odds,
        "fair_decimal_odds": 1.0 + fair_hk_odds if fair_hk_odds is not None else None,
    }


def aggregate_corner_total(
    matrix: Sequence[Sequence[float]], side: str, line: float
) -> dict[str, Any]:
    side = str(side or "").lower()
    if side not in {"over", "under"}:
        raise CornerModelError("corner total side must be over or under")

    def value(home: int, away: int, component: float) -> float:
        total = home + away
        return total - component if side == "over" else component - total

    split_lines, probabilities = _settlement_distribution(matrix, line, value)
    return _settlement_summary(side, line, split_lines, probabilities)


def aggregate_corner_handicap(
    matrix: Sequence[Sequence[float]], side: str, line: float
) -> dict[str, Any]:
    side = str(side or "").lower()
    if side not in {"home", "away"}:
        raise CornerModelError("corner handicap side must be home or away")

    def value(home: int, away: int, component: float) -> float:
        margin = home - away if side == "home" else away - home
        return margin + component

    split_lines, probabilities = _settlement_distribution(matrix, line, value)
    return _settlement_summary(side, line, split_lines, probabilities)


def _fixture_rates(
    model: Mapping[str, Any], home_team: str, away_team: str, unknown_team_policy: str
) -> tuple[float, float, list[str], dict[str, Any]]:
    if not home_team or not away_team or home_team == away_team:
        raise CornerModelError("fixture requires distinct non-empty home and away teams")
    if unknown_team_policy not in {"error", "league_average"}:
        raise CornerModelError("unknown_team_policy must be error or league_average")
    teams = set(model["training"]["teams"])
    unknown = sorted({team for team in (home_team, away_team) if team not in teams})
    if unknown and unknown_team_policy == "error":
        raise CornerModelError("unknown team(s): " + ", ".join(unknown))
    component_map = _validate_fixture_graph_profile(
        model["training"]["dataset_profile"].get("fixture_graph"),
        teams=model["training"]["teams"],
        matches=model["training"]["matches"],
    )
    home_component = component_map.get(home_team)
    away_component = component_map.get(away_team)
    if not unknown and home_component != away_component:
        raise CornerModelError(
            "cross-component fixture is not comparable under the training graph: "
            f"{home_team} ({home_component}) vs {away_team} ({away_component}); "
            "prediction fails closed"
        )
    component_audit = {
        "policy": CROSS_COMPONENT_PREDICTION_POLICY,
        "home_training_component_id": home_component,
        "away_training_component_id": away_component,
        "same_training_component": (
            home_component == away_component if not unknown else None
        ),
    }
    parameters = model["parameters"]
    attack = parameters["attack"]
    concession = parameters["concession"]
    home_eta = (
        float(parameters["home_intercept"])
        + float(attack.get(home_team, 0.0))
        + float(concession.get(away_team, 0.0))
    )
    away_eta = (
        float(parameters["away_intercept"])
        + float(attack.get(away_team, 0.0))
        + float(concession.get(home_team, 0.0))
    )
    return math.exp(home_eta), math.exp(away_eta), unknown, component_audit


def _matrix_expectations(matrix: Sequence[Sequence[float]]) -> dict[str, float]:
    validated = _validate_matrix(matrix)
    home = math.fsum(
        home_count * probability
        for home_count, row in enumerate(validated)
        for probability in row
    )
    away = math.fsum(
        away_count * probability
        for row in validated
        for away_count, probability in enumerate(row)
    )
    covariance = math.fsum(
        (home_count - home) * (away_count - away) * probability
        for home_count, row in enumerate(validated)
        for away_count, probability in enumerate(row)
    )
    return {
        "home": home,
        "away": away,
        "total": home + away,
        "margin": home - away,
        "covariance": covariance,
    }


def predict_model(
    model: Mapping[str, Any],
    home_team: str,
    away_team: str,
    *,
    kickoff: str | datetime,
    generated_at: str | datetime | None = None,
    unknown_team_policy: str = "error",
    tail_tolerance: float = 1e-8,
    hard_max_corners: int = 80,
    total_markets: Iterable[tuple[str, float]] = (),
    corner_handicaps: Iterable[tuple[str, float]] = (),
) -> dict[str, Any]:
    validate_model(model)
    kickoff_time = _parse_aware_datetime(kickoff, "kickoff")
    prediction_time = _parse_aware_datetime(
        generated_at if generated_at is not None else _utc_now(), "generated_at"
    )
    model_time = _parse_aware_datetime(str(model["generated_at"]), "model.generated_at")
    if prediction_time < model_time:
        raise CornerModelError("prediction generated_at cannot predate model generation")
    if prediction_time >= kickoff_time:
        raise CornerModelError("prediction generated_at must be before kickoff")
    training_end = date.fromisoformat(str(model["training"]["end_date"]))
    training_cutoff = _parse_aware_datetime(
        str(model["training"]["cutoff_kickoff_utc"]),
        "model.training.cutoff_kickoff_utc",
    )
    if training_cutoff >= kickoff_time:
        raise CornerModelError(
            "training cutoff kickoff must be strictly before fixture kickoff"
        )
    home_mean, away_mean, unknown, component_audit = _fixture_rates(
        model, home_team, away_team, unknown_team_policy
    )
    tolerance = _require_positive(tail_tolerance, "tail_tolerance")
    if tolerance >= 1.0:
        raise CornerModelError("tail_tolerance must be less than one")
    marginal_tolerance = tolerance / 3.0
    home_distribution = nb2_distribution(
        home_mean,
        float(model["parameters"]["home_dispersion"]),
        tail_tolerance=marginal_tolerance,
        hard_max_corners=hard_max_corners,
    )
    away_distribution = nb2_distribution(
        away_mean,
        float(model["parameters"]["away_dispersion"]),
        tail_tolerance=marginal_tolerance,
        hard_max_corners=hard_max_corners,
    )
    raw_retained = (
        float(home_distribution["raw_retained_probability"])
        * float(away_distribution["raw_retained_probability"])
    )
    raw_omitted = max(0.0, 1.0 - raw_retained)
    if raw_omitted > tolerance + 1e-15:
        raise CornerModelError("joint corner tail exceeds requested tolerance")
    matrix = [
        [home_probability * away_probability / raw_retained for away_probability in away_distribution["probabilities"]]
        for home_probability in home_distribution["probabilities"]
    ]
    expected = _matrix_expectations(matrix)
    totals = [
        aggregate_corner_total(matrix, side, line) for side, line in total_markets
    ]
    handicaps = [
        aggregate_corner_handicap(matrix, side, line)
        for side, line in corner_handicaps
    ]
    warnings = []
    fallback_used = bool(unknown)
    if fallback_used:
        warnings.append(
            "league_average unknown-team fallback used; output is observation-only"
        )
    prediction: dict[str, Any] = {
        "artifact_type": PREDICTION_ARTIFACT_TYPE,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "model_version": model["model_version"],
        "model_hash": model["model_hash"],
        "formal_eligible": False,
        "authority_scope": "core_research_observation_only",
        "generated_at": prediction_time.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "fixture": {
            "league_key": model["training"]["dataset_profile"]["league_key"],
            "home_team": home_team,
            "away_team": away_team,
            "kickoff": kickoff_time.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            ),
            "kickoff_epoch": int(kickoff_time.timestamp()),
            "unknown_team_policy": unknown_team_policy,
            "unknown_teams": unknown,
            "fixture_graph_policy": component_audit["policy"],
            "home_training_component_id": component_audit[
                "home_training_component_id"
            ],
            "away_training_component_id": component_audit[
                "away_training_component_id"
            ],
            "same_training_component": component_audit[
                "same_training_component"
            ],
        },
        "provenance": {
            "training_source_data_hash": model["training"]["source_data_hash"],
            "training_source_lineage": copy.deepcopy(
                model["training"].get("source_lineage")
            ),
            "training_dataset_profile": copy.deepcopy(
                model["training"]["dataset_profile"]
            ),
            "training_cutoff_date": training_end.isoformat(),
            "training_cutoff_kickoff_utc": _canonical_datetime(
                training_cutoff, "training_cutoff"
            ),
            "training_cutoff_kickoff_epoch": int(training_cutoff.timestamp()),
            "strictly_before_kickoff": True,
            "generated_before_kickoff": True,
        },
        "dependence": {
            "model": DEPENDENCE_MODEL,
            "fitted_correlation": False,
            "analytical_covariance": 0.0,
            "matrix_covariance": expected["covariance"],
        },
        "distribution_parameters": {
            "home_mean": home_mean,
            "away_mean": away_mean,
            "home_dispersion": float(model["parameters"]["home_dispersion"]),
            "away_dispersion": float(model["parameters"]["away_dispersion"]),
        },
        "joint_corner_matrix": {
            "probabilities": matrix,
            "home_corners_max": len(matrix) - 1,
            "away_corners_max": len(matrix[0]) - 1,
            "normalization_factor": 1.0 / raw_retained,
        },
        "marginal_tail_audit": {
            "home_raw_retained_probability": home_distribution[
                "raw_retained_probability"
            ],
            "away_raw_retained_probability": away_distribution[
                "raw_retained_probability"
            ],
            "home_raw_omitted_probability": home_distribution[
                "raw_omitted_probability"
            ],
            "away_raw_omitted_probability": away_distribution[
                "raw_omitted_probability"
            ],
        },
        "tail_mass": {
            "tolerance": tolerance,
            "raw_retained_probability": raw_retained,
            "raw_omitted_probability": raw_omitted,
            "tolerance_met": True,
        },
        "expected_corners": {
            "home": expected["home"],
            "away": expected["away"],
            "total": expected["total"],
            "margin": expected["margin"],
        },
        "corner_totals": totals,
        "corner_handicaps": handicaps,
        "usage_policy": {
            "status": "observation_only",
            "unknown_team_fallback_used": fallback_used,
            "known_team_model_input": not fallback_used,
            "same_training_component_model_input": not fallback_used,
            "source_bound_manager_verified": False,
            "eligible_for_formal_model_input": False,
            "formal_ineligible_reason": (
                "standalone model output has not been verified against its "
                "registered source-bound dataset and evaluation"
            ),
        },
        "warnings": warnings,
    }
    prediction["prediction_hash"] = calculate_prediction_hash(prediction)
    validate_prediction(prediction, model=model)
    return prediction


def _assert_close(actual: Any, expected: float, name: str, tolerance: float = 1e-10) -> None:
    number = _require_finite(actual, name)
    if abs(number - expected) > tolerance:
        raise CornerModelError(f"{name} does not match its canonical distribution")


def _validate_market_list(
    raw: Any,
    matrix: Sequence[Sequence[float]],
    *,
    market: str,
) -> None:
    if not isinstance(raw, list):
        raise CornerModelError(f"{market} must be a list")
    observed: set[tuple[str, float]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise CornerModelError(f"{market}[{index}] must be an object")
        side = str(item.get("side") or "").lower()
        line = _require_finite(item.get("line"), f"{market}[{index}].line")
        key = (side, line)
        if key in observed:
            raise CornerModelError(f"{market} contains a duplicate requested market")
        observed.add(key)
        expected = (
            aggregate_corner_total(matrix, side, line)
            if market == "corner_totals"
            else aggregate_corner_handicap(matrix, side, line)
        )
        if item.get("split_lines") != expected["split_lines"]:
            raise CornerModelError(f"{market}[{index}] split_lines are inconsistent")
        supplied_probabilities = item.get("probabilities")
        if not isinstance(supplied_probabilities, Mapping) or set(
            supplied_probabilities
        ) != set(SETTLEMENT_STATES):
            raise CornerModelError(f"{market}[{index}] needs all five settlement states")
        for state in SETTLEMENT_STATES:
            _assert_close(
                supplied_probabilities[state],
                expected["probabilities"][state],
                f"{market}[{index}].probabilities.{state}",
            )
        for field in (
            "positive_state_probability",
            "fair_hong_kong_odds",
            "fair_decimal_odds",
        ):
            if expected[field] is None:
                if item.get(field) is not None:
                    raise CornerModelError(f"{market}[{index}].{field} must be null")
            else:
                _assert_close(
                    item.get(field), expected[field], f"{market}[{index}].{field}"
                )


def validate_prediction(
    prediction: Mapping[str, Any],
    *,
    model: Mapping[str, Any] | None = None,
) -> None:
    if not isinstance(prediction, Mapping):
        raise CornerModelError("prediction must be an object")
    if prediction.get("artifact_type") != PREDICTION_ARTIFACT_TYPE:
        raise CornerModelError("unexpected prediction artifact_type")
    if prediction.get("schema_version") != PREDICTION_SCHEMA_VERSION:
        raise CornerModelError("unsupported prediction schema_version")
    if prediction.get("model_version") != MODEL_VERSION:
        raise CornerModelError("unsupported prediction model_version")
    if (
        prediction.get("formal_eligible") is not False
        or prediction.get("authority_scope") != "core_research_observation_only"
    ):
        raise CornerModelError("core prediction authority must remain observation-only")
    if not HASH_RE.fullmatch(str(prediction.get("model_hash") or "")):
        raise CornerModelError("prediction model_hash must be a SHA-256 hash")
    stored_hash = prediction.get("prediction_hash")
    if not isinstance(stored_hash, str) or not HASH_RE.fullmatch(stored_hash):
        raise CornerModelError("prediction_hash must be a SHA-256 hash")
    if stored_hash != calculate_prediction_hash(prediction):
        raise CornerModelError("prediction_hash does not match prediction contents")
    generated = _parse_aware_datetime(
        str(prediction.get("generated_at") or ""), "prediction.generated_at"
    )
    fixture = prediction.get("fixture")
    if not isinstance(fixture, Mapping):
        raise CornerModelError("prediction fixture metadata is missing")
    home_team = str(fixture.get("home_team") or "").strip()
    away_team = str(fixture.get("away_team") or "").strip()
    league_key = str(fixture.get("league_key") or "").strip()
    if not LEAGUE_KEY_RE.fullmatch(league_key):
        raise CornerModelError("prediction fixture league_key is invalid")
    if not home_team or not away_team or home_team == away_team:
        raise CornerModelError("prediction fixture teams must be distinct and non-empty")
    kickoff = _parse_aware_datetime(str(fixture.get("kickoff") or ""), "fixture.kickoff")
    if fixture.get("kickoff_epoch") != int(kickoff.timestamp()):
        raise CornerModelError("prediction fixture kickoff_epoch is inconsistent")
    if generated >= kickoff:
        raise CornerModelError("prediction must be generated before kickoff")
    provenance = prediction.get("provenance")
    if not isinstance(provenance, Mapping):
        raise CornerModelError("prediction provenance is missing")
    try:
        cutoff = date.fromisoformat(
            str(provenance.get("training_cutoff_date") or "")
        )
    except ValueError as exc:
        raise CornerModelError("prediction training cutoff must be an ISO date") from exc
    cutoff_kickoff = _parse_aware_datetime(
        str(provenance.get("training_cutoff_kickoff_utc") or ""),
        "prediction.training_cutoff_kickoff_utc",
    )
    if provenance.get("training_cutoff_kickoff_epoch") != int(
        cutoff_kickoff.timestamp()
    ):
        raise CornerModelError("prediction training cutoff epoch is inconsistent")
    if cutoff != cutoff_kickoff.date() or cutoff_kickoff >= kickoff:
        raise CornerModelError("prediction training cutoff must predate kickoff")
    if (
        provenance.get("strictly_before_kickoff") is not True
        or provenance.get("generated_before_kickoff") is not True
    ):
        raise CornerModelError("prediction timing provenance is incomplete")
    if not HASH_RE.fullmatch(str(provenance.get("training_source_data_hash") or "")):
        raise CornerModelError("prediction training source hash is invalid")
    provenance_profile = provenance.get("training_dataset_profile")
    required_profile_fields = {
        "league_key",
        "rows",
        "kickoff_utc_start",
        "kickoff_utc_end",
        "kickoff_epoch_start",
        "kickoff_epoch_end",
        "seasons",
        "phases",
        "competition_regimes",
        "fixture_set_hash",
        "response_set_hash",
        "semantic_rows_hash",
        "fixture_graph",
    }
    if (
        not isinstance(provenance_profile, Mapping)
        or set(provenance_profile) != required_profile_fields
        or provenance_profile.get("league_key") != league_key
    ):
        raise CornerModelError(
            "prediction fixture league_key does not match its dataset profile"
        )
    for field in ("fixture_set_hash", "response_set_hash", "semantic_rows_hash"):
        if not HASH_RE.fullmatch(str(provenance_profile.get(field) or "")):
            raise CornerModelError(
                f"prediction dataset profile {field} is invalid"
            )
    profile_rows = provenance_profile.get("rows")
    if isinstance(profile_rows, bool) or not isinstance(profile_rows, int) or profile_rows < 2:
        raise CornerModelError("prediction dataset profile rows are invalid")
    component_map = _validate_fixture_graph_profile(
        provenance_profile["fixture_graph"], matches=profile_rows
    )
    profile_end = _parse_aware_datetime(
        str(provenance_profile.get("kickoff_utc_end") or ""),
        "prediction.dataset_profile.kickoff_utc_end",
    )
    if (
        profile_end != cutoff_kickoff
        or provenance_profile.get("kickoff_epoch_end")
        != int(cutoff_kickoff.timestamp())
    ):
        raise CornerModelError(
            "prediction dataset profile cutoff does not match provenance"
        )
    lineage = provenance.get("training_source_lineage")
    if lineage is not None and _normalize_source_lineage(lineage) != lineage:
        raise CornerModelError("prediction source lineage is not canonical")
    dependence = prediction.get("dependence")
    if not isinstance(dependence, Mapping) or dependence.get("model") != DEPENDENCE_MODEL:
        raise CornerModelError("prediction dependence must be independent_nb")
    if dependence.get("fitted_correlation") is not False:
        raise CornerModelError("prediction cannot claim a fitted correlation")
    _assert_close(dependence.get("analytical_covariance"), 0.0, "analytical covariance")
    parameters = prediction.get("distribution_parameters")
    if not isinstance(parameters, Mapping):
        raise CornerModelError("prediction distribution parameters are missing")
    home_mean = _require_positive(parameters.get("home_mean"), "home_mean")
    away_mean = _require_positive(parameters.get("away_mean"), "away_mean")
    home_dispersion = _require_positive(
        parameters.get("home_dispersion"), "home_dispersion"
    )
    away_dispersion = _require_positive(
        parameters.get("away_dispersion"), "away_dispersion"
    )
    matrix_payload = prediction.get("joint_corner_matrix")
    if not isinstance(matrix_payload, Mapping):
        raise CornerModelError("joint_corner_matrix is missing")
    matrix = _validate_matrix(matrix_payload.get("probabilities"))
    if matrix_payload.get("home_corners_max") != len(matrix) - 1:
        raise CornerModelError("home_corners_max is inconsistent")
    if matrix_payload.get("away_corners_max") != len(matrix[0]) - 1:
        raise CornerModelError("away_corners_max is inconsistent")
    home_raw = _nb2_prefix(home_mean, home_dispersion, len(matrix) - 1)
    away_raw = _nb2_prefix(away_mean, away_dispersion, len(matrix[0]) - 1)
    home_retained = math.fsum(home_raw)
    away_retained = math.fsum(away_raw)
    raw_retained = home_retained * away_retained
    raw_omitted = max(0.0, 1.0 - raw_retained)
    tail = prediction.get("tail_mass")
    if not isinstance(tail, Mapping):
        raise CornerModelError("tail_mass metadata is missing")
    tolerance = _require_positive(tail.get("tolerance"), "tail_mass.tolerance")
    if tolerance >= 1.0 or raw_omitted > tolerance + 1e-12:
        raise CornerModelError("prediction raw omitted tail exceeds tolerance")
    if tail.get("tolerance_met") is not True:
        raise CornerModelError("prediction tail tolerance must be met")
    _assert_close(
        tail.get("raw_retained_probability"),
        raw_retained,
        "tail_mass.raw_retained_probability",
    )
    _assert_close(
        tail.get("raw_omitted_probability"),
        raw_omitted,
        "tail_mass.raw_omitted_probability",
    )
    _assert_close(
        matrix_payload.get("normalization_factor"),
        1.0 / raw_retained,
        "joint_corner_matrix.normalization_factor",
    )
    marginal = prediction.get("marginal_tail_audit")
    if not isinstance(marginal, Mapping):
        raise CornerModelError("marginal_tail_audit is missing")
    for field, expected in (
        ("home_raw_retained_probability", home_retained),
        ("away_raw_retained_probability", away_retained),
        ("home_raw_omitted_probability", max(0.0, 1.0 - home_retained)),
        ("away_raw_omitted_probability", max(0.0, 1.0 - away_retained)),
    ):
        _assert_close(marginal.get(field), expected, f"marginal_tail_audit.{field}")
    for home, row in enumerate(matrix):
        for away, probability in enumerate(row):
            _assert_close(
                probability,
                home_raw[home] * away_raw[away] / raw_retained,
                f"joint_corner_matrix[{home}][{away}]",
                tolerance=1e-11,
            )
    expected = _matrix_expectations(matrix)
    _assert_close(
        dependence.get("matrix_covariance"),
        expected["covariance"],
        "dependence.matrix_covariance",
        tolerance=1e-9,
    )
    expected_payload = prediction.get("expected_corners")
    if not isinstance(expected_payload, Mapping):
        raise CornerModelError("expected_corners is missing")
    for field in ("home", "away", "total", "margin"):
        _assert_close(
            expected_payload.get(field), expected[field], f"expected_corners.{field}"
        )
    _validate_market_list(
        prediction.get("corner_totals"), matrix, market="corner_totals"
    )
    _validate_market_list(
        prediction.get("corner_handicaps"), matrix, market="corner_handicaps"
    )
    policy = str(fixture.get("unknown_team_policy") or "")
    unknown = fixture.get("unknown_teams")
    if policy not in {"error", "league_average"} or not isinstance(unknown, list):
        raise CornerModelError("fixture unknown-team metadata is invalid")
    if unknown != sorted(set(str(team) for team in unknown)) or any(
        not str(team).strip() for team in unknown
    ):
        raise CornerModelError("fixture unknown_teams must be sorted unique names")
    if not set(unknown).issubset({home_team, away_team}):
        raise CornerModelError("fixture unknown_teams must belong to the fixture")
    expected_unknown = sorted(
        team for team in (home_team, away_team) if team not in component_map
    )
    if unknown != expected_unknown:
        raise CornerModelError(
            "fixture unknown_teams do not match the training fixture graph"
        )
    home_component = component_map.get(home_team)
    away_component = component_map.get(away_team)
    if fixture.get("fixture_graph_policy") != CROSS_COMPONENT_PREDICTION_POLICY:
        raise CornerModelError("fixture graph prediction policy is invalid")
    if (
        fixture.get("home_training_component_id") != home_component
        or fixture.get("away_training_component_id") != away_component
    ):
        raise CornerModelError("fixture training component identity is inconsistent")
    expected_same_component = (
        home_component == away_component if not expected_unknown else None
    )
    if fixture.get("same_training_component") is not expected_same_component:
        raise CornerModelError("fixture component comparability is inconsistent")
    if not expected_unknown and not expected_same_component:
        raise CornerModelError("cross-component prediction must fail closed")
    usage = prediction.get("usage_policy")
    if not isinstance(usage, Mapping):
        raise CornerModelError("usage_policy is missing")
    fallback = bool(expected_unknown)
    if fallback and policy != "league_average":
        raise CornerModelError("unknown teams require league_average policy")
    verified = usage.get("source_bound_manager_verified") is True
    if verified:
        raise CornerModelError(
            "core prediction validation cannot grant manager verification authority"
        )
    expected_status = (
        "registered_model_distribution" if verified and not fallback else "observation_only"
    )
    if (
        usage.get("status") != expected_status
        or usage.get("unknown_team_fallback_used") is not fallback
        or usage.get("known_team_model_input") is not (not fallback)
        or usage.get("same_training_component_model_input") is not (not fallback)
        or usage.get("source_bound_manager_verified") is not verified
        or usage.get("eligible_for_formal_model_input") is not (verified and not fallback)
    ):
        raise CornerModelError(
            "usage_policy does not match source verification and unknown-team handling"
        )
    if not verified and not str(usage.get("formal_ineligible_reason") or "").strip():
        raise CornerModelError(
            "unverified standalone prediction needs a formal-ineligible reason"
        )
    warnings = prediction.get("warnings")
    if not isinstance(warnings, list) or (fallback and not warnings):
        raise CornerModelError("unknown-team fallback requires an observation warning")
    if model is not None:
        validate_model(model)
        if prediction.get("model_hash") != model.get("model_hash"):
            raise CornerModelError("prediction model_hash does not match model")
        if provenance.get("training_source_data_hash") != model["training"][
            "source_data_hash"
        ]:
            raise CornerModelError("prediction source hash does not match model")
        if provenance.get("training_source_lineage") != model["training"].get(
            "source_lineage"
        ):
            raise CornerModelError("prediction source lineage does not match model")
        if provenance.get("training_dataset_profile") != model["training"].get(
            "dataset_profile"
        ):
            raise CornerModelError("prediction dataset profile does not match model")
        if league_key != model["training"]["dataset_profile"]["league_key"]:
            raise CornerModelError("prediction league_key does not match model")
        if (
            cutoff.isoformat() != model["training"]["end_date"]
            or _canonical_datetime(cutoff_kickoff, "prediction cutoff")
            != model["training"]["cutoff_kickoff_utc"]
        ):
            raise CornerModelError("prediction cutoff does not match model")
        model_generated = _parse_aware_datetime(
            str(model["generated_at"]), "model.generated_at"
        )
        if generated < model_generated:
            raise CornerModelError("prediction predates model generation")
        expected_home, expected_away, expected_unknown, expected_components = _fixture_rates(
            model,
            home_team,
            away_team,
            policy,
        )
        _assert_close(home_mean, expected_home, "home_mean versus model")
        _assert_close(away_mean, expected_away, "away_mean versus model")
        if unknown != expected_unknown:
            raise CornerModelError("prediction unknown teams do not match model")
        if fixture.get("home_training_component_id") != expected_components.get(
            "home_training_component_id"
        ) or fixture.get("away_training_component_id") != expected_components.get(
            "away_training_component_id"
        ):
            raise CornerModelError("prediction fixture components do not match model")


def save_json(value: Mapping[str, Any], output: str | Path | None) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    if output is None:
        sys.stdout.write(payload)
        return
    path = Path(output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=path.parent, suffix=".tmp"
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)


def load_model(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CornerModelError(f"cannot load model: {path}") from exc
    if not isinstance(value, dict):
        raise CornerModelError("model JSON must contain an object")
    validate_model(value)
    return value


def load_prediction(
    path: str | Path, *, model: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CornerModelError(f"cannot load prediction: {path}") from exc
    if not isinstance(value, dict):
        raise CornerModelError("prediction JSON must contain an object")
    validate_prediction(value, model=model)
    return value


def _write_records_csv(path: Path, records: Sequence[Mapping[str, Any]]) -> str:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=TRAINING_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        for row in records:
            writer.writerow(
                {
                    "date": row["date"].isoformat(),
                    "kickoff_utc": _canonical_datetime(
                        row["kickoff_utc"], "kickoff_utc"
                    ),
                    "kickoff_epoch": int(row["kickoff_epoch"]),
                    "league_key": row["league_key"],
                    "home_team": row["home_team"],
                    "away_team": row["away_team"],
                    "home_corners": row["home_corners"],
                    "away_corners": row["away_corners"],
                    "match_id": row["match_id"],
                    "season": row["season"],
                    "phase": row["phase"],
                    "competition_regime": row["competition_regime"],
                    "fixture_fingerprint": row["fixture_fingerprint"],
                    "source_url": row["source_url"],
                    "source_collected_at": _canonical_datetime(
                        row["source_collected_at"], "source_collected_at"
                    ),
                    "source_response_sha256": row["source_response_sha256"],
                }
            )
    return _source_hash(path)


def _date_groups(records: Sequence[Mapping[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for row in records:
        if not groups or groups[-1][0]["date"] != row["date"]:
            groups.append([])
        groups[-1].append(dict(row))
    return groups


def _count_crps(probabilities: Sequence[float], actual: int, minimum: int = 0) -> float:
    if actual < minimum:
        raise CornerModelError("actual count is below distribution support")
    maximum = minimum + len(probabilities) - 1
    upper = max(maximum, actual)
    cumulative = 0.0
    score = 0.0
    for value in range(minimum, upper + 1):
        if minimum <= value <= maximum:
            cumulative += probabilities[value - minimum]
        observed = 1.0 if value >= actual else 0.0
        score += (cumulative - observed) ** 2
    return score


def _total_distribution(matrix: Sequence[Sequence[float]]) -> list[float]:
    validated = _validate_matrix(matrix)
    probabilities = [0.0] * (len(validated) + len(validated[0]) - 1)
    for home, row in enumerate(validated):
        for away, probability in enumerate(row):
            probabilities[home + away] += probability
    return probabilities


def _margin_distribution(
    matrix: Sequence[Sequence[float]],
) -> tuple[int, list[float]]:
    validated = _validate_matrix(matrix)
    minimum = -(len(validated[0]) - 1)
    maximum = len(validated) - 1
    probabilities = [0.0] * (maximum - minimum + 1)
    for home, row in enumerate(validated):
        for away, probability in enumerate(row):
            probabilities[home - away - minimum] += probability
    return minimum, probabilities


def _outer_matrix(
    home_probabilities: Sequence[float], away_probabilities: Sequence[float]
) -> list[list[float]]:
    retained = math.fsum(float(value) for value in home_probabilities) * math.fsum(
        float(value) for value in away_probabilities
    )
    if retained <= 0.0:
        raise CornerModelError("baseline distributions have zero retained mass")
    matrix = [
        [float(home) * float(away) / retained for away in away_probabilities]
        for home in home_probabilities
    ]
    return _validate_matrix(matrix)


def _weighted_empirical_distribution(
    counts: Sequence[int], weights: Sequence[float], *, maximum: int = 99
) -> list[float]:
    if len(counts) != len(weights) or not counts:
        raise CornerModelError("empirical baseline counts and weights are inconsistent")
    if any(value < 0 or value > maximum for value in counts):
        raise CornerModelError("empirical baseline count is outside its support")
    # A total prior mass of one match prevents unseen-count infinities.  Centre
    # that mass on the training-only league mean instead of spreading it
    # uniformly over 0..99: a uniform prior has mean 49.5 and badly distorts
    # the small early walk-forward blocks for which smoothing matters most.
    prior_mean = max(
        _weighted_mean([float(value) for value in counts], weights), 1e-3
    )
    prior_dispersion = _moment_dispersion(counts, weights)
    raw_prior = _nb2_prefix(prior_mean, prior_dispersion, maximum)
    retained_prior = math.fsum(raw_prior)
    if retained_prior <= 0.0:
        raise CornerModelError("empirical baseline smoothing prior has zero mass")
    mass = [value / retained_prior for value in raw_prior]
    for count, weight in zip(counts, weights):
        mass[count] += float(weight)
    total = math.fsum(mass)
    return [value / total for value in mass]


def _block_baselines(
    records: Sequence[Mapping[str, Any]],
    *,
    half_life_days: float,
    tail_tolerance: float,
    hard_max_corners: int,
) -> dict[str, dict[str, Any]]:
    weights = _time_weights(records, half_life_days)
    home_counts = [int(row["home_corners"]) for row in records]
    away_counts = [int(row["away_corners"]) for row in records]
    empirical_home = _weighted_empirical_distribution(home_counts, weights)
    empirical_away = _weighted_empirical_distribution(away_counts, weights)

    home_mean = max(
        _weighted_mean([float(value) for value in home_counts], weights), 1e-3
    )
    away_mean = max(
        _weighted_mean([float(value) for value in away_counts], weights), 1e-3
    )
    home_dispersion = _moment_dispersion(home_counts, weights)
    away_dispersion = _moment_dispersion(away_counts, weights)
    marginal_tolerance = _require_positive(tail_tolerance, "tail_tolerance") / 3.0
    nb_home = nb2_distribution(
        home_mean,
        home_dispersion,
        tail_tolerance=marginal_tolerance,
        hard_max_corners=hard_max_corners,
    )["probabilities"]
    nb_away = nb2_distribution(
        away_mean,
        away_dispersion,
        tail_tolerance=marginal_tolerance,
        hard_max_corners=hard_max_corners,
    )["probabilities"]
    return {
        "league_empirical": {
            "method": "time_weighted_mean_centered_smoothed_independent_empirical",
            "home_probabilities": empirical_home,
            "away_probabilities": empirical_away,
            "matrix": _outer_matrix(empirical_home, empirical_away),
        },
        "league_nb": {
            "method": "time_weighted_league_average_independent_nb2",
            "home_mean": home_mean,
            "away_mean": away_mean,
            "home_dispersion": home_dispersion,
            "away_dispersion": away_dispersion,
            "home_probabilities": nb_home,
            "away_probabilities": nb_away,
            "matrix": _outer_matrix(nb_home, nb_away),
        },
    }


def _score_baseline(
    baseline: Mapping[str, Any], actual_home: int, actual_away: int
) -> dict[str, float]:
    matrix = baseline["matrix"]
    home_probabilities = baseline["home_probabilities"]
    away_probabilities = baseline["away_probabilities"]
    if baseline.get("method") == "time_weighted_league_average_independent_nb2":
        joint_log_loss = -(
            nb2_log_pmf(
                actual_home,
                float(baseline["home_mean"]),
                float(baseline["home_dispersion"]),
            )
            + nb2_log_pmf(
                actual_away,
                float(baseline["away_mean"]),
                float(baseline["away_dispersion"]),
            )
        )
    else:
        probability = (
            float(home_probabilities[actual_home])
            * float(away_probabilities[actual_away])
        )
        joint_log_loss = -math.log(max(probability, LOG_LOSS_FLOOR))
    totals = _total_distribution(matrix)
    margin_minimum, margins = _margin_distribution(matrix)
    return {
        "joint_log_loss": joint_log_loss,
        "total_crps": _count_crps(totals, actual_home + actual_away),
        "margin_crps": _count_crps(
            margins, actual_home - actual_away, margin_minimum
        ),
    }


def _paired_comparison(
    model_values: Sequence[float],
    baseline_values: Sequence[float],
    block_ids: Sequence[int],
) -> dict[str, Any]:
    if (
        len(model_values) != len(baseline_values)
        or len(model_values) != len(block_ids)
        or not model_values
    ):
        raise CornerModelError("paired baseline comparison is empty or inconsistent")
    improvements = [
        float(baseline) - float(model)
        for model, baseline in zip(model_values, baseline_values)
    ]
    count = len(improvements)
    mean = math.fsum(improvements) / count
    baseline_mean = math.fsum(float(value) for value in baseline_values) / count
    model_mean = math.fsum(float(value) for value in model_values) / count
    clusters: dict[int, list[float]] = {}
    for block, improvement in zip(block_ids, improvements):
        clusters.setdefault(int(block), []).append(improvement)
    independent_units = len(clusters)
    if independent_units >= 2:
        cluster_sums = [
            math.fsum(value - mean for value in values)
            for values in clusters.values()
        ]
        variance = (
            independent_units
            / (independent_units - 1)
            * math.fsum(value * value for value in cluster_sums)
            / (count * count)
        )
        standard_error = math.sqrt(max(0.0, variance))
        lower_bound: float | None = mean - ONE_SIDED_95_Z * standard_error
    else:
        standard_error = None
        lower_bound = None
    return {
        "predictions": count,
        "independent_units": independent_units,
        "uncertainty_unit": "walk_forward_block_cluster",
        "model_mean": model_mean,
        "baseline_mean": baseline_mean,
        "mean_improvement": mean,
        "relative_improvement": (
            mean / baseline_mean if baseline_mean > 0.0 else None
        ),
        "sample_standard_error": standard_error,
        "one_sided_95_lower_bound": lower_bound,
        "uncertainty_estimable": independent_units >= 2,
    }


def backtest_model(
    csv_path: str | Path,
    *,
    min_train_matches: int = 200,
    test_block_size: int = 50,
    half_life_days: float = 365.0,
    iterations: int = 300,
    learning_rate: float = 0.03,
    regularization: float = 0.02,
    unknown_team_policy: str = "error",
    tail_tolerance: float = 1e-8,
    hard_max_corners: int = 80,
    source_lineage: Mapping[str, Any] | None = None,
    allow_research_cohorts: bool = False,
) -> dict[str, Any]:
    """Run a deterministic expanding-window backtest on real UTC kickoffs."""

    if (
        isinstance(min_train_matches, bool)
        or not isinstance(min_train_matches, int)
        or min_train_matches < 2
    ):
        raise CornerModelError("min_train_matches must be at least two")
    if (
        isinstance(test_block_size, bool)
        or not isinstance(test_block_size, int)
        or test_block_size < 1
    ):
        raise CornerModelError("test_block_size must be positive")
    if unknown_team_policy not in {"error", "league_average"}:
        raise CornerModelError("unknown_team_policy must be error or league_average")
    source = Path(csv_path).resolve()
    records = load_training_csv(
        source, allow_research_cohorts=allow_research_cohorts
    )
    normalized_lineage = _normalize_source_lineage(source_lineage)
    dataset_profile = training_dataset_profile(records)
    groups = _date_groups(records)
    holdout_groups: list[list[dict[str, Any]]] = []
    holdout_target = max(1, math.ceil(len(records) * HOLDOUT_FRACTION))
    development_groups = list(groups)
    holdout_count = 0
    while development_groups and holdout_count < holdout_target:
        group = development_groups.pop()
        holdout_groups.insert(0, group)
        holdout_count += len(group)
    # Never sacrifice the basic expanding-window evaluation merely to create a
    # nominal holdout from an undersized history.  In that case the artifact is
    # explicitly development-only and the manager forbids candidate status.
    if sum(len(group) for group in development_groups) <= min_train_matches:
        development_groups = list(groups)
        holdout_groups = []
        holdout_count = 0
    train_groups: list[list[dict[str, Any]]] = []
    remaining = list(development_groups)
    train_count = 0
    while remaining and train_count < min_train_matches:
        group = remaining.pop(0)
        train_groups.append(group)
        train_count += len(group)
    if not remaining:
        raise CornerModelError("backtest needs matches after the initial training window")
    remaining_matches = sum(len(group) for group in remaining)
    adaptive_block_size = math.ceil(
        remaining_matches / MAX_WALK_FORWARD_BLOCKS
    )
    effective_test_block_size = max(test_block_size, adaptive_block_size)

    blocks: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    excluded_unknown = 0
    excluded_component_incomparable = 0
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        block_number = 0
        while remaining:
            test_groups: list[list[dict[str, Any]]] = []
            test_count = 0
            while remaining and test_count < effective_test_block_size:
                group = remaining.pop(0)
                test_groups.append(group)
                test_count += len(group)
            training_rows = [row for group in train_groups for row in group]
            test_rows = [row for group in test_groups for row in group]
            block_number += 1
            subset_path = directory / f"corner-train-{block_number}.csv"
            subset_hash = _write_records_csv(subset_path, training_rows)
            test_start = min(row["date"] for row in test_rows)
            first_test_kickoff = min(row["kickoff_utc"] for row in test_rows)
            generated_time = first_test_kickoff - timedelta(microseconds=1)
            generated = _canonical_datetime(generated_time, "block.generated_at")
            model = _fit_records(
                training_rows,
                source_data_hash=subset_hash,
                source_name=subset_path.name,
                half_life_days=half_life_days,
                iterations=iterations,
                learning_rate=learning_rate,
                regularization=regularization,
                generated_at=generated,
                historical_simulation=True,
                research_cohort_opt_in=allow_research_cohorts,
            )
            cutoff = max(row["date"] for row in training_rows)
            cutoff_kickoff = max(row["kickoff_utc"] for row in training_rows)
            if cutoff >= test_start or cutoff_kickoff >= first_test_kickoff:
                raise CornerModelError("walk-forward training cutoff leaked into test block")
            baselines = _block_baselines(
                training_rows,
                half_life_days=half_life_days,
                tail_tolerance=tail_tolerance,
                hard_max_corners=hard_max_corners,
            )
            block_forecasts = 0
            block_excluded = 0
            block_component_excluded = 0
            for row in test_rows:
                kickoff = _canonical_datetime(row["kickoff_utc"], "fixture.kickoff")
                try:
                    prediction = predict_model(
                        model,
                        str(row["home_team"]),
                        str(row["away_team"]),
                        kickoff=kickoff,
                        generated_at=generated,
                        unknown_team_policy=unknown_team_policy,
                        tail_tolerance=tail_tolerance,
                        hard_max_corners=hard_max_corners,
                    )
                except CornerModelError as exc:
                    message = str(exc)
                    if unknown_team_policy == "error" and "unknown team" in message:
                        excluded_unknown += 1
                        block_excluded += 1
                        continue
                    if "cross-component fixture" in message:
                        excluded_component_incomparable += 1
                        block_component_excluded += 1
                        continue
                    raise
                matrix = prediction["joint_corner_matrix"]["probabilities"]
                actual_home = int(row["home_corners"])
                actual_away = int(row["away_corners"])
                parameters = prediction["distribution_parameters"]
                joint_probability = math.exp(
                    nb2_log_pmf(
                        actual_home,
                        float(parameters["home_mean"]),
                        float(parameters["home_dispersion"]),
                    )
                    + nb2_log_pmf(
                        actual_away,
                        float(parameters["away_mean"]),
                        float(parameters["away_dispersion"]),
                    )
                )
                totals = _total_distribution(matrix)
                margin_minimum, margins = _margin_distribution(matrix)
                expected = prediction["expected_corners"]
                total_diagnostic_line = round(float(expected["total"]) * 4.0) / 4.0
                handicap_diagnostic_line = (
                    round(-float(expected["margin"]) * 4.0) / 4.0
                )
                baseline_scores = {
                    baseline_name: _score_baseline(
                        baseline, actual_home, actual_away
                    )
                    for baseline_name, baseline in baselines.items()
                }
                forecasts.append(
                    {
                        "date": row["date"].isoformat(),
                        "block": block_number,
                        "kickoff_utc": kickoff,
                        "kickoff_epoch": int(row["kickoff_epoch"]),
                        "league_key": row["league_key"],
                        "match_id": row["match_id"],
                        "home_team": row["home_team"],
                        "away_team": row["away_team"],
                        "season": row["season"],
                        "phase": row["phase"],
                        "competition_regime": row["competition_regime"],
                        "fixture_fingerprint": row["fixture_fingerprint"],
                        "source_response_sha256": row["source_response_sha256"],
                        "actual_home_corners": actual_home,
                        "actual_away_corners": actual_away,
                        "actual_total_corners": actual_home + actual_away,
                        "actual_corner_margin": actual_home - actual_away,
                        "model_hash": model["model_hash"],
                        "prediction_hash": prediction["prediction_hash"],
                        "training_cutoff_date": cutoff.isoformat(),
                        "training_cutoff_kickoff_utc": _canonical_datetime(
                            cutoff_kickoff, "training_cutoff_kickoff"
                        ),
                        "training_cutoff_kickoff_epoch": int(
                            cutoff_kickoff.timestamp()
                        ),
                        "unknown_team_fallback_used": prediction["usage_policy"][
                            "unknown_team_fallback_used"
                        ],
                        "joint_probability": joint_probability,
                        "home_probability": math.exp(
                            nb2_log_pmf(
                                actual_home,
                                float(parameters["home_mean"]),
                                float(parameters["home_dispersion"]),
                            )
                        ),
                        "away_probability": math.exp(
                            nb2_log_pmf(
                                actual_away,
                                float(parameters["away_mean"]),
                                float(parameters["away_dispersion"]),
                            )
                        ),
                        "distribution_parameters": copy.deepcopy(parameters),
                        "expected_corners": copy.deepcopy(expected),
                        "settlement_diagnostics": {
                            "corner_total": aggregate_corner_total(
                                matrix, "over", total_diagnostic_line
                            ),
                            "corner_handicap": aggregate_corner_handicap(
                                matrix, "home", handicap_diagnostic_line
                            ),
                        },
                        "joint_log_loss": -math.log(
                            max(joint_probability, LOG_LOSS_FLOOR)
                        ),
                        "home_log_loss": -nb2_log_pmf(
                            actual_home,
                            float(parameters["home_mean"]),
                            float(parameters["home_dispersion"]),
                        ),
                        "away_log_loss": -nb2_log_pmf(
                            actual_away,
                            float(parameters["away_mean"]),
                            float(parameters["away_dispersion"]),
                        ),
                        "total_crps": _count_crps(
                            totals, actual_home + actual_away
                        ),
                        "margin_crps": _count_crps(
                            margins, actual_home - actual_away, margin_minimum
                        ),
                        "home_mae": abs(float(expected["home"]) - actual_home),
                        "away_mae": abs(float(expected["away"]) - actual_away),
                        "total_mae": abs(
                            float(expected["total"]) - actual_home - actual_away
                        ),
                        "baselines": baseline_scores,
                    }
                )
                block_forecasts += 1
            blocks.append(
                {
                    "block": block_number,
                    "training_matches": len(training_rows),
                    "training_cutoff_date": cutoff.isoformat(),
                    "training_cutoff_kickoff_utc": _canonical_datetime(
                        cutoff_kickoff, "training_cutoff_kickoff"
                    ),
                    "training_cutoff_kickoff_epoch": int(
                        cutoff_kickoff.timestamp()
                    ),
                    "test_dates": [
                        group[0]["date"].isoformat() for group in test_groups
                    ],
                    "test_kickoff_utc_start": _canonical_datetime(
                        first_test_kickoff, "test_kickoff_utc_start"
                    ),
                    "test_kickoff_utc_end": _canonical_datetime(
                        max(row["kickoff_utc"] for row in test_rows),
                        "test_kickoff_utc_end",
                    ),
                    "test_matches": len(test_rows),
                    "forecast_matches": block_forecasts,
                    "excluded_unknown_team_matches": block_excluded,
                    "excluded_component_incomparable_matches": block_component_excluded,
                    "model_hash": model["model_hash"],
                }
            )
            train_groups.extend(test_groups)
    if not forecasts:
        raise CornerModelError("backtest produced no eligible predictions")

    def average(field: str) -> float:
        return math.fsum(float(row[field]) for row in forecasts) / len(forecasts)

    baseline_summary: dict[str, Any] = {}
    comparisons: dict[str, Any] = {}
    for baseline_name in BASELINE_NAMES:
        baseline_summary[baseline_name] = {
            "method": (
                "time_weighted_mean_centered_smoothed_independent_empirical"
                if baseline_name == "league_empirical"
                else "time_weighted_league_average_independent_nb2"
            ),
            "predictions": len(forecasts),
            "metrics": {
                metric: math.fsum(
                    float(row["baselines"][baseline_name][metric])
                    for row in forecasts
                )
                / len(forecasts)
                for metric in COMPARISON_METRICS
            },
        }
        comparisons[baseline_name] = {
            metric: _paired_comparison(
                [float(row[metric]) for row in forecasts],
                [
                    float(row["baselines"][baseline_name][metric])
                    for row in forecasts
                ],
                [int(row["block"]) for row in forecasts],
            )
            for metric in COMPARISON_METRICS
        }

    holdout_rows = [row for group in holdout_groups for row in group]
    holdout_forecasts: list[dict[str, Any]] = []
    holdout_excluded = 0
    holdout_component_excluded = 0
    holdout_model_hash: str | None = None
    if holdout_rows:
        holdout_training_rows = [
            row for group in development_groups for row in group
        ]
        first_holdout_kickoff = min(row["kickoff_utc"] for row in holdout_rows)
        holdout_generated = _canonical_datetime(
            first_holdout_kickoff - timedelta(microseconds=1),
            "holdout.generated_at",
        )
        with tempfile.TemporaryDirectory() as holdout_temporary:
            holdout_train_path = Path(holdout_temporary) / "corner-holdout-train.csv"
            holdout_train_hash = _write_records_csv(
                holdout_train_path, holdout_training_rows
            )
            holdout_model = _fit_records(
                holdout_training_rows,
                source_data_hash=holdout_train_hash,
                source_name=holdout_train_path.name,
                half_life_days=half_life_days,
                iterations=iterations,
                learning_rate=learning_rate,
                regularization=regularization,
                generated_at=holdout_generated,
                historical_simulation=True,
                research_cohort_opt_in=allow_research_cohorts,
            )
        holdout_model_hash = str(holdout_model["model_hash"])
        holdout_baselines = _block_baselines(
            holdout_training_rows,
            half_life_days=half_life_days,
            tail_tolerance=tail_tolerance,
            hard_max_corners=hard_max_corners,
        )
        holdout_group_by_date = {
            group[0]["date"]: index
            for index, group in enumerate(holdout_groups, start=1)
        }
        for row in holdout_rows:
            try:
                prediction = predict_model(
                    holdout_model,
                    str(row["home_team"]),
                    str(row["away_team"]),
                    kickoff=_canonical_datetime(row["kickoff_utc"], "holdout.kickoff"),
                    generated_at=holdout_generated,
                    unknown_team_policy=unknown_team_policy,
                    tail_tolerance=tail_tolerance,
                    hard_max_corners=hard_max_corners,
                )
            except CornerModelError as exc:
                message = str(exc)
                if unknown_team_policy == "error" and "unknown team" in message:
                    holdout_excluded += 1
                    continue
                if "cross-component fixture" in message:
                    holdout_component_excluded += 1
                    continue
                raise
            parameters = prediction["distribution_parameters"]
            matrix = prediction["joint_corner_matrix"]["probabilities"]
            actual_home = int(row["home_corners"])
            actual_away = int(row["away_corners"])
            expected = prediction["expected_corners"]
            total_distribution = _total_distribution(matrix)
            margin_minimum, margin_distribution = _margin_distribution(matrix)
            joint_log_loss = -(
                nb2_log_pmf(
                    actual_home,
                    float(parameters["home_mean"]),
                    float(parameters["home_dispersion"]),
                )
                + nb2_log_pmf(
                    actual_away,
                    float(parameters["away_mean"]),
                    float(parameters["away_dispersion"]),
                )
            )
            holdout_forecasts.append(
                {
                    "date": row["date"].isoformat(),
                    "date_group": holdout_group_by_date[row["date"]],
                    "kickoff_utc": _canonical_datetime(
                        row["kickoff_utc"], "holdout.kickoff"
                    ),
                    "match_id": row["match_id"],
                    "fixture_fingerprint": row["fixture_fingerprint"],
                    "source_response_sha256": row["source_response_sha256"],
                    "prediction_hash": prediction["prediction_hash"],
                    "joint_log_loss": joint_log_loss,
                    "home_log_loss": -nb2_log_pmf(
                        actual_home,
                        float(parameters["home_mean"]),
                        float(parameters["home_dispersion"]),
                    ),
                    "away_log_loss": -nb2_log_pmf(
                        actual_away,
                        float(parameters["away_mean"]),
                        float(parameters["away_dispersion"]),
                    ),
                    "total_crps": _count_crps(
                        total_distribution, actual_home + actual_away
                    ),
                    "margin_crps": _count_crps(
                        margin_distribution,
                        actual_home - actual_away,
                        margin_minimum,
                    ),
                    "home_mae": abs(float(expected["home"]) - actual_home),
                    "away_mae": abs(float(expected["away"]) - actual_away),
                    "total_mae": abs(
                        float(expected["total"]) - actual_home - actual_away
                    ),
                    "baselines": {
                        name: _score_baseline(
                            baseline, actual_home, actual_away
                        )
                        for name, baseline in holdout_baselines.items()
                    },
                }
            )

    holdout_available = (
        len(holdout_rows) >= MIN_HOLDOUT_MATCHES
        and len(holdout_groups) >= MIN_HOLDOUT_DATE_GROUPS
        and len(holdout_forecasts) >= MIN_HOLDOUT_MATCHES
    )
    holdout_metric_fields = (
        "joint_log_loss",
        "home_log_loss",
        "away_log_loss",
        "total_crps",
        "margin_crps",
        "home_mae",
        "away_mae",
        "total_mae",
    )
    holdout_metrics = (
        {
            field: math.fsum(float(row[field]) for row in holdout_forecasts)
            / len(holdout_forecasts)
            for field in holdout_metric_fields
        }
        if holdout_forecasts
        else None
    )
    holdout_comparisons = (
        {
            baseline: {
                metric: _paired_comparison(
                    [float(row[metric]) for row in holdout_forecasts],
                    [
                        float(row["baselines"][baseline][metric])
                        for row in holdout_forecasts
                    ],
                    [int(row["date_group"]) for row in holdout_forecasts],
                )
                for metric in COMPARISON_METRICS
            }
            for baseline in BASELINE_NAMES
        }
        if holdout_forecasts
        else {}
    )
    holdout_report = {
        "policy_version": HOLDOUT_POLICY_VERSION,
        "selection": "latest_complete_utc_date_groups_20_percent",
        "minimum_matches": MIN_HOLDOUT_MATCHES,
        "minimum_date_groups": MIN_HOLDOUT_DATE_GROUPS,
        "untouched_by_development_walk_forward": bool(holdout_rows),
        "not_used_in_candidate_metric_thresholds": True,
        "status": "available" if holdout_available else "insufficient_history",
        "development_only": not holdout_available,
        "training_matches": sum(len(group) for group in development_groups),
        "matches": len(holdout_rows),
        "date_groups": len(holdout_groups),
        "predictions": len(holdout_forecasts),
        "excluded_unknown_team_matches": holdout_excluded,
        "excluded_component_incomparable_matches": holdout_component_excluded,
        "date_start": holdout_rows[0]["date"].isoformat() if holdout_rows else None,
        "date_end": holdout_rows[-1]["date"].isoformat() if holdout_rows else None,
        "model_hash": holdout_model_hash,
        "metrics": holdout_metrics,
        "comparisons": holdout_comparisons,
        "prediction_audit": holdout_forecasts,
    }

    backtest: dict[str, Any] = {
        "artifact_type": BACKTEST_ARTIFACT_TYPE,
        "schema_version": BACKTEST_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "dependence": DEPENDENCE_MODEL,
        "source_data_hash": _source_hash(source),
        "source_lineage": normalized_lineage,
        "dataset_profile": dataset_profile,
        "evaluation_policy": {
            "split": "bounded_expanding_window_complete_utc_date_groups_v2",
            "evidence_scope": (
                "development_plus_untouched_fixed_holdout"
                if holdout_available
                else "development_only"
            ),
            "untouched_fixed_holdout_bound": holdout_available,
            "same_date_groups_kept_together": True,
            "requested_test_block_size": int(test_block_size),
            "effective_test_block_size": int(effective_test_block_size),
            "maximum_walk_forward_blocks": MAX_WALK_FORWARD_BLOCKS,
            "real_kickoff_utc_required": True,
            "synthetic_kickoff_forbidden": True,
            "archival_collection_time_not_treated_as_historical_availability": True,
            "unknown_team_policy": unknown_team_policy,
            "cross_component_prediction_policy": CROSS_COMPONENT_PREDICTION_POLICY,
            "research_cohort_opt_in": bool(allow_research_cohorts),
        },
        "fit_config": {
            "half_life_days": float(half_life_days),
            "iterations": int(iterations),
            "learning_rate": float(learning_rate),
            "regularization": float(regularization),
            "tail_tolerance": float(tail_tolerance),
            "hard_max_corners": int(hard_max_corners),
            "min_train_matches": int(min_train_matches),
            "test_block_size": int(test_block_size),
        },
        "sample": {
            "input_matches": len(records),
            "predictions": len(forecasts),
            "excluded_unknown_team_matches": excluded_unknown,
            "excluded_component_incomparable_matches": excluded_component_incomparable,
            "blocks": len(blocks),
        },
        "metrics": {
            field: average(field)
            for field in (
                "joint_log_loss",
                "home_log_loss",
                "away_log_loss",
                "total_crps",
                "margin_crps",
                "home_mae",
                "away_mae",
                "total_mae",
            )
        },
        "baselines": baseline_summary,
        "comparisons": comparisons,
        "untouched_holdout": holdout_report,
        "comparison_policy": {
            "paired_on_same_walk_forward_predictions": True,
            "metrics": list(COMPARISON_METRICS),
            "baselines": list(BASELINE_NAMES),
            "confidence": "one_sided_95_normal_approximation",
            "z_value": ONE_SIDED_95_Z,
        },
        "blocks": blocks,
        "predictions": forecasts,
    }
    backtest["backtest_hash"] = calculate_backtest_hash(backtest)
    return backtest


def _parse_market(raw: str, name: str, sides: set[str]) -> tuple[str, float]:
    try:
        side, line = raw.split(":", 1)
        side = side.strip().lower()
        value = float(line)
    except (AttributeError, ValueError) as exc:
        raise CornerModelError(f"{name} must be SIDE:LINE") from exc
    if side not in sides or not math.isfinite(value):
        raise CornerModelError(f"invalid {name}: {raw}")
    _split_quarter_line(value)
    return side, value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fit = subparsers.add_parser("fit", help="fit an independent NB2 corner model")
    fit.add_argument("--input", required=True)
    fit.add_argument("--output", required=True)
    fit.add_argument("--half-life-days", type=float, default=365.0)
    fit.add_argument("--iterations", type=int, default=600)
    fit.add_argument("--learning-rate", type=float, default=0.03)
    fit.add_argument("--regularization", type=float, default=0.02)
    fit.add_argument("--generated-at")

    predict = subparsers.add_parser("predict", help="create a corner prediction artifact")
    predict.add_argument("--model", required=True)
    predict.add_argument("--home-team", required=True)
    predict.add_argument("--away-team", required=True)
    predict.add_argument("--kickoff", required=True)
    predict.add_argument("--generated-at")
    predict.add_argument("--output", required=True)
    predict.add_argument(
        "--unknown-team-policy", choices=("error", "league_average"), default="error"
    )
    predict.add_argument("--tail-tolerance", type=float, default=1e-8)
    predict.add_argument("--hard-max-corners", type=int, default=80)
    predict.add_argument("--total", action="append", default=[])
    predict.add_argument("--handicap", action="append", default=[])

    backtest = subparsers.add_parser(
        "backtest", help="run an expanding-window complete-date backtest"
    )
    backtest.add_argument("--input", required=True)
    backtest.add_argument("--output", required=True)
    backtest.add_argument("--min-train-matches", type=int, default=200)
    backtest.add_argument("--test-block-size", type=int, default=50)
    backtest.add_argument("--half-life-days", type=float, default=365.0)
    backtest.add_argument("--iterations", type=int, default=300)
    backtest.add_argument("--learning-rate", type=float, default=0.03)
    backtest.add_argument("--regularization", type=float, default=0.02)
    backtest.add_argument(
        "--unknown-team-policy", choices=("error", "league_average"), default="error"
    )
    backtest.add_argument("--tail-tolerance", type=float, default=1e-8)
    backtest.add_argument("--hard-max-corners", type=int, default=80)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fit":
            model = fit_model(
                args.input,
                half_life_days=args.half_life_days,
                iterations=args.iterations,
                learning_rate=args.learning_rate,
                regularization=args.regularization,
                generated_at=args.generated_at,
            )
            save_json(model, args.output)
            return 0
        if args.command == "predict":
            totals = [
                _parse_market(value, "corner total", {"over", "under"})
                for value in args.total
            ]
            handicaps = [
                _parse_market(value, "corner handicap", {"home", "away"})
                for value in args.handicap
            ]
            prediction = predict_model(
                load_model(args.model),
                args.home_team,
                args.away_team,
                kickoff=args.kickoff,
                generated_at=args.generated_at,
                unknown_team_policy=args.unknown_team_policy,
                tail_tolerance=args.tail_tolerance,
                hard_max_corners=args.hard_max_corners,
                total_markets=totals,
                corner_handicaps=handicaps,
            )
            save_json(prediction, args.output)
            return 0
        backtest = backtest_model(
            args.input,
            min_train_matches=args.min_train_matches,
            test_block_size=args.test_block_size,
            half_life_days=args.half_life_days,
            iterations=args.iterations,
            learning_rate=args.learning_rate,
            regularization=args.regularization,
            unknown_team_policy=args.unknown_team_policy,
            tail_tolerance=args.tail_tolerance,
            hard_max_corners=args.hard_max_corners,
        )
        save_json(backtest, args.output)
        return 0
    except CornerModelError as exc:
        parser.exit(2, f"corner_model: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build auditable league-scoped corner training CSVs from Titan evidence.

Only complete regulation-time corner results are admitted.  Missing,
conflicting and extra-time-ambiguous rows remain counted in the manifest but
can never enter model fitting.  The generated CSVs are the exact inputs bound
by :mod:`corner_model_manager`.
"""

from __future__ import annotations

import argparse
import copy
import csv
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence


ARTIFACT_TYPE = "soccer_corner_history_dataset_bundle"
SCHEMA_VERSION = "2.0.0"
BUILDER_VERSION = "corner-history-dataset-builder/2.0.0"
SOURCE_SCHEMA_VERSION = "1.0.0"
SOURCE_COLLECTOR_VERSION = "titan-corner-history/1.0.0"
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SOURCE_COPY_FILENAME = "corner_history.source.json"

COMPETITIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "brazil-serie-a": ("brazil_serie_a", "巴甲", ("Brazil Serie A",)),
    "norway-eliteserien": ("norway_eliteserien", "挪超", ("Eliteserien",)),
    "japan-j1": ("japan_j1", "日职", ("J1 League",)),
    "usa-mls": ("usa_mls", "美职联", ("MLS", "Major League Soccer")),
    "england-premier-league": (
        "england_premier_league",
        "英超",
        ("Premier League", "EPL"),
    ),
    "france-ligue-1": ("france_ligue_1", "法甲", ("Ligue 1",)),
    "spain-la-liga": ("spain_la_liga", "西甲", ("La Liga",)),
    "germany-bundesliga": (
        "germany_bundesliga",
        "德甲",
        ("Bundesliga",),
    ),
    "italy-serie-a": ("italy_serie_a", "意甲", ("Serie A",)),
    "south-korea-k-league-1": (
        "korea_k_league_1",
        "韩K联",
        ("K League 1", "K1 League"),
    ),
    "sweden-allsvenskan": (
        "sweden_allsvenskan",
        "瑞典超",
        ("瑞超", "Allsvenskan"),
    ),
    "finland-veikkausliiga": (
        "finland_veikkausliiga",
        "芬超",
        ("Veikkausliiga", "Finland Veikkausliiga"),
    ),
    "uefa-champions-league": (
        "uefa_champions_league",
        "欧冠",
        ("UEFA Champions League", "UCL"),
    ),
    "afc-champions-league": (
        "afc_champions_league",
        "亚冠",
        ("AFC Champions League", "ACL"),
    ),
}

CSV_FIELDS = (
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

ELIGIBLE_PHASES_BY_COMPETITION: dict[str, tuple[str, ...]] = {
    "brazil-serie-a": ("regular_season",),
    "norway-eliteserien": ("regular_season",),
    "japan-j1": ("regular_season",),
    "usa-mls": ("regular_season",),
    "england-premier-league": ("regular_season",),
    "france-ligue-1": ("regular_season",),
    "spain-la-liga": ("regular_season",),
    "germany-bundesliga": ("regular_season",),
    "italy-serie-a": ("regular_season",),
    "south-korea-k-league-1": (
        "regular_season",
        "championship_split",
        "relegation_split",
    ),
    "sweden-allsvenskan": ("regular_season",),
    "finland-veikkausliiga": (
        "regular_season",
        "championship_split",
        "relegation_split",
    ),
    "uefa-champions-league": ("group_stage", "league_phase"),
    "afc-champions-league": (
        "group_stage",
        "league_phase",
        "group_or_league_stage",
    ),
}

ELIGIBLE_REGIMES_BY_COMPETITION: dict[str, tuple[str, ...]] = {
    "brazil-serie-a": ("regular",),
    "norway-eliteserien": ("regular",),
    "japan-j1": ("regular",),
    "usa-mls": ("regular",),
    "england-premier-league": ("regular",),
    "france-ligue-1": ("18-team", "20-team"),
    "spain-la-liga": ("regular",),
    "germany-bundesliga": ("regular",),
    "italy-serie-a": ("regular",),
    "south-korea-k-league-1": ("33-plus-split", "covid-27-round"),
    "sweden-allsvenskan": ("regular",),
    "finland-veikkausliiga": ("regular",),
    "uefa-champions-league": ("32-team-groups", "36-team-league-phase"),
    "afc-champions-league": (
        "calendar-year-acl",
        "cross-year-acl",
        "24-team-acl-elite",
    ),
}

SELECTION_POLICY = {
    "version": "regulation-corner-training-selection/2.0.0",
    "required_corner_data_status": "complete",
    "required_corner_period": "regulation_90",
    "eligible_regimes_by_competition": {
        key: list(values)
        for key, values in sorted(ELIGIBLE_REGIMES_BY_COMPETITION.items())
    },
    "regime_policy_scope": "competition_specific_versioned_allowlist",
    "eligible_phases_by_competition": {
        key: list(values)
        for key, values in sorted(ELIGIBLE_PHASES_BY_COMPETITION.items())
    },
    "special_season_hard_exclusions": ["japan-j1:2026"],
    "phase_cohort_policy": (
        "exclude_entire phase cohorts that can include extra time; never select "
        "individual rows by observed result"
    ),
    "extra_time_ambiguous_excluded": True,
    "conflicting_excluded": True,
    "missing_excluded": True,
    "post_as_of_finished_rows_excluded": True,
    "half_corner_missing_value_policy": "preserve_null_never_zero_fill",
}

SCHEDULE_IDENTITY_FIELDS = (
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


class CornerDatasetError(ValueError):
    """Raised when source evidence is not safe for training."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise CornerDatasetError("artifact contains non-canonical values") from error


def _canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def calculate_source_bundle_hash(source: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(source))
    payload.pop("bundle_hash", None)
    return _canonical_hash(payload)


def calculate_manifest_hash(manifest: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(manifest))
    payload.pop("bundle_hash", None)
    return _canonical_hash(payload)


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    _atomic_bytes(path, payload.encode("utf-8"))


def _atomic_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=CSV_FIELDS,
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return _file_hash(path)


def _as_of(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise CornerDatasetError("as_of_date must use YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise CornerDatasetError("as_of_date must use canonical YYYY-MM-DD")
    return parsed


def _nonnegative_count(value: Any, field: str, match_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 99:
        raise CornerDatasetError(
            f"match {match_id}: {field} must be an integer between 0 and 99"
        )
    return value


def _aware_datetime(value: Any, field: str, match_id: str) -> datetime:
    text = str(value or "").strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise CornerDatasetError(f"match {match_id}: {field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CornerDatasetError(f"match {match_id}: {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _source_kickoff(raw: Mapping[str, Any], match_id: str) -> tuple[datetime, int]:
    kickoff = _aware_datetime(raw.get("kickoff_utc"), "kickoff_utc", match_id)
    epoch = raw.get("kickoff_epoch")
    if isinstance(epoch, bool):
        raise CornerDatasetError(f"match {match_id}: kickoff_epoch is invalid")
    try:
        parsed_epoch = int(epoch)
    except (TypeError, ValueError) as error:
        raise CornerDatasetError(f"match {match_id}: kickoff_epoch is invalid") from error
    if parsed_epoch <= 0 or parsed_epoch != int(kickoff.timestamp()):
        raise CornerDatasetError(
            f"match {match_id}: kickoff_epoch does not match kickoff_utc"
        )
    if str(raw.get("source_timezone") or "") != "Asia/Shanghai":
        raise CornerDatasetError(
            f"match {match_id}: source_timezone must be Asia/Shanghai"
        )
    return kickoff, parsed_epoch


def _season_year(raw: Mapping[str, Any]) -> int | None:
    value = raw.get("season_start_year")
    if value not in {None, ""}:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    match = re.search(r"\b(20\d{2})\b", str(raw.get("season_label") or ""))
    return int(match.group(1)) if match else None


def _normalized_regime(raw: Mapping[str, Any]) -> str:
    regime = str(raw.get("competition_regime") or "").strip().casefold()
    return "regular" if regime == "standard" else regime


PHASE_ALIASES = {
    "regular": "regular_season",
    "regular season": "regular_season",
    "regular_season": "regular_season",
    "常规赛": "regular_season",
    "championship split": "championship_split",
    "championship_split": "championship_split",
    "争冠组": "championship_split",
    "relegation split": "relegation_split",
    "relegation_split": "relegation_split",
    "保级组": "relegation_split",
    "group stage": "group_stage",
    "group_stage": "group_stage",
    "小组赛": "group_stage",
    "league phase": "league_phase",
    "league_phase": "league_phase",
    "联赛阶段": "league_phase",
    "group_or_league_stage": "group_or_league_stage",
}


def _normalized_phase(raw: Mapping[str, Any]) -> str:
    phase = str(raw.get("phase") or "").strip().casefold()
    explicit = PHASE_ALIASES.get(
        phase, phase.replace("-", "_").replace(" ", "_")
    )
    competition = str(raw.get("competition_key") or "").strip()
    round_text = str(raw.get("round") or "").strip()
    phase_and_round = f"{phase} {round_text}".strip()
    folded_round = phase_and_round.casefold()

    if competition == "south-korea-k-league-1":
        if "争冠组" in phase_and_round:
            return "championship_split"
        if "保级组" in phase_and_round:
            return "relegation_split"
    elif competition == "finland-veikkausliiga":
        if any(token in folded_round for token in ("争冠", "冠军组", "mestaruus")):
            return "championship_split"
        if any(
            token in folded_round
            for token in ("保级", "降级组", "挑战组", "karsinta", "haastaja")
        ):
            return "relegation_split"
        if any(
            token in folded_round
            for token in ("欧战", "欧会", "欧协", "欧罗巴", "eurolopputurnaus", "final")
        ):
            return "european_playoff"
    elif competition == "italy-serie-a" and "降级附加赛" in phase_and_round:
        return "relegation_playoff"
    elif competition == "usa-mls" and any(
        token in folded_round for token in ("季后", "附加赛", "playoff")
    ):
        return "playoffs"
    elif competition == "uefa-champions-league" and phase_and_round:
        if any(
            token in phase_and_round
            for token in ("预选", "第一圈", "第二圈", "第三圈", "附加赛")
        ):
            return "qualifying"
        if any(
            token in phase_and_round
            for token in ("淘汰", "十六强", "半准决赛", "准决赛", "决赛")
        ):
            return "knockout"
        if explicit == "group_stage" or "分组赛" in phase_and_round:
            return "group_stage"
        if explicit == "league_phase" or "联赛阶段" in phase_and_round:
            return "league_phase"
        return "knockout"
    elif competition == "afc-champions-league" and phase_and_round:
        if any(token in phase_and_round for token in ("资格", "预选", "附加赛")):
            return "qualifying"
        if any(
            token in phase_and_round
            for token in ("淘汰", "十六强", "半准决赛", "准决赛", "决赛")
        ):
            return "knockout"
        if (
            explicit in {"group_stage", "league_phase", "group_or_league_stage"}
            or "分组" in phase_and_round
        ):
            return "group_or_league_stage"
        return "knockout"
    return explicit


def calculate_fixture_fingerprint(raw: Mapping[str, Any]) -> str:
    payload = {"match_id": str(raw.get("match_id") or "")}
    payload.update({field: raw.get(field) for field in SCHEDULE_IDENTITY_FIELDS})
    return _canonical_hash(payload)


def _canonical_utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def load_source(path: str | Path) -> dict[str, Any]:
    source_path = Path(path).resolve()
    try:
        source = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CornerDatasetError(f"cannot read corner history: {source_path}") from error
    if not isinstance(source, dict):
        raise CornerDatasetError("corner history must contain a JSON object")
    if source.get("schema_version") != SOURCE_SCHEMA_VERSION:
        raise CornerDatasetError("corner history schema_version is unsupported")
    if source.get("collector_version") != SOURCE_COLLECTOR_VERSION:
        raise CornerDatasetError("corner history collector_version is unsupported")
    if source.get("bundle_hash") != calculate_source_bundle_hash(source):
        raise CornerDatasetError("corner history bundle_hash does not match contents")
    if not isinstance(source.get("matches"), list):
        raise CornerDatasetError("corner history matches must be a list")
    return source


def _selected_competitions(
    *,
    league_keys: Sequence[str] | None,
    competition_keys: Sequence[str] | None,
) -> tuple[str, ...]:
    """Resolve an optional, deterministic subset of source competitions.

    The two key spaces are intentionally mutually exclusive so a caller can
    never accidentally request the union or intersection of two disagreeing
    filters.  ``None`` preserves the historical all-league build exactly.
    """

    if league_keys is not None and competition_keys is not None:
        raise CornerDatasetError(
            "league_keys and competition_keys are mutually exclusive"
        )
    if league_keys is None and competition_keys is None:
        return tuple(COMPETITIONS)

    raw_values = league_keys if league_keys is not None else competition_keys
    filter_name = "league_keys" if league_keys is not None else "competition_keys"
    if isinstance(raw_values, (str, bytes)) or raw_values is None:
        raise CornerDatasetError(f"{filter_name} must be a non-empty sequence")
    values = list(raw_values)
    if not values:
        raise CornerDatasetError(f"{filter_name} must be a non-empty sequence")
    if any(
        not isinstance(value, str) or not value or value.strip() != value
        for value in values
    ):
        raise CornerDatasetError(f"{filter_name} contains an invalid key")
    if len(set(values)) != len(values):
        raise CornerDatasetError(f"{filter_name} contains a duplicate key")

    if competition_keys is not None:
        unknown = sorted(set(values) - set(COMPETITIONS))
        if unknown:
            raise CornerDatasetError(
                "unsupported competition_keys: " + ", ".join(unknown)
            )
        selected = set(values)
    else:
        competition_by_league = {
            league_key: competition_key
            for competition_key, (league_key, _league, _aliases) in COMPETITIONS.items()
        }
        unknown = sorted(set(values) - set(competition_by_league))
        if unknown:
            raise CornerDatasetError(
                "unsupported league_keys: " + ", ".join(unknown)
            )
        selected = {competition_by_league[value] for value in values}
    return tuple(key for key in COMPETITIONS if key in selected)


def build_dataset(
    source_path: str | Path,
    output_dir: str | Path,
    *,
    as_of_date: str,
    league_keys: Sequence[str] | None = None,
    competition_keys: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate one collector bundle and write full or selected league CSVs.

    Filtering is intended for a targeted deterministic replay of a previously
    full-audited league.  The source bundle hash and copied source remain the
    complete collector artifact, while row-level validation and CSV output are
    limited to the selected competition set.  Omitting both filters retains
    the complete fourteen-league validation/build behavior.
    """

    audit_date = _as_of(as_of_date)
    selected_competitions = _selected_competitions(
        league_keys=league_keys,
        competition_keys=competition_keys,
    )
    selected_set = set(selected_competitions)
    source_file = Path(source_path).resolve()
    source = load_source(source_file)
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    by_league: dict[str, list[dict[str, Any]]] = {
        key: [] for key in selected_competitions
    }
    qa: dict[str, dict[str, Any]] = {
        key: {
            "source_rows": 0,
            "training_rows": 0,
            "status_counts": {},
            "excluded_reasons": {},
            "excluded_cohorts": {},
        }
        for key in selected_competitions
    }
    seen_match_ids: dict[str, tuple[Any, ...]] = {}
    seen_fixtures: set[tuple[str, str, str, str]] = set()

    for index, raw in enumerate(source["matches"]):
        if not isinstance(raw, dict):
            raise CornerDatasetError(f"matches[{index}] must be an object")
        match_id = str(raw.get("match_id") or "").strip()
        if not match_id.isdigit() or int(match_id) <= 0:
            raise CornerDatasetError(f"matches[{index}] has invalid match_id")
        competition_key = str(raw.get("competition_key") or "").strip()
        if competition_key not in COMPETITIONS:
            raise CornerDatasetError(
                f"match {match_id}: unsupported competition_key {competition_key!r}"
            )
        if competition_key not in selected_set:
            continue
        home = str(raw.get("home_team") or "").strip()
        away = str(raw.get("away_team") or "").strip()
        if not home or not away or home == away:
            raise CornerDatasetError(f"match {match_id}: invalid team identity")
        kickoff, kickoff_epoch = _source_kickoff(raw, match_id)
        kickoff_date = kickoff.date()
        identity = (competition_key, str(kickoff_epoch), home, away)
        prior_identity = seen_match_ids.get(match_id)
        if prior_identity is not None:
            status = "duplicate" if prior_identity == identity else "conflicting"
            raise CornerDatasetError(f"match {match_id}: {status} match_id")
        seen_match_ids[match_id] = identity
        if identity in seen_fixtures:
            raise CornerDatasetError(f"match {match_id}: duplicate dated fixture")
        seen_fixtures.add(identity)

        status = str(raw.get("corner_data_status") or "").strip()
        period = str(raw.get("corner_period") or "").strip()
        block = qa[competition_key]
        block["source_rows"] += 1
        statuses = block["status_counts"]
        statuses[status] = statuses.get(status, 0) + 1
        regime = _normalized_regime(raw)
        phase = _normalized_phase(raw)

        exclusion: str | None = None
        excluded_cohort: str | None = None
        if kickoff_date > audit_date:
            exclusion = "post_as_of_date"
        elif competition_key == "japan-j1" and _season_year(raw) == 2026:
            exclusion = "competition_regime_not_training_eligible"
            excluded_cohort = "regime:japan-j1:2026"
        elif regime not in ELIGIBLE_REGIMES_BY_COMPETITION[competition_key]:
            exclusion = "competition_regime_not_training_eligible"
            excluded_cohort = f"regime:{regime or 'missing'}"
        elif phase not in ELIGIBLE_PHASES_BY_COMPETITION[competition_key]:
            exclusion = "phase_not_training_eligible"
            excluded_cohort = f"phase:{phase or 'missing'}"
        elif status != SELECTION_POLICY["required_corner_data_status"]:
            exclusion = status or "missing_status"
        elif period != SELECTION_POLICY["required_corner_period"]:
            exclusion = "non_regulation_corner_period"
        if exclusion is not None:
            reasons = block["excluded_reasons"]
            reasons[exclusion] = reasons.get(exclusion, 0) + 1
            if excluded_cohort is not None:
                cohorts = block["excluded_cohorts"]
                cohorts[excluded_cohort] = cohorts.get(excluded_cohort, 0) + 1
            continue

        reasons = raw.get("corner_exclusion_reasons")
        if reasons not in ([], None):
            raise CornerDatasetError(
                f"match {match_id}: complete row has corner_exclusion_reasons"
            )
        source_hash = raw.get("source_response_sha256")
        if not isinstance(source_hash, str) or not HASH_RE.fullmatch(source_hash):
            raise CornerDatasetError(f"match {match_id}: invalid source_response_sha256")
        fixture_fingerprint = raw.get("schedule_fixture_sha256")
        if not isinstance(fixture_fingerprint, str) or not HASH_RE.fullmatch(
            fixture_fingerprint
        ):
            raise CornerDatasetError(f"match {match_id}: invalid schedule_fixture_sha256")
        if fixture_fingerprint != calculate_fixture_fingerprint(raw):
            raise CornerDatasetError(
                f"match {match_id}: schedule_fixture_sha256 does not match fixture"
            )
        source_url = str(raw.get("source_url") or "").strip()
        if not source_url.startswith("https://"):
            raise CornerDatasetError(f"match {match_id}: source_url must use HTTPS")
        source_collected = _aware_datetime(
            raw.get("source_collected_at"), "source_collected_at", match_id
        )
        if source_collected < kickoff:
            raise CornerDatasetError(
                f"match {match_id}: source_collected_at cannot precede kickoff_utc"
            )
        home_corners = _nonnegative_count(raw.get("home_corners"), "home_corners", match_id)
        away_corners = _nonnegative_count(raw.get("away_corners"), "away_corners", match_id)
        total_corners = _nonnegative_count(raw.get("total_corners"), "total_corners", match_id)
        if total_corners != home_corners + away_corners:
            raise CornerDatasetError(f"match {match_id}: total_corners does not reconcile")

        half_home = raw.get("half_home_corners")
        half_away = raw.get("half_away_corners")
        half_total = raw.get("half_total_corners")
        if any(value is not None for value in (half_home, half_away, half_total)):
            if any(value is None for value in (half_home, half_away, half_total)):
                raise CornerDatasetError(f"match {match_id}: partial half-corner tuple")
            parsed_half_home = _nonnegative_count(
                half_home, "half_home_corners", match_id
            )
            parsed_half_away = _nonnegative_count(
                half_away, "half_away_corners", match_id
            )
            parsed_half_total = _nonnegative_count(
                half_total, "half_total_corners", match_id
            )
            if (
                parsed_half_total != parsed_half_home + parsed_half_away
                or parsed_half_home > home_corners
                or parsed_half_away > away_corners
            ):
                raise CornerDatasetError(f"match {match_id}: half corners do not reconcile")

        row = {
            "date": kickoff_date.isoformat(),
            "kickoff_utc": _canonical_utc(kickoff),
            "kickoff_epoch": kickoff_epoch,
            "league_key": COMPETITIONS[competition_key][0],
            "home_team": home,
            "away_team": away,
            "home_corners": home_corners,
            "away_corners": away_corners,
            "match_id": match_id,
            "season": str(raw.get("season_label") or "").strip(),
            "phase": phase,
            "competition_regime": regime,
            "fixture_fingerprint": fixture_fingerprint,
            "source_url": source_url,
            "source_collected_at": _canonical_utc(source_collected),
            "source_response_sha256": source_hash,
        }
        by_league[competition_key].append(row)
        block["training_rows"] += 1

    leagues: list[dict[str, Any]] = []
    for source_key in selected_competitions:
        league_key, league, aliases = COMPETITIONS[source_key]
        rows = sorted(
            by_league[source_key],
            key=lambda row: (int(row["kickoff_epoch"]), int(row["match_id"])),
        )
        if len(rows) < 2:
            raise CornerDatasetError(
                f"{source_key} has fewer than two complete regulation-time rows"
            )
        filename = f"{league_key}-corners.csv"
        csv_path = destination / filename
        csv_hash = _atomic_csv(csv_path, rows)
        block = qa[source_key]
        block["status_counts"] = dict(sorted(block["status_counts"].items()))
        block["excluded_reasons"] = dict(sorted(block["excluded_reasons"].items()))
        block["excluded_cohorts"] = dict(sorted(block["excluded_cohorts"].items()))
        block["training_coverage"] = round(
            block["training_rows"] / block["source_rows"], 8
        )
        regime_counts = dict(
            sorted(Counter(row["competition_regime"] for row in rows).items())
        )
        phase_counts = dict(sorted(Counter(row["phase"] for row in rows).items()))
        leagues.append(
            {
                "source_competition_key": source_key,
                "league_key": league_key,
                "league": league,
                "aliases": [league_key, league, *aliases],
                "dataset_file": filename,
                "dataset_sha256": csv_hash,
                "rows": len(rows),
                "date_start": rows[0]["date"],
                "date_end": rows[-1]["date"],
                "kickoff_utc_start": rows[0]["kickoff_utc"],
                "kickoff_utc_end": rows[-1]["kickoff_utc"],
                "fixture_set_hash": _canonical_hash(
                    [
                        {
                            "match_id": row["match_id"],
                            "fixture_fingerprint": row["fixture_fingerprint"],
                        }
                        for row in rows
                    ]
                ),
                "response_set_hash": _canonical_hash(
                    [
                        {
                            "match_id": row["match_id"],
                            "source_response_sha256": row["source_response_sha256"],
                        }
                        for row in rows
                    ]
                ),
                "regimes": regime_counts,
                "phases": phase_counts,
                "selection_policy_version": SELECTION_POLICY["version"],
                "allowed_competition_regimes": list(
                    ELIGIBLE_REGIMES_BY_COMPETITION[source_key]
                ),
                "allowed_phases": list(ELIGIBLE_PHASES_BY_COMPETITION[source_key]),
                "qa": block,
            }
        )

    source_copy = destination / SOURCE_COPY_FILENAME
    try:
        source_payload = source_file.read_bytes()
    except OSError as error:
        raise CornerDatasetError(f"cannot reread corner history: {source_file}") from error
    _atomic_bytes(source_copy, source_payload)
    # Re-open the copied evidence before binding it into the manifest.  This
    # prevents a manifest from pointing at a missing or semantically invalid
    # source file even when the original input was valid.
    copied_source = load_source(source_copy)
    if copied_source.get("bundle_hash") != source.get("bundle_hash"):
        raise CornerDatasetError("copied corner history does not match input bundle")

    manifest: dict[str, Any] = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "as_of_date": audit_date.isoformat(),
        "source_file": SOURCE_COPY_FILENAME,
        "source_file_sha256": _file_hash(source_copy),
        "source_bundle_hash": source["bundle_hash"],
        "selection_policy": copy.deepcopy(SELECTION_POLICY),
        "leagues": sorted(leagues, key=lambda item: item["league_key"]),
    }
    manifest["bundle_hash"] = calculate_manifest_hash(manifest)
    _atomic_json(destination / "manifest.json", manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build auditable per-league corner training datasets"
    )
    parser.add_argument("--input", required=True, help="corner_history.json")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--as-of-date", required=True, help="YYYY-MM-DD")
    filters = parser.add_mutually_exclusive_group()
    filters.add_argument(
        "--league-key",
        action="append",
        dest="league_keys",
        help="build only this output league key; repeat for multiple leagues",
    )
    filters.add_argument(
        "--competition-key",
        action="append",
        dest="competition_keys",
        help="build only this source competition key; repeat for multiple leagues",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_dataset(
            args.input,
            args.output_dir,
            as_of_date=args.as_of_date,
            league_keys=args.league_keys,
            competition_keys=args.competition_keys,
        )
    except CornerDatasetError as error:
        raise SystemExit(f"error: {error}") from error
    print(
        json.dumps(
            {
                "event": "corner-dataset-built",
                "leagues": len(manifest["leagues"]),
                "rows": sum(item["rows"] for item in manifest["leagues"]),
                "bundle_hash": manifest["bundle_hash"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

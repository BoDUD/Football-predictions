from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from scripts import corner_history_dataset_builder as builder


TEAMS = ("A", "B", "C", "D")
SCHEDULES = (
    (("A", "B"), ("C", "D")),
    (("A", "C"), ("D", "B")),
    (("A", "D"), ("B", "C")),
)


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(
    *,
    match_id: int,
    competition_key: str,
    kickoff: datetime,
    home: str,
    away: str,
    home_corners: int,
    away_corners: int,
) -> dict:
    regime = builder.ELIGIBLE_REGIMES_BY_COMPETITION[competition_key][0]
    phase = builder.ELIGIBLE_PHASES_BY_COMPETITION[competition_key][0]
    local = kickoff.astimezone(timezone(timedelta(hours=8)))
    value = {
        "schema_version": builder.SOURCE_SCHEMA_VERSION,
        "collector_version": builder.SOURCE_COLLECTOR_VERSION,
        "match_id": str(match_id),
        "competition_key": competition_key,
        "competition_name": competition_key,
        "competition_id": str(100 + list(builder.COMPETITIONS).index(competition_key)),
        "season_label": "2023",
        "season_start_year": 2023,
        "competition_regime": regime,
        "phase": phase,
        "round": phase,
        "kickoff": local.strftime("%Y-%m-%d %H:%M"),
        "kickoff_utc": kickoff.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "kickoff_epoch": int(kickoff.timestamp()),
        "source_timezone": "Asia/Shanghai",
        "home_team_id": str(match_id * 2),
        "away_team_id": str(match_id * 2 + 1),
        "home_team": home,
        "away_team": away,
        "home_goals": 1,
        "away_goals": 0,
        "home_corners": home_corners,
        "away_corners": away_corners,
        "total_corners": home_corners + away_corners,
        "half_home_corners": None,
        "half_away_corners": None,
        "half_total_corners": None,
        "corner_period": "regulation_90",
        "corner_data_status": "complete",
        "corner_exclusion_reasons": [],
        "corner_odds": [],
        "source_url": f"https://example.test/corner/{match_id}",
        "source_collected_at": (kickoff + timedelta(hours=3)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "source_response_sha256": _hash(f"response:{match_id}"),
    }
    value["schedule_fixture_sha256"] = builder.calculate_fixture_fingerprint(value)
    return value


def build_source_bound_dataset(
    base: Path,
    *,
    target_league_key: str = "korea_k_league_1",
    days: int = 16,
    start: date = date(2023, 1, 1),
    result_offset: int = 0,
    strong_signal: bool = False,
) -> tuple[Path, Path]:
    source_key = next(
        key
        for key, (league_key, _league, _aliases) in builder.COMPETITIONS.items()
        if league_key == target_league_key
    )
    matches: list[dict] = []
    match_id = 1000000
    for competition_key in builder.COMPETITIONS:
        if competition_key == source_key:
            for day in range(days):
                match_date = start + timedelta(days=day)
                for fixture_index, (home, away) in enumerate(SCHEDULES[day % 3]):
                    match_id += 1
                    kickoff = datetime(
                        match_date.year,
                        match_date.month,
                        match_date.day,
                        10 + fixture_index,
                        tzinfo=timezone.utc,
                    )
                    matches.append(
                        _record(
                            match_id=match_id,
                            competition_key=competition_key,
                            kickoff=kickoff,
                            home=home,
                            away=away,
                            home_corners=(
                                2 + 2 * TEAMS.index(home) + day % 2
                                if strong_signal
                                else 3
                                + (2 * TEAMS.index(home) + day + result_offset) % 7
                            ),
                            away_corners=(
                                1 + TEAMS.index(away) + day % 2
                                if strong_signal
                                else 2
                                + (TEAMS.index(away) + 2 * day + result_offset) % 6
                            ),
                        )
                    )
        else:
            for day in range(2):
                match_id += 1
                match_date = start + timedelta(days=day)
                kickoff = datetime(
                    match_date.year,
                    match_date.month,
                    match_date.day,
                    8,
                    tzinfo=timezone.utc,
                )
                prefix = str(list(builder.COMPETITIONS).index(competition_key))
                matches.append(
                    _record(
                        match_id=match_id,
                        competition_key=competition_key,
                        kickoff=kickoff,
                        home=f"H{prefix}",
                        away=f"A{prefix}",
                        home_corners=4 + day,
                        away_corners=3 + day,
                    )
                )
    source = {
        "schema_version": builder.SOURCE_SCHEMA_VERSION,
        "collector_version": builder.SOURCE_COLLECTOR_VERSION,
        "generated_at": "2024-01-01T00:00:00Z",
        "source": "https://example.test",
        "qa": {"matches": len(matches)},
        "matches": sorted(
            matches, key=lambda row: (int(row["kickoff_epoch"]), int(row["match_id"]))
        ),
    }
    source["bundle_hash"] = builder.calculate_source_bundle_hash(source)
    source_path = base / "corner_history.json"
    source_path.write_text(
        json.dumps(source, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    output = base / "dataset"
    manifest = builder.build_dataset(
        source_path,
        output,
        as_of_date="2024-12-31",
    )
    entry = next(
        item for item in manifest["leagues"] if item["league_key"] == target_league_key
    )
    return output / entry["dataset_file"], output / "manifest.json"

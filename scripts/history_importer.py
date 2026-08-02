#!/usr/bin/env python3
"""Import the supported historical league workbooks without data leakage.

The source workbooks are static XLSX files with two header rows.  This module
uses only the Python standard library, validates the complete workbook schema,
and emits two deliberately separate datasets:

* ``*-scores.csv`` contains only fields that are safe for score-model fitting.
* ``*-opening-markets.csv`` contains untimestamped opening prices for research
  baselines.  It is never consumed by :mod:`score_model` automatically.

Closing prices, rankings, half/full-time labels, and result labels are checked
for source integrity but are quarantined from training outputs.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timedelta, timezone, tzinfo
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Iterable, Mapping, Sequence
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DATASET_ARTIFACT_TYPE = "soccer_history_dataset_bundle"
DATASET_SCHEMA_VERSION = "1.0.0"
IMPORTER_VERSION = "league-workbook-importer/1.1.0"

REGULAR_COMPETITION_REGIME = "regular"
JAPAN_J1_VISION_REGIME = "2026_vision_regional"
# The official opening may be presented as 7 February in other timezones, but
# the supplied source has three finished fixtures dated 6 February in its
# explicitly declared source timezone.  These bounds deliberately preserve the
# complete, reproducible 180-match season=2026 batch in that source.
JAPAN_J1_VISION_SOURCE_DATE_START = date(2026, 2, 6)
JAPAN_J1_VISION_SOURCE_DATE_END = date(2026, 5, 24)

LEAGUE_SPECS = {
    "巴甲": {"league_key": "brazil_serie_a", "filename": "brazil-serie-a"},
    "挪超": {"league_key": "norway_eliteserien", "filename": "norway-eliteserien"},
    "日职": {"league_key": "japan_j1", "filename": "japan-j1"},
    "美职联": {"league_key": "usa_mls", "filename": "usa-mls"},
}

CORE_HEADERS = [
    "编号",
    "年份",
    "赛事",
    "轮次",
    "比赛时间",
    "状态",
    "排名",
    "主队名称",
    "客队名称",
    "排名",
    "半场比分",
    "全场比分",
    "总进球数",
    "半全场",
    "胜平负",
]
BOOKMAKERS = (
    ("36", "36*"),
    ("macau", "澳*"),
    ("william", "威*"),
    ("crown", "皇*"),
)
MARKET_GROUPS = (
    ("open_1x2", "初", ("胜", "平", "负")),
    ("close_1x2", "终", ("胜", "平", "负")),
    ("open_asian", "亚初", ("主水", "盘口", "客水")),
    ("close_asian", "亚终", ("主水", "盘口", "客水")),
    ("open_total", "大小初", ("大水", "盘口", "小水")),
    ("close_total", "大小终", ("大水", "盘口", "小水")),
)
EXPECTED_HEADERS = CORE_HEADERS + [
    field
    for _book_key, _book_label in BOOKMAKERS
    for _market_key, _market_label, fields in MARKET_GROUPS
    for field in fields
]
EXPECTED_TOP_HEADERS = {
    11: "结果",
    **{
        16 + book_index * 18 + market_index * 3: book_label + market_label
        for book_index, (_book_key, book_label) in enumerate(BOOKMAKERS)
        for market_index, (_market_key, market_label, _fields) in enumerate(MARKET_GROUPS)
    },
}

SCORE_RE = re.compile(r"^\s*(\d+)\s*[-:]\s*(\d+)\s*$")
KICKOFF_RE = re.compile(
    r"^\s*(?P<month>\d{1,2})-(?P<day>\d{1,2})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*$"
)
ROUND_RE = re.compile(r"(?:^|\s)第(?P<round>\d+)轮$")
CELL_REF_RE = re.compile(r"^(?P<column>[A-Z]+)(?P<row>\d+)$")

SCORE_FIELDS = (
    "date",
    "home_team",
    "away_team",
    "home_goals",
    "away_goals",
    "half_home_goals",
    "half_away_goals",
    "half_result",
    "full_result",
    "htft_result",
    "league_key",
    "league",
    "season",
    "competition_regime",
    "round",
    "source_row",
    "source_kickoff",
    "source_timezone",
    "kickoff_utc",
)
MARKET_FIELDS = (
    "league_key",
    "league",
    "season",
    "competition_regime",
    "round",
    "source_row",
    "source_kickoff",
    "source_timezone",
    "kickoff_utc",
    "home_team",
    "away_team",
    "bookmaker",
    "home_odds",
    "draw_odds",
    "away_odds",
    "asian_home_price",
    "asian_line",
    "asian_away_price",
    "total_over_price",
    "total_line",
    "total_under_price",
    "opening_1x2_complete",
    "opening_asian_complete",
    "opening_total_complete",
)


class HistoryImportError(ValueError):
    """Raised when a workbook cannot be imported without unsafe assumptions."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value)).strip()


def _integer(value: Any, field: str, row_number: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise HistoryImportError(f"row {row_number}: {field} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        result = int(value)
    elif re.fullmatch(r"[-+]?\d+", _text(value)):
        result = int(_text(value))
    else:
        raise HistoryImportError(f"row {row_number}: {field} must be an integer")
    if result < minimum:
        raise HistoryImportError(
            f"row {row_number}: {field} must be >= {minimum}"
        )
    return result


def _score(value: Any, field: str, row_number: int) -> tuple[int, int]:
    match = SCORE_RE.fullmatch(_text(value))
    if not match:
        raise HistoryImportError(f"row {row_number}: invalid {field}")
    home, away = int(match.group(1)), int(match.group(2))
    if home > 99 or away > 99:
        raise HistoryImportError(f"row {row_number}: implausible {field}")
    return home, away


def _result_label(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "胜"
    if home_goals == away_goals:
        return "平"
    return "负"


def _result_code(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "H"
    if home_goals == away_goals:
        return "D"
    return "A"


def _half_full_label(
    half_home: int, half_away: int, full_home: int, full_away: int
) -> str:
    return f"{_result_label(half_home, half_away)}-{_result_label(full_home, full_away)}"


def _column_index(reference: str) -> int:
    match = CELL_REF_RE.fullmatch(reference)
    if not match:
        raise HistoryImportError(f"invalid XLSX cell reference: {reference}")
    result = 0
    for character in match.group("column"):
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _xml_text(element: ET.Element) -> str:
    return "".join(node.text or "" for node in element.iter() if node.tag.endswith("}t"))


def _parse_numeric(raw: str) -> int | float | str:
    try:
        number = float(raw)
    except ValueError:
        return raw
    if not math.isfinite(number):
        return raw
    if number.is_integer():
        return int(number)
    return number


def _resolve_sheet_member(target: str) -> str:
    normalized = target.replace("\\", "/")
    if normalized.startswith("/"):
        return normalized.lstrip("/")
    return str(PurePosixPath("xl") / normalized)


def read_xlsx_rows(path: str | Path) -> tuple[str, list[list[Any]]]:
    """Return the single worksheet as dense rows and reject every formula."""

    source = Path(path)
    try:
        archive = ZipFile(source)
    except (OSError, BadZipFile) as exc:
        raise HistoryImportError(f"cannot read XLSX workbook: {source}") from exc

    with archive:
        try:
            workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships_root = ET.fromstring(
                archive.read("xl/_rels/workbook.xml.rels")
            )
        except (KeyError, ET.ParseError) as exc:
            raise HistoryImportError(f"invalid XLSX workbook structure: {source}") from exc

        sheets = [node for node in workbook_root.iter() if node.tag.endswith("}sheet")]
        if len(sheets) != 1:
            raise HistoryImportError(
                f"workbook must contain exactly one worksheet; found {len(sheets)}"
            )
        sheet = sheets[0]
        sheet_name = _text(sheet.attrib.get("name"))
        relationship_id = next(
            (value for key, value in sheet.attrib.items() if key.endswith("}id")), None
        )
        relationship_targets = {
            node.attrib.get("Id"): node.attrib.get("Target")
            for node in relationships_root
            if node.attrib.get("Id") and node.attrib.get("Target")
        }
        if relationship_id not in relationship_targets:
            raise HistoryImportError("worksheet relationship is missing")
        sheet_member = _resolve_sheet_member(relationship_targets[relationship_id])

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            except ET.ParseError as exc:
                raise HistoryImportError("invalid shared strings XML") from exc
            shared_strings = [
                _xml_text(node)
                for node in shared_root
                if node.tag.endswith("}si")
            ]

        try:
            worksheet_root = ET.fromstring(archive.read(sheet_member))
        except (KeyError, ET.ParseError) as exc:
            raise HistoryImportError("cannot read the worksheet XML") from exc

        dense_rows: dict[int, dict[int, Any]] = {}
        formula_cells: list[str] = []
        for cell in (node for node in worksheet_root.iter() if node.tag.endswith("}c")):
            reference = cell.attrib.get("r", "")
            match = CELL_REF_RE.fullmatch(reference)
            if not match:
                raise HistoryImportError(f"invalid XLSX cell reference: {reference}")
            row_index = int(match.group("row"))
            column_index = _column_index(reference)
            if any(child.tag.endswith("}f") for child in cell):
                formula_cells.append(reference)
            cell_type = cell.attrib.get("t")
            value_node = next(
                (child for child in cell if child.tag.endswith("}v")), None
            )
            if cell_type == "inlineStr":
                value = _xml_text(cell)
            elif value_node is None or value_node.text is None:
                value = None
            elif cell_type == "s":
                try:
                    value = shared_strings[int(value_node.text)]
                except (ValueError, IndexError) as exc:
                    raise HistoryImportError(
                        f"invalid shared string index at {reference}"
                    ) from exc
            elif cell_type == "b":
                value = value_node.text == "1"
            elif cell_type in {"str", "e"}:
                value = value_node.text
            else:
                value = _parse_numeric(value_node.text)
            dense_rows.setdefault(row_index, {})[column_index] = value

        if formula_cells:
            preview = ", ".join(formula_cells[:5])
            raise HistoryImportError(
                f"workbook formulas are not allowed ({len(formula_cells)} cells; {preview})"
            )
        if not dense_rows:
            raise HistoryImportError("worksheet is empty")
        maximum_row = max(dense_rows)
        maximum_column = max(max(columns) for columns in dense_rows.values())
        rows = [
            [dense_rows.get(row, {}).get(column) for column in range(1, maximum_column + 1)]
            for row in range(1, maximum_row + 1)
        ]
        return sheet_name, rows


def _validate_schema(sheet_name: str, rows: Sequence[Sequence[Any]]) -> None:
    if sheet_name not in LEAGUE_SPECS:
        supported = ", ".join(sorted(LEAGUE_SPECS))
        raise HistoryImportError(
            f"unsupported worksheet {sheet_name!r}; expected one of {supported}"
        )
    if len(rows) < 3:
        raise HistoryImportError("workbook needs two headers and at least one data row")
    width = max(len(row) for row in rows)
    if width != len(EXPECTED_HEADERS):
        raise HistoryImportError(
            f"workbook must have {len(EXPECTED_HEADERS)} columns; found {width}"
        )
    actual_headers = [_text(value) for value in rows[1]]
    if actual_headers != EXPECTED_HEADERS:
        mismatch = next(
            index
            for index, (actual, expected) in enumerate(
                zip(actual_headers, EXPECTED_HEADERS), start=1
            )
            if actual != expected
        )
        raise HistoryImportError(
            f"header mismatch in column {mismatch}: expected "
            f"{EXPECTED_HEADERS[mismatch - 1]!r}, found {actual_headers[mismatch - 1]!r}"
        )
    actual_top = {
        index: _text(value) for index, value in enumerate(rows[0], start=1) if _text(value)
    }
    if actual_top != EXPECTED_TOP_HEADERS:
        raise HistoryImportError("merged market header schema does not match the supported format")


def _parse_source_kickoff(
    season: int,
    raw: Any,
    row_number: int,
    calendar_year: int,
    source_zone: tzinfo,
) -> tuple[datetime, int, int]:
    match = KICKOFF_RE.fullmatch(_text(raw))
    if not match:
        raise HistoryImportError(f"row {row_number}: invalid 比赛时间")
    month = int(match.group("month"))
    day = int(match.group("day"))
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if not 1 <= month <= 12:
        raise HistoryImportError(f"row {row_number}: invalid kickoff month")
    try:
        kickoff = datetime(calendar_year, month, day, hour, minute, tzinfo=source_zone)
    except ValueError as exc:
        raise HistoryImportError(f"row {row_number}: invalid kickoff date") from exc
    return kickoff, month, season


def _load_timezone(name: str) -> tzinfo:
    """Load an IANA zone, with a dependency-free Shanghai fallback.

    Windows Python installations often omit the optional IANA tzdata package.
    China has used UTC+08 continuously throughout this dataset's 2020+ range,
    so the explicit fallback is exact for the supported source workbooks.
    """

    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name)
        if name in {"UTC", "Etc/UTC"}:
            return timezone.utc
        raise HistoryImportError(f"unknown source timezone: {name}") from exc


def _market_value(row: Sequence[Any], book_index: int, market_index: int) -> tuple[Any, Any, Any]:
    start = 15 + book_index * 18 + market_index * 3
    return row[start], row[start + 1], row[start + 2]


def _complete(values: Iterable[Any]) -> bool:
    return all(_text(value) not in {"", "-"} for value in values)


def _market_cell(value: Any) -> Any:
    """Normalize the workbook's explicit missing-market sentinel to blank."""

    return "" if _text(value) == "-" else value


def _round_number(raw: Any) -> str:
    value = _text(raw)
    match = ROUND_RE.search(value)
    return match.group("round") if match else value


def _competition_regime(
    league_key: str, season: int, source_date: date
) -> str:
    """Return the explicitly versioned competition format for one fixture."""

    if (
        league_key == "japan_j1"
        and season == 2026
        and JAPAN_J1_VISION_SOURCE_DATE_START
        <= source_date
        <= JAPAN_J1_VISION_SOURCE_DATE_END
    ):
        return JAPAN_J1_VISION_REGIME
    return REGULAR_COMPETITION_REGIME


def import_workbook(
    path: str | Path, *, source_timezone: str
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate and normalize one supported workbook."""

    source = Path(path).resolve()
    source_zone = _load_timezone(source_timezone)
    sheet_name, rows = read_xlsx_rows(source)
    _validate_schema(sheet_name, rows)
    spec = LEAGUE_SPECS[sheet_name]

    score_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []
    season_counts: Counter[int] = Counter()
    regime_counts: Counter[tuple[int, str]] = Counter()
    season_state: dict[int, dict[str, Any]] = {}
    rollover_events: list[dict[str, Any]] = []
    fixture_keys: dict[tuple[str, str, str], int] = {}
    bookmaker_completeness = {
        book_key: {"opening_1x2": 0, "opening_asian": 0, "opening_total": 0}
        for book_key, _book_label in BOOKMAKERS
    }

    expected_identifier = 1
    for row_number, source_row in enumerate(rows[2:], start=3):
        row = list(source_row) + [None] * (len(EXPECTED_HEADERS) - len(source_row))
        if not any(_text(value) for value in row):
            if any(
                any(_text(value) for value in later)
                for later in rows[row_number:]
            ):
                raise HistoryImportError(f"row {row_number}: blank row inside the data region")
            continue

        identifier = _integer(row[0], "编号", row_number, minimum=1)
        if identifier != expected_identifier:
            raise HistoryImportError(
                f"row {row_number}: 编号 must be continuous; expected {expected_identifier}"
            )
        expected_identifier += 1
        season = _integer(row[1], "年份", row_number, minimum=1900)
        if season > datetime.now(timezone.utc).year + 1:
            raise HistoryImportError(f"row {row_number}: 年份 is implausibly far in the future")
        if _text(row[2]) != sheet_name:
            raise HistoryImportError(
                f"row {row_number}: 赛事 {_text(row[2])!r} does not match sheet {sheet_name!r}"
            )
        if _text(row[5]) != "完":
            raise HistoryImportError(
                f"row {row_number}: only finished rows (状态=完) may be imported"
            )

        kickoff_match = KICKOFF_RE.fullmatch(_text(row[4]))
        if not kickoff_match:
            raise HistoryImportError(f"row {row_number}: invalid 比赛时间")
        month = int(kickoff_match.group("month"))
        state = season_state.setdefault(
            season, {"calendar_year": season, "previous_month": None, "previous": None}
        )
        previous_month = state["previous_month"]
        if previous_month is not None and previous_month >= 10 and month <= 3:
            if state["calendar_year"] != season:
                raise HistoryImportError(
                    f"row {row_number}: more than one calendar rollover in season {season}"
                )
            state["calendar_year"] += 1
            rollover_events.append(
                {
                    "season": season,
                    "source_row": row_number,
                    "from_calendar_year": season,
                    "to_calendar_year": season + 1,
                    "trigger": f"{previous_month:02d}->{month:02d}",
                }
            )
        kickoff, month, _ = _parse_source_kickoff(
            season,
            row[4],
            row_number,
            state["calendar_year"],
            source_zone,
        )
        if state["previous"] is not None and kickoff < state["previous"]:
            raise HistoryImportError(
                f"row {row_number}: kickoff order goes backwards inside season {season}; "
                "calendar-year inference is unsafe"
            )
        state["previous_month"] = month
        state["previous"] = kickoff

        home_team, away_team = _text(row[7]), _text(row[8])
        if not home_team or not away_team or home_team == away_team:
            raise HistoryImportError(f"row {row_number}: invalid team names")
        half_home, half_away = _score(row[10], "半场比分", row_number)
        home_goals, away_goals = _score(row[11], "全场比分", row_number)
        if half_home > home_goals or half_away > away_goals:
            raise HistoryImportError(
                f"row {row_number}: 半场比分 cannot exceed 全场比分"
            )
        total_goals = _integer(row[12], "总进球数", row_number)
        if total_goals != home_goals + away_goals:
            raise HistoryImportError(f"row {row_number}: 总进球数 does not match 全场比分")
        if _text(row[13]) != _half_full_label(
            half_home, half_away, home_goals, away_goals
        ):
            raise HistoryImportError(f"row {row_number}: 半全场 does not match scores")
        if _text(row[14]) != _result_label(home_goals, away_goals):
            raise HistoryImportError(f"row {row_number}: 胜平负 does not match 全场比分")

        kickoff_utc = kickoff.astimezone(timezone.utc)
        source_kickoff = kickoff.isoformat(timespec="minutes")
        kickoff_utc_text = kickoff_utc.isoformat(timespec="minutes").replace("+00:00", "Z")
        fixture_key = (kickoff_utc_text, home_team, away_team)
        if fixture_key in fixture_keys:
            raise HistoryImportError(
                f"row {row_number}: duplicate fixture also present at row {fixture_keys[fixture_key]}"
            )
        fixture_keys[fixture_key] = row_number
        round_value = _round_number(row[3])
        competition_regime = _competition_regime(
            spec["league_key"], season, kickoff.date()
        )
        common = {
            "league_key": spec["league_key"],
            "league": sheet_name,
            "season": season,
            "competition_regime": competition_regime,
            "round": round_value,
            "source_row": row_number,
            "source_kickoff": source_kickoff,
            "source_timezone": source_timezone,
            "kickoff_utc": kickoff_utc_text,
            "home_team": home_team,
            "away_team": away_team,
        }
        score_rows.append(
            {
                "date": kickoff_utc.date().isoformat(),
                "home_team": home_team,
                "away_team": away_team,
                "home_goals": home_goals,
                "away_goals": away_goals,
                "half_home_goals": half_home,
                "half_away_goals": half_away,
                "half_result": _result_code(half_home, half_away),
                "full_result": _result_code(home_goals, away_goals),
                "htft_result": _result_code(half_home, half_away)
                + _result_code(home_goals, away_goals),
                "league_key": spec["league_key"],
                "league": sheet_name,
                "season": season,
                "competition_regime": competition_regime,
                "round": round_value,
                "source_row": row_number,
                "source_kickoff": source_kickoff,
                "source_timezone": source_timezone,
                "kickoff_utc": kickoff_utc_text,
            }
        )
        season_counts[season] += 1
        regime_counts[(season, competition_regime)] += 1

        for book_index, (book_key, _book_label) in enumerate(BOOKMAKERS):
            opening_1x2 = _market_value(row, book_index, 0)
            opening_asian = _market_value(row, book_index, 2)
            opening_total = _market_value(row, book_index, 4)
            one_x_two_complete = _complete(opening_1x2)
            asian_complete = _complete(opening_asian)
            total_complete = _complete(opening_total)
            bookmaker_completeness[book_key]["opening_1x2"] += int(one_x_two_complete)
            bookmaker_completeness[book_key]["opening_asian"] += int(asian_complete)
            bookmaker_completeness[book_key]["opening_total"] += int(total_complete)
            market_rows.append(
                {
                    **common,
                    "bookmaker": book_key,
                    "home_odds": _market_cell(opening_1x2[0]),
                    "draw_odds": _market_cell(opening_1x2[1]),
                    "away_odds": _market_cell(opening_1x2[2]),
                    "asian_home_price": _market_cell(opening_asian[0]),
                    "asian_line": _market_cell(opening_asian[1]),
                    "asian_away_price": _market_cell(opening_asian[2]),
                    "total_over_price": _market_cell(opening_total[0]),
                    "total_line": _market_cell(opening_total[1]),
                    "total_under_price": _market_cell(opening_total[2]),
                    "opening_1x2_complete": str(one_x_two_complete).lower(),
                    "opening_asian_complete": str(asian_complete).lower(),
                    "opening_total_complete": str(total_complete).lower(),
                }
            )

    if not score_rows:
        raise HistoryImportError("workbook contains no finished matches")
    score_rows.sort(
        key=lambda row: (
            row["kickoff_utc"], row["home_team"], row["away_team"], row["source_row"]
        )
    )
    market_rows.sort(
        key=lambda row: (
            row["kickoff_utc"], row["home_team"], row["away_team"], row["bookmaker"]
        )
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    summary = {
        "league": sheet_name,
        "league_key": spec["league_key"],
        "output_stem": spec["filename"],
        "source_file": source.name,
        "source_sha256": "sha256:" + source_hash,
        "rows": len(score_rows),
        "seasons": {str(year): season_counts[year] for year in sorted(season_counts)},
        "competition_regimes": {
            str(year): {
                regime: regime_counts[(year, regime)]
                for regime in sorted(
                    value
                    for counted_year, value in regime_counts
                    if counted_year == year
                )
            }
            for year in sorted(season_counts)
        },
        "utc_date_start": score_rows[0]["date"],
        "utc_date_end": score_rows[-1]["date"],
        "calendar_rollovers": rollover_events,
        "bookmaker_opening_completeness": {
            bookmaker: {
                market: {"rows": count, "rate": round(count / len(score_rows), 6)}
                for market, count in markets.items()
            }
            for bookmaker, markets in bookmaker_completeness.items()
        },
    }
    return summary, score_rows, market_rows


def _atomic_csv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", delete=False, dir=path.parent, suffix=".tmp"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        temporary = Path(handle.name)
    temporary.replace(path)
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    ) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=path.parent, suffix=".tmp"
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)


def _canonical_manifest_hash(manifest: Mapping[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("bundle_hash", None)
    return "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _bundle_file(destination: Path, raw_name: Any, label: str) -> Path:
    name = _text(raw_name)
    posix_name = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or posix_name.is_absolute()
        or len(posix_name.parts) != 1
        or posix_name.name != name
    ):
        raise HistoryImportError(f"{label} must be a safe bundle filename")
    return destination / name


def _read_bundle_csv(
    path: Path, fields: Sequence[str], label: str
) -> list[dict[str, str]]:
    if not path.is_file():
        raise HistoryImportError(f"{label} does not exist: {path.name}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(fields):
            raise HistoryImportError(f"{label} field schema does not match importer")
        return list(reader)


def _parse_bundle_source_date(row: Mapping[str, str], label: str) -> date:
    try:
        kickoff = datetime.fromisoformat(row["source_kickoff"])
    except (KeyError, TypeError, ValueError) as error:
        raise HistoryImportError(f"{label} has an invalid source_kickoff") from error
    if kickoff.tzinfo is None or kickoff.utcoffset() is None:
        raise HistoryImportError(f"{label} source_kickoff must include a timezone")
    return kickoff.date()


def _flat_regime_counts(
    league_summaries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for summary in sorted(league_summaries, key=lambda item: item["league_key"]):
        by_season = summary["competition_regimes"]
        for raw_season in sorted(by_season, key=int):
            for regime in sorted(by_season[raw_season]):
                result.append(
                    {
                        "league_key": summary["league_key"],
                        "season": int(raw_season),
                        "competition_regime": regime,
                        "rows": by_season[raw_season][regime],
                    }
                )
    return result


def validate_bundle(output_dir: str | Path) -> dict[str, Any]:
    """Semantically validate hashes, schemas and competition-format labels."""

    destination = Path(output_dir).resolve()
    manifest_path = destination / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoryImportError("manifest.json is missing or invalid") from error
    if not isinstance(manifest, dict):
        raise HistoryImportError("manifest.json must contain an object")
    if manifest.get("artifact_type") != DATASET_ARTIFACT_TYPE:
        raise HistoryImportError("manifest artifact_type is invalid")
    if manifest.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise HistoryImportError("manifest schema_version is unsupported")
    if manifest.get("importer_version") != IMPORTER_VERSION:
        raise HistoryImportError("manifest importer_version is unsupported")
    if manifest.get("bundle_hash") != _canonical_manifest_hash(manifest):
        raise HistoryImportError("manifest bundle_hash does not match contents")

    league_summaries = manifest.get("leagues")
    if not isinstance(league_summaries, list) or not league_summaries:
        raise HistoryImportError("manifest leagues must be a non-empty list")
    observed_leagues: set[str] = set()
    validated_summaries: list[Mapping[str, Any]] = []
    for summary_index, summary in enumerate(league_summaries):
        label = f"manifest.leagues[{summary_index}]"
        if not isinstance(summary, dict):
            raise HistoryImportError(f"{label} must be an object")
        league_key = _text(summary.get("league_key"))
        if league_key not in {spec["league_key"] for spec in LEAGUE_SPECS.values()}:
            raise HistoryImportError(f"{label}.league_key is unsupported")
        if league_key in observed_leagues:
            raise HistoryImportError(f"duplicate manifest league: {league_key}")
        observed_leagues.add(league_key)

        score_meta = summary.get("score_dataset")
        market_meta = summary.get("opening_market_research")
        if not isinstance(score_meta, dict) or not isinstance(market_meta, dict):
            raise HistoryImportError(f"{label} dataset metadata is invalid")
        score_path = _bundle_file(
            destination, score_meta.get("file"), f"{label}.score_dataset.file"
        )
        market_path = _bundle_file(
            destination,
            market_meta.get("file"),
            f"{label}.opening_market_research.file",
        )
        if not score_path.is_file():
            raise HistoryImportError(
                f"{label}.score_dataset.file does not exist: {score_path.name}"
            )
        if not market_path.is_file():
            raise HistoryImportError(
                f"{label}.opening_market_research.file does not exist: {market_path.name}"
            )
        score_hash = "sha256:" + hashlib.sha256(score_path.read_bytes()).hexdigest()
        market_hash = "sha256:" + hashlib.sha256(market_path.read_bytes()).hexdigest()
        if score_meta.get("sha256") != score_hash:
            raise HistoryImportError(f"{label} score dataset hash mismatch")
        if market_meta.get("sha256") != market_hash:
            raise HistoryImportError(f"{label} opening market dataset hash mismatch")

        score_rows = _read_bundle_csv(score_path, SCORE_FIELDS, f"{label} scores")
        market_rows = _read_bundle_csv(
            market_path, MARKET_FIELDS, f"{label} opening markets"
        )
        if score_meta.get("rows") != len(score_rows) or summary.get("rows") != len(
            score_rows
        ):
            raise HistoryImportError(f"{label} score row count mismatch")
        if market_meta.get("rows") != len(market_rows):
            raise HistoryImportError(f"{label} opening market row count mismatch")

        season_counts: Counter[int] = Counter()
        regime_counts: Counter[tuple[int, str]] = Counter()
        for row_index, row in enumerate(score_rows, start=2):
            row_label = f"{score_path.name}:{row_index}"
            try:
                season = int(row["season"])
            except (KeyError, TypeError, ValueError) as error:
                raise HistoryImportError(f"{row_label} has an invalid season") from error
            if row.get("league_key") != league_key:
                raise HistoryImportError(f"{row_label} league_key does not match manifest")
            expected_regime = _competition_regime(
                league_key, season, _parse_bundle_source_date(row, row_label)
            )
            if row.get("competition_regime") != expected_regime:
                raise HistoryImportError(
                    f"{row_label} competition_regime must be {expected_regime}"
                )
            season_counts[season] += 1
            regime_counts[(season, expected_regime)] += 1

        expected_seasons = {
            str(season): season_counts[season] for season in sorted(season_counts)
        }
        expected_regimes = {
            str(season): {
                regime: regime_counts[(season, regime)]
                for counted_season, regime in sorted(regime_counts)
                if counted_season == season
            }
            for season in sorted(season_counts)
        }
        if summary.get("seasons") != expected_seasons:
            raise HistoryImportError(f"{label}.seasons does not match score rows")
        if summary.get("competition_regimes") != expected_regimes:
            raise HistoryImportError(
                f"{label}.competition_regimes does not match score rows"
            )

        market_regime_counts: Counter[tuple[int, str]] = Counter()
        for row_index, row in enumerate(market_rows, start=2):
            row_label = f"{market_path.name}:{row_index}"
            try:
                season = int(row["season"])
            except (KeyError, TypeError, ValueError) as error:
                raise HistoryImportError(f"{row_label} has an invalid season") from error
            if row.get("league_key") != league_key:
                raise HistoryImportError(f"{row_label} league_key does not match manifest")
            expected_regime = _competition_regime(
                league_key, season, _parse_bundle_source_date(row, row_label)
            )
            if row.get("competition_regime") != expected_regime:
                raise HistoryImportError(
                    f"{row_label} competition_regime must be {expected_regime}"
                )
            market_regime_counts[(season, expected_regime)] += 1
        expected_market_counts = Counter(
            {
                key: count * len(BOOKMAKERS)
                for key, count in regime_counts.items()
            }
        )
        if market_regime_counts != expected_market_counts:
            raise HistoryImportError(
                f"{label} opening market regime counts do not match score rows"
            )
        validated_summaries.append(summary)

    expected_flat_counts = _flat_regime_counts(validated_summaries)
    if manifest.get("competition_regime_counts") != expected_flat_counts:
        raise HistoryImportError(
            "manifest competition_regime_counts does not match league score rows"
        )
    return manifest


def _expand_inputs(values: Sequence[str | Path]) -> list[Path]:
    discovered: list[Path] = []
    for raw in values:
        path = Path(raw).resolve()
        if path.is_dir():
            discovered.extend(sorted(path.glob("*.xlsx"), key=lambda item: item.name))
        elif path.is_file():
            discovered.append(path)
        else:
            raise HistoryImportError(f"input does not exist: {path}")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in discovered:
        resolved = path.resolve()
        if resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    if not unique:
        raise HistoryImportError("no XLSX input files were found")
    return unique


def import_bundle(
    inputs: Sequence[str | Path],
    output_dir: str | Path,
    *,
    source_timezone: str,
) -> dict[str, Any]:
    """Import one or more workbooks and write a deterministic local bundle."""

    source_paths = _expand_inputs(inputs)
    destination = Path(output_dir).resolve()
    league_summaries: list[dict[str, Any]] = []
    observed_leagues: set[str] = set()
    imported: list[
        tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]
    ] = []
    for source in source_paths:
        summary, score_rows, market_rows = import_workbook(
            source, source_timezone=source_timezone
        )
        if summary["league_key"] in observed_leagues:
            raise HistoryImportError(
                f"duplicate league workbook: {summary['league_key']}"
            )
        observed_leagues.add(summary["league_key"])
        imported.append((summary, score_rows, market_rows))

    # No output is touched until every source workbook has passed validation.
    for summary, score_rows, market_rows in imported:
        score_name = summary["output_stem"] + "-scores.csv"
        market_name = summary["output_stem"] + "-opening-markets.csv"
        score_path = destination / score_name
        market_path = destination / market_name
        summary["score_dataset"] = {
            "file": score_name,
            "sha256": _atomic_csv(score_path, SCORE_FIELDS, score_rows),
            "rows": len(score_rows),
        }
        summary["opening_market_research"] = {
            "file": market_name,
            "sha256": _atomic_csv(market_path, MARKET_FIELDS, market_rows),
            "rows": len(market_rows),
            "policy": "research_only_untimestamped_opening_snapshot",
        }
        league_summaries.append(summary)

    manifest: dict[str, Any] = {
        "artifact_type": DATASET_ARTIFACT_TYPE,
        "schema_version": DATASET_SCHEMA_VERSION,
        "importer_version": IMPORTER_VERSION,
        "source_timezone": source_timezone,
        "kickoff_year_policy": (
            "season year, then one explicit Oct-Dec to Jan-Mar rollover while preserving "
            "source row chronology"
        ),
        "training_feature_whitelist": [
            "date", "home_team", "away_team"
        ],
        "outcome_label_fields": [
            "home_goals",
            "away_goals",
            "half_home_goals",
            "half_away_goals",
            "half_result",
            "full_result",
            "htft_result",
        ],
        "research_only_fields": [
            "opening 1X2", "opening Asian handicap", "opening goal total"
        ],
        "quarantined_fields": [
            "all closing prices",
            "rankings",
            "total-goals label",
            "half-time/full-time result label",
            "win/draw/loss result label",
        ],
        "caveats": [
            "The workbooks do not contain the exact collection timestamp for opening prices.",
            "Opening prices may be used only as a research baseline until pre-kickoff provenance is verified.",
            "Source kickoff timezone is supplied explicitly by the importer operator.",
        ],
        "leagues": sorted(league_summaries, key=lambda item: item["league_key"]),
    }
    manifest["competition_regime_counts"] = _flat_regime_counts(
        manifest["leagues"]
    )
    manifest["bundle_hash"] = _canonical_manifest_hash(manifest)
    _atomic_json(destination / "manifest.json", manifest)
    return validate_bundle(destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and import supported historical league XLSX workbooks"
    )
    parser.add_argument(
        "inputs", nargs="+", help="XLSX file(s) or directories containing XLSX files"
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--source-timezone",
        default="Asia/Shanghai",
        help="IANA timezone used by the workbook kickoff strings",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = import_bundle(
            args.inputs,
            args.output_dir,
            source_timezone=args.source_timezone,
        )
    except HistoryImportError as exc:
        parser.exit(2, f"history_importer: error: {exc}\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

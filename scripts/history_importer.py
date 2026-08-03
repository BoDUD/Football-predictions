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
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DATASET_ARTIFACT_TYPE = "soccer_history_dataset_bundle"
DATASET_SCHEMA_VERSION = "1.2.0"
IMPORTER_VERSION = "league-workbook-importer/1.5.0"

REGULAR_COMPETITION_REGIME = "regular"
JAPAN_J1_VISION_REGIME = "2026_vision_regional"
# The official opening may be presented as 7 February in other timezones, but
# the supplied source has three finished fixtures dated 6 February in its
# explicitly declared source timezone.  These bounds deliberately preserve the
# complete regional and placement tournament in the audited 2026 snapshot.
JAPAN_J1_VISION_SOURCE_DATE_START = date(2026, 2, 6)
JAPAN_J1_VISION_SOURCE_DATE_END = date(2026, 6, 6)
SEASON_COMPLETENESS_POLICY_VERSION = "known-schedule-match-counts-v1"
# Expected finished-match totals for the source competition scope.  Stable
# round-robin totals are repeated deliberately so every supported season has an
# explicit, reviewable expectation.  Variable or exceptional formats (for
# example pandemic MLS, the Serie A relegation playoff, and continental cups)
# use their season-specific official/source-scope schedule totals.
EXPECTED_SEASON_MATCH_COUNTS: dict[str, dict[int, int]] = {
    "afc_champions_league": {
        2020: 113,
        2021: 140,
        2022: 137,
        2023: 162,
        2024: 113,
        2025: 117,
        2026: 117,
    },
    "brazil_serie_a": {season: 380 for season in range(2020, 2027)},
    "england_premier_league": {season: 380 for season in range(2020, 2027)},
    "france_ligue_1": {
        2020: 380,
        2021: 380,
        2022: 380,
        2023: 306,
        2024: 306,
        2025: 306,
        2026: 306,
    },
    # Veikkausliiga Titan scope includes every listed championship/relegation,
    # European-qualification and relegation-final phase, de-duplicated by match.
    "finland_veikkausliiga": {
        2020: 132,
        2021: 164,
        2022: 169,
        2023: 169,
        2024: 169,
        2025: 179,
        2026: 179,
    },
    "germany_bundesliga": {season: 306 for season in range(2020, 2027)},
    "italy_serie_a": {
        2020: 380,
        2021: 380,
        2022: 381,
        2023: 380,
        2024: 380,
        2025: 380,
        2026: 380,
    },
    "japan_j1": {
        2020: 306,
        2021: 380,
        2022: 306,
        2023: 306,
        2024: 380,
        2025: 380,
        2026: 200,
    },
    "korea_k_league_1": {
        2020: 162,
        2021: 228,
        2022: 228,
        2023: 228,
        2024: 228,
        2025: 228,
        2026: 228,
    },
    "norway_eliteserien": {
        2020: 240,
        2021: 240,
        2022: 240,
        2023: 240,
        2024: 240,
        2025: 242,
        2026: 242,
    },
    "spain_la_liga": {season: 380 for season in range(2020, 2027)},
    "sweden_allsvenskan": {season: 240 for season in range(2020, 2027)},
    "uefa_champions_league": {
        2020: 176,
        2021: 218,
        2022: 214,
        2023: 214,
        2024: 279,
        2025: 281,
        2026: 281,
    },
    # MLS includes every Titan-listed tournament/postseason phase, not only the
    # regular-season table.  This keeps the workbook promise of all matches;
    # the model manager can still isolate regular competition regimes.
    "usa_mls": {
        2020: 324,
        2021: 472,
        2022: 489,
        2023: 521,
        2024: 522,
        2025: 540,
        2026: 540,
    },
}
SEASON_COMPLETENESS_POLICY = {
    "version": SEASON_COMPLETENESS_POLICY_VERSION,
    "as_of_field": "as_of_date",
    "complete_rule": "observed_matches == expected_matches",
    "partial_rule": (
        "observed_matches < expected_matches, or no verified expected total is registered"
    ),
    "overflow_rule": "observed_matches > expected_matches is rejected",
    "expectation_scope": "supported competition seasons 2020-2026",
}

LEAGUE_SPECS = {
    "巴甲": {
        "league_key": "brazil_serie_a",
        "filename": "brazil-serie-a",
        "calendar_policy": "brazil_2020_delayed",
    },
    "挪超": {
        "league_key": "norway_eliteserien",
        "filename": "norway-eliteserien",
        "calendar_policy": "calendar_year",
    },
    "日职": {
        "league_key": "japan_j1",
        "filename": "japan-j1",
        "calendar_policy": "calendar_year",
    },
    "美职联": {
        "league_key": "usa_mls",
        "filename": "usa-mls",
        "calendar_policy": "calendar_year",
    },
    "英超": {
        "league_key": "england_premier_league",
        "filename": "england-premier-league",
        "calendar_policy": "autumn_to_spring",
    },
    "法甲": {
        "league_key": "france_ligue_1",
        "filename": "france-ligue-1",
        "calendar_policy": "autumn_to_spring",
    },
    "西甲": {
        "league_key": "spain_la_liga",
        "filename": "spain-la-liga",
        "calendar_policy": "autumn_to_spring",
    },
    "德甲": {
        "league_key": "germany_bundesliga",
        "filename": "germany-bundesliga",
        "calendar_policy": "autumn_to_spring",
    },
    "意甲": {
        "league_key": "italy_serie_a",
        "filename": "italy-serie-a",
        "calendar_policy": "autumn_to_spring",
    },
    "韩K联": {
        "league_key": "korea_k_league_1",
        "filename": "korea-k-league-1",
        "calendar_policy": "calendar_year",
    },
    "瑞典超": {
        "league_key": "sweden_allsvenskan",
        "filename": "sweden-allsvenskan",
        "calendar_policy": "calendar_year",
    },
    "芬超": {
        "league_key": "finland_veikkausliiga",
        "filename": "finland-veikkausliiga",
        "calendar_policy": "calendar_year",
        "aliases": (
            "finland_veikkausliiga",
            "芬超",
            "芬兰超级联赛",
            "Veikkausliiga",
            "Finland Veikkausliiga",
        ),
    },
    "欧冠": {
        "league_key": "uefa_champions_league",
        "filename": "uefa-champions-league",
        "calendar_policy": "autumn_to_spring",
    },
    "亚冠": {
        "league_key": "afc_champions_league",
        "filename": "afc-champions-league",
        "calendar_policy": "afc_transition",
    },
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
CORNER_AUDIT_HEADERS = [
    "Titan比赛ID",
    "全场角球主队",
    "全场角球客队",
    "全场角球总数",
    "全场角球差",
    "半场角球主队",
    "半场角球客队",
    "半场角球总数",
    "角球数据状态",
    "角球结果来源",
    "角球采集时间",
    "加时口径状态",
]
CORNER_AUDIT_TOP_HEADER = "角球审计"
ALLOWED_AUXILIARY_SHEETS = frozenset({"角球盘口", "数据质量"})
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
ROUND_RE = re.compile(r"^第(?P<round>\d+)轮$")
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
    "format_version",
    "phase_group",
    "season_status",
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
    "format_version",
    "phase_group",
    "season_status",
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
    """Return the unique league sheet and reject formulas in every sheet.

    User-facing history workbooks may also contain the registered ``角球盘口``
    and ``数据质量`` audit sheets.  They are never imported as HT/FT features.
    """

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
        primary_sheets = [
            node for node in sheets if _text(node.attrib.get("name")) in LEAGUE_SPECS
        ]
        if len(primary_sheets) != 1:
            raise HistoryImportError(
                "workbook must contain exactly one supported competition worksheet; "
                f"found {len(primary_sheets)}"
            )
        sheet_names = [_text(node.attrib.get("name")) for node in sheets]
        if len(set(sheet_names)) != len(sheet_names):
            raise HistoryImportError("workbook contains duplicate worksheet names")
        unsupported_auxiliary = sorted(
            name
            for name in sheet_names
            if name not in LEAGUE_SPECS and name not in ALLOWED_AUXILIARY_SHEETS
        )
        if unsupported_auxiliary:
            raise HistoryImportError(
                "workbook contains unsupported auxiliary worksheets: "
                + ", ".join(unsupported_auxiliary)
            )
        sheet = primary_sheets[0]
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

        worksheet_roots: dict[str, ET.Element] = {}
        for candidate in sheets:
            candidate_name = _text(candidate.attrib.get("name"))
            candidate_relationship_id = next(
                (
                    value
                    for key, value in candidate.attrib.items()
                    if key.endswith("}id")
                ),
                None,
            )
            if candidate_relationship_id not in relationship_targets:
                raise HistoryImportError(
                    f"worksheet relationship is missing for {candidate_name!r}"
                )
            candidate_member = _resolve_sheet_member(
                relationship_targets[candidate_relationship_id]
            )
            try:
                candidate_root = ET.fromstring(archive.read(candidate_member))
            except (KeyError, ET.ParseError) as exc:
                raise HistoryImportError(
                    f"cannot read worksheet XML for {candidate_name!r}"
                ) from exc
            formula_cells = [
                cell.attrib.get("r", "")
                for cell in candidate_root.iter()
                if cell.tag.endswith("}c")
                and any(child.tag.endswith("}f") for child in cell)
            ]
            if formula_cells:
                preview = ", ".join(formula_cells[:5])
                raise HistoryImportError(
                    f"workbook formulas are not allowed in {candidate_name!r} "
                    f"({len(formula_cells)} cells; {preview})"
                )
            worksheet_roots[candidate_name] = candidate_root
        worksheet_root = worksheet_roots[sheet_name]

        dense_rows: dict[int, dict[int, Any]] = {}
        for cell in (node for node in worksheet_root.iter() if node.tag.endswith("}c")):
            reference = cell.attrib.get("r", "")
            match = CELL_REF_RE.fullmatch(reference)
            if not match:
                raise HistoryImportError(f"invalid XLSX cell reference: {reference}")
            row_index = int(match.group("row"))
            column_index = _column_index(reference)
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
    allowed_widths = {
        len(EXPECTED_HEADERS),
        len(EXPECTED_HEADERS) + len(CORNER_AUDIT_HEADERS),
    }
    if width not in allowed_widths:
        raise HistoryImportError(
            "workbook must have either the 87-column core schema or the "
            f"registered 99-column corner-audit schema; found {width}"
        )
    actual_headers = [_text(value) for value in rows[1][: len(EXPECTED_HEADERS)]]
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
    if width > len(EXPECTED_HEADERS):
        corner_headers = [
            _text(value)
            for value in rows[1][
                len(EXPECTED_HEADERS) : len(EXPECTED_HEADERS)
                + len(CORNER_AUDIT_HEADERS)
            ]
        ]
        if corner_headers != CORNER_AUDIT_HEADERS:
            raise HistoryImportError(
                "registered corner-audit column schema does not match"
            )
        corner_top = [
            _text(value)
            for value in rows[0][
                len(EXPECTED_HEADERS) : len(EXPECTED_HEADERS)
                + len(CORNER_AUDIT_HEADERS)
            ]
        ]
        if corner_top != [CORNER_AUDIT_TOP_HEADER] + [""] * (
            len(CORNER_AUDIT_HEADERS) - 1
        ):
            raise HistoryImportError(
                "merged corner-audit header schema does not match"
            )
    actual_top = {
        index: _text(value)
        for index, value in enumerate(rows[0][: len(EXPECTED_HEADERS)], start=1)
        if _text(value)
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
    # Only collapse a plain ``第N轮`` label.  Prefixes such as ``争冠组`` and
    # ``分组赛`` carry material competition-phase information and must survive
    # the import for cohort-aware evaluation.
    match = ROUND_RE.fullmatch(value)
    return match.group("round") if match else value


def _calendar_rollover_allowed(
    policy: str,
    season: int,
    previous_month: int,
    month: int,
) -> bool:
    """Return whether a source season may explicitly cross a calendar year."""

    if policy == "calendar_year":
        return False
    if policy == "autumn_to_spring":
        return previous_month >= 10 and month <= 3
    if policy == "brazil_2020_delayed":
        return season == 2020 and previous_month == 12 and month == 1
    if policy == "afc_transition":
        return (
            (previous_month >= 10 and month <= 3)
            or (season == 2022 and previous_month == 8 and month == 2)
        )
    raise HistoryImportError(f"unsupported calendar policy: {policy}")


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


def _format_version(league_key: str, season: int) -> str:
    """Preserve material competition-format changes without fragmenting teams."""

    if league_key == "france_ligue_1":
        return "ligue1_20_team" if season <= 2022 else "ligue1_18_team"
    if league_key == "korea_k_league_1":
        return "k1_2020_shortened" if season == 2020 else "k1_12_team_split"
    if league_key == "finland_veikkausliiga":
        if season == 2020:
            return "veikkausliiga_2020_regular_only"
        if season <= 2024:
            return "veikkausliiga_12_team_single_championship_split"
        return "veikkausliiga_12_team_double_championship_split"
    if league_key == "uefa_champions_league":
        return "ucl_32_team_group" if season <= 2023 else "ucl_36_team_league_phase"
    if league_key == "afc_champions_league":
        if season <= 2021:
            return "afc_legacy_calendar"
        if season == 2022:
            return "afc_2022_transition"
        if season == 2023:
            return "afc_40_team_cross_year"
        return "afc_elite_24_team_league_phase"
    if league_key == "japan_j1" and season == 2026:
        return "j1_2026_vision_regional"
    return "standard_league_format"


def _phase_group(league_key: str, raw_round: Any) -> str:
    """Map source round labels to a stable, audit-only phase family."""

    value = _text(raw_round)
    if league_key == "korea_k_league_1":
        if value.startswith("争冠组"):
            return "championship_split"
        if value.startswith("保级组"):
            return "relegation_split"
        return "regular_season"
    if league_key == "finland_veikkausliiga":
        normalized = value.casefold()
        if any(token in normalized for token in ("争冠", "冠军组", "mestaruus")):
            return "championship_split"
        if any(
            token in normalized
            for token in ("保级", "降级组", "挑战组", "karsinta", "haastaja")
        ):
            return "relegation_split"
        if any(
            token in normalized
            for token in ("欧战", "欧会", "欧协", "欧罗巴", "eurolopputurnaus")
        ):
            return "european_playoff"
        return "regular_season"
    if league_key == "italy_serie_a" and "降级附加赛" in value:
        return "relegation_playoff"
    if league_key == "uefa_champions_league":
        if any(
            token in value
            for token in ("预选", "第一圈", "第二圈", "第三圈", "附加赛")
        ):
            return "qualifying"
        if "分组赛" in value:
            return "group_stage"
        if "联赛阶段" in value:
            return "league_phase"
        return "knockout"
    if league_key == "afc_champions_league":
        if any(token in value for token in ("资格", "预选", "附加赛")):
            return "qualifying"
        if "分组" in value:
            return "group_or_league_stage"
        return "knockout"
    return "regular_season"


def _parse_as_of_date(value: str | date | None, label: str = "as_of_date") -> date:
    """Return one explicit canonical audit date."""

    if isinstance(value, datetime):
        raise HistoryImportError(f"{label} must be a calendar date, not a datetime")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise HistoryImportError(f"{label} is required and must use YYYY-MM-DD")
    normalized = value.strip()
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError as error:
        raise HistoryImportError(f"{label} must use YYYY-MM-DD") from error
    if parsed.isoformat() != normalized:
        raise HistoryImportError(f"{label} must use canonical YYYY-MM-DD")
    return parsed


def _season_completeness(
    league_key: str,
    season: int,
    observed_matches: int,
    as_of_date: str | date,
) -> dict[str, Any]:
    """Build an auditable season-completeness decision from schedule totals."""

    audit_date = _parse_as_of_date(as_of_date)
    if isinstance(observed_matches, bool) or not isinstance(observed_matches, int):
        raise HistoryImportError("observed_matches must be an integer")
    if observed_matches < 1:
        raise HistoryImportError("observed_matches must be positive")
    expected = EXPECTED_SEASON_MATCH_COUNTS.get(league_key, {}).get(season)
    expectation_id = (
        f"{SEASON_COMPLETENESS_POLICY_VERSION}:{league_key}:{season}"
        if expected is not None
        else None
    )
    if expected is not None and observed_matches > expected:
        raise HistoryImportError(
            f"{league_key} season {season} has {observed_matches} rows, exceeding "
            f"the audited schedule expectation of {expected}"
        )
    complete = expected is not None and observed_matches == expected
    return {
        "as_of_date": audit_date.isoformat(),
        "observed_matches": observed_matches,
        "expected_matches": expected,
        "remaining_matches": None if expected is None else expected - observed_matches,
        "expectation_id": expectation_id,
        "expectation_basis": (
            "versioned source-scope competition schedule total"
            if expected is not None
            else "no verified schedule total registered"
        ),
        "expectation_verified": expected is not None,
        "status": "complete" if complete else f"partial_as_of_{audit_date.isoformat()}",
    }


def _season_status(
    league_key: str,
    season: int,
    observed_matches: int,
    as_of_date: str | date,
) -> str:
    """Return a status only when row count and audit date are explicit."""

    return _season_completeness(
        league_key, season, observed_matches, as_of_date
    )["status"]


def import_workbook(
    path: str | Path, *, source_timezone: str, as_of_date: str | date
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate and normalize one supported workbook."""

    source = Path(path).resolve()
    audit_date = _parse_as_of_date(as_of_date)
    source_zone = _load_timezone(source_timezone)
    sheet_name, rows = read_xlsx_rows(source)
    _validate_schema(sheet_name, rows)
    spec = LEAGUE_SPECS[sheet_name]

    score_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []
    season_counts: Counter[int] = Counter()
    regime_counts: Counter[tuple[int, str]] = Counter()
    format_counts: Counter[tuple[int, str]] = Counter()
    phase_counts: Counter[tuple[int, str]] = Counter()
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
        # Calendar-year leagues must never turn an out-of-order row into a new
        # year.  Cross-year competitions use an explicit per-competition
        # policy, including the exceptional delayed 2022 AFC season.
        if (
            previous_month is not None
            and _calendar_rollover_allowed(
                spec["calendar_policy"], season, previous_month, month
            )
        ):
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
        if kickoff.date() > audit_date:
            raise HistoryImportError(
                f"row {row_number}: finished fixture date {kickoff.date().isoformat()} "
                f"is after as_of_date {audit_date.isoformat()}"
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
        format_version = _format_version(spec["league_key"], season)
        phase_group = _phase_group(spec["league_key"], row[3])
        common = {
            "league_key": spec["league_key"],
            "league": sheet_name,
            "season": season,
            "competition_regime": competition_regime,
            "format_version": format_version,
            "phase_group": phase_group,
            "season_status": "",
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
                "format_version": format_version,
                "phase_group": phase_group,
                "season_status": "",
                "round": round_value,
                "source_row": row_number,
                "source_kickoff": source_kickoff,
                "source_timezone": source_timezone,
                "kickoff_utc": kickoff_utc_text,
            }
        )
        season_counts[season] += 1
        regime_counts[(season, competition_regime)] += 1
        format_counts[(season, format_version)] += 1
        phase_counts[(season, phase_group)] += 1

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
    season_completeness = {
        str(season): _season_completeness(
            spec["league_key"], season, season_counts[season], audit_date
        )
        for season in sorted(season_counts)
    }
    status_counts: Counter[tuple[int, str]] = Counter()
    for row in score_rows:
        status = season_completeness[str(row["season"])]["status"]
        row["season_status"] = status
        status_counts[(row["season"], status)] += 1
    for row in market_rows:
        row["season_status"] = season_completeness[str(row["season"])]["status"]
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
        "aliases": list(
            spec.get("aliases", (spec["league_key"], sheet_name))
        ),
        "output_stem": spec["filename"],
        "source_file": source.name,
        "source_sha256": "sha256:" + source_hash,
        "rows": len(score_rows),
        "seasons": {str(year): season_counts[year] for year in sorted(season_counts)},
        "season_completeness": season_completeness,
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
        "format_versions": {
            str(year): {
                value: format_counts[(year, value)]
                for value in sorted(
                    label for counted_year, label in format_counts if counted_year == year
                )
            }
            for year in sorted(season_counts)
        },
        "phase_groups": {
            str(year): {
                value: phase_counts[(year, value)]
                for value in sorted(
                    label for counted_year, label in phase_counts if counted_year == year
                )
            }
            for year in sorted(season_counts)
        },
        "season_statuses": {
            str(year): {
                value: status_counts[(year, value)]
                for value in sorted(
                    label for counted_year, label in status_counts if counted_year == year
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


def _bundle_integer(raw: Any, label: str, *, minimum: int = 0) -> int:
    value = _text(raw)
    if not re.fullmatch(r"\d+", value):
        raise HistoryImportError(f"{label} must be a non-negative integer")
    parsed = int(value)
    if parsed < minimum:
        raise HistoryImportError(f"{label} must be >= {minimum}")
    return parsed


def _bundle_date(raw: Any, label: str) -> date:
    value = _text(raw)
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise HistoryImportError(f"{label} must use YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise HistoryImportError(f"{label} must use canonical YYYY-MM-DD")
    return parsed


def _bundle_datetime(raw: Any, label: str) -> datetime:
    value = _text(raw)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise HistoryImportError(f"{label} must be an ISO-8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoryImportError(f"{label} must include a timezone")
    return parsed


def _canonical_utc_minute(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="minutes").replace(
        "+00:00", "Z"
    )


def validate_bundle(output_dir: str | Path) -> dict[str, Any]:
    """Completely validate hashes, row semantics, fixture identity and context."""

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
    audit_date = _parse_as_of_date(manifest.get("as_of_date"), "manifest.as_of_date")
    if manifest.get("season_completeness_policy") != SEASON_COMPLETENESS_POLICY:
        raise HistoryImportError("manifest season_completeness_policy is invalid")
    source_timezone = _text(manifest.get("source_timezone"))
    if not source_timezone:
        raise HistoryImportError("manifest source_timezone is required")
    source_zone = _load_timezone(source_timezone)

    league_summaries = manifest.get("leagues")
    if not isinstance(league_summaries, list) or not league_summaries:
        raise HistoryImportError("manifest leagues must be a non-empty list")
    specifications = {spec["league_key"]: (name, spec) for name, spec in LEAGUE_SPECS.items()}
    expected_bookmakers = {book_key for book_key, _book_label in BOOKMAKERS}
    observed_leagues: set[str] = set()
    validated_summaries: list[Mapping[str, Any]] = []
    for summary_index, summary in enumerate(league_summaries):
        label = f"manifest.leagues[{summary_index}]"
        if not isinstance(summary, dict):
            raise HistoryImportError(f"{label} must be an object")
        league_key = _text(summary.get("league_key"))
        if league_key not in specifications:
            raise HistoryImportError(f"{label}.league_key is unsupported")
        if league_key in observed_leagues:
            raise HistoryImportError(f"duplicate manifest league: {league_key}")
        observed_leagues.add(league_key)
        expected_league, specification = specifications[league_key]
        if summary.get("league") != expected_league:
            raise HistoryImportError(f"{label}.league does not match league_key")
        if summary.get("output_stem") != specification["filename"]:
            raise HistoryImportError(f"{label}.output_stem does not match league_key")
        expected_aliases = list(
            specification.get("aliases", (league_key, expected_league))
        )
        if summary.get("aliases") != expected_aliases:
            raise HistoryImportError(f"{label}.aliases do not match league_key")

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
        for row_index, row in enumerate(score_rows, start=2):
            season_counts[
                _bundle_integer(
                    row.get("season"), f"{score_path.name}:{row_index} season", minimum=1900
                )
            ] += 1
        expected_completeness = {
            str(season): _season_completeness(
                league_key, season, season_counts[season], audit_date
            )
            for season in sorted(season_counts)
        }

        regime_counts: Counter[tuple[int, str]] = Counter()
        format_counts: Counter[tuple[int, str]] = Counter()
        phase_counts: Counter[tuple[int, str]] = Counter()
        status_counts: Counter[tuple[int, str]] = Counter()
        fixture_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        dated_fixtures: set[tuple[str, str, str]] = set()
        score_dates: list[str] = []
        identity_fields = (
            "league_key",
            "league",
            "season",
            "competition_regime",
            "format_version",
            "phase_group",
            "season_status",
            "round",
            "source_row",
            "source_kickoff",
            "source_timezone",
            "kickoff_utc",
            "home_team",
            "away_team",
        )
        for row_index, row in enumerate(score_rows, start=2):
            row_label = f"{score_path.name}:{row_index}"
            season = _bundle_integer(row.get("season"), f"{row_label} season", minimum=1900)
            if row.get("league_key") != league_key:
                raise HistoryImportError(f"{row_label} league_key does not match manifest")
            if row.get("league") != expected_league:
                raise HistoryImportError(f"{row_label} league does not match manifest")
            home_team = _text(row.get("home_team"))
            away_team = _text(row.get("away_team"))
            if not home_team or not away_team or home_team == away_team:
                raise HistoryImportError(f"{row_label} has invalid team names")

            match_date = _bundle_date(row.get("date"), f"{row_label} date")
            kickoff_utc_raw = _text(row.get("kickoff_utc"))
            kickoff_utc = _bundle_datetime(kickoff_utc_raw, f"{row_label} kickoff_utc")
            if kickoff_utc_raw != _canonical_utc_minute(kickoff_utc):
                raise HistoryImportError(f"{row_label} kickoff_utc is not canonical UTC")
            if kickoff_utc.astimezone(timezone.utc).date() != match_date:
                raise HistoryImportError(f"{row_label} date and kickoff_utc disagree")
            source_kickoff_raw = _text(row.get("source_kickoff"))
            source_kickoff = _bundle_datetime(
                source_kickoff_raw, f"{row_label} source_kickoff"
            )
            if source_kickoff.isoformat(timespec="minutes") != source_kickoff_raw:
                raise HistoryImportError(f"{row_label} source_kickoff is not canonical")
            if row.get("source_timezone") != source_timezone:
                raise HistoryImportError(f"{row_label} source_timezone does not match manifest")
            if source_kickoff_raw != kickoff_utc.astimezone(source_zone).isoformat(
                timespec="minutes"
            ):
                raise HistoryImportError(
                    f"{row_label} source_kickoff and kickoff_utc disagree"
                )
            if source_kickoff.date() > audit_date:
                raise HistoryImportError(
                    f"{row_label} finished fixture is after manifest.as_of_date"
                )

            goals = {
                field: _bundle_integer(row.get(field), f"{row_label} {field}")
                for field in (
                    "home_goals",
                    "away_goals",
                    "half_home_goals",
                    "half_away_goals",
                )
            }
            if (
                goals["half_home_goals"] > goals["home_goals"]
                or goals["half_away_goals"] > goals["away_goals"]
            ):
                raise HistoryImportError(
                    f"{row_label} half-time goals cannot exceed full-time goals"
                )
            expected_half_result = _result_code(
                goals["half_home_goals"], goals["half_away_goals"]
            )
            expected_full_result = _result_code(
                goals["home_goals"], goals["away_goals"]
            )
            if row.get("half_result") != expected_half_result:
                raise HistoryImportError(f"{row_label} half_result disagrees with scores")
            if row.get("full_result") != expected_full_result:
                raise HistoryImportError(f"{row_label} full_result disagrees with scores")
            if row.get("htft_result") != expected_half_result + expected_full_result:
                raise HistoryImportError(f"{row_label} htft_result disagrees with scores")

            source_date = source_kickoff.date()
            expected_regime = _competition_regime(league_key, season, source_date)
            expected_format = _format_version(league_key, season)
            expected_phase = _phase_group(league_key, row.get("round"))
            expected_status = expected_completeness[str(season)]["status"]
            for field, expected in (
                ("competition_regime", expected_regime),
                ("format_version", expected_format),
                ("phase_group", expected_phase),
                ("season_status", expected_status),
            ):
                if row.get(field) != expected:
                    raise HistoryImportError(f"{row_label} {field} must be {expected}")
            source_row = _bundle_integer(
                row.get("source_row"), f"{row_label} source_row", minimum=3
            )
            fixture_key = (kickoff_utc_raw, home_team, away_team)
            dated_key = (match_date.isoformat(), home_team, away_team)
            if fixture_key in fixture_rows or dated_key in dated_fixtures:
                raise HistoryImportError(f"{row_label} duplicate fixture")
            dated_fixtures.add(dated_key)
            identity = {
                "league_key": league_key,
                "league": expected_league,
                "season": season,
                "competition_regime": expected_regime,
                "format_version": expected_format,
                "phase_group": expected_phase,
                "season_status": expected_status,
                "round": row.get("round"),
                "source_row": source_row,
                "source_kickoff": source_kickoff_raw,
                "source_timezone": source_timezone,
                "kickoff_utc": kickoff_utc_raw,
                "home_team": home_team,
                "away_team": away_team,
            }
            fixture_rows[fixture_key] = identity
            score_dates.append(match_date.isoformat())
            regime_counts[(season, expected_regime)] += 1
            format_counts[(season, expected_format)] += 1
            phase_counts[(season, expected_phase)] += 1
            status_counts[(season, expected_status)] += 1

        expected_seasons = {
            str(season): season_counts[season] for season in sorted(season_counts)
        }
        expected_contexts: tuple[tuple[str, Counter[tuple[int, str]]], ...] = (
            ("competition_regimes", regime_counts),
            ("format_versions", format_counts),
            ("phase_groups", phase_counts),
            ("season_statuses", status_counts),
        )
        if summary.get("seasons") != expected_seasons:
            raise HistoryImportError(f"{label}.seasons does not match score rows")
        if summary.get("season_completeness") != expected_completeness:
            raise HistoryImportError(
                f"{label}.season_completeness does not match score rows and as_of_date"
            )
        for field, counts in expected_contexts:
            expected = {
                str(season): {
                    value: counts[(season, value)]
                    for counted_season, value in sorted(counts)
                    if counted_season == season
                }
                for season in sorted(season_counts)
            }
            if summary.get(field) != expected:
                raise HistoryImportError(f"{label}.{field} does not match score rows")
        if summary.get("utc_date_start") != min(score_dates):
            raise HistoryImportError(f"{label}.utc_date_start does not match score rows")
        if summary.get("utc_date_end") != max(score_dates):
            raise HistoryImportError(f"{label}.utc_date_end does not match score rows")

        market_regime_counts: Counter[tuple[int, str]] = Counter()
        market_format_counts: Counter[tuple[int, str]] = Counter()
        market_phase_counts: Counter[tuple[int, str]] = Counter()
        market_status_counts: Counter[tuple[int, str]] = Counter()
        market_books: dict[tuple[str, str, str], set[str]] = {}
        bookmaker_completeness = {
            bookmaker: {"opening_1x2": 0, "opening_asian": 0, "opening_total": 0}
            for bookmaker in sorted(expected_bookmakers)
        }
        completeness_fields = {
            "opening_1x2": ("home_odds", "draw_odds", "away_odds"),
            "opening_asian": (
                "asian_home_price",
                "asian_line",
                "asian_away_price",
            ),
            "opening_total": (
                "total_over_price",
                "total_line",
                "total_under_price",
            ),
        }
        for row_index, row in enumerate(market_rows, start=2):
            row_label = f"{market_path.name}:{row_index}"
            season = _bundle_integer(row.get("season"), f"{row_label} season", minimum=1900)
            if row.get("league_key") != league_key or row.get("league") != expected_league:
                raise HistoryImportError(f"{row_label} league identity does not match manifest")
            home_team = _text(row.get("home_team"))
            away_team = _text(row.get("away_team"))
            if not home_team or not away_team or home_team == away_team:
                raise HistoryImportError(f"{row_label} has invalid team names")
            kickoff_utc_raw = _text(row.get("kickoff_utc"))
            kickoff_utc = _bundle_datetime(kickoff_utc_raw, f"{row_label} kickoff_utc")
            if kickoff_utc_raw != _canonical_utc_minute(kickoff_utc):
                raise HistoryImportError(f"{row_label} kickoff_utc is not canonical UTC")
            source_kickoff_raw = _text(row.get("source_kickoff"))
            source_kickoff = _bundle_datetime(
                source_kickoff_raw, f"{row_label} source_kickoff"
            )
            if source_kickoff.isoformat(timespec="minutes") != source_kickoff_raw:
                raise HistoryImportError(f"{row_label} source_kickoff is not canonical")
            if row.get("source_timezone") != source_timezone:
                raise HistoryImportError(f"{row_label} source_timezone does not match manifest")
            if source_kickoff_raw != kickoff_utc.astimezone(source_zone).isoformat(
                timespec="minutes"
            ):
                raise HistoryImportError(
                    f"{row_label} source_kickoff and kickoff_utc disagree"
                )
            if source_kickoff.date() > audit_date:
                raise HistoryImportError(
                    f"{row_label} finished fixture is after manifest.as_of_date"
                )
            fixture_key = (kickoff_utc_raw, home_team, away_team)
            score_identity = fixture_rows.get(fixture_key)
            if score_identity is None:
                raise HistoryImportError(
                    f"{row_label} fixture identity has no matching score row"
                )
            expected_regime = _competition_regime(
                league_key, season, source_kickoff.date()
            )
            expected_format = _format_version(league_key, season)
            expected_phase = _phase_group(league_key, row.get("round"))
            expected_status = expected_completeness.get(str(season), {}).get("status")
            market_identity = {
                "league_key": league_key,
                "league": expected_league,
                "season": season,
                "competition_regime": row.get("competition_regime"),
                "format_version": row.get("format_version"),
                "phase_group": row.get("phase_group"),
                "season_status": row.get("season_status"),
                "round": row.get("round"),
                "source_row": _bundle_integer(
                    row.get("source_row"), f"{row_label} source_row", minimum=3
                ),
                "source_kickoff": source_kickoff_raw,
                "source_timezone": source_timezone,
                "kickoff_utc": kickoff_utc_raw,
                "home_team": home_team,
                "away_team": away_team,
            }
            for field, expected in (
                ("competition_regime", expected_regime),
                ("format_version", expected_format),
                ("phase_group", expected_phase),
                ("season_status", expected_status),
            ):
                if market_identity[field] != expected:
                    raise HistoryImportError(f"{row_label} {field} must be {expected}")
            if any(market_identity[field] != score_identity[field] for field in identity_fields):
                raise HistoryImportError(
                    f"{row_label} fixture identity does not match its score row"
                )

            bookmaker = _text(row.get("bookmaker"))
            if bookmaker not in expected_bookmakers:
                raise HistoryImportError(f"{row_label} bookmaker is not a target company")
            books = market_books.setdefault(fixture_key, set())
            if bookmaker in books:
                raise HistoryImportError(
                    f"{row_label} duplicate bookmaker {bookmaker} for fixture"
                )
            books.add(bookmaker)
            for market_name, value_fields in completeness_fields.items():
                expected_complete = all(_text(row.get(field)) for field in value_fields)
                flag = row.get(f"{market_name}_complete")
                if flag not in {"true", "false"} or (flag == "true") != expected_complete:
                    raise HistoryImportError(
                        f"{row_label} {market_name}_complete disagrees with market cells"
                    )
                bookmaker_completeness[bookmaker][market_name] += int(expected_complete)
            market_regime_counts[(season, expected_regime)] += 1
            market_format_counts[(season, expected_format)] += 1
            market_phase_counts[(season, expected_phase)] += 1
            market_status_counts[(season, expected_status)] += 1

        if set(market_books) != set(fixture_rows):
            raise HistoryImportError(f"{label} opening market fixtures do not match score rows")
        for fixture_key, books in market_books.items():
            if books != expected_bookmakers:
                missing = sorted(expected_bookmakers - books)
                extra = sorted(books - expected_bookmakers)
                raise HistoryImportError(
                    f"{label} fixture {fixture_key} must contain exactly the four target "
                    f"bookmakers; missing={missing}, extra={extra}"
                )
        if len(market_rows) != len(score_rows) * len(expected_bookmakers):
            raise HistoryImportError(
                f"{label} opening market rows must equal four target companies per fixture"
            )
        for observed, expected, field in (
            (
                market_regime_counts,
                Counter({key: count * len(BOOKMAKERS) for key, count in regime_counts.items()}),
                "competition_regime",
            ),
            (
                market_format_counts,
                Counter({key: count * len(BOOKMAKERS) for key, count in format_counts.items()}),
                "format_version",
            ),
            (
                market_phase_counts,
                Counter({key: count * len(BOOKMAKERS) for key, count in phase_counts.items()}),
                "phase_group",
            ),
            (
                market_status_counts,
                Counter({key: count * len(BOOKMAKERS) for key, count in status_counts.items()}),
                "season_status",
            ),
        ):
            if observed != expected:
                raise HistoryImportError(
                    f"{label} opening market {field} counts do not match score rows"
                )
        expected_bookmaker_summary = {
            bookmaker: {
                market: {
                    "rows": count,
                    "rate": round(count / len(score_rows), 6),
                }
                for market, count in markets.items()
            }
            for bookmaker, markets in bookmaker_completeness.items()
        }
        if summary.get("bookmaker_opening_completeness") != expected_bookmaker_summary:
            raise HistoryImportError(
                f"{label}.bookmaker_opening_completeness does not match market rows"
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
    as_of_date: str | date,
) -> dict[str, Any]:
    """Import one or more workbooks and write a deterministic local bundle."""

    audit_date = _parse_as_of_date(as_of_date)
    source_paths = _expand_inputs(inputs)
    destination = Path(output_dir).resolve()
    league_summaries: list[dict[str, Any]] = []
    observed_leagues: set[str] = set()
    imported: list[
        tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]
    ] = []
    for source in source_paths:
        summary, score_rows, market_rows = import_workbook(
            source,
            source_timezone=source_timezone,
            as_of_date=audit_date,
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
        "as_of_date": audit_date.isoformat(),
        "season_completeness_policy": dict(SEASON_COMPLETENESS_POLICY),
        "source_timezone": source_timezone,
        "kickoff_year_policy": (
            "explicit per-competition calendar policy; cross-year rollover is "
            "allowed once only for autumn-to-spring competitions and the documented "
            "Brazil 2020/AFC 2022 exceptions"
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
            "Rows labelled partial_as_of_* are right-censored snapshots and cannot support model promotion.",
            "Competition format_version and phase_group labels must be evaluated as separate cohorts when material.",
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
        "--as-of-date",
        required=True,
        help=(
            "audited data snapshot date in YYYY-MM-DD; every finished source fixture "
            "must be on or before this date"
        ),
    )
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
            as_of_date=args.as_of_date,
        )
    except HistoryImportError as exc:
        parser.exit(2, f"history_importer: error: {exc}\n")
    # Windows terminals can inherit a legacy code page (for example cp932),
    # while league labels are intentionally multilingual.  Keep the CLI
    # usable without requiring callers to set PYTHONIOENCODING manually.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

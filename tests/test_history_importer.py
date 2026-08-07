from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "history_importer.py"
SPEC = importlib.util.spec_from_file_location("soccer_history_importer", SCRIPT)
assert SPEC and SPEC.loader
history_importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history_importer)


SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOCUMENT_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
CLOSING_SENTINEL = "CLOSE_ONLY_SECRET"
AS_OF_DATE = "2026-12-31"


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _xml_bytes(element: ET.Element) -> bytes:
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def _worksheet_xml(
    rows: list[list[object]], *, formula_ref: str | None = None
) -> bytes:
    worksheet = ET.Element(f"{{{SPREADSHEET_NS}}}worksheet")
    sheet_data = ET.SubElement(worksheet, f"{{{SPREADSHEET_NS}}}sheetData")
    for row_index, values in enumerate(rows, start=1):
        row = ET.SubElement(
            sheet_data, f"{{{SPREADSHEET_NS}}}row", {"r": str(row_index)}
        )
        for column_index, value in enumerate(values, start=1):
            if value is None:
                continue
            reference = f"{_column_name(column_index)}{row_index}"
            cell = ET.SubElement(row, f"{{{SPREADSHEET_NS}}}c", {"r": reference})
            if reference == formula_ref:
                ET.SubElement(cell, f"{{{SPREADSHEET_NS}}}f").text = "1+1"
                ET.SubElement(cell, f"{{{SPREADSHEET_NS}}}v").text = "2"
            elif isinstance(value, bool):
                cell.set("t", "b")
                ET.SubElement(cell, f"{{{SPREADSHEET_NS}}}v").text = (
                    "1" if value else "0"
                )
            elif isinstance(value, (int, float)):
                ET.SubElement(cell, f"{{{SPREADSHEET_NS}}}v").text = str(value)
            else:
                cell.set("t", "inlineStr")
                inline = ET.SubElement(cell, f"{{{SPREADSHEET_NS}}}is")
                ET.SubElement(inline, f"{{{SPREADSHEET_NS}}}t").text = str(value)
    return _xml_bytes(worksheet)


def _write_workbook(
    path: Path,
    data_rows: list[list[object]],
    *,
    sheet_name: str = "巴甲",
    top_header: list[object] | None = None,
    lower_header: list[object] | None = None,
    formula_ref: str | None = None,
    corner_extension: bool = False,
    corner_match_ids: tuple[int, ...] | None = None,
    auxiliary_sheets: tuple[str, ...] = (),
    auxiliary_formula_ref: str | None = None,
) -> None:
    top = [None] * len(history_importer.EXPECTED_HEADERS)
    for column, value in history_importer.EXPECTED_TOP_HEADERS.items():
        top[column - 1] = value
    if top_header is not None:
        top = top_header
    lower = list(history_importer.EXPECTED_HEADERS)
    if lower_header is not None:
        lower = lower_header
    output_rows = [list(row) for row in data_rows]
    if corner_extension:
        if corner_match_ids is not None and len(corner_match_ids) != len(output_rows):
            raise ValueError("corner_match_ids must match the data row count")
        top.extend(
            [history_importer.CORNER_AUDIT_TOP_HEADER]
            + [None] * (len(history_importer.CORNER_AUDIT_HEADERS) - 1)
        )
        lower.extend(history_importer.CORNER_AUDIT_HEADERS)
        for index, row in enumerate(output_rows, start=1):
            row.extend(
                [
                    (
                        corner_match_ids[index - 1]
                        if corner_match_ids is not None
                        else 1000000 + index
                    ),
                    6,
                    4,
                    10,
                    2,
                    None,
                    None,
                    None,
                    "complete",
                    "https://example.test/corners",
                    "2026-08-03T00:00:00Z",
                    "regulation_90",
                ]
            )

    workbook = ET.Element(
        f"{{{SPREADSHEET_NS}}}workbook",
        {"xmlns:r": DOCUMENT_REL_NS},
    )
    sheets = ET.SubElement(workbook, f"{{{SPREADSHEET_NS}}}sheets")
    ET.SubElement(
        sheets,
        f"{{{SPREADSHEET_NS}}}sheet",
        {
            "name": sheet_name,
            "sheetId": "1",
            f"{{{DOCUMENT_REL_NS}}}id": "rId1",
        },
    )
    for index, auxiliary_name in enumerate(auxiliary_sheets, start=2):
        ET.SubElement(
            sheets,
            f"{{{SPREADSHEET_NS}}}sheet",
            {
                "name": auxiliary_name,
                "sheetId": str(index),
                f"{{{DOCUMENT_REL_NS}}}id": f"rId{index}",
            },
        )

    workbook_relationships = ET.Element(f"{{{PACKAGE_REL_NS}}}Relationships")
    ET.SubElement(
        workbook_relationships,
        f"{{{PACKAGE_REL_NS}}}Relationship",
        {
            "Id": "rId1",
            "Type": f"{DOCUMENT_REL_NS}/worksheet",
            "Target": "worksheets/sheet1.xml",
        },
    )
    for index, _auxiliary_name in enumerate(auxiliary_sheets, start=2):
        ET.SubElement(
            workbook_relationships,
            f"{{{PACKAGE_REL_NS}}}Relationship",
            {
                "Id": f"rId{index}",
                "Type": f"{DOCUMENT_REL_NS}/worksheet",
                "Target": f"worksheets/sheet{index}.xml",
            },
        )

    package_relationships = ET.Element(f"{{{PACKAGE_REL_NS}}}Relationships")
    ET.SubElement(
        package_relationships,
        f"{{{PACKAGE_REL_NS}}}Relationship",
        {
            "Id": "rId1",
            "Type": f"{DOCUMENT_REL_NS}/officeDocument",
            "Target": "xl/workbook.xml",
        },
    )
    content_types = ET.Element(f"{{{CONTENT_TYPES_NS}}}Types")
    ET.SubElement(
        content_types,
        f"{{{CONTENT_TYPES_NS}}}Default",
        {
            "Extension": "rels",
            "ContentType": "application/vnd.openxmlformats-package.relationships+xml",
        },
    )
    ET.SubElement(
        content_types,
        f"{{{CONTENT_TYPES_NS}}}Default",
        {"Extension": "xml", "ContentType": "application/xml"},
    )
    ET.SubElement(
        content_types,
        f"{{{CONTENT_TYPES_NS}}}Override",
        {
            "PartName": "/xl/workbook.xml",
            "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        },
    )
    for index, _auxiliary_name in enumerate(auxiliary_sheets, start=2):
        ET.SubElement(
            content_types,
            f"{{{CONTENT_TYPES_NS}}}Override",
            {
                "PartName": f"/xl/worksheets/sheet{index}.xml",
                "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
            },
        )
    ET.SubElement(
        content_types,
        f"{{{CONTENT_TYPES_NS}}}Override",
        {
            "PartName": "/xl/worksheets/sheet1.xml",
            "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
        },
    )

    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _xml_bytes(content_types))
        archive.writestr("_rels/.rels", _xml_bytes(package_relationships))
        archive.writestr("xl/workbook.xml", _xml_bytes(workbook))
        archive.writestr(
            "xl/_rels/workbook.xml.rels", _xml_bytes(workbook_relationships)
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            _worksheet_xml([top, lower, *output_rows], formula_ref=formula_ref),
        )
        for index, auxiliary_name in enumerate(auxiliary_sheets, start=2):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _worksheet_xml(
                    [[auxiliary_name], ["审计数据"]],
                    formula_ref=auxiliary_formula_ref,
                ),
            )


def _result_label(home: int, away: int) -> str:
    if home > away:
        return "胜"
    if home == away:
        return "平"
    return "负"


def _data_row(
    identifier: int,
    kickoff: str,
    *,
    season: int = 2020,
    home: str = "主队甲",
    away: str = "客队乙",
    half_score: str = "0-0",
    full_score: str = "1-0",
    total_goals: int | None = None,
    league: str = "巴甲",
    round_label: str | None = None,
) -> list[object]:
    half_home, half_away = map(int, half_score.split("-"))
    full_home, full_away = map(int, full_score.split("-"))
    row: list[object] = [None] * len(history_importer.EXPECTED_HEADERS)
    row[:15] = [
        identifier,
        season,
        league,
        round_label or f"{league} 第{identifier}轮",
        kickoff,
        "完",
        1,
        home,
        away,
        2,
        half_score,
        full_score,
        full_home + full_away if total_goals is None else total_goals,
        f"{_result_label(half_home, half_away)}-{_result_label(full_home, full_away)}",
        _result_label(full_home, full_away),
    ]
    for book_index, _bookmaker in enumerate(history_importer.BOOKMAKERS):
        offset = 15 + book_index * 18
        row[offset : offset + 18] = [
            1.8 + book_index * 0.01,
            3.2,
            4.1,
            CLOSING_SENTINEL,
            CLOSING_SENTINEL,
            CLOSING_SENTINEL,
            0.91,
            "平/半",
            0.97,
            CLOSING_SENTINEL,
            CLOSING_SENTINEL,
            CLOSING_SENTINEL,
            0.90,
            "2.5",
            0.98,
            CLOSING_SENTINEL,
            CLOSING_SENTINEL,
            CLOSING_SENTINEL,
        ]
    return row


class HistoryImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _import(
        self,
        rows: list[list[object]],
        *,
        as_of_date: str = AS_OF_DATE,
        **workbook_options,
    ):
        path = self.base / "history.xlsx"
        _write_workbook(path, rows, **workbook_options)
        return history_importer.import_workbook(
            path,
            source_timezone="Asia/Shanghai",
            as_of_date=as_of_date,
        )

    def _bundle(
        self,
        name: str,
        rows: list[list[object]],
        *,
        sheet_name: str = "巴甲",
        as_of_date: str = AS_OF_DATE,
    ) -> tuple[Path, dict[str, object]]:
        workbook = self.base / f"{name}.xlsx"
        output_dir = self.base / f"{name}-bundle"
        _write_workbook(workbook, rows, sheet_name=sheet_name)
        manifest = history_importer.import_bundle(
            [workbook],
            output_dir,
            source_timezone="Asia/Shanghai",
            as_of_date=as_of_date,
        )
        return output_dir, manifest

    def _tamper_csv_and_rehash(
        self,
        output_dir: Path,
        manifest: dict[str, object],
        *,
        metadata_key: str,
        fields: tuple[str, ...],
        mutate,
    ) -> None:
        league = manifest["leagues"][0]
        metadata = league[metadata_key]
        path = output_dir / metadata["file"]
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        mutate(rows)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        metadata["sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["bundle_hash"] = history_importer._canonical_manifest_hash(manifest)
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_two_level_headers_and_half_full_time_labels_are_normalized(self):
        summary, score_rows, market_rows = self._import(
            [
                _data_row(
                    1,
                    "08-01 20:00",
                    half_score="0-0",
                    full_score="2-1",
                )
            ]
        )

        self.assertEqual(summary["league_key"], "brazil_serie_a")
        self.assertEqual(len(score_rows), 1)
        self.assertEqual(len(market_rows), len(history_importer.BOOKMAKERS))
        self.assertEqual(score_rows[0]["half_home_goals"], 0)
        self.assertEqual(score_rows[0]["half_away_goals"], 0)
        self.assertEqual(score_rows[0]["half_result"], "D")
        self.assertEqual(score_rows[0]["full_result"], "H")
        self.assertEqual(score_rows[0]["htft_result"], "DH")
        self.assertEqual(score_rows[0]["competition_regime"], "regular")
        self.assertEqual(score_rows[0]["format_version"], "standard_league_format")
        self.assertEqual(score_rows[0]["phase_group"], "regular_season")
        self.assertEqual(score_rows[0]["season_status"], "partial_as_of_2026-12-31")
        self.assertTrue(
            all(row["competition_regime"] == "regular" for row in market_rows)
        )
        self.assertEqual(set(score_rows[0]), set(history_importer.SCORE_FIELDS))

    def test_expanded_competition_workbooks_have_stable_model_keys(self):
        expected = {
            "巴西杯": ("brazil_cup", "brazil-cup"),
            "英超": ("england_premier_league", "england-premier-league"),
            "法甲": ("france_ligue_1", "france-ligue-1"),
            "西甲": ("spain_la_liga", "spain-la-liga"),
            "德甲": ("germany_bundesliga", "germany-bundesliga"),
            "意甲": ("italy_serie_a", "italy-serie-a"),
            "韩K联": ("korea_k_league_1", "korea-k-league-1"),
            "瑞典超": ("sweden_allsvenskan", "sweden-allsvenskan"),
            "芬超": ("finland_veikkausliiga", "finland-veikkausliiga"),
            "欧冠": ("uefa_champions_league", "uefa-champions-league"),
            "欧国联": ("uefa_nations_league", "uefa-nations-league"),
            "亚冠": ("afc_champions_league", "afc-champions-league"),
        }

        for league, (league_key, output_stem) in expected.items():
            with self.subTest(league=league):
                summary, score_rows, _market_rows = self._import(
                    [_data_row(1, "08-01 20:00", league=league)],
                    sheet_name=league,
                    corner_extension=league == "欧国联",
                )
                self.assertEqual(summary["league_key"], league_key)
                self.assertEqual(summary["output_stem"], output_stem)
                self.assertEqual(score_rows[0]["league_key"], league_key)

    def test_cup_and_nations_formats_phases_and_cross_year_cycle_are_audited(self):
        brazil_summary, brazil_rows, _ = self._import(
            [
                _data_row(
                    1,
                    "02-20 20:30",
                    season=2026,
                    league="巴西杯",
                    round_label="第一圈",
                )
            ],
            sheet_name="巴西杯",
        )
        self.assertEqual(brazil_summary["league_key"], "brazil_cup")
        self.assertEqual(brazil_rows[0]["competition_regime"], "national_knockout_cup")
        self.assertEqual(
            brazil_rows[0]["format_version"], "copa_do_brasil_2026_expanded"
        )
        self.assertEqual(brazil_rows[0]["phase_group"], "knockout")

        nations_summary, nations_rows, _ = self._import(
            [
                _data_row(
                    1,
                    "09-03 20:45",
                    season=2020,
                    home="甲",
                    away="乙",
                    league="欧国联",
                    round_label="A联赛 第1轮",
                ),
                _data_row(
                    2,
                    "11-18 20:45",
                    season=2020,
                    home="丙",
                    away="丁",
                    league="欧国联",
                    round_label="B联赛 第6轮",
                ),
                _data_row(
                    3,
                    "10-06 20:45",
                    season=2020,
                    home="戊",
                    away="己",
                    league="欧国联",
                    round_label="A联半决赛",
                ),
            ],
            sheet_name="欧国联",
            as_of_date="2021-12-31",
            corner_extension=True,
        )
        self.assertEqual(nations_summary["league_key"], "uefa_nations_league")
        self.assertEqual(
            nations_summary["calendar_rollovers"][0]["to_calendar_year"], 2021
        )
        self.assertEqual(
            {row["competition_regime"] for row in nations_rows},
            {"national_team_league_and_knockout"},
        )
        self.assertEqual(
            {row["format_version"] for row in nations_rows},
            {"uefa_nations_league_2020_2022_edition"},
        )
        self.assertEqual(
            [row["phase_group"] for row in nations_rows],
            ["league_phase", "league_phase", "knockout"],
        )

    def test_norway_relegation_playoff_round_is_explicitly_non_regular(self):
        norway_sheet = next(
            name
            for name, spec in history_importer.LEAGUE_SPECS.items()
            if spec["league_key"] == "norway_eliteserien"
        )
        summary, score_rows, market_rows = self._import(
            [
                _data_row(
                    1,
                    "11-30 17:00",
                    season=2025,
                    league=norway_sheet,
                    home="Regular Home",
                    away="Regular Away",
                    round_label="30",
                ),
                _data_row(
                    2,
                    "12-07 17:00",
                    season=2025,
                    league=norway_sheet,
                    home="Playoff Home",
                    away="Playoff Away",
                    round_label="保级附加赛 第1轮",
                ),
            ],
            sheet_name=norway_sheet,
            as_of_date="2025-12-31",
        )

        self.assertEqual(
            [row["competition_regime"] for row in score_rows],
            ["regular", "relegation_playoff"],
        )
        self.assertEqual(
            [row["phase_group"] for row in score_rows],
            ["regular_season", "relegation_playoff"],
        )
        self.assertEqual(
            summary["competition_regimes"]["2025"],
            {"regular": 1, "relegation_playoff": 1},
        )
        self.assertEqual(
            summary["phase_groups"]["2025"],
            {"regular_season": 1, "relegation_playoff": 1},
        )
        self.assertEqual(
            {row["competition_regime"] for row in market_rows},
            {"regular", "relegation_playoff"},
        )

    def test_nations_2020_cycle_can_cross_2021_into_2022_playouts(self):
        summary, score_rows, _ = self._import(
            [
                _data_row(
                    1,
                    "11-18 20:45",
                    season=2020,
                    home="甲",
                    away="乙",
                    league="欧国联",
                    round_label="A联赛 第6轮",
                ),
                _data_row(
                    2,
                    "10-10 20:45",
                    season=2020,
                    home="丙",
                    away="丁",
                    league="欧国联",
                    round_label="A联决赛",
                ),
                _data_row(
                    3,
                    "03-25 20:45",
                    season=2020,
                    home="戊",
                    away="己",
                    league="欧国联",
                    round_label="C联淘汰 首回合",
                ),
            ],
            sheet_name="欧国联",
            as_of_date="2022-12-31",
            corner_extension=True,
        )

        self.assertEqual(
            [row["source_kickoff"] for row in score_rows],
            [
                "2020-11-18T20:45+08:00",
                "2021-10-10T20:45+08:00",
                "2022-03-25T20:45+08:00",
            ],
        )
        self.assertEqual(
            summary["calendar_rollovers"],
            [
                {
                    "season": 2020,
                    "source_row": 4,
                    "from_calendar_year": 2020,
                    "to_calendar_year": 2021,
                    "trigger": "11->10",
                },
                {
                    "season": 2020,
                    "source_row": 5,
                    "from_calendar_year": 2021,
                    "to_calendar_year": 2022,
                    "trigger": "10->03",
                },
            ],
        )

    def test_nations_administrative_awards_are_rejected_by_titan_match_id(self):
        for match_id in (1858422, 1858413):
            with (
                self.subTest(match_id=match_id),
                self.assertRaisesRegex(
                    history_importer.HistoryImportError,
                    rf"administrative result.*{match_id}|match {match_id}.*administrative",
                ),
            ):
                self._import(
                    [
                        _data_row(
                            1,
                            "11-18 20:45",
                            season=2020,
                            league="欧国联",
                            full_score="3-0",
                            round_label="A联赛 第6轮",
                        )
                    ],
                    sheet_name="欧国联",
                    corner_extension=True,
                    corner_match_ids=(match_id,),
                )

    def test_nations_requires_source_bound_titan_match_id(self):
        with self.assertRaisesRegex(
            history_importer.HistoryImportError,
            "Titan比赛ID is required",
        ):
            self._import(
                [_data_row(1, "09-03 20:45", league="欧国联")],
                sheet_name="欧国联",
            )

    def test_finland_multistage_format_aliases_and_partial_status_are_audited(self):
        summary, score_rows, market_rows = self._import(
            [
                _data_row(
                    1,
                    "04-04 18:00",
                    season=2026,
                    home="甲",
                    away="乙",
                    league="芬超",
                    round_label="第1轮",
                ),
                _data_row(
                    2,
                    "09-09 18:00",
                    season=2026,
                    home="丙",
                    away="丁",
                    league="芬超",
                    round_label="争冠组 第1轮",
                ),
                _data_row(
                    3,
                    "09-10 18:00",
                    season=2026,
                    home="戊",
                    away="己",
                    league="芬超",
                    round_label="保级组 第1轮",
                ),
                _data_row(
                    4,
                    "10-20 18:00",
                    season=2026,
                    home="庚",
                    away="辛",
                    league="芬超",
                    round_label="欧会杯附加赛",
                ),
            ],
            sheet_name="芬超",
        )

        self.assertEqual(summary["league_key"], "finland_veikkausliiga")
        self.assertEqual(
            summary["aliases"],
            [
                "finland_veikkausliiga",
                "芬超",
                "芬兰超级联赛",
                "Veikkausliiga",
                "Finland Veikkausliiga",
            ],
        )
        self.assertEqual(
            [row["phase_group"] for row in score_rows],
            [
                "regular_season",
                "championship_split",
                "relegation_split",
                "european_playoff",
            ],
        )
        self.assertEqual(
            {row["format_version"] for row in score_rows},
            {"veikkausliiga_12_team_double_championship_split"},
        )
        self.assertEqual(
            {row["season_status"] for row in score_rows},
            {"partial_as_of_2026-12-31"},
        )
        self.assertEqual(
            summary["season_completeness"]["2026"]["expected_matches"], 179
        )
        self.assertEqual(
            summary["season_completeness"]["2026"]["remaining_matches"], 175
        )
        self.assertEqual(len(market_rows), 4 * len(history_importer.BOOKMAKERS))

    def test_japan_2026_vision_batch_marks_all_source_fixtures(self):
        start = date(2026, 2, 6)
        end = date(2026, 6, 6)
        span_days = (end - start).days
        rows: list[list[object]] = []
        slots_by_date: dict[date, int] = {}
        for index in range(180):
            fixture_date = start + timedelta(days=index * span_days // 179)
            slot = slots_by_date.get(fixture_date, 0)
            slots_by_date[fixture_date] = slot + 1
            rows.append(
                _data_row(
                    index + 1,
                    f"{fixture_date:%m-%d} {12 + slot:02d}:00",
                    season=2026,
                    home=f"主队{index}",
                    away=f"客队{index}",
                    league="日职",
                )
            )

        summary, score_rows, market_rows = self._import(rows, sheet_name="日职")

        self.assertEqual(len(score_rows), 180)
        self.assertEqual(
            {row["competition_regime"] for row in score_rows},
            {"2026_vision_regional"},
        )
        self.assertEqual(
            summary["competition_regimes"],
            {"2026": {"2026_vision_regional": 180}},
        )
        self.assertEqual(len(market_rows), 180 * len(history_importer.BOOKMAKERS))
        self.assertEqual(
            {row["season_status"] for row in score_rows},
            {"partial_as_of_2026-12-31"},
        )
        self.assertEqual(
            summary["season_completeness"]["2026"],
            {
                "as_of_date": AS_OF_DATE,
                "observed_matches": 180,
                "expected_matches": 200,
                "remaining_matches": 20,
                "expectation_id": ("known-schedule-match-counts-v1:japan_j1:2026"),
                "expectation_basis": (
                    "versioned source-scope competition schedule total"
                ),
                "expectation_verified": True,
                "status": "partial_as_of_2026-12-31",
            },
        )
        self.assertEqual(
            {row["competition_regime"] for row in market_rows},
            {"2026_vision_regional"},
        )

    def test_japan_vision_date_bounds_include_placement_finals(self):
        _summary, score_rows, _market_rows = self._import(
            [
                _data_row(
                    1,
                    "02-05 12:00",
                    season=2026,
                    home="甲",
                    away="乙",
                    league="日职",
                ),
                _data_row(
                    2,
                    "02-06 12:00",
                    season=2026,
                    home="丙",
                    away="丁",
                    league="日职",
                ),
                _data_row(
                    3,
                    "06-06 12:00",
                    season=2026,
                    home="戊",
                    away="己",
                    league="日职",
                ),
                _data_row(
                    4,
                    "06-07 12:00",
                    season=2026,
                    home="庚",
                    away="辛",
                    league="日职",
                ),
            ],
            sheet_name="日职",
        )

        self.assertEqual(
            [row["competition_regime"] for row in score_rows],
            [
                "regular",
                "2026_vision_regional",
                "2026_vision_regional",
                "regular",
            ],
        )

    def test_both_header_rows_are_part_of_the_schema_contract(self):
        top = [None] * len(history_importer.EXPECTED_HEADERS)
        for column, value in history_importer.EXPECTED_TOP_HEADERS.items():
            top[column - 1] = value
        top[15] = "错误分组"
        with self.subTest("top header"):
            with self.assertRaisesRegex(
                history_importer.HistoryImportError, "market header schema"
            ):
                self._import([_data_row(1, "08-01 20:00")], top_header=top)

        lower = list(history_importer.EXPECTED_HEADERS)
        lower[4] = "开球"
        with self.subTest("lower header"):
            with self.assertRaisesRegex(
                history_importer.HistoryImportError, "header mismatch"
            ):
                self._import([_data_row(1, "08-01 20:00")], lower_header=lower)

    def test_brazil_2020_december_to_january_rollover_is_explicit(self):
        summary, score_rows, _market_rows = self._import(
            [
                _data_row(1, "12-20 20:00", home="甲", away="乙"),
                _data_row(2, "01-10 20:00", home="丙", away="丁"),
            ]
        )

        self.assertEqual(score_rows[0]["source_kickoff"], "2020-12-20T20:00+08:00")
        self.assertEqual(score_rows[1]["source_kickoff"], "2021-01-10T20:00+08:00")
        self.assertEqual(score_rows[1]["kickoff_utc"], "2021-01-10T12:00Z")
        self.assertEqual(score_rows[1]["date"], "2021-01-10")
        self.assertEqual(score_rows[1]["season"], 2020)
        self.assertEqual(
            summary["calendar_rollovers"],
            [
                {
                    "season": 2020,
                    "source_row": 4,
                    "from_calendar_year": 2020,
                    "to_calendar_year": 2021,
                    "trigger": "12->01",
                }
            ],
        )

    def test_brazil_cup_2020_december_to_march_rollover_is_explicit(self):
        brazil_cup_sheet = next(
            name
            for name, spec in history_importer.LEAGUE_SPECS.items()
            if spec["league_key"] == "brazil_cup"
        )
        summary, score_rows, _market_rows = self._import(
            [
                _data_row(
                    1,
                    "12-31 08:30",
                    season=2020,
                    league=brazil_cup_sheet,
                    home="Palmeiras",
                    away="America Mineiro",
                    round_label="Semi-final second leg",
                ),
                _data_row(
                    2,
                    "03-01 08:00",
                    season=2020,
                    league=brazil_cup_sheet,
                    home="Gremio",
                    away="Palmeiras",
                    round_label="Final first leg",
                ),
                _data_row(
                    3,
                    "03-08 05:00",
                    season=2020,
                    league=brazil_cup_sheet,
                    home="Palmeiras",
                    away="Gremio",
                    round_label="Final second leg",
                ),
            ],
            sheet_name=brazil_cup_sheet,
            as_of_date="2021-12-31",
        )

        self.assertEqual(
            [row["source_kickoff"] for row in score_rows],
            [
                "2020-12-31T08:30+08:00",
                "2021-03-01T08:00+08:00",
                "2021-03-08T05:00+08:00",
            ],
        )
        self.assertEqual(
            summary["calendar_rollovers"],
            [
                {
                    "season": 2020,
                    "source_row": 4,
                    "from_calendar_year": 2020,
                    "to_calendar_year": 2021,
                    "trigger": "12->03",
                }
            ],
        )

    def test_brazil_cup_rollover_is_not_allowed_outside_2020(self):
        brazil_cup_sheet = next(
            name
            for name, spec in history_importer.LEAGUE_SPECS.items()
            if spec["league_key"] == "brazil_cup"
        )
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "kickoff order goes backwards"
        ):
            self._import(
                [
                    _data_row(
                        1,
                        "12-31 08:30",
                        season=2021,
                        league=brazil_cup_sheet,
                        home="Team A",
                        away="Team B",
                    ),
                    _data_row(
                        2,
                        "03-01 08:00",
                        season=2021,
                        league=brazil_cup_sheet,
                        home="Team C",
                        away="Team D",
                    ),
                ],
                sheet_name=brazil_cup_sheet,
                as_of_date="2022-12-31",
            )

    def test_completed_nations_cycles_can_reach_third_calendar_year(self):
        nations_sheet = next(
            name
            for name, spec in history_importer.LEAGUE_SPECS.items()
            if spec["league_key"] == "uefa_nations_league"
        )
        for season in (2022, 2024):
            with self.subTest(season=season):
                summary, score_rows, _market_rows = self._import(
                    [
                        _data_row(
                            1,
                            "11-18 20:45",
                            season=season,
                            league=nations_sheet,
                            home="Team A",
                            away="Team B",
                            round_label="League phase",
                        ),
                        _data_row(
                            2,
                            "06-09 03:00",
                            season=season,
                            league=nations_sheet,
                            home="Team C",
                            away="Team D",
                            round_label="Final",
                        ),
                        _data_row(
                            3,
                            "03-27 01:00",
                            season=season,
                            league=nations_sheet,
                            home="Team E",
                            away="Team F",
                            round_label="C/D play-out first leg",
                        ),
                    ],
                    sheet_name=nations_sheet,
                    as_of_date=f"{season + 2}-12-31",
                    corner_extension=True,
                )

                self.assertEqual(
                    [row["source_kickoff"] for row in score_rows],
                    [
                        f"{season}-11-18T20:45+08:00",
                        f"{season + 1}-06-09T03:00+08:00",
                        f"{season + 2}-03-27T01:00+08:00",
                    ],
                )
                self.assertEqual(
                    [
                        event["to_calendar_year"]
                        for event in summary["calendar_rollovers"]
                    ],
                    [season + 1, season + 2],
                )
                self.assertEqual(
                    {row["format_version"] for row in score_rows},
                    {f"uefa_nations_league_{season}_{season + 2}_edition"},
                )

    def test_afc_delayed_season_august_to_february_rollover_is_explicit(self):
        summary, score_rows, _market_rows = self._import(
            [
                _data_row(
                    1,
                    "08-25 18:30",
                    season=2022,
                    home="甲",
                    away="乙",
                    league="亚冠",
                ),
                _data_row(
                    2,
                    "02-19 23:00",
                    season=2022,
                    home="丙",
                    away="丁",
                    league="亚冠",
                ),
            ],
            sheet_name="亚冠",
        )

        self.assertEqual(score_rows[0]["source_kickoff"], "2022-08-25T18:30+08:00")
        self.assertEqual(score_rows[1]["source_kickoff"], "2023-02-19T23:00+08:00")
        self.assertEqual(
            summary["calendar_rollovers"],
            [
                {
                    "season": 2022,
                    "source_row": 4,
                    "from_calendar_year": 2022,
                    "to_calendar_year": 2023,
                    "trigger": "08->02",
                }
            ],
        )

    def test_calendar_year_league_cannot_infer_august_to_february_rollover(self):
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "kickoff order goes backwards"
        ):
            self._import(
                [
                    _data_row(
                        1,
                        "08-25 18:30",
                        season=2022,
                        home="甲",
                        away="乙",
                        league="韩K联",
                    ),
                    _data_row(
                        2,
                        "02-19 19:00",
                        season=2022,
                        home="丙",
                        away="丁",
                        league="韩K联",
                    ),
                ],
                sheet_name="韩K联",
            )

    def test_format_phase_and_partial_season_semantics_are_preserved(self):
        summary, score_rows, market_rows = self._import(
            [
                _data_row(
                    1,
                    "03-01 18:00",
                    season=2026,
                    home="甲",
                    away="乙",
                    league="韩K联",
                    round_label="第1轮",
                ),
                _data_row(
                    2,
                    "10-20 18:00",
                    season=2026,
                    home="丙",
                    away="丁",
                    league="韩K联",
                    round_label="争冠组 第1轮",
                ),
                _data_row(
                    3,
                    "10-21 18:00",
                    season=2026,
                    home="戊",
                    away="己",
                    league="韩K联",
                    round_label="保级组 第1轮",
                ),
            ],
            sheet_name="韩K联",
        )

        self.assertEqual(
            [row["round"] for row in score_rows],
            ["1", "争冠组 第1轮", "保级组 第1轮"],
        )
        self.assertEqual(
            [row["phase_group"] for row in score_rows],
            ["regular_season", "championship_split", "relegation_split"],
        )
        self.assertEqual(
            {row["format_version"] for row in score_rows},
            {"k1_12_team_split"},
        )
        self.assertEqual(
            {row["season_status"] for row in score_rows},
            {"partial_as_of_2026-12-31"},
        )
        self.assertEqual(
            summary["phase_groups"],
            {
                "2026": {
                    "championship_split": 1,
                    "regular_season": 1,
                    "relegation_split": 1,
                }
            },
        )
        self.assertTrue(
            all(
                row["season_status"] == "partial_as_of_2026-12-31"
                for row in market_rows
            )
        )

    def test_ucl_format_version_changes_without_merging_phase_labels(self):
        _summary, score_rows, _market_rows = self._import(
            [
                _data_row(
                    1,
                    "09-01 20:00",
                    season=2023,
                    home="甲",
                    away="乙",
                    league="欧冠",
                    round_label="分组赛 第1轮",
                ),
                _data_row(
                    2,
                    "09-02 20:00",
                    season=2024,
                    home="丙",
                    away="丁",
                    league="欧冠",
                    round_label="联赛阶段 第1轮",
                ),
            ],
            sheet_name="欧冠",
        )

        self.assertEqual(
            [row["format_version"] for row in score_rows],
            ["ucl_32_team_group", "ucl_36_team_league_phase"],
        )
        self.assertEqual(
            [row["phase_group"] for row in score_rows],
            ["group_stage", "league_phase"],
        )
        self.assertEqual(
            [row["round"] for row in score_rows],
            ["分组赛 第1轮", "联赛阶段 第1轮"],
        )

    def test_any_formula_is_rejected_even_when_a_cached_value_exists(self):
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "formulas are not allowed"
        ):
            self._import([_data_row(1, "08-01 20:00")], formula_ref="P3")

    def test_registered_corner_extension_and_audit_sheets_are_importable(self):
        summary, score_rows, market_rows = self._import(
            [_data_row(1, "08-01 20:00")],
            corner_extension=True,
            auxiliary_sheets=("角球盘口", "数据质量"),
        )
        self.assertEqual(summary["rows"], 1)
        self.assertEqual(len(score_rows), 1)
        self.assertEqual(len(market_rows), len(history_importer.BOOKMAKERS))

    def test_unregistered_auxiliary_sheet_is_rejected(self):
        with self.assertRaisesRegex(
            history_importer.HistoryImportError,
            "unsupported auxiliary worksheets",
        ):
            self._import(
                [_data_row(1, "08-01 20:00")],
                auxiliary_sheets=("任意隐藏训练特征",),
            )

    def test_corner_extension_header_must_match_exact_registered_schema(self):
        top = [None] * len(history_importer.EXPECTED_HEADERS)
        for column, value in history_importer.EXPECTED_TOP_HEADERS.items():
            top[column - 1] = value
        top.extend(
            [history_importer.CORNER_AUDIT_TOP_HEADER]
            + [None] * (len(history_importer.CORNER_AUDIT_HEADERS) - 1)
        )
        lower = list(history_importer.EXPECTED_HEADERS) + list(
            history_importer.CORNER_AUDIT_HEADERS
        )
        lower[-1] = "可疑未来特征"
        with self.assertRaisesRegex(
            history_importer.HistoryImportError,
            "corner-audit column schema",
        ):
            self._import(
                [_data_row(1, "08-01 20:00")],
                top_header=top,
                lower_header=lower,
            )

    def test_formula_in_registered_auxiliary_sheet_is_rejected(self):
        with self.assertRaisesRegex(
            history_importer.HistoryImportError,
            "formulas are not allowed.*角球盘口",
        ):
            self._import(
                [_data_row(1, "08-01 20:00")],
                auxiliary_sheets=("角球盘口",),
                auxiliary_formula_ref="A1",
            )

    def test_malformed_score_is_rejected(self):
        row = _data_row(1, "08-01 20:00")
        row[10] = "not-a-score"
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "invalid 半场比分"
        ):
            self._import([row])

    def test_total_goals_conflict_is_rejected(self):
        row = _data_row(1, "08-01 20:00", full_score="2-1", total_goals=2)
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "总进球数 does not match"
        ):
            self._import([row])

    def test_half_time_goals_cannot_exceed_full_time_goals(self):
        row = _data_row(1, "08-01 20:00", half_score="2-0", full_score="1-0")
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "cannot exceed"
        ):
            self._import([row])

    def test_backwards_kickoff_order_inside_a_season_is_rejected(self):
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "kickoff order goes backwards"
        ):
            self._import(
                [
                    _data_row(1, "08-02 20:00", home="甲", away="乙"),
                    _data_row(2, "08-01 20:00", home="丙", away="丁"),
                ]
            )

    def test_duplicate_fixture_is_rejected(self):
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "duplicate fixture"
        ):
            self._import(
                [
                    _data_row(1, "08-01 20:00"),
                    _data_row(2, "08-01 20:00"),
                ]
            )

    def test_finished_fixture_after_explicit_as_of_date_is_rejected(self):
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "after as_of_date 2026-08-03"
        ):
            self._import(
                [
                    _data_row(
                        1,
                        "08-04 18:00",
                        season=2026,
                        league="芬超",
                    )
                ],
                sheet_name="芬超",
                as_of_date="2026-08-03",
            )

    def test_known_schedule_overflow_is_rejected(self):
        with self.assertRaisesRegex(
            history_importer.HistoryImportError,
            "exceeding the audited schedule expectation",
        ):
            history_importer._season_completeness(
                "brazil_serie_a", 2026, 381, "2026-12-31"
            )

    def test_bundle_outputs_opening_markets_but_never_closing_prices(self):
        workbook = self.base / "history.xlsx"
        _write_workbook(workbook, [_data_row(1, "08-01 20:00")])
        output_dir = self.base / "bundle"

        manifest = history_importer.import_bundle(
            [workbook],
            output_dir,
            source_timezone="Asia/Shanghai",
            as_of_date=AS_OF_DATE,
        )
        score_path = output_dir / "brazil-serie-a-scores.csv"
        market_path = output_dir / "brazil-serie-a-opening-markets.csv"
        score_text = score_path.read_text(encoding="utf-8")
        market_text = market_path.read_text(encoding="utf-8")

        self.assertIn(
            CLOSING_SENTINEL, str(history_importer.read_xlsx_rows(workbook)[1])
        )
        self.assertNotIn(CLOSING_SENTINEL, score_text)
        self.assertNotIn(CLOSING_SENTINEL, market_text)
        with market_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            self.assertEqual(reader.fieldnames, list(history_importer.MARKET_FIELDS))
            self.assertFalse(
                any("close" in field.lower() for field in reader.fieldnames or [])
            )
            self.assertEqual(len(list(reader)), len(history_importer.BOOKMAKERS))
        self.assertIn("all closing prices", manifest["quarantined_fields"])
        self.assertEqual(manifest["as_of_date"], AS_OF_DATE)
        self.assertEqual(
            manifest["season_completeness_policy"],
            history_importer.SEASON_COMPLETENESS_POLICY,
        )
        self.assertEqual(
            manifest["outcome_label_fields"][-3:],
            ["half_result", "full_result", "htft_result"],
        )
        self.assertEqual(
            manifest["competition_regime_counts"],
            [
                {
                    "league_key": "brazil_serie_a",
                    "season": 2020,
                    "competition_regime": "regular",
                    "rows": 1,
                }
            ],
        )
        self.assertEqual(history_importer.validate_bundle(output_dir), manifest)

    def test_semantic_validator_rejects_rehashed_regime_tampering(self):
        workbook = self.base / "japan-history.xlsx"
        _write_workbook(
            workbook,
            [
                _data_row(
                    1,
                    "02-06 18:00",
                    season=2026,
                    league="日职",
                )
            ],
            sheet_name="日职",
        )
        output_dir = self.base / "bundle"
        manifest = history_importer.import_bundle(
            [workbook],
            output_dir,
            source_timezone="Asia/Shanghai",
            as_of_date=AS_OF_DATE,
        )
        score_path = output_dir / "japan-j1-scores.csv"
        with score_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["competition_regime"] = "regular"
        with score_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=history_importer.SCORE_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        league = manifest["leagues"][0]
        league["score_dataset"]["sha256"] = (
            "sha256:" + hashlib.sha256(score_path.read_bytes()).hexdigest()
        )
        league["competition_regimes"] = {"2026": {"regular": 1}}
        manifest["competition_regime_counts"] = [
            {
                "league_key": "japan_j1",
                "season": 2026,
                "competition_regime": "regular",
                "rows": 1,
            }
        ]
        manifest["bundle_hash"] = history_importer._canonical_manifest_hash(manifest)
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            history_importer.HistoryImportError,
            "competition_regime must be 2026_vision_regional",
        ):
            history_importer.validate_bundle(output_dir)

    def test_semantic_validator_rejects_rehashed_score_tampering(self):
        cases = (
            (
                "negative-goal",
                lambda rows: rows[0].update({"home_goals": "-1"}),
                "home_goals must be a non-negative integer",
            ),
            (
                "half-exceeds-full",
                lambda rows: rows[0].update(
                    {"home_goals": "1", "half_home_goals": "2"}
                ),
                "half-time goals cannot exceed full-time goals",
            ),
            (
                "result-label",
                lambda rows: rows[0].update({"htft_result": "AA"}),
                "htft_result disagrees with scores",
            ),
            (
                "half-result-label",
                lambda rows: rows[0].update({"half_result": "H"}),
                "half_result disagrees with scores",
            ),
            (
                "full-result-label",
                lambda rows: rows[0].update({"full_result": "A"}),
                "full_result disagrees with scores",
            ),
            (
                "date-kickoff",
                lambda rows: rows[0].update({"date": "2020-08-02"}),
                "date and kickoff_utc disagree",
            ),
            (
                "source-utc-kickoff",
                lambda rows: rows[0].update(
                    {"source_kickoff": "2020-08-01T21:00+08:00"}
                ),
                "source_kickoff and kickoff_utc disagree",
            ),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name):
                output_dir, manifest = self._bundle(name, [_data_row(1, "08-01 20:00")])
                self._tamper_csv_and_rehash(
                    output_dir,
                    manifest,
                    metadata_key="score_dataset",
                    fields=history_importer.SCORE_FIELDS,
                    mutate=mutate,
                )
                with self.assertRaisesRegex(
                    history_importer.HistoryImportError, message
                ):
                    history_importer.validate_bundle(output_dir)

    def test_semantic_validator_rejects_rehashed_duplicate_score_fixture(self):
        output_dir, manifest = self._bundle(
            "duplicate-score",
            [
                _data_row(1, "08-01 20:00", home="甲", away="乙"),
                _data_row(2, "08-02 20:00", home="丙", away="丁"),
            ],
        )

        def duplicate(rows):
            for field in (
                "date",
                "home_team",
                "away_team",
                "source_kickoff",
                "kickoff_utc",
            ):
                rows[1][field] = rows[0][field]

        self._tamper_csv_and_rehash(
            output_dir,
            manifest,
            metadata_key="score_dataset",
            fields=history_importer.SCORE_FIELDS,
            mutate=duplicate,
        )
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "duplicate fixture"
        ):
            history_importer.validate_bundle(output_dir)

    def test_semantic_validator_rejects_rehashed_market_company_tampering(self):
        output_dir, manifest = self._bundle(
            "market-company", [_data_row(1, "08-01 20:00")]
        )

        def duplicate_company(rows):
            crown = next(row for row in rows if row["bookmaker"] == "crown")
            crown["bookmaker"] = "36"

        self._tamper_csv_and_rehash(
            output_dir,
            manifest,
            metadata_key="opening_market_research",
            fields=history_importer.MARKET_FIELDS,
            mutate=duplicate_company,
        )
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "duplicate bookmaker 36"
        ):
            history_importer.validate_bundle(output_dir)

    def test_semantic_validator_rejects_rehashed_market_fixture_identity(self):
        output_dir, manifest = self._bundle(
            "market-identity", [_data_row(1, "08-01 20:00")]
        )
        self._tamper_csv_and_rehash(
            output_dir,
            manifest,
            metadata_key="opening_market_research",
            fields=history_importer.MARKET_FIELDS,
            mutate=lambda rows: rows[0].update({"home_team": "伪造主队"}),
        )
        with self.assertRaisesRegex(
            history_importer.HistoryImportError,
            "fixture identity has no matching score row",
        ):
            history_importer.validate_bundle(output_dir)

    def test_dash_opening_market_sentinels_export_as_incomplete_blank_cells(self):
        row = _data_row(1, "08-01 20:00")
        first_bookmaker_offset = 15
        for market_offset in (0, 6, 12):
            start = first_bookmaker_offset + market_offset
            row[start : start + 3] = ["-", "-", "-"]
        workbook = self.base / "history.xlsx"
        _write_workbook(workbook, [row])
        output_dir = self.base / "bundle"

        manifest = history_importer.import_bundle(
            [workbook],
            output_dir,
            source_timezone="Asia/Shanghai",
            as_of_date=AS_OF_DATE,
        )
        market_path = output_dir / "brazil-serie-a-opening-markets.csv"
        with market_path.open(encoding="utf-8", newline="") as handle:
            market_rows = list(csv.DictReader(handle))
        missing = next(item for item in market_rows if item["bookmaker"] == "36")

        for field in (
            "home_odds",
            "draw_odds",
            "away_odds",
            "asian_home_price",
            "asian_line",
            "asian_away_price",
            "total_over_price",
            "total_line",
            "total_under_price",
        ):
            self.assertEqual(missing[field], "", field)
        self.assertEqual(missing["opening_1x2_complete"], "false")
        self.assertEqual(missing["opening_asian_complete"], "false")
        self.assertEqual(missing["opening_total_complete"], "false")
        completeness = manifest["leagues"][0]["bookmaker_opening_completeness"]["36"]
        self.assertEqual(completeness["opening_1x2"], {"rows": 0, "rate": 0.0})
        self.assertEqual(completeness["opening_asian"], {"rows": 0, "rate": 0.0})
        self.assertEqual(completeness["opening_total"], {"rows": 0, "rate": 0.0})


if __name__ == "__main__":
    unittest.main()

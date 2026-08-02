from __future__ import annotations

import csv
from datetime import date, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "history_importer.py"
SPEC = importlib.util.spec_from_file_location("soccer_history_importer", SCRIPT)
assert SPEC and SPEC.loader
history_importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history_importer)


SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOCUMENT_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
CLOSING_SENTINEL = "CLOSE_ONLY_SECRET"


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _xml_bytes(element: ET.Element) -> bytes:
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def _worksheet_xml(rows: list[list[object]], *, formula_ref: str | None = None) -> bytes:
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
            cell = ET.SubElement(
                row, f"{{{SPREADSHEET_NS}}}c", {"r": reference}
            )
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
) -> None:
    top = [None] * len(history_importer.EXPECTED_HEADERS)
    for column, value in history_importer.EXPECTED_TOP_HEADERS.items():
        top[column - 1] = value
    if top_header is not None:
        top = top_header
    lower = list(history_importer.EXPECTED_HEADERS)
    if lower_header is not None:
        lower = lower_header

    workbook = ET.Element(
        f"{{{SPREADSHEET_NS}}}workbook",
        {f"xmlns:r": DOCUMENT_REL_NS},
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
        {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"},
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
            _worksheet_xml([top, lower, *data_rows], formula_ref=formula_ref),
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
) -> list[object]:
    half_home, half_away = map(int, half_score.split("-"))
    full_home, full_away = map(int, full_score.split("-"))
    row: list[object] = [None] * len(history_importer.EXPECTED_HEADERS)
    row[:15] = [
        identifier,
        season,
        league,
        f"{league} 第{identifier}轮",
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

    def _import(self, rows: list[list[object]], **workbook_options):
        path = self.base / "history.xlsx"
        _write_workbook(path, rows, **workbook_options)
        return history_importer.import_workbook(
            path, source_timezone="Asia/Shanghai"
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
        self.assertTrue(
            all(row["competition_regime"] == "regular" for row in market_rows)
        )
        self.assertEqual(set(score_rows[0]), set(history_importer.SCORE_FIELDS))

    def test_japan_2026_vision_batch_marks_all_180_source_fixtures(self):
        start = date(2026, 2, 6)
        end = date(2026, 5, 24)
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

        summary, score_rows, market_rows = self._import(
            rows, sheet_name="日职"
        )

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
            {row["competition_regime"] for row in market_rows},
            {"2026_vision_regional"},
        )

    def test_japan_vision_date_bounds_are_inclusive_and_other_rows_are_regular(self):
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
                    "05-24 12:00",
                    season=2026,
                    home="戊",
                    away="己",
                    league="日职",
                ),
                _data_row(
                    4,
                    "05-25 12:00",
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

    def test_any_formula_is_rejected_even_when_a_cached_value_exists(self):
        with self.assertRaisesRegex(
            history_importer.HistoryImportError, "formulas are not allowed"
        ):
            self._import([_data_row(1, "08-01 20:00")], formula_ref="P3")

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

    def test_bundle_outputs_opening_markets_but_never_closing_prices(self):
        workbook = self.base / "history.xlsx"
        _write_workbook(workbook, [_data_row(1, "08-01 20:00")])
        output_dir = self.base / "bundle"

        manifest = history_importer.import_bundle(
            [workbook], output_dir, source_timezone="Asia/Shanghai"
        )
        score_path = output_dir / "brazil-serie-a-scores.csv"
        market_path = output_dir / "brazil-serie-a-opening-markets.csv"
        score_text = score_path.read_text(encoding="utf-8")
        market_text = market_path.read_text(encoding="utf-8")

        self.assertIn(CLOSING_SENTINEL, str(history_importer.read_xlsx_rows(workbook)[1]))
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
            [workbook], output_dir, source_timezone="Asia/Shanghai"
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
        league["score_dataset"]["sha256"] = "sha256:" + hashlib.sha256(
            score_path.read_bytes()
        ).hexdigest()
        league["competition_regimes"] = {"2026": {"regular": 1}}
        manifest["competition_regime_counts"] = [
            {
                "league_key": "japan_j1",
                "season": 2026,
                "competition_regime": "regular",
                "rows": 1,
            }
        ]
        manifest["bundle_hash"] = history_importer._canonical_manifest_hash(
            manifest
        )
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            history_importer.HistoryImportError,
            "competition_regime must be 2026_vision_regional",
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
            [workbook], output_dir, source_timezone="Asia/Shanghai"
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

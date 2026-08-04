#!/usr/bin/env python3
"""Render a simple, archive-bound post-match review card.

The command accepts only a reviewed history record and a match id.  It never
accepts recommendation, HT/FT, or score text from the caller.  All pre-match
content is read from ``settlement_basis``; HT/FT and score references are two
views of the same validated joint-path events returned by
``public_market_outlook``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html import escape
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence
import unicodedata


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPT_DIR))

import memory_store
import plain_text_formatter
import public_market_outlook


WIDTH = 1520
SIDE_MARGIN = 40
TOP_MARGIN = 28
TITLE_HEIGHT = 170
HEADER_HEIGHT = 70
FOOTER_HEIGHT = 170
MIN_ROW_HEIGHT = 178
TABLE_WIDTH = WIDTH - 2 * SIDE_MARGIN

COLUMNS = (
    ("编号", "identifier", 100),
    ("赛事", "league", 110),
    ("比赛", "match", 240),
    ("半场", "half_time_score", 100),
    ("全场", "final_score", 100),
    ("主推结算", "primary_settlement", 250),
    ("半全场参考", "htft_reference", 260),
    ("波胆参考", "score_reference", 280),
)

if sum(width for _label, _key, width in COLUMNS) != TABLE_WIDTH:
    raise RuntimeError("review card columns do not fill the table width")

HTFT_LABELS = {"H": "胜", "D": "平", "A": "负"}
RESULT_LABELS = {
    "win": "红",
    "half_win": "半红",
    "push": "走",
    "half_loss": "半黑",
    "loss": "黑",
}
FORBIDDEN_ELLIPSES = ("…", "...")
CELL_FONT_SIZES = (20, 22, 22, 22, 22, 21, 21, 21)

COLORS = {
    "page": "#fff8fa",
    "panel": "#ffffff",
    "header": "#df5c7b",
    "header_dark": "#ba244d",
    "row": "#fffdfd",
    "grid": "#efc9d3",
    "text": "#2b1d22",
    "muted": "#7c6570",
    "reference": "#168d88",
    "warning": "#c91543",
    "positive": "#b51f49",
    "negative": "#198d88",
}


class ReviewCardError(ValueError):
    """Raised when a reviewed archive cannot safely be rendered."""


@dataclass(frozen=True)
class JointEvent:
    rank: int
    htft: str
    htft_label: str
    score: str
    probability: float

    @property
    def percentage(self) -> float:
        return self.probability * 100.0


@dataclass(frozen=True)
class ReviewCard:
    identifier: str
    league: str
    home_team: str
    away_team: str
    half_time_score: str
    final_score: str
    primary_settlement: str
    primary_result: str | None
    settlement_stage: str
    settlement_archived_at: str
    settlement_hash: str
    reviewed_at: str
    events: tuple[JointEvent, ...]
    joint_status: str

    @property
    def match(self) -> str:
        return f"{self.home_team}\nvs {self.away_team}"

    @property
    def htft_reference(self) -> str:
        if not self.events:
            return "数据不足"
        lines = ["高方差·非推荐"]
        lines.extend(
            f"{event.rank}. {event.htft_label} {event.percentage:.1f}%"
            for event in self.events
        )
        return "\n".join(lines)

    @property
    def score_reference(self) -> str:
        if not self.events:
            return "数据不足"
        lines = ["高方差·非推荐"]
        lines.extend(
            f"{event.rank}. {event.score} {event.percentage:.1f}%"
            for event in self.events
        )
        return "\n".join(lines)

    @property
    def row_values(self) -> tuple[str, ...]:
        return (
            self.identifier,
            self.league,
            self.match,
            self.half_time_score,
            self.final_score,
            self.primary_settlement,
            self.htft_reference,
            self.score_reference,
        )


def _canonical_hash(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewCardError("settlement basis is not finite canonical JSON") from exc
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewCardError(f"{field} must be a non-empty string")
    text = value.strip()
    if any(token in text for token in FORBIDDEN_ELLIPSES):
        raise ReviewCardError(f"{field} contains a forbidden ellipsis")
    return text


def _optional_equal(
    basis: Mapping[str, Any], record: Mapping[str, Any], key: str
) -> None:
    if key in basis and basis.get(key) is not None and basis.get(key) != record.get(key):
        raise ReviewCardError(f"settlement_basis.{key} conflicts with reviewed record")


def _parse_score(value: Any, field: str) -> tuple[str, int, int]:
    text = _required_text(value, field)
    match = re.fullmatch(r"(\d+)-(\d+)", text)
    if match is None:
        raise ReviewCardError(f"{field} must use non-negative H-A score form")
    return text, int(match.group(1)), int(match.group(2))


def _parse_optional_score(
    value: Any, field: str
) -> tuple[str, int | None, int | None]:
    """Render an unavailable score honestly while validating any supplied value."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return "未取得", None, None
    return _parse_score(value, field)


def load_history_records(path: Path) -> dict[str, dict[str, Any]]:
    """Load history without accepting a caller-authored presentation payload."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewCardError(f"cannot read valid UTF-8 history from {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise ReviewCardError("history must be a JSON array")
    records: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ReviewCardError(f"history item {index} must be an object")
        match_id = str(item.get("match_id") or "").strip()
        if not match_id:
            raise ReviewCardError(f"history item {index} has no match_id")
        if match_id in records:
            raise ReviewCardError(f"history contains duplicate match_id {match_id}")
        records[match_id] = item
    return records


def _validate_settlement_binding(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("mode") != "prematch" or record.get("status") != "reviewed":
        raise ReviewCardError("review card requires a terminal reviewed prematch record")
    basis = record.get("settlement_basis")
    if not isinstance(basis, dict):
        raise ReviewCardError("reviewed record has no immutable settlement_basis")
    if basis.get("policy") != "latest_active_prematch_version":
        raise ReviewCardError("settlement_basis policy is not supported")
    if basis.get("grading_scope") not in {None, "primary_only"}:
        raise ReviewCardError("settlement_basis grading scope is not primary_only")
    stage = basis.get("analysis_stage")
    if stage not in {"initial", "lineup-check"}:
        raise ReviewCardError("settlement_basis analysis_stage is invalid")
    _required_text(basis.get("version_archived_at"), "settlement_basis.version_archived_at")

    for key in ("match_id", "home_team", "away_team", "kickoff"):
        _optional_equal(basis, record, key)
    if basis.get("fixture_id") is not None and str(basis.get("fixture_id")) != str(
        record.get("match_id")
    ):
        raise ReviewCardError("settlement_basis.fixture_id conflicts with reviewed record")

    basis_market = basis.get("primary_market")
    basis_pick = basis.get("primary_pick")
    if basis_market in {"", "none"}:
        basis_market = None
    if record.get("primary_market") in {"", "none"}:
        record_market = None
    else:
        record_market = record.get("primary_market")
    if basis_market != record_market or basis_pick != record.get("primary_pick"):
        raise ReviewCardError("reviewed primary conflicts with immutable settlement_basis")

    basis_result = basis.get("primary_result")
    record_result = record.get("primary_result")
    if basis_result != record_result:
        raise ReviewCardError("reviewed primary result conflicts with settlement_basis")

    has_primary = isinstance(basis_market, str) and isinstance(basis_pick, dict)
    if bool(basis.get("counts_toward_primary_record")) != has_primary:
        raise ReviewCardError("settlement_basis primary counting flag is inconsistent")
    if bool(record.get("counts_toward_primary_record")) != has_primary:
        raise ReviewCardError("reviewed primary counting flag is inconsistent")
    if has_primary and basis_result not in RESULT_LABELS:
        raise ReviewCardError("formal primary has no valid frozen settlement result")
    if not has_primary and (basis_market is not None or basis_pick is not None or basis_result is not None):
        raise ReviewCardError("no-primary settlement basis is inconsistent")
    return basis


def _probability(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewCardError(f"{field} must be a finite probability")
    probability = float(value)
    if not math.isfinite(probability) or not 0.0 < probability <= 1.0:
        raise ReviewCardError(f"{field} must be in (0, 1]")
    return probability


def _joint_events(record: dict[str, Any]) -> tuple[JointEvent, ...]:
    """Read one ordered event list; never pair independent market rankings."""
    try:
        artifact = memory_store.validated_joint_scenario_audit(record)
    except (TypeError, ValueError):
        return ()
    if not isinstance(artifact, Mapping):
        return ()
    try:
        outlook = public_market_outlook.build_public_market_outlook(artifact)
        block = outlook["joint_scenarios"]
        raw_items = block["display_items"]
        display_count = block["display_count"]
        if (
            not isinstance(block, Mapping)
            or block.get("status") != "high_variance_reference"
            or block.get("recommendation_eligible") is not False
            or block.get("counts_as_primary") is not False
            or not isinstance(raw_items, list)
            or len(raw_items) not in {2, 3}
            or display_count != len(raw_items)
        ):
            raise ReviewCardError("public joint display policy is invalid")

        events: list[JointEvent] = []
        previous_probability: float | None = None
        for rank, item in enumerate(raw_items, start=1):
            if not isinstance(item, Mapping):
                raise ReviewCardError("public joint display item is invalid")
            htft = str(item.get("htft") or "")
            score = str(item.get("score") or "")
            score_match = re.fullmatch(r"(\d+)-(\d+)", score)
            if htft not in {
                left + right
                for left in ("H", "D", "A")
                for right in ("H", "D", "A")
            } or score_match is None:
                raise ReviewCardError("public joint display event identity is invalid")
            if item.get("slot") != rank:
                raise ReviewCardError("public joint display order is not canonical")
            if (
                item.get("status") != "high_variance_reference"
                or item.get("recommendation_eligible") is not False
                or item.get("counts_toward_primary_record") is not False
                or item.get("odds_available") is not False
                or item.get("counts_as_primary") is not False
                or item.get("requires_bookmaker_odds") is not False
            ):
                raise ReviewCardError("public joint display event violates safety policy")
            home_goals = int(score_match.group(1))
            away_goals = int(score_match.group(2))
            if item.get("home_goals") != home_goals or item.get("away_goals") != away_goals:
                raise ReviewCardError("public joint display score fields conflict")
            probability = _probability(item.get("probability"), f"joint event {rank}")
            if previous_probability is not None and probability > previous_probability + 1e-12:
                raise ReviewCardError("public joint display events are not ranked")
            previous_probability = probability
            events.append(
                JointEvent(
                    rank=rank,
                    htft=htft,
                    htft_label=f"{HTFT_LABELS[htft[0]]}/{HTFT_LABELS[htft[1]]}",
                    score=score,
                    probability=probability,
                )
            )
        return tuple(events)
    except (
        KeyError,
        TypeError,
        ValueError,
        public_market_outlook.PublicMarketOutlookError,
        ReviewCardError,
    ):
        return ()


def build_card(record: dict[str, Any]) -> ReviewCard:
    basis = _validate_settlement_binding(record)
    identifier = _required_text(str(record.get("match_id") or ""), "match_id")
    home_team = _required_text(record.get("home_team"), "home_team")
    away_team = _required_text(record.get("away_team"), "away_team")
    half_score, half_home, half_away = _parse_optional_score(
        record.get("half_time_score"), "half_time_score"
    )
    final_score, final_home, final_away = _parse_score(record.get("final_score"), "final_score")
    if (
        half_home is not None
        and half_away is not None
        and (half_home > final_home or half_away > final_away)
    ):
        raise ReviewCardError("half-time score cannot exceed final score")

    primary = basis.get("primary_pick")
    if isinstance(primary, dict):
        context = dict(record)
        context.update(basis)
        pick_text = plain_text_formatter.format_pick(
            str(basis.get("primary_market")), primary, context
        )
        if pick_text == "无正式推荐" or not pick_text.strip():
            raise ReviewCardError("frozen formal primary cannot be rendered")
        primary_result = str(basis.get("primary_result"))
        primary_settlement = f"主推：{pick_text}\n结算：{RESULT_LABELS[primary_result]}"
    else:
        primary_result = None
        primary_settlement = "主推：无正式推荐\n（不结算、不计战绩）"

    league = plain_text_formatter.league_display_name(record)
    league = _required_text(league, "league")
    reviewed_at = _required_text(record.get("reviewed_at"), "reviewed_at")
    settlement_archived_at = _required_text(
        basis.get("version_archived_at"), "settlement_basis.version_archived_at"
    )
    events = _joint_events(record)
    card = ReviewCard(
        identifier=identifier,
        league=league,
        home_team=home_team,
        away_team=away_team,
        half_time_score=half_score,
        final_score=final_score,
        primary_settlement=primary_settlement,
        primary_result=primary_result,
        settlement_stage=str(basis["analysis_stage"]),
        settlement_archived_at=settlement_archived_at,
        settlement_hash=_canonical_hash(basis),
        reviewed_at=reviewed_at,
        events=events,
        joint_status=("validated_joint_paths" if events else "data_insufficient"),
    )
    _assert_no_ellipsis(visible_text(card))
    return card


def load_card(history_path: Path, match_id: str) -> ReviewCard:
    records = load_history_records(history_path)
    identifier = str(match_id).strip()
    record = records.get(identifier)
    if record is None:
        raise ReviewCardError(f"match_id {identifier} was not found")
    return build_card(record)


def joint_reference_note(card: ReviewCard) -> str:
    if card.joint_status == "validated_joint_paths":
        return "联合参考来自结算依据绑定的冻结联合路径；高方差，不计主推或战绩。"
    return "冻结结算依据未包含可验证联合路径；不从赛后结果或其他版本补填。"


def visible_text(card: ReviewCard) -> tuple[str, ...]:
    """Return every user-visible string for deterministic QA."""
    stage_label = "临场版" if card.settlement_stage == "lineup-check" else "初盘版"
    return (
        "赛后复盘",
        f"{card.league}｜{card.identifier}｜{stage_label}最终结算依据",
        *(label for label, _key, _width in COLUMNS),
        *card.row_values,
        joint_reference_note(card),
        f"赛前版本归档：{card.settlement_archived_at}",
        f"结算绑定：{card.settlement_hash}",
        "复盘用于校准分析，不代表未来收益",
    )


def _assert_no_ellipsis(values: Sequence[str]) -> None:
    for value in values:
        if any(token in str(value) for token in FORBIDDEN_ELLIPSES):
            raise ReviewCardError("review card output must not contain ellipses")


def _wrap_units(text: str, maximum_units: int) -> tuple[str, ...]:
    """Wrap all content without truncation or ellipsis insertion."""
    if maximum_units < 2:
        raise ReviewCardError("column is too narrow to wrap text")
    output: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        if not paragraph:
            output.append("")
            continue
        current: list[str] = []
        used = 0
        for character in paragraph:
            if unicodedata.east_asian_width(character) in {"W", "F"}:
                cost = 2
            elif character.isascii() and character.isalpha():
                # Latin letters can be as wide as a CJK glyph in Microsoft
                # YaHei (notably W/M).  Review cells use a fixed font size, so
                # budget them conservatively instead of letting a long club
                # name cross the next column.  Digits and punctuation retain a
                # one-unit cost so normal Titan IDs and scores stay on one line.
                cost = 2
            else:
                cost = 1
            if current and used + cost > maximum_units:
                output.append("".join(current))
                current = []
                used = 0
            current.append(character)
            used += cost
        if current:
            output.append("".join(current))
    return tuple(output or [""])


def _wrapped_cells(card: ReviewCard) -> tuple[tuple[str, ...], ...]:
    cells = []
    for value, (_label, _key, width), font_size in zip(
        card.row_values, COLUMNS, CELL_FONT_SIZES
    ):
        # A CJK glyph or Latin letter consumes two visual units and is roughly
        # one font-size wide.  Digits/punctuation consume one.  This
        # conservative budget leaves horizontal cell padding.
        cells.append(
            _wrap_units(value, max(2, int((width - 18) / (font_size / 2))))
        )
    return tuple(cells)


def _row_height(card: ReviewCard) -> int:
    maximum_lines = max(len(lines) for lines in _wrapped_cells(card))
    return max(MIN_ROW_HEIGHT, 34 + maximum_lines * 34)


def _card_height(card: ReviewCard) -> int:
    return TOP_MARGIN * 2 + TITLE_HEIGHT + HEADER_HEIGHT + _row_height(card) + FOOTER_HEIGHT


def _svg_multiline(
    x: float,
    center_y: float,
    lines: Sequence[str],
    *,
    font_size: int,
    color: str,
    weight: int = 500,
) -> str:
    line_height = font_size + 8
    first_y = center_y - (len(lines) - 1) * line_height / 2
    tspans = []
    for index, line in enumerate(lines):
        dy = "0" if index == 0 else str(line_height)
        tspans.append(f'<tspan x="{x:.1f}" dy="{dy}">{escape(line)}</tspan>')
    return (
        f'<text x="{x:.1f}" y="{first_y:.1f}" text-anchor="middle" '
        f'font-size="{font_size}" font-weight="{weight}" fill="{color}">'
        + "".join(tspans)
        + "</text>"
    )


def render_svg(card: ReviewCard) -> str:
    _assert_no_ellipsis(visible_text(card))
    height = _card_height(card)
    panel_height = height - TOP_MARGIN * 2
    table_y = TOP_MARGIN + TITLE_HEIGHT
    row_y = table_y + HEADER_HEIGHT
    row_height = _row_height(card)
    footer_y = row_y + row_height
    cells = _wrapped_cells(card)
    result_color = (
        COLORS["positive"]
        if card.primary_result in {"win", "half_win"}
        else COLORS["negative"]
        if card.primary_result in {"loss", "half_loss"}
        else COLORS["text"]
    )

    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}">',
        f'<rect width="{WIDTH}" height="{height}" fill="{COLORS["page"]}"/>',
        f'<rect x="{SIDE_MARGIN}" y="{TOP_MARGIN}" width="{TABLE_WIDTH}" height="{panel_height}" rx="22" fill="{COLORS["panel"]}" stroke="{COLORS["grid"]}"/>',
        '<style>text{font-family:"Microsoft YaHei","Noto Sans CJK SC","Arial Unicode MS",sans-serif}</style>',
        f'<text x="{SIDE_MARGIN + 30}" y="{TOP_MARGIN + 62}" font-size="48" font-weight="800" fill="{COLORS["text"]}">赛后复盘</text>',
        f'<text x="{SIDE_MARGIN + 30}" y="{TOP_MARGIN + 113}" font-size="27" font-weight="600" fill="{COLORS["header_dark"]}">{escape(card.league)}｜{escape(card.identifier)}｜{"临场版" if card.settlement_stage == "lineup-check" else "初盘版"}最终结算依据</text>',
        f'<rect x="{SIDE_MARGIN}" y="{table_y}" width="{TABLE_WIDTH}" height="{HEADER_HEIGHT}" fill="{COLORS["header"]}"/>',
        f'<rect x="{SIDE_MARGIN}" y="{row_y}" width="{TABLE_WIDTH}" height="{row_height}" fill="{COLORS["row"]}"/>',
    ]

    x = SIDE_MARGIN
    for index, ((label, _key, width), lines, font_size) in enumerate(
        zip(COLUMNS, cells, CELL_FONT_SIZES)
    ):
        center_x = x + width / 2
        pieces.append(
            _svg_multiline(
                center_x,
                table_y + HEADER_HEIGHT / 2,
                (label,),
                font_size=24,
                color="#ffffff",
                weight=700,
            )
        )
        color = result_color if index == 5 else COLORS["reference"] if index in {6, 7} else COLORS["text"]
        pieces.append(
            _svg_multiline(
                center_x,
                row_y + row_height / 2,
                lines,
                font_size=font_size,
                color=color,
                weight=700 if index in {5, 6, 7} else 500,
            )
        )
        if index:
            pieces.append(
                f'<line x1="{x}" y1="{table_y}" x2="{x}" y2="{footer_y}" stroke="{COLORS["grid"]}"/>'
            )
        x += width
    pieces.extend(
        [
            f'<line x1="{SIDE_MARGIN}" y1="{row_y}" x2="{SIDE_MARGIN + TABLE_WIDTH}" y2="{row_y}" stroke="{COLORS["grid"]}"/>',
            f'<line x1="{SIDE_MARGIN}" y1="{footer_y}" x2="{SIDE_MARGIN + TABLE_WIDTH}" y2="{footer_y}" stroke="{COLORS["grid"]}"/>',
            f'<text x="{SIDE_MARGIN + 30}" y="{footer_y + 43}" font-size="23" font-weight="700" fill="{COLORS["warning"]}">{escape(joint_reference_note(card))}</text>',
            f'<text x="{SIDE_MARGIN + 30}" y="{footer_y + 82}" font-size="20" fill="{COLORS["muted"]}">赛前版本归档：{escape(card.settlement_archived_at)}</text>',
            f'<text x="{SIDE_MARGIN + 30}" y="{footer_y + 115}" font-size="18" fill="{COLORS["muted"]}">结算绑定：{escape(card.settlement_hash)}</text>',
            f'<text x="{SIDE_MARGIN + 30}" y="{footer_y + 146}" font-size="20" fill="{COLORS["muted"]}">复盘用于校准分析，不代表未来收益</text>',
            "</svg>",
        ]
    )
    svg = "".join(pieces)
    _assert_no_ellipsis((svg,))
    return svg


def _hex_color(value: str) -> tuple[int, int, int]:
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def _font_candidates(bold: bool) -> tuple[str, ...]:
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    names = (
        ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "arialbd.ttf")
        if bold
        else ("msyh.ttc", "simhei.ttf", "simsun.ttc", "arial.ttf")
    )
    return tuple(str(windows / name) for name in names) + (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )


def _font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    for candidate in _font_candidates(bold):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_centered_lines(draw, box, lines: Sequence[str], *, font, fill) -> None:
    left, top, right, bottom = box
    spacing = 8
    heights = []
    for line in lines:
        bounds = draw.textbbox((0, 0), line or " ", font=font)
        heights.append(bounds[3] - bounds[1])
    total_height = sum(heights) + spacing * max(0, len(lines) - 1)
    y = top + (bottom - top - total_height) / 2
    for line, line_height in zip(lines, heights):
        bounds = draw.textbbox((0, 0), line or " ", font=font)
        width = bounds[2] - bounds[0]
        draw.text(((left + right - width) / 2, y), line, font=font, fill=fill)
        y += line_height + spacing


def render_png(card: ReviewCard) -> bytes:
    from io import BytesIO
    from PIL import Image, ImageDraw

    _assert_no_ellipsis(visible_text(card))
    height = _card_height(card)
    table_y = TOP_MARGIN + TITLE_HEIGHT
    row_y = table_y + HEADER_HEIGHT
    row_height = _row_height(card)
    footer_y = row_y + row_height
    cells = _wrapped_cells(card)
    image = Image.new("RGB", (WIDTH, height), _hex_color(COLORS["page"]))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (SIDE_MARGIN, TOP_MARGIN, WIDTH - SIDE_MARGIN, height - TOP_MARGIN),
        radius=22,
        fill=_hex_color(COLORS["panel"]),
        outline=_hex_color(COLORS["grid"]),
        width=2,
    )
    draw.text(
        (SIDE_MARGIN + 30, TOP_MARGIN + 24),
        "赛后复盘",
        font=_font(48, bold=True),
        fill=_hex_color(COLORS["text"]),
    )
    stage_label = "临场版" if card.settlement_stage == "lineup-check" else "初盘版"
    draw.text(
        (SIDE_MARGIN + 30, TOP_MARGIN + 91),
        f"{card.league}｜{card.identifier}｜{stage_label}最终结算依据",
        font=_font(27, bold=True),
        fill=_hex_color(COLORS["header_dark"]),
    )
    draw.rectangle(
        (SIDE_MARGIN, table_y, WIDTH - SIDE_MARGIN, table_y + HEADER_HEIGHT),
        fill=_hex_color(COLORS["header"]),
    )
    draw.rectangle(
        (SIDE_MARGIN, row_y, WIDTH - SIDE_MARGIN, footer_y),
        fill=_hex_color(COLORS["row"]),
    )
    result_color = (
        COLORS["positive"]
        if card.primary_result in {"win", "half_win"}
        else COLORS["negative"]
        if card.primary_result in {"loss", "half_loss"}
        else COLORS["text"]
    )
    x = SIDE_MARGIN
    for index, ((label, _key, width), lines, font_size) in enumerate(
        zip(COLUMNS, cells, CELL_FONT_SIZES)
    ):
        _draw_centered_lines(
            draw,
            (x, table_y, x + width, row_y),
            (label,),
            font=_font(24, bold=True),
            fill=(255, 255, 255),
        )
        color = result_color if index == 5 else COLORS["reference"] if index in {6, 7} else COLORS["text"]
        _draw_centered_lines(
            draw,
            (x + 8, row_y + 8, x + width - 8, footer_y - 8),
            lines,
            font=_font(font_size, bold=index in {5, 6, 7}),
            fill=_hex_color(color),
        )
        if index:
            draw.line((x, table_y, x, footer_y), fill=_hex_color(COLORS["grid"]), width=1)
        x += width
    draw.line(
        (SIDE_MARGIN, row_y, WIDTH - SIDE_MARGIN, row_y),
        fill=_hex_color(COLORS["grid"]),
        width=1,
    )
    draw.line(
        (SIDE_MARGIN, footer_y, WIDTH - SIDE_MARGIN, footer_y),
        fill=_hex_color(COLORS["grid"]),
        width=1,
    )
    footer_lines = (
        (joint_reference_note(card), 23, COLORS["warning"], True),
        (f"赛前版本归档：{card.settlement_archived_at}", 20, COLORS["muted"], False),
        (f"结算绑定：{card.settlement_hash}", 18, COLORS["muted"], False),
        ("复盘用于校准分析，不代表未来收益", 20, COLORS["muted"], False),
    )
    y = footer_y + 21
    for text, size, color, bold in footer_lines:
        draw.text(
            (SIDE_MARGIN + 30, y),
            text,
            font=_font(size, bold=bold),
            fill=_hex_color(color),
        )
        y += 34
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_file(history_path: Path, match_id: str, output_path: Path) -> Path:
    card = load_card(history_path, match_id)
    suffix = output_path.suffix.casefold()
    if suffix not in {".svg", ".png"}:
        raise ReviewCardError("output extension must be .svg or .png")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".svg":
        data = render_svg(card).encode("utf-8")
    else:
        data = render_png(card)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=output_path.parent, prefix=output_path.name + ".", delete=False
    ) as temporary:
        temporary.write(data)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = render_file(args.history, str(args.match_id), args.output)
    print(
        json.dumps(
            {"ok": True, "match_id": str(args.match_id), "output": str(output)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

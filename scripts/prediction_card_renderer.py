#!/usr/bin/env python3
"""Render a deterministic daily football prediction card from UTF-8 JSON.

SVG output uses only the Python standard library.  PNG/JPEG output is optional
and uses Pillow when it is installed.  The renderer deliberately distinguishes
formal recommendations from observations so a decorative marker cannot turn an
unsettled observation into a claimed primary pick.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html import escape
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence
import unicodedata


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPT_DIR))

import plain_text_formatter
import memory_store


WIDTH = 1520
SIDE_MARGIN = 40
TITLE_HEIGHT = 190
HEADER_HEIGHT = 70
ROW_HEIGHT = 72
FOOTER_HEIGHT = 170
TABLE_WIDTH = WIDTH - 2 * SIDE_MARGIN

COLUMNS = (
    ("编号", "id", 110),
    ("时间", "time", 100),
    ("赛事", "league", 120),
    ("主队 vs 客队", "match", 360),
    ("主推", "primary", 210),
    ("总进球", "total_goals", 150),
    ("半全场", "htft", 190),
    ("比分", "scores", 200),
)

REQUIRED_TOP_LEVEL = ("date", "title", "subtitle", "rows")
REQUIRED_ROW_FIELDS = (
    "id",
    "archive_match_id",
    "time",
    "league",
    "home_team",
    "away_team",
    "total_goals",
    "htft",
    "scores",
    "status",
)
ALLOWED_STATUSES = frozenset({"formal_primary", "observation", "no_bet"})
HTFT_RESULT_LABELS = {"H": "胜", "D": "平", "A": "负"}

COLORS = {
    "page": "#fff8fa",
    "panel": "#ffffff",
    "header": "#df5c7b",
    "header_dark": "#ba244d",
    "row_alt": "#fff3f6",
    "grid": "#efc9d3",
    "text": "#2b1d22",
    "muted": "#7c6570",
    "star": "#c91543",
    "observation": "#168d88",
    "no_bet": "#777077",
    "warning": "#a52b47",
}


@dataclass(frozen=True)
class CardRow:
    identifier: str
    time: str
    league: str
    match: str
    primary: str
    total_goals: str
    htft: str
    scores: str
    status: str
    star: bool


@dataclass(frozen=True)
class Card:
    date: str
    title: str
    subtitle: str
    rows: tuple[CardRow, ...]

    @property
    def height(self) -> int:
        return TITLE_HEIGHT + HEADER_HEIGHT + len(self.rows) * ROW_HEIGHT + FOOTER_HEIGHT


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _display_value(value: Any, field: str) -> str:
    """Normalize a printable scalar or a short sequence to one line."""
    if isinstance(value, str):
        result = value.strip()
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        if not value or any(not isinstance(item, (str, int, float)) for item in value):
            raise ValueError(f"{field} must be text or a non-empty scalar list")
        result = " / ".join(str(item).strip() for item in value)
    else:
        raise ValueError(f"{field} must be text or a non-empty scalar list")
    if not result or "\n" in result or "\r" in result:
        raise ValueError(f"{field} must be non-empty single-line text")
    return result


def _pair_value(value: Any, field: str) -> str:
    """Normalize a presentation pair without accepting slash-packed free text."""
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{field} must be an array containing exactly two values")
    if any(not isinstance(item, (str, int, float)) for item in value):
        raise ValueError(f"{field} must contain exactly two scalar values")
    normalized = [str(item).strip() for item in value]
    if any(not item or "\n" in item or "\r" in item for item in normalized):
        raise ValueError(f"{field} must contain exactly two non-empty single-line values")
    return " / ".join(normalized)


def load_history_records(path: Path) -> dict[str, Mapping[str, Any]]:
    """Load the immutable prediction archive used to authorize poster statuses."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read valid UTF-8 prediction history from {path}: {error}") from error
    if not isinstance(raw, list):
        raise ValueError("prediction history must be a JSON array")
    records: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(raw):
        if not isinstance(record, Mapping):
            raise ValueError(f"prediction history item {index} must be a JSON object")
        match_id = record.get("match_id")
        if not isinstance(match_id, str) or not match_id.strip():
            raise ValueError(f"prediction history item {index} has no valid match_id")
        if match_id in records:
            raise ValueError(f"prediction history contains duplicate match_id {match_id}")
        records[match_id] = record
    return records


def _archived_primary(record: Mapping[str, Any], prefix: str) -> str | None:
    primary = record.get("primary_pick")
    market = record.get("primary_market")
    if primary is None and market in {None, "", "none"}:
        return None
    if not isinstance(primary, dict) or not isinstance(market, str) or market in {"", "none"}:
        raise ValueError(f"{prefix} archive has inconsistent primary_market/primary_pick")
    label = plain_text_formatter.format_pick(market, primary, dict(record))
    if not label or label == "无正式推荐":
        raise ValueError(f"{prefix} archive primary cannot be rendered")
    return label


def _archived_observation_labels(
    record: Mapping[str, Any], prefix: str
) -> tuple[str, ...]:
    """Return only best-observation labels from validated candidate audits."""
    raw_audits = record.get("candidate_audits")
    if not isinstance(raw_audits, list):
        return ()
    labels: list[str] = []
    for audit in raw_audits:
        if not isinstance(audit, dict):
            continue
        try:
            valid = memory_store.validated_observation_audit(audit)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            continue
        market: str | None = None
        candidate: dict[str, Any] | None = None
        if audit.get("kind") == memory_store.CORNER_OBSERVATION_KIND:
            best = audit.get("best_observation")
            if isinstance(best, dict):
                market = str(best.get("market") or "")
                candidate = best
        elif audit.get("market") == "htft":
            top_two = audit.get("top_two")
            if isinstance(top_two, list) and top_two and isinstance(top_two[0], dict):
                market = "htft"
                candidate = top_two[0]
        if not market or candidate is None:
            continue
        if market == "htft":
            selection = str(candidate.get("selection") or "").upper()
            if len(selection) != 2 or any(
                code not in HTFT_RESULT_LABELS for code in selection
            ):
                continue
            label = "/".join(HTFT_RESULT_LABELS[code] for code in selection)
            label += plain_text_formatter.price(candidate.get("odds"))
        else:
            label = plain_text_formatter.format_pick(
                market, candidate, dict(record)
            )
        if label and label != "无正式推荐" and label not in labels:
            labels.append(label)
    return tuple(labels)


def validate_payload(
    payload: Mapping[str, Any],
    archived_records: Mapping[str, Mapping[str, Any]] | None = None,
) -> Card:
    """Validate and normalize the input document."""
    if not isinstance(payload, Mapping):
        raise ValueError("input must be a JSON object")
    missing = [field for field in REQUIRED_TOP_LEVEL if field not in payload]
    if missing:
        raise ValueError(f"missing top-level fields: {', '.join(missing)}")

    date = _required_text(payload["date"], "date")
    title = _required_text(payload["title"], "title")
    subtitle = _required_text(payload["subtitle"], "subtitle")
    raw_rows = payload["rows"]
    if not isinstance(raw_rows, list):
        raise ValueError("rows must be a JSON array")

    rows: list[CardRow] = []
    for index, raw in enumerate(raw_rows):
        prefix = f"rows[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{prefix} must be a JSON object")
        missing_row = [field for field in REQUIRED_ROW_FIELDS if field not in raw]
        if missing_row:
            raise ValueError(f"{prefix} missing fields: {', '.join(missing_row)}")
        status = raw["status"]
        if not isinstance(status, str) or status not in ALLOWED_STATUSES:
            raise ValueError(
                f"{prefix}.status must be one of: " + ", ".join(sorted(ALLOWED_STATUSES))
            )
        if "star" in raw:
            raise ValueError(
                f"{prefix}.star must not be supplied; it is derived from the archived primary"
            )
        if archived_records is None:
            raise ValueError("prediction history is required to render recommendation status")
        archive_match_id = _required_text(
            raw["archive_match_id"], f"{prefix}.archive_match_id"
        )
        archived = archived_records.get(archive_match_id)
        if archived is None:
            raise ValueError(f"{prefix} archive_match_id {archive_match_id} was not found")
        if archived.get("mode") != "prematch" or archived.get("status") != "pending":
            raise ValueError(f"{prefix} must bind to an active pending prematch archive")

        home = _required_text(raw["home_team"], f"{prefix}.home_team")
        away = _required_text(raw["away_team"], f"{prefix}.away_team")
        if archived.get("home_team") != home or archived.get("away_team") != away:
            raise ValueError(f"{prefix} teams do not match the archived active fixture")
        archived_primary = _archived_primary(archived, prefix)

        if status == "formal_primary":
            if archived_primary is None:
                raise ValueError(f"{prefix} cannot be formal_primary without an archived primary_pick")
            supplied_primary = raw.get("primary")
            if supplied_primary is not None and _display_value(
                supplied_primary, f"{prefix}.primary"
            ) != archived_primary:
                raise ValueError(f"{prefix}.primary does not match the archived active primary")
            primary = archived_primary
            star = True
        else:
            if archived_primary is not None:
                raise ValueError(
                    f"{prefix}.{status} conflicts with an archived active formal primary"
                )
            star = False
            primary = _display_value(raw.get("primary", "无正式推荐"), f"{prefix}.primary")
        if status == "no_bet":
            primary = "无正式推荐"
        elif status == "observation":
            authorized_labels = _archived_observation_labels(archived, prefix)
            if not authorized_labels:
                raise ValueError(
                    f"{prefix} cannot be observation without validated candidate_audits"
                )
            if primary not in authorized_labels:
                raise ValueError(
                    f"{prefix}.primary does not match an archived best observation"
                )
            primary = f"◇ {primary}"
        rows.append(
            CardRow(
                identifier=_required_text(raw["id"], f"{prefix}.id"),
                time=_required_text(raw["time"], f"{prefix}.time"),
                league=_required_text(raw["league"], f"{prefix}.league"),
                match=f"{home} vs {away}",
                primary=primary,
                total_goals=_display_value(raw["total_goals"], f"{prefix}.total_goals"),
                htft=_pair_value(raw["htft"], f"{prefix}.htft"),
                scores=_pair_value(raw["scores"], f"{prefix}.scores"),
                status=status,
                star=star,
            )
        )
    return Card(date=date, title=title, subtitle=subtitle, rows=tuple(rows))


def _visual_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _truncate(text: str, max_units: int) -> str:
    if _visual_width(text) <= max_units:
        return text
    output: list[str] = []
    used = 0
    for char in text:
        cost = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + cost > max_units - 2:
            break
        output.append(char)
        used += cost
    return "".join(output) + "…"


def _row_values(row: CardRow) -> tuple[str, ...]:
    return (
        row.identifier,
        row.time,
        row.league,
        row.match,
        row.primary,
        row.total_goals,
        row.htft,
        row.scores,
    )


def render_svg(card: Card) -> str:
    """Return a standalone SVG document."""
    height = card.height
    table_top = TITLE_HEIGHT
    rows_top = table_top + HEADER_HEIGHT
    table_bottom = rows_top + len(card.rows) * ROW_HEIGHT
    formal_count = sum(row.status == "formal_primary" for row in card.rows)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}">',
        f'<rect width="{WIDTH}" height="{height}" fill="{COLORS["page"]}"/>',
        f'<rect x="{SIDE_MARGIN}" y="28" width="{TABLE_WIDTH}" height="{height - 56}" rx="20" fill="{COLORS["panel"]}" stroke="{COLORS["grid"]}"/>',
        f'<text x="72" y="92" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="48" font-weight="700" fill="{COLORS["text"]}">{escape(_truncate(card.title, 34))}</text>',
        f'<text x="74" y="142" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="25" fill="{COLORS["header_dark"]}">{escape(_truncate(card.date + "｜" + card.subtitle, 76))}</text>',
        f'<rect x="{WIDTH - 202}" y="60" width="126" height="54" rx="14" fill="{COLORS["header"]}"/>',
        f'<text x="{WIDTH - 139}" y="96" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="23" font-weight="700" fill="#ffffff">{len(card.rows)} 场</text>',
        f'<rect x="{SIDE_MARGIN}" y="{table_top}" width="{TABLE_WIDTH}" height="{HEADER_HEIGHT}" fill="{COLORS["header"]}"/>',
    ]

    x = SIDE_MARGIN
    for label, _key, width in COLUMNS:
        parts.append(
            f'<text x="{x + width / 2:g}" y="{table_top + 45}" text-anchor="middle" '
            'font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" '
            f'font-size="23" font-weight="700" fill="#ffffff">{escape(label)}</text>'
        )
        x += width

    unit_limits = (12, 10, 12, 34, 20, 16, 20, 22)
    for row_index, row in enumerate(card.rows):
        y = rows_top + row_index * ROW_HEIGHT
        if row_index % 2:
            parts.append(
                f'<rect x="{SIDE_MARGIN}" y="{y}" width="{TABLE_WIDTH}" height="{ROW_HEIGHT}" fill="{COLORS["row_alt"]}"/>'
            )
        x = SIDE_MARGIN
        for column_index, ((_, key, width), value) in enumerate(zip(COLUMNS, _row_values(row))):
            fill = COLORS["text"]
            weight = "600" if key in {"time", "primary"} else "500"
            shown = _truncate(value, unit_limits[column_index])
            if key == "primary":
                if row.status == "observation":
                    fill = COLORS["observation"]
                elif row.status == "no_bet":
                    fill = COLORS["no_bet"]
            parts.append(
                f'<text x="{x + width / 2:g}" y="{y + 45}" text-anchor="middle" '
                'font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" '
                f'font-size="21" font-weight="{weight}" fill="{fill}">{escape(shown)}'
                + (f'<tspan fill="{COLORS["star"]}"> ★</tspan>' if key == "primary" and row.star else "")
                + '</text>'
            )
            x += width

    # Grid is drawn after fills so every boundary remains crisp.
    x = SIDE_MARGIN
    for _label, _key, width in COLUMNS[:-1]:
        x += width
        parts.append(
            f'<line x1="{x}" y1="{table_top}" x2="{x}" y2="{table_bottom}" stroke="{COLORS["grid"]}"/>'
        )
    for index in range(len(card.rows) + 1):
        y = rows_top + index * ROW_HEIGHT
        parts.append(
            f'<line x1="{SIDE_MARGIN}" y1="{y}" x2="{SIDE_MARGIN + TABLE_WIDTH}" y2="{y}" stroke="{COLORS["grid"]}"/>'
        )
    parts.append(
        f'<rect x="{SIDE_MARGIN}" y="{table_top}" width="{TABLE_WIDTH}" height="{table_bottom - table_top}" fill="none" stroke="{COLORS["grid"]}"/>'
    )

    footer_y = table_bottom + 48
    parts.extend(
        [
            f'<text x="72" y="{footer_y}" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="21" font-weight="600" fill="{COLORS["star"]}">★ 正式主推中的最高信心方向</text>',
            f'<text x="460" y="{footer_y}" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="21" font-weight="600" fill="{COLORS["observation"]}">◇ 观察方向（不计主推）</text>',
            f'<text x="{WIDTH - 76}" y="{footer_y}" text-anchor="end" font-family="Microsoft YaHei, sans-serif" font-size="20" fill="{COLORS["muted"]}">正式主推 {formal_count} 场</text>',
            f'<line x1="72" y1="{footer_y + 27}" x2="{WIDTH - 72}" y2="{footer_y + 27}" stroke="{COLORS["grid"]}"/>',
            f'<text x="72" y="{footer_y + 67}" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="20" font-weight="600" fill="{COLORS["warning"]}">提示：仅供比赛分析与模型复盘，不承诺收益；请理性参考。</text>',
            f'<text x="72" y="{footer_y + 104}" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="17" fill="{COLORS["muted"]}">赛前信息、首发、伤停与盘口变化可能改变结论；以临场最终有效版本为准。</text>',
            '</svg>',
        ]
    )
    return "\n".join(parts) + "\n"


def _font_candidates() -> tuple[Path, ...]:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    return (
        windows / "msyh.ttc",
        windows / "msyhbd.ttc",
        windows / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )


def _load_font(size: int, *, bold: bool = False):
    try:
        from PIL import ImageFont
    except ImportError as error:  # pragma: no cover - exercised by CLI environments
        raise RuntimeError("PNG/JPEG output requires Pillow") from error
    candidates = list(_font_candidates())
    if bold:
        windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates = [windows / "msyhbd.ttc", windows / "simhei.ttf"] + candidates
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _pil_center(draw: Any, box: tuple[int, int, int, int], text: str, font: Any, fill: str) -> None:
    left, top, right, bottom = box
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.text(
        ((left + right - width) / 2, (top + bottom - height) / 2 - bounds[1]),
        text,
        font=font,
        fill=fill,
    )


def render_raster(card: Card, output_format: str) -> bytes:
    """Render PNG or JPEG bytes using Pillow."""
    try:
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError("PNG/JPEG output requires Pillow") from error
    from io import BytesIO

    image = Image.new("RGB", (WIDTH, card.height), COLORS["page"])
    draw = ImageDraw.Draw(image)
    title_font = _load_font(48, bold=True)
    subtitle_font = _load_font(25)
    header_font = _load_font(23, bold=True)
    cell_font = _load_font(21)
    cell_bold = _load_font(21, bold=True)
    footer_font = _load_font(20)
    small_font = _load_font(17)

    draw.rounded_rectangle(
        (SIDE_MARGIN, 28, SIDE_MARGIN + TABLE_WIDTH, card.height - 28),
        radius=20,
        fill=COLORS["panel"],
        outline=COLORS["grid"],
    )
    draw.text((72, 48), _truncate(card.title, 34), font=title_font, fill=COLORS["text"])
    draw.text(
        (74, 112),
        _truncate(card.date + "｜" + card.subtitle, 76),
        font=subtitle_font,
        fill=COLORS["header_dark"],
    )
    draw.rounded_rectangle((WIDTH - 202, 60, WIDTH - 76, 114), radius=14, fill=COLORS["header"])
    _pil_center(draw, (WIDTH - 202, 60, WIDTH - 76, 114), f"{len(card.rows)} 场", header_font, "#ffffff")

    table_top = TITLE_HEIGHT
    rows_top = table_top + HEADER_HEIGHT
    table_bottom = rows_top + len(card.rows) * ROW_HEIGHT
    draw.rectangle((SIDE_MARGIN, table_top, SIDE_MARGIN + TABLE_WIDTH, rows_top), fill=COLORS["header"])
    x = SIDE_MARGIN
    for label, _key, width in COLUMNS:
        _pil_center(draw, (x, table_top, x + width, rows_top), label, header_font, "#ffffff")
        x += width

    limits = (12, 10, 12, 34, 20, 16, 20, 22)
    for row_index, row in enumerate(card.rows):
        y = rows_top + row_index * ROW_HEIGHT
        if row_index % 2:
            draw.rectangle((SIDE_MARGIN, y, SIDE_MARGIN + TABLE_WIDTH, y + ROW_HEIGHT), fill=COLORS["row_alt"])
        x = SIDE_MARGIN
        for column_index, ((_, key, width), value) in enumerate(zip(COLUMNS, _row_values(row))):
            shown = _truncate(value, limits[column_index])
            fill = COLORS["text"]
            font = cell_bold if key in {"time", "primary"} else cell_font
            if key == "primary" and row.status == "observation":
                fill = COLORS["observation"]
            elif key == "primary" and row.status == "no_bet":
                fill = COLORS["no_bet"]
            if key == "primary" and row.star:
                primary_bounds = draw.textbbox((0, 0), shown, font=font)
                star_bounds = draw.textbbox((0, 0), " ★", font=font)
                total_width = primary_bounds[2] - primary_bounds[0] + star_bounds[2] - star_bounds[0]
                start_x = x + (width - total_width) / 2
                baseline_y = y + (ROW_HEIGHT - (primary_bounds[3] - primary_bounds[1])) / 2 - primary_bounds[1]
                draw.text((start_x, baseline_y), shown, font=font, fill=fill)
                draw.text((start_x + primary_bounds[2] - primary_bounds[0], baseline_y), " ★", font=font, fill=COLORS["star"])
            else:
                _pil_center(draw, (x, y, x + width, y + ROW_HEIGHT), shown, font, fill)
            x += width

    x = SIDE_MARGIN
    for _label, _key, width in COLUMNS[:-1]:
        x += width
        draw.line((x, table_top, x, table_bottom), fill=COLORS["grid"])
    for index in range(len(card.rows) + 1):
        y = rows_top + index * ROW_HEIGHT
        draw.line((SIDE_MARGIN, y, SIDE_MARGIN + TABLE_WIDTH, y), fill=COLORS["grid"])
    draw.rectangle((SIDE_MARGIN, table_top, SIDE_MARGIN + TABLE_WIDTH, table_bottom), outline=COLORS["grid"])

    footer_y = table_bottom + 32
    draw.text((72, footer_y), "★ 正式主推中的最高信心方向", font=footer_font, fill=COLORS["star"])
    draw.text((460, footer_y), "◇ 观察方向（不计主推）", font=footer_font, fill=COLORS["observation"])
    formal_count = sum(row.status == "formal_primary" for row in card.rows)
    count_text = f"正式主推 {formal_count} 场"
    count_width = draw.textbbox((0, 0), count_text, font=footer_font)[2]
    draw.text((WIDTH - 76 - count_width, footer_y), count_text, font=footer_font, fill=COLORS["muted"])
    draw.line((72, footer_y + 45, WIDTH - 72, footer_y + 45), fill=COLORS["grid"])
    draw.text((72, footer_y + 66), "提示：仅供比赛分析与模型复盘，不承诺收益；请理性参考。", font=footer_font, fill=COLORS["warning"])
    draw.text((72, footer_y + 105), "赛前信息、首发、伤停与盘口变化可能改变结论；以临场最终有效版本为准。", font=small_font, fill=COLORS["muted"])

    buffer = BytesIO()
    normalized = output_format.upper()
    if normalized == "JPG":
        normalized = "JPEG"
    save_options: dict[str, Any] = {}
    if normalized == "PNG":
        save_options = {"compress_level": 9, "optimize": False}
    elif normalized == "JPEG":
        save_options = {"quality": 92, "subsampling": 0, "optimize": False, "progressive": False}
    image.save(buffer, format=normalized, **save_options)
    return buffer.getvalue()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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


def render_file(input_path: Path, output_path: Path, history_path: Path) -> Path:
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read valid UTF-8 JSON from {input_path}: {error}") from error
    archived_records = load_history_records(history_path)
    card = validate_payload(payload, archived_records)
    suffix = output_path.suffix.lower()
    if suffix == ".svg":
        data = render_svg(card).encode("utf-8")
    elif suffix in {".png", ".jpg", ".jpeg"}:
        data = render_raster(card, suffix[1:])
    else:
        raise ValueError("output extension must be .svg, .png, .jpg, or .jpeg")
    _atomic_write(output_path, data)
    return output_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a daily football prediction card")
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 JSON input")
    parser.add_argument(
        "--history",
        required=True,
        type=Path,
        help="archived .codex/soccer-predict/history.json used to authorize row status",
    )
    parser.add_argument("--output", required=True, type=Path, help=".svg, .png, .jpg, or .jpeg output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        destination = render_file(
            arguments.input.resolve(),
            arguments.output.resolve(),
            arguments.history.resolve(),
        )
    except (ValueError, RuntimeError) as error:
        raise SystemExit(f"error: {error}") from error
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Render concise, archive-bound football prediction cards.

Initial and lineup-check cards deliberately use the same eight-column layout.
Every market value is reconstructed from the selected immutable prediction
version; callers cannot inject derived picks.  Long content is wrapped and the
font is reduced when necessary.  Content is never replaced by an ellipsis.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from html import escape
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
PANEL_VERTICAL_MARGIN = 28
TITLE_HEIGHT = 190
HEADER_HEIGHT = 70
ROW_HEIGHT = 132
FOOTER_HEIGHT = 190
TABLE_WIDTH = WIDTH - 2 * SIDE_MARGIN

# Keep this layout intentionally close to the compact card the user selected.
# Widths sum to TABLE_WIDTH exactly.
COLUMNS = (
    ("编号", "id", 100),
    ("时间", "time", 90),
    ("赛事", "league", 110),
    ("主队 vs 客队", "match", 320),
    ("主推", "primary", 250),
    ("总进球", "total_goals", 170),
    ("半全场", "htft", 220),
    ("波胆", "scores", 180),
)

# Visual-width wrapping limits.  A CJK glyph counts as two units and Latin
# glyphs use a conservative 1.5-unit estimate.  The identifier column allows a
# normal Titan match ID to stay on one line; genuinely wider labels still wrap
# and are fitted against their measured Pillow bounds.
CELL_WRAP_UNITS = (14, 8, 12, 30, 24, 18, 22, 18)

REQUIRED_TOP_LEVEL = ("rows",)
ARCHIVE_DERIVED_HEADER_FIELDS = frozenset({"date", "title", "subtitle"})
REQUIRED_ROW_FIELDS = (
    "id",
    "archive_match_id",
    "archive_stage",
    "archive_version_hash",
    "time",
    "league",
    "home_team",
    "away_team",
    "status",
)
ARCHIVE_DERIVED_ROW_FIELDS = frozenset(
    {
        "one_x_two",
        "win_draw_loss",
        "total_goals",
        "htft",
        "scores",
        "joint_scenarios",
        "joint_top_two",
        "derived",
    }
)
ALLOWED_STATUSES = frozenset({"formal_primary", "observation", "no_bet"})
HTFT_RESULT_LABELS = {"H": "胜", "D": "平", "A": "负"}
FORBIDDEN_METADATA = re.compile(r"[★◇]|主推|推荐|稳胆|必中|必红")
FORBIDDEN_ELLIPSES = ("…", "...")

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

FOOTER_SOURCE_NOTE = (
    "总进球取冻结联合第1名比分映射；半全场与波胆按联合 Top 2 配对，不展示独立榜单或第三项。"
)


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


def _safe_metadata_text(
    value: Any,
    field: str,
    *,
    max_length: int,
    max_visual_units: int | None = None,
) -> str:
    result = _required_text(value, field)
    if "\n" in result or "\r" in result:
        raise ValueError(f"{field} must be single-line text")
    if len(result) > max_length:
        raise ValueError(f"{field} is too long for the fixed card layout")
    if max_visual_units is not None and _visual_width(result) > max_visual_units:
        raise ValueError(f"{field} is too wide for the fixed card layout")
    if FORBIDDEN_METADATA.search(result) or plain_text_formatter.MARKET_DIRECTION_TEXT.search(
        result
    ):
        raise ValueError(f"{field} must not contain recommendation markers")
    if any(token in result for token in FORBIDDEN_ELLIPSES):
        raise ValueError(f"{field} must not contain an ellipsis")
    return result


def _display_value(value: Any, field: str) -> str:
    """Normalize a printable scalar or short sequence without hiding content."""
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


def load_history_records(path: Path) -> dict[str, Mapping[str, Any]]:
    """Load the immutable prediction archive used to authorize card content."""
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
    """Return only fully qualified, independently validated observations."""
    del prefix  # Kept in the signature for stable caller error context.
    return plain_text_formatter.qualified_observation_references(dict(record))


def archive_version_hash(version: Mapping[str, Any]) -> str:
    """Return the deterministic public binding for one archived version."""
    snapshot = memory_store.revision_snapshot(dict(version))
    return memory_store.canonical_prediction_hash(memory_store.snapshot_payload(snapshot))


def _select_archived_version(
    record: Mapping[str, Any], stage: str, prefix: str
) -> dict[str, Any]:
    if stage not in {"initial", "lineup-check"}:
        raise ValueError(f"{prefix}.archive_stage must be initial or lineup-check")
    try:
        selected = plain_text_formatter.select_version(dict(record), stage)
    except ValueError as exc:
        raise ValueError(f"{prefix} cannot bind archive_stage={stage}: {exc}") from exc
    return plain_text_formatter.merged_version(dict(record), selected)


def _expected_kickoff_time(version: Mapping[str, Any]) -> str:
    rendered = plain_text_formatter.format_time(
        version.get("kickoff"), str(version.get("user_timezone") or "Asia/Tokyo")
    )
    match = re.search(r"\b\d{4}-\d{2}-\d{2} (\d{2}:\d{2})\b", rendered)
    if match is None:
        raise ValueError("archived fixture kickoff cannot be rendered as local time")
    return match.group(1)


def _expected_kickoff_date(version: Mapping[str, Any]) -> str:
    rendered = plain_text_formatter.format_time(
        version.get("kickoff"), str(version.get("user_timezone") or "Asia/Tokyo")
    )
    match = re.search(r"\b(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}\b", rendered)
    if match is None:
        raise ValueError("archived fixture kickoff date cannot be rendered")
    return match.group(1)


def _joint_artifact_display(
    record: Mapping[str, Any],
) -> tuple[str, str, str]:
    """Return display values from one validated artifact, or fail closed as a unit.

    The single goal-range value is projected from frozen joint rank 1.  The
    HT/FT and score columns are the same two frozen joint events.  No
    independently ranked marginal may occupy a compact scenario field.
    """

    insufficient = ("数据不足", "数据不足", "数据不足")
    try:
        artifact = memory_store.validated_joint_scenario_audit(record)
    except (TypeError, ValueError):
        return insufficient
    if not isinstance(artifact, Mapping):
        return insufficient

    try:
        public = public_market_outlook.build_public_market_outlook(artifact)
        scenario_block = public["joint_scenarios"]
        scenario_items = scenario_block["items"]
        if (
            not isinstance(scenario_items, list)
            or len(scenario_items) != 2
            or scenario_block.get("display_count") != 2
            or scenario_block.get("display_policy")
            != public_market_outlook.JOINT_DISPLAY_POLICY
        ):
            raise ValueError("joint scenarios must contain the global joint Top 2")
        rank_one_goal_range: str | None = None
        htft_lines: list[str] = []
        seen_events: set[tuple[str, str]] = set()
        score_lines: list[str] = []
        previous_probability: float | None = None
        for rank, item in enumerate(scenario_items, start=1):
            if item.get("slot") != rank:
                raise ValueError("invalid joint display slot")
            htft_code = str(item["htft"])
            if len(htft_code) != 2 or any(code not in HTFT_RESULT_LABELS for code in htft_code):
                raise ValueError("invalid HT/FT display code")
            score = str(item["score"])
            score_match = re.fullmatch(r"(\d+)-(\d+)", score)
            if score_match is None:
                raise ValueError("invalid joint display score")
            home_goals = int(score_match.group(1))
            away_goals = int(score_match.group(2))
            scenario_total_goals = home_goals + away_goals
            expected_goal_range = (
                "0-1" if scenario_total_goals <= 1 else
                "2-3" if scenario_total_goals <= 3 else
                "4-6" if scenario_total_goals <= 6 else
                "7+"
            )
            if item.get("total_goals") != scenario_total_goals:
                raise ValueError("joint display total-goals field conflicts with score")
            if item.get("goal_range_code") != expected_goal_range:
                raise ValueError("joint display goal range conflicts with score")
            goal_range_label = str(item.get("goal_range_label") or "")
            if goal_range_label != f"{expected_goal_range}球":
                raise ValueError("joint display goal-range label is invalid")
            full_result = "H" if home_goals > away_goals else "A" if home_goals < away_goals else "D"
            if htft_code[1] != full_result:
                raise ValueError("joint display score conflicts with HT/FT result")
            event_identity = (htft_code, score)
            if event_identity in seen_events:
                raise ValueError("joint Top 2 events must be distinct")
            seen_events.add(event_identity)
            percentage = float(item["percentage"])
            if not math.isfinite(percentage) or percentage <= 0.0:
                raise ValueError("invalid joint display probability")
            probability = percentage / 100.0
            if previous_probability is not None and probability > previous_probability:
                raise ValueError("joint Top 2 must be probability-ranked")
            previous_probability = probability
            label = "".join(HTFT_RESULT_LABELS[code] for code in htft_code)
            if rank == 1:
                rank_one_goal_range = goal_range_label
            htft_lines.append(label)
            score_lines.append(f"{score} {percentage:.1f}%")
        if rank_one_goal_range is None:
            raise ValueError("joint rank-1 goal range is unavailable")
        return rank_one_goal_range, "\n".join(htft_lines), "\n".join(score_lines)
    except (
        KeyError,
        TypeError,
        ValueError,
        public_market_outlook.PublicMarketOutlookError,
    ):
        return insufficient


def validate_payload(
    payload: Mapping[str, Any],
    archived_records: Mapping[str, Mapping[str, Any]] | None = None,
) -> Card:
    """Validate input and rebuild every displayed market from the archive."""
    if not isinstance(payload, Mapping):
        raise ValueError("input must be a JSON object")
    missing = [field for field in REQUIRED_TOP_LEVEL if field not in payload]
    if missing:
        raise ValueError(f"missing top-level fields: {', '.join(missing)}")
    supplied_headers = sorted(ARCHIVE_DERIVED_HEADER_FIELDS.intersection(payload))
    if supplied_headers:
        raise ValueError(
            "header metadata is archive-derived and must not be supplied: "
            + ", ".join(supplied_headers)
        )
    raw_rows = payload["rows"]
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("rows must be a non-empty JSON array")

    rows: list[CardRow] = []
    seen_match_ids: set[str] = set()
    archive_stages: set[str] = set()
    kickoff_dates: set[str] = set()
    archive_match_ids: list[str] = []
    for index, raw in enumerate(raw_rows):
        prefix = f"rows[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{prefix} must be a JSON object")
        supplied_derived = sorted(ARCHIVE_DERIVED_ROW_FIELDS.intersection(raw))
        if supplied_derived:
            raise ValueError(
                f"{prefix} archive-derived fields must not be supplied: "
                + ", ".join(supplied_derived)
            )
        missing_row = [field for field in REQUIRED_ROW_FIELDS if field not in raw]
        if missing_row:
            raise ValueError(f"{prefix} missing fields: {', '.join(missing_row)}")
        status = raw["status"]
        if not isinstance(status, str) or status not in ALLOWED_STATUSES:
            raise ValueError(
                f"{prefix}.status must be one of: " + ", ".join(sorted(ALLOWED_STATUSES))
            )
        if "star" in raw:
            raise ValueError(f"{prefix}.star must not be supplied; it is archive-derived")
        if archived_records is None:
            raise ValueError("prediction history is required to render recommendation status")

        archive_match_id = _required_text(raw["archive_match_id"], f"{prefix}.archive_match_id")
        archived = archived_records.get(archive_match_id)
        if archived is None:
            raise ValueError(f"{prefix} archive_match_id {archive_match_id} was not found")
        if archived.get("mode") != "prematch" or archived.get("status") not in {
            "pending",
            "reviewed",
        }:
            raise ValueError(f"{prefix} must bind to a prematch prediction archive")

        archive_stage = _required_text(raw["archive_stage"], f"{prefix}.archive_stage")
        if archive_match_id in seen_match_ids:
            raise ValueError(f"{prefix} duplicates an archived match")
        seen_match_ids.add(archive_match_id)
        archive_stages.add(archive_stage)
        archive_match_ids.append(archive_match_id)
        archived_version = _select_archived_version(archived, archive_stage, prefix)
        kickoff_dates.add(_expected_kickoff_date(archived_version))
        supplied_hash = _required_text(
            raw["archive_version_hash"], f"{prefix}.archive_version_hash"
        ).casefold()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", supplied_hash):
            raise ValueError(f"{prefix}.archive_version_hash must be a sha256: hash")
        if supplied_hash != archive_version_hash(archived_version):
            raise ValueError(f"{prefix}.archive_version_hash does not match the selected archived version")

        home = _safe_metadata_text(
            raw["home_team"],
            f"{prefix}.home_team",
            max_length=48,
            max_visual_units=60,
        )
        away = _safe_metadata_text(
            raw["away_team"],
            f"{prefix}.away_team",
            max_length=48,
            max_visual_units=60,
        )
        if archived_version.get("home_team") != home or archived_version.get("away_team") != away:
            raise ValueError(f"{prefix} teams do not match the selected archived fixture")

        supplied_time = _required_text(raw["time"], f"{prefix}.time")
        expected_time = _expected_kickoff_time(archived_version)
        if supplied_time != expected_time:
            raise ValueError(f"{prefix}.time does not match the selected archived fixture ({expected_time})")
        supplied_league = _required_text(raw["league"], f"{prefix}.league")
        expected_league = _safe_metadata_text(
            plain_text_formatter.league_display_name(archived_version),
            f"{prefix}.archived_league_display",
            max_length=24,
            max_visual_units=24,
        )
        evidence_raw = archived_version.get("competition_evidence")
        evidence = memory_store.validated_competition_evidence(archived_version)
        if evidence_raw is not None and not isinstance(evidence, dict):
            raise ValueError(f"{prefix} archived competition evidence is invalid")
        supplied_evidence_hash = raw.get("competition_evidence_hash")
        if isinstance(evidence, dict):
            expected_evidence_hash = str(evidence.get("evidence_hash") or "")
            if supplied_evidence_hash != expected_evidence_hash:
                raise ValueError(
                    f"{prefix}.competition_evidence_hash does not match the archived metadata"
                )
        elif supplied_evidence_hash is not None:
            raise ValueError(
                f"{prefix}.competition_evidence_hash is not authorized by the archive"
            )
        accepted_league_values = {expected_league}
        for field in ("league", "league_key"):
            archived_value = archived_version.get(field)
            if isinstance(archived_value, str) and archived_value.strip():
                accepted_league_values.add(archived_value.strip())
        if supplied_league not in accepted_league_values:
            raise ValueError(f"{prefix}.league does not match the selected archived fixture ({expected_league})")

        total_goals, htft, scores = _joint_artifact_display(
            archived_version
        )

        archived_primary = _archived_primary(archived_version, prefix)
        authorized_labels = _archived_observation_labels(archived_version, prefix)
        derived_status = (
            "formal_primary"
            if archived_primary is not None
            else "observation"
            if authorized_labels
            else "no_bet"
        )
        if status != derived_status:
            if status == "formal_primary":
                raise ValueError(
                    f"{prefix} cannot be formal_primary without an archived primary_pick"
                )
            if status == "observation" and derived_status == "no_bet":
                raise ValueError(
                    f"{prefix} cannot be observation without validated candidate_audits"
                )
            if derived_status == "formal_primary":
                raise ValueError(
                    f"{prefix}.{status} conflicts with an archived active formal primary"
                )
            raise ValueError(
                f"{prefix}.status does not match the archive-derived status ({derived_status})"
            )
        if derived_status == "formal_primary":
            supplied_primary = raw.get("primary")
            if supplied_primary is not None and _display_value(
                supplied_primary, f"{prefix}.primary"
            ) != archived_primary:
                raise ValueError(f"{prefix}.primary does not match the archived active primary")
            primary = archived_primary
            star = True
        else:
            primary = "无正式主推"
            star = False

        rows.append(
            CardRow(
                identifier=_safe_metadata_text(
                    raw["id"], f"{prefix}.id", max_length=24, max_visual_units=20
                ),
                time=supplied_time,
                league=expected_league,
                match=f"{home}\nvs {away}",
                primary=primary,
                total_goals=total_goals,
                htft=htft,
                scores=scores,
                status=status,
                star=star,
            )
        )
    if len(archive_stages) != 1:
        raise ValueError("one prediction card cannot mix initial and lineup-check rows")
    if len(kickoff_dates) != 1:
        raise ValueError("one prediction card cannot mix local kickoff dates")
    stage = next(iter(archive_stages))
    stage_label = "初盘分析" if stage == "initial" else "临场分析"
    date = next(iter(kickoff_dates))
    if len(rows) == 1:
        title = f"{rows[0].league}｜赛前模型卡"
        subtitle = f"{archive_match_ids[0]}｜日本时间 {rows[0].time}｜{stage_label}"
    else:
        title = "今日足球扫盘"
        subtitle = f"{stage_label}｜{len(rows)}场"
    return Card(date=date, title=title, subtitle=subtitle, rows=tuple(rows))


def _char_units(char: str) -> float:
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 2.0
    # Latin glyphs are not monospaced: treating a wide ``W`` as one unit can
    # overflow a cell even when the nominal wrap count passes.  A conservative
    # 1.5-unit width keeps the SVG heuristic and real Pillow bounds aligned.
    if char.isascii() and not char.isspace():
        return 1.5
    return 1.0


def _visual_width(text: str) -> float:
    return sum(_char_units(char) for char in text)


def _wrap_line(text: str, max_units: int) -> list[str]:
    """Wrap one line without discarding or replacing any non-space character."""
    if not text:
        return [""]
    lines: list[str] = []
    current: list[str] = []
    used = 0
    last_break = -1
    for char in text:
        cost = _char_units(char)
        if current and used + cost > max_units:
            if last_break >= 0:
                # Keep the break delimiter in the preceding tspan.  SVG lays a
                # trailing space out harmlessly, while XML ``itertext()`` then
                # preserves the exact visible phrase instead of joining
                # ``主胜`` and ``42.0%`` into ``主胜42.0%``.
                head = "".join(current[: last_break + 1])
                tail = "".join(current[last_break + 1 :])
                if head:
                    lines.append(head)
                current = list(tail)
                used = _visual_width(tail)
            else:
                lines.append("".join(current))
                current = []
                used = 0
            last_break = -1
            for offset, existing in enumerate(current):
                if existing.isspace() or existing in {"/", "｜", "·"}:
                    last_break = offset
        current.append(char)
        used += cost
        if char.isspace() or char in {"/", "｜", "·"}:
            last_break = len(current) - 1
    if current:
        lines.append("".join(current).strip())
    return lines or [""]


def _cell_lines(text: str, max_units: int) -> tuple[str, ...]:
    lines: list[str] = []
    for source_line in str(text).splitlines() or [""]:
        lines.extend(_wrap_line(source_line, max_units))
    return tuple(lines)


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


def _svg_font_size(
    lines: Sequence[str], width: int, height: int, base: int, minimum: int = 11
) -> int:
    widest = max((_visual_width(line) for line in lines), default=1)
    width_fit = int((width - 16) * 2 / max(widest, 1))
    height_fit = int((height - 16) / max(len(lines) * 1.28, 1))
    return max(minimum, min(base, width_fit, height_fit))


def _svg_text(
    *,
    center_x: float,
    top: float,
    height: float,
    lines: Sequence[str],
    font_size: int,
    fill: str,
    weight: str = "500",
) -> str:
    line_height = font_size * 1.28
    block_height = line_height * len(lines)
    first_baseline = top + (height - block_height) / 2 + font_size
    spans = "".join(
        f'<tspan x="{center_x:g}" y="{first_baseline + index * line_height:g}">{escape(line)}</tspan>'
        for index, line in enumerate(lines)
    )
    return (
        f'<text x="{center_x:g}" text-anchor="middle" '
        'font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" '
        f'font-size="{font_size}" font-weight="{weight}" fill="{fill}">{spans}</text>'
    )


def render_svg(card: Card) -> str:
    """Return a standalone SVG card with no truncation markers."""
    height = card.height
    table_top = TITLE_HEIGHT
    rows_top = table_top + HEADER_HEIGHT
    table_bottom = rows_top + len(card.rows) * ROW_HEIGHT
    formal_count = sum(row.status == "formal_primary" for row in card.rows)

    title_size = _svg_font_size((card.title,), 1160, 62, 48, 24)
    subtitle = f"{card.date}｜{card.subtitle}"
    subtitle_size = _svg_font_size((subtitle,), 1180, 40, 25, 16)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}">',
        f'<rect width="{WIDTH}" height="{height}" fill="{COLORS["page"]}"/>',
        f'<rect x="{SIDE_MARGIN}" y="{PANEL_VERTICAL_MARGIN}" width="{TABLE_WIDTH}" height="{height - 2 * PANEL_VERTICAL_MARGIN}" rx="20" fill="{COLORS["panel"]}" stroke="{COLORS["grid"]}"/>',
        f'<text x="72" y="94" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="{title_size}" font-weight="700" fill="{COLORS["text"]}">{escape(card.title)}</text>',
        f'<text x="74" y="143" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="{subtitle_size}" fill="{COLORS["header_dark"]}">{escape(subtitle)}</text>',
        f'<rect x="{WIDTH - 202}" y="60" width="126" height="54" rx="14" fill="{COLORS["header"]}"/>',
        f'<text x="{WIDTH - 139}" y="96" text-anchor="middle" font-family="Microsoft YaHei, sans-serif" font-size="23" font-weight="700" fill="#ffffff">{len(card.rows)} 场</text>',
        f'<rect x="{SIDE_MARGIN}" y="{table_top}" width="{TABLE_WIDTH}" height="{HEADER_HEIGHT}" fill="{COLORS["header"]}"/>',
    ]

    x = SIDE_MARGIN
    for label, _key, width in COLUMNS:
        parts.append(
            f'<text x="{x + width / 2:g}" y="{table_top + 45}" text-anchor="middle" '
            'font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" '
            f'font-size="21" font-weight="700" fill="#ffffff">{escape(label)}</text>'
        )
        x += width

    for row_index, row in enumerate(card.rows):
        y = rows_top + row_index * ROW_HEIGHT
        if row_index % 2:
            parts.append(
                f'<rect x="{SIDE_MARGIN}" y="{y}" width="{TABLE_WIDTH}" height="{ROW_HEIGHT}" fill="{COLORS["row_alt"]}"/>'
            )
        x = SIDE_MARGIN
        for column_index, ((_, key, width), value) in enumerate(zip(COLUMNS, _row_values(row))):
            rendered_value = value + (" ★" if key == "primary" and row.star else "")
            lines = _cell_lines(rendered_value, CELL_WRAP_UNITS[column_index])
            base = 18 if key in {"total_goals", "htft", "scores"} else 21
            if key == "league":
                base = 19
            font_size = _svg_font_size(lines, width, ROW_HEIGHT, base)
            fill = COLORS["text"]
            weight = "600" if key in {"time", "primary"} else "500"
            if key == "primary" and (
                row.status == "observation" or row.primary.startswith("◇")
            ):
                fill = COLORS["observation"]
            elif key == "primary" and row.status == "no_bet":
                fill = COLORS["no_bet"]
            elif key == "primary" and row.star:
                fill = COLORS["header_dark"]
            parts.append(
                _svg_text(
                    center_x=x + width / 2,
                    top=y,
                    height=ROW_HEIGHT,
                    lines=lines,
                    font_size=font_size,
                    fill=fill,
                    weight=weight,
                )
            )
            x += width

    x = SIDE_MARGIN
    for _label, _key, width in COLUMNS[:-1]:
        x += width
        parts.append(f'<line x1="{x}" y1="{table_top}" x2="{x}" y2="{table_bottom}" stroke="{COLORS["grid"]}"/>')
    for index in range(len(card.rows) + 1):
        y = rows_top + index * ROW_HEIGHT
        parts.append(f'<line x1="{SIDE_MARGIN}" y1="{y}" x2="{SIDE_MARGIN + TABLE_WIDTH}" y2="{y}" stroke="{COLORS["grid"]}"/>')
    parts.append(f'<rect x="{SIDE_MARGIN}" y="{table_top}" width="{TABLE_WIDTH}" height="{table_bottom - table_top}" fill="none" stroke="{COLORS["grid"]}"/>')

    footer_y = table_bottom + 36
    parts.extend(
        [
            f'<text x="72" y="{footer_y}" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="20" font-weight="600" fill="{COLORS["star"]}">★ 正式主推中的最高信心方向</text>',
            f'<text x="455" y="{footer_y}" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="20" font-weight="600" fill="{COLORS["observation"]}">无正式主推＝不下注、不结算、不计战绩</text>',
            f'<text x="{WIDTH - 76}" y="{footer_y}" text-anchor="end" font-family="Microsoft YaHei, sans-serif" font-size="20" fill="{COLORS["muted"]}">正式主推 {formal_count} 场</text>',
            f'<line x1="72" y1="{footer_y + 22}" x2="{WIDTH - 72}" y2="{footer_y + 22}" stroke="{COLORS["grid"]}"/>',
            f'<text x="72" y="{footer_y + 61}" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="20" font-weight="600" fill="{COLORS["warning"]}">提示：仅供比赛分析与模型复盘，不承诺收益；请理性参考。</text>',
            f'<text x="72" y="{footer_y + 90}" font-family="Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif" font-size="16" fill="{COLORS["muted"]}">{escape(FOOTER_SOURCE_NOTE)}</text>',
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
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("PNG/JPEG output requires Pillow") from error
    candidates = list(_font_candidates())
    if bold:
        windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates = [windows / "msyhbd.ttc", windows / "simhei.ttf"] + candidates
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _fit_pil_font(
    draw: Any,
    lines: Sequence[str],
    box: tuple[int, int, int, int],
    base: int,
    *,
    bold: bool = False,
):
    left, top, right, bottom = box
    text = "\n".join(lines)
    for size in range(base, 10, -1):
        font = _load_font(size, bold=bold)
        bounds = draw.multiline_textbbox((0, 0), text, font=font, spacing=5, align="center")
        if bounds[2] - bounds[0] <= right - left - 14 and bounds[3] - bounds[1] <= bottom - top - 14:
            return font
    return _load_font(11, bold=bold)


def _pil_center(draw: Any, box: tuple[int, int, int, int], text: str, font: Any, fill: str) -> None:
    left, top, right, bottom = box
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.text(((left + right - width) / 2, (top + bottom - height) / 2 - bounds[1]), text, font=font, fill=fill)


def _pil_center_multiline(
    draw: Any,
    box: tuple[int, int, int, int],
    lines: Sequence[str],
    font: Any,
    fill: str,
) -> None:
    text = "\n".join(lines)
    left, top, right, bottom = box
    bounds = draw.multiline_textbbox((0, 0), text, font=font, spacing=5, align="center")
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.multiline_text(
        ((left + right - width) / 2, (top + bottom - height) / 2 - bounds[1]),
        text,
        font=font,
        fill=fill,
        spacing=5,
        align="center",
    )


def _fit_single_line_font(draw: Any, text: str, max_width: int, base: int, minimum: int):
    for size in range(base, minimum - 1, -1):
        font = _load_font(size, bold=base >= 40)
        bounds = draw.textbbox((0, 0), text, font=font)
        if bounds[2] - bounds[0] <= max_width:
            return font
    return _load_font(minimum, bold=base >= 40)


def render_raster(card: Card, output_format: str) -> bytes:
    """Render PNG or JPEG bytes using the same no-truncation layout."""
    try:
        from PIL import Image, ImageDraw
    except ImportError as error:
        raise RuntimeError("PNG/JPEG output requires Pillow") from error
    from io import BytesIO

    image = Image.new("RGB", (WIDTH, card.height), COLORS["page"])
    draw = ImageDraw.Draw(image)
    title_font = _fit_single_line_font(draw, card.title, 1160, 48, 24)
    subtitle = f"{card.date}｜{card.subtitle}"
    subtitle_font = _fit_single_line_font(draw, subtitle, 1180, 25, 16)
    header_font = _load_font(20, bold=True)
    footer_font = _load_font(20)
    small_font = _load_font(16)

    draw.rounded_rectangle(
        (SIDE_MARGIN, PANEL_VERTICAL_MARGIN, SIDE_MARGIN + TABLE_WIDTH, card.height - PANEL_VERTICAL_MARGIN),
        radius=20,
        fill=COLORS["panel"],
        outline=COLORS["grid"],
    )
    draw.text((72, 48), card.title, font=title_font, fill=COLORS["text"])
    draw.text((74, 112), subtitle, font=subtitle_font, fill=COLORS["header_dark"])
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

    for row_index, row in enumerate(card.rows):
        y = rows_top + row_index * ROW_HEIGHT
        if row_index % 2:
            draw.rectangle((SIDE_MARGIN, y, SIDE_MARGIN + TABLE_WIDTH, y + ROW_HEIGHT), fill=COLORS["row_alt"])
        x = SIDE_MARGIN
        for column_index, ((_, key, width), value) in enumerate(zip(COLUMNS, _row_values(row))):
            rendered_value = value + (" ★" if key == "primary" and row.star else "")
            lines = _cell_lines(rendered_value, CELL_WRAP_UNITS[column_index])
            base = 18 if key in {"total_goals", "htft", "scores"} else 21
            if key == "league":
                base = 19
            bold = key in {"time", "primary"}
            box = (x, y, x + width, y + ROW_HEIGHT)
            font = _fit_pil_font(draw, lines, box, base, bold=bold)
            fill = COLORS["text"]
            if key == "primary" and (
                row.status == "observation" or row.primary.startswith("◇")
            ):
                fill = COLORS["observation"]
            elif key == "primary" and row.status == "no_bet":
                fill = COLORS["no_bet"]
            elif key == "primary" and row.star:
                fill = COLORS["header_dark"]
            _pil_center_multiline(draw, box, lines, font, fill)
            x += width

    x = SIDE_MARGIN
    for _label, _key, width in COLUMNS[:-1]:
        x += width
        draw.line((x, table_top, x, table_bottom), fill=COLORS["grid"])
    for index in range(len(card.rows) + 1):
        y = rows_top + index * ROW_HEIGHT
        draw.line((SIDE_MARGIN, y, SIDE_MARGIN + TABLE_WIDTH, y), fill=COLORS["grid"])
    draw.rectangle((SIDE_MARGIN, table_top, SIDE_MARGIN + TABLE_WIDTH, table_bottom), outline=COLORS["grid"])

    footer_y = table_bottom + 22
    draw.text((72, footer_y), "★ 正式主推中的最高信心方向", font=footer_font, fill=COLORS["star"])
    draw.text(
        (455, footer_y),
        "无正式主推＝不下注、不结算、不计战绩",
        font=footer_font,
        fill=COLORS["observation"],
    )
    count_text = f"正式主推 {sum(row.status == 'formal_primary' for row in card.rows)} 场"
    count_width = draw.textbbox((0, 0), count_text, font=footer_font)[2]
    draw.text((WIDTH - 76 - count_width, footer_y), count_text, font=footer_font, fill=COLORS["muted"])
    draw.line((72, footer_y + 42, WIDTH - 72, footer_y + 42), fill=COLORS["grid"])
    draw.text((72, footer_y + 61), "提示：仅供比赛分析与模型复盘，不承诺收益；请理性参考。", font=footer_font, fill=COLORS["warning"])
    draw.text((72, footer_y + 92), FOOTER_SOURCE_NOTE, font=small_font, fill=COLORS["muted"])

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
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
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
    card = validate_payload(payload, load_history_records(history_path))
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
    parser = argparse.ArgumentParser(description="Render a concise football prediction card")
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 JSON input")
    parser.add_argument("--history", required=True, type=Path, help="immutable prediction history")
    parser.add_argument("--output", required=True, type=Path, help=".svg, .png, .jpg, or .jpeg output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        destination = render_file(
            arguments.input.resolve(), arguments.output.resolve(), arguments.history.resolve()
        )
    except (ValueError, RuntimeError) as error:
        raise SystemExit(f"error: {error}") from error
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

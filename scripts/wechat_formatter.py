#!/usr/bin/env python3
"""Render archived soccer-predict results as compact WeChat-ready plain text."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import memory_store


RESULT_LABELS = {
    "win": "红",
    "half_win": "半红",
    "push": "走",
    "half_loss": "半黑",
    "loss": "黑",
}
HTFT_LABELS = {"H": "主", "D": "平", "A": "客"}
FORBIDDEN_MARKUP = re.compile(r"(?:^|\n)\s*(?:#{1,6}\s|[-*+]\s|```|</?(?:html|table|div|p)\b)", re.I)


def clean_text(value: Any, limit: int = 90) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"[*_`#<>\[\]]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ｜|-")
    if not text:
        return "无"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def percentage(value: Any) -> str:
    if value is None:
        return "未取得"
    return f"{float(value) * 100:.1f}%"


def price(value: Any) -> str:
    return "" if value is None else f" @{float(value):.2f}"


def format_time(value: Any, timezone_name: str = "Asia/Tokyo") -> str:
    if not value:
        return "未取得"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return clean_text(value)
        try:
            target_zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            fixed = {"Asia/Tokyo": timezone(timedelta(hours=9), "Asia/Tokyo")}
            if timezone_name not in fixed:
                raise
            target_zone = fixed[timezone_name]
        local = parsed.astimezone(target_zone)
        return local.strftime("%Y-%m-%d %H:%M（日本时间）")
    except (ValueError, TypeError):
        return clean_text(value)


def format_pick(market: str | None, pick: dict[str, Any] | None, record: dict[str, Any]) -> str:
    if not market or not isinstance(pick, dict):
        return "无正式推荐"
    if market == "asian":
        team = record.get("home_team") if pick.get("side") == "home" else record.get("away_team")
        return f"{clean_text(team)} {float(pick.get('line', 0)):+g}{price(pick.get('odds'))}"
    if market == "total":
        side = "大" if pick.get("side") == "over" else "小"
        return f"{side}{float(pick.get('line', 0)):g}{price(pick.get('odds'))}"
    if market == "half_time":
        half_market = pick.get("market")
        if half_market == "1x2":
            side = {"home": "半场主胜", "draw": "半场平", "away": "半场客胜"}.get(pick.get("side"), "半场")
            return f"{side}{price(pick.get('odds'))}"
        if half_market == "total":
            side = "半场大" if pick.get("side") == "over" else "半场小"
            return f"{side}{float(pick.get('line', 0)):g}{price(pick.get('odds'))}"
        team = record.get("home_team") if pick.get("side") == "home" else record.get("away_team")
        return f"半场 {clean_text(team)} {float(pick.get('line', 0)):+g}{price(pick.get('odds'))}"
    if market == "htft":
        selection = str(pick.get("selection", "")).upper()
        label = "/".join(HTFT_LABELS.get(char, char) for char in selection)
        return f"{label}{price(pick.get('odds'))}"
    return clean_text(pick)


def version_candidates(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in record.get("revisions", []) if isinstance(item, dict)] + [record]


def select_version(record: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == "initial":
        candidates = [item for item in version_candidates(record) if item.get("analysis_stage", "initial") == "initial"]
        if not candidates:
            raise ValueError("No archived initial version is available")
        return candidates[-1]
    if kind == "lineup-check":
        if record.get("analysis_stage") != "lineup-check":
            raise ValueError("The active record is not a lineup-check version")
        return record
    if record.get("status") != "reviewed":
        raise ValueError("Review copy requires a reviewed record")
    return record


def merged_version(record: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
    merged = dict(record)
    merged.update(version)
    return merged


def exact_scores(version: dict[str, Any]) -> str:
    picks = [pick for pick in version.get("exact_score_picks", []) if isinstance(pick, dict)][:2]
    if not picks:
        return "未取得"
    return "、".join(f"{pick.get('score')}（{percentage(pick.get('probability'))}）" for pick in picks)


def primary_line(version: dict[str, Any], record: dict[str, Any]) -> str:
    return format_pick(version.get("primary_market"), version.get("primary_pick"), record)


def secondary_picks(version: dict[str, Any], record: dict[str, Any]) -> str:
    primary_identity = memory_store.pick_identity(version.get("primary_market"), version.get("primary_pick"))
    values = []
    for market, pick in memory_store.formal_picks(version):
        if market not in {"asian", "total"}:
            continue
        if memory_store.pick_identity(market, pick) == primary_identity:
            continue
        values.append(format_pick(market, pick, record))
    return "、".join(values[:2]) if values else "无"


def half_time_text(version: dict[str, Any], record: dict[str, Any]) -> str:
    pick = version.get("half_time_pick")
    return format_pick("half_time", pick, record) if isinstance(pick, dict) else "观察或无正式推荐"


def htft_text(version: dict[str, Any], record: dict[str, Any]) -> str:
    picks = [pick for pick in version.get("htft_picks", []) if isinstance(pick, dict)]
    return "、".join(format_pick("htft", pick, record) for pick in picks[:2]) if picks else "观察或赔率缺失"


def validate_plain_text(lines: list[str]) -> str:
    normalized = "\n".join(clean_text(line, 140) for line in lines if clean_text(line, 140) != "无")
    if FORBIDDEN_MARKUP.search(normalized):
        raise ValueError("Generated message contains Markdown or HTML")
    if len(normalized) > 1400:
        raise ValueError("Generated message exceeds 1400 characters")
    if len(normalized.splitlines()) > 18:
        raise ValueError("Generated message exceeds 18 lines")
    return normalized


def render_initial(record: dict[str, Any]) -> str:
    version = merged_version(record, select_version(record, "initial"))
    primary = version.get("primary_pick") if isinstance(version.get("primary_pick"), dict) else {}
    return validate_plain_text([
        f"【初盘分析｜{record.get('match_id')}】",
        f"赛事：{record.get('league_key') or memory_store.league_key_for_record(record)}",
        f"比赛：{record.get('home_team')} vs {record.get('away_team')}",
        f"开赛：{format_time(record.get('kickoff'))}",
        f"主推：{primary_line(version, record)}",
        f"主推概率：{percentage(primary.get('probability'))}｜EV {percentage(primary.get('ev'))}",
        f"次选：{secondary_picks(version, record)}",
        f"半场：{half_time_text(version, record)}",
        f"半全场：{htft_text(version, record)}",
        f"比分参考：{exact_scores(version)}",
        f"核心判断：{clean_text(version.get('recommendation'))}",
        f"风险：{clean_text(version.get('notes'))}",
        "仅供数据分析参考",
    ])


def render_lineup(record: dict[str, Any]) -> str:
    version = merged_version(record, select_version(record, "lineup-check"))
    primary = version.get("primary_pick") if isinstance(version.get("primary_pick"), dict) else {}
    change = version.get("primary_change") if isinstance(version.get("primary_change"), dict) else {}
    status = change.get("status")
    if status == "maintained":
        change_line = f"主推维持：{primary_line(version, record)}"
    else:
        previous_versions = [item for item in record.get("revisions", []) if isinstance(item, dict)]
        previous = merged_version(record, previous_versions[-1]) if previous_versions else {}
        previous_text = primary_line(previous, record) if previous else "原方向"
        change_line = f"主推变更：{previous_text} → {primary_line(version, record)}"
    return validate_plain_text([
        f"【临场分析｜{record.get('match_id')}】",
        f"赛事：{record.get('league_key') or memory_store.league_key_for_record(record)}",
        f"比赛：{record.get('home_team')} vs {record.get('away_team')}",
        f"检查时间：{format_time(record.get('lineup_rechecked_at'))}",
        "比赛状态：赛前，临场版本已归档",
        change_line,
        f"当前主推：{primary_line(version, record)}",
        f"主推概率：{percentage(primary.get('probability'))}｜EV {percentage(primary.get('ev'))}",
        f"次选：{secondary_picks(version, record)}",
        f"半场：{half_time_text(version, record)}",
        f"半全场：{htft_text(version, record)}",
        f"比分参考：{exact_scores(version)}",
        f"临场判断：{clean_text(version.get('recommendation'))}",
        f"风险：{clean_text(version.get('notes'))}",
        "仅供数据分析参考",
    ])


def result_text(result: Any) -> str:
    return RESULT_LABELS.get(str(result), "未结算")


def review_secondary_picks(basis: dict[str, Any], record: dict[str, Any]) -> str:
    primary_identity = memory_store.pick_identity(basis.get("primary_market"), basis.get("primary_pick"))
    formal = basis.get("formal_picks") if isinstance(basis.get("formal_picks"), dict) else {}
    values = []
    for market in ("asian", "total", "half_time"):
        pick = formal.get(market)
        if isinstance(pick, dict) and memory_store.pick_identity(market, pick) != primary_identity:
            values.append(format_pick(market, pick, record))
    for pick in formal.get("htft", []):
        if isinstance(pick, dict) and memory_store.pick_identity("htft", pick) != primary_identity:
            values.append(format_pick("htft", pick, record))
    return "、".join(values) if values else "无"


def performance_text(block: dict[str, Any] | None) -> str:
    if not isinstance(block, dict):
        return "暂无"
    roi = block.get("roi")
    roi_text = "—" if roi is None else f"{float(roi) * 100:+.2f}%"
    return (
        f"{block.get('matches', 0)}场{block.get('wins', 0)}胜"
        f"{block.get('losses', 0)}负{block.get('pushes', 0)}走｜"
        f"收益{float(block.get('profit_units', 0)):+.2f}u｜ROI {roi_text}"
    )


def render_review(record: dict[str, Any], history: list[dict[str, Any]]) -> str:
    select_version(record, "review")
    stats = memory_store.calculate_stats(history)
    league_key = record.get("league_key") or memory_store.league_key_for_record(record)
    league = stats["leagues"].get(league_key, {})
    basis = record.get("settlement_basis") if isinstance(record.get("settlement_basis"), dict) else memory_store.settlement_basis_for_record(record)
    basis_label = "临场版" if basis.get("analysis_stage") == "lineup-check" else "初盘版"
    primary = basis.get("primary_pick") if isinstance(basis.get("primary_pick"), dict) else {}
    return validate_plain_text([
        f"【赛后复盘｜{league_key}｜{record.get('match_id')}】",
        f"比赛：{record.get('home_team')} vs {record.get('away_team')}",
        f"半场：{record.get('half_time_score') or '未取得'}｜全场：{record.get('final_score') or '未取得'}",
        f"结算依据：{basis_label}最终有效推荐",
        f"主推：{format_pick(basis.get('primary_market'), primary, record)}＝{result_text(record.get('primary_result'))}",
        f"次选参考：{review_secondary_picks(basis, record)}（不结算、不计战绩）",
        f"比分参考：{exact_scores(record)}｜命中排名：{record.get('exact_score_hit_rank') or '未命中'}",
        f"本场关键：{clean_text(record.get('key_learning'))}",
        f"{league_key}主推：{performance_text(league.get('primary'))}",
        f"累计主推：{performance_text(stats.get('primary'))}",
        "复盘用于校准分析，不代表未来收益",
    ])


def render(base_dir: str | None, match_id: str, kind: str) -> str:
    path = memory_store.data_path(base_dir)
    history = memory_store.load_history(path)
    record = memory_store.find_record(history, match_id)
    if not record:
        raise ValueError(f"No archived match found: {match_id}")
    if kind == "initial":
        return render_initial(record)
    if kind == "lineup-check":
        return render_lineup(record)
    return render_review(record, history)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", help="Workspace root; defaults to current directory")
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--kind", choices=("initial", "lineup-check", "review"), required=True)
    return parser


def main() -> int:
    memory_store.configure_stdio()
    args = build_parser().parse_args()
    try:
        print(render(args.base_dir, args.match_id, args.kind))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

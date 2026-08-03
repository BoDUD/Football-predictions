#!/usr/bin/env python3
"""Render archived soccer-predict results as compact copyable plain text."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import memory_store
import public_market_outlook


RESULT_LABELS = {
    "win": "红",
    "half_win": "半红",
    "push": "走",
    "half_loss": "半黑",
    "loss": "黑",
}
HTFT_LABELS = {"H": "主", "D": "平", "A": "客"}
HTFT_SCENARIO_LABELS = {"H": "胜", "D": "平", "A": "负"}
ONE_X_TWO_LABELS = {"home": "主胜", "draw": "平局", "away": "客胜"}
BTTS_LABELS = {"yes": "是", "no": "否"}
LEAGUE_DISPLAY_LABELS = {
    "finland_veikkausliiga": "芬超",
    "finlandveikkausliiga": "芬超",
    "korea_k_league_1": "韩K联",
    "koreakleague1": "韩K联",
    "sweden_allsvenskan": "瑞典超",
    "swedenallsvenskan": "瑞典超",
    "england_premier_league": "英超",
    "france_ligue_1": "法甲",
    "spain_la_liga": "西甲",
    "germany_bundesliga": "德甲",
    "italy_serie_a": "意甲",
    "uefa_champions_league": "欧冠",
    "afc_champions_league": "亚冠",
}
OBSERVATION_GATE_LABELS = {
    "complete_current_market": "完整当前9路赔率",
    "odds_provenance": "赛前赔率来源时间戳",
    "positive_ev": "正EV",
    "positive_edge": "正边际",
    "bookmaker_depth": "至少5家公司",
    "data_quality": "中高数据质量",
    "scenario_stability": "形态稳定性",
    "scenario_coherence": "全场一致性",
    "descriptive_pair_mass_threshold": "Top2概率和描述阈值",
    "league_forward_evidence": "联赛前向验证",
    "market_policy_enabled": "当前政策放行",
}
FORBIDDEN_MARKUP = re.compile(r"(?:^|\n)\s*(?:#{1,6}\s|[-*+]\s|```|</?(?:html|table|div|p)\b)", re.I)
ZERO_ZERO_REFERENCE = re.compile(r"0-0", re.IGNORECASE)
ZERO_ZERO_CONTINUATION = re.compile(
    r"^\s*(?:(?:其|该项|对应)\s*)?"
    r"(?:概率|总?排名|全分布|赔率|EV|期望值|未进前二|进入前二|未进\s*Top-?2)",
    re.IGNORECASE,
)


def clean_text(value: Any, limit: int | None = None) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"[*_`#<>\[\]]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ｜|-")
    if not text:
        return "无"
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def percentage(value: Any) -> str:
    if value is None:
        return "未取得"
    return f"{float(value) * 100:.1f}%"


def price(value: Any) -> str:
    return "" if value is None else f" @{float(value):.2f}"


def goal_range_label(pick: dict[str, Any]) -> str:
    selection = clean_text(pick.get("selection"))
    if selection != "无":
        return selection
    minimum = (
        pick.get("minimum_goals")
        if "minimum_goals" in pick
        else pick.get("min_goals")
    )
    maximum = (
        pick.get("maximum_goals")
        if "maximum_goals" in pick
        else pick.get("max_goals")
    )
    if minimum is None:
        return "未取得"
    if maximum is None:
        return f"{int(minimum)}+"
    return f"{int(minimum)}-{int(maximum)}"


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


def league_display_name(record: dict[str, Any]) -> str:
    """Return a stable user-facing league name instead of an internal key."""

    raw = str(record.get("league_key") or memory_store.league_key_for_record(record) or "").strip()
    normalized = re.sub(r"[\s-]+", "_", raw.casefold())
    compact = normalized.replace("_", "")
    return LEAGUE_DISPLAY_LABELS.get(
        normalized,
        LEAGUE_DISPLAY_LABELS.get(compact, raw or "未取得"),
    )


def format_pick(market: str | None, pick: dict[str, Any] | None, record: dict[str, Any]) -> str:
    if not market or not isinstance(pick, dict):
        return "无正式推荐"
    if market == "asian":
        team = record.get("home_team") if pick.get("side") == "home" else record.get("away_team")
        return f"{clean_text(team)} {float(pick.get('line', 0)):+g}{price(pick.get('odds'))}"
    if market == "total":
        side = "大" if pick.get("side") == "over" else "小"
        return f"{side}{float(pick.get('line', 0)):g}{price(pick.get('odds'))}"
    if market == "goal_range":
        return f"总进球{goal_range_label(pick)}球{price(pick.get('odds'))}"
    if market == "btts":
        side = {"yes": "是", "no": "否"}.get(str(pick.get("side", "")).lower(), clean_text(pick.get("side")))
        return f"双方进球-{side}{price(pick.get('odds'))}"
    if market == "corner_total":
        side = "大" if pick.get("side") == "over" else "小"
        return f"角球{side}{float(pick.get('line', 0)):g}{price(pick.get('odds'))}"
    if market == "corner_handicap":
        side = {"home": "主队", "away": "客队"}.get(
            str(pick.get("side", "")).lower(),
            clean_text(pick.get("side")),
        )
        return f"{side}角球{float(pick.get('line', 0)):+g}{price(pick.get('odds'))}"
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
        raise ValueError("Review plain-text output requires a reviewed record")
    return record


def merged_version(record: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
    merged = dict(record)
    # A later lineup-check artifact must never leak into a historical initial
    # rendering merely because an old revision predates the joint schema.
    if version is not record and "joint_scenario_audit" not in version:
        merged["joint_scenario_audit"] = None
    merged.update(version)
    return merged


def _top_probability(
    value: Any, order: tuple[str, ...]
) -> tuple[str, float] | None:
    if not isinstance(value, dict) or not value:
        return None
    candidates: list[tuple[str, float]] = []
    for key, raw_probability in value.items():
        try:
            probability = float(raw_probability)
        except (TypeError, ValueError):
            return None
        candidates.append((str(key), probability))
    order_index = {key: index for index, key in enumerate(order)}
    return min(
        candidates,
        key=lambda item: (-item[1], order_index.get(item[0], len(order)), item[0]),
    )


def _insufficient_joint_outlook() -> dict[str, str]:
    return {
        "half_time": "数据不足",
        "one_x_two": "数据不足",
        "goal_range": "数据不足",
        "btts": "数据不足",
        "scenarios": "数据不足",
        "source": "未取得可验证联合模型",
    }


def _format_public_market(
    market: dict[str, Any], labels: dict[str, str] | None = None
) -> str:
    raw_items = market.get("display_items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("public market display selection is incomplete")
    ranked = list(raw_items)
    values = " / ".join(
        f"{(labels or {}).get(str(item.get('code')), clean_text(item.get('label')))}"
        f"{percentage(item.get('probability'))}"
        for item in ranked
    )
    gap = float(market.get("gap_percentage_points"))
    clarity = "较明确" if market.get("clarity") == "clear" else "分歧"
    gap_label = "领先第二名" if len(ranked) == 1 else "前二差"
    return f"{values}（{clarity}，{gap_label}{gap:.1f}个百分点）"


def joint_outlook(version: dict[str, Any]) -> dict[str, str]:
    """Return every descriptive football field from one validated artifact.

    The formatter intentionally has no legacy fallback.  Independent HT/FT
    and exact-score lists remain in the archive for review diagnostics only.
    """

    artifact = memory_store.validated_joint_scenario_audit(version)
    if not isinstance(artifact, dict):
        return _insufficient_joint_outlook()
    try:
        public = public_market_outlook.build_public_market_outlook(artifact)
        markets = public["markets"]
        scenarios = public["joint_scenarios"]
        scenario_items = scenarios["items"]
        scenario_text = " / ".join(
            f"{''.join(HTFT_SCENARIO_LABELS[char] for char in item['htft'])}·"
            f"{item['score']} {percentage(item['probability'])}"
            for item in scenario_items
        )
        mode = public.get("probability_mode")
        source = (
            "纯模型（未混入过期盘口）"
            if mode == "model_only"
            else "模型＋有效半场市场锚"
            if mode == "upstream_half_time_market_anchor"
            else clean_text(mode)
        )
        return {
            "half_time": _format_public_market(
                markets["half_time"], {"H": "主胜", "D": "平", "A": "客胜"}
            ),
            "one_x_two": _format_public_market(
                markets["one_x_two"],
                {"home": "主胜", "draw": "平", "away": "客胜"},
            ),
            "goal_range": _format_public_market(markets["goal_ranges"]),
            "btts": _format_public_market(markets["btts"]),
            "scenarios": f"高方差参考（不作推荐）：{scenario_text}",
            "source": source,
        }
    except (KeyError, TypeError, ValueError, public_market_outlook.PublicMarketOutlookError):
        return _insufficient_joint_outlook()


def exact_scores(version: dict[str, Any]) -> str:
    raw_picks = version.get("display_exact_score_picks")
    if not isinstance(raw_picks, list) or not raw_picks:
        raw_picks = version.get("exact_score_picks", [])
    picks = [pick for pick in raw_picks if isinstance(pick, dict)][:2]
    if not picks:
        return "未取得"
    conditioned = (
        isinstance(version.get("display_exact_score_basis"), dict)
        and version["display_exact_score_basis"].get("basis")
        == "primary_total_net_profit"
    )
    if conditioned:
        return "、".join(
            (
                f"{pick.get('score')}（全场{percentage(pick.get('probability'))}，"
                f"主推成立时{percentage(pick.get('conditional_probability'))}）"
            )
            for pick in picks
        )
    return "、".join(
        f"{pick.get('score')}（{percentage(pick.get('probability'))}）"
        for pick in picks
    )


def display_text(version: dict[str, Any], field: str) -> str:
    value = clean_text(version.get(field))
    audit = version.get("zero_zero_audit")
    audit_visible = (
        audit.get("included_in_display_top2", audit.get("included_in_top2"))
        if isinstance(audit, dict)
        else None
    )
    if not isinstance(audit, dict) or audit_visible:
        return value
    clauses = re.split(r"(?<=[。；！？!?])", value)
    hidden = {index for index, clause in enumerate(clauses) if ZERO_ZERO_REFERENCE.search(clause)}
    for index in tuple(hidden):
        next_index = index + 1
        while next_index < len(clauses) and ZERO_ZERO_CONTINUATION.search(clauses[next_index]):
            hidden.add(next_index)
            next_index += 1
        previous_index = index - 1
        while previous_index >= 0 and ZERO_ZERO_CONTINUATION.search(clauses[previous_index]):
            hidden.add(previous_index)
            previous_index -= 1
    return "".join(clause for index, clause in enumerate(clauses) if index not in hidden).strip() or "无"


def primary_line(version: dict[str, Any], record: dict[str, Any]) -> str:
    return format_pick(
        version.get("primary_market"), resolved_primary_pick(version), record
    )


def resolved_primary_pick(version: dict[str, Any]) -> dict[str, Any] | None:
    pick = version.get("primary_pick")
    if not isinstance(pick, dict):
        return None
    if (
        version.get("primary_market") == "half_time"
        and pick.get("market") not in {"1x2", "asian", "total"}
    ):
        half_time_pick = version.get("half_time_pick")
        if isinstance(half_time_pick, dict):
            return half_time_pick
    return pick


def secondary_picks(version: dict[str, Any], record: dict[str, Any]) -> str:
    primary_identity = memory_store.pick_identity(
        version.get("primary_market"), resolved_primary_pick(version)
    )
    ranked_values: list[tuple[float, int, str]] = []
    for sequence_index, (market, pick) in enumerate(memory_store.formal_picks(version)):
        if memory_store.pick_identity(market, pick) == primary_identity:
            continue
        try:
            rank = float(pick.get("confidence_rank"))
        except (TypeError, ValueError):
            rank = math.inf
        ranked_values.append(
            (rank, sequence_index, format_pick(market, pick, record))
        )
    values = [
        item[2] for item in sorted(ranked_values, key=lambda item: (item[0], item[1]))
    ]
    return "、".join(values) if values else "无"


def structured_analysis_summary(version: dict[str, Any]) -> str:
    """Produce a non-directional summary that cannot bypass pick gates."""

    if isinstance(resolved_primary_pick(version), dict):
        return "正式主推来自归档门控；其余字段仅描述同一冻结联合分布"
    return "无正式方向；各字段仅报告同一冻结联合分布及分歧"


def structured_evidence_summary(version: dict[str, Any]) -> str:
    """Expose factual evidence coverage without printing free-form betting prose."""

    quality = {
        "high": "高",
        "medium": "中",
        "low": "低",
        "unknown": "未知",
    }.get(str(version.get("data_quality") or "unknown").casefold(), "未知")
    evidence = version.get("guardrail_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    checks = (
        ("首发", "lineup_confirmed"),
        ("基本面", "fundamental_supported"),
        ("机会质量", "chance_quality_supported"),
        ("进攻部署", "attack_configuration_supported"),
        ("对手尾部风险", "opponent_tail_risk_checked"),
    )
    parts = [f"数据质量{quality}"]
    parts.extend(
        f"{label}{'已核' if evidence.get(key) is True else '未确认'}"
        for label, key in checks
    )
    return "；".join(parts)


def half_time_text(version: dict[str, Any], record: dict[str, Any]) -> str:
    pick = version.get("half_time_pick")
    return format_pick("half_time", pick, record) if isinstance(pick, dict) else "观察或无正式推荐"


def htft_text(version: dict[str, Any], record: dict[str, Any]) -> str:
    picks = [pick for pick in version.get("htft_picks", []) if isinstance(pick, dict)]
    if picks:
        return "、".join(format_pick("htft", pick, record) for pick in picks[:2])
    audits = [
        audit
        for audit in version.get("candidate_audits", [])
        if isinstance(audit, dict) and audit.get("market") == "htft"
    ]
    if not audits:
        return "观察或赔率缺失"
    audit = audits[-1]
    top_two = [
        item for item in audit.get("top_two", []) if isinstance(item, dict)
    ][:2]
    if not top_two:
        return "观察或赔率缺失"
    scenarios = "、".join(
        f"{format_pick('htft', item, record)}（{percentage(item.get('probability'))}）"
        for item in top_two
    )
    priority = (
        "market_policy_enabled",
        "complete_current_market",
        "odds_provenance",
        "bookmaker_depth",
        "positive_ev",
        "positive_edge",
        "league_forward_evidence",
        "data_quality",
        "scenario_stability",
        "scenario_coherence",
        "descriptive_pair_mass_threshold",
    )
    failed = {
        str(gate.get("gate"))
        for item in top_two
        for gate in item.get("gates", [])
        if isinstance(gate, dict) and gate.get("passed") is False
    }
    labels = [OBSERVATION_GATE_LABELS[name] for name in priority if name in failed][:3]
    pair_mass = audit.get("pair_probability_mass")
    mass_text = f"，合计{percentage(pair_mass)}" if pair_mass is not None else ""
    failure_text = f"；未通过：{'、'.join(labels)}" if labels else ""
    return f"观察 {scenarios}{mass_text}{failure_text}"


def observation_review_text(record: dict[str, Any]) -> str | None:
    diagnostics = [
        item
        for item in record.get("observation_diagnostics", [])
        if isinstance(item, dict) and item.get("market") == "htft"
    ]
    if not diagnostics:
        return None
    diagnostic = diagnostics[-1]
    if diagnostic.get("status") != "graded_observation":
        return "半全场观察诊断：缺半场比分，未评级（不结算、不计战绩）"
    actual = "/".join(
        HTFT_LABELS.get(char, char)
        for char in str(diagnostic.get("actual_selection") or "")
    )
    top1 = "命中" if diagnostic.get("top1_hit") is True else "未命中"
    top2 = "命中" if diagnostic.get("top2_hit") is True else "未命中"
    return (
        f"半全场观察诊断：实际{actual}，Top1{top1}、Top2{top2}"
        "（不结算、不计战绩）"
    )


def validate_plain_text(lines: list[str]) -> str:
    cleaned = [clean_text(line) for line in lines]
    normalized = "\n".join(line for line in cleaned if line != "无")
    if FORBIDDEN_MARKUP.search(normalized):
        raise ValueError("Generated message contains Markdown or HTML")
    if len(normalized) > 2000:
        raise ValueError("Generated message exceeds 2000 characters")
    if len(normalized.splitlines()) > 18:
        raise ValueError("Generated message exceeds 18 lines")
    return normalized


def render_initial(record: dict[str, Any]) -> str:
    version = merged_version(record, select_version(record, "initial"))
    primary = version.get("primary_pick") if isinstance(version.get("primary_pick"), dict) else {}
    outlook = joint_outlook(version)
    return validate_plain_text([
        f"【初盘分析｜{record.get('match_id')}】",
        f"赛事：{league_display_name(record)}",
        f"比赛：{record.get('home_team')} vs {record.get('away_team')}",
        f"开赛：{format_time(record.get('kickoff'))}",
        f"主推：{primary_line(version, record)}",
        f"主推概率：{percentage(primary.get('probability'))}｜EV {percentage(primary.get('ev'))}",
        f"次选参考：{secondary_picks(version, record)}（不结算、不计战绩、不计金额）",
        f"半场倾向：{outlook['half_time']}",
        f"胜平负：{outlook['one_x_two']}",
        f"总进球：{outlook['goal_range']}",
        f"双方进球：{outlook['btts']}",
        f"联合情景：{outlook['scenarios']}",
        f"模型说明：{structured_analysis_summary(version)}；{outlook['source']}",
        f"证据状态：{structured_evidence_summary(version)}",
        "仅供数据分析参考",
    ])


def render_lineup(record: dict[str, Any]) -> str:
    version = merged_version(record, select_version(record, "lineup-check"))
    primary = version.get("primary_pick") if isinstance(version.get("primary_pick"), dict) else {}
    outlook = joint_outlook(version)
    change = version.get("primary_change") if isinstance(version.get("primary_change"), dict) else {}
    status = change.get("status")
    if status == "maintained":
        change_line = f"主推维持：{primary_line(version, record)}"
    else:
        previous_versions = [item for item in record.get("revisions", []) if isinstance(item, dict)]
        previous = merged_version(record, previous_versions[-1]) if previous_versions else {}
        previous_text = primary_line(previous, record) if previous else "原方向"
        if not primary:
            change_line = f"主推取消：{previous_text} → 不下注"
        else:
            change_line = f"主推变更：{previous_text} → {primary_line(version, record)}"
    return validate_plain_text([
        f"【临场分析｜{record.get('match_id')}】",
        f"赛事：{league_display_name(record)}",
        f"比赛：{record.get('home_team')} vs {record.get('away_team')}",
        f"检查时间：{format_time(record.get('lineup_rechecked_at'))}",
        "比赛状态：赛前，临场版本已归档",
        change_line,
        f"当前主推：{primary_line(version, record)}",
        f"主推概率：{percentage(primary.get('probability'))}｜EV {percentage(primary.get('ev'))}",
        f"次选参考：{secondary_picks(version, record)}（不结算、不计战绩、不计金额）",
        f"半场倾向：{outlook['half_time']}",
        f"胜平负：{outlook['one_x_two']}",
        f"总进球：{outlook['goal_range']}",
        f"双方进球：{outlook['btts']}",
        f"联合情景：{outlook['scenarios']}",
        f"模型说明：{structured_analysis_summary(version)}；{outlook['source']}",
        f"证据状态：{structured_evidence_summary(version)}",
        "仅供数据分析参考",
    ])


def result_text(result: Any) -> str:
    return RESULT_LABELS.get(str(result), "未结算")


def review_secondary_picks(basis: dict[str, Any], record: dict[str, Any]) -> str:
    primary_identity = memory_store.pick_identity(basis.get("primary_market"), basis.get("primary_pick"))
    formal = basis.get("formal_picks") if isinstance(basis.get("formal_picks"), dict) else {}
    values = []
    for market, formal_value in formal.items():
        picks = formal_value if isinstance(formal_value, list) else [formal_value]
        for pick in picks:
            if not isinstance(pick, dict):
                continue
            if memory_store.pick_identity(market, pick) == primary_identity:
                continue
            values.append(format_pick(market, pick, record))
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
    primary_result_line = (
        f"主推：{format_pick(basis.get('primary_market'), primary, record)}＝"
        f"{result_text(record.get('primary_result'))}"
        if primary
        else "主推：无正式推荐（不结算、不计战绩）"
    )
    learning_scope_line = (
        "学习归档：主推复盘样本"
        if primary
        else "学习归档：无主推观察样本（只用于规则与数据质量复核）"
    )
    observation_line = observation_review_text(record)
    return validate_plain_text([
        f"【赛后复盘｜{league_key}｜{record.get('match_id')}】",
        f"比赛：{record.get('home_team')} vs {record.get('away_team')}",
        f"半场：{record.get('half_time_score') or '未取得'}｜全场：{record.get('final_score') or '未取得'}",
        f"结算依据：{basis_label}最终有效推荐",
        primary_result_line,
        learning_scope_line,
        observation_line,
        f"次选参考：{review_secondary_picks(basis, record)}（不结算、不计战绩、不计金额）",
        f"比分参考：{exact_scores(record)}｜命中排名："
        f"{record.get('display_exact_score_hit_rank', record.get('exact_score_hit_rank')) or '未命中'}",
        f"本场关键：{display_text(record, 'key_learning')}",
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

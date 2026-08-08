#!/usr/bin/env python3
"""Render archived soccer-predict results as compact copyable plain text."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import memory_store
import public_market_outlook
import publication_outlook

RESULT_LABELS = {
    "win": "红",
    "half_win": "半红",
    "push": "走",
    "half_loss": "半黑",
    "loss": "黑",
}
HTFT_LABELS = {"H": "主", "D": "平", "A": "客"}
HTFT_SCENARIO_LABELS = {"H": "胜", "D": "平", "A": "负"}
BTTS_LABELS = {"yes": "是", "no": "否"}
MARKET_DIRECTION_TEXT = re.compile(
    r"(?:"
    r"(?:角球|总进球|进球|半场)?[大小]\s*[+-]?\d+(?:\.\d+)?"
    r"|(?:让|受让)\s*[+-]?\d+(?:\.\d+)?"
    r"|@\s*\d+(?:\.\d+)?"
    r")",
    re.IGNORECASE,
)
UNSAFE_LEAGUE_DISPLAY = re.compile(r"[★◇]|主推|推荐|稳胆|必中|必红|…|\.\.\.")
CANONICAL_LEAGUE_DISPLAY_LABELS = {
    "brazil_serie_a": "巴甲",
    "japan_j1": "日职",
    "norway_eliteserien": "挪超",
    "usa_mls": "美职联",
    "finland_veikkausliiga": "芬超",
    "korea_k_league_1": "韩K联",
    "sweden_allsvenskan": "瑞典超",
    "england_premier_league": "英超",
    "england_league_cup": "英联杯",
    "netherlands_eerste_divisie": "荷乙",
    "france_ligue_1": "法甲",
    "spain_la_liga": "西甲",
    "germany_bundesliga": "德甲",
    "italy_serie_a": "意甲",
    "portugal_primeira_liga": "葡超",
    "uefa_champions_league": "欧冠",
    "afc_champions_league": "亚冠",
    "brazil_cup": "巴西杯",
    "uefa_nations_league": "欧国联",
}
LEAGUE_DISPLAY_LABELS = dict(CANONICAL_LEAGUE_DISPLAY_LABELS)
LEAGUE_DISPLAY_LABELS.update(
    {
        key.replace("_", ""): label
        for key, label in CANONICAL_LEAGUE_DISPLAY_LABELS.items()
    }
)
FORBIDDEN_MARKUP = re.compile(
    r"(?:^|\n)\s*(?:#{1,6}\s|[-*+]\s|```|</?(?:html|table|div|p)\b)", re.I
)
ZERO_ZERO_REFERENCE = re.compile(r"0-0", re.IGNORECASE)
ZERO_ZERO_CONTINUATION = re.compile(
    r"^\s*(?:(?:其|该项|对应)\s*)?"
    r"(?:概率|总?排名|全分布|赔率|EV|期望值|未进前二|进入前二|未进\s*Top-?2)",
    re.IGNORECASE,
)

PUBLICATION_BLOCKER_LABELS = {
    "canonical_model_binding": "模型绑定未通过",
    "odds_provenance": "赔率来源或时效未通过",
    "complete_current_market": "当前市场不完整",
    "positive_ev": "当前模型 EV 未为正",
    "positive_edge": "模型相对无水边际未为正",
    "bookmaker_depth": "公司数量不足",
    "data_quality": "数据质量不足",
    "market_signal_classified": "市场信号未完成分类",
    "adverse_signal_gate": "反向或冲突盘口安全检查未通过",
    "market_specific_evidence": "市场专项证据不足",
    "market_policy_enabled": "该市场当前仅允许观察",
    "league_forward_evidence": "联赛前向发布证据不足",
    "upstream_formal_policy": "上游模型尚未开放正式发布",
    "market_availability": "当前市场候选不可用",
    "market_unavailable": "当前市场候选不可用",
    "candidate_evaluation_unavailable": "候选评估不可用",
}
PUBLICATION_REASON_LABELS = {
    "market_source_missing": "缺少盘口来源",
    "price_basis_missing_or_invalid": "缺少共识或中位数价格依据",
    "market_collected_at_missing": "缺少盘口采集时间",
    "market_complete_false": "当前盘口不完整",
    "positive_current_ev_unavailable": "缺少可复算的当前 EV",
    "current_ev_not_positive": "当前 EV 不为正",
    "positive_model_market_edge_unavailable": "缺少可比无水市场边际",
    "model_market_edge_not_positive": "模型相对无水市场边际不为正",
    "medium_or_high_data_quality_required": "数据质量未达到中或高",
    "market_signal_unclassified": "市场信号未完成分类",
    "adverse_market_safety_thresholds_not_met": "反向或冲突盘口安全阈值未通过",
    "attacking_or_chance_quality_evidence_required": (
        "缺少机会质量证据，或确认首发与进攻配置证据"
    ),
    "corner_profile_evidence_required": "缺少独立角球画像证据",
    "deep_favorite_safety_evidence_required": "深盘热门专项安全证据不足",
    "market_observation_only_under_active_policy": "当前政策仅允许该市场观察",
    "league_forward_release_evidence_unavailable": "联赛级前向发布证据不足",
    "upstream_corner_model_remains_non_formal": "角球模型尚未开放正式发布",
    "source_market_identity_unavailable": "当前盘口身份不可用",
    "canonical_corner_observation_missing": "缺少可验证角球模型候选",
    "canonical_decimal_price_snapshot_unavailable": "缺少规范十进制赔率快照",
    "decision_time_price_snapshot_unavailable": "缺少决策时点赔率快照",
    "candidate_evaluation_missing": "该归档版本缺少当前候选评估",
    "candidate_evaluation_legacy_only": "该归档版本只有历史只读候选评估",
    "candidate_evaluation_ambiguous": "该归档版本包含多份冲突候选评估",
    "candidate_evaluation_invalid": "候选评估未通过重放验证",
}
SAFE_MACHINE_CODE = re.compile(r"[a-z0-9_.:-]+", re.IGNORECASE)


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
        pick.get("minimum_goals") if "minimum_goals" in pick else pick.get("min_goals")
    )
    maximum = (
        pick.get("maximum_goals") if "maximum_goals" in pick else pick.get("max_goals")
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

    identity = memory_store.competition_identity_record(record)
    evidence = memory_store.validated_competition_evidence(identity)
    if isinstance(evidence, dict):
        competition = evidence.get("competition")
        if isinstance(competition, dict):
            label = str(competition.get("label") or "").strip()
            if label:
                return _safe_league_display(label)

    raw_key = str(
        identity.get("league_key") or memory_store.league_key_for_record(identity) or ""
    ).strip()
    normalized_key = re.sub(r"[\s-]+", "_", raw_key.casefold())
    mapped = LEAGUE_DISPLAY_LABELS.get(
        normalized_key, LEAGUE_DISPLAY_LABELS.get(normalized_key.replace("_", ""))
    )
    if mapped:
        return _safe_league_display(mapped)

    raw_label = unicodedata.normalize("NFKC", str(identity.get("league") or "")).strip()
    normalized_label = memory_store.normalize_league_name(raw_label)
    normalized_label_key = re.sub(r"[\s-]+", "_", normalized_label.casefold())
    mapped = LEAGUE_DISPLAY_LABELS.get(
        normalized_label_key,
        LEAGUE_DISPLAY_LABELS.get(normalized_label_key.replace("_", "")),
    )
    if mapped:
        return _safe_league_display(mapped)
    if re.search(r"[\u3400-\u9fff]", normalized_label):
        return _safe_league_display(normalized_label)
    if re.search(r"[\u3400-\u9fff]", raw_key):
        return _safe_league_display(raw_key)
    return "赛事待核验"


def _safe_league_display(value: Any) -> str:
    label = unicodedata.normalize("NFKC", str(value or "")).strip()
    if (
        not label
        or len(label) > 24
        or UNSAFE_LEAGUE_DISPLAY.search(label)
        or MARKET_DIRECTION_TEXT.search(label)
        or not re.search(r"[\u3400-\u9fff]", label)
    ):
        return "赛事待核验"
    return label


def format_pick(
    market: str | None, pick: dict[str, Any] | None, record: dict[str, Any]
) -> str:
    if not market or not isinstance(pick, dict):
        return "无正式推荐"
    if market == "asian":
        team = (
            record.get("home_team")
            if pick.get("side") == "home"
            else record.get("away_team")
        )
        return f"{clean_text(team)} {float(pick.get('line', 0)):+g}{price(pick.get('odds'))}"
    if market == "total":
        side = "大" if pick.get("side") == "over" else "小"
        return f"{side}{float(pick.get('line', 0)):g}{price(pick.get('odds'))}"
    if market == "goal_range":
        return f"总进球{goal_range_label(pick)}球{price(pick.get('odds'))}"
    if market == "btts":
        side = {"yes": "是", "no": "否"}.get(
            str(pick.get("side", "")).lower(), clean_text(pick.get("side"))
        )
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
            side = {"home": "半场主胜", "draw": "半场平", "away": "半场客胜"}.get(
                pick.get("side"), "半场"
            )
            return f"{side}{price(pick.get('odds'))}"
        if half_market == "total":
            side = "半场大" if pick.get("side") == "over" else "半场小"
            return f"{side}{float(pick.get('line', 0)):g}{price(pick.get('odds'))}"
        team = (
            record.get("home_team")
            if pick.get("side") == "home"
            else record.get("away_team")
        )
        return f"半场 {clean_text(team)} {float(pick.get('line', 0)):+g}{price(pick.get('odds'))}"
    if market == "htft":
        selection = str(pick.get("selection", "")).upper()
        label = "/".join(HTFT_LABELS.get(char, char) for char in selection)
        return f"{label}{price(pick.get('odds'))}"
    return clean_text(pick)


def _candidate_pick_for_display(candidate: Mapping[str, Any]) -> dict[str, Any]:
    pick = dict(candidate)
    if candidate.get("market") == "half_time":
        pick["market"] = candidate.get("submarket")
    return pick


def _signed_percentage(value: Any) -> str:
    if value is None:
        return "未取得"
    return f"{float(value) * 100:+.1f}%"


def _edge_percentage_points(value: Any) -> str:
    if value is None:
        return "未取得"
    return f"{float(value):+.1f}pp"


def _safe_machine_reason(value: Any) -> str:
    code = str(value or "").strip()
    if not code:
        return "原因未记录"
    translated = PUBLICATION_REASON_LABELS.get(code)
    if translated:
        return translated
    bookmaker_match = re.fullmatch(r"bookmaker_count_below_(\d+)", code)
    if bookmaker_match:
        return f"公司数未达到{bookmaker_match.group(1)}家"
    if SAFE_MACHINE_CODE.fullmatch(code) and "..." not in code:
        return code
    return "未映射原因（详见归档审计）"


def _publication_blocker_text(value: Any) -> str:
    if isinstance(value, Mapping):
        gate = str(value.get("gate") or "").strip()
        label = PUBLICATION_BLOCKER_LABELS.get(gate)
        if label is None:
            label = (
                gate
                if SAFE_MACHINE_CODE.fullmatch(gate) and "..." not in gate
                else "未映射阻断"
            )
        reasons_raw = value.get("reasons")
        reasons = (
            [_safe_machine_reason(item) for item in reasons_raw]
            if isinstance(reasons_raw, (list, tuple))
            else []
        )
        reasons = list(dict.fromkeys(reasons))
        return f"{label}（{'；'.join(reasons)}）" if reasons else label
    return _safe_machine_reason(value)


def _publication_blockers(summary: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    raw = summary.get("blockers")
    raw = raw if isinstance(raw, Mapping) else {}
    output: dict[str, tuple[str, ...]] = {}
    for category in ("data", "value", "policy", "safety"):
        values = (
            summary.get("safety_blockers")
            if category == "safety"
            else raw.get(category)
        )
        normalized = (
            [_publication_blocker_text(item) for item in values]
            if isinstance(values, (list, tuple))
            else []
        )
        output[category] = tuple(dict.fromkeys(normalized))
    return output


def _blocker_line(summary: Mapping[str, Any]) -> str:
    blockers = _publication_blockers(summary)

    def category_text(category: str) -> str:
        values = blockers[category]
        return "、".join(values) if values else "无"

    line = (
        f"未正式发布｜数据阻断：{category_text('data')}｜"
        f"价值阻断：{category_text('value')}｜"
        f"政策阻断：{category_text('policy')}"
    )
    if blockers["safety"]:
        line += f"｜安全阻断：{category_text('safety')}"
    return line


def _publication_stage_text(
    version: dict[str, Any],
    summary: Mapping[str, Any],
    previous: dict[str, Any] | None = None,
) -> str:
    stage = str(summary.get("stage") or version.get("analysis_stage") or "initial")
    if stage == "initial":
        return "初盘结论待 T−30 复核首发与即时盘口"

    transition: Mapping[str, Any] = {}
    if previous is not None:
        raw_transition = publication_outlook.observation_transition(previous, version)
        if isinstance(raw_transition, Mapping):
            transition = raw_transition
    transition_status = str(transition.get("status") or "not_applicable")
    state = str(summary.get("state") or "no_usable_direction")
    primary_change = version.get("primary_change")
    primary_change = primary_change if isinstance(primary_change, Mapping) else {}
    if state == "formal_primary":
        if transition_status == "upgraded_to_formal":
            return "临场已从观察首选升级为正式主推"
        if transition_status == "formalized_other_direction":
            return "临场已发布正式主推，方向不同于原观察首选"
        if primary_change.get("status") == "maintained":
            return "临场正式主推维持"
        if primary_change.get("status") == "changed":
            return "临场正式主推已变更"
        return "临场正式主推已发布"
    if state == "observation_primary":
        return {
            "maintained": "临场仍受政策阻断，观察首选维持",
            "changed": "临场仍受阻断，观察首选已变更",
            "appeared": "临场出现观察首选，但仍未达到正式发布门槛",
            "disappeared": "临场观察首选已消失，当前无可用方向",
            "formal_cancelled_to_observation": "临场正式主推已取消，当前降为观察首选",
        }.get(transition_status, "临场仍受阻断，保持观察")
    return "临场复核后仍无可用方向"


def publication_display(
    version: dict[str, Any], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return one archive-bound publication view shared by text and images."""

    summary = publication_outlook.publication_summary(version)
    if not isinstance(summary, Mapping):
        raise ValueError("publication summary must be an object")
    state = str(summary.get("state") or "")
    if state not in {"formal_primary", "observation_primary", "no_usable_direction"}:
        raise ValueError("publication summary has an unsupported state")

    formal = summary.get("formal_primary")
    formal_label: str | None = None
    formal_pick: dict[str, Any] | None = None
    if isinstance(formal, Mapping):
        market = str(formal.get("market") or "")
        raw_pick = formal.get("pick")
        if not market or not isinstance(raw_pick, Mapping):
            raise ValueError("formal publication summary is incomplete")
        formal_pick = dict(raw_pick)
        formal_label = format_pick(market, formal_pick, version)

    observation = summary.get("observation_primary")
    observation_label: str | None = None
    observation_value: dict[str, Any] | None = None
    if isinstance(observation, Mapping):
        observation_value = dict(observation)
        market = str(observation_value.get("market") or "")
        if not market:
            raise ValueError("observation publication summary has no market")
        observation_label = format_pick(
            market, _candidate_pick_for_display(observation_value), version
        )

    official = summary.get("official_primary")
    official_value: dict[str, Any] | None = None
    official_label: str | None = None
    if isinstance(official, Mapping):
        official_value = dict(official)
        market = str(official_value.get("market") or "")
        if market == "full_time_1x2":
            official_label = {
                "home": f"{version.get('home_team')}胜",
                "draw": "全场平",
                "away": f"{version.get('away_team')}胜",
            }.get(str(official_value.get("side") or ""))
        elif market:
            official_label = format_pick(
                market, _candidate_pick_for_display(official_value), version
            )
        if not official_label:
            raise ValueError("official evaluation primary has no displayable direction")

    if state == "formal_primary" and formal_label is None:
        raise ValueError("formal publication state has no formal primary")
    if state != "formal_primary" and formal_label is not None:
        raise ValueError("non-formal publication state contains a formal primary")
    if state == "observation_primary" and observation_label is None:
        raise ValueError("observation publication state has no observation primary")
    if state != "observation_primary" and observation_label is not None:
        raise ValueError(
            "non-observation publication state contains an observation primary"
        )

    return {
        "state": state,
        "formal_label": formal_label,
        "formal_pick": formal_pick,
        "observation_label": observation_label,
        "observation": observation_value,
        "official_label": official_label,
        "official_primary": official_value,
        "blocker_line": _blocker_line(summary),
        "stage_text": _publication_stage_text(version, summary, previous),
        "audit_status": summary.get("candidate_evaluation_status"),
    }


def format_official_primary(
    value: Mapping[str, Any], context: Mapping[str, Any]
) -> str:
    market = str(value.get("market") or "")
    if market == "full_time_1x2":
        label = {
            "home": f"{context.get('home_team')}胜",
            "draw": "全场平",
            "away": f"{context.get('away_team')}胜",
        }.get(str(value.get("side") or ""))
        if label:
            return label
    if market:
        return format_pick(market, _candidate_pick_for_display(value), dict(context))
    raise ValueError("official evaluation primary is not displayable")


def publication_text_lines(
    version: dict[str, Any], previous: dict[str, Any] | None = None
) -> list[str]:
    view = publication_display(version, previous)
    official_lines: list[str] = []
    if view.get("official_label"):
        official = view.get("official_primary") or {}
        tier_text = {
            "strict_formal": "严格正式",
            "counterfactual_shadow": "观察首选",
            "forced_executable": "强制评测",
            "model_only_1x2": "模型1X2回退",
        }.get(str(official.get("tier") or ""), "评测")
        official_lines = [
            f"评测主推：{view['official_label']}｜层级：{tier_text}",
            "评测口径：每场必选，计独立命中率；非严格正式方向不下注、不计ROI",
        ]
    if view["state"] == "formal_primary":
        pick = view["formal_pick"] or {}
        return official_lines + [
            f"正式主推：{view['formal_label']}",
            "主推指标："
            f"概率 {percentage(pick.get('probability'))}｜"
            f"EV {_signed_percentage(pick.get('ev'))}｜"
            f"edge {_edge_percentage_points(pick.get('edge_pp'))}",
            f"发布状态：{view['stage_text']}",
        ]
    if view["state"] == "observation_primary":
        observation = view["observation"] or {}
        return official_lines + [
            "正式主推：无",
            f"◇ 观察首选：{view['observation_label']}｜"
            f"模型 EV {_signed_percentage(observation.get('ev'))}｜"
            f"edge {_edge_percentage_points(observation.get('edge_pp'))}｜"
            "不下注、不计战绩",
            str(view["blocker_line"]),
            f"发布状态：{view['stage_text']}",
        ]
    return official_lines + [
        "正式主推：无",
        "— 无可用方向",
        str(view["blocker_line"]),
        f"发布状态：{view['stage_text']}",
    ]


def publication_panel_lines(
    version: dict[str, Any],
    identifier: str,
    previous: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    view = publication_display(version, previous)
    official_lines: tuple[str, ...] = ()
    if view.get("official_label"):
        official = view.get("official_primary") or {}
        official_lines = (
            f"[{identifier}] ★ 评测主推：{view['official_label']}",
            "每场必选评测｜非严格正式方向不下注、不计ROI",
            f"评测层级：{official.get('tier')}",
        )
    if view["state"] == "formal_primary":
        return official_lines + (
            f"[{identifier}] 正式主推已发布：{view['formal_label']}",
            f"发布状态：{view['stage_text']}",
        )
    if view["state"] == "observation_primary":
        observation = view["observation"] or {}
        return official_lines + (
            f"[{identifier}] ◇ 观察首选：{view['observation_label']}",
            f"模型 EV {_signed_percentage(observation.get('ev'))}｜"
            f"相对无水边际 {_edge_percentage_points(observation.get('edge_pp'))}｜"
            "不下注、不计战绩",
            str(view["blocker_line"]),
            f"发布状态：{view['stage_text']}",
        )
    return official_lines + (
        f"[{identifier}] — 无可用方向",
        str(view["blocker_line"]),
        f"发布状态：{view['stage_text']}",
    )


def version_candidates(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in record.get("revisions", []) if isinstance(item, dict)] + [
        record
    ]


def select_version(record: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == "initial":
        candidates = [
            item
            for item in version_candidates(record)
            if item.get("analysis_stage", "initial") == "initial"
        ]
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
    if version is not record and "candidate_audits" not in version:
        merged["candidate_audits"] = []
    # A reviewed top-level record may bind its final candidate audit through
    # settlement_basis.  That later binding must not authorize an earlier
    # revision; a revision may only restore a settlement basis of its own.
    if version is not record:
        merged.pop("settlement_basis", None)
    merged.update(version)
    return merged


def publication_baseline_version(
    record: dict[str, Any], stage: str
) -> dict[str, Any] | None:
    """Return the frozen initial version used for public lineup transitions."""

    if stage != "lineup-check":
        return None
    try:
        selected = select_version(record, "initial")
    except ValueError:
        return None
    return merged_version(record, selected)


def _top_probability(value: Any, order: tuple[str, ...]) -> tuple[str, float] | None:
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
        "goal_range_marginal_top1": "数据不足",
        "btts": "数据不足",
        "scenarios": "数据不足",
        "joint_concentration": "数据不足",
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


def _format_joint_concentration(scenarios: Mapping[str, Any]) -> str:
    top_two = float(scenarios["top2_cumulative_probability"])
    other = float(scenarios["other_scenarios_probability"])
    uncertainty = scenarios["uncertainty"]
    if (
        not isinstance(uncertainty, Mapping)
        or uncertainty.get("schema_version")
        != public_market_outlook.JOINT_UNCERTAINTY_SCHEMA_VERSION
        or uncertainty.get("policy") != public_market_outlook.JOINT_UNCERTAINTY_POLICY
    ):
        raise ValueError("joint uncertainty policy is missing or unsupported")
    normalized_entropy = float(uncertainty["normalized_entropy"])
    label = clean_text(uncertainty.get("label_zh"))
    if (
        not math.isfinite(top_two)
        or not math.isfinite(other)
        or not math.isfinite(normalized_entropy)
        or abs(top_two + other - 1.0) > 1e-9
        or not 0.0 <= normalized_entropy <= 1.0
        or label not in {"低", "中", "高"}
    ):
        raise ValueError("joint uncertainty summary is invalid")
    policy_version = str(uncertainty["schema_version"]).split(".", 1)[0]
    return (
        f"Top2累计{percentage(top_two)}｜其他情景{percentage(other)}｜"
        f"不确定度{label}（归一化熵{percentage(normalized_entropy)}，政策v{policy_version}）"
    )


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
        path_text = " / ".join(
            f"{''.join(HTFT_SCENARIO_LABELS[char] for char in item['htft'])} + "
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
        marginal_audit = public["goal_range_marginal_audit"]
        if (
            not isinstance(marginal_audit, Mapping)
            or marginal_audit.get("label") != "总进球边际第一"
            or marginal_audit.get("role") != "marginal_distribution_audit_only"
            or marginal_audit.get("replaces_joint_scenario") is not False
        ):
            raise ValueError("goal-range marginal audit contract is invalid")
        marginal_goal_range = marginal_audit["top1"]
        concentration = _format_joint_concentration(scenarios)
        return {
            "half_time": _format_public_market(
                markets["half_time"], {"H": "主胜", "D": "平", "A": "客胜"}
            ),
            "one_x_two": _format_public_market(
                markets["one_x_two"],
                {"home": "主胜", "draw": "平", "away": "客胜"},
            ),
            "goal_range": (
                f"{scenario_items[0]['goal_range_label']}（冻结联合第1名比分映射）"
            ),
            "goal_range_marginal_top1": (
                f"{clean_text(marginal_goal_range['label'])}"
                f" {percentage(marginal_goal_range['probability'])}"
                "（边际分布第一，仅审计，不替代联合首选情景）"
            ),
            "btts": _format_public_market(markets["btts"]),
            "scenarios": (
                "联合事件 Top 2（半全场＋波胆逐行同源，"
                f"按联合概率排序，高方差，不作推荐）：{path_text}；{concentration}"
            ),
            "joint_concentration": concentration,
            "source": source,
        }
    except (
        KeyError,
        TypeError,
        ValueError,
        public_market_outlook.PublicMarketOutlookError,
    ):
        return _insufficient_joint_outlook()


def model_leader_reference(version: dict[str, Any]) -> str:
    """Return a separately qualified observation, never a marginal fallback."""

    if isinstance(resolved_primary_pick(version), dict):
        return "无"
    observations = qualified_observation_references(version)
    if observations:
        return f"◇ 观察方向：{observations[0]}（不计主推、不计战绩）"
    return "无"


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
    hidden = {
        index
        for index, clause in enumerate(clauses)
        if ZERO_ZERO_REFERENCE.search(clause)
    }
    for index in tuple(hidden):
        next_index = index + 1
        while next_index < len(clauses) and ZERO_ZERO_CONTINUATION.search(
            clauses[next_index]
        ):
            hidden.add(next_index)
            next_index += 1
        previous_index = index - 1
        while previous_index >= 0 and ZERO_ZERO_CONTINUATION.search(
            clauses[previous_index]
        ):
            hidden.add(previous_index)
            previous_index -= 1
    return (
        "".join(
            clause for index, clause in enumerate(clauses) if index not in hidden
        ).strip()
        or "无"
    )


def primary_line(version: dict[str, Any], record: dict[str, Any]) -> str:
    return format_pick(
        version.get("primary_market"), resolved_primary_pick(version), record
    )


def resolved_primary_pick(version: dict[str, Any]) -> dict[str, Any] | None:
    pick = version.get("primary_pick")
    if not isinstance(pick, dict):
        return None
    if version.get("primary_market") == "half_time" and pick.get("market") not in {
        "1x2",
        "asian",
        "total",
    }:
        half_time_pick = version.get("half_time_pick")
        if isinstance(half_time_pick, dict):
            return half_time_pick
    return pick


def qualified_observation_references(
    version: dict[str, Any],
) -> tuple[str, ...]:
    """Return only independently validated, qualified corner observations."""

    raw_audits = version.get("candidate_audits")
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
        if not valid or audit.get("kind") != memory_store.CORNER_OBSERVATION_KIND:
            continue
        best = audit.get("best_observation")
        if (
            not isinstance(best, dict)
            or best.get("diagnostic_qualification_status") != "qualified"
        ):
            continue
        market = str(best.get("market") or "")
        if not market:
            continue
        label = format_pick(market, best, version)
        if label and label != "无正式推荐" and label not in labels:
            labels.append(label)
    return tuple(labels)


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
        ranked_values.append((rank, sequence_index, format_pick(market, pick, record)))
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
    return (
        format_pick("half_time", pick, record)
        if isinstance(pick, dict)
        else "观察或无正式推荐"
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
    outlook = joint_outlook(version)
    return validate_plain_text(
        [
            f"【初盘分析｜{version.get('match_id')}】",
            f"赛事：{league_display_name(version)}",
            f"比赛：{version.get('home_team')} vs {version.get('away_team')}",
            f"开赛：{format_time(version.get('kickoff'))}",
            *publication_text_lines(version),
            f"次选参考：{secondary_picks(version, version)}（不结算、不计战绩、不计金额）",
            f"半场倾向：{outlook['half_time']}",
            f"胜平负：{outlook['one_x_two']}",
            f"联合首选情景总球：{outlook['goal_range']}｜总进球边际第一：{outlook['goal_range_marginal_top1']}",
            f"双方进球：{outlook['btts']}",
            f"联合情景：{outlook['scenarios']}",
            f"模型说明：{structured_analysis_summary(version)}；{outlook['source']}",
            f"证据状态：{structured_evidence_summary(version)}",
            "仅供数据分析参考",
        ]
    )


def render_lineup(record: dict[str, Any]) -> str:
    version = merged_version(record, select_version(record, "lineup-check"))
    outlook = joint_outlook(version)
    primary = resolved_primary_pick(version)
    change_previous_versions = [
        item for item in record.get("revisions", []) if isinstance(item, dict)
    ]
    change_previous = (
        merged_version(record, change_previous_versions[-1])
        if change_previous_versions
        else {}
    )
    publication_previous = publication_baseline_version(record, "lineup-check")
    change = (
        version.get("primary_change")
        if isinstance(version.get("primary_change"), dict)
        else {}
    )
    status = change.get("status")
    if status == "maintained":
        change_line = f"主推维持：{primary_line(version, version)}"
    else:
        previous_text = (
            primary_line(change_previous, change_previous)
            if change_previous
            else "原方向"
        )
        if not primary:
            change_line = f"主推取消：{previous_text} → 不下注"
        else:
            change_line = (
                f"主推变更：{previous_text} → {primary_line(version, version)}"
            )
    publication_lines = publication_text_lines(version, publication_previous)
    publication_lines[-1] = f"{change_line}｜{publication_lines[-1]}"
    return validate_plain_text(
        [
            f"【临场分析｜{version.get('match_id')}】",
            f"赛事：{league_display_name(version)}",
            f"比赛：{version.get('home_team')} vs {version.get('away_team')}",
            f"检查时间：{format_time(version.get('lineup_rechecked_at'))}",
            "比赛状态：赛前，临场版本已归档",
            *publication_lines,
            f"次选参考：{secondary_picks(version, version)}（不结算、不计战绩、不计金额）",
            f"半场倾向：{outlook['half_time']}",
            f"胜平负：{outlook['one_x_two']}",
            f"联合首选情景总球：{outlook['goal_range']}｜总进球边际第一：{outlook['goal_range_marginal_top1']}",
            f"双方进球：{outlook['btts']}",
            f"联合情景：{outlook['scenarios']}",
            f"模型说明：{structured_analysis_summary(version)}；{outlook['source']}",
            f"证据状态：{structured_evidence_summary(version)}",
            "仅供数据分析参考",
        ]
    )


def result_text(result: Any) -> str:
    return RESULT_LABELS.get(str(result), "未结算")


def review_secondary_picks(basis: dict[str, Any], record: dict[str, Any]) -> str:
    primary_identity = memory_store.pick_identity(
        basis.get("primary_market"), basis.get("primary_pick")
    )
    formal = (
        basis.get("formal_picks") if isinstance(basis.get("formal_picks"), dict) else {}
    )
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
    league_key = memory_store.competition_key_for_record(record)
    league = stats["leagues"].get(league_key, {})
    league_label = league_display_name(record)
    basis = (
        record.get("settlement_basis")
        if isinstance(record.get("settlement_basis"), dict)
        else memory_store.settlement_basis_for_record(record)
    )
    basis_label = (
        "临场版" if basis.get("analysis_stage") == "lineup-check" else "初盘版"
    )
    primary = (
        basis.get("primary_pick") if isinstance(basis.get("primary_pick"), dict) else {}
    )
    primary_result_line = (
        f"主推：{format_pick(basis.get('primary_market'), primary, record)}＝"
        f"{result_text(record.get('primary_result'))}"
        if primary
        else "主推：无正式推荐（不结算、不计战绩）"
    )
    official = basis.get("official_primary")
    official_settlement = record.get("official_primary_settlement")
    official_result_line = (
        f"评测主推：{format_official_primary(official, record)}＝"
        f"{result_text(official_settlement.get('result'))}"
        f"｜层级 {official.get('tier')}｜只计独立命中率、不计ROI"
        if isinstance(official, Mapping) and isinstance(official_settlement, Mapping)
        else "评测主推：旧归档未启用（不回填）"
    )
    learning_scope_line = (
        "学习归档：主推复盘样本"
        if primary
        else "学习归档：无主推观察样本（只用于规则与数据质量复核）"
    )
    outlook = joint_outlook(record)
    return validate_plain_text(
        [
            f"【赛后复盘｜{league_label}｜{record.get('match_id')}】",
            f"比赛：{record.get('home_team')} vs {record.get('away_team')}",
            f"半场：{record.get('half_time_score') or '未取得'}｜全场：{record.get('final_score') or '未取得'}",
            f"结算依据：{basis_label}最终有效推荐",
            official_result_line,
            primary_result_line,
            learning_scope_line,
            f"次选参考：{review_secondary_picks(basis, record)}（不结算、不计战绩、不计金额）",
            f"联合首选情景总球：{outlook['goal_range']}｜总进球边际第一：{outlook['goal_range_marginal_top1']}",
            f"联合情景：{outlook['scenarios']}",
            f"本场关键：{display_text(record, 'key_learning')}",
            f"{league_label}主推：{performance_text(league.get('primary'))}",
            f"累计主推：{performance_text(stats.get('primary'))}",
            "复盘用于校准分析，不代表未来收益",
        ]
    )


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
    parser.add_argument(
        "--base-dir", help="Workspace root; defaults to current directory"
    )
    parser.add_argument("--match-id", required=True)
    parser.add_argument(
        "--kind", choices=("initial", "lineup-check", "review"), required=True
    )
    return parser


def main() -> int:
    memory_store.configure_stdio()
    args = build_parser().parse_args()
    try:
        print(render(args.base_dir, args.match_id, args.kind))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

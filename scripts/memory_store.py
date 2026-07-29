#!/usr/bin/env python3
"""Deterministic workspace-local storage for soccer-predict."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRIMARY_MARKETS = (
    "asian",
    "total",
    "half_time",
    "htft",
    "goal_range",
    "btts",
    "corner_total",
    "corner_handicap",
)
NEW_FORMAL_MARKETS = ("goal_range", "btts", "corner_total", "corner_handicap")
CORNER_MARKETS = ("corner_total", "corner_handicap")
PICK_KEY_BY_MARKET = {
    "asian": "asian_pick",
    "total": "total_pick",
    "half_time": "half_time_pick",
    "htft": "htft_picks",
    "goal_range": "goal_range_pick",
    "btts": "btts_pick",
    "corner_total": "corner_total_pick",
    "corner_handicap": "corner_handicap_pick",
}
RESULT_KEY_BY_MARKET = {
    "asian": "asian_result",
    "total": "total_result",
    "half_time": "half_time_result",
    "goal_range": "goal_range_result",
    "btts": "btts_result",
    "corner_total": "corner_total_result",
    "corner_handicap": "corner_handicap_result",
}
ADVERSE_FORMAL_MIN_EV = 0.08
ADVERSE_FORMAL_MIN_EDGE_PP = 4.0
PROVISIONAL_MIN_FIRMS = 5
PROVISIONAL_CORNER_MIN_FIRMS = 3
LINEUP_CHANGE_MIN_CONFIDENCE_DELTA = 5.0
EV_AUDIT_TOLERANCE = 0.0005
PROBABILITY_AUDIT_TOLERANCE = 1e-4
EDGE_AUDIT_TOLERANCE_PP = 0.1
DEEP_FAVORITE_LINE = -0.75
CONFIDENCE_RANKING_VERSION = "stability-v1"
PRIMARY_SELECTION_BASIS = "highest_stability_adjusted_confidence"
ADVERSE_MARKET_SIGNALS = {"against", "conflicting"}
OBSOLETE_GUARDRAILS = {
    "小样本保护期内，所有正式方向（主推和正式次推）都必须满足EV>=8%、模型相对市场边际>=4pp且数据质量至少为medium。EV在5%-8%的方向只作观察，不得归档为正式方向。",
    "小样本保护期内，亚洲盘和大小球正式方向必须满足EV>=8%、模型相对市场边际>=4pp且数据质量至少为medium；EV在5%-8%的方向只作观察，不得归档为正式方向。",
    "让球方达到-0.75或更深时，主推必须使用独立净胜球/穿盘分布，且具备确认首发、机会质量证据与对手尾部风险检查；不得用1X2胜率、强阵容或控球优势直接替代穿盘概率。",
    "临场主推变更必须记录原因。跨市场、反向或追更差盘口时，原主推须被硬信息证伪，新方向数据质量须为high、首发已确认，且当前EV至少比旧方向高4pp；否则维持原主推或取消主推，不强行寻找替代方向。",
    "小样本保护期内，所有市场主推必须满足EV>=8%、模型相对市场边际>=4pp且数据质量至少为medium；亚洲盘和大小球正式次推同样受此门槛约束。EV在5%-8%的方向只作观察，不得归档为正式方向。",
    "若亚盘与相关欧赔一致明显反向，常规低EV方向降级为观察；只有EV>=8%、边际>=4pp、至少5家公司且有独立阵容或基本面证据时才能正式推荐。",
    "精确比分仅作比赛形态参考，不计入主推命中率。",
    "两个精确比分候选仅作比赛形态参考；分别记录Top-1/Top-2诊断，不计入主推或全部正式方向的命中率与ROI。",
}
DEFAULT_GUARDRAILS = [
    "stability-v1：普通正式方向不再要求EV>=8%；但当前EV与模型相对市场边际都必须严格为正，数据质量至少为medium，且市场与专项证据完整。",
    "所有安全合格方向按结算稳定性55%、EV强度10%、边际强度10%、数据质量10%、市场深度5%、独立证据5%、市场一致性5%计算综合置信度；唯一rank=1可作主推。",
    "盘口与相关欧赔明显反向或冲突时，仍须EV>=8%、边际>=4pp、至少5家公司且有独立阵容或基本面支持，主推与正式次推均不例外。",
    "临场换推以当前综合置信度为准：原主推未失效时，新方向至少高5分；原主推被硬信息证伪时可取消或换为新的安全rank=1方向。",
    "深盘、大小球、伤停冲突与精确比分继续执行专项保护；没有安全候选时允许无主推，不强行下注。",
]
LEAGUE_ALIASES = {
    "韩国K联": "韩K联",
    "韩国K联赛": "韩K联",
    "K联赛": "韩K联",
}
LEAGUE_STAGE_SUFFIX = re.compile(
    r"(?:"
    r"(?:常规赛|小组赛|资格赛|预选赛|附加赛)?第?\d+(?:轮|周|阶段)|"
    r"(?:1/16|1/8|1/4)决赛|十六强|八强|四分之一决赛|半决赛|决赛"
    r")$"
)


def data_path(base_dir: str | None) -> Path:
    base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd().resolve()
    return base / ".codex" / "soccer-predict" / "history.json"


def calibration_path(base_dir: str | None) -> Path:
    return data_path(base_dir).with_name("calibration.json")


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"History must be a JSON array: {path}")
    return data


def save_history(path: Path, history: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def configure_stdio() -> None:
    """Keep JSON output readable on Windows consoles that default to CP932."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Datetime must include timezone: {value}")
    return parsed.astimezone(timezone.utc)


def normalize_league_name(value: Any) -> str:
    """Return a stable league key while preserving the raw label elsewhere."""
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw:
        return "unknown"
    compact = re.sub(r"\s+", "", raw)
    compact = re.sub(r"^(?:19|20)\d{2}(?:[-/](?:19|20)?\d{2})?", "", compact)
    previous = None
    while compact and compact != previous:
        previous = compact
        compact = LEAGUE_STAGE_SUFFIX.sub("", compact)
    compact = compact.strip("-_/·")
    return LEAGUE_ALIASES.get(compact, compact or raw)


def league_key_for_record(record: dict[str, Any]) -> str:
    return normalize_league_name(record.get("league_key") or record.get("league"))


def split_line(line: float) -> tuple[float, float]:
    rounded = round(line * 4)
    if not math.isclose(line * 4, rounded, abs_tol=1e-7):
        raise ValueError(f"Line must use quarter-goal increments: {line}")
    if abs(rounded) % 2 == 1:
        return line - 0.25, line + 0.25
    return line, line


def label_result(value: float) -> str:
    if math.isclose(value, 1.0):
        return "win"
    if math.isclose(value, 0.5):
        return "half_win"
    if math.isclose(value, 0.0):
        return "push"
    if math.isclose(value, -0.5):
        return "half_loss"
    return "loss"


def settle_components(values: tuple[float, float]) -> str:
    score = sum(1.0 if v > 0 else 0.0 if math.isclose(v, 0.0) else -1.0 for v in values) / 2
    return label_result(score)


def settle_asian(pick: dict[str, Any] | None, home: int, away: int) -> str | None:
    if not pick:
        return None
    side = pick["side"]
    margin = home - away if side == "home" else away - home
    a, b = split_line(float(pick["line"]))
    return settle_components((margin + a, margin + b))


def settle_total(pick: dict[str, Any] | None, home: int, away: int) -> str | None:
    if not pick:
        return None
    total = home + away
    a, b = split_line(float(pick["line"]))
    if pick["side"] == "over":
        return settle_components((total - a, total - b))
    return settle_components((a - total, b - total))


def parse_goal_range_selection(value: str) -> dict[str, Any]:
    selection = re.sub(r"\s+", "", str(value or ""))
    inclusive = re.fullmatch(r"(\d+)-(\d+)", selection)
    if inclusive:
        minimum, maximum = (int(part) for part in inclusive.groups())
        if minimum > maximum:
            raise ValueError("Goal-range lower bound cannot exceed its upper bound")
        return {
            "selection": f"{minimum}-{maximum}",
            "minimum_goals": minimum,
            "maximum_goals": maximum,
        }
    or_more = re.fullmatch(r"(\d+)\+", selection)
    if or_more:
        minimum = int(or_more.group(1))
        return {
            "selection": f"{minimum}+",
            "minimum_goals": minimum,
            "maximum_goals": None,
        }
    raise ValueError("Goal range must be inclusive MIN-MAX or N+, for example 2-3 or 7+")


def settle_goal_range(
    pick: dict[str, Any] | None, home: int, away: int
) -> str | None:
    if not pick:
        return None
    total = home + away
    minimum = pick.get("minimum_goals")
    maximum = pick.get("maximum_goals")
    if minimum is None:
        parsed = parse_goal_range_selection(str(pick.get("selection", "")))
        minimum = parsed["minimum_goals"]
        maximum = parsed["maximum_goals"]
    hit = total >= int(minimum) and (
        maximum is None or total <= int(maximum)
    )
    return "win" if hit else "loss"


def settle_btts(pick: dict[str, Any] | None, home: int, away: int) -> str | None:
    if not pick:
        return None
    actual = home > 0 and away > 0
    expected = str(pick.get("side", "")).lower() == "yes"
    return "win" if actual == expected else "loss"


def settle_corner_total(
    pick: dict[str, Any] | None, home_corners: int, away_corners: int
) -> str | None:
    return settle_total(pick, home_corners, away_corners)


def settle_corner_handicap(
    pick: dict[str, Any] | None, home_corners: int, away_corners: int
) -> str | None:
    return settle_asian(pick, home_corners, away_corners)


def result_code(home: int, away: int) -> str:
    if home > away:
        return "H"
    if home < away:
        return "A"
    return "D"


def settle_half_time(pick: dict[str, Any] | None, home: int, away: int) -> str | None:
    if not pick:
        return None
    market = pick.get("market")
    if market == "1x2":
        expected = {"home": "H", "draw": "D", "away": "A"}.get(pick.get("side"))
        return "win" if expected == result_code(home, away) else "loss"
    if market == "asian":
        return settle_asian(pick, home, away)
    if market == "total":
        return settle_total(pick, home, away)
    return None


def settle_htft(picks: list[dict[str, Any]] | None, half_home: int, half_away: int, home: int, away: int) -> list[str]:
    actual = result_code(half_home, half_away) + result_code(home, away)
    return ["win" if str(pick.get("selection", "")).upper() == actual else "loss" for pick in (picks or [])]


def parse_htft_pick(
    value: str, odds_format: str | None = None
) -> dict[str, Any]:
    parts = [part.strip() for part in value.split(":")]
    if len(parts) != 4:
        raise ValueError("HT/FT pick must be SELECTION:ODDS:PROBABILITY:EV, for example DD:3.40:0.31:0.054")
    selection = parts[0].upper()
    if selection not in {a + b for a in "HDA" for b in "HDA"}:
        raise ValueError(f"Invalid HT/FT selection: {selection}")
    pick = {
        "selection": selection,
        "odds": float(parts[1]),
        "probability": float(parts[2]),
        "ev": float(parts[3]),
    }
    if odds_format is not None:
        pick["odds_format"] = odds_format
    return pick


def parse_exact_score_pick(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.split(":")]
    if len(parts) != 2:
        raise ValueError("Exact-score pick must be SCORE:PROBABILITY, for example 2-1:0.126")
    score_parts = parts[0].split("-")
    if len(score_parts) != 2 or not all(part.isdigit() for part in score_parts):
        raise ValueError(f"Invalid exact score: {parts[0]}")
    home, away = (int(part) for part in score_parts)
    probability = float(parts[1])
    if not 0.0 <= probability <= 1.0:
        raise ValueError("Exact-score probability must be between 0 and 1")
    return {"score": f"{home}-{away}", "probability": probability}


def parse_display_exact_score_pick(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.split(":")]
    if len(parts) != 3:
        raise ValueError(
            "Display exact-score pick must be "
            "SCORE:PROBABILITY:UNCONDITIONAL_RANK"
        )
    pick = parse_exact_score_pick(":".join(parts[:2]))
    unconditional_rank = int(parts[2])
    if unconditional_rank < 1:
        raise ValueError("Display exact-score unconditional rank must be at least 1")
    pick["unconditional_rank"] = unconditional_rank
    return pick


def build_display_exact_score_picks(
    args: argparse.Namespace,
    exact_score_picks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    supplied = getattr(args, "display_exact_score_pick", None) or []
    if not supplied:
        display = deepcopy(exact_score_picks)
        for pick in display:
            pick["display_rank"] = int(pick["rank"])
            pick["unconditional_rank"] = int(pick["rank"])
            pick["conditional_probability"] = float(pick["probability"])
            pick["status"] = "unconditional_scenario"
        return display, {
            "version": "unconditional-top-two-v1",
            "basis": "unconditional_top_two",
            "market": None,
            "side": None,
            "line": None,
            "event": None,
            "event_probability": 1.0,
        }

    if len(supplied) != 2:
        raise ValueError(
            "Primary-conditioned display requires exactly two "
            "--display-exact-score-pick values"
        )
    display = [parse_display_exact_score_pick(value) for value in supplied]
    if len({pick["score"] for pick in display}) != 2:
        raise ValueError("Display exact-score picks must contain two distinct scores")
    if len({pick["unconditional_rank"] for pick in display}) != 2:
        raise ValueError("Display exact-score unconditional ranks must be distinct")
    if getattr(args, "primary_market", None) != "total":
        raise ValueError(
            "Primary-conditioned display exact scores currently require a total primary"
        )
    side = getattr(args, "total_side", None)
    line = getattr(args, "total_line", None)
    if side not in {"over", "under"} or line is None:
        raise ValueError(
            "Primary-conditioned display exact scores require total side and line"
        )
    line = float(line)
    event_probability = getattr(
        args, "display_exact_score_event_probability", None
    )
    if event_probability is None:
        raise ValueError(
            "Primary-conditioned display exact scores require "
            "--display-exact-score-event-probability"
        )
    event_probability = float(event_probability)
    if not 0.0 < event_probability <= 1.0:
        raise ValueError("Display exact-score event probability must be in (0, 1]")

    for pick in display:
        home, away = (int(part) for part in pick["score"].split("-"))
        total_goals = home + away
        supports_primary = (
            total_goals > line if side == "over" else total_goals < line
        )
        if not supports_primary:
            raise ValueError(
                f"Display exact score {pick['score']} does not support "
                f"the net-profit branch of {side} {line:g}"
            )
        if float(pick["probability"]) > event_probability + 1e-9:
            raise ValueError(
                "Display exact-score probability cannot exceed event probability"
            )

    display.sort(key=lambda pick: (-float(pick["probability"]), pick["score"]))
    for rank, pick in enumerate(display, start=1):
        pick["display_rank"] = rank
        pick["conditional_probability"] = (
            float(pick["probability"]) / event_probability
        )
        pick["status"] = "primary_conditioned_scenario"
    return display, {
        "version": "primary-conditioned-v1",
        "basis": "primary_total_net_profit",
        "market": "total",
        "side": side,
        "line": line,
        "event": "net_profit",
        "event_probability": event_probability,
    }


def build_zero_zero_audit(
    args: argparse.Namespace,
    exact_score_picks: list[dict[str, Any]],
) -> dict[str, Any]:
    probability = getattr(args, "zero_zero_probability", None)
    rank = getattr(args, "zero_zero_rank", None)
    if probability is None or rank is None:
        raise ValueError(
            "Record requires --zero-zero-probability and --zero-zero-rank "
            "to prove that 0-0 was evaluated"
        )
    probability = float(probability)
    rank = int(rank)
    if not 0.0 <= probability <= 1.0:
        raise ValueError("0-0 probability must be between 0 and 1")
    if rank < 1:
        raise ValueError("0-0 rank must be at least 1")

    zero_zero_pick = next(
        (pick for pick in exact_score_picks if pick.get("score") == "0-0"),
        None,
    )
    if rank <= 2:
        if not zero_zero_pick:
            raise ValueError("0-0 ranked in the Top-2 must be an exact-score pick")
        if int(zero_zero_pick.get("rank", 0)) != rank:
            raise ValueError("0-0 audit rank must match its exact-score pick rank")
        if abs(float(zero_zero_pick.get("probability", 0.0)) - probability) > 1e-4:
            raise ValueError("0-0 audit probability must match its exact-score pick")
    else:
        if zero_zero_pick:
            raise ValueError("0-0 outside the Top-2 cannot replace a higher-ranked pick")
        second_probability = float(exact_score_picks[1]["probability"])
        if probability > second_probability + 1e-4:
            raise ValueError("0-0 probability exceeds the archived second-ranked score")

    odds = getattr(args, "zero_zero_odds", None)
    if odds is not None:
        odds = float(odds)
        if odds <= 1.0:
            raise ValueError("0-0 decimal odds must be greater than 1")
    supplied_ev = getattr(args, "zero_zero_ev", None)
    calculated_ev = probability * odds - 1.0 if odds is not None else None
    if supplied_ev is not None:
        supplied_ev = float(supplied_ev)
        if calculated_ev is None:
            raise ValueError("0-0 EV requires --zero-zero-odds")
        if abs(supplied_ev - calculated_ev) > 1e-4:
            raise ValueError("0-0 EV must equal probability * decimal odds - 1")

    return {
        "score": "0-0",
        "probability": probability,
        "rank": rank,
        "included_in_top2": rank <= 2,
        "status": "top_two" if rank <= 2 else "analyzed_not_top_two",
        "odds": odds,
        "ev": round(calculated_ev, 12) if calculated_ev is not None else None,
    }


def find_record(history: list[dict[str, Any]], match_id: str) -> dict[str, Any] | None:
    return next((item for item in history if str(item.get("match_id")) == str(match_id)), None)


def formal_picks(record: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    picks: list[tuple[str, dict[str, Any]]] = []
    for market in (
        "asian",
        "total",
        "half_time",
        "goal_range",
        "btts",
        "corner_total",
        "corner_handicap",
    ):
        pick = record.get(PICK_KEY_BY_MARKET[market])
        if isinstance(pick, dict):
            picks.append((market, pick))
    for pick in record.get("htft_picks", []):
        if isinstance(pick, dict):
            picks.append(("htft", pick))
    return picks


def require_minimum(value: Any, minimum: float, label: str) -> float:
    if value is None:
        raise ValueError(f"{label} is required by the provisional formal-pick guardrail")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if number + 1e-9 < minimum:
        raise ValueError(f"{label} must be at least {minimum:g}; downgrade the candidate to observation")
    return number


def require_strictly_positive(value: Any, label: str) -> float:
    if value is None:
        raise ValueError(f"{label} is required by the formal-pick safety gate")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if number <= 0.0:
        raise ValueError(
            f"{label} must be greater than 0; downgrade the candidate to observation"
        )
    return number


def htft_implied_edge_pp(pick: dict[str, Any]) -> float | None:
    probability = pick.get("probability")
    odds = pick.get("odds")
    if probability is None or odds is None:
        return None
    price = float(odds)
    odds_format = pick.get("odds_format")
    decimal_price = 1.0 + price if odds_format == "hong_kong" else price
    if decimal_price <= 1.0:
        return None
    return (float(probability) - 1.0 / decimal_price) * 100.0


def effective_edge_pp(
    record: dict[str, Any], market: str, pick: dict[str, Any]
) -> float | None:
    value = pick.get("edge_pp")
    if value is not None:
        return float(value)
    if market == "htft":
        if pick.get("role") == "primary":
            supplied = record.get("guardrail_evidence", {}).get(
                "primary_htft_edge_pp"
            )
            if supplied is not None:
                return float(supplied)
        return htft_implied_edge_pp(pick)
    return None


def effective_firm_count(
    record: dict[str, Any], market: str, pick: dict[str, Any]
) -> float | None:
    value = pick.get("firm_count")
    if value is not None:
        return float(value)
    if market == "htft":
        supplied = record.get("guardrail_evidence", {}).get(
            "primary_htft_firm_count"
        )
        return float(supplied) if supplied is not None else None
    return None


def effective_market_signal(market: str, pick: dict[str, Any]) -> str:
    if market == "htft":
        return "neutral"
    return str(pick.get("market_signal") or "unknown")


def validate_basic_formal_pick(
    record: dict[str, Any], market: str, pick: dict[str, Any]
) -> None:
    if record.get("data_quality") not in {"medium", "high"}:
        raise ValueError(f"{market} formal pick requires medium or high data quality")

    probability = pick.get("probability")
    if (
        probability is None
        or not math.isfinite(float(probability))
        or not 0.0 <= float(probability) <= 1.0
    ):
        raise ValueError(
            f"{market} probability is required and must be between 0 and 1"
        )
    odds = pick.get("odds")
    if odds is None or not math.isfinite(float(odds)) or float(odds) <= 0.0:
        raise ValueError(f"{market} requires a positive current executable price")
    require_strictly_positive(pick.get("ev"), f"{market} EV")
    edge_pp = require_strictly_positive(
        effective_edge_pp(record, market, pick),
        f"{market} model-versus-market edge (pp)",
    )
    firm_count = require_minimum(
        effective_firm_count(record, market, pick),
        1,
        f"{market} bookmaker count",
    )
    if market == "asian" and (
        pick.get("side") not in {"home", "away"} or pick.get("line") is None
    ):
        raise ValueError("Asian formal pick requires side home/away and a current line")
    if market == "total" and (
        pick.get("side") not in {"over", "under"} or pick.get("line") is None
    ):
        raise ValueError("Total formal pick requires side over/under and a current line")
    if market == "half_time" and not pick.get("market"):
        raise ValueError("Half-time formal pick requires a complete market")
    if market == "htft" and not pick.get("selection"):
        raise ValueError("HT/FT formal pick requires a selection")

    signal = effective_market_signal(market, pick)
    if signal not in {"aligned", "neutral", "against", "conflicting"}:
        raise ValueError(
            f"{market} formal pick requires a known aligned, neutral, against, "
            "or conflicting market signal"
        )
    if signal in ADVERSE_MARKET_SIGNALS:
        require_minimum(pick.get("ev"), ADVERSE_FORMAL_MIN_EV, f"{market} adverse-signal EV")
        require_minimum(
            edge_pp,
            ADVERSE_FORMAL_MIN_EDGE_PP,
            f"{market} adverse-signal model-versus-market edge (pp)",
        )
        require_minimum(
            firm_count,
            PROVISIONAL_MIN_FIRMS,
            f"{market} adverse-signal bookmaker count",
        )
        evidence = record.get("guardrail_evidence", {})
        if not (
            evidence.get("lineup_confirmed")
            or evidence.get("fundamental_supported")
        ):
            raise ValueError(
                f"{market} adverse-market formal pick requires independent lineup "
                "or fundamental evidence"
            )


def validate_odds_format(pick: dict[str, Any], market: str) -> None:
    odds_format = pick.get("odds_format")
    if odds_format not in {"decimal", "hong_kong"}:
        raise ValueError(
            f"{market} formal pick requires explicit odds_format decimal or hong_kong"
        )
    odds = pick.get("odds")
    if odds is None:
        raise ValueError(f"{market} odds are required for a complete formal market")
    price = float(odds)
    if not math.isfinite(price):
        raise ValueError(f"{market} odds must be finite")
    minimum = 1.0 if odds_format == "decimal" else 0.0
    if price <= minimum:
        qualifier = "greater than 1" if odds_format == "decimal" else "positive"
        raise ValueError(f"{market} {odds_format} odds must be {qualifier}")


def audited_win_profit(pick: dict[str, Any]) -> float:
    price = float(pick["odds"])
    return price - 1.0 if pick["odds_format"] == "decimal" else price


def validate_market_audit_fields(pick: dict[str, Any], market: str) -> None:
    market_probability = pick.get("market_probability")
    if (
        market_probability is None
        or not math.isfinite(float(market_probability))
        or not 0.0 <= float(market_probability) <= 1.0
    ):
        raise ValueError(
            f"{market} market_probability is required and must be between 0 and 1"
        )
    source = str(pick.get("market_source") or "").strip()
    if not source:
        raise ValueError(f"{market} market_source is required")
    collected_at = str(pick.get("market_collected_at") or "").strip()
    if not collected_at:
        raise ValueError(f"{market} market_collected_at is required")
    try:
        parse_datetime(collected_at)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{market} market_collected_at must be a parseable datetime with timezone"
        ) from exc
    if pick.get("price_basis") not in {"consensus", "median"}:
        raise ValueError(f"{market} price_basis must be consensus or median")

    expected_edge = (
        float(pick["probability"]) - float(market_probability)
    ) * 100.0
    if abs(float(pick["edge_pp"]) - expected_edge) > EDGE_AUDIT_TOLERANCE_PP:
        raise ValueError(
            f"{market} edge_pp must equal "
            "(probability - market_probability) * 100"
        )
    require_strictly_positive(
        expected_edge,
        f"{market} recalculated model-versus-market edge (pp)",
    )


def validate_binary_ev(pick: dict[str, Any], market: str) -> float:
    probability = float(pick["probability"])
    calculated = probability * (1.0 + audited_win_profit(pick)) - 1.0
    if abs(float(pick["ev"]) - calculated) > EV_AUDIT_TOLERANCE:
        raise ValueError(
            f"{market} EV does not match probability and {pick['odds_format']} odds"
        )
    return calculated


def validate_corner_distribution(pick: dict[str, Any], market: str) -> float:
    distribution = pick.get("settlement_probabilities")
    if not isinstance(distribution, dict):
        raise ValueError(
            f"{market} requires a complete five-state settlement probability distribution"
        )
    states = ("full_win", "half_win", "push", "half_loss", "loss")
    values: dict[str, float] = {}
    for state in states:
        value = distribution.get(state)
        if (
            value is None
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ValueError(
                f"{market} settlement probability {state} is required "
                "and must be between 0 and 1"
            )
        values[state] = float(value)
    if abs(sum(values.values()) - 1.0) > PROBABILITY_AUDIT_TOLERANCE:
        raise ValueError(f"{market} settlement probabilities must sum to 1")
    quarter_units = abs(int(round(float(pick["line"]) * 4))) % 4
    allowed_states = {
        0: {"full_win", "push", "loss"},
        1: {"full_win", "half_win", "half_loss", "loss"},
        2: {"full_win", "loss"},
        3: {"full_win", "half_win", "half_loss", "loss"},
    }[quarter_units]
    unreachable = [
        state
        for state, value in values.items()
        if state not in allowed_states and value > PROBABILITY_AUDIT_TOLERANCE
    ]
    if unreachable:
        line = float(pick["line"])
        raise ValueError(
            f"{market} line {line:g} cannot produce settlement states: "
            + ", ".join(unreachable)
        )
    positive_probability = values["full_win"] + values["half_win"]
    if (
        abs(float(pick["probability"]) - positive_probability)
        > PROBABILITY_AUDIT_TOLERANCE
    ):
        raise ValueError(
            f"{market} probability must equal full_win + half_win probability"
        )
    win_profit = audited_win_profit(pick)
    calculated_ev = (
        values["full_win"] * win_profit
        + values["half_win"] * win_profit / 2.0
        - values["half_loss"] / 2.0
        - values["loss"]
    )
    if abs(float(pick["ev"]) - calculated_ev) > EV_AUDIT_TOLERANCE:
        raise ValueError(
            f"{market} EV does not match its five-state settlement distribution "
            f"and {pick['odds_format']} odds"
        )
    return calculated_ev


def validate_new_formal_pick(
    market: str,
    pick: dict[str, Any],
    record: dict[str, Any],
) -> None:
    evidence = record.get("guardrail_evidence", {})
    if pick.get("market_complete") is not True:
        raise ValueError(
            f"{market} formal pick requires explicit market_complete=true"
        )
    validate_odds_format(pick, market)
    validate_market_audit_fields(pick, market)

    if market == "goal_range":
        parsed = parse_goal_range_selection(str(pick.get("selection", "")))
        if (
            pick.get("minimum_goals") != parsed["minimum_goals"]
            or pick.get("maximum_goals") != parsed["maximum_goals"]
        ):
            raise ValueError("Goal-range bounds do not match its selection")
    elif market == "btts":
        if pick.get("side") not in {"yes", "no"}:
            raise ValueError("BTTS formal pick requires side yes or no")
    elif market == "corner_total":
        if pick.get("side") not in {"over", "under"} or pick.get("line") is None:
            raise ValueError(
                "Corner-total formal pick requires side over/under and a line"
            )
        split_line(float(pick["line"]))
    elif market == "corner_handicap":
        if pick.get("side") not in {"home", "away"} or pick.get("line") is None:
            raise ValueError(
                "Corner-handicap formal pick requires side home/away and a line"
            )
        split_line(float(pick["line"]))

    if market in {"goal_range", "btts"} and not (
        evidence.get("chance_quality_supported")
        or (
            evidence.get("lineup_confirmed")
            and evidence.get("attack_configuration_supported")
        )
    ):
        raise ValueError(
            f"{market} formal pick requires attacking-configuration or "
            "chance-quality evidence"
        )
    if market in {"goal_range", "btts"}:
        calculated_ev = validate_binary_ev(pick, market)
        require_strictly_positive(
            calculated_ev,
            f"{market} recalculated EV",
        )
    if market in CORNER_MARKETS:
        calculated_ev = validate_corner_distribution(pick, market)
        require_strictly_positive(
            calculated_ev,
            f"{market} recalculated EV",
        )
        if not evidence.get("corner_profile_supported"):
            raise ValueError(
                f"{market} formal pick requires independent corner-profile evidence"
            )
        require_minimum(
            pick.get("firm_count"),
            PROVISIONAL_CORNER_MIN_FIRMS,
            f"{market} bookmaker count",
        )

def validate_provisional_formal_guardrails(record: dict[str, Any]) -> None:
    """Reject formal picks that do not qualify for the stability-v1 safe pool."""
    evidence = record.get("guardrail_evidence", {})
    data_quality = str(record.get("data_quality") or "unknown")
    all_formal_picks = formal_picks(record)

    if all_formal_picks and data_quality not in {"medium", "high"}:
        raise ValueError("Formal picks require medium or high data quality")
    if all_formal_picks and evidence.get("injury_evidence_status") == "stale_conflict":
        raise ValueError(
            "Stale injury evidence conflicts with the confirmed lineup; recalculate without it "
            "or archive no formal pick"
        )

    for market, pick in all_formal_picks:
        validate_basic_formal_pick(record, market, pick)
        if market in NEW_FORMAL_MARKETS:
            validate_new_formal_pick(market, pick, record)

        if market == "htft":
            require_minimum(
                effective_firm_count(record, market, pick),
                PROVISIONAL_MIN_FIRMS,
                "HT/FT bookmaker count",
            )
        if market == "total":
            require_minimum(
                pick.get("firm_count"),
                PROVISIONAL_MIN_FIRMS,
                "total bookmaker count",
            )
            supported_attack = bool(
                evidence.get("chance_quality_supported")
                or (
                    evidence.get("lineup_confirmed")
                    and evidence.get("attack_configuration_supported")
                )
            )
            if not supported_attack:
                raise ValueError(
                    "Total formal pick requires chance-quality evidence or a confirmed "
                    "attacking configuration; price movement alone is insufficient"
                )
        if (
            market == "asian"
            and float(pick.get("line", 0.0)) <= DEEP_FAVORITE_LINE
        ):
            if data_quality != "high":
                raise ValueError(
                    "Asian favorite -0.75 or deeper requires high data quality"
                )
            if not evidence.get("lineup_confirmed"):
                raise ValueError(
                    "Asian favorite -0.75 or deeper requires confirmed lineups"
                )
            chance_supported = bool(evidence.get("chance_quality_supported"))
            consensus_supported = bool(
                effective_market_signal(market, pick) == "aligned"
                and float(pick.get("firm_count") or 0) >= PROVISIONAL_MIN_FIRMS
                and evidence.get("fundamental_supported")
                and evidence.get("attack_configuration_supported")
            )
            if not (chance_supported or consensus_supported):
                raise ValueError(
                    "Asian favorite -0.75 or deeper requires independent chance-quality "
                    "evidence, or aligned 5-firm consensus plus confirmed attacking "
                    "configuration and fundamental support"
                )
            if not evidence.get("opponent_tail_risk_checked"):
                raise ValueError(
                    "Asian favorite -0.75 or deeper requires an opponent counterattack/"
                    "goalkeeper/set-piece tail-risk check"
                )
            if not pick.get("cover_distribution_validated"):
                raise ValueError(
                    "Asian favorite -0.75 or deeper requires an independently validated "
                    "goal-margin/cover distribution"
                )
            cover_probability = pick.get("cover_probability")
            if (
                cover_probability is None
                or not 0.0 <= float(cover_probability) <= 1.0
            ):
                raise ValueError(
                    "Asian favorite -0.75 or deeper requires --asian-cover-probability "
                    "between 0 and 1"
                )


def settlement_safety(
    pick: dict[str, Any],
) -> tuple[float, str]:
    distribution = pick.get("settlement_probabilities")
    if isinstance(distribution, dict):
        try:
            loss = float(distribution["loss"])
            half_loss = float(distribution["half_loss"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            safety = 1.0 - loss - half_loss / 2.0
            return max(0.0, min(1.0, safety)), "five_state_no_loss"
    probability = float(pick.get("probability") or 0.0)
    return max(0.0, min(1.0, probability)), "model_probability_fallback"


def evidence_coverage(
    record: dict[str, Any], market: str, pick: dict[str, Any]
) -> float:
    evidence = record.get("guardrail_evidence", {})
    if market in {"total", "goal_range", "btts"}:
        return 1.0 if (
            evidence.get("chance_quality_supported")
            or (
                evidence.get("lineup_confirmed")
                and evidence.get("attack_configuration_supported")
            )
        ) else 0.0
    if market in CORNER_MARKETS:
        return 1.0 if evidence.get("corner_profile_supported") else 0.0
    relevant = (
        bool(evidence.get("lineup_confirmed")),
        bool(evidence.get("fundamental_supported")),
        bool(evidence.get("chance_quality_supported")),
    )
    return sum(relevant) / len(relevant)


def confidence_components(
    record: dict[str, Any], market: str, pick: dict[str, Any]
) -> dict[str, Any]:
    safety, safety_source = settlement_safety(pick)
    ev = max(0.0, float(pick.get("ev") or 0.0))
    edge = max(0.0, float(effective_edge_pp(record, market, pick) or 0.0))
    firms = max(0.0, float(effective_firm_count(record, market, pick) or 0.0))
    data_factor = 1.0 if record.get("data_quality") == "high" else 0.75
    evidence_factor = evidence_coverage(record, market, pick)
    signal = effective_market_signal(market, pick)
    alignment_factor = {
        "aligned": 1.0,
        "neutral": 0.75,
        "against": 0.0,
        "conflicting": 0.0,
    }.get(signal, 0.0)
    points = {
        "settlement_safety": 55.0 * safety,
        "ev_strength": 10.0 * min(ev / ADVERSE_FORMAL_MIN_EV, 1.0),
        "edge_strength": 10.0 * min(
            edge / ADVERSE_FORMAL_MIN_EDGE_PP, 1.0
        ),
        "data_quality": 10.0 * data_factor,
        "market_depth": 5.0 * min(firms / PROVISIONAL_MIN_FIRMS, 1.0),
        "independent_evidence": 5.0 * evidence_factor,
        "market_alignment": 5.0 * alignment_factor,
    }
    return {
        "score": round(sum(points.values()), 4),
        "settlement_safety_probability": round(safety, 6),
        "safety_source": safety_source,
        "ev": round(ev, 6),
        "edge_pp": round(edge, 4),
        "firm_count": int(firms) if firms.is_integer() else firms,
        "market_signal": signal,
        "points": {key: round(value, 4) for key, value in points.items()},
    }


def annotate_confidence_ranking(record: dict[str, Any]) -> None:
    picks = formal_picks(record)
    record["confidence_ranking_version"] = CONFIDENCE_RANKING_VERSION
    if not picks:
        record["primary_selection_basis"] = "no_safe_formal_candidate"
        return

    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for order, (market, pick) in enumerate(picks):
        components = confidence_components(record, market, pick)
        pick["confidence_score"] = components["score"]
        pick["confidence_components"] = components
        pick["confidence_ranking_version"] = CONFIDENCE_RANKING_VERSION
        ranked.append((order, market, pick))

    ranked.sort(
        key=lambda item: (
            -float(item[2]["confidence_score"]),
            -float(
                item[2]["confidence_components"][
                    "settlement_safety_probability"
                ]
            ),
            -float(item[2]["confidence_components"]["firm_count"]),
            -float(item[2]["confidence_components"]["edge_pp"]),
            -float(item[2]["confidence_components"]["ev"]),
            item[1],
            str(
                item[2].get("selection")
                or f"{item[2].get('side')}:{item[2].get('line')}"
            ),
            item[0],
        )
    )
    for rank, (_, _, pick) in enumerate(ranked, start=1):
        pick["confidence_rank"] = rank
    record["primary_selection_basis"] = PRIMARY_SELECTION_BASIS


def validate_primary_is_rank_one(record: dict[str, Any]) -> None:
    primary_market = record.get("primary_market")
    primary = record.get("primary_pick")
    if primary_market is None:
        return
    if not isinstance(primary, dict) or primary.get("confidence_rank") != 1:
        best = next(
            (
                (market, pick)
                for market, pick in formal_picks(record)
                if pick.get("confidence_rank") == 1
            ),
            None,
        )
        best_label = best[0] if best else "unknown"
        raise ValueError(
            f"Primary pick must be the unique stability-v1 confidence rank 1; "
            f"current rank-1 market is {best_label}"
        )


def same_primary_direction(
    previous_market: Any,
    previous: dict[str, Any] | None,
    current_market: Any,
    current: dict[str, Any] | None,
) -> bool:
    if (
        not isinstance(previous, dict)
        or not isinstance(current, dict)
        or previous_market != current_market
    ):
        return False
    if previous_market == "htft":
        return str(previous.get("selection", "")).upper() == str(
            current.get("selection", "")
        ).upper()
    if previous_market == "goal_range":
        return previous.get("selection") == current.get("selection")
    if previous_market == "half_time" and previous.get("market") != current.get("market"):
        return False
    return previous.get("side") == current.get("side")


def selected_line_worsened(
    market: Any,
    previous: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> bool:
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    old_line = previous.get("line")
    new_line = current.get("line")
    if old_line is None or new_line is None:
        return False
    old_value = float(old_line)
    new_value = float(new_line)
    if market in {"asian", "corner_handicap"}:
        return new_value < old_value
    if market in {"total", "corner_total"}:
        return (
            current.get("side") == "over" and new_value > old_value
        ) or (
            current.get("side") == "under" and new_value < old_value
        )
    if market == "half_time" and current.get("market") in {"asian", "total"}:
        if current.get("market") == "asian":
            return new_value < old_value
        return (
            current.get("side") == "over" and new_value > old_value
        ) or (
            current.get("side") == "under" and new_value < old_value
        )
    return False


def build_primary_change(
    record: dict[str, Any],
    existing: dict[str, Any] | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    previous_identity = active_primary_identity(existing)
    current_identity = active_primary_identity(record)
    current_primary = record.get("primary_pick")
    current_ev = (
        float(current_primary["ev"])
        if isinstance(current_primary, dict) and current_primary.get("ev") is not None
        else None
    )
    current_confidence = (
        float(current_primary["confidence_score"])
        if isinstance(current_primary, dict)
        and current_primary.get("confidence_score") is not None
        else None
    )

    if record.get("analysis_stage") != "lineup-check":
        return {
            "status": "initial",
            "previous": None,
            "current": list(current_identity) if current_identity else None,
            "reason": None,
            "previous_current_ev": None,
            "new_current_ev": current_ev,
            "ev_improvement": None,
            "previous_current_confidence": None,
            "new_current_confidence": current_confidence,
            "confidence_improvement": None,
            "decision": "initial",
            "guardrail_passed": True,
        }
    if not existing:
        raise ValueError("A lineup-check archive requires an existing initial prediction")
    if previous_identity == current_identity:
        return {
            "status": "maintained",
            "previous": list(previous_identity) if previous_identity else None,
            "current": list(current_identity) if current_identity else None,
            "reason": str(getattr(args, "primary_change_reason", "") or "").strip() or None,
            "previous_current_ev": None,
            "new_current_ev": current_ev,
            "ev_improvement": None,
            "previous_current_confidence": getattr(
                args, "previous_primary_current_confidence", None
            ),
            "new_current_confidence": current_confidence,
            "confidence_improvement": None,
            "decision": "maintained",
            "guardrail_passed": True,
        }

    reason = str(getattr(args, "primary_change_reason", "") or "").strip()
    if not reason:
        raise ValueError("A changed lineup-check primary requires --primary-change-reason")

    previous_market = existing.get("primary_market")
    previous_primary = existing.get("primary_pick")
    current_market = record.get("primary_market")
    if current_identity is None:
        return {
            "status": "changed",
            "previous": list(previous_identity) if previous_identity else None,
            "current": None,
            "reason": reason,
            "previous_current_ev": getattr(args, "previous_primary_current_ev", None),
            "new_current_ev": None,
            "ev_improvement": None,
            "previous_current_confidence": getattr(
                args, "previous_primary_current_confidence", None
            ),
            "new_current_confidence": None,
            "confidence_improvement": None,
            "decision": "cancelled_to_none",
            "guardrail_passed": True,
        }

    evidence = record.get("guardrail_evidence", {})
    if record.get("data_quality") != "high":
        raise ValueError("A changed lineup-check primary requires high data quality")
    if not evidence.get("lineup_confirmed"):
        raise ValueError("A changed lineup-check primary requires confirmed lineups")

    same_direction = same_primary_direction(
        previous_market,
        previous_primary if isinstance(previous_primary, dict) else None,
        current_market,
        current_primary if isinstance(current_primary, dict) else None,
    )
    worse_line = same_direction and selected_line_worsened(
        current_market,
        previous_primary if isinstance(previous_primary, dict) else None,
        current_primary if isinstance(current_primary, dict) else None,
    )
    if worse_line and not bool(getattr(args, "accept_worse_line", False)):
        raise ValueError(
            "The lineup-check line is worse for the same selection; maintain the archived "
            "line or pass --accept-worse-line after the strict replacement gate is met"
        )

    previous_ev = getattr(args, "previous_primary_current_ev", None)
    ev_improvement = (
        current_ev - float(previous_ev)
        if current_ev is not None and previous_ev is not None
        else None
    )
    previous_confidence = getattr(
        args, "previous_primary_current_confidence", None
    )
    confidence_improvement = (
        current_confidence - float(previous_confidence)
        if current_confidence is not None and previous_confidence is not None
        else None
    )
    previous_invalidated = bool(
        getattr(args, "previous_primary_invalidated", False)
    )
    if previous_identity is not None and not previous_invalidated:
        if previous_confidence is None:
            raise ValueError(
                "A lineup-check primary change requires "
                "--previous-primary-current-confidence unless the old thesis is "
                "explicitly invalidated"
            )
        if current_confidence is None:
            raise ValueError(
                "The new lineup-check primary requires a current confidence score"
            )
        if (
            confidence_improvement is None
            or confidence_improvement + 1e-9
            < LINEUP_CHANGE_MIN_CONFIDENCE_DELTA
        ):
            raise ValueError(
                "The new lineup-check primary confidence must exceed the previous "
                "direction by at least 5 points unless the old thesis is invalidated"
            )

    decision = (
        "newly_qualified"
        if previous_identity is None
        else "worse_line_replaced"
        if worse_line
        else "same_direction_line_improved"
        if same_direction
        else "strict_replacement"
    )
    return {
        "status": "changed",
        "previous": list(previous_identity) if previous_identity else None,
        "current": list(current_identity),
        "reason": reason,
        "previous_invalidated": previous_invalidated,
        "previous_current_ev": float(previous_ev) if previous_ev is not None else None,
        "new_current_ev": current_ev,
        "ev_improvement": ev_improvement,
        "previous_current_confidence": (
            float(previous_confidence)
            if previous_confidence is not None
            else None
        ),
        "new_current_confidence": current_confidence,
        "confidence_improvement": confidence_improvement,
        "worse_line": worse_line,
        "decision": decision,
        "guardrail_passed": True,
    }


def pick_identity(market: str | None, pick: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if not market or not isinstance(pick, dict):
        return None
    if market == "htft":
        return (market, str(pick.get("selection", "")).upper())
    if market == "goal_range":
        return (market, pick.get("selection"))
    if market == "half_time":
        return (market, pick.get("market"), pick.get("side"), pick.get("line"))
    return (market, pick.get("side"), pick.get("line"))


def resolve_formal_pick(
    record: dict[str, Any], market: str, htft_selection: str | None = None
) -> dict[str, Any] | None:
    if market not in PRIMARY_MARKETS:
        raise ValueError(f"Unknown primary market: {market}")
    if market != "htft":
        pick = record.get(PICK_KEY_BY_MARKET[market])
        return pick if isinstance(pick, dict) else None
    picks = [pick for pick in record.get("htft_picks", []) if isinstance(pick, dict)]
    if htft_selection:
        wanted = htft_selection.upper()
        return next((pick for pick in picks if str(pick.get("selection", "")).upper() == wanted), None)
    if len(picks) == 1:
        return picks[0]
    return None


def apply_primary_role(
    record: dict[str, Any], primary_market: str | None, htft_selection: str | None = None
) -> None:
    picks = formal_picks(record)
    for _, pick in picks:
        pick["role"] = "secondary"

    if primary_market in {None, "none"}:
        if picks:
            raise ValueError("--primary-market none is valid only when there are no formal picks")
        record["primary_market"] = None
        record["primary_pick"] = None
        return

    selected = resolve_formal_pick(record, primary_market, htft_selection)
    if selected is None:
        suffix = f" ({htft_selection})" if htft_selection else ""
        raise ValueError(f"Primary pick {primary_market}{suffix} is not present among formal picks")
    selected["role"] = "primary"
    snapshot = deepcopy(selected)
    snapshot["market"] = primary_market
    snapshot["role"] = "primary"
    record["primary_market"] = primary_market
    record["primary_pick"] = snapshot


def active_primary_identity(record: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if not record:
        return None
    market = record.get("primary_market")
    primary = record.get("primary_pick")
    return pick_identity(str(market) if market else None, primary if isinstance(primary, dict) else None)


def primary_snapshot_for_stats(
    record: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Return the immutable settlement primary when it exists.

    Reviewed records may later have mutable top-level fields repaired or migrated.
    Official statistics must remain tied to the active pre-match version frozen at
    settlement time.
    """
    basis = record.get("settlement_basis")
    if isinstance(basis, dict):
        market = basis.get("primary_market")
        pick = basis.get("primary_pick")
        return (
            str(market) if market else None,
            pick if isinstance(pick, dict) else None,
        )
    market = record.get("primary_market")
    pick = record.get("primary_pick")
    return (
        str(market) if market else None,
        pick if isinstance(pick, dict) else None,
    )


def primary_result_from_record(record: dict[str, Any]) -> str | None:
    market, primary = primary_snapshot_for_stats(record)
    if not market or not isinstance(primary, dict):
        return None
    if market in RESULT_KEY_BY_MARKET:
        result = record.get(RESULT_KEY_BY_MARKET[str(market)])
        return str(result) if result else None
    if market == "htft":
        selection = str(primary.get("selection", "")).upper()
        basis = record.get("settlement_basis")
        formal = basis.get("formal_picks", {}) if isinstance(basis, dict) else {}
        picks = (
            formal.get("htft", [])
            if isinstance(formal, dict)
            else record.get("htft_picks", [])
        )
        for result, pick in zip(record.get("htft_results", []), picks):
            if isinstance(pick, dict) and str(pick.get("selection", "")).upper() == selection:
                return str(result) if result else None
    return None


def primary_result_for_stats(record: dict[str, Any]) -> str | None:
    """Use the frozen market's result whenever a settlement basis exists."""
    basis = record.get("settlement_basis")
    if isinstance(basis, dict):
        frozen_result = basis.get("primary_result")
        if frozen_result:
            return str(frozen_result)
        result = primary_result_from_record(record)
        if result:
            return result
        # Compatibility for older HT/FT settlement bases that predate a
        # dedicated frozen result and did not persist a parallel results list.
        if basis.get("primary_market") == "htft" and record.get("primary_result"):
            return str(record["primary_result"])
        return None
    result = record.get("primary_result")
    return str(result) if result else primary_result_from_record(record)


def revision_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_stage": record.get("analysis_stage", "initial"),
        "archived_at": record.get("updated_at", record.get("created_at")),
        "predicted_score": record.get("predicted_score"),
        "exact_score_picks": record.get("exact_score_picks", []),
        "display_predicted_score": record.get("display_predicted_score"),
        "display_exact_score_picks": record.get("display_exact_score_picks", []),
        "display_exact_score_basis": record.get("display_exact_score_basis"),
        "zero_zero_audit": record.get("zero_zero_audit"),
        "recommendation": record.get("recommendation"),
        "notes": record.get("notes"),
        "data_quality": record.get("data_quality", "unknown"),
        "guardrail_evidence": record.get("guardrail_evidence", {}),
        "probabilities": record.get("probabilities"),
        "asian_pick": record.get("asian_pick"),
        "total_pick": record.get("total_pick"),
        "half_time_pick": record.get("half_time_pick"),
        "htft_picks": record.get("htft_picks", []),
        "goal_range_pick": record.get("goal_range_pick"),
        "btts_pick": record.get("btts_pick"),
        "corner_total_pick": record.get("corner_total_pick"),
        "corner_handicap_pick": record.get("corner_handicap_pick"),
        "confidence_ranking_version": record.get("confidence_ranking_version"),
        "primary_selection_basis": record.get("primary_selection_basis"),
        "primary_market": record.get("primary_market"),
        "primary_pick": record.get("primary_pick"),
        "primary_change": record.get("primary_change"),
    }


def settlement_basis_for_record(record: dict[str, Any]) -> dict[str, Any]:
    """Freeze the final active pre-match version used for official settlement."""
    stage = str(record.get("analysis_stage") or "initial")
    if stage not in {"initial", "lineup-check"}:
        raise ValueError(f"Unsupported active analysis stage for settlement: {stage}")
    if record.get("lineup_rechecked_at") and stage != "lineup-check":
        raise ValueError("Lineup recheck exists but the active record is not the lineup-check version")
    return {
        "policy": "latest_active_prematch_version",
        "grading_scope": "primary_only",
        "analysis_stage": stage,
        "version_archived_at": record.get("updated_at", record.get("created_at")),
        "lineup_rechecked_at": record.get("lineup_rechecked_at"),
        "confidence_ranking_version": record.get("confidence_ranking_version"),
        "primary_selection_basis": record.get("primary_selection_basis"),
        "primary_market": record.get("primary_market"),
        "primary_pick": deepcopy(record.get("primary_pick")),
        "formal_picks": {
            "asian": deepcopy(record.get("asian_pick")),
            "total": deepcopy(record.get("total_pick")),
            "half_time": deepcopy(record.get("half_time_pick")),
            "htft": deepcopy(record.get("htft_picks", [])),
            "goal_range": deepcopy(record.get("goal_range_pick")),
            "btts": deepcopy(record.get("btts_pick")),
            "corner_total": deepcopy(record.get("corner_total_pick")),
            "corner_handicap": deepcopy(record.get("corner_handicap_pick")),
        },
        "predicted_score": record.get("predicted_score"),
        "exact_score_picks": deepcopy(record.get("exact_score_picks", [])),
        "display_predicted_score": record.get("display_predicted_score"),
        "display_exact_score_picks": deepcopy(
            record.get("display_exact_score_picks", [])
        ),
        "display_exact_score_basis": deepcopy(
            record.get("display_exact_score_basis")
        ),
        "zero_zero_audit": deepcopy(record.get("zero_zero_audit")),
        "revision_count": len(record.get("revisions", [])),
    }


def snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != "archived_at"}


def cmd_record(args: argparse.Namespace) -> dict[str, Any]:
    path = data_path(args.base_dir)
    history = load_history(path)
    existing = find_record(history, args.match_id)
    if existing and existing.get("status") == "reviewed" and not args.force:
        raise ValueError("Reviewed record exists; use --force only when intentionally replacing it")

    timestamp = now_iso()
    revisions = list(existing.get("revisions", [])) if existing else []
    exact_score_picks = [parse_exact_score_pick(value) for value in (args.exact_score_pick or [])]
    if len(exact_score_picks) != 2:
        raise ValueError("Record requires exactly two --exact-score-pick values")
    if len({pick["score"] for pick in exact_score_picks}) != 2:
        raise ValueError("Exact-score picks must contain two distinct scores")
    if sum(float(pick["probability"]) for pick in exact_score_picks) > 1.0 + 1e-9:
        raise ValueError("Exact-score probabilities cannot sum to more than 1")
    exact_score_picks.sort(key=lambda pick: (-float(pick["probability"]), pick["score"]))
    for rank, pick in enumerate(exact_score_picks, start=1):
        pick["rank"] = rank
        pick["status"] = "scenario_only"
    if str(args.predicted_score).strip() != exact_score_picks[0]["score"]:
        raise ValueError("--predicted-score must equal the highest-probability exact-score pick")
    zero_zero_audit = build_zero_zero_audit(args, exact_score_picks)
    display_exact_score_picks, display_exact_score_basis = (
        build_display_exact_score_picks(args, exact_score_picks)
    )
    zero_zero_audit["included_in_display_top2"] = any(
        pick.get("score") == "0-0" for pick in display_exact_score_picks
    )

    record: dict[str, Any] = {
        "match_id": str(args.match_id),
        "mode": "prematch",
        "status": "pending",
        "analysis_stage": args.analysis_stage,
        "league": args.league,
        "league_key": normalize_league_name(args.league),
        "kickoff": args.kickoff,
        "home_team": args.home_team,
        "away_team": args.away_team,
        "predicted_score": args.predicted_score,
        "exact_score_picks": exact_score_picks,
        "display_predicted_score": display_exact_score_picks[0]["score"],
        "display_exact_score_picks": display_exact_score_picks,
        "display_exact_score_basis": display_exact_score_basis,
        "zero_zero_audit": zero_zero_audit,
        "recommendation": args.recommendation,
        "source_url": args.source_url,
        "notes": args.notes,
        "data_quality": args.data_quality,
        "guardrail_evidence": {
            "lineup_confirmed": bool(getattr(args, "lineup_confirmed", False)),
            "fundamental_supported": bool(
                getattr(args, "fundamental_evidence", False)
            ),
            "chance_quality_supported": bool(
                getattr(args, "chance_quality_evidence", False)
            ),
            "attack_configuration_supported": bool(
                getattr(args, "attack_configuration_evidence", False)
            ),
            "opponent_tail_risk_checked": bool(
                getattr(args, "opponent_tail_risk_checked", False)
            ),
            "corner_profile_supported": bool(
                getattr(args, "corner_profile_evidence", False)
            ),
            "injury_evidence_status": getattr(
                args, "injury_evidence_status", "not_used"
            ),
            "primary_htft_edge_pp": getattr(
                args, "primary_htft_edge_pp", None
            ),
            "primary_htft_firm_count": getattr(
                args, "primary_htft_firm_count", None
            ),
        },
        "probabilities": {
            "home_win": args.home_win_probability,
            "draw": args.draw_probability,
            "away_win": args.away_win_probability,
        },
        "created_at": existing.get("created_at", timestamp) if existing else timestamp,
        "updated_at": timestamp,
        "lineup_rechecked_at": timestamp if args.analysis_stage == "lineup-check" else (existing.get("lineup_rechecked_at") if existing else None),
        "revisions": revisions,
        "asian_pick": None,
        "total_pick": None,
        "half_time_pick": None,
        "htft_picks": [],
        "goal_range_pick": None,
        "btts_pick": None,
        "corner_total_pick": None,
        "corner_handicap_pick": None,
    }
    if args.asian_side:
        record["asian_pick"] = {
            "side": args.asian_side,
            "line": args.asian_line,
            "odds": args.asian_odds,
            "probability": args.asian_probability,
            "ev": args.asian_ev,
            "edge_pp": getattr(args, "asian_edge_pp", None),
            "firm_count": getattr(args, "asian_firm_count", None),
            "market_signal": args.asian_market_signal,
            "cover_probability": getattr(args, "asian_cover_probability", None),
            "cover_distribution_validated": bool(
                getattr(args, "asian_cover_distribution_validated", False)
            ),
        }
        if getattr(args, "asian_odds_format", None) is not None:
            record["asian_pick"]["odds_format"] = args.asian_odds_format
    if args.total_side:
        record["total_pick"] = {
            "side": args.total_side,
            "line": args.total_line,
            "odds": args.total_odds,
            "probability": args.total_probability,
            "ev": args.total_ev,
            "edge_pp": getattr(args, "total_edge_pp", None),
            "firm_count": getattr(args, "total_firm_count", None),
            "market_signal": args.total_market_signal,
        }
        if getattr(args, "total_odds_format", None) is not None:
            record["total_pick"]["odds_format"] = args.total_odds_format
    if args.half_market:
        if args.half_market == "1x2" and args.half_side not in {"home", "draw", "away"}:
            raise ValueError("Half-time 1X2 requires --half-side home, draw, or away")
        if args.half_market == "asian" and args.half_side not in {"home", "away"}:
            raise ValueError("Half-time Asian handicap requires --half-side home or away")
        if args.half_market == "total" and args.half_side not in {"over", "under"}:
            raise ValueError("Half-time total requires --half-side over or under")
        if args.half_market in {"asian", "total"} and args.half_line is None:
            raise ValueError("Half-time Asian/total picks require --half-line")
        record["half_time_pick"] = {
            "market": args.half_market,
            "side": args.half_side,
            "line": args.half_line,
            "odds": args.half_odds,
            "probability": args.half_probability,
            "ev": args.half_ev,
            "edge_pp": getattr(args, "half_edge_pp", None),
            "firm_count": getattr(args, "half_firm_count", None),
            "market_signal": args.half_market_signal,
        }
        if getattr(args, "half_odds_format", None) is not None:
            record["half_time_pick"]["odds_format"] = args.half_odds_format
    if args.htft_pick:
        record["htft_picks"] = [
            parse_htft_pick(value, getattr(args, "htft_odds_format", None))
            for value in args.htft_pick
        ]
    if getattr(args, "goal_range_selection", None):
        record["goal_range_pick"] = {
            **parse_goal_range_selection(args.goal_range_selection),
            "odds": getattr(args, "goal_range_odds", None),
            "odds_format": getattr(args, "goal_range_odds_format", None),
            "probability": getattr(args, "goal_range_probability", None),
            "ev": getattr(args, "goal_range_ev", None),
            "edge_pp": getattr(args, "goal_range_edge_pp", None),
            "firm_count": getattr(args, "goal_range_firm_count", None),
            "market_signal": getattr(args, "goal_range_market_signal", "unknown"),
            "market_complete": bool(
                getattr(args, "goal_range_market_complete", False)
            ),
            "market_probability": getattr(
                args, "goal_range_market_probability", None
            ),
            "market_source": getattr(args, "goal_range_market_source", None),
            "market_collected_at": getattr(
                args, "goal_range_market_collected_at", None
            ),
            "price_basis": getattr(args, "goal_range_price_basis", None),
        }
    if getattr(args, "btts_side", None):
        record["btts_pick"] = {
            "side": args.btts_side,
            "odds": getattr(args, "btts_odds", None),
            "odds_format": getattr(args, "btts_odds_format", None),
            "probability": getattr(args, "btts_probability", None),
            "ev": getattr(args, "btts_ev", None),
            "edge_pp": getattr(args, "btts_edge_pp", None),
            "firm_count": getattr(args, "btts_firm_count", None),
            "market_signal": getattr(args, "btts_market_signal", "unknown"),
            "market_complete": bool(
                getattr(args, "btts_market_complete", False)
            ),
            "market_probability": getattr(args, "btts_market_probability", None),
            "market_source": getattr(args, "btts_market_source", None),
            "market_collected_at": getattr(
                args, "btts_market_collected_at", None
            ),
            "price_basis": getattr(args, "btts_price_basis", None),
        }
    if getattr(args, "corner_total_side", None):
        record["corner_total_pick"] = {
            "side": args.corner_total_side,
            "line": getattr(args, "corner_total_line", None),
            "odds": getattr(args, "corner_total_odds", None),
            "odds_format": getattr(args, "corner_total_odds_format", None),
            "probability": getattr(args, "corner_total_probability", None),
            "ev": getattr(args, "corner_total_ev", None),
            "edge_pp": getattr(args, "corner_total_edge_pp", None),
            "firm_count": getattr(args, "corner_total_firm_count", None),
            "market_signal": getattr(args, "corner_total_market_signal", "unknown"),
            "market_complete": bool(
                getattr(args, "corner_total_market_complete", False)
            ),
            "market_probability": getattr(
                args, "corner_total_market_probability", None
            ),
            "market_source": getattr(args, "corner_total_market_source", None),
            "market_collected_at": getattr(
                args, "corner_total_market_collected_at", None
            ),
            "price_basis": getattr(args, "corner_total_price_basis", None),
            "settlement_probabilities": {
                "full_win": getattr(
                    args, "corner_total_full_win_probability", None
                ),
                "half_win": getattr(
                    args, "corner_total_half_win_probability", None
                ),
                "push": getattr(args, "corner_total_push_probability", None),
                "half_loss": getattr(
                    args, "corner_total_half_loss_probability", None
                ),
                "loss": getattr(args, "corner_total_loss_probability", None),
            },
        }
    if getattr(args, "corner_handicap_side", None):
        record["corner_handicap_pick"] = {
            "side": args.corner_handicap_side,
            "line": getattr(args, "corner_handicap_line", None),
            "odds": getattr(args, "corner_handicap_odds", None),
            "odds_format": getattr(args, "corner_handicap_odds_format", None),
            "probability": getattr(args, "corner_handicap_probability", None),
            "ev": getattr(args, "corner_handicap_ev", None),
            "edge_pp": getattr(args, "corner_handicap_edge_pp", None),
            "firm_count": getattr(args, "corner_handicap_firm_count", None),
            "market_signal": getattr(
                args, "corner_handicap_market_signal", "unknown"
            ),
            "market_complete": bool(
                getattr(args, "corner_handicap_market_complete", False)
            ),
            "market_probability": getattr(
                args, "corner_handicap_market_probability", None
            ),
            "market_source": getattr(
                args, "corner_handicap_market_source", None
            ),
            "market_collected_at": getattr(
                args, "corner_handicap_market_collected_at", None
            ),
            "price_basis": getattr(args, "corner_handicap_price_basis", None),
            "settlement_probabilities": {
                "full_win": getattr(
                    args, "corner_handicap_full_win_probability", None
                ),
                "half_win": getattr(
                    args, "corner_handicap_half_win_probability", None
                ),
                "push": getattr(
                    args, "corner_handicap_push_probability", None
                ),
                "half_loss": getattr(
                    args, "corner_handicap_half_loss_probability", None
                ),
                "loss": getattr(
                    args, "corner_handicap_loss_probability", None
                ),
            },
        }

    apply_primary_role(record, args.primary_market, args.primary_htft_selection)
    validate_provisional_formal_guardrails(record)
    annotate_confidence_ranking(record)
    apply_primary_role(record, args.primary_market, args.primary_htft_selection)
    validate_primary_is_rank_one(record)
    record["primary_change"] = build_primary_change(record, existing, args)

    if existing:
        previous_snapshot = revision_snapshot(existing)
        incoming_snapshot = revision_snapshot(record)
        if snapshot_payload(previous_snapshot) == snapshot_payload(incoming_snapshot):
            return {
                "ok": True,
                "duplicate_ignored": True,
                "path": str(path),
                "record": existing,
            }
        if not revisions or snapshot_payload(revisions[-1]) != snapshot_payload(previous_snapshot):
            revisions.append(previous_snapshot)
        record["revisions"] = revisions
        history[history.index(existing)] = record
    else:
        history.append(record)
    save_history(path, history)
    return {"ok": True, "path": str(path), "record": record}


def parse_primary_assignment(value: str) -> tuple[str, str, str | None]:
    parts = [part.strip() for part in value.split(":")]
    if len(parts) not in {2, 3} or not parts[0]:
        raise ValueError("Primary assignment must be MATCH_ID:MARKET[:HTFT_SELECTION]")
    match_id, market = parts[0], parts[1].lower()
    if market not in PRIMARY_MARKETS:
        raise ValueError(f"Primary market must be one of {', '.join(PRIMARY_MARKETS)}")
    selection = parts[2].upper() if len(parts) == 3 and parts[2] else None
    if market == "htft" and not selection:
        raise ValueError("HT/FT primary assignment requires a selection")
    if market != "htft" and selection:
        raise ValueError("Only HT/FT primary assignments accept a selection")
    return match_id, market, selection


def cmd_migrate_primary(args: argparse.Namespace) -> dict[str, Any]:
    path = data_path(args.base_dir)
    history = load_history(path)
    assignments = [parse_primary_assignment(value) for value in args.primary]
    if len({match_id for match_id, _, _ in assignments}) != len(assignments):
        raise ValueError("Each match ID may appear only once in --primary assignments")

    changed: list[str] = []
    for match_id, market, selection in assignments:
        record = find_record(history, match_id)
        if not record:
            raise ValueError(f"No archived pre-match prediction for match {match_id}")
        revisions_before = deepcopy(record.get("revisions", []))
        apply_primary_role(record, market, selection)
        current_primary = active_primary_identity(record)
        record["primary_change"] = {
            "status": "backfilled",
            "previous": None,
            "current": list(current_primary) if current_primary else None,
        }
        if record.get("status") == "reviewed":
            record["primary_result"] = primary_result_from_record(record)
        if record.get("revisions", []) != revisions_before:
            raise ValueError(f"Migration unexpectedly modified revisions for match {match_id}")
        changed.append(match_id)

    if args.write:
        save_history(path, history)
    return {
        "ok": True,
        "path": str(path),
        "written": args.write,
        "changed_match_ids": changed,
        "stats": calculate_stats(history),
    }


def cmd_migrate_leagues(args: argparse.Namespace) -> dict[str, Any]:
    """Backfill stable league keys without touching revisions or settlements."""
    path = data_path(args.base_dir)
    history = load_history(path)
    changed: list[str] = []
    for record in history:
        revisions_before = deepcopy(record.get("revisions", []))
        league_key = normalize_league_name(record.get("league"))
        if record.get("league_key") != league_key:
            record["league_key"] = league_key
            changed.append(str(record.get("match_id")))
        if record.get("revisions", []) != revisions_before:
            raise ValueError(
                f"League migration unexpectedly modified revisions for match {record.get('match_id')}"
            )
    if args.write:
        save_history(path, history)
    return {
        "ok": True,
        "path": str(path),
        "written": args.write,
        "changed_match_ids": changed,
        "stats": calculate_stats(history),
    }


def cmd_migrate_settlement_basis(args: argparse.Namespace) -> dict[str, Any]:
    """Backfill settlement audit metadata without re-grading reviewed records."""
    path = data_path(args.base_dir)
    history = load_history(path)
    changed: list[str] = []
    for record in history:
        if record.get("mode") != "prematch" or record.get("status") != "reviewed":
            continue
        if isinstance(record.get("settlement_basis"), dict):
            continue
        before = deepcopy(record)
        record["settlement_basis"] = settlement_basis_for_record(record)
        without_basis = deepcopy(record)
        without_basis.pop("settlement_basis", None)
        if without_basis != before:
            raise ValueError(
                f"Settlement-basis migration modified graded data for match {record.get('match_id')}"
            )
        changed.append(str(record.get("match_id")))
    if args.write:
        save_history(path, history)
    return {
        "ok": True,
        "path": str(path),
        "written": args.write,
        "changed_match_ids": changed,
        "stats": calculate_stats(history),
    }


def cmd_migrate_learning_scopes(args: argparse.Namespace) -> dict[str, Any]:
    """Backfill review-learning metadata without changing settlement or revisions."""
    path = data_path(args.base_dir)
    history = load_history(path)
    changed: list[str] = []
    missing_learning: list[str] = []
    for record in history:
        if record.get("mode") != "prematch" or record.get("status") != "reviewed":
            continue
        key_learning = str(record.get("key_learning", "")).strip()
        if not key_learning:
            missing_learning.append(str(record.get("match_id")))
            continue
        revisions_before = deepcopy(record.get("revisions", []))
        _, primary = primary_snapshot_for_stats(record)
        counts_toward_primary_record = isinstance(primary, dict)
        learning_scope = (
            "primary"
            if counts_toward_primary_record
            else "no_primary_observation"
        )
        learning_sample = {
            "eligible": True,
            "scope": learning_scope,
            "counts_toward_primary_record": counts_toward_primary_record,
            "key_learning": key_learning,
        }
        expected = {
            "learning_scope": learning_scope,
            "counts_toward_primary_record": counts_toward_primary_record,
            "learning_sample": learning_sample,
        }
        if any(record.get(key) != value for key, value in expected.items()):
            record.update(expected)
            basis = record.get("settlement_basis")
            if isinstance(basis, dict):
                basis["counts_toward_primary_record"] = (
                    counts_toward_primary_record
                )
            changed.append(str(record.get("match_id")))
        if record.get("revisions", []) != revisions_before:
            raise ValueError(
                f"Learning migration unexpectedly modified revisions for match {record.get('match_id')}"
            )
    if args.write:
        save_history(path, history)
    return {
        "ok": True,
        "path": str(path),
        "written": args.write,
        "changed_match_ids": changed,
        "missing_learning_match_ids": missing_learning,
        "stats": calculate_stats(history),
    }


def cmd_due_lineup_check(args: argparse.Namespace) -> dict[str, Any]:
    path = data_path(args.base_dir)
    history = load_history(path)
    current = parse_datetime(args.now) if args.now else datetime.now(timezone.utc)
    due: list[dict[str, Any]] = []
    skipped_invalid_kickoff: list[str] = []
    for record in history:
        if record.get("mode") != "prematch" or record.get("status") != "pending":
            continue
        if record.get("lineup_rechecked_at"):
            continue
        try:
            kickoff = parse_datetime(str(record.get("kickoff", "")))
        except (TypeError, ValueError):
            skipped_invalid_kickoff.append(str(record.get("match_id")))
            continue
        minutes = (kickoff - current).total_seconds() / 60
        if args.min_minutes <= minutes <= args.max_minutes:
            item = dict(record)
            item["minutes_to_kickoff"] = round(minutes, 1)
            due.append(item)
    return {
        "ok": True,
        "path": str(path),
        "checked_at": current.replace(microsecond=0).isoformat(),
        "window_minutes": [args.min_minutes, args.max_minutes],
        "due": due,
        "skipped_invalid_kickoff": skipped_invalid_kickoff,
    }


def cmd_review(args: argparse.Namespace) -> dict[str, Any]:
    if not args.verified_finished:
        raise ValueError(
            "Review refused: verify that the match has an explicit terminal status, then pass --verified-finished"
        )
    path = data_path(args.base_dir)
    history = load_history(path)
    record = find_record(history, args.match_id)
    if not record:
        raise ValueError(f"No archived pre-match prediction for match {args.match_id}")
    if record.get("mode") != "prematch":
        raise ValueError("Only pre-match predictions can be reviewed for accuracy")
    if record.get("status") == "reviewed":
        stats = calculate_stats(history)
        league_key = league_key_for_record(record)
        return {
            "ok": True,
            "already_reviewed": True,
            "path": str(path),
            "match_id": str(record.get("match_id")),
            "final_score": record.get("final_score"),
            "reviewed_at": record.get("reviewed_at"),
            "record": record,
            "league_key": league_key,
            "league_stats": stats["leagues"].get(league_key),
            "stats": stats,
        }

    if not args.key_learning.strip():
        raise ValueError("Review requires a concise non-empty --key-learning grounded in the verified result")

    home, away = int(args.home_score), int(args.away_score)
    settlement_basis = settlement_basis_for_record(record)
    primary_market = settlement_basis.get("primary_market")
    home_corners_arg = getattr(args, "home_corners", None)
    away_corners_arg = getattr(args, "away_corners", None)
    if (home_corners_arg is None) != (away_corners_arg is None):
        raise ValueError(
            "Corner settlement requires both --home-corners and --away-corners"
        )
    corners_available = home_corners_arg is not None and away_corners_arg is not None
    if primary_market in CORNER_MARKETS and not corners_available:
        raise ValueError(
            "Review refused: a corner primary requires verified 90-minute "
            "--home-corners and --away-corners"
        )
    home_corners = int(home_corners_arg) if corners_available else None
    away_corners = int(away_corners_arg) if corners_available else None
    if corners_available and (home_corners < 0 or away_corners < 0):
        raise ValueError("Verified 90-minute corner scores cannot be negative")
    predicted = str(record.get("predicted_score", ""))
    predicted_exact = predicted == f"{home}-{away}"
    actual_score = f"{home}-{away}"
    exact_score_hit_rank = next(
        (
            int(pick.get("rank", index))
            for index, pick in enumerate(record.get("exact_score_picks", []), start=1)
            if isinstance(pick, dict) and str(pick.get("score")) == actual_score
        ),
        1 if predicted_exact else None,
    )
    display_picks = record.get("display_exact_score_picks")
    if not isinstance(display_picks, list) or not display_picks:
        display_picks = record.get("exact_score_picks", [])
    display_predicted = str(
        record.get("display_predicted_score")
        or (
            display_picks[0].get("score")
            if display_picks and isinstance(display_picks[0], dict)
            else predicted
        )
    )
    display_predicted_exact = display_predicted == actual_score
    display_exact_score_hit_rank = next(
        (
            int(pick.get("display_rank", pick.get("rank", index)))
            for index, pick in enumerate(display_picks, start=1)
            if isinstance(pick, dict) and str(pick.get("score")) == actual_score
        ),
        1 if display_predicted_exact else None,
    )
    half_scores_available = args.half_home_score is not None and args.half_away_score is not None
    half_home = int(args.half_home_score) if half_scores_available else None
    half_away = int(args.half_away_score) if half_scores_available else None
    if primary_market in {"half_time", "htft"} and not half_scores_available:
        raise ValueError(
            "Review refused: a half-time or HT/FT primary requires verified "
            "--half-home-score and --half-away-score"
        )
    primary_pick = settlement_basis.get("primary_pick")
    counts_toward_primary_record = bool(
        primary_market and isinstance(primary_pick, dict)
    )
    learning_scope = (
        "primary"
        if counts_toward_primary_record
        else "no_primary_observation"
    )
    primary_result = None
    if isinstance(primary_pick, dict):
        if primary_market == "asian":
            primary_result = settle_asian(primary_pick, home, away)
        elif primary_market == "total":
            primary_result = settle_total(primary_pick, home, away)
        elif primary_market == "half_time" and half_scores_available:
            primary_result = settle_half_time(primary_pick, half_home, half_away)
        elif primary_market == "htft" and half_scores_available:
            results = settle_htft([primary_pick], half_home, half_away, home, away)
            primary_result = results[0] if results else None
        elif primary_market == "goal_range":
            primary_result = settle_goal_range(primary_pick, home, away)
        elif primary_market == "btts":
            primary_result = settle_btts(primary_pick, home, away)
        elif primary_market == "corner_total":
            primary_result = settle_corner_total(
                primary_pick, home_corners, away_corners
            )
        elif primary_market == "corner_handicap":
            primary_result = settle_corner_handicap(
                primary_pick, home_corners, away_corners
            )
    settlement_basis["primary_result"] = primary_result
    settlement_basis["counts_toward_primary_record"] = counts_toward_primary_record
    record.update({
        "status": "reviewed",
        "reviewed_at": now_iso(),
        "final_score": f"{home}-{away}",
        "score_exact": predicted_exact,
        "exact_score_hit_rank": exact_score_hit_rank,
        "exact_score_any_hit": exact_score_hit_rank in {1, 2},
        "display_score_exact": display_predicted_exact,
        "display_exact_score_hit_rank": display_exact_score_hit_rank,
        "display_exact_score_any_hit": display_exact_score_hit_rank in {1, 2},
        "asian_result": primary_result if primary_market == "asian" else None,
        "total_result": primary_result if primary_market == "total" else None,
        "half_time_score": f"{half_home}-{half_away}" if half_scores_available else None,
        "half_time_result": primary_result if primary_market == "half_time" else None,
        "htft_results": [],
        "goal_range_result": primary_result if primary_market == "goal_range" else None,
        "btts_result": primary_result if primary_market == "btts" else None,
        "corner_total_result": (
            primary_result if primary_market == "corner_total" else None
        ),
        "corner_handicap_result": (
            primary_result if primary_market == "corner_handicap" else None
        ),
        "corner_score": (
            f"{home_corners}-{away_corners}" if corners_available else None
        ),
        "home_corners": home_corners,
        "away_corners": away_corners,
        "primary_result": primary_result,
        "key_learning": args.key_learning,
        "learning_scope": learning_scope,
        "counts_toward_primary_record": counts_toward_primary_record,
        "learning_sample": {
            "eligible": True,
            "scope": learning_scope,
            "counts_toward_primary_record": counts_toward_primary_record,
            "key_learning": args.key_learning,
        },
        "league_key": league_key_for_record(record),
        "settlement_basis": settlement_basis,
    })
    warnings = []
    save_history(path, history)
    stats = calculate_stats(history)
    league_key = league_key_for_record(record)
    return {
        "ok": True,
        "path": str(path),
        "record": record,
        "warnings": warnings,
        "league_key": league_key,
        "league_stats": stats["leagues"].get(league_key),
        "stats": stats,
    }


def rate_block(results: list[str]) -> dict[str, Any]:
    decisive = [r for r in results if r != "push"]
    wins = sum(r in {"win", "half_win"} for r in decisive)
    losses = sum(r in {"loss", "half_loss"} for r in decisive)
    return {
        "matches": len(results),
        "graded": len(decisive),
        "wins": wins,
        "losses": losses,
        "pushes": sum(r == "push" for r in results),
        "half_wins": sum(r == "half_win" for r in results),
        "half_losses": sum(r == "half_loss" for r in results),
        "accuracy": round(wins / len(decisive), 4) if decisive else None,
    }


def settlement_profit(
    result: str, odds: Any, odds_format: Any = None
) -> float | None:
    if odds is None:
        return None
    price = float(odds)
    # Historical records stored Hong Kong odds without an explicit format.
    # Preserve that behavior while allowing newly archived decimal prices.
    win_profit = price - 1.0 if odds_format == "decimal" else price
    return {
        "win": win_profit,
        "half_win": win_profit / 2,
        "push": 0.0,
        "half_loss": -0.5,
        "loss": -1.0,
    }.get(result)


def performance_block(
    pairs: list[tuple[str, dict[str, Any]]],
    *,
    calculate_money: bool = True,
) -> dict[str, Any]:
    block = rate_block([result for result, _ in pairs])
    archived_evs = [float(pick["ev"]) for _, pick in pairs if pick.get("ev") is not None]
    if calculate_money:
        profits = [
            settlement_profit(
                result, pick.get("odds"), pick.get("odds_format")
            )
            for result, pick in pairs
        ]
        settled_profits = [value for value in profits if value is not None]
        block.update({
            "monetary_scope": "primary_only",
            "stake_units": len(settled_profits),
            "profit_units": round(sum(settled_profits), 4),
            "roi": round(sum(settled_profits) / len(settled_profits), 4) if settled_profits else None,
        })
    else:
        block.update({
            "monetary_scope": "not_tracked",
            "stake_units": None,
            "profit_units": None,
            "roi": None,
        })
    block["avg_archived_ev"] = round(sum(archived_evs) / len(archived_evs), 4) if archived_evs else None
    signals: dict[str, dict[str, Any]] = {}
    for signal in sorted({str(pick.get("market_signal", "unknown")) for _, pick in pairs}):
        subset = [(result, pick) for result, pick in pairs if str(pick.get("market_signal", "unknown")) == signal]
        signals[signal] = performance_block_without_signals(subset, calculate_money=calculate_money)
    block["by_market_signal"] = signals
    return block


def performance_block_without_signals(
    pairs: list[tuple[str, dict[str, Any]]],
    *,
    calculate_money: bool = True,
) -> dict[str, Any]:
    block = rate_block([result for result, _ in pairs])
    if calculate_money:
        profits = [
            settlement_profit(
                result, pick.get("odds"), pick.get("odds_format")
            )
            for result, pick in pairs
        ]
        settled_profits = [value for value in profits if value is not None]
        block.update({
            "monetary_scope": "primary_only",
            "stake_units": len(settled_profits),
            "profit_units": round(sum(settled_profits), 4),
            "roi": round(sum(settled_profits) / len(settled_profits), 4) if settled_profits else None,
        })
    else:
        block.update({
            "monetary_scope": "not_tracked",
            "stake_units": None,
            "profit_units": None,
            "roi": None,
        })
    return block


def primary_pairs(records: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    pairs: list[tuple[str, dict[str, Any]]] = []
    for record in records:
        _, primary = primary_snapshot_for_stats(record)
        if not isinstance(primary, dict):
            continue
        result = primary_result_for_stats(record)
        if result:
            pairs.append((str(result), primary))
    return pairs


def primary_pairs_for_market(
    records: list[dict[str, Any]], market: str
) -> list[tuple[str, dict[str, Any]]]:
    pairs: list[tuple[str, dict[str, Any]]] = []
    for record in records:
        primary_market, primary = primary_snapshot_for_stats(record)
        if not isinstance(primary, dict) or primary_market != market:
            continue
        result = primary_result_for_stats(record)
        if result:
            pairs.append((str(result), primary))
    return pairs


def primary_market_performance(records: list[dict[str, Any]]) -> dict[str, Any]:
    asian = primary_pairs_for_market(records, "asian")
    totals = primary_pairs_for_market(records, "total")
    half_time = primary_pairs_for_market(records, "half_time")
    htft = primary_pairs_for_market(records, "htft")
    goal_range = primary_pairs_for_market(records, "goal_range")
    btts = primary_pairs_for_market(records, "btts")
    corner_total = primary_pairs_for_market(records, "corner_total")
    corner_handicap = primary_pairs_for_market(records, "corner_handicap")
    combined = (
        asian
        + totals
        + half_time
        + htft
        + goal_range
        + btts
        + corner_total
        + corner_handicap
    )
    return {
        "asian": performance_block(asian, calculate_money=False),
        "totals": performance_block(totals, calculate_money=False),
        "half_time": performance_block(half_time, calculate_money=False),
        "htft": performance_block(htft, calculate_money=False),
        "goal_range": performance_block(goal_range, calculate_money=False),
        "btts": performance_block(btts, calculate_money=False),
        "corner_total": performance_block(corner_total, calculate_money=False),
        "corner_handicap": performance_block(
            corner_handicap, calculate_money=False
        ),
        "combined": performance_block(combined, calculate_money=False),
    }


def exact_score_diagnostics(
    records: list[dict[str, Any]],
    *,
    display: bool = False,
) -> dict[str, Any]:
    def rank_for(record: dict[str, Any]) -> Any:
        if display and "display_exact_score_hit_rank" in record:
            return record.get("display_exact_score_hit_rank")
        return record.get("exact_score_hit_rank")

    def exact_for(record: dict[str, Any]) -> bool:
        if display and "display_score_exact" in record:
            return bool(record.get("display_score_exact"))
        return bool(record.get("score_exact"))

    top1 = sum((rank_for(r) == 1) or exact_for(r) for r in records)
    top2 = sum(
        (rank_for(r) in {1, 2})
        or (rank_for(r) is None and exact_for(r))
        for r in records
    )
    return {
        "top1_hits": top1,
        "top1_rate": round(top1 / len(records), 4) if records else None,
        "top2_hits": top2,
        "top2_rate": round(top2 / len(records), 4) if records else None,
    }


def learning_scope_for_record(record: dict[str, Any]) -> str:
    explicit = str(record.get("learning_scope", "")).strip()
    if explicit in {"primary", "no_primary_observation"}:
        return explicit
    _, primary = primary_snapshot_for_stats(record)
    return "primary" if isinstance(primary, dict) else "no_primary_observation"


def learning_sample_summary(records: list[dict[str, Any]]) -> dict[str, int]:
    samples = [
        record
        for record in records
        if str(record.get("key_learning", "")).strip()
    ]
    primary = sum(
        learning_scope_for_record(record) == "primary" for record in samples
    )
    no_primary = sum(
        learning_scope_for_record(record) == "no_primary_observation"
        for record in samples
    )
    return {
        "total": len(samples),
        "primary": primary,
        "no_primary_observation": no_primary,
    }


def league_performance(records: list[dict[str, Any]], league_key: str) -> dict[str, Any]:
    primary_by_market = primary_market_performance(records)
    primary = performance_block(primary_pairs(records))
    learning_samples = learning_sample_summary(records)
    learnings = [
        {
            "match_id": str(record.get("match_id")),
            "reviewed_at": record.get("reviewed_at"),
            "learning_scope": learning_scope_for_record(record),
            "counts_toward_primary_record": (
                learning_scope_for_record(record) == "primary"
            ),
            "key_learning": str(record.get("key_learning", "")).strip(),
        }
        for record in records[-20:]
        if str(record.get("key_learning", "")).strip()
    ]
    return {
        "league_key": league_key,
        "source_labels": sorted({str(record.get("league", "unknown")) for record in records}),
        "matches": len(records),
        "reviewed_matches": len(records),
        "primary_record_matches": primary["matches"],
        "no_primary_reviewed_matches": sum(
            learning_scope_for_record(record) == "no_primary_observation"
            for record in records
        ),
        "learning_samples": learning_samples,
        "primary": primary,
        "primary_by_market": primary_by_market,
        "all_formal": primary_by_market,
        "secondary_tracking": "disabled",
        "asian": primary_by_market["asian"],
        "totals": primary_by_market["totals"],
        "half_time": primary_by_market["half_time"],
        "htft": primary_by_market["htft"],
        "goal_range": primary_by_market["goal_range"],
        "btts": primary_by_market["btts"],
        "corner_total": primary_by_market["corner_total"],
        "corner_handicap": primary_by_market["corner_handicap"],
        "exact_scores": exact_score_diagnostics(records),
        "display_exact_scores": exact_score_diagnostics(records, display=True),
        "recent_learnings": learnings,
    }


def calculate_stats(history: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = [r for r in history if r.get("mode") == "prematch" and r.get("status") == "reviewed"]
    primary_pairs_all = primary_pairs(reviewed)
    primary = performance_block(primary_pairs_all)
    learning_samples = learning_sample_summary(reviewed)
    exact_scores = exact_score_diagnostics(reviewed)
    display_exact_scores = exact_score_diagnostics(reviewed, display=True)
    leagues: dict[str, dict[str, Any]] = {}
    for league_key in sorted({league_key_for_record(record) for record in reviewed}):
        subset = [record for record in reviewed if league_key_for_record(record) == league_key]
        leagues[league_key] = league_performance(subset, league_key)
    primary_by_market = primary_market_performance(reviewed)
    return {
        "reviewed_matches": len(reviewed),
        "primary_record_matches": primary["matches"],
        "no_primary_reviewed_matches": sum(
            learning_scope_for_record(record) == "no_primary_observation"
            for record in reviewed
        ),
        "pending_matches": sum(r.get("mode") == "prematch" and r.get("status") == "pending" for r in history),
        "learning_samples": learning_samples,
        "primary": primary,
        "primary_by_market": primary_by_market,
        "all_formal": primary_by_market,
        "secondary_tracking": "disabled",
        "asian": primary_by_market["asian"],
        "totals": primary_by_market["totals"],
        "half_time": primary_by_market["half_time"],
        "htft": primary_by_market["htft"],
        "goal_range": primary_by_market["goal_range"],
        "btts": primary_by_market["btts"],
        "corner_total": primary_by_market["corner_total"],
        "corner_handicap": primary_by_market["corner_handicap"],
        "combined": primary_by_market["combined"],
        "exact_scores": exact_scores["top1_hits"],
        "exact_score_rate": exact_scores["top1_rate"],
        "exact_score_top1_hits": exact_scores["top1_hits"],
        "exact_score_top1_rate": exact_scores["top1_rate"],
        "exact_score_top2_hits": exact_scores["top2_hits"],
        "exact_score_top2_rate": exact_scores["top2_rate"],
        "display_exact_score_top1_hits": display_exact_scores["top1_hits"],
        "display_exact_score_top1_rate": display_exact_scores["top1_rate"],
        "display_exact_score_top2_hits": display_exact_scores["top2_hits"],
        "display_exact_score_top2_rate": display_exact_scores["top2_rate"],
        "learnings_recorded": sum(bool(str(r.get("key_learning", "")).strip()) for r in reviewed),
        "leagues": leagues,
    }


def merge_guardrails(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for item in group:
            text = str(item).strip()
            if text and text not in merged:
                merged.append(text)
    return merged


def dynamic_calibration_summary(stats: dict[str, Any], minimum: int) -> str:
    primary = stats["primary"]
    primary_by_market = stats["primary_by_market"]["combined"]
    no_primary = stats["no_primary_reviewed_matches"]

    def roi_text(block: dict[str, Any]) -> str:
        roi = block.get("roi")
        return "—" if roi is None else f"{float(roi) * 100:+.2f}%"

    return (
        f"已复盘{stats['reviewed_matches']}场，按{len(stats['leagues'])}个联赛归类；"
        f"主推{primary['matches']}场"
        f"{primary['wins']}胜{primary['losses']}负{primary['pushes']}走，"
        f"收益{primary['profit_units']:+.2f}u，ROI {roi_text(primary)}，计入战绩；"
        f"无主推{no_primary}场不计战绩并作为学习样本。"
        f"主推分市场统计{primary_by_market['matches']}项"
        f"{primary_by_market['wins']}胜{primary_by_market['losses']}负{primary_by_market['pushes']}走。"
        "次推仅作赛前参考，不结算、不计命中率或金额。"
        f"单市场不足{minimum}个有效样本时只保存guardrail，不调整全局权重。"
    )


def league_calibration_profiles(stats: dict[str, Any], minimum: int) -> dict[str, Any]:
    profiles: dict[str, Any] = {}
    for league_key, league_stats in stats["leagues"].items():
        sample_threshold = {
            market: league_stats["primary_by_market"][market]["graded"] >= minimum
            for market in (
                "asian",
                "totals",
                "half_time",
                "htft",
                "goal_range",
                "btts",
                "corner_total",
                "corner_handicap",
            )
        }
        matches = int(league_stats["reviewed_matches"])
        sample_tier = "anecdotal" if matches < 10 else "provisional" if matches < 20 else "established"
        primary = league_stats["primary"]
        roi = primary.get("roi")
        roi_text = "—" if roi is None else f"{float(roi) * 100:+.2f}%"
        profiles[league_key] = {
            "league_key": league_key,
            "source_labels": league_stats["source_labels"],
            "reviewed_matches": matches,
            "primary_record_matches": league_stats["primary_record_matches"],
            "no_primary_reviewed_matches": league_stats["no_primary_reviewed_matches"],
            "learning_samples": league_stats["learning_samples"],
            "sample_tier": sample_tier,
            "minimum_graded_per_market_for_weight_change": minimum,
            "sample_threshold_met_by_market": sample_threshold,
            "decision": (
                "manual_feature_level_review_required"
                if any(sample_threshold.values())
                else "hold_weights_insufficient_league_sample"
            ),
            "active_weight_adjustments": {},
            "summary": (
                f"{league_key}：主推{primary['matches']}场"
                f"{primary['wins']}胜{primary['losses']}负{primary['pushes']}走，"
                f"收益{primary['profit_units']:+.2f}u，ROI {roi_text}；"
                f"样本层级{sample_tier}。"
            ),
            "primary": primary,
            "primary_by_market": league_stats["primary_by_market"],
            "all_formal": league_stats["all_formal"],
            "secondary_tracking": "disabled",
            "exact_scores": league_stats["exact_scores"],
            "recent_learnings": league_stats["recent_learnings"],
        }
    return profiles


def cmd_calibrate(args: argparse.Namespace) -> dict[str, Any]:
    history_file = data_path(args.base_dir)
    output_file = calibration_path(args.base_dir)
    history = load_history(history_file)
    stats = calculate_stats(history)
    existing: dict[str, Any] = {}
    if output_file.exists():
        loaded = json.loads(output_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    supplied_guardrails = args.guardrail if args.guardrail is not None else existing.get("guardrails", [])
    supplied_guardrails = [item for item in supplied_guardrails if item not in OBSOLETE_GUARDRAILS]
    guardrails = merge_guardrails(DEFAULT_GUARDRAILS, supplied_guardrails)
    minimum = args.minimum_graded
    eligibility = {
        market: stats["primary_by_market"][market]["graded"] >= minimum
        for market in (
            "asian",
            "totals",
            "half_time",
            "htft",
            "goal_range",
            "btts",
            "corner_total",
            "corner_handicap",
        )
    }
    calibration = {
        "updated_at": now_iso(),
        "history_path": str(history_file),
        "reviewed_matches": stats["reviewed_matches"],
        "primary_record_matches": stats["primary_record_matches"],
        "no_primary_reviewed_matches": stats["no_primary_reviewed_matches"],
        "learning_samples": stats["learning_samples"],
        "minimum_graded_per_market_for_weight_change": minimum,
        "weight_change_eligible": eligibility,
        "active_weight_adjustments": existing.get("active_weight_adjustments", {}),
        "summary": dynamic_calibration_summary(stats, minimum),
        "guardrails": guardrails,
        "stats": stats,
        "league_profiles": league_calibration_profiles(stats, minimum),
    }
    if not any(eligibility.values()):
        calibration["decision"] = "hold_weights_insufficient_sample"
        calibration["active_weight_adjustments"] = {}
    else:
        calibration["decision"] = "manual_feature_level_review_required"
    if args.write:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        temp = output_file.with_suffix(".json.tmp")
        temp.write_text(json.dumps(calibration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(output_file)
    return {"ok": True, "path": str(output_file), "written": args.write, "calibration": calibration}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", help="Workspace root; defaults to the current directory")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="Create or update a pending pre-match prediction")
    record.add_argument("--match-id", required=True)
    record.add_argument("--analysis-stage", choices=("initial", "lineup-check"), default="initial")
    record.add_argument("--league", required=True)
    record.add_argument("--kickoff", required=True, help="ISO-like local datetime including timezone when known")
    record.add_argument("--home-team", required=True)
    record.add_argument("--away-team", required=True)
    record.add_argument("--predicted-score", required=True)
    record.add_argument(
        "--exact-score-pick",
        action="append",
        help="Required exactly twice as SCORE:PROBABILITY; rank is derived from probability",
    )
    record.add_argument(
        "--display-exact-score-pick",
        action="append",
        help=(
            "Optional user-facing primary-conditioned scenario as "
            "SCORE:PROBABILITY:UNCONDITIONAL_RANK; required exactly twice "
            "when supplied"
        ),
    )
    record.add_argument(
        "--display-exact-score-event-probability",
        type=float,
        help=(
            "Probability mass of the formal total primary's net-profit branch "
            "used to calculate conditional scenario shares"
        ),
    )
    record.add_argument(
        "--zero-zero-probability",
        type=float,
        required=True,
        help="Model probability for 0-0 from the same full score distribution",
    )
    record.add_argument(
        "--zero-zero-rank",
        type=int,
        required=True,
        help="One-based rank of 0-0 in the full exact-score distribution",
    )
    record.add_argument(
        "--zero-zero-odds",
        type=float,
        help="Current 0-0 decimal odds when actually collected",
    )
    record.add_argument(
        "--zero-zero-ev",
        type=float,
        help="Optional audited 0-0 decimal-odds EV; recalculated and validated",
    )
    record.add_argument("--recommendation", default="")
    record.add_argument("--source-url", default="")
    record.add_argument("--notes", default="")
    record.add_argument("--data-quality", choices=("high", "medium", "low", "unknown"), default="unknown")
    record.add_argument("--lineup-confirmed", action="store_true")
    record.add_argument("--fundamental-evidence", action="store_true")
    record.add_argument("--chance-quality-evidence", action="store_true")
    record.add_argument("--attack-configuration-evidence", action="store_true")
    record.add_argument("--corner-profile-evidence", action="store_true")
    record.add_argument("--opponent-tail-risk-checked", action="store_true")
    record.add_argument(
        "--injury-evidence-status",
        choices=("not_used", "fresh", "confirmed_override", "stale_conflict"),
        default="not_used",
    )
    record.add_argument("--primary-change-reason", default="")
    record.add_argument("--previous-primary-invalidated", action="store_true")
    record.add_argument("--previous-primary-current-ev", type=float)
    record.add_argument("--previous-primary-current-confidence", type=float)
    record.add_argument("--accept-worse-line", action="store_true")
    record.add_argument("--primary-htft-edge-pp", type=float)
    record.add_argument("--primary-htft-firm-count", type=int)
    record.add_argument(
        "--primary-market",
        choices=("none",) + PRIMARY_MARKETS,
        required=True,
        help="Exactly one formal primary market, or 'none' only when no formal picks exist",
    )
    record.add_argument("--primary-htft-selection", help="Required when the HT/FT primary must be selected from multiple picks")
    record.add_argument("--home-win-probability", type=float)
    record.add_argument("--draw-probability", type=float)
    record.add_argument("--away-win-probability", type=float)
    record.add_argument("--asian-side", choices=("home", "away"))
    record.add_argument("--asian-line", type=float)
    record.add_argument("--asian-odds", type=float)
    record.add_argument(
        "--asian-odds-format", choices=("decimal", "hong_kong")
    )
    record.add_argument("--asian-probability", type=float)
    record.add_argument("--asian-ev", type=float)
    record.add_argument(
        "--asian-edge-pp",
        type=float,
        help="Model probability minus no-vig market probability in percentage points",
    )
    record.add_argument("--asian-firm-count", type=int)
    record.add_argument("--asian-cover-probability", type=float)
    record.add_argument("--asian-cover-distribution-validated", action="store_true")
    record.add_argument("--asian-market-signal", choices=("aligned", "neutral", "against", "conflicting", "unknown"), default="unknown")
    record.add_argument("--total-side", choices=("over", "under"))
    record.add_argument("--total-line", type=float)
    record.add_argument("--total-odds", type=float)
    record.add_argument(
        "--total-odds-format", choices=("decimal", "hong_kong")
    )
    record.add_argument("--total-probability", type=float)
    record.add_argument("--total-ev", type=float)
    record.add_argument(
        "--total-edge-pp",
        type=float,
        help="Model probability minus no-vig market probability in percentage points",
    )
    record.add_argument("--total-firm-count", type=int)
    record.add_argument("--total-market-signal", choices=("aligned", "neutral", "against", "conflicting", "unknown"), default="unknown")
    record.add_argument("--half-market", choices=("1x2", "asian", "total"))
    record.add_argument("--half-side", choices=("home", "draw", "away", "over", "under"))
    record.add_argument("--half-line", type=float)
    record.add_argument("--half-odds", type=float)
    record.add_argument(
        "--half-odds-format", choices=("decimal", "hong_kong")
    )
    record.add_argument("--half-probability", type=float)
    record.add_argument("--half-ev", type=float)
    record.add_argument("--half-edge-pp", type=float)
    record.add_argument("--half-firm-count", type=int)
    record.add_argument("--half-market-signal", choices=("aligned", "neutral", "against", "conflicting", "unknown"), default="unknown")
    record.add_argument("--htft-pick", action="append", help="Repeatable SELECTION:ODDS:PROBABILITY:EV, e.g. DD:3.40:0.31:0.054")
    record.add_argument(
        "--htft-odds-format", choices=("decimal", "hong_kong")
    )
    record.add_argument(
        "--goal-range-selection",
        help="Inclusive goal band MIN-MAX or open-ended N+, for example 2-3 or 7+",
    )
    record.add_argument("--goal-range-odds", type=float)
    record.add_argument(
        "--goal-range-odds-format",
        choices=("decimal", "hong_kong"),
    )
    record.add_argument("--goal-range-probability", type=float)
    record.add_argument("--goal-range-ev", type=float)
    record.add_argument("--goal-range-edge-pp", type=float)
    record.add_argument("--goal-range-firm-count", type=int)
    record.add_argument(
        "--goal-range-market-signal",
        choices=("aligned", "neutral", "against", "conflicting", "unknown"),
        default="unknown",
    )
    record.add_argument("--goal-range-market-complete", action="store_true")
    record.add_argument("--goal-range-market-probability", type=float)
    record.add_argument("--goal-range-market-source")
    record.add_argument("--goal-range-market-collected-at")
    record.add_argument(
        "--goal-range-price-basis", choices=("consensus", "median")
    )
    record.add_argument("--btts-side", choices=("yes", "no"))
    record.add_argument("--btts-odds", type=float)
    record.add_argument(
        "--btts-odds-format", choices=("decimal", "hong_kong")
    )
    record.add_argument("--btts-probability", type=float)
    record.add_argument("--btts-ev", type=float)
    record.add_argument("--btts-edge-pp", type=float)
    record.add_argument("--btts-firm-count", type=int)
    record.add_argument(
        "--btts-market-signal",
        choices=("aligned", "neutral", "against", "conflicting", "unknown"),
        default="unknown",
    )
    record.add_argument("--btts-market-complete", action="store_true")
    record.add_argument("--btts-market-probability", type=float)
    record.add_argument("--btts-market-source")
    record.add_argument("--btts-market-collected-at")
    record.add_argument("--btts-price-basis", choices=("consensus", "median"))
    record.add_argument("--corner-total-side", choices=("over", "under"))
    record.add_argument("--corner-total-line", type=float)
    record.add_argument("--corner-total-odds", type=float)
    record.add_argument(
        "--corner-total-odds-format", choices=("decimal", "hong_kong")
    )
    record.add_argument("--corner-total-probability", type=float)
    record.add_argument("--corner-total-ev", type=float)
    record.add_argument("--corner-total-edge-pp", type=float)
    record.add_argument("--corner-total-firm-count", type=int)
    record.add_argument(
        "--corner-total-market-signal",
        choices=("aligned", "neutral", "against", "conflicting", "unknown"),
        default="unknown",
    )
    record.add_argument("--corner-total-market-complete", action="store_true")
    record.add_argument("--corner-total-market-probability", type=float)
    record.add_argument("--corner-total-market-source")
    record.add_argument("--corner-total-market-collected-at")
    record.add_argument(
        "--corner-total-price-basis", choices=("consensus", "median")
    )
    record.add_argument("--corner-total-full-win-probability", type=float)
    record.add_argument("--corner-total-half-win-probability", type=float)
    record.add_argument("--corner-total-push-probability", type=float)
    record.add_argument("--corner-total-half-loss-probability", type=float)
    record.add_argument("--corner-total-loss-probability", type=float)
    record.add_argument(
        "--corner-handicap-side", choices=("home", "away")
    )
    record.add_argument("--corner-handicap-line", type=float)
    record.add_argument("--corner-handicap-odds", type=float)
    record.add_argument(
        "--corner-handicap-odds-format", choices=("decimal", "hong_kong")
    )
    record.add_argument("--corner-handicap-probability", type=float)
    record.add_argument("--corner-handicap-ev", type=float)
    record.add_argument("--corner-handicap-edge-pp", type=float)
    record.add_argument("--corner-handicap-firm-count", type=int)
    record.add_argument(
        "--corner-handicap-market-signal",
        choices=("aligned", "neutral", "against", "conflicting", "unknown"),
        default="unknown",
    )
    record.add_argument(
        "--corner-handicap-market-complete", action="store_true"
    )
    record.add_argument("--corner-handicap-market-probability", type=float)
    record.add_argument("--corner-handicap-market-source")
    record.add_argument("--corner-handicap-market-collected-at")
    record.add_argument(
        "--corner-handicap-price-basis", choices=("consensus", "median")
    )
    record.add_argument("--corner-handicap-full-win-probability", type=float)
    record.add_argument("--corner-handicap-half-win-probability", type=float)
    record.add_argument("--corner-handicap-push-probability", type=float)
    record.add_argument("--corner-handicap-half-loss-probability", type=float)
    record.add_argument("--corner-handicap-loss-probability", type=float)
    record.add_argument("--force", action="store_true")

    review = sub.add_parser("review", help="Settle an archived prediction after verified full-time")
    review.add_argument(
        "--verified-finished",
        action="store_true",
        help="Required assertion that an explicit terminal match status was verified before settlement",
    )
    review.add_argument("--match-id", required=True)
    review.add_argument("--home-score", required=True, type=int)
    review.add_argument("--away-score", required=True, type=int)
    review.add_argument("--half-home-score", type=int)
    review.add_argument("--half-away-score", type=int)
    review.add_argument(
        "--home-corners",
        type=int,
        help="Verified 90-minute home corner count; required for a corner primary",
    )
    review.add_argument(
        "--away-corners",
        type=int,
        help="Verified 90-minute away corner count; required for a corner primary",
    )
    review.add_argument("--key-learning", required=True)

    migrate = sub.add_parser("migrate-primary", help="Backfill one active primary pick without re-settling reviewed matches")
    migrate.add_argument(
        "--primary",
        action="append",
        required=True,
        help="Repeatable MATCH_ID:MARKET[:HTFT_SELECTION] assignment",
    )
    migrate.add_argument("--write", action="store_true", help="Persist the compatibility migration")

    migrate_leagues = sub.add_parser(
        "migrate-leagues",
        help="Backfill normalized league keys without changing revisions or settlements",
    )
    migrate_leagues.add_argument("--write", action="store_true", help="Persist league-key migration")

    migrate_basis = sub.add_parser(
        "migrate-settlement-basis",
        help="Backfill active-version settlement metadata without re-grading matches",
    )
    migrate_basis.add_argument("--write", action="store_true", help="Persist settlement-basis metadata")

    migrate_learning = sub.add_parser(
        "migrate-learning-scopes",
        help="Backfill machine-readable review-learning metadata without re-grading",
    )
    migrate_learning.add_argument(
        "--write", action="store_true", help="Persist review-learning metadata"
    )

    sub.add_parser("pending", help="List pending pre-match predictions")
    due = sub.add_parser("due-lineup-check", help="List pending matches due in the final 30 minutes before kickoff")
    due.add_argument("--now", help="Override current time with an ISO datetime including timezone")
    due.add_argument("--min-minutes", type=float, default=0.0)
    due.add_argument("--max-minutes", type=float, default=30.0)
    sub.add_parser("stats", help="Print cumulative accuracy")
    calibrate = sub.add_parser("calibrate", help="Summarize reviewed performance and persist cautious calibration state")
    calibrate.add_argument("--write", action="store_true", help="Persist calibration.json beside history.json")
    calibrate.add_argument("--minimum-graded", type=int, default=20)
    calibrate.add_argument("--guardrail", action="append")
    return parser


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        path = data_path(args.base_dir)
        if args.command == "record":
            result = cmd_record(args)
        elif args.command == "review":
            result = cmd_review(args)
        elif args.command == "migrate-primary":
            result = cmd_migrate_primary(args)
        elif args.command == "migrate-leagues":
            result = cmd_migrate_leagues(args)
        elif args.command == "migrate-settlement-basis":
            result = cmd_migrate_settlement_basis(args)
        elif args.command == "migrate-learning-scopes":
            result = cmd_migrate_learning_scopes(args)
        elif args.command == "due-lineup-check":
            if args.min_minutes < 0 or args.max_minutes < args.min_minutes:
                raise ValueError("Require 0 <= min-minutes <= max-minutes")
            result = cmd_due_lineup_check(args)
        elif args.command == "calibrate":
            if args.minimum_graded < 1:
                raise ValueError("--minimum-graded must be at least 1")
            result = cmd_calibrate(args)
        else:
            history = load_history(path)
            if args.command == "pending":
                result = {"path": str(path), "pending": [r for r in history if r.get("mode") == "prematch" and r.get("status") == "pending"]}
            else:
                result = {"path": str(path), "stats": calculate_stats(history)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

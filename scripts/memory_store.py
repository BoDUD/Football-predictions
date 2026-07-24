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


PRIMARY_MARKETS = ("asian", "total", "half_time", "htft")
PICK_KEY_BY_MARKET = {
    "asian": "asian_pick",
    "total": "total_pick",
    "half_time": "half_time_pick",
    "htft": "htft_picks",
}
RESULT_KEY_BY_MARKET = {
    "asian": "asian_result",
    "total": "total_result",
    "half_time": "half_time_result",
}
PROVISIONAL_FORMAL_MIN_EV = 0.08
PROVISIONAL_FORMAL_MIN_EDGE_PP = 4.0
PROVISIONAL_MIN_FIRMS = 5
PROVISIONAL_LINEUP_CHANGE_MIN_EV_DELTA = 0.04
DEEP_FAVORITE_LINE = -0.75
DEFAULT_GUARDRAILS = [
    "小样本保护期内，所有市场主推必须满足EV>=8%、模型相对市场边际>=4pp且数据质量至少为medium；亚洲盘和大小球正式次推同样受此门槛约束。EV在5%-8%的方向只作观察，不得归档为正式方向。",
    "让球方达到-0.75或更深时，主推必须使用独立净胜球/穿盘分布，且具备确认首发、机会质量证据与对手尾部风险检查；不得用1X2胜率、强阵容或控球优势直接替代穿盘概率。",
    "临场主推变更必须记录原因。跨市场、反向或追更差盘口时，原主推须被硬信息证伪，新方向数据质量须为high、首发已确认，且当前EV至少比旧方向高4pp；否则维持原主推或取消主推，不强行寻找替代方向。",
    "盘口与相关欧赔同时明显反向时，普通低EV方向降为观察；仅当EV>=8%、边际>=4pp、至少5家公司且有独立阵容或基本面支持时可作正式方向，主推同样受此门槛约束。",
    "伤停表与确认首发冲突时，以确认首发为准；旧伤停不得继续作为进球或让球方向的支持证据。",
    "大小球降水不能单独构成主推依据；必须同时取得多家公司一致性和进攻配置或机会质量证据。",
    "两个精确比分候选仅作比赛形态参考；分别记录Top-1/Top-2诊断，不计入主推命中率与ROI。",
]
OBSOLETE_GUARDRAILS = {
    "若亚盘与相关欧赔一致明显反向，常规低EV方向降级为观察；只有EV>=8%、边际>=4pp、至少5家公司且有独立阵容或基本面证据时才能正式推荐。",
    "精确比分仅作比赛形态参考，不计入主推命中率。",
    "两个精确比分候选仅作比赛形态参考；分别记录Top-1/Top-2诊断，不计入主推或全部正式方向的命中率与ROI。",
}
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
    if parsed.tzinfo is None:
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


def parse_htft_pick(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.split(":")]
    if len(parts) != 4:
        raise ValueError("HT/FT pick must be SELECTION:ODDS:PROBABILITY:EV, for example DD:3.40:0.31:0.054")
    selection = parts[0].upper()
    if selection not in {a + b for a in "HDA" for b in "HDA"}:
        raise ValueError(f"Invalid HT/FT selection: {selection}")
    return {
        "selection": selection,
        "odds": float(parts[1]),
        "probability": float(parts[2]),
        "ev": float(parts[3]),
    }


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


def find_record(history: list[dict[str, Any]], match_id: str) -> dict[str, Any] | None:
    return next((item for item in history if str(item.get("match_id")) == str(match_id)), None)


def formal_picks(record: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    picks: list[tuple[str, dict[str, Any]]] = []
    for market in ("asian", "total", "half_time"):
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
    if number + 1e-9 < minimum:
        raise ValueError(f"{label} must be at least {minimum:g}; downgrade the candidate to observation")
    return number


def validate_provisional_formal_guardrails(record: dict[str, Any]) -> None:
    """Reject formal picks that do not satisfy the active small-sample guardrails."""
    evidence = record.get("guardrail_evidence", {})
    data_quality = str(record.get("data_quality") or "unknown")
    full_time_picks = [
        (market, pick)
        for market, pick in formal_picks(record)
        if market in {"asian", "total"}
    ]

    if full_time_picks and data_quality not in {"medium", "high"}:
        raise ValueError("Full-time formal picks require medium or high data quality")
    if full_time_picks and evidence.get("injury_evidence_status") == "stale_conflict":
        raise ValueError(
            "Stale injury evidence conflicts with the confirmed lineup; recalculate without it "
            "or archive no formal pick"
        )

    for market, pick in full_time_picks:
        require_minimum(pick.get("ev"), PROVISIONAL_FORMAL_MIN_EV, f"{market} EV")
        require_minimum(
            pick.get("edge_pp"),
            PROVISIONAL_FORMAL_MIN_EDGE_PP,
            f"{market} model-versus-market edge (pp)",
        )
        signal = str(pick.get("market_signal") or "unknown")
        if signal == "against":
            require_minimum(
                pick.get("firm_count"),
                PROVISIONAL_MIN_FIRMS,
                f"{market} bookmaker count",
            )
            if not (
                evidence.get("lineup_confirmed")
                or evidence.get("fundamental_supported")
            ):
                raise ValueError(
                    f"{market} against-market formal pick requires independent lineup "
                    "or fundamental evidence"
                )

    primary_market = record.get("primary_market")
    primary = record.get("primary_pick")
    if primary_market in {"half_time", "htft"} and isinstance(primary, dict):
        if data_quality not in {"medium", "high"}:
            raise ValueError(
                "A half-time or HT/FT primary requires medium or high data quality"
            )
        require_minimum(
            primary.get("ev"),
            PROVISIONAL_FORMAL_MIN_EV,
            f"{primary_market} primary EV",
        )
        primary_edge_pp = (
            primary.get("edge_pp")
            if primary_market == "half_time"
            else evidence.get("primary_htft_edge_pp")
        )
        require_minimum(
            primary_edge_pp,
            PROVISIONAL_FORMAL_MIN_EDGE_PP,
            f"{primary_market} primary model-versus-market edge (pp)",
        )
        if primary_market == "htft":
            require_minimum(
                evidence.get("primary_htft_firm_count"),
                PROVISIONAL_MIN_FIRMS,
                "HT/FT primary bookmaker count",
            )
    if primary_market == "total" and isinstance(primary, dict):
        require_minimum(
            primary.get("firm_count"),
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
                "Total primary requires chance-quality evidence or a confirmed attacking "
                "configuration; price movement alone is insufficient"
            )

    if (
        primary_market == "asian"
        and isinstance(primary, dict)
        and float(primary.get("line", 0.0)) <= DEEP_FAVORITE_LINE
    ):
        if data_quality != "high":
            raise ValueError("Asian favorite -0.75 or deeper requires high data quality")
        if not evidence.get("lineup_confirmed"):
            raise ValueError("Asian favorite -0.75 or deeper requires confirmed lineups")
        if not evidence.get("chance_quality_supported"):
            raise ValueError(
                "Asian favorite -0.75 or deeper requires independent chance-quality evidence"
            )
        if not evidence.get("opponent_tail_risk_checked"):
            raise ValueError(
                "Asian favorite -0.75 or deeper requires an opponent counterattack/"
                "goalkeeper/set-piece tail-risk check"
            )
        if not primary.get("cover_distribution_validated"):
            raise ValueError(
                "Asian favorite -0.75 or deeper requires an independently validated "
                "goal-margin/cover distribution"
            )
        cover_probability = primary.get("cover_probability")
        if cover_probability is None or not 0.0 <= float(cover_probability) <= 1.0:
            raise ValueError(
                "Asian favorite -0.75 or deeper requires --asian-cover-probability "
                "between 0 and 1"
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
    if market == "asian":
        return new_value < old_value
    if market == "total":
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

    if record.get("analysis_stage") != "lineup-check":
        return {
            "status": "initial",
            "previous": None,
            "current": list(current_identity) if current_identity else None,
            "reason": None,
            "previous_current_ev": None,
            "new_current_ev": current_ev,
            "ev_improvement": None,
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
    ev_improvement = None
    strict_replacement = previous_identity is not None and (
        not same_direction or worse_line
    )
    if strict_replacement:
        if not bool(getattr(args, "previous_primary_invalidated", False)):
            raise ValueError(
                "Cross-market, opposite-direction, or worse-line primary changes require "
                "--previous-primary-invalidated"
            )
        if previous_ev is None:
            raise ValueError(
                "A strict lineup-check primary change requires --previous-primary-current-ev"
            )
        if current_ev is None:
            raise ValueError("The new lineup-check primary requires a current EV")
        ev_improvement = current_ev - float(previous_ev)
        if ev_improvement + 1e-9 < PROVISIONAL_LINEUP_CHANGE_MIN_EV_DELTA:
            raise ValueError(
                "The new lineup-check primary EV must exceed the previous direction's "
                "current EV by at least 4 percentage points"
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
        "previous_invalidated": bool(
            getattr(args, "previous_primary_invalidated", False)
        ),
        "previous_current_ev": float(previous_ev) if previous_ev is not None else None,
        "new_current_ev": current_ev,
        "ev_improvement": ev_improvement,
        "worse_line": worse_line,
        "decision": decision,
        "guardrail_passed": True,
    }


def pick_identity(market: str | None, pick: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if not market or not isinstance(pick, dict):
        return None
    if market == "htft":
        return (market, str(pick.get("selection", "")).upper())
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


def primary_result_from_record(record: dict[str, Any]) -> str | None:
    market = record.get("primary_market")
    primary = record.get("primary_pick")
    if not market or not isinstance(primary, dict):
        return None
    if market in RESULT_KEY_BY_MARKET:
        result = record.get(RESULT_KEY_BY_MARKET[str(market)])
        return str(result) if result else None
    if market == "htft":
        selection = str(primary.get("selection", "")).upper()
        for result, pick in zip(record.get("htft_results", []), record.get("htft_picks", [])):
            if isinstance(pick, dict) and str(pick.get("selection", "")).upper() == selection:
                return str(result) if result else None
    return None


def revision_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_stage": record.get("analysis_stage", "initial"),
        "archived_at": record.get("updated_at", record.get("created_at")),
        "predicted_score": record.get("predicted_score"),
        "exact_score_picks": record.get("exact_score_picks", []),
        "recommendation": record.get("recommendation"),
        "notes": record.get("notes"),
        "data_quality": record.get("data_quality", "unknown"),
        "guardrail_evidence": record.get("guardrail_evidence", {}),
        "probabilities": record.get("probabilities"),
        "asian_pick": record.get("asian_pick"),
        "total_pick": record.get("total_pick"),
        "half_time_pick": record.get("half_time_pick"),
        "htft_picks": record.get("htft_picks", []),
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
        "primary_market": record.get("primary_market"),
        "primary_pick": deepcopy(record.get("primary_pick")),
        "formal_picks": {
            "asian": deepcopy(record.get("asian_pick")),
            "total": deepcopy(record.get("total_pick")),
            "half_time": deepcopy(record.get("half_time_pick")),
            "htft": deepcopy(record.get("htft_picks", [])),
        },
        "predicted_score": record.get("predicted_score"),
        "exact_score_picks": deepcopy(record.get("exact_score_picks", [])),
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
    if args.htft_pick:
        record["htft_picks"] = [parse_htft_pick(value) for value in args.htft_pick]

    apply_primary_role(record, args.primary_market, args.primary_htft_selection)
    validate_provisional_formal_guardrails(record)
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
    half_scores_available = args.half_home_score is not None and args.half_away_score is not None
    half_home = int(args.half_home_score) if half_scores_available else None
    half_away = int(args.half_away_score) if half_scores_available else None
    primary_market = settlement_basis.get("primary_market")
    primary_pick = settlement_basis.get("primary_pick")
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
    record.update({
        "status": "reviewed",
        "reviewed_at": now_iso(),
        "final_score": f"{home}-{away}",
        "score_exact": predicted_exact,
        "exact_score_hit_rank": exact_score_hit_rank,
        "exact_score_any_hit": exact_score_hit_rank in {1, 2},
        "asian_result": primary_result if primary_market == "asian" else None,
        "total_result": primary_result if primary_market == "total" else None,
        "half_time_score": f"{half_home}-{half_away}" if half_scores_available else None,
        "half_time_result": primary_result if primary_market == "half_time" else None,
        "htft_results": [],
        "primary_result": primary_result,
        "key_learning": args.key_learning,
        "league_key": league_key_for_record(record),
        "settlement_basis": settlement_basis,
    })
    warnings = []
    if primary_market in {"half_time", "htft"} and not half_scores_available:
        warnings.append("Half-time score was not supplied; the primary pick remains ungraded")
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


def settlement_profit(result: str, odds: Any) -> float | None:
    if odds is None:
        return None
    price = float(odds)
    return {
        "win": price,
        "half_win": price / 2,
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
        profits = [settlement_profit(result, pick.get("odds")) for result, pick in pairs]
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
        profits = [settlement_profit(result, pick.get("odds")) for result, pick in pairs]
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
        primary = record.get("primary_pick")
        if not isinstance(primary, dict):
            continue
        result = record.get("primary_result") or primary_result_from_record(record)
        if result:
            pairs.append((str(result), primary))
    return pairs


def primary_pairs_for_market(
    records: list[dict[str, Any]], market: str
) -> list[tuple[str, dict[str, Any]]]:
    pairs: list[tuple[str, dict[str, Any]]] = []
    for record in records:
        primary = record.get("primary_pick")
        if not isinstance(primary, dict) or record.get("primary_market") != market:
            continue
        result = record.get("primary_result") or primary_result_from_record(record)
        if result:
            pairs.append((str(result), primary))
    return pairs


def primary_market_performance(records: list[dict[str, Any]]) -> dict[str, Any]:
    asian = primary_pairs_for_market(records, "asian")
    totals = primary_pairs_for_market(records, "total")
    half_time = primary_pairs_for_market(records, "half_time")
    htft = primary_pairs_for_market(records, "htft")
    return {
        "asian": performance_block(asian, calculate_money=False),
        "totals": performance_block(totals, calculate_money=False),
        "half_time": performance_block(half_time, calculate_money=False),
        "htft": performance_block(htft, calculate_money=False),
        "combined": performance_block(asian + totals + half_time + htft, calculate_money=False),
    }


def exact_score_diagnostics(records: list[dict[str, Any]]) -> dict[str, Any]:
    top1 = sum((r.get("exact_score_hit_rank") == 1) or bool(r.get("score_exact")) for r in records)
    top2 = sum(
        (r.get("exact_score_hit_rank") in {1, 2})
        or (r.get("exact_score_hit_rank") is None and bool(r.get("score_exact")))
        for r in records
    )
    return {
        "top1_hits": top1,
        "top1_rate": round(top1 / len(records), 4) if records else None,
        "top2_hits": top2,
        "top2_rate": round(top2 / len(records), 4) if records else None,
    }


def league_performance(records: list[dict[str, Any]], league_key: str) -> dict[str, Any]:
    primary_by_market = primary_market_performance(records)
    learnings = [
        {
            "match_id": str(record.get("match_id")),
            "reviewed_at": record.get("reviewed_at"),
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
        "primary": performance_block(primary_pairs(records)),
        "primary_by_market": primary_by_market,
        "all_formal": primary_by_market,
        "secondary_tracking": "disabled",
        "asian": primary_by_market["asian"],
        "totals": primary_by_market["totals"],
        "half_time": primary_by_market["half_time"],
        "htft": primary_by_market["htft"],
        "exact_scores": exact_score_diagnostics(records),
        "recent_learnings": learnings,
    }


def calculate_stats(history: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = [r for r in history if r.get("mode") == "prematch" and r.get("status") == "reviewed"]
    primary = primary_pairs(reviewed)
    exact_scores = exact_score_diagnostics(reviewed)
    leagues: dict[str, dict[str, Any]] = {}
    for league_key in sorted({league_key_for_record(record) for record in reviewed}):
        subset = [record for record in reviewed if league_key_for_record(record) == league_key]
        leagues[league_key] = league_performance(subset, league_key)
    primary_by_market = primary_market_performance(reviewed)
    return {
        "reviewed_matches": len(reviewed),
        "pending_matches": sum(r.get("mode") == "prematch" and r.get("status") == "pending" for r in history),
        "primary": performance_block(primary),
        "primary_by_market": primary_by_market,
        "all_formal": primary_by_market,
        "secondary_tracking": "disabled",
        "asian": primary_by_market["asian"],
        "totals": primary_by_market["totals"],
        "half_time": primary_by_market["half_time"],
        "htft": primary_by_market["htft"],
        "combined": primary_by_market["combined"],
        "exact_scores": exact_scores["top1_hits"],
        "exact_score_rate": exact_scores["top1_rate"],
        "exact_score_top1_hits": exact_scores["top1_hits"],
        "exact_score_top1_rate": exact_scores["top1_rate"],
        "exact_score_top2_hits": exact_scores["top2_hits"],
        "exact_score_top2_rate": exact_scores["top2_rate"],
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

    def roi_text(block: dict[str, Any]) -> str:
        roi = block.get("roi")
        return "—" if roi is None else f"{float(roi) * 100:+.2f}%"

    return (
        f"已复盘{stats['reviewed_matches']}场，按{len(stats['leagues'])}个联赛归类；"
        f"主推{primary['matches']}场"
        f"{primary['wins']}胜{primary['losses']}负{primary['pushes']}走，"
        f"收益{primary['profit_units']:+.2f}u，ROI {roi_text(primary)}。"
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
            for market in ("asian", "totals", "half_time", "htft")
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
        for market in ("asian", "totals", "half_time", "htft")
    }
    calibration = {
        "updated_at": now_iso(),
        "history_path": str(history_file),
        "reviewed_matches": stats["reviewed_matches"],
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
    record.add_argument("--recommendation", default="")
    record.add_argument("--source-url", default="")
    record.add_argument("--notes", default="")
    record.add_argument("--data-quality", choices=("high", "medium", "low", "unknown"), default="unknown")
    record.add_argument("--lineup-confirmed", action="store_true")
    record.add_argument("--fundamental-evidence", action="store_true")
    record.add_argument("--chance-quality-evidence", action="store_true")
    record.add_argument("--attack-configuration-evidence", action="store_true")
    record.add_argument("--opponent-tail-risk-checked", action="store_true")
    record.add_argument(
        "--injury-evidence-status",
        choices=("not_used", "fresh", "confirmed_override", "stale_conflict"),
        default="not_used",
    )
    record.add_argument("--primary-change-reason", default="")
    record.add_argument("--previous-primary-invalidated", action="store_true")
    record.add_argument("--previous-primary-current-ev", type=float)
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
    record.add_argument("--half-probability", type=float)
    record.add_argument("--half-ev", type=float)
    record.add_argument("--half-edge-pp", type=float)
    record.add_argument("--half-firm-count", type=int)
    record.add_argument("--half-market-signal", choices=("aligned", "neutral", "against", "conflicting", "unknown"), default="unknown")
    record.add_argument("--htft-pick", action="append", help="Repeatable SELECTION:ODDS:PROBABILITY:EV, e.g. DD:3.40:0.31:0.054")
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

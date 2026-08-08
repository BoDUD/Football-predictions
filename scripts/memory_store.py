#!/usr/bin/env python3
"""Deterministic workspace-local storage for soccer-predict."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from scripts import (
        cohort_scope,
        corner_ranker,
        forward_policy,
        fundamental_evidence,
        htft_ranker,
        joint_scenario_model,
        source_evidence,
    )
except ImportError:  # Direct execution from scripts/.
    script_directory = str(Path(__file__).resolve().parent)
    if script_directory not in sys.path:
        sys.path.insert(0, script_directory)
    import cohort_scope  # type: ignore[no-redef]
    import corner_ranker  # type: ignore[no-redef]
    import forward_policy  # type: ignore[no-redef]
    import fundamental_evidence  # type: ignore[no-redef]
    import htft_ranker  # type: ignore[no-redef]
    import joint_scenario_model  # type: ignore[no-redef]
    import source_evidence  # type: ignore[no-redef]

try:
    from soccer_predict.domain.probabilities import (
        effective_settlement_win_probability,
        matrix_1x2,
        matrix_settlement_distribution,
        validate_probability_matrix,
    )
    from soccer_predict.domain.settlement import (
        parse_goal_range_selection,
        result_code,
        settle_asian,
        settle_btts,
        settle_corner_handicap,
        settle_corner_total,
        settle_goal_range,
        settle_half_time,
        settle_htft,
        settle_total,
        split_line,
    )
except ImportError:  # Direct execution from scripts/ without an installed package.
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from soccer_predict.domain.probabilities import (  # type: ignore[no-redef]
        effective_settlement_win_probability,
        matrix_1x2,
        matrix_settlement_distribution,
        validate_probability_matrix,
    )
    from soccer_predict.domain.settlement import (  # type: ignore[no-redef]
        parse_goal_range_selection,
        result_code,
        settle_asian,
        settle_btts,
        settle_corner_handicap,
        settle_corner_total,
        settle_goal_range,
        settle_half_time,
        settle_htft,
        settle_total,
        split_line,
    )


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
FOOTBALL_MODEL_MARKETS = (
    "asian",
    "total",
    "half_time",
    "htft",
    "goal_range",
    "btts",
)
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
ODDS_AUDIT_TOLERANCE = 0.0005
ONE_X_TWO_LOG_LOSS_FLOOR = 1e-15
HTFT_LOG_LOSS_FLOOR = 1e-15
DEEP_FAVORITE_LINE = -0.75
CONFIDENCE_RANKING_VERSION = "stability-v2"
CONFIDENCE_POLICY_VERSION = "independent-settlement-risk-v2"
STRICT_OOS_POLICY_VERSION = "strict-oos-market-policy-v1"
STRICT_OOS_MARKET_STATUS = {
    "asian": {
        "status": "observation_only",
        "paused_reason": "strict-forward historical accuracy and ROI remain below the release gate",
    },
    "half_time": {
        "status": "observation_only",
        "paused_reason": "strict-forward sample is insufficient and historical results are unstable",
    },
    "htft": {
        "status": "observation_only",
        "paused_reason": "strict-forward sample is insufficient and historical results are unstable",
    },
    "corner_total": {
        "status": "observation_only",
        "paused_reason": "the corner model has historical training evidence only; registry-bound live-forward validation is not yet available",
    },
    "corner_handicap": {
        "status": "observation_only",
        "paused_reason": "the corner model has historical training evidence only; registry-bound live-forward validation is not yet available",
    },
}
PRIMARY_SELECTION_BASIS = "highest_independent_settlement_risk_confidence"
ADVERSE_MARKET_SIGNALS = {"against", "conflicting"}
HTFT_OUTCOMES = tuple(f"{half}{full}" for half in "HDA" for full in "HDA")
OBSERVATION_SCHEMA_VERSION = "candidate-observation/1.0.0"
CANDIDATE_EVALUATION_SCHEMA_VERSION = "candidate-evaluation/3.0.0"
LEGACY_CANDIDATE_EVALUATION_SCHEMA_VERSION = "candidate-evaluation/2.0.0"
CANDIDATE_EVALUATION_ARTIFACT_TYPE = "soccer_candidate_evaluation"
CANDIDATE_EVALUATION_KIND = "multi_market_candidate_evaluation"
CANDIDATE_GATE_CATEGORIES = ("integrity", "value", "risk", "release")
ACTIVE_UNAVAILABLE_REASON_SOURCE_MISSING = "source_market_identity_unavailable"
ACTIVE_UNAVAILABLE_REASON_CORNER_MODEL_MISSING = "canonical_corner_observation_missing"
ACTIVE_UNAVAILABLE_REASON_DECIMAL_PRICE_MISSING = (
    "canonical_decimal_price_snapshot_unavailable"
)
ACTIVE_UNAVAILABLE_REASON_DECISION_TIME_PRICE_MISSING = (
    "decision_time_price_snapshot_unavailable"
)
CANDIDATE_SETTLEMENT_STATES = (
    "full_win",
    "half_win",
    "push",
    "half_loss",
    "loss",
)
PREVIOUS_FORWARD_LEDGER_ARCHIVE_SCHEMA_VERSION = "memory-forward-ledger-archive/1.0.0"
FORWARD_LEDGER_ARCHIVE_SCHEMA_VERSION = "memory-forward-ledger-archive/2.0.0"
PREVIOUS_FORWARD_RECORD_PREDICTION_SCHEMA_VERSION = "memory-forward-prediction/1.0.0"
FORWARD_RECORD_PREDICTION_SCHEMA_VERSION = "memory-forward-prediction/2.0.0"
PREVIOUS_FORWARD_RECORD_COMMITMENT_SCHEMA_VERSION = "memory-forward-commitment/1.0.0"
FORWARD_RECORD_COMMITMENT_SCHEMA_VERSION = "memory-forward-commitment/2.0.0"
PREVIOUS_FORWARD_HISTORY_LEDGER_BINDING_SCHEMA_VERSION = (
    "memory-forward-history-ledger-binding/2.0.0"
)
FORWARD_HISTORY_LEDGER_BINDING_SCHEMA_VERSION = (
    "memory-forward-history-ledger-binding/3.0.0"
)
PREVIOUS_FORWARD_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION = (
    "memory-forward-record-receipt/1.0.0"
)
FORWARD_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION = "memory-forward-record-receipt/2.0.0"
FORWARD_HISTORY_AGGREGATE_ARTIFACT_TYPE = (
    "memory_store_forward_validation_cohort_export"
)
JOINT_SCENARIO_AUDIT_SCHEMA_VERSION = "joint-scenario-audit/1.0.0"
MARKET_EVIDENCE_MAX_AGE_MINUTES_BY_STAGE = {
    "initial": 60,
    "lineup-check": 30,
}
CORNER_OBSERVATION_KIND = "corner_market_observation"
CORNER_OBSERVATION_GATE_ORDER = (
    "complete_current_market",
    "odds_provenance",
    "positive_ev",
    "positive_edge",
    "bookmaker_depth",
    "data_quality",
    "corner_profile_evidence",
    "market_signal_classified",
    "adverse_signal_gate",
    "registered_model_input",
    "deployment_candidate",
    "upstream_formal_policy",
)
OBSERVATION_GATE_ORDER = (
    "complete_current_market",
    "odds_provenance",
    "positive_ev",
    "positive_edge",
    "bookmaker_depth",
    "data_quality",
    "scenario_stability",
    "scenario_coherence",
    "descriptive_pair_mass_threshold",
    "league_forward_evidence",
    "market_policy_enabled",
)
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
    "stability-v2 uses independent settlement-risk, data quality, market depth, independent evidence, and alignment for ranking.",
    "EV and no-vig edge are positive eligibility gates only and never contribute confidence-score points.",
    "盘口与相关欧赔明显反向或冲突时，仍须EV>=8%、边际>=4pp、至少5家公司且有独立阵容或基本面支持，主推与正式次推均不例外。",
    "临场换推以当前综合置信度为准：原主推未失效时，新方向至少高5分；原主推被硬信息证伪时可取消或换为新的安全rank=1方向。",
    "深盘、大小球、伤停冲突与精确比分继续执行专项保护；没有安全候选时允许无主推，不强行下注。",
]
LEAGUE_ALIASES = {
    "巴甲": "brazil_serie_a",
    "巴西联赛": "brazil_serie_a",
    "日职": "japan_j1",
    "挪超": "norway_eliteserien",
    "美职联": "usa_mls",
    "芬超": "finland_veikkausliiga",
    "韩K联": "korea_k_league_1",
    "韩国K联": "korea_k_league_1",
    "韩国K联赛": "korea_k_league_1",
    "K联赛": "korea_k_league_1",
    "瑞典超": "sweden_allsvenskan",
    "英超": "england_premier_league",
    "巴西杯": "brazil_cup",
    "英联杯": "england_league_cup",
    "英格兰联赛杯": "england_league_cup",
    "EFLCup": "england_league_cup",
    "LeagueCup": "england_league_cup",
    "CarabaoCup": "england_league_cup",
    "荷乙": "netherlands_eerste_divisie",
    "EersteDivisie": "netherlands_eerste_divisie",
    "KeukenKampioenDivisie": "netherlands_eerste_divisie",
    "NetherlandsEersteDivisie": "netherlands_eerste_divisie",
    "法甲": "france_ligue_1",
    "西甲": "spain_la_liga",
    "德甲": "germany_bundesliga",
    "意甲": "italy_serie_a",
    "葡超": "portugal_primeira_liga",
    "葡萄牙超级联赛": "portugal_primeira_liga",
    "PrimeiraLiga": "portugal_primeira_liga",
    "LigaPortugal": "portugal_primeira_liga",
    "欧冠": "uefa_champions_league",
    "欧联": "uefa_europa_league",
    "亚冠": "afc_champions_league",
    "欧国联": "uefa_nations_league",
}
LEAGUE_STAGE_SUFFIX = re.compile(
    r"(?:"
    r"(?:常规赛|小组赛|资格赛|预选赛|附加赛)?第?\d+(?:轮|周|阶段)|"
    r"(?:1/16|1/8|1/4)决赛(?:(?:首|次)回合)?|"
    r"(?:十六强|16强|八强|8强|四分之一决赛|半决赛|决赛)(?:(?:首|次)回合)?"
    r")$"
)
COMPETITION_EVIDENCE_SCHEMA_VERSION = "titan-fixture-competition/1.1.0"
SETTLEMENT_IDENTITY_MIGRATION_SCHEMA_VERSION = (
    "legacy-settlement-competition-identity/1.0.0"
)
COMPETITION_KEY_PATTERN = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")
COMPETITION_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")
COMPETITION_SOURCE_PATTERN = re.compile(
    r"https://zq\.titan007\.com/analysis/(?P<match_id>\d+)cn\.htm\Z",
    re.IGNORECASE,
)
COMPETITION_LOCATOR_PATTERN = re.compile(
    r"(?:https?:)?//(?:info|zq)\.titan007\.com/",
    re.IGNORECASE,
)
COMPETITION_EVIDENCE_REGISTRY = {
    "brazil_cup": {
        "label": "巴西杯",
        "source_id": "186",
        "locator_pattern": (
            r"(?:https?:)?//info\.titan007\.com/cup_match/[^?#]+/"
            r"cupmatch_186\.htm(?:[?#].*)?\Z"
        ),
    },
}
TITAN_COMPETITION_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://zq.titan007.com/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}


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


@contextmanager
def history_lock(path: Path):
    """Hold an inter-process exclusive lock for a history read/modify/write.

    The lock lives beside ``history.json`` and deliberately covers the full
    transaction, not merely the final atomic replace.  That prevents two
    scheduler/CLI processes from both reading the same old array and silently
    discarding one another's update.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def locked_history_transaction(function):
    """Serialize a command that may rewrite ``history.json``."""

    @wraps(function)
    def wrapped(args: argparse.Namespace, *function_args, **function_kwargs):
        with history_lock(data_path(args.base_dir)):
            return function(args, *function_args, **function_kwargs)

    return wrapped


def save_history(path: Path, history: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(path)


def utc_now() -> datetime:
    """Patchable clock used by archive timing and audit timestamps."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().astimezone(timezone.utc).replace(microsecond=0).isoformat()


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


def parse_aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a parseable datetime with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include timezone")
    return parsed


def validate_market_evidence_freshness(
    record: dict[str, Any], captured_at: Any, label: str
) -> datetime:
    """Fail closed when current-market evidence is too old for its archive stage."""
    stage = str(record.get("analysis_stage") or "initial").strip()
    maximum_minutes = MARKET_EVIDENCE_MAX_AGE_MINUTES_BY_STAGE.get(stage)
    if maximum_minutes is None:
        raise ValueError(f"{label} cannot be checked for unsupported stage {stage!r}")
    archive_value = record.get("version_archived_at")
    if archive_value is None:
        archive_value = record.get("archived_at")
    if archive_value is None:
        archive_value = record.get("updated_at", record.get("created_at"))
    archive_time = parse_aware_datetime(
        str(archive_value or ""), f"{label} archive time"
    ).astimezone(timezone.utc)
    captured_time = parse_aware_datetime(str(captured_at or ""), label).astimezone(
        timezone.utc
    )
    if captured_time > archive_time:
        raise ValueError(f"{label} cannot be after archive time")
    age = archive_time - captured_time
    maximum_age = timedelta(minutes=maximum_minutes)
    if age > maximum_age:
        age_minutes = age.total_seconds() / 60.0
        raise ValueError(
            f"{label} is stale for {stage}: {age_minutes:.1f} minutes old "
            f"exceeds the {maximum_minutes}-minute limit"
        )
    return captured_time


def validate_candidate_audit_freshness(record: dict[str, Any]) -> None:
    """Validate every archived candidate market snapshot, including nested copies."""

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                child_path = f"{path}.{key}"
                if key == "market_collected_at" and str(item or "").strip():
                    validate_market_evidence_freshness(record, item, child_path)
                else:
                    walk(item, child_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    audits = record.get("candidate_audits", [])
    if not isinstance(audits, list):
        raise ValueError("candidate_audits must be a list")
    walk(audits, "candidate_audits")


def parse_timezone(value: Any, label: str):
    name = str(value or "").strip()
    if not name:
        raise ValueError(f"{label} is required")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        # Windows Python installations may not bundle the IANA tz database.
        # These two non-DST zones are the mandatory source/user zones for this
        # project, so retain strict named-zone semantics without a tzdata wheel.
        fixed = {
            "Asia/Shanghai": timezone(timedelta(hours=8), name="Asia/Shanghai"),
            "Asia/Tokyo": timezone(timedelta(hours=9), name="Asia/Tokyo"),
            "UTC": timezone.utc,
            "Etc/UTC": timezone.utc,
        }.get(name)
        if fixed is not None:
            return fixed
        raise ValueError(f"{label} must be a valid IANA timezone: {name}") from exc


def validate_datetime_zone(
    value: str,
    zone_name: str,
    value_label: str,
    zone_label: str,
) -> datetime:
    parsed = parse_aware_datetime(value, value_label)
    zone = parse_timezone(zone_name, zone_label)
    represented = parsed.astimezone(zone)
    if (
        parsed.replace(tzinfo=None) != represented.replace(tzinfo=None)
        or parsed.utcoffset() != represented.utcoffset()
    ):
        raise ValueError(
            f"{value_label} does not represent its wall time in {zone_label}={zone_name}"
        )
    return parsed


def validate_record_time_metadata(
    args: argparse.Namespace,
    current: datetime,
) -> dict[str, str]:
    page_status = str(getattr(args, "page_status", "") or "").strip().lower()
    if page_status != "prematch":
        raise ValueError("New prediction archives require page_status=prematch")

    source_timezone = str(getattr(args, "source_timezone", "") or "").strip()
    user_timezone = str(getattr(args, "user_timezone", "") or "").strip()
    source_text = str(getattr(args, "source_kickoff", "") or "").strip()
    user_text = str(getattr(args, "user_local_kickoff", "") or "").strip()
    kickoff_text = str(getattr(args, "kickoff", "") or "").strip()
    source = validate_datetime_zone(
        source_text,
        source_timezone,
        "source_kickoff",
        "source_timezone",
    )
    user_local = validate_datetime_zone(
        user_text,
        user_timezone,
        "user_local_kickoff",
        "user_timezone",
    )
    kickoff = parse_aware_datetime(kickoff_text, "kickoff")
    if source.astimezone(timezone.utc) != user_local.astimezone(timezone.utc):
        raise ValueError(
            "source_kickoff and user_local_kickoff must be the same instant"
        )
    if kickoff.astimezone(timezone.utc) != user_local.astimezone(timezone.utc):
        raise ValueError("kickoff must equal user_local_kickoff")
    if (
        kickoff.replace(tzinfo=None) != user_local.replace(tzinfo=None)
        or kickoff.utcoffset() != user_local.utcoffset()
    ):
        raise ValueError("kickoff must use the user-local wall time and offset")

    current_utc = current.astimezone(timezone.utc)
    kickoff_utc = kickoff.astimezone(timezone.utc)
    seconds_to_kickoff = (kickoff_utc - current_utc).total_seconds()
    if seconds_to_kickoff <= 0:
        raise ValueError("New predictions cannot be archived at or after kickoff")
    if getattr(args, "analysis_stage", "initial") == "lineup-check":
        if seconds_to_kickoff > 30 * 60:
            raise ValueError(
                "A lineup-check archive is allowed only from T-30 until kickoff"
            )

    return {
        "page_status": "prematch",
        "source_kickoff": source.isoformat(),
        "source_timezone": source_timezone,
        "user_local_kickoff": user_local.isoformat(),
        "user_timezone": user_timezone,
        "kickoff": kickoff.isoformat(),
    }


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


def competition_identity_record(record: dict[str, Any]) -> dict[str, Any]:
    """Use settlement-frozen competition metadata for reviewed records."""

    basis = record.get("settlement_basis")
    if record.get("status") != "reviewed" or not isinstance(basis, dict):
        return record
    frozen = dict(record)
    for key in (
        "match_id",
        "home_team",
        "away_team",
        "kickoff",
        "source_url",
        "league",
        "league_key",
        "competition_evidence",
    ):
        # A reviewed record must never fill a missing settlement identity field
        # from mutable top-level metadata.  Legacy records remain fail-closed
        # (unknown competition) until the explicit settlement migration freezes
        # their preserved top-level league identity.
        frozen[key] = deepcopy(basis.get(key))
    return frozen


def competition_key_for_record(record: dict[str, Any]) -> str:
    """Return the real competition cohort, separate from any proxy model key."""

    identity = competition_identity_record(record)
    evidence = validated_competition_evidence(identity)
    if isinstance(evidence, dict):
        competition = evidence.get("competition")
        if isinstance(competition, dict):
            key = str(competition.get("key") or "").strip()
            if key:
                return key
    return league_key_for_record(identity)


def validate_probability_close(actual: Any, expected: float, label: str) -> None:
    if actual is None or not math.isfinite(float(actual)):
        raise ValueError(f"{label} is required and must be finite")
    if abs(float(actual) - expected) > PROBABILITY_AUDIT_TOLERANCE:
        raise ValueError(
            f"{label} does not match the archived score-model distribution "
            f"({float(actual):.6f} vs {expected:.6f})"
        )


def normalize_score_model_loss_keys(value: Any, path: str = "score_model") -> Any:
    """Map score-model ``full_loss`` to the store's legacy ``loss`` key."""
    if isinstance(value, list):
        return [
            normalize_score_model_loss_keys(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return deepcopy(value)
    if "full_loss" in value and "loss" in value:
        raise ValueError(
            f"{path} cannot contain both full_loss and loss; stored loss means full_loss"
        )
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = "loss" if key == "full_loss" else key
        normalized[normalized_key] = normalize_score_model_loss_keys(
            item, f"{path}.{key}"
        )
    return normalized


def load_score_model_provenance(args: argparse.Namespace) -> dict[str, Any] | None:
    supplied = str(getattr(args, "score_model_file", "") or "").strip()
    if not supplied:
        if str(getattr(args, "model_version", "") or "").strip():
            raise ValueError("--model-version requires --score-model-file")
        return None
    path = Path(supplied).expanduser()
    if not path.is_absolute():
        base = (
            Path(args.base_dir).expanduser().resolve() if args.base_dir else Path.cwd()
        )
        path = (base / path).resolve()
    raw = path.read_bytes()
    try:
        snapshot = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("score-model file must be UTF-8 JSON") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("score-model JSON must be an object")
    if snapshot.get("artifact_type") != "soccer_score_prediction":
        raise ValueError(
            "score-model file must have artifact_type=soccer_score_prediction"
        )
    cli_version = str(getattr(args, "model_version", "") or "").strip()
    file_version = str(snapshot.get("model_version") or "").strip()
    if not file_version:
        raise ValueError("score prediction artifact requires embedded model_version")
    if cli_version and cli_version != file_version:
        raise ValueError("--model-version must match score-model JSON model_version")
    embedded_model_hash = str(snapshot.get("model_hash") or "").strip()
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", embedded_model_hash):
        raise ValueError(
            "score prediction artifact requires a valid embedded model_hash"
        )
    score_matrix_payload = snapshot.get("score_matrix")
    if not isinstance(score_matrix_payload, dict):
        raise ValueError("score_matrix must be an object with probabilities")
    score_matrix = validate_probability_matrix(
        score_matrix_payload.get("probabilities"),
        "score_matrix.probabilities",
    )
    generated_at = parse_aware_datetime(
        str(snapshot.get("generated_at") or ""),
        "score prediction generated_at",
    )
    fixture = snapshot.get("fixture")
    if not isinstance(fixture, dict):
        raise ValueError("score prediction artifact requires fixture metadata")
    fixture_kickoff = parse_aware_datetime(
        str(fixture.get("kickoff") or ""),
        "score prediction fixture.kickoff",
    )
    if generated_at >= fixture_kickoff:
        raise ValueError("score prediction generated_at must be before fixture.kickoff")
    artifact_provenance = snapshot.get("provenance")
    if not isinstance(artifact_provenance, dict):
        raise ValueError("score prediction artifact requires provenance metadata")
    training = artifact_provenance.get("training")
    if not isinstance(training, dict):
        raise ValueError("score prediction provenance requires training metadata")
    source_data_hash = str(training.get("source_data_hash") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", source_data_hash):
        raise ValueError(
            "score prediction provenance training.source_data_hash must be a SHA-256 hash"
        )
    if artifact_provenance.get("strictly_before_kickoff_utc_date") is not True:
        raise ValueError(
            "score prediction provenance strictly_before_kickoff_utc_date must be true"
        )
    if artifact_provenance.get("generated_before_kickoff") is not True:
        raise ValueError(
            "score prediction provenance generated_before_kickoff must be true"
        )

    def parse_training_date(value: Any, label: str) -> date:
        raw_date = str(value or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            raise ValueError(f"score prediction provenance {label} must be an ISO date")
        try:
            return date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError(
                f"score prediction provenance {label} must be a valid ISO date"
            ) from exc

    training_end = parse_training_date(training.get("end_date"), "training.end_date")
    training_cutoff = parse_training_date(
        artifact_provenance.get("training_cutoff_date"), "training_cutoff_date"
    )
    if training_cutoff != training_end:
        raise ValueError(
            "score prediction provenance training_cutoff_date must equal training.end_date"
        )
    kickoff_utc_date = fixture_kickoff.astimezone(timezone.utc).date()
    if training_end >= kickoff_utc_date or training_cutoff >= kickoff_utc_date:
        raise ValueError(
            "score prediction training cutoff must be strictly before fixture kickoff UTC date"
        )
    tail = snapshot.get("tail_mass")
    if not isinstance(tail, dict) or tail.get("tolerance_met") is not True:
        raise ValueError("score prediction tail_mass.tolerance_met must be true")
    try:
        raw_omitted = float(tail.get("raw_omitted_probability"))
        tolerance = float(tail.get("tolerance"))
    except (TypeError, ValueError) as exc:
        raise ValueError("score prediction tail mass values must be numeric") from exc
    if (
        not math.isfinite(raw_omitted)
        or not math.isfinite(tolerance)
        or raw_omitted < 0.0
        or not 0.0 < tolerance < 1.0
        or raw_omitted > tolerance + 1e-12
    ):
        raise ValueError("score prediction raw omitted tail must not exceed tolerance")
    normalized_snapshot = normalize_score_model_loss_keys(snapshot)
    normalized_snapshot["model_version"] = file_version
    normalized_snapshot["score_matrix"]["probabilities"] = score_matrix
    if "half_time_score_matrix" in snapshot:
        normalized_snapshot["half_time_score_matrix"] = validate_probability_matrix(
            snapshot["half_time_score_matrix"], "half_time_score_matrix"
        )
    if "htft_matrix" in snapshot:
        htft = snapshot["htft_matrix"]
        if not isinstance(htft, dict):
            raise ValueError("htft_matrix must be an object keyed by HH, HD, ..., AA")
        selections = {a + b for a in "HDA" for b in "HDA"}
        if set(htft) != selections:
            raise ValueError(
                "htft_matrix must contain exactly the nine HT/FT selections"
            )
        converted: dict[str, float] = {}
        for selection in sorted(selections):
            value = htft[selection]
            if (
                isinstance(value, bool)
                or not math.isfinite(float(value))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(
                    f"htft_matrix {selection} must be finite and between 0 and 1"
                )
            converted[selection] = float(value)
        if abs(sum(converted.values()) - 1.0) > PROBABILITY_AUDIT_TOLERANCE:
            raise ValueError("htft_matrix probabilities must sum to 1")
        normalized_snapshot["htft_matrix"] = converted
    return {
        "model_version": file_version,
        "model_hash": embedded_model_hash,
        "artifact_sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        "artifact_filename": path.name,
        "raw_snapshot": snapshot,
        "snapshot": normalized_snapshot,
        "score_matrix": score_matrix,
        "generated_at": generated_at.isoformat(),
        "fixture": deepcopy(fixture),
    }


def resolve_observation_input_path(
    args: argparse.Namespace,
    supplied: str,
    label: str,
) -> Path:
    path = Path(supplied).expanduser()
    if not path.is_absolute():
        base = (
            Path(args.base_dir).expanduser().resolve() if args.base_dir else Path.cwd()
        )
        path = (base / path).resolve()
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    return path


def load_observation_json(
    args: argparse.Namespace,
    supplied: str,
    label: str,
) -> tuple[Path, bytes, dict[str, Any]]:
    path = resolve_observation_input_path(args, supplied, label)
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return path, raw, payload


def require_sha256(value: Any, label: str) -> str:
    normalized = str(value or "").lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
        raise ValueError(f"{label} must be a sha256: hash")
    return normalized


def canonical_prediction_hash(payload: dict[str, Any]) -> str:
    value = deepcopy(payload)
    value.pop("prediction_hash", None)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "HT/FT observation model contains non-canonical values"
        ) from exc
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class _TitanCompetitionHeaderParser(HTMLParser):
    """Extract the visible competition link and the two header team links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capture: tuple[str, str] | None = None
        self._text: list[str] = []
        self.competitions: list[tuple[str, str]] = []
        self.teams: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a" or self._capture is not None:
            return
        attributes = {key.casefold(): value or "" for key, value in attrs}
        href = attributes.get("href", "").strip()
        classes = set(attributes.get("class", "").split())
        if "LName" in classes:
            self._capture = ("competition", href)
            self._text = []
        elif "/team/Summary/" in href:
            self._capture = ("team", href)
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or self._capture is None:
            return
        kind, href = self._capture
        text = unicodedata.normalize("NFKC", "".join(self._text)).strip()
        if text:
            target = self.competitions if kind == "competition" else self.teams
            target.append((href, text))
        self._capture = None
        self._text = []


def _source_team_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"^\[[^\]]+\]\s*", "", text)
    text = re.sub(r"\s*\(主\)\s*$", "", text)
    return re.sub(r"\s+", "", text)


def _decode_titan_html(raw: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Titan analysis page is not valid UTF-8 or GB18030 HTML")


def extract_titan_competition_snapshot(
    raw_html: bytes,
    *,
    source_url: str,
    response_url: str,
    record: dict[str, Any],
    collected_at: datetime,
    etag: str = "",
    last_modified: str = "",
) -> dict[str, Any]:
    """Derive competition evidence from the actual Titan analysis-page header."""

    if not raw_html or len(raw_html) > 2_000_000:
        raise ValueError("Titan analysis page size is invalid")
    parser = _TitanCompetitionHeaderParser()
    parser.feed(_decode_titan_html(raw_html))
    if len(parser.competitions) != 1:
        raise ValueError("Titan analysis header must contain one competition link")
    if len(parser.teams) < 2:
        raise ValueError("Titan analysis header does not contain both team links")

    locator, label = parser.competitions[0]
    competition_id_match = re.search(r"cupmatch_(\d+)\.htm(?:[?#].*)?\Z", locator)
    if competition_id_match is None:
        raise ValueError(
            "Titan competition header link has no supported competition id"
        )
    header_home = _source_team_name(parser.teams[0][1])
    header_away = _source_team_name(parser.teams[1][1])
    expected_home = _source_team_name(record.get("home_team"))
    expected_away = _source_team_name(record.get("away_team"))
    if not expected_home or not expected_away:
        raise ValueError("archived fixture teams are missing")
    if header_home != expected_home or header_away != expected_away:
        raise ValueError(
            "Titan analysis header teams do not match the archived fixture"
        )

    return {
        "source_url": source_url,
        "response_url": response_url,
        "page_sha256": f"sha256:{hashlib.sha256(raw_html).hexdigest()}",
        "etag": str(etag or ""),
        "last_modified": str(last_modified or ""),
        "collected_at": collected_at.isoformat(),
        "header": {
            "home_team": parser.teams[0][1],
            "away_team": parser.teams[1][1],
            "competition_label": label,
            "competition_id": competition_id_match.group(1),
            "competition_locator": locator,
        },
    }


def fetch_titan_competition_snapshot(
    record: dict[str, Any], *, timeout_seconds: float = 20.0
) -> dict[str, Any]:
    """Fetch and parse the exact archived Titan fixture page before metadata writes."""

    source_url = str(record.get("source_url") or "").strip()
    source_match = COMPETITION_SOURCE_PATTERN.fullmatch(source_url)
    if source_match is None or source_match.group("match_id") != str(
        record.get("match_id") or ""
    ):
        raise ValueError(
            "competition verification requires the matching Titan analysis URL"
        )
    request = Request(source_url, headers=TITAN_COMPETITION_FETCH_HEADERS)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_html = response.read(2_000_001)
            response_url = str(response.geturl())
            etag = str(response.headers.get("ETag") or "")
            last_modified = str(response.headers.get("Last-Modified") or "")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"cannot fetch Titan competition evidence: {exc}") from exc
    if response_url != source_url:
        raise ValueError(
            "Titan competition evidence redirected away from the archived source"
        )
    return extract_titan_competition_snapshot(
        raw_html,
        source_url=source_url,
        response_url=response_url,
        record=record,
        collected_at=utc_now(),
        etag=etag,
        last_modified=last_modified,
    )


def _competition_evidence_payload(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return the exact hash-bound fixture competition metadata."""

    return {
        "schema_version": evidence.get("schema_version"),
        "status": evidence.get("status"),
        "fixture": deepcopy(evidence.get("fixture")),
        "competition": deepcopy(evidence.get("competition")),
        "source": deepcopy(evidence.get("source")),
    }


def calculate_competition_evidence_hash(evidence: dict[str, Any]) -> str:
    return canonical_prediction_hash(_competition_evidence_payload(evidence))


def build_competition_evidence(
    record: dict[str, Any],
    *,
    competition_key: Any,
    competition_label: Any,
    competition_id: Any,
    verification_source: Any,
    source_locator: Any,
    collected_at: Any,
    _source_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build metadata only after parsing the live Titan fixture header."""

    match_id = str(record.get("match_id") or "").strip()
    if not match_id:
        raise ValueError("competition evidence requires an archived match_id")
    key = str(competition_key or "").strip().casefold()
    if not COMPETITION_KEY_PATTERN.fullmatch(key):
        raise ValueError(
            "competition_key must use lowercase letters, digits, and underscores"
        )
    label = unicodedata.normalize("NFKC", str(competition_label or "")).strip()
    if not label or len(label) > 32 or not COMPETITION_CJK_PATTERN.search(label):
        raise ValueError("competition_label must be a concise Chinese label")
    source_id = str(competition_id or "").strip()
    if not source_id.isdigit() or int(source_id) <= 0:
        raise ValueError("competition_id must be a positive Titan competition id")
    registry_entry = COMPETITION_EVIDENCE_REGISTRY.get(key)
    if not isinstance(registry_entry, dict):
        raise ValueError(
            "competition_key is not registered for source-verified display"
        )
    if label != registry_entry.get("label") or source_id != registry_entry.get(
        "source_id"
    ):
        raise ValueError(
            "competition key, Chinese label, and Titan id do not match the registry"
        )

    source_url = str(verification_source or "").strip()
    source_match = COMPETITION_SOURCE_PATTERN.fullmatch(source_url)
    if source_match is None or source_match.group("match_id") != match_id:
        raise ValueError(
            "competition verification source must be the matching Titan analysis page"
        )
    if source_url != str(record.get("source_url") or "").strip():
        raise ValueError(
            "competition verification source must match the archived source_url"
        )
    snapshot = (
        deepcopy(_source_snapshot)
        if isinstance(_source_snapshot, dict)
        else fetch_titan_competition_snapshot(record)
    )
    if set(snapshot) != {
        "source_url",
        "response_url",
        "page_sha256",
        "etag",
        "last_modified",
        "collected_at",
        "header",
    }:
        raise ValueError("Titan competition source snapshot shape is invalid")
    header = snapshot.get("header")
    if not isinstance(header, dict) or set(header) != {
        "home_team",
        "away_team",
        "competition_label",
        "competition_id",
        "competition_locator",
    }:
        raise ValueError("Titan competition header snapshot shape is invalid")
    if (
        snapshot.get("source_url") != source_url
        or snapshot.get("response_url") != source_url
    ):
        raise ValueError(
            "Titan competition snapshot URL does not match the archived source"
        )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(snapshot.get("page_sha256") or "")):
        raise ValueError("Titan competition snapshot page hash is invalid")
    if _source_team_name(header.get("home_team")) != _source_team_name(
        record.get("home_team")
    ) or _source_team_name(header.get("away_team")) != _source_team_name(
        record.get("away_team")
    ):
        raise ValueError("Titan competition snapshot teams do not match the fixture")
    if (
        str(header.get("competition_label") or "") != label
        or str(header.get("competition_id") or "") != source_id
    ):
        raise ValueError(
            "Titan competition snapshot does not support the supplied competition"
        )

    locator = str(source_locator or "").strip()
    if locator != str(header.get("competition_locator") or ""):
        raise ValueError(
            "competition source locator does not match the Titan header snapshot"
        )
    if not COMPETITION_LOCATOR_PATTERN.match(locator):
        raise ValueError("competition source locator must be a Titan competition link")
    if re.search(rf"(?<!\d){re.escape(source_id)}(?!\d)", locator) is None:
        raise ValueError(
            "competition source locator must contain the Titan competition id"
        )
    if (
        re.fullmatch(str(registry_entry["locator_pattern"]), locator, re.IGNORECASE)
        is None
    ):
        raise ValueError(
            "competition source locator does not match the registered Titan link"
        )

    asserted_collected = parse_aware_datetime(
        str(collected_at or ""), "competition evidence collected_at"
    )
    captured = parse_aware_datetime(
        str(snapshot.get("collected_at") or ""),
        "Titan competition snapshot collected_at",
    )
    if abs((asserted_collected - captured).total_seconds()) > 300:
        raise ValueError(
            "competition collected_at does not match the live page capture"
        )
    if captured > utc_now().astimezone(captured.tzinfo) + timedelta(minutes=5):
        raise ValueError("competition evidence collected_at cannot be in the future")
    kickoff = parse_aware_datetime(
        str(record.get("kickoff") or ""), "competition evidence fixture kickoff"
    )
    evidence = {
        "schema_version": COMPETITION_EVIDENCE_SCHEMA_VERSION,
        "status": "verified_source_metadata",
        "fixture": {
            "match_id": match_id,
            "home_team": str(record.get("home_team") or ""),
            "away_team": str(record.get("away_team") or ""),
            "kickoff": kickoff.isoformat(),
        },
        "competition": {
            "key": key,
            "label": label,
            "source_id": source_id,
        },
        "source": {
            "url": source_url,
            "response_url": str(snapshot["response_url"]),
            "locator": locator,
            "collected_at": captured.isoformat(),
            "method": "titan_analysis_header_link",
            "page_sha256": str(snapshot["page_sha256"]),
            "etag": str(snapshot.get("etag") or ""),
            "last_modified": str(snapshot.get("last_modified") or ""),
            "header": deepcopy(header),
        },
    }
    evidence["evidence_hash"] = calculate_competition_evidence_hash(evidence)
    return evidence


def validated_competition_evidence(
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """Return verified competition metadata only when every fixture binding passes."""

    evidence = record.get("competition_evidence")
    if not isinstance(evidence, dict):
        return None
    try:
        if set(evidence) != {
            "schema_version",
            "status",
            "fixture",
            "competition",
            "source",
            "evidence_hash",
        }:
            return None
        if (
            evidence.get("schema_version") != COMPETITION_EVIDENCE_SCHEMA_VERSION
            or evidence.get("status") != "verified_source_metadata"
            or evidence.get("evidence_hash")
            != calculate_competition_evidence_hash(evidence)
        ):
            return None
        fixture = evidence.get("fixture")
        competition = evidence.get("competition")
        source = evidence.get("source")
        if (
            not isinstance(fixture, dict)
            or not isinstance(competition, dict)
            or not isinstance(source, dict)
        ):
            return None
        if set(fixture) != {"match_id", "home_team", "away_team", "kickoff"}:
            return None
        if set(competition) != {"key", "label", "source_id"}:
            return None
        if set(source) != {
            "url",
            "response_url",
            "locator",
            "collected_at",
            "method",
            "page_sha256",
            "etag",
            "last_modified",
            "header",
        }:
            return None
        if any(
            str(fixture.get(field) or "") != str(record.get(record_field) or "")
            for field, record_field in (
                ("match_id", "match_id"),
                ("home_team", "home_team"),
                ("away_team", "away_team"),
            )
        ):
            return None
        fixture_kickoff = parse_aware_datetime(
            str(fixture.get("kickoff") or ""), "competition evidence fixture kickoff"
        )
        record_kickoff = parse_aware_datetime(
            str(record.get("kickoff") or ""), "competition evidence record kickoff"
        )
        if fixture_kickoff.astimezone(timezone.utc) != record_kickoff.astimezone(
            timezone.utc
        ):
            return None
        key = str(competition.get("key") or "")
        label = str(competition.get("label") or "")
        source_id = str(competition.get("source_id") or "")
        registry_entry = COMPETITION_EVIDENCE_REGISTRY.get(key)
        if (
            not COMPETITION_KEY_PATTERN.fullmatch(key)
            or not label
            or len(label) > 32
            or not COMPETITION_CJK_PATTERN.search(label)
            or not source_id.isdigit()
            or int(source_id) <= 0
            or not isinstance(registry_entry, dict)
            or label != registry_entry.get("label")
            or source_id != registry_entry.get("source_id")
        ):
            return None
        source_url = str(source.get("url") or "")
        source_match = COMPETITION_SOURCE_PATTERN.fullmatch(source_url)
        header = source.get("header")
        if (
            source_match is None
            or source_match.group("match_id") != str(record.get("match_id") or "")
            or source_url != str(record.get("source_url") or "")
            or source.get("response_url") != source_url
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", str(source.get("page_sha256") or "")
            )
            or not isinstance(header, dict)
            or set(header)
            != {
                "home_team",
                "away_team",
                "competition_label",
                "competition_id",
                "competition_locator",
            }
            or _source_team_name(header.get("home_team"))
            != _source_team_name(record.get("home_team"))
            or _source_team_name(header.get("away_team"))
            != _source_team_name(record.get("away_team"))
            or header.get("competition_label") != label
            or str(header.get("competition_id") or "") != source_id
            or header.get("competition_locator") != source.get("locator")
            or not COMPETITION_LOCATOR_PATTERN.match(str(source.get("locator") or ""))
            or re.search(
                rf"(?<!\d){re.escape(source_id)}(?!\d)",
                str(source.get("locator") or ""),
            )
            is None
            or re.fullmatch(
                str(registry_entry["locator_pattern"]),
                str(source.get("locator") or ""),
                re.IGNORECASE,
            )
            is None
            or source.get("method") != "titan_analysis_header_link"
        ):
            return None
        collected = parse_aware_datetime(
            str(source.get("collected_at") or ""), "competition evidence collected_at"
        )
        if collected > utc_now().astimezone(collected.tzinfo) + timedelta(minutes=5):
            return None
    except (TypeError, ValueError):
        return None
    return deepcopy(evidence)


def calculate_joint_scenario_audit_hash(audit: dict[str, Any]) -> str:
    payload = deepcopy(audit)
    payload.pop("audit_hash", None)
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "joint scenario audit must contain finite canonical JSON"
        ) from exc
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def frozen_registry_model_for_record(
    record: dict[str, Any], *, role: str, league_key: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    raw_binding = record.get("forward_policy_binding")
    if raw_binding is None:
        return None
    try:
        binding = forward_policy.validate_record_binding(raw_binding)
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError("active forward policy binding is invalid") from exc
    if binding is None:
        raise ValueError("active forward policy binding is missing")
    policy = binding.get("policy_snapshot")
    lineage = policy.get("artifact_lineage") if isinstance(policy, dict) else None
    registries = lineage.get("model_registries") if isinstance(lineage, dict) else None
    role_binding = registries.get(role) if isinstance(registries, dict) else None
    registered = (
        role_binding.get("registered_models")
        if isinstance(role_binding, dict)
        else None
    )
    model = registered.get(league_key) if isinstance(registered, dict) else None
    if not isinstance(role_binding, dict) or not isinstance(model, dict):
        raise ValueError(
            f"active forward policy has no frozen {role} model for {league_key}"
        )
    return deepcopy(role_binding), deepcopy(model)


def _joint_scenario_lineage(snapshot: dict[str, Any]) -> dict[str, Any]:
    inputs = snapshot.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("joint scenario input_artifacts bindings are missing")

    def binding(name: str) -> dict[str, Any]:
        value = inputs.get(name)
        if not isinstance(value, dict):
            raise ValueError(f"joint scenario inputs.{name} binding is missing")
        return value

    registered = binding("registered_htft_model")
    score = binding("canonical_score_prediction")
    htft = binding("htft_prediction")
    market = binding("market_evidence")
    lineage = {
        "registered_htft_model_content_hash": require_sha256(
            registered.get("content_hash"),
            "joint scenario registered HT/FT model content_hash",
        ),
        "registered_htft_model_hash": require_sha256(
            registered.get("model_hash"),
            "joint scenario registered HT/FT model_hash",
        ),
        "dataset_manifest_hash": require_sha256(
            registered.get("dataset_manifest_hash"),
            "joint scenario dataset_manifest_hash",
        ),
        "canonical_score_content_hash": require_sha256(
            score.get("content_hash"),
            "joint scenario canonical score content_hash",
        ),
        "canonical_score_model_hash": require_sha256(
            score.get("model_hash"),
            "joint scenario canonical score model_hash",
        ),
        "htft_prediction_content_hash": require_sha256(
            htft.get("content_hash"),
            "joint scenario HT/FT prediction content_hash",
        ),
        "htft_prediction_hash": require_sha256(
            htft.get("prediction_hash"),
            "joint scenario HT/FT prediction_hash",
        ),
        "htft_prediction_model_hash": require_sha256(
            htft.get("model_hash"),
            "joint scenario HT/FT prediction model_hash",
        ),
        "market_evidence_content_hash": None,
    }
    market_hash = market.get("content_hash")
    if market_hash is not None:
        lineage["market_evidence_content_hash"] = require_sha256(
            market_hash,
            "joint scenario market evidence content_hash",
        )
    if lineage["registered_htft_model_hash"] != lineage["htft_prediction_model_hash"]:
        raise ValueError(
            "joint scenario registered model and HT/FT prediction lineage do not match"
        )
    return lineage


def _joint_scenario_context(
    record: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    basis = record.get("settlement_basis")
    if record.get("status") == "reviewed":
        # A reviewed record is terminal.  Its only authorized active version is
        # the immutable settlement snapshot; a later top-level audit injection
        # must never become renderable evidence.
        if not isinstance(basis, dict):
            return None, {}
        return basis.get("joint_scenario_audit"), basis
    if isinstance(basis, dict):
        # Once a match has a settlement basis, absence from that immutable
        # snapshot is authoritative.  A later top-level injection is not a
        # fallback.
        return basis.get("joint_scenario_audit"), basis
    return record.get("joint_scenario_audit"), record


def _validate_joint_market_evidence_freshness(
    snapshot: dict[str, Any], context: dict[str, Any]
) -> None:
    """Apply the active archive-stage TTL to every external joint input."""
    external_anchor = snapshot.get("external_anchor_audit")
    if not isinstance(external_anchor, dict):
        raise ValueError("joint scenario external_anchor_audit is missing")
    if external_anchor.get("enabled") is True:
        validate_market_evidence_freshness(
            context,
            external_anchor.get("captured_at"),
            "joint scenario external half-time anchor captured_at",
        )

    market_evidence = snapshot.get("market_evidence")
    if not isinstance(market_evidence, dict):
        raise ValueError("joint scenario market_evidence audit is missing")
    if market_evidence.get("provided") is True:
        validate_market_evidence_freshness(
            context,
            market_evidence.get("captured_at"),
            "joint scenario attached market evidence captured_at",
        )


def _validate_joint_scenario_record_bindings(
    record: dict[str, Any],
    context: dict[str, Any],
    audit: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    fixture = snapshot.get("fixture")
    fixture_binding = audit.get("fixture_binding")
    if not isinstance(fixture, dict) or not isinstance(fixture_binding, dict):
        raise ValueError("joint scenario fixture binding is missing")
    fixture_id = str(fixture_binding.get("fixture_id") or "").strip()
    if not fixture_id:
        raise ValueError("joint scenario fixture_id binding is required")
    artifact_fixture_ids = [
        str(fixture[field])
        for field in ("fixture_id", "match_id")
        if fixture.get(field) is not None and str(fixture.get(field)).strip()
    ]
    if not artifact_fixture_ids:
        raise ValueError("joint scenario artifact requires fixture_id or match_id")
    for artifact_id_field in ("fixture_id", "match_id"):
        artifact_id = fixture.get(artifact_id_field)
        if artifact_id is not None and str(artifact_id) != fixture_id:
            raise ValueError(
                "joint scenario artifact fixture_id does not match the archive"
            )
    record_fixture_id = record.get("match_id")
    if record_fixture_id is not None and str(record_fixture_id) != fixture_id:
        raise ValueError("joint scenario fixture_id does not match the record")

    for field in ("home_team", "away_team"):
        expected = str(fixture_binding.get(field) or "")
        if not expected or str(fixture.get(field) or "") != expected:
            raise ValueError(f"joint scenario fixture {field} binding does not match")
        record_value = record.get(field)
        if record_value is not None and str(record_value) != expected:
            raise ValueError(
                f"joint scenario fixture {field} does not match the record"
            )

    artifact_kickoff = parse_aware_datetime(
        str(fixture.get("kickoff") or ""), "joint scenario fixture.kickoff"
    )
    bound_kickoff = parse_aware_datetime(
        str(fixture_binding.get("kickoff") or ""),
        "joint scenario fixture binding kickoff",
    )
    if artifact_kickoff.astimezone(timezone.utc) != bound_kickoff.astimezone(
        timezone.utc
    ):
        raise ValueError("joint scenario fixture kickoff binding does not match")
    record_kickoff_text = record.get("kickoff", record.get("user_local_kickoff"))
    if record_kickoff_text is not None:
        record_kickoff = parse_aware_datetime(
            str(record_kickoff_text), "joint scenario record kickoff"
        )
        if record_kickoff.astimezone(timezone.utc) != artifact_kickoff.astimezone(
            timezone.utc
        ):
            raise ValueError("joint scenario fixture kickoff does not match the record")

    generated_at = parse_aware_datetime(
        str(snapshot.get("generated_at") or ""), "joint scenario generated_at"
    )
    archived_at = parse_aware_datetime(
        str(audit.get("archived_at") or ""), "joint scenario archived_at"
    )
    if generated_at >= artifact_kickoff:
        raise ValueError("joint scenario generated_at must be strictly before kickoff")
    if generated_at > archived_at:
        raise ValueError("joint scenario generated_at cannot be after archive time")
    expected_archive = context.get("version_archived_at")
    if expected_archive is None:
        expected_archive = context.get("archived_at")
    if expected_archive is None:
        expected_archive = context.get("updated_at", context.get("created_at"))
    if expected_archive is not None:
        expected_archive_time = parse_aware_datetime(
            str(expected_archive), "joint scenario record archive time"
        )
        if archived_at.astimezone(timezone.utc) != expected_archive_time.astimezone(
            timezone.utc
        ):
            raise ValueError(
                "joint scenario archived_at does not match the active version"
            )
    _validate_joint_market_evidence_freshness(snapshot, context)


def _validate_joint_scenario_input_artifacts(
    context: dict[str, Any],
    snapshot: dict[str, Any],
    lineage: dict[str, Any],
) -> dict[str, str]:
    provenance = context.get("score_model_provenance")
    if not isinstance(provenance, dict):
        raise ValueError("joint scenario requires its bound canonical score artifact")
    raw_score = provenance.get("raw_snapshot")
    if not isinstance(raw_score, dict):
        raise ValueError("joint scenario bound score snapshot is missing")
    score_content_hash = joint_scenario_model.content_hash(raw_score)
    score_model_hash = require_sha256(
        provenance.get("model_hash"), "joint scenario bound score model_hash"
    )
    if (
        score_content_hash != lineage["canonical_score_content_hash"]
        or score_model_hash != lineage["canonical_score_model_hash"]
    ):
        raise ValueError(
            "joint scenario canonical score input hash or model lineage does not match"
        )

    candidate_audits = context.get("candidate_audits", [])
    if not isinstance(candidate_audits, list):
        raise ValueError("joint scenario bound HT/FT audit list is missing")
    htft_audits = [
        item
        for item in candidate_audits
        if isinstance(item, dict) and item.get("market") == "htft"
    ]
    if len(htft_audits) != 1:
        raise ValueError("joint scenario requires exactly one bound HT/FT audit")
    model = htft_audits[0].get("model")
    if not isinstance(model, dict):
        raise ValueError("joint scenario bound HT/FT audit model is missing")
    htft_content_hash = require_sha256(
        model.get("content_hash"), "joint scenario bound HT/FT content_hash"
    )
    htft_prediction_hash = require_sha256(
        model.get("prediction_hash"), "joint scenario bound HT/FT prediction_hash"
    )
    registered_model_hash = require_sha256(
        model.get("model_hash"), "joint scenario bound registered model_hash"
    )
    dataset_manifest_hash = require_sha256(
        model.get("dataset_manifest_hash"),
        "joint scenario bound dataset_manifest_hash",
    )
    if (
        htft_content_hash != lineage["htft_prediction_content_hash"]
        or registered_model_hash != lineage["htft_prediction_model_hash"]
        or registered_model_hash != lineage["registered_htft_model_hash"]
        or htft_prediction_hash != lineage["htft_prediction_hash"]
        or dataset_manifest_hash != lineage["dataset_manifest_hash"]
    ):
        raise ValueError(
            "joint scenario HT/FT input hash or model lineage does not match"
        )
    return {
        "canonical_score_content_hash": score_content_hash,
        "htft_prediction_content_hash": htft_content_hash,
        "htft_prediction_hash": htft_prediction_hash,
        "registered_model_hash": registered_model_hash,
        "dataset_manifest_hash": dataset_manifest_hash,
    }


def _joint_scenario_active_version_binding(
    context: dict[str, Any],
    context_lineage: dict[str, str],
) -> dict[str, str]:
    stage = str(context.get("analysis_stage") or "").strip()
    if stage not in {"initial", "lineup-check"}:
        raise ValueError(
            "joint scenario active version requires a valid analysis_stage"
        )

    fixture_id_value = context.get("fixture_id")
    match_id_value = context.get("match_id")
    fixture_id = str(
        fixture_id_value if fixture_id_value is not None else match_id_value or ""
    ).strip()
    if not fixture_id:
        raise ValueError("joint scenario active version requires fixture_id")
    if (
        fixture_id_value is not None
        and match_id_value is not None
        and str(fixture_id_value) != str(match_id_value)
    ):
        raise ValueError("joint scenario active version fixture identifiers disagree")

    archived_value = context.get("version_archived_at")
    if archived_value is None:
        archived_value = context.get("archived_at")
    if archived_value is None:
        archived_value = context.get("updated_at", context.get("created_at"))
    archived_at = parse_aware_datetime(
        str(archived_value or ""), "joint scenario active version archived_at"
    ).astimezone(timezone.utc)

    return {
        "analysis_stage": stage,
        "version_archived_at": archived_at.isoformat(),
        "fixture_id": fixture_id,
        "canonical_score_content_hash": context_lineage["canonical_score_content_hash"],
        "htft_prediction_content_hash": context_lineage["htft_prediction_content_hash"],
        "htft_prediction_hash": context_lineage["htft_prediction_hash"],
        "registered_model_hash": context_lineage["registered_model_hash"],
        "dataset_manifest_hash": context_lineage["dataset_manifest_hash"],
    }


def _validate_joint_scenario_active_version_binding(
    context: dict[str, Any],
    audit: dict[str, Any],
    context_lineage: dict[str, str],
) -> None:
    binding = audit.get("active_version_binding")
    if not isinstance(binding, dict):
        raise ValueError("joint scenario active_version_binding is missing")
    expected = _joint_scenario_active_version_binding(context, context_lineage)
    if any(binding.get(field) != value for field, value in expected.items()):
        raise ValueError("joint scenario active_version_binding does not match context")

    fixture_binding = audit.get("fixture_binding")
    if (
        not isinstance(fixture_binding, dict)
        or fixture_binding.get("fixture_id") != expected["fixture_id"]
    ):
        raise ValueError("joint scenario active version fixture binding disagrees")
    archived_at = parse_aware_datetime(
        str(audit.get("archived_at") or ""), "joint scenario archived_at"
    ).astimezone(timezone.utc)
    if archived_at.isoformat() != expected["version_archived_at"]:
        raise ValueError("joint scenario active version archive time disagrees")


def validated_joint_scenario_audit(
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the immutable validated raw joint artifact, or ``None``.

    Consumers must never reconstruct a display fallback from legacy exact-score,
    HT/FT, prose, or terminal-result fields when this returns ``None``.
    """

    if not isinstance(record, dict):
        return None
    raw_audit, context = _joint_scenario_context(record)
    if not isinstance(raw_audit, dict):
        return None
    try:
        if (
            raw_audit.get("schema_version") != JOINT_SCENARIO_AUDIT_SCHEMA_VERSION
            or raw_audit.get("status") != "validated_diagnostic"
            or raw_audit.get("formal_eligible") is not False
        ):
            return None
        if raw_audit.get("audit_hash") != calculate_joint_scenario_audit_hash(
            raw_audit
        ):
            return None
        snapshot = raw_audit.get("snapshot")
        if not isinstance(snapshot, dict):
            return None
        joint_scenario_model.validate_prediction(snapshot)
        if (
            raw_audit.get("artifact_type") != snapshot.get("artifact_type")
            or raw_audit.get("model_version") != snapshot.get("model_version")
            or raw_audit.get("prediction_hash") != snapshot.get("prediction_hash")
            or raw_audit.get("snapshot_hash")
            != joint_scenario_model.content_hash(snapshot)
            or raw_audit.get("input_artifacts") != snapshot.get("inputs")
            or raw_audit.get("joint_top_two") != snapshot.get("joint_top_two")
            or raw_audit.get("derived") != snapshot.get("derived")
        ):
            return None
        require_sha256(
            raw_audit.get("artifact_sha256"),
            "joint scenario artifact_sha256",
        )
        require_sha256(raw_audit.get("snapshot_hash"), "joint scenario snapshot_hash")
        require_sha256(
            raw_audit.get("prediction_hash"), "joint scenario prediction_hash"
        )
        lineage = _joint_scenario_lineage(snapshot)
        if raw_audit.get("model_lineage") != lineage:
            return None
        _validate_joint_scenario_record_bindings(record, context, raw_audit, snapshot)
        context_lineage = _validate_joint_scenario_input_artifacts(
            context, snapshot, lineage
        )
        _validate_joint_scenario_active_version_binding(
            context, raw_audit, context_lineage
        )
    except (
        joint_scenario_model.JointScenarioError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None
    return deepcopy(snapshot)


def load_joint_scenario_audit(
    args: argparse.Namespace,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    supplied = str(getattr(args, "joint_scenario_file", "") or "").strip()
    if not supplied:
        return None
    path, raw, snapshot = load_observation_json(args, supplied, "joint scenario file")
    try:
        joint_scenario_model.validate_prediction(snapshot)
    except joint_scenario_model.JointScenarioError as exc:
        raise ValueError(f"invalid joint scenario artifact: {exc}") from exc

    fixture = snapshot.get("fixture")
    if not isinstance(fixture, dict):
        raise ValueError("joint scenario fixture is missing")
    fixture_id = str(record.get("match_id") or "").strip()
    if not fixture_id:
        raise ValueError("joint scenario archival requires a fixture_id")
    artifact_fixture_ids = [
        str(fixture[field])
        for field in ("fixture_id", "match_id")
        if fixture.get(field) is not None and str(fixture.get(field)).strip()
    ]
    if not artifact_fixture_ids:
        raise ValueError("joint scenario artifact requires fixture_id or match_id")
    for artifact_id_field in ("fixture_id", "match_id"):
        artifact_id = fixture.get(artifact_id_field)
        if artifact_id is not None and str(artifact_id) != fixture_id:
            raise ValueError(
                "joint scenario artifact fixture_id does not match the record"
            )
    for field in ("home_team", "away_team"):
        if str(fixture.get(field) or "") != str(record.get(field) or ""):
            raise ValueError(f"joint scenario fixture {field} must match the record")
    fixture_kickoff = parse_aware_datetime(
        str(fixture.get("kickoff") or ""), "joint scenario fixture.kickoff"
    )
    record_kickoff = parse_aware_datetime(
        str(record.get("kickoff") or ""), "joint scenario record kickoff"
    )
    if fixture_kickoff.astimezone(timezone.utc) != record_kickoff.astimezone(
        timezone.utc
    ):
        raise ValueError("joint scenario fixture kickoff must match the record")
    generated_at = parse_aware_datetime(
        str(snapshot.get("generated_at") or ""), "joint scenario generated_at"
    )
    archived_at = parse_aware_datetime(
        str(record.get("updated_at") or ""), "joint scenario record updated_at"
    )
    if generated_at >= record_kickoff:
        raise ValueError("joint scenario generated_at must be strictly before kickoff")
    if generated_at > archived_at:
        raise ValueError("joint scenario generated_at cannot be after archive time")
    _validate_joint_market_evidence_freshness(snapshot, record)

    lineage = _joint_scenario_lineage(snapshot)
    context_lineage = _validate_joint_scenario_input_artifacts(
        record, snapshot, lineage
    )
    active_version_binding = _joint_scenario_active_version_binding(
        record, context_lineage
    )
    audit: dict[str, Any] = {
        "schema_version": JOINT_SCENARIO_AUDIT_SCHEMA_VERSION,
        "status": "validated_diagnostic",
        "formal_eligible": False,
        "fixture_binding": {
            "fixture_id": fixture_id,
            "home_team": str(record.get("home_team") or ""),
            "away_team": str(record.get("away_team") or ""),
            "kickoff": fixture_kickoff.astimezone(timezone.utc).isoformat(),
        },
        "archived_at": archived_at.astimezone(timezone.utc).isoformat(),
        "artifact_type": snapshot.get("artifact_type"),
        "model_version": snapshot.get("model_version"),
        "prediction_hash": snapshot.get("prediction_hash"),
        "artifact_filename": path.name,
        "artifact_sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        "snapshot_hash": joint_scenario_model.content_hash(snapshot),
        "input_artifacts": deepcopy(snapshot.get("inputs")),
        "model_lineage": lineage,
        "active_version_binding": active_version_binding,
        "joint_top_two": deepcopy(snapshot.get("joint_top_two")),
        "derived": deepcopy(snapshot.get("derived")),
        "snapshot": deepcopy(snapshot),
    }
    audit["audit_hash"] = calculate_joint_scenario_audit_hash(audit)
    probe = deepcopy(record)
    probe["joint_scenario_audit"] = audit
    if validated_joint_scenario_audit(probe) is None:
        raise ValueError("joint scenario archive wrapper failed immutable validation")
    return audit


def validate_htft_matrix(value: Any, label: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(HTFT_OUTCOMES):
        raise ValueError(f"{label} must contain exactly HH through AA")
    matrix: dict[str, float] = {}
    for outcome in HTFT_OUTCOMES:
        probability = value.get(outcome)
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not math.isfinite(float(probability))
            or not 0.0 <= float(probability) <= 1.0
        ):
            raise ValueError(f"{label}.{outcome} must be finite and between 0 and 1")
        matrix[outcome] = float(probability)
    if abs(math.fsum(matrix.values()) - 1.0) > PROBABILITY_AUDIT_TOLERANCE:
        raise ValueError(f"{label} probabilities must sum to 1")
    return matrix


def observation_gate(
    name: str,
    passed: bool,
    reasons: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    normalized_reasons = [
        str(item).strip() for item in reasons or [] if str(item).strip()
    ]
    return {
        "gate": name,
        "passed": bool(passed),
        "reasons": [] if passed else normalized_reasons,
    }


def build_htft_scenario_gates(
    scenario: dict[str, Any],
    ranker: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_failures = scenario.get("failed_thresholds", [])
    if not isinstance(raw_failures, list):
        raise ValueError("HT/FT observation failed_thresholds must be a list")
    failures = [str(item) for item in raw_failures]

    def matching(*needles: str) -> list[str]:
        return [
            failure
            for failure in failures
            if any(needle.casefold() in failure.casefold() for needle in needles)
        ]

    ev = scenario.get("ev")
    edge = scenario.get("edge_pp")
    league_evidence = ranker.get("league_gate_evidence")
    market_policy = ranker.get("market_policy")
    complete_reasons = matching("complete current 9-way")
    provenance_reasons = matching("odds provenance")
    depth_reasons = matching("firm count")
    quality_reasons = matching("data quality")
    stability_reasons = matching("scenario stability")
    coherence_reasons = matching("scenario coherence")
    policy_reasons = matching("market policy")
    ev_reasons = [
        failure
        for failure in failures
        if failure.casefold().startswith("ev ")
        or "current odds unavailable" in failure.casefold()
    ]
    edge_reasons = [
        failure
        for failure in failures
        if failure.casefold().startswith("edge ")
        or "no-vig market probability" in failure.casefold()
    ]
    gates = {
        "complete_current_market": observation_gate(
            "complete_current_market",
            not complete_reasons,
            complete_reasons or ["complete current 9-way HT/FT odds unavailable"],
        ),
        "odds_provenance": observation_gate(
            "odds_provenance",
            isinstance(ranker.get("odds_context"), dict) and not provenance_reasons,
            provenance_reasons
            or ["audited pre-kickoff HT/FT odds provenance unavailable"],
        ),
        "positive_ev": observation_gate(
            "positive_ev",
            isinstance(ev, (int, float))
            and not isinstance(ev, bool)
            and float(ev) > 0.0,
            ev_reasons or ["positive current EV unavailable"],
        ),
        "positive_edge": observation_gate(
            "positive_edge",
            isinstance(edge, (int, float))
            and not isinstance(edge, bool)
            and float(edge) > 0.0,
            edge_reasons or ["positive model-versus-market edge unavailable"],
        ),
        "bookmaker_depth": observation_gate(
            "bookmaker_depth",
            not depth_reasons,
            depth_reasons or ["minimum bookmaker depth not demonstrated"],
        ),
        "data_quality": observation_gate(
            "data_quality",
            not quality_reasons,
            quality_reasons or ["medium/high data quality not demonstrated"],
        ),
        "scenario_stability": observation_gate(
            "scenario_stability",
            scenario.get("stability_gate_passed") is True,
            stability_reasons or list(scenario.get("stability_gate_failures", [])),
        ),
        "scenario_coherence": observation_gate(
            "scenario_coherence",
            scenario.get("coherence_gate_passed") is True,
            coherence_reasons or list(scenario.get("coherence_gate_failures", [])),
        ),
        "descriptive_pair_mass_threshold": observation_gate(
            "descriptive_pair_mass_threshold",
            ranker.get("pair_mass_threshold_crossed") is True,
            [
                "descriptive Top-2 pair-mass threshold not crossed; this is not a production release gate"
            ],
        ),
        "league_forward_evidence": observation_gate(
            "league_forward_evidence",
            isinstance(league_evidence, dict)
            and league_evidence.get("production_confidence_eligible") is True,
            [
                "league gate evidence is not forward-confirmed: "
                + str(
                    league_evidence.get("status", "missing")
                    if isinstance(league_evidence, dict)
                    else "missing"
                )
            ],
        ),
        "market_policy_enabled": observation_gate(
            "market_policy_enabled",
            isinstance(market_policy, dict)
            and market_policy.get("htft_formal_enabled") is True,
            policy_reasons or ["active market policy keeps HT/FT observation-only"],
        ),
    }
    return [gates[name] for name in OBSERVATION_GATE_ORDER]


def load_htft_observation_audit(
    args: argparse.Namespace,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    model_file = str(getattr(args, "htft_observation_model_file", "") or "").strip()
    ranker_file = str(getattr(args, "htft_observation_ranker_file", "") or "").strip()
    if bool(model_file) != bool(ranker_file):
        raise ValueError(
            "HT/FT observation archival requires both --htft-observation-model-file "
            "and --htft-observation-ranker-file"
        )
    if not model_file:
        return None

    model_path, model_raw, model = load_observation_json(
        args, model_file, "HT/FT observation model file"
    )
    ranker_path, ranker_raw, ranker = load_observation_json(
        args, ranker_file, "HT/FT observation ranker file"
    )
    if model.get("artifact_type") != "soccer_htft_prediction":
        raise ValueError(
            "HT/FT observation model file must have artifact_type=soccer_htft_prediction"
        )
    if not str(model.get("model_version") or "").strip():
        raise ValueError("HT/FT observation model requires model_version")
    model_hash = require_sha256(model.get("model_hash"), "HT/FT model_hash")
    prediction_hash = require_sha256(
        model.get("prediction_hash"), "HT/FT prediction_hash"
    )
    if prediction_hash != canonical_prediction_hash(model):
        raise ValueError("HT/FT prediction_hash does not match prediction contents")
    htft = model.get("htft")
    if not isinstance(htft, dict):
        raise ValueError("HT/FT observation model file requires htft metadata")
    matrix = validate_htft_matrix(
        htft.get("code_probabilities"), "HT/FT code_probabilities"
    )
    matrix_bytes = json.dumps(
        matrix,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    matrix_hash = f"sha256:{hashlib.sha256(matrix_bytes).hexdigest()}"

    fixture = model.get("fixture")
    if not isinstance(fixture, dict):
        raise ValueError("HT/FT observation model requires fixture metadata")
    if str(fixture.get("home_team") or "") != str(record.get("home_team") or ""):
        raise ValueError("HT/FT observation fixture home_team must match the record")
    if str(fixture.get("away_team") or "") != str(record.get("away_team") or ""):
        raise ValueError("HT/FT observation fixture away_team must match the record")
    if fixture.get("unknown_team_policy") != "error":
        raise ValueError("HT/FT observation fixture requires unknown_team_policy=error")
    fixture_kickoff = parse_aware_datetime(
        str(fixture.get("kickoff") or ""), "HT/FT observation fixture.kickoff"
    )
    record_kickoff = parse_aware_datetime(
        str(record.get("kickoff") or ""), "record kickoff"
    )
    if fixture_kickoff.astimezone(timezone.utc) != record_kickoff.astimezone(
        timezone.utc
    ):
        raise ValueError("HT/FT observation fixture kickoff must match the record")
    generated_at = parse_aware_datetime(
        str(model.get("generated_at") or ""), "HT/FT observation generated_at"
    )
    if generated_at >= record_kickoff:
        raise ValueError("HT/FT observation must be generated before kickoff")
    archived_at = parse_aware_datetime(
        str(record.get("updated_at") or ""), "record updated_at"
    )
    if generated_at > archived_at:
        raise ValueError("HT/FT observation cannot be generated after it is archived")
    provenance = model.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("HT/FT observation model requires provenance metadata")
    if provenance.get("generated_before_kickoff") is not True:
        raise ValueError(
            "HT/FT observation provenance must prove generation before kickoff"
        )
    if provenance.get("strictly_before_kickoff_utc_date") is not True:
        raise ValueError(
            "HT/FT observation provenance must prove a strict training cutoff"
        )
    cutoff_text = str(provenance.get("training_cutoff_date") or "")
    try:
        training_cutoff = date.fromisoformat(cutoff_text)
    except ValueError as exc:
        raise ValueError(
            "HT/FT observation training_cutoff_date must be an ISO date"
        ) from exc
    if training_cutoff >= record_kickoff.astimezone(timezone.utc).date():
        raise ValueError("HT/FT observation training cutoff must predate kickoff")

    training = provenance.get("training")
    if not isinstance(training, dict):
        raise ValueError("HT/FT observation provenance requires training metadata")
    training_competition = str(training.get("competition_key") or "").strip().casefold()
    if not training_competition:
        raise ValueError("HT/FT observation training competition_key is required")
    require_sha256(
        training.get("source_data_hash"),
        "HT/FT observation training source_data_hash",
    )
    dataset_manifest_hash = require_sha256(
        training.get("dataset_manifest_hash"),
        "HT/FT observation training dataset_manifest_hash",
    )
    frozen_htft = frozen_registry_model_for_record(
        record, role="football_htft", league_key=training_competition
    )
    if frozen_htft is not None:
        registry_binding, registered_model = frozen_htft
        if (
            registry_binding.get("dataset_manifest_hash") != dataset_manifest_hash
            or registered_model.get("model_hash") != model_hash
        ):
            raise ValueError(
                "HT/FT observation does not match the cohort-frozen football registry"
            )
    if str(training.get("end_date") or "") != cutoff_text:
        raise ValueError(
            "HT/FT observation training end_date must equal training_cutoff_date"
        )

    input_audit = ranker.get("input_audit")
    expected_input_fields = {
        "odds",
        "market_probabilities",
        "firm_count",
        "data_quality",
        "tolerance_pp",
        "edge_threshold_pp",
        "minimum_firms",
        "exact_score_results",
        "anchor_context",
        "odds_context",
        "league_key",
        "league_evidence",
        "model_hash",
    }
    if not isinstance(input_audit, dict) or set(input_audit) != expected_input_fields:
        raise ValueError("HT/FT observation ranker requires a complete input_audit")
    if input_audit.get("league_key") != training_competition:
        raise ValueError(
            "HT/FT observation ranker league evidence does not match model training"
        )
    if input_audit.get("model_hash") != model_hash:
        raise ValueError(
            "HT/FT observation ranker model_hash does not match prediction"
        )
    odds_context = input_audit.get("odds_context")
    if odds_context is not None:
        if not isinstance(odds_context, dict):
            raise ValueError("HT/FT observation odds_context must be an object")
        validate_market_evidence_freshness(
            record,
            odds_context.get("captured_at"),
            "HT/FT observation market_collected_at",
        )
    marginal_targets = provenance.get("marginal_targets")
    if not isinstance(marginal_targets, dict):
        raise ValueError("HT/FT observation provenance requires marginal_targets")
    half_target = marginal_targets.get("half_time")
    full_target = marginal_targets.get("full_time")
    if not isinstance(half_target, dict) or not isinstance(full_target, dict):
        raise ValueError("HT/FT observation marginal_targets must bind both periods")
    if full_target.get("origin") != "model_component":
        raise ValueError(
            "HT/FT observation full-time marginal must use model_component"
        )
    anchor_context = input_audit.get("anchor_context")
    half_origin = half_target.get("origin")
    external_anchor_enabled = provenance.get("external_anchor_enabled")
    if not isinstance(external_anchor_enabled, bool):
        raise ValueError("HT/FT observation external_anchor_enabled must be boolean")
    half_external = half_origin == "external_de_vigged_anchor"
    if external_anchor_enabled != half_external:
        raise ValueError("HT/FT observation external-anchor flag is inconsistent")

    result_codes = {"home": "H", "draw": "D", "away": "A"}

    def marginal_target_probabilities(
        target: dict[str, Any], label: str
    ) -> dict[str, float]:
        supplied = target.get("probabilities")
        if not isinstance(supplied, dict) or set(supplied) != set(result_codes):
            raise ValueError(f"{label} probabilities must contain home/draw/away")
        normalized: dict[str, float] = {}
        for name, code in result_codes.items():
            value = supplied[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0.0
                or float(value) > 1.0
            ):
                raise ValueError(f"{label} {name} probability is invalid")
            normalized[code] = float(value)
        if abs(math.fsum(normalized.values()) - 1.0) > 1e-10:
            raise ValueError(f"{label} probabilities must sum to 1")
        return normalized

    declared_half_target = marginal_target_probabilities(
        half_target, "HT/FT half-time marginal target"
    )
    declared_full_target = marginal_target_probabilities(
        full_target, "HT/FT full-time marginal target"
    )
    matrix_half_target = {
        half: math.fsum(matrix[f"{half}{full}"] for full in ("H", "D", "A"))
        for half in ("H", "D", "A")
    }
    matrix_full_target = {
        full: math.fsum(matrix[f"{half}{full}"] for half in ("H", "D", "A"))
        for full in ("H", "D", "A")
    }
    if any(
        abs(declared_half_target[result] - matrix_half_target[result]) > 1e-10
        for result in ("H", "D", "A")
    ) or any(
        abs(declared_full_target[result] - matrix_full_target[result]) > 1e-10
        for result in ("H", "D", "A")
    ):
        raise ValueError("HT/FT marginal target provenance does not match model matrix")

    if half_origin == "external_de_vigged_anchor":
        if not isinstance(anchor_context, dict):
            raise ValueError(
                "HT/FT external half-time marginal requires matching anchor_context"
            )
        if (
            half_target.get("de_vigged") is not True
            or anchor_context.get("kind") != "half_time_current_market"
            or anchor_context.get("complete") is not True
            or anchor_context.get("de_vigged") is not True
            or anchor_context.get("production_pair_mass_gate_validated") is not False
            or str(anchor_context.get("source") or "").strip()
            != str(half_target.get("source") or "").strip()
        ):
            raise ValueError(
                "HT/FT ranker anchor_context does not match model marginal provenance"
            )
        model_anchor_time = parse_aware_datetime(
            str(half_target.get("captured_at") or ""),
            "HT/FT model half-time anchor captured_at",
        )
        ranker_anchor_time = parse_aware_datetime(
            str(anchor_context.get("captured_at") or ""),
            "HT/FT ranker anchor_context captured_at",
        )
        if model_anchor_time.astimezone(timezone.utc) != ranker_anchor_time.astimezone(
            timezone.utc
        ):
            raise ValueError(
                "HT/FT ranker anchor_context timing does not match model marginal provenance"
            )
        anchor_time = model_anchor_time.astimezone(timezone.utc)
        if anchor_time >= record_kickoff.astimezone(timezone.utc):
            raise ValueError(
                "HT/FT external half-time anchor captured_at must be strictly before kickoff"
            )
        if anchor_time > archived_at.astimezone(timezone.utc):
            raise ValueError(
                "HT/FT external half-time anchor captured_at cannot be after archive time"
            )
        if anchor_time > generated_at.astimezone(timezone.utc):
            raise ValueError(
                "HT/FT external half-time anchor captured_at cannot be after model generated_at"
            )
        validate_market_evidence_freshness(
            record,
            model_anchor_time,
            "HT/FT external half-time anchor captured_at",
        )
    elif half_origin == "model_component":
        if anchor_context is not None:
            raise ValueError(
                "HT/FT model-component half-time marginal cannot use anchor_context"
            )
    else:
        raise ValueError("HT/FT observation half-time marginal origin is unsupported")
    half_probabilities = {
        half: math.fsum(matrix[f"{half}{full}"] for full in ("H", "D", "A"))
        for half in ("H", "D", "A")
    }
    full_probabilities = {
        full: math.fsum(matrix[f"{half}{full}"] for half in ("H", "D", "A"))
        for full in ("H", "D", "A")
    }
    try:
        reproduced_ranker = htft_ranker.rank_htft(
            matrix,
            half_probabilities,
            full_probabilities,
            odds=input_audit["odds"] or None,
            market_probabilities=input_audit["market_probabilities"],
            firm_count=input_audit["firm_count"],
            data_quality=input_audit["data_quality"],
            tolerance_pp=input_audit["tolerance_pp"],
            edge_threshold_pp=input_audit["edge_threshold_pp"],
            minimum_firms=input_audit["minimum_firms"],
            exact_score_results=input_audit["exact_score_results"],
            anchor_context=input_audit["anchor_context"],
            odds_context=input_audit["odds_context"],
            league_key=input_audit["league_key"],
            league_evidence=input_audit["league_evidence"],
            model_hash=input_audit["model_hash"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"HT/FT observation ranker inputs are invalid: {exc}") from exc
    canonical_ranker = json.dumps(
        ranker,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    canonical_reproduced = json.dumps(
        reproduced_ranker,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if canonical_ranker != canonical_reproduced:
        raise ValueError(
            "HT/FT observation ranker does not reproduce from its audited inputs"
        )

    marginal_validation = ranker.get("marginal_validation")
    if (
        not isinstance(marginal_validation, dict)
        or marginal_validation.get("passed") is not True
    ):
        raise ValueError("HT/FT observation ranker must pass marginal validation")
    scenarios = ranker.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 2:
        raise ValueError("HT/FT observation ranker must contain exactly two scenarios")
    expected_top_two = sorted(
        HTFT_OUTCOMES,
        key=lambda outcome: (-matrix[outcome], HTFT_OUTCOMES.index(outcome)),
    )[:2]
    if [
        str(item.get("selection") or "").upper() for item in scenarios
    ] != expected_top_two:
        raise ValueError(
            "HT/FT observation scenarios must equal the matrix probability Top 2"
        )
    pair_mass = math.fsum(matrix[outcome] for outcome in expected_top_two)
    supplied_pair_mass = ranker.get("pair_probability_mass", ranker.get("pair_mass"))
    if (
        supplied_pair_mass is None
        or not math.isfinite(float(supplied_pair_mass))
        or abs(float(supplied_pair_mass) - pair_mass) > PROBABILITY_AUDIT_TOLERANCE
    ):
        raise ValueError("HT/FT observation pair mass must equal the matrix Top-2 sum")
    market_policy = ranker.get("market_policy")
    formal_count = ranker.get("formal_count")
    if (
        not isinstance(market_policy, dict)
        or market_policy.get("status") != "observation_only"
        or market_policy.get("htft_formal_enabled") is not False
        or isinstance(formal_count, bool)
        or not isinstance(formal_count, (int, float))
        or int(formal_count) != 0
    ):
        raise ValueError(
            "HT/FT observation ranker must preserve the paused market policy"
        )

    top_two: list[dict[str, Any]] = []
    for index, scenario in enumerate(scenarios, start=1):
        if not isinstance(scenario, dict) or scenario.get("status") != "observation":
            raise ValueError("HT/FT observation scenarios must have status=observation")
        selection = expected_top_two[index - 1]
        probability = scenario.get("probability")
        if (
            probability is None
            or not math.isfinite(float(probability))
            or abs(float(probability) - matrix[selection]) > PROBABILITY_AUDIT_TOLERANCE
        ):
            raise ValueError(
                f"HT/FT observation scenario {selection} must match the model matrix"
            )
        gates = build_htft_scenario_gates(scenario, ranker)
        top_two.append(
            {
                "slot": index,
                "selection": selection,
                "probability": matrix[selection],
                "odds": scenario.get("odds"),
                "market_probability": scenario.get("market_probability"),
                "edge_pp": scenario.get("edge_pp"),
                "ev": scenario.get("ev"),
                "market_source": (
                    odds_context.get("source")
                    if isinstance(odds_context, dict)
                    else None
                ),
                "market_collected_at": (
                    odds_context.get("captured_at")
                    if isinstance(odds_context, dict)
                    else None
                ),
                "conditional_stability": scenario.get("conditional_stability"),
                "state_continuity": scenario.get("state_continuity"),
                "gates": gates,
                "diagnostic_qualification_status": scenario.get(
                    "diagnostic_qualification_status"
                ),
                "diagnostic_failed_thresholds": deepcopy(
                    scenario.get("diagnostic_failed_thresholds", [])
                ),
                "failed_thresholds": deepcopy(scenario.get("failed_thresholds", [])),
            }
        )

    model_artifact_sha256 = f"sha256:{hashlib.sha256(model_raw).hexdigest()}"
    ranker_artifact_sha256 = f"sha256:{hashlib.sha256(ranker_raw).hexdigest()}"
    observation_id_payload = (
        model_artifact_sha256 + ":" + ranker_artifact_sha256
    ).encode("ascii")
    observation_id = f"sha256:{hashlib.sha256(observation_id_payload).hexdigest()}"
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "observation_id": observation_id,
        "market": "htft",
        "status": "observation_only",
        "archived_at": record.get("updated_at"),
        "counts_toward_primary_record": False,
        "monetary_scope": "none",
        "model": {
            "artifact_type": model.get("artifact_type"),
            "schema_version": model.get("schema_version"),
            "model_version": model.get("model_version"),
            "content_hash": joint_scenario_model.content_hash(model),
            "model_hash": model_hash,
            "prediction_hash": prediction_hash,
            "dataset_manifest_hash": dataset_manifest_hash,
            **(
                {
                    "registry_role": "football_htft",
                    "registry_hash": frozen_htft[0]["declared_registry_hash"],
                }
                if frozen_htft is not None
                else {}
            ),
            "artifact_sha256": model_artifact_sha256,
            "artifact_filename": model_path.name,
            "matrix_hash": matrix_hash,
            "generated_at": generated_at.isoformat(),
            "training_cutoff_date": cutoff_text,
        },
        "ranker": {
            "artifact_sha256": ranker_artifact_sha256,
            "artifact_filename": ranker_path.name,
            "selection_basis": ranker.get("selection_basis"),
            "matrix_mode": ranker.get("matrix_mode"),
            "anchor_context": deepcopy(anchor_context),
            "odds_context": deepcopy(odds_context),
            "marginal_validation": deepcopy(marginal_validation),
        },
        "matrix": matrix,
        "top_two": top_two,
        "pair_probability_mass": pair_mass,
        "pair_mass_threshold": ranker.get("pair_mass_threshold"),
        "pair_mass_threshold_crossed": ranker.get("pair_mass_threshold_crossed"),
        "pair_mass_gate_passed": ranker.get("pair_mass_gate_passed"),
        "confidence_status": ranker.get("confidence_status"),
        "league_gate_evidence": deepcopy(ranker.get("league_gate_evidence")),
        "market_policy": deepcopy(market_policy),
        "provenance": {
            "strict_forward_oos": True,
            "fixture_validated": True,
            "generated_before_kickoff": True,
            "training_cutoff_before_kickoff": True,
        },
    }


def resolve_observation_model_dir(args: argparse.Namespace, supplied: str) -> Path:
    path = Path(supplied).expanduser()
    if not path.is_absolute():
        base = (
            Path(args.base_dir).expanduser().resolve() if args.base_dir else Path.cwd()
        )
        path = (base / path).resolve()
    if not path.is_dir():
        raise ValueError(f"Corner observation model directory does not exist: {path}")
    return path


def build_corner_candidate_gates(
    candidate: dict[str, Any], ranking: dict[str, Any]
) -> list[dict[str, Any]]:
    raw_failures = candidate.get("failed_thresholds", [])
    if not isinstance(raw_failures, list):
        raise ValueError("Corner observation failed_thresholds must be a list")
    failures = [str(item) for item in raw_failures]

    def matching(*needles: str) -> list[str]:
        return [
            failure
            for failure in failures
            if any(needle.casefold() in failure.casefold() for needle in needles)
        ]

    ev = candidate.get("ev")
    edge = candidate.get("edge_pp")
    firm_count = candidate.get("firm_count")
    data_quality = candidate.get("data_quality")
    signal = candidate.get("market_signal")
    corroboration = candidate.get("adverse_signal_corroboration")
    upstream_policy = ranking.get("upstream_policy")
    deployment_status = (
        upstream_policy.get("deployment_status")
        if isinstance(upstream_policy, dict)
        else None
    )
    adverse = signal in {"against", "conflicting"}
    adverse_passed = not adverse or (
        isinstance(ev, (int, float))
        and not isinstance(ev, bool)
        and float(ev) >= corner_ranker.ADVERSE_MINIMUM_EV
        and isinstance(edge, (int, float))
        and not isinstance(edge, bool)
        and float(edge) >= corner_ranker.ADVERSE_MINIMUM_EDGE_PP
        and isinstance(firm_count, int)
        and not isinstance(firm_count, bool)
        and firm_count >= corner_ranker.ADVERSE_MINIMUM_FIRMS
        and isinstance(corroboration, dict)
        and corroboration.get("qualified") is True
    )
    gates = {
        "complete_current_market": observation_gate(
            "complete_current_market",
            candidate.get("market_complete") is True
            and candidate.get("odds") is not None,
            matching("complete current", "executable odds")
            or ["complete executable two-way corner market unavailable"],
        ),
        "odds_provenance": observation_gate(
            "odds_provenance",
            bool(str(candidate.get("market_source") or "").strip())
            and bool(str(candidate.get("market_collected_at") or "").strip()),
            ["audited pre-kickoff corner odds provenance unavailable"],
        ),
        "positive_ev": observation_gate(
            "positive_ev",
            isinstance(ev, (int, float))
            and not isinstance(ev, bool)
            and float(ev) > 0.0,
            matching("EV ", "EV unavailable") or ["positive current EV unavailable"],
        ),
        "positive_edge": observation_gate(
            "positive_edge",
            isinstance(edge, (int, float))
            and not isinstance(edge, bool)
            and float(edge) > 0.0,
            matching("edge ", "edge unavailable")
            or ["positive model-versus-market edge unavailable"],
        ),
        "bookmaker_depth": observation_gate(
            "bookmaker_depth",
            isinstance(firm_count, int)
            and not isinstance(firm_count, bool)
            and firm_count >= corner_ranker.MINIMUM_FIRMS,
            matching("firm count") or ["minimum bookmaker depth not demonstrated"],
        ),
        "data_quality": observation_gate(
            "data_quality",
            data_quality in {"medium", "high"},
            matching("data quality") or ["medium/high data quality not demonstrated"],
        ),
        "corner_profile_evidence": observation_gate(
            "corner_profile_evidence",
            candidate.get("corner_profile_evidence_qualified") is True,
            matching("corner-profile", "corner rates", "components")
            or ["independent corner-profile evidence is not qualified"],
        ),
        "market_signal_classified": observation_gate(
            "market_signal_classified",
            signal in {"aligned", "neutral", "against", "conflicting"},
            matching("market signal") or ["current market signal is unknown"],
        ),
        "adverse_signal_gate": observation_gate(
            "adverse_signal_gate",
            adverse_passed,
            matching("adverse-signal", "adverse market signal")
            or ["adverse-signal corroboration gate was not cleared"],
        ),
        "registered_model_input": observation_gate(
            "registered_model_input",
            not matching("registered model input is observation-only"),
            matching("registered model input is observation-only")
            or ["registered model input is not production eligible"],
        ),
        "deployment_candidate": observation_gate(
            "deployment_candidate",
            deployment_status == "candidate",
            matching("deployment status is shadow")
            or [
                f"registered corner deployment status is {deployment_status or 'missing'}"
            ],
        ),
        "upstream_formal_policy": observation_gate(
            "upstream_formal_policy",
            candidate.get("upstream_formal_eligible") is True,
            matching("registered prediction formal_corner")
            or ["registered corner policy remains observation-only"],
        ),
    }
    return [gates[name] for name in CORNER_OBSERVATION_GATE_ORDER]


def corner_observation_probabilities(value: Any, *, label: str) -> dict[str, float]:
    states = tuple(corner_ranker.SETTLEMENT_STATES)
    if not isinstance(value, dict) or set(value) != set(states):
        raise ValueError(f"{label} must contain exactly the five settlement states")
    probabilities: dict[str, float] = {}
    for state in states:
        probability = value.get(state)
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not math.isfinite(float(probability))
            or not 0.0 <= float(probability) <= 1.0
        ):
            raise ValueError(f"{label}.{state} must be finite and within [0,1]")
        probabilities[state] = float(probability)
    if abs(math.fsum(probabilities.values()) - 1.0) > 1e-9:
        raise ValueError(f"{label} must sum to one")
    return probabilities


def calculate_corner_observation_audit_hash(audit: dict[str, Any]) -> str:
    payload = deepcopy(audit)
    payload.pop("audit_hash", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def load_corner_observation_audit(
    args: argparse.Namespace,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    model_dir_text = str(
        getattr(args, "corner_observation_model_dir", "") or ""
    ).strip()
    prediction_file = str(
        getattr(args, "corner_observation_prediction_file", "") or ""
    ).strip()
    ranker_file = str(getattr(args, "corner_observation_ranker_file", "") or "").strip()
    supplied_count = sum(
        bool(value) for value in (model_dir_text, prediction_file, ranker_file)
    )
    if supplied_count not in {0, 3}:
        raise ValueError(
            "Corner observation archival requires all three of "
            "--corner-observation-model-dir, --corner-observation-prediction-file, "
            "and --corner-observation-ranker-file"
        )
    if supplied_count == 0:
        return None

    model_dir = resolve_observation_model_dir(args, model_dir_text)
    prediction_path, prediction_raw, prediction = load_observation_json(
        args, prediction_file, "Corner observation prediction file"
    )
    ranker_path, ranker_raw, ranking = load_observation_json(
        args, ranker_file, "Corner observation ranker file"
    )
    try:
        corner_ranker.validate_ranking(
            ranking,
            prediction,
            model_dir=model_dir,
        )
    except corner_ranker.CornerRankerError as exc:
        raise ValueError(f"Corner observation ranking is invalid: {exc}") from exc

    prediction_hash = require_sha256(
        prediction.get("prediction_hash"), "Corner observation prediction_hash"
    )
    ranking_hash = require_sha256(
        ranking.get("ranking_hash"), "Corner observation ranking_hash"
    )
    binding = prediction.get("registry_binding")
    ranking_binding = ranking.get("prediction_binding")
    if not isinstance(binding, dict) or not isinstance(ranking_binding, dict):
        raise ValueError("Corner observation registry lineage is missing")
    if ranking_binding.get("prediction_hash") != prediction_hash:
        raise ValueError("Corner observation ranking does not bind the prediction hash")
    for field in (
        "registry_hash",
        "league_key",
        "dataset_hash",
        "model_hash",
        "evaluation_hash",
        "backtest_hash",
        "lineage_hash",
        "training_cutoff",
    ):
        if ranking_binding.get(field) != binding.get(field):
            raise ValueError(
                f"Corner observation ranking {field} does not match the prediction"
            )
    league_key = str(binding.get("league_key") or "").strip().casefold()
    if league_key != str(record.get("league_key") or "").strip().casefold():
        raise ValueError("Corner observation fixture league_key must match the record")
    for field in (
        "registry_hash",
        "dataset_hash",
        "model_hash",
        "evaluation_hash",
        "backtest_hash",
        "lineage_hash",
    ):
        require_sha256(binding.get(field), f"Corner observation {field}")
    frozen_corner = frozen_registry_model_for_record(
        record, role="corner", league_key=league_key
    )
    if frozen_corner is not None:
        registry_binding, registered_model = frozen_corner
        if (
            binding.get("registry_hash")
            != registry_binding.get("declared_registry_hash")
            or binding.get("dataset_hash") != registered_model.get("dataset_hash")
            or binding.get("model_hash") != registered_model.get("model_hash")
        ):
            raise ValueError(
                "Corner observation does not match the cohort-frozen corner registry"
            )

    prediction_fixture = prediction.get("fixture")
    ranking_fixture = ranking.get("fixture")
    if (
        not isinstance(prediction_fixture, dict)
        or ranking_fixture != prediction_fixture
    ):
        raise ValueError(
            "Corner observation prediction and ranking fixtures must match"
        )
    for field in ("home_team", "away_team"):
        if str(prediction_fixture.get(field) or "") != str(record.get(field) or ""):
            raise ValueError(
                f"Corner observation fixture {field} must match the record"
            )
    prediction_kickoff = parse_aware_datetime(
        str(prediction_fixture.get("kickoff") or ""),
        "Corner observation fixture.kickoff",
    )
    record_kickoff = parse_aware_datetime(
        str(record.get("kickoff") or ""), "record kickoff"
    )
    if prediction_kickoff != record_kickoff:
        raise ValueError("Corner observation fixture kickoff must match the record")
    prediction_time = parse_aware_datetime(
        str(prediction.get("generated_at") or ""),
        "Corner observation prediction.generated_at",
    )
    ranking_time = parse_aware_datetime(
        str(ranking.get("generated_at") or ""),
        "Corner observation ranking.generated_at",
    )
    archived_at = parse_aware_datetime(
        str(record.get("updated_at") or ""), "record updated_at"
    )
    if prediction_time >= record_kickoff or ranking_time >= record_kickoff:
        raise ValueError(
            "Corner observation artifacts must be generated before kickoff"
        )
    if prediction_time > ranking_time:
        raise ValueError("Corner observation ranking cannot predate its prediction")
    if prediction_time > archived_at or ranking_time > archived_at:
        raise ValueError("Corner observation artifacts cannot postdate the archive")
    try:
        training_cutoff = date.fromisoformat(str(binding.get("training_cutoff") or ""))
    except ValueError as exc:
        raise ValueError(
            "Corner observation training_cutoff must be an ISO date"
        ) from exc
    if training_cutoff >= record_kickoff.date():
        raise ValueError("Corner observation training cutoff must predate kickoff")

    if (
        ranking.get("formal_count") != 0
        or ranking.get("primary") is not None
        or ranking.get("market_policy", {}).get("status") != "observation_only"
    ):
        raise ValueError("Corner observation ranking must contain no formal picks")
    raw_candidates = ranking.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("Corner observation ranking contains no candidates")
    lineage = {
        field: deepcopy(ranking_binding[field])
        for field in (
            "prediction_hash",
            "registry_hash",
            "league_key",
            "dataset_hash",
            "model_hash",
            "evaluation_hash",
            "backtest_hash",
            "lineage_hash",
            "training_cutoff",
        )
    }
    if frozen_corner is not None:
        lineage["registry_role"] = "corner"
    candidates: list[dict[str, Any]] = []
    for index, candidate in enumerate(raw_candidates, start=1):
        if not isinstance(candidate, dict):
            raise ValueError("Corner observation candidate must be an object")
        market = candidate.get("market")
        side = candidate.get("side")
        if (
            market not in corner_ranker.MARKETS
            or side not in corner_ranker.MARKET_SIDES[market]
        ):
            raise ValueError("Corner observation candidate market or side is invalid")
        line = candidate.get("line")
        if (
            isinstance(line, bool)
            or not isinstance(line, (int, float))
            or not math.isfinite(float(line))
        ):
            raise ValueError("Corner observation candidate line is invalid")
        if (
            candidate.get("status") != "observation"
            or candidate.get("formal_eligible") is not False
            or candidate.get("role") != "observation"
        ):
            raise ValueError(
                "Corner observation candidates must remain observation-only"
            )
        probabilities = corner_observation_probabilities(
            candidate.get("settlement_probabilities"),
            label=f"Corner observation candidate {index} probabilities",
        )
        for field in ("ev", "edge_pp"):
            value = candidate.get(field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"Corner observation candidate {field} is invalid")
        candidate_id_payload = (
            f"{ranking_hash}:{index}:{market}:{side}:{float(line):.12g}"
        ).encode("utf-8")
        candidates.append(
            {
                "candidate_id": "sha256:"
                + hashlib.sha256(candidate_id_payload).hexdigest(),
                "rank": int(candidate.get("rank", index)),
                "market": market,
                "side": side,
                "line": float(line),
                "snapshot_line": candidate.get("snapshot_line"),
                "settlement_probabilities": probabilities,
                "probability": candidate.get("probability"),
                "odds": candidate.get("odds"),
                "odds_format": candidate.get("odds_format"),
                "decimal_odds": candidate.get("decimal_odds"),
                "market_probability": candidate.get("market_probability"),
                "ev": candidate.get("ev"),
                "edge_pp": candidate.get("edge_pp"),
                "settlement_return_variance": candidate.get(
                    "settlement_return_variance"
                ),
                "firm_count": candidate.get("firm_count"),
                "market_complete": candidate.get("market_complete"),
                "market_source": candidate.get("market_source"),
                "market_collected_at": candidate.get("market_collected_at"),
                "price_basis": candidate.get("price_basis"),
                "market_signal": candidate.get("market_signal"),
                "data_quality": candidate.get("data_quality"),
                "gates": build_corner_candidate_gates(candidate, ranking),
                "diagnostic_qualification_status": candidate.get(
                    "diagnostic_qualification_status"
                ),
                "diagnostic_failed_thresholds": deepcopy(
                    candidate.get("diagnostic_failed_thresholds", [])
                ),
                "policy_failed_thresholds": deepcopy(
                    candidate.get("policy_failed_thresholds", [])
                ),
                "failed_thresholds": deepcopy(candidate.get("failed_thresholds", [])),
                "upstream_formal_flag": candidate.get("upstream_formal_flag"),
                "upstream_formal_eligible": False,
                "formal_eligible": False,
                "status": "observation",
                "lineage": deepcopy(lineage),
            }
        )

    best_raw = ranking.get("best_observation")
    if not isinstance(best_raw, dict):
        raise ValueError("Corner observation ranking requires best_observation")
    best_rank = best_raw.get("rank")
    best_matches = [item for item in candidates if item.get("rank") == best_rank]
    if len(best_matches) != 1:
        raise ValueError(
            "Corner observation best_observation is not a ranked candidate"
        )
    best_observation = deepcopy(best_matches[0])
    prediction_artifact_sha256 = f"sha256:{hashlib.sha256(prediction_raw).hexdigest()}"
    ranker_artifact_sha256 = f"sha256:{hashlib.sha256(ranker_raw).hexdigest()}"
    observation_id = (
        "sha256:"
        + hashlib.sha256(
            (prediction_artifact_sha256 + ":" + ranker_artifact_sha256).encode("ascii")
        ).hexdigest()
    )
    audit = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "kind": CORNER_OBSERVATION_KIND,
        "observation_id": observation_id,
        "market": "corner_markets",
        "status": "observation_only",
        "archived_at": record.get("updated_at"),
        "counts_toward_primary_record": False,
        "monetary_scope": "none",
        "fixture": {
            "league_key": league_key,
            "home_team": prediction_fixture["home_team"],
            "away_team": prediction_fixture["away_team"],
            "kickoff": prediction_kickoff.isoformat(),
        },
        "model": {
            "artifact_type": prediction.get("artifact_type"),
            "schema_version": prediction.get("schema_version"),
            "model_version": prediction.get("model_version"),
            "model_hash": require_sha256(
                prediction.get("model_hash"), "Corner observation model_hash"
            ),
            "prediction_hash": prediction_hash,
            "artifact_sha256": prediction_artifact_sha256,
            "artifact_filename": prediction_path.name,
            "generated_at": prediction_time.isoformat(),
            "training_cutoff_date": training_cutoff.isoformat(),
        },
        "ranker": {
            "artifact_type": ranking.get("artifact_type"),
            "schema_version": ranking.get("schema_version"),
            "ranker_version": ranking.get("ranker_version"),
            "ranking_hash": ranking_hash,
            "artifact_sha256": ranker_artifact_sha256,
            "artifact_filename": ranker_path.name,
            "generated_at": ranking_time.isoformat(),
            "selection_policy": deepcopy(ranking.get("selection_policy")),
        },
        "lineage": lineage,
        "best_observation": best_observation,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "market_policy": deepcopy(ranking.get("market_policy")),
        "upstream_policy": deepcopy(ranking.get("upstream_policy")),
        "provenance": {
            "strict_forward_oos": True,
            "fixture_validated": True,
            "generated_before_kickoff": True,
            "training_cutoff_before_kickoff": True,
            "registry_and_model_reopened": True,
            "ranking_reproduced": True,
        },
    }
    audit["audit_hash"] = calculate_corner_observation_audit_hash(audit)
    return audit


def calculate_candidate_evaluation_audit_hash(audit: dict[str, Any]) -> str:
    value = deepcopy(audit)
    value.pop("audit_hash", None)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "candidate evaluation audit contains non-canonical values"
        ) from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def calculate_candidate_evaluation_source_hash(payload: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "candidate evaluation source contains non-canonical values"
        ) from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _candidate_gate(
    name: str,
    category: str,
    passed: bool,
    reasons: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if category not in CANDIDATE_GATE_CATEGORIES:
        raise ValueError(f"unsupported candidate gate category: {category}")
    normalized = [str(item).strip() for item in reasons or [] if str(item).strip()]
    return {
        "gate": name,
        "category": category,
        "passed": bool(passed),
        "reasons": [] if passed else normalized,
    }


def candidate_directional_fundamental_support(
    record: dict[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any] | None:
    audit = validated_fundamental_evidence_audit(record)
    if (
        not isinstance(audit, dict)
        or audit.get("schema_version") != fundamental_evidence.EVIDENCE_SCHEMA_VERSION
    ):
        return None
    identity_hash = str(candidate.get("market_identity_hash") or "")
    selection = str(candidate.get("settlement_reference_outcome") or "")
    if not identity_hash or not selection:
        return None
    key = f"{identity_hash}:{selection}"
    support = audit.get("candidate_support")
    item = support.get(key) if isinstance(support, dict) else None
    if (
        not isinstance(item, dict)
        or item.get("support_key") != key
        or item.get("market_identity_hash") != identity_hash
        or item.get("selection") != selection
        or item.get("market_identity") != candidate.get("market_identity")
        or item.get("market") != str(candidate.get("market") or "").lower()
        or item.get("submarket") != candidate.get("submarket")
        or item.get("rule_version") != fundamental_evidence.SUPPORT_RULE_VERSION
        or item.get("minimum_sample_matches")
        != fundamental_evidence.MINIMUM_DIRECTIONAL_SAMPLE_MATCHES
    ):
        return None
    return deepcopy(item)


def formal_candidate_directional_support(
    record: dict[str, Any], candidate: Mapping[str, Any], *, market: str | None = None
) -> bool:
    """Apply v2 directional evidence without rewriting legacy local records."""

    support = candidate_directional_fundamental_support(record, candidate)
    if support is not None:
        return bool(
            support.get("directionally_supported") is True
            and support.get("formal_gate_eligible") is True
        )
    audit = validated_fundamental_evidence_audit(record)
    if isinstance(audit, dict) and audit.get("schema_version") in {
        fundamental_evidence.PREVIOUS_DIRECTIONAL_EVIDENCE_SCHEMA_VERSION,
        fundamental_evidence.EVIDENCE_SCHEMA_VERSION,
    }:
        return False
    evidence = record.get("guardrail_evidence", {})
    candidate_market = str(market or candidate.get("market") or "")
    if candidate_market in CORNER_MARKETS:
        return bool(evidence.get("corner_profile_supported"))
    return bool(
        evidence.get("chance_quality_supported")
        or (
            evidence.get("lineup_confirmed")
            and evidence.get("attack_configuration_supported")
        )
    )


def _normalize_candidate_identity(
    raw: dict[str, Any], *, legacy_read_only: bool = False
) -> tuple[dict[str, Any], str]:
    market = str(raw.get("market") or "").strip().lower()
    if market not in PRIMARY_MARKETS:
        raise ValueError(
            f"candidate evaluation market must be one of {', '.join(PRIMARY_MARKETS)}"
        )
    candidate: dict[str, Any] = {"market": market}
    if market in {"asian", "total", "corner_total", "corner_handicap"}:
        allowed = {
            "asian": {"home", "away"},
            "total": {"over", "under"},
            "corner_total": {"over", "under"},
            "corner_handicap": {"home", "away"},
        }[market]
        side = str(raw.get("side") or "").strip().lower()
        line = raw.get("line")
        if (
            side not in allowed
            or isinstance(line, bool)
            or not isinstance(line, (int, float))
        ):
            raise ValueError(f"candidate evaluation {market} side or line is invalid")
        if not math.isfinite(float(line)):
            raise ValueError(f"candidate evaluation {market} line must be finite")
        split_line(float(line))
        candidate.update({"side": side, "line": float(line)})
        identity = f"{market}:{side}:{float(line):.12g}"
    elif market == "half_time":
        submarket = str(raw.get("submarket") or "").strip().lower()
        allowed = {
            "1x2": {"home", "draw", "away"},
            "asian": {"home", "away"},
            "total": {"over", "under"},
        }
        side = str(raw.get("side") or "").strip().lower()
        if submarket not in allowed or side not in allowed[submarket]:
            raise ValueError(
                "candidate evaluation half_time submarket or side is invalid"
            )
        line = raw.get("line")
        if submarket == "1x2":
            if line is not None:
                raise ValueError(
                    "candidate evaluation half_time 1x2 cannot carry a line"
                )
        else:
            if isinstance(line, bool) or not isinstance(line, (int, float)):
                raise ValueError(
                    "candidate evaluation half_time asian/total requires a line"
                )
            if not math.isfinite(float(line)):
                raise ValueError("candidate evaluation half_time line must be finite")
            split_line(float(line))
            line = float(line)
        candidate.update({"submarket": submarket, "side": side, "line": line})
        identity = (
            f"{market}:{submarket}:{side}:{'' if line is None else f'{line:.12g}'}"
        )
    elif market == "htft":
        selection = str(raw.get("selection") or "").strip().upper()
        if selection not in HTFT_OUTCOMES:
            raise ValueError("candidate evaluation HT/FT selection is invalid")
        candidate["selection"] = selection
        identity = f"{market}:{selection}"
    elif market == "goal_range":
        parsed = parse_goal_range_selection(str(raw.get("selection") or ""))
        candidate.update(parsed)
        identity = f"{market}:{parsed['selection']}"
    else:
        side = str(raw.get("side") or "").strip().lower()
        if side not in {"yes", "no"}:
            raise ValueError("candidate evaluation BTTS side must be yes or no")
        candidate["side"] = side
        identity = f"{market}:{side}"
    if legacy_read_only:
        candidate["identity"] = identity
        return candidate, identity
    try:
        market_identity = source_evidence.canonical_market_identity(
            raw.get("market_identity"), label="candidate market_identity"
        )
    except source_evidence.SourceEvidenceError as exc:
        raise ValueError("candidate evaluation market_identity is invalid") from exc
    market_identity_hash = source_evidence.market_identity_hash(market_identity)
    if raw.get("market_identity_hash") != market_identity_hash:
        raise ValueError("candidate evaluation market_identity_hash is invalid")
    if market in {"asian", "total", "corner_total", "corner_handicap"}:
        expected_family = market
        expected_period = "full_time"
        expected_line = float(candidate["line"])
        if market in {"asian", "corner_handicap"} and candidate["side"] == "away":
            expected_line = -expected_line
        reference_outcome = str(candidate["side"])
    elif market == "half_time":
        expected_family = str(candidate["submarket"])
        expected_period = "first_half"
        expected_line = candidate["line"]
        if expected_family == "asian" and candidate["side"] == "away":
            expected_line = -float(expected_line)
        reference_outcome = {
            "home": "H",
            "draw": "D",
            "away": "A",
        }.get(str(candidate["side"]), str(candidate["side"]))
    else:
        expected_family = market
        expected_period = "full_time"
        expected_line = None
        reference_outcome = str(
            candidate.get("selection") or candidate.get("side") or ""
        )
    if (
        market_identity["family"] != expected_family
        or market_identity["period"] != expected_period
        or market_identity["line"] != expected_line
        or reference_outcome not in market_identity["price_outcomes"]
    ):
        raise ValueError(
            "candidate evaluation market_identity does not match market/period/line/selection"
        )
    if raw.get("settlement_reference_outcome") != reference_outcome:
        raise ValueError(
            "candidate evaluation settlement_reference_outcome does not match its selection"
        )
    raw_complete_odds = raw.get("complete_market_odds")
    if isinstance(raw_complete_odds, dict) and set(raw_complete_odds) != set(
        market_identity["price_outcomes"]
    ):
        raise ValueError(
            "candidate evaluation complete_market_odds do not match price_outcomes"
        )
    candidate.update(
        {
            "identity": identity,
            "market_identity": market_identity,
            "market_identity_hash": market_identity_hash,
            "settlement_reference_outcome": reference_outcome,
        }
    )
    return candidate, identity


def _candidate_pick(candidate: dict[str, Any]) -> dict[str, Any]:
    pick = deepcopy(candidate)
    if candidate["market"] == "half_time":
        pick["market"] = candidate["submarket"]
        if candidate["submarket"] == "1x2":
            aliases = {"H": "home", "D": "draw", "A": "away"}
            complete_odds = pick.get("complete_market_odds")
            if isinstance(complete_odds, dict) and set(complete_odds) == set(aliases):
                pick["complete_market_odds"] = {
                    aliases[outcome]: price for outcome, price in complete_odds.items()
                }
            complete_probabilities = pick.get("complete_market_probabilities")
            if isinstance(complete_probabilities, dict) and set(
                complete_probabilities
            ) == set(aliases):
                pick["complete_market_probabilities"] = {
                    aliases[outcome]: probability
                    for outcome, probability in complete_probabilities.items()
                }
    return pick


def _normalize_candidate_distribution(value: Any, label: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(CANDIDATE_SETTLEMENT_STATES):
        raise ValueError(f"{label} must contain exactly the five settlement states")
    normalized: dict[str, float] = {}
    for state in CANDIDATE_SETTLEMENT_STATES:
        raw = value.get(state)
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
            or not 0.0 <= float(raw) <= 1.0
        ):
            raise ValueError(f"{label}.{state} must be finite and between 0 and 1")
        normalized[state] = float(raw)
    if abs(math.fsum(normalized.values()) - 1.0) > PROBABILITY_AUDIT_TOLERANCE:
        raise ValueError(f"{label} must sum to 1")
    return normalized


def _matching_corner_candidate(
    record: dict[str, Any], candidate: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    basis = record.get("settlement_basis")
    raw_audits = (
        basis.get("candidate_audits", [])
        if isinstance(basis, dict) and "candidate_audits" in basis
        else record.get("candidate_audits", [])
    )
    for audit in raw_audits:
        if not isinstance(audit, dict) or audit.get("kind") != CORNER_OBSERVATION_KIND:
            continue
        if not validated_observation_audit(audit, record):
            continue
        for item in audit.get("candidates", []):
            if not isinstance(item, dict):
                continue
            if (
                item.get("market") == candidate["market"]
                and item.get("side") == candidate.get("side")
                and math.isclose(
                    float(item.get("line")), float(candidate.get("line")), abs_tol=1e-12
                )
            ):
                return item, audit
    return None


def _canonical_candidate_distribution(
    record: dict[str, Any], candidate: dict[str, Any]
) -> tuple[dict[str, float], dict[str, Any]]:
    market = candidate["market"]
    if market in CORNER_MARKETS:
        match = _matching_corner_candidate(record, candidate)
        if match is None:
            raise ValueError(
                f"candidate evaluation {market} requires a matching validated corner observation"
            )
        source, source_audit = match
        distribution = _normalize_candidate_distribution(
            source.get("settlement_probabilities"),
            f"candidate evaluation {market} canonical distribution",
        )
        model = source_audit.get("model")
        ranker = source_audit.get("ranker")
        if not isinstance(model, dict) or not isinstance(ranker, dict):
            raise ValueError("candidate evaluation corner model timing is missing")
        prediction_generated_at = parse_aware_datetime(
            str(model.get("generated_at") or ""),
            "candidate evaluation corner prediction generated_at",
        )
        ranking_generated_at = parse_aware_datetime(
            str(ranker.get("generated_at") or ""),
            "candidate evaluation corner ranking generated_at",
        )
        upstream_generated_at = max(
            prediction_generated_at.astimezone(timezone.utc),
            ranking_generated_at.astimezone(timezone.utc),
        )
        return distribution, {
            "source": "validated_corner_observation",
            "candidate_id": source.get("candidate_id"),
            "observation_id": source_audit.get("observation_id"),
            "prediction_hash": model.get("prediction_hash"),
            "ranking_hash": ranker.get("ranking_hash"),
            "prediction_generated_at": prediction_generated_at.isoformat(),
            "ranking_generated_at": ranking_generated_at.isoformat(),
            "upstream_generated_at": upstream_generated_at.isoformat(),
            "upstream_formal_eligible": source.get("upstream_formal_eligible") is True,
        }

    snapshot = validated_joint_scenario_audit(record)
    if not isinstance(snapshot, dict):
        raise ValueError(
            f"candidate evaluation {market} requires a validated joint scenario audit"
        )
    joint_audit = record.get("joint_scenario_audit")
    pick = _candidate_pick(candidate)
    if market == "htft":
        htft = snapshot.get("htft_marginal")
        if not isinstance(htft, dict):
            raise ValueError("candidate evaluation HT/FT marginal is missing")
        matrix = validate_htft_matrix(
            htft.get("code_probabilities"), "candidate evaluation HT/FT matrix"
        )
        probability = matrix[candidate["selection"]]
        distribution = {
            "full_win": probability,
            "half_win": 0.0,
            "push": 0.0,
            "half_loss": 0.0,
            "loss": 1.0 - probability,
        }
    else:
        matrix_key = (
            "half_time_score_marginal"
            if market == "half_time"
            else "full_time_score_marginal"
        )
        matrix_block = snapshot.get(matrix_key)
        if not isinstance(matrix_block, dict):
            raise ValueError(f"candidate evaluation {matrix_key} is missing")
        matrix = validate_probability_matrix(
            matrix_block.get("probabilities"), f"candidate evaluation {matrix_key}"
        )
        if market in {"asian", "total", "half_time"}:
            distribution = matrix_settlement_distribution(matrix, market, pick)
        else:
            probability = 0.0
            for home, row in enumerate(matrix):
                for away, cell_probability in enumerate(row):
                    result = (
                        settle_goal_range(pick, home, away)
                        if market == "goal_range"
                        else settle_btts(pick, home, away)
                    )
                    if result == "win":
                        probability += cell_probability
            distribution = {
                "full_win": probability,
                "half_win": 0.0,
                "push": 0.0,
                "half_loss": 0.0,
                "loss": 1.0 - probability,
            }
    return distribution, {
        "source": "validated_joint_scenario",
        "joint_scenario_audit_hash": (
            joint_audit.get("audit_hash") if isinstance(joint_audit, dict) else None
        ),
        "joint_prediction_hash": snapshot.get("prediction_hash"),
        "upstream_generated_at": parse_aware_datetime(
            str(snapshot.get("generated_at") or ""),
            "candidate evaluation joint scenario generated_at",
        ).isoformat(),
    }


def _evaluate_candidate(
    record: dict[str, Any],
    raw: dict[str, Any],
    observation_id: str,
    index: int,
    generated_at: datetime,
    *,
    legacy_read_only: bool = False,
) -> dict[str, Any]:
    candidate, identity = _normalize_candidate_identity(
        raw, legacy_read_only=legacy_read_only
    )
    market = candidate["market"]
    canonical_distribution, model_binding = _canonical_candidate_distribution(
        record, candidate
    )
    upstream_generated_at = parse_aware_datetime(
        str(model_binding.get("upstream_generated_at") or ""),
        f"candidate evaluation {identity} upstream generated_at",
    )
    if generated_at.astimezone(timezone.utc) < upstream_generated_at.astimezone(
        timezone.utc
    ):
        raise ValueError(
            f"candidate evaluation {identity} generated_at cannot precede its upstream model"
        )
    supplied_distribution = _normalize_candidate_distribution(
        raw.get("settlement_probabilities"),
        f"candidate evaluation {identity} settlement_probabilities",
    )
    for state in CANDIDATE_SETTLEMENT_STATES:
        if (
            abs(supplied_distribution[state] - canonical_distribution[state])
            > PROBABILITY_AUDIT_TOLERANCE
        ):
            raise ValueError(
                f"candidate evaluation {identity} {state} probability does not match the canonical model"
            )
    probability = (
        canonical_distribution["full_win"] + canonical_distribution["half_win"]
    )
    validate_probability_close(
        raw.get("probability"), probability, f"candidate evaluation {identity}"
    )
    candidate.update(
        {
            "candidate_id": "sha256:"
            + hashlib.sha256(
                f"{observation_id}:{index}:{identity}".encode("utf-8")
            ).hexdigest(),
            "source_index": index,
            "probability": probability,
            "settlement_probabilities": canonical_distribution,
            "model_binding": model_binding,
            "odds": raw.get("odds"),
            "odds_format": raw.get("odds_format"),
            "market_complete": raw.get("market_complete") is True,
            "complete_market_odds": deepcopy(raw.get("complete_market_odds")),
            "market_source": raw.get("market_source"),
            "market_collected_at": raw.get("market_collected_at"),
            "price_basis": raw.get("price_basis"),
            "firm_count": raw.get("firm_count"),
            "market_signal": str(raw.get("market_signal") or "unknown").lower(),
            "data_quality": record.get("data_quality", "unknown"),
            "counts_toward_primary_record": False,
            "monetary_scope": "none",
            "status": "observation",
        }
    )
    pick = _candidate_pick(candidate)
    gates: list[dict[str, Any]] = [
        _candidate_gate("canonical_model_binding", "integrity", True)
    ]

    price_error: str | None = None
    try:
        validate_odds_format(pick, market)
    except (TypeError, ValueError) as exc:
        price_error = str(exc)
    provenance_errors: list[str] = []
    source = str(candidate.get("market_source") or "").strip()
    if not source:
        provenance_errors.append("market_source_missing")
    if candidate.get("price_basis") not in {"consensus", "median"}:
        provenance_errors.append("price_basis_missing_or_invalid")
    collected_at = str(candidate.get("market_collected_at") or "").strip()
    if not collected_at:
        provenance_errors.append("market_collected_at_missing")
    else:
        try:
            collected_time = validate_market_evidence_freshness(
                record,
                collected_at,
                f"candidate evaluation {identity} market_collected_at",
            )
            if collected_time > generated_at.astimezone(timezone.utc):
                raise ValueError(
                    f"candidate evaluation {identity} generated_at cannot precede market_collected_at"
                )
        except (TypeError, ValueError) as exc:
            if "cannot precede market_collected_at" in str(exc):
                raise
            provenance_errors.append(str(exc))
    if price_error:
        provenance_errors.append(price_error)
    gates.append(
        _candidate_gate(
            "odds_provenance", "integrity", not provenance_errors, provenance_errors
        )
    )

    no_vig_probability: float | None = None
    complete_market_probabilities: dict[str, float] | None = None
    complete_error: str | None = None
    if candidate["market_complete"] and price_error is None:
        try:
            no_vig_probability, complete_market_probabilities = (
                calculate_complete_market_no_vig(pick, market)
            )
        except (TypeError, ValueError) as exc:
            complete_error = str(exc)
    elif not candidate["market_complete"]:
        complete_error = "market_complete_false"
    else:
        complete_error = price_error
    gates.append(
        _candidate_gate(
            "complete_current_market",
            "integrity",
            complete_error is None,
            [complete_error] if complete_error else [],
        )
    )

    calculated_ev: float | None = None
    if price_error is None:
        win_profit = audited_win_profit(pick)
        calculated_ev = (
            canonical_distribution["full_win"] * win_profit
            + canonical_distribution["half_win"] * win_profit / 2.0
            - canonical_distribution["half_loss"] / 2.0
            - canonical_distribution["loss"]
        )
    supplied_ev = raw.get("ev")
    if supplied_ev is not None:
        if calculated_ev is None or not math.isfinite(float(supplied_ev)):
            raise ValueError(f"candidate evaluation {identity} EV requires valid odds")
        if abs(float(supplied_ev) - calculated_ev) > EV_AUDIT_TOLERANCE:
            raise ValueError(f"candidate evaluation {identity} EV does not reproduce")
    gates.append(
        _candidate_gate(
            "positive_ev",
            "value",
            calculated_ev is not None and calculated_ev > 0.0,
            ["positive_current_ev_unavailable"]
            if calculated_ev is None
            else ["current_ev_not_positive"],
        )
    )

    calculated_edge: float | None = None
    edge_probability = effective_settlement_win_probability(
        canonical_distribution,
        f"candidate evaluation {identity} canonical distribution",
    )
    if no_vig_probability is not None:
        if edge_probability is not None:
            calculated_edge = (edge_probability - no_vig_probability) * 100.0
    supplied_market_probability = raw.get("market_probability")
    if supplied_market_probability is not None:
        if no_vig_probability is None or not math.isfinite(
            float(supplied_market_probability)
        ):
            raise ValueError(
                f"candidate evaluation {identity} market_probability requires a complete market"
            )
        if (
            abs(float(supplied_market_probability) - no_vig_probability)
            > PROBABILITY_AUDIT_TOLERANCE
        ):
            raise ValueError(
                f"candidate evaluation {identity} market_probability does not reproduce"
            )
    supplied_edge = raw.get("edge_pp")
    if supplied_edge is not None:
        if calculated_edge is None or not math.isfinite(float(supplied_edge)):
            raise ValueError(
                f"candidate evaluation {identity} edge requires a complete market"
            )
        if abs(float(supplied_edge) - calculated_edge) > EDGE_AUDIT_TOLERANCE_PP:
            raise ValueError(f"candidate evaluation {identity} edge does not reproduce")
    gates.append(
        _candidate_gate(
            "positive_edge",
            "value",
            calculated_edge is not None and calculated_edge > 0.0,
            ["positive_model_market_edge_unavailable"]
            if calculated_edge is None
            else ["model_market_edge_not_positive"],
        )
    )

    firm_count = candidate.get("firm_count")
    if firm_count is not None and (
        isinstance(firm_count, bool)
        or not isinstance(firm_count, (int, float))
        or not math.isfinite(float(firm_count))
        or int(float(firm_count)) != float(firm_count)
        or int(float(firm_count)) < 0
    ):
        raise ValueError(f"candidate evaluation {identity} firm_count is invalid")
    minimum_firms = (
        PROVISIONAL_CORNER_MIN_FIRMS
        if market in CORNER_MARKETS
        else PROVISIONAL_MIN_FIRMS
        if market in {"total", "htft"}
        else 1
    )
    depth_passed = firm_count is not None and int(float(firm_count)) >= minimum_firms
    gates.append(
        _candidate_gate(
            "bookmaker_depth",
            "risk",
            depth_passed,
            [f"bookmaker_count_below_{minimum_firms}"],
        )
    )
    quality_passed = record.get("data_quality") in {"medium", "high"}
    gates.append(
        _candidate_gate(
            "data_quality",
            "risk",
            quality_passed,
            ["medium_or_high_data_quality_required"],
        )
    )
    signal = candidate["market_signal"]
    signal_passed = signal in {"aligned", "neutral", "against", "conflicting"}
    gates.append(
        _candidate_gate(
            "market_signal_classified",
            "risk",
            signal_passed,
            ["market_signal_unclassified"],
        )
    )
    directional_support = candidate_directional_fundamental_support(record, candidate)
    directionally_supported = bool(
        directional_support
        and directional_support.get("directionally_supported") is True
    )
    formal_directional_support = bool(
        directionally_supported
        and directional_support.get("formal_gate_eligible") is True
    )
    adverse_passed = True
    adverse_reasons: list[str] = []
    if signal in ADVERSE_MARKET_SIGNALS:
        adverse_passed = bool(
            calculated_ev is not None
            and calculated_ev >= ADVERSE_FORMAL_MIN_EV
            and calculated_edge is not None
            and calculated_edge >= ADVERSE_FORMAL_MIN_EDGE_PP
            and firm_count is not None
            and int(float(firm_count)) >= PROVISIONAL_MIN_FIRMS
            and directionally_supported
        )
        if not adverse_passed:
            adverse_reasons.append("adverse_market_safety_thresholds_not_met")
    gates.append(
        _candidate_gate("adverse_signal_gate", "risk", adverse_passed, adverse_reasons)
    )
    market_specific_passed = True
    market_specific_reasons: list[str] = []
    if market in {"total", "goal_range", "btts"}:
        market_specific_passed = directionally_supported
        if not market_specific_passed:
            market_specific_reasons.append(
                "candidate_directional_attacking_evidence_required"
            )
    elif market in CORNER_MARKETS:
        market_specific_passed = directionally_supported
        if not market_specific_passed:
            market_specific_reasons.append(
                "candidate_directional_corner_evidence_required"
            )
    elif market == "asian" and float(candidate.get("line", 0.0)) <= DEEP_FAVORITE_LINE:
        market_specific_passed = bool(
            record.get("data_quality") == "high"
            and raw.get("cover_distribution_validated") is True
            and directionally_supported
        )
        if not market_specific_passed:
            market_specific_reasons.append("deep_favorite_safety_evidence_required")
    gates.append(
        _candidate_gate(
            "market_specific_evidence",
            "risk",
            market_specific_passed,
            market_specific_reasons,
        )
    )
    directional_release_required = market in {
        "total",
        "goal_range",
        "btts",
        *CORNER_MARKETS,
    } or (market == "asian" and float(candidate.get("line", 0.0)) <= DEEP_FAVORITE_LINE)
    if directional_release_required:
        gates.append(
            _candidate_gate(
                "candidate_fundamental_release_evidence",
                "release",
                formal_directional_support,
                [
                    "directional_fundamental_rule_is_shadow_only"
                    if directionally_supported
                    else "candidate_directional_fundamental_support_unavailable"
                ],
            )
        )

    policy_enabled = market not in STRICT_OOS_MARKET_STATUS
    gates.append(
        _candidate_gate(
            "market_policy_enabled",
            "release",
            policy_enabled,
            ["market_observation_only_under_active_policy"],
        )
    )
    if market in {"half_time", "htft"}:
        league_forward = False
        if market == "htft":
            htft_audit = next(
                (
                    item
                    for item in record.get("candidate_audits", [])
                    if isinstance(item, dict) and item.get("market") == "htft"
                ),
                None,
            )
            league_evidence = (
                htft_audit.get("league_gate_evidence")
                if isinstance(htft_audit, dict)
                else None
            )
            league_forward = bool(
                isinstance(league_evidence, dict)
                and league_evidence.get("production_confidence_eligible") is True
            )
        gates.append(
            _candidate_gate(
                "league_forward_evidence",
                "release",
                league_forward,
                ["league_forward_release_evidence_unavailable"],
            )
        )
    if market in CORNER_MARKETS:
        upstream_enabled = model_binding.get("upstream_formal_eligible") is True
        gates.append(
            _candidate_gate(
                "upstream_formal_policy",
                "release",
                upstream_enabled,
                ["upstream_corner_model_remains_non_formal"],
            )
        )

    counterfactual_eligible = all(
        gate["passed"] for gate in gates if gate["category"] != "release"
    )
    formal_eligible = all(gate["passed"] for gate in gates)
    candidate.update(
        {
            "market_probability": no_vig_probability,
            "edge_probability": edge_probability,
            "edge_basis": "five_state_effective_win_probability_vs_complete_market_no_vig",
            "complete_market_probabilities": complete_market_probabilities,
            "ev": calculated_ev,
            "edge_pp": calculated_edge,
            "candidate_fundamental_support": directional_support,
            "gates": gates,
            "counterfactual_eligible": counterfactual_eligible,
            "formal_eligible": formal_eligible,
            "release_blockers": [
                gate["gate"]
                for gate in gates
                if gate["category"] == "release" and not gate["passed"]
            ],
            "rejection_codes": [
                f"{gate['category']}:{gate['gate']}"
                for gate in gates
                if not gate["passed"]
            ],
            "shadow_rank": None,
            "shadow_selected": False,
        }
    )
    return candidate


def _rank_candidate_shadow_selections(
    record: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(str(candidate["market"]), []).append(candidate)
    shadow_selections: dict[str, str] = {}
    for market, market_candidates in grouped.items():
        eligible = [
            item for item in market_candidates if item["counterfactual_eligible"]
        ]
        for item in eligible:
            item["shadow_confidence"] = confidence_components(record, market, item)
        eligible.sort(
            key=lambda item: (
                -float(item["shadow_confidence"]["score"]),
                -float(item["shadow_confidence"]["settlement_safety_probability"]),
                -float(item["shadow_confidence"]["firm_count"]),
                item["identity"],
            )
        )
        for rank, item in enumerate(eligible, start=1):
            item["shadow_rank"] = rank
            item["shadow_selected"] = rank == 1
            if rank == 1:
                shadow_selections[market] = str(item["candidate_id"])
    return shadow_selections


def _candidate_evaluation_record_context(record: dict[str, Any]) -> dict[str, Any]:
    basis = record.get("settlement_basis")
    if not isinstance(basis, dict):
        return record
    context = deepcopy(basis)
    context["status"] = "pending"
    context["updated_at"] = basis.get("version_archived_at", basis.get("archived_at"))
    context["created_at"] = context.get("updated_at")
    return context


def _candidate_evaluation_model_binding(record: dict[str, Any]) -> dict[str, Any]:
    context = _candidate_evaluation_record_context(record)
    joint = validated_joint_scenario_audit(context)
    joint_audit = context.get("joint_scenario_audit")
    score_provenance = context.get("score_model_provenance")
    return {
        "joint_scenario_audit_hash": (
            joint_audit.get("audit_hash") if isinstance(joint_audit, dict) else None
        ),
        "joint_prediction_hash": (
            joint.get("prediction_hash") if isinstance(joint, dict) else None
        ),
        "score_model_hash": (
            score_provenance.get("model_hash")
            if isinstance(score_provenance, dict)
            else None
        ),
    }


def _candidate_evaluation_active_version_binding(
    record: dict[str, Any],
) -> dict[str, Any]:
    context = _candidate_evaluation_record_context(record)
    archived_value = context.get("version_archived_at")
    if archived_value is None:
        archived_value = context.get("archived_at")
    if archived_value is None:
        archived_value = context.get("updated_at", context.get("created_at"))
    archived_at = parse_aware_datetime(
        str(archived_value or ""), "candidate evaluation active version archived_at"
    ).astimezone(timezone.utc)
    fixture_id = str(context.get("fixture_id", context.get("match_id")) or "").strip()
    if not fixture_id:
        raise ValueError("candidate evaluation active version fixture_id is required")
    model = _candidate_evaluation_model_binding(context)
    context_hash = calculate_candidate_evaluation_source_hash(
        {
            "data_quality": context.get("data_quality", "unknown"),
            "guardrail_evidence": context.get("guardrail_evidence", {}),
        }
    )
    return {
        "analysis_stage": str(context.get("analysis_stage") or "initial"),
        "version_archived_at": archived_at.isoformat(),
        "fixture_id": fixture_id,
        **model,
        "evaluation_context_hash": context_hash,
    }


def _candidate_evaluation_observation_id(
    source_payload_hash: str, active_version_binding: dict[str, Any]
) -> str:
    return calculate_candidate_evaluation_source_hash(
        {
            "source_payload_hash": source_payload_hash,
            "active_version_binding": active_version_binding,
        }
    )


def calculate_source_evidence_audit_hash(audit: dict[str, Any]) -> str:
    payload = deepcopy(audit)
    payload.pop("audit_hash", None)
    return canonical_prediction_hash(payload)


def calculate_fundamental_evidence_audit_hash(audit: dict[str, Any]) -> str:
    payload = deepcopy(audit)
    payload.pop("audit_hash", None)
    return canonical_prediction_hash(payload)


def load_fundamental_evidence_audit(
    args: argparse.Namespace, record: dict[str, Any]
) -> dict[str, Any] | None:
    supplied = str(getattr(args, "fundamental_evidence_file", "") or "").strip()
    required = record.get("forward_policy_binding") is not None
    if not supplied:
        if required:
            raise ValueError(
                "active untouched live-forward cohort requires "
                "--fundamental-evidence-file"
            )
        return None
    evidence_path = Path(supplied).resolve()
    try:
        evidence = fundamental_evidence.validate_evidence_file(evidence_path)
    except fundamental_evidence.FundamentalEvidenceError as exc:
        raise ValueError("fundamental evidence replay failed") from exc
    if (
        required
        and evidence.get("schema_version")
        != fundamental_evidence.EVIDENCE_SCHEMA_VERSION
    ):
        raise ValueError(
            "active untouched live-forward cohort requires adapter-bound candidate-directional fundamental-evidence/3"
        )
    fixture = evidence.get("fixture")
    if not isinstance(fixture, dict):
        raise ValueError("fundamental evidence fixture is missing")
    for field in ("match_id", "home_team", "away_team"):
        if str(fixture.get(field) or "") != str(record.get(field) or ""):
            raise ValueError(
                f"fundamental evidence fixture {field} does not match the record"
            )
    evidence_kickoff = parse_aware_datetime(
        str(fixture.get("kickoff") or ""), "fundamental evidence kickoff"
    )
    record_kickoff = parse_aware_datetime(
        str(record.get("kickoff") or ""), "record kickoff"
    )
    generated_at = parse_aware_datetime(
        str(evidence.get("generated_at") or ""),
        "fundamental evidence generated_at",
    )
    archived_at = parse_aware_datetime(
        str(record.get("updated_at") or ""), "fundamental evidence archive time"
    )
    if evidence_kickoff.astimezone(timezone.utc) != record_kickoff.astimezone(
        timezone.utc
    ):
        raise ValueError("fundamental evidence kickoff does not match the record")
    if generated_at >= record_kickoff:
        raise ValueError(
            "fundamental evidence must be generated strictly before kickoff"
        )
    if generated_at > archived_at:
        raise ValueError("fundamental evidence cannot be generated after archive time")
    directional_evidence = evidence.get("schema_version") in {
        fundamental_evidence.PREVIOUS_DIRECTIONAL_EVIDENCE_SCHEMA_VERSION,
        fundamental_evidence.EVIDENCE_SCHEMA_VERSION,
    }
    if directional_evidence:
        availability = evidence.get("availability_claims")
        candidate_support = evidence.get("candidate_support")
        if not isinstance(availability, dict) or set(availability) != set(
            fundamental_evidence.AVAILABILITY_CLAIM_FIELDS
        ):
            raise ValueError("fundamental evidence availability claims are incomplete")
        if not isinstance(candidate_support, dict):
            raise ValueError("fundamental evidence candidate support is incomplete")
        claims = {field: False for field in fundamental_evidence.CLAIM_FIELDS}
    else:
        claims = evidence.get("derived_claims")
        if not isinstance(claims, dict) or set(claims) != set(
            fundamental_evidence.CLAIM_FIELDS
        ):
            raise ValueError("fundamental evidence claims are incomplete")
        availability = None
        candidate_support = None
    audit = {
        "schema_version": evidence["schema_version"],
        "kind": "replayable_fundamental_evidence",
        "evidence_file": str(evidence_path),
        "evidence_hash": evidence["evidence_hash"],
        "fixture": deepcopy(fixture),
        "generated_at": generated_at.isoformat(),
        "derived_claims": deepcopy(claims),
        "availability_claims": deepcopy(availability),
        "candidate_support": deepcopy(candidate_support),
        "bundle": deepcopy(evidence),
        "raw_sources_replayed": len(evidence.get("sources", [])),
        "counts_toward_primary_record": False,
    }
    audit["audit_hash"] = calculate_fundamental_evidence_audit_hash(audit)
    return audit


def validated_fundamental_evidence_audit(
    record: dict[str, Any],
) -> dict[str, Any] | None:
    basis = record.get("settlement_basis")
    context = basis if isinstance(basis, dict) else record
    audit = context.get("fundamental_evidence_audit")
    if audit is None:
        return None
    if (
        not isinstance(audit, dict)
        or audit.get("schema_version")
        not in {
            fundamental_evidence.PREVIOUS_EVIDENCE_SCHEMA_VERSION,
            fundamental_evidence.PREVIOUS_DIRECTIONAL_EVIDENCE_SCHEMA_VERSION,
            fundamental_evidence.EVIDENCE_SCHEMA_VERSION,
        }
        or audit.get("kind") != "replayable_fundamental_evidence"
        or audit.get("counts_toward_primary_record") is not False
        or not isinstance(audit.get("bundle"), dict)
        or audit.get("audit_hash") != calculate_fundamental_evidence_audit_hash(audit)
    ):
        return None
    try:
        replayed = fundamental_evidence.validate_evidence_file(
            str(audit.get("evidence_file") or "")
        )
    except (fundamental_evidence.FundamentalEvidenceError, OSError):
        return None
    if replayed != audit.get("bundle") or replayed.get("evidence_hash") != audit.get(
        "evidence_hash"
    ):
        return None
    fixture = replayed.get("fixture")
    if not isinstance(fixture, dict):
        return None
    for field in ("match_id", "home_team", "away_team"):
        if str(fixture.get(field) or "") != str(context.get(field) or ""):
            return None
    if replayed.get("schema_version") in {
        fundamental_evidence.PREVIOUS_DIRECTIONAL_EVIDENCE_SCHEMA_VERSION,
        fundamental_evidence.EVIDENCE_SCHEMA_VERSION,
    }:
        claims = {field: False for field in fundamental_evidence.CLAIM_FIELDS}
        if (
            replayed.get("availability_claims") != audit.get("availability_claims")
            or replayed.get("candidate_support") != audit.get("candidate_support")
            or audit.get("derived_claims") != claims
        ):
            return None
    else:
        claims = replayed.get("derived_claims")
        if not isinstance(claims, dict) or claims != audit.get("derived_claims"):
            return None
    try:
        evidence_kickoff = parse_aware_datetime(
            str(fixture.get("kickoff") or ""), "fundamental evidence kickoff"
        )
        record_kickoff = parse_aware_datetime(
            str(context.get("kickoff") or ""), "record kickoff"
        )
        generated_at = parse_aware_datetime(
            str(replayed.get("generated_at") or ""),
            "fundamental evidence generated_at",
        )
        archived_value = (
            context.get("version_archived_at")
            or context.get("archived_at")
            or context.get("updated_at")
            or context.get("created_at")
        )
        archived_at = parse_aware_datetime(
            str(archived_value or ""), "fundamental evidence archive time"
        )
    except ValueError:
        return None
    if evidence_kickoff.astimezone(timezone.utc) != record_kickoff.astimezone(
        timezone.utc
    ):
        return None
    if generated_at >= record_kickoff or generated_at > archived_at:
        return None
    if audit.get("raw_sources_replayed") != len(replayed.get("sources", [])):
        return None
    if context.get("guardrail_evidence", {}) != {
        **deepcopy(claims),
        "injury_evidence_status": context.get("guardrail_evidence", {}).get(
            "injury_evidence_status", "not_used"
        ),
        "primary_htft_edge_pp": context.get("guardrail_evidence", {}).get(
            "primary_htft_edge_pp"
        ),
        "primary_htft_firm_count": context.get("guardrail_evidence", {}).get(
            "primary_htft_firm_count"
        ),
    }:
        return None
    return audit


def load_source_evidence_audit(
    args: argparse.Namespace, record: dict[str, Any]
) -> dict[str, Any] | None:
    supplied = str(getattr(args, "source_evidence_file", "") or "").strip()
    required = record.get("forward_policy_binding") is not None
    if not supplied:
        if required:
            raise ValueError(
                "active untouched live-forward cohort requires --source-evidence-file"
            )
        return None
    evidence_path = Path(supplied).resolve()
    try:
        evidence = source_evidence.validate_evidence_file(evidence_path)
    except source_evidence.SourceEvidenceError as exc:
        raise ValueError("source evidence replay failed") from exc
    if (
        required
        and evidence.get("schema_version") != source_evidence.EVIDENCE_SCHEMA_VERSION
    ):
        raise ValueError(
            "active untouched live-forward cohort requires canonical source-evidence/2.0.0"
        )
    fixture = evidence.get("fixture")
    if not isinstance(fixture, dict):
        raise ValueError("source evidence fixture is missing")
    for field in ("match_id", "home_team", "away_team"):
        if str(fixture.get(field) or "") != str(record.get(field) or ""):
            raise ValueError(
                f"source evidence fixture {field} does not match the record"
            )
    evidence_kickoff = parse_aware_datetime(
        str(fixture.get("kickoff") or ""), "source evidence kickoff"
    )
    record_kickoff = parse_aware_datetime(
        str(record.get("kickoff") or ""), "record kickoff"
    )
    if evidence_kickoff.astimezone(timezone.utc) != record_kickoff.astimezone(
        timezone.utc
    ):
        raise ValueError("source evidence kickoff does not match the record")
    generated_at = parse_aware_datetime(
        str(evidence.get("generated_at") or ""), "source evidence generated_at"
    )
    archived_at = parse_aware_datetime(
        str(record.get("updated_at") or ""), "source evidence archive time"
    )
    if generated_at >= record_kickoff:
        raise ValueError("source evidence must be generated strictly before kickoff")
    if generated_at > archived_at:
        raise ValueError("source evidence cannot be generated after archive time")
    audit = {
        "schema_version": evidence["schema_version"],
        "kind": "replayable_source_evidence",
        "evidence_file": str(evidence_path),
        "evidence_hash": evidence["evidence_hash"],
        "fixture": deepcopy(fixture),
        "generated_at": generated_at.isoformat(),
        "bundle": deepcopy(evidence),
        "raw_sources_replayed": len(evidence.get("sources", [])),
        "counts_toward_primary_record": False,
    }
    if evidence.get("schema_version") == source_evidence.EVIDENCE_SCHEMA_VERSION:
        audit["evidence_scope"] = "canonical_market_identity_forward_eligible"
    audit["audit_hash"] = calculate_source_evidence_audit_hash(audit)
    return audit


def validated_source_evidence_audit(record: dict[str, Any]) -> dict[str, Any] | None:
    basis = record.get("settlement_basis")
    context = basis if isinstance(basis, dict) else record
    audit = context.get("source_evidence_audit")
    if audit is None:
        return None
    if not isinstance(audit, dict):
        return None
    if (
        audit.get("schema_version")
        not in {
            source_evidence.EVIDENCE_SCHEMA_VERSION,
            source_evidence.LEGACY_EVIDENCE_SCHEMA_VERSION,
        }
        or audit.get("kind") != "replayable_source_evidence"
        or audit.get("counts_toward_primary_record") is not False
        or not isinstance(audit.get("bundle"), dict)
    ):
        return None
    if (
        audit.get("schema_version") == source_evidence.EVIDENCE_SCHEMA_VERSION
        and audit.get("evidence_scope") != "canonical_market_identity_forward_eligible"
    ):
        return None
    if (
        audit.get("schema_version") == source_evidence.LEGACY_EVIDENCE_SCHEMA_VERSION
        and "evidence_scope" in audit
    ):
        return None
    if audit.get("audit_hash") != calculate_source_evidence_audit_hash(audit):
        return None
    try:
        replayed = source_evidence.validate_evidence_file(
            str(audit.get("evidence_file") or "")
        )
    except (source_evidence.SourceEvidenceError, OSError):
        return None
    if replayed != audit.get("bundle") or replayed.get("evidence_hash") != audit.get(
        "evidence_hash"
    ):
        return None
    fixture = replayed.get("fixture")
    if not isinstance(fixture, dict):
        return None
    for field in ("match_id", "home_team", "away_team"):
        if str(fixture.get(field) or "") != str(context.get(field) or ""):
            return None
    try:
        evidence_kickoff = parse_aware_datetime(
            str(fixture.get("kickoff") or ""), "source evidence kickoff"
        )
        record_kickoff = parse_aware_datetime(
            str(context.get("kickoff") or ""), "record kickoff"
        )
        generated_at = parse_aware_datetime(
            str(replayed.get("generated_at") or ""), "source evidence generated_at"
        )
        archived_value = (
            context.get("version_archived_at")
            or context.get("archived_at")
            or context.get("updated_at")
            or context.get("created_at")
        )
        archived_at = parse_aware_datetime(
            str(archived_value or ""), "source evidence archive time"
        )
    except ValueError:
        return None
    if evidence_kickoff.astimezone(timezone.utc) != record_kickoff.astimezone(
        timezone.utc
    ):
        return None
    if generated_at >= record_kickoff or generated_at > archived_at:
        return None
    if audit.get("raw_sources_replayed") != len(replayed.get("sources", [])):
        return None
    return audit


def load_candidate_evaluation_audit(
    args: argparse.Namespace, record: dict[str, Any]
) -> dict[str, Any] | None:
    supplied = str(getattr(args, "candidate_evaluation_file", "") or "").strip()
    active_forward_binding = record.get("forward_policy_binding") is not None
    required = bool(getattr(args, "require_candidate_evaluations", False)) or bool(
        active_forward_binding
    )
    if not supplied:
        if required:
            if active_forward_binding:
                raise ValueError(
                    "Active untouched cohorts require a current "
                    "--candidate-evaluation-file (candidate-evaluation/3.0.0)"
                )
            raise ValueError(
                "--require-candidate-evaluations requires --candidate-evaluation-file"
            )
        return None
    path, raw_bytes, payload = load_observation_json(
        args, supplied, "candidate evaluation file"
    )
    if (
        payload.get("artifact_type") != CANDIDATE_EVALUATION_ARTIFACT_TYPE
        or payload.get("schema_version") != CANDIDATE_EVALUATION_SCHEMA_VERSION
    ):
        raise ValueError(
            "candidate evaluation file must use soccer_candidate_evaluation "
            "candidate-evaluation/3.0.0"
        )
    if payload.get("policy_version") != STRICT_OOS_POLICY_VERSION:
        raise ValueError(
            "candidate evaluation policy_version does not match the active policy"
        )
    if payload.get("selection_policy_version") != CONFIDENCE_POLICY_VERSION:
        raise ValueError(
            "candidate evaluation selection_policy_version does not match the active selector"
        )
    fixture = payload.get("fixture")
    if not isinstance(fixture, dict):
        raise ValueError("candidate evaluation fixture is required")
    if str(fixture.get("match_id") or "") != str(record.get("match_id") or ""):
        raise ValueError(
            "candidate evaluation fixture match_id does not match the record"
        )
    if str(fixture.get("home_team") or "") != str(record.get("home_team") or ""):
        raise ValueError(
            "candidate evaluation fixture home_team does not match the record"
        )
    if str(fixture.get("away_team") or "") != str(record.get("away_team") or ""):
        raise ValueError(
            "candidate evaluation fixture away_team does not match the record"
        )
    fixture_kickoff = parse_aware_datetime(
        str(fixture.get("kickoff") or ""), "candidate evaluation fixture kickoff"
    )
    record_kickoff = parse_aware_datetime(
        str(record.get("kickoff") or ""), "record kickoff"
    )
    if fixture_kickoff.astimezone(timezone.utc) != record_kickoff.astimezone(
        timezone.utc
    ):
        raise ValueError(
            "candidate evaluation fixture kickoff does not match the record"
        )
    generated_at = parse_aware_datetime(
        str(payload.get("generated_at") or ""), "candidate evaluation generated_at"
    )
    archived_at = parse_aware_datetime(
        str(record.get("updated_at") or ""), "candidate evaluation archive time"
    )
    if generated_at >= record_kickoff:
        raise ValueError(
            "candidate evaluation must be generated strictly before kickoff"
        )
    if generated_at > archived_at:
        raise ValueError("candidate evaluation cannot be generated after archive time")

    manifest_raw = payload.get("market_manifest")
    if not isinstance(manifest_raw, list) or not manifest_raw:
        raise ValueError(
            "candidate evaluation market_manifest must be a non-empty list"
        )
    manifest: dict[str, dict[str, Any]] = {}
    for item in manifest_raw:
        if not isinstance(item, dict):
            raise ValueError(
                "candidate evaluation market_manifest entries must be objects"
            )
        market = str(item.get("market") or "").strip().lower()
        status = str(item.get("status") or "").strip().lower()
        if market not in PRIMARY_MARKETS or market in manifest:
            raise ValueError(
                "candidate evaluation market_manifest markets must be unique and supported"
            )
        if status not in {"evaluated", "unavailable"}:
            raise ValueError(
                "candidate evaluation manifest status must be evaluated or unavailable"
            )
        reasons = (
            [
                str(reason).strip()
                for reason in item.get("reasons", [])
                if str(reason).strip()
            ]
            if isinstance(item.get("reasons", []), list)
            else []
        )
        if status == "unavailable" and not reasons:
            raise ValueError("unavailable candidate evaluation markets require reasons")
        manifest[market] = {"market": market, "status": status, "reasons": reasons}
    if set(manifest) != set(PRIMARY_MARKETS):
        raise ValueError(
            "candidate-evaluation/3.0.0 requires every supported market in market_manifest"
        )

    candidates_raw = payload.get("candidates")
    if not isinstance(candidates_raw, list):
        raise ValueError("candidate evaluation candidates must be a list")
    raw_artifact_sha256 = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    source_payload_hash = calculate_candidate_evaluation_source_hash(payload)
    active_version_binding = _candidate_evaluation_active_version_binding(record)
    observation_id = _candidate_evaluation_observation_id(
        source_payload_hash, active_version_binding
    )
    candidates: list[dict[str, Any]] = []
    identities: set[str] = set()
    source_audit = validated_source_evidence_audit(record)
    source_required = record.get("forward_policy_binding") is not None
    if source_required and source_audit is None:
        raise ValueError(
            "active untouched live-forward cohort requires valid replayable source evidence"
        )
    for index, raw_candidate in enumerate(candidates_raw, start=1):
        if not isinstance(raw_candidate, dict):
            raise ValueError("candidate evaluation candidates must be objects")
        candidate = _evaluate_candidate(
            record, raw_candidate, observation_id, index, generated_at
        )
        if source_audit is not None:
            try:
                candidate["source_evidence_binding"] = source_evidence.match_candidate(
                    source_audit["bundle"], raw_candidate
                )
            except source_evidence.SourceEvidenceError as exc:
                raise ValueError(
                    "candidate evaluation market does not replay from source evidence"
                ) from exc
        elif source_required:
            raise ValueError(
                "candidate evaluation lacks required replayable source evidence"
            )
        market = candidate["market"]
        if market not in manifest or manifest[market]["status"] != "evaluated":
            raise ValueError(
                "candidate evaluation candidate is absent from an evaluated manifest market"
            )
        if candidate["identity"] in identities:
            raise ValueError("candidate evaluation candidate identities must be unique")
        identities.add(candidate["identity"])
        candidates.append(candidate)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate["market"], []).append(candidate)
    for market, entry in manifest.items():
        count = len(grouped.get(market, []))
        if entry["status"] == "evaluated" and count == 0:
            raise ValueError(f"evaluated candidate market {market} has no candidates")
        if entry["status"] == "unavailable" and count:
            raise ValueError(
                f"unavailable candidate market {market} cannot contain candidates"
            )
        entry["candidate_count"] = count

    shadow_selections = _rank_candidate_shadow_selections(record, candidates)

    model_binding = _candidate_evaluation_model_binding(record)
    audit = {
        "schema_version": CANDIDATE_EVALUATION_SCHEMA_VERSION,
        "kind": CANDIDATE_EVALUATION_KIND,
        "observation_id": observation_id,
        "status": "observation_only",
        "archived_at": record.get("updated_at"),
        "counts_toward_primary_record": False,
        "monetary_scope": "none",
        "fixture": {
            "match_id": str(record.get("match_id")),
            "home_team": record.get("home_team"),
            "away_team": record.get("away_team"),
            "kickoff": record_kickoff.isoformat(),
        },
        "model": model_binding,
        "active_version_binding": active_version_binding,
        "artifact": {
            "artifact_type": payload.get("artifact_type"),
            "schema_version": payload.get("schema_version"),
            "artifact_sha256": source_payload_hash,
            "raw_artifact_sha256": raw_artifact_sha256,
            "artifact_filename": path.name,
            "generated_at": generated_at.isoformat(),
            "source_payload_hash": source_payload_hash,
            "source_payload": deepcopy(payload),
        },
        "policy": {
            "market_policy_version": STRICT_OOS_POLICY_VERSION,
            "selection_policy_version": CONFIDENCE_POLICY_VERSION,
            "market_status": deepcopy(STRICT_OOS_MARKET_STATUS),
            "automatic_release_allowed": False,
        },
        "market_manifest": [
            manifest[market] for market in PRIMARY_MARKETS if market in manifest
        ],
        "candidates": candidates,
        "candidate_count": len(candidates),
        "shadow_selections": shadow_selections,
        "provenance": {
            "strict_forward_oos": True,
            "fixture_validated": True,
            "generated_before_kickoff": True,
            "training_cutoff_before_kickoff": True,
            "canonical_probabilities_reproduced": True,
            "market_values_recalculated": True,
            "raw_source_evidence_replayed": source_audit is not None,
            "source_evidence_hash": (
                source_audit.get("evidence_hash")
                if isinstance(source_audit, dict)
                else None
            ),
        },
    }
    audit["audit_hash"] = calculate_candidate_evaluation_audit_hash(audit)
    return audit


def validate_score_model_consistency(
    record: dict[str, Any],
    provenance: dict[str, Any] | None,
) -> dict[str, Any]:
    football_formal = [
        market for market, _ in formal_picks(record) if market in FOOTBALL_MODEL_MARKETS
    ]
    if provenance is None:
        if football_formal:
            raise ValueError(
                "Formal football picks require a validated --score-model-file prediction artifact"
            )
        return {
            "policy_version": STRICT_OOS_POLICY_VERSION,
            "strict_forward_oos": True,
            "reason": "no_formal_core_pick",
        }

    matrix = provenance["score_matrix"]
    fixture = provenance.get("fixture", {})
    if football_formal and fixture.get("unknown_team_policy") != "error":
        raise ValueError(
            "Formal football picks require score prediction fixture.unknown_team_policy=error"
        )
    if str(fixture.get("home_team") or "") != str(record.get("home_team") or ""):
        raise ValueError("score prediction fixture home_team must match the record")
    if str(fixture.get("away_team") or "") != str(record.get("away_team") or ""):
        raise ValueError("score prediction fixture away_team must match the record")
    fixture_kickoff = parse_datetime(str(fixture.get("kickoff") or ""))
    record_kickoff = parse_datetime(str(record.get("kickoff") or ""))
    if fixture_kickoff != record_kickoff:
        raise ValueError(
            "score prediction fixture kickoff must match the record kickoff"
        )
    generated_at = parse_datetime(str(provenance.get("generated_at") or ""))
    if generated_at >= record_kickoff:
        raise ValueError("score prediction generated_at must be before record kickoff")
    if generated_at > parse_datetime(str(record.get("updated_at") or "")):
        raise ValueError(
            "score prediction generated_at cannot be later than archive time"
        )
    derived_1x2 = matrix_1x2(matrix)
    for label, expected in derived_1x2.items():
        validate_probability_close(
            record.get("probabilities", {}).get(label),
            expected,
            f"1X2 {label} probability",
        )
    ranked_scores = sorted(
        (
            {
                "score": f"{home}-{away}",
                "probability": probability,
                "home": home,
                "away": away,
            }
            for home, row in enumerate(matrix)
            for away, probability in enumerate(row)
        ),
        key=lambda item: (-item["probability"], item["home"], item["away"]),
    )
    archived_scores = record.get("exact_score_picks", [])
    for rank, (pick, expected_pick) in enumerate(
        zip(archived_scores, ranked_scores[:2]), start=1
    ):
        if (
            pick.get("score") != expected_pick["score"]
            or int(pick.get("rank", 0)) != rank
        ):
            raise ValueError(
                "Archived exact-score picks must equal the score matrix deterministic Top 2"
            )
        validate_probability_close(
            pick.get("probability"),
            float(expected_pick["probability"]),
            f"exact score rank {rank}",
        )
    if record.get("predicted_score") != ranked_scores[0]["score"]:
        raise ValueError("predicted_score must equal the score matrix rank 1")
    zero_zero_item = next(
        (index, item)
        for index, item in enumerate(ranked_scores, start=1)
        if item["score"] == "0-0"
    )
    zero_zero_rank, zero_zero_pick = zero_zero_item
    zero_zero = float(zero_zero_pick["probability"])
    validate_probability_close(
        record.get("zero_zero_audit", {}).get("probability"),
        zero_zero,
        "0-0 audit probability",
    )
    if int(record.get("zero_zero_audit", {}).get("rank", 0)) != zero_zero_rank:
        raise ValueError("0-0 audit rank must match the full score matrix ranking")

    if record.get("primary_market") == "total":
        total_pick = record.get("total_pick")
        display_basis = record.get("display_exact_score_basis")
        display_picks = record.get("display_exact_score_picks")
        if not isinstance(total_pick, dict):
            raise ValueError("A total primary requires a total pick")
        try:
            basis_matches = (
                isinstance(display_basis, dict)
                and display_basis.get("basis") == "primary_total_net_profit"
                and display_basis.get("market") == "total"
                and display_basis.get("side") == total_pick.get("side")
                and math.isclose(
                    float(display_basis.get("line")),
                    float(total_pick.get("line")),
                    abs_tol=1e-12,
                )
            )
        except (TypeError, ValueError):
            basis_matches = False
        if not basis_matches:
            raise ValueError(
                "A formal total primary requires canonical primary-conditioned display scores"
            )
        if not isinstance(display_picks, list) or len(display_picks) != 2:
            raise ValueError(
                "A formal total primary requires exactly two conditioned display scores"
            )

        unconditional_ranks = {
            item["score"]: index for index, item in enumerate(ranked_scores, start=1)
        }
        branch_scores: list[dict[str, Any]] = []
        event_probability = 0.0
        for home, row in enumerate(matrix):
            for away, probability in enumerate(row):
                if settle_total(total_pick, home, away) not in {"win", "half_win"}:
                    continue
                event_probability += probability
                if probability > 0.0:
                    score = f"{home}-{away}"
                    branch_scores.append(
                        {
                            "score": score,
                            "probability": probability,
                            "home": home,
                            "away": away,
                            "unconditional_rank": unconditional_ranks[score],
                        }
                    )
        branch_scores.sort(
            key=lambda item: (-item["probability"], item["home"], item["away"])
        )
        if len(branch_scores) < 2 or event_probability <= 0.0:
            raise ValueError(
                "The total-primary net-profit branch must contain two positive-probability scores"
            )
        validate_probability_close(
            display_basis.get("event_probability"),
            event_probability,
            "display exact-score event probability",
        )
        for rank, (pick, expected_pick) in enumerate(
            zip(display_picks, branch_scores[:2]), start=1
        ):
            if (
                pick.get("score") != expected_pick["score"]
                or int(pick.get("display_rank", 0)) != rank
                or int(pick.get("unconditional_rank", 0))
                != expected_pick["unconditional_rank"]
            ):
                raise ValueError(
                    "Conditioned display scores must equal the canonical branch Top 2"
                )
            validate_probability_close(
                pick.get("probability"),
                float(expected_pick["probability"]),
                f"conditioned display score rank {rank}",
            )
            validate_probability_close(
                pick.get("conditional_probability"),
                float(expected_pick["probability"]) / event_probability,
                f"conditioned display score rank {rank} conditional probability",
            )
        if record.get("display_predicted_score") != branch_scores[0]["score"]:
            raise ValueError(
                "display_predicted_score must equal the canonical branch rank 1"
            )

    for market in ("asian", "total"):
        pick = record.get(PICK_KEY_BY_MARKET[market])
        if not isinstance(pick, dict):
            continue
        expected_distribution = matrix_settlement_distribution(matrix, market, pick)
        for state, expected in expected_distribution.items():
            validate_probability_close(
                pick.get("settlement_probabilities", {}).get(state),
                expected,
                f"{market} {state} probability",
            )
    for market in ("goal_range", "btts"):
        pick = record.get(PICK_KEY_BY_MARKET[market])
        if not isinstance(pick, dict):
            continue
        expected = 0.0
        for home, row in enumerate(matrix):
            for away, probability in enumerate(row):
                result = (
                    settle_goal_range(pick, home, away)
                    if market == "goal_range"
                    else settle_btts(pick, home, away)
                )
                if result == "win":
                    expected += probability
        validate_probability_close(
            pick.get("probability"), expected, f"{market} probability"
        )

    snapshot = provenance["snapshot"]
    half_pick = record.get("half_time_pick")
    if isinstance(half_pick, dict):
        half_matrix = snapshot.get("half_time_score_matrix")
        if half_matrix is None:
            return {
                "policy_version": STRICT_OOS_POLICY_VERSION,
                "strict_forward_oos": False,
                "reason": "missing_half_time_score_matrix",
            }
        expected_distribution = matrix_settlement_distribution(
            half_matrix, "half_time", half_pick
        )
        for state, expected in expected_distribution.items():
            validate_probability_close(
                half_pick.get("settlement_probabilities", {}).get(state),
                expected,
                f"half_time {state} probability",
            )
    if record.get("htft_picks"):
        htft_matrix = snapshot.get("htft_matrix")
        if htft_matrix is None:
            return {
                "policy_version": STRICT_OOS_POLICY_VERSION,
                "strict_forward_oos": False,
                "reason": "missing_htft_model_matrix",
            }
        for pick in record["htft_picks"]:
            selection = str(pick.get("selection") or "").upper()
            validate_probability_close(
                pick.get("probability"),
                float(htft_matrix[selection]),
                f"HT/FT {selection} model probability",
            )
    return {
        "policy_version": STRICT_OOS_POLICY_VERSION,
        "strict_forward_oos": True,
        "reason": "validated_score_model_provenance",
    }


def parse_htft_pick(value: str, odds_format: str | None = None) -> dict[str, Any]:
    parts = [part.strip() for part in value.split(":")]
    if len(parts) != 4:
        raise ValueError(
            "HT/FT pick must be SELECTION:ODDS:PROBABILITY:EV, for example DD:3.40:0.31:0.054"
        )
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


def parse_selection_float_values(
    values: list[str] | None,
    label: str,
) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for value in values or []:
        parts = [part.strip() for part in str(value).split(":")]
        if len(parts) != 2:
            raise ValueError(f"{label} must be SELECTION:VALUE")
        selection = parts[0].upper()
        if selection in parsed:
            raise ValueError(f"Duplicate {label} selection: {selection}")
        try:
            number = float(parts[1])
        except ValueError as exc:
            raise ValueError(f"{label} value for {selection} must be numeric") from exc
        if not math.isfinite(number):
            raise ValueError(f"{label} value for {selection} must be finite")
        parsed[selection] = number
    return parsed


def parse_market_odds_values(
    values: list[str] | None,
    label: str,
) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for value in values or []:
        parts = [part.strip() for part in str(value).rsplit(":", 1)]
        if len(parts) != 2 or not parts[0]:
            raise ValueError(f"{label} must be LABEL:PRICE")
        outcome = parts[0]
        normalized_outcome = (
            outcome.upper() if label == "HT/FT market odds" else outcome.lower()
        )
        if normalized_outcome in parsed:
            raise ValueError(f"Duplicate {label} outcome: {normalized_outcome}")
        try:
            price = float(parts[1])
        except ValueError as exc:
            raise ValueError(
                f"{label} price for {normalized_outcome} must be numeric"
            ) from exc
        if not math.isfinite(price):
            raise ValueError(f"{label} price for {normalized_outcome} must be finite")
        parsed[normalized_outcome] = price
    return parsed


def settlement_probability_args(
    args: argparse.Namespace, prefix: str
) -> dict[str, Any]:
    return {
        state: getattr(args, f"{prefix}_{state}_probability", None)
        for state in ("full_win", "half_win", "push", "half_loss", "loss")
    }


def market_audit_args(args: argparse.Namespace, prefix: str) -> dict[str, Any]:
    odds_label = "HT/FT market odds" if prefix == "htft" else f"{prefix} market odds"
    return {
        "market_complete": bool(getattr(args, f"{prefix}_market_complete", False)),
        "market_probability": getattr(args, f"{prefix}_market_probability", None),
        "market_source": getattr(args, f"{prefix}_market_source", None),
        "market_collected_at": getattr(args, f"{prefix}_market_collected_at", None),
        "price_basis": getattr(args, f"{prefix}_price_basis", None),
        "complete_market_odds": parse_market_odds_values(
            getattr(args, f"{prefix}_market_odds", None),
            odds_label,
        ),
    }


def validate_market_collection_times(record: dict[str, Any], current: datetime) -> None:
    kickoff = parse_datetime(str(record["kickoff"]))
    current_utc = current.astimezone(timezone.utc)
    for market, pick in formal_picks(record):
        collected_text = str(pick.get("market_collected_at") or "").strip()
        if not collected_text:
            continue
        try:
            collected = parse_datetime(collected_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{market} market_collected_at must be a parseable datetime with timezone"
            ) from exc
        if collected > current_utc:
            raise ValueError(f"{market} market_collected_at cannot be in the future")
        if collected >= kickoff:
            raise ValueError(f"{market} market_collected_at must be before kickoff")
        validate_market_evidence_freshness(
            record,
            collected_text,
            f"{market} market_collected_at",
        )


def parse_exact_score_pick(value: str) -> dict[str, Any]:
    parts = [part.strip() for part in value.split(":")]
    if len(parts) != 2:
        raise ValueError(
            "Exact-score pick must be SCORE:PROBABILITY, for example 2-1:0.126"
        )
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
            "Display exact-score pick must be SCORE:PROBABILITY:UNCONDITIONAL_RANK"
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
    event_probability = getattr(args, "display_exact_score_event_probability", None)
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
        supports_primary = total_goals > line if side == "over" else total_goals < line
        if not supports_primary:
            raise ValueError(
                f"Display exact score {pick['score']} does not support "
                f"the net-profit branch of {side} {line:g}"
            )
        if float(pick["probability"]) > event_probability + 1e-9:
            raise ValueError(
                "Display exact-score probability cannot exceed event probability"
            )

    display.sort(
        key=lambda pick: (
            -float(pick["probability"]),
            *(int(part) for part in str(pick["score"]).split("-")),
        )
    )
    for rank, pick in enumerate(display, start=1):
        pick["display_rank"] = rank
        pick["conditional_probability"] = float(pick["probability"]) / event_probability
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
            raise ValueError(
                "0-0 outside the Top-2 cannot replace a higher-ranked pick"
            )
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
    return next(
        (item for item in history if str(item.get("match_id")) == str(match_id)), None
    )


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
        raise ValueError(
            f"{label} is required by the provisional formal-pick guardrail"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if number + 1e-9 < minimum:
        raise ValueError(
            f"{label} must be at least {minimum:g}; downgrade the candidate to observation"
        )
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


def validate_probability_triplet(probabilities: dict[str, Any]) -> None:
    values: list[float] = []
    for label in ("home_win", "draw", "away_win"):
        value = probabilities.get(label)
        if (
            value is None
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ValueError(
                f"1X2 {label} probability must be finite and between 0 and 1"
            )
        values.append(float(value))
    if abs(sum(values) - 1.0) > PROBABILITY_AUDIT_TOLERANCE:
        raise ValueError("1X2 home/draw/away probabilities must sum to 1")


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
            supplied = record.get("guardrail_evidence", {}).get("primary_htft_edge_pp")
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
        supplied = record.get("guardrail_evidence", {}).get("primary_htft_firm_count")
        return float(supplied) if supplied is not None else None
    return None


def effective_market_signal(market: str, pick: dict[str, Any]) -> str:
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
        raise ValueError(
            "Total formal pick requires side over/under and a current line"
        )
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
        require_minimum(
            pick.get("ev"), ADVERSE_FORMAL_MIN_EV, f"{market} adverse-signal EV"
        )
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
        if not formal_candidate_directional_support(record, pick, market=market):
            raise ValueError(
                f"{market} adverse-market formal pick requires exact candidate-directional evidence"
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


def validate_goal_range_outcome_labels(labels: set[str]) -> None:
    if not labels:
        raise ValueError("goal_range complete market odds are required")
    parsed = [parse_goal_range_selection(label) for label in labels]
    parsed.sort(key=lambda item: int(item["minimum_goals"]))
    if int(parsed[0]["minimum_goals"]) != 0:
        raise ValueError("goal_range complete market bands must start at 0")
    expected_minimum = 0
    open_ended_seen = False
    for index, item in enumerate(parsed):
        minimum = int(item["minimum_goals"])
        maximum = item["maximum_goals"]
        if minimum != expected_minimum or open_ended_seen:
            raise ValueError(
                "goal_range complete market bands must be continuous and non-overlapping"
            )
        if maximum is None:
            if index != len(parsed) - 1:
                raise ValueError("goal_range open-ended N+ band must be last")
            open_ended_seen = True
        else:
            expected_minimum = int(maximum) + 1
    if not open_ended_seen:
        raise ValueError("goal_range complete market bands must end with N+")


def required_market_outcomes(pick: dict[str, Any], market: str) -> tuple[set[str], str]:
    if market in {"asian", "corner_handicap"}:
        return {"home", "away"}, str(pick.get("side") or "").lower()
    if market in {"total", "corner_total"}:
        return {"over", "under"}, str(pick.get("side") or "").lower()
    if market == "btts":
        return {"yes", "no"}, str(pick.get("side") or "").lower()
    if market == "htft":
        outcomes = {a + b for a in "HDA" for b in "HDA"}
        return outcomes, str(pick.get("selection") or "").upper()
    if market == "half_time":
        submarket = pick.get("market")
        if submarket == "1x2":
            return {"home", "draw", "away"}, str(pick.get("side") or "").lower()
        if submarket == "asian":
            return {"home", "away"}, str(pick.get("side") or "").lower()
        if submarket == "total":
            return {"over", "under"}, str(pick.get("side") or "").lower()
    if market == "goal_range":
        odds = pick.get("complete_market_odds")
        labels = set(odds) if isinstance(odds, dict) else set()
        validate_goal_range_outcome_labels(labels)
        return labels, str(pick.get("selection") or "").lower()
    raise ValueError(f"Cannot determine complete market outcomes for {market}")


def calculate_complete_market_no_vig(
    pick: dict[str, Any], market: str
) -> tuple[float, dict[str, float]]:
    odds = pick.get("complete_market_odds")
    if not isinstance(odds, dict):
        raise ValueError(f"{market} formal pick requires complete_market_odds")
    required, selected = required_market_outcomes(pick, market)
    if set(odds) != required:
        missing = sorted(required - set(odds))
        extra = sorted(set(odds) - required)
        raise ValueError(
            f"{market} complete market odds outcomes mismatch; missing={missing}, extra={extra}"
        )
    raw_implied: dict[str, float] = {}
    for outcome, raw_price in odds.items():
        price = float(raw_price)
        if not math.isfinite(price):
            raise ValueError(f"{market} market odds for {outcome} must be finite")
        if pick["odds_format"] == "decimal":
            if price <= 1.0:
                raise ValueError(f"{market} decimal market odds must be greater than 1")
            raw_implied[outcome] = 1.0 / price
        else:
            if price <= 0.0:
                raise ValueError(f"{market} Hong Kong market odds must be positive")
            raw_implied[outcome] = 1.0 / (1.0 + price)
    if selected not in odds:
        raise ValueError(
            f"{market} selected outcome is missing from complete market odds"
        )
    if abs(float(odds[selected]) - float(pick["odds"])) > ODDS_AUDIT_TOLERANCE:
        raise ValueError(
            f"{market} selected complete-market price must match executable pick.odds"
        )
    total_implied = sum(raw_implied.values())
    if not math.isfinite(total_implied) or total_implied <= 0.0:
        raise ValueError(f"{market} complete market implied probabilities are invalid")
    no_vig = {
        outcome: probability / total_implied
        for outcome, probability in raw_implied.items()
    }
    supplied_complete = pick.get("complete_market_probabilities")
    if isinstance(supplied_complete, dict) and supplied_complete:
        if set(supplied_complete) != set(no_vig):
            raise ValueError(
                f"{market} supplied complete no-vig probabilities are incomplete"
            )
        for outcome, expected in no_vig.items():
            if (
                abs(float(supplied_complete[outcome]) - expected)
                > PROBABILITY_AUDIT_TOLERANCE
            ):
                raise ValueError(
                    f"{market} supplied no-vig probability for {outcome} does not match market odds"
                )
    pick["raw_implied_probabilities"] = {
        key: round(value, 12) for key, value in raw_implied.items()
    }
    pick["complete_market_probabilities"] = {
        key: round(value, 12) for key, value in no_vig.items()
    }
    return no_vig[selected], no_vig


def validate_market_audit_fields(pick: dict[str, Any], market: str) -> float | None:
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

    if pick.get("edge_pp") is None:
        raise ValueError(f"{market} edge_pp is required for an auditable formal pick")
    calculated_market_probability, _ = calculate_complete_market_no_vig(pick, market)
    if (
        abs(float(market_probability) - calculated_market_probability)
        > PROBABILITY_AUDIT_TOLERANCE
    ):
        raise ValueError(
            f"{market} market_probability does not match server-calculated no-vig probability"
        )
    pick["market_probability"] = round(calculated_market_probability, 12)
    if isinstance(pick.get("settlement_probabilities"), dict):
        # Split-settlement markets are checked after their five-state
        # distribution has been validated, so half wins/losses are weighted by
        # half a stake instead of being treated as binary outcomes.
        return None
    expected_edge = (float(pick["probability"]) - calculated_market_probability) * 100.0
    if abs(float(pick["edge_pp"]) - expected_edge) > EDGE_AUDIT_TOLERANCE_PP:
        raise ValueError(
            f"{market} edge_pp must equal (probability - market_probability) * 100"
        )
    require_strictly_positive(
        expected_edge,
        f"{market} recalculated model-versus-market edge (pp)",
    )
    pick["edge_pp"] = round(expected_edge, 12)
    return expected_edge


def validate_five_state_market_edge(pick: dict[str, Any], market: str) -> float:
    edge_probability = effective_settlement_win_probability(
        pick.get("settlement_probabilities"),
        f"{market} settlement probabilities",
    )
    if edge_probability is None:
        raise ValueError(
            f"{market} five-state edge is unavailable when every outcome is a push"
        )
    expected_edge = (edge_probability - float(pick["market_probability"])) * 100.0
    if abs(float(pick["edge_pp"]) - expected_edge) > EDGE_AUDIT_TOLERANCE_PP:
        raise ValueError(
            f"{market} edge_pp must use the stake-weighted five-state win probability"
        )
    require_strictly_positive(
        expected_edge,
        f"{market} recalculated model-versus-market edge (pp)",
    )
    pick["edge_probability"] = round(edge_probability, 12)
    pick["edge_basis"] = (
        "five_state_effective_win_probability_vs_complete_market_no_vig"
    )
    pick["edge_pp"] = round(expected_edge, 12)
    return expected_edge


def validate_binary_ev(pick: dict[str, Any], market: str) -> float:
    probability = float(pick["probability"])
    calculated = probability * (1.0 + audited_win_profit(pick)) - 1.0
    if abs(float(pick["ev"]) - calculated) > EV_AUDIT_TOLERANCE:
        raise ValueError(
            f"{market} EV does not match probability and {pick['odds_format']} odds"
        )
    pick["ev"] = round(calculated, 12)
    return calculated


def validate_settlement_distribution(
    pick: dict[str, Any],
    market: str,
    *,
    line: float | None,
) -> float:
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
    if line is None:
        allowed_states = {"full_win", "loss"}
    else:
        split_line(float(line))
        quarter_units = abs(int(round(float(line) * 4))) % 4
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
        line_label = "binary" if line is None else f"line {float(line):g}"
        raise ValueError(
            f"{market} {line_label} cannot produce settlement states: "
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
    pick["ev"] = round(calculated_ev, 12)
    return calculated_ev


def validate_corner_distribution(pick: dict[str, Any], market: str) -> float:
    """Compatibility wrapper for existing callers and tests."""
    return validate_settlement_distribution(
        pick,
        market,
        line=float(pick["line"]),
    )


def validate_complete_market_audit(
    pick: dict[str, Any],
    market: str,
) -> None:
    if pick.get("market_complete") is not True:
        raise ValueError(f"{market} formal pick requires explicit market_complete=true")
    validate_odds_format(pick, market)
    validate_market_audit_fields(pick, market)
    firm_count = pick.get("firm_count")
    if (
        firm_count is None
        or not math.isfinite(float(firm_count))
        or int(float(firm_count)) != float(firm_count)
        or int(float(firm_count)) < 1
    ):
        raise ValueError(f"{market} firm_count must be a positive integer")


def validate_core_formal_pick(
    market: str,
    pick: dict[str, Any],
) -> None:
    validate_complete_market_audit(pick, market)
    if market == "asian":
        if pick.get("side") not in {"home", "away"} or pick.get("line") is None:
            raise ValueError(
                "Asian formal pick requires side home/away and a current line"
            )
        calculated = validate_settlement_distribution(
            pick, market, line=float(pick["line"])
        )
    elif market == "total":
        if pick.get("side") not in {"over", "under"} or pick.get("line") is None:
            raise ValueError(
                "Total formal pick requires side over/under and a current line"
            )
        calculated = validate_settlement_distribution(
            pick, market, line=float(pick["line"])
        )
    elif market == "half_time":
        submarket = pick.get("market")
        if submarket == "1x2":
            if pick.get("side") not in {"home", "draw", "away"}:
                raise ValueError("Half-time 1X2 requires side home, draw, or away")
            calculated = validate_settlement_distribution(pick, market, line=None)
        elif submarket == "asian":
            if pick.get("side") not in {"home", "away"} or pick.get("line") is None:
                raise ValueError("Half-time Asian requires side home/away and a line")
            calculated = validate_settlement_distribution(
                pick, market, line=float(pick["line"])
            )
        elif submarket == "total":
            if pick.get("side") not in {"over", "under"} or pick.get("line") is None:
                raise ValueError("Half-time total requires side over/under and a line")
            calculated = validate_settlement_distribution(
                pick, market, line=float(pick["line"])
            )
        else:
            raise ValueError(
                "Half-time formal pick requires a complete 1x2/asian/total market"
            )
    else:
        raise ValueError(f"Unsupported core formal market: {market}")
    validate_five_state_market_edge(pick, market)
    require_strictly_positive(calculated, f"{market} recalculated EV")


def validate_htft_formal_pick(pick: dict[str, Any]) -> None:
    market = "htft"
    validate_complete_market_audit(pick, market)
    complete_probabilities = pick.get("complete_market_probabilities")
    selections = {a + b for a in "HDA" for b in "HDA"}
    if (
        not isinstance(complete_probabilities, dict)
        or set(complete_probabilities) != selections
    ):
        raise ValueError(
            "HT/FT formal picks require all nine no-vig market probabilities"
        )
    values = []
    for selection in sorted(selections):
        value = complete_probabilities.get(selection)
        if (
            value is None
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
        ):
            raise ValueError(
                f"HT/FT no-vig probability for {selection} must be finite and between 0 and 1"
            )
        values.append(float(value))
    if abs(sum(values) - 1.0) > PROBABILITY_AUDIT_TOLERANCE:
        raise ValueError("HT/FT nine-outcome no-vig probabilities must sum to 1")
    selection = str(pick.get("selection") or "").upper()
    if selection not in selections:
        raise ValueError("HT/FT formal pick requires a valid selection")
    if (
        abs(
            float(pick["market_probability"]) - float(complete_probabilities[selection])
        )
        > PROBABILITY_AUDIT_TOLERANCE
    ):
        raise ValueError(
            "HT/FT selected market_probability must match the complete market"
        )
    calculated = validate_binary_ev(pick, market)
    require_strictly_positive(calculated, "htft recalculated EV")


def validate_new_formal_pick(
    market: str,
    pick: dict[str, Any],
    record: dict[str, Any],
) -> None:
    formal_support = formal_candidate_directional_support(record, pick, market=market)
    if pick.get("market_complete") is not True:
        raise ValueError(f"{market} formal pick requires explicit market_complete=true")
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

    if market in {"goal_range", "btts"} and not formal_support:
        raise ValueError(
            f"{market} formal pick requires exact candidate-directional evidence"
        )
    if market in {"goal_range", "btts"}:
        calculated_ev = validate_binary_ev(pick, market)
        require_strictly_positive(
            calculated_ev,
            f"{market} recalculated EV",
        )
    if market in CORNER_MARKETS:
        calculated_ev = validate_corner_distribution(pick, market)
        validate_five_state_market_edge(pick, market)
        require_strictly_positive(
            calculated_ev,
            f"{market} recalculated EV",
        )
        if not formal_support:
            raise ValueError(
                f"{market} formal pick requires exact candidate-directional corner evidence"
            )
        require_minimum(
            pick.get("firm_count"),
            PROVISIONAL_CORNER_MIN_FIRMS,
            f"{market} bookmaker count",
        )


def validate_provisional_formal_guardrails(record: dict[str, Any]) -> None:
    """Reject formal picks that do not qualify under the active safe-pool policy."""
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
        if market in STRICT_OOS_MARKET_STATUS:
            policy = STRICT_OOS_MARKET_STATUS[market]
            raise ValueError(
                f"{market} is observation_only under {STRICT_OOS_POLICY_VERSION}: "
                f"{policy['paused_reason']}"
            )
        validate_basic_formal_pick(record, market, pick)
        if market in {"asian", "total", "half_time"}:
            validate_core_formal_pick(market, pick)
        elif market == "htft":
            validate_htft_formal_pick(pick)
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
            supported_attack = formal_candidate_directional_support(
                record, pick, market=market
            )
            if not supported_attack:
                raise ValueError(
                    "Total formal pick requires exact candidate-directional evidence; "
                    "availability or price movement alone is insufficient"
                )
        if market == "asian" and float(pick.get("line", 0.0)) <= DEEP_FAVORITE_LINE:
            if data_quality != "high":
                raise ValueError(
                    "Asian favorite -0.75 or deeper requires high data quality"
                )
            directionally_supported = formal_candidate_directional_support(
                record, pick, market=market
            )
            if not directionally_supported:
                raise ValueError(
                    "Asian favorite -0.75 or deeper requires exact candidate-directional evidence"
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
            if cover_probability is None or not 0.0 <= float(cover_probability) <= 1.0:
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
    # A missing settlement distribution must not make the same model
    # probability count once as "safety" and again through EV/edge.
    return 0.5, "neutral_missing_settlement_distribution"


def evidence_coverage(
    record: dict[str, Any], market: str, pick: dict[str, Any]
) -> float:
    support = candidate_directional_fundamental_support(record, pick)
    if market in {"total", "goal_range", "btts", *CORNER_MARKETS}:
        return (
            1.0 if support and support.get("directionally_supported") is True else 0.0
        )
    evidence = record.get("guardrail_evidence", {})
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
        "data_quality": 15.0 * data_factor,
        "market_depth": 10.0 * min(firms / PROVISIONAL_MIN_FIRMS, 1.0),
        "independent_evidence": 10.0 * evidence_factor,
        "market_alignment": 10.0 * alignment_factor,
    }
    return {
        "policy_version": CONFIDENCE_POLICY_VERSION,
        "score": round(sum(points.values()), 4),
        "settlement_safety_probability": round(safety, 6),
        "safety_source": safety_source,
        "ev": round(ev, 6),
        "edge_pp": round(edge, 4),
        "firm_count": int(firms) if firms.is_integer() else firms,
        "market_signal": signal,
        "eligibility_gates": {
            "positive_ev": ev > 0.0,
            "positive_edge": edge > 0.0,
            "ev": round(ev, 6),
            "edge_pp": round(edge, 4),
            "contributes_to_score": False,
        },
        "points": {key: round(value, 4) for key, value in points.items()},
    }


def annotate_confidence_ranking(record: dict[str, Any]) -> None:
    picks = formal_picks(record)
    record["confidence_ranking_version"] = CONFIDENCE_RANKING_VERSION
    record["confidence_policy_version"] = CONFIDENCE_POLICY_VERSION
    if not picks:
        record["primary_selection_basis"] = "no_safe_formal_candidate"
        return

    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for order, (market, pick) in enumerate(picks):
        components = confidence_components(record, market, pick)
        pick["confidence_score"] = components["score"]
        pick["confidence_components"] = components
        pick["confidence_ranking_version"] = CONFIDENCE_RANKING_VERSION
        pick["confidence_policy_version"] = CONFIDENCE_POLICY_VERSION
        ranked.append((order, market, pick))

    ranked.sort(
        key=lambda item: (
            -float(item[2]["confidence_score"]),
            -float(item[2]["confidence_components"]["settlement_safety_probability"]),
            -float(item[2]["confidence_components"]["firm_count"]),
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
            f"Primary pick must be the unique {CONFIDENCE_RANKING_VERSION} confidence rank 1; "
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
        return (
            str(previous.get("selection", "")).upper()
            == str(current.get("selection", "")).upper()
        )
    if previous_market == "goal_range":
        return previous.get("selection") == current.get("selection")
    if previous_market == "half_time" and previous.get("market") != current.get(
        "market"
    ):
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
        return (current.get("side") == "over" and new_value > old_value) or (
            current.get("side") == "under" and new_value < old_value
        )
    if market == "half_time" and current.get("market") in {"asian", "total"}:
        if current.get("market") == "asian":
            return new_value < old_value
        return (current.get("side") == "over" and new_value > old_value) or (
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
        raise ValueError(
            "A lineup-check archive requires an existing initial prediction"
        )
    if previous_identity == current_identity:
        return {
            "status": "maintained",
            "previous": list(previous_identity) if previous_identity else None,
            "current": list(current_identity) if current_identity else None,
            "reason": str(getattr(args, "primary_change_reason", "") or "").strip()
            or None,
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
        raise ValueError(
            "A changed lineup-check primary requires --primary-change-reason"
        )

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
    previous_confidence = getattr(args, "previous_primary_current_confidence", None)
    confidence_improvement = (
        current_confidence - float(previous_confidence)
        if current_confidence is not None and previous_confidence is not None
        else None
    )
    previous_invalidated = bool(getattr(args, "previous_primary_invalidated", False))
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
            or confidence_improvement + 1e-9 < LINEUP_CHANGE_MIN_CONFIDENCE_DELTA
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
            float(previous_confidence) if previous_confidence is not None else None
        ),
        "new_current_confidence": current_confidence,
        "confidence_improvement": confidence_improvement,
        "worse_line": worse_line,
        "decision": decision,
        "guardrail_passed": True,
    }


def pick_identity(
    market: str | None, pick: dict[str, Any] | None
) -> tuple[Any, ...] | None:
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
        return next(
            (
                pick
                for pick in picks
                if str(pick.get("selection", "")).upper() == wanted
            ),
            None,
        )
    if len(picks) == 1:
        return picks[0]
    return None


def apply_primary_role(
    record: dict[str, Any],
    primary_market: str | None,
    htft_selection: str | None = None,
) -> None:
    picks = formal_picks(record)
    for _, pick in picks:
        pick["role"] = "secondary"

    if primary_market in {None, "none"}:
        if picks:
            raise ValueError(
                "--primary-market none is valid only when there are no formal picks"
            )
        record["primary_market"] = None
        record["primary_pick"] = None
        return

    selected = resolve_formal_pick(record, primary_market, htft_selection)
    if selected is None:
        suffix = f" ({htft_selection})" if htft_selection else ""
        raise ValueError(
            f"Primary pick {primary_market}{suffix} is not present among formal picks"
        )
    selected["role"] = "primary"
    snapshot = deepcopy(selected)
    # Half-time picks already use ``market`` for their executable submarket
    # (1x2/asian/total).  Preserve it so formatting and settlement do not
    # mistake a half-time total for a half-time handicap.  Keep the outer
    # market identity in a separate field for every primary snapshot.
    snapshot.setdefault("market", primary_market)
    snapshot["primary_market"] = primary_market
    snapshot["role"] = "primary"
    record["primary_market"] = primary_market
    record["primary_pick"] = snapshot


def active_primary_identity(record: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if not record:
        return None
    market = record.get("primary_market")
    primary = record.get("primary_pick")
    return pick_identity(
        str(market) if market else None, primary if isinstance(primary, dict) else None
    )


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
            if (
                isinstance(pick, dict)
                and str(pick.get("selection", "")).upper() == selection
            ):
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


def _forward_validation_module():
    try:
        from scripts import forward_validation
    except ImportError:  # Direct execution from scripts/.
        import forward_validation  # type: ignore[no-redef]

    return forward_validation


def _forward_market_commitments(ledger_payload: dict[str, Any]) -> list[dict[str, Any]]:
    schema_version = ledger_payload.get("schema_version")
    historical = schema_version == "forward-observations/2.0.0"
    if not historical and schema_version != "forward-observations/3.0.0":
        raise ValueError("Forward validation ledger schema is unsupported")
    expected_binding_schema = (
        forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
        if historical
        else forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
    )
    commitments: list[dict[str, Any]] = []
    for raw in ledger_payload.get("commitments", []):
        if not isinstance(raw, dict) or not isinstance(
            raw.get("prediction_payload"), dict
        ):
            raise ValueError("Forward validation ledger commitment is invalid")
        prediction = raw["prediction_payload"]
        binding = forward_policy.validate_record_binding(
            raw.get("forward_policy_binding")
        )
        if binding is None or binding.get("schema_version") != expected_binding_schema:
            raise ValueError(
                "Forward validation ledger commitment has no compatible policy binding"
            )
        identity = {
            "observation_id": str(prediction.get("observation_id") or ""),
            "prediction_hash": forward_policy._hash_json(prediction),
            "commitment_hash": require_sha256(
                raw.get("commitment_hash"), "forward commitment hash"
            ),
            "binding_hash": require_sha256(
                binding.get("binding_hash"), "forward commitment binding hash"
            ),
        }
        if historical:
            identity["market"] = str(prediction.get("market") or "").lower()
        else:
            identity.update(
                {
                    "family": str(
                        prediction.get("market_identity", {}).get("family")
                        if isinstance(prediction.get("market_identity"), dict)
                        else ""
                    ).lower(),
                    "market_identity_hash": require_sha256(
                        prediction.get("market_identity_hash"),
                        "forward market identity hash",
                    ),
                }
            )
        commitments.append(identity)
    commitments.sort(
        key=(
            (lambda item: (item["market"], item["observation_id"]))
            if historical
            else (
                lambda item: (
                    item["market_identity_hash"],
                    item["observation_id"],
                )
            )
        )
    )
    if not commitments:
        raise ValueError("Forward validation ledger contains no market commitments")
    return commitments


def _validate_previous_forward_ledger_read_only(
    ledger_payload: dict[str, Any],
) -> dict[str, Any]:
    """Structurally replay the defective v2 ledger for quarantine display only.

    This intentionally is not a formal forward-validation entry point.  It proves that an
    already archived record still binds its old hashes, policy, cohort, commitments, and pending
    settlements, while keeping the superseded observation contract out of active writes and
    statistical evaluation.
    """

    if ledger_payload.get("schema_version") != "forward-observations/2.0.0":
        raise ValueError("Historical forward ledger schema is unsupported")
    try:
        policy = forward_policy.validate_policy_manifest(
            ledger_payload.get("policy_manifest") or {}
        )
        cohort = forward_policy.validate_cohort(
            ledger_payload.get("cohort_manifest") or {}
        )
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError("Historical forward ledger policy/cohort is invalid") from exc
    if policy.get("schema_version") != forward_policy.PREVIOUS_POLICY_SCHEMA_VERSION:
        raise ValueError("Historical forward ledger policy schema is incompatible")
    if cohort.get("schema_version") != forward_policy.LEGACY_COHORT_SCHEMA_VERSION:
        raise ValueError("Historical forward ledger cohort schema is incompatible")
    if any(
        ledger_payload.get(field) != expected
        for field, expected in (
            ("cohort_id", cohort.get("cohort_id")),
            ("policy_id", policy.get("policy_id")),
            ("policy_hash", policy.get("policy_hash")),
        )
    ) or any(
        cohort.get(field) != policy.get(field) for field in ("policy_id", "policy_hash")
    ):
        raise ValueError("Historical forward ledger policy/cohort binding is invalid")

    queue = ledger_payload.get("queue_manifest")
    if not isinstance(queue, dict):
        raise ValueError("Historical forward ledger queue is missing")
    queue_without_hash = deepcopy(queue)
    supplied_queue_hash = queue_without_hash.pop("queue_hash", None)
    if (
        queue.get("schema_version") != "forward-eligibility-queue/1.0.0"
        or supplied_queue_hash != forward_policy._hash_json(queue_without_hash)
        or queue.get("cohort_id") != cohort.get("cohort_id")
        or queue.get("policy_id") != policy.get("policy_id")
        or queue.get("policy_hash") != policy.get("policy_hash")
    ):
        raise ValueError("Historical forward ledger queue binding is invalid")
    queue_keys = {
        str(item.get("queue_key") or "")
        for item in queue.get("entries", [])
        if isinstance(item, dict)
    }
    if not queue_keys or "" in queue_keys:
        raise ValueError("Historical forward ledger queue entries are invalid")

    commitments = ledger_payload.get("commitments")
    settlements = ledger_payload.get("settlements")
    if not isinstance(commitments, list) or not commitments:
        raise ValueError("Historical forward ledger commitments are missing")
    if not isinstance(settlements, list):
        raise ValueError("Historical forward ledger settlements are missing")
    rows: list[dict[str, Any]] = []
    commitments_by_observation: dict[str, str] = {}
    committed_queue_keys: set[str] = set()
    for raw in commitments:
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "prediction_payload",
            "forward_policy_binding",
            "commitment_hash",
        }:
            raise ValueError("Historical forward ledger commitment is invalid")
        without_hash = deepcopy(raw)
        supplied_hash = without_hash.pop("commitment_hash", None)
        if (
            raw.get("schema_version") != "forward-observation-commitment/1.0.0"
            or supplied_hash != forward_policy._hash_json(without_hash)
        ):
            raise ValueError("Historical forward ledger commitment hash is invalid")
        prediction = raw.get("prediction_payload")
        if not isinstance(prediction, dict):
            raise ValueError("Historical forward ledger prediction is missing")
        try:
            binding = forward_policy.validate_record_binding(
                raw.get("forward_policy_binding")
            )
        except forward_policy.ForwardPolicyError as exc:
            raise ValueError("Historical forward ledger binding is invalid") from exc
        prediction_hash = forward_policy._hash_json(prediction)
        if (
            binding is None
            or binding.get("schema_version")
            != forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
            or binding.get("observation_commitment_hash") != prediction_hash
            or binding.get("cohort_id") != cohort.get("cohort_id")
            or binding.get("policy_id") != policy.get("policy_id")
            or binding.get("policy_hash") != policy.get("policy_hash")
        ):
            raise ValueError("Historical forward ledger binding does not replay")
        observation_id = str(prediction.get("observation_id") or "")
        queue_key = str(prediction.get("queue_key") or "")
        if (
            not observation_id
            or observation_id in commitments_by_observation
            or queue_key not in queue_keys
            or queue_key in committed_queue_keys
        ):
            raise ValueError("Historical forward ledger identity is invalid")
        commitments_by_observation[observation_id] = str(supplied_hash)
        committed_queue_keys.add(queue_key)
        rows.append(
            {
                "fixture_id": str(prediction.get("fixture_id") or ""),
                "home_team": prediction.get("home_team"),
                "away_team": prediction.get("away_team"),
                "league": prediction.get("league"),
                "market": prediction.get("market"),
                "kickoff": prediction.get("kickoff"),
                "forward_policy_binding": binding,
                "settlement_status": "pending",
                "observed_outcome": None,
            }
        )
    if committed_queue_keys != queue_keys:
        raise ValueError("Historical forward ledger does not cover its frozen queue")

    settled_ids: set[str] = set()
    for settlement in settlements:
        if not isinstance(settlement, dict):
            raise ValueError("Historical forward ledger settlement is invalid")
        without_hash = deepcopy(settlement)
        supplied_hash = without_hash.pop("settlement_hash", None)
        observation_id = str(settlement.get("observation_id") or "")
        if (
            settlement.get("schema_version") != "forward-observation-settlement/1.0.0"
            or supplied_hash != forward_policy._hash_json(without_hash)
            or observation_id not in commitments_by_observation
            or observation_id in settled_ids
            or settlement.get("commitment_hash")
            != commitments_by_observation[observation_id]
            or settlement.get("status") != "pending"
            or settlement.get("observed_outcome") is not None
            or settlement.get("result_collected_at") is not None
            or settlement.get("result_source_evidence_hash") is not None
            or settlement.get("closing_snapshot") is not None
        ):
            raise ValueError("Historical forward ledger settlement does not replay")
        settled_ids.add(observation_id)
    if settled_ids != set(commitments_by_observation):
        raise ValueError("Historical forward ledger settlements are incomplete")
    return {"records": rows, "historical_read_only": True}


def validate_forward_validation_ledger_archive(
    value: Any,
    *,
    record: dict[str, Any] | None = None,
    active_binding: dict[str, Any] | None = None,
    archived_not_after: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Forward validation ledger archive is required")
    archive = deepcopy(value)
    required = {
        "schema_version",
        "fixture_id",
        "ledger_hash",
        "ledger_payload",
        "market_commitments",
        "archive_hash",
    }
    if set(archive) != required:
        raise ValueError("Forward validation ledger archive fields are incomplete")
    archive_schema = archive.get("schema_version")
    if archive_schema not in {
        PREVIOUS_FORWARD_LEDGER_ARCHIVE_SCHEMA_VERSION,
        FORWARD_LEDGER_ARCHIVE_SCHEMA_VERSION,
    }:
        raise ValueError("Forward validation ledger archive schema is unsupported")
    historical = archive_schema == PREVIOUS_FORWARD_LEDGER_ARCHIVE_SCHEMA_VERSION
    if historical and (active_binding is not None or archived_not_after is not None):
        raise ValueError(
            "Historical forward validation ledgers are read-only and cannot enter "
            "an active archive write"
        )
    supplied_archive_hash = archive.pop("archive_hash", None)
    if supplied_archive_hash != forward_policy._hash_json(archive):
        raise ValueError("Forward validation ledger archive hash is invalid")
    ledger_payload = archive.get("ledger_payload")
    if not isinstance(ledger_payload, dict):
        raise ValueError("Forward validation ledger payload is required")
    if archive.get("ledger_hash") != forward_policy._hash_json(ledger_payload):
        raise ValueError("Forward validation ledger content hash is invalid")
    forward_validation = _forward_validation_module()
    try:
        if historical:
            normalized = _validate_previous_forward_ledger_read_only(ledger_payload)
        else:
            normalized = forward_validation.validate_prematch_input(ledger_payload)
    except (forward_validation.ForwardValidationError, ValueError) as exc:
        raise ValueError("Forward validation ledger cannot be replayed") from exc
    expected_market_commitments = _forward_market_commitments(ledger_payload)
    if archive.get("market_commitments") != expected_market_commitments:
        raise ValueError("Forward validation market commitments do not replay")
    fixture_id = str(archive.get("fixture_id") or "")
    rows = normalized.get("records", [])
    if (
        not fixture_id
        or not rows
        or any(str(row.get("fixture_id") or "") != fixture_id for row in rows)
    ):
        raise ValueError("Forward validation ledger must contain exactly one fixture")
    if record is not None:
        if fixture_id != str(record.get("match_id") or ""):
            raise ValueError(
                "Forward validation ledger fixture does not match the record"
            )
        record_kickoff = parse_aware_datetime(
            str(record.get("kickoff") or ""), "record kickoff"
        )
        for row in rows:
            if (
                str(row.get("home_team") or "") != str(record.get("home_team") or "")
                or str(row.get("away_team") or "") != str(record.get("away_team") or "")
                or str(row.get("league") or "") != str(record.get("league") or "")
                or parse_aware_datetime(str(row.get("kickoff") or ""), "ledger kickoff")
                != record_kickoff
            ):
                raise ValueError(
                    "Forward validation ledger fixture metadata does not match"
                )
            if (
                row.get("market") == "1x2"
                and isinstance(row.get("market_identity"), dict)
                and row["market_identity"].get("period") == "full_time"
                and isinstance(row.get("model_probabilities"), dict)
            ):
                expected_probabilities = {
                    "H": record.get("probabilities", {}).get("home_win"),
                    "D": record.get("probabilities", {}).get("draw"),
                    "A": record.get("probabilities", {}).get("away_win"),
                }
                if any(
                    expected_probabilities[outcome] is None
                    or not math.isclose(
                        float(row["model_probabilities"][outcome]),
                        float(expected_probabilities[outcome]),
                        rel_tol=0.0,
                        abs_tol=PROBABILITY_AUDIT_TOLERANCE,
                    )
                    for outcome in ("H", "D", "A")
                ):
                    raise ValueError(
                        "Forward validation 1x2 probabilities do not match the record"
                    )
    validated_active = (
        forward_policy.validate_record_binding(active_binding)
        if active_binding is not None
        else None
    )
    for row in rows:
        binding = row.get("forward_policy_binding")
        if not isinstance(binding, dict):
            raise ValueError("Forward validation ledger row has no committed binding")
        expected_binding_schema = (
            forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
            if historical
            else forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
        )
        if binding.get("schema_version") != expected_binding_schema:
            raise ValueError(
                "Forward validation ledger row binding schema is incompatible"
            )
        if validated_active is not None and any(
            binding.get(field) != validated_active.get(field)
            for field in ("cohort_id", "cohort_hash", "policy_id", "policy_hash")
        ):
            raise ValueError("Forward validation ledger is not in the active cohort")
        if validated_active is not None and binding.get(
            "provenance_binding"
        ) != validated_active.get("provenance_binding"):
            raise ValueError("Forward validation ledger provenance does not match")
        if archived_not_after is not None:
            archived = parse_aware_datetime(
                str(binding.get("archived_at") or ""), "ledger archived_at"
            )
            if archived > archived_not_after.astimezone(timezone.utc):
                raise ValueError("Forward validation ledger was archived in the future")
    archive["archive_hash"] = supplied_archive_hash
    return archive


def load_forward_validation_ledger_archive(
    args: argparse.Namespace,
    record: dict[str, Any],
    active_binding: dict[str, Any] | None,
    *,
    archived_at: datetime,
) -> dict[str, Any] | None:
    supplied = str(getattr(args, "forward_validation_ledger", "") or "").strip()
    if active_binding is None:
        if supplied:
            raise ValueError(
                "--forward-validation-ledger requires an active untouched cohort"
            )
        return None
    if not supplied:
        raise ValueError(
            "Active untouched cohorts require --forward-validation-ledger; "
            "free-form post-hoc commitments are forbidden"
        )
    _path, _raw, payload = load_observation_json(
        args, supplied, "forward validation ledger"
    )
    archive: dict[str, Any] = {
        "schema_version": FORWARD_LEDGER_ARCHIVE_SCHEMA_VERSION,
        "fixture_id": str(record.get("match_id") or ""),
        "ledger_hash": forward_policy._hash_json(payload),
        "ledger_payload": payload,
        "market_commitments": _forward_market_commitments(payload),
    }
    archive["archive_hash"] = forward_policy._hash_json(archive)
    return validate_forward_validation_ledger_archive(
        archive,
        record=record,
        active_binding=active_binding,
        archived_not_after=archived_at,
    )


def _canonical_candidate_categorical_probabilities(
    record: dict[str, Any], market_identity: dict[str, Any]
) -> dict[str, float]:
    snapshot = validated_joint_scenario_audit(record)
    if not isinstance(snapshot, dict):
        raise ValueError(
            "Categorical forward candidates require the frozen canonical joint model"
        )
    family = str(market_identity.get("family") or "")
    period = str(market_identity.get("period") or "")
    outcomes = [str(item) for item in market_identity.get("price_outcomes", [])]
    if family == "htft":
        raw = snapshot.get("htft_marginal", {}).get("code_probabilities")
        if not isinstance(raw, dict) or set(raw) != set(outcomes):
            raise ValueError(
                "Categorical HT/FT outcome space does not match the joint model"
            )
        return {outcome: float(raw[outcome]) for outcome in outcomes}

    matrix_key = (
        "half_time_score_marginal"
        if period == "first_half"
        else "full_time_score_marginal"
    )
    matrix_block = snapshot.get(matrix_key)
    if not isinstance(matrix_block, dict):
        raise ValueError("Categorical candidate score marginal is unavailable")
    matrix = validate_probability_matrix(
        matrix_block.get("probabilities"), "categorical candidate score marginal"
    )
    probabilities = {outcome: 0.0 for outcome in outcomes}
    if family == "1x2" and set(outcomes) == {"H", "D", "A"}:
        for home, row in enumerate(matrix):
            for away, probability in enumerate(row):
                outcome = "H" if home > away else "D" if home == away else "A"
                probabilities[outcome] += float(probability)
    elif family == "goal_range":
        parsed = {outcome: parse_goal_range_selection(outcome) for outcome in outcomes}
        for home, row in enumerate(matrix):
            for away, probability in enumerate(row):
                winners = [
                    outcome
                    for outcome, pick in parsed.items()
                    if settle_goal_range(pick, home, away) == "win"
                ]
                if len(winners) != 1:
                    raise ValueError(
                        "Categorical goal-range outcomes do not form an exact partition"
                    )
                probabilities[winners[0]] += float(probability)
    elif family == "btts" and {item.lower() for item in outcomes} == {"yes", "no"}:
        lookup = {outcome.lower(): outcome for outcome in outcomes}
        for home, row in enumerate(matrix):
            for away, probability in enumerate(row):
                outcome = lookup["yes" if home > 0 and away > 0 else "no"]
                probabilities[outcome] += float(probability)
    else:
        raise ValueError(
            "Candidate-bound categorical semantics are unsupported for this market identity"
        )
    total = math.fsum(probabilities.values())
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=PROBABILITY_AUDIT_TOLERANCE):
        raise ValueError("Categorical candidate probabilities do not sum to one")
    return probabilities


def _candidate_market_for_source_identity(
    market_identity: dict[str, Any],
) -> str | None:
    family = str(market_identity.get("family") or "")
    period = str(market_identity.get("period") or "")
    if period == "first_half" and family in {"1x2", "asian", "total"}:
        return "half_time"
    if period == "full_time" and family in {
        "asian",
        "total",
        "htft",
        "goal_range",
        "btts",
        "corner_total",
        "corner_handicap",
    }:
        return family
    return None


def _corner_identity_has_canonical_model(
    record: dict[str, Any], market_identity: dict[str, Any]
) -> bool:
    """Return whether every quoted corner side has a replayable model candidate."""

    market = _candidate_market_for_source_identity(market_identity)
    if market not in CORNER_MARKETS:
        return False
    raw_line = market_identity.get("line")
    if raw_line is None:
        return False
    for outcome in market_identity.get("price_outcomes", []):
        side = str(outcome)
        line = float(raw_line)
        if market == "corner_handicap" and side == "away":
            line = -line
        if (
            _matching_corner_candidate(
                record, {"market": market, "side": side, "line": line}
            )
            is None
        ):
            return False
    return True


def _verified_source_identity_unavailable_reasons(
    record: dict[str, Any],
    market_identity: dict[str, Any],
    snapshots: list[dict[str, Any]],
    prediction: dict[str, Any],
    *,
    same_time_tolerance_minutes: float,
) -> list[str]:
    """Derive, rather than trust, why a source-visible identity is unavailable."""

    market = _candidate_market_for_source_identity(market_identity)
    if market in FOOTBALL_MODEL_MARKETS:
        model_available = validated_joint_scenario_audit(record) is not None
    elif market in CORNER_MARKETS:
        model_available = _corner_identity_has_canonical_model(record, market_identity)
    else:
        model_available = False
    if not model_available:
        if market in CORNER_MARKETS:
            return [ACTIVE_UNAVAILABLE_REASON_CORNER_MODEL_MISSING]
        raise ValueError(
            "Source-visible football identity has no replayable canonical model"
        )

    decimal_snapshots = [
        snapshot
        for snapshot in snapshots
        if isinstance(snapshot, dict) and snapshot.get("odds_format") == "decimal"
    ]
    if not decimal_snapshots:
        return [ACTIVE_UNAVAILABLE_REASON_DECIMAL_PRICE_MISSING]

    prediction_generated_at = parse_aware_datetime(
        str(prediction.get("generated_at") or ""),
        "source-visible unavailable prediction generated_at",
    ).astimezone(timezone.utc)
    same_time_snapshots = []
    for snapshot in decimal_snapshots:
        collected_at = parse_aware_datetime(
            str(snapshot.get("collected_at") or ""),
            "source-visible market collected_at",
        ).astimezone(timezone.utc)
        if (
            abs((collected_at - prediction_generated_at).total_seconds()) / 60.0
            <= same_time_tolerance_minutes
        ):
            same_time_snapshots.append(snapshot)
    if not same_time_snapshots:
        return [ACTIVE_UNAVAILABLE_REASON_DECISION_TIME_PRICE_MISSING]
    return []


def _candidate_matches_formal_pick(
    candidate: dict[str, Any], market: str, pick: dict[str, Any]
) -> bool:
    if candidate.get("market") != market:
        return False
    if market == "htft":
        return (
            str(candidate.get("selection") or "").upper()
            == str(pick.get("selection") or "").upper()
        )
    if market == "goal_range":
        return candidate.get("selection") == pick.get("selection")
    if market == "half_time":
        if candidate.get("submarket") != pick.get("market") or candidate.get(
            "side"
        ) != pick.get("side"):
            return False
    elif candidate.get("side") != pick.get("side"):
        return False
    candidate_line = candidate.get("line")
    pick_line = pick.get("line")
    if candidate_line is None or pick_line is None:
        return candidate_line is pick_line
    return math.isclose(
        float(candidate_line), float(pick_line), rel_tol=0.0, abs_tol=1e-12
    )


def _same_numeric_mapping(
    left: Any, right: Any, *, tolerance: float = PROBABILITY_AUDIT_TOLERANCE
) -> bool:
    if (
        not isinstance(left, dict)
        or not isinstance(right, dict)
        or set(left) != set(right)
    ):
        return False
    try:
        return all(
            math.isclose(
                float(left[key]),
                float(right[key]),
                rel_tol=0.0,
                abs_tol=tolerance,
            )
            for key in left
        )
    except (TypeError, ValueError):
        return False


def _validate_active_formal_pick_binding(
    record: dict[str, Any],
    audit: dict[str, Any],
    predictions_by_hash: dict[str, dict[str, Any]],
) -> None:
    """Keep every official pick on the exact candidate/source/ledger chain."""

    candidates = [
        candidate
        for candidate in audit.get("candidates", [])
        if isinstance(candidate, dict)
    ]
    bound_candidate_ids: set[str] = set()
    for market, pick in formal_picks(record):
        matches = [
            candidate
            for candidate in candidates
            if _candidate_matches_formal_pick(candidate, market, pick)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Active formal {market} pick must match exactly one evaluated candidate"
            )
        candidate = matches[0]
        if (
            candidate.get("formal_eligible") is not True
            or candidate.get("shadow_selected") is not True
        ):
            raise ValueError(
                f"Active formal {market} pick is not the frozen formal-eligible selection"
            )
        bound_candidate_ids.add(str(candidate.get("candidate_id") or ""))
        identity_hash = require_sha256(
            candidate.get("market_identity_hash"),
            f"active formal {market} candidate identity hash",
        )
        prediction = predictions_by_hash.get(identity_hash)
        execution = (
            prediction.get("execution_entry") if isinstance(prediction, dict) else None
        )
        if (
            not isinstance(prediction, dict)
            or prediction.get("status") != "predicted"
            or not isinstance(execution, dict)
            or execution.get("selection")
            != candidate.get("settlement_reference_outcome")
        ):
            raise ValueError(
                f"Active formal {market} pick does not match the predicted ledger selection"
            )

        source_binding = candidate.get("source_evidence_binding")
        if not isinstance(source_binding, dict):
            raise ValueError(f"Active formal {market} pick has no source binding")
        if (
            pick.get("odds_format") != source_binding.get("odds_format")
            or pick.get("price_basis") != source_binding.get("price_basis")
            or str(pick.get("market_source") or "")
            != str(source_binding.get("source_url") or "")
            or parse_aware_datetime(
                str(pick.get("market_collected_at") or ""),
                f"active formal {market} market_collected_at",
            ).astimezone(timezone.utc)
            != parse_aware_datetime(
                str(source_binding.get("collected_at") or ""),
                f"active formal {market} source collected_at",
            ).astimezone(timezone.utc)
            or int(float(pick.get("firm_count")))
            != int(source_binding.get("firm_count"))
            or not _same_numeric_mapping(
                pick.get("complete_market_odds"),
                source_binding.get("prices"),
                tolerance=5e-7,
            )
            or not math.isclose(
                float(pick.get("odds")),
                float(candidate.get("odds")),
                rel_tol=0.0,
                abs_tol=ODDS_AUDIT_TOLERANCE,
            )
            or not math.isclose(
                float(pick.get("probability")),
                float(candidate.get("probability")),
                rel_tol=0.0,
                abs_tol=PROBABILITY_AUDIT_TOLERANCE,
            )
            or not math.isclose(
                float(pick.get("ev")),
                float(candidate.get("ev")),
                rel_tol=0.0,
                abs_tol=EV_AUDIT_TOLERANCE,
            )
            or not math.isclose(
                float(pick.get("edge_pp")),
                float(candidate.get("edge_pp")),
                rel_tol=0.0,
                abs_tol=EDGE_AUDIT_TOLERANCE_PP,
            )
            or pick.get("market_signal") != candidate.get("market_signal")
            or pick.get("confidence_components") != candidate.get("shadow_confidence")
            or not math.isclose(
                float(pick.get("confidence_score")),
                float((candidate.get("shadow_confidence") or {}).get("score")),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise ValueError(
                f"Active formal {market} pick does not match its frozen candidate source/price/model"
            )
        candidate_distribution = candidate.get("settlement_probabilities")
        pick_distribution = pick.get("settlement_probabilities")
        if isinstance(pick_distribution, dict) and not _same_numeric_mapping(
            pick_distribution, candidate_distribution
        ):
            raise ValueError(
                f"Active formal {market} pick settlement model does not match its candidate"
            )

    expected_candidate_ids = {
        str(candidate.get("candidate_id") or "")
        for candidate in candidates
        if candidate.get("formal_eligible") is True
        and candidate.get("shadow_selected") is True
    }
    if bound_candidate_ids != expected_candidate_ids:
        missing = sorted(expected_candidate_ids - bound_candidate_ids)
        extra = sorted(bound_candidate_ids - expected_candidate_ids)
        raise ValueError(
            "Frozen formal-eligible selected candidates and official formal picks must "
            f"match exactly (missing={missing}, extra={extra})"
        )


def validate_active_candidate_evaluation_ledger_binding(
    record: dict[str, Any], ledger_archive: dict[str, Any]
) -> dict[str, Any]:
    """Bind the complete candidate denominator to the frozen forward queue.

    ``candidate-evaluation/3`` records the eight family-level denominator, including
    families for which no concrete line was available.  The forward queue, by contrast,
    can only contain concrete canonical market identities.  An active record therefore
    needs exactly one current candidate audit; every evaluated concrete identity must be
    present in the queue exactly once, while unavailable families remain explicitly
    represented by the committed candidate manifest rather than by an invented line.
    """

    audits = [
        item
        for item in record.get("candidate_audits", [])
        if isinstance(item, dict) and item.get("kind") == CANDIDATE_EVALUATION_KIND
    ]
    if len(audits) != 1:
        raise ValueError(
            "Active untouched cohorts require exactly one current candidate evaluation audit"
        )
    audit = audits[0]
    if (
        audit.get("schema_version") != CANDIDATE_EVALUATION_SCHEMA_VERSION
        or not validated_candidate_evaluation_audit(audit, record)
    ):
        raise ValueError(
            "Active untouched cohort candidate evaluation audit is invalid or historical"
        )

    manifest = audit.get("market_manifest")
    if not isinstance(manifest, list) or {
        str(item.get("market") or "") for item in manifest if isinstance(item, dict)
    } != set(PRIMARY_MARKETS):
        raise ValueError(
            "Active untouched cohort candidate evaluation must cover all eight markets"
        )

    payload = ledger_archive.get("ledger_payload")
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "forward-observations/3.0.0"
    ):
        raise ValueError(
            "Active candidate evaluations require a current forward-observations/3.0.0 ledger"
        )
    queue = payload.get("queue_manifest")
    raw_entries = queue.get("entries") if isinstance(queue, dict) else None
    raw_commitments = payload.get("commitments")
    if not isinstance(raw_entries, list) or not isinstance(raw_commitments, list):
        raise ValueError("Active forward candidate queue or commitments are missing")
    candidate_generated_at = parse_aware_datetime(
        str(audit.get("artifact", {}).get("generated_at") or ""),
        "candidate evaluation generated_at",
    )
    queue_frozen_at = parse_aware_datetime(
        str(queue.get("frozen_at") or ""), "candidate evaluation queue frozen_at"
    )
    if queue_frozen_at.astimezone(timezone.utc) > candidate_generated_at.astimezone(
        timezone.utc
    ):
        raise ValueError(
            "Candidate evaluation cannot precede its frozen eligibility queue"
        )

    candidate_groups: dict[str, list[dict[str, Any]]] = {}
    for candidate in audit.get("candidates", []):
        if not isinstance(candidate, dict):
            raise ValueError(
                "Active candidate evaluation contains an invalid candidate"
            )
        identity_hash = require_sha256(
            candidate.get("market_identity_hash"),
            "candidate evaluation market identity hash",
        )
        candidate_groups.setdefault(identity_hash, []).append(candidate)

    representatives: dict[str, dict[str, Any]] = {}
    expected_statuses: dict[str, str] = {}
    for identity_hash, candidates in candidate_groups.items():
        identities = [candidate.get("market_identity") for candidate in candidates]
        if any(identity != identities[0] for identity in identities[1:]):
            raise ValueError(
                "Candidate selections sharing an identity hash disagree on canonical identity"
            )
        selected = [
            candidate
            for candidate in candidates
            if candidate.get("shadow_selected") is True
        ]
        if len(selected) > 1:
            raise ValueError(
                "One exact market identity cannot have multiple predicted dispositions"
            )
        representatives[identity_hash] = (
            selected[0]
            if selected
            else min(candidates, key=lambda item: str(item.get("candidate_id") or ""))
        )
        expected_statuses[identity_hash] = "predicted" if selected else "abstained"

    source_audit = validated_source_evidence_audit(record)
    source_bundle = (
        source_audit.get("bundle") if isinstance(source_audit, dict) else None
    )
    market_index = (
        source_bundle.get("market_index") if isinstance(source_bundle, dict) else None
    )
    if not isinstance(market_index, dict):
        raise ValueError(
            "Active candidate denominator requires replayable canonical source evidence"
        )
    source_bundle_generated_at = parse_aware_datetime(
        str(source_bundle.get("generated_at") or ""),
        "active source evidence generated_at",
    )
    if source_bundle_generated_at.astimezone(
        timezone.utc
    ) > candidate_generated_at.astimezone(timezone.utc):
        raise ValueError(
            "Candidate evaluation cannot bind source evidence collected after its "
            "generated_at"
        )
    source_identities: dict[str, dict[str, Any]] = {}
    source_hashes_by_candidate_market: dict[str, set[str]] = {
        market: set() for market in PRIMARY_MARKETS
    }
    for identity_hash, snapshots in market_index.items():
        if not isinstance(snapshots, list) or not snapshots:
            raise ValueError("Active source evidence market index is invalid")
        identity = snapshots[0].get("market_identity")
        if not isinstance(identity, dict) or any(
            not isinstance(snapshot, dict)
            or snapshot.get("market_identity") != identity
            or snapshot.get("market_identity_hash") != identity_hash
            for snapshot in snapshots
        ):
            raise ValueError("Active source evidence market identity does not replay")
        candidate_market = _candidate_market_for_source_identity(identity)
        if candidate_market is None:
            continue
        source_identities[str(identity_hash)] = identity
        source_hashes_by_candidate_market[candidate_market].add(str(identity_hash))

    manifest_by_market = {
        str(item["market"]): item for item in manifest if isinstance(item, dict)
    }
    predictions_by_hash: dict[str, dict[str, Any]] = {}
    for raw_commitment in raw_commitments:
        prediction = (
            raw_commitment.get("prediction_payload")
            if isinstance(raw_commitment, dict)
            else None
        )
        if not isinstance(prediction, dict):
            raise ValueError("Active forward commitment has no prediction payload")
        identity_hash = require_sha256(
            prediction.get("market_identity_hash"),
            "forward commitment market identity hash",
        )
        if identity_hash in predictions_by_hash:
            raise ValueError(
                "Active forward commitments duplicate an exact market identity"
            )
        predictions_by_hash[identity_hash] = prediction

    raw_protocol = (
        payload.get("policy_manifest", {})
        .get("policy", {})
        .get("validation_protocol", {})
    )
    try:
        same_time_tolerance_minutes = float(raw_protocol["same_time_tolerance_minutes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "Active forward policy has no frozen same-time price tolerance"
        ) from exc
    unavailable_source_hashes: set[str] = set()
    for market in PRIMARY_MARKETS:
        source_hashes = source_hashes_by_candidate_market[market]
        candidate_hashes_for_market = {
            identity_hash
            for identity_hash, candidates in candidate_groups.items()
            if any(candidate.get("market") == market for candidate in candidates)
        }
        if manifest_by_market[market].get("status") == "evaluated":
            if source_hashes != candidate_hashes_for_market:
                missing = sorted(source_hashes - candidate_hashes_for_market)
                extra = sorted(candidate_hashes_for_market - source_hashes)
                raise ValueError(
                    "Candidate evaluation does not exactly cover source-visible identities "
                    f"for {market} (missing={missing}, extra={extra})"
                )
            for identity_hash in source_hashes:
                expected_outcomes = set(
                    source_identities[identity_hash].get("price_outcomes", [])
                )
                candidate_outcomes = {
                    str(candidate.get("settlement_reference_outcome") or "")
                    for candidate in candidate_groups[identity_hash]
                }
                if candidate_outcomes != expected_outcomes or len(
                    candidate_groups[identity_hash]
                ) != len(expected_outcomes):
                    missing = sorted(expected_outcomes - candidate_outcomes)
                    extra = sorted(candidate_outcomes - expected_outcomes)
                    raise ValueError(
                        "Candidate evaluation does not cover every quoted price outcome "
                        f"for {identity_hash} (missing={missing}, extra={extra})"
                    )
        else:
            if candidate_hashes_for_market:
                raise ValueError(
                    f"Unavailable candidate market {market} cannot contain candidates"
                )
            if not source_hashes:
                expected_reasons = [ACTIVE_UNAVAILABLE_REASON_SOURCE_MISSING]
            else:
                derived_reasons: set[str] = set()
                for identity_hash in source_hashes:
                    prediction = predictions_by_hash.get(identity_hash)
                    if not isinstance(prediction, dict):
                        prediction = {
                            "generated_at": candidate_generated_at.isoformat()
                        }
                    reasons = _verified_source_identity_unavailable_reasons(
                        record,
                        source_identities[identity_hash],
                        list(market_index[identity_hash]),
                        prediction,
                        same_time_tolerance_minutes=same_time_tolerance_minutes,
                    )
                    if not reasons:
                        raise ValueError(
                            "Source-visible market with a replayable canonical model and "
                            "decision-time price must be evaluated"
                        )
                    derived_reasons.update(reasons)
                expected_reasons = sorted(derived_reasons)
            if manifest_by_market[market].get("reasons") != expected_reasons:
                raise ValueError(
                    f"Unavailable candidate market {market} reasons do not match "
                    "verified source/model conditions"
                )
            unavailable_source_hashes.update(source_hashes)

    entries_by_hash: dict[str, dict[str, Any]] = {}
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("Active forward queue contains an invalid entry")
        identity_hash = require_sha256(
            raw_entry.get("market_identity_hash"), "forward queue market identity hash"
        )
        if identity_hash in entries_by_hash:
            raise ValueError("Active forward queue duplicates an exact market identity")
        entries_by_hash[identity_hash] = raw_entry

    candidate_hashes = set(candidate_groups)
    queue_hashes = set(entries_by_hash)
    all_manifest_unavailable = not candidate_hashes and all(
        isinstance(item, dict) and item.get("status") == "unavailable"
        for item in manifest
    )
    sentinel_identity = {
        "family": "1x2",
        "period": "full_time",
        "line": None,
        "price_outcomes": ["H", "D", "A"],
    }
    sentinel_hash = source_evidence.market_identity_hash(sentinel_identity)
    use_sentinel = all_manifest_unavailable and not unavailable_source_hashes
    expected_queue_hashes = candidate_hashes | unavailable_source_hashes
    if use_sentinel:
        if queue_hashes != {sentinel_hash}:
            raise ValueError(
                "An all-unavailable candidate manifest requires exactly one canonical "
                "fixture-denominator sentinel"
            )
    elif expected_queue_hashes != queue_hashes:
        missing = sorted(expected_queue_hashes - queue_hashes)
        extra = sorted(queue_hashes - expected_queue_hashes)
        raise ValueError(
            "Active forward queue does not exactly cover evaluated and unavailable "
            "source-visible identities "
            f"(missing={missing}, extra={extra})"
        )

    expected_prediction_hashes = (
        {sentinel_hash} if use_sentinel else expected_queue_hashes
    )
    if set(predictions_by_hash) != expected_prediction_hashes:
        raise ValueError(
            "Active forward commitments do not exactly cover evaluated candidate identities"
        )

    if use_sentinel:
        sentinel_entry = entries_by_hash[sentinel_hash]
        sentinel_prediction = predictions_by_hash[sentinel_hash]
        source_rows = (
            source_bundle.get("sources") if isinstance(source_bundle, dict) else None
        )
        if (
            not isinstance(source_bundle, dict)
            or source_bundle.get("market_index") != {}
            or not isinstance(source_rows, list)
            or not source_rows
            or any(
                not isinstance(source_row, dict)
                or not isinstance(source_row.get("parsed"), dict)
                or source_row["parsed"].get("availability_status") != "unavailable"
                for source_row in source_rows
            )
        ):
            raise ValueError(
                "All-unavailable candidate manifests require replayable source evidence "
                "with no available market snapshots"
            )
        sentinel_generated_at = parse_aware_datetime(
            str(sentinel_prediction.get("generated_at") or ""),
            "fixture denominator sentinel generated_at",
        )
        expected_reasons = [
            f"{item['market']}:{reason}"
            for item in manifest
            if isinstance(item, dict)
            for reason in item.get("reasons", [])
        ]
        if (
            sentinel_entry.get("market_identity") != sentinel_identity
            or sentinel_prediction.get("market_identity") != sentinel_identity
            or sentinel_prediction.get("status") != "unavailable"
            or sentinel_prediction.get("settlement_reference_outcome") is not None
            or sentinel_prediction.get("unavailable_reasons") != expected_reasons
            or candidate_generated_at.astimezone(timezone.utc)
            > sentinel_generated_at.astimezone(timezone.utc)
        ):
            raise ValueError(
                "All-unavailable fixture denominator sentinel does not bind the eight-market manifest"
            )
        _validate_active_formal_pick_binding(record, audit, predictions_by_hash)
        return {
            "candidate_evaluation_audit_hash": audit["audit_hash"],
            "evaluated_identity_count": 0,
            "evaluated_market_count": 0,
            "unavailable_market_count": len(PRIMARY_MARKETS),
            "fixture_denominator_sentinel": sentinel_hash,
        }

    for identity_hash in unavailable_source_hashes:
        identity = source_identities[identity_hash]
        entry = entries_by_hash[identity_hash]
        prediction = predictions_by_hash[identity_hash]
        candidate_market = _candidate_market_for_source_identity(identity)
        manifest_entry = manifest_by_market.get(str(candidate_market))
        prediction_generated_at = parse_aware_datetime(
            str(prediction.get("generated_at") or ""),
            "unavailable forward identity generated_at",
        )
        if (
            entry.get("market_identity") != identity
            or prediction.get("market_identity") != identity
            or prediction.get("status") != "unavailable"
            or prediction.get("settlement_reference_outcome") is not None
            or not isinstance(manifest_entry, dict)
            or manifest_entry.get("status") != "unavailable"
            or prediction.get("unavailable_reasons") != manifest_entry.get("reasons")
            or candidate_generated_at.astimezone(timezone.utc)
            > prediction_generated_at.astimezone(timezone.utc)
        ):
            raise ValueError(
                "Unavailable forward identity does not bind its source-visible market manifest"
            )

    market_schemas = payload.get("market_schemas")
    if not isinstance(market_schemas, dict):
        raise ValueError("Active forward market schemas are missing")
    for identity_hash, candidates in candidate_groups.items():
        candidate = representatives[identity_hash]
        entry = entries_by_hash[identity_hash]
        prediction = predictions_by_hash[identity_hash]
        if entry.get("market_identity") != candidate.get(
            "market_identity"
        ) or prediction.get("market_identity") != candidate.get("market_identity"):
            raise ValueError(
                "Active forward queue identity does not match its evaluated candidate"
            )

        expected_status = expected_statuses[identity_hash]
        if prediction.get("status") != expected_status:
            raise ValueError(
                "Active forward commitment status does not match the candidate disposition"
            )
        family = str(candidate.get("market_identity", {}).get("family") or "")
        semantics = market_schemas.get(family, {}).get("settlement_semantics")
        model_probabilities = prediction.get("model_probabilities")
        if not isinstance(model_probabilities, dict):
            raise ValueError("Modeled candidate commitment probabilities are missing")
        if semantics == "five_state_return":
            if prediction.get("settlement_reference_outcome") != candidate.get(
                "settlement_reference_outcome"
            ):
                raise ValueError(
                    "Active forward commitment selection does not match the evaluated candidate"
                )
            expected_probabilities = candidate.get("settlement_probabilities")
        elif semantics == "categorical":
            if prediction.get("settlement_reference_outcome") is not None:
                raise ValueError(
                    "Categorical candidate commitments cannot carry a settlement reference"
                )
            execution = prediction.get("execution_entry")
            if isinstance(execution, dict) and execution.get(
                "selection"
            ) != candidate.get("settlement_reference_outcome"):
                raise ValueError(
                    "Predicted categorical commitment does not bind the selected candidate outcome"
                )
            expected_probabilities = _canonical_candidate_categorical_probabilities(
                record, candidate["market_identity"]
            )
        else:
            raise ValueError("Candidate-bound forward market semantics are unsupported")
        if (
            not isinstance(expected_probabilities, dict)
            or set(model_probabilities) != set(expected_probabilities)
            or any(
                not math.isclose(
                    float(model_probabilities[outcome]),
                    float(expected_probabilities[outcome]),
                    rel_tol=0.0,
                    abs_tol=PROBABILITY_AUDIT_TOLERANCE,
                )
                for outcome in model_probabilities
            )
        ):
            raise ValueError(
                "Active forward model probabilities do not match the frozen candidate model"
            )
        prediction_generated_at = parse_aware_datetime(
            str(prediction.get("generated_at") or ""),
            "forward candidate commitment generated_at",
        )
        if candidate_generated_at.astimezone(
            timezone.utc
        ) > prediction_generated_at.astimezone(timezone.utc):
            raise ValueError(
                "Forward commitment cannot precede its candidate evaluation disposition"
            )

        source_binding = candidate.get("source_evidence_binding")
        bookmaker = prediction.get("bookmaker_snapshot")
        if not isinstance(source_binding, dict) or not isinstance(bookmaker, dict):
            raise ValueError(
                "Active candidate and forward commitment require the same source snapshot"
            )
        if any(
            source_binding.get(candidate_field) != bookmaker.get(ledger_field)
            for candidate_field, ledger_field in (
                ("evidence_hash", "source_evidence_hash"),
                ("source_url", "source_url"),
                ("collected_at", "collected_at"),
                ("price_basis", "price_basis"),
                ("odds_format", "odds_format"),
                ("firm_count", "firm_count"),
            )
        ):
            raise ValueError(
                "Active candidate and forward commitment source snapshots do not match"
            )
        if any(
            other.get("source_evidence_binding") != source_binding
            for other in candidates
        ):
            raise ValueError(
                "Selections sharing one exact identity must use the same frozen source snapshot"
            )
        candidate_prices = source_binding.get("prices")
        ledger_prices = bookmaker.get("complete_market_odds")
        if (
            not isinstance(candidate_prices, dict)
            or not isinstance(ledger_prices, dict)
            or set(candidate_prices) != set(ledger_prices)
            or any(
                not math.isclose(
                    float(candidate_prices[outcome]),
                    float(ledger_prices[outcome]),
                    rel_tol=0.0,
                    abs_tol=5e-7,
                )
                for outcome in candidate_prices
            )
        ):
            raise ValueError(
                "Active candidate and forward commitment prices do not match"
            )

    _validate_active_formal_pick_binding(record, audit, predictions_by_hash)
    return {
        "candidate_evaluation_audit_hash": audit["audit_hash"],
        "evaluated_identity_count": len(candidate_groups),
        "evaluated_market_count": sum(
            item.get("status") == "evaluated"
            for item in manifest
            if isinstance(item, dict)
        ),
        "unavailable_market_count": sum(
            item.get("status") == "unavailable"
            for item in manifest
            if isinstance(item, dict)
        ),
    }


def _canonical_forward_record_prediction_payload(
    record: dict[str, Any],
    ledger_archive: dict[str, Any],
    provenance_binding: dict[str, Any],
    *,
    schema_version: str,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "archived_at": str(record.get("updated_at") or record.get("created_at") or ""),
        "analysis_stage": str(record.get("analysis_stage") or "initial"),
        "fixture": {
            "match_id": str(record.get("match_id") or ""),
            "league": record.get("league"),
            "kickoff": record.get("kickoff"),
            "home_team": record.get("home_team"),
            "away_team": record.get("away_team"),
        },
        "model_outputs": {
            "probabilities": deepcopy(record.get("probabilities")),
            "predicted_score": record.get("predicted_score"),
            "exact_score_picks": deepcopy(record.get("exact_score_picks", [])),
            "asian_pick": deepcopy(record.get("asian_pick")),
            "total_pick": deepcopy(record.get("total_pick")),
            "half_time_pick": deepcopy(record.get("half_time_pick")),
            "htft_picks": deepcopy(record.get("htft_picks", [])),
            "goal_range_pick": deepcopy(record.get("goal_range_pick")),
            "btts_pick": deepcopy(record.get("btts_pick")),
            "corner_total_pick": deepcopy(record.get("corner_total_pick")),
            "corner_handicap_pick": deepcopy(record.get("corner_handicap_pick")),
            "primary_market": record.get("primary_market"),
            "primary_pick": deepcopy(record.get("primary_pick")),
            "candidate_audits": deepcopy(record.get("candidate_audits", [])),
            "joint_scenario_audit": deepcopy(record.get("joint_scenario_audit")),
            "score_model_provenance": deepcopy(record.get("score_model_provenance")),
            "source_evidence_audit": deepcopy(record.get("source_evidence_audit")),
            "evaluation_eligibility": deepcopy(record.get("evaluation_eligibility")),
            "market_status": deepcopy(record.get("market_status")),
        },
        "ledger": {
            "ledger_hash": ledger_archive["ledger_hash"],
            "archive_hash": ledger_archive["archive_hash"],
            "market_commitments": deepcopy(ledger_archive["market_commitments"]),
        },
        "provenance_binding": deepcopy(provenance_binding),
    }


def canonical_forward_record_prediction_payload(
    record: dict[str, Any],
    ledger_archive: dict[str, Any],
    provenance_binding: dict[str, Any],
) -> dict[str, Any]:
    """Build only the current prediction commitment payload.

    Previous schemas are reconstructed internally for immutable replay and are never exposed
    through the active builder.
    """

    return _canonical_forward_record_prediction_payload(
        record,
        ledger_archive,
        provenance_binding,
        schema_version=FORWARD_RECORD_PREDICTION_SCHEMA_VERSION,
    )


def build_forward_record_prediction_commitment(
    record: dict[str, Any],
    ledger_archive: dict[str, Any],
    base_binding: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    validated_base = forward_policy.validate_record_binding(base_binding)
    if (
        validated_base is None
        or validated_base.get("schema_version")
        != forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION
    ):
        raise ValueError(
            "Active cohort record requires a provenance-complete base binding"
        )
    payload = canonical_forward_record_prediction_payload(
        record, ledger_archive, validated_base["provenance_binding"]
    )
    prediction_hash = forward_policy._hash_json(payload)
    committed_binding = forward_policy.bind_observation_commitment(
        validated_base, prediction_hash
    )
    commitment: dict[str, Any] = {
        "schema_version": FORWARD_RECORD_COMMITMENT_SCHEMA_VERSION,
        "prediction_payload": payload,
        "prediction_hash": prediction_hash,
        "forward_policy_binding": committed_binding,
    }
    commitment["commitment_hash"] = forward_policy._hash_json(commitment)
    return commitment, committed_binding


def validate_forward_record_prediction_commitment(
    record: dict[str, Any],
) -> dict[str, Any] | None:
    raw = record.get("forward_prediction_commitment")
    binding_raw = record.get("forward_policy_binding")
    if raw is None and binding_raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Forward-bound record has no prediction commitment")
    value = deepcopy(raw)
    required = {
        "schema_version",
        "prediction_payload",
        "prediction_hash",
        "forward_policy_binding",
        "commitment_hash",
    }
    if set(value) != required:
        raise ValueError("Forward record commitment fields are incomplete")
    commitment_schema = value.get("schema_version")
    if commitment_schema not in {
        PREVIOUS_FORWARD_RECORD_COMMITMENT_SCHEMA_VERSION,
        FORWARD_RECORD_COMMITMENT_SCHEMA_VERSION,
    }:
        raise ValueError("Forward record commitment schema is unsupported")
    historical = commitment_schema == PREVIOUS_FORWARD_RECORD_COMMITMENT_SCHEMA_VERSION
    supplied_hash = value.pop("commitment_hash", None)
    if supplied_hash != forward_policy._hash_json(value):
        raise ValueError("Forward record commitment hash is invalid")
    ledger = validate_forward_validation_ledger_archive(
        record.get("forward_validation_ledger"), record=record
    )
    expected_ledger_schema = (
        PREVIOUS_FORWARD_LEDGER_ARCHIVE_SCHEMA_VERSION
        if historical
        else FORWARD_LEDGER_ARCHIVE_SCHEMA_VERSION
    )
    if ledger.get("schema_version") != expected_ledger_schema:
        raise ValueError("Forward record commitment and ledger schemas do not match")
    if not historical:
        validate_active_candidate_evaluation_ledger_binding(record, ledger)
    try:
        binding = forward_policy.validate_record_binding(
            value.get("forward_policy_binding")
        )
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError("Forward record commitment binding is invalid") from exc
    expected_binding_schema = (
        forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
        if historical
        else forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
    )
    if (
        binding is None
        or binding.get("schema_version") != expected_binding_schema
        or binding != binding_raw
    ):
        raise ValueError("Forward record does not preserve its committed binding")
    expected_payload = _canonical_forward_record_prediction_payload(
        record,
        ledger,
        binding["provenance_binding"],
        schema_version=(
            PREVIOUS_FORWARD_RECORD_PREDICTION_SCHEMA_VERSION
            if historical
            else FORWARD_RECORD_PREDICTION_SCHEMA_VERSION
        ),
    )
    expected_prediction_hash = forward_policy._hash_json(expected_payload)
    if (
        value.get("prediction_payload") != expected_payload
        or value.get("prediction_hash") != expected_prediction_hash
        or binding.get("observation_commitment_hash") != expected_prediction_hash
    ):
        raise ValueError("Forward record prediction no longer matches its commitment")
    expected_archive_hash = forward_policy._hash_json(
        snapshot_payload(revision_snapshot(record))
    )
    if record.get("archive_version_hash") != expected_archive_hash:
        raise ValueError(
            "Forward record archive hash no longer matches its committed binding"
        )
    value["commitment_hash"] = supplied_hash
    return value


def _forward_observed_settlement_state(
    record: dict[str, Any], prediction: dict[str, Any], schema: dict[str, Any]
) -> str | None:
    final = re.fullmatch(
        r"\s*(\d+)\s*-\s*(\d+)\s*", str(record.get("final_score") or "")
    )
    if final is None:
        return None
    home, away = (int(final.group(1)), int(final.group(2)))
    full_code = "H" if home > away else "A" if away > home else "D"
    raw_states = schema.get("settlement_states")
    if not isinstance(raw_states, list):
        return None
    settlement_states = set(raw_states)
    identity = prediction.get("market_identity")
    if not isinstance(identity, dict):
        return None
    family = str(identity.get("family") or "")
    period = str(identity.get("period") or "")
    semantics = str(schema.get("settlement_semantics") or "")
    half = re.fullmatch(
        r"\s*(\d+)\s*-\s*(\d+)\s*", str(record.get("half_time_score") or "")
    )
    if period == "first_half":
        if half is None:
            return None
        score_home, score_away = int(half.group(1)), int(half.group(2))
    else:
        score_home, score_away = home, away
    if semantics == "five_state_return":
        reference = str(prediction.get("settlement_reference_outcome") or "")
        line = identity.get("line")
        if reference not in identity.get("price_outcomes", []) or line is None:
            return None
        if family in {"asian", "corner_handicap"}:
            pick_line = float(line) if reference == "home" else -float(line)
            pick = {"side": reference, "line": pick_line}
            if family == "corner_handicap":
                home_corners = record.get("home_corners")
                away_corners = record.get("away_corners")
                if home_corners is None or away_corners is None:
                    return None
                observed = settle_corner_handicap(
                    pick, int(home_corners), int(away_corners)
                )
            else:
                observed = settle_asian(pick, score_home, score_away)
        elif family in {"total", "corner_total"}:
            pick = {"side": reference, "line": float(line)}
            if family == "corner_total":
                home_corners = record.get("home_corners")
                away_corners = record.get("away_corners")
                if home_corners is None or away_corners is None:
                    return None
                observed = settle_corner_total(
                    pick, int(home_corners), int(away_corners)
                )
            else:
                observed = settle_total(pick, score_home, score_away)
        else:
            return None
        normalized = "full_win" if observed == "win" else observed
        return normalized if normalized in settlement_states else None
    if settlement_states == {"H", "D", "A"}:
        return result_code(score_home, score_away)
    if family == "htft" and half is not None:
        half_home, half_away = int(half.group(1)), int(half.group(2))
        half_code = (
            "H" if half_home > half_away else "A" if half_away > half_home else "D"
        )
        candidate = f"{half_code}{full_code}"
        return candidate if candidate in settlement_states else None
    if family == "goal_range":
        total_goals = score_home + score_away
        matches: list[str] = []
        for state in raw_states:
            try:
                parsed = parse_goal_range_selection(str(state))
            except ValueError:
                return None
            minimum = int(parsed["minimum_goals"])
            maximum = parsed["maximum_goals"]
            if total_goals >= minimum and (
                maximum is None or total_goals <= int(maximum)
            ):
                matches.append(str(state))
        return matches[0] if len(matches) == 1 else None
    actual_score = f"{home}-{away}"
    if actual_score in settlement_states:
        return actual_score
    if settlement_states in ({"yes", "no"}, {"Y", "N"}):
        yes = home > 0 and away > 0
        return (
            ("yes" if yes else "no")
            if "yes" in settlement_states
            else ("Y" if yes else "N")
        )
    return None


def _previous_forward_observed_outcome(
    record: dict[str, Any], market: str, outcomes: list[str]
) -> str | None:
    """Replay the v2 observation outcome contract without upgrading it."""

    final = re.fullmatch(
        r"\s*(\d+)\s*-\s*(\d+)\s*", str(record.get("final_score") or "")
    )
    if final is None:
        return None
    home, away = (int(final.group(1)), int(final.group(2)))
    full_code = "H" if home > away else "A" if away > home else "D"
    outcome_set = set(outcomes)
    if outcome_set == {"H", "D", "A"}:
        return full_code
    half = re.fullmatch(
        r"\s*(\d+)\s*-\s*(\d+)\s*", str(record.get("half_time_score") or "")
    )
    if market == "htft" and half is not None:
        half_home, half_away = int(half.group(1)), int(half.group(2))
        half_code = (
            "H" if half_home > half_away else "A" if half_away > half_home else "D"
        )
        candidate = f"{half_code}{full_code}"
        return candidate if candidate in outcome_set else None
    actual_score = f"{home}-{away}"
    if actual_score in outcome_set:
        return actual_score
    if outcome_set in ({"yes", "no"}, {"Y", "N"}):
        yes = home > 0 and away > 0
        return (
            ("yes" if yes else "no") if "yes" in outcome_set else ("Y" if yes else "N")
        )
    return None


def _forward_validation_micro_ledger_for_record(
    record: dict[str, Any], *, cohort_closure: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Replay one archived micro-ledger and append only verified post-match settlement.

    The queue, probabilities, baselines and commitment objects are copied byte-for-byte
    from the pre-match archive.  This function never reconstructs them from reviewed data.
    """

    commitment = validate_forward_record_prediction_commitment(record)
    if commitment is None:
        raise ValueError("Record has no replayable forward prediction commitment")
    ledger_archive = validate_forward_validation_ledger_archive(
        record.get("forward_validation_ledger"), record=record
    )
    payload = deepcopy(ledger_archive["ledger_payload"])
    historical = (
        ledger_archive.get("schema_version")
        == PREVIOUS_FORWARD_LEDGER_ARCHIVE_SCHEMA_VERSION
    )
    if cohort_closure is not None:
        payload["cohort_closure"] = deepcopy(cohort_closure)
    if record.get("status") == "reviewed":
        verification = record.get("result_verification")
        if not isinstance(verification, dict):
            raise ValueError("Reviewed forward record has no result verification")
        predictions = {
            item["prediction_payload"]["observation_id"]: item["prediction_payload"]
            for item in payload.get("commitments", [])
        }
        schemas = payload.get("market_schemas", {})
        for settlement in payload.get("settlements", []):
            observation_id = str(settlement.get("observation_id") or "")
            prediction = predictions.get(observation_id)
            if not isinstance(prediction, dict):
                raise ValueError("Forward settlement has no archived prediction")
            if historical:
                market = str(prediction.get("market") or "")
                schema = schemas.get(market, {})
                outcomes = schema.get("outcomes") if isinstance(schema, dict) else None
                if not isinstance(outcomes, list):
                    raise ValueError("Forward market schema outcomes are unavailable")
                observed = _previous_forward_observed_outcome(
                    record, market, list(outcomes)
                )
            else:
                raw_identity = prediction.get("market_identity")
                family = (
                    str(raw_identity.get("family") or "")
                    if isinstance(raw_identity, dict)
                    else ""
                )
                schema = schemas.get(family, {})
                if not isinstance(schema, dict) or not isinstance(
                    schema.get("settlement_states"), list
                ):
                    raise ValueError(
                        "Forward market schema settlement states are unavailable"
                    )
                observed = _forward_observed_settlement_state(
                    record, prediction, schema
                )
            if observed is None:
                continue
            settlement.update(
                {
                    "status": "settled",
                    (
                        "observed_outcome"
                        if historical
                        else "observed_settlement_state"
                    ): observed,
                    "result_collected_at": verification.get("collected_at"),
                    "result_source_evidence_hash": forward_policy._hash_json(
                        {
                            "fixture_id": record.get("match_id"),
                            "source": verification.get("source"),
                            "collected_at": verification.get("collected_at"),
                            "final_score": record.get("final_score"),
                            "half_time_score": record.get("half_time_score"),
                        }
                    ),
                }
            )
            settlement.pop("settlement_hash", None)
            settlement["settlement_hash"] = forward_policy._hash_json(settlement)
    return payload


def _forward_history_record_receipt(
    record: dict[str, Any], *, cohort_closure: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build one self-contained receipt only after replaying the stored record chain."""

    commitment = validate_forward_record_prediction_commitment(record)
    if commitment is None:
        raise ValueError("Record has no replayable forward prediction commitment")
    ledger_archive = validate_forward_validation_ledger_archive(
        record.get("forward_validation_ledger"), record=record
    )
    ledger_payload = _forward_validation_micro_ledger_for_record(
        record, cohort_closure=cohort_closure
    )
    prediction_payload = commitment.get("prediction_payload")
    binding = commitment.get("forward_policy_binding")
    if not isinstance(prediction_payload, dict) or not isinstance(binding, dict):
        raise ValueError("Forward record commitment is incomplete")
    archived_at = str(prediction_payload.get("archived_at") or "")
    if parse_aware_datetime(
        archived_at, "forward record archived_at"
    ) != parse_aware_datetime(
        str(binding.get("archived_at") or ""), "forward binding archived_at"
    ):
        raise ValueError(
            "Forward record archived_at does not match its committed binding"
        )
    archive_version_hash = require_sha256(
        record.get("archive_version_hash"), "forward archive version hash"
    )
    snapshot = snapshot_payload(revision_snapshot(record))
    if forward_policy._hash_json(snapshot) != archive_version_hash:
        raise ValueError("Forward record archive snapshot does not replay")
    market_commitments = _forward_market_commitments(ledger_archive["ledger_payload"])
    historical = (
        commitment.get("schema_version")
        == PREVIOUS_FORWARD_RECORD_COMMITMENT_SCHEMA_VERSION
    )
    receipt: dict[str, Any] = {
        "schema_version": (
            PREVIOUS_FORWARD_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION
            if historical
            else FORWARD_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION
        ),
        "fixture_id": str(record.get("match_id") or ""),
        "record_archived_at": archived_at,
        "archive_version_hash": archive_version_hash,
        "record_commitment_hash": require_sha256(
            commitment.get("commitment_hash"), "forward record commitment hash"
        ),
        "record_binding_hash": require_sha256(
            binding.get("binding_hash"), "forward record binding hash"
        ),
        "prematch_ledger_hash": require_sha256(
            ledger_archive.get("ledger_hash"), "forward pre-match ledger hash"
        ),
        "ledger_payload_hash": forward_policy._hash_json(ledger_payload),
        "market_commitments": market_commitments,
        "ledger_payload": ledger_payload,
        "archive_snapshot_payload": snapshot,
    }
    if not receipt["fixture_id"]:
        raise ValueError("Forward history receipt fixture_id is missing")
    receipt["receipt_hash"] = forward_policy._hash_json(receipt)
    return receipt


def _execution_receipt_hashes_from_receipt(receipt: dict[str, Any]) -> list[str]:
    """Replay firm evidence and return canonical receipt identities for closure."""

    try:
        from scripts import execution_evidence
    except ImportError:  # Direct execution from scripts/.
        import execution_evidence  # type: ignore[no-redef]

    ledger = receipt.get("ledger_payload")
    commitments = ledger.get("commitments") if isinstance(ledger, dict) else None
    if not isinstance(commitments, list):
        raise ValueError("Forward record receipt commitments are missing")
    hashes: list[str] = []
    for commitment in commitments:
        prediction = (
            commitment.get("prediction_payload")
            if isinstance(commitment, dict)
            else None
        )
        execution = (
            prediction.get("execution_entry") if isinstance(prediction, dict) else None
        )
        if execution is None:
            continue
        if not isinstance(execution, dict):
            raise ValueError("Forward execution entry is invalid")
        evidence_file = str(execution.get("execution_evidence_file") or "").strip()
        if not evidence_file:
            raise ValueError("Current forward execution lacks firm receipt evidence")
        try:
            evidence = execution_evidence.validate_evidence_file(evidence_file)
        except (execution_evidence.ExecutionEvidenceError, OSError) as exc:
            raise ValueError("Forward execution receipt cannot be replayed") from exc
        if evidence.get("evidence_hash") != require_sha256(
            execution.get("execution_evidence_hash"),
            "forward execution evidence hash",
        ):
            raise ValueError("Forward execution evidence hash does not match")
        firm = evidence.get("firm")
        offer = evidence.get("offer")
        if not isinstance(firm, dict) or not isinstance(offer, dict):
            raise ValueError("Forward execution receipt identity is missing")
        identity = {
            "firm_id": firm.get("firm_id"),
            "account_region": firm.get("account_region"),
            "receipt_id": offer.get("receipt_id"),
        }
        hashes.append(forward_policy._hash_json(identity))
    if len(hashes) != len(set(hashes)):
        raise ValueError(
            "One firm receipt identity cannot fund multiple record entries"
        )
    return sorted(hashes)


def _record_manifest_entry_from_receipt(
    receipt: dict[str, Any],
    *,
    manifest_schema_version: str = forward_policy.RECORD_MANIFEST_SCHEMA_VERSION,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "fixture_id": str(receipt.get("fixture_id") or ""),
        "archive_version_hash": require_sha256(
            receipt.get("archive_version_hash"), "forward archive version hash"
        ),
        "record_commitment_hash": require_sha256(
            receipt.get("record_commitment_hash"), "forward record commitment hash"
        ),
        "record_binding_hash": require_sha256(
            receipt.get("record_binding_hash"), "forward record binding hash"
        ),
        "prematch_ledger_hash": require_sha256(
            receipt.get("prematch_ledger_hash"), "forward pre-match ledger hash"
        ),
    }
    snapshot = receipt.get("archive_snapshot_payload")
    binding = (
        snapshot.get("forward_policy_binding") if isinstance(snapshot, dict) else None
    )
    request = (
        binding.get("cohort_request_binding") if isinstance(binding, dict) else None
    )
    if request is not None:
        entry["request_event_hash"] = require_sha256(
            request.get("request_event_hash"),
            "forward cohort request event hash",
        )
        if manifest_schema_version in {
            forward_policy.PREVIOUS_FULL_RECORD_MANIFEST_SCHEMA_VERSION,
            forward_policy.PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
            forward_policy.RECORD_MANIFEST_SCHEMA_VERSION,
        } and request.get("schema_version") in {
            cohort_scope.PREVIOUS_FULL_REQUEST_BINDING_SCHEMA_VERSION,
            cohort_scope.REQUEST_BINDING_SCHEMA_VERSION,
        }:
            fixture = request.get("fixture")
            if not isinstance(fixture, dict):
                raise ValueError("Forward cohort request fixture is missing")
            entry.update(
                {
                    "request_fixture_id": str(request.get("request_fixture_id") or ""),
                    "fixture": deepcopy(fixture),
                    "fixture_event_hash": require_sha256(
                        request.get("fixture_event_hash"),
                        "forward cohort fixture event hash",
                    ),
                    "execution_receipt_hashes": (
                        _execution_receipt_hashes_from_receipt(receipt)
                    ),
                }
            )
            if manifest_schema_version in {
                forward_policy.PREVIOUS_EVENT_BOUND_RECORD_MANIFEST_SCHEMA_VERSION,
                forward_policy.RECORD_MANIFEST_SCHEMA_VERSION,
            } and (
                request.get("schema_version")
                == cohort_scope.REQUEST_BINDING_SCHEMA_VERSION
            ):
                entry["fixture_event_at"] = str(request.get("fixture_event_at") or "")
    if manifest_schema_version == forward_policy.RECORD_MANIFEST_SCHEMA_VERSION:
        entry["record_archived_at"] = str(receipt.get("record_archived_at") or "")
    return entry


def forward_record_manifest_for_records(
    records: list[dict[str, Any]],
    *,
    cohort_manifest: dict[str, Any],
    denominator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the complete canonical record index used to close one cohort."""

    try:
        cohort = forward_policy.validate_cohort(cohort_manifest)
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError("Forward record manifest cohort is invalid") from exc
    receipts = [_forward_history_record_receipt(record) for record in records]
    if any(
        receipt.get("schema_version") != FORWARD_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION
        for receipt in receipts
    ):
        raise ValueError(
            "Historical forward records are read-only and cannot create a new cohort closure"
        )
    receipts.sort(key=lambda item: str(item["fixture_id"]))
    entries = [_record_manifest_entry_from_receipt(receipt) for receipt in receipts]
    fixture_ids = [entry["fixture_id"] for entry in entries]
    if len(set(fixture_ids)) != len(fixture_ids):
        raise ValueError("Forward record manifest contains duplicate fixture records")
    receipt_hashes = [
        receipt_hash
        for entry in entries
        for receipt_hash in entry.get("execution_receipt_hashes", [])
    ]
    if len(receipt_hashes) != len(set(receipt_hashes)):
        raise ValueError(
            "Forward record manifest reuses one firm/account/receipt identity"
        )
    for receipt in receipts:
        ledger = receipt["ledger_payload"]
        if (
            ledger.get("cohort_id") != cohort["cohort_id"]
            or ledger.get("policy_id") != cohort["policy_id"]
            or ledger.get("policy_hash") != cohort["policy_hash"]
            or ledger.get("cohort_manifest") != cohort
        ):
            raise ValueError(
                "Forward record manifest cannot mix records from another cohort or policy"
            )
    manifest: dict[str, Any] = {
        "schema_version": forward_policy.RECORD_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "soccer_untouched_live_forward_record_manifest",
        "cohort_id": cohort["cohort_id"],
        "cohort_hash": cohort["cohort_hash"],
        "policy_id": cohort["policy_id"],
        "policy_hash": cohort["policy_hash"],
        "record_count": len(entries),
        "records": entries,
        "max_record_archived_at": (
            max(
                forward_policy._aware_datetime(
                    entry["record_archived_at"], "forward record archived_at"
                )
                for entry in entries
            ).isoformat()
            if entries
            else None
        ),
    }
    if denominator is not None:
        manifest["denominator"] = deepcopy(denominator)
        manifest["denominator_hash"] = require_sha256(
            denominator.get("denominator_hash"),
            "forward cohort denominator hash",
        )
    manifest["manifest_hash"] = forward_policy._hash_json(manifest)
    try:
        return forward_policy.validate_record_manifest(manifest, cohort=cohort)
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError("Forward record manifest is invalid") from exc


def forward_validation_input_for_records(
    records: list[dict[str, Any]], *, cohort_closure: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Export one canonical cohort envelope from independently committed records."""

    if not records:
        raise ValueError("Forward cohort export requires at least one record")
    if cohort_closure is None:
        raise ValueError(
            "Formal forward cohort export requires a closed cohort record manifest"
        )
    try:
        closure = forward_policy.validate_closure(
            cohort_closure, require_record_manifest=True
        )
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError("Forward cohort closure is invalid") from exc
    receipts = [
        _forward_history_record_receipt(record, cohort_closure=closure)
        for record in records
    ]
    receipts.sort(key=lambda item: str(item["fixture_id"]))
    fixture_ids = [str(item["fixture_id"]) for item in receipts]
    if len(set(fixture_ids)) != len(fixture_ids):
        raise ValueError("Forward cohort export contains duplicate fixture records")
    receipt_schemas = {str(item.get("schema_version") or "") for item in receipts}
    if len(receipt_schemas) != 1:
        raise ValueError(
            "Forward cohort export cannot mix historical and current records"
        )
    historical = receipt_schemas == {
        PREVIOUS_FORWARD_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION
    }
    if not historical and receipt_schemas != {
        FORWARD_HISTORY_RECORD_RECEIPT_SCHEMA_VERSION
    }:
        raise ValueError("Forward cohort export receipt schema is unsupported")
    if historical:
        raise ValueError(
            "Historical forward-observations/2.0.0 records are defect-quarantined "
            "and cannot enter formal validation export"
        )

    first_ledger = receipts[0]["ledger_payload"]
    policy = deepcopy(first_ledger.get("policy_manifest"))
    cohort = deepcopy(first_ledger.get("cohort_manifest"))
    if not isinstance(policy, dict) or not isinstance(cohort, dict):
        raise ValueError("Forward cohort export policy/cohort is missing")
    try:
        validated_policy = forward_policy.validate_policy_manifest(policy)
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError("Forward cohort export policy is invalid") from exc
    schema_contract = forward_policy.closure_schema_contract(
        validated_policy["software"]["package_version"]
    )
    try:
        closure = forward_policy.validate_closure(
            closure,
            cohort=cohort,
            require_record_manifest=True,
            required_closure_schema=schema_contract["closure"],
            required_record_manifest_schema=schema_contract["record_manifest"],
            required_denominator_schema=schema_contract["denominator"],
        )
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError(
            "Forward cohort closure does not bind the archived cohort"
        ) from exc
    cohort_id = str(first_ledger.get("cohort_id") or "")
    policy_id = str(first_ledger.get("policy_id") or "")
    policy_hash = require_sha256(
        first_ledger.get("policy_hash"), "forward cohort policy hash"
    )
    for receipt in receipts:
        ledger = receipt["ledger_payload"]
        if (
            ledger.get("cohort_id") != cohort_id
            or ledger.get("policy_id") != policy_id
            or ledger.get("policy_hash") != policy_hash
            or ledger.get("policy_manifest") != policy
            or ledger.get("cohort_manifest") != cohort
            or ledger.get("cohort_closure") != closure
        ):
            raise ValueError(
                "Forward cohort export cannot mix policies, cohorts, or closures"
            )

    receipt_manifest_entries = [
        _record_manifest_entry_from_receipt(
            receipt,
            manifest_schema_version=closure["record_manifest"]["schema_version"],
        )
        for receipt in receipts
    ]
    if receipt_manifest_entries != closure["record_manifest"]["records"]:
        raise ValueError(
            "Forward cohort receipts do not exactly cover the closed record manifest"
        )

    history_binding: dict[str, Any] = {
        "schema_version": (
            PREVIOUS_FORWARD_HISTORY_LEDGER_BINDING_SCHEMA_VERSION
            if historical
            else FORWARD_HISTORY_LEDGER_BINDING_SCHEMA_VERSION
        ),
        "artifact_type": FORWARD_HISTORY_AGGREGATE_ARTIFACT_TYPE,
        "cohort_id": cohort_id,
        "policy_id": policy_id,
        "policy_hash": policy_hash,
        "fixture_ids": fixture_ids,
        "receipts": receipts,
    }
    history_binding["binding_hash"] = forward_policy._hash_json(history_binding)
    return {
        "schema_version": (
            "forward-observations/2.0.0" if historical else "forward-observations/3.0.0"
        ),
        "artifact_type": FORWARD_HISTORY_AGGREGATE_ARTIFACT_TYPE,
        "cohort_id": cohort_id,
        "policy_id": policy_id,
        "policy_hash": policy_hash,
        "policy_manifest": policy,
        "cohort_manifest": cohort,
        "cohort_closure": deepcopy(closure),
        "history_ledger_binding": history_binding,
    }


def forward_validation_input_for_record(
    record: dict[str, Any], *, cohort_closure: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Compatibility wrapper returning a one-record canonical cohort envelope."""

    return forward_validation_input_for_records([record], cohort_closure=cohort_closure)


def _raw_forward_policy_binding(record: dict[str, Any]) -> dict[str, Any] | None:
    basis = record.get("settlement_basis")
    if isinstance(basis, dict) and isinstance(
        basis.get("forward_policy_binding"), dict
    ):
        return basis["forward_policy_binding"]
    binding = record.get("forward_policy_binding")
    return binding if isinstance(binding, dict) else None


def _load_immutable_forward_cohort(
    base_dir: str | Path, cohort_id: str
) -> dict[str, Any]:
    cohort_path = forward_policy.cohort_manifest_path(base_dir, cohort_id)
    try:
        raw = json.loads(cohort_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Forward cohort manifest is unavailable or invalid: {cohort_path}"
        ) from exc
    if not isinstance(raw, dict):
        raise ValueError("Forward cohort manifest must be a JSON object")
    try:
        return forward_policy.validate_cohort(raw)
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError("Forward cohort manifest is invalid") from exc


def _all_forward_records_for_cohort(
    history: list[dict[str, Any]], cohort_id: str
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for record in history:
        binding = _raw_forward_policy_binding(record)
        if binding is None:
            continue
        if str(binding.get("cohort_id") or "") == cohort_id:
            selected.append(record)
    return selected


def _scope_snapshot_for_cohort(cohort: dict[str, Any]) -> dict[str, Any] | None:
    if "scope_hash" not in cohort:
        return None
    policy_file = Path(str(cohort.get("policy_file") or "")).resolve()
    try:
        raw = json.loads(policy_file.read_text(encoding="utf-8"))
        policy = forward_policy.validate_policy_manifest(raw)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        forward_policy.ForwardPolicyError,
    ) as exc:
        raise ValueError("Scoped cohort policy is unavailable or invalid") from exc
    scope = policy.get("cohort_scope", {}).get("scope_snapshot")
    try:
        frozen = cohort_scope.validate_scope(scope)
    except cohort_scope.CohortScopeError as exc:
        raise ValueError("Scoped cohort denominator definition is invalid") from exc
    if frozen["scope_id"] != cohort.get("scope_id") or frozen[
        "scope_hash"
    ] != cohort.get("scope_hash"):
        raise ValueError("Scoped cohort does not match its policy scope")
    return frozen


def _denominator_for_records(
    *,
    base_dir: str | Path,
    cohort: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    scope = _scope_snapshot_for_cohort(cohort)
    if scope is None:
        return None
    receipts = [_forward_history_record_receipt(record) for record in records]
    receipt_entries = sorted(
        (_record_manifest_entry_from_receipt(receipt) for receipt in receipts),
        key=lambda item: item["fixture_id"],
    )
    try:
        event_binding = {
            "cohort_hash": cohort["cohort_hash"],
            "policy_id": cohort["policy_id"],
            "policy_hash": cohort["policy_hash"],
            "starts_at": cohort["starts_at"],
        }
        events = cohort_scope.load_events(
            base_dir,
            str(cohort["cohort_id"]),
            scope=scope,
            cohort_binding=event_binding,
        )
        return cohort_scope.build_denominator(
            scope=scope,
            cohort_id=str(cohort["cohort_id"]),
            events=events,
            record_manifest={"records": receipt_entries},
            cohort_binding=event_binding,
        )
    except cohort_scope.CohortScopeError as exc:
        raise ValueError(
            "Forward cohort denominator is incomplete or irreproducible"
        ) from exc


def _write_json_atomically(output: str | Path, payload: dict[str, Any]) -> Path:
    path = Path(output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _write_forward_record_manifest_once(
    output: str | Path,
    manifest: dict[str, Any],
    *,
    cohort: dict[str, Any],
) -> Path:
    """Create the close manifest once, or accept an identical crash-retry."""

    path = Path(output).resolve()
    if not path.exists():
        return _write_json_atomically(path, manifest)
    try:
        existing_raw = json.loads(path.read_text(encoding="utf-8"))
        existing = forward_policy.validate_record_manifest(existing_raw, cohort=cohort)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        forward_policy.ForwardPolicyError,
    ) as exc:
        raise ValueError(
            "Existing forward record manifest is invalid; refusing crash recovery"
        ) from exc
    if existing != manifest:
        raise ValueError(
            "Existing forward record manifest differs; refusing crash recovery"
        )
    return path


def _forward_record_manifest_export_directory(base_dir: str | Path) -> Path:
    return (
        Path(base_dir).resolve()
        / ".codex"
        / "soccer-predict"
        / "forward-record-manifest-exports"
    )


def _reject_symlink_components(path: Path, *, boundary: Path) -> None:
    """Reject a manifest target whose existing path components contain links."""

    try:
        relative = path.relative_to(boundary)
    except ValueError as exc:
        raise ValueError(
            "Forward record manifest output is outside the workspace"
        ) from exc
    current = boundary
    for part in relative.parts:
        current /= part
        is_junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (callable(is_junction) and bool(is_junction())):
            raise ValueError(
                "Forward record manifest output cannot traverse a symbolic link or junction"
            )


def _safe_forward_record_manifest_output(
    output: str | Path,
    *,
    base_dir: str | Path,
    cohort_id: str,
    allow_canonical: bool = True,
) -> Path:
    """Resolve a manifest publication target inside one of two safe locations."""

    workspace = Path(os.path.abspath(os.fspath(base_dir)))
    forward_policy.cohort_manifest_path(base_dir, cohort_id)
    filename = f"{cohort_id}-record-manifest.json"
    canonical_directory = workspace / ".codex" / "soccer-predict" / "forward-cohorts"
    export_directory = (
        workspace / ".codex" / "soccer-predict" / "forward-record-manifest-exports"
    )
    canonical = canonical_directory / filename
    _reject_symlink_components(canonical_directory, boundary=workspace)
    _reject_symlink_components(export_directory, boundary=workspace)
    lexical = Path(os.path.abspath(os.fspath(output)))
    is_junction = getattr(lexical, "is_junction", None)
    if lexical.is_symlink() or (callable(is_junction) and bool(is_junction())):
        raise ValueError(
            "Forward record manifest output cannot traverse a symbolic link or junction"
        )
    resolved = lexical.resolve(strict=False)
    resolved_canonical = canonical.resolve(strict=False)
    resolved_export_directory = export_directory.resolve(strict=False)
    safe_export = (
        resolved.parent == resolved_export_directory and resolved.name == filename
    )
    if not ((allow_canonical and resolved == resolved_canonical) or safe_export):
        raise ValueError(
            "Forward record manifest output must be "
            + ("the canonical cohort manifest or " if allow_canonical else "")
            + "its exact cohort filename inside forward-record-manifest-exports"
        )
    _reject_symlink_components(resolved, boundary=workspace.resolve())
    return resolved


def _write_manifest_json_atomically(
    path: Path, payload: Mapping[str, Any], *, boundary: Path
) -> Path:
    """Write through an exclusively created temp file after link-safe validation."""

    _reject_symlink_components(path, boundary=boundary)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        _reject_symlink_components(path, boundary=boundary)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def _publish_forward_record_manifest(
    output: str | Path,
    manifest: dict[str, Any],
    *,
    base_dir: str | Path,
    cohort_id: str,
    allow_canonical: bool,
) -> Path:
    """Publish a manifest without permitting state-file overwrite paths."""

    clean_cohort_id = str(cohort_id or "").strip()
    if manifest.get("cohort_id") != clean_cohort_id:
        raise ValueError("Closed record manifest does not match its cohort_id")
    path = _safe_forward_record_manifest_output(
        output,
        base_dir=base_dir,
        cohort_id=clean_cohort_id,
        allow_canonical=allow_canonical,
    )
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing = None
        if (
            isinstance(existing, Mapping)
            and existing.get("artifact_type")
            == "soccer_untouched_live_forward_record_manifest"
            and existing.get("cohort_id") != clean_cohort_id
        ):
            raise ValueError(
                "Forward record manifest output belongs to a different cohort"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(path, boundary=Path(base_dir).resolve())
    return _write_manifest_json_atomically(
        path,
        manifest,
        boundary=Path(base_dir).resolve(),
    )


def publish_closed_record_manifest(
    output: str | Path,
    manifest: dict[str, Any],
    *,
    base_dir: str | Path,
    cohort_id: str,
) -> Path:
    """Publish a closure-authorized manifest to a canonical or safe export path."""

    return _publish_forward_record_manifest(
        output,
        manifest,
        base_dir=base_dir,
        cohort_id=cohort_id,
        allow_canonical=True,
    )


def publish_record_manifest_preview(
    output: str | Path,
    manifest: dict[str, Any],
    *,
    base_dir: str | Path,
    cohort_id: str,
) -> Path:
    """Publish a preview only inside the dedicated export directory."""

    return _publish_forward_record_manifest(
        output,
        manifest,
        base_dir=base_dir,
        cohort_id=cohort_id,
        allow_canonical=False,
    )


@locked_history_transaction
def cmd_export_forward_record_manifest(args: argparse.Namespace) -> dict[str, Any]:
    """Write the complete record index for one immutable cohort from history."""

    cohort_id = str(args.cohort_id or "").strip()
    if not cohort_id:
        raise ValueError("--cohort-id is required")
    history = load_history(data_path(args.base_dir))
    cohort = _load_immutable_forward_cohort(args.base_dir, cohort_id)
    selected = _all_forward_records_for_cohort(history, cohort_id)
    denominator = _denominator_for_records(
        base_dir=args.base_dir, cohort=cohort, records=selected
    )
    manifest = forward_record_manifest_for_records(
        selected, cohort_manifest=cohort, denominator=denominator
    )
    output = publish_record_manifest_preview(
        args.output,
        manifest,
        base_dir=args.base_dir,
        cohort_id=cohort_id,
    )
    return {
        "ok": True,
        "path": str(output),
        "cohort_id": cohort_id,
        "fixture_ids": [entry["fixture_id"] for entry in manifest["records"]],
        "record_count": manifest["record_count"],
        "manifest_hash": manifest["manifest_hash"],
    }


@locked_history_transaction
def cmd_close_forward_cohort(args: argparse.Namespace) -> dict[str, Any]:
    """Atomically snapshot all cohort records and close the active policy boundary."""

    cohort_id = str(args.cohort_id or "").strip()
    if not cohort_id:
        raise ValueError("--cohort-id is required")
    history = load_history(data_path(args.base_dir))
    cohort = _load_immutable_forward_cohort(args.base_dir, cohort_id)
    selected = _all_forward_records_for_cohort(history, cohort_id)
    denominator = _denominator_for_records(
        base_dir=args.base_dir, cohort=cohort, records=selected
    )
    manifest = forward_record_manifest_for_records(
        selected, cohort_manifest=cohort, denominator=denominator
    )
    requested_output = str(getattr(args, "record_manifest_output", "") or "").strip()
    manifest_output = _safe_forward_record_manifest_output(
        (
            requested_output
            if requested_output
            else forward_policy.cohort_directory(args.base_dir)
            / f"{cohort_id}-record-manifest.json"
        ),
        base_dir=args.base_dir,
        cohort_id=cohort_id,
    )
    try:
        closure_path, closure = forward_policy.close_cohort(
            base_dir=args.base_dir,
            record_manifest=manifest,
            closed_at=getattr(args, "closed_at", None),
        )
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError("Forward cohort could not be closed") from exc
    closed_manifest = closure.get("record_manifest")
    if not isinstance(closed_manifest, dict) or closed_manifest != manifest:
        raise ValueError("Closed cohort did not preserve the candidate record manifest")
    manifest_path = publish_closed_record_manifest(
        manifest_output,
        closed_manifest,
        base_dir=args.base_dir,
        cohort_id=cohort_id,
    )
    return {
        "ok": True,
        "cohort_id": cohort_id,
        "record_manifest_path": str(manifest_path),
        "record_count": closed_manifest["record_count"],
        "manifest_hash": closed_manifest["manifest_hash"],
        "closure_path": str(closure_path),
        "closure_hash": closure["closure_hash"],
    }


@locked_history_transaction
def cmd_export_forward_validation(args: argparse.Namespace) -> dict[str, Any]:
    """Export a canonical multi-record cohort envelope from archived history only."""

    history_file = data_path(args.base_dir)
    history = load_history(history_file)
    requested_cohort = str(args.cohort_id or "").strip()
    if not requested_cohort:
        raise ValueError("--cohort-id is required")
    if getattr(args, "match_id", None):
        raise ValueError(
            "--match-id is not supported for formal validation; export the complete cohort"
        )
    selected = _all_forward_records_for_cohort(history, requested_cohort)

    if not selected:
        raise ValueError("Forward cohort export selected no archived records")
    selected_cohorts = {
        str((_raw_forward_policy_binding(record) or {}).get("cohort_id") or "")
        for record in selected
    }
    if "" in selected_cohorts or len(selected_cohorts) != 1:
        raise ValueError(
            "Forward cohort export records do not share one cohort binding"
        )
    selected_cohort = next(iter(selected_cohorts))
    if requested_cohort and selected_cohort != requested_cohort:
        raise ValueError("Selected records do not match --cohort-id")

    closure_file = str(args.cohort_closure_file or "").strip()
    if not closure_file:
        raise ValueError("--cohort-closure-file is required for formal validation")
    try:
        loaded_closure = json.loads(Path(closure_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("--cohort-closure-file is unavailable or invalid") from exc
    if not isinstance(loaded_closure, dict):
        raise ValueError("--cohort-closure-file must contain a JSON object")
    cohort = _load_immutable_forward_cohort(args.base_dir, requested_cohort)
    try:
        closure = forward_policy.validate_closure(
            loaded_closure, cohort=cohort, require_record_manifest=True
        )
    except forward_policy.ForwardPolicyError as exc:
        raise ValueError("--cohort-closure-file is invalid") from exc
    if closure["cohort_id"] != requested_cohort:
        raise ValueError("Cohort closure does not match --cohort-id")

    payload = forward_validation_input_for_records(selected, cohort_closure=closure)
    output = _write_json_atomically(args.output, payload)
    history_binding = payload["history_ledger_binding"]
    return {
        "ok": True,
        "path": str(output),
        "cohort_id": selected_cohort,
        "fixture_ids": history_binding["fixture_ids"],
        "receipt_count": len(history_binding["receipts"]),
        "binding_hash": history_binding["binding_hash"],
    }


def revision_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "pending",
        "settlement_basis": None,
        "analysis_stage": record.get("analysis_stage", "initial"),
        "archived_at": record.get("updated_at", record.get("created_at")),
        "fixture_id": record.get("match_id"),
        "match_id": record.get("match_id"),
        "home_team": record.get("home_team"),
        "away_team": record.get("away_team"),
        "kickoff": record.get("kickoff"),
        "source_url": record.get("source_url"),
        "league": record.get("league"),
        "league_key": record.get("league_key"),
        "competition_evidence": deepcopy(record.get("competition_evidence")),
        "page_status": record.get("page_status"),
        "source_kickoff": record.get("source_kickoff"),
        "source_timezone": record.get("source_timezone"),
        "user_local_kickoff": record.get("user_local_kickoff", record.get("kickoff")),
        "user_timezone": record.get("user_timezone"),
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
        "candidate_audits": deepcopy(record.get("candidate_audits", [])),
        "joint_scenario_audit": deepcopy(record.get("joint_scenario_audit")),
        "source_evidence_audit": deepcopy(record.get("source_evidence_audit")),
        "fundamental_evidence_audit": deepcopy(
            record.get("fundamental_evidence_audit")
        ),
        "confidence_ranking_version": record.get("confidence_ranking_version"),
        "confidence_policy_version": record.get("confidence_policy_version"),
        "primary_selection_basis": record.get("primary_selection_basis"),
        "score_model_provenance": record.get("score_model_provenance"),
        "evaluation_eligibility": record.get("evaluation_eligibility"),
        "strict_oos_policy_version": record.get("strict_oos_policy_version"),
        "market_status": record.get("market_status"),
        "forward_policy_binding": deepcopy(record.get("forward_policy_binding")),
        "forward_validation_ledger": deepcopy(record.get("forward_validation_ledger")),
        "forward_prediction_commitment": deepcopy(
            record.get("forward_prediction_commitment")
        ),
        "archive_version_hash": record.get("archive_version_hash"),
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
        raise ValueError(
            "Lineup recheck exists but the active record is not the lineup-check version"
        )
    return {
        "policy": "latest_active_prematch_version",
        "grading_scope": "primary_only",
        "analysis_stage": stage,
        "version_archived_at": record.get("updated_at", record.get("created_at")),
        "fixture_id": record.get("match_id"),
        "match_id": record.get("match_id"),
        "home_team": record.get("home_team"),
        "away_team": record.get("away_team"),
        "kickoff": record.get("kickoff"),
        "source_url": record.get("source_url"),
        "league": record.get("league"),
        "league_key": record.get("league_key"),
        "competition_evidence": deepcopy(record.get("competition_evidence")),
        "lineup_rechecked_at": record.get("lineup_rechecked_at"),
        "data_quality": record.get("data_quality", "unknown"),
        "guardrail_evidence": deepcopy(record.get("guardrail_evidence", {})),
        "confidence_ranking_version": record.get("confidence_ranking_version"),
        "confidence_policy_version": record.get("confidence_policy_version"),
        "primary_selection_basis": record.get("primary_selection_basis"),
        "score_model_provenance": deepcopy(record.get("score_model_provenance")),
        "evaluation_eligibility": deepcopy(record.get("evaluation_eligibility")),
        "strict_oos_policy_version": record.get("strict_oos_policy_version"),
        "forward_policy_binding": deepcopy(record.get("forward_policy_binding")),
        "forward_validation_ledger": deepcopy(record.get("forward_validation_ledger")),
        "forward_prediction_commitment": deepcopy(
            record.get("forward_prediction_commitment")
        ),
        "archive_version_hash": record.get("archive_version_hash"),
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
        "candidate_audits": deepcopy(record.get("candidate_audits", [])),
        "joint_scenario_audit": deepcopy(record.get("joint_scenario_audit")),
        "source_evidence_audit": deepcopy(record.get("source_evidence_audit")),
        "fundamental_evidence_audit": deepcopy(
            record.get("fundamental_evidence_audit")
        ),
        "predicted_score": record.get("predicted_score"),
        "exact_score_picks": deepcopy(record.get("exact_score_picks", [])),
        "display_predicted_score": record.get("display_predicted_score"),
        "display_exact_score_picks": deepcopy(
            record.get("display_exact_score_picks", [])
        ),
        "display_exact_score_basis": deepcopy(record.get("display_exact_score_basis")),
        "zero_zero_audit": deepcopy(record.get("zero_zero_audit")),
        "revision_count": len(record.get("revisions", [])),
    }


def snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(snapshot)
    payload.pop("archived_at", None)
    payload.pop("archive_version_hash", None)
    for audit in payload.get("candidate_audits", []):
        if isinstance(audit, dict):
            audit.pop("archived_at", None)
    joint_audit = payload.get("joint_scenario_audit")
    if isinstance(joint_audit, dict):
        joint_audit.pop("archived_at", None)
        joint_audit.pop("audit_hash", None)
        active_binding = joint_audit.get("active_version_binding")
        if isinstance(active_binding, dict):
            active_binding.pop("version_archived_at", None)
    return payload


@locked_history_transaction
def cmd_record(args: argparse.Namespace) -> dict[str, Any]:
    if bool(getattr(args, "force", False)):
        raise ValueError(
            "--force is disabled; archived prediction versions are immutable"
        )
    path = data_path(args.base_dir)
    history = load_history(path)
    existing = find_record(history, args.match_id)
    if existing and existing.get("status") == "reviewed":
        raise ValueError("Reviewed records are terminal and cannot be overwritten")

    current = utc_now()
    time_metadata = validate_record_time_metadata(args, current)
    timestamp = current.astimezone(timezone.utc).isoformat()
    league_key = normalize_league_name(getattr(args, "league_key", None) or args.league)
    base_forward_binding = forward_policy.load_active_binding(
        base_dir=args.base_dir or Path.cwd(),
        repo_root=Path(__file__).resolve().parents[1],
        archived_at=timestamp,
        fixture_id=str(args.match_id),
        competition_key=league_key,
        home_team=args.home_team,
        away_team=args.away_team,
        kickoff=time_metadata["kickoff"],
    )
    revisions = list(existing.get("revisions", [])) if existing else []
    exact_score_picks = [
        parse_exact_score_pick(value) for value in (args.exact_score_pick or [])
    ]
    if len(exact_score_picks) != 2:
        raise ValueError("Record requires exactly two --exact-score-pick values")
    if len({pick["score"] for pick in exact_score_picks}) != 2:
        raise ValueError("Exact-score picks must contain two distinct scores")
    if sum(float(pick["probability"]) for pick in exact_score_picks) > 1.0 + 1e-9:
        raise ValueError("Exact-score probabilities cannot sum to more than 1")
    exact_score_picks.sort(
        key=lambda pick: (
            -float(pick["probability"]),
            *(int(part) for part in str(pick["score"]).split("-")),
        )
    )
    for rank, pick in enumerate(exact_score_picks, start=1):
        pick["rank"] = rank
        pick["status"] = "scenario_only"
    if str(args.predicted_score).strip() != exact_score_picks[0]["score"]:
        raise ValueError(
            "--predicted-score must equal the highest-probability exact-score pick"
        )
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
        "league_key": league_key,
        "kickoff": time_metadata["kickoff"],
        "page_status": time_metadata["page_status"],
        "source_kickoff": time_metadata["source_kickoff"],
        "source_timezone": time_metadata["source_timezone"],
        "user_local_kickoff": time_metadata["user_local_kickoff"],
        "user_timezone": time_metadata["user_timezone"],
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
        "competition_evidence": deepcopy(existing.get("competition_evidence"))
        if existing
        else None,
        "metadata_revisions": deepcopy(existing.get("metadata_revisions", []))
        if existing
        else [],
        "notes": args.notes,
        "data_quality": args.data_quality,
        "guardrail_evidence": {
            "lineup_confirmed": False,
            "fundamental_supported": False,
            "chance_quality_supported": False,
            "attack_configuration_supported": False,
            "opponent_tail_risk_checked": False,
            "corner_profile_supported": False,
            "injury_evidence_status": getattr(
                args, "injury_evidence_status", "not_used"
            ),
            "primary_htft_edge_pp": getattr(args, "primary_htft_edge_pp", None),
            "primary_htft_firm_count": getattr(args, "primary_htft_firm_count", None),
        },
        "probabilities": {
            "home_win": args.home_win_probability,
            "draw": args.draw_probability,
            "away_win": args.away_win_probability,
        },
        "created_at": existing.get("created_at", timestamp) if existing else timestamp,
        "updated_at": timestamp,
        "lineup_rechecked_at": timestamp
        if args.analysis_stage == "lineup-check"
        else (existing.get("lineup_rechecked_at") if existing else None),
        "revisions": revisions,
        "asian_pick": None,
        "total_pick": None,
        "half_time_pick": None,
        "htft_picks": [],
        "goal_range_pick": None,
        "btts_pick": None,
        "corner_total_pick": None,
        "corner_handicap_pick": None,
        "candidate_audits": [],
        "joint_scenario_audit": None,
        "source_evidence_audit": None,
        "fundamental_evidence_audit": None,
        "strict_oos_policy_version": STRICT_OOS_POLICY_VERSION,
        "market_status": deepcopy(STRICT_OOS_MARKET_STATUS),
        "forward_policy_binding": base_forward_binding,
    }
    competition_values = {
        "competition_key": getattr(args, "competition_key", None),
        "competition_label": getattr(args, "competition_label", None),
        "competition_id": getattr(args, "competition_id", None),
        "verification_source": getattr(args, "competition_verification_source", None),
        "source_locator": getattr(args, "competition_source_locator", None),
        "collected_at": getattr(args, "competition_collected_at", None),
    }
    supplied_competition_values = [
        value is not None and str(value).strip() != ""
        for value in competition_values.values()
    ]
    if any(supplied_competition_values) and not all(supplied_competition_values):
        raise ValueError("competition evidence requires every --competition-* field")
    if all(supplied_competition_values):
        supplied_evidence = build_competition_evidence(record, **competition_values)
        existing_raw_evidence = (
            existing.get("competition_evidence") if isinstance(existing, dict) else None
        )
        previous_evidence = (
            validated_competition_evidence(existing)
            if isinstance(existing, dict)
            else None
        )
        if existing_raw_evidence is not None and previous_evidence is None:
            raise ValueError("existing competition evidence is invalid")
        if previous_evidence is not None and (
            previous_evidence.get("competition") != supplied_evidence.get("competition")
            or previous_evidence.get("source", {}).get("url")
            != supplied_evidence.get("source", {}).get("url")
            or previous_evidence.get("source", {}).get("locator")
            != supplied_evidence.get("source", {}).get("locator")
        ):
            raise ValueError(
                "lineup-check competition evidence conflicts with the initial archive"
            )
        # A legitimate kickoff correction changes the fixture binding, so the
        # freshly fetched evidence must replace (not reuse) the initial copy.
        record["competition_evidence"] = supplied_evidence
    elif record.get("competition_evidence") is not None:
        inherited_evidence = validated_competition_evidence(record)
        if inherited_evidence is None:
            raise ValueError(
                "fixture metadata changed; supply fresh source-verified "
                "--competition-* evidence for this version"
            )
        record["competition_evidence"] = inherited_evidence
    if (
        record.get("competition_evidence") is not None
        and validated_competition_evidence(record) is None
    ):
        raise ValueError(
            "competition evidence is invalid for the incoming archived version"
        )
    record["source_evidence_audit"] = load_source_evidence_audit(args, record)
    record["fundamental_evidence_audit"] = load_fundamental_evidence_audit(args, record)
    if record["fundamental_evidence_audit"] is not None:
        record["guardrail_evidence"].update(
            deepcopy(record["fundamental_evidence_audit"]["derived_claims"])
        )
    elif base_forward_binding is None:
        record["guardrail_evidence"].update(
            {
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
            }
        )
    htft_observation = load_htft_observation_audit(args, record)
    if htft_observation is not None:
        record["candidate_audits"].append(htft_observation)
    corner_observation = load_corner_observation_audit(args, record)
    if corner_observation is not None:
        record["candidate_audits"].append(corner_observation)
    validate_candidate_audit_freshness(record)
    validate_probability_triplet(record["probabilities"])
    if args.asian_side:
        record["asian_pick"] = {
            "side": args.asian_side,
            "line": args.asian_line,
            "odds": args.asian_odds,
            "odds_format": getattr(args, "asian_odds_format", None),
            "probability": args.asian_probability,
            "ev": args.asian_ev,
            "edge_pp": getattr(args, "asian_edge_pp", None),
            "firm_count": getattr(args, "asian_firm_count", None),
            "market_signal": args.asian_market_signal,
            **market_audit_args(args, "asian"),
            "settlement_probabilities": settlement_probability_args(args, "asian"),
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
            "odds_format": getattr(args, "total_odds_format", None),
            "probability": args.total_probability,
            "ev": args.total_ev,
            "edge_pp": getattr(args, "total_edge_pp", None),
            "firm_count": getattr(args, "total_firm_count", None),
            "market_signal": args.total_market_signal,
            **market_audit_args(args, "total"),
            "settlement_probabilities": settlement_probability_args(args, "total"),
        }
    if args.half_market:
        if args.half_market == "1x2" and args.half_side not in {"home", "draw", "away"}:
            raise ValueError("Half-time 1X2 requires --half-side home, draw, or away")
        if args.half_market == "asian" and args.half_side not in {"home", "away"}:
            raise ValueError(
                "Half-time Asian handicap requires --half-side home or away"
            )
        if args.half_market == "total" and args.half_side not in {"over", "under"}:
            raise ValueError("Half-time total requires --half-side over or under")
        if args.half_market in {"asian", "total"} and args.half_line is None:
            raise ValueError("Half-time Asian/total picks require --half-line")
        record["half_time_pick"] = {
            "market": args.half_market,
            "side": args.half_side,
            "line": args.half_line,
            "odds": args.half_odds,
            "odds_format": getattr(args, "half_odds_format", None),
            "probability": args.half_probability,
            "ev": args.half_ev,
            "edge_pp": getattr(args, "half_edge_pp", None),
            "firm_count": getattr(args, "half_firm_count", None),
            "market_signal": args.half_market_signal,
            **market_audit_args(args, "half"),
            "settlement_probabilities": settlement_probability_args(args, "half"),
        }
    if args.htft_pick:
        htft_market_probabilities = parse_selection_float_values(
            getattr(args, "htft_market_probability", None),
            "HT/FT market probability",
        )
        htft_edge_values = parse_selection_float_values(
            getattr(args, "htft_edge_pp", None),
            "HT/FT edge_pp",
        )
        htft_market_odds = parse_market_odds_values(
            getattr(args, "htft_market_odds", None),
            "HT/FT market odds",
        )
        record["htft_picks"] = [
            parse_htft_pick(value, getattr(args, "htft_odds_format", None))
            for value in args.htft_pick
        ]
        for pick in record["htft_picks"]:
            selection = str(pick["selection"])
            pick.update(
                {
                    "market_complete": bool(
                        getattr(args, "htft_market_complete", False)
                    ),
                    "market_probability": htft_market_probabilities.get(selection),
                    "complete_market_probabilities": deepcopy(
                        htft_market_probabilities
                    ),
                    "market_source": getattr(args, "htft_market_source", None),
                    "market_collected_at": getattr(
                        args, "htft_market_collected_at", None
                    ),
                    "price_basis": getattr(args, "htft_price_basis", None),
                    "firm_count": getattr(args, "htft_firm_count", None),
                    "market_signal": getattr(args, "htft_market_signal", "unknown"),
                    "edge_pp": htft_edge_values.get(selection),
                    "complete_market_odds": deepcopy(htft_market_odds),
                }
            )
            if pick["edge_pp"] is None and pick.get("market_probability") is not None:
                pick["edge_pp"] = (
                    float(pick["probability"]) - float(pick["market_probability"])
                ) * 100.0
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
            "market_complete": bool(getattr(args, "goal_range_market_complete", False)),
            "market_probability": getattr(args, "goal_range_market_probability", None),
            "market_source": getattr(args, "goal_range_market_source", None),
            "market_collected_at": getattr(
                args, "goal_range_market_collected_at", None
            ),
            "price_basis": getattr(args, "goal_range_price_basis", None),
            "complete_market_odds": parse_market_odds_values(
                getattr(args, "goal_range_market_odds", None),
                "goal_range market odds",
            ),
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
            "market_complete": bool(getattr(args, "btts_market_complete", False)),
            "market_probability": getattr(args, "btts_market_probability", None),
            "market_source": getattr(args, "btts_market_source", None),
            "market_collected_at": getattr(args, "btts_market_collected_at", None),
            "price_basis": getattr(args, "btts_price_basis", None),
            "complete_market_odds": parse_market_odds_values(
                getattr(args, "btts_market_odds", None),
                "btts market odds",
            ),
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
            "complete_market_odds": parse_market_odds_values(
                getattr(args, "corner_total_market_odds", None),
                "corner_total market odds",
            ),
            "settlement_probabilities": {
                "full_win": getattr(args, "corner_total_full_win_probability", None),
                "half_win": getattr(args, "corner_total_half_win_probability", None),
                "push": getattr(args, "corner_total_push_probability", None),
                "half_loss": getattr(args, "corner_total_half_loss_probability", None),
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
            "market_signal": getattr(args, "corner_handicap_market_signal", "unknown"),
            "market_complete": bool(
                getattr(args, "corner_handicap_market_complete", False)
            ),
            "market_probability": getattr(
                args, "corner_handicap_market_probability", None
            ),
            "market_source": getattr(args, "corner_handicap_market_source", None),
            "market_collected_at": getattr(
                args, "corner_handicap_market_collected_at", None
            ),
            "price_basis": getattr(args, "corner_handicap_price_basis", None),
            "complete_market_odds": parse_market_odds_values(
                getattr(args, "corner_handicap_market_odds", None),
                "corner_handicap market odds",
            ),
            "settlement_probabilities": {
                "full_win": getattr(args, "corner_handicap_full_win_probability", None),
                "half_win": getattr(args, "corner_handicap_half_win_probability", None),
                "push": getattr(args, "corner_handicap_push_probability", None),
                "half_loss": getattr(
                    args, "corner_handicap_half_loss_probability", None
                ),
                "loss": getattr(args, "corner_handicap_loss_probability", None),
            },
        }

    provenance = load_score_model_provenance(args)
    record["score_model_provenance"] = provenance
    frozen_score = frozen_registry_model_for_record(
        record,
        role="football_htft",
        league_key=str(record.get("league_key") or "").strip().casefold(),
    )
    if frozen_score is not None and (
        not isinstance(provenance, dict)
        or provenance.get("model_hash")
        != frozen_score[1].get("full_time_component_model_hash")
    ):
        raise ValueError(
            "Canonical score model does not match the cohort-frozen football registry"
        )
    record["joint_scenario_audit"] = load_joint_scenario_audit(args, record)
    if (
        bool(getattr(args, "require_complete_analysis", True))
        and validated_joint_scenario_audit(record) is None
    ):
        raise ValueError(
            "complete analysis requires a valid --joint-scenario-file; "
            "the initial/lineup card was not archived"
        )
    candidate_evaluation = load_candidate_evaluation_audit(args, record)
    if candidate_evaluation is not None:
        record["candidate_audits"].append(candidate_evaluation)
    validate_market_collection_times(record, current)
    apply_primary_role(record, args.primary_market, args.primary_htft_selection)
    if getattr(args, "primary_htft_edge_pp", None) is not None:
        if record.get("primary_market") != "htft":
            raise ValueError(
                "--primary-htft-edge-pp is valid only for an HT/FT primary"
            )
        expected = float(record["primary_pick"]["edge_pp"])
        if abs(float(args.primary_htft_edge_pp) - expected) > EDGE_AUDIT_TOLERANCE_PP:
            raise ValueError(
                "primary HT/FT edge_pp does not match the complete no-vig market"
            )
    validate_provisional_formal_guardrails(record)
    record["evaluation_eligibility"] = validate_score_model_consistency(
        record, provenance
    )
    annotate_confidence_ranking(record)
    apply_primary_role(record, args.primary_market, args.primary_htft_selection)
    validate_primary_is_rank_one(record)
    record["primary_change"] = build_primary_change(record, existing, args)

    ledger_archive = load_forward_validation_ledger_archive(
        args,
        record,
        base_forward_binding,
        archived_at=current,
    )
    if base_forward_binding is not None:
        assert ledger_archive is not None
        validate_active_candidate_evaluation_ledger_binding(record, ledger_archive)
        commitment, committed_binding = build_forward_record_prediction_commitment(
            record, ledger_archive, base_forward_binding
        )
        record["forward_validation_ledger"] = ledger_archive
        record["forward_prediction_commitment"] = commitment
        record["forward_policy_binding"] = committed_binding
        record["archive_version_hash"] = forward_policy._hash_json(
            snapshot_payload(revision_snapshot(record))
        )
        validate_forward_record_prediction_commitment(record)

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
        previous_stage = str(existing.get("analysis_stage") or "initial")
        incoming_stage = str(record.get("analysis_stage") or "initial")
        if not (previous_stage == "initial" and incoming_stage == "lineup-check"):
            raise ValueError(
                "Prediction archive is immutable; only the transition initial -> lineup-check is allowed"
            )
        if not revisions or snapshot_payload(revisions[-1]) != snapshot_payload(
            previous_snapshot
        ):
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


@locked_history_transaction
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
        record["legacy"] = True
        record["backfill"] = True
        record["evaluation_eligibility"] = {
            "policy_version": STRICT_OOS_POLICY_VERSION,
            "strict_forward_oos": False,
            "reason": "legacy_primary_backfill",
        }
        if record.get("status") == "reviewed":
            record["primary_result"] = primary_result_from_record(record)
        if record.get("revisions", []) != revisions_before:
            raise ValueError(
                f"Migration unexpectedly modified revisions for match {match_id}"
            )
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


@locked_history_transaction
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


@locked_history_transaction
def cmd_attach_competition_evidence(args: argparse.Namespace) -> dict[str, Any]:
    """Append source-verified metadata to one still-pending prediction."""

    path = data_path(args.base_dir)
    history = load_history(path)
    record = find_record(history, args.match_id)
    if not isinstance(record, dict):
        raise ValueError(f"No archived match found: {args.match_id}")
    if record.get("mode") != "prematch" or record.get("status") != "pending":
        raise ValueError(
            "competition evidence can be attached only to a pending prematch archive"
        )

    revisions_before = deepcopy(record.get("revisions", []))
    settlement_before = deepcopy(record.get("settlement_basis"))
    archive_hash_before = canonical_prediction_hash(
        snapshot_payload(revision_snapshot(record))
    )
    supplied = build_competition_evidence(
        record,
        competition_key=args.competition_key,
        competition_label=args.competition_label,
        competition_id=args.competition_id,
        verification_source=args.verification_source,
        source_locator=args.source_locator,
        collected_at=args.collected_at,
    )
    existing_raw = record.get("competition_evidence")
    existing = validated_competition_evidence(record)
    legacy_replaced = False
    if (
        existing_raw is not None
        and existing is None
        and (
            not isinstance(existing_raw, dict)
            or existing_raw.get("schema_version") != "titan-fixture-competition/1.0.0"
        )
    ):
        raise ValueError("Archived competition evidence is invalid")
    if isinstance(existing_raw, dict) and existing is None:
        legacy_replaced = True
    duplicate_ignored = False
    if existing is not None:
        same_identity = (
            existing.get("fixture") == supplied.get("fixture")
            and existing.get("competition") == supplied.get("competition")
            and existing.get("source", {}).get("url")
            == supplied.get("source", {}).get("url")
            and existing.get("source", {}).get("locator")
            == supplied.get("source", {}).get("locator")
        )
        if not same_identity:
            raise ValueError("Conflicting competition evidence is already archived")
        duplicate_ignored = True
    else:
        record["competition_evidence"] = supplied
        revisions = record.setdefault("metadata_revisions", [])
        if not isinstance(revisions, list):
            raise ValueError("Archived metadata revision log is invalid")
        revisions.append(
            {
                "kind": "competition_evidence",
                "attached_at": supplied["source"]["collected_at"],
                "previous_evidence_hash": (
                    existing_raw.get("evidence_hash")
                    if isinstance(existing_raw, dict)
                    else None
                ),
                "evidence_hash": supplied["evidence_hash"],
                "source_page_sha256": supplied["source"]["page_sha256"],
            }
        )

    if record.get("revisions", []) != revisions_before:
        raise ValueError(
            "Competition evidence unexpectedly modified prediction revisions"
        )
    if record.get("settlement_basis") != settlement_before:
        raise ValueError("Competition evidence unexpectedly modified settlement data")
    archive_hash_after = canonical_prediction_hash(
        snapshot_payload(revision_snapshot(record))
    )
    if args.write and not duplicate_ignored:
        save_history(path, history)
    return {
        "ok": True,
        "path": str(path),
        "written": bool(args.write and not duplicate_ignored),
        "duplicate_ignored": duplicate_ignored,
        "legacy_replaced": legacy_replaced,
        "match_id": str(record.get("match_id")),
        "competition_evidence": existing or supplied,
        "archive_version_hash_before": archive_hash_before,
        "archive_version_hash_after": archive_hash_after,
        "archive_version_hash_changed": archive_hash_before != archive_hash_after,
        "prediction_fields_unchanged": True,
    }


@locked_history_transaction
def cmd_migrate_settlement_basis(args: argparse.Namespace) -> dict[str, Any]:
    """Backfill settlement audit metadata without re-grading reviewed records."""
    path = data_path(args.base_dir)
    history = load_history(path)
    changed: list[str] = []
    for record in history:
        if record.get("mode") != "prematch" or record.get("status") != "reviewed":
            continue
        before = deepcopy(record)
        existing_basis = record.get("settlement_basis")
        if isinstance(existing_basis, dict):
            basis = deepcopy(existing_basis)
        else:
            basis = settlement_basis_for_record(record)

        missing_identity_fields = [
            field
            for field in ("source_url", "league", "league_key", "competition_evidence")
            if field not in basis
        ]
        if not isinstance(existing_basis, dict):
            # A top-level evidence object on a legacy reviewed record cannot
            # prove that it existed when the match was settled.  Do not promote
            # it into the immutable basis during compatibility migration.
            basis["competition_evidence"] = None
            missing_identity_fields = [
                "source_url",
                "league",
                "league_key",
                "competition_evidence",
            ]

        if not missing_identity_fields:
            continue

        for field in ("source_url", "league", "league_key"):
            if field not in basis:
                basis[field] = deepcopy(record.get(field))
        if "competition_evidence" not in basis:
            basis["competition_evidence"] = None
        basis["competition_identity_migration"] = {
            "schema_version": SETTLEMENT_IDENTITY_MIGRATION_SCHEMA_VERSION,
            "source": "legacy_reviewed_record_top_level_at_migration",
            "migrated_at": utc_now().isoformat(),
            "frozen_fields": ["source_url", "league", "league_key"],
            "competition_evidence_status": (
                "preserved_from_existing_settlement_basis"
                if isinstance(existing_basis, dict)
                and "competition_evidence" in existing_basis
                and existing_basis.get("competition_evidence") is not None
                else "unavailable_in_legacy_settlement_basis"
            ),
        }
        record["settlement_basis"] = basis
        without_basis = deepcopy(record)
        without_basis.pop("settlement_basis", None)
        before_without_basis = deepcopy(before)
        before_without_basis.pop("settlement_basis", None)
        if without_basis != before_without_basis:
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


@locked_history_transaction
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
            "primary" if counts_toward_primary_record else "no_primary_observation"
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
                basis["counts_toward_primary_record"] = counts_toward_primary_record
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


def settle_candidate_observations(
    candidate_audits: list[dict[str, Any]],
    *,
    half_home: int | None,
    half_away: int | None,
    home: int,
    away: int,
    home_corners: int | None = None,
    away_corners: int | None = None,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for audit in candidate_audits:
        if not isinstance(audit, dict):
            continue
        if audit.get("kind") == CANDIDATE_EVALUATION_KIND:
            candidate_results: list[dict[str, Any]] = []
            shadow_results: dict[str, str | None] = {}
            for candidate in audit.get("candidates", []):
                if not isinstance(candidate, dict):
                    continue
                market = candidate.get("market")
                pick = _candidate_pick(candidate)
                raw_result: str | None
                if market == "asian":
                    raw_result = settle_asian(pick, home, away)
                elif market == "total":
                    raw_result = settle_total(pick, home, away)
                elif market == "half_time":
                    raw_result = (
                        settle_half_time(pick, half_home, half_away)
                        if half_home is not None and half_away is not None
                        else None
                    )
                elif market == "htft":
                    raw_result = (
                        settle_htft([pick], half_home, half_away, home, away)[0]
                        if half_home is not None and half_away is not None
                        else None
                    )
                elif market == "goal_range":
                    raw_result = settle_goal_range(pick, home, away)
                elif market == "btts":
                    raw_result = settle_btts(pick, home, away)
                elif market == "corner_total":
                    raw_result = (
                        settle_corner_total(pick, home_corners, away_corners)
                        if home_corners is not None and away_corners is not None
                        else None
                    )
                elif market == "corner_handicap":
                    raw_result = (
                        settle_corner_handicap(pick, home_corners, away_corners)
                        if home_corners is not None and away_corners is not None
                        else None
                    )
                else:
                    raw_result = None
                settlement_result = "full_win" if raw_result == "win" else raw_result
                item = {
                    "candidate_id": candidate.get("candidate_id"),
                    "market": market,
                    "settlement_result": settlement_result,
                    "counterfactual_eligible": candidate.get("counterfactual_eligible")
                    is True,
                    "formal_eligible": candidate.get("formal_eligible") is True,
                    "shadow_selected": candidate.get("shadow_selected") is True,
                    "counts_toward_primary_record": False,
                    "monetary_scope": "none",
                }
                candidate_results.append(item)
                if item["shadow_selected"]:
                    shadow_results[str(market)] = settlement_result
            graded = sum(
                item["settlement_result"] is not None for item in candidate_results
            )
            if graded == len(candidate_results) and candidate_results:
                status = "graded_observation"
            elif graded:
                status = "partially_graded_observation"
            else:
                status = "ungraded_missing_required_result"
            diagnostics.append(
                {
                    "observation_id": audit.get("observation_id"),
                    "kind": CANDIDATE_EVALUATION_KIND,
                    "market": "multi_market",
                    "status": status,
                    "candidate_results": candidate_results,
                    "shadow_selection_results": shadow_results,
                    "graded_candidates": graded,
                    "ungraded_candidates": len(candidate_results) - graded,
                    "counts_toward_primary_record": False,
                    "monetary_scope": "none",
                }
            )
            continue
        if audit.get("kind") == CORNER_OBSERVATION_KIND:
            candidates = [
                item for item in audit.get("candidates", []) if isinstance(item, dict)
            ]
            best = audit.get("best_observation")
            best_candidate_id = (
                best.get("candidate_id") if isinstance(best, dict) else None
            )
            corner_base = {
                "observation_id": audit.get("observation_id"),
                "kind": CORNER_OBSERVATION_KIND,
                "market": "corner_markets",
                "counts_toward_primary_record": False,
                "monetary_scope": "none",
            }
            if home_corners is None or away_corners is None:
                diagnostics.append(
                    {
                        **corner_base,
                        "status": "ungraded_missing_corner_score",
                        "reason": "verified_90_minute_corner_counts_unavailable",
                        "home_corners": None,
                        "away_corners": None,
                        "best_observation_result": None,
                        "candidate_results": [
                            {
                                "candidate_id": item.get("candidate_id"),
                                "market": item.get("market"),
                                "side": item.get("side"),
                                "line": item.get("line"),
                                "settlement_result": None,
                                "is_best_observation": (
                                    item.get("candidate_id") == best_candidate_id
                                ),
                            }
                            for item in candidates
                        ],
                    }
                )
                continue

            candidate_results: list[dict[str, Any]] = []
            best_result = None
            for candidate in candidates:
                market = candidate.get("market")
                if market == "corner_total":
                    raw_result = settle_corner_total(
                        candidate, home_corners, away_corners
                    )
                elif market == "corner_handicap":
                    raw_result = settle_corner_handicap(
                        candidate, home_corners, away_corners
                    )
                else:
                    continue
                settlement_result = "full_win" if raw_result == "win" else raw_result
                is_best = candidate.get("candidate_id") == best_candidate_id
                if is_best:
                    best_result = settlement_result
                candidate_results.append(
                    {
                        "candidate_id": candidate.get("candidate_id"),
                        "market": market,
                        "side": candidate.get("side"),
                        "line": candidate.get("line"),
                        "settlement_result": settlement_result,
                        "is_best_observation": is_best,
                    }
                )
            diagnostics.append(
                {
                    **corner_base,
                    "status": "graded_observation",
                    "reason": None,
                    "home_corners": home_corners,
                    "away_corners": away_corners,
                    "best_observation_result": best_result,
                    "candidate_results": candidate_results,
                }
            )
            continue
        if audit.get("market") != "htft":
            continue
        base = {
            "observation_id": audit.get("observation_id"),
            "market": "htft",
            "counts_toward_primary_record": False,
            "monetary_scope": "none",
        }
        if half_home is None or half_away is None:
            diagnostics.append(
                {
                    **base,
                    "status": "ungraded_missing_half_time_score",
                    "actual_selection": None,
                    "top1_hit": None,
                    "top2_hit": None,
                    "nine_class_brier": None,
                    "nine_class_log_loss": None,
                }
            )
            continue
        matrix = validate_htft_matrix(audit.get("matrix"), "archived HT/FT matrix")
        actual = result_code(half_home, half_away) + result_code(home, away)
        top_two = [item for item in audit.get("top_two", []) if isinstance(item, dict)]
        selections = [str(item.get("selection") or "").upper() for item in top_two]
        actual_probability = matrix[actual]
        diagnostics.append(
            {
                **base,
                "status": "graded_observation",
                "actual_selection": actual,
                "actual_probability": actual_probability,
                "top1_selection": selections[0] if selections else None,
                "top2_selections": selections[:2],
                "top1_hit": bool(selections and selections[0] == actual),
                "top2_hit": actual in selections[:2],
                "nine_class_brier": math.fsum(
                    (matrix[outcome] - (1.0 if outcome == actual else 0.0)) ** 2
                    for outcome in HTFT_OUTCOMES
                ),
                "nine_class_log_loss": -math.log(
                    max(actual_probability, HTFT_LOG_LOSS_FLOOR)
                ),
                "candidate_results": [
                    {
                        "slot": item.get("slot"),
                        "selection": str(item.get("selection") or "").upper(),
                        "observed_hit": str(item.get("selection") or "").upper()
                        == actual,
                    }
                    for item in top_two[:2]
                ],
            }
        )
    return diagnostics


@locked_history_transaction
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
        league_key = competition_key_for_record(record)
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
        raise ValueError(
            "Review requires a concise non-empty --key-learning grounded in the verified result"
        )

    home, away = int(args.home_score), int(args.away_score)
    if home < 0 or away < 0:
        raise ValueError("Verified final scores cannot be negative")
    verification_source = str(getattr(args, "verification_source", "") or "").strip()
    if not verification_source:
        raise ValueError("Review requires --verification-source")
    verification_collected_text = str(
        getattr(args, "verification_collected_at", "") or ""
    ).strip()
    verification_collected = parse_aware_datetime(
        verification_collected_text,
        "verification_collected_at",
    )
    if verification_collected.astimezone(timezone.utc) > utc_now().astimezone(
        timezone.utc
    ):
        raise ValueError("verification_collected_at cannot be in the future")
    if verification_collected.astimezone(timezone.utc) < parse_datetime(
        str(record.get("kickoff") or "")
    ):
        raise ValueError("verification_collected_at must be at or after kickoff")
    settlement_basis = settlement_basis_for_record(record)
    if settlement_basis.get("forward_policy_binding") is not None:
        try:
            validated_review_binding = forward_policy.validate_record_binding(
                settlement_basis["forward_policy_binding"]
            )
        except forward_policy.ForwardPolicyError as exc:
            raise ValueError(
                "Review refused: frozen forward-policy binding is invalid"
            ) from exc
        if validated_review_binding is not None and validated_review_binding.get(
            "schema_version"
        ) in {
            forward_policy.PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
            forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
        }:
            try:
                validate_forward_record_prediction_commitment(record)
            except ValueError as exc:
                raise ValueError(
                    "Review refused: frozen forward prediction commitment is invalid"
                ) from exc
        if validated_review_binding is not None and validated_review_binding.get(
            "schema_version"
        ) in {
            forward_policy.PREVIOUS_PROVENANCE_RECORD_BINDING_SCHEMA_VERSION,
            forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
        }:
            try:
                validate_forward_record_prediction_commitment(record)
            except ValueError as exc:
                raise ValueError(
                    "Review refused: historical forward prediction commitment is invalid"
                ) from exc
    if settlement_basis.get("forward_policy_binding") is not None:
        if validated_source_evidence_audit(settlement_basis) is None:
            raise ValueError(
                "Review refused: forward-bound records require replayable archived source evidence"
            )
        review_policy = (
            validated_review_binding.get("policy_snapshot")
            if isinstance(validated_review_binding, dict)
            else None
        )
        if (
            isinstance(review_policy, dict)
            and forward_policy._policy_uses_role_aware_lineage(review_policy)
            and validated_fundamental_evidence_audit(settlement_basis) is None
        ):
            raise ValueError(
                "Review refused: current forward-bound records require replayable "
                "fundamental evidence"
            )
    elif settlement_basis.get("source_evidence_audit") is not None and (
        validated_source_evidence_audit(settlement_basis) is None
    ):
        raise ValueError("Review refused: archived source evidence cannot be replayed")
    elif settlement_basis.get("fundamental_evidence_audit") is not None and (
        validated_fundamental_evidence_audit(settlement_basis) is None
    ):
        raise ValueError(
            "Review refused: archived fundamental evidence cannot be replayed"
        )
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
    if (args.half_home_score is None) != (args.half_away_score is None):
        raise ValueError(
            "Half-time score verification requires both --half-home-score and --half-away-score"
        )
    half_scores_available = (
        args.half_home_score is not None and args.half_away_score is not None
    )
    half_home = int(args.half_home_score) if half_scores_available else None
    half_away = int(args.half_away_score) if half_scores_available else None
    if half_scores_available and (half_home < 0 or half_away < 0):
        raise ValueError("Verified half-time scores cannot be negative")
    if half_scores_available and (half_home > home or half_away > away):
        raise ValueError(
            "Verified half-time home/away goals cannot exceed the corresponding "
            "full-time home/away goals"
        )
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
        "primary" if counts_toward_primary_record else "no_primary_observation"
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
    candidate_audits = [
        audit
        for audit in settlement_basis.get("candidate_audits", [])
        if isinstance(audit, dict)
    ]
    if any(
        audit.get("kind") == CANDIDATE_EVALUATION_KIND
        and not validated_candidate_evaluation_audit(audit, record)
        for audit in candidate_audits
    ):
        raise ValueError(
            "Review refused: archived candidate evaluation cannot be replayed from its frozen inputs"
        )
    observation_diagnostics = settle_candidate_observations(
        candidate_audits,
        half_home=half_home,
        half_away=half_away,
        home=home,
        away=away,
        home_corners=home_corners,
        away_corners=away_corners,
    )
    settlement_basis["primary_result"] = primary_result
    settlement_basis["counts_toward_primary_record"] = counts_toward_primary_record
    settlement_basis["observation_diagnostics"] = deepcopy(observation_diagnostics)
    record.update(
        {
            "status": "reviewed",
            "reviewed_at": now_iso(),
            "result_verification": {
                "verified_finished": True,
                "source": verification_source,
                "collected_at": verification_collected.isoformat(),
            },
            "final_score": f"{home}-{away}",
            "score_exact": predicted_exact,
            "exact_score_hit_rank": exact_score_hit_rank,
            "exact_score_any_hit": exact_score_hit_rank in {1, 2},
            "display_score_exact": display_predicted_exact,
            "display_exact_score_hit_rank": display_exact_score_hit_rank,
            "display_exact_score_any_hit": display_exact_score_hit_rank in {1, 2},
            "asian_result": primary_result if primary_market == "asian" else None,
            "total_result": primary_result if primary_market == "total" else None,
            "half_time_score": f"{half_home}-{half_away}"
            if half_scores_available
            else None,
            "half_time_result": primary_result
            if primary_market == "half_time"
            else None,
            "htft_results": [],
            "goal_range_result": primary_result
            if primary_market == "goal_range"
            else None,
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
            "observation_diagnostics": observation_diagnostics,
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
            "competition_key": competition_key_for_record(record),
            "settlement_basis": settlement_basis,
        }
    )
    warnings = []
    save_history(path, history)
    stats = calculate_stats(history)
    league_key = competition_key_for_record(record)
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


def probability_calibration_block(
    pairs: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    states = ("full_win", "half_win", "push", "half_loss", "loss")
    actual_state = {
        "win": "full_win",
        "half_win": "half_win",
        "push": "push",
        "half_loss": "half_loss",
        "loss": "loss",
    }
    multiclass_briers: list[float] = []
    multiclass_log_losses: list[float] = []
    binary_observations: list[tuple[float, float]] = []
    positive_observations: list[tuple[float, float]] = []
    for result, pick in pairs:
        distribution = pick.get("settlement_probabilities")
        if isinstance(distribution, dict) and all(
            state in distribution for state in states
        ):
            try:
                probabilities = {state: float(distribution[state]) for state in states}
            except (TypeError, ValueError):
                probabilities = {}
            if (
                probabilities
                and all(
                    math.isfinite(value) and 0.0 <= value <= 1.0
                    for value in probabilities.values()
                )
                and abs(sum(probabilities.values()) - 1.0)
                <= PROBABILITY_AUDIT_TOLERANCE
                and result in actual_state
            ):
                observed_state = actual_state[result]
                multiclass_briers.append(
                    sum(
                        (
                            probabilities[state]
                            - (1.0 if state == observed_state else 0.0)
                        )
                        ** 2
                        for state in states
                    )
                    / len(states)
                )
                multiclass_log_losses.append(
                    -math.log(max(probabilities[observed_state], 1e-15))
                )
                positive_probability = (
                    probabilities["full_win"] + probabilities["half_win"]
                )
                positive_observations.append(
                    (
                        positive_probability,
                        1.0 if result in {"win", "half_win"} else 0.0,
                    )
                )
                continue
        value = pick.get("probability")
        if result != "push" and value is not None:
            try:
                probability = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(probability) and 0.0 <= probability <= 1.0:
                binary_observations.append(
                    (probability, 1.0 if result in {"win", "half_win"} else 0.0)
                )
                positive_observations.append(binary_observations[-1])
    if not positive_observations:
        return {
            "samples": 0,
            "avg_predicted_probability": None,
            "observed_positive_rate": None,
            "calibration_gap": None,
            "brier_score": None,
            "log_loss": None,
            "diagnosis": "insufficient_data",
            "method": "no_usable_probabilities",
            "five_state_samples": 0,
            "legacy_binary_samples": 0,
        }
    average = sum(value for value, _ in positive_observations) / len(
        positive_observations
    )
    observed_rate = sum(value for _, value in positive_observations) / len(
        positive_observations
    )
    gap = average - observed_rate
    legacy_brier = (
        sum(
            (probability - observed) ** 2
            for probability, observed in binary_observations
        )
        / len(binary_observations)
        if binary_observations
        else None
    )
    brier = (
        sum(multiclass_briers) / len(multiclass_briers)
        if multiclass_briers
        else legacy_brier
    )
    log_loss = (
        sum(multiclass_log_losses) / len(multiclass_log_losses)
        if multiclass_log_losses
        else None
    )
    diagnosis = (
        "overconfident"
        if gap > 0.05
        else "underconfident"
        if gap < -0.05
        else "roughly_aligned"
    )
    return {
        "samples": len(positive_observations),
        "avg_predicted_probability": round(average, 4),
        "observed_positive_rate": round(observed_rate, 4),
        "calibration_gap": round(gap, 4),
        "brier_score": round(brier, 4),
        "log_loss": round(log_loss, 4) if log_loss is not None else None,
        "diagnosis": diagnosis,
        "method": (
            "five_state_multiclass_mean_brier"
            if multiclass_briers and not binary_observations
            else "mixed_five_state_and_legacy_binary"
            if multiclass_briers
            else "legacy_binary_approximation"
        ),
        "five_state_samples": len(multiclass_briers),
        "legacy_binary_samples": len(binary_observations),
        "legacy_binary_brier": round(legacy_brier, 4)
        if legacy_brier is not None
        else None,
    }


def settlement_profit(result: str, odds: Any, odds_format: Any = None) -> float | None:
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
    archived_evs = [
        float(pick["ev"]) for _, pick in pairs if pick.get("ev") is not None
    ]
    if calculate_money:
        profits = [
            settlement_profit(result, pick.get("odds"), pick.get("odds_format"))
            for result, pick in pairs
        ]
        settled_profits = [value for value in profits if value is not None]
        block.update(
            {
                "monetary_scope": "primary_only",
                "stake_units": len(settled_profits),
                "profit_units": round(sum(settled_profits), 4),
                "roi": round(sum(settled_profits) / len(settled_profits), 4)
                if settled_profits
                else None,
            }
        )
    else:
        block.update(
            {
                "monetary_scope": "not_tracked",
                "stake_units": None,
                "profit_units": None,
                "roi": None,
            }
        )
    block["avg_archived_ev"] = (
        round(sum(archived_evs) / len(archived_evs), 4) if archived_evs else None
    )
    block["probability_calibration"] = probability_calibration_block(pairs)
    signals: dict[str, dict[str, Any]] = {}
    for signal in sorted(
        {str(pick.get("market_signal", "unknown")) for _, pick in pairs}
    ):
        subset = [
            (result, pick)
            for result, pick in pairs
            if str(pick.get("market_signal", "unknown")) == signal
        ]
        signals[signal] = performance_block_without_signals(
            subset, calculate_money=calculate_money
        )
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
            settlement_profit(result, pick.get("odds"), pick.get("odds_format"))
            for result, pick in pairs
        ]
        settled_profits = [value for value in profits if value is not None]
        block.update(
            {
                "monetary_scope": "primary_only",
                "stake_units": len(settled_profits),
                "profit_units": round(sum(settled_profits), 4),
                "roi": round(sum(settled_profits) / len(settled_profits), 4)
                if settled_profits
                else None,
            }
        )
    else:
        block.update(
            {
                "monetary_scope": "not_tracked",
                "stake_units": None,
                "profit_units": None,
                "roi": None,
            }
        )
    block["probability_calibration"] = probability_calibration_block(pairs)
    return block


def is_strict_forward_oos_record(record: dict[str, Any]) -> bool:
    basis = record.get("settlement_basis")
    if isinstance(basis, dict):
        # A reviewed record is graded from its frozen active version.  Never
        # let later top-level drift upgrade or downgrade the evaluation cohort.
        eligibility = basis.get("evaluation_eligibility")
    else:
        eligibility = record.get("evaluation_eligibility")
    if isinstance(eligibility, dict) and isinstance(
        eligibility.get("strict_forward_oos"), bool
    ):
        return bool(eligibility["strict_forward_oos"])
    return False


def forward_policy_binding_for_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return a self-consistent committed binding, including historical replay."""

    basis = record.get("settlement_basis")
    binding = (
        basis.get("forward_policy_binding")
        if isinstance(basis, dict)
        else record.get("forward_policy_binding")
    )
    try:
        validated = forward_policy.validate_record_binding(binding)
        if validated is None or validated.get("schema_version") not in {
            forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
            forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION,
        }:
            return None
        validate_forward_record_prediction_commitment(record)
        return validated
    except (forward_policy.ForwardPolicyError, ValueError):
        return None


def untouched_live_forward_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep a truly frozen confirmation cohort separate from generic strict-OOS data."""

    bound_records = [
        (record, forward_policy_binding_for_record(record))
        for record in records
        if is_strict_forward_oos_record(record)
    ]
    historical = [
        (record, binding)
        for record, binding in bound_records
        if binding is not None
        and binding.get("schema_version")
        == forward_policy.PREVIOUS_PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
    ]
    eligible = [
        record
        for record, binding in bound_records
        if binding is not None
        and binding.get("schema_version")
        == forward_policy.PROVENANCE_COMMITTED_RECORD_BINDING_SCHEMA_VERSION
        and validated_source_evidence_audit(record) is not None
    ]
    cohorts: dict[str, dict[str, Any]] = {}
    for record in eligible:
        binding = forward_policy_binding_for_record(record)
        assert binding is not None
        cohort_id = str(binding["cohort_id"])
        block = cohorts.setdefault(
            cohort_id,
            {
                "cohort_id": cohort_id,
                "cohort_starts_at": binding["cohort_starts_at"],
                "policy_id": binding["policy_id"],
                "policy_hash": binding["policy_hash"],
                "records": [],
            },
        )
        if (
            block["policy_id"] != binding["policy_id"]
            or block["policy_hash"] != binding["policy_hash"]
        ):
            raise ValueError(
                f"untouched cohort {cohort_id} contains more than one frozen policy"
            )
        block["records"].append(record)
    rendered: dict[str, Any] = {}
    for cohort_id, block in cohorts.items():
        cohort_records = block.pop("records")
        selection = selection_policy_block(cohort_records)
        proper_scores = one_x_two_metrics(cohort_records)
        primary = performance_block(primary_pairs(cohort_records))
        blockers = [
            "same_time_bookmaker_no_vig_comparison_not_yet_demonstrated",
            "clustered_confidence_interval_not_yet_demonstrated",
            "executable_price_slippage_and_clv_not_yet_demonstrated",
        ]
        rendered[cohort_id] = {
            **block,
            "reviewed_matches": len(cohort_records),
            "primary": primary,
            "primary_by_market": primary_market_performance(cohort_records),
            "selection_policy": selection,
            "one_x_two_metrics": proper_scores,
            "baseline_status": {
                "historical_frequency": "available_in_component_evaluations",
                "independent_ht_x_ft": "available_in_component_evaluations",
                "simple_poisson_dixon_coles": "available_in_component_evaluations",
                "same_time_bookmaker_no_vig": "required_not_yet_demonstrated",
                "closing_line_and_clv": "required_not_yet_demonstrated",
            },
            "promotion_eligible": False,
            "promotion_blockers": blockers,
        }
    return {
        "evaluation_scope": "untouched_live_forward_frozen_policy",
        "reviewed_matches": len(eligible),
        "cohort_count": len(rendered),
        "cohorts": rendered,
        "historical_quarantine": {
            "record_count": len(historical),
            "cohort_ids": sorted(
                {str(binding["cohort_id"]) for _record, binding in historical}
            ),
            "reason": "forward_observations_v2_defective_historical_read_only",
            "formal_export_eligible": False,
            "promotion_evidence_eligible": False,
        },
        "promotion_is_manual": True,
        "no_claim_without_same_time_bookmaker_baseline": True,
    }


def strict_forward_exclusion_reason(record: dict[str, Any]) -> str | None:
    if is_strict_forward_oos_record(record):
        return None
    basis = record.get("settlement_basis")
    eligibility = (
        basis.get("evaluation_eligibility")
        if isinstance(basis, dict)
        else record.get("evaluation_eligibility")
    )
    if isinstance(eligibility, dict) and eligibility.get("reason"):
        return str(eligibility["reason"])
    if record.get("legacy") or record.get("backfill"):
        return "legacy_or_backfill"
    primary_change = record.get("primary_change")
    if (
        isinstance(primary_change, dict)
        and primary_change.get("status") == "backfilled"
    ):
        return "legacy_primary_backfill"
    return "legacy_schema_missing_explicit_eligibility"


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
        "asian": performance_block(asian),
        "totals": performance_block(totals),
        "half_time": performance_block(half_time),
        "htft": performance_block(htft),
        "goal_range": performance_block(goal_range),
        "btts": performance_block(btts),
        "corner_total": performance_block(corner_total),
        "corner_handicap": performance_block(corner_handicap),
        "combined": performance_block(combined),
    }


def exact_score_diagnostics(
    records: list[dict[str, Any]],
    *,
    display: bool = False,
) -> dict[str, Any]:
    strict_records = strict_forward_reviewed_records(records)
    model_records = [
        record
        for record in strict_records
        if has_validated_score_model_provenance(record)
    ]

    def rank_for(record: dict[str, Any]) -> Any:
        if display and "display_exact_score_hit_rank" in record:
            return record.get("display_exact_score_hit_rank")
        return record.get("exact_score_hit_rank")

    def exact_for(record: dict[str, Any]) -> bool:
        if display and "display_score_exact" in record:
            return bool(record.get("display_score_exact"))
        return bool(record.get("score_exact"))

    top1 = sum((rank_for(r) == 1) or exact_for(r) for r in model_records)
    top2 = sum(
        (rank_for(r) in {1, 2}) or (rank_for(r) is None and exact_for(r))
        for r in model_records
    )
    excluded = len(strict_records) - len(model_records)
    return {
        "samples": len(model_records),
        "excluded": excluded,
        "excluded_reasons": (
            {"missing_validated_score_model_provenance": excluded} if excluded else {}
        ),
        "top1_hits": top1,
        "top1_rate": round(top1 / len(model_records), 4) if model_records else None,
        "top2_hits": top2,
        "top2_rate": round(top2 / len(model_records), 4) if model_records else None,
    }


def learning_scope_for_record(record: dict[str, Any]) -> str:
    explicit = str(record.get("learning_scope", "")).strip()
    if explicit in {"primary", "no_primary_observation"}:
        return explicit
    _, primary = primary_snapshot_for_stats(record)
    return "primary" if isinstance(primary, dict) else "no_primary_observation"


def learning_sample_summary(records: list[dict[str, Any]]) -> dict[str, int]:
    samples = [
        record for record in records if str(record.get("key_learning", "")).strip()
    ]
    primary = sum(learning_scope_for_record(record) == "primary" for record in samples)
    no_primary = sum(
        learning_scope_for_record(record) == "no_primary_observation"
        for record in samples
    )
    return {
        "total": len(samples),
        "primary": primary,
        "no_primary_observation": no_primary,
    }


def strict_forward_reviewed_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the only cohort eligible for forward-performance headlines."""
    return [
        record
        for record in records
        if record.get("mode") == "prematch"
        and record.get("status") == "reviewed"
        and is_strict_forward_oos_record(record)
    ]


def selection_policy_block(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure selection coverage without conditioning on whether a bet existed."""
    eligible = strict_forward_reviewed_records(records)
    selected = 0
    for record in eligible:
        market, primary = primary_snapshot_for_stats(record)
        if market and isinstance(primary, dict):
            selected += 1
    abstained = len(eligible) - selected
    return {
        "evaluation_scope": "strict_forward_oos_reviewed",
        "policy_version": CONFIDENCE_POLICY_VERSION,
        "selection_basis": PRIMARY_SELECTION_BASIS,
        "eligible_reviewed_matches": len(eligible),
        "selected_primary_matches": selected,
        "abstained_matches": abstained,
        "coverage": round(selected / len(eligible), 4) if eligible else None,
        "abstention_rate": round(abstained / len(eligible), 4) if eligible else None,
    }


def has_validated_score_model_provenance(record: dict[str, Any]) -> bool:
    """Recognize a frozen canonical-model cohort without trusting a label alone."""
    basis = record.get("settlement_basis")
    if isinstance(basis, dict):
        eligibility = basis.get("evaluation_eligibility")
        provenance = basis.get("score_model_provenance")
    else:
        eligibility = record.get("evaluation_eligibility")
        provenance = record.get("score_model_provenance")

    if not (
        isinstance(eligibility, dict)
        and eligibility.get("strict_forward_oos") is True
        and eligibility.get("reason") == "validated_score_model_provenance"
        and isinstance(provenance, dict)
    ):
        return False
    snapshot = provenance.get("snapshot")
    matrix = provenance.get("score_matrix")
    return bool(
        re.fullmatch(r"sha256:[0-9a-f]{64}", str(provenance.get("model_hash") or ""))
        and re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(provenance.get("artifact_sha256") or "")
        )
        and isinstance(snapshot, dict)
        and snapshot.get("artifact_type") == "soccer_score_prediction"
        and isinstance(matrix, list)
        and matrix
    )


def one_x_two_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate strict-OOS 1X2 proper scores and quarantine malformed rows."""
    eligible = strict_forward_reviewed_records(records)
    brier_scores: list[float] = []
    log_losses: list[float] = []
    excluded_reasons: dict[str, int] = {}

    def exclude(reason: str) -> None:
        excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1

    for record in eligible:
        if not has_validated_score_model_provenance(record):
            exclude("missing_validated_score_model_provenance")
            continue
        final_score = record.get("final_score")
        if final_score is None or not str(final_score).strip():
            exclude("missing_final_score")
            continue
        score_match = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", str(final_score))
        if not score_match:
            exclude("invalid_final_score")
            continue

        raw_probabilities = record.get("probabilities")
        if not isinstance(raw_probabilities, dict):
            exclude(
                "missing_1x2_probabilities"
                if raw_probabilities is None
                else "invalid_1x2_probabilities"
            )
            continue
        labels = ("home_win", "draw", "away_win")
        if any(label not in raw_probabilities for label in labels):
            exclude("missing_1x2_probabilities")
            continue
        values = [raw_probabilities[label] for label in labels]
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
            for value in values
        ):
            exclude("invalid_1x2_probabilities")
            continue
        probabilities = {label: float(raw_probabilities[label]) for label in labels}
        if abs(math.fsum(probabilities.values()) - 1.0) > PROBABILITY_AUDIT_TOLERANCE:
            exclude("non_normalized_1x2_probabilities")
            continue

        home_score, away_score = (int(value) for value in score_match.groups())
        actual = (
            "home_win"
            if home_score > away_score
            else "draw"
            if home_score == away_score
            else "away_win"
        )
        brier_scores.append(
            math.fsum(
                (probabilities[label] - (1.0 if label == actual else 0.0)) ** 2
                for label in labels
            )
        )
        log_losses.append(
            -math.log(max(probabilities[actual], ONE_X_TWO_LOG_LOSS_FLOOR))
        )

    sample_count = len(brier_scores)
    excluded_count = len(eligible) - sample_count
    return {
        "evaluation_scope": "strict_forward_oos_reviewed",
        "strict_reviewed_matches": len(eligible),
        "sample_count": sample_count,
        "excluded_count": excluded_count,
        "excluded_reasons": dict(sorted(excluded_reasons.items())),
        "one_x_two_multiclass_brier": (
            math.fsum(brier_scores) / sample_count if sample_count else None
        ),
        "one_x_two_multiclass_log_loss": (
            math.fsum(log_losses) / sample_count if sample_count else None
        ),
        "definitions": {
            "one_x_two_multiclass_brier": (
                "sum of squared error over home/draw/away, averaged by match"
            ),
            "one_x_two_multiclass_log_loss": (
                "negative natural log of the observed 1X2 outcome probability, "
                "averaged by match"
            ),
            "log_loss_floor": ONE_X_TWO_LOG_LOSS_FLOOR,
        },
    }


def frozen_candidate_audits(record: dict[str, Any]) -> list[dict[str, Any]]:
    basis = record.get("settlement_basis")
    raw = (
        basis.get("candidate_audits", [])
        if isinstance(basis, dict) and "candidate_audits" in basis
        else record.get("candidate_audits", [])
    )
    return (
        [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, list)
        else []
    )


def frozen_observation_diagnostics(
    record: dict[str, Any],
) -> list[dict[str, Any]]:
    basis = record.get("settlement_basis")
    raw = (
        basis.get("observation_diagnostics", [])
        if isinstance(basis, dict) and "observation_diagnostics" in basis
        else record.get("observation_diagnostics", [])
    )
    return (
        [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, list)
        else []
    )


def _replay_candidate_evaluation_audit(
    audit: dict[str, Any], record: dict[str, Any]
) -> bool:
    context = _candidate_evaluation_record_context(record)
    artifact = audit.get("artifact")
    if not isinstance(artifact, dict):
        return False
    source = artifact.get("source_payload")
    if not isinstance(source, dict):
        return False
    try:
        calculated_source_hash = calculate_candidate_evaluation_source_hash(source)
        if artifact.get("source_payload_hash") != calculated_source_hash:
            return False
        source_schema = source.get("schema_version")
        if (
            source.get("artifact_type") != CANDIDATE_EVALUATION_ARTIFACT_TYPE
            or source_schema
            not in {
                CANDIDATE_EVALUATION_SCHEMA_VERSION,
                LEGACY_CANDIDATE_EVALUATION_SCHEMA_VERSION,
            }
            or audit.get("schema_version") != source_schema
            or artifact.get("schema_version") != source_schema
            or source.get("policy_version") != STRICT_OOS_POLICY_VERSION
            or source.get("selection_policy_version") != CONFIDENCE_POLICY_VERSION
        ):
            return False
        fixture = source.get("fixture")
        if not isinstance(fixture, dict):
            return False
        source_kickoff = parse_aware_datetime(
            str(fixture.get("kickoff") or ""),
            "candidate evaluation source fixture kickoff",
        )
        context_kickoff = parse_aware_datetime(
            str(context.get("kickoff") or ""),
            "candidate evaluation frozen fixture kickoff",
        )
        if (
            str(fixture.get("match_id") or "")
            != str(context.get("match_id") or context.get("fixture_id") or "")
            or fixture.get("home_team") != context.get("home_team")
            or fixture.get("away_team") != context.get("away_team")
            or source_kickoff.astimezone(timezone.utc)
            != context_kickoff.astimezone(timezone.utc)
        ):
            return False
        generated_at = parse_aware_datetime(
            str(source.get("generated_at") or ""),
            "candidate evaluation source generated_at",
        )
        active_binding = _candidate_evaluation_active_version_binding(context)
        expected_observation_id = _candidate_evaluation_observation_id(
            calculated_source_hash, active_binding
        )
        if audit.get("observation_id") != expected_observation_id:
            return False
        archived_at = parse_aware_datetime(
            str(active_binding["version_archived_at"]),
            "candidate evaluation frozen archive time",
        )
        if generated_at >= context_kickoff or generated_at.astimezone(
            timezone.utc
        ) > archived_at.astimezone(timezone.utc):
            return False
        if parse_aware_datetime(
            str(audit.get("archived_at") or ""),
            "candidate evaluation archived_at",
        ).astimezone(timezone.utc) != archived_at.astimezone(timezone.utc):
            return False
        if artifact.get("generated_at") != generated_at.isoformat():
            return False

        manifest_raw = source.get("market_manifest")
        if not isinstance(manifest_raw, list) or not manifest_raw:
            return False
        manifest: dict[str, dict[str, Any]] = {}
        for raw_entry in manifest_raw:
            if not isinstance(raw_entry, dict):
                return False
            market = str(raw_entry.get("market") or "").strip().lower()
            status = str(raw_entry.get("status") or "").strip().lower()
            if market not in PRIMARY_MARKETS or market in manifest:
                return False
            if status not in {"evaluated", "unavailable"}:
                return False
            raw_reasons = raw_entry.get("reasons", [])
            reasons = (
                [str(reason).strip() for reason in raw_reasons if str(reason).strip()]
                if isinstance(raw_reasons, list)
                else []
            )
            if status == "unavailable" and not reasons:
                return False
            manifest[market] = {
                "market": market,
                "status": status,
                "reasons": reasons,
            }
        if set(manifest) != set(PRIMARY_MARKETS):
            return False

        raw_candidates = source.get("candidates")
        if not isinstance(raw_candidates, list):
            return False
        if artifact.get("artifact_sha256") != calculated_source_hash:
            return False
        replayed: list[dict[str, Any]] = []
        identities: set[str] = set()
        grouped: dict[str, list[dict[str, Any]]] = {}
        source_audit = validated_source_evidence_audit(context)
        source_required = context.get("forward_policy_binding") is not None
        if source_required and source_audit is None:
            return False
        for index, raw_candidate in enumerate(raw_candidates, start=1):
            if not isinstance(raw_candidate, dict):
                return False
            candidate = _evaluate_candidate(
                context,
                raw_candidate,
                expected_observation_id,
                index,
                generated_at,
                legacy_read_only=(
                    source_schema == LEGACY_CANDIDATE_EVALUATION_SCHEMA_VERSION
                ),
            )
            if source_audit is not None:
                candidate["source_evidence_binding"] = source_evidence.match_candidate(
                    source_audit["bundle"], raw_candidate
                )
            elif source_required:
                return False
            market = str(candidate["market"])
            if manifest[market]["status"] != "evaluated":
                return False
            if candidate["identity"] in identities:
                return False
            identities.add(str(candidate["identity"]))
            replayed.append(candidate)
            grouped.setdefault(market, []).append(candidate)
        for market, entry in manifest.items():
            count = len(grouped.get(market, []))
            if entry["status"] == "evaluated" and count == 0:
                return False
            if entry["status"] == "unavailable" and count:
                return False
            entry["candidate_count"] = count
        shadow_selections = _rank_candidate_shadow_selections(context, replayed)
        expected_manifest = [manifest[market] for market in PRIMARY_MARKETS]
        expected_fixture = {
            "match_id": str(context.get("match_id") or context.get("fixture_id")),
            "home_team": context.get("home_team"),
            "away_team": context.get("away_team"),
            "kickoff": context_kickoff.isoformat(),
        }
        expected_policy = {
            "market_policy_version": STRICT_OOS_POLICY_VERSION,
            "selection_policy_version": CONFIDENCE_POLICY_VERSION,
            "market_status": deepcopy(STRICT_OOS_MARKET_STATUS),
            "automatic_release_allowed": False,
        }
        expected_provenance = {
            "strict_forward_oos": True,
            "fixture_validated": True,
            "generated_before_kickoff": True,
            "training_cutoff_before_kickoff": True,
            "canonical_probabilities_reproduced": True,
            "market_values_recalculated": True,
            "raw_source_evidence_replayed": source_audit is not None,
            "source_evidence_hash": (
                source_audit.get("evidence_hash")
                if isinstance(source_audit, dict)
                else None
            ),
        }
        return bool(
            audit.get("fixture") == expected_fixture
            and audit.get("model") == _candidate_evaluation_model_binding(context)
            and audit.get("active_version_binding") == active_binding
            and audit.get("policy") == expected_policy
            and audit.get("provenance") == expected_provenance
            and audit.get("market_manifest") == expected_manifest
            and audit.get("candidates") == replayed
            and audit.get("candidate_count") == len(replayed)
            and audit.get("shadow_selections") == shadow_selections
        )
    except (KeyError, TypeError, ValueError):
        return False


def validated_candidate_evaluation_audit(
    audit: dict[str, Any], record: dict[str, Any] | None = None
) -> bool:
    if (
        audit.get("schema_version")
        not in {
            CANDIDATE_EVALUATION_SCHEMA_VERSION,
            LEGACY_CANDIDATE_EVALUATION_SCHEMA_VERSION,
        }
        or audit.get("kind") != CANDIDATE_EVALUATION_KIND
        or audit.get("status") != "observation_only"
        or audit.get("counts_toward_primary_record") is not False
        or audit.get("monetary_scope") != "none"
    ):
        return False
    fixture = audit.get("fixture")
    if not isinstance(fixture, dict):
        return False
    try:
        fixture_kickoff = parse_aware_datetime(
            str(fixture.get("kickoff") or ""),
            "candidate evaluation fixture kickoff",
        )
    except (TypeError, ValueError):
        return False
    if (
        not str(fixture.get("match_id") or "")
        or not str(fixture.get("home_team") or "")
        or not str(fixture.get("away_team") or "")
    ):
        return False
    if record is not None:
        try:
            record_kickoff = parse_aware_datetime(
                str(record.get("kickoff") or ""), "record kickoff"
            )
        except (TypeError, ValueError):
            return False
        if (
            str(fixture.get("match_id")) != str(record.get("match_id") or "")
            or fixture.get("home_team") != record.get("home_team")
            or fixture.get("away_team") != record.get("away_team")
            or fixture_kickoff.astimezone(timezone.utc)
            != record_kickoff.astimezone(timezone.utc)
        ):
            return False

    artifact = audit.get("artifact")
    policy = audit.get("policy")
    provenance = audit.get("provenance")
    candidates = audit.get("candidates")
    manifest = audit.get("market_manifest")
    if (
        not isinstance(artifact, dict)
        or not isinstance(policy, dict)
        or not isinstance(provenance, dict)
        or not isinstance(audit.get("model"), dict)
        or not isinstance(audit.get("active_version_binding"), dict)
        or not isinstance(candidates, list)
        or not isinstance(manifest, list)
        or audit.get("candidate_count") != len(candidates)
        or policy.get("market_policy_version") != STRICT_OOS_POLICY_VERSION
        or policy.get("selection_policy_version") != CONFIDENCE_POLICY_VERSION
        or policy.get("automatic_release_allowed") is not False
    ):
        return False
    if not all(
        provenance.get(key) is True
        for key in (
            "strict_forward_oos",
            "fixture_validated",
            "generated_before_kickoff",
            "training_cutoff_before_kickoff",
            "canonical_probabilities_reproduced",
            "market_values_recalculated",
        )
    ):
        return False
    artifact_sha256 = str(artifact.get("artifact_sha256") or "")
    raw_artifact_sha256 = str(artifact.get("raw_artifact_sha256") or "")
    observation_id = str(audit.get("observation_id") or "")
    source_payload = artifact.get("source_payload")
    source_payload_hash = str(artifact.get("source_payload_hash") or "")
    if (
        artifact.get("artifact_type") != CANDIDATE_EVALUATION_ARTIFACT_TYPE
        or artifact.get("schema_version") != audit.get("schema_version")
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_sha256)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", raw_artifact_sha256)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", observation_id)
        or not isinstance(source_payload, dict)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", source_payload_hash)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(audit.get("audit_hash") or ""))
    ):
        return False
    try:
        if audit.get("audit_hash") != calculate_candidate_evaluation_audit_hash(audit):
            return False
    except (TypeError, ValueError):
        return False
    manifest_by_market: dict[str, dict[str, Any]] = {}
    for entry in manifest:
        if not isinstance(entry, dict):
            return False
        market = entry.get("market")
        status = entry.get("status")
        if (
            market not in PRIMARY_MARKETS
            or market in manifest_by_market
            or status not in {"evaluated", "unavailable"}
            or isinstance(entry.get("candidate_count"), bool)
            or not isinstance(entry.get("candidate_count"), int)
            or entry.get("candidate_count") < 0
            or (status == "unavailable" and not entry.get("reasons"))
        ):
            return False
        manifest_by_market[str(market)] = entry
    ids: set[str] = set()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return False
        market = candidate.get("market")
        identity = str(candidate.get("identity") or "")
        source_index = candidate.get("source_index")
        if (
            market not in manifest_by_market
            or manifest_by_market[str(market)].get("status") != "evaluated"
            or isinstance(source_index, bool)
            or not isinstance(source_index, int)
            or source_index < 1
            or not identity
        ):
            return False
        expected_id = (
            "sha256:"
            + hashlib.sha256(
                f"{observation_id}:{source_index}:{identity}".encode("utf-8")
            ).hexdigest()
        )
        candidate_id = str(candidate.get("candidate_id") or "")
        if candidate_id != expected_id or candidate_id in ids:
            return False
        ids.add(candidate_id)
        try:
            distribution = _normalize_candidate_distribution(
                candidate.get("settlement_probabilities"),
                "archived candidate evaluation distribution",
            )
            probability = float(candidate.get("probability"))
        except (TypeError, ValueError):
            return False
        if (
            not math.isfinite(probability)
            or abs(probability - distribution["full_win"] - distribution["half_win"])
            > PROBABILITY_AUDIT_TOLERANCE
        ):
            return False
        gates = candidate.get("gates")
        if not isinstance(gates, list) or not gates:
            return False
        for gate in gates:
            if (
                not isinstance(gate, dict)
                or not str(gate.get("gate") or "")
                or gate.get("category") not in CANDIDATE_GATE_CATEGORIES
                or not isinstance(gate.get("passed"), bool)
                or not isinstance(gate.get("reasons"), list)
                or (gate.get("passed") is True and gate.get("reasons"))
            ):
                return False
        counterfactual = all(
            gate["passed"] for gate in gates if gate["category"] != "release"
        )
        formal = all(gate["passed"] for gate in gates)
        if (
            candidate.get("counterfactual_eligible") is not counterfactual
            or candidate.get("formal_eligible") is not formal
            or candidate.get("counts_toward_primary_record") is not False
            or candidate.get("monetary_scope") != "none"
            or candidate.get("status") != "observation"
        ):
            return False
        expected_release = [
            gate["gate"]
            for gate in gates
            if gate["category"] == "release" and not gate["passed"]
        ]
        if candidate.get("release_blockers") != expected_release:
            return False
        grouped.setdefault(str(market), []).append(candidate)
    for market, entry in manifest_by_market.items():
        count = len(grouped.get(market, []))
        if entry.get("candidate_count") != count:
            return False
        if (entry.get("status") == "evaluated") != (count > 0):
            return False
    expected_selections: dict[str, str] = {}
    for market, market_candidates in grouped.items():
        eligible = [
            candidate
            for candidate in market_candidates
            if candidate.get("counterfactual_eligible") is True
        ]
        ranks = sorted(candidate.get("shadow_rank") for candidate in eligible)
        if ranks != list(range(1, len(eligible) + 1)):
            return False
        selected = [
            candidate
            for candidate in market_candidates
            if candidate.get("shadow_selected") is True
        ]
        if len(selected) != (1 if eligible else 0):
            return False
        if selected:
            if selected[0].get("shadow_rank") != 1:
                return False
            expected_selections[market] = str(selected[0].get("candidate_id"))
        if any(
            candidate.get("shadow_rank") is not None
            or candidate.get("shadow_selected") is True
            for candidate in market_candidates
            if candidate.get("counterfactual_eligible") is not True
        ):
            return False
    if audit.get("shadow_selections") != expected_selections:
        return False
    if record is None:
        return False
    return _replay_candidate_evaluation_audit(audit, record)


def candidate_evaluation_evidence_scope(audit: dict[str, Any]) -> str:
    if audit.get("schema_version") == CANDIDATE_EVALUATION_SCHEMA_VERSION:
        return "canonical_market_identity_forward_eligible"
    if audit.get("schema_version") == LEGACY_CANDIDATE_EVALUATION_SCHEMA_VERSION:
        return "legacy_read_only_quarantined"
    return "unsupported"


def validated_observation_audit(
    audit: dict[str, Any], record: dict[str, Any] | None = None
) -> bool:
    if audit.get("schema_version") in {
        CANDIDATE_EVALUATION_SCHEMA_VERSION,
        LEGACY_CANDIDATE_EVALUATION_SCHEMA_VERSION,
    }:
        return validated_candidate_evaluation_audit(audit, record)
    if (
        audit.get("schema_version") != OBSERVATION_SCHEMA_VERSION
        or audit.get("status") != "observation_only"
        or audit.get("counts_toward_primary_record") is not False
        or audit.get("monetary_scope") != "none"
    ):
        return False
    provenance = audit.get("provenance")
    model = audit.get("model")
    if not isinstance(provenance, dict) or not isinstance(model, dict):
        return False
    if not all(
        provenance.get(key) is True
        for key in (
            "strict_forward_oos",
            "fixture_validated",
            "generated_before_kickoff",
            "training_cutoff_before_kickoff",
        )
    ):
        return False

    if audit.get("kind") == CORNER_OBSERVATION_KIND:
        ranker = audit.get("ranker")
        fixture = audit.get("fixture")
        lineage = audit.get("lineage")
        candidates = audit.get("candidates")
        best = audit.get("best_observation")
        if (
            audit.get("market") != "corner_markets"
            or not isinstance(ranker, dict)
            or not isinstance(fixture, dict)
            or not isinstance(lineage, dict)
            or not isinstance(candidates, list)
            or not candidates
            or audit.get("candidate_count") != len(candidates)
            or not isinstance(best, dict)
            or not isinstance(audit.get("market_policy"), dict)
            or audit["market_policy"].get("status") != "observation_only"
            or provenance.get("registry_and_model_reopened") is not True
            or provenance.get("ranking_reproduced") is not True
        ):
            return False
        hashes = (
            (model, "model_hash"),
            (model, "prediction_hash"),
            (model, "artifact_sha256"),
            (ranker, "ranking_hash"),
            (ranker, "artifact_sha256"),
            (audit, "observation_id"),
            (audit, "audit_hash"),
        )
        if any(
            not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get(key) or ""))
            for value, key in hashes
        ):
            return False
        try:
            calculated_audit_hash = calculate_corner_observation_audit_hash(audit)
        except (TypeError, ValueError):
            return False
        if audit.get("audit_hash") != calculated_audit_hash:
            return False
        expected_observation_id = (
            "sha256:"
            + hashlib.sha256(
                (
                    str(model["artifact_sha256"]) + ":" + str(ranker["artifact_sha256"])
                ).encode("ascii")
            ).hexdigest()
        )
        if audit.get("observation_id") != expected_observation_id:
            return False
        lineage_fields = {
            "prediction_hash",
            "registry_hash",
            "league_key",
            "dataset_hash",
            "model_hash",
            "evaluation_hash",
            "backtest_hash",
            "lineage_hash",
            "training_cutoff",
        }
        if lineage.get("registry_role") is not None:
            lineage_fields.add("registry_role")
        if set(lineage) != lineage_fields:
            return False
        if lineage.get("registry_role") not in {None, "corner"}:
            return False
        for key in (
            "prediction_hash",
            "registry_hash",
            "dataset_hash",
            "model_hash",
            "evaluation_hash",
            "backtest_hash",
            "lineage_hash",
        ):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(lineage.get(key) or "")):
                return False
        if (
            model.get("prediction_hash") != lineage.get("prediction_hash")
            or model.get("model_hash") != lineage.get("model_hash")
            or fixture.get("league_key") != lineage.get("league_key")
            or model.get("training_cutoff_date") != lineage.get("training_cutoff")
        ):
            return False
        if record is not None and record.get("forward_policy_binding") is not None:
            try:
                frozen_corner = frozen_registry_model_for_record(
                    record,
                    role="corner",
                    league_key=str(lineage.get("league_key") or "").casefold(),
                )
            except ValueError:
                return False
            if (
                frozen_corner is None
                or lineage.get("registry_role") != "corner"
                or lineage.get("registry_hash")
                != frozen_corner[0].get("declared_registry_hash")
                or lineage.get("dataset_hash") != frozen_corner[1].get("dataset_hash")
                or lineage.get("model_hash") != frozen_corner[1].get("model_hash")
            ):
                return False
        try:
            kickoff = parse_aware_datetime(
                str(fixture.get("kickoff") or ""),
                "archived corner observation kickoff",
            )
            prediction_time = parse_aware_datetime(
                str(model.get("generated_at") or ""),
                "archived corner observation prediction time",
            )
            ranking_time = parse_aware_datetime(
                str(ranker.get("generated_at") or ""),
                "archived corner observation ranking time",
            )
            cutoff = date.fromisoformat(str(lineage.get("training_cutoff") or ""))
        except ValueError:
            return False
        if (
            not str(fixture.get("home_team") or "").strip()
            or not str(fixture.get("away_team") or "").strip()
            or prediction_time >= kickoff
            or ranking_time >= kickoff
            or prediction_time > ranking_time
            or cutoff >= kickoff.date()
        ):
            return False

        candidate_ids: set[str] = set()
        ranks: list[int] = []
        ranking_hash = str(ranker["ranking_hash"])
        for index, candidate in enumerate(candidates, start=1):
            if not isinstance(candidate, dict):
                return False
            market = candidate.get("market")
            side = candidate.get("side")
            line = candidate.get("line")
            if (
                market not in corner_ranker.MARKETS
                or side not in corner_ranker.MARKET_SIDES[market]
                or isinstance(line, bool)
                or not isinstance(line, (int, float))
                or not math.isfinite(float(line))
                or isinstance(candidate.get("rank"), bool)
                or not isinstance(candidate.get("rank"), int)
                or candidate.get("status") != "observation"
                or candidate.get("formal_eligible") is not False
                or candidate.get("upstream_formal_eligible") is not False
                or candidate.get("lineage") != lineage
            ):
                return False
            try:
                rank = int(candidate.get("rank"))
                corner_observation_probabilities(
                    candidate.get("settlement_probabilities"),
                    label="archived corner observation probabilities",
                )
            except (TypeError, ValueError):
                return False
            ranks.append(rank)
            candidate_id = str(candidate.get("candidate_id") or "")
            expected_candidate_id = (
                "sha256:"
                + hashlib.sha256(
                    (
                        f"{ranking_hash}:{index}:{market}:{side}:{float(line):.12g}"
                    ).encode("utf-8")
                ).hexdigest()
            )
            if candidate_id != expected_candidate_id or candidate_id in candidate_ids:
                return False
            candidate_ids.add(candidate_id)
            gates = candidate.get("gates")
            if (
                not isinstance(gates, list)
                or [gate.get("gate") for gate in gates if isinstance(gate, dict)]
                != list(CORNER_OBSERVATION_GATE_ORDER)
                or any(
                    not isinstance(gate, dict)
                    or not isinstance(gate.get("passed"), bool)
                    or not isinstance(gate.get("reasons"), list)
                    for gate in gates
                )
            ):
                return False
        if ranks != list(range(1, len(candidates) + 1)):
            return False
        matching_best = [
            candidate
            for candidate in candidates
            if candidate.get("candidate_id") == best.get("candidate_id")
        ]
        return len(matching_best) == 1 and best == matching_best[0]

    if audit.get("market") != "htft":
        return False
    for key in ("model_hash", "prediction_hash", "artifact_sha256", "matrix_hash"):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(model.get(key) or "")):
            return False
    try:
        matrix = validate_htft_matrix(
            audit.get("matrix"), "archived observation matrix"
        )
    except ValueError:
        return False
    matrix_bytes = json.dumps(
        matrix,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    if model.get("matrix_hash") != f"sha256:{hashlib.sha256(matrix_bytes).hexdigest()}":
        return False
    if record is not None and record.get("forward_policy_binding") is not None:
        try:
            frozen_htft = frozen_registry_model_for_record(
                record,
                role="football_htft",
                league_key=league_key_for_record(record),
            )
        except ValueError:
            return False
        if (
            frozen_htft is None
            or model.get("registry_role") != "football_htft"
            or model.get("registry_hash")
            != frozen_htft[0].get("declared_registry_hash")
            or model.get("dataset_manifest_hash")
            != frozen_htft[0].get("dataset_manifest_hash")
            or model.get("model_hash") != frozen_htft[1].get("model_hash")
        ):
            return False
    top_two = audit.get("top_two")
    if (
        not isinstance(top_two, list)
        or len(top_two) != 2
        or not all(isinstance(item, dict) for item in top_two)
    ):
        return False
    expected = sorted(
        HTFT_OUTCOMES,
        key=lambda outcome: (-matrix[outcome], HTFT_OUTCOMES.index(outcome)),
    )[:2]
    if [str(item.get("selection") or "").upper() for item in top_two] != expected:
        return False
    return True


def validated_candidate_evaluation_diagnostic(
    audit: dict[str, Any], record: dict[str, Any]
) -> dict[str, Any] | None:
    if record.get("status") != "reviewed":
        return None
    verification = record.get("result_verification")
    if (
        not isinstance(verification, dict)
        or verification.get("verified_finished") is not True
        or not str(verification.get("source") or "").strip()
    ):
        return None

    def score_pair(value: Any, label: str) -> tuple[int, int] | None:
        if value is None or str(value).strip() == "":
            return None
        match = re.fullmatch(r"(\d+)-(\d+)", str(value).strip())
        if match is None:
            raise ValueError(f"{label} must be HOME-AWAY")
        return int(match.group(1)), int(match.group(2))

    try:
        verification_time = parse_aware_datetime(
            str(verification.get("collected_at") or ""),
            "candidate result verification collected_at",
        )
        kickoff_time = parse_aware_datetime(
            str(record.get("kickoff") or ""), "candidate result fixture kickoff"
        )
        if verification_time.astimezone(timezone.utc) < kickoff_time.astimezone(
            timezone.utc
        ):
            return None
        final = score_pair(record.get("final_score"), "candidate final_score")
        if final is None:
            return None
        half = score_pair(record.get("half_time_score"), "candidate half_time_score")
        home_corners = record.get("home_corners")
        away_corners = record.get("away_corners")
        if (home_corners is None) != (away_corners is None):
            return None
        if home_corners is not None:
            if (
                isinstance(home_corners, bool)
                or isinstance(away_corners, bool)
                or int(home_corners) != home_corners
                or int(away_corners) != away_corners
                or int(home_corners) < 0
                or int(away_corners) < 0
            ):
                return None
            home_corners = int(home_corners)
            away_corners = int(away_corners)
        expected = settle_candidate_observations(
            [audit],
            half_home=half[0] if half is not None else None,
            half_away=half[1] if half is not None else None,
            home=final[0],
            away=final[1],
            home_corners=home_corners,
            away_corners=away_corners,
        )
    except (TypeError, ValueError):
        return None
    if len(expected) != 1:
        return None
    matches = [
        item
        for item in frozen_observation_diagnostics(record)
        if item.get("kind") == CANDIDATE_EVALUATION_KIND
        and item.get("observation_id") == audit.get("observation_id")
    ]
    if len(matches) != 1 or matches[0] != expected[0]:
        return None
    return matches[0]


def candidate_evaluation_selection_unit(
    record: dict[str, Any], market: str
) -> tuple[str, str]:
    context = _candidate_evaluation_record_context(record)
    return (
        str(context.get("match_id") or context.get("fixture_id") or ""),
        str(market),
    )


def observation_gate_funnel(records: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = [
        record
        for record in records
        if record.get("mode") == "prematch" and record.get("status") == "reviewed"
    ]
    markets: dict[str, dict[str, Any]] = {}
    reviewed_match_ids: set[str] = set()

    def market_block(market: str) -> dict[str, Any]:
        return markets.setdefault(
            market,
            {
                "market": market,
                "observation_records": 0,
                "candidate_count": 0,
                "excluded_observations": 0,
                "excluded_reasons": {},
                "gate_funnel": {},
                "_top1_hits": [],
                "_top2_hits": [],
                "_brier": [],
                "_log_loss": [],
                "_ungraded": 0,
                "_corner_graded_observations": 0,
                "_corner_ungraded_observations": 0,
                "_corner_graded_candidates": 0,
                "_corner_ungraded_candidates": 0,
                "_corner_results": {
                    state: 0 for state in corner_ranker.SETTLEMENT_STATES
                },
            },
        )

    def add_candidate_gates(
        block: dict[str, Any], candidates: list[dict[str, Any]]
    ) -> None:
        for candidate in candidates:
            for gate in candidate.get("gates", []):
                if not isinstance(gate, dict) or not str(gate.get("gate") or ""):
                    continue
                name = str(gate["gate"])
                gate_block = block["gate_funnel"].setdefault(
                    name,
                    {
                        "evaluated": 0,
                        "passed": 0,
                        "failed": 0,
                        "failure_reasons": {},
                    },
                )
                gate_block["evaluated"] += 1
                if gate.get("passed") is True:
                    gate_block["passed"] += 1
                else:
                    gate_block["failed"] += 1
                    for reason in gate.get("reasons", []):
                        normalized = str(reason).strip()
                        if normalized:
                            failure_reasons = gate_block["failure_reasons"]
                            failure_reasons[normalized] = (
                                failure_reasons.get(normalized, 0) + 1
                            )

    for record in reviewed:
        diagnostics_by_id = {
            str(item.get("observation_id")): item
            for item in frozen_observation_diagnostics(record)
            if item.get("observation_id")
        }
        for audit in frozen_candidate_audits(record):
            if not validated_observation_audit(audit, record):
                block = market_block(str(audit.get("market") or "unknown"))
                block["excluded_observations"] += 1
                reasons = block["excluded_reasons"]
                reasons["invalid_observation_provenance"] = (
                    reasons.get("invalid_observation_provenance", 0) + 1
                )
                continue
            reviewed_match_ids.add(str(record.get("match_id")))

            diagnostic = diagnostics_by_id.get(str(audit.get("observation_id")))
            if audit.get("kind") == CANDIDATE_EVALUATION_KIND:
                # v2 has its own one-selection-per-market cohort and release
                # funnel below.  Keeping it out of the v1 observation funnel
                # avoids double-counting the bound HT/FT and corner audits.
                continue
            if audit.get("kind") == CORNER_OBSERVATION_KIND:
                grouped: dict[str, list[dict[str, Any]]] = {}
                for candidate in audit.get("candidates", []):
                    if isinstance(candidate, dict):
                        grouped.setdefault(str(candidate.get("market")), []).append(
                            candidate
                        )
                for market, candidates in grouped.items():
                    block = market_block(market)
                    block["observation_records"] += 1
                    block["candidate_count"] += len(candidates)
                    add_candidate_gates(block, candidates)
                    if (
                        not isinstance(diagnostic, dict)
                        or diagnostic.get("status") != "graded_observation"
                    ):
                        block["_corner_ungraded_observations"] += 1
                        block["_corner_ungraded_candidates"] += len(candidates)
                        continue
                    results_by_id = {
                        str(item.get("candidate_id")): item.get("settlement_result")
                        for item in diagnostic.get("candidate_results", [])
                        if isinstance(item, dict) and item.get("candidate_id")
                    }
                    block["_corner_graded_observations"] += 1
                    for candidate in candidates:
                        result = results_by_id.get(str(candidate.get("candidate_id")))
                        if result in corner_ranker.SETTLEMENT_STATES:
                            block["_corner_results"][result] += 1
                            block["_corner_graded_candidates"] += 1
                        else:
                            block["_corner_ungraded_candidates"] += 1
                continue

            market = str(audit.get("market") or "unknown")
            block = market_block(market)
            block["observation_records"] += 1
            candidates = [
                item for item in audit.get("top_two", []) if isinstance(item, dict)
            ]
            block["candidate_count"] += len(candidates)
            add_candidate_gates(block, candidates)
            if (
                not isinstance(diagnostic, dict)
                or diagnostic.get("status") != "graded_observation"
            ):
                block["_ungraded"] += 1
                continue
            block["_top1_hits"].append(bool(diagnostic.get("top1_hit")))
            block["_top2_hits"].append(bool(diagnostic.get("top2_hit")))
            block["_brier"].append(float(diagnostic["nine_class_brier"]))
            block["_log_loss"].append(float(diagnostic["nine_class_log_loss"]))

    output_markets: dict[str, Any] = {}
    for market, block in sorted(markets.items()):
        top1 = block.pop("_top1_hits")
        top2 = block.pop("_top2_hits")
        brier = block.pop("_brier")
        log_loss = block.pop("_log_loss")
        ungraded = block.pop("_ungraded")
        corner_graded_observations = block.pop("_corner_graded_observations")
        corner_ungraded_observations = block.pop("_corner_ungraded_observations")
        corner_graded_candidates = block.pop("_corner_graded_candidates")
        corner_ungraded_candidates = block.pop("_corner_ungraded_candidates")
        corner_results = block.pop("_corner_results")
        graded = len(top1)
        block["gate_funnel"] = {
            name: {
                **values,
                "failure_reasons": dict(sorted(values["failure_reasons"].items())),
            }
            for name, values in sorted(block["gate_funnel"].items())
        }
        if market in CORNER_MARKETS:
            block["diagnostics"] = {
                "graded_observations": corner_graded_observations,
                "ungraded_observations": corner_ungraded_observations,
                "graded_candidates": corner_graded_candidates,
                "ungraded_candidates": corner_ungraded_candidates,
                "settlement_results": corner_results,
                "positive_settlements": (
                    corner_results["full_win"] + corner_results["half_win"]
                ),
                "negative_settlements": (
                    corner_results["half_loss"] + corner_results["loss"]
                ),
                "counts_toward_primary_record": False,
                "monetary_scope": "none",
            }
        else:
            block["diagnostics"] = {
                "graded_observations": graded,
                "ungraded_observations": ungraded,
                "top1_hits": sum(top1),
                "top1_rate": round(sum(top1) / graded, 4) if graded else None,
                "top2_hits": sum(top2),
                "top2_rate": round(sum(top2) / graded, 4) if graded else None,
                "nine_class_brier": math.fsum(brier) / graded if graded else None,
                "nine_class_log_loss": (
                    math.fsum(log_loss) / graded if graded else None
                ),
                "counts_toward_primary_record": False,
                "monetary_scope": "none",
            }
        output_markets[market] = block
    return {
        "evaluation_scope": "strict_live_forward_reviewed_observations",
        "reviewed_matches_with_observations": len(reviewed_match_ids),
        "markets": output_markets,
    }


def shadow_selection_by_market(records: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = strict_forward_reviewed_records(records)
    markets: dict[str, dict[str, Any]] = {
        market: {
            "market": market,
            "evaluated_records": 0,
            "unavailable_records": 0,
            "counterfactual_abstentions": 0,
            "shadow_selected": 0,
            "graded_shadow_selections": 0,
            "ungraded_shadow_selections": 0,
            "settlement_results": {state: 0 for state in CANDIDATE_SETTLEMENT_STATES},
            "counts_toward_primary_record": False,
            "monetary_scope": "none",
        }
        for market in PRIMARY_MARKETS
    }
    reviewed_match_ids: set[str] = set()
    seen_selection_units: set[tuple[str, str]] = set()
    for record in reviewed:
        for audit in frozen_candidate_audits(record):
            observation_id = str(audit.get("observation_id") or "")
            if (
                audit.get("kind") != CANDIDATE_EVALUATION_KIND
                or not validated_candidate_evaluation_audit(audit, record)
                or not observation_id
            ):
                continue
            diagnostic = validated_candidate_evaluation_diagnostic(audit, record)
            if diagnostic is None:
                continue
            result_by_id = {
                str(item.get("candidate_id")): item.get("settlement_result")
                for item in (
                    diagnostic.get("candidate_results", [])
                    if isinstance(diagnostic, dict)
                    else []
                )
                if isinstance(item, dict) and item.get("candidate_id")
            }
            selected_by_market = {
                str(candidate.get("market")): candidate
                for candidate in audit.get("candidates", [])
                if isinstance(candidate, dict)
                and candidate.get("shadow_selected") is True
            }
            for entry in audit.get("market_manifest", []):
                if not isinstance(entry, dict) or entry.get("market") not in markets:
                    continue
                market = str(entry["market"])
                selection_unit = candidate_evaluation_selection_unit(record, market)
                if not selection_unit[0] or selection_unit in seen_selection_units:
                    continue
                seen_selection_units.add(selection_unit)
                reviewed_match_ids.add(selection_unit[0])
                block = markets[market]
                if entry.get("status") == "unavailable":
                    block["unavailable_records"] += 1
                    continue
                block["evaluated_records"] += 1
                selected = selected_by_market.get(market)
                if not isinstance(selected, dict):
                    block["counterfactual_abstentions"] += 1
                    continue
                block["shadow_selected"] += 1
                result = result_by_id.get(str(selected.get("candidate_id")))
                if result in CANDIDATE_SETTLEMENT_STATES:
                    block["graded_shadow_selections"] += 1
                    block["settlement_results"][str(result)] += 1
                else:
                    block["ungraded_shadow_selections"] += 1
    return {
        "evaluation_scope": "strict_forward_oos_reviewed_candidate_evaluation_v2",
        "selection_unit": "at_most_one_shadow_selected_per_match_market",
        "reviewed_matches_with_candidate_evaluations": len(reviewed_match_ids),
        "markets": markets,
    }


def release_blocker_funnel(records: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = strict_forward_reviewed_records(records)
    markets: dict[str, dict[str, Any]] = {
        market: {
            "market": market,
            "counterfactual_candidates": 0,
            "release_gates": {},
        }
        for market in PRIMARY_MARKETS
    }
    seen_selection_units: set[tuple[str, str]] = set()
    for record in reviewed:
        for audit in frozen_candidate_audits(record):
            observation_id = str(audit.get("observation_id") or "")
            if (
                audit.get("kind") != CANDIDATE_EVALUATION_KIND
                or not validated_candidate_evaluation_audit(audit, record)
                or not observation_id
            ):
                continue
            grouped: dict[str, list[dict[str, Any]]] = {}
            for candidate in audit.get("candidates", []):
                if isinstance(candidate, dict) and candidate.get("market") in markets:
                    grouped.setdefault(str(candidate["market"]), []).append(candidate)
            for market, market_candidates in grouped.items():
                selection_unit = candidate_evaluation_selection_unit(record, market)
                if not selection_unit[0] or selection_unit in seen_selection_units:
                    continue
                seen_selection_units.add(selection_unit)
                block = markets[market]
                for candidate in market_candidates:
                    if candidate.get("counterfactual_eligible") is not True:
                        continue
                    block["counterfactual_candidates"] += 1
                    for gate in candidate.get("gates", []):
                        if (
                            not isinstance(gate, dict)
                            or gate.get("category") != "release"
                        ):
                            continue
                        name = str(gate.get("gate") or "")
                        if not name:
                            continue
                        gate_block = block["release_gates"].setdefault(
                            name,
                            {
                                "evaluated": 0,
                                "passed": 0,
                                "failed": 0,
                                "failure_reasons": {},
                            },
                        )
                        gate_block["evaluated"] += 1
                        if gate.get("passed") is True:
                            gate_block["passed"] += 1
                        else:
                            gate_block["failed"] += 1
                            for reason in gate.get("reasons", []):
                                normalized = str(reason).strip()
                                if normalized:
                                    reasons = gate_block["failure_reasons"]
                                    reasons[normalized] = reasons.get(normalized, 0) + 1
    for block in markets.values():
        block["release_gates"] = {
            name: {
                **values,
                "failure_reasons": dict(sorted(values["failure_reasons"].items())),
            }
            for name, values in sorted(block["release_gates"].items())
        }
    return {
        "evaluation_scope": "counterfactual_candidates_release_gates_only",
        "markets": markets,
    }


def shadow_review_trigger_by_market(
    shadow: dict[str, Any], minimum: int
) -> dict[str, bool]:
    raw_markets = shadow.get("markets", {}) if isinstance(shadow, dict) else {}
    return {
        market: int(raw_markets.get(market, {}).get("graded_shadow_selections", 0))
        >= minimum
        for market in PRIMARY_MARKETS
    }


def league_performance(
    records: list[dict[str, Any]], league_key: str
) -> dict[str, Any]:
    strict_records = strict_forward_reviewed_records(records)
    primary_by_market = primary_market_performance(strict_records)
    primary = performance_block(primary_pairs(strict_records))
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
    shadow = shadow_selection_by_market(records)
    release_funnel = release_blocker_funnel(records)
    return {
        "league_key": league_key,
        "source_labels": sorted(
            {
                str(competition_identity_record(record).get("league") or "unknown")
                for record in records
            }
        ),
        "matches": len(records),
        "reviewed_matches": len(records),
        "strict_forward_reviewed_matches": len(strict_records),
        "excluded_from_strict_forward": len(records) - len(strict_records),
        "primary_record_matches": primary["matches"],
        "no_primary_reviewed_matches": sum(
            learning_scope_for_record(record) == "no_primary_observation"
            for record in records
        ),
        "strict_no_primary_reviewed_matches": selection_policy_block(records)[
            "abstained_matches"
        ],
        "learning_samples": learning_samples,
        "selection_policy": selection_policy_block(records),
        "one_x_two_metrics": one_x_two_metrics(records),
        "observation_gate_funnel": observation_gate_funnel(records),
        "shadow_selection_by_market": shadow,
        "release_blocker_funnel": release_funnel,
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
        "exact_scores": exact_score_diagnostics(strict_records),
        "display_exact_scores": exact_score_diagnostics(strict_records, display=True),
        "recent_learnings": learnings,
    }


def _recent_candidate_gate_funnels(
    history: list[dict[str, Any]], windows: tuple[int, ...] = (50, 100)
) -> dict[str, Any]:
    """Load the read-only gate diagnostic lazily to avoid an import cycle."""

    try:
        from scripts import gate_stats
    except ImportError:  # Direct execution from scripts/.
        import gate_stats  # type: ignore[no-redef]

    return gate_stats.recent_candidate_gate_funnels(history, windows=windows)


def calculate_stats(
    history: list[dict[str, Any]], *, gate_windows: tuple[int, ...] = (50, 100)
) -> dict[str, Any]:
    reviewed = [
        r
        for r in history
        if r.get("mode") == "prematch" and r.get("status") == "reviewed"
    ]
    strict_reviewed = strict_forward_reviewed_records(reviewed)
    excluded_reviewed = [
        record for record in reviewed if not is_strict_forward_oos_record(record)
    ]
    exclusion_reasons: dict[str, int] = {}
    for record in excluded_reviewed:
        reason = strict_forward_exclusion_reason(record) or "unknown"
        exclusion_reasons[reason] = exclusion_reasons.get(reason, 0) + 1
    primary_pairs_all = primary_pairs(strict_reviewed)
    primary = performance_block(primary_pairs_all)
    learning_samples = learning_sample_summary(reviewed)
    exact_scores = exact_score_diagnostics(strict_reviewed)
    display_exact_scores = exact_score_diagnostics(strict_reviewed, display=True)
    leagues: dict[str, dict[str, Any]] = {}
    for league_key in sorted(
        {competition_key_for_record(record) for record in reviewed}
    ):
        subset = [
            record
            for record in reviewed
            if competition_key_for_record(record) == league_key
        ]
        leagues[league_key] = league_performance(subset, league_key)
    primary_by_market = primary_market_performance(strict_reviewed)
    quarantined_primary = performance_block(primary_pairs(excluded_reviewed))
    quarantined_by_market = primary_market_performance(excluded_reviewed)
    shadow = shadow_selection_by_market(reviewed)
    release_funnel = release_blocker_funnel(reviewed)
    recent_gate_funnels = _recent_candidate_gate_funnels(history, windows=gate_windows)
    return {
        "evaluation_scope": "strict_forward_oos",
        "reviewed_matches": len(reviewed),
        "strict_forward_reviewed_matches": len(strict_reviewed),
        "excluded_from_strict_forward": {
            "matches": len(excluded_reviewed),
            "match_ids": [str(record.get("match_id")) for record in excluded_reviewed],
            "by_reason": exclusion_reasons,
        },
        "legacy_or_quarantined": {
            "reviewed_matches": len(excluded_reviewed),
            "primary": quarantined_primary,
            "primary_by_market": quarantined_by_market,
            "by_reason": exclusion_reasons,
            "match_ids": [str(record.get("match_id")) for record in excluded_reviewed],
        },
        "primary_record_matches": primary["matches"],
        "no_primary_reviewed_matches": sum(
            learning_scope_for_record(record) == "no_primary_observation"
            for record in reviewed
        ),
        "strict_no_primary_reviewed_matches": selection_policy_block(reviewed)[
            "abstained_matches"
        ],
        "pending_matches": sum(
            r.get("mode") == "prematch" and r.get("status") == "pending"
            for r in history
        ),
        "learning_samples": learning_samples,
        "selection_policy": selection_policy_block(reviewed),
        "one_x_two_metrics": one_x_two_metrics(reviewed),
        "observation_gate_funnel": observation_gate_funnel(reviewed),
        "shadow_selection_by_market": shadow,
        "release_blocker_funnel": release_funnel,
        "recent_candidate_gate_funnels": recent_gate_funnels,
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
        "exact_score_diagnostics": exact_scores,
        "display_exact_score_diagnostics": display_exact_scores,
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
        "learnings_recorded": sum(
            bool(str(r.get("key_learning", "")).strip()) for r in reviewed
        ),
        "market_status_policy_version": STRICT_OOS_POLICY_VERSION,
        "market_status": deepcopy(STRICT_OOS_MARKET_STATUS),
        "untouched_live_forward": untouched_live_forward_summary(reviewed),
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
    quarantined_primary = stats["legacy_or_quarantined"]["primary"]
    selection = stats["selection_policy"]
    excluded = stats["excluded_from_strict_forward"]["matches"]
    no_primary_learning = stats["learning_samples"]["no_primary_observation"]

    def roi_text(block: dict[str, Any]) -> str:
        roi = block.get("roi")
        return "—" if roi is None else f"{float(roi) * 100:+.2f}%"

    return (
        f"已复盘{stats['reviewed_matches']}场，按{len(stats['leagues'])}个联赛归类；"
        f"严格前瞻{stats['strict_forward_reviewed_matches']}场，隔离{excluded}场。"
        f"严格主推{primary['matches']}场"
        f"{primary['wins']}胜{primary['losses']}负{primary['pushes']}走，"
        f"收益{primary['profit_units']:+.2f}u，ROI {roi_text(primary)}，计入战绩；"
        f"严格弃赛{selection['abstained_matches']}场，覆盖率"
        f"{selection['coverage'] if selection['coverage'] is not None else '—'}。"
        f"无主推学习样本{no_primary_learning}场，不计战绩。"
        f"隔离主推仍保留{quarantined_primary['matches']}场"
        f"{quarantined_primary['wins']}胜{quarantined_primary['losses']}负"
        f"{quarantined_primary['pushes']}走，收益"
        f"{quarantined_primary['profit_units']:+.2f}u，ROI "
        f"{roi_text(quarantined_primary)}，仅作描述与取证参考。"
        f"主推分市场统计{primary_by_market['matches']}项"
        f"{primary_by_market['wins']}胜{primary_by_market['losses']}负{primary_by_market['pushes']}走。"
        "次推仅作赛前参考，不结算、不计命中率或金额。"
        f"单市场{minimum}个严格样本只是人工复核触发线，不自动调整模型参数或市场政策。"
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
        shadow_threshold = shadow_review_trigger_by_market(
            league_stats["shadow_selection_by_market"], minimum
        )
        matches = int(league_stats["reviewed_matches"])
        strict_matches = int(league_stats["strict_forward_reviewed_matches"])
        sample_tier = (
            "anecdotal"
            if strict_matches < 10
            else "provisional"
            if strict_matches < 20
            else "review_ready"
        )
        primary = league_stats["primary"]
        roi = primary.get("roi")
        roi_text = "—" if roi is None else f"{float(roi) * 100:+.2f}%"
        profiles[league_key] = {
            "league_key": league_key,
            "source_labels": league_stats["source_labels"],
            "reviewed_matches": matches,
            "strict_forward_reviewed_matches": strict_matches,
            "primary_record_matches": league_stats["primary_record_matches"],
            "no_primary_reviewed_matches": league_stats["no_primary_reviewed_matches"],
            "strict_no_primary_reviewed_matches": league_stats[
                "strict_no_primary_reviewed_matches"
            ],
            "learning_samples": league_stats["learning_samples"],
            "selection_policy": league_stats["selection_policy"],
            "sample_tier": sample_tier,
            "minimum_graded_per_market_for_manual_review": minimum,
            "sample_review_trigger_met_by_market": sample_threshold,
            "shadow_selection_by_market": league_stats["shadow_selection_by_market"],
            "shadow_review_trigger_met_by_market": shadow_threshold,
            "release_blocker_funnel": league_stats["release_blocker_funnel"],
            "decision": (
                "manual_model_validation_required"
                if any(sample_threshold.values()) or any(shadow_threshold.values())
                else "hold_insufficient_strict_league_sample"
            ),
            "active_weight_adjustments": {},
            "parameter_change_authorized": False,
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
            "one_x_two_metrics": league_stats["one_x_two_metrics"],
            "observation_gate_funnel": league_stats["observation_gate_funnel"],
            "exact_scores": league_stats["exact_scores"],
            "recent_learnings": league_stats["recent_learnings"],
        }
    return profiles


def cmd_calibrate(args: argparse.Namespace) -> dict[str, Any]:
    history_file = data_path(args.base_dir)
    output_file = calibration_path(args.base_dir)
    history = load_history(history_file)
    stats = calculate_stats(
        history,
        gate_windows=tuple(getattr(args, "gate_windows", (50, 100))),
    )
    existing: dict[str, Any] = {}
    if output_file.exists():
        loaded = json.loads(output_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    supplied_guardrails = (
        args.guardrail if args.guardrail is not None else existing.get("guardrails", [])
    )
    supplied_guardrails = [
        item for item in supplied_guardrails if item not in OBSOLETE_GUARDRAILS
    ]
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
    shadow_eligibility = shadow_review_trigger_by_market(
        stats["shadow_selection_by_market"], minimum
    )
    calibration = {
        "updated_at": now_iso(),
        "history_path": str(history_file),
        "reviewed_matches": stats["reviewed_matches"],
        "primary_record_matches": stats["primary_record_matches"],
        "no_primary_reviewed_matches": stats["no_primary_reviewed_matches"],
        "strict_no_primary_reviewed_matches": stats[
            "strict_no_primary_reviewed_matches"
        ],
        "learning_samples": stats["learning_samples"],
        "selection_policy": stats["selection_policy"],
        "one_x_two_metrics": stats["one_x_two_metrics"],
        "observation_gate_funnel": stats["observation_gate_funnel"],
        "minimum_graded_per_market_for_manual_review": minimum,
        "sample_review_trigger_met_by_market": eligibility,
        "shadow_selection_by_market": stats["shadow_selection_by_market"],
        "shadow_review_trigger_met_by_market": shadow_eligibility,
        "release_blocker_funnel": stats["release_blocker_funnel"],
        "recent_candidate_gate_funnels": stats["recent_candidate_gate_funnels"],
        "weight_change_eligible": {market: False for market in eligibility},
        "active_weight_adjustments": {},
        "parameter_change_authorized": False,
        "market_status_policy_version": STRICT_OOS_POLICY_VERSION,
        "market_status": deepcopy(STRICT_OOS_MARKET_STATUS),
        "untouched_live_forward": stats["untouched_live_forward"],
        "summary": dynamic_calibration_summary(stats, minimum),
        "guardrails": guardrails,
        "stats": stats,
        "league_profiles": league_calibration_profiles(stats, minimum),
    }
    if not any(eligibility.values()) and not any(shadow_eligibility.values()):
        calibration["decision"] = "hold_insufficient_strict_sample"
    else:
        calibration["decision"] = "manual_model_validation_required"
    if args.write:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        temp = output_file.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(calibration, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(output_file)
    return {
        "ok": True,
        "path": str(output_file),
        "written": args.write,
        "calibration": calibration,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir", help="Workspace root; defaults to the current directory"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser(
        "record", help="Create or update a pending pre-match prediction"
    )
    record.add_argument("--match-id", required=True)
    record.add_argument(
        "--forward-validation-ledger",
        help=(
            "pre-match forward-observations/3.0.0 JSON with only pending settlements; "
            "required whenever an untouched live-forward cohort is active"
        ),
    )
    record.add_argument(
        "--analysis-stage", choices=("initial", "lineup-check"), default="initial"
    )
    record.add_argument("--league", required=True)
    record.add_argument(
        "--league-key",
        help="canonical model/registry key; defaults to normalized --league",
    )
    record.add_argument(
        "--kickoff",
        required=True,
        help="User-local kickoff datetime with explicit timezone",
    )
    record.add_argument(
        "--page-status",
        required=True,
        choices=("prematch", "live", "finished"),
        help="Explicit source-page state; only prematch can be archived",
    )
    record.add_argument("--source-kickoff", required=True)
    record.add_argument("--source-timezone", required=True)
    record.add_argument("--user-local-kickoff", required=True)
    record.add_argument("--user-timezone", required=True)
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
    record.add_argument("--competition-key")
    record.add_argument("--competition-label")
    record.add_argument("--competition-id")
    record.add_argument("--competition-verification-source")
    record.add_argument("--competition-source-locator")
    record.add_argument("--competition-collected-at")
    record.add_argument(
        "--require-complete-analysis",
        action="store_true",
        default=True,
        help=(
            "Fail closed unless a valid source-bound joint scenario artifact "
            "is archived for this version (enabled by default)"
        ),
    )
    record.add_argument("--notes", default="")
    record.add_argument("--model-version")
    record.add_argument(
        "--score-model-file",
        help="UTF-8 JSON score-model snapshot; its exact bytes are SHA-256 archived",
    )
    record.add_argument(
        "--joint-scenario-file",
        help=(
            "Validated soccer_joint_scenario_prediction JSON for the same "
            "fixture; archived as an immutable diagnostic snapshot"
        ),
    )
    record.add_argument(
        "--htft-observation-model-file",
        help=(
            "Registered soccer_htft_prediction JSON used only for a structured "
            "observation audit; requires --htft-observation-ranker-file"
        ),
    )
    record.add_argument(
        "--htft-observation-ranker-file",
        help=(
            "JSON output from htft_ranker.py for the same pre-kickoff model; "
            "archived as observation-only and never as a formal pick"
        ),
    )
    record.add_argument(
        "--corner-observation-model-dir",
        help=(
            "Directory containing the registered corner model lineage; requires "
            "both corner observation JSON files"
        ),
    )
    record.add_argument(
        "--corner-observation-prediction-file",
        help=(
            "Registered pre-kickoff corner prediction JSON, archived only as "
            "diagnostic observation evidence"
        ),
    )
    record.add_argument(
        "--corner-observation-ranker-file",
        help=(
            "Validated corner_ranker JSON for the same prediction; never creates "
            "a formal pick or stake"
        ),
    )
    record.add_argument(
        "--source-evidence-file",
        help=(
            "source-evidence/2.0.0 bundle rebuilt from content-addressed visible-page "
            "snapshots; mandatory while an untouched forward cohort is active"
        ),
    )
    record.add_argument(
        "--fundamental-evidence-file",
        help=(
            "fundamental-evidence/1.0.0 bundle replayed from content-addressed "
            "fixture sources; mandatory while an untouched forward cohort is active"
        ),
    )
    record.add_argument(
        "--candidate-evaluation-file",
        help=(
            "candidate-evaluation/3.0.0 multi-market audit artifact; probabilities, "
            "EV, edge, timing, gates, and shadow selection are revalidated before archive"
        ),
    )
    record.add_argument(
        "--require-candidate-evaluations",
        action="store_true",
        help=(
            "Fail closed unless --candidate-evaluation-file supplies an evaluated or "
            "explicitly unavailable manifest entry for every supported market"
        ),
    )
    record.add_argument(
        "--data-quality",
        choices=("high", "medium", "low", "unknown"),
        default="unknown",
    )
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
    record.add_argument(
        "--primary-htft-selection",
        help="Required when the HT/FT primary must be selected from multiple picks",
    )
    record.add_argument("--home-win-probability", type=float)
    record.add_argument("--draw-probability", type=float)
    record.add_argument("--away-win-probability", type=float)
    record.add_argument("--asian-side", choices=("home", "away"))
    record.add_argument("--asian-line", type=float)
    record.add_argument("--asian-odds", type=float)
    record.add_argument("--asian-odds-format", choices=("decimal", "hong_kong"))
    record.add_argument("--asian-probability", type=float)
    record.add_argument("--asian-ev", type=float)
    record.add_argument(
        "--asian-edge-pp",
        type=float,
        help="Model probability minus no-vig market probability in percentage points",
    )
    record.add_argument("--asian-firm-count", type=int)
    record.add_argument("--asian-market-complete", action="store_true")
    record.add_argument("--asian-market-odds", action="append")
    record.add_argument("--asian-market-probability", type=float)
    record.add_argument("--asian-market-source")
    record.add_argument("--asian-market-collected-at")
    record.add_argument("--asian-price-basis", choices=("consensus", "median"))
    record.add_argument("--asian-full-win-probability", type=float)
    record.add_argument("--asian-half-win-probability", type=float)
    record.add_argument("--asian-push-probability", type=float)
    record.add_argument("--asian-half-loss-probability", type=float)
    record.add_argument("--asian-loss-probability", type=float)
    record.add_argument("--asian-cover-probability", type=float)
    record.add_argument("--asian-cover-distribution-validated", action="store_true")
    record.add_argument(
        "--asian-market-signal",
        choices=("aligned", "neutral", "against", "conflicting", "unknown"),
        default="unknown",
    )
    record.add_argument("--total-side", choices=("over", "under"))
    record.add_argument("--total-line", type=float)
    record.add_argument("--total-odds", type=float)
    record.add_argument("--total-odds-format", choices=("decimal", "hong_kong"))
    record.add_argument("--total-probability", type=float)
    record.add_argument("--total-ev", type=float)
    record.add_argument(
        "--total-edge-pp",
        type=float,
        help="Model probability minus no-vig market probability in percentage points",
    )
    record.add_argument("--total-firm-count", type=int)
    record.add_argument("--total-market-complete", action="store_true")
    record.add_argument("--total-market-odds", action="append")
    record.add_argument("--total-market-probability", type=float)
    record.add_argument("--total-market-source")
    record.add_argument("--total-market-collected-at")
    record.add_argument("--total-price-basis", choices=("consensus", "median"))
    record.add_argument("--total-full-win-probability", type=float)
    record.add_argument("--total-half-win-probability", type=float)
    record.add_argument("--total-push-probability", type=float)
    record.add_argument("--total-half-loss-probability", type=float)
    record.add_argument("--total-loss-probability", type=float)
    record.add_argument(
        "--total-market-signal",
        choices=("aligned", "neutral", "against", "conflicting", "unknown"),
        default="unknown",
    )
    record.add_argument("--half-market", choices=("1x2", "asian", "total"))
    record.add_argument(
        "--half-side", choices=("home", "draw", "away", "over", "under")
    )
    record.add_argument("--half-line", type=float)
    record.add_argument("--half-odds", type=float)
    record.add_argument("--half-odds-format", choices=("decimal", "hong_kong"))
    record.add_argument("--half-probability", type=float)
    record.add_argument("--half-ev", type=float)
    record.add_argument("--half-edge-pp", type=float)
    record.add_argument("--half-firm-count", type=int)
    record.add_argument("--half-market-complete", action="store_true")
    record.add_argument("--half-market-odds", action="append")
    record.add_argument("--half-market-probability", type=float)
    record.add_argument("--half-market-source")
    record.add_argument("--half-market-collected-at")
    record.add_argument("--half-price-basis", choices=("consensus", "median"))
    record.add_argument("--half-full-win-probability", type=float)
    record.add_argument("--half-half-win-probability", type=float)
    record.add_argument("--half-push-probability", type=float)
    record.add_argument("--half-half-loss-probability", type=float)
    record.add_argument("--half-loss-probability", type=float)
    record.add_argument(
        "--half-market-signal",
        choices=("aligned", "neutral", "against", "conflicting", "unknown"),
        default="unknown",
    )
    record.add_argument(
        "--htft-pick",
        action="append",
        help="Repeatable SELECTION:ODDS:PROBABILITY:EV, e.g. DD:3.40:0.31:0.054",
    )
    record.add_argument("--htft-odds-format", choices=("decimal", "hong_kong"))
    record.add_argument("--htft-market-complete", action="store_true")
    record.add_argument("--htft-market-odds", action="append")
    record.add_argument(
        "--htft-market-probability",
        action="append",
        help="Required nine times as SELECTION:NO_VIG_PROBABILITY",
    )
    record.add_argument(
        "--htft-edge-pp",
        action="append",
        help="Optional repeatable SELECTION:EDGE_PP; supplied values are audited",
    )
    record.add_argument("--htft-market-source")
    record.add_argument("--htft-market-collected-at")
    record.add_argument("--htft-price-basis", choices=("consensus", "median"))
    record.add_argument("--htft-firm-count", type=int)
    record.add_argument(
        "--htft-market-signal",
        choices=("aligned", "neutral", "against", "conflicting", "unknown"),
        default="unknown",
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
    record.add_argument("--goal-range-market-odds", action="append")
    record.add_argument("--goal-range-market-probability", type=float)
    record.add_argument("--goal-range-market-source")
    record.add_argument("--goal-range-market-collected-at")
    record.add_argument("--goal-range-price-basis", choices=("consensus", "median"))
    record.add_argument("--btts-side", choices=("yes", "no"))
    record.add_argument("--btts-odds", type=float)
    record.add_argument("--btts-odds-format", choices=("decimal", "hong_kong"))
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
    record.add_argument("--btts-market-odds", action="append")
    record.add_argument("--btts-market-probability", type=float)
    record.add_argument("--btts-market-source")
    record.add_argument("--btts-market-collected-at")
    record.add_argument("--btts-price-basis", choices=("consensus", "median"))
    record.add_argument("--corner-total-side", choices=("over", "under"))
    record.add_argument("--corner-total-line", type=float)
    record.add_argument("--corner-total-odds", type=float)
    record.add_argument("--corner-total-odds-format", choices=("decimal", "hong_kong"))
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
    record.add_argument("--corner-total-market-odds", action="append")
    record.add_argument("--corner-total-market-probability", type=float)
    record.add_argument("--corner-total-market-source")
    record.add_argument("--corner-total-market-collected-at")
    record.add_argument("--corner-total-price-basis", choices=("consensus", "median"))
    record.add_argument("--corner-total-full-win-probability", type=float)
    record.add_argument("--corner-total-half-win-probability", type=float)
    record.add_argument("--corner-total-push-probability", type=float)
    record.add_argument("--corner-total-half-loss-probability", type=float)
    record.add_argument("--corner-total-loss-probability", type=float)
    record.add_argument("--corner-handicap-side", choices=("home", "away"))
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
    record.add_argument("--corner-handicap-market-complete", action="store_true")
    record.add_argument("--corner-handicap-market-odds", action="append")
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
    record.add_argument("--force", action="store_true", help=argparse.SUPPRESS)
    review = sub.add_parser(
        "review", help="Settle an archived prediction after verified full-time"
    )
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
    review.add_argument("--verification-source", required=True)
    review.add_argument("--verification-collected-at", required=True)

    migrate = sub.add_parser(
        "migrate-primary",
        help="Backfill one active primary pick without re-settling reviewed matches",
    )
    migrate.add_argument(
        "--primary",
        action="append",
        required=True,
        help="Repeatable MATCH_ID:MARKET[:HTFT_SELECTION] assignment",
    )
    migrate.add_argument(
        "--write", action="store_true", help="Persist the compatibility migration"
    )

    migrate_leagues = sub.add_parser(
        "migrate-leagues",
        help="Backfill normalized league keys without changing revisions or settlements",
    )
    migrate_leagues.add_argument(
        "--write", action="store_true", help="Persist league-key migration"
    )

    competition = sub.add_parser(
        "attach-competition-evidence",
        help="Attach source-verified fixture competition metadata without changing predictions",
    )
    competition.add_argument("--match-id", required=True)
    competition.add_argument("--competition-key", required=True)
    competition.add_argument("--competition-label", required=True)
    competition.add_argument("--competition-id", required=True)
    competition.add_argument("--verification-source", required=True)
    competition.add_argument("--source-locator", required=True)
    competition.add_argument("--collected-at", required=True)
    competition.add_argument("--write", action="store_true")

    migrate_basis = sub.add_parser(
        "migrate-settlement-basis",
        help="Backfill active-version settlement metadata without re-grading matches",
    )
    migrate_basis.add_argument(
        "--write", action="store_true", help="Persist settlement-basis metadata"
    )

    migrate_learning = sub.add_parser(
        "migrate-learning-scopes",
        help="Backfill machine-readable review-learning metadata without re-grading",
    )
    migrate_learning.add_argument(
        "--write", action="store_true", help="Persist review-learning metadata"
    )

    export_manifest = sub.add_parser(
        "export-forward-record-manifest",
        help="Preview the complete canonical record manifest for one forward cohort",
    )
    export_manifest.add_argument(
        "--output",
        required=True,
        help=(
            "Exact COHORT_ID-record-manifest.json path inside "
            ".codex/soccer-predict/forward-record-manifest-exports"
        ),
    )
    export_manifest.add_argument("--cohort-id", required=True)

    close_forward = sub.add_parser(
        "close-forward-cohort",
        help=(
            "Atomically build the complete record manifest from history and close the "
            "active forward cohort"
        ),
    )
    close_forward.add_argument("--cohort-id", required=True)
    close_forward.add_argument("--closed-at")
    close_forward.add_argument(
        "--record-manifest-output",
        help=(
            "Optional exact COHORT_ID-record-manifest.json path inside "
            ".codex/soccer-predict/forward-record-manifest-exports; defaults beside "
            "the immutable cohort manifests"
        ),
    )

    export_forward = sub.add_parser(
        "export-forward-validation",
        help=(
            "Export a receipt-bound forward-validation cohort envelope from archived "
            "memory-store records"
        ),
    )
    export_forward.add_argument("--output", required=True)
    export_forward.add_argument("--cohort-id", required=True)
    export_forward.add_argument(
        "--cohort-closure-file",
        required=True,
        help="Record-manifest-bound immutable cohort closure",
    )

    sub.add_parser("pending", help="List pending pre-match predictions")
    due = sub.add_parser(
        "due-lineup-check",
        help="List pending matches due in the final 30 minutes before kickoff",
    )
    due.add_argument(
        "--now", help="Override current time with an ISO datetime including timezone"
    )
    due.add_argument("--min-minutes", type=float, default=0.0)
    due.add_argument("--max-minutes", type=float, default=30.0)
    stats = sub.add_parser(
        "stats", help="Print cumulative accuracy and recent candidate-gate diagnostics"
    )
    stats.add_argument(
        "--gate-windows",
        type=int,
        nargs="+",
        default=[50, 100],
        help="Distinct-match windows for descriptive gate diagnostics",
    )
    gate_stats = sub.add_parser(
        "gate-stats",
        help="Replay recent candidate gates without calculating settlement performance",
    )
    gate_stats.add_argument("--windows", type=int, nargs="+", default=[50, 100])
    calibrate = sub.add_parser(
        "calibrate",
        help="Summarize reviewed performance and persist cautious calibration state",
    )
    calibrate.add_argument(
        "--write",
        action="store_true",
        help="Persist calibration.json beside history.json",
    )
    calibrate.add_argument("--minimum-graded", type=int, default=20)
    calibrate.add_argument(
        "--gate-windows",
        type=int,
        nargs="+",
        default=[50, 100],
        help="Distinct-match windows mirrored into calibration diagnostics",
    )
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
        elif args.command == "attach-competition-evidence":
            result = cmd_attach_competition_evidence(args)
        elif args.command == "migrate-settlement-basis":
            result = cmd_migrate_settlement_basis(args)
        elif args.command == "migrate-learning-scopes":
            result = cmd_migrate_learning_scopes(args)
        elif args.command == "export-forward-record-manifest":
            result = cmd_export_forward_record_manifest(args)
        elif args.command == "close-forward-cohort":
            result = cmd_close_forward_cohort(args)
        elif args.command == "export-forward-validation":
            result = cmd_export_forward_validation(args)
        elif args.command == "due-lineup-check":
            if args.min_minutes < 0 or args.max_minutes < args.min_minutes:
                raise ValueError("Require 0 <= min-minutes <= max-minutes")
            result = cmd_due_lineup_check(args)
        elif args.command == "calibrate":
            if args.minimum_graded < 1:
                raise ValueError("--minimum-graded must be at least 1")
            if any(window < 1 for window in args.gate_windows):
                raise ValueError("--gate-windows must contain positive integers")
            result = cmd_calibrate(args)
        elif args.command == "gate-stats":
            if any(window < 1 for window in args.windows):
                raise ValueError("--windows must contain positive integers")
            result = {
                "path": str(path),
                "recent_candidate_gate_funnels": _recent_candidate_gate_funnels(
                    load_history(path), windows=tuple(args.windows)
                ),
            }
        else:
            history = load_history(path)
            if args.command == "pending":
                result = {
                    "path": str(path),
                    "pending": [
                        r
                        for r in history
                        if r.get("mode") == "prematch" and r.get("status") == "pending"
                    ],
                }
            else:
                if any(window < 1 for window in args.gate_windows):
                    raise ValueError("--gate-windows must contain positive integers")
                result = {
                    "path": str(path),
                    "stats": calculate_stats(
                        history, gate_windows=tuple(args.gate_windows)
                    ),
                }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

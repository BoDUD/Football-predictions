#!/usr/bin/env python3
"""Deterministic workspace-local storage for soccer-predict."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
import sys
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
DEFAULT_GUARDRAILS = [
    "盘口与相关欧赔同时明显反向时，普通低EV方向降为观察；仅当EV>=8%、边际>=4pp、至少5家公司且有独立阵容或基本面支持时可作正式方向，主推同样受此门槛约束。",
    "伤停表与确认首发冲突时，以确认首发为准；旧伤停不得继续作为进球或让球方向的支持证据。",
    "大小球降水不能单独构成主推依据；必须同时取得多家公司一致性和进攻配置或机会质量证据。",
    "两个精确比分候选仅作比赛形态参考；分别记录Top-1/Top-2诊断，不计入主推或全部正式方向的命中率与ROI。",
]
OBSOLETE_GUARDRAILS = {
    "若亚盘与相关欧赔一致明显反向，常规低EV方向降级为观察；只有EV>=8%、边际>=4pp、至少5家公司且有独立阵容或基本面证据时才能正式推荐。",
    "精确比分仅作比赛形态参考，不计入主推命中率。",
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
        "probabilities": record.get("probabilities"),
        "asian_pick": record.get("asian_pick"),
        "total_pick": record.get("total_pick"),
        "half_time_pick": record.get("half_time_pick"),
        "htft_picks": record.get("htft_picks", []),
        "primary_market": record.get("primary_market"),
        "primary_pick": record.get("primary_pick"),
        "primary_change": record.get("primary_change"),
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
    previous_primary = active_primary_identity(existing)
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
        "kickoff": args.kickoff,
        "home_team": args.home_team,
        "away_team": args.away_team,
        "predicted_score": args.predicted_score,
        "exact_score_picks": exact_score_picks,
        "recommendation": args.recommendation,
        "source_url": args.source_url,
        "notes": args.notes,
        "data_quality": args.data_quality,
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
            "market_signal": args.asian_market_signal,
        }
    if args.total_side:
        record["total_pick"] = {
            "side": args.total_side,
            "line": args.total_line,
            "odds": args.total_odds,
            "probability": args.total_probability,
            "ev": args.total_ev,
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
            "market_signal": args.half_market_signal,
        }
    if args.htft_pick:
        record["htft_picks"] = [parse_htft_pick(value) for value in args.htft_pick]

    apply_primary_role(record, args.primary_market, args.primary_htft_selection)
    current_primary = active_primary_identity(record)
    if args.analysis_stage == "lineup-check":
        change_status = "maintained" if existing and previous_primary == current_primary else "changed"
    else:
        change_status = "initial"
    record["primary_change"] = {
        "status": change_status,
        "previous": list(previous_primary) if previous_primary else None,
        "current": list(current_primary) if current_primary else None,
    }

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
        return {
            "ok": True,
            "already_reviewed": True,
            "path": str(path),
            "match_id": str(record.get("match_id")),
            "final_score": record.get("final_score"),
            "reviewed_at": record.get("reviewed_at"),
            "record": record,
            "stats": calculate_stats(history),
        }

    if not args.key_learning.strip():
        raise ValueError("Review requires a concise non-empty --key-learning grounded in the verified result")

    home, away = int(args.home_score), int(args.away_score)
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
    record.update({
        "status": "reviewed",
        "reviewed_at": now_iso(),
        "final_score": f"{home}-{away}",
        "score_exact": predicted_exact,
        "exact_score_hit_rank": exact_score_hit_rank,
        "exact_score_any_hit": exact_score_hit_rank in {1, 2},
        "asian_result": settle_asian(record.get("asian_pick"), home, away),
        "total_result": settle_total(record.get("total_pick"), home, away),
        "half_time_score": f"{half_home}-{half_away}" if half_scores_available else None,
        "half_time_result": settle_half_time(record.get("half_time_pick"), half_home, half_away) if half_scores_available else None,
        "htft_results": settle_htft(record.get("htft_picks"), half_home, half_away, home, away) if half_scores_available else [],
        "key_learning": args.key_learning,
    })
    record["primary_result"] = primary_result_from_record(record)
    warnings = []
    if (record.get("half_time_pick") or record.get("htft_picks")) and not half_scores_available:
        warnings.append("Half-time score was not supplied; half-time and HT/FT picks remain ungraded")
    save_history(path, history)
    return {"ok": True, "path": str(path), "record": record, "warnings": warnings, "stats": calculate_stats(history)}


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


def performance_block(pairs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    block = rate_block([result for result, _ in pairs])
    profits = [settlement_profit(result, pick.get("odds")) for result, pick in pairs]
    settled_profits = [value for value in profits if value is not None]
    archived_evs = [float(pick["ev"]) for _, pick in pairs if pick.get("ev") is not None]
    block.update({
        "stake_units": len(settled_profits),
        "profit_units": round(sum(settled_profits), 4),
        "roi": round(sum(settled_profits) / len(settled_profits), 4) if settled_profits else None,
        "avg_archived_ev": round(sum(archived_evs) / len(archived_evs), 4) if archived_evs else None,
    })
    signals: dict[str, dict[str, Any]] = {}
    for signal in sorted({str(pick.get("market_signal", "unknown")) for _, pick in pairs}):
        subset = [(result, pick) for result, pick in pairs if str(pick.get("market_signal", "unknown")) == signal]
        signals[signal] = performance_block_without_signals(subset)
    block["by_market_signal"] = signals
    return block


def performance_block_without_signals(pairs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    block = rate_block([result for result, _ in pairs])
    profits = [settlement_profit(result, pick.get("odds")) for result, pick in pairs]
    settled_profits = [value for value in profits if value is not None]
    block.update({
        "stake_units": len(settled_profits),
        "profit_units": round(sum(settled_profits), 4),
        "roi": round(sum(settled_profits) / len(settled_profits), 4) if settled_profits else None,
    })
    return block


def market_pairs(records: list[dict[str, Any]], result_key: str, pick_key: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (str(record[result_key]), record[pick_key])
        for record in records
        if record.get(result_key) and isinstance(record.get(pick_key), dict)
    ]


def htft_pairs(records: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    pairs: list[tuple[str, dict[str, Any]]] = []
    for record in records:
        for result, pick in zip(record.get("htft_results", []), record.get("htft_picks", [])):
            if result and isinstance(pick, dict):
                pairs.append((str(result), pick))
    return pairs


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


def calculate_stats(history: list[dict[str, Any]]) -> dict[str, Any]:
    reviewed = [r for r in history if r.get("mode") == "prematch" and r.get("status") == "reviewed"]
    asian = market_pairs(reviewed, "asian_result", "asian_pick")
    totals = market_pairs(reviewed, "total_result", "total_pick")
    half_time = market_pairs(reviewed, "half_time_result", "half_time_pick")
    htft = htft_pairs(reviewed)
    primary = primary_pairs(reviewed)
    exact_top1 = sum((r.get("exact_score_hit_rank") == 1) or bool(r.get("score_exact")) for r in reviewed)
    exact_top2 = sum(
        (r.get("exact_score_hit_rank") in {1, 2})
        or (r.get("exact_score_hit_rank") is None and bool(r.get("score_exact")))
        for r in reviewed
    )
    leagues: dict[str, dict[str, Any]] = {}
    for league in sorted({str(r.get("league", "unknown")) for r in reviewed}):
        subset = [r for r in reviewed if str(r.get("league", "unknown")) == league]
        leagues[league] = {
            "matches": len(subset),
            "primary": performance_block(primary_pairs(subset)),
            "asian": performance_block(market_pairs(subset, "asian_result", "asian_pick")),
            "totals": performance_block(market_pairs(subset, "total_result", "total_pick")),
            "half_time": performance_block(market_pairs(subset, "half_time_result", "half_time_pick")),
            "htft": performance_block(htft_pairs(subset)),
        }
    combined = asian + totals + half_time + htft
    all_formal = {
        "asian": performance_block(asian),
        "totals": performance_block(totals),
        "half_time": performance_block(half_time),
        "htft": performance_block(htft),
        "combined": performance_block(combined),
    }
    return {
        "reviewed_matches": len(reviewed),
        "pending_matches": sum(r.get("mode") == "prematch" and r.get("status") == "pending" for r in history),
        "primary": performance_block(primary),
        "all_formal": all_formal,
        "asian": all_formal["asian"],
        "totals": all_formal["totals"],
        "half_time": all_formal["half_time"],
        "htft": all_formal["htft"],
        "combined": all_formal["combined"],
        "exact_scores": exact_top1,
        "exact_score_rate": round(exact_top1 / len(reviewed), 4) if reviewed else None,
        "exact_score_top1_hits": exact_top1,
        "exact_score_top1_rate": round(exact_top1 / len(reviewed), 4) if reviewed else None,
        "exact_score_top2_hits": exact_top2,
        "exact_score_top2_rate": round(exact_top2 / len(reviewed), 4) if reviewed else None,
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
    all_formal = stats["all_formal"]["combined"]

    def roi_text(block: dict[str, Any]) -> str:
        roi = block.get("roi")
        return "—" if roi is None else f"{float(roi) * 100:+.2f}%"

    return (
        f"已复盘{stats['reviewed_matches']}场；主推{primary['matches']}场"
        f"{primary['wins']}胜{primary['losses']}负{primary['pushes']}走，"
        f"收益{primary['profit_units']:+.2f}u，ROI {roi_text(primary)}。"
        f"全部正式方向{all_formal['matches']}项"
        f"{all_formal['wins']}胜{all_formal['losses']}负{all_formal['pushes']}走，"
        f"收益{all_formal['profit_units']:+.2f}u，ROI {roi_text(all_formal)}。"
        f"单市场不足{minimum}个有效样本时只保存guardrail，不调整全局权重。"
    )


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
        market: stats["all_formal"][market]["graded"] >= minimum
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
    record.add_argument("--asian-market-signal", choices=("aligned", "neutral", "against", "conflicting", "unknown"), default="unknown")
    record.add_argument("--total-side", choices=("over", "under"))
    record.add_argument("--total-line", type=float)
    record.add_argument("--total-odds", type=float)
    record.add_argument("--total-probability", type=float)
    record.add_argument("--total-ev", type=float)
    record.add_argument("--total-market-signal", choices=("aligned", "neutral", "against", "conflicting", "unknown"), default="unknown")
    record.add_argument("--half-market", choices=("1x2", "asian", "total"))
    record.add_argument("--half-side", choices=("home", "draw", "away", "over", "under"))
    record.add_argument("--half-line", type=float)
    record.add_argument("--half-odds", type=float)
    record.add_argument("--half-probability", type=float)
    record.add_argument("--half-ev", type=float)
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

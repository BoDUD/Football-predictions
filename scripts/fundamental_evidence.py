#!/usr/bin/env python3
"""Build replayable, source-bound fixture fundamentals and guardrail claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

try:
    from scripts import source_evidence
except ImportError:  # Direct execution from scripts/.
    import source_evidence  # type: ignore[no-redef]

PREVIOUS_RAW_SCHEMA_VERSION = "visible-fundamental-snapshot/1.0.0"
RAW_SCHEMA_VERSION = "visible-fundamental-snapshot/2.0.0"
PREVIOUS_EVIDENCE_SCHEMA_VERSION = "fundamental-evidence/1.0.0"
EVIDENCE_SCHEMA_VERSION = "fundamental-evidence/2.0.0"
PARSER_VERSION = "visible-fundamental-parser/2.0.0"
PREVIOUS_PARSER_VERSION = "visible-fundamental-parser/1.0.0"
SUPPORT_RULE_VERSION = "candidate-fundamental-support/1.0.0-shadow"
CLAIM_FIELDS = (
    "lineup_confirmed",
    "fundamental_supported",
    "chance_quality_supported",
    "attack_configuration_supported",
    "opponent_tail_risk_checked",
    "corner_profile_supported",
)
AVAILABILITY_CLAIM_FIELDS = (
    "confirmed_lineups_available",
    "fundamentals_available",
    "chance_quality_available",
    "attack_configuration_available",
    "opponent_tail_risk_available",
    "corner_profile_available",
)
SOURCE_CLASSES = frozenset(
    {
        "official_confirmed",
        "verified_provider",
        "predicted_lineup",
        "statistical_provider",
    }
)


class FundamentalEvidenceError(ValueError):
    """Raised when fundamental evidence cannot be replayed from frozen bytes."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _hash_json(value: Any) -> str:
    return _hash_bytes(_canonical(value))


def _aware(value: Any, label: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise FundamentalEvidenceError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FundamentalEvidenceError(f"{label} must include an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _text(value: Any, label: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise FundamentalEvidenceError(f"{label} is required")
    return text


def _url(value: Any) -> str:
    url = _text(value, "source_url")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FundamentalEvidenceError("source_url must be an https URL")
    return url


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise FundamentalEvidenceError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FundamentalEvidenceError(f"{label} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise FundamentalEvidenceError(f"{label} must be finite and non-negative")
    return number


def parse_snapshot(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FundamentalEvidenceError(
            "fundamental snapshot is not UTF-8 JSON"
        ) from exc
    if not isinstance(payload, Mapping):
        raise FundamentalEvidenceError(
            "fundamental snapshot schema_version is unsupported"
        )
    schema_version = payload.get("schema_version")
    if schema_version not in {PREVIOUS_RAW_SCHEMA_VERSION, RAW_SCHEMA_VERSION}:
        raise FundamentalEvidenceError(
            "fundamental snapshot schema_version is unsupported"
        )
    source_class = None
    if schema_version == RAW_SCHEMA_VERSION:
        source_class = _text(payload.get("source_class"), "source_class")
        if source_class not in SOURCE_CLASSES:
            raise FundamentalEvidenceError("source_class is unsupported")
    fixture = payload.get("fixture")
    if not isinstance(fixture, Mapping):
        raise FundamentalEvidenceError("fundamental snapshot fixture is missing")
    kickoff = _aware(fixture.get("kickoff"), "fixture.kickoff")
    collected = _aware(payload.get("collected_at"), "collected_at")
    if collected >= kickoff:
        raise FundamentalEvidenceError(
            "fundamental snapshot was not collected before kickoff"
        )
    normalized_fixture = {
        "match_id": _text(fixture.get("match_id"), "fixture.match_id"),
        "home_team": _text(fixture.get("home_team"), "fixture.home_team"),
        "away_team": _text(fixture.get("away_team"), "fixture.away_team"),
        "kickoff": kickoff.isoformat(),
    }
    lineups = payload.get("confirmed_lineups")
    normalized_lineups = None
    if lineups is not None:
        if not isinstance(lineups, Mapping):
            raise FundamentalEvidenceError("confirmed_lineups must be an object")
        normalized_lineups = {}
        for side in ("home", "away"):
            players = lineups.get(side)
            if (
                not isinstance(players, list)
                or len(players) != 11
                or len(
                    {_text(player, f"confirmed_lineups.{side}") for player in players}
                )
                != 11
            ):
                raise FundamentalEvidenceError(
                    f"confirmed_lineups.{side} must contain 11 unique players"
                )
            normalized_lineups[side] = [
                _text(player, f"confirmed_lineups.{side}") for player in players
            ]
        if schema_version == RAW_SCHEMA_VERSION and source_class not in {
            "official_confirmed",
            "verified_provider",
        }:
            raise FundamentalEvidenceError(
                "only official_confirmed or verified_provider sources can confirm lineups"
            )
    raw_fundamentals = payload.get("fundamentals")
    fundamentals = None
    if raw_fundamentals is not None:
        if not isinstance(raw_fundamentals, Mapping):
            raise FundamentalEvidenceError("fundamentals must be an object")
        fundamentals = {
            side: {
                "sample_matches": int(
                    _finite(
                        raw_fundamentals.get(side, {}).get("sample_matches"),
                        f"fundamentals.{side}.sample_matches",
                    )
                ),
                "goals_for_per_match": _finite(
                    raw_fundamentals.get(side, {}).get("goals_for_per_match"),
                    f"fundamentals.{side}.goals_for_per_match",
                ),
                "goals_against_per_match": _finite(
                    raw_fundamentals.get(side, {}).get("goals_against_per_match"),
                    f"fundamentals.{side}.goals_against_per_match",
                ),
            }
            for side in ("home", "away")
        }
    raw_chance = payload.get("chance_quality")
    chance_quality = None
    if raw_chance is not None:
        if not isinstance(raw_chance, Mapping):
            raise FundamentalEvidenceError("chance_quality must be an object")
        chance_quality = {
            side: {
                "sample_matches": int(
                    _finite(
                        raw_chance.get(side, {}).get("sample_matches"),
                        f"chance_quality.{side}.sample_matches",
                    )
                ),
                "xg_per_match": _finite(
                    raw_chance.get(side, {}).get("xg_per_match"),
                    f"chance_quality.{side}.xg_per_match",
                ),
                "xga_per_match": _finite(
                    raw_chance.get(side, {}).get("xga_per_match"),
                    f"chance_quality.{side}.xga_per_match",
                ),
            }
            for side in ("home", "away")
        }
    raw_attack = payload.get("attack_configuration")
    attack_configuration = None
    if raw_attack is not None:
        if not isinstance(raw_attack, Mapping):
            raise FundamentalEvidenceError("attack_configuration must be an object")
        attack_configuration = {
            side: {
                "formation": _text(
                    raw_attack.get(side, {}).get("formation"),
                    f"attack_configuration.{side}.formation",
                ),
                "recognized_attackers": sorted(
                    {
                        _text(item, f"attack_configuration.{side}.recognized_attackers")
                        for item in raw_attack.get(side, {}).get(
                            "recognized_attackers", []
                        )
                    }
                ),
            }
            for side in ("home", "away")
        }
        if schema_version == RAW_SCHEMA_VERSION and normalized_lineups is not None:
            for side in ("home", "away"):
                if not set(attack_configuration[side]["recognized_attackers"]).issubset(
                    set(normalized_lineups[side])
                ):
                    raise FundamentalEvidenceError(
                        f"attack_configuration.{side}.recognized_attackers must be confirmed starters"
                    )
    raw_tail = payload.get("opponent_tail_risk")
    opponent_tail_risk = None
    if raw_tail is not None:
        if not isinstance(raw_tail, Mapping) or raw_tail.get("checked") is not True:
            raise FundamentalEvidenceError(
                "opponent_tail_risk must explicitly be checked"
            )
        opponent_tail_risk = {
            "checked": True,
            "notes": _text(raw_tail.get("notes"), "opponent_tail_risk.notes"),
        }
    raw_corner = payload.get("corner_profile")
    corner_profile = None
    if raw_corner is not None:
        if not isinstance(raw_corner, Mapping):
            raise FundamentalEvidenceError("corner_profile must be an object")
        corner_profile = {
            side: {
                "sample_matches": int(
                    _finite(
                        raw_corner.get(side, {}).get("sample_matches"),
                        f"corner_profile.{side}.sample_matches",
                    )
                ),
                "corners_for_per_match": _finite(
                    raw_corner.get(side, {}).get("corners_for_per_match"),
                    f"corner_profile.{side}.corners_for_per_match",
                ),
                "corners_against_per_match": _finite(
                    raw_corner.get(side, {}).get("corners_against_per_match"),
                    f"corner_profile.{side}.corners_against_per_match",
                ),
            }
            for side in ("home", "away")
        }
    claims = {
        "lineup_confirmed": normalized_lineups is not None,
        "fundamental_supported": bool(
            fundamentals
            and all(item["sample_matches"] >= 5 for item in fundamentals.values())
        ),
        "chance_quality_supported": bool(
            chance_quality
            and all(item["sample_matches"] >= 5 for item in chance_quality.values())
        ),
        "attack_configuration_supported": bool(
            attack_configuration
            and all(
                len(item["recognized_attackers"]) >= 1
                for item in attack_configuration.values()
            )
        ),
        "opponent_tail_risk_checked": opponent_tail_risk is not None,
        "corner_profile_supported": bool(
            corner_profile
            and all(item["sample_matches"] >= 5 for item in corner_profile.values())
        ),
    }
    availability_claims = {
        "confirmed_lineups_available": normalized_lineups is not None,
        "fundamentals_available": fundamentals is not None,
        "chance_quality_available": chance_quality is not None,
        "attack_configuration_available": attack_configuration is not None,
        "opponent_tail_risk_available": opponent_tail_risk is not None,
        "corner_profile_available": corner_profile is not None,
    }
    support_requests: list[dict[str, Any]] = []
    if schema_version == RAW_SCHEMA_VERSION:
        raw_requests = payload.get("candidate_support_requests", [])
        if not isinstance(raw_requests, list):
            raise FundamentalEvidenceError("candidate_support_requests must be a list")
        seen: set[str] = set()
        for index, request in enumerate(raw_requests):
            if not isinstance(request, Mapping):
                raise FundamentalEvidenceError(
                    f"candidate_support_requests[{index}] must be an object"
                )
            try:
                identity = source_evidence.canonical_market_identity(
                    request.get("market_identity"),
                    label=f"candidate_support_requests[{index}].market_identity",
                )
            except source_evidence.SourceEvidenceError as exc:
                raise FundamentalEvidenceError(
                    f"candidate_support_requests[{index}] market identity is invalid"
                ) from exc
            identity_hash = source_evidence.market_identity_hash(identity)
            if request.get("market_identity_hash") != identity_hash:
                raise FundamentalEvidenceError(
                    f"candidate_support_requests[{index}] market identity hash is invalid"
                )
            market = _text(
                request.get("market"), f"candidate_support_requests[{index}].market"
            ).lower()
            selection = _text(
                request.get("selection"),
                f"candidate_support_requests[{index}].selection",
            )
            if selection not in identity["price_outcomes"]:
                raise FundamentalEvidenceError(
                    f"candidate_support_requests[{index}] selection is outside the market"
                )
            key = f"{identity_hash}:{selection}"
            if key in seen:
                raise FundamentalEvidenceError(
                    "candidate support request is duplicated"
                )
            seen.add(key)
            support_requests.append(
                {
                    "market": market,
                    "selection": selection,
                    "market_identity": identity,
                    "market_identity_hash": identity_hash,
                    "support_key": key,
                }
            )
        support_requests.sort(key=lambda item: item["support_key"])
    result = {
        "schema_version": schema_version,
        "parser_version": (
            PARSER_VERSION
            if schema_version == RAW_SCHEMA_VERSION
            else PREVIOUS_PARSER_VERSION
        ),
        "source_class": source_class,
        "source_url": _url(payload.get("source_url")),
        "collected_at": collected.isoformat(),
        "fixture": normalized_fixture,
        "confirmed_lineups": normalized_lineups,
        "fundamentals": fundamentals,
        "chance_quality": chance_quality,
        "attack_configuration": attack_configuration,
        "opponent_tail_risk": opponent_tail_risk,
        "corner_profile": corner_profile,
        "derived_claims": claims,
        "availability_claims": availability_claims,
        "candidate_support_requests": support_requests,
    }
    if schema_version == PREVIOUS_RAW_SCHEMA_VERSION:
        result.pop("source_class", None)
        result.pop("availability_claims", None)
        result.pop("candidate_support_requests", None)
    return result


def _source_entry(raw: bytes, relative_path: str) -> dict[str, Any]:
    return {
        "raw_response_path": relative_path,
        "raw_response_sha256": _hash_bytes(raw),
        "raw_response_bytes": len(raw),
        "parsed": parse_snapshot(raw),
    }


def _poisson_probability(rate: float, goals: int) -> float:
    return math.exp(-rate) * (rate**goals) / math.factorial(goals)


def _goal_range_probability(rate: float, label: str) -> float | None:
    text = str(label).strip()
    if text.endswith("+") and text[:-1].isdigit():
        lower = int(text[:-1])
        return max(
            0.0, 1.0 - math.fsum(_poisson_probability(rate, i) for i in range(lower))
        )
    parts = text.split("-")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    lower, upper = (int(part) for part in parts)
    if upper < lower:
        return None
    return math.fsum(_poisson_probability(rate, i) for i in range(lower, upper + 1))


def _directional_support(
    parsed: Mapping[str, Any], request: Mapping[str, Any]
) -> tuple[bool, bool, dict[str, Any]]:
    market = str(request["market"])
    selection = str(request["selection"])
    identity = request["market_identity"]
    line = identity.get("line")
    chance = parsed.get("chance_quality")
    corner = parsed.get("corner_profile")
    if market in {"asian", "total", "goal_range", "btts"} and isinstance(
        chance, Mapping
    ):
        home_rate = (
            float(chance["home"]["xg_per_match"])
            + float(chance["away"]["xga_per_match"])
        ) / 2.0
        away_rate = (
            float(chance["away"]["xg_per_match"])
            + float(chance["home"]["xga_per_match"])
        ) / 2.0
        metrics: dict[str, Any] = {
            "home_expected_goals": home_rate,
            "away_expected_goals": away_rate,
            "total_expected_goals": home_rate + away_rate,
        }
        if market == "asian" and isinstance(line, (int, float)):
            tail_risk = parsed.get("opponent_tail_risk")
            if (
                not isinstance(tail_risk, Mapping)
                or tail_risk.get("checked") is not True
            ):
                return False, False, {"reason": "opponent_tail_risk_check_unavailable"}
            adjusted_home_margin = home_rate - away_rate + float(line)
            metrics["adjusted_home_goal_margin"] = adjusted_home_margin
            metrics["opponent_tail_risk_checked"] = True
            if selection.lower() == "home":
                return True, adjusted_home_margin > 0.0, metrics
            if selection.lower() == "away":
                return True, adjusted_home_margin < 0.0, metrics
            return False, False, {"reason": "unsupported_asian_selection"}
        if market == "total" and isinstance(line, (int, float)):
            supported = (
                home_rate + away_rate > float(line)
                if selection.lower() == "over"
                else home_rate + away_rate < float(line)
            )
            return True, supported, metrics
        if market == "btts":
            probability = (1.0 - math.exp(-home_rate)) * (1.0 - math.exp(-away_rate))
            metrics["btts_yes_probability"] = probability
            supported = (
                probability > 0.5 if selection.lower() == "yes" else probability < 0.5
            )
            return True, supported, metrics
        if market == "goal_range":
            probabilities = {
                outcome: _goal_range_probability(home_rate + away_rate, outcome)
                for outcome in identity["price_outcomes"]
            }
            if any(value is None for value in probabilities.values()):
                return False, False, {"reason": "unsupported_goal_range_topology"}
            metrics["range_probabilities"] = probabilities
            maximum = max(float(value) for value in probabilities.values())
            winners = sorted(
                outcome
                for outcome, probability in probabilities.items()
                if math.isclose(float(probability), maximum, abs_tol=1e-12)
            )
            return True, winners == [selection], metrics
    if (
        market == "corner_total"
        and isinstance(corner, Mapping)
        and isinstance(line, (int, float))
    ):
        home_rate = (
            float(corner["home"]["corners_for_per_match"])
            + float(corner["away"]["corners_against_per_match"])
        ) / 2.0
        away_rate = (
            float(corner["away"]["corners_for_per_match"])
            + float(corner["home"]["corners_against_per_match"])
        ) / 2.0
        total = home_rate + away_rate
        return (
            True,
            total > float(line) if selection.lower() == "over" else total < float(line),
            {
                "home_expected_corners": home_rate,
                "away_expected_corners": away_rate,
                "total_expected_corners": total,
            },
        )
    return False, False, {"reason": "no_versioned_direction_rule_for_market"}


def _rebuild_current(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    fixture = entries[0]["parsed"]["fixture"]
    ordered = sorted(
        entries,
        key=lambda item: (
            item["parsed"]["collected_at"],
            item["parsed"]["source_url"],
            item["raw_response_sha256"],
        ),
    )
    confirmed = [
        item["parsed"]["confirmed_lineups"]
        for item in ordered
        if item["parsed"]["confirmed_lineups"] is not None
    ]
    canonical_lineups = [
        {side: sorted(lineup[side]) for side in ("home", "away")}
        for lineup in confirmed
    ]
    if canonical_lineups and any(
        item != canonical_lineups[0] for item in canonical_lineups[1:]
    ):
        raise FundamentalEvidenceError("confirmed lineup sources conflict")
    merged_lineup = canonical_lineups[0] if canonical_lineups else None
    if merged_lineup is not None:
        for item in ordered:
            attack = item["parsed"].get("attack_configuration")
            if not isinstance(attack, Mapping):
                continue
            for side in ("home", "away"):
                if not set(attack[side]["recognized_attackers"]).issubset(
                    set(merged_lineup[side])
                ):
                    raise FundamentalEvidenceError(
                        "recognized attackers conflict with the confirmed lineup"
                    )
    requests: dict[str, dict[str, Any]] = {}
    for item in ordered:
        for request in item["parsed"]["candidate_support_requests"]:
            key = request["support_key"]
            if key in requests and requests[key] != request:
                raise FundamentalEvidenceError(
                    "candidate support request identity conflicts"
                )
            requests[key] = request
    candidate_support: dict[str, dict[str, Any]] = {}
    for key, request in sorted(requests.items()):
        evaluations = []
        for item in ordered:
            available, supported, metrics = _directional_support(
                item["parsed"], request
            )
            if available:
                evaluations.append(
                    {
                        "source_sha256": item["raw_response_sha256"],
                        "supported": supported,
                        "metrics": metrics,
                    }
                )
        directionally_supported = bool(evaluations) and all(
            item["supported"] for item in evaluations
        )
        candidate_support[key] = {
            **deepcopy(request),
            "rule_version": SUPPORT_RULE_VERSION,
            "directionally_supported": directionally_supported,
            "formal_gate_eligible": False,
            "release_status": "shadow_only_pending_forward_validation",
            "source_evaluations": evaluations,
            "reason": (
                "direction_supported_in_shadow"
                if directionally_supported
                else "direction_not_supported_or_evidence_unavailable"
            ),
        }
    availability = {
        field: any(item["parsed"]["availability_claims"][field] for item in ordered)
        for field in AVAILABILITY_CLAIM_FIELDS
    }
    availability["confirmed_lineups_available"] = merged_lineup is not None
    value: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "artifact_type": "soccer_replayable_fundamental_evidence",
        "parser_version": PARSER_VERSION,
        "fixture": fixture,
        "generated_at": max(item["parsed"]["collected_at"] for item in ordered),
        "sources": ordered,
        "availability_claims": availability,
        "confirmed_lineup_consensus": merged_lineup,
        "candidate_support_rule_version": SUPPORT_RULE_VERSION,
        "candidate_support": candidate_support,
        "replay_policy": (
            "reparse_sources_fail_closed_on_lineup_conflict_and_recompute_directional_support"
        ),
    }
    value["evidence_hash"] = _hash_json(value)
    return value


def _rebuild(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        raise FundamentalEvidenceError("fundamental evidence has no sources")
    fixture = entries[0]["parsed"]["fixture"]
    if any(item["parsed"]["fixture"] != fixture for item in entries):
        raise FundamentalEvidenceError("fundamental sources bind different fixtures")
    schemas = {item["parsed"].get("schema_version") for item in entries}
    if len(schemas) != 1:
        raise FundamentalEvidenceError("fundamental evidence cannot mix source schemas")
    if schemas == {RAW_SCHEMA_VERSION}:
        return _rebuild_current(entries)
    if schemas != {PREVIOUS_RAW_SCHEMA_VERSION}:
        raise FundamentalEvidenceError("fundamental source schema is unsupported")
    ordered = sorted(
        entries,
        key=lambda item: (
            item["parsed"]["collected_at"],
            item["parsed"]["source_url"],
            item["raw_response_sha256"],
        ),
    )
    merged_claims = {
        field: any(item["parsed"]["derived_claims"][field] for item in ordered)
        for field in CLAIM_FIELDS
    }
    value: dict[str, Any] = {
        "schema_version": PREVIOUS_EVIDENCE_SCHEMA_VERSION,
        "artifact_type": "soccer_replayable_fundamental_evidence",
        "parser_version": PREVIOUS_PARSER_VERSION,
        "fixture": fixture,
        "generated_at": max(item["parsed"]["collected_at"] for item in ordered),
        "sources": ordered,
        "derived_claims": merged_claims,
        "replay_policy": "reparse_every_content_addressed_fundamental_source",
    }
    value["evidence_hash"] = _hash_json(value)
    return value


def build_evidence(
    source_files: Sequence[str | Path], *, output_dir: str | Path
) -> tuple[Path, dict[str, Any]]:
    if not source_files:
        raise FundamentalEvidenceError("at least one source file is required")
    target = Path(output_dir).resolve()
    raw_dir = target / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for source in source_files:
        raw = Path(source).resolve().read_bytes()
        digest = _hash_bytes(raw).split(":", 1)[1]
        relative = f"raw/{digest}.json"
        raw_path = target / relative
        if raw_path.exists() and raw_path.read_bytes() != raw:
            raise FundamentalEvidenceError(
                "content-addressed fundamental source collision"
            )
        if not raw_path.exists():
            raw_path.write_bytes(raw)
        entries.append(_source_entry(raw, relative))
    evidence = _rebuild(entries)
    output = target / f"{evidence['fixture']['match_id']}-fundamental-evidence.json"
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if output.exists():
        if validate_evidence_file(output) != evidence:
            raise FundamentalEvidenceError(
                "different fundamental evidence already exists"
            )
    else:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
    return output, evidence


def validate_evidence_file(path: str | Path) -> dict[str, Any]:
    evidence_path = Path(path).resolve()
    try:
        value = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FundamentalEvidenceError(
            "fundamental evidence is unavailable or invalid"
        ) from exc
    if not isinstance(value, Mapping) or value.get("schema_version") not in {
        PREVIOUS_EVIDENCE_SCHEMA_VERSION,
        EVIDENCE_SCHEMA_VERSION,
    }:
        raise FundamentalEvidenceError(
            "fundamental evidence schema_version is unsupported"
        )
    supplied = value.get("evidence_hash")
    without_hash = dict(value)
    without_hash.pop("evidence_hash", None)
    if supplied != _hash_json(without_hash):
        raise FundamentalEvidenceError("fundamental evidence hash is invalid")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise FundamentalEvidenceError("fundamental evidence sources are missing")
    replayed = []
    raw_root = (evidence_path.parent / "raw").resolve()
    for source in sources:
        if not isinstance(source, Mapping):
            raise FundamentalEvidenceError("fundamental evidence source is invalid")
        relative = Path(str(source.get("raw_response_path") or ""))
        if (
            relative.is_absolute()
            or relative.parts[:1] != ("raw",)
            or ".." in relative.parts
        ):
            raise FundamentalEvidenceError("fundamental raw source path is unsafe")
        raw_path = (evidence_path.parent / relative).resolve()
        try:
            raw_path.relative_to(raw_root)
            raw = raw_path.read_bytes()
        except (OSError, ValueError) as exc:
            raise FundamentalEvidenceError(
                "fundamental raw source is unavailable"
            ) from exc
        digest = _hash_bytes(raw).split(":", 1)[1]
        expected = _source_entry(raw, f"raw/{digest}.json")
        if dict(source) != expected:
            raise FundamentalEvidenceError("fundamental source replay does not match")
        replayed.append(expected)
    rebuilt = _rebuild(replayed)
    if rebuilt != value:
        raise FundamentalEvidenceError("fundamental evidence bundle does not replay")
    return dict(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--source-file", action="append", required=True)
    build.add_argument("--output-dir", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--evidence", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "build":
            path, artifact = build_evidence(
                args.source_file, output_dir=args.output_dir
            )
        else:
            path = Path(args.evidence).resolve()
            artifact = validate_evidence_file(path)
        print(
            json.dumps(
                {"ok": True, "path": str(path), "artifact": artifact},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (FundamentalEvidenceError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

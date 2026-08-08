#!/usr/bin/env python3
"""Build replayable, source-bound fixture fundamentals and guardrail claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

RAW_SCHEMA_VERSION = "visible-fundamental-snapshot/1.0.0"
EVIDENCE_SCHEMA_VERSION = "fundamental-evidence/1.0.0"
PARSER_VERSION = "visible-fundamental-parser/1.0.0"
CLAIM_FIELDS = (
    "lineup_confirmed",
    "fundamental_supported",
    "chance_quality_supported",
    "attack_configuration_supported",
    "opponent_tail_risk_checked",
    "corner_profile_supported",
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
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != RAW_SCHEMA_VERSION
    ):
        raise FundamentalEvidenceError(
            "fundamental snapshot schema_version is unsupported"
        )
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
    return {
        "schema_version": RAW_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
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
    }


def _source_entry(raw: bytes, relative_path: str) -> dict[str, Any]:
    return {
        "raw_response_path": relative_path,
        "raw_response_sha256": _hash_bytes(raw),
        "raw_response_bytes": len(raw),
        "parsed": parse_snapshot(raw),
    }


def _rebuild(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        raise FundamentalEvidenceError("fundamental evidence has no sources")
    fixture = entries[0]["parsed"]["fixture"]
    if any(item["parsed"]["fixture"] != fixture for item in entries):
        raise FundamentalEvidenceError("fundamental sources bind different fixtures")
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
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "artifact_type": "soccer_replayable_fundamental_evidence",
        "parser_version": PARSER_VERSION,
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
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != EVIDENCE_SCHEMA_VERSION
    ):
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

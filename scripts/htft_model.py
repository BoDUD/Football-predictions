#!/usr/bin/env python3
"""Train and evaluate a dedicated half-time/full-time football model.

The model fits three deterministic Dixon-Coles components from one league's
history: half-time goals, second-half goals, and full-time goals.  The
production seed applies a smoothed historical nine-cell association lift to
the fixture's half-time and full-time marginals.  IPF (iterative proportional
fitting) then aligns that joint exactly to both marginals.  The half-time plus
second-half score convolution remains available only as an explicit
experiment.

Optional current-market marginals are deliberately opt-in.  They must be
complete, already de-vigged, timestamped, and source-labelled so an anchored
prediction cannot be confused with the score-model-only baseline.

Only the Python standard library and the adjacent ``score_model.py`` module
are required.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # Works when imported from the repository root.
    from scripts import score_model
except ImportError:  # Works when invoked directly as scripts/htft_model.py.
    import score_model  # type: ignore[no-redef]


MODEL_ARTIFACT_TYPE = "soccer_htft_model"
PREDICTION_ARTIFACT_TYPE = "soccer_htft_prediction"
BACKTEST_ARTIFACT_TYPE = "soccer_htft_backtest"
MODEL_SCHEMA_VERSION = "1.0.0"
PREDICTION_SCHEMA_VERSION = "1.0.0"
BACKTEST_SCHEMA_VERSION = "1.0.0"
MODEL_VERSION = "htft-dixon-coles-ipf/1.0.0"
BACKTEST_LOG_LOSS_FLOOR = 1e-15
RESULTS = ("home", "draw", "away")
RESULT_CODES = {"home": "H", "draw": "D", "away": "A"}
HTFT_CLASSES = tuple(
    f"{half_time}_{full_time}" for half_time in RESULTS for full_time in RESULTS
)
REQUIRED_COLUMNS = {
    "date",
    "home_team",
    "away_team",
    "home_goals",
    "away_goals",
    "half_home_goals",
    "half_away_goals",
}


class HTFTModelError(ValueError):
    """Raised when HT/FT data, artifacts, or predictions are unsafe."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sha256_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTFTModelError("artifact contains non-canonical values") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _without_generated_at(value: Any) -> Any:
    """Remove wall-clock metadata recursively from a model hash payload."""

    if isinstance(value, Mapping):
        return {
            key: _without_generated_at(item)
            for key, item in value.items()
            if key != "generated_at"
        }
    if isinstance(value, list):
        return [_without_generated_at(item) for item in value]
    return value


def calculate_model_hash(model: Mapping[str, Any]) -> str:
    payload = dict(model)
    payload.pop("model_hash", None)
    return _sha256_json(_without_generated_at(payload))


def calculate_prediction_hash(prediction: Mapping[str, Any]) -> str:
    payload = dict(prediction)
    payload.pop("prediction_hash", None)
    return _sha256_json(payload)


def calculate_backtest_hash(backtest: Mapping[str, Any]) -> str:
    payload = dict(backtest)
    payload.pop("backtest_hash", None)
    return _sha256_json(payload)


def _require_finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise HTFTModelError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise HTFTModelError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise HTFTModelError(f"{name} must be finite")
    return result


def _require_integer(value: Any, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HTFTModelError(f"{name} must be an integer >= {minimum}")
    return value


def _parse_iso_date(raw: Any, name: str) -> date:
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise HTFTModelError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTFTModelError(f"{name} must be a valid ISO date") from exc


def _parse_match_date(raw: str, row_number: int) -> date:
    value = (raw or "").strip()
    if not value:
        raise HTFTModelError(f"row {row_number}: date is required")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise HTFTModelError(
                f"row {row_number}: date must be a valid ISO date"
            ) from exc
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTFTModelError(f"row {row_number}: date must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HTFTModelError(
            f"row {row_number}: datetime date values need an explicit offset"
        )
    return parsed.astimezone(timezone.utc).date()


def _parse_aware_datetime(raw: Any, name: str) -> tuple[datetime, str]:
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTFTModelError(f"{name} must be an ISO-8601 datetime") from exc
    else:
        raise HTFTModelError(f"{name} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HTFTModelError(f"{name} needs an explicit UTC offset")
    normalized = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    canonical = normalized.isoformat(timespec=timespec).replace("+00:00", "Z")
    return normalized, canonical


def _parse_goal(raw: str, field: str, row_number: int) -> int:
    value = (raw or "").strip()
    if not re.fullmatch(r"\d+", value):
        raise HTFTModelError(
            f"row {row_number}: {field} must be a non-negative integer"
        )
    result = int(value)
    if result > 99:
        raise HTFTModelError(f"row {row_number}: {field} is implausibly large")
    return result


def load_training_csv(path: str | Path) -> list[dict[str, Any]]:
    """Load finished matches with a strictly valid half-time/full-time score."""

    source = Path(path)
    try:
        handle = source.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise HTFTModelError(f"cannot read training CSV: {source}") from exc

    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise HTFTModelError("training CSV has no header")
        missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames))
        if missing:
            raise HTFTModelError("training CSV missing columns: " + ", ".join(missing))
        has_league_key = "league_key" in reader.fieldnames

        records: list[dict[str, Any]] = []
        fixtures: dict[tuple[date, str, str], tuple[int, int, int, int]] = {}
        for row_number, row in enumerate(reader, start=2):
            home_team = (row.get("home_team") or "").strip()
            away_team = (row.get("away_team") or "").strip()
            if not home_team or not away_team:
                raise HTFTModelError(
                    f"row {row_number}: home_team and away_team are required"
                )
            if home_team == away_team:
                raise HTFTModelError(
                    f"row {row_number}: home_team and away_team must differ"
                )
            match_date = _parse_match_date(row.get("date") or "", row_number)
            home_goals = _parse_goal(
                row.get("home_goals") or "", "home_goals", row_number
            )
            away_goals = _parse_goal(
                row.get("away_goals") or "", "away_goals", row_number
            )
            half_home_goals = _parse_goal(
                row.get("half_home_goals") or "", "half_home_goals", row_number
            )
            half_away_goals = _parse_goal(
                row.get("half_away_goals") or "", "half_away_goals", row_number
            )
            if half_home_goals > home_goals or half_away_goals > away_goals:
                raise HTFTModelError(
                    f"row {row_number}: half-time goals cannot exceed full-time goals"
                )
            competition_key = (row.get("league_key") or "").strip()
            if has_league_key and not competition_key:
                raise HTFTModelError(
                    f"row {row_number}: league_key is required when the column exists"
                )
            fixture_key = (match_date, home_team, away_team)
            score = (
                home_goals,
                away_goals,
                half_home_goals,
                half_away_goals,
            )
            if fixture_key in fixtures:
                status = (
                    "duplicate" if fixtures[fixture_key] == score else "conflicting"
                )
                raise HTFTModelError(
                    f"row {row_number}: {status} HT/FT score for "
                    f"{match_date.isoformat()} {home_team} vs {away_team}"
                )
            fixtures[fixture_key] = score
            records.append(
                {
                    "date": match_date,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_goals": home_goals,
                    "away_goals": away_goals,
                    "half_home_goals": half_home_goals,
                    "half_away_goals": half_away_goals,
                    "competition_key": competition_key or None,
                }
            )

    if len(records) < 2:
        raise HTFTModelError("training CSV needs at least two matches")
    teams = {row["home_team"] for row in records} | {
        row["away_team"] for row in records
    }
    if len(teams) < 2:
        raise HTFTModelError("training CSV needs at least two teams")
    adjacency = {team: set() for team in teams}
    for row in records:
        adjacency[row["home_team"]].add(row["away_team"])
        adjacency[row["away_team"]].add(row["home_team"])
    connected: set[str] = set()
    pending = [min(teams)]
    while pending:
        team = pending.pop()
        if team in connected:
            continue
        connected.add(team)
        pending.extend(sorted(adjacency[team] - connected, reverse=True))
    if connected != teams:
        raise HTFTModelError(
            "training fixture graph is disconnected; unrelated team component: "
            + ", ".join(sorted(teams - connected))
        )
    competition_keys = {
        row["competition_key"] for row in records if row["competition_key"]
    }
    if len(competition_keys) > 1:
        raise HTFTModelError(
            "training CSV mixes multiple league_key values: "
            + ", ".join(sorted(competition_keys))
        )
    return records


def _resolve_competition_key(
    records: Sequence[dict[str, Any]], explicit: str | None
) -> str:
    observed = {
        row.get("competition_key")
        for row in records
        if isinstance(row.get("competition_key"), str)
        and row["competition_key"].strip()
    }
    explicit_value = explicit.strip() if isinstance(explicit, str) else ""
    if len(observed) > 1:
        raise HTFTModelError("training rows contain multiple competition keys")
    observed_value = next(iter(observed), "")
    if explicit_value and observed_value and explicit_value != observed_value:
        raise HTFTModelError("explicit competition_key does not match CSV league_key")
    resolved = explicit_value or observed_value
    if not resolved:
        raise HTFTModelError(
            "single-league training requires CSV league_key or explicit competition_key"
        )
    for row in records:
        row["competition_key"] = resolved
    return resolved


def _canonical_training_rows(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = [
        {
            "date": row["date"].isoformat(),
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "home_goals": int(row["home_goals"]),
            "away_goals": int(row["away_goals"]),
            "half_home_goals": int(row["half_home_goals"]),
            "half_away_goals": int(row["half_away_goals"]),
            "competition_key": row.get("competition_key"),
        }
        for row in records
    ]
    return sorted(
        rows,
        key=lambda row: (
            row["date"],
            row["home_team"],
            row["away_team"],
            row["home_goals"],
            row["away_goals"],
            row["half_home_goals"],
            row["half_away_goals"],
            row.get("competition_key") or "",
        ),
    )


def _fit_empirical_association(
    records: Sequence[Mapping[str, Any]],
    *,
    smoothing_alpha: float,
    half_life_days: float | None = None,
) -> dict[str, Any]:
    """Estimate a smoothed, optionally time-decayed HT/FT association seed."""

    alpha = _require_finite(smoothing_alpha, "association_smoothing_alpha")
    if alpha <= 0.0:
        raise HTFTModelError("association_smoothing_alpha must be positive")
    if half_life_days is not None:
        half_life_days = _require_finite(half_life_days, "association_half_life_days")
        if half_life_days <= 0.0:
            raise HTFTModelError("association_half_life_days must be positive")
    reference_date = max(row["date"] for row in records)
    counts = {
        code: 0 for code in ("HH", "HD", "HA", "DH", "DD", "DA", "AH", "AD", "AA")
    }
    weighted_counts = {code: 0.0 for code in counts}
    for row in records:
        half = RESULT_CODES[_result(row["half_home_goals"], row["half_away_goals"])]
        full = RESULT_CODES[_result(row["home_goals"], row["away_goals"])]
        code = half + full
        counts[code] += 1
        if half_life_days is None:
            weight = 1.0
        else:
            age_days = max(0, (reference_date - row["date"]).days)
            weight = math.exp(-math.log(2.0) * age_days / half_life_days)
        weighted_counts[code] += weight
    effective_sample_weight = math.fsum(weighted_counts.values())
    denominator = effective_sample_weight + alpha * 9.0
    seed_joint = [
        [
            (weighted_counts[RESULT_CODES[half] + RESULT_CODES[full]] + alpha)
            / denominator
            for full in RESULTS
        ]
        for half in RESULTS
    ]
    _validate_joint(seed_joint)
    return {
        "method": "dirichlet_smoothed_empirical_htft_joint",
        "smoothing_alpha": alpha,
        "sample_count": len(records),
        "counts": counts,
        "weighted_counts": weighted_counts,
        "effective_sample_weight": effective_sample_weight,
        "time_decay": {
            "mode": "none" if half_life_days is None else "exponential_half_life",
            "half_life_days": half_life_days,
            "reference_date": reference_date.isoformat(),
            "weight_formula": (
                "uniform_weight_1"
                if half_life_days is None
                else "exp(-log(2) * age_days / half_life_days)"
            ),
        },
        "seed_joint": seed_joint,
    }


def _write_component_csv(
    path: Path,
    records: Sequence[Mapping[str, Any]],
    component: str,
) -> None:
    if component not in {"half_time", "second_half", "full_time"}:
        raise HTFTModelError(f"unknown score component: {component}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "home_team", "away_team", "home_goals", "away_goals"])
        for row in records:
            if component == "half_time":
                home_goals = row["half_home_goals"]
                away_goals = row["half_away_goals"]
            elif component == "second_half":
                home_goals = row["home_goals"] - row["half_home_goals"]
                away_goals = row["away_goals"] - row["half_away_goals"]
            else:
                home_goals = row["home_goals"]
                away_goals = row["away_goals"]
            writer.writerow(
                [
                    row["date"].isoformat(),
                    row["home_team"],
                    row["away_team"],
                    home_goals,
                    away_goals,
                ]
            )


def _score_fit_config(
    *,
    half_life_days: float,
    iterations: int,
    learning_rate: float,
    regularization: float,
    rho_min: float,
    rho_max: float,
    rho_step: float,
) -> dict[str, Any]:
    return {
        "half_life_days": half_life_days,
        "iterations": iterations,
        "learning_rate": learning_rate,
        "regularization": regularization,
        "rho_grid": {
            "minimum": rho_min,
            "maximum": rho_max,
            "step": rho_step,
        },
    }


def fit_model(
    csv_path: str | Path,
    *,
    half_time_half_life_days: float = 730.0,
    second_half_half_life_days: float = 365.0,
    full_time_half_life_days: float = 365.0,
    iterations: int = 1200,
    learning_rate: float = 0.03,
    regularization: float = 0.02,
    rho_min: float = -0.20,
    rho_max: float = 0.20,
    rho_step: float = 0.01,
    ipf_tolerance: float = 1e-12,
    ipf_max_iterations: int = 1000,
    association_smoothing_alpha: float = 0.5,
    association_power: float = 1.0,
    association_half_life_days: float | None = None,
    competition_key: str | None = None,
    dataset_manifest_hash: str | None = None,
) -> dict[str, Any]:
    """Fit one league-scoped HT/FT model from finished historical matches."""

    ipf_tolerance = _require_finite(ipf_tolerance, "ipf_tolerance")
    if not 0.0 < ipf_tolerance < 1.0:
        raise HTFTModelError("ipf_tolerance must be between zero and one")
    if (
        isinstance(ipf_max_iterations, bool)
        or int(ipf_max_iterations) != ipf_max_iterations
        or ipf_max_iterations < 1
    ):
        raise HTFTModelError("ipf_max_iterations must be a positive integer")
    ipf_max_iterations = int(ipf_max_iterations)

    records = sorted(
        load_training_csv(csv_path),
        key=lambda row: (
            row["date"],
            row["home_team"],
            row["away_team"],
            row["home_goals"],
            row["away_goals"],
        ),
    )
    competition_key = _resolve_competition_key(records, competition_key)
    if dataset_manifest_hash is not None and (
        not isinstance(dataset_manifest_hash, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", dataset_manifest_hash)
    ):
        raise HTFTModelError("dataset_manifest_hash must be a SHA-256 hash")
    teams = sorted(
        {row["home_team"] for row in records} | {row["away_team"] for row in records}
    )
    component_models: dict[str, Any] = {}
    component_half_lives = {
        "half_time": half_time_half_life_days,
        "second_half": second_half_half_life_days,
        "full_time": full_time_half_life_days,
    }
    with tempfile.TemporaryDirectory(prefix="soccer-htft-fit-") as temporary:
        temporary_path = Path(temporary)
        for component in ("half_time", "second_half", "full_time"):
            component_path = temporary_path / f"{component}.csv"
            _write_component_csv(component_path, records, component)
            try:
                component_models[component] = score_model.fit_model(
                    component_path,
                    half_life_days=component_half_lives[component],
                    iterations=iterations,
                    learning_rate=learning_rate,
                    regularization=regularization,
                    rho_min=rho_min,
                    rho_max=rho_max,
                    rho_step=rho_step,
                )
            except score_model.ScoreModelError as exc:
                raise HTFTModelError(
                    f"cannot fit {component} score component: {exc}"
                ) from exc

    reference_date = max(row["date"] for row in records)
    association_power = _require_finite(association_power, "association_power")
    if association_power < 0.0:
        raise HTFTModelError("association_power cannot be negative")
    association = _fit_empirical_association(
        records,
        smoothing_alpha=association_smoothing_alpha,
        half_life_days=association_half_life_days,
    )
    association["power"] = association_power
    association_joint = association["seed_joint"]
    association_rows, association_columns = _matrix_marginals(association_joint)
    association["half_time_marginal"] = association_rows
    association["full_time_marginal"] = association_columns
    association["lift"] = [
        [
            association_joint[row][column]
            / (association_rows[RESULTS[row]] * association_columns[RESULTS[column]])
            for column in range(3)
        ]
        for row in range(3)
    ]
    model: dict[str, Any] = {
        "artifact_type": MODEL_ARTIFACT_TYPE,
        "schema_version": MODEL_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at": _utc_now(),
        "training": {
            "source_data_hash": _sha256_json(_canonical_training_rows(records)),
            "match_count": len(records),
            "team_count": len(teams),
            "start_date": min(row["date"] for row in records).isoformat(),
            "end_date": reference_date.isoformat(),
            "scope": "single_league",
            "competition_key": competition_key,
            "dataset_manifest_hash": dataset_manifest_hash,
            "component_source_data_hashes": {
                name: component_models[name]["training"]["source_data_hash"]
                for name in ("half_time", "second_half", "full_time")
            },
        },
        "config": {
            "score_models": {
                name: _score_fit_config(
                    half_life_days=component_half_lives[name],
                    iterations=int(iterations),
                    learning_rate=learning_rate,
                    regularization=regularization,
                    rho_min=rho_min,
                    rho_max=rho_max,
                    rho_step=rho_step,
                )
                for name in ("half_time", "second_half", "full_time")
            },
            "ipf": {
                "tolerance": ipf_tolerance,
                "max_iterations": ipf_max_iterations,
            },
        },
        "components": component_models,
        "empirical_association": association,
        "construction": {
            "default_seed": "empirical_association_ipf",
            "validated_configuration": {
                "half_time_half_life_days": half_time_half_life_days,
                "full_time_half_life_days": full_time_half_life_days,
                "association_power": association_power,
            },
            "experimental_seed": "independent_half_time_and_second_half_score_convolution",
            "marginal_alignment": "iterative_proportional_fitting",
            "half_time_target": "half_time_component_1x2",
            "full_time_target": "full_time_component_1x2",
            "class_order": list(HTFT_CLASSES),
        },
    }
    model["model_hash"] = calculate_model_hash(model)
    validate_model(model)
    return model


def validate_model(model: Mapping[str, Any], *, verify_hash: bool = True) -> None:
    if not isinstance(model, Mapping):
        raise HTFTModelError("model must be a JSON object")
    if model.get("artifact_type") != MODEL_ARTIFACT_TYPE:
        raise HTFTModelError("unexpected model artifact_type")
    if model.get("schema_version") != MODEL_SCHEMA_VERSION:
        raise HTFTModelError("unsupported model schema_version")
    if model.get("model_version") != MODEL_VERSION:
        raise HTFTModelError("unsupported model_version")
    generated_at, _ = _parse_aware_datetime(model.get("generated_at"), "generated_at")

    training = model.get("training")
    if not isinstance(training, Mapping):
        raise HTFTModelError("model training metadata is missing")
    source_hash = training.get("source_data_hash")
    if not isinstance(source_hash, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", source_hash
    ):
        raise HTFTModelError("training.source_data_hash must be a SHA-256 hash")
    match_count = _require_integer(
        training.get("match_count"), "training.match_count", minimum=2
    )
    team_count = _require_integer(
        training.get("team_count"), "training.team_count", minimum=2
    )
    start_date = _parse_iso_date(training.get("start_date"), "training.start_date")
    end_date = _parse_iso_date(training.get("end_date"), "training.end_date")
    if start_date > end_date:
        raise HTFTModelError("training.start_date cannot be after training.end_date")
    if end_date > generated_at.date():
        raise HTFTModelError("training.end_date cannot be after model generated_at")
    if training.get("scope") != "single_league":
        raise HTFTModelError("training.scope must be single_league")

    config = model.get("config")
    if not isinstance(config, Mapping):
        raise HTFTModelError("model config is missing")
    score_configs = config.get("score_models")
    ipf_config = config.get("ipf")
    if (
        not isinstance(score_configs, Mapping)
        or set(score_configs) != {"half_time", "second_half", "full_time"}
        or not isinstance(ipf_config, Mapping)
    ):
        raise HTFTModelError("model score_models and ipf configs are required")
    ipf_tolerance = _require_finite(ipf_config.get("tolerance"), "config.ipf.tolerance")
    if not 0.0 < ipf_tolerance < 1.0:
        raise HTFTModelError("config.ipf.tolerance must be between zero and one")
    _require_integer(ipf_config.get("max_iterations"), "config.ipf.max_iterations")

    components = model.get("components")
    if not isinstance(components, Mapping) or set(components) != {
        "half_time",
        "second_half",
        "full_time",
    }:
        raise HTFTModelError("model must contain exactly three score components")
    component_hashes = training.get("component_source_data_hashes")
    if not isinstance(component_hashes, Mapping) or set(component_hashes) != set(
        components
    ):
        raise HTFTModelError("training.component_source_data_hashes is invalid")

    team_sets: list[set[str]] = []
    for name in ("half_time", "second_half", "full_time"):
        component = components[name]
        try:
            score_model.validate_model(component)
        except score_model.ScoreModelError as exc:
            raise HTFTModelError(f"invalid {name} component: {exc}") from exc
        component_training = component["training"]
        for field in ("match_count", "team_count", "start_date", "end_date"):
            if component_training[field] != training[field]:
                raise HTFTModelError(
                    f"{name} training.{field} does not match HT/FT training metadata"
                )
        if component_hashes[name] != component_training["source_data_hash"]:
            raise HTFTModelError(f"{name} source data hash metadata does not match")
        if component["config"] != score_configs[name]:
            raise HTFTModelError(f"{name} score config does not match HT/FT config")
        component_generated_at, _ = _parse_aware_datetime(
            component["generated_at"], f"components.{name}.generated_at"
        )
        if component_generated_at > generated_at:
            raise HTFTModelError(
                f"components.{name}.generated_at cannot be after model.generated_at"
            )
        team_sets.append(set(component["parameters"]["attack"]))
    if any(team_set != team_sets[0] for team_set in team_sets[1:]):
        raise HTFTModelError("score components have different team sets")
    if len(team_sets[0]) != team_count:
        raise HTFTModelError("training.team_count does not match score components")

    competition_key = training.get("competition_key")
    if not isinstance(competition_key, str) or not competition_key.strip():
        raise HTFTModelError("training.competition_key is required")
    manifest_hash = training.get("dataset_manifest_hash")
    if manifest_hash is not None and (
        not isinstance(manifest_hash, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_hash)
    ):
        raise HTFTModelError("training.dataset_manifest_hash must be a SHA-256 hash")
    association = model.get("empirical_association")
    if not isinstance(association, Mapping):
        raise HTFTModelError("model empirical_association is missing")
    if association.get("method") != "dirichlet_smoothed_empirical_htft_joint":
        raise HTFTModelError("empirical_association method is unsupported")
    alpha = _require_finite(
        association.get("smoothing_alpha"),
        "empirical_association.smoothing_alpha",
    )
    power = _require_finite(association.get("power"), "empirical_association.power")
    if alpha <= 0.0 or power < 0.0:
        raise HTFTModelError("empirical association alpha/power is invalid")
    if association.get("sample_count") != match_count:
        raise HTFTModelError("empirical association sample_count does not match")
    counts = association.get("counts")
    expected_codes = {"HH", "HD", "HA", "DH", "DD", "DA", "AH", "AD", "AA"}
    if not isinstance(counts, Mapping) or set(counts) != expected_codes:
        raise HTFTModelError("empirical association counts are invalid")
    if (
        any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        )
        or sum(counts.values()) != match_count
    ):
        raise HTFTModelError("empirical association counts do not match training")
    time_decay = association.get("time_decay")
    weighted_counts = association.get("weighted_counts")
    effective_sample_weight = association.get("effective_sample_weight")
    if time_decay is None:
        # Immutable artifacts created before time-decay support used uniform
        # weights and did not persist weighting metadata.
        expected_weighted_counts = {
            code: float(value) for code, value in counts.items()
        }
        expected_effective_weight = float(match_count)
    else:
        if not isinstance(time_decay, Mapping):
            raise HTFTModelError("empirical association time_decay is invalid")
        mode = time_decay.get("mode")
        reference = _parse_iso_date(
            time_decay.get("reference_date"),
            "empirical_association.time_decay.reference_date",
        )
        if reference != end_date:
            raise HTFTModelError(
                "empirical association decay reference must equal training.end_date"
            )
        if mode == "none":
            if time_decay.get("half_life_days") is not None or (
                time_decay.get("weight_formula") != "uniform_weight_1"
            ):
                raise HTFTModelError(
                    "empirical association uniform decay metadata is invalid"
                )
        elif mode == "exponential_half_life":
            decay_half_life = _require_finite(
                time_decay.get("half_life_days"),
                "empirical_association.time_decay.half_life_days",
            )
            if decay_half_life <= 0.0 or time_decay.get("weight_formula") != (
                "exp(-log(2) * age_days / half_life_days)"
            ):
                raise HTFTModelError(
                    "empirical association half-life metadata is invalid"
                )
        else:
            raise HTFTModelError("empirical association decay mode is unsupported")
        if (
            not isinstance(weighted_counts, Mapping)
            or set(weighted_counts) != expected_codes
        ):
            raise HTFTModelError("empirical association weighted_counts are invalid")
        expected_weighted_counts = {
            code: _require_finite(
                weighted_counts[code],
                f"empirical_association.weighted_counts.{code}",
            )
            for code in expected_codes
        }
        if any(value < 0.0 for value in expected_weighted_counts.values()):
            raise HTFTModelError(
                "empirical association weighted_counts cannot be negative"
            )
        expected_effective_weight = _require_finite(
            effective_sample_weight,
            "empirical_association.effective_sample_weight",
        )
        if (
            expected_effective_weight <= 0.0
            or expected_effective_weight > match_count + 1e-9
            or abs(
                math.fsum(expected_weighted_counts.values()) - expected_effective_weight
            )
            > 1e-10
        ):
            raise HTFTModelError(
                "empirical association effective sample weight is invalid"
            )
        if (
            mode == "exponential_half_life"
            and start_date < end_date
            and expected_effective_weight >= match_count - 1e-12
        ):
            raise HTFTModelError(
                "time-decayed association must downweight older training rows"
            )
        if mode == "none" and (
            abs(expected_effective_weight - match_count) > 1e-12
            or any(
                abs(expected_weighted_counts[code] - counts[code]) > 1e-12
                for code in expected_codes
            )
        ):
            raise HTFTModelError(
                "empirical association uniform weights do not match raw counts"
            )
    seed_joint = association.get("seed_joint")
    _validate_joint(seed_joint)
    expected_denominator = expected_effective_weight + alpha * 9.0
    expected_seed_joint = [
        [
            (expected_weighted_counts[RESULT_CODES[half] + RESULT_CODES[full]] + alpha)
            / expected_denominator
            for full in RESULTS
        ]
        for half in RESULTS
    ]
    if any(
        abs(seed_joint[row][column] - expected_seed_joint[row][column]) > 1e-12
        for row in range(3)
        for column in range(3)
    ):
        raise HTFTModelError(
            "empirical association seed does not match counts and smoothing"
        )
    half_association = _validated_marginal(
        association.get("half_time_marginal"),
        "empirical_association.half_time_marginal",
        require_positive=True,
    )
    full_association = _validated_marginal(
        association.get("full_time_marginal"),
        "empirical_association.full_time_marginal",
        require_positive=True,
    )
    derived_half_association, derived_full_association = _matrix_marginals(seed_joint)
    if any(
        abs(half_association[result] - derived_half_association[result]) > 1e-12
        or abs(full_association[result] - derived_full_association[result]) > 1e-12
        for result in RESULTS
    ):
        raise HTFTModelError(
            "empirical association marginals do not match the seed joint"
        )
    lift = association.get("lift")
    if (
        not isinstance(lift, list)
        or len(lift) != 3
        or any(not isinstance(row, list) or len(row) != 3 for row in lift)
    ):
        raise HTFTModelError("empirical association lift must be 3x3")
    if any(
        _require_finite(value, "empirical_association.lift") <= 0.0
        for row in lift
        for value in row
    ):
        raise HTFTModelError("empirical association lift must be positive")
    expected_lift = [
        [
            seed_joint[row][column]
            / (
                derived_half_association[RESULTS[row]]
                * derived_full_association[RESULTS[column]]
            )
            for column in range(3)
        ]
        for row in range(3)
    ]
    if any(
        abs(lift[row][column] - expected_lift[row][column]) > 1e-12
        for row in range(3)
        for column in range(3)
    ):
        raise HTFTModelError("empirical association lift does not match its seed joint")

    construction = model.get("construction")
    if not isinstance(construction, Mapping):
        raise HTFTModelError("model construction metadata is missing")
    if construction.get("default_seed") != "empirical_association_ipf":
        raise HTFTModelError("model default HT/FT seed is unsupported")
    if construction.get("class_order") != list(HTFT_CLASSES):
        raise HTFTModelError("model construction class_order is unsupported")
    validated_configuration = construction.get("validated_configuration")
    expected_validated_configuration = {
        "half_time_half_life_days": score_configs["half_time"]["half_life_days"],
        "full_time_half_life_days": score_configs["full_time"]["half_life_days"],
        "association_power": power,
    }
    if validated_configuration != expected_validated_configuration:
        raise HTFTModelError(
            "model validated configuration disagrees with fitted components"
        )
    if verify_hash:
        stored_hash = model.get("model_hash")
        if not isinstance(stored_hash, str) or stored_hash != calculate_model_hash(
            model
        ):
            raise HTFTModelError("model_hash does not match model contents")


def save_json(value: Mapping[str, Any], path: str | Path | None) -> None:
    encoded = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    if path is None:
        sys.stdout.write(encoded)
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(encoded, encoding="utf-8")


def load_model(path: str | Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTFTModelError(f"cannot read model JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise HTFTModelError("model JSON must contain an object")
    validate_model(raw)
    return raw


def _result(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"


def _result_index(home_goals: int, away_goals: int) -> int:
    return RESULTS.index(_result(home_goals, away_goals))


def build_raw_joint(
    half_time_matrix: Sequence[Sequence[float]],
    second_half_matrix: Sequence[Sequence[float]],
) -> list[list[float]]:
    """Convolve two score matrices into HT-result x FT-result probabilities."""

    score_model._validate_matrix(half_time_matrix)
    score_model._validate_matrix(second_half_matrix)
    buckets: list[list[list[float]]] = [[[] for _ in RESULTS] for _ in RESULTS]
    for half_home, half_row in enumerate(half_time_matrix):
        for half_away, half_probability in enumerate(half_row):
            half_index = _result_index(half_home, half_away)
            for second_home, second_row in enumerate(second_half_matrix):
                for second_away, second_probability in enumerate(second_row):
                    full_index = _result_index(
                        half_home + second_home,
                        half_away + second_away,
                    )
                    buckets[half_index][full_index].append(
                        half_probability * second_probability
                    )
    matrix = [
        [math.fsum(buckets[row][column]) for column in range(3)] for row in range(3)
    ]
    _validate_joint(matrix)
    return matrix


def _validate_joint(matrix: Sequence[Sequence[float]]) -> None:
    if not isinstance(matrix, Sequence) or len(matrix) != 3:
        raise HTFTModelError("HT/FT joint matrix must have three rows")
    total = 0.0
    for row_index, row in enumerate(matrix):
        if not isinstance(row, Sequence) or len(row) != 3:
            raise HTFTModelError("HT/FT joint matrix must be 3x3")
        for column_index, raw in enumerate(row):
            value = _require_finite(raw, f"joint[{row_index}][{column_index}]")
            if value < 0.0:
                raise HTFTModelError("HT/FT joint probabilities cannot be negative")
            total += value
    if abs(total - 1.0) > 1e-9:
        raise HTFTModelError("HT/FT joint probabilities must sum to one")


def _validated_marginal(
    values: Mapping[str, Any], name: str, *, require_positive: bool = False
) -> dict[str, float]:
    if not isinstance(values, Mapping) or set(values) != set(RESULTS):
        raise HTFTModelError(f"{name} must contain exactly home, draw, and away")
    result = {key: _require_finite(values[key], f"{name}.{key}") for key in RESULTS}
    if any(value < 0.0 for value in result.values()):
        raise HTFTModelError(f"{name} probabilities cannot be negative")
    if require_positive and any(value <= 0.0 for value in result.values()):
        raise HTFTModelError(f"{name} probabilities must be strictly positive")
    if abs(math.fsum(result.values()) - 1.0) > 1e-9:
        raise HTFTModelError(f"{name} probabilities must sum to one")
    return result


def iterative_proportional_fit(
    raw_joint: Sequence[Sequence[float]],
    half_time_marginal: Mapping[str, Any],
    full_time_marginal: Mapping[str, Any],
    *,
    tolerance: float = 1e-12,
    max_iterations: int = 1000,
) -> tuple[list[list[float]], dict[str, Any]]:
    """Rake a positive 3x3 joint to exact half/full-time 1X2 marginals."""

    _validate_joint(raw_joint)
    rows = _validated_marginal(
        half_time_marginal, "half_time_marginal", require_positive=True
    )
    columns = _validated_marginal(
        full_time_marginal, "full_time_marginal", require_positive=True
    )
    tolerance = _require_finite(tolerance, "tolerance")
    if not 0.0 < tolerance < 1.0:
        raise HTFTModelError("tolerance must be between zero and one")
    max_iterations = _require_integer(max_iterations, "max_iterations")
    matrix = [[float(value) for value in row] for row in raw_joint]
    if any(value <= 0.0 for row in matrix for value in row):
        raise HTFTModelError("IPF requires a strictly positive raw joint matrix")

    target_rows = [rows[result] for result in RESULTS]
    target_columns = [columns[result] for result in RESULTS]
    maximum_error = math.inf
    for iteration in range(1, max_iterations + 1):
        for row_index, target in enumerate(target_rows):
            current = math.fsum(matrix[row_index])
            if current <= 0.0:
                raise HTFTModelError("IPF encountered an empty row")
            factor = target / current
            matrix[row_index] = [value * factor for value in matrix[row_index]]
        for column_index, target in enumerate(target_columns):
            current = math.fsum(matrix[row][column_index] for row in range(3))
            if current <= 0.0:
                raise HTFTModelError("IPF encountered an empty column")
            factor = target / current
            for row_index in range(3):
                matrix[row_index][column_index] *= factor

        row_sums = [math.fsum(row) for row in matrix]
        column_sums = [
            math.fsum(matrix[row][column] for row in range(3)) for column in range(3)
        ]
        maximum_error = max(
            *(abs(actual - target) for actual, target in zip(row_sums, target_rows)),
            *(
                abs(actual - target)
                for actual, target in zip(column_sums, target_columns)
            ),
        )
        if maximum_error <= tolerance:
            break
    else:
        raise HTFTModelError("IPF did not converge within config.ipf.max_iterations")

    _validate_joint(matrix)
    return matrix, {
        "converged": True,
        "iterations": iteration,
        "tolerance": tolerance,
        "maximum_marginal_error": maximum_error,
    }


def _matrix_marginals(
    matrix: Sequence[Sequence[float]],
) -> tuple[dict[str, float], dict[str, float]]:
    _validate_joint(matrix)
    row_marginal = {
        result: math.fsum(matrix[index]) for index, result in enumerate(RESULTS)
    }
    column_marginal = {
        result: math.fsum(matrix[row][index] for row in range(3))
        for index, result in enumerate(RESULTS)
    }
    return row_marginal, column_marginal


def build_empirical_association_seed(
    association: Mapping[str, Any],
    half_time_marginal: Mapping[str, Any],
    full_time_marginal: Mapping[str, Any],
) -> list[list[float]]:
    """Combine predicted marginals with the training-only HT/FT association lift."""

    half = _validated_marginal(
        half_time_marginal, "half_time_marginal", require_positive=True
    )
    full = _validated_marginal(
        full_time_marginal, "full_time_marginal", require_positive=True
    )
    lift = association.get("lift") if isinstance(association, Mapping) else None
    power = _require_finite(
        association.get("power") if isinstance(association, Mapping) else None,
        "empirical_association.power",
    )
    if power < 0.0:
        raise HTFTModelError("empirical_association.power cannot be negative")
    if (
        not isinstance(lift, list)
        or len(lift) != 3
        or any(not isinstance(row, list) or len(row) != 3 for row in lift)
    ):
        raise HTFTModelError("empirical association lift must be 3x3")
    seed = [
        [
            half[half_result]
            * full[full_result]
            * _require_finite(
                lift[row_index][column_index],
                f"empirical_association.lift[{row_index}][{column_index}]",
            )
            ** power
            for column_index, full_result in enumerate(RESULTS)
        ]
        for row_index, half_result in enumerate(RESULTS)
    ]
    total = math.fsum(math.fsum(row) for row in seed)
    if not math.isfinite(total) or total <= 0.0:
        raise HTFTModelError("empirical association seed has no probability mass")
    normalized = [[value / total for value in row] for row in seed]
    _validate_joint(normalized)
    return normalized


def _component_prediction(
    component: Mapping[str, Any],
    home_team: str,
    away_team: str,
    *,
    max_goals: int,
    hard_max_goals: int,
    tail_tolerance: float,
    allow_large_tail: bool,
    unknown_team_policy: str,
) -> dict[str, Any]:
    try:
        home_rate, away_rate, warnings = score_model.expected_rates(
            component,
            home_team,
            away_team,
            unknown_team_policy=unknown_team_policy,
        )
        matrix, tail = score_model.build_score_matrix(
            home_rate,
            away_rate,
            component["parameters"]["rho"],
            max_goals=max_goals,
            hard_max_goals=hard_max_goals,
            tail_tolerance=tail_tolerance,
            allow_large_tail=allow_large_tail,
        )
    except score_model.ScoreModelError as exc:
        raise HTFTModelError(str(exc)) from exc
    if tail["raw_omitted_probability"] > 0.0:
        warnings.append(
            "finite score grid was normalized after retaining the reported tail audit"
        )
    return {
        "latent_rates": {"home": home_rate, "away": away_rate},
        "matrix": matrix,
        "one_x_two": score_model.aggregate_one_x_two(matrix),
        "tail_mass": tail,
        "warnings": warnings,
        "score_matrix_hash": _sha256_json(matrix),
    }


def _validate_anchor(
    anchor: Mapping[str, Any],
    name: str,
    *,
    generated_at: datetime,
    kickoff: datetime,
) -> tuple[dict[str, float], dict[str, Any]]:
    if not isinstance(anchor, Mapping) or set(anchor) != {
        "probabilities",
        "source",
        "captured_at",
        "de_vigged",
    }:
        raise HTFTModelError(
            f"{name} anchor must contain probabilities, source, captured_at, and de_vigged"
        )
    if anchor.get("de_vigged") is not True:
        raise HTFTModelError(f"{name} anchor must explicitly be de_vigged")
    source = anchor.get("source")
    if not isinstance(source, str) or not source.strip():
        raise HTFTModelError(f"{name} anchor.source is required")
    probabilities = _validated_marginal(
        anchor.get("probabilities"),
        f"{name} anchor.probabilities",
        require_positive=True,
    )
    captured_at, canonical_captured_at = _parse_aware_datetime(
        anchor.get("captured_at"), f"{name} anchor.captured_at"
    )
    if captured_at > generated_at:
        raise HTFTModelError(f"{name} anchor.captured_at cannot be after generated_at")
    if captured_at >= kickoff:
        raise HTFTModelError(f"{name} anchor.captured_at must be before kickoff")
    return probabilities, {
        "origin": "external_de_vigged_anchor",
        "source": source.strip(),
        "captured_at": canonical_captured_at,
        "de_vigged": True,
        "probabilities": probabilities,
    }


def predict_model(
    model: Mapping[str, Any],
    home_team: str,
    away_team: str,
    *,
    kickoff: str | datetime,
    generated_at: str | datetime | None = None,
    max_goals: int = 8,
    hard_max_goals: int = 30,
    tail_tolerance: float = 1e-8,
    allow_large_tail: bool = False,
    unknown_team_policy: str = "error",
    half_time_anchor: Mapping[str, Any] | None = None,
    full_time_anchor: Mapping[str, Any] | None = None,
    seed_method: str = "empirical_association",
) -> dict[str, Any]:
    """Predict all nine HT/FT classes with strict pre-kickoff provenance."""

    validate_model(model)
    if not isinstance(home_team, str) or not home_team.strip():
        raise HTFTModelError("home_team is required")
    if not isinstance(away_team, str) or not away_team.strip():
        raise HTFTModelError("away_team is required")
    if home_team == away_team:
        raise HTFTModelError("home_team and away_team must differ")
    if unknown_team_policy not in {"error", "league_average"}:
        raise HTFTModelError("unknown_team_policy must be error or league_average")
    if seed_method not in {"empirical_association", "experimental_score_convolution"}:
        raise HTFTModelError(
            "seed_method must be empirical_association or experimental_score_convolution"
        )

    kickoff_datetime, canonical_kickoff = _parse_aware_datetime(kickoff, "kickoff")
    prediction_datetime, canonical_generated_at = _parse_aware_datetime(
        generated_at if generated_at is not None else _utc_now(), "generated_at"
    )
    if prediction_datetime >= kickoff_datetime:
        raise HTFTModelError("generated_at must be strictly before kickoff")
    training_end = _parse_iso_date(model["training"]["end_date"], "training.end_date")
    if training_end >= kickoff_datetime.date():
        raise HTFTModelError(
            "training.end_date must be strictly before kickoff's UTC date"
        )
    if prediction_datetime.date() < training_end:
        raise HTFTModelError("generated_at cannot be before training.end_date")
    model_generated_at, _ = _parse_aware_datetime(
        model["generated_at"], "model.generated_at"
    )
    if prediction_datetime < model_generated_at:
        raise HTFTModelError("generated_at cannot be before model.generated_at")

    component_outputs = {
        name: _component_prediction(
            model["components"][name],
            home_team,
            away_team,
            max_goals=max_goals,
            hard_max_goals=hard_max_goals,
            tail_tolerance=tail_tolerance,
            allow_large_tail=allow_large_tail,
            unknown_team_policy=unknown_team_policy,
        )
        for name in ("half_time", "second_half", "full_time")
    }
    model_half_marginal = component_outputs["half_time"]["one_x_two"]
    model_full_marginal = component_outputs["full_time"]["one_x_two"]

    if half_time_anchor is None:
        half_target = _validated_marginal(
            model_half_marginal, "model half-time marginal", require_positive=True
        )
        half_provenance = {
            "origin": "model_component",
            "component": "half_time",
            "model_hash": model["components"]["half_time"]["model_hash"],
            "probabilities": half_target,
        }
    else:
        half_target, half_provenance = _validate_anchor(
            half_time_anchor,
            "half_time",
            generated_at=prediction_datetime,
            kickoff=kickoff_datetime,
        )
    if full_time_anchor is None:
        full_target = _validated_marginal(
            model_full_marginal, "model full-time marginal", require_positive=True
        )
        full_provenance = {
            "origin": "model_component",
            "component": "full_time",
            "model_hash": model["components"]["full_time"]["model_hash"],
            "probabilities": full_target,
        }
    else:
        full_target, full_provenance = _validate_anchor(
            full_time_anchor,
            "full_time",
            generated_at=prediction_datetime,
            kickoff=kickoff_datetime,
        )

    if seed_method == "empirical_association":
        raw_joint = build_empirical_association_seed(
            model["empirical_association"], half_target, full_target
        )
        construction_method = "empirical_association_lift_then_ipf"
        association_audit: dict[str, Any] | None = {
            "training_sample_count": model["empirical_association"]["sample_count"],
            "effective_sample_weight": model["empirical_association"].get(
                "effective_sample_weight",
                float(model["empirical_association"]["sample_count"]),
            ),
            "time_decay": copy.deepcopy(
                model["empirical_association"].get(
                    "time_decay",
                    {
                        "mode": "none",
                        "half_life_days": None,
                        "reference_date": model["training"]["end_date"],
                        "weight_formula": "uniform_weight_1",
                    },
                )
            ),
            "smoothing_alpha": model["empirical_association"]["smoothing_alpha"],
            "association_power": model["empirical_association"]["power"],
            "model_association_hash": _sha256_json(model["empirical_association"]),
        }
    else:
        raw_joint = build_raw_joint(
            component_outputs["half_time"]["matrix"],
            component_outputs["second_half"]["matrix"],
        )
        construction_method = (
            "experimental_half_time_plus_second_half_convolution_then_ipf"
        )
        association_audit = None
    raw_half_marginal, raw_full_marginal = _matrix_marginals(raw_joint)

    ipf_config = model["config"]["ipf"]
    joint, ipf_audit = iterative_proportional_fit(
        raw_joint,
        half_target,
        full_target,
        tolerance=ipf_config["tolerance"],
        max_iterations=ipf_config["max_iterations"],
    )
    ipf_audit["max_iterations"] = ipf_config["max_iterations"]
    aligned_half_marginal, aligned_full_marginal = _matrix_marginals(joint)
    probabilities = {
        f"{half}_{full}": joint[half_index][full_index]
        for half_index, half in enumerate(RESULTS)
        for full_index, full in enumerate(RESULTS)
    }
    code_probabilities = {
        RESULT_CODES[half] + RESULT_CODES[full]: joint[half_index][full_index]
        for half_index, half in enumerate(RESULTS)
        for full_index, full in enumerate(RESULTS)
    }
    half_time_code_probabilities = {
        RESULT_CODES[result]: aligned_half_marginal[result] for result in RESULTS
    }
    full_time_code_probabilities = {
        RESULT_CODES[result]: aligned_full_marginal[result] for result in RESULTS
    }
    class_index = {name: index for index, name in enumerate(HTFT_CLASSES)}
    ranked = sorted(
        probabilities.items(),
        key=lambda item: (-item[1], class_index[item[0]]),
    )
    warnings: list[str] = []
    for name, output in component_outputs.items():
        for warning in output["warnings"]:
            labelled = f"{name}: {warning}"
            if labelled not in warnings:
                warnings.append(labelled)

    prediction: dict[str, Any] = {
        "artifact_type": PREDICTION_ARTIFACT_TYPE,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "model_version": model["model_version"],
        "model_hash": model["model_hash"],
        "generated_at": canonical_generated_at,
        "fixture": {
            "home_team": home_team,
            "away_team": away_team,
            "kickoff": canonical_kickoff,
            "unknown_team_policy": unknown_team_policy,
            "competition_key": model["training"]["competition_key"],
        },
        "provenance": {
            "training": {
                "source_data_hash": model["training"]["source_data_hash"],
                "match_count": model["training"]["match_count"],
                "start_date": model["training"]["start_date"],
                "end_date": model["training"]["end_date"],
                "scope": model["training"]["scope"],
                "competition_key": model["training"]["competition_key"],
                "dataset_manifest_hash": model["training"]["dataset_manifest_hash"],
            },
            "training_cutoff_date": training_end.isoformat(),
            "strictly_before_kickoff_utc_date": True,
            "generated_before_kickoff": True,
            "marginal_targets": {
                "half_time": half_provenance,
                "full_time": full_provenance,
            },
            "external_anchor_enabled": (
                half_time_anchor is not None or full_time_anchor is not None
            ),
        },
        "components": {
            name: {
                "model_hash": model["components"][name]["model_hash"],
                "latent_rates": output["latent_rates"],
                "one_x_two": output["one_x_two"],
                "score_matrix_hash": output["score_matrix_hash"],
                "tail_mass": output["tail_mass"],
            }
            for name, output in component_outputs.items()
        },
        "joint_construction": {
            "method": construction_method,
            "seed_method": seed_method,
            "raw_joint": raw_joint,
            "raw_half_time_marginal": raw_half_marginal,
            "raw_full_time_marginal": raw_full_marginal,
            "association": association_audit,
            "ipf": ipf_audit,
        },
        "htft": {
            "class_order": list(HTFT_CLASSES),
            "matrix_rows_half_time": list(RESULTS),
            "matrix_columns_full_time": list(RESULTS),
            "joint_matrix": joint,
            "probabilities": probabilities,
            "code_probabilities": code_probabilities,
            "half_time_marginal": aligned_half_marginal,
            "full_time_marginal": aligned_full_marginal,
            "half_time_code_probabilities": half_time_code_probabilities,
            "full_time_code_probabilities": full_time_code_probabilities,
            "ranked": [
                {
                    "class": class_name,
                    "code": RESULT_CODES[class_name.split("_", 1)[0]]
                    + RESULT_CODES[class_name.split("_", 1)[1]],
                    "probability": probability,
                }
                for class_name, probability in ranked
            ],
            "top_one": {
                "class": ranked[0][0],
                "code": RESULT_CODES[ranked[0][0].split("_", 1)[0]]
                + RESULT_CODES[ranked[0][0].split("_", 1)[1]],
                "probability": ranked[0][1],
            },
            "top_two": [
                {
                    "class": class_name,
                    "code": RESULT_CODES[class_name.split("_", 1)[0]]
                    + RESULT_CODES[class_name.split("_", 1)[1]],
                    "probability": probability,
                }
                for class_name, probability in ranked[:2]
            ],
        },
        "warnings": warnings,
    }
    prediction["prediction_hash"] = calculate_prediction_hash(prediction)
    validate_prediction(prediction, model=model)
    return prediction


def validate_prediction(
    prediction: Mapping[str, Any],
    *,
    model: Mapping[str, Any] | None = None,
    verify_hash: bool = True,
) -> None:
    if not isinstance(prediction, Mapping):
        raise HTFTModelError("prediction must be a JSON object")
    if prediction.get("artifact_type") != PREDICTION_ARTIFACT_TYPE:
        raise HTFTModelError("unexpected prediction artifact_type")
    if prediction.get("schema_version") != PREDICTION_SCHEMA_VERSION:
        raise HTFTModelError("unsupported prediction schema_version")
    if prediction.get("model_version") != MODEL_VERSION:
        raise HTFTModelError("unsupported prediction model_version")
    if not isinstance(prediction.get("model_hash"), str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", prediction["model_hash"]
    ):
        raise HTFTModelError("prediction.model_hash must be a SHA-256 hash")
    if model is not None:
        validate_model(model)
        if prediction["model_hash"] != model["model_hash"]:
            raise HTFTModelError("prediction model_hash does not match model artifact")
    generated_at, _ = _parse_aware_datetime(
        prediction.get("generated_at"), "generated_at"
    )
    fixture = prediction.get("fixture")
    if not isinstance(fixture, Mapping):
        raise HTFTModelError("prediction fixture is missing")
    kickoff, _ = _parse_aware_datetime(fixture.get("kickoff"), "fixture.kickoff")
    if generated_at >= kickoff:
        raise HTFTModelError("prediction must be generated strictly before kickoff")
    home_team = fixture.get("home_team")
    away_team = fixture.get("away_team")
    if not isinstance(home_team, str) or not home_team.strip():
        raise HTFTModelError("prediction fixture.home_team is required")
    if not isinstance(away_team, str) or not away_team.strip():
        raise HTFTModelError("prediction fixture.away_team is required")
    if home_team == away_team:
        raise HTFTModelError("prediction fixture teams must differ")
    if fixture.get("unknown_team_policy") not in {"error", "league_average"}:
        raise HTFTModelError("prediction fixture unknown_team_policy is unsupported")
    htft = prediction.get("htft")
    if not isinstance(htft, Mapping):
        raise HTFTModelError("prediction HT/FT payload is missing")
    if htft.get("class_order") != list(HTFT_CLASSES):
        raise HTFTModelError("prediction HT/FT class order is unsupported")
    if htft.get("matrix_rows_half_time") != list(RESULTS):
        raise HTFTModelError("prediction HT/FT matrix row order is unsupported")
    if htft.get("matrix_columns_full_time") != list(RESULTS):
        raise HTFTModelError("prediction HT/FT matrix column order is unsupported")
    joint_matrix = htft.get("joint_matrix")
    _validate_joint(joint_matrix)
    probabilities = htft.get("probabilities")
    if not isinstance(probabilities, Mapping) or set(probabilities) != set(
        HTFT_CLASSES
    ):
        raise HTFTModelError("prediction must contain all nine HT/FT classes")
    for row_index, half in enumerate(RESULTS):
        for column_index, full in enumerate(RESULTS):
            name = f"{half}_{full}"
            value = _require_finite(probabilities[name], f"htft.probabilities.{name}")
            if abs(value - joint_matrix[row_index][column_index]) > 1e-12:
                raise HTFTModelError(
                    "HT/FT class probabilities do not match joint matrix"
                )
    code_probabilities = htft.get("code_probabilities")
    expected_codes = {
        RESULT_CODES[half] + RESULT_CODES[full] for half in RESULTS for full in RESULTS
    }
    if (
        not isinstance(code_probabilities, Mapping)
        or set(code_probabilities) != expected_codes
    ):
        raise HTFTModelError("prediction code_probabilities must contain HH through AA")
    for row_index, half in enumerate(RESULTS):
        for column_index, full in enumerate(RESULTS):
            code = RESULT_CODES[half] + RESULT_CODES[full]
            value = _require_finite(
                code_probabilities[code], f"htft.code_probabilities.{code}"
            )
            if abs(value - joint_matrix[row_index][column_index]) > 1e-12:
                raise HTFTModelError(
                    "HT/FT code probabilities do not match joint matrix"
                )
    declared_half = _validated_marginal(
        htft.get("half_time_marginal"), "half_time_marginal"
    )
    declared_full = _validated_marginal(
        htft.get("full_time_marginal"), "full_time_marginal"
    )
    derived_half, derived_full = _matrix_marginals(joint_matrix)
    for result in RESULTS:
        if abs(declared_half[result] - derived_half[result]) > 1e-10:
            raise HTFTModelError("half-time marginal does not match HT/FT matrix rows")
        if abs(declared_full[result] - derived_full[result]) > 1e-10:
            raise HTFTModelError(
                "full-time marginal does not match HT/FT matrix columns"
            )
    for field, declared in (
        ("half_time_code_probabilities", declared_half),
        ("full_time_code_probabilities", declared_full),
    ):
        coded = htft.get(field)
        if not isinstance(coded, Mapping) or set(coded) != {"H", "D", "A"}:
            raise HTFTModelError(f"prediction {field} must contain H, D, and A")
        normalized_coded = _validated_marginal(
            {
                "home": coded["H"],
                "draw": coded["D"],
                "away": coded["A"],
            },
            field,
        )
        if any(
            abs(normalized_coded[result] - declared[result]) > 1e-10
            for result in RESULTS
        ):
            raise HTFTModelError(f"prediction {field} disagrees with matrix marginal")

    ranked_expected = sorted(
        probabilities.items(),
        key=lambda item: (-item[1], HTFT_CLASSES.index(item[0])),
    )
    ranked = htft.get("ranked")
    if not isinstance(ranked, list) or len(ranked) != len(HTFT_CLASSES):
        raise HTFTModelError("prediction HT/FT ranked list must contain nine classes")
    for actual, (class_name, probability) in zip(ranked, ranked_expected, strict=True):
        expected_code = (
            RESULT_CODES[class_name.split("_", 1)[0]]
            + RESULT_CODES[class_name.split("_", 1)[1]]
        )
        if (
            not isinstance(actual, Mapping)
            or actual.get("class") != class_name
            or actual.get("code") != expected_code
            or abs(
                _require_finite(actual.get("probability"), "htft.ranked.probability")
                - probability
            )
            > 1e-12
        ):
            raise HTFTModelError("prediction HT/FT ranked list is inconsistent")
    expected_top_two = ranked[:2]
    if htft.get("top_two") != expected_top_two or htft.get("top_one") != ranked[0]:
        raise HTFTModelError("prediction HT/FT Top-1/Top-2 are inconsistent")

    provenance = prediction.get("provenance")
    components = prediction.get("components")
    if not isinstance(provenance, Mapping) or not isinstance(components, Mapping):
        raise HTFTModelError("prediction provenance and components are required")
    if set(components) != {"half_time", "second_half", "full_time"}:
        raise HTFTModelError("prediction must contain all three score components")
    training = provenance.get("training")
    if not isinstance(training, Mapping):
        raise HTFTModelError("prediction training provenance is required")
    training_end = _parse_iso_date(training.get("end_date"), "training.end_date")
    if training_end >= kickoff.date():
        raise HTFTModelError("prediction training cutoff must be before kickoff date")
    if generated_at.date() < training_end:
        raise HTFTModelError("prediction cannot predate its training cutoff")
    targets = provenance.get("marginal_targets")
    if not isinstance(targets, Mapping) or set(targets) != {"half_time", "full_time"}:
        raise HTFTModelError("prediction marginal target provenance is incomplete")
    external_origins = 0
    validated_targets: dict[str, dict[str, float]] = {}
    for name, declared in (("half_time", declared_half), ("full_time", declared_full)):
        target = targets.get(name)
        if not isinstance(target, Mapping):
            raise HTFTModelError(f"prediction {name} marginal target is missing")
        target_probabilities = _validated_marginal(
            target.get("probabilities"), f"provenance.marginal_targets.{name}"
        )
        validated_targets[name] = target_probabilities
        if any(
            abs(target_probabilities[result] - declared[result]) > 1e-10
            for result in RESULTS
        ):
            raise HTFTModelError(f"prediction {name} target disagrees with matrix")
        origin = target.get("origin")
        if origin == "external_de_vigged_anchor":
            external_origins += 1
            if target.get("de_vigged") is not True or not target.get("source"):
                raise HTFTModelError(f"prediction {name} external anchor is unaudited")
            captured_at, _ = _parse_aware_datetime(
                target.get("captured_at"),
                f"provenance.marginal_targets.{name}.captured_at",
            )
            if captured_at > generated_at or captured_at >= kickoff:
                raise HTFTModelError(
                    f"prediction {name} external anchor has invalid timing"
                )
        elif origin == "model_component":
            component = components.get(name)
            if not isinstance(component, Mapping):
                raise HTFTModelError(f"prediction {name} component is missing")
            component_probabilities = _validated_marginal(
                component.get("one_x_two"), f"components.{name}.one_x_two"
            )
            if any(
                abs(component_probabilities[result] - declared[result]) > 1e-10
                for result in RESULTS
            ):
                raise HTFTModelError(
                    f"prediction {name} model component disagrees with matrix"
                )
        else:
            raise HTFTModelError(f"prediction {name} marginal origin is unsupported")
    external_enabled = provenance.get("external_anchor_enabled")
    if not isinstance(external_enabled, bool) or external_enabled != bool(
        external_origins
    ):
        raise HTFTModelError("prediction external-anchor flag is inconsistent")

    construction = prediction.get("joint_construction")
    if not isinstance(construction, Mapping):
        raise HTFTModelError("prediction joint_construction is required")
    seed_method = construction.get("seed_method")
    expected_methods = {
        "empirical_association": "empirical_association_lift_then_ipf",
        "experimental_score_convolution": (
            "experimental_half_time_plus_second_half_convolution_then_ipf"
        ),
    }
    if seed_method not in expected_methods:
        raise HTFTModelError("prediction joint seed method is unsupported")
    if construction.get("method") != expected_methods[seed_method]:
        raise HTFTModelError("prediction joint construction method is inconsistent")

    raw_joint = construction.get("raw_joint")
    _validate_joint(raw_joint)
    raw_half, raw_full = _matrix_marginals(raw_joint)
    for field, derived in (
        ("raw_half_time_marginal", raw_half),
        ("raw_full_time_marginal", raw_full),
    ):
        declared_raw = _validated_marginal(construction.get(field), field)
        if any(
            abs(declared_raw[result] - derived[result]) > 1e-12 for result in RESULTS
        ):
            raise HTFTModelError(
                f"prediction {field} does not match the recorded joint seed"
            )

    ipf = construction.get("ipf")
    if not isinstance(ipf, Mapping):
        raise HTFTModelError("prediction IPF audit is required")
    if ipf.get("converged") is not True:
        raise HTFTModelError("prediction IPF audit must report convergence")
    tolerance = _require_finite(
        ipf.get("tolerance"), "joint_construction.ipf.tolerance"
    )
    if tolerance <= 0.0:
        raise HTFTModelError("prediction IPF tolerance must be positive")
    max_iterations = _require_integer(
        ipf.get("max_iterations"), "joint_construction.ipf.max_iterations"
    )
    iterations = _require_integer(
        ipf.get("iterations"), "joint_construction.ipf.iterations"
    )
    if iterations > max_iterations:
        raise HTFTModelError("prediction IPF iterations exceed max_iterations")
    maximum_error = _require_finite(
        ipf.get("maximum_marginal_error"),
        "joint_construction.ipf.maximum_marginal_error",
    )
    if maximum_error < 0.0 or maximum_error > tolerance:
        raise HTFTModelError("prediction IPF marginal error exceeds tolerance")

    reconstructed_joint, reconstructed_audit = iterative_proportional_fit(
        raw_joint,
        validated_targets["half_time"],
        validated_targets["full_time"],
        tolerance=tolerance,
        max_iterations=max_iterations,
    )
    for row_index in range(3):
        for column_index in range(3):
            if (
                abs(
                    reconstructed_joint[row_index][column_index]
                    - joint_matrix[row_index][column_index]
                )
                > 1e-12
            ):
                raise HTFTModelError(
                    "prediction joint matrix does not match IPF reconstruction"
                )
    if (
        reconstructed_audit["iterations"] != iterations
        or abs(reconstructed_audit["maximum_marginal_error"] - maximum_error) > 1e-15
    ):
        raise HTFTModelError("prediction IPF audit does not match reconstruction")

    association = construction.get("association")
    if seed_method == "empirical_association":
        if not isinstance(association, Mapping):
            raise HTFTModelError("prediction empirical association audit is required")
        training_sample_count = _require_integer(
            association.get("training_sample_count"),
            "joint_construction.association.training_sample_count",
        )
        smoothing_alpha = _require_finite(
            association.get("smoothing_alpha"),
            "joint_construction.association.smoothing_alpha",
        )
        association_power = _require_finite(
            association.get("association_power"),
            "joint_construction.association.association_power",
        )
        declared_effective_weight = association.get("effective_sample_weight")
        if declared_effective_weight is not None:
            declared_effective_weight = _require_finite(
                declared_effective_weight,
                "joint_construction.association.effective_sample_weight",
            )
            if declared_effective_weight <= 0.0:
                raise HTFTModelError(
                    "prediction empirical association effective weight is invalid"
                )
        declared_time_decay = association.get("time_decay")
        if declared_time_decay is not None and not isinstance(
            declared_time_decay, Mapping
        ):
            raise HTFTModelError(
                "prediction empirical association time_decay is invalid"
            )
        association_hash = association.get("model_association_hash")
        if smoothing_alpha <= 0.0 or association_power < 0.0:
            raise HTFTModelError("prediction empirical association audit is invalid")
        if not isinstance(association_hash, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", association_hash
        ):
            raise HTFTModelError(
                "prediction empirical association hash must be a SHA-256 hash"
            )
        if model is not None:
            model_association = model["empirical_association"]
            if association_hash != _sha256_json(model_association):
                raise HTFTModelError(
                    "prediction empirical association does not match model artifact"
                )
            if (
                training_sample_count != model_association["sample_count"]
                or abs(smoothing_alpha - model_association["smoothing_alpha"]) > 1e-15
                or abs(association_power - model_association["power"]) > 1e-15
            ):
                raise HTFTModelError(
                    "prediction empirical association metadata does not match model artifact"
                )
            expected_effective_weight = model_association.get(
                "effective_sample_weight", float(model_association["sample_count"])
            )
            expected_time_decay = model_association.get(
                "time_decay",
                {
                    "mode": "none",
                    "half_life_days": None,
                    "reference_date": model["training"]["end_date"],
                    "weight_formula": "uniform_weight_1",
                },
            )
            if "time_decay" in model_association and (
                declared_time_decay is None or declared_effective_weight is None
            ):
                raise HTFTModelError(
                    "prediction association weighting audit is missing"
                )
            if (
                declared_effective_weight is not None
                and abs(declared_effective_weight - expected_effective_weight) > 1e-12
            ):
                raise HTFTModelError(
                    "prediction association effective weight does not match model"
                )
            if declared_time_decay is not None and dict(declared_time_decay) != dict(
                expected_time_decay
            ):
                raise HTFTModelError(
                    "prediction association time_decay does not match model"
                )
            reconstructed_seed = build_empirical_association_seed(
                model_association, declared_half, declared_full
            )
            for row_index in range(3):
                for column_index in range(3):
                    if (
                        abs(
                            reconstructed_seed[row_index][column_index]
                            - raw_joint[row_index][column_index]
                        )
                        > 1e-12
                    ):
                        raise HTFTModelError(
                            "prediction empirical association seed does not match model artifact"
                        )
    elif association is not None:
        raise HTFTModelError(
            "experimental score-convolution prediction cannot claim an association audit"
        )
    if verify_hash:
        stored_hash = prediction.get("prediction_hash")
        if not isinstance(stored_hash, str) or stored_hash != calculate_prediction_hash(
            prediction
        ):
            raise HTFTModelError("prediction_hash does not match prediction contents")


def _write_training_subset(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "date",
                "home_team",
                "away_team",
                "home_goals",
                "away_goals",
                "half_home_goals",
                "half_away_goals",
                "league_key",
            ]
        )
        for row in records:
            writer.writerow(
                [
                    row["date"].isoformat(),
                    row["home_team"],
                    row["away_team"],
                    row["home_goals"],
                    row["away_goals"],
                    row["half_home_goals"],
                    row["half_away_goals"],
                    row.get("competition_key") or "",
                ]
            )


def _as_of_model(model: Mapping[str, Any], generated_at: str) -> dict[str, Any]:
    """Apply a historical as-of timestamp without changing deterministic hashes."""

    result = copy.deepcopy(model)
    result["generated_at"] = generated_at
    for component in result["components"].values():
        component["generated_at"] = generated_at
    validate_model(result)
    return result


def backtest_model(
    csv_path: str | Path,
    *,
    min_train_matches: int,
    test_block_size: int,
    half_time_half_life_days: float = 730.0,
    second_half_half_life_days: float = 365.0,
    full_time_half_life_days: float = 365.0,
    iterations: int = 1200,
    learning_rate: float = 0.03,
    regularization: float = 0.02,
    rho_min: float = -0.20,
    rho_max: float = 0.20,
    rho_step: float = 0.01,
    ipf_tolerance: float = 1e-12,
    ipf_max_iterations: int = 1000,
    max_goals: int = 10,
    hard_max_goals: int = 30,
    tail_tolerance: float = 1e-8,
    unknown_team_policy: str = "error",
    association_smoothing_alpha: float = 0.5,
    association_power: float = 1.0,
    association_half_life_days: float | None = None,
    competition_key: str | None = None,
    dataset_manifest_hash: str | None = None,
    seed_method: str = "empirical_association",
) -> dict[str, Any]:
    """Run a date-grouped expanding-window HT/FT walk-forward test."""

    min_train_matches = _require_integer(
        min_train_matches, "min_train_matches", minimum=2
    )
    test_block_size = _require_integer(test_block_size, "test_block_size")
    if unknown_team_policy not in {"error", "league_average"}:
        raise HTFTModelError("unknown_team_policy must be error or league_average")
    if seed_method not in {"empirical_association", "experimental_score_convolution"}:
        raise HTFTModelError(
            "seed_method must be empirical_association or experimental_score_convolution"
        )
    records = sorted(
        load_training_csv(csv_path),
        key=lambda row: (
            row["date"],
            row["home_team"],
            row["away_team"],
            row["home_goals"],
            row["away_goals"],
        ),
    )
    competition_key = _resolve_competition_key(records, competition_key)
    date_groups: list[tuple[date, list[dict[str, Any]]]] = []
    for row in records:
        if not date_groups or date_groups[-1][0] != row["date"]:
            date_groups.append((row["date"], []))
        date_groups[-1][1].append(row)

    accumulated = 0
    first_test_group: int | None = None
    for index, (_, group) in enumerate(date_groups):
        if accumulated >= min_train_matches:
            first_test_group = index
            break
        accumulated += len(group)
    if first_test_group is None:
        raise HTFTModelError(
            "not enough later matches remain after the minimum training window"
        )

    epsilon = BACKTEST_LOG_LOSS_FLOOR
    blocks: list[dict[str, Any]] = []
    forecasts: list[dict[str, Any]] = []
    log_losses: list[float] = []
    brier_scores: list[float] = []
    top_one_hits = 0
    top_two_hits = 0
    with tempfile.TemporaryDirectory(prefix="soccer-htft-backtest-") as temporary:
        temporary_path = Path(temporary)
        group_cursor = first_test_group
        block_number = 0
        while group_cursor < len(date_groups):
            block_number += 1
            block_groups: list[tuple[date, list[dict[str, Any]]]] = []
            block_match_count = 0
            while group_cursor < len(date_groups) and (
                not block_groups or block_match_count < test_block_size
            ):
                block_groups.append(date_groups[group_cursor])
                block_match_count += len(date_groups[group_cursor][1])
                group_cursor += 1
            test_start_date = block_groups[0][0]
            test_end_date = block_groups[-1][0]
            training_records = [row for row in records if row["date"] < test_start_date]
            test_records = [row for _, group in block_groups for row in group]
            if len(training_records) < min_train_matches:
                raise HTFTModelError(
                    "walk-forward training window is unexpectedly short"
                )
            if any(row["date"] >= test_start_date for row in training_records):
                raise HTFTModelError("walk-forward cutoff leaked a test date")

            subset_path = temporary_path / f"train-{block_number}.csv"
            _write_training_subset(subset_path, training_records)
            try:
                model = fit_model(
                    subset_path,
                    half_time_half_life_days=half_time_half_life_days,
                    second_half_half_life_days=second_half_half_life_days,
                    full_time_half_life_days=full_time_half_life_days,
                    iterations=iterations,
                    learning_rate=learning_rate,
                    regularization=regularization,
                    rho_min=rho_min,
                    rho_max=rho_max,
                    rho_step=rho_step,
                    ipf_tolerance=ipf_tolerance,
                    ipf_max_iterations=ipf_max_iterations,
                    association_smoothing_alpha=association_smoothing_alpha,
                    association_power=association_power,
                    association_half_life_days=association_half_life_days,
                    competition_key=competition_key,
                    dataset_manifest_hash=dataset_manifest_hash,
                )
            except HTFTModelError as exc:
                raise HTFTModelError(
                    f"walk-forward block {block_number} cannot fit safely: {exc}"
                ) from exc
            prediction_generated_at = test_start_date.isoformat() + "T00:00:00Z"
            model = _as_of_model(model, prediction_generated_at)
            cutoff_date = model["training"]["end_date"]
            blocks.append(
                {
                    "block": block_number,
                    "training_match_count": len(training_records),
                    "training_start_date": model["training"]["start_date"],
                    "training_cutoff_date": cutoff_date,
                    "test_start_date": test_start_date.isoformat(),
                    "test_end_date": test_end_date.isoformat(),
                    "test_match_count": len(test_records),
                    "model_hash": model["model_hash"],
                    "prediction_generated_at": prediction_generated_at,
                }
            )

            for row in test_records:
                fixture_cutoff = row["date"].isoformat() + "T23:59:59Z"
                prediction = predict_model(
                    model,
                    row["home_team"],
                    row["away_team"],
                    kickoff=fixture_cutoff,
                    generated_at=prediction_generated_at,
                    max_goals=max_goals,
                    hard_max_goals=hard_max_goals,
                    tail_tolerance=tail_tolerance,
                    unknown_team_policy=unknown_team_policy,
                    seed_method=seed_method,
                )
                half_result = _result(row["half_home_goals"], row["half_away_goals"])
                full_result = _result(row["home_goals"], row["away_goals"])
                actual_class = f"{half_result}_{full_result}"
                probabilities = prediction["htft"]["probabilities"]
                actual_probability = probabilities[actual_class]
                log_loss = -math.log(max(actual_probability, epsilon))
                brier = math.fsum(
                    (
                        probabilities[class_name]
                        - (1.0 if class_name == actual_class else 0.0)
                    )
                    ** 2
                    for class_name in HTFT_CLASSES
                )
                top_two = [item["class"] for item in prediction["htft"]["top_two"]]
                top_one_hit = top_two[0] == actual_class
                top_two_hit = actual_class in top_two
                top_one_hits += int(top_one_hit)
                top_two_hits += int(top_two_hit)
                log_losses.append(log_loss)
                brier_scores.append(brier)
                forecasts.append(
                    {
                        "block": block_number,
                        "date": row["date"].isoformat(),
                        "home_team": row["home_team"],
                        "away_team": row["away_team"],
                        "actual_half_time_score": (
                            f"{row['half_home_goals']}-{row['half_away_goals']}"
                        ),
                        "actual_full_time_score": (
                            f"{row['home_goals']}-{row['away_goals']}"
                        ),
                        "actual_class": actual_class,
                        "probabilities": dict(probabilities),
                        "actual_class_probability": actual_probability,
                        "model_hash": model["model_hash"],
                        "training_cutoff_date": cutoff_date,
                        "prediction_generated_at": prediction_generated_at,
                        "date_only_fixture_cutoff": fixture_cutoff,
                        "prediction_hash": prediction["prediction_hash"],
                        "top_two": prediction["htft"]["top_two"],
                        "scores": {
                            "nine_class_log_loss": log_loss,
                            "nine_class_brier": brier,
                            "top_one_hit": top_one_hit,
                            "top_two_hit": top_two_hit,
                        },
                        "warnings": prediction["warnings"],
                    }
                )

    sample_count = len(forecasts)
    if sample_count == 0:
        raise HTFTModelError("walk-forward produced no test predictions")
    artifact: dict[str, Any] = {
        "artifact_type": BACKTEST_ARTIFACT_TYPE,
        "schema_version": BACKTEST_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "input_data_hash": _sha256_json(_canonical_training_rows(records)),
        "split_policy": {
            "method": "expanding_window_contiguous_date_blocks",
            "random_split": False,
            "same_date_split_allowed": False,
            "min_train_matches": min_train_matches,
            "requested_test_block_size": test_block_size,
            "strict_training_cutoff": "training date < first test date",
            "date_only_evaluation_convention": (
                "prediction at 00:00Z; synthetic fixture cutoff 23:59:59Z"
            ),
        },
        "fit_config": {
            "score_models": {
                name: _score_fit_config(
                    half_life_days=half_life,
                    iterations=int(iterations),
                    learning_rate=learning_rate,
                    regularization=regularization,
                    rho_min=rho_min,
                    rho_max=rho_max,
                    rho_step=rho_step,
                )
                for name, half_life in {
                    "half_time": half_time_half_life_days,
                    "second_half": second_half_half_life_days,
                    "full_time": full_time_half_life_days,
                }.items()
            },
            "ipf": {
                "tolerance": ipf_tolerance,
                "max_iterations": ipf_max_iterations,
            },
            "max_goals": max_goals,
            "hard_max_goals": hard_max_goals,
            "tail_tolerance": tail_tolerance,
            "unknown_team_policy": unknown_team_policy,
            "association_smoothing_alpha": association_smoothing_alpha,
            "association_power": association_power,
            "association_half_life_days": association_half_life_days,
            "seed_method": seed_method,
            "competition_key": competition_key,
            "dataset_manifest_hash": dataset_manifest_hash,
            "external_anchor_enabled": False,
        },
        "blocks": blocks,
        "predictions": forecasts,
        "metrics": {
            "sample_count": sample_count,
            "nine_class_log_loss": math.fsum(log_losses) / sample_count,
            "nine_class_brier": math.fsum(brier_scores) / sample_count,
            "top_one_accuracy": top_one_hits / sample_count,
            "top_two_accuracy": top_two_hits / sample_count,
            "top_one_hits": top_one_hits,
            "top_two_hits": top_two_hits,
            "definitions": {
                "nine_class_log_loss": (
                    "negative log probability assigned to the observed HT/FT class"
                ),
                "nine_class_brier": (
                    "sum of squared error over all nine HT/FT classes, averaged by match"
                ),
                "top_one_accuracy": "observed class equals the highest-probability class",
                "top_two_accuracy": "observed class is among the two highest probabilities",
                "log_loss_floor": epsilon,
            },
        },
    }
    artifact["backtest_hash"] = calculate_backtest_hash(artifact)
    validate_backtest(artifact)
    return artifact


def validate_backtest(backtest: Mapping[str, Any], *, verify_hash: bool = True) -> None:
    if not isinstance(backtest, Mapping):
        raise HTFTModelError("backtest must be a JSON object")
    if backtest.get("artifact_type") != BACKTEST_ARTIFACT_TYPE:
        raise HTFTModelError("unexpected backtest artifact_type")
    if backtest.get("schema_version") != BACKTEST_SCHEMA_VERSION:
        raise HTFTModelError("unsupported backtest schema_version")
    if backtest.get("model_version") != MODEL_VERSION:
        raise HTFTModelError("unsupported backtest model_version")
    input_hash = backtest.get("input_data_hash")
    if not isinstance(input_hash, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", input_hash
    ):
        raise HTFTModelError("backtest.input_data_hash must be a SHA-256 hash")
    split_policy = backtest.get("split_policy")
    fit_config = backtest.get("fit_config")
    blocks = backtest.get("blocks")
    metrics = backtest.get("metrics")
    predictions = backtest.get("predictions")
    if (
        not isinstance(split_policy, Mapping)
        or not isinstance(fit_config, Mapping)
        or not isinstance(blocks, list)
        or not blocks
        or not isinstance(metrics, Mapping)
        or not isinstance(predictions, list)
    ):
        raise HTFTModelError(
            "backtest split policy, fit config, blocks, metrics, and predictions are required"
        )
    if (
        split_policy.get("method") != "expanding_window_contiguous_date_blocks"
        or split_policy.get("random_split") is not False
        or split_policy.get("same_date_split_allowed") is not False
        or split_policy.get("strict_training_cutoff")
        != "training date < first test date"
    ):
        raise HTFTModelError("backtest split policy is unsupported")
    _require_integer(
        split_policy.get("min_train_matches"),
        "split_policy.min_train_matches",
        minimum=2,
    )
    _require_integer(
        split_policy.get("requested_test_block_size"),
        "split_policy.requested_test_block_size",
    )

    block_by_number: dict[int, dict[str, Any]] = {}
    prior_block: dict[str, Any] | None = None
    expected_total_predictions = 0
    for expected_number, raw_block in enumerate(blocks, start=1):
        if not isinstance(raw_block, Mapping):
            raise HTFTModelError("backtest block must be a JSON object")
        block_number = _require_integer(raw_block.get("block"), "blocks.block")
        if block_number != expected_number:
            raise HTFTModelError("backtest blocks must be sequential from one")
        training_match_count = _require_integer(
            raw_block.get("training_match_count"),
            "blocks.training_match_count",
            minimum=2,
        )
        test_match_count = _require_integer(
            raw_block.get("test_match_count"), "blocks.test_match_count"
        )
        training_start = _parse_iso_date(
            raw_block.get("training_start_date"), "blocks.training_start_date"
        )
        training_cutoff = _parse_iso_date(
            raw_block.get("training_cutoff_date"), "blocks.training_cutoff_date"
        )
        test_start = _parse_iso_date(
            raw_block.get("test_start_date"), "blocks.test_start_date"
        )
        test_end = _parse_iso_date(
            raw_block.get("test_end_date"), "blocks.test_end_date"
        )
        if not training_start <= training_cutoff < test_start <= test_end:
            raise HTFTModelError(
                "backtest block date cutoffs are not strictly out of sample"
            )
        model_hash = raw_block.get("model_hash")
        if not isinstance(model_hash, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", model_hash
        ):
            raise HTFTModelError("blocks.model_hash must be a SHA-256 hash")
        generated_at, canonical_generated_at = _parse_aware_datetime(
            raw_block.get("prediction_generated_at"),
            "blocks.prediction_generated_at",
        )
        if canonical_generated_at != test_start.isoformat() + "T00:00:00Z":
            raise HTFTModelError(
                "backtest block prediction timestamp must be test-start 00:00Z"
            )
        if prior_block is not None:
            if training_start != prior_block["training_start"]:
                raise HTFTModelError(
                    "backtest expanding windows changed training start"
                )
            if test_start <= prior_block["test_end"]:
                raise HTFTModelError(
                    "backtest test blocks overlap or split the same date"
                )
            if training_cutoff < prior_block["test_end"]:
                raise HTFTModelError(
                    "backtest expanding window omitted an earlier test date"
                )
            if training_match_count != (
                prior_block["training_match_count"] + prior_block["test_match_count"]
            ):
                raise HTFTModelError(
                    "backtest expanding-window training count is inconsistent"
                )
        normalized_block = {
            "training_start": training_start,
            "training_cutoff": training_cutoff,
            "test_start": test_start,
            "test_end": test_end,
            "training_match_count": training_match_count,
            "test_match_count": test_match_count,
            "model_hash": model_hash,
            "prediction_generated_at": generated_at,
            "prediction_generated_at_raw": raw_block["prediction_generated_at"],
        }
        block_by_number[block_number] = normalized_block
        prior_block = normalized_block
        expected_total_predictions += test_match_count

    definitions = metrics.get("definitions")
    if not isinstance(definitions, Mapping):
        raise HTFTModelError("backtest metric definitions are required")
    epsilon = _require_finite(
        definitions.get("log_loss_floor"), "metrics.definitions.log_loss_floor"
    )
    if abs(epsilon - BACKTEST_LOG_LOSS_FLOOR) > 0.0:
        raise HTFTModelError("backtest log-loss floor is unsupported")

    recomputed_log_losses: list[float] = []
    recomputed_brier_scores: list[float] = []
    recomputed_top_one_hits = 0
    recomputed_top_two_hits = 0
    prediction_counts = {block_number: 0 for block_number in block_by_number}

    def parse_score(raw: Any, name: str) -> tuple[int, int]:
        if not isinstance(raw, str) or not re.fullmatch(r"\d+-\d+", raw):
            raise HTFTModelError(f"{name} must be a non-negative score")
        home_goals, away_goals = (int(value) for value in raw.split("-", 1))
        return home_goals, away_goals

    for forecast in predictions:
        if not isinstance(forecast, Mapping):
            raise HTFTModelError("backtest prediction must be a JSON object")
        block_number = _require_integer(forecast.get("block"), "predictions.block")
        block = block_by_number.get(block_number)
        if block is None:
            raise HTFTModelError("backtest prediction references an unknown block")
        prediction_counts[block_number] += 1
        prediction_date = _parse_iso_date(forecast.get("date"), "predictions.date")
        if not block["test_start"] <= prediction_date <= block["test_end"]:
            raise HTFTModelError("backtest prediction date is outside its test block")
        training_cutoff = _parse_iso_date(
            forecast.get("training_cutoff_date"),
            "predictions.training_cutoff_date",
        )
        if (
            training_cutoff != block["training_cutoff"]
            or training_cutoff >= prediction_date
        ):
            raise HTFTModelError(
                "backtest prediction training cutoff is not strictly before the fixture"
            )
        if forecast.get("model_hash") != block["model_hash"]:
            raise HTFTModelError(
                "backtest prediction model_hash does not match its block"
            )
        prediction_hash = forecast.get("prediction_hash")
        if not isinstance(prediction_hash, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", prediction_hash
        ):
            raise HTFTModelError("predictions.prediction_hash must be a SHA-256 hash")
        generated_at, _ = _parse_aware_datetime(
            forecast.get("prediction_generated_at"),
            "predictions.prediction_generated_at",
        )
        if (
            generated_at != block["prediction_generated_at"]
            or forecast.get("prediction_generated_at")
            != block["prediction_generated_at_raw"]
        ):
            raise HTFTModelError(
                "backtest prediction timestamp does not match its block"
            )
        fixture_cutoff, canonical_fixture_cutoff = _parse_aware_datetime(
            forecast.get("date_only_fixture_cutoff"),
            "predictions.date_only_fixture_cutoff",
        )
        expected_fixture_cutoff = prediction_date.isoformat() + "T23:59:59Z"
        if (
            canonical_fixture_cutoff != expected_fixture_cutoff
            or generated_at >= fixture_cutoff
        ):
            raise HTFTModelError("backtest synthetic fixture cutoff is inconsistent")
        home_team = forecast.get("home_team")
        away_team = forecast.get("away_team")
        if (
            not isinstance(home_team, str)
            or not home_team.strip()
            or not isinstance(away_team, str)
            or not away_team.strip()
            or home_team == away_team
        ):
            raise HTFTModelError("backtest prediction teams are invalid")

        half_score = parse_score(
            forecast.get("actual_half_time_score"),
            "predictions.actual_half_time_score",
        )
        full_score = parse_score(
            forecast.get("actual_full_time_score"),
            "predictions.actual_full_time_score",
        )
        if half_score[0] > full_score[0] or half_score[1] > full_score[1]:
            raise HTFTModelError(
                "backtest half-time score cannot exceed full-time score"
            )
        expected_actual_class = f"{_result(*half_score)}_{_result(*full_score)}"
        actual_class = forecast.get("actual_class")
        if actual_class != expected_actual_class:
            raise HTFTModelError("backtest observed HT/FT class does not match scores")

        probabilities = forecast.get("probabilities")
        if not isinstance(probabilities, Mapping) or set(probabilities) != set(
            HTFT_CLASSES
        ):
            raise HTFTModelError(
                "backtest prediction must store all nine probabilities"
            )
        normalized_probabilities: dict[str, float] = {}
        for class_name in HTFT_CLASSES:
            probability = _require_finite(
                probabilities[class_name],
                f"predictions.probabilities.{class_name}",
            )
            if probability < 0.0:
                raise HTFTModelError("backtest class probabilities cannot be negative")
            normalized_probabilities[class_name] = probability
        if abs(math.fsum(normalized_probabilities.values()) - 1.0) > 1e-12:
            raise HTFTModelError("backtest class probabilities must sum to one")
        actual_probability = normalized_probabilities[actual_class]
        if (
            abs(
                _require_finite(
                    forecast.get("actual_class_probability"),
                    "predictions.actual_class_probability",
                )
                - actual_probability
            )
            > 1e-15
        ):
            raise HTFTModelError(
                "backtest actual-class probability does not match probability vector"
            )
        ranked = sorted(
            normalized_probabilities.items(),
            key=lambda item: (-item[1], HTFT_CLASSES.index(item[0])),
        )
        expected_top_two = [
            {
                "class": class_name,
                "code": RESULT_CODES[class_name.split("_", 1)[0]]
                + RESULT_CODES[class_name.split("_", 1)[1]],
                "probability": probability,
            }
            for class_name, probability in ranked[:2]
        ]
        if forecast.get("top_two") != expected_top_two:
            raise HTFTModelError("backtest Top-2 does not match probability vector")

        log_loss = -math.log(max(actual_probability, epsilon))
        brier = math.fsum(
            (
                normalized_probabilities[class_name]
                - (1.0 if class_name == actual_class else 0.0)
            )
            ** 2
            for class_name in HTFT_CLASSES
        )
        top_one_hit = ranked[0][0] == actual_class
        top_two_hit = actual_class in {item[0] for item in ranked[:2]}
        scores = forecast.get("scores")
        if not isinstance(scores, Mapping):
            raise HTFTModelError("backtest per-prediction scores are required")
        if (
            abs(
                _require_finite(
                    scores.get("nine_class_log_loss"),
                    "predictions.scores.nine_class_log_loss",
                )
                - log_loss
            )
            > 1e-12
            or abs(
                _require_finite(
                    scores.get("nine_class_brier"),
                    "predictions.scores.nine_class_brier",
                )
                - brier
            )
            > 1e-12
        ):
            raise HTFTModelError("backtest per-prediction scores do not recompute")
        if (
            scores.get("top_one_hit") is not top_one_hit
            or scores.get("top_two_hit") is not top_two_hit
        ):
            raise HTFTModelError("backtest per-prediction hit flags do not recompute")
        recomputed_log_losses.append(log_loss)
        recomputed_brier_scores.append(brier)
        recomputed_top_one_hits += int(top_one_hit)
        recomputed_top_two_hits += int(top_two_hit)

    sample_count = _require_integer(metrics.get("sample_count"), "metrics.sample_count")
    if (
        sample_count != len(predictions)
        or sample_count != expected_total_predictions
        or any(
            prediction_counts[number] != block["test_match_count"]
            for number, block in block_by_number.items()
        )
    ):
        raise HTFTModelError(
            "backtest sample counts do not match blocks and predictions"
        )
    expected_metrics = {
        "nine_class_log_loss": math.fsum(recomputed_log_losses) / sample_count,
        "nine_class_brier": math.fsum(recomputed_brier_scores) / sample_count,
        "top_one_accuracy": recomputed_top_one_hits / sample_count,
        "top_two_accuracy": recomputed_top_two_hits / sample_count,
    }
    for name, expected in expected_metrics.items():
        value = _require_finite(metrics.get(name), f"metrics.{name}")
        if abs(value - expected) > 1e-12:
            raise HTFTModelError(f"metrics.{name} does not recompute from predictions")
    stored_top_one_hits = _require_integer(
        metrics.get("top_one_hits"), "metrics.top_one_hits", minimum=0
    )
    stored_top_two_hits = _require_integer(
        metrics.get("top_two_hits"), "metrics.top_two_hits", minimum=0
    )
    if (
        stored_top_one_hits != recomputed_top_one_hits
        or stored_top_two_hits != recomputed_top_two_hits
    ):
        raise HTFTModelError("backtest aggregate hit counts do not recompute")
    if verify_hash:
        stored_hash = backtest.get("backtest_hash")
        if not isinstance(stored_hash, str) or stored_hash != calculate_backtest_hash(
            backtest
        ):
            raise HTFTModelError("backtest_hash does not match backtest contents")


def _parse_probability_triplet(raw: str, name: str) -> dict[str, float]:
    try:
        parts = [float(part.strip()) for part in raw.split(",")]
    except ValueError as exc:
        raise HTFTModelError(f"{name} must be HOME,DRAW,AWAY probabilities") from exc
    if len(parts) != 3:
        raise HTFTModelError(f"{name} must contain exactly three probabilities")
    return _validated_marginal(dict(zip(RESULTS, parts)), name, require_positive=True)


def _cli_anchor(
    probabilities: str | None,
    source: str | None,
    captured_at: str | None,
    name: str,
) -> dict[str, Any] | None:
    supplied = [value is not None for value in (probabilities, source, captured_at)]
    if not any(supplied):
        return None
    if not all(supplied):
        raise HTFTModelError(
            f"{name} anchor requires marginal, source, and captured-at together"
        )
    return {
        "probabilities": _parse_probability_triplet(probabilities or "", name),
        "source": source,
        "captured_at": captured_at,
        "de_vigged": True,
    }


def _add_fit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--half-time-half-life-days", type=float, default=730.0)
    parser.add_argument("--second-half-half-life-days", type=float, default=365.0)
    parser.add_argument("--full-time-half-life-days", type=float, default=365.0)
    parser.add_argument("--iterations", type=int, default=1200)
    parser.add_argument("--learning-rate", type=float, default=0.03)
    parser.add_argument("--regularization", type=float, default=0.02)
    parser.add_argument("--rho-min", type=float, default=-0.20)
    parser.add_argument("--rho-max", type=float, default=0.20)
    parser.add_argument("--rho-step", type=float, default=0.01)
    parser.add_argument("--ipf-tolerance", type=float, default=1e-12)
    parser.add_argument("--ipf-max-iterations", type=int, default=1000)
    parser.add_argument("--association-smoothing-alpha", type=float, default=0.5)
    parser.add_argument("--association-power", type=float, default=1.0)
    parser.add_argument(
        "--association-half-life-days",
        type=float,
        help=(
            "optional exponential half-life for HT/FT association counts; "
            "omitting it preserves the validated uniform-weight configuration"
        ),
    )
    parser.add_argument(
        "--competition-key",
        help="single competition identifier; must match CSV league_key when present",
    )
    parser.add_argument(
        "--dataset-manifest-hash",
        help="optional sha256:... hash of the audited import manifest",
    )


def _fit_kwargs(arguments: argparse.Namespace) -> dict[str, Any]:
    return {
        "half_time_half_life_days": arguments.half_time_half_life_days,
        "second_half_half_life_days": arguments.second_half_half_life_days,
        "full_time_half_life_days": arguments.full_time_half_life_days,
        "iterations": arguments.iterations,
        "learning_rate": arguments.learning_rate,
        "regularization": arguments.regularization,
        "rho_min": arguments.rho_min,
        "rho_max": arguments.rho_max,
        "rho_step": arguments.rho_step,
        "ipf_tolerance": arguments.ipf_tolerance,
        "ipf_max_iterations": arguments.ipf_max_iterations,
        "association_smoothing_alpha": arguments.association_smoothing_alpha,
        "association_power": arguments.association_power,
        "association_half_life_days": arguments.association_half_life_days,
        "competition_key": arguments.competition_key,
        "dataset_manifest_hash": arguments.dataset_manifest_hash,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train and use a dedicated 9-class half-time/full-time model"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fit = subparsers.add_parser("fit", help="fit a league-scoped HT/FT model")
    fit.add_argument("--input", required=True, help="historical HT/FT CSV")
    fit.add_argument("--output", required=True, help="model JSON output")
    _add_fit_arguments(fit)

    predict = subparsers.add_parser("predict", help="predict nine HT/FT classes")
    predict.add_argument("--model", required=True, help="model JSON")
    predict.add_argument("--home-team", required=True)
    predict.add_argument("--away-team", required=True)
    predict.add_argument("--kickoff", required=True, help="timezone-aware ISO datetime")
    predict.add_argument("--generated-at", help="timezone-aware as-of datetime")
    predict.add_argument("--output", required=True, help="prediction JSON output")
    predict.add_argument("--max-goals", type=int, default=8)
    predict.add_argument("--hard-max-goals", type=int, default=30)
    predict.add_argument("--tail-tolerance", type=float, default=1e-8)
    predict.add_argument("--allow-large-tail", action="store_true")
    predict.add_argument(
        "--unknown-team-policy",
        choices=("error", "league_average"),
        default="error",
    )
    predict.add_argument(
        "--seed-method",
        choices=("empirical_association", "experimental_score_convolution"),
        default="empirical_association",
        help="validated empirical association is the default; convolution is experimental",
    )
    predict.add_argument(
        "--half-time-marginal",
        help="optional already de-vigged HOME,DRAW,AWAY probabilities",
    )
    predict.add_argument("--half-time-anchor-source")
    predict.add_argument("--half-time-anchor-captured-at")
    predict.add_argument(
        "--full-time-marginal",
        help="optional already de-vigged HOME,DRAW,AWAY probabilities",
    )
    predict.add_argument("--full-time-anchor-source")
    predict.add_argument("--full-time-anchor-captured-at")

    backtest = subparsers.add_parser(
        "backtest", help="run expanding-window HT/FT evaluation"
    )
    backtest.add_argument("--input", required=True, help="historical HT/FT CSV")
    backtest.add_argument("--output", required=True, help="backtest JSON output")
    backtest.add_argument("--min-train-matches", type=int, required=True)
    backtest.add_argument("--test-block-size", type=int, required=True)
    _add_fit_arguments(backtest)
    backtest.add_argument("--max-goals", type=int, default=10)
    backtest.add_argument("--hard-max-goals", type=int, default=30)
    backtest.add_argument("--tail-tolerance", type=float, default=1e-8)
    backtest.add_argument(
        "--unknown-team-policy",
        choices=("error", "league_average"),
        default="error",
    )
    backtest.add_argument(
        "--seed-method",
        choices=("empirical_association", "experimental_score_convolution"),
        default="empirical_association",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "fit":
            artifact = fit_model(arguments.input, **_fit_kwargs(arguments))
        elif arguments.command == "predict":
            half_time_anchor = _cli_anchor(
                arguments.half_time_marginal,
                arguments.half_time_anchor_source,
                arguments.half_time_anchor_captured_at,
                "half_time",
            )
            full_time_anchor = _cli_anchor(
                arguments.full_time_marginal,
                arguments.full_time_anchor_source,
                arguments.full_time_anchor_captured_at,
                "full_time",
            )
            artifact = predict_model(
                load_model(arguments.model),
                arguments.home_team,
                arguments.away_team,
                kickoff=arguments.kickoff,
                generated_at=arguments.generated_at,
                max_goals=arguments.max_goals,
                hard_max_goals=arguments.hard_max_goals,
                tail_tolerance=arguments.tail_tolerance,
                allow_large_tail=arguments.allow_large_tail,
                unknown_team_policy=arguments.unknown_team_policy,
                seed_method=arguments.seed_method,
                half_time_anchor=half_time_anchor,
                full_time_anchor=full_time_anchor,
            )
        else:
            artifact = backtest_model(
                arguments.input,
                min_train_matches=arguments.min_train_matches,
                test_block_size=arguments.test_block_size,
                max_goals=arguments.max_goals,
                hard_max_goals=arguments.hard_max_goals,
                tail_tolerance=arguments.tail_tolerance,
                unknown_team_policy=arguments.unknown_team_policy,
                seed_method=arguments.seed_method,
                **_fit_kwargs(arguments),
            )
        save_json(artifact, arguments.output)
    except (HTFTModelError, score_model.ScoreModelError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

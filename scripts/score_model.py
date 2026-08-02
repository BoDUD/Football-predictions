#!/usr/bin/env python3
"""Train and use a deterministic, time-decayed Dixon-Coles score model.

The prediction artifact contains one canonical, normalized score matrix.  Every
reported football market is aggregated from that matrix; no market gets an
independent probability estimate.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence


MODEL_ARTIFACT_TYPE = "soccer_score_model"
PREDICTION_ARTIFACT_TYPE = "soccer_score_prediction"
BACKTEST_ARTIFACT_TYPE = "soccer_score_backtest"
MODEL_SCHEMA_VERSION = "1.0.0"
PREDICTION_SCHEMA_VERSION = "1.0.0"
BACKTEST_SCHEMA_VERSION = "1.0.0"
MODEL_VERSION = "dixon-coles-time-decay/1.0.0"
REQUIRED_COLUMNS = {
    "date",
    "home_team",
    "away_team",
    "home_goals",
    "away_goals",
}
SETTLEMENT_STATES = (
    "full_win",
    "half_win",
    "push",
    "half_loss",
    "full_loss",
)


class ScoreModelError(ValueError):
    """Raised when training data, a model, or a prediction is unsafe to use."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _require_finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ScoreModelError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ScoreModelError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ScoreModelError(f"{name} must be finite")
    return number


def _parse_aware_datetime(raw: Any, name: str) -> tuple[datetime, str]:
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ScoreModelError(f"{name} must be an ISO-8601 datetime") from exc
    else:
        raise ScoreModelError(f"{name} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScoreModelError(f"{name} needs an explicit UTC offset")
    normalized = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    canonical = normalized.isoformat(timespec=timespec).replace("+00:00", "Z")
    return normalized, canonical


def _parse_iso_date_field(raw: Any, name: str) -> date:
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ScoreModelError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ScoreModelError(f"{name} must be a valid ISO date") from exc


def _require_positive_integer(value: Any, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ScoreModelError(f"{name} must be an integer >= {minimum}")
    return value


def _parse_match_date(raw: str, row_number: int) -> date:
    value = (raw or "").strip()
    if not value:
        raise ScoreModelError(f"row {row_number}: date is required")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ScoreModelError(
                f"row {row_number}: date must be a valid ISO date"
            ) from exc

    # Datetimes are accepted only when their offset is explicit.  This avoids
    # silently assigning a local timezone to historical observations.
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScoreModelError(
            f"row {row_number}: date must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScoreModelError(
            f"row {row_number}: datetime date values need an explicit offset"
        )
    return parsed.astimezone(timezone.utc).date()


def _parse_goal(raw: str, field: str, row_number: int) -> int:
    value = (raw or "").strip()
    if not re.fullmatch(r"\d+", value):
        raise ScoreModelError(
            f"row {row_number}: {field} must be a non-negative integer"
        )
    result = int(value)
    if result > 99:
        raise ScoreModelError(f"row {row_number}: {field} is implausibly large")
    return result


def load_training_csv(path: str | Path) -> list[dict[str, Any]]:
    """Load and strictly validate regulation-time historical score rows."""

    source = Path(path)
    try:
        handle = source.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ScoreModelError(f"cannot read training CSV: {source}") from exc

    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ScoreModelError("training CSV has no header")
        missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames))
        if missing:
            raise ScoreModelError(
                "training CSV missing columns: " + ", ".join(missing)
            )

        records: list[dict[str, Any]] = []
        fixtures: dict[tuple[date, str, str], tuple[int, int]] = {}
        for row_number, row in enumerate(reader, start=2):
            home_team = (row.get("home_team") or "").strip()
            away_team = (row.get("away_team") or "").strip()
            if not home_team or not away_team:
                raise ScoreModelError(
                    f"row {row_number}: home_team and away_team are required"
                )
            if home_team == away_team:
                raise ScoreModelError(
                    f"row {row_number}: home_team and away_team must differ"
                )
            match_date = _parse_match_date(row.get("date") or "", row_number)
            home_goals = _parse_goal(
                row.get("home_goals") or "", "home_goals", row_number
            )
            away_goals = _parse_goal(
                row.get("away_goals") or "", "away_goals", row_number
            )
            fixture_key = (match_date, home_team, away_team)
            score = (home_goals, away_goals)
            if fixture_key in fixtures:
                status = "duplicate" if fixtures[fixture_key] == score else "conflicting"
                raise ScoreModelError(
                    f"row {row_number}: {status} score for "
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
                }
            )

    if len(records) < 2:
        raise ScoreModelError("training CSV needs at least two matches")
    teams = {row["home_team"] for row in records} | {
        row["away_team"] for row in records
    }
    if len(teams) < 2:
        raise ScoreModelError("training CSV needs at least two teams")
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
        disconnected = ", ".join(sorted(teams - connected))
        raise ScoreModelError(
            "training fixture graph is disconnected; unrelated team component: "
            + disconnected
        )
    return records


def _canonical_training_rows(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        {
            "date": row["date"].isoformat(),
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "home_goals": int(row["home_goals"]),
            "away_goals": int(row["away_goals"]),
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
        ),
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
        raise ScoreModelError("artifact contains non-canonical values") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def calculate_model_hash(model: Mapping[str, Any]) -> str:
    """Hash the reproducible model payload, excluding wall-clock metadata."""

    payload = dict(model)
    payload.pop("model_hash", None)
    payload.pop("generated_at", None)
    return _sha256_json(payload)


def _safe_rate(log_rate: float) -> float:
    # This is an optimizer guard, not a prediction fallback.  Fitted parameters
    # themselves remain bounded and prediction rejects invalid model values.
    return math.exp(max(-10.0, min(10.0, log_rate)))


def _dc_tau(
    home_goals: int,
    away_goals: int,
    home_rate: float,
    away_rate: float,
    rho: float,
) -> float:
    if home_goals == 0 and away_goals == 0:
        return 1.0 - home_rate * away_rate * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + home_rate * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + away_rate * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def _poisson_nll(
    records: Sequence[Mapping[str, Any]],
    weights: Sequence[float],
    teams: Sequence[str],
    values: Sequence[float],
    regularization: float,
) -> float:
    team_index = {team: index for index, team in enumerate(teams)}
    count = len(teams)
    intercept, home_advantage = values[0], values[1]
    attack = values[2 : 2 + count]
    defense = values[2 + count :]
    total_weight = sum(weights)
    loss = 0.0
    for row, weight in zip(records, weights):
        home = team_index[row["home_team"]]
        away = team_index[row["away_team"]]
        home_rate = _safe_rate(
            intercept + home_advantage + attack[home] + defense[away]
        )
        away_rate = _safe_rate(intercept + attack[away] + defense[home])
        loss += weight * (
            home_rate
            - row["home_goals"] * math.log(home_rate)
            + math.lgamma(row["home_goals"] + 1)
            + away_rate
            - row["away_goals"] * math.log(away_rate)
            + math.lgamma(row["away_goals"] + 1)
        )
    penalty = regularization * sum(value * value for value in values[1:])
    return loss / total_weight + penalty


def _fit_poisson_parameters(
    records: Sequence[Mapping[str, Any]],
    weights: Sequence[float],
    teams: Sequence[str],
    *,
    iterations: int,
    learning_rate: float,
    regularization: float,
) -> tuple[list[float], float]:
    team_index = {team: index for index, team in enumerate(teams)}
    count = len(teams)
    total_weight = sum(weights)
    weighted_goals = sum(
        weight * (row["home_goals"] + row["away_goals"])
        for row, weight in zip(records, weights)
    )
    baseline = max(0.05, weighted_goals / (2.0 * total_weight))
    values = [math.log(baseline), 0.1] + [0.0] * (2 * count)
    first_moment = [0.0] * len(values)
    second_moment = [0.0] * len(values)
    beta1 = 0.9
    beta2 = 0.999
    epsilon = 1e-8

    for step in range(1, iterations + 1):
        gradient = [0.0] * len(values)
        intercept, home_advantage = values[0], values[1]
        attack_offset = 2
        defense_offset = 2 + count

        for row, weight in zip(records, weights):
            home = team_index[row["home_team"]]
            away = team_index[row["away_team"]]
            home_rate = _safe_rate(
                intercept
                + home_advantage
                + values[attack_offset + home]
                + values[defense_offset + away]
            )
            away_rate = _safe_rate(
                intercept
                + values[attack_offset + away]
                + values[defense_offset + home]
            )
            home_error = weight * (home_rate - row["home_goals"]) / total_weight
            away_error = weight * (away_rate - row["away_goals"]) / total_weight

            gradient[0] += home_error + away_error
            gradient[1] += home_error
            gradient[attack_offset + home] += home_error
            gradient[defense_offset + away] += home_error
            gradient[attack_offset + away] += away_error
            gradient[defense_offset + home] += away_error

        for index in range(1, len(values)):
            gradient[index] += 2.0 * regularization * values[index]

        for index, grad in enumerate(gradient):
            first_moment[index] = beta1 * first_moment[index] + (1.0 - beta1) * grad
            second_moment[index] = (
                beta2 * second_moment[index] + (1.0 - beta2) * grad * grad
            )
            corrected_first = first_moment[index] / (1.0 - beta1**step)
            corrected_second = second_moment[index] / (1.0 - beta2**step)
            values[index] -= learning_rate * corrected_first / (
                math.sqrt(corrected_second) + epsilon
            )

        # Zero-centre attack and defence while preserving every fitted log-rate.
        attack_mean = sum(values[attack_offset:defense_offset]) / count
        for index in range(attack_offset, defense_offset):
            values[index] -= attack_mean
        values[0] += attack_mean

        defense_mean = sum(values[defense_offset:]) / count
        for index in range(defense_offset, len(values)):
            values[index] -= defense_mean
        values[0] += defense_mean

        values[0] = max(-3.0, min(3.0, values[0]))
        values[1] = max(-2.0, min(2.0, values[1]))
        for index in range(attack_offset, len(values)):
            values[index] = max(-3.0, min(3.0, values[index]))

    loss = _poisson_nll(records, weights, teams, values, regularization)
    return values, loss


def _select_rho(
    records: Sequence[Mapping[str, Any]],
    weights: Sequence[float],
    teams: Sequence[str],
    values: Sequence[float],
    *,
    rho_min: float,
    rho_max: float,
    rho_step: float,
) -> tuple[float, float]:
    count = len(teams)
    team_index = {team: index for index, team in enumerate(teams)}
    attack = values[2 : 2 + count]
    defense = values[2 + count :]
    intercept, home_advantage = values[0], values[1]
    total_weight = sum(weights)

    # The fitted model must define a valid low-score distribution for every
    # legal pairing of known teams, including pairings absent from the training
    # schedule.  Exact maxima over known home/away combinations let each rho
    # candidate be checked once without an O(fixtures * teams^2) inner loop.
    known_rates: list[tuple[float, float]] = []
    for home in range(count):
        for away in range(count):
            if home == away:
                continue
            home_log_rate = (
                intercept
                + home_advantage
                + attack[home]
                + defense[away]
            )
            away_log_rate = intercept + attack[away] + defense[home]
            try:
                home_rate = math.exp(home_log_rate)
                away_rate = math.exp(away_log_rate)
            except OverflowError as exc:
                raise ScoreModelError(
                    "known-team parameters produce an unsafe expected-goals rate"
                ) from exc
            if not math.isfinite(home_rate) or not math.isfinite(away_rate):
                raise ScoreModelError(
                    "known-team parameters produce a non-finite expected-goals rate"
                )
            known_rates.append((home_rate, away_rate))
    maximum_home_rate = max(home_rate for home_rate, _ in known_rates)
    maximum_away_rate = max(away_rate for _, away_rate in known_rates)
    maximum_rate_product = max(
        home_rate * away_rate for home_rate, away_rate in known_rates
    )

    steps = int(math.floor((rho_max - rho_min) / rho_step + 1e-12))
    candidates = [round(rho_min + index * rho_step, 12) for index in range(steps + 1)]
    if not candidates or candidates[-1] < rho_max - 1e-10:
        candidates.append(rho_max)

    scored: list[tuple[float, float, float]] = []
    for rho in candidates:
        domain_taus = (
            1.0 - maximum_rate_product * rho,
            1.0 + maximum_home_rate * rho,
            1.0 + maximum_away_rate * rho,
            1.0 - rho,
        )
        if any(
            not math.isfinite(candidate_tau) or candidate_tau <= 0.0
            for candidate_tau in domain_taus
        ):
            continue
        loss = 0.0
        valid = True
        for row, weight in zip(records, weights):
            home = team_index[row["home_team"]]
            away = team_index[row["away_team"]]
            home_rate = _safe_rate(
                intercept + home_advantage + attack[home] + defense[away]
            )
            away_rate = _safe_rate(intercept + attack[away] + defense[home])
            tau = _dc_tau(
                row["home_goals"],
                row["away_goals"],
                home_rate,
                away_rate,
                rho,
            )
            if tau <= 0.0 or not math.isfinite(tau):
                valid = False
                break
            loss += weight * (
                home_rate
                - row["home_goals"] * math.log(home_rate)
                + math.lgamma(row["home_goals"] + 1)
                + away_rate
                - row["away_goals"] * math.log(away_rate)
                + math.lgamma(row["away_goals"] + 1)
                - math.log(tau)
            )
        if valid:
            normalized_loss = loss / total_weight
            scored.append((normalized_loss, abs(rho), rho))

    if not scored:
        raise ScoreModelError("rho grid has no valid Dixon-Coles correction")
    best_loss, _, best_rho = min(scored)
    return best_rho, best_loss


def fit_model(
    csv_path: str | Path,
    *,
    half_life_days: float = 365.0,
    iterations: int = 1200,
    learning_rate: float = 0.03,
    regularization: float = 0.02,
    rho_min: float = -0.20,
    rho_max: float = 0.20,
    rho_step: float = 0.01,
) -> dict[str, Any]:
    """Fit a deterministic time-decayed Poisson/Dixon-Coles baseline."""

    half_life_days = _require_finite(half_life_days, "half_life_days")
    learning_rate = _require_finite(learning_rate, "learning_rate")
    regularization = _require_finite(regularization, "regularization")
    rho_min = _require_finite(rho_min, "rho_min")
    rho_max = _require_finite(rho_max, "rho_max")
    rho_step = _require_finite(rho_step, "rho_step")
    if half_life_days <= 0.0:
        raise ScoreModelError("half_life_days must be positive")
    if isinstance(iterations, bool) or int(iterations) != iterations or iterations <= 0:
        raise ScoreModelError("iterations must be a positive integer")
    iterations = int(iterations)
    if learning_rate <= 0.0:
        raise ScoreModelError("learning_rate must be positive")
    if regularization < 0.0:
        raise ScoreModelError("regularization cannot be negative")
    if rho_min >= rho_max or rho_step <= 0.0:
        raise ScoreModelError("rho grid requires rho_min < rho_max and rho_step > 0")
    if rho_min <= -1.0 or rho_max >= 1.0:
        raise ScoreModelError("rho grid must stay strictly inside (-1, 1)")

    records = load_training_csv(csv_path)
    records = sorted(
        records,
        key=lambda row: (
            row["date"],
            row["home_team"],
            row["away_team"],
            row["home_goals"],
            row["away_goals"],
        ),
    )
    teams = sorted(
        {row["home_team"] for row in records}
        | {row["away_team"] for row in records}
    )
    reference_date = max(row["date"] for row in records)
    weights = [
        math.exp(
            -math.log(2.0)
            * max(0, (reference_date - row["date"]).days)
            / half_life_days
        )
        for row in records
    ]

    values, poisson_loss = _fit_poisson_parameters(
        records,
        weights,
        teams,
        iterations=iterations,
        learning_rate=learning_rate,
        regularization=regularization,
    )
    rho, dc_loss = _select_rho(
        records,
        weights,
        teams,
        values,
        rho_min=rho_min,
        rho_max=rho_max,
        rho_step=rho_step,
    )

    count = len(teams)
    canonical_rows = _canonical_training_rows(records)
    model: dict[str, Any] = {
        "artifact_type": MODEL_ARTIFACT_TYPE,
        "schema_version": MODEL_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at": _utc_now(),
        "training": {
            "source_data_hash": _sha256_json(canonical_rows),
            "match_count": len(records),
            "team_count": count,
            "start_date": min(row["date"] for row in records).isoformat(),
            "end_date": reference_date.isoformat(),
            "weight_reference_date": reference_date.isoformat(),
            "effective_sample_weight": sum(weights),
        },
        "config": {
            "half_life_days": half_life_days,
            "iterations": iterations,
            "learning_rate": learning_rate,
            "regularization": regularization,
            "rho_grid": {
                "minimum": rho_min,
                "maximum": rho_max,
                "step": rho_step,
            },
        },
        "parameters": {
            "intercept": values[0],
            "home_advantage": values[1],
            "attack": {
                team: values[2 + index] for index, team in enumerate(teams)
            },
            "defense": {
                team: values[2 + count + index]
                for index, team in enumerate(teams)
            },
            "rho": rho,
        },
        "fit": {
            "objective": "weighted_negative_log_likelihood",
            "poisson_nll": poisson_loss,
            "dixon_coles_nll": dc_loss,
            "optimizer": "deterministic_adam_then_rho_grid",
        },
    }
    model["model_hash"] = calculate_model_hash(model)
    validate_model(model)
    return model


def validate_model(model: Mapping[str, Any], *, verify_hash: bool = True) -> None:
    if not isinstance(model, Mapping):
        raise ScoreModelError("model must be a JSON object")
    if model.get("artifact_type") != MODEL_ARTIFACT_TYPE:
        raise ScoreModelError("unexpected model artifact_type")
    if model.get("schema_version") != MODEL_SCHEMA_VERSION:
        raise ScoreModelError("unsupported model schema_version")
    if model.get("model_version") != MODEL_VERSION:
        raise ScoreModelError("unsupported model_version")

    generated_at, _ = _parse_aware_datetime(model.get("generated_at"), "generated_at")
    training = model.get("training")
    if not isinstance(training, Mapping):
        raise ScoreModelError("model training metadata is missing")
    source_hash = training.get("source_data_hash")
    if not isinstance(source_hash, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", source_hash
    ):
        raise ScoreModelError("training.source_data_hash must be a SHA-256 hash")
    match_count = _require_positive_integer(
        training.get("match_count"), "training.match_count", minimum=2
    )
    team_count = _require_positive_integer(
        training.get("team_count"), "training.team_count", minimum=2
    )
    start_date = _parse_iso_date_field(training.get("start_date"), "training.start_date")
    end_date = _parse_iso_date_field(training.get("end_date"), "training.end_date")
    reference_date = _parse_iso_date_field(
        training.get("weight_reference_date"), "training.weight_reference_date"
    )
    if start_date > end_date:
        raise ScoreModelError("training.start_date cannot be after training.end_date")
    if reference_date != end_date:
        raise ScoreModelError("training.weight_reference_date must equal training.end_date")
    if end_date > generated_at.date():
        raise ScoreModelError("training.end_date cannot be after model generated_at")
    effective_weight = _require_finite(
        training.get("effective_sample_weight"),
        "training.effective_sample_weight",
    )
    if effective_weight <= 0.0 or effective_weight > match_count + 1e-9:
        raise ScoreModelError(
            "training.effective_sample_weight must be in (0, match_count]"
        )

    config = model.get("config")
    if not isinstance(config, Mapping):
        raise ScoreModelError("model config is missing")
    half_life_days = _require_finite(
        config.get("half_life_days"), "config.half_life_days"
    )
    if half_life_days <= 0.0:
        raise ScoreModelError("config.half_life_days must be positive")
    _require_positive_integer(config.get("iterations"), "config.iterations")
    learning_rate = _require_finite(
        config.get("learning_rate"), "config.learning_rate"
    )
    regularization = _require_finite(
        config.get("regularization"), "config.regularization"
    )
    if learning_rate <= 0.0:
        raise ScoreModelError("config.learning_rate must be positive")
    if regularization < 0.0:
        raise ScoreModelError("config.regularization cannot be negative")
    rho_grid = config.get("rho_grid")
    if not isinstance(rho_grid, Mapping):
        raise ScoreModelError("config.rho_grid is missing")
    rho_minimum = _require_finite(
        rho_grid.get("minimum"), "config.rho_grid.minimum"
    )
    rho_maximum = _require_finite(
        rho_grid.get("maximum"), "config.rho_grid.maximum"
    )
    rho_step = _require_finite(rho_grid.get("step"), "config.rho_grid.step")
    if (
        not -1.0 < rho_minimum < rho_maximum < 1.0
        or rho_step <= 0.0
    ):
        raise ScoreModelError("config.rho_grid has invalid bounds or step")

    parameters = model.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ScoreModelError("model parameters are missing")
    attack = parameters.get("attack")
    defense = parameters.get("defense")
    if not isinstance(attack, Mapping) or not isinstance(defense, Mapping):
        raise ScoreModelError("attack and defense parameters must be objects")
    if set(attack) != set(defense) or len(attack) < 2:
        raise ScoreModelError("attack and defense team sets must match")
    if len(attack) != team_count:
        raise ScoreModelError("training.team_count does not match model parameters")
    for name in ("intercept", "home_advantage", "rho"):
        _require_finite(parameters.get(name), f"parameters.{name}")
    rho = float(parameters["rho"])
    if not -1.0 < rho < 1.0:
        raise ScoreModelError("parameters.rho must be inside (-1, 1)")
    if rho < rho_minimum - 1e-12 or rho > rho_maximum + 1e-12:
        raise ScoreModelError("parameters.rho is outside config.rho_grid")
    for group_name, group in (("attack", attack), ("defense", defense)):
        for team, value in group.items():
            if not isinstance(team, str) or not team.strip():
                raise ScoreModelError(f"parameters.{group_name} has an invalid team")
            _require_finite(value, f"parameters.{group_name}.{team}")
    fit = model.get("fit")
    if not isinstance(fit, Mapping):
        raise ScoreModelError("model fit metadata is missing")
    if fit.get("objective") != "weighted_negative_log_likelihood":
        raise ScoreModelError("fit.objective is unsupported")
    if fit.get("optimizer") != "deterministic_adam_then_rho_grid":
        raise ScoreModelError("fit.optimizer is unsupported")
    _require_finite(fit.get("poisson_nll"), "fit.poisson_nll")
    _require_finite(fit.get("dixon_coles_nll"), "fit.dixon_coles_nll")
    if verify_hash:
        stored_hash = model.get("model_hash")
        if not isinstance(stored_hash, str) or stored_hash != calculate_model_hash(model):
            raise ScoreModelError("model_hash does not match model contents")


def save_json(value: Mapping[str, Any], path: str | Path | None) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
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
        raise ScoreModelError(f"cannot read model JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ScoreModelError("model JSON must contain an object")
    validate_model(raw)
    return raw


def _team_strength(
    group: Mapping[str, Any],
    team: str,
    *,
    policy: str,
    warnings: list[str],
) -> float:
    if team in group:
        return _require_finite(group[team], f"team parameter {team}")
    if policy != "league_average":
        raise ScoreModelError(
            f"unknown team {team!r}; use unknown_team_policy='league_average' "
            "explicitly to fall back"
        )
    warning = f"unknown team {team!r} explicitly replaced by league-average attack/defense"
    if warning not in warnings:
        warnings.append(warning)
    return 0.0


def expected_rates(
    model: Mapping[str, Any],
    home_team: str,
    away_team: str,
    *,
    unknown_team_policy: str = "error",
) -> tuple[float, float, list[str]]:
    validate_model(model)
    if not isinstance(home_team, str) or not home_team.strip():
        raise ScoreModelError("home_team is required")
    if not isinstance(away_team, str) or not away_team.strip():
        raise ScoreModelError("away_team is required")
    if home_team == away_team:
        raise ScoreModelError("home_team and away_team must differ")
    if unknown_team_policy not in {"error", "league_average"}:
        raise ScoreModelError("unknown_team_policy must be error or league_average")

    parameters = model["parameters"]
    attack = parameters["attack"]
    defense = parameters["defense"]
    warnings: list[str] = []
    home_attack = _team_strength(
        attack, home_team, policy=unknown_team_policy, warnings=warnings
    )
    home_defense = _team_strength(
        defense, home_team, policy=unknown_team_policy, warnings=warnings
    )
    away_attack = _team_strength(
        attack, away_team, policy=unknown_team_policy, warnings=warnings
    )
    away_defense = _team_strength(
        defense, away_team, policy=unknown_team_policy, warnings=warnings
    )
    home_log_rate = (
        _require_finite(parameters["intercept"], "parameters.intercept")
        + _require_finite(parameters["home_advantage"], "parameters.home_advantage")
        + home_attack
        + away_defense
    )
    away_log_rate = (
        _require_finite(parameters["intercept"], "parameters.intercept")
        + away_attack
        + home_defense
    )
    if home_log_rate > 20.0 or away_log_rate > 20.0:
        raise ScoreModelError("model produces an unsafe expected-goals rate")
    home_rate = math.exp(home_log_rate)
    away_rate = math.exp(away_log_rate)
    if (
        not math.isfinite(home_rate)
        or not math.isfinite(away_rate)
        or home_rate < 0.0
        or away_rate < 0.0
    ):
        raise ScoreModelError("model expected-goals rates are invalid")
    return home_rate, away_rate, warnings


def _poisson_probabilities(rate: float, maximum: int) -> list[float]:
    if not math.isfinite(rate) or rate < 0.0:
        raise ScoreModelError("Poisson rate must be finite and non-negative")
    probabilities = [math.exp(-rate)]
    for goals in range(1, maximum + 1):
        probabilities.append(probabilities[-1] * rate / goals)
    return probabilities


def build_score_matrix(
    home_rate: float,
    away_rate: float,
    rho: float,
    *,
    max_goals: int = 8,
    hard_max_goals: int = 30,
    tail_tolerance: float = 1e-8,
    allow_large_tail: bool = False,
) -> tuple[list[list[float]], dict[str, Any]]:
    """Build and normalize a finite Dixon-Coles matrix with a tail audit."""

    home_rate = _require_finite(home_rate, "home_rate")
    away_rate = _require_finite(away_rate, "away_rate")
    rho = _require_finite(rho, "rho")
    tail_tolerance = _require_finite(tail_tolerance, "tail_tolerance")
    if home_rate < 0.0 or away_rate < 0.0:
        raise ScoreModelError("expected-goals rates cannot be negative")
    if not -1.0 < rho < 1.0:
        raise ScoreModelError("rho must be inside (-1, 1)")
    if isinstance(max_goals, bool) or int(max_goals) != max_goals or max_goals < 1:
        raise ScoreModelError("max_goals must be an integer of at least 1")
    if (
        isinstance(hard_max_goals, bool)
        or int(hard_max_goals) != hard_max_goals
        or hard_max_goals < max_goals
    ):
        raise ScoreModelError("hard_max_goals must be an integer >= max_goals")
    if not 0.0 < tail_tolerance < 1.0:
        raise ScoreModelError("tail_tolerance must be between zero and one")
    max_goals = int(max_goals)
    hard_max_goals = int(hard_max_goals)

    tau_values = [
        _dc_tau(h, a, home_rate, away_rate, rho)
        for h, a in ((0, 0), (0, 1), (1, 0), (1, 1))
    ]
    if any(not math.isfinite(value) or value <= 0.0 for value in tau_values):
        raise ScoreModelError(
            "Dixon-Coles rho produces a non-positive low-score probability"
        )

    raw_matrix: list[list[float]] = []
    captured_mass = 0.0
    selected_maximum = max_goals
    while True:
        home_probabilities = _poisson_probabilities(home_rate, selected_maximum)
        away_probabilities = _poisson_probabilities(away_rate, selected_maximum)
        raw_matrix = []
        for home_goals, home_probability in enumerate(home_probabilities):
            row: list[float] = []
            for away_goals, away_probability in enumerate(away_probabilities):
                probability = (
                    home_probability
                    * away_probability
                    * _dc_tau(
                        home_goals,
                        away_goals,
                        home_rate,
                        away_rate,
                        rho,
                    )
                )
                if not math.isfinite(probability) or probability < 0.0:
                    raise ScoreModelError("score matrix contains an invalid probability")
                row.append(probability)
            raw_matrix.append(row)
        captured_mass = math.fsum(math.fsum(row) for row in raw_matrix)
        if not math.isfinite(captured_mass) or captured_mass <= 0.0:
            raise ScoreModelError("score matrix has no finite probability mass")
        if captured_mass > 1.0 + 1e-10:
            raise ScoreModelError("score matrix captured mass exceeds one")
        omitted_mass = max(0.0, 1.0 - captured_mass)
        if omitted_mass <= tail_tolerance or selected_maximum >= hard_max_goals:
            break
        selected_maximum += 1

    omitted_mass = max(0.0, 1.0 - captured_mass)
    tolerance_met = omitted_mass <= tail_tolerance
    if not tolerance_met and not allow_large_tail:
        raise ScoreModelError(
            "score tail exceeds tolerance at hard_max_goals; increase the limit "
            "or explicitly allow it"
        )
    matrix = [
        [probability / captured_mass for probability in row] for row in raw_matrix
    ]
    normalized_mass = math.fsum(math.fsum(row) for row in matrix)
    if abs(normalized_mass - 1.0) > 1e-12:
        raise ScoreModelError("normalized score matrix does not sum to one")
    tail = {
        "raw_omitted_probability": omitted_mass,
        "captured_probability_before_normalization": captured_mass,
        "normalization_factor": 1.0 / captured_mass,
        "tolerance": tail_tolerance,
        "tolerance_met": tolerance_met,
        "truncated_at_home_goals": selected_maximum,
        "truncated_at_away_goals": selected_maximum,
        "renormalized": True,
    }
    return matrix, tail


def _validate_matrix(matrix: Sequence[Sequence[float]]) -> None:
    if not isinstance(matrix, Sequence) or not matrix:
        raise ScoreModelError("score matrix must be non-empty")
    width: int | None = None
    total = 0.0
    for row_index, row in enumerate(matrix):
        if not isinstance(row, Sequence) or not row:
            raise ScoreModelError(f"score matrix row {row_index} is empty")
        if width is None:
            width = len(row)
        elif len(row) != width:
            raise ScoreModelError("score matrix must be rectangular")
        for column_index, raw in enumerate(row):
            probability = _require_finite(
                raw, f"score_matrix[{row_index}][{column_index}]"
            )
            if probability < 0.0:
                raise ScoreModelError("score matrix probabilities cannot be negative")
            total += probability
    if abs(total - 1.0) > 1e-9:
        raise ScoreModelError("score matrix probabilities must sum to one")


def aggregate_one_x_two(matrix: Sequence[Sequence[float]]) -> dict[str, float]:
    _validate_matrix(matrix)
    result = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            if home_goals > away_goals:
                result["home"] += probability
            elif home_goals == away_goals:
                result["draw"] += probability
            else:
                result["away"] += probability
    return result


def aggregate_btts(matrix: Sequence[Sequence[float]]) -> dict[str, float]:
    _validate_matrix(matrix)
    yes = math.fsum(
        probability
        for home_goals, row in enumerate(matrix)
        for away_goals, probability in enumerate(row)
        if home_goals >= 1 and away_goals >= 1
    )
    return {"yes": yes, "no": 1.0 - yes}


def aggregate_goal_ranges(matrix: Sequence[Sequence[float]]) -> dict[str, float]:
    _validate_matrix(matrix)
    ranges = {"0-1": 0.0, "2-3": 0.0, "4-6": 0.0, "7+": 0.0}
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            goals = home_goals + away_goals
            if goals <= 1:
                ranges["0-1"] += probability
            elif goals <= 3:
                ranges["2-3"] += probability
            elif goals <= 6:
                ranges["4-6"] += probability
            else:
                ranges["7+"] += probability
    return ranges


def rank_exact_scores(
    matrix: Sequence[Sequence[float]], *, limit: int = 10
) -> dict[str, Any]:
    _validate_matrix(matrix)
    if isinstance(limit, bool) or int(limit) != limit or limit < 2:
        raise ScoreModelError("exact-score limit must be an integer of at least two")
    ranked = sorted(
        (
            {
                "score": f"{home_goals}-{away_goals}",
                "home_goals": home_goals,
                "away_goals": away_goals,
                "probability": probability,
            }
            for home_goals, row in enumerate(matrix)
            for away_goals, probability in enumerate(row)
        ),
        key=lambda item: (
            -item["probability"],
            item["home_goals"],
            item["away_goals"],
        ),
    )
    zero_zero_rank = next(
        index for index, item in enumerate(ranked, start=1) if item["score"] == "0-0"
    )
    zero_zero_probability = matrix[0][0]
    return {
        "top_two": ranked[:2],
        "ranked": ranked[: int(limit)],
        "zero_zero_audit": {
            "score": "0-0",
            "probability": zero_zero_probability,
            "rank": zero_zero_rank,
            "included_in_top_two": zero_zero_rank <= 2,
        },
    }


def _split_quarter_line(line: float) -> list[float]:
    line = _require_finite(line, "line")
    quarter_units = round(line * 4.0)
    if abs(line * 4.0 - quarter_units) > 1e-8:
        raise ScoreModelError("line must be a multiple of 0.25")
    if quarter_units % 2 == 0:
        return [quarter_units / 4.0]
    return [(quarter_units - 1) / 4.0, (quarter_units + 1) / 4.0]


def _component_outcome(value: float) -> str:
    if value > 1e-12:
        return "win"
    if value < -1e-12:
        return "loss"
    return "push"


def _combined_settlement_state(outcomes: Sequence[str]) -> str:
    if len(outcomes) == 1:
        return {"win": "full_win", "push": "push", "loss": "full_loss"}[
            outcomes[0]
        ]
    ordered = sorted(outcomes)
    mapping = {
        ("win", "win"): "full_win",
        ("push", "win"): "half_win",
        ("push", "push"): "push",
        ("loss", "push"): "half_loss",
        ("loss", "loss"): "full_loss",
    }
    key = tuple(ordered)
    if key not in mapping:
        raise ScoreModelError("split line produced contradictory win/loss components")
    return mapping[key]


def _settlement_distribution(
    matrix: Sequence[Sequence[float]],
    line: float,
    value_for_score: Any,
) -> tuple[list[float], dict[str, float]]:
    _validate_matrix(matrix)
    split_lines = _split_quarter_line(line)
    probabilities = {state: 0.0 for state in SETTLEMENT_STATES}
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            outcomes = [
                _component_outcome(value_for_score(home_goals, away_goals, part))
                for part in split_lines
            ]
            state = _combined_settlement_state(outcomes)
            probabilities[state] += probability
    if abs(math.fsum(probabilities.values()) - 1.0) > 1e-9:
        raise ScoreModelError("settlement probabilities do not sum to one")
    return split_lines, probabilities


def aggregate_total(
    matrix: Sequence[Sequence[float]], side: str, line: float
) -> dict[str, Any]:
    side = (side or "").lower()
    if side not in {"over", "under"}:
        raise ScoreModelError("total side must be over or under")

    def value(home_goals: int, away_goals: int, component: float) -> float:
        total = home_goals + away_goals
        return total - component if side == "over" else component - total

    split_lines, probabilities = _settlement_distribution(matrix, line, value)
    return {
        "side": side,
        "line": float(line),
        "split_lines": split_lines,
        "probabilities": probabilities,
    }


def aggregate_asian_handicap(
    matrix: Sequence[Sequence[float]], side: str, line: float
) -> dict[str, Any]:
    """Aggregate a handicap applied from the selected team's perspective.

    Example: ``side='home', line=-0.75`` prices the home team giving 0.75.
    ``side='away', line=0.75`` is the corresponding away selection.
    """

    side = (side or "").lower()
    if side not in {"home", "away"}:
        raise ScoreModelError("Asian handicap side must be home or away")

    def value(home_goals: int, away_goals: int, component: float) -> float:
        margin = (
            home_goals - away_goals if side == "home" else away_goals - home_goals
        )
        return margin + component

    split_lines, probabilities = _settlement_distribution(matrix, line, value)
    return {
        "side": side,
        "line": float(line),
        "split_lines": split_lines,
        "probabilities": probabilities,
    }


def _matrix_expected_goals(matrix: Sequence[Sequence[float]]) -> dict[str, float]:
    _validate_matrix(matrix)
    home = math.fsum(
        home_goals * probability
        for home_goals, row in enumerate(matrix)
        for probability in row
    )
    away = math.fsum(
        away_goals * probability
        for row in matrix
        for away_goals, probability in enumerate(row)
    )
    return {"home": home, "away": away, "total": home + away}


def predict_model(
    model: Mapping[str, Any],
    home_team: str,
    away_team: str,
    *,
    kickoff: str | datetime,
    generated_at: str | datetime | None = None,
    total_markets: Iterable[tuple[str, float]] = (),
    asian_handicaps: Iterable[tuple[str, float]] = (),
    max_goals: int = 8,
    hard_max_goals: int = 30,
    tail_tolerance: float = 1e-8,
    allow_large_tail: bool = False,
    unknown_team_policy: str = "error",
) -> dict[str, Any]:
    validate_model(model)
    kickoff_datetime, canonical_kickoff = _parse_aware_datetime(kickoff, "kickoff")
    prediction_datetime, canonical_generated_at = _parse_aware_datetime(
        generated_at if generated_at is not None else _utc_now(),
        "generated_at",
    )
    if prediction_datetime >= kickoff_datetime:
        raise ScoreModelError("generated_at must be strictly before kickoff")
    training_end = _parse_iso_date_field(
        model["training"]["end_date"], "training.end_date"
    )
    if training_end >= kickoff_datetime.date():
        raise ScoreModelError(
            "training.end_date must be strictly before kickoff's UTC date"
        )
    if prediction_datetime.date() < training_end:
        raise ScoreModelError("generated_at cannot be before training.end_date")
    model_generated_at, _ = _parse_aware_datetime(
        model["generated_at"], "model.generated_at"
    )
    if prediction_datetime < model_generated_at:
        raise ScoreModelError("generated_at cannot be before model.generated_at")

    home_rate, away_rate, warnings = expected_rates(
        model,
        home_team,
        away_team,
        unknown_team_policy=unknown_team_policy,
    )
    rho = _require_finite(model["parameters"]["rho"], "parameters.rho")
    matrix, tail = build_score_matrix(
        home_rate,
        away_rate,
        rho,
        max_goals=max_goals,
        hard_max_goals=hard_max_goals,
        tail_tolerance=tail_tolerance,
        allow_large_tail=allow_large_tail,
    )
    if tail["raw_omitted_probability"] > 0.0:
        warnings.append(
            "finite score grid was normalized after retaining the reported tail audit"
        )
    totals: dict[str, Any] = {}
    for side, line in total_markets:
        result = aggregate_total(matrix, side, line)
        key = f"{result['side']}_{result['line']:+g}"
        if key in totals:
            raise ScoreModelError(f"duplicate total market: {key}")
        totals[key] = result
    handicaps: dict[str, Any] = {}
    for side, line in asian_handicaps:
        result = aggregate_asian_handicap(matrix, side, line)
        key = f"{result['side']}_{result['line']:+g}"
        if key in handicaps:
            raise ScoreModelError(f"duplicate Asian handicap: {key}")
        handicaps[key] = result

    prediction = {
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
        },
        "provenance": {
            "model_schema_version": model["schema_version"],
            "training": {
                "source_data_hash": model["training"]["source_data_hash"],
                "match_count": model["training"]["match_count"],
                "start_date": model["training"]["start_date"],
                "end_date": model["training"]["end_date"],
            },
            "training_cutoff_date": training_end.isoformat(),
            "strictly_before_kickoff_utc_date": True,
            "generated_before_kickoff": True,
        },
        "latent_rates": {"home": home_rate, "away": away_rate},
        "expected_goals": _matrix_expected_goals(matrix),
        "score_matrix": {
            "home_goals_max": len(matrix) - 1,
            "away_goals_max": len(matrix[0]) - 1,
            "probabilities": matrix,
        },
        "tail_mass": tail,
        "one_x_two": aggregate_one_x_two(matrix),
        "btts": aggregate_btts(matrix),
        "goal_ranges": aggregate_goal_ranges(matrix),
        "exact_scores": rank_exact_scores(matrix),
        "totals": totals,
        "asian_handicaps": handicaps,
        "warnings": warnings,
    }
    # A final canonical serialization catches NaN/Infinity before archival.
    _sha256_json(prediction)
    return prediction


def _write_training_subset(
    path: Path, records: Sequence[Mapping[str, Any]]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["date", "home_team", "away_team", "home_goals", "away_goals"]
        )
        for row in records:
            writer.writerow(
                [
                    row["date"].isoformat(),
                    row["home_team"],
                    row["away_team"],
                    row["home_goals"],
                    row["away_goals"],
                ]
            )


def _actual_result(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home"
    if home_goals < away_goals:
        return "away"
    return "draw"


def _actual_goal_band(home_goals: int, away_goals: int) -> str:
    total = home_goals + away_goals
    if total <= 1:
        return "0-1"
    if total <= 3:
        return "2-3"
    return "4+"


def backtest_model(
    csv_path: str | Path,
    *,
    min_train_matches: int,
    test_block_size: int,
    half_life_days: float = 365.0,
    iterations: int = 1200,
    learning_rate: float = 0.03,
    regularization: float = 0.02,
    rho_min: float = -0.20,
    rho_max: float = 0.20,
    rho_step: float = 0.01,
    max_goals: int = 12,
    hard_max_goals: int = 30,
    tail_tolerance: float = 1e-8,
    unknown_team_policy: str = "error",
) -> dict[str, Any]:
    """Run a deterministic expanding-window, date-grouped walk-forward test."""

    min_train_matches = _require_positive_integer(
        min_train_matches, "min_train_matches", minimum=2
    )
    test_block_size = _require_positive_integer(
        test_block_size, "test_block_size"
    )
    if unknown_team_policy not in {"error", "league_average"}:
        raise ScoreModelError("unknown_team_policy must be error or league_average")

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
        raise ScoreModelError(
            "not enough later matches remain after the minimum training window"
        )

    epsilon = 1e-15
    forecast_rows: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    one_x_two_log_losses: list[float] = []
    one_x_two_brier_scores: list[float] = []
    exact_score_log_losses: list[float] = []
    goal_band_rps_scores: list[float] = []

    with tempfile.TemporaryDirectory(prefix="soccer-score-backtest-") as temporary:
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
                group = date_groups[group_cursor]
                block_groups.append(group)
                block_match_count += len(group[1])
                group_cursor += 1

            test_start_date = block_groups[0][0]
            test_end_date = block_groups[-1][0]
            training_records = [
                row for row in records if row["date"] < test_start_date
            ]
            test_records = [row for _, group in block_groups for row in group]
            if len(training_records) < min_train_matches:
                raise ScoreModelError("walk-forward training window is unexpectedly short")
            if any(row["date"] >= test_start_date for row in training_records):
                raise ScoreModelError("walk-forward cutoff leaked a test date")

            subset_path = temporary_path / f"train-{block_number}.csv"
            _write_training_subset(subset_path, training_records)
            try:
                model = fit_model(
                    subset_path,
                    half_life_days=half_life_days,
                    iterations=iterations,
                    learning_rate=learning_rate,
                    regularization=regularization,
                    rho_min=rho_min,
                    rho_max=rho_max,
                    rho_step=rho_step,
                )
            except ScoreModelError as exc:
                raise ScoreModelError(
                    f"walk-forward block {block_number} cannot fit safely: {exc}"
                ) from exc

            cutoff_date = model["training"]["end_date"]
            prediction_generated_at = test_start_date.isoformat() + "T00:00:00Z"
            # The model is being reconstructed as it would have existed at this
            # historical cutoff.  generated_at is excluded from model_hash, so
            # this explicit as-of timestamp preserves deterministic parameters
            # while satisfying the same provenance boundary as live prediction.
            model = dict(model)
            model["generated_at"] = prediction_generated_at
            validate_model(model)
            block_entry = {
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
            blocks.append(block_entry)

            for row in test_records:
                # Historical CSVs have date precision only.  The entire date is
                # kept in the test block, prediction is timestamped at 00:00Z,
                # and the synthetic cutoff fixture time is at the end of that
                # same UTC date.  It is provenance metadata, not a claimed real
                # kickoff time.
                date_only_cutoff = row["date"].isoformat() + "T23:59:59Z"
                prediction = predict_model(
                    model,
                    row["home_team"],
                    row["away_team"],
                    kickoff=date_only_cutoff,
                    generated_at=prediction_generated_at,
                    max_goals=max_goals,
                    hard_max_goals=hard_max_goals,
                    tail_tolerance=tail_tolerance,
                    unknown_team_policy=unknown_team_policy,
                )
                actual_result = _actual_result(
                    row["home_goals"], row["away_goals"]
                )
                one_x_two = prediction["one_x_two"]
                actual_result_probability = max(one_x_two[actual_result], epsilon)
                one_x_two_log_loss = -math.log(actual_result_probability)
                one_x_two_brier = math.fsum(
                    (
                        one_x_two[result]
                        - (1.0 if result == actual_result else 0.0)
                    )
                    ** 2
                    for result in ("home", "draw", "away")
                )

                matrix = prediction["score_matrix"]["probabilities"]
                if (
                    row["home_goals"] < len(matrix)
                    and row["away_goals"] < len(matrix[0])
                ):
                    actual_score_probability = matrix[row["home_goals"]][
                        row["away_goals"]
                    ]
                    actual_score_in_grid = True
                else:
                    actual_score_probability = 0.0
                    actual_score_in_grid = False
                exact_score_log_loss = -math.log(
                    max(actual_score_probability, epsilon)
                )

                ranges = prediction["goal_ranges"]
                goal_bands = {
                    "0-1": ranges["0-1"],
                    "2-3": ranges["2-3"],
                    "4+": ranges["4-6"] + ranges["7+"],
                }
                actual_band = _actual_goal_band(
                    row["home_goals"], row["away_goals"]
                )
                predicted_cumulative = (
                    goal_bands["0-1"],
                    goal_bands["0-1"] + goal_bands["2-3"],
                )
                actual_cumulative = (
                    1.0 if actual_band == "0-1" else 0.0,
                    0.0 if actual_band == "4+" else 1.0,
                )
                # Normalized ordered RPS for three classes: mean squared CDF
                # error at the two internal category boundaries.
                goal_band_rps = math.fsum(
                    (predicted - observed) ** 2
                    for predicted, observed in zip(
                        predicted_cumulative, actual_cumulative
                    )
                ) / 2.0

                one_x_two_log_losses.append(one_x_two_log_loss)
                one_x_two_brier_scores.append(one_x_two_brier)
                exact_score_log_losses.append(exact_score_log_loss)
                goal_band_rps_scores.append(goal_band_rps)
                forecast_rows.append(
                    {
                        "block": block_number,
                        "date": row["date"].isoformat(),
                        "home_team": row["home_team"],
                        "away_team": row["away_team"],
                        "actual_score": f"{row['home_goals']}-{row['away_goals']}",
                        "actual_result": actual_result,
                        "actual_goal_band": actual_band,
                        "model_hash": model["model_hash"],
                        "model_version": model["model_version"],
                        "training_cutoff_date": cutoff_date,
                        "prediction_generated_at": prediction_generated_at,
                        "date_only_fixture_cutoff": date_only_cutoff,
                        "prediction": {
                            "one_x_two": one_x_two,
                            "goal_bands": goal_bands,
                            "expected_goals": prediction["expected_goals"],
                            "exact_score_top_two": prediction["exact_scores"][
                                "top_two"
                            ],
                            "actual_score_probability": actual_score_probability,
                            "actual_score_in_grid": actual_score_in_grid,
                            "score_matrix_hash": _sha256_json(
                                prediction["score_matrix"]
                            ),
                            "tail_mass": prediction["tail_mass"],
                            "warnings": prediction["warnings"],
                        },
                        "scores": {
                            "one_x_two_log_loss": one_x_two_log_loss,
                            "one_x_two_brier": one_x_two_brier,
                            "exact_score_log_loss": exact_score_log_loss,
                            "goal_band_ordered_rps": goal_band_rps,
                        },
                    }
                )

    sample_count = len(forecast_rows)
    if sample_count == 0:
        raise ScoreModelError("walk-forward produced no test predictions")
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
            "date_only_evaluation_convention": (
                "prediction at 00:00Z; synthetic fixture cutoff 23:59:59Z"
            ),
        },
        "fit_config": {
            "half_life_days": half_life_days,
            "iterations": iterations,
            "learning_rate": learning_rate,
            "regularization": regularization,
            "rho_grid": {
                "minimum": rho_min,
                "maximum": rho_max,
                "step": rho_step,
            },
            "max_goals": max_goals,
            "hard_max_goals": hard_max_goals,
            "tail_tolerance": tail_tolerance,
            "unknown_team_policy": unknown_team_policy,
        },
        "blocks": blocks,
        "predictions": forecast_rows,
        "metrics": {
            "sample_count": sample_count,
            "one_x_two_multiclass_log_loss": math.fsum(
                one_x_two_log_losses
            )
            / sample_count,
            "one_x_two_multiclass_brier": math.fsum(
                one_x_two_brier_scores
            )
            / sample_count,
            "exact_score_log_loss": math.fsum(exact_score_log_losses)
            / sample_count,
            "goal_band_ordered_rps": math.fsum(goal_band_rps_scores)
            / sample_count,
            "definitions": {
                "one_x_two_multiclass_brier": (
                    "sum of squared error over home/draw/away, averaged by match"
                ),
                "goal_band_ordered_rps": (
                    "mean squared CDF error at the two boundaries of "
                    "0-1, 2-3, 4+ goals"
                ),
                "log_loss_floor": epsilon,
            },
        },
    }
    artifact["backtest_hash"] = _sha256_json(artifact)
    return artifact


def _parse_market_spec(raw: str, *, market: str) -> tuple[str, float]:
    try:
        side, line_raw = raw.rsplit(":", 1)
    except ValueError as exc:
        raise ScoreModelError(
            f"{market} must use SIDE:LINE, for example over:2.25"
        ) from exc
    side = side.strip().lower()
    line = _require_finite(line_raw, f"{market} line")
    _split_quarter_line(line)
    if market == "total" and side not in {"over", "under"}:
        raise ScoreModelError("total side must be over or under")
    if market == "asian" and side not in {"home", "away"}:
        raise ScoreModelError("Asian side must be home or away")
    return side, line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train and use a deterministic Dixon-Coles football score model"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fit = subparsers.add_parser("fit", help="fit a model from historical scores")
    fit.add_argument("--input", required=True, help="historical CSV")
    fit.add_argument("--output", required=True, help="model JSON output")
    fit.add_argument("--half-life-days", type=float, default=365.0)
    fit.add_argument("--iterations", type=int, default=1200)
    fit.add_argument("--learning-rate", type=float, default=0.03)
    fit.add_argument("--regularization", type=float, default=0.02)
    fit.add_argument("--rho-min", type=float, default=-0.20)
    fit.add_argument("--rho-max", type=float, default=0.20)
    fit.add_argument("--rho-step", type=float, default=0.01)

    backtest = subparsers.add_parser(
        "backtest",
        help="run deterministic expanding-window walk-forward evaluation",
    )
    backtest.add_argument("--input", required=True, help="historical CSV")
    backtest.add_argument("--output", required=True, help="backtest JSON output")
    backtest.add_argument("--min-train-matches", required=True, type=int)
    backtest.add_argument("--test-block-size", required=True, type=int)
    backtest.add_argument("--half-life-days", type=float, default=365.0)
    backtest.add_argument("--iterations", type=int, default=1200)
    backtest.add_argument("--learning-rate", type=float, default=0.03)
    backtest.add_argument("--regularization", type=float, default=0.02)
    backtest.add_argument("--rho-min", type=float, default=-0.20)
    backtest.add_argument("--rho-max", type=float, default=0.20)
    backtest.add_argument("--rho-step", type=float, default=0.01)
    backtest.add_argument("--max-goals", type=int, default=12)
    backtest.add_argument("--hard-max-goals", type=int, default=30)
    backtest.add_argument("--tail-tolerance", type=float, default=1e-8)
    backtest.add_argument(
        "--unknown-team-policy",
        choices=("error", "league_average"),
        default="error",
    )

    predict = subparsers.add_parser(
        "predict", help="create one canonical score-distribution artifact"
    )
    predict.add_argument("--model", required=True, help="model JSON")
    predict.add_argument("--home-team", required=True)
    predict.add_argument("--away-team", required=True)
    predict.add_argument(
        "--kickoff",
        required=True,
        help="timezone-aware ISO-8601 kickoff; stored canonically in UTC",
    )
    predict.add_argument("--output", help="prediction JSON; stdout when omitted")
    predict.add_argument(
        "--total",
        action="append",
        default=[],
        metavar="SIDE:LINE",
        help="derive a total, for example over:2.25 (repeatable)",
    )
    predict.add_argument(
        "--asian",
        action="append",
        default=[],
        metavar="SIDE:LINE",
        help="derive a selected-side handicap, for example home:-0.75 (repeatable)",
    )
    predict.add_argument("--max-goals", type=int, default=8)
    predict.add_argument("--hard-max-goals", type=int, default=30)
    predict.add_argument("--tail-tolerance", type=float, default=1e-8)
    predict.add_argument("--allow-large-tail", action="store_true")
    predict.add_argument(
        "--unknown-team-policy",
        choices=("error", "league_average"),
        default="error",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "fit":
            model = fit_model(
                args.input,
                half_life_days=args.half_life_days,
                iterations=args.iterations,
                learning_rate=args.learning_rate,
                regularization=args.regularization,
                rho_min=args.rho_min,
                rho_max=args.rho_max,
                rho_step=args.rho_step,
            )
            save_json(model, args.output)
            return 0

        if args.command == "backtest":
            artifact = backtest_model(
                args.input,
                min_train_matches=args.min_train_matches,
                test_block_size=args.test_block_size,
                half_life_days=args.half_life_days,
                iterations=args.iterations,
                learning_rate=args.learning_rate,
                regularization=args.regularization,
                rho_min=args.rho_min,
                rho_max=args.rho_max,
                rho_step=args.rho_step,
                max_goals=args.max_goals,
                hard_max_goals=args.hard_max_goals,
                tail_tolerance=args.tail_tolerance,
                unknown_team_policy=args.unknown_team_policy,
            )
            save_json(artifact, args.output)
            return 0

        model = load_model(args.model)
        totals = [_parse_market_spec(raw, market="total") for raw in args.total]
        handicaps = [_parse_market_spec(raw, market="asian") for raw in args.asian]
        prediction = predict_model(
            model,
            args.home_team,
            args.away_team,
            kickoff=args.kickoff,
            generated_at=_utc_now(),
            total_markets=totals,
            asian_handicaps=handicaps,
            max_goals=args.max_goals,
            hard_max_goals=args.hard_max_goals,
            tail_tolerance=args.tail_tolerance,
            allow_large_tail=args.allow_large_tail,
            unknown_team_policy=args.unknown_team_policy,
        )
        save_json(prediction, args.output)
        return 0
    except ScoreModelError as exc:
        parser.exit(2, f"error: {exc}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

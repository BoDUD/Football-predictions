#!/usr/bin/env python3
"""Rank current full-time corner markets from a registered corner prediction.

The ranker accepts only predictions bound to ``corner_model_manager``.  It
recalculates split-line expected value from the model's five settlement states
and removes margin from a complete two-way current market before calculating
edge.  Diagnostic qualification requires at least three firms, medium/high
data quality, a strictly pre-kickoff snapshot, and structured independent
corner-profile evidence.

Deployment authority is inherited, never invented here.  In particular, the
current registered manager sets both formal corner flags to false, so even a
diagnostically attractive price is emitted as an observation and can never be
made a formal primary by this ranker.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # Imported from the repository root.
    from scripts import corner_model, corner_model_manager
except ImportError:  # Invoked directly as scripts/corner_ranker.py.
    import corner_model  # type: ignore[no-redef]
    import corner_model_manager  # type: ignore[no-redef]


RANKING_ARTIFACT_TYPE = "soccer_corner_market_ranking"
RANKING_SCHEMA_VERSION = "2.0.0"
RANKER_VERSION = "corner-current-market-ranker/2.0.0"
SELECTION_POLICY_VERSION = "corner-current-market-ranking/2.0.0"
MINIMUM_FIRMS = 3
ADVERSE_MINIMUM_FIRMS = 5
ADVERSE_MINIMUM_EV = 0.08
ADVERSE_MINIMUM_EDGE_PP = 4.0
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

MARKETS = ("corner_total", "corner_handicap")
MARKET_SIDES = {
    "corner_total": ("over", "under"),
    "corner_handicap": ("home", "away"),
}
PREDICTION_MARKET_FIELDS = {
    "corner_total": "corner_totals",
    "corner_handicap": "corner_handicaps",
}
FORMAL_FLAG_FIELDS = {
    "corner_total": "formal_corner_total_eligible",
    "corner_handicap": "formal_corner_handicap_eligible",
}
MARKET_SIGNALS = ("aligned", "neutral", "against", "conflicting", "unknown")
DATA_QUALITIES = ("high", "medium", "low", "unknown")
ODDS_FORMATS = ("decimal", "hong_kong")
PRICE_BASES = ("consensus", "median")
SETTLEMENT_STATES = (
    "full_win",
    "half_win",
    "push",
    "half_loss",
    "loss",
)

CORNER_EVIDENCE_COMPONENTS = frozenset(
    {
        "home_away_corners_for_against",
        "opponent_adjusted_corner_rates",
        "width_crossing",
        "dangerous_attacks",
        "set_piece_volume",
        "match_state_tendencies",
        "confirmed_personnel",
    }
)
QUANTITATIVE_CORNER_COMPONENTS = frozenset(
    {
        "home_away_corners_for_against",
        "opponent_adjusted_corner_rates",
    }
)
ADVERSE_CORROBORATION_KINDS = frozenset({"confirmed_lineup", "fundamental"})


class CornerRankerError(ValueError):
    """Raised when prediction, market, or ranking evidence is unsafe."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_aware_datetime(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CornerRankerError(f"{name} must be an ISO-8601 datetime") from exc
    else:
        raise CornerRankerError(f"{name} must be an ISO-8601 datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CornerRankerError(f"{name} needs an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def _canonical_datetime(value: Any, name: str) -> str:
    return (
        _parse_aware_datetime(value, name)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise CornerRankerError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CornerRankerError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise CornerRankerError(f"{name} must be finite")
    return number


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CornerRankerError(f"{name} must be a non-negative integer")
    return value


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CornerRankerError(
            "ranking contains values that cannot be hashed safely"
        ) from exc


def calculate_ranking_hash(ranking: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(ranking))
    payload.pop("ranking_hash", None)
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _required_hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise CornerRankerError(f"{name} must be a SHA-256 hash")
    return value


def _file_hash(path: Path) -> str:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CornerRankerError(f"cannot read registered model file: {path}") from exc


def _quarter_line(value: Any, name: str) -> float:
    line = _finite(value, name)
    units = round(line * 4.0)
    if abs(line * 4.0 - units) > 1e-8:
        raise CornerRankerError(f"{name} must be a multiple of 0.25")
    result = units / 4.0
    return 0.0 if result == 0.0 else result


def _decimal_price(value: Any, odds_format: str, name: str) -> tuple[float, float]:
    price = _finite(value, name)
    if odds_format == "decimal":
        if price <= 1.0:
            raise CornerRankerError(f"{name} decimal odds must be greater than one")
        return price, price - 1.0
    if odds_format == "hong_kong":
        if price <= 0.0:
            raise CornerRankerError(f"{name} Hong Kong odds must be positive")
        return 1.0 + price, price
    raise CornerRankerError(f"{name} has unsupported odds_format")


def _normalize_evidence(
    value: Mapping[str, Any] | None,
    *,
    kickoff: datetime,
    generated_at: datetime,
) -> dict[str, Any]:
    if value is None:
        return {
            "available": False,
            "independent_from_goal_model": False,
            "source": None,
            "collected_at": None,
            "summary": None,
            "components": [],
            "qualified": False,
            "failed_requirements": ["independent corner-profile evidence unavailable"],
        }
    if not isinstance(value, Mapping):
        raise CornerRankerError("corner_profile_evidence must be an object")
    available = value.get("available")
    if not isinstance(available, bool):
        raise CornerRankerError("corner_profile_evidence.available must be boolean")
    if not available:
        return {
            "available": False,
            "independent_from_goal_model": False,
            "source": None,
            "collected_at": None,
            "summary": None,
            "components": [],
            "qualified": False,
            "failed_requirements": ["independent corner-profile evidence unavailable"],
        }
    independent = value.get("independent_from_goal_model")
    if not isinstance(independent, bool):
        raise CornerRankerError(
            "corner_profile_evidence.independent_from_goal_model must be boolean"
        )
    source = value.get("source")
    summary = value.get("summary")
    if not isinstance(source, str) or not source.strip():
        raise CornerRankerError("corner_profile_evidence.source is required")
    if not isinstance(summary, str) or not summary.strip():
        raise CornerRankerError("corner_profile_evidence.summary is required")
    collected = _parse_aware_datetime(
        value.get("collected_at"), "corner_profile_evidence.collected_at"
    )
    if collected >= kickoff:
        raise CornerRankerError(
            "corner_profile_evidence.collected_at must be strictly before kickoff"
        )
    if collected > generated_at:
        raise CornerRankerError(
            "corner_profile_evidence cannot be collected after ranking generation"
        )
    raw_components = value.get("components")
    if not isinstance(raw_components, list) or any(
        not isinstance(component, str) or not component.strip()
        for component in raw_components
    ):
        raise CornerRankerError(
            "corner_profile_evidence.components must be a list of names"
        )
    components = sorted(set(component.strip() for component in raw_components))
    unsupported = sorted(set(components) - CORNER_EVIDENCE_COMPONENTS)
    if unsupported:
        raise CornerRankerError(
            "unsupported corner evidence components: " + ", ".join(unsupported)
        )
    failed: list[str] = []
    if not independent:
        failed.append("evidence is not independent from the football-goal model")
    if len(components) < 2:
        failed.append("fewer than two independent corner-profile components")
    if not QUANTITATIVE_CORNER_COMPONENTS.intersection(components):
        failed.append(
            "quantitative home/away or opponent-adjusted corner rates unavailable"
        )
    return {
        "available": True,
        "independent_from_goal_model": independent,
        "source": source.strip(),
        "collected_at": collected.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "summary": summary.strip(),
        "components": components,
        "qualified": not failed,
        "failed_requirements": failed,
    }


def _normalize_adverse_corroboration(
    value: Mapping[str, Any] | None,
    *,
    kickoff: datetime,
    generated_at: datetime,
    name: str,
) -> dict[str, Any]:
    if value is None:
        return {
            "available": False,
            "kind": None,
            "source": None,
            "collected_at": None,
            "summary": None,
            "qualified": False,
        }
    if not isinstance(value, Mapping):
        raise CornerRankerError(f"{name} must be an object")
    available = value.get("available")
    if not isinstance(available, bool):
        raise CornerRankerError(f"{name}.available must be boolean")
    if not available:
        return {
            "available": False,
            "kind": None,
            "source": None,
            "collected_at": None,
            "summary": None,
            "qualified": False,
        }
    kind = value.get("kind")
    source = value.get("source")
    summary = value.get("summary")
    if kind not in ADVERSE_CORROBORATION_KINDS:
        raise CornerRankerError(f"{name}.kind must be confirmed_lineup or fundamental")
    if not isinstance(source, str) or not source.strip():
        raise CornerRankerError(f"{name}.source is required")
    if not isinstance(summary, str) or not summary.strip():
        raise CornerRankerError(f"{name}.summary is required")
    collected = _parse_aware_datetime(value.get("collected_at"), f"{name}.collected_at")
    if collected >= kickoff:
        raise CornerRankerError(f"{name}.collected_at must be strictly before kickoff")
    if collected > generated_at:
        raise CornerRankerError(f"{name} cannot be collected after ranking generation")
    return {
        "available": True,
        "kind": str(kind),
        "source": source.strip(),
        "collected_at": collected.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "summary": summary.strip(),
        "qualified": True,
    }


def _normalize_market_snapshot(
    value: Mapping[str, Any],
    *,
    kickoff: datetime,
    generated_at: datetime,
    index: int,
) -> dict[str, Any]:
    name = f"markets[{index}]"
    if not isinstance(value, Mapping):
        raise CornerRankerError(f"{name} must be an object")
    market = value.get("market")
    if market not in MARKETS:
        raise CornerRankerError(
            f"{name}.market must be corner_total or corner_handicap"
        )
    line = _quarter_line(value.get("line"), f"{name}.line")
    if market == "corner_total" and line < 0.0:
        raise CornerRankerError(f"{name}.line cannot be negative for corner totals")
    odds_format = value.get("odds_format")
    if odds_format not in ODDS_FORMATS:
        raise CornerRankerError(f"{name}.odds_format must be decimal or hong_kong")
    complete = value.get("market_complete")
    if not isinstance(complete, bool):
        raise CornerRankerError(f"{name}.market_complete must be boolean")
    firm_count = _nonnegative_int(value.get("firm_count"), f"{name}.firm_count")
    source = value.get("market_source")
    if not isinstance(source, str) or not source.strip():
        raise CornerRankerError(f"{name}.market_source is required")
    collected = _parse_aware_datetime(
        value.get("market_collected_at"), f"{name}.market_collected_at"
    )
    if collected >= kickoff:
        raise CornerRankerError(
            f"{name}.market_collected_at must be strictly before kickoff"
        )
    if collected > generated_at:
        raise CornerRankerError(f"{name} cannot be collected after ranking generation")
    price_basis = value.get("price_basis")
    if price_basis not in PRICE_BASES:
        raise CornerRankerError(f"{name}.price_basis must be consensus or median")
    market_signal = value.get("market_signal")
    if market_signal not in MARKET_SIGNALS:
        raise CornerRankerError(
            f"{name}.market_signal must be aligned, neutral, against, "
            "conflicting, or unknown"
        )
    raw_odds = value.get("complete_market_odds")
    if not isinstance(raw_odds, Mapping):
        raise CornerRankerError(f"{name}.complete_market_odds must be an object")
    expected_sides = set(MARKET_SIDES[str(market)])
    normalized_odds: dict[str, float] = {}
    for raw_side, raw_price in raw_odds.items():
        if not isinstance(raw_side, str):
            raise CornerRankerError(f"{name} market outcome labels must be strings")
        side = raw_side.strip().lower()
        if side not in expected_sides or side in normalized_odds:
            raise CornerRankerError(f"{name} contains an invalid or duplicate outcome")
        price = _finite(raw_price, f"{name}.complete_market_odds.{side}")
        _decimal_price(price, str(odds_format), f"{name}.complete_market_odds.{side}")
        normalized_odds[side] = price
    if complete and set(normalized_odds) != expected_sides:
        raise CornerRankerError(
            f"{name} declares complete=true without both market outcomes"
        )
    adverse = _normalize_adverse_corroboration(
        value.get("adverse_signal_corroboration"),
        kickoff=kickoff,
        generated_at=generated_at,
        name=f"{name}.adverse_signal_corroboration",
    )
    return {
        "market": str(market),
        "line": line,
        "odds_format": str(odds_format),
        "complete_market_odds": {
            side: normalized_odds[side]
            for side in MARKET_SIDES[str(market)]
            if side in normalized_odds
        },
        "firm_count": firm_count,
        "market_complete": complete,
        "market_source": source.strip(),
        "market_collected_at": collected.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "price_basis": str(price_basis),
        "market_signal": str(market_signal),
        "adverse_signal_corroboration": adverse,
    }


def _entry_for_prediction(
    registry: Mapping[str, Any], prediction: Mapping[str, Any]
) -> Mapping[str, Any]:
    binding = prediction.get("registry_binding")
    if not isinstance(binding, Mapping):
        raise CornerRankerError("registered prediction binding is missing")
    league_key = binding.get("league_key")
    matches = [
        entry for entry in registry["leagues"] if entry.get("league_key") == league_key
    ]
    if len(matches) != 1:
        raise CornerRankerError("prediction league is not uniquely registered")
    return matches[0]


def _load_verified_inputs(
    prediction: Mapping[str, Any], model_dir: str | Path
) -> tuple[dict[str, Any], Mapping[str, Any], dict[str, Any]]:
    binding = prediction.get("registry_binding")
    if not isinstance(binding, Mapping):
        raise CornerRankerError("registered prediction binding is missing")
    league_key = binding.get("league_key")
    if not isinstance(league_key, str) or not league_key:
        raise CornerRankerError("registered prediction league_key is missing")
    try:
        registry, selected_entry = corner_model_manager.load_registered_league(
            model_dir, league_key
        )
    except corner_model_manager.CornerModelManagerError as exc:
        raise CornerRankerError(f"corner registry is invalid: {exc}") from exc
    entry = _entry_for_prediction(registry, prediction)
    if entry != selected_entry:
        raise CornerRankerError("targeted corner registry entry is inconsistent")
    filename = entry.get("model_file")
    if (
        not isinstance(filename, str)
        or Path(filename).is_absolute()
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or not filename.lower().endswith(".json")
    ):
        raise CornerRankerError("registered model_file is unsafe")
    model_path = Path(model_dir).resolve() / filename
    if _file_hash(model_path) != entry.get("model_file_sha256"):
        raise CornerRankerError("registered corner model file hash does not match")
    try:
        model = corner_model.load_model(model_path)
        corner_model_manager.validate_registered_prediction(
            prediction, registry, model=model
        )
    except (
        corner_model.CornerModelError,
        corner_model_manager.CornerModelManagerError,
    ) as exc:
        raise CornerRankerError(f"registered prediction is invalid: {exc}") from exc
    return registry, entry, model


def _prediction_market_distribution(
    prediction: Mapping[str, Any], market: str, side: str, line: float
) -> dict[str, Any]:
    field = PREDICTION_MARKET_FIELDS[market]
    values = prediction.get(field)
    if not isinstance(values, list):
        raise CornerRankerError(f"prediction {field} is missing")
    matches = [
        value
        for value in values
        if isinstance(value, Mapping)
        and value.get("side") == side
        and abs(float(value.get("line")) - line) <= 1e-10
    ]
    if len(matches) != 1:
        raise CornerRankerError(
            f"prediction needs exactly one five-state {market} distribution "
            f"for {side}:{line:g}"
        )
    item = matches[0]
    raw_probabilities = item.get("probabilities")
    if not isinstance(raw_probabilities, Mapping) or set(raw_probabilities) != set(
        SETTLEMENT_STATES
    ):
        raise CornerRankerError(
            f"prediction {market} {side}:{line:g} lacks all five settlement states"
        )
    probabilities: dict[str, float] = {}
    for state in SETTLEMENT_STATES:
        probability = _finite(
            raw_probabilities[state], f"{market} {side}:{line:g}.{state}"
        )
        if not 0.0 <= probability <= 1.0:
            raise CornerRankerError(
                "corner settlement probabilities must be within [0,1]"
            )
        probabilities[state] = probability
    if abs(math.fsum(probabilities.values()) - 1.0) > 1e-9:
        raise CornerRankerError("corner settlement probabilities must sum to one")
    return {
        "probabilities": probabilities,
        "split_lines": copy.deepcopy(item.get("split_lines")),
        "fair_decimal_odds": item.get("fair_decimal_odds"),
        "fair_hong_kong_odds": item.get("fair_hong_kong_odds"),
    }


def _settlement_ev(
    probabilities: Mapping[str, float], net_win_odds: float
) -> tuple[float, float]:
    returns = {
        "full_win": net_win_odds,
        "half_win": net_win_odds / 2.0,
        "push": 0.0,
        "half_loss": -0.5,
        "loss": -1.0,
    }
    expected = math.fsum(
        float(probabilities[state]) * returns[state] for state in SETTLEMENT_STATES
    )
    variance = math.fsum(
        float(probabilities[state]) * (returns[state] - expected) ** 2
        for state in SETTLEMENT_STATES
    )
    return expected, variance


def _settlement_equivalent_probability(
    probabilities: Mapping[str, float],
) -> tuple[float, float, float]:
    """Map five-state Asian settlement mass onto a comparable two-way rate.

    A half win or half loss represents only half of the stake, while pushes
    carry no directional information.  This makes opposite quarter-line
    directions exact complements and avoids counting a half win as a full win
    when comparing the model with a two-way no-vig market probability.
    """

    win_mass = float(probabilities["full_win"]) + 0.5 * float(probabilities["half_win"])
    loss_mass = float(probabilities["loss"]) + 0.5 * float(probabilities["half_loss"])
    directional_mass = win_mass + loss_mass
    if directional_mass <= 0.0 or not math.isfinite(directional_mass):
        raise CornerRankerError("settlement distribution has no directional stake mass")
    return win_mass / directional_mass, win_mass, loss_mass


def _candidate_rank_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    data_rank = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
    signal_rank = {
        "aligned": 0,
        "neutral": 1,
        "against": 2,
        "conflicting": 3,
        "unknown": 4,
    }
    side_rank = {
        "corner_total": {"over": 0, "under": 1},
        "corner_handicap": {"home": 0, "away": 1},
    }
    ev = candidate.get("ev")
    edge = candidate.get("edge_pp")
    variance = candidate.get("settlement_return_variance")
    return (
        0 if candidate.get("formal_eligible") is True else 1,
        0 if candidate.get("diagnostic_qualification_status") == "qualified" else 1,
        data_rank[str(candidate["data_quality"])],
        -int(candidate["firm_count"]),
        signal_rank[str(candidate["market_signal"])],
        -(float(ev) if ev is not None else -math.inf),
        float(variance) if variance is not None else math.inf,
        -(float(edge) if edge is not None else -math.inf),
        MARKETS.index(str(candidate["market"])),
        float(candidate["snapshot_line"]),
        side_rank[str(candidate["market"])][str(candidate["side"])],
    )


def _build_ranking(
    prediction: Mapping[str, Any],
    market_snapshots: Sequence[Mapping[str, Any]],
    *,
    model_dir: str | Path,
    generated_at: str | datetime,
    data_quality: str,
    corner_profile_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    registry, entry, _model = _load_verified_inputs(prediction, model_dir)
    if data_quality not in DATA_QUALITIES:
        raise CornerRankerError("data_quality must be high, medium, low, or unknown")
    fixture = prediction.get("fixture")
    if not isinstance(fixture, Mapping):
        raise CornerRankerError("prediction fixture is missing")
    kickoff = _parse_aware_datetime(fixture.get("kickoff"), "fixture.kickoff")
    prediction_time = _parse_aware_datetime(
        prediction.get("generated_at"), "prediction.generated_at"
    )
    ranking_time = _parse_aware_datetime(generated_at, "generated_at")
    if ranking_time < prediction_time:
        raise CornerRankerError("ranking generated_at cannot predate the prediction")
    if ranking_time >= kickoff:
        raise CornerRankerError("ranking generated_at must be strictly before kickoff")
    if (
        not isinstance(market_snapshots, Sequence)
        or isinstance(market_snapshots, (str, bytes))
        or not market_snapshots
    ):
        raise CornerRankerError("at least one current corner market is required")
    normalized_markets = [
        _normalize_market_snapshot(
            value,
            kickoff=kickoff,
            generated_at=ranking_time,
            index=index,
        )
        for index, value in enumerate(market_snapshots)
    ]
    identities: set[tuple[str, float]] = set()
    for market in normalized_markets:
        identity = (market["market"], float(market["line"]))
        if identity in identities:
            raise CornerRankerError(
                "duplicate current market snapshot for the same market and line"
            )
        identities.add(identity)
    evidence = _normalize_evidence(
        corner_profile_evidence,
        kickoff=kickoff,
        generated_at=ranking_time,
    )

    candidates: list[dict[str, Any]] = []
    model_input_eligible = (
        prediction["usage_policy"].get("eligible_for_formal_model_input") is True
    )
    deployment_status = prediction.get("deployment_status")
    for snapshot in normalized_markets:
        market = str(snapshot["market"])
        sides = MARKET_SIDES[market]
        odds = snapshot["complete_market_odds"]
        complete = snapshot["market_complete"] is True and set(odds) == set(sides)
        raw_implied: dict[str, float] | None = None
        no_vig: dict[str, float] | None = None
        overround: float | None = None
        if complete:
            raw_implied = {}
            for side in sides:
                decimal_odds, _net = _decimal_price(
                    odds[side],
                    snapshot["odds_format"],
                    f"{market}.{side}.odds",
                )
                raw_implied[side] = 1.0 / decimal_odds
            implied_total = math.fsum(raw_implied.values())
            if implied_total <= 0.0 or not math.isfinite(implied_total):
                raise CornerRankerError(
                    "complete market implied probability is invalid"
                )
            no_vig = {side: raw_implied[side] / implied_total for side in sides}
            overround = implied_total - 1.0

        for side in sides:
            candidate_line = float(snapshot["line"])
            if market == "corner_handicap" and side == "away":
                candidate_line = -candidate_line
                if candidate_line == 0.0:
                    candidate_line = 0.0
            distribution = _prediction_market_distribution(
                prediction, market, side, candidate_line
            )
            probabilities = distribution["probabilities"]
            comparable_probability, equivalent_win_mass, equivalent_loss_mass = (
                _settlement_equivalent_probability(probabilities)
            )
            positive_settlement_probability = (
                probabilities["full_win"] + probabilities["half_win"]
            )
            quoted_odds = odds.get(side)
            decimal_odds: float | None = None
            net_win_odds: float | None = None
            ev: float | None = None
            variance: float | None = None
            if quoted_odds is not None:
                decimal_odds, net_win_odds = _decimal_price(
                    quoted_odds,
                    snapshot["odds_format"],
                    f"{market}.{side}.odds",
                )
                ev, variance = _settlement_ev(probabilities, net_win_odds)
            market_probability = no_vig.get(side) if no_vig is not None else None
            edge_pp = (
                (comparable_probability - market_probability) * 100.0
                if market_probability is not None
                else None
            )

            diagnostic_failures: list[str] = []
            if not complete:
                diagnostic_failures.append(
                    "complete current two-way corner market unavailable"
                )
            if quoted_odds is None:
                diagnostic_failures.append(
                    "selected current executable odds unavailable"
                )
            if snapshot["firm_count"] < MINIMUM_FIRMS:
                diagnostic_failures.append(
                    f"firm count {snapshot['firm_count']} < {MINIMUM_FIRMS}"
                )
            if data_quality not in {"medium", "high"}:
                diagnostic_failures.append(f"data quality {data_quality}")
            if not evidence["qualified"]:
                diagnostic_failures.extend(evidence["failed_requirements"])
            if not model_input_eligible:
                diagnostic_failures.append(
                    "registered model input is observation-only (unknown-team fallback)"
                )
            if deployment_status == "shadow":
                diagnostic_failures.append(
                    "registered model deployment status is shadow"
                )
            if snapshot["market_signal"] == "unknown":
                diagnostic_failures.append("current market signal is unknown")
            if ev is None:
                diagnostic_failures.append("EV unavailable")
            elif ev <= 0.0:
                diagnostic_failures.append(f"EV {ev * 100:.2f}% is not positive")
            if edge_pp is None:
                diagnostic_failures.append(
                    "no-vig model-versus-market edge unavailable"
                )
            elif edge_pp <= 0.0:
                diagnostic_failures.append(f"edge {edge_pp:.2f}pp is not positive")
            if snapshot["market_signal"] in {"against", "conflicting"}:
                if ev is None or ev < ADVERSE_MINIMUM_EV:
                    diagnostic_failures.append(
                        f"adverse-signal EV below {ADVERSE_MINIMUM_EV * 100:.0f}%"
                    )
                if edge_pp is None or edge_pp < ADVERSE_MINIMUM_EDGE_PP:
                    diagnostic_failures.append(
                        f"adverse-signal edge below {ADVERSE_MINIMUM_EDGE_PP:.0f}pp"
                    )
                if snapshot["firm_count"] < ADVERSE_MINIMUM_FIRMS:
                    diagnostic_failures.append(
                        f"adverse-signal firm count {snapshot['firm_count']} "
                        f"< {ADVERSE_MINIMUM_FIRMS}"
                    )
                if not snapshot["adverse_signal_corroboration"]["qualified"]:
                    diagnostic_failures.append(
                        "adverse market signal lacks independent confirmed-lineup "
                        "or fundamental corroboration"
                    )

            upstream_flag = FORMAL_FLAG_FIELDS[market]
            upstream_formal_eligible = prediction.get(upstream_flag) is True
            policy_failures: list[str] = []
            if not upstream_formal_eligible:
                policy_failures.append(
                    f"registered prediction {upstream_flag}=false: "
                    f"{prediction.get('formal_corner_ineligible_reason')}"
                )
            diagnostic_qualified = not diagnostic_failures
            formal_eligible = diagnostic_qualified and upstream_formal_eligible
            candidate = {
                "market": market,
                "period": "full_time_90_minutes",
                "side": side,
                "line": candidate_line,
                "snapshot_line": float(snapshot["line"]),
                "odds": quoted_odds,
                "odds_format": snapshot["odds_format"],
                "decimal_odds": decimal_odds,
                "net_win_odds": net_win_odds,
                "settlement_probabilities": {
                    state: probabilities[state] for state in SETTLEMENT_STATES
                },
                "split_lines": distribution["split_lines"],
                "probability": comparable_probability,
                "probability_basis": "half_stake_weighted_directional_mass_excluding_push",
                "equivalent_win_mass": equivalent_win_mass,
                "equivalent_loss_mass": equivalent_loss_mass,
                "positive_settlement_probability": positive_settlement_probability,
                "fair_decimal_odds": distribution["fair_decimal_odds"],
                "fair_hong_kong_odds": distribution["fair_hong_kong_odds"],
                "raw_implied_probabilities": copy.deepcopy(raw_implied),
                "complete_market_probabilities": copy.deepcopy(no_vig),
                "market_probability": market_probability,
                "overround": overround,
                "edge_pp": edge_pp,
                "ev": ev,
                "settlement_return_variance": variance,
                "firm_count": snapshot["firm_count"],
                "market_complete": complete,
                "market_source": snapshot["market_source"],
                "market_collected_at": snapshot["market_collected_at"],
                "price_basis": snapshot["price_basis"],
                "market_signal": snapshot["market_signal"],
                "data_quality": data_quality,
                "corner_profile_evidence_qualified": evidence["qualified"],
                "adverse_signal_corroboration": copy.deepcopy(
                    snapshot["adverse_signal_corroboration"]
                ),
                "diagnostic_qualification_status": (
                    "qualified" if diagnostic_qualified else "unqualified"
                ),
                "diagnostic_failed_thresholds": diagnostic_failures,
                "upstream_formal_flag": upstream_flag,
                "upstream_formal_eligible": upstream_formal_eligible,
                "formal_eligible": formal_eligible,
                "policy_failed_thresholds": policy_failures,
                "failed_thresholds": [*diagnostic_failures, *policy_failures],
                "status": "formal" if formal_eligible else "observation",
                "role": "unassigned" if formal_eligible else "observation",
            }
            candidates.append(candidate)

    candidates.sort(key=_candidate_rank_key)
    formal_seen = 0
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
        if candidate["formal_eligible"]:
            formal_seen += 1
            candidate["role"] = "primary" if formal_seen == 1 else "secondary"

    formal_candidates = [item for item in candidates if item["formal_eligible"]]
    observations = [item for item in candidates if not item["formal_eligible"]]
    upstream_policy = {
        "deployment_status": prediction.get("deployment_status"),
        "deployment_policy_version": prediction.get("deployment_policy_version"),
        "formal_corner_total_eligible": prediction.get("formal_corner_total_eligible"),
        "formal_corner_handicap_eligible": prediction.get(
            "formal_corner_handicap_eligible"
        ),
        "formal_corner_ineligible_reason": prediction.get(
            "formal_corner_ineligible_reason"
        ),
        "inherited_without_relaxation": True,
    }
    ranking: dict[str, Any] = {
        "artifact_type": RANKING_ARTIFACT_TYPE,
        "schema_version": RANKING_SCHEMA_VERSION,
        "ranker_version": RANKER_VERSION,
        "generated_at": ranking_time.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "fixture": copy.deepcopy(fixture),
        "settlement_scope": {
            "period": "full_time_90_minutes",
            "includes_stoppage_time": True,
            "includes_extra_time": False,
            "includes_shootout": False,
        },
        "prediction_binding": {
            "prediction_hash": prediction["prediction_hash"],
            "registry_hash": registry["registry_hash"],
            "league_key": entry["league_key"],
            "dataset_hash": entry["dataset_hash"],
            "model_hash": entry["model_hash"],
            "evaluation_hash": entry["evaluation_hash"],
            "backtest_hash": entry["backtest_hash"],
            "lineage_hash": entry["lineage_hash"],
            "training_cutoff": entry["training_cutoff"],
        },
        "upstream_policy": upstream_policy,
        "input_audit": {
            "markets": normalized_markets,
            "data_quality": data_quality,
            "corner_profile_evidence": evidence,
            "minimum_firms": MINIMUM_FIRMS,
            "adverse_signal_gate": {
                "minimum_firms": ADVERSE_MINIMUM_FIRMS,
                "minimum_ev": ADVERSE_MINIMUM_EV,
                "minimum_edge_pp": ADVERSE_MINIMUM_EDGE_PP,
                "independent_corroboration_required": True,
            },
        },
        "selection_policy": {
            "version": SELECTION_POLICY_VERSION,
            "edge_probability_basis": (
                "(full_win + 0.5*half_win) / "
                "(full_win + 0.5*half_win + loss + 0.5*half_loss)"
            ),
            "push_mass_excluded_from_directional_edge": True,
            "formal_candidates_must_pass_every_diagnostic_gate": True,
            "upstream_formal_flags_are_hard_gates": True,
            "formal_primary_count_maximum": 1,
            "ranking_order": [
                "formal_eligibility",
                "diagnostic_qualification",
                "data_quality",
                "market_depth",
                "market_signal",
                "expected_value",
                "settlement_variance",
                "edge",
                "canonical_market_side_order",
            ],
        },
        "market_policy": {
            "status": "formal_available" if formal_candidates else "observation_only",
            "formal_count": len(formal_candidates),
            "diagnostically_qualified_count": sum(
                item["diagnostic_qualification_status"] == "qualified"
                for item in candidates
            ),
            "upstream_flags_block_formal_picks": not any(
                upstream_policy[field] is True
                for field in (
                    "formal_corner_total_eligible",
                    "formal_corner_handicap_eligible",
                )
            ),
            "diagnostic_qualification_cannot_override_upstream_policy": True,
        },
        "candidates": candidates,
        "primary": copy.deepcopy(formal_candidates[0]) if formal_candidates else None,
        "secondary": [copy.deepcopy(item) for item in formal_candidates[1:]],
        "best_observation": copy.deepcopy(observations[0]) if observations else None,
        "formal_count": len(formal_candidates),
        "observation_count": len(observations),
    }
    ranking["ranking_hash"] = calculate_ranking_hash(ranking)
    return ranking


def rank_corner_markets(
    prediction: Mapping[str, Any],
    market_snapshots: Sequence[Mapping[str, Any]],
    *,
    model_dir: str | Path,
    generated_at: str | datetime | None = None,
    data_quality: str = "unknown",
    corner_profile_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic ranking of audited corner-market directions."""

    return _build_ranking(
        prediction,
        market_snapshots,
        model_dir=model_dir,
        generated_at=generated_at if generated_at is not None else _utc_now(),
        data_quality=data_quality,
        corner_profile_evidence=corner_profile_evidence,
    )


def validate_ranking(
    ranking: Mapping[str, Any],
    prediction: Mapping[str, Any],
    *,
    model_dir: str | Path,
) -> None:
    """Rebuild a saved ranking from its audited inputs and reject any mismatch."""

    if not isinstance(ranking, Mapping):
        raise CornerRankerError("ranking must be a JSON object")
    if ranking.get("artifact_type") != RANKING_ARTIFACT_TYPE:
        raise CornerRankerError("unexpected ranking artifact_type")
    if ranking.get("schema_version") != RANKING_SCHEMA_VERSION:
        raise CornerRankerError("unsupported ranking schema_version")
    if ranking.get("ranker_version") != RANKER_VERSION:
        raise CornerRankerError("unsupported ranker_version")
    stored_hash = _required_hash(ranking.get("ranking_hash"), "ranking_hash")
    if stored_hash != calculate_ranking_hash(ranking):
        raise CornerRankerError("ranking_hash does not match ranking contents")
    input_audit = ranking.get("input_audit")
    if not isinstance(input_audit, Mapping):
        raise CornerRankerError("ranking input_audit is missing")
    if input_audit.get("minimum_firms") != MINIMUM_FIRMS:
        raise CornerRankerError("ranking minimum_firms policy changed")
    expected = _build_ranking(
        prediction,
        input_audit.get("markets"),
        model_dir=model_dir,
        generated_at=ranking.get("generated_at"),
        data_quality=str(input_audit.get("data_quality")),
        corner_profile_evidence=input_audit.get("corner_profile_evidence"),
    )
    if dict(ranking) != expected:
        raise CornerRankerError(
            "ranking does not reproduce from prediction and audited market inputs"
        )


def _read_json(path: str | Path, name: str) -> Any:
    source = Path(path).resolve()
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CornerRankerError(f"cannot read {name}: {source}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prediction", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument(
        "--markets",
        required=True,
        help="JSON list, or an object with a markets list, of current corner markets",
    )
    parser.add_argument("--corner-profile-evidence", help="structured evidence JSON")
    parser.add_argument(
        "--data-quality",
        choices=DATA_QUALITIES,
        default="unknown",
    )
    parser.add_argument("--generated-at")
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        prediction = _read_json(args.prediction, "registered corner prediction")
        if not isinstance(prediction, Mapping):
            raise CornerRankerError("prediction JSON must contain an object")
        market_payload = _read_json(args.markets, "corner markets")
        markets = (
            market_payload.get("markets")
            if isinstance(market_payload, Mapping)
            else market_payload
        )
        evidence = (
            _read_json(args.corner_profile_evidence, "corner-profile evidence")
            if args.corner_profile_evidence
            else None
        )
        ranking = rank_corner_markets(
            prediction,
            markets,
            model_dir=args.model_dir,
            generated_at=args.generated_at,
            data_quality=args.data_quality,
            corner_profile_evidence=evidence,
        )
        corner_model.save_json(ranking, args.output)
        return 0
    except CornerRankerError as exc:
        parser.exit(2, f"corner_ranker: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

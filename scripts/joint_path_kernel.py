#!/usr/bin/env python3
"""Compact, auditable kernel for half-time/full-time score paths.

The physical path state is always the four-tuple
``(ht_home, ht_away, second_home, second_away)``.  Full-time goals are derived
only by addition.  The compact artifact deliberately stores no path rows.  It
stores the two normalized component score matrices and one multiplier for each
``(half-time result, full-time exact score)`` group.  :func:`iter_paths` and
:func:`validate_kernel` reconstruct every path from those values.

``build_compact_kernel`` accepts either a nine-cell HT/FT target or an already
aligned three-plane event distribution.  With an HT/FT target, feasibility is
proved independently inside each full-time-result block using all seven
non-empty subsets of the three half-time-result supply rows (the fractional
Hall conditions).  A deterministic max-flow then constructs one feasible
alignment.  Statistical callers that already have a preferred alignment (for
example a minimum-KL/IPF solution) should pass ``aligned_event_planes``.

Tail terminology is explicit.  Component matrices are conditional on their
own retained truncation windows.  ``conditional_convolution_retained`` is the
mass of their product that fits inside the stored full-score grid.  Therefore
the overall raw retained mass is::

    (1 - half_raw_omitted) * (1 - second_raw_omitted)
        * conditional_convolution_retained

and overall raw omitted mass is one minus that expression.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Iterator, Mapping, Sequence

ARTIFACT_TYPE = "soccer_joint_path_kernel"
SCHEMA_VERSION = 1
RESULT_ORDER = ("H", "D", "A")
HTFT_ORDER = tuple(half + full for half in RESULT_ORDER for full in RESULT_ORDER)

NORMALIZATION_TOLERANCE = 1e-9
TARGET_TOLERANCE = 1e-8
FEASIBILITY_TOLERANCE = 1e-10
MAX_COMPONENT_AXIS = 16
MAX_COMPONENT_CELLS = 256
MAX_FULL_SCORE_CELLS = 961
MAX_PATH_STATES = 65_536
MAX_GROUP_SCALE = 1e12
# A 12-goal Poisson grid routinely spans 1e20--1e30 while remaining far from
# IEEE-754 underflow.  The earlier 1e15 guard rejected valid production model
# tails; 1e100 still leaves more than two hundred decimal orders of safety.
MAX_MATRIX_DYNAMIC_RANGE = 1e100
MIN_CONDITIONAL_CONVOLUTION_RETAINED = 1e-12


class PathKernelError(ValueError):
    """Raised when a compact path-kernel input or artifact is unsafe."""


class PathKernelFeasibilityError(PathKernelError):
    """Raised when score and HT/FT targets have incompatible structural support.

    The complete fractional Hall report is available through :attr:`audit`.
    """

    def __init__(self, message: str, audit: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.audit = dict(audit)


@dataclass(frozen=True, slots=True)
class PathState:
    """One reconstructed physical score path and its conditional probability."""

    ht_home: int
    ht_away: int
    second_home: int
    second_away: int
    probability: float

    @property
    def ft_home(self) -> int:
        """Full-time home goals, derived from the two component states."""

        return self.ht_home + self.second_home

    @property
    def ft_away(self) -> int:
        """Full-time away goals, derived from the two component states."""

        return self.ht_away + self.second_away


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise PathKernelError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PathKernelError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise PathKernelError(f"{name} must be finite")
    return result


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PathKernelError(f"{name} must be an integer >= {minimum}")
    return value


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


def _positive_dynamic_range(values: Sequence[float], name: str) -> None:
    positive = [value for value in values if value > 0.0]
    if not positive:
        raise PathKernelError(f"{name} must contain positive probability mass")
    ratio = max(positive) / min(positive)
    if not math.isfinite(ratio) or ratio > MAX_MATRIX_DYNAMIC_RANGE:
        raise PathKernelError(
            f"{name} positive dynamic range exceeds {MAX_MATRIX_DYNAMIC_RANGE:g}"
        )


def _matrix(
    value: Any,
    name: str,
    *,
    require_normalized: bool,
    max_axis: int,
    max_cells: int,
    allow_zero_total: bool = False,
) -> list[list[float]]:
    if not _is_sequence(value) or not value:
        raise PathKernelError(f"{name} must be a non-empty rectangular matrix")
    if len(value) > max_axis:
        raise PathKernelError(f"{name} row count exceeds {max_axis}")
    rows: list[list[float]] = []
    column_count: int | None = None
    flat: list[float] = []
    for row_index, raw_row in enumerate(value):
        if not _is_sequence(raw_row) or not raw_row:
            raise PathKernelError(f"{name}[{row_index}] must be a non-empty row")
        if column_count is None:
            column_count = len(raw_row)
            if column_count > max_axis:
                raise PathKernelError(f"{name} column count exceeds {max_axis}")
        elif len(raw_row) != column_count:
            raise PathKernelError(f"{name} must be rectangular")
        row: list[float] = []
        for column_index, raw_cell in enumerate(raw_row):
            cell = _finite(raw_cell, f"{name}[{row_index}][{column_index}]")
            if cell < 0.0:
                raise PathKernelError(f"{name} cannot contain negative values")
            row.append(cell)
            flat.append(cell)
        rows.append(row)
    assert column_count is not None
    if len(rows) * column_count > max_cells:
        raise PathKernelError(f"{name} cell count exceeds {max_cells}")
    if any(cell > 0.0 for cell in flat):
        _positive_dynamic_range(flat, name)
    elif not allow_zero_total:
        raise PathKernelError(f"{name} must contain positive probability mass")
    if require_normalized:
        total = math.fsum(flat)
        if abs(total - 1.0) > NORMALIZATION_TOLERANCE:
            raise PathKernelError(f"{name} must sum to one (got {total:.17g})")
        # Canonicalize only after proving the caller supplied a normalized matrix.
        rows = [[cell / total for cell in row] for row in rows]
    return rows


def _raw_omitted(value: Any, name: str) -> float:
    result = _finite(value, name)
    if result < 0.0 or result >= 1.0:
        raise PathKernelError(f"{name} must be in [0, 1)")
    return result


def _result_code(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "H"
    if home_goals < away_goals:
        return "A"
    return "D"


def _canonical_htft(value: Any, name: str = "htft_target") -> dict[str, float]:
    if isinstance(value, Mapping):
        if set(value) != set(HTFT_ORDER):
            raise PathKernelError(f"{name} must contain exactly all nine HT/FT codes")
        result = {code: _finite(value[code], f"{name}.{code}") for code in HTFT_ORDER}
    elif _is_sequence(value) and len(value) == 3:
        result = {}
        for half_index, raw_row in enumerate(value):
            if not _is_sequence(raw_row) or len(raw_row) != 3:
                raise PathKernelError(f"{name} matrix must be 3x3")
            for full_index, raw_cell in enumerate(raw_row):
                code = RESULT_ORDER[half_index] + RESULT_ORDER[full_index]
                result[code] = _finite(raw_cell, f"{name}[{half_index}][{full_index}]")
    else:
        raise PathKernelError(f"{name} must be a nine-code mapping or 3x3 matrix")
    if any(probability < 0.0 for probability in result.values()):
        raise PathKernelError(f"{name} cannot contain negative probabilities")
    _positive_dynamic_range(list(result.values()), name)
    total = math.fsum(result.values())
    if abs(total - 1.0) > NORMALIZATION_TOLERANCE:
        raise PathKernelError(f"{name} must sum to one (got {total:.17g})")
    return {code: result[code] / total for code in HTFT_ORDER}


def _score_result_marginal(matrix: Sequence[Sequence[float]]) -> dict[str, float]:
    return {
        result: math.fsum(
            probability
            for home, row in enumerate(matrix)
            for away, probability in enumerate(row)
            if _result_code(home, away) == result
        )
        for result in RESULT_ORDER
    }


def _htft_full_result_marginal(htft: Mapping[str, float]) -> dict[str, float]:
    return {
        full: math.fsum(htft[half + full] for half in RESULT_ORDER)
        for full in RESULT_ORDER
    }


def _check_target_columns(
    full_target: Sequence[Sequence[float]], htft_target: Mapping[str, float]
) -> None:
    score_result = _score_result_marginal(full_target)
    htft_result = _htft_full_result_marginal(htft_target)
    for result in RESULT_ORDER:
        if abs(score_result[result] - htft_result[result]) > TARGET_TOLERANCE:
            raise PathKernelError(
                "HT/FT full-result marginal does not match full-score target "
                f"for {result}: {htft_result[result]:.17g} != "
                f"{score_result[result]:.17g}"
            )


def _empty_planes(rows: int, columns: int) -> list[list[list[float]]]:
    return [
        [[0.0 for _away in range(columns)] for _home in range(rows)]
        for _half_result in RESULT_ORDER
    ]


def _seed_planes(
    half: Sequence[Sequence[float]],
    second: Sequence[Sequence[float]],
    full_rows: int,
    full_columns: int,
) -> tuple[list[list[list[float]]], float]:
    planes = _empty_planes(full_rows, full_columns)
    retained_terms: list[float] = []
    half_index = {result: index for index, result in enumerate(RESULT_ORDER)}
    for ht_home, half_row in enumerate(half):
        for ht_away, half_probability in enumerate(half_row):
            result_index = half_index[_result_code(ht_home, ht_away)]
            for second_home, second_row in enumerate(second):
                ft_home = ht_home + second_home
                for second_away, second_probability in enumerate(second_row):
                    ft_away = ht_away + second_away
                    probability = half_probability * second_probability
                    if ft_home < full_rows and ft_away < full_columns:
                        planes[result_index][ft_home][ft_away] += probability
                        retained_terms.append(probability)
    retained = math.fsum(retained_terms)
    if not math.isfinite(retained) or retained < MIN_CONDITIONAL_CONVOLUTION_RETAINED:
        raise PathKernelError(
            "conditional convolution retained probability is too small"
        )
    if retained > 1.0 + NORMALIZATION_TOLERANCE:
        raise PathKernelError(
            "conditional convolution retained probability exceeds one"
        )
    normalized = [
        [[cell / retained for cell in row] for row in plane] for plane in planes
    ]
    return normalized, min(1.0, retained)


def _tail_audit(
    half_raw_omitted: float,
    second_raw_omitted: float,
    conditional_retained: float,
) -> dict[str, Any]:
    conditional_omitted = max(0.0, 1.0 - conditional_retained)
    component_raw_retained = (1.0 - half_raw_omitted) * (1.0 - second_raw_omitted)
    overall_raw_retained = component_raw_retained * conditional_retained
    overall_raw_omitted = 1.0 - overall_raw_retained
    return {
        "scope": "component-conditional convolution plus upstream raw tails",
        "half_raw_omitted_probability": half_raw_omitted,
        "second_raw_omitted_probability": second_raw_omitted,
        "conditional_convolution_retained_probability": conditional_retained,
        "conditional_convolution_omitted_probability": conditional_omitted,
        "component_raw_joint_retained_probability": component_raw_retained,
        "overall_raw_retained_probability": overall_raw_retained,
        "overall_raw_omitted_probability": overall_raw_omitted,
        "overall_raw_omitted_formula": (
            "1 - (1 - half_raw_omitted_probability) * "
            "(1 - second_raw_omitted_probability) * "
            "conditional_convolution_retained_probability"
        ),
    }


def fractional_hall_audit(
    seed_event_planes: Sequence[Sequence[Sequence[float]]],
    full_score_target: Sequence[Sequence[float]],
    htft_target: Mapping[str, float],
) -> dict[str, Any]:
    """Audit all fractional Hall subsets in each full-time-result block.

    ``seed_event_planes`` must use the canonical ``H, D, A`` half-result order.
    The returned report contains seven subset checks for each of the three
    full-result blocks, the global minimum slack, and a concrete conflict when
    infeasible.  A subset slack is its reachable demand minus its supply.
    """

    row_subsets = [
        subset
        for size in range(1, len(RESULT_ORDER) + 1)
        for subset in combinations(RESULT_ORDER, size)
    ]
    blocks: dict[str, Any] = {}
    global_minimum = math.inf
    global_minimum_location: dict[str, Any] | None = None
    global_conflict: dict[str, Any] | None = None
    feasible = True
    result_index = {result: index for index, result in enumerate(RESULT_ORDER)}

    for full_result in RESULT_ORDER:
        cells = [
            (home, away, full_score_target[home][away])
            for home, row in enumerate(full_score_target)
            for away, _probability in enumerate(row)
            if _result_code(home, away) == full_result
        ]
        supply_by_half = {
            half: htft_target[half + full_result] for half in RESULT_ORDER
        }
        block_supply = math.fsum(supply_by_half.values())
        block_demand = math.fsum(probability for _home, _away, probability in cells)
        balance_error = block_demand - block_supply
        checks: list[dict[str, Any]] = []
        block_minimum = math.inf
        block_minimum_check: dict[str, Any] | None = None
        for subset in row_subsets:
            subset_supply = math.fsum(supply_by_half[half] for half in subset)
            neighbor_cells = [
                [home, away]
                for home, away, _probability in cells
                if any(
                    seed_event_planes[result_index[half]][home][away] > 0.0
                    for half in subset
                )
            ]
            neighbor_lookup = {(home, away) for home, away in neighbor_cells}
            neighbor_demand = math.fsum(
                probability
                for home, away, probability in cells
                if (home, away) in neighbor_lookup
            )
            slack = neighbor_demand - subset_supply
            check = {
                "half_result_subset": list(subset),
                "subset_supply_probability": subset_supply,
                "neighbor_demand_probability": neighbor_demand,
                "slack_probability": slack,
                "neighbor_cell_count": len(neighbor_cells),
            }
            checks.append(check)
            if slack < block_minimum:
                block_minimum = slack
                block_minimum_check = {**check, "neighbor_score_cells": neighbor_cells}
            if slack < global_minimum:
                global_minimum = slack
                global_minimum_location = {
                    "full_result": full_result,
                    **check,
                    "neighbor_score_cells": neighbor_cells,
                }

        conflict: dict[str, Any] | None = None
        if block_minimum < -FEASIBILITY_TOLERANCE:
            feasible = False
            assert block_minimum_check is not None
            conflict = {
                "type": "fractional_hall_subset",
                "full_result": full_result,
                **block_minimum_check,
            }
        elif abs(balance_error) > FEASIBILITY_TOLERANCE:
            feasible = False
            conflict = {
                "type": "block_supply_demand_balance",
                "full_result": full_result,
                "block_supply_probability": block_supply,
                "block_demand_probability": block_demand,
                "balance_error_probability": balance_error,
            }
        if global_conflict is None and conflict is not None:
            global_conflict = conflict
        blocks[full_result] = {
            "supply_by_half_result": supply_by_half,
            "block_supply_probability": block_supply,
            "block_demand_probability": block_demand,
            "balance_error_probability": balance_error,
            "minimum_subset_slack_probability": block_minimum,
            "subset_checks": checks,
            "feasible": conflict is None,
            "conflict": conflict,
        }

    return {
        "method": "fractional_hall_all_7_nonempty_half_result_subsets_per_ft_block",
        "half_result_order": list(RESULT_ORDER),
        "full_result_order": list(RESULT_ORDER),
        "subset_count_per_block": 7,
        "tolerance": FEASIBILITY_TOLERANCE,
        "minimum_subset_slack_probability": global_minimum,
        "minimum_subset_location": global_minimum_location,
        "blocks": blocks,
        "feasible": feasible,
        "conflict": global_conflict,
    }


def _coerce_aligned_planes(
    value: Any,
    full_target: Sequence[Sequence[float]],
    seed: Sequence[Sequence[Sequence[float]]],
) -> tuple[list[list[list[float]]], dict[str, float]]:
    rows = len(full_target)
    columns = len(full_target[0])
    if not _is_sequence(value) or len(value) != 3:
        raise PathKernelError("aligned_event_planes must contain three H/D/A planes")
    planes: list[list[list[float]]] = []
    all_values: list[float] = []
    for plane_index, raw_plane in enumerate(value):
        plane = _matrix(
            raw_plane,
            f"aligned_event_planes[{plane_index}]",
            require_normalized=False,
            max_axis=max(rows, columns),
            max_cells=MAX_FULL_SCORE_CELLS,
            allow_zero_total=True,
        )
        if len(plane) != rows or len(plane[0]) != columns:
            raise PathKernelError(
                "aligned_event_planes shape must match full_score_target"
            )
        planes.append(plane)
        all_values.extend(cell for row in plane for cell in row)
    _positive_dynamic_range(all_values, "aligned_event_planes")
    total = math.fsum(all_values)
    if abs(total - 1.0) > NORMALIZATION_TOLERANCE:
        raise PathKernelError("aligned_event_planes must sum to one")
    for home, row in enumerate(full_target):
        for away, target in enumerate(row):
            actual = math.fsum(planes[index][home][away] for index in range(3))
            if abs(actual - target) > TARGET_TOLERANCE:
                raise PathKernelError(
                    "aligned_event_planes do not reproduce full_score_target at "
                    f"({home}, {away})"
                )
    htft = {code: 0.0 for code in HTFT_ORDER}
    for half_index, half_result in enumerate(RESULT_ORDER):
        for home, row in enumerate(full_target):
            for away, _target in enumerate(row):
                code = half_result + _result_code(home, away)
                htft[code] += planes[half_index][home][away]
    htft = _canonical_htft(htft, "derived aligned HT/FT target")
    _check_target_columns(full_target, htft)
    return planes, htft


def _max_flow_block(
    full_result: str,
    seed: Sequence[Sequence[Sequence[float]]],
    full_target: Sequence[Sequence[float]],
    htft_target: Mapping[str, float],
) -> dict[tuple[int, int, int], float]:
    cells = [
        (home, away)
        for home, row in enumerate(full_target)
        for away, probability in enumerate(row)
        if _result_code(home, away) == full_result and probability > 0.0
    ]
    row_count = 3
    source = 0
    row_offset = 1
    cell_offset = row_offset + row_count
    sink = cell_offset + len(cells)
    residual: list[dict[int, float]] = [dict() for _node in range(sink + 1)]
    original: dict[tuple[int, int], float] = {}

    def add_edge(start: int, end: int, capacity: float) -> None:
        residual[start][end] = capacity
        residual[end].setdefault(start, 0.0)
        original[(start, end)] = capacity

    for half_index, half_result in enumerate(RESULT_ORDER):
        add_edge(
            source, row_offset + half_index, htft_target[half_result + full_result]
        )
    for cell_index, (home, away) in enumerate(cells):
        cell_node = cell_offset + cell_index
        demand = full_target[home][away]
        add_edge(cell_node, sink, demand)
        for half_index in range(3):
            if seed[half_index][home][away] > 0.0:
                add_edge(
                    row_offset + half_index,
                    cell_node,
                    min(htft_target[RESULT_ORDER[half_index] + full_result], demand),
                )

    flow = 0.0
    while True:
        parent = [-1 for _node in range(sink + 1)]
        parent[source] = source
        queue: deque[int] = deque([source])
        while queue and parent[sink] == -1:
            node = queue.popleft()
            for neighbor in sorted(residual[node]):
                if parent[neighbor] == -1 and residual[node][neighbor] > 0.0:
                    parent[neighbor] = node
                    queue.append(neighbor)
                    if neighbor == sink:
                        break
        if parent[sink] == -1:
            break
        increment = math.inf
        node = sink
        while node != source:
            previous = parent[node]
            increment = min(increment, residual[previous][node])
            node = previous
        if increment <= 0.0 or not math.isfinite(increment):
            break
        node = sink
        while node != source:
            previous = parent[node]
            residual[previous][node] -= increment
            if residual[previous][node] < 0.0 and residual[previous][node] > -1e-15:
                residual[previous][node] = 0.0
            residual[node][previous] += increment
            node = previous
        flow += increment

    required = math.fsum(htft_target[half + full_result] for half in RESULT_ORDER)
    if abs(flow - required) > FEASIBILITY_TOLERANCE:
        raise PathKernelError(
            f"deterministic flow failed after Hall feasibility for {full_result}"
        )
    allocation: dict[tuple[int, int, int], float] = {}
    for half_index in range(3):
        row_node = row_offset + half_index
        for cell_index, (home, away) in enumerate(cells):
            cell_node = cell_offset + cell_index
            if (row_node, cell_node) not in original:
                continue
            amount = original[(row_node, cell_node)] - residual[row_node][cell_node]
            if amount > 0.0:
                allocation[(half_index, home, away)] = amount
    return allocation


def _align_from_htft(
    seed: Sequence[Sequence[Sequence[float]]],
    full_target: Sequence[Sequence[float]],
    htft_target: Mapping[str, float],
) -> list[list[list[float]]]:
    planes = _empty_planes(len(full_target), len(full_target[0]))
    for full_result in RESULT_ORDER:
        for (half_index, home, away), probability in _max_flow_block(
            full_result, seed, full_target, htft_target
        ).items():
            planes[half_index][home][away] = probability
    return planes


def _group_scale_entries(
    seed: Sequence[Sequence[Sequence[float]]],
    aligned: Sequence[Sequence[Sequence[float]]],
) -> list[list[float | int]]:
    entries: list[list[float | int]] = []
    for half_index in range(3):
        for home, row in enumerate(aligned[half_index]):
            for away, aligned_probability in enumerate(row):
                if aligned_probability <= 0.0:
                    continue
                seed_probability = seed[half_index][home][away]
                if seed_probability <= 0.0:
                    raise PathKernelFeasibilityError(
                        "aligned mass has no component-path support",
                        {
                            "feasible": False,
                            "conflict": {
                                "type": "unsupported_aligned_group",
                                "half_result": RESULT_ORDER[half_index],
                                "full_score": [home, away],
                            },
                        },
                    )
                scale = aligned_probability / seed_probability
                if not math.isfinite(scale) or scale > MAX_GROUP_SCALE:
                    raise PathKernelError(
                        "group scale exceeds the safe finite dynamic limit at "
                        f"({RESULT_ORDER[half_index]}, {home}, {away})"
                    )
                entries.append([half_index, home, away, scale])
    _positive_dynamic_range(
        [float(entry[3]) for entry in entries], "positive group scales"
    )
    return entries


def build_compact_kernel(
    half_time_matrix: Sequence[Sequence[float]],
    second_half_matrix: Sequence[Sequence[float]],
    full_score_target: Sequence[Sequence[float]],
    *,
    htft_target: Mapping[str, float] | Sequence[Sequence[float]] | None = None,
    aligned_event_planes: Sequence[Sequence[Sequence[float]]] | None = None,
    half_raw_omitted: float = 0.0,
    second_raw_omitted: float = 0.0,
) -> dict[str, Any]:
    """Build a compact conditional path distribution.

    At least one of ``htft_target`` and ``aligned_event_planes`` is required.
    When both are supplied, the aligned planes must reproduce the explicit
    HT/FT target within the target tolerance; storing that canonical target
    keeps a pre-solver Hall audit byte-for-byte reproducible despite harmless
    floating-point residuals in an external iterative solver.
    Component and target matrices must be finite, nonnegative, rectangular and
    normalized.  A full-score target may be a prefix crop of the natural score
    convolution; omitted convolution mass is audited separately from raw
    component tails.

    The returned JSON-compatible artifact contains only two component matrices
    plus sparse group scales.  It never contains per-path rows.  Use
    :func:`validate_kernel` for exhaustive reconstruction and marginal checks,
    or :func:`iter_paths` to stream true four-coordinate path states.
    """

    if htft_target is None and aligned_event_planes is None:
        raise PathKernelError("provide htft_target, aligned_event_planes, or both")
    half = _matrix(
        half_time_matrix,
        "half_time_matrix",
        require_normalized=True,
        max_axis=MAX_COMPONENT_AXIS,
        max_cells=MAX_COMPONENT_CELLS,
    )
    second = _matrix(
        second_half_matrix,
        "second_half_matrix",
        require_normalized=True,
        max_axis=MAX_COMPONENT_AXIS,
        max_cells=MAX_COMPONENT_CELLS,
    )
    maximum_path_states = len(half) * len(half[0]) * len(second) * len(second[0])
    if maximum_path_states > MAX_PATH_STATES:
        raise PathKernelError(f"path state count exceeds {MAX_PATH_STATES}")
    natural_rows = len(half) + len(second) - 1
    natural_columns = len(half[0]) + len(second[0]) - 1
    full = _matrix(
        full_score_target,
        "full_score_target",
        require_normalized=True,
        max_axis=max(natural_rows, natural_columns),
        max_cells=MAX_FULL_SCORE_CELLS,
    )
    if len(full) > natural_rows or len(full[0]) > natural_columns:
        raise PathKernelError(
            "full_score_target cannot exceed the natural convolution dimensions"
        )
    half_raw = _raw_omitted(half_raw_omitted, "half_raw_omitted")
    second_raw = _raw_omitted(second_raw_omitted, "second_raw_omitted")
    seed, conditional_retained = _seed_planes(half, second, len(full), len(full[0]))

    if aligned_event_planes is not None:
        aligned, derived_aligned_htft = _coerce_aligned_planes(
            aligned_event_planes, full, seed
        )
        if htft_target is not None:
            canonical_htft = _canonical_htft(htft_target)
            _check_target_columns(full, canonical_htft)
            for code in HTFT_ORDER:
                if (
                    abs(derived_aligned_htft[code] - canonical_htft[code])
                    > TARGET_TOLERANCE
                ):
                    raise PathKernelError(
                        "aligned_event_planes do not reproduce explicit HT/FT "
                        f"target for {code}"
                    )
        else:
            canonical_htft = derived_aligned_htft
        alignment_mode = "provided_aligned_event_planes"
    else:
        canonical_htft = _canonical_htft(htft_target)
        _check_target_columns(full, canonical_htft)
        aligned = []
        alignment_mode = "htft_target_transport"

    hall = fractional_hall_audit(seed, full, canonical_htft)
    if hall["feasible"] is not True:
        raise PathKernelFeasibilityError(
            "full-score and HT/FT targets fail fractional Hall feasibility",
            hall,
        )
    if aligned_event_planes is None:
        aligned = _align_from_htft(seed, full, canonical_htft)

    artifact: dict[str, Any] = {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "state_definition": {
            "axes": ["ht_home", "ht_away", "second_home", "second_away"],
            "full_time_home": "ht_home + second_home",
            "full_time_away": "ht_away + second_away",
            "half_result_order": list(RESULT_ORDER),
        },
        "alignment_mode": alignment_mode,
        "dimensions": {
            "half_time": [len(half), len(half[0])],
            "second_half": [len(second), len(second[0])],
            "full_score": [len(full), len(full[0])],
            "maximum_path_states": maximum_path_states,
        },
        "components": {
            "half_time": {
                "conditional_score_matrix": half,
                "raw_omitted_probability": half_raw,
            },
            "second_half": {
                "conditional_score_matrix": second,
                "raw_omitted_probability": second_raw,
            },
        },
        "targets": {
            "full_score": full,
            "htft": canonical_htft,
        },
        "group_scales": {
            "encoding": "sparse-v1",
            "default": 0.0,
            "entries": _group_scale_entries(seed, aligned),
        },
        "tail_mass": _tail_audit(half_raw, second_raw, conditional_retained),
        "hall_audit": hall,
    }
    validate_kernel(artifact)
    return artifact


def _close(actual: float, expected: float, name: str) -> None:
    if abs(actual - expected) > TARGET_TOLERANCE:
        raise PathKernelError(f"{name} mismatch: {actual:.17g} != {expected:.17g}")


def _same_audit(actual: Any, expected: Any, name: str) -> None:
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping) or set(actual) != set(expected):
            raise PathKernelError(f"{name} structure mismatch")
        for key, expected_value in expected.items():
            _same_audit(actual[key], expected_value, f"{name}.{key}")
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise PathKernelError(f"{name} structure mismatch")
        for index, expected_value in enumerate(expected):
            _same_audit(actual[index], expected_value, f"{name}[{index}]")
        return
    if isinstance(expected, float):
        _close(_finite(actual, name), expected, name)
        return
    if actual != expected:
        raise PathKernelError(f"{name} mismatch")


def _decode_artifact(artifact: Any) -> dict[str, Any]:
    if not isinstance(artifact, Mapping):
        raise PathKernelError("artifact must be an object")
    if artifact.get("artifact_type") != ARTIFACT_TYPE:
        raise PathKernelError("artifact_type is invalid")
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise PathKernelError("schema_version is invalid")
    expected_state = {
        "axes": ["ht_home", "ht_away", "second_home", "second_away"],
        "full_time_home": "ht_home + second_home",
        "full_time_away": "ht_away + second_away",
        "half_result_order": list(RESULT_ORDER),
    }
    if artifact.get("state_definition") != expected_state:
        raise PathKernelError("state_definition is invalid")
    if artifact.get("alignment_mode") not in {
        "provided_aligned_event_planes",
        "htft_target_transport",
    }:
        raise PathKernelError("alignment_mode is invalid")

    components = artifact.get("components")
    if not isinstance(components, Mapping):
        raise PathKernelError("components must be an object")
    half_component = components.get("half_time")
    second_component = components.get("second_half")
    if not isinstance(half_component, Mapping) or not isinstance(
        second_component, Mapping
    ):
        raise PathKernelError("both component objects are required")
    half = _matrix(
        half_component.get("conditional_score_matrix"),
        "components.half_time.conditional_score_matrix",
        require_normalized=True,
        max_axis=MAX_COMPONENT_AXIS,
        max_cells=MAX_COMPONENT_CELLS,
    )
    second = _matrix(
        second_component.get("conditional_score_matrix"),
        "components.second_half.conditional_score_matrix",
        require_normalized=True,
        max_axis=MAX_COMPONENT_AXIS,
        max_cells=MAX_COMPONENT_CELLS,
    )
    half_raw = _raw_omitted(
        half_component.get("raw_omitted_probability"),
        "components.half_time.raw_omitted_probability",
    )
    second_raw = _raw_omitted(
        second_component.get("raw_omitted_probability"),
        "components.second_half.raw_omitted_probability",
    )
    maximum_path_states = len(half) * len(half[0]) * len(second) * len(second[0])
    if maximum_path_states > MAX_PATH_STATES:
        raise PathKernelError(f"path state count exceeds {MAX_PATH_STATES}")

    targets = artifact.get("targets")
    if not isinstance(targets, Mapping):
        raise PathKernelError("targets must be an object")
    natural_rows = len(half) + len(second) - 1
    natural_columns = len(half[0]) + len(second[0]) - 1
    full = _matrix(
        targets.get("full_score"),
        "targets.full_score",
        require_normalized=True,
        max_axis=max(natural_rows, natural_columns),
        max_cells=MAX_FULL_SCORE_CELLS,
    )
    if len(full) > natural_rows or len(full[0]) > natural_columns:
        raise PathKernelError("full-score dimensions exceed the natural convolution")
    htft = _canonical_htft(targets.get("htft"), "targets.htft")
    _check_target_columns(full, htft)

    dimensions = artifact.get("dimensions")
    expected_dimensions = {
        "half_time": [len(half), len(half[0])],
        "second_half": [len(second), len(second[0])],
        "full_score": [len(full), len(full[0])],
        "maximum_path_states": maximum_path_states,
    }
    if dimensions != expected_dimensions:
        raise PathKernelError("dimensions do not match the stored matrices")

    seed, conditional_retained = _seed_planes(half, second, len(full), len(full[0]))
    expected_tail = _tail_audit(half_raw, second_raw, conditional_retained)
    _same_audit(artifact.get("tail_mass"), expected_tail, "tail_mass")
    expected_hall = fractional_hall_audit(seed, full, htft)
    if expected_hall["feasible"] is not True:
        raise PathKernelFeasibilityError(
            "stored targets fail fractional Hall feasibility", expected_hall
        )
    _same_audit(artifact.get("hall_audit"), expected_hall, "hall_audit")

    scales_payload = artifact.get("group_scales")
    if not isinstance(scales_payload, Mapping):
        raise PathKernelError("group_scales must be an object")
    if scales_payload.get("encoding") != "sparse-v1":
        raise PathKernelError("group_scales encoding must be sparse-v1")
    if _finite(scales_payload.get("default"), "group_scales.default") != 0.0:
        raise PathKernelError("group_scales.default must be zero")
    entries = scales_payload.get("entries")
    if not isinstance(entries, list):
        raise PathKernelError("group_scales.entries must be an array")
    if len(entries) > 3 * len(full) * len(full[0]):
        raise PathKernelError("group_scales.entries exceeds the group count")
    scales: dict[tuple[int, int, int], float] = {}
    for entry_index, entry in enumerate(entries):
        if not _is_sequence(entry) or len(entry) != 4:
            raise PathKernelError(f"group_scales.entries[{entry_index}] is invalid")
        half_index = _integer(entry[0], f"group scale {entry_index} half index")
        home = _integer(entry[1], f"group scale {entry_index} home score")
        away = _integer(entry[2], f"group scale {entry_index} away score")
        if half_index >= 3 or home >= len(full) or away >= len(full[0]):
            raise PathKernelError(
                f"group_scales.entries[{entry_index}] is out of range"
            )
        identity = (half_index, home, away)
        if identity in scales:
            raise PathKernelError("group_scales.entries contains a duplicate identity")
        scale = _finite(entry[3], f"group scale {entry_index} multiplier")
        if scale <= 0.0 or scale > MAX_GROUP_SCALE:
            raise PathKernelError(
                f"group_scales.entries[{entry_index}] multiplier is unsafe"
            )
        if seed[half_index][home][away] <= 0.0:
            raise PathKernelError(
                "positive group scale is attached to unsupported seed"
            )
        scales[identity] = scale
    _positive_dynamic_range(list(scales.values()), "positive group scales")

    return {
        "half": half,
        "second": second,
        "full": full,
        "htft": htft,
        "seed": seed,
        "conditional_retained": conditional_retained,
        "scales": scales,
        "tail": expected_tail,
        "hall": expected_hall,
        "maximum_path_states": maximum_path_states,
    }


def _iter_decoded_paths(decoded: Mapping[str, Any]) -> Iterator[PathState]:
    half = decoded["half"]
    second = decoded["second"]
    full = decoded["full"]
    scales = decoded["scales"]
    retained = decoded["conditional_retained"]
    result_index = {result: index for index, result in enumerate(RESULT_ORDER)}
    for ht_home, half_row in enumerate(half):
        for ht_away, half_probability in enumerate(half_row):
            half_index = result_index[_result_code(ht_home, ht_away)]
            for second_home, second_row in enumerate(second):
                ft_home = ht_home + second_home
                for second_away, second_probability in enumerate(second_row):
                    ft_away = ht_away + second_away
                    if ft_home >= len(full) or ft_away >= len(full[0]):
                        continue
                    scale = scales.get((half_index, ft_home, ft_away), 0.0)
                    probability = (
                        half_probability * second_probability / retained * scale
                    )
                    yield PathState(
                        ht_home=ht_home,
                        ht_away=ht_away,
                        second_home=second_home,
                        second_away=second_away,
                        probability=probability,
                    )


def validate_kernel(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Exhaustively reconstruct and validate a compact kernel artifact.

    Validation traverses all structurally retained paths without materializing
    a path table.  It verifies schema/limits, finite values, support, stored Hall
    and tail audits, total mass, the full-score target, and all nine HT/FT target
    cells.  Returned diagnostics include the reconstructed three joint event
    planes plus exact half-time and second-half score marginals.
    """

    decoded = _decode_artifact(artifact)
    half = decoded["half"]
    second = decoded["second"]
    full = decoded["full"]
    htft_target = decoded["htft"]
    event_planes = _empty_planes(len(full), len(full[0]))
    half_marginal = [[0.0 for _away in row] for row in half]
    second_marginal = [[0.0 for _away in row] for row in second]
    full_marginal = [[0.0 for _away in row] for row in full]
    htft = {code: 0.0 for code in HTFT_ORDER}
    result_index = {result: index for index, result in enumerate(RESULT_ORDER)}
    total_terms: list[float] = []
    structural_path_count = 0
    positive_path_count = 0

    for path in _iter_decoded_paths(decoded):
        base_positive = (
            half[path.ht_home][path.ht_away] > 0.0
            and second[path.second_home][path.second_away] > 0.0
        )
        if base_positive:
            structural_path_count += 1
        probability = path.probability
        if not math.isfinite(probability) or probability < 0.0:
            raise PathKernelError("reconstructed path probability is unsafe")
        if probability <= 0.0:
            continue
        positive_path_count += 1
        half_result = _result_code(path.ht_home, path.ht_away)
        full_result = _result_code(path.ft_home, path.ft_away)
        half_index = result_index[half_result]
        event_planes[half_index][path.ft_home][path.ft_away] += probability
        half_marginal[path.ht_home][path.ht_away] += probability
        second_marginal[path.second_home][path.second_away] += probability
        full_marginal[path.ft_home][path.ft_away] += probability
        htft[half_result + full_result] += probability
        total_terms.append(probability)

    total_probability = math.fsum(total_terms)
    _close(total_probability, 1.0, "reconstructed total probability")
    for home, row in enumerate(full):
        for away, expected in enumerate(row):
            _close(
                full_marginal[home][away],
                expected,
                f"full-score marginal ({home}, {away})",
            )
    for code in HTFT_ORDER:
        _close(htft[code], htft_target[code], f"HT/FT marginal {code}")

    return {
        "state_axes": ["ht_home", "ht_away", "second_home", "second_away"],
        "total_probability": total_probability,
        "maximum_path_states": decoded["maximum_path_states"],
        "structurally_retained_path_count": structural_path_count,
        "positive_path_count": positive_path_count,
        "event_planes": event_planes,
        "full_score_marginal": full_marginal,
        "htft_marginal": htft,
        "half_time_score_marginal": half_marginal,
        "second_half_score_marginal": second_marginal,
        "tail_mass": decoded["tail"],
        "hall_audit": decoded["hall"],
    }


def reconstruct_kernel(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Alias for :func:`validate_kernel` for integration-oriented call sites."""

    return validate_kernel(artifact)


def iter_paths(artifact: Mapping[str, Any]) -> Iterator[PathState]:
    """Yield every retained physical path after first validating the artifact.

    Zero-probability structural paths are yielded as well, which lets an audit
    distinguish impossible paths from feasible groups that received zero target
    mass.  Paths outside a cropped full-score grid belong to the conditional
    convolution tail and are intentionally not yielded.
    """

    validate_kernel(artifact)
    decoded = _decode_artifact(artifact)
    yield from _iter_decoded_paths(decoded)


__all__ = [
    "ARTIFACT_TYPE",
    "SCHEMA_VERSION",
    "RESULT_ORDER",
    "HTFT_ORDER",
    "MAX_COMPONENT_AXIS",
    "MAX_COMPONENT_CELLS",
    "MAX_FULL_SCORE_CELLS",
    "MAX_PATH_STATES",
    "MAX_GROUP_SCALE",
    "MAX_MATRIX_DYNAMIC_RANGE",
    "PathKernelError",
    "PathKernelFeasibilityError",
    "PathState",
    "build_compact_kernel",
    "fractional_hall_audit",
    "iter_paths",
    "reconstruct_kernel",
    "validate_kernel",
]

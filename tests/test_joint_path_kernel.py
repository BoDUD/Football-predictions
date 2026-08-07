from __future__ import annotations

import copy
import math
import unittest

from scripts import joint_path_kernel


def _result(home: int, away: int) -> str:
    if home > away:
        return "H"
    if home < away:
        return "A"
    return "D"


def _product_targets(half, second, *, rows=None, columns=None):
    natural_rows = len(half) + len(second) - 1
    natural_columns = len(half[0]) + len(second[0]) - 1
    rows = natural_rows if rows is None else rows
    columns = natural_columns if columns is None else columns
    planes = [
        [[0.0 for _away in range(columns)] for _home in range(rows)]
        for _half_result in range(3)
    ]
    result_index = {code: index for index, code in enumerate(("H", "D", "A"))}
    retained = 0.0
    for ht_home, half_row in enumerate(half):
        for ht_away, half_probability in enumerate(half_row):
            half_index = result_index[_result(ht_home, ht_away)]
            for second_home, second_row in enumerate(second):
                for second_away, second_probability in enumerate(second_row):
                    ft_home = ht_home + second_home
                    ft_away = ht_away + second_away
                    probability = half_probability * second_probability
                    if ft_home < rows and ft_away < columns:
                        planes[half_index][ft_home][ft_away] += probability
                        retained += probability
    planes = [[[cell / retained for cell in row] for row in plane] for plane in planes]
    full = [
        [
            sum(planes[index][home][away] for index in range(3))
            for away in range(columns)
        ]
        for home in range(rows)
    ]
    htft = {code: 0.0 for code in joint_path_kernel.HTFT_ORDER}
    for half_index, half_result in enumerate(joint_path_kernel.RESULT_ORDER):
        for home in range(rows):
            for away in range(columns):
                htft[half_result + _result(home, away)] += planes[half_index][home][
                    away
                ]
    return planes, full, htft, retained


class JointPathKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.half = [[0.4, 0.1], [0.2, 0.3]]
        self.second = [[0.5, 0.1], [0.2, 0.2]]
        self.planes, self.full, self.htft, _retained = _product_targets(
            self.half, self.second
        )

    def assertMatrixAlmostEqual(self, actual, expected, places=12):
        self.assertEqual(len(actual), len(expected))
        for actual_row, expected_row in zip(actual, expected):
            self.assertEqual(len(actual_row), len(expected_row))
            for actual_cell, expected_cell in zip(actual_row, expected_row):
                self.assertAlmostEqual(actual_cell, expected_cell, places=places)

    def test_aligned_small_matrix_reconstructs_true_paths_and_marginals(self) -> None:
        artifact = joint_path_kernel.build_compact_kernel(
            self.half,
            self.second,
            self.full,
            aligned_event_planes=self.planes,
        )

        self.assertNotIn("paths", artifact)
        self.assertNotIn("joint_cells", artifact)
        self.assertEqual(artifact["group_scales"]["encoding"], "sparse-v1")
        self.assertLessEqual(len(artifact["group_scales"]["entries"]), 3 * 3 * 3)
        for entry in artifact["group_scales"]["entries"]:
            self.assertAlmostEqual(entry[3], 1.0, places=12)

        rebuilt = joint_path_kernel.validate_kernel(artifact)
        for actual_plane, expected_plane in zip(rebuilt["event_planes"], self.planes):
            self.assertMatrixAlmostEqual(actual_plane, expected_plane)
        self.assertMatrixAlmostEqual(rebuilt["full_score_marginal"], self.full)
        self.assertMatrixAlmostEqual(rebuilt["half_time_score_marginal"], self.half)
        self.assertMatrixAlmostEqual(rebuilt["second_half_score_marginal"], self.second)
        for code in joint_path_kernel.HTFT_ORDER:
            self.assertAlmostEqual(rebuilt["htft_marginal"][code], self.htft[code])

        paths = list(joint_path_kernel.iter_paths(artifact))
        self.assertEqual(len(paths), 16)
        self.assertAlmostEqual(sum(path.probability for path in paths), 1.0)
        for path in paths:
            self.assertEqual(path.ft_home, path.ht_home + path.second_home)
            self.assertEqual(path.ft_away, path.ht_away + path.second_away)

    def test_aligned_planes_can_bind_an_explicit_pre_solver_htft_target(self) -> None:
        artifact = joint_path_kernel.build_compact_kernel(
            self.half,
            self.second,
            self.full,
            htft_target=self.htft,
            aligned_event_planes=self.planes,
        )
        for code in joint_path_kernel.HTFT_ORDER:
            self.assertAlmostEqual(artifact["targets"]["htft"][code], self.htft[code])
        self.assertTrue(artifact["hall_audit"]["feasible"])
        joint_path_kernel.validate_kernel(artifact)

    def test_htft_transport_checks_all_hall_subsets_and_reproduces_targets(
        self,
    ) -> None:
        artifact = joint_path_kernel.build_compact_kernel(
            self.half,
            self.second,
            self.full,
            htft_target=self.htft,
        )

        self.assertEqual(artifact["alignment_mode"], "htft_target_transport")
        hall = artifact["hall_audit"]
        self.assertTrue(hall["feasible"])
        self.assertEqual(hall["subset_count_per_block"], 7)
        self.assertTrue(math.isfinite(hall["minimum_subset_slack_probability"]))
        for full_result in joint_path_kernel.RESULT_ORDER:
            self.assertEqual(len(hall["blocks"][full_result]["subset_checks"]), 7)
            subsets = {
                tuple(check["half_result_subset"])
                for check in hall["blocks"][full_result]["subset_checks"]
            }
            self.assertEqual(
                subsets,
                {
                    ("H",),
                    ("D",),
                    ("A",),
                    ("H", "D"),
                    ("H", "A"),
                    ("D", "A"),
                    ("H", "D", "A"),
                },
            )

        rebuilt = joint_path_kernel.reconstruct_kernel(artifact)
        self.assertMatrixAlmostEqual(rebuilt["full_score_marginal"], self.full)
        for code in joint_path_kernel.HTFT_ORDER:
            self.assertAlmostEqual(rebuilt["htft_marginal"][code], self.htft[code])

    def test_fractional_hall_fails_fast_on_incompatible_support(self) -> None:
        half = [[0.5], [0.5]]
        second = [[1.0]]
        full = [[0.5], [0.5]]
        target = {code: 0.0 for code in joint_path_kernel.HTFT_ORDER}
        target["HD"] = 0.5
        target["DH"] = 0.5

        with self.assertRaises(joint_path_kernel.PathKernelFeasibilityError) as caught:
            joint_path_kernel.build_compact_kernel(
                half, second, full, htft_target=target
            )

        audit = caught.exception.audit
        self.assertFalse(audit["feasible"])
        self.assertLess(audit["minimum_subset_slack_probability"], 0.0)
        self.assertEqual(audit["conflict"]["type"], "fractional_hall_subset")
        self.assertIn("neighbor_score_cells", audit["conflict"])

    def test_conditional_and_overall_raw_tail_are_distinct(self) -> None:
        half = [[0.5], [0.5]]
        second = [[0.5], [0.5]]
        full = [[1.0 / 3.0], [2.0 / 3.0]]
        target = {code: 0.0 for code in joint_path_kernel.HTFT_ORDER}
        target["DD"] = 1.0 / 3.0
        target["DH"] = 1.0 / 3.0
        target["HH"] = 1.0 / 3.0

        artifact = joint_path_kernel.build_compact_kernel(
            half,
            second,
            full,
            htft_target=target,
            half_raw_omitted=0.1,
            second_raw_omitted=0.2,
        )
        tail = artifact["tail_mass"]
        self.assertAlmostEqual(
            tail["conditional_convolution_retained_probability"], 0.75
        )
        self.assertAlmostEqual(
            tail["conditional_convolution_omitted_probability"], 0.25
        )
        self.assertAlmostEqual(tail["component_raw_joint_retained_probability"], 0.72)
        self.assertAlmostEqual(tail["overall_raw_retained_probability"], 0.54)
        self.assertAlmostEqual(tail["overall_raw_omitted_probability"], 0.46)
        self.assertIn(
            "conditional_convolution_retained_probability",
            tail["overall_raw_omitted_formula"],
        )
        rebuilt = joint_path_kernel.validate_kernel(artifact)
        self.assertAlmostEqual(
            rebuilt["tail_mass"]["overall_raw_omitted_probability"], 0.46
        )

    def test_validator_rejects_scale_duplicate_tail_and_hall_tampering(self) -> None:
        artifact = joint_path_kernel.build_compact_kernel(
            self.half,
            self.second,
            self.full,
            aligned_event_planes=self.planes,
        )

        changed_scale = copy.deepcopy(artifact)
        changed_scale["group_scales"]["entries"][0][3] *= 1.1
        with self.assertRaises(joint_path_kernel.PathKernelError):
            joint_path_kernel.validate_kernel(changed_scale)

        duplicate = copy.deepcopy(artifact)
        duplicate["group_scales"]["entries"].append(
            list(duplicate["group_scales"]["entries"][0])
        )
        with self.assertRaisesRegex(
            joint_path_kernel.PathKernelError, "duplicate identity"
        ):
            joint_path_kernel.validate_kernel(duplicate)

        changed_tail = copy.deepcopy(artifact)
        changed_tail["tail_mass"]["overall_raw_omitted_probability"] += 0.01
        with self.assertRaisesRegex(joint_path_kernel.PathKernelError, "tail_mass"):
            joint_path_kernel.validate_kernel(changed_tail)

        changed_hall = copy.deepcopy(artifact)
        changed_hall["hall_audit"]["minimum_subset_slack_probability"] += 0.01
        with self.assertRaisesRegex(joint_path_kernel.PathKernelError, "hall_audit"):
            joint_path_kernel.validate_kernel(changed_hall)

    def test_rejects_unsupported_aligned_group(self) -> None:
        with self.assertRaises(joint_path_kernel.PathKernelFeasibilityError) as caught:
            joint_path_kernel.build_compact_kernel(
                [[0.5], [0.5]],
                [[0.5], [0.5]],
                [[0.25], [0.5], [0.25]],
                aligned_event_planes=[
                    [[0.0], [0.25], [0.0]],
                    [[0.25], [0.25], [0.25]],
                    [[0.0], [0.0], [0.0]],
                ],
            )
        self.assertEqual(
            caught.exception.audit["conflict"]["type"],
            "unsupported_aligned_group",
        )

    def test_rejects_dimensions_nonfinite_and_extreme_dynamic_range(self) -> None:
        oversized_half = [[1.0 / 17.0] for _index in range(17)]
        with self.assertRaisesRegex(joint_path_kernel.PathKernelError, "row count"):
            joint_path_kernel.build_compact_kernel(
                oversized_half,
                [[1.0]],
                [[1.0]],
                htft_target={
                    code: (1.0 if code == "DD" else 0.0)
                    for code in joint_path_kernel.HTFT_ORDER
                },
            )

        with self.assertRaisesRegex(joint_path_kernel.PathKernelError, "finite"):
            joint_path_kernel.build_compact_kernel(
                [[float("nan")]],
                [[1.0]],
                [[1.0]],
                htft_target={
                    code: (1.0 if code == "DD" else 0.0)
                    for code in joint_path_kernel.HTFT_ORDER
                },
            )

        with self.assertRaisesRegex(joint_path_kernel.PathKernelError, "dynamic range"):
            joint_path_kernel.build_compact_kernel(
                [[1.0 - 1e-101, 1e-101]],
                [[1.0]],
                [[1.0]],
                htft_target={
                    code: (1.0 if code == "DD" else 0.0)
                    for code in joint_path_kernel.HTFT_ORDER
                },
            )

    def test_validator_rejects_dimension_and_sparse_coordinate_tampering(self) -> None:
        artifact = joint_path_kernel.build_compact_kernel(
            self.half,
            self.second,
            self.full,
            aligned_event_planes=self.planes,
        )
        changed_dimensions = copy.deepcopy(artifact)
        changed_dimensions["dimensions"]["maximum_path_states"] += 1
        with self.assertRaisesRegex(joint_path_kernel.PathKernelError, "dimensions"):
            joint_path_kernel.validate_kernel(changed_dimensions)

        changed_coordinate = copy.deepcopy(artifact)
        changed_coordinate["group_scales"]["entries"][0][1] = 999
        with self.assertRaisesRegex(joint_path_kernel.PathKernelError, "out of range"):
            joint_path_kernel.validate_kernel(changed_coordinate)


if __name__ == "__main__":
    unittest.main()

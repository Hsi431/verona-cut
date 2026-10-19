import unittest

from verona.alignment import alignment_offsets


class AlignmentTests(unittest.TestCase):
    def test_aligns_different_sizes_to_group_edges_and_centers(self):
        bounds = [(10, 20, 30, 40), (-5, 5, 5, 15), (40, -10, 60, 0)]
        self.assertEqual(alignment_offsets(bounds, "left"), [(-15, 0), (0, 0), (-45, 0)])
        self.assertEqual(alignment_offsets(bounds, "right"), [(30, 0), (55, 0), (0, 0)])
        self.assertEqual(alignment_offsets(bounds, "hcenter"), [(7.5, 0), (27.5, 0), (-22.5, 0)])
        self.assertEqual(alignment_offsets(bounds, "top"), [(0, -30), (0, -15), (0, 0)])
        self.assertEqual(alignment_offsets(bounds, "bottom"), [(0, 0), (0, 25), (0, 40)])
        self.assertEqual(alignment_offsets(bounds, "vcenter"), [(0, -15), (0, 5), (0, 20)])

    def test_distribution_sorts_unsorted_items_and_keeps_endpoints(self):
        bounds = [(30, 0, 40, 4), (0, 0, 10, 4), (18, 0, 23, 4), (60, 0, 80, 4)]
        offsets = alignment_offsets(bounds, "distribute_h")
        self.assertAlmostEqual(offsets[0][0], 25 / 3)
        self.assertEqual(offsets[1], (0, 0))
        self.assertAlmostEqual(offsets[2][0], 11 / 3)
        self.assertEqual(offsets[3], (0, 0))
        moved = [(left + dx, right + dx) for (left, _, right, _), (dx, _) in zip(bounds, offsets)]
        for actual, expected in zip(moved, [(115 / 3, 145 / 3), (0, 10),
                                           (65 / 3, 80 / 3), (60, 80)]):
            for actual_edge, expected_edge in zip(actual, expected):
                self.assertAlmostEqual(actual_edge, expected_edge)
        self.assertEqual(alignment_offsets([(0, 0, 4, 10), (0, 40, 4, 60),
                                           (0, 12, 4, 17)], "distribute_v"),
                         [(0, 0), (0, 0), (0, 10.5)])

    def test_distribution_allows_negative_gaps_for_overlap(self):
        bounds = [(0, 0, 20, 10), (10, 0, 30, 10), (25, 0, 45, 10)]
        offsets = alignment_offsets(bounds, "distribute_h")
        self.assertEqual(offsets, [(0, 0), (2.5, 0), (0, 0)])

    def test_exact_alignment_and_small_distribution_rejection(self):
        bounds = [(1, 2, 5, 6), (1, 10, 5, 14)]
        self.assertEqual(alignment_offsets(bounds, "left"), [(0, 0), (0, 0)])
        for mode in ("distribute_h", "distribute_v"):
            with self.assertRaises(ValueError):
                alignment_offsets(bounds, mode)

    def test_rejects_nonfinite_or_reversed_bounds(self):
        with self.assertRaises(ValueError):
            alignment_offsets([(0, 0, float("inf"), 1)], "left")
        with self.assertRaises(ValueError):
            alignment_offsets([(2, 0, 1, 1)], "left")


if __name__ == "__main__":
    unittest.main()

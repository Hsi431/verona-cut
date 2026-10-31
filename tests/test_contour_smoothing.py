import math
import unittest

from verona.contour_smoothing import smooth_contours


def _area(contour):
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(contour, contour[1:])) / 2


def _distance_to_polyline(point, contour):
    best = math.inf
    for a, b in zip(contour, contour[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        t = 0 if length2 == 0 else max(0, min(1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length2))
        best = min(best, math.hypot(point[0] - a[0] - t * dx, point[1] - a[1] - t * dy))
    return best


def _sampled_hausdorff(first, second, samples=12):
    def directed(source, target):
        distances = []
        for start, end in zip(source, source[1:]):
            for i in range(samples + 1):
                t = i / samples
                point = (start[0] * (1 - t) + end[0] * t,
                         start[1] * (1 - t) + end[1] * t)
                distances.append(_distance_to_polyline(point, target))
        return max(distances, default=0)
    return max(directed(first, second), directed(second, first))


def _orientation(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(a, b, c, d):
    epsilon = 1e-12
    values = (_orientation(a, b, c), _orientation(a, b, d),
              _orientation(c, d, a), _orientation(c, d, b))
    if values[0] * values[1] < -epsilon and values[2] * values[3] < -epsilon:
        return True

    def on_segment(start, point, end):
        return (min(start[0], end[0]) - epsilon <= point[0] <= max(start[0], end[0]) + epsilon
                and min(start[1], end[1]) - epsilon <= point[1] <= max(start[1], end[1]) + epsilon)

    return any(abs(value) <= epsilon and on_segment(start, point, end)
               for value, start, point, end in (
                   (values[0], a, c, b), (values[1], a, d, b),
                   (values[2], c, a, d), (values[3], c, b, d)))


def _assert_simple(test, contour):
    edges = list(zip(contour, contour[1:]))
    for i, (a, b) in enumerate(edges):
        for j in range(i + 1, len(edges)):
            if j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            c, d = edges[j]
            test.assertFalse(_segments_intersect(a, b, c, d), (i, j, a, b, c, d))


def _point_in_polygon(point, contour):
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(contour, contour[1:]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _radial_ring(radius_fn, count=120, *, reverse=False):
    contour = []
    for i in range(count):
        angle = 2 * math.pi * i / count
        radius = radius_fn(angle)
        contour.append((radius * math.cos(angle), radius * math.sin(angle)))
    contour.append(contour[0])
    if reverse:
        contour.reverse()
    return contour


class ContourSmoothingTests(unittest.TestCase):
    def test_noisy_straight_staircase_is_reduced_within_tolerance(self):
        contour = [(i * 0.1, (i % 2) * 0.025) for i in range(80)]
        smoothed = smooth_contours([contour], 0.1)[0]
        self.assertLess(len(smoothed), len(contour) // 3)
        self.assertTrue(all(_distance_to_polyline(point, contour) <= 0.1 for point in smoothed))

    def test_axis_aligned_right_angle_stairs_are_reduced_with_bidirectional_bound(self):
        staircase = [(0.0, 0.0)]
        x = y = 0.0
        for _ in range(40):
            x += 0.01
            staircase.append((x, y))
            y += 0.01
            staircase.append((x, y))
        result = smooth_contours([staircase], 0.1)[0]
        self.assertLess(len(result), len(staircase) // 10)
        self.assertEqual((result[0], result[-1]), (staircase[0], staircase[-1]))
        self.assertLessEqual(_sampled_hausdorff(staircase, result), 0.1)

    def test_polygonal_arc_is_smoothed_and_stays_close(self):
        arc = [(math.cos(i * math.pi / 40), math.sin(i * math.pi / 40)) for i in range(41)]
        smoothed = smooth_contours([arc], 0.05)[0]
        self.assertLess(len(smoothed), len(arc))
        self.assertTrue(all(_distance_to_polyline(point, arc) <= 0.05 for point in smoothed))

    def test_closed_noisy_circle_and_asymmetric_ring_stay_simple_and_close(self):
        rings = [
            _radial_ring(lambda angle: 1 + 0.015 * math.sin(19 * angle)),
            _radial_ring(lambda angle: 0.85 + 0.25 * math.sin(2 * angle)
                         + 0.1 * math.cos(3 * angle) + 0.01 * math.sin(17 * angle)),
        ]
        for original in rings:
            with self.subTest(points=len(original)):
                smoothed = smooth_contours([original], 0.08)[0]
                self.assertEqual(smoothed[0], smoothed[-1])
                self.assertEqual(math.copysign(1, _area(smoothed)), math.copysign(1, _area(original)))
                self.assertLess(len(smoothed), len(original))
                self.assertLessEqual(_sampled_hausdorff(original, smoothed), 0.08)
                _assert_simple(self, smoothed)

    def test_rectangle_corners_and_closed_seam_are_preserved(self):
        rectangle = [(0, 0), (2, 0), (2, 1), (0, 1), (0, 0)]
        result = smooth_contours([rectangle], 0.2)[0]
        self.assertEqual(result[0], result[-1])
        self.assertEqual(set(rectangle[:-1]), set(result[:-1]))
        self.assertGreater(_area(result), 0)

    def test_open_endpoints_and_winding_are_preserved(self):
        open_path = [(0, 0), (1, 0.01), (2, 0)]
        ring = [(0, 0), (0, 2), (2, 2), (2, 0), (0, 0)]
        open_result, ring_result = smooth_contours([open_path, ring], 0.1)
        self.assertEqual((open_result[0], open_result[-1]), (open_path[0], open_path[-1]))
        self.assertEqual(ring_result[0], ring_result[-1])
        self.assertLess(_area(ring_result), 0)

    def test_zero_tolerance_and_degenerate_input_do_not_mutate(self):
        original = [[(0, 0), (0, 0), (1, 0)]]
        snapshot = [contour[:] for contour in original]
        self.assertEqual(smooth_contours(original, 0), original)
        result = smooth_contours(original, 0.1)
        self.assertEqual(original, snapshot)
        self.assertTrue(result)

    def test_hole_contours_remain_separate(self):
        outer = [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)]
        hole = [(1, 1), (1, 2), (2, 2), (2, 1), (1, 1)]
        result = smooth_contours([outer, hole], 0.15)
        self.assertEqual(len(result), 2)
        self.assertGreater(_area(result[0]), 0)
        self.assertLess(_area(result[1]), 0)

    def test_tiny_hole_and_nearby_closed_contours_do_not_cross_or_collapse(self):
        outer = _radial_ring(lambda _angle: 0.08, count=12)
        hole = _radial_ring(lambda _angle: 0.03, count=12, reverse=True)
        neighbor = _radial_ring(lambda _angle: 0.06, count=12)
        neighbor = [(x + 0.141, y) for x, y in neighbor]
        result = smooth_contours([outer, hole, neighbor], 0.1)
        smoothed_outer, smoothed_hole, smoothed_neighbor = result
        for contour in result:
            self.assertEqual(contour[0], contour[-1])
            self.assertGreaterEqual(len(contour), 4)
            self.assertNotEqual(_area(contour), 0)
            _assert_simple(self, contour)
        self.assertGreater(_area(smoothed_outer), 0)
        self.assertLess(_area(smoothed_hole), 0)
        self.assertGreater(_area(smoothed_neighbor), 0)
        self.assertTrue(all(_point_in_polygon(point, smoothed_outer) for point in smoothed_hole[:-1]))
        for first, second in ((smoothed_outer, smoothed_hole), (smoothed_outer, smoothed_neighbor),
                              (smoothed_hole, smoothed_neighbor)):
            for a, b in zip(first, first[1:]):
                for c, d in zip(second, second[1:]):
                    self.assertFalse(_segments_intersect(a, b, c, d))

    def test_negative_tolerance_is_rejected(self):
        with self.assertRaises(ValueError):
            smooth_contours([], -0.01)


if __name__ == "__main__":
    unittest.main()

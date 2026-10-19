import math
import re
import tempfile
import unittest
from pathlib import Path

from verona.commands import encode_plt
from verona.core import load_svg


class PlotterEncodingTests(unittest.TestCase):
    def test_asymmetric_axis_mapping(self):
        data = encode_plt([[(2.5, 3.5), (220.25, 3.5)]], force=10, speed=5)
        self.assertEqual(data, b"IN;FSIZE140,8810;U SP1;!FS10;VS5;U140,100 D140,8810 U0,0 U !PG;")

    def test_separate_contours_lift_tool(self):
        data = encode_plt([[(0, 0), (10, 0)], [(20, 0), (20, 10)]], tool=1)
        self.assertIn(b"D0,400 U0,800 D400,800", data)
        self.assertIn(b"SP2;", data)
        self.assertNotIn(b"!FS", data)
        self.assertNotIn(b"VS", data)
        self.assertNotIn(b"TransmitSingle", data)

    def test_native_single_precision_before_rounding(self):
        # Native TPointF(.0125) is slightly above .0125, so its plotter
        # coordinate rounds to 1, rather than Python double's round(.5)==0.
        data = encode_plt([[(0, 0), (.0125, .0125)], [(0, 0), (.0625, .0625)]])
        self.assertIn(b"U0,0 D1,1 U0,0 D2,2 ", data)

    def test_redundant_straight_points_preserve_corners_and_lifts(self):
        data = encode_plt([[(0, 0), (1, 0), (2, 0), (2, 1), (2, 2)],
                           [(4, 4), (5, 5), (6, 6)]])
        self.assertIn(b"U0,0 D0,80 D80,80 U160,160 D240,240 ", data)
        self.assertNotIn(b"D0,40 ", data)

    def test_backtracking_and_closed_contour_keep_the_same_directed_trace(self):
        source = [(0, 0), (1, 0), (2, 0), (3, 0), (3, 1), (3, 2),
                  (2, 2), (1, 2), (2, 2), (3, 2), (3, 1), (3, 0),
                  (2, 0), (1, 0), (0, 0)]
        for bounds in (None, (1, 1, 10, 10)):
            points = source if bounds is None else [(x+1, y+1) for x, y in source]
            data = encode_plt([points], registration_bounds=bounds)
            moves = [(int(a), int(b)) for a, b in re.findall(rb"[UD](\d+),(\d+) ", data)][:-1]
            expected = [(round(y*40), round(x*40)) if bounds is None
                        else (round((10-y)*40), round((x-1)*40)) for x, y in points]

            def lattice_trace(path):
                trace = [path[0]]
                for a, b in zip(path, path[1:]):
                    dx, dy = b[0]-a[0], b[1]-a[1]
                    steps = math.gcd(dx, dy)
                    trace.extend((a[0]+dx*i//steps, a[1]+dy*i//steps)
                                 for i in range(1, steps+1))
                return trace

            with self.subTest(bounds=bounds):
                self.assertLess(len(moves), len(expected))
                self.assertEqual(moves[0], moves[-1])
                self.assertEqual(lattice_trace(moves), lattice_trace(expected))

    def test_print_cut_bounds_header_and_asymmetric_coordinate_mapping(self):
        data = encode_plt([[(11, 23), (76, 91)]], force=10, speed=5,
                          registration_bounds=(10, 20, 80, 95))
        self.assertEqual(
            data,
            b"IN;PA;BD:103,0;IN;TB26,3000,2800;CT1;IN;PU;U SP1;!FS10;VS5;"
            b"U2880,40 D160,2640 U2880,0 U !PG;!PG;",
        )

    def test_print_cut_rejects_invalid_bounds_and_outside_points(self):
        for bounds in ((0, 0, 0, 10), (0, 0, 301, 10), (0, 0, 10, math.inf),
                       (1, 2, 3), (False, 0, 10, 10), (-1, 1, 10, 10)):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                encode_plt([[(1, 1), (2, 2)]], registration_bounds=bounds)
        for point in ((9.99, 20), (80.01, 50), (40, 19.99), (40, 95.01)):
            with self.subTest(point=point), self.assertRaises(ValueError):
                encode_plt([[(10, 20), point]], registration_bounds=(10, 20, 80, 95))

    def test_registration_adjustment_uses_page_axes_and_keeps_frame(self):
        points = [[(11, 23), (76, 91)]]
        data = encode_plt(points, registration_bounds=(10, 20, 80, 95),
                          registration_offset=(-.3, .2))
        self.assertIn(b"TB26,3000,2800;", data)
        self.assertIn(b"U2872,28 D152,2628 ", data)
        self.assertTrue(data.endswith(b"U2872,0 U !PG;!PG;"))
        self.assertEqual(encode_plt(points, registration_bounds=(10, 20, 80, 95)),
                         encode_plt(points, registration_bounds=(10, 20, 80, 95), registration_offset=(0, 0)))

    def test_invalid_adjustment_and_adjusted_frame_escape_are_rejected(self):
        for offset in ((True, 0), (0, float("nan")), (6, 0), (1,), "0,0"):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                encode_plt([[(11, 23), (76, 91)]], registration_bounds=(10, 20, 80, 95),
                           registration_offset=offset)
        with self.assertRaises(ValueError):
            encode_plt([[(1, 1), (2, 2)]], registration_offset=(.1, 0))
        for offset in ((-.1, 0), (0, -.1), (.1, 0), (0, .1)):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                encode_plt([[(10, 20), (80, 95)]], registration_bounds=(10, 20, 80, 95),
                           registration_offset=offset)

    def test_deduplicates_quantized_points_and_rejects_empty(self):
        data = encode_plt([[(0, 0), (.001, .001), (1, 1)]])
        self.assertNotIn(b"D0,0", data)
        with self.assertRaises(ValueError):
            encode_plt([[(0, 0), (.001, .001)]])

    def test_bounds_nonfinite_and_material_settings(self):
        for point in ((301, 0), (0, 301), (-1, 0), (0, -1), (math.nan, 0), (0, math.inf)):
            with self.subTest(point=point), self.assertRaises(ValueError):
                encode_plt([[(0, 0), point]])
        for settings in ({"force": 161}, {"speed": 14}, {"speed": 0}, {"force": True}, {"tool": 2}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                encode_plt([[(0, 0), (1, 1)]], **settings)


class SvgGeometryTests(unittest.TestCase):
    def load(self, content):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "test.svg"
            filename.write_text(content)
            return load_svg(filename)

    def test_physical_units_viewbox_and_nested_transform(self):
        contours = self.load('''<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" viewBox="0 0 200 100">
            <g transform="translate(20,10)"><g transform="scale(2)"><path d="M0 0L10 0"/></g></g></svg>''')
        self.assertEqual(contours, [[(2.0, 1.0), (4.0, 1.0)]])

    def test_subpaths_do_not_become_cutting_bridges(self):
        contours = self.load('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L10 0 M20 0L30 0"/></svg>')
        self.assertEqual(len(contours), 2)
        self.assertEqual([len(path) for path in contours], [2, 2])

    def test_preserve_aspect_ratio_alignment(self):
        contours = self.load('<svg width="20mm" height="10mm" viewBox="0 0 10 10"><path d="M0 0L10 10"/></svg>')
        self.assertEqual(contours, [[(5.0, 0.0), (15.0, 10.0)]])

    def test_hidden_groups_and_curves(self):
        contours = self.load('''<svg width="10mm" height="10mm" viewBox="0 0 10 10">
            <g style="display:none"><path d="M0 0L1000 1000"/></g>
            <path d="M0 0C0 10 10 10 10 0"/></svg>''')
        self.assertEqual(len(contours), 1)
        self.assertGreater(len(contours[0]), 100)
        self.assertEqual(contours[0][0], (0, 0))
        self.assertEqual(contours[0][-1], (10, 0))

    def test_rejects_unsupported_visible_content(self):
        for content in ('<text>Hello</text>', '<image href="x.png"/>', '<use href="#x"/>',
                        '<path clip-path="url(#x)" d="M0 0L1 1"/>', '<style>path {display:none}</style>'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.load(f'<svg>{content}</svg>')

    def test_malformed_xml_is_a_value_error(self):
        for content in ('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L1 1"/>', "not xml"):
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "SVG"):
                self.load(content)


if __name__ == "__main__":
    unittest.main()

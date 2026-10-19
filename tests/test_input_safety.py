import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from verona import core, commands, input_safety
from verona.ui import VeronaWindow


def design(items=None):
    return {"format": "verona", "version": 1, "units": "mm",
            "items": items if items is not None else [
                {"name": "line", "contours": [[[1, 2], [3, 4]]]}]}


class InputSafetyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "fixture.jdesign"

    def read(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return input_safety.read_design(self.path)

    def test_file_size_boundary_and_check_before_json_parse(self):
        text = json.dumps(design())
        self.path.write_text(text)
        with patch.object(input_safety, "MAX_DESIGN_BYTES", len(text)):
            self.assertEqual(input_safety.read_design(self.path), design())
        with patch.object(input_safety, "MAX_DESIGN_BYTES", len(text)-1), \
                patch.object(input_safety.json, "loads") as parse, self.assertRaises(ValueError):
            input_safety.read_design(self.path)
        parse.assert_not_called()

    def test_read_is_bounded_if_file_grows_after_size_check(self):
        class GrowingFile(io.BytesIO):
            def fileno(self): return 123
            def read(self, size):
                reads.append(size)
                return super().read(size)
        reads = []
        source = GrowingFile(b"x" * 20)
        with patch.object(Path, "open", return_value=source), \
                patch.object(input_safety.os, "fstat", return_value=type("Stat", (), {"st_size": 0})()), \
                self.assertRaises(ValueError):
            input_safety.read_bounded(self.path, 10)
        self.assertEqual(reads, [11])

    def test_depth_boundary_and_deep_metadata_rejected_before_parse(self):
        with patch.object(input_safety, "MAX_DEPTH", 3):
            self.assertEqual(self.read({"a": [[0]]}), {"a": [[0]]})
            self.path.write_text('{"a":' + '[' * 1000 + '0' + ']' * 1000 + '}')
            with patch.object(input_safety.json, "loads") as parse, self.assertRaises(ValueError):
                input_safety.read_design(self.path)
            parse.assert_not_called()

    def test_quoted_brackets_escaped_quotes_and_backslashes_are_not_structure(self):
        data = {"a": '[\\\"' + '[' * 300 + '\\]"', "b": "\\", "c": "\"\\\""}
        with patch.object(input_safety, "MAX_DEPTH", 1), patch.object(input_safety, "MAX_JSON_TOKENS", 6):
            self.assertEqual(self.read(data), data)

    def test_total_json_structure_budget_is_checked_before_parse(self):
        self.path.write_text('[0,0,0,0]')
        with patch.object(input_safety, "MAX_JSON_TOKENS", 4):
            self.assertEqual(input_safety.read_design(self.path), [0, 0, 0, 0])
        with patch.object(input_safety, "MAX_JSON_TOKENS", 3), \
                patch.object(input_safety.json, "loads") as parse, self.assertRaises(ValueError):
            input_safety.read_design(self.path)
        parse.assert_not_called()

    def test_item_and_point_limits_run_before_image_decode_or_copy(self):
        item = {"name": "image", "contours": [[[1, 2], [3, 4]]],
                "image_clip": [[[1, 2], [3, 4]]], "image_data": "invalid!"}
        for limits in ({"MAX_ITEMS": 0}, {"MAX_POINTS": 3}):
            data = design([item]);before = copy.deepcopy(data)
            with patch.multiple(input_safety, **limits), \
                    patch("verona.ui._qoi_image") as decode, \
                    patch.object(VeronaWindow, "_validate_contours") as points, self.assertRaises(ValueError):
                VeronaWindow._validate_document(data)
            decode.assert_not_called();points.assert_not_called()
            self.assertEqual(data, before)

    def test_geometry_budget_includes_noncut_clips_and_trusted_items(self):
        item = {"name": "line", "cut": False, "contours": [[[1, 2], [3, 4]]],
                "image_clip": [[[1, 2], [3, 4]]]}
        with patch.object(input_safety, "MAX_POINTS", 4):
            input_safety.check_geometry_budget([item])
        with patch.object(input_safety, "MAX_POINTS", 3), self.assertRaises(ValueError):
            VeronaWindow._validate_document(design([item]), trusted_items=[item])

    def test_boundary_valid_design_keeps_geometry_and_plt(self):
        original = design()
        with patch.object(input_safety, "MAX_ITEMS", 1), patch.object(input_safety, "MAX_POINTS", 2):
            items, settings = VeronaWindow._validate_document(self.read(original))
        self.assertEqual(items[0]["contours"], [[(1., 2.), (3., 4.)]])
        self.assertEqual(commands.encode_document(items, {}, settings),
                         commands.encode_plt([[(1., 2.), (3., 4.)]]))


class SvgBudgetTests(unittest.TestCase):
    def load(self, content):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.svg"
            path.write_text(content)
            return core.load_svg(path)

    def test_svg_file_limit_before_xml_parse(self):
        with patch.object(input_safety, "MAX_SVG_BYTES", 4), \
                patch.object(core, "parse_svg_root") as parse, self.assertRaises(ValueError):
            self.load('<svg><path d="M0 0L1 1"/></svg>')
        parse.assert_not_called()

    def test_svg_tree_depth_and_element_boundaries(self):
        data = '<svg><g><path d="M0 0L1 1"/></g></svg>'
        with patch.object(input_safety, "MAX_DEPTH", 3), patch.object(core, "_MAX_SVG_ELEMENTS", 3):
            self.assertEqual(len(self.load(data)), 1)
        for limits in ({"MAX_DEPTH": 2}, {"MAX_DEPTH": 256}):
            with patch.multiple(input_safety, **limits), self.assertRaises(ValueError):
                self.load(data if limits['MAX_DEPTH'] == 2 else '<svg>'+'<g>'*1100+'<path d="M0 0L1 1"/>'+'</g>'*1100+'</svg>')
        with patch.object(core, "_MAX_SVG_ELEMENTS", 2), self.assertRaises(ValueError):
            self.load(data)

    def test_svg_points_are_document_wide_with_subpaths_and_closure(self):
        data = '<svg><path d="M0 0L1 0Z M2 0L3 0"/></svg>'
        with patch.object(input_safety, "MAX_POINTS", 5):
            self.assertEqual(sum(map(len, self.load(data))), 5)
        with patch.object(input_safety, "MAX_POINTS", 4), self.assertRaises(ValueError):
            self.load(data)
        with patch.object(input_safety, "MAX_POINTS", 3), self.assertRaises(ValueError):
            self.load('<svg><path d="M0 0L1 1"/><path d="M2 2L3 3"/></svg>')

    def test_svg_cubic_is_rejected_before_sampling(self):
        with patch.object(input_safety, "MAX_POINTS", 3), \
                patch("svgpathtools.CubicBezier.point", side_effect=AssertionError("sampling started")), \
                self.assertRaises(ValueError):
            self.load('<svg><path d="M0 0C0 10 10 10 10 0"/></svg>')

    def test_svg_source_complexity_is_checked_before_segment_parse(self):
        for content in ('<path d="M0 0L1 1"/>', '<polyline points="0,0 1,1"/>',
                        '<g transform="translate(1,2) scale(2)"><path d="M0 0L1 1"/></g>'):
            with patch.object(core, "_MAX_SVG_TOKENS", 2), \
                    patch.object(core, "parse_path") as parse, self.assertRaises(ValueError):
                self.load('<svg>'+content+'</svg>')
            parse.assert_not_called()

    def test_svg_tiny_tolerance_is_a_value_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'fixture.svg';path.write_text('<svg><path d="M0 0C0 10 10 10 10 0"/></svg>')
            with self.assertRaises(ValueError):
                core.load_svg(path, 1e-320)

    def test_svg_nonfinite_coordinates_and_transforms_are_rejected(self):
        for content in ('<path d="M0 0L1e999 1"/>',
                        '<path transform="scale(1e999)" d="M0 0L1 1"/>'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.load('<svg>'+content+'</svg>')

    def test_truncated_svg_paths_raise_value_error(self):
        for path in ('M0', 'M0 0C1'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.load('<svg><path d="'+path+'"/></svg>')

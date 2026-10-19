"""Packing preserves physical size and documents bounded heuristic behavior."""
import copy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QSettings, QTemporaryDir
from PySide6.QtGui import QPainterPath, QPolygonF
from PySide6.QtWidgets import QApplication, QDialog

from verona.artwork import contour_path, image_path, item_bounds
from verona.editor_actions import build_edit
from verona.commands import encode_plt
from verona.packing import pack_items
from verona.printing import registration_geometry
from verona.ui import VeronaWindow, PackingDialog
from tests.test_editor_actions import image_item, rectangle


class PackingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = QTemporaryDir()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.path())
        cls.app = QApplication.instance() or QApplication([])

    def test_whole_pattern_rotation_increases_count_without_resizing(self):
        items = [rectangle(width=40, height=60)]
        original = copy.deepcopy(items)
        metadata = {"print_cut": {"enabled": True, "paper_width": 90, "paper_height": 130,
                                  "bounds": [10, 10, 71, 111], "offset": [.1, -.1]}}
        _, _, fixed = build_edit(items, metadata, [0], "fill_area", {"gap_mm": 2})
        result, selected, rotated = build_edit(items, metadata, [0], "fill_area",
                                               {"gap_mm": 2, "allow_rotate": True})
        self.assertEqual(fixed["copies"], 1)
        self.assertEqual(rotated["copies"], 2)
        self.assertEqual(rotated["rotation_deg"], 90)
        self.assertEqual(len(selected), 2)
        for item in result:
            box = item_bounds(item)
            self.assertAlmostEqual(box.width(), 60)
            self.assertAlmostEqual(box.height(), 40)
            self.assertGreaterEqual(box.left(), 10)
            self.assertLessEqual(box.bottom(), 111)
        self.assertEqual(items, original)

    def test_rotation_ties_keep_original_and_default_geometry_is_unchanged(self):
        items = [image_item(bleed={"width_mm": 1, "color": "#ff0000"})]
        fixed = build_edit(items, {}, [0], "fill_area", {"max_copies": 1})
        rotated = build_edit(items, {}, [0], "fill_area", {"max_copies": 1, "allow_rotate": True})
        self.assertEqual(rotated[2]["rotation_deg"], 0)
        self.assertEqual(rotated[0], fixed[0])
        self.assertEqual(rotated[0][0]["bleed"]["width_mm"], 1)
        with self.assertRaisesRegex(ValueError, "boolean"):
            build_edit(items, {}, [0], "fill_area", {"allow_rotate": 1})

    def test_rotation_keeps_other_objects_and_obstacle_clearance(self):
        items = [rectangle(width=40, height=60), rectangle("obstacle", x=10, y=10, width=10, height=10)]
        metadata = {"print_cut": {"enabled": True, "paper_width": 90, "paper_height": 130,
                                  "bounds": [10, 10, 71, 111]}}
        result, selected, details = build_edit(items, metadata, [0], "fill_area",
                                               {"gap_mm": 2, "allow_rotate": True})
        self.assertEqual(result[0], items[1])
        obstacle = item_bounds(result[0]).adjusted(-1, -1, 1, 1)
        for index in selected:
            self.assertFalse(item_bounds(result[index]).adjusted(-1, -1, 1, 1).intersects(obstacle))
        self.assertIn(details["rotation_deg"], (0, 90))

    def test_mixed_counts_preserve_group_relationships_unselected_work_and_sizes(self):
        items = [image_item(group_path=["a", "nested"]),
                 rectangle(x=45, y=20, width=10, group_path=["a"]),
                 rectangle("large", x=90, y=60, width=30, height=40),
                 rectangle("keep", x=160, y=160)]
        original = copy.deepcopy(items)
        result, selected, details = pack_items(items, {}, [0, 2],
            {"copies_per_unit": [2, 3], "allow_rotate": True, "gap_mm": 2})
        self.assertEqual(details["copies"], 5)
        self.assertEqual([unit["copies"] for unit in details["units"]], [2, 3])
        self.assertEqual(result[0], original[3])
        self.assertEqual(len(selected), 7)
        group_ids = {item["group_path"][0] for item in result if "group_path" in item}
        self.assertEqual(len(group_ids), 2)
        self.assertNotIn("a", group_ids)
        for group in group_ids:
            pair = [item for item in result if item.get("group_path", [None])[0] == group]
            boxes = [item_bounds(item) for item in pair]
            self.assertEqual(len(pair), 2)
            self.assertAlmostEqual(boxes[1].left()-boxes[0].left(), 25)
            self.assertEqual(pair[0]["image_data"], original[0]["image_data"])
        self.assertEqual(items, original)

    def test_contour_packing_interlocks_two_L_shapes_that_rectangles_cannot_fit(self):
        item = {"name": "L", "cut": True, "contours": [[(0, 0), (2, 0), (2, 8),
            (10, 8), (10, 10), (0, 10), (0, 0)]]}
        metadata = {"print_cut": {"enabled": True, "paper_width": 30, "paper_height": 30,
                                  "bounds": [5, 5, 18.1, 15.1]}}
        parameters = {"copies_per_unit": [2], "allow_rotate": True, "gap_mm": 1}
        with self.assertRaises(ValueError):
            pack_items([item], metadata, [0], parameters)
        result, _, details = pack_items([item], metadata, [0], {**parameters, "shape_aware": True})
        self.assertEqual(details["copies"], 2)
        self.assertEqual([place["rotation_deg"] for place in details["placements"]], [0, 180])
        self.assertFalse(contour_path(result[0]["contours"]).intersects(contour_path(result[1]["contours"])))
        self.assertTrue(all(abs(item_bounds(copy).width()-10) < 1e-8 for copy in result))

    def test_printed_footprints_avoid_registration_marks_and_effective_frame(self):
        metadata = {"print_cut": {"enabled": True, "paper_width": 100, "paper_height": 100,
                                  "bounds": [10, 10, 90, 90], "offset": [-.5, .5]}}
        result, _, details = pack_items([image_item()], metadata, [0],
                                        {"copies_per_unit": [3], "allow_rotate": True})
        marks = QPainterPath()
        for polygon in registration_geometry([10, 10, 90, 90]):
            marks.addPolygon(QPolygonF([QPointF(*point) for point in polygon]))
            marks.closeSubpath()
        for item in result:
            self.assertFalse(image_path(item).intersects(marks))
            box = item_bounds(item, printed=True)
            self.assertGreaterEqual(box.left(), 10.5)
            self.assertLessEqual(box.bottom(), 89.5)
        self.assertEqual(details["registration_mark_clearance_mm"], 2)
        payload = encode_plt([contour for item in result for contour in item["contours"]],
                             registration_bounds=[10, 10, 90, 90], registration_offset=[-.5, .5])
        self.assertTrue(payload.endswith(b" U !PG;!PG;"))

    def test_fill_adds_copies_and_invalid_or_limited_search_keeps_source_unchanged(self):
        items = [rectangle(width=12, height=12)]
        metadata = {"print_cut": {"enabled": True, "paper_width": 40, "paper_height": 40,
                                  "bounds": [5, 5, 35, 35]}}
        _, _, details = pack_items(items, metadata, [0], {"fill": True})
        self.assertEqual(details["copies"], 4)
        original = copy.deepcopy(items)
        for parameters in ({"copies_per_unit": [True]}, {"copies_per_unit": [1001]},
                           {"shape_aware": 1}, {"copies_per_unit": []}):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                pack_items(items, metadata, [0], parameters)
        with patch("verona.packing.time.monotonic", side_effect=[0, 0, 20]), self.assertRaises(ValueError):
            pack_items(items, metadata, [0], {"copies_per_unit": [2]})
        self.assertEqual(items, original)

    def test_gui_applies_exact_cached_preview_and_undoes_as_one_step(self):
        window = VeronaWindow()
        window.items = [rectangle(), rectangle(x=80, y=80, width=10, height=30)]
        window.refresh_all()
        window.select_many([0, 1])
        original = window._document_state()
        try:
            def accept(dialog):
                self.assertFalse(dialog.apply_button.isEnabled())
                dialog.copies_spin.setValue(2)
                dialog.preview()
                self.assertTrue(dialog.apply_button.isEnabled())
                self.assertIs(dialog.build_items(), dialog.plan)
                dialog.gap_spin.setValue(3)
                self.assertFalse(dialog.apply_button.isEnabled())
                dialog.preview()
                return QDialog.DialogCode.Accepted

            with patch.object(PackingDialog, "exec", accept), \
                    patch("verona.packing.pack_items", wraps=pack_items) as pack:
                window.pack_selected()
            self.assertEqual(pack.call_count, 2)
            self.assertEqual(len(window.items), 4)
            self.assertEqual(len(window.undo_stack), 1)
            window.undo()
            self.assertEqual(window._document_state(), original)
        finally:
            with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                window.close()

    def test_open_cut_lines_have_a_nonempty_collision_footprint(self):
        line = {"name": "open line", "cut": True, "contours": [[(20, 20), (40, 40)]]}
        metadata = {"print_cut": {"enabled": True, "paper_width": 40, "paper_height": 40,
                                  "bounds": [5, 5, 35, 35]}}
        result, _, details = pack_items([line], metadata, [0],
                                        {"copies_per_unit": [2], "shape_aware": True})
        self.assertEqual(details["copies"], 2)
        self.assertNotEqual(result[0]["contours"], result[1]["contours"])
        self.assertFalse(contour_path(result[0]["contours"]).intersects(contour_path(result[1]["contours"])))
        for item in result:
            start, end = item["contours"][0]
            self.assertAlmostEqual(end[0]-start[0], 20)
            self.assertAlmostEqual(end[1]-start[1], 20)

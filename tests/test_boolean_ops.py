"""Path boolean operations through the editor's shared edit pipeline."""
import os
import re
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt
from PySide6.QtGui import QPainterPath
from PySide6.QtWidgets import QApplication

from verona.artwork import contour_path
from verona.commands import encode_document_job
from verona.editor_actions import build_edit
from verona.editor_document import build_document_edit
from verona.geometry import flatten_path
from verona.ui import VeronaWindow


def rectangle(name, x, y, width, height, **properties):
    return {"name": name, "contours": [[(x, y), (x+width, y), (x+width, y+height),
                                         (x, y+height), (x, y)]],
            "cut": True, **properties}


def polygon_area(contour):
    return abs(sum(x1*y2-x2*y1 for (x1, y1), (x2, y2)
                   in zip(contour, contour[1:])) / 2)


def has_proper_self_crossing(contour):
    edges = list(zip(contour, contour[1:]))
    for first, (a, b) in enumerate(edges):
        for second, (c, d) in enumerate(edges):
            if second <= first+1 or (first == 0 and second == len(edges)-1):
                continue
            ab_c = (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
            ab_d = (b[0]-a[0])*(d[1]-a[1])-(b[1]-a[1])*(d[0]-a[0])
            cd_a = (d[0]-c[0])*(a[1]-c[1])-(d[1]-c[1])*(a[0]-c[0])
            cd_b = (d[0]-c[0])*(b[1]-c[1])-(d[1]-c[1])*(b[0]-c[0])
            if ab_c*ab_d < -1e-12 and cd_a*cd_b < -1e-12:
                return True
    return False


def ring(name, cx, cy):
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    path.addEllipse(QRectF(cx-30, cy-30, 60, 60))
    path.addEllipse(QRectF(cx-10, cy-10, 20, 20))
    return {"name": name, "contours": flatten_path(path), "cut": True}


class BooleanPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings.cleanup()

    def test_known_square_areas_and_base_item_properties(self):
        bottom = rectangle("bottom", 20, 20, 20, 20, print=True, cut=True,
                           print_style={"fill": "#123456"}, cut_passes=3)
        source = [bottom, rectangle("top", 30, 30, 20, 20)]
        expected = {"weld": 700, "intersect": 100, "subtract": 300, "exclude": 600}
        for mode, area in expected.items():
            with self.subTest(mode=mode):
                result, selection, details = build_edit(source, {}, [0, 1], "boolean", {"mode": mode})
                self.assertEqual(len(result), 1)
                self.assertEqual(selection, [0])
                self.assertEqual(result[0]["name"], "bottom")
                self.assertEqual(result[0]["print"], True)
                self.assertEqual(result[0]["cut"], True)
                self.assertEqual(result[0]["print_style"], {"fill": "#123456"})
                self.assertEqual(result[0]["cut_passes"], 3)
                self.assertAlmostEqual(sum(polygon_area(c) for c in result[0]["contours"]), area,
                                       delta=area*.005)
                self.assertEqual(details["mode"], mode)
                self.assertEqual(details["precision_mm"], .01)
                self.assertTrue(all(polygon_area(c) > 0 and not has_proper_self_crossing(c)
                                    for c in result[0]["contours"]))

    def test_welded_text_is_no_longer_editable_text(self):
        items, *_ = build_document_edit([], {}, {}, [], "add_text",
                                        {"text": "O", "x_mm": 20, "y_mm": 20, "height_mm": 20})
        self.assertIn("text_data", items[0])
        items.append(rectangle("bar", 15, 25, 40, 5))
        result, _, _ = build_edit(items, {}, [0, 1], "boolean", {"mode": "weld"})
        self.assertNotIn("text_data", result[0])
        self.assertNotIn("text_source_contours", result[0])

    def test_welding_two_rings_keeps_both_holes(self):
        result, _, _ = build_edit([ring("left", 40, 80), ring("right", 80, 80)],
                                  {}, [0, 1], "boolean", {"mode": "weld"})
        item = result[0]
        combined = contour_path(item["contours"])
        self.assertEqual(len(item["contours"]), 3)
        self.assertFalse(combined.contains(QPointF(40, 80)))
        self.assertFalse(combined.contains(QPointF(80, 80)))
        self.assertTrue(combined.contains(QPointF(60, 80)))
        self.assertTrue(all(polygon_area(contour) > 0 for contour in item["contours"]))

    def test_subtract_outputs_a_real_inner_contour_in_plt(self):
        result, _, _ = build_edit([rectangle("base", 20, 20, 40, 40),
                                   rectangle("cutter", 30, 30, 20, 20)],
                                  {}, [0, 1], "boolean", {"mode": "subtract"})
        item = result[0]
        combined = contour_path(item["contours"])
        self.assertEqual(len(item["contours"]), 2)
        self.assertFalse(combined.contains(QPointF(40, 40)))
        self.assertTrue(combined.contains(QPointF(25, 25)))
        self.assertLess(polygon_area(item["contours"][0]), polygon_area(item["contours"][1]))
        self.assertTrue(contour_path([item["contours"][1]]).contains(QPointF(25, 25)))
        payload, job = encode_document_job([item], {},
            {"custom": False, "force": None, "speed": None, "tool": 0, "passes": 1})
        self.assertEqual(len(job["contours"]), 2)
        self.assertLess(polygon_area(job["contours"][0]), polygon_area(job["contours"][1]))
        self.assertEqual(len(re.findall(rb"U\d+,\d+ ", payload))-1, 2)

    def test_group_operation_uses_vector_members_and_preserves_group_image(self):
        image = {"name": "photo", "image_data": "image", "contours": [], "cut": False,
                 "group_path": ["group"]}
        source = [rectangle("a", 20, 20, 20, 20, group_path=["group"]), image,
                  rectangle("b", 30, 30, 20, 20, group_path=["group"])]
        result, selected, _ = build_edit(source, {}, [0], "boolean", {"mode": "weld"})
        self.assertEqual(selected, [0])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "a")
        self.assertEqual(result[1], image)
        self.assertEqual(result[0]["group_path"], ["group"])

    def test_invalid_selection_and_image_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "two"):
            build_edit([rectangle("one", 20, 20, 20, 20)], {}, [0], "boolean", {"mode": "weld"})
        image = {"name": "photo", "image_data": "image", "contours": [], "cut": True}
        with self.assertRaisesRegex(ValueError, "Images"):
            build_edit([image, rectangle("vector", 20, 20, 20, 20)], {}, [0, 1],
                       "boolean", {"mode": "weld"})
        with self.assertRaisesRegex(ValueError, "Unlock"):
            build_edit([rectangle("a", 20, 20, 20, 20),
                        rectangle("b", 30, 30, 20, 20, locked=True)], {}, [0, 1],
                       "boolean", {"mode": "weld"})
        with self.assertRaises(ValueError):
            build_edit([rectangle("a", 20, 20, 20, 20), rectangle("b", 30, 30, 20, 20)],
                       {}, [0, 1], "boolean", {"mode": "unknown"})
        with self.assertRaisesRegex(ValueError, "no filled area"):
            build_edit([rectangle("a", 20, 20, 20, 20), rectangle("b", 40, 20, 20, 20)],
                       {}, [0, 1], "boolean", {"mode": "intersect"})

    def test_document_edit_routes_boolean_to_build_edit(self):
        result, metadata, settings, selection, details = build_document_edit(
            [rectangle("a", 20, 20, 20, 20), rectangle("b", 30, 30, 20, 20)],
            {}, {"custom": False, "tool": 0, "passes": 1}, [0, 1],
            "boolean", {"mode": "intersect"})
        self.assertEqual(len(result), 1)
        self.assertEqual(metadata, {})
        self.assertEqual(selection, [0])
        self.assertAlmostEqual(sum(polygon_area(c) for c in result[0]["contours"]), 100)
        self.assertEqual(details["action"], "boolean")
        self.assertEqual(settings["passes"], 1)

    def test_gui_disables_invalid_selection_and_boolean_edit_is_one_undo(self):
        window = VeronaWindow()
        original = [rectangle("a", 20, 20, 20, 20), rectangle("b", 30, 30, 20, 20)]
        try:
            window.items = original
            window.refresh_all()
            window.select_many([0])
            self.assertFalse(window.boolean_actions["weld"].isEnabled())
            self.assertIn("two", window.boolean_actions["weld"].toolTip())
            window.select_many([0, 1])
            self.assertTrue(window.boolean_actions["weld"].isEnabled())
            self.assertEqual(window.boolean_actions["weld"].shortcut().toString(), "Ctrl+Shift+U")
            window.boolean_selected("weld")
            self.assertEqual(len(window.items), 1)
            self.assertEqual(len(window.undo_stack), 1)
            window.undo()
            self.assertEqual(window.items, original)
            window.select_many([0])
            window.items[0] = {**window.items[0], "image_data": "image"}
            window.refresh_all()
            self.assertFalse(window.boolean_actions["weld"].isEnabled())
            self.assertIn("Images", window.boolean_actions["weld"].toolTip())
        finally:
            with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                window.close()

"""Offscreen behavior tests for canvas transforms, groups, and editor view state."""

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt, QSettings
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QFileDialog

from verona.i18n import set_language
from verona.ui import PAGE_MARGIN, VeronaWindow, _item_path


class TransformHandleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_temp = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          cls.settings_temp.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings_temp.cleanup()

    def setUp(self):
        set_language("zh_TW")
        QSettings("Verona", "Workspace").clear()
        self.window = VeronaWindow()

    def tearDown(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()

    @staticmethod
    def _item(name, left, top, right, bottom, **extra):
        return {"name": name,
                "contours": [[(left, top), (right, top), (right, bottom),
                              (left, bottom), (left, top)]], **extra}

    def _send_mouse(self, event_type, world, *, button, buttons, modifiers=Qt.KeyboardModifier.NoModifier):
        canvas = self.window.canvas
        local = QPointF(34 + world.x() * canvas.zoom, 34 + world.y() * canvas.zoom)
        global_pos = QPointF(canvas.mapToGlobal(local.toPoint()))
        event = QMouseEvent(event_type, local, global_pos, button, buttons, modifiers)
        QApplication.sendEvent(canvas, event)

    def _drag(self, start, end, *, modifiers=Qt.KeyboardModifier.NoModifier):
        self._send_mouse(QMouseEvent.Type.MouseButtonPress, start,
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
        self._send_mouse(QMouseEvent.Type.MouseMove, end,
                         button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.LeftButton,
                         modifiers=modifiers)
        self._send_mouse(QMouseEvent.Type.MouseButtonRelease, end,
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.NoButton,
                         modifiers=modifiers)

    def test_apply_size_keeps_untouched_geometry_including_negative_and_unrounded(self):
        self.window.items = [self._item("off page", -5, -3, 20, 10),
                             self._item("fine", 12.345, 40.006, 30.1234, 51)]
        self.window.refresh_all()
        for index in range(2):
            self.window.select(index)
            original = copy.deepcopy(self.window.items[index]["contours"])
            undo_steps = len(self.window.undo_stack)
            self.window.object_name.setText(f"renamed {index}")
            self.window.apply_size()
            self.assertEqual(self.window.items[index]["contours"], original)
            self.assertEqual(self.window.items[index]["name"], f"renamed {index}")
            self.assertEqual(len(self.window.undo_stack), undo_steps + 1)
        self.window.select(0)
        self.assertEqual((self.window.x_spin.value(), self.window.y_spin.value()), (-5, -3))
        self.window.x_spin.setValue(-2)
        self.window.apply_size()
        self.assertEqual(_item_path(self.window.items[0]).boundingRect().getCoords(), (-2, -3, 23, 10))

    def test_apply_size_uses_qt_rounding_when_fields_are_unchanged(self):
        self.window.items = [self._item("rounded", 12.125, -5.125, 32.25, 15.25)]
        self.window.refresh_all()
        self.window.select(0)
        original = copy.deepcopy(self.window.items[0]["contours"])
        self.assertEqual(self.window.x_spin.value(), 12.13)
        self.assertEqual(self.window.y_spin.value(), -5.13)
        self.window.apply_size()
        self.assertEqual(self.window.items[0]["contours"], original)

    def test_drag_maps_cached_paths_instead_of_rebuilding_them(self):
        from verona import ui
        self.window.items = [self._item("big", 10, 10, 60, 40)]
        self.window.items[0]["contours"][0] = [(10 + x / 10, 10) for x in range(500)] + [(60, 40), (10, 10)]
        self.window.refresh_all()
        self.window.select(0)
        canvas = self.window.canvas
        start = QPointF(PAGE_MARGIN + 30 * canvas.zoom, PAGE_MARGIN + 10 * canvas.zoom)
        canvas.mousePressEvent(QMouseEvent(QMouseEvent.Type.MouseButtonPress, start, start,
                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        self.assertIsNotNone(canvas.drag_before)
        canvas.grab()
        with patch("verona.ui._path_from_contours", wraps=ui._path_from_contours) as build:
            for step in (1, 2, 3):
                point = start + QPointF(step * 2.5 * canvas.zoom, step * canvas.zoom)
                canvas.mouseMoveEvent(QMouseEvent(QMouseEvent.Type.MouseMove, point, point, Qt.MouseButton.NoButton,
                                      Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
                canvas.grab()
        self.assertEqual(build.call_count, 0)
        moved = canvas.item_path(self.window.items[0])
        self.assertEqual(moved, ui._path_from_contours(self.window.items[0]["contours"]))

    def test_zoom_scale_anchor_ratio_undo_and_escape_cancel(self):
        self.window.change_zoom(100)
        self.assertEqual(self.window.canvas.zoom, 64.0)
        self.assertEqual(self.window.zoom_label.text(), "6400%")

        self.window.items = [self._item("rect", 10, 10, 20, 20)]
        self.window.select(0)
        self.window.lock_ratio.setChecked(True)
        before = copy.deepcopy(self.window.items)
        bottom_right = self.window.canvas.selection_handles()["se"]
        self._drag(bottom_right, QPointF(25, 30))
        bounds = _item_path(self.window.items[0]).boundingRect()
        self.assertAlmostEqual(bounds.left(), 10)
        self.assertAlmostEqual(bounds.top(), 10)
        self.assertAlmostEqual(bounds.width(), 20)
        self.assertAlmostEqual(bounds.height(), 20)
        self.assertEqual(len(self.window.undo_stack), 1)
        self.window.undo()
        self.assertEqual(self.window.items, before)

        start = self.window.canvas.selection_handles()["se"]
        self._send_mouse(QMouseEvent.Type.MouseButtonPress, start,
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
        self._send_mouse(QMouseEvent.Type.MouseMove, QPointF(28, 29),
                         button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.LeftButton)
        self.assertNotEqual(self.window.items, before)
        self.window.canvas.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress,
                                                   Qt.Key.Key_Escape,
                                                   Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self.window.items, before)
        self.assertEqual(len(self.window.undo_stack), 0)

    def test_print_cut_warning_updates_during_drag_resize_and_escape(self):
        self.window.items = [self._item("rect", 30, 30, 50, 50)]
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 210.,
                                              "paper_height": 297., "bounds": [10., 10., 80., 80.]}}
        self.window.refresh_all()
        self.window.select(0)
        self.window.show()
        QApplication.processEvents()
        origin = self.window.canvas.mapToGlobal(QPointF().toPoint())
        before = copy.deepcopy(self.window.items)
        self._send_mouse(QMouseEvent.Type.MouseButtonPress, QPointF(35, 35),
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
        self._send_mouse(QMouseEvent.Type.MouseMove, QPointF(5, 35),
                         button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        self.assertFalse(self.window.bounds_warning.isHidden())
        self.assertEqual(self.window.canvas.mapToGlobal(QPointF().toPoint()), origin)
        self.window.canvas.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress,
                                                   Qt.Key.Key_Escape,
                                                   Qt.KeyboardModifier.NoModifier))
        self.assertTrue(self.window.bounds_warning.isHidden())
        self.assertEqual(self.window.items, before)
        self._drag(self.window.canvas.selection_handles()["se"], QPointF(85, 85))
        self.assertFalse(self.window.bounds_warning.isHidden())
        self.window.undo()
        self.assertTrue(self.window.bounds_warning.isHidden())
        self.window.redo()
        self.assertFalse(self.window.bounds_warning.isHidden())

    def test_shift_rotation_transforms_image_clip_and_cutline_together(self):
        item = self._item("image", 10, 10, 20, 20, cut=False,
                          image_transform=[10, 10, 1, 0, 0, 1],
                          image_clip=[[(10, 10), (20, 10), (20, 20), (10, 10)]])
        self.window.items = [item]
        self.window.select(0)
        handle = self.window.canvas.selection_handles()["rotate"]
        center = self.window.selection_bounds().center()
        start_angle = QPointF(handle)
        end = QPointF(center.x() + 24, center.y())
        shift = Qt.KeyboardModifier.ShiftModifier
        self._send_mouse(QMouseEvent.Type.MouseButtonPress, start_angle,
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
        self._send_mouse(QMouseEvent.Type.MouseMove, end,
                         button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.LeftButton,
                         modifiers=shift)
        self._send_mouse(QMouseEvent.Type.MouseButtonRelease, end,
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.NoButton,
                         modifiers=shift)
        transformed = self.window.items[0]
        self.assertFalse(transformed["cut"])
        self.assertAlmostEqual(transformed["image_transform"][0], 20)
        self.assertAlmostEqual(transformed["image_transform"][1], 10)
        clip = _item_path({"contours": transformed["image_clip"]}).boundingRect()
        contour = _item_path(transformed).boundingRect()
        self.assertAlmostEqual(clip.left(), contour.left())
        self.assertAlmostEqual(clip.top(), contour.top())
        self.assertAlmostEqual(clip.right(), contour.right())
        self.assertAlmostEqual(clip.bottom(), contour.bottom())
        self.assertEqual(len(self.window.undo_stack), 1)

    def test_group_click_duplication_alignment_and_undo_preserve_group_geometry(self):
        self.window.items = [
            self._item("a", 30, 10, 40, 20, group_path=["group"]),
            self._item("b", 50, 10, 60, 20, group_path=["group"]),
            self._item("free", 0, 10, 10, 20),
        ]
        first_bounds = [_item_path(item).boundingRect() for item in self.window.items]
        self._send_mouse(QMouseEvent.Type.MouseButtonPress, QPointF(32, 12),
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
        self._send_mouse(QMouseEvent.Type.MouseButtonRelease, QPointF(32, 12),
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.NoButton)
        self.assertEqual(self.window.selected_indices(), {0, 1})

        self.window.duplicate_selected()
        self.assertNotEqual(self.window.items[0]["group_path"], self.window.items[3]["group_path"])
        self.assertEqual(self.window.items[3]["group_path"], self.window.items[4]["group_path"])
        self.assertEqual(self.window.selected_indices(), {3, 4})
        self.window.select_many({0, 2})
        self.window.align_selected("left")
        moved_bounds = [_item_path(item).boundingRect() for item in self.window.items]
        self.assertAlmostEqual(moved_bounds[0].left(), moved_bounds[2].left())
        self.assertAlmostEqual(moved_bounds[1].left() - moved_bounds[0].left(),
                               first_bounds[1].left() - first_bounds[0].left())
        self.assertEqual(len(self.window.undo_stack), 2)
        self.window.undo()
        self.assertAlmostEqual(_item_path(self.window.items[0]).boundingRect().left(), 30)
        self.assertAlmostEqual(_item_path(self.window.items[1]).boundingRect().left(), 50)

        self.window.select_many({0})
        ctrl = Qt.KeyboardModifier.ControlModifier
        self._send_mouse(QMouseEvent.Type.MouseButtonPress, QPointF(32, 12),
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton,
                         modifiers=ctrl)
        self._send_mouse(QMouseEvent.Type.MouseButtonRelease, QPointF(32, 12),
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.NoButton,
                         modifiers=ctrl)
        self.assertEqual(self.window.selected_indices(), set())

    def test_group_path_save_roundtrip_and_undo(self):
        self.window.items = [self._item("a", 1, 1, 4, 4),
                             self._item("b", 8, 1, 11, 4)]
        self.window.select_many({0, 1})
        self.window.group_selected()
        expected = copy.deepcopy([item["group_path"] for item in self.window.items])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "grouped.jdesign"
            with patch.object(QFileDialog, "getSaveFileName",
                              return_value=(str(path), "Design")):
                self.assertTrue(self.window.save_design())
            reopened = VeronaWindow()
            try:
                self.assertTrue(reopened._load_document(str(path)))
                self.assertEqual([item["group_path"] for item in reopened.items], expected)
            finally:
                reopened.close()
        self.window.undo()
        self.assertTrue(all("group_path" not in item for item in self.window.items))

    def test_cut_section_language_tabs_and_view_controls_do_not_change_document_or_plt(self):
        self.assertFalse(self.window.cut_section.isExpanded())
        self.window.cut_section.setExpanded(True)
        self.window._change_language("en")
        self.assertTrue(self.window.cut_section.isExpanded())

        self.window.items = [self._item("on", 0, 0, 10, 10, cut=True),
                             self._item("off", 20, 0, 30, 10, cut=False)]
        self.window.saved_snapshot = self.window._document_state()
        plt_before = self.window._encoded_plt()
        self.window.image_opacity.setValue(55)
        self.window.cutline_view.setChecked(True)
        self.window.image_opacity.setValue(42)
        self.assertTrue(self.window.cutline_view.isChecked())
        self.assertEqual(self.window.image_opacity.value(), 42)
        self.assertEqual(self.window._encoded_plt(), plt_before)
        self.assertFalse(self.window.is_dirty())

        first_view = (self.window.image_opacity.value(), self.window.cutline_view.isChecked())
        self.window.new_design()
        self.window.image_opacity.setValue(75)
        self.window.tab_bar.setCurrentIndex(0)
        self.assertEqual((self.window.image_opacity.value(), self.window.cutline_view.isChecked()),
                         first_view)
        self.window.tab_bar.setCurrentIndex(1)
        self.assertEqual((self.window.image_opacity.value(), self.window.cutline_view.isChecked()),
                         (75, False))


if __name__ == "__main__":
    unittest.main()

"""User-facing regression tests for editor documents and canvas gestures."""

import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QSettings
from PySide6.QtGui import QCloseEvent, QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from verona.i18n import set_language
from verona.ui import VeronaWindow, _item_path


class EditorWorkflowTests(unittest.TestCase):
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
        self.settings = QSettings("Verona", "Workspace")
        self.settings.clear()
        self.window = VeronaWindow()

    def tearDown(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()

    def test_application_event_filter_only_while_shown_on_x11(self):
        calls = []
        original = VeronaWindow.eventFilter

        def spy(window, watched, event):
            calls.append(window)
            return original(window, watched, event)

        def probe():
            calls.clear()
            QApplication.sendEvent(self.app, QEvent(QEvent.Type.User))
            return self.window in calls

        with patch.object(VeronaWindow, "eventFilter", spy):
            self.window.show()
            self.assertFalse(probe())
            self.window.hide()
            with patch.object(QApplication, "platformName", return_value="xcb"):
                self.window.show()
                self.assertTrue(probe())
                self.window.hide()
                self.assertFalse(probe())

    def test_another_window_does_not_repolish_the_whole_application(self):
        with patch.object(QApplication, "setStyle") as style, \
                patch.object(QApplication, "setPalette") as palette, \
                patch.object(QApplication, "setFont") as font:
            other = VeronaWindow()
        self.assertEqual((style.call_count, palette.call_count, font.call_count), (0, 0, 0))
        self.assertEqual(other.theme_name, self.window.theme_name)
        other.close()

    def test_new_tab_preserves_each_documents_state_and_close_cancel(self):
        self.window.add_rectangle()
        self.window.metadata = {"label": "first"}
        self.window.wifi_host.setText("192.0.2.10")
        self.window.change_zoom(1.2)
        first_items = copy.deepcopy(self.window.items)
        first_undo = copy.deepcopy(self.window.undo_stack)
        self.window.saved_snapshot = self.window._document_state()

        self.window.new_design()
        self.assertEqual(self.window.tab_bar.count(), 2)
        self.assertEqual(self.window.items, [])
        self.window.add_ellipse()
        self.window.metadata = {"label": "second"}
        self.window.wifi_host.setText("192.0.2.20")
        self.window.change_zoom(1.1)
        second_zoom = self.window.canvas.zoom

        self.window.tab_bar.setCurrentIndex(0)
        self.assertEqual(self.window.items, first_items)
        self.assertEqual(self.window.metadata, {"label": "first"})
        self.assertEqual(self.window.wifi_host.text(), "192.0.2.10")
        self.assertEqual(self.window.undo_stack, first_undo)
        self.assertAlmostEqual(self.window.canvas.zoom, 1.8 * 1.2)

        self.window.tab_bar.setCurrentIndex(1)
        self.assertEqual(len(self.window.items), 1)
        self.assertEqual(self.window.metadata, {"label": "second"})
        self.assertEqual(self.window.wifi_host.text(), "192.0.2.20")
        self.assertAlmostEqual(self.window.canvas.zoom, second_zoom)
        count = self.window.tab_bar.count()
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="cancel"):
            self.assertFalse(self.window.close_document(1))
        self.assertEqual(self.window.tab_bar.count(), count)
        self.assertEqual(len(self.window.items), 1)

    def test_open_reuses_initial_empty_tab(self):
        document = {"format": "verona", "version": 1, "units": "mm",
                    "items": [], "settings": {}, "metadata": {"loaded": True}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty.jdesign"
            path.write_text(json.dumps(document), encoding="utf-8")
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "Design")):
                self.window.open_design()
        self.assertEqual(self.window.tab_bar.count(), 1)
        self.assertEqual(self.window.current_file, str(path))
        self.assertEqual(self.window.metadata, {"loaded": True})

    @staticmethod
    def _image_item(left, right, name):
        qoi = (b"qoif" + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
               + bytes((4, 0, 0xff, 20, 30, 40, 255)) + b"\0\0\0\0\0\0\0\1")
        return {
            "name": name,
            "contours": [[(left, 0), (right, 0), (right, 10), (left, 0)]],
            "image_data": base64.b64encode(qoi).decode("ascii"),
            "image_transform": [left, 1, 1, 0, 0, 1],
            "image_clip": [[(left, 1), (right, 1), (right, 9), (left, 1)]],
        }

    def test_alignment_moves_cutline_image_and_mask_together(self):
        self.window.items = [self._image_item(10, 20, "a"),
                             self._image_item(30, 40, "b")]
        self.window.select_many([0, 1])
        before = copy.deepcopy(self.window.items)

        self.window.align_selected("left")

        self.assertAlmostEqual(_item_path(self.window.items[0]).boundingRect().left(),
                               _item_path(self.window.items[1]).boundingRect().left())
        for old, new in zip(before, self.window.items):
            dx = new["image_transform"][0] - old["image_transform"][0]
            self.assertAlmostEqual(new["image_transform"][1], old["image_transform"][1])
            self.assertAlmostEqual(new["image_clip"][0][0][0] - old["image_clip"][0][0][0], dx)
            self.assertAlmostEqual(_item_path(new).boundingRect().left()
                                   - _item_path(old).boundingRect().left(), dx)

    def test_distribute_three_selection_is_even_and_undoable(self):
        self.window.items = [self._image_item(0, 10, "a"),
                             self._image_item(30, 40, "b"),
                             self._image_item(70, 80, "c")]
        self.window.select_many([0, 1, 2])
        before = copy.deepcopy(self.window.items)

        self.window.align_selected("distribute_h")

        lefts = [_item_path(item).boundingRect().left() for item in self.window.items]
        gaps = [lefts[index + 1] - _item_path(self.window.items[index]).boundingRect().right()
                for index in range(2)]
        self.assertAlmostEqual(gaps[0], gaps[1])
        self.window.undo()
        self.assertEqual(self.window.items, before)

    def _wheel(self, modifiers):
        canvas = self.window.canvas
        pos = QPointF(50, 50)
        event = QWheelEvent(pos, QPointF(canvas.mapToGlobal(QPoint(50, 50))),
                            QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                            modifiers, Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(canvas, event)

    def test_only_shift_wheel_changes_canvas_zoom(self):
        canvas = self.window.canvas
        original = canvas.zoom
        self._wheel(Qt.KeyboardModifier.NoModifier)
        self.assertEqual(canvas.zoom, original)
        self._wheel(Qt.KeyboardModifier.ShiftModifier)
        self.assertNotEqual(canvas.zoom, original)

    @staticmethod
    def _send_mouse(canvas, event_type, local_pos, button, buttons, modifiers):
        global_pos = QPointF(canvas.mapToGlobal(local_pos.toPoint()))
        event = QMouseEvent(event_type, local_pos, global_pos, button, buttons, modifiers)
        QApplication.sendEvent(canvas, event)

    def test_alt_drag_duplicates_once_and_one_undo_removes_copy(self):
        self.window.add_rectangle()
        self.window.add_ellipse()
        self.window.select_many([0, 1])
        canvas = self.window.canvas
        zoom = canvas.zoom
        bounds = _item_path(self.window.items[0]).boundingRect()
        start = QPointF((bounds.center().x() * zoom) + 34,
                        (bounds.center().y() * zoom) + 34)
        end = start + QPointF(18, 0)
        alt = Qt.KeyboardModifier.AltModifier
        self._send_mouse(canvas, QMouseEvent.Type.MouseButtonPress, start,
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, alt)
        self._send_mouse(canvas, QMouseEvent.Type.MouseMove, end,
                         Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, alt)
        self._send_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, end,
                         Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, alt)
        self.assertEqual(len(self.window.items), 4)
        self.window.undo()
        self.assertEqual(len(self.window.items), 2)

    def test_alt_click_without_drag_does_not_duplicate(self):
        self.window.add_rectangle()
        canvas = self.window.canvas
        bounds = _item_path(self.window.items[0]).boundingRect()
        pos = QPointF((bounds.center().x() * canvas.zoom) + 34,
                      (bounds.center().y() * canvas.zoom) + 34)
        alt = Qt.KeyboardModifier.AltModifier
        self._send_mouse(canvas, QMouseEvent.Type.MouseButtonPress, pos,
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, alt)
        self._send_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, pos,
                         Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, alt)
        self.assertEqual(len(self.window.items), 1)

    def test_control_click_adds_and_removes_items_from_selection(self):
        self.window.add_rectangle()
        self.window.add_rectangle()
        self.window.items[1]["contours"] = [
            [(x + 70, y) for x, y in contour] for contour in self.window.items[1]["contours"]
        ]
        self.window.select(0)
        canvas = self.window.canvas
        zoom = canvas.zoom

        def ctrl_click(index):
            bounds = _item_path(self.window.items[index]).boundingRect()
            pos = QPointF(bounds.center().x() * zoom + 34,
                          bounds.center().y() * zoom + 34)
            ctrl = Qt.KeyboardModifier.ControlModifier
            self._send_mouse(canvas, QMouseEvent.Type.MouseButtonPress, pos,
                             Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, ctrl)
            self._send_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, pos,
                             Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, ctrl)

        ctrl_click(1)
        self.assertEqual(self.window.selected_indices(), {0, 1})
        ctrl_click(0)
        self.assertEqual(self.window.selected_indices(), {1})

    def test_space_drag_pans_scrollbars(self):
        self.window.resize(850, 600)
        self.window.show()
        self.window.canvas.set_zoom(4.0)
        self.app.processEvents()
        horizontal = self.window.canvas_scroll.horizontalScrollBar()
        vertical = self.window.canvas_scroll.verticalScrollBar()
        self.assertGreater(horizontal.maximum(), 100)
        self.assertGreater(vertical.maximum(), 100)
        horizontal.setValue(120)
        vertical.setValue(120)
        before = horizontal.value(), vertical.value()

        canvas = self.window.canvas
        canvas.setFocus()
        self.app.processEvents()
        QTest.keyPress(canvas, Qt.Key.Key_Space)
        start = QPointF(120, 120)
        end = QPointF(170, 155)
        self._send_mouse(canvas, QMouseEvent.Type.MouseButtonPress, start,
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        self._send_mouse(canvas, QMouseEvent.Type.MouseMove, end,
                         Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        self._send_mouse(canvas, QMouseEvent.Type.MouseButtonRelease, end,
                         Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier)
        QTest.keyRelease(canvas, Qt.Key.Key_Space)
        self.assertNotEqual((horizontal.value(), vertical.value()), before)
        self.assertEqual((horizontal.value(), vertical.value()), (70, 85))

    def test_language_switch_preserves_tabs_active_selection_history_and_sections(self):
        self.window.add_rectangle()
        self.window.new_design()
        self.window.add_rectangle()
        self.window.add_ellipse()
        self.window.select_many([0, 1])
        self.window.mark_section.setExpanded(True)
        self.window.connection_section.setExpanded(True)
        active_index = self.window.tab_bar.currentIndex()
        items = copy.deepcopy(self.window.items)
        selection = self.window.selected_indices()
        undo = copy.deepcopy(self.window.undo_stack)

        self.window._change_language("en")

        self.assertEqual(self.window.tab_bar.count(), 2)
        self.assertEqual(self.window.tab_bar.currentIndex(), active_index)
        self.assertEqual(self.window.items, items)
        self.assertEqual(self.window.selected_indices(), selection)
        self.assertEqual(self.window.undo_stack, undo)
        self.assertTrue(self.window.mark_section.isExpanded())
        self.assertTrue(self.window.connection_section.isExpanded())

    def test_close_discard_resets_closed_document_and_keeps_last_tab(self):
        self.window.add_rectangle()
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.assertTrue(self.window.close_document(0))
        self.assertEqual(self.window.tab_bar.count(), 1)
        self.assertEqual(self.window.items, [])

        self.window.add_ellipse()
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.assertTrue(self.window.close_document(0))
        self.assertEqual(self.window.tab_bar.count(), 1)
        self.assertEqual(self.window.items, [])
        self.assertIsNone(self.window.current_file)
        self.assertFalse(self.window.is_dirty())

    def test_snapshot_and_saved_baseline_do_not_share_nested_mutable_data(self):
        self.window.items = [{
            "name": "picture",
            "contours": [[(1.0, 2.0), (3.0, 4.0)]],
            "image_transform": [1.0, 2.0, 1.0, 0.0, 0.0, 1.0],
        }]
        self.window.metadata = {"nested": {"values": [1, 2]}}
        baseline = self.window._document_state()
        self.window.saved_snapshot = copy.deepcopy(baseline)
        snapshot = self.window._snapshot()

        self.window.items[0]["contours"][0][0] = (99.0, 98.0)
        self.window.items[0]["contours"][0].append((5.0, 6.0))
        self.window.items[0]["image_transform"][0] = 90.0
        self.window.metadata["nested"]["values"].append(3)

        expected_contour = [(1.0, 2.0), (3.0, 4.0)]
        for saved in (snapshot, baseline, self.window.saved_snapshot):
            self.assertEqual(saved["items"][0]["contours"][0], expected_contour)
            self.assertEqual(saved["items"][0]["image_transform"][0], 1.0)
            self.assertEqual(saved["metadata"], {"nested": {"values": [1, 2]}})

    def test_opening_second_file_keeps_first_dirty_and_reopening_switches_to_it(self):
        first = {"format": "verona", "version": 1, "units": "mm",
                 "items": [], "settings": {}, "metadata": {"doc": "first"}}
        second = {"format": "verona", "version": 1, "units": "mm",
                  "items": [], "settings": {}, "metadata": {"doc": "second"}}
        with tempfile.TemporaryDirectory() as folder:
            first_path = Path(folder) / "first.jdesign"
            second_path = Path(folder) / "second.jdesign"
            first_path.write_text(json.dumps(first), encoding="utf-8")
            second_path.write_text(json.dumps(second), encoding="utf-8")

            with patch.object(QFileDialog, "getOpenFileName",
                              return_value=(str(first_path), "Design")):
                self.window.open_design()
            self.window.add_rectangle()
            first_items = copy.deepcopy(self.window.items)
            self.assertTrue(self.window.is_dirty())

            with patch.object(QFileDialog, "getOpenFileName",
                              return_value=(str(second_path), "Design")):
                self.window.open_design()
            self.assertEqual(self.window.tab_bar.count(), 2)
            self.assertEqual(self.window.current_file, str(second_path))

            with patch.object(QFileDialog, "getOpenFileName",
                              return_value=(str(first_path), "Design")):
                self.window.open_design()
            self.assertEqual(self.window.tab_bar.count(), 2)
            self.assertEqual(self.window.tab_bar.currentIndex(), 0)
            self.assertEqual(self.window.items, first_items)
            self.assertEqual(self.window.metadata, {"doc": "first"})
            self.assertTrue(self.window.is_dirty())

    def test_all_inspector_sections_collapse_and_keep_state_across_language_changes(self):
        names = ("layer_section", "path_section", "transform_section", "alignment_section",
                 "view_section", "cut_section", "mark_section", "connection_section")
        for index, name in enumerate(names):
            getattr(self.window, name).setExpanded(index % 2 == 1)
        before = copy.deepcopy(self.window._document_state())
        self.window._change_language("en")
        for index, name in enumerate(names):
            section = getattr(self.window, name)
            self.assertEqual(section.isExpanded(), index % 2 == 1)
            self.assertEqual(section._content.isHidden(), index % 2 == 0)
        self.assertEqual(self.window._document_state(), before)


    def test_window_discard_closes_dirty_tabs_without_restoring_or_saving_them(self):
        self.window.add_rectangle()
        self.window.current_file = "first.lds"
        self.window.new_design()
        self.window.add_ellipse()
        self.window.current_file = "second.lds"
        before = copy.deepcopy(self.window._snapshot())
        event = QCloseEvent()
        with patch.object(self.window, "_restore_tab_state", side_effect=AssertionError("Must not redraw discarded tabs")), \
                patch.object(self.window, "_document_state", side_effect=AssertionError("Must not copy discarded artwork")), \
                patch.object(self.window, "save_design") as save, \
                patch.object(self.window, "_confirm_unsaved_changes", return_value="discard") as confirm:
            self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual([call.args for call in confirm.call_args_list], [("first.lds",), ("second.lds",)])
        save.assert_not_called()
        self.assertEqual(self.window._snapshot(), before)

    def test_window_close_cancel_preserves_both_dirty_tabs_and_active_document(self):
        self.window.add_rectangle()
        self.window.new_design()
        self.window.add_ellipse()
        before = copy.deepcopy(self.window._snapshot())
        event = QCloseEvent()
        with patch.object(self.window, "_confirm_unsaved_changes", side_effect=["discard", "cancel"]):
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(self.window.tab_bar.currentIndex(), 1)
        self.assertEqual(self.window._snapshot(), before)
        self.window.tab_bar.setCurrentIndex(0)
        self.assertTrue(self.window.is_dirty())
        self.assertFalse(self.window._closing)

    def test_window_close_reentry_during_confirmation_is_ignored(self):
        self.window.add_rectangle()
        nested, outer = QCloseEvent(), QCloseEvent()

        def confirm(_filename):
            self.window.closeEvent(nested)
            return "discard"

        with patch.object(self.window, "_confirm_unsaved_changes", side_effect=confirm) as prompt:
            self.window.closeEvent(outer)
        prompt.assert_called_once()
        self.assertFalse(nested.isAccepted())
        self.assertTrue(outer.isAccepted())
        self.assertFalse(self.window._closing)

    def test_clean_window_closes_without_copying_or_restoring_tabs(self):
        self.window.add_rectangle()
        self.window.saved_snapshot = self.window._document_state()
        self.window.new_design()
        event = QCloseEvent()
        with patch.object(self.window, "_restore_tab_state", side_effect=AssertionError("Must not restore clean tabs")), \
                patch.object(self.window, "_document_state", side_effect=AssertionError("Must not copy clean artwork")), \
                patch.object(self.window, "_confirm_unsaved_changes") as prompt:
            self.window.closeEvent(event)
        self.assertTrue(event.isAccepted())
        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()

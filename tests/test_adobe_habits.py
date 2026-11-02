"""Adobe-familiar canvas and editing controls use the live editor entry points."""
import copy
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from verona.i18n import set_language, tr
from verona.ui import VeronaWindow, _item_path


def rectangle(name="object", x=20, y=20, width=20, height=10, **extra):
    return {"name": name, "cut": True,
            "contours": [[(x, y), (x + width, y), (x + width, y + height),
                          (x, y + height), (x, y)]], **extra}


class AdobeHabitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = tempfile.TemporaryDirectory(prefix="verona-adobe-settings-")
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          cls.settings.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings.cleanup()

    def setUp(self):
        set_language("zh_TW")
        QSettings("Verona", "Workspace").clear()
        self.window = VeronaWindow(host="")
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def install(self, items, selection=(0,)):
        self.window.items = copy.deepcopy(items)
        self.window.undo_stack.clear()
        self.window.redo_stack.clear()
        self.window.select_many(selection)
        self.window.refresh_all()

    @staticmethod
    def key(widget, key, modifiers=Qt.KeyboardModifier.NoModifier, *, repeat=False):
        event = QKeyEvent(QKeyEvent.Type.KeyPress, key, modifiers, "", repeat, 1)
        QApplication.sendEvent(widget, event)
        return event

    def bounds(self, index=0):
        return _item_path(self.window.items[index]).boundingRect()

    def test_arrow_nudges_move_point_one_mm_and_shift_moves_one_mm(self):
        self.install([rectangle()])
        canvas = self.window.canvas
        self.key(canvas, Qt.Key.Key_Right)
        self.assertAlmostEqual(self.bounds().left(), 20.1)
        QTest.qWait(500)
        self.key(canvas, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
        self.assertAlmostEqual(self.bounds().top(), 21)

    def test_arrow_nudge_can_be_undone_before_repeat_timer_expires(self):
        original = [rectangle()]
        self.install(original)
        self.key(self.window.canvas, Qt.Key.Key_Right)
        self.assertTrue(self.window.undo_action.isEnabled())
        self.window.undo()
        self.assertEqual(self.window.items, original)

    def test_arrow_auto_repeat_events_make_one_undo_step(self):
        original = [rectangle()]
        self.install(original)
        canvas = self.window.canvas
        self.key(canvas, Qt.Key.Key_Right)
        for _ in range(4):
            self.key(canvas, Qt.Key.Key_Right, repeat=True)
        self.assertAlmostEqual(self.bounds().left(), 20.5)
        QTest.qWait(600)
        self.assertEqual(len(self.window.undo_stack), 1)
        self.window.undo()
        self.assertEqual(self.window.items, original)
        self.window.redo()
        self.assertAlmostEqual(self.bounds().left(), 20.5)

    def test_arrow_keys_in_a_double_spinbox_stay_with_the_field(self):
        self.install([rectangle()])
        before = copy.deepcopy(self.window.items)
        self.window.width_spin.setFocus()
        self.app.processEvents()
        self.key(self.window.width_spin, Qt.Key.Key_Right)
        self.assertEqual(self.window.items, before)
        self.assertEqual(self.window.undo_stack, [])

    def test_locked_object_does_not_move_with_arrow_keys(self):
        self.install([rectangle(locked=True)])
        before = copy.deepcopy(self.window.items)
        self.key(self.window.canvas, Qt.Key.Key_Right)
        QTest.qWait(500)
        self.assertEqual(self.window.items, before)
        self.assertEqual(self.window.undo_stack, [])

    def test_copy_cut_paste_and_paste_in_place_work_across_tabs_and_undo(self):
        self.install([rectangle("source")])
        self.window.copy_action.trigger()
        self.window.new_design()
        self.window.paste_action.trigger()
        self.assertEqual(len(self.window.items), 1)
        self.assertAlmostEqual(self.bounds().left(), 25)
        self.assertAlmostEqual(self.bounds().top(), 25)
        self.window.undo()
        self.assertEqual(self.window.items, [])
        self.window.paste_in_place_action.trigger()
        self.assertAlmostEqual(self.bounds().left(), 20)
        self.assertAlmostEqual(self.bounds().top(), 20)
        self.window.undo()
        self.window.tab_bar.setCurrentIndex(0)
        self.window.cut_action.trigger()
        self.assertEqual(self.window.items, [])
        self.window.undo()
        self.assertEqual(self.window.items[0]["name"], "source")
        self.assertEqual(self.bounds().left(), 20)

    def test_backspace_deletes_selected_object_undoably(self):
        original = [rectangle()]
        self.install(original)
        self.key(self.window.canvas, Qt.Key.Key_Backspace)
        self.assertEqual(self.window.items, [])
        self.window.undo()
        self.assertEqual(self.window.items, original)

    def wheel(self, point, modifiers, delta=120):
        canvas = self.window.canvas
        viewport = self.window.canvas_scroll.viewport()
        position = QPointF(canvas.mapFrom(viewport, point))
        event = QWheelEvent(position, QPointF(canvas.mapToGlobal(position.toPoint())),
                            QPoint(0, 0), QPoint(0, delta), Qt.MouseButton.NoButton,
                            modifiers, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(canvas, event)
        self.app.processEvents()
        return event

    def test_ctrl_and_alt_wheel_zoom_at_cursor_while_shift_wheel_scrolls_horizontally(self):
        canvas = self.window.canvas
        viewport = self.window.canvas_scroll.viewport()
        point = QPoint(180, 140)
        for modifier in (Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.AltModifier):
            canvas.set_zoom(3.0)
            self.window._fit_page_height = False
            self.app.processEvents()
            self.window.canvas_scroll.horizontalScrollBar().setValue(100)
            self.window.canvas_scroll.verticalScrollBar().setValue(100)
            world = canvas._world_point(QPointF(canvas.mapFrom(viewport, point)))
            self.wheel(point, modifier)
            self.assertAlmostEqual(canvas.zoom, 3.75)
            mapped = canvas.mapTo(viewport,
                                  (canvas.canvas_origin() + world * canvas.zoom).toPoint())
            self.assertLessEqual(abs(mapped.x() - point.x()), 2)
            self.assertLessEqual(abs(mapped.y() - point.y()), 2)

        canvas.set_zoom(4.0)
        self.window._fit_page_height = False
        self.app.processEvents()
        bar = self.window.canvas_scroll.horizontalScrollBar()
        bar.setValue(min(100, bar.maximum()))
        before, zoom = bar.value(), canvas.zoom
        event = self.wheel(point, Qt.KeyboardModifier.ShiftModifier)
        self.assertTrue(event.isAccepted())
        self.assertEqual(canvas.zoom, zoom)
        self.assertLess(bar.value(), before)

    def test_zoom_actions_expose_adobe_shortcuts_and_100_percent(self):
        self.window._fit_page_height = False
        self.window.canvas.set_zoom(2.0)
        self.window.zoom_in_action.trigger()
        self.assertAlmostEqual(self.window.canvas.zoom, 2.5)
        self.window.zoom_out_action.trigger()
        self.assertAlmostEqual(self.window.canvas.zoom, 2.0)
        self.window.zoom_100_action.trigger()
        self.assertEqual(self.window.canvas.zoom, 1.0)
        self.assertIn("Ctrl+=", [shortcut.toString() for shortcut in
                                  self.window.zoom_in_action.shortcuts()])
        self.assertIn("Ctrl++", [shortcut.toString() for shortcut in
                                  self.window.zoom_in_action.shortcuts()])
        self.assertIn("Ctrl+-", [shortcut.toString() for shortcut in
                                  self.window.zoom_out_action.shortcuts()])
        self.assertEqual(self.window.zoom_100_action.shortcut().toString(), "Ctrl+1")

    def test_layer_order_actions_move_one_or_all_the_way_and_undo(self):
        original = [rectangle(name, x=index * 30) for index, name in enumerate("abc")]
        cases = ((self.window.bring_forward_action, ["a", "c", "b"]),
                 (self.window.send_backward_action, ["b", "a", "c"]),
                 (self.window.bring_to_front_action, ["a", "c", "b"]),
                 (self.window.send_to_back_action, ["b", "a", "c"]))
        self.assertEqual(self.window.bring_forward_action.shortcut().toString(), "Ctrl+]")
        self.assertEqual(self.window.send_backward_action.shortcut().toString(), "Ctrl+[")
        self.assertEqual(self.window.bring_to_front_action.shortcut().toString(), "Ctrl+Shift+]")
        self.assertEqual(self.window.send_to_back_action.shortcut().toString(), "Ctrl+Shift+[")
        for action, expected in cases:
            with self.subTest(action=action.text()):
                self.install(original, (1,))
                action.trigger()
                self.assertEqual([item["name"] for item in self.window.items], expected)
                self.window.undo()
                self.assertEqual(self.window.items, original)

    def test_escape_and_deselect_action_clear_selection_without_history(self):
        self.install([rectangle(), rectangle("other", 60, 20)], (0,))
        self.key(self.window.canvas, Qt.Key.Key_Escape)
        self.assertEqual(self.window.selected_indices(), set())
        self.window.select(0)
        self.window.deselect_action.trigger()
        self.assertEqual(self.window.selected_indices(), set())
        self.assertEqual(self.window.undo_stack, [])

    def test_inspector_enter_applies_name_and_dimensions_with_undo(self):
        original = [rectangle("before")]
        self.install(original)
        self.window.object_name.setText("entered")
        QTest.keyClick(self.window.object_name, Qt.Key.Key_Return)
        self.assertEqual(self.window.items[0]["name"], "entered")
        self.window.undo()
        self.assertEqual(self.window.items, original)

        self.window.width_spin.setFocus()
        self.window.width_spin.setValue(30)
        QTest.keyClick(self.window.width_spin, Qt.Key.Key_Return)
        self.assertAlmostEqual(self.bounds().width(), 30)
        self.assertAlmostEqual(self.bounds().height(), 15)
        self.window.undo()
        self.assertEqual(self.window.items, original)
        self.assertFalse(any(button.text() == tr("套用尺寸")
                             for button in self.window.selection_edit_buttons))

    def test_inspector_focus_out_applies_position_with_undo(self):
        original = [rectangle()]
        self.install(original)
        self.window.x_spin.setValue(27)
        self.window.x_spin.setFocus()
        self.app.processEvents()
        self.window.canvas.setFocus()
        self.app.processEvents()
        self.assertAlmostEqual(self.bounds().left(), 27)
        self.assertEqual(len(self.window.undo_stack), 1)
        self.window.undo()
        self.assertEqual(self.window.items, original)

    def test_operation_tooltips_have_matching_english_text(self):
        self.window._change_language("en")
        self.assertIn("Shift-wheel scrolls horizontally", self.window.canvas.toolTip())
        self.assertIn("Press Enter or leave", self.window.path_section.toolTip())
        self.assertNotIn("Shift-wheel to zoom", self.window.path_section.toolTip())


if __name__ == "__main__":
    unittest.main()

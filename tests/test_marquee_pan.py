"""Offscreen regression tests for canvas marquee selection and held-space panning."""

import copy
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt, QSettings
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from verona.i18n import set_language
from verona.ui import VeronaWindow


class MarqueePanTests(unittest.TestCase):
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
    def _rect(name, left, top, right, bottom, **extra):
        return {"name": name,
                "contours": [[(left, top), (right, top), (right, bottom),
                              (left, bottom), (left, top)]], **extra}

    def _send_mouse(self, event_type, world, *, button=Qt.MouseButton.NoButton,
                    buttons=Qt.MouseButton.NoButton, modifiers=Qt.KeyboardModifier.NoModifier):
        canvas = self.window.canvas
        local = QPointF(34 + world.x() * canvas.zoom, 34 + world.y() * canvas.zoom)
        global_pos = QPointF(canvas.mapToGlobal(local.toPoint()))
        event = QMouseEvent(event_type, local, global_pos, button, buttons, modifiers)
        QApplication.sendEvent(canvas, event)

    def _marquee(self, start, end, modifiers=Qt.KeyboardModifier.NoModifier):
        self._send_mouse(QMouseEvent.Type.MouseButtonPress, start,
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton,
                         modifiers=modifiers)
        self._send_mouse(QMouseEvent.Type.MouseMove, end,
                         buttons=Qt.MouseButton.LeftButton, modifiers=modifiers)
        self._send_mouse(QMouseEvent.Type.MouseButtonRelease, end,
                         button=Qt.MouseButton.LeftButton, modifiers=modifiers)

    def _send_key(self, event_type, key, *, auto_repeat=False):
        event = QKeyEvent(event_type, key, Qt.KeyboardModifier.NoModifier,
                          "", auto_repeat, 1)
        QApplication.sendEvent(self.window.canvas, event)

    def test_forward_and_reverse_marquee_select_same_items_without_editing_document(self):
        self.window.items = [self._rect("a", 20, 20, 30, 30),
                             self._rect("b", 60, 50, 70, 60),
                             self._rect("outside", 100, 100, 110, 110)]
        before = copy.deepcopy(self.window.items)
        self.window.saved_snapshot = self.window._document_state()

        self._marquee(QPointF(10, 10), QPointF(80, 70))
        self.assertEqual(self.window.selected_indices(), {0, 1})
        self._marquee(QPointF(80, 70), QPointF(10, 10))
        self.assertEqual(self.window.selected_indices(), {0, 1})
        self.assertEqual(self.window.items, before)
        self.assertEqual(self.window.undo_stack, [])
        self.assertFalse(self.window.is_dirty())

    def test_additive_marquee_expands_group_ignores_guide_and_escape_restores_selection(self):
        self.window.items = [
            self._rect("group member in frame", 20, 20, 30, 30,
                       group_path=["outer", "inner"]),
            self._rect("group member outside frame", 100, 100, 110, 110,
                       group_path=["outer"]),
            self._rect("registration", 40, 40, 50, 50,
                       registration_guide=True),
            self._rect("base selection", 70, 20, 80, 30),
        ]
        before = copy.deepcopy(self.window.items)
        self.window.saved_snapshot = self.window._document_state()
        self.window.select(3)

        self._marquee(QPointF(10, 10), QPointF(60, 60), Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.window.selected_indices(), {0, 1, 3})
        self.assertEqual(self.window.items, before)
        self.assertEqual(self.window.undo_stack, [])
        self.assertFalse(self.window.is_dirty())

        self.window.select(3)
        self._send_mouse(QMouseEvent.Type.MouseButtonPress, QPointF(10, 10),
                         button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
        self._send_mouse(QMouseEvent.Type.MouseMove, QPointF(60, 60),
                         buttons=Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.selected_indices(), {0, 1})
        self._send_key(QEvent.Type.KeyPress, Qt.Key.Key_Escape)
        self.assertEqual(self.window.selected_indices(), {3})
        self.assertIsNone(self.window.canvas.marquee_start)
        self.assertEqual(self.window.items, before)
        self.assertEqual(self.window.undo_stack, [])
        self.assertFalse(self.window.is_dirty())

    def test_space_pan_survives_autorepeat_releases_hover_and_repeated_drags(self):
        self.window.resize(850, 600)
        self.window.show()
        self.window.canvas.set_zoom(4.0)
        self.app.processEvents()
        self.window.items = [self._rect("kept", 10, 10, 20, 20)]
        self.window.select(0)
        self.window.saved_snapshot = self.window._document_state()
        items_before = copy.deepcopy(self.window.items)
        undo_before = copy.deepcopy(self.window.undo_stack)

        horizontal = self.window.canvas_scroll.horizontalScrollBar()
        vertical = self.window.canvas_scroll.verticalScrollBar()
        horizontal.setValue(300)
        vertical.setValue(250)
        self.window.canvas.setFocus()
        self._send_key(QEvent.Type.KeyPress, Qt.Key.Key_Space)
        self.assertTrue(self.window.canvas.space_pressed)
        self.assertEqual(self.window.canvas.cursor().shape(), Qt.CursorShape.OpenHandCursor)

        def pan(start, end):
            self._send_mouse(QMouseEvent.Type.MouseButtonPress, start,
                             button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
            self._send_mouse(QMouseEvent.Type.MouseMove, end,
                             buttons=Qt.MouseButton.LeftButton)
            self._send_mouse(QMouseEvent.Type.MouseButtonRelease, end,
                             button=Qt.MouseButton.LeftButton)

        pan(QPointF(120, 120), QPointF(145, 135))
        first_position = horizontal.value(), vertical.value()
        self._send_key(QEvent.Type.KeyRelease, Qt.Key.Key_Space, auto_repeat=True)
        self.assertTrue(self.window.canvas.space_pressed)
        self.assertEqual(self.window.canvas.cursor().shape(), Qt.CursorShape.OpenHandCursor)
        self._send_key(QEvent.Type.KeyPress, Qt.Key.Key_Space, auto_repeat=True)
        self.assertTrue(self.window.canvas.space_pressed)
        self._send_mouse(QMouseEvent.Type.MouseMove, QPointF(160, 140))
        self.assertEqual(self.window.canvas.cursor().shape(), Qt.CursorShape.OpenHandCursor)

        pan(QPointF(130, 100), QPointF(160, 120))
        second_position = horizontal.value(), vertical.value()
        self.assertNotEqual(first_position, second_position)
        self.assertLess(second_position[0], first_position[0])
        self.assertLess(second_position[1], first_position[1])
        self.assertTrue(self.window.canvas.space_pressed)

        self._send_key(QEvent.Type.KeyRelease, Qt.Key.Key_Space)
        self.assertFalse(self.window.canvas.space_pressed)
        self.assertEqual(self.window.canvas.cursor().shape(), Qt.CursorShape.ArrowCursor)
        self.assertEqual(self.window.items, items_before)
        self.assertEqual(self.window.undo_stack, undo_before)
        self.assertFalse(self.window.is_dirty())


if __name__ == "__main__":
    unittest.main()

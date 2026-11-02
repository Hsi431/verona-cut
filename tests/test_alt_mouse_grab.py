"""Regression tests for releasing the X11 Alt mouse grab safely."""

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QSettings, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from verona.i18n import set_language
from verona.ui import VeronaWindow


class AltMouseGrabTests(unittest.TestCase):
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
        set_language("en")
        self.window = VeronaWindow(host="")
        self.window.resize(900, 650)
        self.window.show()
        self.app.processEvents()
        self.window._alt_mouse_grab = True

    def tearDown(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def _send_mouse(self, event_type, global_pos, *, modifiers=Qt.KeyboardModifier.NoModifier,
                    button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton):
        local = QPointF(self.window.canvas.mapFromGlobal(global_pos.toPoint()))
        event = QMouseEvent(event_type, local, global_pos, button, buttons, modifiers)
        with patch.object(QWidget, "mouseGrabber", return_value=self.window.canvas), \
                patch.object(self.window.canvas, "releaseMouse") as release_mouse:
            filtered = self.window.eventFilter(self.window.canvas, event)
        return filtered, release_mouse

    def test_meta_key_release_without_alt_releases_canvas_grab(self):
        event = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Meta,
                          Qt.KeyboardModifier.NoModifier)
        with patch.object(QWidget, "mouseGrabber", return_value=self.window.canvas), \
                patch.object(self.window.canvas, "releaseMouse") as release_mouse, \
                patch.object(QApplication, "queryKeyboardModifiers",
                             return_value=Qt.KeyboardModifier.NoModifier):
            self.window.eventFilter(self.window.canvas, event)
        self.assertFalse(self.window._alt_mouse_grab)
        release_mouse.assert_called_once()

    def test_alt_press_keeps_grab_and_unmodified_click_reaches_print_button(self):
        button_center = self.window.print_button.rect().center()
        global_pos = QPointF(self.window.print_button.mapToGlobal(button_center))
        received_clicks = []
        action = self.window.print_button.defaultAction()
        action.triggered.disconnect()
        action.triggered.connect(lambda _checked=False: received_clicks.append(True))

        self._send_mouse(QMouseEvent.Type.MouseButtonPress, global_pos,
                         modifiers=Qt.KeyboardModifier.AltModifier)
        self.assertTrue(self.window._alt_mouse_grab)
        self.assertEqual(received_clicks, [])

        press_handled, release_mouse = self._send_mouse(
            QMouseEvent.Type.MouseButtonPress, global_pos)
        self.assertFalse(self.window._alt_mouse_grab)
        release_mouse.assert_called_once()
        self.assertTrue(press_handled)

        release = QMouseEvent(
            QMouseEvent.Type.MouseButtonRelease,
            QPointF(self.window.canvas.mapFromGlobal(global_pos.toPoint())),
            global_pos, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier)
        self.window.eventFilter(self.window.canvas, release)
        self.assertEqual(received_clicks, [True])


if __name__ == "__main__":
    unittest.main()

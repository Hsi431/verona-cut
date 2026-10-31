"""GUI stop controls never imply that a cancelled transfer is complete."""
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QSettings
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from verona.i18n import set_language
from verona.transport import TransferCancelled
from verona.ui import VeronaWindow


class TransferControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_dir = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          cls.settings_dir.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def setUp(self):
        self.state_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"VERONA_HOST": "", "XDG_STATE_HOME": self.state_dir.name})
        self.env.start()
        QSettings("Verona", "Workspace").clear()
        set_language("zh_TW")
        self.window = VeronaWindow(host="192.0.2.1", port=8080)
        self.window.add_rectangle()
        self.window._confirm_send = lambda *_args: True
        self.dispatches = []
        self.background_dispatch = self.window._dispatch
        self.window._dispatch = lambda function, succeeded, failed: self.dispatches.append(
            (function, succeeded, failed))

    def tearDown(self):
        self.window._confirm_unsaved_changes = lambda *_args: "discard"
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.env.stop()
        self.state_dir.cleanup()
        set_language("zh_TW")

    def start_send(self, mode, transport_mock):
        self.window.connection_mode.setCurrentIndex(
            self.window.connection_mode.findData(mode))
        if mode == "usb":
            from types import SimpleNamespace
            self.window.usb_devices = [SimpleNamespace(bus=1, address=2)]
            self.window.usb_combo.clear()
            self.window.usb_combo.addItem("Mock Juliet", 0)
            self.window.usb_combo.setCurrentIndex(0)
        self.window.send_to_cutter()
        self.assertEqual(len(self.dispatches), 1)
        function, succeeded, failed = self.dispatches.pop()
        self.assertTrue(self.window.sending)
        self.assertFalse(self.window.send_button.isEnabled())
        with patch("verona.transport.send_tcp" if mode == "wifi" else "verona.transport.send_usb",
                   transport_mock):
            result = function()
        return result, succeeded, failed, transport_mock

    def test_wifi_and_usb_receive_the_active_cancel_event_and_hold_busy_until_callback(self):
        for mode in ("wifi", "usb"):
            with self.subTest(mode=mode):
                sender = unittest.mock.Mock(return_value=12)
                result, succeeded, _failed, sender = self.start_send(mode, sender)
                cancel_event = self.window._send_cancel_event
                self.assertIsNotNone(cancel_event)
                self.assertFalse(cancel_event.is_set())
                self.assertIs(sender.call_args.kwargs["cancel_event"], cancel_event)
                if mode == "wifi":
                    self.assertTrue(callable(sender.call_args.kwargs["on_progress"]))
                self.assertEqual(result, 12)
                self.assertTrue(self.window.sending)
                with patch.object(QMessageBox, "information") as info, \
                        patch.object(QMessageBox, "warning") as warning:
                    succeeded(result)
                info.assert_not_called()
                warning.assert_not_called()
                self.assertFalse(self.window.sending)
                self.assertTrue(self.window.send_button.isEnabled())
                self.assertEqual(self.window.status_label.text(), "已傳送 12 bytes")

    def test_stop_keeps_send_busy_and_success_race_is_reported_as_stopped(self):
        sender = unittest.mock.Mock(return_value=12)
        _result, succeeded, _failed, _sender = self.start_send("wifi", sender)
        pending = self.window._send_cancel_event
        with patch.object(self.window, "_remember_wifi") as remember, \
                patch.object(QMessageBox, "warning") as warning:
            self.window.stop_send_button.click()
            self.assertTrue(pending.is_set())
            self.assertTrue(self.window.sending)
            self.assertFalse(self.window.send_button.isEnabled())
            self.assertFalse(self.window.stop_send_button.isEnabled())
            self.assertIn("正在停止", self.window.status_label.text())
            succeeded(12)
            remember.assert_not_called()
            warning.assert_called_once()
        self.assertFalse(self.window.sending)
        self.assertEqual(self.window.status_label.text(), "已停止傳送")
        self.assertNotIn("已傳送", self.window.status_label.text())

    def test_language_switch_keeps_stop_control_visible_and_fitted(self):
        self.window.sending = True
        self.window._send_cancel_event = threading.Event()
        self.window._send_cancel_event.set()
        self.window.resize(1000, 720)
        self.window.show()
        for language in ("en", "zh_TW"):
            self.window._change_language(language)
            self.app.processEvents()
            button = self.window.stop_send_button
            self.assertTrue(self.window.sending)
            self.assertTrue(button.isVisible())
            self.assertFalse(button.isEnabled())
            self.assertTrue(self.window._send_cancel_event.is_set())
            self.assertGreater(button.width(), 0)
            self.assertLessEqual(button.geometry().right(), button.parentWidget().rect().right())
            self.assertIn("停止" if language == "zh_TW" else "stopping",
                          self.window.status_label.text().lower())

    def test_close_accept_sets_cancel_but_cancelled_close_does_not(self):
        self.window.sending = True
        self.window._send_cancel_event = threading.Event()
        self.window._confirm_unsaved_changes = lambda *_args: "discard"
        accepted = QCloseEvent()
        self.window.closeEvent(accepted)
        self.assertTrue(accepted.isAccepted())
        self.assertTrue(self.window._send_cancel_event.is_set())

        self.window._closed = False
        self.window.sending = True
        self.window._send_cancel_event = threading.Event()
        self.window.add_rectangle()
        self.window._confirm_unsaved_changes = lambda *_args: "cancel"
        rejected = QCloseEvent()
        self.window.closeEvent(rejected)
        self.assertFalse(rejected.isAccepted())
        self.assertFalse(self.window._send_cancel_event.is_set())
        self.assertTrue(self.window.sending)

    def test_real_background_dispatch_waits_for_worker_cancel_callback_without_retry(self):
        self.window._dispatch = self.background_dispatch
        entered_transport = threading.Event()
        release_transport = threading.Event()

        def wait_then_cancel(payload, host, port, *, on_progress=None, cancel_event=None):
            entered_transport.set()
            if not release_transport.wait(3):
                raise AssertionError("test did not release mocked transport")
            if cancel_event is None or not cancel_event.is_set():
                raise AssertionError("stop event was not forwarded")
            raise TransferCancelled("mocked transfer cancelled", 0, len(payload))

        self.window.wifi_host.setText("192.0.2.1")
        self.window.connection_mode.setCurrentIndex(
            self.window.connection_mode.findData("wifi"))
        with patch("verona.transport.send_tcp", side_effect=wait_then_cancel) as sender, \
                patch.object(QMessageBox, "warning") as warning:
            self.window.send_to_cutter()
            self.assertTrue(entered_transport.wait(2), "background worker did not start")
            pending = self.window._send_cancel_event
            self.window.stop_send_button.click()
            self.assertTrue(pending.is_set())
            self.assertTrue(self.window.sending)
            self.assertFalse(self.window.send_button.isEnabled())
            self.assertEqual(len(self.window._tasks), 1)
            release_transport.set()

            deadline = time.monotonic() + 3
            while self.window._tasks and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)
            self.app.processEvents()
            self.assertFalse(self.window._tasks, "worker callback did not finish")
            sender.assert_called_once()
            warning.assert_called_once()

        self.assertFalse(self.window.sending)
        self.assertTrue(self.window.send_button.isEnabled())
        self.assertEqual(self.window.status_label.text(), "已停止傳送")


if __name__ == "__main__":
    unittest.main()

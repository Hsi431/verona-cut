"""System information stays asynchronous, read-only and safe to copy."""
import copy
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QSettings, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from verona.device_lock import device_lock
from verona.i18n import set_language
from verona.system_info import SystemInfoDialog
from verona.themes import THEMES
from verona.ui import VeronaWindow


INFO = {"identity": {"model": "SS12", "software_version": "V22.0613",
        "hardware_version": "V7.1202_4300", "bs_version": "example-version",
        "date_code": "220613", "program_header": "PGSR_SSA"},
        "identifier_fields_omitted": ["MID", "NUM", "PID"]}


class SystemInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.folder.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"VERONA_HOST": "", "XDG_STATE_HOME": self.folder.name}))
        QSettings("Verona", "Workspace").clear()
        set_language("zh_TW")
        self.window = VeronaWindow(host="192.0.2.10", port=8081)
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject()
            dialog.deleteLater()
        self.window._confirm_unsaved_changes = lambda *_args: "discard"
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        set_language("zh_TW")

    def dialog(self):
        dialog = SystemInfoDialog(self.window)
        self.dialogs.append(dialog)
        return dialog

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(condition())

    def test_both_gui_entries_open_the_same_modal_with_explicit_refresh(self):
        with patch("verona.system_info.SystemInfoDialog") as factory, \
                patch("socket.create_connection") as connect:
            for entry in (self.window.system_info_action, self.window.system_info_button):
                factory.reset_mock()
                if hasattr(entry, "trigger"):
                    entry.trigger()
                else:
                    entry.click()
                factory.assert_called_once_with(self.window)
                dialog = factory.return_value
                self.assertEqual([call[0] for call in dialog.method_calls], ["refresh", "exec", "deleteLater"])
            connect.assert_not_called()
        self.window.connection_section.setExpanded(True)
        self.assertFalse(self.window.system_info_button.isHidden())
        self.window.connection_mode.setCurrentIndex(self.window.connection_mode.findData("usb"))
        self.assertTrue(self.window.system_info_button.isHidden())

    def test_invalid_or_missing_endpoint_does_not_dispatch_or_connect(self):
        for host in ("", "invalid", "255.255.255.255", "224.0.0.1"):
            self.window.wifi_host.setText(host)
            with self.subTest(host=host), patch.object(self.window, "_dispatch") as dispatch, \
                    patch("socket.create_connection") as connect:
                dialog = self.dialog()
                dialog.refresh()
                self.assertIn("有效 Wi-Fi IP", dialog.status.text())
                self.assertFalse(dialog.copy_button.isEnabled())
                self.assertTrue(dialog.refresh_button.isEnabled())
                dispatch.assert_not_called()
                connect.assert_not_called()

    def test_loading_ignores_duplicate_clicks_then_displays_only_known_fields(self):
        dialog = self.dialog()
        with patch.object(self.window, "_dispatch") as dispatch:
            dialog.refresh()
            dialog.refresh()
        dispatch.assert_called_once()
        self.assertFalse(dialog.refresh_button.isEnabled())
        self.assertFalse(dialog.copy_button.isEnabled())
        self.assertIn("正在讀取", dialog.status.text())
        task, success, _failure = dispatch.call_args.args
        with patch("verona.transport.query_tcp_info", return_value=INFO) as query:
            result = task()
        query.assert_called_once_with("192.0.2.10", 8081)
        result = copy.deepcopy(result)
        result["identity"]["serial_number"] = "do-not-copy-this"
        success(result)
        self.assertTrue(dialog.refresh_button.isEnabled())
        self.assertTrue(dialog.copy_button.isEnabled())
        self.assertEqual(dialog.values["software_version"].text(), "V22.0613")
        self.assertEqual(dialog.info, INFO["identity"])
        for label in dialog.values.values():
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
            self.assertTrue(label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse)

    def test_copy_excludes_endpoint_and_identifiers_and_keeps_unverified_values_raw(self):
        dialog = self.dialog()
        dialog._ready({"identity": {**INFO["identity"], "MID": "private-machine", "NUM": "private-number"}})
        with patch("verona.system_info.QApplication.clipboard") as clipboard:
            dialog.copy_info()
        report = clipboard.return_value.setText.call_args.args[0]
        self.assertIn("韌體版本: V22.0613", report)
        self.assertIn("日期代碼（MDATE）: 220613", report)
        self.assertIn("版本欄位（BSVER）: example-version", report)
        for excluded in ("192.0.2.10", "8081", "private-machine", "private-number", "MID", "NUM", "PID"):
            self.assertNotIn(excluded, report)

    def test_failed_refresh_clears_previous_results_and_retry_can_succeed(self):
        dialog = self.dialog()
        dialog._ready(INFO)
        with patch.object(self.window, "_dispatch") as dispatch:
            dialog.refresh()
        self.assertIsNone(dialog.info)
        self.assertTrue(all(label.text() == "—" for label in dialog.values.values()))
        failure = dispatch.call_args.args[2]
        failure("connection refused")
        self.assertIn("connection refused", dialog.status.text())
        self.assertFalse(dialog.copy_button.isEnabled())
        self.assertTrue(dialog.refresh_button.isEnabled())
        with patch.object(self.window, "_dispatch") as dispatch:
            dialog.refresh()
        dispatch.call_args.args[1]({"identity": {key: INFO["identity"][key]
            for key in ("model", "software_version", "hardware_version")}})
        self.assertEqual(dialog.values["bs_version"].text(), "未回報")
        self.assertTrue(dialog.copy_button.isEnabled())

    def test_busy_device_lock_prevents_a_query_without_retries(self):
        def dispatch(task, success, failure):
            try:
                success(task())
            except Exception as error:
                failure(str(error))
        dialog = self.dialog()
        with device_lock(), patch.object(self.window, "_dispatch", side_effect=dispatch), \
                patch("verona.transport.query_tcp_info") as query:
            dialog.refresh()
        query.assert_not_called()
        self.assertIn("device lock", dialog.status.text())
        self.assertFalse(dialog.copy_button.isEnabled())

    def test_callbacks_after_close_and_deletion_do_not_touch_widgets(self):
        dialog = self.dialog()
        with patch.object(self.window, "_dispatch") as dispatch:
            dialog.refresh()
        _task, success, failure = dispatch.call_args.args
        dialog.reject()
        self.dialogs.remove(dialog)
        dialog.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        success(INFO)
        failure("late failure")

    def test_real_background_dispatch_keeps_ui_responsive_and_preserves_the_design(self):
        self.window.add_rectangle()
        before = copy.deepcopy((self.window._snapshot(), self.window._documents, self.window.saved_snapshot))
        started, release = threading.Event(), threading.Event()
        threads = []

        def query(*_args):
            threads.append(threading.current_thread())
            started.set()
            if not release.wait(2):
                raise TimeoutError("test worker timed out")
            return INFO

        dialog = self.dialog()
        with patch("verona.transport.query_tcp_info", side_effect=query) as query_mock, \
                patch("verona.transport.send_tcp") as send:
            dialog.refresh()
            try:
                self.wait_for(started.is_set)
                self.assertIsNone(dialog.info)
                self.assertFalse(dialog.refresh_button.isEnabled())
            finally:
                release.set()
            self.wait_for(lambda: dialog.info is not None and not self.window._tasks)
        self.assertIsNot(threads[0], threading.current_thread())
        query_mock.assert_called_once()
        send.assert_not_called()
        self.assertEqual((self.window._snapshot(), self.window._documents, self.window.saved_snapshot), before)

    def test_english_and_theme_inheritance_keep_the_same_fields(self):
        set_language("en")
        self.window._apply_theme("dark")
        dialog = self.dialog()
        dialog._ready(INFO)
        self.assertEqual(dialog.windowTitle(), "System information")
        self.assertEqual(dialog.copy_button.text(), "Copy information")
        self.assertEqual(dialog.palette().color(QPalette.ColorRole.WindowText), QColor(THEMES["dark"]["text"]))
        texts = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertNotIn("Cutter information", texts)
        self.assertIn("Firmware version", texts)
        self.assertTrue(dialog.refresh_button.toolTip())
        with patch("verona.system_info.QApplication.clipboard") as clipboard:
            dialog.copy_info()
        self.assertIn("Firmware version: V22.0613", clipboard.return_value.setText.call_args.args[0])

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QMessageBox, QWidget

from verona import firmware
from verona import firmware_history
from verona.firmware import FirmwareUpdateError
from verona.firmware_dialog import FirmwareUpdateDialog
from verona.i18n import set_language
from verona.editor_widgets import SafeSpinBox
from tests.test_firmware import fixture
from verona.ui import VeronaWindow


class FakeOwner(QWidget):
    def __init__(self):
        super().__init__()
        self.connection_mode = QComboBox()
        self.connection_mode.addItem("Wi-Fi", "wifi")
        self.wifi_host = QLineEdit("192.0.2.1")
        self.wifi_port = SafeSpinBox()
        self.wifi_port.setRange(1, 65535)
        self.wifi_port.setValue(8080)
        self.pending = []
        self.defer = False

    def _dispatch(self, function, succeeded, failed):
        if self.defer:
            self.pending.append((function, succeeded, failed))
            return
        try:
            succeeded(function())
        except Exception as error:
            failed(str(error))

    def run_pending(self):
        function, succeeded, failed = self.pending.pop(0)
        try:
            succeeded(function())
        except Exception as error:
            failed(str(error))


class FirmwareDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        set_language("en")
        self.temp = tempfile.TemporaryDirectory()
        self.enterContext(patch.dict(os.environ, {
            "XDG_STATE_HOME": str(Path(self.temp.name) / "xdg-state")}))
        self.path = Path(self.temp.name) / "synthetic.bin"
        self.path.write_bytes(fixture())
        self.image = firmware.inspect_firmware(fixture())
        self.owner = FakeOwner()
        self.dialog = FirmwareUpdateDialog(self.owner)

    def tearDown(self):
        self.dialog.reject()
        self.dialog.deleteLater()
        self.owner.deleteLater()
        self.temp.cleanup()
        set_language("zh_TW")
        self.app.processEvents()

    def ready_for_update(self):
        self.dialog.load_file(self.path)
        with patch("verona.firmware.query_identity", return_value={
                "MODE": "SS12", "HVER": "V7.1202_4300", "SVER": "V22.0613"}):
            self.dialog.read_identity()
        self.dialog.compatible.setChecked(True)
        self.assertTrue(self.dialog.start_button.isEnabled())

    def test_open_and_file_inspection_do_not_query_or_transfer(self):
        with patch("verona.firmware.load_firmware", wraps=firmware.load_firmware) as loader, \
                patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")), \
                patch("verona.firmware.query_identity") as query, \
                patch("verona.firmware.update_firmware") as update:
            self.assertIsNone(self.dialog.image)
            self.assertTrue(self.dialog.progress_bar.isHidden())
            self.dialog.load_file(self.path)
            self.assertIsNone(self.dialog.identity)
            self.assertFalse(self.dialog.start_button.isEnabled())
            query.assert_not_called()
            update.assert_not_called()
            loader.assert_called_once_with(self.path)
            self.assertIn(f"{self.image.summary()['file_bytes']} bytes", self.dialog.image_details.text())
            for version in self.image.summary()["version_candidates"]:
                self.assertIn(version, self.dialog.image_details.text())
            self.assertIn(self.dialog.compatibility_note.text(), (
                "實驗性傳輸 · 韌體相容性未確認",
                "Experimental transfer · firmware compatibility is unverified."))
            for implementation_term in ("PGSR_SSA", "HDSUM", "PGOK"):
                self.assertNotIn(implementation_term, self.dialog.image_details.text())

    def test_start_requires_file_identity_and_explicit_compatibility_assertion(self):
        with patch("verona.firmware.query_identity", return_value={
                "MODE": "SS12", "HVER": "V7.1202_4300", "SVER": "V22.0613"}) as query:
            self.dialog.load_file(self.path)
            self.assertFalse(self.dialog.start_button.isEnabled())
            self.dialog.read_identity()
            query.assert_called_once_with("192.0.2.1", 8080, usb_node=None)
            self.assertFalse(self.dialog.start_button.isEnabled())
            self.dialog.compatible.setChecked(True)
            self.assertTrue(self.dialog.start_button.isEnabled())
            self.dialog.host.setText("192.0.2.2")
            self.assertIsNone(self.dialog.identity)
            self.assertFalse(self.dialog.compatible.isChecked())
            self.assertFalse(self.dialog.start_button.isEnabled())

    def test_starting_a_new_identity_read_clears_previous_read_and_assertion(self):
        self.ready_for_update()
        self.owner.defer = True
        self.dialog.read_identity()
        self.assertIsNone(self.dialog.identity)
        self.assertFalse(self.dialog.compatible.isChecked())
        self.assertFalse(self.dialog.start_button.isEnabled())
        self.assertEqual(len(self.owner.pending), 1)

    def test_usb_node_refresh_is_explicit_and_queries_use_selected_node(self):
        self.dialog.mode.setCurrentIndex(self.dialog.mode.findData("usb"))
        with patch("verona.firmware.list_usb_nodes",
                   return_value=["/dev/usb/lp0", "/dev/usb/lp1"]) as listing, \
                patch("verona.firmware.query_identity", return_value={
                    "MODE": "SS12", "HVER": "V7.1202_4300", "SVER": "V22.0613"}) as query:
            listing.assert_not_called()
            self.dialog.refresh_usb_nodes()
            listing.assert_called_once_with()
            self.dialog.usb_node.setCurrentIndex(self.dialog.usb_node.findData("/dev/usb/lp0"))
            self.dialog.load_file(self.path)
            self.dialog.read_identity()
            query.assert_called_once_with(None, 8080, usb_node="/dev/usb/lp0")
            self.dialog.compatible.setChecked(True)
            self.dialog.usb_node.setCurrentIndex(self.dialog.usb_node.findData("/dev/usb/lp1"))
            self.assertIsNone(self.dialog.identity)
            self.assertFalse(self.dialog.compatible.isChecked())

    def test_form_hides_entire_wifi_and_usb_rows(self):
        self.assertTrue(self.dialog.form.isRowVisible(self.dialog.wifi_row))
        self.assertFalse(self.dialog.form.isRowVisible(self.dialog.usb_row))
        self.dialog.mode.setCurrentIndex(self.dialog.mode.findData("usb"))
        self.assertFalse(self.dialog.form.isRowVisible(self.dialog.wifi_row))
        self.assertTrue(self.dialog.form.isRowVisible(self.dialog.usb_row))

    def test_cancelled_confirmation_sends_nothing(self):
        self.ready_for_update()
        with patch("verona.firmware_dialog.QMessageBox.warning", return_value=
                   QMessageBox.StandardButton.No) as confirmation, \
                patch("verona.firmware.update_firmware") as update:
            self.dialog.start_update()
        confirmation.assert_called_once()
        update.assert_not_called()
        self.assertIn("Cancelled", self.dialog.status.text())

    def test_busy_update_cannot_be_restarted_or_closed(self):
        self.ready_for_update()
        self.owner.defer = True

        def update(*_args, **_kwargs):
            started = firmware_history.latest_attempt()
            self.assertEqual(started["status"], "started")
            self.assertIsNone(started["bytes_header_sent"])
            self.assertIsNone(started["bytes_body_sent"])
            return {"status": "pgok_received", "bytes_header_sent": 71, "bytes_body_sent": 10}

        with patch("verona.firmware_dialog.QMessageBox.warning", return_value=
                   QMessageBox.StandardButton.Yes), \
                patch("verona.firmware.update_firmware", side_effect=update) as update:
            self.dialog.start_update()
            self.dialog.start_update()
            self.dialog.reject()
            self.dialog.show()
            self.dialog.close()
            self.assertTrue(self.dialog._updating)
            self.assertTrue(self.dialog.isVisible())
            self.assertFalse(self.dialog.progress_bar.isHidden())
            self.assertEqual(len(self.owner.pending), 1)
            self.assertIsNone(firmware_history.latest_attempt())
            self.assertFalse(self.dialog.history_button.isEnabled())
            self.dialog.history_button.click()
            self.owner.run_pending()
        update.assert_called_once()
        self.assertIn("Data was sent", self.dialog.status.text())
        self.assertTrue(self.dialog.progress_bar.isHidden())
        self.assertNotIn("version verified", self.dialog.status.text().lower())
        self.assertIsNone(self.dialog.identity)

    def test_failed_update_retains_stage_and_byte_counts_without_retry(self):
        self.ready_for_update()
        error = FirmwareUpdateError("connection stopped", stage="body",
                                    bytes_header_sent=71, bytes_body_sent=1024)
        with patch("verona.firmware_dialog.QMessageBox.warning", return_value=
                   QMessageBox.StandardButton.Yes), \
                patch("verona.firmware.update_firmware", side_effect=error) as update:
            self.dialog.start_update()
        update.assert_called_once()
        status = self.dialog.status.text()
        self.assertIn("body", status)
        self.assertIn("at least 71 header bytes", status)
        self.assertIn("1024 body bytes", status)
        self.assertIn("no automatic retry", status)
        set_language("zh_TW")
        self.dialog.history_path = None
        self.dialog._update_finished(("firmware_error", FirmwareUpdateError(
            "connection stopped", stage="body", bytes_header_sent=71, bytes_body_sent=1024)))
        self.assertIn("至少", self.dialog.status.text())

    def test_pgok_result_updates_progress_without_claiming_version_verified(self):
        self.ready_for_update()

        def transfer(*_args, on_progress, **_kwargs):
            on_progress(512, 1024)
            return {"status": "pgok_received"}

        with patch("verona.firmware_dialog.QMessageBox.warning", return_value=
                   QMessageBox.StandardButton.Yes), \
                patch("verona.firmware.update_firmware", side_effect=transfer), \
                patch("verona.firmware.verify_firmware_version") as verify:
            self.dialog.start_update()
        self.assertEqual(self.dialog.progress_bar.value(), 500)
        self.assertIn("Data was sent", self.dialog.status.text())
        self.assertNotIn("PGOK", self.dialog.status.text())
        verify.assert_not_called()

    def test_bad_file_clears_previous_image_and_identity(self):
        self.ready_for_update()
        bad_path = Path(self.temp.name) / "bad.bin"
        bad_path.write_bytes(b"not a firmware image")
        with patch("verona.firmware.load_firmware", side_effect=ValueError("invalid header")) as load:
            self.dialog.load_file(bad_path)
        load.assert_called_once_with(bad_path)
        self.assertIsNone(self.dialog.image)
        self.assertIsNone(self.dialog.identity)
        self.assertFalse(self.dialog.compatible.isChecked())
        self.assertFalse(self.dialog.start_button.isEnabled())

    def test_initial_history_failure_prevents_any_firmware_send(self):
        self.ready_for_update()
        with patch("verona.firmware_dialog.QMessageBox.warning", return_value=
                   QMessageBox.StandardButton.Yes), \
                patch("verona.firmware_history.begin_attempt", side_effect=OSError("disk full")), \
                patch("verona.firmware.update_firmware") as update:
            self.dialog.start_update()
        update.assert_not_called()
        self.assertFalse(self.dialog._updating)
        self.assertIn("no firmware data was sent", self.dialog.status.text())

    def test_completion_history_failure_keeps_actual_transfer_result(self):
        self.ready_for_update()
        with patch("verona.firmware_dialog.QMessageBox.warning", return_value=
                   QMessageBox.StandardButton.Yes), \
                patch("verona.firmware_history.finish_attempt", side_effect=OSError("disk full")), \
                patch("verona.firmware.update_firmware", return_value={
                    "status": "pgok_received", "bytes_header_sent": 71,
                    "bytes_body_sent": len(self.image.body)}) as update:
            self.dialog.start_update()
        update.assert_called_once()
        self.assertIn("Data was sent", self.dialog.status.text())
        self.assertIn("saving firmware history failed", self.dialog.status.text().lower())

    def test_error_history_redacts_endpoint_node_and_full_path(self):
        self.ready_for_update()
        self.dialog.mode.setCurrentIndex(self.dialog.mode.findData("usb"))
        self.dialog.usb_node.addItem("/dev/usb/lp0", "/dev/usb/lp0")
        self.dialog.usb_node.setCurrentIndex(self.dialog.usb_node.findData("/dev/usb/lp0"))
        self.dialog.load_file(self.path)
        with patch("verona.firmware.query_identity", return_value={
                "MODE": "SS12", "HVER": "V7.1202_4300", "SVER": "V22.0613"}):
            self.dialog.read_identity()
        self.dialog.compatible.setChecked(True)
        error = FirmwareUpdateError(
            f"PGREADY timeout at 192.0.2.1 /dev/usb/lp0 {self.path}", stage="header")
        error.__cause__ = ConnectionRefusedError(111, "Connection refused")
        with patch("verona.firmware_dialog.QMessageBox.warning", return_value=
                   QMessageBox.StandardButton.Yes), \
                patch("verona.firmware.update_firmware", side_effect=error):
            self.dialog.start_update()
        record = firmware_history.latest_attempt()
        encoded = str(record)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["transport"], "usb")
        self.assertEqual(record["error"]["errno"], 111)
        self.assertIn("PGREADY timeout", encoded)
        self.assertNotIn("192.0.2.1", encoded)
        self.assertNotIn("/dev/usb/lp0", encoded)
        self.assertNotIn(str(self.path), encoded)
        self.assertEqual(record["file_name"], self.path.name)
        self.assertNotIn("serial", encoded.lower())
        reopened = FirmwareUpdateDialog(self.owner)
        with patch("verona.firmware.query_identity") as query, \
                patch("verona.firmware.update_firmware") as update:
            reopened.show_last_history()
        query.assert_not_called()
        update.assert_not_called()
        rendered = reopened.history_view.toPlainText()
        self.assertIn('"status": "failed"', rendered)
        self.assertIn("PGREADY timeout", rendered)
        self.assertNotIn("192.0.2.1", rendered)
        self.assertNotIn("/dev/usb/lp0", rendered)
        self.assertNotIn(str(self.path), rendered)
        reopened.reject()
        reopened.deleteLater()

    def test_saved_started_attempt_is_read_after_reopening_dialog(self):
        record_path = firmware_history.begin_attempt(
            file_name=str(self.path), sha256=self.image.sha256,
            candidate_versions=self.image.summary()["version_candidates"],
            transport="wifi", expected_identity={
                "MODE": "SS12", "HVER": "V7.1202_4300", "SVER": "V22.0613"})
        first = firmware_history.latest_attempt()
        self.assertEqual(first["status"], "started")
        self.assertIsNone(first["bytes_header_sent"])
        self.assertIsNone(first["bytes_body_sent"])
        self.assertFalse(first["version_verified"])
        reopened = FirmwareUpdateDialog(self.owner)
        with patch("verona.firmware.query_identity") as query, \
                patch("verona.firmware.update_firmware") as update:
            reopened.show_last_history()
        query.assert_not_called()
        update.assert_not_called()
        self.assertIn('"status": "started"', reopened.history_view.toPlainText())
        self.assertIn(self.image.sha256, reopened.history_view.toPlainText())
        self.assertNotIn(str(self.path), reopened.history_view.toPlainText())
        reopened.reject()
        reopened.deleteLater()

    def test_worker_uses_pre_dispatch_snapshot_not_live_widgets(self):
        self.ready_for_update()
        self.owner.defer = True
        with patch("verona.firmware_dialog.QMessageBox.warning", return_value=
                   QMessageBox.StandardButton.Yes), \
                patch("verona.firmware.update_firmware", return_value={
                    "status": "pgok_received", "bytes_header_sent": 71,
                    "bytes_body_sent": len(self.image.body)}) as update:
            self.dialog.start_update()
            self.dialog.host.setText("192.0.2.99")
            self.owner.run_pending()
        self.assertEqual(update.call_args.args[1], "192.0.2.1")
        self.assertEqual(update.call_args.kwargs["expected_hver"], "V7.1202_4300")
        record = firmware_history.latest_attempt()
        self.assertEqual(record["status"], "pgok_received")
        self.assertNotIn("192.0.2.1", str(record))


class FirmwareWindowIntegrationTests(unittest.TestCase):
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
        set_language("en")
        self.window = VeronaWindow(host="192.0.2.10", port=8081)

    def tearDown(self):
        self.window._confirm_unsaved_changes = lambda *_args: "discard"
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        set_language("zh_TW")

    def test_both_entries_open_same_dialog_without_device_activity(self):
        self.window.connection_section.setExpanded(True)
        with patch("verona.firmware_dialog.FirmwareUpdateDialog") as factory, \
                patch("verona.firmware.query_identity") as query, \
                patch("verona.firmware.update_firmware") as update, \
                patch("verona.firmware.list_usb_nodes") as enumerate_usb, \
                patch("verona.firmware.load_firmware") as load, \
                patch("verona.firmware_history.latest_attempt") as latest:
            self.assertEqual(self.window.firmware_action.text(), "Firmware update (experimental)…")
            self.assertEqual(self.window.firmware_button.text(), "Firmware update (experimental)…")
            for entry in (self.window.firmware_action, self.window.firmware_button):
                factory.reset_mock()
                entry.trigger() if hasattr(entry, "trigger") else entry.click()
                factory.assert_called_once_with(self.window)
                self.assertEqual([call[0] for call in factory.return_value.method_calls],
                                 ["exec", "deleteLater"])
            query.assert_not_called()
            update.assert_not_called()
            enumerate_usb.assert_not_called()
            load.assert_not_called()
            latest.assert_not_called()

    def test_dialog_combo_and_safe_spinbox_ignore_wheel_with_real_owner_guard(self):
        dialog = FirmwareUpdateDialog(self.window)
        dialog.usb_node.addItem("/dev/usb/lp0", "/dev/usb/lp0")
        dialog.show()
        self.app.processEvents()

        def wheel(widget):
            event = QWheelEvent(
                QPointF(2, 2), QPointF(widget.mapToGlobal(QPoint(2, 2))),
                QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False,
            )
            QApplication.sendEvent(widget, event)
            return event

        dialog.mode.setFocus()
        before_mode = dialog.mode.currentIndex()
        self.assertFalse(wheel(dialog.mode).isAccepted())
        self.assertEqual(dialog.mode.currentIndex(), before_mode)
        dialog.mode.setCurrentIndex(dialog.mode.findData("usb"))
        dialog.usb_node.setFocus()
        before_node = dialog.usb_node.currentIndex()
        self.assertFalse(wheel(dialog.usb_node).isAccepted())
        self.assertEqual(dialog.usb_node.currentIndex(), before_node)
        dialog.port.setFocus()
        before_port = dialog.port.value()
        self.assertFalse(wheel(dialog.port).isAccepted())
        self.assertEqual(dialog.port.value(), before_port)
        dialog.close()


if __name__ == "__main__":
    unittest.main()

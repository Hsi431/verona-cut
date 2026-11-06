"""Background packing preview behavior through the real editor dialog."""
import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTemporaryDir, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from tests.fixtures.nesting_cases import scenarios
from verona.i18n import tr
from verona.packing import pack_items as real_pack_items
from verona.ui import PackingDialog, VeronaWindow


class PackingPreviewAsyncTests(unittest.TestCase):
    def setUp(self):
        engine = patch("verona.nesting_engine.nesting_available", return_value=False)
        engine.start()
        self.addCleanup(engine.stop)

    @classmethod
    def setUpClass(cls):
        cls.settings = QTemporaryDir()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          cls.settings.path())
        cls.app = QApplication.instance() or QApplication([])

    def _window_and_dialog(self, items=None, metadata=None):
        window = VeronaWindow()
        window.items = items or [{"name": "sticker", "cut": True, "contours": [[
            (20, 20), (30, 20), (30, 30), (20, 30), (20, 20)]]}]
        window.metadata = metadata or {}
        dialog = PackingDialog(window, list(range(len(window.items))))
        return window, dialog

    def _preview_button(self, dialog):
        return dialog.findChildren(QPushButton)[0]

    def _pump_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()
        self.assertTrue(predicate(), "timed out waiting for packing preview")

    def _pump_for(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()

    def _close(self, window, dialog):
        dialog.close()
        with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
            window.close()

    def test_preview_keeps_ui_responsive_until_background_result(self):
        window, dialog = self._window_and_dialog()
        started, release = threading.Event(), threading.Event()
        timer = QTimer()
        ticks = []
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start(50)

        def blocked_pack(items, metadata, indices, parameters):
            started.set()
            release.wait(1)
            return real_pack_items(items, metadata, indices, parameters)

        try:
            dialog.time_limit_spin.setValue(.1)
            with patch("verona.packing.pack_items", side_effect=blocked_pack):
                began = time.monotonic()
                dialog.preview()
                elapsed = time.monotonic() - began
                self.assertLess(elapsed, .4, "preview blocked the UI thread")
                self._pump_until(lambda: started.is_set())
                self.assertFalse(self._preview_button(dialog).isEnabled())
                self.assertFalse(dialog.apply_button.isEnabled())
                self.assertEqual(dialog.summary.text(), tr("排版中…"))
                self._pump_until(lambda: len(ticks) >= 2, timeout=.5)
                release.set()
                self._pump_until(lambda: dialog.apply_button.isEnabled())
            self.assertIsNotNone(dialog.plan)
            self.assertFalse(dialog.preview_label.pixmap().isNull())
        finally:
            release.set()
            timer.stop()
            self._close(window, dialog)

    def test_invalidation_discards_late_result_and_restores_preview_button(self):
        window, dialog = self._window_and_dialog()
        started, release, returned = threading.Event(), threading.Event(), threading.Event()

        def blocked_pack(items, metadata, indices, parameters):
            started.set()
            release.wait(1)
            try:
                return real_pack_items(items, metadata, indices, parameters)
            finally:
                returned.set()

        try:
            dialog.time_limit_spin.setValue(.1)
            with patch("verona.packing.pack_items", side_effect=blocked_pack):
                began = time.monotonic()
                dialog.preview()
                self.assertLess(time.monotonic() - began, .4,
                                "preview blocked the UI thread")
                self._pump_until(lambda: started.is_set())
                dialog.gap_spin.setValue(3)
                self.assertIsNone(dialog.plan)
                self.assertFalse(dialog.apply_button.isEnabled())
                self.assertTrue(self._preview_button(dialog).isEnabled())
                release.set()
                self._pump_until(lambda: returned.is_set())
                self._pump_for(.1)
            self.assertIsNone(dialog.plan)
            self.assertFalse(dialog.apply_button.isEnabled())
        finally:
            release.set()
            self._close(window, dialog)

    def test_closed_dialog_ignores_late_result(self):
        window, dialog = self._window_and_dialog()
        started, release = threading.Event(), threading.Event()

        def blocked_pack(items, metadata, indices, parameters):
            started.set()
            release.wait(1)
            return real_pack_items(items, metadata, indices, parameters)

        try:
            dialog.time_limit_spin.setValue(.1)
            with patch("verona.packing.pack_items", side_effect=blocked_pack), \
                    patch("verona.editor_actions.render_preview") as render, \
                    patch.object(QMessageBox, "warning") as warning:
                began = time.monotonic()
                dialog.preview()
                self.assertLess(time.monotonic() - began, .4,
                                "preview blocked the UI thread")
                self._pump_until(lambda: started.is_set())
                dialog.close()
                release.set()
                self._pump_for(.2)
                self.assertIsNone(dialog.plan)
                render.assert_not_called()
                warning.assert_not_called()
        finally:
            release.set()
            self._close(window, dialog)

    def test_pack_failure_warns_and_reenables_preview(self):
        window, dialog = self._window_and_dialog()
        started, release = threading.Event(), threading.Event()

        def fail_pack(*_args):
            started.set()
            release.wait(1)
            raise ValueError("bad layout")

        try:
            with patch("verona.packing.pack_items", side_effect=fail_pack), \
                    patch.object(QMessageBox, "warning") as warning:
                began = time.monotonic()
                dialog.preview()
                self.assertLess(time.monotonic() - began, .4,
                                "preview blocked the UI thread")
                self._pump_until(lambda: started.is_set())
                self.assertFalse(self._preview_button(dialog).isEnabled())
                self.assertFalse(dialog.apply_button.isEnabled())
                release.set()
                self._pump_until(lambda: warning.called)
                warning.assert_called_once()
                self.assertIn("bad layout", warning.call_args.args[2])
                self.assertFalse(dialog.apply_button.isEnabled())
                self.assertTrue(self._preview_button(dialog).isEnabled())
        finally:
            release.set()
            self._close(window, dialog)

    def test_real_outline_sticker_preview_keeps_timer_responsive(self):
        _, items, metadata, parameters = scenarios()[5]
        window, dialog = self._window_and_dialog(items, metadata)
        dialog.fill_check.setChecked(parameters["fill"])
        dialog.shape_check.setChecked(parameters["shape_aware"])
        dialog.rotate_check.setChecked(parameters["allow_rotate"])
        dialog.gap_spin.setValue(parameters["gap_mm"])
        dialog.time_limit_spin.setValue(7.4)
        timer = QTimer()
        gaps, previous = [], [time.monotonic()]

        def tick():
            now = time.monotonic()
            gaps.append(now - previous[0])
            previous[0] = now

        timer.timeout.connect(tick)
        timer.start(50)
        try:
            dialog.preview()
            self._pump_until(lambda: dialog.apply_button.isEnabled(), timeout=10)
            self._pump_until(lambda: len(gaps) >= 4, timeout=2)
            maximum_ms = max(gaps) * 1000
            print(f"PACKING_PREVIEW_MAX_UI_GAP_MS={maximum_ms:.1f}")
            self.assertLess(maximum_ms, 200)
        finally:
            timer.stop()
            self._close(window, dialog)


if __name__ == "__main__":
    unittest.main()

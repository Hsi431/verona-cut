"""Named settings stay local, apply explicitly and follow document undo."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from verona.document_state import default_cut_settings
from verona.i18n import set_language
from verona.material_presets import MaterialPresetStore, _KEY
from verona.recovery import RecoveryStore
from verona.ui import VeronaWindow


VALUES = {"custom": True, "speed": 11, "force": 85, "passes": 2, "tool": 0}


class MaterialPresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "workspace.ini")
        self.settings = QSettings(self.path, QSettings.Format.IniFormat)
        self.store = MaterialPresetStore(self.settings)

    def test_round_trip_rename_case_overwrite_and_delete(self):
        self.assertEqual(self.store.load(), {})
        self.assertEqual(self.store.save("  Sticker paper  ", VALUES), "Sticker paper")
        reopened = MaterialPresetStore(QSettings(self.path, QSettings.Format.IniFormat))
        self.assertEqual(reopened.load(), {"Sticker paper": VALUES})
        with self.assertRaises(FileExistsError):
            self.store.save("sticker paper", {**VALUES, "force": 40})
        self.store.save("STICKER PAPER", {**VALUES, "force": 40}, overwrite=True)
        self.assertEqual(list(self.store.load()), ["Sticker paper"])
        self.assertEqual(self.store.load()["Sticker paper"]["force"], 40)
        self.store.delete("Sticker paper")
        self.assertEqual(reopened.load(), {})

    def test_invalid_parameters_and_names_leave_saved_presets_unchanged(self):
        self.store.save("Paper", VALUES)
        for key, value in (("custom", 1), ("speed", 0), ("speed", True), ("force", 161),
                           ("passes", 11), ("tool", 2), ("host", "192.0.2.1")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.store.save("Invalid", {**VALUES, key: value})
        for name in ("", " ", "a" * 81, "bad\nname", 42):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.save(name, VALUES)
        self.assertEqual(self.store.load(), {"Paper": VALUES})

    def test_corrupt_and_unknown_versions_are_preserved(self):
        for raw in ("not JSON", "[]", '{"version":true,"presets":{}}',
                    '{"version":2,"presets":{}}',
                    json.dumps({"version": 1, "presets": {"Paper": {**VALUES, "port": 8080}}})):
            self.settings.setValue(_KEY, raw)
            with self.assertRaises(ValueError):
                self.store.load()
            with self.assertRaises(ValueError):
                self.store.save("New", VALUES)
            self.assertEqual(self.settings.value(_KEY), raw)

    def test_settings_write_error_restores_previous_in_memory_value(self):
        self.store.save("Paper", VALUES)
        previous = self.settings.value(_KEY)
        with patch.object(self.settings, "status", return_value=QSettings.Status.AccessError):
            with self.assertRaises(OSError):
                self.store.save("Other", VALUES)
        self.assertEqual(self.settings.value(_KEY), previous)


class MaterialPresetUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, self.temp.name)
        QSettings("Verona", "Workspace").clear()
        set_language("zh_TW")
        self.window = VeronaWindow()
        self.widget = self.window.material_presets
        self.addCleanup(self.close_window)

    def close_window(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()

    def select_preset(self, name="Paper", values=None):
        self.widget.store.save(name, values or VALUES)
        self.widget.reload(name)

    def test_save_keeps_document_unchanged_and_excludes_connection_parameters(self):
        self.window._set_cut_settings({**default_cut_settings(), **VALUES, "host": "192.0.2.11", "port": 9090})
        before = self.window._snapshot()
        with patch.object(QInputDialog, "getText", return_value=("Paper", True)):
            self.widget.save_current()
        self.assertEqual(self.window._snapshot(), before)
        self.assertEqual(self.widget.store.load(), {"Paper": VALUES})
        self.assertNotIn("host", self.window.settings.value(_KEY))
        self.assertNotIn("port", self.window.settings.value(_KEY))

    def test_selection_does_not_apply_then_apply_is_undoable_and_keeps_connection(self):
        self.window.add_rectangle()
        self.window.wifi_host.setText("192.0.2.22")
        self.window.wifi_port.setValue(9090)
        original = self.window._snapshot()
        self.select_preset()
        self.assertEqual(self.window._snapshot(), original)
        self.widget.apply_selected()
        expected = {**original["settings"], **VALUES}
        self.assertEqual(self.window._cut_settings(), expected)
        self.assertEqual(self.window.items, original["items"])
        self.assertEqual(len(self.window.undo_stack), 2)
        payload = self.window._encoded_plt()
        self.assertIn(b"VS11", payload)
        self.assertIn(b"!FS85", payload)
        self.widget.apply_selected()
        self.assertEqual(len(self.window.undo_stack), 2)
        self.window.undo()
        self.assertEqual(self.window._snapshot(), original)
        self.window.redo()
        self.assertEqual(self.window._cut_settings(), expected)

    def test_apply_only_changes_active_tab_and_survives_language_save_and_recovery(self):
        self.window.add_rectangle()
        first = copy.deepcopy(self.window._snapshot())
        self.window.new_design()
        self.window.add_ellipse()
        self.select_preset()
        self.widget.apply_selected()
        self.window._change_language("en")
        self.widget = self.window.material_presets
        self.assertEqual(self.widget.combo.currentData(), "Paper")
        self.assertEqual({key: self.window._cut_settings()[key] for key in VALUES}, VALUES)
        output = Path(self.temp.name) / "applied.jdesign"
        self.window._write_document(str(output))
        self.assertEqual({key: json.loads(output.read_text())["settings"][key] for key in VALUES}, VALUES)
        self.window.add_rectangle()
        directory = Path(self.temp.name) / "recovery"
        source = RecoveryStore(directory)
        source.checkpoint(self.window._documents)
        source.close(discard=False)
        reader = RecoveryStore(directory)
        try:
            drafts, errors = reader.candidates()
            self.assertEqual(errors, [])
            recovered = next(document for _, document in drafts if document.settings["speed"] == 11)
            self.assertEqual({key: recovered.settings[key] for key in VALUES}, VALUES)
        finally:
            reader.close()
        self.window.tab_bar.setCurrentIndex(0)
        self.assertEqual(self.window._snapshot(), first)

    def test_overwrite_and_delete_require_confirmation_and_cancel_keeps_values(self):
        self.select_preset()
        with patch.object(QInputDialog, "getText", return_value=("paper", True)), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.widget.save_current()
            self.widget.delete_selected()
        self.assertEqual(self.widget.store.load(), {"Paper": VALUES})
        with patch.object(QInputDialog, "getText", return_value=("paper", True)), \
                patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.widget.save_current()
        self.assertEqual(self.widget.store.load()["Paper"]["speed"], 5)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.widget.delete_selected()
        self.assertEqual(self.widget.store.load(), {})
        self.assertFalse(self.widget.apply_button.isEnabled())

    def test_sending_blocks_preset_application_and_corruption_disables_controls(self):
        self.select_preset()
        before = self.window._cut_settings()
        self.window.sending = True
        self.window._update_send_controls()
        self.widget.apply_selected()
        self.assertFalse(self.widget.apply_button.isEnabled())
        self.assertEqual(self.window._cut_settings(), before)
        self.window.sending = False
        self.window._update_send_controls()
        self.assertTrue(self.widget.apply_button.isEnabled())
        self.window.settings.setValue(_KEY, "broken")
        self.widget.reload()
        self.assertFalse(self.widget.save_button.isEnabled())
        with patch.object(QMessageBox, "warning") as warning:
            self.widget.save_current()
        warning.assert_called_once()
        self.assertEqual(self.window.settings.value(_KEY), "broken")

    def test_long_names_fit_inspector_in_both_languages(self):
        self.select_preset("材料" * 40)
        for language in ("en", "zh_TW"):
            self.window._change_language(language)
            self.widget = self.window.material_presets
            self.window.cut_section.setExpanded(True)
            self.window.resize(1000, 700)
            self.window.show()
            QTest.qWait(30)
            self.window.inspector_scroll.ensureWidgetVisible(self.widget)
            self.app.processEvents()
            self.assertLessEqual(self.widget.sizeHint().width(), self.window.inspector_scroll.width())
            for button in (self.widget.apply_button, self.widget.save_button, self.widget.delete_button):
                self.assertTrue(button.isVisible())
                self.assertGreaterEqual(button.width(), button.sizeHint().width())
                self.assertGreaterEqual(button.height(), button.sizeHint().height())
                self.assertTrue(self.widget.rect().contains(button.geometry()))


if __name__ == "__main__":
    unittest.main()

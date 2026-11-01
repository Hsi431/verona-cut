"""Recovery survives crashes without replacing original designs."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from verona.document_state import DocumentState
from verona.i18n import get_language, set_language, tr
from verona.recovery import AUTOSAVE_INTERVAL_MS, RecoveryController, RecoveryStore
from verona.ui import VeronaWindow


def changed_document(name="unsaved"):
    document = DocumentState()
    document.items = [{"name": name, "contours": [[(1., 2.), (3., 4.)]]}]
    return document


class RecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "recovery"
        self.store = RecoveryStore(self.directory)
        self.addCleanup(self.store.close)

    def test_real_process_exit_leaves_recoverable_draft_and_original_untouched(self):
        original = Path(self.temp.name) / "original.jdesign"
        original.write_text("original bytes")
        script = '''import os,sys
from pathlib import Path
from verona.document_state import DocumentState
from verona.recovery import RecoveryStore
store=RecoveryStore(sys.argv[1])
document=DocumentState()
document.items=[{"name":"after crash","contours":[[(1.,2.),(3.,4.)]]}]
document.current_file=sys.argv[2]
store.checkpoint([document])
os._exit(0)
'''
        subprocess.run([sys.executable, "-c", script, str(self.directory), str(original)], check=True)
        drafts, errors = self.store.candidates()
        self.assertEqual(errors, [])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0][1].items[0]["name"], "after crash")
        self.assertTrue(drafts[0][1].is_dirty())
        self.assertEqual(drafts[0][1].current_file, str(original))
        self.assertEqual(original.read_text(), "original bytes")

    def test_live_workspace_is_skipped_and_closed_workspace_can_be_recovered(self):
        other = RecoveryStore(self.directory)
        other.checkpoint([changed_document()])
        self.assertEqual(self.store.candidates(), ([], []))
        other.close(discard=False)
        self.assertEqual(len(self.store.candidates()[0]), 1)

    def test_private_permissions_no_rewrite_when_unchanged_and_clean_drafts_removed(self):
        document = changed_document()
        self.store.checkpoint([document])
        path = next(self.store.directory.glob("*.json"))
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with patch("verona.recovery.write_private_json", side_effect=AssertionError("Repeated write")):
            self.store.checkpoint([document])
        document.mark_saved("saved.jdesign")
        self.store.checkpoint([document])
        self.assertFalse(path.exists())

    def test_failed_atomic_write_keeps_previous_draft_and_cleans_temporary_file(self):
        document = changed_document()
        self.store.checkpoint([document])
        path = next(self.store.directory.glob("*.json"))
        previous = path.read_bytes()
        document.items[0]["name"] = "changed again"
        with patch("verona.workspace_storage.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.checkpoint([document])
        self.assertEqual(path.read_bytes(), previous)
        self.assertFalse(list(self.store.directory.glob(".write-*")))

    def test_close_is_idempotent(self):
        other = RecoveryStore(self.directory)
        other.checkpoint([changed_document()])
        other.close()
        other.close()
        self.assertFalse(other.directory.exists())

    def test_corrupt_draft_is_reported_and_preserved(self):
        other = RecoveryStore(self.directory)
        other.checkpoint([changed_document()])
        path = next(other.directory.glob("*.json"))
        path.write_text("{broken")
        other.close(discard=False)
        drafts, errors = self.store.candidates()
        self.assertEqual(drafts, [])
        self.assertEqual(errors, [path])
        self.store.release_pending()
        self.assertTrue(path.exists())


class RecoveryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "recovery"
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, self.temp.name)
        self.window = VeronaWindow()
        self.window.recovery = RecoveryController(self.window, directory=self.directory)
        self.controller = self.window.recovery
        self.addCleanup(self.close_window)

    def close_window(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()

    def stale_drafts(self):
        source = RecoveryStore(self.directory)
        first, second = changed_document("first"), changed_document("second")
        first.settings["speed"] = 11
        second.settings["force"] = 85
        source.checkpoint([first, second])
        paths = list(source.directory.glob("*.json"))
        source.close(discard=False)
        return paths

    def test_restore_all_tabs_in_order_and_recheckpoint_before_removing_old_files(self):
        paths = self.stale_drafts()
        with patch.object(self.controller, "_choice", return_value="restore"):
            self.controller.restore_available()
        self.assertEqual([d.items[0]["name"] for d in self.window._documents], ["first", "second"])
        self.assertEqual(self.window._documents[0].settings["speed"], 11)
        self.assertEqual(self.window._documents[1].settings["force"], 85)
        self.assertTrue(all(d.is_dirty() for d in self.window._documents))
        self.assertFalse(any(p.exists() for p in paths))
        self.assertEqual(len(list(self.controller.store.directory.glob("*.json"))), 2)
        self.assertEqual(self.controller.timer.interval(), AUTOSAVE_INTERVAL_MS)

    def test_failed_transfer_keeps_original_drafts(self):
        paths = self.stale_drafts()
        with patch.object(self.controller, "_choice", return_value="restore"), \
                patch.object(self.controller.store, "checkpoint", side_effect=OSError("disk full")):
            self.controller.restore_available()
        self.assertTrue(all(p.exists() for p in paths))
        self.assertIn("disk full", self.window.status_label.text())

    def test_later_keeps_drafts_and_discard_removes_them(self):
        paths = self.stale_drafts()
        with patch.object(self.controller, "_choice", return_value="later"):
            self.controller.restore_available()
        self.assertTrue(all(p.exists() for p in paths))
        with patch.object(self.controller, "_choice", return_value="discard"):
            self.controller.restore_available()
        self.assertFalse(any(p.exists() for p in paths))
        self.assertEqual(len(self.window._documents), 1)

    def test_save_close_tab_and_cancel_window_close_update_recovery(self):
        self.window.add_rectangle()
        self.assertTrue(self.controller.checkpoint())
        self.assertEqual(len(list(self.controller.store.directory.glob("*.json"))), 1)
        event = QCloseEvent()
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="cancel"):
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertTrue(self.controller.timer.isActive())
        self.window._write_document(str(Path(self.temp.name) / "saved.jdesign"))
        self.assertFalse(list(self.controller.store.directory.glob("*.json")))
        self.window.add_ellipse()
        self.controller.checkpoint()
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.assertTrue(self.window.close_document(0))
        self.assertFalse(list(self.controller.store.directory.glob("*.json")))

    def test_recovery_and_undo_have_distinct_english_labels(self):
        previous = get_language()
        try:
            set_language("en")
            self.assertEqual(tr("復原設計"), "Restore")
            self.assertEqual(tr("復原"), "Undo")
        finally:
            set_language(previous)

    def test_corrupt_drafts_warn_without_removing_files(self):
        paths = self.stale_drafts()
        paths[0].write_text("invalid")
        with patch.object(self.controller, "_choice", return_value="later"), \
                patch.object(QMessageBox, "warning") as warning:
            self.controller.restore_available()
        warning.assert_called_once()
        self.assertTrue(paths[0].exists())


if __name__ == "__main__":
    unittest.main()

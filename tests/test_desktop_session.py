"""Real local IPC between desktop launches, without opening extra windows."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
import zipfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QElapsedTimer, QProcess, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from verona.desktop_session import DesktopSession
from verona.ui import VeronaWindow
from tests.test_lds import document as native_document, vector


class DesktopSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, self.folder.name)
        self.name = "verona-test-" + uuid.uuid4().hex
        self.session = DesktopSession(name=self.name)
        self.assertTrue(self.session.start([]))
        self.addCleanup(self.session.close)

    def forward(self, paths):
        code = ("import json,sys; from PySide6.QtCore import QCoreApplication; "
                "from verona.desktop_session import DesktopSession; app=QCoreApplication([]); "
                "session=DesktopSession(name=sys.argv[1]); print(session.start(json.loads(sys.argv[2])))")
        process = QProcess()
        process.start(sys.executable, ["-c", code, self.name, json.dumps(paths)])
        self.assertTrue(process.waitForStarted())
        timer = QElapsedTimer()
        timer.start()
        while process.state() != QProcess.ProcessState.NotRunning and timer.elapsed() < 8000:
            QTest.qWait(10)
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
            process.waitForFinished()
            self.fail("Desktop request timed out")
        self.assertEqual(process.exitCode(), 0, bytes(process.readAllStandardError()).decode())
        self.assertEqual(bytes(process.readAllStandardOutput()).strip(), b"False")
        self.app.processEvents()

    def test_successive_desktop_opens_use_tabs_and_keep_dirty_document_settings(self):
        window = VeronaWindow()
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.hide)
        self.session.openRequested.connect(window.open_paths)
        document = {"format": "verona", "version": 1, "units": "mm", "items": [], "settings": {}}
        first = Path(self.folder.name) / "舊的設計 空格.lds"
        second = Path(self.folder.name) / "another.jdesign"
        with zipfile.ZipFile(first, "w") as archive:
            archive.writestr("CDOC", native_document(vector([(5., 5., 0x10), (15., 5., 0x21)])))
        second.write_text(json.dumps(document))
        window.custom_settings.setChecked(True)
        window.passes_spin.setValue(5)
        self.forward([str(first)])
        self.assertFalse(window.custom_settings.isChecked())
        self.assertEqual((window.passes_spin.value(), window.speed_spin.value(), window.force_spin.value()), (1, 5, 10))
        window.add_rectangle()
        window.passes_spin.setValue(3)
        window.custom_settings.setChecked(True)
        window.speed_spin.setValue(7)
        window.force_spin.setValue(42)
        original = copy.deepcopy(window._document_state())
        self.forward([str(second)])
        self.assertEqual(window.tab_bar.count(), 3)
        self.assertEqual(window.current_file, str(second))
        self.forward([str(first)])
        self.assertEqual(window.tab_bar.count(), 3)
        self.assertEqual(window._document_state(), original)
        self.assertTrue(window.is_dirty())
        self.assertTrue(window.tab_bar.tabText(1).endswith(" *"))

    def test_launch_without_file_activates_existing_workspace(self):
        requests = []
        self.session.openRequested.connect(requests.append)
        self.forward([])
        self.assertEqual(requests, [[]])

    def test_bad_desktop_path_leaves_current_tab_and_settings_untouched(self):
        window = VeronaWindow()
        self.addCleanup(window.deleteLater)
        window.add_rectangle()
        before = copy.deepcopy(window._document_state())
        with patch.object(QMessageBox, "critical") as error:
            self.assertFalse(window.open_path(str(Path(self.folder.name) / "missing.lds")))
        error.assert_called_once()
        self.assertEqual(window._document_state(), before)
        self.assertEqual(window.tab_bar.count(), 1)

    def test_closed_owner_can_be_replaced(self):
        self.session.close()
        replacement = DesktopSession(name=self.name)
        self.addCleanup(replacement.close)
        self.assertTrue(replacement.start([]))

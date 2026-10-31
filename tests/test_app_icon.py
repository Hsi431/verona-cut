"""The packaged app icon renders and matches both desktop launchers."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtGui import QIcon
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from verona.i18n import get_language, set_language
from verona.ui import VeronaWindow, main


ICON = Path(__file__).resolve().parents[1] / "src/verona/icons/verona.svg"


class AppIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.settings_dir = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          cls.settings_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def test_svg_renders_with_transparent_corners_at_desktop_sizes(self):
        self.assertTrue(QSvgRenderer(str(ICON)).isValid())
        icon = QIcon(str(ICON))
        for size in (16, 24, 32, 48, 64, 256, 512):
            with self.subTest(size=size):
                pixmap = icon.pixmap(size, size)
                self.assertFalse(pixmap.isNull())
                self.assertEqual((pixmap.width(), pixmap.height()), (size, size))
                self.assertEqual(pixmap.toImage().pixelColor(0, 0).alpha(), 0)

    def test_editor_window_uses_the_app_icon(self):
        window = VeronaWindow(host="")
        try:
            self.assertFalse(window.windowIcon().isNull())
            self.assertEqual(window.windowIcon().pixmap(48, 48).toImage(),
                             QIcon(str(ICON)).pixmap(48, 48).toImage())
        finally:
            window.close()
            window.deleteLater()

    def test_launch_identifies_the_matching_desktop_entry_in_each_language(self):
        name = self.app.applicationName()
        desktop = self.app.desktopFileName()
        icon = self.app.windowIcon()
        language = get_language()
        try:
            with patch.dict(os.environ, {"VERONA_HOST": ""}), \
                    patch("verona.desktop_session.DesktopSession") as session:
                session.return_value.start.return_value = False
                for selected, entry in (("en", "verona-en"), ("zh_TW", "verona-zh-TW")):
                    self.assertEqual(main(["--lang", selected]), 0)
                    self.assertEqual(self.app.applicationName(), "Verona")
                    self.assertEqual(self.app.desktopFileName(), entry)
                    self.assertFalse(self.app.windowIcon().isNull())
        finally:
            self.app.setApplicationName(name)
            self.app.setDesktopFileName(desktop)
            self.app.setWindowIcon(icon)
            set_language(language)

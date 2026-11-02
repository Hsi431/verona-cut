"""Regression checks for small editor UI polish fixes."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from verona.i18n import set_language, tr
from verona.ui import VeronaWindow


class UiPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_dir = tempfile.TemporaryDirectory(prefix="T-polish-settings-")
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          cls.settings_dir.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def setUp(self):
        self.settings = QSettings("Verona", "Workspace")
        self.settings.clear()
        set_language("zh_TW")
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window._confirm_unsaved_changes = lambda *_args: "discard"
            window.close()
            window.deleteLater()
        self.app.processEvents()
        self.settings.clear()

    def window(self):
        window = VeronaWindow()
        self.windows.append(window)
        return window

    def test_section_expansion_is_restored_after_reopen_and_language_rebuild(self):
        window = self.window()
        window.path_section.setExpanded(True)
        window.layer_section.setExpanded(False)
        window._change_language("en")
        self.assertTrue(window.path_section.isExpanded())
        self.assertFalse(window.layer_section.isExpanded())
        reopened = self.window()
        self.assertTrue(reopened.path_section.isExpanded())
        self.assertFalse(reopened.layer_section.isExpanded())

    def test_toolbar_spacing_survives_theme_menu_switch(self):
        window = self.window()
        window.resize(1280, 800)
        window.show()
        self.app.processEvents()
        self.assertIsNot(window.focusWidget(), window.import_button)
        self.assertTrue(window.import_button.focusPolicy() & Qt.FocusPolicy.TabFocus)

        def button_positions():
            widgets = [window.design_toolbar.widgetForAction(action)
                       for action in window.design_toolbar.actions()]
            widgets.append(window.import_button)
            return [(widget.mapTo(window, widget.rect().topLeft()).x(), widget.width())
                    for widget in sorted(set(widgets), key=lambda item: item.mapTo(window, item.rect().topLeft()).x())]

        before = button_positions()
        window.theme_actions["dark"].trigger()
        self.app.processEvents()
        self.assertEqual(button_positions(), before)

    def test_empty_canvas_and_layer_hints_are_view_only_and_status_is_singular(self):
        window = self.window()
        window.show()
        self.app.processEvents()
        window.refresh_all()
        self.assertEqual(window.layer_panel.stack_layout.currentWidget(), window.layer_panel.empty_hint)
        self.assertEqual(window.layer_panel.empty_hint.text(), tr("尚無物件"))
        image = window.canvas.grab().toImage()
        origin = window.canvas.canvas_origin()
        center_x = round(origin.x() + 150 * window.canvas.zoom)
        center_y = round(origin.y() + 148.5 * window.canvas.zoom)
        muted = window.theme_colors["muted"]
        ink = sum(image.pixelColor(x, y).name() == muted
                  for y in range(center_y - 10, center_y + 11)
                  for x in range(center_x - 200, center_x + 201))
        self.assertGreater(ink, 0)
        self.assertEqual(window.items, [])
        with patch("verona.ui.QMessageBox.information") as information:
            window.export_plt()
        information.assert_called_once()
        with self.assertRaisesRegex(ValueError, "沒有可切割"):
            window._encoded_plt()
        with patch("verona.ui.QFileDialog.getSaveFileName", return_value=("/tmp/T-polish-pytest/empty.pdf", "PDF")), \
             patch("verona.printing.write_print_pdf") as write_pdf:
            window.export_print_pdf()
        self.assertEqual(write_pdf.call_args.args[1], [])

        window._change_language("en")
        window.items = [{"name": "one", "contours": []}]
        window.refresh_all()
        self.assertEqual(window.status_label.text(), "1 path · Workspace 300 × 300 mm")
        self.assertEqual(window.path_section._title, "Object")
        self.assertEqual(tr("物件"), "Object")


if __name__ == "__main__":
    unittest.main()

"""Appearance changes preserve documents and physical output."""
import copy
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QApplication, QDialog, QDoubleSpinBox, QLineEdit, QStyle, QStyleOptionSpinBox, QVBoxLayout
from PySide6.QtTest import QTest

from verona.i18n import set_language
from verona.printing import write_print_pdf
from verona.themes import THEMES
from verona.ui import VeronaWindow, PAPER
from tests.test_editor_actions import image_item, rectangle


class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.directory.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        set_language("zh_TW")
        self.settings = QSettings("Verona", "Workspace")
        self.settings.clear()
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window._apply_theme("light")
            window.deleteLater()
        self.settings.clear()

    def window(self):
        window = VeronaWindow()
        self.windows.append(window)
        return window

    def test_theme_menu_persists_choice_and_reopens_checked(self):
        window = self.window()
        self.assertEqual(window.theme_name, "light")
        window.theme_actions["dark"].trigger()
        self.assertEqual(window.theme_name, "dark")
        self.assertEqual(self.settings.value("theme"), "dark")
        self.assertTrue(window.theme_actions["dark"].isChecked())
        self.assertFalse(window.theme_actions["light"].isChecked())
        reopened = self.window()
        self.assertEqual(reopened.theme_name, "dark")
        self.assertTrue(reopened.theme_actions["dark"].isChecked())
        reopened.theme_actions["light"].trigger()
        self.assertEqual(window.theme_name, "light")
        self.assertEqual(reopened.theme_name, "light")

    def test_switch_preserves_dirty_tabs_history_selection_zoom_and_widgets(self):
        window = self.window()
        window.add_rectangle()
        window.add_ellipse()
        window.undo()
        window.new_design()
        window.add_rectangle()
        window.select(0)
        window.canvas.set_zoom(2.5)
        window.mark_section.setExpanded(True)
        window.canvas_scroll.horizontalScrollBar().setValue(20)
        before = copy.deepcopy((window._snapshot(), window._documents, window.undo_stack,
            window.redo_stack, window.saved_snapshot, window._active_document))
        canvas, tab_bar = window.canvas, window.tab_bar
        view = (canvas.zoom, window.mark_section.isExpanded(),
                window.canvas_scroll.horizontalScrollBar().value())
        for theme in ("dark", "light"):
            window._change_theme(theme)
            self.assertIs(window.canvas, canvas)
            self.assertIs(window.tab_bar, tab_bar)
            self.assertEqual((window._snapshot(), window._documents, window.undo_stack,
                window.redo_stack, window.saved_snapshot, window._active_document), before)
            self.assertEqual((canvas.zoom, window.mark_section.isExpanded(),
                window.canvas_scroll.horizontalScrollBar().value()), view)
        window.tab_bar.setCurrentIndex(0)
        self.assertTrue(window.redo_stack)

    def test_language_rebuild_retains_dark_mode_and_translated_menu(self):
        window = self.window()
        window.add_rectangle()
        window._change_theme("dark")
        for language, label, menu in (("en", "Dark", "Appearance"), ("zh_TW", "暗色", "外觀")):
            with self.subTest(language=language):
                window._change_language(language)
                self.assertEqual(window.theme_name, "dark")
                self.assertEqual(window.theme_actions["dark"].text(), label)
                self.assertTrue(window.theme_actions["dark"].isChecked())
                self.assertIn(menu, [action.text() for action in window.menuBar().actions()])
                self.assertEqual(len(window.items), 1)
                self.assertEqual(len(window.undo_stack), 1)

    def test_dark_chrome_renders_but_paper_and_print_colors_stay_light(self):
        window = self.window()
        window.show()
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                window._change_theme(theme)
                self.app.processEvents()
                image = window.grab().toImage()
                self.assertEqual(image.pixelColor(4, image.height()-4), QColor(THEMES[theme]["background"]))
                canvas = window.canvas.grab().toImage()
                self.assertEqual(canvas.pixelColor(4, 4), QColor(THEMES[theme]["canvas"]))
                self.assertEqual(canvas.pixelColor(100, 100), PAPER)

    def test_dark_dialog_input_and_disabled_text_inherit_readable_palette(self):
        window = self.window()
        window._change_theme("dark")
        dialog = QDialog(window)
        layout = QVBoxLayout(dialog)
        field = QLineEdit("Readable text")
        layout.addWidget(field)
        spin = QDoubleSpinBox()
        spin.setValue(10)
        layout.addWidget(spin)
        dialog.show()
        self.app.processEvents()
        self.assertEqual(field.palette().color(QPalette.ColorRole.Text), QColor(THEMES["dark"]["text"]))
        self.assertEqual(field.palette().color(QPalette.ColorRole.Base), QColor(THEMES["dark"]["input"]))
        option = QStyleOptionSpinBox()
        spin.initStyleOption(option)
        up = spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option,
                                        QStyle.SubControl.SC_SpinBoxUp, spin)
        QTest.mouseClick(spin, Qt.MouseButton.LeftButton, pos=up.center())
        self.assertEqual(spin.value(), 11)
        field.setEnabled(False)
        self.app.processEvents()
        self.assertEqual(field.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text),
                         QColor(THEMES["dark"]["disabled"]))
        dialog.close()

    def test_theme_does_not_change_cut_commands_or_pdf_pixels(self):
        window = self.window()
        window.items = [image_item(bleed={"width_mm": 1, "color": "#ff0000"}),
            rectangle(x=50, print_style={"fill": "#0000ff", "stroke": "#00ff00", "width_mm": .3})]
        window.metadata = {"print_cut": {"enabled": True, "paper_width": 100, "paper_height": 100,
            "bounds": [6, 6, 94, 94], "offset": [-.1, .1]}}
        before = copy.deepcopy(window._snapshot())
        outputs, images = [], []
        with tempfile.TemporaryDirectory() as folder:
            for theme in ("light", "dark"):
                window._change_theme(theme)
                outputs.append(window._encoded_plt())
                path = Path(folder)/f"{theme}.pdf"
                write_print_pdf(path, window.items, window.metadata)
                pdf = QPdfDocument()
                self.assertEqual(pdf.load(str(path)), QPdfDocument.Error.None_)
                images.append(pdf.render(0, QSize(1000, 1000)))
                pdf.close()
        self.assertFalse(images[0].isNull())
        self.assertEqual(images[0], images[1])
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(window._snapshot(), before)

    def test_unknown_saved_theme_falls_back_to_light(self):
        self.settings.setValue("theme", "unsupported")
        window = self.window()
        self.assertEqual(window.theme_name, "light")
        self.assertTrue(window.theme_actions["light"].isChecked())


if __name__ == "__main__":
    unittest.main()

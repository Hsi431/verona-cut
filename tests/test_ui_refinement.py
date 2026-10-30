"""Behavior checks for the compact editor toolbar and inspector."""
import copy
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel, QMenu,
                               QPushButton, QStyleOptionToolButton, QToolButton)

from verona.i18n import set_language, tr
from verona.raster_import import _png_base64
from verona.ui import BleedDialog, PackingDialog, VeronaWindow, _item_path


class UiRefinementTests(unittest.TestCase):
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
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {key: self.temp.name for key in
                            ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")})
        environment.start()
        self.addCleanup(environment.stop)
        settings = QSettings("Verona", "Workspace")
        settings.clear()
        settings.sync()
        set_language("zh_TW")
        self.window = VeronaWindow()
        self.addCleanup(self._close_window, self.window)

    @staticmethod
    def _close_window(window):
        with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
            window.close()

    def _action(self, label):
        return next(action for action in self.window.design_toolbar.actions()
                    if action.text() == tr(label))

    def _image_item(self, name, x):
        image = QImage(12, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor("#447766"))
        contour = [(x, 20), (x + 12, 20), (x + 12, 28), (x, 28), (x, 20)]
        return {"name": name, "contours": [contour], "cut": True,
                "image_data": _png_base64(image),
                "image_transform": [x, 20, 1, 0, 0, 1],
                "image_clip": [copy.deepcopy(contour)]}

    def test_toolbar_and_canvas_fit_1000_by_700_in_both_languages(self):
        for language in ("zh_TW", "en"):
            with self.subTest(language=language):
                set_language(language)
                window = VeronaWindow()
                self.addCleanup(self._close_window, window)
                window.resize(1000, 700)
                window.show()
                self.app.processEvents()
                self.assertEqual(window.size().width(), 1000)
                self.assertLessEqual(window.minimumSizeHint().width(), 1000)
                self.assertLessEqual(window.design_toolbar.width(), window.centralWidget().width())

    def test_native_toolbar_actions_create_rectangle_and_circle(self):
        for label in ("矩形", "圓形"):
            action = self._action(label)
            button = self.window.design_toolbar.widgetForAction(action)
            self.assertIsInstance(button, QToolButton)
            button.click()
            self.assertEqual(self.window.items[-1]["name"], tr(label))
        self.assertEqual(len(self.window.items), 2)

    def test_narrow_toolbar_keeps_overflow_actions_in_native_menu(self):
        self.window.show()
        self.app.processEvents()
        toolbar = self.window.design_toolbar
        toolbar.setFixedWidth(300)
        self.app.processEvents()

        extension = toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
        self.assertIsNotNone(extension)
        self.assertTrue(extension.isVisible())
        circle = self._action("圓形")
        self.assertTrue(toolbar.widgetForAction(circle).isHidden())
        native_menu = extension.menu()
        self.assertIsInstance(native_menu, QMenu)
        self.assertIn(circle, native_menu.actions())

    def test_geometry_controls_keep_labels_accessible_edit_and_undo(self):
        button = self.window.design_toolbar.widgetForAction(self._action("矩形"))
        button.click()
        self.window.saved_snapshot = self.window._document_state()
        self.window._refresh_tab_titles()

        for control, visible_label, accessible_name in (
                (self.window.x_spin, "X", "X 起點"),
                (self.window.y_spin, "Y", "Y 起點"),
                (self.window.width_spin, "寬度", "寬度"),
                (self.window.height_spin, "高度", "高度")):
            label = next((widget for widget in self.window.findChildren(QLabel)
                          if widget.buddy() is control), None)
            self.assertIsNotNone(label)
            self.assertEqual(label.text(), tr(visible_label))
            self.assertEqual(control.accessibleName(), tr(accessible_name))

        self.window.lock_ratio.setChecked(False)
        self.window.x_spin.setValue(11)
        self.window.y_spin.setValue(17)
        self.window.width_spin.setValue(80)
        self.window.height_spin.setValue(35)
        self.window.selection_edit_buttons[0].click()
        bounds = _item_path(self.window.items[0]).boundingRect()
        self.assertAlmostEqual(bounds.left(), 11)
        self.assertAlmostEqual(bounds.top(), 17)
        self.assertAlmostEqual(bounds.width(), 80)
        self.assertAlmostEqual(bounds.height(), 35)
        self.assertTrue(self.window.is_dirty())
        self.assertTrue(self.window.tab_bar.tabText(0).endswith("*"))

        self.window.undo()
        self.assertFalse(self.window.is_dirty())
        self.assertFalse(self.window.tab_bar.tabText(0).endswith("*"))
        self.assertAlmostEqual(_item_path(self.window.items[0]).boundingRect().width(), 40)

    def test_bleed_apply_keeps_multi_selection_scope_and_accessible_controls(self):
        self.window.items = [self._image_item("first", 15),
                             {"name": "vector", "contours": [[(70, 20), (80, 20),
                              (80, 30), (70, 30), (70, 20)]], "cut": True},
                             self._image_item("second", 35)]
        self.window.select_many([0, 2])
        untouched = copy.deepcopy(self.window.items[1])
        dialog = BleedDialog(self.window, [0, 2])
        apply_button = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
        self.assertEqual(apply_button.text(), tr("套用至 {count} 張圖片", count=2))
        self.assertTrue(apply_button.isEnabled())
        form = dialog.layout().itemAt(0).layout()
        self.assertEqual(form.labelForField(dialog.width_spin).text(), tr("外側出血"))
        self.assertEqual(form.labelForField(dialog.inner_spin).text(), tr("內側補色"))
        preview = next(button for button in dialog.findChildren(QPushButton)
                       if button.text() == tr("預覽"))
        self.assertTrue(preview.toolTip())

        def accept_with_changed_width():
            dialog.width_spin.setValue(1.25)
            return QDialog.DialogCode.Accepted

        dialog.exec = accept_with_changed_width
        with patch("verona.ui.BleedDialog", return_value=dialog):
            self.window.edit_selected_bleed()
        self.assertEqual(self.window.items[1], untouched)
        self.assertEqual(self.window.items[0]["bleed"]["width_mm"], 1.25)
        self.assertEqual(self.window.items[2]["bleed"]["width_mm"], 1.25)

    def test_packing_preview_state_is_invalidated_before_apply(self):
        self.window.items = [{"name": "rectangle", "contours": [[(10, 10), (30, 10),
                              (30, 20), (10, 20), (10, 10)]], "cut": True}]
        dialog = PackingDialog(self.window, [0])
        self.assertEqual(dialog.preview_label.text(), tr("尚未預覽"))
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.plan = (["synthetic"], [0], {})
        dialog.apply_button.setEnabled(True)

        dialog.copies_spin.setValue(2)

        self.assertIsNone(dialog.plan)
        self.assertFalse(dialog.apply_button.isEnabled())
        self.assertFalse(dialog.summary.isHidden())
        dialog.close()

    @staticmethod
    def _contrast(first, second):
        def luminance(color):
            channels = []
            for channel in (color.red(), color.green(), color.blue()):
                value = channel / 255
                channels.append(value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4)
            return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]
        high, low = sorted((luminance(first), luminance(second)), reverse=True)
        return (high + .05) / (low + .05)

    def test_section_headers_are_keyboard_operable_full_width_and_contrasting(self):
        self.window.resize(1000, 700)
        self.window.show()
        self.app.processEvents()
        section = self.window.path_section
        toggle = section._toggle
        self.assertTrue(toggle.isCheckable())
        self.assertGreaterEqual(toggle.width(), section.width() - 2)
        self.assertTrue(section.toolTip())

        toggle.setFocus()
        QTest.keyClick(toggle, Qt.Key.Key_Space)
        self.assertTrue(section.isExpanded())
        self.assertFalse(section._content.isHidden())
        QTest.keyClick(toggle, Qt.Key.Key_Space)
        self.assertFalse(section.isExpanded())

        panel = self.window.inspector_scroll.widget()
        for theme in ("light", "dark"):
            self.window._apply_theme(theme)
            section.setExpanded(True)
            self.app.processEvents()
            option = QStyleOptionToolButton()
            toggle.initStyleOption(option)
            foreground = option.palette.color(QPalette.ColorRole.ButtonText)
            image = panel.grab().toImage()
            button_origin = toggle.mapTo(panel, toggle.rect().topLeft())
            background = image.pixelColor(button_origin.x() + toggle.width() - 5,
                                          button_origin.y() + toggle.height() // 2)
            self.assertGreaterEqual(self._contrast(foreground, background), 4.5,
                                    f"{theme} section heading lacks text contrast")


if __name__ == "__main__":
    unittest.main()

"""Editable text object regression coverage."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QSettings, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from verona.artwork import item_bounds
from verona.design_validation import validate_document
from verona.editor_document import build_document_edit
from verona.text_tool import font_families, font_styles, make_text_data, text_transform
from verona.ui import VeronaWindow


class EditableTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          "/tmp/text-tool-test-config")

    def setUp(self):
        self.window = VeronaWindow()

    def tearDown(self):
        self.window.deleteLater()
        self.app.processEvents()

    def test_inserted_text_keeps_editable_parameters_with_its_outline(self):
        with patch("verona.ui.TextLayoutDialog") as text_dialog:
            text_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            text_dialog.return_value.text_parameters.return_value = make_text_data(
                "Verona", "Noto Sans", "Regular", 10, 0, 12)
            self.window.add_text()
        item = self.window.items[0]
        self.assertIn("text_data", item)
        self.assertIn("text_source_contours", item)
        self.assertEqual(item["text_data"]["content"], "Verona")
        self.assertGreater(len(item["contours"]), 0)

    def test_requested_glyph_height_and_letter_spacing_reach_outline_builder(self):
        family = font_families()[0]
        base, *_ = build_document_edit([], {}, {}, [], "add_text", {
            "text": "AB", "font_family": family, "height_mm": 12.0,
            "x_mm": 10.0, "y_mm": 20.0})
        spaced, *_ = build_document_edit([], {}, {}, [], "add_text", {
            "text": "AB", "font_family": family, "height_mm": 12.0,
            "letter_spacing_mm": 1.5, "x_mm": 10.0, "y_mm": 20.0})
        self.assertLess(abs(item_bounds(base[0]).height() - 12), .24)
        self.assertGreater(item_bounds(spaced[0]).width(), item_bounds(base[0]).width())
        with self.assertRaises(ValueError):
            build_document_edit([], {}, {}, [], "add_text", {
                "text": "  ", "font_family": family, "height_mm": 12.0})

    def test_rotated_and_moved_text_keeps_its_transform_after_edit_and_reopen(self):
        family = font_families()[0]
        style = font_styles(family)[0]
        with patch("verona.ui.TextLayoutDialog") as text_dialog:
            text_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            text_dialog.return_value.text_parameters.return_value = make_text_data(
                "Verona", family, style, 10, 0, 12)
            self.window.add_text()
        self.window.transform_selected(rotation=31, mirror_x=True)
        bounds = self.window.selection_bounds()
        self.window.x_spin.setValue(bounds.left() + 17.25)
        self.window.y_spin.setValue(bounds.top() + 8.5)
        self.window.apply_size()
        item = self.window.items[0]
        old_width = item_bounds(item).width()
        original_transform = text_transform(item["text_source_contours"], item["contours"])
        expected = (original_transform.m11(), original_transform.m12(), original_transform.m21(),
                    original_transform.m22(), original_transform.dx(), original_transform.dy())
        with patch("verona.ui.TextLayoutDialog") as text_dialog:
            text_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            text_dialog.return_value.text_parameters.return_value = make_text_data(
                "Verona cut", family, style, 10, 0, 12,
                origin_mm=tuple(item["text_data"]["origin_mm"]))
            self.window.edit_selected_text()
        edited = self.window.items[0]
        changed = text_transform(edited["text_source_contours"], edited["contours"])
        actual = (changed.m11(), changed.m12(), changed.m21(), changed.m22(),
                  changed.dx(), changed.dy())
        self.assertEqual(edited["text_data"]["content"], "Verona cut")
        self.assertGreater(item_bounds(edited).width(), old_width)
        for before, after in zip(expected, actual):
            self.assertAlmostEqual(before, after, delta=.03)

        os.makedirs("/tmp/text-tool-tests", exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="roundtrip-", dir="/tmp/text-tool-tests") as folder:
            filename = os.path.join(folder, "editable.jdesign")
            with patch.object(QFileDialog, "getSaveFileName", return_value=(filename, "Design")):
                self.assertTrue(self.window.save_design(save_as=True))
            reopened = VeronaWindow()
            self.assertTrue(reopened.open_path(filename))
            restored = reopened.items[0]
            self.assertIn("text_data", restored)
            self.assertEqual(restored["text_data"]["content"], "Verona cut")
            restored_transform = text_transform(restored["text_source_contours"], restored["contours"])
            self.assertAlmostEqual(restored_transform.m12(), expected[1], delta=.03)
            self.assertAlmostEqual(restored_transform.dx(), expected[4], delta=.03)
            reopened.select(0)
            with patch("verona.ui.TextLayoutDialog") as text_dialog:
                text_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
                text_dialog.return_value.text_parameters.return_value = make_text_data(
                    "Verona cut again", family, style, 10, 0, 12,
                    origin_mm=tuple(restored["text_data"]["origin_mm"]))
                reopened.edit_selected_text()
            self.assertEqual(reopened.items[0]["text_data"]["content"], "Verona cut again")
            reopened.deleteLater()
            self.app.processEvents()

    def test_missing_font_keeps_saved_outline_and_edit_prompts_for_replacement(self):
        family = font_families()[0]
        style = font_styles(family)[0]
        with patch("verona.ui.TextLayoutDialog") as text_dialog:
            text_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            text_dialog.return_value.text_parameters.return_value = make_text_data(
                "Verona", family, style, 10, 0, 12)
            self.window.add_text()
        item = self.window.items[0]
        original = [list(contour) for contour in item["contours"]]
        item["text_data"]["font_family"] = "Verona Missing Family 0123"
        safe, _ = validate_document({"format": "verona", "version": 1, "units": "mm",
                                     "items": [item]})
        self.assertEqual(safe[0]["contours"], original)
        self.window.items = safe
        self.window.select(0)
        replacement = font_families()[0]
        with patch.object(QMessageBox, "warning") as warning, \
                patch("verona.ui.TextLayoutDialog") as text_dialog:
            text_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            text_dialog.return_value.text_parameters.return_value = make_text_data(
                "Verona", replacement, font_styles(replacement)[0], 10, 0, 12,
                origin_mm=tuple(item["text_data"]["origin_mm"]))
            self.window.edit_selected_text()
        warning.assert_called_once()
        self.assertEqual(self.window.items[0]["text_data"]["font_family"], replacement)

    def test_convert_to_outlines_removes_text_data_and_undo_restores_it(self):
        family = font_families()[0]
        with patch("verona.ui.TextLayoutDialog") as text_dialog:
            text_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            text_dialog.return_value.text_parameters.return_value = make_text_data(
                "Verona", family, font_styles(family)[0], 10, 0, 12)
            self.window.add_text()
        original = [list(contour) for contour in self.window.items[0]["contours"]]
        self.window.convert_selected_text_to_outlines()
        self.assertNotIn("text_data", self.window.items[0])
        self.assertNotIn("text_source_contours", self.window.items[0])
        self.assertEqual(self.window.items[0]["contours"], original)
        self.window.undo()
        self.assertIn("text_data", self.window.items[0])

    def test_canvas_double_click_opens_text_editor(self):
        family = font_families()[0]
        style = font_styles(family)[0]
        with patch("verona.ui.TextLayoutDialog") as text_dialog:
            text_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            text_dialog.return_value.text_parameters.return_value = make_text_data(
                "Verona", family, style, 10, 0, 12)
            self.window.add_text()
        item = self.window.items[0]
        origin = self.window.canvas.canvas_origin()
        bounds = item_bounds(item)
        point = QPointF(origin.x() + bounds.center().x() * self.window.canvas.zoom,
                        origin.y() + bounds.center().y() * self.window.canvas.zoom)
        event = QMouseEvent(QMouseEvent.Type.MouseButtonDblClick, point,
            QPointF(self.window.canvas.mapToGlobal(point.toPoint())), Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        with patch("verona.ui.TextLayoutDialog") as text_dialog:
            text_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            text_dialog.return_value.text_parameters.return_value = make_text_data(
                "Edited", family, style, 10, 0, 12,
                origin_mm=tuple(item["text_data"]["origin_mm"]))
            self.window.canvas.mouseDoubleClickEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(self.window.items[0]["text_data"]["content"], "Edited")
        menu = self.window.canvas_context_menu()
        self.assertIn(self.window.edit_text_action, menu.actions())
        self.assertTrue(self.window.edit_text_action.isEnabled())


if __name__ == "__main__":
    unittest.main()

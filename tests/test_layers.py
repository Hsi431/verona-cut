"""Layer state controls actual output, survives save/undo and prevents edits."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtPdf import QPdfDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from verona.artwork import draw_printed_item
from verona.commands import encode_document
from verona.editor_actions import build_edit
from verona.editor_document import build_document_edit
from verona.i18n import set_language, tr
from verona.layers import printable
from verona.path_order import cut_contours
from verona.printing import write_print_pdf
from verona.raster_import import ImportImageDialog
from verona.ui import VeronaWindow
from tests.test_editor_actions import rectangle
from tests.test_raster_import import png_base64


def image_item(color="red"):
    image = QImage(4, 4, QImage.Format.Format_RGBA8888)
    image.fill(QColor(color))
    item = rectangle(x=20, y=20, width=20, height=20)
    item.update(name=color, image_data=png_base64(image), image_transform=[20., 20., 5., 0., 0., 5.])
    return item


class LayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, self.directory.name)
        set_language("en")
        self.window = VeronaWindow()

    def tearDown(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def install(self, items):
        self.window.items = items
        self.window.select(-1)
        self.window.refresh_all()

    def row(self, object_index):
        tree = self.window.layer_panel.tree
        return next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                    if tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == object_index)

    def test_import_modes_are_explicit_and_print_only_has_no_cut_job_or_marks(self):
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor("red"))
        dialog = ImportImageDialog(image, "photo")
        self.assertEqual([dialog.mode_combo.itemText(i) for i in range(3)],
                         [tr("列印＋切割"), tr("僅列印"), tr("僅切割")])
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("background"))
        self.assertFalse(dialog.mask_mode_combo.isEnabled())
        self.assertTrue(dialog.width_spin.isEnabled())
        self.window._append_image_items(dialog.build_items(), "background")
        self.assertEqual(cut_contours(self.window.items, self.window.metadata), [])
        self.assertFalse(self.window.metadata["print_cut"]["enabled"])
        output = Path(self.directory.name) / "print-only.pdf"
        write_print_pdf(output, self.window.items, self.window.metadata)
        pdf = QPdfDocument()
        pdf.load(str(output))
        page = pdf.render(0, pdf.pagePointSize(0).toSize())
        self.assertEqual(page.pixelColor(17, 17), QColor("white"))
        self.assertEqual(page.pixelColor(page.width()//2, page.height()//2), QColor("red"))
        pdf.close()
        dialog.deleteLater()

    def test_cut_only_retains_original_image_and_can_be_switched_to_print(self):
        image = QImage(8, 8, QImage.Format.Format_RGBA8888)
        image.fill(QColor("red"))
        dialog = ImportImageDialog(image, "photo")
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("cut_only"))
        self.window._append_image_items(dialog.build_items(), "cut_only")
        item = self.window.items[0]
        pixels, contours = item["image_data"], copy.deepcopy(item["contours"])
        self.assertFalse(printable(item))
        self.window.output_mode.setCurrentIndex(self.window.output_mode.findData("print_only"))
        self.assertTrue(printable(item))
        self.assertFalse(item["cut"])
        self.assertEqual((item["image_data"], item["contours"]), (pixels, contours))
        self.window.undo()
        self.assertFalse(printable(self.window.items[0]))
        self.assertTrue(self.window.items[0]["cut"])
        dialog.deleteLater()

    def test_layer_selection_rename_and_state_changes_are_undoable(self):
        self.install([image_item("red"), image_item("blue")])
        self.row(0).setSelected(True)
        self.assertEqual(self.window.selected_indices(), {0})
        self.row(0).setText(0, "Background artwork")
        self.assertEqual(self.window.items[0]["name"], "Background artwork")
        self.window.undo()
        self.assertEqual(self.window.items[0]["name"], "red")
        self.row(0).setCheckState(4, Qt.CheckState.Unchecked)
        self.assertFalse(self.window.items[0]["cut"])
        self.assertEqual(self.window.output_mode.currentData(), "print_only")

    def test_hidden_layer_is_absent_from_cut_data_printing_and_bounds_checks(self):
        hidden, normal = image_item(), image_item("blue")
        hidden["contours"] = [[(400., 400.), (450., 450.)]]
        hidden["visible"] = False
        self.install([hidden, normal])
        settings = self.window._cut_settings()
        self.assertEqual(encode_document(self.window.items, {}, settings), encode_document([normal], {}, settings))
        self.window.metadata = {"print_cut": {"enabled": True, "bounds": [6, 6, 204, 291]}}
        self.assertFalse(self.window._print_cut_violations())
        canvas = QImage(80, 80, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(QColor("white"))
        painter = QPainter(canvas)
        draw_printed_item(painter, hidden)
        painter.end()
        self.assertEqual(canvas.pixelColor(25, 25), QColor("white"))

    def test_locked_layers_still_output_but_all_geometry_edits_are_blocked(self):
        item = image_item()
        item["locked"] = True
        self.install([item])
        self.window.select(0)
        before = self.window._snapshot()
        for operation in (self.window.delete_selected, self.window.duplicate_selected,
                          self.window.apply_size, lambda: self.window.transform_selected(rotation=90),
                          self.window.edit_nodes, self.window.edit_selected_image):
            operation()
            self.assertEqual(self.window._snapshot(), before)
        self.assertFalse(self.window.width_spin.isEnabled())
        self.assertFalse(self.window.canvas.selection_handles())
        self.assertTrue(encode_document(self.window.items, {}, self.window._cut_settings()))
        self.assertTrue(printable(item))
        with self.assertRaisesRegex(ValueError, "Unlock"):
            build_edit(self.window.items, {}, [0], "delete", {})
        with self.assertRaisesRegex(ValueError, "Unlock"):
            build_document_edit(self.window.items, {}, self.window._cut_settings(), [0], "vector_print", {})
        self.row(0).setCheckState(2, Qt.CheckState.Unchecked)
        self.assertTrue(self.window.width_spin.isEnabled())
        self.window.delete_selected()
        self.assertEqual(self.window.items, [])

    def test_locked_and_hidden_items_are_not_dragged_from_canvas(self):
        item = image_item()
        for state in ({"locked": True}, {"visible": False}):
            self.install([{**item, **state}])
            before = copy.deepcopy(self.window.items)
            zoom = self.window.canvas.zoom
            start = QPoint(round(30*zoom+34), round(30*zoom+34))
            QTest.mousePress(self.window.canvas, Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(self.window.canvas, start+QPoint(30, 0))
            QTest.mouseRelease(self.window.canvas, Qt.MouseButton.LeftButton, pos=start+QPoint(30, 0))
            self.assertEqual(self.window.items, before)

    def test_front_back_order_changes_print_stacking_and_preserves_selection_and_undo(self):
        self.install([image_item("red"), image_item("blue")])
        self.window.select(0)
        before = copy.deepcopy(self.window.items)
        self.window.move_layers(1)
        self.assertEqual([item["name"] for item in self.window.items], ["blue", "red"])
        self.assertEqual(self.window.selected_indices(), {1})
        self.assertEqual(self.window.layer_panel.tree.topLevelItem(0).text(0), "red")
        canvas = QImage(80, 80, QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(canvas)
        for item in self.window.items:
            draw_printed_item(painter, item)
        painter.end()
        self.assertEqual(canvas.pixelColor(25, 25), QColor("red"))
        self.window.undo()
        self.assertEqual(self.window.items, before)
        self.assertEqual(self.window.selected_indices(), {0})

    def test_flags_and_order_roundtrip_and_non_boolean_flags_are_rejected(self):
        items = [image_item(), image_item("blue")]
        items[0].update(visible=False, locked=True, print=False)
        self.install(items)
        path = Path(self.directory.name) / "layers.jdesign"
        self.window._write_document(str(path))
        data = json.loads(path.read_text())
        restored, _ = VeronaWindow._validate_document(data)
        self.assertEqual(restored, items)
        for key in ("visible", "locked", "print", "cut"):
            data["items"][0][key] = "false"
            with self.subTest(key=key), self.assertRaises(ValueError):
                VeronaWindow._validate_document(data)
            data["items"][0][key] = False

    def test_print_switch_does_not_change_cut_bytes_and_locked_checkbox_cannot_edit_output(self):
        self.install([image_item()])
        self.window.select(0)
        before = self.window._encoded_plt()
        self.row(0).setCheckState(3, Qt.CheckState.Unchecked)
        self.assertEqual(self.window._encoded_plt(), before)
        self.assertFalse(printable(self.window.items[0]))
        self.row(0).setCheckState(2, Qt.CheckState.Checked)
        self.row(0).setCheckState(4, Qt.CheckState.Unchecked)
        self.assertTrue(self.window.items[0]["cut"])
        self.assertEqual(self.row(0).checkState(4), Qt.CheckState.Checked)


if __name__ == "__main__":
    unittest.main()

"""Raster import, print fidelity and editable-document integration."""

import copy
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from verona.i18n import set_language
from verona.printing import write_print_pdf
from verona.raster_import import ImportImageDialog, decode_embedded_image
from verona.ui import VeronaWindow, _item_path


class RasterWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_folder = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings_folder.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings_folder.cleanup()

    def setUp(self):
        set_language("zh_TW")
        QSettings("Verona", "Workspace").clear()

    @staticmethod
    def image():
        image = QImage(30, 20, QImage.Format.Format_RGBA8888)
        image.fill(QColor("white"))
        painter = QPainter(image)
        painter.fillRect(10, 5, 10, 10, QColor("black"))
        painter.end()
        return image

    def test_import_png_enables_a4_and_roundtrips_without_changing_source(self):
        window = VeronaWindow()
        reopened = VeronaWindow()
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "art.png"
            self.image().save(str(source))
            original = source.read_bytes()
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "PNG")), \
                    patch.object(ImportImageDialog, "exec", lambda self: QDialog.DialogCode.Accepted):
                window.import_image()
            self.assertEqual(len(window.items), 1)
            self.assertEqual(len(window.undo_stack), 1)
            self.assertTrue(window.metadata["print_cut"]["enabled"])
            bounds = _item_path(window.items[0]).boundingRect()
            self.assertAlmostEqual(bounds.center().x(), 105)
            self.assertAlmostEqual(bounds.center().y(), 148.5)
            plt = window._encoded_plt()
            window.current_file = str(Path(folder) / "art.jdesign")
            self.assertTrue(window.save_design())
            self.assertTrue(reopened._load_document(window.current_file))
            self.assertEqual(reopened._encoded_plt(), plt)
            self.assertEqual(reopened.items, window.items)
            self.assertEqual(source.read_bytes(), original)
            window.undo()
            self.assertEqual(window.items, [])
            self.assertNotIn("print_cut", window.metadata)
        with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
            window.close()
        reopened.close()

    def test_fading_canvas_does_not_change_printed_png_or_cut_commands(self):
        window = VeronaWindow()
        dialog = ImportImageDialog(self.image(), "art")
        window.items = dialog.build_items()
        window.metadata = {"print_cut": {"enabled": True, "paper_width": 300, "paper_height": 300,
                                        "bounds": [6, 6, 294, 294]}}
        window.refresh_all()
        source = copy.deepcopy(window.items)
        plt = window._encoded_plt()
        streams = []
        with tempfile.TemporaryDirectory() as folder:
            for fade in (False, True):
                window.cutline_view.setChecked(fade)
                filename = Path(folder) / f"fade-{fade}.pdf"
                write_print_pdf(filename, window.items, window.metadata)
                streams.append([zlib.decompress(data) for header, data in
                    re.findall(rb"<<([^<>]*)>>\s*stream\n(.*?)\nendstream", filename.read_bytes(), re.S)
                    if b"/FlateDecode" in header])
        self.assertTrue(streams[0])
        self.assertEqual(streams[0], streams[1])
        self.assertEqual(window.items, source)
        self.assertEqual(window._encoded_plt(), plt)
        with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
            window.close()
        dialog.close()

    def test_oversized_print_cut_import_warns_without_changing_geometry(self):
        window = VeronaWindow()
        image = QImage(100, 100, QImage.Format.Format_RGBA8888)
        image.fill(QColor("black"))

        def accept_oversized(dialog):
            dialog.width_spin.setValue(220)
            return QDialog.DialogCode.Accepted

        try:
            with tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / "large.png"
                image.save(str(source))
                with patch.object(QFileDialog, "getOpenFileName", return_value=(str(source), "PNG")), \
                        patch.object(ImportImageDialog, "exec", accept_oversized):
                    window.import_image()
            self.assertFalse(window.bounds_warning.isHidden())
            self.assertEqual(set(window._print_cut_violations()), {0})
            bounds = _item_path(window.items[0]).boundingRect()
            self.assertAlmostEqual(bounds.width(), 220)
            self.assertAlmostEqual(bounds.left(), -5)
            with self.assertRaises(ValueError):
                window._encoded_plt()
            window.undo()
            self.assertTrue(window.bounds_warning.isHidden())
            window.redo()
            self.assertFalse(window.bounds_warning.isHidden())
        finally:
            with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                window.close()

    def test_bleed_remains_visible_after_opaque_white_background_is_removed(self):
        dialog = ImportImageDialog(self.image(), "art")
        dialog.width_spin.setValue(60)
        dialog.bleed_color = QColor("magenta")
        cut_before = dialog.build_items()[0]["contours"]
        dialog.bleed_spin.setValue(2)
        item = dialog.build_items()[0]
        raster = decode_embedded_image(item["image_data"])
        margin = (raster.width() - dialog.image.width()) // 2
        self.assertEqual(raster.pixelColor(margin+9, margin+10), QColor("magenta"))
        self.assertEqual(raster.pixelColor(margin+12, margin+10), QColor("black"))
        self.assertEqual(item["contours"], cut_before)
        clip = _item_path({"contours": item["image_clip"]})
        tx, ty, sx, _, _, sy = item["image_transform"]
        self.assertTrue(clip.contains(QPointF(tx+(margin+9.5)*sx, ty+(margin+10.5)*sy)))
        dialog.close()

    def test_empty_mask_stays_in_dialog_and_oversized_bleed_is_rejected(self):
        image = QImage(20, 20, QImage.Format.Format_RGBA8888)
        image.fill(QColor("white"))
        dialog = ImportImageDialog(image, "empty")
        with patch.object(QMessageBox, "warning") as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        dialog.background_color = QColor("black")
        dialog.width_spin.setValue(.0001)
        dialog.bleed_spin.setValue(5)
        with self.assertRaises(ValueError):
            dialog.build_items()
        dialog.close()

    def test_dpi_changes_physical_size_and_minimum_area_removes_speckles(self):
        image = QImage(100, 100, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillRect(10, 10, 60, 60, QColor("black"))
        painter.fillRect(80, 80, 2, 2, QColor("black"))
        painter.end()
        dialog = ImportImageDialog(image, "speckles")
        dialog.dpi_spin.setValue(25.4)
        self.assertAlmostEqual(dialog.width_spin.value(), 100)
        self.assertAlmostEqual(dialog.height_spin.value(), 100)
        self.assertEqual(len(dialog.build_items()[0]["contours"]), 2)
        dialog.minimum_area.setValue(5)
        self.assertEqual(len(dialog.build_items()[0]["contours"]), 1)
        dialog.width_spin.setValue(50)
        self.assertAlmostEqual(dialog.dpi_spin.value(), 50.8)
        dialog.close()


if __name__ == "__main__":
    unittest.main()

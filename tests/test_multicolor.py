"""Foreground color unions share GUI/MCP tracing and keep original artwork."""
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QColorDialog

from verona.mask_edit import EditImageDialog, retrace_item
from verona.raster_import import ImportImageDialog, _png_base64, _trace_mask, raster_options
from verona.ui import VeronaWindow
from tests.test_editor_actions import image_item


class MulticolorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def image():
        image = QImage(12, 6, QImage.Format.Format_RGBA8888)
        image.fill(QColor("white"))
        painter = QPainter(image)
        for x, color in ((0, "red"), (4, "blue"), (8, "green")):
            painter.fillRect(x, 0, 3, 6, QColor(color))
        painter.end()
        return image

    def test_selected_color_union_does_not_select_other_colors_or_transparent_pixels(self):
        image = self.image()
        image.setPixelColor(0, 0, QColor(255, 0, 0, 0))
        mask = _trace_mask(image, QColor("white"), 0, False, False, ["#ff0000", "#0000ff"])
        self.assertEqual(int(mask.sum()), 35)
        self.assertFalse(mask[0, 0])
        self.assertFalse(mask[:, 8:11].any())
        self.assertTrue(mask[:, 4:7].all())

    def test_tolerance_includes_boundary_and_invalid_palette_is_rejected(self):
        image = QImage(1, 1, QImage.Format.Format_RGBA8888)
        image.fill(QColor(245, 4, 4))
        self.assertFalse(_trace_mask(image, QColor("white"), 9, False, False, ["#ff0000"])[0, 0])
        self.assertTrue(_trace_mask(image, QColor("white"), 10, False, False, ["#ff0000"])[0, 0])
        for colors in ([], ["red"], [True], ["#ff0000"]*17):
            with self.subTest(colors_count=len(colors)), self.assertRaises(ValueError):
                raster_options(image, {"mask_mode": "colors", "colors": colors})

    def test_gui_palette_and_reedit_roundtrip_preserve_color_selection_and_pixels(self):
        image = self.image()
        dialog = ImportImageDialog(image, "palette")
        try:
            dialog.mask_mode_combo.setCurrentIndex(dialog.mask_mode_combo.findData("colors"))
            for color in ("red", "blue"):
                with patch.object(QColorDialog, "getColor", return_value=QColor(color)):
                    dialog._choose_foreground_color()
            self.assertEqual(dialog.foreground_colors, ["#ff0000", "#0000ff"])
            item = dialog.build_items()[0]
            self.assertEqual(len(item["contours"]), 2)
            self.assertEqual(item["trace_options"]["colors"], dialog.foreground_colors)
            updated = retrace_item(item, {"mask_mode": "colors", "colors": ["#0000ff"]})
            self.assertEqual(updated["image_data"], item["image_data"])
            self.assertEqual(len(updated["contours"]), 1)
            loaded, _ = VeronaWindow._validate_document(json.loads(json.dumps({
                "format": "verona", "version": 1, "units": "mm", "items": [updated]})))
            reopened = EditImageDialog(loaded[0])
            try:
                self.assertEqual(reopened.foreground_colors, ["#0000ff"])
                self.assertEqual(reopened.build_items(), loaded)
            finally:
                reopened.close()
            dialog._clear_foreground_colors()
            self.assertEqual(dialog.build_items(), [])
        finally:
            dialog.close()

    def test_retrace_selected_colors_uses_same_geometry_as_import(self):
        image = self.image()
        item = image_item(image_data=_png_base64(image), image_transform=[0, 0, 1, 0, 0, 1])
        item.pop("image_clip")
        result = retrace_item(item, {"mask_mode": "colors", "colors": ["#ff0000", "#0000ff"],
                                     "tolerance": 0})
        self.assertEqual(len(result["contours"]), 2)
        self.assertEqual(result["image_data"], item["image_data"])

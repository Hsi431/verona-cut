"""Document-mm mask brushes and actual preview mouse mapping."""
import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QTransform
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from verona.mask_edit import EditImageDialog, paint_mask, retrace_item, working_mask
from verona.raster_import import decode_embedded_image
from verona.ui import _transform_item
from tests.test_editor_actions import image_item


class MaskBrushTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_round_remove_and_include_strokes_use_document_mm_and_preserve_source(self):
        item = image_item()
        image = decode_embedded_image(item["image_data"])
        original = working_mask(item, image)
        stroke = {"mode": "remove", "width_mm": 3, "points_mm": [[25.5, 25.5], [34.5, 25.5]]}
        changed = paint_mask(item, image, original, [stroke])
        self.assertFalse(changed[5, 5:15].any())
        self.assertTrue(changed[0].all())
        restored = paint_mask(item, image, changed, [{**stroke, "mode": "include"}])
        self.assertTrue(restored.all())
        self.assertTrue(original.all())

    def test_world_brush_tracks_mirror_rotation_and_scale(self):
        item = image_item()
        transform = QTransform().translate(80, 30).rotate(90).scale(-2, 2)
        _transform_item(item, transform)
        source = decode_embedded_image(item["image_data"])
        center = transform.map(QPointF(30.5, 25.5))
        changed = paint_mask(item, source, working_mask(item, source), [{"mode": "remove",
            "width_mm": 4, "points_mm": [[center.x(), center.y()]]}])
        self.assertFalse(changed[5, 10])
        self.assertTrue(changed[0, 0])

    def test_brush_retrace_saves_mask_but_keeps_bitmap_and_repeated_results(self):
        item = image_item()
        original = copy.deepcopy(item)
        result = retrace_item(item, {"brush_strokes": [{"mode": "remove", "width_mm": 3,
                                                       "points_mm": [[30.5, 25.5]]}]})
        self.assertEqual(result["image_data"], original["image_data"])
        self.assertEqual(result["trace_options"]["mask_mode"], "saved")
        self.assertEqual(len(result["contours"]), 2)
        reopened = retrace_item(result, {})
        self.assertEqual(reopened["contours"], result["contours"])
        self.assertEqual(item, original)

    def test_bad_strokes_cannot_modify_the_working_mask(self):
        item = image_item()
        image = decode_embedded_image(item["image_data"])
        mask = working_mask(item, image)
        for strokes in ([], [{}], [{"mode": "erase", "width_mm": 1, "points_mm": [[30, 25]]}],
            [{"mode": "remove", "width_mm": True, "points_mm": [[30, 25]]}],
            [{"mode": "remove", "width_mm": 1, "points_mm": [[float("nan"), 25]]}],
            [{"mode": "remove", "width_mm": 1, "points_mm": [[30, 25]]*1001}]):
            with self.subTest(count=len(strokes)), self.assertRaises(ValueError):
                paint_mask(item, image, mask, strokes)
            self.assertTrue(mask.all())

    def test_gui_mouse_brush_maps_preview_to_saved_mask_and_keeps_controls_locked(self):
        item = image_item()
        dialog = EditImageDialog(item)
        try:
            dialog.show()
            self.app.processEvents()
            dialog.brush_check.setChecked(True)
            dialog.brush_mode.setCurrentIndex(dialog.brush_mode.findData("remove"))
            dialog.brush_width.setValue(3)
            transform, _ = dialog.preview_to_source.inverted()
            point = transform.map(QPointF(10.5, 5.5))
            pixmap = dialog.preview_label.pixmap()
            point += QPointF((dialog.preview_label.width()-pixmap.width())/2,
                             (dialog.preview_label.height()-pixmap.height())/2)
            QTest.mouseClick(dialog.preview_label, Qt.MouseButton.LeftButton, pos=point.toPoint())
            result = dialog.build_items()[0]
            self.assertFalse(working_mask(result, dialog.image)[5, 10])
            self.assertEqual(result["image_data"], item["image_data"])
            self.assertFalse(dialog.bleed_spin.isEnabled())
            dialog.mask_mode_combo.setCurrentIndex(dialog.mask_mode_combo.findData("alpha"))
            self.assertIsNone(dialog.manual_mask)
            self.assertTrue(working_mask(dialog.build_items()[0], dialog.image).all())
        finally:
            dialog.close()

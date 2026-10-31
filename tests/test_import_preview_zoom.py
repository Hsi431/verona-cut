"""Import preview view gestures redraw at source quality without changing import data."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from verona.mask_edit import EditImageDialog, working_mask
from verona.raster_import import ImportImageDialog, _png_base64, build_raster_items, raster_options


class ImportPreviewZoomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": self.temp.name, "XDG_STATE_HOME": self.temp.name,
            "XDG_CACHE_HOME": self.temp.name, "VERONA_HOST": "",
        })
        self.environment.start()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          self.temp.name)

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    @staticmethod
    def image():
        image = QImage(120, 90, QImage.Format.Format_RGBA8888)
        image.fill(QColor("white"))
        for y in range(20, 70):
            for x in range(25, 95):
                image.setPixelColor(x, y, QColor("#163c32"))
        return image

    @staticmethod
    def to_buffer(dialog, label_point):
        pixmap = dialog.preview_label.pixmap()
        return label_point - QPointF((dialog.preview_label.width() - pixmap.width()) / 2,
                                     (dialog.preview_label.height() - pixmap.height()) / 2)

    def test_wheel_zoom_is_cursor_anchored_and_keeps_viewport_buffers_and_import_data(self):
        dialog = ImportImageDialog(self.image(), "synthetic")
        try:
            dialog.show()
            self.app.processEvents()
            label = dialog.preview_label
            cursor = QPointF(label.width() * .58, label.height() * .47)
            source_before = dialog.preview_to_source.map(self.to_buffer(dialog, cursor))
            buffer_size = dialog.preview_layers[0].size()
            source_bytes = _png_base64(dialog.image)
            options = dialog.trace_options()
            width, height, dpi = (dialog.width_spin.value(), dialog.height_spin.value(),
                                  dialog.dpi_spin.value())
            image_bounds_before = dialog.preview_to_source.inverted()[0].mapRect(
                dialog.image.rect())

            with patch.object(dialog, "_mask_and_paths", wraps=dialog._mask_and_paths) as trace:
                event = QWheelEvent(cursor, label.mapToGlobal(cursor.toPoint()), QPoint(),
                                    QPoint(0, 120), Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                                    False)
                QApplication.sendEvent(label, event)
                self.assertEqual(trace.call_count, 0)

            source_after = dialog.preview_to_source.map(self.to_buffer(dialog, cursor))
            image_bounds_after = dialog.preview_to_source.inverted()[0].mapRect(dialog.image.rect())
            self.assertAlmostEqual(source_before.x(), source_after.x(), places=5)
            self.assertAlmostEqual(source_before.y(), source_after.y(), places=5)
            self.assertAlmostEqual(image_bounds_after.width() / image_bounds_before.width(), 1.15,
                                   places=2)
            self.assertEqual(dialog.preview_layers[0].size(), buffer_size)
            self.assertEqual(dialog.preview_label.pixmap().size(), buffer_size)
            self.assertEqual(dialog.trace_options(), options)
            self.assertEqual((dialog.width_spin.value(), dialog.height_spin.value(),
                              dialog.dpi_spin.value()), (width, height, dpi))
            self.assertEqual(_png_base64(dialog.image), source_bytes)
        finally:
            dialog.close()

    def test_middle_drag_pans_and_fit_resets_pan_and_zoom(self):
        dialog = ImportImageDialog(self.image(), "synthetic")
        try:
            dialog.show()
            self.app.processEvents()
            label = dialog.preview_label
            dialog._zoom_preview_at(QPointF(label.rect().center()), 2)
            self.assertEqual(dialog.preview_zoom, 2)
            point = QPoint(label.width() // 2, label.height() // 2)
            delta = QPoint(35, -22)
            buffer_point = self.to_buffer(dialog, QPointF(point))
            source_before = dialog.preview_to_source.map(buffer_point)
            QTest.mousePress(label, Qt.MouseButton.MiddleButton, pos=point)
            QTest.mouseMove(label, point + delta)
            QTest.mouseRelease(label, Qt.MouseButton.MiddleButton, pos=point + delta)
            source_after = dialog.preview_to_source.map(self.to_buffer(
                dialog, QPointF(point + delta)))
            self.assertAlmostEqual(source_before.x(), source_after.x(), places=5)
            self.assertAlmostEqual(source_before.y(), source_after.y(), places=5)

            painted = []
            label.paint_callback = lambda *_args: painted.append(True) or True
            space_delta = QPoint(-18, 27)
            source_before = dialog.preview_to_source.map(self.to_buffer(dialog, QPointF(point)))
            label.setFocus()
            QTest.keyPress(label, Qt.Key.Key_Space)
            QTest.mousePress(label, Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseMove(label, point + space_delta)
            QTest.mouseRelease(label, Qt.MouseButton.LeftButton, pos=point + space_delta)
            QTest.keyRelease(label, Qt.Key.Key_Space)
            source_after = dialog.preview_to_source.map(self.to_buffer(
                dialog, QPointF(point + space_delta)))
            self.assertAlmostEqual(source_before.x(), source_after.x(), places=5)
            self.assertAlmostEqual(source_before.y(), source_after.y(), places=5)
            self.assertEqual(painted, [])
            dialog._fit_preview()
            self.assertEqual(dialog.preview_zoom, 1)
            self.assertEqual(dialog.preview_pan, QPointF())
        finally:
            dialog.close()

    def test_zoomed_mask_brush_still_maps_to_source_pixels(self):
        image = self.image()
        options = raster_options(image, {"mask_mode": "background", "background": "#ffffff"})
        item = build_raster_items(image, "synthetic", options)[0]
        dialog = EditImageDialog(item)
        try:
            dialog.show()
            self.app.processEvents()
            dialog.brush_check.setChecked(True)
            dialog.brush_mode.setCurrentIndex(dialog.brush_mode.findData("remove"))
            dialog.brush_width.setValue(1.5)
            center = QPointF(dialog.preview_label.rect().center())
            dialog._zoom_preview_at(center, 2.5)
            preview_transform = dialog.preview_to_source.inverted()[0]
            target = preview_transform.map(QPointF(60, 45))
            pixmap = dialog.preview_label.pixmap()
            target += QPointF((dialog.preview_label.width() - pixmap.width()) / 2,
                              (dialog.preview_label.height() - pixmap.height()) / 2)
            self.assertTrue(dialog._paint_preview(target, None))
            dialog._finish_stroke()
            result = dialog.build_items()[0]
            self.assertFalse(working_mask(result, dialog.image)[45, 60])
            self.assertEqual(result["image_data"], item["image_data"])
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()

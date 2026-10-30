import base64
import gc
import os
import struct
import unittest
import zlib
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QScrollArea

from verona.editor_widgets import SafeDoubleSpinBox
from verona.i18n import tr
from verona import raster_import
from verona.raster_import import ImportImageDialog, _rgba_array, decode_embedded_image


def png_base64(image):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return base64.b64encode(bytes(data)).decode("ascii")


class RasterImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_decode_png_and_qoi_and_reject_invalid_input(self):
        source = QImage(2, 1, QImage.Format.Format_RGBA8888)
        source.setPixelColor(0, 0, QColor(12, 34, 56, 255))
        source.setPixelColor(1, 0, QColor(78, 90, 12, 0))
        decoded = decode_embedded_image(png_base64(source))
        self.assertEqual((decoded.width(), decoded.height()), (2, 1))
        self.assertEqual(decoded.pixelColor(1, 0).alpha(), 0)

        qoi = (b"qoif" + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
               + bytes((4, 0, 0xff, 1, 2, 3, 255)) + b"\0\0\0\0\0\0\0\1")
        self.assertEqual(decode_embedded_image(base64.b64encode(qoi).decode()).pixelColor(0, 0),
                         QColor(1, 2, 3, 255))
        with self.assertRaises(ValueError):
            decode_embedded_image("not base64!")

        def chunk(kind, data):
            payload = kind + data
            return (struct.pack(">I", len(data)) + payload
                    + struct.pack(">I", zlib.crc32(payload) & 0xffffffff))

        header = struct.pack(">IIBBBBB", 4001, 4000, 8, 6, 0, 0, 0)
        rows = b"\0" + b"\0" * (4001 * 4)
        oversized_png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                         + chunk(b"IDAT", zlib.compress(rows * 4000)) + chunk(b"IEND", b""))
        with self.assertRaises(ValueError):
            decode_embedded_image(base64.b64encode(oversized_png).decode())

    def test_rgba_view_remains_valid_after_source_image_is_released(self):
        image = QImage(1, 1, QImage.Format.Format_RGBA8888)
        image.setPixelColor(0, 0, QColor(15, 25, 35, 45))
        pixels = _rgba_array(image)
        del image
        gc.collect()
        self.assertEqual(pixels[0, 0].tolist(), [15, 25, 35, 45])

    def test_shared_decoder_does_not_share_caller_pixel_mutations(self):
        source = QImage(2, 2, QImage.Format.Format_RGBA8888)
        source.fill(QColor(17, 27, 37))
        encoded = png_base64(source)
        first = decode_embedded_image(encoded)
        first.setPixelColor(0, 0, QColor("red"))
        second = decode_embedded_image(encoded)
        self.assertEqual(second.pixelColor(0, 0), QColor(17, 27, 37))
        self.assertEqual(first.pixelColor(0, 0), QColor("red"))

    def test_image_cache_evicts_by_bytes_and_recency_without_dropping_images(self):
        images = []
        for color in ("red", "green", "blue"):
            source = QImage(2, 2, QImage.Format.Format_RGBA8888)
            source.fill(QColor(color))
            images.append(png_base64(source))
        with patch.dict(raster_import._DECODED_IMAGES, clear=True), \
                patch.object(raster_import, "_MAX_CACHE_BYTES", 32), \
                patch.object(raster_import, "_decode_image", wraps=raster_import._decode_image) as decode:
            first = decode_embedded_image(images[0])
            decode_embedded_image(images[1])
            decode_embedded_image(images[0])  # Red is now the most recent.
            decode_embedded_image(images[2])
            self.assertEqual(decode.call_count, 3)
            self.assertEqual(list(raster_import._DECODED_IMAGES), [images[0], images[2]])
            self.assertLessEqual(sum(i.sizeInBytes() for i in raster_import._DECODED_IMAGES.values()), 32)
            self.assertEqual(first.pixelColor(0, 0), QColor("red"))
            self.assertEqual(decode_embedded_image(images[1]).pixelColor(0, 0), QColor("green"))
            self.assertEqual(decode.call_count, 4)

    def test_image_larger_than_cache_is_still_available_and_entry_count_is_bounded(self):
        source = QImage(2, 2, QImage.Format.Format_RGBA8888)
        source.fill(QColor("red"))
        encoded = png_base64(source)
        with patch.dict(raster_import._DECODED_IMAGES, clear=True), \
                patch.object(raster_import, "_MAX_CACHE_BYTES", 15):
            self.assertEqual(decode_embedded_image(encoded).pixelColor(0, 0), QColor("red"))
            self.assertFalse(raster_import._DECODED_IMAGES)
        with patch.dict(raster_import._DECODED_IMAGES, clear=True):
            for number in range(33):
                source.fill(QColor(number, 0, 0))
                decode_embedded_image(png_base64(source))
            self.assertEqual(len(raster_import._DECODED_IMAGES), 32)

    def test_failed_decode_does_not_evict_valid_images_or_cache_a_failure(self):
        source = QImage(2, 2, QImage.Format.Format_RGBA8888)
        source.fill(QColor("red"))
        encoded = png_base64(source)
        with patch.dict(raster_import._DECODED_IMAGES, clear=True):
            decode_embedded_image(encoded)
            with self.assertRaises(ValueError):
                decode_embedded_image("invalid!")
            self.assertEqual(list(raster_import._DECODED_IMAGES), [encoded])
            self.assertEqual(decode_embedded_image(encoded).pixelColor(0, 0), QColor("red"))

    @staticmethod
    def _transparent_donut():
        image = QImage(12, 12, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.fillRect(1, 1, 10, 10, QColor("red"))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(4, 4, 4, 4, Qt.GlobalColor.transparent)
        painter.end()
        return image

    def test_color_summary_only_appears_when_selected_colors_are_relevant(self):
        dialog = ImportImageDialog(self._transparent_donut(), "donut")
        self.assertTrue(dialog.color_summary.isHidden())
        dialog.foreground_colors = ["#ff0000"]
        dialog.mask_mode_combo.setCurrentIndex(dialog.mask_mode_combo.findData("colors"))
        self.assertFalse(dialog.color_summary.isHidden())
        self.assertEqual(dialog.color_summary.text(), "#ff0000")
        dialog.mask_mode_combo.setCurrentIndex(dialog.mask_mode_combo.findData("background"))
        self.assertTrue(dialog.color_summary.isHidden())
        dialog.close()

    def test_import_dialog_retraces_the_mask_only_for_mask_options(self):
        dialog = ImportImageDialog(self._transparent_donut(), "donut")
        reference = raster_import.build_raster_items(
            dialog.image, "donut", {**dialog.trace_options(), "width_mm": 30, "height_mm": 30,
                                    "offset_mm": .5, "bleed_mm": 1, "mode": "print_cut"})
        with patch("verona.raster_import.trace_raw", wraps=raster_import.trace_raw) as raw:
            dialog.width_spin.setValue(30)
            dialog.offset_spin.setValue(.5)
            dialog.bleed_spin.setValue(1)
            dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("cut_only"))
            dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("print_cut"))
            dialog.clip_check.setChecked(False)
            dialog.clip_check.setChecked(True)
            self.assertEqual(raw.call_count, 0)
            dialog.tolerance_spin.setValue(40)
            self.assertEqual(raw.call_count, 1)
            dialog.tolerance_spin.setValue(20)
        self.assertEqual(dialog.build_items(), reference)

    def test_alpha_trace_preserves_hole_and_can_fill_it(self):
        dialog = ImportImageDialog(self._transparent_donut(), "donut")
        with_hole = dialog.build_items()[0]
        self.assertGreaterEqual(len(with_hole["contours"]), 2)
        dialog.holes_check.setChecked(False)
        without_hole = dialog.build_items()[0]
        self.assertEqual(len(without_hole["contours"]), 1)
        self.assertTrue(without_hole["image_clip"])
        dialog.close()

    def test_background_color_tolerance_traces_object_not_image_bounds(self):
        image = QImage(30, 20, QImage.Format.Format_RGBA8888)
        image.fill(QColor("white"))
        painter = QPainter(image)
        painter.fillRect(10, 5, 10, 10, QColor("black"))
        painter.end()
        dialog = ImportImageDialog(image, "black square")
        dialog.mask_mode_combo.setCurrentIndex(dialog.mask_mode_combo.findData("background"))
        dialog.tolerance_spin.setValue(24)
        item = dialog.build_items()[0]
        xs = [x for contour in item["contours"] for x, _ in contour]
        self.assertLess(max(xs) - min(xs), dialog.width_spin.value() * 0.6)
        self.assertGreater(max(xs) - min(xs), 0)
        dialog.close()

    def test_modes_clip_ratio_and_preview_opacity_output(self):
        dialog = ImportImageDialog(self._transparent_donut(), "art")
        dialog.width_spin.setValue(60)
        self.assertAlmostEqual(dialog.height_spin.value(), 60)
        print_item = dialog.build_items()[0]
        self.assertTrue(print_item["cut"])
        self.assertIn("image_data", print_item)
        self.assertEqual(print_item["image_clip"], print_item["contours"])
        dialog.opacity_slider.setValue(10)
        self.assertEqual(dialog.build_items()[0], print_item)

        dialog.keep_ratio_check.setChecked(False)
        dialog.height_spin.setValue(30)
        self.assertAlmostEqual(dialog.width_spin.value(), 60)
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("cut_only"))
        cut_item = dialog.build_items()[0]
        self.assertTrue(cut_item["cut"])
        self.assertFalse(cut_item["print"])
        self.assertIn("image_data", cut_item)
        self.assertIn("image_transform", cut_item)

        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("background"))
        background_item = dialog.build_items()[0]
        self.assertFalse(background_item["cut"])
        self.assertIn("image_data", background_item)
        self.assertEqual(len(background_item["contours"]), 1)
        dialog.close()

    def test_alpha_detection_defaults_to_alpha_only_when_transparency_exists(self):
        transparent = ImportImageDialog(self._transparent_donut(), "transparent")
        self.assertEqual(transparent.mask_mode_combo.currentData(), "alpha")
        opaque = QImage(8, 6, QImage.Format.Format_RGBA8888)
        opaque.fill(QColor("white"))
        opaque_dialog = ImportImageDialog(opaque, "opaque")
        self.assertEqual(opaque_dialog.mask_mode_combo.currentData(), "background")
        transparent.close()
        opaque_dialog.close()

    def test_offset_is_measured_in_mm_and_clip_stays_on_unoffset_mask(self):
        image = QImage(30, 20, QImage.Format.Format_RGBA8888)
        image.fill(QColor("white"))
        painter = QPainter(image)
        painter.fillRect(10, 5, 10, 10, QColor("black"))
        painter.end()
        dialog = ImportImageDialog(image, "offset")
        dialog.mask_mode_combo.setCurrentIndex(dialog.mask_mode_combo.findData("background"))
        dialog.keep_ratio_check.setChecked(False)
        dialog.width_spin.setValue(60)
        dialog.height_spin.setValue(20)
        dialog.offset_spin.setValue(1)
        item = dialog.build_items()[0]

        def bounds(contours):
            points = [point for contour in contours for point in contour]
            return (min(x for x, _ in points), min(y for _, y in points),
                    max(x for x, _ in points), max(y for _, y in points))

        cut = bounds(item["contours"])
        clip = bounds(item["image_clip"])
        self.assertAlmostEqual(clip[0] - cut[0], 1, delta=0.08)
        self.assertAlmostEqual(clip[1] - cut[1], 1, delta=0.08)
        self.assertAlmostEqual(cut[2] - clip[2], 1, delta=0.08)
        self.assertAlmostEqual(cut[3] - clip[3], 1, delta=0.08)
        dialog.close()

    def test_filling_holes_keeps_an_island_inside_a_hole(self):
        image = self._transparent_donut()
        painter = QPainter(image)
        painter.fillRect(5, 5, 2, 2, QColor("blue"))
        painter.end()
        dialog = ImportImageDialog(image, "island")
        contours_with_hole = dialog.build_items()[0]["contours"]
        self.assertEqual(len(contours_with_hole), 3)
        dialog.holes_check.setChecked(False)
        filled_contours = dialog.build_items()[0]["contours"]
        self.assertEqual(len(filled_contours), 1)
        dialog.close()

    def test_smoothing_reduces_jagged_contour_points(self):
        image = QImage(48, 32, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        path = QPainterPath()
        path.moveTo(3, 4)
        for x in range(3, 44):
            path.lineTo(x, 10 if x % 2 else 12)
        path.lineTo(44, 28)
        path.lineTo(3, 28)
        path.closeSubpath()
        painter = QPainter(image)
        painter.fillPath(path, QColor("red"))
        painter.end()
        dialog = ImportImageDialog(image, "jagged")
        dialog.smoothing_spin.setValue(0)
        raw_count = sum(map(len, dialog.build_items()[0]["contours"]))
        dialog.smoothing_spin.setValue(100)
        smooth_count = sum(map(len, dialog.build_items()[0]["contours"]))
        self.assertLess(smooth_count, raw_count)
        dialog.close()

    def test_bleed_adds_print_pixels_without_changing_cut_path(self):
        dialog = ImportImageDialog(self._transparent_donut(), "bleed")
        dialog.bleed_color = QColor("magenta")
        base = dialog.build_items()[0]
        dialog.bleed_spin.setValue(2)
        with_bleed = dialog.build_items()[0]
        self.assertEqual(with_bleed["contours"], base["contours"])
        self.assertNotEqual(with_bleed["image_transform"], base["image_transform"])
        self.assertNotEqual(with_bleed["image_clip"], base["image_clip"])
        raster = decode_embedded_image(with_bleed["image_data"])
        self.assertGreater(raster.width(), dialog.image.width())
        margin_x = (raster.width() - dialog.image.width()) // 2
        bleed_pixels = [raster.pixelColor(x, y) for y in range(raster.height())
                        for x in list(range(margin_x)) +
                        list(range(margin_x + dialog.image.width(), raster.width()))]
        self.assertIn(QColor("magenta"), bleed_pixels)
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("cut_only"))
        self.assertFalse(dialog.bleed_spin.isEnabled())
        dialog.close()

    def test_aspect_lock_extreme_ratio_and_compact_scrollable_dialog(self):
        image = QImage(2, 1200, QImage.Format.Format_RGBA8888)
        image.fill(QColor("red"))
        dialog = ImportImageDialog(image, "portrait")
        dialog.show()
        self.app.processEvents()
        self.assertAlmostEqual(dialog.width_spin.value() / dialog.height_spin.value(), 2 / 1200,
                               delta=1e-5)
        self.assertIsInstance(dialog.offset_spin, SafeDoubleSpinBox)
        self.assertIsInstance(dialog.width_spin, SafeDoubleSpinBox)
        self.assertIsInstance(dialog.height_spin, SafeDoubleSpinBox)
        self.assertIsInstance(dialog.settings_scroll, QScrollArea)
        buttons = dialog.findChild(QDialogButtonBox)
        self.assertLessEqual(dialog.height(), 800)
        self.assertTrue(buttons.isVisible())
        self.assertEqual(buttons.button(QDialogButtonBox.StandardButton.Ok).text(), tr("匯入"))
        dialog.width_spin.setValue(5)
        self.assertAlmostEqual(dialog.height_spin.value(), 300)
        self.assertAlmostEqual(dialog.width_spin.value(), .5)
        item = dialog.build_items()[0]
        self.assertAlmostEqual(item["image_transform"][2], item["image_transform"][5])
        dialog.close()


if __name__ == "__main__":
    unittest.main()

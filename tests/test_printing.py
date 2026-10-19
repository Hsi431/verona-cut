import base64
import os
import re
import tempfile
import unittest
import zlib
import weakref
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMarginsF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication
from verona.printing import draw_registration_marks, printer_profiles, registration_geometry, write_print_pdf


class PrintPdfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_printer_profiles_report_driver_margins_without_submitting_output(self):
        info, printer = MagicMock(), MagicMock()
        info.printerName.return_value = "test queue"
        info.isDefault.return_value = True
        printer.setPageSize.return_value = True
        printer.pageLayout.return_value.minimumMargins.return_value = QMarginsF(5, 6, 7, 8)
        with patch("PySide6.QtPrintSupport.QPrinterInfo.availablePrinters", return_value=[info]), \
                patch("PySide6.QtPrintSupport.QPrinter", return_value=printer):
            profiles = printer_profiles(210, 297)
        self.assertEqual(profiles, [{"name": "test queue", "default": True,
                                   "paper_accepted": True, "margins_mm": [5, 6, 7, 8]}])
        printer.newPage.assert_not_called()

    def test_printed_items_respect_caller_clip_opacity_and_painter_state(self):
        from verona.artwork import draw_printed_item
        from tests.test_editor_actions import image_item, rectangle
        image = QImage(500, 500, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        painter.scale(10, 10)
        painter.setClipRect(QRectF(20, 20, 15, 10))
        painter.setOpacity(.5)
        painter.setPen(QPen(QColor("blue"), .4))
        painter.setBrush(QColor("yellow"))
        state = painter.transform(), painter.clipPath(), painter.opacity(), painter.pen(), painter.brush()
        try:
            for item in (image_item(bleed={"width_mm": 1, "inner_mm": .5, "color": "#ff0000"}),
                         rectangle(x=22, y=22, width=2, height=2,
                             print_style={"fill": "#0000ff", "stroke": "#00ff00", "width_mm": .3})):
                draw_printed_item(painter, item)
                self.assertEqual((painter.transform(), painter.clipPath(), painter.opacity(),
                                  painter.pen(), painter.brush()), state)
        finally:
            painter.end()
        self.assertEqual(image.pixelColor(360, 250), QColor("white"))
        self.assertEqual(image.pixelColor(195, 250), QColor("white"))
        self.assertIn(image.pixelColor(300, 250).red(), (127, 128))

    def test_compact_marks_keep_bottom_left_long(self):
        polygons = registration_geometry([20, 30, 80, 90], stroke=1, short_length=4)
        self.assertEqual(len(polygons), 4)
        bottom_left, top_left, top_right, bottom_right = polygons
        self.assertEqual(bottom_left[0], (19.5, 90.5))
        self.assertEqual(bottom_left[1], (32.5, 90.5))
        self.assertEqual(bottom_left[4], (20.5, 77.5))
        self.assertEqual(top_left[1], (24, 29.5))
        self.assertEqual(top_right[1], (76, 29.5))
        self.assertEqual(bottom_right[1], (76, 90.5))
        self.assertEqual(polygons, registration_geometry([20, 30, 80, 90], stroke=1))

    def test_pdf_does_not_pin_previously_drawn_decoded_images(self):
        from verona import artwork, printing
        from verona.raster_import import _png_base64
        from tests.test_editor_actions import image_item
        references = []
        source = QImage(2, 2, QImage.Format.Format_RGBA8888)
        source.fill(QColor("blue"))
        items = [image_item(), image_item(image_data=_png_base64(source))]

        def decode(_encoded):
            self.assertTrue(all(reference() is None for reference in references))
            image = QImage(2, 2, QImage.Format.Format_RGBA8888)
            image.fill(QColor("red"))
            references.append(weakref.ref(image))
            return image

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(artwork, "decode_embedded_image", side_effect=decode), \
                patch.object(printing, "decode_embedded_image", side_effect=decode, create=True):
            write_print_pdf(Path(folder) / "images.pdf", items,
                {"print_cut": {"enabled": True, "paper_width": 100,
                    "paper_height": 100, "bounds": [6, 6, 94, 94]}})
        self.assertEqual(len(references), 2)
        self.assertTrue(all(reference() is None for reference in references))

    def test_rendered_corners_are_solid_right_angles_without_triangle(self):
        image = QImage(2100, 2100, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        painter.scale(20, 20)
        draw_registration_marks(painter, [20, 30, 80, 90],
                                {"mark_short_length": 12.5, "mark_stroke": 1.5})
        painter.end()

        def black(x, y):
            return image.pixelColor(round(x * 20), round(y * 20)).lightness() < 128

        for x, y, dx, dy in ((20, 30, 1, 1), (80, 30, -1, 1),
                              (20, 90, 1, -1), (80, 90, -1, -1)):
            with self.subTest(corner=(x, y)):
                # The old separate FlatCap strokes left this outer quadrant white.
                self.assertTrue(black(x - dx * .5, y - dy * .5))
                self.assertTrue(black(x + dx * 10, y))
                self.assertTrue(black(x, y + dy * 10))
                self.assertFalse(black(x + dx, y + dy))
                self.assertFalse(black(x - dx, y - dy))
        self.assertFalse(black(25, 96))

    def test_compact_rendering_shortens_only_three_corners(self):
        image = QImage(2100, 2100, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        painter.scale(20, 20)
        draw_registration_marks(painter, [20, 30, 80, 90],
                                {"mark_stroke": 1, "mark_short_length": 8})
        painter.end()
        self.assertEqual(image.pixelColor(30 * 20, 90 * 20).lightness(), 0)
        self.assertEqual(image.pixelColor(30 * 20, 30 * 20).lightness(), 255)
        self.assertEqual(image.pixelColor(round(19.4 * 20), 85 * 20).lightness(), 255)

    def test_invalid_profile_does_not_create_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bad.pdf"
            for field, value in (("mark_stroke", 0), ("mark_stroke", True),
                                 ("mark_stroke", float("nan")), ("mark_short_length", 13),
                                 ("mark_short_length", 3.9)):
                registration = {"enabled": True, "paper_width": 210, "paper_height": 297,
                                "bounds": [20, 20, 80, 80], field: value}
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    write_print_pdf(output, [], {"print_cut": registration}, calibration=True)
                self.assertFalse(output.exists())

    def test_pdf_is_one_physical_a4_page_with_native_image(self):
        qoi = b"qoif" + (1).to_bytes(4, "big") * 2 + bytes((4, 0, 0xff, 10, 20, 30, 255)) + b"\0" * 7 + b"\1"
        item = {"image_data": base64.b64encode(qoi).decode(), "image_transform": [40, 40, 10, 0, 0, 10]}
        metadata = {"print_cut": {"enabled": True, "paper_width": 210, "paper_height": 297,
                                  "bounds": [20, 20, 80, 80]}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "print.pdf"
            write_print_pdf(output, [item], metadata)
            data = output.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        box = re.search(rb"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]", data)
        self.assertIsNotNone(box)
        self.assertAlmostEqual(float(box[1]), 210 * 72 / 25.4, delta=1)
        self.assertAlmostEqual(float(box[2]), 297 * 72 / 25.4, delta=1)
        self.assertIn(b"/Subtype /Image", data)
        self.assertRegex(data, rb"/Count\s+1\b")

    def test_invalid_paper_and_missing_artwork_do_not_create_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bad.pdf"
            for registration in ({}, {"enabled": True, "paper_width": 210, "paper_height": 297,
                                       "bounds": [20, 20, 80, 80]},
                                  {"enabled": True, "paper_width": float("nan"), "paper_height": 297,
                                   "bounds": [20, 20, 80, 80]}):
                with self.subTest(registration=registration), self.assertRaises(ValueError):
                    write_print_pdf(output, [], {"print_cut": registration})
                self.assertFalse(output.exists())

    def test_cut_adjustment_does_not_shift_printed_art_or_marks(self):
        items = [{"cut": True, "contours": [[(30, 30), (50, 30), (50, 40), (30, 30)]]}]
        registration = {"enabled": True, "paper_width": 210, "paper_height": 297,
                        "bounds": [20, 20, 80, 80]}
        with tempfile.TemporaryDirectory() as directory:
            before, after = Path(directory) / "before.pdf", Path(directory) / "after.pdf"
            write_print_pdf(before, items, {"print_cut": registration}, calibration=True)
            write_print_pdf(after, items, {"print_cut": {**registration, "offset": [-.3, .2]}},
                            calibration=True)
            streams = []
            for path in (before, after):
                streams.append([zlib.decompress(data) for header, data in
                                re.findall(rb"<<([^<>]*)>>\s*stream\n(.*?)\nendstream",
                                           path.read_bytes(), re.S) if b"/FlateDecode" in header])
            self.assertTrue(streams[0])
            self.assertEqual(streams[0], streams[1])


if __name__ == "__main__":
    unittest.main()

"""Ruler labels remain readable inside the fixed canvas margin at every zoom."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, QRectF, QSettings
from PySide6.QtGui import QImage, QPainter, QRegion
from PySide6.QtWidgets import QApplication

from verona.ui import VeronaWindow


class CanvasRulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.directory.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.window = VeronaWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()

    def ruler_bounds(self, zoom):
        labels = []

        class RecordingPainter(QPainter):
            def drawText(self, point, text):
                transform = self.transform()
                bounds = QRectF(self.fontMetrics().boundingRect(text)).translated(point)
                labels.append((transform.mapRect(bounds), transform.isRotating()))
                super().drawText(point, text)

        canvas = self.window.canvas
        canvas.set_zoom(zoom)
        canvas.resize(canvas.minimumSize())
        # Render a bounded region instead of allocating a 19268² image at 6400%.
        image = QImage(600, 600, QImage.Format.Format_ARGB32_Premultiplied)
        with patch("verona.ui.QPainter", RecordingPainter):
            canvas.render(image, QPoint(), QRegion(QRect(0, 0, 600, 600)))
        self.assertTrue(labels)
        return labels

    def test_labels_fit_the_margin_and_do_not_overlap_at_supported_zooms(self):
        for zoom in (.1, .5, 1.8, 5.49, 10, 64):
            with self.subTest(zoom=zoom):
                labels = self.ruler_bounds(zoom)
                canvas = self.window.canvas
                for bounds, _ in labels:
                    self.assertGreaterEqual(bounds.left(), 1)
                    self.assertGreaterEqual(bounds.top(), 1)
                    self.assertLessEqual(bounds.right(), canvas.width()-1)
                    self.assertLessEqual(bounds.bottom(), canvas.height()-1)
                for rotated in (False, True):
                    axis = [bounds for bounds, vertical in labels if vertical == rotated]
                    axis.sort(key=lambda bounds: bounds.top() if rotated else bounds.left())
                    for before, after in zip(axis, axis[1:]):
                        self.assertLess(before.bottom() if rotated else before.right(),
                                        after.top() if rotated else after.left())

    def test_label_text_keeps_its_screen_size_when_zooming_in(self):
        heights = []
        for zoom in (1.8, 5.49, 64):
            labels = self.ruler_bounds(zoom)
            heights.append(next(bounds.height() for bounds, rotated in labels if not rotated))
        self.assertGreater(heights[0], 8)
        self.assertAlmostEqual(heights[0], heights[1])
        self.assertAlmostEqual(heights[1], heights[2])


if __name__ == "__main__":
    unittest.main()

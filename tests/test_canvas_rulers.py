"""Canvas rulers stay fixed while their ticks track the page."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRectF, QSettings, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from verona.ui import PAGE_MARGIN, RULER_SIZE, VeronaWindow


class CanvasRulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "VERONA_HOST": "", "XDG_CONFIG_HOME": self.temp.name,
            "XDG_STATE_HOME": self.temp.name, "XDG_CACHE_HOME": self.temp.name,
        })
        self.environment.start()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          self.temp.name)
        self.window = VeronaWindow()

    def tearDown(self):
        self.window._confirm_unsaved_changes = lambda *_args: "discard"
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def process_layout(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not self.window._fit_timer.isActive():
                break
            time.sleep(.005)
        self.app.processEvents()

    def render_ruler(self, ruler):
        image = QImage(ruler.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        ruler.render(image)
        return image

    def ruler_label_bounds(self, ruler):
        labels = []

        class RecordingPainter(QPainter):
            def drawText(self, point, text):
                bounds = QRectF(self.fontMetrics().boundingRect(text)).translated(point)
                labels.append(self.transform().mapRect(bounds))
                super().drawText(point, text)

        with patch("verona.ui.QPainter", RecordingPainter):
            self.render_ruler(ruler)
        return labels

    def test_separate_rulers_keep_labels_inside_and_nonoverlapping(self):
        self.window.show()
        self.process_layout()
        scroll = self.window.canvas_scroll
        canvas = self.window.canvas
        for zoom in (.1, .5, 1.8, 5.49, 10, 64):
            with self.subTest(zoom=zoom):
                canvas.set_zoom(zoom)
                scroll._update_rulers()
                groups = ((self.ruler_label_bounds(scroll.horizontal_ruler),
                           scroll.horizontal_ruler, False),
                          (self.ruler_label_bounds(scroll.vertical_ruler),
                           scroll.vertical_ruler, True))
                for labels, ruler, vertical_axis in groups:
                    self.assertTrue(labels)
                    for bounds in labels:
                        self.assertGreaterEqual(bounds.left(), 0)
                        self.assertGreaterEqual(bounds.top(), 0)
                        self.assertLessEqual(bounds.right(), ruler.width())
                        self.assertLessEqual(bounds.bottom(), ruler.height())
                    labels.sort(key=lambda bounds: bounds.top() if vertical_axis else bounds.left())
                    for before, after in zip(labels, labels[1:]):
                        self.assertLessEqual(before.bottom() if vertical_axis else before.right(),
                                             after.top() if vertical_axis else after.left())

    def test_ruler_label_size_stays_constant_when_zoom_changes(self):
        heights = []

        class RecordingPainter(QPainter):
            def drawText(self, point, text):
                heights.append(self.fontMetrics().boundingRect(text).height())
                super().drawText(point, text)

        self.window.show()
        self.process_layout()
        ruler = self.window.canvas_scroll.horizontal_ruler
        for zoom in (1.8, 5.49, 64):
            self.window.canvas.set_zoom(zoom)
            with patch("verona.ui.QPainter", RecordingPainter):
                self.render_ruler(ruler)
        self.assertGreater(heights[0], 8)
        self.assertEqual(len(set(heights)), 1)

    def test_rulers_stay_anchored_when_canvas_scrolls_and_window_resizes(self):
        self.window.resize(1000, 700)
        self.window.show()
        self.process_layout()
        scroll = self.window.canvas_scroll
        h_ruler, v_ruler = scroll.horizontal_ruler, scroll.vertical_ruler

        def assert_anchored():
            self.assertEqual(h_ruler.geometry().top(), 0)
            self.assertEqual(v_ruler.geometry().left(), 0)
            self.assertEqual(h_ruler.geometry().left(), scroll.viewport().geometry().left())
            self.assertEqual(v_ruler.geometry().top(), scroll.viewport().geometry().top())
            self.assertEqual(h_ruler.mapTo(scroll, QPoint(0, 0)).y(), 0)
            self.assertEqual(v_ruler.mapTo(scroll, QPoint(0, 0)).x(), 0)

        assert_anchored()
        self.window.change_zoom(2.0)
        self.process_layout()
        scroll.horizontalScrollBar().setValue(scroll.horizontalScrollBar().maximum())
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        self.process_layout()
        self.assertNotEqual(self.window.canvas.pos(), QPoint(0, 0))
        assert_anchored()
        self.window.resize(1200, 760)
        self.process_layout()
        assert_anchored()

    def test_zero_and_twenty_millimeter_ticks_align_with_page(self):
        self.window.resize(1100, 760)
        self.window.show()
        self.process_layout()
        canvas = self.window.canvas
        scroll = self.window.canvas_scroll
        canvas.set_zoom(1.8)
        scroll.horizontalScrollBar().setValue(0)
        scroll.verticalScrollBar().setValue(0)
        self.process_layout()

        def line_positions(ruler):
            positions = []

            class RecordingPainter(QPainter):
                def drawLine(self, *args):
                    if len(args) == 2 and all(isinstance(arg, QPointF) for arg in args):
                        a, b = args
                        positions.append((a.x(), a.y(), b.x(), b.y()))
                    super().drawLine(*args)

            with patch("verona.ui.QPainter", RecordingPainter):
                self.render_ruler(ruler)
            return positions

        h_lines = line_positions(scroll.horizontal_ruler)
        v_lines = line_positions(scroll.vertical_ruler)
        origin = canvas.canvas_origin()
        expected_h = [scroll.horizontal_ruler.mapFromGlobal(
                          canvas.mapToGlobal(QPoint(round(origin.x() + mm * canvas.zoom),
                                                   round(origin.y())))).x()
                      for mm in (0, 20)]
        expected_v = [scroll.vertical_ruler.mapFromGlobal(
                          canvas.mapToGlobal(QPoint(round(origin.x()),
                                                   round(origin.y() + mm * canvas.zoom)))).y()
                      for mm in (0, 20)]
        observed_h = [round(x1) for x1, y1, x2, y2 in h_lines
                      if abs(x1 - x2) < .1 and abs(abs(y1 - y2) - 5) < .1]
        observed_v = [round(y1) for x1, y1, x2, y2 in v_lines
                      if abs(y1 - y2) < .1 and abs(abs(x1 - x2) - 5) < .1]
        for expected in expected_h:
            self.assertTrue(any(abs(expected - actual) <= 1 for actual in observed_h))
        for expected in expected_v:
            self.assertTrue(any(abs(expected - actual) <= 1 for actual in observed_v))

    def test_scrolled_four_x_rulers_align_at_one_hundred_and_one_twenty_mm(self):
        self.window.resize(1100, 760)
        self.window.show()
        self.process_layout()
        canvas = self.window.canvas
        scroll = self.window.canvas_scroll
        self.window.change_zoom(4.0 / canvas.zoom)
        self.process_layout()
        scroll.horizontalScrollBar().setValue(203)
        scroll.verticalScrollBar().setValue(197)
        self.process_layout()
        self.assertEqual(scroll.horizontalScrollBar().value(), 203)
        self.assertEqual(scroll.verticalScrollBar().value(), 197)

        def tick_positions(ruler, horizontal):
            positions = []

            class RecordingPainter(QPainter):
                def drawLine(self, *args):
                    if len(args) == 2 and all(isinstance(arg, QPointF) for arg in args):
                        a, b = args
                        if horizontal and abs(a.x() - b.x()) < .1 and abs(abs(a.y() - b.y()) - 5) < .1:
                            positions.append(round(a.x()))
                        if not horizontal and abs(a.y() - b.y()) < .1 and abs(abs(a.x() - b.x()) - 5) < .1:
                            positions.append(round(a.y()))
                    super().drawLine(*args)

            with patch("verona.ui.QPainter", RecordingPainter):
                self.render_ruler(ruler)
            return positions

        h_positions = tick_positions(scroll.horizontal_ruler, True)
        v_positions = tick_positions(scroll.vertical_ruler, False)
        origin = canvas.canvas_origin()
        for mm in (100, 120):
            expected_h = scroll.horizontal_ruler.mapFromGlobal(canvas.mapToGlobal(
                QPoint(round(origin.x() + mm * canvas.zoom), round(origin.y())))).x()
            expected_v = scroll.vertical_ruler.mapFromGlobal(canvas.mapToGlobal(
                QPoint(round(origin.x()), round(origin.y() + mm * canvas.zoom)))).y()
            self.assertTrue(any(abs(expected_h - actual) <= 1 for actual in h_positions),
                            f"{mm} mm horizontal tick is misaligned")
            self.assertTrue(any(abs(expected_v - actual) <= 1 for actual in v_positions),
                            f"{mm} mm vertical tick is misaligned")

    def test_page_height_fit_still_fits_page_inside_viewport(self):
        self.window.resize(1100, 760)
        self.window.show()
        self.process_layout()
        self.window.metadata["print_cut"] = {"paper_height": 297.0}
        self.window.canvas.set_zoom(1.1)
        self.window.fit_page_height()
        self.process_layout()
        viewport_height = self.window.canvas_scroll.maximumViewportSize().height()
        rendered_height = 297.0 * self.window.canvas.zoom + 2 * PAGE_MARGIN
        self.assertTrue(self.window._fit_page_height)
        self.assertTrue(self.window.fit_height_action.isChecked())
        self.assertLessEqual(rendered_height, viewport_height + 2)


if __name__ == "__main__":
    unittest.main()

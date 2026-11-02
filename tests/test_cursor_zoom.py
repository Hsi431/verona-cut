"""Cursor-anchored canvas zoom should account for scroll-area layout changes."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication

from verona.i18n import get_language, set_language
from verona.ui import VeronaWindow


class CursorZoomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_dir = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          cls.settings_dir.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def setUp(self):
        self.original_language = get_language()
        QSettings("Verona", "Workspace").clear()
        self.window = VeronaWindow()
        self.window.resize(1000, 700)
        self.window.items = [{"name": "synthetic", "cut": True,
                              "contours": [[(10., 10.), (80., 10.), (80., 80.),
                                            (10., 10.)]]}]
        self.window.refresh_all()
        self.window.show()
        self.app.processEvents()
        self.window._fit_timer.stop()
        self.window._fit_page_height = False

    def tearDown(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        set_language(self.original_language)

    def set_view(self, zoom, horizontal=0, vertical=0):
        self.window._fit_timer.stop()
        self.window._fit_page_height = False
        self.window.canvas.set_zoom(zoom)
        self.app.processEvents()
        scroll = self.window.canvas_scroll
        scroll.horizontalScrollBar().setValue(horizontal)
        scroll.verticalScrollBar().setValue(vertical)
        self.app.processEvents()
        self.assertFalse(self.window._fit_page_height)

    def wheel(self, viewport_point, delta, *, process=True):
        canvas = self.window.canvas
        viewport = self.window.canvas_scroll.viewport()
        position = QPointF(canvas.mapFrom(viewport, viewport_point))
        global_position = QPointF(canvas.mapToGlobal(position.toPoint()))
        event = QWheelEvent(position, global_position, QPoint(0, 0), QPoint(0, delta),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        self.app.sendEvent(canvas, event)
        if process:
            self.app.processEvents()
        self.assertTrue(event.isAccepted())

    def assert_anchor_follows_zoom(self, viewport_point, delta):
        canvas = self.window.canvas
        scroll = self.window.canvas_scroll
        viewport = scroll.viewport()
        point = QPointF(viewport_point)
        local_before = QPointF(canvas.mapFrom(viewport, viewport_point))
        world = canvas._world_point(local_before)
        old_zoom = canvas.zoom

        self.wheel(viewport_point, delta)

        self.assertAlmostEqual(canvas.zoom, old_zoom * 1.25 ** (delta / 120), places=6)
        local_after = canvas.canvas_origin() + world * canvas.zoom
        actual_screen = QPointF(canvas.mapTo(viewport, local_after.toPoint()))

        bars = (scroll.horizontalScrollBar(), scroll.verticalScrollBar())
        local = (local_after.x(), local_after.y())
        target = (point.x(), point.y())
        actual = (actual_screen.x(), actual_screen.y())
        for axis, (bar, local_value, target_value, actual_value) in enumerate(
                zip(bars, local, target, actual)):
            # Reconstruct the widget's unscrolled viewport offset after Qt has
            # settled the resize. This separates true cursor drift from a
            # scrollbar endpoint that cannot compensate the requested motion.
            top = canvas.mapTo(viewport, QPoint(0, 0))
            unscrolled = (top.x() + bars[0].value(), top.y() + bars[1].value())[axis]
            requested = round(unscrolled + local_value - target_value)
            clamped = min(bar.maximum(), max(bar.minimum(), requested))
            expected_screen = unscrolled + local_value - clamped
            drift = actual_value - expected_screen
            self.assertLessEqual(abs(drift), 1.5,
                                 f"axis={axis}, cursor drift from feasible anchor="
                                 f"{drift:.2f}px (scrollbar range "
                                 f"{bar.minimum()}..{bar.maximum()})")
            self.assertLessEqual(abs(actual_value - target_value), 1.5,
                                 f"axis={axis}, cursor moved "
                                 f"{actual_value-target_value:.2f}px")

    def test_zoom_in_tracks_left_right_and_upper_lower_cursor_positions(self):
        for point in (QPoint(110, 100), QPoint(420, 100),
                      QPoint(110, 360), QPoint(420, 360)):
            with self.subTest(point=point.toTuple()):
                self.set_view(3.0, horizontal=180, vertical=160)
                self.assert_anchor_follows_zoom(point, 120)

    def test_zoom_out_tracks_cursor_when_both_scrollbars_have_room(self):
        for point in (QPoint(110, 100), QPoint(420, 100),
                      QPoint(110, 360), QPoint(420, 360)):
            with self.subTest(point=point.toTuple()):
                self.set_view(3.0, horizontal=180, vertical=160)
                self.assert_anchor_follows_zoom(point, -120)

    def test_reversing_zoom_at_the_same_cursor_does_not_accumulate_drift(self):
        self.set_view(3.0, horizontal=160, vertical=140)
        point = QPoint(300, 250)
        self.assert_anchor_follows_zoom(point, 120)
        self.assert_anchor_follows_zoom(point, -120)
        self.assertAlmostEqual(self.window.canvas.zoom, 3.0, places=6)

    def test_zoom_out_across_page_fit_keeps_anchor_with_view_offset(self):
        self.set_view(1.5)
        point = QPoint(300, 250)
        self.assert_anchor_follows_zoom(point, -120)

    def test_repeated_zoom_across_page_fit_keeps_cursor_anchor(self):
        self.set_view(1.5)
        point = QPoint(300, 250)
        for delta in (-120, 120, -120, -120, 120):
            with self.subTest(delta=delta, zoom=self.window.canvas.zoom):
                self.assert_anchor_follows_zoom(point, delta)

    def test_unprocessed_wheel_burst_keeps_latest_cursor_anchor(self):
        self.set_view(3.0, horizontal=160, vertical=140)
        viewport = self.window.canvas_scroll.viewport()
        point = QPoint(300, 250)
        world = self.window.canvas._world_point(
            QPointF(self.window.canvas.mapFrom(viewport, point)))

        self.wheel(point, 120, process=False)
        self.wheel(point, 120, process=False)
        self.assertTrue(self.window.canvas._zoom_timer.isActive())
        self.app.processEvents()

        canvas = self.window.canvas
        local = canvas.canvas_origin() + world * canvas.zoom
        screen = canvas.mapTo(viewport, local.toPoint())
        self.assertAlmostEqual(canvas.zoom, 3.0 * 1.25**2, places=6)
        self.assertLessEqual(abs(screen.x() - point.x()), 1.5)
        self.assertLessEqual(abs(screen.y() - point.y()), 1.5)
        self.assertIsNone(canvas._zoom_anchor)

    def test_zoom_limits_clamp_scale_and_finish_anchor(self):
        for zoom, delta in ((64.0, 120), (0.1, -120)):
            with self.subTest(zoom=zoom):
                self.set_view(zoom, horizontal=100, vertical=100)
                self.wheel(QPoint(300, 250), delta)
                self.assertEqual(self.window.canvas.zoom, zoom)
                self.assertIsNone(self.window.canvas._zoom_anchor)
                self.assertFalse(self.window.canvas._zoom_timer.isActive())

    def test_fit_or_tab_switch_clears_pending_zoom_anchor(self):
        self.set_view(2.0, horizontal=100, vertical=100)
        self.wheel(QPoint(300, 250), 120, process=False)
        self.assertIsNotNone(self.window.canvas._zoom_anchor)
        self.window.fit_page_height()
        self.assertIsNone(self.window.canvas._zoom_anchor)
        self.assertFalse(self.window.canvas._zoom_timer.isActive())
        self.app.processEvents()

        self.set_view(2.0, horizontal=100, vertical=100)
        self.wheel(QPoint(300, 250), 120, process=False)
        self.assertIsNotNone(self.window.canvas._zoom_anchor)
        self.window.new_design()
        self.assertIsNone(self.window.canvas._zoom_anchor)
        self.assertFalse(self.window.canvas._zoom_timer.isActive())
        self.app.processEvents()

    def test_manual_view_offset_survives_tab_and_language_rebuild_but_fit_resets(self):
        self.set_view(2.0, horizontal=100, vertical=100)
        original_offset = QPointF(17.0, -9.0)
        self.window.canvas._view_offset = QPointF(original_offset)
        self.window.new_design()
        self.assertTrue(self.window.canvas._view_offset.isNull())
        self.window.tab_bar.setCurrentIndex(0)
        self.assertEqual(self.window.canvas._view_offset, original_offset)

        language = "en" if get_language() != "en" else "zh_TW"
        self.window._change_language(language)
        self.assertEqual(self.window.canvas._view_offset, original_offset)

        self.window.fit_page_height()
        self.assertTrue(self.window.canvas._view_offset.isNull())


if __name__ == "__main__":
    unittest.main()

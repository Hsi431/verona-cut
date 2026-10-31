"""Workspace sizing, page fit and per-document view state."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from verona.ui import PAGE_HEIGHT_MM, PAGE_MARGIN, PAGE_WIDTH_MM, VeronaWindow


class WorkspaceLayoutTests(unittest.TestCase):
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
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "VERONA_HOST": "", "XDG_CONFIG_HOME": self.temp.name,
            "XDG_STATE_HOME": self.temp.name, "XDG_CACHE_HOME": self.temp.name,
        })
        self.environment.start()
        QSettings("Verona", "Workspace").clear()
        QSettings("Verona", "Workspace").sync()
        self.window = VeronaWindow()

    def tearDown(self):
        self.window._confirm_unsaved_changes = lambda *_args: "discard"
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def wait_for_layout(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not self.window._fit_timer.isActive():
                break
            time.sleep(.005)
        self.app.processEvents()

    def write_document(self, *, paper_height=297.0):
        document = {
            "format": "verona", "version": 1, "units": "mm",
            "items": [{"name": "reference", "contours": [[
                [20.0, 30.0], [60.0, 30.0], [60.0, 50.0], [20.0, 50.0], [20.0, 30.0]
            ]], "cut": True}],
            "metadata": {"print_cut": {"enabled": False, "paper_width": 210.0,
                                         "paper_height": paper_height}},
        }
        path = Path(self.temp.name) / "workspace.jdesign"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path, document

    def test_canvas_fills_viewport_and_page_stays_centered_at_common_sizes(self):
        for width, height in ((2048, 1114), (1000, 700)):
            with self.subTest(size=(width, height)):
                self.window.resize(width, height)
                self.window.show()
                self.wait_for_layout()
                viewport = self.window.canvas_scroll.viewport()
                canvas = self.window.canvas
                self.assertTrue(self.window.canvas_scroll.widgetResizable())
                self.assertGreater(viewport.width(), 0)
                self.assertGreater(viewport.height(), 0)
                self.assertGreaterEqual(canvas.width(), viewport.width())
                self.assertGreaterEqual(canvas.height(), viewport.height())
                page_height = PAGE_HEIGHT_MM * canvas.zoom + 2 * PAGE_MARGIN
                self.assertAlmostEqual(page_height, viewport.height(), delta=2)
                self.assertGreaterEqual(canvas.minimumWidth(),
                                        PAGE_WIDTH_MM * canvas.zoom + 2 * PAGE_MARGIN - 1)
                self.assertGreaterEqual(canvas.minimumHeight(),
                                        PAGE_HEIGHT_MM * canvas.zoom + 2 * PAGE_MARGIN - 1)
                if canvas.width() >= PAGE_WIDTH_MM * canvas.zoom + 2 * PAGE_MARGIN:
                    self.assertAlmostEqual(canvas.canvas_origin().x(),
                                           (canvas.width() - PAGE_WIDTH_MM * canvas.zoom) / 2,
                                           delta=1)
                self.assertFalse(self.window.is_dirty())

    def test_open_file_fits_paper_height_without_changing_design_units_or_data(self):
        path, source = self.write_document(paper_height=297.0)
        self.window.resize(1000, 700)
        self.window.show()
        with patch("verona.ui.QFileDialog.getOpenFileName", return_value=(str(path), "Design")):
            self.window.open_design()
        self.wait_for_layout()

        self.assertTrue(self.window._fit_page_height)
        self.assertTrue(self.window.fit_height_action.isChecked())
        self.assertAlmostEqual(self.window.metadata["print_cut"]["paper_height"], 297.0)
        self.assertEqual(self.window.items[0]["name"], source["items"][0]["name"])
        self.assertAlmostEqual(self.window.items[0]["contours"][0][0][0], 20.0)
        self.assertAlmostEqual(self.window.items[0]["contours"][0][0][1], 30.0)
        self.assertEqual(self.window._cut_settings()["connection"], "wifi")
        viewport = self.window.canvas_scroll.viewport()
        self.assertLessEqual(297.0 * self.window.canvas.zoom + 2 * PAGE_MARGIN,
                             viewport.height() + 1)
        self.assertFalse(self.window.is_dirty())

    def test_manual_zoom_survives_resize_and_ctrl_zero_restores_page_fit(self):
        path, _source = self.write_document()
        self.window.resize(1000, 700)
        self.window.show()
        self.window.open_path(str(path))
        self.wait_for_layout()
        self.window.change_zoom(0.8)
        manual_zoom = self.window.canvas.zoom
        self.assertFalse(self.window._fit_page_height)
        self.assertFalse(self.window.fit_height_action.isChecked())

        self.window.resize(2048, 1114)
        self.wait_for_layout()
        self.assertAlmostEqual(self.window.canvas.zoom, manual_zoom, places=7)
        self.assertFalse(self.window.is_dirty())

        self.window.activateWindow()
        self.window.setFocus()
        QTest.keyClick(self.window, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
        self.wait_for_layout()
        self.assertTrue(self.window._fit_page_height)
        self.assertTrue(self.window.fit_height_action.isChecked())
        self.assertNotAlmostEqual(self.window.canvas.zoom, manual_zoom, places=5)
        self.assertLessEqual(297.0 * self.window.canvas.zoom + 2 * PAGE_MARGIN,
                             self.window.canvas_scroll.viewport().height() + 1)
        self.assertFalse(self.window.is_dirty())

    def test_page_and_manual_view_state_are_restored_per_tab(self):
        path, _source = self.write_document()
        self.window.resize(1000, 700)
        self.window.show()
        self.window.open_path(str(path))
        self.wait_for_layout()
        self.window.change_zoom(0.75)
        manual_zoom = self.window.canvas.zoom
        self.assertFalse(self.window._fit_page_height)

        self.window.new_design()
        self.wait_for_layout()
        self.assertEqual(self.window.tab_bar.currentIndex(), 1)
        self.assertTrue(self.window._fit_page_height)
        fit_zoom = self.window.canvas.zoom
        self.window.tab_bar.setCurrentIndex(0)
        self.assertFalse(self.window._fit_page_height)
        self.assertFalse(self.window.fit_height_action.isChecked())
        self.assertAlmostEqual(self.window.canvas.zoom, manual_zoom, places=7)
        self.window.tab_bar.setCurrentIndex(1)
        self.assertTrue(self.window._fit_page_height)
        self.assertTrue(self.window.fit_height_action.isChecked())
        self.assertAlmostEqual(self.window.canvas.zoom, fit_zoom, places=7)
        self.assertFalse(self.window.is_dirty())

    def test_canvas_pixel_and_world_coordinates_round_trip(self):
        self.window.resize(1000, 700)
        self.window.show()
        self.wait_for_layout()
        self.window.canvas.set_zoom(1.37)
        self.app.processEvents()
        world = QPointF(23.75, 178.125)
        origin = self.window.canvas.canvas_origin()
        pixel = QPointF(origin.x() + world.x() * self.window.canvas.zoom,
                        origin.y() + world.y() * self.window.canvas.zoom)
        result = self.window.canvas._world_point(pixel)
        self.assertAlmostEqual(result.x(), world.x(), places=7)
        self.assertAlmostEqual(result.y(), world.y(), places=7)
        self.assertEqual(self.window._cut_settings()["custom"], False)
        self.assertFalse(self.window.is_dirty())


if __name__ == "__main__":
    unittest.main()

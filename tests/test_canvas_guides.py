"""Mouse-driven smart alignment and ruler guide behavior."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QSettings, QSize, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QApplication

from verona import printing
from verona.commands import encode_document
from verona.design_validation import validate_document
from verona.i18n import set_language, tr
from verona.preview_dialogs import PlotPreviewCanvas
from verona.ui import VeronaWindow
from tests.test_editor_actions import rectangle


NO_MODIFIERS = Qt.KeyboardModifier.NoModifier


class CanvasGuideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        Path("/tmp/verona-snap-guides-test").mkdir(parents=True, exist_ok=True)
        cls.settings_dir = tempfile.TemporaryDirectory(
            prefix="settings-", dir="/tmp/verona-snap-guides-test")
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          cls.settings_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.settings_dir.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="case-", dir="/tmp/verona-snap-guides-test")
        QSettings("Verona", "Workspace").clear()
        set_language("en")
        self.windows = [VeronaWindow(host="")]

    def tearDown(self):
        for window in self.windows:
            with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                window.close()
            window.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    @property
    def window(self):
        return self.windows[0]

    def local(self, window, world):
        canvas = window.canvas
        origin = canvas.canvas_origin()
        return QPointF(origin.x() + world.x() * canvas.zoom,
                       origin.y() + world.y() * canvas.zoom)

    def send_mouse(self, window, target, event_type, local, global_pos=None, *,
                   button=Qt.MouseButton.LeftButton,
                   buttons=Qt.MouseButton.LeftButton, modifiers=NO_MODIFIERS):
        if global_pos is None:
            global_pos = QPointF(target.mapToGlobal(local.toPoint()))
        event = QMouseEvent(event_type, local, global_pos, button, buttons, modifiers)
        QApplication.sendEvent(target, event)

    def drag_canvas(self, window, start_world, end_world, *, modifiers=NO_MODIFIERS):
        start, end = self.local(window, start_world), self.local(window, end_world)
        self.send_mouse(window, window.canvas, QMouseEvent.Type.MouseButtonPress, start)
        self.send_mouse(window, window.canvas, QMouseEvent.Type.MouseMove, end,
                        button=Qt.MouseButton.NoButton, modifiers=modifiers)
        self.send_mouse(window, window.canvas, QMouseEvent.Type.MouseButtonRelease, end,
                        buttons=Qt.MouseButton.NoButton, modifiers=modifiers)

    @staticmethod
    def bounds(window, index):
        return window.canvas.item_path(window.items[index]).boundingRect()

    def test_object_drag_snaps_at_six_screen_pixels_across_zooms_and_ctrl_bypasses(self):
        for zoom in (0.5, 1.0, 2.0):
            with self.subTest(zoom=zoom, distance_px=4):
                self.window.canvas.set_zoom(zoom)
                self.window.items = [rectangle("moving", 20, 20, 80, 60),
                                    rectangle("target", 150, 20, 30, 60)]
                self.window.select(0)
                delta = 50 - 4 / zoom
                self.drag_canvas(self.window, QPointF(60, 50),
                                 QPointF(60 + delta, 50))
                self.assertAlmostEqual(self.bounds(self.window, 0).right(), 150)
                self.window.undo()
            with self.subTest(zoom=zoom, distance_px=10):
                self.window.items = [rectangle("moving", 20, 20, 80, 60),
                                    rectangle("target", 150, 20, 30, 60)]
                self.window.select(0)
                delta = 50 - 10 / zoom
                self.drag_canvas(self.window, QPointF(60, 50),
                                 QPointF(60 + delta, 50))
                self.assertAlmostEqual(self.bounds(self.window, 0).right(), 150 - 10 / zoom)

        self.window.canvas.set_zoom(1)
        self.window.items = [rectangle("moving", 20, 20, 80, 60),
                            rectangle("target", 150, 20, 30, 60)]
        self.window.select(0)
        self.drag_canvas(self.window, QPointF(60, 50), QPointF(106, 50),
                         modifiers=Qt.KeyboardModifier.ControlModifier)
        self.assertAlmostEqual(self.bounds(self.window, 0).right(), 146)

    def test_resize_handle_snaps_the_dragged_edge(self):
        self.window.canvas.set_zoom(1)
        self.window.items = [rectangle("resized", 20, 20, 20, 20),
                            rectangle("target", 60, 20, 20, 20)]
        self.window.lock_ratio.setChecked(False)
        self.window.select(0)
        handle = self.window.canvas.selection_handles()["se"]
        self.drag_canvas(self.window, handle, QPointF(56, 40))
        self.assertAlmostEqual(self.bounds(self.window, 0).right(), 60)

    def test_object_drag_snaps_to_reference_guide_even_when_smart_guides_are_off(self):
        self.window.canvas.set_zoom(1)
        self.window.metadata = {"guides": [{"orientation": "vertical", "position": 70.0}]}
        self.window.settings.setValue("guides/smart", False)
        self.window.items = [rectangle("moving", 20, 20, 30, 30)]
        self.window.select(0)
        self.drag_canvas(self.window, QPointF(35, 35), QPointF(51, 35))
        self.assertAlmostEqual(self.bounds(self.window, 0).right(), 70)

    def test_ruler_guide_creation_save_reopen_delete_and_undo(self):
        self.window.show()
        self.app.processEvents()
        canvas = self.window.canvas
        ruler = self.window.canvas_scroll.horizontal_ruler
        world_start = QPointF(70, 0)
        canvas_start = self.local(self.window, world_start)
        global_start = QPointF(canvas.mapToGlobal(canvas_start.toPoint()))
        ruler_start = QPointF(ruler.mapFromGlobal(global_start.toPoint()))
        ruler_start.setY(ruler.height() / 2)
        self.send_mouse(self.window, ruler, QMouseEvent.Type.MouseButtonPress, ruler_start)

        target_world = QPointF(70, 80)
        target_local = self.local(self.window, target_world)
        target_global = QPointF(canvas.mapToGlobal(target_local.toPoint()))
        self.send_mouse(self.window, ruler, QMouseEvent.Type.MouseMove,
                        QPointF(ruler.mapFromGlobal(target_global.toPoint())), target_global,
                        button=Qt.MouseButton.NoButton)
        preview = canvas.grab().toImage()
        position = canvas.guide_drag["position"]
        zoom = canvas.zoom
        origin = canvas.canvas_origin()
        label_x = round(origin.x() + min(position + 4 / zoom, 300 - 40 / zoom) * zoom)
        label_y = round(origin.y() + 14)
        label_pixels = 0
        for y in range(max(0, label_y - 12), min(preview.height(), label_y + 3)):
            for x in range(max(0, label_x - 2), min(preview.width(), label_x + 60)):
                color = preview.pixelColor(x, y)
                if (color.red() > 100 and color.blue() > 100
                        and color.red() - color.green() > 50
                        and color.blue() - color.green() > 30):
                    label_pixels += 1
        self.assertGreater(label_pixels, 0, "live ruler guide position is not painted")
        self.send_mouse(self.window, ruler, QMouseEvent.Type.MouseButtonRelease,
                        QPointF(ruler.mapFromGlobal(target_global.toPoint())), target_global,
                        buttons=Qt.MouseButton.NoButton)
        saved_position = self.window.metadata["guides"][0]["position"]
        self.assertAlmostEqual(saved_position, 70, delta=0.5)
        self.assertEqual(len(self.window.undo_stack), 1)

        self.drag_canvas(self.window, QPointF(70, 80), QPointF(80, 80))
        moved_position = self.window.metadata["guides"][0]["position"]
        self.assertAlmostEqual(moved_position, 80, delta=0.5)
        self.assertEqual(len(self.window.undo_stack), 2)

        design_path = Path(self.tmp.name) / "guides.jdesign"
        self.window._write_document(str(design_path))
        reopened = VeronaWindow(host="")
        self.windows.append(reopened)
        items, settings, metadata = reopened._read_document(str(design_path))
        reopened._install_document(str(design_path), items, settings, metadata)
        self.assertEqual(reopened.metadata["guides"], self.window.metadata["guides"])

        reopened.show()
        self.app.processEvents()
        canvas = reopened.canvas
        world = QPointF(80, 80)
        line_point = self.local(reopened, world)
        global_point = QPointF(canvas.mapToGlobal(line_point.toPoint()))
        self.send_mouse(reopened, canvas, QMouseEvent.Type.MouseButtonPress,
                        line_point, global_point)
        ruler = reopened.canvas_scroll.horizontal_ruler
        ruler_global = QPointF(global_point.x(), ruler.mapToGlobal(
            QPoint(0, ruler.height() // 2)).y())
        self.send_mouse(reopened, canvas, QMouseEvent.Type.MouseMove,
                        QPointF(canvas.mapFromGlobal(ruler_global.toPoint())), ruler_global,
                        button=Qt.MouseButton.NoButton)
        self.send_mouse(reopened, canvas, QMouseEvent.Type.MouseButtonRelease,
                        QPointF(canvas.mapFromGlobal(ruler_global.toPoint())), ruler_global,
                        buttons=Qt.MouseButton.NoButton)
        self.assertNotIn("guides", reopened.metadata)
        reopened.undo()
        self.assertEqual(reopened.metadata["guides"], self.window.metadata["guides"])

    def test_left_ruler_creates_and_deletes_a_horizontal_guide(self):
        self.window.show()
        self.app.processEvents()
        canvas = self.window.canvas
        ruler = self.window.canvas_scroll.vertical_ruler
        source_local = self.local(self.window, QPointF(0, 95))
        source_global = QPointF(canvas.mapToGlobal(source_local.toPoint()))
        ruler_start = QPointF(ruler.mapFromGlobal(source_global.toPoint()))
        ruler_start.setX(ruler.width() / 2)
        self.send_mouse(self.window, ruler, QMouseEvent.Type.MouseButtonPress,
                        ruler_start, source_global)

        target_local = self.local(self.window, QPointF(100, 95))
        target_global = QPointF(canvas.mapToGlobal(target_local.toPoint()))
        self.send_mouse(self.window, ruler, QMouseEvent.Type.MouseMove,
                        QPointF(ruler.mapFromGlobal(target_global.toPoint())), target_global,
                        button=Qt.MouseButton.NoButton)
        self.send_mouse(self.window, ruler, QMouseEvent.Type.MouseButtonRelease,
                        QPointF(ruler.mapFromGlobal(target_global.toPoint())), target_global,
                        buttons=Qt.MouseButton.NoButton)
        self.assertEqual(self.window.metadata["guides"][0]["orientation"], "horizontal")
        self.assertAlmostEqual(self.window.metadata["guides"][0]["position"], 95, delta=0.5)

        line_local = self.local(self.window, QPointF(100, 95))
        line_global = QPointF(canvas.mapToGlobal(line_local.toPoint()))
        self.send_mouse(self.window, canvas, QMouseEvent.Type.MouseButtonPress,
                        line_local, line_global)
        ruler_global = QPointF(ruler.mapToGlobal(
            QPoint(ruler.width() // 2, line_global.toPoint().y())))
        self.send_mouse(self.window, canvas, QMouseEvent.Type.MouseMove,
                        QPointF(canvas.mapFromGlobal(ruler_global.toPoint())), ruler_global,
                        button=Qt.MouseButton.NoButton)
        self.send_mouse(self.window, canvas, QMouseEvent.Type.MouseButtonRelease,
                        QPointF(canvas.mapFromGlobal(ruler_global.toPoint())), ruler_global,
                        buttons=Qt.MouseButton.NoButton)
        self.assertNotIn("guides", self.window.metadata)

    def test_guide_metadata_validation_and_print_and_plt_output_are_unchanged(self):
        items = [rectangle("printed", 20, 20, 30, 30, print=True,
                           print_style={"fill": "#000000", "stroke": None, "width_mm": .2})]
        registration = {"enabled": False, "paper_width": 210., "paper_height": 297.,
                        "bounds": [6., 6., 204., 291.]}
        base = {"print_cut": registration}
        with_guides = {**base, "guides": [
            {"orientation": "vertical", "position": 70.},
            {"orientation": "horizontal", "position": 90.}]}
        settings = {"custom": True, "speed": 5, "force": 10, "tool": 0,
                    "passes": 1, "connection": "wifi", "host": "", "port": 8080}
        plt_without = encode_document(items, base, settings)
        plt_with = encode_document(items, with_guides, settings)
        self.assertEqual(plt_with, plt_without)
        self.assertEqual(PlotPreviewCanvas(plt_without).grab().toImage(),
                         PlotPreviewCanvas(plt_with).grab().toImage())

        for bad_guides in (
                [{"orientation": "diagonal", "position": 20}],
                [{"orientation": "vertical", "position": float("nan")}],
                [{"orientation": "vertical", "position": True}],
                [{"orientation": "vertical", "position": 301}],
        ):
            data = {"format": "verona", "version": 1, "units": "mm",
                    "items": items, "metadata": {"guides": bad_guides}}
            with self.subTest(guides=bad_guides), self.assertRaises(ValueError):
                validate_document(data)

        without_pdf, with_pdf = Path(self.tmp.name) / "plain.pdf", Path(self.tmp.name) / "guides.pdf"
        printing.write_print_pdf(without_pdf, items, base)
        printing.write_print_pdf(with_pdf, items, with_guides)
        rendered = []
        for filename in (without_pdf, with_pdf):
            pdf = QPdfDocument()
            self.assertEqual(pdf.load(str(filename)), QPdfDocument.Error.None_)
            rendered.append(pdf.render(0, QSize(210, 297)))
            pdf.close()
        self.assertEqual(rendered[0], rendered[1])

    def test_appearance_actions_persist_and_explain_ctrl_snap_override(self):
        appearance_action = next(action for action in self.window.menuBar().actions()
                                 if action.text() == tr("外觀"))
        menu = appearance_action.menu()
        actions = {action.text(): action for action in menu.actions() if not action.isSeparator()}
        show = actions[tr("顯示／隱藏參考線")]
        smart = actions[tr("智慧參考線")]
        lock = actions[tr("鎖定參考線")]
        self.assertEqual(show.shortcut().toString(), "Ctrl+;")
        self.assertEqual(smart.shortcut().toString(), "Ctrl+U")
        self.assertEqual(lock.shortcut().toString(), "Ctrl+Alt+;")
        self.assertIn("Ctrl", smart.toolTip())
        show.trigger()
        smart.trigger()
        lock.trigger()
        settings = QSettings("Verona", "Workspace")
        self.assertFalse(settings.value("guides/visible", True, type=bool))
        self.assertFalse(settings.value("guides/smart", True, type=bool))
        self.assertTrue(settings.value("guides/locked", False, type=bool))
        self.assertFalse(self.window.canvas.begin_ruler_guide("vertical", QPointF()))
        self.window.metadata = {"guides": [{"orientation": "horizontal", "position": 25.0}]}
        clear = actions[tr("清除參考線")]
        clear.trigger()
        self.assertNotIn("guides", self.window.metadata)
        self.window.undo()
        self.assertEqual(self.window.metadata["guides"],
                         [{"orientation": "horizontal", "position": 25.0}])


if __name__ == "__main__":
    unittest.main()

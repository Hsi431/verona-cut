"""Nearest source colors extend only into the bleed ring, retaining source/cuts."""
import copy
import math
import os
from pathlib import Path
import tempfile
import unittest
import weakref
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QSettings, QSize, QTemporaryDir, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QApplication

from verona.artwork import draw_bleed
from verona.edge_bleed import edge_bleed_image
from verona.editor_actions import build_edit
from verona.printing import write_print_pdf
from verona.raster_import import _png_base64
from verona.ui import BleedDialog, VeronaWindow, _transform_item
from tests.test_editor_actions import image_item, rectangle


def two_colors(left="red", right="blue"):
    source = QImage(20, 10, QImage.Format.Format_RGBA8888)
    source.fill(QColor(left))
    painter = QPainter(source)
    painter.fillRect(10, 0, 10, 10, QColor(right))
    painter.end()
    return image_item(image_data=_png_base64(source))


class EdgeBleedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = QTemporaryDir()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.path())
        cls.app = QApplication.instance() or QApplication([])

    def extend(self, item):
        return build_edit([item], {}, [0], "bleed", {"width_mm": 1, "color": "#ffffff", "mode": "edge"})[0][0]

    def test_source_colors_extend_without_painting_the_interior_or_outside_ring(self):
        item = two_colors()
        original = copy.deepcopy(item)
        changed = self.extend(item)
        canvas = QImage(700, 700, QImage.Format.Format_RGBA8888)
        canvas.fill(Qt.GlobalColor.white)
        painter = QPainter(canvas)
        painter.scale(10, 10)
        draw_bleed(painter, changed)
        painter.end()
        self.assertEqual(canvas.pixelColor(195, 255), QColor("red"))
        self.assertEqual(canvas.pixelColor(405, 255), QColor("blue"))
        self.assertEqual(canvas.pixelColor(180, 255), QColor("white"))
        self.assertEqual(canvas.pixelColor(250, 250), QColor("white"))
        for key in ("image_data", "image_clip", "image_transform", "contours"):
            self.assertEqual(changed[key], original[key])
        self.assertEqual(item, original)

    def test_world_rotation_and_working_layer_limit(self):
        item = two_colors()
        transform = QTransform().translate(80, 0).rotate(90)
        _transform_item(item, transform)
        changed = self.extend(item)
        canvas = QImage(900, 900, QImage.Format.Format_RGBA8888)
        canvas.fill(Qt.GlobalColor.white)
        painter = QPainter(canvas)
        painter.scale(10, 10)
        draw_bleed(painter, changed)
        painter.end()
        red = transform.map(QPointF(19.5, 25.5))
        blue = transform.map(QPointF(40.5, 25.5))
        self.assertEqual(canvas.pixelColor(round(red.x()*10), round(red.y()*10)), QColor("red"))
        self.assertEqual(canvas.pixelColor(round(blue.x()*10), round(blue.y()*10)), QColor("blue"))
        large = two_colors()
        _transform_item(large, QTransform.fromScale(20, 20))
        large["bleed"] = {"width_mm": 1, "mode": "edge", "color": "#ffffff"}
        layer, _ = edge_bleed_image(large)
        self.assertLessEqual(max(layer.width(), layer.height()), 1200)

    def test_hole_center_and_mask_are_not_filled(self):
        item = two_colors()
        hole = rectangle(x=27, y=23, width=6, height=4)["contours"]
        item["contours"] += hole
        item["image_clip"] += copy.deepcopy(hole)
        changed = self.extend(item)
        canvas = QImage(700, 700, QImage.Format.Format_RGBA8888)
        canvas.fill(Qt.GlobalColor.white)
        painter = QPainter(canvas)
        painter.scale(10, 10)
        draw_bleed(painter, changed)
        painter.end()
        self.assertEqual(canvas.pixelColor(300, 250), QColor("white"))

    def test_detailed_edge_preview_and_canvas_never_build_offset_boolean_geometry(self):
        from verona import artwork
        item = two_colors()
        contour = [(30 + 9 * math.cos(i * math.tau / 5000),
                    25 + 4 * math.sin(i * math.tau / 5000)) for i in range(5000)]
        contour.append(contour[0])
        item["contours"] = [contour]
        item["image_clip"] = copy.deepcopy(item["contours"])
        with patch.object(artwork, "_offset_path", side_effect=AssertionError("Edge display must not offset contours")):
            changed = self.extend(item)
            canvas = QImage(700, 700, QImage.Format.Format_RGBA8888)
            canvas.fill(Qt.GlobalColor.white)
            painter = QPainter(canvas)
            painter.scale(10, 10)
            draw_bleed(painter, changed)
            painter.end()
            window = VeronaWindow()
            window.items = [changed]
            window.refresh_all()
            window.canvas.grab()
            self.assertEqual(changed["contours"], item["contours"])
            self.assertEqual(changed["image_data"], item["image_data"])
            window.deleteLater()

    def test_empty_source_is_rejected_before_applying_or_loading(self):
        source = QImage(20, 10, QImage.Format.Format_RGBA8888)
        source.fill(Qt.GlobalColor.transparent)
        item = image_item(image_data=_png_base64(source))
        original = copy.deepcopy(item)
        with self.assertRaisesRegex(ValueError, "no visible source"):
            self.extend(item)
        item["bleed"] = {"width_mm": 1, "color": "#ffffff", "mode": "edge"}
        with self.assertRaisesRegex(ValueError, "no visible source"):
            VeronaWindow._validate_document({"format": "verona", "version": 1,
                                            "units": "mm", "items": [item]})
        item.pop("bleed")
        self.assertEqual(item, original)

    def test_canvas_pdf_save_and_cache_invalidation_keep_plt_identical(self):
        window, reopened = VeronaWindow(), VeronaWindow()
        window.items = [two_colors()]
        window.metadata = {"print_cut": {"enabled": True, "paper_width": 100,
            "paper_height": 100, "bounds": [6, 6, 94, 94]}}
        window.refresh_all()
        before = window._encoded_plt()
        window.items = [self.extend(window.items[0])]
        window.refresh_all()
        window.canvas.set_zoom(5)
        try:
            with patch("verona.edge_bleed.edge_bleed_image", wraps=edge_bleed_image) as extension:
                first = window.canvas.grab().toImage()
                self.assertEqual(first.pixelColor(round(34+19.5*5), round(34+25.5*5)), QColor("red"))
                window.canvas.grab()
                self.assertEqual(extension.call_count, 1)
                window.items[0]["image_data"] = two_colors("green", "yellow")["image_data"]
                changed = window.canvas.grab().toImage()
                self.assertEqual(extension.call_count, 2)
                self.assertEqual(changed.pixelColor(round(34+19.5*5), round(34+25.5*5)), QColor("green"))
                window.items[0]["bleed"]["width_mm"] = 2
                window.canvas.grab()
                self.assertEqual(extension.call_count, 3)
                # A rigid move reuses the layer; the result matches a fresh computation.
                _transform_item(window.items[0], QTransform.fromTranslate(10, 0))
                moved = window.canvas.grab().toImage()
                self.assertEqual(extension.call_count, 3)
                self.assertEqual(moved.pixelColor(round(34+28.5*5), round(34+25.5*5)), QColor("green"))
                # refresh_all keeps still-valid entries, so drop them to compare
                # against a fresh computation.
                window.refresh_all()
                window.canvas._path_cache.clear()
                with patch.dict("verona.edge_bleed._LAYERS", clear=True):
                    self.assertEqual(window.canvas.grab().toImage(), moved)
                self.assertEqual(extension.call_count, 4)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "edge.jdesign"
                window._write_document(str(path))
                self.assertTrue(reopened._load_document(str(path)))
                self.assertEqual(reopened.items, window.items)
                self.assertEqual(reopened._encoded_plt(), window._encoded_plt())
                # Translation changes PLT, but the bleed operation alone did not.
                _transform_item(window.items[0], QTransform.fromTranslate(-10, 0))
                self.assertEqual(window._encoded_plt(), before)
                pdf_path = Path(folder) / "edge.pdf"
                write_print_pdf(pdf_path, window.items, window.metadata)
                self.assertNotIn(b"/DCTDecode", pdf_path.read_bytes())
                pdf = QPdfDocument()
                pdf.load(str(pdf_path))
                image = pdf.render(0, QSize(1000, 1000))
                self.assertEqual(image.pixelColor(195, 255), QColor("green"))
                self.assertEqual(image.pixelColor(405, 255), QColor("yellow"))
                pdf.close()
            window.select(0)
            dialog = BleedDialog(window, [0])
            try:
                self.assertEqual(dialog.mode_combo.currentData(), "edge")
                self.assertFalse(dialog.color_button.isEnabled())
                dialog.inner_spin.setValue(.5)
                self.assertTrue(dialog.color_button.isEnabled())
            finally:
                dialog.close()
        finally:
            for editor in (window, reopened):
                with patch.object(editor, "_confirm_unsaved_changes", return_value="discard"):
                    editor.close()

    def test_gui_cut_edits_cannot_leave_edge_bleed_without_source_colors(self):
        source = QImage(20, 10, QImage.Format.Format_RGBA8888)
        source.fill(Qt.GlobalColor.transparent)
        painter = QPainter(source)
        painter.fillRect(0, 0, 10, 10, QColor("red"))
        painter.end()
        item = self.extend(image_item(image_data=_png_base64(source)))
        transparent = rectangle("cutter", x=32, y=22, width=6, height=6)
        moved = copy.deepcopy(item)
        moved["contours"] = copy.deepcopy(transparent["contours"])

        class Dialog:
            def __init__(self, *_args):
                self.item = moved

            def exec(self):
                from PySide6.QtWidgets import QDialog
                return QDialog.DialogCode.Accepted

            def build_items(self):
                return [moved]

            def deleteLater(self):
                pass

        window = VeronaWindow()
        try:
            for name, run in (("link", lambda: window.link_selected_cut_paths()),
                              ("nodes", lambda: window.edit_nodes()),
                              ("mask", lambda: window.edit_selected_image())):
                window.items = [copy.deepcopy(item), copy.deepcopy(transparent)]
                window.select_many([0, 1] if name == "link" else [0])
                original = copy.deepcopy(window.items)
                window.undo_stack.clear()
                with self.subTest(name=name), \
                        patch("verona.node_editor.NodeEditorDialog", Dialog), \
                        patch("verona.mask_edit.EditImageDialog", Dialog), \
                        patch("verona.ui.QMessageBox.warning") as warning:
                    run()
                    warning.assert_called_once()
                    self.assertIn("no visible source", warning.call_args.args[2])
                    self.assertEqual(window.items, original)
                    self.assertFalse(window.undo_stack)
        finally:
            with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                window.close()

    def test_canvas_edge_layers_are_bounded_by_bytes_and_released_on_refresh(self):
        from verona import ui
        window = VeronaWindow()
        items = [self.extend(two_colors()) for _ in range(3)]
        references = []

        def layer(_item):
            image = QImage(2, 2, QImage.Format.Format_RGBA8888)
            image.fill(QColor("red"))
            references.append(weakref.ref(image))
            return image, QTransform()

        try:
            with patch.object(ui, "_MAX_CANVAS_EDGE_BYTES", 32, create=True), \
                    patch("verona.edge_bleed.edge_bleed_image", side_effect=layer):
                for item in items:
                    window.canvas.item_edge_bleed(item)
                self.assertIsNone(references[0]())
                self.assertIsNotNone(references[1]())
                self.assertIsNotNone(references[2]())
                window.refresh_all()
                self.assertTrue(all(reference() is None for reference in references))
        finally:
            window.close()

    def test_many_small_edge_layers_stay_warm_beyond_global_entry_limit(self):
        window = VeronaWindow()
        items = [self.extend(two_colors()) for _ in range(20)]
        try:
            with patch("verona.edge_bleed.edge_bleed_image", wraps=edge_bleed_image) as compute:
                for _ in range(2):
                    for item in items:
                        window.canvas.item_edge_bleed(item)
                self.assertEqual(compute.call_count, len(items))
        finally:
            window.close()

    def test_drag_moves_reuse_the_edge_layer_but_other_changes_recompute(self):
        import numpy as np
        from verona import edge_bleed
        window = VeronaWindow()
        window.items = [self.extend(two_colors())]
        window.refresh_all()
        window.canvas.set_zoom(5)
        source = copy.deepcopy(window.items[0])

        def pixels():
            image = window.canvas.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            return np.frombuffer(image.constBits(), np.uint8).reshape(
                image.height(), image.width(), 4).astype(int)

        try:
            for transform, exact in ((QTransform.fromTranslate(3.3, -1.7), True),
                                     (QTransform().translate(30, 25).rotate(90).translate(-30, -25), True),
                                     (QTransform().translate(30, 25).rotate(33).scale(-1, 1).translate(-30, -25), False)):
                _transform_item(window.items[0], QTransform(), source)
                window.canvas._path_cache.clear()
                window.canvas.grab()
                with self.subTest(transform=transform), \
                        patch("verona.edge_bleed._edge_bleed_image", wraps=edge_bleed._edge_bleed_image) as compute:
                    # Like a canvas drag: transform the drag-start item in place.
                    _transform_item(window.items[0], transform, source)
                    moved = pixels()
                    self.assertEqual(compute.call_count, 0)
                    window.canvas._path_cache.clear()
                    with patch.dict(edge_bleed._LAYERS, clear=True):
                        difference = np.abs(moved - pixels()).max(axis=-1)
                    self.assertEqual(compute.call_count, 1)
                    # Translation and right angles are exact; other angles only resample colors.
                    self.assertLess((difference > 0).mean() if exact else (difference > 40).mean(),
                                    1e-9 if exact else 1e-3)
            with patch("verona.edge_bleed._edge_bleed_image", wraps=edge_bleed._edge_bleed_image) as compute:
                _transform_item(window.items[0], QTransform().scale(1.5, 1.5), source)
                window.canvas.grab()
                self.assertEqual(compute.call_count, 1)
                # A new cut with the same image placement is not a move.
                window.items[0]["contours"] = rectangle(x=30, y=30, width=8, height=8)["contours"]
                window.canvas.grab()
                self.assertEqual(compute.call_count, 2)
        finally:
            with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                window.close()

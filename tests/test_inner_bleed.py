"""Printed inner edge color is reversible and never changes cutting geometry."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QSettings, QSize, QTemporaryDir
from PySide6.QtGui import QColor, QTransform
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from verona.artwork import inner_bleed_path, item_bounds, validate_bleed
from verona.editor_actions import build_edit
from verona.printing import write_print_pdf
from verona.mask_edit import EditImageDialog
from verona.ui import BleedDialog, VeronaWindow, PackingDialog, _transform_item
from tests.test_editor_actions import image_item, rectangle


class InnerBleedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = QTemporaryDir()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.path())
        cls.app = QApplication.instance() or QApplication([])

    def test_closed_own_edit_dialogs_release_children_and_preview_failure_preserves_work(self):
        window = VeronaWindow()
        window.items = [image_item()]
        window.refresh_all()
        window.select(0)
        original = window._document_state()
        try:
            for cls, method in ((EditImageDialog, window.edit_selected_image),
                                (BleedDialog, window.edit_selected_bleed),
                                (PackingDialog, window.pack_selected)):
                with patch.object(cls, "exec", lambda dialog: QDialog.DialogCode.Rejected):
                    method()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertFalse(window.findChildren(cls))
            dialog = BleedDialog(window, [0])
            with patch("verona.editor_actions.render_preview", side_effect=OSError("test failure")), \
                    patch.object(QMessageBox, "warning") as warning:
                dialog.preview()
            warning.assert_called_once()
            self.assertEqual(window._document_state(), original)
            dialog.deleteLater()
        finally:
            with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                window.close()

    def test_inner_band_stays_inside_contour_and_leaves_hole_and_center_clear(self):
        item = image_item(bleed={"width_mm": 0, "inner_mm": .8, "color": "#ff0000"})
        item["contours"] += rectangle(x=27, y=23, width=6, height=4)["contours"]
        path = inner_bleed_path(item)
        self.assertTrue(path.contains(QPointF(20.4, 25)))
        self.assertFalse(path.contains(QPointF(19.9, 25)))
        self.assertFalse(path.contains(QPointF(23, 25)))
        self.assertFalse(path.contains(QPointF(30, 25)))

    def test_optional_inner_schema_and_explicit_group_image_indices_preserve_sources(self):
        items = [image_item(group_path=["a"]), rectangle(x=70, group_path=["a"])]
        original = copy.deepcopy(items)
        changed, selected, _ = build_edit(items, {}, [0], "bleed",
            {"width_mm": 0, "inner_mm": .8, "color": "#FF0000"})
        self.assertEqual(selected, [0])
        self.assertEqual(changed[1], original[1])
        for key in ("contours", "image_data", "image_clip", "image_transform"):
            self.assertEqual(changed[0][key], original[0][key])
        self.assertEqual(item_bounds(changed[0], printed=True), item_bounds(original[0], printed=True))
        removed, _, _ = build_edit(changed, {}, [0], "bleed", {"width_mm": 0, "color": "#ff0000"})
        self.assertNotIn("bleed", removed[0])
        self.assertEqual(items, original)
        self.assertEqual(validate_bleed({"width_mm": 1, "color": "#FF0000"}),
                         {"width_mm": 1., "color": "#ff0000"})
        for inner in (True, -1, 1.1, float("nan")):
            with self.subTest(inner=inner), self.assertRaises(ValueError):
                validate_bleed({"width_mm": 1, "color": "#ff0000", "inner_mm": inner})

    def test_gui_preview_canvas_pdf_roundtrip_undo_and_cache_preserve_plt(self):
        window, reopened = VeronaWindow(), VeronaWindow()
        window.items = [image_item()]
        window.metadata = {"print_cut": {"enabled": True, "paper_width": 100,
            "paper_height": 100, "bounds": [6, 6, 94, 94]}}
        window.refresh_all()
        window.select(0)
        before = window._encoded_plt()
        original = copy.deepcopy(window.items)
        try:
            def accept(dialog):
                dialog.width_spin.setValue(1)
                dialog.inner_spin.setValue(.8)
                dialog.color = QColor("red")
                dialog.preview()
                self.assertFalse(dialog.preview_label.pixmap().isNull())
                return QDialog.DialogCode.Accepted

            with patch.object(BleedDialog, "exec", accept):
                window.edit_selected_bleed()
            self.assertEqual(len(window.undo_stack), 1)
            self.assertEqual(window._encoded_plt(), before)
            window.select_many([])
            window.canvas.set_zoom(5)
            canvas = window.canvas.grab().toImage()
            self.assertEqual(canvas.pixelColor(round(34+20.4*5), round(34+25.5*5)), QColor("red"))
            self.assertEqual(canvas.pixelColor(round(34+22*5), round(34+25.5*5)), QColor("black"))
            with tempfile.TemporaryDirectory() as folder:
                pdf_path = Path(folder) / "inner.pdf"
                write_print_pdf(pdf_path, window.items, window.metadata)
                pdf = QPdfDocument()
                pdf.load(str(pdf_path))
                image = pdf.render(0, QSize(1000, 1000))
                self.assertEqual(image.pixelColor(204, 255), QColor("red"))
                self.assertEqual(image.pixelColor(220, 255), QColor("black"))
                self.assertEqual(image.pixelColor(195, 255), QColor("red"))
                pdf.close()
                path = Path(folder) / "inner.jdesign"
                window._write_document(str(path))
                self.assertTrue(reopened._load_document(str(path)))
                self.assertEqual(reopened.items, window.items)
                self.assertEqual(reopened._encoded_plt(), before)
            window.items[0]["bleed"]["inner_mm"] = .2
            narrow = window.canvas.grab().toImage()
            self.assertEqual(narrow.pixelColor(round(34+20.4*5), round(34+25.5*5)), QColor("black"))
            window.items[0]["bleed"]["inner_mm"] = .8
            _transform_item(window.items[0], QTransform.fromTranslate(10, 0))
            moved = window.canvas.grab().toImage()
            self.assertEqual(moved.pixelColor(round(34+30.4*5), round(34+25.5*5)), QColor("red"))
            window.undo()
            self.assertEqual(window.items, original)
        finally:
            for editor in (window, reopened):
                with patch.object(editor, "_confirm_unsaved_changes", return_value="discard"):
                    editor.close()

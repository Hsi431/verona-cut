"""Batch cutline smoothing preserves ownership and saves the edited geometry."""
import copy
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog

from verona.lds import load_lds
from verona import mask_edit
from verona.mask_edit import EditImageDialog, retrace_item
from verona.smoothing_dialog import ContourSmoothingDialog
from verona.ui import VeronaWindow
from tests.test_editor_actions import image_item, rectangle
from tests.test_lds import document as native_document, vector


def jagged_contour(x=20., y=20.):
    return [(x, y), (x+2, y+.15), (x+4, y), (x+6, y+.15), (x+8, y),
            (x+10, y+.15), (x+12, y), (x+14, y+.15), (x+16, y),
            (x+18, y+.15), (x+20, y), (x+20, y+10), (x, y+10), (x, y)]


class PostEditSmoothingTests(unittest.TestCase):
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
        QSettings("Verona", "Workspace").clear()
        self.window = VeronaWindow(host="")

    def tearDown(self):
        self.window._confirm_unsaved_changes = lambda *_args: "discard"
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def native_lds_item(self):
        path = Path(self.temp.name) / "native.lds"
        points = [(0, 0, 16), (40, 0, 1), (40, 30, 1), (0, 30, 129)]
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("CDOC", native_document(vector(points)))
        item = load_lds(path).items[0]
        item["contours"] = [jagged_contour()]
        return item

    @staticmethod
    def vector_item(name="vector", x=60):
        contour = jagged_contour(x)
        item = rectangle(name, contours=[contour])
        item["node_paths"] = {"0": {"closed": True, "nodes": [
            {"point": point, "in": None, "out": None} for point in contour[:-1]]}}
        return item

    def batch_items(self):
        native = self.native_lds_item()
        vector_item = self.vector_item()
        raster = image_item(name="raster")
        raster["contours"] = [jagged_contour(100)]
        raster["image_clip"] = [[(101, 21), (119, 21), (119, 29), (101, 29), (101, 21)]]
        raster["bleed"] = {"width_mm": 1., "color": "#00ff00"}
        return [native, vector_item, raster, rectangle("unselected", 150, 50),
                rectangle("locked", 180, 50, locked=True),
                rectangle("registration", 210, 50, registration_guide=True)]

    @staticmethod
    def accept_with(dialog, tolerance):
        dialog.tolerance_spin.setValue(tolerance)
        return QDialog.DialogCode.Accepted

    def apply_smoothing(self, tolerance=.5):
        self.run_smoothing_dialog(QDialog.DialogCode.Accepted, tolerance)

    def run_smoothing_dialog(self, result, tolerance=.1):
        dialog_type = ContourSmoothingDialog

        def make_dialog(owner, indices):
            dialog = dialog_type(owner, indices)
            if result == QDialog.DialogCode.Accepted:
                dialog.exec = lambda: self.accept_with(dialog, tolerance)
            else:
                dialog.exec = lambda: result
            return dialog

        with patch("verona.smoothing_dialog.ContourSmoothingDialog", side_effect=make_dialog):
            self.window.smooth_selected_cutlines()

    def test_batch_smooths_selected_lds_vector_and_image_cutlines_and_undo_redo(self):
        self.window.items = self.batch_items()
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 210.,
                                               "paper_height": 297., "bounds": [5, 5, 205, 292]}}
        original = copy.deepcopy(self.window.items)
        metadata = copy.deepcopy(self.window.metadata)
        self.window.select_many([0, 1, 2])
        self.apply_smoothing(.5)

        self.assertEqual(len(self.window.undo_stack), 1)
        for index in (0, 1, 2):
            self.assertNotEqual(self.window.items[index]["contours"], original[index]["contours"])
        self.assertFalse(self.window.items[1].get("node_paths"))
        self.assertEqual(self.window.items[3:], original[3:])
        self.assertEqual(self.window.metadata, metadata)
        for key in ("image_data", "image_transform", "image_clip", "bleed"):
            self.assertEqual(self.window.items[2][key], original[2][key])

        smoothed = copy.deepcopy(self.window.items)
        self.window.undo()
        self.assertEqual(self.window.items, original)
        self.window.redo()
        self.assertEqual(self.window.items, smoothed)

        reopened = VeronaWindow(host="")
        try:
            for suffix in (".jdesign", ".lds"):
                path = Path(self.temp.name) / f"smoothed{suffix}"
                self.window._write_document(str(path))
                self.assertTrue(reopened._load_document(str(path)))
                self.assertEqual(reopened.items, smoothed)
                self.assertEqual(reopened.metadata, metadata)
        finally:
            reopened._confirm_unsaved_changes = lambda *_args: "discard"
            reopened.close()
            reopened.deleteLater()

    def test_cancel_and_zero_tolerance_leave_document_and_history_unchanged(self):
        self.window.items = self.batch_items()[:3]
        self.window.select_many([0, 1, 2])
        original = copy.deepcopy(self.window.items)
        self.run_smoothing_dialog(QDialog.DialogCode.Rejected)
        self.assertEqual(self.window.items, original)
        self.assertEqual(self.window.undo_stack, [])

        self.apply_smoothing(0)
        self.assertEqual(self.window.items, original)
        self.assertEqual(self.window.undo_stack, [])

    def test_locked_or_registration_only_selection_cannot_open_smoothing(self):
        self.window.items = [rectangle("locked", locked=True),
                             rectangle("registration", registration_guide=True)]
        original = copy.deepcopy(self.window.items)
        with patch("verona.smoothing_dialog.ContourSmoothingDialog") as execute:
            self.window.select(0)
            self.assertFalse(self.window.smooth_cutline_button.isEnabled())
            self.window.smooth_selected_cutlines()
            self.window.select(1)
            self.assertFalse(self.window.smooth_cutline_button.isEnabled())
            self.window.smooth_selected_cutlines()
        execute.assert_not_called()
        self.assertEqual(self.window.items, original)
        self.assertEqual(self.window.undo_stack, [])

    def test_legacy_mask_dialog_defaults_smoothing_to_zero_without_retracing(self):
        legacy = retrace_item(image_item(), {})
        legacy["trace_options"].pop("cut_smoothing_mm", None)
        original = copy.deepcopy(legacy)
        dialog = None
        try:
            with patch.object(mask_edit, "mask_paths", wraps=mask_edit.mask_paths) as retrace:
                dialog = EditImageDialog(legacy)
                self.assertEqual(dialog.cut_smoothing_spin.value(), 0)
                self.assertEqual(dialog.build_items(), [original])
                self.assertEqual(retrace.call_count, 0)
        finally:
            if dialog is not None:
                dialog.close()


if __name__ == "__main__":
    unittest.main()

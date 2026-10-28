"""Native bleed import and editing selected existing artwork without retracing."""
import copy
import math
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from verona import artwork
from verona.lds import load_lds
from verona.native_images import encode_qoi
from verona.ui import BleedDialog, VeronaWindow
from tests.test_lds import common, curve, document, record
from tests.test_editor_actions import image_item, rectangle


def native_image(settings, matrix=(10, 20, .1, 0, 0, .1)):
    points = [(0, 0, 16), (10, 0, 1), (10, 10, 1), (0, 10, 129)]
    return document(record(1012, 3, 2) + record(0, 100, 3, common(0xFF73B000, matrix))
        + b"".join(record(0, 110, 4, state) for state in settings)
        + record(0, 11, 5, curve(points, matrix), 101)
        + record(0, 11, 6, curve(points, matrix), 102)
        + record(0, 26, 7, struct.pack("<Q", 42))
        + record(0, 10, 8, encode_qoi(10, 10, b"\x00\x00\x00\xff" * 100))
        + record(1012, 4, 2))


class PostEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, self.folder.name)

    def load_native(self, states, matrix=(10, 20, .1, 0, 0, .1)):
        path = Path(self.folder.name) / "original.lds"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("CDOC", native_image(states, matrix))
        return load_lds(path).items[0]

    def window(self):
        window = VeronaWindow()
        self.addCleanup(window.deleteLater)
        return window

    def test_native_pixel_bleed_converts_to_mm_under_rotation_scale_and_mirror(self):
        state = struct.pack("<ffBBB", 0, 4, 1, 0, 1)
        for a, b, c, d in ((.1, 0, 0, .1), (0, .2, -.2, 0), (-.3, 0, 0, .3)):
            with self.subTest(matrix=(a, b, c, d)):
                item = self.load_native([state], (10, 20, a, b, c, d))
                scale = math.hypot(a + c, b + d) / math.sqrt(2)
                self.assertAlmostEqual(item["bleed"]["width_mm"], 4 * scale)
                self.assertAlmostEqual(item["bleed"]["inner_mm"], 1.5 * scale)
                self.assertEqual(item["bleed"]["color"], "#73b000")
                baseline = self.load_native([], (10, 20, a, b, c, d))
                for key in ("image_data", "image_transform", "image_clip", "contours"):
                    self.assertEqual(item[key], baseline[key])

    def test_native_disabled_and_inner_only_bleed_and_bad_records(self):
        self.assertNotIn("bleed", self.load_native([struct.pack("<ffBBB", 0, 4, 0, 0, 1)]))
        inner = self.load_native([struct.pack("<ffBBB", 0, 0, 1, 0, 1)])["bleed"]
        self.assertEqual(inner["width_mm"], 0)
        self.assertAlmostEqual(inner["inner_mm"], .15)
        valid = struct.pack("<ffBBB", 0, 4, 1, 0, 1)
        for states in ([valid, valid], [b"\0" * 10], [struct.pack("<ffBBB", 0, float("nan"), 1, 0, 1)],
                       [struct.pack("<ffBBB", 0, -1, 1, 0, 1)], [struct.pack("<ffBBB", 0, 4, 2, 0, 1)]):
            with self.subTest(states=states), self.assertRaises(ValueError):
                self.load_native(states)

    def test_sidebar_bulk_bleed_preserves_artwork_selection_undo_and_round_trips(self):
        window = self.window()
        window.items = [image_item(), rectangle(x=70), image_item(name="second", visible=False)]
        window.refresh_all()
        window.select_many([0, 1, 2])
        original = copy.deepcopy(window._document_state())
        self.assertTrue(window.edit_bleed_button.isEnabled())
        self.assertFalse(window.edit_image_button.isEnabled())

        def apply(dialog):
            self.assertEqual(dialog.indices, [0, 2])
            dialog.width_spin.setValue(.35)
            dialog.inner_spin.setValue(.1)
            dialog.color = QColor("#73b000")
            return QDialog.DialogCode.Accepted

        with patch.object(BleedDialog, "exec", apply), \
                patch("verona.raster_import.trace_paths", side_effect=AssertionError("Must not retrace")):
            window.edit_bleed_button.click()
        self.assertEqual(len(window.undo_stack), 1)
        self.assertEqual(window.selected_indices(), {0, 2})
        self.assertTrue(window.remove_bleed_button.isEnabled())
        for index in (0, 2):
            self.assertEqual(window.items[index]["bleed"], {"width_mm": .35, "inner_mm": .1, "color": "#73b000"})
            for key in ("image_data", "image_transform", "image_clip", "contours"):
                self.assertEqual(window.items[index][key], original["items"][index][key])
        edited = copy.deepcopy(window._document_state())
        for suffix in (".jdesign", ".lds"):
            path = Path(self.folder.name) / ("edited" + suffix)
            window._write_document(str(path))
            reopened = self.window()
            self.assertTrue(reopened._load_document(str(path)))
            self.assertEqual(reopened._document_state(), edited)
        window.undo()
        self.assertEqual(window._document_state(), original)
        window.redo()
        self.assertEqual(window._document_state(), edited)
        window.select_many([0, 1, 2])
        window.remove_bleed_button.click()
        self.assertEqual(window.selected_indices(), {0, 1, 2})
        self.assertNotIn("bleed", window.items[0])
        window.undo()
        self.assertEqual(window._document_state(), edited)

    def test_sidebar_selection_locked_and_language_states(self):
        window = self.window()
        self.assertFalse(window.edit_bleed_button.isEnabled())
        window.items = [image_item(locked=True), image_item()]
        window.refresh_all()
        window.select_many([0, 1])
        self.assertFalse(window.edit_bleed_button.isEnabled())
        window.select_many([1])
        self.assertTrue(window.edit_image_button.isEnabled())
        window.post_edit_section.setExpanded(True)
        window._change_language("en")
        self.assertTrue(window.post_edit_section.isExpanded())
        self.assertEqual(window.edit_bleed_button.text(), "Add / adjust bleed…")

    def test_solid_outer_and_inner_display_use_strokes_without_offset_booleans(self):
        item = image_item(bleed={"width_mm": 1., "inner_mm": .5, "color": "#ff0000"})
        pixels = QImage(500, 400, QImage.Format.Format_RGBA8888)
        pixels.fill(QColor("white"))
        painter = QPainter(pixels)
        painter.scale(10, 10)
        with patch.object(artwork, "_offset_path", side_effect=AssertionError("Display must not offset outlines")):
            artwork.draw_printed_item(painter, item)
            bounds = artwork.item_bounds(item, printed=True)
        painter.end()
        self.assertEqual(bounds.getRect(), (19., 19., 22., 12.))
        for x, expected in ((188, "#ffffff"), (195, "#ff0000"), (202, "#ff0000"), (215, "#000000")):
            self.assertEqual(pixels.pixelColor(x, 250).name(), expected)

    def test_restore_original_bleed_preserves_unsaved_edits_and_can_be_undone(self):
        original = self.load_native([struct.pack("<ffBBB", 0, 4, 1, 0, 1)])
        path = Path(self.folder.name) / "original.lds"
        backup = path.with_name(path.name + ".bak")
        backup.write_bytes(path.read_bytes())
        window = self.window()
        current = copy.deepcopy(original)
        current["name"] = "Renamed artwork"
        current["cut"] = False
        current["bleed"] = {"width_mm": 1., "color": "#ffffff", "mode": "edge"}
        window.items = [current, rectangle("extra object", x=70)]
        window.metadata = {"unsaved": "keep this"}
        window.current_file = str(path)
        window.refresh_all()
        window.select_many([0, 1])
        before = copy.deepcopy(window._document_state())
        self.assertTrue(window.restore_bleed_button.isEnabled())
        window.restore_bleed_button.click()
        expected = copy.deepcopy(before)
        expected["items"][0]["bleed"] = original["bleed"]
        self.assertEqual(window._document_state(), expected)
        self.assertEqual(window.selected_indices(), {0, 1})
        self.assertTrue(window.is_dirty())
        self.assertEqual(backup.read_bytes(), path.read_bytes())
        window.undo()
        self.assertEqual(window._document_state(), before)

    def test_restore_unmatched_backup_leaves_every_selected_image_untouched(self):
        item = self.load_native([struct.pack("<ffBBB", 0, 4, 1, 0, 1)])
        path = Path(self.folder.name) / "original.lds"
        path.with_name(path.name + ".bak").write_bytes(path.read_bytes())
        window = self.window()
        changed = copy.deepcopy(item)
        changed["image_transform"][0] += 3
        window.items = [item, changed]
        window.current_file = str(path)
        window.refresh_all()
        window.select_many([0, 1])
        before = copy.deepcopy(window._document_state())
        with patch.object(QMessageBox, "warning") as warning:
            window.restore_bleed_button.click()
        warning.assert_called_once()
        self.assertEqual(window._document_state(), before)
        self.assertFalse(window.undo_stack)

    def test_restore_can_choose_original_after_saving_as_jdesign_and_cancel_is_safe(self):
        original = self.load_native([struct.pack("<ffBBB", 0, 4, 1, 0, 1)])
        window = self.window()
        window.items = [copy.deepcopy(original)]
        window.items[0].pop("bleed")
        window.current_file = str(Path(self.folder.name) / "copy.jdesign")
        window.refresh_all()
        window.select(0)
        before = copy.deepcopy(window._document_state())
        with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")):
            window.restore_bleed_button.click()
        self.assertEqual(window._document_state(), before)
        self.assertFalse(window.undo_stack)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(Path(self.folder.name) / "original.lds"), "")):
            window.restore_bleed_button.click()
        self.assertEqual(window.items[0]["bleed"], original["bleed"])

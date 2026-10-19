"""Saved-mask editing preserves source pixels and world-space image geometry."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTemporaryDir
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import QApplication, QDialog

from verona.artwork import item_bounds
from verona.mask_edit import EditImageDialog, retrace_item, working_mask
from verona.raster_import import decode_embedded_image
from verona.ui import VeronaWindow, _transform_item
from tests.test_editor_actions import image_item, rectangle


class MaskEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = QTemporaryDir()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.path())
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_without_changes_retains_exact_original_even_with_custom_cut(self):
        item = image_item(cut=False, group_path=["a"])
        item["contours"] = rectangle(x=19.2, y=18.7, width=22.6)["contours"]
        original = copy.deepcopy(item)
        dialog = EditImageDialog(item)
        try:
            self.assertEqual(dialog.build_items(), [original])
            self.assertFalse(dialog.width_spin.isEnabled())
            self.assertEqual(item, original)
        finally:
            dialog.close()

    def test_retrace_retains_image_mask_affine_cut_flag_groups_and_print_bleed(self):
        item = image_item(cut=False, group_path=["a"], bleed={"width_mm": .5, "color": "#ff0000"})
        original = copy.deepcopy(item)
        updated = retrace_item(item, {"offset_mm": 1})
        for key in ("image_data", "image_transform", "cut", "group_path", "bleed"):
            self.assertEqual(updated[key], original[key])
        self.assertEqual(updated["image_clip"], original["image_clip"])
        box = item_bounds(updated)
        self.assertEqual((box.left(), box.top(), box.right(), box.bottom()), (19, 19, 41, 31))
        self.assertEqual(item, original)

    def test_saved_source_mask_and_mm_offset_survive_rotation_mirror_and_shear(self):
        for transform in (QTransform().translate(80, 30).rotate(90).scale(-2, .5),
                          QTransform(1, .2, .3, 1, 20, 40)):
            item = image_item()
            _transform_item(item, transform)
            unchanged = retrace_item(item, {"offset_mm": 0})
            for actual, expected in zip(unchanged["image_clip"][0], item["image_clip"][0], strict=True):
                for value, wanted in zip(actual, expected, strict=True):
                    self.assertAlmostEqual(value, wanted, places=10)
            self.assertEqual(unchanged["image_transform"], item["image_transform"])
            updated = retrace_item(item, {"offset_mm": 1})
            for actual, expected in zip(updated["image_clip"][0], item["image_clip"][0], strict=True):
                for value, wanted in zip(actual, expected, strict=True):
                    self.assertAlmostEqual(value, wanted, places=10)
            before, after = item_bounds(item), item_bounds(updated)
            self.assertLess(after.left(), before.left())
            self.assertGreater(after.right(), before.right())
            self.assertLessEqual(before.left()-after.left(), 1.001)
            self.assertLessEqual(after.right()-before.right(), 1.001)

    def test_trace_state_roundtrip_and_reopen_do_not_accumulate_offset(self):
        updated = retrace_item(image_item(), {"offset_mm": 1, "clip": False})
        document = {"format": "verona", "version": 1, "units": "mm", "items": [updated]}
        loaded, _ = VeronaWindow._validate_document(json.loads(json.dumps(document)))
        self.assertEqual(loaded[0], updated)
        repeated = retrace_item(loaded[0], {})
        self.assertEqual(repeated["contours"], updated["contours"])
        self.assertEqual(working_mask(loaded[0], decode_embedded_image(updated["image_data"])).shape, (10, 20))
        dialog = EditImageDialog(loaded[0])
        try:
            self.assertEqual(dialog.build_items(), loaded)
        finally:
            dialog.close()

    def test_saved_mask_inversion_changes_once_and_can_be_reversed_after_save(self):
        item = image_item()
        item["image_clip"] = rectangle(width=10)["contours"]
        image = decode_embedded_image(item["image_data"])
        original = working_mask(item, image)
        inverted = retrace_item(item, {"inverted": True})
        self.assertTrue(np.array_equal(working_mask(inverted, image), ~original))
        repeated = retrace_item(inverted, {})
        self.assertEqual(repeated["contours"], inverted["contours"])
        restored = retrace_item(inverted, {"inverted": False})
        self.assertTrue(np.array_equal(working_mask(restored, image), original))

    def test_invalid_saved_state_and_empty_tracing_cannot_replace_design(self):
        updated = retrace_item(image_item(), {})
        for changes in ({"trace_mask": "invalid"}, {"trace_options": {"tolerance": True}},
                        {"trace_options": {"width_mm": 50}}, {"trace_mask": image_item()["image_data"]}):
            bad = {**updated, **changes}
            if changes == {"trace_mask": image_item()["image_data"]}:
                # Same-sized opaque black bitmap is valid storage, but yields an empty mask.
                with self.assertRaises(ValueError):
                    retrace_item(bad, {})
            else:
                with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                    VeronaWindow._validate_document({"format": "verona", "version": 1,
                        "units": "mm", "items": [bad]})
        with self.assertRaises(ValueError):
            retrace_item(rectangle(), {})
        with self.assertRaises(ValueError):
            retrace_item(image_item(), {"mask_mode": "background", "background": "#000000"})

    def test_gui_edit_and_cancel_undo_and_save_preserve_existing_document(self):
        window = VeronaWindow()
        reopened = VeronaWindow()
        window.items = [image_item()]
        window.refresh_all()
        window.select(0)
        original = window._document_state()
        try:
            with patch.object(EditImageDialog, "exec", lambda dialog: QDialog.DialogCode.Rejected):
                window.edit_selected_image()
            self.assertEqual(window._document_state(), original)
            with patch.object(EditImageDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted):
                window.edit_selected_image()
            self.assertEqual(window._document_state(), original)
            self.assertEqual(len(window.undo_stack), 0)

            def accept_change(dialog):
                dialog.offset_spin.setValue(1)
                return QDialog.DialogCode.Accepted

            with patch.object(EditImageDialog, "exec", accept_change):
                window.edit_selected_image()
            self.assertEqual(len(window.undo_stack), 1)
            self.assertEqual(window.items[0]["image_data"], original["items"][0]["image_data"])
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "edited.jdesign"
                window._write_document(str(path))
                self.assertTrue(reopened._load_document(str(path)))
                self.assertEqual(reopened.items, window.items)
            window.undo()
            self.assertEqual(window.items, original["items"])
        finally:
            for editor in (window, reopened):
                with patch.object(editor, "_confirm_unsaved_changes", return_value="discard"):
                    editor.close()

    def test_import_bleed_pixels_do_not_grow_retraced_cut(self):
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtCore import Qt
        from verona.raster_import import build_raster_items, raster_options
        source = QImage(100, 50, QImage.Format.Format_RGBA8888)
        source.fill(Qt.GlobalColor.transparent)
        painter = QPainter(source)
        painter.fillRect(20, 10, 60, 30, QColor("red"))
        painter.end()
        options = raster_options(source, {"mask_mode": "alpha", "width_mm": 20, "bleed_mm": 2,
                                          "bleed_color": "#00ff00"})
        item = build_raster_items(source, "art", options)[0]
        items = VeronaWindow._validate_document({"format": "verona", "version": 1,
                                                 "units": "mm", "items": [item]})[0]
        for changes in ({"smoothing": 1}, {"offset_mm": 0}, {"keep_holes": False}):
            updated = retrace_item(items[0], changes)
            for printed in (False, True):
                old, new = item_bounds(item, printed=printed), item_bounds(updated, printed=printed)
                with self.subTest(changes=changes, printed=printed):
                    for a, b in ((old.left(), new.left()), (old.top(), new.top()),
                                 (old.right(), new.right()), (old.bottom(), new.bottom())):
                        self.assertAlmostEqual(a, b, delta=.3)
        self.assertEqual(items[0]["raster_bleed_mm"], 2)
        self.assertEqual(items[0]["trace_mask"], item["trace_mask"])
        for mode in ("alpha", "background"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "saved mask"):
                retrace_item(items[0], {"mask_mode": mode})
        dialog = EditImageDialog(items[0])
        try:
            model = dialog.mask_mode_combo.model()
            enabled = {dialog.mask_mode_combo.itemData(row): model.item(row).isEnabled()
                       for row in range(dialog.mask_mode_combo.count())}
            self.assertEqual(enabled, {"alpha": False, "background": False, "colors": False, "saved": True})
        finally:
            dialog.close()

    def test_import_bleed_retrace_preserves_affine_print_clip_and_cut_payload(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QImage, QPainter
        from verona.artwork import contour_path
        from verona.commands import encode_document
        from verona.editor_actions import render_preview
        from verona.raster_import import build_raster_items, raster_options
        source = QImage(100, 50, QImage.Format.Format_RGBA8888)
        source.fill(Qt.GlobalColor.transparent)
        painter = QPainter(source)
        painter.fillRect(20, 10, 60, 30, QColor("red"))
        painter.end()
        for height in (10, 15):
            original = build_raster_items(source, "art", raster_options(source,
                {"mask_mode": "alpha", "width_mm": 20, "height_mm": height,
                 "bleed_mm": 2, "bleed_color": "#00ff00"}))[0]
            for transform in (QTransform().scale(2, 2), QTransform().scale(2, 1),
                              QTransform().scale(.5, .5),
                              QTransform().rotate(33).scale(-2, 1).shear(.3, .1)):
                with self.subTest(height=height, transform=transform):
                    item = copy.deepcopy(original)
                    _transform_item(item, QTransform.fromTranslate(-120, -120))
                    _transform_item(item, transform)
                    _transform_item(item, QTransform.fromTranslate(100, 100))
                    item = VeronaWindow._validate_document({"format": "verona",
                        "version": 1, "units": "mm", "items": [item]})[0][0]
                    updated = retrace_item(item, {"offset_mm": 0})
                    before = contour_path(item["image_clip"])
                    after = contour_path(updated["image_clip"])
                    self.assertTrue(before.subtracted(after).isEmpty())
                    self.assertTrue(after.subtracted(before).isEmpty())
                    settings = {"custom": False, "tool": 0}
                    self.assertEqual(encode_document([item], {}, settings),
                                     encode_document([updated], {}, settings))
                    with tempfile.TemporaryDirectory() as folder:
                        paths = [Path(folder) / name for name in ("before.png", "after.png")]
                        for current, path in zip((item, updated), paths):
                            render_preview([{**current, "cut": False}], {}, path)
                        self.assertEqual(QImage(str(paths[0])), QImage(str(paths[1])))

    def test_import_bleed_scale_roundtrips_and_legacy_clip_is_preserved(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        from verona.raster_import import build_raster_items, raster_options
        source = QImage(100, 50, QImage.Format.Format_RGBA8888)
        source.fill(Qt.GlobalColor.red)
        item = build_raster_items(source, "art", raster_options(source,
            {"mask_mode": "alpha", "width_mm": 20, "height_mm": 15, "bleed_mm": 2}))[0]
        document = {"format": "verona", "version": 1, "units": "mm", "items": [item]}
        saved = VeronaWindow._validate_document(json.loads(json.dumps(document)))[0][0]
        self.assertEqual(saved["raster_bleed_scale"], [.2, .3])
        for scale in ([], [0, .3], [True, .3], [float("nan"), .3], [.2, .3, .4]):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                VeronaWindow._validate_document({**document, "items": [{**item, "raster_bleed_scale": scale}]})
        without_bleed = {key: value for key, value in item.items() if key != "raster_bleed_mm"}
        for invalid in (without_bleed, {**rectangle(), "raster_bleed_scale": [.2, .3]}):
            with self.subTest(invalid=invalid["name"]), self.assertRaises(ValueError):
                VeronaWindow._validate_document({**document, "items": [invalid]})
        legacy = copy.deepcopy(saved)
        legacy.pop("raster_bleed_scale")
        _transform_item(legacy, QTransform().scale(2, 1))
        updated = retrace_item(legacy, {"offset_mm": 0})
        self.assertEqual(updated["image_clip"], legacy["image_clip"])

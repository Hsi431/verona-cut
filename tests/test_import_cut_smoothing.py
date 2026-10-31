"""Millimetre cut smoothing affects only the shared cut path."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from verona import raster_import
from verona.raster_import import (ImportImageDialog, build_raster_items, raster_options,
                                  trace_paths)


class ImportCutSmoothingTests(unittest.TestCase):
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

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    @staticmethod
    def image():
        image = QImage(80, 60, QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        for y in range(8, 52):
            for x in range(10, 70):
                if not (x < 14 and y < 15):
                    image.setPixelColor(x, y, QColor("#263c34"))
        return image

    def options(self, **changes):
        return raster_options(self.image(), {"mask_mode": "alpha", "width_mm": 80,
                                             "height_mm": 60, "smoothing": 0,
                                             **changes})

    def test_legacy_missing_option_defaults_to_zero_and_validation_is_bounded(self):
        image = self.image()
        legacy = raster_options(image, {"mask_mode": "alpha", "width_mm": 80, "height_mm": 60})
        explicit = raster_options(image, {"mask_mode": "alpha", "width_mm": 80, "height_mm": 60,
                                          "cut_smoothing_mm": 0})
        self.assertEqual(legacy["cut_smoothing_mm"], 0)
        legacy_paths, explicit_paths = trace_paths(image, legacy), trace_paths(image, explicit)
        self.assertTrue(np.array_equal(legacy_paths[0], explicit_paths[0]))
        for legacy_path, explicit_path in zip(legacy_paths[1:], explicit_paths[1:]):
            self.assertEqual(legacy_path, explicit_path)
        for value in (-.001, .501, float("nan"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                raster_options(image, {"cut_smoothing_mm": value})

    def test_dialog_exposes_disabled_zero_default_and_millimetre_control(self):
        dialog = ImportImageDialog(self.image(), "synthetic")
        try:
            self.assertEqual(dialog.cut_smoothing_spin.value(), .1)
            self.assertEqual(dialog.cut_smoothing_spin.decimals(), 3)
            self.assertAlmostEqual(dialog.cut_smoothing_spin.singleStep(), .02)
            self.assertEqual(dialog.trace_options()["cut_smoothing_mm"], .1)
            self.assertIn("mm", dialog.cut_smoothing_spin.suffix())
            self.assertTrue(dialog.cut_smoothing_spin.toolTip())
            dialog.cut_smoothing_spin.setValue(0)
            self.assertEqual(dialog.trace_options()["cut_smoothing_mm"], 0)
        finally:
            dialog.close()

    def test_smoothing_only_changes_cut_path_and_preview_and_export_share_it(self):
        image = self.image()
        options = self.options(cut_smoothing_mm=.2)
        calls = []

        def shift_contours(contours, tolerance):
            calls.append((contours, tolerance))
            return [[(x + .5, y) for x, y in contour] for contour in contours]

        with patch.object(raster_import, "smooth_contours", side_effect=shift_contours):
            mask, raw, cut, bleed = trace_paths(image, options)
        base_options = dict(options, cut_smoothing_mm=0)
        base_mask, base_raw, base_cut, base_bleed = trace_paths(image, base_options)
        self.assertTrue(np.array_equal(mask, base_mask))
        self.assertEqual(raw, base_raw)
        self.assertEqual(bleed, base_bleed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], .2)
        self.assertAlmostEqual(cut.boundingRect().left(), base_cut.boundingRect().left() + .5,
                               places=5)

        dialog = ImportImageDialog(image, "synthetic")
        try:
            dialog.cut_smoothing_spin.setValue(.2)
            cached_paths = dialog._mask_and_paths()
            with patch.object(raster_import, "smooth_contours", side_effect=shift_contours) as smoother:
                # Build reuses the exact traced cut path cached for the preview.
                dialog._mask_cache = (None, None)
                cached_paths = dialog._mask_and_paths()
                preview_cut = cached_paths[2]
                items = dialog.build_items()
                self.assertEqual(smoother.call_count, 1)
            separately_built = build_raster_items(image, "synthetic", dialog.trace_options(),
                                                  cached_paths)
            self.assertEqual(items[0]["contours"], separately_built[0]["contours"])
            self.assertEqual(items[0]["image_clip"], build_raster_items(
                image, "synthetic", dict(dialog.trace_options(), cut_smoothing_mm=0))[0]["image_clip"])
            expected = raster_import._path_contours(
                preview_cut, dialog.width_spin.value(), dialog.height_spin.value(),
                image.width(), image.height())
            self.assertEqual(items[0]["contours"], expected)
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()

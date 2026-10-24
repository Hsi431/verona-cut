"""Bleed and offset geometry on traced outlines stays interactive."""
import copy
import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QPointF, QSettings
from PySide6.QtGui import QImage, QTransform
from PySide6.QtWidgets import QApplication

from verona import artwork
from verona.raster_import import build_raster_items, raster_options
from verona.ui import VeronaWindow, _transform_item
from tests.test_editor_actions import image_item


def traced_item(size=1200, blobs=250):
    """A pixel-stepped multi-blob outline like a real traced photo."""
    rng = np.random.default_rng(1)
    rgba = np.zeros((size, size, 4), np.uint8)
    yy, xx = np.mgrid[0:size, 0:size]
    for _ in range(blobs):
        cx, cy = rng.integers(0, size, 2)
        radius = rng.integers(size // 120 + 5, size // 15 + 5)
        inside = (xx - cx) ** 2 + (yy - cy) ** 2 < radius * radius
        rgba[inside, :3] = rng.integers(0, 255, 3)
        rgba[inside, 3] = 255
    image = QImage(rgba.data, size, size, size * 4, QImage.Format.Format_RGBA8888).copy()
    return build_raster_items(image, "traced", raster_options(image, {"width_mm": 80}))[0]


class BleedGeometryCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings.cleanup()

    def test_traced_outline_bleed_does_not_grow_quadratically(self):
        item = traced_item()
        self.assertGreater(sum(map(len, item["contours"])), 10_000)
        # A width no other test uses, so no cached ring can answer.
        item["bleed"] = {"width_mm": 2.0137, "color": "#ffffff"}
        start = time.perf_counter()
        ring = artwork.bleed_path(item)
        # One boolean over the whole stroke took about 25 s here.
        self.assertLess(time.perf_counter() - start, 10)
        self.assertFalse(ring.isEmpty())

    def test_outer_bleed_is_a_round_offset_of_the_cut(self):
        item = image_item(bleed={"width_mm": 1.0, "color": "#ffffff"})
        ring = artwork.bleed_path(item)
        # The cut is (20, 20)–(40, 30); a point 0.85 mm from its corner is
        # inside a round offset but outside a bevelled one.
        self.assertTrue(ring.contains(QPointF(40.6, 30.6)))
        self.assertFalse(ring.contains(QPointF(40.8, 30.8)))
        self.assertFalse(ring.contains(QPointF(30, 25)))

    def test_canvas_reuses_bleed_rings_across_paints_moves_and_refreshes(self):
        window = VeronaWindow()
        solid = image_item(bleed={"width_mm": 1.0, "color": "#ffffff", "inner_mm": .5})
        edge = image_item(bleed={"width_mm": 1.0, "color": "#ffffff", "mode": "edge"})
        _transform_item(edge, QTransform.fromTranslate(0, 40))
        window.items = [solid, edge]
        window.refresh_all()
        window.canvas.grab()
        source = copy.deepcopy(window.items[0])
        with patch("verona.artwork._offset_path", wraps=artwork._offset_path) as offset:
            window.canvas.grab()
            self.assertEqual(offset.call_count, 0)
            # Like a canvas drag: rigid moves of the drag-start item.
            for transform in (QTransform.fromTranslate(2.5, -1),
                              QTransform().translate(30, 25).rotate(90).translate(-30, -25)):
                _transform_item(window.items[0], transform, source)
                window.canvas.grab()
            self.assertEqual(offset.call_count, 0)
            window.refresh_all()
            window.canvas.grab()
            self.assertEqual(offset.call_count, 0)
        with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
            window.close()


if __name__ == "__main__":
    unittest.main()

"""Geometry helpers preserve path precision and remain independent of the UI."""
import math
import os
from pathlib import Path
import subprocess
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath, QTransform

from verona.geometry import (contour_path, copy_items, copy_snapshot, flatten_path,
                             item_path, transform_item)


ROOT = Path(__file__).resolve().parents[1]


class GeometryBoundaryTests(unittest.TestCase):
    def test_contour_and_item_paths_preserve_empty_open_and_closed_contours(self):
        contours = [[], [(1, 2), (3, 4)], [(5, 6), (7, 6), (7, 9), (5, 6)]]
        path = contour_path(contours)
        item = item_path({"contours": contours})
        self.assertEqual(path, item)
        self.assertEqual(len(path.toSubpathPolygons()), 2)
        self.assertEqual(path.currentPosition(), QPointF(5, 6))

    def test_copy_items_and_snapshot_detach_mutable_containers_but_reuse_points(self):
        point = (1.0, 2.0)
        item = {"contours": [[point, (3.0, 4.0)]],
                "image_clip": [[(5.0, 6.0), (7.0, 8.0)]],
                "nested": {"labels": ["original"]}}
        copied = copy_items([item])[0]
        self.assertIsNot(copied, item)
        self.assertIsNot(copied["contours"], item["contours"])
        self.assertIsNot(copied["contours"][0], item["contours"][0])
        self.assertIs(copied["contours"][0][0], point)
        self.assertIsNot(copied["image_clip"][0], item["image_clip"][0])
        self.assertIsNot(copied["nested"], item["nested"])
        copied["contours"][0].append((9.0, 10.0))
        copied["nested"]["labels"].append("copy")
        self.assertEqual(len(item["contours"][0]), 2)
        self.assertEqual(item["nested"]["labels"], ["original"])

        snapshot = {"items": [item], "selection": [0], "metadata": {"mode": "test"}}
        saved = copy_snapshot(snapshot)
        self.assertIsNot(saved["items"], snapshot["items"])
        self.assertIs(saved["items"][0]["contours"][0][0], point)
        saved["selection"].append(1)
        saved["metadata"]["mode"] = "changed"
        self.assertEqual(snapshot["selection"], [0])
        self.assertEqual(snapshot["metadata"]["mode"], "test")

    def test_flatten_path_keeps_curve_endpoints_and_scaled_flattening_precision(self):
        path = QPainterPath(QPointF(0, 0))
        path.cubicTo(QPointF(0, 1), QPointF(1, 1), QPointF(1, 0))
        flattened = flatten_path(path)
        self.assertEqual(len(flattened), 1)
        self.assertEqual(flattened[0][0], (0.0, 0.0))
        self.assertEqual(flattened[0][-1], (1.0, 0.0))
        self.assertGreater(len(flattened[0]), 8)

        def point_distance(point, start, end):
            dx, dy = end[0] - start[0], end[1] - start[1]
            length2 = dx*dx + dy*dy
            t = 0 if not length2 else max(0, min(1,
                ((point[0]-start[0])*dx + (point[1]-start[1])*dy) / length2))
            return math.hypot(point[0] - start[0] - t*dx,
                              point[1] - start[1] - t*dy)

        for step in range(201):
            t = step / 200
            exact = (3*t*t - 2*t*t*t, 3*t*(1-t))
            error = min(point_distance(exact, a, b)
                        for a, b in zip(flattened[0], flattened[0][1:]))
            self.assertLessEqual(error, .015)

    def test_transform_item_moves_contours_nodes_clips_and_image_basis(self):
        source = {
            "contours": [[(1., 2.), (4., 2.), (1., 2.)]],
            "image_clip": [[(2., 3.), (5., 3.), (2., 3.)]],
            "node_paths": {"0": {"closed": True, "nodes": [
                {"point": (1., 2.), "in": None, "out": (2., 2.)},
                {"point": (4., 2.), "in": (3., 2.), "out": None}]}},
            "image_transform": [10., 20., 2., 1., 3., 4.],
        }
        original = copy_items([source])[0]
        target = copy_items([source])[0]
        transform = QTransform().translate(5, 7).rotate(90).scale(2, 3)
        transform_item(target, transform, source)

        for before, after in zip(source["image_clip"][0], target["image_clip"][0]):
            mapped = transform.map(QPointF(*before))
            self.assertAlmostEqual(after[0], mapped.x())
            self.assertAlmostEqual(after[1], mapped.y())
        nodes = target["node_paths"]["0"]["nodes"]
        mapped_point = transform.map(QPointF(1, 2))
        mapped_out = transform.map(QPointF(2, 2))
        self.assertEqual(nodes[0]["point"], (mapped_point.x(), mapped_point.y()))
        self.assertEqual(nodes[0]["out"], (mapped_out.x(), mapped_out.y()))
        self.assertEqual(target["contours"][0][0], nodes[0]["point"])

        tx, ty, a, b, c, d = source["image_transform"]
        origin = transform.map(QPointF(tx, ty))
        x_basis = transform.map(QPointF(tx+a, ty+b))
        y_basis = transform.map(QPointF(tx+c, ty+d))
        self.assertEqual(target["image_transform"], [origin.x(), origin.y(),
            x_basis.x()-origin.x(), x_basis.y()-origin.y(),
            y_basis.x()-origin.x(), y_basis.y()-origin.y()])
        self.assertEqual(source, original)

    def test_helper_modules_import_without_loading_ui_or_creating_application(self):
        for module in ("verona.geometry", "verona.editor_actions", "verona.editor_document",
                       "verona.packing", "verona.file_io"):
            with self.subTest(module=module):
                source = ("import importlib, sys; "
                          f"importlib.import_module('{module}'); "
                          "from PySide6.QtWidgets import QApplication; "
                          "assert 'verona.ui' not in sys.modules; "
                          "assert QApplication.instance() is None")
                environment = dict(os.environ)
                environment["PYTHONPATH"] = str(ROOT/"src")
                result = subprocess.run([sys.executable, "-c", source], cwd=ROOT,
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

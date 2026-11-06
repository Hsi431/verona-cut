"""Regression coverage for the real smart-packing preview path."""
import math
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from tests.fixtures.nesting_cases import rectangle
from tests.test_packing_nesting import _assert_independent_layout
from verona.nesting_engine import pack_with_nesting
from verona.packing import pack_items
from verona.ui import PackingDialog, VeronaWindow


class SmartPackingRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window_and_dialog(self, items=None, metadata=None):
        window = VeronaWindow()
        window.items = items or [rectangle("sticker", 20, 20)]
        window.metadata = metadata or {}
        return window, PackingDialog(window, list(range(len(window.items))))

    def _pump_until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()
        self.assertTrue(predicate(), "timed out waiting for packing preview")

    def _pump_for(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()

    def _close(self, window, dialog):
        dialog.close()
        with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
            window.close()

    @staticmethod
    def _plan(items):
        return (items, list(range(len(items))), {
            "copies": 1, "paper_utilization_percent": 1.0, "page_count": 1,
            "unplaced_count": 0, "unplaced": [], "limit_reached": False,
            "placements": [], "units": [],
        })

    @staticmethod
    def _contains(point, contour):
        x, y = point
        contained = False
        for (x1, y1), (x2, y2) in zip(contour, contour[1:]):
            if (y1 > y) != (y2 > y) and x < (x2-x1)*(y-y1)/(y2-y1)+x1:
                contained = not contained
        return contained

    def test_engine_preview_does_not_run_legacy_packer(self):
        window, dialog = self._window_and_dialog()
        started, release = threading.Event(), threading.Event()

        def nested(items, *_args):
            started.set()
            release.wait(2)
            return self._plan(items)

        try:
            with patch("verona.nesting_engine.nesting_available", return_value=True), \
                    patch("verona.nesting_engine.pack_with_nesting", side_effect=nested), \
                    patch("verona.packing.pack_items", wraps=pack_items) as legacy:
                dialog.preview()
                self._pump_until(lambda: started.is_set())
                release.set()
                self._pump_until(dialog.apply_button.isEnabled)
                legacy.assert_not_called()
        finally:
            release.set()
            self._close(window, dialog)

    def test_engine_progress_advances_then_hides_after_result(self):
        window, dialog = self._window_and_dialog()
        started, release = threading.Event(), threading.Event()
        values = []
        sampler = QTimer()
        sampler.timeout.connect(lambda: values.append(dialog.progress_bar.value()))
        sampler.start(30)

        def nested(items, *_args):
            started.set()
            release.wait(2)
            return self._plan(items)

        try:
            dialog.time_limit_spin.setValue(2)
            with patch("verona.nesting_engine.nesting_available", return_value=True), \
                    patch("verona.nesting_engine.pack_with_nesting", side_effect=nested):
                dialog.preview()
                self.assertFalse(dialog.progress_bar.isHidden())
                self._pump_until(lambda: started.is_set())
                self._pump_for(.35)
                self.assertGreater(len(set(values)), 1)
                release.set()
                self._pump_until(dialog.apply_button.isEnabled)
                self.assertTrue(dialog.progress_bar.isHidden())
                self.assertFalse(dialog.progress_timer.isActive())
                self.assertEqual(dialog.progress_bar.value(), 100)
        finally:
            release.set()
            sampler.stop()
            self._close(window, dialog)

    def test_progress_timer_stops_when_settings_change_or_dialog_closes(self):
        for close_dialog in (False, True):
            with self.subTest(close_dialog=close_dialog):
                window, dialog = self._window_and_dialog()
                started, release = threading.Event(), threading.Event()

                def nested(items, *_args):
                    started.set()
                    release.wait(2)
                    return self._plan(items)

                try:
                    with patch("verona.nesting_engine.nesting_available", return_value=True), \
                            patch("verona.nesting_engine.pack_with_nesting", side_effect=nested):
                        dialog.preview()
                        self._pump_until(lambda: started.is_set())
                        self.assertTrue(dialog.progress_timer.isActive())
                        if close_dialog:
                            dialog.close()
                        else:
                            dialog.gap_spin.setValue(dialog.gap_spin.value() + 1)
                        self.assertFalse(dialog.progress_timer.isActive())
                        self.assertTrue(dialog.progress_bar.isHidden())
                        release.set()
                        self._pump_for(.15)
                finally:
                    release.set()
                    self._close(window, dialog)

    def test_engine_defaults_enable_rotation_and_shape_aware_packing(self):
        with patch("verona.nesting_engine.nesting_available", return_value=True):
            window, dialog = self._window_and_dialog()
        try:
            self.assertTrue(dialog.rotate_check.isChecked())
            self.assertTrue(dialog.shape_check.isChecked())
        finally:
            self._close(window, dialog)

    def test_missing_engine_uses_legacy_pack_items(self):
        with patch("verona.nesting_engine.nesting_available", return_value=False):
            window, dialog = self._window_and_dialog()
        started, release = threading.Event(), threading.Event()
        source = list(window.items)
        self.assertFalse(dialog.rotate_check.isChecked())
        self.assertFalse(dialog.shape_check.isChecked())

        def legacy(items, *_args):
            started.set()
            release.wait(2)
            return self._plan(items)

        try:
            with patch("verona.nesting_engine.nesting_available", return_value=False), \
                    patch("verona.packing.pack_items", side_effect=legacy) as old_pack:
                dialog.preview()
                self._pump_until(lambda: started.is_set())
                self.assertEqual(dialog.progress_bar.minimum(), 0)
                self.assertEqual(dialog.progress_bar.maximum(), 0)
                release.set()
                self._pump_until(dialog.apply_button.isEnabled)
                old_pack.assert_called_once()
                self.assertEqual(dialog.plan[0], source)
        finally:
            release.set()
            self._close(window, dialog)

    def test_fill_seed_ranking_prefers_more_total_copies(self):
        source = [rectangle("sticker", 20, 20)]

        def solve(_module, shapes, _demands, _requested, _extras, _allowed, _holes,
                  _width, _height, _gap, _seconds, _fill_seconds, seed):
            points = [(0, 0), (22.2, 0)] if seed == 7 else [(0, 0)]
            return [SimpleNamespace(id="unit-0", rotation=0,
                                    translation=point) for point in points]

        with patch("verona.nesting_engine._spyrrow", return_value=object()), \
                patch("verona.nesting_engine._solve_seed", side_effect=solve):
            plan = pack_with_nesting(source, {}, [0], {
                "copies_per_unit": [1], "gap_mm": 2, "fill": True,
                "time_limit_seconds": 5,
            }, seed_count=2, seed_start=7)
        self.assertIsNotNone(plan)
        self.assertEqual(plan[2]["copies"], 2)

    def test_parallel_seeds_share_budget_and_fast_finish_is_not_a_limit(self):
        source = [rectangle("sticker", 20, 20)]
        seconds = []

        def solve(_module, _shapes, _demands, _requested, _extras, _allowed, _holes,
                  _width, _height, _gap, solver_seconds, _fill_seconds, _seed):
            seconds.append(solver_seconds)
            time.sleep(1.1)
            return [SimpleNamespace(id="unit-0", rotation=0, translation=(0, 0))]

        started = time.monotonic()
        with patch("verona.nesting_engine._spyrrow", return_value=object()), \
                patch("verona.nesting_engine._solve_seed", side_effect=solve):
            plan = pack_with_nesting(source, {}, [0], {
                "copies_per_unit": [1], "gap_mm": 2, "time_limit_seconds": 5,
            }, seed_count=3)
        elapsed = time.monotonic() - started
        self.assertIsNotNone(plan)
        self.assertEqual(len(seconds), 3)
        self.assertEqual(len(set(seconds)), 1)
        self.assertGreater(seconds[0], 2)
        self.assertLess(elapsed, 5)
        self.assertFalse(plan[2]["limit_reached"])

    def test_seed_search_that_uses_its_budget_reports_limit_reached(self):
        source = [rectangle("sticker", 20, 20)]

        def solve(_module, _shapes, _demands, _requested, _extras, _allowed, _holes,
                  _width, _height, _gap, solver_seconds, _fill_seconds, _seed):
            time.sleep(solver_seconds + .1)
            return [SimpleNamespace(id="unit-0", rotation=0, translation=(0, 0))]

        with patch("verona.nesting_engine._spyrrow", return_value=object()), \
                patch("verona.nesting_engine._solve_seed", side_effect=solve):
            plan = pack_with_nesting(source, {}, [0], {
                "copies_per_unit": [1], "gap_mm": 2, "time_limit_seconds": 5,
            }, seed_count=2)
        self.assertIsNotNone(plan)
        self.assertTrue(plan[2]["limit_reached"])

    def test_paired_cutline_contours_follow_selected_images(self):
        def square(name, left, top, size, *, image=False):
            contour = [(left, top), (left+size, top), (left+size, top+size),
                       (left, top+size), (left, top)]
            item = {"name": name, "cut": True, "print": False,
                    "contours": [contour]}
            if image:
                item["image_data"] = "unused"
            return item

        images = [square("image a", 10, 10, 20, image=True),
                  square("image b", 60, 10, 20, image=True)]
        cutters = {"name": "combined cutter", "cut": True, "print": False,
                   "contours": [
                       [(9, 9), (31, 9), (31, 31), (9, 31), (9, 9)],
                       [(59, 9), (81, 9), (81, 31), (59, 31), (59, 9)],
                   ]}
        source = [cutters, *images]
        result, selected, details = pack_items(source, {}, [0, 1, 2], {
            "copies_per_unit": [1, 1], "gap_mm": 2,
        })
        self.assertEqual(details["copies"], 2)
        self.assertEqual(len(selected), 3)
        image_items = [item for item in result if "image_data" in item]
        cut_items = [item for item in result if item["name"] == "combined cutter"]
        self.assertEqual(len(image_items), 2)
        self.assertEqual(len(cut_items), 1)
        self.assertEqual(len(cut_items[0]["contours"]), 2)
        paired_shapes = []
        for image in image_items:
            image_contour = image["contours"][0]
            matches = []
            for contour in cut_items[0]["contours"]:
                if all(self._contains(point, contour) for point in image_contour[:-1]):
                    matches.append(contour)
            self.assertEqual(len(matches), 1)
            paired_shapes.append({"name": image["name"],
                                  "contours": [image_contour, matches[0]]})
        self.assertFalse(any(key.startswith("_packing_")
                             for item in result for key in item))
        _assert_independent_layout(self, paired_shapes, [0, 1], {}, 2)

    def test_paired_cutline_contours_restore_after_nesting_engine(self):
        def square(name, left, top, size, *, image=False):
            contour = [(left, top), (left+size, top), (left+size, top+size),
                       (left, top+size), (left, top)]
            item = {"name": name, "cut": True, "print": False,
                    "contours": [contour]}
            if image:
                item["image_data"] = "unused"
            return item

        source = [
            {"name": "combined cutter", "cut": True, "print": False,
             "contours": [
                 [(9, 9), (31, 9), (31, 31), (9, 31), (9, 9)],
                 [(59, 9), (81, 9), (81, 31), (59, 31), (59, 9)],
             ]},
            square("image a", 10, 10, 20, image=True),
            square("image b", 60, 10, 20, image=True),
        ]

        def solve(*args):
            return [
                SimpleNamespace(id="unit-0", rotation=0, translation=(0, 0)),
                SimpleNamespace(id="unit-1", rotation=0, translation=(24.2, 0)),
            ]

        with patch("verona.nesting_engine._spyrrow", return_value=object()), \
                patch("verona.nesting_engine._solve_seed", side_effect=solve):
            plan = pack_with_nesting(source, {}, [0, 1, 2], {
                "copies_per_unit": [1, 1], "gap_mm": 2, "time_limit_seconds": 5,
            }, seed_count=1)
        self.assertIsNotNone(plan)
        image_items = [item for item in plan[0] if "image_data" in item]
        cut_items = [item for item in plan[0] if item["name"] == "combined cutter"]
        self.assertEqual((len(image_items), len(cut_items)), (2, 1))
        self.assertEqual(len(cut_items[0]["contours"]), 2)
        self.assertEqual([unit["source_indices"] for unit in plan[2]["units"]],
                         [[0, 1], [0, 2]])

    def test_pack_items_keeps_configured_edge_clearance(self):
        source = [rectangle("first", 30, 20), rectangle("second", 25, 22)]
        result, selected, details = pack_items(source, {}, [0, 1], {
            "copies_per_unit": [1, 1], "gap_mm": 1.0, "shape_aware": True,
        })
        self.assertEqual(details["unplaced_count"], 0)
        _assert_independent_layout(self, result, selected, {}, 1.0)

    def test_long_outline_packing_skips_legacy_piecewise_offset(self):
        count = 6000
        points = []
        for index in range(count):
            angle = 2 * math.pi * index / count
            radius = 20 + 2 * math.sin(31 * angle) + math.sin(97 * angle)
            points.append((25 + radius * math.cos(angle), 25 + radius * math.sin(angle)))
        points.append(points[0])
        source = [{"name": "long outline", "cut": True, "print": False,
                   "contours": [points]}]
        with patch("verona.packing._offset_path",
                   side_effect=AssertionError("legacy _offset_path was used"), create=True):
            result, selected, details = pack_items(source, {}, [0], {
                "copies_per_unit": [1], "gap_mm": 1, "time_limit_seconds": 5,
            })
        self.assertEqual(details["copies"], 1)
        self.assertEqual(len(selected), 1)


if __name__ == "__main__":
    unittest.main()

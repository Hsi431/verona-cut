"""Product-path tests for the optional spyrrow strip-nesting backend."""
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from tests.fixtures.nesting_cases import rectangle
from verona.i18n import tr
from verona.preview_dialogs import PackingDialog
from verona.ui import VeronaWindow


def _box_clearance(first, second):
    dx = max(first[0] - second[2], second[0] - first[2], 0)
    dy = max(first[1] - second[3], second[1] - first[3], 0)
    return (dx*dx + dy*dy) ** .5


class NestingEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _pump_until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.003)
        self.app.processEvents()
        self.assertTrue(predicate(), "timed out waiting for nesting result")

    def test_official_spyrrow_without_holes_is_reported_unavailable(self):
        from verona.nesting_engine import nesting_available

        class OfficialInstance:
            def __init__(self, name, height, items):
                pass

        module = SimpleNamespace(StripPackingInstance=OfficialInstance)
        with patch.dict("sys.modules", {"spyrrow": module}):
            self.assertFalse(nesting_available())

    def test_real_window_keeps_legacy_preview_when_engine_is_unavailable(self):
        window = VeronaWindow()
        window.items = [rectangle("sticker", 12, 10)]
        window.metadata = {}
        window.refresh_all()
        window.select_many([0])
        with patch("verona.nesting_engine.nesting_available", return_value=False):
            def accept(dialog):
                dialog.time_limit_spin.setValue(2)
                dialog.preview()
                self._pump_until(lambda: dialog.apply_button.isEnabled())
                self.assertIsNotNone(dialog.plan)
                self.assertNotIn("engine", dialog.plan[2])
                dialog.accept()
                return QDialog.DialogCode.Accepted

            try:
                with patch.object(PackingDialog, "exec", new=accept):
                    window.pack_selected()
                    self.assertEqual(len(window.items), 1)
                    self.assertEqual(window.items[0]["name"], "sticker")
                    self.assertEqual(len(window.undo_stack), 1)
            finally:
                with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                    window.close()

    def test_window_can_apply_first_preview_while_nesting_is_still_running(self):
        window = VeronaWindow()
        window.items = [rectangle("sticker", 12, 10)]
        window.metadata = {}
        window.refresh_all()
        window.select_many([0])
        started, release, returned = threading.Event(), threading.Event(), threading.Event()

        def blocked_nesting(*_args, **_kwargs):
            started.set()
            release.wait(2)
            returned.set()
            return None

        with patch("verona.nesting_engine.nesting_available", return_value=True), \
                patch("verona.nesting_engine.pack_with_nesting", side_effect=blocked_nesting):
            def accept(dialog):
                dialog.time_limit_spin.setValue(2)
                dialog.preview()
                self._pump_until(lambda: started.is_set())
                self._pump_until(lambda: dialog.apply_button.isEnabled())
                self.assertIn(tr("排版中…"), dialog.summary.text())
                self.assertIsNotNone(dialog.plan)
                dialog.apply_button.click()
                return QDialog.DialogCode.Accepted

            try:
                with patch.object(PackingDialog, "exec", new=accept):
                    window.pack_selected()
                    self.assertEqual(window.items[0]["name"], "sticker")
                    self.assertEqual(len(window.undo_stack), 1)
            finally:
                release.set()
                self._pump_until(lambda: returned.is_set())
                with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                    window.close()

    def test_setting_change_discards_late_nesting_result_after_first_preview(self):
        window, dialog = VeronaWindow(), None
        window.items = [rectangle("sticker", 12, 10)]
        window.metadata = {}
        window.refresh_all()
        dialog = PackingDialog(window, [0])
        started, release, returned = threading.Event(), threading.Event(), threading.Event()

        def blocked_nesting(*_args, **_kwargs):
            started.set()
            release.wait(2)
            returned.set()
            return None

        with patch("verona.nesting_engine.nesting_available", return_value=True), \
                patch("verona.nesting_engine.pack_with_nesting", side_effect=blocked_nesting):
            try:
                dialog.time_limit_spin.setValue(2)
                dialog.preview()
                self._pump_until(lambda: started.is_set())
                self._pump_until(lambda: dialog.apply_button.isEnabled())
                self.assertIsNotNone(dialog.plan)
                dialog.gap_spin.setValue(3)
                self.assertIsNone(dialog.plan)
                self.assertFalse(dialog.apply_button.isEnabled())
                release.set()
                self._pump_until(lambda: returned.is_set())
                self.app.processEvents()
                self.assertIsNone(dialog.plan)
            finally:
                release.set()
                dialog.close()
                with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                    window.close()

    def test_static_keepouts_and_markers_are_respected_by_real_outline_result(self):
        from verona.nesting_engine import nesting_available, pack_with_nesting

        if not nesting_available():
            self.skipTest("patched spyrrow wheel with holes= is not installed")
        fixed = rectangle("fixed object", 20, 60, 14, 14)
        selected = rectangle("sticker", 15, 15)
        metadata = {"print_cut": {"enabled": True, "paper_width": 120,
                    "paper_height": 100, "bounds": [10, 10, 110, 90]}}
        plan = pack_with_nesting([fixed, selected], metadata, [1], {
            "copies_per_unit": [4], "gap_mm": 2, "margin_mm": 4,
            "allow_rotate": False, "shape_aware": True,
            "rotation_step_deg": 90, "time_limit_seconds": 2,
        }, seed_count=2)
        self.assertIsNotNone(plan)
        items, selection, details = plan
        self.assertEqual(details["engine"], "spyrrow")
        self.assertGreater(details["copies"], 0)
        self.assertEqual(details["unplaced_count"], 0)
        self.assertEqual(len(selection), details["copies"])
        fixed_box = [14, 14, 34, 74]
        for placement in details["placements"]:
            self.assertGreaterEqual(_box_clearance(placement["bounds_mm"], fixed_box), 2-1e-5)
            self.assertGreaterEqual(placement["bounds_mm"][0], 14-1e-5)
            self.assertGreaterEqual(placement["bounds_mm"][1], 14-1e-5)
            self.assertLessEqual(placement["bounds_mm"][2], 106+1e-5)
            self.assertLessEqual(placement["bounds_mm"][3], 86+1e-5)
        self.assertEqual(len(items), details["copies"] + 1)

    def test_unplaceable_input_reports_unplaced_and_verification_failure_discards(self):
        from verona.nesting_engine import nesting_available, pack_with_nesting

        if not nesting_available():
            self.skipTest("patched spyrrow wheel with holes= is not installed")
        source = [rectangle("too large", 70, 70)]
        metadata = {"print_cut": {"enabled": True, "paper_width": 60,
                    "paper_height": 60, "bounds": [5, 5, 55, 55]}}
        plan = pack_with_nesting(source, metadata, [0], {
            "copies_per_unit": [1], "gap_mm": 1, "margin_mm": 0,
            "allow_rotate": False, "shape_aware": False,
            "rotation_step_deg": 90, "time_limit_seconds": 2,
        }, seed_count=1)
        self.assertIsNotNone(plan)
        self.assertEqual(plan[2]["unplaced_count"], 1)
        self.assertEqual(plan[2]["unplaced"][0]["objects"], ["too large"])
        with patch("verona.nesting_engine._verify_layout", return_value=False) as verify:
            rejected = pack_with_nesting(source, metadata, [0], {
                "copies_per_unit": [1], "gap_mm": 1, "margin_mm": 0,
                "allow_rotate": False, "shape_aware": False,
                "rotation_step_deg": 90, "time_limit_seconds": 2,
            }, seed_count=1)
        verify.assert_called()
        self.assertIsNone(rejected)

    def test_fill_adds_only_selected_unit_copies(self):
        from verona.nesting_engine import nesting_available, pack_with_nesting

        if not nesting_available():
            self.skipTest("patched spyrrow wheel with holes= is not installed")
        source = [rectangle("selected sticker", 10, 10)]
        plan = pack_with_nesting(source, {}, [0], {
            "copies_per_unit": [1], "gap_mm": 1, "margin_mm": 0,
            "allow_rotate": True, "shape_aware": True, "fill": True,
            "rotation_step_deg": 90, "time_limit_seconds": 4,
        }, seed_count=1)
        self.assertIsNotNone(plan)
        result, selection, details = plan
        self.assertGreater(details["copies"], 1)
        self.assertEqual(details["units"][0]["requested_placed"], 1)
        self.assertEqual(len(selection), details["copies"])
        self.assertEqual([result[index]["name"] for index in selection],
                         ["selected sticker"]*details["copies"])


if __name__ == "__main__":
    unittest.main()

"""Optional job order preserves directed contours and default encoded bytes."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTemporaryDir
from PySide6.QtWidgets import QApplication

from verona.commands import encode_document, encode_plt
from verona.i18n import set_language
from verona.mcp_server import VeronaController
from verona.path_order import analyze_path_order, cut_contours
from verona.ui import VeronaWindow
from tests.test_editor_actions import rectangle


class PathOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = QTemporaryDir()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.path())
        cls.app = QApplication.instance() or QApplication([])

    def test_shared_encoder_retains_default_bytes_and_saved_settings_overrides(self):
        items = [rectangle(), rectangle(x=70, y=70, cut=False)]
        settings = {"custom": True, "force": 15, "speed": 11, "tool": 1}
        for metadata in ({}, {"print_cut": {"enabled": True, "paper_width": 100,
                "paper_height": 100, "bounds": [10, 10, 90, 90], "offset": [-.1, .1]}}):
            registration = metadata.get("print_cut", {})
            expected = encode_plt(items[0]["contours"], force=15, speed=11, tool=1,
                registration_bounds=registration.get("bounds"), registration_offset=registration.get("offset", (0, 0)))
            self.assertEqual(encode_document(items, metadata, settings), expected)
            self.assertEqual(encode_document(items, {**metadata, "optimize_paths": False}, settings), expected)
            self.assertIn(b"!FS17;", encode_document(items, metadata, settings, force=17))

    def test_gui_confirmation_uses_the_encoded_job_snapshot(self):
        from verona.commands import encode_document_job
        from verona.i18n import tr
        window = VeronaWindow()
        window.items = [rectangle(x=20, y=20, width=10, height=10)]
        window.metadata = {"print_cut": {"enabled": True, "bounds": [6, 6, 94, 94],
            "paper_width": 100, "paper_height": 100, "offset": [-.1, .2]}}
        try:
            payload, job = encode_document_job(window.items, window.metadata,
                {"custom": True, "force": 17, "speed": 7, "tool": 1})
            # Later control values must not describe the already encoded payload.
            window.force_spin.setValue(20)
            window.speed_spin.setValue(5)
            window.tool_combo.setCurrentIndex(0)
            window.metadata["print_cut"]["offset"] = [1, 1]
            details = window._send_confirmation(payload, job)
            self.assertIn(tr("記號筆"), details)
            self.assertIn(tr("速度 {speed} / 壓力 {force}", speed=7, force=17), details)
            self.assertIn("-0.100", details)
            self.assertIn("+0.200", details)
            self.assertIn("X 20.00", details)
        finally:
            with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                window.close()

    def test_nearest_candidate_reduces_travel_without_changing_any_directed_path(self):
        items = [{"name": "lines", "contours": [[(100, 10), (101, 10)],
            [(10, 10), (11, 10)], [(90, 10), (91, 10)]]}]
        original = copy.deepcopy(items)
        contours, details = analyze_path_order(items, {})
        self.assertGreater(details["travel_saved_mm"], 0)
        self.assertEqual([entry["contour_index"] for entry in details["order"]], [1, 2, 0])
        for contour in contours:
            self.assertTrue(any(contour is source for source in items[0]["contours"]))
        self.assertEqual(items, original)

    def test_nested_holes_and_duplicate_boundaries_keep_every_contour_inside_first(self):
        items = [rectangle(width=80, height=80, x=10, y=10),
                 rectangle(width=40, height=40, x=30, y=30),
                 rectangle(width=20, height=20, x=40, y=40)]
        items.append(copy.deepcopy(items[0]))
        items.append(rectangle(width=100, height=100, cut=False))
        result, details = analyze_path_order(items, {})
        order = [entry["item_index"] for entry in details["order"]]
        self.assertEqual(len(result), 4)
        self.assertLess(order.index(2), order.index(1))
        self.assertLess(order.index(1), order.index(0))
        self.assertLess(order.index(1), order.index(3))
        self.assertNotIn(4, order)
        self.assertEqual(sum(contour == items[0]["contours"][0] for contour in result), 2)

    def test_inside_first_honestly_reports_a_travel_increase_for_an_open_inner_cut(self):
        items = [{"name": "outer", "contours": [[(80, 0), (100, 0), (100, 100),
                (0, 100), (0, 0), (80, 0)]]},
                 {"name": "inside", "contours": [[(90, 10), (10, 10)]]}]
        result, details = analyze_path_order(items, {})
        self.assertEqual(result, [items[1]["contours"][0], items[0]["contours"][0]])
        self.assertTrue(details["travel_increased_for_inside_first"])
        self.assertLess(details["travel_saved_mm"], 0)

    def test_limit_and_bad_flags_reject_without_changes_and_disable_bypasses_search(self):
        many = [{"name": "many", "contours": [[(1, 1), (2, 2)]]*4001}]
        with self.assertRaisesRegex(ValueError, "4000"):
            analyze_path_order(many, {}, True)
        self.assertEqual(len(cut_contours(many, {})), 4001)
        self.assertEqual(analyze_path_order(many, {}, False)[1]["contours"], 4001)
        with self.assertRaises(ValueError):
            cut_contours(many, {"optimize_paths": 1})
        items = [rectangle(width=80, height=80), rectangle(x=30, y=30)]
        original = copy.deepcopy(items)
        with patch("verona.path_order.time.monotonic", side_effect=[0, 11]), self.assertRaises(ValueError):
            analyze_path_order(items, {})
        self.assertEqual(items, original)
        with self.assertRaises(ValueError):
            VeronaWindow._validate_document({"format": "verona", "version": 1,
                "units": "mm", "items": items, "metadata": {"optimize_paths": 1}})

    def test_gui_flag_roundtrip_tabs_language_undo_and_mcp_encode_the_same_snapshot(self):
        window, reopened = VeronaWindow(), VeronaWindow()
        window.items = [rectangle(width=60, height=60), rectangle(x=30, y=30)]
        window.metadata = {"print_cut": {"enabled": True, "paper_width": 100,
            "paper_height": 100, "bounds": [10, 10, 90, 90]}}
        window.refresh_all()
        original = copy.deepcopy(window.items)
        before = window._encoded_plt()
        try:
            window.set_path_order(True)
            self.assertTrue(window.path_order_action.isChecked())
            self.assertEqual(window.items, original)
            self.assertEqual(len(window.undo_stack), 1)
            after = window._encoded_plt()
            self.assertNotEqual(after, before)
            self.assertTrue(after.endswith(b" U2800,0 U !PG;!PG;"))
            self.assertTrue(before.endswith(b" U2800,0 U !PG;!PG;"))
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "ordered.jdesign"
                window._write_document(str(path))
                self.assertTrue(reopened._load_document(str(path)))
                self.assertTrue(reopened.path_order_action.isChecked())
                self.assertEqual(reopened._encoded_plt(), after)
                controller = VeronaController(lock_path=Path(folder)/"device.lock")
                job = controller.prepare_job(path)
                self.assertTrue(job["path_order_enabled"])
                self.assertEqual(controller.jobs[job["job_id"]], after)
            window._change_language("en")
            self.assertTrue(window.path_order_action.isChecked())
            self.assertEqual(window._encoded_plt(), after)
            window.new_design()
            self.assertFalse(window.path_order_action.isChecked())
            window._switch_document(0)
            self.assertTrue(window.path_order_action.isChecked())
            window.undo()
            self.assertFalse(window.path_order_action.isChecked())
            self.assertEqual(window._encoded_plt(), before)
        finally:
            for editor in (window, reopened):
                with patch.object(editor, "_confirm_unsaved_changes", return_value="discard"):
                    editor.close()
            set_language("zh_TW")

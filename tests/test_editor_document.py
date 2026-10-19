"""Detached creation/configuration and group-aware alignment regression tests."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from verona.artwork import item_bounds
from verona.editor_document import build_document_edit
from verona.ui import VeronaWindow
from verona.raster_import import ImportImageDialog
from tests.test_editor_actions import rectangle
from tests import test_raster_import


class EditorDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def build(self, items, action, parameters, selection=None, metadata=None):
        return build_document_edit(items, metadata or {}, {}, selection or [], action, parameters)

    def test_alignment_and_distribution_treat_groups_as_units(self):
        items = [rectangle(x=10, y=10, group_path=["pair"]),
                 rectangle(x=35, y=10, group_path=["pair"]),
                 rectangle(x=80, y=50), rectangle(x=150, y=100)]
        original = copy.deepcopy(items)
        aligned, _, _, selected, details = self.build(items, "align", {"mode": "top"}, [0, 2, 3])
        self.assertEqual(selected, [0, 1, 2, 3])
        self.assertEqual(details["units"], 3)
        self.assertTrue(all(item_bounds(item).top() == 10 for item in aligned))
        self.assertEqual(item_bounds(aligned[1]).left()-item_bounds(aligned[0]).left(), 25)
        distributed, *_ = self.build(items, "align", {"mode": "distribute_h"}, [0, 2, 3])
        first_right = item_bounds(distributed[1]).right()
        middle = item_bounds(distributed[2])
        last_left = item_bounds(distributed[3]).left()
        self.assertAlmostEqual(middle.left()-first_right, last_left-middle.right())
        self.assertEqual(items, original)

    def test_new_shapes_use_exact_mm_without_changing_existing_artwork(self):
        original = [rectangle()]
        for shape in ("rectangle", "ellipse"):
            result, _, _, selected, _ = self.build(original, "add_shape",
                {"shape": shape, "x_mm": 12.5, "y_mm": 9.5, "width_mm": 32, "height_mm": 18})
            self.assertEqual(selected, [1])
            box = item_bounds(result[-1])
            self.assertEqual((box.left(), box.top(), box.width(), box.height()), (12.5, 9.5, 32, 18))
            self.assertEqual(result[0], original[0])

    def test_text_size_scales_physically_and_reports_resolved_font(self):
        small, *_, details = self.build([], "add_text", {"text": "AB", "font_size_mm": 5})
        large, *_ = self.build([], "add_text", {"text": "AB", "font_size_mm": 10})
        self.assertTrue(details["resolved_font"])
        self.assertAlmostEqual(item_bounds(large[0]).width(), item_bounds(small[0]).width()*2, places=3)
        self.assertLess(item_bounds(large[0]).height(), 12)

    def test_configure_detaches_metadata_and_preserves_artwork_and_selection(self):
        metadata = {"print_cut": {"enabled": True, "paper_width": 100, "paper_height": 100,
            "bounds": [6, 6, 94, 94], "printer_margins": [5]*4}}
        before = copy.deepcopy(metadata)
        items = [rectangle()]
        result, updated, settings, selection, details = self.build(items, "configure",
            {"registration": {"paper_height": 200, "mark_stroke": 1.5},
             "cut_settings": {"custom": True, "force": 15, "speed": 11, "tool": 1}}, [0], metadata)
        self.assertEqual(updated["print_cut"]["bounds"], [6., 6., 94., 194.])
        self.assertTrue(details["requires_reprint"])
        self.assertEqual(selection, [0])
        self.assertEqual(result, items)
        self.assertEqual(settings["tool"], 1)
        self.assertEqual(metadata, before)
        _, _, _, _, offset = self.build(items, "configure",
            {"registration": {"offset": [-.1, .1]}}, metadata=metadata)
        self.assertFalse(offset["requires_reprint"])

    def test_invalid_plans_reject_unknown_parameters_and_printer_clipping(self):
        metadata = {"print_cut": {"enabled": True, "paper_width": 100, "paper_height": 100,
            "bounds": [6, 6, 94, 94], "printer_margins": [5]*4}}
        for action, parameters in (
            ("add_shape", {"shape": "rectangle", "width_mm": True}),
            ("add_shape", {"shape": "triangle"}),
            ("add_text", {"text": " "}),
            ("configure", {}),
            ("configure", {"cut_settings": {"host": "192.0.2.10"}}),
            ("configure", {"registration": {"margins": [5]*4}}),
            ("configure", {"registration": {"margins": [60]*4}}),
        ):
            with self.subTest(action=action, parameters=parameters), self.assertRaises(ValueError):
                result, meta, settings, *_ = self.build([], action, parameters, metadata=metadata)
                VeronaWindow._validate_document({"format": "verona", "version": 1,
                    "units": "mm", "items": result, "metadata": meta, "settings": settings})

    def test_import_svg_keeps_physical_size_existing_metadata_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "shape.svg"
            source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="20mm" '
                'viewBox="0 0 40 20"><rect width="40" height="20"/></svg>')
            original = source.read_bytes()
            metadata = {"note": "keep existing work"}
            existing = [rectangle()]
            result, updated, _, selected, _ = self.build(existing, "import_artwork",
                {"path": str(source), "x_mm": 12, "y_mm": 18}, metadata=metadata)
            box = item_bounds(result[1])
            self.assertEqual((box.left(), box.top(), box.width(), box.height()), (12, 18, 40, 20))
            self.assertEqual(selected, [1])
            self.assertEqual(updated, metadata)
            self.assertEqual(result[0], existing[0])
            self.assertEqual(source.read_bytes(), original)

    def test_raster_import_matches_gui_geometry_and_creates_detached_registration(self):
        image = test_raster_import.RasterImportTests._transparent_donut()
        dialog = ImportImageDialog(image, "donut")
        dialog.width_spin.setValue(60)
        gui_item = dialog.build_items()[0]
        try:
            with tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / "donut.png"
                image.save(str(source))
                original = source.read_bytes()
                result, metadata, _, selected, details = self.build([], "import_artwork",
                    {"path": str(source), "tracing": {"width_mm": 60}})
                self.assertEqual(selected, [0])
                self.assertTrue(details["requires_reprint"])
                self.assertEqual(metadata["print_cut"]["bounds"], [6., 6., 204., 291.])
                self.assertEqual(len(result[0]["contours"]), len(gui_item["contours"]))
                box, gui_box = item_bounds(result[0]), item_bounds(gui_item)
                self.assertAlmostEqual(box.width(), gui_box.width())
                self.assertAlmostEqual(box.height(), gui_box.height())
                self.assertAlmostEqual(box.center().x(), 105)
                self.assertAlmostEqual(box.center().y(), 148.5)
                self.assertEqual(result[0]["image_data"], gui_item["image_data"])
                self.assertEqual(source.read_bytes(), original)
                for changes in ({"width_mm": True}, {"clip": 1}, {"tolerance": .5}, {"unknown": 1}):
                    with self.subTest(changes=changes), self.assertRaises(ValueError):
                        self.build([], "import_artwork", {"path": str(source), "tracing": changes})
        finally:
            dialog.close()

    def test_explicit_printer_selection_uses_reported_margins_and_rejects_unknown_queue(self):
        profiles = [{"name": "test queue", "paper_accepted": True, "margins_mm": [5, 6, 7, 8]}]
        with patch("verona.printing.printer_profiles", return_value=profiles):
            _, metadata, _, _, details = self.build([], "configure",
                {"registration": {"printer_name": "test queue"}})
            self.assertEqual(metadata["print_cut"]["bounds"], [5.5, 6.5, 202.5, 288.5])
            self.assertEqual(metadata["print_cut"]["printer_margins"], [5, 6, 7, 8])
            self.assertTrue(details["requires_reprint"])
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.build([], "configure", {"registration": {"printer_name": "other queue"}})

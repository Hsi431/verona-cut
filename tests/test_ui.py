import copy
import base64
import json
import math
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt, QSettings, QMarginsF
from PySide6.QtGui import QCloseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QInputDialog, QToolButton
from types import SimpleNamespace

from verona import core, commands
from verona.i18n import get_language, set_language
from verona.ui import VeronaWindow, PlotPreviewCanvas, _item_path


ROOT = Path(__file__).resolve().parents[1]


class DesignUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings_temp = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings_temp.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings_temp.cleanup()

    def setUp(self):
        QSettings("Verona", "Workspace").remove("wifi")
        set_language("zh_TW")
        self.window = VeronaWindow()

    def test_svg_multisubpath_and_curve_flattening(self):
        filename = ROOT / "examples" / "sample-design.svg"
        contours = core.load_svg(filename)
        self.assertEqual(len(contours), 2)
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(filename), "SVG")):
            self.window.import_svg()
        self.assertEqual(len(self.window.items), 1)
        self.assertEqual(len(self.window.items[0]["contours"]), 2)
        # The sample includes a circle; scaled flattening keeps the outline smooth.
        self.assertGreater(len(self.window.items[0]["contours"][1]), 100)
        self.assertTrue(all(math.isfinite(v) for contour in self.window.items[0]["contours"]
                            for point in contour for v in point))

    @staticmethod
    def _tiny_qoi():
        return (b"qoif" + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
                + bytes((4, 0, 0xff, 10, 20, 30, 255)) + b"\0\0\0\0\0\0\0\1")

    def test_native_open_save_reopen_image_and_metadata(self):
        from verona.lds import NativeDesign
        item = {"name": "native image", "contours": [[(1., 1.), (3., 1.), (3., 3.), (1., 1.)]],
                "cut": False, "source_color": 0xff000000,
                "image_data": base64.b64encode(self._tiny_qoi()).decode("ascii"),
                "image_transform": [10., 20., 2., 0., 0., 2.],
                "image_clip": [[(10., 20.), (12., 20.), (12., 22.), (10., 20.)]]}
        metadata = {"source_format": "lds", "source_name": "native.lds", "source_preview": "cHJldmlldw=="}
        with patch("verona.lds.load_lds", return_value=NativeDesign([item], metadata)), \
                patch.object(QFileDialog, "getOpenFileName", return_value=("/tmp/native.lds", "LDS")):
            self.window.open_design()
        self.assertEqual(self.window.items[0]["image_transform"], item["image_transform"])
        self.assertEqual(self.window.metadata, metadata)
        self.assertTrue(self.window.native_preview_action.isEnabled())
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "native.jdesign"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(output), "Design")) as dialog:
                self.assertTrue(self.window.save_design(save_as=True))
                self.assertEqual(Path(dialog.call_args.args[2]).name, "native.lds")
            reopened = VeronaWindow()
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(output), "Design")):
                reopened.open_design()
            self.assertEqual(reopened.items, self.window.items)
            self.assertEqual(reopened.metadata, metadata)

    def test_transform_keeps_image_transform_and_clip_aligned(self):
        image = base64.b64encode(self._tiny_qoi()).decode("ascii")
        item = {"name": "image", "contours": [[(0., 0.), (2., 0.), (2., 2.), (0., 0.)]],
                "cut": True, "image_data": image, "image_transform": [0., 0., 1., 0., 0., 1.],
                "image_clip": [[(0., 0.), (2., 0.), (2., 2.), (0., 0.)]]}
        self.window.items = [item]
        self.window.select(0)
        self.window.transform_selected(rotation=90)
        self.assertAlmostEqual(self.window.items[0]["image_transform"][0], 2.0, places=5)
        self.assertAlmostEqual(self.window.items[0]["image_transform"][1], 0.0, places=5)
        path = _item_path(self.window.items[0]).boundingRect()
        clip = _item_path({"contours": self.window.items[0]["image_clip"]}).boundingRect()
        self.assertAlmostEqual(path.left(), clip.left(), places=5)
        self.assertAlmostEqual(path.top(), clip.top(), places=5)

    def test_invalid_and_over_budget_lds_keep_unsaved_document_selection_and_undo(self):
        import zipfile
        from tests.test_lds import document, record, vector
        self.window.add_rectangle()
        self.window.select(0)
        before = copy.deepcopy((self.window._document_state(), self.window.selected_indices(),
                                self.window.undo_stack, self.window.current_file))
        line = vector([(0, 0, 16), (1, 0, 1)])
        malformed = record(1014, 3, 20) + record(0, 120, 21) + line + record(1014, 4, 20)
        with tempfile.TemporaryDirectory() as folder:
            for content in (malformed, line):
                path = Path(folder) / "invalid.lds"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("CDOC", document(content))
                with patch("verona.lds._MAX_POINTS", 1), patch.object(QMessageBox, "critical") as error:
                    self.assertFalse(self.window._load_document(str(path)))
                    error.assert_called_once()
                self.assertEqual((self.window._document_state(), self.window.selected_indices(),
                                  self.window.undo_stack, self.window.current_file), before)

    def test_json_limits_preserve_unsaved_document_selection_and_undo(self):
        from verona import input_safety
        from tests.test_input_safety import design
        self.window.add_rectangle()
        self.window.select(0)
        before = copy.deepcopy((self.window._document_state(), self.window.selected_indices(),
                                self.window.undo_stack, self.window.current_file))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"fixture.jdesign"
            path.write_text(json.dumps(design()))
            for limits in ({"MAX_DESIGN_BYTES": 4}, {"MAX_POINTS": 1}, {"MAX_DEPTH": 1}):
                with patch.multiple(input_safety, **limits), patch.object(QMessageBox, "critical") as error:
                    self.assertFalse(self.window._load_document(str(path)))
                error.assert_called_once()
                self.assertEqual((self.window._document_state(), self.window.selected_indices(),
                                  self.window.undo_stack, self.window.current_file), before)

    def test_svg_limit_failure_preserves_unsaved_document_selection_and_undo(self):
        from verona import input_safety
        self.window.add_rectangle()
        self.window.select(0)
        before = copy.deepcopy((self.window._document_state(), self.window.selected_indices(),
                                self.window.undo_stack, self.window.current_file))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"fixture.svg";path.write_text('<svg><path d="M0 0L1 1"/></svg>')
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(path), "SVG")), \
                    patch.object(input_safety, "MAX_POINTS", 1), patch.object(QMessageBox, "critical") as error:
                self.window.import_svg()
            error.assert_called_once()
            self.assertEqual((self.window._document_state(), self.window.selected_indices(),
                              self.window.undo_stack, self.window.current_file), before)

    def test_cut_false_item_is_omitted_from_plt(self):
        self.window.items = [
            {"name": "disabled", "cut": False, "contours": [[(1., 1.), (2., 2.)]]},
            {"name": "active", "cut": True, "contours": [[(3., 3.), (4., 4.)]]},
        ]
        with patch.object(commands, "encode_plt", return_value=b"ok") as encoder:
            self.assertEqual(self.window._encoded_plt(), b"ok")
        self.assertEqual(encoder.call_args.args[0], self.window.items[1]["contours"])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "enabled-only.plt"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(output), "PLT")):
                self.window.export_plt()
            self.assertEqual(output.read_bytes(), commands.encode_plt(self.window.items[1]["contours"]))
            self.assertIn("1 條輪廓", self.window.status_label.text())

    def test_cut_checkbox_toggle_is_undoable_and_registration_is_locked(self):
        self.window.add_rectangle()
        self.window.cut_checkbox.setChecked(False)
        self.assertFalse(self.window.items[0]["cut"])
        self.window.undo()
        self.assertTrue(self.window.items[0].get("cut", True))
        self.window.items[0]["registration_guide"] = True
        self.window.refresh_properties()
        self.assertFalse(self.window.cut_checkbox.isEnabled())

    def test_print_cut_state_routes_registration_and_survives_undo_and_language(self):
        self.window.items = [{"name": "cut", "cut": True,
                              "contours": [[(8., 9.), (24., 9.), (24., 33.), (8., 9.)]]}]
        self.window.metadata = {"print_cut": {"enabled": True, "bounds": [6., 6., 26., 36.],
                                              "paper_width": 210., "paper_height": 297.}}
        self.window.refresh_all()
        self.assertTrue(self.window.registration_checkbox.isEnabled())
        self.assertTrue(self.window.registration_checkbox.isChecked())
        expected = self.window._encoded_plt()
        self.assertIn(b"TB26,1200,800;", expected)
        self.window.registration_checkbox.setChecked(False)
        self.assertTrue(self.window._encoded_plt().startswith(b"IN;FSIZE"))
        self.window.undo()
        self.assertEqual(self.window._encoded_plt(), expected)
        self.window._change_language("en")
        self.assertEqual(self.window._encoded_plt(), expected)
        self.assertIn("Print & Cut", self.window._send_confirmation(expected))
        self.window.metadata["print_cut"]["bounds"] = None
        with self.assertRaises(ValueError):
            self.window._encoded_plt()

    def test_print_cut_bounds_warning_matches_send_and_ignores_backgrounds(self):
        self.window.metadata = {"print_cut": {"enabled": True, "bounds": [10., 20., 80., 95.],
                                              "paper_width": 210., "paper_height": 297.}}
        cases = [
            ([(10., 20.), (80., 95.)], (0, 0), False),
            ([(9.99, 20.), (30., 30.)], (0, 0), True),
            ([(30., 30.), (80.01, 50.)], (0, 0), True),
            ([(30., 19.99), (30., 60.)], (0, 0), True),
            ([(15., 95.01), (60., 95.01)], (0, 0), True),
            ([(10., 20.), (80., 95.)], (-.1, 0), True),
            ([(10., 20.), (80., 95.)], (0, -.1), True),
            ([(10., 20.), (80., 95.)], (.1, 0), True),
            ([(10., 20.), (80., 95.)], (0, .1), True),
            ([(11., 21.), (79., 94.)], (.1, -.1), False),
        ]
        for points, offset, outside in cases:
            with self.subTest(points=points, offset=offset):
                self.window.items = [
                    {"name": "art", "contours": [points]},
                    {"name": "background", "cut": False,
                     "contours": [[(-20., -20.), (320., 320.)]]},
                ]
                self.window.metadata["print_cut"]["offset"] = offset
                before = self.window._document_state()
                self.window.refresh_all()
                self.assertEqual(self.window._document_state(), before)
                self.assertEqual(not self.window.bounds_warning.isHidden(), outside)
                self.assertEqual(set(self.window._print_cut_violations()), {0} if outside else set())
                if outside:
                    self.assertIn("1 個物件", self.window.bounds_warning.text())
                    self.assertIn("art", self.window.bounds_warning.toolTip())
                    self.assertNotIn("background", self.window.bounds_warning.toolTip())
                    with self.assertRaises(ValueError):
                        self.window._encoded_plt()
                else:
                    self.assertIn(b"TB26", self.window._encoded_plt())

        self.window.metadata["print_cut"]["offset"] = (2, 0)
        self.window.refresh_all()
        self.window._change_language("en")
        self.assertFalse(self.window.bounds_warning.isHidden())
        self.assertIn("exceed the cutting area", self.window.bounds_warning.text())
        self.window.registration_checkbox.setChecked(False)
        self.assertTrue(self.window.bounds_warning.isHidden())
        self.assertIn(b"FSIZE", self.window._encoded_plt())

    def test_print_cut_bounds_warning_tracks_marks_cut_flag_and_history(self):
        self.window.items = [{"name": "edge", "contours": [[(8., 10.), (20., 20.)]]}]
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 210.,
                                              "paper_height": 297., "bounds": [6., 6., 204., 291.]}}
        self.window.refresh_all()
        self.assertTrue(self.window.bounds_warning.isHidden())
        self.window.mark_margins[0].setValue(12)
        self.window.apply_mark_margins()
        self.assertFalse(self.window.bounds_warning.isHidden())
        self.window.undo()
        self.assertTrue(self.window.bounds_warning.isHidden())
        self.window.redo()
        self.assertFalse(self.window.bounds_warning.isHidden())
        self.window.select(0)
        self.window.cut_checkbox.setChecked(False)
        self.assertTrue(self.window.bounds_warning.isHidden())
        self.window.cut_checkbox.setChecked(True)
        self.assertFalse(self.window.bounds_warning.isHidden())

    def test_printer_margins_maximize_marks_without_moving_art_and_can_undo(self):
        self.window.items = [{"name": "cut", "contours": [[(10., 10.), (20., 20.)]]}]
        original_items = copy.deepcopy(self.window.items)
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 210.,
                                              "paper_height": 297., "bounds": [6., 6., 204., 291.],
                                              "mark_stroke": 1.5}}
        self.window.refresh_all()
        layout = SimpleNamespace(setUnits=lambda units: None, minimumMargins=lambda: QMarginsF(5, 5, 5, 5))
        printer = SimpleNamespace(setPageSize=lambda size: None, pageLayout=lambda: layout)
        with patch("PySide6.QtPrintSupport.QPrinterInfo.availablePrinters", return_value=[object()]), \
                patch("PySide6.QtPrintSupport.QPrinter", return_value=printer):
            self.window.maximize_mark_area()
        self.assertEqual(self.window.metadata["print_cut"]["bounds"], [5.75, 5.75, 204.25, 291.25])
        self.assertEqual([spin.minimum() for spin in self.window.mark_margins], [5.75] * 4)
        self.assertEqual(self.window.items, original_items)
        self.assertIn("重新列印", self.window.status_label.text())
        self.window.undo()
        self.assertEqual(self.window.metadata["print_cut"]["bounds"], [6., 6., 204., 291.])
        self.window.mark_margins[0].setValue(8)
        self.window.apply_mark_margins()
        self.assertEqual(self.window.metadata["print_cut"]["bounds"], [8., 6., 204., 291.])
        self.window.metadata["source_format"] = "calibration"
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "calibration.pdf"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(output), "PDF")):
                self.window.export_print_pdf()
            self.assertTrue(output.read_bytes().startswith(b"%PDF"))

    def test_compact_mark_profile_maximizes_saves_and_undoes(self):
        self.window.items = [{"name": "cut", "contours": [[(10., 10.), (20., 20.)]]}]
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 210.,
            "paper_height": 297., "bounds": [6., 6., 204., 291.], "printer_margins": [5.] * 4,
            "mark_stroke": 1.5}}
        self.window.refresh_all()
        original = copy.deepcopy(self.window.metadata)
        self.window.mark_stroke.setValue(1)
        self.window.mark_short_length.setValue(4)
        self.assertEqual([spin.minimum() for spin in self.window.mark_margins], [5.5] * 4)
        layout = SimpleNamespace(setUnits=lambda units: None, minimumMargins=lambda: QMarginsF(5, 5, 5, 5))
        printer = SimpleNamespace(setPageSize=lambda size: None, pageLayout=lambda: layout)
        with patch("PySide6.QtPrintSupport.QPrinterInfo.availablePrinters", return_value=[object()]), \
                patch("PySide6.QtPrintSupport.QPrinter", return_value=printer):
            self.window.maximize_mark_area()
        profile = self.window.metadata["print_cut"]
        self.assertEqual(profile["bounds"], [5.5, 5.5, 204.5, 291.5])
        self.assertEqual((profile["mark_stroke"], profile["mark_short_length"]), (1, 4))
        expected = self.window._encoded_plt()
        with tempfile.TemporaryDirectory() as folder:
            design = Path(folder) / "compact.jdesign"
            self.window.current_file = str(design)
            self.assertTrue(self.window.save_design())
            reopened = VeronaWindow()
            reopened._load_document(str(design))
            self.assertEqual(reopened.metadata, self.window.metadata)
            self.assertEqual(reopened._encoded_plt(), expected)
            reopened._change_language("en")
            self.assertEqual(reopened.mark_stroke.value(), 1)
            self.assertEqual(reopened.mark_short_length.value(), 4)
            self.assertEqual(reopened._encoded_plt(), expected)
            invalid = json.loads(design.read_text())
            invalid["metadata"]["print_cut"]["mark_stroke"] = float("nan")
            with self.assertRaises(ValueError):
                self.window._validate_document(invalid)
        self.window.undo()
        self.assertEqual(self.window.metadata, original)
        self.assertEqual(self.window.mark_stroke.value(), 1.5)

    def test_printer_margins_leaving_no_area_do_not_replace_registration(self):
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 210.,
            "paper_height": 297., "bounds": [6., 6., 204., 291.]}}
        self.window.refresh_all()
        original = copy.deepcopy(self.window.metadata)
        layout = SimpleNamespace(setUnits=lambda units: None,
                                 minimumMargins=lambda: QMarginsF(110, 5, 110, 5))
        printer = SimpleNamespace(setPageSize=lambda size: None, pageLayout=lambda: layout)
        with patch("PySide6.QtPrintSupport.QPrinterInfo.availablePrinters", return_value=[object()]), \
                patch("PySide6.QtPrintSupport.QPrinter", return_value=printer), \
                patch.object(QMessageBox, "warning") as warning:
            self.window.maximize_mark_area()
        warning.assert_called_once()
        self.assertEqual(self.window.metadata, original)

    def test_registration_adjustment_saves_undoes_and_leaves_design_geometry(self):
        self.window.items = [{"name": "cut", "contours": [[(11., 23.), (76., 91.)]]}]
        original_items = copy.deepcopy(self.window.items)
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 210.,
            "paper_height": 297., "bounds": [10., 20., 80., 95.]}}
        self.window.refresh_all()
        original = self.window._encoded_plt()
        for spin, value in zip(self.window.registration_offsets, (-.3, .2)):
            spin.setValue(value)
        self.window.apply_registration_offset()
        adjusted = self.window._encoded_plt()
        self.assertNotEqual(adjusted, original)
        self.assertEqual(self.window.items, original_items)
        self.assertIn(b"U2872,28 D152,2628 ", adjusted)
        self.assertIn("-0.300", self.window._send_confirmation(adjusted))
        self.assertIn("+0.200", self.window._send_confirmation(adjusted))
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / "adjusted.jdesign"
            self.window.current_file = str(filename)
            self.assertTrue(self.window.save_design())
            reopened = VeronaWindow()
            reopened._load_document(str(filename))
            self.assertEqual(reopened._encoded_plt(), adjusted)
            reopened._change_language("en")
            self.assertEqual([s.value() for s in reopened.registration_offsets], [-.3, .2])
            self.assertEqual(reopened._encoded_plt(), adjusted)
            invalid = json.loads(filename.read_text())
            invalid["metadata"]["print_cut"]["offset"] = [float("nan"), 0]
            with self.assertRaises(ValueError):
                self.window._validate_document(invalid)
        self.window.undo()
        self.assertEqual(self.window._encoded_plt(), original)
        self.window.redo()
        self.assertEqual(self.window._encoded_plt(), adjusted)
        self.window.registration_checkbox.setChecked(False)
        self.assertTrue(self.window._encoded_plt().startswith(b"IN;FSIZE"))
        self.assertTrue(all(not s.isEnabled() for s in self.window.registration_offsets))

    def test_adjustment_rejects_frame_escape_without_changing_document(self):
        self.window.items = [{"name": "cut", "contours": [[(10., 20.), (80., 95.)]]}]
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 210.,
            "paper_height": 297., "bounds": [10., 20., 80., 95.]}}
        self.window.refresh_all()
        before = copy.deepcopy(self.window.metadata)
        self.window.registration_offsets[0].setValue(-.3)
        with patch.object(QMessageBox, "warning") as warning:
            self.window.apply_registration_offset()
        warning.assert_called_once()
        self.assertEqual(self.window.metadata, before)

    def test_design_roundtrip_settings_text_and_real_plt_export(self):
        svg = ROOT / "examples" / "sample-design.svg"
        with patch.object(QFileDialog, "getOpenFileName", return_value=(str(svg), "SVG")):
            self.window.import_svg()
        with patch.object(QInputDialog, "getText", return_value=("Juliet", True)):
            self.window.add_text()
        self.assertGreater(len(self.window.items[1]["contours"]), 1)
        self.window.custom_settings.setChecked(True)
        self.window.speed_spin.setValue(8)
        self.window.force_spin.setValue(12)
        self.window.tool_combo.setCurrentIndex(self.window.tool_combo.findData(1))
        self.window.wifi_host.setText("192.0.2.8")
        self.window.wifi_port.setValue(9101)

        with tempfile.TemporaryDirectory() as folder:
            design = Path(folder) / "roundtrip.jdesign"
            plt = Path(folder) / "roundtrip.plt"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(design), "Design")):
                self.window.save_design()
            reopened = VeronaWindow()
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(design), "Design")):
                reopened.open_design()
            self.assertEqual(reopened.items, self.window.items)
            self.assertEqual(reopened._cut_settings(), self.window._cut_settings())

            expected = commands.encode_plt(
                [contour for item in reopened.items for contour in item["contours"]],
                force=12, speed=8, tool=1,
            )
            self.assertTrue(expected.startswith(b"IN;FSIZE"))
            self.assertIn(b"SP2;!FS12;VS8;", expected)
            preview = PlotPreviewCanvas(expected)
            self.assertTrue(any(mode == "D" for mode, _ in preview.moves))
            self.assertTrue(any(mode == "U" for mode, _ in preview.moves))
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(plt), "PLT")):
                reopened.export_plt()
            self.assertEqual(plt.read_bytes(), expected)

    def test_pen_calibration_design_exports_the_reference_golden_plt(self):
        design = ROOT / "examples" / "pen-calibration.jdesign"
        expected = (ROOT / "examples" / "pen-calibration.plt").read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "pen-calibration.plt"
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(design), "Design")):
                self.window.open_design()
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(output), "PLT")):
                self.window.export_plt()
            self.assertEqual(output.read_bytes(), expected)

    def test_invalid_design_is_rejected_without_replacing_open_scene(self):
        self.window.add_rectangle()
        before = self.window._snapshot()
        invalid = {
            "format": "verona", "version": 1, "units": "mm",
            "items": [{"name": "bad", "contours": [[[1, 2], [float("nan"), 3]]]}],
            "settings": {"custom": False, "speed": 5, "force": 10, "tool": 0},
        }
        with tempfile.TemporaryDirectory() as folder:
            design = Path(folder) / "invalid.jdesign"
            design.write_text(json.dumps(invalid), encoding="utf-8")
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(design), "Design")), \
                    patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"), \
                    patch.object(QMessageBox, "critical"):
                self.window.open_design()
        self.assertEqual(self.window._snapshot(), before)

    def test_delete_duplicate_new_and_undo(self):
        self.window.add_ellipse()
        self.window.duplicate_selected()
        self.assertEqual(len(self.window.items), 2)
        self.window.delete_selected()
        self.assertEqual(len(self.window.items), 1)
        self.window.undo()
        self.assertEqual(len(self.window.items), 2)
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.new_design()
        self.assertEqual(self.window.items, [])
        self.window.undo()
        self.assertEqual(self.window.items, [])
        self.window.tab_bar.setCurrentIndex(0)
        self.assertEqual(len(self.window.items), 2)
        self.window.undo()
        self.assertEqual(len(self.window.items), 1)
        self.window.undo()
        self.assertEqual(self.window.items, [])

    def test_unsaved_save_cancel_preserves_tabs_and_close_work(self):
        self.window.add_rectangle()
        before = self.window._document_state()
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="save"), \
                patch.object(QFileDialog, "getSaveFileName", return_value=("", "")):
            self.window.new_design()
            with patch.object(QFileDialog, "getOpenFileName", return_value=("", "")) as open_dialog:
                self.window.open_design()
                open_dialog.assert_called_once()
            event = QCloseEvent()
            self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(self.window.tab_bar.count(), 2)
        self.window.tab_bar.setCurrentIndex(0)
        self.assertEqual(self.window._document_state(), before)

        with tempfile.TemporaryDirectory() as folder:
            # A missing parent directory fails the write regardless of how it is done.
            self.window.current_file = str(Path(folder) / "missing" / "unwritable.jdesign")
            with patch.object(self.window, "_confirm_unsaved_changes", return_value="save"), \
                    patch.object(QMessageBox, "critical") as critical:
                self.assertFalse(self.window.close_document(0))
            critical.assert_called_once()
        self.assertEqual(self.window._document_state(), before)
        self.assertTrue(self.window.is_dirty())

    def test_saved_baseline_stays_clean_after_language_switch_and_avoids_prompt(self):
        self.window.add_rectangle()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(QFileDialog, "getSaveFileName",
                             return_value=(str(Path(folder) / "saved.jdesign"), "Design")):
            self.assertTrue(self.window.save_design())
        self.assertFalse(self.window.is_dirty())
        self.window._change_language("en")
        self.assertFalse(self.window.is_dirty())
        with patch.object(self.window, "_confirm_unsaved_changes") as confirm:
            self.window.new_design()
            confirm.assert_not_called()

    def test_canvas_drag_undo_and_position_controls(self):
        self.window.add_rectangle()
        self.window.width_spin.setValue(80)
        self.assertAlmostEqual(self.window.height_spin.value(), 50)
        self.window.x_spin.setValue(17)
        self.window.y_spin.setValue(21)
        self.window.apply_size()
        bounds = _item_path(self.window.items[0]).boundingRect()
        self.assertAlmostEqual(bounds.left(), 17)
        self.assertAlmostEqual(bounds.top(), 21)
        self.assertAlmostEqual(bounds.width(), 80)
        self.assertAlmostEqual(bounds.height(), 50)
        self.window.undo()
        canvas = self.window.canvas
        before = _item_path(self.window.items[0]).boundingRect().left()
        zoom = canvas.zoom
        point = QPoint(round(150 * zoom + 34), round(150 * zoom + 34))
        end = QPoint(point.x() + round(10 * zoom), point.y())
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(canvas, end, delay=10)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
        moved = _item_path(self.window.items[0]).boundingRect().left()
        self.assertAlmostEqual(moved - before, 10, delta=0.5)
        self.window.undo()
        self.assertAlmostEqual(_item_path(self.window.items[0]).boundingRect().left(), before)

    def _wait_for_tasks(self):
        deadline = time.monotonic() + 3
        while self.window._tasks and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(10)
        self.app.processEvents()
        self.assertFalse(self.window._tasks, "background UI task did not finish")

    def test_wifi_send_uses_worker_confirmation_and_no_real_socket(self):
        self.window.add_rectangle()
        self.window.wifi_host.setText("192.0.2.44")
        payload = self.window._encoded_plt()
        with patch("verona.transport.send_tcp", return_value=len(payload)) as send, \
                patch.object(self.window, "_confirm_send", return_value=True) as confirm, \
                patch.object(QMessageBox, "information"):
            self.window.send_to_cutter()
            self.assertFalse(self.window.send_button.isEnabled())
            self._wait_for_tasks()
            send.assert_called_once_with(payload, "192.0.2.44", 8080, on_progress=ANY)
            self.assertTrue(callable(send.call_args.kwargs["on_progress"]))
            self.assertIn("X 130.00", confirm.call_args.args[0])
            self.assertTrue(self.window.send_button.isEnabled())
            self.assertIn("bytes", self.window.status_label.text())
            self.assertEqual(QSettings("Verona", "Workspace").value("wifi/host"), "192.0.2.44")
            reopened = VeronaWindow()
            self.assertEqual(reopened.wifi_host.text(), "192.0.2.44")
            self.assertEqual(reopened.wifi_port.value(), 8080)

    def test_wifi_check_is_async_and_never_sends_plt(self):
        self.window.wifi_host.setText("192.0.2.56")
        with patch("verona.transport.query_tcp_version", return_value="V22.0613") as query, \
                patch("verona.transport.send_tcp") as send:
            self.window.check_wifi_connection()
            self.assertFalse(self.window.wifi_check_button.isEnabled())
            self._wait_for_tasks()
            query.assert_called_once_with("192.0.2.56", 8080)
            send.assert_not_called()
            self.assertIn("V22.0613", self.window.status_label.text())
            self.assertEqual(QSettings("Verona", "Workspace").value("wifi/host"), "192.0.2.56")

    def test_document_endpoint_overrides_saved_wifi_preference(self):
        from PySide6.QtCore import QSettings
        settings = QSettings("Verona", "Workspace")
        settings.setValue("wifi/host", "192.0.2.77")
        settings.setValue("wifi/port", 8123)
        data = {"format": "verona", "version": 1, "units": "mm", "items": [],
                "settings": {"connection": "wifi", "host": "192.0.2.88", "port": 9001}}
        with tempfile.TemporaryDirectory() as folder:
            design = Path(folder) / "doc.jdesign"
            design.write_text(json.dumps(data), encoding="utf-8")
            with patch.object(QFileDialog, "getOpenFileName", return_value=(str(design), "Design")):
                self.window.open_design()
        self.assertEqual(self.window.wifi_host.text(), "192.0.2.88")
        self.assertEqual(self.window.wifi_port.value(), 9001)

    def test_invalid_document_host_reports_a_message(self):
        for host in ("0.0.0.0", "224.0.0.1"):
            with self.subTest(host=host), self.assertRaises(ValueError) as caught:
                VeronaWindow._validate_document({"format": "verona", "version": 1,
                                                "units": "mm", "items": [], "settings": {"host": host}})
            self.assertEqual(str(caught.exception), "設計檔中的連線 IP 無效。")

    def test_gui_send_is_blocked_by_cli_and_mcp_locks(self):
        from verona.cli import device_lock as cli_device_lock
        from verona.mcp_server import VeronaController

        self.window.add_rectangle()
        self.window.wifi_host.setText("192.0.2.1")
        self.window.usb_devices = [SimpleNamespace(bus=1, address=2)]
        self.window.usb_combo.addItem("Mock Juliet", 0)
        self.window.usb_combo.setCurrentIndex(self.window.usb_combo.findData(0))
        with tempfile.TemporaryDirectory() as state, patch.dict(os.environ, {"XDG_STATE_HOME": state}):
            controller = VeronaController()
            for owner in (cli_device_lock, controller.device_lock):
                for mode in ("wifi", "usb"):
                    with self.subTest(owner=owner.__name__, mode=mode), owner(), \
                            patch("verona.transport.send_tcp") as tcp, \
                            patch("verona.transport.send_usb") as usb, \
                            patch.object(self.window, "_confirm_send", return_value=True), \
                            patch.object(QMessageBox, "information"), \
                            patch.object(QMessageBox, "warning") as warning:
                        self.window.connection_mode.setCurrentIndex(self.window.connection_mode.findData(mode))
                        self.window.send_to_cutter()
                        self._wait_for_tasks()
                        tcp.assert_not_called()
                        usb.assert_not_called()
                        warning.assert_called_once()
                        self.assertIn("device", warning.call_args.args[2])
                        self.assertFalse(self.window.sending)
                        self.assertTrue(self.window.send_button.isEnabled())

    def test_gui_version_query_is_blocked_by_cli_and_mcp_locks(self):
        from verona.cli import device_lock as cli_device_lock
        from verona.mcp_server import VeronaController

        self.window.wifi_host.setText("192.0.2.1")
        with tempfile.TemporaryDirectory() as state, patch.dict(os.environ, {"XDG_STATE_HOME": state}):
            controller = VeronaController()
            for owner in (cli_device_lock, controller.device_lock):
                with self.subTest(owner=owner.__name__), owner(), \
                        patch("verona.transport.query_tcp_version", return_value="V22.0613") as query, \
                        patch.object(QMessageBox, "warning") as warning:
                    self.window.check_wifi_connection()
                    self._wait_for_tasks()
                    query.assert_not_called()
                    warning.assert_called_once()
                    self.assertIn("device", warning.call_args.args[2])
                    self.assertTrue(self.window.wifi_check_button.isEnabled())

    def test_usb_refresh_and_send_are_mocked(self):
        device = SimpleNamespace(bus=2, address=7)
        self.window.add_rectangle()
        self.window.connection_mode.setCurrentIndex(self.window.connection_mode.findData("usb"))
        with patch("verona.transport.usb_devices", return_value=[device]) as enumerate_usb:
            self.window.refresh_usb_devices()
            self._wait_for_tasks()
        enumerate_usb.assert_called_once_with()
        self.assertIs(self.window.usb_devices[0], device)
        payload = self.window._encoded_plt()
        with patch("verona.transport.send_usb", return_value=len(payload)) as send, \
                patch.object(self.window, "_confirm_send", return_value=True), \
                patch.object(QMessageBox, "information"):
            self.window.send_to_cutter()
            self._wait_for_tasks()
            send.assert_called_once_with(payload, device)

    def test_transport_failure_warns_about_partial_data_and_restores_button(self):
        self.window.add_rectangle()
        with patch("verona.transport.send_tcp", side_effect=OSError("connection reset")), \
                patch.object(self.window, "_confirm_send", return_value=True), \
                patch.object(QMessageBox, "warning") as warning:
            self.window.wifi_host.setText("192.0.2.9")
            self.window.send_to_cutter()
            self._wait_for_tasks()
            self.assertTrue(self.window.send_button.isEnabled())
            self.assertIn("傳送失敗", self.window.status_label.text())
            self.assertIn("部分資料送出", warning.call_args.args[2])

    def test_bilingual_switch_preserves_document_settings_history_and_selection(self):
        from PySide6.QtCore import QSettings
        self.window.add_rectangle()
        self.window.current_file = "/tmp/work.jdesign"
        self.window.wifi_host.setText("192.0.2.2")
        self.window.usb_devices = [SimpleNamespace(bus=1, address=4)]
        self.window.usb_combo.addItem("Juliet USB 1", 0)
        self.window.connection_mode.setCurrentIndex(self.window.connection_mode.findData("usb"))
        self.window.custom_settings.setChecked(True)
        self.window.speed_spin.setValue(7)
        self.window.selected = 0
        self.window.change_zoom(1.1)
        before = self.window._snapshot()
        undo = copy.deepcopy(self.window.undo_stack)
        saved_pref = QSettings("Verona", "Workspace").value("language")
        try:
            self.window._change_language("en")
            self.assertEqual(get_language(), "en")
            self.assertEqual(self.window.items, before["items"])
            self.assertEqual(self.window._cut_settings(), before["settings"])
            self.assertEqual(self.window.current_file, before["file"])
            self.assertEqual(self.window.selected, before["selected"])
            self.assertEqual(self.window.usb_combo.currentData(), 0)
            self.assertAlmostEqual(self.window.canvas.zoom, 1.8 * 1.1)
            self.assertEqual(self.window.undo_stack, undo)
            self.assertEqual(self.window.send_button.text(), "Send to Juliet…")
            self.assertIn("Workspace 300 × 300 mm", self.window.status_label.text())
            self.assertIn("does not match", __import__("verona.i18n", fromlist=["tr"]).tr(
                "所選 USB 裝置不符合支援的切割機識別碼"))
            self.window._change_language("zh_TW")
            self.assertEqual(self.window.send_button.text(), "傳送至 Juliet…")
            self.assertIn("工作區 300 × 300 mm", self.window.status_label.text())
        finally:
            if saved_pref is None:
                QSettings("Verona", "Workspace").remove("language")
            else:
                QSettings("Verona", "Workspace").setValue("language", saved_pref)

    def test_compact_height_keeps_toolbar_and_scrolls_to_output(self):
        for language in ("en", "zh_TW"):
            self.window._change_language(language)
            for width, height in ((1280, 720), (1366, 768)):
                self.window.resize(width, height)
                self.window.show()
                self.app.processEvents()
                self.assertTrue(self.window.inspector_scroll.widgetResizable())
                self.window.inspector_scroll.ensureWidgetVisible(self.window.send_button)
                self.window.inspector_scroll.verticalScrollBar().setValue(
                    self.window.inspector_scroll.verticalScrollBar().maximum())
                self.app.processEvents()
                button_rect = self.window.send_button.rect()
                button_top_left = self.window.send_button.mapTo(self.window.inspector_scroll.viewport(), button_rect.topLeft())
                self.assertGreaterEqual(button_top_left.y(), 0)
                self.assertLessEqual(button_top_left.y() + button_rect.height(),
                                     self.window.inspector_scroll.viewport().height())
                self.assertTrue(self.window.send_button.isVisible())
                self.assertTrue(self.window.output_button.isVisible())
                preview = next(button for button in self.window.findChildren(QToolButton)
                               if button.text() == ("Cut Preview" if language == "en" else "切割預覽"))
                preview_bottom_right = preview.mapTo(self.window, preview.rect().bottomRight())
                self.assertLessEqual(preview_bottom_right.x(), self.window.width())
        self.window.hide()

    def test_failed_save_keeps_existing_design_file_and_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "keep.jdesign"
            path.write_text("original")
            os.chmod(path, 0o640)
            self.window.current_file = str(path)
            self.window.metadata = {"unserializable": object()}
            with patch.object(QMessageBox, "critical") as critical:
                self.assertFalse(self.window.save_design())
            critical.assert_called_once()
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(os.listdir(folder), ["keep.jdesign"])
            self.window.metadata = {}
            self.assertTrue(self.window.save_design())
            self.assertEqual(json.loads(path.read_text())["format"], "verona")
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(os.listdir(folder), ["keep.jdesign"])


if __name__ == "__main__":
    unittest.main()

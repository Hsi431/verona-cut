"""Paper selection, isolated CUPS options and ICC print rendering regressions."""
import copy
import hashlib
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QSizeF
from PySide6.QtGui import QPageLayout, QPageSize
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QApplication

from verona import print_dialog
from verona.print_dialog import PrintDialog
from verona.print_dialog import icc_profile_settings_key, validate_printer_settings
from verona.i18n import tr
from verona.printing import _draw_print_page
from verona.ui import VeronaWindow
from tests.test_editor_actions import rectangle


class PrintPaperIccTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cups_options_parse_media_codes_and_match_design_page(self):
        self.assertIsNotNone(importlib.util.find_spec("verona.printer_options"))
        from verona.printer_options import display_choice, page_size_choice, parse_lpoptions

        options = parse_lpoptions(
            "PageSize/Media Size: *A4 A5 A6 B5 Letter 4X6FULL 5X7\n"
            "MediaType/Print Quality: *PLAIN_NORMAL PMMATT_HIGH PMPHOTO_HIGH PSGLOS_HIGH\n"
            "Ink/Ink: *COLOR MONO\n"
            "cupsPrintQuality/CUPS Print Quality: *Normal High\n"
            "Brightness/Brightness: -50 0 50\n"
        )
        self.assertEqual(options.get("MediaType", {}).get("choices"),
                         ["PLAIN_NORMAL", "PMMATT_HIGH", "PMPHOTO_HIGH", "PSGLOS_HIGH"])
        self.assertEqual(display_choice("PLAIN_NORMAL"), f"{tr('普通紙')} · {tr('標準')}")
        self.assertEqual(display_choice("PMMATT_HIGH"), f"{tr('雪面銅版紙')} · {tr('高品質')}")
        self.assertEqual(display_choice("DRIVER_UNKNOWN"), "DRIVER_UNKNOWN")
        self.assertEqual(page_size_choice(options, 210, 297), ("PageSize", "A4"))
        self.assertEqual(page_size_choice(options, 101.6, 152.4), ("PageSize", "4X6FULL"))
        self.assertEqual(page_size_choice(parse_lpoptions("PageSize/Size: A4 Custom\n"), 123, 234),
                         ("PageSize", "Custom.123x234mm"))
        self.assertEqual(page_size_choice(parse_lpoptions(
            "PageSize/Media Size: *A4 Custom.WIDTHxHEIGHT\n"), 234, 123),
            ("PageSize", "Custom.123x234mm"))

    def test_missing_cups_reader_returns_fallback_without_crashing(self):
        self.assertIsNotNone(importlib.util.find_spec("verona.printer_options"))
        from verona.printer_options import parse_lpoptions, read_printer_options

        with patch("verona.printer_options.subprocess.run", side_effect=FileNotFoundError("lpoptions")):
            self.assertIsNone(read_printer_options("queue"))
        with patch("verona.printer_options.subprocess.run", return_value=type(
                "Result", (), {"stdout": "PageSize/Media Size: *A4 Letter\n"})()) as run:
            self.assertEqual(read_printer_options("queue"), parse_lpoptions("PageSize/Media Size: *A4 Letter\n"))
        self.assertEqual(run.call_args.args[0], ["lpoptions", "-p", "queue", "-l"])
        with patch("verona.printer_options.subprocess.run", return_value=type(
                "Result", (), {"stdout": "unrecognized output"})()):
            self.assertIsNone(read_printer_options("queue"))
        self.assertTrue(hasattr(print_dialog, "_MEDIA_OPTIONS"))
        self.assertTrue({"Ink", "Brightness", "Contrast", "Saturation", "cupsPrintQuality"}
                        <= print_dialog._MEDIA_OPTIONS)

    def test_driver_option_whitelist_round_trips_without_global_queue_writes(self):
        settings = {"printer_name": "queue", "cups_options": [
            "MediaType", "PMMATT_HIGH", "Ink", "MONO", "Brightness", "10",
            "Contrast", "5", "Saturation", "0", "cupsPrintQuality", "5"]}
        self.assertEqual(validate_printer_settings(settings), settings)
        self.assertEqual(validate_printer_settings(copy.deepcopy(settings))["cups_options"],
                         settings["cups_options"])

    def test_paper_controls_change_save_restore_export_and_undo_without_moving_marks(self):
        window = VeronaWindow()
        self.addCleanup(window.deleteLater)
        window.items = [rectangle(x=20, y=20, width=12, height=12,
                                  print_style={"fill": "#000000", "stroke": None, "width_mm": .2})]
        window.metadata = {"print_cut": {"enabled": True, "paper_width": 210., "paper_height": 297.,
                                           "bounds": [6., 6., 204., 291.]}}
        saved_options = ["MediaType", "PMMATT_HIGH", "Ink", "MONO", "Brightness", "10",
                         "Contrast", "5", "Saturation", "0", "cupsPrintQuality", "5"]
        window.metadata["printer_settings"] = {"printer_name": "queue", "copies": 2,
                                                "cups_options": saved_options}
        window._sync_registration_controls()
        original_bounds = copy.deepcopy(window.metadata["print_cut"]["bounds"])
        self.assertIsNotNone(getattr(window, "paper_size_combo", None))
        self.assertEqual([window.paper_size_combo.itemData(index)
                          for index in range(window.paper_size_combo.count())],
                         ["a4", "a5", "a6", "b5", "letter", "4x6", "5x7", "custom"])

        window.paper_size_combo.setCurrentIndex(window.paper_size_combo.findData("a5"))
        window.paper_orientation_combo.setCurrentIndex(
            window.paper_orientation_combo.findData("landscape"))
        self.assertEqual((window.metadata["print_cut"]["paper_width"],
                          window.metadata["print_cut"]["paper_height"]), (210., 148.))
        self.assertEqual(window.metadata["print_cut"]["bounds"], original_bounds)
        self.assertFalse(window.paper_warning.isHidden())
        self.assertTrue(any(text in window.paper_warning.text()
                            for text in ("定位標記超出", "Registration marks exceed")))
        from verona.printing import print_page_size
        with self.assertRaises(ValueError) as error:
            print_page_size(window.items, window.metadata)
        self.assertTrue(any(text in str(error.exception)
                            for text in ("定位標記超出", "Registration marks exceed")))

        with tempfile.TemporaryDirectory() as folder:
            jdesign = Path(folder) / "paper.jdesign"
            lds = Path(folder) / "paper.lds"
            window._write_document(str(jdesign))
            reopened = VeronaWindow()
            self.addCleanup(reopened.deleteLater)
            self.assertTrue(reopened.open_path(str(jdesign)))
            self.assertEqual(reopened.metadata["print_cut"]["paper_width"], 210.)
            self.assertEqual(reopened.metadata["print_cut"]["paper_height"], 148.)
            self.assertEqual(reopened.metadata["printer_settings"]["cups_options"], saved_options)
            window._write_document(str(lds))
            from verona.lds import load_lds
            native = load_lds(lds)
            self.assertEqual(native.metadata["print_cut"]["paper_width"], 210.)
            self.assertEqual(native.metadata["print_cut"]["paper_height"], 148.)
            self.assertEqual(native.metadata["printer_settings"]["cups_options"], saved_options)
            native_only = Path(folder) / "paper-native.lds"
            with zipfile.ZipFile(lds) as source, zipfile.ZipFile(native_only, "w", zipfile.ZIP_DEFLATED) as target:
                for name in source.namelist():
                    if name != "VERONA.json":
                        target.writestr(name, source.read(name))
            exported = load_lds(native_only)
            self.assertEqual(exported.metadata["print_cut"]["paper_width"], 210.)
            self.assertEqual(exported.metadata["print_cut"]["paper_height"], 148.)

        window.undo()
        self.assertEqual((window.metadata["print_cut"]["paper_width"],
                          window.metadata["print_cut"]["paper_height"]), (148., 210.))
        window.undo()
        self.assertEqual((window.metadata["print_cut"]["paper_width"],
                          window.metadata["print_cut"]["paper_height"]), (210., 297.))
        self.assertEqual(window.metadata["print_cut"]["bounds"], original_bounds)

    def test_icc_render_keeps_black_registration_pixels_and_unprofiled_pixels_exact(self):
        profile = Path("/usr/share/color/icc/colord/SWOP_TR005_coated_5.icc")
        self.assertTrue(profile.is_file())
        self.assertIn("color_profile_path", __import__("inspect").signature(_draw_print_page).parameters)
        items = [rectangle(x=40, y=40, width=12, height=12,
                           print_style={"fill": "#2277cc", "stroke": None, "width_mm": .2})]
        metadata = {"print_cut": {"enabled": True, "paper_width": 100., "paper_height": 100.,
                                  "bounds": [20., 20., 80., 80.]}}

        def render(**kwargs):
            image = QImage(378, 378, QImage.Format.Format_RGB32)
            image.fill(QColor("white"))
            painter = QPainter(image)
            try:
                _draw_print_page(painter, items, metadata, **kwargs)
            finally:
                painter.end()
            return image

        baseline = render()
        repeated = render()
        profiled = render(color_profile_path=str(profile), rendering_intent="relative", preview=True)
        device_profiled = render(color_profile_path=str(profile), rendering_intent="perceptual")
        self.assertEqual(bytes(baseline.constBits()), bytes(repeated.constBits()))
        self.assertEqual(hashlib.sha256(bytes(baseline.constBits())).hexdigest(),
                         "ab2a4e55d449fa3a5dfe6dce79bc632001041751769b8ea73880bcdd360719ea")
        self.assertEqual(baseline.pixelColor(301, 75), QColor("black"))
        self.assertEqual(profiled.pixelColor(301, 75), QColor("black"))
        self.assertEqual(device_profiled.pixelColor(301, 75), QColor("black"))
        for x, y in ((301, 75), (75, 301), (300, 300), (75, 75)):
            self.assertEqual(baseline.pixelColor(x, y), profiled.pixelColor(x, y))
            self.assertEqual(baseline.pixelColor(x, y), device_profiled.pixelColor(x, y))
        for x, y in ((20, 20), (80, 20), (20, 80), (80, 80)):
            left, top = round((x - 3) * 96 / 25.4), round((y - 3) * 96 / 25.4)
            crop = (baseline.copy(left, top, 24, 24), profiled.copy(left, top, 24, 24),
                    device_profiled.copy(left, top, 24, 24))
            self.assertEqual(bytes(crop[0].constBits()), bytes(crop[1].constBits()))
            self.assertEqual(bytes(crop[0].constBits()), bytes(crop[2].constBits()))
        self.assertNotEqual(baseline.pixelColor(168, 168), profiled.pixelColor(168, 168))

    def test_print_dialog_options_failure_leaves_native_settings_fallback(self):
        self.assertIsNotNone(importlib.util.find_spec("verona.printer_options"))
        with patch("verona.print_dialog.read_printer_options", return_value=None):
            from PySide6.QtPrintSupport import QPrinter
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            dialog = PrintDialog(printer, [rectangle()], {"print_cut": {
                "enabled": True, "paper_width": 210., "paper_height": 297.,
                "bounds": [6., 6., 204., 291.]}})
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.options_available)
        self.assertTrue(dialog.more_settings_button.isEnabled())
        self.assertFalse(dialog.driver_controls.isVisible())

    def test_print_dialog_applies_driver_choices_only_to_current_printer_job(self):
        from verona.printer_options import parse_lpoptions

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setPageSize(QPageSize(QSizeF(210, 297), QPageSize.Unit.Millimeter))
        options = parse_lpoptions(
            "PageSize/Media Size: *A5 A4 Letter\n"
            "MediaType/Print Quality: *PLAIN_NORMAL PMMATT_HIGH\n"
            "Ink/Ink: *COLOR MONO\n"
            "cupsPrintQuality/CUPS Print Quality: *Normal High\n"
            "Brightness/Brightness: *0 10\n"
            "Contrast/Contrast: *0 10\n"
            "Saturation/Saturation: *0 10\n")
        engine = printer.printEngine()
        with patch("verona.print_dialog.read_printer_options", return_value=options), \
                patch.object(engine, "property", return_value=["PageSize", "A5", "MediaType", "PLAIN_NORMAL",
                                                                "Ink", "COLOR"]), \
                patch.object(engine, "setProperty") as set_property:
            dialog = PrintDialog(printer, [rectangle()], {"print_cut": {
                "enabled": True, "paper_width": 210., "paper_height": 148.,
                "bounds": [6., 6., 204., 142.]}})
            self.addCleanup(dialog.deleteLater)
            self.assertTrue(dialog.options_available)
            self.assertFalse(dialog.paper_error.text())
            paper = printer.pageLayout().fullRect(QPageLayout.Unit.Millimeter)
            self.assertAlmostEqual(paper.width(), 210, delta=.1)
            self.assertAlmostEqual(paper.height(), 148, delta=.1)
            self.assertEqual(dialog.quality_combo.count(), 2)
            self.assertEqual(dialog.advanced_form.rowCount(), 3)
            self.assertTrue(dialog.advanced_options.isHidden())
            dialog.advanced_toggle.click()
            self.assertFalse(dialog.advanced_options.isHidden())
            dialog.media_combo.setCurrentIndex(dialog.media_combo.findData("PMMATT_HIGH"))
            dialog.color_combo.setCurrentIndex(dialog.color_combo.findData("MONO"))
            dialog.copies_spin.setValue(3)
        calls = [call.args for call in set_property.call_args_list]
        self.assertIn((print_dialog._CUPS_OPTIONS, ["PageSize", "A5", "MediaType", "PMMATT_HIGH",
                                                     "Ink", "COLOR"]), calls)
        self.assertIn((print_dialog._CUPS_OPTIONS, ["PageSize", "A5", "MediaType", "PLAIN_NORMAL",
                                                     "Ink", "MONO"]), calls)
        self.assertEqual(printer.copyCount(), 3)

    def test_icc_profile_preference_is_local_and_scoped_to_printer_and_media(self):
        profile = "/usr/share/color/icc/colord/SWOP_TR005_coated_5.icc"
        self.assertTrue(Path(profile).is_file())
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        with patch("verona.print_dialog.read_printer_options", return_value=None):
            dialog = PrintDialog(printer, [rectangle()], {"print_cut": {
                "enabled": True, "paper_width": 210., "paper_height": 297.,
                "bounds": [6., 6., 204., 291.]}})
        self.addCleanup(dialog.deleteLater)
        with tempfile.TemporaryDirectory() as folder:
            QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, folder)
            settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                                 "Verona", "Workspace")
            dialog._select_profile(profile)
            dialog.save_color_preferences(settings)
            key = icc_profile_settings_key(printer.printerName(), "")
            self.assertEqual(settings.value(key + "/profile"), profile)
            self.assertNotIn(profile, str(dialog.metadata))
            self.assertNotEqual(key, icc_profile_settings_key(printer.printerName(), "Glossy"))


if __name__ == "__main__":
    unittest.main()

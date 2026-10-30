"""Saved media preferences and safe preview, without sending physical jobs."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QSizeF
from PySide6.QtGui import QPageSize
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton

from verona.print_dialog import (
    PrintDialog, _CUPS_OPTIONS, apply_printer_settings, printer_settings, validate_printer_settings,
)
from verona.ui import VeronaWindow
from tests.test_editor_actions import rectangle


class PrintSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, self.folder.name)
        self.items = [rectangle(x=20, y=20, print_style={"fill": "#0044cc", "stroke": None, "width_mm": .2})]
        self.metadata = {"print_cut": {"enabled": True, "paper_width": 210., "paper_height": 297.,
                                     "bounds": [6., 6., 204., 291.]}}

    def printer(self):
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(Path(self.folder.name) / "must-not-print.pdf"))
        printer.setPageSize(QPageSize(QSizeF(210, 297), QPageSize.Unit.Millimeter))
        printer.setFullPage(True)
        return printer

    def test_public_printer_preferences_round_trip_and_skip_other_printers(self):
        source = self.printer()
        source.setResolution(600)
        source.setColorMode(QPrinter.ColorMode.GrayScale)
        source.setCopyCount(2)
        saved = printer_settings(source)
        restored = self.printer()
        apply_printer_settings(restored, saved)
        self.assertEqual(printer_settings(restored), saved)
        before = printer_settings(restored)
        apply_printer_settings(restored, {**saved, "printer_name": "Unavailable", "copies": 3})
        self.assertEqual(printer_settings(restored), before)

    def test_driver_media_and_quality_options_are_captured_and_restored(self):
        source = self.printer()
        options = ["MediaType", "Glossy", "print-quality", "5", "number-up", "2"]
        with patch.object(source.printEngine(), "property", return_value=options):
            saved = printer_settings(source)
        self.assertEqual(saved["cups_options"], options[:4])
        with patch.object(source.printEngine(), "setProperty") as setter:
            apply_printer_settings(source, saved)
        setter.assert_called_with(_CUPS_OPTIONS, options[:4])

    def test_preview_draws_without_creating_a_print_file_and_blocks_wrong_paper(self):
        printer = self.printer()
        dialog = PrintDialog(printer, self.items, self.metadata)
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.app.processEvents()
        self.assertFalse(Path(printer.outputFileName()).exists())
        self.assertEqual(dialog.preview.pageCount(), 1)
        self.assertTrue(dialog.print_button.isEnabled())
        self.assertFalse(dialog.error.text())
        self.assertIn("210", dialog.summary["paper"].text())
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
        dialog.preview.updatePreview()
        self.assertFalse(dialog.print_button.isEnabled())
        self.assertTrue(dialog.error.text())
        self.assertFalse(Path(printer.outputFileName()).exists())
        dialog.hide()

    def test_print_dialog_keeps_scale_warning_without_duplicate_intro(self):
        dialog = PrintDialog(self.printer(), self.items, self.metadata)
        self.addCleanup(dialog.deleteLater)
        texts = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertTrue(any(text in texts for text in ("100% 原尺寸 · 不縮放",
                                                        "100% actual size · No scaling")))
        self.assertNotIn("Print preview", texts)
        self.assertNotIn("Check the paper and artwork before printing. Settings are saved with the design.", texts)
        settings = next(button for button in dialog.findChildren(QPushButton)
                        if button.text() in ("印表機與紙材設定…", "Printer and media settings…"))
        self.assertIn(settings.toolTip(), ("設定會隨目前設計儲存。",
                                          "Settings are saved with the current design."))

    def test_native_settings_accept_and_cancel_never_submit_jobs(self):
        printer = self.printer()
        dialog = PrintDialog(printer, self.items, self.metadata)
        self.addCleanup(dialog.deleteLater)
        for result in (QDialog.DialogCode.Accepted, QDialog.DialogCode.Rejected):
            with patch("verona.print_dialog.QPrintDialog") as native, \
                    patch("verona.printing.print_document") as submit:
                native.return_value.exec.return_value = result
                dialog._configure()
                submit.assert_not_called()
        self.assertFalse(Path(printer.outputFileName()).exists())

    def test_paper_cut_settings_and_media_survive_both_formats_and_tab_switches(self):
        window = VeronaWindow()
        self.addCleanup(window.deleteLater)
        window.items = copy.deepcopy(self.items)
        window.metadata = copy.deepcopy(self.metadata)
        saved = {**printer_settings(self.printer()), "printer_name": "Studio printer",
                 "cups_options": ["MediaType", "Glossy", "print-quality", "5"]}
        window.metadata["printer_settings"] = saved
        window.passes_spin.setValue(4)
        window.custom_settings.setChecked(True)
        window.speed_spin.setValue(7)
        window.force_spin.setValue(42)
        expected = copy.deepcopy(window._document_state())
        for suffix in (".jdesign", ".lds"):
            path = Path(self.folder.name) / ("design" + suffix)
            window._write_document(str(path))
            other = VeronaWindow()
            self.addCleanup(other.deleteLater)
            self.assertTrue(other.open_path(str(path)))
            self.assertEqual(other._document_state(), expected)
            window.new_design()
            window.passes_spin.setValue(1)
            self.assertTrue(window.open_path(str(path)))
            self.assertEqual(window._document_state(), expected)
            self.assertFalse(window.is_dirty())

    def test_invalid_saved_preferences_reject_before_loading(self):
        for settings in ([], {"resolution": True}, {"copies": 0}, {"paper_source": 200},
                         {"cups_options": ["MediaType"]}, {"cups_options": ["number-up", "2"]}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                validate_printer_settings(settings)

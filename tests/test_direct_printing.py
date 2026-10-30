"""Direct printing without submitting jobs to real printers."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMarginsF, QSettings, QSize, QSizeF
from PySide6.QtGui import QAction, QPageLayout, QPageSize
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from verona import printing
from verona.i18n import set_language
from verona.ui import VeronaWindow
from tests.test_editor_actions import image_item, rectangle


class DirectPrintingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings.cleanup()

    def setUp(self):
        set_language("zh_TW")
        self.items = [image_item(bleed={"width_mm": 1, "color": "#ff0000"}),
                      rectangle(x=50, print_style={"fill": "#0000ff", "stroke": None, "width_mm": .2}),
                      rectangle(x=75)]
        self.metadata = {"print_cut": {"enabled": True, "paper_width": 210,
            "paper_height": 297, "bounds": [5.5, 5.5, 204.5, 291.5], "offset": [-.3, .2]}}

    def pdf_printer(self, filename):
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(filename))
        printer.setResolution(600)
        registration = self.metadata["print_cut"]
        printer.setPageSize(QPageSize(QSizeF(registration["paper_width"], registration["paper_height"]),
                                      QPageSize.Unit.Millimeter))
        return printer

    def window(self):
        window = VeronaWindow()
        window.items = copy.deepcopy(self.items)
        window.metadata = copy.deepcopy(self.metadata)
        self.addCleanup(window.deleteLater)
        return window

    def test_printer_output_matches_pdf_in_actual_size_despite_user_margins(self):
        before = copy.deepcopy(self.items)
        for width, height, calibration in ((210, 297, False), (297, 210, False), (100, 150, True)):
            with self.subTest(size=(width, height), calibration=calibration), tempfile.TemporaryDirectory() as folder:
                self.metadata["print_cut"].update(paper_width=width, paper_height=height,
                    bounds=[5.5, 5.5, width-5.5, height-5.5])
                direct, exported = Path(folder)/"direct.pdf", Path(folder)/"export.pdf"
                printer = self.pdf_printer(direct)
                printer.setPageMargins(QMarginsF(12, 15, 12, 15))
                printing.print_document(printer, self.items, self.metadata, calibration=calibration)
                printing.write_print_pdf(exported, self.items, self.metadata, calibration=calibration)
                images = []
                for filename in (direct, exported):
                    pdf = QPdfDocument()
                    self.assertEqual(pdf.load(str(filename)), QPdfDocument.Error.None_)
                    self.assertEqual(pdf.pageCount(), 1)
                    size = pdf.pagePointSize(0)
                    # Qt rounds the PDF MediaBox to whole points (up to 0.177 mm).
                    self.assertAlmostEqual(size.width()*25.4/72, width, delta=.18)
                    self.assertAlmostEqual(size.height()*25.4/72, height, delta=.18)
                    images.append(pdf.render(0, QSize(width*5, height*5)))
                    pdf.close()
                self.assertFalse(images[0].isNull())
                self.assertEqual(images[0], images[1])
                # A 20 mm vector remains 20 mm and the cut-only outline stays unprinted.
                self.assertEqual(images[0].pixelColor(300, 125).name(), "#0000ff")
                for x, color in ((245, "#ffffff"), (255, "#0000ff"),
                                 (345, "#0000ff"), (355, "#ffffff")):
                    self.assertEqual(images[0].pixelColor(x, 125).name(), color)
                if not calibration:
                    self.assertEqual(images[0].pixelColor(375, 125).name(), "#ffffff")
        self.assertEqual(self.items, before)

    def test_paper_mismatch_is_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/"bad.pdf"
            printer = self.pdf_printer(output)
            printer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
            with self.assertRaisesRegex(ValueError, "紙張尺寸與方向"):
                printing.print_document(printer, self.items, self.metadata)
            self.assertFalse(output.exists())

    def test_mark_strokes_must_fit_all_four_driver_margins(self):
        with tempfile.TemporaryDirectory() as folder:
            for values in ((5.1, 5, 5, 5), (5, 5.1, 5, 5), (5, 5, 5.1, 5), (5, 5, 5, 5.1)):
                with self.subTest(margins=values):
                    output = Path(folder)/"bad.pdf"
                    printer = self.pdf_printer(output)
                    layout = printer.pageLayout()
                    layout.setUnits(QPageLayout.Unit.Millimeter)
                    layout.setMinimumMargins(QMarginsF(*values))
                    with patch.object(printer, "pageLayout", return_value=layout), \
                            self.assertRaisesRegex(ValueError, "可列印範圍"):
                        printing.print_document(printer, self.items, self.metadata)
                    self.assertFalse(output.exists())

    def test_invalid_artwork_never_starts_printer(self):
        for items, metadata in (([], self.metadata), (self.items, {})):
            printer = MagicMock()
            with self.subTest(items=len(items)), patch.object(printing, "QPainter") as painter, \
                    self.assertRaises(ValueError):
                printing.print_document(printer, items, metadata)
            painter.assert_not_called()

    def test_printer_begin_and_finish_failures_are_reported_without_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            printer = self.pdf_printer(Path(folder)/"unused.pdf")
            for active, finished, state in ((False, True, QPrinter.PrinterState.Idle),
                    (True, False, QPrinter.PrinterState.Idle), (True, True, QPrinter.PrinterState.Error)):
                with self.subTest(active=active, finished=finished, state=state), \
                        patch.object(printing, "QPainter") as painter, \
                        patch.object(printing, "_draw_print_page"), \
                        patch.object(printer, "printerState", return_value=state):
                    painter.return_value.isActive.return_value = active
                    painter.return_value.end.return_value = finished
                    with self.assertRaises(OSError):
                        printing.print_document(printer, self.items, self.metadata)
                    painter.assert_called_once_with(printer)

    def test_render_failure_aborts_partial_job(self):
        with tempfile.TemporaryDirectory() as folder:
            printer = self.pdf_printer(Path(folder)/"unused.pdf")
            with patch.object(printing, "QPainter") as painter, \
                    patch.object(printing, "_draw_print_page", side_effect=ValueError("bad image")), \
                    patch.object(printer, "abort") as abort:
                with self.assertRaisesRegex(ValueError, "bad image"):
                    printing.print_document(printer, self.items, self.metadata)
                abort.assert_called_once()
                painter.return_value.end.assert_called_once()

    def test_print_action_and_dialog_submit_current_document_in_both_languages(self):
        for language, label in (("zh_TW", "列印…"), ("en", "Print…")):
            set_language(language)
            window = self.window()
            before = copy.deepcopy(window._snapshot())
            for calibration, toolbar in ((False, False), (False, True), (True, False), (True, True)):
                with self.subTest(language=language, calibration=calibration, toolbar=toolbar), \
                        patch.object(QPrinterInfo, "availablePrinters", return_value=[MagicMock()]), \
                        patch("PySide6.QtPrintSupport.QPrinter") as printer_class, \
                        patch("verona.print_dialog.PrintDialog") as dialog_class, \
                        patch.object(printing, "print_document") as submit, \
                        patch("verona.print_dialog.printer_settings", return_value={"printer_name": "Test printer"}):
                    printer_class.return_value.printerName.return_value = "Test printer"
                    dialog_class.return_value.exec.return_value = QDialog.DialogCode.Accepted
                    if calibration:
                        window.metadata["source_format"] = "calibration"
                    actions = [a for a in window.findChildren(QAction)
                               if a.text() == label and a.shortcut().toString() == "Ctrl+P"]
                    self.assertEqual(len(actions), 1)
                    if toolbar:
                        self.assertEqual(window.print_button.text(), label)
                        window.print_button.click()
                    else:
                        actions[0].trigger()
                    submit.assert_called_once_with(printer_class.return_value, window.items,
                                                   window.metadata, calibration=calibration)
                    self.assertIn("Test printer", window.status_label.text())
                    self.assertIn("100%", window.status_label.text())
            window.metadata.pop("source_format")
            before["metadata"]["printer_settings"] = {"printer_name": "Test printer"}
            self.assertEqual(window._snapshot(), before)
            self.assertTrue(window.is_dirty())

    def test_cancelled_dialog_does_not_submit_or_change_document(self):
        window = self.window()
        before = copy.deepcopy(window._snapshot())
        status = window.status_label.text()
        with patch.object(QPrinterInfo, "availablePrinters", return_value=[MagicMock()]), \
                patch("PySide6.QtPrintSupport.QPrinter"), \
                patch("verona.print_dialog.PrintDialog") as dialog, \
                patch.object(printing, "print_document") as submit:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            window.print_design()
            submit.assert_not_called()
        self.assertEqual(window._snapshot(), before)
        self.assertEqual(window.status_label.text(), status)

    def test_missing_queues_and_invalid_document_never_open_print_dialog(self):
        window = self.window()
        with patch.object(QPrinterInfo, "availablePrinters", return_value=[]), \
                patch("verona.print_dialog.PrintDialog") as dialog, \
                patch.object(QMessageBox, "warning") as warning:
            window.print_design()
            warning.assert_called_once()
            dialog.assert_not_called()
        window.items = []
        with patch("verona.print_dialog.PrintDialog") as dialog, \
                patch.object(QMessageBox, "critical") as critical:
            window.print_design()
            critical.assert_called_once()
            dialog.assert_not_called()

    def test_submission_failure_is_shown_without_success_status_or_retry(self):
        window = self.window()
        status = window.status_label.text()
        with patch.object(QPrinterInfo, "availablePrinters", return_value=[MagicMock()]), \
                patch("PySide6.QtPrintSupport.QPrinter"), \
                patch("verona.print_dialog.PrintDialog") as dialog, \
                patch.object(printing, "print_document", side_effect=OSError("queue failed")) as submit, \
                patch("verona.print_dialog.printer_settings", return_value={"printer_name": "Test printer"}), \
                patch.object(QMessageBox, "critical") as critical:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            window.print_design()
            submit.assert_called_once()
            self.assertEqual(critical.call_args.args[2], "queue failed")
        self.assertEqual(window.status_label.text(), status)


if __name__ == "__main__":
    unittest.main()

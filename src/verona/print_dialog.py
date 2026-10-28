"""Print preview and document-local printer preferences; no implicit submission."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QPageLayout, QPainter
from PySide6.QtPrintSupport import QPrintDialog, QPrintEngine, QPrintPreviewWidget, QPrinter
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from .i18n import tr
from .printing import _draw_print_page, validate_printer


# Qt 6.10's CUPS backend stores driver-specific options as alternating strings.
# PySide exposes QPrintEngine but does not name this private backend property.
_CUPS_OPTIONS = QPrintEngine.PrintEnginePropertyKey(0xfe00)
_MEDIA_OPTIONS = {"MediaType", "media-type", "media", "media-source", "InputSlot",
                  "Quality", "print-quality", "Resolution", "ColorModel", "OutputMode"}


def validate_printer_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError(tr("設計檔中的列印設定無效。"))
    name = settings.get("printer_name", "")
    options = settings.get("cups_options", [])
    if (not isinstance(name, str) or len(name) > 1024 or "\0" in name
            or not isinstance(options, list) or len(options) > 128 or len(options) % 2
            or any(not isinstance(value, str) or len(value) > 1024 or "\0" in value for value in options)
            or any(key not in _MEDIA_OPTIONS for key in options[::2])):
        raise ValueError(tr("設計檔中的列印設定無效。"))
    for key, low, high in (("resolution", 1, 9600), ("color_mode", 0, 1),
                           ("paper_source", 0, 14), ("copies", 1, 999)):
        if key in settings and (type(settings[key]) is not int or not low <= settings[key] <= high):
            raise ValueError(tr("設計檔中的列印設定無效。"))
    if "collate" in settings and type(settings["collate"]) is not bool:
        raise ValueError(tr("設計檔中的列印設定無效。"))
    return settings


def printer_settings(printer):
    options = printer.printEngine().property(_CUPS_OPTIONS) or []
    media = []
    for key, value in zip(options[::2], options[1::2]):
        if key in _MEDIA_OPTIONS:
            media.extend((key, value))
    return validate_printer_settings({"printer_name": printer.printerName(),
        "resolution": printer.resolution(), "color_mode": printer.colorMode().value,
        "paper_source": printer.paperSource().value, "copies": printer.copyCount(),
        "collate": printer.collateCopies(), "cups_options": media})


def apply_printer_settings(printer, settings):
    validate_printer_settings(settings)
    if settings.get("printer_name") != printer.printerName():
        return
    if "resolution" in settings:
        printer.setResolution(settings["resolution"])
    if "color_mode" in settings:
        printer.setColorMode(QPrinter.ColorMode(settings["color_mode"]))
    if "paper_source" in settings:
        printer.setPaperSource(QPrinter.PaperSource(settings["paper_source"]))
    printer.setCopyCount(settings.get("copies", 1))
    printer.setCollateCopies(settings.get("collate", False))
    if settings.get("cups_options"):
        printer.printEngine().setProperty(_CUPS_OPTIONS, settings["cups_options"])


class PrintDialog(QDialog):
    def __init__(self, printer, items, metadata, *, calibration=False, saved_printer_name=None, parent=None):
        super().__init__(parent)
        self.printer, self.items, self.metadata = printer, items, metadata
        self.calibration = calibration
        self.setWindowTitle(tr("列印（100% 原尺寸）"))
        self.resize(980, 720)
        outer = QVBoxLayout(self)
        heading = QLabel(tr("列印預覽"), objectName="title")
        outer.addWidget(heading)
        note = QLabel(tr("確認紙材與圖稿後列印；設定會隨設計儲存。"), objectName="muted")
        note.setWordWrap(True)
        outer.addWidget(note)
        body = QHBoxLayout()
        self.preview = QPrintPreviewWidget(printer, self)
        self.preview.paintRequested.connect(self._paint_preview)
        body.addWidget(self.preview, 1)
        panel = QWidget()
        panel.setFixedWidth(260)
        details = QVBoxLayout(panel)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setVerticalSpacing(12)
        self.summary = {}
        for key, label in (("printer", "印表機"), ("paper", "列印紙張"), ("media", "紙材／品質"),
                           ("resolution", "解析度"), ("copies", "份數")):
            value = QLabel()
            value.setWordWrap(True)
            value.setTextFormat(Qt.TextFormat.PlainText)
            self.summary[key] = value
            form.addRow(tr(label), value)
        details.addLayout(form)
        settings = QPushButton(tr("印表機與紙材設定…"))
        settings.clicked.connect(self._configure)
        details.addWidget(settings)
        actual_size = QLabel(tr("100% 原尺寸 · 不縮放"))
        details.addWidget(actual_size)
        saved_name = saved_printer_name or metadata.get("printer_settings", {}).get("printer_name")
        if saved_name and saved_name != printer.printerName():
            missing = QLabel(tr("原印表機 {name} 無法使用，請確認目前設定。", name=saved_name))
            missing.setTextFormat(Qt.TextFormat.PlainText)
            missing.setWordWrap(True)
            details.addWidget(missing)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        details.addWidget(self.error)
        details.addStretch()
        body.addWidget(panel)
        outer.addLayout(body, 1)
        footer = QHBoxLayout()
        for label, callback in (("−", self.preview.zoomOut), ("+", self.preview.zoomIn),
                                (tr("整頁檢視"), self.preview.fitInView)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, action=callback: action())
            footer.addWidget(button)
        footer.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.print_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.print_button.setText(tr("列印"))
        self.print_button.setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        outer.addLayout(footer)
        self._refresh_summary()

    def _refresh_summary(self):
        settings = printer_settings(self.printer)
        paper = self.printer.pageLayout().fullRect(QPageLayout.Unit.Millimeter)
        self.summary["printer"].setText(self.printer.printerName())
        self.summary["paper"].setText(f"{self.printer.pageLayout().pageSize().name()} · "
                                      f"{paper.width():g} × {paper.height():g} mm")
        options = settings["cups_options"]
        self.summary["media"].setText("\n".join(f"{key}: {value}" for key, value in
                                              zip(options[::2], options[1::2])) or tr("印表機預設"))
        color = tr("彩色") if self.printer.colorMode() == QPrinter.ColorMode.Color else tr("灰階")
        self.summary["resolution"].setText(f"{settings['resolution']} DPI · {color}")
        self.summary["copies"].setText(str(settings["copies"]))

    def _configure(self):
        dialog = QPrintDialog(self.printer, self)
        dialog.setWindowTitle(tr("印表機與紙材設定…"))
        dialog.setOptions(QPrintDialog.PrintDialogOption.PrintShowPageSize
                          | QPrintDialog.PrintDialogOption.PrintCollateCopies)
        for buttons in dialog.findChildren(QDialogButtonBox):
            for button in buttons.buttons():
                if buttons.buttonRole(button) == QDialogButtonBox.ButtonRole.AcceptRole:
                    button.setText(tr("套用設定"))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.printer.setDuplex(QPrinter.DuplexMode.DuplexNone)
            self.printer.setFullPage(True)
            self._refresh_summary()
            self.preview.updatePreview()

    def _paint_preview(self, printer):
        try:
            validate_printer(printer, self.items, self.metadata, calibration=self.calibration)
        except ValueError as error:
            self.error.setText(str(error))
            self.print_button.setEnabled(False)
            return
        painter = QPainter(printer)
        try:
            _draw_print_page(painter, self.items, self.metadata, calibration=self.calibration)
            self.error.clear()
            self.print_button.setEnabled(True)
        except Exception as error:
            self.error.setText(str(error))
            self.print_button.setEnabled(False)
        finally:
            painter.end()

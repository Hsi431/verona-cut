"""Print preview and document-local printer preferences; no implicit submission."""
import hashlib
import os

from PySide6.QtCore import Qt, QSizeF, QSettings
from PySide6.QtGui import QPageLayout, QPageSize, QPainter
from PySide6.QtPrintSupport import QPrintDialog, QPrintEngine, QPrintPreviewWidget, QPrinter, QPrinterInfo
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from .i18n import tr
from .printer_options import display_choice, page_size_choice, read_printer_options
from .printing import _draw_print_page, validate_printer


# Qt 6.10's CUPS backend stores driver-specific options as alternating strings.
# PySide exposes QPrintEngine but does not name this private backend property.
_CUPS_OPTIONS = QPrintEngine.PrintEnginePropertyKey(0xfe00)
_MEDIA_OPTIONS = {"MediaType", "media-type", "media", "media-source", "InputSlot",
                  "Quality", "print-quality", "Resolution", "ColorModel", "OutputMode",
                  "Ink", "Brightness", "Contrast", "Saturation", "cupsPrintQuality"}


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
        apply_job_cups_options(printer, settings["cups_options"], replace=True)


def apply_job_cups_options(printer, options, *, replace=False):
    """Apply driver options to this QPrinter job without changing queue defaults."""
    if (not isinstance(options, list) or len(options) > 128 or len(options) % 2
            or any(not isinstance(value, str) or len(value) > 1024 or "\0" in value for value in options)
            or any(key not in _MEDIA_OPTIONS | {"PageSize", "page-size", "media-size"}
                   for key in options[::2])):
        raise ValueError(tr("設計檔中的列印設定無效。"))
    current = [] if replace else list(printer.printEngine().property(_CUPS_OPTIONS) or [])
    values = dict(zip(current[::2], current[1::2]))
    values.update(zip(options[::2], options[1::2]))
    flattened = [part for key, value in values.items() for part in (key, value)]
    printer.printEngine().setProperty(_CUPS_OPTIONS, flattened)


def _set_cups_option(printer, key, value):
    """Change only the active QPrinter job's CUPS options."""
    apply_job_cups_options(printer, [key, str(value)])


def apply_print_job_options(printer, options, available_options, paper_width_mm, paper_height_mm):
    """Apply validated, explicit MCP settings to one QPrinter job."""
    if not isinstance(options, dict):
        raise ValueError(tr("列印選項必須是物件。"))
    allowed = {"media", "quality", "color_mode", "copies", "resolution",
               "brightness", "contrast", "saturation"}
    if set(options) - allowed:
        raise ValueError(tr("列印選項包含不支援的欄位。"))
    if (isinstance(paper_width_mm, bool) or not isinstance(paper_width_mm, (int, float))
            or isinstance(paper_height_mm, bool) or not isinstance(paper_height_mm, (int, float))):
        raise ValueError(tr("列印紙張尺寸無效。"))

    cups_options = []

    def choose(name, aliases):
        value = options.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError(tr("列印選項 {name} 無效。", name=name))
        key = next((actual for actual in (available_options or {})
                    if actual.casefold() in aliases), None)
        if key is None:
            raise ValueError(tr("這台印表機不支援列印選項 {name}。", name=name))
        choices = available_options[key].get("choices", [])
        actual_value = next((item for item in choices if item.casefold() == value.casefold()), None)
        if actual_value is None:
            raise ValueError(tr("印表機不支援指定的列印選項 {name}。", name=name))
        cups_options.extend((key, actual_value))
        return actual_value

    page = page_size_choice(available_options, paper_width_mm, paper_height_mm) \
        if available_options else None
    if page:
        cups_options.extend(page)

    media = choose("media", {"mediatype", "media-type", "media", "media-source", "inputslot"})
    quality = choose("quality", {"cupsprintquality", "quality", "print-quality"})
    for name in ("brightness", "contrast", "saturation"):
        choose(name, {name})

    color_mode = options.get("color_mode", "color")
    if color_mode not in ("color", "grayscale"):
        raise ValueError(tr("色彩模式必須是 color 或 grayscale。"))
    ink_key = next((key for key in (available_options or {}) if key.casefold() == "ink"), None)
    if ink_key:
        choices = available_options[ink_key].get("choices", [])
        accepted = {"color", "colour"} if color_mode == "color" else {
            "mono", "monochrome", "gray", "grayscale", "grey", "greyscale"}
        ink_value = next((choice for choice in choices if choice.casefold() in accepted), None)
        if ink_value is not None:
            cups_options.extend((ink_key, ink_value))
    from PySide6.QtPrintSupport import QPrinter
    printer.setColorMode(QPrinter.ColorMode.Color if color_mode == "color"
                         else QPrinter.ColorMode.GrayScale)

    copies = options.get("copies", 1)
    if type(copies) is not int or not 1 <= copies <= 999:
        raise ValueError(tr("列印份數必須介於 1 與 999。"))
    resolution = options.get("resolution", 300)
    if type(resolution) is not int or not 72 <= resolution <= 600:
        raise ValueError(tr("列印解析度必須介於 72 與 600 DPI。"))
    printer.setCopyCount(copies)
    printer.setResolution(resolution)
    printer.setFullPage(True)
    if cups_options:
        apply_job_cups_options(printer, cups_options)
    return {"media": media, "quality": quality, "color_mode": color_mode,
            "copies": copies, "resolution": resolution, "cups_options": cups_options}


def icc_profile_settings_key(printer_name, media_value):
    identity = f"{printer_name}\0{media_value}".encode("utf-8")
    return "printing/icc/" + hashlib.sha256(identity).hexdigest()


class PrintDialog(QDialog):
    def __init__(self, printer, items, metadata, *, calibration=False, saved_printer_name=None, parent=None):
        super().__init__(parent)
        self.printer, self.items, self.metadata = printer, items, metadata
        self.calibration = calibration
        self.design_width = float(metadata.get("print_cut", {}).get("paper_width", 0))
        self.design_height = float(metadata.get("print_cut", {}).get("paper_height", 0))
        self.setWindowTitle(tr("列印"))
        self.resize(1020, 740)
        outer = QVBoxLayout(self)
        body = QHBoxLayout()
        self.preview = QPrintPreviewWidget(printer, self)
        self.preview.paintRequested.connect(self._paint_preview)
        body.addWidget(self.preview, 1)
        panel = QWidget()
        panel.setFixedWidth(320)
        details = QVBoxLayout(panel)

        self.driver_controls = QWidget()
        driver_layout = QVBoxLayout(self.driver_controls)
        driver_layout.setContentsMargins(0, 0, 0, 0)
        self.driver_form = QFormLayout()
        self.driver_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.driver_form.setVerticalSpacing(8)
        self.printer_combo = QComboBox()
        self._populate_printers(saved_printer_name)
        self.driver_form.addRow(tr("印表機"), self.printer_combo)
        self.paper_value = QLabel()
        self.paper_value.setWordWrap(True)
        self.paper_value.setTextFormat(Qt.TextFormat.PlainText)
        self.driver_form.addRow(tr("列印紙張"), self.paper_value)
        self.paper_error = QLabel()
        self.paper_error.setWordWrap(True)
        self.paper_error.setTextFormat(Qt.TextFormat.PlainText)
        self.paper_error.hide()
        self.driver_form.addRow(self.paper_error)
        self.media_combo = QComboBox()
        self.media_combo.currentIndexChanged.connect(self._media_changed)
        self.media_row = QLabel(tr("紙材／品質"))
        self.driver_form.addRow(self.media_row, self.media_combo)
        self.quality_combo = QComboBox()
        self.quality_combo.currentIndexChanged.connect(self._quality_changed)
        self.quality_row = QLabel(tr("列印品質"))
        self.driver_form.addRow(self.quality_row, self.quality_combo)
        self.color_combo = QComboBox()
        self.color_combo.addItem(tr("彩色"), QPrinter.ColorMode.Color.value)
        self.color_combo.addItem(tr("灰階"), QPrinter.ColorMode.GrayScale.value)
        self.color_combo.currentIndexChanged.connect(self._color_changed)
        self.driver_form.addRow(tr("色彩"), self.color_combo)
        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 999)
        self.copies_spin.setValue(printer.copyCount())
        self.copies_spin.valueChanged.connect(self.printer.setCopyCount)
        self.driver_form.addRow(tr("份數"), self.copies_spin)
        driver_layout.addLayout(self.driver_form)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText(tr("進階"))
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_options = QWidget()
        self.advanced_form = QFormLayout(self.advanced_options)
        self.advanced_form.setContentsMargins(0, 0, 0, 0)
        self.advanced_options.hide()
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        driver_layout.addWidget(self.advanced_toggle)
        driver_layout.addWidget(self.advanced_options)
        details.addWidget(self.driver_controls)
        self.options_fallback = QLabel(tr("無法讀取印表機選項；請使用系統列印設定。"))
        self.options_fallback.setWordWrap(True)
        self.options_fallback.setTextFormat(Qt.TextFormat.PlainText)
        details.addWidget(self.options_fallback)

        self.summary = {}
        self.summary_form = QFormLayout()
        self.summary_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.summary_form.setVerticalSpacing(8)
        for key, label in (("printer", "印表機"), ("paper", "列印紙張"), ("media", "紙材／品質"),
                           ("resolution", "解析度"), ("copies", "份數")):
            value = QLabel()
            value.setWordWrap(True)
            value.setTextFormat(Qt.TextFormat.PlainText)
            self.summary[key] = value
            self.summary_form.addRow(tr(label), value)
        self.summary_widget = QWidget()
        self.summary_widget.setLayout(self.summary_form)
        details.addWidget(self.summary_widget)

        self.icc_form = QFormLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.addItem(tr("不使用（交給印表機）"), "")
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        self.choose_profile_button = QPushButton(tr("選擇 ICC 檔…"))
        self.choose_profile_button.clicked.connect(self._choose_profile)
        self.icc_form.addRow(tr("色彩描述檔"), self.profile_combo)
        self.icc_form.addRow("", self.choose_profile_button)
        self.intent_combo = QComboBox()
        self.intent_combo.addItem(tr("感知"), "perceptual")
        self.intent_combo.addItem(tr("相對色度"), "relative")
        self.intent_combo.currentIndexChanged.connect(self.preview.updatePreview)
        self.icc_form.addRow(tr("演算意圖"), self.intent_combo)
        details.addLayout(self.icc_form)

        settings = QPushButton(tr("更多印表機設定…"))
        settings.setAccessibleName(tr("更多印表機設定…"))
        settings.setToolTip(tr("設定會隨目前設計儲存。"))
        settings.clicked.connect(self._configure)
        self.more_settings_button = settings
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
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        self.print_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.print_button.setText(tr("列印"))
        self.print_button.setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        outer.addLayout(footer)

        self.options = None
        self.options_available = False
        self.option_keys = {}
        self.advanced_combos = {}
        self.color_profile_path = ""
        self.settings = QSettings("Verona", "Workspace")
        self.printer_combo.currentIndexChanged.connect(self._printer_changed)
        self._load_options()
        self._refresh_summary()

    def _populate_printers(self, selected_name):
        printers = QPrinterInfo.availablePrinters()
        names = [info.printerName() for info in printers]
        current = self.printer.printerName()
        if current and current not in names:
            names.append(current)
        if not names:
            names = [current or tr("目前印表機")]
        for name in names:
            self.printer_combo.addItem(name, name)
        desired = selected_name or current
        index = self.printer_combo.findData(desired)
        if index >= 0:
            self.printer_combo.setCurrentIndex(index)

    def _toggle_advanced(self, expanded):
        self.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.advanced_options.setVisible(expanded)

    def _option_value_map(self):
        values = self.printer.printEngine().property(_CUPS_OPTIONS) or []
        return dict(zip(values[::2], values[1::2]))

    def _find_option(self, names):
        return next((key for key in self.options or {} if key.casefold() in names), None)

    def _configure_combo(self, combo, key):
        combo.blockSignals(True)
        combo.clear()
        if key and self.options and key in self.options:
            option = self.options[key]
            current = self._option_value_map().get(key, option.get("default"))
            for value in option["choices"]:
                combo.addItem(display_choice(value), value)
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)
        return bool(combo.count())

    def _load_options(self):
        try:
            self.options = read_printer_options(self.printer.printerName())
        except (OSError, ValueError, TypeError):
            self.options = None
        self.options_available = bool(self.options)
        self.driver_controls.setVisible(self.options_available)
        self.options_fallback.setVisible(not self.options_available)
        self.summary_widget.setVisible(not self.options_available)
        if not self.options_available:
            self.paper_error.clear()
            self.paper_error.hide()
            self._load_profile_preference("")
            self.preview.updatePreview()
            return

        page = page_size_choice(self.options, self.design_width, self.design_height)
        if page is None:
            self.paper_error.setText(tr("這台印表機沒有設計紙張尺寸；請開啟系統列印設定選擇支援的紙張。"))
        else:
            key, value = page
            self.option_keys["PageSize"] = key
            self._set_option(key, value)
            size = QPageSize(QSizeF(min(self.design_width, self.design_height),
                                    max(self.design_width, self.design_height)),
                             QPageSize.Unit.Millimeter)
            accepted = self.printer.setPageSize(size)
            self.printer.setPageOrientation(QPageLayout.Orientation.Landscape
                if self.design_width > self.design_height else QPageLayout.Orientation.Portrait)
            self.paper_error.setText("" if accepted else tr("這台印表機無法使用設計紙張尺寸。"))
        self.paper_error.setVisible(bool(self.paper_error.text()))
        media_key = self._find_option({"mediatype", "media-type", "media", "media-source", "inputslot"})
        quality_key = self._find_option({"cupsprintquality", "quality", "print-quality"})
        self.option_keys["Media"] = media_key
        self.option_keys["Quality"] = quality_key
        self.media_row.setVisible(self._configure_combo(self.media_combo, media_key))
        self.media_combo.setVisible(bool(media_key))
        self.quality_row.setVisible(self._configure_combo(self.quality_combo, quality_key))
        self.quality_combo.setVisible(bool(quality_key))

        ink_key = self._find_option({"ink"})
        self.option_keys["Ink"] = ink_key
        if ink_key:
            self._configure_combo(self.color_combo, ink_key)
            values = self.options[ink_key]["choices"]
            for index, value in enumerate(values):
                label = tr("彩色") if value.casefold() in ("color", "colour") else (
                    tr("灰階") if value.casefold() in ("mono", "monochrome", "gray", "grayscale") else value)
                self.color_combo.setItemText(index, label)
            current = self._option_value_map().get(ink_key, self.options[ink_key].get("default"))
            self.color_combo.setCurrentIndex(max(0, self.color_combo.findData(current)))
        else:
            self.color_combo.blockSignals(True)
            self.color_combo.setCurrentIndex(0 if self.printer.colorMode() == QPrinter.ColorMode.Color else 1)
            self.color_combo.blockSignals(False)

        self._clear_advanced()
        for key, label in (("Brightness", "亮度"), ("Contrast", "對比"), ("Saturation", "飽和度")):
            actual = self._find_option({key.casefold()})
            if not actual:
                continue
            combo = QComboBox()
            combo.currentIndexChanged.connect(lambda _index, option=actual, control=combo:
                                               self._set_option(option, control.currentData()))
            self._configure_combo(combo, actual)
            self.advanced_form.addRow(tr(label), combo)
            self.advanced_combos[actual] = combo
        self._load_profile_preference(self._current_media_value())
        self._refresh_summary()
        self.preview.updatePreview()

    def _clear_advanced(self):
        while self.advanced_form.rowCount():
            self.advanced_form.removeRow(0)
        self.advanced_combos.clear()

    def _set_option(self, key, value):
        if key and value is not None:
            _set_cups_option(self.printer, key, str(value))
            self._refresh_summary()
            self.preview.updatePreview()

    def _current_media_value(self):
        if self.media_combo.count():
            return self.media_combo.currentData() or ""
        return ""

    def _media_changed(self, _index):
        self._set_option(self.option_keys.get("Media"), self.media_combo.currentData())
        self._load_profile_preference(self._current_media_value())

    def _quality_changed(self, _index):
        self._set_option(self.option_keys.get("Quality"), self.quality_combo.currentData())

    def _color_changed(self, _index):
        key = self.option_keys.get("Ink")
        if key:
            self._set_option(key, self.color_combo.currentData())
            mode = QPrinter.ColorMode.Color if self.color_combo.currentData().casefold() in ("color", "colour") \
                else QPrinter.ColorMode.GrayScale
            self.printer.setColorMode(mode)
        else:
            self.printer.setColorMode(QPrinter.ColorMode(self.color_combo.currentData()))
        self._refresh_summary()
        self.preview.updatePreview()

    def _printer_changed(self, _index):
        name = self.printer_combo.currentData()
        if name and name != self.printer.printerName():
            self.printer.setPrinterName(name)
            self.printer.printEngine().setProperty(_CUPS_OPTIONS, [])
            self._load_options()
            self._refresh_summary()

    def _profile_key(self, media_value=None):
        return icc_profile_settings_key(self.printer.printerName(),
                                        self._current_media_value() if media_value is None else media_value)

    def _load_profile_preference(self, media_value):
        key = self._profile_key(media_value)
        path = self.settings.value(key + "/profile", "", type=str)
        intent = self.settings.value(key + "/intent", "perceptual", type=str)
        if path and os.path.isfile(path):
            self._select_profile(path)
        else:
            self._select_profile("")
        index = self.intent_combo.findData(intent)
        if index >= 0:
            self.intent_combo.setCurrentIndex(index)

    def _select_profile(self, path):
        self.color_profile_path = path
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem(tr("不使用（交給印表機）"), "")
        if path:
            self.profile_combo.addItem(os.path.basename(path), path)
            self.profile_combo.setCurrentIndex(1)
        self.profile_combo.blockSignals(False)
        self.preview.updatePreview()

    def _choose_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("選擇 ICC 色彩描述檔"), "",
                                               tr("ICC 色彩描述檔 (*.icc *.icm);;所有檔案 (*)"))
        if path:
            self._select_profile(path)

    def _profile_changed(self, _index):
        self.color_profile_path = self.profile_combo.currentData() or ""
        self.preview.updatePreview()

    def save_color_preferences(self, settings=None):
        if settings is None:
            settings = self.settings
        key = self._profile_key()
        settings.setValue(key + "/profile", self.color_profile_path)
        settings.setValue(key + "/intent", self.intent_combo.currentData())

    def _refresh_summary(self):
        settings = printer_settings(self.printer)
        if self.design_width and self.design_height:
            paper_text = f"{self.design_width:g} × {self.design_height:g} mm"
        else:
            paper = self.printer.pageLayout().fullRect(QPageLayout.Unit.Millimeter)
            paper_text = f"{paper.width():g} × {paper.height():g} mm"
        self.paper_value.setText(paper_text)
        self.summary["printer"].setText(self.printer.printerName())
        self.summary["paper"].setText(paper_text)
        options = settings["cups_options"]
        self.summary["media"].setText("\n".join(f"{key}: {display_choice(value)}" for key, value in
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
            self._load_options()
            self._refresh_summary()
            self.preview.updatePreview()

    def _paint_preview(self, printer):
        try:
            if self.paper_error.text():
                raise ValueError(self.paper_error.text())
            validate_printer(printer, self.items, self.metadata, calibration=self.calibration)
        except ValueError as error:
            self.error.setText(str(error))
            self.print_button.setEnabled(False)
            return
        painter = QPainter(printer)
        try:
            _draw_print_page(painter, self.items, self.metadata, calibration=self.calibration,
                             color_profile_path=self.color_profile_path or None,
                             rendering_intent=self.intent_combo.currentData() or "perceptual", preview=True)
            self.error.clear()
            self.print_button.setEnabled(True)
        except Exception as error:
            self.error.setText(str(error))
            self.print_button.setEnabled(False)
        finally:
            painter.end()

"""Editable text layout, font selection and its live preview dialog."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QFont, QFontDatabase, QFontInfo, QFontMetricsF,
    QPainter, QPainterPath, QPen, QTransform)
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QCompleter, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QVBoxLayout, QWidget)

from .i18n import tr


def font_families() -> list[str]:
    return list(QFontDatabase.families())


def installed_font_family(family: str) -> str | None:
    folded = family.casefold()
    return next((name for name in font_families() if name.casefold() == folded), None)


def font_styles(family: str) -> list[str]:
    return list(QFontDatabase.styles(family)) or [""]


def resolved_font_family(family: str, style: str = "") -> str:
    return QFontInfo(_font(family, style)).family()


def _font(family: str, style: str = "") -> QFont:
    font = QFontDatabase.font(family, style, 1000) if style else QFont(family)
    font.setPixelSize(1000)
    return font


def make_text_data(content: str, font_family: str, font_style: str, height_mm: float,
                   letter_spacing_mm: float = 0, line_spacing_mm: float = 12,
                   alignment: str = "left", origin_mm: tuple[float, float] = (0, 0)) -> dict[str, Any]:
    return {"content": content, "font_family": font_family, "font_style": font_style,
            "height_mm": float(height_mm), "letter_spacing_mm": float(letter_spacing_mm),
            "line_spacing_mm": float(line_spacing_mm), "alignment": alignment,
            "origin_mm": [float(origin_mm[0]), float(origin_mm[1])]}


def render_text_path(data: dict[str, Any], origin: tuple[float, float] | None = None) -> QPainterPath:
    """Build glyph outlines whose visible height on each line matches height_mm."""
    lines = data["content"].split("\n")
    family, style = data["font_family"], data.get("font_style", "")
    font = _font(family, style)
    metrics = QFontMetricsF(font)
    reference_height = max((metrics.tightBoundingRect(line).height() for line in lines), default=0)
    if reference_height <= 0:
        raise ValueError(tr("文字必須包含可顯示的字元。"))
    scale = float(data["height_mm"]) / reference_height
    spacing = float(data.get("letter_spacing_mm", 0))
    if spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing / scale)
    metrics = QFontMetricsF(font)
    advances = [metrics.horizontalAdvance(line) for line in lines]
    widest = max(advances, default=0)
    line_spacing = float(data.get("line_spacing_mm", data["height_mm"] * 1.2))
    path = QPainterPath()
    for index, (line, advance) in enumerate(zip(lines, advances)):
        alignment = data.get("alignment", "left")
        x = (widest - advance) / 2 if alignment == "center" else widest - advance if alignment == "right" else 0
        baseline = metrics.ascent() + index * line_spacing / scale
        path.addText(QPointF(x, baseline), font, line)
    path = QTransform.fromScale(scale, scale).map(path)
    bounds = path.boundingRect()
    target = origin if origin is not None else tuple(data.get("origin_mm", (0, 0)))
    path.translate(target[0] - bounds.left(), target[1] - bounds.top())
    return path


def render_legacy_text(text: str, family: str, size_mm: float, x_mm: float, y_mm: float,
                       *, style: str = "", letter_spacing_mm: float = 0):
    """Keep the original MCP em-size geometry when its new options are omitted."""
    font = _font(family, style)
    if letter_spacing_mm:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing,
                              letter_spacing_mm * 1000 / size_mm)
    path = QPainterPath()
    path.addText(QPointF(0, 0), font, text)
    path = QTransform.fromScale(size_mm / 1000, size_mm / 1000).map(path)
    path.translate(x_mm, y_mm + size_mm)
    bounds = path.boundingRect()
    data = make_text_data(text, family, QFontInfo(font).styleName() or style,
                          bounds.height(), letter_spacing_mm,
                          QFontMetricsF(font).lineSpacing() * size_mm / 1000,
                          "left", (bounds.left(), bounds.top()))
    return path, data, QFontInfo(font).family()


def text_transform(source_contours, current_contours) -> QTransform:
    """Recover affine transforms applied by ordinary canvas geometry operations."""
    if len(source_contours) != len(current_contours) or any(
            len(source) != len(current) for source, current in zip(source_contours, current_contours)):
        raise ValueError(tr("文字輪廓點數已變更；先轉成輪廓再編輯節點。"))
    count = 0
    sum_x = sum_y = sum_u = sum_v = 0.0
    for source_path, current_path in zip(source_contours, current_contours):
        for (x, y), (u, v) in zip(source_path, current_path):
            count += 1
            sum_x += x
            sum_y += y
            sum_u += u
            sum_v += v
    if not count:
        raise ValueError(tr("無法保留文字變形；先轉成輪廓再編輯節點。"))
    mean_x, mean_y = sum_x/count, sum_y/count
    mean_u, mean_v = sum_u/count, sum_v/count
    xx = xy = yy = xu = yu = xv = yv = 0.0
    for source_path, current_path in zip(source_contours, current_contours):
        for (x, y), (u, v) in zip(source_path, current_path):
            dx, dy = x-mean_x, y-mean_y
            du, dv = u-mean_u, v-mean_v
            xx += dx*dx
            xy += dx*dy
            yy += dy*dy
            xu += dx*du
            yu += dy*du
            xv += dx*dv
            yv += dy*dv
    determinant = xx*yy - xy*xy
    if determinant <= 1e-9:
        raise ValueError(tr("無法保留文字變形；先轉成輪廓再編輯節點。"))
    a = (xu*yy - yu*xy) / determinant
    c = (yu*xx - xu*xy) / determinant
    b = (xv*yy - yv*xy) / determinant
    d = (yv*xx - xv*xy) / determinant
    tx, ty = mean_u-a*mean_x-c*mean_y, mean_v-b*mean_x-d*mean_y
    transform = QTransform(a, b, c, d, tx, ty)
    for source_path, current_path in zip(source_contours, current_contours):
        for (x, y), (u, v) in zip(source_path, current_path):
            mapped = transform.map(QPointF(x, y))
            if abs(mapped.x()-u) > .12 or abs(mapped.y()-v) > .12:
                raise ValueError(tr("文字輪廓已直接編輯；先轉成輪廓再編輯文字。"))
    return transform


def transform_text_contours(contours, transform: QTransform):
    return [[(point.x(), point.y()) for point in (transform.map(QPointF(*value)) for value in contour)]
            for contour in contours]


class TextPreview(QWidget):
    def __init__(self, dialog: "TextLayoutDialog") -> None:
        super().__init__(dialog)
        self.dialog = dialog
        self.setMinimumSize(300, 250)
        self.setObjectName("textPreview")

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        painter.setPen(QPen(self.palette().mid().color(), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 6, 6)
        try:
            path = render_text_path(self.dialog.text_parameters())
        except (ValueError, KeyError):
            return
        bounds = path.boundingRect()
        if bounds.isEmpty():
            return
        margin = 22
        scale = min((self.width() - margin * 2) / bounds.width(),
                    (self.height() - margin * 2) / bounds.height())
        if scale <= 0:
            return
        painter.translate((self.width() - bounds.width() * scale) / 2,
                          (self.height() - bounds.height() * scale) / 2)
        painter.scale(scale, scale)
        painter.translate(-bounds.left(), -bounds.top())
        painter.setPen(QPen(self.palette().text().color(), 0))
        painter.setBrush(QBrush(self.palette().text().color()))
        painter.drawPath(path)


class TextLayoutDialog(QDialog):
    def __init__(self, text_data: dict[str, Any] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.text_data = dict(text_data or {})
        self.families = font_families()
        current_family = self.text_data.get("font_family", "Noto Sans")
        installed = installed_font_family(current_family)
        if installed:
            current_family = installed
        else:
            current_family = self.families[0] if self.families else QFont().family()
        self.setWindowTitle(tr("建立文字輪廓") if text_data is None else tr("編輯文字…"))
        self.setMinimumWidth(850)

        self.content_edit = QPlainTextEdit(self.text_data.get("content", ""))
        self.content_edit.setMinimumHeight(108)
        self.content_edit.setPlaceholderText(tr("輸入一行或多行文字"))
        self.font_combo = QComboBox()
        self.font_combo.setEditable(True)
        self.font_combo.addItems(self.families)
        self.font_combo.setCurrentText(current_family)
        completer = QCompleter(self.families, self.font_combo)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.font_combo.setCompleter(completer)
        self.style_combo = QComboBox()
        self._refresh_styles(self.text_data.get("font_style", ""))

        self.height_spin = self._spin(self.text_data.get("height_mm", 10), .1, 300, .5, 2)
        self.letter_spacing_spin = self._spin(self.text_data.get("letter_spacing_mm", 0), -5, 50, .1, 2)
        self.line_spacing_spin = self._spin(self.text_data.get("line_spacing_mm", 12), 0, 300, .5, 2)
        self.alignment_combo = QComboBox()
        for value, label in (("left", "左"), ("center", "中"), ("right", "右")):
            self.alignment_combo.addItem(tr(label), value)
        alignment = self.text_data.get("alignment", "left")
        self.alignment_combo.setCurrentIndex(max(0, self.alignment_combo.findData(alignment)))

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.addRow(tr("文字"), self.content_edit)
        form.addRow(tr("字型"), self.font_combo)
        form.addRow(tr("樣式"), self.style_combo)
        form.addRow(tr("字身高度"), self.height_spin)
        form.addRow(tr("字距"), self.letter_spacing_spin)
        form.addRow(tr("行距（基線距離）"), self.line_spacing_spin)
        form.addRow(tr("對齊"), self.alignment_combo)

        self.preview = TextPreview(self)
        preview_panel = QVBoxLayout()
        preview_panel.setContentsMargins(12, 4, 0, 0)
        preview_label = QLabel(tr("即時預覽"))
        preview_label.setStyleSheet("font-weight: 600")
        preview_panel.addWidget(preview_label)
        preview_panel.addWidget(self.preview, 1)

        body = QHBoxLayout()
        body.addLayout(form, 3)
        body.addLayout(preview_panel, 2)
        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("套用"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.font_combo.currentTextChanged.connect(self._family_changed)
        for control in (self.content_edit, self.style_combo, self.height_spin,
                        self.letter_spacing_spin, self.line_spacing_spin, self.alignment_combo):
            signal = control.textChanged if isinstance(control, QPlainTextEdit) else (
                control.currentTextChanged if isinstance(control, QComboBox) else control.valueChanged)
            signal.connect(lambda *_: self.preview.update())
        self.resize(940, 510)

    @staticmethod
    def _spin(value: float, minimum: float, maximum: float, step: float, decimals: int) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setSuffix(" mm")
        spin.setValue(float(value))
        return spin

    def _refresh_styles(self, preferred: str = "") -> None:
        family = self.font_combo.currentText().strip()
        styles = font_styles(family) if family in self.families else []
        self.style_combo.clear()
        self.style_combo.addItems(styles)
        index = self.style_combo.findText(preferred)
        if index < 0:
            index = self.style_combo.findText("Regular")
        self.style_combo.setCurrentIndex(max(0, index))

    def _family_changed(self, family: str) -> None:
        self._refresh_styles(self.style_combo.currentText())
        self.preview.update()

    def text_parameters(self) -> dict[str, Any]:
        family = self.font_combo.currentText().strip()
        style = self.style_combo.currentText()
        if not family or family not in self.families:
            raise ValueError(tr("請從已安裝字型中選擇字型。"))
        content = self.content_edit.toPlainText()
        if not content.strip() or len(content) > 1024:
            raise ValueError(tr("文字必須包含 1–1024 個字元且不能只有空白。"))
        return make_text_data(content, family, style, self.height_spin.value(),
            self.letter_spacing_spin.value(), self.line_spacing_spin.value(),
            self.alignment_combo.currentData(), tuple(self.text_data.get("origin_mm", (0, 0))))

    def accept(self) -> None:
        try:
            data = self.text_parameters()
            render_text_path(data)
        except ValueError as error:
            QMessageBox.warning(self, tr("文字設定無效"), str(error))
            return
        super().accept()

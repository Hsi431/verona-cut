"""Preview bounded cut-contour smoothing before applying it to a document."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from .artwork import contour_path, draw_printed_item, item_bounds
from .contour_smoothing import smooth_contours
from .editor_widgets import SafeDoubleSpinBox
from .i18n import tr


class SmoothingCanvas(QWidget):
    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self.scale = 1.0
        self.origin = QPointF()
        self.drag_position = None
        self.setMinimumSize(520, 420)
        self.setMouseTracking(True)

    def _bounds(self):
        item = self.dialog.item
        bounds = item_bounds(item, printed=True)
        paths = self.dialog.build_contours()[self.dialog.preview_index]
        for contours in (item["contours"], paths):
            bounds = bounds.united(contour_path(contours).controlPointRect())
        return bounds

    def fit(self):
        bounds = self._bounds()
        if bounds.isEmpty():
            bounds = QRectF(0, 0, 1, 1)
        self.scale = min(max(1, self.width() - 56) / max(1, bounds.width()),
                         max(1, self.height() - 56) / max(1, bounds.height()))
        self.origin = QPointF(self.width() / 2 - bounds.center().x() * self.scale,
                              self.height() / 2 - bounds.center().y() * self.scale)
        self.update()

    def resizeEvent(self, event):
        self.fit()
        super().resizeEvent(event)

    def _world(self, screen):
        point = (screen - self.origin) / self.scale
        return point

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#f5f6f4"))
        painter.save()
        painter.translate(self.origin)
        painter.scale(self.scale, self.scale)
        item = self.dialog.item
        draw_printed_item(painter, item)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        original_pen = QPen(QColor("#737b78"), 1, Qt.PenStyle.DashLine)
        original_pen.setCosmetic(True)
        painter.setPen(original_pen)
        painter.drawPath(contour_path(item["contours"]))
        result = self.dialog.build_contours()[self.dialog.preview_index]
        new_pen = QPen(QColor("#138a52"), 2)
        new_pen.setCosmetic(True)
        painter.setPen(new_pen)
        painter.drawPath(contour_path(result))
        painter.restore()

    def wheelEvent(self, event):
        world = self._world(event.position())
        self.scale = max(0.01, min(1000.0, self.scale * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15)))
        self.origin = event.position() - world * self.scale
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            self.drag_position = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_position is not None:
            self.origin += event.position() - self.drag_position
            self.drag_position = event.position()
            self.update()
            event.accept()

    def mouseReleaseEvent(self, event):
        if self.drag_position is not None:
            self.drag_position = None
            self.unsetCursor()
            event.accept()


class ContourSmoothingDialog(QDialog):
    def __init__(self, owner, indices):
        super().__init__(owner)
        self.owner = owner
        self.indices = list(dict.fromkeys(indices))
        self._original = {index: [list(contour) for contour in owner.items[index]["contours"]]
                          for index in self.indices}
        self.preview_index = self.indices[0]
        self.item = owner.items[self.preview_index]
        self.setWindowTitle(tr("平滑切割線"))

        self.object_combo = QComboBox()
        self.object_combo.setVisible(len(self.indices) > 1)
        for index in self.indices:
            item = owner.items[index]
            self.object_combo.addItem(item.get("name", tr("物件 {number}", number=index + 1)), index)
        self.object_combo.currentIndexChanged.connect(self._preview_changed)

        self.tolerance_spin = SafeDoubleSpinBox()
        self.tolerance_spin.setRange(0, 0.5)
        self.tolerance_spin.setDecimals(3)
        self.tolerance_spin.setSingleStep(0.02)
        self.tolerance_spin.setSuffix(" mm")
        self.tolerance_spin.setValue(0.1)
        self.tolerance_spin.valueChanged.connect(self._parameters_changed)

        form = QFormLayout()
        if len(self.indices) > 1:
            form.addRow(tr("預覽物件"), self.object_combo)
        form.addRow(tr("最大偏移"), self.tolerance_spin)

        self.canvas = SmoothingCanvas(self)
        self.fit_button = QPushButton(tr("符合視窗"))
        self.fit_button.clicked.connect(self.canvas.fit)
        self.count_label = QLabel()
        self.legend = QLabel(tr("灰色虛線：原切割線　綠色：平滑後"))
        self.legend.setWordWrap(True)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.fit_button)
        toolbar.addWidget(self.count_label)
        toolbar.addStretch()
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(toolbar)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.legend)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("套用"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_count()
        self.resize(900, 640)

    def build_contours(self):
        """Return new contours for each selected item without changing the owner."""
        tolerance = self.tolerance_spin.value()
        if getattr(self, "_cached_tolerance", None) != tolerance:
            self._cached_contours = {index: smooth_contours(contours, tolerance)
                                     for index, contours in self._original.items()}
            self._cached_tolerance = tolerance
        return self._cached_contours

    def _update_count(self):
        before = sum(map(len, self._original[self.preview_index]))
        after = sum(map(len, self.build_contours()[self.preview_index]))
        self.count_label.setText(tr("節點 {before} → {after}", before=before, after=after))

    def _preview_changed(self, combo_index):
        if combo_index < 0:
            return
        self.preview_index = self.object_combo.itemData(combo_index)
        self.item = self.owner.items[self.preview_index]
        self._update_count()
        self.canvas.fit()

    def _parameters_changed(self, _value):
        self._update_count()
        self.canvas.update()

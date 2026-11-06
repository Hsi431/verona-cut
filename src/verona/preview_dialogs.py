"""Independent preview and artwork configuration dialogs."""

from __future__ import annotations

import copy
import re
import weakref
from pathlib import Path
from typing import Any

from PySide6.QtCore import QLineF, QObject, QPointF, QRectF, Qt, QTemporaryDir, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPalette
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget)

from .editor_widgets import SafeDoubleSpinBox, SafeSpinBox
from .grouping import selection_units as group_units
from .i18n import tr
from .view_constants import (ACCENT, GRID_MAJOR, PAGE_HEIGHT_MM, PAGE_MARGIN,
    PAGE_WIDTH_MM, PAPER)


class PreviewDialog(QDialog):
    def __init__(self, owner: "VeronaWindow") -> None:
        super().__init__(owner)
        self.setWindowTitle(tr("切割預覽"))
        self.resize(760, 700)
        layout = QVBoxLayout(self)
        payload = owner._encoded_plt()
        canvas = PlotPreviewCanvas(payload)
        scroll = QScrollArea()
        scroll.setWidget(canvas)
        scroll.setWidgetResizable(False)
        layout.addWidget(scroll, 1)
        canvas.setToolTip(tr("實線為刀具落下路徑；虛線為抬刀移動路徑。"))
        layout.addWidget(QLabel(tr("實線：切割 · 虛線：移動")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("關閉"))
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


class BleedDialog(QDialog):
    def __init__(self, owner, indices):
        super().__init__(owner)
        self.owner, self.indices = owner, indices
        self.setWindowTitle(tr("設定列印出血"))
        saved = owner.items[indices[0]].get("bleed", {})
        self.color = QColor(saved.get("color", "#ffffff"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("純色出血"), "solid")
        self.mode_combo.addItem(tr("延伸邊緣色"), "edge")
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(saved.get("mode", "solid")))
        self.width_spin, self.inner_spin = SafeDoubleSpinBox(), SafeDoubleSpinBox()
        for spin, maximum, value in ((self.width_spin, 5, saved.get("width_mm", 0)),
                                      (self.inner_spin, 1, saved.get("inner_mm", 0))):
            spin.setRange(0, maximum)
            spin.setDecimals(3)
            spin.setSingleStep(0.05)
            spin.setSuffix(" mm")
            spin.setValue(value)
        self.color_button = QPushButton(tr("出血顏色…"))
        self.color_button.clicked.connect(self.choose_color)
        preview_button = QPushButton(tr("預覽"))
        preview_button.setToolTip(tr("按預覽檢查出血；內側補色會覆蓋邊緣圖案，不移動切線。"))
        preview_button.clicked.connect(self.preview)
        form = QFormLayout()
        form.addRow(tr("出血方式"), self.mode_combo)
        form.addRow(tr("外側出血"), self.width_spin)
        form.addRow(tr("內側補色"), self.inner_spin)
        form.addRow(self.color_button, preview_button)
        self.mode_combo.currentIndexChanged.connect(self._update_mode)
        self.inner_spin.valueChanged.connect(self._update_mode)
        self._update_mode()
        self.inner_spin.setToolTip(tr("內側補色會覆蓋邊緣圖案，不移動切線。"))
        self.preview_label = QLabel(tr("尚未預覽"))
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setWordWrap(True)
        self.preview_label.setMinimumSize(360, 360)
        self.directory = QTemporaryDir()
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.preview_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            tr("套用至 {count} 張圖片", count=len(indices)) if len(indices) > 1 else tr("套用"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        layout.addWidget(buttons)

    def choose_color(self):
        color = QColorDialog.getColor(self.color, self, tr("出血顏色"))
        if color.isValid():
            self.color = color

    def _update_mode(self, *_args):
        self.color_button.setEnabled(self.mode_combo.currentData() == "solid" or self.inner_spin.value() > 0)

    def build_items(self):
        from .editor_actions import build_edit
        return build_edit(self.owner.items, self.owner.metadata, self.indices, "bleed",
            {"width_mm": self.width_spin.value(), "inner_mm": self.inner_spin.value(),
             "color": self.color.name(), "mode": self.mode_combo.currentData()})

    def preview(self):
        from .editor_actions import render_preview
        try:
            items, _, _ = self.build_items()
            path = Path(self.directory.path()) / "preview.png"
            render_preview(items, self.owner.metadata, path)
            self.preview_label.setPixmap(QPixmap(str(path)).scaled(360, 360,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, tr("設定列印出血"), str(error))


class _PackingPreviewSignals(QObject):
    nestingPreview = Signal(int, object, object)


class PackingDialog(QDialog):

    def __init__(self, owner, indices):
        super().__init__(owner)
        self.owner, self.indices, self.plan = owner, indices, None
        self._preview_state = {"generation": 0, "closed": False}
        self._nesting_preview_signals = _PackingPreviewSignals()
        self._nesting_preview_signals.nestingPreview.connect(self._show_nesting_preview)
        preview_state = self._preview_state
        self.finished.connect(lambda *_args, state=preview_state: _close_packing_preview(state))
        self.destroyed.connect(lambda *_args, state=preview_state: _close_packing_preview(state))
        self.setWindowTitle(tr("自動排版"))
        self.copies_spin = SafeSpinBox()
        self.copies_spin.setRange(1, 1000)
        self.fill_check = QCheckBox(tr("繼續填滿剩餘範圍"))
        self.rotate_check = QCheckBox(tr("允許旋轉"))
        self.shape_check = QCheckBox(tr("依實際輪廓排列"))
        self.pages_check = QCheckBox(tr("放不下的份數另開新分頁"))
        self.rotation_spin = SafeSpinBox()
        self.rotation_spin.setRange(5, 90)
        self.rotation_spin.setSingleStep(5)
        self.rotation_spin.setSuffix("°")
        self.rotation_spin.setValue(90)
        self.rotation_spin.setEnabled(False)
        self.time_limit_spin = SafeDoubleSpinBox()
        self.time_limit_spin.setRange(.1, 12)
        self.time_limit_spin.setSingleStep(.5)
        self.time_limit_spin.setSuffix(" s")
        self.time_limit_spin.setValue(12)
        self.gap_spin, self.margin_spin = SafeDoubleSpinBox(), SafeDoubleSpinBox()
        for spin, maximum, value in ((self.gap_spin, 300, 2), (self.margin_spin, 150, 0)):
            spin.setRange(0, maximum)
            spin.setSuffix(" mm")
            spin.setValue(value)
        form = QFormLayout()
        form.addRow(tr("每組份數"), self.copies_spin)
        form.addRow(tr("間距"), self.gap_spin)
        form.addRow(tr("留邊"), self.margin_spin)
        form.addRow(self.rotate_check)
        form.addRow(tr("旋轉間隔"), self.rotation_spin)
        form.addRow(tr("搜尋時間上限"), self.time_limit_spin)
        form.addRow(self.shape_check)
        form.addRow(self.fill_check)
        form.addRow(self.pages_check)
        self.preview_button = QPushButton(tr("預覽"))
        self.preview_button.setToolTip(tr("先預覽排版，再套用；圖案維持原尺寸。"))
        self.preview_button.clicked.connect(self.preview)
        self.preview_label = QLabel(tr("尚未預覽"))
        self.preview_label.setMinimumSize(360, 360)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setWordWrap(True)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.hide()
        self.directory = QTemporaryDir()
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.preview_button)
        layout.addWidget(self.preview_label)
        layout.addWidget(self.summary)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText(tr("套用"))
        self.apply_button.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        layout.addWidget(buttons)
        for spin in (self.copies_spin, self.gap_spin, self.margin_spin,
                     self.rotation_spin, self.time_limit_spin):
            spin.valueChanged.connect(self.invalidate)
        for check in (self.fill_check, self.rotate_check, self.shape_check, self.pages_check):
            check.toggled.connect(self.invalidate)
        self.rotate_check.toggled.connect(self._rotation_controls_changed)
        self.shape_check.toggled.connect(self._rotation_controls_changed)

    def invalidate(self, *_args):
        self._preview_state["generation"] += 1
        self.plan = None
        self.preview_button.setEnabled(True)
        self.apply_button.setEnabled(False)
        self.summary.setText(tr("設定已變更，請重新預覽。"))
        self.summary.show()

    def _rotation_controls_changed(self, *_args):
        self.rotation_spin.setEnabled(self.rotate_check.isChecked() and self.shape_check.isChecked())

    def _parameters(self, items=None, indices=None):
        items = self.owner.items if items is None else items
        indices = self.indices if indices is None else indices
        units = group_units(items, indices)
        return {"copies_per_unit": [self.copies_spin.value()]*len(units),
                "gap_mm": self.gap_spin.value(), "margin_mm": self.margin_spin.value(),
                "allow_rotate": self.rotate_check.isChecked(), "shape_aware": self.shape_check.isChecked(),
                "rotation_step_deg": self.rotation_spin.value(),
                "time_limit_seconds": self.time_limit_spin.value(),
                "new_page_for_unplaced": self.pages_check.isChecked(),
                "fill": self.fill_check.isChecked()}

    def build_items(self):
        from .packing import pack_items
        if self.plan is None:
            self.plan = pack_items(self.owner.items, self.owner.metadata, self.indices,
                                       self._parameters())
        return self.plan

    @Slot(int, object, object)
    def _show_nesting_preview(self, generation, plan, metadata):
        if (self._preview_state["closed"]
                or self._preview_state["generation"] != generation):
            return
        self._present_plan(generation, plan, metadata, searching=True)

    def _present_plan(self, generation, plan, metadata, *, searching):
        if (self._preview_state["closed"]
                or self._preview_state["generation"] != generation):
            return
        from .editor_actions import render_preview
        try:
            items, _, details = plan
            path = Path(self.directory.path()) / "preview.png"
            render_preview(items, metadata, path)
            self.preview_label.setPixmap(QPixmap(str(path)).scaled(360, 360,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            text = tr("共 {copies} 組副本；{objects} 個物件。", copies=details["copies"], objects=len(items))
            text += " " + tr("紙張使用率 {percent:.1f}%。", percent=details["paper_utilization_percent"])
            if details["page_count"] > 1:
                text += " " + tr("共 {pages} 個分頁。", pages=details["page_count"])
            if details["unplaced_count"]:
                names = sorted({name for entry in details["unplaced"] for name in entry["objects"]})
                text += " " + tr("放不下 {count} 份：{objects}", count=details["unplaced_count"],
                                 objects="、".join(names[:8]))
            if details["limit_reached"]:
                text += " " + tr("已達本次排版上限。")
            if searching:
                text += " " + tr("排版中…")
        except (ValueError, OSError) as error:
            self.plan = None
            self.apply_button.setEnabled(False)
            if not searching:
                self.preview_button.setEnabled(True)
            QMessageBox.warning(self, tr("自動排版"), str(error))
            return
        self.plan = plan
        self.summary.setText(text)
        self.summary.show()
        self.preview_button.setEnabled(not searching)
        self.apply_button.setEnabled(True)

    def preview(self):
        if self._preview_state["closed"]:
            return
        from .packing import pack_items
        state = self._preview_state
        state["generation"] += 1
        generation = state["generation"]
        self.plan = None
        try:
            items = copy.deepcopy(self.owner.items)
            metadata = copy.deepcopy(self.owner.metadata)
            indices = list(self.indices)
            parameters = copy.deepcopy(self._parameters(items, indices))
        except (ValueError, OSError) as error:
            self.apply_button.setEnabled(False)
            QMessageBox.warning(self, tr("自動排版"), str(error))
            return

        dialog_ref = weakref.ref(self)

        def current_dialog():
            if state["closed"] or state["generation"] != generation:
                return None
            return dialog_ref()

        def succeeded(plan):
            dialog = current_dialog()
            if dialog is None:
                return
            dialog._present_plan(generation, plan, metadata, searching=False)

        def failed(error):
            dialog = current_dialog()
            if dialog is None:
                return
            dialog.plan = None
            dialog.apply_button.setEnabled(False)
            dialog.preview_button.setEnabled(True)
            QMessageBox.warning(dialog, tr("自動排版"), error)

        self.preview_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.summary.setText(tr("排版中…"))
        self.summary.show()

        def pack():
            first_plan = pack_items(items, metadata, indices, parameters)
            if parameters["new_page_for_unplaced"]:
                return first_plan
            try:
                from .nesting_engine import (layout_is_better, nesting_available,
                                             pack_with_nesting)
                if (not nesting_available()
                        or parameters["time_limit_seconds"] < 2):
                    return first_plan
                preview_signal.nestingPreview.emit(generation, first_plan, metadata)
                candidate = pack_with_nesting(items, metadata, indices, parameters)
                if (candidate is not None
                        and layout_is_better(candidate, first_plan, metadata, parameters)):
                    return candidate
            except (ImportError, OSError, RuntimeError, TypeError, ValueError):
                return first_plan
            return first_plan

        preview_signal = self._nesting_preview_signals
        self.owner._dispatch(pack, succeeded, failed)

    def close(self):
        _close_packing_preview(self._preview_state)
        return super().close()

    def closeEvent(self, event):
        _close_packing_preview(self._preview_state)
        super().closeEvent(event)


def _close_packing_preview(state):
    if not state["closed"]:
        state["closed"] = True
        state["generation"] += 1


class PlotPreviewCanvas(QWidget):
    """Draw the actual U/D move sequence produced by the PLT encoder."""

    def __init__(self, payload: bytes) -> None:
        super().__init__()
        self.zoom = 1.8
        text = payload.decode("ascii")
        size = re.search(r"FSIZE(\d+),(\d+)", text)
        self.feed_max = max(PAGE_HEIGHT_MM, int(size.group(1)) / 40 if size else PAGE_HEIGHT_MM)
        self.moves: list[tuple[str, QPointF]] = []
        for mode, feed, cross in re.findall(r"([UD])(\d+),(\d+)", text):
            self.moves.append((mode, QPointF(int(cross) / 40, int(feed) / 40)))
        # Group segments by pen once; a QPen and drawLine call per move made
        # every repaint (including scrolling) slow for large jobs.
        self.lines = {"U": [], "D": []}
        current = QPointF(0, 0)
        for mode, point in self.moves:
            self.lines[mode].append(QLineF(current, point))
            current = point
        self.setMinimumSize(int(PAGE_WIDTH_MM * self.zoom + 68),
                            int(self.feed_max * self.zoom + 68))

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Shadow))
        painter.save()
        painter.translate(PAGE_MARGIN, PAGE_MARGIN)
        painter.scale(self.zoom, self.zoom)
        painter.setPen(QPen(QColor("#c7d1cc"), 0.55))
        painter.setBrush(PAPER)
        painter.drawRect(QRectF(0, 0, PAGE_WIDTH_MM, self.feed_max))
        painter.setPen(QPen(GRID_MAJOR, 0.25))
        for x in range(0, 301, 10):
            painter.drawLine(QPointF(x, 0), QPointF(x, self.feed_max))
        for y in range(0, int(self.feed_max) + 1, 10):
            painter.drawLine(QPointF(0, y), QPointF(PAGE_WIDTH_MM, y))

        painter.setPen(QPen(QColor("#b87531"), 0.55, Qt.PenStyle.DashLine))
        painter.drawLines(self.lines["U"])
        painter.setPen(QPen(ACCENT, 0.85, Qt.PenStyle.SolidLine))
        painter.drawLines(self.lines["D"])
        painter.restore()

"""Focused node editing with document-mm fields and direct handle manipulation."""
import copy

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QTransform
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from .editor_widgets import SafeDoubleSpinBox
from .i18n import tr
from .nodes import editable_path, edit_path, insert_node, move_node


class NodeCanvas(QWidget):
    def __init__(self, dialog):
        super().__init__()
        self.dialog, self.drag = dialog, None
        self.setMinimumSize(480, 400)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(tr("節點預覽"))
        self.fit()

    def fit(self):
        from .artwork import contour_path
        bounds = contour_path(self.dialog.item["contours"]).boundingRect()
        points = [point for node in self.dialog.path["nodes"]
                  for key in ("point", "in", "out") if (point := node.get(key)) is not None]
        bounds = bounds.united(QRectF(QPointF(min(p[0] for p in points), min(p[1] for p in points)),
                                     QPointF(max(p[0] for p in points), max(p[1] for p in points))))
        self.scale = min(max(1, self.width()-64)/max(1, bounds.width()), max(1, self.height()-64)/max(1, bounds.height()))
        self.origin = QPointF(self.width()/2-bounds.center().x()*self.scale, self.height()/2-bounds.center().y()*self.scale)
        self.update()

    def resizeEvent(self, event):
        self.fit()
        super().resizeEvent(event)

    def screen(self, point):
        return QPointF(*point)*self.scale+self.origin

    def world(self, point):
        p = (point-self.origin)/self.scale
        return (p.x(), p.y())

    def paintEvent(self, _event):
        from .ui import PAPER, ACCENT, _qoi_image
        from .artwork import contour_path
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), PAPER)
        painter.save()
        painter.translate(self.origin)
        painter.scale(self.scale, self.scale)
        item = self.dialog.item
        if "image_data" in item:
            painter.save()
            painter.setOpacity(.4)
            if item.get("image_clip"):
                painter.setClipPath(contour_path(item["image_clip"]))
            tx, ty, a, b, c, d = item["image_transform"]
            painter.setTransform(QTransform(a, b, c, d, tx, ty), True)
            painter.drawImage(QPointF(0, 0), _qoi_image(item["image_data"]))
            painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for i, contour in enumerate(item["contours"]):
            pen = QPen(ACCENT if i == self.dialog.contour_index else QColor("#aebbb4"), 2 if i == self.dialog.contour_index else 1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(contour_path([contour]))
        painter.restore()
        for i, node in enumerate(self.dialog.path["nodes"]):
            p = self.screen(node["point"])
            if i == self.dialog.node_index:
                for key in ("in", "out"):
                    if node.get(key) is not None:
                        target = self.screen(node[key])
                        painter.setPen(QPen(ACCENT, 1, Qt.PenStyle.DashLine))
                        painter.drawLine(p, target)
                        painter.setBrush(PAPER)
                        painter.drawEllipse(target, 5, 5)
            painter.setPen(QPen(ACCENT, 1))
            painter.setBrush(ACCENT if i == self.dialog.node_index else PAPER)
            painter.drawEllipse(p, 5, 5)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        candidates = []
        for i, node in enumerate(self.dialog.path["nodes"]):
            for key in ("point", "in", "out") if i == self.dialog.node_index else ("point",):
                if node.get(key) is not None:
                    distance = (self.screen(node[key])-event.position()).manhattanLength()
                    if distance <= 12:
                        candidates.append((distance, i, key))
        if candidates:
            _, i, key = min(candidates)
            self.dialog.node_combo.setCurrentIndex(i)
            self.drag = (i, key)
            self.setFocus()
            self.update()

    def mouseMoveEvent(self, event):
        if self.drag is not None:
            i, key = self.drag
            point = self.world(event.position())
            if key == "point":
                move_node(self.dialog.path, i, point)
            else:
                self.dialog.path["nodes"][i][key] = point
            self.dialog.changed()

    def mouseReleaseEvent(self, _event):
        self.drag = None

    def wheelEvent(self, event):
        old = self.world(event.position())
        self.scale = max(.01, min(1000., self.scale*(1.2 if event.angleDelta().y() > 0 else 1/1.2)))
        self.origin = event.position()-QPointF(*old)*self.scale
        self.update()
        event.accept()

    def keyPressEvent(self, event):
        delta = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}.get(event.key())
        if delta:
            p = self.dialog.path["nodes"][self.dialog.node_index]["point"]
            step = 1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else .1
            move_node(self.dialog.path, self.dialog.node_index, tuple(v+d*step for v, d in zip(p, delta)))
            self.dialog.changed()
        else:
            super().keyPressEvent(event)


class NodeEditorDialog(QDialog):
    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("編輯節點與曲線"))
        self.item = copy.deepcopy(item)
        self.contour_index = self.node_index = 0
        self.path = editable_path(self.item, 0)
        self.contour_combo, self.node_combo = QComboBox(), QComboBox()
        self.contour_combo.addItems([str(i+1) for i in range(len(item["contours"]))])
        self.closed = QCheckBox(tr("封閉路徑"))
        self.canvas = NodeCanvas(self)
        form = QFormLayout()
        form.addRow(tr("輪廓"), self.contour_combo)
        form.addRow(tr("節點"), self.node_combo)
        form.addRow(self.closed)
        self.fields, self.handles = {}, {}
        for key, label in (("point", "點位"), ("in", "進入把手"), ("out", "離開把手")):
            row = QHBoxLayout()
            for axis in (0, 1):
                spin = SafeDoubleSpinBox()
                spin.setDecimals(4)
                spin.setRange(-10000, 10000)
                spin.setSingleStep(.1)
                spin.setPrefix("X " if axis == 0 else "Y ")
                spin.setSuffix(" mm")
                spin.valueChanged.connect(self.fields_changed)
                self.fields[key, axis] = spin
                row.addWidget(spin)
            if key != "point":
                check = QCheckBox(tr("啟用"))
                check.toggled.connect(self.fields_changed)
                self.handles[key] = check
                row.addWidget(check)
            form.addRow(tr(label), row)
        add, remove, fit = QPushButton(tr("插入中點")), QPushButton(tr("刪除節點")), QPushButton(tr("符合預覽"))
        add.clicked.connect(self.insert)
        remove.clicked.connect(self.remove)
        fit.clicked.connect(self.canvas.fit)
        operations = QHBoxLayout()
        operations.addWidget(add)
        operations.addWidget(remove)
        form.addRow(operations)
        form.addRow(fit)
        hint = QLabel(tr("拖曳點位或把手；滾輪縮放。方向鍵移動 0.1 mm，Shift 為 1 mm。"))
        hint.setWordWrap(True)
        self.error = QLabel()
        self.error.setWordWrap(True)
        panel = QVBoxLayout()
        panel.addLayout(form)
        panel.addWidget(hint)
        panel.addStretch()
        panel.addWidget(self.error)
        body = QHBoxLayout()
        body.addWidget(self.canvas, 1)
        body.addLayout(panel)
        layout = QVBoxLayout(self)
        layout.addLayout(body)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText(tr("套用"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.contour_combo.currentIndexChanged.connect(self.change_contour)
        self.node_combo.currentIndexChanged.connect(self.select_node)
        self.closed.toggled.connect(self.fields_changed)
        self.reload_nodes()
        self.resize(920, 560)

    def reload_nodes(self):
        self.node_combo.blockSignals(True)
        self.node_combo.clear()
        self.node_combo.addItems([str(i+1) for i in range(len(self.path["nodes"]))])
        self.node_index = min(self.node_index, len(self.path["nodes"])-1)
        self.node_combo.setCurrentIndex(self.node_index)
        self.node_combo.blockSignals(False)
        self.select_node(self.node_index)

    def change_contour(self, index):
        self.contour_index, self.node_index = index, 0
        self.path = editable_path(self.item, index)
        self.error.clear()
        self.apply_button.setEnabled(True)
        self.reload_nodes()

    def select_node(self, index):
        self.node_index = index
        node = self.path["nodes"][index]
        self.closed.blockSignals(True)
        self.closed.setChecked(self.path["closed"])
        self.closed.blockSignals(False)
        for key in ("point", "in", "out"):
            point = node.get(key) or node["point"]
            if key in self.handles:
                self.handles[key].blockSignals(True)
                self.handles[key].setChecked(node.get(key) is not None)
                self.handles[key].blockSignals(False)
            for axis in (0, 1):
                spin = self.fields[key, axis]
                spin.blockSignals(True)
                spin.setValue(point[axis])
                spin.setEnabled(key == "point" or node.get(key) is not None)
                spin.blockSignals(False)
        self.canvas.update()

    def fields_changed(self, *_args):
        node = self.path["nodes"][self.node_index]
        move_node(self.path, self.node_index, tuple(self.fields["point", i].value() for i in (0, 1)))
        for key, check in self.handles.items():
            if self.sender() in (check, self.fields[key, 0], self.fields[key, 1]):
                node[key] = tuple(self.fields[key, i].value() for i in (0, 1)) if check.isChecked() else None
        self.path["closed"] = self.closed.isChecked()
        self.changed()

    def changed(self):
        try:
            candidate = edit_path(self.item, self.contour_index, self.path)
            if sum(map(len, candidate["contours"])) > 2_000_000:
                raise ValueError(tr("曲線細分超過上限，請縮小曲線或簡化節點。"))
            self.item = candidate
            self.error.clear()
            self.apply_button.setEnabled(True)
        except ValueError as error:
            self.error.setText(str(error))
            self.apply_button.setEnabled(False)
        self.select_node(self.node_index)

    def insert(self):
        try:
            insert_node(self.path, self.node_index)
            self.node_index += 1
            self.reload_nodes()
            self.changed()
        except ValueError as error:
            self.error.setText(str(error))

    def remove(self):
        if len(self.path["nodes"]) <= (3 if self.path["closed"] else 2):
            self.error.setText(tr("開放路徑至少 2 點、封閉路徑至少 3 點，最多 10000 點。"))
            return
        self.path["nodes"].pop(self.node_index)
        self.reload_nodes()
        self.changed()

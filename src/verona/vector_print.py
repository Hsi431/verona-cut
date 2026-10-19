"""Explicit vector print styling; separate from cutting inclusion."""
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QDialog, QDialogButtonBox,
    QFormLayout, QLabel, QPushButton, QVBoxLayout)
from .editor_widgets import SafeDoubleSpinBox
from .i18n import tr


class VectorPrintDialog(QDialog):
    def __init__(self, style=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("設定向量列印"))
        style = style or {"fill": "#202020", "stroke": None, "width_mm": .2}
        self.colors = {key: QColor(style[key] or "#202020") for key in ("fill", "stroke")}
        self.checks, self.buttons = {}, {}
        form = QFormLayout()
        for key, label in (("fill", "列印填色"), ("stroke", "列印線條")):
            check, button = QCheckBox(tr(label)), QPushButton()
            check.setChecked(style[key] is not None)
            self.checks[key], self.buttons[key] = check, button
            button.setText(self.colors[key].name())
            button.setEnabled(check.isChecked())
            check.toggled.connect(button.setEnabled)
            button.clicked.connect(lambda _checked=False, role=key: self.choose_color(role))
            form.addRow(check, button)
        self.width_spin = SafeDoubleSpinBox()
        self.width_spin.setDecimals(2);self.width_spin.setRange(.01, 5);self.width_spin.setSuffix(" mm")
        self.width_spin.setValue(style["width_mm"])
        self.width_spin.setEnabled(self.checks["stroke"].isChecked())
        self.checks["stroke"].toggled.connect(self.width_spin.setEnabled)
        form.addRow(tr("列印線寬"), self.width_spin)
        hint = QLabel(tr("列印樣式不改切割線；取消兩項勾選可恢復僅切割。變更後請重印。"))
        hint.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addLayout(form);layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("套用"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(400, 240)

    def choose_color(self, role):
        color = QColorDialog.getColor(self.colors[role], self)
        if color.isValid():
            self.colors[role] = color
            self.buttons[role].setText(color.name())

    def print_style(self):
        return {"fill": self.colors["fill"].name() if self.checks["fill"].isChecked() else None,
                "stroke": self.colors["stroke"].name() if self.checks["stroke"].isChecked() else None,
                "width_mm": self.width_spin.value()}

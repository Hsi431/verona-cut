"""Reusable widgets for the editor inspector."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QAbstractSpinBox, QComboBox, QDoubleSpinBox, QSpinBox, QToolButton, QVBoxLayout, QWidget, QSizePolicy


class WheelGuard(QObject):
    """Keep scrolling from changing controls, including those in Qt dialogs."""

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and isinstance(watched, (QComboBox, QAbstractSpinBox)):
            event.ignore()
            parent = watched.parentWidget()
            while parent is not None and not isinstance(parent, QAbstractScrollArea):
                parent = parent.parentWidget()
            if parent is not None:
                QApplication.sendEvent(parent.viewport(), event)
            return True
        return False


class SafeSpinBox(QSpinBox):
    """A spin box that leaves wheel scrolling to its parent."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class SafeDoubleSpinBox(QDoubleSpinBox):
    """A double spin box that leaves wheel scrolling to its parent."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class CollapsibleSection(QWidget):
    """A titled section that shows or hides a supplied content widget."""

    expandedChanged = Signal(bool)

    def __init__(self, title: str, content: QWidget, expanded: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._content = content
        self._toggle = QToolButton(self)
        self._toggle.setObjectName("sectionToggle")
        self._toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._toggle.setText(title)
        self._toggle.setCheckable(True)
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.clicked.connect(self.setExpanded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._toggle)
        layout.addWidget(content)
        self.setExpanded(expanded)

    def setExpanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        self._toggle.setChecked(expanded)
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self._content.setVisible(expanded)
        self.expandedChanged.emit(expanded)

    def isExpanded(self) -> bool:
        return self._toggle.isChecked()

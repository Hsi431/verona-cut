"""Read-only screenshots and widget inspection for a live Verona window."""
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QApplication,
                               QComboBox, QDialog, QLabel, QLineEdit, QMenu,
                               QPlainTextEdit, QTextEdit, QWidget)


MAX_INSPECT_NODES = 500
MAX_TEXT_LENGTH = 240


def _dialog_for(window):
    app = QApplication.instance()
    modal = app.activeModalWidget() if app else None
    if isinstance(modal, QDialog) and _belongs_to(window, modal):
        return modal
    active = app.activeWindow() if app else None
    if isinstance(active, QDialog) and _belongs_to(window, active):
        return active
    dialogs = [dialog for dialog in window.findChildren(QDialog)
               if dialog.isVisible() and _belongs_to(window, dialog)]
    return dialogs[-1] if dialogs else None


def _belongs_to(window, widget):
    parent = widget
    while parent is not None:
        if parent is window:
            return True
        parent = parent.parentWidget()
    return False


def _target_widget(window, target):
    if target == "window":
        return window
    if target == "canvas":
        return window.canvas
    if target == "active_dialog":
        dialog = _dialog_for(window)
        if dialog is None:
            raise ValueError("No open dialog is available for active_dialog.")
        return dialog
    raise ValueError("target must be window, active_dialog or canvas.")


def screenshot(window, directory, editor_id, target="window", max_width_px=1600):
    if type(max_width_px) is not int or not 1 <= max_width_px <= 4096:
        raise ValueError("max_width_px must be an integer from 1 to 4096.")
    widget = _target_widget(window, target)
    pixmap = widget.grab()
    if pixmap.isNull() or pixmap.width() < 1 or pixmap.height() < 1:
        raise ValueError(f"Could not capture the {target} widget.")
    if pixmap.width() > max_width_px:
        height = max(1, round(pixmap.height() * max_width_px / pixmap.width()))
        pixmap = pixmap.scaled(max_width_px, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
    path = Path(directory) / f"{editor_id}.{target}.ui.png"
    if not pixmap.save(str(path), "PNG"):
        raise OSError("Could not save the UI screenshot.")
    return {"path": str(path), "target": target, "width_px": pixmap.width(),
            "height_px": pixmap.height(), "format": "png"}


def _clip(value):
    value = str(value or "")
    if len(value) > MAX_TEXT_LENGTH:
        return value[:MAX_TEXT_LENGTH] + "…"
    return value


def _widget_text(widget):
    if isinstance(widget, QDialog):
        return widget.windowTitle()
    if isinstance(widget, QAbstractButton):
        return widget.text()
    if isinstance(widget, QLabel):
        return widget.text()
    if isinstance(widget, QLineEdit):
        return widget.text()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QAbstractSpinBox):
        return widget.lineEdit().text()
    if isinstance(widget, QTextEdit | QPlainTextEdit):
        return widget.toPlainText()
    if isinstance(widget, QMenu):
        return widget.title()
    return ""


def _widget_value(widget):
    if isinstance(widget, QLineEdit):
        return widget.text()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QAbstractSpinBox):
        return widget.value()
    if isinstance(widget, QTextEdit | QPlainTextEdit):
        return widget.toPlainText()
    return None


def _command_id(value):
    if not isinstance(value, str):
        return None
    from .command_registry import COMMANDS_BY_ID
    return value if value in COMMANDS_BY_ID else None


def _rect_for(widget, target):
    top_left = widget.mapTo(target, QPoint(0, 0))
    return QRect(top_left, widget.size())


def inspect(window, target="window", max_depth=8):
    if type(max_depth) is not int or not 0 <= max_depth <= 16:
        raise ValueError("max_depth must be an integer from 0 to 16.")
    root = _target_widget(window, target)
    state = {"count": 0, "truncated": False}

    def visit(widget, depth):
        if state["count"] >= MAX_INSPECT_NODES:
            state["truncated"] = True
            return None
        state["count"] += 1
        text = _clip(_widget_text(widget))
        checked = widget.isChecked() if isinstance(widget, QAbstractButton) and widget.isCheckable() else None
        command = _command_id(widget.property("verona_command_id"))
        default_action = widget.defaultAction() if hasattr(widget, "defaultAction") else None
        if command is None and default_action is not None:
            command = _command_id(default_action.data())
        rect = _rect_for(widget, root)
        node = {"type": type(widget).__name__, "objectName": widget.objectName(),
                "text": text, "enabled": widget.isEnabled(), "visible": widget.isVisible(),
                "checked": checked,
                "geometry": {"x": rect.x(), "y": rect.y(), "width": rect.width(),
                             "height": rect.height()}, "children": []}
        value = _widget_value(widget)
        if value is not None:
            node["value"] = _clip(value)
        if command is not None:
            node["command_id"] = command
        if depth >= max_depth:
            if (any(isinstance(child, QWidget) and child.isVisible() for child in widget.children())
                    or isinstance(widget, QMenu) and any(
                        action.isVisible() and not action.isSeparator() for action in widget.actions())):
                state["truncated"] = True
            return node
        for child in widget.children():
            if isinstance(child, QWidget) and child.isVisible():
                child_node = visit(child, depth + 1)
                if child_node is not None:
                    node["children"].append(child_node)
                if state["count"] >= MAX_INSPECT_NODES:
                    if _visible_widget_children(widget, child):
                        state["truncated"] = True
                    break
        if isinstance(widget, QMenu):
            for action in widget.actions():
                if not action.isVisible() or action.isSeparator():
                    continue
                if state["count"] >= MAX_INSPECT_NODES:
                    state["truncated"] = True
                    break
                state["count"] += 1
                action_rect = widget.actionGeometry(action)
                mapped = widget.mapTo(root, action_rect.topLeft())
                action_node = {"type": "QAction", "objectName": action.objectName(),
                               "text": _clip(action.text()), "enabled": action.isEnabled(),
                               "visible": action.isVisible(), "checked": action.isChecked()
                               if action.isCheckable() else None,
                               "geometry": {"x": mapped.x(), "y": mapped.y(),
                                            "width": action_rect.width(),
                                            "height": action_rect.height()}, "children": []}
                command_id = _command_id(action.data())
                if command_id is not None:
                    action_node["command_id"] = command_id
                node["children"].append(action_node)
        return node

    return {"target": target, "root": visit(root, 0), "node_count": state["count"],
            "max_nodes": MAX_INSPECT_NODES, "truncated": state["truncated"]}


def _visible_widget_children(widget, after):
    children = widget.children()
    try:
        index = children.index(after)
    except ValueError:
        index = -1
    return any(isinstance(child, QWidget) and child.isVisible() for child in children[index + 1:])


def status(window):
    tabs = [{"index": index, "title": window.tab_bar.tabText(index),
             "active": index == window._active_document}
            for index in range(window.tab_bar.count())]
    warning = window.bounds_warning
    dialog = _dialog_for(window)
    return {"tabs": tabs, "active_tab": window._active_document,
            "status_text": window.status_label.text(),
            "bounds_warning": not warning.isHidden(),
            "bounds_warning_text": warning.text() if not warning.isHidden() else "",
            "sending": bool(window.sending),
            "active_dialog_title": dialog.windowTitle() if dialog else None}

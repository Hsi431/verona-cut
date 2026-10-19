"""Qt Widgets interface for the Linux Verona design workspace."""

from __future__ import annotations

import copy
import argparse
import base64
import binascii
import ipaddress
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QRunnable, QRectF, Qt, QTemporaryDir, QThreadPool, QSettings, Signal, Slot
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QCursor, QFont, QImage, QPainter, QPainterPath, QPainterPathStroker, QPen, QPixmap,
    QTransform, QPalette,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QInputDialog, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QDoubleSpinBox, QSplitter, QToolButton, QVBoxLayout, QWidget, QTabBar,
)
from .i18n import get_language, set_language, tr
from .transport import TCP_PORT
from . import commands
from .layers import cuttable, editable, printable
from .device_lock import device_lock
from .editor_widgets import CollapsibleSection, SafeDoubleSpinBox, SafeSpinBox
from .themes import THEMES, palette as theme_palette, stylesheet as theme_stylesheet
from .grouping import expand_selection, group_items, ungroup_items, remap_group_ids, selection_units as group_units


PAGE_WIDTH_MM = 300.0
PAGE_HEIGHT_MM = 300.0
PAGE_MARGIN = 34.0
DEFAULT_ZOOM = 1.8
_MAX_CANVAS_EDGE_BYTES = 128 * 1024 * 1024

ACCENT = QColor("#087e72")
PAPER = QColor("#fffefa")
GRID = QColor("#e8eeea")
GRID_MAJOR = QColor("#d3ddd8")


def _path_from_contours(contours: list[list[tuple[float, float]]]) -> QPainterPath:
    path = QPainterPath()
    for contour in contours:
        if not contour:
            continue
        path.moveTo(*contour[0])
        for point in contour[1:]:
            path.lineTo(*point)
        if len(contour) > 2 and contour[0] == contour[-1]:
            path.closeSubpath()
    return path


def _flatten(path: QPainterPath) -> list[list[tuple[float, float]]]:
    result: list[list[tuple[float, float]]] = []
    precise = QTransform.fromScale(20, 20).map(path)
    for polygon in precise.toSubpathPolygons():
        points = [(float(point.x()) / 20, float(point.y()) / 20) for point in polygon]
        if len(points) >= 2:
            result.append(points)
    return result


def _item_path(item: dict[str, Any]) -> QPainterPath:
    return _path_from_contours(item.get("contours", []))


def _copy_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Validated points are immutable tuples. Detach contour lists without
    # revisiting every coordinate in every undo snapshot.
    memo = {id(contour): list(contour) for item in items
            for key in ("contours", "image_clip") for contour in item.get(key, [])}
    return copy.deepcopy(items, memo)


def _copy_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(snapshot, {id(snapshot["items"]): _copy_items(snapshot["items"])})


def _qoi_image(encoded: str) -> QImage:
    from .raster_import import decode_embedded_image
    return decode_embedded_image(encoded)


def _transform_item(item: dict[str, Any], transform: QTransform,
                    source: dict[str, Any] | None = None) -> None:
    source = source or item
    a, b, c, d = transform.m11(), transform.m12(), transform.m21(), transform.m22()
    tx, ty = transform.dx(), transform.dy()
    for key in ("contours", "image_clip"):
        if key in source:
            item[key] = [[(a*x + c*y + tx, b*x + d*y + ty) for x, y in contour]
                         for contour in source[key]]
    if source.get("node_paths"):
        from .nodes import flatten_path
        paths = copy.deepcopy(source["node_paths"])
        for key, path in paths.items():
            for node in path["nodes"]:
                for role in ("point", "in", "out"):
                    if node.get(role) is not None:
                        point = transform.map(QPointF(*node[role]))
                        node[role] = (point.x(), point.y())
            item["contours"][int(key)] = flatten_path(path)
        item["node_paths"] = paths
    if "image_transform" in source:
        tx, ty, a, b, c, d = source["image_transform"]
        origin = transform.map(QPointF(tx, ty))
        x_basis = transform.map(QPointF(tx + a, ty + b))
        y_basis = transform.map(QPointF(tx + c, ty + d))
        item["image_transform"] = [origin.x(), origin.y(), x_basis.x() - origin.x(),
                                   x_basis.y() - origin.y(), y_basis.x() - origin.x(),
                                   y_basis.y() - origin.y()]


def _trust_key(item: Any):
    try:
        key = item["name"], len(item["contours"]), tuple(item["contours"][0][0])
        hash(key)
        return key
    except (TypeError, KeyError, IndexError):
        return None


def _display_name(name: str) -> str:
    return tr(name) if name in {"矩形", "圓形", "文字輪廓", "路徑"} else name


class DesignCanvas(QWidget):
    selectionChanged = Signal(int)
    geometryChanged = Signal()

    def __init__(self, owner: "VeronaWindow", *, interactive: bool = True) -> None:
        super().__init__()
        self.owner = owner
        self.zoom = DEFAULT_ZOOM
        self.interactive = interactive
        self.drag_start: QPointF | None = None
        self.drag_index = -1
        self.drag_before: dict[str, Any] | None = None
        self.drag_path: QPainterPath | None = None
        self._path_cache = {}
        self.drag_duplicate = False
        self.drag_moved = False
        self.drag_handle = None
        self.drag_bounds = None
        self.handle_points = {}
        self.pan_start = None
        self.pan_scroll = None
        self.space_pressed = False
        self.marquee_start = self.marquee_end = None
        self.marquee_base = set()
        self.marquee_before = set()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setToolTip(tr("拖曳四角縮放、上方圓點旋轉；Shift 旋轉每次 15°；Esc 取消拖曳。"))
        self._resize_to_page()

    def _resize_to_page(self) -> None:
        self.setMinimumSize(
            int(PAGE_WIDTH_MM * self.zoom + PAGE_MARGIN * 2),
            int(PAGE_HEIGHT_MM * self.zoom + PAGE_MARGIN * 2),
        )

    def set_zoom(self, value: float) -> None:
        self.zoom = max(0.1, min(64.0, value))
        self._resize_to_page()
        self.update()

    def _world_point(self, point: QPointF) -> QPointF:
        return QPointF((point.x() - PAGE_MARGIN) / self.zoom,
                       (point.y() - PAGE_MARGIN) / self.zoom)

    def item_path(self, item: dict[str, Any], key: str = "contours") -> QPainterPath:
        contours = item.get(key, [])
        cache_key = (id(item), key)
        cached = self._path_cache.get(cache_key)
        if cached is None or cached[0] is not contours:
            cached = (contours, _path_from_contours(contours))
            self._path_cache[cache_key] = cached
        return cached[1]

    def item_bleed_path(self, item: dict[str, Any], *, inner=False) -> QPainterPath:
        from .artwork import bleed_path, inner_bleed_path
        contours = item["contours"]
        settings = item["bleed"].get("inner_mm", 0) if inner else item["bleed"]["width_mm"]
        key = (id(item), "inner_bleed" if inner else "bleed")
        cached = self._path_cache.get(key)
        if cached is None or cached[0] is not contours or cached[1] != settings:
            cached = (contours, settings, (inner_bleed_path if inner else bleed_path)(item))
            self._path_cache[key] = cached
        return cached[2]

    def item_edge_bleed(self, item):
        from .edge_bleed import edge_bleed_image, moved_edge_layer
        contours, clip, data = item["contours"], item.get("image_clip"), item["image_data"]
        transform, width = tuple(item["image_transform"]), item["bleed"]["width_mm"]
        key = (id(item), "edge_bleed")
        cached = self._path_cache.pop(key, None)
        if (cached is not None and cached["contours"] is contours and cached["clip_ref"] is clip
                and cached["image_data"] is data and cached["transform"] == transform
                and cached["width"] == width):
            self._path_cache[key] = cached
            return cached["layer"]
        # A drag moves items rigidly on every mouse event; map the layer computed
        # for the base position instead of re-running the distance transform.
        base = cached and cached["base"]
        layer = moved_edge_layer(base, base["layer"], item) if base else None
        if layer is None:
            base = {"contours": contours, "clip": clip or [], "image_data": data,
                    "transform": transform, "width": width, "layer": edge_bleed_image(item)}
            layer = base["layer"]
        self._path_cache[key] = {"contours": contours, "clip_ref": clip, "image_data": data,
                                 "transform": transform, "width": width, "layer": layer, "base": base}
        edge_keys = [key for key in self._path_cache if key[1] == "edge_bleed"]
        used = sum(self._path_cache[key]["base"]["layer"][0].sizeInBytes() for key in edge_keys)
        for old_key in edge_keys:
            if used <= _MAX_CANVAS_EDGE_BYTES:
                break
            old = self._path_cache.pop(old_key)
            used -= old["base"]["layer"][0].sizeInBytes()
        return layer

    def selection_handles(self) -> dict[str, QPointF]:
        if (self.owner._selection_blocked()
                or not any(self.owner.items[i].get("visible", True) for i in self.owner.selected_indices())):
            return {}
        bounds = self.owner.selection_bounds()
        if bounds.isNull():
            return {}
        return {"nw": bounds.topLeft(), "ne": bounds.topRight(),
                "sw": bounds.bottomLeft(), "se": bounds.bottomRight(),
                "rotate": QPointF(bounds.center().x(), bounds.top() - 24 / self.zoom)}

    def handle_at(self, point: QPointF) -> str | None:
        for name, target in self.selection_handles().items():
            if (point - target).manhattanLength() * self.zoom <= 10:
                return name
        return None

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(self.owner.theme_colors["canvas"]))
        painter.save()
        painter.translate(PAGE_MARGIN, PAGE_MARGIN)
        painter.scale(self.zoom, self.zoom)

        page = QRectF(0, 0, PAGE_WIDTH_MM, PAGE_HEIGHT_MM)
        painter.setPen(QPen(QColor("#c7d1cc"), 0.55))
        painter.setBrush(PAPER)
        painter.drawRect(page)

        painter.save()
        painter.setClipRect(page)
        for x in range(0, int(PAGE_WIDTH_MM) + 1, 5):
            major = x % 10 == 0
            painter.setPen(QPen(GRID_MAJOR if major else GRID, 0.28 if major else 0.18))
            painter.drawLine(QPointF(x, 0), QPointF(x, PAGE_HEIGHT_MM))
        for y in range(0, int(PAGE_HEIGHT_MM) + 1, 5):
            major = y % 10 == 0
            painter.setPen(QPen(GRID_MAJOR if major else GRID, 0.28 if major else 0.18))
            painter.drawLine(QPointF(0, y), QPointF(PAGE_WIDTH_MM, y))

        registration = self.owner.metadata.get("print_cut", {})
        if registration.get("paper_width", 0) > 0 and registration.get("paper_height", 0) > 0:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#aebbb4"), 0.45))
            painter.drawRect(QRectF(0, 0, registration["paper_width"], registration["paper_height"]))

        from .artwork import draw_printed_item
        for item in self.owner.items:
            if not printable(item):
                continue
            if "image_data" not in item:
                draw_printed_item(painter, item)
                continue
            bleed = item.get("bleed", {})
            edge = bleed.get("width_mm") and bleed.get("mode") == "edge"
            painter.save()
            painter.setOpacity(self.owner.image_opacity.value() / 100)
            draw_printed_item(painter, item, image=_qoi_image(item["image_data"]),
                clip_path=self.item_path(item, "image_clip") if item.get("image_clip") else None,
                outer_path=self.item_bleed_path(item) if bleed.get("width_mm") and not edge else None,
                inner_path=self.item_bleed_path(item, inner=True) if bleed.get("inner_mm") else None,
                bleed_layer=self.item_edge_bleed(item) if edge else None)
            painter.restore()

        if registration.get("enabled") and registration.get("bounds"):
            from .printing import draw_registration_marks
            left, top, right, bottom = registration["bounds"]
            frame = QPainterPath()
            frame.addRect(QRectF(left, top, right - left, bottom - top))
            outside = QPainterPath()
            outside.addRect(page)
            painter.fillPath(outside.subtracted(frame), QColor(190, 32, 48, 18))
            pen = QPen(ACCENT, 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(frame)
            draw_registration_marks(painter, registration["bounds"], registration)

        violations = self.owner._print_cut_violations()
        selected_indices = self.owner.selected_indices()
        for index, item in enumerate(self.owner.items):
            if not item.get("visible", True):
                continue
            if not item.get("cut", True) and index not in selected_indices:
                continue
            path = self.item_path(item)
            selected = index in selected_indices
            color = (QColor("#d45c3d") if selected else QColor("#e5484d")
                     if self.owner.cutline_view.isChecked() and item.get("cut", True)
                     else ACCENT if item.get("cut", True) else QColor("#9ba9a3"))
            if index in violations:
                color = QColor("#be2030")
            painter.setBrush(Qt.BrushStyle.NoBrush)
            pen = QPen(color, 2.5 if index in violations else 1.0,
                       Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                       Qt.PenJoinStyle.RoundJoin)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(path)
        if selected_indices and self.selection_handles():
            painter.save()
            painter.setClipping(False)
            bounds = self.owner.selection_bounds()
            pen = QPen(QColor("#d45c3d"), 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawRect(bounds)
            painter.drawLine(QPointF(bounds.center().x(), bounds.top()), self.selection_handles()["rotate"])
            pen.setStyle(Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            painter.setBrush(Qt.GlobalColor.white)
            radius = 4 / self.zoom
            for name, point in self.selection_handles().items():
                if name == "rotate":
                    painter.drawEllipse(point, radius, radius)
                else:
                    painter.drawRect(QRectF(point.x()-radius, point.y()-radius, radius*2, radius*2))
            painter.restore()
        if self.marquee_start is not None:
            painter.save()
            painter.setClipping(False)
            pen = QPen(ACCENT, 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(QColor(8, 126, 114, 30))
            painter.drawRect(QRectF(self.marquee_start, self.marquee_end).normalized())
            painter.restore()
        painter.restore()
        painter.restore()

        # Keep rulers readable inside the fixed screen margin at every zoom.
        painter.setPen(QPen(QColor(self.owner.theme_colors["ruler"]), 1))
        font = QFont("Noto Sans")
        font.setPixelSize(11)
        painter.setFont(font)
        label_step = 20 * max(1, math.ceil(
            (painter.fontMetrics().horizontalAdvance("300") + 8) / (20 * self.zoom)))
        for value in range(0, 301, 10):
            position = PAGE_MARGIN + value * self.zoom
            painter.drawLine(QPointF(position, PAGE_MARGIN - 6), QPointF(position, PAGE_MARGIN))
            if value % label_step == 0:
                painter.drawText(QPointF(position + 4, PAGE_MARGIN - 12), str(value))
            painter.drawLine(QPointF(PAGE_MARGIN - 6, position), QPointF(PAGE_MARGIN, position))
            if value % label_step == 0:
                painter.save()
                painter.translate(PAGE_MARGIN - 12, position + 3)
                painter.rotate(-90)
                painter.drawText(QPointF(0, 0), str(value))
                painter.restore()

    def mousePressEvent(self, event: Any) -> None:
        if not self.interactive or event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        if self.space_pressed:
            self.pan_start = event.globalPosition() if hasattr(event, "globalPosition") else event.position()
            self.pan_scroll = (self.owner.canvas_scroll.horizontalScrollBar().value(),
                               self.owner.canvas_scroll.verticalScrollBar().value())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        point = self._world_point(event.position())
        modifiers = event.modifiers() if hasattr(event, "modifiers") else Qt.KeyboardModifier.NoModifier
        handle = self.handle_at(point)
        if handle and not modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            self.drag_handle = handle
            self.drag_bounds = self.owner.selection_bounds()
            self.drag_index = self.owner.selected
            self.drag_start = point
            self.drag_before = self.owner._snapshot()
            self.drag_duplicate = False
            self.drag_moved = False
            return
        hit = -1
        for index in range(len(self.owner.items) - 1, -1, -1):
            item = self.owner.items[index]
            if item.get("registration_guide", False) or not item.get("visible", True) or not editable(item):
                continue
            path = self.item_path(self.owner.items[index])
            if not path.controlPointRect().adjusted(-6, -6, 6, 6).contains(point):
                continue
            stroker = QPainterPathStroker()
            stroker.setWidth(3.0)
            if path.contains(point) or stroker.createStroke(path).contains(point):
                hit = index
                break
        if hit < 0:
            self.marquee_start = self.marquee_end = point
            self.marquee_before = self.owner.selected_indices()
            additive = bool(modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
            self.marquee_base = self.owner.selected_indices() if additive else set()
            self.owner.select_many(self.marquee_base)
            self.update()
            return
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            selected = self.owner.selected_indices()
            if hit in selected:
                selected.difference_update(expand_selection(self.owner.items, [hit]))
            elif hit >= 0:
                selected.update(expand_selection(self.owner.items, [hit]))
            self.owner.select_many(selected)
        elif hit not in self.owner.selected_indices():
            self.owner.select(hit)
        if hit in self.owner.selected_indices() and not self.owner._selection_blocked():
            self.drag_handle = None
            self.drag_index = hit
            self.drag_start = point
            self.drag_before = self.owner._snapshot()
            self.drag_duplicate = bool(modifiers & Qt.KeyboardModifier.AltModifier)
            self.drag_moved = False
        self.update()

    def mouseMoveEvent(self, event: Any) -> None:
        if self.pan_start is not None:
            delta = (event.globalPosition() if hasattr(event, "globalPosition") else event.position()) - self.pan_start
            self.owner.canvas_scroll.horizontalScrollBar().setValue(self.pan_scroll[0] - round(delta.x()))
            self.owner.canvas_scroll.verticalScrollBar().setValue(self.pan_scroll[1] - round(delta.y()))
            return
        if self.marquee_start is not None:
            self.marquee_end = self._world_point(event.position())
            rectangle = QRectF(self.marquee_start, self.marquee_end).normalized()
            hits = {index for index, item in enumerate(self.owner.items)
                    if not item.get("registration_guide", False)
                    and item.get("visible", True) and editable(item)
                    and (self.item_path(item).intersects(rectangle)
                         or rectangle.contains(self.item_path(item).controlPointRect()))}
            self.owner.select_many(self.marquee_base | hits)
            self.update()
            return
        if self.drag_start is None or self.drag_index < 0:
            if self.space_pressed:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
                return
            handle = self.handle_at(self._world_point(event.position()))
            cursor = (Qt.CursorShape.CrossCursor if handle == "rotate" else
                      Qt.CursorShape.SizeFDiagCursor if handle in ("nw", "se") else
                      Qt.CursorShape.SizeBDiagCursor if handle else Qt.CursorShape.ArrowCursor)
            self.setCursor(cursor)
            return
        point = self._world_point(event.position())
        delta = point - self.drag_start
        transform = QTransform.fromTranslate(delta.x(), delta.y())
        if delta.manhattanLength() < .01:
            return
        if self.drag_handle:
            bounds = self.drag_bounds
            modifiers = event.modifiers() if hasattr(event, "modifiers") else Qt.KeyboardModifier.NoModifier
            transform = QTransform()
            if self.drag_handle == "rotate":
                center = bounds.center()
                start = self.drag_start - center
                end = point - center
                angle = math.degrees(math.atan2(end.y(), end.x()) - math.atan2(start.y(), start.x()))
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    angle = round(angle / 15) * 15
                transform.translate(center.x(), center.y())
                transform.rotate(angle)
                transform.translate(-center.x(), -center.y())
            else:
                opposite = {"nw": bounds.bottomRight(), "ne": bounds.bottomLeft(),
                            "sw": bounds.topRight(), "se": bounds.topLeft()}[self.drag_handle]
                corner = {"nw": bounds.topLeft(), "ne": bounds.topRight(),
                          "sw": bounds.bottomLeft(), "se": bounds.bottomRight()}[self.drag_handle]
                if bounds.width() <= 0 or bounds.height() <= 0:
                    return
                sx = max(.1 / bounds.width(), (point.x()-opposite.x()) / (corner.x()-opposite.x()))
                sy = max(.1 / bounds.height(), (point.y()-opposite.y()) / (corner.y()-opposite.y()))
                if self.owner.lock_ratio.isChecked() or modifiers & Qt.KeyboardModifier.ShiftModifier:
                    sx = sy = max(sx, sy)
                transform.translate(opposite.x(), opposite.y())
                transform.scale(sx, sy)
                transform.translate(-opposite.x(), -opposite.y())
        indices = self.drag_before.get("selection", [self.drag_index])
        if self.drag_duplicate and not self.drag_moved:
            copies = _copy_items([self.drag_before["items"][i] for i in indices])
            remap_group_ids(copies)
            self.owner.items.extend(copies)
            self.owner.select_many(range(len(self.owner.items) - len(indices), len(self.owner.items)))
        targets = sorted(self.owner.selected_indices())
        for target, source_index in zip(targets, indices):
            _transform_item(self.owner.items[target], transform, self.drag_before["items"][source_index])
        self.drag_moved = True
        self.geometryChanged.emit()
        self.update()

    def mouseReleaseEvent(self, event: Any) -> None:
        if self.pan_start is not None:
            self.pan_start = self.pan_scroll = None
            self.setCursor(Qt.CursorShape.OpenHandCursor if self.space_pressed else Qt.CursorShape.ArrowCursor)
            return
        if self.marquee_start is not None:
            self.marquee_start = self.marquee_end = None
            self.marquee_base = set()
            self.update()
            return
        if self.drag_start is not None and self.drag_before is not None:
            if self.owner._snapshot() != self.drag_before:
                self.owner.push_undo(self.drag_before)
        self.drag_start = None
        self.drag_index = -1
        self.drag_before = None
        self.drag_path = None
        self.drag_handle = self.drag_bounds = None
        self.owner._refresh_tab_titles()

    def wheelEvent(self, event: Any) -> None:
        if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            event.ignore()
            return
        point = self._world_point(event.position())
        scroll = self.owner.canvas_scroll
        old_x, old_y = event.position().x() - scroll.horizontalScrollBar().value(), event.position().y() - scroll.verticalScrollBar().value()
        self.owner.change_zoom(1.25 ** (event.angleDelta().y() / 120))
        scroll.horizontalScrollBar().setValue(round(PAGE_MARGIN + point.x()*self.zoom - old_x))
        scroll.verticalScrollBar().setValue(round(PAGE_MARGIN + point.y()*self.zoom - old_y))
        event.accept()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Escape and self.marquee_start is not None:
            before = self.marquee_before
            self.marquee_start = self.marquee_end = None
            self.owner.select_many(before)
            event.accept()
        elif event.key() == Qt.Key.Key_Escape and self.drag_before is not None:
            self.owner._restore(self.drag_before)
            self.drag_before = self.drag_start = self.drag_handle = self.drag_bounds = None
            self.drag_index = -1
            event.accept()
        elif event.key() == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self.space_pressed = True
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self.space_pressed = False
                self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event: Any) -> None:
        self.mouseReleaseEvent(None)
        self.space_pressed = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().focusOutEvent(event)


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
        layout.addWidget(QLabel(tr("實線為刀具落下路徑；虛線為抬刀移動路徑。")))
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
            spin.setDecimals(2)
            spin.setSuffix(" mm")
            spin.setValue(value)
        self.color_button = QPushButton(tr("出血顏色…"))
        self.color_button.clicked.connect(self.choose_color)
        preview_button = QPushButton(tr("預覽"))
        preview_button.clicked.connect(self.preview)
        form = QFormLayout()
        form.addRow(tr("出血方式"), self.mode_combo)
        form.addRow(tr("外側出血"), self.width_spin)
        form.addRow(tr("內側補色"), self.inner_spin)
        form.addRow(self.color_button, preview_button)
        self.mode_combo.currentIndexChanged.connect(self._update_mode)
        self.inner_spin.valueChanged.connect(self._update_mode)
        self._update_mode()
        self.preview_label = QLabel(tr("按預覽檢查出血；內側補色會覆蓋邊緣圖案，不移動切線。"))
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
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("套用"))
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


class PackingDialog(QDialog):
    def __init__(self, owner, indices):
        super().__init__(owner)
        self.owner, self.indices, self.plan = owner, indices, None
        self.setWindowTitle(tr("自動排版"))
        self.copies_spin = SafeSpinBox()
        self.copies_spin.setRange(1, 1000)
        self.fill_check = QCheckBox(tr("繼續填滿剩餘範圍"))
        self.rotate_check = QCheckBox(tr("允許 90 度倍數旋轉"))
        self.shape_check = QCheckBox(tr("依實際輪廓排列"))
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
        form.addRow(self.shape_check)
        form.addRow(self.fill_check)
        preview = QPushButton(tr("預覽"))
        preview.clicked.connect(self.preview)
        self.preview_label = QLabel(tr("先預覽排版，再套用；圖案維持原尺寸。"))
        self.preview_label.setMinimumSize(360, 360)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setWordWrap(True)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.directory = QTemporaryDir()
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(preview)
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
        for spin in (self.copies_spin, self.gap_spin, self.margin_spin):
            spin.valueChanged.connect(self.invalidate)
        for check in (self.fill_check, self.rotate_check, self.shape_check):
            check.toggled.connect(self.invalidate)

    def invalidate(self, *_args):
        self.plan = None
        self.apply_button.setEnabled(False)
        self.summary.setText(tr("設定已變更，請重新預覽。"))

    def build_items(self):
        from .packing import pack_items
        if self.plan is None:
            units = group_units(self.owner.items, self.indices)
            self.plan = pack_items(self.owner.items, self.owner.metadata, self.indices,
                {"copies_per_unit": [self.copies_spin.value()]*len(units),
                 "gap_mm": self.gap_spin.value(), "margin_mm": self.margin_spin.value(),
                 "allow_rotate": self.rotate_check.isChecked(), "shape_aware": self.shape_check.isChecked(),
                 "fill": self.fill_check.isChecked()})
        return self.plan

    def preview(self):
        from .editor_actions import render_preview
        try:
            items, _, details = self.build_items()
            path = Path(self.directory.path()) / "preview.png"
            render_preview(items, self.owner.metadata, path)
            self.preview_label.setPixmap(QPixmap(str(path)).scaled(360, 360,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            text = tr("共 {copies} 組副本；{objects} 個物件。", copies=details["copies"], objects=len(items))
            if details["limit_reached"]:
                text += " " + tr("已達本次排版上限。")
            self.summary.setText(text)
            self.apply_button.setEnabled(True)
        except (ValueError, OSError) as error:
            self.apply_button.setEnabled(False)
            QMessageBox.warning(self, tr("自動排版"), str(error))


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

        current = QPointF(0, 0)
        for mode, point in self.moves:
            color = ACCENT if mode == "D" else QColor("#b87531")
            style = Qt.PenStyle.SolidLine if mode == "D" else Qt.PenStyle.DashLine
            painter.setPen(QPen(color, 0.85 if mode == "D" else 0.55, style))
            painter.drawLine(current, point)
            current = point
        painter.restore()


class TaskSignals(QObject):
    succeeded = Signal(object, object)
    failed = Signal(object, str)


class FunctionTask(QRunnable):
    def __init__(self, function: Any) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.function = function
        self.signals = TaskSignals()

    def run(self) -> None:
        try:
            self.signals.succeeded.emit(self, self.function())
        except Exception as error:
            self.signals.failed.emit(self, str(error))


class VeronaWindow(QMainWindow):
    sendProgress = Signal(int, int)

    def __init__(self, *, host=None, port=None) -> None:
        super().__init__()
        app = QApplication.instance()
        app.setStyle("Fusion")
        app.setFont(QFont("Noto Sans", 10))
        self.setWindowTitle(tr("Verona"))
        self.resize(1280, 800)
        self.items: list[dict[str, Any]] = []
        self.selected = -1
        self.undo_stack: list[dict[str, Any]] = []
        self.redo_stack: list[dict[str, Any]] = []
        self.current_file: str | None = None
        self.metadata: dict[str, Any] = {}
        self.selection: set[int] = set()
        self._documents: list[dict[str, Any]] = []
        self._active_document = 0
        self._tasks: dict[FunctionTask, tuple[Any, Any]] = {}
        self.usb_devices: list[Any] = []
        self.sending = False
        self._ratio_locked = True
        self.settings = QSettings("Verona", "Workspace")
        self.theme_name = self.settings.value("theme", "light", type=str)
        self._build_ui()
        self.sendProgress.connect(self._send_progress)
        self.wifi_host.setText(host if host is not None else
                               os.environ.get("VERONA_HOST") or self.settings.value("wifi/host", "", type=str))
        self.wifi_port.setValue(port if port is not None else self.settings.value("wifi/port", TCP_PORT, type=int))
        self._build_menu()
        self._refresh_history_actions()
        self.saved_snapshot = self._document_state()
        self._documents.append(self._tab_state())
        self._refresh_tab_titles()
        self._alt_mouse_grab = False

    def showEvent(self, event) -> None:
        QApplication.instance().installEventFilter(self)
        super().showEvent(event)

    def _release_alt_grab(self) -> None:
        if QWidget.mouseGrabber() is self.canvas:
            self.canvas.releaseMouse()
        self._alt_mouse_grab = False

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.KeyRelease and event.key() == Qt.Key.Key_Alt and self._alt_mouse_grab:
            self._release_alt_grab()
            event.accept()
            return True
        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide, QEvent.Type.FocusOut):
            if watched in (self, self.canvas) and self._alt_mouse_grab:
                self._release_alt_grab()
        if (kind == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Alt
                and QApplication.platformName() == "xcb" and self.isActiveWindow()
                and isinstance(watched, QWidget) and watched.window() is self
                and self.canvas.rect().contains(self.canvas.mapFromGlobal(QCursor.pos()))):
            # X11 desktops can reserve Alt-drag for moving windows. Capture
            # only this canvas gesture; release on Alt-up, focus loss or hide.
            self.canvas.setFocus()
            self.canvas.grabMouse()
            self._alt_mouse_grab = True
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _build_ui(self) -> None:
        self._apply_theme(self.theme_name)
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(12)

        header = QFrame(objectName="header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 16, 10)
        brand = QVBoxLayout()
        brand.setSpacing(1)
        brand.addWidget(QLabel(tr("Verona  /  Linux 工作區"), objectName="eyebrow"))
        brand.addWidget(QLabel(tr("Verona"), objectName="title"))
        header_layout.addLayout(brand)
        header_layout.addStretch(1)
        self.file_label = QLabel(tr("未命名設計"))
        self.file_label.setObjectName("muted")
        header_layout.addWidget(self.file_label)
        outer.addWidget(header)

        actions = QHBoxLayout()
        actions.setSpacing(7)
        self._tool(actions, tr("新增設計"), self.new_design)
        self._tool(actions, tr("匯入 SVG"), self.import_svg)
        self._tool(actions, tr("匯入圖片"), self.import_image)
        self._tool(actions, tr("矩形"), self.add_rectangle)
        self._tool(actions, tr("圓形"), self.add_ellipse)
        self._tool(actions, tr("文字輪廓"), self.add_text)
        self._tool(actions, tr("複製"), self.duplicate_selected)
        self._tool(actions, tr("刪除"), self.delete_selected)
        actions.addSpacing(8)
        self.undo_action = QAction(tr("復原"), self)
        self.undo_action.setShortcut("Ctrl+Z")
        self.undo_action.triggered.connect(self.undo)
        self.redo_action = QAction(tr("重做"), self)
        self.redo_action.setShortcut("Ctrl+Shift+Z")
        self.redo_action.triggered.connect(self.redo)
        self._tool(actions, "−", lambda: self.change_zoom(0.8))
        self.zoom_label = QLabel(f"{round(DEFAULT_ZOOM * 100)}%")
        self.zoom_label.setMinimumWidth(44)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        actions.addWidget(self.zoom_label)
        self._tool(actions, "+", lambda: self.change_zoom(1.25))
        actions.addStretch(1)
        self._tool(actions, tr("切割預覽"), self.show_preview)
        outer.addLayout(actions)

        self.tab_bar = QTabBar()
        self.tab_bar.setTabsClosable(True)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setMovable(False)
        self.tab_bar.currentChanged.connect(self._switch_document)
        self.tab_bar.tabCloseRequested.connect(self.close_document)
        outer.addWidget(self.tab_bar)
        self.bounds_warning = QLabel(objectName="boundsWarning")
        self.bounds_warning.setWordWrap(True)
        self.bounds_warning.setTextFormat(Qt.TextFormat.PlainText)
        self.bounds_warning.hide()
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.canvas = DesignCanvas(self)
        self.canvas.geometryChanged.connect(self.refresh_properties)
        scroll = QScrollArea()
        self.canvas_scroll = scroll
        scroll.setWidget(self.canvas)
        scroll.setWidgetResizable(False)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        splitter.addWidget(scroll)
        self.inspector_scroll = QScrollArea()
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inspector_scroll.setMinimumWidth(286)
        self.inspector_scroll.setWidget(self._build_inspector())
        inspector_panel = QWidget()
        inspector_layout = QVBoxLayout(inspector_panel)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.addWidget(self.bounds_warning)
        from .layer_panel import LayerPanel
        self.layer_panel = LayerPanel(self)
        side_splitter = QSplitter(Qt.Orientation.Vertical)
        side_splitter.addWidget(self.layer_panel)
        side_splitter.addWidget(self.inspector_scroll)
        side_splitter.setSizes([220, 500])
        side_splitter.setChildrenCollapsible(False)
        inspector_panel.setMinimumWidth(370)
        inspector_layout.addWidget(side_splitter)
        splitter.addWidget(inspector_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([840, 390])
        outer.addWidget(splitter, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel(tr("工作區 300 × 300 mm"))
        self.status_label.setObjectName("muted")
        footer.addWidget(self.status_label)
        footer.addStretch(1)
        outer.addLayout(footer)
        self.setCentralWidget(root)

    def _tab_state(self) -> dict[str, Any]:
        return {"items": self.items, "selected": self.selected, "selection": self.selection,
                "metadata": self.metadata, "file": self.current_file,
                "settings": self._cut_settings(), "saved": self.saved_snapshot,
                "undo": self.undo_stack, "redo": self.redo_stack,
                "zoom": self.canvas.zoom,
                "view": (self.image_opacity.value(), self.cutline_view.isChecked()),
                "scroll": (self.canvas_scroll.horizontalScrollBar().value(),
                           self.canvas_scroll.verticalScrollBar().value())}

    def _switch_document(self, index: int) -> None:
        if not 0 <= index < len(self._documents) or index == self._active_document:
            return
        self._documents[self._active_document] = self._tab_state()
        self._active_document = index
        self._restore_tab_state(self._documents[index])

    def _restore_tab_state(self, state: dict[str, Any]) -> None:
        self.items, self.metadata = state["items"], state["metadata"]
        self.current_file, self.saved_snapshot = state["file"], state["saved"]
        self.selected, self.selection = state["selected"], state["selection"]
        self.undo_stack, self.redo_stack = state["undo"], state["redo"]
        self._set_cut_settings(state["settings"])
        self.cutline_view.setChecked(state.get("view", (100, False))[1])
        self.image_opacity.setValue(state.get("view", (100, False))[0])
        self.canvas.set_zoom(state["zoom"])
        self.zoom_label.setText(f"{round(self.canvas.zoom * 100)}%")
        self.canvas_scroll.horizontalScrollBar().setValue(state["scroll"][0])
        self.canvas_scroll.verticalScrollBar().setValue(state["scroll"][1])
        self.native_preview_action.setEnabled(bool(self.metadata.get("source_preview")))
        self.file_label.setText(Path(self.current_file).name if self.current_file else tr("未命名設計"))
        self.refresh_all()

    def _refresh_tab_titles(self) -> None:
        if not hasattr(self, "tab_bar") or not self._documents:
            return
        self._documents[self._active_document] = self._tab_state()
        self.tab_bar.blockSignals(True)
        while self.tab_bar.count() < len(self._documents):
            self.tab_bar.addTab("")
        while self.tab_bar.count() > len(self._documents):
            self.tab_bar.removeTab(self.tab_bar.count()-1)
        for index, state in enumerate(self._documents):
            dirty = {"items": state["items"], "settings": state["settings"],
                     "metadata": state["metadata"]} != state["saved"]
            name = Path(state["file"]).name if state["file"] else tr("未命名設計")
            self.tab_bar.setTabText(index, name + (" *" if dirty else ""))
            self.tab_bar.setTabToolTip(index, state["file"] or name)
        self.tab_bar.setCurrentIndex(self._active_document)
        self.tab_bar.blockSignals(False)

    def close_document(self, index: int) -> bool:
        if not 0 <= index < len(self._documents):
            return False
        self.tab_bar.setCurrentIndex(index)
        if not self._maybe_save_changes():
            return False
        if len(self._documents) == 1:
            self.items, self.metadata = [], {}
            self.current_file = None
            self.selected, self.selection = -1, set()
            self.undo_stack, self.redo_stack = [], []
            self.saved_snapshot = self._document_state()
            self.native_preview_action.setEnabled(False)
            self.file_label.setText(tr("未命名設計"))
            self.refresh_all()
            return True
        del self._documents[index]
        next_index = min(index, len(self._documents)-1)
        self._active_document = next_index
        self._restore_tab_state(self._documents[next_index])
        return True

    def _tool(self, layout: QHBoxLayout, label: str, callback: Any) -> QToolButton:
        button = QToolButton()
        button.setText(label)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _build_inspector(self) -> QWidget:
        panel = QFrame(objectName="inspector")
        panel.setMinimumWidth(270)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)
        layout.addWidget(QLabel(tr("路徑設定"), objectName="title"))
        selection_hint = QLabel(tr("Ctrl／Shift 點選可多選；Shift 滾輪縮放。"), objectName="muted")
        selection_hint.setWordWrap(True)
        layout.addWidget(selection_hint)

        geometry = QFormLayout()
        geometry.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        geometry.setVerticalSpacing(4)
        self.object_name = QLineEdit()
        self.object_name.setPlaceholderText(tr("物件名稱"))
        geometry.addRow(tr("名稱"), self.object_name)
        self.width_spin = self._mm_spin()
        self.height_spin = self._mm_spin()
        self.x_spin = self._mm_spin()
        self.x_spin.setRange(0, PAGE_WIDTH_MM)
        self.y_spin = self._mm_spin()
        self.y_spin.setRange(0, 99999)
        geometry.addRow(tr("X 起點"), self.x_spin)
        geometry.addRow(tr("Y 起點"), self.y_spin)
        geometry.addRow(tr("寬度"), self.width_spin)
        geometry.addRow(tr("高度"), self.height_spin)
        layout.addLayout(geometry)
        self.lock_ratio = QCheckBox(tr("鎖定比例"))
        self.lock_ratio.setChecked(True)
        layout.addWidget(self.lock_ratio)
        self.cut_checkbox = QCheckBox(tr("包含於切割"))
        self.cut_checkbox.toggled.connect(self._cut_toggled)
        layout.addWidget(self.cut_checkbox)
        self.output_mode = QComboBox()
        for label, mode in (("僅列印", "print_only"), ("僅切割", "cut_only"),
                            ("列印＋切割", "both"), ("不輸出", "none")):
            self.output_mode.addItem(tr(label), mode)
        self.output_mode.setPlaceholderText(tr("多種輸出設定"))
        self.output_mode.currentIndexChanged.connect(self._output_mode_changed)
        output = QFormLayout()
        output.addRow(tr("輸出用途"), self.output_mode)
        layout.addLayout(output)
        self.width_spin.valueChanged.connect(lambda value: self._dimension_changed("width", value))
        self.height_spin.valueChanged.connect(lambda value: self._dimension_changed("height", value))
        apply_size = QPushButton(tr("套用尺寸"))
        self.selection_edit_buttons = [apply_size]
        apply_size.clicked.connect(self.apply_size)
        layout.addWidget(apply_size)
        layout.addSpacing(6)
        layout.addWidget(QLabel(tr("變形與群組")))
        transforms = QGridLayout()
        transforms.setSpacing(6)
        for index, (label, callback) in enumerate((
                ("水平鏡像", lambda: self.transform_selected(mirror_x=True)),
                ("垂直鏡像", lambda: self.transform_selected(mirror_y=True)),
                ("旋轉 −90°", lambda: self.transform_selected(rotation=-90)),
                ("旋轉 +90°", lambda: self.transform_selected(rotation=90)),
                ("群組", self.group_selected), ("解散群組", self.ungroup_selected))):
            button = QPushButton(tr(label))
            if index < 4:
                self.selection_edit_buttons.append(button)
            button.setMinimumHeight(30)
            button.clicked.connect(callback)
            transforms.addWidget(button, index // 2, index % 2)
            if label == "群組":
                self.group_button = button
            elif label == "解散群組":
                self.ungroup_button = button
        for column in range(2):
            transforms.setColumnStretch(column, 1)
        layout.addLayout(transforms)
        layout.addSpacing(6)
        layout.addWidget(QLabel(tr("對齊與均分")))
        align_grid = QGridLayout()
        align_grid.setSpacing(6)
        self.alignment_buttons = {}
        for index, (mode, label) in enumerate((
                ("left", "靠左"), ("hcenter", "水平置中"), ("right", "靠右"),
                ("top", "靠上"), ("vcenter", "垂直置中"), ("bottom", "靠下"))):
            button = QPushButton()
            button.setText(tr(label))
            button.setMinimumHeight(30)
            button.clicked.connect(lambda _checked=False, value=mode: self.align_selected(value))
            align_grid.addWidget(button, index // 3, index % 3)
            self.alignment_buttons[mode] = button
        for column in range(3):
            align_grid.setColumnStretch(column, 1)
        layout.addLayout(align_grid)
        distribution = QHBoxLayout()
        distribution.setSpacing(6)
        for mode, label in (("distribute_h", "水平均分"), ("distribute_v", "垂直均分")):
            button = QPushButton(tr(label))
            button.setMinimumHeight(30)
            button.clicked.connect(lambda _checked=False, value=mode: self.align_selected(value))
            distribution.addWidget(button)
            self.alignment_buttons[mode] = button
        layout.addLayout(distribution)
        layout.addSpacing(6)
        self.cutline_view = QCheckBox(tr("淡化圖片並凸顯裁切線"))
        self.cutline_view.toggled.connect(self._toggle_cutline_view)
        layout.addWidget(self.cutline_view)
        image_view = QFormLayout()
        self.image_opacity = SafeSpinBox()
        self.image_opacity.setRange(0, 100)
        self.image_opacity.setValue(100)
        self.image_opacity.setSuffix(" %")
        self.image_opacity.valueChanged.connect(lambda: self.canvas.update())
        image_view.addRow(tr("圖片顯示不透明度"), self.image_opacity)
        layout.addLayout(image_view)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("separator")
        layout.addWidget(separator)
        cut_content = QWidget()
        cut_layout = QVBoxLayout(cut_content)
        cut_layout.setContentsMargins(0, 4, 0, 4)
        self.cut_section = CollapsibleSection(tr("切割工具"), cut_content)
        layout.addWidget(self.cut_section)
        settings = QFormLayout()
        settings.setVerticalSpacing(4)
        self.tool_combo = QComboBox()
        self.tool_combo.addItem(tr("刀具"), 0)
        self.tool_combo.addItem(tr("記號筆"), 1)
        settings.addRow(tr("工具"), self.tool_combo)
        cut_layout.addLayout(settings)
        self.custom_settings = QCheckBox(tr("指定速度與壓力"))
        self.custom_settings.toggled.connect(self._toggle_cut_settings)
        cut_layout.addWidget(self.custom_settings)
        cut = QFormLayout()
        cut.setVerticalSpacing(4)
        self.speed_spin = SafeSpinBox()
        self.speed_spin.setRange(1, 13)
        self.speed_spin.setValue(5)
        self.force_spin = SafeSpinBox()
        self.force_spin.setRange(1, 160)
        self.force_spin.setValue(10)
        cut.addRow(tr("速度"), self.speed_spin)
        cut.addRow(tr("壓力"), self.force_spin)
        cut_layout.addLayout(cut)
        self.speed_spin.setEnabled(False)
        self.force_spin.setEnabled(False)
        self.registration_checkbox = QCheckBox(tr("Print & Cut 自動對位").replace("&", "&&"))
        self.registration_checkbox.toggled.connect(self._registration_toggled)
        layout.addWidget(self.registration_checkbox)
        self.registration_info = QLabel(objectName="muted")
        self.registration_info.setWordWrap(True)
        layout.addWidget(self.registration_info)
        mark_content = QWidget()
        mark_layout = QVBoxLayout(mark_content)
        mark_layout.setContentsMargins(0, 4, 0, 4)
        self.mark_section = CollapsibleSection(tr("紙張與定位設定"), mark_content)
        layout.addWidget(self.mark_section)
        mark_layout.addWidget(QLabel(tr("標記距紙邊")))
        self.mark_margins = []
        margins_grid = QGridLayout()
        for index, label in enumerate(("左", "上", "右", "下")):
            spin = self._mm_spin()
            spin.setRange(0.75, 299)
            self.mark_margins.append(spin)
            margins_grid.addWidget(QLabel(tr(label)), index // 2, (index % 2) * 2)
            margins_grid.addWidget(spin, index // 2, (index % 2) * 2 + 1)
        mark_layout.addLayout(margins_grid)
        mark_style = QFormLayout()
        mark_style.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.mark_stroke = self._mm_spin()
        self.mark_stroke.setRange(0.5, 1.5)
        self.mark_stroke.setSingleStep(0.1)
        self.mark_stroke.valueChanged.connect(self._update_mark_minimums)
        self.mark_short_length = self._mm_spin()
        self.mark_short_length.setRange(4, 12.5)
        mark_style.addRow(tr("定位線寬"), self.mark_stroke)
        mark_style.addRow(tr("其餘三個角臂長"), self.mark_short_length)
        mark_layout.addLayout(mark_style)
        mark_note = QLabel(tr("左下角臂長固定 12.5 mm；無方向三角形。"), objectName="muted")
        mark_note.setWordWrap(True)
        mark_layout.addWidget(mark_note)
        self.apply_marks_button = QPushButton(tr("套用定位標記"))
        self.apply_marks_button.clicked.connect(self.apply_mark_margins)
        mark_layout.addWidget(self.apply_marks_button)
        self.maximize_marks_button = QPushButton(tr("依印表機邊界最大化"))
        self.maximize_marks_button.clicked.connect(self.maximize_mark_area)
        mark_layout.addWidget(self.maximize_marks_button)
        adjustment = QFormLayout()
        self.registration_offsets = []
        for label in ("切割微調 X", "切割微調 Y"):
            spin = self._mm_spin()
            spin.setRange(-5, 5)
            spin.setDecimals(3)
            spin.setSingleStep(0.025)
            self.registration_offsets.append(spin)
            adjustment.addRow(tr(label), spin)
        mark_layout.addLayout(adjustment)
        offset_note = QLabel(tr("正 X 向右、正 Y 向下；請依量測結果調整。"), objectName="muted")
        offset_note.setWordWrap(True)
        mark_layout.addWidget(offset_note)
        self.apply_offsets_button = QPushButton(tr("套用切割微調"))
        self.apply_offsets_button.clicked.connect(self.apply_registration_offset)
        mark_layout.addWidget(self.apply_offsets_button)
        self._sync_registration_controls()

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("separator")
        layout.addWidget(separator)
        connection_content = QWidget()
        connection_layout = QVBoxLayout(connection_content)
        connection_layout.setContentsMargins(0, 4, 0, 4)
        self.connection_section = CollapsibleSection(tr("傳送連線"), connection_content)
        layout.addWidget(self.connection_section)
        self.connection_form = QFormLayout()
        self.connection_form.setVerticalSpacing(4)
        self.connection_mode = QComboBox()
        self.connection_mode.addItem(tr("Wi‑Fi"), "wifi")
        self.connection_mode.addItem(tr("USB"), "usb")
        self.connection_form.addRow(tr("方式"), self.connection_mode)
        self.wifi_host = QLineEdit()
        self.wifi_host.setPlaceholderText(tr("Juliet 螢幕顯示的 IPv4"))
        self.connection_form.addRow(tr("IP 位址"), self.wifi_host)
        self.wifi_port = SafeSpinBox()
        self.wifi_port.setRange(1, 65535)
        self.wifi_port.setValue(TCP_PORT)
        self.connection_form.addRow(tr("連接埠"), self.wifi_port)
        connection_layout.addLayout(self.connection_form)
        self.wifi_check_button = QPushButton(tr("檢查連線"))
        self.wifi_check_button.clicked.connect(self.check_wifi_connection)
        connection_layout.addWidget(self.wifi_check_button)
        self.usb_row = QWidget()
        usb_layout = QHBoxLayout(self.usb_row)
        usb_layout.setContentsMargins(0, 0, 0, 0)
        usb_layout.setSpacing(5)
        self.usb_combo = QComboBox()
        self.usb_combo.addItem(tr("請重新整理 USB 裝置"), None)
        usb_layout.addWidget(self.usb_combo, 1)
        self.usb_refresh = QPushButton(tr("重新整理"))
        self.usb_refresh.clicked.connect(self.refresh_usb_devices)
        usb_layout.addWidget(self.usb_refresh)
        connection_layout.addWidget(self.usb_row)
        self.connection_mode.currentIndexChanged.connect(self._update_connection_mode)
        self._update_connection_mode()
        layout.addStretch(1)
        self.output_button = QPushButton(tr("匯出 PLT…"))
        self.output_button.clicked.connect(self.export_plt)
        layout.addWidget(self.output_button)
        self.send_button = QPushButton(tr("傳送至 Juliet…"))
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.send_to_cutter)
        layout.addWidget(self.send_button)
        return panel

    @staticmethod
    def _mm_spin() -> QDoubleSpinBox:
        spin = SafeDoubleSpinBox()
        spin.setRange(0.1, PAGE_WIDTH_MM)
        spin.setDecimals(2)
        spin.setSingleStep(1.0)
        spin.setSuffix(" mm")
        return spin

    def _toggle_cut_settings(self, enabled: bool) -> None:
        self.speed_spin.setEnabled(enabled)
        self.force_spin.setEnabled(enabled)

    def _cut_toggled(self, checked: bool) -> None:
        self.set_layer_property(sorted(self.selected_indices()), "cut", checked)

    def _update_connection_mode(self, *_args: Any) -> None:
        wifi = self.connection_mode.currentData() == "wifi"
        self.connection_form.setRowVisible(self.wifi_host, wifi)
        self.connection_form.setRowVisible(self.wifi_port, wifi)
        self.wifi_check_button.setVisible(wifi)
        self.usb_row.setVisible(not wifi)

    def refresh_usb_devices(self) -> None:
        from . import transport
        self.usb_refresh.setEnabled(False)
        self.usb_combo.clear()
        self.usb_combo.addItem(tr("正在列舉 USB 裝置…"), None)
        self._dispatch(transport.usb_devices, self._usb_devices_ready,
                       lambda error: self._usb_devices_failed(error))

    def _usb_devices_ready(self, devices: list[Any]) -> None:
        self.usb_devices = devices
        self.usb_combo.clear()
        if not devices:
            self.usb_combo.addItem(tr("未找到 Juliet USB 裝置"), None)
        for index, device in enumerate(devices, 1):
            bus = getattr(device, "bus", "?")
            address = getattr(device, "address", "?")
            self.usb_combo.addItem(tr("Juliet USB {index} · 匯流排 {bus} / 裝置 {address}", index=index, bus=bus, address=address), index - 1)
        self.usb_refresh.setEnabled(True)

    def _usb_devices_failed(self, error: str) -> None:
        self.usb_combo.clear()
        self.usb_combo.addItem(tr("USB 裝置列舉失敗"), None)
        self.usb_refresh.setEnabled(True)
        QMessageBox.warning(self, tr("USB 列舉失敗"), error)

    def _dispatch(self, function: Any, succeeded: Any, failed: Any) -> None:
        task = FunctionTask(function)
        self._tasks[task] = (succeeded, failed)
        task.signals.succeeded.connect(self._task_succeeded)
        task.signals.failed.connect(self._task_failed)
        QThreadPool.globalInstance().start(task)

    def _remember_wifi(self, host: str, port: int) -> None:
        self.settings.setValue("wifi/host", host)
        self.settings.setValue("wifi/port", port)

    def check_wifi_connection(self) -> None:
        from . import transport
        host, port = self.wifi_host.text().strip(), self.wifi_port.value()
        self.wifi_check_button.setEnabled(False)
        self.status_label.setText(tr("正在檢查 Juliet 連線…"))

        def succeeded(version: str) -> None:
            self.wifi_check_button.setEnabled(True)
            self._remember_wifi(host, port)
            self.status_label.setText(tr("已連線 · 韌體 {version}", version=version))

        def failed(error: str) -> None:
            self.wifi_check_button.setEnabled(True)
            self.status_label.setText(tr("連線檢查失敗"))
            QMessageBox.warning(self, tr("連線檢查失敗"), error)

        def query():
            with device_lock():
                return transport.query_tcp_version(host, port)

        self._dispatch(query, succeeded, failed)

    @Slot(object, object)
    def _task_succeeded(self, task: FunctionTask, result: Any) -> None:
        callbacks = self._tasks.pop(task, None)
        if callbacks:
            callbacks[0](result)

    @Slot(object, str)
    def _task_failed(self, task: FunctionTask, error: str) -> None:
        callbacks = self._tasks.pop(task, None)
        if callbacks:
            callbacks[1](error)

    def _send_confirmation(self, payload: bytes, job=None) -> str:
        if job is None:
            from .commands import encode_document_job
            _, job = encode_document_job(self.items, self.metadata, self._cut_settings())
        chosen = {id(contour) for contour in job["contours"]}
        paths = [(item, _path_from_contours([contour for contour in item["contours"]
                  if id(contour) in chosen]).boundingRect()) for item in self.items
                 if any(id(contour) in chosen for contour in item["contours"])]
        tool = tr("刀具") if job["tool"] == 0 else tr("記號筆")
        settings = (tr("速度 {speed} / 壓力 {force}", speed=job["speed"], force=job["force"])
                    if job["force"] is not None else tr("沿用機器目前的速度與壓力"))
        path_lines = [
            tr("• {name}：X {x}, Y {y} mm；{width} × {height} mm", name=_display_name(item['name']),
               x=f"{rect.left():.2f}", y=f"{rect.top():.2f}", width=f"{rect.width():.2f}", height=f"{rect.height():.2f}")
            for item, rect in paths
        ]
        target = (tr("Wi‑Fi {host}:{port}", host=self.wifi_host.text().strip(), port=self.wifi_port.value())
                  if self.connection_mode.currentData() == "wifi" else tr("USB 裝置清單中的 Juliet"))
        details = tr("即將透過 {target} 傳送 {bytes} bytes。\n\n工具：{tool}\n切割設定：{settings}\n\n路徑與尺寸：\n{paths}\n\n傳送成功只表示資料已送出，切割結果仍需在機器端確認。",
                  target=target, bytes=len(payload), tool=tool, settings=settings, paths="\n".join(path_lines))
        if job["registration_bounds"]:
            details = tr("此工作使用 Print & Cut 定位指令，請使用與目前標記設定一致的列印紙張。") + "\n\n" + details
            offset = job["registration_offset"]
            if any(offset):
                details += "\n\n" + tr("切割微調：X {x} mm、Y {y} mm（正值向右／向下）。",
                                             x=f"{offset[0]:+.3f}", y=f"{offset[1]:+.3f}")
        return details

    def send_to_cutter(self) -> None:
        if self.sending:
            return
        if not self.items:
            QMessageBox.information(self, tr("沒有切割路徑"), tr("先匯入 SVG 或新增圖形，再傳送。"))
            return
        mode = self.connection_mode.currentData()
        device = None
        host = self.wifi_host.text().strip()
        port = self.wifi_port.value()
        if mode == "wifi":
            try:
                address = ipaddress.IPv4Address(host)
                if address.is_unspecified or address.is_multicast or str(address) == "255.255.255.255":
                    raise ValueError
            except ValueError:
                QMessageBox.warning(self, tr("IP 位址無效"), tr("請輸入 Juliet 螢幕顯示的單一 IPv4 位址。"))
                return
        else:
            index = self.usb_combo.currentData()
            if index is None or not 0 <= int(index) < len(self.usb_devices):
                QMessageBox.warning(self, tr("未選取 USB 裝置"), tr("請先重新整理並選取 Juliet USB 裝置。"))
                return
            device = self.usb_devices[int(index)]
        try:
            from .commands import encode_document_job
            payload, job = encode_document_job(self.items, self.metadata, self._cut_settings())
        except Exception as error:
            QMessageBox.warning(self, tr("設計無法傳送"), str(error))
            return

        if not self._confirm_send(self._send_confirmation(payload, job)):
            return

        from . import transport
        self.sending = True
        self.send_button.setEnabled(False)
        self.status_label.setText(tr("正在傳送資料…"))
        if mode == "wifi":
            task = lambda: transport.send_tcp(payload, host, port, on_progress=self.sendProgress.emit)
            self._pending_wifi = (host, port)
        else:
            task = lambda: transport.send_usb(payload, device)
        def send():
            with device_lock():
                return task()

        self._dispatch(send, self._send_succeeded, self._send_failed)

    def _confirm_send(self, details: str) -> bool:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle(tr("確認傳送至 Juliet"))
        dialog.setText(details)
        yes = dialog.addButton(tr("是"), QMessageBox.ButtonRole.YesRole)
        dialog.addButton(tr("否"), QMessageBox.ButtonRole.NoRole)
        dialog.setDefaultButton(dialog.buttons()[-1])
        dialog.exec()
        return dialog.clickedButton() is yes

    @Slot(int, int)
    def _send_progress(self, sent: int, total: int) -> None:
        if self.sending:
            self.status_label.setText(tr("正在傳送：{percent}%（{sent}/{total} bytes）",
                                         percent=sent * 100 // total, sent=sent, total=total))

    def _send_succeeded(self, sent: int) -> None:
        if hasattr(self, "_pending_wifi"):
            self._remember_wifi(*self._pending_wifi)
            del self._pending_wifi
        self.sending = False
        self.send_button.setEnabled(True)
        self.status_label.setText(tr("已傳送 {sent} bytes · 實機切割結果尚未驗證", sent=sent))
        QMessageBox.information(
            self, tr("資料已傳送"), tr("傳輸層已送出 {sent} bytes。請在 Juliet 機器端確認切割結果。", sent=sent),
        )

    def _send_failed(self, error: str) -> None:
        if hasattr(self, "_pending_wifi"):
            del self._pending_wifi
        self.sending = False
        self.send_button.setEnabled(True)
        self.status_label.setText(tr("傳送失敗"))
        QMessageBox.warning(
            self, tr("傳送失敗"),
            tr("{error}\n\n傳輸中斷前可能已有部分資料送出，請先檢查 Juliet 狀態。此介面不會自動重送。", error=error),
        )

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu(tr("檔案"))
        new_action = QAction(tr("新增設計"), self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self.new_design)
        file_menu.addAction(new_action)
        for label, shortcut, callback in (
            (tr("開啟設計…"), "Ctrl+O", self.open_design),
            (tr("儲存設計…"), "Ctrl+S", self.save_design),
            (tr("匯入 SVG…"), "Ctrl+I", self.import_svg),
            (tr("匯入圖片…"), "Ctrl+Shift+I", self.import_image),
            (tr("匯入 PDF／SVG 圖稿…"), "", self.import_document_artwork),
            (tr("匯出 PLT…"), "Ctrl+E", self.export_plt),
        ):
            action = QAction(label, self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            file_menu.addAction(action)
        self.native_preview_action = QAction(tr("原廠預覽…"), self)
        self.native_preview_action.triggered.connect(self.show_native_preview)
        file_menu.addAction(self.native_preview_action)
        self.native_preview_action.setEnabled(bool(self.metadata.get("source_preview")))
        print_action = QAction(tr("列印…"), self)
        print_action.setShortcut("Ctrl+P")
        print_action.triggered.connect(self.print_design)
        file_menu.addAction(print_action)
        print_pdf_action = QAction(tr("匯出列印 PDF…"), self)
        print_pdf_action.triggered.connect(self.export_print_pdf)
        file_menu.addAction(print_pdf_action)
        close_action = QAction(tr("關閉分頁"), self)
        close_action.setShortcut("Ctrl+W")
        close_action.triggered.connect(lambda: self.close_document(self._active_document))
        file_menu.addAction(close_action)
        for label, shortcut, step in (("下一個分頁", "Ctrl+Tab", 1),
                                       ("上一個分頁", "Ctrl+Shift+Tab", -1)):
            action = QAction(tr(label), self)
            action.setShortcut(shortcut)
            action.triggered.connect(lambda _checked=False, delta=step:
                                     self.tab_bar.setCurrentIndex((self._active_document + delta) % len(self._documents)))
            file_menu.addAction(action)
        edit_menu = self.menuBar().addMenu(tr("編輯"))
        duplicate_action = QAction(tr("複製物件"), self)
        duplicate_action.setShortcut("Ctrl+D")
        duplicate_action.triggered.connect(self.duplicate_selected)
        edit_menu.addAction(duplicate_action)
        delete_action = QAction(tr("刪除物件"), self)
        delete_action.setShortcut("Delete")
        delete_action.triggered.connect(self.delete_selected)
        edit_menu.addAction(delete_action)
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        self.group_action = QAction(tr("群組"), self)
        self.group_action.setShortcut("Ctrl+G")
        self.group_action.triggered.connect(self.group_selected)
        edit_menu.addAction(self.group_action)
        self.ungroup_action = QAction(tr("解散群組"), self)
        self.ungroup_action.setShortcut("Ctrl+Shift+G")
        self.ungroup_action.triggered.connect(self.ungroup_selected)
        edit_menu.addAction(self.ungroup_action)
        link_cut = QAction(tr("將選取切線附到圖片"), self)
        link_cut.triggered.connect(self.link_selected_cut_paths)
        edit_menu.addAction(link_cut)
        edit_mask = QAction(tr("編輯圖片遮罩…"), self)
        edit_mask.triggered.connect(self.edit_selected_image)
        edit_menu.addAction(edit_mask)
        nodes = QAction(tr("編輯節點與曲線…"), self)
        nodes.triggered.connect(self.edit_nodes)
        edit_menu.addAction(nodes)
        printed = QAction(tr("設定向量列印…"), self)
        printed.triggered.connect(self.edit_vector_print)
        edit_menu.addAction(printed)
        bleed = QAction(tr("設定列印出血…"), self)
        bleed.triggered.connect(self.edit_selected_bleed)
        edit_menu.addAction(bleed)
        pack = QAction(tr("自動排版…"), self)
        pack.triggered.connect(self.pack_selected)
        edit_menu.addAction(pack)
        self.path_order_action = QAction(tr("自動排序切割路徑（內孔先切）"), self)
        self.path_order_action.setCheckable(True)
        self.path_order_action.setChecked(self.metadata.get("optimize_paths", False))
        self.path_order_action.triggered.connect(self.set_path_order)
        edit_menu.addAction(self.path_order_action)
        select_all = QAction(tr("全選物件"), self.canvas)
        select_all.setShortcut("Ctrl+A")
        select_all.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        select_all.triggered.connect(lambda: self.select_many(range(len(self.items))))
        self.canvas.addAction(select_all)
        edit_menu.addAction(select_all)
        appearance_menu = self.menuBar().addMenu(tr("外觀"))
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        self.theme_actions = {}
        for name, label in (("light", "淺色"), ("dark", "暗色")):
            action = QAction(tr(label), self)
            action.setCheckable(True)
            action.setChecked(self.theme_name == name)
            theme_group.addAction(action)
            appearance_menu.addAction(action)
            action.triggered.connect(lambda _checked=False, value=name: self._change_theme(value))
            self.theme_actions[name] = action
        language_menu = self.menuBar().addMenu(tr("語言"))
        group = QActionGroup(self)
        group.setExclusive(True)
        for code, label in (("en", "English"), ("zh_TW", "繁體中文")):
            action = QAction(tr(label), self)
            action.setCheckable(True)
            action.setChecked(get_language() == code)
            group.addAction(action)
            language_menu.addAction(action)
            action.triggered.connect(lambda _checked=False, value=code: self._change_language(value))

    def _apply_theme(self, name: str) -> None:
        self.theme_name = name if name in THEMES else "light"
        self.theme_colors = THEMES[self.theme_name]
        QApplication.instance().setPalette(theme_palette(self.theme_colors))
        self.setStyleSheet(theme_stylesheet(self.theme_colors))
        for value, action in getattr(self, "theme_actions", {}).items():
            action.setChecked(value == self.theme_name)
        if hasattr(self, "canvas"):
            self.canvas.update()

    def _change_theme(self, name: str) -> None:
        self.settings.setValue("theme", name)
        for window in QApplication.topLevelWidgets():
            if isinstance(window, VeronaWindow):
                window._apply_theme(name)

    def _change_language(self, language: str) -> None:
        if language == get_language():
            return
        self._release_alt_grab()
        snapshot = self._snapshot()
        undo, redo = self.undo_stack, self.redo_stack
        sections = self.mark_section.isExpanded(), self.connection_section.isExpanded(), self.cut_section.isExpanded()
        image_view = self.image_opacity.value(), self.cutline_view.isChecked()
        scroll_position = (self.canvas_scroll.horizontalScrollBar().value(),
                           self.canvas_scroll.verticalScrollBar().value())
        zoom = self.canvas.zoom
        ratio_locked = self.lock_ratio.isChecked()
        was_sending = self.sending
        usb_devices = self.usb_devices[:]
        usb_index = self.usb_combo.currentData()
        set_language(language)
        QSettings("Verona", "Workspace").setValue("language", language)
        self.menuBar().clear()
        self._build_ui()
        self._build_menu()
        self._restore(snapshot)
        self.mark_section.setExpanded(sections[0])
        self.connection_section.setExpanded(sections[1])
        self.cut_section.setExpanded(sections[2])
        self.cutline_view.setChecked(image_view[1])
        self.image_opacity.setValue(image_view[0])
        self.usb_devices = usb_devices
        self.usb_combo.clear()
        if usb_devices:
            for index, device in enumerate(usb_devices, 1):
                self.usb_combo.addItem(tr("Juliet USB {index} · 匯流排 {bus} / 裝置 {address}",
                                          index=index, bus=getattr(device, "bus", "?"),
                                          address=getattr(device, "address", "?")), index - 1)
            if usb_index is not None:
                self.usb_combo.setCurrentIndex(self.usb_combo.findData(usb_index))
        else:
            self.usb_combo.addItem(tr("請重新整理 USB 裝置"), None)
        self.undo_stack, self.redo_stack = undo, redo
        self.canvas.set_zoom(zoom)
        self.zoom_label.setText(f"{round(zoom * 100)}%")
        self.canvas_scroll.horizontalScrollBar().setValue(scroll_position[0])
        self.canvas_scroll.verticalScrollBar().setValue(scroll_position[1])
        self.lock_ratio.setChecked(ratio_locked)
        self.sending = was_sending
        self.send_button.setEnabled(not was_sending)
        if was_sending:
            self.status_label.setText(tr("正在傳送資料…"))
        self._refresh_history_actions()
        self._refresh_tab_titles()

    def _cut_settings(self) -> dict[str, Any]:
        host = self.wifi_host.text().strip()
        if host:
            try:
                address = ipaddress.IPv4Address(host)
                host = "" if address.is_unspecified or address.is_multicast else str(address)
            except ipaddress.AddressValueError:
                host = ""
        return {
            "custom": self.custom_settings.isChecked(),
            "speed": self.speed_spin.value(),
            "force": self.force_spin.value(),
            "tool": int(self.tool_combo.currentData()),
            "connection": self.connection_mode.currentData(),
            "host": host,
            "port": self.wifi_port.value(),
        }

    def _snapshot(self) -> dict[str, Any]:
        return {"items": _copy_items(self.items), "selected": self.selected,
                "selection": sorted(self.selected_indices()),
                "settings": self._cut_settings(), "file": self.current_file,
                "metadata": copy.deepcopy(self.metadata)}

    def push_undo(self, previous: dict[str, Any]) -> None:
        self.undo_stack.append(_copy_snapshot(previous))
        self.redo_stack.clear()
        self._refresh_history_actions()
        self._refresh_tab_titles()

    def _refresh_history_actions(self) -> None:
        if hasattr(self, "undo_action"):
            self.undo_action.setEnabled(bool(self.undo_stack))
            self.redo_action.setEnabled(bool(self.redo_stack))

    def undo(self) -> None:
        if not self.undo_stack:
            return
        self.redo_stack.append(self._snapshot())
        self._restore(self.undo_stack.pop())
        self.refresh_all()

    def redo(self) -> None:
        if not self.redo_stack:
            return
        self.undo_stack.append(self._snapshot())
        self._restore(self.redo_stack.pop())
        self.refresh_all()

    def _selection_blocked(self) -> bool:
        return any(not editable(self.items[i]) for i in self.selected_indices())

    def set_layer_property(self, indices, key, value):
        if key not in ("name", "visible", "locked", "print", "cut"):
            raise ValueError("Unknown layer property.")
        before = self._snapshot()
        for index in indices:
            item = self.items[index]
            if key not in ("visible", "locked") and (not editable(item) or item.get("registration_guide")):
                continue
            if key == "name":
                if value.strip():
                    item[key] = value.strip()
            elif key == "print":
                self._set_layer_print(item, value)
            elif (key == "visible" and value) or (key == "locked" and not value):
                item.pop(key, None)
            else:
                item[key] = bool(value)
        if before["items"] != self.items:
            if key == "print" and value:
                self._ensure_print_paper()
            self.push_undo(before)
        self.layer_panel.fingerprint = None
        self.refresh_all()

    @staticmethod
    def _set_layer_print(item, enabled):
        if enabled:
            item.pop("print", None)
            if "image_data" not in item and not item.get("print_style"):
                item["print_style"] = {"fill": None, "stroke": "#000000", "width_mm": .2}
        else:
            item["print"] = False

    def _ensure_print_paper(self):
        if not self.metadata.get("print_cut", {}).get("bounds"):
            self.metadata["print_cut"] = {"enabled": False, "paper_width": 210., "paper_height": 297.,
                                          "bounds": [6., 6., 204., 291.]}

    def _output_mode_changed(self, _index):
        mode = self.output_mode.currentData()
        if mode is None or self._selection_blocked():
            return
        before = self._snapshot()
        for index in self.selected_indices():
            item = self.items[index]
            if item.get("registration_guide"):
                continue
            self._set_layer_print(item, mode in ("print_only", "both"))
            item["cut"] = mode in ("cut_only", "both")
        if before["items"] != self.items:
            if mode in ("print_only", "both"):
                self._ensure_print_paper()
            self.push_undo(before)
            self.refresh_all()

    def move_layers(self, direction):
        selected = self.selected_indices()
        if not selected or self._selection_blocked():
            return
        order = list(range(len(self.items)))
        positions = range(len(order) - 2, -1, -1) if direction > 0 else range(1, len(order))
        for position in positions:
            neighbor = position + (1 if direction > 0 else -1)
            if order[position] in selected and order[neighbor] not in selected:
                order[position], order[neighbor] = order[neighbor], order[position]
        if order == list(range(len(self.items))):
            return
        before = self._snapshot()
        self.items = [self.items[index] for index in order]
        self.selection = {new for new, old in enumerate(order) if old in selected}
        self.selected = max(self.selection)
        self.push_undo(before)
        self.refresh_all()

    def select(self, index: int) -> None:
        self.select_many([index] if index >= 0 else [])

    def selected_indices(self) -> set[int]:
        # Keep the existing single-selection API usable by document loaders.
        if self.selected not in self.selection:
            return {self.selected} if 0 <= self.selected < len(self.items) else set()
        return {i for i in self.selection if 0 <= i < len(self.items)}

    def select_many(self, indices) -> None:
        self.selection = expand_selection(self.items, indices)
        self.selected = max(self.selection, default=-1)
        self.refresh_properties()
        self.canvas.update()

    def selection_units(self) -> list[list[int]]:
        return group_units(self.items, self.selected_indices())

    def selection_bounds(self, indices=None) -> QRectF:
        indices = sorted(self.selected_indices() if indices is None else indices)
        if not indices:
            return QRectF()
        bounds = self.canvas.item_path(self.items[indices[0]]).boundingRect()
        for index in indices[1:]:
            bounds = bounds.united(self.canvas.item_path(self.items[index]).boundingRect())
        return bounds

    def group_selected(self) -> None:
        if self._selection_blocked():
            return
        before = self._snapshot()
        if group_items(self.items, self.selected_indices()):
            self.push_undo(before)
            self.refresh_all()

    def ungroup_selected(self) -> None:
        if self._selection_blocked():
            return
        before = self._snapshot()
        if ungroup_items(self.items, self.selected_indices()):
            self.push_undo(before)
            self.refresh_all()

    def link_selected_cut_paths(self) -> None:
        if self._selection_blocked():
            return
        from .editor_actions import build_edit
        before = self._snapshot()
        try:
            items, selection, _ = build_edit(self.items, self.metadata,
                sorted(self.selected_indices()), "link_cut", {})
        except ValueError:
            QMessageBox.warning(self, tr("無法連動切線"),
                tr("請選取一張圖片與一個獨立切線物件。該切線物件的全部輪廓會取代圖片原本的切線；可復原。"))
            return
        try:
            from .edge_bleed import check_edge_bleed
            check_edge_bleed(items[selection[0]])
        except ValueError as error:
            QMessageBox.warning(self, tr("無法連動切線"), str(error))
            return
        self.items = items
        self.select_many(selection)
        self.push_undo(before)
        self.refresh_all()

    def edit_selected_image(self) -> None:
        if self._selection_blocked():
            return
        from .mask_edit import EditImageDialog
        selected = sorted(self.selected_indices())
        if len(selected) != 1 or "image_data" not in self.items[selected[0]]:
            QMessageBox.warning(self, tr("編輯圖片遮罩"), tr("請選取一張圖片。"))
            return
        try:
            index = selected[0]
            dialog = EditImageDialog(self.items[index], self)
            try:
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return
                item = dialog.build_items()[0]
                if item == self.items[index]:
                    return
                from .edge_bleed import check_edge_bleed
                check_edge_bleed(item)
                before = self._snapshot()
                self.items[index] = item
                self.push_undo(before)
                self.refresh_all()
            finally:
                dialog.deleteLater()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, tr("編輯圖片遮罩"), str(error))

    def edit_selected_bleed(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(i for i in self.selected_indices() if "image_data" in self.items[i]
                         and not self.items[i].get("registration_guide"))
        if not indices:
            QMessageBox.warning(self, tr("設定列印出血"), tr("請選取一張圖片。"))
            return
        dialog = BleedDialog(self, indices)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            items, selection, _ = dialog.build_items()
            if items != self.items:
                before = self._snapshot()
                self.items = items
                self.select_many(selection)
                self.push_undo(before)
                self.refresh_all()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, tr("設定列印出血"), str(error))
        finally:
            dialog.deleteLater()

    def pack_selected(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(self.selected_indices())
        if not indices:
            QMessageBox.warning(self, tr("自動排版"), tr("請先選取物件。"))
            return
        dialog = PackingDialog(self, indices)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            items, selection, _ = dialog.build_items()
            before = self._snapshot()
            self.items = items
            self.select_many(selection)
            self.push_undo(before)
            self.refresh_all()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, tr("自動排版"), str(error))
        finally:
            dialog.deleteLater()

    def edit_nodes(self):
        if self._selection_blocked():
            return
        from .node_editor import NodeEditorDialog
        indices = sorted(self.selected_indices())
        if len(indices) != 1 or self.items[indices[0]].get("registration_guide"):
            QMessageBox.warning(self, tr("編輯節點與曲線"), tr("請選取一個可編輯的物件。"))
            return
        index = indices[0]
        dialog = NodeEditorDialog(self.items[index], self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.item != self.items[index]:
                from .edge_bleed import check_edge_bleed
                try:
                    check_edge_bleed(dialog.item)
                except ValueError as error:
                    QMessageBox.warning(self, tr("編輯節點與曲線"), str(error))
                    return
                before = self._snapshot()
                self.items[index] = dialog.item
                self.push_undo(before)
                self.refresh_all()
        finally:
            dialog.deleteLater()

    def edit_vector_print(self):
        if self._selection_blocked():
            return
        from .vector_print import VectorPrintDialog
        from .editor_document import build_document_edit
        indices = sorted(self.selected_indices())
        if not indices or any("image_data" in self.items[i] or self.items[i].get("registration_guide") for i in indices):
            QMessageBox.warning(self, tr("設定向量列印"), tr("請選取可列印的向量物件。"))
            return
        dialog = VectorPrintDialog(self.items[indices[0]].get("print_style"), self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                items, _, _, _, _ = build_document_edit(self.items, self.metadata, self._cut_settings(),
                    indices, "vector_print", dialog.print_style())
                if items != self.items:
                    before = self._snapshot()
                    self.items = items
                    self.push_undo(before)
                    self.refresh_all()
        finally:
            dialog.deleteLater()

    def set_path_order(self, enabled):
        from .path_order import analyze_path_order
        if enabled == self.metadata.get("optimize_paths", False):
            return
        try:
            _, details = analyze_path_order(self.items, self.metadata, enabled)
        except ValueError as error:
            self.refresh_all()
            QMessageBox.warning(self, tr("自動排序切割路徑（內孔先切）"), str(error))
            return
        before = self._snapshot()
        if enabled:
            self.metadata["optimize_paths"] = True
        else:
            self.metadata.pop("optimize_paths", None)
        self.push_undo(before)
        self.refresh_all()
        self.status_label.setText(tr("估計空走：{before} → {after} mm；不含定位搜尋與實機耗時。",
            before=f"{details['travel_before_mm']:.2f}", after=f"{details['travel_after_mm']:.2f}")
            if enabled else tr("已還原文件切線順序。"))

    def _toggle_cutline_view(self, enabled: bool) -> None:
        self.image_opacity.setValue(30 if enabled else 100)
        self.canvas.update()

    def align_selected(self, mode: str) -> None:
        if self._selection_blocked():
            return
        from .alignment import alignment_offsets
        units = self.selection_units()
        if len(units) < (3 if mode.startswith("distribute_") else 2):
            return
        bounds = [self.selection_bounds(unit) for unit in units]
        offsets = alignment_offsets([(b.left(), b.top(), b.right(), b.bottom()) for b in bounds], mode)
        if not any(dx or dy for dx, dy in offsets):
            return
        before = self._snapshot()
        for unit, (dx, dy) in zip(units, offsets):
            for index in unit:
                _transform_item(self.items[index], QTransform.fromTranslate(dx, dy))
        self.push_undo(before)
        self.refresh_all()

    def _restore(self, snapshot: dict[str, Any]) -> None:
        self.items = _copy_items(snapshot["items"])
        self.selected = snapshot["selected"]
        self.selection = set(snapshot.get("selection", [self.selected] if self.selected >= 0 else []))
        self._set_cut_settings(snapshot["settings"])
        self.current_file = snapshot["file"]
        self.metadata = copy.deepcopy(snapshot.get("metadata", {}))
        if hasattr(self, "native_preview_action"):
            self.native_preview_action.setEnabled(bool(self.metadata.get("source_preview")))
        self.file_label.setText(Path(self.current_file).name if self.current_file else tr("未命名設計"))
        self.refresh_all()

    def _set_cut_settings(self, settings: dict[str, Any]) -> None:
        self.custom_settings.setChecked(settings["custom"])
        self.speed_spin.setValue(settings["speed"])
        self.force_spin.setValue(settings["force"])
        self.tool_combo.setCurrentIndex(self.tool_combo.findData(settings["tool"]))
        self.connection_mode.setCurrentIndex(self.connection_mode.findData(settings["connection"]))
        self.wifi_host.setText(settings["host"])
        self.wifi_port.setValue(settings["port"])

    def refresh_properties(self) -> None:
        count = len(self.selected_indices())
        enabled = count == 1
        blocked = self._selection_blocked()
        unit_count = 0 if blocked else len(self.selection_units())
        for mode, button in self.alignment_buttons.items():
            button.setEnabled(unit_count >= (3 if mode.startswith("distribute_") else 2))
        self.group_button.setEnabled(unit_count >= 2)
        has_group = not blocked and any(self.items[i].get("group_path") for i in self.selected_indices())
        self.ungroup_button.setEnabled(has_group)
        if hasattr(self, "group_action"):
            self.group_action.setEnabled(unit_count >= 2)
            self.ungroup_action.setEnabled(has_group)
        self.object_name.setEnabled(enabled and not blocked)
        for control in (self.width_spin, self.height_spin, self.x_spin, self.y_spin, *self.selection_edit_buttons):
            control.setEnabled(count > 0 and not blocked)
        self.cut_checkbox.blockSignals(True)
        self.cut_checkbox.setChecked(self.items[self.selected].get("cut", True) if enabled else True)
        self.cut_checkbox.setEnabled(enabled and not blocked and not self.items[self.selected].get("registration_guide", False))
        self.cut_checkbox.blockSignals(False)
        modes = set()
        for index in self.selected_indices():
            item = self.items[index]
            prints = printable({**item, "visible": True})
            cuts = item.get("cut", True)
            modes.add("both" if prints and cuts else "print_only" if prints else "cut_only" if cuts else "none")
        self.output_mode.blockSignals(True)
        self.output_mode.setCurrentIndex(self.output_mode.findData(next(iter(modes))) if len(modes) == 1 else -1)
        self.output_mode.setEnabled(count > 0 and not blocked and not any(
            self.items[i].get("registration_guide") for i in self.selected_indices()))
        self.output_mode.blockSignals(False)
        self.layer_panel.refresh()
        if count:
            item = self.items[self.selected]
            bounds = self.selection_bounds()
            self.object_name.blockSignals(True)
            self.object_name.setText(_display_name(item.get("name", "路徑")) if enabled else "")
            self.object_name.blockSignals(False)
            self.width_spin.blockSignals(True)
            self.height_spin.blockSignals(True)
            self.width_spin.setValue(max(0.1, bounds.width()))
            self.height_spin.setValue(max(0.1, bounds.height()))
            self.width_spin.blockSignals(False)
            self.height_spin.blockSignals(False)
            self.x_spin.setValue(max(0, bounds.left()))
            self.y_spin.setValue(max(0, bounds.top()))
        else:
            self.object_name.clear()
            self.width_spin.setValue(0.1)
            self.height_spin.setValue(0.1)
            self.x_spin.setValue(0)
            self.y_spin.setValue(0)
        self.status_label.setText(
            tr("已選取 {selected} 個物件 · 共 {count} 個物件", selected=count, count=len(self.items))
            if count > 1 else tr("{count} 個路徑 · 工作區 300 × 300 mm", count=len(self.items)))
        self._refresh_bounds_warning()

    def _print_cut_violations(self) -> dict[int, str]:
        from .commands import registration_point_error
        registration = self.metadata.get("print_cut", {})
        if not registration.get("enabled") or not registration.get("bounds"):
            return {}
        violations = {}
        for index, item in enumerate(self.items):
            if not cuttable(item):
                continue
            path = self.canvas.item_path(item)
            if path.isEmpty():
                continue
            # All contours are straight segments. Extrema of the cached path
            # suffice for a rectangular frame, including zero-width/height paths.
            bounds = path.controlPointRect()
            for point in (bounds.topLeft(), bounds.bottomRight()):
                error = registration_point_error(point.x(), point.y(), registration["bounds"],
                                                 registration.get("offset", (0, 0)))
                if error:
                    violations[index] = error
                    break
        return violations

    def _refresh_bounds_warning(self) -> None:
        violations = self._print_cut_violations()
        self.bounds_warning.setVisible(bool(violations))
        self.bounds_warning.setText(tr(
            "Print & Cut：{count} 個物件的切割線出界，無法送件。請移回虛線框內或縮小；也請檢查切割微調。",
            count=len(violations)) if violations else "")
        self.bounds_warning.setToolTip("\n".join(
            f"{_display_name(self.items[index].get('name', '路徑'))}：{tr(error)}"
            for index, error in violations.items()))

    def refresh_all(self) -> None:
        if hasattr(self, "path_order_action"):
            self.path_order_action.blockSignals(True)
            self.path_order_action.setChecked(self.metadata.get("optimize_paths", False))
            self.path_order_action.blockSignals(False)
        self.canvas._path_cache.clear()
        self.canvas.update()
        self.refresh_properties()
        self._refresh_history_actions()
        self._sync_registration_controls()
        self._refresh_tab_titles()

    def _sync_registration_controls(self) -> None:
        registration = self.metadata.get("print_cut", {})
        self.registration_checkbox.blockSignals(True)
        self.registration_checkbox.setEnabled(bool(registration))
        self.registration_checkbox.setChecked(registration.get("enabled", False))
        self.registration_checkbox.blockSignals(False)
        bounds = registration.get("bounds")
        if bounds:
            self.registration_info.setText(tr("定位範圍：{width} × {height} mm",
                width=f"{bounds[2] - bounds[0]:.2f}", height=f"{bounds[3] - bounds[1]:.2f}"))
        elif registration.get("enabled"):
            self.registration_info.setText(tr("此原生檔缺少可用的定位標記範圍。"))
        else:
            self.registration_info.setText(tr("一般切割：由機器設定原點。"))
        width, height = registration.get("paper_width", 0), registration.get("paper_height", 0)
        enabled = bool(registration.get("enabled") and bounds and width > 0 and height > 0)
        margins = ([bounds[0], bounds[1], width - bounds[2], height - bounds[3]]
                   if bounds and width > 0 and height > 0 else [6] * 4)
        from .printing import registration_profile
        stroke, short_length = registration_profile(registration)
        self.mark_stroke.blockSignals(True)
        self.mark_stroke.setValue(stroke)
        self.mark_stroke.blockSignals(False)
        self.mark_short_length.setValue(short_length)
        self.mark_stroke.setEnabled(enabled)
        self.mark_short_length.setEnabled(enabled)
        minimums = registration.get("printer_margins", [0] * 4)
        for spin, value, minimum in zip(self.mark_margins, margins, minimums):
            spin.setMinimum(minimum + stroke / 2)
            spin.setValue(value)
            spin.setEnabled(enabled)
        self.apply_marks_button.setEnabled(enabled)
        self.maximize_marks_button.setEnabled(enabled)
        for spin, value in zip(self.registration_offsets, registration.get("offset", (0, 0))):
            spin.setValue(value)
            spin.setEnabled(enabled)
        self.apply_offsets_button.setEnabled(enabled)

    def apply_registration_offset(self) -> None:
        before = self._snapshot()
        registration = self.metadata["print_cut"]
        old_offset = registration.get("offset")
        registration["offset"] = [spin.value() for spin in self.registration_offsets]
        try:
            self._encoded_plt()
        except ValueError as error:
            if old_offset is None:
                registration.pop("offset")
            else:
                registration["offset"] = old_offset
            QMessageBox.warning(self, tr("無法套用切割微調"), str(error))
            return
        self.push_undo(before)
        self.refresh_all()
        self.status_label.setText(tr("已套用切割微調；列印圖案使用原設計位置。"))

    def _update_mark_minimums(self) -> None:
        minimums = self.metadata.get("print_cut", {}).get("printer_margins", [0] * 4)
        for spin, minimum in zip(self.mark_margins, minimums):
            spin.setMinimum(minimum + self.mark_stroke.value() / 2)

    def _registration_toggled(self, enabled: bool) -> None:
        registration = self.metadata.get("print_cut")
        if not registration:
            return
        before = self._snapshot()
        registration["enabled"] = enabled
        self.push_undo(before)
        self.refresh_all()

    def apply_mark_margins(self) -> None:
        registration = self.metadata["print_cut"]
        left, top, right_margin, bottom_margin = [spin.value() for spin in self.mark_margins]
        right = registration["paper_width"] - right_margin
        bottom = registration["paper_height"] - bottom_margin
        if left >= right or top >= bottom:
            QMessageBox.warning(self, tr("定位範圍無效"), tr("紙邊距太大，沒有剩餘的定位範圍。"))
            return
        before = self._snapshot()
        registration["bounds"] = [left, top, right, bottom]
        registration["mark_stroke"] = self.mark_stroke.value()
        registration["mark_short_length"] = self.mark_short_length.value()
        self.push_undo(before)
        self.refresh_all()
        self.status_label.setText(tr("定位標記已變更，請重新列印。"))

    def maximize_mark_area(self) -> None:
        from PySide6.QtGui import QPageSize, QPageLayout
        from PySide6.QtCore import QSizeF
        from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
        printers = QPrinterInfo.availablePrinters()
        if not printers:
            QMessageBox.warning(self, tr("找不到印表機"), tr("先在 Linux 設定印表機，再讀取可列印邊界。"))
            return
        selected = printers[0]
        if len(printers) > 1:
            name, accepted = QInputDialog.getItem(self, tr("選擇印表機"), tr("印表機"),
                                                  [printer.printerName() for printer in printers], 0, False)
            if not accepted:
                return
            selected = next(printer for printer in printers if printer.printerName() == name)
        registration = self.metadata["print_cut"]
        printer = QPrinter(selected)
        printer.setPageSize(QPageSize(QSizeF(registration["paper_width"], registration["paper_height"]),
                                      QPageSize.Unit.Millimeter))
        page_layout = printer.pageLayout()
        page_layout.setUnits(QPageLayout.Unit.Millimeter)
        margins = page_layout.minimumMargins()
        minimums = [margins.left(), margins.top(), margins.right(), margins.bottom()]
        half = self.mark_stroke.value() / 2
        bounds = [minimums[0] + half, minimums[1] + half,
                  registration["paper_width"] - minimums[2] - half,
                  registration["paper_height"] - minimums[3] - half]
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            QMessageBox.warning(self, tr("定位範圍無效"), tr("紙邊距太大，沒有剩餘的定位範圍。"))
            return
        before = self._snapshot()
        registration["mark_stroke"] = self.mark_stroke.value()
        registration["mark_short_length"] = self.mark_short_length.value()
        registration["printer_margins"] = minimums
        registration["bounds"] = bounds
        self.push_undo(before)
        self.refresh_all()
        self.status_label.setText(tr("已依印表機邊界最大化，請重新列印並測試對位。"))

    def change_zoom(self, factor: float) -> None:
        self.canvas.set_zoom(self.canvas.zoom * factor)
        self.zoom_label.setText(f"{round(self.canvas.zoom * 100)}%")

    def _add_path(self, name: str, path: QPainterPath) -> None:
        path.translate(PAGE_WIDTH_MM / 2 - path.boundingRect().center().x(),
                       PAGE_HEIGHT_MM / 2 - path.boundingRect().center().y())
        before = self._snapshot()
        self.items.append({"name": name, "contours": _flatten(path), "cut": True})
        self.push_undo(before)
        self.select(len(self.items) - 1)

    def add_rectangle(self) -> None:
        path = QPainterPath()
        path.addRect(QRectF(0, 0, 40, 25))
        self._add_path(tr("矩形"), path)

    def add_ellipse(self) -> None:
        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, 30, 30))
        self._add_path(tr("圓形"), path)

    def add_text(self) -> None:
        text, accepted = QInputDialog.getText(self, tr("建立文字輪廓"), tr("文字"))
        if not accepted or not text:
            return
        path = QPainterPath()
        font = QFont("Noto Sans")
        font.setPointSizeF(10 * 72 / 25.4)
        path.addText(QPointF(0, 10), font, text)
        self._add_path(tr("文字輪廓"), path)

    def import_svg(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, tr("匯入 SVG"), "", tr("SVG 向量圖 (*.svg)"))
        if not filename:
            return
        try:
            from . import core
            contours = core.load_svg(filename)
            if not contours:
                raise ValueError(tr("SVG 沒有可匯入的向量輪廓。"))
            path = _path_from_contours(contours)
            self._add_path(Path(filename).stem, path)
            self.current_file = None
            self.metadata = {}
            self.native_preview_action.setEnabled(False)
            self.file_label.setText(Path(filename).name)
        except Exception as error:
            QMessageBox.critical(self, tr("匯入失敗"), str(error))

    def import_image(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, tr("匯入圖片"), "",
            tr("點陣圖片 (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)"))
        if not filename:
            return
        try:
            from PySide6.QtGui import QImageReader
            from .raster_import import ImportImageDialog
            reader = QImageReader(filename)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.width() * size.height() > 16_000_000:
                raise ValueError(tr("圖片尺寸超過 1600 萬像素"))
            image = reader.read()
            if image.isNull():
                raise ValueError(tr("無法讀取圖片或此格式未受支援。"))
            dialog = ImportImageDialog(image, Path(filename).stem, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self._append_image_items(dialog.build_items(), dialog.mode_combo.currentData())
        except Exception as error:
            QMessageBox.critical(self, tr("匯入失敗"), str(error))

    def _append_image_items(self, items, mode):
        if not items:
            raise ValueError(tr("遮罩沒有可匯入的輪廓，請調整去背設定。"))
        before = self._snapshot()
        registration = self.metadata.get("print_cut", {})
        if mode in ("print_cut", "background") and not registration.get("bounds"):
            registration = {"enabled": mode == "print_cut", "paper_width": 210., "paper_height": 297.,
                            "bounds": [6., 6., 204., 291.]}
            self.metadata["print_cut"] = registration
        elif mode == "print_cut":
            registration["enabled"] = True
        if registration.get("bounds"):
            left, top, right, bottom = registration["bounds"]
            all_bounds = _item_path(items[0]).boundingRect()
            for item in items[1:]:
                all_bounds = all_bounds.united(_item_path(item).boundingRect())
            center = all_bounds.center()
            transform = QTransform.fromTranslate((left+right)/2-center.x(), (top+bottom)/2-center.y())
            for item in items:
                _transform_item(item, transform)
        start = len(self.items)
        self.items.extend(items)
        if len(items) > 1:
            group_items(self.items, range(start, len(self.items)))
        self.select_many(range(start, len(self.items)))
        self.push_undo(before)
        self.refresh_all()

    def import_document_artwork(self):
        from .document_import import pdf_page_count, render_artwork
        from .raster_import import ImportImageDialog
        filename, _ = QFileDialog.getOpenFileName(self, tr("匯入 PDF／SVG 圖稿"), "", "PDF / SVG (*.pdf *.svg)")
        if not filename:
            return
        dialog = None
        try:
            page = 1
            if Path(filename).suffix.lower() == ".pdf":
                count = pdf_page_count(filename)
                if count > 1:
                    page, accepted = QInputDialog.getInt(self, tr("匯入 PDF／SVG 圖稿"), tr("頁碼"), 1, 1, count)
                    if not accepted:
                        return
            image, dimensions, _ = render_artwork(filename, page)
            dialog = ImportImageDialog(image, Path(filename).stem, self)
            dialog.setWindowTitle(tr("匯入 PDF／SVG 圖稿"))
            for spin, value in ((dialog.width_spin, dimensions["width_mm"]), (dialog.height_spin, dimensions["height_mm"]),
                                (dialog.dpi_spin, image.width()*25.4/dimensions["width_mm"])):
                spin.blockSignals(True);spin.setValue(value);spin.blockSignals(False)
            dialog.update_preview()
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self._append_image_items(dialog.build_items(), dialog.mode_combo.currentData())
        except (ValueError, OSError) as error:
            QMessageBox.critical(self, tr("匯入失敗"), str(error))
        finally:
            if dialog is not None:
                dialog.deleteLater()

    def apply_size(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(self.selected_indices())
        if not indices:
            return
        before = self._snapshot()
        bounds = self.selection_bounds()
        if bounds.width() <= 0 or bounds.height() <= 0:
            return
        sx = self.width_spin.value() / bounds.width()
        sy = self.height_spin.value() / bounds.height()
        left, top = self.x_spin.value(), self.y_spin.value()
        transform = QTransform()
        transform.translate(left, top)
        transform.scale(sx, sy)
        transform.translate(-bounds.left(), -bounds.top())
        for index in indices:
            _transform_item(self.items[index], transform)
        if len(indices) == 1:
            item = self.items[indices[0]]
            item["name"] = self.object_name.text() or item.get("name", "路徑")
        self.push_undo(before)
        self.refresh_all()

    def _dimension_changed(self, axis: str, value: float) -> None:
        if not self.lock_ratio.isChecked() or self.selected < 0:
            return
        bounds = self.selection_bounds()
        if bounds.width() <= 0 or bounds.height() <= 0:
            return
        target = self.height_spin if axis == "width" else self.width_spin
        target.blockSignals(True)
        target.setValue(value * (bounds.height() / bounds.width() if axis == "width"
                                 else bounds.width() / bounds.height()))
        target.blockSignals(False)

    def new_design(self) -> None:
        self._documents[self._active_document] = self._tab_state()
        self._active_document = len(self._documents)
        self._documents.append({})
        self.items = []
        self.metadata = {}
        self.native_preview_action.setEnabled(False)
        self.selected = -1
        self.selection = set()
        self.undo_stack, self.redo_stack = [], []
        self.current_file = None
        self.file_label.setText(tr("未命名設計"))
        self.saved_snapshot = self._document_state()
        self.canvas.set_zoom(DEFAULT_ZOOM)
        self.cutline_view.setChecked(False)
        self.image_opacity.setValue(100)
        self.zoom_label.setText(f"{round(DEFAULT_ZOOM * 100)}%")
        self.refresh_all()

    def duplicate_selected(self) -> None:
        if self._selection_blocked():
            return
        indices = sorted(self.selected_indices())
        if not indices:
            return
        before = self._snapshot()
        copies = _copy_items([self.items[i] for i in indices])
        remap_group_ids(copies)
        start = len(self.items)
        for source in copies:
            _transform_item(source, QTransform.fromTranslate(5, 5))
            source["name"] = tr("{name} 複本", name=_display_name(source.get("name", "路徑")))
        self.items.extend(copies)
        self.push_undo(before)
        self.select_many(range(start, len(self.items)))

    def delete_selected(self) -> None:
        if self._selection_blocked():
            return
        indices = self.selected_indices()
        if not indices:
            return
        before = self._snapshot()
        self.items = [item for index, item in enumerate(self.items) if index not in indices]
        self.selected, self.selection = -1, set()
        self.push_undo(before)
        self.refresh_all()

    def transform_selected(self, *, mirror_x: bool = False, mirror_y: bool = False,
                           rotation: float = 0) -> None:
        if self._selection_blocked():
            return
        indices = sorted(self.selected_indices())
        if not indices:
            return
        before = self._snapshot()
        bounds = self.canvas.item_path(self.items[indices[0]]).boundingRect()
        for index in indices[1:]:
            bounds = bounds.united(self.canvas.item_path(self.items[index]).boundingRect())
        center = bounds.center()
        transform = QTransform()
        transform.translate(center.x(), center.y())
        transform.rotate(rotation)
        transform.scale(-1 if mirror_x else 1, -1 if mirror_y else 1)
        transform.translate(-center.x(), -center.y())
        for index in indices:
            _transform_item(self.items[index], transform)
        self.push_undo(before)
        self.refresh_all()

    def show_preview(self) -> None:
        try:
            PreviewDialog(self).exec()
        except Exception as error:
            QMessageBox.warning(self, tr("無法建立切割預覽"), str(error))

    def show_native_preview(self) -> None:
        encoded = self.metadata.get("source_preview")
        if not encoded:
            return
        try:
            pixmap = QPixmap()
            if not pixmap.loadFromData(base64.b64decode(encoded, validate=True), "PNG"):
                raise ValueError(tr("原廠預覽圖片格式無效。"))
            dialog = QDialog(self)
            dialog.setWindowTitle(tr("原廠預覽"))
            layout = QVBoxLayout(dialog)
            label = QLabel()
            label.setPixmap(pixmap.scaled(1000, 800, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
            layout.addWidget(label)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            buttons.accepted.connect(dialog.accept)
            layout.addWidget(buttons)
            dialog.exec()
        except (ValueError, binascii.Error) as error:
            QMessageBox.warning(self, tr("無法顯示原廠預覽"), str(error))

    def open_design(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, tr("開啟設計"), "", tr("Verona 設計 (*.jdesign *.json *.lds)"))
        if not filename:
            return
        self._refresh_tab_titles()
        for index, state in enumerate(self._documents):
            if state["file"] and Path(state["file"]).resolve() == Path(filename).resolve():
                self.tab_bar.setCurrentIndex(index)
                return
        new_tab = bool(self.items or self.current_file or self.is_dirty())
        if new_tab:
            self.new_design()
        if not self._load_document(filename) and new_tab:
            self.close_document(self._active_document)

    def _load_document(self, filename: str) -> bool:
        try:
            self._install_document(filename, *self._read_document(filename))
            return True
        except Exception as error:
            QMessageBox.critical(self, tr("開啟失敗"), str(error))
            return False

    def _read_document(self, filename):
        if Path(filename).suffix.lower() == ".lds":
            from .lds import load_lds
            native = load_lds(filename)
            return native.items, self._cut_settings(), native.metadata
        from .input_safety import read_design
        data = read_design(filename)
        items, settings = self._validate_document(data)
        document_settings = data.get("settings", {})
        if "host" not in document_settings:
            settings["host"] = self.settings.value("wifi/host", "", type=str)
        if "port" not in document_settings:
            settings["port"] = self.settings.value("wifi/port", TCP_PORT, type=int)
        return items, settings, data.get("metadata", {})

    def _install_document(self, filename, items, settings, metadata):
        self.items = items
        self.metadata = copy.deepcopy(metadata)
        self.native_preview_action.setEnabled(bool(self.metadata.get("source_preview")))
        self._set_cut_settings(settings)
        self.selected = -1
        self.selection = set()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.current_file = filename
        self.file_label.setText(Path(filename).name)
        self.saved_snapshot = self._document_state()
        self.refresh_all()

    @staticmethod
    def _validate_document(data: Any, trusted_items=()) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not isinstance(data, dict) or data.get("format") not in ("verona", "juliet-design-linux"):
            raise ValueError(tr("這不是 Verona 設計檔。"))
        if data.get("version") != 1 or data.get("units") != "mm":
            raise ValueError(tr("設計檔版本或座標單位不支援。"))
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise ValueError(tr("設計檔缺少 items 清單。"))
        from .input_safety import check_geometry_budget
        check_geometry_budget(raw_items)
        items = []
        trusted = {}
        for item in trusted_items:
            trusted.setdefault(_trust_key(item), []).append(item)
        for number, raw_item in enumerate(raw_items, 1):
            key = _trust_key(raw_item) if trusted else None
            if key is not None and any(raw_item == known for known in trusted.get(key, ())):
                # Unchanged objects already in the editor skip image decoding,
                # mask and edge-bleed checks; only edited objects are re-validated.
                items.append(_copy_items([raw_item])[0])
                continue
            if not isinstance(raw_item, dict) or not isinstance(raw_item.get("name"), str):
                raise ValueError(tr("第 {number} 個物件格式錯誤。", number=number))
            contours = raw_item.get("contours")
            if not isinstance(contours, list) or not contours:
                raise ValueError(tr("第 {number} 個物件沒有路徑。", number=number))
            safe_item = {"name": raw_item["name"],
                         "contours": VeronaWindow._validate_contours(contours, number)}
            if "node_paths" in raw_item:
                from .nodes import validate_saved_paths
                safe_item["node_paths"] = validate_saved_paths(safe_item, raw_item["node_paths"])
            if "print_style" in raw_item:
                from .artwork import validate_print_style
                if "image_data" in raw_item or raw_item.get("registration_guide"):
                    raise ValueError("Only ordinary vector objects have a print style.")
                style = validate_print_style(raw_item["print_style"])
                if style["fill"] or style["stroke"]:
                    safe_item["print_style"] = style
            if "group_path" in raw_item:
                path = raw_item["group_path"]
                if (not isinstance(path, list) or not path
                        or any(not isinstance(value, str) or not value for value in path)
                        or len(set(path)) != len(path)):
                    raise ValueError(tr("第 {number} 個物件的群組資料無效。", number=number))
                safe_item["group_path"] = path[:]
            for key in ("cut", "print", "visible", "locked"):
                if key in raw_item:
                    if type(raw_item[key]) is not bool:
                        raise ValueError(tr("第 {number} 個物件的切割狀態無效。", number=number))
                    safe_item[key] = raw_item[key]
            if "source_color" in raw_item:
                color = raw_item["source_color"]
                if type(color) is not int or not 0 <= color <= 0xFFFFFFFF:
                    raise ValueError(tr("第 {number} 個物件的原始顏色無效。", number=number))
                safe_item["source_color"] = color
            if "registration_guide" in raw_item:
                if type(raw_item["registration_guide"]) is not bool:
                    raise ValueError(tr("第 {number} 個物件的定位狀態無效。", number=number))
                safe_item["registration_guide"] = raw_item["registration_guide"]
            if any(key in raw_item for key in ("image_transform", "image_clip")) and "image_data" not in raw_item:
                raise ValueError(tr("第 {number} 個物件的圖片資料無效。", number=number))
            if "image_data" in raw_item:
                try:
                    encoded = raw_item["image_data"]
                    if not isinstance(encoded, str):
                        raise ValueError
                    _qoi_image(encoded)
                except (ValueError, binascii.Error) as error:
                    raise ValueError(tr("第 {number} 個物件的圖片資料無效。", number=number)) from error
                safe_item["image_data"] = encoded
                transform = raw_item.get("image_transform")
                if (not isinstance(transform, (list, tuple)) or len(transform) != 6
                        or any(isinstance(value, bool) or not isinstance(value, (int, float))
                               or not math.isfinite(value) for value in transform)):
                    raise ValueError(tr("第 {number} 個物件的圖片轉換無效。", number=number))
                safe_item["image_transform"] = [float(value) for value in transform]
                if "image_clip" in raw_item:
                    safe_item["image_clip"] = VeronaWindow._validate_contours(raw_item["image_clip"], number, allow_empty=True)
                if "bleed" in raw_item:
                    from .artwork import validate_bleed
                    safe_item["bleed"] = validate_bleed(raw_item["bleed"])
                    if safe_item["bleed"].get("mode") == "edge" and safe_item["bleed"]["width_mm"]:
                        from .edge_bleed import edge_bleed_image
                        edge_bleed_image(safe_item)
                if "raster_bleed_mm" in raw_item:
                    value = raw_item["raster_bleed_mm"]
                    if (isinstance(value, bool) or not isinstance(value, (int, float))
                            or not math.isfinite(value) or not 0 < value <= 5):
                        raise ValueError(tr("第 {number} 個物件的圖片資料無效。", number=number))
                    safe_item["raster_bleed_mm"] = float(value)
                if "raster_bleed_scale" in raw_item:
                    scale = raw_item["raster_bleed_scale"]
                    if ("raster_bleed_mm" not in safe_item or not isinstance(scale, (list, tuple))
                            or len(scale) != 2 or any(isinstance(value, bool)
                                or not isinstance(value, (int, float)) or not math.isfinite(value)
                                or value <= 0 for value in scale)):
                        raise ValueError(tr("第 {number} 個物件的圖片資料無效。", number=number))
                    safe_item["raster_bleed_scale"] = [float(value) for value in scale]
                if "trace_options" in raw_item:
                    from .mask_edit import validate_trace_options
                    safe_item["trace_options"] = validate_trace_options(_qoi_image(encoded), raw_item["trace_options"])
                if "trace_mask" in raw_item:
                    from .mask_edit import working_mask
                    candidate = {**safe_item, "trace_mask": raw_item["trace_mask"]}
                    working_mask(candidate, _qoi_image(encoded))
                    safe_item["trace_mask"] = raw_item["trace_mask"]
            elif "bleed" in raw_item:
                raise ValueError("Bleed is only supported on image objects.")
            if "image_data" not in raw_item and any(key in raw_item for key in
                    ("trace_options", "trace_mask", "raster_bleed_mm", "raster_bleed_scale")):
                raise ValueError("Saved tracing state requires an image object.")
            items.append(safe_item)

        defaults = {"custom": False, "speed": 5, "force": 10, "tool": 0,
                    "connection": "wifi", "host": "", "port": TCP_PORT}
        raw_settings = data.get("settings", {})
        if not isinstance(raw_settings, dict):
            raise ValueError(tr("設計檔中的切割設定無效。"))
        settings = {**defaults, **raw_settings}
        host = settings["host"]
        if not isinstance(host, str):
            raise ValueError(tr("設計檔中的連線 IP 無效。"))
        if host:
            try:
                address = ipaddress.IPv4Address(host)
                if address.is_unspecified or address.is_multicast:
                    raise ValueError
            except ValueError as error:
                raise ValueError(tr("設計檔中的連線 IP 無效。")) from error
        if (type(settings["custom"]) is not bool
                or type(settings["speed"]) is not int or not 1 <= settings["speed"] <= 13
                or type(settings["force"]) is not int or not 1 <= settings["force"] <= 160
                or type(settings["tool"]) is not int or settings["tool"] not in (0, 1)
                or settings["connection"] not in ("wifi", "usb")
                or type(settings["port"]) is not int or not 1 <= settings["port"] <= 65535):
            raise ValueError(tr("設計檔中的切割或連線設定無效。"))
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError(tr("設計檔中的中繼資料無效。"))
        if "optimize_paths" in metadata and type(metadata["optimize_paths"]) is not bool:
            raise ValueError(tr("設計檔中的路徑排序設定無效。"))
        if "print_cut" in metadata:
            registration = metadata["print_cut"]
            if not isinstance(registration, dict) or type(registration.get("enabled")) is not bool:
                raise ValueError(tr("設計檔中的定位設定無效。"))
            bounds = registration.get("bounds")
            if bounds is not None and (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) for value in bounds)
                    or not (bounds[0] < bounds[2] and bounds[1] < bounds[3])):
                raise ValueError(tr("設計檔中的定位設定無效。"))
            dimensions = [registration.get("paper_width"), registration.get("paper_height")]
            minimums = registration.get("printer_margins", [0] * 4)
            if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0 for value in dimensions)
                    or not isinstance(minimums, list) or len(minimums) != 4
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) or value < 0 for value in minimums)):
                raise ValueError(tr("設計檔中的定位設定無效。"))
            if registration["enabled"] and min(dimensions) <= 0:
                raise ValueError(tr("設計檔中的定位設定無效。"))
            from .printing import registration_profile
            registration_profile(registration)
            from .commands import validate_registration_offset
            validate_registration_offset(registration.get("offset", (0, 0)))
        return items, {key: settings[key] for key in defaults}

    @staticmethod
    def _validate_contours(contours: Any, number: int, *, allow_empty: bool = False):
        if not isinstance(contours, list) or (not contours and not allow_empty):
            raise ValueError(tr("第 {number} 個物件沒有路徑。", number=number))
        safe = []
        for contour in contours:
            if not isinstance(contour, list) or len(contour) < 2:
                raise ValueError(tr("第 {number} 個物件有空白或過短的輪廓。", number=number))
            points = []
            for point in contour:
                if (not isinstance(point, (list, tuple)) or len(point) != 2
                        or any(isinstance(value, bool) or not isinstance(value, (int, float))
                               or not math.isfinite(value) for value in point)):
                    raise ValueError(tr("第 {number} 個物件包含無效座標。", number=number))
                points.append((float(point[0]), float(point[1])))
            safe.append(points)
        return safe

    def save_design(self) -> bool:
        filename = self.current_file
        if filename and Path(filename).suffix.lower() == ".lds":
            filename = None
        if not filename:
            default_name = (Path(self.current_file).with_suffix(".jdesign").name
                            if self.current_file and Path(self.current_file).suffix.lower() == ".lds"
                            else "design.jdesign")
            filename, _ = QFileDialog.getSaveFileName(self, tr("儲存設計"), default_name,
                                                       tr("Verona 設計 (*.jdesign)"))
        if not filename:
            return False
        if not filename.endswith((".jdesign", ".json")):
            filename += ".jdesign"
        try:
            self._write_document(filename)
            return True
        except (OSError, TypeError, ValueError) as error:
            QMessageBox.critical(self, tr("儲存失敗"), str(error))
            return False

    def _write_document(self, filename, *, overwrite=True):
        data = {"format": "verona", "version": 1, "units": "mm",
                "items": self.items, "settings": self._cut_settings()}
        if self.metadata:
            data["metadata"] = self.metadata
        # Serialize before touching the target, then replace it in one step so a
        # failed save never leaves a truncated design behind.
        text = json.dumps(data, ensure_ascii=False, indent=2)
        target = Path(filename)
        if not overwrite:
            with target.open("x", encoding="utf-8") as output:
                output.write(text)
        else:
            handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as output:
                    output.write(text)
                if target.exists():
                    mode = target.stat().st_mode & 0o777
                else:
                    umask = os.umask(0)
                    os.umask(umask)
                    mode = 0o666 & ~umask
                os.chmod(temporary, mode)
                os.replace(temporary, target)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        self.current_file = filename
        self.file_label.setText(Path(filename).name)
        self.saved_snapshot = self._document_state()
        self._refresh_tab_titles()

    def _document_state(self) -> dict[str, Any]:
        return {"items": _copy_items(self.items), "settings": self._cut_settings(),
                "metadata": copy.deepcopy(self.metadata)}

    def is_dirty(self) -> bool:
        return self._document_state() != self.saved_snapshot

    def _confirm_unsaved_changes(self) -> str:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(tr("未儲存的變更"))
        dialog.setText(tr("這份設計有尚未儲存的變更。要先儲存嗎？"))
        save = dialog.addButton(tr("儲存"), QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton(tr("捨棄"), QMessageBox.ButtonRole.DestructiveRole)
        dialog.addButton(tr("取消"), QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(save)
        dialog.exec()
        clicked = dialog.clickedButton()
        if clicked is save:
            return "save"
        if clicked is not None and clicked.text() == tr("捨棄"):
            return "discard"
        return "cancel"

    def _maybe_save_changes(self) -> bool:
        if not self.is_dirty():
            return True
        choice = self._confirm_unsaved_changes()
        if choice == "discard":
            return True
        if choice == "save":
            return self.save_design()
        return False

    def closeEvent(self, event: Any) -> None:
        original = self._active_document
        for index in range(len(self._documents)):
            self.tab_bar.setCurrentIndex(index)
            if not self._maybe_save_changes():
                self.tab_bar.setCurrentIndex(original)
                event.ignore()
                return
        self._release_alt_grab()
        QApplication.instance().removeEventFilter(self)
        event.accept()

    def export_plt(self) -> None:
        if not self.items:
            QMessageBox.information(self, tr("沒有切割路徑"), tr("先匯入 SVG 或新增圖形，再匯出 PLT。"))
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr("匯出 PLT"), "design.plt", tr("PLT 檔案 (*.plt)"))
        if not filename:
            return
        if not filename.lower().endswith(".plt"):
            filename += ".plt"
        try:
            payload = self._encoded_plt()
            Path(filename).write_bytes(payload)
            count = sum(len(item["contours"]) for item in self.items if cuttable(item))
            self.status_label.setText(tr("已匯出 {name} · {count} 條輪廓", name=Path(filename).name, count=count))
        except Exception as error:
            QMessageBox.critical(self, tr("PLT 匯出失敗"), str(error))

    def print_design(self) -> None:
        from PySide6.QtCore import QSizeF
        from PySide6.QtGui import QPageLayout, QPageSize
        from PySide6.QtPrintSupport import QPrintDialog, QPrinter, QPrinterInfo
        from .printing import print_document, print_page_size
        try:
            calibration = self.metadata.get("source_format") == "calibration"
            size = print_page_size(self.items, self.metadata, calibration=calibration)
            printers = QPrinterInfo.availablePrinters()
            if not printers:
                QMessageBox.warning(self, tr("找不到印表機"), tr("請先在 Linux 設定印表機，並確認列印服務已啟動。"))
                return
            selected = next((info for info in printers if info.isDefault()), printers[0])
            printer = QPrinter(selected, QPrinter.PrinterMode.HighResolution)
            printer.setDocName(Path(self.current_file).stem if self.current_file else tr("未命名設計"))
            width, height = size.width(), size.height()
            printer.setPageSize(QPageSize(QSizeF(min(width, height), max(width, height)), QPageSize.Unit.Millimeter))
            printer.setPageOrientation(QPageLayout.Orientation.Landscape if width > height else QPageLayout.Orientation.Portrait)
            printer.setFullPage(True)
            printer.setDuplex(QPrinter.DuplexMode.DuplexNone)
            dialog = QPrintDialog(printer, self)
            dialog.setWindowTitle(tr("列印（100% 原尺寸）"))
            dialog.setOptions(QPrintDialog.PrintDialogOption.PrintShowPageSize
                              | QPrintDialog.PrintDialogOption.PrintCollateCopies)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            print_document(printer, self.items, self.metadata, calibration=calibration)
            self.status_label.setText(tr("已送至 {name} · 100% 原尺寸", name=printer.printerName()))
        except Exception as error:
            QMessageBox.critical(self, tr("列印失敗"), str(error))

    def export_print_pdf(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, tr("匯出列印 PDF"), "print.pdf", tr("PDF 檔案 (*.pdf)"))
        if not filename:
            return
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        try:
            from .printing import write_print_pdf
            write_print_pdf(filename, self.items, self.metadata,
                            calibration=self.metadata.get("source_format") == "calibration")
            self.status_label.setText(tr("已匯出 {name} · 列印時選擇 100% 原尺寸", name=Path(filename).name))
        except Exception as error:
            QMessageBox.critical(self, tr("列印 PDF 匯出失敗"), str(error))

    def _encoded_plt(self) -> bytes:
        return commands.encode_document(self.items, self.metadata, self._cut_settings())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verona offline design and cutting workspace")
    parser.add_argument("--lang", choices=("en", "zh_TW"))
    parser.add_argument("--open", dest="open_path")
    parser.add_argument("--host", help="Single cutter IPv4 address; overrides VERONA_HOST")
    parser.add_argument("--port", type=int, help="Cutter TCP port (default: 8080)")
    options, qt_args = parser.parse_known_args(argv)
    from .transport import _ipv4_endpoint
    host = options.host if options.host is not None else os.environ.get("VERONA_HOST") or None
    if host is not None or options.port is not None:
        try:
            _ipv4_endpoint(host if host is not None else "127.0.0.1",
                           options.port if options.port is not None else TCP_PORT)
        except ValueError as error:
            parser.error(str(error))
    settings = QSettings("Verona", "Workspace")
    set_language(options.lang or settings.value("language", os.environ.get("VERONA_LANGUAGE", "en")))
    app = QApplication.instance() or QApplication([sys.argv[0], *qt_args])
    set_language(get_language())
    app.setStyle("Fusion")
    app.setFont(QFont("Noto Sans", 10))
    window = VeronaWindow(host=host, port=options.port)
    from .editor_bridge import EditorBridge
    try:
        window.editor_bridge = EditorBridge(window)
        app.aboutToQuit.connect(window.editor_bridge.close)
    except (OSError, RuntimeError) as error:
        window.status_label.setText(tr("AI 協作連線無法啟動：{reason}", reason=str(error)))
    window.show()
    if options.open_path:
        window._load_document(options.open_path)
        if host is not None:
            window.wifi_host.setText(host)
        if options.port is not None:
            window.wifi_port.setValue(options.port)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

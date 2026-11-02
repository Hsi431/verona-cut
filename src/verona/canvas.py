"""Canvas widgets and page ruler painting."""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QContextMenuEvent, QFont, QPainter, QPainterPath,
    QPainterPathStroker, QPen, QTransform)
from PySide6.QtWidgets import QFrame, QScrollArea, QWidget

from .geometry import (copy_items as _copy_items, contour_path as _path_from_contours,
    transform_item as _transform_item)
from .grouping import expand_selection, remap_group_ids
from .i18n import tr
from .layers import editable, printable
from .view_constants import (ACCENT, DEFAULT_ZOOM, GRID, GRID_MAJOR, PAGE_HEIGHT_MM,
    PAGE_MARGIN, PAGE_WIDTH_MM, PAPER, RULER_SIZE, _MAX_CANVAS_EDGE_BYTES)


def _qoi_image(encoded: str):
    from .raster_import import decode_embedded_image
    return decode_embedded_image(encoded)


class CanvasRuler(QWidget):
    def __init__(self, scroll: "CanvasScrollArea", orientation: Qt.Orientation | None) -> None:
        super().__init__(scroll)
        self.scroll = scroll
        self.orientation = orientation

    def paintEvent(self, _event: Any) -> None:
        canvas = self.scroll.widget()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(canvas.owner.theme_colors["canvas"]))
        if self.orientation is None:
            return
        painter.setPen(QPen(QColor(canvas.owner.theme_colors["ruler"]), 1))
        font = QFont("Noto Sans")
        font.setPixelSize(11)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        label_step = 20 * max(1, math.ceil(
            (metrics.horizontalAdvance("300") + 8) / (20 * canvas.zoom)))
        horizontal = self.orientation == Qt.Orientation.Horizontal
        origin = canvas.canvas_origin() + QPointF(canvas.pos())
        start = origin.x() if horizontal else origin.y()
        length = self.width() if horizontal else self.height()
        if horizontal:
            painter.drawLine(0, RULER_SIZE - 1, self.width(), RULER_SIZE - 1)
        else:
            painter.drawLine(RULER_SIZE - 1, 0, RULER_SIZE - 1, self.height())
        for value in range(0, 301, 10):
            position = start + value * canvas.zoom
            if not 0 <= position < length:
                continue
            if horizontal:
                painter.drawLine(QPointF(position, RULER_SIZE - 6), QPointF(position, RULER_SIZE - 1))
            else:
                painter.drawLine(QPointF(RULER_SIZE - 6, position), QPointF(RULER_SIZE - 1, position))
            if value % label_step:
                continue
            text = str(value)
            bounds = QRectF(metrics.boundingRect(text))
            if horizontal:
                point = QPointF(position + 4, RULER_SIZE - 11)
                if self.rect().adjusted(1, 1, -1, -1).contains(bounds.translated(point).toAlignedRect()):
                    painter.drawText(point, text)
            else:
                painter.save()
                painter.translate(RULER_SIZE - 11, position + 3)
                painter.rotate(-90)
                if self.rect().adjusted(1, 1, -1, -1).contains(painter.transform().mapRect(bounds).toAlignedRect()):
                    painter.drawText(QPointF(0, 0), text)
                painter.restore()


class CanvasScrollArea(QScrollArea):
    """Keep rulers outside the scrolling canvas and align them to its origin."""

    def __init__(self, canvas: "DesignCanvas") -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.horizontal_ruler = CanvasRuler(self, Qt.Orientation.Horizontal)
        self.vertical_ruler = CanvasRuler(self, Qt.Orientation.Vertical)
        self.ruler_corner = CanvasRuler(self, None)
        self.setViewportMargins(RULER_SIZE, RULER_SIZE, 0, 0)
        self.setWidget(canvas)
        self.setWidgetResizable(True)
        self.viewport().setAcceptDrops(True)
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.valueChanged.connect(self._update_rulers)

    def _update_rulers(self, *_args: Any) -> None:
        self.horizontal_ruler.update()
        self.vertical_ruler.update()

    def viewportEvent(self, event) -> bool:
        result = super().viewportEvent(event)
        if event.type() == QEvent.Type.Resize:
            viewport = self.viewport().geometry()
            self.horizontal_ruler.setGeometry(viewport.x(), 0, viewport.width(), RULER_SIZE)
            self.vertical_ruler.setGeometry(0, viewport.y(), RULER_SIZE, viewport.height())
            self.ruler_corner.setGeometry(0, 0, RULER_SIZE, RULER_SIZE)
            self._update_rulers()
        return result

    def eventFilter(self, watched, event) -> bool:
        if watched is self.widget() and event.type() in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Paint):
            self._update_rulers()
        return super().eventFilter(watched, event)


class DesignCanvas(QWidget):
    selectionChanged = Signal(int)
    geometryChanged = Signal()

    def __init__(self, owner: "VeronaWindow", *, interactive: bool = True) -> None:
        super().__init__()
        self.owner = owner
        self.zoom = DEFAULT_ZOOM
        self._view_offset = QPointF()
        self._zoom_anchor = None
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.timeout.connect(self._restore_zoom_anchor)
        self._nudge_before = None
        self._nudge_key = None
        self._nudge_redo = None
        self._nudge_timer = QTimer(self)
        self._nudge_timer.setSingleShot(True)
        self._nudge_timer.timeout.connect(self.finish_nudge)
        self.interactive = interactive
        self.drag_start: QPointF | None = None
        self.drag_index = -1
        self.drag_before: dict[str, Any] | None = None
        self.drag_path: QPainterPath | None = None
        self._path_cache = {}
        self.drag_duplicate = False
        self.drag_moved = False
        self.drag_toggle_selection = None
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
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setToolTip(tr("右鍵開啟物件選單；Shift 拖曳鎖定水平／垂直，Alt 拖曳複製。\n"
                           "方向鍵移動選取物件 0.1 mm，Shift 為 1 mm；Ctrl／Alt 滾輪以游標為中心縮放，"
                           "Shift 滾輪水平捲動。\nCtrl+C／X／V 複製、剪下、貼上；Ctrl+Shift+V 原位貼上；"
                           "Backspace 刪除；Esc 取消選取。拖曳四角縮放、上方圓點旋轉；"
                           "Shift 旋轉每次 15°；Esc 取消拖曳。"))
        self._resize_to_page()

    def _resize_to_page(self) -> None:
        self.setMinimumSize(
            int(PAGE_WIDTH_MM * self.zoom + PAGE_MARGIN * 2),
            int(PAGE_HEIGHT_MM * self.zoom + PAGE_MARGIN * 2),
        )

    def set_zoom(self, value: float) -> None:
        self._zoom_timer.stop()
        self._zoom_anchor = None
        self.zoom = max(0.1, min(64.0, value))
        self._resize_to_page()
        self.update()

    def canvas_origin(self) -> QPointF:
        return QPointF(max(PAGE_MARGIN, (self.width() - PAGE_WIDTH_MM * self.zoom) / 2),
                       max(PAGE_MARGIN, (self.height() - PAGE_HEIGHT_MM * self.zoom) / 2)) + self._view_offset

    def _world_point(self, point: QPointF) -> QPointF:
        origin = self.canvas_origin()
        return QPointF((point.x() - origin.x()) / self.zoom,
                       (point.y() - origin.y()) / self.zoom)

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
        from .edge_bleed import rigid_move
        contours = item["contours"]
        width = item["bleed"].get("inner_mm", 0) if inner else item["bleed"]["width_mm"]
        key = (id(item), "inner_bleed" if inner else "bleed")
        cached = self._path_cache.get(key)
        if cached is not None and cached["contours"] is contours and cached["width"] == width:
            return cached["path"]
        # Dragging moves the item rigidly on every mouse event; map the ring
        # computed at the base position instead of offsetting the cut again.
        base = cached["base"] if cached is not None and cached["width"] == width else None
        move = rigid_move(base, item) if base else None
        if move is None:
            base = {"contours": contours, "clip": item.get("image_clip") or [], "image_data": item["image_data"],
                    "transform": tuple(item["image_transform"]),
                    "path": (inner_bleed_path if inner else bleed_path)(item)}
            path = base["path"]
        else:
            path = move.map(base["path"])
        self._path_cache[key] = {"contours": contours, "width": width, "path": path, "base": base}
        return path

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

    def _share_drag_paths(self) -> None:
        """The drag-start copies have the live items' geometry; reuse their paths."""
        for index in self.drag_before["selection"]:
            item, source = self.owner.items[index], self.drag_before["items"][index]
            for key in ("contours", "image_clip"):
                if key in item:
                    self._path_cache[(id(source), key)] = (source[key], self.item_path(item, key))

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
        origin = self.canvas_origin()
        painter.translate(origin)
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

    def item_at(self, point: QPointF, *, include_locked=False) -> int:
        for index in range(len(self.owner.items) - 1, -1, -1):
            item = self.owner.items[index]
            if (item.get("registration_guide", False) or not item.get("visible", True)
                    or (not include_locked and not editable(item))):
                continue
            path = self.item_path(item)
            if not path.controlPointRect().adjusted(-6, -6, 6, 6).contains(point):
                continue
            stroker = QPainterPathStroker()
            stroker.setWidth(3.0)
            if path.contains(point) or stroker.createStroke(path).contains(point):
                return index
        return -1

    def contextMenuEvent(self, event: Any) -> None:
        if (not self.interactive or self.drag_before is not None
                or self.marquee_start is not None or self.pan_start is not None):
            event.ignore()
            return
        if event.reason() == QContextMenuEvent.Reason.Mouse:
            hit = self.item_at(self._world_point(QPointF(event.pos())), include_locked=True)
            if hit >= 0 and hit not in self.owner.selected_indices():
                self.owner.select(hit)
        menu = self.owner.canvas_context_menu()
        try:
            menu.exec(event.globalPos())
        finally:
            menu.deleteLater()
        event.accept()

    def mousePressEvent(self, event: Any) -> None:
        if not self.interactive or event.button() != Qt.MouseButton.LeftButton:
            return
        self.finish_nudge()
        self.setFocus()
        self.drag_toggle_selection = None
        if self.space_pressed:
            self.pan_start = event.globalPosition() if hasattr(event, "globalPosition") else event.position()
            self.pan_offset_start = QPointF(self._view_offset)
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
            self._share_drag_paths()
            self.drag_duplicate = False
            self.drag_moved = False
            return
        hit = self.item_at(point)
        if hit < 0:
            self.marquee_start = self.marquee_end = point
            self.marquee_before = self.owner.selected_indices()
            additive = bool(modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
            self.marquee_base = self.owner.selected_indices() if additive else set()
            self.owner.select_many(self.marquee_base)
            self.update()
            return
        if (modifiers & Qt.KeyboardModifier.ControlModifier
                or (modifiers & Qt.KeyboardModifier.ShiftModifier and not modifiers & Qt.KeyboardModifier.AltModifier)):
            selected = self.owner.selected_indices()
            if hit in selected:
                if modifiers & Qt.KeyboardModifier.ControlModifier:
                    selected.difference_update(expand_selection(self.owner.items, [hit]))
                else:
                    # A Shift click toggles selection only if it does not become a drag.
                    self.drag_toggle_selection = hit
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
            self._share_drag_paths()
            self.drag_duplicate = bool(modifiers & Qt.KeyboardModifier.AltModifier)
            self.drag_moved = False
        self.update()

    def mouseMoveEvent(self, event: Any) -> None:
        if self.pan_start is not None:
            delta = (event.globalPosition() if hasattr(event, "globalPosition") else event.position()) - self.pan_start
            self.owner.canvas_scroll.horizontalScrollBar().setValue(self.pan_scroll[0] - round(delta.x()))
            self.owner.canvas_scroll.verticalScrollBar().setValue(self.pan_scroll[1] - round(delta.y()))
            self._view_offset = self.pan_offset_start + delta - QPointF(
                self.pan_scroll[0] - self.owner.canvas_scroll.horizontalScrollBar().value(),
                self.pan_scroll[1] - self.owner.canvas_scroll.verticalScrollBar().value())
            self.update()
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
        modifiers = event.modifiers() if hasattr(event, "modifiers") else Qt.KeyboardModifier.NoModifier
        if not self.drag_handle and modifiers & Qt.KeyboardModifier.ShiftModifier:
            delta = QPointF(delta.x(), 0) if abs(delta.x()) >= abs(delta.y()) else QPointF(0, delta.y())
        transform = QTransform.fromTranslate(delta.x(), delta.y())
        if delta.manhattanLength() < .01 and not self.drag_moved:
            return
        if self.drag_handle:
            bounds = self.drag_bounds
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
            item, source = self.owner.items[target], self.drag_before["items"][source_index]
            _transform_item(item, transform, source)
            if not source.get("node_paths"):
                # The new points are the affine image of the drag-start points;
                # map that cached path instead of rebuilding it point by point.
                for key in ("contours", "image_clip"):
                    if key in item:
                        self._path_cache[(id(item), key)] = (item[key], transform.map(self.item_path(source, key)))
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
            if self.drag_toggle_selection is not None and not self.drag_moved and event is not None:
                self.owner.select_many(self.owner.selected_indices() -
                    expand_selection(self.owner.items, [self.drag_toggle_selection]))
            elif self.owner._snapshot() != self.drag_before:
                self.owner.push_undo(self.drag_before)
        self.drag_toggle_selection = None
        self.drag_start = None
        self.drag_index = -1
        self.drag_before = None
        self.drag_path = None
        self.drag_handle = self.drag_bounds = None
        self.owner._refresh_tab_titles()

    def wheelEvent(self, event: Any) -> None:
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ShiftModifier and not modifiers & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            delta = event.pixelDelta().y() or event.angleDelta().y()
            if delta:
                bar = self.owner.canvas_scroll.horizontalScrollBar()
                bar.setValue(bar.value() - delta)
            event.accept()
            return
        if not modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
            event.ignore()
            return
        target = event.position() + QPointF(self.pos())
        if self._zoom_anchor is None:
            point = self._world_point(event.position())
        else:
            previous_point, previous_target = self._zoom_anchor
            point = previous_point + (target - previous_target) / self.zoom
        delta = event.angleDelta().y() or event.pixelDelta().y()
        self.owner.change_zoom(1.25 ** (delta / 120))
        self._zoom_anchor = point, target
        self._zoom_timer.start(0)
        event.accept()

    def _restore_zoom_anchor(self) -> None:
        if self._zoom_anchor is None:
            return
        point, target = self._zoom_anchor
        self._zoom_anchor = None
        scroll = self.owner.canvas_scroll
        origin = self.canvas_origin()
        scroll.horizontalScrollBar().setValue(round(origin.x() + point.x()*self.zoom - target.x()))
        scroll.verticalScrollBar().setValue(round(origin.y() + point.y()*self.zoom - target.y()))
        # When the page fits, scrollbars cannot compensate. Keep the remaining
        # translation in the view so zoom stays anchored even at the endpoints.
        self._view_offset += target - (self.canvas_origin() + point * self.zoom + QPointF(self.pos()))
        self.update()

    def keyPressEvent(self, event: Any) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
                self.finish_nudge()
                super().keyPressEvent(event)
                return
            nudge_key = (key, bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            if (self._nudge_before is not None
                    and (not event.isAutoRepeat() or self._nudge_key != nudge_key)):
                self.finish_nudge()
            selected = self.owner.selected_indices()
            if self.interactive and selected and not self.owner._selection_blocked():
                if self._nudge_before is None:
                    before = self.owner._snapshot()
                    self._nudge_redo = list(self.owner.redo_stack)
                    self.owner.push_undo(before)
                    self._nudge_before = before
                    self._nudge_key = nudge_key
                step = 1.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 0.1
                dx = step if key == Qt.Key.Key_Right else -step if key == Qt.Key.Key_Left else 0
                dy = step if key == Qt.Key.Key_Down else -step if key == Qt.Key.Key_Up else 0
                transform = QTransform.fromTranslate(dx, dy)
                for index in sorted(selected):
                    _transform_item(self.owner.items[index], transform)
                self.geometryChanged.emit()
                self.update()
                self._nudge_timer.start(450)
            event.accept()
        else:
            self.finish_nudge()
        if key == Qt.Key.Key_Escape and self.marquee_start is not None:
            before = self.marquee_before
            self.marquee_start = self.marquee_end = None
            self.owner.select_many(before)
            event.accept()
        elif key == Qt.Key.Key_Escape and self.drag_before is not None:
            self.owner._restore(self.drag_before)
            self.drag_before = self.drag_start = self.drag_handle = self.drag_bounds = None
            self.drag_toggle_selection = None
            self.drag_index = -1
            event.accept()
        elif key == Qt.Key.Key_Escape and self.interactive and self.pan_start is None:
            self.owner.select_many([])
            event.accept()
        elif key == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self.space_pressed = True
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
        elif key not in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: Any) -> None:
        if (event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down)
                and not event.isAutoRepeat() and self._nudge_key is not None
                and event.key() == self._nudge_key[0]):
            self.finish_nudge()
        if event.key() == Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self.space_pressed = False
                self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event: Any) -> None:
        self.finish_nudge()
        self.mouseReleaseEvent(None)
        self.space_pressed = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().focusOutEvent(event)

    def finish_nudge(self) -> None:
        before = self._nudge_before
        if before is None:
            return
        self._nudge_timer.stop()
        self._nudge_before = None
        self._nudge_key = None
        if self.owner._snapshot() == before:
            if self.owner.undo_stack and self.owner.undo_stack[-1] == before:
                self.owner.undo_stack.pop()
            self.owner.redo_stack = self._nudge_redo or []
            self.owner._refresh_history_actions()
        self._nudge_redo = None
        self.owner._refresh_tab_titles()

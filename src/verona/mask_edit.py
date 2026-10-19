"""Reopen and retrace saved image masks without changing original pixels or affine."""
import copy
import math

import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialogButtonBox, QHBoxLayout, QLabel, QWidget

from .artwork import contour_path, image_path
from .i18n import tr
from .editor_widgets import SafeDoubleSpinBox
from .raster_import import (ImportImageDialog, TRACE_KEYS, _offset_path, _png_base64, _polygon_area,
                            _rgba_array, decode_embedded_image, raster_options, trace_paths)


def validate_trace_options(image, changes):
    if not isinstance(changes, dict) or set(changes) - TRACE_KEYS:
        raise ValueError("Unknown saved-mask tracing options.")
    mode = changes.get("mask_mode", "saved")
    if mode not in ("saved", "alpha", "background", "colors"):
        raise ValueError("Choose saved, alpha, background or colors mask mode.")
    options = raster_options(image, {**changes, "mask_mode": "alpha" if mode == "saved" else mode,
                                    "width_mm": 100, "height_mm": 100})
    return {key: mode if key == "mask_mode" else options[key] for key in TRACE_KEYS}


def image_transform(item):
    tx, ty, a, b, c, d = item["image_transform"]
    transform = QTransform(a, b, c, d, tx, ty)
    if not transform.isInvertible():
        raise ValueError("The image transform is singular and cannot be edited as a mask.")
    return transform


def working_mask(item, image):
    factor = min(1., 1200 / max(image.width(), image.height()))
    width, height = max(1, round(image.width()*factor)), max(1, round(image.height()*factor))
    if "trace_mask" in item:
        mask = decode_embedded_image(item["trace_mask"])
        if mask.width() != width or mask.height() != height:
            raise ValueError("The saved working mask dimensions do not match its image.")
        return _rgba_array(mask)[:, :, 0] > 127
    raster = QImage(width, height, QImage.Format.Format_RGB32)
    raster.fill(Qt.GlobalColor.black)
    painter = QPainter(raster)
    try:
        inverse, _ = image_transform(item).inverted()
        path = inverse.map(image_path(item))
        painter.scale(width/image.width(), height/image.height())
        painter.fillPath(path, Qt.GlobalColor.white)
    finally:
        painter.end()
    return _rgba_array(raster)[:, :, 0] > 127


def mask_paths(item, image, options, mask=None):
    # Trace source pixels first, then offset/filter in actual document mm so
    # rotations, mirrors and shear do not change the meaning of the distance.
    tracing = raster_options(image, {**options, "mask_mode": "alpha" if options["mask_mode"] == "saved"
        else options["mask_mode"], "width_mm": 100, "height_mm": 100,
        "offset_mm": 0, "minimum_area_mm2": 0})
    if options["mask_mode"] == "saved" and mask is None:
        mask = working_mask(item, image)
        if options["inverted"] != item.get("trace_options", {}).get("inverted", False):
            mask = ~mask
    mask, raw, _, _ = trace_paths(image, tracing, mask_override=mask)
    raw = image_transform(item).map(raw)
    if options["minimum_area_mm2"]:
        filtered = QPainterPath()
        filtered.setFillRule(Qt.FillRule.WindingFill)
        for polygon in raw.toSubpathPolygons():
            if abs(_polygon_area(polygon)) >= options["minimum_area_mm2"]:
                filtered.addPolygon(polygon)
                filtered.closeSubpath()
        raw = filtered.simplified()
    return mask, raw, _offset_path(raw, options["offset_mm"])


def mask_image(mask):
    height, width = mask.shape
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[:, :, :3] = mask[:, :, None] * 255
    rgba[:, :, 3] = 255
    return QImage(rgba.data, width, height, width*4, QImage.Format.Format_RGBA8888).copy()


def paint_mask(item, image, mask, strokes):
    """Paint bounded round brush strokes in document mm onto a detached binary mask."""
    from .editor_actions import finite_number
    if not isinstance(strokes, list) or not 1 <= len(strokes) <= 100:
        raise ValueError("Provide 1–100 brush strokes.")
    if sum(len(stroke["points_mm"]) for stroke in strokes if isinstance(stroke, dict)
           and isinstance(stroke.get("points_mm"), list)) > 1000:
        raise ValueError("Brush strokes are limited to 1000 points per edit.")
    target = mask_image(mask)
    inverse, _ = image_transform(item).inverted()
    sx, sy = target.width()/image.width(), target.height()/image.height()
    painter = QPainter(target)
    painter.setTransform(QTransform(inverse.m11()*sx, inverse.m12()*sy,
        inverse.m21()*sx, inverse.m22()*sy, inverse.dx()*sx, inverse.dy()*sy))
    try:
        for stroke in strokes:
            if not isinstance(stroke, dict) or set(stroke) != {"mode", "width_mm", "points_mm"}:
                raise ValueError("A stroke needs mode, width_mm and points_mm.")
            if stroke["mode"] not in ("include", "remove"):
                raise ValueError("Brush mode must be include or remove.")
            width = finite_number(stroke["width_mm"], "width_mm", .1, 20)
            points = stroke["points_mm"]
            if not isinstance(points, list) or not points:
                raise ValueError("Choose at least one stroke point in document mm.")
            coordinates = []
            for point in points:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError("Each stroke point must be [x,y] in document mm.")
                coordinates.append(QPointF(*(finite_number(value, "coordinate", -10000, 10000) for value in point)))
            color = Qt.GlobalColor.white if stroke["mode"] == "include" else Qt.GlobalColor.black
            if len(coordinates) == 1:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawEllipse(coordinates[0], width/2, width/2)
            else:
                path = QPainterPath(coordinates[0])
                for point in coordinates[1:]:
                    path.lineTo(point)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(color, width, Qt.PenStyle.SolidLine,
                                    Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                painter.drawPath(path)
    finally:
        painter.end()
    return _rgba_array(target)[:, :, 0] > 127


def retrace_item(item, changes, mask=None):
    from .ui import _flatten
    if "image_data" not in item or item.get("registration_guide"):
        raise ValueError("Choose an editable image object.")
    if not isinstance(changes, dict):
        raise ValueError("Tracing changes must be an options object.")
    changes = dict(changes)
    strokes = changes.pop("brush_strokes", None)
    image = decode_embedded_image(item["image_data"])
    options = validate_trace_options(image, {**item.get("trace_options", {}), **changes})
    baked_bleed = item.get("raster_bleed_mm", 0)
    if baked_bleed and options["mask_mode"] != "saved":
        raise ValueError("This image has its import bleed in the pixels; edit the saved mask instead "
                         "of retracing alpha, background or colors.")
    mask, raw, cut = mask_paths(item, image, options, mask)
    if strokes is not None:
        mask = paint_mask(item, image, mask, strokes)
        options["mask_mode"] = "saved"
        mask, raw, cut = mask_paths(item, image, options, mask)
    contours = _flatten(cut)
    if not contours:
        raise ValueError("The edited mask has no usable contours.")
    result = copy.deepcopy(item)
    result["contours"] = contours
    result.pop("node_paths", None)
    if options["clip"]:
        if baked_bleed:
            # Rebuild in the import's physical coordinates before applying the
            # current affine, so baked pixels and their clip scale together.
            if "raster_bleed_scale" in item:
                sx, sy = item["raster_bleed_scale"]
                transform = image_transform(item)
                inverse, _ = transform.inverted()
                original = QTransform.fromScale(sx, sy).map(inverse.map(raw))
                expanded = _offset_path(original, baked_bleed).simplified()
                result["image_clip"] = _flatten(transform.map(QTransform.fromScale(1/sx, 1/sy).map(expanded)))
            # Older files lack the import scale; keep their existing print clip.
        else:
            result["image_clip"] = _flatten(raw)
    else:
        result.pop("image_clip", None)
    result["trace_options"] = options
    result["trace_mask"] = _png_base64(mask_image(mask))
    return result


class EditImageDialog(ImportImageDialog):
    """Reuse tracing controls while keeping the saved image's position and pixels."""
    def __init__(self, item, parent=None):
        image = decode_embedded_image(item["image_data"])
        super().__init__(image, item["name"], parent)
        self.setWindowTitle(tr("編輯圖片遮罩"))
        self.editing_item = copy.deepcopy(item)
        self.manual_mask = None
        self.mask_serial = 0
        controls = self.findChildren(QWidget)
        for control in controls:
            control.blockSignals(True)
        self.mask_mode_combo.addItem(tr("已存遮罩"), "saved")
        options = validate_trace_options(image, item.get("trace_options", {}))
        self.mask_mode_combo.setCurrentIndex(self.mask_mode_combo.findData("saved"))
        if item.get("raster_bleed_mm"):
            for row in range(self.mask_mode_combo.count()):
                if self.mask_mode_combo.itemData(row) != "saved":
                    self.mask_mode_combo.model().item(row).setEnabled(False)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData("print_cut"))
        self.background_color = QColor(options["background"])
        self.foreground_colors = options["colors"][:]
        for control, key in ((self.tolerance_spin, "tolerance"), (self.smoothing_spin, "smoothing"),
                (self.minimum_area, "minimum_area_mm2"), (self.offset_spin, "offset_mm")):
            control.setValue(options[key])
        for control, key in ((self.clip_check, "clip"), (self.invert_check, "inverted"),
                             (self.holes_check, "keep_holes")):
            control.setChecked(options[key])
        self.bleed_spin.setValue(0)
        transform = image_transform(item)
        width = math.hypot(transform.m11(), transform.m12())*image.width()
        height = math.hypot(transform.m21(), transform.m22())*image.height()
        self.width_spin.setMaximum(max(300, width))
        self.height_spin.setMaximum(max(300, height))
        self.width_spin.setValue(width)
        self.height_spin.setValue(height)
        self.dpi_spin.setValue(image.width()*25.4/width)
        self.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).setText(tr("套用"))
        for control in controls:
            control.blockSignals(False)
        self._update_controls()
        self.initial_options = self.trace_options()
        self.brush_check = QCheckBox(tr("手繪遮罩"))
        self.brush_mode = QComboBox()
        self.brush_mode.addItem(tr("補回遮罩"), "include")
        self.brush_mode.addItem(tr("擦除遮罩"), "remove")
        self.brush_width = SafeDoubleSpinBox()
        self.brush_width.setRange(.1, 20)
        self.brush_width.setValue(1)
        self.brush_width.setSuffix(" mm")
        row = QHBoxLayout()
        row.addWidget(self.brush_check)
        row.addWidget(self.brush_mode)
        row.addWidget(QLabel(tr("筆刷直徑")))
        row.addWidget(self.brush_width)
        row.addStretch(1)
        self.layout().insertLayout(1, row)
        self.preview_label.paint_callback = self._paint_preview
        self.mask_mode_combo.currentIndexChanged.connect(self._mask_source_changed)
        self.update_preview()

    def _update_controls(self, *_args):
        super()._update_controls()
        if hasattr(self, "editing_item"):
            for control in (self.width_spin, self.height_spin, self.dpi_spin, self.keep_ratio_check,
                            self.mode_combo, self.bleed_spin, self.bleed_color_button):
                control.setEnabled(False)

    def _mask_source_changed(self):
        if self.mask_mode_combo.currentData() != "saved":
            self.manual_mask = None
            self.mask_serial += 1
            self.update_preview()

    def _paint_preview(self, point, previous):
        if not self.brush_check.isChecked():
            return False
        pixmap = self.preview_label.pixmap()
        offset = QPointF((self.preview_label.width()-pixmap.width())/2,
                         (self.preview_label.height()-pixmap.height())/2)
        transform = image_transform(self.editing_item)
        points = [point] if previous is None else [previous, point]
        world = [transform.map(self.preview_to_source.map(value-offset)) for value in points]
        mask = self._mask_and_paths()[0]
        self.manual_mask = paint_mask(self.editing_item, self.image, mask, [{
            "mode": self.brush_mode.currentData(), "width_mm": self.brush_width.value(),
            "points_mm": [[value.x(), value.y()] for value in world]}])
        self.manual_inverted = self.invert_check.isChecked()
        self.mask_mode_combo.blockSignals(True)
        self.mask_mode_combo.setCurrentIndex(self.mask_mode_combo.findData("saved"))
        self.mask_mode_combo.blockSignals(False)
        self.mask_serial += 1
        self._update_controls()
        return True

    def _mask_and_paths(self, image=None):
        if not hasattr(self, "editing_item"):
            return super()._mask_and_paths(image)
        options = {key: self.trace_options()[key] for key in TRACE_KEYS}
        key = (tuple(sorted(options.items())), hasattr(self, "initial_options")
               and self.trace_options() == self.initial_options, self.mask_serial)
        if getattr(self, "_edit_cache", (None, None))[0] == key:
            return self._edit_cache[1]
        inverse, _ = image_transform(self.editing_item).inverted()
        if hasattr(self, "initial_options") and self.trace_options() == self.initial_options and self.manual_mask is None:
            raw, cut = image_path(self.editing_item), contour_path(self.editing_item["contours"])
            mask = working_mask(self.editing_item, self.image)
        else:
            mask = self.manual_mask
            if mask is not None and options["inverted"] != self.manual_inverted:
                mask = ~mask
            mask, raw, cut = mask_paths(self.editing_item, self.image, options, mask)
        result = mask, inverse.map(raw), inverse.map(cut), inverse.map(raw)
        self._edit_cache = key, result
        return result

    def build_items(self):
        if self.trace_options() == self.initial_options and self.manual_mask is None:
            return [copy.deepcopy(self.editing_item)]
        changes = {key: self.trace_options()[key] for key in TRACE_KEYS}
        mask = self.manual_mask
        if mask is not None and changes["inverted"] != self.manual_inverted:
            mask = ~mask
        return [retrace_item(self.editing_item, changes, mask)]

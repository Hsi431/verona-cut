"""Raster image tracing and import options for the editor."""

from __future__ import annotations

import base64
from collections import OrderedDict
import math
from typing import Any

import numpy as np
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QBitmap, QColor, QImage, QImageReader, QPainter, QPainterPath, QPainterPathStroker,
    QPen, QPixmap, QRegion, QTransform,
)
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget,
)

from .editor_widgets import SafeDoubleSpinBox
from .i18n import tr
from .native_images import decode_qoi


_MAX_PIXELS = 16_000_000
_MAX_CACHE_BYTES = 512 * 1024 * 1024
_DECODED_IMAGES = OrderedDict()
TRACE_KEYS = {"mask_mode", "background", "tolerance", "inverted", "keep_holes",
              "smoothing", "minimum_area_mm2", "offset_mm", "clip", "colors"}


class ImageSizeSpinBox(SafeDoubleSpinBox):
    def textFromValue(self, value: float) -> str:
        return self.locale().toString(value, "f", self.decimals()).rstrip("0").rstrip(self.locale().decimalPoint())


class MaskPreviewLabel(QLabel):
    paint_callback = None
    last_point = None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.paint_callback:
            self.last_point = event.position()
            if self.paint_callback(self.last_point, None):
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.paint_callback and self.last_point is not None and event.buttons() & Qt.MouseButton.LeftButton:
            previous, self.last_point = self.last_point, event.position()
            if self.paint_callback(self.last_point, previous):
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.last_point = None
        super().mouseReleaseEvent(event)


def decode_embedded_image(encoded: str) -> QImage:
    """Decode a base64 QOI or PNG image with a bounded pixel count."""
    if not isinstance(encoded, str):
        raise ValueError(tr("圖片資料必須是 base64 文字"))
    # QImage's implicitly shared copy detaches if a caller paints into it.
    return QImage(_decoded_image(encoded))


def _decoded_image(encoded):
    cached = _DECODED_IMAGES.get(encoded)
    if cached is not None:
        _DECODED_IMAGES.move_to_end(encoded)
        return cached
    image = _decode_image(encoded)
    size = image.sizeInBytes()
    if size <= _MAX_CACHE_BYTES:
        used = sum(value.sizeInBytes() for value in _DECODED_IMAGES.values())
        while _DECODED_IMAGES and (len(_DECODED_IMAGES) >= 32 or used + size > _MAX_CACHE_BYTES):
            _, old = _DECODED_IMAGES.popitem(last=False)
            used -= old.sizeInBytes()
        _DECODED_IMAGES[encoded] = image
    return image


def _decode_image(encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise ValueError(tr("圖片 base64 格式無效")) from error
    if raw.startswith(b"qoif"):
        width, height, rgba = decode_qoi(raw)
        image = QImage(rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
    else:
        data = QByteArray(raw)
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer, b"PNG")
        size = reader.size()
        if size.isValid() and size.width() * size.height() > _MAX_PIXELS:
            raise ValueError(tr("圖片尺寸超過 1600 萬像素"))
        image = reader.read()
        if image.isNull():
            raise ValueError(tr("圖片格式無效；目前支援 PNG 與 QOI"))
    if image.width() <= 0 or image.height() <= 0 or image.width() * image.height() > _MAX_PIXELS:
        raise ValueError(tr("圖片尺寸超過 1600 萬像素"))
    return image.convertToFormat(QImage.Format.Format_RGBA8888)


def _rgba_array(image: QImage) -> np.ndarray:
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    raw = np.frombuffer(rgba.constBits(), dtype=np.uint8,
                        count=rgba.sizeInBytes()).reshape(rgba.height(), rgba.bytesPerLine())
    return raw[:, :rgba.width() * 4].reshape(rgba.height(), rgba.width(), 4).copy()


def _trace_mask(image: QImage, background: QColor, tolerance: int,
                alpha_mask: bool, inverted: bool, foreground_colors=None) -> np.ndarray:
    rgba = _rgba_array(image)
    alpha = rgba[:, :, 3] > 0
    if foreground_colors is not None:
        mask = np.zeros(alpha.shape, dtype=bool)
        rgb = rgba[:, :, :3].astype(np.int16)
        for value in foreground_colors:
            color = QColor(value)
            target = np.array([color.red(), color.green(), color.blue()], dtype=np.int16)
            mask |= np.max(np.abs(rgb-target), axis=2) <= tolerance
        mask &= alpha
    elif alpha_mask:
        mask = alpha
    else:
        color = np.array([background.red(), background.green(), background.blue()], dtype=np.int16)
        delta = rgba[:, :, :3].astype(np.int16) - color
        mask = alpha & (np.max(np.abs(delta), axis=2) > tolerance)
    return ~mask if inverted else mask


def _raster_path(mask: np.ndarray, source_width: int, source_height: int) -> QPainterPath:
    height, width = mask.shape
    # QBitmap treats black pixels as the region. A mono conversion thresholds the mask.
    gray = np.where(mask, 0, 255).astype(np.uint8)
    gray_image = QImage(gray.data, width, height, width, QImage.Format.Format_Grayscale8).copy()
    bitmap = QBitmap.fromImage(gray_image.convertToFormat(QImage.Format.Format_Mono))
    region = QRegion(bitmap)
    path = QPainterPath()
    path.addRegion(region)
    path = path.simplified()
    transform_scale_x = source_width / width
    transform_scale_y = source_height / height
    path = path * QTransform.fromScale(transform_scale_x, transform_scale_y)
    return path.simplified()


def _polygon_area(points) -> float:
    return sum(points[i].x() * points[(i + 1) % len(points)].y()
               - points[(i + 1) % len(points)].x() * points[i].y()
               for i in range(len(points))) / 2


def _without_holes(path: QPainterPath) -> QPainterPath:
    polygons = path.toSubpathPolygons()
    if len(polygons) < 2:
        return path
    areas = [_polygon_area(polygon) for polygon in polygons]
    direction = 1 if areas[max(range(len(areas)), key=lambda i: abs(areas[i]))] >= 0 else -1
    result = QPainterPath()
    result.setFillRule(Qt.FillRule.WindingFill)
    for polygon, area in zip(polygons, areas):
        if area * direction > 0:
            result.addPolygon(polygon)
            result.closeSubpath()
    return result.simplified()


def _simplify_path(path: QPainterPath, amount: int) -> QPainterPath:
    if amount <= 0:
        return path
    epsilon = amount / 100 * 1.5
    result = QPainterPath()
    result.setFillRule(Qt.FillRule.OddEvenFill)
    for polygon in path.toSubpathPolygons():
        points = list(polygon)
        if len(points) <= 4:
            result.addPolygon(polygon)
            result.closeSubpath()
            continue
        closed = points[0] == points[-1]
        if closed:
            points.pop()
        keep = {0, len(points) - 1}
        stack = [(0, len(points) - 1)]
        while stack:
            start, end = stack.pop()
            a, b = points[start], points[end]
            dx, dy = b.x() - a.x(), b.y() - a.y()
            denominator = dx * dx + dy * dy
            best_distance, best_index = 0.0, -1
            for index in range(start + 1, end):
                point = points[index]
                if denominator:
                    t = max(0.0, min(1.0, ((point.x() - a.x()) * dx + (point.y() - a.y()) * dy)
                                          / denominator))
                    distance = math.hypot(point.x() - (a.x() + t * dx),
                                          point.y() - (a.y() + t * dy))
                else:
                    distance = math.hypot(point.x() - a.x(), point.y() - a.y())
                if distance > best_distance:
                    best_distance, best_index = distance, index
            if best_distance > epsilon:
                keep.add(best_index)
                stack.extend(((start, best_index), (best_index, end)))
        simplified = [points[index] for index in sorted(keep)]
        if len(simplified) >= 3:
            result.addPolygon(simplified)
            result.closeSubpath()
    return result.simplified()


def _offset_path(path: QPainterPath, amount: float) -> QPainterPath:
    if abs(amount) < 1e-9:
        return path
    stroker = QPainterPathStroker()
    stroker.setWidth(abs(amount) * 2)
    stroke = stroker.createStroke(path)
    return (path.united(stroke) if amount > 0 else path.subtracted(stroke)).simplified()


def _path_contours(path: QPainterPath, width_mm: float, height_mm: float,
                   image_width: int, image_height: int) -> list[list[tuple[float, float]]]:
    scale_x, scale_y = width_mm / image_width, height_mm / image_height
    tx, ty = (300.0 - width_mm) / 2, (300.0 - height_mm) / 2
    contours = []
    for polygon in path.toSubpathPolygons():
        points = [(tx + point.x() * scale_x, ty + point.y() * scale_y) for point in polygon]
        if len(points) >= 3:
            if points[0] != points[-1]:
                points.append(points[0])
            contours.append(points)
    return contours


def _png_base64(image: QImage) -> str:
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
        raise ValueError(tr("無法編碼 PNG 圖片"))
    buffer.close()
    return base64.b64encode(bytes(data)).decode("ascii")


def raster_options(image, changes):
    """Validate explicit tracing options without creating a dialog."""
    if image.isNull() or image.width() * image.height() > _MAX_PIXELS:
        raise ValueError(tr("圖片無效或尺寸超過 1600 萬像素"))
    dpi = image.dotsPerMeterX() * .0254 if image.dotsPerMeterX() else 96
    width = min(150., image.width()*25.4/dpi, 300.*image.width()/image.height())
    options = {"mode": "print_cut", "mask_mode": "alpha" if np.any(_rgba_array(image)[:, :, 3] < 255)
               else "background", "background": "#ffffff", "tolerance": 20,
               "inverted": False, "keep_holes": True, "smoothing": 0, "colors": [],
               "minimum_area_mm2": 0., "offset_mm": 0., "width_mm": width,
               "height_mm": width*image.height()/image.width(), "clip": True,
               "bleed_mm": 0., "bleed_color": "#ffffff"}
    if not isinstance(changes, dict) or set(changes) - set(options):
        raise ValueError("Unknown tracing options.")
    options.update(changes)
    if "width_mm" in changes and "height_mm" not in changes:
        if isinstance(options["width_mm"], (int, float)):
            options["height_mm"] = options["width_mm"] * image.height()/image.width()
    elif "height_mm" in changes and "width_mm" not in changes:
        if isinstance(options["height_mm"], (int, float)):
            options["width_mm"] = options["height_mm"] * image.width()/image.height()
    if options["mode"] not in ("print_cut", "cut_only", "background") or options["mask_mode"] not in ("alpha", "background", "colors"):
        raise ValueError("Choose a supported image mode and mask source.")
    for name in ("inverted", "keep_holes", "clip"):
        if type(options[name]) is not bool:
            raise ValueError(f"{name} must be a boolean.")
    for name, low, high in (("width_mm", .000001, 300), ("height_mm", .000001, 300),
            ("minimum_area_mm2", 0, 1000), ("offset_mm", -5, 5), ("bleed_mm", 0, 5),
            ("tolerance", 0, 255), ("smoothing", 0, 100)):
        value = options[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} must be finite and between {low} and {high}.")
        if name in ("tolerance", "smoothing") and type(value) is not int:
            raise ValueError(f"{name} must be an integer.")
    for name in ("background", "bleed_color"):
        color = options[name]
        if not isinstance(color, str) or len(color) != 7 or not color.startswith("#") or not QColor(color).isValid():
            raise ValueError(f"{name} must be #RRGGBB.")
    colors = options["colors"]
    if (not isinstance(colors, list) or len(colors) > 16
            or any(not isinstance(color, str) or len(color) != 7 or not color.startswith("#")
                   or not QColor(color).isValid() for color in colors)
            or options["mask_mode"] == "colors" and not colors):
        raise ValueError("Color selection requires 1–16 #RRGGBB foreground colors.")
    return options


def trace_paths(image, options, mask_override=None):
    """Return the shared mask, print clip, cut path and bleed path."""
    max_edge = 1200
    factor = min(1.0, max_edge / max(image.width(), image.height()))
    width = max(1, round(image.width() * factor))
    height = max(1, round(image.height() * factor))
    trace_image = image if (width, height) == (image.width(), image.height()) else image.scaled(
        width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    mask = (mask_override.copy() if mask_override is not None else
            _trace_mask(trace_image, QColor(options["background"]), int(options["tolerance"]),
                        options["mask_mode"] == "alpha", options["inverted"],
                        options["colors"] if options["mask_mode"] == "colors" else None))
    path = _raster_path(mask, image.width(), image.height())
    path = _simplify_path(path, int(options["smoothing"]))
    if not options["keep_holes"]:
        path = _without_holes(path)
    to_mm = QTransform.fromScale(options["width_mm"] / image.width(),
                                 options["height_mm"] / image.height())
    from_mm = QTransform.fromScale(image.width() / options["width_mm"],
                                   image.height() / options["height_mm"])
    path_mm = path * to_mm
    if options["minimum_area_mm2"] > 0:
        filtered = QPainterPath()
        filtered.setFillRule(Qt.FillRule.WindingFill)
        for polygon in path_mm.toSubpathPolygons():
            if abs(_polygon_area(polygon)) >= options["minimum_area_mm2"]:
                filtered.addPolygon(polygon)
                filtered.closeSubpath()
        path_mm = filtered.simplified()
        path = path_mm * from_mm
    cut_path = _offset_path(path_mm, options["offset_mm"]) * from_mm
    bleed_path = _offset_path(path_mm, options["bleed_mm"]) * from_mm
    result = mask, path, cut_path.simplified(), bleed_path.simplified()
    return result


def build_raster_items(source_image, name, options, paths=None):
    """Build raster artwork using the same paths as the import dialog."""
    _, raw_path, cut_path, bleed_path = paths if paths is not None else trace_paths(source_image, options)
    mode = options["mode"]
    if mode == "background":
        cut_path = QPainterPath()
        cut_path.addRect(0, 0, source_image.width(), source_image.height())
    contours = _path_contours(cut_path, options["width_mm"], options["height_mm"],
                              source_image.width(), source_image.height())
    if not contours:
        return []
    scale_x = options["width_mm"] / source_image.width()
    scale_y = options["height_mm"] / source_image.height()
    height_mm = options["height_mm"]
    tx, ty = (300 - options["width_mm"]) / 2, (300 - height_mm) / 2
    item = {"name": name, "contours": contours, "cut": mode != "background"}
    if mode == "cut_only":
        item["print"] = False
    image = source_image
    margin_x = margin_y = 0
    clip_path = raw_path
    if mode == "print_cut" and options["bleed_mm"] > 0:
        margin_x = math.ceil(options["bleed_mm"] / scale_x)
        margin_y = math.ceil(options["bleed_mm"] / scale_y)
        if (source_image.width() + margin_x * 2) * (source_image.height() + margin_y * 2) > _MAX_PIXELS:
            raise ValueError(tr("出血後的圖片超過 1600 萬像素，請縮小出血或增加圖片尺寸。"))
        image = QImage(source_image.width() + margin_x * 2,
                       source_image.height() + margin_y * 2,
                       QImage.Format.Format_RGBA8888)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(margin_x, margin_y)
        painter.fillPath(bleed_path, QColor(options["bleed_color"]))
        if options["clip"]:
            painter.setClipPath(raw_path)
        painter.drawImage(0, 0, source_image)
        painter.end()
        clip_path = bleed_path
    item["image_transform"] = [tx - margin_x * scale_x, ty - margin_y * scale_y,
                               scale_x, 0.0, 0.0, scale_y]
    item["image_data"] = _png_base64(image)
    item["trace_options"] = {key: options[key] for key in TRACE_KEYS}
    if margin_x or margin_y:
        # The bleed ring is now part of the pixels; later retracing must
        # start from the pre-bleed outline instead of the padded image.
        factor = min(1.0, 1200 / max(image.width(), image.height()))
        mask = QImage(max(1, round(image.width() * factor)), max(1, round(image.height() * factor)),
                      QImage.Format.Format_RGB32)
        mask.fill(Qt.GlobalColor.black)
        painter = QPainter(mask)
        painter.scale(mask.width() / image.width(), mask.height() / image.height())
        painter.translate(margin_x, margin_y)
        painter.fillPath(raw_path, Qt.GlobalColor.white)
        painter.end()
        item["trace_mask"] = _png_base64(mask)
        item["trace_options"]["mask_mode"] = "saved"
        item["raster_bleed_mm"] = float(options["bleed_mm"])
        item["raster_bleed_scale"] = [scale_x, scale_y]
    if options["clip"] and mode in ("print_cut", "cut_only"):
        item["image_clip"] = _path_contours(
            clip_path, options["width_mm"], options["height_mm"],
            source_image.width(), source_image.height())
    return [item]


class ImportImageDialog(QDialog):
    """Interactive mask and contour preview for importing one raster image."""

    def __init__(self, image: QImage, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if image.isNull() or image.width() * image.height() > _MAX_PIXELS:
            raise ValueError(tr("圖片無效或尺寸超過 1600 萬像素"))
        self.image = image.convertToFormat(QImage.Format.Format_RGBA8888)
        self.image_name = name
        self.background_color = QColor(Qt.GlobalColor.white)
        self.foreground_colors = []
        self.setWindowTitle(tr("匯入點陣圖片"))

        source_alpha = _rgba_array(self.image)[:, :, 3]
        self.preview_label = MaskPreviewLabel()
        self.preview_label.setMinimumSize(500, 350)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mode_combo = QComboBox()
        for label, mode in (("列印＋切割", "print_cut"), ("僅列印", "background"),
                            ("僅切割", "cut_only")):
            self.mode_combo.addItem(tr(label), mode)
        self.mask_mode_combo = QComboBox()
        self.mask_mode_combo.addItem(tr("透明度通道"), "alpha")
        self.mask_mode_combo.addItem(tr("背景顏色"), "background")
        self.mask_mode_combo.addItem(tr("指定多色"), "colors")
        self.mask_mode_combo.setCurrentIndex(
            self.mask_mode_combo.findData("alpha" if np.any(source_alpha < 255) else "background"))
        self.background_button = QPushButton(tr("背景顏色…"))
        self.background_button.clicked.connect(self._choose_background)
        self.add_color_button = QPushButton(tr("新增選取色…"))
        self.add_color_button.clicked.connect(self._choose_foreground_color)
        self.clear_colors_button = QPushButton(tr("清除選取色"))
        self.clear_colors_button.clicked.connect(self._clear_foreground_colors)
        self.color_summary = QLabel()
        self.tolerance_spin = SafeDoubleSpinBox()
        self.tolerance_spin.setRange(0, 255)
        self.tolerance_spin.setDecimals(0)
        self.tolerance_spin.setValue(20)
        self.invert_check = QCheckBox(tr("反轉遮罩"))
        self.holes_check = QCheckBox(tr("保留內孔"))
        self.holes_check.setChecked(True)
        self.smoothing_spin = SafeDoubleSpinBox()
        self.smoothing_spin.setRange(0, 100)
        self.smoothing_spin.setDecimals(0)
        self.minimum_area = SafeDoubleSpinBox()
        self.minimum_area.setRange(0, 1000)
        self.minimum_area.setDecimals(3)
        self.minimum_area.setSuffix(" mm²")
        self.offset_spin = SafeDoubleSpinBox()
        self.offset_spin.setRange(-5, 5)
        self.offset_spin.setDecimals(2)
        self.offset_spin.setSuffix(" mm")
        self.clip_check = QCheckBox(tr("依遮罩裁切圖片"))
        self.clip_check.setChecked(True)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(45)
        self.width_spin = ImageSizeSpinBox()
        self.width_spin.setRange(0.000001, 300)
        self.width_spin.setDecimals(8)
        self.width_spin.setSuffix(" mm")
        self.height_spin = ImageSizeSpinBox()
        self.height_spin.setRange(0.000001, 300)
        self.height_spin.setDecimals(8)
        self.height_spin.setSuffix(" mm")
        self.bleed_spin = SafeDoubleSpinBox()
        self.bleed_spin.setRange(0, 5)
        self.bleed_spin.setDecimals(2)
        self.bleed_spin.setSuffix(" mm")
        self.bleed_color = QColor("white")
        self.bleed_color_button = QPushButton(tr("出血顏色…"))
        self.bleed_color_button.clicked.connect(self._choose_bleed_color)
        self.keep_ratio_check = QCheckBox(tr("鎖定比例"))
        self.keep_ratio_check.setChecked(True)
        dpi = self.image.dotsPerMeterX() * 0.0254 if self.image.dotsPerMeterX() else 96
        default_width = min(150.0, self.image.width() * 25.4 / dpi,
                            300.0 * self.image.width() / self.image.height())
        self.width_spin.setValue(default_width)
        self.height_spin.setValue(default_width * self.image.height() / self.image.width())
        self.dpi_spin = ImageSizeSpinBox()
        self.dpi_spin.setDecimals(2)
        self.dpi_spin.setRange(0.01, 1_000_000_000_000_000)
        self.dpi_spin.setSuffix(" dpi")
        self.dpi_spin.setValue(self.image.width()*25.4 / self.width_spin.value())

        form = QFormLayout()
        form.addRow(tr("遮罩來源"), self.mask_mode_combo)
        form.addRow(self.background_button)
        form.addRow(self.add_color_button, self.clear_colors_button)
        form.addRow(self.color_summary)
        form.addRow(tr("顏色容差"), self.tolerance_spin)
        form.addRow(tr("輪廓平滑"), self.smoothing_spin)
        form.addRow(tr("最小輪廓面積"), self.minimum_area)
        form.addRow(tr("輪廓偏移"), self.offset_spin)
        form.addRow(tr("圖片寬度"), self.width_spin)
        form.addRow(tr("圖片高度"), self.height_spin)
        form.addRow(tr("水平解析度"), self.dpi_spin)
        form.addRow(self.keep_ratio_check, self.clip_check)
        form.addRow(tr("輪廓出血"), self.bleed_spin)
        form.addRow(self.bleed_color_button, QLabel())
        form.addRow(tr("預覽遮罩透明度"), self.opacity_slider)
        form.addRow(self.invert_check, self.holes_check)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        preview_column = QVBoxLayout()
        preview_column.addWidget(self.preview_label, 1)
        settings_widget = QWidget()
        settings_widget.setLayout(form)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setWidget(settings_widget)
        self.settings_scroll.setMinimumWidth(330)
        content = QHBoxLayout()
        content.addLayout(preview_column, 1)
        content.addWidget(self.settings_scroll)
        layout = QVBoxLayout(self)
        output_row = QFormLayout()
        output_row.addRow(tr("輸出用途"), self.mode_combo)
        self.mode_help = QLabel(objectName="muted")
        self.mode_help.setWordWrap(True)
        output_row.addRow(self.mode_help)
        layout.addLayout(output_row)
        layout.addLayout(content, 1)
        layout.addWidget(buttons)
        self.resize(980, 720)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("匯入"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        self._original_ratio = self.image.height() / self.image.width()
        self.width_spin.valueChanged.connect(self._width_changed)
        self.height_spin.valueChanged.connect(self._height_changed)
        self.dpi_spin.valueChanged.connect(self._dpi_changed)
        self.keep_ratio_check.toggled.connect(self._ratio_toggled)
        self.mask_mode_combo.currentIndexChanged.connect(self._update_controls)
        self.mode_combo.currentIndexChanged.connect(self._update_controls)
        for control in (self.mode_combo, self.invert_check, self.holes_check,
                        self.smoothing_spin, self.offset_spin, self.clip_check,
                        self.opacity_slider, self.tolerance_spin, self.bleed_spin, self.minimum_area):
            signal = getattr(control, "currentIndexChanged", None) or getattr(control, "toggled", None)
            if signal is None:
                signal = getattr(control, "valueChanged")
            signal.connect(self.update_preview)
        self._update_controls()
        self.update_preview()

    def _width_changed(self, width: float) -> None:
        if self.keep_ratio_check.isChecked():
            width = min(width, 300 / self._original_ratio)
            self.width_spin.blockSignals(True)
            self.width_spin.setValue(width)
            self.width_spin.blockSignals(False)
            self.height_spin.blockSignals(True)
            self.height_spin.setValue(width * self._original_ratio)
            self.height_spin.blockSignals(False)
        self.dpi_spin.blockSignals(True)
        self.dpi_spin.setValue(self.image.width()*25.4 / self.width_spin.value())
        self.dpi_spin.blockSignals(False)
        self.update_preview()

    def _height_changed(self, height: float) -> None:
        if self.keep_ratio_check.isChecked():
            height = min(height, 300 * self._original_ratio)
            self.height_spin.blockSignals(True)
            self.height_spin.setValue(height)
            self.height_spin.blockSignals(False)
            self.width_spin.blockSignals(True)
            self.width_spin.setValue(height / self._original_ratio)
            self.width_spin.blockSignals(False)
            self.dpi_spin.blockSignals(True)
            self.dpi_spin.setValue(self.image.width()*25.4 / self.width_spin.value())
            self.dpi_spin.blockSignals(False)
        self.update_preview()

    def _dpi_changed(self, dpi: float) -> None:
        self.width_spin.setValue(self.image.width()*25.4 / dpi)
        self._width_changed(self.width_spin.value())

    def _ratio_toggled(self, locked: bool) -> None:
        if locked:
            self._height_changed(self.height_spin.value())

    def _choose_background(self) -> None:
        color = QColorDialog.getColor(self.background_color, self, tr("背景顏色"))
        if color.isValid():
            self.background_color = color
            self.update_preview()

    def _update_controls(self, *_args) -> None:
        mode = self.mode_combo.currentData()
        self.mode_help.setText(tr({
            "background": "只列印圖片，不產生切割工作；加入後仍可修改輸出用途。",
            "cut_only": "只輸出切線，圖片不列印；原始圖片會保留以便日後修改。",
            "print_cut": "列印圖片並使用定位標記切割輪廓。",
        }[mode]))
        tracing = mode != "background"
        for control in (self.mask_mode_combo, self.invert_check, self.holes_check,
                        self.smoothing_spin, self.minimum_area, self.offset_spin,
                        self.clip_check, self.opacity_slider):
            control.setEnabled(tracing)
        color_mode = tracing and self.mask_mode_combo.currentData() == "background"
        self.background_button.setEnabled(color_mode)
        foreground = tracing and self.mask_mode_combo.currentData() == "colors"
        self.tolerance_spin.setEnabled(color_mode or foreground)
        self.add_color_button.setEnabled(foreground and len(self.foreground_colors) < 16)
        self.clear_colors_button.setEnabled(foreground and bool(self.foreground_colors))
        self.color_summary.setText(", ".join(self.foreground_colors))
        self.color_summary.setWordWrap(True)
        is_print = self.mode_combo.currentData() == "print_cut"
        self.bleed_spin.setEnabled(is_print)
        self.bleed_color_button.setEnabled(is_print)
        self.update_preview()

    def _choose_foreground_color(self):
        color = QColorDialog.getColor(QColor("#000000"), self, tr("選取顏色"))
        if color.isValid() and color.name() not in self.foreground_colors and len(self.foreground_colors) < 16:
            self.foreground_colors.append(color.name())
            self._update_controls()

    def _clear_foreground_colors(self):
        self.foreground_colors = []
        self._update_controls()

    def _choose_bleed_color(self) -> None:
        color = QColorDialog.getColor(self.bleed_color, self, tr("出血顏色"))
        if color.isValid():
            self.bleed_color = color
            self.update_preview()

    def trace_options(self):
        return {"mode": self.mode_combo.currentData(), "mask_mode": self.mask_mode_combo.currentData(),
                "colors": self.foreground_colors[:],
                "background": self.background_color.name(), "tolerance": int(self.tolerance_spin.value()),
                "inverted": self.invert_check.isChecked(), "keep_holes": self.holes_check.isChecked(),
                "smoothing": int(self.smoothing_spin.value()), "minimum_area_mm2": self.minimum_area.value(),
                "offset_mm": self.offset_spin.value(), "width_mm": self.width_spin.value(),
                "height_mm": self.height_spin.value(), "bleed_mm": self.bleed_spin.value(),
                "bleed_color": self.bleed_color.name(), "clip": self.clip_check.isChecked()}

    def _mask_and_paths(self, image: QImage | None = None):
        image = self.image if image is None else image
        options = self.trace_options()
        key = (image.cacheKey(), tuple(options.items()))
        if getattr(self, "_mask_cache", (None, None))[0] != key:
            self._mask_cache = key, trace_paths(image, options)
        return self._mask_cache[1]

    def update_preview(self, *_args) -> None:
        if not hasattr(self, "preview_label"):
            return
        max_size = self.preview_label.size() - QSize(16, 16)
        _, raw_path, cut_path, bleed_path = self._mask_and_paths()
        sx, sy = self.width_spin.value() / self.image.width(), self.height_spin.value() / self.image.height()
        to_mm = QTransform.fromScale(sx, sy)
        view = QRectF(0, 0, self.width_spin.value(), self.height_spin.value())
        view = view.united((cut_path * to_mm).boundingRect())
        is_print = self.mode_combo.currentData() == "print_cut"
        if is_print and self.bleed_spin.value():
            view = view.united((bleed_path * to_mm).boundingRect())
        factor = min((max_size.width()-16) / view.width(), (max_size.height()-16) / view.height())
        canvas = QImage(max_size, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(QColor("#edf0ee"))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate((canvas.width()-view.width()*factor)/2-view.left()*factor,
                          (canvas.height()-view.height()*factor)/2-view.top()*factor)
        painter.scale(factor*sx, factor*sy)
        self.preview_to_source, _ = painter.worldTransform().inverted()
        if is_print and self.bleed_spin.value():
            painter.fillPath(bleed_path, self.bleed_color)
        painter.save()
        if self.clip_check.isChecked() and is_print:
            painter.setClipPath(raw_path)
        painter.drawImage(0, 0, self.image)
        painter.restore()
        if self.mode_combo.currentData() != "background":
            painter.fillPath(raw_path, QColor(8, 126, 114, round(self.opacity_slider.value() * 2.55)))
            pen = QPen(QColor("#d45c3d"), 1.5)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(cut_path)
        painter.end()
        self.preview_label.setPixmap(QPixmap.fromImage(canvas))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self.update_preview)

    def accept(self) -> None:
        try:
            if not self.build_items():
                raise ValueError(tr("遮罩沒有可匯入的輪廓，請調整去背設定。"))
        except ValueError as error:
            QMessageBox.warning(self, tr("無法匯入圖片"), str(error))
            return
        super().accept()

    def build_items(self) -> list[dict[str, Any]]:
        return build_raster_items(self.image, self.image_name, self.trace_options(), self._mask_and_paths())

"""Physical printer/PDF output and solid right-angle registration marks.

Printer jobs are submitted only by an explicit call to print_document.
"""
import math

from PySide6.QtCore import QMarginsF, QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import (QColorSpace, QPageLayout, QPageSize, QPainter, QPainterPath,
                           QPdfWriter, QPen, QPolygonF, QImage)

from .i18n import tr
from .artwork import draw_printed_item
from .layers import cuttable, printable


MARK_LENGTH_MM = 12.5
MARK_STROKE_MM = 1.0
SHORT_MARK_LENGTH_MM = 4.0


def printer_profiles(paper_width, paper_height):
    """Read installed print-queue names and driver-reported hard margins; never print."""
    from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
    profiles = []
    for info in QPrinterInfo.availablePrinters():
        printer = QPrinter(info)
        size = QPageSize(QSizeF(min(paper_width, paper_height), max(paper_width, paper_height)),
                         QPageSize.Unit.Millimeter)
        accepted = printer.setPageSize(size)
        set_orientation = getattr(printer, "setPageOrientation", None)
        if set_orientation:
            set_orientation(QPageLayout.Orientation.Landscape if paper_width > paper_height
                            else QPageLayout.Orientation.Portrait)
        layout = printer.pageLayout()
        layout.setUnits(QPageLayout.Unit.Millimeter)
        margins = layout.minimumMargins()
        profiles.append({"name": info.printerName(), "default": info.isDefault(),
                         "paper_accepted": accepted,
                         "margins_mm": [margins.left(), margins.top(), margins.right(), margins.bottom()]})
    return profiles


def registration_profile(registration):
    stroke = registration.get("mark_stroke", MARK_STROKE_MM)
    short_length = registration.get("mark_short_length", SHORT_MARK_LENGTH_MM)
    for value, low, high in ((stroke, 0.5, 1.5), (short_length, 4, 12.5)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high):
            raise ValueError(tr("定位標記尺寸無效。"))
    return stroke, short_length


def registration_geometry(bounds, *, stroke=MARK_STROKE_MM, short_length=SHORT_MARK_LENGTH_MM):
    """Four filled Ls; the bottom-left keeps the native 12.5 mm arms."""
    registration_profile({"mark_stroke": stroke, "mark_short_length": short_length})
    left, top, right, bottom = bounds
    half = stroke / 2
    polygons = []
    for x, y, dx, dy, length in (
            (left, bottom, 1, -1, MARK_LENGTH_MM),
            (left, top, 1, 1, short_length),
            (right, top, -1, 1, short_length),
            (right, bottom, -1, -1, short_length)):
        local = [(-half, -half), (length, -half), (length, half),
                 (half, half), (half, length), (-half, length)]
        polygons.append([(x + dx * u, y + dy * v) for u, v in local])
    return polygons


def draw_registration_marks(painter, bounds, registration=None):
    stroke, short_length = registration_profile(registration or {})
    polygons = registration_geometry(bounds, stroke=stroke, short_length=short_length)
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.GlobalColor.black)
    for polygon in polygons:
        painter.drawPolygon(QPolygonF([QPointF(*point) for point in polygon]))
    painter.restore()


def _path(contours):
    path = QPainterPath()
    for contour in contours:
        if not contour:
            continue
        path.moveTo(*contour[0])
        for point in contour[1:]:
            path.lineTo(*point)
        if len(contour) > 2 and contour[-1] == contour[0]:
            path.closeSubpath()
    return path


def print_page_size(items, metadata, *, calibration=False):
    """Validate printable artwork and return its physical paper size before output."""
    registration = metadata.get("print_cut", {})
    if not registration:
        raise ValueError(tr("請先啟用 Print & Cut。"))
    width, height = registration.get("paper_width"), registration.get("paper_height")
    bounds = registration.get("bounds")
    if (any(isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 300 for value in (width, height))
            or not isinstance(bounds, (list, tuple)) or len(bounds) != 4
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in bounds)
            or not (bounds[0] < bounds[2] and bounds[1] < bounds[3])):
        raise ValueError(tr("列印紙張或定位範圍無效。"))
    if not calibration and not any(printable(item) for item in items):
        raise ValueError(tr("沒有可列印的圖稿。"))
    stroke, _ = registration_profile(registration)
    if (bounds[0] < stroke / 2 or bounds[1] < stroke / 2
            or bounds[2] + stroke / 2 > width or bounds[3] + stroke / 2 > height):
        raise ValueError(tr("定位標記超出設計紙張範圍；請調整紙邊距，或使用依印表機邊界最大化。"))
    return QSizeF(width, height)


def write_print_pdf(filename, items, metadata, *, calibration=False):
    size = print_page_size(items, metadata, calibration=calibration)
    writer = QPdfWriter(str(filename))
    writer.setResolution(600)
    writer.setPageSize(QPageSize(size, QPageSize.Unit.Millimeter))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))
    writer.setCreator("Verona")
    painter = QPainter(writer)
    if not painter.isActive():
        raise OSError(tr("無法建立列印 PDF。"))
    try:
        _draw_print_page(painter, items, metadata, calibration=calibration)
    finally:
        painter.end()


def validate_printer(printer, items, metadata, *, calibration=False):
    """Check physical paper and hard margins without starting a print job."""
    size = print_page_size(items, metadata, calibration=calibration)
    if not printer.isValid():
        raise ValueError(tr("找不到印表機"))
    layout = printer.pageLayout()
    paper = layout.fullRect(QPageLayout.Unit.Millimeter)
    if abs(paper.width() - size.width()) > .1 or abs(paper.height() - size.height()) > .1:
        raise ValueError(tr("請在列印設定選擇與設計一致的紙張尺寸與方向（{width} × {height} mm）。",
                            width=size.width(), height=size.height()))
    layout.setUnits(QPageLayout.Unit.Millimeter)
    margins = layout.minimumMargins()
    registration = metadata["print_cut"]
    left, top, right, bottom = registration["bounds"]
    half = registration_profile(registration)[0] / 2
    if registration.get("enabled") and (left - half < margins.left() - .01 or top - half < margins.top() - .01
            or right + half > size.width() - margins.right() + .01
            or bottom + half > size.height() - margins.bottom() + .01):
        raise ValueError(tr("定位標記超出這台印表機的可列印範圍，請先依印表機邊界調整定位範圍。"))


def print_document(printer, items, metadata, *, calibration=False,
                   color_profile_path=None, rendering_intent="perceptual"):
    """Print one sheet at actual size using a printer approved in the print dialog."""
    from PySide6.QtPrintSupport import QPrinter
    validate_printer(printer, items, metadata, calibration=calibration)
    # Keep document millimetres relative to the paper corner, not the hard margins.
    printer.setFullPage(True)
    painter = QPainter(printer)
    if not painter.isActive():
        raise OSError(tr("無法開始列印，請檢查印表機與列印佇列。"))
    try:
        _draw_print_page(painter, items, metadata, calibration=calibration,
                         color_profile_path=color_profile_path, rendering_intent=rendering_intent)
    except Exception:
        printer.abort()
        raise
    finally:
        finished = painter.end()
    if not finished or printer.printerState() in (QPrinter.PrinterState.Error, QPrinter.PrinterState.Aborted):
        raise OSError(tr("列印傳送失敗，請檢查列印佇列；程式不會自動重送。"))


def _draw_print_page(painter, items, metadata, *, calibration=False, color_profile_path=None,
                     rendering_intent="perceptual", preview=False):
    registration = metadata["print_cut"]
    width, height = registration["paper_width"], registration["paper_height"]
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.LosslessImageRendering)
        device = painter.device()
        painter.scale(device.logicalDpiX() / 25.4, device.logicalDpiY() / 25.4)
        painter.fillRect(QRectF(0, 0, width, height), Qt.GlobalColor.white)
        if color_profile_path:
            _draw_profiled_artwork(painter, items, width, height, color_profile_path,
                                   rendering_intent, preview)
        else:
            for item in items:
                draw_printed_item(painter, item)
        if calibration:
            painter.setPen(QPen(Qt.GlobalColor.black, 0.15))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for item in items:
                if cuttable(item):
                    painter.drawPath(_path(item["contours"]))
        if registration.get("enabled"):
            draw_registration_marks(painter, registration["bounds"], registration)
    finally:
        painter.restore()


def _draw_profiled_artwork(painter, items, width, height, profile_path, intent, preview):
    """Render artwork through an ICC transform while leaving marks and cut lines untouched."""
    from PIL import Image, ImageCms

    if intent not in ("perceptual", "relative"):
        raise ValueError(tr("不支援的 ICC 演算意圖。"))
    try:
        with open(profile_path, "rb") as profile_file:
            profile_bytes = profile_file.read()
        destination = ImageCms.getOpenProfile(profile_path)
        source = ImageCms.createProfile("sRGB")
        color_space = destination.profile.xcolor_space.strip()
        if color_space not in ("RGB", "CMYK"):
            raise ValueError(tr("ICC 色彩描述檔必須是 RGB 或 CMYK 輸出描述檔。"))
        output_mode = color_space
        intent_value = (ImageCms.Intent.PERCEPTUAL if intent == "perceptual"
                        else ImageCms.Intent.RELATIVE_COLORIMETRIC)
        output_transform = ImageCms.buildTransform(
            source, destination, "RGB", output_mode, renderingIntent=intent_value)
        preview_transform = (ImageCms.buildTransform(
            destination, source, output_mode, "RGB", renderingIntent=intent_value) if preview else None)
        screen_space = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
        target_space = QColorSpace.fromIccProfile(profile_bytes) if color_space == "RGB" else QColorSpace()
    except Exception as error:
        raise ValueError(tr("無法讀取或套用 ICC 色彩描述檔：{error}", error=error)) from error

    dpi_x = max(1, painter.device().logicalDpiX())
    dpi_y = max(1, painter.device().logicalDpiY())
    pixels_w = max(1, math.ceil(width * dpi_x / 25.4))
    pixels_h = max(1, math.ceil(height * dpi_y / 25.4))
    tile_size = max(pixels_w, pixels_h) if pixels_w * pixels_h <= 16_000_000 else 2048
    for top in range(0, pixels_h, tile_size):
        for left in range(0, pixels_w, tile_size):
            tile_width, tile_height = min(tile_size, pixels_w-left), min(tile_size, pixels_h-top)
            layer = QImage(tile_width, tile_height, QImage.Format.Format_RGBA8888)
            layer.fill(Qt.GlobalColor.transparent)
            artwork = QPainter(layer)
            try:
                artwork.setRenderHint(QPainter.RenderHint.Antialiasing)
                artwork.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                artwork.setRenderHint(QPainter.RenderHint.LosslessImageRendering)
                artwork.scale(dpi_x / 25.4, dpi_y / 25.4)
                artwork.translate(-left * 25.4 / dpi_x, -top * 25.4 / dpi_y)
                for item in items:
                    draw_printed_item(artwork, item)
            finally:
                artwork.end()
            try:
                rgba = Image.frombytes("RGBA", (tile_width, tile_height), bytes(layer.constBits()))
                alpha = rgba.getchannel("A")
                rgb = rgba.convert("RGB")
                if output_mode == "CMYK" and not preview:
                    white = Image.new("RGB", rgb.size, "white")
                    rgb = Image.composite(rgb, white, alpha)
                converted = ImageCms.applyTransform(rgb, output_transform)
                if preview:
                    converted = ImageCms.applyTransform(converted, preview_transform)
                    converted.putalpha(alpha)
                    raw = converted.tobytes("raw", "RGBA")
                    image = QImage(raw, tile_width, tile_height, tile_width * 4,
                                   QImage.Format.Format_RGBA8888).copy()
                    image.setColorSpace(screen_space)
                elif output_mode == "CMYK":
                    raw = converted.tobytes("raw", "CMYK")
                    image = QImage(raw, tile_width, tile_height, tile_width * 4,
                                   QImage.Format.Format_CMYK8888).copy()
                else:
                    converted.putalpha(alpha)
                    raw = converted.tobytes("raw", "RGBA")
                    image = QImage(raw, tile_width, tile_height, tile_width * 4,
                                   QImage.Format.Format_RGBA8888).copy()
                    if target_space.isValid():
                        image.setColorSpace(target_space)
            except Exception as error:
                raise ValueError(tr("ICC 色彩轉換失敗：{error}", error=error)) from error
            x_mm, y_mm = left * 25.4 / dpi_x, top * 25.4 / dpi_y
            w_mm, h_mm = tile_width * 25.4 / dpi_x, tile_height * 25.4 / dpi_y
            painter.drawImage(QRectF(x_mm, y_mm, w_mm, h_mm), image)

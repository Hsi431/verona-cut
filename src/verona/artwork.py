"""Shared printed artwork geometry; bleed never changes cutting contours."""
from collections import OrderedDict
import math
import re

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen, QTransform

from .raster_import import _offset_path, _stroke_region, decode_embedded_image
from .layers import printable


_BLEED_PATHS = OrderedDict()
_BLEED_PATH_LIMIT = 64


def contour_path(contours):
    path = QPainterPath()
    for contour in contours:
        if contour:
            path.moveTo(*contour[0])
            for point in contour[1:]:
                path.lineTo(*point)
            if len(contour) > 2 and contour[0] == contour[-1]:
                path.closeSubpath()
    return path


def validate_bleed(value):
    if (not isinstance(value, dict) or not {"width_mm", "color"} <= set(value)
            or set(value) - {"width_mm", "color", "inner_mm", "mode"}):
        raise ValueError("Bleed requires width_mm and a #RRGGBB color.")
    width = value["width_mm"]
    if (isinstance(width, bool) or not isinstance(width, (int, float))
            or not math.isfinite(width) or not 0 <= width <= 5
            or not isinstance(value["color"], str)
            or not re.fullmatch(r"#[0-9a-fA-F]{6}", value["color"])):
        raise ValueError("Bleed width must be 0–5 mm and color must be #RRGGBB.")
    inner = value.get("inner_mm", 0)
    if isinstance(inner, bool) or not isinstance(inner, (int, float)) or not math.isfinite(inner) or not 0 <= inner <= 1:
        raise ValueError("Inner bleed must be a finite width from 0 to 1 mm.")
    result = {"width_mm": float(width), "color": value["color"].lower()}
    if inner:
        result["inner_mm"] = float(inner)
    mode = value.get("mode", "solid")
    if mode not in ("solid", "edge"):
        raise ValueError("Bleed mode must be solid or edge.")
    if mode == "edge":
        result["mode"] = mode
    return result


def image_path(item):
    if item.get("image_clip"):
        return contour_path(item["image_clip"])
    image = decode_embedded_image(item["image_data"])
    path = QPainterPath()
    path.addRect(QRectF(0, 0, image.width(), image.height()))
    tx, ty, a, b, c, d = item["image_transform"]
    return QTransform(a, b, c, d, tx, ty).map(path)


def _cached_bleed(kind, width, contours, build):
    """Bleed rings depend only on the cut contours and width; share them across outputs."""
    key = (kind, width, tuple(tuple(map(tuple, contour)) for contour in contours))
    path = _BLEED_PATHS.get(key)
    if path is None:
        path = build(contour_path(contours))
        _BLEED_PATHS[key] = path
        while len(_BLEED_PATHS) > _BLEED_PATH_LIMIT:
            _BLEED_PATHS.popitem(last=False)
    else:
        _BLEED_PATHS.move_to_end(key)
    return QPainterPath(path)


def bleed_path(item):
    bleed = item.get("bleed")
    if not bleed or not bleed["width_mm"]:
        return QPainterPath()
    width = bleed["width_mm"]
    return _cached_bleed("outer", width, item["contours"],
                         lambda base: _offset_path(base, width).subtracted(base))


def draw_bleed(painter, item):
    if item.get("bleed", {}).get("width_mm"):
        if item["bleed"].get("mode") == "edge":
            from .edge_bleed import draw_edge_bleed
            draw_edge_bleed(painter, item)
        else:
            _draw_solid_band(painter, item)


def inner_bleed_path(item):
    width = item.get("bleed", {}).get("inner_mm", 0)
    if not width:
        return QPainterPath()
    return _cached_bleed("inner", width, item["contours"],
                         lambda base: base.subtracted(_offset_path(base, -width)))


def draw_inner_bleed(painter, item):
    if item.get("bleed", {}).get("inner_mm"):
        _draw_solid_band(painter, item, inner=True)


def _draw_solid_band(painter, item, *, inner=False):
    """Clip a round stroke instead of resolving thousands of overlapping joins."""
    width = item["bleed"]["inner_mm" if inner else "width_mm"]
    base = contour_path(item["contours"])
    clip = base
    if not inner:
        clip = QPainterPath()
        clip.setFillRule(Qt.FillRule.OddEvenFill)
        clip.addRect(base.controlPointRect().adjusted(-width, -width, width, width))
        clip.addPath(base)
    painter.save()
    try:
        painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        pen = QPen(QColor(item["bleed"]["color"]), width * 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.strokePath(base, pen)
    finally:
        painter.restore()


def draw_printed_item(painter, item, *, image=None, clip_path=None, outer_path=None,
                      inner_path=None, bleed_layer=None):
    """Draw one printed item in document order, accepting the canvas's cached geometry."""
    if not printable(item):
        return
    if "image_data" not in item:
        draw_vector_print(painter, item)
        return
    bleed = item.get("bleed", {})
    painter.save()
    try:
        if bleed.get("width_mm"):
            if bleed.get("mode") == "edge":
                from .edge_bleed import draw_edge_bleed
                draw_edge_bleed(painter, item, bleed_layer, outer_path)
            else:
                if outer_path is not None:
                    painter.fillPath(outer_path, QColor(bleed["color"]))
                else:
                    _draw_solid_band(painter, item)
        if item.get("image_clip"):
            painter.setClipPath(clip_path if clip_path is not None else contour_path(item["image_clip"]),
                                Qt.ClipOperation.IntersectClip)
        tx, ty, a, b, c, d = item["image_transform"]
        painter.setTransform(QTransform(a, b, c, d, tx, ty), True)
        painter.drawImage(QPointF(0, 0), decode_embedded_image(item["image_data"]) if image is None else image)
    finally:
        painter.restore()
    if bleed.get("inner_mm"):
        if inner_path is not None:
            painter.fillPath(inner_path, QColor(bleed["color"]))
        else:
            _draw_solid_band(painter, item, inner=True)


def item_bounds(item, *, printed=False):
    bounds = contour_path(item["contours"]).controlPointRect()
    if printed and printable(item) and "image_data" in item:
        printed_path = image_path(item)
        bounds = bounds.united(printed_path.controlPointRect())
        width = item.get("bleed", {}).get("width_mm", 0)
        if width:
            bounds = bounds.united(contour_path(item["contours"]).controlPointRect().adjusted(-width, -width, width, width))
    elif printed and printable(item) and item.get("print_style"):
        bounds = bounds.united(vector_print_path(item).controlPointRect())
    return bounds


def validate_print_style(value):
    if not isinstance(value, dict) or set(value) != {"fill", "stroke", "width_mm"}:
        raise ValueError("Vector print style requires fill, stroke and width_mm.")
    result = dict(value)
    for key in ("fill", "stroke"):
        color = value[key]
        if color is not None:
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ValueError("Print colors must be #RRGGBB or null.")
            result[key] = color.lower()
    width = value["width_mm"]
    if isinstance(width, bool) or not isinstance(width, (int, float)) or not math.isfinite(width) or not .01 <= width <= 5:
        raise ValueError("Printed stroke width must be0.01–5mm.")
    result["width_mm"] = float(width)
    return result


def vector_print_path(item):
    style = item.get("print_style")
    if not style:
        return QPainterPath()
    path = contour_path(item["contours"])
    result = path if style["fill"] else QPainterPath()
    if style["stroke"]:
        result = result.united(_stroke_region(path, style["width_mm"]))
    return result


def draw_vector_print(painter, item):
    style = item.get("print_style")
    if not style:
        return
    painter.save()
    painter.setBrush(QColor(style["fill"]) if style["fill"] else Qt.BrushStyle.NoBrush)
    if style["stroke"]:
        pen = QPen(QColor(style["stroke"]), style["width_mm"])
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
    else:
        painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPath(contour_path(item["contours"]))
    painter.restore()


def bounds_list(bounds):
    return [bounds.left(), bounds.top(), bounds.right(), bounds.bottom()]

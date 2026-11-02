"""Closed-vector boolean operations with bounded millimetre flattening."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainterPath, QTransform


BOOLEAN_MODES = {"weld", "subtract", "intersect", "exclude"}
_BOOLEAN_SCALE = 100
_MIN_CONTOUR_AREA_MM2 = 1e-8


def _scaled_path(item):
    contours = item.get("contours")
    if not isinstance(contours, list) or not contours:
        raise ValueError("Choose vector objects with closed contours for path operations.")
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    for contour in contours:
        if not isinstance(contour, list) or len(contour) < 4 or contour[0] != contour[-1]:
            raise ValueError("Path operations need closed contours with at least three points.")
        for point in contour:
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or any(isinstance(value, bool) or not isinstance(value, (float, int))
                           or not math.isfinite(value) for value in point)):
                raise ValueError("Path operations require finite millimetre coordinates.")
        path.moveTo(*contour[0])
        for x, y in contour[1:-1]:
            path.lineTo(x, y)
        path.closeSubpath()
    scaled = QTransform.fromScale(_BOOLEAN_SCALE, _BOOLEAN_SCALE).map(path).simplified()
    if scaled.isEmpty():
        raise ValueError("A selected object has no filled area to operate on.")
    return scaled


def _contour_area(contour):
    return sum(x1*y2-x2*y1 for (x1, y1), (x2, y2)
               in zip(contour, contour[1:])) / 2


def _flatten_boolean_path(path):
    contours = []
    for polygon in path.toSubpathPolygons():
        points = []
        for point in polygon:
            value = (float(point.x()) / _BOOLEAN_SCALE, float(point.y()) / _BOOLEAN_SCALE)
            if not points or value != points[-1]:
                points.append(value)
        if len(points) > 1 and points[0] != points[-1]:
            points.append(points[0])
        if len(points) < 4 or abs(_contour_area(points)) <= _MIN_CONTOUR_AREA_MM2:
            continue
        contours.append(points)
    if not contours:
        raise ValueError("The selected paths produce no filled area.")
    return contours


def boolean_contours(items, mode):
    """Combine item paths; return closed contours at 0.01 mm working precision."""
    if not isinstance(mode, str) or mode not in BOOLEAN_MODES:
        raise ValueError("mode must be weld, subtract, intersect or exclude.")
    paths = [_scaled_path(item) for item in items]
    if len(paths) < 2:
        raise ValueError("Choose at least two vector objects for path operations.")

    if mode == "subtract":
        other = paths[1]
        for path in paths[2:]:
            other = other.united(path)
        result = paths[0].subtracted(other)
    else:
        result = QPainterPath(paths[0])
        for path in paths[1:]:
            if mode == "weld":
                result = result.united(path)
            elif mode == "intersect":
                result = result.intersected(path)
            else:
                result = result.subtracted(path).united(path.subtracted(result))
    return _flatten_boolean_path(result.simplified())

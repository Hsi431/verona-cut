"""Geometry and detached-copy helpers shared by editor operations."""

from __future__ import annotations

import copy
from typing import Any

from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath, QTransform


def contour_path(contours: list[list[tuple[float, float]]]) -> QPainterPath:
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


def flatten_path(path: QPainterPath) -> list[list[tuple[float, float]]]:
    result: list[list[tuple[float, float]]] = []
    precise = QTransform.fromScale(20, 20).map(path)
    for polygon in precise.toSubpathPolygons():
        points = [(float(point.x()) / 20, float(point.y()) / 20) for point in polygon]
        if len(points) >= 2:
            result.append(points)
    return result


def item_path(item: dict[str, Any]) -> QPainterPath:
    return contour_path(item.get("contours", []))


def copy_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Validated points are immutable tuples. Detach contour lists without
    # revisiting every coordinate in every undo snapshot.
    memo = {id(contour): list(contour) for item in items
            for key in ("contours", "image_clip") for contour in item.get(key, [])}
    return copy.deepcopy(items, memo)


def copy_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(snapshot, {id(snapshot["items"]): copy_items(snapshot["items"])})


def transform_item(item: dict[str, Any], transform: QTransform,
                   source: dict[str, Any] | None = None) -> None:
    source = source or item
    a, b, c, d = transform.m11(), transform.m12(), transform.m21(), transform.m22()
    tx, ty = transform.dx(), transform.dy()
    for key in ("contours", "image_clip"):
        if key in source:
            item[key] = [[(a*x + c*y + tx, b*x + d*y + ty) for x, y in contour]
                         for contour in source[key]]
    if source.get("node_paths"):
        from .nodes import flatten_path as flatten_node_path
        paths = copy.deepcopy(source["node_paths"])
        for key, path in paths.items():
            for node in path["nodes"]:
                for role in ("point", "in", "out"):
                    if node.get(role) is not None:
                        point = transform.map(QPointF(*node[role]))
                        node[role] = (point.x(), point.y())
            item["contours"][int(key)] = flatten_node_path(path)
        item["node_paths"] = paths
    if "image_transform" in source:
        tx, ty, a, b, c, d = source["image_transform"]
        origin = transform.map(QPointF(tx, ty))
        x_basis = transform.map(QPointF(tx + a, ty + b))
        y_basis = transform.map(QPointF(tx + c, ty + d))
        item["image_transform"] = [origin.x(), origin.y(), x_basis.x() - origin.x(),
                                   x_basis.y() - origin.y(), y_basis.x() - origin.x(),
                                   y_basis.y() - origin.y()]

"""Bounded nearest-source color extension, clipped to the outer bleed geometry."""
from collections import OrderedDict
import math

import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QImage, QPainter, QTransform
from scipy.ndimage import distance_transform_edt

from .artwork import _draw_solid_band, contour_path, image_path
from .raster_import import _rgba_array, decode_embedded_image


_LAYERS = OrderedDict()
_LAYER_LIMIT = 16


def _layer_key(item):
    contours = tuple(tuple(map(tuple, contour)) for contour in item["contours"])
    clip = tuple(tuple(map(tuple, contour)) for contour in item.get("image_clip") or ())
    return (item["image_data"], tuple(item["image_transform"]), item["bleed"]["width_mm"], contours, clip)


def edge_bleed_image(item):
    """Return the cached extension layer for this exact image, cut, clip and bleed width."""
    key = _layer_key(item)
    layer = _LAYERS.get(key)
    if layer is None:
        layer = _edge_bleed_image(item)
        _LAYERS[key] = layer
        while len(_LAYERS) > _LAYER_LIMIT:
            _LAYERS.popitem(last=False)
    else:
        _LAYERS.move_to_end(key)
    return layer


def rigid_move(base, item):
    """Return the translate/rotate/mirror mapping base onto item, or None.

    Bleed rings are offsets of the cut, so a rigid move maps geometry computed
    for the base position onto the moved item exactly.
    """
    if item["image_data"] is not base["image_data"]:
        return None
    tx, ty, a, b, c, d = base["transform"]
    inverse, invertible = QTransform(a, b, c, d, tx, ty).inverted()
    if not invertible:
        return None
    tx, ty, a, b, c, d = item["image_transform"]
    move = inverse * QTransform(a, b, c, d, tx, ty)
    linear = np.array([[move.m11(), move.m12()], [move.m21(), move.m22()]])
    if not np.allclose(linear @ linear.T, np.eye(2), rtol=0, atol=1e-9):
        return None
    for old, new in ((base["contours"], item["contours"]), (base["clip"], item.get("image_clip") or [])):
        if len(old) != len(new) or any(len(a) != len(b) for a, b in zip(old, new)):
            return None
        for before, after in zip(old, new):
            mapped = np.asarray(before, dtype=float) @ linear + (move.dx(), move.dy())
            if not np.allclose(mapped, np.asarray(after, dtype=float), rtol=0, atol=1e-6):
                return None
    return move


def moved_edge_layer(base, layer, item):
    """Reuse a layer when the item is the base moved rigidly; any other change returns None."""
    if item["bleed"]["width_mm"] != base["width"]:
        return None
    move = rigid_move(base, item)
    return None if move is None else (layer[0], layer[1] * move)


def _edge_bleed_image(item):
    """Return an extension raster and its world transform; original image stays separate.

    Sample at up to 300 dpi with a maximum 1200-pixel edge. Only visible source
    colors inside the cut contour provide seeds. Display uses a clipped round
    stroke, so detailed contours never require offset boolean operations.
    """
    base = contour_path(item["contours"])
    if base.isEmpty():
        return QImage(), QTransform()
    margin = item["bleed"]["width_mm"]
    box = base.controlPointRect().adjusted(-margin, -margin, margin, margin)
    if box.isEmpty():
        return QImage(), QTransform()
    density = min(300/25.4, 1200/max(box.width(), box.height()))
    width = min(1200, max(1, math.ceil(box.width()*density)))
    height = min(1200, max(1, math.ceil(box.height()*density)))
    sx, sy = width/box.width(), height/box.height()
    raster = QImage(width, height, QImage.Format.Format_RGBA8888)
    raster.fill(Qt.GlobalColor.transparent)
    painter = QPainter(raster)
    try:
        painter.scale(sx, sy)
        painter.translate(-box.left(), -box.top())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setClipPath(base)
        painter.setClipPath(image_path(item), Qt.ClipOperation.IntersectClip)
        tx, ty, a, b, c, d = item["image_transform"]
        painter.setTransform(QTransform(a, b, c, d, tx, ty), True)
        painter.drawImage(QPointF(0, 0), decode_embedded_image(item["image_data"]))
    finally:
        painter.end()
    pixels = _rgba_array(raster)
    alpha = pixels[:, :, 3]
    maximum = int(alpha.max())
    if not maximum:
        raise ValueError("The cut/print mask contains no visible source colors to extend.")
    seeds = alpha >= max(1, math.ceil(maximum*.9))
    nearest = distance_transform_edt(~seeds, sampling=(1/sy, 1/sx),
                                   return_distances=False, return_indices=True)
    extended = np.ascontiguousarray(pixels[nearest[0], nearest[1]])
    image = QImage(extended.data, width, height, width*4, QImage.Format.Format_RGBA8888).copy()
    return image, QTransform(1/sx, 0, 0, 1/sy, box.left(), box.top())


def check_edge_bleed(item):
    """Reject an edited image whose edge bleed would have no source colors to extend."""
    bleed = item.get("bleed")
    if bleed and bleed.get("mode") == "edge" and bleed["width_mm"]:
        edge_bleed_image(item)


def draw_edge_bleed(painter, item, layer=None, ring=None):
    image, transform = edge_bleed_image(item) if layer is None else layer
    if image.isNull():
        return
    if ring is None:
        brush = QBrush(image)
        brush.setTransform(transform)
        _draw_solid_band(painter, item, brush=brush)
        return
    painter.save()
    try:
        painter.setClipPath(ring, Qt.ClipOperation.IntersectClip)
        painter.setTransform(transform, True)
        painter.drawImage(QPointF(0, 0), image)
    finally:
        painter.restore()

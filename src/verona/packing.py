"""Bounded mixed-unit packing with exact footprint collision checks."""
import time

import numpy as np

from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath, QPainterPathStroker, QPolygonF, QTransform

from .artwork import bleed_path, contour_path, image_path, item_bounds, vector_print_path
from .editor_actions import cutting_frame, editable_indices, finite_number
from .grouping import remap_group_ids, selection_units
from .i18n import tr
from .raster_import import _offset_path, _without_holes
from .layers import cuttable, printable


def _footprint(items, shape_aware):
    shape, printed = QPainterPath(), QPainterPath()
    box = None
    for item in items:
        bound = item_bounds(item, printed=True)
        box = bound if box is None else box.united(bound)
        if shape_aware:
            shape = shape.united(contour_path(item["contours"]))
            for contour in item["contours"]:
                if len(contour) < 4 or contour[0] != contour[-1]:
                    stroke = QPainterPathStroker()
                    stroke.setWidth(.000002)
                    shape = shape.united(stroke.createStroke(contour_path([contour])))
        if printable(item) and "image_data" in item:
            art = image_path(item).united(bleed_path(item))
            printed = printed.united(art)
        elif printable(item) and item.get("print_style"):
            printed = printed.united(vector_print_path(item))
    if not shape_aware:
        shape.addRect(box)
    else:
        shape = _without_holes(shape.united(printed))
    if box.width() <= 0 or box.height() <= 0:
        shape.addRect(box.adjusted(-.000001, -.000001, .000001, .000001))
    return shape, printed


def _buffer(path, gap):
    if not gap or path.isEmpty():
        return path
    return _offset_path(path, gap / 2)


def _anchor_values(path, sampled):
    """Edges, and for shapes sampled outline points, that later positions may abut."""
    box = path.boundingRect()
    xs, ys = [], []
    if sampled:
        step = max(1, path.elementCount()//32)
        for index in range(0, path.elementCount(), step):
            point = path.elementAt(index)
            xs.append(point.x)
            ys.append(point.y)
    return (np.array([box.right(), *xs]), np.array([box.left(), *xs]),
            np.array([box.bottom(), *ys]), np.array([box.top(), *ys]))


def _intersects(first, second):
    return first.boundingRect().intersects(second.boundingRect()) and first.intersects(second)


def pack_items(items, metadata, indices, parameters):
    from .ui import _copy_items, _transform_item
    allowed = {"gap_mm", "margin_mm", "copies_per_unit", "fill", "allow_rotate", "shape_aware"}
    if not isinstance(parameters, dict) or set(parameters) - allowed:
        raise ValueError("Unknown packing parameters.")
    gap = finite_number(parameters.get("gap_mm", 2), "gap_mm", 0, 300)
    margin = finite_number(parameters.get("margin_mm", 0), "margin_mm", 0, 150)
    for key in ("fill", "allow_rotate", "shape_aware"):
        if type(parameters.get(key, False)) is not bool:
            raise ValueError(f"{key} must be a boolean.")
    selected = editable_indices(items, indices)
    units = selection_units(items, selected)
    counts = parameters.get("copies_per_unit", [1]*len(units))
    if (not isinstance(counts, list) or len(counts) != len(units)
            or any(type(count) is not int or not 1 <= count <= 1000 for count in counts)
            or sum(counts) > 1000):
        raise ValueError(tr("每組份數需介於 1 至 1000，總份數最多 1000；每個群組或獨立物件各需一個份數。"))
    area = cutting_frame(metadata).adjusted(margin+1e-6, margin+1e-6, -margin-1e-6, -margin-1e-6)
    if area.width() <= 0 or area.height() <= 0:
        raise ValueError(tr("留邊太大，沒有剩餘的可切範圍。"))
    remaining = [item for i, item in enumerate(items) if i not in selected]
    point_counts = [sum(len(c) for i in unit for c in items[i]["contours"]) for unit in units]
    points = sum(len(c) for item in remaining for c in item["contours"])
    if (len(remaining)+sum(count*len(unit) for count, unit in zip(counts, units)) > 2000
            or points+sum(count*n for count, n in zip(counts, point_counts)) > 2_000_000):
        raise ValueError(tr("本次排版最多 2000 個物件與 200 萬個切割點，請減少份數。"))
    shape_aware = parameters.get("shape_aware", False)
    occupied = []
    marks = QPainterPath()
    registration = metadata.get("print_cut", {})
    if registration.get("enabled"):
        from .printing import registration_geometry, registration_profile
        stroke, short = registration_profile(registration)
        for polygon in registration_geometry(registration["bounds"], stroke=stroke, short_length=short):
            marks.addPolygon(QPolygonF([QPointF(*point) for point in polygon]))
            marks.closeSubpath()
    protected_marks = _buffer(marks, gap)
    mark_anchors = _anchor_values(marks, True)
    patterns = []
    for unit in units:
        raw, printed = _footprint([items[i] for i in unit], shape_aware)
        angles = (0, 90, 180, 270) if shape_aware else (0, 90)
        if not parameters.get("allow_rotate"):
            angles = (0,)
        variants = []
        for angle in angles:
            rotation = QTransform().rotate(angle)
            rotated = rotation.map(raw)
            bound = rotated.boundingRect()
            normalize = QTransform.fromTranslate(-bound.left(), -bound.top())
            normalized = normalize.map(rotated)
            variants.append({"angle": angle, "rotation": rotation, "origin": bound.topLeft(),
                "raw": normalized, "collision": _buffer(normalized, gap),
                "printed": _buffer(normalize.map(rotation.map(printed)), gap),
                "width": bound.width(), "height": bound.height()})
        patterns.append(variants)

    trials, limited = 0, False
    deadline = time.monotonic()+12
    placements = []

    # Obstacles only accumulate, so a position rejected once stays rejected;
    # remember it instead of spending the search budget on it again.
    rejected = {}
    anchors = []
    boxes = np.empty((0, 4))

    def add_obstacle(raw, collision):
        nonlocal boxes
        occupied.append((raw, collision))
        box = collision.boundingRect()
        boxes = np.vstack((boxes, (box.left(), box.top(), box.right(), box.bottom())))
        anchors.append(_anchor_values(raw, shape_aware))

    for item in remaining:
        if cuttable(item) or printable(item):
            raw, _ = _footprint([item], shape_aware)
            add_obstacle(raw, _buffer(raw, gap))

    def find_place(unit_id):
        nonlocal trials, limited
        choices = []
        for variant in patterns[unit_id]:
            width, height = variant["width"], variant["height"]
            if width > area.width() or height > area.height():
                continue
            sources = anchors + [mark_anchors] if not variant["printed"].isEmpty() and not marks.isEmpty() else anchors
            after_x, before_x, after_y, before_y = (
                np.concatenate([source[i] for source in sources] or [np.empty(0)]) for i in range(4))
            xs = np.unique(np.concatenate(((area.left(), area.right()-width), after_x+gap, before_x-width-gap)))
            ys = np.unique(np.concatenate(((area.top(), area.bottom()-height), after_y+gap, before_y-height-gap)))
            xs = xs[(xs >= area.left()) & (xs <= area.right()-width)].tolist()
            ys = ys[(ys >= area.top()) & (ys <= area.bottom()-height)].tolist()
            # Bound candidate sets without claiming exhaustive or optimal search.
            if len(xs) > 256:
                xs = [xs[i*(len(xs)-1)//255] for i in range(256)]
                limited = True
            if len(ys) > 256:
                ys = [ys[i*(len(ys)-1)//255] for i in range(256)]
                limited = True
            found = None
            seen = rejected.setdefault(id(variant), set())
            reach = variant["collision"].boundingRect()
            for y in ys:
                for x in xs:
                    if (x, y) in seen:
                        continue
                    if trials >= 20000 or time.monotonic() > deadline:
                        limited = True
                        return min(choices, key=lambda value: (value[1], value[0], value[2]["angle"])) if choices else None
                    trials += 1
                    # Inclusive box overlap is a superset of _intersects; only
                    # those obstacles need the exact path test.
                    near = np.flatnonzero((boxes[:, 0] <= x+reach.right()) & (boxes[:, 2] >= x+reach.left())
                                          & (boxes[:, 1] <= y+reach.bottom()) & (boxes[:, 3] >= y+reach.top()))
                    move = QTransform.fromTranslate(x, y)
                    if len(near):
                        collision = move.map(variant["collision"])
                        if any(_intersects(collision, occupied[index][1]) for index in near):
                            seen.add((x, y))
                            continue
                    if not variant["printed"].isEmpty() and _intersects(move.map(variant["printed"]), protected_marks):
                        seen.add((x, y))
                        continue
                    found = (x, y, variant)
                    break
                if found:
                    break
            if found:
                choices.append(found)
        return min(choices, key=lambda value: (value[1], value[0], value[2]["angle"])) if choices else None

    packed = [0]*len(units)
    def place(unit_id):
        nonlocal points, limited
        if (len(remaining)+sum(len(units[i]) for i, *_ in placements)+len(units[unit_id]) > 2000
                or points+point_counts[unit_id] > 2_000_000 or len(placements) >= 1000):
            limited = True
            return False
        position = find_place(unit_id)
        if position is None:
            return False
        x, y, variant = position
        move = QTransform.fromTranslate(x, y)
        add_obstacle(move.map(variant["raw"]), move.map(variant["collision"]))
        placements.append((unit_id, x, y, variant))
        packed[unit_id] += 1
        points += point_counts[unit_id]
        return True

    order = sorted(range(len(units)), key=lambda i: patterns[i][0]["width"]*patterns[i][0]["height"], reverse=True)
    for unit_id in order:
        for _ in range(counts[unit_id]):
            if not place(unit_id):
                raise ValueError(tr("本次搜尋未能放下全部指定份數。請減少份數，或嘗試允許旋轉與依輪廓排列。"))
    if parameters.get("fill"):
        while len(placements) < 1000:
            added = False
            for unit_id in order:
                added = place(unit_id) or added
                if trials >= 20000 or time.monotonic() > deadline:
                    limited = True
                    break
            if not added or trials >= 20000 or time.monotonic() > deadline:
                break
    result = _copy_items(remaining)
    selection = []
    for unit_id, x, y, variant in placements:
        copies = _copy_items([items[i] for i in units[unit_id]])
        remap_group_ids(copies)
        rotation, origin = variant["rotation"], variant["origin"]
        transform = QTransform(rotation.m11(), rotation.m12(), rotation.m21(), rotation.m22(),
                               x-origin.x(), y-origin.y())
        for item in copies:
            _transform_item(item, transform)
        selection.extend(range(len(result), len(result)+len(copies)))
        result.extend(copies)
    details = {"action": "pack_objects", "layout_method": "bounded corner/vertex bottom-left heuristic; not optimal nesting",
        "shape_aware": shape_aware, "copies": len(placements), "candidate_checks": trials,
        "limit_reached": limited or len(placements) >= 1000 or len(result) >= 2000 or points >= 2_000_000,
        "registration_mark_clearance_mm": gap,
        "placements": [{"unit": unit_id, "rotation_deg": variant["angle"],
                        "bounds_mm": [x, y, x+variant["width"], y+variant["height"]]}
                       for unit_id, x, y, variant in placements],
        "units": [{"source_indices": unit, "requested": requested, "copies": count}
                  for unit, requested, count in zip(units, counts, packed)]}
    return result, selection, details

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
from .geometry import copy_items as _copy_items, transform_item as _transform_item


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


def _path_area(path):
    """Area of a flattened fill path, including the path's hole rule."""
    area = 0.0
    for polygon in path.toFillPolygons():
        points = [(polygon[i].x(), polygon[i].y()) for i in range(len(polygon))]
        if len(points) > 2:
            area += abs(sum(x1*y2-x2*y1 for (x1, y1), (x2, y2)
                            in zip(points, points[1:]+points[:1]))) / 2
    return area


def _material_area(items):
    area = 0.0
    for item in items:
        if not cuttable(item) and not printable(item):
            continue
        shape = contour_path(item["contours"])
        _, printed = _footprint([item], False)
        if not printed.isEmpty():
            shape = shape.united(printed)
        area += _path_area(shape)
    return area


def _pack_single(items, metadata, indices, parameters, *, allow_partial=False,
                 candidate_limit=20000, deadline=None):
    allowed = {"gap_mm", "margin_mm", "copies_per_unit", "fill", "allow_rotate", "shape_aware",
               "rotation_step_deg", "time_limit_seconds", "new_page_for_unplaced"}
    if not isinstance(parameters, dict) or set(parameters) - allowed:
        raise ValueError("Unknown packing parameters.")
    gap = finite_number(parameters.get("gap_mm", 2), "gap_mm", 0, 300)
    margin = finite_number(parameters.get("margin_mm", 0), "margin_mm", 0, 150)
    for key in ("fill", "allow_rotate", "shape_aware", "new_page_for_unplaced"):
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
    rotation_step = parameters.get("rotation_step_deg", 90)
    if type(rotation_step) is not int or not 5 <= rotation_step <= 90:
        raise ValueError("rotation_step_deg must be an integer from 5 to 90.")
    time_limit = finite_number(parameters.get("time_limit_seconds", 12), "time_limit_seconds", .05, 60)
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
        if shape_aware:
            angles = (0, 90, 180, 270) + tuple(angle for angle in range(rotation_step, 360, rotation_step)
                                                   if angle % 90)
        else:
            angles = (0, 90)
        if not parameters.get("allow_rotate"):
            angles = (0,)
        variants = []
        buffered_raw = _buffer(raw, gap)
        buffered_printed = _buffer(printed, gap)
        for angle in angles:
            rotation = QTransform().rotate(angle)
            rotated = rotation.map(raw)
            bound = rotated.boundingRect()
            normalize = QTransform.fromTranslate(-bound.left(), -bound.top())
            normalized = normalize.map(rotated)
            variants.append({"angle": angle, "rotation": rotation, "origin": bound.topLeft(),
                "raw": normalized, "collision": normalize.map(rotation.map(buffered_raw)),
                "printed": normalize.map(rotation.map(buffered_printed)),
                "width": bound.width(), "height": bound.height()})
        patterns.append(variants)

    trials, limited = 0, False
    deadline = deadline if deadline is not None else time.monotonic()+time_limit
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
            found = None
            seen = rejected.setdefault(id(variant), set())
            reach = variant["collision"].boundingRect()
            # First try the compact set formed by bounding edges. Shape vertices
            # remain a fallback for concave interlocks, without forcing every
            # ordinary placement through their full Cartesian product.
            for sampled in (False, True):
                values = [tuple(axis if sampled or source is mark_anchors else axis[:1]
                                for axis in source) for source in sources]
                after_x = np.concatenate([source[0] for source in values] or [np.empty(0)])
                before_x = np.concatenate([source[1] for source in values] or [np.empty(0)])
                after_y = np.concatenate([source[2] for source in values] or [np.empty(0)])
                before_y = np.concatenate([source[3] for source in values] or [np.empty(0)])
                xs = np.unique(np.concatenate(((area.left(), area.right()-width),
                                               after_x+gap, before_x-width-gap)))
                ys = np.unique(np.concatenate(((area.top(), area.bottom()-height),
                                               after_y+gap, before_y-height-gap)))
                xs = xs[(xs >= area.left()) & (xs <= area.right()-width)].tolist()
                ys = ys[(ys >= area.top()) & (ys <= area.bottom()-height)].tolist()
                if len(xs) > 256:
                    xs = [xs[i*(len(xs)-1)//255] for i in range(256)]
                    limited = True
                if len(ys) > 256:
                    ys = [ys[i*(len(ys)-1)//255] for i in range(256)]
                    limited = True
                for y in ys:
                    for x in xs:
                        if (x, y) in seen:
                            continue
                        if trials >= candidate_limit or time.monotonic() > deadline:
                            limited = True
                            return min(choices, key=lambda value: (value[1], value[0], value[2]["angle"])) if choices else None
                        trials += 1
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
    missing = [0]*len(units)
    stop = False
    for unit_id in order:
        for _ in range(counts[unit_id]-packed[unit_id]):
            if not place(unit_id):
                if not allow_partial and not limited:
                    raise ValueError(tr("本次搜尋未能放下全部指定份數。請減少份數，或嘗試允許旋轉與依輪廓排列。"))
                missing[unit_id] = counts[unit_id]-packed[unit_id]
                if limited:
                    stop = True
                    break
                break
        if stop:
            break
    if stop:
        for unit_id in order:
            missing[unit_id] = max(missing[unit_id], counts[unit_id]-packed[unit_id])
    if parameters.get("fill"):
        while len(placements) < 1000:
            added = False
            for unit_id in order:
                added = place(unit_id) or added
                if trials >= candidate_limit or time.monotonic() > deadline:
                    limited = True
                    break
            if not added or trials >= candidate_limit or time.monotonic() > deadline:
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
    usable_area = area.width()*area.height()
    usage = 100*_material_area(result)/usable_area if usable_area else 0
    unplaced = [{"unit": unit_id, "copy": copy_number, "source_indices": units[unit_id],
                 "objects": [items[index]["name"] for index in units[unit_id]]}
                for unit_id, count in enumerate(missing)
                for copy_number in range(counts[unit_id]-count+1, counts[unit_id]+1)]
    details = {"action": "pack_objects", "layout_method": "bounded bottom-left placement with contour fallback",
        "shape_aware": shape_aware, "copies": len(placements), "candidate_checks": trials,
        "paper_utilization_percent": usage, "unplaced_count": len(unplaced), "unplaced": unplaced,
        "page_count": 1, "pages": [{"page": 0, "copies": len(placements),
                                     "paper_utilization_percent": usage}],
        "rotation_step_deg": rotation_step, "time_limit_seconds": time_limit,
        "limit_reached": limited or len(placements) >= 1000 or len(result) >= 2000 or points >= 2_000_000,
        "deterministic": True,
        "registration_mark_clearance_mm": gap,
        "placements": [{"unit": unit_id, "page": 0, "rotation_deg": variant["angle"],
                        "bounds_mm": [x, y, x+variant["width"], y+variant["height"]]}
                       for unit_id, x, y, variant in placements],
        "units": [{"source_indices": unit, "requested": requested, "requested_placed": requested-missing[index],
                   "copies": count}
                  for index, (unit, requested, count) in enumerate(zip(units, counts, packed))]}
    return result, selection, details


def pack_items(items, metadata, indices, parameters):
    """Pack selected units on one page, optionally continuing on new pages."""
    if not isinstance(parameters, dict):
        raise ValueError("Unknown packing parameters.")
    time_limit = finite_number(parameters.get("time_limit_seconds", 12), "time_limit_seconds", .05, 60)
    if type(parameters.get("new_page_for_unplaced", False)) is not bool:
        raise ValueError("new_page_for_unplaced must be a boolean.")
    deadline = time.monotonic()+time_limit
    if not parameters.get("new_page_for_unplaced", False):
        return _pack_single(items, metadata, indices, parameters, deadline=deadline)

    first_parameters = {**parameters, "fill": False, "new_page_for_unplaced": False}
    result, selection, first = _pack_single(items, metadata, indices, first_parameters,
                                             allow_partial=True, deadline=deadline)
    units = [entry["source_indices"] for entry in first["units"]]
    requested = [entry["requested"] for entry in first["units"]]
    placed_requested = [entry["requested_placed"] for entry in first["units"]]
    remaining = [count-done for count, done in zip(requested, placed_requested)]
    original_selected = sorted(index for unit in units for index in unit)
    candidate_checks = first["candidate_checks"]
    limited = first["limit_reached"]
    page_results = [{"items": result, "selection": selection, "placements": first["placements"],
                     "copies": first["copies"]}]
    active_units = [i for i, count in enumerate(remaining) if count]

    while any(remaining) and not limited and len(page_results) < 100:
        active_units = [i for i, count in enumerate(remaining) if count]
        page_source = _copy_items([items[index] for unit_id in active_units for index in units[unit_id]])
        page_indices = list(range(len(page_source)))
        page_counts = [remaining[unit_id] for unit_id in active_units]
        page_parameters = {**parameters, "copies_per_unit": page_counts,
                           "fill": False, "new_page_for_unplaced": False}
        page, page_selection, details = _pack_single(
            page_source, metadata, page_indices, page_parameters, allow_partial=True,
            candidate_limit=max(0, 20000-candidate_checks), deadline=deadline)
        candidate_checks += details["candidate_checks"]
        limited = limited or details["limit_reached"]
        mapped = []
        for placement in details["placements"]:
            mapped.append({**placement, "unit": active_units[placement["unit"]],
                           "page": len(page_results)})
        placed_any = False
        for local_id, unit_id in enumerate(active_units):
            count = details["units"][local_id]["requested_placed"]
            remaining[unit_id] -= count
            placed_requested[unit_id] += count
            placed_any = placed_any or count > 0
        if not placed_any:
            break
        page_results.append({"items": page, "selection": page_selection,
                             "placements": mapped, "copies": details["copies"]})

    if any(remaining) and len(page_results) >= 100:
        limited = True

    # Auto-fill each requested sheet after all minimum copies have been assigned.
    extra_copies = [0]*len(units)
    if parameters.get("fill"):
        for page_index, page_info in enumerate(page_results):
            if candidate_checks >= 20000 or time.monotonic() > deadline:
                limited = True
                break
            prototypes = _copy_items([items[index] for unit in units for index in unit])
            source = page_info["items"] + prototypes
            start = len(page_info["items"])
            prototype_indices = list(range(start, start+len(prototypes)))
            fill_parameters = {**parameters, "copies_per_unit": [1]*len(units),
                               "fill": True, "new_page_for_unplaced": False}
            filled, added_selection, fill_details = _pack_single(
                source, metadata, prototype_indices, fill_parameters, allow_partial=True,
                candidate_limit=max(0, 20000-candidate_checks), deadline=deadline)
            candidate_checks += fill_details["candidate_checks"]
            limited = limited or fill_details["limit_reached"]
            page_info["items"] = filled
            page_info["selection"] += added_selection
            page_info["copies"] += fill_details["copies"]
            for placement in fill_details["placements"]:
                page_info["placements"].append({**placement, "unit": placement["unit"], "page": page_index})
            for unit_id, entry in enumerate(fill_details["units"]):
                extra_copies[unit_id] += entry["copies"]
            if limited:
                break

    usable = cutting_frame(metadata).adjusted(
        finite_number(parameters.get("margin_mm", 0), "margin_mm", 0, 150)+1e-6,
        finite_number(parameters.get("margin_mm", 0), "margin_mm", 0, 150)+1e-6,
        -finite_number(parameters.get("margin_mm", 0), "margin_mm", 0, 150)-1e-6,
        -finite_number(parameters.get("margin_mm", 0), "margin_mm", 0, 150)-1e-6)
    usable_area = usable.width()*usable.height()
    pages = [{"page": page_index, "copies": page_info["copies"],
              "paper_utilization_percent": 100*_material_area(page_info["items"])/usable_area}
             for page_index, page_info in enumerate(page_results)]
    unplaced = [{"unit": unit_id, "copy": copy_number, "source_indices": units[unit_id],
                 "objects": [items[index]["name"] for index in units[unit_id]]}
                for unit_id, count in enumerate(remaining)
                for copy_number in range(requested[unit_id]-count+1, requested[unit_id]+1)]
    copies = sum(placed_requested)+sum(extra_copies)
    total_placements = [placement for page in page_results for placement in page["placements"]]
    details = {"action": "pack_objects", "layout_method": "bounded bottom-left placement with contour fallback",
        "shape_aware": parameters.get("shape_aware", False), "copies": copies,
        "candidate_checks": candidate_checks, "paper_utilization_percent": pages[0]["paper_utilization_percent"],
        "unplaced_count": len(unplaced), "unplaced": unplaced, "page_count": len(page_results),
        "pages": pages, "new_pages": [{"items": page["items"], "selection": page["selection"]}
                                        for page in page_results[1:]],
        "page_limit": 100,
        "rotation_step_deg": parameters.get("rotation_step_deg", 90),
        "time_limit_seconds": time_limit,
        "limit_reached": limited or copies >= 1000,
        "deterministic": True,
        "registration_mark_clearance_mm": finite_number(parameters.get("gap_mm", 2), "gap_mm", 0, 300),
        "placements": total_placements,
        "units": [{"source_indices": unit, "requested": count,
                   "requested_placed": count-missing, "copies": count-missing+extra_copies[index]}
                  for index, (unit, count, missing) in enumerate(zip(units, requested, remaining))]}
    return page_results[0]["items"], page_results[0]["selection"], details

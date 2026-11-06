"""Optional spyrrow adapter with Verona geometry and result validation."""

from concurrent.futures import ThreadPoolExecutor
import importlib
import inspect
import math
import time
from types import SimpleNamespace

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath, QPainterPathStroker, QPolygonF, QTransform

from .editor_actions import cutting_frame, editable_indices, finite_number
from .grouping import remap_group_ids, selection_units
from .layers import cuttable, printable
from .packing import (PACKING_PARAMETER_NAMES, _buffer, _footprint, _material_area,
                      _prepare_packing_source, _restore_packing_plan,
                      _transform_placement)
from .geometry import copy_items as _copy_items


_SIMPLIFY_MM = 0.05
_OUTWARD_MM = 0.05


def _spyrrow():
    try:
        module = importlib.import_module("spyrrow")
        parameters = inspect.signature(module.StripPackingInstance).parameters
    except (ImportError, AttributeError, TypeError, ValueError):
        return None
    return module if "holes" in parameters else None


def nesting_available():
    """Return whether the installed spyrrow exposes Verona's required holes API."""
    return _spyrrow() is not None


def _flattened_polygons(path):
    precise = QTransform.fromScale(20, 20).map(path)
    polygons = []
    for polygon in precise.toFillPolygons():
        points = [(float(point.x())/20, float(point.y())/20) for point in polygon]
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        if len(points) >= 3:
            polygons.append(points)
    return polygons


def _distance_to_segment(point, start, end):
    dx, dy = end[0]-start[0], end[1]-start[1]
    length = dx*dx + dy*dy
    if not length:
        return math.hypot(point[0]-start[0], point[1]-start[1])
    amount = max(0.0, min(1.0, ((point[0]-start[0])*dx + (point[1]-start[1])*dy)/length))
    return math.hypot(point[0]-start[0]-amount*dx, point[1]-start[1]-amount*dy)


def _simplify_open(points, tolerance):
    if len(points) < 3:
        return points
    keep = {0, len(points)-1}
    pending = [(0, len(points)-1)]
    while pending:
        first, last = pending.pop()
        farthest, distance = -1, tolerance
        for index in range(first+1, last):
            candidate = _distance_to_segment(points[index], points[first], points[last])
            if candidate > distance:
                farthest, distance = index, candidate
        if farthest >= 0:
            keep.add(farthest)
            pending.extend(((first, farthest), (farthest, last)))
    return [points[index] for index in sorted(keep)]


def _simplify_ring(points, tolerance=_SIMPLIFY_MM):
    if len(points) < 4:
        return points
    farthest = max(range(1, len(points)),
                   key=lambda index: (points[index][0]-points[0][0])**2
                   + (points[index][1]-points[0][1])**2)
    first_arc = _simplify_open(points[:farthest+1], tolerance)
    second_arc = _simplify_open(points[farthest:] + points[:1], tolerance)
    simplified = first_arc[:-1] + second_arc[:-1]
    return simplified if len(simplified) >= 3 else points


def _convex_hull(points):
    points = sorted(set(points))
    if len(points) <= 3:
        return points

    def cross(origin, first, second):
        return ((first[0]-origin[0])*(second[1]-origin[1])
                - (first[1]-origin[1])*(second[0]-origin[0]))

    lower, upper = [], []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _polygon_path(points):
    path = QPainterPath()
    path.addPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
    path.closeSubpath()
    return path


def _outward_buffer(path, amount):
    stroker = QPainterPathStroker()
    stroker.setWidth(amount*2)
    stroker.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    return path.united(stroker.createStroke(path)).simplified()


def _simplified_polygons(path, *, one_shape=False):
    source = _flattened_polygons(path)
    if one_shape and len(source) > 1:
        source = [_convex_hull([point for polygon in source for point in polygon])]
    expanded = QPainterPath()
    for polygon in source:
        expanded = expanded.united(_outward_buffer(
            _polygon_path(_simplify_ring(polygon)), _OUTWARD_MM))
    result = _flattened_polygons(expanded.simplified())
    if one_shape and len(result) > 1:
        result = [_convex_hull([point for polygon in result for point in polygon])]
    return [polygon for polygon in result if len(polygon) >= 3]


def _registration_path(metadata):
    registration = metadata.get("print_cut", {})
    if not registration.get("enabled"):
        return QPainterPath()
    from .printing import registration_geometry, registration_profile

    stroke, short = registration_profile(registration)
    path = QPainterPath()
    for polygon in registration_geometry(registration["bounds"], stroke=stroke,
                                         short_length=short):
        shape = QPainterPath()
        shape.addPolygon(QPolygonF([QPointF(*point) for point in polygon]))
        shape.closeSubpath()
        path = path.united(shape)
    return path


def _local(path, left, top):
    return QTransform.fromTranslate(-left, -top).map(path)


def _area_for(metadata, margin):
    area = cutting_frame(metadata).adjusted(margin+1e-6, margin+1e-6,
                                             -margin-1e-6, -margin-1e-6)
    if area.width() <= 0 or area.height() <= 0:
        raise ValueError("No usable cutting area remains after margins.")
    return area


def _unit_shapes(items, units):
    shapes, origins, paths, guards = [], [], [], []
    for unit in units:
        raw, _printed = _footprint([items[index] for index in unit], True)
        bounds = raw.boundingRect()
        if raw.isEmpty() or bounds.width() <= 0 or bounds.height() <= 0:
            raise ValueError("A selected unit has no usable collision outline.")
        local = _local(raw, bounds.left(), bounds.top())
        paths.append(local)
        polygons = _simplified_polygons(local, one_shape=True)
        if len(polygons) != 1:
            raise ValueError("A selected unit could not be represented as one outline.")
        shapes.append(polygons[0])
        guards.append(_polygon_path(polygons[0]))
        origins.append(bounds.topLeft())
    return shapes, origins, paths, guards


def _static_holes(items, selected, metadata, area):
    holes = []
    clip = QPainterPath()
    clip.addRect(QRectF(0, 0, area.width(), area.height()))
    registration = _registration_path(metadata)
    sources = [(registration, "registration marks")]
    for index, item in enumerate(items):
        if index in selected or not (cuttable(item) or printable(item)):
            continue
        raw, _printed = _footprint([item], True)
        sources.append((raw, item.get("name", f"object {index}")))
    for path, name in sources:
        if path.isEmpty():
            continue
        local = _local(path, area.left(), area.top())
        clipped = local.intersected(clip).simplified()
        for polygon in _simplified_polygons(clipped):
            if len(polygon) >= 3:
                holes.append(polygon)
    return holes


def _solve_seed(module, shapes, demands, requested, extra_demands, allowed, holes,
                width, height, gap, seconds, fill_seconds, seed):
    items = [module.Item(f"unit-{index}", shape, demand, allowed)
             for index, (shape, demand) in enumerate(zip(shapes, demands)) if demand]
    instance = module.StripPackingInstance("verona", height, items, holes=holes)
    config = module.StripPackingConfig(total_computation_time=seconds,
        seed=seed, min_items_separation=gap, num_workers=1)
    result = list(instance.solve(config).placed_items)
    if not any(extra_demands) or fill_seconds < 1:
        return [SimpleNamespace(id=str(item.id), rotation=float(item.rotation),
                                translation=tuple(item.translation)) for item in result]

    counts = [0]*len(shapes)
    fill_holes = list(holes)
    for placed in result:
        unit_id = int(str(placed.id).removeprefix("unit-"))
        counts[unit_id] += 1
        fill_holes.append(_placed_polygon(shapes[unit_id], float(placed.rotation),
                                          placed.translation))
    if any(counts[index] < requested[index] for index in range(len(requested))):
        return [SimpleNamespace(id=str(item.id), rotation=float(item.rotation),
                                translation=tuple(item.translation)) for item in result]
    remaining_extras = [max(0, extra_demands[index]
                            - max(0, counts[index]-requested[index]))
                        for index in range(len(requested))]
    for _ in range(fill_seconds//2):
        batch = [0]*len(shapes)
        for offset in range(min(20, sum(remaining_extras))):
            index = offset % len(batch)
            while remaining_extras[index] <= batch[index]:
                index = (index+1) % len(batch)
            batch[index] += 1
        if not any(batch):
            break
        fill_items = [module.Item(f"unit-{index}", shape, demand, allowed)
                      for index, (shape, demand) in enumerate(zip(shapes, batch)) if demand]
        fill_instance = module.StripPackingInstance("verona-fill", height, fill_items,
                                                    holes=fill_holes)
        fill_config = module.StripPackingConfig(total_computation_time=1,
            seed=seed, min_items_separation=gap, num_workers=1)
        try:
            filled = fill_instance.solve(fill_config)
        except BaseException:
            break
        added = 0
        for placed in filled.placed_items:
            unit_id = int(str(placed.id).removeprefix("unit-"))
            polygon = _placed_polygon(shapes[unit_id], float(placed.rotation),
                                      placed.translation)
            xs, ys = [point[0] for point in polygon], [point[1] for point in polygon]
            if (min(xs) < -1e-6 or min(ys) < -1e-6
                    or max(xs) > width+1e-6 or max(ys) > height+1e-6):
                continue
            result.append(placed)
            fill_holes.append(polygon)
            remaining_extras[unit_id] -= 1
            added += 1
        if not added:
            break
    return [SimpleNamespace(id=str(item.id), rotation=float(item.rotation),
                            translation=tuple(item.translation)) for item in result]


def _placed_polygon(shape, angle, translation):
    radians = math.radians(angle)
    cosine, sine = math.cos(radians), math.sin(radians)
    tx, ty = translation
    return [(cosine*x-sine*y+tx, sine*x+cosine*y+ty) for x, y in shape]


def _fits_paper(shape, allowed, width, height):
    if allowed is not None:
        angles = allowed
    else:
        angles = [0.0]
        for index, point in enumerate(shape):
            following = shape[(index+1) % len(shape)]
            edge = math.degrees(math.atan2(following[1]-point[1], following[0]-point[0]))
            angles.extend((-edge, -edge+90))
    for angle in angles:
        radians = math.radians(angle)
        cosine, sine = math.cos(radians), math.sin(radians)
        xs = [cosine*x-sine*y for x, y in shape]
        ys = [sine*x+cosine*y for x, y in shape]
        if max(xs)-min(xs) <= width+1e-6 and max(ys)-min(ys) <= height+1e-6:
            return True
    return False


def _intersects(first, second):
    return (first.boundingRect().intersects(second.boundingRect())
            and first.intersects(second))


def _contains_outline(guard, path):
    for polygon in _flattened_polygons(path):
        for index, point in enumerate(polygon):
            following = polygon[(index+1) % len(polygon)]
            midpoint = ((point[0]+following[0])/2, (point[1]+following[1])/2)
            if not guard.contains(QPointF(*point)) or not guard.contains(QPointF(*midpoint)):
                return False
    return True


def _clearance(path, gap):
    if not gap or path.isEmpty():
        return path
    return _buffer(path, gap+2e-4)


def _verify_layout(placed_paths, placed_guards, fixed_paths, marks,
                   obstacle_guards, area, gap):
    for path in placed_paths:
        bounds = path.boundingRect()
        if (bounds.left() < area.left()-1e-6 or bounds.top() < area.top()-1e-6
                or bounds.right() > area.right()+1e-6 or bounds.bottom() > area.bottom()+1e-6):
            return False
    # Each true outline was checked inside its outward-rounded guard before solving.
    # Disjoint guard clearances therefore prove the true shapes keep the requested gap.
    clearances = [_clearance(path, gap) for path in placed_guards]
    fixed_clearances = [_clearance(path, gap) for path in obstacle_guards]
    for index, first in enumerate(clearances):
        if any(_intersects(first, second) for second in clearances[index+1:]):
            return False
        if any(_intersects(first, fixed) for fixed in fixed_clearances):
            return False
    for path in placed_paths:
        if any(_intersects(path, fixed) for fixed in fixed_paths):
            return False
        if not marks.isEmpty() and _intersects(path, marks):
            return False
    for index, path in enumerate(placed_paths):
        if any(_intersects(path, other) for other in placed_paths[index+1:]):
            return False
    if marks.isEmpty():
        return True
    return not any(_intersects(clearance, _clearance(marks, gap))
                   for clearance in clearances)


def pack_with_nesting(items, metadata, indices, parameters, *, seed_count=3,
                      seed_start=20261106):
    """Run patched spyrrow seeds and return only a real-outline-verified plan."""
    if not isinstance(parameters, dict):
        raise ValueError("Packing parameters must be a dictionary.")
    module = _spyrrow()
    if module is None or parameters.get("new_page_for_unplaced", False):
        return None
    if not isinstance(parameters, dict) or set(parameters) - PACKING_PARAMETER_NAMES:
        raise ValueError("Unknown packing parameters.")
    if type(seed_count) is not int or not 1 <= seed_count <= 8:
        raise ValueError("seed_count must be from 1 to 8.")
    if type(seed_start) is not int or seed_start < 0:
        raise ValueError("seed_start must be a nonnegative integer.")
    for key in ("fill", "allow_rotate", "shape_aware", "new_page_for_unplaced"):
        if type(parameters.get(key, False)) is not bool:
            raise ValueError(f"{key} must be a boolean.")
    gap = finite_number(parameters.get("gap_mm", 2), "gap_mm", 0, 300)
    margin = finite_number(parameters.get("margin_mm", 0), "margin_mm", 0, 150)
    time_limit = finite_number(parameters.get("time_limit_seconds", 12),
                               "time_limit_seconds", .05, 60)
    start = time.monotonic()
    items, indices, parameters, pairing = _prepare_packing_source(items, indices, parameters)
    rotation_step = parameters.get("rotation_step_deg", 90)
    if type(rotation_step) is not int or not 5 <= rotation_step <= 90:
        raise ValueError("rotation_step_deg must be an integer from 5 to 90.")
    selected_indices = editable_indices(items, indices)
    units = selection_units(items, selected_indices)
    counts = parameters.get("copies_per_unit", [1]*len(units))
    if (not isinstance(counts, list) or len(counts) != len(units)
            or any(type(count) is not int or not 1 <= count <= 1000 for count in counts)
            or sum(counts) > 1000):
        raise ValueError("Each unit needs 1–1000 copies, with at most 1000 copies total.")
    area = _area_for(metadata, margin)
    if (len(items)-len(selected_indices)+sum(count*len(unit) for count, unit in zip(counts, units)) > 2000):
        raise ValueError("Packing is limited to 2000 objects.")

    shapes, origins, unit_paths, unit_guards = _unit_shapes(items, units)
    holes = _static_holes(items, set(selected_indices), metadata, area)
    remaining = [item for index, item in enumerate(items) if index not in selected_indices]
    fixed_paths = [_footprint([item], True)[0] for item in remaining
                   if cuttable(item) or printable(item)]
    marks = _registration_path(metadata)
    obstacle_guards = [_polygon_path(polygon) for polygon in holes]
    if any(not _contains_outline(guard, path)
           for guard, path in zip(unit_guards, unit_paths)):
        return None
    obstacle_union = QPainterPath()
    for guard in obstacle_guards:
        obstacle_union = obstacle_union.united(guard)
    clip = QPainterPath()
    clip.addRect(QRectF(0, 0, area.width(), area.height()))
    static_true = [marks] if not marks.isEmpty() else []
    static_true.extend(fixed_paths)
    for path in static_true:
        clipped = _local(path, area.left(), area.top()).intersected(clip).simplified()
        if not clipped.isEmpty() and not _contains_outline(obstacle_union, clipped):
            return None
    if not parameters.get("allow_rotate", False):
        allowed = [0.0]
    elif not parameters.get("shape_aware", False):
        allowed = [0.0, 90.0]
    else:
        allowed = None

    static_objects = len(remaining)
    source_points = sum(len(contour) for item in remaining for contour in item.get("contours", []))
    requested_objects = sum(count*len(unit) for count, unit in zip(counts, units))
    requested_points = sum(count*sum(len(contour) for index in unit
                                     for contour in items[index].get("contours", []))
                           for count, unit in zip(counts, units))
    min_unit_objects = min(len(unit) for unit in units)
    min_unit_points = min(sum(len(contour) for index in unit
                              for contour in items[index].get("contours", [])) for unit in units)
    extra_limit = min(1000-sum(counts),
                      max(0, (2000-static_objects-requested_objects)//min_unit_objects),
                      max(0, (2_000_000-source_points-requested_points)//max(1, min_unit_points)))
    extra_targets = [0]*len(units)
    if parameters.get("fill"):
        for offset in range(extra_limit):
            extra_targets[offset % len(extra_targets)] += 1
    demands = [max(3, count) if len(units) == 1 else count for count in counts]
    solver_extras = list(extra_targets)
    solver_demands = [demand if _fits_paper(shape, allowed, area.width(), area.height()) else 0
                      for shape, demand in zip(shapes, demands)]
    solver_extras = [solver_extras[index] if solver_demands[index] else 0
                     for index in range(len(units))]

    postprocess_reserve = min(4.5, max(
        0.35, 0.15 + len(units)*0.2 + sum(len(shape) for shape in shapes)/5000))
    remaining_seconds = time_limit - (time.monotonic()-start) - postprocess_reserve
    if remaining_seconds < 1:
        return None
    solver_seconds = int(remaining_seconds)
    required_seconds = max(1, int(solver_seconds*.65)) if any(solver_extras) else solver_seconds
    fill_seconds = solver_seconds-required_seconds
    start_points = {f"unit-{index}": index for index in range(len(units))}
    outcomes = []
    if any(solver_demands):
        search_deadline = time.monotonic()+solver_seconds
        with ThreadPoolExecutor(max_workers=seed_count, thread_name_prefix="verona-nesting") as executor:
            futures = {executor.submit(_solve_seed, module, shapes, solver_demands, counts,
                                       solver_extras, allowed, holes, area.width(), area.height(),
                                       gap, required_seconds, fill_seconds, seed_start+offset): seed_start+offset
                       for offset in range(seed_count)}
            solutions = []
            for future, seed in futures.items():
                try:
                    solutions.append((seed, future.result()))
                except BaseException:
                    continue
        search_limited = time.monotonic() >= search_deadline-0.05
    else:
        solutions = [(seed_start+offset, None) for offset in range(seed_count)]
        search_limited = False
    for seed, solution in solutions:
        placement_groups = [[] for _ in units]
        for placed in [] if solution is None else solution:
            unit_id = start_points.get(str(placed.id))
            if unit_id is None:
                continue
            tx, ty = (float(value) for value in placed.translation)
            angle = float(placed.rotation)
            rotation = QTransform().rotate(angle)
            local_to_page = QTransform(rotation.m11(), rotation.m12(),
                rotation.m21(), rotation.m22(), tx+area.left(), ty+area.top())
            raw = local_to_page.map(unit_paths[unit_id])
            guard = local_to_page.map(unit_guards[unit_id])
            bounds = raw.boundingRect()
            if (bounds.left() < area.left()-1e-6 or bounds.top() < area.top()-1e-6
                    or bounds.right() > area.right()+1e-6
                    or bounds.bottom() > area.bottom()+1e-6):
                continue
            placement_groups[unit_id].append((unit_id, angle, tx, ty, bounds, raw, guard))

        placements = []
        for unit_id, candidates in enumerate(placement_groups):
            allowed_count = counts[unit_id] + (extra_targets[unit_id]
                if parameters.get("fill") else 0)
            candidates.sort(key=lambda entry: (entry[4].right(), entry[4].top(), entry[1]))
            placements.extend(candidates[:allowed_count])
        if not _verify_layout([entry[5] for entry in placements],
                              [entry[6] for entry in placements], fixed_paths,
                              marks, obstacle_guards, area, gap):
            continue
        copies_by_unit = [0]*len(units)
        for unit_id, *_rest in placements:
            copies_by_unit[unit_id] += 1
        requested_placed = [min(counts[index], copies_by_unit[index])
                            for index in range(len(units))]
        right = max((entry[4].right() for entry in placements), default=area.left())
        outcomes.append(((sum(requested_placed),
                         len(placements) if parameters.get("fill") else 0,
                         -(right-area.left()), -seed),
                         (seed, placements, copies_by_unit, requested_placed)))

    if not outcomes:
        return None
    seed, placements, copies_by_unit, requested_placed = max(outcomes, key=lambda row: row[0])[1]
    copied_items = list(remaining)
    selection, detail_placements = [], []
    for unit_id, angle, tx, ty, bounds, _raw, _guard in placements:
        copied = _copy_items([items[index] for index in units[unit_id]])
        remap_group_ids(copied)
        rotation = QTransform().rotate(angle)
        rotated_origin = rotation.map(origins[unit_id])
        _transform_placement(copied, rotation, rotated_origin,
                             tx+area.left(), ty+area.top())
        selection.extend(range(len(copied_items), len(copied_items)+len(copied)))
        copied_items.extend(copied)
        detail_placements.append({"unit": unit_id, "page": 0, "rotation_deg": angle,
            "bounds_mm": [bounds.left(), bounds.top(), bounds.right(), bounds.bottom()]})
    unplaced = [{"unit": unit_id, "copy": copy_number,
                 "source_indices": units[unit_id],
                 "objects": [items[index]["name"] for index in units[unit_id]]}
                for unit_id, count in enumerate(counts)
                for copy_number in range(requested_placed[unit_id]+1, count+1)]
    used_area = area.width()*area.height()
    usage = 100*_material_area(copied_items)/used_area if used_area else 0.0
    details = {"action": "pack_objects",
        "layout_method": "spyrrow continuous strip packing with static keep-outs",
        "shape_aware": parameters.get("shape_aware", False), "copies": len(placements),
        "candidate_checks": 0, "paper_utilization_percent": usage,
        "unplaced_count": len(unplaced), "unplaced": unplaced, "page_count": 1,
        "pages": [{"page": 0, "copies": len(placements),
                   "paper_utilization_percent": usage}], "new_pages": [], "page_limit": 100,
        "rotation_step_deg": rotation_step, "time_limit_seconds": time_limit,
        "limit_reached": search_limited,
        "deterministic": False, "registration_mark_clearance_mm": gap,
        "placements": detail_placements,
        "units": [{"source_indices": unit, "requested": count,
                   "requested_placed": requested_placed[index],
                   "copies": copies_by_unit[index]}
                  for index, (unit, count) in enumerate(zip(units, counts))],
        "engine": "spyrrow", "seed": seed}
    return _restore_packing_plan((copied_items, selection, details), pairing)

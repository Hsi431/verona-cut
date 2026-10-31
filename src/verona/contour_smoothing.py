"""Bounded geometric smoothing for imported cut contours."""

from __future__ import annotations

import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainterPath

Point = tuple[float, float]


def _distance_to_segment(point: Point, start: Point, end: Point) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length2))
    return math.hypot(point[0] - (start[0] + t * dx), point[1] - (start[1] + t * dy))


def _rdp(points: list[Point], tolerance: float) -> list[Point]:
    if len(points) < 3 or tolerance <= 0:
        return points[:]
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        distances = [_distance_to_segment(points[i], points[first], points[last]) for i in range(first + 1, last)]
        offset = max(range(len(distances)), key=distances.__getitem__)
        if distances[offset] > tolerance:
            index = first + 1 + offset
            keep.add(index)
            stack.extend(((first, index), (index, last)))
    return [point for i, point in enumerate(points) if i in keep]


def _turn(point: Point, previous: Point, following: Point) -> float:
    ax, ay = previous[0] - point[0], previous[1] - point[1]
    bx, by = following[0] - point[0], following[1] - point[1]
    denom = math.hypot(ax, ay) * math.hypot(bx, by)
    if denom == 0:
        return 0.0
    return math.degrees(math.acos(max(-1.0, min(1.0, (ax * bx + ay * by) / denom))))


def _simplify_closed(points: list[Point], tolerance: float) -> list[Point]:
    if len(points) < 4:
        return points[:]
    ring = points[:-1]
    # Two forward arcs avoid treating the closing seam as a straight chord.
    anchor = max(range(1, len(ring)), key=lambda i: (ring[i][0] - ring[0][0]) ** 2 + (ring[i][1] - ring[0][1]) ** 2)
    first_arc = _rdp(ring[:anchor + 1], tolerance)
    second_arc = _rdp(ring[anchor:] + [ring[0]], tolerance)
    simplified = first_arc + second_arc[1:-1]
    if len(simplified) < 3:
        return points[:]
    return simplified + [simplified[0]]


def _chaikin(points: list[Point], closed: bool, budget: float) -> list[Point]:
    core = points[:-1] if closed else points
    if len(core) < 3:
        return points[:]
    output: list[Point] = []
    count = len(core)
    start = 0 if closed else 1
    end = count if closed else count - 1
    if not closed:
        output.append(core[0])
    for i in range(start, end):
        previous, point, following = core[(i - 1) % count], core[i], core[(i + 1) % count]
        if 180.0 - _turn(point, previous, following) >= 50.0:
            output.append(point)
            continue
        for neighbor in (previous, following):
            edge = math.hypot(neighbor[0] - point[0], neighbor[1] - point[1])
            fraction = min(0.25, budget / edge) if edge else 0.0
            output.append((point[0] + (neighbor[0] - point[0]) * fraction,
                           point[1] + (neighbor[1] - point[1]) * fraction))
    if not closed:
        output.append(core[-1])
    else:
        output.append(output[0])
    return output


def _boundary_count(contours):
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    for contour in contours:
        if len(contour) > 2 and contour[0] == contour[-1]:
            path.moveTo(*contour[0])
            for point in contour[1:]:
                path.lineTo(*point)
            path.closeSubpath()
    return len(path.simplified().toSubpathPolygons())


def smooth_contours(contours: list[list[Point]], tolerance_mm: float) -> list[list[Point]]:
    """Reduce jagged cut paths while keeping every point within the tolerance budget.

    Input contours use world-millimetre coordinates. Open endpoints and closed
    ring winding are retained; a closed contour's final point remains an exact
    copy of its first point. The input is never modified.
    """
    if not math.isfinite(tolerance_mm) or tolerance_mm < 0:
        raise ValueError("tolerance_mm must be finite and non-negative")
    result: list[list[Point]] = []
    for contour in contours:
        original = [(float(x), float(y)) for x, y in contour]
        if any(not math.isfinite(value) for point in original for value in point):
            raise ValueError("contour coordinates must be finite")
        if tolerance_mm == 0 or len(original) < 3:
            result.append(original)
            continue
        closed = len(original) > 2 and original[0] == original[-1]
        if closed and min(max(p[axis] for p in original) - min(p[axis] for p in original)
                          for axis in (0, 1)) < tolerance_mm * 4:
            result.append(original)
            continue
        simplified = (_simplify_closed(original, tolerance_mm * 0.35) if closed
                      else _rdp(original, tolerance_mm * 0.35))
        for fraction in (0.25, 0.15, 0.10):
            simplified = _chaikin(simplified, closed, tolerance_mm * fraction)
        final = (_simplify_closed(simplified, tolerance_mm * 0.15) if closed
                 else _rdp(simplified, tolerance_mm * 0.15))
        # Reject collapsed or direction-reversed rings rather than altering topology.
        if closed:
            if len(final) < 4:
                final = original[:]
            else:
                area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(final, final[1:]))
                original_area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(original, original[1:]))
                if area == 0 or area * original_area <= 0:
                    final = original[:]
        if closed and final:
            final[-1] = final[0]
        result.append(final)
    # Boolean normalization detects newly split/merged boundaries without a
    # quadratic Python segment-intersection scan. Preserve unsafe compounds.
    if tolerance_mm and _boundary_count(result) != _boundary_count(contours):
        return [[(float(x), float(y)) for x, y in contour] for contour in contours]
    return result

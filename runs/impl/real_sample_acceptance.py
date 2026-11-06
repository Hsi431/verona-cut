"""Run the optional nesting engine against the three private LDS samples."""

import argparse
import json
import math
import statistics
import sqlite3
import time
from pathlib import Path
import re

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QImage, QPainter, QPainterPath, QPolygonF, QTransform

import verona.lds as lds_module
from verona.editor_actions import render_preview
from verona.geometry import contour_path
from verona.layers import cuttable, printable
from verona.packing import _footprint
from verona.nesting_engine import nesting_available, pack_with_nesting
from verona.printing import registration_geometry


SAMPLES = {
    "guide": "導遊嚕.lds",
    "christmas": "聖誕貼紙.lds",
    "mixed": "綜合.lds",
}


def _native_id(item):
    match = re.search(r"(\d+)$", item.get("name", ""))
    return match.group(1) if match else None


def _ring_bounds(ring):
    path = contour_path([ring])
    bounds = path.boundingRect()
    return [bounds.left(), bounds.top(), bounds.right(), bounds.bottom()]


def _bounds_error(first, second):
    return sum(abs(float(a)-float(b)) for a, b in zip(first, second))


def _sample_inputs(research_root, sample):
    data_root = research_root / "runs" / "real" / sample
    manifest = json.loads((data_root / "input.json").read_text(encoding="utf-8"))
    filename = research_root / "real-samples" / SAMPLES[sample]
    previous_limit = lds_module._MAX_DOCUMENT
    try:
        lds_module._MAX_DOCUMENT = max(previous_limit, 64 * 1024 * 1024)
        design = lds_module.load_lds(filename)
    finally:
        lds_module._MAX_DOCUMENT = previous_limit

    by_native_id = {_native_id(item): item for item in design.items if _native_id(item)}
    stickers, used_cut_contours = [], set()
    for entry in manifest["manual_stickers"]:
        native_ids = [str(value) for value in entry.get("native_ids", [])]
        if not native_ids or native_ids[-1] not in by_native_id:
            raise ValueError(f"{sample}/{entry['id']}: native image was not found")
        image_item = dict(by_native_id[native_ids[-1]])
        if len(native_ids) > 1 and native_ids[0] != native_ids[-1]:
            cut_item = by_native_id.get(native_ids[0])
            if cut_item is None:
                raise ValueError(f"{sample}/{entry['id']}: native cut path was not found")
            target = entry["manual_bounds_mm"]
            options = [(_bounds_error(_ring_bounds(ring), target), index, ring)
                       for index, ring in enumerate(cut_item.get("contours", []))
                       if (native_ids[0], index) not in used_cut_contours]
            if not options:
                raise ValueError(f"{sample}/{entry['id']}: native cut outline was already used")
            error, ring_index, ring = min(options)
            if error > .25:
                raise ValueError(f"{sample}/{entry['id']}: cut outline bounds differ by {error:.4f} mm")
            used_cut_contours.add((native_ids[0], ring_index))
            image_item["contours"] = [ring]
        image_item["name"] = entry["id"]
        if not (cuttable(image_item) or printable(image_item)):
            raise ValueError(f"{sample}/{entry['id']}: source sticker is not editable artwork")
        stickers.append(image_item)

    metadata = dict(design.metadata)
    conditions = manifest["comparison_conditions"]
    parameters = {
        "copies_per_unit": [1]*len(stickers),
        "gap_mm": conditions["gap_mm"],
        "margin_mm": conditions["edge_margin_mm"],
        "fill": False,
        "allow_rotate": True,
        "shape_aware": True,
        "rotation_step_deg": 90,
        "time_limit_seconds": 12,
        "new_page_for_unplaced": False,
    }
    return stickers, metadata, parameters, manifest


def _segments(contour):
    points = [(float(x), float(y)) for x, y in contour]
    if len(points) < 2:
        return []
    pairs = list(zip(points, points[1:]))
    if points[0] != points[-1]:
        pairs.append((points[-1], points[0]))
    return pairs


def _cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _point_segment(point, segment):
    (x, y), ((x1, y1), (x2, y2)) = point, segment
    dx, dy = x2-x1, y2-y1
    denominator = dx*dx+dy*dy
    amount = 0 if not denominator else max(0, min(1, ((x-x1)*dx+(y-y1)*dy)/denominator))
    return math.hypot(x-x1-amount*dx, y-y1-amount*dy)


def _segment_distance(first, second, threshold):
    (a, b), (c, d) = first, second
    if (max(min(a[0], b[0]), min(c[0], d[0]))
            > min(max(a[0], b[0]), max(c[0], d[0]))+threshold
            or max(min(a[1], b[1]), min(c[1], d[1]))
            > min(max(a[1], b[1]), max(c[1], d[1]))+threshold):
        return threshold+1
    ab_c, ab_d = _cross(a, b, c), _cross(a, b, d)
    cd_a, cd_b = _cross(c, d, a), _cross(c, d, b)
    on_ab = lambda p: (min(a[0], b[0])-1e-9 <= p[0] <= max(a[0], b[0])+1e-9
                       and min(a[1], b[1])-1e-9 <= p[1] <= max(a[1], b[1])+1e-9)
    on_cd = lambda p: (min(c[0], d[0])-1e-9 <= p[0] <= max(c[0], d[0])+1e-9
                       and min(c[1], d[1])-1e-9 <= p[1] <= max(c[1], d[1])+1e-9)
    if (ab_c*ab_d < 0 and cd_a*cd_b < 0
            or abs(ab_c) < 1e-9 and on_ab(c) or abs(ab_d) < 1e-9 and on_ab(d)
            or abs(cd_a) < 1e-9 and on_cd(a) or abs(cd_b) < 1e-9 and on_cd(b)):
        return 0
    return min(_point_segment(a, second), _point_segment(b, second),
               _point_segment(c, first), _point_segment(d, first))


def _inside(point, contours):
    x, y = point
    for contour in contours:
        points = [(float(px), float(py)) for px, py in contour]
        contained = False
        for (x1, y1), (x2, y2) in zip(points, points[1:]+points[:1]):
            if (y1 > y) != (y2 > y) and x < (x2-x1)*(y-y1)/(y2-y1)+x1:
                contained = not contained
        if contained:
            return True
    return False


class _SegmentIndex:
    def __init__(self, item):
        self.contours = item["contours"]
        self.segments = [edge for contour in self.contours for edge in _segments(contour)]
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("create virtual table edges using rtree(id,min_x,max_x,min_y,max_y)")
        self.connection.executemany("insert into edges values (?,?,?,?,?)",
            ((index, min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1]))
             for index, (a, b) in enumerate(self.segments)))

    def near(self, edge, threshold):
        (x1, y1), (x2, y2) = edge
        return self.connection.execute(
            "select id from edges where max_x>=? and min_x<=? and max_y>=? and min_y<=?",
            (min(x1, x2)-threshold, max(x1, x2)+threshold,
             min(y1, y2)-threshold, max(y1, y2)+threshold)).fetchall()


def _shape_distance(first, second, threshold, first_index, second_index):
    first_segments, second_segments = first_index.segments, second_index.segments
    if not first_segments or not second_segments:
        return threshold+1
    if (_inside(first_segments[0][0], second_index.contours)
            or _inside(second_segments[0][0], first_index.contours)):
        return 0
    for segment in first_segments:
        for (segment_id,) in second_index.near(segment, threshold):
            distance = _segment_distance(segment, second_segments[segment_id], threshold)
            if distance < threshold-1e-7:
                return distance
    return threshold+1


def _true_footprint(item):
    raw, _printed = _footprint([item], True)
    precise = QTransform.fromScale(20, 20).map(raw.simplified())
    contours = []
    for polygon in precise.toFillPolygons():
        points = [[float(point.x())/20, float(point.y())/20] for point in polygon]
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        if len(points) >= 3:
            contours.append(points)
    return {"name": item.get("name", "sticker"), "contours": contours}


def _verify_layout(items, metadata, gap, margin):
    registration = metadata.get("print_cut", {})
    left, top, right, bottom = registration["bounds"]
    dx, dy = registration.get("offset", (0, 0))
    bounds = (max(left, left-dx)+margin, max(top, top-dy)+margin,
              min(right, right-dx)-margin, min(bottom, bottom-dy)-margin)
    for item in items:
        bleed = item.get("bleed", {}).get("width_mm", 0)
        for contour in item["contours"]:
            for x, y in contour:
                if not (bounds[0]+bleed-1e-6 <= x <= bounds[2]-bleed+1e-6
                        and bounds[1]+bleed-1e-6 <= y <= bounds[3]-bleed+1e-6):
                    raise AssertionError(f"out of bounds: {item['name']}")
    indexes = [_SegmentIndex(item) for item in items]
    for index, first in enumerate(items):
        for other_index, second in enumerate(items[index+1:], index+1):
            required = (gap + first.get("bleed", {}).get("width_mm", 0)
                        + second.get("bleed", {}).get("width_mm", 0))
            distance = _shape_distance(first, second, required,
                                       indexes[index], indexes[other_index])
            if distance < required-1e-6:
                raise AssertionError(f"overlap/clearance {distance:.8f} < {required:.8f}")
    if registration.get("enabled"):
        marks = [dict(name="registration mark", contours=[ring+[ring[0]]])
                 for ring in registration_geometry(registration["bounds"],
                     stroke=registration.get("mark_stroke", 1),
                     short_length=registration.get("mark_short_length", 4))]
        mark_indexes = [_SegmentIndex(mark) for mark in marks]
        for item_index, item in enumerate(items):
            required = gap + item.get("bleed", {}).get("width_mm", 0)
            for mark_index, mark in enumerate(marks):
                distance = _shape_distance(item, mark, required,
                                           indexes[item_index], mark_indexes[mark_index])
                if distance < required-1e-6:
                    raise AssertionError(f"registration clearance {distance:.8f} < {required:.8f}")
        for index in indexes+mark_indexes:
            index.connection.close()
    else:
        for index in indexes:
            index.connection.close()
    return bounds


def _empty_rectangle(items, metadata, bounds, gap, step=.5):
    left, top, right, bottom = bounds
    nx, ny = int((right-left)/step), int((bottom-top)/step)
    clearance = gap + step*math.sqrt(2)/2
    image = QImage(nx, ny, QImage.Format.Format_Grayscale8)
    image.fill(255)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setTransform(QTransform(1/step, 0, 0, 1/step, -left/step, -top/step))
    for item in items:
        for contour in item["contours"]:
            painter.fillPath(contour_path([contour]), QBrush(Qt.GlobalColor.black))
    registration = metadata.get("print_cut", {})
    if registration.get("enabled"):
        for polygon in registration_geometry(registration["bounds"],
                stroke=registration.get("mark_stroke", 1),
                short_length=registration.get("mark_short_length", 4)):
            path = QPainterPath()
            path.addPolygon(QPolygonF([QPointF(*point) for point in polygon]))
            path.closeSubpath()
            painter.fillPath(path, QBrush(Qt.GlobalColor.black))
    painter.end()
    bits = np.frombuffer(image.constBits(), dtype=np.uint8,
                         count=image.bytesPerLine()*image.height())
    blocked = bits.reshape(image.height(), image.bytesPerLine())[:, :nx] < 128
    radius = math.ceil(clearance/step)
    padded = np.pad(blocked.astype(np.uint8), ((radius, radius), (radius, radius)))
    horizontal_sum = np.pad(np.cumsum(padded, axis=1, dtype=np.int32),
                            ((0, 0), (1, 0)))
    horizontal = (horizontal_sum[:, 2*radius+1:]
                  - horizontal_sum[:, :-(2*radius+1)]) > 0
    vertical_sum = np.pad(np.cumsum(horizontal, axis=0, dtype=np.int32),
                          ((1, 0), (0, 0)))
    blocked = (vertical_sum[2*radius+1:, :]
               - vertical_sum[:-(2*radius+1), :]) > 0
    heights = np.zeros(nx, dtype=np.int32)
    best_area = 0.0
    best = (0.0, 0.0, 0.0, 0.0)
    for row in range(ny):
        heights = np.where(blocked[row], 0, heights+1)
        stack = []
        for column in range(nx+1):
            height = int(heights[column]) if column < nx else 0
            start = column
            while stack and stack[-1][1] > height:
                left_column, old_height = stack.pop()
                area = old_height*(column-left_column)*step*step
                if area > best_area:
                    best_area = area
                    best = ((column-left_column)*step, old_height*step,
                            left+left_column*step, top+(row-old_height+1)*step)
                start = left_column
            if not stack or stack[-1][1] < height:
                stack.append((start, height))
    return {"width_mm": best[0], "height_mm": best[1], "area_mm2": best_area,
            "bounds_mm": [best[2], best[3], best[2]+best[0], best[3]+best[1]],
            "grid_mm": step, "conservative_clearance_buffer_mm": radius*step*math.sqrt(2)}


def run(research_root, output):
    if not nesting_available():
        raise RuntimeError("patched spyrrow wheel with holes= is unavailable")
    output.mkdir(parents=True, exist_ok=True)
    summaries = []
    for sample in SAMPLES:
        items, metadata, parameters, manifest = _sample_inputs(research_root, sample)
        started = time.monotonic()
        plan = pack_with_nesting(items, metadata, list(range(len(items))), parameters,
                                 seed_count=3, seed_start=20261106)
        elapsed = time.monotonic()-started
        row = {"sample": sample, "requested": len(items), "elapsed_seconds": elapsed,
               "time_limit_seconds": parameters["time_limit_seconds"]}
        if plan is None:
            row.update({"placed": 0, "geometry_valid": False, "error": "no verified seed"})
            summaries.append(row)
            continue
        result, selection, details = plan
        placed = [_true_footprint(result[index]) for index in selection]
        try:
            bounds = _verify_layout(placed, metadata, parameters["gap_mm"], parameters["margin_mm"])
            row["geometry_valid"] = True
        except AssertionError as error:
            row["geometry_valid"] = False
            row["verification_error"] = str(error)
            bounds = _verify_layout([], metadata, parameters["gap_mm"], parameters["margin_mm"])
        empty = _empty_rectangle(placed, metadata, bounds,
                                 parameters["gap_mm"])
        manual_path = research_root / "runs" / "marks" / "paper" / sample / "manual.json"
        manual = json.loads(manual_path.read_text(encoding="utf-8"))
        image_path = output / f"{sample}.png"
        render_preview(result, metadata, image_path)
        row.update({"placed": details["copies"], "unplaced": details["unplaced_count"],
                    "seed": details["seed"], "max_empty_rectangle": empty,
                    "manual_max_empty_rectangle": manual["max_empty_rectangle"],
                    "engine": details["engine"], "layout_png": str(image_path)})
        summaries.append(row)
        print(f"SAMPLE {sample} placed={row['placed']}/{row['requested']} "
              f"max_empty_mm2={empty['area_mm2']:.1f} "
              f"manual_mm2={manual['max_empty_rectangle']['area_mm2']:.1f} "
              f"elapsed={elapsed:.2f}s seed={details['seed']} "
              f"geometry={'PASS' if row['geometry_valid'] else 'FAIL'}")

    have_plans = [row for row in summaries if "max_empty_rectangle" in row]
    manual_areas = [row["manual_max_empty_rectangle"]["area_mm2"] for row in have_plans]
    engine_areas = [row["max_empty_rectangle"]["area_mm2"] for row in have_plans]
    summary = {"samples": summaries,
               "manual_median_empty_rectangle_mm2": statistics.median(manual_areas) if manual_areas else None,
               "engine_median_empty_rectangle_mm2": statistics.median(engine_areas) if engine_areas else None,
               "all_required_placed": all(row.get("placed") == row["requested"] for row in summaries),
               "all_geometry_valid": all(row.get("geometry_valid", False) for row in summaries)}
    summary["median_empty_not_below_manual"] = bool(have_plans) and (
        summary["engine_median_empty_rectangle_mm2"] >= summary["manual_median_empty_rectangle_mm2"])
    summary_path = output / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(f"MEDIAN_EMPTY engine={summary['engine_median_empty_rectangle_mm2']:.1f} "
          f"manual={summary['manual_median_empty_rectangle_mm2']:.1f} "
          f"pass={summary['median_empty_not_below_manual']}")
    print(f"SUMMARY {summary_path}")
    return (0 if summary["all_required_placed"] and summary["all_geometry_valid"]
            and summary["median_empty_not_below_manual"] else 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-root", type=Path,
        default=Path("/home/fnata_claw/research/verona-nesting-2026-11-06"))
    parser.add_argument("--output", type=Path,
        default=Path("/tmp/verona-smart-pack-evidence"))
    args = parser.parse_args()
    raise SystemExit(run(args.research_root, args.output))


if __name__ == "__main__":
    main()

"""Detached, bounded editing operations shared by the live editor MCP bridge."""
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF, QTransform

from .artwork import bounds_list, contour_path, draw_printed_item, item_bounds, validate_bleed
from .layers import cuttable, editable, printable
from .grouping import expand_selection, group_items, remap_group_ids, selection_units, ungroup_items
from .geometry import (copy_items as _copy_items, flatten_path as _flatten,
                       transform_item as _transform_item)
from .i18n import tr


EDIT_ACTION_PARAMETERS = {
    "transform": {"dx_mm", "dy_mm", "scale", "rotation_deg", "mirror_x", "mirror_y"},
    "duplicate": {"dx_mm", "dy_mm"}, "delete": set(), "set_cut": {"enabled"},
    "group": set(), "ungroup": set(), "layer_order": {"direction"},
    "smooth": {"tolerance_mm"}, "align": {"mode"}, "link_cut": {"contour_indices"},
    "boolean": {"mode"}, "fill_area": {"gap_mm", "margin_mm", "max_copies", "allow_rotate"},
    "bleed": {"width_mm", "color", "inner_mm", "mode"},
    "edit_text": {"text", "font_size_mm", "font_family", "font_style", "height_mm",
                   "letter_spacing_mm", "line_spacing_mm", "alignment"},
    "outline_text": set(),
}


def cutting_frame(metadata):
    registration = metadata.get("print_cut", {})
    if not registration.get("enabled"):
        return QRectF(0, 0, 300, 300)
    bounds = registration.get("bounds")
    if not bounds:
        raise ValueError("The Print & Cut document has no registration frame.")
    left, top, right, bottom = bounds
    dx, dy = registration.get("offset", (0, 0))
    # Both the original and adjusted cut must fit the frame.
    return QRectF(QPointF(max(left, left-dx), max(top, top-dy)),
                  QPointF(min(right, right-dx), min(bottom, bottom-dy)))


def editable_indices(items, indices):
    if (not isinstance(indices, list) or not indices
            or any(type(i) is not int or not 0 <= i < len(items) for i in indices)):
        raise ValueError("Choose a nonempty list of current object indices.")
    if any(items[i].get("registration_guide") for i in indices):
        raise ValueError("Registration guides cannot be edited through these tools.")
    indices = sorted(expand_selection(items, indices))
    if any(not editable(items[i]) for i in indices):
        raise ValueError("Unlock the selected layers before editing them.")
    return indices


def boolean_indices(items, indices):
    """Resolve the selected vector operands, expanding and filtering groups."""
    selected = editable_indices(items, indices)
    groups = {item_path[0] for index in selected
              if isinstance((item_path := items[index].get("group_path")), list)
              and item_path}
    vectors = []
    for index in selected:
        item = items[index]
        path = item.get("group_path")
        grouped = isinstance(path, list) and bool(path) and path[0] in groups
        if "image_data" in item:
            if grouped:
                continue
            raise ValueError("Images cannot be used in path operations.")
        if not item.get("contours"):
            if grouped:
                continue
            raise ValueError("Choose vector objects with closed contours for path operations.")
        vectors.append(index)
    if len(vectors) < 2:
        raise ValueError("Choose at least two vector objects for path operations.")
    return vectors


def finite_number(value, name, low, high):
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or not low <= value <= high):
        raise ValueError(f"{name} must be a finite number from {low} to {high}.")
    return float(value)


def build_edit(items, metadata, indices, action, parameters):
    if (action not in EDIT_ACTION_PARAMETERS or not isinstance(parameters, dict)
            or set(parameters) - EDIT_ACTION_PARAMETERS[action]):
        raise ValueError("Unknown edit action or parameters.")
    selected = (boolean_indices(items, indices) if action == "boolean"
                else editable_indices(items, indices))
    if action == "bleed":
        selected = sorted(set(indices))
    if action == "link_cut":
        # Ownership changes only for the explicit pair, even inside a group.
        selected = sorted(set(indices))
        if len(selected) != 2:
            raise ValueError("Choose one image and one separate cutting-path object.")
    result = _copy_items(items)
    details = {"action": action, "source_indices": selected}
    bounds = item_bounds(items[selected[0]], printed=action == "fill_area")
    for index in selected[1:]:
        bounds = bounds.united(item_bounds(items[index], printed=action == "fill_area"))

    if action == "edit_text":
        if len(selected) != 1:
            raise ValueError(tr("請選擇一個可編輯文字物件。"))
        index = selected[0]
        item = result[index]
        old_data = item.get("text_data")
        if (not isinstance(old_data, dict) or not isinstance(item.get("text_source_contours"), list)
                or "image_data" in item):
            raise ValueError(tr("所選物件不是可編輯文字。"))
        from .text_tool import (make_text_data, render_legacy_text, render_text_path,
                                text_transform, transform_text_contours)
        content = parameters.get("text", old_data.get("content"))
        if not isinstance(content, str) or not content.strip() or len(content) > 1024:
            raise ValueError(tr("文字必須包含 1–1024 個字元且不能只有空白。"))
        family = parameters.get("font_family", old_data.get("font_family"))
        style = parameters.get("font_style", old_data.get("font_style", ""))
        if not isinstance(family, str) or not family.strip() or len(family) > 256:
            raise ValueError(tr("請從已安裝字型中選擇字型。"))
        if not isinstance(style, str) or len(style) > 256:
            raise ValueError(tr("請選擇字型樣式。"))
        spacing = finite_number(parameters.get("letter_spacing_mm", old_data.get("letter_spacing_mm", 0)),
                                "letter_spacing_mm", -5, 50)
        transform = text_transform(item["text_source_contours"], item["contours"])
        if "font_size_mm" in parameters and "height_mm" not in parameters:
            size = finite_number(parameters["font_size_mm"], "font_size_mm", .1, 100)
            source_path, data, _resolved = render_legacy_text(
                content, family, size, *old_data.get("origin_mm", (0, 0)),
                style=style, letter_spacing_mm=spacing)
        else:
            height = finite_number(parameters.get("height_mm", old_data.get("height_mm")),
                                   "height_mm", .1, 100)
            line_spacing = finite_number(parameters.get(
                "line_spacing_mm", old_data.get("line_spacing_mm", height * 1.2)),
                "line_spacing_mm", 0, 300)
            alignment = parameters.get("alignment", old_data.get("alignment", "left"))
            if alignment not in ("left", "center", "right"):
                raise ValueError(tr("文字對齊必須是 left、center 或 right。"))
            data = make_text_data(content, family, style, height, spacing, line_spacing,
                                  alignment, tuple(old_data.get("origin_mm", (0, 0))))
            source_path = render_text_path(data)
        source_contours = _flatten(source_path)
        outlines = transform_text_contours(source_contours, transform)
        if not outlines:
            raise ValueError(tr("文字必須包含可顯示的字元。"))
        item["text_data"] = data
        item["text_source_contours"] = source_contours
        item["contours"] = outlines
        details.update(text_index=index, content_length=len(content),
                       cut_geometry_changed=outlines != items[index]["contours"])
        selected = [index]
    elif action == "outline_text":
        text_indices = [index for index in selected if "text_data" in result[index]]
        if not text_indices:
            raise ValueError(tr("請選擇可編輯文字物件再轉成輪廓。"))
        for index in text_indices:
            result[index].pop("text_data", None)
            result[index].pop("text_source_contours", None)
        details["outlined_objects"] = len(text_indices)
        selected = text_indices
    elif action == "fill_area":
        if type(parameters.get("allow_rotate", False)) is not bool:
            raise ValueError("allow_rotate must be a boolean.")
        if parameters.get("allow_rotate"):
            candidates, errors = [], []
            for angle in (0, 90):
                rotated = _copy_items(items)
                if angle:
                    center = bounds.center()
                    transform = QTransform()
                    transform.translate(center.x(), center.y())
                    transform.rotate(angle)
                    transform.translate(-center.x(), -center.y())
                    for index in selected:
                        _transform_item(rotated[index], transform)
                try:
                    candidate = build_edit(rotated, metadata, indices, "fill_area",
                                           {**parameters, "allow_rotate": False})
                    candidate[2]["rotation_deg"] = angle
                    candidates.append(candidate)
                except ValueError as error:
                    errors.append(str(error))
            if not candidates:
                raise ValueError(errors[0])
            best = max(candidates, key=lambda candidate: candidate[2]["copies"])
            best[2]["layout_method"] = "rectangular grid, choosing whole-pattern 0/90 degrees; not optimal nesting"
            return best

    if action == "boolean":
        from .path_ops import boolean_contours
        base_index = selected[0]
        contours = boolean_contours([items[index] for index in selected], parameters.get("mode"))
        result[base_index]["contours"] = contours
        result[base_index].pop("node_paths", None)
        # The welded outline is no longer the typeset text it came from.
        result[base_index].pop("text_data", None)
        result[base_index].pop("text_source_contours", None)
        result = [item for index, item in enumerate(result)
                  if index == base_index or index not in selected]
        details.update(mode=parameters["mode"], operands=len(selected),
                       result_contours=len(contours), precision_mm=.01)
        selected = [base_index]
    elif action in ("transform", "duplicate"):
        dx = finite_number(parameters.get("dx_mm", 10 if action == "duplicate" else 0), "dx_mm", -300, 300)
        dy = finite_number(parameters.get("dy_mm", 10 if action == "duplicate" else 0), "dy_mm", -300, 300)
        transform = QTransform.fromTranslate(dx, dy)
        if action == "transform":
            scale = finite_number(parameters.get("scale", 1), "scale", .001, 1000)
            angle = finite_number(parameters.get("rotation_deg", 0), "rotation_deg", -360, 360)
            for name in ("mirror_x", "mirror_y"):
                if type(parameters.get(name, False)) is not bool:
                    raise ValueError(f"{name} must be a boolean.")
            center = bounds.center()
            transform.translate(center.x(), center.y())
            transform.rotate(angle)
            transform.scale(-scale if parameters.get("mirror_x") else scale,
                            -scale if parameters.get("mirror_y") else scale)
            transform.translate(-center.x(), -center.y())
        else:
            copies = _copy_items([items[i] for i in selected])
            remap_group_ids(copies)
            selected = list(range(len(result), len(result) + len(copies)))
            result.extend(copies)
        for index in selected:
            _transform_item(result[index], transform)
    elif action == "delete":
        result = [item for i, item in enumerate(result) if i not in selected]
        selected = []
    elif action == "layer_order":
        direction = parameters.get("direction")
        if type(direction) is not int or direction not in (-2, -1, 1, 2):
            raise ValueError("direction must be -2, -1, 1 or 2.")
        order = list(range(len(result)))
        if direction > 1:
            order = [index for index in order if index not in selected] + [
                index for index in order if index in selected]
        elif direction < -1:
            order = [index for index in order if index in selected] + [
                index for index in order if index not in selected]
        else:
            positions = range(len(order) - 2, -1, -1) if direction > 0 else range(1, len(order))
            for position in positions:
                neighbor = position + (1 if direction > 0 else -1)
                if order[position] in selected and order[neighbor] not in selected:
                    order[position], order[neighbor] = order[neighbor], order[position]
        result = [result[index] for index in order]
        selected = [new for new, old in enumerate(order) if old in selected]
    elif action == "set_cut":
        enabled = parameters.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean.")
        for index in selected:
            result[index]["cut"] = enabled
    elif action == "smooth":
        from .contour_smoothing import smooth_contours
        tolerance = finite_number(parameters.get("tolerance_mm"), "tolerance_mm", 0, .5)
        changed = []
        for index in selected:
            item = result[index]
            if not item.get("cut", True) or not item.get("contours"):
                continue
            contours = smooth_contours(item["contours"], tolerance)
            if contours == item["contours"]:
                continue
            item["contours"] = contours
            if item.get("node_paths"):
                item["node_paths"] = {key: path for key, path in item["node_paths"].items()
                    if contours[int(key)] == items[index]["contours"][int(key)]}
            changed.append(index)
        details.update(tolerance_mm=tolerance, changed_indices=changed,
                       cut_geometry_changed=bool(changed))
    elif action in ("group", "ungroup"):
        operation = group_items if action == "group" else ungroup_items
        if not operation(result, selected):
            raise ValueError("This selection has no applicable grouping change.")
    elif action == "link_cut":
        images = [i for i in selected if "image_data" in result[i]]
        if len(images) != 1:
            raise ValueError("Choose one image and one separate cutting-path object.")
        image_index = images[0]
        source_index = next(i for i in selected if i != image_index)
        source = result[source_index]
        if source.get("print_style"):
            raise ValueError("Choose a cut-only vector; duplicate or remove its print style before attaching.")
        chosen = parameters.get("contour_indices", list(range(len(source["contours"]))))
        if (not isinstance(chosen, list) or not chosen
                or any(type(i) is not int or not 0 <= i < len(source["contours"]) for i in chosen)):
            raise ValueError("Choose current contour indices from the cutting-path object.")
        chosen = sorted(set(chosen))
        image = result[image_index]
        details.update(transferred_contours=len(chosen),
                       replaced_image_contours=len(image["contours"]),
                       image_index_before=image_index, source_index_before=source_index)
        image["contours"] = [source["contours"][i] for i in chosen]
        image.pop("node_paths", None)
        if source.get("node_paths"):
            paths = source.pop("node_paths")
            image["node_paths"] = {str(new): paths[str(old)] for new, old in enumerate(chosen) if str(old) in paths}
            remaining = [i for i in range(len(source["contours"])) if i not in chosen]
            source["node_paths"] = {str(new): paths[str(old)] for new, old in enumerate(remaining) if str(old) in paths}
        image["cut"] = True
        source["contours"] = [contour for i, contour in enumerate(source["contours"]) if i not in chosen]
        details["source_remaining_contours"] = len(source["contours"])
        if not source["contours"]:
            result.pop(source_index)
            if source_index < image_index:
                image_index -= 1
        selected = [image_index]
        details["image_index_after"] = image_index
    elif action == "align":
        from .alignment import alignment_offsets
        units = selection_units(result, selected)
        boxes = []
        for unit in units:
            box = item_bounds(result[unit[0]])
            for index in unit[1:]:
                box = box.united(item_bounds(result[index]))
            boxes.append(bounds_list(box))
        mode = parameters.get("mode")
        if len(units) < (3 if mode in ("distribute_h", "distribute_v") else 2):
            raise ValueError("Alignment needs two independent units; distribution needs three.")
        offsets = alignment_offsets(boxes, mode)
        for unit, (dx, dy) in zip(units, offsets):
            for index in unit:
                _transform_item(result[index], QTransform.fromTranslate(dx, dy))
        details.update(mode=mode, units=len(units))
    elif action == "bleed":
        width = finite_number(parameters.get("width_mm"), "width_mm", 0, 5)
        inner = finite_number(parameters.get("inner_mm", 0), "inner_mm", 0, 1)
        color = parameters.get("color")
        for index in selected:
            item = result[index]
            if "image_data" not in item:
                raise ValueError("Bleed requires image objects; choose the image indices explicitly.")
            resolved = color
            if color == "source":
                if "source_color" not in item:
                    raise ValueError(f"Object {index} has no source color. Specify #RRGGBB.")
                resolved = f"#{item['source_color'] & 0xffffff:06x}"
            bleed = validate_bleed({"width_mm": width, "color": resolved, "inner_mm": inner,
                                    "mode": parameters.get("mode", "solid")})
            if width or inner:
                item["bleed"] = bleed
                if width and bleed.get("mode") == "edge":
                    from .edge_bleed import edge_bleed_image
                    edge_bleed_image(item)
                    details["edge_layer_limit_px"] = 1200
            else:
                item.pop("bleed", None)
        details["cut_geometry_changed"] = False
    elif action == "fill_area":
        gap = finite_number(parameters.get("gap_mm", 2), "gap_mm", 0, 300)
        margin = finite_number(parameters.get("margin_mm", 0), "margin_mm", 0, 150)
        maximum = parameters.get("max_copies", 1000)
        if type(maximum) is not int or not 1 <= maximum <= 1000:
            raise ValueError("max_copies must be an integer from 1 to 1000.")
        # An invisible inset keeps floating-point translations off exact edges.
        area = cutting_frame(metadata).adjusted(margin+1e-6, margin+1e-6,
                                                 -margin-1e-6, -margin-1e-6)
        if min(bounds.width(), bounds.height(), area.width(), area.height()) <= 0:
            raise ValueError("The pattern and remaining cutting area must have positive dimensions.")
        columns = max(0, math.floor((area.width()+gap) / (bounds.width()+gap)))
        rows = max(0, math.floor((area.height()+gap) / (bounds.height()+gap)))
        remaining = [item for i, item in enumerate(result) if i not in selected]
        obstacles = [item_bounds(item, printed=True).adjusted(-gap/2, -gap/2, gap/2, gap/2)
                     for item in remaining if cuttable(item) or printable(item)]
        pattern = [items[i] for i in selected]
        # Printed copies keep the same gap from the registration marks as pack_objects.
        marks = QPainterPath()
        registration = metadata.get("print_cut", {})
        if registration.get("enabled") and any("image_data" in item or item.get("print_style")
                                               for item in pattern):
            from .printing import registration_geometry, registration_profile
            stroke, short = registration_profile(registration)
            for polygon in registration_geometry(registration["bounds"], stroke=stroke, short_length=short):
                marks.addPolygon(QPolygonF([QPointF(*point) for point in polygon]))
                marks.closeSubpath()
        points_per_copy = sum(len(contour) for item in pattern for contour in item["contours"])
        remaining_points = sum(len(contour) for item in remaining for contour in item["contours"])
        capacity = min(maximum, (2000-len(remaining)) // len(pattern),
                       (2_000_000-remaining_points) // max(1, points_per_copy))
        selected = []
        count = 0
        for row in range(rows):
            for column in range(columns):
                if count >= capacity:
                    break
                x, y = area.left()+column*(bounds.width()+gap), area.top()+row*(bounds.height()+gap)
                footprint = QRectF(x-gap/2, y-gap/2, bounds.width()+gap, bounds.height()+gap)
                if any(footprint.intersects(obstacle) for obstacle in obstacles):
                    continue
                if marks.intersects(footprint.adjusted(-gap/2, -gap/2, gap/2, gap/2)):
                    continue
                copies = _copy_items(pattern)
                remap_group_ids(copies)
                transform = QTransform.fromTranslate(x-bounds.left(), y-bounds.top())
                for item in copies:
                    _transform_item(item, transform)
                selected.extend(range(len(remaining), len(remaining)+len(copies)))
                remaining.extend(copies)
                count += 1
            if count >= capacity:
                break
        if not count:
            raise ValueError("No copy fits without overlapping the other cutting objects.")
        result = remaining
        details.update(copies=count, grid_columns=columns, grid_rows=rows,
                       grid_candidates=columns*rows, limit_reached=count >= capacity,
                       effective_frame_mm=bounds_list(area), gap_mm=gap,
                       layout_method="rectangular grid, fixed orientation; not optimal nesting")
        details["rotation_deg"] = 0

    if (len(result) > 2000
            or sum(len(contour) for item in result for contour in item["contours"]) > 2_000_000):
        raise ValueError("This edit exceeds the 2000-object / 2-million-cut-point limit.")
    return result, selected, details


def render_preview(items, metadata, filename):
    from .printing import draw_registration_marks
    image = QImage(900, 900, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.scale(3, 3)
    try:
        for item in items:
            draw_printed_item(painter, item)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor("#e5484d"), 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        for item in items:
            if cuttable(item):
                painter.drawPath(contour_path(item["contours"]))
        registration = metadata.get("print_cut", {})
        if registration.get("enabled") and registration.get("bounds"):
            draw_registration_marks(painter, registration["bounds"], registration)
    finally:
        painter.end()
    if not image.save(str(filename)):
        raise OSError("Could not write the editor preview.")

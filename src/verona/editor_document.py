"""Detached document changes for the editor protocol; no device operations."""
import copy
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QFont, QFontInfo, QImageReader, QPainterPath, QTransform

from .editor_actions import build_edit, cutting_frame, finite_number


def build_document_edit(items, metadata, settings, selection, action, parameters):
    from .ui import _copy_items, _flatten, _transform_item
    from .layers import editable
    if action in ("vector_print", "edit_path", "retrace_image", "pack_objects"):
        if any(not editable(items[i]) for i in selection):
            raise ValueError("Unlock the selected layers before editing them.")
    metadata, settings = copy.deepcopy(metadata), dict(settings)
    if action == "vector_print":
        from .artwork import validate_print_style
        if not selection or any("image_data" in items[i] or items[i].get("registration_guide") for i in selection):
            raise ValueError("Choose explicit ordinary vector object indices.")
        style = validate_print_style(parameters)
        result = _copy_items(items)
        for index in selection:
            if style["fill"] or style["stroke"]:
                result[index]["print_style"] = dict(style)
            else:
                result[index].pop("print_style", None)
        return result, metadata, settings, selection, {"action": action, "requires_reprint": result != items,
            "cut_geometry_changed": False, "objects": len(selection)}
    if action == "edit_path":
        from .nodes import edit_path
        if (len(selection) != 1 or not isinstance(parameters, dict)
                or set(parameters) != {"contour_index", "path"}):
            raise ValueError("Choose one object, contour_index and path.")
        result = _copy_items(items)
        index = selection[0]
        result[index] = edit_path(items[index], parameters["contour_index"], parameters["path"])
        if sum(len(c) for item in result for c in item["contours"]) > 2_000_000:
            raise ValueError("This edit exceeds the 2-million-cut-point limit.")
        return result, metadata, settings, selection, {"action": action,
            "object_index": index, "contour_index": parameters["contour_index"],
            "original_pixels_changed": False, "curve_tolerance_mm": .025}
    if action == "path_order":
        from .path_order import analyze_path_order
        if not isinstance(parameters, dict) or set(parameters) != {"enabled"}:
            raise ValueError("Choose enabled=true/false for path ordering.")
        _, details = analyze_path_order(items, metadata, parameters["enabled"])
        if parameters["enabled"]:
            metadata["optimize_paths"] = True
        else:
            metadata.pop("optimize_paths", None)
        return _copy_items(items), metadata, settings, selection, {"action": action, **details}
    if action == "pack_objects":
        from .packing import pack_items
        result, selection, details = pack_items(items, metadata, selection, parameters)
        return result, metadata, settings, selection, details
    if action not in ("add_shape", "add_text", "configure", "import_artwork", "retrace_image"):
        result, selection, details = build_edit(items, metadata, selection, action, parameters)
        return result, metadata, settings, selection, details
    if not isinstance(parameters, dict):
        raise ValueError("Edit parameters must be an object.")
    result = _copy_items(items)
    details = {"action": action}
    if action == "retrace_image":
        from .mask_edit import retrace_item
        if len(selection) != 1:
            raise ValueError("Choose one explicit image index.")
        index = selection[0]
        result[index] = retrace_item(items[index], parameters)
        if sum(len(c) for item in result for c in item["contours"]) > 2_000_000:
            raise ValueError("This edit exceeds the 2-million-cut-point limit.")
        details.update(image_index=index, original_pixels_changed=False)
        return result, metadata, settings, selection, details
    if action == "import_artwork":
        if set(parameters) - {"path", "x_mm", "y_mm", "tracing", "tolerance_mm", "representation", "page", "dpi"}:
            raise ValueError("Unknown import parameters.")
        if not isinstance(parameters.get("path"), str) or not parameters["path"]:
            raise ValueError("Choose an artwork file.")
        if "tracing" in parameters and not isinstance(parameters["tracing"], dict):
            raise ValueError("tracing must be an options object.")
        registration_before = copy.deepcopy(metadata.get("print_cut"))
        source = Path(parameters["path"]).expanduser().resolve()
        if source.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("Agent-imported artwork files are limited to 64 MiB.")
        representation = parameters.get("representation", "vector" if source.suffix.lower() == ".svg" else "raster")
        if representation not in ("vector", "raster") or representation == "vector" and source.suffix.lower() != ".svg":
            raise ValueError("Vector import requires SVG; choose raster for PDF/artwork.")
        if representation == "vector" and any(key in parameters for key in ("page", "dpi")):
            raise ValueError("Page/DPI apply only to rendered artwork.")
        if source.suffix.lower() == ".svg" and representation == "vector":
            from .core import load_svg
            if parameters.get("tracing"):
                raise ValueError("Raster tracing options do not apply to SVG paths.")
            tolerance = finite_number(parameters.get("tolerance_mm", .05), "tolerance_mm", .005, 1)
            imported = [{"name": source.stem, "contours": load_svg(source, tolerance), "cut": True}]
        elif source.suffix.lower() in (".pdf", ".svg", ".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"):
            from .raster_import import build_raster_items, raster_options
            if "tolerance_mm" in parameters:
                raise ValueError("tolerance_mm applies to SVG; use tracing.tolerance for raster images.")
            defaults = {}
            if source.suffix.lower() in (".pdf", ".svg"):
                from .document_import import render_artwork
                image, defaults, rendering = render_artwork(source, parameters.get("page", 1), parameters.get("dpi", 300))
                details.update(rendering)
            else:
                if any(key in parameters for key in ("page", "dpi")):
                    raise ValueError("Page/DPI apply only to PDF/SVG rendering.")
                reader = QImageReader(str(source))
                reader.setAutoTransform(True)
                if reader.size().width() * reader.size().height() > 16_000_000:
                    raise ValueError("Image exceeds 16 million pixels.")
                image = reader.read()
            changes = parameters.get("tracing", {})
            if "width_mm" in changes or "height_mm" in changes:
                defaults = {}
            options = raster_options(image, {**defaults, **changes})
            imported = build_raster_items(image, source.stem, options)
            if options["mode"] in ("print_cut", "background"):
                registration = metadata.get("print_cut", {})
                if not registration.get("bounds"):
                    registration = {"paper_width": 210., "paper_height": 297.,
                                    "bounds": [6., 6., 204., 291.],
                                    "enabled": options["mode"] == "print_cut"}
                    metadata["print_cut"] = registration
                if options["mode"] == "print_cut":
                    registration["enabled"] = True
        else:
            raise ValueError("Import a plain SVG or a supported raster image.")
        if not imported:
            raise ValueError("Tracing found no usable contours. Adjust the mask settings.")
        from .artwork import item_bounds
        box = item_bounds(imported[0])
        for item in imported[1:]:
            box = box.united(item_bounds(item))
        frame = cutting_frame(metadata)
        x = finite_number(parameters.get("x_mm", frame.center().x()-box.width()/2), "x_mm", -300, 600)
        y = finite_number(parameters.get("y_mm", frame.center().y()-box.height()/2), "y_mm", -300, 600)
        for item in imported:
            _transform_item(item, QTransform.fromTranslate(x-box.left(), y-box.top()))
        selection = list(range(len(result), len(result)+len(imported)))
        result.extend(imported)
        if len(result) > 2000 or sum(len(c) for item in result for c in item["contours"]) > 2_000_000:
            raise ValueError("This edit exceeds the 2000-object / 2-million-cut-point limit.")
        details.update(source=str(source), imported_objects=len(imported),
                       requires_reprint=registration_before != metadata.get("print_cut"))
        return result, metadata, settings, selection, details
    if action == "configure":
        if not parameters or set(parameters) - {"registration", "cut_settings"}:
            raise ValueError("Choose registration and/or cut_settings.")
        cut = parameters.get("cut_settings", {})
        if not isinstance(cut, dict) or set(cut) - {"custom", "force", "speed", "tool"}:
            raise ValueError("Only custom, force, speed and tool are editable cut settings.")
        settings.update(cut)
        if "registration" in parameters:
            changes = parameters["registration"]
            allowed = {"enabled", "paper_width", "paper_height", "margins",
                       "mark_stroke", "mark_short_length", "offset", "printer_name"}
            if not isinstance(changes, dict) or not changes or set(changes) - allowed:
                raise ValueError("Unknown or empty registration settings.")
            old = metadata.get("print_cut", {})
            registration = {"enabled": True, "paper_width": 210., "paper_height": 297.,
                            "bounds": [6., 6., 204., 291.], **old}
            width = finite_number(changes.get("paper_width", registration["paper_width"]),
                                  "paper_width", 1, 300)
            height = finite_number(changes.get("paper_height", registration["paper_height"]),
                                   "paper_height", 1, 300)
            bounds = registration.get("bounds")
            margins = changes.get("margins", [bounds[0], bounds[1],
                registration["paper_width"]-bounds[2], registration["paper_height"]-bounds[3]]
                if bounds else [6.] * 4)
            if not isinstance(margins, list) or len(margins) != 4:
                raise ValueError("margins must be [left, top, right, bottom] in mm.")
            margins = [finite_number(value, "margin", 0, 300) for value in margins]
            registration.update({key: value for key, value in changes.items() if key != "margins"})
            registration.update(paper_width=width, paper_height=height,
                bounds=[margins[0], margins[1], width-margins[2], height-margins[3]])
            from .printing import registration_profile
            stroke, _ = registration_profile(registration)
            if "printer_name" in changes:
                from .printing import printer_profiles
                name = changes["printer_name"]
                if not isinstance(name, str) or not name:
                    raise ValueError("Choose an installed printer by name.")
                profiles = [profile for profile in printer_profiles(width, height) if profile["name"] == name]
                if len(profiles) != 1 or not profiles[0]["paper_accepted"]:
                    raise ValueError("The chosen printer is unavailable or rejected the requested paper size.")
                registration["printer_margins"] = profiles[0]["margins_mm"]
                if "margins" not in changes:
                    margins = [value+stroke/2 for value in registration["printer_margins"]]
                    registration["bounds"] = [margins[0], margins[1], width-margins[2], height-margins[3]]
            minimums = registration.get("printer_margins", [0.] * 4)
            if any(margin < minimum + stroke/2 for margin, minimum in zip(margins, minimums)):
                raise ValueError("Marks must remain inside the printer margins, including half their stroke.")
            metadata["print_cut"] = registration
            # Cut adjustment changes the tool path, not the printed artwork.
            details["requires_reprint"] = any(old.get(key) != registration.get(key)
                for key in ("enabled", "paper_width", "paper_height", "bounds",
                            "mark_stroke", "mark_short_length"))
        return result, metadata, settings, selection, details

    allowed = ({"shape", "x_mm", "y_mm", "width_mm", "height_mm", "name"}
               if action == "add_shape" else
               {"text", "x_mm", "y_mm", "font_size_mm", "font_family", "name"})
    if set(parameters) - allowed:
        raise ValueError("Unknown creation parameters.")
    x = finite_number(parameters.get("x_mm", 20), "x_mm", -300, 600)
    y = finite_number(parameters.get("y_mm", 20), "y_mm", -300, 600)
    path = QPainterPath()
    if action == "add_shape":
        shape = parameters.get("shape")
        if shape not in ("rectangle", "ellipse"):
            raise ValueError("shape must be rectangle or ellipse.")
        width = finite_number(parameters.get("width_mm", 40), "width_mm", .01, 300)
        height = finite_number(parameters.get("height_mm", 25), "height_mm", .01, 300)
        (path.addRect if shape == "rectangle" else path.addEllipse)(QRectF(x, y, width, height))
        name = parameters.get("name", shape)
    else:
        text = parameters.get("text")
        family = parameters.get("font_family", "Noto Sans")
        if not isinstance(text, str) or not text.strip() or len(text) > 1024:
            raise ValueError("text must contain 1–1024 characters with visible content.")
        if not isinstance(family, str) or not family.strip() or len(family) > 256:
            raise ValueError("Choose a font family.")
        size = finite_number(parameters.get("font_size_mm", 10), "font_size_mm", .1, 100)
        font = QFont(family)
        font.setPixelSize(1000)
        path.addText(QPointF(0, 0), font, text)
        path = QTransform.fromScale(size/1000, size/1000).map(path)
        path.translate(x, y+size)
        name = parameters.get("name", text[:80])
        details.update(requested_font=family, resolved_font=QFontInfo(font).family(),
                       font_size_mm=size)
    if not isinstance(name, str) or not name.strip() or len(name) > 1024:
        raise ValueError("Choose an object name of 1–1024 characters.")
    contours = _flatten(path)
    if not contours:
        raise ValueError("The chosen text or shape has no outline.")
    result.append({"name": name, "contours": contours, "cut": True})
    if len(result) > 2000 or sum(len(c) for item in result for c in item["contours"]) > 2_000_000:
        raise ValueError("This edit exceeds the 2000-object / 2-million-cut-point limit.")
    return result, metadata, settings, [len(result)-1], details

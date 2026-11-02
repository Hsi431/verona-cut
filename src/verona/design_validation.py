"""Saved design validation shared by UI and backend consumers."""

from __future__ import annotations

import binascii
import ipaddress
import math
from typing import Any

from .geometry import copy_items as _copy_items
from .i18n import tr
from .document_state import default_cut_settings
from .input_safety import MAX_POINTS
from .view_constants import PAGE_HEIGHT_MM, PAGE_WIDTH_MM


def _qoi_image(encoded: str):
    """Decode image data lazily so backend validation does not import UI code."""
    from .raster_import import decode_embedded_image
    return decode_embedded_image(encoded)


def _trust_key(item: Any):
    try:
        key = item["name"], len(item["contours"]), tuple(item["contours"][0][0])
        hash(key)
        return key
    except (TypeError, KeyError, IndexError):
        return None


def validate_document(data: Any, trusted_items=()) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(data, dict) or data.get("format") not in ("verona", "juliet-design-linux"):
        raise ValueError(tr("這不是 Verona 設計檔。"))
    if data.get("version") != 1 or data.get("units") != "mm":
        raise ValueError(tr("設計檔版本或座標單位不支援。"))
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise ValueError(tr("設計檔缺少 items 清單。"))
    from .input_safety import check_geometry_budget
    check_geometry_budget(raw_items)
    raw_points = 0
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        for key in ("contours", "image_clip", "text_source_contours"):
            contours = raw_item.get(key)
            if isinstance(contours, list):
                raw_points += sum(len(contour) for contour in contours if isinstance(contour, list))
                if raw_points > MAX_POINTS:
                    raise ValueError(tr("展開點數超過 {limit} 點，請簡化路徑或拆分文件後重試。",
                                        limit=f"{MAX_POINTS:,}"))
    items = []
    trusted = {}
    text_source_points = 0
    for item in trusted_items:
        trusted.setdefault(_trust_key(item), []).append(item)
    for number, raw_item in enumerate(raw_items, 1):
        key = _trust_key(raw_item) if trusted else None
        if key is not None and any(raw_item == known for known in trusted.get(key, ())):
            # Unchanged objects already in the editor skip image decoding,
            # mask and edge-bleed checks; only edited objects are re-validated.
            items.append(_copy_items([raw_item])[0])
            continue
        if not isinstance(raw_item, dict) or not isinstance(raw_item.get("name"), str):
            raise ValueError(tr("第 {number} 個物件格式錯誤。", number=number))
        contours = raw_item.get("contours")
        if not isinstance(contours, list) or not contours:
            raise ValueError(tr("第 {number} 個物件沒有路徑。", number=number))
        safe_item = {"name": raw_item["name"],
                     "contours": validate_contours(contours, number)}
        if "text_data" in raw_item or "text_source_contours" in raw_item:
            text_data = raw_item.get("text_data")
            if (not isinstance(text_data, dict) or "text_source_contours" not in raw_item
                    or "image_data" in raw_item or raw_item.get("registration_guide")):
                raise ValueError(tr("第 {number} 個物件的文字資料無效。", number=number))
            allowed_text = {"content", "font_family", "font_style", "height_mm",
                            "letter_spacing_mm", "line_spacing_mm", "alignment", "origin_mm"}
            if set(text_data) != allowed_text:
                raise ValueError(tr("第 {number} 個物件的文字資料無效。", number=number))
            content, family, style, alignment = (text_data.get("content"), text_data.get("font_family"),
                                                   text_data.get("font_style"), text_data.get("alignment"))
            if (not isinstance(content, str) or not content.strip() or len(content) > 1024
                    or not isinstance(family, str) or not family.strip() or len(family) > 256
                    or not isinstance(style, str) or len(style) > 256
                    or alignment not in ("left", "center", "right")):
                raise ValueError(tr("第 {number} 個物件的文字資料無效。", number=number))
            numbers = [text_data.get(key) for key in ("height_mm", "letter_spacing_mm", "line_spacing_mm")]
            limits = ((.1, 300), (-5, 50), (0, 300))
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or not lower <= value <= upper
                       for value, (lower, upper) in zip(numbers, limits)):
                raise ValueError(tr("第 {number} 個物件的文字資料無效。", number=number))
            origin = text_data.get("origin_mm")
            if (not isinstance(origin, (list, tuple)) or len(origin) != 2
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) for value in origin)):
                raise ValueError(tr("第 {number} 個物件的文字資料無效。", number=number))
            source = validate_contours(raw_item["text_source_contours"], number)
            text_source_points += sum(map(len, source))
            if (len(source) != len(safe_item["contours"])
                    or any(len(left) != len(right) for left, right in zip(source, safe_item["contours"]))
                    or text_source_points > MAX_POINTS):
                raise ValueError(tr("第 {number} 個物件的文字資料無效。", number=number))
            safe_item["text_data"] = {**text_data, "height_mm": float(numbers[0]),
                "letter_spacing_mm": float(numbers[1]), "line_spacing_mm": float(numbers[2]),
                "origin_mm": [float(origin[0]), float(origin[1])]}
            safe_item["text_source_contours"] = source
        if "node_paths" in raw_item:
            from .nodes import validate_saved_paths
            safe_item["node_paths"] = validate_saved_paths(safe_item, raw_item["node_paths"])
        if "print_style" in raw_item:
            from .artwork import validate_print_style
            if "image_data" in raw_item or raw_item.get("registration_guide"):
                raise ValueError("Only ordinary vector objects have a print style.")
            style = validate_print_style(raw_item["print_style"])
            if style["fill"] or style["stroke"]:
                safe_item["print_style"] = style
        if "group_path" in raw_item:
            path = raw_item["group_path"]
            if (not isinstance(path, list) or not path
                    or any(not isinstance(value, str) or not value for value in path)
                    or len(set(path)) != len(path)):
                raise ValueError(tr("第 {number} 個物件的群組資料無效。", number=number))
            safe_item["group_path"] = path[:]
        for key in ("cut", "print", "visible", "locked"):
            if key in raw_item:
                if type(raw_item[key]) is not bool:
                    raise ValueError(tr("第 {number} 個物件的切割狀態無效。", number=number))
                safe_item[key] = raw_item[key]
        if "source_color" in raw_item:
            color = raw_item["source_color"]
            if type(color) is not int or not 0 <= color <= 0xFFFFFFFF:
                raise ValueError(tr("第 {number} 個物件的原始顏色無效。", number=number))
            safe_item["source_color"] = color
        if "registration_guide" in raw_item:
            if type(raw_item["registration_guide"]) is not bool:
                raise ValueError(tr("第 {number} 個物件的定位狀態無效。", number=number))
            safe_item["registration_guide"] = raw_item["registration_guide"]
        if any(key in raw_item for key in ("image_transform", "image_clip")) and "image_data" not in raw_item:
            raise ValueError(tr("第 {number} 個物件的圖片資料無效。", number=number))
        if "image_data" in raw_item:
            try:
                encoded = raw_item["image_data"]
                if not isinstance(encoded, str):
                    raise ValueError
                _qoi_image(encoded)
            except (ValueError, binascii.Error) as error:
                raise ValueError(tr("第 {number} 個物件的圖片資料無效。", number=number)) from error
            safe_item["image_data"] = encoded
            transform = raw_item.get("image_transform")
            if (not isinstance(transform, (list, tuple)) or len(transform) != 6
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) for value in transform)):
                raise ValueError(tr("第 {number} 個物件的圖片轉換無效。", number=number))
            safe_item["image_transform"] = [float(value) for value in transform]
            if "image_clip" in raw_item:
                safe_item["image_clip"] = validate_contours(raw_item["image_clip"], number, allow_empty=True)
            if "bleed" in raw_item:
                from .artwork import validate_bleed
                safe_item["bleed"] = validate_bleed(raw_item["bleed"])
                if safe_item["bleed"].get("mode") == "edge" and safe_item["bleed"]["width_mm"]:
                    from .edge_bleed import edge_bleed_image
                    edge_bleed_image(safe_item)
            if "raster_bleed_mm" in raw_item:
                value = raw_item["raster_bleed_mm"]
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or not 0 < value <= 5):
                    raise ValueError(tr("第 {number} 個物件的圖片資料無效。", number=number))
                safe_item["raster_bleed_mm"] = float(value)
            if "raster_bleed_scale" in raw_item:
                scale = raw_item["raster_bleed_scale"]
                if ("raster_bleed_mm" not in safe_item or not isinstance(scale, (list, tuple))
                        or len(scale) != 2 or any(isinstance(value, bool)
                            or not isinstance(value, (int, float)) or not math.isfinite(value)
                            or value <= 0 for value in scale)):
                    raise ValueError(tr("第 {number} 個物件的圖片資料無效。", number=number))
                safe_item["raster_bleed_scale"] = [float(value) for value in scale]
            if "trace_options" in raw_item:
                from .mask_edit import validate_trace_options
                safe_item["trace_options"] = validate_trace_options(_qoi_image(encoded), raw_item["trace_options"])
            if "trace_mask" in raw_item:
                from .mask_edit import working_mask
                candidate = {**safe_item, "trace_mask": raw_item["trace_mask"]}
                working_mask(candidate, _qoi_image(encoded))
                safe_item["trace_mask"] = raw_item["trace_mask"]
        elif "bleed" in raw_item:
            raise ValueError("Bleed is only supported on image objects.")
        if "image_data" not in raw_item and any(key in raw_item for key in
                ("trace_options", "trace_mask", "raster_bleed_mm", "raster_bleed_scale")):
            raise ValueError("Saved tracing state requires an image object.")
        items.append(safe_item)

    expanded_points = sum(len(contour) for item in items
                          for key in ("contours", "image_clip", "text_source_contours")
                          for contour in item.get(key, []))
    if expanded_points > MAX_POINTS:
        raise ValueError(tr("展開點數超過 {limit} 點，請簡化路徑或拆分文件後重試。",
                            limit=f"{MAX_POINTS:,}"))

    defaults = default_cut_settings()
    raw_settings = data.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise ValueError(tr("設計檔中的切割設定無效。"))
    settings = {**defaults, **raw_settings}
    host = settings["host"]
    if not isinstance(host, str):
        raise ValueError(tr("設計檔中的連線 IP 無效。"))
    if host:
        try:
            address = ipaddress.IPv4Address(host)
            if address.is_unspecified or address.is_multicast:
                raise ValueError
        except ValueError as error:
            raise ValueError(tr("設計檔中的連線 IP 無效。")) from error
    if (type(settings["custom"]) is not bool
            or type(settings["speed"]) is not int or not 1 <= settings["speed"] <= 13
            or type(settings["force"]) is not int or not 1 <= settings["force"] <= 160
            or type(settings["passes"]) is not int or not 1 <= settings["passes"] <= 10
            or type(settings["tool"]) is not int or settings["tool"] not in (0, 1)
            or settings["connection"] not in ("wifi", "usb")
            or type(settings["port"]) is not int or not 1 <= settings["port"] <= 65535):
        raise ValueError(tr("設計檔中的切割或連線設定無效。"))
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError(tr("設計檔中的中繼資料無效。"))
    if "guides" in metadata:
        guides = metadata["guides"]
        if not isinstance(guides, list) or len(guides) > 1000:
            raise ValueError(tr("設計檔中的參考線設定無效。"))
        for guide in guides:
            if (not isinstance(guide, dict) or set(guide) != {"orientation", "position"}
                    or guide["orientation"] not in ("horizontal", "vertical")):
                raise ValueError(tr("設計檔中的參考線設定無效。"))
            position = guide["position"]
            maximum = PAGE_WIDTH_MM if guide["orientation"] == "vertical" else PAGE_HEIGHT_MM
            if (isinstance(position, bool) or not isinstance(position, (int, float))
                    or not math.isfinite(position) or not 0 <= position <= maximum):
                raise ValueError(tr("設計檔中的參考線設定無效。"))
    if "optimize_paths" in metadata and type(metadata["optimize_paths"]) is not bool:
        raise ValueError(tr("設計檔中的路徑排序設定無效。"))
    if "printer_settings" in metadata:
        from .print_dialog import validate_printer_settings
        validate_printer_settings(metadata["printer_settings"])
    if "print_cut" in metadata:
        registration = metadata["print_cut"]
        if not isinstance(registration, dict) or type(registration.get("enabled")) is not bool:
            raise ValueError(tr("設計檔中的定位設定無效。"))
        bounds = registration.get("bounds")
        if bounds is not None and (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in bounds)
                or not (bounds[0] < bounds[2] and bounds[1] < bounds[3])):
            raise ValueError(tr("設計檔中的定位設定無效。"))
        dimensions = [registration.get("paper_width"), registration.get("paper_height")]
        minimums = registration.get("printer_margins", [0] * 4)
        if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 for value in dimensions)
                or not isinstance(minimums, list) or len(minimums) != 4
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value < 0 for value in minimums)):
            raise ValueError(tr("設計檔中的定位設定無效。"))
        if registration["enabled"] and min(dimensions) <= 0:
            raise ValueError(tr("設計檔中的定位設定無效。"))
        from .printing import registration_profile
        registration_profile(registration)
        from .commands import validate_registration_offset
        validate_registration_offset(registration.get("offset", (0, 0)))
    return items, {key: settings[key] for key in defaults}


def validate_contours(contours: Any, number: int, *, allow_empty: bool = False):
    if not isinstance(contours, list) or (not contours and not allow_empty):
        raise ValueError(tr("第 {number} 個物件沒有路徑。", number=number))
    safe = []
    for contour in contours:
        if not isinstance(contour, list) or len(contour) < 2:
            raise ValueError(tr("第 {number} 個物件有空白或過短的輪廓。", number=number))
        points = []
        for point in contour:
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) for value in point)):
                raise ValueError(tr("第 {number} 個物件包含無效座標。", number=number))
            points.append((float(point[0]), float(point[1])))
        safe.append(points)
    return safe

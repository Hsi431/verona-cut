"""Validated Juliet PLT command building; no hardware access."""
import math
import struct

from .i18n import tr

UNITS_PER_MM = 40
MAX_WIDTH_MM = 300
MAX_HEIGHT_MM = 300


def validate_registration_offset(offset):
    if (not isinstance(offset, (list, tuple)) or len(offset) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) or abs(v) > 5 for v in offset)):
        raise ValueError(tr("對位微調必須是兩個介於 −5 至 5 mm 的有限數值"))
    return tuple(offset)


def registration_point_error(x, y, bounds, offset=(0, 0)):
    """Check a page coordinate against a validated Print & Cut frame."""
    left, top, right, bottom = bounds
    if not left <= x <= right or not top <= y <= bottom:
        return "路徑超出 Print & Cut 邊界"
    mapped_x, mapped_y = x - left + offset[0], bottom - y - offset[1]
    if not 0 <= mapped_x <= right - left or not 0 <= mapped_y <= bottom - top:
        return "微調後的路徑超出 Print & Cut 邊界"
    return None


def encode_plt(contours, *, force=None, speed=None, tool=0, passes=1, registration_bounds=None,
               registration_offset=(0, 0)):
    """Encode ordinary cuts or a bounded Print & Cut registration job.

    Uses float32 quantization and the cutter's feed/cross coordinate order.
    Use None for force/speed to retain the machine's current material settings.
    A small registration cut has passed hardware testing; detailed alignment
    accuracy is still being checked. Blade-offset compensation is not implemented.
    """
    if type(tool) is not int or tool not in (0, 1):
        raise ValueError(tr("工具必須是刀片 (0) 或畫筆 (1)"))
    if type(passes) is not int or not 1 <= passes <= 10:
        raise ValueError(tr("切割次數必須是介於 1–10 的整數"))
    for label, value, upper in (("壓力", force, 160), ("速度", speed, 13)):
        if value is not None and (type(value) is not int or not 1 <= value <= upper):
            raise ValueError(tr("{label}必須介於 1–{upper}", label=tr(label), upper=upper))
    offset_x, offset_y = validate_registration_offset(registration_offset)
    if registration_bounds is None and (offset_x or offset_y):
        raise ValueError(tr("對位微調只適用於 Print & Cut"))
    if registration_bounds is not None:
        try:
            bounds = tuple(registration_bounds)
        except TypeError as error:
            raise ValueError(tr("Print & Cut 邊界必須是四個有限數值")) from error
        if (len(bounds) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                    or not math.isfinite(v) for v in bounds)):
            raise ValueError(tr("Print & Cut 邊界必須是四個有限數值"))
        left, top, right, bottom = bounds
        width, height = right - left, bottom - top
        if (min(bounds) <= 0 or width <= 0 or height <= 0
                or width > MAX_WIDTH_MM or height > MAX_HEIGHT_MM):
            raise ValueError(tr("Print & Cut 邊界必須為正數且尺寸不超過 300 mm"))
    paths = []
    for contour in contours:
        points = []
        for point in contour:
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) for v in point)):
                raise ValueError(tr("路徑包含無效座標"))
            x, y = point
            if registration_bounds is None:
                if (x < -1e-7 or y < -1e-7 or x > MAX_WIDTH_MM + 1e-7
                        or y > MAX_HEIGHT_MM + 1e-7):
                    raise ValueError(tr("路徑超出 300 × 300 mm 工作區"))
                if y > (2**31 - 1) / UNITS_PER_MM:
                    raise ValueError(tr("路徑長度超出機器座標範圍"))
                mapped_x, mapped_y = max(0, x), max(0, y)
            else:
                error = registration_point_error(x, y, bounds, (offset_x, offset_y))
                if error:
                    raise ValueError(tr(error))
                # Offsets use page axes: positive X right, positive Y down.
                mapped_x, mapped_y = x - left + offset_x, bottom - y - offset_y
            # Quantize to float32 before rounding, including half-unit values.
            cross, feed = struct.unpack("<ff", struct.pack("<ff", mapped_x, mapped_y))
            point = (round(feed * UNITS_PER_MM), round(cross * UNITS_PER_MM))
            if any(v > 2**31 - 1 for v in point):
                raise ValueError(tr("路徑長度超出機器座標範圍"))
            if not points or point != points[-1]:
                # Remove only exact forward collinearity on the cutter's integer
                # grid. Keep corners, reversals, contour order and closure.
                while len(points) >= 2:
                    a, b = points[-2:]
                    ab = (b[0] - a[0], b[1] - a[1])
                    bc = (point[0] - b[0], point[1] - b[1])
                    if ab[0] * bc[1] != ab[1] * bc[0] or ab[0] * bc[0] + ab[1] * bc[1] < 0:
                        break
                    points.pop()
                points.append(point)
        if len(points) >= 2:
            paths.append(points)
    if not paths:
        raise ValueError(tr("設計中沒有可切割的線段"))
    if registration_bounds is None:
        feed_max = max(y for path in paths for y, x in path)
        cross_max = max(x for path in paths for y, x in path)
        output = [f"IN;FSIZE{feed_max},{cross_max};U ", f"SP{tool + 1};"]
    else:
        height_units = round(struct.unpack("<f", struct.pack("<f", height))[0] * UNITS_PER_MM)
        width_units = round(struct.unpack("<f", struct.pack("<f", width))[0] * UNITS_PER_MM)
        output = [f"IN;PA;BD:103,0;IN;TB26,{height_units},{width_units};CT1;IN;PU;U ",
                  f"SP{tool + 1};"]
    if force is not None:
        output.append(f"!FS{force};")
    if speed is not None:
        output.append(f"VS{speed};")
    for points in paths:
        path = f"U{points[0][0]},{points[0][1]} " + "".join(
            f"D{feed},{cross} " for feed, cross in points[1:])
        output.extend([path] * passes)
    # Registered jobs return using the maximum encoded feed coordinate.
    end_feed = max(feed for path in paths for feed, cross in path) if registration_bounds is not None else 0
    output.append(f"U{end_feed},0 U !PG;")
    if registration_bounds is not None:
        # Registered jobs require a second finalization command.
        output.append("!PG;")
    return "".join(output).encode("ascii")


def encode_document(items, metadata, settings, *, force=None, speed=None, tool=None, passes=None):
    """Shared GUI/MCP job encoding with the saved optional path-order policy."""
    return encode_document_job(items, metadata, settings, force=force, speed=speed, tool=tool, passes=passes)[0]


def encode_document_job(items, metadata, settings, *, force=None, speed=None, tool=None, passes=None):
    """Return payload and the exact contours/parameters used to encode it."""
    from .path_order import cut_contours
    registration = metadata.get("print_cut", {})
    bounds = registration.get("bounds") if registration.get("enabled") else None
    if registration.get("enabled") and not bounds:
        raise ValueError(tr("此原生檔缺少可用的定位標記範圍。"))
    contours = cut_contours(items, metadata)
    parameters = {"force": force if force is not None else settings["force"] if settings["custom"] else None,
        "speed": speed if speed is not None else settings["speed"] if settings["custom"] else None,
        "passes": settings.get("passes", 1) if passes is None else passes,
        "tool": settings["tool"] if tool is None else tool, "registration_bounds": bounds,
        "registration_offset": registration.get("offset", (0, 0)) if bounds else (0, 0)}
    return encode_plt(contours, **parameters), {"contours": contours, **parameters}

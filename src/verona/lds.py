"""Read native ZIP/CDOC vectors, bitmaps and Verona editing extensions.

Bitmap masks and cutting curves are separate; an ARMS group's geometry is a
registration reference, not a cutting path.
"""
from __future__ import annotations

import base64
import configparser
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import struct
import zipfile

from .i18n import tr
from .native_images import decode_qoi


_HEADER = struct.Struct("<8I")
_SIGNATURE = 0x1E4E9CAD
_IDENTITY = (0.0, 0.0, 1.0, 0.0, 0.0, 1.0)
_CUT_CONTOURS = 0xFF000000
_MAX_DOCUMENT = 32 * 1024 * 1024
_MAX_DEPTH = 256
_MAX_RECORDS = 100_000
_MAX_OBJECTS = 10_000
_MAX_ITEMS = 2_000
_MAX_POINTS = 2_000_000


@dataclass
class NativeDesign:
    items: list[dict]
    metadata: dict
    settings: dict | None = None


@dataclass
class _Shape:
    code: int
    identifier: int
    records: list[tuple[int, int, bytes]] = field(default_factory=list)
    children: list[_Shape] = field(default_factory=list)


def _invalid(reason):
    return ValueError(tr("無法讀取 LDS：{reason}", reason=reason))


def _map_point(matrix, x, y):
    tx, ty, a, b, c, d = matrix
    return tx + a * x + c * y, ty + b * x + d * y


@dataclass
class _PointBudget:
    remaining: int

    def consume(self, count):
        if count > self.remaining:
            raise _invalid(tr("展開點數超過 {limit} 點，請簡化路徑或拆分文件後重試。", limit=f"{_MAX_POINTS:,}"))
        self.remaining -= count


def _curve(payload, tolerance, budget):
    if len(payload) < 30:
        raise _invalid("truncated curve")
    count = struct.unpack_from("<I", payload)[0]
    if count > 1_000_000 or len(payload) != 30 + 12 * count:
        raise _invalid("invalid curve length")
    matrix = struct.unpack_from("<6f", payload, 4)
    if not all(math.isfinite(value) for value in matrix):
        raise _invalid("invalid curve transform")
    # Read controls in wire order without allocating a second, potentially
    # million-element list before the expanded-point budget can be checked.
    points = struct.iter_unpack("<ffI", memoryview(payload)[30:])
    contours, current = [], []
    closed = False

    def finish():
        nonlocal current, closed
        if len(current) >= 2:
            if closed and current[-1] != current[0]:
                budget.consume(1)
                current.append(current[0])
            contours.append(current)
        current, closed = [], False

    for x, y, flags in points:
        if not math.isfinite(x) or not math.isfinite(y):
            raise _invalid("invalid curve coordinates")
        if flags & 0x10:
            finish()
        point = _map_point(matrix, x, y)
        if not current:
            budget.consume(1)
            current.append(point)
        elif flags & 7 == 2:
            try:
                control, endpoint = next(points), next(points)
            except StopIteration:
                raise _invalid("truncated cubic segment") from None
            if not all(math.isfinite(x) and math.isfinite(y) for x, y, _ in (control, endpoint)):
                raise _invalid("invalid curve coordinates")
            if control[2] & 0x10 or endpoint[2] & 0x10:
                raise _invalid("cubic segment crosses a contour boundary")
            p0 = current[-1]
            p1 = point
            p2 = _map_point(matrix, *control[:2])
            p3 = _map_point(matrix, *endpoint[:2])
            length_bound = sum(math.dist(a, b) for a, b in zip((p0, p1, p2), (p1, p2, p3)))
            scaled_length = length_bound / tolerance
            if not math.isfinite(scaled_length) or scaled_length > 200_000:
                raise _invalid("cubic segment is too large")
            steps = max(2, math.ceil(scaled_length))
            budget.consume(steps)
            for step in range(1, steps + 1):
                t = step / steps
                u = 1 - t
                current.append((u**3 * p0[0] + 3*u*u*t*p1[0] + 3*u*t*t*p2[0] + t**3*p3[0],
                                u**3 * p0[1] + 3*u*u*t*p1[1] + 3*u*t*t*p2[1] + t**3*p3[1]))
            flags |= control[2] | endpoint[2]
        else:
            budget.consume(1)
            current.append(point)
        closed = closed or bool(flags & 0x80)
        if flags & 0x20:
            finish()
    finish()
    return contours


def load_lds(filename, tolerance=0.05):
    """Import supported native vector/Logo objects, images and ARMS references.

    Unknown object classes and hidden/multiple layers are rejected instead of
    silently dropping potentially relevant cutting geometry.
    """
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise _invalid("invalid curve tolerance")
    with zipfile.ZipFile(filename) as archive:
        members = [entry for entry in archive.infolist() if entry.filename == "CDOC"]
        if len(members) != 1 or not 52 <= members[0].file_size <= _MAX_DOCUMENT:
            raise _invalid("missing or oversized CDOC")
        data = archive.read(members[0])
        editable = None
        snapshots = [entry for entry in archive.infolist() if entry.filename == "VERONA.json"]
        if len(snapshots) > 1:
            raise _invalid("duplicate Verona editing data")
        if snapshots:
            from .input_safety import MAX_DESIGN_BYTES, decode_design
            if snapshots[0].file_size > MAX_DESIGN_BYTES:
                raise _invalid("oversized Verona editing data")
            candidate = decode_design(archive.read(snapshots[0]))
            if not isinstance(candidate, dict):
                raise _invalid("invalid Verona editing data")
            if candidate.get("cdoc_sha256") == hashlib.sha256(data).hexdigest():
                editable = candidate
        preview = None
        if "PREVIEW" in archive.namelist():
            entry = archive.getinfo("PREVIEW")
            if entry.file_size > 8 * 1024 * 1024:
                raise _invalid("oversized preview")
            preview = archive.read(entry)
    if data[:8] != b"DLCF\x0a\xc3\x47\x0e":
        raise _invalid("unrecognized CDOC header")
    if _HEADER.unpack_from(data, 20)[1:4] != (1000, 3, 0):
        raise _invalid("unrecognized document record")

    root, stack = None, []
    record_count = object_count = item_count = 0
    position = 20
    while position < len(data):
        record_count += 1
        if record_count > _MAX_RECORDS:
            raise _invalid(tr("資料紀錄超過 {limit} 筆，請拆分文件後重試。", limit=f"{_MAX_RECORDS:,}"))
        if position + _HEADER.size > len(data):
            raise _invalid("truncated record header")
        signature, code, kind, size, identifier, marker, _, _ = _HEADER.unpack_from(data, position)
        end = position + _HEADER.size + size
        if signature != _SIGNATURE or end > len(data):
            raise _invalid("invalid record boundary")
        payload = data[position + _HEADER.size:end]
        if kind == 3:
            object_count += 1
            item_count += code in (1011, 1012)
            if len(stack) >= _MAX_DEPTH:
                raise _invalid(tr("物件巢狀超過 {limit} 層，請減少巢狀群組後重試。", limit=f"{_MAX_DEPTH:,}"))
            if object_count > _MAX_OBJECTS:
                raise _invalid(tr("結構物件超過 {limit} 個，請拆分文件後重試。", limit=f"{_MAX_OBJECTS:,}"))
            if item_count > _MAX_ITEMS:
                raise _invalid(tr("圖稿物件超過 {limit} 個，請拆分文件後重試。", limit=f"{_MAX_ITEMS:,}"))
            if code not in (1000, 1011, 1012, 1014, 1021):
                raise ValueError(tr("LDS 包含尚未支援的物件類型：{code}", code=code))
            if size:
                raise _invalid("unexpected object-start payload")
            shape = _Shape(code, identifier)
            if stack:
                stack[-1].children.append(shape)
            elif root is None:
                root = shape
            else:
                raise _invalid("multiple document roots")
            stack.append(shape)
        elif kind == 4:
            if not stack or (stack[-1].code, stack[-1].identifier) != (code, identifier) or size:
                raise _invalid("mismatched object end")
            stack.pop()
        else:
            if not stack:
                raise _invalid("record outside an object")
            stack[-1].records.append((kind, marker, payload))
        position = end
    if root is None or stack:
        raise _invalid("incomplete document")

    print_cut = None
    for kind, marker, payload in root.records:
        if kind == 15:
            media = payload[:56].ljust(56, b"\0")
            enabled = media[11]
            width, height = struct.unpack_from("<2f", media, 12)
            if (enabled not in (0, 1) or not all(math.isfinite(value) for value in (width, height))
                    or (enabled and min(width, height) <= 0)):
                raise _invalid("invalid paper settings")
            print_cut = {"enabled": bool(enabled), "paper_width": width, "paper_height": height,
                         "bounds": None}
        if kind == 14 and marker == 203:
            layers = configparser.ConfigParser()
            try:
                layers.read_string(payload.decode("utf-8-sig"))
                values = layers["layers"]
                unsupported = values.getint("LayerCount", 1) > 1 or any(
                    value != "0" for key, value in values.items() if key.endswith((".hidden", ".notex")))
            except (configparser.Error, KeyError, ValueError) as error:
                raise _invalid("invalid layer settings") from error
            if unsupported:
                raise ValueError(tr("此 LDS 的多圖層或隱藏圖層尚未支援。"))

    image_definitions = {}
    image_assets = {}
    items = []
    registration = []
    native_groups = []
    group_serial = 0
    budget = _PointBudget(_MAX_POINTS)

    def descendant_geometry_records(shape):
        marker = 0 if shape.code == 1011 else 102 if shape.code == 1012 else None
        result = [body for kind, value, body in shape.records if kind == 11 and value == marker]
        for child in shape.children:
            result.extend(descendant_geometry_records(child))
        return result

    def visit(shape, inherited_registration=False, group_path=()):
        nonlocal group_serial
        common = next((body for kind, _, body in shape.records if kind == 100), None)
        is_registration = inherited_registration
        if shape.code in (1014, 1021):
            group_serial += 1
            flags = next((body for kind, _, body in shape.records if kind == 120), b"\0")
            if not flags:
                raise _invalid("empty group state")
            is_registration = is_registration or bool(flags[0])
            page_marks = shape.code == 1014 and len(flags) >= 11 and struct.unpack_from("<I", flags, 7)[0] == 2
            if not is_registration and not page_marks:
                group_path = (*group_path, f"lds:{shape.code}:{shape.identifier}:{group_serial}")
            if shape.code == 1021 and not is_registration:
                represented = [body for child in shape.children for body in descendant_geometry_records(child)]
                if any(body not in represented for kind, _, body in shape.records if kind == 11):
                    raise ValueError(tr("此 LDS 的獨立 Logo 群組曲線尚未支援。"))
            if common is not None:
                if len(common) != 106:
                    raise _invalid("unsupported shape header length")
                if struct.unpack_from("<6f", common, 24) != _IDENTITY:
                    raise ValueError(tr("此 LDS 的群組變形尚未支援。"))
                native_groups.append({"type": shape.code, "id": shape.identifier,
                                      "bounds": list(struct.unpack_from("<4f", common, 4)),
                                      "group_state": flags.hex()})
                if is_registration and not inherited_registration:
                    registration.append({"bounds": list(struct.unpack_from("<4f", common, 4)),
                                         "group_state": flags.hex()})

        if shape.code in (1011, 1012):
            if common is None or len(common) != 106:
                raise _invalid("missing shape properties")
            color = struct.unpack_from("<I", common, 20)[0]
            curve_records = {marker: body for kind, marker, body in shape.records if kind == 11}
            if shape.code == 1011:
                if 0 not in curve_records:
                    raise _invalid("missing vector curve")
                contours = _curve(curve_records[0], tolerance, budget)
                item = {"name": tr("原生路徑 {number}", number=shape.identifier), "contours": contours,
                        "source_color": color, "cut": color == _CUT_CONTOURS and not is_registration}
            else:
                image_key = next((body for kind, _, body in shape.records if kind in (25, 26)), None)
                image_data = next((body for kind, _, body in shape.records if kind in (10, 74)), None)
                if image_key is None or len(image_key) != 8:
                    raise _invalid("missing shared image key")
                if image_data is not None:
                    if not image_data.startswith(b"qoif"):
                        raise ValueError(tr("此 LDS 的圖片編碼尚未支援。"))
                    asset_hash = hashlib.sha256(image_data).hexdigest()
                    if asset_hash not in image_assets:
                        width, height, _ = decode_qoi(image_data)
                        image_assets[asset_hash] = (base64.b64encode(image_data).decode("ascii"), width, height)
                    image_definitions[image_key] = image_assets[asset_hash]
                if image_key not in image_definitions:
                    raise _invalid("undefined shared image reference")
                encoded, width, height = image_definitions[image_key]
                matrix = struct.unpack_from("<6f", common, 24)
                if not all(math.isfinite(value) for value in matrix):
                    raise _invalid("invalid image transform")
                image_clip = _curve(curve_records[101], tolerance, budget) if 101 in curve_records else []
                contours = _curve(curve_records[102], tolerance, budget) if 102 in curve_records else image_clip
                if not contours:
                    corners = ((0, 0), (width, 0), (width, height), (0, height), (0, 0))
                    budget.consume(len(corners))
                    contours = [[_map_point(matrix, x, y) for x, y in corners]]
                item = {"name": tr("原生圖片 {number}", number=shape.identifier), "contours": contours,
                        "source_color": color, "cut": 102 in curve_records and not is_registration,
                        "image_data": encoded, "image_transform": list(matrix), "image_clip": image_clip}
            if contours:
                if is_registration:
                    item["registration_guide"] = True
                elif group_path:
                    item["group_path"] = list(group_path)
                items.append(item)
        for child in shape.children:
            visit(child, is_registration, group_path)

    visit(root)
    if editable is not None:
        from .ui import VeronaWindow
        saved_items, settings = VeronaWindow._validate_document(editable)
        return NativeDesign(saved_items, editable.get("metadata", {}), settings)
    if not items:
        raise _invalid("document has no supported geometry")
    metadata = {"source_format": "lds", "source_name": Path(filename).name,
                "registration": registration, "native_groups": native_groups}
    if print_cut is not None:
        page_marks = [group["bounds"] for group in native_groups
                      if group["type"] == 1014 and len(bytes.fromhex(group["group_state"])) >= 11
                      and struct.unpack_from("<I", bytes.fromhex(group["group_state"]), 7)[0] == 2]
        if len(page_marks) == 1:
            bounds = page_marks[0]
            if not all(math.isfinite(value) for value in bounds) or not (
                    bounds[0] < bounds[2] and bounds[1] < bounds[3]):
                raise _invalid("invalid page-mark bounds")
            print_cut["bounds"] = bounds
        metadata["print_cut"] = print_cut
    if preview:
        metadata["source_preview"] = base64.b64encode(preview).decode("ascii")
    return NativeDesign(items, metadata)

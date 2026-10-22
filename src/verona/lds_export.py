"""Bounded native CDOC output with a hash-bound Verona editing document.

The native stream contains printable artwork and cut paths. The additional
VERONA.json member preserves names, layers, source pixels and editing state;
other applications may ignore it. Official application compatibility is unverified.
"""
import hashlib
import io
import json
import math
from pathlib import Path
import struct
import tempfile
import zipfile

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from .artwork import draw_printed_item, item_bounds
from .input_safety import MAX_DESIGN_BYTES
from .layers import cuttable, printable
from .lds import _HEADER, _SIGNATURE, _MAX_DOCUMENT, _MAX_DEPTH, _MAX_ITEMS, _MAX_OBJECTS, _MAX_POINTS, _MAX_RECORDS
from .native_images import encode_qoi


EDITABLE_MEMBER = "VERONA.json"
_IDENTITY = (0., 0., 1., 0., 0., 1.)
_PIXELS_PER_MM = 300 / 25.4


def _common(bounds, *, color=0xFF000000, matrix=_IDENTITY):
    data = bytearray(106)
    struct.pack_into("<4f", data, 4, bounds.left(), bounds.top(), bounds.right(), bounds.bottom())
    struct.pack_into("<I", data, 20, color)
    struct.pack_into("<6f", data, 24, *matrix)
    return bytes(data)


def _printed_image(item):
    from .raster_import import decode_embedded_image
    if "image_data" in item and not any(item.get("bleed", {}).get(key, 0) for key in ("width_mm", "inner_mm")):
        image = decode_embedded_image(item["image_data"])
        clip = item.get("image_clip") or []
        return image, item["image_transform"], clip
    bounds = item_bounds(item, printed=True)
    width = max(1, math.ceil(bounds.width() * _PIXELS_PER_MM) + 2)
    height = max(1, math.ceil(bounds.height() * _PIXELS_PER_MM) + 2)
    if width * height > 16_000_000:
        raise ValueError("LDS printed artwork exceeds the 16-million-pixel limit.")
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    if not painter.isActive():
        raise ValueError("Could not render LDS printed artwork.")
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.translate(1, 1)
        painter.scale(_PIXELS_PER_MM, _PIXELS_PER_MM)
        painter.translate(-bounds.left(), -bounds.top())
        draw_printed_item(painter, item)
    finally:
        painter.end()
    return image, [bounds.left() - 1/_PIXELS_PER_MM, bounds.top() - 1/_PIXELS_PER_MM,
                   1/_PIXELS_PER_MM, 0., 0., 1/_PIXELS_PER_MM], []


class _DocumentWriter:
    def __init__(self):
        self.data = bytearray(b"DLCF" + struct.pack("<4I", 0x0E47C30A, 0, 1, 3))
        self.identifier = self.records = self.objects = self.items = self.points = 0
        self.images = set()

    def record(self, code, kind, payload=b"", *, marker=0, identifier=None):
        if identifier is None:
            self.identifier += 1
            identifier = self.identifier
        self.records += 1
        if kind == 3:
            self.objects += 1
            self.items += code in (1011, 1012)
        if (self.records > _MAX_RECORDS or self.objects > _MAX_OBJECTS or self.items > _MAX_ITEMS
                or len(self.data) + _HEADER.size + len(payload) > _MAX_DOCUMENT):
            raise ValueError("The design exceeds LDS document limits. Save it as JDESIGN instead.")
        self.data.extend(_HEADER.pack(_SIGNATURE, code, kind, len(payload), identifier, marker, 0, 0))
        self.data.extend(payload)
        return identifier

    def curve(self, contours, marker=0):
        count = sum(len(contour) for contour in contours)
        self.points += count
        if self.points > _MAX_POINTS:
            raise ValueError("The design exceeds the LDS point limit.")
        data = bytearray(struct.pack("<I6fBB", count, *_IDENTITY, 0, 0))
        for contour in contours:
            closed = contour[0] == contour[-1]
            for index, point in enumerate(contour):
                flags = 0x10 if index == 0 else 1
                if index == len(contour) - 1:
                    flags |= 0x20 | (0x80 if closed else 0)
                data.extend(struct.pack("<ffI", *point, flags))
        self.record(0, 11, data, marker=marker)

    def start_group(self, bounds, *, page_marks=False):
        identifier = self.record(1014, 3)
        self.record(0, 100, _common(bounds))
        flags = bytearray(11)
        if page_marks:
            struct.pack_into("<2I", flags, 3, 2, 2)
        self.record(0, 120, flags)
        return identifier

    def item(self, item):
        if printable(item):
            image, matrix, clip = _printed_image(item)
            image = image.convertToFormat(QImage.Format.Format_RGBA8888)
            pixels = bytes(image.constBits())
            encoded = encode_qoi(image.width(), image.height(), pixels)
            key = hashlib.sha256(encoded).digest()[:8]
            identifier = self.record(1012, 3)
            self.record(0, 100, _common(item_bounds(item, printed=True), color=0xFFFFFFFF, matrix=matrix))
            if clip:
                self.curve(clip, 101)
            if cuttable(item):
                self.curve(item["contours"], 102)
            self.record(0, 25 if key in self.images else 26, key)
            if key not in self.images:
                self.record(0, 10, encoded)
                self.images.add(key)
            self.record(1012, 4, identifier=identifier)
        elif cuttable(item):
            identifier = self.record(1011, 3)
            self.record(0, 100, _common(item_bounds(item)))
            self.curve(item["contours"])
            self.record(1011, 4, identifier=identifier)


def _native_document(items, metadata):
    writer = _DocumentWriter()
    root = writer.record(1000, 3)
    registration = metadata.get("print_cut", {})
    if registration:
        media = bytearray(56)
        media[11] = int(registration.get("enabled", False))
        struct.pack_into("<2f", media, 12, registration["paper_width"], registration["paper_height"])
        writer.record(0, 15, media)
    group_bounds = {}
    for item in items:
        if not (printable(item) or cuttable(item)):
            continue
        path = item.get("group_path", [])
        if len(path) + 2 > _MAX_DEPTH:
            raise ValueError("The groups exceed the LDS nesting limit.")
        for depth in range(1, len(path) + 1):
            key = tuple(path[:depth])
            bounds = item_bounds(item, printed=True)
            group_bounds[key] = group_bounds[key].united(bounds) if key in group_bounds else bounds
    active, identifiers = [], []
    for item in items:
        if not (printable(item) or cuttable(item)):
            continue
        path = item.get("group_path", [])
        common = 0
        while common < min(len(path), len(active)) and path[common] == active[common]:
            common += 1
        while len(active) > common:
            writer.record(1014, 4, identifier=identifiers.pop())
            active.pop()
        for group in path[common:]:
            active.append(group)
            identifiers.append(writer.start_group(group_bounds[tuple(active)]))
        writer.item(item)
    while identifiers:
        writer.record(1014, 4, identifier=identifiers.pop())
    if registration.get("enabled") and registration.get("bounds"):
        from .printing import registration_geometry, registration_profile
        left, top, right, bottom = registration["bounds"]
        group = writer.start_group(QRectF(left, top, right-left, bottom-top), page_marks=True)
        stroke, short_length = registration_profile(registration)
        contours = [points + [points[0]] for points in registration_geometry(
            registration["bounds"], stroke=stroke, short_length=short_length)]
        writer.item({"name": "Registration marks", "contours": contours, "cut": False,
                     "print_style": {"fill": "#000000", "stroke": None, "width_mm": .2}})
        writer.record(1014, 4, identifier=group)
    writer.record(1000, 4, identifier=root)
    return bytes(writer.data)


def encode_lds(document):
    """Build a complete archive before the caller touches an existing file."""
    from .ui import VeronaWindow
    from .editor_actions import render_preview
    items, _settings = VeronaWindow._validate_document(document)
    metadata = document.get("metadata", {})
    try:
        cdoc = _native_document(items, metadata)
    except (OverflowError, struct.error) as error:
        raise ValueError("The design contains values outside the LDS numeric range.") from error
    snapshot = json.dumps({**document, "cdoc_sha256": hashlib.sha256(cdoc).hexdigest()},
                          ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(snapshot) > MAX_DESIGN_BYTES:
        raise ValueError("The editable document exceeds the LDS storage limit.")
    with tempfile.TemporaryDirectory(prefix="verona-preview-") as directory:
        preview = Path(directory)/"preview.png"
        render_preview(items, metadata, preview)
        png = preview.read_bytes()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("CDOC", cdoc)
        archive.writestr("PREVIEW", png)
        archive.writestr(EDITABLE_MEMBER, snapshot)
    payload = output.getvalue()
    from .lds import load_lds
    load_lds(io.BytesIO(payload))
    return payload

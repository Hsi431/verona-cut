"""Bounded SVG geometry import with physical millimetre coordinates."""
import math
from io import BytesIO
import re
from xml.etree import ElementTree as ET

import numpy as np
from svgpathtools import Line, parse_path
from svgpathtools.document import CONVERSIONS
from svgpathtools.parser import parse_transform
from svgpathtools.path import transform

from .i18n import tr
from . import input_safety

_MAX_SVG_ELEMENTS = 100_000
_MAX_SVG_TOKENS = 8_000_000
_SVG_TOKENS = re.compile(r"[A-Za-z]|[+-]?(?:\d*\.\d+|\d+\.?)(?:[eE][+-]?\d+)?")


def _length_mm(value):
    match = re.fullmatch(r"\s*([+\-\d.eE]+)\s*(mm|cm|in|pt|pc|px)?\s*", value)
    if not match:
        raise ValueError(tr("SVG 尺寸無法換算成毫米：{value}", value=value))
    number, unit = match.groups()
    result = float(number) * {"mm": 1, "cm": 10, "in": 25.4, "pt": 25.4 / 72,
                              "pc": 25.4 / 6, "px": 25.4 / 96, None: 25.4 / 96}[unit]
    if not math.isfinite(result) or result <= 0:
        raise ValueError(tr("SVG 尺寸必須是正數"))
    return result


def parse_svg_root(data):
    """Parse a plain SVG document; malformed XML is a ValueError like other rejections."""
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError(tr("請將 SVG 儲存為不含外部文件宣告的純 SVG"))
    try:
        parser = ET.iterparse(BytesIO(data), events=("start", "end"))
        depth = elements = 0
        for event, _ in parser:
            if event == "start":
                depth += 1
                elements += 1
                if depth > input_safety.MAX_DEPTH:
                    raise ValueError(tr("文件巢狀超過 {limit} 層，請減少巢狀後重試。", limit=input_safety.MAX_DEPTH))
                if elements > _MAX_SVG_ELEMENTS:
                    raise ValueError(tr("SVG 元素超過 {limit} 個，請拆分文件後重試。", limit=f"{_MAX_SVG_ELEMENTS:,}"))
            else:
                depth -= 1
        root = parser.root
    except ET.ParseError as error:
        raise ValueError(tr("SVG 格式錯誤：{reason}", reason=error)) from error
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError(tr("檔案不是 SVG"))
    return root


def load_svg(filename, tolerance=0.05):
    """Import visible SVG paths/shapes, transforms and physical viewport units.

    Text, images, CSS stylesheets, references and clipping must be converted to
    plain paths first. Reject them instead of silently changing cutting geometry.
    """
    root = parse_svg_root(input_safety.read_bounded(filename, input_safety.MAX_SVG_BYTES))
    viewbox = root.get("viewBox")
    if viewbox:
        if any(index >= 4 for index, _ in enumerate(_SVG_TOKENS.finditer(viewbox))):
            raise ValueError(tr("SVG viewBox 無效"))
        box = [float(v) for v in re.split(r"[\s,]+", viewbox.strip())]
        if len(box) != 4 or not all(math.isfinite(v) for v in box) or min(box[2:]) <= 0:
            raise ValueError(tr("SVG viewBox 無效"))
        bx, by, bw, bh = box
        width = _length_mm(root.get("width", f"{bw}px"))
        height = _length_mm(root.get("height", f"{bh}px"))
        sx, sy = width / bw, height / bh
        aspect = root.get("preserveAspectRatio", "xMidYMid meet").split()
        dx = dy = 0
        if aspect != ["none"]:
            alignment = aspect[0]
            if alignment not in {f"x{x}Y{y}" for x in ("Min", "Mid", "Max") for y in ("Min", "Mid", "Max")} or (len(aspect) > 1 and aspect[1] != "meet"):
                raise ValueError(tr("請使用 SVG 預設 preserveAspectRatio 或 none"))
            scale = min(sx, sy)
            dx = (width - bw * scale) * {"Min": 0, "Mid": .5, "Max": 1}[alignment[1:4]]
            dy = (height - bh * scale) * {"Min": 0, "Mid": .5, "Max": 1}[alignment[5:8]]
            sx = sy = scale
        viewport = np.array([[sx, 0, dx - bx * sx], [0, sy, dy - by * sy], [0, 0, 1]])
    else:
        viewport = np.diag([25.4 / 96, 25.4 / 96, 1])

    result = []
    remaining = input_safety.MAX_POINTS
    source_tokens = 0
    ignored = {"defs", "metadata", "title", "desc", "namedview"}

    def reserve(count):
        nonlocal remaining
        if count > remaining:
            raise ValueError(tr("展開點數超過 {limit} 點，請簡化路徑或拆分文件後重試。", limit=f"{input_safety.MAX_POINTS:,}"))
        remaining -= count

    def visit(element, parent):
        nonlocal source_tokens
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in ignored:
            return
        style = dict(part.split(":", 1) for part in element.get("style", "").split(";") if ":" in part)
        style = {k.strip(): v.strip() for k, v in style.items()}
        if style.get("display", element.get("display")) == "none" or style.get("visibility", element.get("visibility")) == "hidden":
            return
        for attribute in ("clip-path", "mask", "filter"):
            if style.get(attribute, element.get(attribute, "none")) != "none":
                raise ValueError(tr("請先把 SVG 裁切遮罩、濾鏡轉成實際路徑"))
        # Bound source controls before svgpathtools builds segment objects.
        for attribute in ("transform", "d", "points"):
            for token in _SVG_TOKENS.finditer(element.get(attribute, "")):
                source_tokens += 1
                if source_tokens > _MAX_SVG_TOKENS:
                    raise ValueError(tr("SVG 路徑資料過大，請簡化或拆分文件後重試。"))
                if not token[0].isalpha() and not math.isfinite(float(token[0])):
                    raise ValueError(tr("SVG 包含無效座標或變形。"))
        matrix = parent @ parse_transform(element.get("transform"))
        if not np.isfinite(matrix).all():
            raise ValueError(tr("SVG 包含無效座標或變形。"))
        if tag in CONVERSIONS:
            try:
                path = transform(parse_path(CONVERSIONS[tag](element)), matrix)
            except (IndexError, StopIteration, OverflowError, AssertionError) as error:
                raise ValueError(tr("SVG 路徑資料無效，請檢查座標及曲線參數。")) from error
            for subpath in path.continuous_subpaths():
                reserve(1)
                points = [subpath.start]
                for segment in subpath:
                    if isinstance(segment, Line):
                        reserve(1)
                        points.append(segment.end)
                    else:
                        # Dense sampling targets tolerance millimetres per step;
                        # physical cutter accuracy still requires calibration.
                        steps = segment.length(error=1e-5) / tolerance
                        if not math.isfinite(steps) or steps > 200000:
                            raise ValueError(tr("SVG 曲線過大；請先縮小設計"))
                        count = max(2, math.ceil(steps))
                        reserve(count)
                        points.extend(segment.point(i / count) for i in range(1, count + 1))
                if len(points) > 1:
                    if not all(math.isfinite(p.real) and math.isfinite(p.imag) for p in points):
                        raise ValueError(tr("SVG 包含無效座標或變形。"))
                    result.append([(float(p.real), float(p.imag)) for p in points])
        elif tag == "g" or element is root:
            for child in element:
                visit(child, matrix)
        else:
            raise ValueError(tr("SVG 的 {tag} 尚未支援；請先在 Inkscape 轉成純向量路徑", tag=tag))

    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError(tr("曲線精度必須是正數"))
    visit(root, viewport)
    if not result:
        raise ValueError(tr("SVG 沒有可切割的向量路徑"))
    return result

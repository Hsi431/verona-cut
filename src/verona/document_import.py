"""Explicit PDF-page/SVG raster artwork import at physical size, without devices."""
import base64
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter
from PySide6.QtPdf import QPdfDocument, QPdfDocumentRenderOptions
from PySide6.QtSvg import QSvgRenderer, QtSvg

from .core import _length_mm, parse_svg_root
from .i18n import tr


def _pdf(path):
    doc = QPdfDocument()
    error = doc.load(str(path))
    if error != QPdfDocument.Error.None_ or not doc.pageCount():
        doc.close()
        raise ValueError(tr("無法讀取 PDF；加密或損壞的文件不支援。"))
    return doc


def pdf_page_count(path):
    doc = _pdf(path)
    try:
        return doc.pageCount()
    finally:
        doc.close()


def _svg(data):
    root = parse_svg_root(data)
    tags = {"svg", "g", "defs", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
            "text", "tspan", "use", "image", "linearGradient", "radialGradient", "stop", "mask", "style",
            "title", "desc", "metadata"}
    properties = {"fill", "fill-rule", "fill-opacity", "stroke", "stroke-width", "stroke-opacity",
                  "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray",
                  "stroke-dashoffset", "opacity", "font-family", "font-size", "font-style", "font-weight",
                  "text-anchor", "display", "visibility", "color", "stop-color", "stop-opacity", "mask"}
    attributes = properties | {"id", "class", "style", "transform", "x", "y", "width", "height", "viewBox",
        "preserveAspectRatio", "version", "baseProfile", "d", "points", "x1", "y1", "x2", "y2", "cx", "cy",
        "r", "rx", "ry", "fx", "fy", "offset", "gradientUnits", "gradientTransform", "spreadMethod",
        "maskUnits", "maskContentUnits", "dx", "dy", "rotate", "href", "space", "type"}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "metadata":
            element.clear()
    elements = list(root.iter())
    if len(elements) > 10000:
        raise ValueError("SVG exceeds 10000 elements.")
    ids = {el.get("id") for el in elements if el.get("id")}

    def declarations(text):
        for declaration in text.split(";"):
            if not declaration.strip():
                continue
            parts = declaration.split(":", 1)
            if len(parts) != 2 or parts[0].strip() not in properties or any(v in parts[1] for v in ("!", "\\", "@")):
                raise ValueError(tr("SVG 含不支援的樣式；請先轉換或展平。"))
            for target in re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", parts[1], re.I):
                if not target.startswith("#") or target[1:] not in ids:
                    raise ValueError(tr("SVG 外部引用不支援；請內嵌圖片並使用文件內引用。"))

    for el in elements:
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in tags:
            raise ValueError(tr("SVG 含不支援的元素：{tag}", tag=tag))
        if tag == "metadata":
            if list(el):
                # Editor metadata carries no drawable content; remove before Qt parsing.
                el.clear()
            continue
        for key, value in el.attrib.items():
            name = key.rsplit("}", 1)[-1]
            if key.startswith("{") and not key.startswith(("{http://www.w3.org/1999/xlink}", "{http://www.w3.org/XML/1998/namespace}")):
                continue
            if name not in attributes or "@" in value:
                raise ValueError(tr("SVG 含不支援的屬性：{name}", name=name))
            if name == "style":
                declarations(value)
            for target in re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", value, re.I):
                if not target.startswith("#") or target[1:] not in ids:
                    raise ValueError(tr("SVG 外部引用不支援；請內嵌圖片並使用文件內引用。"))
            if name == "href":
                if value.startswith("#") and value[1:] in ids:
                    pass
                elif tag == "image" and re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=\s]+", value):
                    raw = base64.b64decode(value.split(",", 1)[1], validate=False)
                    buffer = QBuffer()
                    buffer.setData(QByteArray(raw));buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                    reader = QImageReader(buffer)
                    if not reader.canRead() or reader.size().width()*reader.size().height() > 16_000_000:
                        raise ValueError("Invalid or oversized embedded SVG image.")
                else:
                    raise ValueError(tr("SVG 外部引用不支援；請內嵌圖片並使用文件內引用。"))
        if tag == "style":
            css = re.sub(r"/\*.*?\*/", "", el.text or "", flags=re.S)
            rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
            if re.sub(r"[^{}]+\{[^{}]*\}", "", css).strip():
                raise ValueError(tr("SVG 含不支援的樣式；請先轉換或展平。"))
            for selector, body in rules:
                if not re.fullmatch(r"\s*[.#]?[\w-]+(?:\s*,\s*[.#]?[\w-]+)*\s*", selector):
                    raise ValueError(tr("SVG 含不支援的樣式；請先轉換或展平。"))
                declarations(body)
                if re.search(r"\bmask\s*:", body):
                    raise ValueError("Use explicit mask attributes instead of stylesheet masks.")
    # Reject use/gradient href cycles, including referenced descendants.
    id_elements = {el.get("id"): el for el in elements if el.get("id")}
    visited = set()
    def check(identifier, stack):
        if identifier in stack or len(stack) >= 100:
            raise ValueError("Cyclic or excessively deep SVG reference.")
        if identifier in visited:
            return
        for child in id_elements[identifier].iter():
            href = child.get("href", child.get("{http://www.w3.org/1999/xlink}href", ""))
            if href.startswith("#"):
                check(href[1:], stack+[identifier])
            for value in child.attrib.values():
                for target in re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", value, re.I):
                    if target.startswith("#"):
                        check(target[1:], stack+[identifier])
        visited.add(identifier)
    for identifier in id_elements:
        check(identifier, [])
    viewbox = [float(v) for v in re.split(r"[\s,]+", root.get("viewBox", "").strip()) if v]
    if viewbox and (len(viewbox) != 4 or not all(math.isfinite(v) for v in viewbox) or min(viewbox[2:]) <= 0):
        raise ValueError(tr("SVG viewBox 無效"))
    default_width = f"{viewbox[2]}px" if viewbox else None
    default_height = f"{viewbox[3]}px" if viewbox else None
    if not root.get("width", default_width) or not root.get("height", default_height):
        raise ValueError("SVG needs physical width/height or a viewBox.")
    width, height = _length_mm(root.get("width", default_width)), _length_mm(root.get("height", default_height))
    return ET.tostring(root), width, height


def render_artwork(path, page=1, dpi=300):
    path = Path(path)
    if path.stat().st_size > 64*1024*1024:
        raise ValueError("Artwork files are limited to 64 MiB.")
    if type(page) is not int or page < 1 or type(dpi) is not int or not 75 <= dpi <= 600:
        raise ValueError("Choose a 1-based page and integer DPI from75 to600.")
    doc, renderer = None, None
    try:
        if path.suffix.lower() == ".pdf":
            doc = _pdf(path)
            if page > doc.pageCount():
                raise ValueError("PDF page is outside the document.")
            size = doc.pagePointSize(page-1)
            width, height = size.width()*25.4/72, size.height()*25.4/72
            pages = doc.pageCount()
        elif path.suffix.lower() == ".svg":
            if page != 1:
                raise ValueError("SVG has one page.")
            data, width, height = _svg(path.read_bytes())
            renderer = QSvgRenderer()
            renderer.setOptions(QtSvg.Option.DisableSMILAnimations | QtSvg.Option.DisableCSSAnimations)
            if not renderer.load(QByteArray(data)) or not renderer.isValid():
                raise ValueError("Qt could not render this SVG.")
            pages = 1
        else:
            raise ValueError("Choose a PDF or SVG artwork file.")
        if not all(math.isfinite(v) and 0 < v <= 300 for v in (width, height)):
            raise ValueError(tr("圖稿頁面需在 300 × 300 mm 工作區內；請先調整來源頁面。"))
        pixels = QSize(max(1, round(width*dpi/25.4)), max(1, round(height*dpi/25.4)))
        if pixels.width()*pixels.height() > 16_000_000:
            raise ValueError(tr("圖片尺寸超過 1600 萬像素"))
        if doc:
            options = QPdfDocumentRenderOptions()
            options.setRenderFlags(QPdfDocumentRenderOptions.RenderFlag.Annotations)
            image = doc.render(page-1, pixels, options)
        else:
            image = QImage(pixels, QImage.Format.Format_RGBA8888)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                renderer.render(painter, QRectF(0, 0, pixels.width(), pixels.height()))
            finally:
                painter.end()
        if image.isNull():
            raise ValueError("Could not render artwork.")
        image.setDotsPerMeterX(round(dpi/0.0254));image.setDotsPerMeterY(round(dpi/0.0254))
        return image, {"width_mm": width, "height_mm": height}, {"page": page, "pages": pages,
            "render_dpi": dpi, "rasterized": True, "pixels": [pixels.width(), pixels.height()]}
    finally:
        if doc:
            doc.close()

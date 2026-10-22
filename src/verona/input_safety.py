"""Budgets shared by saved-design readers and SVG imports."""
import json
import os
from pathlib import Path
import re

from .i18n import tr


MAX_DESIGN_BYTES = 256 * 1024 * 1024
MAX_SVG_BYTES = 64 * 1024 * 1024
MAX_DEPTH = 256
MAX_ITEMS = 2_000
MAX_POINTS = 2_000_000
MAX_JSON_TOKENS = 10_000_000
_JSON_DELIMITERS = re.compile(r'["\\{}\[\],:]')


def read_bounded(filename, limit):
    with Path(filename).open("rb") as source:
        # Check the opened file, then bound the read as well if it grows.
        if os.fstat(source.fileno()).st_size > limit:
            raise ValueError(tr("檔案超過 {limit} MiB，請拆分文件後重試。", limit=limit / 1024**2))
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError(tr("檔案超過 {limit} MiB，請拆分文件後重試。", limit=limit / 1024**2))
    return data


def read_design(filename):
    return decode_design(read_bounded(filename, MAX_DESIGN_BYTES))


def decode_design(data):
    """Apply the same JSON budgets to files and embedded editable documents."""
    if len(data) > MAX_DESIGN_BYTES:
        raise ValueError(tr("檔案超過 {limit} MiB，請拆分文件後重試。", limit=MAX_DESIGN_BYTES / 1024**2))
    text = data.decode("utf-8")
    depth = tokens = 0
    quoted, escape_at = False, -2
    # Scan punctuation without copying large embedded image strings. Brackets
    # and escaped quotes inside strings do not contribute to structure.
    for match in _JSON_DELIMITERS.finditer(text):
        value, position = match[0], match.start()
        if quoted:
            if position == escape_at + 1:
                escape_at = -2
            elif value == "\\":
                escape_at = position
            elif value == '"':
                quoted = False
            continue
        if value == '"':
            quoted = True
        elif value in "[{":
            depth += 1
            tokens += 1
            if depth > MAX_DEPTH:
                raise ValueError(tr("文件巢狀超過 {limit} 層，請減少巢狀後重試。", limit=MAX_DEPTH))
        elif value in "]}":
            depth -= 1
        elif value in ",:":
            tokens += 1
        if tokens > MAX_JSON_TOKENS:
            raise ValueError(tr("JSON 結構過大，請拆分文件後重試。"))
    try:
        return json.loads(text)
    except RecursionError as error:
        raise ValueError(tr("文件巢狀過深，無法讀取。")) from error


def check_geometry_budget(items):
    """Reject aggregate size before copying points or decoding any images."""
    if len(items) > MAX_ITEMS:
        raise ValueError(tr("圖稿物件超過 {limit} 個，請拆分文件後重試。", limit=f"{MAX_ITEMS:,}"))
    points = 0
    for item in items:
        if not isinstance(item, dict):
            continue  # The normal validator reports malformed object details.
        for key in ("contours", "image_clip"):
            contours = item.get(key)
            if not isinstance(contours, list):
                continue
            for contour in contours:
                if isinstance(contour, list):
                    points += len(contour)
                    if points > MAX_POINTS:
                        raise ValueError(tr("展開點數超過 {limit} 點，請簡化路徑或拆分文件後重試。", limit=f"{MAX_POINTS:,}"))

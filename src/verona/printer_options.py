"""Read-only CUPS queue option discovery for per-job print settings."""
import math
import shlex
import subprocess

from .i18n import tr


_MEDIA_NAMES = {
    "PLAIN": "普通紙",
    "PMMATT": "霧面相紙",
    "PMPHOTO": "光面相紙",
    "PSGLOS": "半光澤相紙",
    "HIGH": "高品質",
    "NORMAL": "標準",
}

_PAPER_SIZES = (
    ((210, 297), ("a4", "iso_a4_210x297mm")),
    ((148, 210), ("a5", "iso_a5_148x210mm")),
    ((105, 148), ("a6", "iso_a6_105x148mm")),
    ((176, 250), ("b5", "iso_b5_176x250mm")),
    ((215.9, 279.4), ("letter", "na_letter_8.5x11in", "usletter")),
    ((101.6, 152.4), ("4x6full", "4x6", "na_index-4x6_4x6in")),
    ((127, 177.8), ("5x7", "na_5x7_5x7in")),
)


def parse_lpoptions(output):
    """Parse ``lpoptions -l`` lines, retaining values and each driver's default."""
    parsed = {}
    if not isinstance(output, str):
        return parsed
    for line in output.splitlines():
        if ":" not in line:
            continue
        header, choices_text = line.split(":", 1)
        key, separator, label = header.partition("/")
        key = key.strip()
        if not key:
            continue
        try:
            tokens = shlex.split(choices_text)
        except ValueError:
            continue
        choices, default = [], None
        for token in tokens:
            selected = token.startswith("*")
            value = token[1:] if selected else token
            if value:
                choices.append(value)
                if selected:
                    default = value
        if choices:
            parsed[key] = {"label": label.strip() if separator else key,
                           "choices": choices, "default": default}
    return parsed


def read_printer_options(printer_name):
    """Return parsed queue options, or ``None`` when CUPS cannot report them."""
    if not isinstance(printer_name, str) or not printer_name or "\0" in printer_name:
        return None
    try:
        result = subprocess.run(["lpoptions", "-p", printer_name, "-l"],
                                check=True, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    options = parse_lpoptions(result.stdout)
    return options or None


def display_choice(value):
    """Translate known Epson media/quality tokens and preserve unknown codes."""
    parts = value.split("_") if isinstance(value, str) else []
    translated = [_MEDIA_NAMES.get(part.upper()) for part in parts]
    if not parts or any(part is None for part in translated):
        return value
    return " · ".join(tr(part) for part in translated)


def page_size_choice(options, width_mm, height_mm):
    """Find the CUPS PageSize value corresponding to a design's physical size."""
    if (isinstance(width_mm, bool) or isinstance(height_mm, bool)
            or not isinstance(width_mm, (int, float)) or not isinstance(height_mm, (int, float))
            or not math.isfinite(width_mm) or not math.isfinite(height_mm)):
        return None
    page = next(((key, value) for key, value in options.items()
                 if key.casefold() in ("pagesize", "page-size", "media-size")), None)
    if not page:
        return None
    page_key, page_option = page
    width, height = sorted((float(width_mm), float(height_mm)))
    for (size_width, size_height), aliases in _PAPER_SIZES:
        expected_width, expected_height = sorted((size_width, size_height))
        if abs(width-expected_width) > .5 or abs(height-expected_height) > .5:
            continue
        for alias in aliases:
            choice = next((item for item in page_option["choices"]
                           if item.casefold() == alias.casefold()), None)
            if choice is not None:
                return page_key, choice
        return None
    custom = next((choice for choice in page_option["choices"]
                   if choice.casefold() == "custom" or choice.casefold().startswith("custom.")), None)
    if custom is not None:
        return page_key, custom
    return None

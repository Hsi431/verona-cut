"""Extract the embedded LDS preview for Linux file-manager thumbnails."""
import argparse
from pathlib import Path
import sys
import zipfile

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImageReader


MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_PREVIEW_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_PIXELS = 16_000_000


def create_thumbnail(source, destination, size):
    if not 1 <= size <= 1024:
        raise ValueError("Thumbnail size must be between 1 and 1024 pixels.")
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("The thumbnail cannot overwrite the source design.")
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("LDS archive exceeds 64 MiB.")
    with zipfile.ZipFile(source) as archive:
        previews = [entry for entry in archive.infolist() if entry.filename == "PREVIEW"]
        if len(previews) != 1 or not 0 < previews[0].file_size <= MAX_PREVIEW_BYTES:
            raise ValueError("LDS has no unique, supported embedded preview.")
        with archive.open(previews[0]) as stream:
            data = stream.read(MAX_PREVIEW_BYTES + 1)
    if len(data) > MAX_PREVIEW_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("LDS preview is not a supported PNG.")
    buffer = QBuffer()
    buffer.setData(data)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer, b"PNG")
    dimensions = reader.size()
    if (not dimensions.isValid() or dimensions.isEmpty()
            or dimensions.width() * dimensions.height() > MAX_PREVIEW_PIXELS):
        raise ValueError("LDS preview dimensions exceed the supported limits.")
    if max(dimensions.width(), dimensions.height()) > size:
        reader.setScaledSize(dimensions.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise ValueError("Unable to decode the LDS preview.")
    if not image.save(str(destination), "PNG"):
        raise OSError("Unable to save the thumbnail.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("size", type=int)
    options = parser.parse_args(argv)
    try:
        create_thumbnail(options.source, options.destination, options.size)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as error:
        print(f"Verona thumbnail: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

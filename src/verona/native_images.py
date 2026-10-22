"""Small standard-library codecs for images embedded in native designs."""


_QOI_END_MARKER = b"\0\0\0\0\0\0\0\1"
_MAX_QOI_PIXELS = 16_000_000


def encode_qoi(width: int, height: int, rgba: bytes) -> bytes:
    """Encode unpremultiplied RGBA pixels using QOI's lossless chunk format."""
    if (type(width) is not int or type(height) is not int or min(width, height) <= 0
            or width * height > _MAX_QOI_PIXELS or len(rgba) != width * height * 4):
        raise ValueError("invalid QOI dimensions or RGBA length")
    output = bytearray(b"qoif" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x04\x00")
    index = [None] * 64
    previous = (0, 0, 0, 255)
    run = 0
    for position in range(0, len(rgba), 4):
        pixel = tuple(rgba[position:position + 4])
        if pixel == previous:
            run += 1
            if run == 62 or position + 4 == len(rgba):
                output.append(0xC0 | (run - 1))
                run = 0
            continue
        if run:
            output.append(0xC0 | (run - 1))
            run = 0
        red, green, blue, alpha = pixel
        slot = (red * 3 + green * 5 + blue * 7 + alpha * 11) % 64
        if index[slot] == pixel:
            output.append(slot)
        else:
            index[slot] = pixel
            dr, dg, db = (value - old for value, old in zip(pixel[:3], previous[:3]))
            if alpha != previous[3]:
                output.extend((0xFF, *pixel))
            elif all(-2 <= value <= 1 for value in (dr, dg, db)):
                output.append(0x40 | ((dr + 2) << 4) | ((dg + 2) << 2) | (db + 2))
            elif -32 <= dg <= 31 and -8 <= dr - dg <= 7 and -8 <= db - dg <= 7:
                output.extend((0x80 | (dg + 32), ((dr - dg + 8) << 4) | (db - dg + 8)))
            else:
                output.extend((0xFE, red, green, blue))
        previous = pixel
    output.extend(_QOI_END_MARKER)
    return bytes(output)


def decode_qoi(data: bytes) -> tuple[int, int, bytes]:
    """Decode a QOI image to width, height, and unpremultiplied RGBA bytes."""
    if len(data) < 22 or data[:4] != b"qoif":
        raise ValueError("invalid or truncated QOI header")

    width = int.from_bytes(data[4:8], "big")
    height = int.from_bytes(data[8:12], "big")
    channels, colorspace = data[12:14]
    pixel_count = width * height
    if not width or not height or pixel_count > _MAX_QOI_PIXELS:
        raise ValueError("invalid QOI dimensions")
    if channels not in (3, 4) or colorspace not in (0, 1):
        raise ValueError("invalid QOI channel or colorspace value")
    if data[-8:] != _QOI_END_MARKER:
        raise ValueError("invalid QOI end marker")

    pixels_end = len(data) - 8
    position = 14
    red, green, blue, alpha = 0, 0, 0, 255
    index = [(0, 0, 0, 0)] * 64
    output = bytearray()
    decoded = 0

    while decoded < pixel_count:
        if position >= pixels_end:
            raise ValueError("truncated QOI pixel data")

        opcode = data[position]
        position += 1
        run = 1

        if opcode == 0xFE:
            if position + 3 > pixels_end:
                raise ValueError("truncated QOI RGB chunk")
            red, green, blue = data[position:position + 3]
            position += 3
        elif opcode == 0xFF:
            if position + 4 > pixels_end:
                raise ValueError("truncated QOI RGBA chunk")
            red, green, blue, alpha = data[position:position + 4]
            position += 4
        elif opcode >> 6 == 0:
            red, green, blue, alpha = index[opcode & 0x3F]
        elif opcode >> 6 == 1:
            red = (red + ((opcode >> 4 & 0x03) - 2)) & 0xFF
            green = (green + ((opcode >> 2 & 0x03) - 2)) & 0xFF
            blue = (blue + ((opcode & 0x03) - 2)) & 0xFF
        elif opcode >> 6 == 2:
            if position >= pixels_end:
                raise ValueError("truncated QOI luma chunk")
            second = data[position]
            position += 1
            green_diff = (opcode & 0x3F) - 32
            red = (red + green_diff + ((second >> 4) - 8)) & 0xFF
            green = (green + green_diff) & 0xFF
            blue = (blue + green_diff + ((second & 0x0F) - 8)) & 0xFF
        else:
            run = (opcode & 0x3F) + 1
            if decoded + run > pixel_count:
                raise ValueError("QOI run exceeds image dimensions")

        pixel = bytes((red, green, blue, alpha))
        output.extend(pixel * run)
        decoded += run
        index[(red * 3 + green * 5 + blue * 7 + alpha * 11) % 64] = (red, green, blue, alpha)

    if position != pixels_end:
        raise ValueError("unexpected data after QOI pixels")
    return width, height, bytes(output)

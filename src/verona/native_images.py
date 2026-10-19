"""Small standard-library decoders for images embedded in native designs."""


_QOI_END_MARKER = b"\0\0\0\0\0\0\0\1"
_MAX_QOI_PIXELS = 16_000_000


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

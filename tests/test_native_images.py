import unittest

from verona.native_images import decode_qoi


_END = b"\0\0\0\0\0\0\0\1"


def qoi(width, height, chunks, channels=4, colorspace=0):
    return (b"qoif" + width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + bytes((channels, colorspace)) + chunks + _END)


class QoiDecoderTests(unittest.TestCase):
    def test_rgb_and_rgba(self):
        self.assertEqual(decode_qoi(qoi(1, 1, b"\xfe\x12\x34\x56", channels=3)),
                         (1, 1, b"\x12\x34\x56\xff"))
        self.assertEqual(decode_qoi(qoi(1, 1, b"\xff\x12\x34\x56\x78")),
                         (1, 1, b"\x12\x34\x56\x78"))

    def test_index(self):
        pixel = (10, 20, 30, 255)
        index = (pixel[0] * 3 + pixel[1] * 5 + pixel[2] * 7 + pixel[3] * 11) % 64
        chunks = b"\xff\x0a\x14\x1e\xff" + bytes((index,))
        rgba = bytes(pixel) * 2
        self.assertEqual(decode_qoi(qoi(2, 1, chunks)), (2, 1, rgba))

    def test_diff_luma_and_run(self):
        self.assertEqual(decode_qoi(qoi(1, 1, b"\x7a")), (1, 1, b"\x01\x00\x00\xff"))
        self.assertEqual(decode_qoi(qoi(1, 1, b"\xa1\x7a")), (1, 1, b"\x00\x01\x03\xff"))
        self.assertEqual(decode_qoi(qoi(3, 1, b"\xc2")), (3, 1, b"\x00\x00\x00\xff" * 3))

    def test_rejects_bad_header_dimensions_and_limits(self):
        for data in (
            b"qoif",
            b"nope" + qoi(1, 1, b"\xc0")[4:],
            qoi(0, 1, b""),
            qoi(1, 0, b""),
            qoi(1, 1, b"", channels=2),
            qoi(1, 1, b"", colorspace=2),
            qoi(4001, 4000, b""),
        ):
            with self.subTest(data=data[:14]), self.assertRaises(ValueError):
                decode_qoi(data)

    def test_rejects_truncation_bad_runs_and_extra_data(self):
        for data in (
            qoi(1, 1, b"\xfe\x01\x02"),
            qoi(1, 1, b"\xff\x01\x02\x03"),
            qoi(1, 1, b"\x80"),
            qoi(1, 1, b"\xc1"),
            qoi(1, 1, b"\xc0\x00"),
            qoi(1, 1, b""),
            qoi(1, 1, b"")[:-8] + b"\0\0\0\0\0\0\0\0",
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                decode_qoi(data)


if __name__ == "__main__":
    unittest.main()

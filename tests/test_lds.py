import base64
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from verona import lds
from verona.lds import load_lds


IDENTITY = (0, 0, 1, 0, 0, 1)
FILE_HEADER = b"DLCF" + struct.pack("<4I", 0x0E47C30A, 0, 1, 3)


def record(code, kind, identifier, payload=b"", marker=0):
    return struct.pack("<8I", 0x1E4E9CAD, code, kind, len(payload), identifier, marker, 0, 0) + payload


def common(color=0xFF000000, matrix=IDENTITY):
    data = bytearray(106)
    struct.pack_into("<4f", data, 4, 0, 0, 20, 10)
    struct.pack_into("<I", data, 20, color)
    struct.pack_into("<6f", data, 24, *matrix)
    return bytes(data)


def curve(points, matrix=IDENTITY):
    return struct.pack("<I6fBB", len(points), *matrix, 0, 0) + b"".join(
        struct.pack("<ffI", *point) for point in points)


def vector(points, matrix=IDENTITY, color=0xFF000000):
    return (record(1011, 3, 2) + record(0, 100, 3, common(color, matrix))
            + record(0, 11, 4, curve(points, matrix)) + record(1011, 4, 2))


def document(content):
    return FILE_HEADER + record(1000, 3, 1) + content + record(1000, 4, 1)


class NativeDocumentTests(unittest.TestCase):
    def load(self, data, preview=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.lds"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("CDOC", data)
                if preview is not None:
                    archive.writestr("PREVIEW", preview)
            original = path.read_bytes()
            result = load_lds(path)
            self.assertEqual(path.read_bytes(), original)
            return result

    def test_cubic_wire_order_and_affine_transform(self):
        # The native iterator consumes the type-2 point and the next two
        # points. Type-3 is used for both the second control and endpoint.
        points = [(0, 0, 16), (0, 5, 2), (5, 5, 3), (5, 0, 131)]
        result = self.load(document(vector(points, (10, 20, 2, 0, 0, 2))))
        contours = result.items[0]["contours"]
        self.assertEqual(len(contours), 1)
        self.assertEqual(contours[0][0], (10, 20))
        self.assertEqual(contours[0][-1], (10, 20))
        self.assertEqual(contours[0][-2], (20, 20))
        self.assertAlmostEqual(max(y for x, y in contours[0]), 27.5)
        self.assertTrue(result.items[0]["cut"])

    def test_subpaths_keep_open_travel_separate_from_closed_shapes(self):
        points = [(0, 0, 16), (5, 0, 33), (10, 0, 16), (12, 0, 1), (12, 2, 129)]
        result = self.load(document(vector(points)))
        self.assertEqual(result.items[0]["contours"], [
            [(0, 0), (5, 0)], [(10, 0), (12, 0), (12, 2), (10, 0)]])

    def test_shared_image_mask_and_cut_curve_have_distinct_roles(self):
        qoi = b"qoif" + struct.pack(">IIBB", 1, 1, 4, 0) + b"\xff\xff\x00\x00\xff" + b"\0" * 7 + b"\1"
        key = struct.pack("<Q", 42)
        mask = [(0, 0, 16), (1, 0, 1), (1, 1, 1), (0, 1, 129)]
        cut = [(0, 0, 16), (2, 0, 1), (2, 2, 1), (0, 2, 129)]
        parts = []
        for identifier, matrix in ((2, (10, 20, 2, 0, 0, 2)), (10, (20, 20, 2, 0, 0, 2))):
            parts.extend((record(1012, 3, identifier),
                          record(0, 100, identifier + 1, common(0xFFFFFFFF, matrix)),
                          record(0, 11, identifier + 2, curve(mask, matrix), 101),
                          record(0, 11, identifier + 3, curve(cut, matrix), 102),
                          record(0, 26 if identifier == 2 else 25, identifier + 4, key)))
            if identifier == 2:
                parts.append(record(0, 10, identifier + 5, qoi))
            parts.append(record(1012, 4, identifier))
        result = self.load(document(b"".join(parts)))
        self.assertEqual(len(result.items), 2)
        first, second = result.items
        self.assertIs(first["image_data"], second["image_data"])
        self.assertEqual(base64.b64decode(first["image_data"]), qoi)
        self.assertEqual(first["image_transform"], [10, 20, 2, 0, 0, 2])
        self.assertEqual(max(x for x, y in first["image_clip"][0]), 12)
        self.assertEqual(max(x for x, y in first["contours"][0]), 14)
        self.assertTrue(first["cut"])

    def test_colored_logo_cut_curve_is_enabled_but_mask_only_is_not(self):
        qoi = b"qoif" + struct.pack(">IIBB", 1, 1, 4, 0) + b"\xff\xff\x00\x00\xff" + b"\0" * 7 + b"\1"
        points = [(0, 0, 16), (1, 0, 1), (1, 1, 1), (0, 1, 129)]
        for color in (0xFF000000, 0xFFFFFFFF, 0xFFFBD347, 0xFFED5D00):
            for has_cut in (False, True):
                with self.subTest(color=hex(color), has_cut=has_cut):
                    records = (record(1012, 3, 2) + record(0, 100, 3, common(color))
                               + record(0, 11, 4, curve(points), 101)
                               + record(0, 26, 5, struct.pack("<Q", 42))
                               + record(0, 10, 6, qoi))
                    if has_cut:
                        records += record(0, 11, 7, curve(points), 102)
                    result = self.load(document(records + record(1012, 4, 2)))
                    self.assertEqual(result.items[0]["cut"], has_cut)
                    self.assertEqual(result.items[0]["source_color"], color)

    def test_legacy_arms_guides_are_preserved_and_excluded_from_cuts(self):
        points = [(0, 0, 16), (10, 0, 1)]
        group = (record(1014, 3, 20) + record(0, 100, 21, common())
                 + record(0, 120, 22, b"\1" + b"\0" * 10)
                 + vector(points) + record(1014, 4, 20))
        result = self.load(document(group))
        self.assertFalse(result.items[0]["cut"])
        self.assertTrue(result.items[0]["registration_guide"])
        self.assertEqual(result.metadata["registration"][0]["bounds"], [0, 0, 20, 10])

    def test_non_arms_group_modes_are_not_misread_as_boolean(self):
        state = bytes.fromhex("0000000200000002000000")
        group = (record(1014, 3, 20) + record(0, 100, 21, common())
                 + record(0, 120, 22, state)
                 + vector([(0, 0, 16), (10, 0, 1)]) + record(1014, 4, 20))
        result = self.load(document(group))
        self.assertEqual(result.metadata["registration"], [])
        self.assertTrue(result.items[0]["cut"])
        self.assertEqual(result.metadata["native_groups"][0]["group_state"], state.hex())

    def test_saved_print_cut_paper_and_page_marks_are_distinct_from_legacy_arms(self):
        media = bytearray(40)
        media[11] = 1
        struct.pack_into("<2f", media, 12, 210, 297)
        group = (record(1014, 3, 20) + record(0, 100, 21, common())
                 + record(0, 120, 22, bytes.fromhex("0000000200000002000000"))
                 + vector([(0, 0, 16), (10, 0, 1)]) + record(1014, 4, 20))
        result = self.load(document(record(0, 15, 30, bytes(media)) + group))
        self.assertEqual(result.metadata["registration"], [])
        self.assertEqual(result.metadata["print_cut"], {
            "enabled": True, "paper_width": 210, "paper_height": 297, "bounds": [0, 0, 20, 10]})
        media[11] = 2
        with self.assertRaises(ValueError):
            self.load(document(record(0, 15, 30, bytes(media)) + group))

    def test_bad_headers_lengths_object_types_and_cubic_data_are_rejected(self):
        valid = document(vector([(0, 0, 16), (10, 0, 1)]))
        malformed = (b"BAD!" + valid[4:], valid[:-1],
                     document(record(9999, 3, 2) + record(9999, 4, 2)),
                     document(vector([(0, 0, 16), (10, 0, 2)])),
                     document(vector([(0, 0, 16), (float("nan"), 0, 1)])))
        for data in malformed:
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                self.load(data)

    def test_hidden_layers_fail_instead_of_importing_hidden_cut_geometry(self):
        layers = b"\xef\xbb\xbf[layers]\nLayerCount=1\nL000.hidden=1\n"
        data = document(record(0, 14, 20, layers, 203) + vector([(0, 0, 16), (10, 0, 1)]))
        with self.assertRaises(ValueError):
            self.load(data)

    def test_empty_group_state_is_rejected_without_index_error(self):
        data = document(record(1014, 3, 20) + record(0, 120, 21)
                        + vector([(0, 0, 16), (1, 0, 1)]) + record(1014, 4, 20))
        with self.assertRaisesRegex(ValueError, "group state"):
            self.load(data)

    def test_deep_objects_are_rejected_before_recursive_visiting(self):
        content = (b"".join(record(1014, 3, i) for i in range(1100))
                   + vector([(0, 0, 16), (1, 0, 1)])
                   + b"".join(record(1014, 4, i) for i in reversed(range(1100))))
        with self.assertRaisesRegex(ValueError, "depth|層"):
            self.load(document(content))

    def test_structural_limits_accept_the_boundary_and_stop_before_curve_work(self):
        data = document(vector([(0, 0, 16), (1, 0, 1)]))
        for name, boundary in (("_MAX_RECORDS", 6), ("_MAX_OBJECTS", 2),
                               ("_MAX_DEPTH", 2), ("_MAX_ITEMS", 1)):
            with self.subTest(limit=name), patch.object(lds, name, boundary, create=True):
                self.assertEqual(len(self.load(data).items), 1)
            with self.subTest(over=name), patch.object(lds, name, boundary - 1, create=True), \
                    patch.object(lds, "_curve", wraps=lds._curve) as expand:
                with self.assertRaises(ValueError):
                    self.load(data)
                expand.assert_not_called()

    def test_point_budget_is_document_wide_and_counts_closure(self):
        points = [(0, 0, 16), (1, 0, 1), (1, 1, 129)]
        data = document(vector(points) * 2)
        with patch.object(lds, "_MAX_POINTS", 8, create=True):
            result = self.load(data)
            self.assertEqual(sum(len(c) for item in result.items for c in item["contours"]), 8)
        with patch.object(lds, "_MAX_POINTS", 7, create=True), self.assertRaisesRegex(ValueError, "point|點"):
            self.load(data)

    def test_cubic_budget_is_checked_before_interpolation(self):
        data = document(vector([(0, 0, 16), (0, 5, 2), (5, 5, 3), (5, 0, 3)]))
        with patch.object(lds, "_MAX_POINTS", 301, create=True):
            self.assertEqual(len(self.load(data).items[0]["contours"][0]), 301)
        with patch.object(lds, "_MAX_POINTS", 10, create=True), self.assertRaisesRegex(ValueError, "point|點"):
            self.load(data)

    def test_extremely_small_tolerance_is_rejected_without_overflow(self):
        payload = curve([(0, 0, 16), (0, 5, 2), (5, 5, 3), (5, 0, 3)])
        with self.assertRaisesRegex(ValueError, "cubic"):
            lds._curve(payload, 1e-320, lds._PointBudget(1000))

    def test_fallback_image_rectangle_uses_the_same_point_budget(self):
        qoi = b"qoif" + struct.pack(">IIBB", 1, 1, 4, 0) + b"\xc0" + b"\0" * 7 + b"\1"
        image = (record(1012, 3, 2) + record(0, 100, 3, common())
                 + record(0, 26, 4, struct.pack("<Q", 42)) + record(0, 10, 5, qoi)
                 + record(1012, 4, 2))
        with patch.object(lds, "_MAX_POINTS", 5):
            self.assertEqual(len(self.load(document(image)).items[0]["contours"][0]), 5)
        with patch.object(lds, "_MAX_POINTS", 4), self.assertRaisesRegex(ValueError, "point|點"):
            self.load(document(image))

    def test_clip_cut_and_registration_share_the_point_budget(self):
        qoi = b"qoif" + struct.pack(">IIBB", 1, 1, 4, 0) + b"\xc0" + b"\0" * 7 + b"\1"
        line = [(0, 0, 16), (1, 0, 1)]
        image = (record(1012, 3, 2) + record(0, 100, 3, common())
                 + record(0, 26, 4, struct.pack("<Q", 42)) + record(0, 10, 5, qoi)
                 + record(0, 11, 6, curve(line), 101) + record(0, 11, 7, curve(line), 102)
                 + record(1012, 4, 2))
        guide = (record(1014, 3, 20) + record(0, 120, 21, b"\1")
                 + vector(line) + record(1014, 4, 20))
        with patch.object(lds, "_MAX_POINTS", 6, create=True):
            self.assertEqual(len(self.load(document(image + guide)).items), 2)
        with patch.object(lds, "_MAX_POINTS", 5, create=True), self.assertRaisesRegex(ValueError, "point|點"):
            self.load(document(image + guide))

    def test_malformed_layer_settings_raise_value_error(self):
        for layers in (b"[other]\nx=1", b"not an ini", b"[layers]\nx=%invalid", b"\xff"):
            with self.subTest(layers=layers), self.assertRaises(ValueError):
                self.load(document(record(0, 14, 20, layers, 203) + vector([(0, 0, 16), (1, 0, 1)])))

    def test_nested_groups_survive_without_grouping_the_entire_page_or_duplicate_cuts(self):
        from verona.grouping import expand_selection, ungroup_items
        line = [(20, 20, 16), (30, 20, 1)]
        nested = (record(1021, 3, 30) + record(0, 100, 31, common())
                  + record(0, 11, 32, curve(line)) + vector(line) + record(1021, 4, 30))
        ordinary = (record(1014, 3, 20) + record(0, 100, 21, common())
                    + vector([(40, 40, 16), (50, 40, 1)]) + nested + record(1014, 4, 20))
        page = (record(1014, 3, 10) + record(0, 100, 11, common())
                + record(0, 120, 12, bytes.fromhex("0000000200000002000000"))
                + ordinary + vector([(60, 60, 16), (70, 60, 1)]) + record(1014, 4, 10))
        items = self.load(document(page)).items
        self.assertEqual(len(items), 3)
        self.assertEqual(len(items[0]["group_path"]), 1)
        self.assertEqual(len(items[1]["group_path"]), 2)
        self.assertNotIn("group_path", items[2])
        self.assertEqual(expand_selection(items, [1]), {0, 1})
        self.assertEqual(items[1]["contours"], [[(20., 20.), (30., 20.)]])
        self.assertTrue(ungroup_items(items, [0, 1]))
        self.assertEqual(expand_selection(items, [1]), {1})

    def test_unrepresented_logo_group_curve_and_unknown_transforms_layers_reject(self):
        line = [(20, 20, 16), (30, 20, 1)]
        logo = (record(1021, 3, 30) + record(0, 100, 31, common())
                + record(0, 11, 32, curve(line)) + vector([(40, 40, 16), (50, 40, 1)])
                + record(1021, 4, 30))
        transformed = (record(1014, 3, 20) + record(0, 100, 21, common(matrix=(2, 3, 1, 0, 0, 1)))
                       + vector(line) + record(1014, 4, 20))
        layers = record(0, 14, 20, b"[layers]\nLayerCount=2\n", 203) + vector(line)
        for payload in (logo, transformed, layers):
            with self.assertRaises(ValueError):
                self.load(document(payload))
        noncut = (record(1021, 3, 30) + record(0, 100, 31, common())
                  + record(0, 11, 32, curve(line)) + vector(line, color=0xFF808080)
                  + record(1021, 4, 30))
        result = self.load(document(noncut))
        self.assertEqual(len(result.items), 1)
        self.assertFalse(result.items[0]["cut"])


if __name__ == "__main__":
    unittest.main()

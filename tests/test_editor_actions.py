"""Geometry and print-fidelity regressions for agent editing operations."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from PySide6.QtWidgets import QApplication

from verona.artwork import draw_bleed, item_bounds
from verona.commands import encode_plt
from verona.editor_actions import build_edit
from verona.raster_import import _png_base64
from verona.printing import write_print_pdf
from verona.ui import VeronaWindow, _transform_item


def rectangle(name="pattern", x=20, y=20, width=20, height=10, **extra):
    return {"name": name, "contours": [[(x, y), (x+width, y), (x+width, y+height),
                                        (x, y+height), (x, y)]], "cut": True, **extra}


def image_item(**extra):
    image = QImage(20, 10, QImage.Format.Format_RGBA8888)
    image.fill(QColor("black"))
    item = rectangle(image_data=_png_base64(image),
                     image_transform=[20, 20, 1, 0, 0, 1],
                     source_color=0xFF00FF00)
    item["image_clip"] = copy.deepcopy(item["contours"])
    item.update(extra)
    return item


class EditorActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings.cleanup()

    def test_fill_respects_adjusted_frame_spacing_obstacles_and_source(self):
        source = [rectangle(), rectangle("obstacle", 11, 11, 20, 10)]
        original = copy.deepcopy(source)
        metadata = {"print_cut": {"enabled": True, "bounds": [10, 10, 100, 100],
                                  "offset": [.1, -.1]}}
        result, selection, details = build_edit(source, metadata, [0], "fill_area",
                                                 {"gap_mm": 2, "margin_mm": 1})
        self.assertEqual(source, original)
        self.assertEqual(result[0], source[1])
        self.assertGreater(details["copies"], 1)
        self.assertEqual(len(selection), details["copies"])
        for i, index in enumerate(selection):
            bounds = item_bounds(result[index])
            self.assertFalse(bounds.intersects(item_bounds(result[0])))
            for other in selection[i+1:]:
                self.assertFalse(bounds.intersects(item_bounds(result[other])))
        payload = encode_plt([contour for item in result for contour in item["contours"]],
                             registration_bounds=metadata["print_cut"]["bounds"],
                             registration_offset=metadata["print_cut"]["offset"])
        self.assertIn(b"TB26", payload)

    def test_fill_counts_print_bleed_and_keeps_copied_groups_independent(self):
        source = [image_item(bleed={"width_mm": 2., "color": "#00ff00"},
                             group_path=["original"])]
        metadata = {"print_cut": {"enabled": True, "bounds": [10, 10, 90, 65]}}
        result, selection, details = build_edit(source, metadata, [0], "fill_area", {"gap_mm": 2})
        # Three of the 3 x 3 grid slots would print over the registration marks.
        self.assertEqual(details["copies"], 6)
        self.assertEqual(len({tuple(item["group_path"]) for item in result}), 6)
        self.assertNotIn(["original"], [item["group_path"] for item in result])
        for i, item in enumerate(result):
            printed = item_bounds(item, printed=True)
            for other in result[i+1:]:
                self.assertFalse(printed.intersects(item_bounds(other, printed=True)))
        self.assertEqual(selection, list(range(6)))

    def test_batch_bleed_changes_printed_pixels_but_not_cuts_or_original_pixels(self):
        source = [image_item(), image_item(source_color=0xFFFF0000)]
        original = copy.deepcopy(source)
        before = encode_plt([c for item in source for c in item["contours"]])
        result, _, _ = build_edit(source, {}, [0, 1], "bleed", {"width_mm": 2, "color": "source"})
        self.assertEqual(source, original)
        self.assertEqual(result[0]["bleed"]["color"], "#00ff00")
        self.assertEqual(result[1]["bleed"]["color"], "#ff0000")
        self.assertEqual(before, encode_plt([c for item in result for c in item["contours"]]))
        for old, new in zip(source, result):
            for key in ("contours", "image_clip", "image_data", "image_transform"):
                self.assertEqual(old[key], new[key])
        image = QImage(80, 60, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        draw_bleed(painter, result[0])
        painter.end()
        self.assertEqual(image.pixelColor(19, 25), QColor("#00ff00"))
        self.assertEqual(image.pixelColor(16, 25), QColor("white"))
        document = {"format": "verona", "version": 1, "units": "mm", "items": result}
        saved, _ = VeronaWindow._validate_document(document)
        self.assertEqual(saved[0]["bleed"], result[0]["bleed"])
        removed, _, _ = build_edit(result, {}, [0, 1], "bleed", {"width_mm": 0, "color": "source"})
        self.assertEqual(removed, original)

    def test_transform_and_duplicate_keep_image_mask_and_physical_bleed(self):
        source = [image_item(bleed={"width_mm": 1., "color": "#00ff00"},
                             group_path=["outer", "inner"])]
        result, selected, _ = build_edit(source, {}, [0], "transform",
                                          {"dx_mm": 3, "dy_mm": 4, "scale": 2, "rotation_deg": 90})
        self.assertEqual(result[0]["bleed"]["width_mm"], 1)
        self.assertEqual(result[0]["image_clip"], result[0]["contours"])
        tx, ty, a, b, c, d = result[0]["image_transform"]
        self.assertEqual((tx, ty), (43, 9))
        self.assertEqual(selected, [0])
        copies, selected, _ = build_edit(source, {}, [0], "duplicate", {"dx_mm": 50, "dy_mm": 0})
        self.assertEqual(selected, [1])
        self.assertEqual(copies[0], source[0])
        self.assertNotEqual(copies[0]["group_path"], copies[1]["group_path"])
        self.assertAlmostEqual(item_bounds(copies[1]).left(), 70)

    def test_bleed_tracks_cut_edges_without_filling_transparent_interior(self):
        source = image_item()
        source["contours"] = rectangle(x=19.5, y=19.5, width=21, height=11)["contours"]
        result, _, _ = build_edit([source], {}, [0], "bleed",
                                  {"width_mm": 1, "color": "#00ff00"})
        self.assertEqual(result[0]["contours"], source["contours"])
        printed = item_bounds(result[0], printed=True)
        self.assertAlmostEqual(printed.left(), 18.5)
        self.assertAlmostEqual(printed.right(), 41.5)
        image = QImage(1600, 1200, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.white)
        painter = QPainter(image)
        painter.scale(20, 20)
        draw_bleed(painter, result[0])
        painter.end()
        self.assertEqual(image.pixelColor(375, 500), QColor("#00ff00"))
        self.assertEqual(image.pixelColor(600, 500), QColor("white"))
        self.assertEqual(image.pixelColor(350, 500), QColor("white"))
        disabled, _, _ = build_edit(result, {}, [0], "set_cut", {"enabled": False})
        uncut = QImage(1600, 1200, QImage.Format.Format_RGB32)
        uncut.fill(Qt.GlobalColor.white)
        painter = QPainter(uncut)
        painter.scale(20, 20)
        draw_bleed(painter, disabled[0])
        painter.end()
        self.assertEqual(uncut, image)

    def test_group_cut_flags_and_delete_work_on_complete_groups(self):
        source = [rectangle("a"), rectangle("b", x=60)]
        grouped, _, _ = build_edit(source, {}, [0, 1], "group", {})
        self.assertEqual(grouped[0]["group_path"], grouped[1]["group_path"])
        disabled, selected, _ = build_edit(grouped, {}, [0], "set_cut", {"enabled": False})
        self.assertEqual(selected, [0, 1])
        self.assertTrue(all(not item["cut"] for item in disabled))
        ungrouped, _, _ = build_edit(grouped, {}, [0], "ungroup", {})
        self.assertTrue(all("group_path" not in item for item in ungrouped))
        deleted, selected, _ = build_edit(grouped, {}, [0], "delete", {})
        self.assertEqual((deleted, selected), ([], []))

    def test_invalid_or_impossible_plans_leave_sources_unchanged(self):
        source = [image_item(), rectangle("guide", registration_guide=True)]
        original = copy.deepcopy(source)
        cases = [
            ([True], "delete", {}), ([99], "delete", {}), ([1], "delete", {}),
            ([0], "bleed", {"width_mm": float("nan"), "color": "#ff0000"}),
            ([0], "bleed", {"width_mm": 1, "color": "red"}),
            ([0], "fill_area", {"max_copies": True}),
            ([0], "transform", {"scale": 0}), ([0], "transform", {"dx_mm": True}),
            ([0], "transform", {"unexpected": 1}), ([0], "send_job", {}),
        ]
        for indices, action, parameters in cases:
            with self.subTest(action=action, parameters=parameters), self.assertRaises(ValueError):
                build_edit(source, {}, indices, action, parameters)
            self.assertEqual(source, original)
        with self.assertRaises(ValueError):
            build_edit(source, {"print_cut": {"enabled": True, "bounds": [10, 10, 15, 15]}},
                       [0], "fill_area", {})

    def test_fill_copy_limit_is_reported_without_overstating_capacity(self):
        result, _, details = build_edit([rectangle()], {}, [0], "fill_area",
                                         {"max_copies": 3})
        self.assertEqual(len(result), 3)
        self.assertTrue(details["limit_reached"])
        self.assertGreater(details["grid_candidates"], details["copies"])

    def test_bleed_roundtrips_and_changes_canvas_and_pdf_without_changing_plt(self):
        from PySide6.QtPdf import QPdfDocument
        window, reopened = VeronaWindow(), VeronaWindow()
        window.items = [image_item()]
        window.metadata = {"print_cut": {"enabled": True, "paper_width": 100,
                                          "paper_height": 100, "bounds": [6, 6, 94, 94]}}
        window.refresh_all()
        before = window._encoded_plt()
        try:
            with tempfile.TemporaryDirectory() as folder:
                first, second = Path(folder) / "before.pdf", Path(folder) / "after.pdf"
                write_print_pdf(first, window.items, window.metadata)
                items, _, _ = build_edit(window.items, window.metadata, [0], "bleed",
                                          {"width_mm": 1, "color": "#00ff00"})
                window.items = items
                window.refresh_all()
                window.canvas.set_zoom(5)
                self.assertEqual(window._encoded_plt(), before)
                canvas = window.canvas.grab().toImage()
                self.assertEqual(canvas.pixelColor(round(34+19.5*5), round(34+25.5*5)),
                                 QColor("#00ff00"))
                write_print_pdf(second, window.items, window.metadata)
                colors = []
                for filename in (first, second):
                    pdf = QPdfDocument()
                    pdf.load(str(filename))
                    image = pdf.render(0, QSize(1000, 1000))
                    self.assertFalse(image.isNull())
                    colors.append(image.pixelColor(195, 255))
                    pdf.close()
                self.assertEqual(colors, [QColor("white"), QColor("#00ff00")])
                window.current_file = str(Path(folder) / "with-bleed.jdesign")
                self.assertTrue(window.save_design())
                self.assertTrue(reopened._load_document(window.current_file))
                self.assertEqual(reopened.items[0]["bleed"], {"width_mm": 1., "color": "#00ff00"})
                self.assertEqual(reopened._encoded_plt(), before)
                # Repaint uses the cached ring; width and geometry edits must
                # invalidate it without recoloring the old location.
                self.assertEqual(window.canvas.grab().toImage(), canvas)
                window.items[0]["bleed"]["width_mm"] = 2
                wider = window.canvas.grab().toImage()
                self.assertEqual(wider.pixelColor(round(34+18.5*5), round(34+25.5*5)),
                                 QColor("#00ff00"))
                _transform_item(window.items[0], QTransform.fromTranslate(10, 0))
                moved = window.canvas.grab().toImage()
                self.assertNotEqual(moved.pixelColor(round(34+18.5*5), round(34+25.5*5)),
                                    QColor("#00ff00"))
                self.assertEqual(moved.pixelColor(round(34+28.5*5), round(34+25.5*5)),
                                 QColor("#00ff00"))
        finally:
            for current in (window, reopened):
                with patch.object(current, "_confirm_unsaved_changes", return_value="discard"):
                    current.close()

    def test_fill_keeps_printed_copies_clear_of_registration_marks(self):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPainterPath, QPolygonF
        from verona.printing import registration_geometry
        metadata = {"print_cut": {"enabled": True, "bounds": [6, 6, 204, 291]}}
        marks = QPainterPath()
        for polygon in registration_geometry(metadata["print_cut"]["bounds"]):
            marks.addPolygon(QPolygonF([QPointF(*point) for point in polygon]))
            marks.closeSubpath()
        for gap in (0, 2):
            result, selection, details = build_edit([image_item()], metadata, [0], "fill_area",
                                                    {"gap_mm": gap, "margin_mm": 0})
            self.assertGreater(details["copies"], 0)
            for index in selection:
                box = item_bounds(result[index], printed=True)
                with self.subTest(gap=gap, index=index):
                    self.assertFalse(marks.intersects(box.adjusted(-gap*.95, -gap*.95, gap*.95, gap*.95)))
        # Cut-only copies print nothing, so they may still start at the frame corner.
        result, selection, _ = build_edit([rectangle()], metadata, [0], "fill_area", {"gap_mm": 2})
        self.assertAlmostEqual(item_bounds(result[selection[0]]).left(), 6, places=4)


if __name__ == "__main__":
    unittest.main()

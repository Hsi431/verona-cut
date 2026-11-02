"""Independent correctness checks for bounded contour nesting."""
import math
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from verona.editor_actions import cutting_frame
from verona.editor_document import build_document_edit
from verona.packing import pack_items
from verona.printing import registration_geometry
from tests.fixtures.nesting_cases import rectangle, scenarios
from verona.ui import PackingDialog, VeronaWindow


def _segments(contour):
    points = [(float(x), float(y)) for x, y in contour]
    if len(points) < 2:
        return []
    pairs = list(zip(points, points[1:]))
    if points[0] != points[-1]:
        pairs.append((points[-1], points[0]))
    return pairs


def _cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def _point_segment(point, segment):
    (x, y), ((x1, y1), (x2, y2)) = point, segment
    dx, dy = x2-x1, y2-y1
    denominator = dx*dx+dy*dy
    amount = 0 if not denominator else max(0, min(1, ((x-x1)*dx+(y-y1)*dy)/denominator))
    return math.hypot(x-x1-amount*dx, y-y1-amount*dy)


def _segment_distance(first, second, threshold):
    (a, b), (c, d) = first, second
    if (max(min(a[0], b[0]), min(c[0], d[0]))
            > min(max(a[0], b[0]), max(c[0], d[0]))+threshold
            or max(min(a[1], b[1]), min(c[1], d[1]))
            > min(max(a[1], b[1]), max(c[1], d[1]))+threshold):
        return threshold+1
    ab_c, ab_d = _cross(a, b, c), _cross(a, b, d)
    cd_a, cd_b = _cross(c, d, a), _cross(c, d, b)
    on_ab = lambda p: (min(a[0], b[0])-1e-9 <= p[0] <= max(a[0], b[0])+1e-9
                       and min(a[1], b[1])-1e-9 <= p[1] <= max(a[1], b[1])+1e-9)
    on_cd = lambda p: (min(c[0], d[0])-1e-9 <= p[0] <= max(c[0], d[0])+1e-9
                       and min(c[1], d[1])-1e-9 <= p[1] <= max(c[1], d[1])+1e-9)
    if (ab_c*ab_d < 0 and cd_a*cd_b < 0
            or abs(ab_c) < 1e-9 and on_ab(c) or abs(ab_d) < 1e-9 and on_ab(d)
            or abs(cd_a) < 1e-9 and on_cd(a) or abs(cd_b) < 1e-9 and on_cd(b)):
        return 0
    return min(_point_segment(a, second), _point_segment(b, second),
               _point_segment(c, first), _point_segment(d, first))


def _inside(point, contours):
    x, y = point
    for contour in contours:
        points = [(float(px), float(py)) for px, py in contour]
        contained = False
        for (x1, y1), (x2, y2) in zip(points, points[1:]+points[:1]):
            if (y1 > y) != (y2 > y) and x < (x2-x1)*(y-y1)/(y2-y1)+x1:
                contained = not contained
        if contained:
            return True
    return False


def _shape_distance(first, second, threshold):
    first_segments = [edge for contour in first["contours"] for edge in _segments(contour)]
    second_segments = [edge for contour in second["contours"] for edge in _segments(contour)]
    if (first_segments and (_inside(first_segments[0][0], second["contours"])
                            or _inside(second_segments[0][0], first["contours"]))):
        return 0
    closest = threshold+1
    for a in first_segments:
        for b in second_segments:
            closest = min(closest, _segment_distance(a, b, closest))
            if closest < threshold-1e-7:
                return closest
    return closest


def _assert_independent_layout(test, items, selected, metadata, gap, margin=0):
    frame = cutting_frame(metadata).adjusted(margin, margin, -margin, -margin)
    selected_items = [items[index] for index in selected]
    for item in selected_items:
        bleed = item.get("bleed", {}).get("width_mm", 0)
        points = [point for contour in item["contours"] for point in contour]
        test.assertTrue(all(frame.left()+bleed-1e-6 <= x <= frame.right()-bleed+1e-6
                            and frame.top()+bleed-1e-6 <= y <= frame.bottom()-bleed+1e-6
                            for x, y in points), item["name"])
    for i, first in enumerate(selected_items):
        for second in selected_items[i+1:]:
            required = gap + first.get("bleed", {}).get("width_mm", 0) \
                + second.get("bleed", {}).get("width_mm", 0)
            test.assertGreaterEqual(_shape_distance(first, second, required), required-1e-6,
                                    (first["name"], second["name"]))
    registration = metadata.get("print_cut", {})
    if registration.get("enabled"):
        mark_contours = registration_geometry(registration["bounds"])
        for item in selected_items:
            if not item.get("image_data"):
                continue
            required = gap + item.get("bleed", {}).get("width_mm", 0)
            for mark in mark_contours:
                mark_item = {"contours": [mark]}
                test.assertGreaterEqual(_shape_distance(item, mark_item, required), required-1e-6,
                                        (item["name"], "registration mark"))


class PackingNestingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_six_fill_fixtures_have_independently_clear_geometries(self):
        for name, source, metadata, parameters in scenarios():
            with self.subTest(case=name):
                result, selected, details = pack_items(source, metadata, list(range(len(source))), parameters)
                _assert_independent_layout(self, result, selected, metadata,
                                           parameters["gap_mm"], parameters.get("margin_mm", 0))
                self.assertGreater(details["paper_utilization_percent"], 0)
                self.assertLessEqual(details["paper_utilization_percent"], 100)
                self.assertEqual(details["unplaced_count"], 0)
                self.assertEqual(details["page_count"], 1)

    def test_circle_fill_uses_a_more_productive_bounded_candidate_scan(self):
        _, source, metadata, parameters = scenarios()[0]
        _, _, details = pack_items(source, metadata, [0], parameters)
        self.assertGreaterEqual(details["copies"], 10)

    def test_fine_rotation_is_deterministic_and_reported(self):
        name, source, metadata, parameters = scenarios()[2]
        parameters = {**parameters, "rotation_step_deg": 15}
        first = pack_items(source, metadata, list(range(len(source))), parameters)
        second = pack_items(source, metadata, list(range(len(source))), parameters)
        self.assertEqual(first, second)
        self.assertTrue(all(placement["rotation_deg"] % 15 == 0
                            for placement in first[2]["placements"]))
        _assert_independent_layout(self, first[0], first[1], metadata, parameters["gap_mm"])

    def test_unplaced_requested_copies_can_continue_on_new_design_pages(self):
        source = [rectangle("large sticker", 70, 70)]
        metadata = {"print_cut": {"enabled": True, "paper_width": 120, "paper_height": 120,
                                  "bounds": [10, 10, 110, 110]}}
        result, selected, details = pack_items(source, metadata, [0],
            {"copies_per_unit": [3], "new_page_for_unplaced": True})
        self.assertEqual(details["page_count"], 3)
        self.assertEqual(len(details["new_pages"]), 2)
        self.assertEqual(details["copies"], 3)
        self.assertEqual(details["unplaced_count"], 0)
        _assert_independent_layout(self, result, selected, metadata, 2)
        for page in details["new_pages"]:
            _assert_independent_layout(self, page["items"], page["selection"], metadata, 2)

    def test_document_edit_keeps_mcp_overflow_pages_in_the_preview_plan(self):
        source = [rectangle("large sticker", 70, 70)]
        metadata = {"print_cut": {"enabled": True, "paper_width": 120, "paper_height": 120,
                                  "bounds": [10, 10, 110, 110]}}
        items, after_metadata, settings, selection, details = build_document_edit(
            source, metadata, {}, [0], "pack_objects",
            {"copies_per_unit": [3], "new_page_for_unplaced": True})
        self.assertEqual(details["page_count"], 3)
        self.assertEqual(len(details["new_pages"]), 2)
        self.assertEqual(len(items), 1)
        self.assertEqual(after_metadata, metadata)
        self.assertEqual(settings, {})
        self.assertEqual(len(selection), 1)

    def test_gui_applies_overflow_pages_with_matching_paper_settings_and_undo(self):
        window = VeronaWindow()
        source = [rectangle("large sticker", 70, 70)]
        metadata = {"print_cut": {"enabled": True, "paper_width": 120, "paper_height": 120,
                                  "bounds": [10, 10, 110, 110]}}
        window.items, window.metadata = source, metadata
        window.refresh_all()
        window.select_many([0])
        settings = window._cut_settings()
        try:
            def accept(dialog):
                dialog.copies_spin.setValue(3)
                dialog.pages_check.setChecked(True)
                dialog.preview()
                self.assertTrue(dialog.apply_button.isEnabled())
                return QDialog.DialogCode.Accepted

            with patch.object(PackingDialog, "exec", accept):
                window.pack_selected()
            self.assertEqual(len(window._documents), 3)
            for page in window._documents:
                self.assertEqual(page.metadata, metadata)
                self.assertEqual(page.settings, settings)
                self.assertEqual(len(page.undo_stack), 1)
                self.assertEqual(len(page.items), 1)
            window.tab_bar.setCurrentIndex(1)
            window.undo()
            self.assertEqual(window.items, [])
        finally:
            with patch.object(window, "_confirm_unsaved_changes", return_value="discard"):
                window.close()

    def test_time_limit_returns_current_best_and_names_unplaced_copy(self):
        source = [rectangle("limited sticker", 20, 20)]
        with patch("verona.packing.time.monotonic", side_effect=[0, 0, 20]):
            result, selected, details = pack_items(source, {}, [0], {"copies_per_unit": [2]})
        self.assertEqual(details["copies"], 1)
        self.assertEqual(details["unplaced_count"], 1)
        self.assertEqual(details["unplaced"][0]["objects"], ["limited sticker"])
        self.assertTrue(details["limit_reached"])
        _assert_independent_layout(self, result, selected, {}, 2)

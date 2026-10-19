"""Explicit ownership transfer preserves pixels/masks and moves cuts with artwork."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTemporaryDir
from PySide6.QtWidgets import QApplication, QMessageBox

from verona.editor_actions import build_edit
from verona.ui import VeronaWindow
from tests.test_editor_actions import image_item, rectangle


class CutLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = QTemporaryDir()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.path())
        cls.app = QApplication.instance() or QApplication([])

    def test_selected_contours_transfer_without_guessing_or_changing_pixels(self):
        source = rectangle("aggregate")
        other = rectangle("other", x=60, y=60)["contours"][0]
        source["contours"].append(other)
        image = image_item(cut=False)
        items = [source, image]
        original = copy.deepcopy(items)
        result, selected, details = build_edit(items, {}, [0, 1], "link_cut", {"contour_indices": [0]})
        self.assertEqual(result[0]["contours"], [other])
        self.assertEqual(result[1]["contours"], [original[0]["contours"][0]])
        self.assertEqual(selected, [1])
        self.assertTrue(result[1]["cut"])
        self.assertEqual(details["source_remaining_contours"], 1)
        for key in ("image_data", "image_clip", "image_transform"):
            self.assertEqual(result[1][key], original[1][key])
        self.assertEqual(items, original)

    def test_whole_transfer_deletes_only_empty_source_and_reindexes_image(self):
        items = [rectangle("external"), image_item(cut=False), rectangle("keep", x=80)]
        result, selected, details = build_edit(items, {}, [1, 0], "link_cut", {})
        self.assertEqual(len(result), 2)
        self.assertEqual(selected, [0])
        self.assertEqual(details["image_index_after"], 0)
        self.assertEqual(result[1], items[2])

    def test_explicit_pair_inside_groups_does_not_transfer_other_members(self):
        items = [image_item(group_path=["a"]), rectangle("external", group_path=["b"]),
                 rectangle("keep", x=80, group_path=["b"])]
        result, _, _ = build_edit(items, {}, [0, 1], "link_cut", {})
        self.assertEqual(result[1], items[2])

    def test_invalid_pairs_and_contour_indices_leave_sources_unchanged(self):
        items = [image_item(), rectangle(), image_item(), rectangle(registration_guide=True)]
        original = copy.deepcopy(items)
        for indices, parameters in (([0], {}), ([0, 2], {}), ([1, 3], {}),
                ([0, 1], {"contour_indices": []}), ([0, 1], {"contour_indices": [True]}),
                ([0, 1], {"contour_indices": [5]})):
            with self.subTest(indices=indices, parameters=parameters), self.assertRaises(ValueError):
                build_edit(items, {}, indices, "link_cut", parameters)
            self.assertEqual(items, original)

    def test_gui_link_transforms_copies_roundtrips_and_undoes_as_one_object(self):
        window = VeronaWindow()
        reopened = VeronaWindow()
        try:
            window.items = [rectangle("external"), image_item(cut=False)]
            window.refresh_all()
            window.select_many([0, 1])
            original = copy.deepcopy(window.items)
            window.link_selected_cut_paths()
            linked = copy.deepcopy(window.items)
            self.assertEqual(len(linked), 1)
            self.assertEqual(len(window.undo_stack), 1)
            window.transform_selected(mirror_x=True, rotation=90)
            self.assertEqual(window.items[0]["contours"], window.items[0]["image_clip"])
            window.duplicate_selected()
            self.assertEqual(len(window.items), 2)
            self.assertTrue(all(item["contours"] == item["image_clip"] for item in window.items))
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "linked.jdesign"
                window._write_document(str(path))
                self.assertTrue(reopened._load_document(str(path)))
                self.assertEqual(reopened.items, window.items)
            window.undo()
            window.undo()
            self.assertEqual(window.items, linked)
            window.undo()
            self.assertEqual(window.items, original)
            window.select(0)
            with patch.object(QMessageBox, "warning") as warning:
                window.link_selected_cut_paths()
            warning.assert_called_once()
            self.assertEqual(window.items, original)
        finally:
            for editor in (window, reopened):
                with patch.object(editor, "_confirm_unsaved_changes", return_value="discard"):
                    editor.close()

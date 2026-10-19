"""Real local IPC, stale-state protection and one-step undo for the editor."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog

from verona.editor_bridge import EditorBridge
from verona.editor_client import editor_directory, editor_request, list_editors
from PySide6.QtGui import QTransform

from verona.ui import VeronaWindow, _transform_item
from tests.test_editor_actions import image_item, rectangle


class EditorBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings.cleanup()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.directory = Path(self.folder.name) / "editors"
        self.window = VeronaWindow()
        self.window.items = [image_item(), rectangle("other", x=65, y=65)]
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 100,
                                              "paper_height": 100, "bounds": [10, 10, 90, 90]}}
        self.window.refresh_all()
        self.bridge = EditorBridge(self.window, directory=self.directory)
        self.executor = ThreadPoolExecutor(max_workers=1)

    def tearDown(self):
        self.bridge.close()
        self.executor.shutdown(wait=True)
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        self.folder.cleanup()

    def run_client(self, function, *args, **kwargs):
        future = self.executor.submit(function, *args, **kwargs)
        deadline = time.monotonic() + 15
        while not future.done() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.001)
        return future.result(timeout=1)

    def request(self, command, **arguments):
        return self.run_client(editor_request, self.bridge.editor_id, command,
                               arguments, directory=self.directory, timeout=10)

    def test_discovery_and_state_read_actual_unsaved_document(self):
        editors = self.run_client(list_editors, directory=self.directory)
        self.assertEqual([editor["editor_id"] for editor in editors], [self.bridge.editor_id])
        state = self.request("state", include_geometry=True, object_indices=[0])
        self.assertTrue(state["dirty"])
        self.assertEqual(state["objects"][0]["cut_geometry_mm"],
                         json.loads(json.dumps(self.window.items[0]["contours"])))
        self.assertNotIn("image_data", state["objects"][0])
        self.assertEqual(state["effective_cutting_frame_mm"], [10, 10, 90, 90])

    def test_configuration_preview_uses_new_frame_and_applies_as_one_undo(self):
        self.window.select(0)
        original = self.window._document_state()
        state = self.request("state")
        plan = self.request("preview", expected_revision=state["revision"], action="configure",
            object_indices=[], parameters={"registration": {"margins": [30, 30, 30, 30]},
                "cut_settings": {"custom": True, "force": 23, "speed": 7, "tool": 1}})
        self.assertTrue(plan["details"]["requires_reprint"])
        self.assertTrue(plan["objects"][0]["bounds_error"])
        self.assertEqual(self.window._document_state(), original)
        applied = self.request("apply", expected_revision=state["revision"], plan_id=plan["plan_id"])
        self.assertEqual(applied["effective_cutting_frame_mm"], [30, 30, 70, 70])
        self.assertEqual(applied["settings"]["force"], 23)
        self.assertEqual(applied["selection"], [0])
        self.assertEqual(applied["undo_steps"], 1)
        self.request("history", expected_revision=applied["revision"])
        self.assertEqual(self.window._document_state(), original)

    def test_creation_and_configuration_are_validated_before_any_live_change(self):
        state = self.request("state")
        original = self.window._document_state()
        for settings in ({"force": True}, {"speed": 14}, {"tool": 2}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.request("preview", expected_revision=state["revision"], action="configure",
                             object_indices=[], parameters={"cut_settings": settings})
        self.assertEqual(self.window._document_state(), original)
        plan = self.request("preview", expected_revision=state["revision"], action="add_shape",
            object_indices=[], parameters={"shape": "ellipse", "x_mm": 20, "y_mm": 20,
                                           "width_mm": 25, "height_mm": 15})
        self.assertEqual(plan["after_objects"], 3)
        applied = self.request("apply", expected_revision=state["revision"], plan_id=plan["plan_id"])
        self.assertEqual(applied["selection"], [2])
        self.assertEqual(applied["objects"][2]["bounds_mm"], [20, 20, 45, 35])

    def test_import_preview_appends_and_undo_preserves_unsaved_work(self):
        source = Path(self.folder.name) / "shape.svg"
        source.write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect width="40" height="20"/></svg>')
        original = self.window._document_state()
        state = self.request("state")
        plan = self.request("preview", expected_revision=state["revision"], action="import_artwork",
            object_indices=[], parameters={"path": str(source)})
        self.assertEqual(self.window._document_state(), original)
        applied = self.request("apply", expected_revision=state["revision"], plan_id=plan["plan_id"])
        self.assertEqual(len(applied["objects"]), 3)
        self.assertEqual(applied["metadata"], state["metadata"])
        self.request("history", expected_revision=applied["revision"])
        self.assertEqual(self.window._document_state(), original)

    def test_failed_lds_open_preserves_live_state_revision_selection_and_undo(self):
        import zipfile
        from tests.test_lds import document, record, vector
        self.window.select(0)
        self.window.push_undo(self.window._snapshot())
        before = self.request("state")
        original = copy.deepcopy((self.window._document_state(), self.window.undo_stack))
        line = vector([(0, 0, 16), (1, 0, 1)])
        malformed = record(1014, 3, 20) + record(0, 120, 21) + line + record(1014, 4, 20)
        for content in (malformed, line):
            path = Path(self.folder.name) / "invalid.lds"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("CDOC", document(content))
            with patch("verona.lds._MAX_POINTS", 1), self.assertRaises(ValueError):
                self.request("open_document", expected_revision=before["revision"], path=str(path))
            self.assertEqual(self.request("state"), before)
            self.assertEqual((self.window._document_state(), self.window.undo_stack), original)

    def test_json_limits_preserve_state_over_real_ipc(self):
        from verona import input_safety
        from tests.test_input_safety import design
        source = Path(self.folder.name)/"fixture.jdesign"
        source.write_text(json.dumps(design()))
        before = self.request("state")
        original = copy.deepcopy(self.window._document_state())
        for limits in ({"MAX_DESIGN_BYTES": 4}, {"MAX_POINTS": 1}, {"MAX_DEPTH": 1}):
            with patch.multiple(input_safety, **limits), self.assertRaises(ValueError):
                self.request("open_document", expected_revision=before["revision"], path=str(source))
            self.assertEqual(self.request("state"), before)
            self.assertEqual(self.window._document_state(), original)

    def test_printer_query_is_read_only_and_returns_only_installed_profiles(self):
        before = self.window._document_state()
        profiles = [{"name": "test queue", "paper_accepted": True, "margins_mm": [5]*4}]
        with patch("verona.printing.printer_profiles", return_value=profiles) as query:
            result = self.request("printers", paper_width_mm=210, paper_height_mm=297)
        query.assert_called_once_with(210, 297)
        self.assertEqual(result["printers"], profiles)
        self.assertFalse(result["printed"])
        self.assertEqual(self.window._document_state(), before)

    def test_link_cut_preview_apply_and_undo_preserve_mask_and_source_objects(self):
        original = self.window._document_state()
        state = self.request("state")
        plan = self.request("preview", expected_revision=state["revision"], action="link_cut",
                            object_indices=[0, 1], parameters={})
        self.assertEqual(self.window._document_state(), original)
        self.assertEqual(plan["after_objects"], 1)
        applied = self.request("apply", expected_revision=state["revision"], plan_id=plan["plan_id"])
        self.assertEqual(applied["selection"], [0])
        self.assertEqual(self.window.items[0]["image_clip"], original["items"][0]["image_clip"])
        self.assertEqual(self.window.items[0]["contours"], original["items"][1]["contours"])
        self.request("history", expected_revision=applied["revision"])
        self.assertEqual(self.window._document_state(), original)

    def test_retrace_preview_and_undo_preserve_original_image_and_affine(self):
        original = self.window._document_state()
        state = self.request("state")
        plan = self.request("preview", expected_revision=state["revision"], action="retrace_image",
                            object_indices=[0], parameters={"offset_mm": 1})
        self.assertEqual(self.window._document_state(), original)
        applied = self.request("apply", expected_revision=state["revision"], plan_id=plan["plan_id"])
        self.assertEqual(applied["objects"][0]["bounds_mm"], [19, 19, 41, 31])
        self.assertEqual(self.window.items[0]["image_data"], original["items"][0]["image_data"])
        self.assertEqual(self.window.items[0]["image_transform"], original["items"][0]["image_transform"])
        self.request("history", expected_revision=applied["revision"])
        self.assertEqual(self.window._document_state(), original)

    def test_mixed_pack_preview_apply_and_undo_preserve_source_document(self):
        original = self.window._document_state()
        state = self.request("state")
        plan = self.request("preview", expected_revision=state["revision"], action="pack_objects",
                            object_indices=[0, 1], parameters={"copies_per_unit": [2, 1], "allow_rotate": True})
        self.assertEqual(plan["details"]["copies"], 3)
        self.assertEqual(self.window._document_state(), original)
        applied = self.request("apply", expected_revision=state["revision"], plan_id=plan["plan_id"])
        self.assertEqual(len(applied["objects"]), 3)
        self.request("history", expected_revision=applied["revision"])
        self.assertEqual(self.window._document_state(), original)

    def test_path_order_preview_toolpath_image_and_undo_preserve_artwork_and_selection(self):
        self.window.items = [rectangle(width=60, height=60), rectangle(x=30, y=30)]
        self.window.refresh_all()
        self.window.select(0)
        original = self.window._document_state()
        state = self.request("state")
        plan = self.request("preview", expected_revision=state["revision"], action="path_order",
                            object_indices=[], parameters={"enabled": True})
        self.assertEqual(self.window._document_state(), original)
        self.assertEqual(plan["details"]["order"][0]["item_index"], 1)
        picture = self.request("image", plan_id=plan["plan_id"], view="toolpath")
        path = Path(picture["path"])
        self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))
        applied = self.request("apply", expected_revision=state["revision"], plan_id=plan["plan_id"])
        self.assertFalse(path.exists())
        self.assertTrue(applied["metadata"]["optimize_paths"])
        self.assertEqual(applied["selection"], [0])
        self.assertEqual(self.window.items, original["items"])
        self.request("history", expected_revision=applied["revision"])
        self.assertEqual(self.window._document_state(), original)

    def test_preview_apply_undo_redo_and_consumption_via_real_socket(self):
        original = copy.deepcopy(self.window.items)
        state = self.request("state")
        plan = self.request("preview", expected_revision=state["revision"], action="bleed",
                            object_indices=[0], parameters={"width_mm": 1, "color": "source"})
        self.assertEqual(self.window.items, original)
        self.assertEqual(len(self.window.undo_stack), 0)
        self.assertTrue(Path(plan["preview_path"]).is_file())
        applied = self.request("apply", expected_revision=state["revision"], plan_id=plan["plan_id"])
        self.assertEqual(self.window.items[0]["bleed"], {"width_mm": 1., "color": "#00ff00"})
        self.assertEqual(len(self.window.undo_stack), 1)
        with self.assertRaises(ValueError):
            self.request("apply", expected_revision=applied["revision"], plan_id=plan["plan_id"])
        undone = self.request("history", expected_revision=applied["revision"])
        self.assertEqual(self.window.items, original)
        self.request("history", expected_revision=undone["revision"], redo=True)
        self.assertIn("bleed", self.window.items[0])

    def test_manual_edits_selection_and_tabs_invalidate_preview(self):
        for change in ("geometry", "selection", "tab"):
            with self.subTest(change=change):
                state = self.request("state")
                plan = self.request("preview", expected_revision=state["revision"], action="duplicate",
                                    object_indices=[0], parameters={})
                if change == "geometry":
                    self.window.items[0]["contours"][0][0] = (21, 20)
                elif change == "selection":
                    self.window.select(1)
                else:
                    self.window.new_design()
                current = self.window._document_state()
                with self.assertRaisesRegex(ValueError, "changed"):
                    self.request("apply", expected_revision=state["revision"], plan_id=plan["plan_id"])
                self.assertEqual(self.window._document_state(), current)

    def test_sending_and_manual_gestures_block_modifications(self):
        state = self.request("state")
        self.window.sending = True
        with self.assertRaisesRegex(ValueError, "in progress"):
            self.request("preview", expected_revision=state["revision"], action="delete",
                         object_indices=[0], parameters={})
        self.window.sending = False
        self.window.canvas.drag_before = self.window._snapshot()
        with self.assertRaisesRegex(ValueError, "in progress"):
            self.request("select", expected_revision=state["revision"], object_indices=[0])
        self.window.canvas.drag_before = None

    def test_modal_dialog_blocks_agent_edits_until_user_finishes(self):
        state = self.request("state")
        original = self.window._document_state()
        dialog = QDialog(self.window)
        dialog.setModal(True)
        dialog.show()
        self.app.processEvents()
        try:
            with self.assertRaisesRegex(ValueError, "modal dialog"):
                self.request("preview", expected_revision=state["revision"], action="delete",
                             object_indices=[0], parameters={})
            self.assertEqual(self.request("state")["revision"], state["revision"])
            self.assertEqual(self.window._document_state(), original)
        finally:
            dialog.close()
            self.app.processEvents()
        plan = self.request("preview", expected_revision=state["revision"], action="duplicate",
                            object_indices=[0], parameters={})
        self.assertEqual(plan["after_objects"], 3)

    def test_bad_commands_and_large_requests_cannot_edit_or_contact_devices(self):
        before = self.window._document_state()
        with patch("verona.transport.send_tcp") as send:
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                self.request("send_job", payload="anything")
            with self.assertRaisesRegex(ValueError, "64 KiB"):
                self.request("state", unused="a" * 65536)
        send.assert_not_called()
        self.assertEqual(self.window._document_state(), before)
        with self.assertRaises(ValueError):
            editor_request("../other", "state", directory=self.directory)

    def test_preview_limit_and_close_remove_only_this_editors_artifacts(self):
        state = self.request("state")
        first = None
        for _ in range(5):
            plan = self.request("preview", expected_revision=state["revision"], action="duplicate",
                                object_indices=[0], parameters={})
            if first is None:
                first = plan
        self.assertEqual(len(self.bridge.plans), 4)
        self.assertFalse(Path(first["preview_path"]).exists())
        marker = self.directory / "unrelated.txt"
        marker.write_text("keep")
        self.bridge.close()
        self.assertTrue(marker.exists())
        self.assertFalse(self.bridge.socket_path.exists())
        self.assertEqual(list(self.directory.glob(f"{self.bridge.editor_id}.*")), [])

    def test_socket_directory_rejects_public_access(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.folder.name}):
            directory = editor_directory(create=True)
            directory.chmod(0o755)
            with self.assertRaises(PermissionError):
                editor_directory()

    def test_open_preserves_unsaved_tab_and_file_outputs_refuse_overwrite(self):
        original = self.window._document_state()
        source = Path(self.folder.name) / "source.jdesign"
        source.write_text(json.dumps({"format": "verona", "version": 1,
                                     "units": "mm", **original}))
        state = self.request("state")
        invalid = Path(self.folder.name) / "invalid.json"
        invalid.write_text("{}")
        with self.assertRaises(ValueError):
            self.request("open_document", expected_revision=state["revision"], path=str(invalid))
        self.assertEqual(len(self.window._documents), 1)
        self.assertEqual(self.window._document_state(), original)
        opened = self.request("open_document", expected_revision=state["revision"], path=str(source))
        self.assertEqual(len(opened["tabs"]), 2)
        self.assertEqual(self.window._documents[0]["items"], original["items"])
        self.assertEqual(self.window._documents[0]["metadata"], original["metadata"])
        occupied = Path(self.folder.name) / "keep.jdesign"
        occupied.write_text("keep")
        with self.assertRaises(ValueError):
            self.request("save_document", expected_revision=opened["revision"], path=str(occupied))
        self.assertEqual(occupied.read_text(), "keep")
        with self.assertRaisesRegex(ValueError, "LDS"):
            self.request("save_document", expected_revision=opened["revision"], path=str(occupied.with_suffix(".lds")))
        saved = self.request("save_document", expected_revision=opened["revision"],
                             path=str(Path(self.folder.name) / "saved.jdesign"))
        self.assertFalse(saved["dirty"])
        pdf = Path(self.folder.name) / "printed.pdf"
        exported = self.request("export_pdf", expected_revision=saved["revision"], path=str(pdf))
        self.assertFalse(exported["printed"])
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        before = pdf.read_bytes()
        with self.assertRaises(ValueError):
            self.request("export_pdf", expected_revision=saved["revision"], path=str(pdf))
        self.assertEqual(pdf.read_bytes(), before)
        self.assertEqual(self.bridge.revision(), saved["revision"])

    def test_revision_digests_images_once_and_still_tracks_every_change(self):
        import hashlib
        import numpy as np
        from PySide6.QtGui import QImage
        from verona.raster_import import _png_base64

        def noise(seed):
            pixels = np.random.default_rng(seed).integers(0, 255, (200, 200, 4), dtype=np.uint8)
            return _png_base64(QImage(pixels.data, 200, 200, 800, QImage.Format.Format_RGBA8888).copy())
        self.window.items[0]["image_data"] = noise(1)
        self.window.metadata["source_preview"] = noise(2)
        first = self.bridge.revision()
        with patch("verona.editor_bridge.hashlib.sha256", wraps=hashlib.sha256) as sha:
            self.assertEqual(self.bridge.revision(), first)
            self.assertEqual(self.bridge.state()["revision"], first)
        # Later revisions hash only the small document summary, not the images again.
        hashed = sum(len(call.args[0]) for call in sha.call_args_list)
        self.assertLess(hashed, len(self.window.items[0]["image_data"]) // 10)
        # Equal content in a different string object is the same document.
        self.window.items[0]["image_data"] = "".join(list(self.window.items[0]["image_data"]))
        self.assertEqual(self.bridge.revision(), first)
        seen = {first}
        for change in (lambda: self.window.items[0].__setitem__("image_data", noise(3)),
                       lambda: self.window.metadata.__setitem__("source_preview", noise(4)),
                       lambda: self.window.items[1]["contours"][0].__setitem__(0, (65.5, 65.)),
                       lambda: self.window.items[0].__setitem__("cut", False)):
            change()
            revision = self.bridge.revision()
            self.assertNotIn(revision, seen)
            seen.add(revision)

    def test_preview_revalidates_and_rerenders_only_edited_edge_bleed_images(self):
        from verona import edge_bleed
        from verona.editor_actions import build_edit, render_preview
        from tests.test_edge_bleed import two_colors
        items = []
        for offset in (0, 30, 60):
            item = two_colors()
            _transform_item(item, QTransform.fromTranslate(offset, offset))
            items.append(build_edit([item], {}, [0], "bleed", {"width_mm": 1, "color": "#ffffff",
                                                                "mode": "edge"})[0][0])
        self.window.items = items
        self.window.metadata = {}
        self.window.refresh_all()
        with patch.dict(edge_bleed._LAYERS, clear=True), \
                patch("verona.edge_bleed._edge_bleed_image", wraps=edge_bleed._edge_bleed_image) as compute:
            render_preview(self.window.items, self.window.metadata, Path(self.folder.name) / "warm.png")
            self.assertEqual(compute.call_count, 3)
            compute.reset_mock()
            plan = self.bridge.preview(self.bridge.revision(), "transform", [1], {"dx_mm": 1})
            self.assertEqual(compute.call_count, 1)
        self.assertEqual(plan["after_objects"], 3)
        # An edited object is still fully validated; unchanged ones are trusted.
        broken = copy.deepcopy(items[1])
        broken["contours"] = rectangle(x=73, y=53, width=5, height=5)["contours"]
        document = {"format": "verona", "version": 1, "units": "mm"}
        with self.assertRaisesRegex(ValueError, "no visible source"):
            VeronaWindow._validate_document({**document, "items": [items[0], broken]}, trusted_items=items)
        with patch("verona.edge_bleed.edge_bleed_image") as checked:
            validated, _ = VeronaWindow._validate_document({**document, "items": copy.deepcopy(items)},
                                                           trusted_items=items)
        checked.assert_not_called()
        self.assertEqual(validated, items)
        self.assertFalse(any(new is old or new["contours"] is old["contours"]
                             for new, old in zip(validated, items)))


if __name__ == "__main__":
    unittest.main()

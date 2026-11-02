"""MCP print and live-editor cut preparation; all hardware calls are mocked."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QSizeF
from PySide6.QtGui import QImage, QPageSize
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QApplication

from verona.editor_bridge import EditorBridge
from verona.i18n import get_language, set_language
from verona.mcp_server import VeronaController, create_server
from verona import printing
from verona.print_dialog import _CUPS_OPTIONS, apply_print_job_options
from verona.printer_options import display_choice_zh
from verona.ui import VeronaWindow
from tests.test_editor_actions import rectangle


class McpPrintWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        previous_language = get_language()
        set_language("zh_TW")
        self.addCleanup(set_language, previous_language)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        runtime = Path(self.temp.name) / "runtime"
        runtime.mkdir(mode=0o700)
        os.environ["XDG_RUNTIME_DIR"] = str(runtime)
        self.directory = runtime / "verona" / "editors"
        self.directory.mkdir(mode=0o700, parents=True)
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope,
                          str(Path(self.temp.name) / "settings"))
        self.window = VeronaWindow()
        self.window.items = [rectangle("print-and-cut", print_style={
            "fill": "#2040c0", "stroke": None, "width_mm": .2})]
        self.window.metadata = {"print_cut": {"enabled": True,
            "paper_width": 100., "paper_height": 100., "bounds": [10., 10., 90., 90.],
            "offset": [0., 0.]}}
        self.window.refresh_all()
        self.bridge = EditorBridge(self.window, directory=self.directory, listen=False)
        self.addCleanup(self._close_window)
        self.controller = VeronaController(lock_path=Path(self.temp.name) / "device.lock")
        self.server = create_server(self.controller)

    def _close_window(self):
        self.bridge.close()
        from unittest.mock import patch as mock_patch
        with mock_patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        if self.old_runtime_dir is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = self.old_runtime_dir

    def _printer(self, _name):
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(Path(self.temp.name) / "must-not-submit.pdf"))
        printer.setPageSize(QPageSize(QSizeF(100, 100), QPageSize.Unit.Millimeter))
        return printer

    def _rpc(self, _editor_id, command, arguments=None, **_kwargs):
        return self.bridge.dispatch(command, **(arguments or {}))

    def _prepare_print(self, extra_options=None):
        revision = self.bridge.revision()
        options = {"media": "PLAIN", "quality": "HIGH", "color_mode": "grayscale",
                   "copies": 2, "brightness": "50", "icc_profile_path": None,
                   "rendering_intent": "perceptual"}
        options.update(extra_options or {})
        with patch("verona.editor_bridge._make_print_printer", side_effect=self._printer), \
                patch("verona.printer_options.read_printer_options", return_value={
                    "PageSize": {"choices": ["Custom"]},
                    "MediaType": {"choices": ["PLAIN", "PHOTO"], "default": "PLAIN"},
                    "Quality": {"choices": ["HIGH", "NORMAL"], "default": "NORMAL"},
                    "Ink": {"choices": ["Color", "Mono"], "default": "Color"},
                    "Brightness": {"choices": ["0", "25", "50"], "default": "25"}}), \
                patch("verona.mcp_server.editor_request", side_effect=self._rpc):
            prepared = self.controller.prepare_print(self.bridge.editor_id, revision,
                "Test printer", options)
        return prepared

    def test_prepare_preview_and_confirmed_print_use_identical_pixels_once(self):
        with patch("verona.transport.send_tcp") as tcp, patch("verona.transport.send_usb") as usb:
            prepared = self._prepare_print()
        tcp.assert_not_called()
        usb.assert_not_called()
        arguments = {"editor_id": self.bridge.editor_id, "expected_revision": prepared["revision"],
                     "printer_name": "Test printer", "options": {}}
        with patch.object(self.controller, "prepare_print", return_value=prepared):
            tool = self.server._tool_manager.get_tool("verona_prepare_print")
            content = tool.fn(**arguments)
        self.assertEqual(len(content), 2)
        summary = json.loads(content[0].text)
        self.assertEqual(str(content[1].path), prepared["preview_png"])
        converted = tool.fn_metadata.convert_result(content)
        self.assertEqual(len(converted.content), 2)
        self.assertEqual(converted.content[1].type, "image")
        self.assertEqual(summary["summary"]["media"], "普通紙")
        self.assertEqual(summary["summary"]["quality"], "高品質")
        self.assertEqual(summary["summary"]["color"], "灰階")
        self.assertEqual(summary["summary"]["copies"], 2)
        self.assertEqual(summary["revision"], self.bridge.revision())
        self.assertEqual(summary["summary"]["registration_bounds_mm"], [10., 10., 90., 90.])

        image = QImage(summary["preview_png"])
        self.assertFalse(image.isNull())
        self.assertEqual(hashlib.sha256(Path(summary["preview_png"]).read_bytes()).hexdigest(),
                         summary["preview_sha256"])
        with patch("verona.mcp_server.editor_request", side_effect=self._rpc), \
                patch("verona.printing.print_document") as rejected_submit:
            with self.assertRaisesRegex(ValueError, "explicit consent"):
                self.controller.print_prepared(summary["print_id"], summary["sha256"], False)
            rejected_submit.assert_not_called()
        with patch("verona.mcp_server.editor_request", side_effect=self._rpc), \
                patch("verona.printing.print_document") as rejected_submit:
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                self.controller.print_prepared(summary["print_id"], "0" * 64, True)
            rejected_submit.assert_not_called()

        with patch("verona.mcp_server.editor_request", side_effect=self._rpc), \
                patch("verona.printing.print_document") as submit, \
                patch("verona.transport.send_tcp") as tcp, patch("verona.transport.send_usb") as usb:
            actual = self.controller.print_prepared(summary["print_id"], summary["sha256"], True)
            self.assertEqual(actual["status"], "submitted")
            submitted_image = submit.call_args.kwargs["rendered_image"]
            self.assertEqual(submitted_image, image)
            submit.assert_called_once()
            with self.assertRaisesRegex(ValueError, "already attempted"):
                self.controller.print_prepared(summary["print_id"], summary["sha256"], True)
            submit.assert_called_once()
            tcp.assert_not_called()
            usb.assert_not_called()
        self.assertFalse((Path(self.temp.name) / "must-not-submit.pdf").exists())

    def test_changed_revision_rejects_print_and_consumes_that_attempt(self):
        prepared = self._prepare_print()
        self.window.items[0]["name"] = "new revision"
        with patch("verona.mcp_server.editor_request", side_effect=self._rpc), \
                patch("verona.printing.print_document") as submit:
            with self.assertRaisesRegex(ValueError, "editor changed"):
                self.controller.print_prepared(prepared["print_id"], prepared["sha256"], True)
        self.assertNotIn(prepared["print_id"], self.controller.print_jobs)
        self.assertNotIn(prepared["print_id"], self.bridge.print_jobs)
        submit.assert_not_called()
        self.assertFalse((Path(self.temp.name) / "must-not-submit.pdf").exists())

    def test_sha_mismatch_rejects_without_consuming_or_submitting(self):
        prepared = self._prepare_print()
        with patch("verona.mcp_server.editor_request", side_effect=self._rpc), \
                patch("verona.printing.print_document") as submit:
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                self.controller.print_prepared(prepared["print_id"], "0" * 64, True)
        self.assertIn(prepared["print_id"], self.controller.print_jobs)
        self.assertIn(prepared["print_id"], self.bridge.print_jobs)
        submit.assert_not_called()

    def test_icc_path_and_rendering_intent_are_applied_to_the_prepared_page(self):
        from PIL import ImageCms
        profile_path = Path(self.temp.name) / "srgb.icc"
        profile_path.write_bytes(ImageCms.ImageCmsProfile(
            ImageCms.createProfile("sRGB")).tobytes())
        with patch("verona.printing._draw_profiled_artwork",
                   wraps=printing._draw_profiled_artwork) as render:
            prepared = self._prepare_print({"icc_profile_path": str(profile_path),
                                            "rendering_intent": "relative"})
        self.assertEqual(prepared["summary"]["icc_profile_path"], str(profile_path))
        self.assertEqual(prepared["summary"]["rendering_intent"], "relative")
        self.assertEqual(render.call_args.args[4], str(profile_path))
        self.assertEqual(render.call_args.args[5], "relative")

    def test_cut_from_editor_matches_saved_prepare_payload_and_guards_revision(self):
        saved_path = Path(self.temp.name) / "same-design.jdesign"
        self.window._write_document(str(saved_path))
        printed = self._prepare_print()
        revision = printed["revision"]
        saved = self.controller.prepare_job(str(saved_path))
        with patch("verona.mcp_server.editor_request", side_effect=self._rpc), \
                patch("verona.transport.send_tcp") as tcp, patch("verona.transport.send_usb") as usb:
            live = self.controller.prepare_job_from_editor(self.bridge.editor_id, revision)
            self.assertEqual(self.controller.jobs[live["job_id"]], self.controller.jobs[saved["job_id"]])
            self.assertEqual(live["content_sha256"], saved["content_sha256"])
            self.assertEqual(live["revision"], printed["revision"])
            self.assertEqual(live["content_sha256"], printed["content_sha256"])
            self.assertEqual(live["revision"], revision)
            self.assertEqual(live["registration_bounds_mm"], [10., 10., 90., 90.])
            self.assertEqual(live["registration_bounds_mm"],
                             printed["summary"]["registration_bounds_mm"])
            with self.assertRaisesRegex(ValueError, "editor changed"):
                self.controller.prepare_job_from_editor(self.bridge.editor_id, "stale")
            tcp.assert_not_called()
            usb.assert_not_called()

    def test_printer_job_options_are_per_job_and_reject_unsupported_choices(self):
        printer = MagicMock()
        printer.printEngine().property.return_value = ["OutputOrder", "Reverse"]
        available = {"PageSize": {"choices": ["Custom"]},
                     "MediaType": {"choices": ["PLAIN", "PHOTO"]},
                     "Quality": {"choices": ["HIGH"]}, "Ink": {"choices": ["Color", "Mono"]},
                     "Brightness": {"choices": ["0", "50"]}}
        with patch("verona.printer_options.subprocess.run") as lpoptions:
            applied = apply_print_job_options(printer, {"media": "PHOTO", "quality": "HIGH",
                "color_mode": "grayscale", "copies": 3, "brightness": "50"}, available, 100, 100)
        lpoptions.assert_not_called()
        self.assertEqual(applied["media"], "PHOTO")
        self.assertEqual(applied["quality"], "HIGH")
        self.assertEqual(printer.setCopyCount.call_args.args, (3,))
        printer.printEngine().setProperty.assert_called_once()
        self.assertEqual(printer.printEngine().setProperty.call_args.args[0], _CUPS_OPTIONS)
        cups = printer.printEngine().setProperty.call_args.args[1]
        options = dict(zip(cups[::2], cups[1::2]))
        self.assertEqual(options["PageSize"], "Custom.100x100mm")
        self.assertEqual(options["MediaType"], "PHOTO")
        self.assertEqual(options["Quality"], "HIGH")
        self.assertEqual(options["Ink"], "Mono")
        self.assertEqual(options["Brightness"], "50")
        self.assertEqual(options["MediaType"], "PHOTO")
        self.assertEqual(options["OutputOrder"], "Reverse")
        printer.reset_mock()
        with self.assertRaisesRegex(ValueError, "does not support|不支援"):
            apply_print_job_options(printer, {"media": "GLOSSY"}, available, 100, 100)
        printer.printEngine().setProperty.assert_not_called()

    def test_media_summary_uses_chinese_catalogue_names_when_ui_is_english(self):
        set_language("en")
        self.assertEqual(display_choice_zh("PMMATT_HIGH"), "雪面銅版紙 · 高品質")


if __name__ == "__main__":
    unittest.main()

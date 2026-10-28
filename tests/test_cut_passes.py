"""Multiple passes repeat tool paths inside one registered cutting job."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QSettings
from PySide6.QtWidgets import QApplication

from verona import cli, commands
from verona.editor_document import build_document_edit
from verona.i18n import set_language
from verona.mcp_server import VeronaController
from verona.ui import VeronaWindow


def document(passes=1):
    return {"format": "verona", "version": 1, "units": "mm",
            "settings": {"passes": passes},
            "items": [{"name": "line", "contours": [[[5, 5], [15, 5]]]},
                      {"name": "print only", "cut": False, "contours": [[[20, 5], [30, 5]]]}]}


class CutPassTests(unittest.TestCase):
    def test_repeats_each_contour_with_a_raised_return_and_one_job_footer(self):
        payload = commands.encode_plt([[(5, 5), (15, 5)], [(20, 5), (30, 5)]], passes=3)
        self.assertEqual(payload, b"IN;FSIZE200,1200;U SP1;"
            + b"U200,200 D200,600 " * 3 + b"U200,800 D200,1200 " * 3 + b"U0,0 U !PG;")

    def test_registered_passes_scan_once_and_preserve_the_closed_path(self):
        payload = commands.encode_plt([[(11, 23), (76, 91), (11, 23)]], passes=10,
            registration_bounds=(10, 20, 80, 95), registration_offset=(-.3, .2))
        self.assertEqual(payload.count(b"TB26,"), 1)
        self.assertEqual(payload.count(b"CT1;"), 1)
        self.assertEqual(payload.count(b"U2872,28 D152,2628 D2872,28 "), 10)
        self.assertTrue(payload.endswith(b"U2872,0 U !PG;!PG;"))

    def test_pass_count_rejects_nonintegers_and_out_of_range_values(self):
        for value in (0, -1, 11, True, False, None, 2., "2", float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                commands.encode_plt([[(1, 1), (2, 2)]], passes=value)

    def test_saved_passes_apply_without_overriding_machine_force_and_speed(self):
        data = document(3)
        original = copy.deepcopy(data)
        items, settings = VeronaWindow._validate_document(data)
        payload, job = commands.encode_document_job(items, {}, settings)
        self.assertEqual(job["passes"], 3)
        self.assertEqual(len(job["contours"]), 1)
        self.assertEqual(payload.count(b"D200,600 "), 3)
        self.assertNotIn(b"!FS", payload)
        self.assertNotIn(b"VS", payload)
        self.assertNotIn(b"D200,1200", payload)
        self.assertEqual(commands.encode_document(items, {}, settings, passes=1).count(b"D200,600 "), 1)
        self.assertEqual(data, original)

    def test_legacy_documents_default_to_one_and_invalid_saved_passes_reject(self):
        data = document()
        del data["settings"]["passes"]
        self.assertEqual(VeronaWindow._validate_document(data)[1]["passes"], 1)
        for value in (0, 11, True, 1., "2", None):
            data["settings"]["passes"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                VeronaWindow._validate_document(data)

    def test_cli_and_mcp_use_saved_passes_or_an_explicit_override_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"design.jdesign"
            source.write_text(json.dumps(document(3)))
            controller = VeronaController(lock_path=Path(folder)/"device.lock")
            with patch("socket.create_connection") as connect:
                self.assertEqual(cli.prepare_job(source).count(b"D200,600 "), 3)
                for count in (1, 5):
                    output, errors = io.StringIO(), io.StringIO()
                    destination = Path(folder)/"job.plt"
                    with redirect_stdout(output), redirect_stderr(errors):
                        self.assertEqual(cli.main(["cut", str(source), "--passes", str(count),
                            "--dry-run", "--output", str(destination)]), 0)
                    self.assertEqual(destination.read_bytes().count(b"D200,600 "), count)
                    job = controller.prepare_job(source, passes=count)
                    self.assertEqual(job["passes"], count)
                    self.assertEqual(controller.jobs[job["job_id"]], destination.read_bytes())
                self.assertEqual(controller.prepare_job(source)["passes"], 3)
                connect.assert_not_called()

    def test_svg_passes_and_invalid_cli_values_never_connect(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/"line.svg"
            source.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="40mm" '
                              'viewBox="0 0 40 40"><path d="M5 5L15 5"/></svg>')
            with patch("socket.create_connection") as connect:
                self.assertEqual(cli.prepare_job(source, passes=4).count(b"D200,600 "), 4)
                for count in (0, 11):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(cli.main(["cut", str(source), "--passes", str(count)]), 2)
                connect.assert_not_called()

    def test_agent_configuration_preserves_validated_passes(self):
        data = document()
        items, settings = VeronaWindow._validate_document(data)
        result, metadata, updated, *_ = build_document_edit(items, {}, settings, [0],
            "configure", {"cut_settings": {"passes": 4}})
        self.assertEqual(VeronaWindow._validate_document({**data, "items": result,
            "settings": updated, "metadata": metadata})[1]["passes"], 4)


class CutPassUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.folder.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"VERONA_HOST": "", "XDG_STATE_HOME": self.folder.name}))
        set_language("zh_TW")
        self.window = VeronaWindow()

    def tearDown(self):
        self.window._confirm_unsaved_changes = lambda *_args: "discard"
        self.window.close()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        set_language("zh_TW")

    def test_passes_are_independent_of_force_speed_and_survive_both_design_formats(self):
        self.assertEqual(self.window.passes_spin.value(), 1)
        self.assertEqual((self.window.passes_spin.minimum(), self.window.passes_spin.maximum()), (1, 10))
        self.assertFalse(self.window.custom_settings.isChecked())
        self.assertTrue(self.window.passes_spin.isEnabled())
        self.assertFalse(self.window.is_dirty())
        self.window.passes_spin.setValue(3)
        self.assertTrue(self.window.is_dirty())
        self.window.items = document()["items"]
        expected = self.window._encoded_plt()
        with tempfile.TemporaryDirectory() as folder:
            for suffix in (".jdesign", ".lds"):
                path = Path(folder)/("design" + suffix)
                self.window._write_document(str(path))
                self.window.passes_spin.setValue(1)
                self.assertTrue(self.window._load_document(str(path)))
                self.assertEqual(self.window.passes_spin.value(), 3)
                self.assertEqual(self.window._encoded_plt(), expected)

    def test_confirmation_reports_passes_and_sender_transmits_one_job(self):
        self.window.items = document()["items"]
        self.window.passes_spin.setValue(3)
        self.window.wifi_host.setText("192.0.2.1")
        details = self.window._send_confirmation(self.window._encoded_plt())
        self.assertIn("每條輪廓 3 次", details)
        set_language("en")
        self.assertIn("3 pass(es) per contour", self.window._send_confirmation(self.window._encoded_plt()))
        with patch.object(self.window, "_confirm_send", return_value=True), \
                patch.object(self.window, "_dispatch", side_effect=lambda task, *_: task()), \
                patch("verona.transport.send_tcp", side_effect=lambda payload, *_args, **_kw: len(payload)) as send:
            self.window.send_to_cutter()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0].count(b"D200,600 "), 3)

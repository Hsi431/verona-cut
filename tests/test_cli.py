"""Public CLI operations stay offline unless a device operation is explicit."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from verona import cli, commands


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.jdesign"
        self.document = {"format": "verona", "version": 1, "units": "mm",
                         "items": [{"name": "line", "contours": [[[5, 5], [25, 15]]]}]}
        self.write()
        self.enterContext(patch.dict(os.environ, {"VERONA_HOST": "", "XDG_STATE_HOME": self.directory.name}))

    def write(self):
        self.path.write_text(json.dumps(self.document))

    def run_cli(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli.main(list(map(str, arguments)))
        return code, json.loads(output.getvalue()) if output.getvalue() else None, errors.getvalue()

    def test_default_and_explicit_dry_runs_export_exact_example_without_connections(self):
        expected = (ROOT / "examples/pen-calibration.plt").read_bytes()
        output = Path(self.directory.name) / "preview.plt"
        for flags in ((), ("--dry-run",)):
            with self.subTest(flags=flags), patch("socket.create_connection") as connect:
                code, result, error = self.run_cli("cut", ROOT / "examples/pen-calibration.jdesign",
                                                   *flags, "--output", output)
                self.assertEqual((code, error), (0, ""))
                self.assertTrue(result["dry_run"])
                self.assertEqual(result["bytes"], len(expected))
                self.assertEqual(output.read_bytes(), expected)
                connect.assert_not_called()

    def test_svg_dry_run_needs_no_host(self):
        with patch("socket.create_connection") as connect:
            code, result, error = self.run_cli("cut", ROOT / "examples/sample-design.svg", "--tool", "pen")
        self.assertEqual((code, error), (0, ""))
        self.assertGreater(result["bytes"], 50)
        connect.assert_not_called()

    def test_legacy_document_identifier_remains_readable(self):
        current = cli.prepare_job(self.path)
        self.document["format"] = "juliet-design-linux"
        self.write()
        self.assertEqual(cli.prepare_job(self.path), current)

    def test_invalid_geometry_or_material_settings_never_reach_transport(self):
        for point in ([301, 0], [-1, 0], [True, 0], ["5", 0], [float("nan"), 0]):
            self.document["items"][0]["contours"] = [[[1, 1], point]]
            self.write()
            with self.subTest(point=point), patch("verona.transport.send_tcp") as send:
                code, _, error = self.run_cli("cut", self.path, "--host", "192.0.2.1", "--send")
                self.assertEqual(code, 2)
                self.assertTrue(error)
                send.assert_not_called()
        for flag, value in (("--force", 0), ("--force", 161), ("--speed", 0), ("--speed", 14)):
            with self.subTest(flag=flag, value=value), patch("verona.transport.send_tcp") as send:
                code, _, _ = self.run_cli("cut", ROOT / "examples/pen-calibration.jdesign",
                                          flag, value, "--host", "192.0.2.1", "--send")
                self.assertEqual(code, 2)
                send.assert_not_called()

    def test_builder_rejects_boolean_and_non_numeric_coordinates(self):
        for point in ((True, 0), ("5", 0), (None, 1), (1,), (1, 2, 3)):
            with self.subTest(point=point), self.assertRaises(ValueError):
                commands.encode_plt([[(0, 0), point]])

    def test_status_queries_once_using_environment_host_without_sending_job(self):
        with patch.dict(os.environ, {"VERONA_HOST": "192.0.2.1"}), \
                patch("verona.transport.query_tcp_version", return_value="V22.0613") as query, \
                patch("verona.transport.send_tcp") as send:
            code, result, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(result["version"], "V22.0613")
        self.assertEqual(result["physical_readiness"], "unknown")
        query.assert_called_once_with("192.0.2.1", 8080)
        send.assert_not_called()

    def test_optional_mcp_can_start_offline_with_an_empty_host_environment(self):
        from verona import mcp_server
        with patch.object(mcp_server, "create_server") as create, \
                patch("socket.create_connection") as connect:
            mcp_server.main([])
        self.assertIsNone(create.call_args.args[0].host)
        create.return_value.run.assert_called_once_with(transport="stdio")
        connect.assert_not_called()

    def test_explicit_send_overrides_environment_and_uses_selected_parameters(self):
        with patch.dict(os.environ, {"VERONA_HOST": "192.0.2.1"}), \
                patch("verona.transport.send_tcp", side_effect=lambda data, *_: len(data)) as send:
            code, result, error = self.run_cli("cut", self.path, "--send", "--host", "192.0.2.2",
                                               "--port", 8081, "--tool", "pen", "--force", 2, "--speed", 3)
        self.assertEqual((code, error), (0, ""))
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["physical_completion"], "unknown")
        send.assert_called_once()
        payload, host, port = send.call_args.args
        self.assertEqual((host, port), ("192.0.2.2", 8081))
        self.assertIn(b"SP2;!FS2;VS3;", payload)

    def test_failure_is_not_retried(self):
        with patch("verona.transport.send_tcp", side_effect=OSError("partial transfer")) as send:
            code, _, error = self.run_cli("cut", self.path, "--host", "192.0.2.1", "--send")
        self.assertEqual(code, 2)
        self.assertIn("partial transfer", error)
        send.assert_called_once()

    def test_missing_or_invalid_endpoint_does_not_connect(self):
        for flags in ((), ("--host", "255.255.255.255"), ("--host", "192.0.2.1", "--port", "0")):
            with self.subTest(flags=flags), patch("socket.create_connection") as connect:
                code, _, _ = self.run_cli("cut", self.path, "--send", *flags)
                self.assertEqual(code, 2)
                connect.assert_not_called()

    def test_transport_rejects_non_integer_or_out_of_range_ports_before_connecting(self):
        for port in (True, False, 8080.5, "8080", 0, 65536):
            with self.subTest(port=port), patch("socket.create_connection") as connect:
                with self.assertRaises(ValueError):
                    cli.transport.send_tcp(b"IN;", "192.0.2.1", port)
                connect.assert_not_called()

    def test_gui_endpoint_precedence_is_explicit_then_environment_then_saved(self):
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        from verona.ui import VeronaWindow
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, self.directory.name)
        app = QApplication.instance() or QApplication([])
        settings = QSettings("Verona", "Workspace")
        settings.setValue("wifi/host", "192.0.2.1")
        settings.setValue("wifi/port", 8081)
        for environment, explicit, expected in (("", None, "192.0.2.1"),
                                                 ("192.0.2.2", None, "192.0.2.2"),
                                                 ("192.0.2.2", "192.0.2.3", "192.0.2.3")):
            with self.subTest(environment=environment, explicit=explicit), \
                    patch.dict(os.environ, {"VERONA_HOST": environment}):
                window = VeronaWindow(host=explicit)
                self.assertEqual(window.windowTitle(), "Verona")
                self.assertEqual(window.wifi_host.text(), expected)
                self.assertEqual(window.wifi_port.value(), 8081)
                window.close()
                window.deleteLater()
        app.processEvents()

    def test_raw_plt_is_rejected_and_source_cannot_be_overwritten(self):
        original = self.path.read_bytes()
        for arguments in (("cut", ROOT / "examples/pen-calibration.plt"),
                          ("cut", self.path, "--output", self.path)):
            code, _, _ = self.run_cli(*arguments)
            self.assertEqual(code, 2)
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

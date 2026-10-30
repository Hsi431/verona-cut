from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from verona import firmware_cli


class FirmwareCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "synthetic.bin"
        self.path.write_bytes(b"synthetic test input; not a firmware image")
        self.sha = "a" * 64
        self.image = SimpleNamespace(sha256=self.sha, summary=lambda: {
            "sha256": self.sha, "version_candidates": ["V23.0919"],
            "compatibility_verified": False})
        self.arguments = ["update", str(self.path), "--host", "192.0.2.1",
                          "--sha256", self.sha, "--expected-mode", "SS12",
                          "--expected-hver", "V7.1202_4300", "--expected-sver", "V22.0613",
                          "--target-version", "V23.0919"]
        loader = patch.object(firmware_cli, "load_firmware", return_value=self.image)
        self.loader = loader.start()
        self.addCleanup(loader.stop)

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = firmware_cli.main(arguments)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_inspect_is_offline_and_preserves_unknown_compatibility(self):
        with patch.object(firmware_cli, "query_identity") as query, \
                patch.object(firmware_cli, "update_firmware") as update:
            code, result, _ = self.invoke(["inspect", str(self.path)])
        self.assertEqual(code, 0)
        self.assertFalse(result["compatibility_verified"])
        query.assert_not_called()
        update.assert_not_called()

    def test_missing_assertions_and_wrong_hash_cannot_start_update(self):
        with patch.object(firmware_cli, "update_firmware") as update:
            for flags in ([], ["--machine-ready"], ["--confirm-compatible"]):
                code, _, _ = self.invoke(self.arguments + flags)
                self.assertEqual(code, 1)
            arguments = self.arguments.copy()
            arguments[arguments.index("--sha256") + 1] = "b" * 64
            code, _, _ = self.invoke(arguments + ["--machine-ready", "--confirm-compatible"])
            self.assertEqual(code, 1)
        update.assert_not_called()

    def test_conflicting_target_version_cannot_start_update(self):
        arguments = self.arguments.copy()
        arguments[arguments.index("--target-version") + 1] = "V26.0605"
        with patch.object(firmware_cli, "update_firmware") as update:
            code, result, _ = self.invoke(arguments + ["--machine-ready", "--confirm-compatible"])
        self.assertEqual(code, 1)
        self.assertIn("conflicts", result["error"])
        update.assert_not_called()

    def test_pgok_requires_separate_version_verification(self):
        def transfer(*_args, on_progress, **_kwargs):
            on_progress(1024, 1500)
            on_progress(1500, 1500)
            return {"status": "pgok_received", "bytes_body_sent": 1500,
                    "physical_completion": "unknown", "version_verified": False}

        with patch.object(firmware_cli, "update_firmware", side_effect=transfer) as update, \
                patch.object(firmware_cli, "verify_firmware_version", return_value={
                    "software_version": "V23.0919", "version_verified": True}) as verify:
            code, result, stderr = self.invoke(self.arguments + ["--machine-ready", "--confirm-compatible"])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "version_verified")
        self.assertTrue(result["pgok_received"])
        self.assertEqual(result["physical_completion"], "unknown")
        self.assertEqual(result["bytes_body_sent"], 1500)
        self.assertIn("1500/1500", stderr)
        self.assertEqual(update.call_count, 1)
        self.assertEqual(update.call_args.kwargs["expected_hver"], "V7.1202_4300")
        verify.assert_called_once_with("192.0.2.1", "V23.0919", 8080)

    def test_failed_or_mismatching_readback_never_repeats_flash(self):
        for verification in (OSError("device rebooting"), {"version_verified": False,
                             "software_version": "V22.0613"}):
            with self.subTest(verification=type(verification).__name__), \
                    patch.object(firmware_cli, "update_firmware", return_value={
                        "status": "pgok_received", "bytes_body_sent": 1234}) as update, \
                    patch.object(firmware_cli, "verify_firmware_version") as verify:
                if isinstance(verification, Exception):
                    verify.side_effect = verification
                else:
                    verify.return_value = verification
                code, result, _ = self.invoke(self.arguments + ["--machine-ready", "--confirm-compatible"])
                self.assertEqual(code, 3)
                self.assertEqual(result["status"], "version_unverified")
                self.assertFalse(result["automatic_retry"])
                self.assertTrue(result["pgok_received"])
                self.assertEqual(result["bytes_body_sent"], 1234)
                update.assert_called_once()
                verify.assert_called_once()

    def test_later_verify_command_only_reads_version(self):
        with patch.object(firmware_cli, "update_firmware") as update, \
                patch.object(firmware_cli, "verify_firmware_version", return_value={
                    "version_verified": True, "software_version": "V23.0919"}) as verify:
            code, result, _ = self.invoke(["verify", "--host", "192.0.2.1",
                                          "--target-version", "V23.0919"])
        self.assertEqual(code, 0)
        self.assertTrue(result["version_verified"])
        verify.assert_called_once()
        update.assert_not_called()

    def test_interrupt_during_body_reports_warning_counts_and_exit_130(self):
        from verona import firmware
        from tests.test_firmware import Clock, FakePeer, fixture

        image = firmware.inspect_firmware(fixture())
        self.loader.return_value = image
        arguments = self.arguments.copy()
        arguments[arguments.index("--sha256") + 1] = image.sha256
        clock = Clock()
        peer = FakePeer(image, clock, fail_body_at=1024, send_error=KeyboardInterrupt())
        with patch.object(firmware.socket, "create_connection", return_value=peer) as connect, \
                patch.object(firmware.time, "monotonic", clock), \
                patch.dict("os.environ", {"XDG_STATE_HOME": str(self.path.parent / "state")}), \
                patch.object(firmware_cli, "verify_firmware_version") as verify:
            code, result, _ = self.invoke(arguments + ["--machine-ready", "--confirm-compatible"])
        self.assertEqual(code, 130)
        self.assertIn("Interrupted", result["error"])
        self.assertIn("Do not automatically repeat", result["error"])
        self.assertEqual(result["stage"], "body")
        self.assertEqual(result["bytes_body_sent"], 1024)
        self.assertEqual(result["bytes_header_sent"], len(image.header))
        self.assertTrue(result["update_may_have_started"])
        self.assertFalse(result["automatic_retry"])
        connect.assert_called_once()
        verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from verona.firmware_history import begin_attempt, finish_attempt, latest_attempt


class FirmwareHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.expected = {"MODE": "SS12", "HVER": "V7.1202_4300", "SVER": "V22.0613"}

    def begin(self, **kwargs):
        values = {"file_name": "/private/path/firmware.bin", "sha256": "a" * 64,
                  "candidate_versions": ["V26.0605"], "transport": "wifi",
                  "expected_identity": self.expected, "directory": self.root}
        values.update(kwargs)
        return begin_attempt(**values)

    def test_start_record_is_private_minimal_and_has_unknown_counts(self):
        path = self.begin()
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "started")
        self.assertEqual(record["file_name"], "firmware.bin")
        self.assertEqual(record["expected_identity"], self.expected)
        self.assertIsNone(record["bytes_header_sent"])
        self.assertIsNone(record["bytes_body_sent"])
        self.assertFalse(record["version_verified"])
        self.assertNotIn("/private/path", json.dumps(record))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_completion_atomically_updates_same_file_and_refuses_rewrite(self):
        path = self.begin()
        finish_attempt(path, status="failed", stage="header", bytes_header_sent=71,
                       bytes_body_sent=0, error_type="FirmwareUpdateError",
                       error_message="PGREADY timeout", counts_lower_bounds=True)
        record = latest_attempt(self.root)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["stage"], "header")
        self.assertEqual(record["bytes_header_sent"], 71)
        self.assertEqual(record["bytes_body_sent"], 0)
        self.assertTrue(record["counts_lower_bounds"])
        self.assertFalse(record["version_verified"])
        self.assertEqual(record["error"]["message"], "PGREADY timeout")
        with self.assertRaisesRegex(ValueError, "started"):
            finish_attempt(path, status="failed", stage="body", bytes_header_sent=71,
                           bytes_body_sent=2)

    def test_default_directory_uses_xdg_state_home(self):
        xdg = self.root / "state"
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg)}):
            path = self.begin(directory=None)
        self.assertEqual(path.parent, xdg / "verona" / "firmware-history")

    def test_default_directory_falls_back_to_local_state(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("pathlib.Path.home", return_value=self.root):
            path = self.begin(directory=None)
        self.assertEqual(path.parent, self.root / ".local/state/verona/firmware-history")

    def test_latest_limits_history_read_size(self):
        self.begin()
        newest = self.root / "attempt-zzzz-large.json"
        newest.write_text(" " * (64 * 1024 + 1), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "read limit"):
            latest_attempt(self.root)
        empty = self.root / "empty"
        self.assertIsNone(latest_attempt(empty))


if __name__ == "__main__":
    unittest.main()

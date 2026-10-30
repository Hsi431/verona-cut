"""Keep internal notes out of public source and package archives."""
from pathlib import Path
import runpy
import unittest


CHECKER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check-release.py"))


class ReleasePrivacyTests(unittest.TestCase):
    def test_internal_notes_are_rejected_without_sensitive_content(self):
        for path in ("docs/internal/handoff.md", "verona-cut-0.1/docs/internal/handoff.md"):
            with self.subTest(path=path):
                self.assertIn("private/research directory",
                              list(CHECKER["findings"](path, b"synthetic note")))

    def test_documented_public_files_are_allowed(self):
        for path in ("docs/FIRMWARE_UPDATE.md", "verona-cut-0.1/docs/ARCHITECTURE.md"):
            with self.subTest(path=path):
                self.assertEqual(list(CHECKER["findings"](path, b"public documentation")), [])


if __name__ == "__main__":
    unittest.main()

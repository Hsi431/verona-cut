"""Keep internal notes out of public source and package archives."""
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
import runpy
import tarfile
import tempfile
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

    def test_only_the_pinned_spyrrow_wheel_is_allowed_as_binary(self):
        path = "vendor/spyrrow/spyrrow-0.9.0-cp311-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
        data = (CHECKER["ROOT"] / path).read_bytes()
        self.assertEqual(list(CHECKER["findings"](path, data)), [])
        self.assertIn("unexpected binary content",
                      list(CHECKER["findings"]("vendor/spyrrow/other.whl", data)))

    def test_changed_pinned_spyrrow_wheel_is_rejected(self):
        path = "vendor/spyrrow/spyrrow-0.9.0-cp311-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
        data = (CHECKER["ROOT"] / path).read_bytes() + b"changed"
        self.assertIn("unexpected binary content",
                      list(CHECKER["findings"](path, data)))

    def test_tar_with_pinned_wheel_is_checked_as_tar(self):
        path = "vendor/spyrrow/spyrrow-0.9.0-cp311-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
        data = (CHECKER["ROOT"] / path).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "public.tar"
            with tarfile.open(archive, "w") as package:
                for name, contents in ((path, data), ("vendor/spyrrow/other.whl", data)):
                    member = tarfile.TarInfo(name)
                    member.size = len(contents)
                    package.addfile(member, BytesIO(contents))
            output = StringIO()
            with redirect_stdout(output):
                result = CHECKER["main"]([str(archive)])
        self.assertEqual(result, 1)
        report = output.getvalue()
        self.assertIn("Checked 2 files; 1 findings.", report)
        self.assertIn("vendor/spyrrow/other.whl: unexpected binary content", report)
        self.assertNotIn(f"{path}: unexpected binary content", report)


if __name__ == "__main__":
    unittest.main()

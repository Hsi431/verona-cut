"""LDS desktop integration without changing the user's file associations."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile
import zlib

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from verona import desktop_integration, thumbnailer


ROOT = Path(__file__).resolve().parents[1]


def png(width=80, height=40):
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(QColor(10, 20, 30, 128))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class LdsThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name)/"中文 設計.lds"
        self.output = Path(self.directory.name)/"thumbnail.png"

    def archive(self, previews):
        with warnings.catch_warnings(), zipfile.ZipFile(self.source, "w", zipfile.ZIP_DEFLATED) as archive:
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("CDOC", b"Geometry need not be parsed to display an embedded preview.")
            for preview in previews:
                archive.writestr("PREVIEW", preview)

    def test_preview_only_preserves_alpha_aspect_and_source_without_upscaling(self):
        self.archive([png()])
        original = self.source.read_bytes()
        original_open = zipfile.ZipFile.open

        def open_preview(archive, entry, *args, **kwargs):
            self.assertEqual(entry.filename, "PREVIEW")
            return original_open(archive, entry, *args, **kwargs)

        for size, expected in ((32, (32, 16)), (256, (80, 40))):
            with self.subTest(size=size), patch.object(zipfile.ZipFile, "open", open_preview):
                thumbnailer.create_thumbnail(self.source, self.output, size)
                image = QImage(str(self.output))
                self.assertEqual((image.width(), image.height()), expected)
                self.assertEqual(image.pixelColor(2, 2).alpha(), 128)
                self.assertEqual(self.source.read_bytes(), original)

    def test_missing_duplicate_invalid_and_oversized_previews_are_rejected(self):
        for previews in ([], [png(), png()], [b"not an image"], [b"\x89PNG\r\n\x1a\n"]):
            with self.subTest(previews=len(previews)):
                self.archive(previews)
                with self.assertRaises(ValueError):
                    thumbnailer.create_thumbnail(self.source, self.output, 128)
                self.assertFalse(self.output.exists())
        self.archive([png()])
        for constant in ("MAX_PREVIEW_BYTES", "MAX_ARCHIVE_BYTES"):
            with self.subTest(limit=constant), patch.object(thumbnailer, constant, 8), self.assertRaises(ValueError):
                thumbnailer.create_thumbnail(self.source, self.output, 128)
            self.assertFalse(self.output.exists())

    def test_excessive_png_dimensions_reject_before_decoding(self):
        header = struct.pack(">IIBBBBB", 5000, 4000, 8, 6, 0, 0, 0)
        chunk = b"IHDR" + header
        data = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + chunk + struct.pack(">I", zlib.crc32(chunk))
        self.archive([data])
        with self.assertRaisesRegex(ValueError, "dimensions"):
            thumbnailer.create_thumbnail(self.source, self.output, 128)
        self.assertFalse(self.output.exists())

    def test_invalid_sizes_and_source_overwrite_are_rejected(self):
        self.archive([png()])
        original = self.source.read_bytes()
        for size in (0, -1, 1025):
            with self.subTest(size=size), self.assertRaises(ValueError):
                thumbnailer.create_thumbnail(self.source, self.output, size)
        with self.assertRaisesRegex(ValueError, "overwrite"):
            thumbnailer.create_thumbnail(self.source, self.source, 128)
        self.assertEqual(self.source.read_bytes(), original)

    def test_cli_handles_unicode_spaces_and_foreign_working_directory(self):
        self.archive([png()])
        result = subprocess.run([sys.executable, str(ROOT/"src/verona/thumbnailer.py"),
            str(self.source), str(self.output), "32"], cwd=self.directory.name, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(QImage(str(self.output)).width(), 32)

    def test_bad_archive_returns_failure_without_output(self):
        self.source.write_bytes(b"not a zip file")
        with patch("sys.stderr"):
            self.assertEqual(thumbnailer.main([str(self.source), str(self.output), "128"]), 1)
        self.assertFalse(self.output.exists())


class DesktopIntegrationTests(unittest.TestCase):
    def test_installer_registers_jdesign_and_lds_only_in_user_data_directories(self):
        with tempfile.TemporaryDirectory() as folder:
            data_home = Path(folder)/"data"
            with patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}), \
                    patch.object(desktop_integration.subprocess, "run") as run:
                desktop_integration.install_desktop()
            applications = data_home/"applications"
            for language, argument in (("zh-TW", "zh_TW"), ("en", "en")):
                entry = applications/f"verona-{language}.desktop"
                text = entry.read_text()
                self.assertIn(f"Exec={desktop_integration._exec_argument(sys.executable)} -m verona.desktop_integration --launch {argument} %f\n", text)
                self.assertIn(f"MimeType={desktop_integration.DESIGN_MIME};{desktop_integration.LDS_MIME};\n", text)
            self.assertIn('<glob pattern="*.lds"/>', (data_home/"mime/packages/verona.xml").read_text())
            self.assertIn('<glob pattern="*.jdesign"/>', (data_home/"mime/packages/verona.xml").read_text())
            self.assertIn("-m verona.thumbnailer %i %o %s", (data_home/"thumbnailers/verona-lds.thumbnailer").read_text())
            self.assertEqual([call.args[0] for call in run.call_args_list], [
                ["update-mime-database", str(data_home/"mime")],
                ["update-desktop-database", str(applications)],
                ["xdg-mime", "default", "verona-en.desktop", desktop_integration.DESIGN_MIME],
                ["xdg-mime", "default", "verona-en.desktop", desktop_integration.LDS_MIME]])

    def test_installed_launcher_preserves_filename_and_opens_without_a_file(self):
        with patch("verona.ui.main", return_value=0) as launch:
            for suffix in (".lds", ".jdesign"):
                filename = "中文 空格 $(not-a-command)" + suffix
                self.assertEqual(desktop_integration.main(["--launch", "en", filename]), 0)
                launch.assert_called_once_with(["--lang", "en", "--open", filename])
                launch.reset_mock()
            launch.reset_mock()
            self.assertEqual(desktop_integration.main(["--launch", "zh_TW"]), 0)
            launch.assert_called_once_with(["--lang", "zh_TW"])

    def test_launcher_opens_exact_filename_and_menu_launch_needs_no_file(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)/"project with spaces"
            project.mkdir()
            launcher = project/"run-desktop.sh"
            shutil.copyfile(ROOT/"run-desktop.sh", launcher)
            launcher.chmod(0o755)
            recorded = project/"arguments.json"
            runner = project/"run.sh"
            runner.write_text("#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n"
                              f"Path({str(recorded)!r}).write_text(json.dumps(sys.argv[1:]))\n")
            runner.chmod(0o755)
            filename = str(project/"中文 空格 $(not-a-command).lds")
            subprocess.run([str(launcher), "zh_TW", filename], check=True)
            self.assertEqual(json.loads(recorded.read_text()), ["--lang", "zh_TW", "--open", filename])
            subprocess.run([str(launcher), "en"], check=True)
            self.assertEqual(json.loads(recorded.read_text()), ["--lang", "en"])


if __name__ == "__main__":
    unittest.main()

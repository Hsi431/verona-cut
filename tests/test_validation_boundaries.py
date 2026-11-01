"""Backend document consumers validate without importing the Qt editor."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tests.test_lds import document as native_document, vector


class ValidationBoundaryTests(unittest.TestCase):
    def test_cli_mcp_and_lds_validation_do_not_import_ui(self):
        design = {"format": "verona", "version": 1, "units": "mm",
                  "items": [{"name": "synthetic", "contours": [[[1, 2], [3, 4]]]}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design_path = root / "synthetic.jdesign"
            design_path.write_text(json.dumps(design), encoding="utf-8")

            cdoc = native_document(vector([(0, 0, 16), (8, 8, 1)]))
            editable = {**design, "cdoc_sha256": hashlib.sha256(cdoc).hexdigest()}
            lds_path = root / "synthetic.lds"
            with zipfile.ZipFile(lds_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("CDOC", cdoc)
                archive.writestr("VERONA.json", json.dumps(editable))

            script = r"""
import sys
from pathlib import Path
from verona.cli import prepare_job
from verona.lds import load_lds
from verona.mcp_server import VeronaController

design, native = sys.argv[1:]
assert prepare_job(design).startswith(b"IN;")
controller = VeronaController("192.0.2.1", lock_path=Path(native).with_suffix(".lock"))
assert controller.prepare_job(design)["bytes"] > 0
assert load_lds(native).items[0]["name"] == "synthetic"
assert "verona.ui" not in sys.modules
"""
            environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                           "XDG_STATE_HOME": directory}
            result = subprocess.run([sys.executable, "-c", script, str(design_path), str(lds_path)],
                                    cwd=Path(__file__).resolve().parents[1], env=environment,
                                    text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

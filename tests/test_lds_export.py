"""Real native records, complete editable round trips and safe LDS replacement."""
import copy
import io
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtGui import QAction, QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from verona.artwork import draw_printed_item
from verona.commands import encode_document
from verona.i18n import set_language, tr
from verona.lds import load_lds
from verona import lds_export
from verona.native_images import decode_qoi, encode_qoi
from verona.ui import VeronaWindow
from tests.test_editor_actions import image_item, rectangle
from tests.test_lds import document as native_document, vector
from tests.test_nodes import curved
from verona.nodes import edit_path


def design(items, metadata=None):
    return {"format": "verona", "version": 1, "units": "mm", "items": items,
            "settings": {"custom": True, "speed": 5, "force": 10, "tool": 1, "passes": 1,
                         "connection": "wifi", "host": "", "port": 8080},
            "metadata": metadata or {}}


class QoiEncodingTests(unittest.TestCase):
    def test_encoder_matches_known_pixel_and_existing_decoder_across_chunk_types(self):
        red = encode_qoi(1, 1, b"\xff\x00\x00\xff")
        self.assertEqual(red[14:-8], b"\xfe\xff\x00\x00")
        pixels = [(0,0,0,255)] * 70 + [(1,1,1,255),(12,11,13,255),
                  (200,40,20,255),(20,50,90,0),(1,1,1,255)]
        randomizer = random.Random(42)
        pixels += [tuple(randomizer.randrange(256) for _ in range(4)) for _ in range(500)]
        rgba = bytes(value for pixel in pixels for value in pixel)
        self.assertEqual(decode_qoi(encode_qoi(len(pixels), 1, rgba)), (len(pixels), 1, rgba))

    def test_bad_dimensions_and_lengths_fail_before_encoding(self):
        for width, height, pixels in ((0,1,b""),(True,1,b"0000"),(2,1,b"0000"),(16000001,1,b"")):
            with self.subTest(size=(width,height)), self.assertRaises(ValueError):
                encode_qoi(width,height,pixels)


class LdsExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, str(self.root/"settings"))
        set_language("en")
        self.window = VeronaWindow(host="")

    def tearDown(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def save_archive(self, payload, *, omit=(), replace=None):
        path = self.root/"saved.lds"
        with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as target:
            for name in source.namelist():
                if name not in omit:
                    target.writestr(name, (replace or {}).get(name, source.read(name)))
        return path

    def source_lds(self):
        path = self.root/"original.lds"
        with zipfile.ZipFile(path,"w",zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("CDOC",native_document(vector([(20,20,16),(60,20,1),(60,60,1),(20,20,129)])))
        return path

    def test_complete_editable_state_and_cut_bytes_round_trip_with_real_native_records(self):
        items = [rectangle("曲線切割",20,20,40,40, group_path=["outer"]),
                 image_item(group_path=["outer"], bleed={"width_mm":1.,"color":"#00ff00"}),
                 rectangle("hidden",120,20,20,20,visible=False),
                 rectangle("locked",160,20,20,20,locked=True,print=False),
                 rectangle("no output",200,20,20,20,cut=False,print=False)]
        data = design(items, {"print_cut":{"enabled":True,"paper_width":210.,"paper_height":297.,
                                            "bounds":[6.,6.,204.,291.],"offset":[.1,-.1]},
                              "optimize_paths":True})
        data["settings"]["passes"] = 3
        original = copy.deepcopy(data)
        payload = lds_export.encode_lds(data)
        loaded = load_lds(self.save_archive(payload))
        self.assertEqual(loaded.items, data["items"])
        self.assertEqual(loaded.metadata, data["metadata"])
        self.assertEqual(loaded.settings, data["settings"])
        self.assertEqual(encode_document(loaded.items,loaded.metadata,loaded.settings),
                         encode_document(data["items"],data["metadata"],data["settings"]))
        self.assertEqual(data, original)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(set(archive.namelist()), {"CDOC","PREVIEW","VERONA.json"})
            self.assertTrue(archive.read("CDOC").startswith(b"DLCF"))
            self.assertTrue(archive.read("PREVIEW").startswith(b"\x89PNG\r\n\x1a\n"))
        native = load_lds(self.save_archive(payload,omit=("VERONA.json",)))
        self.assertEqual(sum(item["cut"] for item in native.items),3)
        self.assertEqual(native.metadata["print_cut"]["bounds"],[6.,6.,204.,291.])
        self.assertTrue(any("image_data" in item for item in native.items))

    def test_native_print_only_cut_only_and_printed_vector_have_distinct_outputs(self):
        source = [image_item(cut=False), image_item(print=False),
                  rectangle("blue print",70,70,20,20,cut=False,
                            print_style={"fill":"#0000ff","stroke":None,"width_mm":.2})]
        payload = lds_export.encode_lds(design(source))
        native = load_lds(self.save_archive(payload,omit=("VERONA.json",)))
        self.assertEqual([item["cut"] for item in native.items],[False,True,False])
        self.assertIn("image_data",native.items[0])
        self.assertNotIn("image_data",native.items[1])
        image = QImage(120,120,QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        painter = QPainter(image)
        draw_printed_item(painter,native.items[2])
        painter.end()
        self.assertEqual(image.pixelColor(80,80),QColor("blue"))

    def test_shared_images_use_one_native_definition(self):
        first = image_item()
        second = copy.deepcopy(first)
        second["image_transform"][0] += 50
        payload = lds_export.encode_lds(design([first,second]))
        native = load_lds(self.save_archive(payload,omit=("VERONA.json",)))
        self.assertIs(native.items[0]["image_data"], native.items[1]["image_data"])
        self.assertEqual(native.items[1]["image_transform"][0],70)

    def test_cubic_controls_and_original_pixels_remain_editable_after_native_save(self):
        item=edit_path(image_item(),0,curved())
        item["name"]="Editable curve"
        data=design([item])
        loaded=load_lds(self.save_archive(lds_export.encode_lds(data)))
        self.assertEqual(loaded.items[0]["node_paths"],item["node_paths"])
        self.assertEqual(loaded.items[0]["image_data"],item["image_data"])
        self.assertEqual(loaded.items[0]["image_clip"],item["image_clip"])
        self.assertEqual(encode_document(loaded.items,loaded.metadata,loaded.settings),
                         encode_document(data["items"],data["metadata"],data["settings"]))

    def test_empty_and_hidden_only_designs_keep_editable_objects(self):
        for items in ([],[rectangle("hidden",visible=False)]):
            with self.subTest(count=len(items)):
                loaded=load_lds(self.save_archive(lds_export.encode_lds(design(items))))
                self.assertEqual(loaded.items,items)

    def test_changed_native_payload_ignores_stale_editable_snapshot(self):
        payload = lds_export.encode_lds(design([rectangle("old")]))
        updated=native_document(vector([(90,90,16),(100,90,33)]))
        loaded=load_lds(self.save_archive(payload,replace={"CDOC":updated}))
        self.assertEqual(loaded.items[0]["contours"],[[(90.,90.),(100.,90.)]])
        self.assertIsNone(loaded.settings)

    def test_invalid_or_oversized_editable_data_is_rejected(self):
        payload=lds_export.encode_lds(design([rectangle()]))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            snapshot=json.loads(archive.read("VERONA.json"))
        snapshot["items"][0]["locked"]="not a boolean"
        path=self.save_archive(payload,replace={"VERONA.json":json.dumps(snapshot).encode()})
        with self.assertRaises(ValueError):
            load_lds(path)
        path=self.save_archive(payload)
        with patch("verona.input_safety.MAX_DESIGN_BYTES",8),self.assertRaises(ValueError):
            load_lds(path)

    def test_native_limits_and_nonfinite_values_fail_before_a_file_is_written(self):
        data=design([rectangle()])
        with patch.object(lds_export,"_MAX_DOCUMENT",60),self.assertRaises(ValueError):
            lds_export.encode_lds(data)
        with patch.object(lds_export,"_MAX_POINTS",1),self.assertRaises(ValueError):
            lds_export.encode_lds(data)
        data["items"][0]["contours"][0][0]=(float("nan"),0)
        with self.assertRaises(ValueError):
            lds_export.encode_lds(data)

    def test_ctrl_s_saves_native_in_place_with_first_original_backup_and_reopens(self):
        path=self.source_lds()
        path.chmod(0o600)
        original=path.read_bytes()
        self.assertTrue(self.window._load_document(str(path)))
        self.window.items[0]["name"]="First edit"
        save=next(action for action in self.window.findChildren(QAction) if action.shortcut().toString()=="Ctrl+S")
        with patch.object(QFileDialog,"getSaveFileName") as dialog:
            save.trigger()
            dialog.assert_not_called()
        backup=path.with_name(path.name+".bak")
        self.assertEqual(backup.read_bytes(),original)
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(backup.stat().st_mode&0o777,0o600)
        self.assertEqual(load_lds(path).items[0]["name"],"First edit")
        self.assertFalse(self.window.is_dirty())
        self.window.items[0]["name"]="Second edit"
        self.assertTrue(self.window.save_design())
        self.assertEqual(backup.read_bytes(),original)
        self.assertEqual(load_lds(path).items[0]["name"],"Second edit")

    def test_save_as_accepts_native_filter_and_keeps_the_current_document_on_cancel(self):
        self.window.items=[rectangle()]
        self.window.select(0)
        output=self.root/"新的 設計"
        native_filter=tr("LDS 設計（原廠相容性未驗證） (*.lds)")
        with patch.object(QFileDialog,"getSaveFileName",return_value=(str(output),native_filter)):
            self.assertTrue(self.window.save_design(save_as=True))
        output=output.with_suffix(".lds")
        self.assertEqual(self.window.current_file,str(output))
        self.assertEqual(load_lds(output).items,self.window.items)
        before=self.window._snapshot()
        with patch.object(QFileDialog,"getSaveFileName",return_value=("","")):
            self.assertFalse(self.window.save_design(save_as=True))
        self.assertEqual(self.window._snapshot(),before)

    def test_failed_replacement_or_backup_keeps_original_and_unsaved_state(self):
        for operation in ("os.replace","shutil.copyfileobj"):
            with self.subTest(operation=operation):
                path=self.source_lds()
                original=path.read_bytes()
                self.window._load_document(str(path))
                self.window.items[0]["name"]="Unsaved change"
                with patch("verona.ui."+operation,side_effect=OSError("test failure")), \
                        patch.object(QMessageBox,"critical") as message:
                    self.assertFalse(self.window.save_design())
                    message.assert_called_once()
                self.assertEqual(path.read_bytes(),original)
                self.assertTrue(self.window.is_dirty())
                self.assertEqual(list(self.root.glob(".original.lds.*.tmp")),[])
                backup=path.with_name(path.name+".bak")
                if backup.exists():
                    self.assertEqual(backup.read_bytes(),original)
                    backup.unlink()

    def test_create_only_native_save_refuses_existing_file(self):
        path=self.source_lds()
        original=path.read_bytes()
        self.window.items=[rectangle()]
        with self.assertRaises(FileExistsError):
            self.window._write_document(str(path),overwrite=False)
        self.assertEqual(path.read_bytes(),original)
        self.assertFalse(path.with_name(path.name+".bak").exists())


if __name__=="__main__":
    unittest.main()

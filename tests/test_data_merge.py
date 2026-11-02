"""Data merge, editable text commands, and command schema drift coverage."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QApplication

from verona.command_registry import COMMANDS, COMMANDS_BY_ID
from verona.editor_actions import build_edit
from verona.editor_bridge import EditorBridge
from verona.editor_document import build_document_edit
from verona.data_merge import DataMergeDialog
from verona.text_tool import _font, font_families, font_styles, make_text_data, render_text_path, text_transform
from verona.ui import VeronaWindow
from verona.geometry import flatten_path


TEMP_ROOT = Path("/tmp/variable-data-tests")


def rectangle(name="label", x=10, y=10, width=35, height=35):
    return {"name": name, "contours": [[(x, y), (x+width, y), (x+width, y+height),
                                          (x, y+height), (x, y)]], "cut": True}


def text_item(text="Name {name}", x=12, y=12, height=6):
    family = next(name for name in font_families()
                  if QFontMetricsF(_font(name)).inFontUcs4(ord("王")))
    data = make_text_data(text, family, font_styles(family)[0], height,
                          0, height * 1.2, "left", (x, y))
    contours = flatten_path(render_text_path(data))
    return {"name": "Name", "contours": contours, "cut": True,
            "text_data": data, "text_source_contours": contours}


class DataMergeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TEMP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        cls.settings = tempfile.TemporaryDirectory(prefix="settings-", dir=TEMP_ROOT)
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings.cleanup()

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="case-", dir=TEMP_ROOT)
        self.window = VeronaWindow()
        self.bridge = EditorBridge(self.window, directory=Path(self.workspace.name) / "bridge", listen=False)

    def tearDown(self):
        self.bridge.close()
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        self.workspace.cleanup()

    @staticmethod
    def build(items, action, parameters, selection, metadata=None):
        return build_document_edit(items, metadata or {}, {}, selection, action, parameters)

    def test_text_edit_preserves_position_rotation_mirror_and_scale(self):
        created, *_ = self.build([], "add_text", {
            "text": "Before", "font_family": font_families()[0],
            "font_style": font_styles(font_families()[0])[0], "height_mm": 8,
            "x_mm": 23, "y_mm": 31,
        }, [])
        transformed, _, _ = build_edit(created, {}, [0], "transform", {
            "dx_mm": 19, "dy_mm": 7, "scale": 1.4, "rotation_deg": 31,
            "mirror_x": True, "mirror_y": False,
        })
        before = transformed[0]
        transform_before = text_transform(before["text_source_contours"], before["contours"])
        edited, _, _, selection, _ = self.build(transformed, "edit_text", {"text": "After"}, [0])
        transform_after = text_transform(edited[0]["text_source_contours"], edited[0]["contours"])
        self.assertEqual(selection, [0])
        self.assertEqual(edited[0]["text_data"]["content"], "After")
        for name in ("m11", "m12", "m21", "m22", "dx", "dy"):
            self.assertAlmostEqual(getattr(transform_after, name)(), getattr(transform_before, name)(), places=5)

    def test_text_outline_uses_command_action_and_undo_restores_editable_metadata(self):
        item = text_item("VERONA")
        self.window.items = [copy.deepcopy(item)]
        self.window.refresh_all()
        self.window.select(0)
        self.window._execute_registered_command("text.outline")
        self.assertNotIn("text_data", self.window.items[0])
        self.assertNotIn("text_source_contours", self.window.items[0])
        self.window.undo()
        self.assertEqual(self.window.items[0]["text_data"], item["text_data"])

    def test_three_rows_make_three_text_copies_and_keep_the_template(self):
        template = [rectangle(width=30, height=15), text_item("Hello {name} · No.{number}")]
        original = copy.deepcopy(template)
        result, _, _, selection, details = self.build(template, "data_merge", {
            "rows": [{"name": "王小明", "number": "01"},
                    {"name": "陳怡君", "number": "02"},
                    {"name": "林大偉", "number": "03"}],
        }, [0, 1])
        self.assertEqual(len(selection), 6)
        self.assertEqual(len(result), len(template) * 4)
        self.assertEqual([result[i]["text_data"]["content"] for i in selection[1::2]], [
            "Hello 王小明 · No.01", "Hello 陳怡君 · No.02", "Hello 林大偉 · No.03",
        ])
        self.assertEqual(result[:2], original)
        self.assertEqual(details["rows"], 3)

    def test_utf8_bom_csv_parses_chinese_headers_and_values(self):
        result, _, _, selection, _ = self.build([text_item("{姓名}／{編號}")], "data_merge", {
            "csv_text": "\ufeff姓名,編號\n王小明,001\n陳怡君,002\n",
        }, [0])
        self.assertCountEqual([result[index]["text_data"]["content"] for index in selection],
                              ["王小明／001", "陳怡君／002"])

    def test_data_merge_dialog_previews_fields_and_first_five_chinese_rows(self):
        dialog = DataMergeDialog([text_item("姓名：{姓名}／編號：{編號}")], [0])
        dialog.csv_edit.setPlainText("姓名,編號\n王小明,001\n陳怡君,002\n林大偉,003\n"
                                     "周雅婷,004\n吳承翰,005\n鄭心怡,006\n")
        self.assertEqual(dialog.preview_table.columnCount(), 2)
        self.assertEqual(dialog.preview_table.rowCount(), 5)
        self.assertEqual(dialog.preview_table.horizontalHeaderItem(0).text(), "姓名")
        self.assertEqual(dialog.preview_table.item(0, 0).text(), "王小明")
        self.assertEqual(dialog.preview_table.item(4, 1).text(), "005")
        self.assertTrue(dialog.apply_button.isEnabled())
        dialog.close()

    def test_missing_and_empty_fields_fail_in_preview_without_mutating_source(self):
        source = [text_item("{姓名} {編號}")]
        original = copy.deepcopy(source)
        for parameters, expected in (
            ({"rows": [{"姓名": "王小明"}]}, r"(?:第 1 列|Row 1).*編號"),
            ({"rows": [{"姓名": "", "編號": "01"}]}, r"(?:第 1 列|Row 1).*姓名"),
        ):
            with self.subTest(parameters=parameters), self.assertRaisesRegex(ValueError, expected):
                self.build(source, "data_merge", parameters, [0])
            self.assertEqual(source, original)

    def test_missing_font_glyph_is_reported_with_its_row_and_field(self):
        unsupported = chr(0x10ffff)
        with self.assertRaisesRegex(ValueError, r"(?:Row 1|第 1 列).*glyph"):
            self.build([text_item("{glyph}")], "data_merge", {
                "rows": [{"glyph": unsupported}], "auto_pack": False,
            }, [0])

    def test_overflow_packing_opens_new_tabs_when_applied(self):
        template = [rectangle(), text_item("{name}")]
        self.window.items = copy.deepcopy(template)
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 40,
            "paper_height": 40, "bounds": [1, 1, 39, 39]}}
        self.window.refresh_all()
        initial_tabs = len(self.window._documents)
        revision = self.bridge.revision()
        plan = self.bridge.dispatch("command_batch_preview", expected_revision=revision, steps=[
            {"command_id": "data.merge", "object_indices": [0, 1], "parameters": {
                "rows": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
                "auto_pack": True, "new_page_for_unplaced": True, "gap_mm": 0,
                "time_limit_seconds": 1,
            }},
        ])
        self.assertGreater(plan["details"]["steps"][0]["page_count"], 1)
        self.bridge.dispatch("apply", expected_revision=revision, plan_id=plan["plan_id"])
        self.assertGreater(len(self.window._documents), initial_tabs)
        self.window.undo()
        self.assertEqual(len(self.window._documents), initial_tabs)
        self.assertEqual(self.window.items, template)
        self.window.redo()
        self.assertGreater(len(self.window._documents), initial_tabs)

    def test_gui_overflow_pages_undo_and_redo_as_one_operation(self):
        template = [rectangle(), text_item("{name}")]
        self.window.items = copy.deepcopy(template)
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 40,
            "paper_height": 40, "bounds": [1, 1, 39, 39]}}
        self.window.refresh_all()
        self.window._execute_registered_command("data.merge", [0, 1], {
            "rows": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
            "auto_pack": True, "new_page_for_unplaced": True, "gap_mm": 0,
            "time_limit_seconds": 1,
        })
        self.assertGreater(len(self.window._documents), 1)
        self.window.undo()
        self.assertEqual(len(self.window._documents), 1)
        self.assertEqual(self.window.items, template)
        self.window.redo()
        self.assertGreater(len(self.window._documents), 1)

    def test_mcp_command_batch_applies_data_merge_as_one_undo_step(self):
        self.window.items = [text_item("{name}")]
        self.window.refresh_all()
        revision = self.bridge.revision()
        plan = self.bridge.dispatch("command_batch_preview", expected_revision=revision, steps=[
            {"command_id": "data.merge", "object_indices": [0],
             "parameters": {"rows": [{"name": "Ada"}, {"name": "Lin"}]}},
        ])
        applied = self.bridge.dispatch("apply", expected_revision=revision, plan_id=plan["plan_id"])
        self.assertEqual(applied["undo_steps"], 1)
        self.assertEqual(len(self.window.items), 3)
        self.assertEqual(self.window.items[1]["text_data"]["content"], "Ada")
        self.window.undo()
        self.assertEqual(len(self.window.items), 1)

    def test_registered_parameters_match_actual_action_acceptance(self):
        from verona.editor_document import ACTION_PARAMETER_NAMES

        for command in COMMANDS:
            with self.subTest(command=command["id"]):
                actual = ACTION_PARAMETER_NAMES[command["action"]]
                declared = {parameter["name"] for parameter in command["parameters"]}
                declared.update(command.get("fixed", {}))
                self.assertEqual(declared, actual)

    def test_parameter_drift_guard_detects_a_missing_registration(self):
        from verona.editor_document import ACTION_PARAMETER_NAMES

        command = copy.deepcopy(COMMANDS_BY_ID["arrange.pack"])
        command["parameters"] = [parameter for parameter in command["parameters"]
                                 if parameter["name"] != "new_page_for_unplaced"]
        declared = {parameter["name"] for parameter in command["parameters"]}
        declared.update(command.get("fixed", {}))
        with self.assertRaises(AssertionError):
            self.assertEqual(declared, ACTION_PARAMETER_NAMES[command["action"]])


if __name__ == "__main__":
    unittest.main()

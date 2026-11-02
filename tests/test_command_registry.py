"""Registered menu coverage and detached command execution regressions."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog

from verona.command_registry import COMMANDS, COMMANDS_BY_ID, MENU_ACTION_IDS, MENU_EXCLUSIONS
from verona.editor_bridge import EditorBridge
from verona.i18n import tr
from verona.ui_translations import ENGLISH
from verona.ui import VeronaWindow
from tests.test_editor_actions import image_item, rectangle


TEMP_ROOT = Path("/tmp/mcp-commands-tests")


class CommandRegistryTests(unittest.TestCase):
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
    def install(window, items, selection):
        window.items = copy.deepcopy(items)
        window.metadata = {}
        window.selected, window.selection = -1, set()
        window.undo_stack.clear()
        window.redo_stack.clear()
        window.refresh_all()
        window.select_many(selection)

    @staticmethod
    def normalized_state(window):
        state = copy.deepcopy(window._document_state())
        groups = {}
        for item in state["items"]:
            for position, group_id in enumerate(item.get("group_path", [])):
                key = position, group_id
                groups.setdefault(key, f"group-{len(groups)}")
                item["group_path"][position] = groups[key]
        return state

    def test_every_main_menu_action_has_a_registry_entry_or_reasoned_exclusion(self):
        seen_commands = set()
        seen_exclusions = set()

        def scan(menu):
            for action in menu.actions():
                if action.isSeparator():
                    continue
                if action.menu() is not None:
                    scan(action.menu())
                    continue
                command_id = action.data()
                self.assertIsInstance(command_id, str, action.text())
                if command_id in COMMANDS_BY_ID:
                    seen_commands.add(command_id)
                else:
                    self.assertIn(command_id, MENU_EXCLUSIONS, action.text())
                    self.assertTrue(MENU_EXCLUSIONS[command_id].strip())
                    seen_exclusions.add(command_id)

        for action in self.window.menuBar().actions():
            if action.menu() is not None:
                scan(action.menu())
        self.assertEqual(seen_commands, set(MENU_ACTION_IDS))
        self.assertEqual(seen_exclusions, set(MENU_EXCLUSIONS))
        self.assertEqual(len(COMMANDS_BY_ID), len(COMMANDS))
        for command in COMMANDS:
            self.assertEqual(command["label_en"], ENGLISH[command["label_zh"]])

    def test_command_list_uses_explicit_objects_and_reports_image_boolean_rejection(self):
        self.install(self.window, [image_item()], [0])
        values = self.bridge.dispatch("command_list", object_indices=[0])
        weld = next(command for command in values if command["id"] == "path.weld")
        self.assertFalse(weld["enabled"])
        self.assertIn("Images cannot be used in path operations", weld["disabled_reason"])
        duplicate = next(command for command in values if command["id"] == "edit.duplicate")
        self.assertTrue(duplicate["enabled"])
        self.assertIn("accepts_images", duplicate["selection"])
        shape = next(command for command in values if command["id"] == "shape.add_rectangle")
        self.assertEqual(shape["selection"]["minimum"], 0)
        self.assertIsNone(shape["selection"]["maximum"])

    def test_command_preview_apply_matches_gui_for_registered_action_classes(self):
        cases = (
            ("shape.add_rectangle", [], [], {"name": tr("矩形")},
             lambda window: window.add_rectangle(), []),
            ("edit.transform", [0], [0], {"rotation_deg": 90},
             lambda window: window.transform_selected(rotation=90), [rectangle()]),
            ("edit.set_cut", [0], [0], {"enabled": False},
             lambda window: window._cut_toggled(False), [rectangle()]),
            ("edit.duplicate", [0], [0], {},
             lambda window: window.duplicate_selected(), [rectangle()]),
            ("edit.delete", [0], [0], {},
             lambda window: window.delete_selected(), [rectangle()]),
            ("edit.group", [0, 1], [0, 1], {},
             lambda window: window.group_selected(), [rectangle("a"), rectangle("b", x=60)]),
            ("edit.ungroup", [0], [0], {},
             lambda window: window.ungroup_selected(), [rectangle(group_path=["group"])]),
            ("path.weld", [0, 1], [0, 1], {},
             lambda window: window.boolean_selected("weld"),
             [rectangle("a"), rectangle("b", x=30)]),
            ("arrange.bring_forward", [0], [0], {},
             lambda window: window.move_layers(1),
             [rectangle("a"), rectangle("b", x=60), rectangle("c", x=100)]),
            ("align.left", [0, 1], [0, 1], {},
             lambda window: window.align_selected("left"),
             [rectangle("a", x=10), rectangle("b", x=60, y=50)]),
            ("path.smooth", [0], [0], {"tolerance_mm": .1},
             self._smooth_gui,
             [rectangle(contours=[[(20, 20), (22, 20.15), (24, 20), (26, 20.15),
                                   (28, 20), (30, 20.15), (32, 20), (34, 20.15),
                                   (36, 20), (38, 20.15), (40, 20), (40, 30),
                                   (20, 30), (20, 20)]])]),
        )
        for command_id, command_indices, gui_selection, parameters, gui_call, items in cases:
            with self.subTest(command_id=command_id):
                gui = VeronaWindow()
                try:
                    self.install(self.window, items, command_indices)
                    self.install(gui, items, gui_selection)
                    before = copy.deepcopy(self.window._document_state())
                    revision = self.bridge.revision()
                    plan = self.bridge.dispatch("command_preview", expected_revision=revision,
                        command_id=command_id, object_indices=command_indices, parameters=parameters)
                    self.assertEqual(self.window._document_state(), before)
                    self.assertEqual(len(self.window.undo_stack), 0)
                    self.bridge.dispatch("apply", expected_revision=revision, plan_id=plan["plan_id"])
                    gui_call(gui)
                    self.assertEqual(self.normalized_state(self.window), self.normalized_state(gui))
                    self.assertEqual(self.window.selected_indices(), gui.selected_indices())
                    self.assertEqual(len(self.window.undo_stack), 1)
                    self.assertEqual(len(gui.undo_stack), 1)
                finally:
                    with patch.object(gui, "_confirm_unsaved_changes", return_value="discard"):
                        gui.close()

    @staticmethod
    def _smooth_gui(window):
        class Dialog:
            class Spin:
                @staticmethod
                def value():
                    return .1

            tolerance_spin = Spin()

            @staticmethod
            def exec():
                return QDialog.DialogCode.Accepted

            @staticmethod
            def deleteLater():
                return None

        with patch("verona.smoothing_dialog.ContourSmoothingDialog", return_value=Dialog()):
            window.smooth_selected_cutlines()

    def test_batch_text_duplicate_weld_applies_as_one_undo_and_undoes_to_original(self):
        self.install(self.window, [rectangle("base")], [])
        before = copy.deepcopy(self.window._document_state())
        revision = self.bridge.revision()
        plan = self.bridge.dispatch("command_batch_preview", expected_revision=revision, steps=[
            {"command_id": "text.add", "object_indices": [],
             "parameters": {"text": "A", "x_mm": 40, "y_mm": 40}},
            {"command_id": "edit.duplicate", "object_indices": [1], "parameters": {}},
            {"command_id": "path.weld", "object_indices": [1, 2], "parameters": {}},
        ])
        self.assertEqual(plan["details"]["step_count"], 3)
        self.assertEqual(self.window._document_state(), before)
        applied = self.bridge.dispatch("apply", expected_revision=revision, plan_id=plan["plan_id"])
        self.assertEqual(applied["undo_steps"], 1)
        self.assertEqual(len(self.window.items), 2)
        undone = self.bridge.dispatch("history", expected_revision=applied["revision"])
        self.assertEqual(undone["undo_steps"], 0)
        self.assertEqual(self.window._document_state(), before)

    def test_batch_failure_reports_step_and_creates_no_plan_or_live_change(self):
        self.install(self.window, [rectangle("base")], [])
        before = copy.deepcopy(self.window._document_state())
        revision = self.bridge.revision()
        with self.assertRaisesRegex(ValueError, r"Step 2 \(path.weld\) failed"):
            self.bridge.dispatch("command_batch_preview", expected_revision=revision, steps=[
                {"command_id": "text.add", "object_indices": [], "parameters": {"text": "A"}},
                {"command_id": "path.weld", "object_indices": [1], "parameters": {}},
            ])
        self.assertEqual(self.bridge.plans, {})
        self.assertEqual(self.window._document_state(), before)
        self.assertEqual(self.window.undo_stack, [])

    def test_command_preview_rejects_expired_revision(self):
        self.install(self.window, [rectangle()], [0])
        revision = self.bridge.revision()
        self.window.items[0]["name"] = "changed"
        with self.assertRaisesRegex(ValueError, "The editor changed"):
            self.bridge.dispatch("command_preview", expected_revision=revision,
                command_id="edit.duplicate", object_indices=[0], parameters={})
        self.assertEqual(self.bridge.plans, {})


if __name__ == "__main__":
    unittest.main()

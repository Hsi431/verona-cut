"""Read-only UI screenshot, widget inspection and status regressions."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (QApplication, QDialog, QLabel, QLineEdit, QMenu,
                               QPushButton, QVBoxLayout)

from verona.editor_bridge import EditorBridge
from verona.ui import VeronaWindow
from tests.test_editor_actions import rectangle


TEMP_ROOT = Path("/tmp/mcp-ui-view-tests")


class UiIntrospectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        cls.settings = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, cls.settings.name)
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.settings.cleanup()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.window = VeronaWindow()
        self.window.items = [rectangle("outside", x=75, y=75, width=15, height=15)]
        self.window.metadata = {"print_cut": {"enabled": True, "paper_width": 100,
                                               "paper_height": 100, "bounds": [10, 10, 70, 70]}}
        self.window.refresh_all()
        self.directory = Path(self.folder.name) / "bridge"
        self.bridge = EditorBridge(self.window, directory=self.directory, listen=False)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        for dialog in self.window.findChildren(QDialog):
            dialog.close()
        self.bridge.close()
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        self.folder.cleanup()
        self.app.processEvents()

    def test_window_and_canvas_screenshots_are_sized_and_nonblank(self):
        result = self.bridge.dispatch("ui_screenshot", target="window", max_width_px=320)
        self.assertEqual(result["target"], "window")
        self.assertLessEqual(result["width_px"], 320)
        image = QImage(result["path"])
        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()),
                         (result["width_px"], result["height_px"]))
        colors = {image.pixelColor(x, y).rgba()
                  for x in range(0, image.width(), max(1, image.width() // 12))
                  for y in range(0, image.height(), max(1, image.height() // 12))}
        self.assertGreater(len(colors), 1)
        canvas = self.bridge.dispatch("ui_screenshot", target="canvas", max_width_px=1600)
        self.assertGreater(canvas["width_px"], 0)
        self.assertGreater(canvas["height_px"], 0)

    def test_dialog_inspection_keeps_focus_value_and_revision_unchanged(self):
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Text settings")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Current text"))
        field = QLineEdit("still editing")
        field.setObjectName("textValue")
        layout.addWidget(field)
        hidden = QLineEdit("hidden value")
        hidden.setObjectName("hiddenValue")
        hidden.hide()
        layout.addWidget(hidden)
        command = QPushButton("Apply layout")
        command.setObjectName("applyLayout")
        command.setProperty("verona_command_id", "arrange.pack")
        layout.addWidget(command)
        dialog.resize(360, 180)
        dialog.show()
        dialog.activateWindow()
        field.setFocus()
        self.app.processEvents()
        finished = []
        field.editingFinished.connect(lambda: finished.append(True))
        before_revision = self.bridge.revision()
        before_focus = QApplication.focusWidget()

        tree = self.bridge.dispatch("ui_inspect", target="active_dialog", max_depth=8)
        shot = self.bridge.dispatch("ui_screenshot", target="active_dialog", max_width_px=1600)

        self.assertEqual(tree["target"], "active_dialog")
        self.assertEqual(tree["root"]["text"], "Text settings")
        nodes = _flatten(tree["root"])
        by_name = {node["objectName"]: node for node in nodes}
        self.assertIn("textValue", by_name)
        self.assertEqual(by_name["textValue"].get("value"), "still editing")
        self.assertNotIn("hiddenValue", by_name)
        self.assertEqual(by_name["applyLayout"]["command_id"], "arrange.pack")
        self.assertEqual((shot["width_px"], shot["height_px"]), (360, 180))
        self.assertEqual(self.bridge.revision(), before_revision)
        self.assertIs(QApplication.focusWidget(), before_focus)
        self.assertIs(QApplication.focusWidget(), field)
        self.assertEqual(field.text(), "still editing")
        self.assertEqual(finished, [])

    def test_missing_dialog_invalid_width_and_depth_are_clear_errors(self):
        with self.assertRaisesRegex(ValueError, "No open dialog"):
            self.bridge.dispatch("ui_screenshot", target="active_dialog")
        with self.assertRaisesRegex(ValueError, "max_width_px"):
            self.bridge.dispatch("ui_screenshot", max_width_px=0)
        with self.assertRaisesRegex(ValueError, "max_depth"):
            self.bridge.dispatch("ui_inspect", max_depth=17)

    def test_visible_menu_actions_include_registered_command_ids(self):
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Commands")
        menu = QMenu("Paths", dialog)
        action = menu.addAction("Weld")
        action.setData("path.weld")
        dialog.show()
        menu.popup(dialog.mapToGlobal(QPoint(20, 20)))
        self.app.processEvents()

        tree = self.bridge.dispatch("ui_inspect", target="active_dialog")
        actions = [node for node in _flatten(tree["root"]) if node["type"] == "QAction"]

        self.assertTrue(menu.isVisible())
        self.assertTrue(any(node["text"] == "Weld" and node.get("command_id") == "path.weld"
                            for node in actions))
        menu.close()

    def test_inspect_depth_and_node_limit_report_truncation(self):
        limited = self.bridge.dispatch("ui_inspect", target="window", max_depth=0)
        self.assertTrue(limited["truncated"])
        self.assertLessEqual(limited["node_count"], limited["max_nodes"])
        dialog = QDialog(self.window)
        for index in range(520):
            label = QLabel(str(index), dialog)
            label.setGeometry(0, 0, 1, 1)
        dialog.show()
        self.app.processEvents()
        crowded = self.bridge.dispatch("ui_inspect", target="active_dialog", max_depth=8)
        self.assertEqual(crowded["node_count"], crowded["max_nodes"])
        self.assertTrue(crowded["truncated"])

    def test_status_reports_tabs_warning_sending_and_dialog(self):
        self.window.sending = True
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Pack objects")
        dialog.show()
        self.app.processEvents()

        result = self.bridge.dispatch("ui_status")

        self.assertEqual(result["active_tab"], 0)
        self.assertEqual(len(result["tabs"]), 1)
        self.assertTrue(result["tabs"][0]["active"])
        self.assertEqual(result["status_text"], self.window.status_label.text())
        self.assertTrue(result["bounds_warning"])
        self.assertEqual(result["bounds_warning_text"], self.window.bounds_warning.text())
        self.assertTrue(result["sending"])
        self.assertEqual(result["active_dialog_title"], "Pack objects")


def _flatten(node):
    result = [node]
    for child in node["children"]:
        result.extend(_flatten(child))
    return result

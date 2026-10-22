"""Canvas context actions and constrained drags operate on the real editor state."""
import copy
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QSettings, Qt
from PySide6.QtGui import QContextMenuEvent, QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from verona.i18n import set_language, tr
from verona.ui import DesignCanvas, VeronaWindow, _item_path
from tests.test_editor_actions import image_item, rectangle


SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT_SHIFT = Qt.KeyboardModifier.AltModifier | SHIFT
NONE = Qt.KeyboardModifier.NoModifier


class CanvasInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, self.directory.name)
        set_language("en")
        self.window = VeronaWindow(host="")

    def tearDown(self):
        with patch.object(self.window, "_confirm_unsaved_changes", return_value="discard"):
            self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def install(self, items, selection=()):
        self.window.items = items
        self.window.undo_stack.clear()
        self.window.redo_stack.clear()
        self.window.select_many(selection)
        self.window.refresh_all()

    @staticmethod
    def action(menu, label):
        for action in menu.actions():
            if action.text() == tr(label):
                return action
            if action.menu():
                found = CanvasInteractionTests.action(action.menu(), label)
                if found is not None:
                    return found
        return None

    def context(self, world, callback, reason=QContextMenuEvent.Reason.Mouse):
        canvas = self.window.canvas
        local = QPointF(34 + world[0]*canvas.zoom, 34 + world[1]*canvas.zoom).toPoint()
        event = QContextMenuEvent(reason, local, canvas.mapToGlobal(local))
        build_menu = self.window.canvas_context_menu

        def popup():
            menu = build_menu()
            return SimpleNamespace(exec=lambda _point: callback(menu), deleteLater=menu.deleteLater)

        with patch.object(self.window, "canvas_context_menu", side_effect=popup):
            QApplication.sendEvent(canvas, event)
        self.assertTrue(event.isAccepted())

    def trigger(self, label, world=(40, 40)):
        self.context(world, lambda menu: self.action(menu, label).trigger())

    def mouse(self, kind, world, modifiers=NONE):
        canvas = self.window.canvas
        local = QPointF(34 + world[0]*canvas.zoom, 34 + world[1]*canvas.zoom)
        button = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseMove else Qt.MouseButton.LeftButton
        buttons = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
        event = QMouseEvent(kind, local, QPointF(canvas.mapToGlobal(local.toPoint())), button, buttons, modifiers)
        QApplication.sendEvent(canvas, event)

    def drag(self, start, end, modifiers):
        self.mouse(QMouseEvent.Type.MouseButtonPress, start, modifiers)
        self.mouse(QMouseEvent.Type.MouseMove, end, modifiers)
        self.mouse(QMouseEvent.Type.MouseButtonRelease, end, modifiers)

    def test_right_click_preserves_multi_selection_and_groups_ungroups_with_undo(self):
        self.install([rectangle("a", 20, 20, 40, 40), rectangle("b", 100, 20, 40, 40)], {0, 1})
        contours = copy.deepcopy([item["contours"] for item in self.window.items])

        def group(menu):
            self.assertEqual(self.window.selected_indices(), {0, 1})
            self.assertTrue(self.action(menu, "群組").isEnabled())
            self.assertFalse(self.action(menu, "解散群組").isEnabled())
            self.action(menu, "群組").trigger()

        self.context((40, 40), group)
        paths = [item["group_path"] for item in self.window.items]
        self.assertEqual(paths[0], paths[1])
        self.assertEqual([item["contours"] for item in self.window.items], contours)
        self.assertEqual(len(self.window.undo_stack), 1)
        self.trigger("解散群組")
        self.assertTrue(all("group_path" not in item for item in self.window.items))
        self.window.undo()
        self.assertEqual([item["group_path"] for item in self.window.items], paths)
        self.window.undo()
        self.assertTrue(all("group_path" not in item for item in self.window.items))
        self.window.redo()
        self.assertEqual([item["group_path"] for item in self.window.items], paths)

    def test_context_target_group_empty_space_and_keyboard_preserve_intended_selection(self):
        self.install([rectangle("a", 20, 20, 40, 40),
                      rectangle("b", 100, 20, 40, 40, group_path=["pair"]),
                      rectangle("c", 180, 20, 40, 40, group_path=["pair"])], {0})
        check = lambda menu: self.assertEqual(self.window.selected_indices(), {1, 2})
        self.context((120, 40), check)
        self.context((280, 280), check)
        self.context((40, 40), check, QContextMenuEvent.Reason.Keyboard)
        self.assertEqual(self.window.undo_stack, [])

    def test_locked_hit_can_be_unlocked_without_selecting_hidden_items_or_guides(self):
        self.install([rectangle("under", 20, 20, 40, 40),
                      rectangle("locked", 20, 20, 40, 40, locked=True),
                      rectangle("hidden", 20, 20, 40, 40, visible=False),
                      rectangle("guide", 20, 20, 40, 40, registration_guide=True)], {0})
        before = copy.deepcopy(self.window.items)

        def unlock(menu):
            self.assertEqual(self.window.selected_indices(), {1})
            for label in ("複製物件", "刪除物件", "群組", "排列順序", "對齊與均分", "變形"):
                self.assertFalse(self.action(menu, label).isEnabled(), label)
            self.action(menu, "解除鎖定").trigger()

        self.context((40, 40), unlock)
        self.assertNotIn("locked", self.window.items[1])
        self.assertEqual(self.window.items[0], before[0])
        self.window.undo()
        self.assertEqual(self.window.items, before)

    def test_empty_selection_select_all_duplicate_and_delete_use_existing_actions(self):
        self.install([rectangle("a", 20, 20, 40, 40)])

        def select_all(menu):
            for label in ("複製物件", "刪除物件", "群組", "解散群組", "對齊與均分", "變形"):
                self.assertFalse(self.action(menu, label).isEnabled(), label)
            self.assertIs(self.action(menu, "復原"), self.window.undo_action)
            self.action(menu, "全選物件").trigger()

        self.context((280, 280), select_all)
        self.assertEqual(self.window.selected_indices(), {0})
        self.trigger("複製物件")
        self.assertEqual(len(self.window.items), 2)
        self.assertEqual(self.window.selected_indices(), {1})
        self.context((280, 280), lambda menu: self.action(menu, "刪除物件").trigger())
        self.assertEqual(len(self.window.items), 1)
        self.window.undo()
        self.assertEqual(len(self.window.items), 2)

    def test_context_alignment_stacking_and_transform_change_geometry_and_undo(self):
        self.install([rectangle("a", 20, 20, 40, 40), rectangle("b", 100, 90, 30, 20),
                      rectangle("c", 180, 20, 40, 40)], {0, 1})
        before = copy.deepcopy(self.window.items)

        def align(menu):
            self.assertFalse(self.action(menu, "水平均分").isEnabled())
            self.action(menu, "靠左").trigger()

        self.context((40, 40), align)
        self.assertEqual(_item_path(self.window.items[1]).boundingRect().left(), 20)
        self.window.undo()
        self.assertEqual(self.window.items, before)
        self.window.select(0)
        self.trigger("上移")
        self.assertEqual([item["name"] for item in self.window.items], ["b", "a", "c"])
        self.assertEqual(self.window.selected_indices(), {1})
        self.window.undo()
        self.window.select(1)
        self.context((280, 280), lambda menu: self.action(menu, "旋轉 +90°").trigger())
        bounds = self.window.selection_bounds()
        self.assertAlmostEqual(bounds.width(), 20)
        self.assertAlmostEqual(bounds.height(), 30)
        self.window.undo()
        self.assertEqual(self.window.items, before)

    def test_context_visibility_and_locking_are_undoable(self):
        self.install([rectangle("a", 20, 20, 40, 40)], {0})
        before = copy.deepcopy(self.window.items)
        self.trigger("隱藏")
        self.assertFalse(self.window.items[0]["visible"])
        self.context((280, 280), lambda menu: self.action(menu, "顯示").trigger())
        self.assertNotIn("visible", self.window.items[0])
        self.trigger("鎖定")
        self.assertTrue(self.window.items[0]["locked"])
        self.trigger("解除鎖定")
        self.assertNotIn("locked", self.window.items[0])
        for _ in range(4):
            self.window.undo()
        self.assertEqual(self.window.items, before)

    def test_preview_canvas_does_not_open_an_edit_menu(self):
        preview = DesignCanvas(self.window, interactive=False)
        with patch.object(self.window, "canvas_context_menu") as factory:
            event = QContextMenuEvent(QContextMenuEvent.Reason.Keyboard, preview.rect().center(),
                                      preview.mapToGlobal(preview.rect().center()))
            QApplication.sendEvent(preview, event)
            factory.assert_not_called()
            self.assertFalse(event.isAccepted())
        preview.deleteLater()

    def test_native_popup_group_action_responds_to_mouse_click(self):
        self.install([rectangle("a", 20, 20, 40, 40), rectangle("b", 100, 20, 40, 40)], {0, 1})
        self.window.show()
        menu = self.window.canvas_context_menu()
        menu.popup(self.window.canvas.mapToGlobal(self.window.canvas.rect().center()))
        self.app.processEvents()
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(self.window.group_action).center())
        self.assertEqual(self.window.items[0]["group_path"], self.window.items[1]["group_path"])
        self.assertEqual(len(self.window.undo_stack), 1)
        self.assertFalse(menu.isVisible())
        menu.deleteLater()

    def test_shift_drag_locks_horizontal_or_vertical_without_deselecting(self):
        for end, delta in (((65, 47), (25, 0)), ((47, 65), (0, 25)), ((15, 33), (-25, 0))):
            with self.subTest(end=end):
                self.install([rectangle("a", 20, 20, 40, 40)], {0})
                before = copy.deepcopy(self.window.items)
                self.drag((40, 40), end, SHIFT)
                bounds = self.window.selection_bounds()
                self.assertAlmostEqual(bounds.left(), 20 + delta[0])
                self.assertAlmostEqual(bounds.top(), 20 + delta[1])
                self.assertEqual(self.window.selected_indices(), {0})
                self.assertEqual(len(self.window.undo_stack), 1)
                self.window.undo()
                self.assertEqual(self.window.items, before)
                self.window.redo()
                self.assertAlmostEqual(self.window.selection_bounds().left(), 20 + delta[0])

    def test_shift_click_toggles_a_group_without_moving_or_creating_undo(self):
        self.install([rectangle("a", 20, 20, 40, 40, group_path=["pair"]),
                      rectangle("b", 100, 20, 40, 40, group_path=["pair"])], {0})
        before = copy.deepcopy(self.window.items)
        for expected in (set(), {0, 1}):
            self.mouse(QMouseEvent.Type.MouseButtonPress, (40, 40), SHIFT)
            self.mouse(QMouseEvent.Type.MouseButtonRelease, (40, 40), SHIFT)
            self.assertEqual(self.window.selected_indices(), expected)
        self.assertEqual(self.window.items, before)
        self.assertEqual(self.window.undo_stack, [])

    def test_alt_shift_drag_copies_group_and_translates_image_and_clip_once(self):
        for end, delta in (((55, 32), (25, 0)), ((37, 50), (0, 25))):
            with self.subTest(end=end):
                self.install([image_item(group_path=["pair"]),
                              rectangle("vector", 100, 20, 30, 20, group_path=["pair"])], {0})
                before = copy.deepcopy(self.window.items)
                self.mouse(QMouseEvent.Type.MouseButtonPress, (30, 25), ALT_SHIFT)
                self.mouse(QMouseEvent.Type.MouseMove, end, ALT_SHIFT)
                self.mouse(QMouseEvent.Type.MouseMove, end, ALT_SHIFT)
                self.mouse(QMouseEvent.Type.MouseButtonRelease, end, ALT_SHIFT)
                self.assertEqual(len(self.window.items), 4)
                self.assertEqual(self.window.items[:2], before)
                self.assertEqual(self.window.selected_indices(), {2, 3})
                copied = self.window.items[2]
                self.assertEqual(copied["group_path"], self.window.items[3]["group_path"])
                self.assertNotEqual(copied["group_path"], before[0]["group_path"])
                self.assertEqual(copied["image_data"], before[0]["image_data"])
                for key in ("contours", "image_clip"):
                    expected = [[(x+delta[0], y+delta[1]) for x, y in contour] for contour in before[0][key]]
                    self.assertEqual(copied[key], expected)
                self.assertEqual(copied["image_transform"][:2], [20+delta[0], 20+delta[1]])
                self.assertEqual(len(self.window.undo_stack), 1)
                self.window.undo()
                self.assertEqual(self.window.items, before)

    def test_shift_can_change_during_drag_and_return_to_origin_without_an_edit(self):
        self.install([rectangle("a", 20, 20, 40, 40)], {0})
        before = copy.deepcopy(self.window.items)
        self.mouse(QMouseEvent.Type.MouseButtonPress, (40, 40))
        self.mouse(QMouseEvent.Type.MouseMove, (65, 47))
        self.assertAlmostEqual(self.window.selection_bounds().top(), 27)
        self.mouse(QMouseEvent.Type.MouseMove, (65, 47), SHIFT)
        self.assertAlmostEqual(self.window.selection_bounds().top(), 20)
        self.mouse(QMouseEvent.Type.MouseMove, (60, 60))
        self.assertAlmostEqual(self.window.selection_bounds().top(), 40)
        self.mouse(QMouseEvent.Type.MouseMove, (40, 40), SHIFT)
        self.mouse(QMouseEvent.Type.MouseButtonRelease, (40, 40), SHIFT)
        self.assertEqual(self.window.items, before)
        self.assertEqual(self.window.undo_stack, [])

    def test_alt_shift_click_creates_no_copy_and_escape_cancels_a_copy_drag(self):
        self.install([rectangle("a", 20, 20, 40, 40)], {0})
        before = copy.deepcopy(self.window.items)
        self.mouse(QMouseEvent.Type.MouseButtonPress, (40, 40), ALT_SHIFT)
        self.mouse(QMouseEvent.Type.MouseButtonRelease, (40, 40), ALT_SHIFT)
        self.assertEqual(self.window.items, before)
        self.mouse(QMouseEvent.Type.MouseButtonPress, (40, 40), ALT_SHIFT)
        self.mouse(QMouseEvent.Type.MouseMove, (65, 47), ALT_SHIFT)
        self.assertEqual(len(self.window.items), 2)
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, NONE)
        QApplication.sendEvent(self.window.canvas, event)
        self.mouse(QMouseEvent.Type.MouseButtonRelease, (65, 47), ALT_SHIFT)
        self.assertEqual(self.window.items, before)
        self.assertEqual(self.window.selected_indices(), {0})
        self.assertEqual(self.window.undo_stack, [])


if __name__ == "__main__":
    unittest.main()

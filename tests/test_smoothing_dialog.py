import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QWidget

from verona.smoothing_dialog import ContourSmoothingDialog


class FakeOwner(QWidget):
    def __init__(self):
        super().__init__()
        self.items = [
            {"name": "A", "cut": True, "contours": [[(i * .1, (i % 2) * .02) for i in range(40)]]},
            {"name": "B", "cut": True, "contours": [[(0, 0), (1, .02), (2, 0)]]},
        ]


class SmoothingDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.owner = FakeOwner()
        self.original = copy.deepcopy(self.owner.items)
        self.dialog = ContourSmoothingDialog(self.owner, [0, 1])
        self.addCleanup(self.dialog.close)

    def test_controls_default_range_and_multi_item_selector(self):
        spin = self.dialog.tolerance_spin
        self.assertEqual(spin.value(), .1)
        self.assertEqual((spin.minimum(), spin.maximum(), spin.decimals()), (0, .5, 3))
        self.assertEqual(spin.singleStep(), .02)
        self.assertFalse(self.dialog.object_combo.isHidden())
        self.assertEqual(self.dialog.object_combo.count(), 2)

    def test_parameter_change_uses_open_time_original_not_prior_preview(self):
        self.dialog.tolerance_spin.setValue(.2)
        at_high = self.dialog.build_contours()
        self.dialog.tolerance_spin.setValue(.04)
        self.dialog.tolerance_spin.setValue(.2)
        self.assertEqual(self.dialog.build_contours(), at_high)
        self.assertEqual(self.owner.items, self.original)

    def test_preview_zoom_does_not_change_build_result(self):
        self.dialog.show()
        self.app.processEvents()
        before = self.dialog.build_contours()
        canvas = self.dialog.canvas
        cursor = QPointF(canvas.width() * .4, canvas.height() * .45)
        event = QWheelEvent(cursor, canvas.mapToGlobal(cursor.toPoint()), QPoint(),
                            QPoint(0, 120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(canvas, event)
        self.assertEqual(self.dialog.build_contours(), before)
        self.assertEqual(self.owner.items, self.original)

    def test_batch_build_returns_all_items_without_mutation_and_cancel_is_detached(self):
        result = self.dialog.build_contours()
        self.assertEqual(set(result), {0, 1})
        self.assertIsNot(result[0], self.owner.items[0]["contours"])
        self.dialog.reject()
        self.assertEqual(self.owner.items, self.original)

    def test_single_item_hides_combo_and_preview_switch_changes_only_view_target(self):
        self.dialog.close()
        one = ContourSmoothingDialog(self.owner, [1])
        self.addCleanup(one.close)
        self.assertFalse(one.object_combo.isVisible())
        self.assertEqual(set(one.build_contours()), {1})
        self.assertEqual(one.preview_index, 1)


if __name__ == "__main__":
    unittest.main()

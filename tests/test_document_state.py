"""Document state and history work without constructing editor widgets."""

import unittest

from verona.document_state import DocumentState


class DocumentStateTests(unittest.TestCase):
    def test_documents_and_returned_settings_do_not_share_mutable_state(self):
        first, second = DocumentState(), DocumentState()
        first.settings["speed"] = 11
        first.items.append({"name": "line", "contours": [[(1., 2.), (3., 4.)]]})
        first.metadata["nested"] = {"values": [1]}
        first.selection.add(0)
        state = first.state()
        state["settings"]["speed"] = 2
        state["items"][0]["contours"][0].append((5., 6.))
        state["metadata"]["nested"]["values"].append(2)
        self.assertEqual(first.settings["speed"], 11)
        self.assertEqual(len(first.items[0]["contours"][0]), 2)
        self.assertEqual(first.metadata["nested"]["values"], [1])
        self.assertEqual(second.settings["speed"], 5)
        self.assertEqual((second.items, second.metadata, second.selection), ([], {}, set()))

    def test_history_restores_settings_selection_and_metadata_and_invalidates_redo(self):
        document = DocumentState()
        document.items = [{"name": "line", "contours": [[(1., 2.), (3., 4.)]]}]
        document.selected, document.selection = 0, {0}
        document.metadata = {"nested": {"values": [1]}}
        before = document.snapshot()
        document.items[0]["name"] = "edited"
        document.settings["passes"] = 2
        document.metadata["nested"]["values"].append(2)
        document.push_undo(before)
        self.assertTrue(document.undo())
        self.assertEqual(document.snapshot(), before)
        self.assertTrue(document.redo())
        self.assertEqual(document.items[0]["name"], "edited")
        self.assertEqual(document.settings["passes"], 2)
        self.assertTrue(document.undo())
        document.push_undo(document.snapshot())
        self.assertFalse(document.redo())
        document.items[0]["name"] = "new edit"
        self.assertEqual(before["items"][0]["name"], "line")

    def test_saved_baseline_ignores_selection_and_view_but_tracks_content_and_settings(self):
        document = DocumentState()
        document.items = [{"name": "line", "contours": [[(1., 2.), (3., 4.)]]}]
        document.mark_saved("saved.jdesign")
        document.selected, document.selection = 0, {0}
        document.view["zoom"] = 3.6
        self.assertFalse(document.is_dirty())
        document.settings["force"] = 80
        self.assertTrue(document.is_dirty())
        document.mark_saved("saved.jdesign")
        document.items[0]["contours"][0].append((5., 6.))
        self.assertTrue(document.is_dirty())
        self.assertEqual(len(document.saved_snapshot["items"][0]["contours"][0]), 2)

    def test_install_resets_history_and_detaches_metadata_and_settings(self):
        document = DocumentState()
        document.push_undo(document.snapshot())
        document.selected, document.selection = 0, {0}
        settings = {**document.settings, "tool": 1, "speed": 9}
        metadata = {"nested": {"values": [1]}}
        document.install("opened.jdesign", [], settings, metadata)
        settings["speed"] = 2
        metadata["nested"]["values"].append(2)
        self.assertEqual(document.settings["speed"], 9)
        self.assertEqual(document.metadata["nested"]["values"], [1])
        self.assertEqual((document.selected, document.selection), (-1, set()))
        self.assertEqual((document.undo_stack, document.redo_stack), ([], []))
        self.assertFalse(document.is_dirty())


if __name__ == "__main__":
    unittest.main()

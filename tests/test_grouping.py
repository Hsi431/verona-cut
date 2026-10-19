import copy
import unittest
from uuid import UUID

from verona.grouping import (
    expand_selection,
    group_items,
    remap_group_ids,
    ungroup_items,
)


class GroupingTests(unittest.TestCase):
    def test_selection_expands_to_outer_group_and_excludes_registration_guides(self):
        items = [
            {"group_path": ["outer", "inner"]},
            {"group_path": ["outer"]},
            {"group_path": ["other"], "registration_guide": True},
            {},
        ]
        self.assertEqual(expand_selection(items, {0, 2, 3, 99}), {0, 1, 3})

    def test_grouping_preserves_data_and_existing_nested_groups(self):
        items = [
            {"name": "nested", "group_path": ["old-outer", "inner"],
             "contours": [[(1, 2), (3, 4)]], "image_data": "abc", "cut": True},
            {"name": "member", "group_path": ["old-outer"], "contours": [[(5, 6)]]},
            {"name": "free", "contours": [[(7, 8)]]},
            {"name": "guide", "registration_guide": True, "contours": []},
        ]
        before_geometry = [copy.deepcopy(item.get("contours")) for item in items]
        self.assertTrue(group_items(items, {0, 2, 3}))
        new_outer = items[0]["group_path"][0]
        UUID(new_outer)
        self.assertEqual(items[0]["group_path"], [new_outer, "old-outer", "inner"])
        self.assertEqual(items[1]["group_path"], [new_outer, "old-outer"])
        self.assertEqual(items[2]["group_path"], [new_outer])
        self.assertNotIn("group_path", items[3])
        self.assertEqual([item.get("contours") for item in items], before_geometry)
        self.assertEqual(items[0]["image_data"], "abc")
        self.assertTrue(items[0]["cut"])

    def test_repeated_grouping_of_same_outer_group_is_noop(self):
        items = [{"group_path": ["outer", "inner"]}, {"group_path": ["outer"]}]
        self.assertFalse(group_items(items, {0}))
        self.assertEqual([item["group_path"] for item in items],
                         [["outer", "inner"], ["outer"]])
        self.assertFalse(group_items(items, {0, 1}))

    def test_ungroup_removes_only_outer_layer_and_keeps_inner_groups(self):
        items = [
            {"group_path": ["outer", "inner", "deep"]},
            {"group_path": ["outer", "inner"]},
            {"group_path": ["other"]},
            {"registration_guide": True, "group_path": ["outer"]},
            {},
        ]
        self.assertTrue(ungroup_items(items, {0}))
        self.assertEqual(items[0]["group_path"], ["inner", "deep"])
        self.assertEqual(items[1]["group_path"], ["inner"])
        self.assertEqual(items[2]["group_path"], ["other"])
        self.assertEqual(items[3]["group_path"], ["outer"])
        self.assertFalse(ungroup_items(items, {3, 4}))
        self.assertEqual(items[2]["group_path"], ["other"])

    def test_remap_changes_ids_and_preserves_relations_within_copy(self):
        items = [{"group_path": ["outer", "inner"]}, {"group_path": ["outer", "inner"]},
                 {"group_path": ["outer"]}, {}]
        original = copy.deepcopy(items)
        remap_group_ids(items)
        self.assertNotEqual(items[0]["group_path"][0], "outer")
        self.assertNotEqual(items[0]["group_path"][1], "inner")
        self.assertEqual(items[0]["group_path"], items[1]["group_path"])
        self.assertEqual(items[0]["group_path"][0], items[2]["group_path"][0])
        self.assertEqual(items[2]["group_path"], [items[0]["group_path"][0]])
        self.assertEqual(original[0]["group_path"], ["outer", "inner"])
        self.assertNotIn("group_path", items[3])

    def test_grouping_requires_two_editable_items(self):
        items = [{}, {"registration_guide": True}]
        self.assertFalse(group_items(items, {0, 1}))
        self.assertFalse(group_items(items, {0}))


if __name__ == "__main__":
    unittest.main()

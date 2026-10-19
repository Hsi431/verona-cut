"""Pure helpers for grouping flat design items through metadata paths."""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping, Sequence
from uuid import uuid4


Item = MutableMapping[str, object]


def _editable_indices(items: Sequence[Item], indices: Iterable[int]) -> set[int]:
    return {index for index in indices
            if 0 <= index < len(items) and not items[index].get("registration_guide")}


def expand_selection(items: Sequence[Item], indices: Iterable[int]) -> set[int]:
    """Expand selected members to every editable item in their outer groups."""
    selected = _editable_indices(items, indices)
    outer_ids = {
        path[0]
        for index in selected
        if isinstance((path := items[index].get("group_path")), list) and path
    }
    if not outer_ids:
        return selected
    selected.update(
        index for index, item in enumerate(items)
        if not item.get("registration_guide")
        and isinstance((path := item.get("group_path")), list)
        and path and path[0] in outer_ids
    )
    return selected


def selection_units(items: Sequence[Item], indices: Iterable[int]) -> list[list[int]]:
    """Return stable outer-group / standalone units for alignment and packing."""
    units = {}
    for index in sorted(expand_selection(items, indices)):
        path = items[index].get("group_path", [])
        key = ("group", path[0]) if path else ("item", index)
        units.setdefault(key, []).append(index)
    return list(units.values())


def group_items(items: Sequence[Item], indices: Iterable[int]) -> bool:
    """Wrap selected editable items in a new outer group, preserving inner paths."""
    selected = expand_selection(items, indices)
    if len(selected) < 2:
        return False

    outer_ids = {
        path[0]
        for index in selected
        if isinstance((path := items[index].get("group_path")), list) and path
    }
    if len(outer_ids) == 1 and all(
        isinstance(items[index].get("group_path"), list)
        and items[index]["group_path"]
        and items[index]["group_path"][0] in outer_ids
        for index in selected
    ):
        return False

    group_id = str(uuid4())
    for index in selected:
        item = items[index]
        path = item.get("group_path", [])
        item["group_path"] = [group_id, *path] if isinstance(path, list) else [group_id]
    return True


def ungroup_items(items: Sequence[Item], indices: Iterable[int]) -> bool:
    """Remove the outer group layer for each selected group."""
    selected = expand_selection(items, indices)
    outer_ids = {
        path[0]
        for index in selected
        if isinstance((path := items[index].get("group_path")), list) and path
    }
    if not outer_ids:
        return False

    changed = False
    for item in items:
        path = item.get("group_path")
        if (not item.get("registration_guide") and isinstance(path, list)
                and path and path[0] in outer_ids):
            item["group_path"] = path[1:]
            if not item["group_path"]:
                del item["group_path"]
            changed = True
    return changed


def remap_group_ids(items: Sequence[Item]) -> None:
    """Give copied items fresh group IDs while keeping their shared paths intact."""
    remapped: dict[str, str] = {}
    for item in items:
        path = item.get("group_path")
        if isinstance(path, list):
            item["group_path"] = [remapped.setdefault(group_id, str(uuid4()))
                                  for group_id in path]

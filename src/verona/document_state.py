"""Document contents, cut settings and edit history, independent of widgets."""

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

from .geometry import copy_items, copy_snapshot
from .transport import TCP_PORT


def default_cut_settings() -> dict[str, Any]:
    return {"custom": False, "speed": 5, "force": 10, "tool": 0, "passes": 1,
            "connection": "wifi", "host": "", "port": TCP_PORT}


@dataclass
class DocumentState:
    settings: dict[str, Any] = field(default_factory=default_cut_settings)
    items: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    current_file: str | None = None
    selected: int = -1
    selection: set[int] = field(default_factory=set)
    undo_stack: list[dict[str, Any]] = field(default_factory=list)
    redo_stack: list[dict[str, Any]] = field(default_factory=list)
    saved_snapshot: dict[str, Any] = field(init=False)
    view: dict[str, Any] = field(default_factory=dict)
    recovery_id: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.settings = dict(self.settings)
        self.saved_snapshot = self.state()

    def state(self) -> dict[str, Any]:
        return {"items": copy_items(self.items), "settings": dict(self.settings),
                "metadata": copy.deepcopy(self.metadata)}

    def is_dirty(self) -> bool:
        return {"items": self.items, "settings": self.settings,
                "metadata": self.metadata} != self.saved_snapshot

    def selected_indices(self) -> set[int]:
        if self.selected not in self.selection:
            return {self.selected} if 0 <= self.selected < len(self.items) else set()
        return {i for i in self.selection if 0 <= i < len(self.items)}

    def snapshot(self) -> dict[str, Any]:
        return {**self.state(), "selected": self.selected,
                "selection": sorted(self.selected_indices()), "file": self.current_file}

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.items = copy_items(snapshot["items"])
        self.selected = snapshot["selected"]
        self.selection = set(snapshot.get("selection", [self.selected] if self.selected >= 0 else []))
        self.settings = dict(snapshot["settings"])
        self.current_file = snapshot["file"]
        self.metadata = copy.deepcopy(snapshot.get("metadata", {}))

    def push_undo(self, previous: dict[str, Any]) -> None:
        self.undo_stack.append(copy_snapshot(previous))
        self.redo_stack.clear()

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        self.redo_stack.append(self.snapshot())
        self.restore(self.undo_stack.pop())
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        self.undo_stack.append(self.snapshot())
        self.restore(self.redo_stack.pop())
        return True

    def install(self, filename, items, settings, metadata) -> None:
        self.items = items
        self.settings = dict(settings)
        self.metadata = copy.deepcopy(metadata)
        self.selected, self.selection = -1, set()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.mark_saved(filename)

    def mark_saved(self, filename) -> None:
        self.current_file = filename
        self.saved_snapshot = self.state()

    def design(self) -> dict[str, Any]:
        data = {"format": "verona", "version": 1, "units": "mm",
                "items": self.items, "settings": dict(self.settings)}
        if self.metadata:
            data["metadata"] = self.metadata
        return data

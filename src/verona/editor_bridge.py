"""GUI-thread editing endpoint. Local, same-user access; no machine commands."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

import numpy as np
from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer

from .artwork import bounds_list, item_bounds
from .commands import registration_point_error
from .editor_actions import cutting_frame, editable_indices, render_preview
from .editor_document import build_document_edit
from .editor_client import editor_directory
from .layers import cuttable, printable


class EditorBridge(QObject):
    def __init__(self, window, *, directory=None):
        super().__init__(window)
        self.window = window
        self.directory = Path(directory) if directory else editor_directory(create=True)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.editor_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.socket_path = self.directory / f"{self.editor_id}.sock"
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(str(self.socket_path)):
            raise RuntimeError(self.server.errorString())
        self.connections = {}
        self.plans = {}
        self._digests = {}
        self.server.newConnection.connect(self._accept)

    def close(self):
        for connection in list(self.connections):
            connection.abort()
        self.server.close()
        for path in self.directory.glob(f"{self.editor_id}.*"):
            path.unlink(missing_ok=True)
        self.plans.clear()

    def _accept(self):
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            self.connections[connection] = bytearray()
            timer = QTimer(connection)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda c=connection: c.abort())
            timer.start(5000)
            connection.readyRead.connect(lambda c=connection, t=timer: self._read(c, t))
            connection.disconnected.connect(lambda c=connection: self._disconnected(c))

    def _disconnected(self, connection):
        self.connections.pop(connection, None)
        connection.deleteLater()

    def _read(self, connection, timer):
        buffer = self.connections.get(connection)
        if buffer is None:
            return
        buffer.extend(bytes(connection.readAll()))
        if len(buffer) <= 65536 and b"\n" not in buffer:
            return
        timer.stop()
        try:
            if len(buffer) > 65536:
                raise ValueError("The editor request exceeds 64 KiB.")
            message = json.loads(buffer)
            if not isinstance(message, dict) or not isinstance(message.get("arguments", {}), dict):
                raise ValueError("Expected an editor command and arguments object.")
            result = self.dispatch(message.get("command"), **message.get("arguments", {}))
            response = {"ok": True, "result": result}
        except (ValueError, TypeError, KeyError, RuntimeError, OSError) as error:
            response = {"ok": False, "error": str(error)}
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(encoded) > 8 * 1024 * 1024:
            encoded = b'{"ok":false,"error":"Response too large; request fewer objects."}\n'
        connection.write(encoded)
        connection.disconnectFromServer()

    def revision(self):
        window = self.window
        self._seen_digests = {}
        document = {"items": [self._digested(item) for item in window.items],
                    "settings": window._cut_settings(), "metadata": self._digested(window.metadata)}
        state = {"editor_id": self.editor_id, "document": document, "file": window.current_file,
                 "tab": window._active_document, "selection": sorted(window.selected_indices()),
                 "undo": len(window.undo_stack), "redo": len(window.redo_stack)}
        self._digests = self._seen_digests
        return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False, allow_nan=False).encode()).hexdigest()

    def _digested(self, mapping):
        """Shallow copy with embedded images, masks and point lists replaced by their digests."""
        result = {}
        for key, value in mapping.items():
            if isinstance(value, str) and len(value) > 4096:
                value = self._digest(value)
            elif key in ("contours", "image_clip") and isinstance(value, list):
                value = self._geometry_digest(value)
            result[key] = value
        return result

    @staticmethod
    def _geometry_digest(contours):
        # Hashing packed doubles is several times faster than JSON for large
        # traced documents and still changes with any coordinate.
        parts = []
        for contour in contours:
            points = np.asarray(contour, dtype=np.float64)
            parts += (len(points).to_bytes(8, "little"), points.tobytes())
        return "sha256:" + hashlib.sha256(b"".join(parts)).hexdigest()

    def _digest(self, text):
        # Strings are immutable, so a digest stays valid while the same object is alive.
        cached = self._digests.get(id(text))
        if cached is None or cached[0] is not text:
            cached = text, "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        self._seen_digests[id(text)] = cached
        return cached[1]

    def _guard(self, expected_revision):
        from PySide6.QtWidgets import QApplication
        window = self.window
        if (window.sending or window.canvas.drag_before is not None or window.canvas.marquee_start is not None
                or QApplication.activeModalWidget() is not None):
            raise ValueError("Sending, a manual canvas gesture or a modal dialog is in progress.")
        if not isinstance(expected_revision, str) or expected_revision != self.revision():
            raise ValueError("The editor changed. Read its current state and review a new plan.")

    def _objects(self, items, indices=None, include_geometry=False, metadata=None):
        if type(include_geometry) is not bool:
            raise ValueError("include_geometry must be a boolean.")
        if indices is None:
            indices = list(range(len(items)))
        if (not isinstance(indices, list)
                or any(type(i) is not int or not 0 <= i < len(items) for i in indices)):
            raise ValueError("Invalid object indices.")
        registration = (self.window.metadata if metadata is None else metadata).get("print_cut", {})
        objects = []
        for index in indices:
            item = items[index]
            bounds = item_bounds(item)
            error = None
            if cuttable(item) and registration.get("enabled") and registration.get("bounds"):
                for point in (bounds.topLeft(), bounds.bottomRight()):
                    error = registration_point_error(point.x(), point.y(), registration["bounds"],
                                                     registration.get("offset", (0, 0)))
                    if error:
                        break
            elif cuttable(item) and not registration.get("enabled"):
                if (bounds.left() < -1e-7 or bounds.top() < -1e-7
                        or bounds.right() > 300+1e-7 or bounds.bottom() > 300+1e-7):
                    error = "路徑超出 300 × 300 mm 工作區"
            data = {"index": index, "name": item["name"], "cut": item.get("cut", True),
                    "visible": item.get("visible", True), "locked": item.get("locked", False),
                    "print": bool(printable({**item, "visible": True})),
                    "image": "image_data" in item, "registration_guide": item.get("registration_guide", False),
                    "group_path": item.get("group_path", []),
                    "bounds_mm": bounds_list(bounds),
                    "print_bounds_mm": bounds_list(item_bounds(item, printed=True)) if printable(item) else None,
                    "print_style": item.get("print_style"),
                    "contours": len(item["contours"]),
                    "points": sum(map(len, item["contours"])), "bounds_error": error,
                    "bleed": item.get("bleed"), "source_color": item.get("source_color"),
                    "trace_options": item.get("trace_options")}
            if include_geometry:
                data["cut_geometry_mm"] = item["contours"]
                data["image_clip_mm"] = item.get("image_clip", [])
                data["node_paths_mm"] = item.get("node_paths", {})
            objects.append(data)
        return objects

    def state(self, object_indices=None, include_geometry=False):
        window = self.window
        try:
            frame, frame_error = bounds_list(cutting_frame(window.metadata)), None
        except ValueError as error:
            frame, frame_error = None, str(error)
        return {"editor_id": self.editor_id, "revision": self.revision(),
                "file": window.current_file, "dirty": window.is_dirty(),
                "active_tab": window._active_document,
                "tabs": [{"index": i, "file": window.current_file if i == window._active_document else tab["file"]}
                         for i, tab in enumerate(window._documents)],
                "selection": sorted(window.selected_indices()), "units": "mm",
                "metadata": {key: value for key, value in window.metadata.items()
                             if key != "source_preview"},
                "effective_cutting_frame_mm": frame, "frame_error": frame_error,
                "settings": window._cut_settings(), "sending": window.sending,
                "undo_steps": len(window.undo_stack), "redo_steps": len(window.redo_stack),
                "objects": self._objects(window.items, object_indices, include_geometry),
                "edit_actions": ["transform", "duplicate", "delete", "set_cut", "group",
                                 "ungroup", "align", "link_cut", "add_shape", "add_text", "configure", "import_artwork",
                                 "retrace_image", "edit_path", "vector_print", "fill_area", "pack_objects", "path_order", "bleed"],
                "hardware_operations": False}

    def summary(self):
        """Discovery fields only; no per-object bounds or geometry."""
        window = self.window
        return {"editor_id": self.editor_id, "file": window.current_file, "dirty": window.is_dirty(),
                "active_tab": window._active_document, "revision": self.revision()}

    def preview(self, expected_revision, action, object_indices, parameters):
        self._guard(expected_revision)
        window = self.window
        if (not isinstance(object_indices, list)
                or any(type(i) is not int or not 0 <= i < len(window.items) for i in object_indices)):
            raise ValueError("Invalid object indices.")
        selection = sorted(window.selected_indices()) if action in ("configure", "path_order") else object_indices
        items, metadata, settings, selection, details = build_document_edit(
            window.items, window.metadata, window._cut_settings(), selection, action, parameters)
        document = {"format": "verona", "version": 1, "units": "mm",
                    "items": items, "metadata": metadata, "settings": settings}
        items, settings = window._validate_document(document, trusted_items=window.items)
        plan_id = uuid.uuid4().hex
        filename = self.directory / f"{self.editor_id}.{plan_id}.png"
        render_preview(items, metadata, filename)
        while len(self.plans) >= 4:
            self._discard_plan(next(iter(self.plans)))
        self.plans[plan_id] = {"revision": expected_revision, "items": items,
                              "selection": selection, "filename": filename,
                              "metadata": metadata, "settings": settings}
        return {"plan_id": plan_id, "revision": expected_revision, "details": details,
                "before_objects": len(window.items), "after_objects": len(items),
                "selection_after": selection, "objects": self._objects(items, metadata=metadata),
                "metadata_after": {k: v for k, v in metadata.items() if k != "source_preview"},
                "settings_after": settings,
                "preview_path": str(filename), "applied": False, "hardware_operations": False}

    def _discard_plan(self, plan_id):
        plan = self.plans.pop(plan_id)
        plan["filename"].unlink(missing_ok=True)
        if plan.get("toolpath"):
            plan["toolpath"].unlink(missing_ok=True)

    def apply(self, expected_revision, plan_id):
        self._guard(expected_revision)
        plan = self.plans.get(plan_id)
        if plan is None or plan["revision"] != expected_revision:
            raise ValueError("Unknown, expired or stale preview plan.")
        window = self.window
        before = window._snapshot()
        window.items = plan["items"]
        window.metadata = plan["metadata"]
        window._set_cut_settings(plan["settings"])
        window.selected, window.selection = -1, set()
        window.select_many(plan["selection"])
        window.push_undo(before)
        window.refresh_all()
        self._discard_plan(plan_id)
        return self.state()

    def select(self, expected_revision, object_indices):
        self._guard(expected_revision)
        indices = editable_indices(self.window.items, object_indices) if object_indices else []
        self.window.select_many(indices)
        return self.state()

    def history(self, expected_revision, redo=False):
        self._guard(expected_revision)
        if type(redo) is not bool:
            raise ValueError("redo must be a boolean.")
        if not (self.window.redo_stack if redo else self.window.undo_stack):
            raise ValueError("There is no applicable history step.")
        (self.window.redo if redo else self.window.undo)()
        return self.state()

    def image(self, plan_id=None, view="artwork"):
        if view not in ("artwork", "toolpath"):
            raise ValueError("Choose artwork or toolpath view.")
        if view == "toolpath":
            from .commands import encode_document
            from .ui import PlotPreviewCanvas
            plan = self.plans.get(plan_id) if plan_id is not None else None
            if plan_id is not None and plan is None:
                raise ValueError("Unknown or expired preview plan.")
            window = self.window
            payload = encode_document(plan["items"] if plan else window.items,
                plan["metadata"] if plan else window.metadata,
                plan["settings"] if plan else window._cut_settings())
            path = self.directory / f"{self.editor_id}.{plan_id or 'current'}.toolpath.png"
            canvas = PlotPreviewCanvas(payload)
            try:
                if not canvas.grab().save(str(path)):
                    raise OSError("Could not save the toolpath preview.")
            finally:
                canvas.deleteLater()
            if plan:
                plan["toolpath"] = path
            return {"path": str(path), "revision": plan["revision"] if plan else self.revision(),
                    "view": "toolpath", "hardware_operations": False}
        if plan_id is not None:
            plan = self.plans.get(plan_id)
            if plan is None:
                raise ValueError("Unknown or expired preview plan.")
            return {"path": str(plan["filename"]), "revision": plan["revision"]}
        filename = self.directory / f"{self.editor_id}.current.png"
        render_preview(self.window.items, self.window.metadata, filename)
        return {"path": str(filename), "revision": self.revision()}

    def open_document(self, expected_revision, path):
        self._guard(expected_revision)
        source = Path(path).expanduser().resolve()
        if source.suffix.lower() not in (".jdesign", ".json", ".lds"):
            raise ValueError("Open a .jdesign, .json or .lds design.")
        if source.suffix.lower() == ".lds" and source.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("Agent-opened LDS files are limited to 64 MiB.")
        document = self.window._read_document(str(source))
        # Parse first; preserve every existing tab and unsaved change.
        self.window.new_design()
        self.window._install_document(str(source), *document)
        return self.state()

    def save_document(self, expected_revision, path, overwrite=False):
        self._guard(expected_revision)
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be a boolean.")
        destination = Path(path).expanduser().resolve()
        if destination.suffix.lower() not in (".jdesign", ".json"):
            raise ValueError("Automation saves .jdesign or .json; use the GUI to save LDS files.")
        self.window._write_document(str(destination), overwrite=overwrite)
        return self.state()

    def export_pdf(self, expected_revision, path, overwrite=False):
        from .printing import write_print_pdf
        self._guard(expected_revision)
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be a boolean.")
        destination = Path(path).expanduser().resolve()
        if destination.suffix.lower() != ".pdf":
            raise ValueError("Choose a .pdf output path.")
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".pdf", delete=False) as output:
            temporary = Path(output.name)
        try:
            write_print_pdf(temporary, self.window.items, self.window.metadata,
                            calibration=self.window.metadata.get("source_format") == "calibration")
            if overwrite:
                os.replace(temporary, destination)
            else:
                from .ui import publish_new_file
                publish_new_file(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": str(destination), "revision": self.revision(),
                "printed": False, "hardware_operations": False}

    def dispatch(self, command, **arguments):
        handlers = {"state": self.state, "summary": self.summary, "preview": self.preview, "apply": self.apply,
                    "select": self.select, "history": self.history, "image": self.image,
                    "open_document": self.open_document, "save_document": self.save_document,
                    "export_pdf": self.export_pdf, "printers": self.printers}
        if command not in handlers:
            raise ValueError("Unsupported editor command; no device operations are available here.")
        return handlers[command](**arguments)

    def printers(self, paper_width_mm=210, paper_height_mm=297):
        from .editor_actions import finite_number
        from .printing import printer_profiles
        width = finite_number(paper_width_mm, "paper_width_mm", 1, 300)
        height = finite_number(paper_height_mm, "paper_height_mm", 1, 300)
        return {"printers": printer_profiles(width, height), "paper_width_mm": width,
                "paper_height_mm": height, "printed": False}

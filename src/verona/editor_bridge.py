"""GUI-thread editing endpoint. Local, same-user access; no machine commands."""
import hashlib
import json
import os
import copy
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


PRINT_PREVIEW_DPI = 150


def _make_print_printer(printer_name):
    from PySide6.QtPrintSupport import QPrinter, QPrinterInfo
    info = next((candidate for candidate in QPrinterInfo.availablePrinters()
                 if candidate.printerName() == printer_name), None)
    if info is None:
        raise ValueError("The selected print queue is not available.")
    printer = QPrinter(info, QPrinter.PrinterMode.HighResolution)
    if not printer.isValid():
        raise ValueError("The selected print queue is not valid.")
    return printer


class EditorBridge(QObject):
    def __init__(self, window, *, directory=None, listen=True):
        super().__init__(window)
        self.window = window
        self.directory = Path(directory) if directory else editor_directory(create=True)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.editor_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self.socket_path = self.directory / f"{self.editor_id}.sock"
        self.server = QLocalServer(self) if listen else None
        if self.server is not None:
            self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            if not self.server.listen(str(self.socket_path)):
                raise RuntimeError(self.server.errorString())
        self.connections = {}
        self.plans = {}
        self.print_jobs = {}
        self._digests = {}
        if self.server is not None:
            self.server.newConnection.connect(self._accept)

    def close(self):
        for connection in list(self.connections):
            connection.abort()
        if self.server is not None:
            self.server.close()
        for path in self.directory.glob(f"{self.editor_id}.*"):
            path.unlink(missing_ok=True)
        self.plans.clear()
        self.print_jobs.clear()

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
                "tabs": [{"index": i, "file": tab.current_file}
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
        details, new_pages = self._split_new_pages(details, metadata, settings, window.items)
        items, metadata, settings = self._validate_detached(items, metadata, settings, window.items)
        return self._make_plan(expected_revision, items, metadata, settings, selection, details, new_pages)

    def _split_new_pages(self, details, metadata, settings, trusted_items):
        """Validate overflow pages a packing edit wants to open as new tabs."""
        details = dict(details)
        validated_pages = []
        for page in details.pop("new_pages", []):
            page_document = {"format": "verona", "version": 1, "units": "mm",
                             "items": page["items"], "metadata": metadata, "settings": settings}
            page_items, page_settings = self.window._validate_document(page_document, trusted_items=trusted_items)
            validated_pages.append({"items": page_items, "selection": page["selection"],
                                    "settings": page_settings, "metadata": copy.deepcopy(metadata)})
        return details, validated_pages

    def _validate_detached(self, items, metadata, settings, trusted_items):
        document = {"format": "verona", "version": 1, "units": "mm",
                    "items": items, "metadata": metadata, "settings": settings}
        items, settings = self.window._validate_document(document, trusted_items=trusted_items)
        return items, metadata, settings

    def _make_plan(self, expected_revision, items, metadata, settings, selection, details, new_pages=()):
        window = self.window
        validated_pages = list(new_pages)
        plan_id = uuid.uuid4().hex
        filename = self.directory / f"{self.editor_id}.{plan_id}.png"
        render_preview(items, metadata, filename)
        while len(self.plans) >= 4:
            self._discard_plan(next(iter(self.plans)))
        self.plans[plan_id] = {"revision": expected_revision, "items": items,
                              "selection": selection, "filename": filename,
                              "metadata": metadata, "settings": settings,
                              "new_pages": validated_pages}
        return {"plan_id": plan_id, "revision": expected_revision, "details": details,
                "before_objects": len(window.items), "after_objects": len(items),
                "selection_after": selection, "objects": self._objects(items, metadata=metadata),
                "metadata_after": {k: v for k, v in metadata.items() if k != "source_preview"},
                "settings_after": settings,
                "preview_path": str(filename), "applied": False, "hardware_operations": False}

    def command_list(self, object_indices=None, filter=None):
        from .command_registry import command_list
        if filter is not None and not isinstance(filter, str):
            raise ValueError("filter must be a string.")
        if object_indices is not None and (not isinstance(object_indices, list)
                or any(type(index) is not int or not 0 <= index < len(self.window.items)
                       for index in object_indices)):
            raise ValueError("Invalid object indices.")
        return command_list(self.window.items, object_indices, filter)

    def _build_command_step(self, items, metadata, settings, selection, step):
        from .command_registry import command_availability, command_for_step
        if not isinstance(step, dict) or set(step) != {"command_id", "object_indices", "parameters"}:
            raise ValueError("Each step needs command_id, object_indices and parameters.")
        command_id = step["command_id"]
        object_indices = step["object_indices"]
        if not isinstance(command_id, str):
            raise ValueError("command_id must be a string.")
        enabled, reason = command_availability(items, command_id, object_indices)
        if not enabled:
            raise ValueError(reason)
        action, parameters = command_for_step(command_id, step["parameters"])
        target_selection = selection if action in ("configure", "path_order") else object_indices
        before_count = len(items)
        result, metadata, settings, next_selection, details = build_document_edit(
            items, metadata, settings, target_selection, action, parameters)
        if command_id == "edit.duplicate":
            from .i18n import tr
            for item in result[before_count:]:
                item["name"] = tr("{name} 複本", name=item.get("name", "路徑"))
        details, new_pages = self._split_new_pages(details, metadata, settings, items)
        result, metadata, settings = self._validate_detached(result, metadata, settings, items)
        details = {"command_id": command_id, **details}
        return result, metadata, settings, next_selection, details, new_pages

    def command_preview(self, expected_revision, command_id, object_indices, parameters):
        self._guard(expected_revision)
        window = self.window
        step = {"command_id": command_id, "object_indices": object_indices,
                "parameters": parameters}
        items, metadata, settings, selection, details, new_pages = self._build_command_step(
            window.items, window.metadata, window._cut_settings(),
            sorted(window.selected_indices()), step)
        return self._make_plan(expected_revision, items, metadata, settings, selection, details, new_pages)

    def command_batch_preview(self, expected_revision, steps):
        self._guard(expected_revision)
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps must be a nonempty list.")
        if len(steps) > 100:
            raise ValueError("A command batch can contain at most 100 steps.")
        window = self.window
        items = window.items
        metadata = window.metadata
        settings = window._cut_settings()
        selection = sorted(window.selected_indices())
        step_details = []
        new_pages = []
        for number, step in enumerate(steps, 1):
            try:
                items, metadata, settings, selection, details, pages = self._build_command_step(
                    items, metadata, settings, selection, step)
                new_pages.extend(pages)
            except (ValueError, TypeError, KeyError, RuntimeError, OSError) as error:
                command_id = step.get("command_id") if isinstance(step, dict) else None
                raise ValueError(f"Step {number} ({command_id or 'unknown'}) failed: {error}") from error
            step_details.append(details)
        details = {"action": "command_batch", "step_count": len(step_details),
                   "steps": step_details,
                   "indexing": "Each step's object_indices refer to the current intermediate item list after all earlier steps."}
        return self._make_plan(expected_revision, items, metadata, settings, selection, details, new_pages)

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
        source_document = window.document
        source_tab = window._active_document
        before = window._snapshot()
        window.items = plan["items"]
        window.metadata = plan["metadata"]
        window._set_cut_settings(plan["settings"])
        window.selected, window.selection = -1, set()
        window.select_many(plan["selection"])
        window.push_undo(before)
        window.refresh_all()
        page_documents = []
        for page in plan.get("new_pages", []):
            window.new_design()
            window.metadata = copy.deepcopy(page["metadata"])
            window._set_cut_settings(page["settings"])
            before_page = window._snapshot()
            window.items = page["items"]
            window.selected, window.selection = -1, set()
            window.select_many(page["selection"])
            window.push_undo(before_page)
            window.refresh_all()
            page_documents.append(window.document)
        if page_documents:
            window.tab_bar.setCurrentIndex(source_tab)
            window._register_multi_tab_operation(source_document, page_documents)
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
            from .preview_dialogs import PlotPreviewCanvas
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
                from .file_io import publish_new_file
                publish_new_file(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": str(destination), "revision": self.revision(),
                "printed": False, "hardware_operations": False}

    def prepare_print(self, expected_revision, print_id, printer_name, options):
        self._guard(expected_revision)
        if (not isinstance(print_id, str) or len(print_id) != 32
                or any(character not in "0123456789abcdef" for character in print_id)):
            raise ValueError("Invalid print_id.")
        if not isinstance(printer_name, str) or not printer_name or len(printer_name) > 1024 or "\0" in printer_name:
            raise ValueError("Choose a valid printer name from verona_list_printers.")
        if not isinstance(options, dict):
            raise ValueError("options must be an object.")
        options = copy.deepcopy(options)
        icc_profile_path = options.pop("icc_profile_path", None)
        rendering_intent = options.pop("rendering_intent", "perceptual")
        if rendering_intent not in ("perceptual", "relative"):
            raise ValueError("rendering_intent must be perceptual or relative.")
        if icc_profile_path is not None:
            if not isinstance(icc_profile_path, str) or not icc_profile_path or "\0" in icc_profile_path:
                raise ValueError("icc_profile_path must be a valid file path or omitted.")
            icc_profile_path = str(Path(icc_profile_path).expanduser().resolve(strict=True))
            if not Path(icc_profile_path).is_file():
                raise ValueError("icc_profile_path must refer to a file.")

        from PySide6.QtCore import QSizeF
        from PySide6.QtGui import QPageLayout, QPageSize
        from .print_dialog import apply_print_job_options
        from .printer_options import display_choice_zh, read_printer_options
        from .printing import (print_page_size, render_print_image, validate_printer,
                               write_print_preview_png)
        from .mcp_server import content_sha256

        window = self.window
        items, metadata = copy.deepcopy(window.items), copy.deepcopy(window.metadata)
        settings = copy.deepcopy(window._cut_settings())
        size = print_page_size(items, metadata, calibration=metadata.get("source_format") == "calibration")
        printer = _make_print_printer(printer_name)
        page = QPageSize(size, QPageSize.Unit.Millimeter)
        if not printer.setPageSize(page):
            raise ValueError("The selected print queue cannot use the design paper size.")
        printer.setPageOrientation(QPageLayout.Orientation.Landscape if size.width() > size.height()
                                   else QPageLayout.Orientation.Portrait)
        available_options = read_printer_options(printer_name)
        applied = apply_print_job_options(printer, options, available_options,
                                          size.width(), size.height())
        validate_printer(printer, items, metadata,
                         calibration=metadata.get("source_format") == "calibration")
        image = render_print_image(items, metadata,
            calibration=metadata.get("source_format") == "calibration",
            color_profile_path=icc_profile_path, rendering_intent=rendering_intent,
            dpi=PRINT_PREVIEW_DPI)
        preview_path = self.directory / f"{self.editor_id}.{print_id}.print.png"
        write_print_preview_png(preview_path, image)
        revision = self.revision()
        content_digest = content_sha256(items, metadata, settings)
        registration = metadata["print_cut"]
        registration_bounds = registration.get("bounds") if registration.get("enabled") else None
        media_value = applied["media"]
        result = {"print_id": print_id, "sha256": content_digest, "revision": revision,
            "content_sha256": content_digest, "preview_png": str(preview_path),
            "preview_sha256": hashlib.sha256(preview_path.read_bytes()).hexdigest(),
            "summary": {"printer": printer_name,
                "paper": f"{size.width():g} × {size.height():g} mm",
                "media": display_choice_zh(media_value) if media_value else "印表機預設",
                "quality": display_choice_zh(applied["quality"]) if applied["quality"] else "印表機預設",
                "color": "彩色" if applied["color_mode"] == "color" else "灰階",
                "copies": applied["copies"], "resolution_dpi": applied["resolution"],
                "preview_dpi": PRINT_PREVIEW_DPI,
                "icc_profile_path": icc_profile_path, "rendering_intent": rendering_intent,
                "registration_marks_enabled": bool(registration.get("enabled")),
                "registration_bounds_mm": registration_bounds},
            "hardware_operations": False, "printed": False}
        if print_id in self.print_jobs:
            preview_path.unlink(missing_ok=True)
            raise ValueError("print_id has already been prepared.")
        self.print_jobs[print_id] = {"sha256": content_digest, "revision": revision,
            "preview_png": str(preview_path), "printer": printer, "items": items,
            "metadata": metadata, "calibration": metadata.get("source_format") == "calibration",
            "color_profile_path": icc_profile_path, "rendering_intent": rendering_intent}
        return result

    def print_prepared(self, print_id, sha256, confirmed_by_user):
        if confirmed_by_user is not True:
            raise ValueError("Show the prepared preview and summary to the user and obtain explicit consent first.")
        prepared = self.print_jobs.get(print_id)
        if prepared is None:
            raise ValueError("Unknown or already attempted print_id; it cannot be replayed.")
        if not isinstance(sha256, str) or sha256 != prepared["sha256"]:
            raise ValueError("Print SHA-256 does not match the prepared snapshot; nothing was printed.")
        del self.print_jobs[print_id]
        self._guard(prepared["revision"])
        from .printing import print_document
        # Submit through the same vector page renderer as the GUI print dialog;
        # the PNG preview is a lower-resolution rendering of that same page.
        print_document(prepared["printer"], prepared["items"], prepared["metadata"],
            calibration=prepared["calibration"], color_profile_path=prepared["color_profile_path"],
            rendering_intent=prepared["rendering_intent"])
        return {"status": "submitted", "print_id": print_id, "sha256": sha256,
                "revision": prepared["revision"], "printed": True,
                "automatic_retry": False, "print_id_consumed": True}

    def prepare_job(self, expected_revision, force=None, speed=None, tool=None, passes=None):
        self._guard(expected_revision)
        from .commands import encode_document_job
        from .mcp_server import content_sha256
        window = self.window
        items, metadata = copy.deepcopy(window.items), copy.deepcopy(window.metadata)
        settings = copy.deepcopy(window._cut_settings())
        payload, job = encode_document_job(items, metadata, settings,
            force=force, speed=speed, tool=tool, passes=passes)
        points = [point for contour in job["contours"] for point in contour]
        registration = metadata.get("print_cut", {})
        content_digest = content_sha256(items, metadata, settings)
        payload_path = self.directory / f"{self.editor_id}.{uuid.uuid4().hex}.job.plt"
        payload_path.write_bytes(payload)
        bounds = job["registration_bounds"]
        return {"payload_path": str(payload_path), "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload), "source": window.current_file or f"editor:{self.editor_id}",
            "contours": len(job["contours"]), "points": len(points),
            "design_bounds_mm": [min(p[0] for p in points), min(p[1] for p in points),
                                 max(p[0] for p in points), max(p[1] for p in points)],
            "force": job["force"], "speed": job["speed"],
            "tool": "blade" if job["tool"] == 0 else "pen", "passes": job["passes"],
            "path_order_enabled": metadata.get("optimize_paths", False),
            "print_cut": bool(bounds), "registration_bounds_mm": bounds,
            "registration_offset_mm": list(job["registration_offset"]),
            "registration_profile": {key: registration[key] for key in
                ("mark_stroke", "mark_short_length", "paper_width", "paper_height") if key in registration},
            "command_start": payload[:200].decode("ascii"), "command_end": payload[-80:].decode("ascii"),
            "revision": expected_revision, "content_sha256": content_digest,
            "hardware_operations": False}

    def dispatch(self, command, **arguments):
        handlers = {"state": self.state, "summary": self.summary, "preview": self.preview, "apply": self.apply,
                    "select": self.select, "history": self.history, "image": self.image,
                    "ui_screenshot": self.ui_screenshot, "ui_inspect": self.ui_inspect,
                    "ui_status": self.ui_status,
                    "open_document": self.open_document, "save_document": self.save_document,
                    "export_pdf": self.export_pdf, "printers": self.printers,
                    "prepare_print": self.prepare_print, "print_prepared": self.print_prepared,
                    "prepare_job": self.prepare_job,
                    "command_list": self.command_list, "command_preview": self.command_preview,
                    "command_batch_preview": self.command_batch_preview}
        if command not in handlers:
            raise ValueError("Unsupported editor command; no device operations are available here.")
        return handlers[command](**arguments)

    def ui_screenshot(self, target="window", max_width_px=1600):
        from .ui_introspection import screenshot
        return screenshot(self.window, self.directory, self.editor_id, target, max_width_px)

    def ui_inspect(self, target="window", max_depth=8):
        from .ui_introspection import inspect
        return inspect(self.window, target, max_depth)

    def ui_status(self):
        from .ui_introspection import status
        return status(self.window)

    def printers(self, paper_width_mm=210, paper_height_mm=297):
        from .editor_actions import finite_number
        from .printing import printer_profiles
        width = finite_number(paper_width_mm, "paper_width_mm", 1, 300)
        height = finite_number(paper_height_mm, "paper_height_mm", 1, 300)
        return {"printers": printer_profiles(width, height), "paper_width_mm": width,
                "paper_height_mm": height, "printed": False}

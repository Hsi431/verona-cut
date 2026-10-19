"""Local stdio MCP control using the editor's existing encoder and transport."""
import argparse
import hashlib
import os
from pathlib import Path
import threading
from typing import Any, Literal
import uuid

from . import commands, transport
from .device_lock import device_lock
from .editor_client import editor_request, list_editors


class VeronaController:
    def __init__(self, host=None, port=transport.TCP_PORT, *, lock_path=None):
        self.host, self.port = host, port
        if host is not None:
            transport._ipv4_endpoint(host, port)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.lock_path = Path(lock_path) if lock_path else state_root / "verona/mcp-device.lock"

    def device_lock(self):
        return device_lock(self.lock_path)

    def capabilities(self):
        return {"tcp_endpoint": {"host": self.host, "port": self.port},
                "usb_vid_pid": "0483:5750", "input_format": "jdesign version 1, mm",
                "transport_success_means": "bytes delivered, physical completion unknown",
                "automatic_retry": False, "raw_camera_image_api": False,
                "firmware_update": False, "device_abort_command": False,
                "motion_requires": ["prepared job", "matching SHA-256", "machine_ready=true"],
                "live_editor": {"discovery": "verona_list_editors", "unsaved_state": True,
                                "preview_before_apply": True, "revision_guard": True,
                                "undoable_edits": True, "machine_operations": False},
                "device_lock_scope": "Verona GUI, CLI and MCP operations for this user"}

    def query_version(self):
        if self.host is None:
            raise ValueError("Start the server with --host to enable TCP operations.")
        with self.device_lock():
            version = transport.query_tcp_version(self.host, self.port)
        return {"software_version": version, "host": self.host, "port": self.port}

    def list_usb(self):
        return [{"bus": device.bus, "address": device.address,
                 "vendor_id": device.idVendor, "product_id": device.idProduct}
                for device in transport.usb_devices()]

    def prepare_job(self, design_path, force=None, speed=None, tool=None):
        # Reuse the editor's full document validation without creating a window.
        from .ui import VeronaWindow
        path = Path(design_path).expanduser().resolve()
        from .input_safety import read_design
        data = read_design(path)
        items, settings = VeronaWindow._validate_document(data)
        metadata = data.get("metadata", {})
        payload, job = commands.encode_document_job(items, metadata, settings, force=force, speed=speed, tool=tool)
        contours = job["contours"]
        bounds = job["registration_bounds"]
        registration = metadata.get("print_cut", {})
        digest = hashlib.sha256(payload).hexdigest()
        points = [point for contour in contours for point in contour]
        job_id = uuid.uuid4().hex
        with self.jobs_lock:
            self.jobs[job_id] = payload
        return {"job_id": job_id, "sha256": digest, "bytes": len(payload),
                "source": str(path), "contours": len(contours), "points": len(points),
                "design_bounds_mm": [min(p[0] for p in points), min(p[1] for p in points),
                                     max(p[0] for p in points), max(p[1] for p in points)],
                "force": job["force"], "speed": job["speed"], "tool": "blade" if job["tool"] == 0 else "pen",
                "path_order_enabled": metadata.get("optimize_paths", False),
                "print_cut": bool(bounds), "registration_bounds_mm": bounds,
                "registration_offset_mm": list(job["registration_offset"]),
                "registration_profile": {key: registration[key] for key in
                    ("mark_stroke", "mark_short_length", "paper_width", "paper_height")
                    if key in registration},
                "command_start": payload[:200].decode("ascii"),
                "command_end": payload[-80:].decode("ascii"),
                "hardware_operations": False}

    def send_job(self, job_id, sha256, machine_ready, connection="tcp",
                 usb_bus=None, usb_address=None):
        if machine_ready is not True:
            raise ValueError("Confirm the user has loaded the intended material and is ready for this job.")
        if connection not in ("tcp", "usb"):
            raise ValueError("connection must be tcp or usb")
        if connection == "tcp" and self.host is None:
            raise ValueError("Start the server with --host to enable TCP operations.")
        if connection == "usb" and (usb_bus is None or usb_address is None):
            raise ValueError("Select an explicit bus/address from verona_list_usb.")
        with self.device_lock():
            with self.jobs_lock:
                payload = self.jobs.get(job_id)
                if payload is None:
                    raise ValueError("Unknown or already attempted job; it cannot be replayed.")
                if hashlib.sha256(payload).hexdigest() != sha256:
                    raise ValueError("Job SHA-256 does not match the prepared snapshot; no data sent.")
                if connection == "usb":
                    devices = [device for device in transport.usb_devices()
                               if device.bus == usb_bus and device.address == usb_address]
                    if len(devices) != 1:
                        raise ValueError("Selected USB device is not uniquely available; no data sent.")
                # Consume before entering transport, including failures/cancellation.
                del self.jobs[job_id]
            progress = {"bytes_sent": 0}

            def on_progress(sent, _total):
                progress["bytes_sent"] = sent

            try:
                sent = (transport.send_tcp(payload, self.host, self.port, on_progress=on_progress)
                        if connection == "tcp" else transport.send_usb(payload, devices[0]))
            except Exception as error:
                return {"status": "failed", "job_id": job_id, "sha256": sha256,
                        "bytes_total": len(payload),
                        "bytes_sent": getattr(error, "bytes_sent", progress["bytes_sent"])
                                      if connection == "tcp" else getattr(error, "bytes_sent", None),
                        "error": str(error), "delivery_may_be_partial": True,
                        "physical_completion": "unknown", "automatic_retry": False,
                        "job_consumed": True}
        return {"status": "transport_delivered", "job_id": job_id, "sha256": sha256,
                "bytes_sent": sent, "bytes_total": len(payload),
                "physical_completion": "unknown", "automatic_retry": False,
                "job_consumed": True}


def create_server(controller):
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
    from mcp.server.mcpserver.utilities.types import Image
    from pydantic import StrictBool, StrictFloat, StrictInt
    server = MCPServer("Verona", instructions=(
        "Prepare and review the exact job before sending. machine_ready asserts current user "
        "readiness; it is not a hardware interlock. Send only within the user's authorization. "
        "Never automatically prepare a replacement job or retry a failed/consumed job. "
        "Delivery is not physical completion. No camera capture, abort or firmware update API. "
        "GUI, CLI and MCP operations share a non-blocking device lock. "
        "Startup does not contact the device. Read verona_operating_guide for editing workflows. "
        "Use verona_list_editors and verona_editor_state to identify the user's live document. "
        "Preview edits, inspect the returned image and bounds, then apply the exact plan with "
        "its expected revision. Edits are undoable and never send machine commands."))
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    def invoke(function, *args):
        try:
            return function(*args)
        except (ValueError, RuntimeError, OSError) as error:
            raise ToolError(str(error)) from error

    @server.tool(annotations=readonly)
    def verona_capabilities() -> dict[str, Any]:
        """Read supported controls and configured endpoint; no device operations."""
        return controller.capabilities()

    @server.tool(annotations=readonly)
    def verona_operating_guide() -> dict[str, str]:
        """Read the editor, Print & Cut, bleed, layout and machine-operation rules."""
        from importlib.resources import files
        return {"guide": files("verona").joinpath("agent-guide.md").read_text(encoding="utf-8")}

    @server.tool(annotations=readonly)
    def verona_list_editors() -> list[dict[str, Any]]:
        """Find same-user running Verona editors, including their current file and revision."""
        return invoke(list_editors)

    @server.tool(annotations=readonly)
    def verona_editor_state(editor_id: str, object_indices: list[StrictInt] | None = None,
                            include_geometry: StrictBool = False) -> dict[str, Any]:
        """Read live unsaved state, selection, settings, cutting frame, objects and bounds errors.

        Object indices belong to this revision. Geometry is optional; request a
        small explicit subset when needed. Source bitmap bytes are not returned.
        """
        return invoke(editor_request, editor_id, "state",
                      {"object_indices": object_indices, "include_geometry": include_geometry})

    @server.tool(annotations=readonly)
    def verona_list_printers(editor_id: str, paper_width_mm: StrictFloat = 210,
                              paper_height_mm: StrictFloat = 297) -> dict[str, Any]:
        """Read installed print queues and driver-reported minimum margins for the requested paper.

        Does not submit printing or change the document. Choose a returned name
        explicitly in configure_document.registration.printer_name to fit marks.
        """
        return invoke(editor_request, editor_id, "printers",
            {"paper_width_mm": paper_width_mm, "paper_height_mm": paper_height_mm})

    @server.tool(annotations=readonly)
    def verona_editor_image(editor_id: str, plan_id: str | None = None,
                             view: Literal["artwork", "toolpath"] = "artwork") -> Image:
        """View the current document or detached plan without changing it.

        artwork is the full design. toolpath renders actual encoded U/D moves,
        including saved path order/settings, in machine cross/feed axes. It
        requires valid cut geometry and does not contact or move the cutter.
        """
        result = invoke(editor_request, editor_id, "image", {"plan_id": plan_id, "view": view})
        return Image(path=result["path"])

    preview_annotation = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)

    @server.tool(annotations=preview_annotation)
    def verona_preview_edit(editor_id: str, expected_revision: str,
                            action: Literal["transform", "duplicate", "delete", "set_cut", "group", "ungroup", "align"],
                            object_indices: list[StrictInt], parameters: dict[str, Any]) -> dict[str, Any]:
        """Preview an undoable edit without changing the user's document.

        transform: dx_mm/dy_mm, uniform scale, rotation_deg, mirror_x/mirror_y.
        duplicate: dx_mm/dy_mm. set_cut: enabled boolean. align: mode is left,
        hcenter, right, top, vcenter, bottom, distribute_h or distribute_v.
        group/ungroup/delete: {}. Alignment treats outer groups as units.
        Groups expand together; registration guides cannot be edited.
        """
        return invoke(editor_request, editor_id, "preview",
                      {"expected_revision": expected_revision, "action": action,
                       "object_indices": object_indices, "parameters": parameters})

    @server.tool(annotations=preview_annotation)
    def verona_add_shape(editor_id: str, expected_revision: str,
                          shape: Literal["rectangle", "ellipse"], x_mm: StrictFloat,
                          y_mm: StrictFloat, width_mm: StrictFloat, height_mm: StrictFloat) -> dict[str, Any]:
        """Preview a new cut outline at the given top-left position and physical size in mm."""
        return invoke(editor_request, editor_id, "preview",
            {"expected_revision": expected_revision, "action": "add_shape", "object_indices": [],
             "parameters": {"shape": shape, "x_mm": x_mm, "y_mm": y_mm,
                            "width_mm": width_mm, "height_mm": height_mm}})

    @server.tool(annotations=preview_annotation)
    def verona_add_text(editor_id: str, expected_revision: str, text: str,
                         x_mm: StrictFloat, y_mm: StrictFloat, font_size_mm: StrictFloat = 10,
                         font_family: str = "Noto Sans") -> dict[str, Any]:
        """Preview text converted to cut outlines. Font size is the em size, not the glyph height.

        Reports the resolved font family; inspect the preview for fallback fonts
        and missing glyphs. Text becomes outlines, not editable typeset text.
        """
        return invoke(editor_request, editor_id, "preview",
            {"expected_revision": expected_revision, "action": "add_text", "object_indices": [],
             "parameters": {"text": text, "x_mm": x_mm, "y_mm": y_mm,
                            "font_size_mm": font_size_mm, "font_family": font_family}})

    @server.tool(annotations=preview_annotation)
    def verona_configure_document(editor_id: str, expected_revision: str,
                                    registration: dict[str, Any] | None = None,
                                    cut_settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Preview paper, registration or saved cut settings; never contact a device.

        registration: enabled, paper_width/paper_height (mm, <=300), margins
        [left,top,right,bottom] (mm), mark_stroke (0.5–1.5 mm),
        mark_short_length (4–12.5 mm), offset [x,y] (-5–5 mm), printer_name
        (installed queue; fits reported hard margins unless margins are explicit).
        cut_settings: custom bool, force int1–160, speed int1–13, tool int0/1.
        Reports requires_reprint for changes to printed marks or paper.
        Existing artwork keeps its size/position; out-of-bounds cuts stay blocked.
        """
        parameters = {}
        if registration is not None:
            parameters["registration"] = registration
        if cut_settings is not None:
            parameters["cut_settings"] = cut_settings
        return invoke(editor_request, editor_id, "preview",
            {"expected_revision": expected_revision, "action": "configure", "object_indices": [],
             "parameters": parameters})

    @server.tool(annotations=preview_annotation)
    def verona_import_artwork(editor_id: str, expected_revision: str, path: str,
                               x_mm: StrictFloat | None = None, y_mm: StrictFloat | None = None,
                               tracing: dict[str, Any] | None = None,
                               representation: Literal["vector", "raster"] | None = None,
                               page: StrictInt | None = None, dpi: StrictInt | None = None) -> dict[str, Any]:
        """Preview importing SVG, PDF-page artwork or raster into the live document, preserving existing work.

        x_mm/y_mm place the cut bounding box top-left; omitted axes center in
        the cutting frame. SVG keeps its physical size. Raster tracing options:
        mode print_cut/cut_only/background; mask_mode alpha/background/colors;
        colors is a list of 1–16 foreground #RRGGBB colors in colors mode;
        width_mm/height_mm (one axis preserves aspect ratio); background #RRGGBB;
        tolerance int0–255; inverted/keep_holes/clip bool; smoothing int0–100;
        minimum_area_mm2 0–1000; offset_mm -5–5; bleed_mm 0–5 and bleed_color.
        Print & Cut creates A4 marks if absent. Review out-of-bounds and reprint
        when the registration layout changes. No printing or machine operations.
        SVG defaults to vector paths; representation=raster explicitly renders
        supported text/simpleCSS/gradients/internal references/embedded images/masks.
        Unsupported tags/styles (including clipPath/filters/animation) and external
        references reject. PDF renders one 1-based page (default1), no vector-path
        extraction. dpi75–600 (default300),16M pixels,64MiB source,300mm page cap.
        Physical page size is retained unless tracing supplies explicit dimensions.
        """
        parameters = {"path": path}
        if x_mm is not None:
            parameters["x_mm"] = x_mm
        if y_mm is not None:
            parameters["y_mm"] = y_mm
        if tracing is not None:
            parameters["tracing"] = tracing
        for key, value in (("representation", representation), ("page", page), ("dpi", dpi)):
            if value is not None:
                parameters[key] = value
        return invoke(editor_request, editor_id, "preview",
            {"expected_revision": expected_revision, "action": "import_artwork",
             "object_indices": [], "parameters": parameters})

    @server.tool(annotations=preview_annotation)
    def verona_retrace_image(editor_id: str, expected_revision: str, object_index: StrictInt,
                              parameters: dict[str, Any]) -> dict[str, Any]:
        """Preview reediting one saved image's mask/cut contours, retaining pixels, affine and cut flag.

        parameters: mask_mode saved/alpha/background/colors; background #RRGGBB;
        colors is a list of 1–16 foreground #RRGGBB colors in colors mode;
        tolerance int0–255; inverted/keep_holes/clip bool; smoothing int0–100;
        minimum_area_mm2 0–1000; offset_mm -5–5 in document coordinates.
        brush_strokes: 1–100 objects with mode include/remove, width_mm 0.1–20,
        points_mm [[x,y],...]; at most 1000 points. Strokes use document mm,
        retain bitmap pixels and save the resulting mask, not a stroke history.
        Saved masks can be reused without reconstructing unsaved operation history.
        A new mask/offset can replace manually attached cuts; inspect the preview.
        """
        return invoke(editor_request, editor_id, "preview",
            {"expected_revision": expected_revision, "action": "retrace_image",
             "object_indices": [object_index], "parameters": parameters})

    @server.tool(annotations=preview_annotation)
    def verona_link_cut_paths(editor_id: str, expected_revision: str, image_index: StrictInt,
                               cut_index: StrictInt, contour_indices: list[StrictInt] | None = None) -> dict[str, Any]:
        """Preview attaching explicitly chosen external contours to one image; no automatic matching.

        Replaces the image's current cut contours and enables its cut flag.
        Removes only the transferred contours from the separate vector object;
        deletes that source object if empty. None transfers all its contours.
        Image pixels, mask and transform stay unchanged. Once applied, image
        and attached cuts move/scale/duplicate/save as one object, with one undo.
        Inspect the exact geometry and preview, especially with aggregate paths.
        """
        parameters = {} if contour_indices is None else {"contour_indices": contour_indices}
        return invoke(editor_request, editor_id, "preview",
            {"expected_revision": expected_revision, "action": "link_cut",
             "object_indices": [image_index, cut_index], "parameters": parameters})

    @server.tool(annotations=preview_annotation)
    def verona_fill_area(editor_id: str, expected_revision: str, object_indices: list[StrictInt],
                          gap_mm: StrictFloat = 2, margin_mm: StrictFloat = 0,
                          max_copies: StrictInt = 1000, allow_rotate: StrictBool = False) -> dict[str, Any]:
        """Preview filling the cutting frame with a repeated selected pattern.

        Replaces the selected originals with fixed-orientation grid copies, keeps
        other objects and avoids their cutting footprints. Includes print bleed.
        allow_rotate=true compares 0/90 degrees for the whole pattern and chooses
        the actual greater copy count, retaining 0 on ties. Reports rotation/limits;
        this is rectangular packing, not mixed-orientation or optimal shape nesting.
        """
        return invoke(editor_request, editor_id, "preview",
                      {"expected_revision": expected_revision, "action": "fill_area",
                       "object_indices": object_indices,
                       "parameters": {"gap_mm": gap_mm, "margin_mm": margin_mm,
                                      "max_copies": max_copies, "allow_rotate": allow_rotate}})

    @server.tool(annotations=preview_annotation)
    def verona_pack_objects(editor_id: str, expected_revision: str, object_indices: list[StrictInt],
                              copies_per_unit: list[StrictInt] | None = None,
                              gap_mm: StrictFloat = 2, margin_mm: StrictFloat = 0,
                              allow_rotate: StrictBool = False, shape_aware: StrictBool = False,
                              fill: StrictBool = False) -> dict[str, Any]:
        """Preview mixed-size packing of outer groups and standalone objects, preserving sizes.

        Units follow ascending source index. copies_per_unit defaults to1each;
        fill=false places exactly requested counts or rejects the entire plan.
        fill=true then adds copies while space/limits permit; per-unit counts
        are reported. Rotation allows quarter turns. shape_aware uses real
        footprints (holes filled conservatively), otherwise rectangles.
        Includes print bleed, obstacles and mark clearance for printed artwork.
        Bounded corner/vertex heuristic:20000checks/12seconds, not optimal nesting.
        Copies1000/objects2000/cutpoints2million max. Read limit_reached.
        """
        parameters = {"gap_mm": gap_mm, "margin_mm": margin_mm, "allow_rotate": allow_rotate,
                      "shape_aware": shape_aware, "fill": fill}
        if copies_per_unit is not None:
            parameters["copies_per_unit"] = copies_per_unit
        return invoke(editor_request, editor_id, "preview",
            {"expected_revision": expected_revision, "action": "pack_objects",
             "object_indices": object_indices, "parameters": parameters})

    @server.tool(annotations=preview_annotation)
    def verona_set_vector_print(editor_id: str, expected_revision: str, object_indices: list[StrictInt],
                                 fill: str | None = None, stroke: str | None = None,
                                 width_mm: StrictFloat = .2) -> dict[str, Any]:
        """Preview explicit vector printing; cut flags/points remain independent.

        Colors are #RRGGBB or null. Both null removes printing. Physical stroke
        width0.01–5mm remains fixed in mm under object transforms; round caps/joins,
        odd-even fill. Only these explicit vector indices, without group expansion.
        Canvas, preview, PDF and packing footprints include the style. One undo;
        changes require a matching reprint. No actual printer operation.
        """
        return invoke(editor_request, editor_id, "preview",
            {"expected_revision": expected_revision, "action": "vector_print", "object_indices": object_indices,
             "parameters": {"fill": fill, "stroke": stroke, "width_mm": width_mm}})

    @server.tool(annotations=preview_annotation)
    def verona_edit_path(editor_id: str, expected_revision: str, object_index: StrictInt,
                         contour_index: StrictInt, nodes: list[dict[str, Any]], closed: StrictBool) -> dict[str, Any]:
        """Preview replacing one explicit cut contour with editable line/cubic nodes in document mm.

        Each node has point:[x,y] and optional in/out:[x,y] or null. Adjacent
        out/in handles define a cubic; missing handles use their anchors. A
        segment with neither handle is straight. Supply at least2open/3closed
        nodes, at most10000. Closure is implicit; do not repeat the first node.
        Saved controls and adaptive0.025mm polylines share transforms/undo/save.
        Read node_paths_mm in include_geometry state; an absent contour entry
        is a polyline whose cut_geometry_mm supplies anchors (omit closing copy).
        Changes cut geometry only, retains original image and mask; contour-based
        bleed follows edits. It does not recover original unsaved curve handles.
        """
        return invoke(editor_request, editor_id, "preview",
            {"expected_revision": expected_revision, "action": "edit_path",
             "object_indices": [object_index], "parameters": {"contour_index": contour_index,
                 "path": {"nodes": nodes, "closed": closed}}})

    @server.tool(annotations=preview_annotation)
    def verona_optimize_paths(editor_id: str, expected_revision: str,
                               enabled: StrictBool = True) -> dict[str, Any]:
        """Preview explicit whole-job path ordering without moving artwork or changing contour points.

        Enforces inside-first and compares stable/nearest eligible orders, keeping
        the original when it is valid and no longer. Reports estimated pen-up
        distance and any increase required by inside-first. Physical speed and
        registration acquisition are not measured. Each contour start/direction
        stays unchanged; the policy is saved and recalculated after editing.
        At most4000contours/100000relations/10s containment search. Disable always
        restores document contour order. Apply is one undo; no device operations.
        """
        return invoke(editor_request, editor_id, "preview",
            {"expected_revision": expected_revision, "action": "path_order",
             "object_indices": [], "parameters": {"enabled": enabled}})

    @server.tool(annotations=preview_annotation)
    def verona_add_bleed(editor_id: str, expected_revision: str, object_indices: list[StrictInt],
                          width_mm: StrictFloat, color: str, inner_mm: StrictFloat = 0,
                          mode: Literal["solid", "edge"] = "solid") -> dict[str, Any]:
        """Preview printed bleed on explicit image objects; cut lines stay unchanged.

        color is #RRGGBB or 'source' to use each object's saved source color.
        If source color is unavailable, ask for a color. Width 0 removes this
        added bleed when inner_mm is also 0. Optional inner_mm (0–1 mm) paints
        a thin color band inside the cut AFTER the bitmap; it visibly covers
        edge artwork. Source pixels, masks and cut paths are retained.
        Only the explicit image indices change, even inside groups.
        mode=edge extends nearest visible source colors into the outer ring,
        using up to300dpi /1200pixels per edge; original image print resolution
        stays unchanged. No texture synthesis. Color still controls inner bleed.
        """
        return invoke(editor_request, editor_id, "preview",
                      {"expected_revision": expected_revision, "action": "bleed",
                       "object_indices": object_indices,
                       "parameters": {"width_mm": width_mm, "color": color, "inner_mm": inner_mm, "mode": mode}})

    @server.tool(annotations=preview_annotation)
    def verona_apply_edit(editor_id: str, expected_revision: str, plan_id: str) -> dict[str, Any]:
        """Apply one reviewed preview as a single undo step; reject a changed document or used plan."""
        return invoke(editor_request, editor_id, "apply",
                      {"expected_revision": expected_revision, "plan_id": plan_id})

    @server.tool(annotations=preview_annotation)
    def verona_editor_select(editor_id: str, expected_revision: str,
                             object_indices: list[StrictInt]) -> dict[str, Any]:
        """Select explicit objects in the live editor; [] clears selection. Does not edit artwork."""
        return invoke(editor_request, editor_id, "select",
                      {"expected_revision": expected_revision, "object_indices": object_indices})

    @server.tool(annotations=preview_annotation)
    def verona_editor_history(editor_id: str, expected_revision: str,
                              redo: StrictBool = False) -> dict[str, Any]:
        """Undo the latest editor change, or redo with redo=true. Includes manual user edits."""
        return invoke(editor_request, editor_id, "history",
                      {"expected_revision": expected_revision, "redo": redo})

    @server.tool(annotations=preview_annotation)
    def verona_editor_open(editor_id: str, expected_revision: str, path: str) -> dict[str, Any]:
        """Open a jdesign/json/LDS in a new tab, preserving all current unsaved tabs; max 64 MiB."""
        return invoke(editor_request, editor_id, "open_document",
                      {"expected_revision": expected_revision, "path": path})

    file_annotation = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

    @server.tool(annotations=file_annotation)
    def verona_editor_save(editor_id: str, expected_revision: str, path: str,
                            overwrite: StrictBool = False) -> dict[str, Any]:
        """Save the live unsaved document as jdesign/json. Existing files need explicit overwrite=true.

        LDS cannot be overwritten. Saving is a disk operation, not an undoable
        artwork edit. Use an output path authorized by the user.
        """
        return invoke(editor_request, editor_id, "save_document",
                      {"expected_revision": expected_revision, "path": path, "overwrite": overwrite})

    @server.tool(annotations=file_annotation)
    def verona_editor_export_pdf(editor_id: str, expected_revision: str, path: str,
                                  overwrite: StrictBool = False) -> dict[str, Any]:
        """Export the current artwork, printed bleed and marks to PDF without sending to a printer.

        Existing files require explicit overwrite=true. Print the result at
        100% actual size with a matching registration layout.
        """
        return invoke(editor_request, editor_id, "export_pdf",
                      {"expected_revision": expected_revision, "path": path, "overwrite": overwrite})

    @server.tool(annotations=readonly)
    def verona_query_version() -> dict[str, Any]:
        """Send only the official read-only ;RSVER; query to the configured TCP endpoint."""
        return invoke(controller.query_version)

    @server.tool(annotations=readonly)
    def verona_list_usb() -> list[dict[str, int]]:
        """Enumerate matching USB devices without configuring or claiming interfaces."""
        return invoke(controller.list_usb)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False))
    def verona_prepare_job(design_path: str, force: StrictInt | None = None,
                           speed: StrictInt | None = None, tool: StrictInt | None = None) -> dict[str, Any]:
        """Validate a saved jdesign and snapshot its encoded job; does not contact the cutter.

        Review bounds, tool, force/speed, registration and SHA before sending.
        None retains saved settings; saved custom=false retains machine force/speed.
        """
        return invoke(controller.prepare_job, design_path, force, speed, tool)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                            idempotentHint=False))
    def verona_send_job(job_id: str, sha256: str, machine_ready: StrictBool,
                        connection: Literal["tcp", "usb"] = "tcp",
                        usb_bus: StrictInt | None = None, usb_address: StrictInt | None = None) -> dict[str, Any]:
        """Execute one prepared job. Can move the head/material and lower the blade.

        Requires current user authorization and loaded material. Consumes the job
        before transport; failure may leave partial device data. Never auto-retry.
        """
        return invoke(controller.send_job, job_id, sha256, machine_ready, connection, usb_bus, usb_address)

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verona local stdio MCP server")
    parser.add_argument("--host", default=os.environ.get("VERONA_HOST") or None,
                        help="Single cutter IPv4 address; defaults to VERONA_HOST")
    parser.add_argument("--port", type=int, default=transport.TCP_PORT)
    args = parser.parse_args(argv)
    try:
        server = create_server(VeronaController(args.host, args.port))
    except ImportError as error:
        raise SystemExit("Install the MCP extra: pip install '.[mcp]'") from error
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

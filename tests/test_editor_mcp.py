"""An actual offscreen GUI controlled through the optional SDK's stdio client."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.test_editor_actions import image_item


ROOT = Path(__file__).resolve().parents[1]
HAS_MCP = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(HAS_MCP, "optional MCP SDK is not installed")
class EditorMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_agent_reads_unsaved_state_previews_applies_and_undoes(self):
        from mcp import Client, StdioServerParameters
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            runtime = directory / "runtime"
            runtime.mkdir(mode=0o700)
            source = directory / "fixture.jdesign"
            document = {"format": "verona", "version": 1, "units": "mm",
                        "items": [image_item()],
                        "metadata": {"print_cut": {"enabled": True, "paper_width": 100,
                                                   "paper_height": 100, "bounds": [10, 10, 90, 90]}}}
            source.write_text(json.dumps(document))
            original = source.read_bytes()
            env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "XDG_RUNTIME_DIR": str(runtime),
                   "XDG_CONFIG_HOME": str(directory / "config")}
            gui = await asyncio.create_subprocess_exec(sys.executable, "-m", "verona",
                "--open", str(source), cwd=ROOT, env=env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            try:
                socket_dir = runtime / "verona/editors"
                for _ in range(300):
                    if list(socket_dir.glob("*.sock")):
                        break
                    if gui.returncode is not None:
                        self.fail((await gui.stderr.read()).decode())
                    await asyncio.sleep(.02)
                self.assertTrue(list(socket_dir.glob("*.sock")), "GUI bridge did not start")
                parameters = StdioServerParameters(command=sys.executable,
                    args=["-m", "verona.mcp_server"], cwd=ROOT, env=env)
                async with Client(parameters, read_timeout_seconds=20) as client:
                    async def call(name, **arguments):
                        result = await client.call_tool(name, arguments)
                        self.assertFalse(result.is_error, str(result.content))
                        data = result.structured_content
                        return data.get("result", data)

                    editors = await call("verona_list_editors")
                    self.assertEqual(len(editors), 1)
                    editor_id = editors[0]["editor_id"]
                    state = await call("verona_editor_state", editor_id=editor_id,
                                       object_indices=[0], include_geometry=True)
                    self.assertEqual(state["file"], str(source))
                    self.assertFalse(state["dirty"])
                    geometry = state["objects"][0]["cut_geometry_mm"]
                    plan = await call("verona_add_bleed", editor_id=editor_id,
                                      expected_revision=state["revision"], object_indices=[0],
                                      width_mm=1, color="source", inner_mm=.5, mode="edge")
                    unchanged = await call("verona_editor_state", editor_id=editor_id)
                    self.assertEqual(unchanged["revision"], state["revision"])
                    picture = await client.call_tool("verona_editor_image",
                        {"editor_id": editor_id, "plan_id": plan["plan_id"]})
                    self.assertFalse(picture.is_error)
                    self.assertTrue(any(content.type == "image" and content.mime_type == "image/png"
                                        for content in picture.content))
                    applied = await call("verona_apply_edit", editor_id=editor_id,
                                         expected_revision=state["revision"], plan_id=plan["plan_id"])
                    self.assertTrue(applied["dirty"])
                    self.assertEqual(applied["undo_steps"], 1)
                    self.assertEqual(source.read_bytes(), original)
                    after = await call("verona_editor_state", editor_id=editor_id,
                                       object_indices=[0], include_geometry=True)
                    self.assertEqual(after["objects"][0]["cut_geometry_mm"], geometry)
                    self.assertEqual(after["objects"][0]["bleed"]["width_mm"], 1)
                    self.assertEqual(after["objects"][0]["bleed"]["inner_mm"], .5)
                    self.assertEqual(after["objects"][0]["bleed"]["mode"], "edge")
                    undone = await call("verona_editor_history", editor_id=editor_id,
                                        expected_revision=after["revision"])
                    self.assertFalse(undone["dirty"])
                    fill = await call("verona_fill_area", editor_id=editor_id,
                                      expected_revision=undone["revision"], object_indices=[0],
                                      gap_mm=2, margin_mm=2, max_copies=3, allow_rotate=True)
                    self.assertEqual(fill["details"]["copies"], 3)
                    self.assertEqual(fill["details"]["rotation_deg"], 0)
                    filled = await call("verona_apply_edit", editor_id=editor_id,
                                        expected_revision=undone["revision"], plan_id=fill["plan_id"])
                    self.assertEqual(len(filled["objects"]), 3)
                    stale = await client.call_tool("verona_apply_edit",
                        {"editor_id": editor_id, "expected_revision": undone["revision"],
                         "plan_id": fill["plan_id"]})
                    self.assertTrue(stale.is_error)
                    self.assertEqual(source.read_bytes(), original)
                    saved_path = directory / "agent-output.jdesign"
                    saved = await call("verona_editor_save", editor_id=editor_id,
                                       expected_revision=filled["revision"], path=str(saved_path))
                    self.assertFalse(saved["dirty"])
                    self.assertEqual(len(json.loads(saved_path.read_text())["items"]), 3)
                    pdf_path = directory / "agent-output.pdf"
                    exported = await call("verona_editor_export_pdf", editor_id=editor_id,
                                          expected_revision=saved["revision"], path=str(pdf_path))
                    self.assertFalse(exported["printed"])
                    self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF"))
                    opened = await call("verona_editor_open", editor_id=editor_id,
                                        expected_revision=saved["revision"], path=str(source))
                    self.assertEqual(len(opened["tabs"]), 2)
                    self.assertEqual(len(opened["objects"]), 1)
                    self.assertEqual(source.read_bytes(), original)
                    shape = await call("verona_add_shape", editor_id=editor_id,
                        expected_revision=opened["revision"], shape="rectangle", x_mm=40,
                        y_mm=40, width_mm=12, height_mm=8)
                    created = await call("verona_apply_edit", editor_id=editor_id,
                        expected_revision=opened["revision"], plan_id=shape["plan_id"])
                    self.assertEqual(created["objects"][1]["bounds_mm"], [40, 40, 52, 48])
                    ink = await call("verona_set_vector_print", editor_id=editor_id,
                        expected_revision=created["revision"], object_indices=[1], fill="#ff0000")
                    inked = await call("verona_apply_edit", editor_id=editor_id,
                        expected_revision=created["revision"], plan_id=ink["plan_id"])
                    self.assertEqual(inked["objects"][1]["print_style"]["fill"], "#ff0000")
                    created = await call("verona_editor_history", editor_id=editor_id,
                        expected_revision=inked["revision"])
                    align = await call("verona_preview_edit", editor_id=editor_id,
                        expected_revision=created["revision"], action="align", object_indices=[0, 1],
                        parameters={"mode": "left"})
                    self.assertEqual(align["objects"][1]["bounds_mm"][0], 20)
                    text = await call("verona_add_text", editor_id=editor_id,
                        expected_revision=created["revision"], text="Juliet", x_mm=35, y_mm=55,
                        font_size_mm=5)
                    self.assertTrue(text["details"]["resolved_font"])
                    configured = await call("verona_configure_document", editor_id=editor_id,
                        expected_revision=created["revision"], registration={"mark_short_length": 8},
                        cut_settings={"custom": True, "force": 15, "speed": 11, "tool": 1})
                    self.assertTrue(configured["details"]["requires_reprint"])
                    applied = await call("verona_apply_edit", editor_id=editor_id,
                        expected_revision=created["revision"], plan_id=configured["plan_id"])
                    self.assertEqual(applied["settings"]["tool"], 1)
                    restored = await call("verona_editor_history", editor_id=editor_id,
                        expected_revision=applied["revision"])
                    self.assertEqual(restored["settings"], created["settings"])
                    self.assertEqual(source.read_bytes(), original)
                    svg = directory / "import.svg"
                    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" '
                        'viewBox="0 0 20 10"><rect width="20" height="10"/></svg>')
                    imported = await call("verona_import_artwork", editor_id=editor_id,
                        expected_revision=restored["revision"], path=str(svg), x_mm=50, y_mm=50)
                    rendered = await call("verona_import_artwork", editor_id=editor_id,
                        expected_revision=restored["revision"], path=str(svg), representation="raster",
                        dpi=150, tracing={"mode": "background"})
                    self.assertTrue(rendered["details"]["rasterized"])
                    self.assertTrue(rendered["objects"][-1]["image"])
                    self.assertEqual(imported["objects"][-1]["bounds_mm"], [50, 50, 70, 60])
                    applied = await call("verona_apply_edit", editor_id=editor_id,
                        expected_revision=restored["revision"], plan_id=imported["plan_id"])
                    self.assertEqual(len(applied["objects"]), 3)
                    linked = await call("verona_link_cut_paths", editor_id=editor_id,
                        expected_revision=applied["revision"], image_index=0, cut_index=2)
                    self.assertEqual(linked["after_objects"], 2)
                    linked_state = await call("verona_apply_edit", editor_id=editor_id,
                        expected_revision=applied["revision"], plan_id=linked["plan_id"])
                    self.assertEqual(len(linked_state["objects"]), 2)
                    restored = await call("verona_editor_history", editor_id=editor_id,
                        expected_revision=linked_state["revision"])
                    retraced = await call("verona_retrace_image", editor_id=editor_id,
                        expected_revision=restored["revision"], object_index=0,
                        parameters={"mask_mode": "saved", "offset_mm": 1, "brush_strokes": [
                            {"mode": "remove", "width_mm": 2, "points_mm": [[30.5, 25.5]]}]})
                    self.assertFalse(retraced["details"]["original_pixels_changed"])
                    applied = await call("verona_apply_edit", editor_id=editor_id,
                        expected_revision=restored["revision"], plan_id=retraced["plan_id"])
                    self.assertEqual(applied["objects"][0]["trace_options"]["offset_mm"], 1)
                    packed = await call("verona_pack_objects", editor_id=editor_id,
                        expected_revision=applied["revision"], object_indices=[0, 1],
                        copies_per_unit=[1, 1], allow_rotate=True, shape_aware=True)
                    self.assertEqual(packed["details"]["copies"], 2)
                    packed_state = await call("verona_apply_edit", editor_id=editor_id,
                        expected_revision=applied["revision"], plan_id=packed["plan_id"])
                    self.assertEqual(len(packed_state["objects"]), 3)
                    ordered = await call("verona_optimize_paths", editor_id=editor_id,
                        expected_revision=packed_state["revision"])
                    picture = await client.call_tool("verona_editor_image", {
                        "editor_id": editor_id, "plan_id": ordered["plan_id"], "view": "toolpath"})
                    self.assertFalse(picture.is_error)
                    self.assertTrue(any(content.type == "image" for content in picture.content))
                    ordered_state = await call("verona_apply_edit", editor_id=editor_id,
                        expected_revision=packed_state["revision"], plan_id=ordered["plan_id"])
                    self.assertTrue(ordered_state["metadata"]["optimize_paths"])
                    nodes = [{"point": [20, 20], "out": [20, 40]},
                             {"point": [40, 20], "in": [40, 40]}]
                    curve = await call("verona_edit_path", editor_id=editor_id,
                        expected_revision=ordered_state["revision"], object_index=0,
                        contour_index=0, nodes=nodes, closed=False)
                    curved_state = await call("verona_apply_edit", editor_id=editor_id,
                        expected_revision=ordered_state["revision"], plan_id=curve["plan_id"])
                    geometry = await call("verona_editor_state", editor_id=editor_id,
                        object_indices=[0], include_geometry=True)
                    self.assertEqual(geometry["objects"][0]["node_paths_mm"]["0"]["nodes"][0]["out"], [20., 40.])
                    await call("verona_editor_history", editor_id=editor_id,
                        expected_revision=curved_state["revision"])
                    self.assertEqual(source.read_bytes(), original)
            finally:
                if gui.returncode is None:
                    gui.terminate()
                    try:
                        await asyncio.wait_for(gui.wait(), 5)
                    except TimeoutError:
                        gui.kill()
                        await gui.wait()

    async def test_sdk_rejects_boolean_numbers_before_contacting_editor(self):
        from mcp.server.mcpserver.exceptions import ToolError
        from verona.mcp_server import create_server, VeronaController
        server = create_server(VeronaController())
        with patch("verona.mcp_server.editor_request") as request:
            for tool, extra in (("verona_add_bleed", {"width_mm": True, "color": "#ffffff"}),
                                ("verona_fill_area", {"gap_mm": True})):
                with self.subTest(tool=tool), self.assertRaises(ToolError):
                    await server.call_tool(tool, {"editor_id": "123-012345abcdef",
                        "expected_revision": "revision", "object_indices": [0], **extra})
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

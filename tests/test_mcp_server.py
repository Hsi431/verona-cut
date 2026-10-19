import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from verona.mcp_server import VeronaController, create_server

ROOT = Path(__file__).resolve().parents[1]
HAS_MCP = importlib.util.find_spec("mcp") is not None


class McpControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lock = Path(self.temp.name) / "device.lock"
        self.controller = VeronaController("192.0.2.1", lock_path=self.lock)
        self.document = {"format": "verona", "version": 1, "units": "mm",
                         "items": [{"name": "cut", "contours": [[[1, 2], [3, 4]]]},
                                   {"name": "image outline", "cut": False,
                                    "contours": [[[100, 100], [110, 110]]]}]}
        self.path = Path(self.temp.name) / "test.jdesign"
        self.write_document()

    def write_document(self):
        self.path.write_text(json.dumps(self.document))

    def prepare(self, **kwargs):
        return self.controller.prepare_job(str(self.path), **kwargs)

    def test_startup_and_capabilities_do_not_contact_or_enumerate_devices(self):
        with patch("verona.transport.socket.create_connection") as connect, \
                patch("verona.transport.usb_devices") as usb:
            capabilities = VeronaController("192.0.2.1", lock_path=self.lock).capabilities()
        connect.assert_not_called()
        usb.assert_not_called()
        self.assertFalse(capabilities["firmware_update"])
        self.assertFalse(capabilities["raw_camera_image_api"])

    def test_prepare_filters_noncut_objects_and_retains_machine_settings(self):
        with patch("verona.transport.send_tcp") as send:
            job = self.prepare()
        send.assert_not_called()
        self.assertEqual(job["contours"], 1)
        self.assertEqual(job["design_bounds_mm"], [1, 2, 3, 4])
        self.assertIsNone(job["force"])
        self.assertIsNone(job["speed"])
        self.assertEqual(self.controller.jobs[job["job_id"]],
                         b"IN;FSIZE160,120;U SP1;U80,40 D160,120 U0,0 U !PG;")

    def test_prepare_limits_do_not_create_jobs_or_change_the_source(self):
        from verona import input_safety
        original = self.path.read_bytes()
        for limits in ({"MAX_DESIGN_BYTES": 4}, {"MAX_POINTS": 3}, {"MAX_ITEMS": 1}, {"MAX_DEPTH": 1}):
            with patch.multiple(input_safety, **limits), self.assertRaises(ValueError):
                self.prepare()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertFalse(self.controller.jobs)

    def test_saved_settings_and_overrides_are_encoded(self):
        self.document["settings"] = {"custom": True, "force": 15, "speed": 11, "tool": 1}
        self.write_document()
        job = self.prepare(speed=7)
        self.assertEqual((job["force"], job["speed"], job["tool"]), (15, 7, "pen"))
        self.assertIn(b"SP2;!FS15;VS7;", self.controller.jobs[job["job_id"]])

    def test_prepare_summary_uses_the_encoders_selected_contours_and_parameters(self):
        from verona import commands
        from tests.test_editor_actions import rectangle
        self.document["items"] = [rectangle(x=20, y=20, width=60, height=60),
                                  rectangle(x=30, y=30, width=10, height=10),
                                  rectangle(x=90, y=90, cut=False)]
        self.document["settings"] = {"custom": True, "force": 15, "speed": 11, "tool": 1}
        registration = {"enabled": True, "paper_width": 100, "paper_height": 100,
                        "bounds": [6, 6, 94, 94], "offset": [-.1, .2]}
        for enabled in (False, True):
            self.document["metadata"] = {"print_cut": registration, "optimize_paths": enabled}
            self.write_document()
            chosen = self.document["items"][1]["contours"]
            # A future path policy may filter contours; the summary must follow it.
            with self.subTest(enabled=enabled), \
                    patch("verona.path_order.cut_contours", return_value=chosen), \
                    patch.object(commands, "encode_plt", wraps=commands.encode_plt) as encoder:
                job = self.prepare(force=17, speed=7, tool=0)
            contours, = encoder.call_args.args
            parameters = encoder.call_args.kwargs
            self.assertEqual(job["contours"], len(contours))
            self.assertEqual(job["points"], sum(map(len, contours)))
            self.assertEqual(job["design_bounds_mm"], [30, 30, 40, 40])
            self.assertEqual((job["force"], job["speed"]), (parameters["force"], parameters["speed"]))
            self.assertEqual(job["tool"], "blade" if parameters["tool"] == 0 else "pen")
            self.assertEqual(job["registration_bounds_mm"], parameters["registration_bounds"])
            self.assertEqual(job["registration_offset_mm"], list(parameters["registration_offset"]))
            self.assertEqual(self.controller.jobs[job["job_id"]], commands.encode_plt(chosen,
                force=17, speed=7, tool=0, registration_bounds=registration["bounds"],
                registration_offset=registration["offset"]))

    def test_print_cut_uses_established_offsets_and_corrected_footer(self):
        # Keep this protocol fixture self-contained in the release tests.
        data = copy.deepcopy(self.document)
        data["items"] = [{"name": "rectangle", "contours": [
            [[25.75, 25.75], [45.75, 25.75], [45.75, 35.75], [25.75, 35.75], [25.75, 25.75]]]}]
        data["settings"] = {"custom": True, "force": 15, "speed": 11}
        data["metadata"] = {"print_cut": {"enabled": True, "bounds": [5.75, 5.75, 65.75, 65.75],
                            "paper_width": 210, "paper_height": 297,
                            "mark_stroke": 1, "mark_short_length": 8, "offset": [-0.1, 0.1]}}
        self.document = data
        self.write_document()
        job = self.prepare()
        payload = self.controller.jobs[job["job_id"]]
        self.assertEqual(payload, b"IN;PA;BD:103,0;IN;TB26,2400,2400;CT1;IN;PU;U SP1;!FS15;VS11;"
                                  b"U1596,796 D1596,1596 D1196,1596 D1196,796 D1596,796 U1596,0 U !PG;!PG;")
        self.assertEqual(job["registration_offset_mm"], [-0.1, 0.1])
        self.assertTrue(job["print_cut"])

    def test_prepare_rejects_invalid_units_coordinates_and_settings(self):
        for mutate in (lambda d: d.update(units="in"),
                       lambda d: d["items"][0]["contours"][0][0].__setitem__(0, float("nan")),
                       lambda d: d.update(settings={"custom": True, "force": 0})):
            original = copy.deepcopy(self.document)
            mutate(self.document)
            self.write_document()
            with self.assertRaises(ValueError):
                self.prepare()
            self.document = original
        self.assertFalse(self.controller.jobs)

    def test_readiness_and_hash_failures_preserve_job_without_send(self):
        job = self.prepare()
        with patch("verona.transport.send_tcp") as send:
            for ready in (False, "true", 1):
                with self.assertRaises(ValueError):
                    self.controller.send_job(job["job_id"], job["sha256"], ready)
            with self.assertRaises(ValueError):
                self.controller.send_job(job["job_id"], "0" * 64, True)
        send.assert_not_called()
        self.assertIn(job["job_id"], self.controller.jobs)

    def test_snapshot_survives_source_changes_and_is_delivered_only_once(self):
        job = self.prepare()
        payload = self.controller.jobs[job["job_id"]]
        self.path.write_text("changed after review")
        with patch("verona.transport.send_tcp", return_value=len(payload)) as send:
            result = self.controller.send_job(job["job_id"], job["sha256"], True)
            with self.assertRaises(ValueError):
                self.controller.send_job(job["job_id"], job["sha256"], True)
        self.assertEqual(result["status"], "transport_delivered")
        self.assertEqual(result["physical_completion"], "unknown")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args, (payload, "192.0.2.1", 8080))

    def test_partial_failure_reports_progress_and_consumes_job(self):
        job = self.prepare()

        def failing(_payload, _host, _port, *, on_progress):
            on_progress(11, job["bytes"])
            raise OSError("simulated disconnect")

        with patch("verona.transport.send_tcp", side_effect=failing) as send:
            result = self.controller.send_job(job["job_id"], job["sha256"], True)
            with self.assertRaises(ValueError):
                self.controller.send_job(job["job_id"], job["sha256"], True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["bytes_sent"], 11)
        self.assertFalse(result["automatic_retry"])
        self.assertTrue(result["delivery_may_be_partial"])
        send.assert_called_once()

    def test_device_lock_blocks_another_controller_without_consuming_job(self):
        job = self.prepare()
        other = VeronaController("192.0.2.2", lock_path=self.lock)
        with other.device_lock(), patch("verona.transport.send_tcp") as send:
            with self.assertRaisesRegex(RuntimeError, "Another Verona"):
                self.controller.send_job(job["job_id"], job["sha256"], True)
        send.assert_not_called()
        self.assertIn(job["job_id"], self.controller.jobs)

    def test_query_uses_only_configured_endpoint(self):
        with patch("verona.transport.query_tcp_version", return_value="V22.0613") as query:
            self.assertEqual(self.controller.query_version()["software_version"], "V22.0613")
        query.assert_called_once_with("192.0.2.1", 8080)
        with self.assertRaises(ValueError):
            VeronaController(lock_path=self.lock).query_version()

    def test_usb_requires_exact_selection_and_failure_still_consumes_job(self):
        device = SimpleNamespace(bus=1, address=5, idVendor=0x0483, idProduct=0x5750)
        job = self.prepare()
        with patch("verona.transport.usb_devices", return_value=[device]), \
                patch("verona.transport.send_usb", side_effect=OSError("USB timeout")) as send:
            with self.assertRaises(ValueError):
                self.controller.send_job(job["job_id"], job["sha256"], True, "usb", 1, 6)
            result = self.controller.send_job(job["job_id"], job["sha256"], True, "usb", 1, 5)
        send.assert_called_once()
        self.assertIsNone(result["bytes_sent"])
        self.assertTrue(result["job_consumed"])


@unittest.skipUnless(HAS_MCP, "optional MCP SDK is not installed")
class McpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_rejects_coerced_readiness_and_boolean_force(self):
        from mcp.server.mcpserver.exceptions import ToolError
        with tempfile.TemporaryDirectory() as tmp:
            server = create_server(VeronaController(lock_path=Path(tmp) / "lock"))
            for ready in ("true", 1):
                with self.assertRaises(ToolError):
                    await server.call_tool("verona_send_job", {
                        "job_id": "invalid", "sha256": "invalid", "machine_ready": ready})
            with self.assertRaises(ToolError):
                await server.call_tool("verona_prepare_job", {
                    "design_path": str(ROOT / "examples/raster-contour-demo.jdesign"), "force": True})

    async def test_stdio_handshake_tools_annotations_and_offline_preparation(self):
        from mcp import Client, StdioServerParameters
        parameters = StdioServerParameters(command=sys.executable,
            args=["-m", "verona.mcp_server"], cwd=ROOT,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        async with Client(parameters, read_timeout_seconds=10) as client:
            tools = await client.list_tools()
            tools = {tool.name: tool for tool in tools.tools}
            self.assertEqual(len(tools), 29)
            self.assertTrue(tools["verona_editor_state"].annotations.read_only_hint)
            self.assertFalse(tools["verona_apply_edit"].annotations.destructive_hint)
            self.assertIn("verona_fill_area", tools)
            self.assertIn("verona_add_bleed", tools)
            self.assertTrue(tools["verona_send_job"].annotations.destructive_hint)
            self.assertFalse(tools["verona_send_job"].annotations.idempotent_hint)
            capabilities = await client.call_tool("verona_capabilities", {})
            self.assertFalse(capabilities.is_error)
            self.assertIsNone(capabilities.structured_content["tcp_endpoint"]["host"])
            job = await client.call_tool("verona_prepare_job", {
                "design_path": str(ROOT / "examples/raster-contour-demo.jdesign")})
            self.assertFalse(job.is_error)
            self.assertFalse(job.structured_content["hardware_operations"])
            rejected = await client.call_tool("verona_send_job", {
                "job_id": job.structured_content["job_id"],
                "sha256": job.structured_content["sha256"], "machine_ready": False})
            self.assertTrue(rejected.is_error)
            self.assertIn("loaded", str(rejected.content))

    async def test_sdk_execution_retains_expected_errors_and_no_transport(self):
        from mcp.server.mcpserver.exceptions import ToolError
        with tempfile.TemporaryDirectory() as tmp:
            controller = VeronaController(lock_path=Path(tmp) / "lock")
            server = create_server(controller)
            with patch("verona.transport.send_tcp") as send:
                with self.assertRaisesRegex(ToolError, "loaded"):
                    await server.call_tool("verona_send_job", {
                        "job_id": "invalid", "sha256": "invalid", "machine_ready": False})
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()

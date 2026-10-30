import errno
import fcntl
import io
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from verona import firmware, firmware_cli, firmware_usb
from tests.test_firmware import Clock, FakePeer, fixture


class UsbConnectionTests(unittest.TestCase):
    def test_invalid_node_rejected_before_open(self):
        for node in ("/dev/lp0", "/dev/usb/lpX", "/tmp/lp0"):
            with self.subTest(node=node), patch.object(firmware_usb.os, "open") as open_device:
                with self.assertRaises(ValueError):
                    firmware_usb.UsbConnection(node, 1)
                open_device.assert_not_called()

    def test_unsupported_sysfs_identity_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / "usb1" / "1-1"
            interface = device / "1-1:1.0"
            interface.mkdir(parents=True)
            (device / "idVendor").write_text("1234\n")
            (device / "idProduct").write_text("5750\n")
            (interface / "bInterfaceClass").write_text("07\n")
            sysfs_device = root / "lp0" / "device"
            sysfs_device.parent.mkdir()
            sysfs_device.symlink_to(interface)

            def redirected_path(value):
                value = str(value)
                if value.startswith("/sys/class/usbmisc"):
                    value = value.replace("/sys/class/usbmisc", str(root), 1)
                return Path(value)

            with patch.object(firmware_usb, "Path", side_effect=redirected_path), \
                    patch.object(firmware_usb.os, "open") as open_device:
                with self.assertRaisesRegex(ValueError, "not the supported"):
                    firmware_usb.UsbConnection("/dev/usb/lp0", 1)
            open_device.assert_not_called()

    def test_pending_data_closes_device_without_writing(self):
        with patch.object(firmware_usb, "_usb_interface"), \
                patch.object(firmware_usb.os, "open", return_value=19) as open_device, \
                patch.object(firmware_usb.os, "fstat", return_value=type("Stat", (), {"st_mode": stat.S_IFCHR})()), \
                patch.object(firmware_usb.os, "read", return_value=b"stale"), \
                patch.object(firmware_usb.os, "write") as write_device, \
                patch.object(firmware_usb.os, "close") as close_device:
            with self.assertRaisesRegex(ValueError, "Pending USB data"):
                firmware_usb.UsbConnection("/dev/usb/lp0", 1)
        open_device.assert_called_once_with("/dev/usb/lp0", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        close_device.assert_called_once_with(19)
        write_device.assert_not_called()

    def test_nonblocking_partial_read_write_and_timeout(self):
        connection = firmware_usb.UsbConnection.__new__(firmware_usb.UsbConnection)
        connection.fd = 41
        connection.settimeout(0)
        with patch.object(firmware_usb.select, "select", return_value=([], [], [])), \
                self.assertRaises(BlockingIOError) as caught:
            connection.recv(8)
        self.assertEqual(caught.exception.errno, errno.EAGAIN)

        with patch.object(firmware_usb.select, "select", return_value=([41], [], [])), \
                patch.object(firmware_usb.os, "read", side_effect=[b"re", b"ply"]):
            self.assertEqual(connection.recv(2), b"re")
            self.assertEqual(connection.recv(8), b"ply")

        connection.settimeout(0.01)
        with patch.object(firmware_usb.select, "select", return_value=([], [], [])), \
                self.assertRaises(socket.timeout):
            connection.recv(1)
        connection.settimeout(0)
        with patch.object(firmware_usb.select, "select", return_value=([], [41], [])), \
                patch.object(firmware_usb.os, "write", return_value=2) as write_device:
            self.assertEqual(connection.send(b"abcdef"), 2)
        write_device.assert_called_once_with(41, b"abcdef")
        with patch.object(firmware_usb.os, "close") as close_device:
            connection.close()
            connection.close()
        self.assertIsNone(connection.fd)
        close_device.assert_called_once_with(41)

    def test_list_nodes_filters_sysfs_without_opening_devices(self):
        nodes = [Path("/dev/usb/lp0"), Path("/dev/usb/lp1"), Path("/dev/usb/lp2")]

        def validate(node):
            if node.name == "lp1":
                raise ValueError("unsupported device IDs")
            if node.name == "lp2":
                raise OSError("missing sysfs metadata")
            return Path("/sys/devices/usb1/1-1/1-1:1.0")

        with patch.object(firmware_usb.Path, "glob", return_value=nodes), \
                patch.object(firmware_usb, "_usb_interface", side_effect=validate) as inspect_sysfs, \
                patch.object(firmware_usb.os, "open") as open_device:
            self.assertEqual(firmware_usb.list_usb_nodes(), ["/dev/usb/lp0"])
        self.assertEqual(inspect_sysfs.call_count, 3)
        open_device.assert_not_called()


class UsbCoreTests(unittest.TestCase):
    def setUp(self):
        self.image = firmware.inspect_firmware(fixture(b"synthetic firmware"))
        self.clock = Clock()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lock = Path(self.temp.name) / "device.lock"
        self.peer = FakePeer(self.image, self.clock)
        self.usb = patch.object(firmware, "UsbConnection", return_value=self.peer)
        self.usb_mock = self.usb.start()
        self.addCleanup(self.usb.stop)
        self.network = patch.object(firmware.socket, "create_connection")
        self.network_mock = self.network.start()
        self.addCleanup(self.network.stop)
        self.timer = patch.object(firmware.time, "monotonic", self.clock)
        self.timer.start()
        self.addCleanup(self.timer.stop)

    def test_usb_identity_and_update_use_protocol_without_tcp(self):
        identity = firmware.query_identity(usb_node="/dev/usb/lp0", lock_path=self.lock)
        self.assertEqual(identity, self.peer.identity)
        self.assertEqual([kind for _, kind, _ in self.peer.writes], ["identity"] * 3)
        self.assertEqual(self.peer.commands, b"")

        self.clock.now = 0
        self.peer = FakePeer(self.image, self.clock)
        self.usb_mock.return_value = self.peer
        result = firmware.update_firmware(
            self.image, expected_sha256=self.image.sha256, expected_mode="SS12",
            expected_hver="V7.1202_4300", expected_sver="V22.0613",
            usb_node="/dev/usb/lp0", lock_path=self.lock)
        self.assertEqual(result["status"], "pgok_received")
        self.assertEqual(self.peer.header, self.image.header)
        self.assertEqual(self.peer.body, self.image.body)
        self.network_mock.assert_not_called()
        self.usb_mock.assert_called_with("/dev/usb/lp0", unittest.mock.ANY)

    def test_usb_verify_sends_only_sver(self):
        self.peer = FakePeer(self.image, self.clock, version_only=True)
        self.usb_mock.return_value = self.peer
        result = firmware.verify_firmware_version(None, "V22.0613", usb_node="/dev/usb/lp0",
                                                  lock_path=self.lock)
        self.assertTrue(result["version_verified"])
        self.assertEqual([data for _, _, data in self.peer.writes], [b";RSVER;"])
        self.assertEqual(self.peer.header, b"")
        self.network_mock.assert_not_called()

    def test_lock_and_hash_fail_before_usb_open(self):
        with self.assertRaises(ValueError):
            firmware.update_firmware(
                self.image, expected_sha256="0" * 64, expected_mode="SS12",
                expected_hver="V7.1202_4300", expected_sver="V22.0613",
                usb_node="/dev/usb/lp0", lock_path=self.lock)
        self.usb_mock.assert_not_called()
        with self.lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(firmware.FirmwareUpdateError):
                firmware.query_identity(usb_node="/dev/usb/lp0", lock_path=self.lock)
        self.usb_mock.assert_not_called()

    def test_host_and_usb_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "either Wi-Fi or USB"):
            firmware.query_identity("192.0.2.1", usb_node="/dev/usb/lp0", lock_path=self.lock)
        self.usb_mock.assert_not_called()
        self.network_mock.assert_not_called()

    def test_local_socketpair_usb_roundtrip_sends_exact_header_and_body(self):
        try:
            sender, server = socket.socketpair()
        except PermissionError as error:
            self.skipTest(f"sandbox denied local socketpair: {error}")
        self.addCleanup(server.close)

        server_errors = []

        def recv_exact(count):
            chunks = bytearray()
            while len(chunks) < count:
                block = server.recv(count - len(chunks))
                if not block:
                    raise AssertionError("USB peer closed before expected bytes arrived")
                chunks.extend(block)
            return bytes(chunks)

        def emulate_cutter():
            try:
                for command, key, value in ((b";RMODE;", "MODE", "SS12"),
                                            (b";RHVER;", "HVER", "V7.1202_4300"),
                                            (b";RSVER;", "SVER", "V22.0613")):
                    self.assertEqual(recv_exact(len(command)), command)
                    server.sendall(f"{key}={value};".encode("ascii"))
                self.assertEqual(recv_exact(len(self.image.header)), self.image.header)
                server.sendall(b"PGRE")
                server.sendall(b"ADY;")
                self.assertEqual(recv_exact(len(self.image.body)), self.image.body)
                server.sendall(b"PGOK;")
            except BaseException as error:
                server_errors.append(error)

        thread = threading.Thread(target=emulate_cutter, daemon=True)
        thread.start()
        sender.setblocking(False)
        client_fd = sender.detach()

        def make_usb_connection(_node, timeout):
            connection = firmware_usb.UsbConnection.__new__(firmware_usb.UsbConnection)
            connection.fd = client_fd
            connection.settimeout(timeout)
            return connection

        with patch.object(firmware, "UsbConnection", side_effect=make_usb_connection) as usb_factory, \
                patch.object(firmware.socket, "create_connection") as tcp_connect, \
                patch.object(firmware, "BODY_DELAY", 0):
            try:
                result = firmware.update_firmware(
                    self.image, expected_sha256=self.image.sha256, expected_mode="SS12",
                    expected_hver="V7.1202_4300", expected_sver="V22.0613",
                    usb_node="/dev/usb/lp0", lock_path=self.lock, total_timeout=10)
            except BaseException:
                thread.join(timeout=2)
                if server_errors:
                    if isinstance(server_errors[0], PermissionError):
                        self.skipTest(f"sandbox denied local socketpair I/O: {server_errors[0]}")
                    raise server_errors[0]
                raise

        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "local cutter emulator did not finish")
        if server_errors:
            raise server_errors[0]
        self.assertEqual(result["status"], "pgok_received")
        usb_factory.assert_called_once()
        tcp_connect.assert_not_called()


class UsbCliTests(unittest.TestCase):
    def test_status_and_verify_require_exactly_one_endpoint(self):
        for command in ("status", "verify"):
            args = [command]
            if command == "verify":
                args += ["--target-version", "V26.0605"]
            with redirect_stderr(io.StringIO()):
                with self.subTest(command=command), self.assertRaises(SystemExit):
                    firmware_cli._parser().parse_args(args)
                with self.subTest(command=command, both=True), self.assertRaises(SystemExit):
                    firmware_cli._parser().parse_args(args + ["--host", "192.0.2.1", "--usb-node", "/dev/usb/lp0"])


if __name__ == "__main__":
    unittest.main()

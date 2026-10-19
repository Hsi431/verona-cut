import socket
import threading
import time
import unittest
from unittest.mock import patch

from verona.transport import TCP_PORT, query_tcp_version, send_tcp, send_usb


class TcpTests(unittest.TestCase):
    def setUp(self):
        pacing = patch("verona.transport.TCP_CHUNK_DELAY", 0)
        pacing.start()
        self.addCleanup(pacing.stop)

    def test_version_query_sends_exact_request_and_reads_segmented_reply(self):
        class Connection:
            def __init__(self):
                self.sent = []
                self.chunks = iter((b"SVER=V22.", b"0613;"))
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def sendall(self, data): self.sent.append(data)
            def settimeout(self, _timeout): pass
            def recv(self, _size): return next(self.chunks)

        connection = Connection()
        with patch("verona.transport.socket.create_connection", return_value=connection) as connect:
            self.assertEqual(query_tcp_version("192.0.2.3"), "V22.0613")
        self.assertEqual(connection.sent, [b";RSVER;"])
        connect.assert_called_once_with(("192.0.2.3", TCP_PORT), timeout=2.0)

    def test_version_query_rejects_malformed_and_empty_replies(self):
        class Connection:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def sendall(self, _data): pass
            def settimeout(self, _timeout): pass
            def recv(self, size):
                data, self.data = self.data, b""
                return data[:size]
        for reply in (b"", b"SVER=V1", b"BAD=V1;", b"SVER=V1;extra", b"SVER=\xff;"):
            with self.subTest(reply=reply), patch(
                    "verona.transport.socket.create_connection",
                    return_value=Connection(reply)):
                with self.assertRaises(ValueError):
                    query_tcp_version("192.0.2.3")

    def test_version_query_timeout_does_not_retry(self):
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def sendall(self, _data): pass
            def settimeout(self, _timeout): pass
            def recv(self, _size): raise socket.timeout()
        with patch("verona.transport.socket.create_connection", return_value=Connection()) as connect:
            with self.assertRaises(TimeoutError):
                query_tcp_version("192.0.2.3", timeout=0.1)
            connect.assert_called_once()

    def test_local_socket_delivery_preserves_every_byte(self):
        payload = b"IN;FSIZE400,800;U U0,0 D400,800 U0,0 U !PG;" * 200
        sender, receiver = socket.socketpair()
        receiver.settimeout(2)
        try:
            with patch("verona.transport.socket.create_connection", return_value=sender) as connect:
                self.assertEqual(send_tcp(payload, "127.0.0.1", 12345), len(payload))
                connect.assert_called_once_with(("127.0.0.1", 12345), timeout=2.0)
            received = bytearray()
            while len(received) < len(payload):
                received.extend(receiver.recv(len(payload) - len(received)))
            self.assertEqual(received, payload)
        finally:
            sender.close()
            receiver.close()

    def test_no_automatic_retry_after_write_failure(self):
        with patch("verona.transport.socket.create_connection") as connect:
            connect.return_value.__enter__.return_value.send.side_effect = OSError("short connection")
            with self.assertRaises(OSError):
                send_tcp(b"IN;", "192.0.2.1")
            self.assertEqual(connect.call_count, 1)

    def test_short_socket_writes_continue_only_from_accepted_offset(self):
        class Connection:
            def __init__(self): self.data, self.sizes, self.timeouts = bytearray(), [], []
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def settimeout(self, value): self.timeouts.append(value)
            def send(self, data):
                self.sizes.append(len(data))
                count = min(127, len(data))
                self.data.extend(data[:count])
                return count
        connection = Connection()
        payload = bytes(range(256)) * 3000
        with patch("verona.transport.socket.create_connection", return_value=connection) as connect:
            self.assertEqual(send_tcp(payload, "192.0.2.1"), len(payload))
        self.assertEqual(connection.data, payload)
        self.assertLessEqual(max(connection.sizes), 1024)
        self.assertTrue(all(value == 60 for value in connection.timeouts))
        connect.assert_called_once()

    def test_sdk_pacing_and_progress_follow_each_accepted_write(self):
        with patch("verona.transport.socket.create_connection") as connect, \
                patch("verona.transport.TCP_CHUNK_DELAY", .3), \
                patch("verona.transport.time.sleep") as sleep:
            connection = connect.return_value.__enter__.return_value
            connection.send.side_effect = [1024, 476]
            progress = []
            self.assertEqual(send_tcp(b"X" * 1500, "192.0.2.1",
                                      on_progress=lambda sent, total: progress.append((sent, total))), 1500)
            self.assertEqual(progress, [(1024, 1500), (1500, 1500)])
            self.assertEqual(sleep.call_count, 2)
            sleep.assert_called_with(.3)

    def test_partial_timeout_reports_count_without_reconnecting(self):
        with patch("verona.transport.socket.create_connection") as connect:
            connection = connect.return_value.__enter__.return_value
            connection.send.side_effect = [127, 127, socket.timeout("receiver stalled")]
            with self.assertRaises(TimeoutError) as caught:
                send_tcp(b"X" * 5000, "192.0.2.1")
            self.assertEqual(caught.exception.bytes_sent, 254)
            self.assertEqual(caught.exception.bytes_total, 5000)
            self.assertIn("254/5000", str(caught.exception))
            connect.assert_called_once()
            self.assertEqual(connection.send.call_count, 3)

    def test_full_transfer_has_a_total_deadline(self):
        with patch("verona.transport.socket.create_connection") as connect, \
                patch("verona.transport.time.monotonic", side_effect=[0, 0, 301]):
            connection = connect.return_value.__enter__.return_value
            connection.send.return_value = 1024
            with self.assertRaises(TimeoutError) as caught:
                send_tcp(b"X" * 5000, "192.0.2.1")
            self.assertEqual(caught.exception.bytes_sent, 1024)
            self.assertEqual(connection.send.call_count, 1)

    def test_two_megabyte_job_has_enough_budget_for_pacing_and_short_writes(self):
        payload = b"X" * 2_000_000
        for max_write in (1024, 127):
            with self.subTest(max_write=max_write):
                clock = [0.0]

                class Connection:
                    def __init__(self): self.data = bytearray()
                    def __enter__(self): return self
                    def __exit__(self, *_args): pass
                    def settimeout(self, _timeout): pass
                    def send(self, data):
                        accepted = bytes(data[:max_write])
                        self.data.extend(accepted)
                        return len(accepted)

                def sleep(delay):
                    clock[0] += delay

                connection = Connection()
                with patch("verona.transport.socket.create_connection", return_value=connection) as connect, \
                        patch("verona.transport.TCP_CHUNK_DELAY", .3), \
                        patch("verona.transport.time.monotonic", side_effect=lambda: clock[0]), \
                        patch("verona.transport.time.sleep", side_effect=sleep):
                    self.assertEqual(send_tcp(payload, "192.0.2.1"), len(payload))
                self.assertEqual(connection.data, payload)
                self.assertGreater(clock[0], 300)
                connect.assert_called_once()

    def test_large_job_survives_receiver_pause_beyond_connect_timeout(self):
        payload = bytes(range(256)) * 2100
        sender, receiver = socket.socketpair()
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        receiver.settimeout(5)
        received, failures = bytearray(), []

        def consume():
            try:
                time.sleep(2.1)
                while True:
                    chunk = receiver.recv(4096)
                    if not chunk: break
                    received.extend(chunk)
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        try:
            with patch("verona.transport.socket.create_connection", return_value=sender) as connect:
                self.assertEqual(send_tcp(payload, "127.0.0.1", write_timeout=4), len(payload))
                connect.assert_called_once_with(("127.0.0.1", TCP_PORT), timeout=2.0)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures)
            self.assertEqual(received, payload)
        finally:
            sender.close()
            receiver.close()
            thread.join(5)

    def test_invalid_addresses_do_not_connect(self):
        with patch("verona.transport.socket.create_connection") as connect:
            for host in ("not-a-host", "0.0.0.0", "224.0.0.1", "255.255.255.255"):
                with self.subTest(host=host), self.assertRaises(ValueError):
                    send_tcp(b"IN;", host)
            connect.assert_not_called()


class Endpoint:
    bmAttributes = 2

    def __init__(self, address):
        self.bEndpointAddress = address
        self.data = bytearray()

    def write(self, chunk, timeout):
        count = min(127, len(chunk))  # exercise short-transfer continuation
        self.data.extend(chunk[:count])
        return count


class Interface(list):
    bInterfaceClass = 7
    bInterfaceNumber = 2
    bAlternateSetting = 0


class Device:
    idVendor = 0x0483
    idProduct = 0x5750

    def __init__(self, interfaces):
        self.interfaces = interfaces

    def get_active_configuration(self):
        return self.interfaces


class UsbTests(unittest.TestCase):
    def test_printer_class_accepts_two_endpoints(self):
        outgoing = Endpoint(1)
        device = Device([Interface([Endpoint(0x81), outgoing])])
        with patch("usb.util.claim_interface"), patch("usb.util.release_interface"), patch("usb.util.dispose_resources"):
            self.assertEqual(send_usb(b"IN;", device), 3)
        self.assertEqual(outgoing.data, b"IN;")

    def test_seven_endpoints_do_not_replace_printer_class_check(self):
        outgoing = Endpoint(1)
        interface = Interface([Endpoint(0x81), outgoing, Endpoint(0x82), Endpoint(2),
                               Endpoint(0x83), Endpoint(3), Endpoint(0x84)])
        interface.bInterfaceClass = 0xff
        with self.assertRaises(ValueError):
            send_usb(b"IN;", Device([interface]))
        self.assertEqual(outgoing.data, b"")

    def test_endpoint_selection_short_writes_and_cleanup(self):
        first, last = Endpoint(1), Endpoint(3)
        interface = Interface([Endpoint(0x81), first, Endpoint(0x82), last])
        device = Device([interface])
        payload = b"IN;" * 900
        with patch("usb.util.claim_interface") as claim, patch("usb.util.release_interface") as release, patch("usb.util.dispose_resources") as dispose:
            self.assertEqual(send_usb(payload, device), len(payload))
            claim.assert_called_once_with(device, 2)
            release.assert_called_once_with(device, 2)
            dispose.assert_called_once_with(device)
        self.assertEqual(last.data, payload)
        self.assertEqual(first.data, b"")

    def test_ambiguous_interfaces_never_write(self):
        endpoint = Endpoint(3)
        interface = Interface([Endpoint(0x81), endpoint])
        with self.assertRaises(ValueError):
            send_usb(b"IN;", Device([interface, interface]))
        self.assertEqual(endpoint.data, b"")

    def test_failure_releases_interface_without_retry(self):
        outgoing = Endpoint(3)
        outgoing.write = lambda *args, **kwargs: 0
        device = Device([Interface([Endpoint(0x81), outgoing])])
        with patch("usb.util.claim_interface"), patch("usb.util.release_interface") as release, patch("usb.util.dispose_resources") as dispose:
            with self.assertRaises(OSError):
                send_usb(b"IN;", device)
            release.assert_called_once_with(device, 2)
            dispose.assert_called_once_with(device)


if __name__ == "__main__":
    unittest.main()

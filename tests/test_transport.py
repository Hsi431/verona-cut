import socket
import select
import struct
import threading
import time
import unittest
from unittest.mock import patch

from verona.transport import (TCP_PORT, DeviceInterrupted, TransferCancelled,
                              query_tcp_version, send_tcp, send_usb)


class TcpTests(unittest.TestCase):
    def setUp(self):
        pacing = patch("verona.transport.TCP_CHUNK_DELAY", 0)
        pacing.start()
        self.addCleanup(pacing.stop)

    @staticmethod
    def _ready_select(readers, writers, _errors, _timeout):
        return [], list(writers), []

    def _patch_fake_select(self):
        return patch("verona.transport.select.select", side_effect=self._ready_select)

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
            with self._patch_fake_select(), self.assertRaises(OSError):
                send_tcp(b"IN;", "192.0.2.1")
            self.assertEqual(connect.call_count, 1)

    def test_short_socket_writes_continue_only_from_accepted_offset(self):
        class Connection:
            def __init__(self): self.data, self.sizes, self.timeouts, self.linger = bytearray(), [], [], []
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def settimeout(self, value): self.timeouts.append(value)
            def setblocking(self, _value): pass
            def setsockopt(self, *args): self.linger.append(args)
            def send(self, data):
                self.sizes.append(len(data))
                count = min(127, len(data))
                self.data.extend(data[:count])
                return count
        connection = Connection()
        payload = bytes(range(256)) * 3000
        with patch("verona.transport.socket.create_connection", return_value=connection) as connect, \
                self._patch_fake_select():
            self.assertEqual(send_tcp(payload, "192.0.2.1"), len(payload))
        self.assertEqual(connection.data, payload)
        self.assertLessEqual(max(connection.sizes), 1024)
        self.assertTrue(all(value == 60 for value in connection.timeouts))
        self.assertEqual(connection.linger, [])
        connect.assert_called_once()

    def test_cancel_aborts_local_socket_queue_and_does_not_send_more(self):
        cancel = threading.Event()
        with patch("verona.transport.socket.create_connection") as connect, \
                patch("verona.transport.TCP_CHUNK_DELAY", .3), \
                self._patch_fake_select():
            connection = connect.return_value.__enter__.return_value

            def accept_then_cancel(data):
                cancel.set()
                return min(1024, len(data))

            connection.send.side_effect = accept_then_cancel
            with self.assertRaises(TransferCancelled) as caught:
                send_tcp(b"C" * 4096, "192.0.2.1", cancel_event=cancel)
            self.assertEqual((caught.exception.bytes_sent, caught.exception.bytes_total),
                             (1024, 4096))
            self.assertEqual(connection.send.call_count, 1)
            connection.setsockopt.assert_called_once_with(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

    def test_linger_failure_does_not_mask_original_write_error(self):
        with patch("verona.transport.socket.create_connection") as connect, \
                self._patch_fake_select():
            connection = connect.return_value.__enter__.return_value
            connection.send.side_effect = OSError("original write error")
            connection.setsockopt.side_effect = PermissionError("linger setup failed")
            with self.assertRaises(OSError) as caught:
                send_tcp(b"ERR;", "192.0.2.1")
            self.assertIn("original write error", str(caught.exception))
            self.assertNotIn("linger setup failed", str(caught.exception))

    def test_sdk_pacing_and_progress_follow_each_accepted_write(self):
        clock = [0.0]
        def select(readers, writers, _errors, timeout):
            if writers:
                return [], list(writers), []
            clock[0] += timeout
            return [], [], []
        with patch("verona.transport.socket.create_connection") as connect, \
                patch("verona.transport.TCP_CHUNK_DELAY", .3), \
                patch("verona.transport.time.monotonic", side_effect=lambda: clock[0]), \
                patch("verona.transport.select.select", side_effect=select):
            connection = connect.return_value.__enter__.return_value
            connection.send.side_effect = [1024, 476]
            progress = []
            self.assertEqual(send_tcp(b"X" * 1500, "192.0.2.1",
                                      on_progress=lambda sent, total: progress.append((sent, total))), 1500)
            self.assertEqual(progress, [(1024, 1500), (1500, 1500)])
            self.assertAlmostEqual(clock[0], .6)

    def test_partial_timeout_reports_count_without_reconnecting(self):
        with patch("verona.transport.socket.create_connection") as connect, \
                self._patch_fake_select():
            connection = connect.return_value.__enter__.return_value
            connection.send.side_effect = [127, 127, socket.timeout("receiver stalled")]
            with self.assertRaises(TimeoutError) as caught:
                send_tcp(b"X" * 5000, "192.0.2.1")
            self.assertEqual(caught.exception.bytes_sent, 254)
            self.assertEqual(caught.exception.bytes_total, 5000)
            self.assertIn("254/5000", str(caught.exception))
            connect.assert_called_once()
            self.assertEqual(connection.send.call_count, 3)

    def test_blocked_nonblocking_send_cannot_continue_after_write_deadline(self):
        clock = [0.0]

        def select_always_writable(_readers, writers, _errors, timeout):
            clock[0] += timeout
            return [], list(writers), []

        with patch("verona.transport.socket.create_connection") as connect, \
                patch("verona.transport.time.monotonic", side_effect=lambda: clock[0]), \
                patch("verona.transport.select.select", side_effect=select_always_writable):
            connection = connect.return_value.__enter__.return_value
            connection.send.side_effect = BlockingIOError()
            with self.assertRaises(TimeoutError) as caught:
                send_tcp(b"X" * 2048, "192.0.2.1", write_timeout=.12)
            self.assertEqual(caught.exception.bytes_sent, 0)
            self.assertEqual(caught.exception.bytes_total, 2048)
            self.assertEqual(connection.send.call_count, 2)
            connect.assert_called_once()

    def test_cancel_during_paced_wait_stops_after_accepted_chunk(self):
        payload = b"P" * 4096
        cancel = threading.Event()
        paced_wait = threading.Event()
        sender, receiver = socket.socketpair()
        received = bytearray()

        def cancel_after_chunk():
            received.extend(receiver.recv(1024))
            paced_wait.wait(1)
            cancel.set()

        original_select = select.select
        def signal_paced_wait(readers, writers, errors, timeout):
            if not writers:
                paced_wait.set()
            return original_select(readers, writers, errors, timeout)

        thread = threading.Thread(target=cancel_after_chunk, daemon=True)
        thread.start()
        try:
            with patch("verona.transport.socket.create_connection", return_value=sender), \
                    patch("verona.transport.TCP_CHUNK_DELAY", .5), \
                    patch("verona.transport.select.select", side_effect=signal_paced_wait):
                with self.assertRaises(TransferCancelled) as caught:
                    send_tcp(payload, "127.0.0.1", cancel_event=cancel)
            self.assertEqual((caught.exception.bytes_sent, caught.exception.bytes_total),
                             (1024, len(payload)))
            self.assertEqual(received, payload[:1024])
            self.assertTrue(paced_wait.is_set())
            thread.join(1)
            self.assertFalse(thread.is_alive())
        finally:
            sender.close()
            receiver.close()

    def test_peer_fin_during_pacing_stops_without_replay(self):
        payload = b"F" * 4096
        sender, receiver = socket.socketpair()
        received = bytearray()

        def close_after_chunk():
            received.extend(receiver.recv(1024))
            receiver.close()

        thread = threading.Thread(target=close_after_chunk, daemon=True)
        thread.start()
        try:
            with patch("verona.transport.socket.create_connection", return_value=sender), \
                    patch("verona.transport.TCP_CHUNK_DELAY", .5):
                with self.assertRaises(DeviceInterrupted) as caught:
                    send_tcp(payload, "127.0.0.1")
            self.assertEqual((caught.exception.bytes_sent, caught.exception.bytes_total),
                             (1024, len(payload)))
            self.assertEqual(received, payload[:1024])
            thread.join(1)
            self.assertFalse(thread.is_alive())
        finally:
            sender.close()
            receiver.close()

    def test_unexpected_peer_data_during_pacing_stops_without_more_bytes(self):
        payload = b"E" * 4096
        sender, receiver = socket.socketpair()
        received = bytearray()

        def reply_after_chunk():
            received.extend(receiver.recv(1024))
            receiver.sendall(b"ERR=VS;")

        thread = threading.Thread(target=reply_after_chunk, daemon=True)
        thread.start()
        try:
            with patch("verona.transport.socket.create_connection", return_value=sender), \
                    patch("verona.transport.TCP_CHUNK_DELAY", .5):
                with self.assertRaises(DeviceInterrupted) as caught:
                    send_tcp(payload, "127.0.0.1")
            self.assertEqual((caught.exception.bytes_sent, caught.exception.bytes_total),
                             (1024, len(payload)))
            self.assertEqual(received, payload[:1024])
            thread.join(1)
            self.assertFalse(thread.is_alive())
        finally:
            sender.close()
            receiver.close()

    def test_cancel_interrupts_a_blocked_socket_write(self):
        payload = b"B" * (8 * 1024 * 1024)
        cancel = threading.Event()
        sender, receiver = socket.socketpair()
        timer = threading.Timer(.1, cancel.set)
        timer.start()
        try:
            with patch("verona.transport.socket.create_connection", return_value=sender), \
                    patch("verona.transport.TCP_CHUNK_DELAY", 0):
                started = time.monotonic()
                with self.assertRaises(TransferCancelled) as caught:
                    send_tcp(payload, "127.0.0.1", cancel_event=cancel)
                elapsed = time.monotonic() - started
            self.assertGreater(caught.exception.bytes_sent, 0)
            self.assertLess(caught.exception.bytes_sent, len(payload))
            self.assertEqual(caught.exception.bytes_total, len(payload))
            self.assertLess(elapsed, 1.0)
        finally:
            timer.cancel()
            sender.close()
            receiver.close()

    def test_full_transfer_has_a_total_deadline(self):
        clock = [0.0]
        def send_then_expire(_data):
            clock[0] = 301.0
            return 1024
        with patch("verona.transport.socket.create_connection") as connect, \
                patch("verona.transport.time.monotonic", side_effect=lambda: clock[0]), \
                self._patch_fake_select():
            connection = connect.return_value.__enter__.return_value
            connection.send.side_effect = send_then_expire
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
                    def setblocking(self, _value): pass
                    def send(self, data):
                        accepted = bytes(data[:max_write])
                        self.data.extend(accepted)
                        return len(accepted)

                connection = Connection()
                clock = [0.0]
                def select(readers, writers, _errors, timeout):
                    if writers:
                        return [], list(writers), []
                    clock[0] += timeout
                    return [], [], []
                with patch("verona.transport.socket.create_connection", return_value=connection) as connect, \
                        patch("verona.transport.TCP_CHUNK_DELAY", .3), \
                        patch("verona.transport.time.monotonic", side_effect=lambda: clock[0]), \
                        patch("verona.transport.select.select", side_effect=select):
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

    def test_cancel_between_usb_writes_stops_and_reports_accepted_bytes(self):
        cancel = threading.Event()

        class CancellingEndpoint(Endpoint):
            def write(self, chunk, timeout):
                count = min(127, len(chunk))
                self.data.extend(chunk[:count])
                cancel.set()
                return count

        outgoing = CancellingEndpoint(1)
        device = Device([Interface([Endpoint(0x81), outgoing])])
        payload = b"U" * 1000
        with patch("usb.util.claim_interface"), \
                patch("usb.util.release_interface") as release, \
                patch("usb.util.dispose_resources") as dispose:
            with self.assertRaises(TransferCancelled) as caught:
                send_usb(payload, device, cancel_event=cancel)
        self.assertEqual((caught.exception.bytes_sent, caught.exception.bytes_total),
                         (127, len(payload)))
        self.assertEqual(outgoing.data, payload[:127])
        release.assert_called_once_with(device, 2)
        dispose.assert_called_once_with(device)

    def test_cancel_set_by_final_usb_write_is_not_reported_as_success(self):
        cancel = threading.Event()

        class CancellingEndpoint(Endpoint):
            def write(self, chunk, timeout):
                self.data.extend(chunk)
                cancel.set()
                return len(chunk)

        outgoing = CancellingEndpoint(1)
        device = Device([Interface([Endpoint(0x81), outgoing])])
        payload = b"U" * 127
        with patch("usb.util.claim_interface"), \
                patch("usb.util.release_interface"), \
                patch("usb.util.dispose_resources"):
            with self.assertRaises(TransferCancelled) as caught:
                send_usb(payload, device, cancel_event=cancel)
        self.assertEqual((caught.exception.bytes_sent, caught.exception.bytes_total),
                         (len(payload), len(payload)))
        self.assertEqual(outgoing.data, payload)


if __name__ == "__main__":
    unittest.main()

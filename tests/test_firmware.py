import fcntl
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from verona import firmware


def fixture(payload=bytes(range(256)) * 9, **overrides):
    fields = {"PGSR_SSA": "0", "HEADLEN": "0071", "DATLEN": f"{len(payload) + 11:06d}",
              "CHECKSUM": f"{sum(payload) & 0xffffffff:08x}", "HDSUM": "00000000"}
    fields.update(overrides)
    header = (f"PGSR_SSA={fields['PGSR_SSA']};HEADLEN={fields['HEADLEN']};"
              f"DATLEN={fields['DATLEN']};CHECKSUM={fields['CHECKSUM']};"
              f"HDSUM={fields['HDSUM']}").encode("ascii")
    return header + firmware.PGSTART + payload


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakePeer:
    """In-memory peer; recv advances a fake clock instead of using any network."""
    def __init__(self, image, clock, *, ready_after=1, ready_reply=b"PGREADY;",
                 ok_reply=b"PGOK;", fragment=False, max_write=1024,
                 fail_body_at=None, send_error=None, identity=None, version_only=False):
        self.image, self.clock = image, clock
        self.ready_after, self.ready_reply, self.ok_reply = ready_after, ready_reply, ok_reply
        self.fragment, self.max_write = fragment, max_write
        self.fail_body_at, self.send_error = fail_body_at, send_error
        self.identity = identity or {"MODE": "SS12", "HVER": "V7.1202_4300", "SVER": "V22.0613"}
        self.requests = ((b";RSVER;", "SVER"),) if version_only else (
            (b";RMODE;", "MODE"), (b";RHVER;", "HVER"), (b";RSVER;", "SVER"))
        self.timeout = 2.0
        self.pending = []
        self.commands = b""
        self.identity_count = 0
        self.header = b""
        self.body = b""
        self.writes = []
        self.header_attempts = 0
        self.ready_delivered = False
        self.received_markers = b""
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def settimeout(self, timeout):
        self.timeout = timeout

    def enqueue(self, message, delay=0.001):
        parts = [bytes([value]) for value in message] if self.fragment and message else [message]
        for index, part in enumerate(parts):
            self.pending.append((self.clock.now + delay + index * 0.001, part))
        self.pending.sort(key=lambda event: event[0])

    def send(self, data):
        kind = "identity" if self.identity_count < len(self.requests) else ("body" if self.ready_delivered else "header")
        if kind == "body" and self.fail_body_at is not None and len(self.body) >= self.fail_body_at:
            raise self.send_error or BrokenPipeError("Fake peer rejected more body bytes")
        count = min(len(data), self.max_write)
        if kind == "body" and self.fail_body_at is not None:
            count = min(count, self.fail_body_at - len(self.body))
        accepted = bytes(data[:count])
        self.writes.append((self.clock.now, kind, accepted))
        if kind == "identity":
            self.commands += accepted
            request, key = self.requests[self.identity_count]
            if len(self.commands) >= len(request):
                if self.commands != request:
                    raise AssertionError(f"Unexpected identity command {self.commands!r}")
                self.commands = b""
                self.identity_count += 1
                self.enqueue(f"{key}={self.identity[key]};".encode("ascii"))
        elif kind == "header":
            self.header += accepted
            if len(self.header) % len(self.image.header) == 0:
                self.header_attempts += 1
                if self.ready_after == self.header_attempts:
                    self.enqueue(self.ready_reply, delay=0.05)
        else:
            self.body += accepted
            if len(self.body) == len(self.image.body) and self.ok_reply is not None:
                self.enqueue(self.ok_reply, delay=0.05)
        return count

    def sendall(self, data):
        sent = 0
        while sent < len(data):
            sent += self.send(data[sent:])

    def recv(self, count):
        if not self.pending or self.pending[0][0] > self.clock.now + self.timeout:
            if self.timeout == 0:
                raise BlockingIOError
            self.clock.now += self.timeout
            raise socket.timeout("Fake receive timeout")
        when, data = self.pending.pop(0)
        self.clock.now = max(self.clock.now, when)
        if len(data) > count:
            self.pending.insert(0, (self.clock.now, data[count:]))
            data = data[:count]
        self.received_markers += data
        if b"PGREADY" in self.received_markers:
            self.ready_delivered = True
        return data


class InspectorTests(unittest.TestCase):
    def test_official_split_has_one_byte_overlap(self):
        data = fixture()
        image = firmware.inspect_firmware(data)
        self.assertEqual(image.marker_offset, 70)
        self.assertEqual(image.header, data[:71])
        self.assertEqual(image.body, data[70:])
        self.assertEqual(len(image.header) + len(image.body), len(data) + 1)

    def test_small_fixture_and_metadata_limits(self):
        image = firmware.inspect_firmware(fixture(b"test", HDSUM="12345678"))
        summary = image.summary()
        self.assertEqual(summary["file_bytes"], 86)
        self.assertTrue(summary["checksum_relation_matches"])
        for key in ("hdsum_verified", "compatibility_verified", "real_device_update_verified"):
            self.assertFalse(summary[key])

    def test_version_literals_remain_candidates(self):
        image = firmware.inspect_firmware(fixture(b"V26.0605\0"))
        self.assertEqual(image.summary()["version_candidates"], ["V26.0605"])
        image = firmware.inspect_firmware(fixture(b"V26.0605\0V25.0210\0"))
        self.assertIsNone(image.summary()["candidate_version"])

    def test_invalid_lengths_numbers_and_format(self):
        for changes in ({"HEADLEN": "0070"}, {"DATLEN": "000000"},
                        {"PGSR_SSA": "1"}, {"HEADLEN": "-071"}, {"HDSUM": "no-value"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                firmware.inspect_firmware(fixture(**changes))

    def test_corrupt_payload(self):
        data = fixture()
        with self.assertRaisesRegex(ValueError, "checksum"):
            firmware.inspect_firmware(data[:-1] + bytes([data[-1] ^ 1]))

    def test_missing_and_duplicate_marker(self):
        for data in (fixture().replace(firmware.PGSTART, b"missing"), fixture() + firmware.PGSTART):
            with self.subTest(length=len(data)), self.assertRaisesRegex(ValueError, "one PGSTART"):
                firmware.inspect_firmware(data)

    def test_firmware_size_limit_applies_to_inspect_and_bounded_file_read(self):
        oversized = b"x" * (firmware.MAX_FIRMWARE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "larger than 2 MiB"):
            firmware.inspect_firmware(oversized)

        handle = Mock()
        handle.__enter__ = Mock(return_value=handle)
        handle.__exit__ = Mock(return_value=False)
        handle.read.return_value = oversized
        with patch.object(Path, "open", return_value=handle):
            with self.assertRaisesRegex(ValueError, "larger than 2 MiB"):
                firmware.load_firmware("synthetic.bin")
        handle.read.assert_called_once_with(firmware.MAX_FIRMWARE_BYTES + 1)


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.image = firmware.inspect_firmware(fixture())
        self.clock = Clock()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lock = Path(self.temp.name) / "device.lock"
        self.peer = FakePeer(self.image, self.clock)
        self.connect = self.enterContext(patch.object(firmware.socket, "create_connection", return_value=self.peer))
        self.enterContext(patch.object(firmware.time, "monotonic", self.clock))

    def update(self, **kwargs):
        defaults = {"expected_sha256": self.image.sha256, "expected_mode": "SS12",
                    "expected_hver": "V7.1202_4300", "expected_sver": "V22.0613",
                    "lock_path": self.lock}
        defaults.update(kwargs)
        return firmware.update_firmware(self.image, "192.0.2.1", **defaults)

    def peer_with(self, **kwargs):
        self.peer = FakePeer(self.image, self.clock, **kwargs)
        self.connect.return_value = self.peer

    def test_complete_transfer_is_not_version_verification(self):
        progress = []
        result = self.update(on_progress=lambda sent, total: progress.append((sent, total)))
        self.assertEqual(result["status"], "pgok_received")
        self.assertFalse(result["version_verified"])
        self.assertEqual(result["device_state"], "unknown")
        self.assertEqual(self.peer.header, self.image.header)
        self.assertEqual(self.peer.body, self.image.body)
        self.assertEqual(progress[-1], (len(self.image.body), len(self.image.body)))
        self.connect.assert_called_once()
        self.assertTrue(self.peer.closed)

    def test_chunk_size_and_pacing(self):
        self.update()
        writes = [(when, data) for when, kind, data in self.peer.writes if kind == "body"]
        self.assertEqual([len(data) for _, data in writes], [1024, 1024, 268])
        self.assertTrue(all(b[0] - a[0] >= 0.3 - 1e-9 for a, b in zip(writes, writes[1:])))

    def test_partial_socket_writes_preserve_exact_bytes_and_counts(self):
        self.peer_with(max_write=23)
        result = self.update()
        self.assertEqual(self.peer.body, self.image.body)
        self.assertEqual(result["bytes_header_sent"], len(self.image.header))
        self.assertEqual(result["bytes_body_sent"], len(self.image.body))

    def test_bytewise_identity_and_protocol_replies(self):
        self.peer_with(fragment=True)
        self.assertEqual(self.update()["status"], "pgok_received")

    def test_every_ready_split_and_merged_duplicate(self):
        for split in range(1, len(b"PGREADY")):
            with self.subTest(split=split):
                self.clock.now = 0
                self.peer_with(ready_reply=b"PGREADY;PGREADY;")
                original = self.peer.enqueue

                def enqueue(message, delay=0.001):
                    if message.startswith(b"PGREADY"):
                        original(message[:split], delay)
                        original(message[split:], delay + 0.01)
                    else:
                        original(message, delay)

                self.peer.enqueue = enqueue
                result = self.update()
                self.assertEqual(result["bytes_body_sent"], len(self.image.body))

    def test_coalesced_early_ok_rejected_before_any_body(self):
        self.peer_with(ready_reply=b"PGREADY;PGREADY;PGREADY;PGOK;")
        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update()
        self.assertEqual(caught.exception.stage, "body")
        self.assertEqual(caught.exception.bytes_body_sent, 0)
        self.assertEqual(self.peer.body, b"")

    def test_ok_without_ready_rejected(self):
        self.peer_with(ready_reply=b"PGOK;")
        with self.assertRaisesRegex(firmware.FirmwareUpdateError, "before the complete"):
            self.update()
        self.assertEqual(self.peer.body, b"")

    def test_header_repeats_only_until_ready(self):
        self.peer_with(ready_after=3)
        result = self.update()
        self.assertEqual(result["header_attempts"], 3)
        self.assertEqual(self.peer.header, self.image.header * 3)
        writes = [(when, data) for when, kind, data in self.peer.writes if kind == "header"]
        self.assertTrue(all(b[0] - a[0] >= 0.2 - 1e-9 for a, b in zip(writes, writes[1:])))

    def test_header_timeout_and_attempt_bound(self):
        self.peer_with(ready_after=None)
        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update(ready_timeout=100)
        self.assertEqual(caught.exception.stage, "header")
        self.assertEqual(caught.exception.bytes_body_sent, 0)
        self.assertLessEqual(self.peer.header_attempts, 200)
        self.assertLessEqual(self.clock.now, 40.01)
        self.connect.assert_called_once()

    def test_identity_mismatch_prevents_header(self):
        self.peer_with(identity={"MODE": "OTHER", "HVER": "V7.1202_4300", "SVER": "V22.0613"})
        with self.assertRaisesRegex(firmware.FirmwareUpdateError, "identity mismatch") as caught:
            self.update()
        self.assertEqual(caught.exception.stage, "identity")
        self.assertEqual(caught.exception.bytes_header_sent, 0)
        self.assertEqual(self.peer.header, b"")

    def test_unsolicited_ready_after_identity_prevents_header(self):
        original_recv = self.peer.recv
        queued = False

        def recv(count):
            nonlocal queued
            data = original_recv(count)
            if not queued and b"SVER=" in data:
                queued = True
                self.peer.enqueue(b"PGREADY;", delay=0)
            return data

        self.peer.recv = recv
        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update()
        self.assertEqual(caught.exception.stage, "identity")
        self.assertEqual(caught.exception.bytes_header_sent, 0)
        self.assertEqual(caught.exception.bytes_body_sent, 0)
        self.assertEqual(self.peer.header, b"")

    def test_hash_mismatch_prevents_connection(self):
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.update(expected_sha256="0" * 64)
        self.connect.assert_not_called()

    def test_partial_body_error_retains_counts_without_reconnection(self):
        self.peer_with(fail_body_at=100)
        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update()
        result = caught.exception.as_dict()
        self.assertEqual(result["bytes_body_sent"], 100)
        self.assertTrue(result["update_may_have_started"])
        self.assertFalse(result["automatic_retry"])
        self.assertEqual(result["device_state"], "unknown")
        self.connect.assert_called_once()

    def test_partial_header_error_retains_counts(self):
        original = self.peer.send

        def send(data):
            if self.peer.identity_count == 3:
                if self.peer.header:
                    raise BrokenPipeError("Fake header interruption")
                return original(data[:5])
            return original(data)

        self.peer.send = send
        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update()
        self.assertEqual(caught.exception.bytes_header_sent, 5)
        self.assertEqual(caught.exception.bytes_body_sent, 0)
        self.assertTrue(caught.exception.as_dict()["update_may_have_started"])

    def test_first_header_write_timeout_does_not_claim_no_update(self):
        original = self.peer.send

        def send(data):
            if self.peer.identity_count == 3:
                raise socket.timeout("Fake first header write timeout")
            return original(data)

        self.peer.send = send
        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update()
        result = caught.exception.as_dict()
        self.assertEqual(result["bytes_header_sent"], 0)
        self.assertTrue(result["update_may_have_started"])
        self.assertTrue(result["byte_counts_are_lower_bounds"])

    def test_early_ok_during_body_stops_before_next_write(self):
        original = self.peer.send

        def send(data):
            count = original(data)
            if len(self.peer.body) == 1024:
                self.peer.enqueue(b"PGOK;", delay=0.01)
            return count

        self.peer.send = send
        with self.assertRaisesRegex(firmware.FirmwareUpdateError, "before the complete") as caught:
            self.update()
        self.assertEqual(caught.exception.bytes_body_sent, 1024)
        self.connect.assert_called_once()

    def test_zero_body_write_is_a_failure(self):
        original = self.peer.send
        self.peer.send = lambda data: 0 if self.peer.ready_delivered else original(data)
        with self.assertRaisesRegex(firmware.FirmwareUpdateError, "zero bytes") as caught:
            self.update()
        self.assertEqual(caught.exception.bytes_body_sent, 0)
        self.assertEqual(caught.exception.stage, "body")

    def test_body_write_timeout_does_not_reconnect(self):
        self.peer_with(fail_body_at=100, send_error=socket.timeout("Fake body timeout"))
        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update()
        self.assertEqual(caught.exception.bytes_body_sent, 100)
        self.connect.assert_called_once()

    def test_eof_after_body_is_unknown(self):
        self.peer_with(ok_reply=b"")
        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update()
        self.assertEqual(caught.exception.stage, "completion")
        self.assertEqual(caught.exception.bytes_body_sent, len(self.image.body))
        self.assertEqual(caught.exception.device_state, "unknown")

    def test_missing_ok_times_out_without_replay(self):
        self.peer_with(ok_reply=None)
        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update(completion_timeout=0.1)
        self.assertEqual(caught.exception.stage, "completion")
        self.assertEqual(self.peer.body, self.image.body)
        self.connect.assert_called_once()

    def test_total_deadline_stops_body(self):
        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update(total_timeout=0.5)
        self.assertEqual(caught.exception.stage, "body")
        self.assertGreater(caught.exception.bytes_body_sent, 0)
        self.assertLess(caught.exception.bytes_body_sent, len(self.image.body))

    def test_timeouts_reject_bool_nonfinite_and_nonpositive(self):
        for key in ("ready_timeout", "completion_timeout", "total_timeout"):
            for value in (True, False, 0, -1, float("nan"), float("inf")):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.update(**{key: value})
        self.connect.assert_not_called()

    def test_peer_noise_does_not_grow_reply_buffer(self):
        self.peer_with(ready_reply=b"x" * 10000 + b"PGREADY;")
        self.assertEqual(self.update()["status"], "pgok_received")

    def test_shared_device_lock_prevents_connection(self):
        with self.lock.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(firmware.FirmwareUpdateError, "device lock"):
                self.update()
        self.connect.assert_not_called()

    def test_query_and_verify_are_read_only(self):
        identity = firmware.query_identity("192.0.2.1", lock_path=self.lock)
        self.assertEqual(identity, self.peer.identity)
        self.assertEqual([kind for _, kind, _ in self.peer.writes], ["identity"] * 3)
        self.assertEqual(self.peer.header, b"")

    def test_verify_sends_only_one_rsver_query(self):
        self.peer_with(version_only=True)
        result = firmware.verify_firmware_version("192.0.2.1", "V22.0613", lock_path=self.lock)
        self.assertTrue(result["version_verified"])
        self.assertEqual([data for _, _, data in self.peer.writes], [b";RSVER;"])
        self.assertEqual(self.peer.header, b"")

    def test_verify_mismatch_does_not_reflash(self):
        self.peer_with(version_only=True)
        result = firmware.verify_firmware_version("192.0.2.1", "V26.0605", lock_path=self.lock)
        self.assertFalse(result["version_verified"])
        self.assertEqual(result["software_version"], "V22.0613")
        self.assertEqual(self.peer.header, b"")

    def test_progress_failure_preserves_transfer_counts(self):
        def progress(sent, total):
            raise RuntimeError("Fake callback failure")

        with self.assertRaises(firmware.FirmwareUpdateError) as caught:
            self.update(on_progress=progress)
        self.assertEqual(caught.exception.bytes_body_sent, 1024)
        self.assertEqual(caught.exception.stage, "body")


if __name__ == "__main__":
    unittest.main()

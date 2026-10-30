"""Explicit Wi-Fi/USB firmware transfer following the recovered SDK flow.

Real-device flashing remains unvalidated. HDSUM and hardware compatibility are
unknown; a matching checksum or PGOK does not establish either.
"""
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import socket
import time

from .transport import TCP_PORT, _ipv4_endpoint, query_tcp_version
from .device_lock import DeviceBusyError, device_lock
from .firmware_usb import UsbConnection, list_usb_nodes


PGSTART = b";;;;PGSTART;"
HEADER_ATTEMPTS = 200
HEADER_DELAY = 0.2
BODY_DELAY = 0.3
CHUNK_BYTES = 1024
MAX_FIRMWARE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class FirmwareImage:
    data: bytes
    marker_offset: int
    fields: dict

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()

    @property
    def header(self):
        return self.data[:self.marker_offset + 1]

    @property
    def body(self):
        return self.data[self.marker_offset:]

    def summary(self):
        candidates = sorted({value.decode("ascii") for value in
                             re.findall(rb"\bV[0-9]{2}\.[0-9]{4}(?=\x00)", self.data)})
        return {"sha256": self.sha256, "file_bytes": len(self.data),
                "marker_offset": self.marker_offset, "marker_bytes": len(PGSTART),
                "header_bytes": len(self.header), "body_bytes": len(self.body),
                "fields": dict(self.fields), "version_candidates": candidates,
                "candidate_version": candidates[0] if len(candidates) == 1 else None,
                "checksum_relation_matches": True, "hdsum_verified": False,
                "compatibility_verified": False, "real_device_update_verified": False}


def inspect_firmware(data):
    """Validate the container relationships observed in the four official BINs."""
    if not isinstance(data, bytes):
        raise TypeError("Firmware input must be bytes")
    if len(data) > MAX_FIRMWARE_BYTES:
        raise ValueError("Firmware files larger than 2 MiB are not supported")
    if data.count(PGSTART) != 1:
        raise ValueError("Expected exactly one PGSTART marker")
    marker = data.index(PGSTART)
    header = data[:marker + 1]
    match = re.fullmatch(
        rb"PGSR_SSA=([0-9]+);HEADLEN=([0-9]+);DATLEN=([0-9]+);"
        rb"CHECKSUM=([0-9a-fA-F]{8});HDSUM=([0-9a-fA-F]{8});", header)
    if match is None:
        raise ValueError("Unrecognized firmware header")
    keys = ("PGSR_SSA", "HEADLEN", "DATLEN", "CHECKSUM", "HDSUM")
    fields = dict(zip(keys, (value.decode("ascii") for value in match.groups())))
    if fields["PGSR_SSA"] != "0":
        raise ValueError("Only the observed PGSR_SSA=0 format is supported")
    if int(fields["HEADLEN"]) != len(header):
        raise ValueError("HEADLEN does not match header bytes")
    if int(fields["DATLEN"]) != len(data) - len(header):
        raise ValueError("DATLEN does not match container length")
    payload = data[marker + len(PGSTART):]
    if not payload or (sum(payload) & 0xffffffff) != int(fields["CHECKSUM"], 16):
        raise ValueError("Observed payload checksum relation does not match")
    return FirmwareImage(data, marker, fields)


def load_firmware(path):
    """Read a local container with a fixed limit, including for special files."""
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_FIRMWARE_BYTES + 1)
    return inspect_firmware(data)


class FirmwareUpdateError(OSError):
    def __init__(self, message, *, stage, bytes_header_sent=0, bytes_body_sent=0):
        super().__init__(message)
        self.stage = stage
        self.bytes_header_sent = bytes_header_sent
        self.bytes_body_sent = bytes_body_sent
        self.device_state = "unknown"

    def as_dict(self):
        return {"status": "failed", "error": str(self), "stage": self.stage,
                "bytes_header_sent": self.bytes_header_sent,
                "bytes_body_sent": self.bytes_body_sent,
                "update_may_have_started": self.stage in ("header", "body", "completion"),
                "byte_counts_are_lower_bounds": True,
                "device_state": self.device_state, "version_verified": False,
                "automatic_retry": False}


def _positive_timeout(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return float(value)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Firmware operation deadline exceeded")
    return remaining


@contextmanager
def _device_lock(lock_path=None):
    # Acquire the same lock as the GUI and MCP; translate acquisition errors
    # without masking failures raised during the transfer itself.
    with ExitStack() as stack:
        try:
            stack.enter_context(device_lock(lock_path))
        except DeviceBusyError as cause:
            raise FirmwareUpdateError(str(cause), stage="lock") from cause
        yield


class _Receiver:
    def __init__(self, connection):
        self.connection = connection
        self.buffer = b""

    def read(self, timeout):
        self.connection.settimeout(timeout)
        data = self.connection.recv(CHUNK_BYTES)
        if not data:
            raise ConnectionError("Cutter closed the connection; update state is unknown")
        self.buffer += data
        if len(self.buffer) > 4096:
            raise ValueError("Cutter reply exceeded the receive limit")

    def marker(self):
        found = [(self.buffer.find(token), token) for token in (b"PGREADY", b"PGOK")]
        found = [(index, token) for index, token in found if index >= 0]
        if not found:
            self.buffer = self.buffer[-6:]
            return None
        index, token = min(found)
        self.buffer = self.buffer[index + len(token):]
        return token


def _write(connection, data, deadline):
    """Use send instead of sendall so successful partial writes are measurable."""
    sent = 0
    while sent < len(data):
        connection.settimeout(min(30.0, _remaining(deadline)))
        count = connection.send(data[sent:sent + CHUNK_BYTES])
        if count <= 0:
            raise ConnectionError("Socket accepted zero bytes")
        sent += count


def _identity(connection, deadline):
    receiver = _Receiver(connection)
    identity = {}
    for key, request in (("MODE", b";RMODE;"), ("HVER", b";RHVER;"), ("SVER", b";RSVER;")):
        _write(connection, request, deadline)
        while b";" not in receiver.buffer:
            receiver.read(_remaining(deadline))
        record, receiver.buffer = receiver.buffer.split(b";", 1)
        prefix = key.encode("ascii") + b"="
        if not record.startswith(prefix) or len(record) <= len(prefix):
            raise ValueError(f"Invalid {key} reply")
        value = record[len(prefix):].decode("ascii", errors="strict")
        if not value.isprintable() or value != value.strip():
            raise ValueError(f"Invalid {key} reply")
        identity[key] = value
    if receiver.buffer:
        raise ValueError("Unexpected data after identity replies")
    return identity


def _endpoint(host, port, usb_node):
    if usb_node is not None:
        if host is not None:
            raise ValueError("Choose either Wi-Fi or USB, not both")
        return None
    if not isinstance(host, str) or not host.strip():
        raise ValueError("Specify the cutter's IPv4 address or USB device")
    return _ipv4_endpoint(host, port)


def _connect(address, port, timeout, usb_node):
    if usb_node is not None:
        return UsbConnection(usb_node, timeout)
    return socket.create_connection((address, port), timeout=timeout)


def query_identity(host=None, port=TCP_PORT, timeout=2.0, *, usb_node=None, lock_path=None):
    address = _endpoint(host, port, usb_node)
    timeout = _positive_timeout(timeout, "timeout")
    deadline = time.monotonic() + timeout
    with _device_lock(lock_path):
        with _connect(address, port, _remaining(deadline), usb_node) as connection:
            return _identity(connection, deadline)


def verify_firmware_version(host, expected_sver, port=TCP_PORT, timeout=2.0, *, usb_node=None, lock_path=None):
    if not isinstance(expected_sver, str) or not expected_sver:
        raise ValueError("Expected SVER is required")
    timeout = _positive_timeout(timeout, "timeout")
    _endpoint(host, port, usb_node)
    with _device_lock(lock_path):
        if usb_node is None:
            version = query_tcp_version(host, port, timeout)
        else:
            deadline = time.monotonic() + timeout
            with _connect(None, port, _remaining(deadline), usb_node) as connection:
                receiver = _Receiver(connection)
                _write(connection, b";RSVER;", deadline)
                while b";" not in receiver.buffer:
                    receiver.read(_remaining(deadline))
                reply = receiver.buffer
                if (not reply.startswith(b"SVER=") or not reply.endswith(b";")
                        or reply.count(b";") != 1 or len(reply) <= 6):
                    raise ValueError("Invalid SVER reply")
                version = reply[5:-1].decode("ascii", errors="strict")
                if not version.isprintable() or version != version.strip():
                    raise ValueError("Invalid SVER reply")
    return {"software_version": version, "expected_sver": expected_sver,
            "version_verified": version == expected_sver, "automatic_retry": False}


def update_firmware(image, host=None, *, expected_sha256, expected_mode, expected_hver,
                    expected_sver, port=TCP_PORT, ready_timeout=40.0,
                    completion_timeout=30.0, total_timeout=600.0, on_progress=None,
                    usb_node=None, lock_path=None):
    """Make one explicit firmware attempt, with no reconnect or body replay.

    PGOK means the protocol marker was received, not that the new version or
    hardware compatibility was verified. Header transmission may reset the MCU.
    """
    # Reinspect bytes so a manually constructed/mutated metadata object cannot
    # bypass the actual container validation.
    image = inspect_firmware(image.data)
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("Expected SHA-256 must contain 64 hexadecimal characters")
    if image.sha256 != expected_sha256.lower():
        raise ValueError("Firmware SHA-256 mismatch; no connection opened")
    expected = {"MODE": expected_mode, "HVER": expected_hver, "SVER": expected_sver}
    if any(not isinstance(value, str) or not value or ";" in value for value in expected.values()):
        raise ValueError("Expected MODE, HVER and SVER must be nonempty text")
    address = _endpoint(host, port, usb_node)
    ready_timeout = min(40.0, _positive_timeout(ready_timeout, "ready_timeout"))
    completion_timeout = _positive_timeout(completion_timeout, "completion_timeout")
    total_timeout = _positive_timeout(total_timeout, "total_timeout")
    deadline = time.monotonic() + total_timeout
    counts = {"bytes_header_sent": 0, "bytes_body_sent": 0}
    stage = "connect"
    attempts = 0
    try:
        with _device_lock(lock_path):
            with _connect(address, port, min(2.0, _remaining(deadline)), usb_node) as connection:
                stage = "identity"
                identity = _identity(connection, min(deadline, time.monotonic() + 2.0))
                if identity != expected:
                    raise ValueError(f"Device identity mismatch: expected {expected}, received {identity}")
                receiver = _Receiver(connection)

                def events(until, *, allow_ready=False, body_complete=False, poll=False):
                    while True:
                        token = receiver.marker()
                        if token == b"PGOK":
                            if not body_complete:
                                raise ValueError("PGOK arrived before the complete firmware body was sent")
                            return "ok"
                        if token == b"PGREADY" and allow_ready:
                            return "ready"
                        if token is not None:
                            continue
                        if time.monotonic() >= until and not poll:
                            return None
                        try:
                            _remaining(deadline)
                            receiver.read(0.0 if poll else min(_remaining(deadline), _remaining(until)))
                        except (BlockingIOError, socket.timeout):
                            return None

                def send_piece(payload, key, until):
                    connection.settimeout(min(30.0, _remaining(until), _remaining(deadline)))
                    count = connection.send(payload[:CHUNK_BYTES])
                    if count <= 0:
                        raise ConnectionError("Socket accepted zero bytes")
                    counts[key] += count
                    return count

                if events(time.monotonic(), allow_ready=True, poll=True) == "ready":
                    raise ValueError("Unsolicited PGREADY before the firmware header; nothing sent")
                stage = "header"
                ready_deadline = min(deadline, time.monotonic() + ready_timeout)
                ready = False
                for attempts in range(1, HEADER_ATTEMPTS + 1):
                    offset = 0
                    while offset < len(image.header):
                        offset += send_piece(image.header[offset:], "bytes_header_sent", ready_deadline)
                        ready = events(min(ready_deadline, time.monotonic() + HEADER_DELAY), allow_ready=True) == "ready"
                        if ready:
                            break
                    if ready:
                        break
                    _remaining(ready_deadline)
                if not ready:
                    raise TimeoutError("PGREADY not received within header attempt limit")

                stage = "body"
                # Reject a coalesced PGREADY+PGOK before recording any body byte.
                events(deadline, poll=True)
                body = image.body
                while counts["bytes_body_sent"] < len(body):
                    events(deadline, poll=True)
                    offset = counts["bytes_body_sent"]
                    send_piece(body[offset:], "bytes_body_sent", deadline)
                    if on_progress is not None:
                        on_progress(counts["bytes_body_sent"], len(body))
                    if counts["bytes_body_sent"] < len(body):
                        events(min(deadline, time.monotonic() + BODY_DELAY))
                stage = "completion"
                completion_deadline = min(deadline, time.monotonic() + completion_timeout)
                while events(completion_deadline, body_complete=True) != "ok":
                    _remaining(completion_deadline)
                return {"status": "pgok_received", "identity_before": identity,
                        "sha256": image.sha256, "header_attempts": attempts, **counts,
                        "physical_completion": "unknown", "device_state": "unknown",
                        "version_verified": False, "automatic_retry": False}
    except FirmwareUpdateError:
        raise
    except (Exception, KeyboardInterrupt) as cause:
        message = ("Interrupted by user; interruption does not stop or roll back the device. "
                   "Device state unknown. Do not automatically repeat the update."
                   if isinstance(cause, KeyboardInterrupt) else str(cause))
        raise FirmwareUpdateError(message, stage=stage, **counts) from cause

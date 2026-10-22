"""Bounded identity diagnostics omit identifiers and never send motion bytes."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from verona import cli, transport


REPLY = (b"PGHEAD=PGSR_SSA;MDATE=220613;NUM=\xff\x00\x01\x80\xfe;PID=example-product;"
         b"SVER=V22.0613;HVER=V7.1202_4300;BSVER=example-version;MODE=SS12;"
         b"MID=example-machine;OK=RINFO;")


class Connection:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.sent = []
    def __enter__(self): return self
    def __exit__(self, *_args): pass
    def sendall(self, payload): self.sent.append(payload)
    def settimeout(self, _timeout): pass
    def recv(self, size):
        return next(self.chunks, b"")[:size]


class WifiInfoTests(unittest.TestCase):
    def test_segmented_reply_returns_identity_without_device_specific_values(self):
        connection = Connection([REPLY[:15], REPLY[15:75], REPLY[75:]])
        with patch("verona.transport.socket.create_connection", return_value=connection) as connect:
            info = transport.query_tcp_info("192.0.2.1")
        connect.assert_called_once_with(("192.0.2.1", 8080), timeout=2.)
        self.assertEqual(connection.sent, [b";RINFO;"])
        self.assertEqual(info["identity"]["model"], "SS12")
        self.assertEqual(info["identity"]["software_version"], "V22.0613")
        self.assertEqual(info["identity"]["hardware_version"], "V7.1202_4300")
        self.assertEqual(info["identifier_fields_omitted"], ["MID", "NUM", "PID"])
        for value in ("example-number", "example-product", "example-machine"):
            self.assertNotIn(value, json.dumps(info))

    def test_empty_incomplete_duplicate_wrong_and_oversized_replies_reject(self):
        for reply in (b"", b"ERR=RINFO;", REPLY[:-1], REPLY + b"unexpected",
                      REPLY.replace(b"MODE=SS12;", b"MODE=SS12;MODE=SS24;"),
                      REPLY.replace(b"SVER=V22.0613;", b""),
                      REPLY.replace(b"SS12", b"\xff"),
                      REPLY.replace(b"SS12", b"bad\nmode"), b"x"*4096):
            with self.subTest(reply_length=len(reply)), patch(
                    "verona.transport.socket.create_connection", return_value=Connection([reply])):
                with self.assertRaises(ValueError):
                    transport.query_tcp_info("192.0.2.1")

    def test_timeout_has_no_retry_and_invalid_endpoint_has_no_connection(self):
        connection = Connection([])
        with patch("verona.transport.socket.create_connection", return_value=connection) as connect, \
                patch.object(connection, "recv", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                transport.query_tcp_info("192.0.2.1")
            connect.assert_called_once()
        with patch("verona.transport.socket.create_connection") as connect:
            with self.assertRaises(ValueError):
                transport.query_tcp_info("255.255.255.255")
            connect.assert_not_called()

    def test_details_cli_uses_device_lock_and_keeps_readiness_unknown(self):
        connection = Connection([REPLY])
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"XDG_STATE_HOME": folder}), \
                patch("verona.transport.socket.create_connection", return_value=connection), \
                patch("verona.transport.send_tcp") as send, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["status", "--host", "192.0.2.1", "--details"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["identity"]["model"], "SS12")
        self.assertEqual(result["physical_readiness"], "unknown")
        send.assert_not_called()

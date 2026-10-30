"""Explicit command-line firmware inspection and Wi-Fi/USB update workflow."""
import argparse
import json
from pathlib import Path
import re
import sys

from .firmware import (
    FirmwareUpdateError, load_firmware, query_identity,
    update_firmware, verify_firmware_version,
)


def _sha256(value):
    value = value.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError("SHA-256 must contain exactly 64 hexadecimal characters")
    return value


def _version(value):
    if not re.fullmatch(r"V[0-9]{2}\.[0-9]{4}", value):
        raise argparse.ArgumentTypeError("Use the expected firmware version, for example V23.0919")
    return value


def _parser():
    parser = argparse.ArgumentParser(
        description="Juliet Wi-Fi/USB firmware utility; hardware flashing has not been validated.",
        epilog="inspect is offline. status/verify are read-only. update can reset and move the cutter.")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Inspect a local BIN without contacting a device")
    inspect.add_argument("file", type=Path)
    status = commands.add_parser("status", help="Read MODE, HVER and SVER; no update")
    verify = commands.add_parser("verify", help="Read SVER once and compare with an expected version")
    verify.add_argument("--target-version", required=True, type=_version)
    update = commands.add_parser("update", help="Explicitly flash a previously reviewed compatible BIN")
    update.add_argument("file", type=Path)
    update.add_argument("--sha256", required=True, type=_sha256)
    update.add_argument("--expected-mode", required=True)
    update.add_argument("--expected-hver", required=True)
    update.add_argument("--expected-sver", required=True, type=_version)
    update.add_argument("--target-version", required=True, type=_version)
    update.add_argument("--confirm-compatible", action="store_true",
                        help="Assert that this exact BIN was independently confirmed for this hardware")
    update.add_argument("--machine-ready", action="store_true",
                        help="Assert the cutter is idle, other senders are closed and movement is clear")
    for command in (status, verify, update):
        endpoint = command.add_mutually_exclusive_group(required=True)
        endpoint.add_argument("--host", help="Single cutter IPv4 address from its LCD")
        endpoint.add_argument("--usb-node", help="Explicit Linux cutter device, e.g. /dev/usb/lp0")
        command.add_argument("--port", type=int, default=8080)
    return parser


def _emit(result):
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            _emit({"status": "inspected", **load_firmware(args.file).summary()})
            return 0
        connection = {"usb_node": args.usb_node} if args.usb_node is not None else {}
        if args.command == "status":
            _emit({"status": "identity_read", "identity": query_identity(args.host, args.port, **connection)})
            return 0
        if args.command == "verify":
            result = verify_firmware_version(args.host, args.target_version, args.port, **connection)
            _emit(result)
            return 0 if result["version_verified"] else 3

        if not args.confirm_compatible or not args.machine_ready:
            raise ValueError("update requires --confirm-compatible and --machine-ready; "
                             "these are operator assertions, not automatic compatibility detection")
        image = load_firmware(args.file)
        if image.sha256 != args.sha256:
            raise ValueError("BIN SHA-256 differs from the reviewed file; nothing sent")
        candidates = image.summary().get("version_candidates", [])
        if candidates and args.target_version not in candidates:
            raise ValueError("Target version conflicts with the version strings found in the BIN; nothing sent")
        last_bucket = -1

        def progress(sent, total):
            nonlocal last_bucket
            bucket = min(20, sent * 20 // total)
            if bucket > last_bucket:
                last_bucket = bucket
                print(f"Firmware body: {sent}/{total} bytes ({sent * 100 // total}%)", file=sys.stderr)

        result = update_firmware(
            image, args.host, expected_sha256=args.sha256,
            expected_mode=args.expected_mode, expected_hver=args.expected_hver,
            expected_sver=args.expected_sver, port=args.port, on_progress=progress, **connection)
        result["pgok_received"] = True
        # One read-only attempt after PGOK. A reboot may need more time; the
        # separate verify command can be used later without repeating a flash.
        try:
            verification = verify_firmware_version(args.host, args.target_version, args.port, **connection)
            result["verification"] = verification
            verified = verification["version_verified"]
        except (ValueError, OSError) as error:
            result["verification"] = {"version_verified": False, "error": str(error)}
            verified = False
        result["version_verified"] = verified
        result["status"] = "version_verified" if verified else "version_unverified"
        result["automatic_retry"] = False
        _emit(result)
        return 0 if verified else 3
    except FirmwareUpdateError as error:
        interrupted = isinstance(error.__cause__, KeyboardInterrupt)
        result = error.as_dict()
        if interrupted:
            result["status"] = "interrupted"
        _emit(result)
        return 130 if interrupted else 1
    except (ValueError, OSError) as error:
        _emit({"status": "failed", "error": str(error), "automatic_retry": False})
        return 1
    except KeyboardInterrupt:
        _emit({"status": "interrupted", "device_state": "unknown", "automatic_retry": False,
               "error": "Interruption does not stop or roll back the device. Do not automatically repeat the update."})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit status queries, offline job validation and one-shot cut delivery."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from . import commands, transport
from .device_lock import device_lock


def prepare_job(path, *, force=None, speed=None, tool=None, passes=None):
    path = Path(path).expanduser()
    if path.suffix.lower() == ".svg":
        from .core import load_svg
        return commands.encode_plt(load_svg(path), force=force, speed=speed,
                                   tool=0 if tool is None else tool, passes=1 if passes is None else passes)
    if path.suffix.lower() != ".jdesign":
        raise ValueError("Use a .jdesign or plain path-based .svg file; raw PLT input is not accepted.")
    from .input_safety import read_design
    from .ui import VeronaWindow
    data = read_design(path)
    items, settings = VeronaWindow._validate_document(data)
    return commands.encode_document(items, data.get("metadata", {}), settings,
                                    force=force, speed=speed, tool=tool, passes=passes)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verona offline job validation and Juliet Wi-Fi control")
    subcommands = parser.add_subparsers(dest="operation", required=True)
    status = subcommands.add_parser("status", help="Query the version without moving the cutter")
    status.add_argument("--details", action="store_true", help="Read RINFO identity fields; omit device identifiers")
    cut = subcommands.add_parser("cut", help="Validate a design; dry-run unless --send is explicit")
    for command in (status, cut):
        command.add_argument("--host", default=os.environ.get("VERONA_HOST") or None,
                             help="Cutter IPv4 address; defaults to VERONA_HOST")
        command.add_argument("--port", type=int, default=transport.TCP_PORT)
    cut.add_argument("design", type=Path)
    cut.add_argument("--force", type=int)
    cut.add_argument("--speed", type=int)
    cut.add_argument("--tool", choices=("blade", "pen"))
    cut.add_argument("--passes", type=int, help="Cut each contour 1–10 times; defaults to the saved setting or 1")
    mode = cut.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Validate/export without hardware access (default)")
    mode.add_argument("--send", action="store_true", help="Send once; ensure the machine and material are ready")
    cut.add_argument("--output", type=Path, help="Write the validated PLT job to this file")
    args = parser.parse_args(argv)
    try:
        if args.operation == "status" or args.send:
            if not args.host:
                raise ValueError("Specify --host or set VERONA_HOST to your cutter's IPv4 address.")
            transport._ipv4_endpoint(args.host, args.port)
        if args.operation == "status":
            with device_lock():
                if args.details:
                    info = transport.query_tcp_info(args.host, args.port)
                    result = {"operation": "status", **info, "physical_readiness": "unknown"}
                else:
                    version = transport.query_tcp_version(args.host, args.port)
                    result = {"operation": "status", "version": version, "physical_readiness": "unknown"}
        else:
            tool = None if args.tool is None else (0 if args.tool == "blade" else 1)
            payload = prepare_job(args.design, force=args.force, speed=args.speed, tool=tool, passes=args.passes)
            if args.output:
                if args.output.resolve() == args.design.resolve():
                    raise ValueError("Output must differ from the source design.")
                args.output.write_bytes(payload)
            result = {"operation": "cut", "dry_run": not args.send, "bytes": len(payload),
                      "sha256": hashlib.sha256(payload).hexdigest(), "automatic_retry": False}
            if args.send:
                with device_lock():
                    result["bytes_sent"] = transport.send_tcp(payload, args.host, args.port)
                result["delivery"] = "local_transport_accepted"
                result["physical_completion"] = "unknown"
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"verona-cut: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

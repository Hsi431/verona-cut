#!/usr/bin/env python3
"""Check the public tree or built archives without printing sensitive values."""
import ipaddress
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
LOCAL_ONLY = {".git", ".venv", "__pycache__", "build", "dist", ".pytest_cache"}
FORBIDDEN_DIRS = {"research", "captures", "dumps", "firmware", ".agent", ".codegraph"}
FORBIDDEN_SUFFIXES = {".pcap", ".pcapng", ".bin", ".dll", ".exe", ".dylib", ".so",
                      ".o", ".elf", ".dmg", ".lds", ".log", ".pyc"}
PATTERNS = {
    "personal absolute path": re.compile(r"/(?:home|Users|media)/[^\s\"'<>]+"),
    "MAC address": re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential-shaped value": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{32,})\b"),
    "wireless network name": re.compile(r"(?i)\bssid\s*[:=]\s*[\"'][^\"']+[\"']"),
}
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def findings(name, data):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        yield "non-relative archive path"
    if FORBIDDEN_DIRS.intersection(path.parts):
        yield "private/research directory"
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        yield "excluded file type"
    if path.suffix.lower() in {".jdesign", ".plt"} and "examples" not in path.parts:
        yield "private design/job outside examples"
    if path.name.startswith(".env") and path.name != ".env.example":
        yield "local environment file"
    if path.suffix.lower() == ".png":
        if not ({"examples", "docs"} & set(path.parts)):
            yield "image outside documented asset directories"
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        yield "unexpected binary content"
        return
    for category, pattern in PATTERNS.items():
        if pattern.search(text):
            yield category
    # The checker contains the network ranges it detects, not a device address.
    if path.name != "check-release.py":
        for match in IPV4.finditer(text):
            try:
                address = ipaddress.ip_address(match[0])
            except ValueError:
                continue
            if any(address in network for network in PRIVATE_NETWORKS):
                yield "hardcoded private IPv4 address"
                break


def entries(archive=None):
    if archive is None:
        for path in sorted(ROOT.rglob("*")):
            relative = path.relative_to(ROOT)
            if any(part in LOCAL_ONLY or part.endswith(".egg-info") for part in relative.parts):
                continue
            if path.is_symlink():
                yield str(relative), b"", "symlink requires review"
            elif path.is_file():
                yield str(relative), path.read_bytes(), None
    elif zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            for name in package.namelist():
                if not name.endswith("/"):
                    yield name, package.read(name), None
    else:
        with tarfile.open(archive, "r:*") as package:
            for member in package.getmembers():
                if member.isfile():
                    yield member.name, package.extractfile(member).read(), None
                elif not member.isdir():
                    yield member.name, b"", "archive link requires review"


def main(argv=None):
    archives = list(sys.argv[1:] if argv is None else argv)
    failures = checked = 0
    for archive in archives or [None]:
        for name, data, error in entries(archive):
            checked += 1
            for category in ([error] if error else findings(name, data)):
                print(f"{archive or 'tree'}: {name}: {category}")
                failures += 1
    print(f"Checked {checked} files; {failures} findings. No sensitive values printed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

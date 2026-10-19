"""Same-user Unix-socket client for running Verona editors; no device access."""
import json
import os
from pathlib import Path
import re
import socket
import stat


_EDITOR_ID = re.compile(r"[0-9]+-[0-9a-f]{12}")


def editor_directory(*, create=False):
    base = Path(os.environ.get("XDG_RUNTIME_DIR", Path.home() / ".local/state"))
    directory = base / "verona" / "editors"
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.exists():
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise PermissionError("The editor socket directory must be private to this user.")
    return directory


def editor_request(editor_id, command, arguments=None, *, directory=None, timeout=60):
    if not isinstance(editor_id, str) or not _EDITOR_ID.fullmatch(editor_id):
        raise ValueError("Choose an editor_id returned by verona_list_editors.")
    path = (Path(directory) if directory else editor_directory()) / f"{editor_id}.sock"
    request = json.dumps({"command": command, "arguments": arguments or {}},
                         allow_nan=False).encode() + b"\n"
    if len(request) > 65536:
        raise ValueError("The editor request exceeds 64 KiB.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(request)
        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                raise RuntimeError("The editor closed before returning a complete response.")
            response.extend(chunk)
            if len(response) > 8 * 1024 * 1024:
                raise RuntimeError("The editor response exceeds 8 MiB.")
    data = json.loads(response)
    if not data.get("ok"):
        raise ValueError(data.get("error", "The editor rejected the request."))
    return data["result"]


def list_editors(*, directory=None):
    directory = Path(directory) if directory else editor_directory()
    result = []
    for path in sorted(directory.glob("*.sock")):
        if not _EDITOR_ID.fullmatch(path.stem):
            continue
        try:
            state = editor_request(path.stem, "state", directory=directory, timeout=5)
            result.append({key: state[key] for key in
                           ("editor_id", "file", "dirty", "active_tab", "revision")})
        except (OSError, ValueError, RuntimeError):
            continue
    return result

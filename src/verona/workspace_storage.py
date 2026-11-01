"""Private, atomic JSON storage for local workspace data."""
import json
import os
from pathlib import Path
import tempfile


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise PermissionError("Workspace data directory must be private to this user.")
    return path


def write_private_json(path, data, *, max_bytes):
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > max_bytes:
        raise ValueError("Workspace data exceeds its size limit.")
    path = Path(path)
    handle, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)

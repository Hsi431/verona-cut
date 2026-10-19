"""One non-blocking, per-user lock for GUI, CLI and MCP operations."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


class DeviceBusyError(RuntimeError):
    pass


@contextmanager
def device_lock(path=None):
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    path = Path(path) if path else state / "verona/mcp-device.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeviceBusyError("Another Verona operation is using the device lock; no data sent.") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

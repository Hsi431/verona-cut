"""Small private JSON history for explicit GUI firmware attempts."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid


def _history_dir(directory=None):
    if directory is not None:
        return Path(directory)
    state_home = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
    return Path(state_home) / "verona" / "firmware-history"


def _atomic_json(path, record):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def begin_attempt(*, file_name, sha256, candidate_versions, transport,
                  expected_identity, directory=None):
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    path = _history_dir(directory) / f"attempt-{timestamp}-{uuid.uuid4().hex}.json"
    record = {
        "started_utc": now.isoformat(),
        "status": "started",
        "file_name": Path(file_name).name,
        "sha256": sha256,
        "candidate_versions": list(candidate_versions),
        "transport": transport,
        "expected_identity": {key: expected_identity[key] for key in ("MODE", "HVER", "SVER")},
        "stage": "started",
        "bytes_header_sent": None,
        "bytes_body_sent": None,
        "counts_lower_bounds": True,
        "version_verified": False,
    }
    _atomic_json(path, record)
    return path


def finish_attempt(path, *, status, stage, bytes_header_sent, bytes_body_sent,
                   error_type=None, error_errno=None, error_message=None,
                   counts_lower_bounds=True):
    path = Path(path)
    with path.open("r", encoding="utf-8") as stream:
        record = json.load(stream)
    if record.get("status") != "started":
        raise ValueError("Only a started firmware attempt can be completed")
    if status not in ("pgok_received", "failed"):
        raise ValueError("Invalid firmware history result")
    header_count = None if bytes_header_sent is None else max(0, int(bytes_header_sent))
    body_count = None if bytes_body_sent is None else max(0, int(bytes_body_sent))
    record.update({
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "stage": stage,
        "bytes_header_sent": header_count,
        "bytes_body_sent": body_count,
        "counts_lower_bounds": bool(counts_lower_bounds),
        "version_verified": False,
    })
    if error_type is not None:
        record["error"] = {"type": str(error_type)[:100], "errno": error_errno}
        if error_message:
            record["error"]["message"] = str(error_message)[:500]
    _atomic_json(path, record)
    return record


def latest_attempt(directory=None):
    root = _history_dir(directory)
    paths = sorted(root.glob("attempt-*.json"), reverse=True)
    if not paths:
        return None
    with paths[0].open("r", encoding="utf-8") as stream:
        contents = stream.read(64 * 1024 + 1)
    if len(contents) > 64 * 1024:
        raise ValueError("Latest firmware history record exceeds the read limit")
    record = json.loads(contents)
    if not isinstance(record, dict) or record.get("status") not in (
            "started", "pgok_received", "failed"):
        raise ValueError("Latest firmware history record is invalid")
    return record

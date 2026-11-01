"""Design serialization and atomic publication, independent of editor widgets."""
import errno
import json
import os
from pathlib import Path
import shutil
import tempfile


def publish_new_file(source, target) -> None:
    """Give a fully written file a new name, never replacing or truncating one."""
    try:
        # A hard link appears complete and fails if the name already exists.
        os.link(source, target)
    except FileExistsError:
        raise
    except OSError:
        # Some removable-media filesystems have no hard links.
        with open(target, "xb") as output, open(source, "rb") as data:
            try:
                shutil.copyfileobj(data, output)
            except BaseException:
                output.close()
                Path(target).unlink(missing_ok=True)
                raise



def write_document(filename, data, *, overwrite=True):
    # Serialize before touching the target, then replace it in one step so a
    # failed save never leaves a truncated design behind.
    target = Path(filename)
    if target.suffix.lower() == ".lds":
        from .lds_export import encode_lds
        payload = encode_lds(data)
    else:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    if not overwrite and target.exists():
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(target))
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(payload)
        if overwrite and target.exists():
            mode = target.stat().st_mode & 0o777
        else:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(temporary, mode)
        if overwrite:
            if target.suffix.lower() == ".lds" and target.exists():
                backup = target.with_name(target.name + ".bak")
                try:
                    backup_output = backup.open("xb")
                except FileExistsError:
                    pass
                else:
                    try:
                        with backup_output, target.open("rb") as original:
                            os.chmod(backup, mode)
                            shutil.copyfileobj(original, backup_output)
                    except BaseException:
                        backup.unlink(missing_ok=True)
                        raise
            os.replace(temporary, target)
        else:
            publish_new_file(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)

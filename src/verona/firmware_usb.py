"""Bounded duplex firmware I/O through Linux's existing usblp driver."""
import errno
import math
import os
from pathlib import Path
import re
import select
import socket
import stat


def _usb_interface(node):
    if not re.fullmatch(r"/dev/usb/lp[0-9]+", str(node)):
        raise ValueError("Select an explicit /dev/usb/lpN cutter device")
    interface = (Path("/sys/class/usbmisc") / Path(node).name / "device").resolve(strict=True)
    device = interface.parent
    if ((device / "idVendor").read_text().strip().lower() != "0483"
            or (device / "idProduct").read_text().strip().lower() != "5750"
            or (interface / "bInterfaceClass").read_text().strip() != "07"):
        raise ValueError("USB device is not the supported printer-class cutter")
    return interface


def list_usb_nodes():
    """List matching nodes using sysfs metadata; never open a device."""
    nodes = []
    for node in sorted(Path("/dev/usb").glob("lp*")):
        try:
            _usb_interface(node)
        except (OSError, ValueError):
            continue
        nodes.append(str(node))
    return nodes


class UsbConnection:
    def __init__(self, node, timeout):
        self.fd = None
        self.settimeout(timeout)
        _usb_interface(node)
        self.fd = os.open(node, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            if not stat.S_ISCHR(os.fstat(self.fd).st_mode):
                raise ValueError("USB path is not a character device")
            try:
                pending = os.read(self.fd, 256)
            except BlockingIOError:
                pending = b""
            if pending:
                raise ValueError("Pending USB data; no request sent")
        except BaseException:
            self.close()
            raise

    def settimeout(self, value):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise ValueError("USB timeout must be a finite nonnegative number")
        self.timeout = value

    def _wait(self, writing):
        readable, writable, _ = select.select(
            [] if writing else [self.fd], [self.fd] if writing else [], [], self.timeout)
        if not (writable if writing else readable):
            if self.timeout == 0:
                raise BlockingIOError(errno.EAGAIN, "USB not ready")
            raise socket.timeout("USB operation deadline exceeded")

    def recv(self, count):
        self._wait(False)
        return os.read(self.fd, count)

    def send(self, data):
        self._wait(True)
        return os.write(self.fd, data)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

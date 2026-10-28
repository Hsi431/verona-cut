"""Route desktop launches to tabs in the same user's running workspace."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QLockFile, QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class DesktopSession(QObject):
    openRequested = Signal(list)

    def __init__(self, parent=None, *, name=None):
        super().__init__(parent)
        session = [os.getuid(), os.environ.get("XDG_SESSION_ID", ""),
                   os.environ.get("WAYLAND_DISPLAY", ""), os.environ.get("DISPLAY", "")]
        name = name or "verona-" + hashlib.sha256(json.dumps(session).encode()).hexdigest()[:20]
        self.endpoint = str(Path(tempfile.gettempdir()) / name)
        self.lock = QLockFile(self.endpoint + ".lock")
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        self.connections = {}

    def start(self, paths):
        """Return true for the owner; a later launch forwards and exits."""
        if self.lock.tryLock(0):
            QLocalServer.removeServer(self.endpoint)
            if not self.server.listen(self.endpoint):
                self.lock.unlock()
                raise OSError(self.server.errorString())
            return True
        connection = QLocalSocket()
        connection.connectToServer(self.endpoint)
        if not connection.waitForConnected(3000):
            raise OSError(connection.errorString())
        payload = json.dumps([str(Path(path).expanduser().resolve()) for path in paths],
                             ensure_ascii=False).encode() + b"\n"
        connection.write(payload)
        connection.flush()
        if not connection.waitForReadyRead(5000) or bytes(connection.readAll()) != b"ok\n":
            raise OSError("The running Verona workspace did not accept the open request.")
        connection.disconnectFromServer()
        return False

    def _accept(self):
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            self.connections[connection] = bytearray()
            timer = QTimer(connection)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda c=connection: c.abort())
            timer.start(5000)
            connection.readyRead.connect(lambda c=connection, t=timer: self._read(c, t))
            connection.disconnected.connect(lambda c=connection: self._disconnected(c))

    def _read(self, connection, timer):
        buffer = self.connections[connection]
        buffer.extend(bytes(connection.readAll()))
        if len(buffer) > 65536:
            connection.abort()
            return
        if b"\n" not in buffer:
            return
        timer.stop()
        try:
            paths = json.loads(buffer)
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                raise ValueError("Invalid file list")
        except (ValueError, UnicodeError):
            connection.abort()
            return
        connection.write(b"ok\n")
        connection.flush()
        connection.disconnectFromServer()
        QTimer.singleShot(0, lambda: self.openRequested.emit(paths))

    def _disconnected(self, connection):
        self.connections.pop(connection, None)
        connection.deleteLater()

    def close(self):
        for connection in list(self.connections):
            connection.abort()
        self.server.close()
        self.lock.unlock()

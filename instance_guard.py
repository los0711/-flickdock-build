"""Per-user single instance with a local activation message, no network port."""
from __future__ import annotations

import hashlib
from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class InstanceGuard(QObject):
    activated = Signal()

    def __init__(self, parent, profile: str):
        super().__init__(parent)
        self.name = 'FlickDock.Desktop.' + hashlib.sha256(profile.lower().encode()).hexdigest()[:20]
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._connected)
        self.sockets = set()

    def acquire(self) -> bool:
        client = QLocalSocket(self)
        client.connectToServer(self.name)
        if client.waitForConnected(150):
            client.write(b'show\n')
            client.waitForBytesWritten(150)
            client.disconnectFromServer()
            client.deleteLater()
            return False
        client.abort()
        client.deleteLater()
        if self.server.listen(self.name):
            return True
        # Check once more for a simultaneous first launch before removing a
        # stale endpoint. On Windows the named pipe vanishes with its process.
        client = QLocalSocket(self)
        client.connectToServer(self.name)
        if client.waitForConnected(150):
            client.write(b'show\n')
            client.waitForBytesWritten(150)
            client.disconnectFromServer()
            client.deleteLater()
            return False
        client.abort()
        client.deleteLater()
        QLocalServer.removeServer(self.name)
        return self.server.listen(self.name)

    def _connected(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.sockets.add(socket)
            socket.readyRead.connect(lambda item=socket: self._read(item))
            socket.disconnected.connect(lambda item=socket: self._dispose(item))
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket):
        if b'show' in bytes(socket.readAll()):
            self.activated.emit()
        socket.disconnectFromServer()

    def _dispose(self, socket):
        self.sockets.discard(socket)
        socket.deleteLater()

    def close(self):
        self.server.close()
        for socket in list(self.sockets):
            socket.abort()
        self.sockets.clear()

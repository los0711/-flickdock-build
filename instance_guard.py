"""Per-user single instance with a local activation message, no network port."""
from __future__ import annotations

import hashlib
from pathlib import Path
from PySide6.QtCore import QObject, Signal, QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class InstanceGuard(QObject):
    activated = Signal()

    def __init__(self, parent, profile: str):
        super().__init__(parent)
        self.profile=Path(profile).resolve()
        self.name = 'FlickDock.Desktop.' + hashlib.sha256(str(self.profile).lower().encode()).hexdigest()[:20]
        self.lock=QLockFile(str(self.profile/'desktop-instance.lock'))
        # A running application is not stale because a socket connection is
        # delayed or because the lock is old. Qt still removes dead-PID locks.
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._connected)
        self.sockets = set()

    def acquire(self) -> bool:
        self.profile.mkdir(parents=True,exist_ok=True)
        if self.lock.tryLock(0):
            if self.server.listen(self.name):return True
            # Only the process owning the profile may retire a stale endpoint.
            QLocalServer.removeServer(self.name)
            if self.server.listen(self.name):return True
            self.lock.unlock()
            raise OSError('无法建立 FlickDock 本地激活入口：'+self.server.errorString())
        if self.lock.error()!=QLockFile.LockError.LockFailedError:
            raise OSError('无法锁定 FlickDock 应用数据目录：'+self.lock.error().name)
        client = QLocalSocket(self)
        client.connectToServer(self.name)
        if client.waitForConnected(150):
            client.write(b'show\n')
            client.waitForBytesWritten(150)
            client.disconnectFromServer()
        else:client.abort()
        client.deleteLater()
        return False

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
        self.lock.unlock()

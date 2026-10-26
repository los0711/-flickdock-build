"""Account-scoped cloud change hints; the sync journal remains authoritative.

WebSocket messages carry no content, path, token or account details. A missed
hint is harmless because ShelfSync retains bounded periodic reconciliation.
"""
from __future__ import annotations

import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtWebSockets import QWebSocket


class CloudEvents(QObject):
    content_changed = Signal()
    devices_changed = Signal()

    def __init__(self, account, parent=None, *, enabled=True):
        super().__init__(parent)
        self.account = account
        self.enabled = enabled
        self.closed = False
        self._identity = ''
        self._attempt = 0
        self.socket = QWebSocket(parent=self)
        self.socket.connected.connect(self._connected)
        self.socket.disconnected.connect(self._disconnected)
        self.socket.textMessageReceived.connect(self._message)
        self.socket.errorOccurred.connect(self._disconnected)
        self.retry = QTimer(self); self.retry.setSingleShot(True)
        self.retry.timeout.connect(self._connect)
        self.content_debounce = QTimer(self); self.content_debounce.setSingleShot(True)
        self.content_debounce.timeout.connect(self.content_changed)
        self.devices_debounce = QTimer(self); self.devices_debounce.setSingleShot(True)
        self.devices_debounce.timeout.connect(self.devices_changed)
        account.account_changed.connect(self._account_changed)

    def _account_changed(self, account):
        self.retry.stop(); self.content_debounce.stop(); self.devices_debounce.stop()
        self.socket.abort()
        self._attempt = 0
        self._identity = account.get('id','') if isinstance(account,dict) else ''
        if self.enabled and self._identity and self.account.api.token:
            self.retry.start(0)

    def _connect(self):
        if self.closed or not self.enabled or not self._identity or not self.account.signed_in:
            return
        url = QUrl(self.account.api.origin)
        if url.scheme() != 'https' or not url.host() or url.userName() or url.password() or url.hasQuery() or url.hasFragment():
            return
        url.setScheme('wss'); url.setPath('/v1/events')
        request = QNetworkRequest(url)
        request.setRawHeader(b'Authorization', ('Bearer '+self.account.api.token).encode('ascii'))
        self.socket.open(request)

    def _connected(self):
        self._attempt = 0
        self.content_debounce.start(50)
        self.devices_debounce.start(50)

    def _disconnected(self, *_args):
        if self.closed or not self.enabled or not self._identity or not self.account.signed_in or self.retry.isActive():
            return
        # Back off to at most one reconnect per five minutes during outages.
        self._attempt = min(self._attempt+1, 9)
        self.retry.start(min(300000, 1000 * (2 ** (self._attempt-1))))

    def _message(self, message):
        if len(message)>256: return
        try: value=json.loads(message)
        except (ValueError,TypeError): return
        if not isinstance(value,dict) or value.get('schema_version')!=1:return
        if value.get('type')=='content': self.content_debounce.start(90)
        elif value.get('type')=='devices': self.devices_debounce.start(250)

    def close(self):
        self.closed=True; self.retry.stop()
        self.content_debounce.stop(); self.devices_debounce.stop()
        self.socket.abort()

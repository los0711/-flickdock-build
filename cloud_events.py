"""Account-scoped cloud change hints; the sync journal remains authoritative.

Shelf hints carry no file bodies. Bounded committed clipboard text may travel
over this authenticated TLS socket; the journal still recovers missed hints.
"""
from __future__ import annotations

import json
import re
import secrets

from PySide6.QtCore import QObject, QTimer, QUrl, Signal,Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtNetwork import QNetworkRequest,QNetworkInformation
from PySide6.QtWebSockets import QWebSocket
from app_version import APP_VERSION
from network_environment import proxy_routes
from PySide6.QtNetwork import QNetworkProxy


CONNECT_TIMEOUT_MS=12000
RECONNECT_BASE_MS=1000
RECONNECT_MAX_MS=30000
RECONNECT_JITTER_PERCENT=20


class CloudEvents(QObject):
    content_changed = Signal()
    devices_changed = Signal()
    clipboard_changed = Signal()
    clipboard_payload = Signal(dict)
    clipboard_delivery = Signal(dict)
    transfer_progress = Signal(dict)

    def __init__(self, account, parent=None, *, enabled=True):
        super().__init__(parent)
        self.account = account
        self.enabled = enabled
        self.closed = False
        self._identity = ''
        self._attempt = 0
        self._inline_version=0
        self._proxy_routes=[];self._proxy_index=0
        self.socket = QWebSocket(parent=self)
        self.socket.connected.connect(self._connected)
        self.socket.disconnected.connect(self._disconnected)
        self.socket.textMessageReceived.connect(self._message)
        self.socket.errorOccurred.connect(self._disconnected)
        self.connect_timeout=QTimer(self);self.connect_timeout.setSingleShot(True);self.connect_timeout.timeout.connect(self._connect_timed_out)
        self.retry = QTimer(self); self.retry.setSingleShot(True)
        self.retry.timeout.connect(self._connect)
        self.retry.setTimerType(Qt.TimerType.PreciseTimer)
        self.content_debounce = QTimer(self); self.content_debounce.setSingleShot(True)
        self.content_debounce.timeout.connect(self.content_changed)
        self.devices_debounce = QTimer(self); self.devices_debounce.setSingleShot(True)
        self.devices_debounce.timeout.connect(self.devices_changed)
        account.account_changed.connect(self._account_changed)
        origin_signal=getattr(getattr(account,'api',None),'origin_changed',None)
        if origin_signal is not None:
            origin_signal.connect(lambda _:self._account_changed(account.account) if account.signed_in else None)
        self._network_info=None
        if enabled and QGuiApplication.instance() and QGuiApplication.platformName() not in ('offscreen','minimal'):
            QNetworkInformation.loadDefaultBackend();self._network_info=QNetworkInformation.instance()
            if self._network_info is not None:
                self._network_info.reachabilityChanged.connect(self._network_changed)
                self._network_info.transportMediumChanged.connect(self._network_changed)

    def _account_changed(self, account):
        self.retry.stop();self.connect_timeout.stop()
        self.content_debounce.stop(); self.devices_debounce.stop()
        self.socket.abort()
        self._attempt = 0
        self._inline_version=0
        self._proxy_routes=[];self._proxy_index=0
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
        request.setRawHeader(b'User-Agent',('FlickDock/'+APP_VERSION+' (CloudSync)').encode('ascii'))
        routes=proxy_routes(self.account.api.origin)
        if getattr(self.account.api,'prefers_direct',False):routes.sort(key=lambda proxy:proxy.type()!=QNetworkProxy.NoProxy)
        if routes!=self._proxy_routes:self._proxy_routes=routes;self._proxy_index=0
        self.socket.setProxy(routes[self._proxy_index%len(routes)])
        self.connect_timeout.start(CONNECT_TIMEOUT_MS)
        self.socket.open(request)

    def _connected(self):
        self.connect_timeout.stop()
        recovered = self._attempt > 0
        self._attempt = 0
        # Login already fetches shelf changes and devices. Reconcile only
        # after a dropped socket, when hints may have been missed.
        if recovered:
            self.devices_debounce.start(50)

    def _disconnected(self, *_args):
        self.connect_timeout.stop()
        if self.closed or not self.enabled or not self._identity or not self.account.signed_in or self.retry.isActive():
            return
        self._attempt = min(self._attempt+1, 6)
        self._proxy_index+=1
        base=min(RECONNECT_MAX_MS,RECONNECT_BASE_MS*2**(self._attempt-1))
        spread=base*RECONNECT_JITTER_PERCENT//100
        self.retry.start(min(RECONNECT_MAX_MS,base-spread+secrets.randbelow(2*spread+1)))

    def _connect_timed_out(self):
        if self.closed:return
        self.socket.abort();self._disconnected()

    def _network_changed(self,*_):
        # OS notifications invalidate a stale socket without sending probes.
        # Reachability is only a hint, never permission to suppress a connect.
        if self.closed or not self.enabled or not self._identity or not self.account.signed_in:return
        self.socket.abort();self._disconnected()

    def _message(self, message):
        if len(message)>65536: return
        try: value=json.loads(message)
        except (ValueError,TypeError): return
        if not isinstance(value,dict) or value.get('schema_version')!=1:return
        if value.get('type')=='clipboard_delivery':
            from clipboard_delivery import delivery_state
            try:receipt=delivery_state(value.get('delivery'))
            except ValueError:return
            self.clipboard_delivery.emit(receipt);return
        if value.get('type')=='clipboard_inline':
            head=value.get('clipboard')
            if (not isinstance(head,dict) or head.get('kind')!='text' or not head.get('ready') or
                    type(head.get('version')) is not int or not 1<=head['version']<=2**53-1 or
                    not isinstance(head.get('inline'),str) or len(head['inline'])>43692 or
                    type(head.get('size')) is not int or not 0<head['size']<=32768):return
            self._inline_version=max(self._inline_version,head['version'])
            self.clipboard_payload.emit(value);return
        if len(message)>512:return
        if value.get('type')=='content': self.content_debounce.start(90)
        elif value.get('type')=='clipboard':
            if type(value.get('version')) is int and value['version']<=self._inline_version:return
            self.clipboard_changed.emit()
        elif value.get('type')=='devices': self.devices_debounce.start(250)
        elif value.get('type')=='progress':
            if not re.fullmatch('[a-f0-9-]{36}',str(value.get('upload_id',''))):return
            if any(type(value.get(key)) is not int for key in ('received_bytes','total','at')):return
            if not 0<=value['received_bytes']<=value['total']<=2**53-1:return
            self.transfer_progress.emit(value)

    def close(self):
        self.closed=True;self.retry.stop();self.connect_timeout.stop()
        if self._network_info is not None:
            self._network_info.reachabilityChanged.disconnect(self._network_changed)
            self._network_info.transportMediumChanged.disconnect(self._network_changed)
            self._network_info=None
        self.content_debounce.stop(); self.devices_debounce.stop()
        self.socket.abort()

"""Real Qt WebSockets: no idle heartbeat, bounded reconnect, one shared recovery wake."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time,unittest
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QObject,QTimer,QUrl,Signal
from PySide6.QtNetwork import QHostAddress,QAbstractSocket
from PySide6.QtWebSockets import QWebSocketServer
from PySide6.QtWidgets import QApplication
from cloud_events import CloudEvents,RECONNECT_MAX_MS
from sync_scheduler import SyncScheduler

APP=QApplication.instance() or QApplication([])

def until(predicate,seconds=2):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        APP.processEvents()
        if predicate():return
        time.sleep(.002)
    raise AssertionError('Native event connection did not settle')

class Account(QObject):
    account_changed=Signal(object)
    def __init__(self):
        super().__init__();self.signed_in=True
        self.api=SimpleNamespace(origin='https://events.example.invalid',token='disposable-fixture-token')

class EventPushTests(unittest.TestCase):
    def setUp(self):
        self.account=Account();self.stream=CloudEvents(self.account);self.stream._identity='disposable-account'
        self.server=QWebSocketServer('isolated-event-test',QWebSocketServer.SslMode.NonSecureMode)
        self.assertTrue(self.server.listen(QHostAddress.SpecialAddress.LocalHost,0));self.peers=[]
        self.server.newConnection.connect(lambda:self.peers.append(self.server.nextPendingConnection()))
        self.url=QUrl(f'ws://127.0.0.1:{self.server.serverPort()}');self.stream.socket.open(self.url)
        until(lambda:bool(self.peers) and self.stream.socket.state()==QAbstractSocket.ConnectedState)

    def tearDown(self):
        self.stream.close()
        for peer in self.peers:peer.abort();peer.deleteLater()
        self.server.close();self.server.deleteLater();self.stream.deleteLater();APP.processEvents()

    def test_connected_idle_has_no_active_timers_or_application_messages(self):
        messages=[];self.peers[0].textMessageReceived.connect(messages.append)
        self.assertFalse(any(timer.isActive() for timer in self.stream.findChildren(QTimer)))
        end=time.monotonic()+.1
        while time.monotonic()<end:APP.processEvents();time.sleep(.002)
        self.assertEqual(messages,[]);self.assertFalse(hasattr(self.stream,'heartbeat'))

    def test_closed_socket_reconnects_and_wakes_each_registered_sync_once(self):
        scheduler=SyncScheduler(connected=False);calls=[]
        for kind in ('shelf','clipboard'):
            scheduler.register(kind,lambda kind=kind:calls.append(kind)).configure(enabled=True)
        scheduler.bind_socket(self.stream.socket)
        try:
            until(lambda:len(calls)==2);calls.clear()
            self.peers[0].close();until(lambda:self.stream.retry.isActive())
            self.assertFalse(scheduler.connected);self.assertFalse(scheduler.timer.isActive())
            self.assertEqual(calls,[]);self.stream.retry.stop()
            self.stream.socket.open(self.url);until(lambda:len(calls)==2)
            self.assertCountEqual(calls,['shelf','clipboard']);self.assertEqual(self.stream._attempt,0)
            self.assertFalse(scheduler.timer.isActive())
        finally:scheduler.close()

    def test_exponential_backoff_has_jitter_and_never_exceeds_thirty_seconds(self):
        waits=[]
        with patch('cloud_events.secrets.randbelow',return_value=0):
            for _ in range(8):
                self.stream.retry.stop();self.stream._disconnected();waits.append(self.stream.retry.interval())
        self.assertEqual(waits[:5],[800,1600,3200,6400,12800])
        self.assertTrue(all(0<delay<=RECONNECT_MAX_MS for delay in waits))
        self.stream._attempt=0;self.stream.retry.stop()
        with patch('cloud_events.secrets.randbelow',side_effect=lambda limit:limit-1):self.stream._disconnected()
        self.assertEqual(self.stream.retry.interval(),1200)

    def test_network_change_and_logout_release_pending_connections(self):
        self.stream._network_changed();until(lambda:self.stream.retry.isActive())
        self.account.signed_in=False;self.account.account_changed.emit(None)
        self.assertFalse(self.stream.retry.isActive());self.assertFalse(self.stream.connect_timeout.isActive())
        self.stream.close();self.stream._connect_timed_out()
        self.assertFalse(any(timer.isActive() for timer in self.stream.findChildren(QTimer)))

if __name__=='__main__':unittest.main()

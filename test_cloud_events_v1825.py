"""Native control pings detect a silent socket without account polling traffic."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocketServer
from PySide6.QtWidgets import QApplication

from cloud_events import CloudEvents, HEARTBEAT_INTERVAL_MS

APP=QApplication.instance() or QApplication([])


def until(predicate,seconds=2):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        APP.processEvents()
        if predicate():return
        time.sleep(.002)
    raise AssertionError('Cloud event connection did not converge')


class Account(QObject):
    account_changed=Signal(object)
    def __init__(self):
        super().__init__()
        self.signed_in=True
        self.api=SimpleNamespace(origin='https://events.example.invalid',token='disposable-fixture-token')


class EventHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.account=Account()
        self.stream=CloudEvents(self.account)
        self.stream._identity='disposable-account'
        self.server=QWebSocketServer('isolated-event-test',QWebSocketServer.SslMode.NonSecureMode)
        self.assertTrue(self.server.listen(QHostAddress.SpecialAddress.LocalHost,0))
        self.peers=[]
        self.server.newConnection.connect(lambda:self.peers.append(self.server.nextPendingConnection()))
        self.stream.socket.open(QUrl(f'ws://127.0.0.1:{self.server.serverPort()}'))
        until(lambda:bool(self.peers) and self.stream.heartbeat.isActive())

    def tearDown(self):
        self.stream.close()
        for peer in self.peers:peer.abort();peer.deleteLater()
        self.server.close();self.server.deleteLater();self.stream.deleteLater()
        APP.processEvents()

    def test_actual_native_pong_rearms_without_application_messages(self):
        messages=[]
        self.peers[0].textMessageReceived.connect(messages.append)
        self.stream._ping()
        self.assertIsNotNone(self.stream._probe_payload)
        until(lambda:self.stream._probe_payload is None)
        self.assertTrue(self.stream.heartbeat.isActive())
        self.assertLessEqual(self.stream.heartbeat.remainingTime(),HEARTBEAT_INTERVAL_MS+1000)
        self.assertFalse(self.stream.watchdog.isActive())
        self.assertEqual(messages,[])

    def test_missing_pong_closes_and_recovers_missed_content(self):
        with patch.object(self.stream.socket,'ping'):
            self.stream._ping()
        self.stream._pong(1,b'stale-pong')
        self.assertIsNotNone(self.stream._probe_payload)
        self.stream.watchdog.start(15)
        until(lambda:self.stream.retry.isActive())
        self.assertFalse(self.stream.heartbeat.isActive())
        self.assertIsNone(self.stream._probe_payload)
        self.assertEqual(self.stream._attempt,1)
        self.stream.retry.stop()
        recovered=[]
        self.stream.content_changed.connect(lambda:recovered.append(True))
        self.stream.socket.open(QUrl(f'ws://127.0.0.1:{self.server.serverPort()}'))
        until(lambda:bool(recovered))
        self.assertEqual(self.stream._attempt,0)

    def test_logout_and_close_release_pending_heartbeat(self):
        with patch.object(self.stream.socket,'ping'):
            self.stream._ping()
        self.account.signed_in=False
        self.account.account_changed.emit(None)
        self.assertFalse(self.stream.heartbeat.isActive())
        self.assertFalse(self.stream.watchdog.isActive())
        self.assertFalse(self.stream.retry.isActive())
        self.assertIsNone(self.stream._probe_payload)
        self.stream.close();self.stream._stalled()
        self.assertFalse(self.stream.retry.isActive())


if __name__=='__main__':unittest.main()

"""A cloud timeout must recover even when an event hint arrives in backoff."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from cloud_api import CloudError
from shelf_sync import ShelfSync


def spin(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(.002)
    return False


class Api(QObject):
    def __init__(self, first_error):
        super().__init__()
        self.token = 'fixture-session'
        self.calls = 0
        self.pending = set()
        self.first_error = first_error

    def request(self, method, path, callback, **kwargs):
        assert method == 'GET' and path.startswith('/v1/sync/changes?')
        self.calls += 1
        attempt = self.calls
        key = uuid4().hex
        self.pending.add(key)

        def complete():
            if key not in self.pending:
                return
            self.pending.remove(key)
            if attempt == 1:
                callback(None, self.first_error)
            else:
                callback({'changes': [], 'transfers': [], 'cursor': '0', 'has_more': False}, None)

        QTimer.singleShot(0, complete)
        return key

    def cancel(self, key):
        self.pending.discard(key)


class Service(QObject):
    account_changed = Signal(object)

    def __init__(self, first_error):
        super().__init__()
        self.api = Api(first_error)

    @property
    def signed_in(self):
        return bool(self.api.token)


class Adapter:
    def __init__(self):
        self.view = {'permanent': [], 'texts': {}, 'metadata': {}}

    def snapshot(self):
        return copy.deepcopy(self.view)

    def restore(self, view):
        self.view = copy.deepcopy(view)

    def title(self, ref):
        return Path(ref).name


app = QApplication.instance() or QApplication([])
for label, first_error in (
    ('network timeout', CloudError('同步请求超时，请检查网络。')),
    ('monthly quota', CloudError('月度额度不足。', 429, 'STORAGE_REQUEST_BUDGET')),
):
    with tempfile.TemporaryDirectory(prefix='flickdock-sync-timeout-recovery-') as directory:
        service = Service(first_error)
        engine = ShelfSync(directory, service, Adapter(), poll_ms=600000)
        # Compress the production backoff while keeping the real Qt debounce
        # and account-scoped sync state machine.
        engine._retry_delay = lambda error: .2
        statuses = []
        engine.status_changed.connect(statuses.append)
        service.account_changed.emit({'id': 'fixture-account'})
        assert spin(lambda: service.api.calls == 1 and not engine.running and engine.debounce.isActive())
        assert engine.state['not_before'] > time.time()
        # Opening the overview or receiving a WebSocket hint before the
        # scheduled retry used to consume that same timer and strand sync
        # until the 5-minute poll (or a manual logout/login).
        engine.request_refresh()
        assert spin(lambda: service.api.calls == 2 and not engine.running, 1.0), (label, service.api.calls, statuses)
        assert statuses[-1] == '搁架已同步。'
        if label == 'network timeout':
            # The cleanup retry has its own deadline. An early overview
            # refresh may still pull changes, but must retain that deadline.
            engine.state['cleanup_not_before'] = time.time() + .2
            engine.debounce.start(201)
            engine.request_refresh()
            assert spin(lambda: service.api.calls == 3 and not engine.running, .5)
            assert spin(lambda: service.api.calls == 4 and not engine.running, 1.0), (service.api.calls, statuses)
        engine.close()
    print('PASS '+label+', early change hint, automatic retry without re-login')

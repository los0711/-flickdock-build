"""Fresh downloads reuse the transport's verified digest without rereading each part."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication
import shelf_sync


class Transport(QObject):
    unauthorized = Signal()

    def __init__(self, record, payload, chunk_size):
        super().__init__()
        self.record, self.payload, self.chunk_size = record, payload, chunk_size
        self.requests = {}

    def request(self, method, path, callback, **options):
        key = uuid4().hex
        self.requests[key] = True

        def deliver():
            if not self.requests.pop(key, None):
                return
            try:
                if path.endswith('/content'):
                    result = {'record':self.record, 'chunk_size':self.chunk_size,
                              'part_count':len(self.payload) // self.chunk_size}
                else:
                    index = int(path.split('/parts/')[1].split('?')[0])
                    data = self.payload[index*self.chunk_size:(index+1)*self.chunk_size]
                    assert options['expected'] == {'size':len(data), 'with_digest':True}
                    target = Path(options['destination'])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    result = {'path':target, 'sha256':hashlib.sha256(data).hexdigest()}
                callback(result, None)
            except Exception as error:
                callback(None, error)

        QTimer.singleShot(0, deliver)
        return key

    def cancel(self, key):
        self.requests.pop(key, None)


class Service(QObject):
    account_changed = Signal(object)

    def __init__(self, api):
        super().__init__()
        self.api = api
        self.signed_in = False


class Adapter:
    def snapshot(self):
        return {'permanent':[], 'texts':{}, 'metadata':{}}


def main():
    app = QApplication.instance() or QApplication([])
    chunk = 3 * 1024 * 1024
    payload = bytes(range(256)) * (chunk * 2 // 256)
    identity = str(uuid4())
    record = {'id':identity, 'version':1, 'name':'原文件.bin',
              'size':len(payload), 'sha256':hashlib.sha256(payload).hexdigest()}
    with tempfile.TemporaryDirectory(prefix='flickdock-cloud-fast-download-') as temp:
        service = Service(Transport(record, payload, chunk))
        engine = shelf_sync.ShelfSync(temp, service, Adapter(), poll_ms=600000)
        engine.account_id = 'test'; engine.owner = 'test'
        engine.state = {'entries':{}, 'downloads':{}, 'cursor':'0'}
        target = engine._download_target(record)
        result = []
        original = shelf_sync._checked_download_part

        def no_duplicate_hash(*_args):
            raise AssertionError('fresh network part was read and hashed twice')

        shelf_sync._checked_download_part = no_duplicate_hash
        try:
            engine._download_record(record, target, lambda path, error: result.append((path, error)))
            end = time.monotonic() + 10
            while not result and time.monotonic() < end:
                app.processEvents()
                time.sleep(.002)
        finally:
            shelf_sync._checked_download_part = original
        assert result and result[0][1] is None, result
        assert result[0][0] == target and target.read_bytes() == payload
        engine.close()
    print('PASS verified transport digest avoids per-part disk reread; final whole-file hash remains')


if __name__ == '__main__':
    main()

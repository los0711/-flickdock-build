"""Offscreen real-socket regression for bounded Qt cloud downloads."""
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import time
from threading import Event, Thread

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from cloud_api import CloudApi


def main():
    assert os.environ.get('QT_QPA_PLATFORM') == 'offscreen'
    payload = bytes(range(256)) * 10000
    digest = hashlib.sha256(payload).hexdigest()
    slow_started, release_slow = Event(), Event()

    class Handler(BaseHTTPRequestHandler):
        uploaded = None

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('X-Content-SHA256', digest)
            self.end_headers()
            for index in range(0, len(payload), 65536):
                self.wfile.write(payload[index:index+65536])

        def do_PUT(self):
            size = int(self.headers['Content-Length'])
            digest = hashlib.sha256()
            received = 0
            if self.path.endswith('/slow'):
                part = self.rfile.read(min(65536, size))
                received += len(part)
                digest.update(part)
                slow_started.set()
                release_slow.wait(5)
            while received < size:
                part = self.rfile.read(min(1024*1024, size-received))
                if not part:
                    break
                received += len(part)
                digest.update(part)
            Handler.uploaded = (received, digest.hexdigest())
            body = json.dumps({'schema_version':1, 'received':received}).encode('utf8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    app = QApplication([])
    with tempfile.TemporaryDirectory(prefix='flickdock-cloud-stream-') as temp:
        destination = Path(temp) / 'content.bin'
        client = CloudApi(f'http://127.0.0.1:{server.server_port}', allow_test_http=True)
        client.token = 'test-only'
        result = []
        client.request('GET', '/v1/items/test/parts/0',
                       lambda path, error: (result.append((path, error)), app.quit()),
                       destination=destination, expected={'size':len(payload)})
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert result, 'timed out'
        path, error = result[0]
        if error:
            raise AssertionError(f'{error}; status={error.status}; details={error.details}')
        assert path == destination and destination.read_bytes() == payload
        verified = []
        verified_destination = Path(temp) / 'verified-content.bin'
        client.request('GET', '/v1/items/test/parts/0',
                       lambda value, error: (verified.append((value, error)), app.quit()),
                       destination=verified_destination,
                       expected={'size':len(payload), 'with_digest':True})
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert verified and verified[0][1] is None
        assert verified[0][0] == {'path':verified_destination, 'sha256':digest}
        assert verified_destination.read_bytes() == payload
        staged = Path(temp) / 'staged-original.bin'
        staged.write_bytes(payload * 5)
        staged_digest = hashlib.sha256(payload * 5).hexdigest()
        uploaded = []
        progress = []
        client.request('PUT', '/v1/uploads/test/parts/0',
                       lambda value, error: (uploaded.append((value, error)), app.quit()),
                       source=staged, headers={'X-Content-SHA256':staged_digest},
                       progress=lambda sent, total:progress.append((sent, total)))
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert uploaded and uploaded[0][1] is None, uploaded
        assert uploaded[0][0]['received'] == staged.stat().st_size
        assert Handler.uploaded == (staged.stat().st_size, staged_digest)
        assert progress and progress[-1][0] == staged.stat().st_size
        staged.unlink()  # QFile handle must be closed when the reply finishes.
        cancelled = []
        interrupted_file = Path(temp) / 'cancelled-original.bin'
        interrupted_file.write_bytes(payload * 5)
        key = client.request('PUT', '/v1/uploads/test/slow',
                             lambda value, error:cancelled.append((value, error)),
                             source=interrupted_file,
                             headers={'X-Content-SHA256':staged_digest})
        deadline = time.monotonic() + 5
        while not slow_started.is_set() and time.monotonic() < deadline:
            app.processEvents()
        assert slow_started.is_set()
        client.cancel(key)
        release_slow.set()
        assert cancelled and cancelled[0][1].code == 'cancelled'
        interrupted_file.unlink()  # Windows catches a leaked QFile here.
        client.close()
    server.shutdown()
    print('PASS Qt real-socket verified download digest and streamed file upload')


if __name__ == '__main__':
    main()

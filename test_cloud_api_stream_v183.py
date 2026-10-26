"""Offscreen real-socket regression for bounded Qt cloud downloads."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
from threading import Thread

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from cloud_api import CloudApi


def main():
    assert os.environ.get('QT_QPA_PLATFORM') == 'offscreen'
    payload = bytes(range(256)) * 10000
    digest = hashlib.sha256(payload).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('X-Content-SHA256', digest)
            self.end_headers()
            for index in range(0, len(payload), 65536):
                self.wfile.write(payload[index:index+65536])

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
        client.close()
    server.shutdown()
    print('PASS Qt real-socket 2.56 MB cloud download')


if __name__ == '__main__':
    main()

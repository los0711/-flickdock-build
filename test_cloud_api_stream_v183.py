"""Offscreen real-socket regression for bounded Qt cloud downloads."""
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import time
from threading import Event, Thread
from unittest.mock import patch

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
            if self.path.endswith(('/json-complete-disconnect', '/json-partial-disconnect')):
                body = b'{"schema_version":1,"ready":true}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body) + 1))
                self.end_headers()
                self.wfile.write(body[:-1] if self.path.endswith('/json-partial-disconnect') else body)
                self.close_connection = True
                return
            if self.path.endswith('/malformed-error'):
                body = b'{"error":[]}'
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            # A proxy may terminate the response after the complete verified
            # file while its advertised HTTP length is still inconsistent.
            self.send_header('Content-Length', str(len(payload) + (1 if self.path.endswith('/complete-disconnect') else 0)))
            self.send_header('X-Content-SHA256', digest)
            self.end_headers()
            body = payload[:-1] if self.path.endswith('/partial-disconnect') else payload
            for index in range(0, len(body), 65536):
                self.wfile.write(body[index:index+65536])
                if self.path.endswith('/slow-download'): time.sleep(.005)
            if self.path.endswith(('/complete-disconnect', '/partial-disconnect')):
                self.close_connection = True

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
        verified_progress = []
        verified_destination = Path(temp) / 'verified-content.bin'
        client.request('GET', '/v1/items/test/parts/0/slow-download',
                       lambda value, error: (verified.append((value, error)), app.quit()),
                       destination=verified_destination,
                       expected={'size':len(payload), 'with_digest':True},
                       receive_progress=lambda received,total:verified_progress.append((received,total)))
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert verified and verified[0][1] is None
        assert verified[0][0] == {'path':verified_destination, 'sha256':digest}
        assert verified_destination.read_bytes() == payload
        assert verified_progress and verified_progress[-1] == (len(payload),len(payload))
        assert any(0 < received < total for received,total in verified_progress), verified_progress
        assert [received for received,_ in verified_progress] == sorted({received for received,_ in verified_progress})
        disconnected = []
        disconnected_destination = Path(temp) / 'complete-disconnect.bin'
        client.request('GET', '/v1/items/test/parts/0/complete-disconnect',
                       lambda value, error: (disconnected.append((value, error)), app.quit()),
                       destination=disconnected_destination,
                       expected={'size':len(payload), 'with_digest':True})
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert disconnected, 'complete body with an early close timed out'
        assert disconnected[0][1] is None, disconnected[0][1]
        assert disconnected[0][0] == {'path':disconnected_destination, 'sha256':digest}
        assert disconnected_destination.read_bytes() == payload
        partial = []
        partial_destination = Path(temp) / 'partial-disconnect.bin'
        client.request('GET', '/v1/items/test/parts/0/partial-disconnect',
                       lambda value, error: (partial.append((value, error)), app.quit()),
                       destination=partial_destination,
                       expected={'size':len(payload), 'with_digest':True})
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert partial and partial[0][1] is not None, 'a truncated file must still retry'
        assert not partial_destination.exists(), 'partial bytes must not enter the shelf'
        locked = []
        locked_destination = Path(temp) / 'locked-destination.bin'
        with patch('cloud_api.os.replace', side_effect=PermissionError(13, 'denied', str(locked_destination))):
            client.request('GET', '/v1/items/test/parts/0',
                           lambda value, error: (locked.append((value, error)), app.quit()),
                           destination=locked_destination,
                           expected={'size':len(payload), 'with_digest':True})
            QTimer.singleShot(15000, app.quit)
            app.exec()
        assert locked and locked[0][1].code == 'DOWNLOAD_SAVE_FAILED', locked
        assert str(locked_destination) not in str(locked[0][1].details)
        assert not locked_destination.exists()
        assert not list(Path(temp).glob('.*.part')), 'failed finalization must clean its temp file'
        cleanup_denied = []
        cleanup_destination = Path(temp) / 'cleanup-denied.bin'
        original_unlink = Path.unlink
        def deny_partial_cleanup(path, *args, **kwargs):
            if path.name.startswith('.cleanup-denied.bin-') and path.suffix == '.part':
                raise PermissionError(13, 'locked')
            return original_unlink(path, *args, **kwargs)
        with (patch('cloud_api.os.replace', side_effect=PermissionError(13, 'denied')),
              patch('cloud_api.Path.unlink', deny_partial_cleanup)):
            client.request('GET', '/v1/items/test/parts/0',
                           lambda value, error: (cleanup_denied.append((value, error)), app.quit()),
                           destination=cleanup_destination,
                           expected={'size':len(payload), 'with_digest':True})
            QTimer.singleShot(15000, app.quit)
            app.exec()
        assert len(cleanup_denied) == 1 and cleanup_denied[0][1].code == 'DOWNLOAD_SAVE_FAILED'
        for leftover in Path(temp).glob('.cleanup-denied.bin-*.part'):
            leftover.unlink()
        malformed = []
        client.request('GET', '/v1/items/test/malformed-error',
                       lambda value, error: (malformed.append((value, error)), app.quit()))
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert malformed and malformed[0][1].status == 503
        assert str(malformed[0][1]) == '云同步请求失败，请稍后重试。'
        complete_json = []
        client.request('GET', '/v1/test/json-complete-disconnect',
                       lambda value, error: (complete_json.append((value,error)),app.quit()))
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert complete_json == [({'schema_version':1,'ready':True},None)], complete_json
        partial_json = []
        client.request('GET', '/v1/test/json-partial-disconnect',
                       lambda value, error: (partial_json.append((value,error)),app.quit()))
        QTimer.singleShot(15000, app.quit)
        app.exec()
        assert partial_json and partial_json[0][1] is not None, partial_json
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

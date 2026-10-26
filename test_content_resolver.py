"""Local HTTP/offscreen resolution regressions; no external network or UI."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit

os.environ["QT_QPA_PLATFORM"] = "offscreen"
state = tempfile.TemporaryDirectory(prefix="flickdock-resolver-")
os.environ["APPDATA"] = state.name

from PySide6.QtCore import QBuffer, QEventLoop, QIODevice, QMimeData, QTimer, QUrl, Qt
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QNetworkProxy
from PySide6.QtWidgets import QApplication
from content_resolver import ContentResolver, candidates, MAX_DOWNLOAD_BYTES, _redirect_url
from shelf_content import ContentRepository

app = QApplication([])
png_buffer = QBuffer()
png_buffer.open(QIODevice.OpenModeFlag.WriteOnly)
image = QImage(24, 18, QImage.Format.Format_ARGB32)
image.fill(Qt.GlobalColor.blue)
assert image.save(png_buffer, "PNG")
PNG = bytes(png_buffer.data())
jpeg_buffer = QBuffer()
jpeg_buffer.open(QIODevice.OpenModeFlag.WriteOnly)
assert image.save(jpeg_buffer, "JPEG")
JPEG = bytes(jpeg_buffer.data())
SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><defs><linearGradient id="g"><stop stop-color="blue"/><stop offset="1" stop-color="red"/></linearGradient></defs><rect width="32" height="32" fill="url('#g')"/><script>alert(1)</script><image href="file:///private/file.png"/></svg>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/image")
            self.end_headers()
            return
        if path in ("/loop", "/unsafe-redirect"):
            self.send_response(302)
            self.send_header("Location", "/loop" if path == "/loop" else "file:///private.png")
            self.end_headers()
            return
        if path == "/large":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(MAX_DOWNLOAD_BYTES + 1))
            self.end_headers()
            return
        if path == "/chunked-large":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            try:
                self.wfile.write(b"x" * 100_000)
            except OSError:
                pass
            return
        if path == "/slow":
            time.sleep(.25)
        if path == "/page":
            body = '''<!doctype html><html><head><title>fallback</title><meta property="og:title" content="测试网页标题"><meta name="description" content="正文描述"><meta property="og:image" content="/image"><meta property="og:site_name" content="Local Test"></head><body>content</body></html>'''.encode()
            content_type = "text/html; charset=utf-8"
        elif path == "/page-cover-fails":
            body = b'<html><head><title>Keep title</title><meta property="og:image" content="/missing"></head></html>'
            content_type = "text/html"
        elif path == "/plain":
            body, content_type = b"not an image or HTML page", "text/plain"
        elif path == "/svg":
            body, content_type = SVG, "application/octet-stream"
        elif path == "/jpeg":
            body, content_type = JPEG, "application/octet-stream"
        elif path in ("/image", "/slow"):
            body, content_type = PNG, "application/octet-stream"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server.daemon_threads = True
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{server.server_address[1]}"
root = Path(state.name)
repo = ContentRepository()
retained = set()
results = {}
changed = []
resolver = ContentResolver(repo, lambda: root / "captured", retained.__contains__,
                           on_changed=changed.append)
resolver.network.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
resolver.resolved.connect(lambda ref, result: results.__setitem__(ref, result))


def reference(value):
    ref = repo.add_text(value)
    retained.add(ref)
    return ref


def wait_for(ref, timeout=2500):
    if ref not in results:
        wait = QEventLoop()
        def ready(found_ref, result):
            if found_ref == ref:
                wait.quit()
        resolver.resolved.connect(ready)
        QTimer.singleShot(timeout, wait.quit)
        wait.exec()
        resolver.resolved.disconnect(ready)
    assert ref in results, f"resolution timed out in test: {repo.text(ref)}"
    return results[ref]


def pause(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


try:
    assert _redirect_url("https://example.test/a", QUrl("/b")) == "https://example.test/b"
    assert _redirect_url("https://example.test/a", QUrl("http://example.test/b")) is None
    assert _redirect_url("http://example.test/a", QUrl("file:///private.png")) is None
    # MIME HTML image extraction uses the actual image rather than parent URL.
    mime = QMimeData()
    mime.setUrls([QUrl(base + "/page")])
    mime.setHtml('<a href="/page"><img src="/image"></a>')
    assert candidates(mime) == [base + "/image"]
    mime = QMimeData()
    mime.setText(base + "/image")
    assert candidates(mime) == [base + "/image"]
    mime.setText(base + "/image this is ordinary selected text")
    assert not candidates(mime)
    for invalid in ("javascript:alert(1)", "file:///private.png", "ftp://example.test/image.png"):
        mime.setText(invalid)
        assert not candidates(mime)
    data = "data:image/png;base64," + base64.b64encode(PNG).decode()
    mime.setHtml(f'<img src="{data}">')
    assert candidates(mime) == [data]
    print("PASS MIME URL, HTML image/data URI, ordinary text and unsafe scheme classification")

    # Unknown extension + octet-stream still captures actual image bytes.
    for suffix, expected, expected_bytes in (("/image", ".png", PNG), ("/jpeg", ".jpg", JPEG), ("/redirect", ".png", PNG)):
        url = base + suffix
        ref = reference(url)
        assert resolver.resolve(ref, url)
        result = wait_for(ref)
        assert result["kind"] == "image" and not result["error"], result
        path = Path(result["path"])
        assert path.suffix == expected and path.read_bytes() == expected_bytes
        assert repo.text(ref) == url, "resolver does not replace native URL until owner applies result"
    print("PASS extensionless PNG/JPEG sniffing, safe redirect and original URL preservation")

    ref = reference(base + "/page")
    resolver.resolve(ref, repo.text(ref))
    result = wait_for(ref)
    assert result["kind"] == "link" and result["title"] == "测试网页标题"
    assert result["description"] == "正文描述" and result["site_name"] == "Local Test"
    assert Path(result["preview_image"]).read_bytes() == PNG
    assert "path" not in result and repo.text(ref) == base + "/page"
    ref = reference(base + "/page-cover-fails")
    resolver.resolve(ref, repo.text(ref))
    result = wait_for(ref)
    assert result["title"] == "Keep title" and result["error"]
    print("PASS webpage title/description/cover and partial-cover failure preserves metadata")

    for value, extension in ((data, ".png"), ("data:image/svg+xml," + quote(SVG.decode()), ".svg"), (base + "/svg", ".svg")):
        ref = reference(value)
        resolver.resolve(ref, value)
        result = wait_for(ref)
        assert not result["error"], result
        path = Path(result["path"])
        assert path.suffix == extension
        if extension == ".svg":
            saved = path.read_text(encoding="utf8")
            assert "script" not in saved and "file:///" not in saved and "#g" in saved
    print("PASS base64/percent-encoded data images and vector SVG sanitization")

    for unsafe in (b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///private">]><svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">&x;</svg>',
                   b'<svg xmlns="http://www.w3.org/2000/svg" width="100000" height="100000"><rect width="100000" height="100000"/></svg>'):
        value = "data:image/svg+xml;base64," + base64.b64encode(unsafe).decode()
        ref = reference(value)
        resolver.resolve(ref, value)
        result = wait_for(ref)
        assert result["error"] and "path" not in result

    for suffix in ("/missing", "/plain", "/large", "/loop", "/unsafe-redirect"):
        url = base + suffix
        ref = reference(url)
        resolver.resolve(ref, url)
        result = wait_for(ref)
        assert result["error"] and "path" not in result and result["source_url"] == url
        assert repo.text(ref) == url
    print("PASS network, unsupported content, oversize and redirect bounds retain original native URL")

    # Cancellation/removal never emits a result or writes an image back later.
    before = set((root / "captured").iterdir())
    removed = reference(base + "/slow?removed")
    resolver.resolve(removed, repo.text(removed))
    QTimer.singleShot(20, lambda: retained.remove(removed))
    cancelled = reference(base + "/slow?cancelled")
    resolver.resolve(cancelled, repo.text(cancelled))
    QTimer.singleShot(30, lambda: resolver.cancel(cancelled))
    pause(400)
    assert removed not in results and cancelled not in results
    assert set((root / "captured").iterdir()) == before
    print("PASS removed/cancelled items are never re-added or saved")

    before_write_results = []
    race_ref = reference(data + " ")
    def directory_after_removal():
        retained.remove(race_ref)
        return root / "removed-before-write"
    race = ContentResolver(repo, directory_after_removal, retained.__contains__,
                           on_ready=lambda *args: before_write_results.append(args))
    race.resolve(race_ref, data)
    pause(40)
    assert not before_write_results and not (root / "removed-before-write").exists()
    assert not race.pending_refs
    race.close()

    small_results = {}
    small = ContentResolver(repo, lambda: root / "small", retained.__contains__,
                            on_ready=lambda ref, value: small_results.__setitem__(ref, value),
                            timeout_ms=50, max_download_bytes=64_000)
    small.network.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
    oversized = reference(base + "/chunked-large")
    slow = reference(base + "/slow?timeout")
    small.resolve(oversized, repo.text(oversized))
    small.resolve(slow, repo.text(slow))
    pause(350)
    assert small_results[oversized]["error"] and small_results[slow]["error"]
    assert not (root / "small").exists()
    small.close()
    assert not list(root.rglob("*.txt")), "no native URL or text becomes a plaintext capture file"
    assert not resolver.pending_refs
    print("CONTENT_RESOLVER_OK")
finally:
    resolver.close()
    server.shutdown()
    server.server_close()
    app.processEvents()
    state.cleanup()

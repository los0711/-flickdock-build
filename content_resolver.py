"""Asynchronous image capture and webpage metadata for native URL items.

The original URL lives in ContentRepository throughout resolution. This module
never creates text documents, opens external apps, runs HTML/JavaScript, ignores
TLS errors, or changes shelf references. The owner applies a returned image
path/metadata only while the original reference is still retained.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
from typing import Callable
from urllib.parse import unquote, unquote_to_bytes, urlsplit
from uuid import uuid4
import xml.etree.ElementTree as ET

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImageReader
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest, QSslSocket
from PySide6.QtSvg import QSvgRenderer

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
TIMEOUT_MS = 15_000
MAX_REDIRECTS = 5


def _web_url(value: str, base: str = "", *, allow_data: bool = True) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value):
        return None
    if allow_data and value.lower().startswith("data:image/"):
        return value
    url = QUrl(value)
    if base and url.isRelative():
        url = QUrl(base).resolved(url)
    if (url.isValid() and url.scheme().lower() in ("http", "https")
            and url.host() and not url.userInfo()):
        return url.toString(QUrl.ComponentFormattingOption.FullyEncoded)
    return None


def _redirect_url(current: str, target: QUrl) -> str | None:
    resolved = _web_url(QUrl(current).resolved(target).toString(), allow_data=False)
    if resolved and not (QUrl(current).scheme().lower() == "https"
                         and QUrl(resolved).scheme().lower() == "http"):
        return resolved
    return None


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.images = []
        self.links = []
        self.base = ""
        self.title_parts = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        values = {str(key).lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key and values.get("content") and key not in self.meta:
                self.meta[key] = values["content"].strip()
        elif tag == "img" and values.get("src"):
            self.images.append(values["src"])
        elif tag == "a" and values.get("href"):
            self.links.append(values["href"])
        elif tag == "base" and not self.base:
            self.base = values.get("href", "")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, text):
        if self._in_title:
            self.title_parts.append(text)

    @property
    def title(self):
        return " ".join(" ".join(self.title_parts).split())


def candidates(mime) -> list[str]:
    """Extract image/link drag URLs without converting ordinary text to URLs.

    An HTML image's actual source precedes its surrounding webpage URL. Local
    file references and raw QImage data remain ContentRepository's job.
    """
    if mime is None:
        return []
    if mime.hasImage():
        return []
    urls = [_web_url(url.toString()) for url in mime.urls() if not url.isLocalFile()]
    urls = [value for value in urls if value]
    html = mime.html() if mime.hasHtml() else ""
    parsed = _PageParser()
    if html:
        try:
            parsed.feed(html[:2 * 1024 * 1024])
        except (ValueError, AssertionError):
            pass
    source = re.search(r"(?im)^SourceURL:\s*(https?://[^\r\n]+)", html)
    base = _web_url(parsed.base, urls[0] if urls else "", allow_data=False)
    base = base or (source.group(1) if source else (urls[0] if urls else ""))
    image_urls = [_web_url(value, base) for value in parsed.images]
    values = [value for value in image_urls if value]
    if not values:
        values = urls
    if not values and mime.hasText():
        text = mime.text().strip()
        value = _web_url(text) if not re.search(r"\s", text) else None
        if value:
            values = [value]
    if not values:
        values = [value for raw in parsed.links if (value := _web_url(raw, base))]
    return list(dict.fromkeys(values))


def _decode_html(body: bytes, content_type: str) -> str:
    charset = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", content_type, re.I)
    if charset is None:
        charset = re.search(br"charset\s*=\s*[\"']?([\w.-]+)", body[:2048], re.I)
    encoding = charset.group(1) if charset else "utf-8"
    if isinstance(encoding, bytes):
        encoding = encoding.decode("ascii", errors="ignore")
    try:
        return body[:2 * 1024 * 1024].decode(encoding, errors="replace")
    except LookupError:
        return body[:2 * 1024 * 1024].decode("utf-8", errors="replace")


def _external_css(value: str) -> bool:
    if re.search(r"@import|javascript:|expression\(", value, re.I):
        return True
    return any(not match.group(1).strip().strip("'\"").startswith("#")
               for match in re.finditer(r"url\(([^)]*)\)", value, re.I))


def _image_bytes(body: bytes, content_type: str) -> tuple[bytes, str] | None:
    """Validate real image data; SVG keeps vectors with external actions stripped."""
    prefix = body.lstrip()[:512].lower()
    svg_hint = "image/svg" in content_type or b"<svg" in prefix
    if svg_hint:
        if re.search(br"<!\s*(?:doctype|entity)\b", body, re.I):
            raise ValueError("SVG 包含不支持的外部实体")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as ex:
            raise ValueError("SVG 图片内容不完整") from ex
        if root.tag.rsplit("}", 1)[-1].lower() != "svg":
            raise ValueError("返回内容不是 SVG 图片")
        for parent in list(root.iter()):
            for child in list(parent):
                if child.tag.rsplit("}", 1)[-1].lower() in (
                    "script", "foreignobject", "animate", "animatetransform", "animatemotion", "set"):
                    parent.remove(child)
            for key, value in list(parent.attrib.items()):
                local = key.rsplit("}", 1)[-1].lower()
                if local.startswith("on") or local == "base":
                    del parent.attrib[key]
                elif local in ("href", "src") and not value.strip().startswith("#"):
                    # External resources, including local files, never get a
                    # chance to load during an in-app SVG preview.
                    del parent.attrib[key]
                elif _external_css(value):
                    del parent.attrib[key]
            if parent.tag.rsplit("}", 1)[-1].lower() == "style" and parent.text:
                if _external_css(parent.text):
                    parent.text = ""
        safe = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        renderer = QSvgRenderer(QByteArray(safe))
        if not renderer.isValid():
            raise ValueError("SVG 无法预览")
        size = renderer.defaultSize()
        if size.width() <= 0 or size.height() <= 0 or size.width() * size.height() > MAX_IMAGE_PIXELS:
            raise ValueError("SVG 图片尺寸过大或无效")
        return safe, ".svg"

    signature = (body.startswith(b"\x89PNG\r\n\x1a\n") or body.startswith(b"\xff\xd8\xff")
                 or body.startswith((b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*"))
                 or (body.startswith(b"RIFF") and body[8:12] == b"WEBP"))
    if not signature and not content_type.startswith("image/"):
        return None
    buffer = QBuffer()
    buffer.setData(QByteArray(body))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    if not reader.canRead():
        raise ValueError("图片格式无法识别")
    image_format = bytes(reader.format()).decode("ascii", errors="ignore").lower()
    size = reader.size()
    if size.width() <= 0 or size.height() <= 0 or size.width() * size.height() > MAX_IMAGE_PIXELS:
        raise ValueError("图片尺寸过大或无效")
    reader.setScaledSize(size.scaled(QSize(768, 768), Qt.AspectRatioMode.KeepAspectRatio))
    if reader.read().isNull():
        raise ValueError("图片内容不完整")
    extension = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "gif": ".gif",
                 "webp": ".webp", "bmp": ".bmp", "tiff": ".tiff", "ico": ".ico"}.get(image_format)
    if not extension:
        raise ValueError("暂不支持此图片格式")
    return body, extension


@dataclass
class _Job:
    ref: str
    source: str
    timer: QTimer
    reply: QNetworkReply | None = None
    body: bytearray = field(default_factory=bytearray)
    total: int = 0
    redirects: int = 0
    phase: str = "primary"
    result: dict = field(default_factory=dict)
    created_paths: list[Path] = field(default_factory=list)


class _Removed(Exception):
    pass


class ContentResolver(QObject):
    resolved = Signal(str, dict)
    candidates = staticmethod(candidates)

    def __init__(self, repository, capture_dir_callable: Callable[[], Path],
                 is_retained: Callable[[str], bool], on_ready: Callable | None = None,
                 on_changed: Callable | None = None, parent=None, *,
                 timeout_ms: int = TIMEOUT_MS, max_download_bytes: int = MAX_DOWNLOAD_BYTES,
                 max_redirects: int = MAX_REDIRECTS):
        super().__init__(parent)
        self.repository = repository
        self.capture_dir_callable = capture_dir_callable
        self.is_retained = is_retained
        self.on_ready, self.on_changed = on_ready, on_changed
        self.timeout_ms = max(20, int(timeout_ms))
        self.max_download_bytes = max(1, min(MAX_DOWNLOAD_BYTES, int(max_download_bytes)))
        self.max_redirects = max(0, min(MAX_REDIRECTS, int(max_redirects)))
        if sys.platform == "win32" and "schannel" in QSslSocket.availableBackends():
            QSslSocket.setActiveBackend("schannel")
        self.network = QNetworkAccessManager(self)
        self._jobs: dict[str, _Job] = {}
        self._closed = False

    @property
    def pending_refs(self):
        return frozenset(self._jobs)

    def _retained(self, ref):
        try:
            return not self._closed and bool(self.is_retained(ref))
        except (RuntimeError, KeyError):
            return False

    def resolve(self, ref: str, url: str) -> bool:
        self.cancel(ref)
        if not self._retained(ref):
            return False
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        job = _Job(ref, str(url), timer)
        self._jobs[ref] = job
        timer.timeout.connect(lambda: self._error(job, "下载超时，已保留原链接"))
        timer.start(self.timeout_ms)
        if self.on_changed:
            self.on_changed(ref)
        # Even data URIs and invalid URLs complete asynchronously, after the
        # owner has finished inserting its native URL placeholder.
        QTimer.singleShot(0, lambda: self._start(job, str(url)))
        return True

    def _live(self, job):
        if self._jobs.get(job.ref) is not job:
            return False
        if not self._retained(job.ref):
            self.cancel(job.ref)
            return False
        return True

    def _start(self, job, value):
        if not self._live(job):
            return
        url = _web_url(value)
        if not url:
            self._error(job, "链接格式不支持，已保留原内容")
        elif url.lower().startswith("data:image/"):
            self._data_image(job, url)
        else:
            self._request(job, QUrl(url))

    def _request(self, job, url):
        if not self._live(job):
            return
        request = QNetworkRequest(url)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setMaximumRedirectsAllowed(self.max_redirects)
        request.setTransferTimeout(self.timeout_ms)
        request.setRawHeader(b"User-Agent", b"FlickDock/1.6 LinkPreview")
        request.setRawHeader(b"Accept", b"image/*,text/html;q=0.9,*/*;q=0.1")
        job.body.clear()
        reply = self.network.get(request)
        reply.setReadBufferSize(128 * 1024)
        job.reply = reply
        reply.metaDataChanged.connect(lambda: self._metadata(job, reply))
        reply.readyRead.connect(lambda: self._drain(job, reply))
        reply.finished.connect(lambda: self._finished(job, reply))

    def _metadata(self, job, reply):
        if not self._live(job) or job.reply is not reply:
            return
        length = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        try:
            if length is not None and int(length) + job.total - len(job.body) > self.max_download_bytes:
                self._error(job, "内容超过 20 MB 下载限制，已保留原链接")
        except (TypeError, ValueError):
            pass

    def _drain(self, job, reply):
        if not self._live(job) or job.reply is not reply:
            return
        while reply.bytesAvailable() > 0:
            chunk = bytes(reply.read(min(65536, self.max_download_bytes - job.total + 1)))
            if not chunk:
                break
            job.total += len(chunk)
            if job.total > self.max_download_bytes:
                self._error(job, "内容超过 20 MB 下载限制，已保留原链接")
                return
            job.body.extend(chunk)

    def _finished(self, job, reply):
        if not self._live(job) or job.reply is not reply:
            return
        self._drain(job, reply)
        if not self._live(job) or job.reply is not reply:
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        target = reply.attribute(QNetworkRequest.Attribute.RedirectionTargetAttribute)
        final_url = reply.url().toString()
        content_type = bytes(reply.rawHeader("Content-Type")).decode("ascii", errors="ignore").lower()
        error = reply.error()
        body = bytes(job.body)
        job.reply = None
        reply.deleteLater()
        if target is not None and not QUrl(target).isEmpty() and status in (301, 302, 303, 307, 308):
            redirected = _redirect_url(final_url, QUrl(target))
            if not redirected or job.redirects >= self.max_redirects:
                self._error(job, "链接重定向过多或不支持，已保留原链接")
                return
            job.redirects += 1
            self._request(job, QUrl(redirected))
            return
        if error != QNetworkReply.NetworkError.NoError or (status is not None and int(status) >= 400):
            self._error(job, f"链接暂时无法下载{f'（HTTP {status}）' if status else ''}，已保留原链接")
            return
        self._process_body(job, body, content_type, final_url)

    def _data_image(self, job, url):
        encoding_factor = 4 / 3 if ";base64," in url[:200].lower() else 3
        if len(url) > int(self.max_download_bytes * encoding_factor) + 4096:
            self._error(job, "图片超过 20 MB 下载限制，已保留原内容")
            return
        try:
            header, encoded = url.split(",", 1)
            media_type = header[5:].split(";", 1)[0].lower()
            if not media_type.startswith("image/"):
                raise ValueError("不支持的数据图片")
            raw = unquote_to_bytes(encoded)
            body = base64.b64decode(re.sub(br"\s+", b"", raw), validate=True) if ";base64" in header.lower() else raw
            if len(body) + job.total > self.max_download_bytes:
                raise ValueError("图片超过 20 MB 下载限制")
            job.total += len(body)
            self._process_body(job, body, media_type, url)
        except (ValueError, TypeError) as ex:
            self._error(job, str(ex) or "数据图片无法读取，已保留原内容")

    def _process_body(self, job, body, content_type, final_url):
        if not self._live(job):
            return
        try:
            image = _image_bytes(body, content_type)
            if image:
                path = self._write_image(job, image[0], image[1], final_url)
                if job.phase == "cover":
                    job.result["preview_image"] = str(path)
                    self._finish(job, job.result)
                else:
                    self._finish(job, {"kind": "image", "path": str(path),
                                       "title": path.name, "source_url": job.source,
                                       "final_url": final_url, "error": ""})
                return
            if job.phase == "cover":
                self._error(job, "网页封面无法预览")
                return
            prefix = body.lstrip()[:1024].lower()
            if ("text/html" in content_type or "application/xhtml" in content_type
                    or any(marker in prefix for marker in (b"<!doctype html", b"<html", b"<head", b"<meta"))):
                self._webpage(job, body, content_type, final_url)
            else:
                self._error(job, "此链接没有可预览的图片或网页，已保留原链接")
        except _Removed:
            self.cancel(job.ref)
        except (OSError, ValueError) as ex:
            self._error(job, str(ex) or "内容暂时无法预览，已保留原链接")

    def _webpage(self, job, body, content_type, final_url):
        page = _PageParser()
        page.feed(_decode_html(body, content_type))
        title = page.meta.get("og:title") or page.meta.get("twitter:title") or page.title
        description = page.meta.get("og:description") or page.meta.get("description") or page.meta.get("twitter:description") or ""
        host = QUrl(final_url).host()
        job.result = {"kind": "link", "title": (title or host or "网页链接")[:500],
                      "description": description[:2000], "source_url": job.source,
                      "final_url": final_url, "site_name": page.meta.get("og:site_name", host)[:300],
                      "error": ""}
        cover = page.meta.get("og:image") or page.meta.get("twitter:image") or page.meta.get("twitter:image:src")
        cover_url = _web_url(cover, final_url) if cover else None
        if cover_url:
            job.phase, job.redirects = "cover", 0
            self._start(job, cover_url)
        else:
            self._finish(job, job.result)

    def _write_image(self, job, body, extension, source):
        if not self._live(job):
            raise _Removed()
        directory = Path(self.capture_dir_callable())
        if not self._retained(job.ref):
            raise _Removed()
        directory.mkdir(parents=True, exist_ok=True)
        stem = unquote(Path(urlsplit(source).path).stem) if not source.startswith("data:") else "网页图片"
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")[:70] or "网页图片"
        if stem.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            stem = "图片_" + stem
        path = directory / f"{stem}-{uuid4().hex[:10]}{extension}"
        partial = path.with_suffix(path.suffix + ".part")
        try:
            if not self._retained(job.ref):
                raise _Removed()
            partial.write_bytes(body)
            if not self._retained(job.ref):
                raise _Removed()
            partial.replace(path)
        finally:
            if partial.exists():
                partial.unlink()
        job.created_paths.append(path)
        return path.resolve()

    def _error(self, job, message):
        if not self._live(job):
            return
        result = dict(job.result) if job.phase == "cover" else {
            "kind": "link", "title": QUrl(job.source).host() or "网页链接", "source_url": job.source}
        result["error"] = message
        self._finish(job, result)

    def _finish(self, job, result):
        if not self._live(job):
            return
        self._jobs.pop(job.ref, None)
        self._dispose(job)
        self.resolved.emit(job.ref, dict(result))
        if self.on_ready:
            self.on_ready(job.ref, dict(result))
        if self.on_changed:
            self.on_changed(job.ref)

    def _dispose(self, job):
        job.timer.stop()
        job.timer.deleteLater()
        if job.reply is not None:
            reply, job.reply = job.reply, None
            if reply.isRunning():
                reply.abort()
            reply.deleteLater()
        job.body.clear()

    def cancel(self, ref):
        job = self._jobs.pop(ref, None)
        if job is None:
            return False
        self._dispose(job)
        for path in job.created_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return True

    def cancel_all(self):
        for ref in list(self._jobs):
            self.cancel(ref)

    def close(self):
        self._closed = True
        self.cancel_all()

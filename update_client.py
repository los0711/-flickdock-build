"""Asynchronous, bounded HTTPS updates; verified downloads never execute here."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
import platform as platform_module
from pathlib import Path
import re
import sys
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest, QSslSocket


# Stable public release URL; Gitee redirects raw files to its public CDN.
DEFAULT_MANIFEST_URL = 'https://gitee.com/rooogerluo/flick-dock-releases/raw/master/updates/latest.json'
MAC_MANIFEST_BASE = 'https://gitee.com/rooogerluo/flick-dock-releases/raw/master/updates/'
DEFAULT_ALLOWED_HOSTS = frozenset({'gitee.com', 'raw.giteeusercontent.com', 'foruda.gitee.com'})
MAX_MANIFEST_BYTES = 128 * 1024
MAX_INSTALLER_BYTES = 1024 * 1024 * 1024
MAX_REDIRECTS = 5
_VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z')


def version_tuple(value):
    """Stable SemVer components: numeric, no leading zeros or path content."""
    if not isinstance(value, str) or len(value) > 48 or not _VERSION.fullmatch(value):
        raise ValueError('版本号需要使用规范的 X.Y.Z 格式。')
    return tuple(map(int, value.split('.')))


def compare_versions(left, right):
    a, b = version_tuple(left), version_tuple(right)
    return (a > b) - (a < b)


def trusted_url(value, allowed_hosts):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('更新链接无效。')
    url = QUrl(value, QUrl.ParsingMode.StrictMode)
    hosts = {str(host).lower().rstrip('.') for host in allowed_hosts}
    if (not url.isValid() or url.scheme().lower() != 'https' or not url.host()
            or url.host().lower().rstrip('.') not in hosts
            or url.userName() or url.password() or url.hasFragment()):
        raise ValueError('更新链接必须来自受信任的 HTTPS 下载来源。')
    return url


def update_architecture(machine=None):
    value = str(machine or platform_module.machine()).lower()
    aliases = {'aarch64': 'arm64', 'amd64': 'x86_64', 'x64': 'x86_64'}
    return aliases.get(value, value)


def manifest_endpoint(platform=None, machine=None):
    platform = sys.platform if platform is None else platform
    if platform != 'darwin':
        return DEFAULT_MANIFEST_URL
    architecture = update_architecture(machine)
    if architecture not in ('arm64', 'x86_64'):
        raise ValueError('当前 Mac 架构没有对应的更新通道。')
    return MAC_MANIFEST_BASE + 'macos-' + architecture + '.json'


def validate_manifest(value, allowed_hosts=DEFAULT_ALLOWED_HOSTS, *, platform=None, machine=None):
    platform = sys.platform if platform is None else platform
    if not isinstance(value, dict) or value.get('schema_version') != 1 or type(value.get('schema_version')) is not int:
        raise ValueError('更新清单格式不支持。')
    version = value.get('version')
    version_tuple(version)
    published = value.get('published_at')
    if not isinstance(published, str) or len(published) > 40:
        raise ValueError('更新清单缺少发布时间。')
    try:
        stamp = datetime.fromisoformat(published.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
            raise ValueError()
    except (ValueError, OverflowError):
        raise ValueError('发布时间需要使用 UTC ISO8601 格式。') from None
    notes = value.get('notes')
    if not isinstance(notes, str) or len(notes) > 32768:
        raise ValueError('更新说明无效或过长。')
    installer = value.get('installer')
    if not isinstance(installer, dict):
        raise ValueError('更新清单缺少安装包。')
    url = trusted_url(installer.get('url'), allowed_hosts).toString()
    size, digest = installer.get('size'), installer.get('sha256')
    if type(size) is not int or not 0 < size <= MAX_INSTALLER_BYTES:
        raise ValueError('安装包大小无效或超过 1 GB 限制。')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('安装包 SHA256 校验值无效。')
    filename = installer.get('filename')
    extension = '(?:dmg|zip)' if platform == 'darwin' else 'exe'
    if filename is not None and (not isinstance(filename, str) or len(filename) > 128
            or not re.fullmatch(r'[^<>:"/\\|?*\x00-\x1f]+\.' + extension, filename, re.IGNORECASE)
            or filename.endswith((' ', '.'))):
        raise ValueError('安装包文件名无效。')
    result = dict(schema_version=1, version=version, published_at=published, notes=notes,
                  installer=dict(url=url, size=size, sha256=digest))
    if filename is not None:
        result['installer']['filename'] = filename
    if platform == 'darwin':
        architecture = value.get('architecture')
        if (value.get('platform') != 'darwin' or architecture not in (update_architecture(machine), 'universal2')
                or not QUrl(url).path().lower().endswith(('.dmg', '.zip'))):
            raise ValueError('更新清单必须是匹配此 Mac 架构的 macOS 安装包。')
        if filename is not None and not filename.lower().endswith(QUrl(url).path().lower()[-4:]):
            raise ValueError('Mac 安装包文件名与下载格式不一致。')
        result.update(platform='darwin', architecture=architecture)
    elif value.get('platform') not in (None, 'win32'):
        raise ValueError('更新清单不是 Windows 安装包。')
    return result


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('更新清单包含重复字段。')
        result[key] = value
    return result


class UpdateClient(QObject):
    checking = Signal(bool)
    update_available = Signal(dict, bool)
    up_to_date = Signal(bool)
    failed = Signal(str, bool)
    progress = Signal(int, int)
    verified_file = Signal(str, dict, bool)
    cancelled = Signal(bool)

    def __init__(self, current_version, updates_dir=None, parent=None, *,
                 endpoint=None, allowed_hosts=DEFAULT_ALLOWED_HOSTS,
                 network=None, timeout_ms=15000, platform=None, machine=None):
        super().__init__(parent)
        version_tuple(current_version)
        self.current_version = current_version
        self.platform = sys.platform if platform is None else platform
        self.machine = update_architecture(machine)
        self.endpoint = manifest_endpoint(self.platform, self.machine) if endpoint is None else endpoint
        self.allowed_hosts = frozenset(str(host).lower().rstrip('.') for host in allowed_hosts)
        if updates_dir is None:
            from platform_native import data_directory
            updates_dir = data_directory(platform=self.platform) / 'updates'
        self.updates_dir = Path(updates_dir)
        self.timeout_ms = max(50, min(120000, int(timeout_ms)))
        if os.name == 'nt' and 'schannel' in QSslSocket.availableBackends():
            QSslSocket.setActiveBackend('schannel')
        self.network = network if network is not None else QNetworkAccessManager(self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(lambda: self._fail('检查更新超时。' if self._mode == 'check' else '下载安装包超时。'))
        self._closed = False
        self._mode = None
        self._manual = False
        self._reply = None
        self._generation = 0
        self._manifest = None
        self._download_manifest = None
        self._body = bytearray()
        self._prefix = bytearray()
        self._received = 0
        self._redirect_bytes = 0
        self._redirects = 0
        self._file = None
        self._part = None
        self._final = None
        self._hash = None
        self.last_operation = ''

    @property
    def busy(self):
        return self._mode is not None

    @property
    def available_manifest(self):
        return copy.deepcopy(self._manifest)

    def check(self, manual=False):
        if self._closed:
            return False
        if self.busy:
            if self._mode == 'check':
                self._manual = self._manual or bool(manual)
                return True
            return False
        self._begin('check', manual)
        self._manifest = None
        self.checking.emit(self._manual)
        self._timer.start(self.timeout_ms)
        self._request(self.endpoint)
        return True

    def download(self, manifest=None, manual=True):
        if self._closed or self.busy:
            return False
        self._begin('download', manual)
        try:
            candidate = validate_manifest(manifest if manifest is not None else self._manifest, self.allowed_hosts,
                                          platform=self.platform, machine=self.machine)
            if compare_versions(candidate['version'], self.current_version) <= 0:
                raise ValueError('安装包版本不高于当前版本，已拒绝降级或重复安装。')
            self._download_manifest = candidate
            directory = self.updates_dir / candidate['version']
            directory.mkdir(parents=True, exist_ok=True)
            self._part = directory / (('update-' if self.platform == 'darwin' else 'setup-') + uuid4().hex + '.part')
            mac_dmg = self.platform == 'darwin' and candidate['installer']['url'].lower().split('?', 1)[0].endswith('.dmg')
            self._final = directory / ('update.dmg' if mac_dmg else 'update.zip' if self.platform == 'darwin' else 'setup.exe')
            self._file = self._part.open('xb')
            self._hash = hashlib.sha256()
            self.progress.emit(0, candidate['installer']['size'])
            if not self.busy:
                return True
            self._timer.start(max(300000, self.timeout_ms * 20))
            self._request(candidate['installer']['url'])
        except (OSError, ValueError, TypeError) as error:
            self._fail(str(error))
        return True

    def _begin(self, mode, manual):
        self._generation += 1
        self._mode, self.last_operation = mode, mode
        self._manual = bool(manual)
        self._body.clear()
        self._prefix.clear()
        self._received = self._redirect_bytes = self._redirects = 0

    def _request(self, value):
        if self._closed or not self.busy:
            return
        try:
            url = trusted_url(value, self.allowed_hosts)
            request = QNetworkRequest(url)
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                 QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
            request.setMaximumRedirectsAllowed(MAX_REDIRECTS)
            request.setTransferTimeout(self.timeout_ms)
            request.setRawHeader(b'User-Agent', ('FlickDock/' + self.current_version).encode('ascii'))
            request.setRawHeader(b'Accept', b'application/json' if self._mode == 'check' else b'application/octet-stream')
            request.setRawHeader(b'Cache-Control', b'no-cache')
            reply = self.network.get(request)
            self._reply = reply
            reply.setReadBufferSize(128 * 1024)
            generation = self._generation
            reply.metaDataChanged.connect(lambda: self._metadata(reply, generation))
            reply.readyRead.connect(lambda: self._drain(reply, generation))
            reply.finished.connect(lambda: self._finished(reply, generation))
        except (ValueError, OSError, RuntimeError) as error:
            self._fail(str(error))

    def _live(self, reply, generation):
        return not self._closed and self.busy and self._reply is reply and generation == self._generation

    @staticmethod
    def _status(reply):
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        return int(status) if status is not None else 0

    def _metadata(self, reply, generation):
        if not self._live(reply, generation):
            return
        status = self._status(reply)
        if status in (301, 302, 303, 307, 308):
            return
        if status and status != 200:
            self._fail(f'更新服务器返回 HTTP {status}。')
            return
        content_type = bytes(reply.rawHeader('Content-Type')).lower().split(b';', 1)[0].strip()
        if content_type in (b'text/html', b'application/xhtml+xml'):
            self._fail('更新地址返回了网页或登录页面。')
            return
        length = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
        if length is not None:
            try:
                length = int(length)
                limit = MAX_MANIFEST_BYTES if self._mode == 'check' else self._download_manifest['installer']['size']
                if length < 0 or length > limit or (self._mode == 'download' and length != limit):
                    self._fail('更新内容大小与清单不符或超过限制。')
            except (ValueError, TypeError):
                self._fail('更新服务器返回了无效的内容大小。')

    def _drain(self, reply, generation):
        if not self._live(reply, generation):
            return
        self._metadata(reply, generation)
        if not self._live(reply, generation):
            return
        redirected = self._status(reply) in (301, 302, 303, 307, 308)
        budget = 512 * 1024
        try:
            while self._live(reply, generation) and reply.bytesAvailable() > 0 and budget > 0:
                chunk = bytes(reply.read(min(65536, budget)))
                if not chunk:
                    break
                budget -= len(chunk)
                if redirected:
                    self._redirect_bytes += len(chunk)
                    if self._redirect_bytes > MAX_MANIFEST_BYTES:
                        raise ValueError('更新重定向内容超过限制。')
                    continue
                self._received += len(chunk)
                limit = MAX_MANIFEST_BYTES if self._mode == 'check' else self._download_manifest['installer']['size']
                if self._received > limit:
                    raise ValueError('更新内容超过大小限制。')
                if len(self._prefix) < 512:
                    self._prefix.extend(chunk[:512-len(self._prefix)])
                    if bytes(self._prefix).lstrip().lower().startswith((b'<html', b'<!doctype', b'<head', b'<body')):
                        raise ValueError('更新地址返回了网页或登录页面。')
                if self._mode == 'check':
                    self._body.extend(chunk)
                else:
                    self._file.write(chunk)
                    self._hash.update(chunk)
                    self.progress.emit(self._received, limit)
                    if not self._live(reply, generation):
                        return
        except (OSError, ValueError) as error:
            self._fail(str(error))
            return
        if self._live(reply, generation) and reply.bytesAvailable() > 0:
            QTimer.singleShot(0, lambda: self._drain(reply, generation))

    def _finished(self, reply, generation):
        if not self._live(reply, generation):
            return
        self._drain(reply, generation)
        if not self._live(reply, generation):
            return
        if reply.bytesAvailable() > 0:
            QTimer.singleShot(0, lambda: self._finished(reply, generation))
            return
        status = self._status(reply)
        target = reply.attribute(QNetworkRequest.Attribute.RedirectionTargetAttribute)
        if status in (301, 302, 303, 307, 308):
            if target is None or self._redirects >= MAX_REDIRECTS:
                self._fail('更新链接重定向无效或次数过多。')
                return
            resolved = reply.url().resolved(QUrl(target)).toString()
            self._reply = None
            reply.deleteLater()
            self._redirects += 1
            self._request(resolved)
            return
        if reply.error() != QNetworkReply.NetworkError.NoError or status != 200:
            if reply.error() == QNetworkReply.NetworkError.TimeoutError:
                self._fail('检查更新超时。' if self._mode == 'check' else '下载安装包超时。')
                return
            self._fail('更新请求失败：' + reply.errorString())
            return
        try:
            if self._mode == 'check':
                parsed = json.loads(bytes(self._body).decode('utf-8-sig'), object_pairs_hook=_json_object,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError('更新清单包含无效数值。')))
                candidate = validate_manifest(parsed, self.allowed_hosts, platform=self.platform, machine=self.machine)
                manual = self._manual
                self._complete()
                if compare_versions(candidate['version'], self.current_version) > 0:
                    self._manifest = candidate
                    self.update_available.emit(copy.deepcopy(candidate), manual)
                else:
                    self.up_to_date.emit(manual)
            else:
                candidate = self._download_manifest
                installer = candidate['installer']
                if self._received != installer['size'] or self._hash.hexdigest() != installer['sha256']:
                    raise ValueError('安装包大小或 SHA256 校验失败，已删除下载文件。')
                self._file.flush()
                os.fsync(self._file.fileno())
                if self.platform == 'darwin' and self._final.suffix == '.dmg':
                    if self._received < 512:
                        raise ValueError('下载内容不是 macOS DMG 安装镜像。')
                    with self._part.open('rb') as image:
                        image.seek(-512, os.SEEK_END)
                        signature = image.read(4)
                    if signature != b'koly':
                        raise ValueError('下载内容不是 macOS DMG 安装镜像。')
                else:
                    magic = b'PK\x03\x04' if self.platform == 'darwin' else b'MZ'
                    if not bytes(self._prefix).startswith(magic):
                        raise ValueError('下载内容不是 macOS ZIP 应用包。' if self.platform == 'darwin' else '下载内容不是 Windows 安装程序。')
                self._file.close()
                self._file = None
                final = self._final
                os.replace(self._part, final)
                self._part = None
                manual = self._manual
                self._complete()
                self.verified_file.emit(str(final), copy.deepcopy(candidate), manual)
        except (OSError, ValueError, TypeError, UnicodeError) as error:
            self._fail(str(error) or '更新清单无效。')

    def _detach_reply(self):
        reply, self._reply = self._reply, None
        if reply is not None:
            if not reply.isFinished():
                reply.abort()
            reply.deleteLater()

    def _complete(self):
        self._timer.stop()
        self._generation += 1
        self._detach_reply()
        self._mode = None
        self._body.clear()

    def _cleanup_partial(self):
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._part is not None:
            self._part.unlink(missing_ok=True)
            self._part = None

    def _fail(self, message):
        if not self.busy:
            return
        manual = self._manual
        self._complete()
        try:
            self._cleanup_partial()
        except OSError:
            message += ' 下载暂存文件暂时无法删除。'
        if not self._closed:
            self.failed.emit(str(message), manual)

    def cancel(self):
        if not self.busy:
            return False
        manual = self._manual
        self._complete()
        try:
            self._cleanup_partial()
        except OSError:
            if not self._closed:
                self.failed.emit('下载已取消，但暂存文件暂时无法删除。', manual)
            return True
        if not self._closed:
            self.cancelled.emit(manual)
        return True

    def close(self):
        self._closed = True
        self.cancel()

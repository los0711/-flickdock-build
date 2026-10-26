"""Consent based, encrypted LAN transfers; original bytes never pass through R2.

Discovery is an untrusted directory. TLS pins the discovered session certificate;
both screens show the same six digit authentication code for the user to compare.
An incoming manifest stays in memory until the receiver explicitly accepts it.
One ordered TCP stream per transfer avoids per-chunk network acknowledgements.
Interrupted connections resume their verified prefix within the running session.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import dataclass, field
import errno
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
from uuid import uuid4

from PySide6.QtCore import QObject, Signal
from selection_archive import _is_link, _source_reader
from sync_files import safe_name

DISCOVERY_PORT = 45875
BLOCK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 100000
MAX_TOTAL_BYTES = (1 << 63) - 1
PROTOCOL = 'flickdock-lan-1'


def _crypto_library():
    """Use the OpenSSL dependency already shipped for Python ssl/scrypt."""
    import _ssl
    roots = [Path(_ssl.__file__).parent, Path(sys.executable).parent]
    if getattr(sys, '_MEIPASS', None): roots.insert(0, Path(sys._MEIPASS))
    candidates = []
    for root in roots:
        for location in (root, root / 'lib', root.parent / 'lib'):
            candidates.extend(location.glob('libcrypto*.dll'))
            candidates.extend(location.glob('libcrypto*.dylib'))
            candidates.extend(location.glob('libcrypto*.so*'))
    found = ctypes.util.find_library('crypto')
    if found: candidates.append(found)
    for candidate in candidates:
        try:
            library = ctypes.CDLL(str(candidate))
            if hasattr(library, 'EVP_PKEY_Q_keygen'): return library
        except OSError: pass
    raise RuntimeError('无法加载局域网加密组件，请重新安装 FlickDock。')


def _ephemeral_certificate():
    """Generate P-256 key/certificate with OpenSSL, never handwritten crypto."""
    lib = _crypto_library(); pointer = ctypes.c_void_p
    def fn(name, result, *args):
        function = getattr(lib, name); function.restype = result; function.argtypes = list(args)
        return function
    keygen = fn('EVP_PKEY_Q_keygen', pointer, pointer, ctypes.c_char_p, ctypes.c_char_p)
    free_key = fn('EVP_PKEY_free', None, pointer)
    new_cert = fn('X509_new', pointer); free_cert = fn('X509_free', None, pointer)
    set_version = fn('X509_set_version', ctypes.c_int, pointer, ctypes.c_long)
    serial = fn('X509_get_serialNumber', pointer, pointer)
    set_serial = fn('ASN1_INTEGER_set', ctypes.c_int, pointer, ctypes.c_long)
    before = fn('X509_getm_notBefore', pointer, pointer)
    after = fn('X509_getm_notAfter', pointer, pointer)
    adjust = fn('X509_gmtime_adj', pointer, pointer, ctypes.c_long)
    public = fn('X509_set_pubkey', ctypes.c_int, pointer, pointer)
    subject = fn('X509_get_subject_name', pointer, pointer)
    entry = fn('X509_NAME_add_entry_by_txt', ctypes.c_int, pointer, ctypes.c_char_p,
               ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int)
    issuer = fn('X509_set_issuer_name', ctypes.c_int, pointer, pointer)
    sha256 = fn('EVP_sha256', pointer)
    sign = fn('X509_sign', ctypes.c_int, pointer, pointer, pointer)
    new_bio = fn('BIO_new', pointer, pointer); memory_bio = fn('BIO_s_mem', pointer)
    free_bio = fn('BIO_free', ctypes.c_int, pointer)
    control = fn('BIO_ctrl', ctypes.c_long, pointer, ctypes.c_int, ctypes.c_long, pointer)
    read = fn('BIO_read', ctypes.c_int, pointer, pointer, ctypes.c_int)
    pem_cert = fn('PEM_write_bio_X509', ctypes.c_int, pointer, pointer)
    pem_key = fn('PEM_write_bio_PrivateKey', ctypes.c_int, pointer, pointer, pointer,
                 pointer, ctypes.c_int, pointer, pointer)
    key = keygen(None, None, b'EC', ctypes.c_char_p(b'prime256v1'))
    certificate = new_cert()
    try:
        if not key or not certificate: raise RuntimeError('局域网加密初始化失败。')
        results = [set_version(certificate, 2), set_serial(serial(certificate), secrets.randbelow(1 << 30) + 1),
                   bool(adjust(before(certificate), -60)), bool(adjust(after(certificate), 86400)),
                   public(certificate, key)]
        name = subject(certificate)
        results.extend([entry(name, b'CN', 0x1001, b'FlickDock LAN session', -1, -1, 0),
                        issuer(certificate, name), sign(certificate, key, sha256())])
        if not all(results): raise RuntimeError('局域网证书生成失败。')
        def export(writer):
            bio = new_bio(memory_bio())
            if not bio: raise RuntimeError('局域网证书导出失败。')
            try:
                if writer(bio) != 1: raise RuntimeError('局域网证书导出失败。')
                size = control(bio, 10, 0, None)
                if not 0 < size < 65536: raise RuntimeError('局域网证书大小无效。')
                output = ctypes.create_string_buffer(size)
                if read(bio, output, size) != size: raise RuntimeError('局域网证书导出失败。')
                return output.raw
            finally: free_bio(bio)
        certificate_pem = export(lambda bio: pem_cert(bio, certificate))
        key_pem = export(lambda bio: pem_key(bio, key, None, None, 0, None, None))
        der = ssl.PEM_cert_to_DER_cert(certificate_pem.decode('ascii'))
        return certificate_pem, key_pem, hashlib.sha256(der).hexdigest()
    finally:
        if certificate: free_cert(certificate)
        if key: free_key(key)


def _send_json(stream, data):
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf8')
    if len(payload) > MAX_MANIFEST_BYTES: raise ValueError('传输清单过大。')
    stream.sendall(struct.pack('!I', len(payload)) + payload)


def _read_exact(stream, size, cancel, *, timeout=45):
    output = bytearray(); last_data = time.monotonic()
    while len(output) < size:
        if cancel.is_set(): raise InterruptedError('已取消传输。')
        try: data = stream.recv(min(size - len(output), BLOCK_BYTES))
        except socket.timeout:
            if time.monotonic() - last_data > timeout: raise TimeoutError('局域网连接超时。')
            continue
        if not data: raise ConnectionError('局域网连接已中断。')
        output.extend(data); last_data = time.monotonic()
    return bytes(output)


def _read_json(stream, cancel, *, timeout=45):
    size = struct.unpack('!I', _read_exact(stream, 4, cancel, timeout=timeout))[0]
    if size > MAX_MANIFEST_BYTES: raise ValueError('传输清单过大。')
    result = json.loads(_read_exact(stream, size, cancel, timeout=timeout).decode('utf8'))
    if not isinstance(result, dict): raise ValueError('传输消息无效。')
    return result


def _portable_path(value):
    if not isinstance(value, str) or not value or '\\' in value: raise ValueError('文件路径无效。')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('', '.', '..') or safe_name(p) != p for p in path.parts):
        raise ValueError('文件名无法安全地跨设备传输。')
    if str(path) != value or len(value) > 2048: raise ValueError('文件路径无效。')
    return path


def _validate_manifest(entries):
    if not isinstance(entries, list) or not 0 < len(entries) <= MAX_ENTRIES:
        raise ValueError('传输项目数量无效。')
    seen = {}; total = 0
    for row in entries:
        if not isinstance(row, dict): raise ValueError('传输清单无效。')
        path = _portable_path(row.get('path')); key = str(path).casefold()
        if key in seen: raise ValueError('传输清单包含重复的文件名。')
        kind = row.get('kind'); size = row.get('size')
        if kind not in ('file', 'directory') or type(size) is not int or size < 0 or (kind == 'directory' and size):
            raise ValueError('传输项目大小无效。')
        for parent in path.parents:
            if str(parent) == '.': continue
            if seen.get(str(parent).casefold()) != 'directory': raise ValueError('传输清单目录无效。')
        seen[key] = kind; total += size
        if total > MAX_TOTAL_BYTES: raise ValueError('传输总大小无效。')
    return total


def _make_manifest(paths, cancel):
    entries = []; sources = {}; used = set(); selected = []
    for raw in paths:
        path = Path(raw).absolute()
        if _is_link(path): raise ValueError('局域网传输不跟随符号链接或目录联接。')
        canonical = path.resolve(strict=True)
        if canonical not in selected: selected.append(canonical)
    selected = [p for p in selected if not any(p != other and other.is_dir() and p.is_relative_to(other) for other in selected)]
    def add(source, relative):
        if cancel.is_set(): raise InterruptedError('已取消传输。')
        if _is_link(source): raise ValueError('文件夹中含有符号链接或目录联接。')
        _portable_path(relative)
        info = source.stat(); folder = source.is_dir()
        if not folder and not source.is_file(): raise ValueError('无法传输此文件类型。')
        entries.append({'path': relative, 'kind': 'directory' if folder else 'file', 'size': 0 if folder else info.st_size})
        if len(entries) > MAX_ENTRIES: raise ValueError('传输项目过多。')
        if folder:
            for child in sorted(source.iterdir(), key=lambda p: p.name.casefold()): add(child, relative + '/' + child.name)
        else: sources[relative] = (source, info.st_size, info.st_mtime_ns)
    for path in selected:
        root = path.name; index = 2
        while root.casefold() in used:
            root = f'{path.stem} ({index}){path.suffix}' if path.is_file() else f'{path.name} ({index})'; index += 1
        used.add(root.casefold()); add(path, root)
    total = _validate_manifest(entries)
    # Encode once before connecting, so no receiver waits on oversized manifests.
    if len(json.dumps(entries, ensure_ascii=False).encode('utf8')) > MAX_MANIFEST_BYTES - 4096:
        raise ValueError('传输清单过大。')
    return entries, sources, total


def _authentication_code(stream, transfer_id):
    # TLS Finished is generated by OpenSSL from the session's secret key.
    # Public discovery hashes or freely chosen offer tokens alone would let an
    # active intermediary brute-force a matching six-digit code offline.
    binding = stream.get_channel_binding('tls-unique')
    if not binding: raise ValueError('当前系统不支持局域网身份核对，请更新 FlickDock。')
    digest = hashlib.sha256(binding + transfer_id.encode('ascii')).digest()
    return f'{int.from_bytes(digest[:8], "big") % 1000000:06d}'


@dataclass
class _Transfer:
    transfer_id: str
    direction: str
    peer: str
    cancel: threading.Event = field(default_factory=threading.Event)
    accepted: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    socket: object = None
    destination: object = None
    stage: object = None
    entries: list = field(default_factory=list)
    token: str = ''
    manifest_hash: str = ''
    next_file: int = 0
    offset: int = 0
    total: int = 0
    done: int = 0
    active: bool = False
    finished: bool = False
    result_paths: list = field(default_factory=list)
    remote_peer: dict = field(default_factory=dict)
    updated: float = field(default_factory=time.monotonic)


class LanTransferService(QObject):
    peers_changed = Signal(list)
    offer_received = Signal(dict)
    progress = Signal(dict)
    completed = Signal(dict)
    status_changed = Signal(str)

    def __init__(self, parent=None, *, device_name='', device_id='', bind_host='0.0.0.0',
                 discovery=True, discovery_port=DISCOVERY_PORT):
        super().__init__(parent)
        self.device_name = str(device_name or socket.gethostname())[:64]
        self.device_id = device_id or uuid4().hex
        self.bind_host = bind_host; self.discovery_enabled = bool(discovery)
        self.discovery_port = discovery_port; self.port = 0; self.fingerprint = ''
        self._stopped = threading.Event(); self._lock = threading.RLock()
        self._peers = {}; self._transfers = {}; self._threads = []
        self._listener = None; self._discovery_socket = None; self._certificate_dir = None
        self._slots = threading.BoundedSemaphore(4); self._context = None; self._starting = False

    def _thread(self, function, *args):
        thread = threading.Thread(target=function, args=args, daemon=True, name='FlickDock-LAN')
        with self._lock:
            self._threads[:] = [existing for existing in self._threads if existing.is_alive()]
            self._threads.append(thread); thread.start()

    def start(self):
        if self._starting or self._listener: return
        self._starting = True; self._stopped.clear(); self._thread(self._start)

    def _start(self):
        try:
            cert, key, self.fingerprint = _ephemeral_certificate()
            folder = tempfile.TemporaryDirectory(prefix='flickdock-lan-tls-'); self._certificate_dir = folder
            certificate = Path(folder.name, 'session.pem'); private = Path(folder.name, 'session-key.pem')
            certificate.write_bytes(cert)
            descriptor = os.open(str(private), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as output: output.write(key)
            self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._context.minimum_version = ssl.TLSVersion.TLSv1_2
            self._context.load_cert_chain(str(certificate), str(private))
            # OpenSSL has copied the key into memory; do not retain it on disk.
            folder.cleanup(); self._certificate_dir = None
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.bind_host, 0)); listener.listen(8); listener.settimeout(.5)
            self._listener = listener; self.port = listener.getsockname()[1]
            if self._stopped.is_set(): listener.close(); return
            self.status_changed.emit('局域网互传已就绪。')
            if self.discovery_enabled: self._thread(self._discovery)
            while not self._stopped.is_set():
                try: stream, address = listener.accept()
                except socket.timeout: self._expire(); continue
                except OSError:
                    if self._stopped.is_set(): break
                    raise
                if not _local_address(address[0]) or not self._slots.acquire(blocking=False):
                    stream.close(); continue
                self._thread(self._incoming, stream, address[0])
        except Exception as error:
            if not self._stopped.is_set(): self.status_changed.emit(str(error))
        finally:
            self._starting = False

    def own_peer(self, host='127.0.0.1'):
        """Useful for explicit address pairing and disposable loopback verification."""
        return {'id': self.device_id, 'name': self.device_name, 'host': host,
                'port': self.port, 'fingerprint': self.fingerprint, 'protocol': PROTOCOL}

    def remember_peer(self, peer):
        peer = _validate_peer(peer)
        if peer['id'] == self.device_id: return
        with self._lock:
            self._peers[peer['id']] = dict(peer, seen=time.monotonic())
            peers = self.peers()
        self.peers_changed.emit(peers)

    def peers(self):
        with self._lock:
            return [dict(row) for row in sorted(self._peers.values(), key=lambda row: row['name'].casefold())]

    def _discovery(self):
        discovery = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            discovery.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            discovery.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            discovery.bind(('', self.discovery_port)); discovery.settimeout(.5)
            self._discovery_socket = discovery; next_announcement = 0
            while not self._stopped.is_set():
                now = time.monotonic()
                if now >= next_announcement:
                    payload = self.own_peer(); payload.pop('host')
                    encoded = json.dumps(payload, ensure_ascii=False).encode('utf8')
                    for host in _broadcast_addresses():
                        try: discovery.sendto(encoded, (host, self.discovery_port))
                        except OSError: pass
                    next_announcement = now + 3
                try:
                    payload, address = discovery.recvfrom(2049)
                    if len(payload) > 2048 or not _local_address(address[0]): continue
                    row = json.loads(payload.decode('utf8')); row['host'] = address[0]
                    self.remember_peer(row)
                except (socket.timeout, ValueError, TypeError, KeyError): pass
                self._expire()
        except OSError:
            if not self._stopped.is_set(): self.status_changed.emit('局域网发现不可用，请检查网络权限或防火墙。')
        finally: discovery.close()

    def _expire(self):
        now = time.monotonic(); changed = False
        with self._lock:
            for identifier, row in list(self._peers.items()):
                if now - row['seen'] > 15: del self._peers[identifier]; changed = True
            for identifier, transfer in list(self._transfers.items()):
                if not transfer.active and now - transfer.updated > 1800:
                    transfer.cancel.set()
                    if self._cleanup(transfer): del self._transfers[identifier]
                    else: transfer.updated = now - 1770  # Retry an occupied stage after 30 s.
        if changed: self.peers_changed.emit(self.peers())

    def send(self, peer_id, paths):
        with self._lock:
            peer = dict(self._peers.get(peer_id) or {})
            if not peer: raise ValueError('这台设备已离线，请重新选择。')
            if sum(t.active and t.direction == 'send' for t in self._transfers.values()) >= 2:
                raise ValueError('已有两项局域网传输正在进行。')
            identifier = uuid4().hex
            transfer = _Transfer(identifier, 'send', peer['name'], active=True)
            transfer.remote_peer = peer
            self._transfers[identifier] = transfer
        self._thread(self._outgoing, transfer, peer, list(paths))
        return identifier

    def accept(self, transfer_id, destination):
        with self._lock: transfer = self._transfers.get(transfer_id)
        if not transfer or transfer.direction != 'receive' or transfer.cancel.is_set(): return False
        destination = Path(destination).expanduser().resolve(strict=True)
        if not destination.is_dir() or _is_link(destination): raise ValueError('请选择可写入的接收文件夹。')
        if shutil.disk_usage(destination).free < transfer.total + 1024 * 1024:
            raise ValueError('接收位置的可用空间不足。')
        transfer.destination = destination; transfer.accepted.set(); return True

    def reject(self, transfer_id): self.cancel(transfer_id)

    def cancel(self, transfer_id):
        with self._lock: transfer = self._transfers.get(transfer_id)
        if not transfer or transfer.finished: return
        transfer.cancel.set(); transfer.accepted.set()
        stream = transfer.socket
        if stream:
            try: stream.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        if transfer.direction == 'send' and transfer.token:
            self._thread(self._cancel_remote, transfer)
        if not transfer.active:
            cleaned = self._cleanup(transfer)
            self._notify(transfer, 'cancelled' if cleaned else 'error',
                         message='' if cleaned else '未完成文件暂时被占用，将继续重试清理。')

    def _cancel_remote(self, transfer):
        """A second authenticated control stream distinguishes cancel from loss.

        Closing a data socket alone is a resumable interruption. A random transfer
        token sent only inside TLS authorizes removal of that transfer's staging.
        """
        peer = transfer.remote_peer
        for attempt in range(4):
            stream = None
            try:
                raw = socket.create_connection((peer['host'], peer['port']), timeout=2)
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                try: stream = context.wrap_socket(raw, server_hostname='flickdock-lan')
                except BaseException: raw.close(); raise
                if not secrets.compare_digest(hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest(), peer['fingerprint']): return
                stream.settimeout(1)
                _send_json(stream, {'protocol': PROTOCOL, 'id': transfer.transfer_id,
                                    'token': transfer.token, 'action': 'cancel'})
                answer = _read_json(stream, threading.Event(), timeout=2)
                if answer.get('state') == 'cancelled': return
            except (OSError, ConnectionError, TimeoutError):
                if attempt < 3: time.sleep(.3)
            finally:
                if stream: stream.close()

    def _notify(self, transfer, state, **extra):
        transfer.updated = time.monotonic()
        self.progress.emit(dict(id=transfer.transfer_id, direction=transfer.direction, peer=transfer.peer,
                                state=state, received=transfer.done, total=transfer.total, **extra))

    def _outgoing(self, transfer, peer, paths):
        stream = None
        try:
            self._notify(transfer, 'preparing')
            entries, sources, total = _make_manifest(paths, transfer.cancel)
            transfer.total = total; token = secrets.token_hex(32)
            transfer.token = token
            code = ''
            offer = dict(protocol=PROTOCOL, id=transfer.transfer_id, token=token,
                         sender=self.device_name, entries=entries)
            files = [row for row in entries if row['kind'] == 'file']
            for attempt in range(3):
                if transfer.cancel.is_set(): raise InterruptedError('已取消传输。')
                try:
                    raw = socket.create_connection((peer['host'], peer['port']), timeout=8)
                    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    context.check_hostname = False; context.verify_mode = ssl.CERT_NONE
                    context.minimum_version = ssl.TLSVersion.TLSv1_2
                    try: stream = context.wrap_socket(raw, server_hostname='flickdock-lan')
                    except BaseException:
                        raw.close(); raise
                    if not secrets.compare_digest(hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest(), peer['fingerprint']):
                        raise ValueError('设备的加密身份已改变，请重新发现并核对接收设备。')
                    stream.settimeout(1); transfer.socket = stream
                    code = _authentication_code(stream, transfer.transfer_id)
                    _send_json(stream, offer); self._notify(transfer, 'waiting', code=code)
                    reply = _read_json(stream, transfer.cancel, timeout=180)
                    if reply.get('state') != 'ready': raise ValueError(reply.get('message') or '接收方拒绝了传输。')
                    index, offset = reply.get('next_file'), reply.get('offset')
                    if type(index) is not int or not 0 <= index <= len(files) or type(offset) is not int or offset < 0:
                        raise ValueError('接收方的续传位置无效。')
                    if index == len(files) and offset: raise ValueError('接收方的续传位置无效。')
                    transfer.done = sum(row['size'] for row in files[:index]) + offset
                    last_emit = 0
                    for number in range(index, len(files)):
                        row = files[number]; source, size, modified = sources[row['path']]
                        info = source.stat()
                        if info.st_size != size or info.st_mtime_ns != modified: raise ValueError('原文件已改变，请重新发起传输。')
                        resume = offset if number == index else 0
                        if resume > size: raise ValueError('接收方的续传位置无效。')
                        digest = hashlib.sha256()
                        with _source_reader(source) as incoming:
                            prefix = resume
                            while prefix:
                                if transfer.cancel.is_set(): raise InterruptedError('已取消传输。')
                                data = incoming.read(min(BLOCK_BYTES, prefix))
                                if not data: raise ValueError('原文件已改变，请重新发起传输。')
                                digest.update(data); prefix -= len(data)
                            remaining = size - resume
                            while remaining:
                                if transfer.cancel.is_set(): raise InterruptedError('已取消传输。')
                                data = incoming.read(min(BLOCK_BYTES, remaining))
                                if not data: raise ValueError('原文件已改变，请重新发起传输。')
                                stream.sendall(data); digest.update(data)
                                transfer.done += len(data); remaining -= len(data)
                                if time.monotonic() - last_emit >= .08:
                                    self._notify(transfer, 'transferring', name=row['path'], code=code); last_emit = time.monotonic()
                            if incoming.read(1): raise ValueError('原文件已改变，请重新发起传输。')
                        current = source.stat()
                        if current.st_size != size or current.st_mtime_ns != modified: raise ValueError('原文件已改变，请重新发起传输。')
                        _send_json(stream, {'sha256': digest.hexdigest()})
                        answer = _read_json(stream, transfer.cancel)
                        if answer.get('state') != 'verified': raise ValueError('接收方文件校验失败。')
                    reply = _read_json(stream, transfer.cancel)
                    if reply.get('state') != 'complete': raise ValueError('接收方保存文件失败。')
                    transfer.finished = True; self._notify(transfer, 'complete', code=code)
                    self.completed.emit({'id': transfer.transfer_id, 'direction': 'send', 'peer': transfer.peer,
                                         'paths': [str(path) for path in paths], 'total': total})
                    return
                except (ConnectionError, TimeoutError, socket.timeout, OSError):
                    if transfer.cancel.is_set() or attempt == 2: raise
                    self._notify(transfer, 'reconnecting', code=code)
                    transfer.cancel.wait(.5 * (attempt + 1))
                finally:
                    if stream: stream.close(); stream = None
                    transfer.socket = None
        except Exception as error:
            self._notify(transfer, 'cancelled' if transfer.cancel.is_set() else 'error', message=str(error))
            if transfer.token and not transfer.cancel.is_set():
                self._thread(self._cancel_remote, transfer)
        finally: transfer.active = False; transfer.updated = time.monotonic()

    def _incoming(self, raw, host):
        stream = None; transfer = None; acquired = False; terminal = None
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            raw.settimeout(8); stream = self._context.wrap_socket(raw, server_side=True)
            stream.settimeout(1); request = _read_json(stream, self._stopped, timeout=10)
            identifier, token = request.get('id'), request.get('token')
            if request.get('protocol') != PROTOCOL or not _hex(identifier, 32) or not _hex(token, 64):
                raise ValueError('局域网传输协议无效。')
            if request.get('action') == 'cancel':
                with self._lock: candidate = self._transfers.get(identifier)
                if not candidate or candidate.direction != 'receive' or not secrets.compare_digest(candidate.token, token):
                    raise ValueError('无法取消这项传输。')
                self.cancel(identifier); _send_json(stream, {'state': 'cancelled'}); return
            entries = request.get('entries'); total = _validate_manifest(entries)
            manifest_hash = hashlib.sha256(json.dumps(entries, sort_keys=True).encode('utf8')).hexdigest()
            with self._lock:
                transfer = self._transfers.get(identifier)
                if transfer:
                    if transfer.direction != 'receive' or transfer.active or transfer.cancel.is_set() or not secrets.compare_digest(transfer.token, token) or transfer.manifest_hash != manifest_hash:
                        raise ValueError('无法恢复这项传输。')
                    if transfer.finished:
                        # A final acknowledgement can be lost after the atomic
                        # rename. A retry confirms that same completed transfer.
                        _send_json(stream, {'state': 'ready', 'next_file': sum(e['kind'] == 'file' for e in entries), 'offset': 0})
                        _send_json(stream, {'state': 'complete'}); return
                else:
                    if len([t for t in self._transfers.values() if not t.finished and not t.cancel.is_set()]) >= 16:
                        raise ValueError('待处理的传输请求过多。')
                    sender = str(request.get('sender') or '局域网设备')[:64]
                    transfer = _Transfer(identifier, 'receive', sender, entries=entries, token=token,
                                         manifest_hash=manifest_hash, total=total)
                    self._transfers[identifier] = transfer
                    code = _authentication_code(stream, identifier)
                    self.offer_received.emit({'id': identifier, 'peer': sender, 'host': host, 'code': code,
                                              'total': total, 'count': sum(e['kind'] == 'file' for e in entries),
                                              'names': [e['path'] for e in entries if '/' not in e['path']]})
                transfer.active = True; transfer.socket = stream; acquired = True
            deadline = time.monotonic() + 180
            while not transfer.accepted.wait(.2):
                if transfer.cancel.is_set() or self._stopped.is_set(): raise InterruptedError('已取消传输。')
                if time.monotonic() > deadline: transfer.cancel.set(); raise TimeoutError('接收请求已超时。')
            if transfer.cancel.is_set() or not transfer.destination: raise InterruptedError('已拒绝传输。')
            if transfer.stage is None:
                stage = transfer.destination / ('.flickdock-receiving-' + identifier)
                stage.mkdir(mode=0o700); transfer.stage = stage
                for row in entries:
                    if row['kind'] == 'directory': stage.joinpath(*PurePosixPath(row['path']).parts).mkdir()
            files = [row for row in entries if row['kind'] == 'file']
            _send_json(stream, {'state': 'ready', 'next_file': transfer.next_file, 'offset': transfer.offset})
            last_emit = 0
            for number in range(transfer.next_file, len(files)):
                row = files[number]; target = transfer.stage.joinpath(*PurePosixPath(row['path']).parts)
                offset = transfer.offset if number == transfer.next_file else 0
                digest = hashlib.sha256()
                if offset:
                    with target.open('rb') as prefix:
                        remaining = offset
                        while remaining:
                            if transfer.cancel.is_set(): raise InterruptedError('已取消传输。')
                            data = prefix.read(min(BLOCK_BYTES, remaining))
                            if not data: raise ValueError('本机续传数据不完整。')
                            digest.update(data); remaining -= len(data)
                with target.open('r+b' if offset else 'xb') as output:
                    output.seek(offset)
                    while offset < row['size']:
                        data = _read_exact(stream, min(BLOCK_BYTES, row['size'] - offset), transfer.cancel)
                        output.write(data); digest.update(data); offset += len(data)
                        transfer.offset = offset; transfer.done += len(data)
                        if time.monotonic() - last_emit >= .08:
                            self._notify(transfer, 'transferring', name=row['path']); last_emit = time.monotonic()
                    output.flush(); os.fsync(output.fileno())
                trailer = _read_json(stream, transfer.cancel)
                if not secrets.compare_digest(str(trailer.get('sha256')), digest.hexdigest()):
                    raise ValueError('文件完整性校验失败，已移除未完成文件。')
                transfer.next_file = number + 1; transfer.offset = 0
                _send_json(stream, {'state': 'verified'})
            if transfer.cancel.is_set(): raise InterruptedError('已取消传输。')
            folder = transfer.destination / ('FlickDock ' + time.strftime('%Y-%m-%d %H-%M-%S') + '-' + identifier[:6])
            # Never replace an existing user path or expose unverified partial data.
            if folder.exists(): raise ValueError('接收目标已存在，请重试。')
            os.rename(transfer.stage, folder); transfer.stage = None; transfer.finished = True
            result_paths = [str(folder / e['path']) for e in entries if '/' not in e['path']]
            transfer.result_paths = result_paths
            self._notify(transfer, 'complete')
            self.completed.emit({'id': identifier, 'direction': 'receive', 'peer': transfer.peer,
                                 'paths': result_paths, 'total': total})
            _send_json(stream, {'state': 'complete'})
        except Exception as error:
            if transfer and acquired and not transfer.finished:
                state = 'cancelled' if transfer.cancel.is_set() else 'interrupted'
                network_error = isinstance(error, (ConnectionError, TimeoutError, socket.timeout, ssl.SSLError)) or (
                    isinstance(error, OSError) and error.errno in (errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE, errno.ETIMEDOUT))
                if not transfer.cancel.is_set() and not network_error:
                    transfer.cancel.set(); state = 'error'
                if state in ('cancelled', 'error'): terminal = (state, str(error))
                else: self._notify(transfer, state, message=str(error))
            if stream and not (transfer and transfer.finished):
                try: _send_json(stream, {'state': 'error', 'message': str(error)})
                except OSError: pass
        finally:
            if stream: stream.close()
            else: raw.close()
            if transfer and acquired:
                transfer.socket = None
                cleaned = self._cleanup(transfer) if transfer.cancel.is_set() else True
                transfer.active = False; transfer.updated = time.monotonic()
                if terminal:
                    state, message = terminal
                    if not cleaned: state, message = 'error', '未完成文件暂时被占用，将继续重试清理。'
                    self._notify(transfer, state, message=message)
            self._slots.release()

    @staticmethod
    def _cleanup(transfer):
        if not transfer.stage: return True
        stage = Path(transfer.stage)
        try:
            # A recursive removal always resolves and checks the exact owned
            # child of the user-selected destination before touching disk.
            if (not transfer.destination or stage.name != '.flickdock-receiving-' + transfer.transfer_id
                    or not _hex(transfer.transfer_id, 32) or stage.is_symlink()
                    or (hasattr(stage, 'is_junction') and stage.is_junction())
                    or stage.resolve().parent != Path(transfer.destination).resolve()):
                return False
            for attempt in range(3):
                try:
                    if stage.exists(): shutil.rmtree(stage)
                    transfer.stage = None; return True
                except OSError:
                    if attempt < 2: time.sleep(.05 * (attempt + 1))
            return False
        except (OSError, ValueError): return False

    def close(self):
        self._stopped.set()
        with self._lock:
            transfers = list(self._transfers.values())
            for transfer in transfers: self.cancel(transfer.transfer_id)
        for stream in (self._listener, self._discovery_socket):
            if stream:
                try: stream.close()
                except OSError: pass
        # Qt shutdown need not wait for discovery/handshake threads; they have
        # bounded socket timeouts. Writers close handles and clean in finally.
        if self._certificate_dir: self._certificate_dir.cleanup()


def _hex(value, length):
    return isinstance(value, str) and len(value) == length and all(c in '0123456789abcdef' for c in value)


def _local_address(host):
    try:
        address = ipaddress.ip_address(host)
        return address.version == 4 and not address.is_multicast and not address.is_unspecified and (address.is_private or address.is_link_local or address.is_loopback)
    except ValueError: return False


def _validate_peer(row):
    if not isinstance(row, dict) or row.get('protocol') != PROTOCOL or not _hex(row.get('id'), 32) or not _hex(row.get('fingerprint'), 64):
        raise ValueError('设备发现信息无效。')
    if not _local_address(row.get('host', '')) or type(row.get('port')) is not int or not 0 < row['port'] < 65536:
        raise ValueError('设备地址无效。')
    return {'id': row['id'], 'name': str(row.get('name') or '局域网设备')[:64], 'host': row['host'],
            'port': row['port'], 'fingerprint': row['fingerprint'], 'protocol': PROTOCOL}


def _broadcast_addresses():
    addresses = {'255.255.255.255'}
    try:
        from PySide6.QtNetwork import QNetworkInterface
        for interface in QNetworkInterface.allInterfaces():
            for entry in interface.addressEntries():
                broadcast = entry.broadcast().toString()
                if broadcast and ':' not in broadcast: addresses.add(broadcast)
    except Exception: pass
    return addresses

"""Bounded asynchronous cloud transport. Session headers never follow redirects."""
from __future__ import annotations
import hashlib
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal, QFile, QIODevice
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from app_version import APP_VERSION


class CloudError(Exception):
    def __init__(self, message, status=0, code='', details=None):
        super().__init__(message)
        self.status, self.code, self.details = status, code, details


class CloudApi(QObject):
    unauthorized = Signal()
    supports_file_upload = True

    def __init__(self, origin, parent=None, *, network=None, allow_test_http=False):
        super().__init__(parent)
        self.origin = str(origin).rstrip('/')
        self.network = network or QNetworkAccessManager(self)
        self.token = ''
        self._requests = {}
        self._deferred = {}
        self._closed = False
        self._allow_test_http = allow_test_http

    def _url(self, path):
        base = QUrl(self.origin)
        local_test = (self._allow_test_http and base.scheme() == 'http'
                      and base.host() in ('127.0.0.1', 'localhost', '::1'))
        if (not base.isValid() or not base.host() or base.userName() or base.password()
                or base.hasQuery() or base.hasFragment() or base.path() not in ('','/')
                or not (base.scheme() == 'https' or local_test)):
            raise CloudError('云同步服务尚未连接。')
        if not path.startswith('/v1/') or '://' in path or '#' in path:
            raise CloudError('同步请求无效。')
        return QUrl(self.origin + path)

    def request(self, method, path, callback, *, data=None, raw=None, source=None, headers=None,
                authenticated=True, destination=None, expected=None, timeout_ms=45000,
                progress=None):
        """callback(result,error); optional progress(sent,total) reports raw upload.

        Upload progress counts bytes handed to the network, not committed R2
        content. Only the successful protocol response confirms acceptance.
        """
        key = uuid4().hex
        source_file = None
        try:
            if self._closed: raise CloudError('同步服务已关闭。')
            request = QNetworkRequest(self._url(path))
            request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                                 QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
            request.setRawHeader(b'Accept', b'application/json')
            request.setRawHeader(b'User-Agent',('FlickDock/'+APP_VERSION+' (CloudSync)').encode('ascii'))
            request.setRawHeader(b'Cache-Control', b'no-store')
            if authenticated:
                if not self.token: raise CloudError('请先登录账号。', 401, 'unauthorized')
                request.setRawHeader(b'Authorization', ('Bearer ' + self.token).encode('ascii'))
            if source is not None and (raw is not None or data is not None):
                raise CloudError('同步请求不能重复提供文件内容。')
            payload = raw if raw is not None else json.dumps(data, ensure_ascii=False).encode('utf8') if data is not None else b''
            upload_size = len(payload)
            if source is not None:
                path_to_send = Path(source)
                if path_to_send.is_symlink() or not path_to_send.is_file():
                    raise CloudError('待上传文件已改变。')
                upload_size = path_to_send.stat().st_size
                source_file = QFile(str(path_to_send))
                if not source_file.open(QIODevice.OpenModeFlag.ReadOnly):
                    raise CloudError('无法读取待上传文件。')
                # Qt otherwise may buffer the complete 80 MiB payload before
                # sending it. QFile remains open until this reply finishes.
                request.setAttribute(QNetworkRequest.Attribute.DoNotBufferUploadDataAttribute, True)
                request.setRawHeader(b'Content-Length', str(upload_size).encode('ascii'))
            # A large streamed R2 PUT is one network request rather than many
            # small requests. Keep Qt's idle timeout, while allowing the total
            # upload time to scale with bytes on a slower uplink.
            total_timeout = (max(timeout_ms, min(15 * 60 * 1000,
                60000 + (upload_size * 1000 // (256 * 1024))))
                if (raw is not None or source is not None) and upload_size > 4 * 1024 * 1024 else timeout_ms)
            request.setTransferTimeout(timeout_ms)
            request.setRawHeader(b'Content-Type', b'application/octet-stream' if raw is not None or source is not None else b'application/json')
            for name, value in (headers or {}).items(): request.setRawHeader(name.encode('ascii'), str(value).encode('ascii'))
            reply = self.network.sendCustomRequest(request, method.encode('ascii'), source_file or payload)
            timer = QTimer(self); timer.setSingleShot(True)
            state = {'reply':reply,'callback':callback,'timer':timer,'body':bytearray(),
                     'received':0,'expected':expected,'destination':Path(destination) if destination else None,
                     'part':None,'file':None,'hash':hashlib.sha256(),'source_file':source_file,
                     'progress':progress if callable(progress) and (raw is not None or source is not None) else None,
                     'upload_total':upload_size,'upload_sent':-1,'progress_at':0.,
                     'auth_token':self.token if authenticated else None}
            self._requests[key] = state
            timer.timeout.connect(lambda: self._finish(key, error=CloudError('同步请求超时，请检查网络。')))
            reply.readyRead.connect(lambda: self._drain(key))
            reply.finished.connect(lambda: self._finish(key))
            if state['progress'] is not None:
                # Delivery is queued onto this object's GUI event loop. State
                # lookup then discards signals already queued before cancel.
                signal=getattr(reply,'uploadProgress',None)
                if signal is not None:
                    report=lambda sent,total:self._upload_progress(key,sent,total)
                    try:signal.connect(report,Qt.ConnectionType.QueuedConnection)
                    except TypeError:signal.connect(report)  # Minimal injected test transports.
            timer.start(max(total_timeout, 300000) if destination else total_timeout)
        except Exception as error:
            if source_file is not None and key not in self._requests:
                source_file.close()
                source_file.deleteLater()
            problem = error if isinstance(error, CloudError) else CloudError('无法连接云同步服务。')
            if not self._closed:
                self._deferred[key]=(callback,problem)
                QTimer.singleShot(0,lambda:self._validation_error(key))
        return key

    def _validation_error(self,key):
        pending=self._deferred.pop(key,None)
        if pending is not None and not self._closed:pending[0](None,pending[1])

    def _upload_progress(self,key,sent,total):
        state=self._requests.get(key)
        if (self._closed or state is None or state['progress'] is None
                or state['auth_token'] is not None and state['auth_token']!=self.token):return
        try:sent=int(sent)
        except (TypeError,ValueError,OverflowError):return
        if sent<0:return
        total=state['upload_total'];sent=min(sent,total)
        if sent<=state['upload_sent']:return
        now=time.monotonic()
        if state['upload_sent']>=0 and sent<total and now-state['progress_at']<.06:return
        state['upload_sent'],state['progress_at']=sent,now
        try:state['progress'](sent,total)
        except Exception:pass  # A stale/deleted view must not abort a transfer.

    def _drain(self, key):
        state = self._requests.get(key)
        if state is None: return
        reply = state['reply']
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
        try:
            budget = 512 * 1024
            while reply.bytesAvailable() and budget > 0:
                chunk = bytes(reply.read(min(65536, budget))); budget -= len(chunk)
                if not chunk: break
                state['received'] += len(chunk)
                if state['destination'] is not None and status == 200:
                    expected = state['expected'] or {}
                    if state['received'] > expected.get('size', 0): raise CloudError('同步文件大小与记录不符。')
                    if state['file'] is None:
                        target = state['destination']; target.parent.mkdir(parents=True, exist_ok=True)
                        part = target.with_name('.' + target.name + '-' + key + '.part')
                        state['part'], state['file'] = part, part.open('xb')
                    state['file'].write(chunk); state['hash'].update(chunk)
                else:
                    if len(state['body']) + len(chunk) > 2 * 1024 * 1024: raise CloudError('云服务响应超过限制。')
                    state['body'].extend(chunk)
            if reply.bytesAvailable(): QTimer.singleShot(0, lambda: self._drain(key))
        except Exception as error:
            self._finish(key, error=error if isinstance(error, CloudError) else CloudError('无法保存同步文件。'))

    def _finish(self, key, error=None):
        state = self._requests.get(key)
        if state is None: return
        if error is None:
            self._drain(key)
            state = self._requests.get(key)
            if state is None: return
            if state['reply'].bytesAvailable():
                QTimer.singleShot(0, lambda: self._finish(key)); return
        self._requests.pop(key, None)
        state['timer'].stop(); state['timer'].deleteLater()
        reply = state['reply']
        status = int(reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0)
        result = None
        try:
            if error is not None: raise error
            if status == 200 and reply.error() != QNetworkReply.NetworkError.NoError:
                raise CloudError('同步网络连接已中断。',status)
            if status == 200 and state['destination'] is not None:
                expected = state['expected']
                digest = expected.get('sha256') or bytes(reply.rawHeader('X-Content-SHA256')).decode('ascii','replace')
                if (not re.fullmatch(r'[0-9a-f]{64}',digest) or state['received'] != expected['size'] or state['hash'].hexdigest() != digest):
                    raise CloudError('同步文件校验失败，将自动重试。', details={
                        'received_size':state['received'], 'expected_size':expected['size'],
                        'hash_matches':state['hash'].hexdigest() == digest,
                        'network_error':int(reply.error())})
                if state['file'] is None:
                    state['part'] = state['destination'].with_name('.' + key + '.part')
                    state['file'] = state['part'].open('xb')
                state['file'].flush(); os.fsync(state['file'].fileno()); state['file'].close(); state['file'] = None
                os.replace(state['part'], state['destination']); state['part'] = None
                # The bytes were hashed while streaming to this fsynced file.
                # Callers that opt in can avoid rereading a large part before
                # the final whole-file verification. Keep the legacy Path
                # result for clients and injected transports using this API.
                result = ({'path':state['destination'],'sha256':digest}
                          if expected.get('with_digest') else state['destination'])
            else:
                response_body = bytes(state['body'])
                if status in (429, 503, 1027) and b'1027' in response_body and b'cloudflare' in response_body.lower():
                    now = time.time()
                    raise CloudError('云同步免费请求额度今日已用完。', status, 'PLATFORM_QUOTA',
                                     {'error': {'retry_after': int(86400 - now % 86400) + 1}})
                try: parsed = json.loads(state['body'].decode('utf8')) if state['body'] else {}
                except (UnicodeError,ValueError): raise CloudError('云同步服务返回了无效响应。',status)
                if status not in (200,201,202,204):
                    problem = parsed.get('error', {}) if isinstance(parsed,dict) else {}
                    if isinstance(problem,str): problem = {'message':problem}
                    raise CloudError(problem.get('message') or '云同步请求失败，请稍后重试。',status,problem.get('code',''),parsed)
                if reply.error() != QNetworkReply.NetworkError.NoError: raise CloudError('同步网络连接已中断。',status)
                if not isinstance(parsed,dict) or parsed.get('schema_version') != 1:
                    raise CloudError('云同步服务版本不匹配。',status)
                result = parsed
        except Exception as problem:
            error = problem if isinstance(problem,CloudError) else CloudError(
                '云同步失败，请稍后重试。',status,
                details={'cause':type(problem).__name__,'detail':str(problem)[:160]})
        finally:
            if not reply.isFinished(): reply.abort()
            reply.deleteLater()
            if state['source_file'] is not None:
                state['source_file'].close()
                state['source_file'].deleteLater()
            if state['file'] is not None: state['file'].close()
            if state['part'] is not None: state['part'].unlink(missing_ok=True)
        if status == 401 and state['auth_token'] and state['auth_token'] == self.token and not self._closed:
            self.unauthorized.emit()
        if not self._closed: state['callback'](result,error)

    def cancel(self, key):
        pending=self._deferred.pop(key,None)
        if pending is not None:
            if not self._closed:pending[0](None,CloudError('同步操作已取消。',code='cancelled'))
            return
        self._finish(key, CloudError('同步操作已取消。',code='cancelled'))

    def cancel_all(self):
        for key in (*self._requests,*self._deferred): self.cancel(key)

    def close(self):
        self._closed = True
        self.cancel_all()

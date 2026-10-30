"""Recoverable Qt HTTPS download; disk verification stays off the UI thread."""
from __future__ import annotations
import hashlib,os,re,threading,time,weakref
from collections import deque
from pathlib import Path
from PySide6.QtCore import QObject,QRunnable,QThreadPool,QTimer,QUrl,Signal,Qt
from PySide6.QtNetwork import QNetworkReply,QNetworkRequest

_leases={}
_lease_lock=threading.RLock()


class _Signals(QObject):
    finished=Signal(object,object)


class _Disk(QRunnable):
    def __init__(self,function):
        super().__init__();self.function=function;self.cancelled=threading.Event();self.signals=_Signals()
    def run(self):
        try:value,error=self.function(self.cancelled),None
        except Exception as problem:value,error=None,problem
        self.signals.finished.emit(value,error)


def _inspect(path,limit,cancel):
    digest=hashlib.sha256();prefix=b'';size=0
    if path.is_symlink():raise ValueError('更新暂存位置不安全。')
    if path.exists():
        with path.open('rb') as stream:
            while data:=stream.read(1024*1024):
                if cancel.is_set():raise InterruptedError()
                if not prefix:prefix=data[:512]
                digest.update(data);size+=len(data)
                if size>limit:raise ValueError('更新暂存内容超过安装包大小。')
    return size,digest,prefix


class UpdateDownload(QObject):
    progress=Signal(int,int)
    status=Signal(str,dict)
    completed=Signal(str)
    failed=Signal(str)

    def __init__(self,network,manifest,directory,platform,version,hosts,*,timeout_ms=15000,parent=None):
        super().__init__(parent);self.network=network;self.manifest=manifest;self.platform=platform
        self.version=version;self.hosts=hosts;self.info=manifest['installer'];self.total=self.info['size']
        self.directory=Path(directory);self.part=self.directory/('package-'+self.info['sha256']+'.part')
        self._lease=object();self._lease_key=str(self.part.absolute())
        suffix='.dmg' if platform=='darwin' and QUrl(self.info['url']).path().endswith('.dmg') else '.zip' if platform=='darwin' else '.exe'
        self.final=self.directory/('update'+suffix if platform=='darwin' else 'setup.exe')
        self.timeout_ms=max(50,int(timeout_ms));self.stall_ms=max(1000,self.timeout_ms*3)
        self.reply=None;self.file=None;self.jobs=set();self.stopped=False;self.delete_partial=False;self.retain_lease=False
        self.received=0;self.digest=hashlib.sha256();self.prefix=b'';self.attempts=0;self.max_retries=5
        self.redirects=0;self.offset=0;self.headers=False;self.phase='connecting';self.samples=deque()
        self._last=time.monotonic();self._request_started=self._last;self._last_status=0
        self.watch=QTimer(self);self.watch.setInterval(250);self.watch.timeout.connect(self._watch)
        self.retry=QTimer(self);self.retry.setSingleShot(True);self.retry.timeout.connect(self._restart)

    def _disk(self,function,callback):
        job=_Disk(function);self.jobs.add(job)
        def done(value,error):
            self.jobs.discard(job)
            if self.stopped:
                if self.retain_lease:return
                try:self._release(discard=self.delete_partial)
                except OSError:pass # A locked temporary file is safe to retain.
                return
            callback(value,error)
        job.signals.finished.connect(done);QThreadPool.globalInstance().start(job)

    def start(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        with _lease_lock:
            existing=_leases.get(self._lease_key)
            previous=existing[1]() if existing else None
            if previous is not None and not previous.stopped:raise RuntimeError('此安装包已有下载任务，请稍后重试。')
            _leases[self._lease_key]=(self._lease,weakref.ref(self))
        self._emit('connecting',detail='正在检查可恢复的下载…')
        def ready(value,error):
            if error:self._fail(str(error),discard=True);return
            self.received,self.digest,self.prefix=value
            self.progress.emit(self.received,self.total)
            if self.stopped:return
            if self.received==self.total:self._verify();return
            try:self.file=self.part.open('ab')
            except OSError as problem:self._fail('无法写入更新暂存文件：'+str(problem));return
            self._restart()
        self._disk(lambda cancel:_inspect(self.part,self.total,cancel),ready)

    def _emit(self,phase,**extra):
        self.phase=phase;now=time.monotonic()
        while len(self.samples)>1 and self.samples[1][0]<=now-4:self.samples.popleft()
        speed=0
        if len(self.samples)>1:
            elapsed=now-self.samples[0][0]
            if elapsed>.05:speed=max(0,(self.received-self.samples[0][1])/elapsed)
        self.status.emit(phase,dict(received=self.received,total=self.total,speed=speed,
            eta=(self.total-self.received)/speed if speed>0 else None,attempt=self.attempts,**extra))
        self._last_status=now

    def _restart(self):
        if self.stopped:return
        self.redirects=0;self.offset=self.received;self.headers=False
        self.samples.clear();self.samples.append((time.monotonic(),self.received))
        self._emit('reconnecting' if self.attempts else 'connecting',detail='正在重新连接…' if self.attempts else '正在连接下载服务器…')
        self._request(self.info['url'])

    def _request(self,url):
        if self.stopped:return
        from update_client import trusted_url
        try:
            request=QNetworkRequest(trusted_url(url,self.hosts))
            request.setAttribute(QNetworkRequest.RedirectPolicyAttribute,QNetworkRequest.ManualRedirectPolicy)
            request.setTransferTimeout(self.stall_ms)
            request.setRawHeader(b'User-Agent',('FlickDock/'+self.version).encode('ascii'))
            request.setRawHeader(b'Accept',b'application/octet-stream');request.setRawHeader(b'Accept-Encoding',b'identity')
            if self.offset:request.setRawHeader(b'Range',f'bytes={self.offset}-'.encode('ascii'))
            self.headers=False;self._request_started=self._last=time.monotonic()
            reply=self.network.get(request);self.reply=reply;reply.setReadBufferSize(1024*1024)
            reply.metaDataChanged.connect(lambda:self._metadata(reply))
            reply.readyRead.connect(lambda:self._drain(reply))
            reply.finished.connect(lambda:self._finished(reply));self.watch.start()
        except (ValueError,OSError,RuntimeError) as error:self._fail(str(error),discard=True)

    def _live(self,reply):return not self.stopped and self.reply is reply
    def _code(self,reply):return int(reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0)

    def _metadata(self,reply):
        if not self._live(reply) or self.headers:return
        code=self._code(reply)
        if code in (0,301,302,303,307,308):return
        if code in (403,408,429,500,502,503,504):self._recover(f'下载服务器返回 HTTP {code}。');return
        if code==416 and self.offset:
            self._reset();self._recover('服务器无法继续此下载，正在重新建立连接。');return
        if code not in (200,206):self._fail(f'下载服务器返回 HTTP {code}。');return
        if bytes(reply.rawHeader('Content-Type')).lower().split(b';')[0] in (b'text/html',b'application/xhtml+xml'):
            self._fail('下载地址返回了网页或登录页面。',discard=True);return
        if bytes(reply.rawHeader('Content-Encoding')).strip().lower() not in (b'',b'identity'):
            self._fail('下载服务器使用了不支持的内容编码。',discard=True);return
        if code==206:
            match=re.fullmatch(rb'bytes (\d+)-(\d+)/(\d+)',bytes(reply.rawHeader('Content-Range')))
            if not match or tuple(map(int,match.groups()))!=(self.offset,self.total-1,self.total):
                self._fail('服务器续传范围与安装包清单不符。',discard=True);return
        elif self.offset:self._reset() # A server without Range starts a fresh full body.
        length=reply.header(QNetworkRequest.ContentLengthHeader)
        if length is not None and int(length)!=self.total-self.received:
            self._fail('更新内容大小与清单不符。',discard=True);return
        self.headers=True;self._last=time.monotonic();self._emit('downloading')

    def _reset(self):
        if self.file:self.file.seek(0);self.file.truncate(0)
        self.received=self.offset=0;self.digest=hashlib.sha256();self.prefix=b''
        self.samples.clear();self.samples.append((time.monotonic(),0));self.progress.emit(0,self.total)

    def _drain(self,reply):
        if not self._live(reply):return
        self._metadata(reply)
        if not self._live(reply) or not self.headers:return
        started=time.monotonic();budget=1024*1024
        try:
            while reply.bytesAvailable()>0 and budget>0 and self._live(reply):
                data=bytes(reply.read(min(65536,budget)))
                if not data:break
                if self.received+len(data)>self.total:raise ValueError('更新内容超过大小限制。')
                if len(self.prefix)<512:self.prefix+=data[:512-len(self.prefix)]
                if self.prefix.lstrip().lower().startswith((b'<html',b'<!doctype',b'<head',b'<body')):raise ValueError('下载地址返回了网页或登录页面。')
                self.file.write(data);self.digest.update(data);self.received+=len(data);budget-=len(data)
                self._last=time.monotonic();self.samples.append((self._last,self.received))
                if self.stopped:return
                if self._last-self._last_status>=.15:
                    self.progress.emit(self.received,self.total);self._emit('downloading')
                if time.monotonic()-started>=.004:break
            if self._live(reply) and reply.bytesAvailable()>0:QTimer.singleShot(0,lambda:self._drain(reply))
        except (OSError,ValueError) as error:self._fail(str(error),discard=isinstance(error,ValueError))

    def _finished(self,reply):
        if not self._live(reply):return
        code=self._code(reply)
        if code in (301,302,303,307,308):
            target=reply.attribute(QNetworkRequest.RedirectionTargetAttribute)
            if target is None or self.redirects>=5:self._fail('更新重定向无效或次数过多。',discard=True);return
            url=reply.url().resolved(QUrl(target)).toString();self._detach();self.redirects+=1;self._request(url);return
        self._drain(reply)
        if not self._live(reply):return
        if reply.bytesAvailable()>0:QTimer.singleShot(0,lambda:self._finished(reply));return
        if reply.error()!=QNetworkReply.NoError or self.received!=self.total:
            kind=reply.error()
            detail='TLS 证书验证失败' if kind==QNetworkReply.SslHandshakeFailedError else 'DNS 解析失败' if kind==QNetworkReply.HostNotFoundError else '系统代理连接失败' if kind in (QNetworkReply.ProxyConnectionRefusedError,QNetworkReply.ProxyConnectionClosedError,QNetworkReply.ProxyNotFoundError,QNetworkReply.ProxyTimeoutError) else '下载连接中断或内容未传完'
            if kind==QNetworkReply.SslHandshakeFailedError:self._fail(detail+'。');return
            self._recover(detail+'。');return
        self._detach();self._verify()

    def _watch(self):
        if self.stopped:return
        elapsed=time.monotonic()-(self._last if self.headers else self._request_started)
        if elapsed*1000>(self.stall_ms if self.headers else self.timeout_ms):
            self._recover('下载数据停滞超时。' if self.headers else '连接下载服务器超时。')
        elif self.phase=='downloading' and time.monotonic()-self._last_status>.5:self._emit('downloading')

    def _detach(self):
        self.watch.stop();reply,self.reply=self.reply,None
        if reply is not None:
            if not reply.isFinished():reply.abort()
            reply.deleteLater()

    def _recover(self,reason):
        self._detach()
        if self.attempts>=self.max_retries:self._fail(reason+' 已保留下载进度，可重试继续。');return
        self.attempts+=1
        self._emit('reconnecting',detail=reason+f' 正在重新连接（{self.attempts}/{self.max_retries}）…')
        # Only this dedicated update manager is reset; other file tasks continue.
        self.network.clearConnectionCache()
        self.retry.start(min(16000,1000*2**(self.attempts-1)))

    def _verify(self):
        if self.file:self.file.close();self.file=None
        self.progress.emit(self.received,self.total)
        self._emit('verifying',detail='正在校验安装包完整性及发布签名…' if self.manifest.get('publisher_signature') else '正在校验安装包完整性…')
        def verify(cancel):
            size,digest,prefix=_inspect(self.part,self.total,cancel)
            if size!=self.total or digest.hexdigest()!=self.info['sha256']:raise ValueError('安装包大小或 SHA256 校验失败，已删除损坏的下载。')
            if self.final.suffix=='.dmg':
                with self.part.open('rb') as stream:stream.seek(-512,2);valid=stream.read(4)==b'koly'
            else:valid=prefix.startswith(b'PK\x03\x04' if self.platform=='darwin' else b'MZ')
            if not valid:raise ValueError('下载内容不是有效的安装包。')
            if cancel.is_set():raise InterruptedError()
            with self.part.open('r+b') as stream:os.fsync(stream.fileno())
            if cancel.is_set():raise InterruptedError()
            with _lease_lock:
                if cancel.is_set() or not self._owns():raise InterruptedError()
                os.replace(self.part,self.final)
            return str(self.final)
        def done(path,error):
            if error:self._fail(str(error),discard=True)
            else:self.stopped=True;self._release();self.completed.emit(path)
        self._disk(verify,done)

    def _fail(self,message,*,discard=False):
        if self.stopped:return
        try:self.stop(discard=discard)
        except OSError:message+=' 更新暂存文件暂时无法清理。'
        self.failed.emit(message)

    def _owns(self):return _leases.get(self._lease_key,(None,None))[0] is self._lease

    def _release(self,*,discard=False):
        with _lease_lock:
            if not self._owns():return
            try:
                if discard:self.part.unlink(missing_ok=True)
            finally:_leases.pop(self._lease_key,None)

    def stop(self,*,discard=False,retain=False):
        self.stopped=True;self.delete_partial=discard;self.retain_lease=retain;self.retry.stop();self._detach()
        for job in self.jobs:job.cancelled.set()
        if self.file:self.file.close();self.file=None
        if not self.jobs and not retain:self._release(discard=discard)

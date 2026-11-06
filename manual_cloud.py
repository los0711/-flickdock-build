"""Finite manual R2 jobs reuse the account's verified streaming transport."""
from __future__ import annotations
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

from PySide6.QtCore import QObject,Signal
from cloud_api import CloudError
from manual_payload import descriptor,digest_file,IO_BYTES
from transfer_priority import yield_bulk_payload

REQUEST_TIMEOUT_MS=300000
WAIT_SLICE_SECONDS=.05
MAX_PART_BYTES=90*1024*1024


class ManualCloud(QObject):
    """Qt owns network objects; workers own disk work and wait without nesting Qt."""
    _dispatch=Signal(object)
    _cancel=Signal(object)

    def __init__(self,account,parent=None):
        super().__init__(parent);self.account=account;self._closed=False;self._active={}
        self._dispatch.connect(self._start);self._cancel.connect(self._abort)

    def _identity(self):
        return (self.account.device_id,self.account.api.token) if self.account.signed_in else None

    def _start(self,request):
        if self._closed or request['cancel'].is_set() or request['identity']!=self._identity():
            request.update(error=CloudError('账号或传输状态已改变。'));request['ready'].set();return
        self._active[id(request)]=request
        def finished(result,error):
            self._active.pop(id(request),None)
            if request['identity']!=self._identity():error=CloudError('账号已改变，传输已停止。')
            request.update(result=result,error=error);request['ready'].set()
        request['key']=self.account.api.request(request['method'],request['path'],finished,
            timeout_ms=REQUEST_TIMEOUT_MS,**request['options'])

    def _abort(self,request):
        if id(request) in self._active:self.account.api.cancel(request['key'])

    def call(self,method,path,cancel,**options):
        if threading.current_thread() is threading.main_thread():raise RuntimeError('Manual cloud disk jobs must run on a worker')
        identity=self._identity()
        if identity is None:raise CloudError('云端兜底需要两端登录同一账号。')
        if cancel.is_set():raise InterruptedError('已取消传输。')
        request=dict(method=method,path=path,cancel=cancel,identity=identity,options=options,ready=threading.Event())
        self._dispatch.emit(request);deadline=time.monotonic()+REQUEST_TIMEOUT_MS/1000+5
        while not request['ready'].wait(WAIT_SLICE_SECONDS):
            if cancel.is_set() or self._closed:
                self._cancel.emit(request);raise InterruptedError('已取消传输。')
            if time.monotonic()>deadline:
                self._cancel.emit(request);raise TimeoutError('云端传输超时。')
        if request.get('error'):raise request['error']
        return request['result']

    def upload(self,payload,cancel,progress):
        spec=descriptor(payload['descriptor']);item=str(uuid4());upload=str(uuid4());committed=False;failure=None
        try:
            result=self.call('POST','/v1/uploads',cancel,data=dict(upload_id=upload,item_id=item,base_version=0,
                kind='transfer',name='content.zip',mime_type='application/zip',**spec,fast_upload=True,fast_multipart=True))
            if result.get('instant'):committed=True;return result['record']
            chunk=result['chunk_size'];count=result['part_count']
            if type(chunk) is not int or not 0<chunk<=MAX_PART_BYTES or type(count) is not int or count!=(spec['size']+chunk-1)//chunk:
                raise ValueError('云端返回了无效的文件分块。')
            path=Path(payload['path']);offset=0
            from sync_files import hash_part
            for index in range(count):
                size=min(chunk,spec['size']-offset);part=hash_part(path,index,chunk,cancel)
                self.call('PUT',f'/v1/uploads/{upload}/parts/{index}',cancel,source=path,source_offset=offset,source_size=size,
                    headers={'X-Content-SHA256':part},progress=lambda done,total,base=offset:progress(base+done,spec['size']))
                offset+=size
            result=self.call('POST',f'/v1/uploads/{upload}/commit',cancel,data={});committed=True
            record=result['record']
            if record['id']!=item or record['sha256']!=spec['sha256'] or record['size']!=spec['size']:raise ValueError('云端文件确认与发送内容不一致。')
            return record
        except Exception as error:
            failure=error;raise
        finally:
            if not committed:
                # A fresh cancellation token retires the server reservation even
                # after the user cancels the byte stream. Failures remain visible
                # in the worker diagnostic; the server also expires reservations.
                try:self.call('DELETE','/v1/uploads/'+upload,threading.Event())
                except CloudError as cleanup:
                    current=(cleanup.details or {}).get('current_record')
                    if (cleanup.code=='ALREADY_COMMITTED' and isinstance(current,dict)
                            and current.get('id')==item and current.get('kind')=='transfer'
                            and current.get('sha256')==spec['sha256'] and current.get('size')==spec['size']):
                        self.retire(current)
                    elif failure is not None:raise ExceptionGroup('上传失败，临时文件清理也未完成。',[failure,cleanup])
                    else:raise

    def download(self,record,folder,cancel,progress):
        spec=descriptor({key:record[key] for key in ('size','sha256')});item=record['id'];version=record['version']
        result=self.call('GET',f'/v1/items/{item}/content',cancel,headers={'X-FlickDock-Fast-Content':'1'})
        current=result['record']
        if any(current.get(key)!=record[key] for key in ('id','version','size','sha256')) or current['kind']!='transfer':
            raise ValueError('云端文件已改变，请重新发送。')
        chunk=result['chunk_size'];count=result['part_count']
        if type(chunk) is not int or not 0<chunk<=MAX_PART_BYTES or type(count) is not int or count!=(spec['size']+chunk-1)//chunk:
            raise ValueError('云端返回了无效的文件分块。')
        folder=Path(folder);folder.mkdir(mode=0o700);target=folder/'content.zip';received=0
        with target.open('xb') as output:
            for index in range(count):
                size=min(chunk,spec['size']-received);part=folder/f'part-{index}'
                self.call('GET',f'/v1/items/{item}/parts/{index}?version={version}',cancel,destination=part,
                    headers={'X-FlickDock-Fast-Content':'1'},expected={'size':size},
                    receive_progress=lambda done,total,base=received:progress(base+done,spec['size']))
                with part.open('rb') as source:
                    while block:=source.read(IO_BYTES):
                        yield_bulk_payload(cancel)
                        if cancel.is_set():raise InterruptedError('已取消传输。')
                        output.write(block)
                part.unlink();received+=size
            output.flush();os.fsync(output.fileno())
        if received!=spec['size'] or digest_file(target,cancel)!=spec['sha256']:raise ValueError('云端接收文件完整性校验失败。')
        return target

    def retire(self,record):
        return self.call('DELETE','/v1/items/'+record['id'],threading.Event(),data={'base_version':record['version']})

    def close(self):
        self._closed=True
        for request in tuple(self._active.values()):self._abort(request)

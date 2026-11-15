"""Media results and per-source jobs, independent of the window layout."""
import base64
import hashlib
import json
import logging
from pathlib import Path
import shutil
import time
from uuid import uuid4
from PySide6.QtCore import QObject,QThreadPool,QTimer,Signal
from PySide6.QtGui import QImage
from video_jobs import VideoJob
from video_links import video_url
from transfer_workbench import reveal_file

MAX_DOWNLOADS=2
MAX_COVER_DOWNLOADS=2
FILE_RELEASE_RETRY_MS=25
FILE_RELEASE_TIMEOUT_SECONDS=5
MAX_HISTORY_ENTRIES=500
LOG=logging.getLogger(__name__)

def _read_download_history(path,cancel):
    try:
        with path.open('rb') as stream:raw=stream.read(512*1024+1)
    except FileNotFoundError:return {}
    if len(raw)>512*1024:raise ValueError('Download index exceeds limit')
    data=json.loads(raw)
    if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('entries'),dict):
        raise ValueError('Invalid download index')
    result={}
    for key,value in list(data['entries'].items())[-MAX_HISTORY_ENTRIES:]:
        if cancel.is_set():return {}
        if not isinstance(key,str) or len(key)!=64 or not isinstance(value,list) or len(value)!=3:continue
        name,size,mtime=value
        if not isinstance(name,str) or not Path(name).is_absolute() or type(size)!=int or type(mtime)!=int:continue
        path=Path(name)
        try:
            stat=path.stat()
            available=not path.is_symlink() and path.is_file() and stat.st_size==size and stat.st_mtime_ns==mtime
        except OSError:available=False
        result[key]=value+[available]
    return result

class VideoController(QObject):
    changed=Signal()
    parsed=Signal(object)
    preview_requested=Signal(object)

    def __init__(self,manager):
        super().__init__(manager.app)
        self.manager=manager;self.entries=[];self.jobs=set();self.extracting=None
        self.generation=0;self.message='支持主流视频站点、X、Facebook；其他网站也可粘贴链接尝试。'
        self.message_error=False
        self.closed=False;self.priority=False;self.cover_queue=[];self.cover_jobs=0
        self._downloaded={}
        self._history_path=Path(manager.settings.base)/'video-download-history.json'
        self._history={};self._history_ready=False;self._history_failed=False;self._pending_history_actions=[]
        self._history_pool=QThreadPool.globalInstance()
        from sync_files import DiskWork
        self._history_job=DiskWork(_read_download_history,self._history_path)
        self._history_job.signals.done.connect(self._history_loaded);self._history_pool.start(self._history_job)

    def sources(self):
        return [row for entry in self.entries for row in entry['sources']]

    def parse(self,text):
        try:url=video_url(text)
        except (ValueError,UnicodeError) as error:self.message=str(error);self.message_error=True;self.changed.emit();return
        self.generation+=1;generation=self.generation
        if self.extracting:self.extracting.cancel()
        job=VideoJob(dict(action='extract',url=url),self);self.jobs.add(job);self.extracting=job
        self.message='正在解析视频信息…';self.message_error=False;self.changed.emit()
        def complete(result):
            self.jobs.discard(job);job.deleteLater()
            if self.closed or generation!=self.generation:return
            self.extracting=None
            if result['error']:self.message=result['error'];self.message_error=True;self.changed.emit();return
            for old in self.sources():
                if old['status']=='complete' and old['path']:
                    self._downloaded[self._source_identity(old)]=(old['path'],old['received'])
            self.entries=[entry for entry in self.entries if any(row['job'] or row['status']=='queued' for row in entry['sources'])]
            added=[]
            for metadata in result['result']['entries']:
                if any((entry['id'],entry['url'])==(metadata['id'],metadata['url']) for entry in self.entries):continue
                entry=dict(metadata,key=uuid4().hex,sources=[],cover=None,cover_state='idle',cover_error='')
                for option in metadata['options']:
                    row=dict(key=uuid4().hex,entry=entry,option=dict(option),status='ready',
                        progress=None,received=0,total=None,speed=None,path=None,job=None,actions=set(),shelved=False,error='')
                    self._restore_downloaded(row)
                    entry['sources'].append(row)
                self.entries.append(entry);added.append(entry)
            self.message=f'已解析 {len(added)} 项，全部视频与音频来源已展开。'+('当前显示前 20 项。' if result['result'].get('limited') else '')
            self.changed.emit()
            for entry in added:self.request_cover(entry)
            if added:self.parsed.emit(added[0])
        job.finished.connect(complete);job.start()

    def cancel_parse(self):
        if self.extracting:self.extracting.cancel()

    def request_cover(self,entry):
        if self.closed or entry not in self.entries or entry['cover_state']!='idle':return
        if not entry.get('thumbnail_url'):
            entry.update(cover_state='unavailable',cover_error='此来源没有提供封面。');self.changed.emit();return
        entry['cover_state']='queued';self.cover_queue.append(entry);self._kick_covers()

    def _kick_covers(self):
        while not self.closed and self.cover_queue and self.cover_jobs<MAX_COVER_DOWNLOADS:
            entry=self.cover_queue.pop(0)
            if entry not in self.entries:continue
            entry['cover_state']='loading';self.cover_jobs+=1
            job=VideoJob(dict(action='thumbnail',url=entry['thumbnail_url'],page_url=entry['url']),self);self.jobs.add(job)
            job.finished.connect(lambda result,entry=entry,job=job:self._cover_complete(entry,job,result));job.start()

    def _cover_complete(self,entry,job,result):
        self.cover_jobs-=1;self.jobs.discard(job);job.deleteLater()
        if not self.closed and entry in self.entries:
            error=result['error'];image=None
            if not error:
                try:
                    encoded=result['result']['image']
                    if not isinstance(encoded,str) or len(encoded)>700000:raise ValueError('封面响应超过大小限制。')
                    image=QImage.fromData(base64.b64decode(encoded,validate=True))
                    if image.isNull() or max(image.width(),image.height())>1280:raise ValueError('封面图像无效。')
                except (ValueError,TypeError,KeyError) as problem:error=str(problem)
            entry.update(cover=image if not error else None,cover_state='error' if error else 'ready',cover_error=error)
            self.changed.emit()
        self._kick_covers()

    def action(self,row,action):
        if self.closed or row not in self.sources() or action not in ('download','shelf','folder'):return
        if action=='folder':
            try:
                if not row['path'] or row['status']!='complete':raise ValueError('下载完成后才可打开所在文件夹。')
                reveal_file(row['path'])
            except (OSError,ValueError) as error:self.message=str(error);self.message_error=True;self.changed.emit()
            return
        if not self._history_ready and row['status']=='ready':
            if not any(saved is row and saved_action==action for saved,saved_action in self._pending_history_actions):
                self._pending_history_actions.append((row,action))
            self.message='正在检查已下载的文件…';self.message_error=False;self.changed.emit();return
        if row.get('history_stale'):
            if action=='shelf':
                self.message='已下载的文件不存在或已被移动、修改，请重新下载。';self.message_error=True;self.changed.emit();return
            row['history_stale']=False;self._history.pop(self._history_key(row),None)
        if row['path'] and Path(row['path']).is_file():
            if action=='shelf':self._shelve(row)
            else:self.message='此来源已下载完成。'
            self.changed.emit();return
        if action=='shelf' and row['status']=='complete':
            self.message='已下载的文件不存在或已被移动，请重新下载。';self.message_error=True;self.changed.emit();return
        row['actions'].add(action)
        if row['job'] or row['status']=='queued':self.changed.emit();return
        row.update(status='queued',error='');self._kick()

    def _shelve(self,row):
        if row['shelved']:return
        try:
            self.manager.shelf.add_paths('permanent',[row['path']]);row['shelved']=True
        except (OSError,ValueError) as error:self.message=str(error);self.message_error=True

    @staticmethod
    def _source_identity(row):
        return (row['entry']['url'],row['entry']['id'],row['option']['id'])

    @classmethod
    def _history_key(cls,row):
        data=json.dumps(cls._source_identity(row),ensure_ascii=False,separators=(',',':')).encode('utf8')
        return hashlib.sha256(data).hexdigest()

    def _restore_downloaded(self,row):
        if row['status']!='ready' or row['job']:return
        saved=self._downloaded.get(self._source_identity(row)) or self._history.get(self._history_key(row))
        if saved:
            if len(saved)>3 and not saved[3]:row['history_stale']=True
            else:row.update(path=saved[0],status='complete',received=saved[1],total=saved[1],progress=1.,history_stale=False)

    def _history_loaded(self,result,error):
        if self.closed:return
        self._history_ready=True
        if error:
            # Preserve the original index for diagnosis rather than replacing
            # it with an empty one on a read or validation failure.
            self._history_failed=True;LOG.warning('Video download index could not be read: %s',type(error).__name__)
        else:self._history=result
        for row in self.sources():self._restore_downloaded(row)
        pending,self._pending_history_actions=self._pending_history_actions,[]
        self.changed.emit()
        for row,action in pending:self.action(row,action)

    def _remember_download(self,row,path,stat):
        self._downloaded[self._source_identity(row)]=(str(path),stat.st_size)
        self._history[self._history_key(row)]=[str(path),stat.st_size,stat.st_mtime_ns,True]
        while len(self._history)>MAX_HISTORY_ENTRIES:self._history.pop(next(iter(self._history)))
        if self._history_ready and not self._history_failed:
            from account_store import atomic_json
            from shelf_journal import coordinator
            coordinator(self.manager.settings.base,atomic_json).submit(self._history_path,
                dict(version=1,entries={key:value[:3] for key,value in self._history.items()}))

    def _kick(self):
        if self.closed:return
        count=sum(row['job'] is not None for row in self.sources())
        for row in self.sources():
            if count>=MAX_DOWNLOADS:break
            if row['status']!='queued':continue
            try:self._start_download(row)
            except OSError as error:row.update(status='error',error=str(error));continue
            count+=1
        self.changed.emit()

    def _start_download(self,row):
        token=uuid4().hex;folder=Path(self.manager.settings.video_download_dir).expanduser().absolute()
        folder.mkdir(parents=True,exist_ok=True)
        request=dict(action='download',url=row['entry']['url'],id=row['entry']['id'],option=row['option']['id'],folder=str(folder),token=token)
        job=VideoJob(request,self);self.jobs.add(job);row.update(job=job,status='downloading',progress=None,received=0,total=None,speed=None)
        def progress(value):
            if row['job'] is not job or value['type']!='progress':return
            total=value.get('total');received=value.get('bytes',0)
            row.update(status=value.get('phase','downloading'),received=received,total=total,speed=value.get('speed'),progress=min(1.,received/total) if total else None)
            self.changed.emit()
        def complete(result):
            stage=folder/('.flickdock-download-'+token);deadline=time.monotonic()+FILE_RELEASE_TIMEOUT_SECONDS
            def cleanup():
                try:
                    if stage.is_dir() and not stage.is_symlink() and not (hasattr(stage,'is_junction') and stage.is_junction()) and stage.resolve().parent==folder.resolve():shutil.rmtree(stage)
                except OSError as error:
                    # Windows can retain a terminated process's file handles
                    # briefly after its exit notification. Retry only that case.
                    if getattr(error,'winerror',None)==32 and time.monotonic()<deadline:
                        QTimer.singleShot(FILE_RELEASE_RETRY_MS,self,cleanup);return
                    result.update(error='下载临时文件清理失败：'+str(error),cancelled=False)
                settled(result)
            cleanup()
        def settled(result):
            self.jobs.discard(job);job.deleteLater();row['job']=None
            if self.closed:return
            if result['error']:row.update(status='cancelled' if result['cancelled'] and not job.error else 'error',error=result['error'])
            else:
                path=Path(result['result']['path'])
                if not path.is_file() or path.is_symlink() or path.resolve().parent!=folder.resolve():row.update(status='error',error='下载文件位置或内容无效。')
                else:
                    stat=path.stat()
                    row.update(status='complete',path=str(path),progress=1.,received=stat.st_size,total=stat.st_size)
                    self._remember_download(row,path,stat)
                    actions=set(row['actions']);row['actions'].clear()
                    if 'shelf' in actions:self._shelve(row)
            self._kick()
        job.changed.connect(progress);job.finished.connect(complete);job.start();job.set_priority(self.priority)

    def cancel(self,row):
        row['actions'].clear()
        self._pending_history_actions=[(saved,action) for saved,action in self._pending_history_actions if saved is not row]
        if row['job']:row['job'].cancel()
        elif row['status']=='queued':row['status']='cancelled';self.changed.emit()

    def set_priority(self,active):
        self.priority=bool(active)
        for job in self.jobs:
            if job.request['action']=='download':job.set_priority(active)

    def close(self):
        self.closed=True;self.generation+=1;self.cover_queue.clear()
        self._history_job.cancel.set();self._pending_history_actions.clear()
        for job in list(self.jobs):job.cancel()
        for job in list(self.jobs):job.process.waitForFinished(1000)

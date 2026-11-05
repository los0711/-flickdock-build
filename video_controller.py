"""Media results and per-source jobs, independent of the window layout."""
import base64
from pathlib import Path
import shutil
from uuid import uuid4
from PySide6.QtCore import QObject,Signal
from PySide6.QtGui import QImage
from video_jobs import VideoJob
from video_links import video_url
from transfer_workbench import reveal_file

MAX_DOWNLOADS=2
MAX_COVER_DOWNLOADS=2

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
            self.entries=[entry for entry in self.entries if any(row['job'] or row['status']=='queued' for row in entry['sources'])]
            added=[]
            for metadata in result['result']['entries']:
                if any((entry['id'],entry['url'])==(metadata['id'],metadata['url']) for entry in self.entries):continue
                entry=dict(metadata,key=uuid4().hex,sources=[],cover=None,cover_state='idle',cover_error='')
                for option in metadata['options']:
                    entry['sources'].append(dict(key=uuid4().hex,entry=entry,option=dict(option),status='ready',
                        progress=None,received=0,total=None,speed=None,path=None,job=None,actions=set(),shelved=False,error=''))
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
        if row['path'] and Path(row['path']).is_file():
            if action=='shelf':self._shelve(row)
            else:self.message='此来源已下载完成。'
            self.changed.emit();return
        row['actions'].add(action)
        if row['job'] or row['status']=='queued':self.changed.emit();return
        row.update(status='queued',error='');self._kick()

    def _shelve(self,row):
        if row['shelved']:return
        from shelf_intake_limit import MAX_SHELF_BYTES
        try:
            if Path(row['path']).stat().st_size>MAX_SHELF_BYTES:raise ValueError('文件已下载；超过搁架 1 GB 上限，可在文件互传中发送。')
            self.manager.shelf.add_paths('permanent',[row['path']]);row['shelved']=True
        except (OSError,ValueError) as error:self.message=str(error);self.message_error=True

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
            self.jobs.discard(job);job.deleteLater();stage=folder/('.flickdock-download-'+token)
            if stage.is_dir() and not stage.is_symlink() and not (hasattr(stage,'is_junction') and stage.is_junction()) and stage.resolve().parent==folder.resolve():shutil.rmtree(stage)
            row['job']=None
            if self.closed:return
            if result['error']:row.update(status='cancelled' if result['cancelled'] and not job.error else 'error',error=result['error'])
            else:
                path=Path(result['result']['path'])
                if not path.is_file() or path.is_symlink() or path.resolve().parent!=folder.resolve():row.update(status='error',error='下载文件位置或内容无效。')
                else:
                    row.update(status='complete',path=str(path),progress=1.,received=path.stat().st_size,total=path.stat().st_size)
                    actions=set(row['actions']);row['actions'].clear()
                    if 'shelf' in actions:self._shelve(row)
            self._kick()
        job.changed.connect(progress);job.finished.connect(complete);job.start();job.set_priority(self.priority)

    def cancel(self,row):
        row['actions'].clear()
        if row['job']:row['job'].cancel()
        elif row['status']=='queued':row['status']='cancelled';self.changed.emit()

    def set_priority(self,active):
        self.priority=bool(active)
        for job in self.jobs:
            if job.request['action']=='download':job.set_priority(active)

    def close(self):
        self.closed=True;self.generation+=1;self.cover_queue.clear()
        for job in list(self.jobs):job.cancel()
        for job in list(self.jobs):job.process.waitForFinished(1000)

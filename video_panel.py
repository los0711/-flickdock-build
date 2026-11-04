"""Video parsing state, download actions and the existing Qt modal surface."""
from pathlib import Path
import shutil
from uuid import uuid4

from PySide6.QtCore import QObject,QTimer,QUrl,Qt,Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame,QHBoxLayout,QLabel,QLineEdit,QProgressBar,QVBoxLayout
from app_settings import ReferenceModalDialog,SettingsCloseButton,SettingsComboBox,SettingsToggleSwitch
from native_ui_common import colors
from reference_widgets import button,refresh_tree
from video_jobs import VideoJob
from video_links import video_url

MAX_DOWNLOADS=2


class VideoController(QObject):
    changed=Signal()
    def __init__(self,manager):
        super().__init__(manager.app);self.manager=manager;self.entries=[];self.jobs=set();self.extracting=None
        self.generation=0;self.message='支持主流视频站点、X、Facebook；其他网站也可粘贴链接尝试。';self.closed=False;self.priority=False

    def parse(self,text):
        try:url=video_url(text)
        except (ValueError,UnicodeError) as error:self.message=str(error);self.changed.emit();return
        self.generation+=1;generation=self.generation
        if self.extracting:self.extracting.cancel()
        job=VideoJob(dict(action='extract',url=url),self);self.jobs.add(job);self.extracting=job
        self.message='正在解析视频信息…';self.changed.emit()
        def complete(result):
            self.jobs.discard(job);job.deleteLater()
            if self.closed or generation!=self.generation:return
            self.extracting=None
            if result['error']:self.message=result['error']
            else:
                self.entries=[row for row in self.entries if row.get('job') or row['status']=='queued']
                for entry in result['result']['entries']:
                    if any((row['id'],row['url'])==(entry['id'],entry['url']) for row in self.entries):continue
                    self.entries.append(dict(entry,key=uuid4().hex,choice=entry['options'][0]['id'],status='ready',progress=None,
                        received=0,total=None,speed=None,path=None,job=None,actions=set(),error=''))
                self.message=f'已解析 {len(result["result"]["entries"])} 项，选择画质后下载。'+('当前显示前 20 项。' if result['result'].get('limited') else '')
            self.changed.emit()
        job.finished.connect(complete);job.start()

    def cancel_parse(self):
        if self.extracting:self.extracting.cancel()

    def action(self,row,action):
        if self.closed or row not in self.entries or action not in ('download','transfer','shelf'):return
        path=row.get('path')
        if path and Path(path).is_file():
            if action=='download':QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            else:self._deliver(path,{action});self.changed.emit()
            return
        row['actions'].add(action)
        if row.get('job') or row['status']=='queued':self.changed.emit();return
        row.update(status='queued',error='');self._kick()

    def _deliver(self,path,actions):
        if 'shelf' in actions:
            from shelf_intake_limit import MAX_SHELF_BYTES
            if Path(path).stat().st_size>MAX_SHELF_BYTES:self.message='文件已下载；超过搁架 1 GB 上限，可使用传输发送。'
            else:self.manager.shelf.add_paths('permanent',[path])
        if 'transfer' in actions:self.manager.queue_transfer_paths('lan',[path])

    def _kick(self):
        if self.closed:return
        count=sum(row.get('job') is not None for row in self.entries)
        for row in self.entries:
            if count>=MAX_DOWNLOADS:break
            if row['status']!='queued':continue
            self._start_download(row);count+=1
        self.changed.emit()

    def _start_download(self,row):
        token=uuid4().hex;folder=Path(self.manager.settings.video_download_dir).expanduser().absolute()
        request=dict(action='download',url=row['url'],id=row['id'],option=row['choice'],folder=str(folder),token=token)
        job=VideoJob(request,self);self.jobs.add(job);row.update(job=job,status='downloading',progress=None,received=0,total=None,speed=None)
        def progress(value):
            if row.get('job') is not job or value['type']!='progress':return
            total=value.get('total');received=value.get('bytes',0)
            row.update(status=value.get('phase','downloading'),received=received,total=total,speed=value.get('speed'),
                progress=min(1.,received/total) if total else None)
            self.changed.emit()
        def complete(result):
            self.jobs.discard(job);job.deleteLater()
            stage=folder/('.flickdock-download-'+token)
            if stage.is_dir() and not stage.is_symlink() and not (hasattr(stage,'is_junction') and stage.is_junction()) and stage.resolve().parent==folder.resolve():
                shutil.rmtree(stage)
            row['job']=None
            if self.closed:return
            if result['error']:row.update(status='cancelled' if result['cancelled'] and not job.error else 'error',error=result['error'])
            else:
                path=Path(result['result']['path'])
                if not path.is_file() or path.is_symlink() or path.resolve().parent!=folder.resolve():
                    row.update(status='error',error='下载文件位置或内容无效。')
                else:
                    row.update(status='complete',path=str(path),progress=1.,received=path.stat().st_size,total=path.stat().st_size)
                    actions=set(row['actions']);row['actions'].clear();self._deliver(str(path),actions)
            self._kick()
        job.changed.connect(progress);job.finished.connect(complete);job.start();job.set_priority(self.priority)

    def cancel(self,row):
        row['actions'].clear()
        if row.get('job'):row['job'].cancel()
        elif row['status']=='queued':row['status']='cancelled';self.changed.emit()

    def set_priority(self,active):
        self.priority=bool(active)
        for job in self.jobs:
            if job.request['action']=='download':job.set_priority(active)

    def close(self):
        self.closed=True;self.generation+=1
        for job in list(self.jobs):job.cancel()
        for job in list(self.jobs):job.process.waitForFinished(1000)


class VideoCard(QFrame):
    def __init__(self,row,controller,parent):
        super().__init__(parent);self.row=row;self.controller=controller;self.setObjectName('videoResult')
        layout=QVBoxLayout(self);layout.setContentsMargins(14,12,14,12);layout.setSpacing(8)
        self.title=QLabel(row['title'],self);self.title.setTextFormat(Qt.PlainText);self.title.setWordWrap(True);self.title.setObjectName('videoTitle');layout.addWidget(self.title)
        self.meta=QLabel(self);self.meta.setObjectName('videoMeta');self.meta.setTextFormat(Qt.PlainText);layout.addWidget(self.meta)
        actions=QHBoxLayout();actions.setSpacing(6);self.choice=SettingsComboBox(self)
        for option in row['options']:self.choice.addItem(option['label'],option['id'])
        self.choice.currentIndexChanged.connect(self._select);actions.addWidget(self.choice,1)
        self.choice.setAccessibleName('下载画质与音质')
        self.download=button('下载','download','secondary',parent=self);self.transfer=button('','send',parent=self);self.shelf=button('','plus',parent=self)
        self.transfer.setAccessibleName('下载并传输');self.transfer.setToolTip('传输文件');self.shelf.setAccessibleName('下载并加入搁架');self.shelf.setToolTip('加入搁架')
        for control,action in ((self.download,'download'),(self.transfer,'transfer'),(self.shelf,'shelf')):
            control.clicked.connect(lambda checked=False,action=action:controller.action(row,action));actions.addWidget(control)
        self.cancel_button=button('','close',parent=self);self.cancel_button.setAccessibleName('取消下载');self.cancel_button.clicked.connect(lambda:controller.cancel(row));actions.addWidget(self.cancel_button)
        layout.addLayout(actions)
        self.progress=QProgressBar(self);self.progress.setFixedHeight(4);self.progress.setTextVisible(False);layout.addWidget(self.progress)
        self.status=QLabel(self);self.status.setObjectName('videoMeta');self.status.setTextFormat(Qt.PlainText);self.status.setWordWrap(True);layout.addWidget(self.status)
        self.refresh()

    def _select(self):self.row['choice']=self.choice.currentData();self.row['path']=None

    def refresh(self):
        row=self.row;duration=row.get('duration');minutes=f'{int(duration)//60}:{int(duration)%60:02}' if duration is not None else ''
        self.meta.setText(' · '.join(part for part in (row.get('uploader',''),minutes) if part))
        busy=row.get('job') is not None or row['status']=='queued';self.choice.setEnabled(not busy)
        self.cancel_button.setVisible(busy);self.progress.setVisible(busy)
        self.download.setText('打开' if row['status']=='complete' and row.get('path') else '下载')
        self.download.setEnabled(not busy)
        if row['progress'] is None:
            from native_ui_common import reduced_motion
            if reduced_motion():self.progress.hide()
            else:self.progress.setRange(0,0)
        else:self.progress.setRange(0,1000);self.progress.setValue(round(row['progress']*1000))
        labels={'ready':'选择下载、传输或加入搁架。','queued':'等待下载…','downloading':'正在下载…','processing':'正在整理音视频…','complete':'下载完成','cancelled':'已取消','error':row.get('error','下载失败。')}
        text=labels.get(row['status'],'')
        if row['status']=='downloading':
            if row['progress'] is not None:text=f'{row["progress"]:.0%}'
            if row.get('speed'):text+=f' · {row["speed"]/1024**2:.1f} MB/s'
        self.status.setText(text);self.status.setToolTip(row.get('path') or '')


class VideoDialog(ReferenceModalDialog):
    def __init__(self,manager,controller):
        super().__init__(manager.shelf,surface_width=680,modal=False);self.manager=manager;self.controller=controller;self.cards={}
        self.setWindowTitle('FlickDock · 视频解析');self.setWindowFlag(Qt.WindowStaysOnTopHint,True)
        head=QHBoxLayout();title=QLabel('视频解析',self.body);title.setObjectName('videoHeading');head.addWidget(title,1)
        close=SettingsCloseButton(self.body,glyph_size=14);close.setFixedSize(28,28);close.setAccessibleName('关闭视频解析');close.setToolTip('关闭视频解析');close.clicked.connect(self.close);head.addWidget(close);self.close_button=close
        self.body_layout.addLayout(head)
        description=QLabel('粘贴网页链接，解析视频与音频。',self.body);description.setObjectName('videoMeta');self.body_layout.addWidget(description)
        input_row=QHBoxLayout();input_row.setSpacing(8);self.url=QLineEdit(self.body);self.url.setPlaceholderText('粘贴视频网页链接…');self.url.setAccessibleName('视频网页链接');self.url.setMaxLength(8192)
        input_row.addWidget(self.url,1);self.parse_button=button('解析',variant='primary',parent=self.body);self.parse_button.clicked.connect(self._parse);input_row.addWidget(self.parse_button)
        self.stop_button=button('取消',parent=self.body);self.stop_button.clicked.connect(controller.cancel_parse);input_row.addWidget(self.stop_button);self.body_layout.addLayout(input_row);self.url.returnPressed.connect(self._parse)
        prompt_row=QHBoxLayout();self.prompt=SettingsToggleSwitch('复制视频链接时提示解析',self.body);prompt_row.addWidget(self.prompt);prompt_row.addStretch();self.body_layout.addLayout(prompt_row)
        self.prompt.toggled.connect(self._prompt_changed)
        self.message=QLabel(self.body);self.message.setObjectName('videoMeta');self.message.setTextFormat(Qt.PlainText);self.message.setWordWrap(True);self.body_layout.addWidget(self.message)
        self.results=QVBoxLayout();self.results.setSpacing(10);self.body_layout.addLayout(self.results)
        folder_row=QHBoxLayout();folder_row.setSpacing(8);self.folder=QLabel(self.body);self.folder.setObjectName('videoMeta');self.folder.setTextFormat(Qt.PlainText);self.folder.setWordWrap(True);folder_row.addWidget(self.folder,1)
        change=button('更改位置',parent=self.body);change.clicked.connect(self._choose_folder);folder_row.addWidget(change)
        open_folder=button('打开目录','folder',parent=self.body);open_folder.clicked.connect(self._open_folder);folder_row.addWidget(open_folder);self.body_layout.addLayout(folder_row)
        controller.changed.connect(self.refresh);manager.settings.changed.connect(self.refresh);self.refresh()

    def _parse(self):self.controller.parse(self.url.text())
    def _prompt_changed(self,value):
        try:self.manager.settings.update(video_link_prompt=value)
        except (ValueError,OSError) as error:self.controller.message=str(error);self.refresh()
    def _choose_folder(self):
        from PySide6.QtWidgets import QFileDialog
        folder=QFileDialog.getExistingDirectory(self,'选择视频保存位置',self.manager.settings.video_download_dir)
        if folder:
            try:self.manager.settings.update(video_download_dir=folder)
            except (ValueError,OSError) as error:self.controller.message=str(error);self.refresh()
    def _open_folder(self):
        path=Path(self.manager.settings.video_download_dir)
        if path.is_dir():QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:self.controller.message='保存目录尚未创建，首次下载时会创建。';self.refresh()

    def refresh(self):
        self.set_surface_theme(self.manager.settings.theme);dark=self.manager.settings.theme=='dark';self.close_button.dark=dark;self.prompt.dark=dark
        c=colors(self.manager.settings.theme)
        self.setStyleSheet(f'QLabel{{color:{c["text"]};background:transparent;font-size:12px;}} QLabel#videoHeading{{font-size:18px;font-weight:600;}} QLabel#videoTitle{{font-size:13px;font-weight:600;}} QLabel#videoMeta{{font-size:11px;color:{c["secondary"]};}} QFrame#videoResult{{background:{c["card"]};border:0;border-radius:12px;}} QLineEdit{{background:{c["control"]};color:{c["text"]};border:0;border-radius:8px;padding:9px;}} QProgressBar{{background:{c["control"]};border:0;border-radius:2px;}} QProgressBar::chunk{{background:{c["link"]};border-radius:2px;}}')
        self.setStyleSheet(self.styleSheet()+f'QComboBox{{background:{c["control"]};color:{c["text"]};border:0;border-radius:8px;padding:5px 30px 5px 10px;min-height:22px;}} QComboBox:disabled{{color:{c["muted"]};}} QComboBox:focus{{border:1px solid {c["link"]};}} QComboBox::drop-down{{border:0;width:28px;background:transparent;}} QComboBox::down-arrow{{image:none;}} QPushButton#settingsClose{{background:transparent;border:0;border-radius:8px;padding:0;}} QPushButton#settingsClose:hover{{background:{c["hover"]};}} QPushButton#settingsClose:pressed{{background:{c["selected"]};}}')
        blocked=self.prompt.blockSignals(True);self.prompt.setChecked(self.manager.settings.video_link_prompt);self.prompt.blockSignals(blocked)
        self.folder.setText('保存位置：'+self.manager.settings.video_download_dir);self.message.setText(self.controller.message)
        busy=self.controller.extracting is not None;self.parse_button.setEnabled(not busy);self.parse_button.setText('解析中…' if busy else '解析');self.stop_button.setVisible(busy)
        keys={row['key'] for row in self.controller.entries}
        for key in list(self.cards):
            if key not in keys:card=self.cards.pop(key);self.results.removeWidget(card);card.hide();card.deleteLater()
        for row in self.controller.entries:
            if row['key'] not in self.cards:
                card=VideoCard(row,self.controller,self.body);self.cards[row['key']]=card;self.results.addWidget(card)
            card=self.cards[row['key']];card.choice.dark=dark;card.refresh()
        refresh_tree(self)
        self.adjustSize()

"""An expiring opt-in prompt driven by the shared clipboard observation."""
from urllib.parse import urlsplit
from PySide6.QtCore import QObject,QPoint,QThreadPool,QTimer,Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout,QLabel
from app_settings import ReferenceModalDialog
from reference_widgets import button
from native_ui_common import colors
from sync_files import DiskWork
from video_links import LinkPromptHistory,candidate_link,recognized_video
from flickdock_icons import app_icon

PROMPT_DURATION_MS=5000
PROMPT_LOGO_SIZE_PX=24


class VideoLinkPrompt(ReferenceModalDialog):
    def __init__(self,manager):
        super().__init__(manager.shelf,surface_width=350,modal=False);self.manager=manager;self.url=''
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle('视频链接解析')
        header=QHBoxLayout();self.logo=QLabel(self.body);self.logo.setFixedSize(PROMPT_LOGO_SIZE_PX,PROMPT_LOGO_SIZE_PX)
        self.logo.setPixmap(app_icon().pixmap(PROMPT_LOGO_SIZE_PX,PROMPT_LOGO_SIZE_PX));self.logo.setAccessibleName('FlickDock')
        header.addWidget(self.logo);header.addStretch();self.body_layout.addLayout(header)
        self.heading=QLabel('检测到视频链接，是否解析？',self.body);self.heading.setTextFormat(Qt.PlainText);self.body_layout.addWidget(self.heading)
        self.site=QLabel(self.body);self.site.setTextFormat(Qt.PlainText);self.site.setWordWrap(True);self.body_layout.addWidget(self.site)
        actions=QHBoxLayout();actions.addStretch()
        cancel=button('取消',parent=self.body);cancel.clicked.connect(self.close);actions.addWidget(cancel)
        parse=button('去解析','video',parent=self.body);parse.clicked.connect(self._accept);actions.addWidget(parse);self.body_layout.addLayout(actions)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.close)

    def present(self,url):
        self.url=url;parts=urlsplit(url);self.site.setText((parts.hostname or '')+'\n'+parts.path[:90])
        c=colors(self.manager.settings.theme);self.set_surface_theme(self.manager.settings.theme)
        self.setStyleSheet(f'QLabel{{background:transparent;color:{c["text"]};font-size:12px;}}')
        self.adjustSize();screen=QGuiApplication.screenAt(self.manager.shelf.frameGeometry().center()) or QGuiApplication.primaryScreen()
        if screen:
            area=screen.availableGeometry();self.move(area.right()-self.width()-14,area.bottom()-self.height()-14)
        self.show();self.timer.start(PROMPT_DURATION_MS)

    def _accept(self):
        url=self.url;self.close();self.manager.show_video_dialog(url)

    def hideEvent(self,event):self.timer.stop();super().hideEvent(event)


class VideoLinkWatcher(QObject):
    def __init__(self,manager):
        super().__init__(manager.app);self.manager=manager;self.history=LinkPromptHistory();self.prompt=None
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1);self.jobs=set();self.generation=0;self.closed=False
        self.revision=manager.clipboard_sync.view.get('revision')
        manager.clipboard_sync.view_changed.connect(self._observe);manager.settings.changed.connect(self._settings)

    def _settings(self):
        if not self.manager.settings.video_link_prompt:
            self.generation+=1
            for job in self.jobs:job.cancel.set()
            if self.prompt:self.prompt.close()

    def _observe(self,state):
        revision=state.get('revision')
        if revision==self.revision:return
        self.revision=revision;self.generation+=1;generation=self.generation
        for job in self.jobs:job.cancel.set()
        value=state.get('value')
        if self.closed or not self.manager.settings.video_link_prompt or not value or value[0]!='text':return
        url=candidate_link(value[1]['text'])
        if url is None:return
        job=DiskWork(recognized_video,url);self.jobs.add(job)
        def ready(result,error):
            self.jobs.discard(job)
            if self.closed or generation!=self.generation or not self.manager.settings.video_link_prompt:return
            if error is not None:
                import logging
                logging.getLogger(__name__).warning('Video URL recognition failed: %s',type(error).__name__);return
            if result and self.history.claim(url):
                if self.prompt is None:self.prompt=VideoLinkPrompt(self.manager)
                self.prompt.present(url)
        job.signals.done.connect(ready);self.pool.start(job,-1)

    def close(self):
        self.closed=True;self.generation+=1
        for job in self.jobs:job.cancel.set()
        if self.prompt:self.prompt.close()

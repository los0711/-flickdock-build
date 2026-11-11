"""One reusable capsule, renewed by repeated copies of the same video link."""
import logging
from PySide6.QtCore import QObject,QRectF,QThreadPool,Qt
from PySide6.QtGui import QColor,QGuiApplication
from intake_notice import CapsuleNotice,RING_VISIBLE_SIZE_PX
from native_ui_common import colors,icon
from flickdock_icons import draw_glyph
from sync_files import DiskWork
from video_links import candidate_link,recognized_video

PROMPT_DURATION_MS=5000
PROMPT_HOST_WIDTH_PX=320
PROMPT_MAX_WIDTH_PX=296
PROMPT_ACTION_COLOR='#087CEF'
LOG=logging.getLogger(__name__)


class VideoLinkPrompt(CapsuleNotice):
    HOST_WIDTH=PROMPT_HOST_WIDTH_PX
    MAX_CAPSULE_WIDTH=PROMPT_MAX_WIDTH_PX
    NOTICE_ORDER=2

    def __init__(self,manager):
        super().__init__(manager);self.url='';self.setWindowTitle('FlickDock · 视频链接解析')
        self._phase='link';self._text='检测到视频链接，是否解析'
        self._description='点击前往视频解析'

    def _sync_accessibility(self):
        self.setAccessibleName(self._text);self.setAccessibleDescription(self._description)

    def present(self,url):
        self.url=url
        if not self.isVisible() or self._hiding:
            screen=QGuiApplication.screenAt(self.manager.shelf.frameGeometry().center()) or QGuiApplication.primaryScreen()
            self._begin_presentation(screen.availableGeometry())
        self.timer.start(PROMPT_DURATION_MS)

    def _paint_indicator(self,painter,rect,colors):
        circle=QRectF(0,0,RING_VISIBLE_SIZE_PX,RING_VISIBLE_SIZE_PX);circle.moveCenter(rect.center())
        painter.save();painter.setPen(Qt.NoPen);painter.setBrush(QColor(PROMPT_ACTION_COLOR));painter.drawEllipse(circle)
        draw_glyph(painter,'arrow',circle.adjusted(4,4,-4,-4),'#FFFFFF',1.8);painter.restore()

    def _paint_leading(self,painter,rect):
        color=QColor(PROMPT_ACTION_COLOR);color.setAlpha(35)
        painter.save();painter.setPen(Qt.NoPen);painter.setBrush(color);painter.drawEllipse(rect)
        icon('video',colors(self.manager.settings.theme)['link']).paint(painter,rect.adjusted(3,3,-3,-3).toAlignedRect());painter.restore()

    def _activate(self):self._accept()

    def _accept(self):
        url=self.url;self.close();self.manager.show_video_dialog(url)


class VideoLinkWatcher(QObject):
    def __init__(self,manager):
        super().__init__(manager.app);self.manager=manager;self.prompt=None
        self.pool=QThreadPool(self);self.pool.setMaxThreadCount(1);self.jobs=set();self.generation=0;self.closed=False
        self.known_url=None;self.pending_url=None
        self.revision=manager.clipboard_sync.view.get('revision')
        manager.clipboard_sync.view_changed.connect(self._observe)
        manager.clipboard_sync.copy_observed.connect(self._consider)
        manager.settings.changed.connect(self._settings)

    def _settings(self):
        if not self.manager.settings.video_link_prompt:
            self.generation+=1;self.pending_url=None
            for job in self.jobs:job.cancel.set()
            if self.prompt:self.prompt.close()

    def _observe(self,state):
        revision=state.get('revision')
        if revision==self.revision:return
        self.revision=revision;self._consider(state.get('value'))

    def _present(self,url):
        if self.prompt is None:self.prompt=VideoLinkPrompt(self.manager)
        self.prompt.present(url)

    def _consider(self,value):
        if self.closed or not self.manager.settings.video_link_prompt:return
        url=candidate_link(value[1]['text']) if value and value[0]=='text' else None
        if url is not None and url==self.known_url:
            if self.pending_url is not None:
                self.generation+=1;self.pending_url=None
                for job in self.jobs:job.cancel.set()
            self._present(url);return
        if url is not None and url==self.pending_url:return
        self.generation+=1;generation=self.generation;self.pending_url=url
        for job in self.jobs:job.cancel.set()
        if url is None:return
        job=DiskWork(recognized_video,url);self.jobs.add(job)
        def ready(result,error):
            self.jobs.discard(job)
            if self.closed or generation!=self.generation or not self.manager.settings.video_link_prompt:return
            self.pending_url=None
            if error is not None:
                LOG.warning('Video URL recognition failed: %s',type(error).__name__);return
            if result:self.known_url=url;self._present(url)
        job.signals.done.connect(ready);self.pool.start(job,-1)

    def close(self):
        self.closed=True;self.generation+=1;self.pending_url=None
        for job in self.jobs:job.cancel.set()
        if self.prompt:self.prompt.close()

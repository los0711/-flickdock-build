"""Movable media window, expanded source matrix and an owned cover/player dialog."""
from pathlib import Path
import sys
from PySide6.QtCore import QRect,QSize,Qt
from PySide6.QtGui import QPainter,QColor
from PySide6.QtWidgets import QCheckBox,QFrame,QGridLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,QVBoxLayout,QWidget
from app_settings import ReferenceModalDialog,SettingsCloseButton
from native_ui_common import colors
from reference_widgets import button,refresh_tree,icon
from image_preview import ImagePreview
from media_preview import MediaPreview
from window_chrome import WindowChrome

VIDEO_WINDOW_SIZE=QSize(980,740)
VIDEO_WINDOW_MIN_SIZE=QSize(600,420)
SOURCE_GRID_BREAKPOINT_PX=580
SOURCE_CARD_SPACING_PX=10
MAC_WINDOW_CONTROLS=(('close','#FF5F56','关闭视频解析'),('min','#FFBD2E','最小化'),('max','#27C93F','最大化或还原'))

def plain_label(text,parent,name='videoMeta'):
    label=QLabel(text,parent);label.setObjectName(name);label.setTextFormat(Qt.PlainText)
    return label

class CoverButton(QPushButton):
    def __init__(self,entry,parent):
        super().__init__('',parent);self.entry=entry;self.dark=True
        self.setFixedSize(140,86);self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName('查看视频封面预览');self.setToolTip('查看封面，或播放已下载的视频')
    def paintEvent(self,event):
        from PySide6.QtGui import QPainterPath
        c=colors('dark' if self.dark else 'light');p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        path=QPainterPath();path.addRoundedRect(self.rect(),9,9);p.setClipPath(path);p.fillRect(self.rect(),QColor(c['control']))
        image=self.entry['cover']
        if image is not None:
            crop=image.size().scaled(self.size(),Qt.KeepAspectRatioByExpanding)
            target=QRect((self.width()-crop.width())//2,(self.height()-crop.height())//2,crop.width(),crop.height())
            p.setRenderHint(QPainter.SmoothPixmapTransform);p.drawImage(target,image)
        else:
            p.setPen(QColor(c['secondary']));p.drawText(self.rect(),Qt.AlignCenter,'封面加载中…' if self.entry['cover_state'] in ('idle','queued','loading') else '暂无封面')
        p.end()

class VideoSourceCard(QFrame):
    def __init__(self,row,controller,parent):
        super().__init__(parent);self.row=row;self.controller=controller;self.setObjectName('videoSourceCard')
        self.setMinimumWidth(220)
        layout=QVBoxLayout(self);layout.setContentsMargins(13,11,13,9);layout.setSpacing(8)
        head=QHBoxLayout();head.setSpacing(10);self.kind=QLabel(self);self.kind.setFixedSize(28,28);head.addWidget(self.kind)
        text=QVBoxLayout();text.setSpacing(1);self.title=plain_label(row['option']['label'],self,'videoSourceTitle');text.addWidget(self.title)
        self.details=plain_label(row['option']['details'],self);text.addWidget(self.details);head.addLayout(text,1);layout.addLayout(head)
        foot=QHBoxLayout();self.size=plain_label('',self);foot.addWidget(self.size,1)
        self.download=button('','download',parent=self);self.shelf=button('','shelf',parent=self);self.folder=button('','folder',parent=self)
        for control,action,name in ((self.download,'download','下载'),(self.shelf,'shelf','下载后放入搁架'),(self.folder,'folder','打开所在文件夹')):
            control.setFixedSize(30,30);control.setAccessibleName(name);control.setToolTip(name)
            control.clicked.connect(lambda checked=False,action=action:self._invoke(action));foot.addWidget(control)
        layout.addLayout(foot)
        self.status=plain_label('',self);self.status.setWordWrap(True);layout.addWidget(self.status)
        from PySide6.QtWidgets import QProgressBar
        self.progress=QProgressBar(self);self.progress.setFixedHeight(3);self.progress.setTextVisible(False);layout.addWidget(self.progress)
        self._dark=None;self.refresh('dark')

    def _invoke(self,action):
        if action=='download' and (self.row['job'] or self.row['status']=='queued'):self.controller.cancel(self.row)
        else:self.controller.action(self.row,action)

    def mouseDoubleClickEvent(self,event):
        if event.button()==Qt.LeftButton and self.row['path'] and self.row['status']=='complete':
            self.controller.preview_requested.emit(self.row);event.accept();return
        super().mouseDoubleClickEvent(event)

    def refresh(self,theme):
        row=self.row;option=row['option'];busy=row['job'] is not None or row['status']=='queued';complete=row['status']=='complete' and bool(row['path'])
        if self._dark!=(theme=='dark'):
            self._dark=theme=='dark';c=colors(theme);self.kind.setPixmap(icon('audio' if option['kind']=='audio' else 'video',c['secondary']).pixmap(18,18));refresh_tree(self)
        if self.property('downloadComplete')!=bool(complete):
            self.setProperty('downloadComplete',bool(complete));self.style().unpolish(self);self.style().polish(self)
        from lan_panel import _size
        size=row['received'] if complete else option['bytes']
        self.size.setText(('约 ' if option['approximate'] and not complete else '')+_size(size) if size else '大小待确定')
        self.download.setEnabled(not complete);self.shelf.setEnabled(not row['shelved'])
        self.folder.setEnabled(complete);self.folder.setToolTip('打开所在文件夹' if complete else '下载完成后可打开所在文件夹')
        glyph='close' if busy else 'check' if complete else 'download'
        self.download.setAccessibleName('取消下载' if busy else '下载');self.download.setToolTip(self.download.accessibleName())
        if getattr(self.download,'_video_glyph',None)!=glyph:
            role='green' if complete else 'secondary' if busy else 'link'
            self.download._video_glyph=glyph;self.download._reference_icon=(glyph,role);self.download.setIcon(icon(glyph,colors(theme)[role]))
        self.shelf.setToolTip('已放入搁架' if row['shelved'] else '下载后放入搁架')
        labels={'ready':'','queued':'等待下载…','preparing':'正在准备下载组件…','selecting':'正在选择下载线路…','downloading':'正在下载…','processing':'正在整理音视频…','complete':'已下载 · 双击卡片可播放','cancelled':'已取消，可重新下载','error':row['error']}
        status=labels.get(row['status'],'')
        if row['status']=='downloading':
            if row['progress'] is not None:status=f'{row["progress"]:.0%}'
            if row['speed']:status+=f' · {row["speed"]/1024**2:.1f} MB/s'
        if row['shelved']:status='已下载并放入搁架 · 双击可播放'
        self.status.setText(status);self.status.setVisible(bool(status));self.setToolTip(row['path'] or '')
        self.progress.setVisible(busy and row['progress'] is not None)
        if row['progress'] is not None:self.progress.setRange(0,1000);self.progress.setValue(round(row['progress']*1000))

class VideoResult(QFrame):
    def __init__(self,entry,controller,parent):
        super().__init__(parent);self.entry=entry;self.controller=controller;self.cards={};self.grids=[];self.columns=0
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(12)
        summary=QHBoxLayout();summary.setSpacing(14);self.cover=CoverButton(entry,self);summary.addWidget(self.cover)
        self.cover.clicked.connect(lambda:parent.window().show_cover(entry))
        info=QVBoxLayout();info.setSpacing(5);title=plain_label(entry['title'],self,'videoTitle');title.setWordWrap(True);info.addWidget(title)
        duration=entry.get('duration');length=f'{int(duration)//60}:{int(duration)%60:02}' if duration is not None else ''
        meta=plain_label(' · '.join(value for value in (entry['uploader'],length) if value),self);info.addWidget(meta)
        info.addWidget(plain_label(f'解析成功 · {len(entry["sources"])} 个源',self,'videoSuccess'));summary.addLayout(info,1);layout.addLayout(summary)
        for kind,title in (('video','视频来源'),('audio','音频来源')):
            rows=[row for row in entry['sources'] if row['option']['kind']==kind]
            if not rows:continue
            heading=QHBoxLayout();heading.addWidget(plain_label(title,self,'videoSourceTitle'));heading.addWidget(plain_label(str(len(rows)),self));heading.addStretch()
            if kind=='video':heading.addWidget(plain_label('全部来源已展开',self))
            layout.addLayout(heading);grid=QGridLayout();grid.setContentsMargins(0,0,0,0);grid.setSpacing(SOURCE_CARD_SPACING_PX);layout.addLayout(grid)
            cards=[]
            for row in rows:
                card=VideoSourceCard(row,controller,self);self.cards[row['key']]=card;cards.append(card)
            self.grids.append((grid,cards))
        self._reflow(2)

    def resizeEvent(self,event):
        self._reflow(2 if self.width()>=SOURCE_GRID_BREAKPOINT_PX else 1);super().resizeEvent(event)

    def _reflow(self,columns):
        if self.columns==columns:return
        self.columns=columns
        for grid,cards in self.grids:
            for card in cards:grid.removeWidget(card)
            for index,card in enumerate(cards):grid.addWidget(card,index//columns,index%columns)
            for column in range(2):grid.setColumnStretch(column,1 if column<columns else 0)

    def refresh(self,theme):
        self.cover.dark=theme=='dark';self.cover.update()
        for card in self.cards.values():card.refresh(theme)

class VideoPreview(ReferenceModalDialog):
    def __init__(self,owner):
        super().__init__(owner,surface_width=660,modal=True);self.entry=None;self._theme=None;self._cover_key=None;self.setWindowTitle('视频封面预览')
        self.setWindowModality(Qt.WindowModal)
        header=QHBoxLayout();header.addWidget(plain_label('封面预览',self.body,'videoSourceTitle'),1)
        close=SettingsCloseButton(self.body,glyph_size=14);close.setFixedSize(28,28);close.clicked.connect(self.close);close.setAccessibleName('关闭封面预览');header.addWidget(close);self.close_button=close
        self.body_layout.addLayout(header);self.image=ImagePreview(self.body);self.image.setMinimumHeight(300);self.body_layout.addWidget(self.image)
        self.cover_status=plain_label('',self.body);self.body_layout.addWidget(self.cover_status)
        self.title=plain_label('',self.body,'videoTitle');self.title.setWordWrap(True);self.body_layout.addWidget(self.title)
        self.media=MediaPreview(self.body,motion=owner.manager.motion if hasattr(owner.manager,'motion') else True);self.media.setMinimumHeight(300);self.media.hide();self.body_layout.addWidget(self.media)
        self.play=button('播放已下载的视频','play',parent=self.body);self.play.clicked.connect(self._play_downloaded);self.body_layout.addWidget(self.play)

    def set_entry(self,entry,theme):
        self.media.release_source();self.media.hide();self.image.show();self.entry=entry;self.refresh(theme)

    def refresh(self,theme):
        if self._theme!=theme:
            self._theme=theme;self.set_surface_theme(theme);self.close_button.dark=theme=='dark';self.media.apply_theme(theme);refresh_tree(self)
        if self.entry is None:return
        self.title.setText(self.entry['title'])
        image=self.entry['cover'];key=(self.entry['key'],image.cacheKey() if image is not None else None)
        if key!=self._cover_key:self._cover_key=key;self.image.image=image;self.image.update()
        self.cover_status.setText('封面加载中…' if self.entry['cover_state'] in ('idle','queued','loading') else self.entry['cover_error'] or '')
        self.cover_status.setVisible(bool(self.cover_status.text()))
        self.play.setEnabled(any(row['path'] and row['status']=='complete' for row in self.entry['sources']))

    def _play_downloaded(self):
        row=next((row for row in self.entry['sources'] if row['path'] and row['status']=='complete'),None)
        if row is not None:self.show_media(row)

    def show_media(self,row):
        self.image.hide();self.media.show();self.media.set_source(row['path']);self.adjustSize()

    def hideEvent(self,event):
        self.media.release_source();super().hideEvent(event)

class VideoDialog(ReferenceModalDialog):
    def __init__(self,manager,controller):
        self._layout_ready=False
        super().__init__(manager.shelf,surface_width=940,modal=False)
        self.manager=manager;self.controller=controller;self.results={};self.cards={};self._theme=None;self.cover_preview=None
        self.setWindowTitle('FlickDock · 视频解析');self.setWindowFlag(Qt.WindowStaysOnTopHint,True);self.setMinimumSize(VIDEO_WINDOW_MIN_SIZE)
        self.body_layout.setContentsMargins(18,10,18,16);self.body_layout.setSpacing(20)
        self.title_bar=QWidget(self.surface);self.title_bar.setFixedHeight(44)
        header=QHBoxLayout(self.title_bar);header.setContentsMargins(18,0,4,0);header.setSpacing(12)
        header.addWidget(plain_label('视频解析',self.title_bar,'videoSourceTitle'));header.addWidget(plain_label('FlickDock',self.title_bar));header.addStretch()
        self.close_button=SettingsCloseButton(self.title_bar,glyph_size=14);self.close_button.setFixedSize(28,28);self.close_button.clicked.connect(self.close);self.close_button.setAccessibleName('关闭视频解析');header.addWidget(self.close_button)
        self.surface.layout().insertWidget(0,self.title_bar)
        input_container=QWidget(self.surface);input_row=QHBoxLayout(input_container);input_row.setContentsMargins(18,8,18,8);input_row.setSpacing(8)
        self.url=QLineEdit(input_container);self.url.setPlaceholderText('粘贴视频网页链接…');self.url.setAccessibleName('视频网页链接');self.url.setMaxLength(8192);input_row.addWidget(self.url,1)
        self.parse_button=button('解析',variant='primary',parent=input_container);self.parse_button.setFixedHeight(38);self.parse_button.setMinimumWidth(72);self.parse_button.clicked.connect(self._parse);input_row.addWidget(self.parse_button)
        self.stop_button=button('取消',parent=input_container);self.stop_button.clicked.connect(controller.cancel_parse);input_row.addWidget(self.stop_button)
        self.url.returnPressed.connect(self._parse);self.surface.layout().insertWidget(1,input_container)
        self.message=plain_label('',self.body);self.message.setWordWrap(True);self.body_layout.addWidget(self.message)
        self.result_layout=QVBoxLayout();self.result_layout.setSpacing(24);self.body_layout.addLayout(self.result_layout);self.body_layout.addStretch()
        footer=QWidget(self.surface);foot=QVBoxLayout(footer);foot.setContentsMargins(18,10,18,12);foot.setSpacing(9)
        self.prompt=QCheckBox('复制视频链接时提示解析',footer);self.prompt.toggled.connect(self._prompt_changed);foot.addWidget(self.prompt)
        folder_row=QHBoxLayout();self.folder=plain_label('',footer);self.folder.setMinimumWidth(0);folder_row.addWidget(self.folder,1)
        change=button('更改位置',parent=footer);change.clicked.connect(self._choose_folder);folder_row.addWidget(change);self.folder_button=change
        foot.addLayout(folder_row);self.surface.layout().addWidget(footer)
        self.chrome=WindowChrome(self,self.title_bar,manager.settings,'video',surface=self.surface)
        self.traffic_buttons=[]
        if sys.platform=='darwin':
            from native_ui_common import toggle_window_zoom
            from window_chrome import minimize_utility
            traffic=QWidget(self.title_bar);controls=QHBoxLayout(traffic);controls.setContentsMargins(0,0,0,0);controls.setSpacing(8)
            actions={'close':self.close,'min':lambda:minimize_utility(self),'max':lambda:toggle_window_zoom(self)}
            for role,tint,description in MAC_WINDOW_CONTROLS:
                control=QPushButton(traffic);control.setFixedSize(12,12);control.setAutoDefault(False)
                control.setAccessibleName(description);control.setToolTip(description)
                control.setStyleSheet(f'QPushButton{{background:{tint};border:0;border-radius:6px;padding:0;}}')
                control.clicked.connect(actions[role]);controls.addWidget(control);self.traffic_buttons.append(control)
            header.insertWidget(0,traffic);self.close_button.hide()
        self.chrome.default_size=QSize(VIDEO_WINDOW_SIZE)
        self._layout_ready=True;self.resize(VIDEO_WINDOW_SIZE)
        controller.changed.connect(self.refresh);controller.parsed.connect(self.show_cover);controller.preview_requested.connect(self._preview)
        manager.settings.changed.connect(self.refresh);self.refresh()

    def sizeHint(self):
        return QSize(VIDEO_WINDOW_SIZE) if self._layout_ready else super().sizeHint()

    def adjustSize(self):
        if not self._layout_ready:return super().adjustSize()
        if not self.isVisible() and not self.isMaximized():self.chrome.fit_screen(self.chrome.default_size)

    def _parse(self):self.controller.parse(self.url.text())

    def show_cover(self,entry):
        if not self.isVisible() or entry not in self.controller.entries:return
        if self.cover_preview is None:self.cover_preview=VideoPreview(self)
        self.cover_preview.set_entry(entry,self.manager.settings.theme);self.cover_preview.show();self.cover_preview.raise_()

    def _preview(self,row):
        if row not in self.controller.sources() or not row['path']:return
        self.show_cover(row['entry']);self.cover_preview.show_media(row)

    def hideEvent(self,event):
        if self.cover_preview:self.cover_preview.hide()
        super().hideEvent(event)

    def _prompt_changed(self,value):
        try:self.manager.settings.update(video_link_prompt=value)
        except (OSError,ValueError) as error:self.controller.message=str(error);self.controller.message_error=True;self.refresh()

    def _choose_folder(self):
        from PySide6.QtWidgets import QFileDialog
        folder=QFileDialog.getExistingDirectory(self,'选择视频保存位置',self.manager.settings.video_download_dir)
        if folder:
            try:self.manager.settings.update(video_download_dir=folder)
            except (OSError,ValueError) as error:self.controller.message=str(error);self.controller.message_error=True;self.refresh()

    def refresh(self):
        theme=self.manager.settings.theme;dark=theme=='dark'
        if self._theme!=theme:
            self._theme=theme;self.set_surface_theme(theme);self.close_button.dark=dark;c=colors(theme)
            self.setStyleSheet(f'QLabel{{color:{c["text"]};background:transparent;font-size:12px;}} QLabel#videoTitle{{font-size:15px;font-weight:600;}} QLabel#videoSourceTitle{{font-size:12px;font-weight:600;}} QLabel#videoMeta{{font-size:11px;color:{c["secondary"]};}} QLabel#videoSuccess{{font-size:11px;color:{c["green"]};}} QFrame#videoSourceCard{{background:{c["card"]};border:0;border-radius:10px;}} QFrame#videoSourceCard:hover{{background:{c["hover"]};}} QLineEdit{{background:{c["control"]};color:{c["text"]};border:0;border-radius:10px;padding:10px 12px;}} QLineEdit:focus{{border:1px solid {c["link"]};}} QProgressBar{{background:{c["control"]};border:0;border-radius:1px;}} QProgressBar::chunk{{background:{c["link"]};border-radius:1px;}}')
            refresh_tree(self)
            self.setStyleSheet(self.styleSheet()+f'QCheckBox{{font-size:11px;color:{c["secondary"]};spacing:8px;}} QCheckBox::indicator{{width:13px;height:13px;}} QFrame#videoSourceCard[downloadComplete="true"]{{background:{c["selected"]};}}')
        blocked=self.prompt.blockSignals(True);self.prompt.setChecked(self.manager.settings.video_link_prompt);self.prompt.blockSignals(blocked)
        from PySide6.QtGui import QFontMetrics
        folder='保存至 '+self.manager.settings.video_download_dir
        self.folder.setText(QFontMetrics(self.folder.font()).elidedText(folder,Qt.ElideMiddle,max(80,self.width()-210)));self.folder.setToolTip(folder)
        busy=self.controller.extracting is not None;self.parse_button.setEnabled(not busy);self.parse_button.setText('解析中…' if busy else '解析');self.stop_button.setVisible(busy)
        self.message.setText(self.controller.message)
        self.message.setVisible(not self.controller.entries or busy or self.controller.message_error)
        keys={entry['key'] for entry in self.controller.entries}
        for key in list(self.results):
            if key not in keys:
                result=self.results.pop(key);self.result_layout.removeWidget(result);result.hide();result.deleteLater()
                for source_key in result.cards:self.cards.pop(source_key,None)
        for entry in self.controller.entries:
            if entry['key'] not in self.results:
                result=VideoResult(entry,self.controller,self.body);self.results[entry['key']]=result;self.result_layout.addWidget(result);self.cards.update(result.cards)
            self.results[entry['key']].refresh(theme)
        if self.cover_preview and self.cover_preview.entry is not None:
            if self.cover_preview.entry not in self.controller.entries:self.cover_preview.hide()
            else:self.cover_preview.refresh(theme)

"""Movable media window, expanded source matrix and an owned cover/player dialog."""
from pathlib import Path
import sys
import time
from PySide6.QtCore import QEvent,QPointF,QRect,QRectF,QSize,Qt,QTimer
from PySide6.QtGui import QFont,QFontMetricsF,QPainter,QColor,QLinearGradient,QPen
from PySide6.QtWidgets import QButtonGroup,QCheckBox,QFrame,QGridLayout,QHBoxLayout,QLabel,QLineEdit,QPushButton,QSizePolicy,QStackedWidget,QStyle,QStyleOptionButton,QStylePainter,QVBoxLayout,QWidget
from app_settings import ReferenceModalDialog,SettingsCloseButton,SettingsToggleSwitch
from native_ui_common import colors,keyboard_focus_visible,set_ui_weight,RADIUS_CARD_PX,RADIUS_CONTROL_PX
from reference_widgets import IconTextButton,button,css,refresh_tree,icon
from image_preview import ImagePreview
from media_preview import MediaPreview
from window_chrome import WindowChrome

VIDEO_WINDOW_SIZE=QSize(980,740)
VIDEO_WINDOW_MIN_SIZE=QSize(600,420)
SOURCE_GRID_BREAKPOINT_PX=580
SOURCE_CARD_SPACING_PX=10
MAC_WINDOW_CONTROLS=(('close','#FF5F56','关闭视频解析'),('min','#FFBD2E','最小化'),('max','#27C93F','最大化或还原'))
PROMPT_SWITCH_WIDTH_PX=44
SKELETON_CYCLE_SECONDS=1.6


class VideoSkeleton(QWidget):
    """An honest loading placeholder; the frame clock runs only while visible."""
    def __init__(self,parent,*,motion=True):
        super().__init__(parent)
        from display_frames import FramePulse
        self._pulse=FramePulse(self);self._pulse.timeout.connect(self.update)
        self._active=False;self._motion=motion;self.dark=True
        self.setAccessibleName('正在解析视频信息')
        self.setMinimumHeight(420);self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self.hide()

    def set_active(self,active):
        active=bool(active)
        if active==self._active:return
        self._active=active;self.setVisible(active)
        self._sync_pulse()

    def _sync_pulse(self):
        from ui_transitions import motion_allowed
        if self._active and self.isVisible() and self._motion and motion_allowed():
            if not self._pulse.isActive():self._pulse.start()
        else:self._pulse.stop()

    def showEvent(self,event):
        super().showEvent(event);self._sync_pulse()

    def hideEvent(self,event):
        self._pulse.stop();super().hideEvent(event)

    def resizeEvent(self,event):
        height=420 if event.size().width()>=SOURCE_GRID_BREAKPOINT_PX else 600
        if self.minimumHeight()!=height:self.setMinimumHeight(height)
        super().resizeEvent(event)

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);p.setPen(Qt.NoPen)
        base=QColor('#272727' if self.dark else '#E6E6E6')
        shine=QColor('#383838' if self.dark else '#F0F0F0')
        phase=(time.monotonic()%SKELETON_CYCLE_SECONDS)/SKELETON_CYCLE_SECONDS if self._pulse.isActive() else .5
        width=max(1,self.width());gradient=QLinearGradient(-width+2*width*phase,0,2*width*phase,0)
        gradient.setColorAt(0,base);gradient.setColorAt(.5,shine);gradient.setColorAt(1,base);p.setBrush(gradient)
        def block(x,y,w,h,r=6):
            p.drawRoundedRect(QRectF(x,y,max(0,w),h),r,r)
        block(0,0,140,86,10);block(156,8,min(330,width-156),16)
        block(156,34,min(190,width-156),11);block(156,58,min(130,width-156),11)
        block(0,118,92,14)
        columns=2 if width>=SOURCE_GRID_BREAKPOINT_PX else 1;gap=SOURCE_CARD_SPACING_PX
        card_width=(width-gap*(columns-1))/columns
        for index in range(4):
            x=(index%columns)*(card_width+gap);y=146+(index//columns)*98
            block(x,y,card_width,88,10)
        y=146+((4+columns-1)//columns)*98+12
        block(0,y,90,14)

def plain_label(text,parent,name='videoMeta'):
    label=QLabel(text,parent);label.setObjectName(name);label.setTextFormat(Qt.PlainText)
    return label

class CoverButton(QPushButton):
    def __init__(self,entry,parent):
        super().__init__('',parent);self.entry=entry;self.dark=True
        self.setFixedSize(140,86);self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName('查看视频封面预览');self.setToolTip('查看封面，或播放已下载的视频')
    def enterEvent(self,event):super().enterEvent(event);self.update()
    def leaveEvent(self,event):super().leaveEvent(event);self.update()
    def paintEvent(self,event):
        from PySide6.QtGui import QPainterPath
        c=colors('dark' if self.dark else 'light');p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        path=QPainterPath();path.addRoundedRect(self.rect(),RADIUS_CARD_PX,RADIUS_CARD_PX);p.setClipPath(path);p.fillRect(self.rect(),QColor(c['control']))
        image=self.entry['cover']
        if image is not None:
            crop=image.size().scaled(self.size(),Qt.KeepAspectRatioByExpanding)
            target=QRect((self.width()-crop.width())//2,(self.height()-crop.height())//2,crop.width(),crop.height())
            p.setRenderHint(QPainter.SmoothPixmapTransform);p.drawImage(target,image)
        if self.isDown() or self.underMouse():
            p.fillRect(self.rect(),QColor(0,0,0,70 if self.isDown() else 34))
        if image is not None:
            center=QRect((self.width()-30)//2,(self.height()-30)//2,30,30)
            p.setPen(Qt.NoPen);p.setBrush(QColor(0,0,0,120));p.drawEllipse(center)
            icon('play','#FFFFFF').paint(p,center.adjusted(7,7,-7,-7))
        else:
            p.setPen(QColor(c['secondary']));p.drawText(self.rect(),Qt.AlignCenter,'封面加载中…' if self.entry['cover_state'] in ('idle','queued','loading') else '暂无封面')
        if keyboard_focus_visible(self):
            p.setClipping(False);p.setPen(QPen(QColor(c['link']),1.5));p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),RADIUS_CONTROL_PX,RADIUS_CONTROL_PX)
        p.end()

class VideoSourceCard(QFrame):
    def __init__(self,row,controller,parent):
        super().__init__(parent);self.row=row;self.controller=controller;self.setObjectName('videoSourceCard')
        self.setMinimumWidth(220)
        self.setMinimumHeight(0);self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        layout=QVBoxLayout(self);layout.setContentsMargins(12,10,12,10);layout.setSpacing(6)
        head=QHBoxLayout();head.setContentsMargins(0,0,0,0);head.setSpacing(10)
        self.kind_frame=QFrame(self);self.kind_frame.setObjectName('videoSourceIcon');self.kind_frame.setFixedSize(36,36)
        art=QHBoxLayout(self.kind_frame);art.setContentsMargins(0,0,0,0)
        self.kind=QLabel(self.kind_frame);self.kind.setAlignment(Qt.AlignCenter);art.addWidget(self.kind)
        head.addWidget(self.kind_frame,0,Qt.AlignTop)
        text=QVBoxLayout();text.setContentsMargins(0,0,0,0);text.setSpacing(4)
        self.title=plain_label(row['option']['label'],self,'videoSourceTitle');text.addWidget(self.title)
        self.details=plain_label('',self);self.details.setWordWrap(True);self.details.setMinimumWidth(0)
        self.details.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);text.addWidget(self.details)
        text.addStretch();head.addLayout(text,1)
        actions=QVBoxLayout();actions.setContentsMargins(0,0,0,0);actions.setSpacing(4)
        foot=QHBoxLayout();foot.setContentsMargins(0,0,0,0);foot.setSpacing(6)
        self.download=button('','download',variant='secondary',parent=self)
        self.shelf=button('','shelf',variant='secondary',parent=self)
        self.folder=button('','folder',variant='secondary',parent=self)
        for control,action,name in ((self.download,'download','下载'),(self.folder,'folder','打开所在文件夹'),(self.shelf,'shelf','下载后放入搁架')):
            dark_style,light_style=control._reference_style
            css(control,dark_style.replace('background:#272727','background:#333333',1),light_style)
            control.setFixedSize(30,30);control.setAccessibleName(name);control.setToolTip(name)
            control.clicked.connect(lambda checked=False,action=action:self._invoke(action));foot.addWidget(control)
        actions.addLayout(foot);head.addLayout(actions);layout.addLayout(head)
        self.status=plain_label('',self);self.status.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        self.status.setWordWrap(True);layout.addWidget(self.status)
        from PySide6.QtWidgets import QProgressBar
        self.progress_row=QWidget(self);progress_line=QHBoxLayout(self.progress_row)
        progress_line.setContentsMargins(0,0,0,0);progress_line.setSpacing(8)
        self.progress=QProgressBar(self.progress_row);self.progress.setFixedHeight(3);self.progress.setTextVisible(False);progress_line.addWidget(self.progress,1)
        self.progress_percent=plain_label('',self.progress_row);self.progress_percent.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        self.progress_percent.setAccessibleName('下载进度');progress_line.addWidget(self.progress_percent)
        layout.addWidget(self.progress_row)
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
        for control in (self.download,self.folder,self.shelf):
            if control.property('referenceTheme')!=theme:control.setProperty('referenceTheme',theme);control.update()
        if self._dark!=(theme=='dark'):
            self._dark=theme=='dark';c=colors(theme);self.kind.setPixmap(icon('audio' if option['kind']=='audio' else 'video',c['secondary']).pixmap(18,18));refresh_tree(self,theme)
            self.kind_frame.setStyleSheet(f"QFrame#videoSourceIcon{{background:{c['control']};border:1px solid {c['border']};border-radius:{RADIUS_CONTROL_PX}px;}}")
        if self.property('downloadComplete')!=bool(complete):
            self.setProperty('downloadComplete',bool(complete));self.style().unpolish(self);self.style().polish(self)
        from lan_panel import _size
        size=row['received'] if complete else option['bytes']
        size_text=(('约 ' if option['approximate'] and not complete else '')+_size(size)) if size else '大小待确定'
        detail=f"{option['details']} · {size_text}" if option['details'] else size_text
        self.details.setText(detail);self.details.setToolTip(detail);self.details.setAccessibleName(detail)
        self.download.setEnabled(not complete);self.shelf.setEnabled(not row['shelved'])
        self.folder.setEnabled(complete);self.folder.setToolTip('打开所在文件夹' if complete else '下载完成后可打开所在文件夹')
        glyph='close' if busy else 'check' if complete else 'download'
        self.download.setAccessibleName('取消下载' if busy else '下载');self.download.setToolTip(self.download.accessibleName())
        if getattr(self.download,'_video_glyph',None)!=glyph:
            role='green' if complete else 'secondary' if busy else 'link'
            self.download._video_glyph=glyph;self.download._reference_icon=(glyph,role);self.download.setIcon(icon(glyph,colors(theme)[role]))
        self.shelf.setToolTip('已放入搁架' if row['shelved'] else '下载后放入搁架')
        labels={'ready':'','queued':'等待下载…','preparing':'正在安装下载组件…','selecting':'正在选择线路…','downloading':'正在下载…','processing':'正在整理…','complete':'已下载','cancelled':'已取消','error':'下载失败'}
        status=labels.get(row['status'],'')
        if row['status']=='error' and row['error'].startswith('视频下载组件安装失败'):
            status='组件安装失败，请检查网络后重试'
        if row['status']=='downloading':
            if row['speed']:status+=f' · {row["speed"]/1024**2:.1f} MB/s'
        if row['shelved']:status='已放入搁架'
        c=colors(theme)
        status_color=c['red'] if row['status']=='error' else c['green'] if complete else c['secondary']
        self.status.setStyleSheet(f'color:{status_color};background:transparent;border:0;')
        self.status.setText(status);self.status.setVisible(bool(status))
        self.status.setToolTip(row['error'] if row['status']=='error' else status)
        self.status.setAccessibleName((status+'：'+row['error']) if row['status']=='error' and row['error'] else status)
        self.setToolTip(row['error'] if row['status']=='error' else row['path'] or '')
        self.progress_row.setVisible(busy and row['progress'] is not None)
        if row['progress'] is not None:
            self.progress.setRange(0,1000);self.progress.setValue(round(row['progress']*1000))
            self.progress_percent.setText(f'{row["progress"]:.0%}')
            self.progress_percent.setAccessibleName('下载进度 '+self.progress_percent.text())

class VideoSourceTab(IconTextButton):
    """Keep the source count smaller while retaining a single button target."""
    def __init__(self,text,parent=None):
        super().__init__(text,parent)
        self.source_title,self.source_count=text.rsplit('  ',1)
        self._tab_theme='dark'

    def text_fonts(self):
        title=QFont(self.font());set_ui_weight(title,400)
        count=QFont(title)
        if title.pixelSize()>0:count.setPixelSize(max(8,title.pixelSize()-2))
        else:count.setPointSizeF(max(8.,title.pointSizeF()-1.5))
        set_ui_weight(count,400)
        return title,count

    def paintEvent(self,event):
        painter=QStylePainter(self);option=QStyleOptionButton();self.initStyleOption(option)
        option.text='';painter.drawControl(QStyle.CE_PushButton,option)
        c=colors(self._tab_theme);hover=bool(self.property('tabHover')) or self.underMouse()
        painter.setPen(QColor(c['text'] if self.isChecked() or hover else c['secondary']))
        if not self.isEnabled():painter.setOpacity(.55)
        title_font,count_font=self.text_fonts();title_metrics=QFontMetricsF(title_font);count_metrics=QFontMetricsF(count_font)
        title_width=title_metrics.horizontalAdvance(self.source_title);count_width=count_metrics.horizontalAdvance(self.source_count)
        x=(self.width()-title_width-5-count_width)/2
        baseline=(self.height()-title_metrics.height())/2+title_metrics.ascent()
        painter.setFont(title_font);painter.drawText(QPointF(x,baseline),self.source_title)
        painter.setFont(count_font);painter.drawText(QPointF(x+title_width+5,baseline),self.source_count)


class VideoSourceStack(QStackedWidget):
    """Size to the current source page at its actual width, not hidden pages."""
    def __init__(self,parent=None):
        super().__init__(parent)
        policy=QSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        policy.setHeightForWidth(True);self.setSizePolicy(policy)
        self.currentChanged.connect(lambda _index:self.updateGeometry())

    def hasHeightForWidth(self):return True

    def heightForWidth(self,width):
        page=self.currentWidget()
        if page is None:return 0
        layout=page.layout()
        if layout is None:return page.sizeHint().height()
        height=layout.heightForWidth(max(1,width))
        return max(0,height if height>=0 else layout.sizeHint().height())

    def sizeHint(self):
        page=self.currentWidget()
        if page is None:return QSize(0,0)
        return QSize(page.sizeHint().width(),self.heightForWidth(max(1,self.width())))

    def minimumSizeHint(self):
        page=self.currentWidget()
        if page is None:return QSize(0,0)
        return QSize(page.minimumSizeHint().width(),self.heightForWidth(max(1,self.width())))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if event.size().width()!=event.oldSize().width():self.updateGeometry()


class VideoResult(QFrame):
    def __init__(self,entry,controller,parent):
        super().__init__(parent);self.entry=entry;self.controller=controller;self.cards={};self.grids=[];self.columns=0
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(12)
        summary=QHBoxLayout();summary.setSpacing(14);self.cover=CoverButton(entry,self);summary.addWidget(self.cover)
        self.cover.clicked.connect(lambda:parent.window().show_cover(entry))
        info=QVBoxLayout();info.setSpacing(5);title=plain_label(entry['title'],self,'videoTitle');title.setWordWrap(True);info.addWidget(title)
        duration=entry.get('duration');length=f'{int(duration)//60}:{int(duration)%60:02}' if duration is not None else ''
        meta=plain_label(' · '.join(value for value in (entry['uploader'],length) if value),self);info.addWidget(meta)
        info.addWidget(plain_label(f'解析成功 · {len(entry["sources"])} 个源',self,'videoSuccess'));summary.addLayout(info,1);layout.addLayout(summary)
        tabs=QHBoxLayout();tabs.setContentsMargins(0,0,0,0);tabs.setSpacing(6)
        from ui_transitions import SelectionSurface
        self.tabs_frame=SelectionSurface(self,radius=RADIUS_CONTROL_PX-2);self.tabs_frame.setObjectName('videoSourceTabs')
        self.tabs_frame.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed)
        tab_row=QHBoxLayout(self.tabs_frame);tab_row.setContentsMargins(4,4,4,4);tab_row.setSpacing(4)
        self.source_tabs={};self.source_stack=VideoSourceStack(self);self.source_group=QButtonGroup(self);self.source_group.setExclusive(True)
        for index,(kind,title) in enumerate((('video','视频'),('audio','音频'))):
            rows=[row for row in entry['sources'] if row['option']['kind']==kind]
            tab=button(f'{title}  {len(rows)}',variant='secondary',parent=self.tabs_frame,control_type=VideoSourceTab);tab.setCheckable(True);tab.setEnabled(bool(rows))
            tab.setProperty('tabHover',False);tab.installEventFilter(self)
            tab.setAccessibleName(f'{title}来源，共 {len(rows)} 个');tab.setFixedHeight(30)
            self.source_group.addButton(tab,index);self.source_tabs[kind]=tab;tab_row.addWidget(tab);self.tabs_frame.add_selection_button(tab)
            tab.clicked.connect(lambda checked=False,index=index:self.source_stack.setCurrentIndex(index))
            page=QWidget(self.source_stack);grid=QGridLayout(page);grid.setContentsMargins(0,0,0,0);grid.setSpacing(SOURCE_CARD_SPACING_PX);grid.setAlignment(Qt.AlignTop)
            cards=[]
            for row in rows:
                card=VideoSourceCard(row,controller,page);self.cards[row['key']]=card;cards.append(card)
            self.grids.append((grid,cards));self.source_stack.addWidget(page)
        tabs.addWidget(self.tabs_frame);tabs.addStretch();layout.addLayout(tabs);layout.addWidget(self.source_stack)
        initial='video' if self.source_tabs['video'].isEnabled() else 'audio'
        self.source_tabs[initial].setChecked(True);self.source_stack.setCurrentIndex(0 if initial=='video' else 1)
        self.source_stack.currentChanged.connect(lambda _index:self._fit_sources())
        self._reflow(2)
        self._fit_sources()

    def eventFilter(self,watched,event):
        if watched in self.source_tabs.values() and event.type() in (QEvent.Type.Enter,QEvent.Type.Leave):
            watched.setProperty('tabHover',event.type()==QEvent.Type.Enter)
            watched.style().unpolish(watched);watched.style().polish(watched);watched.update()
        return super().eventFilter(watched,event)

    def resizeEvent(self,event):
        self._reflow(2 if self.width()>=SOURCE_GRID_BREAKPOINT_PX else 1)
        self._fit_sources();super().resizeEvent(event)

    def _fit_sources(self):
        if not self.grids:return
        index=self.source_stack.currentIndex()
        if not 0<=index<len(self.grids):return
        grid=self.grids[index][0];width=max(1,self.width())
        height=grid.heightForWidth(width)
        if height<0:height=grid.sizeHint().height()
        if self.source_stack.minimumHeight()!=height or self.source_stack.maximumHeight()!=height:
            self.source_stack.setFixedHeight(max(0,height));self.layout().invalidate();self.updateGeometry()

    def _reflow(self,columns):
        if self.columns==columns:return
        self.columns=columns
        for grid,cards in self.grids:
            for card in cards:grid.removeWidget(card)
            for index,card in enumerate(cards):grid.addWidget(card,index//columns,index%columns)
            for column in range(2):grid.setColumnStretch(column,1 if column<columns else 0)
        self._fit_sources()

    def refresh(self,theme):
        self.cover.dark=theme=='dark';self.cover.update()
        c=colors(theme)
        self.tabs_frame.set_selection_theme(theme)
        self.tabs_frame.setStyleSheet(f"QFrame#videoSourceTabs{{background:{c['control']};border:0;border-radius:{RADIUS_CONTROL_PX}px;}}")
        for tab in self.source_tabs.values():
            tab._tab_theme=theme
            tab.setStyleSheet(f"QPushButton{{background:transparent;color:{c['secondary']};border:1px solid transparent;border-radius:{RADIUS_CONTROL_PX-2}px;padding:4px 12px;}} QPushButton:hover:enabled,QPushButton[tabHover='true']:enabled{{background:{c['hover']};color:{c['text']};}} QPushButton:checked,QPushButton:checked:hover:enabled,QPushButton[tabHover='true']:checked:enabled,QPushButton:checked:pressed{{background:transparent;border:1px solid transparent;color:{c['text']};}} QPushButton:pressed:!checked:enabled{{background:{c['selected']};}} QPushButton:disabled{{color:{c['muted']};}} QPushButton:focus{{border:1px solid {c['ring']};}}")
        for card in self.cards.values():card.refresh(theme)
        for grid,_cards in self.grids:grid.invalidate()
        self._fit_sources()
        # Visibility changes on a status/progress row are delivered by Qt's
        # layout queue; measure again once those changes have settled.
        QTimer.singleShot(0,self,self._fit_sources)

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
        # A topmost shelf must not make this independent utility window inherit
        # its native owner z-order. The manager owns this dialog's lifecycle.
        super().__init__(None,surface_width=940,modal=False)
        self.setFont(manager.shelf.font())
        self.manager=manager;self.controller=controller;self.results={};self.cards={};self._theme=None;self.cover_preview=None
        self.setWindowTitle('FlickDock · 视频解析');self.setMinimumSize(VIDEO_WINDOW_MIN_SIZE)
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
        self.skeleton=VideoSkeleton(self.body,motion=getattr(manager,'motion',True));self.body_layout.addWidget(self.skeleton)
        self.result_layout=QVBoxLayout();self.result_layout.setSpacing(24);self.body_layout.addLayout(self.result_layout);self.body_layout.addStretch()
        self.scroll._flickdock_edge_bounce=True
        footer=QWidget(self.surface);footer.setObjectName('videoFooter');footer.setAttribute(Qt.WA_StyledBackground,True)
        foot=QHBoxLayout(footer);foot.setContentsMargins(18,8,18,8);foot.setSpacing(8)
        self.prompt_label=plain_label('复制视频链接时提示解析',footer,'videoSourceTitle');foot.addWidget(self.prompt_label)
        self.prompt=SettingsToggleSwitch('',footer);self.prompt.setFixedWidth(PROMPT_SWITCH_WIDTH_PX)
        self.prompt.setAccessibleName('复制视频链接时提示解析');self.prompt_label.setBuddy(self.prompt)
        self.prompt.toggled.connect(self._prompt_changed);foot.addWidget(self.prompt,0,Qt.AlignLeft)
        self.folder=plain_label('',footer);self.folder.setMinimumWidth(0);self.folder.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);self.folder.setAlignment(Qt.AlignRight|Qt.AlignVCenter);foot.addWidget(self.folder,1)
        change=button('更改位置',variant='secondary',parent=footer);change.setMinimumWidth(76)
        change.setAccessibleName('更改视频保存位置');change.clicked.connect(self._choose_folder);foot.addWidget(change);self.folder_button=change
        self.surface.layout().addWidget(footer)
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
        controller.changed.connect(self.refresh);controller.preview_requested.connect(self._preview)
        manager.settings.changed.connect(self.refresh);self.refresh()

    def sizeHint(self):
        return QSize(VIDEO_WINDOW_SIZE) if self._layout_ready else super().sizeHint()

    def adjustSize(self):
        if not self._layout_ready:return super().adjustSize()
        if not self.isVisible() and not self.isMaximized():self.chrome.fit_screen(self.chrome.default_size)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'folder'):self._refresh_folder_label()

    def _refresh_folder_label(self):
        from PySide6.QtGui import QFontMetrics
        folder='保存至 '+self.manager.settings.video_download_dir
        available=self.width()-PROMPT_SWITCH_WIDTH_PX-self.prompt_label.sizeHint().width()-150
        self.folder.setText(QFontMetrics(self.folder.font()).elidedText(folder,Qt.ElideMiddle,max(80,available)))
        self.folder.setToolTip(folder)

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
        from theme_accent import current
        accent=current()
        if self._theme!=theme or getattr(self,'_accent',None)!=accent:
            self._accent=accent
            self._theme=theme;self.set_surface_theme(theme);self.close_button.dark=dark;c=colors(theme)
            self.setStyleSheet(f'QLabel{{color:{c["text"]};background:transparent;font-size:12px;}} QLabel#videoTitle{{font-size:15px;font-weight:600;}} QLabel#videoSourceTitle{{font-size:12px;font-weight:600;}} QLabel#videoMeta{{font-size:11px;color:{c["secondary"]};}} QLabel#videoSuccess{{font-size:11px;color:{c["green"]};}} QWidget#videoFooter{{background:{c["background"]};border:0;}} QFrame#videoSourceCard{{background:{c["card"]};border:1px solid {c["border"]};border-radius:{RADIUS_CARD_PX}px;}} QFrame#videoSourceCard:hover{{background:{c["hover"]};}} QLineEdit{{background:{c["input"]};color:{c["text"]};border:1px solid {c["input_border"]};border-radius:{RADIUS_CONTROL_PX}px;padding:10px 12px;}} QLineEdit:focus{{border-color:{c["ring"]};}} QProgressBar{{background:{c["control"]};border:0;border-radius:3px;}} QProgressBar::chunk{{background:{c["action"]};border-radius:3px;}}')
            refresh_tree(self,theme)
            self.prompt.dark=dark
            self.setStyleSheet(self.styleSheet()+f'QCheckBox{{font-size:12px;color:{c["text"]};}} QFrame#videoSourceCard[downloadComplete="true"]{{background:{c["selected"]};}}')
        blocked=self.prompt.blockSignals(True);self.prompt.setChecked(self.manager.settings.video_link_prompt);self.prompt.blockSignals(blocked)
        self._refresh_folder_label()
        busy=self.controller.extracting is not None;self.parse_button.setEnabled(not busy);self.parse_button.setText('解析中…' if busy else '解析');self.stop_button.setVisible(busy)
        self.skeleton.dark=dark;self.skeleton.set_active(busy)
        self.message.setText(self.controller.message)
        c=colors(theme)
        self.message.setStyleSheet(f'color:{c["red"] if self.controller.message_error else c["secondary"]};background:transparent;border:0;')
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

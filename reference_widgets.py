"""Qt artwork and controls transcribed from the supplied FlickDock HTML.

Only presentation lives here. Content, drag MIME, disk work and network services
continue to be owned by the existing shelf and panel classes.
"""
from pathlib import Path
import sys
import re

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QLinearGradient
from PySide6.QtWidgets import (QLabel, QPushButton, QWidget, QFrame, QHBoxLayout,
    QVBoxLayout, QScrollArea, QLineEdit, QProgressBar, QButtonGroup, QSizePolicy,
    QStackedWidget, QPlainTextEdit, QCheckBox, QApplication, QDialog)

import ui_theme
from native_ui_common import colors, icon
from content_stack import StackTile
from flickdock_motion import FeedbackButton
from transfer_visuals import TransferIconLabel, CLOUD_STATUS_LABELS


def palette():
    return colors(ui_theme.current_theme())


def ink(value):
    if str(value).startswith('rgba('):
        return QColor(*[int(float(x)) for x in re.findall(r'[\d.]+',str(value))])
    return QColor(value)


def css(widget, dark, light=None):
    # Chromium synthesizes semibold from the bundled Regular face as bold;
    # Qt only enables that synthesis at 700. Match the supplied HTML weight.
    dark=dark.replace('font:600 ','font:700 ').replace('font-weight:600','font-weight:700')
    if light is not None:light=light.replace('font:600 ','font:700 ').replace('font-weight:600','font-weight:700')
    if sys.platform=='darwin' and QApplication.instance():
        family=QApplication.font().family()
        dark=dark.replace("'HarmonyOS Sans SC'",f"'{family}'")
        if light is not None:light=light.replace("'HarmonyOS Sans SC'",f"'{family}'")
    widget._reference_style=(dark, light if light is not None else dark)
    widget.setStyleSheet(widget._reference_style[ui_theme.current_theme()=='light'])


def refresh_tree(widget):
    for child in [widget, *widget.findChildren(QWidget)]:
        source=getattr(child,'_reference_style',None)
        if source:child.setStyleSheet(source[ui_theme.current_theme()=='light'])
        if getattr(child,'_reference_icon',None):
            name,tint=child._reference_icon
            child.setIcon(icon(name,palette().get(tint,tint)))
        child.update()


def label(value='',size=12,weight=400,tint='text',parent=None):
    widget=QLabel(value,parent)
    dark=colors('dark');light=colors('light')
    css(widget,f"color:{dark.get(tint,tint)};background:transparent;border:0;font:{weight} {size}px 'HarmonyOS Sans SC';",
        f"color:{light.get(tint,tint)};background:transparent;border:0;font:{weight} {size}px 'HarmonyOS Sans SC';")
    widget.setTextFormat(Qt.PlainText)
    return widget


def button(value='',glyph='',variant='quiet',parent=None):
    widget=QPushButton(value,parent)
    widget.setCursor(Qt.PointingHandCursor)
    widget.setFocusPolicy(Qt.StrongFocus)
    widget.setFixedHeight(28)
    sources=[]
    for theme in ('dark','light'):
        c=colors(theme)
        background=c['control'] if variant=='secondary' else c['blue'] if variant=='primary' else 'rgba(255,69,58,38)' if variant=='danger' else 'transparent'
        ink='white' if variant=='primary' else c['red'] if variant=='danger' else c['secondary'] if variant=='quiet' else c['text']
        border='rgba(255,69,58,76)' if variant=='danger' else c['border'] if variant=='secondary' else 'transparent'
        sources.append(f"QPushButton{{background:{background};color:{ink};border:1px solid {border};border-radius:8px;padding:4px 11px;font:500 12px 'HarmonyOS Sans SC';}} QPushButton:hover{{background:{c['hover']};}} QPushButton:pressed{{background:{c['selected']};}} QPushButton:checked{{background:{c['hover']};color:{c['text']};}} QPushButton:disabled{{color:{c['muted']};background:{c['control']};border-color:transparent;}}")
    css(widget,*sources)
    if glyph:
        widget._reference_icon=(glyph,'secondary')
        widget.setIcon(icon(glyph,palette()['secondary']))
    return widget


class ToolbarButton(FeedbackButton):
    """Retain the existing interruptible press spring with the HTML glyphs."""
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        c=palette();v=self.spring.values
        center=QRectF(self.rect()).center()
        p.translate(center);p.scale(v['scale']*self._presentation_scale[0],v['scale']*self._presentation_scale[1]);p.translate(-center)
        if self.glyph in ('copy','check'):
            p.setPen(Qt.NoPen);p.setBrush(QColor(c['blue']));p.drawRoundedRect(QRectF(self.rect()).adjusted(0,1,0,-1),8,8)
            f=p.font();f.setPixelSize(12);p.setFont(f);p.setPen(QColor('white'));p.drawText(self.rect(),Qt.AlignCenter,'复制' if self.glyph=='copy' else '✓');p.end();return
        if self.isChecked() or v['hover']>.01 or v['down']>.01:
            fill=ink(c['selected'] if self.isChecked() else c['hover'])
            if not self.isChecked():fill.setAlphaF(fill.alphaF()*min(1,v['hover']+v['down']))
            p.setPen(Qt.NoPen);p.setBrush(fill);p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),8,8)
        if not self.isEnabled():p.setOpacity(.4)
        name={'pin-filled':'pin','check':'check'}.get(self.glyph,self.glyph)
        tint=self._glyph_tint or (c['blue'] if self.isChecked() else c['secondary'])
        icon(name,c['secondary'] if name=='cloud' else tint).paint(p,QRect((self.width()-16)//2,(self.height()-16)//2,16,16))
        if name=='cloud':
            p.setPen(QPen(ink(c['surface']),2));p.setBrush(QColor(tint));p.drawEllipse(QRectF(self.width()-12,self.height()-12,6,6))
        p.end()


class CloudSelector(QPushButton):
    def __init__(self,shelf,parent=None):
        super().__init__(parent);self.shelf=shelf
        self.setFixedSize(92,28);self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName('云同步方向控制')

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);c=palette()
        enabled=self.shelf.manager.settings.sync_upload_enabled and self.shelf.manager.settings.sync_receive_enabled
        p.setBrush(ink(c['hover'] if self.underMouse() else c['control']));p.setPen(QPen(ink(c['border']),1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),14,14)
        p.setPen(Qt.NoPen);p.setBrush(QColor(c['green'] if enabled else c['red']));p.drawEllipse(QRectF(11,10,8,8))
        f=QFont(self.font());f.setPixelSize(11);p.setFont(f);p.setPen(QColor(c['text']))
        p.drawText(QRectF(24,0,49,28),Qt.AlignVCenter,'云同步' if enabled else '同步受限')
        icon('chevron',c['secondary']).paint(p,76,9,10,10);p.end()


class AvatarButton(ToolbarButton):
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        gradient=QLinearGradient(2,self.height(),self.width()-2,2);gradient.setColorAt(0,QColor('#0A84FF'));gradient.setColorAt(1,QColor('#BF5AF2'))
        p.setPen(Qt.NoPen);p.setBrush(gradient);p.drawEllipse(QRectF(2,2,self.width()-4,self.height()-4))
        p.setPen(QColor('white'));f=p.font();f.setPixelSize(10);f.setBold(True);p.setFont(f);p.drawText(self.rect(),Qt.AlignCenter,getattr(self,'initial','R'));p.end()


class ProfileButton(QPushButton):
    def __init__(self,parent=None):
        super().__init__(parent);self.username='未登录';self.status='登录以同步搁架';self.connected=False
        self.setCursor(Qt.PointingHandCursor);self.setFixedHeight(49)

    def set_profile(self,name,connected=False,status=''):
        self.username=str(name or '未登录');self.connected=bool(connected)
        self.status=str(status or ('云同步已连接' if connected else '登录以同步搁架'));self.update()

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);c=palette()
        p.setPen(QPen(ink(c['divider']),1));p.drawLine(0,0,self.width(),0)
        if self.underMouse():p.setPen(Qt.NoPen);p.setBrush(ink(c['control']));p.drawRoundedRect(self.rect().adjusted(0,2,0,-1),12,12)
        gradient=QLinearGradient(8,41,36,13);gradient.setColorAt(0,QColor('#0A84FF'));gradient.setColorAt(1,QColor('#BF5AF2'));p.setBrush(gradient);p.setPen(Qt.NoPen);p.drawEllipse(QRectF(8,12,28,28))
        f=p.font();f.setPixelSize(12);f.setBold(True);p.setFont(f);p.setPen(QColor('white'));p.drawText(QRectF(8,12,28,28),Qt.AlignCenter,self.username[:1].upper())
        p.setPen(QColor(c['text']));p.drawText(QRectF(46,10,self.width()-68,20),Qt.AlignVCenter,p.fontMetrics().elidedText(self.username,Qt.ElideRight,self.width()-68))
        f.setPixelSize(10);f.setBold(False);p.setFont(f);p.setPen(QColor(c['secondary']));p.drawText(QRectF(46,29,self.width()-68,14),Qt.AlignVCenter,p.fontMetrics().elidedText(self.status,Qt.ElideRight,self.width()-68))
        icon('right',c['muted']).paint(p,QRect(self.width()-20,22,12,12));p.end()


class SelectionCheck(QCheckBox):
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor('#0A84FF') if self.isChecked() else Qt.NoBrush)
        p.setPen(QPen(QColor('#0A84FF') if self.isChecked() else ink(palette()['input_border']),1))
        p.drawRoundedRect(QRectF(.5,.5,14,14),4,4)
        if self.isChecked():
            p.setPen(QPen(QColor('white'),1.5,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));p.drawLine(QPointF(4,7),QPointF(6,10));p.drawLine(QPointF(6,10),QPointF(11,4.5))
        p.end()


class RenameDialog(QDialog):
    """A presentation-only sheet reusing the shelf's existing rename editor."""
    def __init__(self,shelf):
        super().__init__(shelf,Qt.Dialog|Qt.FramelessWindowHint)
        from collections import OrderedDict
        self.shelf=shelf;self._shadow_cache=OrderedDict()
        self.setAttribute(Qt.WA_TranslucentBackground);self.setWindowModality(Qt.WindowModal)
        self.setWindowTitle('重命名文件');self.setFixedSize(420,252)
        layout=QVBoxLayout(self);layout.setContentsMargins(20,20,20,20)
        self.card=QFrame(self);self.card.setObjectName('renameSheet')
        c=palette();css(self.card,f"QFrame#renameSheet{{background:#1C1C1E;border:1px solid rgba(255,255,255,20);border-radius:{c['radius']}px;}}",
            f"QFrame#renameSheet{{background:#FFFFFF;border:1px solid rgba(0,0,0,23);border-radius:{c['radius']}px;}}")
        form=QVBoxLayout(self.card);form.setContentsMargins(20,20,20,20);form.setSpacing(16)
        header=QHBoxLayout();header.addWidget(label('重命名文件',14,600,parent=self.card),1)
        close=ToolbarButton('close',self.card,motion=shelf.manager.motion);close.setFixedSize(24,24);close.clicked.connect(self.reject);header.addWidget(close);form.addLayout(header)
        form.addWidget(label('输入新的文件名，扩展名保持不变。',12,400,'secondary',self.card))
        edit=shelf.rename_edit;edit.setParent(self.card);edit.setFixedHeight(34)
        css(edit,"QLineEdit{background:#242426;color:#F5F5F7;border:1px solid #3F3F46;border-radius:8px;padding:7px 10px;font:12px 'HarmonyOS Sans SC';} QLineEdit:focus{border-color:#0A84FF;}",
            "QLineEdit{background:#FFFFFF;color:#1D1D1F;border:1px solid #D4D4D8;border-radius:8px;padding:7px 10px;font:12px 'HarmonyOS Sans SC';} QLineEdit:focus{border-color:#0A84FF;}")
        form.addWidget(edit);actions=QHBoxLayout();actions.addStretch(1)
        cancel=button('取消',variant='secondary',parent=self.card);cancel.clicked.connect(self.reject)
        self.confirm=button('确定',variant='primary',parent=self.card);self.confirm.clicked.connect(self._commit)
        actions.addWidget(cancel);actions.addWidget(self.confirm);form.addLayout(actions);layout.addWidget(self.card)
        edit.editingFinished.disconnect(shelf.commit_rename)
        edit.returnPressed.connect(self.accept)
        self.rejected.connect(edit.hide)

    def _commit(self):
        self.shelf.commit_rename();self.accept()

    def paintEvent(self,event):
        from surface_morph import paint_surface_shadow
        p=QPainter(self);paint_surface_shadow(p,QRectF(self.card.geometry()),self._shadow_cache)
        p.end()


def item_detail(shelf,ref):
    content=shelf.content
    if content.is_text(ref):
        raw=content.text(ref) or ''
        return f'{len(raw)} 字符 · 原生文字' if content.category(ref)!='links' else '网页链接'
    try:size=Path(ref).stat().st_size
    except OSError:size=int(content.pending.get(ref,{}).get('size') or 0)
    amount=f'{size/1024/1024/1024:.1f} GB' if size>=1024**3 else f'{size/1024/1024:.1f} MB' if size>=1024**2 else f'{size/1024:.1f} KB' if size else ''
    ext=Path(content.title(ref)).suffix.lstrip('.').upper()
    names={'images':'图像','video':'视频','audio':'音频','text':'文字','files':'文件','links':'网页'}
    return ' · '.join(x for x in (amount,(ext+' '+names.get(content.category(ref),'文件')).strip()) if x)


STATUS_COLORS={'synced':'#30D158','uploading':'#0A84FF','pending':'#64D2FF','local':'#A1A1AA','paused':'#FF9F0A','error':'#FF453A'}


def draw_status(p,rect,status):
    p.setPen(QPen(ink(palette()['surface']),2))
    p.setBrush(QColor(STATUS_COLORS.get(status,'#A1A1AA')))
    p.drawEllipse(QRectF(rect))


def thumbnail(shelf,ref,size=40):
    pm=QPixmap(size*2,size*2);pm.setDevicePixelRatio(2);pm.fill(Qt.transparent)
    p=QPainter(pm);p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
    rect=QRectF(.5,.5,size-1,size-1);category=shelf.content.category(ref)
    backgrounds={'images':'#26364A','video':'#202344','audio':'#7E22CE','text':'#4A3D28','links':'#164354'}
    ext=Path(shelf.content.title(ref)).suffix.lower()
    bg=backgrounds.get(category,'#3B3B3F' if ext=='.zip' else '#593140' if ext=='.pdf' else '#263A55')
    p.setPen(QPen(QColor(255,255,255,38),1));p.setBrush(QColor(bg));p.drawRoundedRect(rect,size*.25,size*.25)
    cached=getattr(shelf,'preview_cache',{}).get(ref)
    actual=category=='images' or ext in ('.lnk','.exe','.app') or cached is not None and not cached.isNull()
    if actual:
        provider=getattr(shelf,'item_pixmap',shelf.content.item_pixmap)
        art=provider(ref,round(size),dpr=2)
        clip=QPainterPath();clip.addRoundedRect(rect,9,9);p.setClipPath(clip)
        p.drawPixmap(QRectF(1,1,size-2,size-2),art,QRectF(art.rect()));p.setClipping(False)
    elif category=='text':
        p.setPen(Qt.NoPen)
        for i in range(4):
            p.setBrush(QColor('#FBBF24' if i==0 else '#8F897B'));p.drawRoundedRect(QRectF(size*.23,size*.22+i*size*.18,size*(.5 if i!=2 else .36),size*.07),1,1)
    elif category=='audio':
        p.setPen(QPen(QColor('white'),2))
        for i,h in enumerate((.25,.48,.65,.4,.55,.24)):
            x=size*.22+i*size*.11;p.drawLine(QPointF(x,size*(.5-h/2)),QPointF(x,size*(.5+h/2)))
    elif category in ('video','links'):
        glyph='play' if category=='video' else 'web'
        if category=='video':
            p.setPen(Qt.NoPen);p.setBrush(QColor(255,255,255,42));p.drawEllipse(QRectF(size*.22,size*.22,size*.56,size*.56))
        icon(glyph,'#D8E1F4' if category=='video' else '#64D2FF').paint(p,QRect(round(size*.26),round(size*.26),round(size*.48),round(size*.48)))
    else:
        name=ext.lstrip('.').upper() or ('DIR' if Path(ref).is_dir() else 'FILE')
        color='#FF4567' if name=='PDF' else '#6A90BC' if name=='ZIP' else '#0A84FF'
        p.setPen(Qt.NoPen);p.setBrush(QColor(color));p.drawRoundedRect(QRectF(size*.12,size*.34,size*.76,size*.33),3,3)
        font=QFont();font.setPixelSize(max(8,round(size*.23)));font.setWeight(QFont.Bold);p.setFont(font);p.setPen(QColor('white'))
        p.drawText(QRectF(size*.1,size*.3,size*.8,size*.4),Qt.AlignCenter,name[:4])
    p.end();return pm


class ThumbnailLabel(TransferIconLabel):
    def __init__(self,shelf,ref,parent=None):
        super().__init__(shelf.content,ref,parent)
        self.shelf=shelf;self.ref=ref;self.setFixedSize(40,40)

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        art=thumbnail(self.shelf,self.ref,40)
        p.drawPixmap(0,0,art)
        draw_status(p,QRectF(29,29,10,10),self.cloud_status)
        p.end()


class MiniStack(StackTile):
    def __init__(self,shelf,parent=None,motion=True):
        super().__init__(parent,motion)
        self.shelf=shelf

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
        if not self.refs:
            r=QRectF((self.width()-176)/2,28,176,128)
            p.setPen(QPen(ink(palette()['border']),1,Qt.DashLine));p.setBrush(ink(palette()['card']));p.drawRoundedRect(r,16,16)
            p.setPen(QColor(palette()['muted']));f=p.font();f.setPixelSize(24);p.setFont(f);p.drawText(r.adjusted(0,-10,0,-30),Qt.AlignCenter,'+')
            f.setPixelSize(11);p.setFont(f);p.drawText(r.adjusted(0,34,0,0),Qt.AlignCenter,'点击选择文件');p.end();return
        c=palette();v=self.spring.values
        center=QPointF(self.width()/2,self.height()/2)
        p.translate(center.x(),center.y()+v['y']);p.scale(v['sx'],v['sy']);p.translate(-center)
        refs=list(reversed(self.refs));transforms=((0,1,0,1),(-7,.95,-2,.9),(-14,.9,2.2,.75),(-20,.85,-1.5,.58),(-26,.8,1,.4))
        for idx in reversed(range(len(refs))):
            ref=refs[idx];dy,scale,angle,opacity=transforms[idx]
            p.save();p.setOpacity(opacity);p.translate(center.x(),center.y()+dy);p.rotate(angle);p.scale(scale,scale);p.translate(-96,-64)
            if idx==0:p.translate(0,-18*(1-self.arrival))
            p.setPen(QPen(QColor(255,255,255,38) if ui_theme.current_theme()=='dark' else QColor(0,0,0,26),1))
            p.setBrush(QColor('#242426' if ui_theme.current_theme()=='dark' else '#FFFFFF'));p.drawRoundedRect(QRectF(.5,.5,191,127),16,16)
            p.drawPixmap(14,14,thumbnail(self.shelf,ref,40))
            draw_status(p,QRectF(43,43,10,10),getattr(self.shelf.manager,'sync',None).sync_status_for_ref(ref) if hasattr(getattr(self.shelf.manager,'sync',None),'sync_status_for_ref') else 'local')
            title=self.shelf.content.title(ref);f=QFont(self.font());f.setPixelSize(12);f.setWeight(QFont.DemiBold);p.setFont(f);p.setPen(QColor(c['text']))
            p.drawText(QRectF(14,77,164,20),Qt.AlignVCenter,p.fontMetrics().elidedText(title,Qt.ElideRight,164))
            f.setPixelSize(10);f.setWeight(QFont.Normal);p.setFont(f);p.setPen(QColor(c['secondary']));p.drawText(QRectF(14,99,164,16),Qt.AlignVCenter,item_detail(self.shelf,ref).split(' · ')[0])
            ext=Path(title).suffix.lstrip('.').upper() or ('TEXT' if self.shelf.content.is_text(ref) else 'FILE')
            p.drawText(QRectF(100,14,78,18),Qt.AlignRight|Qt.AlignVCenter,ext[:7])
            state=getattr(self.repository,'transfers',{}).get(ref)
            if idx==0 and state:
                from transfer_visuals import draw_progress_badge
                draw_progress_badge(p,QRectF(162,40,16,16),state)
            p.restore()
        p.end()


def build_overview(view,preview_type):
    """Build the HTML's 52px toolbar and 210 / flexible / 340 split view."""
    view.setObjectName('referenceOverview')
    css(view,'QFrame#referenceOverview{background:#1C1C1E;border:0;}','QFrame#referenceOverview{background:#FFFFFF;border:0;}')
    root=QVBoxLayout(view);root.setContentsMargins(0,0,0,0);root.setSpacing(0)
    view.header=QFrame(view);view.header.setFixedHeight(53)
    css(view.header,'QFrame{background:rgba(255,255,255,3);border:0;border-bottom:1px solid rgba(255,255,255,18);}',
        'QFrame{background:rgba(0,0,0,3);border:0;border-bottom:1px solid rgba(0,0,0,18);}')
    head=QHBoxLayout(view.header);head.setContentsMargins(16,10,16,10);head.setSpacing(9)
    view.title=label('内容总览',15,600,parent=view.header);head.addWidget(view.title)
    view.count_label=label('',11,600,'secondary',view.header);view.count_label.setAlignment(Qt.AlignCenter)
    css(view.count_label,"color:#D4D4D8;background:rgba(255,255,255,28);border:0;border-radius:10px;padding:1px 9px;font:600 11px 'HarmonyOS Sans SC';",
        "color:#515154;background:rgba(0,0,0,18);border:0;border-radius:10px;padding:1px 9px;font:600 11px 'HarmonyOS Sans SC';")
    view.count_label.setFixedHeight(20);head.addWidget(view.count_label)
    view.cloud_selector=CloudSelector(view.shelf,view.header)
    view.cloud_selector.clicked.connect(view.shelf.show_cloud_menu);head.addWidget(view.cloud_selector)
    head.addStretch(1)
    view.search_edit=QLineEdit(view.header);view.search_edit.setPlaceholderText('搜索...');view.search_edit.setFixedSize(176,30)
    view.search_edit.setAccessibleName('搜索搁架内容');view.search_edit.textChanged.connect(lambda _:view.rebuild())
    css(view.search_edit,"QLineEdit{background:#28282A;color:#F5F5F7;border:1px solid #3B3B3F;border-radius:8px;padding:4px 10px;font:12px 'HarmonyOS Sans SC';}",
        "QLineEdit{background:#F0F0F2;color:#1D1D1F;border:1px solid #D4D4D8;border-radius:8px;padding:4px 10px;font:12px 'HarmonyOS Sans SC';}")
    head.addWidget(view.search_edit)
    view.view_segment=QFrame(view.header);view.view_segment.setFixedSize(134,32)
    css(view.view_segment,'QFrame{background:rgba(255,255,255,10);border:1px solid rgba(255,255,255,18);border-radius:9px;}',
        'QFrame{background:rgba(0,0,0,10);border:1px solid rgba(0,0,0,18);border-radius:9px;}')
    segment=QHBoxLayout(view.view_segment);segment.setContentsMargins(3,3,3,3);segment.setSpacing(0)
    view.view_list=button('列表','list',parent=view.view_segment);view.view_grid=button('图标','grid',parent=view.view_segment)
    for control,key in ((view.view_list,'list'),(view.view_grid,'icons')):
        control.setFixedHeight(26);control.setCheckable(True)
        css(control,"QPushButton{background:transparent;border:0;border-radius:6px;color:#A1A1AA;padding:3px 8px;font:12px 'HarmonyOS Sans SC';} QPushButton:checked{background:rgba(255,255,255,31);color:#F5F5F7;font-weight:700;}",
            "QPushButton{background:transparent;border:0;border-radius:6px;color:#86868B;padding:3px 8px;font:12px 'HarmonyOS Sans SC';} QPushButton:checked{background:#FFFFFF;color:#1D1D1F;font-weight:700;}")
        control.clicked.connect(lambda checked=False,key=key:view.change_view_mode(key));segment.addWidget(control)
        control.setChecked(view.shelf.manager.settings.view_mode==key)
    head.addWidget(view.view_segment)
    head.addStretch(1)
    view.zip_button=button('打包所选',variant='secondary',parent=view.header);view.zip_button.clicked.connect(view.compress_selected);head.addWidget(view.zip_button)
    view.zip_all_button=button('打包全部',variant='primary',parent=view.header);view.zip_all_button.clicked.connect(lambda:view.shelf.compress_refs(list(view.shelf.files['permanent'])));head.addWidget(view.zip_all_button)
    view.clear_button=button('清空全部',variant='danger',parent=view.header);view.clear_button.clicked.connect(view.shelf.confirm_clear_all);head.addWidget(view.clear_button)
    head.addSpacing(100)
    root.addWidget(view.header)
    body=QHBoxLayout();body.setContentsMargins(0,0,0,0);body.setSpacing(0);view.body_layout=body
    view.sidebar=QFrame(view);view.sidebar.setFixedWidth(210)
    css(view.sidebar,'QFrame{background:#151517;border:0;border-right:1px solid rgba(255,255,255,15);}',
        'QFrame{background:#F2F4F8;border:0;border-right:1px solid rgba(0,0,0,15);}')
    side=QVBoxLayout(view.sidebar);side.setContentsMargins(10,10,10,10);side.setSpacing(2)
    view.category_group=QButtonGroup(view);view.category_group.setExclusive(True);view.category_buttons={};view.sidebar_buttons={};view.nav_counts={}
    glyphs={'all':'all','images':'image','video':'video','audio':'audio','text':'text','files':'file','links':'web','uploading':'transferring'}
    for key,title in view.CATEGORIES:
        control=button(title,glyphs[key],parent=view.sidebar);control.setCheckable(True);control.setFixedHeight(30)
        css(control,"QPushButton{background:transparent;color:#A1A1AA;border:0;border-radius:12px;text-align:left;padding:6px 12px;font:500 12px 'HarmonyOS Sans SC';} QPushButton:hover{background:rgba(255,255,255,15);} QPushButton:checked{background:#0A84FF;color:white;font-weight:600;}",
            "QPushButton{background:transparent;color:#515154;border:0;border-radius:12px;text-align:left;padding:6px 12px;font:500 12px 'HarmonyOS Sans SC';} QPushButton:hover{background:rgba(0,0,0,12);} QPushButton:checked{background:#0A84FF;color:white;font-weight:600;}")
        control.clicked.connect(lambda checked=False,key=key:view.set_category(key));view.category_group.addButton(control);side.addWidget(control)
        count=label('',11,400,'muted',control);count.setAlignment(Qt.AlignRight|Qt.AlignVCenter);count.setGeometry(145,5,28,20);count.setAttribute(Qt.WA_TransparentForMouseEvents)
        view.nav_counts[key]=count;view.category_buttons[key]=control;view.sidebar_buttons[key]=control
    view.transfer_badge=view.nav_counts['uploading']
    side.addStretch(1)
    view.lan_nav=button('局域网互传','nearby','secondary',view.sidebar);view.lan_nav.setFixedHeight(34);view.lan_nav.clicked.connect(lambda:view.shelf.manager.show_lan_dialog());side.addWidget(view.lan_nav)
    view.settings_nav=button('设置','settings',parent=view.sidebar);view.settings_nav.setFixedHeight(34);view.settings_nav.clicked.connect(lambda:view.shelf.manager.show_settings());side.addWidget(view.settings_nav)
    for control in (view.lan_nav,view.settings_nav):
        old=control._reference_style
        css(control,*[s.replace('padding:4px 11px;','padding:4px 12px;text-align:left;') for s in old])
    view.account_nav=ProfileButton(view.sidebar);view.account_nav.clicked.connect(lambda:view.shelf.manager.show_settings());side.addWidget(view.account_nav)
    body.addWidget(view.sidebar)
    view.middle=QWidget(view);mid=QVBoxLayout(view.middle);view.middle_layout=mid;mid.setContentsMargins(0,0,0,0);mid.setSpacing(0)
    view.selection_bar=QFrame(view.middle);view.selection_bar.setFixedHeight(33)
    css(view.selection_bar,'QFrame{border:0;border-bottom:1px solid rgba(255,255,255,10);background:transparent;}',
        'QFrame{border:0;border-bottom:1px solid rgba(0,0,0,10);background:transparent;}')
    select=QHBoxLayout(view.selection_bar);select.setContentsMargins(16,0,12,0);select.setSpacing(8)
    view.selection_hint=label('',11,400,'secondary',view.selection_bar);select.addWidget(view.selection_hint,1)
    view.select_all_button=button('全选',parent=view.selection_bar);view.select_all_button.clicked.connect(view.toggle_select_all_visible);select.addWidget(view.select_all_button)
    view.paste_button=button('粘贴',parent=view.selection_bar);view.paste_button.clicked.connect(lambda:view.shelf.paste_clipboard('permanent'));select.addWidget(view.paste_button)
    mid.addWidget(view.selection_bar)
    view.transfer_summary=QWidget(view.middle);summary=QHBoxLayout(view.transfer_summary);summary.setContentsMargins(16,10,16,10)
    view.transfer_count=label('',12,600,parent=view.transfer_summary);view.transfer_eta=label('',11,400,'cyan',view.transfer_summary)
    view.transfer_bar=QProgressBar(view.transfer_summary);view.transfer_bar.setTextVisible(False);view.transfer_bar.setFixedHeight(6)
    css(view.transfer_bar,'QProgressBar{border:0;background:#343438;border-radius:3px;} QProgressBar::chunk{background:#0A84FF;border-radius:3px;}',
        'QProgressBar{border:0;background:#DDE4ED;border-radius:3px;} QProgressBar::chunk{background:#0A84FF;border-radius:3px;}')
    summary.addWidget(view.transfer_count);summary.addWidget(view.transfer_bar,1);summary.addWidget(view.transfer_eta);mid.addWidget(view.transfer_summary);view.transfer_summary.hide()
    view.scroll=QScrollArea(view.middle);view.scroll.setWidgetResizable(True);view.scroll.setFrameShape(QFrame.NoFrame);view.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    css(view.scroll,'QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::handle:vertical{background:#49494E;border-radius:3px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}',
        'QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::handle:vertical{background:#CBCBD0;border-radius:3px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}')
    view.rows_body=QWidget();view.rows_layout=QVBoxLayout(view.rows_body);view.rows_layout.setContentsMargins(12,12,12,12);view.rows_layout.setSpacing(6);view.scroll.setWidget(view.rows_body)
    # QScrollArea enables autoFillBackground on setWidget; its inherited
    # transparent palette otherwise clears a hole in transition snapshots.
    view.rows_body.setAutoFillBackground(False);mid.addWidget(view.scroll,1)
    view.empty_state=QWidget(view.middle);empty=QVBoxLayout(view.empty_state);empty.addStretch(1)
    view.empty_symbol=label('⌑',36,400,'muted',view.empty_state);view.empty_title=label('搁架为空',12,500,'secondary',view.empty_state);view.empty_hint=label('拖入文件、图片、文字或链接',11,400,'muted',view.empty_state)
    for control in (view.empty_symbol,view.empty_title,view.empty_hint):control.setAlignment(Qt.AlignCenter);empty.addWidget(control)
    empty.addStretch(1);mid.addWidget(view.empty_state,1);view.empty_state.hide()
    view.transfer_strip=QFrame(view.middle)
    css(view.transfer_strip,'QFrame{background:#121213;border:0;border-top:1px solid rgba(255,255,255,15);}',
        'QFrame{background:#F2F4F8;border:0;border-top:1px solid rgba(0,0,0,15);}')
    strip=QVBoxLayout(view.transfer_strip);strip.setContentsMargins(14,7,14,7);strip.setSpacing(4)
    top=QHBoxLayout();view.strip_title=label('传输中',11,500,'secondary',view.transfer_strip);view.strip_more=button('',parent=view.transfer_strip);view.strip_more.clicked.connect(lambda:view.set_category('uploading'))
    top.addWidget(view.strip_title,1);top.addWidget(view.strip_more);strip.addLayout(top)
    view.strip_rows=[];view._strip_refs=[]
    for index in range(2):
        row=QWidget(view.transfer_strip);line=QHBoxLayout(row);line.setContentsMargins(0,0,0,0);line.setSpacing(7)
        name=button('',parent=row);name.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed);name.clicked.connect(lambda checked=False,index=index:view._open_strip_transfer(index))
        percent=label('',10,400,'cyan',row);progress=QProgressBar(row);progress.setFixedSize(42,3);progress.setTextVisible(False)
        css(progress,'QProgressBar{background:#343438;border:0;} QProgressBar::chunk{background:#64D2FF;}',
            'QProgressBar{background:#DDE4ED;border:0;} QProgressBar::chunk{background:#0A84FF;}')
        line.addWidget(name,1);line.addWidget(percent);line.addWidget(progress);strip.addWidget(row);view.strip_rows.append((row,name,percent,progress))
    mid.addWidget(view.transfer_strip);view.transfer_strip.hide();body.addWidget(view.middle,1)
    view.preview=preview_type(view);view.preview.setFixedWidth(340);body.addWidget(view.preview)
    root.addLayout(body,1)
    # Compatibility members retain original navigation/selection code paths.
    view.category_segments=QWidget(view);view.category_segments.hide()
    view.category_picker=button('全部',parent=view.selection_bar);select.insertWidget(0,view.category_picker);view.category_picker.clicked.connect(view.open_category_menu);view.category_picker.hide()
    view.bulk_more=button('···',parent=view.header);head.insertWidget(head.count()-1,view.bulk_more);view.bulk_more.hide();view.bulk_more.clicked.connect(view.open_bulk_menu)
    view.narrow_view_button=button('预览',parent=view.selection_bar);select.addWidget(view.narrow_view_button);view.narrow_view_button.hide();view.narrow_view_button.clicked.connect(view.toggle_narrow_preview)
    view._narrow_layout=False;view._narrow_preview=False


def build_preview(view,elided_type,image_type,media_type,content_type):
    from transfer_visuals import CloudStatusBadge, TransferOverlay
    view.setObjectName('referencePreview')
    css(view,'QFrame#referencePreview{background:#121213;border:0;border-left:1px solid rgba(255,255,255,15);}',
        'QFrame#referencePreview{background:#F5F6FA;border:0;border-left:1px solid rgba(0,0,0,15);}')
    root=QVBoxLayout(view);root.setContentsMargins(0,0,0,0);root.setSpacing(0)
    head=QFrame(view);head.setFixedHeight(62)
    css(head,'QFrame{border:0;border-bottom:1px solid rgba(255,255,255,15);background:transparent;}',
        'QFrame{border:0;border-bottom:1px solid rgba(0,0,0,15);background:transparent;}')
    header=QHBoxLayout(head);header.setContentsMargins(16,10,16,10);header.setSpacing(8)
    names=QVBoxLayout();names.setSpacing(2)
    view.name=elided_type(view);view.info=elided_type(view)
    for item,size,weight,tint in ((view.name,13,600,'text'),(view.info,11,400,'secondary')):
        css(item,f"color:{colors('dark')[tint]};border:0;background:transparent;font:{weight} {size}px 'HarmonyOS Sans SC';",
            f"color:{colors('light')[tint]};border:0;background:transparent;font:{weight} {size}px 'HarmonyOS Sans SC';")
        names.addWidget(item)
    header.addLayout(names,1);view.cloud_badge=CloudStatusBadge(view);view.cloud_badge.setFixedSize(24,24)
    header.addWidget(view.cloud_badge);view.copy_button=ToolbarButton('copy',view,motion=view.shelf.manager.motion);view.copy_button.setFixedSize(44,26);view.copy_button.clicked.connect(view.copy_current);header.addWidget(view.copy_button);root.addWidget(head)
    content=QWidget(view);body=QVBoxLayout(content);body.setContentsMargins(16,16,16,16);body.setSpacing(12)
    view.transfer_row=QWidget(content);transfer=QHBoxLayout(view.transfer_row);transfer.setContentsMargins(0,0,0,0)
    view.transfer_progress=QProgressBar(view.transfer_row);view.transfer_progress.setTextVisible(False);view.transfer_progress.setFixedHeight(5)
    css(view.transfer_progress,'QProgressBar{background:#333336;border:0;} QProgressBar::chunk{background:#0A84FF;}',
        'QProgressBar{background:#E0E0E5;border:0;} QProgressBar::chunk{background:#0A84FF;}')
    view.transfer_meta=label('',10,400,'cyan',view.transfer_row);view.cancel_transfer=button('取消上传',parent=view.transfer_row);view.cancel_transfer.clicked.connect(lambda:view.shelf.cancel_transfer(view.current_path))
    transfer.addWidget(view.transfer_progress,1);transfer.addWidget(view.transfer_meta);transfer.addWidget(view.cancel_transfer);body.addWidget(view.transfer_row);view.transfer_row.hide()
    view.stack=QStackedWidget(content);view.stack.setFixedHeight(176)
    css(view.stack,'QStackedWidget{background:#242426;border:1px solid #414145;border-radius:12px;}',
        'QStackedWidget{background:#FFFFFF;border:1px solid #DDDEE2;border-radius:12px;}')
    view.icon=content_type(view,motion=view.shelf.manager.motion)
    view.text=QPlainTextEdit(view);view.text.setReadOnly(True);view.text.setAcceptDrops(False);view.text.viewport().setAcceptDrops(False)
    css(view.text,"QPlainTextEdit{background:rgba(245,158,11,10);color:#FDE68A;border:0;border-radius:12px;padding:14px;font:12px 'HarmonyOS Sans SC';}",
        "QPlainTextEdit{background:#FFF9E8;color:#5C4821;border:0;border-radius:12px;padding:14px;font:12px 'HarmonyOS Sans SC';}")
    view.image=image_type(view);view.image.set_loader(view.shelf.image_cache);view.image.source_ready.connect(view._image_source_ready)
    view.media=media_type(view,motion=view.shelf.manager.motion);view.media.frame_ready.connect(view._poster_ready);view.media.playing_changed.connect(view._playing_changed)
    for item in (view.icon,view.text,view.image,view.media):view.stack.addWidget(item)
    body.addWidget(view.stack)
    status=QFrame(content);status.setObjectName('referenceStatus')
    css(status,'QFrame#referenceStatus{background:rgba(255,255,255,7);border:1px solid rgba(255,255,255,15);border-radius:12px;}',
        'QFrame#referenceStatus{background:rgba(255,255,255,184);border:1px solid rgba(0,0,0,15);border-radius:12px;}')
    box=QVBoxLayout(status);box.setContentsMargins(12,10,12,10);box.setSpacing(7)
    line=QHBoxLayout();line.addWidget(label('状态',11,400,'muted',status));line.addStretch(1);view.status_value=label('',11,500,'green',status);line.addWidget(view.status_value);box.addLayout(line)
    view.path_label=elided_type(status);view.path_label.setAccessibleName('内容来源路径')
    css(view.path_label,"color:#A1A1AA;background:transparent;border:0;font:10px 'Consolas';",
        "color:#636366;background:transparent;border:0;font:10px 'Consolas';")
    box.addWidget(view.path_label);body.addWidget(status);body.addStretch(1)
    actions=QHBoxLayout();actions.setSpacing(8)
    view.open_button=button('打开',variant='secondary',parent=content);view.rename_button=button('重命名文件',variant='secondary',parent=content)
    view.open_button.clicked.connect(lambda:view.current_path and view.shelf.open_path(view.current_path));view.rename_button.clicked.connect(lambda:view.current_path and view.shelf.rename_ref(view.current_path))
    for item in (view.open_button,view.rename_button):item.setFixedHeight(32);actions.addWidget(item,1)
    body.addLayout(actions);root.addWidget(content,1)
    view.transfer_overlay=TransferOverlay(view.stack,lambda:view.shelf.content.transfers.get(view.current_path))
    view.group_view=QFrame(view);view.group_view.setObjectName('referenceGroupPreview')
    css(view.group_view,'QFrame#referenceGroupPreview{background:#121213;border:0;}',
        'QFrame#referenceGroupPreview{background:#F5F6FA;border:0;}')
    group=QVBoxLayout(view.group_view);group.setContentsMargins(20,20,20,20);group.setSpacing(16);group.addStretch(1)
    view.group_art=QLabel(view.group_view);view.group_art.setFixedSize(96,80);view.group_art.setAlignment(Qt.AlignCenter);group.addWidget(view.group_art,0,Qt.AlignCenter)
    view.group_title=label('',14,600,parent=view.group_view);view.group_title.setAlignment(Qt.AlignCenter);group.addWidget(view.group_title)
    view.group_copy=button('复制所选',variant='secondary',parent=view.group_view);view.group_copy.setFixedHeight(34);view.group_copy.clicked.connect(view.copy_current);group.addWidget(view.group_copy)
    view.group_zip=button('打包为 ZIP...',variant='primary',parent=view.group_view);view.group_zip.setFixedHeight(34);view.group_zip.clicked.connect(view.popup.compress_selected);group.addWidget(view.group_zip);group.addStretch(1)
    view.group_view.hide()

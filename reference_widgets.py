"""Qt artwork and controls transcribed from the supplied FlickDock HTML.

Only presentation lives here. Content, drag MIME, disk work and network services
continue to be owned by the existing shelf and panel classes.
"""
from pathlib import Path
import sys
import re

from PySide6.QtCore import QObject, QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QIcon, QPalette, QImage, QPainter, QPainterPath, QPen, QPixmap, QLinearGradient
from PySide6.QtWidgets import (QLabel, QPushButton, QWidget, QFrame, QHBoxLayout,
    QVBoxLayout, QGridLayout, QScrollArea, QLineEdit, QProgressBar, QButtonGroup, QSizePolicy,
    QStackedWidget, QPlainTextEdit, QCheckBox, QApplication, QDialog, QRubberBand)

import ui_theme
from native_ui_common import colors, icon, keyboard_focus_visible, readable_qss, SEGMENT_HEIGHT, SEGMENT_INSET, SEGMENT_BUTTON_HEIGHT
from content_stack import StackTile
from flickdock_motion import FeedbackButton
from transfer_visuals import TransferIconLabel, CLOUD_STATUS_LABELS
from lan_panel import _DropZone


class OverviewEmptyDropZone(_DropZone):
    """Use the same unscaled rounded dash path as the transfer drop target."""
    def paintEvent(self, event):
        self.illustration.dark=ui_theme.current_theme()=='dark'
        super().paintEvent(event)


def palette():
    return colors(ui_theme.current_theme())


def ink(value):
    if str(value).startswith('rgba('):
        return QColor(*[int(float(x)) for x in re.findall(r'[\d.]+',str(value))])
    return QColor(value)


def css(widget, dark, light=None):
    # Resolve the source's real 400/500/600/700 weights in the selected OS face.
    if QApplication.instance():
        family=QApplication.font().family()
        dark=dark.replace("'HarmonyOS Sans SC'",f"'{family}'")
        if light is not None:light=light.replace("'HarmonyOS Sans SC'",f"'{family}'")
    widget._reference_style=(readable_qss(dark), readable_qss(light if light is not None else dark))
    resolved=widget._reference_style[ui_theme.current_theme()=='light']
    if widget.styleSheet()!=resolved:widget.setStyleSheet(resolved)


def refresh_tree(widget):
    for child in [widget, *widget.findChildren(QWidget)]:
        source=getattr(child,'_reference_style',None)
        if source and child.styleSheet()!=source[ui_theme.current_theme()=='light']:
            child.setStyleSheet(source[ui_theme.current_theme()=='light'])
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


class IconTextButton(QPushButton):
    """Use one logical icon/text group on both Qt platform styles."""
    def _retarget_icon(self,*,hover=None,pressed=None):
        target=getattr(self,'_icon_target',QPointF())
        target=QPointF(target.x() if hover is None else float(hover),target.y() if pressed is None else float(pressed))
        self._icon_target=target
        from ui_transitions import motion_allowed,exit_curve
        if not self.isEnabled() or not motion_allowed():
            if hasattr(self,'_icon_animation'):self._icon_animation.stop()
            self._icon_value=target if self.isEnabled() else QPointF();self.update();return
        current=getattr(self,'_icon_value',QPointF())
        if current==target:return
        if not hasattr(self,'_icon_animation'):
            self._icon_animation=QVariantAnimation(self)
            self._icon_animation.setEasingCurve(exit_curve())
            def changed(value):self._icon_value=value;self.update()
            self._icon_animation.valueChanged.connect(changed)
        self._icon_animation.stop();self._icon_animation.setDuration(100 if target.y() else 170)
        self._icon_animation.setStartValue(current);self._icon_animation.setEndValue(target);self._icon_animation.start()

    def enterEvent(self,event):self._retarget_icon(hover=1);super().enterEvent(event)
    def leaveEvent(self,event):self._retarget_icon(hover=0);super().leaveEvent(event)
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton and self.isEnabled():self._retarget_icon(pressed=1)
        super().mousePressEvent(event)
    def mouseReleaseEvent(self,event):self._retarget_icon(pressed=0);super().mouseReleaseEvent(event)
    def hideEvent(self,event):
        if hasattr(self,'_icon_animation'):self._icon_animation.stop()
        self._icon_target=self._icon_value=QPointF();super().hideEvent(event)
    def changeEvent(self,event):
        if event.type()==QEvent.EnabledChange and not self.isEnabled():
            if hasattr(self,'_icon_animation'):self._icon_animation.stop()
            self._icon_target=self._icon_value=QPointF()
        super().changeEvent(event)

    def hitButton(self,position):return self.rect().contains(position)
    def paintEvent(self,event):
        if self.icon().isNull():return super().paintEvent(event)
        from PySide6.QtWidgets import QStyleOptionButton, QStyle, QStylePainter
        option=QStyleOptionButton();self.initStyleOption(option)
        art,words=QIcon(option.icon),str(option.text);option.icon=QIcon();option.text=''
        painter=QStylePainter(self);painter.drawControl(QStyle.CE_PushButton,option)
        painter.setFont(self.font())
        if not words:
            size=self.iconSize();rect=QRect((self.width()-size.width())//2,(self.height()-size.height())//2,size.width(),size.height())
            value=getattr(self,'_icon_value',QPointF());glyph=getattr(self,'_reference_icon',('',))[0]
            painter.save();center=QRectF(rect).center();painter.translate(center)
            painter.scale(1-.05*value.y(),1-.05*value.y())
            if glyph in ('settings','refresh','trash'):painter.rotate(value.x()*{'settings':10,'refresh':28,'trash':-5}[glyph])
            elif glyph in ('file','package','package-selected','cloud'):painter.translate(0,-value.x())
            elif glyph in ('nearby','web'):painter.translate(value.x(),0)
            elif glyph in ('expand','collapse'):painter.rotate(value.x()*(6 if glyph=='expand' else -6))
            painter.translate(-center)
            art.paint(painter,rect,Qt.AlignCenter,QIcon.Normal if self.isEnabled() else QIcon.Disabled)
            painter.restore();return
        size=self.iconSize();gap=6;left=bool(self.property('leftAligned'))
        space=self.width()-(56 if left else 16)-size.width()-gap
        text=painter.fontMetrics().elidedText(words,Qt.ElideRight,max(1,space))
        width=painter.fontMetrics().horizontalAdvance(text)+size.width()+gap
        x=12 if left else (self.width()-width)/2
        mode=QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        art.paint(painter,QRect(round(x),round((self.height()-size.height())/2),size.width(),size.height()),Qt.AlignCenter,mode)
        c=palette();variant=self.property('referenceVariant')
        tint=('#FFFFFF' if self.isChecked() and left else c['text'] if self.isChecked() else
              '#FFFFFF' if variant=='primary' else c['red'] if variant=='danger' else c['text'] if variant=='secondary' else c['secondary'])
        painter.setPen(QColor(tint if self.isEnabled() else c['muted']))
        painter.drawText(QRectF(x+size.width()+gap,0,self.width()-x-size.width()-gap-8,self.height()),Qt.AlignLeft|Qt.AlignVCenter,text)


class SortButton(IconTextButton):
    """Stable text baseline with independently tinted vector direction arrows."""
    direction=0
    def set_direction(self,direction):
        if direction==self.direction:return
        self.direction=direction
        from ui_transitions import motion_allowed,exit_curve
        target=QPointF(float(direction==1),float(direction==2))
        if not motion_allowed() or not self.isVisible():
            if hasattr(self,'_sort_animation'):self._sort_animation.stop()
            self._sort_value=target;self.update();return
        if not hasattr(self,'_sort_animation'):
            self._sort_animation=QVariantAnimation(self);self._sort_animation.setDuration(140)
            self._sort_animation.setEasingCurve(exit_curve())
            def changed(value):self._sort_value=value;self.update()
            self._sort_animation.valueChanged.connect(changed)
        self._sort_animation.stop();self._sort_animation.setStartValue(getattr(self,'_sort_value',QPointF()))
        self._sort_animation.setEndValue(target);self._sort_animation.start()
    def hideEvent(self,event):
        if hasattr(self,'_sort_animation'):self._sort_animation.stop()
        self._sort_value=QPointF(float(self.direction==1),float(self.direction==2));super().hideEvent(event)
    def arrow_colors(self):
        c=palette()
        return tuple(c['link'] if self.direction==value else c['muted'] for value in (1,2))
    def paintEvent(self,event):
        from PySide6.QtWidgets import QStyleOptionButton,QStyle,QStylePainter
        from PySide6.QtSvg import QSvgRenderer
        from PySide6.QtCore import QByteArray
        option=QStyleOptionButton();self.initStyleOption(option);option.text='';option.icon=QIcon()
        p=QStylePainter(self);p.drawControl(QStyle.CE_PushButton,option);p.setFont(self.font())
        text_width=p.fontMetrics().horizontalAdvance(self.text());x=(self.width()-text_width-10)/2
        p.setPen(QColor(palette()['secondary']));p.drawText(QRectF(x,0,text_width,self.height()),Qt.AlignLeft|Qt.AlignVCenter,self.text())
        from native_ui_common import cached_svg_renderer
        values=getattr(self,'_sort_value',QPointF(float(self.direction==1),float(self.direction==2)))
        for index,path in enumerate(('M0 4 4 0 8 4Z','M0 6 4 10 8 6Z')):
            rect=QRectF(x+text_width+2,(self.height()-10)/2,8,10)
            for tint,opacity in ((palette()['muted'],1),(palette()['link'],values.x() if index==0 else values.y())):
                if opacity<=0:continue
                svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 10"><path fill="{tint}" d="{path}"/></svg>'
                p.save();p.setOpacity(opacity);cached_svg_renderer(svg).render(p,rect);p.restore()


def button(value='',glyph='',variant='quiet',parent=None,*,control_type=IconTextButton):
    widget=control_type(value,parent)
    widget.setProperty('referenceVariant',variant)
    widget.setCursor(Qt.PointingHandCursor)
    widget.setFocusPolicy(Qt.StrongFocus)
    widget.setFixedHeight(32)
    widget.setIconSize(QSize(16,16))
    sources=[]
    for theme in ('dark','light'):
        c=colors(theme)
        background=c['control'] if variant=='secondary' else c['action'] if variant=='primary' else 'rgba(255,69,58,38)' if variant=='danger' else 'transparent'
        ink='white' if variant=='primary' else c['red'] if variant=='danger' else c['secondary'] if variant=='quiet' else c['text']
        border='rgba(255,69,58,76)' if variant=='danger' else c['border'] if variant=='secondary' else 'transparent'
        hover=c['action'] if variant=='primary' else c['hover']
        pressed=c['action'] if variant=='primary' else c['selected']
        sources.append(f"QPushButton{{background:{background};color:{ink};border:1px solid {border};border-radius:8px;padding:4px 11px;font:500 12px 'HarmonyOS Sans SC';}} QPushButton:hover{{background:{hover};}} QPushButton:pressed{{background:{pressed};}} QPushButton:checked{{background:{c['hover']};color:{c['text']};}} QPushButton:disabled{{color:{c['muted']};background:{c['control']};border-color:transparent;}} QPushButton[keyboardFocus=\"true\"]{{border:1px solid {c['link']};}}")
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
        if self.property('framed'):
            p.setPen(QPen(ink(c['border']),1));p.setBrush(ink(c['control']))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),8,8)
        if self.isChecked() or v['hover']>.01 or v['down']>.01:
            fill=ink(c['selected'] if self.isChecked() else c['hover'])
            if not self.isChecked():fill.setAlphaF(fill.alphaF()*min(1,v['hover']+v['down']))
            p.setPen(Qt.NoPen);p.setBrush(fill);p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),8,8)
        if not self.isEnabled():p.setOpacity(.4)
        name={'pin-filled':'pin','check':'check'}.get(self.glyph,self.glyph)
        tint=self._glyph_tint or (c['blue'] if self.isChecked() else c['secondary'])
        p.save();p.setOpacity(p.opacity()*getattr(self,'_entry_alpha',1.));p.translate(getattr(self,'_entry_dx',0.),0.)
        hover=v['hover']
        if name=='cloud':p.translate(0,-hover)
        elif name in ('expand','collapse'):
            p.translate(center);p.rotate(6*hover if name=='expand' else -6*hover);p.translate(-center)
        elif name=='dock':p.translate(hover,0)
        icon(name,tint).paint(p,QRect((self.width()-16)//2,(self.height()-16)//2,16,16))
        p.restore()
        if self.hasFocus() and self._keyboard_focus:
            p.setPen(QPen(QColor(c['blue']),1.5));p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),7,7)
        p.end()


class CloudSelector(QPushButton):
    def __init__(self,shelf,parent=None):
        super().__init__(parent);self.shelf=shelf
        self.setFixedSize(32,32);self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName('云同步方向控制')

    def hitButton(self,position):return self.rect().contains(position)

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);c=palette()
        enabled=self.shelf.manager.settings.sync_upload_enabled and self.shelf.manager.settings.sync_receive_enabled
        if self.underMouse():
            p.setBrush(ink(c['hover']));p.setPen(Qt.NoPen);p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),8,8)
        icon('cloud',c['green'] if enabled else c['red']).paint(p,QRect(8,8,16,16))
        if keyboard_focus_visible(self):
            p.setPen(QPen(QColor(c['link']),1.5));p.setBrush(Qt.NoBrush);p.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),12,12)
        p.end()


class NetworkStatus(QWidget):
    """Cloud reachability, independent of login and sync-direction switches."""
    def __init__(self,manager,parent=None):
        super().__init__(parent);self.setFixedSize(32,32);self.sx=self.sy=1
        self.setAttribute(Qt.WA_AlwaysShowToolTips,True)
        self.environment=dict(getattr(getattr(getattr(manager,'connectivity',None),'environment',None),'state',{}))
        self.set_state(getattr(getattr(manager,'connectivity',None),'state','checking'))

    def set_state(self,state):
        self.state=state
        message={'online':'云端网络畅通','offline':'云端网络不可达，暂时无法读取云端文件','checking':'正在检查云端网络…'}.get(state,'正在检查云端网络…')
        self.setToolTip(message);self.setAccessibleName(message);self.update()

    def set_environment(self,state):
        self.environment=dict(state);self.set_state(self.state)

    def set_presentation_scale(self,sx,sy):self.sx,self.sy=sx,sy;self.update()

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing);painter.translate(self.width()/2,self.height()/2);painter.scale(self.sx,self.sy)
        c=palette();tint=c['green'] if self.state=='online' else c['red'] if self.state=='offline' else c['muted']
        icon('web',tint).paint(painter,QRect(-8,-8,16,16))
        painter.end()


class LanButton(ToolbarButton):
    """One footer target for the LAN symbol and caption."""
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);c=palette()
        if self.underMouse() or self.property('transferDropActive'):
            p.setPen(Qt.NoPen);p.setBrush(ink(c['hover']));p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),8,8)
        icon('nearby',c['link']).paint(p,QRect(7,(self.height()-16)//2,16,16))
        f=self.font();f.setPixelSize(13 if sys.platform=='win32' else 12);p.setFont(f);p.setPen(QColor(c['text']))
        p.drawText(QRectF(29,0,self.width()-33,self.height()),Qt.AlignLeft|Qt.AlignVCenter,'文件互传')
        if keyboard_focus_visible(self):
            p.setPen(QPen(QColor(c['link']),1.5));p.setBrush(Qt.NoBrush);p.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),7,7)
        p.end()


class AvatarButton(ToolbarButton):
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        c=palette();initial=getattr(self,'initial','')
        p.setPen(Qt.NoPen);p.setBrush(QColor(c['action']) if initial else ink(c['control']));p.drawEllipse(QRectF(4,4,self.width()-8,self.height()-8))
        if initial:
            p.setPen(QColor('white'));f=p.font();f.setPixelSize(11);f.setBold(True);p.setFont(f);p.drawText(self.rect(),Qt.AlignCenter,initial)
        else:icon('user',c['secondary']).paint(p,QRect((self.width()-16)//2,(self.height()-16)//2,16,16))
        if self.hasFocus() and self._keyboard_focus:
            p.setPen(QPen(QColor(c['blue']),1.5));p.setBrush(Qt.NoBrush);p.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),7,7)
        p.end()


class TransferNavButton(IconTextButton):
    """Quiet sidebar entry; the chevron belongs to the same click target."""
    def paintEvent(self,event):
        from PySide6.QtWidgets import QStyleOptionButton,QStyle,QStylePainter
        option=QStyleOptionButton();self.initStyleOption(option);option.text='';option.icon=QIcon()
        p=QStylePainter(self);p.drawControl(QStyle.CE_PushButton,option);p.setRenderHint(QPainter.Antialiasing)
        if self.property('transferDropActive'):
            p.setPen(QPen(QColor(palette()['link']),1));p.setBrush(ink(palette()['hover']));p.drawRoundedRect(QRectF(self.rect()).adjusted(.5,.5,-.5,-.5),8,8)
        c=palette();p.setFont(self.font());p.setPen(QColor(c['secondary']))
        icon('nearby',c['secondary']).paint(p,QRect(12,(self.height()-16)//2,16,16))
        p.drawText(QRectF(36,0,max(0,self.width()-66),self.height()),Qt.AlignLeft|Qt.AlignVCenter,self.text())
        icon('right',c['secondary']).paint(p,QRect(self.width()-24,(self.height()-12)//2,12,12))
        p.end()


class ProfileButton(QPushButton):
    def __init__(self,parent=None):
        super().__init__(parent);self.username='未登录';self.status='登录以同步搁架';self.connected=False
        self.setCursor(Qt.PointingHandCursor);self.setFixedHeight(42)

    def set_profile(self,name,connected=False,status=''):
        self.username=str(name or '未登录');self.connected=bool(connected)
        self.status='' if connected else str(status or '登录以同步搁架');self.update()
        self.setAccessibleName(f'账号与设置，{self.username}，{self.status}')

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);c=palette()
        p.setPen(QPen(ink(c['border']),1));p.setBrush(ink(c['hover']) if self.underMouse() else Qt.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5,1.5,-.5,-1.5),10,10)
        avatar=QRectF(8,(self.height()-24)/2,24,24)
        p.setBrush(QColor(c['action']) if self.connected else ink(c['control']));p.setPen(Qt.NoPen);p.drawEllipse(avatar)
        f=p.font();f.setPixelSize(13 if sys.platform=='win32' else 11);f.setBold(True);p.setFont(f);p.setPen(QColor('white'))
        if self.connected:p.drawText(avatar,Qt.AlignCenter,self.username[:1].upper())
        else:icon('user',c['secondary']).paint(p,QRect(12,round(avatar.center().y()-8),16,16))
        block_top=(self.height()-30)/2 if self.status else 0
        f.setWeight(QFont.DemiBold);p.setFont(f);p.setPen(QColor(c['text']));p.drawText(QRectF(40,block_top,self.width()-70,18 if self.status else self.height()),Qt.AlignLeft|Qt.AlignVCenter,p.fontMetrics().elidedText(self.username,Qt.ElideRight,self.width()-70))
        if self.status:
            f.setPixelSize(10);f.setBold(False);p.setFont(f);p.setPen(QColor(c['secondary']));p.drawText(QRectF(40,block_top+18,self.width()-70,12),Qt.AlignLeft|Qt.AlignVCenter,p.fontMetrics().elidedText(self.status,Qt.ElideRight,self.width()-70))
        icon('right',c['muted']).paint(p,QRect(self.width()-24,(self.height()-12)//2,12,12))
        if keyboard_focus_visible(self):
            p.setPen(QPen(QColor(c['link']),1.5));p.setBrush(Qt.NoBrush);p.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),8,8)
        p.end()


def outside_content_view(widget):
    row=widget if hasattr(widget,'popup') else widget.parentWidget()
    viewport=getattr(getattr(getattr(row,'popup',None),'scroll',None),'viewport',lambda:None)()
    return viewport is not None and not QRect(widget.mapTo(viewport,QPoint()),widget.size()).intersects(viewport.rect())


class SelectionCheck(QCheckBox):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor);self.setAccessibleName('选择此项')

    def hitButton(self,position):return self.rect().contains(position)

    def paintEvent(self,event):
        if outside_content_view(self):return
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.translate((self.width()-15)/2,(self.height()-15)/2)
        p.setBrush(QColor('#0A84FF') if self.isChecked() else Qt.NoBrush)
        p.setPen(QPen(QColor('#0A84FF') if self.isChecked() else ink(palette()['input_border']),1))
        p.drawRoundedRect(QRectF(.5,.5,14,14),4,4)
        if self.isChecked():
            p.setPen(QPen(QColor('white'),1.5,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));p.drawLine(QPointF(4,7),QPointF(6,10));p.drawLine(QPointF(6,10),QPointF(11,4.5))
        p.end()


class MarqueeSelection(QObject):
    """Blank-space selection keeps file drags and checkbox clicks intact."""
    def __init__(self,view):
        super().__init__(view);self.view=view;self.active=False;self.origin=QPoint();self.last=QPoint();self.base=set()
        self.band=QRubberBand(QRubberBand.Rectangle,view.scroll.viewport())
        self.timer=QTimer(self);self.timer.setInterval(30);self.timer.timeout.connect(self._scroll)
        view.rows_body.installEventFilter(self)

    def eventFilter(self,watched,event):
        kind=event.type();view=self.view
        if kind==QEvent.MouseButtonPress and event.button()==Qt.LeftButton:
            child=view.rows_body.childAt(event.position().toPoint())
            while child is not None and child is not view.rows_body:
                if hasattr(child,'path'):return False
                child=child.parentWidget()
            self.active=True;self.origin=event.position().toPoint();self.last=self.origin
            self.base=set(view.selected) if event.modifiers()&(Qt.ControlModifier|Qt.MetaModifier|Qt.ShiftModifier) else set()
            self.band.setGeometry(QRect());self.band.show();self.timer.start();self._select();return True
        if kind==QEvent.MouseMove and self.active:
            self.last=event.position().toPoint();self._select();return True
        if kind==QEvent.MouseButtonRelease and self.active:
            self.last=event.position().toPoint();self._select();self.active=False;self.band.hide();self.timer.stop()
            view.restore_selected_preview();return True
        if kind in (QEvent.Hide,QEvent.UngrabMouse) and self.active:
            self.active=False;self.band.hide();self.timer.stop()
        return False

    def _select(self):
        view=self.view;rect=QRect(self.origin,self.last).normalized()
        top=view.rows_body.mapTo(view.scroll.viewport(),rect.topLeft())
        self.band.setGeometry(QRect(top,rect.size()).intersected(view.scroll.viewport().rect()))
        hits=set()
        for item in view.rows_body.findChildren(QWidget,options=Qt.FindDirectChildrenOnly):
            ref=getattr(item,'path',None)
            if ref and item.isVisible() and rect.intersects(item.geometry()):hits.add(ref)
        view.selected=self.base|hits;view.primary_selection=next((ref for ref in view.visible_refs() if ref in view.selected),None)
        view.hovered_ref=None
        for item in view.rows_body.findChildren(QWidget,options=Qt.FindDirectChildrenOnly):
            if hasattr(item,'set_selected'):item.set_selected(item.path in view.selected)
            else:item.update()
        view.update_selection_actions();view.shelf.touch()

    def _scroll(self):
        if not self.active:return
        viewport=self.view.scroll.viewport();point=self.view.rows_body.mapTo(viewport,self.last)
        delta=-18 if point.y()<20 else 18 if point.y()>viewport.height()-20 else 0
        if delta:
            bar=self.view.scroll.verticalScrollBar();before=bar.value();bar.setValue(before+delta)
            self.last+=QPoint(0,bar.value()-before);self._select()


class TransferSignal(QWidget):
    def __init__(self,parent=None):super().__init__(parent);self.state={};self.setFixedSize(18,18)
    def paintEvent(self,event):
        from transfer_visuals import draw_progress_badge
        p=QPainter(self);draw_progress_badge(p,QRectF(1,1,16,16),self.state);p.end()


class TransferStrip(QFrame):
    def __init__(self,view):
        super().__init__(view.middle);self.view=view;self.setCursor(Qt.PointingHandCursor)
    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton:self.view.set_category('uploading');event.accept()
        else:super().mouseReleaseEvent(event)


class TransferNameButton(QPushButton):
    def set_transfer(self,phase,title):
        self.phase,self.title=phase,title;self.setText(phase+' '+title)
    def paintEvent(self,event):
        p=QPainter(self);p.setFont(self.font());p.setPen(QColor(palette()['secondary']))
        phase=getattr(self,'phase','');width=p.fontMetrics().horizontalAdvance(phase+' ')
        p.drawText(QRect(0,0,width,self.height()),Qt.AlignVCenter,phase)
        font=p.font();font.setWeight(QFont.DemiBold);p.setFont(font);p.setPen(QColor(palette()['text']))
        title=p.fontMetrics().elidedText(getattr(self,'title',''),Qt.ElideRight,max(0,self.width()-width))
        p.drawText(QRect(width,0,self.width()-width,self.height()),Qt.AlignVCenter,title)
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
        close=ToolbarButton('close',self.card,motion=shelf.manager.motion);close.setFixedSize(32,32);close.clicked.connect(self.reject);header.addWidget(close);form.addLayout(header)
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
    cache=getattr(shelf,'file_metadata',None);info=cache.request(ref) if cache is not None else None
    folder=info['is_dir'] if info else Path(ref).is_dir() if cache is None else False
    if folder:
        size=shelf.request_directory_size(ref) if hasattr(shelf,'request_directory_size') else None
        if size is None:return '正在统计大小 · 文件夹'
    else:
        if cache is not None:
            size=info['size'] if info and info['exists'] else content.pending.get(ref,{}).get('size') or content.metadata.get(ref,{}).get('size') or content.transfers.get(ref,{}).get('total')
            if size is None:return '文件信息不可用' if info and info.get('error') else '正在读取文件信息…'
        else:
            try:size=Path(ref).stat().st_size
            except OSError:size=int(content.pending.get(ref,{}).get('size') or content.transfers.get(ref,{}).get('total') or 0)
    amount=f'{size/1024/1024/1024:.1f} GB' if size>=1024**3 else f'{size/1024/1024:.1f} MB' if size>=1024**2 else f'{size/1024:.1f} KB' if size>=1024 else f'{size} B'
    if folder:return amount+' · 文件夹'
    ext=Path(content.title(ref)).suffix.lstrip('.').upper()
    names={'images':'图像','video':'视频','audio':'音频','text':'文字','files':'文件','links':'网页'}
    return ' · '.join(x for x in (amount,(ext+' '+names.get(content.category(ref),'文件')).strip()) if x)


STATUS_COLORS={'synced':'#30D158','uploading':'#0A84FF','pending':'#64D2FF','local':'#A1A1AA','paused':'#FF9F0A','error':'#FF453A'}


def draw_status(p,rect,status):
    from transfer_visuals import draw_cloud_status_badge
    draw_cloud_status_badge(p,QRectF(rect),status)


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
        cache=getattr(shelf,'image_cache',None)
        decoded=cache.image(ref,128) if cache and category=='images' else cached
        if decoded is not None and not decoded.isNull():
            art=QPixmap.fromImage(decoded) if isinstance(decoded,QImage) else decoded
        clip=QPainterPath();clip.addRoundedRect(rect,9,9);p.setClipPath(clip)
        scale=min((size-2)/max(1,art.width()),(size-2)/max(1,art.height()))
        width,height=art.width()*scale,art.height()*scale
        p.drawPixmap(QRectF((size-width)/2,(size-height)/2,width,height),art,QRectF(art.rect()));p.setClipping(False)
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
        cache=getattr(shelf,'file_metadata',None);info=cache.request(ref) if cache is not None and not ext else None
        name=ext.lstrip('.').upper() or ('DIR' if info and info['is_dir'] or cache is None and Path(ref).is_dir() else 'FILE')
        color='#B52B47' if name=='PDF' else '#466B99' if name=='ZIP' else palette()['action']
        p.setPen(Qt.NoPen);p.setBrush(QColor(color));p.drawRoundedRect(QRectF(size*.12,size*.34,size*.76,size*.33),3,3)
        font=QFont();font.setPixelSize(max(8,round(size*.23)));font.setWeight(QFont.Bold);p.setFont(font);p.setPen(QColor('white'))
        p.drawText(QRectF(size*.1,size*.3,size*.8,size*.4),Qt.AlignCenter,name[:4])
    p.end();return pm


class ThumbnailLabel(TransferIconLabel):
    def __init__(self,shelf,ref,parent=None):
        super().__init__(shelf.content,ref,parent)
        self.shelf=shelf;self.ref=ref;self.setFixedSize(40,40)

    def paintEvent(self,event):
        if outside_content_view(self):return
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        art=thumbnail(self.shelf,self.ref,40)
        p.drawPixmap(0,0,art)
        if getattr(self,'status_overlay',True):draw_status(p,QRectF(26,26,14,14),self.cloud_status)
        p.end()


class MiniStack(StackTile):
    def __init__(self,shelf,parent=None,motion=True):
        super().__init__(parent,motion)
        self.shelf=shelf

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
        if not self.refs:
            r=QRectF((self.width()-176)/2,28,176,128)
            active=self._hovered or bool(getattr(self.parent(),'drop_highlight',False))
            color=palette()['link'] if active else palette()['border']
            p.setPen(QPen(ink(color),1,Qt.DashLine));p.setBrush(ink(palette()['card']));p.drawRoundedRect(r,16,16)
            icon('upload',palette()['secondary']).paint(p,QRect(round(r.center().x()-12),round(r.center().y()-31),24,24))
            f=p.font()
            f.setPixelSize(13);p.setFont(f);p.setPen(QColor(palette()['text']));p.drawText(r.adjusted(0,34,0,0),Qt.AlignCenter,'点击选择文件');p.end();return
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
            draw_status(p,QRectF(40,40,16,16),getattr(self.shelf.manager,'sync',None).sync_status_for_ref(ref) if hasattr(getattr(self.shelf.manager,'sync',None),'sync_status_for_ref') else 'local')
            title=self.shelf.content.title(ref);f=QFont(self.font());f.setPixelSize(13 if sys.platform=='win32' else 12);f.setWeight(QFont.DemiBold);p.setFont(f);p.setPen(QColor(c['text']))
            p.drawText(QRectF(14,77,164,20),Qt.AlignVCenter,p.fontMetrics().elidedText(title,Qt.ElideRight,164))
            f.setPixelSize(12 if sys.platform=='win32' else 10);f.setWeight(QFont.Normal);p.setFont(f);p.setPen(QColor(c['secondary']));p.drawText(QRectF(14,99,164,18),Qt.AlignVCenter,item_detail(self.shelf,ref).split(' · ')[0])
            ext=Path(title).suffix.lstrip('.').upper() or ('TEXT' if self.shelf.content.is_text(ref) else 'FILE')
            p.drawText(QRectF(100,14,78,18),Qt.AlignRight|Qt.AlignVCenter,ext[:7])
            state=getattr(self.repository,'transfers',{}).get(ref)
            if idx==0 and state:
                from transfer_visuals import draw_progress_badge
                draw_progress_badge(p,QRectF(162,40,16,16),state)
            p.restore()
        p.end()


def build_overview(view,preview_type):
    """Build the toolbar and 192 / flexible / 340 split view."""
    view.setObjectName('referenceOverview')
    css(view,'QFrame#referenceOverview{background:#1C1C1E;border:0;}','QFrame#referenceOverview{background:#FFFFFF;border:0;}')
    root=QVBoxLayout(view);root.setContentsMargins(0,0,0,0);root.setSpacing(0)
    view.header=QFrame(view);view.header.setFixedHeight(53)
    css(view.header,'QFrame{background:rgba(255,255,255,3);border:0;border-bottom:1px solid rgba(255,255,255,18);}',
        'QFrame{background:rgba(0,0,0,3);border:0;border-bottom:1px solid rgba(0,0,0,18);}')
    head=QHBoxLayout(view.header);head.setContentsMargins(16,10,16,10);head.setSpacing(9)
    from flickdock_icons import app_icon
    view.brand_logo=QLabel(view.header);view.brand_logo.setFixedSize(24,24)
    view.brand_logo.setPixmap(app_icon().pixmap(QSize(24,24),view.devicePixelRatioF()));head.addWidget(view.brand_logo)
    view.title=label('FlickDock',15,600,parent=view.header);head.addWidget(view.title)
    view.count_label=label('',11,600,'secondary',view.header);view.count_label.setAlignment(Qt.AlignCenter)
    css(view.count_label,"color:#D4D4D8;background:rgba(255,255,255,28);border:0;border-radius:10px;padding:1px 9px;font:600 11px 'HarmonyOS Sans SC';",
        "color:#515154;background:rgba(0,0,0,18);border:0;border-radius:10px;padding:1px 9px;font:600 11px 'HarmonyOS Sans SC';")
    view.count_label.setFixedHeight(20)
    view.cloud_selector=CloudSelector(view.shelf,view.header)
    view.cloud_selector.clicked.connect(view.shelf.show_cloud_menu)
    view.network_status=NetworkStatus(view.shelf.manager,view.header)
    head.addStretch(1)
    view.search_edit=QLineEdit(view.header);view.search_edit.setPlaceholderText('搜索...');view.search_edit.setFixedSize(176,32)
    view.search_edit.setClearButtonEnabled(True)
    view.search_edit.setAccessibleName('搜索搁架内容');view.search_edit.textChanged.connect(lambda _:view.rebuild())
    css(view.search_edit,"QLineEdit{background:#28282A;color:#F5F5F7;border:1px solid #3B3B3F;border-radius:8px;padding:4px 10px;font:12px 'HarmonyOS Sans SC';}",
        "QLineEdit{background:#F0F0F2;color:#1D1D1F;border:1px solid #D4D4D8;border-radius:8px;padding:4px 10px;font:12px 'HarmonyOS Sans SC';}")
    head.addWidget(view.search_edit)
    view.view_segment=QFrame(view.header);view.view_segment.setFixedSize(72,SEGMENT_HEIGHT)
    css(view.view_segment,'QFrame{background:rgba(255,255,255,10);border:1px solid rgba(255,255,255,18);border-radius:9px;}',
        'QFrame{background:rgba(0,0,0,10);border:1px solid rgba(0,0,0,18);border-radius:9px;}')
    segment=QHBoxLayout(view.view_segment);segment.setContentsMargins(2,2,2,2);segment.setSpacing(0)
    view.view_list=button('','list',parent=view.view_segment);view.view_grid=button('','grid',parent=view.view_segment)
    view.view_list.setToolTip('列表视图');view.view_list.setAccessibleName('列表视图')
    view.view_grid.setToolTip('图标视图');view.view_grid.setAccessibleName('图标视图')
    view.view_buttons=QButtonGroup(view);view.view_buttons.setExclusive(True)
    for control,key in ((view.view_list,'list'),(view.view_grid,'icons')):
        control.setFixedHeight(SEGMENT_BUTTON_HEIGHT);control.setMinimumWidth(0);control.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed);control.setCheckable(True);view.view_buttons.addButton(control)
        css(control,"QPushButton{background:transparent;border:0;border-radius:6px;color:#A1A1AA;padding:3px 8px;font:12px 'HarmonyOS Sans SC';} QPushButton:checked{background:rgba(255,255,255,31);color:#F5F5F7;font-weight:600;}",
            "QPushButton{background:transparent;border:0;border-radius:6px;color:#515154;padding:0;font:12px 'HarmonyOS Sans SC';} QPushButton:checked{background:transparent;color:#1D1D1F;font-weight:600;}")
        css(control,*[f"QPushButton{{background:transparent;border:0;border-radius:5px;padding:0;color:{colors(t)['secondary']};}} QPushButton:checked{{background:transparent;color:{colors(t)['text']};}}" for t in ('dark','light')])
        control.clicked.connect(lambda checked=False,key=key:view.change_view_mode(key));segment.addWidget(control,1)
        control.setChecked(view.shelf.manager.settings.view_mode==key)
    from ui_transitions import SegmentIndicator
    view.view_indicator=SegmentIndicator(view.view_segment,(view.view_list,view.view_grid))
    head.addWidget(view.view_segment)
    head.addStretch(1)
    view.zip_button=button('','package-selected',parent=view.header);view.zip_button.clicked.connect(view.compress_selected);head.addWidget(view.zip_button)
    view.zip_all_button=button('','package',parent=view.header);view.zip_all_button.clicked.connect(lambda:view.shelf.compress_refs(list(view.shelf.files['permanent'])));head.addWidget(view.zip_all_button)
    view.clear_button=button('','trash',parent=view.header);view.clear_button.clicked.connect(view.shelf.confirm_clear_all);head.addWidget(view.clear_button)
    for control,title in ((view.zip_button,'打包所选'),(view.zip_all_button,'打包全部'),(view.clear_button,'全部删除')):
        control.setFixedSize(32,32);control.setProperty('iconOnly',True);control.setToolTip(title);control.setAccessibleName(title)
    view.clear_button._reference_icon=('trash','red');view.clear_button.setIcon(icon('trash',palette()['red']))
    head.addWidget(view.network_status);head.addWidget(view.cloud_selector);head.addSpacing(156)
    root.addWidget(view.header)
    body=QHBoxLayout();body.setContentsMargins(0,0,0,0);body.setSpacing(0);view.body_layout=body
    view.sidebar=QFrame(view);view.sidebar.setFixedWidth(192)
    css(view.sidebar,'QFrame{background:#151517;border:0;border-right:1px solid rgba(255,255,255,15);}',
        'QFrame{background:#F2F4F8;border:0;border-right:1px solid rgba(0,0,0,15);}')
    side=QVBoxLayout(view.sidebar);side.setContentsMargins(10,10,10,10);side.setSpacing(2)
    view.category_group=QButtonGroup(view);view.category_group.setExclusive(True);view.category_buttons={};view.sidebar_buttons={};view.nav_counts={}
    glyphs={'all':'all','images':'image','video':'video','audio':'audio','text':'text','files':'file','links':'web','uploading':'transferring'}
    for key,title in view.CATEGORIES:
        control=button(title,glyphs[key],parent=view.sidebar);control.setCheckable(True);control.setFixedHeight(34)
        control.setProperty('leftAligned',True)
        css(control,"QPushButton{background:transparent;color:#A1A1AA;border:0;border-radius:12px;text-align:left;padding:6px 12px;font:500 12px 'HarmonyOS Sans SC';} QPushButton:hover{background:rgba(255,255,255,15);} QPushButton:checked{background:#0062CC;color:white;font-weight:600;}",
            "QPushButton{background:transparent;color:#515154;border:0;border-radius:12px;text-align:left;padding:6px 12px;font:500 12px 'HarmonyOS Sans SC';} QPushButton:hover{background:rgba(0,0,0,12);} QPushButton:checked{background:#0062CC;color:white;font-weight:600;}")
        control.clicked.connect(lambda checked=False,key=key:view.set_category(key));view.category_group.addButton(control);side.addWidget(control)
        count=label('',11,400,'muted',control);count.setAlignment(Qt.AlignCenter);count.setGeometry(145,7,28,20);count.setAttribute(Qt.WA_TransparentForMouseEvents)
        view.nav_counts[key]=count;view.category_buttons[key]=control;view.sidebar_buttons[key]=control
    view.transfer_badge=view.nav_counts['uploading']
    side.addStretch(1)
    view.wan_nav=button('远程传输','web','secondary',view.sidebar);view.wan_nav.hide()
    view.lan_nav=button('文件互传',parent=view.sidebar,control_type=TransferNavButton);view.lan_nav.setFixedHeight(34);view.lan_nav.clicked.connect(lambda:view.shelf.manager.show_transfer_dialog());side.addWidget(view.lan_nav)
    view.settings_nav=button('设置','settings',parent=view.sidebar);view.settings_nav.setFixedHeight(34);view.settings_nav.clicked.connect(lambda:view.shelf.manager.show_settings());side.addWidget(view.settings_nav)
    for control in (view.wan_nav,view.lan_nav,view.settings_nav):
        control.setProperty('leftAligned',True)
        old=control._reference_style
        css(control,*[s.replace('padding:4px 11px;','padding:4px 12px;text-align:left;') for s in old])
    view.account_nav=ProfileButton(view.sidebar);view.account_nav.clicked.connect(lambda:view.shelf.manager.show_settings());side.addWidget(view.account_nav)
    body.addWidget(view.sidebar)
    view.middle=QWidget(view);mid=QVBoxLayout(view.middle);view.middle_layout=mid;mid.setContentsMargins(0,0,0,0);mid.setSpacing(0)
    view.selection_bar=QFrame(view.middle);view.selection_bar.setFixedHeight(40)
    css(view.selection_bar,'QFrame{border:0;border-bottom:1px solid rgba(255,255,255,10);background:transparent;}',
        'QFrame{border:0;border-bottom:1px solid rgba(0,0,0,10);background:transparent;}')
    select=QHBoxLayout(view.selection_bar);select.setContentsMargins(12,0,12,0);select.setSpacing(8)
    view.sort_segment=QFrame(view.selection_bar);view.sort_segment.setObjectName('overviewSortSegment');view.sort_segment.setFixedHeight(SEGMENT_HEIGHT)
    css(view.sort_segment,'QFrame{background:rgba(255,255,255,10);border:1px solid rgba(255,255,255,18);border-radius:8px;}',
        'QFrame{background:#ECEEF2;border:1px solid #E0E3E8;border-radius:8px;}')
    sort_layout=QHBoxLayout(view.sort_segment);sort_layout.setContentsMargins(2,2,2,2);sort_layout.setSpacing(0)
    view.sort_group=QButtonGroup(view.sort_segment);view.sort_group.setExclusive(True)
    view.selection_hint=button('默认',parent=view.sort_segment);view.selection_hint.setAccessibleName('恢复默认排序');view.selection_hint.clicked.connect(view.reset_sort)
    view.selection_hint.setCheckable(True);view.sort_group.addButton(view.selection_hint);sort_layout.addWidget(view.selection_hint)
    view.sort_buttons={}
    for key,title in (('time','时间'),('size','大小')):
        control=button(title,parent=view.sort_segment,control_type=SortButton);control.setCheckable(True)
        control.setAccessibleName('按'+title+'排序，正序、倒序、默认循环')
        control.clicked.connect(lambda _,key=key:view.cycle_sort(key))
        view.sort_buttons[key]=control;view.sort_group.addButton(control);sort_layout.addWidget(control)
    for control in (view.selection_hint,*view.sort_buttons.values()):
        type_size=10 if sys.platform=='win32' else 11
        css(control,*[f"QPushButton{{background:transparent;border:0;border-radius:5px;padding:0;font:500 {type_size}px 'HarmonyOS Sans SC';color:{colors(t)['secondary']};}} QPushButton:checked{{background:transparent;color:{colors(t)['text']};}}" for t in ('dark','light')])
        control.ensurePolished();control.setFixedHeight(SEGMENT_BUTTON_HEIGHT)
        control.setFixedWidth(control.fontMetrics().horizontalAdvance(control.text())+(20 if control is view.selection_hint else 26))
    view.sort_indicator=SegmentIndicator(view.sort_segment,(view.selection_hint,*view.sort_buttons.values()))
    select.addWidget(view.sort_segment)
    view.count_label.setParent(view.selection_bar);select.addWidget(view.count_label)
    select.addStretch(1)
    for control in (view.zip_button,view.zip_all_button,view.clear_button):
        head.removeWidget(control);control.setParent(view.selection_bar);select.addWidget(control)
    select.setSpacing(4)
    view.select_all_button=button('全选',variant='secondary',parent=view.selection_bar);view.select_all_button.clicked.connect(view.toggle_select_all_visible);select.addWidget(view.select_all_button)
    view.select_all_button.setFixedHeight(SEGMENT_HEIGHT)
    view.paste_button=button('粘贴',parent=view.selection_bar);view.paste_button.hide()
    mid.addWidget(view.selection_bar)
    view.transfer_summary=QWidget(view.middle);summary=QHBoxLayout(view.transfer_summary);summary.setContentsMargins(16,10,16,10)
    view.transfer_count=label('',12,600,parent=view.transfer_summary);view.transfer_eta=label('',11,400,'link',view.transfer_summary)
    view.transfer_bar=QProgressBar(view.transfer_summary);view.transfer_bar.setTextVisible(False);view.transfer_bar.setFixedHeight(6)
    css(view.transfer_bar,'QProgressBar{border:0;background:#343438;border-radius:3px;} QProgressBar::chunk{background:#0A84FF;border-radius:3px;}',
        'QProgressBar{border:0;background:#DDE4ED;border-radius:3px;} QProgressBar::chunk{background:#0A84FF;border-radius:3px;}')
    summary.addWidget(view.transfer_count);summary.addWidget(view.transfer_bar,1);summary.addWidget(view.transfer_eta);mid.addWidget(view.transfer_summary);view.transfer_summary.hide()
    view.scroll=QScrollArea(view.middle);view.scroll.setWidgetResizable(True);view.scroll.setFrameShape(QFrame.NoFrame);view.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    view.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
    css(view.scroll,'QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::handle:vertical{background:#49494E;border-radius:3px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}',
        'QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::handle:vertical{background:#CBCBD0;border-radius:3px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}')
    view.rows_body=QWidget();view.rows_layout=QVBoxLayout(view.rows_body);view.rows_layout.setContentsMargins(12,12,12,12);view.rows_layout.setSpacing(6);view.scroll.setWidget(view.rows_body)
    # QScrollArea enables autoFillBackground on setWidget; its inherited
    # transparent palette otherwise clears a hole in transition snapshots.
    view.rows_body.setAutoFillBackground(False);mid.addWidget(view.scroll,1)
    view.marquee=MarqueeSelection(view)
    view.empty_state=QWidget(view.middle);empty=QVBoxLayout(view.empty_state);empty.setContentsMargins(16,16,16,16);empty.addStretch(1)
    view.empty_drop=OverviewEmptyDropZone(view.empty_state);view.empty_drop.setFixedSize(280,220)
    view.empty_drop.setAccessibleName('搁架为空，点击选择文件，或拖入文件、图片、文字或链接')
    view.empty_drop.clicked.connect(lambda:view.shelf.pick_files('permanent'))
    view.empty_drop.illustration.hide()
    view.empty_symbol=label('',36,400,'muted',view.empty_drop);view.empty_symbol.setAlignment(Qt.AlignCenter)
    view.empty_drop.layout().insertWidget(1,view.empty_symbol)
    view.empty_title=view.empty_drop.title;view.empty_hint=view.empty_drop.hint
    css(view.empty_title,"color:#F5F5F7;background:transparent;border:0;font:600 14px 'HarmonyOS Sans SC';","color:#1D1D1F;background:transparent;border:0;font:600 14px 'HarmonyOS Sans SC';")
    css(view.empty_hint,"color:#A1A1AA;background:transparent;border:0;font:12px 'HarmonyOS Sans SC';","color:#636366;background:transparent;border:0;font:12px 'HarmonyOS Sans SC';")
    for control in (view.empty_symbol,view.empty_title,view.empty_hint):control.setAttribute(Qt.WA_TransparentForMouseEvents)
    view.empty_hint.setText('拖入内容，或点击选择文件');view.empty_hint.show()
    empty.addWidget(view.empty_drop,0,Qt.AlignCenter)
    empty.addStretch(1);mid.addWidget(view.empty_state,1);view.empty_state.hide()
    view.transfer_strip=TransferStrip(view)
    css(view.transfer_strip,'QFrame{background:transparent;border:0;}',
        'QFrame{background:transparent;border:0;}')
    strip_policy=QSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed);strip_policy.setRetainSizeWhenHidden(True)
    view.transfer_strip.setSizePolicy(strip_policy)
    strip=QVBoxLayout(view.transfer_strip);strip.setContentsMargins(0,0,0,0);strip.setSpacing(0)
    top=QHBoxLayout();view.strip_title=label('传输中',11,500,'secondary',view.transfer_strip);view.strip_more=button('',parent=view.transfer_strip);view.strip_more.clicked.connect(lambda:view.set_category('uploading'))
    view.strip_title.hide()
    view.strip_rows=[];view._strip_refs=[]
    for index in range(1):
        row=QWidget(view.transfer_strip);line=QHBoxLayout(row);line.setContentsMargins(0,0,0,0);line.setSpacing(7)
        view.strip_signal=TransferSignal(row);line.addWidget(view.strip_signal)
        name=TransferNameButton(row);name.setFixedHeight(30);name.setCursor(Qt.PointingHandCursor)
        css(name,"color:#F5F5F7;background:transparent;border:0;font:12px 'HarmonyOS Sans SC';", "color:#1D1D1F;background:transparent;border:0;font:12px 'HarmonyOS Sans SC';")
        name.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed);name.clicked.connect(lambda checked=False,index=index:view._open_strip_transfer(index))
        percent=label('',10,400,'link',row);progress=QProgressBar(row);progress.setFixedSize(42,3);progress.setTextVisible(False)
        css(progress,'QProgressBar{background:#343438;border:0;} QProgressBar::chunk{background:#64D2FF;}',
            'QProgressBar{background:#DDE4ED;border:0;} QProgressBar::chunk{background:#0A84FF;}')
        line.addWidget(name,1);line.addWidget(percent);line.addWidget(view.strip_more);progress.hide();strip.addWidget(row);view.strip_rows.append((row,name,percent,progress))
    # One fixed-height row keeps clipboard position stable as transfers change.
    from clipboard_preview import ClipboardButton
    view.clipboard_footer=QWidget(view.middle);view.clipboard_footer.setFixedHeight(56)
    clipboard_row=QHBoxLayout(view.clipboard_footer);clipboard_row.setContentsMargins(12,12,12,12);clipboard_row.setSpacing(8)
    view.clipboard_button=ClipboardButton(view.shelf.manager,view.clipboard_footer)
    clipboard_row.addWidget(view.clipboard_button);clipboard_row.addWidget(view.transfer_strip,1)
    mid.addWidget(view.clipboard_footer)
    view.transfer_strip.hide();body.addWidget(view.middle,1)
    view.preview=preview_type(view);view.preview.setFixedWidth(340);body.addWidget(view.preview)
    root.addLayout(body,1)
    # Compatibility members retain original navigation/selection code paths.
    view.category_segments=QWidget(view);view.category_segments.hide()
    view.category_picker=button('全部',parent=view.selection_bar);select.insertWidget(0,view.category_picker);view.category_picker.clicked.connect(view.open_category_menu);view.category_picker.hide()
    view.bulk_more=ToolbarButton('more',view.header,motion=view.shelf.manager.motion);view.bulk_more.setFixedSize(32,32);view.bulk_more.setAccessibleName('批量操作');head.insertWidget(head.count()-1,view.bulk_more);view.bulk_more.hide();view.bulk_more.clicked.connect(view.open_bulk_menu)
    view.narrow_view_button=button('预览',parent=view.selection_bar);select.addWidget(view.narrow_view_button);view.narrow_view_button.hide();view.narrow_view_button.clicked.connect(view.toggle_narrow_preview)
    # Match the three body columns instead of centering tools across the window.
    while head.count():head.takeAt(0)
    head.setContentsMargins(0,0,0,0);head.setSpacing(0)
    view.header_brand=QWidget(view.header);view.header_brand.setFixedWidth(192)
    brand=QHBoxLayout(view.header_brand);brand.setContentsMargins(16,10,12,10);brand.setSpacing(9)
    brand.addWidget(view.brand_logo);brand.addWidget(view.title);brand.addStretch(1);head.addWidget(view.header_brand)
    view.header_tools=QWidget(view.header)
    tools=QHBoxLayout(view.header_tools);tools.setContentsMargins(12,10,12,10);tools.setSpacing(9)
    tools.addStretch(1);tools.addWidget(view.search_edit);tools.addWidget(view.view_segment);tools.addWidget(view.bulk_more)
    head.addWidget(view.header_tools,1)
    view.header_system=QWidget(view.header);view.header_system.setFixedWidth(340)
    system=QHBoxLayout(view.header_system);system.setContentsMargins(0,10,16,10);system.setSpacing(4)
    system.addStretch(1);system.addWidget(view.network_status);system.addWidget(view.cloud_selector);system.addSpacing(156)
    head.addWidget(view.header_system)
    for control in (view.network_status,view.cloud_selector):
        policy=control.sizePolicy();policy.setRetainSizeWhenHidden(True);control.setSizePolicy(policy);control.hide()
    view._narrow_layout=False;view._narrow_preview=False


def build_preview(view,elided_type,image_type,media_type,content_type):
    from transfer_visuals import FileStatusPill, TransferOverlay
    view.setObjectName('referencePreview')
    css(view,'QFrame#referencePreview{background:#121213;border:0;border-left:1px solid rgba(255,255,255,15);}',
        'QFrame#referencePreview{background:#F5F6FA;border:0;border-left:1px solid rgba(0,0,0,15);}')
    root=QVBoxLayout(view);root.setContentsMargins(0,0,0,0);root.setSpacing(0)
    head=QFrame(view);head.setFixedHeight(68)
    css(head,'QFrame{border:0;border-bottom:1px solid rgba(255,255,255,15);background:transparent;}',
        'QFrame{border:0;border-bottom:1px solid rgba(0,0,0,15);background:transparent;}')
    header=QHBoxLayout(head);header.setContentsMargins(16,10,16,10);header.setSpacing(8)
    names=QVBoxLayout();names.setSpacing(2)
    view.name=elided_type(view);view.info=elided_type(view)
    for item,size,weight,tint in ((view.name,13,600,'text'),(view.info,11,400,'secondary')):
        css(item,f"color:{colors('dark')[tint]};border:0;background:transparent;font:{weight} {size}px 'HarmonyOS Sans SC';",
            f"color:{colors('light')[tint]};border:0;background:transparent;font:{weight} {size}px 'HarmonyOS Sans SC';")
        if item is view.name:names.addWidget(item)
    header.addLayout(names,1);view.cloud_badge=FileStatusPill(view)
    header.addWidget(view.cloud_badge);view.copy_button=ToolbarButton('copy',view,motion=view.shelf.manager.motion);view.copy_button.setFixedSize(32,32);view.copy_button.clicked.connect(view.copy_current);header.addWidget(view.copy_button);root.addWidget(head)
    view.copy_button.setProperty('framed',True)
    content=QWidget(view);body=QVBoxLayout(content);body.setContentsMargins(16,16,16,16);body.setSpacing(12)
    view.transfer_row=QWidget(content);transfer=QHBoxLayout(view.transfer_row);transfer.setContentsMargins(0,0,0,0)
    view.transfer_progress=QProgressBar(view.transfer_row);view.transfer_progress.setTextVisible(False);view.transfer_progress.setFixedHeight(5)
    css(view.transfer_progress,'QProgressBar{background:#333336;border:0;} QProgressBar::chunk{background:#0A84FF;}',
        'QProgressBar{background:#E0E0E5;border:0;} QProgressBar::chunk{background:#0A84FF;}')
    view.transfer_meta=label('',10,400,'link',view.transfer_row);view.cancel_transfer=button('取消上传',parent=view.transfer_row);view.cancel_transfer.clicked.connect(view.cloud_transfer_action)
    transfer.addWidget(view.transfer_progress,1);transfer.addWidget(view.transfer_meta);transfer.addWidget(view.cancel_transfer);view.transfer_row.hide()
    view.stack=QStackedWidget(content);view.stack.setMinimumHeight(176);view.stack.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
    css(view.stack,'QStackedWidget{background:#242426;border:1px solid #414145;border-radius:12px;}',
        'QStackedWidget{background:#FFFFFF;border:1px solid #DDDEE2;border-radius:12px;}')
    view.icon=content_type(view,motion=view.shelf.manager.motion)
    view.text=QPlainTextEdit(view);view.text.setReadOnly(True);view.text.setAcceptDrops(False);view.text.viewport().setAcceptDrops(False)
    css(view.text,"QPlainTextEdit{background:#242426;color:#F5F5F7;border:0;border-radius:12px;padding:14px;font:13px 'HarmonyOS Sans SC';}",
        "QPlainTextEdit{background:#FFFFFF;color:#1D1D1F;border:0;border-radius:12px;padding:14px;font:13px 'HarmonyOS Sans SC';}")
    view.image=image_type(view);view.image.set_loader(view.shelf.image_cache);view.image.source_ready.connect(view._image_source_ready)
    view.media=media_type(view,motion=view.shelf.manager.motion);view.media.frame_ready.connect(view._poster_ready);view.media.playing_changed.connect(view._playing_changed)
    for item in (view.icon,view.text,view.image,view.media):view.stack.addWidget(item)
    body.addWidget(view.stack,1)
    status=QFrame(content);status.setObjectName('referenceStatus')
    css(status,'QFrame#referenceStatus{background:rgba(255,255,255,7);border:1px solid rgba(255,255,255,15);border-radius:12px;}',
        'QFrame#referenceStatus{background:rgba(255,255,255,184);border:1px solid rgba(0,0,0,15);border-radius:12px;}')
    box=QVBoxLayout(status);box.setContentsMargins(12,10,12,10);box.setSpacing(7)
    types=QHBoxLayout();types.addWidget(label('类型与大小',11,400,'muted',status))
    view.info.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Preferred);view.info.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
    types.addWidget(view.info,1);box.addLayout(types)
    line=QHBoxLayout();line.addWidget(label('状态',11,400,'muted',status));line.addStretch(1);view.status_value=label('',11,500,'green',status);line.addWidget(view.status_value);box.addLayout(line)
    box.addWidget(view.transfer_row)
    view.path_label=elided_type(status);view.path_label.setAccessibleName('内容来源路径')
    css(view.path_label,"color:#A1A1AA;background:transparent;border:0;font:10px 'Consolas';",
        "color:#636366;background:transparent;border:0;font:10px 'Consolas';")
    origin=QHBoxLayout();origin.addWidget(label('内容来源路径',11,400,'muted',status));origin.addWidget(view.path_label,1);box.addLayout(origin);body.addWidget(status)
    upload_time=QHBoxLayout();upload_time.addWidget(label('放入时间',11,400,'muted',status))
    view.upload_time_value=label('暂无时间记录',11,400,'secondary',status);view.upload_time_value.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
    upload_time.addWidget(view.upload_time_value,1);box.addLayout(upload_time)
    view.actions_frame=QWidget(view);actions=QGridLayout(view.actions_frame);actions.setSpacing(8);actions.setContentsMargins(16,0,16,16)
    view.open_button=button('打开',variant='primary',parent=content);view.rename_button=button('重命名文件',variant='secondary',parent=content)
    view.open_button.clicked.connect(lambda:view.current_path and view.shelf.open_path(view.current_path));view.rename_button.clicked.connect(lambda:view.current_path and view.shelf.rename_ref(view.current_path))
    view.reveal_button=button('在文件夹中显示',variant='secondary',parent=content);view.reveal_button.clicked.connect(lambda:view.current_path and view.shelf.reveal_ref(view.current_path))
    def selected_refs():return view.popup.chosen_refs() or ([view.current_path] if view.current_path else [])
    view.copy_selection_button=button('复制所选','copy',variant='secondary',parent=content);view.copy_selection_button.clicked.connect(lambda:view.shelf.copy_items(selected_refs()))
    view.zip_selection_button=button('打包为 ZIP...',variant='secondary',parent=content);view.zip_selection_button.clicked.connect(lambda:view.shelf.compress_refs(selected_refs()))
    view.remove_button=button('移出搁架','trash',variant='danger',parent=content);view.remove_button.clicked.connect(lambda:view.current_path and view.popup.remove_path(view.current_path))
    view.remove_button._reference_icon=('trash','red');view.remove_button.setIcon(icon('trash',palette()['red']))
    for index,item in enumerate((view.open_button,view.reveal_button,view.copy_selection_button,view.rename_button,view.zip_selection_button,view.remove_button)):
        item.setFixedHeight(32);actions.addWidget(item,index//2,index%2)
    actions.setColumnStretch(0,1);actions.setColumnStretch(1,1)
    view.detail_scroll=QScrollArea(view);view.detail_scroll.setWidgetResizable(True);view.detail_scroll.setFrameShape(QFrame.NoFrame);view.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    view.detail_scroll.setWidget(content);view.detail_scroll.setStyleSheet('QScrollArea{background:transparent;border:0;} QScrollArea>QWidget>QWidget{background:transparent;} QScrollBar:vertical{width:6px;}')
    root.addWidget(view.detail_scroll,1)
    root.addWidget(view.actions_frame)
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

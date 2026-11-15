"""Qt artwork and controls transcribed from the supplied FlickDock HTML.

Only presentation lives here. Content, drag MIME, disk work and network services
continue to be owned by the existing shelf and panel classes.
"""
from pathlib import Path
from collections import OrderedDict
import sys
import re
import time

from PySide6.QtCore import QObject, QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QIcon, QPalette, QImage, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (QLabel, QPushButton, QWidget, QFrame, QHBoxLayout,
    QVBoxLayout, QScrollArea, QLineEdit, QProgressBar, QButtonGroup, QSizePolicy, QGridLayout,
    QStackedWidget, QPlainTextEdit, QCheckBox, QApplication, QDialog, QRubberBand)

import ui_theme
from native_ui_common import (colors, icon, keyboard_focus_visible, readable_qss, set_ui_weight, reference_font,
    RADIUS_SMALL_PX,RADIUS_SUBCONTROL_PX,RADIUS_CONTROL_PX,RADIUS_CARD_PX,RADIUS_PANEL_PX,
    SHELF_HEADER_BUTTON_SIZE_PX, SHELF_HEADER_BUTTON_GAP_PX, SHELF_HEADER_RIGHT_MARGIN_PX,
    SHELF_OVERVIEW_HEADER_HEIGHT_PX)
from content_stack import (StackTile,FAN_FRONT_ANGLE_DEGREES,FAN_LAYER_ANGLE_DEGREES,
    FAN_LAYER_OFFSET_X_PX,MINI_FAN_LAYER_OFFSET_Y_PX,STACK_ARRIVAL_OFFSET_PX)
from flickdock_motion import FeedbackButton
from ui_transitions import SelectionSurface
from transfer_visuals import TransferIconLabel, CLOUD_STATUS_LABELS
from lan_panel import _DropZone


SORT_ARROW_SIZE_PX = 10
SORT_ARROW_GAP_PX = 2
SEGMENT_HEIGHT_PX = 32
SEGMENT_BUTTON_HEIGHT_PX = 26
SEGMENT_TEXT_PADDING_PX = 8
SORT_TEXT_PADDING_PX = 4
SEGMENT_BORDER_PX = 1
PROFILE_TEXT_RISE_PX = 1
EMPTY_DROP_RADIUS_PX = RADIUS_PANEL_PX
PREVIEW_INFO_HORIZONTAL_PADDING_PX = 12
PREVIEW_INFO_VERTICAL_PADDING_PX = 8
PREVIEW_INFO_BOTTOM_PADDING_PX = 5
PREVIEW_INFO_TITLE_HEIGHT_PX = 16
PREVIEW_INFO_VALUE_HEIGHT_PX = 28
PREVIEW_INFO_ROW_GAP_PX = 4


class OverviewEmptyDropZone(_DropZone):
    """Match the preview's empty drop target, without scaling its dash path."""
    def __init__(self,parent=None):
        super().__init__(parent);self._transfer_choice_active=False

    def border_path(self):
        rect=QRectF(self.rect()).adjusted(1,1,-1,-1)
        radius=min(EMPTY_DROP_RADIUS_PX,rect.width()/2,rect.height()/2)
        path=QPainterPath();path.addRoundedRect(rect,radius,radius)
        return path

    def set_transfer_choice_active(self,active):
        self._transfer_choice_active=bool(active)
        if active:self._hovered=False;self.set_drag_active(False)

    def set_drag_active(self,active):
        super().set_drag_active(bool(active) and not self._transfer_choice_active)

    def enterEvent(self,event):
        if not self._transfer_choice_active:super().enterEvent(event)

    def paintEvent(self, event):
        self.illustration.dark=ui_theme.current_theme()=='dark'
        super().paintEvent(event)


class EmptyPreviewIcon(QWidget):
    """Repaint with the current theme without retaining a stale pixmap."""
    def paintEvent(self,event):
        painter=QPainter(self)
        icon('file',palette()['secondary']).paint(painter,2,2,24,24)


def palette():
    return colors(ui_theme.current_theme())


def ink(value):
    if str(value).startswith('rgba('):
        return QColor(*[int(float(x)) for x in re.findall(r'[\d.]+',str(value))])
    return QColor(value)


def css(widget, dark, light=None):
    # Resolve the source's real 400/500/600/700 weights in the selected OS face.
    if QApplication.instance():
        from native_ui_common import font_css_stack
        family=font_css_stack()
        dark=dark.replace("'FlickDock UI'",family)
        if light is not None:light=light.replace("'FlickDock UI'",family)
    widget._reference_style=(readable_qss(dark), readable_qss(light if light is not None else dark))
    from theme_accent import resolve_qss
    resolved=resolve_qss(widget._reference_style[ui_theme.current_theme()=='light'],ui_theme.current_theme())
    if widget.styleSheet()!=resolved:widget.setStyleSheet(resolved)


def refresh_tree(widget,theme=None):
    theme=theme if theme in ('light','dark') else ui_theme.current_theme()
    themed_colors=colors(theme)
    for child in [widget, *widget.findChildren(QWidget)]:
        source=getattr(child,'_reference_style',None)
        if source:
            from theme_accent import resolve_qss
            resolved=resolve_qss(source[theme=='light'],theme)
            if child.styleSheet()!=resolved:child.setStyleSheet(resolved)
        if getattr(child,'_reference_icon',None):
            name,tint=child._reference_icon
            child.setIcon(icon(name,themed_colors.get(tint,tint)))
        child.update()


def optical_text_baseline(font,text,height):
    """Center the visible glyphs, including mixed Chinese and Latin text."""
    return height/2-QFontMetricsF(font).tightBoundingRect(text).center().y()


class OpticalLabel(QLabel):
    def paintEvent(self,event):
        p=QPainter(self);p.setFont(self.font());p.setPen(self.palette().color(QPalette.WindowText))
        text=self.fontMetrics().elidedText(self.text(),Qt.ElideRight,self.width())
        width=p.fontMetrics().horizontalAdvance(text)
        x=(self.width()-width)/2 if self.alignment()&Qt.AlignHCenter else max(0,self.width()-width) if self.alignment()&Qt.AlignRight else 0
        p.drawText(QPointF(x,optical_text_baseline(p.font(),text,self.height())),text);p.end()


def label(value='',size=12,weight=400,tint='text',parent=None,*,control_type=QLabel):
    widget=control_type(value,parent)
    dark=colors('dark');light=colors('light')
    css(widget,f"color:{dark.get(tint,tint)};background:transparent;border:0;font:{weight} {size}px 'FlickDock UI';",
        f"color:{light.get(tint,tint)};background:transparent;border:0;font:{weight} {size}px 'FlickDock UI';")
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
            from display_frames import DisplayAnimation
            self._icon_animation=DisplayAnimation(self)
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
        from native_ui_common import button_style_option
        option=button_style_option(self)
        art,words=QIcon(option.icon),str(option.text);option.icon=QIcon();option.text=''
        theme=self.property('referenceTheme')
        c=colors(theme) if theme in ('light','dark') else palette()
        variant=self.property('referenceVariant');left=bool(self.property('leftAligned'))
        surface=self.parentWidget()
        navigation=isinstance(surface,SelectionSurface) and surface._selection_vertical and left
        navigation_ink=c['text'] if self.property('sidebarUtility') else c['secondary']
        active=(surface._selection_rect.contains(QRectF(self.geometry()).center()) and surface._selection_opacity>.55) if navigation else (self.isChecked() and left)
        named_icon=getattr(self,'_reference_icon',None)
        if named_icon:
            icon_tint=(c['on_action'] if active or variant=='primary'
                       else navigation_ink if navigation else c['red'] if variant=='danger' else c.get(named_icon[1],named_icon[1]))
            art=icon(named_icon[0],icon_tint if self.isEnabled() else c['muted'])
        painter=QStylePainter(self);painter.drawControl(QStyle.CE_PushButton,option)
        painter.setFont(self.font())
        if self.property('splitDivider'):
            divider=QColor(c['on_action']);divider.setAlpha(32 if self.isEnabled() else 20)
            painter.save();painter.setPen(QPen(divider,1));painter.drawLine(QPointF(.5,10),QPointF(.5,self.height()-10));painter.restore()
        if not words:
            size=self.iconSize();rect=QRect((self.width()-size.width())//2,(self.height()-size.height())//2,size.width(),size.height())
            value=getattr(self,'_icon_value',QPointF());glyph=getattr(self,'_reference_icon',('',))[0]
            painter.save();center=QRectF(rect).center();painter.translate(center)
            pressed=max(value.y(),1. if self.isDown() else 0.)
            painter.scale(1-.05*pressed,1-.05*pressed)
            if glyph in ('settings','refresh','trash'):painter.rotate(value.x()*{'settings':10,'refresh':28,'trash':-5}[glyph])
            elif glyph in ('file','package','package-selected','cloud'):painter.translate(0,-value.x())
            elif glyph in ('nearby','web'):painter.translate(value.x(),0)
            elif glyph in ('expand','collapse'):painter.rotate(value.x()*(6 if glyph=='expand' else -6))
            elif glyph in ('list','grid'):painter.scale(1+.1*value.x(),1+.1*value.x())
            elif glyph=='chevron-down' and getattr(self,'_popup_chevron',None):painter.rotate(self._popup_chevron.angle)
            painter.translate(-center)
            art.paint(painter,rect,Qt.AlignCenter,QIcon.Normal if self.isEnabled() else QIcon.Disabled)
            painter.restore();return
        size=self.iconSize();gap=6
        space=self.width()-(56 if left else 16)-size.width()-gap
        text=painter.fontMetrics().elidedText(words,Qt.ElideRight,max(1,space))
        width=painter.fontMetrics().horizontalAdvance(text)+size.width()+gap
        x=12 if left else (self.width()-width)/2
        mode=QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        icon_y=round((self.height()-size.height())/2)
        if self.property('alignIconToTextInk') and text:
            metrics=QFontMetricsF(painter.font())
            baseline=(self.height()-metrics.height())/2+metrics.ascent()
            icon_y=round(baseline+metrics.tightBoundingRect(text).center().y()-size.height()/2)
        trailing=bool(self.property('trailingIcon'))
        icon_x=x+painter.fontMetrics().horizontalAdvance(text)+gap if trailing else x
        popup_motion=getattr(self,'_popup_chevron',None)
        painter.save()
        if trailing and popup_motion:
            painter.translate(icon_x+size.width()/2,icon_y+size.height()/2);painter.rotate(popup_motion.angle)
            painter.translate(-icon_x-size.width()/2,-icon_y-size.height()/2)
        art.paint(painter,QRect(round(icon_x),icon_y,size.width(),size.height()),Qt.AlignCenter,mode)
        painter.restore()
        tint=(c['on_action'] if active else navigation_ink if navigation else c['text'] if self.isChecked() else
              c['on_action'] if variant=='primary' else c['red'] if variant=='danger' else c['text'] if variant=='secondary' else c['secondary'])
        painter.setPen(QColor(tint if self.isEnabled() else c['muted']))
        text_x=x if trailing else x+size.width()+gap
        painter.drawText(QRectF(text_x,0,painter.fontMetrics().horizontalAdvance(text)+2,self.height()),Qt.AlignLeft|Qt.AlignVCenter,text)


class HoldRemoveButton(IconTextButton):
    """Complete removal only after a continuous, cancellable 750 ms hold."""
    confirmed=Signal()
    HOLD_MS=750

    def __init__(self,text,parent=None):
        super().__init__(text,parent)
        self._idle_text=text;self._hold_started=None;self._hold_identity=None
        self.identity=lambda:None
        self.setAccessibleName(text);self.setAccessibleDescription('按住 0.75 秒移出，提前松开取消。')
        self._hold_timer=QTimer(self);self._hold_timer.setSingleShot(True);self._hold_timer.setTimerType(Qt.PreciseTimer)
        self._hold_timer.timeout.connect(self._complete_hold)
        from display_frames import FramePulse
        self._hold_frames=FramePulse(self);self._hold_frames.timeout.connect(self.update)

    def _begin_hold(self):
        if self._hold_started is not None or not self.isEnabled():return
        self._hold_identity=self.identity();self._hold_started=time.monotonic()
        self.setDown(True);self.setText('松手取消');self._hold_timer.start(self.HOLD_MS)
        self._hold_frames.start();self.update()

    def cancel_hold(self):
        self._hold_timer.stop();self._hold_frames.stop();self._hold_started=None;self._hold_identity=None
        self.setDown(False);self.setText(self._idle_text);self.update()

    def _complete_hold(self):
        valid=(self._hold_started is not None and self.isEnabled() and self.isVisible()
               and self.identity()==self._hold_identity)
        self.cancel_hold()
        if valid:self.confirmed.emit()

    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:self._begin_hold();event.accept();return
        super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        if self._hold_started is not None and not self.rect().contains(event.position().toPoint()):self.cancel_hold()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton:self.cancel_hold();event.accept();return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self,event):
        if event.key() in (Qt.Key_Space,Qt.Key_Return,Qt.Key_Enter):
            if not event.isAutoRepeat():self._begin_hold()
            event.accept();return
        super().keyPressEvent(event)

    def keyReleaseEvent(self,event):
        if event.key() in (Qt.Key_Space,Qt.Key_Return,Qt.Key_Enter):
            if not event.isAutoRepeat():self.cancel_hold()
            event.accept();return
        super().keyReleaseEvent(event)

    def focusOutEvent(self,event):self.cancel_hold();super().focusOutEvent(event)
    def hideEvent(self,event):self.cancel_hold();super().hideEvent(event)
    def changeEvent(self,event):
        if event.type()==QEvent.EnabledChange and not self.isEnabled() and hasattr(self,'_hold_timer'):self.cancel_hold()
        super().changeEvent(event)

    def paintEvent(self,event):
        if self._hold_started is None:return super().paintEvent(event)
        from PySide6.QtWidgets import QStyle,QStylePainter
        from native_ui_common import button_style_option
        option=button_style_option(self);option.text='';option.icon=QIcon()
        p=QStylePainter(self);p.drawControl(QStyle.CE_PushButton,option)
        rect=QRectF(self.rect()).adjusted(1,1,-1,-1)
        fraction=max(0.,min(1.,(time.monotonic()-self._hold_started)*1000/self.HOLD_MS))
        bar=QRectF(rect.left(),rect.top(),rect.width()*fraction,rect.height())
        shape=QPainterPath();shape.addRoundedRect(rect,RADIUS_CONTROL_PX,RADIUS_CONTROL_PX)
        p.save();p.setRenderHint(QPainter.Antialiasing);p.setClipPath(shape)
        p.fillRect(bar,QColor(palette()['red']));p.restore()
        p.setFont(self.font());p.setPen(QColor(palette()['red']))
        p.drawText(QRectF(self.rect()),Qt.AlignCenter,self.text())
        p.save();p.setClipRect(bar);p.setPen(QColor('white'))
        p.drawText(QRectF(self.rect()),Qt.AlignCenter,self.text());p.restore();p.end()


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
            from display_frames import DisplayAnimation
            self._sort_animation=DisplayAnimation(self);self._sort_animation.setDuration(140)
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
        return tuple(c['sort_arrow_active'] if self.direction==value else c['sort_arrow_idle'] for value in (1,2))
    def paintEvent(self,event):
        from PySide6.QtWidgets import QStyleOptionButton,QStyle,QStylePainter
        from PySide6.QtSvg import QSvgRenderer
        from PySide6.QtCore import QByteArray
        from native_ui_common import button_style_option
        option=button_style_option(self);option.text='';option.icon=QIcon()
        p=QStylePainter(self);p.drawControl(QStyle.CE_PushButton,option);p.setFont(self.font())
        text_width=p.fontMetrics().horizontalAdvance(self.text())
        x=(self.width()-text_width-SORT_ARROW_GAP_PX-SORT_ARROW_SIZE_PX)/2
        c=palette();tint=c['text'] if self.isChecked() else c['secondary']
        p.setPen(QColor(tint if self.isEnabled() else c['muted']))
        p.drawText(QRectF(x,0,text_width,self.height()),Qt.AlignLeft|Qt.AlignVCenter,self.text())
        from native_ui_common import cached_svg_renderer
        values=getattr(self,'_sort_value',QPointF(float(self.direction==1),float(self.direction==2)))
        for index,path in enumerate(('M4 9 12 1 20 9Z','M4 15 12 23 20 15Z')):
            rect=QRectF(x+text_width+SORT_ARROW_GAP_PX,(self.height()-SORT_ARROW_SIZE_PX)/2,SORT_ARROW_SIZE_PX,SORT_ARROW_SIZE_PX)
            for tint,opacity in ((c['sort_arrow_idle'],1),(c['sort_arrow_active'],values.x() if index==0 else values.y())):
                if opacity<=0:continue
                svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="{tint}" d="{path}"/></svg>'
                p.save();p.setOpacity(opacity);cached_svg_renderer(svg).render(p,rect);p.restore()


class SegmentedControl(SelectionSurface):
    """Shared shell and button states for view and ordering controls."""
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setFixedHeight(SEGMENT_HEIGHT_PX)
        self.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Fixed)
        self.setObjectName('segmentedControl')
        css(self,f'QFrame#segmentedControl{{background:rgba(255,255,255,10);border:0;border-radius:{RADIUS_CONTROL_PX}px;}}',
            f'QFrame#segmentedControl{{background:rgba(0,0,0,10);border:0;border-radius:{RADIUS_CONTROL_PX}px;}}')
        layout=QHBoxLayout(self);layout.setContentsMargins(2,2,2,2);layout.setSpacing(0)

    def add_button(self,control,width=None):
        control.setCheckable(True);control.setFixedHeight(SEGMENT_BUTTON_HEIGHT_PX)
        padding=SORT_TEXT_PADDING_PX if isinstance(control,SortButton) else SEGMENT_TEXT_PADDING_PX
        sources=[]
        for theme in ('dark','light'):
            c=colors(theme);selected=c['card']
            sources.append(f"QPushButton{{background:transparent;border:1px solid transparent;border-radius:{RADIUS_SUBCONTROL_PX}px;color:{c['secondary']};padding:0 {padding}px;font:12px 'FlickDock UI';}} QPushButton:hover{{background:{c['control_hover']};}} QPushButton:checked,QPushButton:checked:hover,QPushButton:checked:pressed{{background:transparent;border-color:transparent;color:{c['text']};}} QPushButton:pressed:!checked{{background:{c['control_pressed']};}} QPushButton:disabled{{color:{c['muted']};}} QPushButton[keyboardFocus=\"true\"]{{border-color:{c['ring']};}}")
        css(control,*sources);control.ensurePolished()
        if width is None:
            width=control.fontMetrics().horizontalAdvance(control.text())+2*(padding+SEGMENT_BORDER_PX)+2
            if isinstance(control,SortButton):width+=SORT_ARROW_GAP_PX+SORT_ARROW_SIZE_PX
        control.setFixedWidth(width);self.layout().addWidget(control);self.add_selection_button(control)


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
        if variant=='primary':c.update({key:'@fd_'+key+'@' for key in ('action','on_action','action_hover','action_pressed')})
        background=c['control'] if variant in ('secondary','secondary_blue') else c['action'] if variant=='primary' else 'rgba(255,69,58,30)' if variant=='danger' else 'transparent'
        ink=c['on_action'] if variant=='primary' else c['red'] if variant=='danger' else c['link'] if variant in ('link','secondary_blue') else c['secondary'] if variant=='quiet' else c['text']
        border=c['border'] if variant=='secondary' else 'transparent'
        if variant=='primary':
            hover=c['action_hover'];pressed=c['action_pressed']
        elif variant=='danger':
            hover='rgba(255,69,58,58)';pressed='rgba(255,69,58,78)'
        elif variant in ('secondary','secondary_blue'):
            hover=c['control_hover'];pressed=c['control_pressed']
        else:hover=c['hover'];pressed=c['selected']
        sources.append(f"QPushButton{{background:{background};color:{ink};border:1px solid {border};border-radius:{RADIUS_CONTROL_PX}px;padding:4px 11px;font:500 12px 'FlickDock UI';}} QPushButton:hover:enabled{{background:{hover};}} QPushButton:pressed:enabled{{background:{pressed};}} QPushButton:checked{{background:{c['selected']};color:{c['text']};}} QPushButton:disabled{{color:{c['muted']};background:{c['control']};border-color:transparent;}} QPushButton[keyboardFocus=\"true\"]{{border:1px solid {c['ring']};}}")
    css(widget,*sources)
    if glyph:
        widget._reference_icon=(glyph,'secondary')
        widget.setIcon(icon(glyph,palette()['secondary']))
    return widget


def split_send_button(parent,height):
    """One primary action with a separate keyboard-accessible device menu."""
    frame=QFrame(parent);frame.setObjectName('splitSendControl');frame.setFixedHeight(height)
    frame.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
    css(frame,*[f"QFrame#splitSendControl{{background:@fd_action@;border:0;border-radius:8px;}} "
                 f"QFrame#splitSendControl:disabled{{background:{colors(t)['control']};}}" for t in ('dark','light')])
    layout=QHBoxLayout(frame);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
    primary=button('发送文件',variant='primary',parent=frame);primary.setFixedHeight(height)
    arrow=button('','chevron-down',variant='primary',parent=frame);arrow.setFixedSize(36,height)
    arrow.setIconSize(QSize(14,14));arrow.setProperty('splitDivider',True)
    arrow.setToolTip('选择同账号设备');arrow.setAccessibleName('选择同账号设备')
    for widget,right in ((primary,False),(arrow,True)):
        styles=[]
        for theme in ('dark','light'):
            c=colors(theme)
            c.update({key:'@fd_'+key+'@' for key in ('action','on_action','action_hover','action_pressed')})
            corners=('border-top-left-radius:0;border-bottom-left-radius:0;border-top-right-radius:8px;border-bottom-right-radius:8px;'
                     if right else 'border-top-left-radius:8px;border-bottom-left-radius:8px;border-top-right-radius:0;border-bottom-right-radius:0;')
            styles.append(f"QPushButton{{background:{c['action']};color:{c['on_action']};border:1px solid transparent;"
                          f"{corners}padding:4px 8px;font:400 12px 'FlickDock UI';}}"
                          f"QPushButton:hover:enabled{{background:{c['action_hover']};}} QPushButton:pressed:enabled{{background:{c['action_pressed']};}}"
                          f"QPushButton:disabled{{background:{c['control']};color:{c['muted']};}}"
                          f"QPushButton[keyboardFocus=\"true\"]{{border:1px solid {c['ring']};}}")
        css(widget,*styles)
    layout.addWidget(primary,1);layout.addWidget(arrow)
    return frame,primary,arrow


class ToolbarButton(FeedbackButton):
    """Retain the existing interruptible press spring with the HTML glyphs."""
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        c=palette();v=self.spring.values
        center=QRectF(self.rect()).center()
        p.translate(center);p.scale(v['scale']*self._presentation_scale[0],v['scale']*self._presentation_scale[1]);p.translate(-center)
        if self.isEnabled() and (self.isChecked() or v['hover']>.01 or v['down']>.01 or self.isDown()):
            fill=QColor(255,255,255,38) if self.property('inverseOnSelection') else ink(c['control_pressed'] if self.isDown() or v['down']>.1 else c['selected'] if self.isChecked() else c['control_hover'])
            if not self.isChecked():fill.setAlphaF(fill.alphaF()*min(1,max(v['hover']+v['down'],1. if self.isDown() else 0.)))
            p.setPen(Qt.NoPen);p.setBrush(fill);p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),8,8)
        if not self.isEnabled():p.setOpacity(float(self.property('disabledOpacity') or .4))
        name={'pin-filled':'pin','check':'check'}.get(self.glyph,self.glyph)
        tint=self._glyph_tint or (c['action'] if self.isChecked() and name=='pin' else c['blue'] if self.isChecked() else c['secondary'])
        p.save();p.setOpacity(p.opacity()*getattr(self,'_entry_alpha',1.));p.translate(getattr(self,'_entry_dx',0.),0.)
        if self.property('subtle'):p.setOpacity(p.opacity()*(.45+.4*v['hover']))
        hover=v['hover']
        if name=='cloud':p.translate(0,-hover)
        elif name in ('expand','collapse'):
            p.translate(center);p.rotate(6*hover if name=='expand' else -6*hover);p.translate(-center)
        elif name=='dock':p.translate(hover,0)
        icon(name,tint).paint(p,QRect((self.width()-16)//2,(self.height()-16)//2,16,16))
        p.restore()
        if self.hasFocus() and self._keyboard_focus:
            p.setPen(QPen(QColor('#FAFAFA' if self.property('inverseOnSelection') else c['link']),1.5));p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),7,7)
        p.end()


class LanButton(ToolbarButton):
    """One footer target for the LAN symbol and caption."""
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);c=palette()
        if self.isEnabled() and (self.underMouse() or self.isDown()):
            p.setPen(Qt.NoPen);p.setBrush(ink(c['selected'] if self.isDown() else c['hover']));p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),8,8)
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
        if self.isEnabled() and (self.underMouse() or self.isDown()):
            p.setPen(Qt.NoPen);p.setBrush(ink(c['selected'] if self.isDown() else c['hover']))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),8,8)
        p.setPen(Qt.NoPen);p.setBrush(QColor(c['action']) if initial else ink(c['control']));p.drawEllipse(QRectF(4,4,self.width()-8,self.height()-8))
        if initial:
            p.setPen(QColor(c['on_action']));f=p.font();f.setPixelSize(11);set_ui_weight(f,600);p.setFont(f);p.drawText(self.rect(),Qt.AlignCenter,initial)
        else:icon('user',c['secondary']).paint(p,QRect((self.width()-16)//2,(self.height()-16)//2,16,16))
        if self.hasFocus() and self._keyboard_focus:
            p.setPen(QPen(QColor(c['link']),1.5));p.setBrush(Qt.NoBrush);p.drawRoundedRect(QRectF(self.rect()).adjusted(2,2,-2,-2),7,7)
        p.end()


class ProfileButton(QPushButton):
    def __init__(self,parent=None):
        super().__init__(parent);self.username='未登录';self.status='登录以同步搁架';self.connected=False
        self.setCursor(Qt.PointingHandCursor);self.setFixedHeight(49)

    def set_profile(self,name,connected=False,status=''):
        self.username=str(name or '未登录');self.connected=bool(connected)
        self.status='' if connected else str(status or '登录以同步搁架');self.update()
        self.setAccessibleName(f'账号与设置，{self.username}，{self.status}')

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);c=palette()
        if self.isDown():fill=QColor(255,255,255,40) if ui_theme.current_theme()=='dark' else QColor(0,0,0,34)
        else:fill=ink(c['hover'] if self.underMouse() else c['control'])
        p.setPen(QPen(ink(c['border']),1));p.setBrush(fill)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5,1.5,-.5,-1.5),RADIUS_CARD_PX,RADIUS_CARD_PX)
        avatar=QRectF(8,(self.height()-28)/2,28,28)
        p.setBrush(QColor(c['action']) if self.connected else ink(c['control']));p.setPen(Qt.NoPen);p.drawEllipse(avatar)
        f=p.font();f.setPixelSize(14 if sys.platform=='win32' else 12);set_ui_weight(f,600);p.setFont(f);p.setPen(QColor(c['on_action']))
        if self.connected:p.drawText(avatar,Qt.AlignCenter,self.username[:1].upper())
        else:icon('user',c['secondary']).paint(p,QRect(14,round(avatar.center().y()-8),16,16))
        f.setPixelSize(13 if sys.platform=='win32' else 11)
        set_ui_weight(f,600);p.setFont(f);p.setPen(QColor(c['text']))
        name_height=p.fontMetrics().height()
        status_font=QFont(f);status_font.setPixelSize(10 if sys.platform=='win32' else 9);set_ui_weight(status_font,400)
        from PySide6.QtGui import QFontMetrics
        status_height=QFontMetrics(status_font).height()
        gap=2 if self.status else 0
        top=(self.height()-name_height-status_height-gap)/2-PROFILE_TEXT_RISE_PX if self.status else 0
        p.drawText(QRectF(46,top,self.width()-68,name_height if self.status else self.height()),Qt.AlignVCenter,p.fontMetrics().elidedText(self.username,Qt.ElideRight,self.width()-68))
        if self.status:
            p.setFont(status_font);p.setPen(QColor(c['secondary']));p.drawText(QRectF(46,top+name_height+gap,self.width()-68,status_height),Qt.AlignVCenter,p.fontMetrics().elidedText(self.status,Qt.ElideRight,self.width()-68))
        icon('right',c['muted']).paint(p,QRect(self.width()-20,(self.height()-12)//2,12,12))
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
        c=palette()
        if not self.isEnabled():p.setOpacity(.5)
        fill=QColor(c['action']) if self.isChecked() else QColor(c['hover'] if self.underMouse() or self.isDown() else c['card'])
        p.setBrush(fill)
        from native_ui_common import pixel_aligned_border
        border,stroke=pixel_aligned_border(p,QRectF(0,0,15,15))
        p.setPen(QPen(QColor(c['action']) if self.isChecked() else ink(c['ring'] if self.underMouse() else c['input_border']),stroke))
        p.drawRoundedRect(border,RADIUS_SMALL_PX,RADIUS_SMALL_PX)
        if self.isChecked():
            p.setPen(QPen(QColor(c['on_action']),1.5,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));p.drawLine(QPointF(4,7),QPointF(6,10));p.drawLine(QPointF(6,10),QPointF(11,4.5))
        if keyboard_focus_visible(self):
            p.setPen(QPen(QColor(c['ring']),1));p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(-1,-1,17,17),RADIUS_SMALL_PX+1,RADIUS_SMALL_PX+1)
        p.end()


class MarqueeSelection(QObject):
    """Blank-space selection keeps file drags and checkbox clicks intact."""
    def __init__(self,view):
        super().__init__(view);self.view=view;self.active=False;self.origin=QPoint();self.last=QPoint();self.base=set()
        self.band=QRubberBand(QRubberBand.Rectangle,view.scroll.viewport())
        from display_frames import FramePulse
        self.timer=FramePulse(self);self.timer.timeout.connect(self._scroll)
        self._scroll_time=0.;self._scroll_remainder=0.
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
            self.band.setGeometry(QRect());self.band.show();self._select();self._sync_auto_scroll();return True
        if kind==QEvent.MouseMove and self.active:
            self.last=event.position().toPoint();self._select();self._sync_auto_scroll();return True
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
        for item in view.rows_body.findChildren(QWidget,options=Qt.FindDirectChildrenOnly):
            if hasattr(item,'set_selected'):item.set_selected(item.path in view.selected)
            else:item.update()
        view.update_selection_actions();view.shelf.touch()

    def _scroll_direction(self):
        viewport=self.view.scroll.viewport();point=self.view.rows_body.mapTo(viewport,self.last)
        return -1 if point.y()<20 else 1 if point.y()>viewport.height()-20 else 0

    def _sync_auto_scroll(self):
        if self.active and self._scroll_direction():
            if not self.timer.isActive():
                self._scroll_time=time.perf_counter();self._scroll_remainder=0.;self.timer.start()
        else:self.timer.stop();self._scroll_remainder=0.

    def _scroll(self):
        if not self.active:return
        direction=self._scroll_direction();now=time.perf_counter()
        distance=min(.05,max(0.,now-self._scroll_time))*600+self._scroll_remainder;self._scroll_time=now
        delta=int(distance);self._scroll_remainder=distance-delta
        if direction and delta:
            bar=self.view.scroll.verticalScrollBar();before=bar.value();bar.setValue(before+direction*delta)
            moved=bar.value()-before
            if moved:self.last+=QPoint(0,moved);self._select()
            else:self.timer.stop()


class TransferStrip(QFrame):
    def __init__(self,view):
        super().__init__(view.middle);self.view=view;self.progress_fraction=None;self.paint_fraction=None;self.setCursor(Qt.PointingHandCursor)
        from display_frames import DisplayAnimation
        from PySide6.QtCore import QEasingCurve
        self.progress_animation=DisplayAnimation(self);self.progress_animation.setDuration(220)
        self.progress_animation.setEasingCurve(QEasingCurve.OutCubic)
        self.progress_animation.valueChanged.connect(self._progress_frame)
    def set_progress(self,value):
        value=max(0.,min(1.,float(value))) if value is not None else None
        if value==self.progress_fraction:return
        previous=self.paint_fraction;self.progress_fraction=value;self.progress_animation.stop()
        from ui_transitions import motion_allowed
        if value is None or previous is None or not self.isVisible() or not self.view.shelf.manager.motion or not motion_allowed():
            self._progress_frame(value);return
        self.progress_animation.setStartValue(previous);self.progress_animation.setEndValue(value);self.progress_animation.start()
    def _progress_frame(self,value):
        self.paint_fraction=None if value is None else max(0.,min(1.,float(value)))
        for _,name,percent,_ in getattr(self.view,'strip_rows',()):
            percent.setVisible(self.paint_fraction is not None)
            percent.setText(f'{round(self.paint_fraction*100)}%' if self.paint_fraction is not None else '')
            name.update();percent.update()
        self.update()
    def paintEvent(self,event):
        c=palette();p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        rect=QRectF(self.rect()).adjusted(.5,.5,-.5,-.5)
        path=QPainterPath();path.addRoundedRect(rect,8,8)
        p.fillPath(path,QColor(c['control']))
        if self.paint_fraction:
            p.save();p.setClipPath(path)
            p.fillRect(QRectF(rect.left(),rect.top(),rect.width()*self.paint_fraction,rect.height()),QColor(c['transfer_progress_fill']))
            p.restore()
        p.setPen(QPen(ink(c['border']),1));p.setBrush(Qt.NoBrush);p.drawPath(path);p.end()
    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton:self.view.set_category('uploading');event.accept()
        else:super().mouseReleaseEvent(event)


def _draw_transfer_ink(painter,widget,draw):
    draw(None)
    host=widget.parentWidget()
    while host is not None and not isinstance(host,TransferStrip):host=host.parentWidget()
    if host is None or not host.paint_fraction:return
    origin=widget.mapTo(host,QPoint())
    width=host.width()*host.paint_fraction-origin.x()
    if width<=0:return
    painter.save();painter.setClipRect(QRectF(0,0,width,widget.height()))
    draw(palette()['on_action']);painter.restore()


class TransferPercentLabel(QLabel):
    def paintEvent(self,event):
        p=QPainter(self);p.setFont(self.font())
        def draw(tint):
            p.setPen(QColor(tint or palette()['text']))
            x=self.width()-p.fontMetrics().horizontalAdvance(self.text())
            p.drawText(QPointF(x,optical_text_baseline(p.font(),self.text(),self.height())),self.text())
        _draw_transfer_ink(p,self,draw);p.end()


class TransferNameButton(QPushButton):
    def set_transfer(self,phase,title):
        self.phase,self.title=phase,title;self.setText(phase+' '+title)
    def paintEvent(self,event):
        p=QPainter(self)
        def draw(tint):
            p.setFont(self.font());p.setPen(QColor(tint or palette()['transfer_status_text']))
            phase=getattr(self,'phase','');width=p.fontMetrics().horizontalAdvance(phase+' ')
            p.drawText(QPointF(0,optical_text_baseline(p.font(),phase,self.height())),phase)
            font=p.font();set_ui_weight(font,600);p.setFont(font);p.setPen(QColor(tint or palette()['text']))
            title=p.fontMetrics().elidedText(getattr(self,'title',''),Qt.ElideRight,max(0,self.width()-width))
            p.drawText(QPointF(width,optical_text_baseline(p.font(),title,self.height())),title)
        _draw_transfer_ink(p,self,draw)
        p.end()


class OpticalTextButton(IconTextButton):
    """Keep button feedback while centering the visible text alongside icons."""
    def paintEvent(self,event):
        from PySide6.QtWidgets import QStyle,QStylePainter
        from native_ui_common import button_style_option
        option=button_style_option(self);option.text=''
        p=QStylePainter(self);p.drawControl(QStyle.CE_PushButton,option)
        p.setFont(self.font());p.setPen(self.palette().color(QPalette.ButtonText))
        text=p.fontMetrics().elidedText(self.text(),Qt.ElideRight,max(0,self.width()-8))
        x=(self.width()-p.fontMetrics().horizontalAdvance(text))/2
        p.drawText(QPointF(x,optical_text_baseline(p.font(),text,self.height())),text);p.end()


class RenameDialog(QDialog):
    """A presentation-only sheet reusing the shelf's existing rename editor."""
    def __init__(self,shelf):
        super().__init__(shelf,Qt.Dialog|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        from collections import OrderedDict
        self.shelf=shelf;self._shadow_cache=OrderedDict()
        self.setAttribute(Qt.WA_TranslucentBackground);self.setWindowModality(Qt.WindowModal)
        self.setWindowTitle('重命名文件');self.setFixedSize(420,252)
        layout=QVBoxLayout(self);layout.setContentsMargins(20,20,20,20)
        self.card=QFrame(self);self.card.setObjectName('renameSheet')
        c=palette();css(self.card,f"QFrame#renameSheet{{background:{colors('dark')['surface']};border:1px solid rgba(255,255,255,20);border-radius:{c['radius']}px;}}",
            f"QFrame#renameSheet{{background:#F6F6F6;border:1px solid rgba(0,0,0,23);border-radius:{c['radius']}px;}}")
        form=QVBoxLayout(self.card);form.setContentsMargins(20,20,20,20);form.setSpacing(16)
        header=QHBoxLayout();header.addWidget(label('重命名文件',14,600,parent=self.card),1)
        close=ToolbarButton('close',self.card,motion=shelf.manager.motion);close.setFixedSize(32,32);close.setAutoDefault(False);close.clicked.connect(self.reject);header.addWidget(close);form.addLayout(header)
        form.addWidget(label('修改扩展名可能导致文件不可用',12,400,'secondary',self.card))
        edit=shelf.rename_edit;edit.setParent(self.card);edit.setFixedHeight(34)
        css(edit,*[f"QLineEdit{{background:{colors(t)['input']};color:{colors(t)['text']};border:1px solid {colors(t)['input_border']};border-radius:8px;padding:7px 10px;font:12px 'FlickDock UI';}} QLineEdit:focus{{border-color:{colors(t)['ring']};}}" for t in ('dark','light')])
        form.addWidget(edit);actions=QHBoxLayout();actions.addStretch(1)
        cancel=button('取消',variant='secondary',parent=self.card);cancel.setAutoDefault(False);cancel.clicked.connect(self.reject)
        self.confirm=button('确定',variant='primary',parent=self.card);self.confirm.clicked.connect(self._commit)
        self.confirm.setAutoDefault(True);self.confirm.setDefault(True)
        actions.addWidget(cancel);actions.addWidget(self.confirm);form.addLayout(actions);layout.addWidget(self.card)
        edit.editingFinished.disconnect(shelf.commit_rename)
        edit.returnPressed.disconnect(shelf.commit_rename)
        self.rejected.connect(edit.hide)

    def _commit(self):
        if self.shelf.commit_rename():self.accept()

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


def item_age(shelf,ref):
    import time
    stamp=shelf.content.metadata.get(ref,{}).get('added_at')
    if stamp is None and not shelf.content.is_text(ref):
        cache=getattr(shelf,'file_metadata',None);info=cache.request(ref) if cache is not None else None
        if info and info.get('mtime_ns'):stamp=info['mtime_ns']/1e9
    if not isinstance(stamp,(int,float)) or not 0<stamp<=time.time():return ''
    seconds=max(0,int(time.time()-stamp))
    return '刚刚' if seconds<60 else f'{seconds//60} 分钟前' if seconds<3600 else f'{seconds//3600} 小时前' if seconds<86400 else f'{seconds//86400} 天前'


STATUS_COLORS={'synced':'#30D158','uploading':'#0A84FF','pending':'#64D2FF','local':'#A1A1AA','paused':'#FF9F0A','error':'#FF453A'}


def draw_status(p,rect,status,transfer=None):
    from transfer_visuals import draw_cloud_status_badge
    draw_cloud_status_badge(p,QRectF(rect),status,transfer)


def invalidate_thumbnail(shelf,ref):
    cache=getattr(shelf,'_reference_thumbnail_cache',None)
    if cache is not None:
        for key in tuple(cache):
            if key[0]==ref:cache.pop(key,None)


def thumbnail(shelf,ref,size=40):
    # Artwork is static across scroll frames. Status badges remain live.
    store=getattr(shelf,'_reference_thumbnail_cache',None)
    if store is None:store=shelf._reference_thumbnail_cache=OrderedDict()
    key=(ref,int(size),ui_theme.current_theme())
    if key in store:
        store.move_to_end(key);return store[key]
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
        font=reference_font();font.setPixelSize(max(8,round(size*.23)));set_ui_weight(font,600);p.setFont(font);p.setPen(QColor('white' if name in ('PDF','ZIP') else palette()['on_action']))
        p.drawText(QRectF(size*.1,size*.3,size*.8,size*.4),Qt.AlignCenter,name[:4])
    p.end();store[key]=pm
    while len(store)>256:store.popitem(last=False)
    return pm


class ThumbnailLabel(TransferIconLabel):
    def __init__(self,shelf,ref,parent=None):
        super().__init__(shelf.content,ref,parent)
        self.shelf=shelf;self.ref=ref;self.setFixedSize(40,40)

    def paintEvent(self,event):
        if outside_content_view(self):return
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        size=min(self.width(),self.height());art=thumbnail(self.shelf,self.ref,size)
        p.drawPixmap(0,0,art)
        draw_status(p,QRectF(size-18,size-18,18,18),self.cloud_status,self.repository.transfers.get(self.ref))
        p.end()


class MiniStack(StackTile):
    def __init__(self,shelf,parent=None,motion=True):
        super().__init__(parent,motion)
        self.shelf=shelf
        self._card_art={};self._card_dpr=0;self._card_theme=None
        from native_ui_common import DropBorderMotion
        self.border_motion=DropBorderMotion(self)

    def set_hover(self,value):
        super().set_hover(value)
        self.border_motion.set_active(value and not self.refs)

    def hideEvent(self,event):
        self._hovered=False;self.border_motion.set_active(False)
        super().hideEvent(event)

    def set_stack(self,repository,refs,preview_cache=None):
        super().set_stack(repository,refs,preview_cache)
        self.border_motion.set_active(self._hovered and not self.refs)
        self._cache_cards()

    def _cache_cards(self):
        ratio=self.devicePixelRatioF();cards={};content=self.shelf.content
        for index,ref in enumerate(self.refs):
            depth=len(self.refs)-1-index
            metadata=getattr(self.shelf,'file_metadata',None)
            info=metadata.request(ref) if metadata is not None and not content.is_text(ref) else None
            cached=self.shelf.preview_cache.get(ref);cache=getattr(self.shelf,'image_cache',None)
            cover=content.metadata.get(ref,{}).get('preview_image')
            decoded=cache.image(cover or ref,128) if cache is not None else None
            directory=getattr(self.shelf,'_directory_sizes',{}).get(ref)
            key=(content.title(ref),len(content.text(ref) or '') if content.is_text(ref) else None,
                 tuple(info.get(key) for key in ('size','mtime_ns','exists','error','is_dir')) if info else None,
                 directory[1] if directory else None,content.transfers.get(ref,{}).get('total'),
                 cached.cacheKey() if cached is not None else None,decoded.cacheKey() if decoded is not None else None,
                 cover,ratio,self.font().toString(),ui_theme.current_theme(),depth)
            previous=self._card_art.get(ref)
            cards[ref]=previous if previous is not None and previous[0]==key else (key,self._render_card(ref,ratio,depth))
        self._card_art=cards;self._card_dpr=ratio;self._card_theme=ui_theme.current_theme()

    def _render_card(self,ref,ratio,depth):
        result=QPixmap(round(192*ratio),round(128*ratio));result.setDevicePixelRatio(ratio);result.fill(Qt.transparent)
        p=QPainter(result);p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform);c=palette()
        p.setPen(QPen(QColor(255,255,255,38) if ui_theme.current_theme()=='dark' else QColor('#D4D4D4'),1))
        p.setBrush(QColor(c['stack_cards'][depth]));p.drawRoundedRect(QRectF(.5,.5,191,127),RADIUS_PANEL_PX,RADIUS_PANEL_PX)
        p.drawPixmap(14,14,thumbnail(self.shelf,ref,46))
        title=self.shelf.content.title(ref);f=QFont(self.font());f.setPixelSize(13 if sys.platform=='win32' else 11);set_ui_weight(f,600);p.setFont(f);p.setPen(QColor(c['text']))
        p.drawText(QRectF(14,77,164,20),Qt.AlignVCenter,p.fontMetrics().elidedText(title,Qt.ElideRight,164))
        f.setPixelSize(11 if sys.platform=='win32' else 9);set_ui_weight(f,400);p.setFont(f);p.setPen(QColor(c['file_description']));p.drawText(QRectF(14,99,164,18),Qt.AlignVCenter,item_detail(self.shelf,ref).split(' · ')[0])
        ext=Path(title).suffix.lstrip('.').upper() or ('TEXT' if self.shelf.content.is_text(ref) else 'FILE')
        p.setPen(QColor(c['secondary']))
        p.drawText(QRectF(100,14,78,18),Qt.AlignRight|Qt.AlignVCenter,ext[:7]);p.end();return result

    def presentation_cards(self):
        v=self.spring.values;spread=self.spread.values['spread']
        center=QPointF(self.width()/2,self.height()/2)
        base=QTransform();base.translate(center.x(),center.y()+v['y']);base.scale(v['sx'],v['sy']);base.translate(-center.x(),-center.y())
        refs=list(reversed(self.refs));frames=[]
        transforms=((0,1,0,1),(-7,.95,-2,.9),(-14,.9,2.2,.75),(-20,.85,-1.5,.58),(-26,.8,1,.4))
        for idx in reversed(range(len(refs))):
            dy,scale,angle,opacity=transforms[idx]
            local=QTransform();local.translate(center.x()-idx*FAN_LAYER_OFFSET_X_PX*spread,center.y()+dy+idx*MINI_FAN_LAYER_OFFSET_Y_PX*spread)
            local.rotate(angle+spread*(FAN_FRONT_ANGLE_DEGREES if idx==0 else -idx*FAN_LAYER_ANGLE_DEGREES));local.scale(scale,scale);local.translate(-96,-64)
            if idx==0:local.translate(0,-STACK_ARRIVAL_OFFSET_PX*(1-self.arrival))
            frames.append((refs[idx],local*base,opacity if ui_theme.current_theme()=='dark' else 1.,idx==0))
        return frames

    def paint_card(self,painter,ref,front):
        painter.drawPixmap(0,0,self._card_art[ref][1])
        sync=getattr(self.shelf.manager,'sync',None)
        state=getattr(self.repository,'transfers',{}).get(ref)
        draw_status(painter,QRectF(42,42,18,18),sync.sync_status_for_ref(ref) if hasattr(sync,'sync_status_for_ref') else 'local',state)
        from transfer_visuals import thumbnail_transfer_progress_visible
        if front and thumbnail_transfer_progress_visible(state):
            from transfer_visuals import draw_progress_badge
            draw_progress_badge(painter,QRectF(162,40,16,16),state)

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
        if not self.refs:
            r=QRectF((self.width()-176)/2,28,176,128)
            path=QPainterPath();path.addRoundedRect(r,EMPTY_DROP_RADIUS_PX,EMPTY_DROP_RADIUS_PX)
            self.border_motion.paint(p,path,dark=ui_theme.current_theme()=='dark',idle_fill=ink(palette()['card']))
            icon('upload',palette()['secondary']).paint(p,QRect(round(r.center().x()-12),round(r.center().y()-31),24,24))
            f=p.font()
            f.setPixelSize(13);p.setFont(f);p.setPen(QColor(palette()['text']));p.drawText(r.adjusted(0,34,0,0),Qt.AlignCenter,'点击选择文件');p.end();return
        if self._card_dpr!=self.devicePixelRatioF() or self._card_theme!=ui_theme.current_theme():self._cache_cards()
        for ref,transform,opacity,front in self.presentation_cards():
            p.save();p.setOpacity(opacity);p.setTransform(transform,True)
            self.paint_card(p,ref,front);p.restore()
        p.end()


def build_overview(view,preview_type):
    """Build the HTML's 52px toolbar and 210 / flexible / 340 split view."""
    view.setObjectName('referenceOverview')
    css(view,*[f"QFrame#referenceOverview{{background:{colors(t)['background']};border:0;}}" for t in ('dark','light')])
    root=QVBoxLayout(view);root.setContentsMargins(0,0,0,0);root.setSpacing(0)
    view.header=QFrame(view);view.header.setFixedHeight(SHELF_OVERVIEW_HEADER_HEIGHT_PX)
    css(view.header,*[f"QFrame{{background:{colors(t)['surface']};border:0;border-bottom:1px solid {colors(t)['divider']};}}" for t in ('dark','light')])
    head=QHBoxLayout(view.header);head.setContentsMargins(0,0,0,0);head.setSpacing(0)
    view.header_brand=QWidget(view.header);view.header_brand.setFixedWidth(210)
    # The overview header is slightly taller than the compact one; align the
    # logo and title with the fixed header-button axis in both modes.
    brand=QHBoxLayout(view.header_brand);brand.setContentsMargins(16,8,0,13);brand.setSpacing(9)
    from flickdock_icons import app_icon
    view.brand_logo=QLabel(view.header);view.brand_logo.setFixedSize(24,24)
    view.brand_logo.setPixmap(app_icon().pixmap(QSize(24,24),view.devicePixelRatioF()));brand.addWidget(view.brand_logo)
    view.title=label('FlickDock',14,600,parent=view.header,control_type=OpticalLabel)
    # The brand is the one deliberate exception to the softer UI emphasis.
    view.title.setProperty('flickdockFontWeight',600)
    view.title._reference_style=tuple(style+'\nfont-weight:600;' for style in view.title._reference_style)
    view.title.setStyleSheet(view.title._reference_style[ui_theme.current_theme()=='light'])
    brand.addWidget(view.title)
    view.count_label=label('',10,600,'secondary',view.header);view.count_label.setAlignment(Qt.AlignCenter)
    css(view.count_label,"color:#D4D4D8;background:rgba(255,255,255,28);border:0;border-radius:10px;padding:1px 9px;font:600 10px 'FlickDock UI';",
        "color:#515154;background:rgba(0,0,0,18);border:0;border-radius:10px;padding:1px 9px;font:600 10px 'FlickDock UI';")
    view.count_label.setFixedHeight(20);brand.addWidget(view.count_label);brand.addStretch();head.addWidget(view.header_brand)
    view.header_tools=QWidget(view.header)
    tools=QHBoxLayout(view.header_tools);tools.setContentsMargins(16,10,12,10);tools.setSpacing(9);tools.addStretch()
    view.search_edit=QLineEdit(view.header);view.search_edit.setPlaceholderText('搜索...');view.search_edit.setFixedSize(176,32)
    view.search_edit.setClearButtonEnabled(True)
    view.search_edit.setAccessibleName('搜索搁架内容');view.search_edit.textChanged.connect(lambda _:view.rebuild())
    css(view.search_edit,*[f"QLineEdit{{background:{colors(t)['input']};color:{colors(t)['text']};border:1px solid {colors(t)['input_border']};border-radius:8px;padding:4px 10px;font:12px 'FlickDock UI';}} QLineEdit:focus{{border-color:{colors(t)['ring']};}} QLineEdit:disabled{{color:{colors(t)['muted']};background:{colors(t)['control']};}}" for t in ('dark','light')])
    tools.addWidget(view.search_edit)
    view.view_segment=SegmentedControl(view.header_tools)
    view.view_list=button('','list',parent=view.view_segment);view.view_grid=button('','grid',parent=view.view_segment)
    view.view_list.setToolTip('列表视图');view.view_list.setAccessibleName('列表视图')
    view.view_grid.setToolTip('卡片视图');view.view_grid.setAccessibleName('卡片视图')
    view.view_buttons=QButtonGroup(view);view.view_buttons.setExclusive(True)
    for control,key in ((view.view_list,'list'),(view.view_grid,'icons')):
        view.view_segment.add_button(control,34);view.view_buttons.addButton(control)
        control.clicked.connect(lambda checked=False,key=key:view.change_view_mode(key))
        control.setChecked(view.shelf.manager.settings.view_mode==key)
    tools.addWidget(view.view_segment);head.addWidget(view.header_tools,1)
    view.header_status=QWidget(view.header);view.header_status.setFixedWidth(340)
    head.addWidget(view.header_status)
    root.addWidget(view.header)
    body=QHBoxLayout();body.setContentsMargins(0,0,0,0);body.setSpacing(0);view.body_layout=body
    view.sidebar=SelectionSurface(view,vertical=True,color_role='action',border_role=None,radius=RADIUS_CONTROL_PX,
                                  motion=lambda:getattr(view.shelf.manager,'motion',True))
    view.sidebar.setObjectName('referenceSidebar');view.sidebar.setFixedWidth(210)
    css(view.sidebar,*[f"QFrame#referenceSidebar{{background:{colors(t)['sidebar']};border:0;border-right:1px solid {colors(t)['divider']};}}" for t in ('dark','light')])
    side=QVBoxLayout(view.sidebar);side.setContentsMargins(10,10,10,10);side.setSpacing(2)
    view.category_group=QButtonGroup(view);view.category_group.setExclusive(True);view.category_buttons={};view.sidebar_buttons={};view.nav_counts={}
    glyphs={'all':'all','images':'image','video':'video','audio':'audio','text':'text','files':'file','links':'web','uploading':'transferring'}
    for key,title in view.CATEGORIES:
        control=button(title,glyphs[key],parent=view.sidebar);control.setCheckable(True);control.setFixedHeight(34)
        control.setProperty('leftAligned',True)
        css(control,*[f"QPushButton{{background:transparent;color:{colors(t)['secondary']};border:1px solid transparent;border-radius:{RADIUS_CONTROL_PX}px;text-align:left;padding:6px 12px;font:400 12px 'FlickDock UI';}} QPushButton:hover{{background:{colors(t)['hover']};}} QPushButton:checked,QPushButton:checked:hover{{background:transparent;color:@fd_on_action@;font-weight:400;}} QPushButton[keyboardFocus=\"true\"]{{border-color:{colors(t)['ring']};}}" for t in ('dark','light')])
        control.clicked.connect(lambda checked=False,key=key:view.set_category(key));view.category_group.addButton(control);side.addWidget(control);view.sidebar.add_selection_button(control)
        count=label('',11,400,'muted',control);count.setAlignment(Qt.AlignCenter);count.setGeometry(145,7,28,20);count.setAttribute(Qt.WA_TransparentForMouseEvents)
        view.nav_counts[key]=count;view.category_buttons[key]=control;view.sidebar_buttons[key]=control
    view.transfer_badge=view.nav_counts['uploading']
    side.addStretch(1)
    view.wan_nav=button('远程传输','web','secondary',view.sidebar);view.wan_nav.hide()
    view.video_nav=button('视频解析','video',variant='text',parent=view.sidebar);view.video_nav.setFixedHeight(34);view.video_nav.clicked.connect(lambda:view.shelf.manager.show_video_dialog());side.addWidget(view.video_nav)
    view.lan_nav=button('文件互传','nearby',variant='text',parent=view.sidebar);view.lan_nav.setFixedHeight(34);view.lan_nav.clicked.connect(lambda:view.shelf.manager.show_transfer_dialog());side.addWidget(view.lan_nav)
    view.settings_nav=button('设置','settings',variant='text',parent=view.sidebar);view.settings_nav.setFixedHeight(34);view.settings_nav.clicked.connect(lambda:view.shelf.manager.show_settings());side.addWidget(view.settings_nav)
    for control in (view.video_nav,view.lan_nav,view.settings_nav):control.setProperty('sidebarUtility',True)
    for control in (view.wan_nav,view.video_nav,view.lan_nav,view.settings_nav):
        control.setProperty('leftAligned',True)
        old=control._reference_style
        css(control,*[s.replace('padding:4px 11px;','padding:4px 12px;text-align:left;') for s in old])
    view.account_nav=ProfileButton(view.sidebar);view.account_nav.clicked.connect(lambda:view.shelf.manager.show_settings());side.addWidget(view.account_nav)
    body.addWidget(view.sidebar)
    view.middle=QWidget(view);mid=QVBoxLayout(view.middle);view.middle_layout=mid;mid.setContentsMargins(0,0,0,0);mid.setSpacing(0)
    view.selection_bar=QFrame(view.middle);view.selection_bar.setFixedHeight(40)
    css(view.selection_bar,*[f"QFrame{{border:0;border-bottom:1px solid {colors(t)['divider']};background:{colors(t)['background']};}}" for t in ('dark','light')])
    selection=QVBoxLayout(view.selection_bar);selection.setContentsMargins(0,0,0,0);selection.setSpacing(0)
    view.selection_navigation=QWidget(view.selection_bar);view.selection_navigation.setFixedHeight(36)
    navigation=QHBoxLayout(view.selection_navigation);navigation.setContentsMargins(16,0,12,0);navigation.setSpacing(8)
    selection.addWidget(view.selection_navigation);view.selection_navigation.hide()
    view.selection_actions=QWidget(view.selection_bar);view.selection_actions.setFixedHeight(40);selection.addWidget(view.selection_actions)
    select=QHBoxLayout(view.selection_actions);select.setContentsMargins(12,0,12,0);select.setSpacing(6)
    tools.removeWidget(view.search_edit);view.search_edit.setParent(view.selection_actions)
    view.search_edit.setMinimumWidth(90);view.search_edit.setMaximumWidth(16777215);select.addWidget(view.search_edit,1)
    view.sort_segment=SegmentedControl(view.selection_actions);view.sort_segment.setAccessibleName('排序方式')
    view.sort_buttons={}
    for key,title in (('time','时间'),('size','大小')):
        control=button(title,parent=view.sort_segment,control_type=SortButton)
        control.setAccessibleName('按'+title+'排序，正序、倒序、默认循环')
        control.clicked.connect(lambda _,key=key:view.cycle_sort(key))
        view.sort_buttons[key]=control;view.sort_segment.add_button(control)
    select.addWidget(view.sort_segment)
    tools.removeWidget(view.view_segment);view.view_segment.setParent(view.selection_actions);select.addWidget(view.view_segment)
    view.zip_button=button('','package-selected',parent=view.selection_actions);view.zip_button.clicked.connect(view.compress_selected);select.addWidget(view.zip_button)
    view.zip_all_button=button('','package',parent=view.selection_actions);view.zip_all_button.clicked.connect(lambda:view.shelf.compress_refs(list(view.shelf.files['permanent'])));select.addWidget(view.zip_all_button)
    view.clear_button=button('','trash',parent=view.selection_actions);view.clear_button.clicked.connect(view.shelf.confirm_delete);select.addWidget(view.clear_button)
    for control,title in ((view.zip_button,'打包所选'),(view.zip_all_button,'打包全部'),(view.clear_button,'删除项目')):
        control.setFixedSize(32,32);control.setProperty('iconOnly',True);control.setToolTip(title);control.setAccessibleName(title)
        control._reference_style=tuple(s+' QPushButton:disabled{background:transparent;border-color:transparent;}' for s in control._reference_style)
        control.setStyleSheet(control._reference_style[ui_theme.current_theme()=='light'])
    view.clear_button._reference_icon=('trash','red');view.clear_button.setIcon(icon('trash',palette()['red']))
    view.select_all_button=button('全选',parent=view.selection_bar);view.select_all_button.clicked.connect(view.toggle_select_all_visible);select.addWidget(view.select_all_button)
    view.select_all_button.setFixedWidth(44)
    view.paste_button=button('粘贴',parent=view.selection_bar);view.paste_button.hide()
    mid.addWidget(view.selection_bar)
    view.transfer_summary=QWidget(view.middle);summary=QHBoxLayout(view.transfer_summary);summary.setContentsMargins(16,10,16,10)
    view.transfer_count=label('',12,600,parent=view.transfer_summary);view.transfer_eta=label('',11,400,'link',view.transfer_summary)
    view.transfer_bar=QProgressBar(view.transfer_summary);view.transfer_bar.setTextVisible(False);view.transfer_bar.setFixedHeight(6)
    css(view.transfer_bar,*[f"QProgressBar{{border:0;background:{colors(t)['control']};border-radius:3px;}} QProgressBar::chunk{{background:@fd_action@;border-radius:3px;}}" for t in ('dark','light')])
    summary.addWidget(view.transfer_count);summary.addWidget(view.transfer_bar,1);summary.addWidget(view.transfer_eta);mid.addWidget(view.transfer_summary);view.transfer_summary.hide()
    view.scroll=QScrollArea(view.middle);view.scroll.setWidgetResizable(True);view.scroll.setFrameShape(QFrame.NoFrame);view.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    css(view.scroll,'QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::handle:vertical{background:#49494E;border-radius:3px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}',
        'QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::handle:vertical{background:#C8C8C8;border-radius:3px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}')
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
    css(view.empty_title,"color:#F5F5F7;background:transparent;border:0;font:600 14px 'FlickDock UI';","color:#1D1D1F;background:transparent;border:0;font:600 14px 'FlickDock UI';")
    css(view.empty_hint,"color:#A1A1AA;background:transparent;border:0;font:12px 'FlickDock UI';","color:#636366;background:transparent;border:0;font:12px 'FlickDock UI';")
    for control in (view.empty_symbol,view.empty_title,view.empty_hint):control.setAttribute(Qt.WA_TransparentForMouseEvents)
    view.empty_hint.setText('拖入内容，或点击选择文件');view.empty_hint.show()
    empty.addWidget(view.empty_drop,0,Qt.AlignCenter)
    empty.addStretch(1);mid.addWidget(view.empty_state,1);view.empty_state.hide()
    view.transfer_strip=TransferStrip(view)
    view.transfer_strip.setObjectName('overviewTransferPill');view.transfer_strip.setFixedHeight(32)
    view.transfer_strip.setMinimumWidth(0);view.transfer_strip.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
    css(view.transfer_strip,'QFrame#overviewTransferPill{background:transparent;border:0;}')
    strip=QVBoxLayout(view.transfer_strip);strip.setContentsMargins(12,0,12,0);strip.setSpacing(0)
    top=QHBoxLayout();view.strip_title=label('传输中',11,500,'secondary',view.transfer_strip)
    view.strip_more=button('',parent=view.transfer_strip,control_type=OpticalTextButton)
    view.strip_more.setFixedHeight(28);view.strip_more.clicked.connect(lambda:view.set_category('uploading'))
    view.strip_title.hide()
    view.strip_rows=[];view._strip_refs=[]
    for index in range(1):
        row=QWidget(view.transfer_strip);line=QHBoxLayout(row);line.setContentsMargins(0,0,0,0);line.setSpacing(5)
        name=TransferNameButton(row);name.setFixedHeight(30);name.setCursor(Qt.PointingHandCursor)
        css(name,"color:#F5F5F7;background:transparent;border:0;font:12px 'FlickDock UI';", "color:#1D1D1F;background:transparent;border:0;font:12px 'FlickDock UI';")
        name.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed);name.clicked.connect(lambda checked=False,index=index:view._open_strip_transfer(index))
        percent=label('',10,400,'text',row,control_type=TransferPercentLabel);percent.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        percent.setFixedWidth(percent.fontMetrics().horizontalAdvance('100%')+2)
        progress=QProgressBar(row);progress.setFixedSize(42,3);progress.setTextVisible(False)
        css(progress,'QProgressBar{background:#343438;border:0;} QProgressBar::chunk{background:@fd_action@;}',
            'QProgressBar{background:#E0E0E0;border:0;} QProgressBar::chunk{background:@fd_action@;}')
        line.addWidget(name,1);line.addWidget(view.strip_more);line.addWidget(percent);progress.hide();strip.addWidget(row);view.strip_rows.append((row,name,percent,progress))
    # Keep capacity, clipboard and transfer progress on one fixed-height row.
    from clipboard_preview import ClipboardButton
    view.clipboard_footer=QWidget(view.middle);view.clipboard_footer.setObjectName('overviewFooter');view.clipboard_footer.setFixedHeight(56)
    css(view.clipboard_footer,*[f"QWidget#overviewFooter{{background:{colors(t)['background']};border:0;border-top:1px solid {colors(t)['divider']};}}" for t in ('dark','light')])
    clipboard_row=QHBoxLayout(view.clipboard_footer);clipboard_row.setContentsMargins(12,12,12,12)
    view.clipboard_button=ClipboardButton(view.shelf.manager,view.clipboard_footer)
    clipboard_row.setSpacing(8)
    from shelf_capacity import ShelfCapacityButton
    view.capacity_button=ShelfCapacityButton(view.clipboard_footer,show_value=True)
    clipboard_row.addWidget(view.capacity_button);clipboard_row.addWidget(view.clipboard_button)
    clipboard_row.addWidget(view.transfer_strip,1);clipboard_row.addStretch()
    sync=getattr(view.shelf.manager,'sync',None)
    if sync is not None:
        view.capacity_button.bind_sync(sync)
    mid.addWidget(view.clipboard_footer)
    view.transfer_strip.hide();body.addWidget(view.middle,1)
    view.preview=preview_type(view);view.preview.setFixedWidth(340);body.addWidget(view.preview)
    root.addLayout(body,1)
    # Compatibility members retain original navigation/selection code paths.
    view.category_segments=QWidget(view);view.category_segments.hide()
    view.category_picker=button('全部',parent=view.selection_navigation);navigation.addWidget(view.category_picker);view.category_picker.clicked.connect(view.open_category_menu);view.category_picker.hide()
    view.bulk_more=ToolbarButton('more',view.selection_actions,motion=view.shelf.manager.motion);view.bulk_more.setFixedSize(32,32);view.bulk_more.setAccessibleName('批量操作');select.insertWidget(select.indexOf(view.select_all_button),view.bulk_more);view.bulk_more.hide();view.bulk_more.clicked.connect(view.open_bulk_menu)
    navigation.addStretch()
    view.narrow_view_button=button('预览',parent=view.selection_navigation);navigation.addWidget(view.narrow_view_button);view.narrow_view_button.hide();view.narrow_view_button.clicked.connect(view.toggle_narrow_preview)
    view._narrow_layout=False;view._narrow_preview=False


def build_preview(view,elided_type,image_type,media_type,content_type):
    from transfer_visuals import TransferOverlay
    view.setObjectName('referencePreview')
    css(view,*[f"QFrame#referencePreview{{background:{colors(t)['preview']};border:0;border-left:1px solid {colors(t)['divider']};}}" for t in ('dark','light')])
    root=QVBoxLayout(view);root.setContentsMargins(0,0,0,0);root.setSpacing(0)
    head=QFrame(view);view.preview_head=head;head.setFixedHeight(64)
    css(head,*[f"QFrame{{border:0;border-bottom:1px solid {colors(t)['divider']};background:transparent;}}" for t in ('dark','light')])
    header=QHBoxLayout(head);header.setContentsMargins(16,8,16,8);header.setSpacing(8)
    names=QVBoxLayout();names.setSpacing(0)
    view.name=elided_type(view);view.info=elided_type(view);view.upload_time=elided_type(view)
    for item,size,weight,tint in ((view.name,13,600,'text'),(view.upload_time,9,400,'secondary')):
        css(item,f"color:{colors('dark')[tint]};border:0;background:transparent;font:{weight} {size}px 'FlickDock UI';",
            f"color:{colors('light')[tint]};border:0;background:transparent;font:{weight} {size}px 'FlickDock UI';")
        names.addWidget(item)
    view.name.setFixedHeight(20);view.upload_time.setFixedHeight(16)
    header.addLayout(names,1)
    view.cloud_action=ToolbarButton('cloud-upload',head,motion=view.shelf.manager.motion)
    view.cloud_action.setProperty('disabledOpacity',.72)
    view.cloud_action.setFixedSize(32,32);view.cloud_action.clicked.connect(view.toggle_cloud_copy)
    header.addWidget(view.cloud_action)
    view.copy_button=ToolbarButton('copy',view,motion=view.shelf.manager.motion);view.copy_button.setFixedSize(32,32);view.copy_button.clicked.connect(view.copy_current);header.addWidget(view.copy_button);root.addWidget(head)
    view.content_scroll=QScrollArea(view);view.content_scroll.setWidgetResizable(True);view.content_scroll.setFrameShape(QFrame.NoFrame)
    view.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    css(view.content_scroll,'QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::handle:vertical{background:#49494E;border-radius:3px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}',
        'QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::handle:vertical{background:#C8C8C8;border-radius:3px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}')
    content=QWidget(view.content_scroll);body=QVBoxLayout(content);body.setContentsMargins(16,16,16,16);body.setSpacing(12)
    view.content_scroll.setWidget(content);content.setAutoFillBackground(False)
    view.transfer_row=QWidget(content);transfer=QHBoxLayout(view.transfer_row);transfer.setContentsMargins(0,0,0,0)
    view.transfer_progress=QProgressBar(view.transfer_row);view.transfer_progress.setTextVisible(False);view.transfer_progress.setFixedHeight(5)
    css(view.transfer_progress,*[f"QProgressBar{{background:{colors(t)['control']};border:0;}} QProgressBar::chunk{{background:@fd_action@;}}" for t in ('dark','light')])
    view.transfer_meta=label('',10,400,'link',view.transfer_row);view.cancel_transfer=button('取消上传',parent=view.transfer_row);view.cancel_transfer.clicked.connect(lambda:view.shelf.cancel_transfer(view.current_path))
    view.cancel_transfer.setFixedHeight(26)
    cancel_styles=[style+f' QPushButton{{font-size:{12 if sys.platform=="win32" else 11}px;padding:2px 5px;}}' for style in view.cancel_transfer._reference_style]
    css(view.cancel_transfer,*cancel_styles)
    transfer.addWidget(view.transfer_progress,1);transfer.addWidget(view.transfer_meta);transfer.addWidget(view.cancel_transfer);body.addWidget(view.transfer_row);view.transfer_row.hide()
    view.stack=QStackedWidget(content);view.stack.setMinimumHeight(176);view.stack.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
    css(view.stack,*[f"QStackedWidget{{background:{colors(t)['card']};border:1px solid {colors(t)['border']};border-radius:{RADIUS_CARD_PX}px;}}" for t in ('dark','light')])
    view.icon=content_type(view,motion=view.shelf.manager.motion)
    view.text=QPlainTextEdit(view);view.text.setReadOnly(True);view.text.setAcceptDrops(False);view.text.viewport().setAcceptDrops(False)
    css(view.text,f"QPlainTextEdit{{background:{colors('dark')['card']};color:#F5F5F7;border:0;border-radius:{RADIUS_CARD_PX}px;padding:14px;font:13px 'FlickDock UI';}}",
        "QPlainTextEdit{background:#FFFFFF;color:#1D1D1F;border:0;border-radius:12px;padding:14px;font:13px 'FlickDock UI';}")
    view.image=image_type(view);view.image.set_loader(view.shelf.image_cache);view.image.source_ready.connect(view._image_source_ready)
    view.media=media_type(view,motion=view.shelf.manager.motion);view.media.frame_ready.connect(view._poster_ready);view.media.playing_changed.connect(view._playing_changed)
    view.empty_preview=QWidget(view.stack)
    empty=QVBoxLayout(view.empty_preview);empty.setContentsMargins(0,0,0,0);empty.setSpacing(8);empty.addStretch()
    empty_icon=EmptyPreviewIcon(view.empty_preview);empty_icon.setFixedSize(28,28);view.empty_icon=empty_icon
    empty.addWidget(empty_icon,0,Qt.AlignHCenter)
    empty_text=label('选中文件后显示预览',11,400,'secondary',view.empty_preview);empty_text.setAlignment(Qt.AlignCenter)
    empty.addWidget(empty_text);empty.addStretch()
    for item in (view.icon,view.text,view.image,view.media,view.empty_preview):view.stack.addWidget(item)
    view.stack.setCurrentWidget(view.empty_preview)
    body.addWidget(view.stack,1)
    cards=QHBoxLayout();cards.setSpacing(8)
    status=QFrame(content);view.status_card=status;status.setObjectName('referenceStatus');status.setMinimumWidth(0)
    size_card=QFrame(content);view.size_card=size_card;size_card.setObjectName('referenceSize');size_card.setMinimumWidth(0)
    for card in (status,size_card):
        css(card,*[f"QFrame#{card.objectName()}{{background:{colors(t)['card']};border:1px solid {colors(t)['border']};border-radius:{RADIUS_CARD_PX}px;}}" for t in ('dark','light')])
    box=QVBoxLayout(status);box.setContentsMargins(PREVIEW_INFO_HORIZONTAL_PADDING_PX,PREVIEW_INFO_VERTICAL_PADDING_PX,PREVIEW_INFO_HORIZONTAL_PADDING_PX,PREVIEW_INFO_BOTTOM_PADDING_PX);box.setSpacing(PREVIEW_INFO_ROW_GAP_PX)
    from transfer_visuals import CloudStatusBadge
    status_title=label('同步状态',10,400,'muted',status);status_title.setFixedHeight(PREVIEW_INFO_TITLE_HEIGHT_PX);box.addWidget(status_title)
    line=QHBoxLayout();line.setContentsMargins(0,0,0,0);line.setSpacing(2)
    view.cloud_badge=CloudStatusBadge(status,file_presence=True);view.cloud_badge.hide()
    view.status_value=label('',10,400,'green',status);view.status_value.setWordWrap(True)
    view.status_value.setMinimumWidth(0);view.status_value.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
    line.addWidget(view.status_value,1);box.addLayout(line)
    size_box=QVBoxLayout(size_card);size_box.setContentsMargins(PREVIEW_INFO_HORIZONTAL_PADDING_PX,PREVIEW_INFO_VERTICAL_PADDING_PX,PREVIEW_INFO_HORIZONTAL_PADDING_PX,PREVIEW_INFO_BOTTOM_PADDING_PX);size_box.setSpacing(PREVIEW_INFO_ROW_GAP_PX)
    size_title=label('文件大小与类型',10,400,'muted',size_card);size_title.setFixedHeight(PREVIEW_INFO_TITLE_HEIGHT_PX);size_box.addWidget(size_title)
    view.info.setParent(size_card);view.info.setFixedHeight(PREVIEW_INFO_VALUE_HEIGHT_PX);view.info.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
    css(view.info,"color:#F5F5F7;border:0;background:transparent;font:400 10px 'FlickDock UI';",
        "color:#1D1D1F;border:0;background:transparent;font:400 10px 'FlickDock UI';")
    size_box.addWidget(view.info);cards.addWidget(status,1);cards.addWidget(size_card,1);body.addLayout(cards)
    path_card=QFrame(content);view.path_card=path_card;path_card.setObjectName('referencePath')
    css(path_card,*[f"QFrame#referencePath{{background:{colors(t)['card']};border:1px solid {colors(t)['border']};border-radius:{RADIUS_CARD_PX}px;}}" for t in ('dark','light')])
    path_box=QVBoxLayout(path_card);path_box.setContentsMargins(12,8,12,10);path_box.setSpacing(4)
    path_head=QHBoxLayout();path_head.addWidget(label('来源路径',10,400,'muted',path_card),1)
    view.copy_path_button=button('复制路径','copy',variant='secondary',parent=path_card);view.copy_path_button.setFixedHeight(26)
    view.copy_path_button.setIconSize(QSize(12,12));view.copy_path_button.clicked.connect(view.copy_path)
    path_styles=[style.replace(f'border-radius:{RADIUS_CONTROL_PX}px',f'border-radius:{RADIUS_SUBCONTROL_PX}px') for style in view.copy_path_button._reference_style]
    path_styles[0]=path_styles[0].replace('background:#272727','background:#333333',1)
    path_styles=[style+f' QPushButton{{font-size:{12 if sys.platform=="win32" else 11}px;padding:2px 8px;}}' for style in path_styles]
    css(view.copy_path_button,*path_styles)
    path_head.addWidget(view.copy_path_button);path_box.addLayout(path_head)
    view.path_label=elided_type(path_card);view.path_label.setFixedHeight(18);view.path_label.setAccessibleName('内容来源路径')
    css(view.path_label,"color:#A1A1AA;background:transparent;border:0;font:10px 'FlickDock UI';",
        "color:#636366;background:transparent;border:0;font:10px 'FlickDock UI';")
    path_box.addWidget(view.path_label);body.addWidget(path_card)
    root.addWidget(view.content_scroll,1)
    footer=QFrame(view);view.action_footer=footer;footer.setObjectName('referencePreviewActions')
    css(footer,*[f"QFrame#referencePreviewActions{{background:{colors(t)['preview']};border:0;border-top:1px solid {colors(t)['divider']};}}" for t in ('dark','light')])
    actions=QVBoxLayout(footer);actions.setContentsMargins(16,8,16,12);actions.setSpacing(8)
    primary=QHBoxLayout();primary.setSpacing(8);secondary=QHBoxLayout();secondary.setSpacing(8)
    view.open_button=button('打开文件',variant='secondary',parent=footer);view.rename_button=button('重命名',variant='secondary',parent=footer)
    view.open_button.clicked.connect(lambda:view.current_path and view.shelf.open_path(view.current_path));view.rename_button.clicked.connect(lambda:view.current_path and view.shelf.rename_ref(view.current_path))
    view.zip_button=button('打包 ZIP',variant='secondary',parent=footer);view.zip_button.clicked.connect(view.zip_current)
    view.send_control,view.send_button,view.send_menu_button=split_send_button(footer,36)
    view.send_button.clicked.connect(view.send_current)
    view.send_menu_button.clicked.connect(lambda:view.open_send_menu([view.current_path],view.send_menu_button))
    view.remove_button=button('长按移出',variant='danger',parent=footer,control_type=HoldRemoveButton)
    view.remove_button.identity=lambda:view.current_path
    view.remove_button.confirmed.connect(view.remove_current)
    view.action_buttons=(view.open_button,view.rename_button,view.copy_path_button,view.zip_button,view.send_button,view.remove_button)
    for item in view.action_buttons:item.setAccessibleName(item.text())
    view.open_button.setFixedHeight(36);primary.addWidget(view.open_button,1);primary.addWidget(view.send_control,1)
    for item in (view.rename_button,view.zip_button,view.remove_button):item.setFixedHeight(32);secondary.addWidget(item,1)
    actions.addLayout(primary);actions.addLayout(secondary);root.addWidget(footer)
    view.transfer_overlay=TransferOverlay(view.stack,lambda:view.shelf.content.transfers.get(view.current_path))
    view.group_view=QFrame(view);view.group_view.setObjectName('referenceGroupPreview')
    css(view.group_view,f"QFrame#referenceGroupPreview{{background:{colors('dark')['background']};border:0;}}",
        f"QFrame#referenceGroupPreview{{background:{colors('light')['background']};border:0;}}")
    group=QVBoxLayout(view.group_view);group.setContentsMargins(20,20,20,20);group.setSpacing(16);group.addStretch(1)
    view.group_art=QLabel(view.group_view);view.group_art.setFixedSize(96,80);view.group_art.setAlignment(Qt.AlignCenter);group.addWidget(view.group_art,0,Qt.AlignCenter)
    view.group_title=label('',14,600,parent=view.group_view);view.group_title.setAlignment(Qt.AlignCenter);group.addWidget(view.group_title)
    group_actions=QVBoxLayout();group_actions.setSpacing(8)
    view.group_copy=button('复制所选',variant='secondary',parent=view.group_view);view.group_copy.setFixedHeight(34);view.group_copy.clicked.connect(view.copy_current);group_actions.addWidget(view.group_copy)
    view.group_zip=button('打包为 ZIP…',variant='secondary',parent=view.group_view);view.group_zip.setFixedHeight(34);view.group_zip.clicked.connect(view.popup.compress_selected);group_actions.addWidget(view.group_zip)
    view.group_cloud_row=QWidget(view.group_view);cloud_actions=QHBoxLayout(view.group_cloud_row)
    cloud_actions.setContentsMargins(0,0,0,0);cloud_actions.setSpacing(8)
    view.group_cloud_upload=button('上传至云端',variant='secondary',parent=view.group_cloud_row)
    view.group_cloud_remove=button('从云端移除',variant='secondary',parent=view.group_cloud_row)
    for control in (view.group_cloud_upload,view.group_cloud_remove):control.setFixedHeight(34);cloud_actions.addWidget(control,1)
    view.group_cloud_upload.clicked.connect(view.upload_selected_to_cloud)
    view.group_cloud_remove.clicked.connect(view.remove_selected_from_cloud)
    group_actions.addWidget(view.group_cloud_row)
    view.group_send_control,view.group_send,view.group_send_menu=split_send_button(view.group_view,34)
    view.group_send.clicked.connect(lambda:view._send_other(view.popup.chosen_refs()))
    view.group_send_menu.clicked.connect(lambda:view.open_send_menu(view.popup.chosen_refs(),view.group_send_menu));group_actions.addWidget(view.group_send_control)
    view.group_remove=button('长按移出',variant='danger',parent=view.group_view,control_type=HoldRemoveButton);view.group_remove.setFixedHeight(34)
    view.group_remove.identity=lambda:tuple(view.popup.chosen_refs())
    view.group_remove.confirmed.connect(view.popup.remove_selected);group_actions.addWidget(view.group_remove)
    group.addLayout(group_actions);group.addStretch(1)
    view.group_view.hide()

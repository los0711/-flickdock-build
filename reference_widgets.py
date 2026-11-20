import weakref
"""Qt artwork and controls transcribed from the supplied FlickDock HTML.

Only presentation lives here. Content, drag MIME, disk work and network services
continue to be owned by the existing shelf and panel classes.
"""
from pathlib import Path
from collections import OrderedDict
import sys
import re
import time
import math

from PySide6.QtCore import QObject, QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QIcon, QPalette, QImage, QPainter, QPainterPath, QPen, QPixmap, QTextDocument, QTextOption, QTransform
from PySide6.QtWidgets import (QLabel, QPushButton, QWidget, QFrame, QHBoxLayout,
    QVBoxLayout, QScrollArea, QLineEdit, QProgressBar, QButtonGroup, QSizePolicy, QGridLayout,
    QStackedWidget, QPlainTextEdit, QTextEdit, QCheckBox, QApplication, QDialog, QRubberBand)

import ui_theme
from native_ui_common import SEGMENT_INSET_PX
from native_ui_common import (colors, icon, keyboard_focus_visible, paint_rounded_surface, qss_color, readable_qss, set_ui_weight, reference_font, RoundedSurfaceFrame, RoundedScrollBar, RoundedProgressBar,
    RADIUS_SMALL_PX,RADIUS_SUBCONTROL_PX,RADIUS_CONTROL_PX,RADIUS_CARD_PX,RADIUS_PANEL_PX,
    SHELF_HEADER_BUTTON_SIZE_PX, SHELF_HEADER_BUTTON_GAP_PX, SHELF_HEADER_RIGHT_MARGIN_PX,
    SHELF_OVERVIEW_HEADER_HEIGHT_PX)
from content_stack import StackTile,STACK_ARRIVAL_OFFSET_PX
from flickdock_motion import FeedbackButton,Spring
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
ICON_TEXT_GAP_PX = 6
ICON_TEXT_SIDE_RESERVE_PX = 16
ICON_TEXT_LEFT_RESERVE_PX = 56
ICON_TEXT_MEASURE_SAFETY_PX = 2
PROFILE_TEXT_RISE_PX = 1
EMPTY_DROP_RADIUS_PX = RADIUS_PANEL_PX
COMPACT_EMPTY_LABEL = '拖入文件'
PREVIEW_INFO_HORIZONTAL_PADDING_PX = 12
PREVIEW_INFO_VERTICAL_PADDING_PX = 8
PREVIEW_INFO_BOTTOM_PADDING_PX = 5
PREVIEW_INFO_TITLE_HEIGHT_PX = 16
PREVIEW_INFO_VALUE_HEIGHT_PX = 28
PREVIEW_INFO_ROW_GAP_PX = 4
PREVIEW_STATUS_ACTION_SIZE_PX = 24
PREVIEW_STATUS_ACTION_GLYPH_SIZE_PX = 14
PREVIEW_STATUS_ACTION_TOP_INSET_PX = 4
PREVIEW_STATUS_ACTION_RIGHT_INSET_PX = 8
PREVIEW_HEADER_RESERVED_ACTION_WIDTH_PX = 32
PREVIEW_ACTION_HEIGHT_PX = 32
PREVIEW_ACTION_ICON_SIZE_PX = 14
PREVIEW_ACTION_OPEN_WIDTH_PX = 68
PREVIEW_ACTION_MENU_WIDTH_PX = 24
PREVIEW_ACTION_SPACING_PX = 6
PREVIEW_ACTION_SIDE_MARGIN_PX = 16
PREVIEW_ACTION_ICON_TEXT_GAP_PX = 4
RENAME_EDIT_MIN_HEIGHT_PX = 34
RENAME_EDIT_MAX_LINES = 4
RENAME_EDIT_PADDING_X_PX = 10
RENAME_EDIT_PADDING_Y_PX = 7
RENAME_EDIT_BORDER_PX = 1
RENAME_DIALOG_BASE_HEIGHT_PX = 252


class OverviewEmptyDropZone(_DropZone):
    """Match the preview's empty drop target, without scaling its dash path."""
    def __init__(self,parent=None):
        super().__init__(parent);self._transfer_choice_active=False

    def border_path(self):
        rect=QRectF(self.rect()).adjusted(1,1,-1,-1)
        radius=min(EMPTY_DROP_RADIUS_PX,rect.width()/2,rect.height()/2)
        path=QPainterPath();path.addRoundedRect(rect,radius,radius)
        return path

    def border_radius(self):return EMPTY_DROP_RADIUS_PX

    def set_transfer_choice_active(self,active):
        self._transfer_choice_active=bool(active)
        if active:self._hovered=False;self.set_drag_active(False)

    def set_drag_active(self,active,point=None,*,accepted=False):
        super().set_drag_active(bool(active) and not self._transfer_choice_active,point,accepted=accepted)

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


class RoundedSearchEdit(QLineEdit):
    """Keep Qt's text input while painting its frame at device-pixel precision."""
    def paintEvent(self,event):
        c=palette();p=QPainter(self)
        fill=c['input'] if self.isEnabled() else c['control']
        edge=c['ring'] if self.hasFocus() and self.isEnabled() else c['input_border']
        paint_rounded_surface(p,self.rect(),RADIUS_CONTROL_PX,ink(fill),ink(edge))
        p.end();super().paintEvent(event)


def palette():
    return colors(ui_theme.current_theme())


def ink(value):
    return qss_color(value)


def css(widget, dark, light=None):
    # Resolve the source's real 400/500/600/700 weights in the selected OS face.
    if QApplication.instance():
        from native_ui_common import font_css_stack
        family=font_css_stack(technical=bool(widget.property('flickdockTechnicalFont')))
        dark=dark.replace("'FlickDock UI'",family)
        if light is not None:light=light.replace("'FlickDock UI'",family)
    widget._reference_style=(readable_qss(dark), readable_qss(light if light is not None else dark))
    from theme_accent import resolve_qss
    resolved=resolve_qss(widget._reference_style[ui_theme.current_theme()=='light'],ui_theme.current_theme())
    if widget.styleSheet()!=resolved:widget.setStyleSheet(resolved)


def refresh_tree(widget,theme=None):
    theme=theme if theme in ('light','dark') else ui_theme.current_theme()
    themed_colors=colors(theme)
    from theme_accent import resolve_qss
    was_enabled=widget.updatesEnabled()
    widget.setUpdatesEnabled(False)
    try:
        for child in [widget, *widget.findChildren(QWidget)]:
            source=getattr(child,'_reference_style',None)
            if source:
                resolved=resolve_qss(source[theme=='light'],theme)
                if child.styleSheet()!=resolved:child.setStyleSheet(resolved)
            if getattr(child,'_reference_icon',None):
                name,tint=child._reference_icon
                child.setIcon(icon(name,themed_colors.get(tint,tint)))
    finally:
        widget.setUpdatesEnabled(was_enabled)
    widget.update()


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


class RoundedBadge(QLabel):
    def __init__(self,text='',parent=None):
        super().__init__(text,parent);self._badge_fill='transparent';self._badge_colors=None

    def set_background(self,value):self._badge_fill=value;self.update()
    def set_theme_backgrounds(self,dark,light):self._badge_colors=(dark,light);self.update()

    def paintEvent(self,event):
        fill=(self._badge_colors[ui_theme.current_theme()=='light'] if self._badge_colors is not None else self._badge_fill)
        if fill!='transparent':
            p=QPainter(self);paint_rounded_surface(p,self.rect(),min(self.height()/2,RADIUS_CARD_PX),ink(fill));p.end()
        super().paintEvent(event)


class RoundedPreviewText(QPlainTextEdit):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setVerticalScrollBar(RoundedScrollBar(Qt.Vertical,self,thumb_color=lambda:palette()['input_border']))

    def event(self,event):
        if event.type()==QEvent.Paint:
            p=QPainter(self);paint_rounded_surface(p,self.rect(),RADIUS_CARD_PX,ink(palette()['card']));p.end()
        return super().event(event)


class MenuChoiceButton(QPushButton):
    def paintEvent(self,event):
        from PySide6.QtWidgets import QStyle,QStylePainter
        from native_ui_common import button_style_option
        p=QStylePainter(self)
        if self.isEnabled() and (self.underMouse() or self.property('menuSelected')):
            paint_rounded_surface(p,self.rect(),RADIUS_SUBCONTROL_PX,ink(palette()['hover']))
        option=button_style_option(self);option.rect=self.style().subElementRect(QStyle.SE_PushButtonContents,option,self)
        p.drawControl(QStyle.CE_PushButtonLabel,option);p.end()


def label(value='',size=12,weight=400,tint='text',parent=None,*,control_type=QLabel):
    widget=control_type(value,parent)
    dark=colors('dark');light=colors('light')
    css(widget,f"color:{dark.get(tint,tint)};background:transparent;border:0;font:{weight} {size}px 'FlickDock UI';",
        f"color:{light.get(tint,tint)};background:transparent;border:0;font:{weight} {size}px 'FlickDock UI';")
    widget.setTextFormat(Qt.PlainText)
    return widget


class IconTextButton(QPushButton):
    """Use one logical icon/text group on both Qt platform styles."""
    def _icon_text_spacing(self):
        gap=self.property('iconTextGap')
        gap=gap if isinstance(gap,int) else ICON_TEXT_GAP_PX
        reserved=self.property('iconTextReservedWidth')
        reserved=reserved if isinstance(reserved,int) else (ICON_TEXT_LEFT_RESERVE_PX if self.property('leftAligned') else ICON_TEXT_SIDE_RESERVE_PX)
        return gap,reserved

    def sizeHint(self):
        hint=super().sizeHint()
        if self.text() and not self.icon().isNull():
            gap,reserved=self._icon_text_spacing()
            width=self.fontMetrics().horizontalAdvance(self.text())+self.iconSize().width()+gap+reserved+ICON_TEXT_MEASURE_SAFETY_PX
            hint.setWidth(max(hint.width(),width))
        return hint

    def minimumSizeHint(self):
        hint=super().minimumSizeHint()
        hint.setWidth(max(hint.width(),self.sizeHint().width()))
        return hint

    def _surface_radius(self):
        parent=self.parentWidget()
        if self.property('pillShape') or isinstance(parent,SelectionSurface):
            return self.height()/2.0
        if self.property('previewCopyPath'):
            return RADIUS_SUBCONTROL_PX
        return RADIUS_CONTROL_PX

    def _paint_surface(self,painter):
        theme=self.property('referenceTheme');theme=theme if theme in ('dark','light') else ui_theme.current_theme()
        c=colors(theme);variant=self.property('referenceVariant') or 'quiet'
        enabled=self.isEnabled();down=enabled and self.isDown();hover=enabled and self.underMouse()
        parent=self.parentWidget();name=parent.objectName() if parent else ''
        edge=None;radius=self._surface_radius()
        if name=='splitSendControl':
            fill=c['control'] if not enabled else c['action_pressed'] if down else c['action_hover'] if hover else c['action']
            path=QPainterPath();path.addRoundedRect(QRectF(-self.x(),-self.y(),parent.width(),parent.height()),radius,radius)
            painter.save();painter.setRenderHint(QPainter.Antialiasing);painter.setClipPath(path)
            painter.fillRect(self.rect(),ink(fill));painter.restore()
            if enabled and self.property('keyboardFocus'):
                paint_rounded_surface(painter,QRectF(self.rect()).adjusted(1,1,-1,-1),radius,QColor(Qt.transparent),ink(c['ring']))
            return
        if name=='previewIconActions':
            if not enabled:fill='transparent'
            elif variant=='danger':fill='rgba(255,69,58,58)' if down else 'rgba(255,69,58,30)' if hover else 'transparent'
            else:fill=('#38383d' if theme=='dark' else '#cdcdcf') if down else ('#29292d' if theme=='dark' else '#dddddf') if hover else 'transparent'
        elif self.property('previewOpen'):
            fill=(('#38383d' if theme=='dark' else '#d4d4d7') if down else
                  ('#2c2c31' if theme=='dark' else '#e1e1e3') if hover else
                  ('#1d1d21' if theme=='dark' else '#eeeeef')) if enabled else c['control']
        elif isinstance(parent,SelectionSurface):
            if parent._selection_vertical and parent._selection_color_role=='rail_active':
                fill=(c['rail_item_hover'] if (hover or down) else 'transparent') if enabled and not self.isChecked() else 'transparent'
            else:
                fill=(c['control_pressed'] if down else c['control_hover'] if hover else 'transparent') if enabled and not self.isChecked() else 'transparent'
        elif not enabled:
            fill='transparent' if self.property('iconOnly') else c['card']
            if not self.property('iconOnly'):edge=c['input_border']
        elif variant=='primary':fill=c['action_pressed'] if down else c['action_hover'] if hover else c['action']
        elif self.isChecked():fill=c['selected']
        elif variant=='danger':fill='rgba(255,69,58,78)' if down else 'rgba(255,69,58,58)' if hover else 'rgba(255,69,58,30)'
        elif variant in ('secondary','secondary_blue'):
            fill=c['control_pressed'] if down else c['control_hover'] if hover else c['card']
            if self.property('previewCopyPath') and not down and not hover and theme=='dark':fill='#333333'
            if variant=='secondary':edge=c['input_border']
        else:fill=c['selected'] if down else c['hover'] if hover else 'transparent'
        if enabled and self.property('keyboardFocus'):edge=c['ring']
        paint_rounded_surface(painter,self.rect(),radius,ink(fill),None if edge is None else ink(edge))

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
        from PySide6.QtWidgets import QStyleOptionButton, QStyle, QStylePainter
        from native_ui_common import button_style_option
        option=button_style_option(self)
        if self.icon().isNull():
            painter=QStylePainter(self);self._paint_surface(painter)
            option.rect=self.style().subElementRect(QStyle.SE_PushButtonContents,option,self)
            painter.drawControl(QStyle.CE_PushButtonLabel,option);painter.end();return
        art,words=QIcon(option.icon),str(option.text);option.icon=QIcon();option.text=''
        theme=self.property('referenceTheme')
        c=colors(theme) if theme in ('light','dark') else palette()
        variant=self.property('referenceVariant');left=bool(self.property('leftAligned'))
        if variant=='primary' and self.property('previewAccentInk'):
            from theme_accent import action_tokens
            c.update(action_tokens(theme if theme in ('light','dark') else ui_theme.current_theme()))
        surface=self.parentWidget()
        navigation=isinstance(surface,SelectionSurface) and surface._selection_vertical and left
        is_rail=navigation and surface._selection_color_role=='rail_active'
        is_hseg=isinstance(surface,SelectionSurface) and not surface._selection_vertical
        navigation_ink=c['rail_item_ink'] if is_rail else (c['text'] if self.property('sidebarUtility') else c['secondary'])
        active=(surface._selection_rect.contains(QRectF(self.geometry()).center()) and surface._selection_opacity>.55) if navigation else (self.isChecked() and (left or is_hseg))
        named_icon=getattr(self,'_reference_icon',None)
        if named_icon:
            icon_tint=(c['rail_active_ink'] if (active and is_rail)
                       else c['segment_selected_ink'] if (active and is_hseg)
                       else c['on_action'] if active or variant=='primary'
                       else navigation_ink if navigation else c['red'] if variant=='danger' else c.get(named_icon[1],named_icon[1]))
            art=icon(named_icon[0],icon_tint if self.isEnabled() else c['muted'])
        painter=QStylePainter(self);self._paint_surface(painter)
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
        size=self.iconSize()
        gap,reserved=self._icon_text_spacing()
        space=self.width()-reserved-size.width()-gap
        text=painter.fontMetrics().elidedText(words,Qt.ElideRight,max(1,space))
        width=painter.fontMetrics().horizontalAdvance(text)+size.width()+gap
        x=12 if left else (self.width()-width)/2
        rail_label=getattr(self,'_sidebar_label_alpha',1.)
        rail_compact=getattr(self,'_sidebar_compact_progress',0.)
        if rail_compact:x+=(max(0.,(self.width()-size.width())/2)-x)*rail_compact
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
        tint=(c['rail_active_ink'] if (active and is_rail)
              else c['segment_selected_ink'] if (active and is_hseg)
              else c['on_action'] if active or variant=='primary' else navigation_ink if navigation else
              c['text'] if self.isChecked() or variant=='secondary' else c['red'] if variant=='danger' else c['secondary'])
        painter.setPen(QColor(tint if self.isEnabled() else c['muted']))
        text_x=x if trailing else x+size.width()+gap
        if rail_label>0:
            painter.save()
            painter.setOpacity(rail_label)
            painter.setClipRect(QRectF(text_x,0,max(0,self.width()-text_x),self.height()),Qt.IntersectClip)
            painter.drawText(QRectF(text_x-10*(1-rail_label),0,painter.fontMetrics().horizontalAdvance(text)+2,self.height()),Qt.AlignLeft|Qt.AlignVCenter,text)
            painter.restore()


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
        self.setDown(True)
        if not self.property('iconOnlyHold'):self.setText('松手取消')
        self._hold_timer.start(self.HOLD_MS)
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
        p=QStylePainter(self);self._paint_surface(p)
        rect=QRectF(self.rect()).adjusted(1,1,-1,-1)
        fraction=max(0.,min(1.,(time.monotonic()-self._hold_started)*1000/self.HOLD_MS))
        bar=QRectF(rect.left(),rect.top(),rect.width()*fraction,rect.height())
        shape=QPainterPath();shape.addRoundedRect(rect,RADIUS_CONTROL_PX,RADIUS_CONTROL_PX)
        p.save();p.setRenderHint(QPainter.Antialiasing);p.setClipPath(shape)
        p.fillRect(bar,QColor(palette()['red']));p.restore()
        if self.property('iconOnlyHold'):
            size=self.iconSize()
            target=QRect((self.width()-size.width())//2,(self.height()-size.height())//2,size.width(),size.height())
            icon('trash',palette()['red']).paint(p,target)
            p.save();p.setClipRect(bar);icon('trash','#ffffff').paint(p,target);p.restore()
        else:
            p.setFont(self.font());p.setPen(QColor(palette()['red']))
            p.drawText(QRectF(self.rect()),Qt.AlignCenter,self.text())
            p.save();p.setClipRect(bar);p.setPen(QColor('white'))
            p.drawText(QRectF(self.rect()),Qt.AlignCenter,self.text());p.restore()
        p.end()


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
        p=QStylePainter(self);self._paint_surface(p);p.setFont(self.font())
        text_width=p.fontMetrics().horizontalAdvance(self.text())
        x=(self.width()-text_width-SORT_ARROW_GAP_PX-SORT_ARROW_SIZE_PX)/2
        c=palette();tint=c['segment_selected_ink'] if self.isChecked() else c['secondary']
        p.setPen(QColor(tint if self.isEnabled() else c['muted']))
        p.drawText(QRectF(x,0,text_width,self.height()),Qt.AlignLeft|Qt.AlignVCenter,self.text())
        from native_ui_common import cached_svg_renderer
        values=getattr(self,'_sort_value',QPointF(float(self.direction==1),float(self.direction==2)))
        arrow_active=c['segment_selected_ink'] if self.isChecked() else c['text']
        for index,path in enumerate(('M4 9 12 1 20 9Z','M4 15 12 23 20 15Z')):
            rect=QRectF(x+text_width+SORT_ARROW_GAP_PX,(self.height()-SORT_ARROW_SIZE_PX)/2,SORT_ARROW_SIZE_PX,SORT_ARROW_SIZE_PX)
            for tint,opacity in ((c['sort_arrow_idle'],1),(arrow_active,values.x() if index==0 else values.y())):
                if opacity<=0:continue
                svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="{tint}" d="{path}"/></svg>'
                p.save();p.setOpacity(opacity);cached_svg_renderer(svg).render(p,rect);p.restore()


class _SidebarHitBridge(QObject):
    """Clicks that land between nav rows choose the nearest row."""
    def __init__(self, view):
        super().__init__(view)
        self._view = weakref.ref(view)

    def eventFilter(self, watched, event):
        if event.type() != QEvent.MouseButtonPress or event.button() != Qt.MouseButton.LeftButton:
            return False
        view = self._view()
        if view is None:
            return False
        point = event.position().toPoint()
        rows = [control for control in view.sidebar.findChildren(QPushButton) if control.isVisible()]
        if not rows:
            return False
        best = min(rows, key=lambda control: abs(control.geometry().center().y() - point.y()))
        if best.geometry().contains(point):
            return False
        if abs(best.geometry().center().y() - point.y()) <= 3:
            best.click()
            return True
        return False


class SegmentedControl(SelectionSurface):
    """Shared shell and button states for view and ordering controls."""
    def __init__(self,parent=None):
        super().__init__(parent,radius=SEGMENT_BUTTON_HEIGHT_PX/2)
        self.setFixedHeight(SEGMENT_HEIGHT_PX)
        self.setSizePolicy(QSizePolicy.Fixed,QSizePolicy.Fixed)
        self.setObjectName('segmentedControl')
        css(self,'QFrame#segmentedControl{background:transparent;border:0;}')
        self.set_surface_background('subcard','subcard',SEGMENT_HEIGHT_PX/2,stroke_role='border')
        layout=QHBoxLayout(self);layout.setContentsMargins(*(SEGMENT_INSET_PX,)*4);layout.setSpacing(0)

    def add_button(self,control,width=None):
        control.setCheckable(True);control.setFixedHeight(SEGMENT_BUTTON_HEIGHT_PX)
        padding=SORT_TEXT_PADDING_PX if isinstance(control,SortButton) else SEGMENT_TEXT_PADDING_PX
        sources=[]
        for theme in ('dark','light'):
            c=colors(theme)
            sources.append(f"QPushButton{{background:transparent;border:0;color:{c['secondary']};padding:0 {padding+1}px;font:12px 'FlickDock UI';}} QPushButton:checked,QPushButton:checked:hover,QPushButton:checked:pressed{{color:{c['segment_selected_ink']};}} QPushButton:disabled{{color:{c['muted']};}}")
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
        ink=c['on_action'] if variant=='primary' else c['red'] if variant=='danger' else c['link'] if variant in ('link','secondary_blue') else c['secondary'] if variant=='quiet' else c['text']
        checked=ink if variant=='primary' else c['text']
        sources.append(f"QPushButton{{background:transparent;color:{ink};border:0;padding:5px 12px;font:500 12px 'FlickDock UI';}} QPushButton:checked{{color:{checked};}} QPushButton:disabled{{color:{c['muted']};}}")
    css(widget,*sources)
    if glyph:
        widget._reference_icon=(glyph,'secondary')
        widget.setIcon(icon(glyph,palette()['secondary']))
    return widget


def overview_back_button(parent,callback):
    control=button('返回总览','left',variant='secondary',parent=parent)
    control.setFixedHeight(28)
    control.clicked.connect(callback)
    return control


class _SplitSendSurface(RoundedSurfaceFrame):
    def __init__(self,parent=None):super().__init__(parent,content_inset=0)
    def surface_style(self):
        c=palette();return c['action'] if self.isEnabled() else c['control'],None,RADIUS_CONTROL_PX


class _PreviewActionSurface(RoundedSurfaceFrame):
    def __init__(self,parent=None):super().__init__(parent,content_inset=0)
    def surface_style(self):
        return ('#141417' if ui_theme.current_theme()=='dark' else '#ededee'),None,RADIUS_CONTROL_PX


def split_send_button(parent,height,*,preview=False):
    """One primary action with a separate keyboard-accessible device menu."""
    frame=_SplitSendSurface(parent);frame.setObjectName('splitSendControl');frame.setFixedHeight(height)
    frame.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
    css(frame,'QFrame#splitSendControl{background:transparent;border:0;}')
    layout=QHBoxLayout(frame);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
    primary=button('发送文件','send' if preview else '',variant='primary',parent=frame);primary.setFixedHeight(height)
    arrow=button('','chevron-down',variant='primary',parent=frame)
    arrow.setFixedSize(PREVIEW_ACTION_MENU_WIDTH_PX if preview else 36,height)
    arrow.setIconSize(QSize(12 if preview else 14,12 if preview else 14));arrow.setProperty('splitDivider',True)
    if preview:
        primary.setMinimumWidth(0);primary.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        primary.setIconSize(QSize(PREVIEW_ACTION_ICON_SIZE_PX,PREVIEW_ACTION_ICON_SIZE_PX))
        primary.setProperty('iconTextGap',PREVIEW_ACTION_ICON_TEXT_GAP_PX)
        primary.setProperty('iconTextReservedWidth',4)
        primary.setProperty('previewAccentInk',True);arrow.setProperty('previewAccentInk',True)
    arrow.setToolTip('选择同账号设备');arrow.setAccessibleName('选择同账号设备')
    for widget,right in ((primary,False),(arrow,True)):
        styles=[]
        for theme in ('dark','light'):
            c=colors(theme)
            c.update({key:'@fd_'+key+'@' for key in ('action','on_action','action_hover','action_pressed')})
            styles.append(f"QPushButton{{background:transparent;color:{c['on_action']};border:0;padding:5px 9px;font:400 12px 'FlickDock UI';}}"
                          f"QPushButton:disabled{{color:{c['muted']};}}")
        css(widget,*styles)
    layout.addWidget(primary,1);layout.addWidget(arrow)
    return frame,primary,arrow


def _shelf_pin_glyph(colors):
    if 'shelf_pin_fill' in colors:return 'star-shelf-light',colors['shelf_pin_fill']
    return 'star-filled',colors['shelf_pin']


class ToolbarButton(FeedbackButton):
    """Retain the existing interruptible press spring with the HTML glyphs."""
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        c=palette();v=self.spring.values
        center=QRectF(self.rect()).center()
        p.translate(center);p.scale(v['scale']*self._presentation_scale[0],v['scale']*self._presentation_scale[1]);p.translate(-center)
        if self.property('controlFrame'):
            fill=c['control']
            if self.isEnabled():
                if self.isDown() or v['down']>.1:fill=c['control_pressed']
                elif self.isChecked():fill=c['selected']
                elif self.underMouse() or v['hover']>.01:fill=c['control_hover']
            paint_rounded_surface(p,self.rect(),RADIUS_CONTROL_PX,ink(fill),ink(c['border']))
        elif self.isEnabled() and (self.isChecked() or v['hover']>.01 or v['down']>.01 or self.isDown()):
            fill=QColor(255,255,255,38) if self.property('inverseOnSelection') else ink(c['control_pressed'] if self.isDown() or v['down']>.1 else c['selected'] if self.isChecked() else c['control_hover'])
            if not self.isChecked():fill.setAlphaF(fill.alphaF()*min(1,max(v['hover']+v['down'],1. if self.isDown() else 0.)))
            paint_rounded_surface(p,QRectF(self.rect()).adjusted(1,1,-1,-1),RADIUS_CONTROL_PX,fill)
        if not self.isEnabled():p.setOpacity(float(self.property('disabledOpacity') or .4))
        name={'pin-filled':'pin','check':'check'}.get(self.glyph,self.glyph)
        tint=self._glyph_tint or (c['action'] if self.isChecked() and name=='pin' else c['blue'] if self.isChecked() else c['secondary'])
        if self.property('shelfPin') and name=='star-filled':name,tint=_shelf_pin_glyph(c)
        p.save();p.setOpacity(p.opacity()*getattr(self,'_entry_alpha',1.));p.translate(getattr(self,'_entry_dx',0.),0.)
        if self.property('subtle'):p.setOpacity(p.opacity()*(.45+.4*v['hover']))
        hover=v['hover']
        if name=='cloud':p.translate(0,-hover)
        elif name in ('expand','collapse'):
            p.translate(center);p.rotate(6*hover if name=='expand' else -6*hover);p.translate(-center)
        elif name=='dock':p.translate(hover,0)
        glyph_size=self.property('glyphSize')
        glyph_size=glyph_size if isinstance(glyph_size,int) and glyph_size>0 else 16
        icon(name,tint).paint(p,QRect((self.width()-glyph_size)//2,(self.height()-glyph_size)//2,glyph_size,glyph_size))
        p.restore()
        if self.hasFocus() and self._keyboard_focus:
            paint_rounded_surface(p,QRectF(self.rect()).adjusted(2,2,-2,-2),RADIUS_SUBCONTROL_PX,
                                  QColor(Qt.transparent),QColor('#FAFAFA' if self.property('inverseOnSelection') else c['link']))
        p.end()


class StatusCardAction(ToolbarButton):
    """Reveal the action on card hover, while retaining keyboard access."""
    def __init__(self,glyph,parent=None,*,motion=True):
        super().__init__(glyph,parent,motion=motion)
        self._card_hovered=False
        self.setAttribute(Qt.WA_TransparentForMouseEvents,True)

    def set_card_hovered(self,hovered):
        hovered=bool(hovered)
        if hovered==self._card_hovered:return
        self._card_hovered=hovered
        self.setAttribute(Qt.WA_TransparentForMouseEvents,not hovered)
        self.update()

    def paintEvent(self,event):
        if self._card_hovered or (self.hasFocus() and self._keyboard_focus):super().paintEvent(event)


class PreviewSurfaceFrame(RoundedSurfaceFrame):
    """Preview surface with one device-pixel AA outline at every DPR."""
    def surface_style(self):
        c=palette();return c['subcard'],c['border'],RADIUS_CARD_PX


class BentoPanelFrame(RoundedSurfaceFrame):
    """Rounded content surface within the overview."""
    def surface_style(self):
        c=palette();return c['card'],c['border'],RADIUS_PANEL_PX


class InsetDivider(QWidget):
    """1px horizontal hairline inset from card edges so it never touches rounded corners."""
    def __init__(self,parent=None,inset=16):
        super().__init__(parent);self._inset=int(inset);self.setFixedHeight(1)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
    def paintEvent(self,event):
        p=QPainter(self);p.fillRect(QRectF(self._inset,0,max(0,self.width()-2*self._inset),1),ink(palette()['divider']));p.end()


class PreviewSurfaceStack(QStackedWidget):
    """Keep the empty state transparent and the selected surface outlined."""
    def __init__(self,parent=None):
        super().__init__(parent);self._surface_enabled=True
        self.setFrameShape(QFrame.NoFrame);self.setContentsMargins(1,1,1,1)

    def set_surface_enabled(self,enabled):
        enabled=bool(enabled)
        if self._surface_enabled==enabled:return
        self._surface_enabled=enabled
        inset=1 if enabled else 0;self.setContentsMargins(inset,inset,inset,inset)
        self.update()

    def paintEvent(self,event):
        if not self._surface_enabled:return
        p=QPainter(self);c=palette()
        paint_rounded_surface(p,self.rect(),RADIUS_CARD_PX,ink(c['subcard']),ink(c['border']))
        p.end()


class PreviewStatusCard(PreviewSurfaceFrame):
    """Keep the hover action anchored without shifting the status layout."""
    def __init__(self,parent=None):
        super().__init__(parent);self._hover_action=None

    def set_hover_action(self,action):
        self._hover_action=action
        action.set_card_hovered(self.underMouse())
        self._position_action();action.raise_()

    def _position_action(self):
        if self._hover_action is not None:
            self._hover_action.move(self.width()-self._hover_action.width()-PREVIEW_STATUS_ACTION_RIGHT_INSET_PX,
                                    PREVIEW_STATUS_ACTION_TOP_INSET_PX)

    def enterEvent(self,event):
        if self._hover_action is not None:self._hover_action.set_card_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self,event):
        if self._hover_action is not None:self._hover_action.set_card_hovered(False)
        super().leaveEvent(event)

    def resizeEvent(self,event):
        super().resizeEvent(event);self._position_action()

    def hideEvent(self,event):
        if self._hover_action is not None:self._hover_action.set_card_hovered(False)
        super().hideEvent(event)


class LanButton(ToolbarButton):
    """One footer target for the LAN symbol and caption."""
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);c=palette()
        if self.isEnabled() and (self.underMouse() or self.isDown()):
            paint_rounded_surface(p,QRectF(self.rect()).adjusted(1,1,-1,-1),RADIUS_CONTROL_PX,ink(c['selected'] if self.isDown() else c['hover']))
        icon('nearby',c['link']).paint(p,QRect(7,(self.height()-16)//2,16,16))
        f=self.font();f.setPixelSize(13 if sys.platform=='win32' else 12);p.setFont(f);p.setPen(QColor(c['text']))
        p.drawText(QRectF(29,0,self.width()-33,self.height()),Qt.AlignLeft|Qt.AlignVCenter,'文件互传')
        if keyboard_focus_visible(self):
            paint_rounded_surface(p,QRectF(self.rect()).adjusted(2,2,-2,-2),RADIUS_SUBCONTROL_PX,QColor(Qt.transparent),QColor(c['link']))
        p.end()


class AvatarButton(ToolbarButton):
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        c=palette();initial=getattr(self,'initial','')
        if self.isEnabled() and (self.underMouse() or self.isDown()):
            paint_rounded_surface(p,QRectF(self.rect()).adjusted(1,1,-1,-1),RADIUS_CONTROL_PX,ink(c['selected'] if self.isDown() else c['hover']))
        p.setPen(Qt.NoPen);p.setBrush(QColor(c['action']) if initial else ink(c['control']));p.drawEllipse(QRectF(4,4,self.width()-8,self.height()-8))
        if initial:
            p.setPen(QColor(c['on_action']));f=p.font();f.setPixelSize(11);set_ui_weight(f,600);p.setFont(f);p.drawText(self.rect(),Qt.AlignCenter,initial)
        else:icon('user',c['secondary']).paint(p,QRect((self.width()-16)//2,(self.height()-16)//2,16,16))
        if self.hasFocus() and self._keyboard_focus:
            paint_rounded_surface(p,QRectF(self.rect()).adjusted(2,2,-2,-2),RADIUS_SUBCONTROL_PX,QColor(Qt.transparent),QColor(c['link']))
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
        fill=ink(c['rail_item_hover'] if (self.isDown() or self.underMouse()) else c['rail_item'])
        paint_rounded_surface(p,QRectF(self.rect()).adjusted(0,1,0,-1),18,fill,QColor(255,255,255,18))
        compact=getattr(self,'_sidebar_compact_progress',0.)
        alpha=getattr(self,'_sidebar_label_alpha',1.)
        avatar_x=9+((self.width()-28)/2-9)*compact
        avatar=QRectF(avatar_x,(self.height()-28)/2,28,28)
        p.setBrush(QColor(c['brand_accent']) if self.connected else ink(c['rail_item_hover']));p.setPen(Qt.NoPen);p.drawEllipse(avatar)
        f=p.font();f.setPixelSize(14 if sys.platform=='win32' else 12);set_ui_weight(f,600);p.setFont(f);p.setPen(QColor(c['brand_accent_ink']))
        if self.connected:p.drawText(avatar,Qt.AlignCenter,self.username[:1].upper())
        else:icon('user',c['rail_item_ink']).paint(p,QRect(round(avatar_x+6),round(avatar.center().y()-8),16,16))
        p.save();p.setOpacity(alpha)
        f.setPixelSize(13 if sys.platform=='win32' else 11)
        set_ui_weight(f,600);p.setFont(f);p.setPen(QColor('#FAFAFC'))
        name_height=p.fontMetrics().height()
        status_font=QFont(f);status_font.setPixelSize(10 if sys.platform=='win32' else 9);set_ui_weight(status_font,400)
        from PySide6.QtGui import QFontMetrics
        status_height=QFontMetrics(status_font).height()
        gap=2 if self.status else 0
        top=(self.height()-name_height-status_height-gap)/2-PROFILE_TEXT_RISE_PX if self.status else 0
        text_width=self.width()-68
        if alpha>0 and text_width>0:
            p.drawText(QRectF(46,top,text_width,name_height if self.status else self.height()),Qt.AlignVCenter,p.fontMetrics().elidedText(self.username,Qt.ElideRight,text_width))
            if self.status:
                p.setFont(status_font);p.setPen(QColor(c['rail_item_ink']));p.drawText(QRectF(46,top+name_height+gap,text_width,status_height),Qt.AlignVCenter,p.fontMetrics().elidedText(self.status,Qt.ElideRight,text_width))
            icon('right',c['rail_item_ink']).paint(p,QRect(self.width()-20,(self.height()-12)//2,12,12))
        p.restore()
        if keyboard_focus_visible(self):
            paint_rounded_surface(p,QRectF(self.rect()).adjusted(2,2,-2,-2),RADIUS_CONTROL_PX,QColor(Qt.transparent),QColor(c['link']))
        p.end()


class SidebarToggle(QPushButton):
    def __init__(self,parent):
        super().__init__(parent)
        self.setFixedSize(34,32)
        self.setCursor(Qt.PointingHandCursor)
        self.setCheckable(True)
        self.angle=0.

    def paintEvent(self,event):
        c=palette()
        p=QPainter(self)
        fill=c['rail_item_hover'] if self.underMouse() or self.isDown() else c['rail_item']
        paint_rounded_surface(p,self.rect(),RADIUS_CONTROL_PX,ink(fill),
                              ink(c['rail_active']) if keyboard_focus_visible(self) else None)
        center=QRectF(self.rect()).center()
        p.translate(center);p.rotate(self.angle);p.translate(-center)
        icon('left',c['rail_item_ink']).paint(p,QRect((self.width()-16)//2,(self.height()-16)//2,16,16))
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

    def selection_pose(self,origin=QPointF()):
        from item_transitions import FileSelection
        c=palette()
        chosen=self.isChecked()
        rect=QRectF((self.width()-15)/2,(self.height()-15)/2,15,15).translated(origin)
        fill=c['action'] if chosen else c['hover'] if self.underMouse() or self.isDown() else c['card']
        edge=c['action'] if chosen else c['ring'] if self.underMouse() else c['input_border']
        return FileSelection(rect,ink(fill),ink(edge),ink(c['on_action']) if chosen else QColor(Qt.transparent))

    def paintEvent(self,event):
        if outside_content_view(self):return
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():p.setOpacity(.5)
        pose=self.selection_pose()
        pose.paint(p)
        if keyboard_focus_visible(self):
            paint_rounded_surface(p,pose.rect.adjusted(-1,-1,1,1),8.5,QColor(Qt.transparent),QColor(palette()['ring']))
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
        cap=rect.height()/2.0
        path=QPainterPath();path.addRoundedRect(rect,cap,cap)
        p.fillPath(path,QColor(c['subcard']))
        if self.paint_fraction:
            p.save();p.setClipPath(path)
            p.fillRect(QRectF(rect.left(),rect.top(),rect.width()*self.paint_fraction,rect.height()),ink(c['transfer_progress_fill']))
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
    draw(palette()['text']);painter.restore()


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
        p=QStylePainter(self);self._paint_surface(p)
        p.setFont(self.font());p.setPen(self.palette().color(QPalette.ButtonText))
        text=p.fontMetrics().elidedText(self.text(),Qt.ElideRight,max(0,self.width()-8))
        x=(self.width()-p.fontMetrics().horizontalAdvance(text))/2
        p.drawText(QPointF(x,optical_text_baseline(p.font(),text,self.height())),text);p.end()


class FilenameEdit(QTextEdit):
    """Wrap one filename without accepting hard line breaks."""
    submitted=Signal()
    height_changed=Signal(int)

    def __init__(self,parent=None):
        super().__init__(parent)
        self._preediting=False
        self._measure_document=QTextDocument(self)
        self._measure_document.setDocumentMargin(0)
        option=QTextOption();option.setWrapMode(QTextOption.WrapAnywhere)
        self._measure_document.setDefaultTextOption(option)
        self._measure_timer=QTimer(self);self._measure_timer.setSingleShot(True)
        self._measure_timer.timeout.connect(self._update_height)
        self.setAcceptRichText(False);self.setAcceptDrops(False);self.setTabChangesFocus(True)
        self.setVerticalScrollBar(RoundedScrollBar(Qt.Vertical,self,thumb_color=lambda:palette()['secondary']))
        self.setAccessibleName('文件名')
        self.setWordWrapMode(QTextOption.WrapAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.document().setDocumentMargin(0)
        self.setFixedHeight(RENAME_EDIT_MIN_HEIGHT_PX)
        self.textChanged.connect(self._schedule_measure)

    def event(self,event):
        if event.type()==QEvent.Paint:
            c=palette();p=QPainter(self)
            paint_rounded_surface(p,self.rect(),RADIUS_CONTROL_PX,ink(c['input']),
                                  ink(c['ring'] if self.hasFocus() else c['input_border']))
            p.end()
        return super().event(event)

    def set_filename(self,name):
        self.setPlainText(name)
        self._measure_timer.stop();self._update_height()

    def _schedule_measure(self,*_):
        if not self._measure_timer.isActive():self._measure_timer.start(0)

    def _update_height(self):
        horizontal=2*(RENAME_EDIT_PADDING_X_PX+RENAME_EDIT_BORDER_PX)
        vertical=2*(RENAME_EDIT_PADDING_Y_PX+RENAME_EDIT_BORDER_PX)
        self._measure_document.setDefaultFont(self.font())
        self._measure_document.setTextWidth(max(1,self.width()-horizontal))
        self._measure_document.setPlainText(self.toPlainText())
        natural=math.ceil(self._measure_document.size().height())+vertical
        maximum=max(RENAME_EDIT_MIN_HEIGHT_PX,self.fontMetrics().lineSpacing()*RENAME_EDIT_MAX_LINES+vertical)
        height=max(RENAME_EDIT_MIN_HEIGHT_PX,min(natural,maximum))
        if height!=self.height():
            self.setFixedHeight(height);self.height_changed.emit(height)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'_measure_timer'):self._schedule_measure()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange,QEvent.StyleChange) and hasattr(self,'_measure_timer'):
            self._schedule_measure()

    def inputMethodEvent(self,event):
        self._preediting=bool(event.preeditString())
        super().inputMethodEvent(event)

    def focusOutEvent(self,event):
        super().focusOutEvent(event);self._preediting=False;self.update()

    def focusInEvent(self,event):
        super().focusInEvent(event);self.update()

    def keyPressEvent(self,event):
        if event.key() in (Qt.Key_Return,Qt.Key_Enter):
            if self._preediting:
                from PySide6.QtGui import QGuiApplication
                QGuiApplication.inputMethod().commit()
            else:self.submitted.emit()
            event.accept();return
        super().keyPressEvent(event)

    def insertFromMimeData(self,source):
        if not source.hasText():return
        text=re.sub(r'[\r\n\u2028\u2029]+',' ',source.text())
        cursor=self.textCursor();cursor.insertText(text);self.setTextCursor(cursor)


class _RenameSurface(RoundedSurfaceFrame):
    def surface_style(self):
        dark=ui_theme.current_theme()=='dark';c=palette()
        return c['dialog_surface'] if dark else '#F6F6F6', 'rgba(255,255,255,20)' if dark else 'rgba(0,0,0,23)',c['radius']


class RenameDialog(QDialog):
    """A presentation-only sheet reusing the shelf's existing rename editor."""
    def __init__(self,shelf):
        super().__init__(shelf,Qt.Dialog|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        from collections import OrderedDict
        self.shelf=shelf;self._shadow_cache=OrderedDict()
        self.setAttribute(Qt.WA_TranslucentBackground);self.setWindowModality(Qt.WindowModal)
        self.setWindowTitle('重命名文件');self.setFixedSize(420,RENAME_DIALOG_BASE_HEIGHT_PX)
        layout=QVBoxLayout(self);layout.setContentsMargins(20,20,20,20)
        self.card=_RenameSurface(self);self.card.setObjectName('renameSheet')
        css(self.card,'QFrame#renameSheet{background:transparent;border:0;}')
        form=QVBoxLayout(self.card);form.setContentsMargins(20,20,20,20);form.setSpacing(16)
        header=QHBoxLayout();header.addWidget(label('重命名文件',14,600,parent=self.card),1)
        close=ToolbarButton('close',self.card,motion=shelf.manager.motion);close.setFixedSize(32,32);close.setAutoDefault(False);close.setAccessibleName('关闭名称编辑');close.clicked.connect(self.reject);header.addWidget(close);form.addLayout(header)
        form.addWidget(label('修改扩展名可能导致文件不可用',12,400,'secondary',self.card))
        edit=shelf.rename_edit;edit.setParent(self.card)
        css(edit,*[f"QTextEdit{{background:transparent;color:{colors(t)['text']};border:0;padding:{RENAME_EDIT_PADDING_Y_PX+1}px {RENAME_EDIT_PADDING_X_PX+1}px;font:12px 'FlickDock UI';}} "
                   f"QScrollBar:vertical{{width:6px;background:transparent;margin:2px 0;}} "
                   f"QScrollBar::handle:vertical{{background:transparent;min-height:20px;}} "
                   "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}" for t in ('dark','light')])
        form.addWidget(edit);actions=QHBoxLayout();actions.addStretch(1)
        cancel=button('取消',variant='secondary',parent=self.card);cancel.setAutoDefault(False);cancel.clicked.connect(self.reject)
        self.confirm=button('确定',variant='primary',parent=self.card);self.confirm.clicked.connect(self._commit)
        self.confirm.setAutoDefault(True);self.confirm.setDefault(True)
        actions.addWidget(cancel);actions.addWidget(self.confirm);form.addLayout(actions);layout.addWidget(self.card)
        edit.submitted.connect(self._commit)
        edit.height_changed.connect(self._resize_for_editor)
        self.rejected.connect(edit.hide)

    def _resize_for_editor(self,height):
        self.setFixedHeight(RENAME_DIALOG_BASE_HEIGHT_PX+height-RENAME_EDIT_MIN_HEIGHT_PX)

    def _commit(self):
        if self.shelf.commit_rename():self.accept()

    def paintEvent(self,event):
        from surface_morph import paint_surface_shadow
        p=QPainter(self);p.setCompositionMode(QPainter.CompositionMode_Source);p.fillRect(self.rect(),Qt.transparent)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver);paint_surface_shadow(p,QRectF(self.card.geometry()),self._shadow_cache)
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


def thumbnail(shelf,ref,size=40,dpr=None):
    # Artwork is static across scroll frames. Status badges remain live.
    store=getattr(shelf,'_reference_thumbnail_cache',None)
    if store is None:store=shelf._reference_thumbnail_cache=OrderedDict()
    ratio=float(dpr or shelf.devicePixelRatioF() or 1.)
    key=(ref,int(size),ui_theme.current_theme(),ratio)
    if key in store:
        store.move_to_end(key);return store[key]
    pm=QPixmap(round(size*ratio),round(size*ratio));pm.setDevicePixelRatio(ratio);pm.fill(Qt.transparent)
    p=QPainter(pm);p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
    rect=QRectF(.5,.5,size-1,size-1);category=shelf.content.category(ref)
    backgrounds={'images':'#26364A','video':'#202344','audio':'#7E22CE','text':'#4A3D28','links':'#164354'}
    ext=Path(shelf.content.title(ref)).suffix.lower()
    bg=backgrounds.get(category,'#3B3B3F' if ext=='.zip' else '#593140' if ext=='.pdf' else '#263A55')
    paint_rounded_surface(p,QRectF(0,0,size,size),size*.25,QColor(bg),QColor(255,255,255,38))
    cached=getattr(shelf,'preview_cache',{}).get(ref)
    actual=category=='images' or ext in ('.lnk','.exe','.app') or cached is not None and not cached.isNull()
    if actual:
        provider=getattr(shelf,'item_pixmap',shelf.content.item_pixmap)
        art=provider(ref,round(size),dpr=ratio)
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
        size=min(self.width(),self.height());art=thumbnail(self.shelf,self.ref,size,self.devicePixelRatioF())
        p.drawPixmap(0,0,art)
        draw_status(p,QRectF(size-18,size-18,18,18),self.cloud_status,self.repository.transfers.get(self.ref))
        pins=getattr(self.shelf,'file_pins',None)
        if not self.property('suppressPinBadge') and pins and pins.contains(self.ref):
            icon(*_shelf_pin_glyph(palette())).paint(p,QRect(size-14,0,14,14))
        p.end()


class MiniStack(StackTile):
    uses_default_artwork=False
    SOLO_LIFT_PX=5.
    SOLO_HOVER_SCALE=1.025
    EMPTY_DROP_SIZE=QSize(216,148)

    def __init__(self,shelf,parent=None,motion=True):
        super().__init__(parent,motion)
        self.shelf=shelf
        self._card_art={};self._card_dpr=0;self._card_theme=None
        self._card_rim=QPainterPath()
        self._card_rim.addRoundedRect(QRectF(.5,.5,191,127),RADIUS_PANEL_PX,RADIUS_PANEL_PX)
        self._fan_index=None
        self._fan_lift=Spring(self,dict.fromkeys(range(5),0.),lambda _:self.update(),motion)
        self._fan_scale=Spring(self,dict.fromkeys(range(5),1.),lambda _:self.update(),motion)
        from native_ui_common import DropBorderMotion
        self.border_motion=DropBorderMotion(self)

    def set_drop_preview(self,active,point=None,*,accepted=False):
        if self.refs:return
        if active:
            self.border_motion.move_drag(point if point is not None else self.rect().center())
        else:self.border_motion.end_drag(accepted=accepted)

    def _retarget_spread(self):
        from native_ui_common import reduced_motion
        value=float(self._hovered and len(self.refs)>1)
        if not self.motion or reduced_motion():self.spread.jump({'spread':value})
        else:self.spread.animate_physical({'spread':value},220,18)

    def set_hover(self,value, point=None):
        super().set_hover(value)
        if not self.refs:self.border_motion.set_active(value or self.border_motion.dragging)
        hovered=None
        if value and self.refs and point is not None:
            # Use the resting fan for hit testing. Raising a card must not
            # move its hit area or change which card wins an overlap.
            for ref,transform,_,_ in reversed(self.presentation_cards(hit_test=True)):
                inverse,valid=transform.inverted()
                if valid and QRectF(0,0,192,128).contains(inverse.map(QPointF(point))):
                    hovered=self.refs.index(ref)
                    break
        self._set_fan_index(hovered)

    def _set_fan_index(self,index,*,snap=False):
        if self._fan_index==index and not snap:return
        self._fan_index=index
        solo=len(self.refs)==1
        distance=self.SOLO_LIFT_PX if solo else 22.
        magnification=self.SOLO_HOVER_SCALE if solo else 1.06
        lift={i:-distance if i==index else 0. for i in range(5)}
        scale={i:magnification if i==index else 1. for i in range(5)}
        from native_ui_common import reduced_motion
        if snap or not self.motion or reduced_motion():
            self._fan_lift.jump(lift);self._fan_scale.jump(scale)
        else:
            self._fan_lift.animate_physical(lift,420,20)
            self._fan_scale.animate_physical(scale,420,22)

    def set_pressed(self,value):
        super().set_pressed(value)
        if value and self._fan_index is not None:
            if len(self.refs)>1:self._fan_lift.kick({self._fan_index:-260.})

    def hideEvent(self,event):
        self._hovered=False;self.border_motion.set_active(False)
        self._set_fan_index(None,snap=True)
        super().hideEvent(event)

    def set_stack(self,repository,refs,preview_cache=None):
        previous=self.refs
        super().set_stack(repository,refs,preview_cache)
        if previous!=self.refs:self._set_fan_index(None,snap=True)
        if self.refs:
            if not previous:self.border_motion.reset()
        else:self.border_motion.set_active(self.border_motion.dragging or self._hovered)
        self._cache_cards()

    def _cache_cards(self):
        ratio=self.devicePixelRatioF();cards={};content=self.shelf.content
        font_key=self.font().toString();theme=ui_theme.current_theme()
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
                 cover,ratio,font_key,theme,depth)
            previous=self._card_art.get(ref)
            cards[ref]=previous if previous is not None and previous[0]==key else (key,self._render_card(ref,ratio,depth))
        self._card_art=cards;self._card_dpr=ratio;self._card_theme=theme

    def _render_card(self,ref,ratio,depth):
        result=QPixmap(round(192*ratio),round(128*ratio));result.setDevicePixelRatio(ratio);result.fill(Qt.transparent)
        p=QPainter(result);p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform);c=palette()
        edge=QColor(255,255,255,38) if ui_theme.current_theme()=='dark' else QColor('#D4D4D4')
        paint_rounded_surface(p,QRectF(0,0,192,128),RADIUS_PANEL_PX,QColor(c['stack_cards'][depth]),edge)
        p.drawPixmap(14,14,thumbnail(self.shelf,ref,46,ratio))
        title=self.shelf.content.title(ref);f=QFont(self.font());f.setPixelSize(13 if sys.platform=='win32' else 11);set_ui_weight(f,600);p.setFont(f);p.setPen(QColor(c['text']))
        p.drawText(QRectF(14,77,164,20),Qt.AlignVCenter,p.fontMetrics().elidedText(title,Qt.ElideRight,164))
        f.setPixelSize(11 if sys.platform=='win32' else 9);set_ui_weight(f,400);p.setFont(f);p.setPen(QColor(c['file_description']));p.drawText(QRectF(14,99,164,18),Qt.AlignVCenter,item_detail(self.shelf,ref).split(' · ')[0])
        ext=Path(title).suffix.lstrip('.').upper() or ('TEXT' if self.shelf.content.is_text(ref) else 'FILE')
        p.setPen(QColor(c['secondary']))
        p.drawText(QRectF(100,14,78,18),Qt.AlignRight|Qt.AlignVCenter,ext[:7]);p.end();return result

    def presentation_cards(self,*,hit_test=False):
        v={'x':0.,'y':0.,'sx':1.,'sy':1.} if hit_test else self.spring.values
        spread=float(len(self.refs)>1) if hit_test else self.spread.values['spread']
        center=QPointF(self.width()/2,self.height()/2)
        base=QTransform();base.translate(center.x(),center.y()+v['y']);base.scale(v['sx'],v['sy']);base.translate(-center.x(),-center.y())
        refs=list(reversed(self.refs));frames=[];front=None
        transforms=((0,1,0,1),(-7,.95,-2,.9),(-14,.9,2.2,.75),(-20,.85,-1.5,.58),(-26,.8,1,.4))
        # Leave room for the outside corner when a fanned card rotates around
        # its bottom edge and grows under the pointer.
        span=min(78.,max(14.,self.width()/2-107))
        slots=sorted((index-(len(refs)-1)/2 for index in range(len(refs))),key=abs)
        for idx in reversed(range(len(refs))):
            dy,scale,angle,opacity=transforms[idx]
            source=len(refs)-1-idx
            slot=slots[idx]
            direction=slot/max(1.,(len(refs)-1)/2)
            x=(-idx*2)*(1-spread)+direction*span*spread
            y=dy*(1-spread)+(abs(direction)*12-10)*spread
            lift=0. if hit_test else self._fan_lift.values[source]
            y+=lift
            local=QTransform();local.translate(center.x()+x,center.y()+64+y)
            local.rotate(angle*(1-spread)+12*direction*spread)
            scale=scale*(1-spread)+.83*spread
            if not hit_test:scale*=self._fan_scale.values[source]
            local.scale(scale,scale);local.translate(-96,-128)
            if idx==0 and not hit_test:local.translate(0,-STACK_ARRIVAL_OFFSET_PX*(1-self.arrival))
            opacity=opacity*(1-spread)+spread
            transform=local*base
            if len(refs)==1 and lift<0.:
                # Arrival and hover can overlap briefly. Keep the artwork
                # inside its paint viewport even at the combined peak.
                top=transform.mapRect(QRectF(0,0,192,128)).top()
                if top<3.:transform*=QTransform.fromTranslate(0.,3.-top)
            frame=(refs[idx],transform,max(0.,min(opacity,1.)) if ui_theme.current_theme()=='dark' else 1.,idx==0)
            if not hit_test and source==self._fan_index:front=frame
            else:frames.append(frame)
        if front is not None:frames.append(front)
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

    def empty_drop_rect(self):
        width=min(self.EMPTY_DROP_SIZE.width(),self.width()-12)
        height=min(self.EMPTY_DROP_SIZE.height(),self.height()-12)
        return QRectF((self.width()-width)/2,(self.height()-height)/2+5,width,height)

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHints(QPainter.Antialiasing|QPainter.SmoothPixmapTransform)
        if not self.refs:
            r=self.empty_drop_rect()
            path=QPainterPath();path.addRoundedRect(r,EMPTY_DROP_RADIUS_PX,EMPTY_DROP_RADIUS_PX)
            self.border_motion.paint(p,path,dark=ui_theme.current_theme()=='dark',radius=EMPTY_DROP_RADIUS_PX)
            icon('upload',palette()['secondary']).paint(p,QRect(round(r.center().x()-12),round(r.center().y()-31),24,24))
            f=p.font()
            f.setPixelSize(13);p.setFont(f);p.setPen(QColor(palette()['text']))
            p.drawText(r.adjusted(0,34,0,0),Qt.AlignCenter,'松手添加' if self.border_motion.dragging else COMPACT_EMPTY_LABEL)
            p.end();return
        if self._card_dpr!=self.devicePixelRatioF() or self._card_theme!=ui_theme.current_theme():self._cache_cards()
        reveal=getattr(QApplication.instance(),'_flickdock_border_reveal',None)
        for ref,transform,opacity,front in self.presentation_cards():
            p.save();p.setOpacity(opacity);p.setTransform(transform,True)
            self.paint_card(p,ref,front);p.restore()
            if reveal is not None:
                # Cached card art is painted onto a pixmap, so the app's
                # pointer light needs the live, transformed card outline.
                p.save();p.setOpacity(opacity)
                reveal.paint_stroke(p,transform.map(self._card_rim),1/self.devicePixelRatioF())
                p.restore()
        p.end()


def build_overview(view,preview_type):
    """Build the HTML's 52px toolbar and 210 / flexible / 340 split view."""
    view.setObjectName('referenceOverview')
    css(view,"QFrame#referenceOverview{background:transparent;border:0;}")
    root=QVBoxLayout(view);root.setContentsMargins(1,1,1,1);root.setSpacing(0)
    view.header=QFrame(view);view.header.setFixedHeight(SHELF_OVERVIEW_HEADER_HEIGHT_PX)
    css(view.header,"QFrame{background:transparent;border:0;}")
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
    view.count_label=label('',10,600,'secondary',view.header,control_type=RoundedBadge);view.count_label.setAlignment(Qt.AlignCenter)
    view.count_label.set_theme_backgrounds('rgba(255,255,255,28)','rgba(0,0,0,18)')
    css(view.count_label,"color:#D4D4D8;background:transparent;border:0;padding:1px 9px;font:600 10px 'FlickDock UI';",
        "color:#515154;background:transparent;border:0;padding:1px 9px;font:600 10px 'FlickDock UI';")
    view.count_label.setFixedHeight(20);brand.addWidget(view.count_label);brand.addStretch();head.addWidget(view.header_brand)
    view.header_tools=QWidget(view.header)
    tools=QHBoxLayout(view.header_tools);tools.setContentsMargins(16,10,12,10);tools.setSpacing(9);tools.addStretch()
    view.search_edit=RoundedSearchEdit(view.header);view.search_edit.setPlaceholderText('搜索...');view.search_edit.setFixedSize(176,32)
    view.search_edit.setClearButtonEnabled(True)
    view.search_edit.setAccessibleName('搜索搁架内容');view.search_edit.textChanged.connect(lambda _:view.rebuild())
    css(view.search_edit,*[f"QLineEdit{{background:transparent;color:{colors(t)['text']};border:0;padding:5px 11px;font:12px 'FlickDock UI';}} QLineEdit:disabled{{color:{colors(t)['muted']};}}" for t in ('dark','light')])
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
    body=QHBoxLayout();body.setContentsMargins(12,6,12,12);body.setSpacing(12);view.body_layout=body
    view.sidebar=SelectionSurface(view,vertical=True,color_role='rail_active',border_role=None,radius=20,
                                  motion=lambda:getattr(view.shelf.manager,'motion',True))
    view.sidebar.setObjectName('referenceSidebar');view.sidebar.setFixedWidth(206)
    view.sidebar.set_surface_background('rail','rail',RADIUS_PANEL_PX)
    css(view.sidebar,'QFrame#referenceSidebar{background:transparent;border:0;}')
    side=QVBoxLayout(view.sidebar);side.setContentsMargins(10,12,10,12);side.setSpacing(4)
    view.sidebar_toggle=SidebarToggle(view.sidebar)
    side.addWidget(view.sidebar_toggle,0,Qt.AlignRight)
    view.scope_group=QButtonGroup(view);view.scope_group.setExclusive(True);view.category_buttons={};view.sidebar_buttons={};view.nav_counts={}
    glyphs={'all':'all','pinned':'star','uploading':'transferring'}
    for key,title in view.SCOPES:
        control=button(title,glyphs[key],parent=view.sidebar);control.setCheckable(True);control.setFixedHeight(40)
        control.setProperty('leftAligned',True)
        css(control,*[f"QPushButton{{background:transparent;color:{colors(t)['rail_item_ink']};border:0;text-align:left;padding:7px 14px;font:500 12px 'FlickDock UI';}} QPushButton:checked,QPushButton:checked:hover{{color:{colors(t)['rail_active_ink']};font-weight:600;}}" for t in ('dark','light')])
        control.clicked.connect(lambda checked=False,key=key:view.set_scope(key));view.scope_group.addButton(control);side.addWidget(control);view.sidebar.add_selection_button(control)
        count=label('',11,400,'muted',control,control_type=RoundedBadge);count.setAlignment(Qt.AlignCenter);count.setGeometry(145,10,28,20);count.setAttribute(Qt.WA_TransparentForMouseEvents)
        view.nav_counts[key]=count;view.category_buttons[key]=control;view.sidebar_buttons[key]=control
    view.transfer_badge=view.nav_counts['uploading']
    view.sidebar.installEventFilter(_SidebarHitBridge(view))
    side.addStretch(1)
    view.wan_nav=button('远程传输','web','secondary',view.sidebar);view.wan_nav.hide()
    view.translator_nav=button('翻译器','translate',variant='text',parent=view.sidebar);view.translator_nav.setFixedHeight(34);view.translator_nav.clicked.connect(lambda:view.shelf.manager.show_translator_dialog());side.addWidget(view.translator_nav)
    view.video_nav=button('视频解析','video',variant='text',parent=view.sidebar);view.video_nav.setFixedHeight(34);view.video_nav.clicked.connect(lambda:view.shelf.manager.show_video_dialog());side.addWidget(view.video_nav)
    view.lan_nav=button('文件互传','nearby',variant='text',parent=view.sidebar);view.lan_nav.setFixedHeight(34);view.lan_nav.clicked.connect(lambda:view.shelf.manager.show_transfer_dialog());side.addWidget(view.lan_nav)
    view.settings_nav=button('设置','settings',variant='text',parent=view.sidebar);view.settings_nav.setFixedHeight(34);view.settings_nav.clicked.connect(lambda:view.shelf.manager.show_settings());side.addWidget(view.settings_nav)
    for control in (view.translator_nav,view.video_nav,view.lan_nav,view.settings_nav):control.setProperty('sidebarUtility',True);control.setFixedHeight(40)
    for control in (view.wan_nav,view.translator_nav,view.video_nav,view.lan_nav,view.settings_nav):
        control.setProperty('leftAligned',True)
        old=control._reference_style
        css(control,*[s.replace('padding:4px 11px;','padding:4px 12px;text-align:left;') for s in old])
    view.account_nav=ProfileButton(view.sidebar);view.account_nav.clicked.connect(lambda:view.shelf.manager.show_settings());side.addWidget(view.account_nav)
    body.addWidget(view.sidebar)
    view.middle=BentoPanelFrame(view);view.middle.setObjectName('referenceMiddle');mid=QVBoxLayout(view.middle);view.middle_layout=mid;mid.setContentsMargins(0,4,0,0);mid.setSpacing(0)
    view.selection_bar=QFrame(view.middle);view.selection_bar.setFixedHeight(56)
    css(view.selection_bar,"QFrame{border:0;background:transparent;}")
    selection=QVBoxLayout(view.selection_bar);selection.setContentsMargins(0,0,0,0);selection.setSpacing(0)
    view.selection_navigation=QWidget(view.selection_bar);view.selection_navigation.setFixedHeight(36)
    navigation=QHBoxLayout(view.selection_navigation);navigation.setContentsMargins(16,0,12,0);navigation.setSpacing(8)
    selection.addWidget(view.selection_navigation);view.selection_navigation.hide()
    view.selection_actions=QWidget(view.selection_bar);view.selection_actions.setFixedHeight(56);selection.addWidget(view.selection_actions)
    select=QHBoxLayout(view.selection_actions);select.setContentsMargins(14,10,14,14);select.setSpacing(6)
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
    mid.addWidget(InsetDivider(view.middle,16))
    view.type_bar=QScrollArea(view.middle);view.type_bar.setWidgetResizable(True);view.type_bar.setFrameShape(QFrame.NoFrame);view.type_bar.setFixedHeight(44)
    view.type_bar.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff);view.type_bar.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    css(view.type_bar,'QScrollArea{background:transparent;border:0;} QScrollBar:horizontal{height:4px;background:transparent;} QScrollBar::handle:horizontal{background:rgba(128,128,128,70);border-radius:2px;min-width:24px;} QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0;} QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal{background:transparent;}')
    from ui_transitions import TabPillSurface
    view.type_body=TabPillSurface(view.type_bar,motion=lambda:view.shelf.manager.motion)
    type_line=QHBoxLayout(view.type_body);type_line.setContentsMargins(12,8,12,8);type_line.setSpacing(4)
    view.category_group=QButtonGroup(view);view.category_group.setExclusive(True);view.type_buttons={}
    for key,title in view.TYPES:
        control=button(title+' 0',parent=view.type_body);control.setCheckable(True);control.setFixedHeight(26)
        css(control,*[f"QPushButton{{background:transparent;color:{colors(t)['secondary']};border:0;padding:3px 8px;font:11px 'FlickDock UI';}} QPushButton:checked{{color:{colors(t)['text']};}}" for t in ('dark','light')])
        control.clicked.connect(lambda checked=False,key=key:view.set_type(key));view.category_group.addButton(control);type_line.addWidget(control);view.type_body.add_selection_button(control)
        view.type_buttons[key]=control
    type_line.addStretch(1);view.type_bar.setWidget(view.type_body);view.type_body.setAutoFillBackground(False);view.type_bar.viewport().setAutoFillBackground(False)
    view.scope_buttons=dict(view.sidebar_buttons)
    view.category_buttons.update(view.type_buttons);view.category_buttons['all']=view.scope_buttons['all']
    view.sidebar_buttons.update({key:control for key,control in view.type_buttons.items() if key!='all'})
    mid.addWidget(view.type_bar)
    view.pin_error=label('',10,400,'red',view.middle);view.pin_error.setWordWrap(True);view.pin_error.hide();mid.addWidget(view.pin_error)
    view.scroll=QScrollArea(view.middle);view.scroll.setWidgetResizable(True);view.scroll.setFrameShape(QFrame.NoFrame);view.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);view.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    view.scroll.setProperty('scrollBackdropRole','card')
    view.scroll.setProperty('scrollBackdropFromSnapshot',True)
    view.scroll.setVerticalScrollBar(RoundedScrollBar(Qt.Vertical,view.scroll,thumb_color=lambda:'#49494E' if ui_theme.current_theme()=='dark' else '#C8C8C8'))
    css(view.scroll,'QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::handle:vertical{background:transparent;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}')
    view.rows_body=QWidget();view.rows_layout=QVBoxLayout(view.rows_body);view.rows_layout.setContentsMargins(12,4,12,12);view.rows_layout.setSpacing(6);view.scroll.setWidget(view.rows_body)
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
    view.clipboard_footer=QWidget(view.middle);view.clipboard_footer.setObjectName('overviewFooter');view.clipboard_footer.setFixedHeight(52)
    css(view.clipboard_footer,"QWidget#overviewFooter{background:transparent;border:0;}")
    clipboard_row=QHBoxLayout(view.clipboard_footer);clipboard_row.setContentsMargins(14,0,14,0)
    view.clipboard_button=ClipboardButton(view.shelf.manager,view.clipboard_footer)
    clipboard_row.setSpacing(8)
    from shelf_capacity import ShelfCapacityButton
    view.capacity_button=ShelfCapacityButton(view.clipboard_footer,show_value=True)
    clipboard_row.addWidget(view.capacity_button,0,Qt.AlignVCenter);clipboard_row.addWidget(view.clipboard_button,0,Qt.AlignVCenter)
    clipboard_row.addWidget(view.transfer_strip,1,Qt.AlignVCenter);clipboard_row.addStretch()
    sync=getattr(view.shelf.manager,'sync',None)
    if sync is not None:
        view.capacity_button.bind_sync(sync)
    mid.addWidget(InsetDivider(view.middle,16))
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
    css(view,"QFrame#referencePreview{background:transparent;border:0;}")
    root=QVBoxLayout(view);root.setContentsMargins(0,0,0,0);root.setSpacing(0)
    head=QFrame(view);view.preview_head=head;head.setFixedHeight(64)
    css(head,"QFrame{border:0;background:transparent;}")
    header=QHBoxLayout(head);header.setContentsMargins(16,8,16,8);header.setSpacing(8)
    names=QVBoxLayout();names.setSpacing(0)
    view.name=elided_type(view);view.info=elided_type(view);view.upload_time=elided_type(view)
    view.upload_time.setProperty('flickdockTechnicalFont',True)
    for item,size,weight,tint in ((view.name,12,600,'text'),(view.upload_time,9,400,'secondary')):
        css(item,f"color:{colors('dark')[tint]};border:0;background:transparent;font:{weight} {size}px 'FlickDock UI';",
            f"color:{colors('light')[tint]};border:0;background:transparent;font:{weight} {size}px 'FlickDock UI';")
        names.addWidget(item)
    view.name.setFixedHeight(20);view.upload_time.setFixedHeight(16)
    header.addLayout(names,1)
    reserved=QWidget(head);reserved.setFixedSize(PREVIEW_HEADER_RESERVED_ACTION_WIDTH_PX,32)
    reserved.setAttribute(Qt.WA_TransparentForMouseEvents);header.addWidget(reserved)
    view.copy_button=ToolbarButton('copy',view,motion=view.shelf.manager.motion);view.copy_button.setFixedSize(32,32);view.copy_button.setAccessibleName('复制内容');view.copy_button.clicked.connect(view.copy_current);header.addWidget(view.copy_button);root.addWidget(head)
    view.content_scroll=QScrollArea(view);view.content_scroll.setWidgetResizable(True);view.content_scroll.setFrameShape(QFrame.NoFrame)
    view.content_scroll.setVerticalScrollBar(RoundedScrollBar(Qt.Vertical,view.content_scroll,thumb_color=lambda:'#49494E' if ui_theme.current_theme()=='dark' else '#C8C8C8'))
    view.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    css(view.content_scroll,'QScrollArea{background:transparent;border:0;} QScrollBar:vertical{width:6px;background:transparent;} QScrollBar::handle:vertical{background:transparent;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}')
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
    view.stack=PreviewSurfaceStack(content);view.stack.setMinimumHeight(176);view.stack.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
    view.icon=content_type(view,motion=view.shelf.manager.motion)
    view.text=RoundedPreviewText(view);view.text.setReadOnly(True);view.text.setAcceptDrops(False);view.text.viewport().setAcceptDrops(False)
    if sys.platform=='darwin':view.text.setProperty('scrollEdgeBlur',False)
    css(view.text,"QPlainTextEdit{background:transparent;color:#F5F5F7;border:0;padding:14px;font:13px 'FlickDock UI';}",
        "QPlainTextEdit{background:transparent;color:#1D1D1F;border:0;padding:14px;font:13px 'FlickDock UI';}")
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
    status=PreviewStatusCard(content);view.status_card=status;status.setObjectName('referenceStatus');status.setMinimumWidth(0)
    size_card=PreviewSurfaceFrame(content);view.size_card=size_card;size_card.setObjectName('referenceSize');size_card.setMinimumWidth(0)
    view.cloud_action=StatusCardAction('cloud-upload',status,motion=view.shelf.manager.motion)
    view.cloud_action.setProperty('disabledOpacity',.72)
    view.cloud_action.setProperty('glyphSize',PREVIEW_STATUS_ACTION_GLYPH_SIZE_PX)
    view.cloud_action.setFixedSize(PREVIEW_STATUS_ACTION_SIZE_PX,PREVIEW_STATUS_ACTION_SIZE_PX)
    view.cloud_action.clicked.connect(view.toggle_cloud_copy)
    box=QVBoxLayout(status);box.setContentsMargins(PREVIEW_INFO_HORIZONTAL_PADDING_PX,PREVIEW_INFO_VERTICAL_PADDING_PX,PREVIEW_INFO_HORIZONTAL_PADDING_PX,PREVIEW_INFO_BOTTOM_PADDING_PX);box.setSpacing(PREVIEW_INFO_ROW_GAP_PX)
    from transfer_visuals import CloudStatusBadge
    status_title=label('同步状态',10,400,'muted',status);status_title.setFixedHeight(PREVIEW_INFO_TITLE_HEIGHT_PX);box.addWidget(status_title)
    line=QHBoxLayout();line.setContentsMargins(0,0,0,0);line.setSpacing(2)
    view.cloud_badge=CloudStatusBadge(status,file_presence=True);view.cloud_badge.hide()
    view.status_value=label('',10,400,'green',status);view.status_value.setWordWrap(True)
    view.status_value.setMinimumWidth(0);view.status_value.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
    line.addWidget(view.status_value,1);box.addLayout(line);status.set_hover_action(view.cloud_action)
    size_box=QVBoxLayout(size_card);size_box.setContentsMargins(PREVIEW_INFO_HORIZONTAL_PADDING_PX,PREVIEW_INFO_VERTICAL_PADDING_PX,PREVIEW_INFO_HORIZONTAL_PADDING_PX,PREVIEW_INFO_BOTTOM_PADDING_PX);size_box.setSpacing(PREVIEW_INFO_ROW_GAP_PX)
    size_title=label('文件大小与类型',10,400,'muted',size_card);size_title.setFixedHeight(PREVIEW_INFO_TITLE_HEIGHT_PX);size_box.addWidget(size_title)
    view.info.setParent(size_card);view.info.setFixedHeight(PREVIEW_INFO_VALUE_HEIGHT_PX);view.info.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
    css(view.info,"color:#F5F5F7;border:0;background:transparent;font:400 10px 'FlickDock UI';",
        "color:#1D1D1F;border:0;background:transparent;font:400 10px 'FlickDock UI';")
    size_box.addWidget(view.info);cards.addWidget(status,1);cards.addWidget(size_card,1);body.addLayout(cards)
    path_card=PreviewSurfaceFrame(content);view.path_card=path_card;path_card.setObjectName('referencePath')
    path_box=QVBoxLayout(path_card);path_box.setContentsMargins(12,8,12,10);path_box.setSpacing(4)
    path_head=QHBoxLayout();path_head.addWidget(label('来源路径',10,400,'muted',path_card),1)
    view.copy_path_button=button('复制路径','copy',variant='secondary',parent=path_card);view.copy_path_button.setFixedHeight(26)
    view.copy_path_button.setProperty('previewCopyPath',True)
    view.copy_path_button.setIconSize(QSize(12,12));view.copy_path_button.clicked.connect(view.copy_path)
    path_styles=list(view.copy_path_button._reference_style)
    path_styles=[style+f' QPushButton{{font-size:{12 if sys.platform=="win32" else 11}px;padding:2px 8px;}}' for style in path_styles]
    css(view.copy_path_button,*path_styles)
    path_head.addWidget(view.copy_path_button);path_box.addLayout(path_head)
    view.path_label=elided_type(path_card);view.path_label.setProperty('flickdockTechnicalFont',True);view.path_label.setFixedHeight(18);view.path_label.setAccessibleName('内容来源路径')
    css(view.path_label,"color:#A1A1AA;background:transparent;border:0;font:10px 'FlickDock UI';",
        "color:#636366;background:transparent;border:0;font:10px 'FlickDock UI';")
    path_box.addWidget(view.path_label);body.addWidget(path_card)
    root.addWidget(view.content_scroll,1)
    footer=QFrame(view);view.action_footer=footer;footer.setObjectName('referencePreviewActions')
    css(footer,"QFrame#referencePreviewActions{background:transparent;border:0;}")
    actions=QHBoxLayout(footer)
    actions.setContentsMargins(PREVIEW_ACTION_SIDE_MARGIN_PX,8,PREVIEW_ACTION_SIDE_MARGIN_PX,12)
    actions.setSpacing(0)
    view.send_control,view.send_button,view.send_menu_button=split_send_button(footer,PREVIEW_ACTION_HEIGHT_PX,preview=True)
    view.send_control.setMinimumWidth(0)
    view.send_button.clicked.connect(view.send_current)
    view.send_menu_button.clicked.connect(lambda:view.open_send_menu([view.current_path],view.send_menu_button))

    view.open_button=button('打开','open-external',variant='secondary',parent=footer)
    view.open_button.setProperty('previewOpen',True)
    view.open_button.setFixedSize(PREVIEW_ACTION_OPEN_WIDTH_PX,PREVIEW_ACTION_HEIGHT_PX)
    view.open_button.setIconSize(QSize(PREVIEW_ACTION_ICON_SIZE_PX,PREVIEW_ACTION_ICON_SIZE_PX))
    view.open_button.setProperty('iconTextGap',PREVIEW_ACTION_ICON_TEXT_GAP_PX)
    view.open_button.setProperty('iconTextReservedWidth',8)
    view.open_button._reference_icon=('open-external','text')
    view.open_button.setIcon(icon('open-external',palette()['text']))
    open_styles=[]
    for theme in ('dark','light'):
        c=colors(theme)
        open_styles.append(f"QPushButton{{background:transparent;color:{c['text']};border:0;padding:0;font:500 12px 'FlickDock UI';}} "
                           f"QPushButton:disabled{{color:{c['muted']};}}")
    css(view.open_button,*open_styles)

    icon_group=_PreviewActionSurface(footer);view.action_icon_group=icon_group;icon_group.setObjectName('previewIconActions')
    icon_group.setFixedSize(PREVIEW_ACTION_HEIGHT_PX*3,PREVIEW_ACTION_HEIGHT_PX)
    css(icon_group,'QFrame#previewIconActions{background:transparent;border:0;}')
    icons=QHBoxLayout(icon_group);icons.setContentsMargins(0,0,0,0);icons.setSpacing(0)
    view.rename_button=button('','rename',parent=icon_group)
    view.open_button.clicked.connect(lambda:view.current_path and view.shelf.open_path(view.current_path));view.rename_button.clicked.connect(lambda:view.current_path and view.shelf.rename_ref(view.current_path))
    view.zip_button=button('','package',parent=icon_group);view.zip_button.clicked.connect(view.zip_current)
    view.remove_button=button('','trash',variant='danger',parent=icon_group,control_type=HoldRemoveButton)
    view.remove_button.setProperty('iconOnlyHold',True)
    view.remove_button.identity=lambda:view.current_path
    view.remove_button.confirmed.connect(view.remove_current)
    for control,title in ((view.remove_button,'长按移出搁架'),(view.zip_button,'打包 ZIP'),(view.rename_button,'重命名')):
        control.setFixedSize(PREVIEW_ACTION_HEIGHT_PX,PREVIEW_ACTION_HEIGHT_PX)
        control.setIconSize(QSize(PREVIEW_ACTION_ICON_SIZE_PX,PREVIEW_ACTION_ICON_SIZE_PX))
        control.setToolTip(title);control.setAccessibleName(title)
        styles=[]
        for theme in ('dark','light'):
            c=colors(theme)
            styles.append(f"QPushButton{{background:transparent;border:0;padding:0;color:{c['text']};}} QPushButton:disabled{{color:{c['muted']};}}")
        css(control,*styles);icons.addWidget(control)
    actions.addWidget(icon_group)
    actions.addSpacing(PREVIEW_ACTION_SPACING_PX)
    actions.addWidget(view.open_button)
    actions.addSpacing(PREVIEW_ACTION_SPACING_PX)
    actions.addWidget(view.send_control,1)
    for previous,current in zip((view.remove_button,view.zip_button,view.rename_button,view.open_button,view.send_button),
                                (view.zip_button,view.rename_button,view.open_button,view.send_button,view.send_menu_button)):
        QWidget.setTabOrder(previous,current)
    view.action_buttons=(view.open_button,view.rename_button,view.copy_path_button,view.zip_button,view.send_button,view.remove_button)
    for control,title in ((view.open_button,'打开文件'),(view.copy_path_button,'复制路径'),(view.send_button,'发送文件')):
        control.setAccessibleName(title)
    root.addWidget(footer)
    view.transfer_overlay=TransferOverlay(view.stack,lambda:view.shelf.content.transfers.get(view.current_path))
    view.group_view=QFrame(view);view.group_view.setObjectName('referenceGroupPreview')
    css(view.group_view,"QFrame#referenceGroupPreview{background:transparent;border:0;}")
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

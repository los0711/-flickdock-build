"""Local input-only focus feedback, independent of clipboard/input handling.

Call install_focus_tree once after constructing a form and append focus_qss
after its existing stylesheet. Dynamic inputs can use install_focus directly.
The overlay never accepts input and only paints while focused or fading.
"""
from __future__ import annotations
from surface_morph import rounded_rect_path,draw_rounded_rect
import re
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QRectF,Qt
from frame_animation import FrameAnimation as QVariantAnimation
from PySide6.QtGui import QColor,QPainter,QPen
from PySide6.QtWidgets import (QWidget,QLineEdit,QPlainTextEdit,QTextEdit,
                              QAbstractSpinBox,QKeySequenceEdit,QApplication)
from shiboken6 import isValid

INPUT_TYPES=(QLineEdit,QPlainTextEdit,QTextEdit,QAbstractSpinBox,QKeySequenceEdit)


def focus_qss(scope,neutral_border):
    """Replace only static blue :focus border for opted-in, non-error fields."""
    selectors=','.join(f'{scope} {kind}[focusMotion="true"]:focus' for kind in
                       ('QLineEdit','QPlainTextEdit','QTextEdit','QSpinBox','QDoubleSpinBox','QKeySequenceEdit'))
    return f'\n{selectors} {{border-color:{neutral_border};}}\n'


class _FocusLayer(QWidget):
    def __init__(self,owner,controller):
        super().__init__(owner);self.controller=controller
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setStyleSheet('background:transparent;border:0;')
        self.setFocusPolicy(Qt.NoFocus);self.hide()

    def paintEvent(self,event):
        control=self.controller
        if control.value<=0 or control.is_error():return
        from native_ui_common import colors
        theme=control.theme() if callable(control.theme) else control.theme
        color=QColor(colors(theme)['blue']);color.setAlphaF(control.value)
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(color,1.3));p.setBrush(Qt.NoBrush)
        draw_rounded_rect(p,QRectF(self.rect()).adjusted(.7,.7,-.7,-.7),(control.radius,control.radius))


class InputFocus(QObject):
    def __init__(self,owner,*,theme='light',radius=8):
        super().__init__(owner);self.owner=owner;self.theme=theme;self.radius=radius;self.value=0.
        self.layer=_FocusLayer(owner,self);self.animation=QVariantAnimation(self)
        self.animation.setDuration(160)
        from ui_transitions import exit_curve
        self.animation.setEasingCurve(exit_curve());self.animation.valueChanged.connect(self._frame)
        owner.installEventFilter(self)
        if isinstance(owner,(QAbstractSpinBox,QKeySequenceEdit)):
            for editor in owner.findChildren(QLineEdit):editor.installEventFilter(self)
        self._update_error()
        focused=QApplication.focusWidget()
        if owner.hasFocus() or focused is not None and owner.isAncestorOf(focused):self._target(1.,False)

    def is_error(self):
        for name in ('error','invalid','validationError','validationState'):
            value=self.owner.property(name)
            if value is True or isinstance(value,str) and value.lower() in ('true','error','invalid'):return True
        # Preserve an explicitly red per-field error border as well as the
        # standard validation properties. No text/password value is inspected.
        return bool(re.search(r'border(?:-color)?\s*:[^;}]*?(?:\bred\b|#ff453a\b|#ff3b30\b|#ef4444\b|#dc2626\b)',
                              self.owner.styleSheet(),re.IGNORECASE))

    def _update_error(self):
        enabled=not self.is_error()
        previous=self.owner.property('focusMotion')
        if previous!=enabled:
            self.owner.setProperty('focusMotion',enabled)
            # Let Qt repolish internally. A Python style() wrapper can become
            # invalid during Cocoa style replacement / nested layout delivery.
            style=re.sub(r'\n?/\* focus-motion:[01] \*/','',self.owner.styleSheet())
            self.owner.setStyleSheet(style+f'\n/* focus-motion:{int(enabled)} */')
        if not enabled:self.animation.stop();self._frame(0.)
        elif previous is False:
            focused=QApplication.focusWidget()
            if self.owner.hasFocus() or focused is not None and self.owner.isAncestorOf(focused):self._target(1.,False)

    def _frame(self,value):
        from native_ui_common import reduced_motion
        if self.animation.state()==self.animation.State.Running and reduced_motion():
            self.animation.stop();value=self.animation.endValue()
        self.value=max(0.,min(1.,float(value)))
        if not isValid(self.owner) or not isValid(self.layer):return
        self.layer.setGeometry(self.owner.rect())
        self.layer.setVisible(self.value>0 and self.owner.isVisible() and not self.is_error())
        self.layer.raise_();self.layer.update()

    def _target(self,value,animate):
        from native_ui_common import reduced_motion
        self.animation.stop()
        if self.is_error() or not self.owner.isEnabled():self._frame(0.);return
        if not animate or not self.owner.isVisible() or reduced_motion():self._frame(value);return
        if self.value==value:return
        self.animation.setStartValue(self.value);self.animation.setEndValue(value);self.animation.start()

    def eventFilter(self,watched,event):
        owner=getattr(self,'owner',None)
        if owner is None or not hasattr(self,'layer') or not hasattr(self,'animation'):return False
        if QCoreApplication.closingDown() or not isValid(owner) or not isValid(self.layer):return False
        kind=event.type()
        if kind in (QEvent.FocusIn,QEvent.FocusOut):
            self._target(1. if kind==QEvent.FocusIn else 0.,event.reason()==Qt.MouseFocusReason)
        elif kind==QEvent.Hide:self.animation.stop();self._frame(0.)
        elif kind in (QEvent.Resize,QEvent.Show):self._frame(self.value)
        elif kind==QEvent.EnabledChange and not self.owner.isEnabled():self.animation.stop();self._frame(0.)
        elif kind==QEvent.DynamicPropertyChange:
            if bytes(event.propertyName()) in (b'error',b'invalid',b'validationError',b'validationState'):
                self._update_error()
        elif kind in (QEvent.StyleChange,QEvent.PaletteChange):self._update_error();self.layer.update()
        return False


def install_focus(widget,*,theme='light',radius=8):
    """Idempotent; spin/shortcut internal editors draw one outer focus ring."""
    if isinstance(widget,QLineEdit) and isinstance(widget.parentWidget(),(QAbstractSpinBox,QKeySequenceEdit)):
        widget=widget.parentWidget()
    if not isinstance(widget,INPUT_TYPES):return None
    control=getattr(widget,'_input_focus_motion',None)
    if control is None:
        control=InputFocus(widget,theme=theme,radius=radius);widget._input_focus_motion=control
    else:control.theme=theme;control.radius=radius;control.layer.update()
    return control


def install_focus_tree(owner,*,theme='light',radius=8):
    controls=[]
    for widget in (owner,*owner.findChildren(QWidget)):
        if isinstance(widget,INPUT_TYPES):
            target=widget.parentWidget() if isinstance(widget,QLineEdit) and isinstance(widget.parentWidget(),(QAbstractSpinBox,QKeySequenceEdit)) else widget
            control=getattr(target,'_input_focus_motion',None)
            if control is None:
                control=install_focus(target,theme=theme,radius=radius);control.tree_owner=owner
            elif getattr(control,'tree_owner',None) is owner:
                install_focus(target,theme=theme,radius=radius)
            # A containing settings window must not replace a nested account
            # modal's 12px ring/theme with the settings field's 8px geometry.
            if control not in controls:controls.append(control)
    return controls
